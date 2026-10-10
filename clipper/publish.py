"""File de publication par chaine (SPEC-74e9 4, ADR-35b7 3).

Bibliotheque, pas une etape (ADR-b16b) : lit les sidecars de clip
(output/<video_id>/<clip_id>.json, SPEC-6a47) et les creneaux de la chaine
via clipper.channel.next_slots. Une entree par clip dans
state/publish/<chaine>.json (liste JSON, ecriture atomique tmp+replace ; chaque
cycle lecture-modification-ecriture est sous verrou de fichier
inter-processus, l'API web et le worker etant deux processus).

Le re-rendu du titre d'ecran et l'autopost ne sont pas ici (voir la tache
pipeline / worker).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from clipper import channel as channel_mod
from clipper import tiktok, youtube
from clipper.config import ConfigError

log = logging.getLogger(__name__)

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

CONFIG_DEFAULTS: dict[str, object] = {
    "state_dir": "state/publish",
    "series_default_interval_h": 4,  # intervalle propose par defaut dans le formulaire « Programmer une serie »
}

TIKTOK_STATES = ("published", "scheduled_on_tiktok")
YOUTUBE_STATES = ("published", "scheduled_on_youtube")
SERVICE_STATES = {"tiktok": TIKTOK_STATES, "youtube": YOUTUBE_STATES}
SERVICE_LABELS = {"tiktok": "TikTok", "youtube": "YouTube"}
PUBLISH_MODES = ("immediate", "scheduled")
_POSTPONE_FIRST_BATCH = 8
_POSTPONE_MAX_SLOTS = 512
# ``refused_by_platform`` (TASK-7f582251f6c5) : TikTok a signale un probleme a la verification de contenu ; le clip
# n'est pas publie, il sort des clips disponibles et n'est jamais republie tout seul. Le compte, lui, continue.
REFUSED_BY_PLATFORM = "refused_by_platform"
# ``removed_from_platform`` (TASK-5a7b750462c4) : l'utilisateur a supprime a la main le post (programme ou en ligne)
# sur TikTok/YouTube et le declare dans Clipper, qui n'efface rien lui-meme ; l'entree ne compte plus ni pour les
# creneaux, ni pour les plafonds, ni pour l'apprentissage, et n'est jamais republiee.
REMOVED_FROM_PLATFORM = "removed_from_platform"
VALID_STATUSES = ("approved", "scheduled", "published", "failed", "rejected", REFUSED_BY_PLATFORM, REMOVED_FROM_PLATFORM)
# Entrees pas encore closes (ni publiees, ni refusees) : supprimer leur style les abandonnerait en silence
# (revue r-comptes 7), et set_channel ne doit pas attribuer un style a une video qui en a dans _sans_chaine.
UNFINISHED_STATUSES = ("approved", "scheduled", "failed")
_NON_EDITABLE_STATUSES = ("scheduled", "published")
_NON_MOVABLE_STATUSES = ("rejected", "published", REFUSED_BY_PLATFORM, REMOVED_FROM_PLATFORM)
_PREVIOUS_PART_STATUSES = ("approved", "scheduled", "published")
# Reglages d'une publication du formulaire (SPEC-1ed3 R2) : approve les jetterait en reconstruisant l'entree
# (revue fable-comptes 3 / fable-publication I3), elle se reprend par « Reessayer » ou « Modifier ».
_FORM_FIELDS = ("manual", "publish_mode", "post_options")
_ENTRY_FIELDS = (
    "video_id", "clip_id", "series_id", "part", "status",
    "slot_at", "decided_at", "published_at", "error",
)


# File des clips d'une video sans chaine (SPEC-1ed3 R3) : state/publish/_sans_chaine.json ; le compte de
# chaque entree suffit, aucun preset n'est lu.
NO_CHANNEL = "_sans_chaine"
_NO_CHANNEL_LABEL = "Sans chaîne"


class PublishError(Exception):
    """Entree invalide, clip inconnu/non pret, ou creneau invalide/pris."""


class LimitError(PublishError):
    """Plafond du compte depasse (SPEC-1ed3 R4) : ``reason`` le dit, ``next_at`` est la prochaine heure possible
    (None si aucune dans les 60 jours)."""

    def __init__(self, reason: str, next_at: datetime | None, tz: ZoneInfo) -> None:
        when = next_at.astimezone(tz).strftime("%Y-%m-%d %H:%M") if next_at else "aucune dans les 60 jours"
        super().__init__(f"{reason} ; prochaine heure possible : {when}")
        self.reason, self.next_at = reason, next_at


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _state_dir(state_dir: str | Path | None) -> Path:
    return Path(state_dir) if state_dir is not None else Path(CONFIG_DEFAULTS["state_dir"])


def _state_path(channel: str, state_dir: str | Path | None) -> Path:
    return _state_dir(state_dir) / f"{channel}.json"


def _validate_entry(entry: Any, path: Path) -> None:
    if not isinstance(entry, dict):
        raise PublishError(f"entree invalide dans {path} : pas un objet ({entry!r})")
    for name in _ENTRY_FIELDS:
        if name not in entry:
            raise PublishError(f"entree invalide dans {path} : champ {name!r} manquant")
    if entry["status"] not in VALID_STATUSES:
        raise PublishError(
            f"entree invalide dans {path} : champ 'status' invalide {entry['status']!r} "
            f"(attendu : {' | '.join(VALID_STATUSES)})"
        )


def _load_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise PublishError(f"fichier de publication invalide (pas une liste) : {path}")
    for entry in data:
        _validate_entry(entry, path)
    return data


def _save_entries(path: Path, entries: list[dict[str, Any]]) -> None:
    channel_mod.atomic_write_json(path, entries)


def _locked(path: Path):
    return channel_mod.file_lock(path)


def _refuse_in_progress(entry: dict[str, Any], what: str) -> None:
    if entry.get("in_progress_since"):
        raise PublishError(
            f"{what} refusé pour {entry['video_id']}/{entry['clip_id']} : la publication est en cours "
            "(le worker pilote TikTok), attends sa fin"
        )


def _refuse_to_verify(entry: dict[str, Any], what: str) -> None:
    """Une entree ``failed`` + ``to_verify`` (programmation partie sans id de post : le post est peut-etre deja
    programme sur TikTok) ne redevient jamais republiable par une action ordinaire (audit 10/10, publication-I1) :
    seul ``retry``, apres controle dans TikTok Studio, la relance ; ``resolve_to_verify`` la leve si le releve
    retrouve le post."""
    if entry.get("status") == "failed" and entry.get("to_verify"):
        raise PublishError(
            f"{what} refusé pour {entry['video_id']}/{entry['clip_id']} : programmation à vérifier (le post est "
            "peut-être déjà programmé sur TikTok, risque de doublon) : contrôle TikTok Studio, puis « Réessayer » "
            "s'il n'y est pas, ou attends le prochain relevé s'il y est"
        )


def _find_entry(entries: list[dict[str, Any]], video_id: str, clip_id: str) -> dict[str, Any] | None:
    for entry in entries:
        if entry["video_id"] == video_id and entry["clip_id"] == clip_id:
            return entry
    return None


def _upsert_entry(entries: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    for i, existing in enumerate(entries):
        if existing["video_id"] == entry["video_id"] and existing["clip_id"] == entry["clip_id"]:
            entries[i] = entry
            return
    entries.append(entry)


def channel_settings(channel: str, presets_dir: str | Path, base: str | Path) -> dict[str, Any]:
    """Le [channel] valide d'une chaine ; pour ``NO_CHANNEL`` (video sans chaine) les valeurs par defaut :
    aucun creneau, aucun compte par defaut (le compte est choisi par publication)."""
    if channel == NO_CHANNEL:
        return {**channel_mod.CONFIG_DEFAULTS, "display_name": _NO_CHANNEL_LABEL, "mode": ""}
    return channel_mod.load_channel(channel, presets_dir=presets_dir, base=base)[1]


def _sidecar_path(output_dir: str | Path, video_id: str, clip_id: str) -> Path:
    return Path(output_dir) / video_id / f"{clip_id}.json"


def _read_sidecar(output_dir: str | Path, video_id: str, clip_id: str) -> dict[str, Any]:
    path = _sidecar_path(output_dir, video_id, clip_id)
    if not path.exists():
        raise PublishError(f"clip introuvable : {video_id}/{clip_id} ({path})")
    return json.loads(path.read_text(encoding="utf-8"))


def read_sidecar(output_dir: str | Path, video_id: str, clip_id: str) -> dict[str, Any]:
    """Le sidecar d'un clip (SPEC-6a47) ; ``PublishError`` s'il est absent."""
    return _read_sidecar(output_dir, video_id, clip_id)


def _write_sidecar(output_dir: str | Path, video_id: str, clip_id: str, data: dict[str, Any]) -> None:
    # meme mecanisme que la file (reessais sur PermissionError sous Windows), pas une copie divergente
    channel_mod.atomic_write_json(_sidecar_path(output_dir, video_id, clip_id), data)


def _series_info(video_id: str, clip_id: str, sidecar: dict[str, Any]) -> tuple[str | None, int | None]:
    part = sidecar.get("part")
    parts_total = sidecar.get("parts_total") or 1
    if part is None or parts_total <= 1:
        return None, None
    base = clip_id.rsplit("-p", 1)[0] if "-p" in clip_id else clip_id
    return f"{video_id}:{base}", part


def _sibling_clip_ids(output_dir: str | Path, video_id: str, series_id: str, *, exclude: str) -> list[str]:
    out_dir = Path(output_dir) / video_id
    siblings = []
    for path in sorted(out_dir.glob("*.json")):
        clip_id = path.stem
        if clip_id == exclude:
            continue
        try:
            sidecar = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PublishError(f"sidecar de clip illisible (JSON corrompu) : {path} ({exc})") from exc
        other_series_id, _ = _series_info(video_id, clip_id, sidecar)
        if other_series_id == series_id:
            siblings.append(clip_id)
    return siblings


def series_clip_ids(video_id: str, clip_id: str, output_dir: str | Path = "output") -> list[str]:
    """Tous les clip_id de la serie de ``clip_id`` (lui compris), tries par numero de partie ;
    ``[clip_id]`` si le clip n'appartient a aucune serie (TASK-e99b : cocher une partie entraine
    toute sa serie). Leve ``PublishError`` si le sidecar de ``clip_id`` est introuvable ou
    illisible (ADR-ad2e : pas de repli silencieux sur un clip absent)."""
    sidecar = _read_sidecar(output_dir, video_id, clip_id)
    series_id, part = _series_info(video_id, clip_id, sidecar)
    if series_id is None:
        return [clip_id]
    members = [(part, clip_id)]
    for sibling_id in _sibling_clip_ids(output_dir, video_id, series_id, exclude=clip_id):
        sibling_sidecar = _read_sidecar(output_dir, video_id, sibling_id)
        _, sibling_part = _series_info(video_id, sibling_id, sibling_sidecar)
        members.append((sibling_part, sibling_id))
    members.sort(key=lambda m: (m[0] if m[0] is not None else 0))
    return [cid for _, cid in members]


def _next_free_slot(schedule: dict[str, Any], taken: set[datetime], after: datetime) -> datetime:
    """``taken`` : instants (``datetime``) deja occupes par le compte, compares comme des instants (SPEC-6076 R2) :
    le meme instant en +00:00 ou +02:00 est le meme creneau, jamais deux chaines ISO differentes."""
    n = len(taken) + 1
    while True:
        candidates = channel_mod.next_slots(schedule, after, n)
        for candidate in candidates:
            if candidate not in taken:
                return candidate
        if len(candidates) < n:
            raise PublishError("aucun creneau disponible pour ce compte")
        n += 1


def _require_previous_part(
    entries: list[dict[str, Any]], output_dir: str | Path, video_id: str,
    clip_id: str, series_id: str, part: int,
) -> dict[str, Any]:
    """La partie N>1 d'une serie ne s'approuve que si la partie N-1 est deja
    approved, scheduled ou published : l'ordre des creneaux de la serie est garanti. Rend l'entree de la
    partie N-1 (son creneau borne celui de la partie N)."""
    previous = None
    for sibling_id in _sibling_clip_ids(output_dir, video_id, series_id, exclude=clip_id):
        _, sibling_part = _series_info(video_id, sibling_id, _read_sidecar(output_dir, video_id, sibling_id))
        if sibling_part == part - 1:
            previous = sibling_id
            break
    if previous is None:
        raise PublishError(f"partie {part - 1} introuvable pour approuver {video_id}/{clip_id} (partie {part})")
    entry = _find_entry(entries, video_id, previous)
    status = entry["status"] if entry is not None else "absente de la file"
    if entry is None or status not in _PREVIOUS_PART_STATUSES:
        raise PublishError(
            f"approbation refusee pour {video_id}/{clip_id} (partie {part}) : la partie {part - 1} "
            f"({previous}) doit etre approved, scheduled ou published, elle est {status!r}"
        )
    return entry


def approval_refusal(entry: dict[str, Any] | None) -> str | None:
    """Pourquoi ``approve`` refuse l'entree existante d'un clip, ou None : la meme regle sert a l'approbation
    groupee, validee en entier avant la premiere ecriture (revue fable-comptes 2 / fable-publication M1)."""
    if entry is None:
        return None
    if entry.get("in_progress_since"):
        return "la publication est en cours (le worker pilote TikTok), attends sa fin"
    if entry["status"] not in ("approved", "failed"):
        return f"statut {entry['status']!r}"
    form = [name for name in _FORM_FIELDS if entry.get(name)]
    if form:
        return (f"publication du formulaire ({', '.join(form)}) : utilise « Réessayer » ou « Modifier » dans "
                "l'écran Publication, l'approbation perdrait ses réglages")
    return None


def approve(
    video_id: str,
    clip_id: str,
    channel: str,
    *,
    now: datetime | None = None,
    output_dir: str | Path = "output",
    state_dir: str | Path | None = None,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
    account: str | None = None,
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Approuve un clip (SPEC-74e9 4.2) : entree 'approved', puis 'scheduled'
    au prochain creneau libre si le compte en a. Leve PublishError si le
    sidecar dit ready=false ou si ``account`` manque : le compte de publication (SPEC-6076 R2) se choisit a
    chaque publication, un style n'en porte plus. ``schedule`` : les creneaux du compte
    (``accounts.schedule_of``) ; sans creneau, l'entree reste 'approved'."""
    sidecar = _read_sidecar(output_dir, video_id, clip_id)
    if not sidecar.get("ready"):
        raise PublishError(f"clip non pret pour publication : {video_id}/{clip_id}")
    if not account:
        raise PublishError("compte de publication manquant : choisis un compte prêt à publier")

    series_id, part = _series_info(video_id, clip_id, sidecar)
    channel_settings(channel, presets_dir, base)  # style inconnu ou illisible : erreur explicite

    path = _state_path(channel, state_dir)
    now_dt = _now(now)

    entry: dict[str, Any] = {
        "video_id": video_id,
        "clip_id": clip_id,
        "series_id": series_id,
        "part": part,
        "status": "approved",
        "slot_at": None,
        "decided_at": _iso(now_dt),
        "published_at": None,
        "error": None,
        "account": account,
    }
    with _locked(path):
        entries = _load_entries(path)
        refusal = approval_refusal(_find_entry(entries, video_id, clip_id))
        if refusal is not None:
            raise PublishError(f"approbation refusé pour {video_id}/{clip_id} : {refusal}")
        after = now_dt
        if series_id is not None and part is not None and part > 1:
            previous = _require_previous_part(entries, output_dir, video_id, clip_id, series_id, part)
            if previous.get("slot_at"):  # jamais un creneau avant la partie N-1 (revue fable-comptes 6)
                after = max(now_dt, datetime.fromisoformat(previous["slot_at"]))
        if schedule and schedule["slots"]:
            taken = _account_taken_slots(account, state_dir, presets_dir, base, exclude=(video_id, clip_id))
            slot = _next_free_slot(schedule, taken, after)
            entry["status"] = "scheduled"
            entry["slot_at"] = _iso(slot)

        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def reject(
    video_id: str,
    clip_id: str,
    channel: str,
    *,
    now: datetime | None = None,
    output_dir: str | Path = "output",
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Rejette un clip ; si c'est une partie d'une serie, rejette aussi
    toutes les autres parties (SPEC-74e9 4.2)."""
    sidecar = _read_sidecar(output_dir, video_id, clip_id)
    series_id, part = _series_info(video_id, clip_id, sidecar)

    path = _state_path(channel, state_dir)
    now_dt = _now(now)
    with _locked(path):
        entries = _load_entries(path)
        return _reject_locked(
            entries, path, video_id, clip_id, series_id, part, now_dt, output_dir,
        )


def _reject_locked(
    entries: list[dict[str, Any]], path: Path, video_id: str, clip_id: str,
    series_id: str | None, part: int | None, now_dt: datetime, output_dir: str | Path,
) -> dict[str, Any]:
    members: list[tuple[str, str | None, int | None]] = [(clip_id, series_id, part)]
    if series_id is not None:
        for sibling_id in _sibling_clip_ids(output_dir, video_id, series_id, exclude=clip_id):
            sibling_sidecar = _read_sidecar(output_dir, video_id, sibling_id)
            _, sibling_part = _series_info(video_id, sibling_id, sibling_sidecar)
            members.append((sibling_id, series_id, sibling_part))

    # Verifie TOUS les membres (le clip et ses parties sœurs) avant la moindre ecriture (ADR-ad2e) :
    # un clip publie, ou en cours de pilotage, ne doit jamais devenir 'rejected', meme via une de ses
    # parties sœurs.
    for cid, _sid, _part in members:
        existing = _find_entry(entries, video_id, cid)
        if existing is not None:
            _refuse_in_progress(existing, "refus")
            if existing["status"] == "published":
                raise PublishError(
                    f"refus refusé pour {video_id}/{cid} : statut 'published'"
                )

    def _reject_one(cid: str, series_id: str | None, part: int | None) -> dict[str, Any]:
        existing = _find_entry(entries, video_id, cid)
        entry = dict(existing) if existing is not None else {
            "video_id": video_id, "clip_id": cid, "series_id": series_id, "part": part,
            "status": "rejected", "slot_at": None, "decided_at": None,
            "published_at": None, "error": None,
        }
        entry["status"] = "rejected"
        entry["slot_at"] = None
        entry["decided_at"] = _iso(now_dt)
        _upsert_entry(entries, entry)
        return entry

    rejected = [_reject_one(cid, sid, p) for cid, sid, p in members]
    _save_entries(path, entries)
    return rejected[0]


def move(
    video_id: str,
    clip_id: str,
    channel: str,
    slot_at: datetime,
    *,
    state_dir: str | Path | None = None,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deplace un clip vers un creneau libre du compte (SPEC-74e9 4.3, SPEC-6076 R2) :
    refuse un creneau deja pris ou hors des creneaux du compte (``schedule`` : ``accounts.schedule_of``)."""
    path = _state_path(channel, state_dir)
    channel_settings(channel, presets_dir, base)  # style inconnu ou illisible : erreur explicite
    if not schedule or not schedule["slots"]:
        raise PublishError(f"déplacement impossible pour {video_id}/{clip_id} : le compte n'a aucun créneau (écran Comptes)")
    with _locked(path):
        return _move_locked(path, video_id, clip_id, slot_at, schedule, state_dir=state_dir,
                             presets_dir=presets_dir, base=base)


def _move_locked(
    path: Path, video_id: str, clip_id: str, slot_at: datetime, schedule: dict[str, Any],
    *, state_dir: str | Path | None = None, presets_dir: str | Path = "presets", base: str | Path = "config.toml",
) -> dict[str, Any]:
    entries = _load_entries(path)
    entry = _find_entry(entries, video_id, clip_id)
    if entry is None:
        raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
    if entry["status"] in _NON_MOVABLE_STATUSES:
        raise PublishError(
            f"deplacement refuse pour {video_id}/{clip_id} : statut {entry['status']!r}"
        )
    _refuse_in_progress(entry, "déplacement")
    _refuse_to_verify(entry, "déplacement")

    tz = ZoneInfo(str(schedule["timezone"]))
    local_slot = slot_at.astimezone(tz)
    day = _DAYS[local_slot.weekday()]
    time_str = local_slot.strftime("%H:%M")
    if not any(s["day"] == day and s["time"] == time_str for s in schedule["slots"]):
        raise PublishError(f"creneau hors des creneaux du compte pour {video_id}/{clip_id} : {slot_at}")

    # Creneaux pris = instants de TOUTES les entrees du compte, tous styles confondus (SPEC-6076 R2),
    # compares comme des instants (le meme instant en +00:00 ou +02:00 est le meme creneau).
    taken = _account_taken_slots(entry_account(entry), state_dir, presets_dir, base, exclude=(video_id, clip_id))
    if slot_at in taken:
        raise PublishError(f"creneau deja pris pour {video_id}/{clip_id} : {slot_at}")

    entry = dict(entry)
    entry["slot_at"] = _iso(slot_at)
    entry["status"] = "scheduled"
    _upsert_entry(entries, entry)
    _save_entries(path, entries)
    return entry


def mark_published(
    video_id: str,
    clip_id: str,
    channel: str,
    *,
    now: datetime | None = None,
    state_dir: str | Path | None = None,
    output_dir: str | Path = "output",
    tiktok_state: str | None = None,
    post_url: str | None = None,
    post_id: str | None = None,
    publish_at: str | None = None,
    post_note: str | None = None,
    account: str | None = None,
    service: str = "tiktok",
) -> dict[str, Any]:
    """Marque un clip publie (SPEC-74e9 4.3). A la main, sans argument de plus ; apres une
    publication TikTok (SPEC-9225 R3), ``tiktok_state`` (``published`` | ``scheduled_on_tiktok``),
    l'URL ou l'id du post et ``publish_at`` (l'instant ou le post est en ligne) sont enregistres
    dans l'entree ET dans le sidecar du clip (champ ``tiktok_post``). Pour un compte YouTube (SPEC-5e50 R2),
    ``service="youtube"`` : etat ``published`` | ``scheduled_on_youtube``, URL ``youtube.com/shorts/<id>``,
    sidecar ``youtube_post`` (les champs ``tiktok_state`` / ``tiktok_publish_at`` de l'entree servent aux deux services)."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        if entry["status"] != "scheduled":
            raise PublishError(
                f"publication manuelle refusee pour {video_id}/{clip_id} : statut {entry['status']!r} "
                "(attendu : 'scheduled')"
            )
        if tiktok_state is None:  # declaration a la main : jamais pendant que le worker pilote (fable-publication I2)
            _refuse_in_progress(entry, "déclaration publiée")

        entry = dict(entry)
        entry["status"] = "published"
        entry["published_at"] = _iso(_now(now))
        entry["error"], entry["capture"], entry["halted"] = None, None, False
        entry["waiting_reason"] = None
        entry["in_progress_since"] = None
        entry["in_progress_pid"], entry["in_progress_pid_created_at"] = None, None
        if tiktok_state is not None:
            if service not in SERVICE_STATES:
                raise PublishError(f"service invalide : {service!r} (attendu : {' | '.join(SERVICE_STATES)})")
            states = SERVICE_STATES[service]
            if tiktok_state not in states:
                raise PublishError(f"etat {SERVICE_LABELS[service]} invalide : {tiktok_state!r} "
                                   f"(attendu : {' | '.join(states)})")
            entry.update(tiktok_state=tiktok_state, post_url=post_url, post_id=post_id,
                         tiktok_publish_at=publish_at, post_note=post_note, service=service)
        # l'etat de file (preuve que le post est parti) d'abord : un sidecar qui echoue ensuite ne laisse
        # jamais l'entree « en cours » ni republiable (TASK-0c97)
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    if tiktok_state is not None:
        try:
            sidecar = _read_sidecar(output_dir, video_id, clip_id)
            sidecar["tiktok_post" if service == "tiktok" else "youtube_post"] = {
                "url": post_url, "id": post_id, "state": tiktok_state, "publish_at": publish_at,
                "account": account, "note": post_note,
            }
            _write_sidecar(output_dir, video_id, clip_id, sidecar)
        except (OSError, PublishError) as exc:
            log.error("%s/%s : post parti (%s) mais sidecar non réécrit : %s ; l'entrée reste publiée",
                      video_id, clip_id, post_url or post_id or "sans lien", exc)
    return entry


def attach_post(
    video_id: str,
    clip_id: str,
    channel: str,
    *,
    post_url: str | None,
    post_id: str,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Rattache apres coup un post TikTok a l'entree de publication d'un clip (releve des Publications) :
    ecrit ``post_url`` / ``post_id``. Refuse (``PublishError``) d'ecraser un ``post_id`` deja renseigne et
    different ; le statut de l'entree ne change pas."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        known = entry.get("post_id")
        if known and str(known) != str(post_id):
            raise PublishError(
                f"rattachement refusé pour {video_id}/{clip_id} : l'entrée porte déjà le post {known} (reçu {post_id})")
        entry = dict(entry)
        entry["post_id"] = str(post_id)
        entry["post_url"] = post_url
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def mark_failed(
    video_id: str,
    clip_id: str,
    channel: str,
    reason: str,
    *,
    capture: str | Path | None = None,
    halted: bool = False,
    to_verify: bool = False,
    publish_at: str | None = None,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Echec d'une publication (SPEC-9225 R4) : statut ``failed`` avec la raison et la capture
    d'ecran ; ``halted`` arrete le compte tant que l'entree n'est pas reessayee (``retry``). ``to_verify`` :
    la programmation est partie mais sa presence sur le service n'est pas confirmee (aucun id de post) ; l'entree
    n'est jamais « publiee » ni reprogrammee seule, l'utilisateur verifie dans Studio avant de reessayer. `publish_at` :
    heure effective de la programmation quand elle est connue (enregistree en `tiktok_publish_at`)."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        if entry["status"] not in ("approved", "scheduled", "failed"):
            raise PublishError(f"echec impossible pour {video_id}/{clip_id} : statut {entry['status']!r}")
        entry = dict(entry)
        entry.update(status="failed", error=reason, capture=str(capture) if capture else None, halted=halted,
                     failed_at=_iso(_now(now)), waiting_reason=None, in_progress_since=None, in_progress_pid=None,
                     in_progress_pid_created_at=None, to_verify=to_verify)
        if to_verify and publish_at:  # heure reellement programmee (arrondie par TikTok) : sert au rapprochement
            entry["tiktok_publish_at"] = publish_at
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def resolve_to_verify(
    video_id: str,
    clip_id: str,
    channel: str,
    *,
    post_id: str,
    post_url: str | None,
    account: str | None,
    note: str,
    now: datetime | None = None,
    state_dir: str | Path | None = None,
    output_dir: str | Path = "output",
) -> bool:
    """Une entree ``failed`` + ``to_verify`` (programmation sans id de post, ``mark_failed``) dont le post est
    retrouve dans un releve complet du compte repasse en ``published`` / ``scheduled_on_tiktok`` avec son id et
    son adresse ; ``to_verify`` est retire. Jamais de reprogrammation. Vrai seulement si l'entree etait encore
    « a verifier » (le worker ne journalise qu'une fois), faux sinon."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None or entry["status"] != "failed" or not entry.get("to_verify"):
            return False
        entry = dict(entry)
        publish_at = entry.get("tiktok_publish_at") or entry.get("slot_at")  # heure effective si connue (arrondie par TikTok)
        entry.update(status="published", published_at=_iso(_now(now)), error=None, capture=None, halted=False,
                     to_verify=False, waiting_reason=None, in_progress_since=None, in_progress_pid=None,
                     in_progress_pid_created_at=None, tiktok_state="scheduled_on_tiktok",
                     post_url=post_url, post_id=str(post_id), tiktok_publish_at=publish_at, post_note=note,
                     service="tiktok")
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    try:
        sidecar = _read_sidecar(output_dir, video_id, clip_id)
        sidecar["tiktok_post"] = {"url": post_url, "id": str(post_id), "state": "scheduled_on_tiktok",
                                  "publish_at": publish_at, "account": account, "note": note}
        _write_sidecar(output_dir, video_id, clip_id, sidecar)
    except (OSError, PublishError) as exc:
        log.error("%s/%s : post retrouvé (%s) mais sidecar non réécrit : %s ; l'entrée reste publiée",
                  video_id, clip_id, post_url or post_id, exc)
    return True


def flag_missing_on_tiktok(
    video_id: str,
    clip_id: str,
    channel: str,
    note: str,
    *,
    now: datetime | None = None,
    state_dir: str | Path | None = None,
) -> bool:
    """Signale une entree programmee sur TikTok, sans id de post, que le releve complet du compte ne montre pas :
    ``missing_on_tiktok`` et ``post_note`` (visible dans l'ecran Publication). Rend vrai seulement la premiere
    fois (le worker ne journalise qu'une fois) ; faux si l'entree n'est plus dans cet etat."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if (entry is None or entry["status"] != "published" or entry.get("tiktok_state") != "scheduled_on_tiktok"
                or entry.get("post_id") or entry.get("missing_on_tiktok")):
            return False
        entry = dict(entry)
        entry.update(missing_on_tiktok=True, missing_checked_at=_iso(_now(now)), post_note=note)
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return True


def mark_refused_by_platform(
    video_id: str,
    clip_id: str,
    channel: str,
    reason: str,
    *,
    capture: str | Path | None = None,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Probleme signale par TikTok a la verification de contenu (TASK-7f582251f6c5) : rien n'a ete publie, l'entree
    passe en ``refused_by_platform`` avec la raison et la capture. Contrairement a ``mark_failed``, le compte n'est
    jamais arrete (``halted`` faux) et le creneau est libere ; le clip n'est plus republie."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        if entry["status"] not in ("approved", "scheduled", "failed"):
            raise PublishError(f"refus de plateforme impossible pour {video_id}/{clip_id} : statut {entry['status']!r}")
        entry = dict(entry)
        entry.update(status=REFUSED_BY_PLATFORM, error=reason, capture=str(capture) if capture else None, halted=False,
                     refused_at=_iso(_now(now)), slot_at=None, waiting_reason=None, in_progress_since=None,
                     in_progress_pid=None, in_progress_pid_created_at=None)
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def refused_by_platform(
    *, state_dir: str | Path | None = None, presets_dir: str | Path = "presets",
) -> list[tuple[str, dict[str, Any]]]:
    """(file, entree) des clips refuses par TikTok a la verification de contenu, les plus recents d'abord."""
    found = [(name, e) for name, e in all_entries(state_dir=state_dir, presets_dir=presets_dir)
             if e["status"] == REFUSED_BY_PLATFORM]
    return sorted(found, key=lambda f: f[1].get("refused_at") or "", reverse=True)


def mark_removed_from_platform(
    video_id: str,
    clip_id: str,
    channel: str,
    reason: str | None = None,
    *,
    state_dir: str | Path | None = None,
    output_dir: str | Path = "output",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Le post a ete supprime a la main de la plateforme (TASK-5a7b750462c4) : une entree ``published`` (programmee
    sur TikTok/YouTube ou deja en ligne) passe en ``removed_from_platform`` avec ``removed_at`` et la raison
    (facultative), journalisee. Rien n'est fait sur la plateforme. Le creneau est libere, ``tiktok_state`` et le
    lien du post restent lisibles ; le sidecar du clip garde ``removed_from_platform`` pour que l'apprentissage
    ignore le post. Refuse (``PublishError``) toute autre entree, dont une publication en cours."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        _refuse_in_progress(entry, "déclaration supprimé de la plateforme")
        if entry["status"] != "published":
            raise PublishError(
                f"déclaration supprimé de la plateforme refusée pour {video_id}/{clip_id} : statut {entry['status']!r} "
                "(attendu : 'published', programmé ou en ligne sur la plateforme)")
        stamp = _iso(_now(now))
        entry = dict(entry)
        entry.update(status=REMOVED_FROM_PLATFORM, removed_at=stamp, removed_reason=reason or None, slot_at=None,
                     waiting_reason=None, in_progress_since=None, in_progress_pid=None, in_progress_pid_created_at=None,
                     halted=False)
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    log.info("%s/%s : post déclaré supprimé de la plateforme (%s)", video_id, clip_id, reason or "sans raison")
    try:
        sidecar = _read_sidecar(output_dir, video_id, clip_id)
        sidecar["removed_from_platform"] = {"at": stamp, "reason": reason or None}
        _write_sidecar(output_dir, video_id, clip_id, sidecar)
    except (OSError, PublishError) as exc:
        log.error("%s/%s : suppression déclarée mais sidecar non réécrit : %s", video_id, clip_id, exc)
    return entry


def removed_from_platform(
    *, state_dir: str | Path | None = None, presets_dir: str | Path = "presets",
) -> list[tuple[str, dict[str, Any]]]:
    """(file, entree) des posts declares supprimes de la plateforme, les plus recents d'abord."""
    found = [(name, e) for name, e in all_entries(state_dir=state_dir, presets_dir=presets_dir)
             if e["status"] == REMOVED_FROM_PLATFORM]
    return sorted(found, key=lambda f: f[1].get("removed_at") or "", reverse=True)


def retry(
    video_id: str,
    clip_id: str,
    channel: str,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Remet une entree ``failed`` en attente (bouton « Reessayer ») : ``scheduled`` sur son
    creneau (``approved`` sans creneau), raison, capture et arret du compte effaces."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        if entry["status"] != "failed":
            raise PublishError(
                f"reessai refuse pour {video_id}/{clip_id} : statut {entry['status']!r} (attendu : 'failed')"
            )
        entry = dict(entry)
        entry.update(status="scheduled" if entry["slot_at"] else "approved", error=None, capture=None, halted=False,
                     to_verify=False)
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def set_mode(
    video_id: str,
    clip_id: str,
    channel: str,
    mode: str | None,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Mode de publication d'une entree (``immediate`` | ``scheduled``), ou ``None`` pour
    retomber sur ``[tiktok] publish_mode``."""
    if mode not in (None, *PUBLISH_MODES):
        raise PublishError(f"mode de publication invalide : {mode!r} (attendu : {' | '.join(PUBLISH_MODES)})")
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        _refuse_in_progress(entry, "changement de mode")  # fable-publication M4
        _refuse_to_verify(entry, "changement de mode")
        if entry["status"] in ("published", "rejected", REFUSED_BY_PLATFORM, REMOVED_FROM_PLATFORM):
            raise PublishError(f"changement de mode refusé pour {video_id}/{clip_id} : statut {entry['status']!r}")
        entry = dict(entry)
        entry["publish_mode"] = mode
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def list_entries(channel: str, *, state_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Les entrees de state/publish/<chaine>.json (validees)."""
    return _load_entries(_state_path(channel, state_dir))


def adopt_video_entries(
    video_id: str, channel: str, *, commit: Callable[[], None], state_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Attribution d'un style a une video sans style (revue fable-comptes 1) : toutes ses entrees de
    ``_sans_chaine.json`` passent, intactes, dans ``<channel>.json`` ; sinon la console les chercherait sous
    le style, verrait un clip publie « a valider » et l'approbation le republierait. Refuse (``PublishError``,
    rien n'est ecrit) une entree non terminee (le worker peut la piloter) ou un clip deja present dans la file du
    style. ``commit`` (l'ecriture de pipeline.json) est appele sous les deux verrous, apres l'ecriture de la file
    du style et avant le retrait de la file sans style : a aucun moment un clip publie n'est sans entree visible.
    Rend les entrees deplacees."""
    source, target = _state_path(NO_CHANNEL, state_dir), _state_path(channel, state_dir)
    if not source.exists():
        commit()
        return []
    with _locked(source), _locked(target):
        entries = _load_entries(source)
        moved = [e for e in entries if e["video_id"] == video_id]
        pending = [e for e in moved if e["status"] in UNFINISHED_STATUSES]
        if pending:
            clips = ", ".join(e["clip_id"] for e in pending)
            raise PublishError(
                f"{video_id} a {len(pending)} publication(s) sans style en cours ({clips}) : "
                "annule-les ou attends leur fin avant de lui attribuer une chaîne"
            )
        target_existed = target.exists()
        styled = _load_entries(target)
        clash = [e["clip_id"] for e in moved if _find_entry(styled, video_id, e["clip_id"]) is not None]
        if clash:
            raise PublishError(
                f"{video_id} : {', '.join(clash)} déjà dans la file du style « {channel} » et dans celle sans "
                "style : deux publications pour un même clip, corrige state/publish avant de lui attribuer une chaîne"
            )
        if moved:
            _save_entries(target, styled + moved)
        try:
            commit()
        except BaseException:
            if moved and target_existed:
                _save_entries(target, styled)
            elif moved:
                target.unlink(missing_ok=True)
            raise
        if moved:
            _save_entries(source, [e for e in entries if e["video_id"] != video_id])
    return moved


def entry_account(entry: dict[str, Any]) -> str | None:
    """Compte qui publie une entree (SPEC-00d1 R4) : celui enregistre dans l'entree, None s'il n'y en a pas
    (SPEC-6076 R2 : un style n'a plus de compte, jamais un autre compte en repli)."""
    return entry.get("account") or None


def _queue_names(state_dir: str | Path | None, presets_dir: str | Path) -> list[str]:
    """Les files de publication a lire : chaque style, la file sans style, et toute file restee sous
    ``state_dir`` sans preset (style supprime : ses publications comptent toujours pour les plafonds du compte
    et restent visibles, revue fable-comptes 4)."""
    names = [*channel_mod.list_channels(presets_dir), NO_CHANNEL]
    folder = _state_dir(state_dir)
    if folder.is_dir():
        names.extend(sorted(p.stem for p in folder.glob("*.json") if p.stem not in names))
    return names


def _account_entries(
    account: str, state_dir: str | Path | None, presets_dir: str | Path, base: str | Path,
) -> list[tuple[str, dict[str, Any]]]:
    """(chaine, entree) de toutes les entrees publiees par ``account`` (SPEC-00d1 R4), toutes chaines."""
    found: list[tuple[str, dict[str, Any]]] = []
    try:
        for name in _queue_names(state_dir, presets_dir):
            found.extend((name, e) for e in _load_entries(_state_path(name, state_dir))
                         if entry_account(e) == account)
    except (channel_mod.ChannelError, ConfigError) as exc:
        raise PublishError(f"chaines illisibles pour le compte {account} : {exc}") from exc
    return found


def _account_taken_slots(
    account: str | None, state_dir: str | Path | None, presets_dir: str | Path, base: str | Path,
    *, exclude: tuple[str, str] | None = None,
) -> set[datetime]:
    """Instants (``datetime``) deja occupes par ``account``, toutes chaines confondues (SPEC-6076 R2) :
    compares comme des instants, jamais comme des chaines ISO (le meme instant en +00:00 ou +02:00 est
    le meme creneau). ``exclude`` ecarte l'entree qu'on deplace/programme elle-meme."""
    if not account:
        return set()
    taken: set[datetime] = set()
    for _name, entry in _account_entries(account, state_dir, presets_dir, base):
        if exclude is not None and (entry["video_id"], entry["clip_id"]) == exclude:
            continue
        slot_at = entry.get("slot_at")
        if slot_at:
            taken.add(datetime.fromisoformat(slot_at))
    return taken


def account_publish_times(
    account: str, *, state_dir: str | Path | None = None, presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
) -> list[datetime]:
    """Instants des posts deja faits ou programmes du compte, toutes chaines : ``tiktok_publish_at``
    (l'instant ou le post est en ligne), sinon ``published_at`` (publication manuelle)."""
    times = []
    for _name, entry in _account_entries(account, state_dir, presets_dir, base):
        stamp = entry.get("tiktok_publish_at") or entry.get("published_at")
        if entry["status"] == "published" and stamp:
            times.append(datetime.fromisoformat(stamp))
    return sorted(times)


def halted_account(
    account: str, *, state_dir: str | Path | None = None, presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
) -> dict[str, Any] | None:
    """La premiere entree ``failed`` qui arrete le compte (R4), ou None : le worker n'y
    publie plus rien avant son « Reessayer »."""
    for name, entry in _account_entries(account, state_dir, presets_dir, base):
        if entry["status"] == "failed" and entry.get("halted"):
            return {**entry, "channel": name}
    return None


def last_failure(
    account: str, *, state_dir: str | Path | None = None, presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
) -> dict[str, Any] | None:
    """Le dernier echec de publication du compte (entree ``failed`` la plus recente) avec sa chaine, ou None."""
    failed = [(name, e) for name, e in _account_entries(account, state_dir, presets_dir, base) if e["status"] == "failed"]
    if not failed:
        return None
    name, entry = max(failed, key=lambda f: f[1].get("failed_at") or "")
    return {**entry, "channel": name}


def set_account(
    video_id: str,
    clip_id: str,
    channel: str,
    account: str,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Compte de publication d'une entree (SPEC-00d1 R4), modifiable tant qu'elle n'est pas publiee. Que le compte
    soit pret a publier est verifie par l'appelant (la console) et, a la publication, par le worker."""
    if not isinstance(account, str) or not account:
        raise PublishError("compte de publication manquant : un identifiant de compte est attendu")
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        if entry["status"] in ("published", "rejected", REMOVED_FROM_PLATFORM):
            raise PublishError(f"changement de compte refusé pour {video_id}/{clip_id} : statut {entry['status']!r}")
        _refuse_in_progress(entry, "changement de compte")
        entry = dict(entry)
        entry["account"] = account
        entry["waiting_reason"] = None
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def migrate_missing_account(channel: str, account_id: str, *, state_dir: str | Path | None = None) -> int:
    """Reporte ``account_id`` sur les entrees de ``channel`` qui n'en ont encore aucun (migration SPEC-6076 R2,
    revue r-comptes 11) : jamais une entree qui a deja un compte. Rend le nombre d'entrees modifiees."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        changed = [e for e in entries if not e.get("account")]
        for entry in changed:
            entry["account"] = account_id
        if changed:
            _save_entries(path, entries)
    return len(changed)


def set_waiting_reason(
    video_id: str,
    clip_id: str,
    channel: str,
    reason: str | None,
    *,
    state_dir: str | Path | None = None,
) -> bool:
    """Raison pour laquelle une entree ``scheduled`` reste en attente sans etre tentee (compte pas pret, connexion
    expiree ; SPEC-00d1 R4), ou None une fois levee. Rend True si la raison a change."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        if entry.get("waiting_reason") == reason:
            return False
        entry = dict(entry)
        entry["waiting_reason"] = reason
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return True


def postpone(
    video_id: str,
    clip_id: str,
    channel: str,
    reason: str,
    *,
    allowed: Callable[[datetime], str | None],
    schedule: dict[str, Any] | None = None,
    now: datetime | None = None,
    state_dir: str | Path | None = None,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
) -> dict[str, Any]:
    """Reporte une entree ``scheduled`` au prochain creneau libre du compte (``schedule`` :
    ``accounts.schedule_of``) que ``allowed`` accepte (``allowed(creneau)`` rend None, ou la raison du refus :
    plafonds de R6) ; la raison et la nouvelle date sont gardees dans ``postponed_reason``."""
    channel_settings(channel, presets_dir, base)  # style inconnu ou illisible : erreur explicite
    if not schedule or not schedule["slots"]:
        raise PublishError(f"aucun créneau libre et permis pour reporter {video_id}/{clip_id} ({reason}) : "
                           "le compte n'a aucun créneau (écran Comptes)")
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        if entry["status"] != "scheduled" or not entry["slot_at"]:
            raise PublishError(f"report impossible pour {video_id}/{clip_id} : statut {entry['status']!r} (attendu : 'scheduled')")
        # Creneaux pris = instants de TOUTES les entrees du compte, tous styles confondus (SPEC-6076 R2),
        # compares comme des instants (le meme instant en +00:00 ou +02:00 est le meme creneau).
        taken = _account_taken_slots(entry_account(entry), state_dir, presets_dir, base, exclude=(video_id, clip_id))
        after = max(datetime.fromisoformat(entry["slot_at"]), _now(now))
        slot = None
        n = _POSTPONE_FIRST_BATCH
        while slot is None and n <= _POSTPONE_MAX_SLOTS:
            slot = next((c for c in channel_mod.next_slots(schedule, after, n)
                         if c not in taken and allowed(c) is None), None)
            n *= 2
        if slot is None:
            raise PublishError(f"aucun créneau libre et permis pour reporter {video_id}/{clip_id} ({reason})")
        entry = dict(entry)
        entry["postponed_reason"] = f"{reason} : reporté du {entry['slot_at']} au {_iso(slot)}"
        entry["slot_at"] = _iso(slot)
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def unschedule(
    video_id: str,
    clip_id: str,
    channel: str,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Repasse un clip programme en 'approved', sans creneau. Refuse un clip 'rejected', et un clip
    'published' publie par Clipper (``tiktok_state`` present) : seule l'annulation d'une declaration
    manuelle (« Marquer publié » sans suivi TikTok/YouTube) reste permise."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        _refuse_in_progress(entry, "retour en attente")
        _refuse_to_verify(entry, "retour en attente")
        if entry["status"] in ("rejected", REFUSED_BY_PLATFORM, REMOVED_FROM_PLATFORM):
            raise PublishError(f"retour en attente refusé pour {video_id}/{clip_id} : statut {entry['status']!r}")
        if entry["status"] == "published" and entry.get("tiktok_state"):
            raise PublishError(
                f"retour en attente refusé pour {video_id}/{clip_id} : publication déjà faite "
                f"({entry['tiktok_state']}), annule-la depuis TikTok/YouTube Studio"
            )

        entry = dict(entry)
        entry["status"] = "approved"
        entry["slot_at"] = None
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def edit_caption(
    video_id: str,
    clip_id: str,
    channel: str,
    description: str,
    hashtags: list[str],
    *,
    now: datetime | None = None,
    output_dir: str | Path = "output",
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Reecrit la legende (champ sidecar 'caption') et les hashtags, avec
    edited_at (SPEC-74e9 4.5). Refuse sur une entree scheduled/published."""
    path = _state_path(channel, state_dir)
    entries = _load_entries(path)
    entry = _find_entry(entries, video_id, clip_id)
    if entry is not None and entry["status"] in _NON_EDITABLE_STATUSES:
        raise PublishError(
            f"edition refusee pour {video_id}/{clip_id} : statut {entry['status']!r}"
        )

    sidecar = _read_sidecar(output_dir, video_id, clip_id)
    sidecar["caption"] = description
    sidecar["hashtags"] = hashtags
    sidecar["edited_at"] = _iso(_now(now))
    _write_sidecar(output_dir, video_id, clip_id, sidecar)
    return sidecar


def list_pending(
    channel: str,
    *,
    workspace_dir: str | Path = "workspace",
    output_dir: str | Path = "output",
    state_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Clips ready=true des videos de cette chaine, absents du fichier de
    publication (SPEC-74e9 4.4)."""
    path = _state_path(channel, state_dir)
    entries = _load_entries(path)
    known = {(entry["video_id"], entry["clip_id"]) for entry in entries}

    workspace_root = Path(workspace_dir)
    pending: list[dict[str, Any]] = []
    if not workspace_root.is_dir():
        return pending

    for video_dir in sorted(workspace_root.iterdir()):
        if not video_dir.is_dir():
            continue
        pipeline_path = video_dir / "pipeline.json"
        if not pipeline_path.exists():
            continue
        state = json.loads(pipeline_path.read_text(encoding="utf-8"))
        if state.get("channel") != channel:
            continue

        video_id = video_dir.name
        out_dir = Path(output_dir) / video_id
        if not out_dir.is_dir():
            continue
        for clip_path in sorted(out_dir.glob("*.json")):
            clip = json.loads(clip_path.read_text(encoding="utf-8"))
            if clip.get("ready") and (video_id, clip_path.stem) not in known:
                pending.append(clip)

    return pending


# --------------------------------------------------------------------------
# Publication pilotee depuis l'ecran Publication (SPEC-1ed3) : le formulaire
# « Nouvelle publication » cree l'entree (approbation implicite), ni creneau de
# chaine ni etape d'approbation separee. Maintenant : due tout de suite ;
# Programmer : le worker la programme sur TikTok quand la date entre dans la
# fenetre de TikTok (SPEC-9225 R3), d'ici la Clipper la garde.
# --------------------------------------------------------------------------

_POSTABLE_STATUSES = ("approved",)  # un clip approuve a l'ancienne (sans creneau) se programme depuis le formulaire
_CANCELLABLE_STATUSES = ("approved", "scheduled", "failed")


def _tz(channel_dict: dict[str, Any]) -> ZoneInfo:
    return ZoneInfo(str(channel_dict["timezone"]))


def service_settings(service: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reglages du service (plafonds, fenetre de programmation, reglages par defaut d'un post) : ``settings`` s'il est
    donne, sinon les valeurs par defaut du module du service."""
    if service not in SERVICE_STATES:
        raise PublishError(f"service invalide : {service!r} (attendu : {' | '.join(SERVICE_STATES)})")
    if settings is not None:
        return dict(settings)
    return dict(youtube.CONFIG_DEFAULTS if service == "youtube" else tiktok.CONFIG_DEFAULTS)


def _check_post_input(
    mode: Any, account: Any, publish_at: datetime | None, options: dict[str, Any] | None,
    settings: dict[str, Any], now: datetime, service: str = "tiktok",
) -> dict[str, Any]:
    """Validation commune a la creation et a la modification ; rend les options validees."""
    if mode not in PUBLISH_MODES:
        raise PublishError(f"mode de publication invalide : {mode!r} (attendu : {' | '.join(PUBLISH_MODES)})")
    if not isinstance(account, str) or not account:
        raise PublishError("compte de publication manquant : choisis un compte prêt à publier")
    label = SERVICE_LABELS.get(service, service)
    try:
        if service == "youtube":
            merged = youtube.post_settings(settings, options)
            youtube.check_mode(mode, merged)
        else:
            merged = tiktok.post_settings(settings, options)
    except (tiktok.TikTokError, youtube.YouTubeError) as exc:
        raise PublishError(str(exc)) from exc
    if mode == "scheduled":
        if service == "tiktok" and merged["visibility"] == "private":
            raise PublishError(
                "publication privée programmée refusée : TikTok ne programme pas une vidéo privée "
                "(« Les vidéos privées ne peuvent pas être programmées ») : choisis « Maintenant » ou une autre visibilité"
            )
        if publish_at is None:
            raise PublishError("publication programmée : une date et une heure sont requises")
        if publish_at.tzinfo is None:
            raise PublishError("publication programmée : la date doit avoir un fuseau horaire")
        if publish_at <= now:
            raise PublishError(f"publication programmée : la date {publish_at.isoformat()} est déjà passée")
        minutes = int(settings["schedule_min_minutes"])
        if publish_at < now + timedelta(minutes=minutes):
            raise PublishError(
                f"publication programmée : la date est à moins de {minutes} minutes (avance minimale de {label}) : "
                "choisis « Maintenant » ou une heure plus tardive"
            )
    return dict(options or {})


def planned_times(
    account: str, *, exclude: tuple[str, str] | None = None, state_dir: str | Path | None = None,
    presets_dir: str | Path = "presets", base: str | Path = "config.toml",
) -> list[datetime]:
    """Instants des posts deja faits ou programmes du compte (``account_publish_times``) PLUS ceux de ses
    entrees en attente ou en cours (leur ``slot_at``), toutes files : un plafond se verifie contre tout ce qui
    est deja prevu, pas seulement contre ce qui est parti. ``exclude`` ecarte l'entree qu'on modifie."""
    times = list(account_publish_times(account, state_dir=state_dir, presets_dir=presets_dir, base=base))
    for _name, entry in _account_entries(account, state_dir, presets_dir, base):
        if (entry["video_id"], entry["clip_id"]) == exclude:
            continue
        if entry["status"] in ("approved", "scheduled") and entry.get("slot_at"):
            times.append(datetime.fromisoformat(entry["slot_at"]))
    return sorted(times)


def after_last_schedule(
    account: str, interval_hours: float, *, now: datetime | None = None, state_dir: str | Path | None = None,
    presets_dir: str | Path = "presets", base: str | Path = "config.toml",
) -> datetime:
    """Date « Après la dernière programmation + N h » (SPEC-1ed3) : la derniere publication A VENIR du
    compte (``planned_times``, programmee dans Clipper ou sur le service) plus ``interval_hours`` heures ;
    maintenant plus ``interval_hours`` heures s'il n'y en a aucune."""
    now_dt = _now(now)
    future = [t for t in planned_times(account, state_dir=state_dir, presets_dir=presets_dir, base=base) if t > now_dt]
    base_time = max(future) if future else now_dt
    return base_time + timedelta(hours=interval_hours)


def _check_caps(
    account: str, target: datetime, exclude: tuple[str, str], settings: dict[str, Any], tz: ZoneInfo,
    state_dir: str | Path | None, presets_dir: str | Path, base: str | Path,
) -> None:
    """Plafonds par compte (SPEC-1ed3 R4, SPEC-5e50 R5 : ceux du service du compte, dans ``settings``) : un depassement est refuse ici, avec la raison et la prochaine
    heure possible, jamais reporte en silence."""
    times = planned_times(account, exclude=exclude, state_dir=state_dir, presets_dir=presets_dir, base=base)
    reason = tiktok.check_limits(times, target, settings, tz)
    if reason is not None:
        raise LimitError(reason, tiktok.next_allowed(times, target, settings, tz), tz)


def create_post(
    video_id: str,
    clip_id: str,
    channel: str | None,
    *,
    account: str,
    mode: str,
    publish_at: datetime | None = None,
    options: dict[str, Any] | None = None,
    caption: str | None = None,
    hashtags: list[str] | None = None,
    settings: dict[str, Any] | None = None,
    now: datetime | None = None,
    output_dir: str | Path = "output",
    state_dir: str | Path | None = None,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
    service: str = "tiktok",
    schedule: dict[str, Any] | None = None,
    parts_together: bool | None = None,
) -> dict[str, Any]:
    """Cree l'entree de publication d'un clip depuis le formulaire (SPEC-1ed3 R3) : valider = approuver. ``channel``
    est None pour une video sans chaine (file ``NO_CHANNEL``). ``mode`` ``immediate`` : due tout de suite ;
    ``scheduled`` : due a ``publish_at`` (le worker la programme sur TikTok quand la date entre dans la fenetre).
    ``options`` : reglages par post (visibilite, commentaires, reutilisation, contenu IA, verification de contenu).
    Refuse : clip pas pret, refuse ou deja publie, deja en file, prive + programme, plafond du compte depasse.
    ``service`` (``tiktok`` | ``youtube``, celui du compte) choisit les reglages, options et plafonds valides
    (``settings`` : ceux de ce service). ``schedule`` (``accounts.schedule_of``) : les plafonds par jour (R6) se
    comptent dans le fuseau du COMPTE, pas du style (revue r-comptes 9) ; sans ``schedule``, celui du style sert
    encore (compatibilite). ``parts_together`` (TASK-fc561e4dc7e9) : ``None`` (par defaut, hors formulaire
    série) laisse le champ absent de l'entree (le worker garde l'attente « partie N-1 non publiée ») ; un
    booleen explicite (``create_series``) le fixe dans l'entree, et False leve cette attente pour cette partie."""
    sidecar = _read_sidecar(output_dir, video_id, clip_id)
    if not sidecar.get("ready"):
        raise PublishError(f"clip non prêt pour publication : {video_id}/{clip_id}")
    channel = channel or NO_CHANNEL
    channel_dict = channel_settings(channel, presets_dir, base)
    settings = service_settings(service, settings)
    now_dt = _now(now)
    options = _check_post_input(mode, account, publish_at, options, settings, now_dt, service)
    when = publish_at if mode == "scheduled" else now_dt
    tz = _tz(schedule) if schedule else _tz(channel_dict)
    _check_caps(account, when, (video_id, clip_id), settings, tz, state_dir, presets_dir, base)

    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        existing = _find_entry(entries, video_id, clip_id)
        if existing is not None and existing["status"] not in _POSTABLE_STATUSES:
            status = existing["status"]
            if status in ("rejected", "published", REFUSED_BY_PLATFORM, REMOVED_FROM_PLATFORM):
                raise PublishError(f"publication refusée pour {video_id}/{clip_id} : le clip est {status!r}")
            raise PublishError(
                f"{video_id}/{clip_id} est déjà dans la file de publication (statut {status!r}) : "
                "modifie ou annule l'entrée existante"
            )
        if caption is not None or hashtags is not None:
            _write_caption(output_dir, video_id, clip_id, sidecar, caption, hashtags, now_dt)
        series_id, part = _series_info(video_id, clip_id, sidecar)
        entry: dict[str, Any] = {
            "video_id": video_id, "clip_id": clip_id, "series_id": series_id, "part": part,
            "status": "scheduled", "slot_at": _iso(when), "decided_at": _iso(now_dt),
            "published_at": None, "error": None, "account": account,
            "publish_mode": mode, "post_options": options, "manual": True, "service": service,
        }
        if parts_together is not None:
            entry["parts_together"] = parts_together
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def _write_caption(
    output_dir: str | Path, video_id: str, clip_id: str, sidecar: dict[str, Any], caption: str | None,
    hashtags: list[str] | None, now: datetime,
) -> None:
    if caption is not None:
        if not isinstance(caption, str) or not caption.strip():
            raise PublishError("légende vide : une légende est obligatoire pour publier")
        sidecar["caption"] = caption
    if hashtags is not None:
        if not isinstance(hashtags, list) or not all(isinstance(h, str) for h in hashtags):
            raise PublishError("hashtags invalides : une liste de textes est attendue")
        sidecar["hashtags"] = hashtags
    sidecar["edited_at"] = _iso(now)
    _write_sidecar(output_dir, video_id, clip_id, sidecar)


_UNSET: Any = object()


def update_post(
    video_id: str,
    clip_id: str,
    channel: str | None,
    *,
    account: Any = _UNSET,
    mode: Any = _UNSET,
    publish_at: Any = _UNSET,
    options: Any = _UNSET,
    caption: str | None = None,
    hashtags: list[str] | None = None,
    settings: dict[str, Any] | None = None,
    now: datetime | None = None,
    output_dir: str | Path = "output",
    state_dir: str | Path | None = None,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
    service: str = "tiktok",
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Modifie une entree de publication (SPEC-1ed3 R5) tant qu'elle n'est ni en cours ni publiee. Les champs
    omis sont conserves ; l'ensemble est revalide comme a la creation (prive + programme, date, plafonds).
    ``service`` : celui du compte retenu (voir ``create_post``). ``schedule`` : voir ``create_post`` (revue
    r-comptes 9)."""
    channel = channel or NO_CHANNEL
    channel_dict = channel_settings(channel, presets_dir, base)
    settings = service_settings(service, settings)
    now_dt = _now(now)
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        if entry["status"] in ("published", "rejected", REFUSED_BY_PLATFORM, REMOVED_FROM_PLATFORM):
            raise PublishError(f"modification refusée pour {video_id}/{clip_id} : la publication est {entry['status']!r}")
        _refuse_in_progress(entry, "modification")
        _refuse_to_verify(entry, "modification")
        new_mode = entry.get("publish_mode") if mode is _UNSET else mode
        new_account = entry.get("account") if account is _UNSET else account
        new_options = dict(entry.get("post_options") or {}) if options is _UNSET else options
        if publish_at is not _UNSET:
            new_at = publish_at
        else:
            new_at = datetime.fromisoformat(entry["slot_at"]) if entry.get("slot_at") else None
        new_options = _check_post_input(new_mode, new_account, new_at if new_mode == "scheduled" else None,
                                        new_options, settings, now_dt, service)
        when = new_at if new_mode == "scheduled" else now_dt
        tz = _tz(schedule) if schedule else _tz(channel_dict)
        _check_caps(new_account, when, (video_id, clip_id), settings, tz, state_dir, presets_dir, base)
        if caption is not None or hashtags is not None:
            _write_caption(output_dir, video_id, clip_id, _read_sidecar(output_dir, video_id, clip_id),
                           caption, hashtags, now_dt)
        # Une entree 'approved' (compte sans creneau, ou clip approuve a l'ancienne) devient 'scheduled' :
        # sinon le worker (qui ne prend que 'scheduled') ne la publie jamais (revue r-comptes 6).
        new_status = "scheduled" if entry["status"] == "approved" else entry["status"]
        entry = dict(entry)
        entry.update(status=new_status, account=new_account, publish_mode=new_mode, slot_at=_iso(when),
                     post_options=new_options, waiting_reason=None, postponed_reason=None, service=service)
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def cancel_post(
    video_id: str,
    clip_id: str,
    channel: str | None,
    *,
    state_dir: str | Path | None = None,
) -> None:
    """Annule une publication (SPEC-1ed3 R5) : l'entree est retiree de la file, le clip redevient « a valider ».
    Refuse en cours, publiee ou deja programmee sur TikTok (a annuler dans TikTok Studio)."""
    path = _state_path(channel or NO_CHANNEL, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        _refuse_in_progress(entry, "annulation")
        _refuse_to_verify(entry, "annulation")
        if entry["status"] == "published":
            if entry.get("tiktok_state") == "scheduled_on_tiktok":
                where = " : elle est déjà programmée sur TikTok, annule-la dans TikTok Studio"
            elif entry.get("tiktok_state") == "scheduled_on_youtube":
                where = " : elle est déjà programmée sur YouTube, annule-la dans YouTube Studio"
            else:
                where = " : le clip est publié"
            raise PublishError(f"annulation refusée pour {video_id}/{clip_id}{where}")
        if entry["status"] not in _CANCELLABLE_STATUSES:
            raise PublishError(f"annulation refusée pour {video_id}/{clip_id} : statut {entry['status']!r}")
        _save_entries(path, [e for e in entries if e is not entry])


# Ce qui doit etre identique entre l'instantane du worker et l'entree relue pour qu'il la pilote.
_TAKEOVER_FIELDS: tuple[tuple[str, Callable[[dict[str, Any]], Any]], ...] = (
    ("compte", entry_account),
    ("date", lambda e: e.get("slot_at")),
    ("mode", lambda e: e.get("publish_mode")),
    ("réglages", lambda e: e.get("post_options")),
)


def mark_in_progress(
    video_id: str,
    clip_id: str,
    channel: str,
    *,
    now: datetime | None = None,
    state_dir: str | Path | None = None,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Le worker prend la main sur cette entree (« en cours ») : plus modifiable ni annulable ; efface par
    ``mark_published`` / ``mark_failed``. Prise atomique sous le verrou : l'entree doit etre ``scheduled``,
    pas deja en cours, et (``expected`` : l'instantane lu par le worker) avoir le meme compte, la meme date et
    les memes reglages ; sinon ``PublishError`` et rien n'est ecrit. Rend l'entree relue sous le verrou."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        if entry["status"] != "scheduled":
            raise PublishError(f"prise en main refusée pour {video_id}/{clip_id} : statut {entry['status']!r} "
                               "(attendu : 'scheduled')")
        if entry.get("in_progress_since"):
            raise PublishError(f"prise en main refusée pour {video_id}/{clip_id} : déjà en cours depuis "
                               f"{entry['in_progress_since']}")
        if expected is not None:
            changed = [label for label, read in _TAKEOVER_FIELDS if read(entry) != read(expected)]
            if changed:
                raise PublishError(f"prise en main refusée pour {video_id}/{clip_id} : publication modifiée "
                                   f"depuis sa lecture ({', '.join(changed)}), relue au prochain passage")
        entry = dict(entry)
        # le pilote est identifie par son pid et l'heure de creation de son processus (clipper.worker) : un
        # worker qui redemarre ne passe en echec que l'entree d'un pilote mort (publication-I2)
        from clipper import worker as worker_mod  # import tardif : worker importe publish

        entry["in_progress_since"] = _iso(_now(now))
        entry["in_progress_pid"] = os.getpid()
        entry["in_progress_pid_created_at"] = worker_mod._process_created_at(os.getpid())
        _upsert_entry(entries, entry)
        _save_entries(path, entries)
    return entry


def _holder_alive(entry: dict[str, Any]) -> bool:
    """Le processus qui a pris l'entree en main vit-il encore (pid + heure de creation) ? Une entree sans pid
    (ecrite avant cet enregistrement) n'a plus de pilote connu : interrompue."""
    from clipper import worker as worker_mod  # import tardif : worker importe publish

    pid = entry.get("in_progress_pid")
    return pid is not None and worker_mod.process_alive(int(pid), entry.get("in_progress_pid_created_at"))


def release_in_progress(video_id: str, clip_id: str, channel: str, reason: str, *,
                        state_dir: str | Path | None = None) -> None:
    """Rend au repos une entree prise en main (``mark_in_progress``) mais dont le post n'est pas parti : elle
    reste ``scheduled``, sans « en cours », avec la raison visible (TASK-0c97)."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        entry = _find_entry(entries, video_id, clip_id)
        if entry is None:
            raise PublishError(f"clip absent de la file de publication : {video_id}/{clip_id}")
        entry = dict(entry)
        entry.update(in_progress_since=None, in_progress_pid=None, in_progress_pid_created_at=None,
                     waiting_reason=reason)
        _upsert_entry(entries, entry)
        _save_entries(path, entries)


def fail_interrupted(channel: str, *, now: datetime | None = None, state_dir: str | Path | None = None) -> int:
    """Une entree restee « en cours » alors que son pilote est mort (worker arrete pendant la publication) passe
    en echec explicite, reessayable : jamais bloquee en « en cours ». Une entree dont le pilote vit encore (un
    autre worker, publication-I2) n'est pas touchee : son post part ou echoue par lui. Rend le nombre d'entrees
    touchees."""
    path = _state_path(channel, state_dir)
    with _locked(path):
        entries = _load_entries(path)
        stale = [e for e in entries if e.get("in_progress_since") and not _holder_alive(e)]
        for entry in stale:
            updated = dict(entry)
            updated.update(
                status="failed", in_progress_since=None, in_progress_pid=None, in_progress_pid_created_at=None,
                halted=False, capture=None, waiting_reason=None, failed_at=_iso(_now(now)),
                error="publication interrompue (le worker s'est arrêté pendant la publication) : "
                      "vérifie sur TikTok Studio que le post n'existe pas avant de réessayer")
            _upsert_entry(entries, updated)
        if stale:
            _save_entries(path, entries)
    return len(stale)


def all_entries(
    *, state_dir: str | Path | None = None, presets_dir: str | Path = "presets",
) -> list[tuple[str, dict[str, Any]]]:
    """(file, entree) de toutes les entrees de toutes les chaines (y compris d'un style supprime) et de la file
    des videos sans chaine."""
    found: list[tuple[str, dict[str, Any]]] = []
    try:
        for name in _queue_names(state_dir, presets_dir):
            found.extend((name, e) for e in _load_entries(_state_path(name, state_dir)))
    except channel_mod.ChannelError as exc:
        raise PublishError(f"chaînes illisibles : {exc}") from exc
    return found


# --------------------------------------------------------------------------
# Série programmée (TASK-5bbf, SPEC-1ed3, SPEC-6076 R3/R6) : choix automatique
# (N meilleurs clips par score) ou manuel (ordre de selection choisi par
# l'utilisateur) de clips a publier a cadence reguliere (debut + k x X h, en
# duree reelle). Une serie en plusieurs parties est prise entiere, dans
# l'ordre, ou pas du tout ; N compte des posts (une partie = un post).
# Aucun repli silencieux (ADR-ad2e) : l'apercu (preview_series) dit chaque
# refus, la creation (create_series) est tout ou rien.
# --------------------------------------------------------------------------


def plan_series_dates(start_at: datetime, interval_hours: int, count: int) -> list[datetime]:
    """Dates d'une serie (debut + k x intervalle, k=0..count-1), en duree reelle : conversion
    en UTC puis arithmetique sur des ``timedelta`` (jamais d'arithmetique murale) : un changement
    d'heure d'ete/hiver ne decale jamais l'ecart entre deux publications."""
    if start_at.tzinfo is None:
        raise PublishError("série : la date de début doit avoir un fuseau horaire")
    if not isinstance(interval_hours, int) or isinstance(interval_hours, bool) or interval_hours < 1:
        raise PublishError("intervalle invalide : un nombre entier d'heures >= 1 est attendu (pas de demi-heure)")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise PublishError("nombre de publications invalide : un entier >= 1 est attendu")
    start_utc = start_at.astimezone(timezone.utc)
    step = timedelta(hours=interval_hours)
    return [start_utc + i * step for i in range(count)]


def _unit_members(output_dir: str | Path, video_id: str, clip_id: str, sidecar: dict[str, Any]) -> list[str]:
    """Les clip_id d'une serie (celle de ``clip_id`` incluse), tries par numero de partie ; ``[clip_id]``
    si ce n'est pas une serie en plusieurs parties."""
    series_id, part = _series_info(video_id, clip_id, sidecar)
    if series_id is None:
        return [clip_id]
    ordered = [(part or 1, clip_id)]
    for sibling_id in _sibling_clip_ids(output_dir, video_id, series_id, exclude=clip_id):
        sibling_sidecar = _read_sidecar(output_dir, video_id, sibling_id)
        _, sibling_part = _series_info(video_id, sibling_id, sibling_sidecar)
        ordered.append((sibling_part or 1, sibling_id))
    ordered.sort(key=lambda pair: pair[0])
    return [cid for _, cid in ordered]


def _eligible_units(
    output_dir: str | Path, video_id: str, channel: str | None, entries: dict[tuple[str, str], dict[str, Any]],
    *, together: bool = True,
) -> list[dict[str, Any]]:
    """Unites de ce ``video_id`` pretes a publier (``ready``) et absentes de ``entries`` (jamais publiees, en
    file ni programmees). ``together`` (coche « Parties ensemble », TASK-fc561e4dc7e9) : groupees en series
    entieres (une partie indisponible exclut toute la serie), ou chacune sa propre unite independante si faux.
    Jamais validees (``validated`` : False) : un clip pret n'est pas encore approuve (TASK-16eeaccfaf09)."""
    out_dir = Path(output_dir) / video_id
    seen: set[str] = set()
    units: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("*.json")):
        clip_id = path.stem
        if clip_id in seen:
            continue
        sidecar = _read_sidecar(output_dir, video_id, clip_id)
        if together:
            members = _unit_members(output_dir, video_id, clip_id, sidecar)
            seen.update(members)
            member_sidecars = [sidecar if m == clip_id else _read_sidecar(output_dir, video_id, m) for m in members]
            if not all(s.get("ready") is True for s in member_sidecars):
                continue
            if any((video_id, m) in entries for m in members):
                continue
        else:
            seen.add(clip_id)
            if not sidecar.get("ready") or (video_id, clip_id) in entries:
                continue
            members = [clip_id]
        units.append({
            "video_id": video_id, "channel": channel, "clip_ids": members, "score": sidecar.get("score"),
            "validated": False,
        })
    return units


def _entry_matches_account(entry: dict[str, Any], account: str | None) -> bool:
    return account is None or entry.get("account") in (None, account)


def _validated_units(
    output_dir: str | Path, video_id: str, channel: str | None, entries: dict[tuple[str, str], dict[str, Any]],
    account: str | None, *, together: bool = True,
) -> list[dict[str, Any]]:
    """Unites de ce ``video_id`` DEJA validees : entree de publication 'approved', sans creneau (``slot_at``
    None) et pas en cours (TASK-16eeaccfaf09). Si ``account`` est donne, chaque entree doit avoir ce compte ou
    aucun ; jamais un clip valide pour un autre compte. ``together`` (TASK-fc561e4dc7e9) : une unite est une
    serie entiere, toutes ses parties validees (une partie non validee exclut toute la serie, comme pour
    ``_eligible_units``) ; si faux, chaque partie validee compte seule, meme si sa soeur ne l'est pas."""
    out_dir = Path(output_dir) / video_id
    seen: set[str] = set()
    units: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("*.json")):
        clip_id = path.stem
        if clip_id in seen:
            continue
        sidecar = _read_sidecar(output_dir, video_id, clip_id)
        if together:
            members = _unit_members(output_dir, video_id, clip_id, sidecar)
            seen.update(members)
            member_entries = [entries.get((video_id, m)) for m in members]
            if any(
                e is None or e["status"] != "approved" or e.get("slot_at") is not None or e.get("in_progress_since")
                for e in member_entries
            ):
                continue
            if any(not _entry_matches_account(e, account) for e in member_entries):
                continue
        else:
            seen.add(clip_id)
            entry = entries.get((video_id, clip_id))
            if entry is None or entry["status"] != "approved" or entry.get("slot_at") is not None or entry.get("in_progress_since"):
                continue
            if not _entry_matches_account(entry, account):
                continue
            members = [clip_id]
        units.append({
            "video_id": video_id, "channel": channel, "clip_ids": members, "score": sidecar.get("score"),
            "validated": True,
        })
    return units


def available_series_clips(
    style: str | None, *, account: str | None = None, together: bool = True,
    workspace_dir: str | Path = "workspace", output_dir: str | Path = "output",
    state_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Unites de clips (chacune une serie entiere, parties triees), optionnellement filtrees par ``style``
    (``None`` = tous les styles, y compris les videos sans style). Chaque unite porte ``validated`` : False pour
    un clip pret jamais entre en file (comme avant), True pour un clip deja approuve sans creneau
    (TASK-16eeaccfaf09 : mode manuel = les deux). Avec ``account`` donne, une unite validee pour un AUTRE compte
    est exclue (les unites non validees n'ont pas encore de compte, jamais filtrees). ``together``=False (coche
    « Parties ensemble » decochee, TASK-fc561e4dc7e9) : chaque partie est sa propre unite independante, jamais
    groupee avec ses soeurs. L'ordre rendu n'est PAS trie par score (voir ``preview_series``)."""
    workspace_root = Path(workspace_dir)
    if not workspace_root.is_dir():
        return []
    units: list[dict[str, Any]] = []
    entries_cache: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for video_dir in sorted(p for p in workspace_root.iterdir() if p.is_dir()):
        pipeline_path = video_dir / "pipeline.json"
        if not pipeline_path.exists():
            continue
        state = json.loads(pipeline_path.read_text(encoding="utf-8"))
        channel = state.get("channel")
        if style is not None and channel != style:
            continue
        file_channel = channel or NO_CHANNEL
        if file_channel not in entries_cache:
            entries_cache[file_channel] = {
                (e["video_id"], e["clip_id"]): e for e in _load_entries(_state_path(file_channel, state_dir))
            }
        out_dir = Path(output_dir) / video_dir.name
        if not out_dir.is_dir():
            continue
        entries = entries_cache[file_channel]
        units.extend(_eligible_units(output_dir, video_dir.name, channel, entries, together=together))
        units.extend(_validated_units(output_dir, video_dir.name, channel, entries, account, together=together))
    return units


def auto_series_capacity(
    style: str | None, account: str, *, together: bool = True, workspace_dir: str | Path = "workspace",
    output_dir: str | Path = "output", state_dir: str | Path | None = None,
) -> int:
    """Nombre de posts disponibles pour ``account`` en mode auto (clips valides, une partie = un post,
    TASK-fc561e4dc7e9) : le max du champ « Nombre de vidéos » du formulaire série. ``together``=True ne compte
    que des series entieres (comme ``preview_series`` mode auto) ; False compte chaque partie validee seule."""
    pool = available_series_clips(
        style, account=account, together=together, workspace_dir=workspace_dir, output_dir=output_dir,
        state_dir=state_dir,
    )
    return sum(len(u["clip_ids"]) for u in pool if u["validated"])


def _series_item_refusal(when: datetime, settings: dict[str, Any], now_dt: datetime, service: str) -> str | None:
    """Refus explicite (ADR-ad2e) d'une date de serie, ou None : date deja passee, sous l'avance minimale, ou
    au-dela de la fenetre de programmation du service (contrairement a ``create_post`` seul, une serie refuse
    une date hors fenetre plutot que de la garder en attente : trop de publications a surveiller a la main)."""
    label = SERVICE_LABELS.get(service, service)
    if when <= now_dt:
        return f"date déjà passée : {when.isoformat()}"
    minutes = int(settings["schedule_min_minutes"])
    if when < now_dt + timedelta(minutes=minutes):
        return (f"date à moins de {minutes} minutes (avance minimale de {label}) : "
                "choisis un début plus tardif ou un intervalle plus grand")
    days = int(settings["schedule_max_days"])
    if when > now_dt + timedelta(days=days):
        return (f"date hors fenêtre de programmation de {label} (plus de {days} jours à l'avance) : "
                "réduis le nombre de vidéos, l'intervalle, ou avance le début")
    return None


def _per_clip_refusal(
    clip_id: str, when: datetime, previous: tuple[str, datetime] | None, account_taken: set[datetime],
    series_taken: set[datetime],
) -> str | None:
    """Refus explicite (ADR-ad2e) propre a la date saisie clip par clip (TASK-fa00f90a735a) : creneau deja
    pris sur le compte, meme heure qu'un autre clip de la serie, partie datee avant ou avec la precedente."""
    if when in account_taken:
        return f"créneau déjà pris sur ce compte : {when.isoformat()}"
    if when in series_taken:
        return f"même heure qu'un autre clip de la série : {when.isoformat()}"
    if previous is not None and when <= previous[1]:
        return (f"la partie doit être publiée après la partie précédente ({previous[0]}, "
                f"{previous[1].isoformat()}) : choisis une date plus tardive")
    return None


def _auto_series_units(pool: list[dict[str, Any]], count: int) -> tuple[list[dict[str, Any]], int]:
    """Les meilleures unites (score decroissant) qui tiennent dans ``count`` posts : une unite trop grande
    pour les places restantes est sautee (jamais coupee), la suivante (par score) est tentee (complement
    utilisateur du 2026-10-03)."""
    ordered = sorted(
        pool, key=lambda u: (-(u["score"] if u["score"] is not None else float("-inf")), u["video_id"], u["clip_ids"][0])
    )
    selected: list[dict[str, Any]] = []
    used = 0
    for unit in ordered:
        if used >= count:
            break
        size = len(unit["clip_ids"])
        if used + size > count:
            continue
        selected.append(unit)
        used += size
    return selected, used


def _manual_series_units(
    pool: list[dict[str, Any]], selection: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], int]:
    """Les unites choisies a la main, dans l'ordre de selection : cocher n'importe quelle partie d'une serie
    selectionne la serie entiere ; une serie deja selectionnee (par une autre de ses parties) n'est pas
    comptee deux fois. Leve ``PublishError`` si une selection ne correspond a aucune unite disponible."""
    if not selection:
        raise PublishError("série manuelle : choisis au moins un clip")
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for unit in pool:
        for clip_id in unit["clip_ids"]:
            by_key[(unit["video_id"], clip_id)] = unit

    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    used = 0
    for video_id, clip_id in selection:
        unit = by_key.get((video_id, clip_id))
        if unit is None:
            raise PublishError(
                f"clip indisponible pour la série : {video_id}/{clip_id} (déjà en file, publié ou pas prêt)"
            )
        unit_key = (unit["video_id"], tuple(unit["clip_ids"]))
        if unit_key in seen:
            continue
        seen.add(unit_key)
        selected.append(unit)
        used += len(unit["clip_ids"])
    return selected, used


def preview_series(
    *,
    mode: str,
    style: str | None,
    account: str,
    service: str = "tiktok",
    interval_hours: int | None = None,
    start_at: datetime | None = None,
    count: int | None = None,
    selection: list[tuple[str, str]] | None = None,
    clip_dates: dict[tuple[str, str], datetime] | None = None,
    together: bool = True,
    settings: dict[str, Any] | None = None,
    now: datetime | None = None,
    workspace_dir: str | Path = "workspace",
    output_dir: str | Path = "output",
    state_dir: str | Path | None = None,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apercu d'une serie programmee (SPEC-1ed3, SPEC-6076 R3/R6), sans rien creer. ``together`` (coche
    « Parties ensemble », TASK-fc561e4dc7e9, par defaut True) : True groupe chaque serie en une seule unite
    (une partie indisponible exclut toute la serie, le max ne compte que des series entieres) ; False traite
    chaque partie comme une unite independante, en auto comme en manuel. ``mode`` ``auto`` :
    uniquement des clips DEJA VALIDES (approuves, sans creneau) pour ``account`` (ou aucun compte) -- jamais un
    clip pret mais non valide, ni valide pour un autre compte (TASK-16eeaccfaf09) -- les meilleurs par score
    jusqu'a ``count`` posts (une partie = un post, une serie incomplete est sautee entiere) ; si aucun clip
    n'est valide, le refus le dit explicitement. ``mode`` ``manual`` : ``selection`` (video_id, clip_id) dans
    l'ordre choisi par l'utilisateur, parmi les clips valides ET les clips prets non valides (aucune
    restriction de compte) ; cocher une partie ajoute toute sa serie. Chaque publication prevue est a
    ``start_at + k x interval_hours`` (duree reelle) ; un refus (date hors fenetre, sous l'avance minimale,
    plafond du compte) est explicite par publication (ADR-ad2e), jamais un decalage silencieux. ``schedule``
    (``accounts.schedule_of``) : les plafonds se comptent dans le fuseau du COMPTE, pas du style (revue
    r-comptes 9) ; sans ``schedule``, celui du style sert encore (compatibilite) ; ``create_series`` doit
    passer le meme ``schedule`` qu'ici, sous peine d'un apercu « ok » que la creation refuse. Rend
    ``{"items", "available", "requested", "insufficient", "insufficient_reason", "ok"}`` ; ``ok`` est faux
    des qu'un item est refuse ou que la serie est incomplete. ``clip_dates`` (coche « Heure par clip »,
    TASK-fa00f90a735a, mode manuel seulement) : la date de CHAQUE clip de la serie (parties incluses), libre
    et sans fuseau implicite ; ``interval_hours`` et ``start_at`` sont alors ignores. Chaque date est validee
    clip par clip (memes refus que ci-dessus, plus « creneau deja pris » sur le compte, « meme heure » qu'un
    autre clip de la serie, et une partie datee avant ou a la meme heure que la precedente)."""
    if mode not in ("auto", "manual"):
        raise PublishError(f"mode de série invalide : {mode!r} (attendu : auto | manual)")
    if not account:
        raise PublishError("compte de publication manquant : choisis un compte prêt à publier")

    settings = service_settings(service, settings)
    now_dt = _now(now)
    if clip_dates is not None and mode != "manual":
        raise PublishError("date par clip : seulement en mode manuel")
    # Mode auto : seules les unites deja validees (approuvees, sans creneau) pour CE compte (ou aucun) sont
    # eligibles (TASK-16eeaccfaf09) ; mode manuel : aucune restriction de compte, ready et validees proposees.
    pool = available_series_clips(
        style, account=(account if mode == "auto" else None), together=together,
        workspace_dir=workspace_dir, output_dir=output_dir, state_dir=state_dir,
    )

    if mode == "auto":
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise PublishError("nombre de vidéos invalide : un entier >= 1 est attendu")
        pool = [u for u in pool if u["validated"]]  # jamais un clip pret mais non valide
        selected_units, used = _auto_series_units(pool, count)
        requested = count
    else:
        selected_units, used = _manual_series_units(pool, selection or [])
        requested = used

    first_at = start_at
    if clip_dates is not None:
        for when in clip_dates.values():
            if when.tzinfo is None:
                raise PublishError("série : chaque date doit avoir un fuseau horaire")
        missing = [
            f"{unit['video_id']}/{cid}" for unit in selected_units for cid in unit["clip_ids"]
            if (unit["video_id"], cid) not in clip_dates
        ]
        if missing:
            raise PublishError(f"date manquante pour : {', '.join(missing)}")
        dates = [
            clip_dates[(unit["video_id"], cid)].astimezone(timezone.utc)
            for unit in selected_units for cid in unit["clip_ids"]
        ]
        # Les reglages du post ne dependent pas de la date : une date sonde valide evite qu'un clip trop
        # proche ne fasse refuser tous les autres au titre des reglages (son propre refus est par clip).
        first_at = now_dt + timedelta(minutes=int(settings["schedule_min_minutes"]) + 1)
    else:
        if start_at is None:
            raise PublishError("série : la date de début est manquante")
        if interval_hours is None:
            raise PublishError("intervalle invalide : un nombre entier d'heures >= 1 est attendu (pas de demi-heure)")
        dates = plan_series_dates(start_at, interval_hours, used) if used else []

    # Refus des reglages du post (visibilite privee/non publique programmee) une seule fois : si la
    # creation le refuserait pour chaque publication de la serie (meme reglages, seule la date change),
    # l'apercu doit le dire aussi (revue r-publication M1), jamais "ok" puis refuse a la creation.
    settings_refusal: str | None = None
    try:
        if first_at is not None:
            _check_post_input("scheduled", account, first_at, None, settings, now_dt, service)
    except PublishError as exc:
        settings_refusal = str(exc)

    items: list[dict[str, Any]] = []
    committed: list[datetime] = list(
        planned_times(account, state_dir=state_dir, presets_dir=presets_dir, base=base)
    )
    account_taken = set(committed)
    series_taken: set[datetime] = set()
    i = 0
    for unit in selected_units:
        channel = unit["channel"] or NO_CHANNEL
        tz = _tz(schedule) if schedule else _tz(channel_settings(channel, presets_dir, base))
        previous: tuple[str, datetime] | None = None
        for clip_id in unit["clip_ids"]:
            when = dates[i]
            i += 1
            refusal = settings_refusal or _series_item_refusal(when, settings, now_dt, service)
            if refusal is None and clip_dates is not None:
                refusal = _per_clip_refusal(clip_id, when, previous, account_taken, series_taken)
            previous = (clip_id, when)
            series_taken.add(when)
            if refusal is None:
                reason = tiktok.check_limits(committed, when, settings, tz)
                if reason is not None:
                    next_at = tiktok.next_allowed(committed, when, settings, tz)
                    refusal = str(LimitError(reason, next_at, tz))
                else:
                    committed.append(when)
            items.append({
                "video_id": unit["video_id"], "clip_id": clip_id, "channel": unit["channel"],
                "score": unit["score"], "publish_at": _iso(when), "refusal": refusal,
            })

    insufficient = mode == "auto" and used < count
    insufficient_reason = None
    if insufficient:
        if used == 0 and pool:
            # Des clips valides existent mais aucun ne tient dans count : jamais « aucun clip validé » (faux).
            parts = sum(len(u["clip_ids"]) for u in pool)
            smallest = min(len(u["clip_ids"]) for u in pool)
            n = len(pool)
            clips = f"{n} clip{'s' if n > 1 else ''} validé{'s' if n > 1 else ''}"
            parts_label = f"{parts} partie{'s' if parts > 1 else ''}"
            verb = "tiennent" if n > 1 else "tient"
            insufficient_reason = (
                f"{clips} en {parts_label} ne {verb} pas dans {count} place{'s' if count > 1 else ''} : "
                f"passe à {smallest} vidéo{'s' if smallest > 1 else ''} ou décoche « Parties ensemble »"
            )
        elif used == 0:
            # Message clair (TASK-16eeaccfaf09) : distingue « rien n'est validé » d'un simple manque de clips.
            insufficient_reason = "aucun clip validé disponible : valide d'abord des clips dans l'écran Clips"
        else:
            plural = "s" if used > 1 else ""
            insufficient_reason = f"seulement {used} vidéo{plural} disponible{plural} (demandé : {count})"
    ok = not insufficient and bool(items) and all(it["refusal"] is None for it in items)
    return {
        "mode": mode, "requested": requested, "available": used,
        "insufficient": insufficient, "insufficient_reason": insufficient_reason,
        "items": items, "ok": ok,
    }


def create_series(
    *,
    mode: str,
    style: str | None,
    account: str,
    service: str = "tiktok",
    interval_hours: int | None = None,
    start_at: datetime | None = None,
    count: int | None = None,
    selection: list[tuple[str, str]] | None = None,
    clip_dates: dict[tuple[str, str], datetime] | None = None,
    together: bool = True,
    settings: dict[str, Any] | None = None,
    now: datetime | None = None,
    workspace_dir: str | Path = "workspace",
    output_dir: str | Path = "output",
    state_dir: str | Path | None = None,
    presets_dir: str | Path = "presets",
    base: str | Path = "config.toml",
    schedule: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Cree une serie programmee (SPEC-1ed3, SPEC-6076 R3/R6) : ``preview_series`` d'abord, puis une entree
    ``create_post`` par publication, dans l'ordre. Tout ou rien (ADR-ad2e) : un seul item refuse, ou moins de
    clips que demande, et RIEN n'est cree ; si la creation echoue en cours de route (etat change entre
    l'apercu et la creation), les entrees deja creees sont annulees avant de relever l'erreur. ``schedule`` :
    le meme que celui passe a ``preview_series`` (revue r-comptes 9), sinon un item accepte a l'apercu
    pourrait etre refuse ici (fuseaux differents). ``together`` (TASK-fc561e4dc7e9) : le meme que celui passe
    a ``preview_series`` ; chaque entree creee porte ``parts_together`` a cette valeur (le worker n'attend la
    partie precedente que si elle vaut True)."""
    preview = preview_series(
        mode=mode, style=style, account=account, service=service, interval_hours=interval_hours,
        start_at=start_at, count=count, selection=selection, clip_dates=clip_dates, together=together,
        settings=settings, now=now, workspace_dir=workspace_dir, output_dir=output_dir, state_dir=state_dir, presets_dir=presets_dir, base=base,
        schedule=schedule,
    )
    if preview["insufficient"]:
        raise PublishError(f"série refusée : {preview['insufficient_reason']}")
    if not preview["items"]:
        raise PublishError("série refusée : aucun clip disponible")
    bad = [it for it in preview["items"] if it["refusal"]]
    if bad:
        reasons = "; ".join(f"{it['video_id']}/{it['clip_id']} : {it['refusal']}" for it in bad)
        raise PublishError(f"série refusée (aucune publication créée) : {reasons}")

    created: list[tuple[str | None, dict[str, Any]]] = []
    try:
        for item in preview["items"]:
            when = datetime.fromisoformat(item["publish_at"])
            entry = create_post(
                item["video_id"], item["clip_id"], item["channel"], account=account, mode="scheduled",
                publish_at=when, settings=settings, now=now, output_dir=output_dir, state_dir=state_dir,
                presets_dir=presets_dir, base=base, service=service, schedule=schedule, parts_together=together,
            )
            created.append((item["channel"], entry))
    except Exception as exc:  # toute exception (ADR-ad2e) : pas seulement PublishError/ChannelError/ConfigError,
        # un OSError ou un ValueError (sidecar JSON corrompu) doit aussi declencher l'annulation
        stuck: list[str] = []
        for channel, entry in created:
            try:
                cancel_post(entry["video_id"], entry["clip_id"], channel, state_dir=state_dir)
            except PublishError as cancel_exc:
                stuck.append(f"{entry['video_id']}/{entry['clip_id']} ({cancel_exc})")
        if not stuck:
            raise
        message = (
            f"série interrompue ({exc}) : {len(stuck)} publication(s) déjà prise(s) par le worker, "
            f"non annulée(s) : {'; '.join(stuck)}"
        )
        log.error(message)
        raise PublishError(message) from exc
    return [entry for _, entry in created]
