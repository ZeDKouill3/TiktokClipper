"""Publication TikTok par pilotage d'un vrai navigateur (ADR-1a58, SPEC-9225 R3-R6).

Seul module qui parle a TikTok : ``publish`` et ``fetch_stats`` passent par un
backend choisi par ``[tiktok] backend`` (``browser`` : Playwright sur le vrai
Chrome visible, profil persistant du compte via ``clipper.browser`` ; ``api`` :
erreur explicite tant que l'app developpeur n'existe pas).

Arret sur (R4) : captcha, verification, connexion expiree, element attendu absent
apres delai, page inattendue -> ``TikTokStop`` (code + raison en francais + capture
d'ecran sous ``state/browser/<compte>/captures/``). Jamais de resolution de captcha,
jamais de clic de repli, jamais d'identifiant saisi : seuls la legende, la date et
l'heure de programmation sont remplis.

Fenetres surgissantes : celles du ``[popups]`` du toml (texte -> bouton) sont fermees et
journalisees ; toute autre fenetre modale est un arret R4. Avant le clic final, la
verification de contenu de TikTok doit conclure « Aucun probleme constate » (probleme ou
delai ``content_check_timeout_s`` = arret R4, code ``content_check``).

Tous les selecteurs et adresses vivent dans ``clipper/assets/tiktok_selectors.toml``
(R5) ; aucun n'est ecrit ici. Rythme (R6) : delais aleatoires bornes entre actions,
plafonds par compte (``check_limits``). Pas d'etape : bibliotheque (ADR-b16b), la
file et le worker l'appellent.
"""

from __future__ import annotations

import functools
import json
import logging
import random
import re
import time
import tomllib
from contextlib import AbstractContextManager
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from clipper import browser
from clipper import channel as channel_mod
from clipper.config import Config

logger = logging.getLogger(__name__)

# Defauts d'un compte neuf (docs/tiktok-cadence.md 3.1) ; ceux d'un compte etabli
# (3 posts par jour, 240 min, delais 2-8 s) sont donnes dans le README.
CONFIG_DEFAULTS: dict[str, object] = {
    "backend": "browser",              # browser | api (pas encore disponible)
    "publish_mode": "immediate",       # immediate | scheduled (programme cote TikTok)
    "visibility": "public",            # public | friends | private (test reel : private)
    "allow_comments": True,            # case « Commentaire » (etat par defaut de TikTok : cochee)
    "allow_reuse": True,               # case « Reutilisation du contenu » (duo, collage ; cochee par defaut)
    "ai_generated": False,             # interrupteur « Contenu genere par IA » (coupe par defaut)
    "max_posts_per_day": 1,
    "min_gap_minutes": 480,
    "min_action_delay_s": 0.3,
    "max_action_delay_s": 1,
    "schedule_max_days": 10,           # limite native de TikTok Studio
    "schedule_min_minutes": 15,        # avance minimale native de TikTok Studio
    "action_timeout_s": 30,            # attente d'un element de la page
    "click_timeout_s": 10,             # attente d'un clic (Playwright : 30 s) ; intercepte par une fenetre : elle est fermee, puis un seul nouvel essai
    "upload_timeout_s": 300,           # attente de la fin de l'envoi du mp4
    "publish_confirm_timeout_s": 60,   # attente de la preuve de publication apres « Publier »
    "content_check": "off",            # off : coupe la verification de contenu de TikTok avant de publier
                                       # (rapide, comme a la main) ; wait : attend son resultat (~10 min)
    "content_check_timeout_s": 900,    # attente du resultat de la verification de contenu (~10 min)
    "content_check_retrigger_s": 120,  # « Verification en cours » affiche plus de N s sans resultat : decoche/recoche l'interrupteur
    "content_check_retriggers": 3,     # relances au plus (decoche/recoche ou « Reessayer ») ; toujours dans content_check_timeout_s
    "poll_interval_s": 5,              # pas d'attente entre deux lectures de la verification
    "type_delay_ms": 50,               # delai entre deux touches de la legende
    "events_path": "state/tiktok/events.json",
    "stats_interval_h": 0,             # releve periodique du worker, en heures : 0 = coupe (releve seulement a l'usage, SPEC-47e2 R4)
    "stats_stale_min": 60,             # ecran Statistiques : releve a l'ouverture si le dernier a plus de N minutes (0 = jamais)
    "stats_dir": "state/stats/tiktok",  # historique par compte : <stats_dir>/<compte>/<horodatage>.json (SPEC-86fe R2)
    "stats_detail_days": 7,            # un post publie depuis moins de N jours est relu en detail a chaque releve
    "stats_detail_max": 30,            # detail (3 pages chacun) seulement pour les N posts les plus recents de la liste ; les autres : chiffres de la liste. « Releve complet » : sans plafond
    "stats_audience_min_views": 100,   # Spectateurs / Engagement : TikTok ne les remplit qu'a partir de 100 vues
    "stats_scroll_rounds": 100,        # limite de securite : pas de defilement de la liste des Publications (~5 posts chacun) ; atteinte = arret journalise
    "stats_empty_wait_s": 8,           # attente des lignes (ou de l'etat vide) de la page Publications avant de conclure « aucun post »
    "post_lookup_interval_s": 10,      # apres une publication prouvee, le post n'est pas encore liste : intervalle entre deux relectures de la page Publications
    "post_lookup_timeout_s": 60,       # delai total de ces relectures avant de conclure « a verifier » (0 = une seule lecture)
}

MODES = ("immediate", "scheduled")
VISIBILITIES = ("public", "friends", "private")
POST_OPTIONS = ("visibility", "allow_comments", "allow_reuse", "ai_generated", "content_check")
BACKENDS = ("browser", "api")
MAX_EVENTS = 50
SELECTORS_PATH = Path(__file__).parent / "assets" / "tiktok_selectors.toml"
REQUIRED_SELECTORS = (
    "file_input", "upload_done", "caption_editor", "advanced_settings", "visibility_dropdown",
    "visibility_public", "visibility_private", "visibility_friends", "comment_switch", "reuse_switch",
    "ai_switch", "schedule_now", "schedule_later", "schedule_inputs", "schedule_fields", "schedule_date_picker",
    "schedule_time_picker", "schedule_timezone", "picker_option", "picker_value",
    "datepicker_month", "datepicker_year", "datepicker_nav", "datepicker_day",
    "calendar_month_title", "calendar_year_title", "calendar_arrow", "calendar_day", "timepicker_hour",
    "timepicker_minute", "schedule_picker_close", "content_check_running", "content_check_ok",
    "content_check_problem", "content_check_error", "content_check_retry",
    "content_check_switch", "post_button", "discard_button", "published_marker",
)
MAX_POPUP_ROUNDS = 5   # fenetres successives fermees par un meme controle avant d'abandonner
MAX_MONTH_STEPS = 24   # fleches du calendrier cliquees au plus avant d'abandonner
REQUIRED_STATS_SELECTORS = ("row", "post_link", "likes", "comments", "metric_card", "traffic_sources", "processing",
                            "views", "visibility", "created", "retention_point", "viewers_card", "engagement_card",
                            "page_text", "scroll_script", "fyf_notice")
METRICS = ("views", "watch_total", "watch_avg", "watched_full", "new_followers", "retention")
TILES = ("views", "profile_views", "likes", "comments", "shares")   # tuiles de la page Donnees analytiques (SPEC-86fe R1)
PERIODS = (7, 28, 60, 365)                                           # periodes relevees, en jours
VIEWERS_SECTIONS = ("types", "age", "gender", "locations")
_DETECT_KINDS = ("captcha", "verification", "login")
_POST_ID = re.compile(r"/video/(\d+)")
_POST_ID_END = re.compile(r"/video/(\d+)/?(?:[?#].*)?$")
_SCROLL_IDLE = 2  # pas de defilement consecutifs sans nouvelle ligne, bas atteint, avant de conclure que la liste est finie


class TikTokError(Exception):
    """Reglage, clip, date ou backend invalide ; fichier de selecteurs ou d'evenements illisible."""


class TikTokStop(TikTokError):
    """Arret sur de la page (SPEC-9225 R4). ``code`` : captcha | verification | login |
    element_missing | unexpected_page | content_check | content_check_limit | content_check_refused | publish_unconfirmed ;
    ``capture`` : la capture d'ecran, ou None."""

    def __init__(self, code: str, reason: str, capture: Path | None = None) -> None:
        super().__init__(reason)
        self.code, self.reason, self.capture = code, reason, capture


# ---------------------------------------------------------------- reglages et selecteurs


def get_settings(config: Config | None) -> dict[str, Any]:
    settings = dict(config.section("tiktok")) if config is not None else dict(CONFIG_DEFAULTS)
    for key, allowed in (("backend", BACKENDS), ("publish_mode", MODES), ("visibility", VISIBILITIES),
                         ("content_check", ("off", "wait"))):
        if settings[key] not in allowed:
            raise TikTokError(f"[tiktok] {key} invalide : {settings[key]!r} (attendu : {' | '.join(allowed)})")
    for key, minimum in (("max_posts_per_day", 1), ("min_gap_minutes", 0), ("min_action_delay_s", 0),
                         ("max_action_delay_s", 0), ("schedule_max_days", 1), ("schedule_min_minutes", 0),
                         ("action_timeout_s", 1), ("click_timeout_s", 1), ("upload_timeout_s", 1), ("publish_confirm_timeout_s", 1),
                         ("content_check_timeout_s", 1), ("content_check_retrigger_s", 1), ("content_check_retriggers", 0),
                         ("poll_interval_s", 1), ("type_delay_ms", 0)):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
            raise TikTokError(f"[tiktok] {key} invalide : {value!r} (un nombre >= {minimum} est attendu)")
    hours = settings["stats_interval_h"]
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours < 0:
        raise TikTokError(f"[tiktok] stats_interval_h invalide : {hours!r} (un nombre d'heures >= 0 est attendu, 0 = coupé)")
    for key in ("allow_comments", "allow_reuse", "ai_generated"):
        if not isinstance(settings[key], bool):
            raise TikTokError(f"[tiktok] {key} invalide : {settings[key]!r} (true ou false attendu)")
    if not isinstance(settings["stats_dir"], str) or not settings["stats_dir"]:
        raise TikTokError(f"[tiktok] stats_dir invalide : {settings['stats_dir']!r} (un chemin est attendu)")
    for key, minimum in (("stats_stale_min", 0), ("stats_detail_days", 0), ("stats_detail_max", 0),
                         ("stats_scroll_rounds", 1), ("stats_empty_wait_s", 0), ("stats_audience_min_views", 0)):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
            raise TikTokError(f"[tiktok] {key} invalide : {value!r} (un nombre >= {minimum} est attendu)")
    for key, minimum in (("post_lookup_interval_s", 1), ("post_lookup_timeout_s", 0)):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
            raise TikTokError(f"[tiktok] {key} invalide : {value!r} (un nombre >= {minimum} est attendu)")
    if settings["min_action_delay_s"] > settings["max_action_delay_s"]:
        raise TikTokError(
            f"[tiktok] min_action_delay_s ({settings['min_action_delay_s']}) dépasse max_action_delay_s "
            f"({settings['max_action_delay_s']})"
        )
    return settings


def post_settings(settings: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]:
    """Reglages d'un post (SPEC-1ed3 R2) : ceux de [tiktok] surchargés par ``options`` (visibilite, commentaires,
    reutilisation, contenu IA, verification de contenu). Une option inconnue ou invalide est une erreur explicite."""
    merged = dict(settings)
    for key, value in (options or {}).items():
        if key not in POST_OPTIONS:
            raise TikTokError(f"réglage de publication inconnu : {key!r} (attendu : {' | '.join(POST_OPTIONS)})")
        if key in ("allow_comments", "allow_reuse", "ai_generated"):
            if not isinstance(value, bool):
                raise TikTokError(f"réglage de publication {key} invalide : {value!r} (true ou false attendu)")
        else:
            allowed = VISIBILITIES if key == "visibility" else ("off", "wait")
            if value not in allowed:
                raise TikTokError(f"réglage de publication {key} invalide : {value!r} (attendu : {' | '.join(allowed)})")
        merged[key] = value
    return merged


def load_selectors(path: str | Path | None = None) -> dict[str, Any]:
    """Selecteurs et adresses de TikTok Studio (R5), valides ou ``TikTokError`` qui nomme la cle."""
    target = Path(path) if path is not None else SELECTORS_PATH
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TikTokError(f"fichier de sélecteurs TikTok introuvable : {target}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TikTokError(f"fichier de sélecteurs TikTok illisible ({target}) : {exc}") from exc

    def need(table: str, key: str, kind: type) -> None:
        value = data.get(table, {}).get(key)
        ok = isinstance(value, kind) and (bool(value) or kind is list)
        if kind is list:
            ok = isinstance(value, list) and all(isinstance(v, str) and v for v in value)
        if not ok:
            raise TikTokError(f"fichier de sélecteurs TikTok ({target.name}) : [{table}] {key} manquant ou invalide")

    need("urls", "upload", str)
    need("expect", "upload_url_prefix", str)
    need("expect", "login_url_markers", list)
    for key in REQUIRED_SELECTORS:
        need("selectors", key, str)
    for key in _DETECT_KINDS:
        need("detect", key, list)
    need("labels", "post_now", str)
    need("labels", "post_scheduled", str)
    need("continue_publish", "dialog", str)
    need("continue_publish", "cancel", str)
    need("expect", "published_url_prefix", str)
    need("modal", "container", str)
    need("modal", "button", str)
    months = data.get("calendar", {}).get("months")
    if not (isinstance(months, list) and len(months) == 12 and all(isinstance(m, str) and m for m in months)):
        raise TikTokError(f"fichier de sélecteurs TikTok ({target.name}) : [calendar] months manquant ou invalide (12 noms de mois attendus)")
    short = data.get("calendar", {}).get("months_short")
    if not (isinstance(short, list) and len(short) == 12 and all(isinstance(m, str) and m for m in short)):
        raise TikTokError(f"fichier de sélecteurs TikTok ({target.name}) : [calendar] months_short manquant ou invalide (12 abréviations de mois attendues)")
    popups = data.get("popups")
    if not (isinstance(popups, dict) and popups and all(
            isinstance(k, str) and k and isinstance(v, str) and v and '"' not in v for k, v in popups.items())):
        raise TikTokError(f"fichier de sélecteurs TikTok ({target.name}) : [popups] manquant ou invalide "
                          f"(texte de la fenêtre -> libellé du bouton, sans guillemet double)")
    need("urls", "stats", str)
    need("urls", "analytics", str)
    need("urls", "analytics_viewers", str)
    need("urls", "analytics_engagement", str)
    need("urls", "analytics_account", str)
    need("expect", "stats_url_prefix", str)
    need("expect", "analytics_url_prefix", str)
    need("expect", "account_analytics_url_prefix", str)
    for key in REQUIRED_STATS_SELECTORS:
        need("stats", key, str)
    need("stats", "unavailable", list)
    if not isinstance(data["stats"].get("empty_state"), str):
        raise TikTokError(f"fichier de sélecteurs TikTok ({target.name}) : [stats] empty_state manquant ou invalide "
                          f"(un sélecteur, ou \"\" tant qu'il n'est pas vérifié en réel)")
    for key in METRICS:
        need("metrics", key, str)
    for key in ("period_button", "period_option", "period_label", "tile"):
        need("account", key, str)
    for key in TILES:
        need("tiles", key, str)
    for key in ("total", *VIEWERS_SECTIONS):
        need("viewers", key, str)
    for key in ("likes_over_time", "comment_words", "shares"):
        need("engagement", key, str)
    return data


# ---------------------------------------------------------------- contenu, limites


def clip_payload(sidecar: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
    """Le mp4 et la legende + hashtags d'un sidecar de clip (SPEC-6a47)."""
    if not isinstance(sidecar.get("caption"), str) or not sidecar["caption"].strip():
        raise TikTokError(f"clip {sidecar.get('video_id')}/{sidecar.get('clip_id')} : caption absente du sidecar")
    hashtags = sidecar.get("hashtags")
    if not isinstance(hashtags, list):
        raise TikTokError(f"clip {sidecar.get('video_id')}/{sidecar.get('clip_id')} : hashtags absents du sidecar")
    path = Path(output_dir) / sidecar["video_id"] / f"{sidecar['clip_id']}.mp4"
    return {"video_path": path, "caption": sidecar["caption"], "hashtags": list(hashtags)}


def check_limits(times: list[datetime], target: datetime, settings: dict[str, Any], tz: tzinfo) -> str | None:
    """R6 : ``None`` si une publication a ``target`` respecte les plafonds du compte
    (``times`` : ses autres publications), sinon la raison, en francais."""
    local = target.astimezone(tz)
    cap = int(settings["max_posts_per_day"])
    same_day = [t for t in times if t.astimezone(tz).date() == local.date()]
    if len(same_day) >= cap:
        return f"plafond de {cap} publication(s) par jour atteint le {local.date().isoformat()}"
    gap = timedelta(minutes=float(settings["min_gap_minutes"]))
    for t in sorted(times):
        if abs(t - target) < gap:
            return (f"écart minimal de {settings['min_gap_minutes']} minutes non respecté avec la "
                    f"publication du {t.astimezone(tz).strftime('%Y-%m-%d %H:%M')}")
    return None


def next_allowed(times: list[datetime], after: datetime, settings: dict[str, Any], tz: tzinfo) -> datetime | None:
    """Premiere heure ``>= after`` qui respecte les plafonds du compte (``check_limits``), ou None si aucune
    n'existe dans les 60 jours : ``after`` lui-meme, la fin de chaque ecart minimal et chaque minuit local."""
    candidates = {after}
    candidates.update(t + timedelta(minutes=float(settings["min_gap_minutes"])) for t in times)
    midnight = datetime.combine(after.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    candidates.update((midnight + timedelta(days=d)).astimezone(timezone.utc) for d in range(1, 61))
    for candidate in sorted(c for c in candidates if c >= after):
        if check_limits(times, candidate, settings, tz) is None:
            return candidate
    return None


# ---------------------------------------------------------------- evenements console


def _events_path(config: Config | None) -> Path:
    return Path(get_settings(config)["events_path"])


def emit_event(event: dict[str, Any], *, config: Config | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Ajoute un evenement au journal lu par la console (ecriture atomique ; la console le
    voit changer par son flux d'evenements) ; seuls les ``MAX_EVENTS`` derniers restent."""
    path = _events_path(config)
    entry = {**event, "at": (now or datetime.now(timezone.utc)).isoformat()}
    with channel_mod.file_lock(path):
        events = read_events(config=config)
        events.append(entry)
        channel_mod.atomic_write_json(path, events[-MAX_EVENTS:])
    return entry


def read_events(since: str | None = None, *, config: Config | None = None) -> list[dict[str, Any]]:
    path = _events_path(config)
    if not path.is_file():
        return []
    try:
        events = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(events, list):
            raise ValueError("pas une liste")
        return [e for e in events if since is None or e["at"] > since]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TikTokError(f"journal des événements TikTok illisible ({path}) : {exc}") from exc


# ---------------------------------------------------------------- lecture des chiffres affiches

_ABSENT = frozenset({"", "-", "--", "–", "—", "N/A", "n/a"})
_SUFFIX = {"": 1, "k": 1_000, "m": 1_000_000, "md": 1_000_000_000, "b": 1_000_000_000}
_COUNT = re.compile(r"(\d[\d ]*)(?:[.,](\d+))?\s*([kKmMbB]|Md)?")
_COUNT_THOUSANDS = re.compile(r"\d{1,3}(?:,\d{3})+")  # « 1,432 », « 12,345,678 » : virgule + exactement 3 chiffres = milliers anglais
_PERCENT_NUMBER = r"\d(?:[\d ]*\d)?(?:[.,]\d+)*"  # « 4 300,0 », « 4,300.0 », « 12,345,678 » (espaces = milliers)
_PERCENT = re.compile(rf"({_PERCENT_NUMBER})\s*%")
_BELOW_ONE = re.compile(r"<\s*\d+(?:[.,]\d+)?\s*%")
_CLOCK = re.compile(r"(?:(\d+):)?(\d+):(\d{2})")
_HMS = re.compile(r"(?:(\d+)\s*h\s*:?\s*)?(?:(\d+)\s*m\s*:?\s*)?(?:(\d+(?:[.,]\d+)?)\s*s)?")  # 0h:00m:00s, 12s
_SECONDS = re.compile(r"(?:(\d+)\s*min\s*)?(?:(\d+(?:[.,]\d+)?)\s*s)?|(\d+)\s*min")


def _squash(text: str) -> str:
    return " ".join(text.replace("\u202f", " ").replace("\xa0", " ").split())


_PARIS = ZoneInfo("Europe/Paris")


def _naive_utc(stamp: Any) -> datetime | None:
    """Date ISO d'une ligne (sans fuseau : l'heure de Paris de la page) comme instant aware, ``None`` si absente."""
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=_PARIS)


def _paris_minute(moment: datetime) -> datetime:
    """Instant en heure de Paris naive, a la minute : la forme des dates affichees par TikTok (sans fuseau)."""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=_PARIS)
    return aware.astimezone(_PARIS).replace(tzinfo=None, second=0, microsecond=0)


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"texte attendu, reçu {value!r}")
    return value.replace("\u202f", " ").replace("\xa0", " ").strip()


def parse_count(value: Any) -> int | None:
    """« 1 200 », « 1,432 », « 1,2 K », « 2.5M » -> entier ; tiret ou vide -> ``None`` ; autre -> ``ValueError``."""
    text = _text(value)
    if text in _ABSENT:
        return None
    if _COUNT_THOUSANDS.fullmatch(text):
        return int(text.replace(",", ""))
    match = _COUNT.fullmatch(text)
    if match is None or (match[2] and not match[3]):  # decimale sans K/M : ambigu, pas de supposition
        raise ValueError(f"nombre illisible : {text!r}")
    number = float(match[1].replace(" ", "") + ("." + match[2] if match[2] else ""))
    return int(round(number * _SUFFIX[(match[3] or "").lower()]))


def _percent_number(raw: str, text: str) -> float:
    """Nombre d'un pourcentage avec separateur de milliers eventuel ; ``ValueError`` si ambigu.

    Espaces entre chiffres = milliers ; « , » et « . » ensemble : les « , » sont des milliers ; « , » seule :
    milliers si au moins deux groupes de exactement 3 chiffres apres le premier, sinon virgule decimale ;
    « . » seul : un seul point decimal."""
    digits = raw.replace(" ", "")
    if "," in digits and "." in digits:
        if not re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", digits):
            raise ValueError(f"pourcentage illisible : {text!r}")
        digits = digits.replace(",", "")
    elif "," in digits:
        head, *groups = digits.split(",")
        if len(groups) >= 2 and all(len(g) == 3 for g in groups):
            digits = head + "".join(groups)
        elif len(groups) == 1:
            digits = head + "." + groups[0]
        else:
            raise ValueError(f"pourcentage illisible : {text!r}")
    if digits.count(".") > 1:
        raise ValueError(f"pourcentage illisible : {text!r}")
    return float(digits)


def parse_percent(value: Any) -> float | None:
    """« 23,4 % » -> 0.234 (fraction) ; tiret ou vide -> ``None`` ; autre -> ``ValueError``."""
    text = _text(value)
    if text in _ABSENT:
        return None
    match = _PERCENT.fullmatch(text)
    if match is None:
        raise ValueError(f"pourcentage illisible : {text!r}")
    return round(_percent_number(match[1], text) / 100, 6)


def parse_duration(value: Any) -> float | None:
    """« 12,5 s », « 0h:01m:05s », « 1:05 », « 1 min 5 s » -> secondes ; tiret ou vide -> ``None`` ; autre -> ``ValueError``."""
    text = _text(value)
    if text in _ABSENT:
        return None
    clock = _CLOCK.fullmatch(text)
    if clock:
        return float(int(clock[1] or 0) * 3600 + int(clock[2]) * 60 + int(clock[3]))
    hms = _HMS.fullmatch(text)
    if hms is not None and any(hms.groups()):
        return float(int(hms[1] or 0) * 3600 + int(hms[2] or 0) * 60 + float((hms[3] or "0").replace(",", ".")))
    match = _SECONDS.fullmatch(text)
    if match is None or not text or not any(match.groups()):
        raise ValueError(f"durée illisible : {text!r}")
    minutes = match[1] or match[3]
    return float(int(minutes or 0) * 60 + float((match[2] or "0").replace(",", ".")))


_TILE_DELTA = re.compile(r"(?<=\S)\s+[+\-\u2212\u2013]\d[\d.,]*\s*[kKmM]?$")
_CHANGE = re.compile(rf"([+\-\u2212\u2013]?)\s*({_PERCENT_NUMBER})\s*%")
_SHORT_DATE = re.compile(r"(\d{1,2})\s+([^\W\d_]+)\.?\s+(\d{4})(?:[,\s]+(\d{1,2})[:h](\d{2}))?")
# Page Publications : « 2 oct., 12:30 » sans annee (annee de la page, deduite de la date du releve).
_SHORT_DATE_NO_YEAR = re.compile(r"(\d{1,2})\s+([^\W\d_]+)\.?,?\s*(?:(\d{1,2})[:h](\d{2}))?")


def parse_change(value: Any) -> float | None:
    """Evolution affichee par TikTok, « +12,5% » / « -3 % » / « 4% » -> pourcentage signe (12.5, -3.0, 4.0) ;
    tiret ou vide -> ``None`` ; autre -> ``ValueError``."""
    text = _text(value)
    if text in _ABSENT:
        return None
    match = _CHANGE.fullmatch(text)
    if match is None:
        raise ValueError(f"évolution illisible : {text!r}")
    number = _percent_number(match[2], text)
    return -number if match[1] in ("-", "\u2212", "\u2013") else number


def parse_date(value: Any, months: list[str], today: datetime | None = None) -> str | None:
    """Date de creation affichee (« 2026-10-01 14:05 », « 01/10/2026 14:05 », « 1 oct. 2026, 14:05 ») -> ISO 8601
    sans fuseau (l'heure de la page) ; format inconnu -> ``None`` (le texte brut reste dans ``posted_at_text``)."""
    text = _squash(value) if isinstance(value, str) else ""
    for pattern, order in ((r"(\d{4})-(\d{2})-(\d{2})(?:[ T]+(\d{1,2}):(\d{2}))?", "ymd"),
                           (r"(\d{1,2})/(\d{1,2})/(\d{4})(?:[ ,]+(\d{1,2}):(\d{2}))?", "dmy")):
        match = re.fullmatch(pattern, text)
        if match:
            a, b, c = (int(match[i]) for i in (1, 2, 3))
            year, month, day = (a, b, c) if order == "ymd" else (c, b, a)
            return _iso_date(year, month, day, match[4], match[5])
    match = _SHORT_DATE.fullmatch(text)
    if match:
        names = [m.casefold() for m in months]
        word = match[2].casefold()
        index = next((i for i, name in enumerate(names) if name == word or (len(word) >= 3 and name.startswith(word))), None)
        if index is not None:
            return _iso_date(int(match[3]), index + 1, int(match[1]), match[4], match[5])
    match = _SHORT_DATE_NO_YEAR.fullmatch(text)
    if match and today is not None:
        names = [m.casefold() for m in months]
        word = match[2].casefold()
        index = next((i for i, name in enumerate(names) if name == word or (len(word) >= 3 and name.startswith(word))), None)
        if index is not None:
            # annee du releve en heure de Paris (celle de la page, pas UTC) ; une date a plus de 31 jours dans le
            # futur appartient a l'annee precedente
            paris = (today if today.tzinfo else today.replace(tzinfo=timezone.utc)).astimezone(_PARIS).replace(tzinfo=None)
            iso = _iso_date(paris.year, index + 1, int(match[1]), match[3], match[4])
            if iso is not None and datetime.fromisoformat(iso) > paris + timedelta(days=31):
                iso = _iso_date(paris.year - 1, index + 1, int(match[1]), match[3], match[4])
            return iso
    return None


def _iso_date(year: int, month: int, day: int, hour: str | None, minute: str | None) -> str | None:
    try:
        return datetime(year, month, day, int(hour or 0), int(minute or 0)).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- backend browser


Opener = Callable[..., AbstractContextManager]


class _Flow:
    """Une publication pilotee sur une page. Chaque methode garde d'abord la page
    (captcha, verification, connexion) avant d'agir."""

    def __init__(self, page: Any, account: str, selectors: dict[str, Any], settings: dict[str, Any], *,
                 now: datetime, sleep: Callable[[float], None], rng: Any, on_tick: Callable[[], None] | None,
                 harvest: bool = False, config: Config | None = None) -> None:
        self.page, self.account, self.sel, self.settings = page, account, selectors, settings
        self.config = config  # [browser] state_dir : dossier de la capture d'arret
        self.now, self._sleep, self.rng, self.on_tick = now, sleep, rng, on_tick
        self.harvesting, self._harvested, self._lenient = harvest, False, False  # releve opportuniste (SPEC-86fe R4)

    # -- arret sur
    def stop(self, code: str, reason: str) -> TikTokStop:
        capture: Path | None = None
        target = browser.profile_dir(self.account, self.config) / "captures" / f"{self.now.strftime('%Y%m%dT%H%M%S')}-{code}.png"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(target), full_page=True)
            capture = target
        except Exception as exc:  # noqa: BLE001 - dit dans la raison, jamais avale
            reason += f" (capture d'écran impossible : {exc})"
        if capture is not None:
            # le HTML de la page, a cote de la capture : ce qu'il y avait sous l'ecran (sans jamais masquer l'arret)
            try:
                target.with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 - journalise, l'arret R4 d'origine reste tel quel
                logger.warning("TikTok %s : HTML de la page non enregistré à côté de la capture : %s", self.account, exc)
        logger.error("TikTok %s : %s", self.account, reason)
        return TikTokStop(code, reason, capture)

    def reject(self, code: str, reason: str) -> Exception:
        """Valeur ou page illisible : un arret R4 avec capture, sauf pendant un releve opportuniste (la
        publication ou la verification en cours ne doit jamais s'arreter pour un releve en plus) : ``ValueError``."""
        return ValueError(reason) if self._lenient else self.stop(code, reason)

    def guard(self, modals: bool = True) -> None:
        url = str(self.page.url)
        if any(marker in url for marker in self.sel["expect"]["login_url_markers"]):
            raise self.stop("login", f"connexion expirée : reconnecte le compte {self.account} (page : {url})")
        for code, label in (("captcha", "captcha détecté : arrêt immédiat, à résoudre à la main"),
                            ("verification", "vérification de compte demandée : arrêt immédiat, à faire à la main"),
                            ("login", f"connexion expirée : reconnecte le compte {self.account}")):
            for css in self.sel["detect"][code]:
                if self.page.query_selector(css) is not None:
                    raise self.stop(code, label)
        if modals:
            self.close_popups()
        self.harvest(only_if_rows=True)

    def close_popups(self) -> None:
        """Ferme les fenetres connues (``[popups]`` : texte -> bouton) et le journalise ; toute autre
        fenetre modale est un arret R4 (jamais de clic de repli). Une fenetre a la fois, la plus haute
        d'abord (la derniere dans l'ordre du DOM, portail le plus recent) : l'overlay d'une fenetre
        du dessus intercepte les clics de celle du dessous, qui n'est lue qu'une fois la premiere fermee."""
        known = self.sel["popups"]
        for _ in range(MAX_POPUP_ROUNDS):
            shown = [m for m in self.page.query_selector_all(self.sel["modal"]["container"]) if m.is_visible()]
            if not shown:
                return
            modal = shown[-1]
            text = " ".join(str(modal.inner_text()).split())
            fragment = next((f for f in known if f.casefold() in text.casefold()), None)
            if fragment is None:
                raise self.stop("unexpected_page", f"fenêtre inattendue : {text[:150]!r}")
            label = known[fragment]
            button = modal.query_selector(self.sel["modal"]["button"].format(label=label))
            if button is None:
                raise self.stop("element_missing", f"fenêtre connue « {fragment} » sans son bouton « {label} »")
            button.click(timeout=float(self.settings["click_timeout_s"]) * 1000)   # jamais les 30 s de Playwright
            logger.info("TikTok %s : fenêtre connue « %s » fermée par « %s »", self.account, fragment, label)
            if self.settings["content_check"] == "wait" and self.sel["modal"]["content_check_popup"].casefold() in fragment.casefold():
                # « Annuler » refuse seulement l'activation automatique proposee par TikTok : il ne change pas
                # l'interrupteur « Vérification de contenu simple », que le parcours controle et attend ensuite
                # ([tiktok] content_check = "wait"). Jamais « Activer » (le programme n'active rien a la place de
                # l'utilisateur) ; la politique n'est pas modifiee en silence : elle est dite ici.
                logger.warning("TikTok %s : fenêtre « %s » fermée par « %s » ; la vérification de contenu demandée "
                               "([tiktok] content_check = \"wait\") reste contrôlée par le parcours, rien n'est désactivé",
                               self.account, fragment, label)
        raise self.stop("unexpected_page", f"fenêtres surgissantes qui reviennent après {MAX_POPUP_ROUNDS} fermetures")

    # -- actions
    def pause(self) -> None:
        if self.on_tick is not None:
            self.on_tick()
        self._sleep(self.rng.uniform(float(self.settings["min_action_delay_s"]), float(self.settings["max_action_delay_s"])))
        if self.on_tick is not None:
            self.on_tick()

    def wait(self, name: str, *, timeout_key: str = "action_timeout_s", state: str | None = None,
             table: str = "selectors", modals: bool = True, optional: bool = False) -> Any:
        self.guard(modals)
        timeout = float(self.settings[timeout_key])
        try:
            element = self.page.wait_for_selector(self.sel[table][name], timeout=timeout * 1000, state=state)
        except Exception as exc:
            if "Timeout" not in type(exc).__name__:
                raise
            element = None
        if element is None:
            if optional:
                return None
            raise self.stop("element_missing", f"élément attendu absent après {timeout:g} s : {name}")
        return element

    def click_with(self, find: Callable[[], Any], first: Any = None) -> None:
        """Clic unique du parcours de publication. Une fenetre surgie entre la verification et le clic
        (l'overlay « intercepts pointer events », ou une fenetre modale visible) : ``close_popups`` (fenetres
        connues seulement, une inconnue est un arret R4), l'element est recherche a nouveau et clique UNE
        fois de plus ; un second echec remonte (arret R4 par l'appelant). Jamais de clic de repli."""
        timeout_ms = float(self.settings["click_timeout_s"]) * 1000
        try:
            (first if first is not None else find()).click(timeout=timeout_ms)
            return
        except Exception as exc:
            if not self._click_blocked(exc):
                raise
            logger.info("TikTok %s : clic bloqué par une fenêtre, fermeture des fenêtres connues puis nouvel essai : %s",
                        self.account, str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        self.close_popups()
        find().click(timeout=timeout_ms)

    def _click_blocked(self, exc: Exception) -> bool:
        if "intercepts pointer events" in str(exc):
            return True
        if "Timeout" not in type(exc).__name__:
            return False
        return any(m.is_visible() for m in self.page.query_selector_all(self.sel["modal"]["container"]))

    def click(self, name: str) -> None:
        self.click_with(lambda: self.wait(name))
        self.pause()

    def all(self, name: str) -> list[Any]:
        """Tous les elements d'un selecteur, une fois le premier apparu."""
        self.wait(name)
        return list(self.page.query_selector_all(self.sel["selectors"][name]))

    def type_caption(self, text: str) -> None:
        """L'editeur Draft.js est pre-rempli du nom du fichier : clic, tout selectionner, effacer, puis
        le texte d'un coup par insert_text (un seul evenement de saisie, instantane ; ``fill`` n'est pas
        pris en compte par l'editeur)."""
        self.click_with(lambda: self.wait("caption_editor"))
        keyboard = self.page.keyboard
        keyboard.press("Control+A")
        keyboard.press("Backspace")
        keyboard.insert_text(text)
        keyboard.press("Escape")  # ferme la liste de suggestions de hashtags qui recouvre le formulaire
        self.pause()

    def expand_settings(self) -> None:
        """« Afficher plus » seulement si les parametres sont repliees (visibilite non affichee)."""
        dropdown = self.page.query_selector(self.sel["selectors"]["visibility_dropdown"])
        if dropdown is None or not dropdown.is_visible():
            self.click("advanced_settings")

    def apply_options(self) -> None:
        """Commentaires, reutilisation, contenu genere par IA (SPEC-1ed3 R2) : chaque case est lue, et cliquee
        seulement si elle n'est pas deja dans l'etat voulu. Bloc absent = arret R4, jamais un clic de repli."""
        for key, name, label in (("allow_comments", "comment_switch", "commentaires"),
                                 ("allow_reuse", "reuse_switch", "réutilisation du contenu"),
                                 ("ai_generated", "ai_switch", "contenu généré par IA")):
            box = self.page.query_selector(self.sel["selectors"][name])
            if box is None:
                raise self.stop("element_missing", f"case « {label} » introuvable (réglage de publication {key})")
            wanted = bool(self.settings[key])
            if bool(box.is_checked()) == wanted:
                continue
            if not box.is_enabled():
                # TikTok desactive certaines cases selon la video (ex. duo/collage indisponibles) :
                # restriction de TikTok, pas une page inattendue -> journalise et continue.
                logger.warning("TikTok %s : case « %s » désactivée par TikTok pour cette vidéo, laissée telle quelle",
                               self.account, label)
                continue
            # Case dessinee en CSS par-dessus un input invisible : Playwright refuse check() ; un clic
            # JavaScript sur l'input declenche le meme changement que le clic de l'utilisateur.
            box.evaluate("el => el.click()")
            self.pause()
            if bool(box.is_checked()) != wanted:
                raise self.stop("unexpected_page", f"la case « {label} » n'a pas pu être réglée")
            logger.info("TikTok %s : %s réglé à %s", self.account, label, "oui" if wanted else "non")

    def schedule_now(self) -> None:
        # Video privee : TikTok grise « Maintenant » et « Programmer », « Maintenant » deja coche.
        # Clic inutile (et impossible) quand le choix est deja fait ; grise ET non coche = arret.
        radio = self.wait("schedule_now").query_selector(self.sel["selectors"]["radio_input"])
        if radio is not None and radio.is_checked():
            logger.info("TikTok %s : « Maintenant » deja choisi, aucun clic", self.account)
            return
        if radio is not None and not radio.is_enabled():
            raise self.stop("unexpected_page", "« Maintenant » est grisé sans être coché : publication impossible")
        self.click("schedule_now")

    def schedule_later(self, target: datetime) -> tuple[datetime, str | None]:
        """Programmation par les champs de TikTok : Programmer, date (calendrier), heure (selecteur).
        Rend l'instant reellement programme (minutes arrondies au pas propose) et une note ou None."""
        local = target.astimezone(ZoneInfo(browser.TIMEZONE))  # heure de Paris, jamais le fuseau du PC (R8)
        self.click("schedule_later")
        self.wait("schedule_fields")
        if self.page.query_selector(self.sel["selectors"]["schedule_date_picker"]) is not None:
            return self.schedule_with_lists(local)   # nouvelle page Studio (releve 2026-10-26) : listes deroulantes
        if len(self.all("schedule_inputs")) != 2 or ":" not in str(self.field(0).input_value()):
            raise self.stop("unexpected_page", "champs de programmation inattendus : l'heure (valeur avec « : »), "
                                               "puis la date, sont attendus")
        self.click_with(lambda: self.field(1))
        self.pause()
        self.pick_date(local)
        self.click("schedule_picker_close")
        self.click_with(lambda: self.field(0))
        self.pause()
        minute = self.pick_time(local)
        self.click("schedule_picker_close")
        effective = local.replace(minute=minute, second=0, microsecond=0) if minute != local.minute else local
        shown = (str(self.field(1).input_value()).strip(), str(self.field(0).input_value()).strip())
        expected = (effective.strftime("%Y-%m-%d"), effective.strftime("%H:%M"))
        if shown != expected:
            raise self.stop("unexpected_page", f"programmation non prise en compte : {shown[0]} {shown[1]} affiché, "
                                               f"{expected[0]} {expected[1]} attendu")
        note = None
        if minute != local.minute:
            note = f"minutes arrondies au pas proposé par TikTok : {local.minute:02d} -> {minute:02d}"
            logger.warning("TikTok %s : %s (heure demandée %s)", self.account, note, local.strftime("%H:%M"))
        return effective, note

    def field(self, index: int) -> Any:
        """Champ de programmation (0 heure, 1 date), relu a chaque fois : la page les redessine."""
        fields = self.page.query_selector_all(self.sel["selectors"]["schedule_inputs"])
        if len(fields) != 2:
            raise self.stop("unexpected_page", f"{len(fields)} champ(s) de programmation affiché(s), 2 attendus")
        return fields[index]

    def pick_date(self, target: datetime) -> None:
        months = [m.casefold() for m in self.sel["calendar"]["months"]]
        for _ in range(MAX_MONTH_STEPS + 1):
            month_text = str(self.wait("calendar_month_title").inner_text()).strip()
            year_text = str(self.wait("calendar_year_title").inner_text()).strip()
            if month_text.casefold() not in months or not year_text.isdigit():
                raise self.stop("unexpected_page", f"mois affiché illisible dans le calendrier : {month_text!r} {year_text!r}")
            gap = (target.year - int(year_text)) * 12 + target.month - (months.index(month_text.casefold()) + 1)
            if gap == 0:
                break
            arrows = self.all("calendar_arrow")
            if len(arrows) < 2:
                raise self.stop("element_missing", "flèches du calendrier absentes (2 attendues : précédent, suivant)")
            self.click_with(lambda: self.all("calendar_arrow")[1 if gap > 0 else 0])
            self.pause()
        else:
            raise self.stop("unexpected_page", f"mois cible {target.year}-{target.month:02d} introuvable dans le calendrier")
        for index, day in enumerate(self.all("calendar_day")):
            if str(day.inner_text()).strip() == str(target.day):
                self.click_with(lambda: self.all("calendar_day")[index])
                self.pause()
                return
        raise self.stop("element_missing", f"jour {target.day} absent du calendrier ({target.strftime('%Y-%m')})")

    def pick_calendar_day(self, target: datetime) -> None:
        """Nouvelle page Studio : la liste date s'ouvre en calendrier DatePicker (titre mois / annee, fleches, jours).
        Seuls les jours du mois affiche et non grises sont cliquables (``datepicker_day``) : un jour grise du mois
        voisin ou passe n'est jamais clique. Le jour choisi est relu dans la liste fermee par l'appelant."""
        months = [m.casefold() for m in self.sel["calendar"]["months"]]
        for _ in range(MAX_MONTH_STEPS + 1):
            month_text = str(self.wait("datepicker_month").inner_text()).strip()
            year_text = str(self.wait("datepicker_year").inner_text()).strip()
            if month_text.casefold() not in months or not year_text.isdigit():
                raise self.stop("unexpected_page", f"mois affiché illisible dans le calendrier : {month_text!r} {year_text!r}")
            gap = (target.year - int(year_text)) * 12 + target.month - (months.index(month_text.casefold()) + 1)
            if gap == 0:
                break
            arrows = self.all("datepicker_nav")
            if len(arrows) != 2:
                raise self.stop("element_missing", f"flèches du calendrier : {len(arrows)} trouvée(s), 2 attendues (précédent, suivant)")
            self.click_with(lambda: self.all("datepicker_nav")[1 if gap > 0 else 0])
            self.pause()
        else:
            raise self.stop("unexpected_page", f"mois cible {target.year}-{target.month:02d} introuvable dans le calendrier")
        days = [d for d in self.page.query_selector_all(self.sel["selectors"]["datepicker_day"])
                if str(d.inner_text()).strip() == str(target.day)]
        if len(days) != 1:
            raise self.stop("element_missing", f"jour {target.day} {'absent' if not days else 'ambigu'} ou grisé dans le calendrier "
                                               f"({target.strftime('%Y-%m')}) : {len(days)} jour(s) cliquable(s) trouvé(s)")
        self.click_with(lambda: next(d for d in self.page.query_selector_all(self.sel["selectors"]["datepicker_day"])
                                     if str(d.inner_text()).strip() == str(target.day)))
        self.pause()

    def pick_time(self, target: datetime) -> int:
        """Clique l'heure et la minute ; rend la minute choisie (la plus proche de la demandee
        parmi celles que TikTok propose)."""
        for index, option in enumerate(self.all("timepicker_hour")):
            if str(option.inner_text()).strip().isdigit() and int(str(option.inner_text()).strip()) == target.hour:
                self.click_with(lambda: self.all("timepicker_hour")[index])
                self.pause()
                break
        else:
            raise self.stop("element_missing", f"heure {target.hour:02d} absente du sélecteur d'heure")
        offered = {int(t): o for o in self.all("timepicker_minute") if (t := str(o.inner_text()).strip()).isdigit()}
        if not offered:
            raise self.stop("element_missing", "minutes absentes du sélecteur d'heure")
        minute = min(offered, key=lambda m: (abs(m - target.minute), m))
        self.click_with(lambda: {int(str(o.inner_text()).strip()): o for o in self.all("timepicker_minute")
                                 if str(o.inner_text()).strip().isdigit()}[minute])
        self.pause()
        return minute

    def schedule_with_lists(self, local: datetime) -> tuple[datetime, str | None]:
        """Nouvelle page Studio : date et heure sont des listes deroulantes (« 26 oct. 2026 », « 17:15 ») et un
        fuseau est affiche (« (GMT+1) Heure locale »). Le fuseau est lu d'abord (jamais change) : autre que Paris,
        arret R4 avant tout clic. Chaque valeur choisie est relue dans la liste fermee."""
        self.check_timezone(local)
        self.open_list("schedule_date_picker")
        self.pick_calendar_day(local)
        self.close_list("schedule_date_picker")
        self.open_list("schedule_time_picker")
        minute = self.time_minute(local)
        self.pick_option(lambda options: self.time_option(options, local.hour, minute), f"heure {local.hour:02d}:{minute:02d}")
        self.close_list("schedule_time_picker")
        effective = local.replace(minute=minute, second=0, microsecond=0) if minute != local.minute else local
        date_shown, time_shown = self.list_value("schedule_date_picker"), self.list_value("schedule_time_picker")
        if not self.is_date(date_shown, effective) or time_shown != effective.strftime("%H:%M"):
            raise self.stop("unexpected_page", f"programmation non prise en compte : {date_shown} {time_shown} affiché, "
                                               f"{effective.strftime('%Y-%m-%d %H:%M')} attendu")
        note = None
        if minute != local.minute:
            note = f"minutes arrondies au pas proposé par TikTok : {local.minute:02d} -> {minute:02d}"
            logger.warning("TikTok %s : %s (heure demandée %s)", self.account, note, local.strftime("%H:%M"))
        return effective, note

    def check_timezone(self, local: datetime) -> None:
        field = self.page.query_selector(self.sel["selectors"]["schedule_timezone"])
        if field is None:
            raise self.stop("element_missing", "fuseau horaire de la programmation absent : impossible de vérifier l'heure de Paris")
        label = " ".join(str(field.input_value()).split())
        match = re.search(r"\(GMT([+-])(\d{1,2})(?::?(\d{2}))?\)", label)
        if match is None:
            raise self.stop("unexpected_page", f"fuseau horaire illisible dans la page : {label!r}")
        shown = (1 if match[1] == "+" else -1) * (int(match[2]) * 60 + int(match[3] or 0))
        # Paris a la date visee ; la page peut afficher le decalage du jour (changement d'heure entre les deux)
        allowed = {int(local.utcoffset().total_seconds() // 60),
                   int(self.now.astimezone(ZoneInfo(browser.TIMEZONE)).utcoffset().total_seconds() // 60)}
        if shown not in allowed:
            expected = " ou ".join(f"GMT{m // 60:+d}" for m in sorted(allowed))
            raise self.stop("unexpected_page", f"fuseau affiché « {label} » : ce n'est pas l'heure de Paris "
                                               f"({expected} attendu), rien n'est programmé")

    def open_list(self, name: str) -> None:
        self.click_with(lambda: self.wait(name))
        self.pause()

    def close_list(self, name: str) -> None:
        """La liste se ferme d'elle-meme au choix ; sinon (encore ouverte), clic hors d'elle."""
        picker = self.page.query_selector(self.sel["selectors"][name])
        if picker is not None and str(picker.get_attribute("aria-expanded")) == "true":
            self.click("schedule_picker_close")

    def list_value(self, name: str) -> str:
        picker = self.wait(name)
        value = picker.query_selector(self.sel["selectors"]["picker_value"])
        if value is None:
            raise self.stop("element_missing", f"valeur affichée de la liste absente : {name}")
        return " ".join(str(value.inner_text()).split())

    def pick_option(self, choose: Callable[[list[Any]], Any], what: str) -> None:
        options = self.all("picker_option")
        if choose(options) is None:
            raise self.stop("element_missing", f"{what} absente de la liste déroulante ({len(options)} choix proposés)")
        self.click_with(lambda: choose(list(self.page.query_selector_all(self.sel["selectors"]["picker_option"]))))
        self.pause()

    def date_option(self, options: list[Any], local: datetime) -> Any:
        return next((o for o in options if self.is_date(str(o.inner_text()), local)), None)

    def is_date(self, text: str, local: datetime) -> bool:
        match = re.fullmatch(r"\s*(\d{1,2})\s+([^\s\d.]+)\.?\s+(\d{4})\s*", text.replace("\xa0", " "))
        if match is None:
            return False
        word = match[2].casefold()
        for names in (self.sel["calendar"]["months"], self.sel["calendar"]["months_short"]):
            folded = [n.casefold().rstrip(".") for n in names]
            if word in folded and folded.index(word) + 1 == local.month:
                return (int(match[1]), int(match[3])) == (local.day, local.year)
        return False

    def time_minute(self, local: datetime) -> int:
        """La minute proposee la plus proche, dans l'heure demandee (pas de TikTok : 15 minutes en general)."""
        minutes = []
        self.wait("picker_option", optional=True)   # absent = liste inconnue : l'arret ci-dessous dit laquelle
        for o in self.page.query_selector_all(self.sel["selectors"]["picker_option"]):
            m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(o.inner_text()))
            if m and int(m[1]) == local.hour:
                minutes.append(int(m[2]))
        if not minutes:
            raise self.stop("element_missing", f"heure {local.hour:02d} absente de la liste d'heures : aucun choix reconnu "
                                               f"({self.sel['selectors']['picker_option']}) dans la liste ouverte, rien n'est cliqué ; "
                                               f"HTML de la page enregistré à côté de la capture pour relever la liste")
        return min(minutes, key=lambda m: (abs(m - local.minute), m))

    def time_option(self, options: list[Any], hour: int, minute: int) -> Any:
        return next((o for o in options if re.fullmatch(r"\s*0?%d:%02d\s*" % (hour, minute), str(o.inner_text()))), None)

    def disable_content_check(self) -> None:
        """Coupe l'interrupteur « Verification de contenu simple » s'il est actif (TikTok modere de toute
        facon apres publication) : plus d'attente ni de fenetre « Continuer a publier ? »."""
        switch = self.page.query_selector(self.sel["selectors"]["content_check_switch"])
        if switch is None:
            raise self.stop("element_missing", "interrupteur « Vérification de contenu simple » introuvable")
        limit_reached = self.page.query_selector(self.sel["selectors"]["content_check_limit"]) is not None
        if switch.is_checked() and (switch.is_disabled() or limit_reached):
            # Limite quotidienne de vérifications atteinte : TikTok grise l'interrupteur (« Tu as atteint la
            # limite de vérifications pour aujourd'hui »), aucune vérification ne tourne, rien à couper.
            if limit_reached:
                logger.info("TikTok %s : vérification de contenu indisponible (interrupteur grisé, limite du jour)",
                            self.account)
            else:
                logger.info("TikTok %s : vérification de contenu déjà lancée (interrupteur grisé) : impossible à "
                            "couper, sa fin sera attendue avant la publication", self.account)
            return
        if switch.is_checked():
            switch.uncheck(force=True)
            self.pause()
            if switch.is_checked():
                raise self.stop("unexpected_page", "la vérification de contenu n'a pas pu être coupée")
            logger.info("TikTok %s : vérification de contenu coupée avant publication", self.account)

    def shown(self, css: str) -> bool:
        """Vrai si un element VISIBLE correspond : un texte reste parfois dans la page sans etre affiche
        (releve reel 2026-10-05 : « Vérification en cours » trouve apres la fin de la verification)."""
        elements = list(self.page.query_selector_all(css) or [])
        if not elements:
            element = self.page.query_selector(css)
            elements = [element] if element is not None else []
        return any(e.is_visible() for e in elements)

    def enable_content_check(self) -> None:
        """Mode ``wait`` : l'interrupteur « Verification de contenu simple » doit etre allume avant d'attendre le
        resultat, sinon aucun scan ne demarre. Eteint et accessible : allume une fois pour cette video ; deja
        allume : aucun clic ; grise eteint, introuvable ou refuse : arret R4 tout de suite, sans attendre le delai."""
        switch = self.page.query_selector(self.sel["selectors"]["content_check_switch"])
        if switch is None:
            raise self.stop("element_missing", "interrupteur « Vérification de contenu simple » introuvable : impossible "
                                               "de savoir si la vérification tourne ([tiktok] content_check = \"wait\")")
        if switch.is_checked():
            return
        if switch.is_disabled():
            raise self.stop("unexpected_page", "interrupteur « Vérification de contenu simple » grisé alors qu'éteint : "
                                               "impossible à allumer ([tiktok] content_check = \"wait\")")
        switch.check(force=True)
        self.pause()
        if not switch.is_checked():
            raise self.stop("unexpected_page", "l'interrupteur « Vérification de contenu simple » n'a pas pu être allumé")
        logger.info("TikTok %s : interrupteur allumé pour cette vidéo : [tiktok] content_check = wait", self.account)

    def await_content_check(self) -> None:
        """Avant le clic final : attend « Aucun probleme constate ». Probleme signale par TikTok : arret de CE clip
        (code ``content_check_refused``, le worker le marque refuse par la plateforme et le compte continue) ; delai
        depasse : arret R4 (code ``content_check``). Jamais de publication d'un contenu non verifie."""
        sel = self.sel["selectors"]
        timeout, interval = float(self.settings["content_check_timeout_s"]), float(self.settings["poll_interval_s"])
        retrigger_s, max_retriggers = float(self.settings["content_check_retrigger_s"]), int(self.settings["content_check_retriggers"])
        waited, since, retriggers = 0.0, 0.0, 0   # since : depart, derniere relance, ou derniere fois non « en cours »
        acted = None                              # instant de la derniere relance (tentee) : espace les erreurs
        while True:
            self.guard()
            # « Vérification en cours » encore affiché : pas fini, même si « Aucun problème constaté » existe
            # ailleurs dans la page (relevé réel 2026-10-05, fausse fin puis fenêtre « Continuer à publier ? »).
            running = self.shown(sel["content_check_running"])
            if not running and self.shown(sel["content_check_ok"]):
                logger.info("TikTok %s : vérification de contenu sans problème constaté", self.account)
                return
            problem = self.page.query_selector(sel["content_check_problem"])
            if problem is not None:
                detail = " ".join(str(problem.inner_text()).split())[:150]
                raise self.stop("content_check_refused", "vérification de contenu : problème signalé par TikTok"
                                + (f" ({detail})" if detail else ""))
            if not running and self.shown(sel["content_check_ok"]):
                logger.info("TikTok %s : vérification de contenu sans problème constaté", self.account)
                return
            # Limite quotidienne atteinte (relevé réel 2026-10-20, 20 publications dans la journée) : aucun scan ne
            # tournera avant demain, attendre le délai ne sert à rien ; le compte s'arrête tout de suite.
            if self.shown(sel["content_check_limit"]):
                raise self.stop("content_check_limit", "limite quotidienne de vérifications de contenu atteinte "
                                                       "(« Réessaie demain ») : compte à reprendre demain")
            if waited >= timeout:
                raise self.stop("content_check", f"vérification de contenu non terminée après {timeout:g} s "
                                                 f"([tiktok] content_check_timeout_s)")
            if not running:
                since = waited
            # Erreur TikTok (relevé réel 2026-10-06) : « Réessayer » ; sinon « en cours » trop long : décocher/recocher.
            error = self.page.query_selector(sel["content_check_error"]) if self.shown(sel["content_check_error"]) else None
            if error is not None and (acted is None or waited - acted >= retrigger_s):
                if retriggers >= max_retriggers:
                    message = " ".join(str(error.inner_text()).split())[:150]
                    raise self.stop("content_check", f"vérification de contenu en erreur après {retriggers} relance(s) "
                                                     f"([tiktok] content_check_retriggers) : « {message} »")
                if self._relaunch_content_check(retriggers + 1, max_retriggers, error=True):
                    retriggers += 1
                since = acted = waited
            elif error is None and running and retriggers < max_retriggers and waited - since >= retrigger_s:
                if self._relaunch_content_check(retriggers + 1, max_retriggers, error=False):
                    retriggers += 1
                since = acted = waited
            self.page.wait_for_timeout(interval * 1000)
            waited += interval
            if self.on_tick is not None:
                self.on_tick()

    def _relaunch_content_check(self, number: int, total: int, *, error: bool) -> bool:
        """Relance le scan : lien « Réessayer » (erreur TikTok) sinon interrupteur décoché puis recoché. Faux (et
        journalisé) si rien ne peut être relancé : l'attente continue telle quelle. La case doit être recochée,
        sinon arrêt ``unexpected_page`` : jamais de publication sans vérification en mode wait."""
        sel = self.sel["selectors"]
        if error:
            link = self.page.query_selector(sel["content_check_retry"])
            if link is not None:
                self.click_with(lambda: self.page.query_selector(sel["content_check_retry"]) or link)
                self.pause()
                logger.info("TikTok %s : vérification de contenu en erreur, « Réessayer » cliqué (relance %d/%d)",
                            self.account, number, total)
                return True
        switch = self.page.query_selector(sel["content_check_switch"])
        if switch is None:
            logger.info("TikTok %s : vérification de contenu bloquée, interrupteur introuvable : relance impossible, "
                        "l'attente continue", self.account)
            return False
        if switch.is_disabled():
            logger.info("TikTok %s : vérification de contenu bloquée, interrupteur grisé : relance impossible, "
                        "l'attente continue", self.account)
            return False
        switch.uncheck(force=True)
        self.pause()
        switch.check(force=True)
        self.pause()
        if not switch.is_checked():
            raise self.stop("unexpected_page", "la vérification de contenu n'a pas été réactivée après la relance")
        logger.info("TikTok %s : vérification de contenu bloquée, interrupteur décoché puis recoché (relance %d/%d)",
                    self.account, number, total)
        return True

    def post(self, mode: str) -> None:
        """Le bouton final unique ; son texte doit correspondre au mode, sinon la page n'est pas dans l'etat voulu."""
        button = self.wait("post_button")
        expected = self.sel["labels"]["post_scheduled" if mode == "scheduled" else "post_now"]
        label = " ".join(str(button.inner_text()).split())
        if label != expected:
            raise self.stop("unexpected_page", f"bouton final « {label} » au lieu de « {expected} » : "
                                               f"la page n'est pas en mode {mode}")
        self.click_with(lambda: self.wait("post_button"), first=button)
        self.pause()

    def await_published(self, mode: str) -> None:
        """Preuve de publication apres le clic final : navigation vers la page Publications (principale)
        ou message « Video publiee » (secours), avant ``publish_confirm_timeout_s``. La fenetre « Continuer
        a publier ? » (verification encore en cours) est annulee, la fin de la verification attendue, puis
        le bouton re-clique UNE fois. Ni preuve ni fenetre : arret R4 ``publish_unconfirmed``, jamais un
        succes suppose."""
        sel, labels = self.sel["selectors"], self.sel["continue_publish"]
        timeout, interval = float(self.settings["publish_confirm_timeout_s"]), float(self.settings["poll_interval_s"])
        waited, retried = 0.0, False
        while True:
            self.guard(modals=False)  # la fenetre « Continuer a publier ? » est connue : traitee ci-dessous
            dialog = self.continue_dialog()
            if dialog is not None:
                if retried:
                    raise self.stop("publish_unconfirmed", f"la fenêtre « {labels['dialog']} ? » est revenue "
                                                           f"après le nouvel essai : publication non confirmée")
                button = dialog.query_selector(self.sel["modal"]["button"].format(label=labels["cancel"]))
                if button is None:
                    raise self.stop("element_missing", f"fenêtre « {labels['dialog']} ? » sans son bouton "
                                                       f"« {labels['cancel']} »")
                button.click()
                logger.info("TikTok %s : fenêtre « %s ? » annulée, attente de la vérification de contenu avant "
                            "un nouvel essai", self.account, labels["dialog"])
                self.pause()
                self.await_content_check()
                self.post(mode)
                retried, waited = True, 0.0
                continue
            if str(self.page.url).startswith(self.sel["expect"]["published_url_prefix"]):
                logger.info("TikTok %s : publication prouvée par la navigation vers la page Publications", self.account)
                return
            if self.page.query_selector(sel["published_marker"]) is not None:
                logger.info("TikTok %s : publication prouvée par le message de confirmation", self.account)
                return
            if waited >= timeout:
                raise self.stop("publish_unconfirmed",
                                f"aucune preuve de publication après {timeout:g} s ([tiktok] publish_confirm_timeout_s) : "
                                f"ni navigation vers la page Publications ni message de confirmation ; la vidéo "
                                f"est peut-être publiée, à vérifier à la main avant de réessayer")
            self.page.wait_for_timeout(interval * 1000)
            waited += interval
            if self.on_tick is not None:
                self.on_tick()

    def continue_dialog(self) -> Any | None:
        fragment = self.sel["continue_publish"]["dialog"].casefold()
        for modal in self.page.query_selector_all(self.sel["modal"]["container"]):
            if modal.is_visible() and fragment in " ".join(str(modal.inner_text()).split()).casefold():
                return modal
        return None

    def run(self, clip: dict[str, Any], mode: str, schedule_at: datetime | None) -> dict[str, Any]:
        self.page.goto(self.sel["urls"]["upload"])
        self.guard()
        if not str(self.page.url).startswith(self.sel["expect"]["upload_url_prefix"]):
            raise self.stop("unexpected_page", f"page inattendue : {self.page.url}")
        self.pause()

        self.wait("file_input", state="attached")
        self.page.set_input_files(self.sel["selectors"]["file_input"], str(clip["video_path"]))
        self.pause()
        self.wait("upload_done", timeout_key="upload_timeout_s")
        self.pause()

        self.type_caption(" ".join([clip["caption"], *clip["hashtags"]]))
        self.expand_settings()
        wanted = self.sel["labels"]["visibility_" + str(self.settings["visibility"])]
        current = " ".join(str(self.wait("visibility_dropdown").inner_text()).split())
        if current != wanted:  # deja la bonne visibilite (« Tout le monde » par defaut) : aucun clic
            self.click("visibility_dropdown")
            self.click("visibility_" + str(self.settings["visibility"]))
        self.apply_options()

        effective, note = schedule_at, None
        if mode == "scheduled":
            effective, note = self.schedule_later(schedule_at)
        else:
            self.schedule_now()

        if self.settings["content_check"] == "off":
            self.disable_content_check()
        else:
            self.enable_content_check()
            self.await_content_check()
        self.post(mode)

        self.await_published(mode)
        return self.result(clip, mode, schedule_at, effective, note)

    def find_post_link(self, clip: dict[str, Any], target: datetime | None = None) -> tuple[str | None, str | None]:
        """Page Publications : le premier lien de post dont le texte est le debut de la legende publiee.
        Rend (adresse complete, id) ou (None, raison). Ne leve jamais : la publication est deja prouvee.
        TikTok liste le nouveau post avec retard : tant qu'il manque, la page est rechargee toutes les
        ``post_lookup_interval_s`` pendant au plus ``post_lookup_timeout_s`` (temps d'attente des essais compris).
        ``target`` : l'heure programmee (programmation seulement) ; si plusieurs liens portent la legende, celui dont
        l'heure affichee est cette minute l'emporte (sinon le premier, ambiguite journalisee)."""
        interval, timeout = float(self.settings["post_lookup_interval_s"]), float(self.settings["post_lookup_timeout_s"])
        waited, attempts = 0.0, 0
        while True:
            attempts += 1
            url, found, spent = self._lookup_post_link(clip, reload=attempts > 1, target=target)
            broken, waited = spent < 0, waited + max(spent, 0.0)
            if url is not None:
                if attempts > 1:
                    logger.info("TikTok %s : post retrouvé sur la page Publications après %d essai(s), %g s",
                                self.account, attempts, waited)
                return url, found
            if broken or waited + interval > timeout:  # lecture cassee (pas de nouvel essai) ou delai epuise
                logger.warning("TikTok %s : post introuvable sur la page Publications après %d essai(s), %g s "
                               "([tiktok] post_lookup_timeout_s = %g) : %s", self.account, attempts, waited,
                               timeout, found)
                return None, found
            self.page.wait_for_timeout(interval * 1000)
            waited += interval

    def _lookup_post_link(self, clip: dict[str, Any], *, reload: bool,
                          target: datetime | None = None) -> tuple[str | None, str | None, float]:
        """Un essai de ``find_post_link`` : (adresse, id) ou (None, raison), et le temps d'attente de la page
        consomme (negatif : l'essai a casse, inutile de recommencer)."""
        try:
            if reload or not str(self.page.url).startswith(self.sel["expect"]["stats_url_prefix"]):
                self.page.goto(self.sel["urls"]["stats"])
            selector = self.sel["stats"]["post_link"]
            try:
                self.page.wait_for_selector(selector, timeout=float(self.settings["action_timeout_s"]) * 1000)
            except Exception as exc:
                if "Timeout" not in type(exc).__name__:
                    raise
                spent = float(self.settings["action_timeout_s"])
                return None, f"aucun lien de post affiché après {spent:g} s", spent
            self.harvest()
            published = _squash(" ".join([clip["caption"], *clip["hashtags"]]))
            # La liste est virtualisee et triee par date decroissante : un post programme a une date future, loin
            # sous les autres programmes ; on descend donc la liste comme le releve (``list_posts``) jusqu'au post
            # ou jusqu'au bas (``stats_scroll_rounds`` pas au plus), au lieu de ne lire que les premieres lignes.
            seen: set[str] = set()
            idle = 0
            for _ in range(int(self.settings["stats_scroll_rounds"]) + 1):
                matches: list[tuple[str, str]] = []
                fresh = False
                for link in self.page.query_selector_all(selector):
                    text = _squash(str(link.inner_text())).rstrip("….").rstrip()
                    href = link.get_attribute("href") or ""
                    found = _POST_ID_END.search(href)
                    if found is not None and found.group(1) not in seen:
                        seen.add(found.group(1))
                        fresh = True
                    if text and (published.startswith(text) or text.startswith(published)) and found is not None:
                        matches.append((urljoin(str(self.page.url), href), found.group(1)))
                if len(matches) == 1:
                    return matches[0][0], matches[0][1], 0.0
                if matches:
                    url, post_id = self._ambiguous_link(matches, target)
                    return url, post_id, 0.0
                at_bottom = self.page.evaluate(self.sel["stats"]["scroll_script"]) is True
                self.pause()
                idle = 0 if fresh else idle + 1
                if at_bottom and idle >= _SCROLL_IDLE:
                    break
            return None, "aucun lien dont le texte correspond à la légende publiée", 0.0
        except Exception as exc:  # noqa: BLE001 - dit dans la note, jamais avale
            return None, f"{type(exc).__name__} : {exc}", -1.0

    def _ambiguous_link(self, matches: list[tuple[str, str]], target: datetime | None) -> tuple[str, str]:
        """Plusieurs liens portent la legende (texte tronque) : celui dont l'heure affichee est la minute de
        ``target`` (heure de Paris). Sinon, le premier lien, comme avant, avec l'ambiguite journalisee : jamais
        devine en silence (ADR-ad2e)."""
        wanted = _paris_minute(target) if target is not None else None
        if wanted is not None:
            times = self._displayed_times()
            hits = [match for match in matches if times.get(match[1]) == wanted]
            if len(hits) == 1:
                logger.info("TikTok %s : %d posts portent la légende, celui de %s désigné par son heure affichée",
                            self.account, len(matches), wanted.isoformat(timespec="minutes"))
                return hits[0]
        logger.warning("TikTok %s : %d liens de post portent la légende publiée (texte tronqué) et l'heure affichée "
                       "ne désigne pas un seul post (créneau %s) : premier lien retenu, à vérifier (%s)",
                       self.account, len(matches), wanted.isoformat(timespec="minutes") if wanted else "inconnu",
                       matches[0][1])
        return matches[0]

    def _displayed_times(self) -> dict[str, datetime]:
        """Heure affichée (Paris, à la minute) de chaque post de la page Publications, par id ; un post dont la
        date n'est pas lisible n'y figure pas (rien n'est inventé)."""
        sel = self.sel["stats"]
        times: dict[str, datetime] = {}
        for row in self.page.query_selector_all(sel["row"]):
            link = row.query_selector(sel["post_link"])
            match = _POST_ID.search((link.get_attribute("href") or "") if link is not None else "")
            cell = row.query_selector(sel["created"])
            if match is None or cell is None:
                continue
            stamp = parse_date(cell.inner_text(), self.sel["calendar"]["months"], self.now)
            if stamp is not None:
                times[match.group(1)] = datetime.fromisoformat(stamp).replace(second=0, microsecond=0)
        return times

    # -- releve des statistiques (SPEC-86fe R1) : lecture seule, aucun clic hors le menu des periodes
    def stats(self, previous: dict[str, dict[str, Any]], full: bool = False) -> dict[str, Any]:
        """Page Donnees analytiques du compte (4 periodes), liste des Publications, puis l'analyse des posts
        a lire en detail. ``previous`` : les posts deja releves (historique fusionne). ``full`` : sans le plafond
        ``stats_detail_max``. Un post sans detail ni ancien detail porte ``detail_not_read`` (jamais de champ invente)."""
        overview = self.account_overview()
        rows = self.list_posts()
        todo = self.pick_details(rows, previous, full=full)
        posts = []
        for post_id, row in rows.items():
            if post_id in todo:
                posts.append(self.read_post(post_id, row))
            elif (previous.get(post_id) or {}).get("detailed_at"):
                posts.append(dict(row))  # la vue fusionnee garde son detail ancien
            else:
                posts.append({**row, "detail_not_read": True})
        return {"overview": overview, "posts": posts}

    def open_page(self, url: str, prefix: str) -> None:
        self.page.goto(url)
        self.guard()
        if not str(self.page.url).startswith(prefix):
            raise self.reject("unexpected_page", f"page inattendue : {self.page.url}")
        self.pause()

    def account_overview(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Tuiles de la page Donnees analytiques pour 7, 28, 60 et 365 jours : ``{periode: {tuile: {value, change_pct}}}``."""
        self.open_page(self.sel["urls"]["analytics_account"], self.sel["expect"]["account_analytics_url_prefix"])
        self.wait("tile", table="account")
        out = {}
        for days in PERIODS:
            self.select_period(days)
            out[str(days)] = self.read_tiles(days)
        return out

    def select_period(self, days: int) -> None:
        acc = self.sel["account"]
        label = acc["period_label"].format(days=days).casefold()
        button = self.wait("period_button", table="account")
        if label in _squash(str(button.inner_text())).casefold():
            return  # deja la periode affichee : aucun clic
        button.click()
        self.pause()
        selector = acc["period_option"].format(days=days)
        timeout = float(self.settings["action_timeout_s"])
        try:
            option = self.page.wait_for_selector(selector, timeout=timeout * 1000)
        except Exception as exc:
            if "Timeout" not in type(exc).__name__:
                raise
            option = None
        if option is None:
            raise self.stop("element_missing", f"période « {acc['period_label'].format(days=days)} » absente du menu "
                                               f"après {timeout:g} s")
        option.click()
        self.pause()
        self.guard()
        shown = self.page.query_selector(acc["period_button"])
        if shown is None or label not in _squash(str(shown.inner_text())).casefold():
            raise self.stop("unexpected_page", f"la période {days} jours n'est pas affichée après le choix")

    def read_tiles(self, days: int) -> dict[str, dict[str, Any]]:
        buttons = [_squash(str(b.inner_text())) for b in self.page.query_selector_all(self.sel["account"]["tile"])]
        tiles: dict[str, dict[str, Any]] = {}
        for key in TILES:
            label = _squash(self.sel["tiles"][key])
            found = next((b for b in buttons if b.casefold().startswith(label.casefold())), None)
            if found is None:
                tiles[key] = {"value": None, "change_pct": None}  # tuile absente de la page : null, jamais 0
                continue
            rest = found[len(label):].strip()
            change = re.search(r"\(([^()]*)\)\s*$", rest)
            body = rest[:change.start()].strip() if change else rest
            body = re.sub(r"^(?:--|\|)\s+(?=\S)", "", body)  # « -- 0 » : le premier tiret est un separateur
            body = _TILE_DELTA.sub("", body)  # « 24 -2 (-7.7%) » : variation absolue avant le pourcentage, ignoree
            where = f"tuile {key}, {days} jours"
            tiles[key] = {"value": self.read_value(body, key, parse_count, where),
                          "change_pct": self.read_value(change[1] if change else None, key, parse_change, where)}
        return tiles

    def list_posts(self) -> dict[str, dict[str, Any]]:
        """Page Publications : un dict par post (id -> ligne). La liste est virtualisee (le DOM ne garde que
        quelques lignes) : on la descend pas a pas (``scroll_script``) et on CUMULE les lignes lues a chaque pas.
        Fin = bas atteint et ``_SCROLL_IDLE`` tours de suite sans nouvelle ligne. Un compte sans aucun post (page
        vide) rend ``{}`` sans erreur. Si la fin n'est pas atteinte apres ``stats_scroll_rounds`` pas, c'est un arret
        journalise (R7 : jamais une liste tronquee en silence)."""
        self.open_page(self.sel["urls"]["stats"], self.sel["expect"]["stats_url_prefix"])
        rows = self.wait_rows_or_empty()
        if not rows:
            return {}
        rounds = int(self.settings["stats_scroll_rounds"])
        idle = 0
        for _ in range(rounds):
            at_bottom = self.page.evaluate(self.sel["stats"]["scroll_script"]) is True
            self.pause()
            fresh = {i: row for i, row in self.read_rows().items() if i not in rows}
            rows.update(fresh)
            idle = 0 if fresh else idle + 1
            if at_bottom and idle >= _SCROLL_IDLE:
                return rows
        raise self.stop("scroll_limit", f"la liste des Publications n'est pas finie après {rounds} défilements ({len(rows)} posts "
                                        f"lus) : relevé arrêté pour ne pas garder une liste tronquée, augmente "
                                        f"[tiktok] stats_scroll_rounds")
    def wait_rows_or_empty(self) -> dict[str, dict[str, Any]]:
        """Attend les lignes de la page Publications OU son etat vide, en course et quelques secondes seulement
        (``stats_empty_wait_s``, pas les 30 s d'un repere manquant) : un compte neuf n'a aucune ligne. Rend les
        lignes lues, ``{}`` si la page n'en affiche aucune."""
        sel = self.sel["stats"]
        self.guard()
        selector = f"{sel['row']}, {sel['empty_state']}" if sel["empty_state"] else sel["row"]
        try:
            self.page.wait_for_selector(selector, timeout=float(self.settings["stats_empty_wait_s"]) * 1000)
        except Exception as exc:
            if "Timeout" not in type(exc).__name__:
                raise
        self.guard()
        rows = self.read_rows()
        if not rows:
            logger.info("TikTok %s : la page Publications n'affiche aucun post (compte sans publication)", self.account)
        return rows

    def read_rows(self) -> dict[str, dict[str, Any]]:
        """Une ligne par post de la page Publications : legende, date, visibilite, vues, likes, commentaires."""
        sel = self.sel["stats"]
        found: dict[str, dict[str, Any]] = {}
        for number, row in enumerate(self.page.query_selector_all(sel["row"]), start=1):
            link = row.query_selector(sel["post_link"])
            href = (link.get_attribute("href") or "") if link is not None else ""
            match = _POST_ID.search(href)
            if match is None:
                raise self.reject("unexpected_page", f"ligne {number} de la liste sans lien de post exploitable")
            where = f"ligne {number}"
            texts = {key: row.query_selector(sel[key]) for key in ("views", "likes", "comments", "visibility", "created")}
            texts = {key: None if cell is None else cell.inner_text() for key, cell in texts.items()}
            created = _squash(texts["created"]) if isinstance(texts["created"], str) and texts["created"].strip() else None
            visibility = _squash(texts["visibility"]) if isinstance(texts["visibility"], str) and texts["visibility"].strip() else None
            labels = self.sel["labels"]
            visibility = next((name for name in VISIBILITIES if visibility == labels["visibility_" + name]), visibility)
            found.setdefault(match.group(1), {
                "post_id": match.group(1), "post_url": urljoin(str(self.page.url), href),
                "caption": _squash(str(link.inner_text())) or None,
                "posted_at": parse_date(created, self.sel["calendar"]["months"], self.now), "posted_at_text": created,
                "visibility": visibility,
                **{key: self.read_value(texts[key], key, parse_count, where) for key in ("views", "likes", "comments")}})
        return found

    def read_value(self, text: Any, key: str, parse: Callable[[Any], Any], where: str) -> Any:
        """Une valeur affichee, convertie ; absente de la page ou « en cours de traitement » : ``None``
        explicite, jamais un 0 invente ; illisible : arret sur."""
        if text is None:
            return None
        if isinstance(text, str) and self.sel["stats"]["processing"].casefold() in text.casefold():
            return None
        try:
            return parse(text)
        except ValueError:
            raise self.reject("unexpected_page", f"valeur illisible ({where}, {key}) : {text!r}") from None

    def pick_details(self, rows: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]],
                     full: bool = False) -> set[str]:
        """Posts a lire en detail (analyse + spectateurs + engagement) : jamais lus, encore « en cours de
        traitement » (vues nulles) ou publies depuis moins de ``stats_detail_days``, parmi les ``stats_detail_max``
        plus recents de la liste (ordre de la page Publications, recents d'abord) ; ``full`` : toute la liste."""
        recent = timedelta(days=float(self.settings["stats_detail_days"]))
        fresh, others = [], []
        within = list(rows.items()) if full else list(rows.items())[: int(self.settings["stats_detail_max"])]
        for post_id, row in within:
            posted = _naive_utc(row.get("posted_at"))
            if posted is not None and posted > self.now:
                continue  # programme, pas encore en ligne : rien a lire (jamais de mesure inventee, ADR-ad2e)
            old = previous.get(post_id)
            if old is None or not old.get("detailed_at"):
                fresh.append(post_id)
                continue
            if old.get("views") is None or (posted is not None and self.now - posted < recent):
                others.append(post_id)
        if len(within) < len(rows):
            logger.info("TikTok %s : détail relevé pour les %d posts les plus récents sur %d ([tiktok] stats_detail_max)",
                        self.account, len(within), len(rows))
        return set(fresh + others)

    def read_post(self, post_id: str, row: dict[str, Any]) -> dict[str, Any]:
        """Analyse d'un post : cartes de metriques lues par libelle, puis onglets Spectateurs et Engagement."""
        self.open_page(self.sel["urls"]["analytics"].format(post_id=post_id), self.sel["expect"]["analytics_url_prefix"])
        self.wait("metric_card", table="stats")
        self.guard()
        cards: dict[str, str] = {}
        for card in self.page.query_selector_all(self.sel["stats"]["metric_card"]):
            parts = [part.strip() for part in re.split(r"\s*\|\s*|\n+", str(card.inner_text())) if part.strip()]
            if len(parts) >= 2:
                cards.setdefault(_squash(parts[0]).casefold(), " ".join(parts[1:]))
        post: dict[str, Any] = dict(row)
        for key, field, parse in (("views", "views", parse_count), ("watch_total", "watch_total_s", parse_duration),
                                  ("watch_avg", "avg_watch_s", parse_duration), ("watched_full", "watched_full", parse_percent),
                                  ("new_followers", "new_followers", parse_count), ("retention", "retention", parse_percent)):
            value = cards.get(_squash(self.sel["metrics"][key]).casefold())
            read = self.read_value(value, key, parse, f"post {post_id}")
            post[field] = read if read is not None or field != "views" else row.get("views")
        post["retention_curve"] = self.read_retention_curve(post_id)
        post["fyf_eligible"], post["fyf_notice"] = self.read_fyf_notice(post_id, row)
        sources = self.page.query_selector(self.sel["stats"]["traffic_sources"])
        text = _squash(str(sources.inner_text())) if sources is not None else ""
        post["traffic_sources"] = None if not text or self.sel["stats"]["processing"].casefold() in text.casefold() else text
        # Spectateurs et Engagement restent vides (« en cours de traitement », « dès 100 vues ») sous le seuil :
        # les ouvrir coute deux attentes inutiles par post. Releves seulement a partir de stats_audience_min_views.
        views = post.get("views")
        if isinstance(views, (int, float)) and views >= int(self.settings["stats_audience_min_views"]):
            post["viewers"] = self.read_viewers(post_id)
            engagement = self.read_engagement(post_id)
        else:
            post["viewers"], engagement = None, None
        post["engagement"] = engagement
        post["shares"] = None if engagement is None else engagement.get("shares")
        post["detailed_at"] = self.now.isoformat()
        post["detail_not_read"] = False
        return post

    def read_fyf_notice(self, post_id: str, row: dict[str, Any]) -> tuple[bool | None, str | None]:
        """Bandeau « pas eligible au fil Pour toi » de l'analyse (repere par son texte) : ``(eligible, texte)``.
        Absent = eligible ; post programme pas encore en ligne = ``(None, None)`` (rien a dire, jamais devine)."""
        posted = _naive_utc(row.get("posted_at"))
        if posted is not None and posted > self.now:
            return None, None
        banner = self.page.query_selector(self.sel["stats"]["fyf_notice"])
        if banner is None:
            return True, None
        text = _squash(str(banner.inner_text()))
        if not text:
            raise self.reject("unexpected_page", f"bandeau de restriction vide (post {post_id})")
        return False, text

    def read_retention_curve(self, post_id: str) -> list[dict[str, Any]] | None:
        """Courbe de retention : un point par element ``retention_point`` (« instant | part encore presente »)."""
        points = []
        for number, element in enumerate(self.page.query_selector_all(self.sel["stats"]["retention_point"]), start=1):
            parts = [part.strip() for part in re.split(r"\s*\|\s*|\n+", str(element.inner_text())) if part.strip()]
            if len(parts) != 2:
                raise self.reject("unexpected_page", f"point {number} de la rétention illisible (post {post_id}) : "
                                                     f"{str(element.inner_text())!r}")
            where = f"post {post_id}, point {number} de la rétention"
            points.append({"t_s": self.read_value(parts[0], "t_s", parse_duration, where),
                           "share": self.read_value(parts[1], "share", parse_percent, where)})
        return points or None

    def read_cards(self, url_key: str, post_id: str, card_key: str) -> list[list[str]] | None:
        """Cartes d'un onglet du post, chacune en lignes de texte ; ``None`` quand TikTok dit que l'onglet n'a pas
        encore de chiffres (moins de 100 vues, en cours de traitement)."""
        self.open_page(self.sel["urls"][url_key].format(post_id=post_id), self.sel["expect"]["analytics_url_prefix"])
        if self.wait(card_key, table="stats", optional=True) is None:
            body = self.page.query_selector(self.sel["stats"]["page_text"])
            text = _squash(str(body.inner_text())).casefold() if body is not None else ""
            if any(marker.casefold() in text for marker in self.sel["stats"]["unavailable"]):
                return None
            raise self.stop("element_missing", f"élément attendu absent après {float(self.settings['action_timeout_s']):g} s : {card_key}")
        self.guard()
        cards = []
        for card in self.page.query_selector_all(self.sel["stats"][card_key]):
            lines = [_squash(part) for part in re.split(r"\s*\|\s*|\n+", str(card.inner_text())) if part.strip()]
            if lines:
                cards.append(lines)
        return cards

    def card(self, cards: list[list[str]], heading: str) -> list[str] | None:
        """Lignes d'une carte (sans son titre) ; ``None`` si absente, ou si elle dit n'avoir pas encore de chiffres."""
        wanted = _squash(heading).casefold()
        lines = next((c[1:] for c in cards if c[0].casefold() == wanted), None)
        if lines is None:
            return None
        text = " ".join(lines).casefold()
        return None if any(marker.casefold() in text for marker in self.sel["stats"]["unavailable"]) else lines

    def entries(self, lines: list[str], where: str) -> list[dict[str, Any]]:
        """Couples « libelle, valeur » d'une carte ; une valeur « x % » est une fraction, sinon un nombre. Une barre
        « types de spectateurs » affiche ses pourcentages PUIS ses libelles : « 63 %, 37 %, Nouveaux, Recurrents »."""
        if lines and _PERCENT.fullmatch(lines[0]):
            return self.bar_entries(lines, where)
        if len(lines) % 2:
            raise self.reject("unexpected_page", f"carte illisible ({where}) : couples libellé / valeur attendus, reçu {lines!r}")
        return [self.entry(label, value, where) for label, value in zip(lines[0::2], lines[1::2])]

    def entry(self, label: str, value: str, where: str) -> dict[str, Any]:
        if _BELOW_ONE.fullmatch(value):  # « <1% » : sous 1 %, valeur exacte inconnue, jamais devinee
            return {"label": label, "value": None}
        return {"label": label, "value": self.read_value(value, label, parse_percent if "%" in value else parse_count, where)}

    def bar_entries(self, lines: list[str], where: str) -> list[dict[str, Any]]:
        out, rest = [], list(lines)
        while rest:
            size = next((n for n, line in enumerate(rest) if not _PERCENT.fullmatch(line)), len(rest))
            values, labels = rest[:size], rest[size:2 * size]
            if size == 0 or len(labels) != size or any(_PERCENT.fullmatch(label) for label in labels):
                raise self.reject("unexpected_page", f"carte illisible ({where}) : pourcentages puis libellés attendus, reçu {lines!r}")
            out += [self.entry(label, value, where) for label, value in zip(labels, values)]
            rest = rest[2 * size:]
        return out

    def read_viewers(self, post_id: str) -> dict[str, Any] | None:
        cards = self.read_cards("analytics_viewers", post_id, "viewers_card")
        if cards is None:
            return None
        where, labels = f"post {post_id}, spectateurs", self.sel["viewers"]
        total = self.card(cards, labels["total"])
        out: dict[str, Any] = {"total": self.read_value(total[0], "total", parse_count, where) if total else None}
        for key in VIEWERS_SECTIONS:
            lines = self.card(cards, labels[key])
            out[key] = self.entries(lines, f"{where}, {key}") if lines else None
        return out

    def read_engagement(self, post_id: str) -> dict[str, Any] | None:
        cards = self.read_cards("analytics_engagement", post_id, "engagement_card")
        if cards is None:
            return None
        where, labels = f"post {post_id}, engagement", self.sel["engagement"]
        shares = self.card(cards, labels["shares"])
        out: dict[str, Any] = {"shares": self.read_value(shares[0], "shares", parse_count, where) if shares else None}
        for key in ("likes_over_time", "comment_words"):
            lines = self.card(cards, labels[key])
            out[key] = self.entries(lines, f"{where}, {key}") if lines else None
        return out

    # -- releve opportuniste (SPEC-86fe R4) : la page Publications est deja affichee pour autre chose
    def harvest(self, only_if_rows: bool = False) -> None:
        """Lit au passage ce que la page Publications affiche (posts, vues, likes, commentaires) et l'ajoute a
        l'historique : aucune navigation, aucune attente. Une seule fois par ouverture du navigateur ; ne leve
        jamais (la publication ou la verification en cours prime), l'echec est journalise."""
        if not self.harvesting or self._harvested or not str(self.page.url).startswith(self.sel["expect"]["stats_url_prefix"]):
            return
        if only_if_rows and not self.page.query_selector_all(self.sel["stats"]["row"]):
            return
        self._lenient = True
        try:
            rows = list(self.read_rows().values())
            if rows:
                append_snapshot(self.account, self.settings, {
                    "account": self.account, "fetched_at": self.now.isoformat(), "source": "tiktok_studio",
                    "origin": "opportunistic", "overview": None, "posts": rows})
                self._harvested = True
                logger.info("TikTok %s : %d post(s) relevés au passage sur la page Publications", self.account, len(rows))
        except Exception as exc:  # noqa: BLE001 - jamais un arret : dit dans le journal
            self._harvested = True
            logger.warning("TikTok %s : relevé au passage impossible : %s", self.account, exc)
        finally:
            self._lenient = False

    def result(self, clip: dict[str, Any], mode: str, schedule_at: datetime | None,
               effective: datetime | None = None, rounding: str | None = None) -> dict[str, Any]:
        notes = [rounding] if rounding else []
        when = (effective if rounding else schedule_at) if mode == "scheduled" else self.now
        url, found = self.find_post_link(clip, target=when if mode == "scheduled" else None)
        post_id = found if url is not None else None
        if url is None:
            notes.append("post programmé : son adresse publique n'existe pas encore" if mode == "scheduled"
                         else f"publication réussie mais lien du post introuvable sur la page Publications "
                              f"({found}) : à vérifier à la main")
        return {
            "post_url": url,
            "post_id": post_id,
            "state": "scheduled_on_tiktok" if mode == "scheduled" else "published",
            "publish_at": when.isoformat(),
            "note": " ; ".join(notes) or None,
        }


class BrowserBackend:
    def publish(self, clip: dict[str, Any], account: str, *, mode: str, schedule_at: datetime | None,
                settings: dict[str, Any], selectors: dict[str, Any], now: datetime, opener: Opener | None,
                sleep: Callable[[float], None], rng: Any, on_tick: Callable[[], None] | None,
                config: Config | None = None) -> dict[str, Any]:
        open_profile = opener or browser._open_context
        with open_profile(account, headless=False) as context:  # visible : jamais de navigateur cache (ADR-1a58)
            page = browser.fresh_page(context)
            flow = _Flow(page, account, selectors, settings, now=now, sleep=sleep, rng=rng, on_tick=on_tick,
                         harvest=True, config=config)
            try:
                return flow.run(clip, mode, schedule_at)
            except TikTokStop:
                raise
            except Exception as exc:  # noqa: BLE001 - erreur Playwright : arret sur avec capture
                raise flow.stop("unexpected_page", f"page inattendue : {type(exc).__name__} : {exc}") from exc

    def fetch_stats(self, account: str, previous: dict[str, dict[str, Any]], *, settings: dict[str, Any],
                    selectors: dict[str, Any], now: datetime, opener: Opener | None, sleep: Callable[[float], None],
                    rng: Any, on_tick: Callable[[], None] | None, full: bool = False,
                    config: Config | None = None) -> dict[str, Any]:
        open_profile = opener or browser._open_context
        with open_profile(account, headless=False) as context:
            page = browser.fresh_page(context)
            flow = _Flow(page, account, selectors, settings, now=now, sleep=sleep, rng=rng, on_tick=on_tick,
                         config=config)
            try:
                return flow.stats(previous, full=full)
            except TikTokStop:
                raise
            except Exception as exc:  # noqa: BLE001 - erreur Playwright : arret sur avec capture
                raise flow.stop("unexpected_page", f"page inattendue : {type(exc).__name__} : {exc}") from exc


class ApiBackend:
    def publish(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise TikTokError("backend api pas encore disponible : règle [tiktok] backend = \"browser\"")

    def fetch_stats(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise TikTokError("backend api pas encore disponible : statistiques seulement par le navigateur")


_BACKENDS = {"browser": BrowserBackend, "api": ApiBackend}


# ---------------------------------------------------------------- interface


def _default_opener(config: Config | None) -> Opener:
    """Profil ouvert par clipper.browser avec les reglages [browser] du config (pilot_wait_s), jamais les defauts."""
    return functools.partial(browser._open_context, config=config)


def publish(
    clip: dict[str, Any], account: str, *, mode: str | None = None, schedule_at: datetime | None = None,
    config: Config | None = None, now: datetime | None = None, selectors: dict[str, Any] | None = None,
    opener: Opener | None = None, sleep: Callable[[float], None] = time.sleep, rng: Any = None,
    on_tick: Callable[[], None] | None = None, options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publie ``clip`` ({video_path, caption, hashtags}) sur le compte, avec les reglages ``options`` de ce post
    (visibilite, commentaires, reutilisation, contenu IA, verification de contenu ; sinon ceux de [tiktok]), ``mode`` ``immediate``
    ou ``scheduled`` (date ``schedule_at``, programmation cote TikTok). Rend
    ``{post_url, post_id, state, publish_at, note}`` ; leve ``TikTokError`` (reglage, date,
    clip), ``TikTokStop`` (R4) ou ``BrowserError`` (Chrome/Playwright absent)."""
    settings = post_settings(get_settings(config), options)
    backend = _BACKENDS[settings["backend"]]()
    mode = mode if mode is not None else str(settings["publish_mode"])
    if not account:
        raise TikTokError("compte TikTok manquant : aucun compte n'a été choisi pour cette publication")
    if mode not in MODES:
        raise TikTokError(f"mode de publication invalide : {mode!r} (attendu : {' | '.join(MODES)})")
    if not Path(clip["video_path"]).is_file():
        raise TikTokError(f"mp4 introuvable : {clip['video_path']}")
    now = now or datetime.now(timezone.utc)
    if mode == "scheduled":
        if settings["visibility"] == "private":
            raise TikTokError("programmation refusée : TikTok ne programme pas une vidéo privée (« Les vidéos privées "
                              "ne peuvent pas être programmées ») : règle [tiktok] visibility = \"public\" ou publie "
                              "en mode immédiat")
        _check_schedule(schedule_at, settings, now)
    if isinstance(backend, ApiBackend):
        return backend.publish()
    return backend.publish(
        clip, browser.validate_account(account), mode=mode, schedule_at=schedule_at, settings=settings,
        selectors=selectors or load_selectors(), now=now, opener=opener or _default_opener(config), sleep=sleep,
        rng=rng or random.Random(), on_tick=on_tick, config=config,
    )


def _check_schedule(schedule_at: datetime | None, settings: dict[str, Any], now: datetime) -> None:
    if schedule_at is None or schedule_at.tzinfo is None:
        raise TikTokError("publication programmée : une date avec fuseau horaire est requise")
    days, minutes = settings["schedule_max_days"], settings["schedule_min_minutes"]
    if schedule_at > now + timedelta(days=days):
        raise TikTokError(
            f"programmation refusée : {schedule_at.isoformat()} dépasse la limite de {days} jours "
            f"de TikTok ([tiktok] schedule_max_days)"
        )
    if schedule_at < now + timedelta(minutes=minutes):
        raise TikTokError(
            f"programmation refusée : {schedule_at.isoformat()} est à moins de {minutes} minutes "
            f"(avance minimale de TikTok, [tiktok] schedule_min_minutes) : publie en mode immédiat"
        )


# ---------------------------------------------------------------- historique des releves (SPEC-86fe R2)

_ERROR_SUFFIX = ".error.json"


def _history_dir(account: str, settings: dict[str, Any]) -> Path:
    return Path(settings["stats_dir"]) / browser.validate_account(account)


def _stamp_name(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _append(account: str, settings: dict[str, Any], stamp: str, suffix: str, data: dict[str, Any]) -> Path:
    """Ecrit ``data`` dans un fichier neuf de l'historique ; un fichier existant n'est jamais ecrase : en cas
    d'horodatage identique, un numero est ajoute au nom."""
    folder = _history_dir(account, settings)
    base = _stamp_name(stamp)
    with channel_mod.file_lock(folder / "history"):
        number, path = 1, folder / f"{base}{suffix}"
        while path.exists():
            number += 1
            path = folder / f"{base}-{number}{suffix}"
        channel_mod.atomic_write_json(path, data)
    return path


def append_snapshot(account: str, settings: dict[str, Any], snapshot: dict[str, Any]) -> Path:
    """Ajoute un releve (complet ou opportuniste) a l'historique du compte, horodate ; jamais d'ecrasement."""
    return _append(account, settings, snapshot["fetched_at"], ".json", snapshot)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("objet JSON attendu")
        return data
    except (OSError, ValueError) as exc:
        raise TikTokError(f"relevé des statistiques TikTok illisible ({path}) : {exc}") from exc


def read_history(account: str, *, config: Config | None = None) -> list[dict[str, Any]]:
    """Tous les releves du compte (state/stats/tiktok/<compte>/), du plus ancien au plus recent ; liste vide
    sans releve ; un fichier illisible est une ``TikTokError`` (jamais ignore)."""
    folder = _history_dir(account, get_settings(config))
    if not folder.is_dir():
        return []
    snapshots = []
    for path in sorted(folder.glob("*.json")):
        if path.name.endswith(_ERROR_SUFFIX):
            continue
        data = _read_json(path)
        if not isinstance(data.get("posts"), list) or not isinstance(data.get("fetched_at"), str):
            raise TikTokError(f"relevé des statistiques TikTok illisible ({path}) : posts et fetched_at attendus")
        number = re.search(r"-(\d+)\.json$", path.name)  # horodatages identiques : l'ordre d'ajout
        snapshots.append(((datetime.fromisoformat(data["fetched_at"]), int(number[1]) if number else 1), data))
    return [data for _, data in sorted(snapshots, key=lambda item: item[0])]


def read_error(account: str, *, config: Config | None = None) -> dict[str, Any] | None:
    """Dernier arret du releve (R4) s'il est plus recent que le dernier releve complet, sinon ``None``."""
    folder = _history_dir(account, get_settings(config))
    errors = sorted(folder.glob(f"*{_ERROR_SUFFIX}")) if folder.is_dir() else []
    if not errors:
        return None
    error = _read_json(errors[-1])
    last = _last_full(read_history(account, config=config))
    if last is not None and datetime.fromisoformat(last["fetched_at"]) >= datetime.fromisoformat(error["at"]):
        return None
    return error


def _last_full(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((s for s in reversed(history) if s.get("origin") == "full"), None)


def _last_attempt(account: str, config: Config | None) -> datetime | None:
    """Date du dernier essai de releve complet, reussi ou arrete ; ``None`` s'il n'y en a jamais eu. Un releve
    opportuniste (page Publications vue au passage) n'est pas un essai de releve complet."""
    stamps = []
    last = _last_full(read_history(account, config=config))
    if last is not None:
        stamps.append(last["fetched_at"])
    folder = _history_dir(account, get_settings(config))
    for path in folder.glob(f"*{_ERROR_SUFFIX}") if folder.is_dir() else []:
        stamps.append(_read_json(path)["at"])
    return max(datetime.fromisoformat(s) for s in stamps) if stamps else None


def stats_due(account: str, *, config: Config | None = None, now: datetime | None = None) -> bool:
    """Releve periodique du worker (SPEC-47e2 R4) : jamais si ``stats_interval_h`` vaut 0 (coupe, defaut) ; sinon
    vrai si le dernier essai de releve complet (reussi ou arrete) date de ``stats_interval_h`` ou plus, ou s'il n'y
    en a jamais eu : un arret sur n'est pas retente a chaque passage du worker."""
    hours = float(get_settings(config)["stats_interval_h"])
    if hours == 0:
        return False
    last = _last_attempt(account, config)
    return last is None or (now or datetime.now(timezone.utc)) - last >= timedelta(hours=hours)


def stats_stale(account: str, *, config: Config | None = None, now: datetime | None = None) -> bool:
    """Ouverture de l'ecran Statistiques (SPEC-47e2 R4a) : vrai si le dernier essai de releve complet date de
    ``stats_stale_min`` minutes ou plus, ou s'il n'y en a jamais eu ; jamais si ``stats_stale_min`` vaut 0. Un arret
    sur compte comme un essai : le navigateur n'est pas rouvert a chaque ouverture de l'ecran."""
    minutes = float(get_settings(config)["stats_stale_min"])
    if minutes == 0:
        return False
    last = _last_attempt(account, config)
    return last is None or (now or datetime.now(timezone.utc)) - last >= timedelta(minutes=minutes)


def _record_stats_failure(account: str, exc: Exception, config: Config | None, settings: dict[str, Any],
                          now: datetime) -> None:
    """R4 : l'echec est ajoute a l'historique (le dernier releve reste), et signale a la console."""
    code = exc.code if isinstance(exc, TikTokStop) else "browser"
    capture = str(exc.capture) if isinstance(exc, TikTokStop) and exc.capture else None
    reason = exc.reason if isinstance(exc, TikTokStop) else str(exc)
    _append(account, settings, now.isoformat(), _ERROR_SUFFIX,
            {"at": now.isoformat(), "code": code, "reason": reason, "capture": capture})
    emit_event({"level": "error", "account": account, "channel": None, "video_id": None, "clip_id": None,
                "reason": f"relevé des statistiques : {reason}", "capture": capture}, config=config, now=now)


def merged_posts(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Etat le plus recent de chaque post : les releves sont rejoues du plus ancien au plus recent, une cle presente
    dans un releve (meme ``None`` : « TikTok ne l'affiche pas ») remplace l'ancienne, une cle absente (releve
    opportuniste, sans detail) laisse l'ancienne valeur. ``first_seen`` / ``last_seen`` : dates de releve."""
    merged: dict[str, dict[str, Any]] = {}
    for snapshot in history:
        for post in snapshot["posts"]:
            known = merged.setdefault(post["post_id"], {"first_seen": snapshot["fetched_at"]})
            known.update(post)
            known["last_seen"] = snapshot["fetched_at"]
    return merged


def deleted_post_ids(history: list[dict[str, Any]]) -> set[str]:
    """Posts supprimes sur TikTok : releves AVANT le DERNIER releve complet (la page Publications defilee en
    entier) et absents de ce releve. Un post vu seulement apres le dernier complet (programme par Clipper, page
    opportuniste) n'est jamais declare supprime. Un releve opportuniste ne voit qu'une partie de la liste : il ne
    supprime rien ; sans releve complet, rien n'est declare supprime. L'historique n'est jamais modifie (SPEC-47e2 R2)."""
    last = _last_full(history)
    if last is None:
        return set()
    last_at = datetime.fromisoformat(last["fetched_at"])
    shown = {post["post_id"] for post in last["posts"]}
    seen_before = {post["post_id"] for snapshot in history if datetime.fromisoformat(snapshot["fetched_at"]) <= last_at
                   for post in snapshot["posts"]}
    return seen_before - shown


def _published_posts(account: str, config: Config | None) -> list[dict[str, Any]]:
    """Posts publies de ``account`` connus par les sidecars de clips (``tiktok_post``, ecrit a la
    publication) : ce qui relie un post releve a son clip, par l'id ou l'adresse du post (R5)."""
    root = Path(config.output_dir if config is not None else "output")
    found = []
    for path in sorted(root.glob("*/*.json")) if root.is_dir() else []:
        try:
            sidecar = json.loads(path.read_text(encoding="utf-8"))
            post = sidecar.get("tiktok_post") if isinstance(sidecar, dict) else None
            if not isinstance(post, dict) or post.get("account") != account:
                continue
            post_id = post.get("id") or (_POST_ID.search(post["url"]).group(1) if post.get("url") else None)
            if post_id:
                found.append({"video_id": path.parent.name, "clip_id": path.stem, "post_id": str(post_id)})
        except (OSError, ValueError, AttributeError) as exc:
            raise TikTokError(f"sidecar illisible pour relier les statistiques ({path}) : {exc}") from exc
    return found


def fetch_stats(
    account: str, *, config: Config | None = None, now: datetime | None = None,
    selectors: dict[str, Any] | None = None, opener: Opener | None = None,
    sleep: Callable[[float], None] = time.sleep, rng: Any = None, on_tick: Callable[[], None] | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Releve complet de ``account`` dans TikTok Studio (SPEC-86fe R1, lecture seule) : page Donnees analytiques
    (7, 28, 60 et 365 jours), liste des Publications (tous les posts du compte, publies ou non par Clipper), puis
    l'analyse, les spectateurs et l'engagement des posts a lire en detail (les ``stats_detail_max`` plus recents,
    tous si ``full``). Ajoute le releve horodate a
    l'historique ``<stats_dir>/<compte>/`` (jamais d'ecrasement) et le rend. Une valeur absente de la page ou
    « en cours de traitement » est ``None``. Leve ``TikTokStop`` (R4 : l'echec est aussi ajoute a l'historique
    et signale a la console), ``BrowserError`` ou ``TikTokError``. Le compte doit etre pret a publier : c'est a
    l'appelant (console, worker) de le verifier."""
    settings = get_settings(config)
    backend = _BACKENDS[settings["backend"]]()
    if not account:
        raise TikTokError("compte TikTok manquant : aucun relevé de statistiques sans compte")
    if isinstance(backend, ApiBackend):
        return backend.fetch_stats()
    account = browser.validate_account(account)
    now = now or datetime.now(timezone.utc)
    previous = merged_posts(read_history(account, config=config))
    try:
        data = backend.fetch_stats(
            account, previous, settings=settings, selectors=selectors or load_selectors(), now=now,
            opener=opener or _default_opener(config),
            sleep=sleep, rng=rng or random.Random(), on_tick=on_tick, full=full, config=config)
    except (TikTokStop, browser.BrowserError) as exc:
        _record_stats_failure(account, exc, config, settings, now)
        raise
    snapshot = {"account": account, "fetched_at": now.isoformat(), "source": "tiktok_studio", "origin": "full",
                "overview": data["overview"], "posts": data["posts"]}
    append_snapshot(account, settings, snapshot)
    return snapshot


# ---------------------------------------------------------------- agregats pour la console (SPEC-86fe R2, R3, R5)

VIDEO_SORTS = ("posted_at", "caption", "views", "likes", "comments", "shares", "avg_watch_s", "watched_full")
_LIGHT_FIELDS = ("post_id", "post_url", "caption", "posted_at", "posted_at_text", "visibility", "views", "likes",
                 "comments", "shares", "avg_watch_s", "watched_full", "fyf_eligible", "fyf_notice", "detail_not_read")


def _day(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(timezone.utc).date()


def account_overview(account: str, period: int, *, config: Config | None = None) -> dict[str, Any]:
    """Vue d'ensemble du compte pour ``period`` jours (7, 28, 60 ou 365), calculee sur l'historique : les tuiles du dernier
    releve complet (valeur, evolution donnee par TikTok ``change_pct``, evolution recalculee depuis l'historique
    ``history_change_pct`` = valeur du releve d'il y a ``period`` jours) et, par tuile, la courbe par jour : un point
    par jour (dernier releve complet du jour, la valeur de la tuile de la periode), vide un jour sans releve, et la
    meme courbe decalee de ``period`` jours (periode precedente). Rien n'est invente : un jour sans releve, une tuile
    que TikTok n'affichait pas ou une base nulle donnent ``None``."""
    if period not in PERIODS:
        raise TikTokError(f"période invalide : {period!r} (attendu : {' | '.join(str(p) for p in PERIODS)} jours)")
    history = read_history(account, config=config)
    last = _last_full(history)
    out: dict[str, Any] = {
        "account": account, "period": period, "snapshots": len(history),
        "fetched_at": history[-1]["fetched_at"] if history else None,
        "last_full_at": last["fetched_at"] if last else None, "error": read_error(account, config=config),
        "tiles": None, "series": None}
    if last is None:
        return out
    by_day: dict[str, dict[date, Any]] = {key: {} for key in TILES}
    for snapshot in history:
        if snapshot.get("origin") != "full":
            continue
        for key in TILES:
            by_day[key][_day(snapshot["fetched_at"])] = ((snapshot["overview"] or {}).get(str(period)) or {}).get(key) or {}
    end = _day(last["fetched_at"])
    days = [end - timedelta(days=period - 1 - i) for i in range(period)]
    gap = timedelta(days=period)

    def value(key: str, day: date) -> Any:
        return by_day[key].get(day, {}).get("value")

    tiles, series = {}, {}
    for key in TILES:
        shown = by_day[key][end]
        current, before = shown.get("value"), value(key, end - gap)
        tiles[key] = {"value": current, "change_pct": shown.get("change_pct"),
                      "history_change_pct": None if current is None or not before else (current - before) / before * 100}
        series[key] = {"labels": [d.isoformat() for d in days], "values": [value(key, d) for d in days],
                       "previous": [value(key, d - gap) for d in days]}
    out.update(tiles=tiles, series=series)
    return out


def _clip_links(account: str, config: Config | None) -> dict[str, dict[str, str]]:
    links: dict[str, dict[str, str]] = {}
    for found in _published_posts(account, config):
        links.setdefault(found["post_id"], {"video_id": found["video_id"], "clip_id": found["clip_id"]})
    return links


def list_videos(account: str, *, sort: str = "posted_at", descending: bool = True, query: str = "",
                config: Config | None = None) -> list[dict[str, Any]]:
    """Toutes les videos du compte vues dans TikTok Studio (dernier etat connu de chacune), triees par ``sort``
    (une valeur absente est toujours en dernier) et filtrees par ``query`` (partie de la legende, sans la casse).
    Chaque video porte son clip Clipper (``clip`` : video_id et clip_id enregistres a la publication) ou
    ``outside_clipper`` : publiee hors de Clipper (R5)."""
    if sort not in VIDEO_SORTS:
        raise TikTokError(f"tri inconnu : {sort!r} (attendu : {' | '.join(VIDEO_SORTS)})")
    links = _clip_links(account, config)
    wanted = query.strip().casefold()
    history = read_history(account, config=config)
    deleted = deleted_post_ids(history)
    videos = []
    for post in merged_posts(history).values():
        if post["post_id"] in deleted:
            continue  # supprime sur TikTok : absent du dernier releve complet
        if wanted and wanted not in (post.get("caption") or "").casefold():
            continue
        light = {key: post.get(key) for key in _LIGHT_FIELDS}
        clip = links.get(post["post_id"])
        videos.append({**light, "processing": post.get("views") is None, "clip": clip, "outside_clipper": clip is None})

    def key(video: dict[str, Any]) -> Any:
        value = video[sort]
        return value.casefold() if isinstance(value, str) else value

    present = sorted((v for v in videos if v[sort] is not None), key=key, reverse=descending)
    return present + [v for v in videos if v[sort] is None]


def video_detail(account: str, post_id: str, *, config: Config | None = None) -> dict[str, Any]:
    """Fiche d'une video : tout ce que le releve sait d'elle (chiffres, retention, spectateurs, engagement), son
    clip Clipper ou ``outside_clipper``, et son historique (vues, likes, commentaires a chaque releve)."""
    history = read_history(account, config=config)
    post = merged_posts(history).get(str(post_id))
    if post is None:
        raise TikTokError(f"vidéo {post_id} introuvable dans les relevés du compte {account}")
    if str(post_id) in deleted_post_ids(history):
        raise TikTokError(f"vidéo {post_id} introuvable : supprimée de TikTok (absente du dernier relevé du compte {account})")
    clip = _clip_links(account, config).get(post["post_id"])
    track = [{"fetched_at": s["fetched_at"], **{k: p.get(k) for k in ("views", "likes", "comments")}}
             for s in history for p in s["posts"] if p["post_id"] == post["post_id"]]
    return {**post, "processing": post.get("views") is None, "clip": clip, "outside_clipper": clip is None,
            "history": track}
