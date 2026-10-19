"""repartition.py : plan du lendemain par compte TikTok, prepare chaque soir (SPEC-78dc R0-R7).

Bibliotheque (ADR-b16b) : n'importe ni ``clipper.web`` ni une etape du pipeline ; importe ``publish``, ``accounts``,
``tiktok`` et ``channel``. Le plan est un fichier JSON atomique ``<state_dir>/<AAAA-MM-JJ>.json`` ecrit sous verrou
(ADR-35b7) : le worker le calcule (``run_if_due``), l'API le relit, le modifie et le valide. Rien n'est cree ni envoye
ici : un plan ``proposed`` n'est qu'une proposition. Aucun chiffre invente, aucun repli silencieux (ADR-ad2e) : un
reglage hors domaine, un fichier illisible ou un creneau manquant sont une ``RepartitionError`` ou une note du plan.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import unicodedata
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from clipper import accounts, channel, publish, tiktok
from clipper.config import Config, ConfigError, load_config

log = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    "enabled": True,  # faux : le worker ne calcule rien, l'ecran le dit
    "state_dir": "state/repartition",  # un fichier <AAAA-MM-JJ>.json par jour planifie
    "compute_time": "20:00",  # heure de Paris a partir de laquelle le worker calcule le plan du lendemain
    "posts_per_day": 6,  # posts vises par compte et par jour, publications deja prevues comprises
    "default_grid_start": "08:00",  # grille par defaut d'un compte sans creneau fixe ce jour-la
    "default_grid_end": "22:00",  # derniere heure de la grille par defaut (incluse)
    "default_grid_gap_min": 150,  # ecart minimal (minutes) entre deux posts d'un meme compte, grille et complements
    "account_stagger_min": 30,  # decalage (minutes) entre comptes a meme heure de grille, par rang du compte
    "max_per_source": 2,  # clips d'une meme source (jeu connu, sinon VOD) par compte et par jour
    "excluded_sources": [],  # streamers (meta.json channel) ou styles jamais planifies
    "prime_start": "18:00",  # debut des creneaux du soir (les meilleurs clips)
    "prime_end": "22:00",  # fin (incluse) des creneaux du soir
    "exploration_per_day": 1,  # clips d'exploration au plus par jour, tous comptes confondus
    "bonus_window_days": 7,  # fenetre des posts releves qui servent au bonus d'une source
    "bonus_min_age_h": 24,  # age minimal (heures) d'un post releve pour compter : les vues mûrissent
    "bonus_min_posts": 3,  # posts releves d'une source au moins pour qu'elle ait un bonus
    "bonus_points": 5.0,  # amplitude maximale du bonus, en points de score
    "title_ab_enabled": False,  # vrai : test A/B des titres, un style de titre par compte, en rotation hebdomadaire
    "title_ab_start": "2026-10-14",  # premier jour (AAAA-MM-JJ, heure de Paris) de la semaine 1 de la rotation
    "title_ab_accounts": [],  # ids des comptes TikTok du test, dans l'ordre de la rotation (hors liste : inchangés)
    "title_ab_styles": ["citation", "promesse", "promesse_emoji"],  # styles de titre, dans l'ordre de la rotation
}

PARIS = ZoneInfo("Europe/Paris")
STATUSES = ("proposed", "validated", "error")
_ERROR_RETRY = timedelta(minutes=5)  # un plan en erreur n'est retente qu'apres ce delai (le tour du worker est court)
_HANDLED = (publish.PublishError, accounts.AccountsError, tiktok.TikTokError, ConfigError, OSError, ValueError)
_logged_errors: set[tuple[str, str, str]] = set()
_HM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_CLIP_NUMBER = re.compile(r"^(\d+)")
TITLE_STYLES = ("citation", "promesse", "promesse_emoji")  # SPEC-810f ; les memes que [captions] screen_title_style


class RepartitionError(Exception):
    """Reglage hors domaine, fichier d'etat ou de travail illisible, plan deja valide : jamais corrige en silence."""


# ---------------------------------------------------------------- reglages (R0)


def _minutes(settings: dict[str, Any], key: str) -> int:
    value = settings[key]
    found = _HM.match(value) if isinstance(value, str) else None
    if not found:
        raise RepartitionError(f"[repartition] {key} invalide : {value!r} (une heure HH:MM est attendue)")
    return int(found[1]) * 60 + int(found[2])


def _integer(settings: dict[str, Any], key: str, minimum: int) -> int:
    value = settings[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RepartitionError(f"[repartition] {key} invalide : {value!r} (un entier >= {minimum} est attendu)")
    return value


def _settings(config: Config | None) -> dict[str, Any]:
    settings = dict(config.section("repartition")) if config is not None else dict(CONFIG_DEFAULTS)
    if not isinstance(settings["enabled"], bool):
        raise RepartitionError(f"[repartition] enabled invalide : {settings['enabled']!r} (true ou false est attendu)")
    if not isinstance(settings["state_dir"], str) or not settings["state_dir"]:
        raise RepartitionError(f"[repartition] state_dir invalide : {settings['state_dir']!r} (un chemin est attendu)")
    for key in ("compute_time", "default_grid_start", "default_grid_end", "prime_start", "prime_end"):
        _minutes(settings, key)
    for key, minimum in (("posts_per_day", 1), ("default_grid_gap_min", 1), ("max_per_source", 1),
                         ("bonus_window_days", 1), ("bonus_min_age_h", 0), ("bonus_min_posts", 1),
                         ("account_stagger_min", 0), ("exploration_per_day", 0)):
        _integer(settings, key, minimum)
    if _minutes(settings, "prime_end") <= _minutes(settings, "prime_start"):
        raise RepartitionError(
            f"[repartition] prime_end ({settings['prime_end']}) doit etre apres prime_start ({settings['prime_start']})")
    if _minutes(settings, "default_grid_end") < _minutes(settings, "default_grid_start"):
        raise RepartitionError(f"[repartition] default_grid_end ({settings['default_grid_end']}) est avant "
                               f"default_grid_start ({settings['default_grid_start']})")
    points = settings["bonus_points"]
    if isinstance(points, bool) or not isinstance(points, (int, float)) or points < 0:
        raise RepartitionError(f"[repartition] bonus_points invalide : {points!r} (un nombre >= 0 est attendu)")
    sources = settings["excluded_sources"]
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise RepartitionError(f"[repartition] excluded_sources invalide : {sources!r} (une liste de chaines est attendue)")
    _check_title_ab(settings)
    return settings


def _check_title_ab(settings: dict[str, Any]) -> None:
    """Reglages ``title_ab_*`` (SPEC-6d1f, test A/B des titres) : toute valeur hors domaine est une erreur nommee."""
    if not isinstance(settings["title_ab_enabled"], bool):
        raise RepartitionError(
            f"[repartition] title_ab_enabled invalide : {settings['title_ab_enabled']!r} (true ou false est attendu)")
    start = settings["title_ab_start"]
    try:
        if not isinstance(start, str):
            raise ValueError(start)
        date.fromisoformat(start)
    except ValueError:
        raise RepartitionError(f"[repartition] title_ab_start invalide : {start!r} (AAAA-MM-JJ est attendu)") from None
    styles = settings["title_ab_styles"]
    if not isinstance(styles, list) or not styles or not all(isinstance(s, str) for s in styles):
        raise RepartitionError(
            f"[repartition] title_ab_styles invalide : {styles!r} (une liste non vide de styles est attendue)")
    unknown = [s for s in styles if s not in TITLE_STYLES]
    if unknown:
        raise RepartitionError(
            f"[repartition] title_ab_styles : style inconnu {unknown!r} (valeurs permises : {', '.join(TITLE_STYLES)})")
    if len(set(styles)) != len(styles):
        raise RepartitionError(f"[repartition] title_ab_styles invalide : {styles!r} (un style ne figure qu'une fois)")
    accounts_ = settings["title_ab_accounts"]
    if not isinstance(accounts_, list) or not all(isinstance(a, str) and a for a in accounts_):
        raise RepartitionError(
            f"[repartition] title_ab_accounts invalide : {accounts_!r} (une liste d'ids de comptes est attendue)")
    if len(set(accounts_)) != len(accounts_):
        raise RepartitionError(
            f"[repartition] title_ab_accounts invalide : {accounts_!r} (un compte ne figure qu'une fois)")
    if settings["title_ab_enabled"] and not accounts_:
        raise RepartitionError("[repartition] title_ab_accounts est vide alors que title_ab_enabled est vrai")


def title_style_for(account: str, day: date | str, settings: dict[str, Any]) -> str | None:
    """Style de titre du compte ``account`` le jour ``day`` (heure de Paris) dans le test A/B, ``None`` si le test est
    éteint, pas commencé, ou si le compte n'est pas dans ``title_ab_accounts``. Semaine ``w`` = jours écoulés depuis
    ``title_ab_start`` // 7 (0 pour la première) ; style = styles[(rang du compte + w) % len(styles)]."""
    _check_title_ab(settings)
    target = _as_day(day)
    if not settings["title_ab_enabled"] or account not in settings["title_ab_accounts"]:
        return None
    elapsed = (target - date.fromisoformat(settings["title_ab_start"])).days
    if elapsed < 0:
        return None
    styles = settings["title_ab_styles"]
    return styles[(settings["title_ab_accounts"].index(account) + elapsed // 7) % len(styles)]


def clip_title_style(output_dir: str | Path, video_id: str, clip_id: str) -> str | None:
    """Style du titre réellement dessiné sur le clip (sidecar : ``screen_title_style`` et ``title_shown`` vrai),
    ``None`` sinon : un clip sans titre dessiné ou sans style n'est jamais deviné (SPEC-6d1f)."""
    sidecar = publish.read_sidecar(output_dir, video_id, clip_id)
    style = sidecar.get("screen_title_style")
    return style if sidecar.get("title_shown") is True and style in TITLE_STYLES else None


def _ab_refusal(clip_style: str | None, account_style: str | None) -> str | None:
    """Raison pour laquelle un clip ne peut pas partir sur un compte du test, ``None`` s'il le peut (ou si le compte
    n'est pas dans le test)."""
    if account_style is None:
        return None
    if clip_style is None:
        return "no_title_style"
    return None if clip_style == account_style else "style_mismatch"


def _ab_text(reason: str, video_id: str, clip_id: str, account_id: str, clip_style: str | None,
             account_style: str | None) -> str:
    if reason == "no_title_style":
        return (f"{video_id}/{clip_id} : style de titre inconnu (sidecar sans screen_title_style ou titre non "
                f"dessiné), refusé pour le compte {account_id} du test A/B des titres")
    return (f"{video_id}/{clip_id} : titre en « {clip_style} » mais le compte {account_id} est en "
            f"« {account_style} » ce jour-là (test A/B des titres)")


def least_supplied_style(supply: dict[str, int], styles: list[str]) -> str:
    """Style de ``styles`` qui a le moins de clips disponibles (``supply``) ; égalité : le premier de l'ordre de config."""
    return min(styles, key=lambda style: (supply.get(style, 0), styles.index(style)))


def style_supply(config: Config, styles: list[str]) -> dict[str, int]:
    """Clips disponibles (prêts, absents de toute file de publication, ou validés sans créneau) par style de titre
    de ``styles``. Un clip sans style de titre dessiné ne compte pour aucun."""
    workspace, output = Path(config.workspace_dir), Path(config.output_dir)
    counts = {style: 0 for style in styles}
    for unit in publish.available_series_clips(
            None, account=None, together=False, workspace_dir=workspace, output_dir=output,
            state_dir=config.section("publish")["state_dir"]):
        style = clip_title_style(output, unit["video_id"], unit["clip_ids"][0])
        if style in counts:
            counts[style] += 1
    return counts


# ---------------------------------------------------------------- fichiers et dates


def _worker_video_id(item: dict[str, Any]) -> str:
    """Id que le worker donne à un candidat de la veille : une VOD Twitch (id numérique) a le préfixe ``v``."""
    video_id = str(item["video_id"])
    return video_id if item.get("source") != "twitch" or video_id.startswith("v") else f"v{video_id}"


def _as_day(day: date | str) -> date:
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        raise RepartitionError(f"jour invalide : {day!r} (AAAA-MM-JJ est attendu)") from None


def _paris(moment: datetime) -> datetime:
    """Instant en heure de Paris ; une date sans fuseau est deja une heure de Paris (celle de TikTok Studio)."""
    return moment.replace(tzinfo=PARIS) if moment.tzinfo is None else moment.astimezone(PARIS)


def plan_path(day: date | str, config: Config | None = None) -> Path:
    settings = _settings(config)
    return Path(settings["state_dir"]) / f"{_as_day(day).isoformat()}.json"


def read_plan(day: date | str, config: Config | None = None) -> dict[str, Any] | None:
    """Le plan du jour, ``None`` s'il n'existe pas ; un fichier illisible est une ``RepartitionError``."""
    return _read_json(plan_path(day, config), None)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepartitionError(f"fichier illisible : {path} ({exc})") from exc


def normalize(name: str) -> str:
    """Cle de jeu : minuscules, sans accents ni ponctuation, espaces reduits (meme regle que la veille)."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _fmt(number: float) -> str:
    return f"{int(round(number)):,}".replace(",", " ")


# ---------------------------------------------------------------- creneaux (R1)


def is_prime(slot: datetime, settings: dict[str, Any]) -> bool:
    local = _paris(slot)
    minute = local.hour * 60 + local.minute
    return _minutes(settings, "prime_start") <= minute <= _minutes(settings, "prime_end")


def entry_time(entry: dict[str, Any]) -> datetime | None:
    """Instant d'une entree de publication, comme ``publish.planned_times`` : publiee -> sa mise en ligne,
    approuvee ou programmee -> son creneau."""
    if entry["status"] == "published":
        stamp = entry.get("tiktok_publish_at") or entry.get("published_at")
    elif entry["status"] in ("approved", "scheduled"):
        stamp = entry.get("slot_at")
    else:
        return None
    return datetime.fromisoformat(stamp) if stamp else None


def _account_slots(
    account: dict[str, Any], rank: int, day: date, settings: dict[str, Any], planned: list[datetime],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Creneaux du jour d'un compte et notes : creneaux fixes (tous gardes), puis heures de la grille par defaut
    pour atteindre ``posts_per_day`` en comptant les publications deja prevues ce jour-la."""
    schedule = accounts.schedule_of(account)
    zone = ZoneInfo(str(schedule["timezone"]))
    fixed: list[datetime] = []
    if schedule["slots"]:
        before = datetime.combine(day, time(0, 0), zone) - timedelta(seconds=1)
        fixed = [s for s in channel.next_slots(schedule, before, len(schedule["slots"])) if s.date() == day]
    taken = list(planned)
    kept_fixed = [s for s in fixed if s not in planned]
    taken.extend(kept_fixed)
    gap = timedelta(minutes=int(settings["default_grid_gap_min"]))
    wanted = int(settings["posts_per_day"]) - len(planned) - len(kept_fixed)
    grid: list[datetime] = []
    if wanted > 0:
        start = _minutes(settings, "default_grid_start") + int(settings["account_stagger_min"]) * rank
        end = _minutes(settings, "default_grid_end")
        minute = start
        while minute <= end and len(grid) < wanted:
            candidate = datetime.combine(day, time(minute // 60, minute % 60), PARIS)
            if all(abs(candidate - other) >= gap for other in taken):
                grid.append(candidate)
                taken.append(candidate)
            minute += int(settings["default_grid_gap_min"])
    notes: list[str] = []
    possible = len(kept_fixed) + len(grid)
    if possible + len(planned) < int(settings["posts_per_day"]):
        notes.append(f"{account['label']} : {possible} créneau(x) possible(s) sur "
                     f"{int(settings['posts_per_day']) - len(planned)} voulus (écart minimal "
                     f"{settings['default_grid_gap_min']} min, jamais de créneau rapproché)")
    slots = [{"slot_at": s.isoformat(), "kind": "fixed", "prime": is_prime(s, settings)} for s in kept_fixed]
    slots += [{"slot_at": s.isoformat(), "kind": "grid", "prime": is_prime(s, settings)} for s in grid]
    slots.sort(key=lambda s: datetime.fromisoformat(s["slot_at"]))
    return slots, notes


# ---------------------------------------------------------------- sources, vivier, bonus (R2-R4)


class World:
    """Ce que le calcul lit : files, sidecars, meta.json, instantane de la veille. Lu une fois par calcul."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.workspace = Path(config.workspace_dir)
        self.output = Path(config.output_dir)
        self.publish_dir = config.section("publish")["state_dir"]
        self.presets_dir = config.section("watch")["presets_dir"]
        self.base_config = config.section("watch")["base_config"]
        self._meta: dict[str, dict[str, Any]] = {}
        self._games: dict[str, str] | None = None
        self._moments: dict[str, dict[int, bool] | None] = {}
        self.notes: list[str] = []  # fichiers de jour de la veille ignorés, rapportés dans le plan

    def meta(self, video_id: str) -> dict[str, Any]:
        if video_id not in self._meta:
            data = _read_json(self.workspace / video_id / "meta.json", {})
            if not isinstance(data, dict):
                raise RepartitionError(f"meta.json invalide : {self.workspace / video_id / 'meta.json'}")
            self._meta[video_id] = data
        return self._meta[video_id]

    def style(self, video_id: str) -> str | None:
        """Style (chaîne) de la vidéo : ``channel`` de son ``pipeline.json``, ``None`` si absent."""
        state = _read_json(self.workspace / video_id / "pipeline.json", {})
        channel = state.get("channel") if isinstance(state, dict) else None
        return channel if isinstance(channel, str) else None

    def veille_games(self) -> dict[str, str]:
        """``video_id`` (préfixe ``v`` comme les dossiers du workspace) -> jeu : ``seen.json`` queued d'abord, puis les
        candidats de chaque ``days/<jour>.json``, le plus récent gagnant. Un fichier illisible est noté, jamais fatal."""
        if self._games is None:
            veille = Path(str(self.config.section("veille")["state_dir"]))
            seen = _read_json(veille / "seen.json", {})
            queued = seen.get("queued", []) if isinstance(seen, dict) else []
            self._games = {}
            for item in queued if isinstance(queued, list) else []:
                if isinstance(item, dict) and item.get("video_id") and item.get("game_name"):
                    self._games.setdefault(item["video_id"], item["game_name"])
            days = veille / "days"
            for path in sorted(days.glob("*.json"), reverse=True) if days.is_dir() else []:
                try:
                    data = _read_json(path, {})
                except RepartitionError as exc:
                    self.notes.append(f"fichier de jour de la veille ignoré : {exc}")
                    continue
                candidates = data.get("candidates", []) if isinstance(data, dict) else []
                for item in candidates if isinstance(candidates, list) else []:
                    if isinstance(item, dict) and item.get("video_id") and item.get("game_name"):
                        self._games.setdefault(_worker_video_id(item), item["game_name"])
        return self._games

    def source(self, video_id: str) -> dict[str, Any]:
        """``source_key``, ``game_name``, ``source_from`` : meta.json, puis veille, sinon la VOD (jamais le titre)."""
        for origin, name in (("meta", self.meta(video_id).get("game")), ("veille", self.veille_games().get(video_id))):
            if isinstance(name, str) and normalize(name):
                return {"source_key": f"jeu:{normalize(name)}", "game_name": name, "source_from": origin}
        return {"source_key": f"vod:{video_id}", "game_name": None, "source_from": "vod"}

    def exploration(self, video_id: str, clip_id: str, notes: list[str]) -> bool:
        if video_id not in self._moments:
            path = self.workspace / video_id / "moments.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._moments[video_id] = {m["id"]: m.get("exploration") is True for m in data["moments"]}
            except (OSError, ValueError, KeyError, TypeError):
                self._moments[video_id] = None
                notes.append(f"moments.json absent ou illisible pour {video_id} : clip traité comme non exploration")
        moments = self._moments[video_id]
        number = _CLIP_NUMBER.match(clip_id)
        return bool(moments and number and moments.get(int(number[1])))

    def title_cap(self) -> int:
        """Plafond ``[captions] screen_title_max_chars`` (0 = aucun filtre), lu dans la config sans importer
        ``clipper.captions`` (ADR-b16b). Une valeur hors domaine est une ``RepartitionError``, jamais ignorée."""
        value = self.config.section("captions").get("screen_title_max_chars", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RepartitionError(f"[captions] screen_title_max_chars doit être un entier >= 0, reçu {value!r}")
        return value

    def title_length(self, video_id: str, clip_id: str) -> int | None:
        """Longueur du ``screen_title`` du sidecar, ``None`` sans titre texte."""
        title = publish.read_sidecar(self.output, video_id, clip_id).get("screen_title")
        return len(title) if isinstance(title, str) else None

    def queued_videos(self) -> set[str]:
        queue = _read_json(Path(str(self.config.section("worker")["queue_path"])), [])
        if not isinstance(queue, list):
            raise RepartitionError("file de traitement invalide (une liste est attendue)")
        return {e["video_id"] for e in queue
                if isinstance(e, dict) and e.get("status") in ("waiting", "running") and e.get("video_id")}


def source_stats(world: World, active: list[dict[str, Any]], now: datetime, settings: dict[str, Any]) -> tuple[
        dict[str, list[float]], list[float]]:
    """Vues reelles des posts releves dans la fenetre, relies a un clip Clipper (``tiktok_post.id``), tous comptes
    confondus : par ``source_key`` et au total. Rien n'est estime (R4)."""
    linked: dict[str, str] = {}
    for path in sorted(world.output.glob("*/*.json")) if world.output.is_dir() else []:
        sidecar = _read_json(path, {})
        post = sidecar.get("tiktok_post") if isinstance(sidecar, dict) else None
        if isinstance(post, dict) and post.get("id"):
            linked[str(post["id"])] = path.parent.name
    since = now - timedelta(days=int(settings["bonus_window_days"]))
    mature = now - timedelta(hours=int(settings["bonus_min_age_h"]))  # un post plus recent n'a pas encore ses vues finales
    by_source: dict[str, list[float]] = {}
    every: list[float] = []
    seen: set[str] = set()
    for acc in active:
        history = tiktok.read_history(acc["id"], config=world.config)
        for post_id, post in sorted(tiktok.merged_posts(history).items()):
            views, stamp = post.get("views"), post.get("posted_at")
            if post_id in seen or post_id not in linked or isinstance(views, bool) or not isinstance(views, (int, float)):
                continue
            try:
                posted = _paris(datetime.fromisoformat(stamp)) if isinstance(stamp, str) else None
            except ValueError:
                posted = None
            if posted is None or not since <= posted <= mature:
                continue
            seen.add(post_id)
            by_source.setdefault(world.source(linked[post_id])["source_key"], []).append(views)
            every.append(views)
    return by_source, every


def source_bonus(
    source_key: str, by_source: dict[str, list[float]], every: list[float], settings: dict[str, Any],
) -> tuple[float, str]:
    """Bonus (points) d'une source d'après la médiane de ses posts relevés contre la médiane de tous, et sa raison."""
    mine = by_source.get(source_key, [])
    if not mine:
        return 0, "no_stats"
    if len(mine) < int(settings["bonus_min_posts"]):
        return 0, "below_min_posts"
    median, reference = statistics.median(mine), statistics.median(every)
    ratio = max(-1.0, min(1.0, median / reference - 1)) if reference > 0 else 0.0
    return (round(float(settings["bonus_points"]) * ratio, 2),
            f"{len(mine)} posts, médiane {_fmt(median)} vues, référence {_fmt(reference)}, "
            f"posts d'au moins {int(settings['bonus_min_age_h'])} h")


_EXCLUSION_TEXT = {
    "multi_part_series": "partie d'une série en plusieurs parties, jamais planifiée seule",
    "excluded_source": "source exclue par excluded_sources",
    "in_processing_queue": "vidéo encore dans la file de traitement",
}


def clip_exclusion(world: World, video_id: str, clip_id: str, excluded_sources: list[str],
                   queued: set[str]) -> str | None:
    """Raison pour laquelle un clip ne peut pas être choisi (R2), ou ``None`` : partie d'une série en plusieurs parties
    (``multi_part_series``), source exclue (``excluded_source``, streamer ou style), vidéo dans la file de traitement
    (``in_processing_queue``). Source unique de ces règles : le vivier, le compte et le PUT du plan s'y réfèrent."""
    if _multi_part(world, video_id, clip_id):
        return "multi_part_series"
    excluded = {s.casefold() for s in excluded_sources}
    own = {n.casefold() for n in (world.meta(video_id).get("channel"), world.style(video_id)) if isinstance(n, str)}
    if own & excluded:
        return "excluded_source"
    return "in_processing_queue" if video_id in queued else None


def _pool(world: World, active: list[dict[str, Any]], settings: dict[str, Any], notes: list[str],
          day: date, ab_refused: list[dict[str, str]],
          title_refused: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Clips choisissables (R2) avec les comptes qui peuvent les prendre, et les exclusions comptees par raison.
    Test A/B des titres : un compte du test ne garde que les clips dont le style est le sien ce jour-là ; chaque refus
    est ajouté à ``ab_refused`` (et, pour un clip sans style, dit dans ``notes``).
    Un titre d'écran plus long que ``[captions] screen_title_max_chars`` (si > 0) écarte le clip : refus dans
    ``title_refused`` et dans ``notes``."""
    cap = world.title_cap()
    clips: dict[tuple[str, str], dict[str, Any]] = {}
    for acc in active:
        units = publish.available_series_clips(
            None, account=acc["id"], together=False, workspace_dir=world.workspace, output_dir=world.output,
            state_dir=world.publish_dir)
        for unit in units:
            for clip_id in unit["clip_ids"]:
                found = clips.setdefault((unit["video_id"], clip_id), {
                    "video_id": unit["video_id"], "clip_id": clip_id, "style": unit["channel"],
                    "score": unit["score"], "accounts": set()})
                found["accounts"].add(acc["id"])
    queued = world.queued_videos()
    names = {n.casefold() for c in clips.values() for n in (world.meta(c["video_id"]).get("channel"), c["style"])
             if isinstance(n, str)}
    for source in settings["excluded_sources"]:
        if source.casefold() not in names:
            notes.append(f"{source} : aucune vidéo pour cette source")
    pool: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for key in sorted(clips):
        clip = clips[key]
        reason = clip_exclusion(world, key[0], key[1], settings["excluded_sources"], queued)
        if reason:
            excluded.append({"video_id": key[0], "clip_id": key[1], "reason": reason})
            continue
        length = world.title_length(key[0], key[1]) if cap else None
        if length is not None and length > cap:
            title_refused.append({"video_id": key[0], "clip_id": key[1], "length": length, "max_chars": cap})
            notes.append(f"{key[0]}/{key[1]} : titre d'écran de {length} caractères, plafond {cap} "
                         "([captions] screen_title_max_chars) : clip écarté")
            continue
        clip["title_style"] = clip_title_style(world.output, key[0], key[1]) if settings["title_ab_enabled"] else None
        for account_id in sorted(clip["accounts"]):
            refusal = _ab_refusal(clip["title_style"], title_style_for(account_id, day, settings))
            if refusal:
                clip["accounts"].discard(account_id)
                ab_refused.append({"video_id": key[0], "clip_id": key[1], "account": account_id, "reason": refusal})
        if clip["title_style"] is None and any(r["reason"] == "no_title_style" and (r["video_id"], r["clip_id"]) == key
                                               for r in ab_refused):
            notes.append(f"{key[0]}/{key[1]} : style de titre inconnu (sidecar sans screen_title_style ou titre non "
                         "dessiné), refusé pour les comptes du test A/B des titres")
        pool.append(clip)
    return pool, excluded


# ---------------------------------------------------------------- calcul (R1-R7)


def _take(state: dict[str, Any], slot: dict[str, Any], clip: dict[str, Any]) -> None:
    """Pose le clip sur le créneau du compte : la ligne du plan, le créneau consommé, le compte de la source."""
    state["free"].remove(slot)
    state["counts"][clip["source_key"]] = state["counts"].get(clip["source_key"], 0) + 1
    state["lines"].append({
        "slot_at": slot["slot_at"], "video_id": clip["video_id"], "clip_id": clip["clip_id"],
        "score": clip["score"], "bonus": clip["bonus"], "bonus_reason": clip["bonus_reason"],
        "adjusted": clip["adjusted"], "source_key": clip["source_key"], "game_name": clip["game_name"],
        "source_from": clip["source_from"], "exploration": clip["exploration"], "prime": slot["prime"],
        **({"title_style": clip["title_style"]} if state["ab"] else {})})


def _reserve_exploration(states: list[dict[str, Any]], remaining: list[dict[str, Any]], settings: dict[str, Any],
                         notes: list[str]) -> None:
    """R6 : avant le remplissage par score, réserve min(exploration_per_day, clips d'exploration disponibles)
    créneaux, au plus un par compte et par jour, tourniquet des comptes, sur le premier créneau hors soir de
    chaque compte, avec le meilleur clip d'exploration qu'il peut prendre. Sans aucun créneau hors soir, rien
    n'est réservé et une note le dit."""
    wanted = min(int(settings["exploration_per_day"]), sum(1 for c in remaining if c["exploration"]))
    if wanted == 0:
        return
    if not any(not s["prime"] for state in states for s in state["free"]):
        notes.append("exploration : aucun créneau hors soir, aucun clip d'exploration réservé")
        return
    reserved = 0
    for state in states:
        if reserved == wanted:
            break
        off_peak = next((s for s in state["free"] if not s["prime"]), None)
        clip = next((c for c in remaining if c["exploration"] and state["account"]["id"] in c["accounts"]
                     and state["counts"].get(c["source_key"], 0) < int(settings["max_per_source"])), None)
        if off_peak is None or clip is None:
            continue
        _take(state, off_peak, clip)
        remaining.remove(clip)
        reserved += 1
        at = _paris(datetime.fromisoformat(off_peak["slot_at"])).strftime("%H:%M")
        notes.append(f"exploration : {clip['video_id']}/{clip['clip_id']} sur {state['account']['label']} à {at}")


def _build(day: date, now: datetime, settings: dict[str, Any], config: Config, computed_by: str) -> dict[str, Any]:
    world = World(config)
    notes: list[str] = []
    active: list[dict[str, Any]] = []
    for account in accounts.list_accounts(config):
        ready = account["ready_to_publish"] and not account.get("paused_at")
        if account["service"] == "tiktok" and ready:
            active.append(account)
        elif account["service"] != "tiktok" and ready:
            notes.append(f"{account['label']} : compte {accounts.SERVICE_LABELS.get(account['service'], account['service'])}"
                         " prêt, hors périmètre v1 (non planifié)")

    ab_styles = {a["id"]: style for a in active if (style := title_style_for(a["id"], day, settings))}
    ab_refused: list[dict[str, str]] = []
    title_refused: list[dict[str, Any]] = []
    pool, excluded = _pool(world, active, settings, notes, day, ab_refused, title_refused)
    by_source, every = source_stats(world, active, now, settings)
    if not every:
        notes.append("aucun relevé récent")
    candidates: list[dict[str, Any]] = []
    for clip in pool:
        source = world.source(clip["video_id"])
        bonus, reason = source_bonus(source["source_key"], by_source, every, settings)
        score = clip["score"] if isinstance(clip["score"], (int, float)) and not isinstance(clip["score"], bool) else None
        candidates.append({
            **clip, **source, "score": score, "bonus": bonus, "bonus_reason": reason,
            "adjusted": None if score is None else round(score + bonus, 2),
            "exploration": world.exploration(clip["video_id"], clip["clip_id"], notes)})
    candidates.sort(key=lambda c: (c["adjusted"] is None, -(c["adjusted"] or 0), c["video_id"], c["clip_id"]))

    states: list[dict[str, Any]] = []
    for rank, account in enumerate(active):
        planned_entries = [(n, e) for n, e in publish._account_entries(account["id"], world.publish_dir,
                                                                      world.presets_dir, world.base_config)
                           if (t := entry_time(e)) is not None and _paris(t).date() == day]
        planned = publish.planned_times(account["id"], state_dir=world.publish_dir, presets_dir=world.presets_dir)
        planned = [t for t in planned if _paris(t).date() == day]
        slots, slot_notes = _account_slots(account, rank, day, settings, planned)
        counts: dict[str, int] = {}
        for _name, entry in planned_entries:
            key = world.source(entry["video_id"])["source_key"]
            counts[key] = counts.get(key, 0) + 1
        order = [s for s in slots if s["prime"]] + [s for s in slots if not s["prime"]]
        states.append({"account": account, "slots": slots, "free": order, "counts": counts, "lines": [],
                       "notes": slot_notes, "ab": bool(ab_styles)})

    remaining = list(candidates)
    _reserve_exploration(states, remaining, settings, notes)
    progressed = True
    while progressed:
        progressed = False
        for state in states:
            if not state["free"]:
                continue
            for clip in remaining:
                if state["account"]["id"] not in clip["accounts"] or clip["exploration"]:
                    continue
                if state["counts"].get(clip["source_key"], 0) >= int(settings["max_per_source"]):
                    continue
                _take(state, state["free"][0], clip)
                remaining.remove(clip)
                progressed = True
                break

    plan_accounts = []
    for state in states:
        lines = sorted(state["lines"], key=lambda line: datetime.fromisoformat(line["slot_at"]))
        notes_here = list(state["notes"])
        if state["free"]:
            notes_here.append(f"{len(state['free'])} créneau(x) sans clip : vivier insuffisant")
        plan_accounts.append({"account": state["account"]["id"], "label": state["account"]["label"],
                              "slots": state["slots"], "lines": lines, "notes": notes_here,
                              **({"title_style": ab_styles.get(state["account"]["id"])} if ab_styles else {})})
    notes.extend(world.notes)
    plan = {"day": day.isoformat(), "computed_at": now.isoformat(), "computed_by": computed_by,
            "status": "proposed", "accounts": plan_accounts, "pool": len(pool), "excluded": excluded,
            "notes": notes, "validated_at": None, "created": [], "title_too_long": title_refused}
    if ab_styles:
        plan["title_ab"] = {"start": settings["title_ab_start"], "styles": ab_styles, "refused": ab_refused}
    return plan


def _compute_locked(day: date, now: datetime, settings: dict[str, Any], config: Config, computed_by: str,
                    path: Path) -> dict[str, Any]:
    existing = _read_json(path, None)
    if isinstance(existing, dict) and existing.get("status") == "validated":
        raise RepartitionError(f"le plan du {day.isoformat()} est déjà validé : il n'est plus recalculé")
    plan = _build(day, now, settings, config, computed_by)
    channel.atomic_write_json(path, plan)
    return plan


def compute_plan(day: date | str, now: datetime, *, config: Config | None = None,
                 computed_by: str = "worker") -> dict[str, Any]:
    """Calcule le plan de ``day`` (heure de Paris), le remplace dans ``<state_dir>/<jour>.json`` sous verrou et le
    rend. Un plan ``validated`` n'est jamais recalcule (``RepartitionError``). Deterministe hors ``computed_at``."""
    config = config or load_config()
    settings = _settings(config)
    if computed_by not in ("worker", "web"):
        raise RepartitionError(f"computed_by invalide : {computed_by!r} (worker ou web est attendu)")
    target = _as_day(day)
    path = Path(settings["state_dir"]) / f"{target.isoformat()}.json"
    with channel.file_lock(path):
        return _compute_locked(target, now, settings, config, computed_by, path)


def _record_error(path: Path | None, day: date, now: datetime, exc: Exception) -> None:
    """Ecrit l'erreur dans le fichier du jour (l'ecran la montre) et la journalise une seule fois."""
    message = str(exc) or type(exc).__name__
    key = (day.isoformat(), type(exc).__name__, message)
    if key not in _logged_errors:
        _logged_errors.add(key)
        log.warning("répartition du %s : plan non calculé (%s : %s)", day.isoformat(), type(exc).__name__, message)
    if path is None:
        return
    try:
        with channel.file_lock(path):
            current = _read_json(path, None)
            if isinstance(current, dict) and current.get("status") in ("proposed", "validated"):
                return
            channel.atomic_write_json(path, {
                "day": day.isoformat(), "computed_at": now.isoformat(), "computed_by": "worker", "status": "error",
                "error": {"type": type(exc).__name__, "message": message}})
    except (RepartitionError, OSError, ValueError) as write_exc:
        log.warning("répartition du %s : erreur non écrite (%s)", day.isoformat(), write_exc)


def run_if_due(now: datetime, *, config: Config | None = None) -> dict[str, Any] | None:
    """Appelee a chaque tour du worker : calcule le plan de demain (Paris) quand ``compute_time`` est passe et qu'aucun
    plan ``proposed`` ou ``validated`` n'existe. Rend le plan calcule, ``None`` sinon. Une erreur est ecrite dans le
    fichier du jour et journalisee une fois, jamais propagee ; un plan ``proposed`` n'est jamais recalcule seul."""
    config = config or load_config()
    raw = config.section("repartition")
    if raw.get("enabled") is False:
        return None
    local = _paris(now)
    day = local.date() + timedelta(days=1)
    path = Path(str(raw["state_dir"])) / f"{day.isoformat()}.json" if isinstance(raw.get("state_dir"), str) else None
    try:
        settings = _settings(config)
        if local.hour * 60 + local.minute < _minutes(settings, "compute_time"):
            return None
        with channel.file_lock(path):
            existing = _read_json(path, None)
            if isinstance(existing, dict):
                if existing.get("status") in ("proposed", "validated"):
                    return None
                stamp = existing.get("computed_at")
                if existing.get("status") == "error" and isinstance(stamp, str) and now - datetime.fromisoformat(
                        stamp) < _ERROR_RETRY:
                    return None
            return _compute_locked(day, now, settings, config, "worker", path)
    except (RepartitionError, *_HANDLED) as exc:
        _record_error(path, day, now, exc)
        return None


# ---------------------------------------------------------------- API publique (écran et routes web)
#
# Ce que les routes ``/api/repartition*`` de clipper.web appellent, seules fonctions de ce module qu'il importe.


def read_settings(config: Config | None) -> dict[str, Any]:
    """Réglages ``[repartition]`` validés (R0) ; ``config`` à ``None`` donne les valeurs par défaut.
    Une valeur hors domaine est une ``RepartitionError``, jamais corrigée."""
    return _settings(config)


def describe_line(
    world: World, settings: dict[str, Any], stats: tuple[dict[str, list[float]], list[float]], *,
    video_id: str, clip_id: str, slot_at: datetime, score: object,
) -> dict[str, Any]:
    """Une ligne redécrite depuis le clip : score, bonus, source, exploration, créneau du soir (R2-R6). Le score
    n'est repris que s'il est un nombre ; ``warning`` reste vide, ``line_warnings`` le remplit."""
    slot = _paris(slot_at)
    source = world.source(video_id)
    bonus, reason = source_bonus(source["source_key"], *stats, settings)
    number = score if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    return {"slot_at": slot.isoformat(), "video_id": video_id, "clip_id": clip_id, "score": number,
            "bonus": bonus, "bonus_reason": reason,
            "adjusted": None if number is None else round(number + bonus, 2),
            **source, "exploration": world.exploration(video_id, clip_id, []),
            "prime": is_prime(slot, settings), "warning": None,
            **({"title_style": clip_title_style(world.output, video_id, clip_id)} if settings["title_ab_enabled"] else {})}


def _multi_part(world: World, video_id: str, clip_id: str) -> bool:
    return (publish.read_sidecar(world.output, video_id, clip_id).get("parts_total") or 1) > 1  # un clip seul est « 1 sur 1 »


def account_pool(account_id: str, *, world: World, settings: dict[str, Any],
                 day: date | str | None = None) -> list[dict[str, Any]]:
    """Vivier de l'écran pour un compte (R2) : les clips que ``account_id`` peut prendre (jamais un clip validé pour un
    autre compte), chaque partie seule, sans ce que le plan exclut (``clip_exclusion``). Test A/B des titres actif :
    ``day`` est requis et un compte du test ne voit que les clips de son style ce jour-là."""
    if settings["title_ab_enabled"] and day is None:
        raise RepartitionError("account_pool : le jour du plan est requis quand title_ab_enabled est vrai")
    account_style = title_style_for(account_id, day, settings) if day is not None else None
    units = publish.available_series_clips(
        None, account=account_id, together=False, workspace_dir=world.workspace, output_dir=world.output,
        state_dir=world.publish_dir)
    queued = world.queued_videos()
    return [u for u in units
            if clip_exclusion(world, u["video_id"], u["clip_ids"][0], settings["excluded_sources"], queued) is None
            and _ab_refusal(clip_title_style(world.output, u["video_id"], u["clip_ids"][0]), account_style) is None]


def line_error(
    world: World, account_id: str, day: date | str, *, video_id: str, clip_id: str, slot_at: datetime,
    entries: list[tuple[str, dict[str, Any]]],
) -> str | None:
    """Raison pour laquelle une ligne n'a pas sa place dans le plan de ``account_id`` ce ``day``, ou ``None`` : créneau
    sur un autre jour (heure de Paris), clip exclu par R2 (``clip_exclusion``), clip déjà validé pour un autre compte.
    Une erreur explicite, jamais une ligne corrigée en silence (ADR-ad2e)."""
    target = _as_day(day)
    if _paris(slot_at).date() != target:
        return f"créneau du {_paris(slot_at).date().isoformat()} hors du jour du plan ({target.isoformat()})"
    reason = clip_exclusion(world, video_id, clip_id, read_settings(world.config)["excluded_sources"],
                            world.queued_videos())
    if reason:
        return f"{video_id}/{clip_id} : {reason} ({_EXCLUSION_TEXT[reason]})"
    account_style = title_style_for(account_id, target, read_settings(world.config))
    clip_style = clip_title_style(world.output, video_id, clip_id)
    refusal = _ab_refusal(clip_style, account_style)
    if refusal:
        return _ab_text(refusal, video_id, clip_id, account_id, clip_style, account_style)
    for _channel, entry in entries:
        owner = entry.get("account")
        if ((entry["video_id"], entry["clip_id"]) == (video_id, clip_id) and entry["status"] == "approved"
                and owner and owner != account_id):
            return f"{video_id}/{clip_id} est validé pour le compte {owner}, pas pour {account_id}"
    return None


def line_warnings(plan: dict[str, Any], day: date | str, settings: dict[str, Any], world: World,
                  entries: list[tuple[str, dict[str, Any]]]) -> None:
    """``warning`` de chaque ligne du plan, rempli en place (c'est le choix de l'utilisateur, jamais un refus) : plus
    de ``max_per_source`` clips d'une même source par compte et par jour, publications déjà prévues comprises (R3) ;
    plus de ``exploration_per_day`` clips d'exploration par jour, ou un clip d'exploration le soir (R6).
    ``entries`` : les entrées de publication de tous les canaux, ``(canal, entrée)`` comme ``publish.all_entries``."""
    target = _as_day(day)
    counts: dict[tuple[str, str], int] = {}
    for _channel, entry in entries:
        when = entry_time(entry)
        if when is not None and _paris(when).date() == target and entry.get("account"):
            key = (entry["account"], world.source(entry["video_id"])["source_key"])
            counts[key] = counts.get(key, 0) + 1
    created = {(made["account"], made["video_id"], made["clip_id"]) for made in plan.get("created") or []}
    cap = world.title_cap()
    every = sorted(((line, account["account"]) for account in plan["accounts"] for line in account["lines"]),
                   key=lambda found: datetime.fromisoformat(found[0]["slot_at"]))
    explorations = 0
    for line, account_id in every:
        notes = []
        key = (account_id, line["source_key"])
        if (account_id, line["video_id"], line["clip_id"]) in created:
            # déjà créée : ``entries`` la compte, la compter ici aussi la doublerait ; plus rien à avertir
            explorations += bool(line["exploration"])
            line["warning"] = None
            continue
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > int(settings["max_per_source"]):
            notes.append(f"plus de {settings['max_per_source']} clips de la même source ({line['source_key']}) "
                         "ce jour-là sur ce compte")
        if line["exploration"]:
            explorations += 1
            if explorations > int(settings["exploration_per_day"]):
                notes.append(f"plus de {settings['exploration_per_day']} clip(s) d'exploration par jour")
            if line["prime"]:
                notes.append("clip d'exploration sur un créneau du soir")
        length = world.title_length(line["video_id"], line["clip_id"]) if cap else None
        if length is not None and length > cap:
            notes.append(f"titre d'écran de {length} caractères, plafond {cap} ([captions] screen_title_max_chars)")
        line["warning"] = "; ".join(notes) or None
