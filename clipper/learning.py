"""learning.py : boucle d'apprentissage, 1/4 : rattacher apres releve les posts TikTok aux clips Clipper
sans id de post (ADR-c260, SPEC-00db R1).

Bibliotheque (ADR-b16b) : n'importe ni ``clipper.web`` ni une etape ; appelee par le worker. Un post programme
n'a pas d'adresse a la publication : le releve des Publications (SPEC-47e2) le voit ensuite avec son id et sa
legende. Rien n'est devine (ADR-ad2e) : zero ou plusieurs candidats = non relie, raison ecrite dans
``state/learning/links.json``.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from clipper import accounts as accounts_mod
from clipper import channel as channel_mod
from clipper import jury, jury_calibration, jury_coach, llm, outcomes, publish, tiktok
from clipper.config import Config

log = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    "state_dir": "state/learning",  # links.json : derniers passages et clips non relies, avec la raison
    "enabled": True,  # false : le worker ne rattache rien
    "link_window_h": 12,  # ecart maximal (heures) entre la date prevue du clip et la date du post releve
    "maturity_days": 3,  # age minimal (jours depuis la publication) d'un releve pour que ses vues comptent
    "window_days": 90,  # fenetre des posts du compte qui servent de reference au rang des vues
    "min_account_posts": 10,  # posts murs a vues > 0 pour qu'un compte entre dans l'apprentissage
    "coach_min_new_cases": 10,  # clips scored nouveaux depuis le dernier passage du coach pour en declencher un
    "coach_min_interval_days": 7,  # jours minimaux entre deux passages du coach (ADR-c260 : cout borne)
    "veille_report_days": 30,  # fenetre (jours) des VOD mises en file que reprend state/veille/bilan.json
    "veille_report_max": 20,  # nombre maximal d entrees du bilan, les plus recentes d abord
    "veille_report_min_interval_s": 60,  # secondes minimales entre deux calculs du bilan (hors relevé qui vient de tourner)
    "zero_view_alert_hours": 24,  # age (heures depuis la mise en ligne) a partir duquel un post a 0 vue est signale
    "zero_view_alert_max_views": 0,  # vues au plus au dernier releve pour qu'un post soit en alerte
    "zero_view_alert_account_min": 2,  # posts en alerte d'un meme compte pour une alerte au niveau du compte
    "breakdown_min_n": 5,  # clips mûrs sous ce nombre dans un groupe (jeu, streamer, heure, compte) : « trop peu pour conclure »
    "retention_min_n": 30,  # clips scored sous ce nombre : le tableau de retention est montre avec un avertissement, sans conclusion
}
MOMENT_SOURCES = ("transcript", "action")

REASONS = ("none", "ambiguous")


class LearningError(Exception):
    """Etat ou sidecar illisible : jamais ignore, nomme le fichier."""


def _settings(config: Config | None) -> dict[str, Any]:
    settings = dict(config.section("learning")) if config is not None else dict(CONFIG_DEFAULTS)
    window = settings["link_window_h"]
    if isinstance(window, bool) or not isinstance(window, (int, float)) or window <= 0:
        raise LearningError(f"[learning] link_window_h invalide : {window!r} (un nombre d'heures > 0 est attendu)")
    maturity = settings["maturity_days"]
    if isinstance(maturity, bool) or not isinstance(maturity, (int, float)) or maturity < 1:
        raise LearningError(f"[learning] maturity_days invalide : {maturity!r} (un nombre de jours >= 1 est attendu)")
    retention_min = settings["retention_min_n"]
    if isinstance(retention_min, bool) or not isinstance(retention_min, int) or retention_min < 1:
        raise LearningError(f"[learning] retention_min_n invalide : {retention_min!r} (un entier >= 1 est attendu)")
    breakdown_min = settings["breakdown_min_n"]
    if isinstance(breakdown_min, bool) or not isinstance(breakdown_min, int) or breakdown_min < 1:
        raise LearningError(f"[learning] breakdown_min_n invalide : {breakdown_min!r} (un entier >= 1 est attendu)")
    days = settings["window_days"]
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days <= 0:
        raise LearningError(f"[learning] window_days invalide : {days!r} (un nombre de jours > 0 est attendu)")
    minimum = settings["min_account_posts"]
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 2:
        raise LearningError(f"[learning] min_account_posts invalide : {minimum!r} (un entier >= 2 est attendu)")
    cases = settings["coach_min_new_cases"]
    if isinstance(cases, bool) or not isinstance(cases, int) or cases < 1:
        raise LearningError(f"[learning] coach_min_new_cases invalide : {cases!r} (un entier >= 1 est attendu)")
    interval = settings["coach_min_interval_days"]
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0:
        raise LearningError(f"[learning] coach_min_interval_days invalide : {interval!r} (un nombre de jours >= 0 est attendu)")
    for key in ("veille_report_days", "veille_report_max"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise LearningError(f"[learning] {key} invalide : {value!r} (un entier >= 1 est attendu)")
    interval_s = settings["veille_report_min_interval_s"]
    if isinstance(interval_s, bool) or not isinstance(interval_s, (int, float)) or interval_s < 0:
        raise LearningError(f"[learning] veille_report_min_interval_s invalide : {interval_s!r} (un nombre de secondes >= 0 est attendu)")
    hours = settings["zero_view_alert_hours"]
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
        raise LearningError(f"[learning] zero_view_alert_hours invalide : {hours!r} (un nombre d'heures > 0 est attendu)")
    max_views = settings["zero_view_alert_max_views"]
    if isinstance(max_views, bool) or not isinstance(max_views, int) or max_views < 0:
        raise LearningError(f"[learning] zero_view_alert_max_views invalide : {max_views!r} (un entier >= 0 est attendu)")
    account_min = settings["zero_view_alert_account_min"]
    if isinstance(account_min, bool) or not isinstance(account_min, int) or account_min < 1:
        raise LearningError(f"[learning] zero_view_alert_account_min invalide : {account_min!r} (un entier >= 1 est attendu)")
    if not isinstance(settings["enabled"], bool):
        raise LearningError(f"[learning] enabled invalide : {settings['enabled']!r} (true ou false attendu)")
    return settings


def _links_path(settings: dict[str, Any]) -> Path:
    return Path(settings["state_dir"]) / "links.json"


def _read_links(settings: dict[str, Any]) -> dict[str, Any]:
    path = _links_path(settings)
    if not path.exists():
        return {"last_run": {}, "snapshots": {}, "unlinked": [], "counts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("objet JSON attendu")
    except (OSError, ValueError) as exc:
        raise LearningError(f"état du rattachement illisible ({path}) : {exc}") from exc
    return {"last_run": {}, "snapshots": {}, "unlinked": [], "counts": {}, **data}


def _write_links(settings: dict[str, Any], links: dict[str, Any]) -> None:
    channel_mod.atomic_write_json(_links_path(settings), links)


def _now_iso(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _wanted_caption(sidecar: dict[str, Any]) -> str:
    return tiktok._squash(" ".join([str(sidecar.get("caption") or ""), *map(str, sidecar.get("hashtags") or [])]))


def _shown_text(shown: Any) -> str:
    return tiktok._squash(shown).rstrip("….").rstrip() if isinstance(shown, str) else ""


def _caption_matches(wanted: str, shown: Any) -> bool:
    """Meme regle que ``tiktok.find_post_link`` : l'une commence par l'autre (texte relevé parfois tronqué)."""
    text = _shown_text(shown)
    return bool(text and wanted and (wanted.startswith(text) or text.startswith(wanted)))


def _caption_equal(wanted: str, shown: Any) -> bool:
    """Legende identique (au tronquage ``…`` pres) : prime sur le simple prefixe."""
    return bool(wanted) and _shown_text(shown) == wanted


def _read_sidecars(config: Config | None) -> list[tuple[Path, dict[str, Any]]]:
    root = Path(config.output_dir if config is not None else "output")
    found = []
    for path in sorted(root.glob("*/*.json")) if root.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("objet JSON attendu")
        except (OSError, ValueError) as exc:
            raise LearningError(f"sidecar illisible pour rattacher les posts ({path}) : {exc}") from exc
        found.append((path, data))
    return found


def _entry_channel(video_id: str, clip_id: str, config: Config | None) -> str | None:
    """Fichier de publication (= chaine) qui porte l'entree du clip, ``None`` s'il n'y en a pas."""
    folder = Path(config.section("publish")["state_dir"] if config is not None else publish.CONFIG_DEFAULTS["state_dir"])
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LearningError(f"file de publication illisible ({path}) : {exc}") from exc
        if isinstance(entries, list) and any(
                isinstance(e, dict) and e.get("video_id") == video_id and e.get("clip_id") == clip_id for e in entries):
            return path.stem
    return None


def link_posts(account: str, *, config: Config | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Relie les clips du compte sans id de post aux posts du dernier releve : legende (regle de
    ``find_post_link``) et date du post a ``link_window_h`` heures de la date prevue. Un seul candidat : id et
    adresse ecrits dans le sidecar et l'entree de publication ; zero ou plusieurs : rien n'est modifie, la raison
    (``none`` | ``ambiguous``) est ecrite dans links.json. Rend ``{"linked": [...], "unlinked": {raison: n}}``."""
    settings = _settings(config)
    with channel_mod.file_lock(_links_path(settings)):  # plusieurs workers : un seul rattache a la fois
        return _link_posts(account, settings, config, now)


def _link_posts(account: str, settings: dict[str, Any], config: Config | None, now: datetime | None) -> dict[str, Any]:
    window = timedelta(hours=float(settings["link_window_h"]))
    history = tiktok.read_history(account, config=config)
    posts = tiktok.merged_posts(history)
    sidecars = _read_sidecars(config)  # d'abord : un sidecar illisible est une LearningError, pas une TikTokError
    taken = {found["post_id"] for found in tiktok._published_posts(account, config)}
    stamp = _now_iso(now)
    linked: list[dict[str, str]] = []
    unlinked: list[dict[str, Any]] = []

    deleted = tiktok.deleted_post_ids(history)  # SPEC-00db R1 : posts supprimés exclus
    todo: list[dict[str, Any]] = []
    for path, sidecar in sidecars:
        post = sidecar.get("tiktok_post")
        if (not isinstance(post, dict) or post.get("account") != account or post.get("id") or post.get("url")
                or sidecar.get("removed_from_platform")):  # post supprimé de la plateforme : jamais rattaché
            continue
        planned = tiktok._naive_utc(post.get("publish_at"))
        wanted = _wanted_caption(sidecar)
        cands: dict[str, bool] = {}  # post_id -> légende identique ?
        for post_id, seen in posts.items():
            if post_id in taken or post_id in deleted or planned is None or not _caption_matches(wanted, seen.get("caption")):
                continue
            posted = tiktok._naive_utc(seen.get("posted_at"))
            if posted is not None and abs(posted - planned) <= window:
                cands[post_id] = _caption_equal(wanted, seen.get("caption"))
        todo.append({"path": path, "sidecar": sidecar, "post": post, "planned": planned, "cands": cands})

    exact_owned = {pid for item in todo for pid, exact in item["cands"].items() if exact}
    for item in todo:
        cands = {pid: exact for pid, exact in item["cands"].items() if exact or pid not in exact_owned}
        item["matches"] = [pid for pid, exact in cands.items() if exact] or list(cands)  # égalité exacte avant préfixe
    rivals: dict[str, int] = {}
    for item in todo:
        for pid in item["matches"]:
            rivals[pid] = rivals.get(pid, 0) + 1
    for item in todo:
        if any(rivals[pid] > 1 for pid in item["matches"]):  # des clips se disputent ces posts : l'heure prévue à la minute départage
            at_minute = [pid for pid in item["matches"]
                         if (posted := tiktok._naive_utc(posts[pid].get("posted_at"))) is not None
                         and posted.replace(second=0, microsecond=0) == item["planned"].replace(second=0, microsecond=0)]
            item["matches"] = at_minute or item["matches"]  # la fenêtre à défaut
    claimed: dict[str, int] = {}
    for item in todo:
        for pid in item["matches"]:
            claimed[pid] = claimed.get(pid, 0) + 1

    for item in todo:
        path, sidecar, post, matches = item["path"], item["sidecar"], item["post"], item["matches"]
        video_id, clip_id = path.parent.name, path.stem
        if len(matches) == 1 and claimed[matches[0]] == 1:
            post_id = matches[0]
            url = posts[post_id].get("post_url")
            channel = _entry_channel(video_id, clip_id, config)
            if channel is not None:
                publish.attach_post(video_id, clip_id, channel, post_url=url, post_id=post_id,
                                    state_dir=config.section("publish")["state_dir"] if config is not None else None)
            sidecar["tiktok_post"] = {**post, "id": post_id, "url": url, "linked_by": "stats", "linked_at": stamp}
            channel_mod.atomic_write_json(path, sidecar)
            linked.append({"video_id": video_id, "clip_id": clip_id, "post_id": post_id})
        else:
            record = {"video_id": video_id, "clip_id": clip_id, "account": account,
                      "reason": "ambiguous" if matches else "none", "matches": sorted(matches), "checked_at": stamp}
            shared = max((claimed[pid] for pid in matches), default=0)
            if len(matches) == 1 and shared > 1:
                record["detail"] = f"post partagé par {shared} clips"
            unlinked.append(record)

    links = _read_links(settings)
    mine = {(r["video_id"], r["clip_id"]) for r in [*unlinked, *linked]}
    kept = [r for r in links["unlinked"] if r.get("account") != account or (r["video_id"], r["clip_id"]) not in mine]
    links["unlinked"] = kept + unlinked
    counts = {"linked": links["counts"].get(account, {}).get("linked", 0) + len(linked)}
    counts.update({reason: sum(1 for r in unlinked if r["reason"] == reason) for reason in REASONS})
    links["counts"][account] = counts
    _write_links(settings, links)
    return {"linked": linked, "unlinked": {reason: counts[reason] for reason in REASONS}}


def _snapshot_names(account: str, config: Config | None) -> list[str]:
    """Fichiers de relevés du compte, tels qu'ils existent maintenant. Un relevé est « neuf » parce que son fichier
    n'a pas encore été traité, jamais parce que son ``fetched_at`` (pris au début du relevé, fichier écrit à la fin)
    dépasse un instant."""
    folder = tiktok._history_dir(account, tiktok.get_settings(config))
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.glob("*.json") if not p.name.endswith(tiktok._ERROR_SUFFIX))


def _all_seen(names: list[str], seen: list[str] | None) -> bool:
    """Vrai si ``seen`` existe (état déjà posé) et couvre chaque fichier de ``names`` ; sans état, tout est dû."""
    return seen is not None and set(names) <= set(seen)


def link_if_due(now: datetime, *, config: Config | None = None) -> list[dict[str, str]]:
    """Passage du worker : traite chaque compte TikTok dont le dernier releve est plus recent que son dernier
    passage (ou jamais traite), note ``last_run`` et rend les rattachements faits. Coupe par ``enabled``."""
    settings = _settings(config)
    if not settings["enabled"]:
        return []
    stats_dir = Path(tiktok.get_settings(config)["stats_dir"])
    accounts = sorted(p.name for p in stats_dir.iterdir() if p.is_dir()) if stats_dir.is_dir() else []
    done: list[dict[str, str]] = []
    for account in accounts:
        history = tiktok.read_history(account, config=config)
        if not history:
            continue
        links = _read_links(settings)
        names = _snapshot_names(account, config)  # avant le traitement : un relevé qui arrive pendant, reste dû
        if _all_seen(names, links["snapshots"].get(account)):
            continue
        done.extend(link_posts(account, config=config, now=now)["linked"])
        with channel_mod.file_lock(_links_path(settings)):
            links = _read_links(settings)
            links["last_run"][account] = now.isoformat()
            links["snapshots"][account] = names
            _write_links(settings, links)
    return done


# ---------------------------------------------------------------- versement stats -> outcomes (SPEC-00db R2-R5, R9)

_CLIP_ID = re.compile(r"(\d{2,})(?:-p\d+)?")
_SYNC_EMPTY: dict[str, Any] = {"last_sync": None, "last_error": None, "results": [], "scored": [], "excluded": [],
                               "accounts": {}, "calibration": None, "snapshots": {}}


def _sync_path(settings: dict[str, Any]) -> Path:
    return Path(settings["state_dir"]) / "sync.json"


def _read_sync(settings: dict[str, Any]) -> dict[str, Any]:
    path = _sync_path(settings)
    if not path.exists():
        return {k: (v.copy() if isinstance(v, (list, dict)) else v) for k, v in _SYNC_EMPTY.items()}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("objet JSON attendu")
    except (OSError, ValueError) as exc:
        raise LearningError(f"état du versement illisible ({path}) : {exc}") from exc
    return {**_SYNC_EMPTY, **data}


def record_error(config: Config | None, where: str, exc: BaseException, now: datetime | None = None) -> None:
    """Écrit l'erreur dans ``sync.json.last_error`` (``{at, where, message}``) ; le prochain versement réussi l'efface."""
    settings = _settings(config)
    with channel_mod.file_lock(_sync_path(settings)):
        state = _read_sync(settings)
        state["last_error"] = {"at": _now_iso(now), "where": where, "message": str(exc)}
        channel_mod.atomic_write_json(_sync_path(settings), state)


def _moment_id(video_id: str, clip_id: str) -> int:
    found = _CLIP_ID.fullmatch(clip_id)
    if not found:
        raise LearningError(f"clip_id {clip_id!r} de {video_id} : la forme NN ou NN-pK est attendue (captions._clip_id)")
    return int(found[1])


def _aware(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class _Account:
    """Relevés d'un compte : pour chaque post, le relevé de maturité retenu, et la référence du rang."""

    def __init__(self, history: list[dict[str, Any]], settings: dict[str, Any], now: datetime) -> None:
        self.latest = tiktok.merged_posts(history)
        self.mature: dict[str, tuple[datetime, str, int, datetime]] = {}  # post -> (fetched, fetched_at, views, posted)
        maturity = timedelta(days=float(settings["maturity_days"]))
        for snapshot in history:
            fetched = _aware(snapshot["fetched_at"])
            for post in snapshot["posts"]:
                posted = tiktok._naive_utc(post.get("posted_at"))
                views = post.get("views")
                if post["post_id"] in self.mature or posted is None or views is None or fetched - posted < maturity:
                    continue
                self.mature[post["post_id"]] = (fetched, snapshot["fetched_at"], views, posted)
        since = now - timedelta(days=float(settings["window_days"]))
        self.reference = {pid: views for pid, (_, _, views, posted) in self.mature.items() if since <= posted <= now}
        self.viewed = sum(1 for views in self.reference.values() if views > 0)
        self.eligible = self.viewed >= settings["min_account_posts"]

    def percentile(self, post_id: str) -> float:
        """Rang fractionnaire 0-1 des vues à maturité du post parmi la référence (ex aequo : rang moyen)."""
        reference = {**self.reference, post_id: self.mature[post_id][2]}
        own = reference[post_id]
        less = sum(1 for v in reference.values() if v < own)
        equal = sum(1 for v in reference.values() if v == own)
        return 0.5 if len(reference) == 1 else (less + (equal + 1) / 2 - 1) / (len(reference) - 1)


def _history_accounts(config: Config | None) -> list[str]:
    stats_dir = Path(tiktok.get_settings(config)["stats_dir"])
    return sorted(p.name for p in stats_dir.iterdir() if p.is_dir()) if stats_dir.is_dir() else []


def _post_id_of(post: dict[str, Any]) -> str | None:
    if post.get("id"):
        return str(post["id"])
    found = tiktok._POST_ID.search(post["url"]) if post.get("url") else None
    return found.group(1) if found else None


def _moment(config: Config | None, video_id: str, moment_id: int, cache: dict[str, Any]) -> dict[str, Any] | None:
    if video_id not in cache:
        path = Path(config.workspace_dir if config is not None else "workspace") / video_id / "moments.json"
        cache[video_id] = None
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cache[video_id] = {m["id"]: m for m in data["moments"]}
                cache[f"{video_id}#judges"] = (data.get("jury") or {}).get("judges") or []  # juges : niveau fichier
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise LearningError(f"moments.json illisible ({path}) : {exc}") from exc
    return (cache[video_id] or {}).get(moment_id)


def _journal_keys(journal_path: str) -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {"result": set(), "stats": set()}
    for entry in outcomes.read(journal_path):
        if entry.get("kind") in keys and entry.get("video_id") and entry.get("clip_id"):
            keys[entry["kind"]].add(f"{entry['video_id']}/{entry['clip_id']}")
    return keys


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _pct_watched(avg_watch_s: Any, duration: Any) -> float | None:
    """Part vue du clip (0 a 1, bornee, 3 decimales) ; null si l'un des deux manque ou si la duree n'est pas > 0 (ADR-ad2e)."""
    watch, length = _number(avg_watch_s), _number(duration)
    if watch is None or length is None or length <= 0:
        return None
    return round(min(1.0, watch / length), 3)  # TikTok compte les boucles dans le temps moyen : jamais > 1


def _perspectives(cache: dict[str, Any], video_id: str) -> dict[str, str] | None:
    """Empreinte de perspective par juge, lue dans moments.json (jury.judges[].perspective_sha, au niveau du
    fichier comme l'ecrit moments.run) ; None si absente (moments.json ancien ou sans jury) : jamais inventee
    (TASK-4e58554d15d3). ``cache`` est celui de ``_moment``, deja rempli pour ``video_id``."""
    judges = cache.get(f"{video_id}#judges") or []
    perspectives = {j["name"]: j["perspective_sha"] for j in judges if j.get("perspective_sha")}
    return perspectives or None


def _moment_source(config: Config | None, video_id: str, moment_id: int, cache: dict[str, Any]) -> str | None:
    moment = _moment(config, video_id, moment_id, cache)
    if moment is None:
        return None
    if "source" not in moment:  # moments.json n'écrit ce champ qu'en transcript+action : sans lui, c'est la transcription
        return "transcript"
    source = moment["source"]
    return source if source in MOMENT_SOURCES else None


def sync(now: datetime, *, config: Config | None = None) -> dict[str, Any]:
    """Verse les résultats dans le journal ``clipper.outcomes`` (SPEC-00db R2-R3, R9) : pour chaque clip relié
    (``tiktok_post.id`` présent dans les relevés du compte), une entrée ``result`` une seule fois et, dès que le
    clip est mûr et son compte éligible, une entrée ``stats`` une seule fois (``views_percentile`` : rang des vues
    à maturité parmi les posts mûrs du compte). Un compte youtube, un compte sous ``min_account_posts``, un clip
    trop jeune : rien dans ``stats``, la raison dans ``sync.json.excluded``. Après l'ajout d'au moins une entrée,
    recalibre les poids du jury (R5) avec les traces de ``moments.json``. Rend le contenu de ``sync.json``."""
    settings = _settings(config)
    journal_path = (config.section("outcomes") if config is not None else outcomes.CONFIG_DEFAULTS)["journal_path"]
    stamp = _now_iso(now)
    with channel_mod.file_lock(_sync_path(settings)):  # plusieurs workers : un seul verse à la fois
        state = _read_sync(settings)
        snapshots = {a: _snapshot_names(a, config) for a in _history_accounts(config)}  # avant la lecture des relevés
        sidecars = _read_sidecars(config)
        youtube_accounts = ({a["id"] for a in accounts_mod.list_accounts(config) if a.get("service") == "youtube"}
                            if config is not None else set())
        done = _journal_keys(journal_path)
        results, scored = set(state["results"]) | done["result"], set(state["scored"]) | done["stats"]
        views = {}
        for account in _history_accounts(config):
            history = tiktok.read_history(account, config=config)
            if history:
                views[account] = _Account(history, settings, now)
        excluded: list[dict[str, Any]] = []
        linked: list[tuple[str, str, int]] = []  # clips reliés ET notés, pour la calibration
        added = 0
        moments: dict[str, Any] = {}

        for path, sidecar in sidecars:
            video_id, clip_id = path.parent.name, path.stem
            removed = sidecar.get("removed_from_platform")
            if removed:  # post supprimé de la plateforme (TASK-5a7b750462c4) : ni résultat, ni stats, ni calibration
                post = sidecar.get("tiktok_post") if isinstance(sidecar.get("tiktok_post"), dict) else sidecar.get("youtube_post")
                excluded.append({"video_id": video_id, "clip_id": clip_id,
                                 "account": post.get("account") if isinstance(post, dict) else None,
                                 "reason": "removed_from_platform"})
                continue
            if isinstance(sidecar.get("youtube_post"), dict):
                excluded.append({"video_id": video_id, "clip_id": clip_id, "account": sidecar["youtube_post"].get("account"),
                                 "reason": "service_without_stats"})
                continue
            post = sidecar.get("tiktok_post")
            if not isinstance(post, dict) or _post_id_of(post) is None:
                continue
            account, post_id, key = post.get("account"), _post_id_of(post), f"{video_id}/{clip_id}"
            where = {"video_id": video_id, "clip_id": clip_id, "account": account}
            if account in youtube_accounts:
                excluded.append({**where, "reason": "service_without_stats"})
                continue
            moment_id = _moment_id(video_id, clip_id)
            seen = views.get(account)
            if seen is None or post_id not in seen.latest:
                excluded.append({**where, "reason": "not_in_stats"})
                continue
            if key not in results:
                outcomes.record(video_id, clip_id, moment_id, qa=sidecar.get("qa"), human_decision=None, path=journal_path)
                results.add(key)
                added += 1
            if post_id not in seen.mature:
                excluded.append({**where, "reason": "immature"})
            elif not seen.eligible:
                excluded.append({**where, "reason": "account_below_min"})
            elif key not in scored:
                fetched, fetched_at, at_maturity, posted = seen.mature[post_id]
                row = seen.latest[post_id]
                pct_watched = _pct_watched(row.get("avg_watch_s"), sidecar.get("duration"))
                entry = {
                    "kind": "stats", "video_id": video_id, "clip_id": clip_id, "moment_id": moment_id, "post_id": post_id,
                    "account": account, "posted_at": row.get("posted_at"), "fetched_at": fetched_at,
                    "age_days": round((fetched - posted).total_seconds() / 86400, 2),
                    "stats": {"views": row.get("views"), "views_at_maturity": at_maturity,
                              "views_percentile": seen.percentile(post_id),
                              **{k: row.get(k) for k in ("likes", "comments", "shares", "avg_watch_s", "watched_full",
                                                         "new_followers")},
                              "pct_watched": pct_watched},  # aussi dans stats : la calibration y lit sa métrique
                    "recorded_at": stamp,
                    "duration": _number(sidecar.get("duration")),
                    "pct_watched": pct_watched,
                    "moment_source": _moment_source(config, video_id, moment_id, moments),
                }
                moment = _moment(config, video_id, moment_id, moments)
                if (moment or {}).get("exploration") is True:
                    entry["exploration"] = True
                perspectives = _perspectives(moments, video_id)
                if perspectives is not None:
                    entry["perspectives"] = perspectives
                outcomes._append(entry, journal_path)
                scored.add(key)
                added += 1
            if key in scored:  # seul un clip qui a des statistiques compte dans la calibration (ADR-1cf0, ADR-ad2e)
                linked.append((video_id, clip_id, moment_id))

        state.update(
            last_sync=stamp, snapshots=snapshots, results=sorted(results), scored=sorted(scored), excluded=excluded,
            accounts={a: {"mature_posts": len(v.reference), "viewed_posts": v.viewed, "eligible": v.eligible}
                      for a, v in sorted(views.items())})
        retry = isinstance(state["last_error"], dict) and state["last_error"].get("where") == "calibrate"
        state["last_error"] = None
        if added or retry:
            try:
                state["calibration"] = _calibrate(linked, config, now, moments)
            except Exception as exc:
                state["last_error"] = {"at": stamp, "where": "calibrate", "message": str(exc)}
                channel_mod.atomic_write_json(_sync_path(settings), state)
                exc.where = "calibrate"  # type: ignore[attr-defined]
                raise
        channel_mod.atomic_write_json(_sync_path(settings), state)
        return state


def _calibrate(linked: list[tuple[str, str, int]], config: Config | None, now: datetime, cache: dict[str, Any]) -> dict[str, Any]:
    """Recalibre les poids du jury (R5) avec les clips reliés dont le moment porte ``jury.trace`` ; les autres sont comptés."""
    traces: dict[tuple[str, int], dict[str, Any]] = {}
    untraced = 0
    for video_id, _, moment_id in linked:
        trace = ((_moment(config, video_id, moment_id, cache) or {}).get("jury") or {}).get("trace")
        if trace is None:
            untraced += 1
        else:
            traces[(video_id, moment_id)] = {"video_id": video_id, "moment_id": moment_id, "candidate": {"trace": trace}}
    section = config.section("jury_calibration") if config is not None else jury_calibration.CONFIG_DEFAULTS
    metric = section["stats_metric"]
    journal = outcomes.read((config.section("outcomes") if config is not None else outcomes.CONFIG_DEFAULTS)["journal_path"])
    stats = [e for e in journal if e.get("kind") == "stats"]
    if stats and not any((e.get("stats") or {}).get(metric) is not None for e in stats):  # ADR-ad2e : jamais en silence
        raise jury_calibration.CalibrationError(  # avant tout appel qui écrit jury_weights.json
            f"[jury_calibration] stats_metric = {metric!r} : aucune des {len(stats)} entrées stats du journal ne porte "
            "cette métrique, la calibration n'a rien appris")
    jury_calibration.calibrate(list(traces.values()), config=config, now=now)
    weights = section["weights_path"]
    return {"at": _now_iso(now), "clips": len(traces), "untraced": untraced, "weights_path": str(weights)}


def _snapshot_newer_than_sync(settings: dict[str, Any], config: Config | None) -> bool:
    seen = _read_sync(settings).get("snapshots") or {}
    return any(not _all_seen(_snapshot_names(a, config), seen.get(a))
               for a in _history_accounts(config) if tiktok.read_history(a, config=config))


_veille_report_computed: dict[str, datetime] = {}  # dernier calcul du bilan par fichier (TASK-1e46f) : mémoire du worker


def run_if_due(now: datetime, *, config: Config | None = None) -> dict[str, Any]:
    """Passage du worker (SPEC-00db R4) : ``link_if_due``, puis ``sync`` seulement si un relevé est plus récent que
    ``sync.json.last_sync``. Coupé par ``enabled``. Une erreur porte ``where`` (``link`` | ``sync`` | ``calibrate``) et
    remonte : le worker l'écrit dans ``sync.json.last_error`` et la journalise."""
    settings = _settings(config)
    if not settings["enabled"]:
        return {"linked": [], "synced": False}
    try:
        linked = link_if_due(now, config=config)
    except Exception as exc:
        exc.where = "link"  # type: ignore[attr-defined]
        raise
    due = _snapshot_newer_than_sync(settings, config)
    if due:
        try:
            sync(now, config=config)
        except Exception as exc:
            if not hasattr(exc, "where"):
                exc.where = "sync"  # type: ignore[attr-defined]
            raise
    target = str(_veille_report_target(config))  # bilan : recalculé au plus une fois par intervalle, ou dès qu'un relevé tourne
    last = _veille_report_computed.get(target)
    if due or last is None or now - last >= timedelta(seconds=settings["veille_report_min_interval_s"]):
        try:
            write_veille_report(now, config=config)
        except Exception as exc:
            exc.where = "veille_report"  # type: ignore[attr-defined]
            raise
        _veille_report_computed[target] = now
    try:  # apres le releve : une alerte ne se journalise qu'une fois par post (TASK-974e)
        log_zero_view_alerts(now, config=config)
    except Exception as exc:
        exc.where = "zero_views"  # type: ignore[attr-defined]
        raise
    try:
        coached = coach_if_due(now, config=config)
    except (jury_coach.CoachError, jury.JuryError, llm.LLMError) as exc:
        message = str(exc)
        record_error(config, "coach", exc, now)
        if message not in _logged_coach_errors:
            _logged_coach_errors.add(message)
            log.error("coach des prompts impossible : %s", message)
        coached = []
    return {"linked": linked, "synced": due, "coached": coached}


# ---------------------------------------------------------------- coach des prompts (SPEC-00db R6-R7)

_logged_coach_errors: set[str] = set()
EXCLUDED_JUDGES = jury_coach.EXCLUDED_JUDGES  # conformite : jamais coache, donc jamais adopte


class ProposalError(LearningError):
    """Proposition du coach introuvable (``status`` 404) ou deja decidee (409)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _coach_path(settings: dict[str, Any]) -> Path:
    return Path(settings["state_dir"]) / "coach.json"


def _read_coach(settings: dict[str, Any]) -> dict[str, Any]:
    path = _coach_path(settings)
    if not path.exists():
        return {"last_run": None, "runs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("runs", []), list):
            raise ValueError("objet JSON avec une liste runs attendu")
    except (OSError, ValueError) as exc:
        raise LearningError(f"état du coach illisible ({path}) : {exc}") from exc
    return {"last_run": None, "runs": [], **data}


def read_coach(config: Config | None) -> dict[str, Any]:
    """``state/learning/coach.json`` : ``{last_run, runs: [{at, cases, judges: [...]}]}`` (vide s'il n'existe pas)."""
    return _read_coach(_settings(config))


def _new_scored(scored: set[str], last_run: datetime | None, journal_path: str) -> int:
    """Clips scored dont l'entree ``stats`` du journal date d'apres ``last_run`` (tous, s'il n'a jamais tourne)."""
    if last_run is None:
        return len(scored)
    fresh = {f"{e['video_id']}/{e['clip_id']}" for e in outcomes.read(journal_path)
             if e.get("kind") == "stats" and _aware(e["recorded_at"]) > last_run}
    return len(scored & fresh)


def _case_rubric(config: Config, video_id: str) -> dict[str, Any]:
    """Grille qui a note la video : ``rubric.path`` de son moments.json (ecrit par l'etape moments), chargee.
    ``LearningError`` si elle est absente ou illisible : jamais une autre grille (ADR-ad2e)."""
    from clipper import moments  # lazy : la grille est lue, aucune etape n'est lancee
    path = Path(config.workspace_dir) / video_id / "moments.json"
    try:
        rubric = json.loads(path.read_text(encoding="utf-8")).get("rubric")
    except (OSError, ValueError, AttributeError) as exc:
        raise LearningError(f"moments.json illisible ({path}) : {exc}") from exc
    value = rubric.get("path") if isinstance(rubric, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise LearningError(f"{path} : rubric.path absent, grille du clip inconnue")
    try:
        criteria = moments.load_rubric(value)["criteria"]
    except moments.MomentsError as exc:
        raise LearningError(f"grille {value} illisible : {exc}") from exc
    return {"id": str(rubric.get("source") or value), "criteria": criteria}


def _coach_cases(scored: set[str], config: Config, skipped: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Cas du coach, chacun avec sa grille. Un clip dont la grille est illisible est ecarte et ajoute a
    ``skipped`` (``{video_id, moment_id, reason}``), jamais rejoue avec une autre grille."""
    rubrics: dict[str, Any] = {}
    sidecars = {f"{p.parent.name}/{p.stem}": d for p, d in _read_sidecars(config)}
    cache: dict[str, Any] = {}
    cases = []
    skipped = skipped if skipped is not None else []
    for key in sorted(scored):
        video_id, clip_id = key.split("/", 1)
        sidecar = sidecars.get(key)
        if sidecar is None:
            continue
        moment_id = _moment_id(video_id, clip_id)
        trace = ((_moment(config, video_id, moment_id, cache) or {}).get("jury") or {}).get("trace")
        if trace is None:
            continue
        if video_id not in rubrics:
            try:
                rubrics[video_id] = _case_rubric(config, video_id)
            except LearningError as exc:
                rubrics[video_id] = str(exc)
        rubric = rubrics[video_id]
        if isinstance(rubric, str):
            skipped.append({"video_id": video_id, "moment_id": moment_id, "reason": rubric})
            log.warning("coach : clip %s ecarte, grille illisible : %s", key, rubric)
            continue
        context = " — ".join(str(sidecar[k]) for k in ("source_title", "screen_title") if sidecar.get(k))
        cases.append({"video_id": video_id, "moment_id": moment_id, "text": str(sidecar.get("transcript") or ""),
                      "context": context, "trace": trace, "rubric": rubric})
    return cases


def active_perspectives(config: Config) -> dict[str, str]:
    """Juge actif -> perspective en place (defauts de ``clipper.jury`` surcharges par ``[jury.judges.*]``)."""
    merged = jury._deep_merge(jury.CONFIG_DEFAULTS, config.section("jury"))
    return {j["name"]: j["perspective"] for j in jury._judges(merged)}


def coach_if_due(now: datetime, *, config: Config | None = None) -> list[dict[str, Any]]:
    """Passage du coach (R6) : au moins ``coach_min_new_cases`` clips scored nouveaux depuis ``last_run`` et
    ``coach_min_interval_days`` ecoules, sinon aucun appel LLM. Sinon ``jury_coach.propose`` sur les clips scored
    dont le moment porte une trace ; chaque entree rendue est consignee dans ``coach.json`` (``proposed`` si
    acceptee, ``rejected`` sinon). Rien n'est applique : ni ``config.toml`` ni ``jury.py`` ni les poids ne bougent.
    Rend les entrees consignees (liste vide si rien n'etait du)."""
    settings = _settings(config)
    if not settings["enabled"]:
        return []
    if config is None:
        from clipper.config import load_config
        config = load_config()
    journal_path = config.section("outcomes")["journal_path"]
    with channel_mod.file_lock(_coach_path(settings)):
        coach = _read_coach(settings)
        last_run = _aware(coach["last_run"]) if coach["last_run"] else None
        if last_run is not None and now - last_run < timedelta(days=float(settings["coach_min_interval_days"])):
            return []
        scored = set(_read_sync(settings)["scored"])
        if _new_scored(scored, last_run, journal_path) < settings["coach_min_new_cases"]:
            return []
        skipped: list[dict[str, Any]] = []
        cases = _coach_cases(scored, config, skipped)
        if not cases:
            return []
        merged = jury._deep_merge(jury.CONFIG_DEFAULTS, config.section("jury"))
        judge_configs = {j["name"]: jury._JudgeConfig(config, j["usage"], j["model"]) for j in jury._judges(merged)}
        results = jury_coach.propose(cases, None, active_perspectives(config), config=config, now=now,
                                     judge_configs=judge_configs,
                                     usage_log_path=Path(settings["state_dir"]) / "llm_usage.jsonl")
        entries = [{**r, "status": "proposed" if r["accepted"] else "rejected", "decided_at": None, "decided_by": None}
                   for r in results]
        coach["runs"].append({"at": now.isoformat(), "cases": len(cases), "skipped": skipped, "judges": entries})
        coach["last_run"] = now.isoformat()
        channel_mod.atomic_write_json(_coach_path(settings), coach)
        return entries


_VERSION_BODY = re.compile(r"\A# [^\n]*\n\n(.*?)\n\n## Justification\n", re.DOTALL)


def proposal_perspective(path: str | Path) -> str:
    """Perspective proposee, lue dans ``prompts/jury/<juge>/vN.md`` (entre le titre et « Justification »)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LearningError(f"proposition du coach illisible ({path}) : {exc}") from exc
    found = _VERSION_BODY.match(text)
    if not found or not found[1].strip():
        raise LearningError(f"proposition du coach mal formée ({path}) : perspective introuvable")
    return found[1].strip()


def _find_entry(coach: dict[str, Any], judge: str, version: int) -> dict[str, Any]:
    for run in coach["runs"]:
        for entry in run["judges"]:
            if entry.get("judge") == judge and entry.get("version") == version and entry.get("accepted"):
                return entry
    raise ProposalError(f"aucune proposition du coach pour {judge} v{version}", 404)


def find_proposal(config: Config | None, judge: str, version: int) -> dict[str, Any]:
    """Proposition acceptee par le coach pour ``judge`` ``version`` (``ProposalError`` 404 si inconnue)."""
    return _find_entry(read_coach(config), judge, version)


def decide_proposal(config: Config | None, judge: str, version: int, status: str, *, by: str,
                    now: datetime | None = None) -> dict[str, Any]:
    """Marque la proposition ``adopted`` ou ``refused`` (``decided_at``, ``decided_by``) ; deja decidee =
    ``ProposalError`` 409. N'ecrit que ``coach.json`` : appliquer la perspective est l'affaire de l'appelant."""
    if status not in ("adopted", "refused"):
        raise LearningError(f"décision inconnue {status!r} (adopted ou refused attendu)")
    settings = _settings(config)
    with channel_mod.file_lock(_coach_path(settings)):
        coach = _read_coach(settings)
        entry = _find_entry(coach, judge, version)
        if entry["status"] != "proposed":
            raise ProposalError(f"proposition {judge} v{version} déjà {entry['status']}", 409)
        entry.update(status=status, decided_at=_now_iso(now), decided_by=by)
        channel_mod.atomic_write_json(_coach_path(settings), coach)
        return entry


def proposals(config: Config | None) -> list[dict[str, Any]]:
    """Propositions du coach acceptees (proposed | adopted | refused), la plus recente d'abord, avec la perspective
    proposee (lue dans ``prompts/jury/<juge>/vN.md``) et celle en place dans la config du jury. Lecture seule."""
    coach = read_coach(config)
    try:
        current = active_perspectives(config) if config is not None else {}
    except jury.JuryError:
        current = {}
    found = []
    for run in reversed(coach["runs"]):
        for entry in run["judges"]:
            if not entry.get("accepted"):
                continue
            try:
                proposed, error = proposal_perspective(entry["path"]), None
            except LearningError as exc:
                proposed, error = None, str(exc)
            found.append({"judge": entry["judge"], "version": entry["version"], "metric": entry["metric"], "at": run["at"],
                          "perspective_proposed": proposed, "perspective_current": current.get(entry["judge"]),
                          "status": entry["status"], "decided_at": entry["decided_at"], "decided_by": entry["decided_by"],
                          "error": error})
    return found


def _retention(config: Config | None, settings: dict[str, Any]) -> dict[str, Any]:
    """Tableau de retention a maturite (une ligne par clip scored, meilleure part vue d'abord) ; aucune correlation."""
    journal_path = (config.section("outcomes") if config is not None else outcomes.CONFIG_DEFAULTS)["journal_path"]
    try:
        entries = outcomes.read(journal_path)
    except (OSError, ValueError) as exc:  # JSONDecodeError (ligne tronquée) inclus : jamais ignoré
        raise LearningError(f"journal des résultats illisible ({journal_path}) : {exc}") from exc
    rows = [{"video_id": e["video_id"], "clip_id": e["clip_id"], "duration": e.get("duration"),
             "watched_full": (e.get("stats") or {}).get("watched_full"), "pct_watched": e.get("pct_watched"),
             "views_percentile": (e.get("stats") or {}).get("views_percentile"),
             "moment_source": e.get("moment_source")}
            for e in entries if e.get("kind") == "stats"]
    rows.sort(key=lambda r: (r["pct_watched"] is None, -(r["pct_watched"] or 0), r["video_id"], r["clip_id"]))
    n, minimum = len(rows), settings["retention_min_n"]
    message = f"n = {n}, trop peu pour conclure (minimum {minimum})" if n < minimum else None
    return {"n": n, "min_n": minimum, "message": message, "rows": rows}


# ---------------------------------------------------------------- vues médianes par jeu, streamer, heure, compte

_UNKNOWN = "inconnu"
_BREAKDOWN_LABELS = {"game": "jeu inconnu", "streamer": "streamer inconnu", "hour": "heure inconnue", "account": "compte inconnu"}


def _json_file(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LearningError(f"{what} illisible : {path.name} ({path}) : {exc}") from exc


def _veille_days_games(config: Config | None) -> dict[str, str]:
    """``video_id`` du worker (préfixe ``v`` pour Twitch) -> ``game_name`` : ``seen.json`` queued d'abord (SPEC-6d1f,
    SPEC-8a45 : une VOD mise en file garde son jeu même quand son jour est rejoué), puis les candidats de ``days/*.json``,
    le plus récent gagnant. Même correspondance d'identifiant que ``repartition`` : forme brute Twitch et forme ``v<id>``."""
    sdir = Path(config.section("veille")["state_dir"] if config is not None else "state/veille")
    days = sdir / "days"
    games: dict[str, str] = {}
    seen_path = sdir / "seen.json"
    seen = _json_file(seen_path, "seen.json de la veille") if seen_path.exists() else {}
    queued = seen.get("queued", []) if isinstance(seen, dict) else []
    for item in queued if isinstance(queued, list) else []:
        if isinstance(item, dict) and item.get("video_id") and item.get("game_name"):
            raw = str(item["video_id"])
            for video in (raw, raw if raw.startswith("v") else f"v{raw}"):
                games.setdefault(video, item["game_name"])
    for path in sorted(days.glob("*.json"), reverse=True) if days.is_dir() else []:
        data = _json_file(path, "jour de la veille")
        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        for item in candidates if isinstance(candidates, list) else []:
            if isinstance(item, dict) and item.get("video_id") and item.get("game_name"):
                video = str(item["video_id"])
                if item.get("source") == "twitch" and not video.startswith("v"):
                    video = f"v{video}"
                games.setdefault(video, item["game_name"])
    return games


def _post_hour(entry: dict[str, Any]) -> int | None:
    """Heure pleine de Paris du post : ``posted_at`` des relevés est déjà l'heure de Paris naïve (jamais convertie),
    sinon ``slot_at`` de l'entrée (converti seulement s'il porte un fuseau)."""
    for field in ("posted_at", "slot_at"):
        value = entry.get(field)
        if not isinstance(value, str) or not value:
            continue
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            continue
        if moment.tzinfo is not None:
            moment = moment.astimezone(ZoneInfo(accounts_mod.DEFAULT_TIMEZONE))
        return moment.hour
    return None


def _breakdown(config: Config | None, settings: dict[str, Any]) -> dict[str, Any]:
    journal_path = (config.section("outcomes") if config is not None else outcomes.CONFIG_DEFAULTS)["journal_path"]
    try:
        entries = outcomes.read(journal_path)
    except (OSError, ValueError) as exc:
        raise LearningError(f"journal des résultats illisible ({journal_path}) : {exc}") from exc
    workspace = Path(config.workspace_dir) if config is not None else Path("workspace")
    metas: dict[str, dict[str, Any]] = {}
    veille_games: dict[str, str] | None = None
    rows: list[dict[str, Any]] = []
    skipped = 0
    for entry in entries:
        if entry.get("kind") != "stats":
            continue
        views = (entry.get("stats") or {}).get("views_at_maturity")
        if isinstance(views, bool) or not isinstance(views, (int, float)):
            skipped += 1
            continue
        video = entry.get("video_id")
        if video not in metas:
            path = workspace / str(video) / "meta.json"
            data = _json_file(path, "meta.json") if path.exists() else {}
            metas[video] = data if isinstance(data, dict) else {}
        meta = metas[video]
        game = meta.get("game") or None
        if game is None:
            if veille_games is None:
                veille_games = _veille_days_games(config)
            game = veille_games.get(video)
        hour = _post_hour(entry)
        keys = {"game": game, "streamer": meta.get("channel") or None, "hour": None if hour is None else f"{hour:02d}",
                "account": entry.get("account") or None}
        rows.append({"video_id": video, "clip_id": entry.get("clip_id"), "views": views, "pct": entry.get("pct_watched"),
                     "keys": keys})
    groups: dict[str, list[dict[str, Any]]] = {}
    for name in _BREAKDOWN_LABELS:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(row["keys"][name] or _UNKNOWN, []).append(row)
        lines = []
        for key, members in buckets.items():
            pcts = [m["pct"] for m in members if isinstance(m["pct"], (int, float)) and not isinstance(m["pct"], bool)]
            best = max(members, key=lambda m: m["views"])
            label = _BREAKDOWN_LABELS[name] if key == _UNKNOWN else f"{key} h" if name == "hour" else key
            lines.append({"key": key, "label": label, "n": len(members),
                          "median_views": statistics.median(m["views"] for m in members),
                          "median_pct_watched": statistics.median(pcts) if pcts else None,
                          "best": {"video_id": best["video_id"], "clip_id": best["clip_id"], "views": best["views"]},
                          "few": len(members) < settings["breakdown_min_n"]})
        lines.sort(key=lambda g: (-g["median_views"], g["key"]))
        groups[name] = lines
    return {"n": len(rows), "min_n": settings["breakdown_min_n"], "skipped": skipped, "groups": groups}


def breakdown(config: Config | None) -> dict[str, Any]:
    """Vues médianes à maturité par jeu, streamer, heure pleine de Paris et compte, depuis les entrées ``stats`` du
    journal des résultats. Rien d'estimé : une entrée sans vues est ignorée et comptée dans ``skipped`` ; un groupe sous
    ``breakdown_min_n`` porte ``few`` mais reste affiché. Lecture seule, sans réseau."""
    return _breakdown(config, _settings(config))


def status(config: Config | None) -> dict[str, Any]:
    """Etat de la boucle pour l'ecran Statistiques (lecture seule : aucun calcul, aucun appel LLM)."""
    settings = _settings(config)
    weights_path = Path((config.section("jury_calibration") if config is not None else jury_calibration.CONFIG_DEFAULTS)["weights_path"])
    try:
        weights = json.loads(weights_path.read_text(encoding="utf-8")) if weights_path.exists() else None
    except (OSError, ValueError) as exc:
        raise LearningError(f"poids du jury illisibles ({weights_path}) : {exc}") from exc
    return {"enabled": settings["enabled"], "links": _read_links(settings), "sync": _read_sync(settings),
            "weights": weights, "coach": proposals(config), "retention": _retention(config, settings),
            "breakdown": _breakdown(config, settings)}


# ---------------------------------------------------------------- bilan des VOD de veille (SPEC-00db R8)


def _read_state_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LearningError(f"fichier d'état de la veille illisible : {path.name} ({path}) : {exc}") from exc


def _processing_videos(config: Config | None) -> set[str]:
    """VOD encore dans la file du worker (waiting ou running) : leurs clips ne sont pas finis, ce n'est pas un zéro."""
    if config is not None:
        path = Path(str(config.section("worker")["queue_path"]))
    else:
        from clipper import worker  # import local : worker importe learning
        path = Path(str(worker.CONFIG_DEFAULTS["queue_path"]))
    entries = _read_state_json(path, [])
    try:
        return {e["video_id"] for e in entries}
    except (KeyError, TypeError) as exc:
        raise LearningError(f"file d'attente illisible : {path.name} ({path}) : {exc}") from exc


def _vod_missing(clips: int, published: int, mature: int, excluded: list[dict[str, Any]], processing: bool) -> str | None:
    if mature:
        return None
    if clips == 0:
        return "processing" if processing else "no_clips"
    if published == 0:
        return "not_published"
    return "account_below_min" if any(e.get("reason") == "account_below_min" for e in excluded) else "immature"


def _veille_report_target(config: Config | None) -> Path:
    return Path(config.section("veille")["state_dir"] if config is not None else "state/veille") / "bilan.json"


def _same_report(path: Path, report: dict[str, Any]) -> bool:
    """Le fichier porte déjà ce contenu hors ``computed_at`` (lu sous le verrou). Illisible : faux, donc réécrit."""
    if not path.exists():
        return False
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(previous, dict):
        return False
    return ({k: v for k, v in previous.items() if k != "computed_at"}
            == {k: v for k, v in report.items() if k != "computed_at"})


def write_veille_report(now: datetime, *, config: Config | None = None) -> dict[str, Any]:
    """Écrit ``<[veille] state_dir>/bilan.json`` (SPEC-00db R8) : pour les VOD mises en file (``seen.json``) dans les
    ``veille_report_days`` derniers jours, au plus ``veille_report_max``, les plus récentes d'abord : clips produits
    (sidecars réels de la VOD), ``processing`` (encore dans la file du worker), clips publiés, clips mûrs, rang moyen et vues max à maturité lus dans les entrées ``stats`` du journal (jamais recalculés), ou
    ``missing`` (la raison) et des chiffres ``null``. Le fichier est le contrat avec ``clipper.veille`` (qui ne
    l'importe pas) ; déterministe hors ``computed_at``. N'écrit le fichier que si son contenu hors ``computed_at``
    change (TASK-1e46f) ; rend le rapport dans tous les cas."""
    settings = _settings(config)
    sdir = _veille_report_target(config).parent
    journal_path = (config.section("outcomes") if config is not None else outcomes.CONFIG_DEFAULTS)["journal_path"]
    seen = _read_state_json(sdir / "seen.json", {"queued": []})
    since = now - timedelta(days=int(settings["veille_report_days"]))
    queued = [q for q in seen.get("queued", []) if _aware(q["at"]) >= since]
    queued.sort(key=lambda q: _aware(q["at"]), reverse=True)
    queued = queued[:int(settings["veille_report_max"])]
    sidecars: dict[str, list[dict[str, Any]]] = {}
    for path, sidecar in _read_sidecars(config):
        sidecars.setdefault(path.parent.name, []).append(sidecar)
    stats: dict[str, list[dict[str, Any]]] = {}
    for entry in outcomes.read(journal_path):
        if entry.get("kind") == "stats" and entry.get("video_id"):
            stats.setdefault(entry["video_id"], []).append(entry["stats"])
    excluded = _read_sync(settings)["excluded"]
    processing_videos = _processing_videos(config)
    entries = []
    for item in queued:
        video = item["video_id"]
        if item.get("title"):  # instantané écrit à la décision (SPEC-8a45 R32) : survit au rejeu du jour
            candidate = item
        else:  # ancienne entrée : repli sur le fichier du jour, tout ou rien (R33)
            day = _read_state_json(sdir / "days" / f"{item['date']}.json", {"proposals": []})
            proposal = next((p for p in day.get("proposals", []) if p.get("candidate_id") == item["candidate_id"]), None)
            candidate = (proposal or {}).get("candidate") or {}
        clips = sidecars.get(video, [])
        published = sum(1 for s in clips if isinstance(s.get("tiktok_post"), dict) and not s.get("removed_from_platform"))
        rows = stats.get(video, [])
        ranks = [r["views_percentile"] for r in rows if r.get("views_percentile") is not None]
        views = [r["views_at_maturity"] for r in rows if r.get("views_at_maturity") is not None]
        entries.append({
            "picked_on": item["date"], "candidate_id": item["candidate_id"], "source": candidate.get("source"),
            "game_name": candidate.get("game_name"), "channel_name": candidate.get("channel_name"),
            "title": candidate.get("title"), "video_id": video, "clips_produced": len(clips), "processing": video in processing_videos,
            "clips_published": published, "clips_mature": len(rows),
            "views_percentile_mean": sum(ranks) / len(ranks) if ranks else None,
            "views_at_maturity_max": max(views) if views else None,
            "missing": _vod_missing(len(clips), published, len(rows), [e for e in excluded if e.get("video_id") == video],
                                  video in processing_videos),
        })
    report = {"computed_at": _now_iso(now), "days": settings["veille_report_days"], "entries": entries}
    target = _veille_report_target(config)
    with channel_mod.file_lock(target):
        if not _same_report(target, report):
            channel_mod.atomic_write_json(target, report)
    return report

# ---------------------------------------------------------------- alerte « 0 vue à 24 h » (TASK-974e)

_ZERO_VIEWS_LOG = "zero_views.json"  # posts déjà journalisés en WARNING : une seule ligne par post


def _zero_views_path(settings: dict[str, Any]) -> Path:
    return Path(settings["state_dir"]) / _ZERO_VIEWS_LOG


def zero_view_alerts(now: datetime, *, config: Config | None = None) -> dict[str, Any]:
    """Posts TikTok en ligne depuis au moins ``zero_view_alert_hours`` dont le dernier relevé donne au plus
    ``zero_view_alert_max_views`` vues (TASK-974e). Lecture seule, sans réseau : elle lit les relevés déjà faits.
    Un compte avec ``zero_view_alert_account_min`` posts en alerte devient une alerte de compte. Ignorés : posts
    supprimés de la plateforme (sidecar ou relevé complet), comptes en pause manuelle, vues non affichées. Un post
    lié à un clip, sans relevé après le délai, est rendu à part dans ``no_reading``, jamais compté à zéro (ADR-ad2e).
    Rend ``{hours, accounts: [{account, level, posts}], no_reading: [...]}``."""
    settings = _settings(config)
    hours = float(settings["zero_view_alert_hours"])
    max_views = settings["zero_view_alert_max_views"]
    minimum = settings["zero_view_alert_account_min"]
    delay = timedelta(hours=hours)
    sidecars = _read_sidecars(config)
    accounts = accounts_mod.list_accounts(config) if config is not None else []
    paused = {a["id"] for a in accounts if accounts_mod.pause_reason(a)}
    removed: set[tuple[Any, str]] = set()  # (compte, id) supprimés de la plateforme (TASK-5a7b)
    linked: dict[tuple[Any, str], tuple[str, str]] = {}  # (compte, id) -> (video_id, clip_id)
    for path, sidecar in sidecars:
        post = sidecar.get("tiktok_post")
        if not isinstance(post, dict) or not _post_id_of(post):
            continue
        key = (post.get("account"), _post_id_of(post))
        linked[key] = (path.parent.name, path.stem)
        if sidecar.get("removed_from_platform"):
            removed.add(key)

    found: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, tuple[dict[str, Any], set[str]]] = {}  # compte -> (posts releves, posts supprimes)
    for account in _history_accounts(config):
        if account in paused:
            continue
        history = tiktok.read_history(account, config=config)
        if not history:
            continue
        latest, deleted = tiktok.merged_posts(history), tiktok.deleted_post_ids(history)
        seen[account] = (latest, deleted)
        for post_id, row in latest.items():
            posted, views = tiktok._naive_utc(row.get("posted_at")), row.get("views")
            if post_id in deleted or (account, post_id) in removed or posted is None or views is None:
                continue
            if now - posted >= delay and views <= max_views:
                video_id, clip_id = linked.get((account, post_id), (None, None))
                found.setdefault(account, []).append({
                    "video_id": video_id, "clip_id": clip_id, "post_id": post_id, "posted_at": row.get("posted_at"),
                    "views": views, "read_at": row.get("last_seen")})

    no_reading: list[dict[str, Any]] = []
    for path, sidecar in sidecars:  # posts lies a un clip, absents des releves apres le delai
        post = sidecar.get("tiktok_post")
        if not isinstance(post, dict) or not _post_id_of(post) or post.get("account") in paused:
            continue
        account, post_id = post.get("account"), _post_id_of(post)
        published = tiktok._naive_utc(post.get("publish_at"))
        if (account, post_id) in removed or published is None or now - published < delay:
            continue
        latest, deleted = seen.get(account, ({}, set()))
        if post_id in deleted or post_id in latest:
            continue
        no_reading.append({"video_id": path.parent.name, "clip_id": path.stem, "account": account, "post_id": post_id,
                           "reason": "no_reading"})

    accounts_out = []
    for account in sorted(found):
        posts = sorted(found[account], key=lambda p: (p["posted_at"] or "", p["post_id"]))
        accounts_out.append({"account": account, "level": "account" if len(posts) >= minimum else "post", "posts": posts})
    return {"hours": hours, "accounts": accounts_out, "no_reading": no_reading}


def log_zero_view_alerts(now: datetime, *, config: Config | None = None) -> dict[str, Any]:
    """Journalise en WARNING chaque post en alerte une seule fois (``state/learning/zero_views.json``) ; rend les
    alertes lues par ``zero_view_alerts``."""
    settings = _settings(config)
    alerts = zero_view_alerts(now, config=config)
    path = _zero_views_path(settings)
    logged = set(_read_state_json(path, {"logged": []}).get("logged", []))
    fresh = []
    for group in alerts["accounts"]:
        for post in group["posts"]:
            key = f"{group['account']}/{post['post_id']}"
            if key in logged:
                continue
            log.warning("0 vue à %d h : post %s du compte %s (clip %s/%s) publié le %s, %s vue(s) au relevé du %s%s",
                        int(alerts["hours"]), post["post_id"], group["account"], post["video_id"], post["clip_id"],
                        post["posted_at"], post["views"], post["read_at"],
                        " : le compte ne diffuse peut-être plus" if group["level"] == "account" else "")
            fresh.append(key)
    if fresh:
        channel_mod.atomic_write_json(path, {"logged": sorted(logged | set(fresh))})
    return alerts
