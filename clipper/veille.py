"""Veille des sujets chauds : relevé quotidien (ADR-ca9a, SPEC-bdd9 R1, R2, R4, R5).

Bibliothèque, pas une étape (ADR-b16b) : rien sous ``workspace/<video_id>/``,
aucun import de ``clipper.web`` ni d'une étape. Tout l'état est en JSON sous
``state/veille/`` (écriture atomique sous verrou) :

    history/<date>.json   relevé du jour (viewers Twitch, joueurs Steam, YouTube)
    days/<date>.json      sources, jeux, candidats, exclus, llm, propositions
    seen.json             VOD déjà mises en file ou ignorées
    selection/<date>.json meilleurs clips du jour (gardés / archivés / restaurés)
    refresh.json          demande de relevé immédiat (déposée par l'interface)

Les collecteurs sont injectés : ``collectors = {"twitch", "youtube", "steam"}``,
chacun un callable ``collector(settings) -> dict`` (``settings`` = la table
``[veille]`` fusionnée). Contrat de retour :

    twitch  {"games": [{"name", "viewers_fr"}],
             "vods":  [VOD]}
    youtube {"videos": [VOD]}
    steam   {"games": [{"appid", "name", "players" (pic du jour), "concurrent" (instantané | None), "rank",
             "last_week_rank"}],
             "unnamed": [{"appid", "reason"}]}  (optionnel : jeux sans nom, avec leur raison)
    steam_players   (settings, appids) -> {"players": {appid: int | None}, "skipped": n}  (SPEC-df51 R18, appelé après les autres)
    steam_followers (settings, appids) -> {"followers": {appid: int | None}, "skipped": n}  (SPEC-df51 R21)
    steam_reviews   (settings, appids) -> {"histograms": {appid: [{"date", "value", "up", "down"}] | None}, "skipped": n,
                    "rate_limited": n}  (SPEC-85a0 R24, jeux suivis, après le test d'accès)
    twitch_vods_30d (settings, game_ids) -> {"vods": {game_id: {"since", "points": [{"date", "vods", "views"}]} | None},
                    "skipped": n, "rate_limited": n}  (SPEC-85a0 R25 ; ``twitch`` rend en plus ``twitch_id`` par jeu)
    steam_fr {"games": [{"appid", "name", "rank", "last_week_rank"}]}  (top des ventes du pays ``region`` ;
             ``last_week_rank`` 0 = absent du top la semaine dernière)
    igdb    {"games": [{"igdb_id", "name", "slug", "url", "hypes" (entier), "first_release_date" | None,
             "cover_image_id" | None, "steam_appid" | None, "release_dates": [{"date" (YYYY-MM-DD, jour UTC), "ts",
             "human", "platform", "region", "status", "date_format"}]}], "skipped_rows": n}  (SPEC-df51 R12 ;
             un jeu Twitch porte en plus ``igdb_id``, chaîne vide si Twitch ne l'a pas)

avec ``VOD = {video_id, url, title, channel_name, game_name | None,
duration_s, published_at (ISO 8601), view_count, views_per_hour}``. Un direct
en cours n'est pas une VOD : le collecteur ne le rend pas. Un collecteur qui
lève, ou dont la réponse est inexploitable, met sa source en ``error`` ; les
autres continuent (ADR-ad2e : aucun chiffre inventé, une donnée absente est
``null``). Sans collecteurs injectés, ``clipper.veille_sources`` fournit les réels.

Un relevé tourne par voies parallèles, une par hôte, en trois phases séparées par une barrière (SPEC-85a0 R29), sous
l'échéance globale ``veille_deadline_s``. Un collecteur dont la signature accepte ``deadline`` (argument nommé,
callable rendant les secondes restantes) la reçoit et s'arrête avant sa prochaine requête, pause ou essai : il rend
alors en plus ``"deadline_stopped": k`` (éléments non relevés, clé absente sinon) et ``collect`` marque sa source
``partial``. Un collecteur sans ``deadline`` est appelé comme avant.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clipper import channel as channel_mod
from clipper import llm, publish, veille_sources
from clipper.config import Config, load_config
from clipper.workspace import DownloadError, extract_video_id

CONFIG_DEFAULTS: dict[str, object] = {
    "enabled": False,
    "run_at": "07:00",
    "timezone": "Europe/Paris",
    "language": "fr",
    "region": "FR",
    "taste": "",
    "max_vods_per_day": 3,
    "best_clips_per_day": 3,
    "baseline_days": 7,
    "history_days": 90,
    "rise_min_pct": 50,
    "vod_min_duration_s": 1800,
    "vod_max_age_h": 36,
    "twitch_top_games": 20,
    "twitch_vods_per_game": 10,
    "youtube_max_results": 50,
    "youtube_min_duration_s": 600,
    "steam_top": 100,
    "steam_name_lookups_max": 100,
    "twitch_access_attempts": 5,
    "twitch_access_retry_pause_s": 3.0,
    "twitch_access_workers": 4,  # jeux testés en même temps ; au plus une VOD par jeu à la fois, mêmes essais par VOD
    "steam_sellers_top": 50,
    "twitch_client_id": "",
    "twitch_client_secret": "",
    "youtube_api_key": "",
    "state_dir": "state/veille",
    "http_timeout_s": 20,
    "steam_rank_gain_min": 5,
    "steam_risers_max": 10,
    "upcoming_days": 14,
    "release_window_days": 15,
    "igdb_min_hypes": 5,
    "igdb_recent_max": 12,
    "igdb_upcoming_max": 20,
    "igdb_pages_max": 4,
    "youtube_game_min_chars": 5,
    "steam_players_lookups_max": 30,
    "steam_followers_lookups_max": 50,
    "steam_followers_pause_s": 3.0,
    "steam_followers_retry_max": 3,
    "steam_followers_retry_wait_max_s": 60.0,
    "community_min_steam_players": 1000,
    "community_min_steam_followers": 10000,
    "community_min_twitch_viewers": 200,
    "community_min_hypes": 50,
    "max_vods_per_game": 1,
    "trend_days": 30,
    "trend_games_max": 40,
    "steam_reviews_pause_s": 2.0,
    "steam_reviews_retry_max": 3,
    "steam_reviews_retry_wait_max_s": 60.0,
    "twitch_history_pages_max": 5,
    "twitch_history_retry_max": 2,
    "twitch_history_retry_wait_max_s": 60.0,
    "veille_deadline_s": 480,
    "llm_retry_delay_min": 30,  # limite de session (429) pendant le choix : on réessaie le choix seul après ce délai
    "llm_retry_max": 3,  # nombre de reprises du choix avant l'échec explicite
}

# Clés retirées qu'un config.toml peut encore porter : ignorées à la lecture (clipper.config).
LEGACY_KEYS = (
    "igdb_releases_max",  # SPEC-4efa, remplacée par igdb_recent_max / igdb_upcoming_max (SPEC-df51 R11)
    "twitch_access_check_max",  # SPEC-85a0 R26 : le test d'accès n'a plus de plafond, il s'arrête par jeu
)

log = logging.getLogger(__name__)

SOURCES = ("twitch", "youtube", "steam", "steam_fr", "igdb", "steam_players", "steam_followers", "steam_reviews",
           "twitch_vods_30d")
LOOKUP_SOURCES = ("steam_players", "steam_followers", "steam_reviews", "twitch_vods_30d")  # appelés après les autres
_APPID_SOURCES = ("steam_players", "steam_followers")  # par appid, avant le filtre de communauté (R18, R21)
Collector = Callable[[dict[str, object]], dict[str, Any]]
_NOT_STARTED = "échéance atteinte avant le début"  # erreur d'une source dont la phase n'avait pas commencé (R29)
_UNITS = {"twitch": "requête(s)", "youtube": "requête(s)", "steam": "jeu(x)", "steam_fr": "requête(s)",
          "igdb": "page(s)", "steam_players": "appid(s)", "steam_followers": "appid(s)", "steam_reviews": "appid(s)",
          "twitch_vods_30d": "jeu(x)"}  # ce que ``deadline_stopped`` compte, par source

# Clés exigées par source (steam n'en demande aucune).
_REQUIRED_KEYS = {
    "twitch": ("twitch_client_id", "twitch_client_secret"),
    "youtube": ("youtube_api_key",),
    "steam": (),
    "steam_fr": (),
    "igdb": ("twitch_client_id", "twitch_client_secret"),  # même jeton d'app que Twitch (ADR-798c)
    "steam_players": (),
    "steam_followers": (),
    "steam_reviews": (),
    "twitch_vods_30d": ("twitch_client_id", "twitch_client_secret"),
}
_RUN_AT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATE_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


class VeilleError(Exception):
    """Réglage invalide ou fichier d'état illisible."""


# --------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------


def settings(config: Config) -> dict[str, object]:
    """Table ``[veille]`` validée (R1) ; ``VeilleError`` nomme la clé fautive."""
    table = config.section("veille")
    for key in ("max_vods_per_day", "best_clips_per_day", "baseline_days"):
        value = table[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise VeilleError(f"[veille] {key} doit être un entier >= 1 (reçu {value!r})")
    for key, minimum in (("upcoming_days", 1), ("release_window_days", 0), ("igdb_min_hypes", 1),
                         ("igdb_recent_max", 1), ("igdb_upcoming_max", 1), ("igdb_pages_max", 1)):
        value = table[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise VeilleError(f"[veille] {key} doit être un entier >= {minimum} (reçu {value!r})")
    for key, minimum in (("steam_players_lookups_max", 0), ("trend_games_max", 0), ("community_min_steam_players", 0), ("community_min_steam_followers", 0),
                         ("community_min_twitch_viewers", 0), ("community_min_hypes", 0), ("max_vods_per_game", 1)):
        value = table[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise VeilleError(f"[veille] {key} doit être un entier >= {minimum} (reçu {value!r})")
    for key, low, high in (("steam_followers_lookups_max", 0, 60), ("steam_followers_retry_max", 0, 10),
                           ("trend_days", 7, 90), ("steam_reviews_retry_max", 0, 10), ("twitch_history_pages_max", 1, 5),
                           ("twitch_history_retry_max", 0, 10), ("twitch_access_attempts", 1, 10),
                           ("twitch_access_workers", 1, 16), ("veille_deadline_s", 60, 3600), ("llm_retry_delay_min", 1, 1440),
                           ("llm_retry_max", 0, 20)):
        value = table[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise VeilleError(f"[veille] {key} doit être un entier entre {low} et {high} (reçu {value!r})")
    for key, low, high in (("steam_followers_pause_s", 0.2, 60.0), ("steam_followers_retry_wait_max_s", 1.0, 600.0),
                           ("steam_reviews_pause_s", 0.2, 60.0), ("steam_reviews_retry_wait_max_s", 1.0, 600.0),
                           ("twitch_history_retry_wait_max_s", 1.0, 600.0), ("twitch_access_retry_pause_s", 0.0, 60.0)):
        value = table[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not low <= value <= high:
            raise VeilleError(f"[veille] {key} doit être un nombre entre {low} et {high} (reçu {value!r})")
    for key, low, high in (("youtube_max_results", 1, 50), ("twitch_top_games", 1, 100), ("twitch_vods_per_game", 1, 100)):
        value = table[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise VeilleError(f"[veille] {key} doit être un entier entre {low} et {high} (reçu {value!r})")
    for key in ("steam_top", "steam_sellers_top"):
        value = table[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise VeilleError(f"[veille] {key} doit être un entier >= 1 (reçu {value!r})")
    for key in ("vod_min_duration_s", "youtube_min_duration_s", "steam_name_lookups_max", "rise_min_pct"):
        value = table[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise VeilleError(f"[veille] {key} doit être un entier >= 0 (reçu {value!r})")
    for key, low, high in (("vod_max_age_h", 0.0, 8760.0), ("http_timeout_s", 1.0, 300.0)):
        value = table[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not low < value <= high:
            raise VeilleError(f"[veille] {key} doit être un nombre entre {low} (exclu) et {high} (reçu {value!r})")
    min_chars = table["youtube_game_min_chars"]
    if not isinstance(min_chars, int) or isinstance(min_chars, bool) or min_chars < 1:
        raise VeilleError(f"[veille] youtube_game_min_chars doit être un entier >= 1 (reçu {min_chars!r})")
    history_days = table["history_days"]
    if not isinstance(history_days, int) or isinstance(history_days, bool) or history_days < table["baseline_days"]:
        raise VeilleError(
            f"[veille] history_days doit être un entier >= baseline_days={table['baseline_days']} (reçu {history_days!r})")
    if history_days < table["trend_days"]:  # type: ignore[operator]
        raise VeilleError(
            f"[veille] history_days doit être >= trend_days={table['trend_days']} (reçu {history_days!r}) : "
            "l'historique gardé doit couvrir la courbe de tendance")
    run_at = table["run_at"]
    if not isinstance(run_at, str) or not _RUN_AT.match(run_at):
        raise VeilleError(f"[veille] run_at doit être au format HH:MM (reçu {run_at!r})")
    try:
        ZoneInfo(str(table["timezone"]))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise VeilleError(f"[veille] timezone inconnu : {table['timezone']!r}") from exc
    return table


def _state_dir(table: dict[str, object]) -> Path:
    return Path(str(table["state_dir"]))


# --------------------------------------------------------------------------
# Fichiers
# --------------------------------------------------------------------------


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise VeilleError(f"fichier d'état de la veille illisible : {path.name} ({exc})") from exc


def _write(path: Path, data: Any) -> None:
    with channel_mod.file_lock(path):
        channel_mod.atomic_write_json(path, data)


def _seen_video_ids(sdir: Path) -> set[str]:
    seen = _read_json(sdir / "seen.json", {"queued": [], "ignored": []})
    try:
        return {e["video_id"] for key in ("queued", "ignored") for e in seen.get(key, [])}
    except (AttributeError, KeyError, TypeError) as exc:
        raise VeilleError(f"fichier d'état de la veille illisible : seen.json ({exc})") from exc


def _queue_video_ids(config: Config) -> set[str]:
    path = Path(str(config.section("worker")["queue_path"]))
    entries = _read_json(path, [])
    try:
        return {e["video_id"] for e in entries}
    except (KeyError, TypeError) as exc:
        raise VeilleError(f"file d'attente illisible : {path.name} ({exc})") from exc


def _worker_video_id(vod: dict[str, Any]) -> str:
    """Id que le worker donne à cette VOD (``workspace.extract_video_id`` : Twitch 2893407960 -> v2893407960) ;
    la file et ``seen.json`` le portent. Vide si l'URL n'est pas reconnue : seul l'id source compte alors."""
    # Règle d'id partagée avec le worker (workspace : module sans étape, ADR-ca9a).
    try:
        return extract_video_id(str(vod["url"]))
    except DownloadError:
        return ""


def _load_history(sdir: Path, today: date, baseline_days: int) -> list[dict[str, Any]]:
    """Les ``baseline_days`` relevés précédents les plus récents (plus ancien d'abord)."""
    folder = sdir / "history"
    if not folder.exists():
        return []
    previous = sorted(p for p in folder.iterdir() if _DATE_FILE.match(p.name) and p.stem < today.isoformat())
    return [_read_json(p, None) for p in previous[-baseline_days:]]


def _prune_history(sdir: Path, today: date, history_days: int) -> None:
    folder = sdir / "history"
    if not folder.exists():
        return
    limit = (today - timedelta(days=history_days)).isoformat()
    for path in folder.iterdir():
        if _DATE_FILE.match(path.name) and path.stem < limit:
            path.unlink()


# --------------------------------------------------------------------------
# Jeux et montée (R4)
# --------------------------------------------------------------------------


def normalize(name: str) -> str:
    """Clé de jeu : minuscules, sans accents ni ponctuation, espaces réduits."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _delta(today: float | None, avg_values: list[float]) -> tuple[float | None, int | None]:
    """(moyenne, delta en %) ; delta null avec moins de 2 relevés ou une moyenne nulle."""
    if today is None or len(avg_values) < 2:
        return _mean(avg_values), None
    avg = _mean(avg_values)
    if not avg:
        return avg, None
    return avg, round((today - avg) / avg * 100)


def _rank_signal(info: dict[str, Any] | None) -> tuple[int | None, bool | None]:
    """Montée immédiate Steam : ``(gain de rang vs semaine dernière, nouveau dans le top)``.
    ``last_week_rank`` 0 = absent du top la semaine dernière (nouveau, pas de gain chiffré) ;
    rang inconnu (champ absent, jeu non relevé) = ``(None, None)``, jamais inventé."""
    if not info or not isinstance(info.get("rank"), int) or not isinstance(info.get("last_week_rank"), int):
        return None, None
    if info["last_week_rank"] <= 0:
        return None, True
    return info["last_week_rank"] - info["rank"], False


def _sellers_signal(info: dict[str, Any] | None) -> dict[str, Any]:
    """Champs « ventes du pays » d'un jeu : rang, rang de la semaine dernière, gain de places, nouveau dans le top.
    ``last_week_rank`` absent ou 0 = nouveau (pas de gain chiffré) ; jeu hors du top ventes = tout à ``None``."""
    if not info:
        return {"steam_sellers_rank": None, "steam_sellers_last_week_rank": None,
                "steam_sellers_gain": None, "steam_sellers_new": None}
    last = info.get("last_week_rank")
    new = not isinstance(last, int) or last <= 0
    return {"steam_sellers_rank": info["rank"], "steam_sellers_last_week_rank": None if new else last,
            "steam_sellers_gain": None if new else last - info["rank"], "steam_sellers_new": new}


def _sellers_risers(
    sellers: dict[str, dict[str, Any]],
    known_keys: set[str],
    youtube: dict[str, dict[str, Any]],
    vod_counts: dict[str, int],
    previous: list[dict[str, Any]],
    table: dict[str, object],
) -> list[dict[str, Any]]:
    """Jeux qui montent dans le top ventes du pays (nouveau, ou gain >= ``steam_rank_gain_min``) et pas
    encore dans la liste (Twitch, Steam mondial) : champs Twitch/joueurs à null, jamais inventés."""
    gain_min = int(table["steam_rank_gain_min"])  # type: ignore[call-overload]
    risers: list[dict[str, Any]] = []
    for appid, info in sellers.items():
        key = normalize(info["name"])
        signal = _sellers_signal(info)
        if key in known_keys or not (signal["steam_sellers_new"] or signal["steam_sellers_gain"] >= gain_min):
            continue
        risers.append({
            "key": key, "name": info["name"], "source": "steam_fr", "twitch_match": False,
            "twitch_fr_viewers": None, "twitch_avg": None, "twitch_delta_pct": None,
            "steam_appid": appid, "steam_match": False, "steam_players": None,
            "steam_avg": None, "steam_delta_pct": None,
            "steam_rank": None, "steam_rank_gain": None, "steam_new_in_top": None, **signal,
            "youtube_views_per_hour": youtube.get(key, {}).get("views_per_hour_sum"),
            "vod_count": vod_counts.get(key, 0),
            "baseline_days_available": len(previous),
        })
    risers.sort(key=lambda g: (not g["steam_sellers_new"], g["steam_sellers_rank"] if g["steam_sellers_new"] else -g["steam_sellers_gain"]))
    return risers[: int(table["steam_risers_max"])]  # type: ignore[call-overload]


def _build_games(
    twitch: dict[str, dict[str, Any]],
    steam: dict[str, dict[str, Any]],
    youtube: dict[str, dict[str, Any]],
    vod_counts: dict[str, int],
    previous: list[dict[str, Any]],
    table: dict[str, object] | None = None,
    sellers: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    steam_by_key = {normalize(info["name"]): (appid, info) for appid, info in steam.items()}
    games: list[dict[str, Any]] = []
    for key, info in twitch.items():
        appid, steam_info = steam_by_key.get(key, (None, None))
        twitch_avg, twitch_delta = _delta(
            info["viewers_fr"], [p["twitch"][key]["viewers_fr"] for p in previous if key in p.get("twitch", {})])
        steam_values = [p["steam"][appid]["players"] for p in previous if appid and appid in p.get("steam", {})]
        steam_players = steam_info["players"] if steam_info else None
        steam_avg, steam_delta = _delta(steam_players, steam_values) if steam_info else (None, None)
        rank_gain, new_in_top = _rank_signal(steam_info)
        games.append({
            "key": key, "name": info["name"], "source": "twitch", "twitch_match": True,
            "twitch_fr_viewers": info["viewers_fr"], "twitch_avg": twitch_avg, "twitch_delta_pct": twitch_delta,
            "steam_appid": appid, "steam_match": steam_info is not None, "steam_players": steam_players,
            "steam_avg": steam_avg, "steam_delta_pct": steam_delta,
            "steam_rank": steam_info.get("rank") if steam_info else None,
            "steam_rank_gain": rank_gain, "steam_new_in_top": new_in_top,
            "youtube_views_per_hour": youtube.get(key, {}).get("views_per_hour_sum"),
            "vod_count": vod_counts.get(key, 0),
            "baseline_days_available": len(previous),
        })
    if table is not None:
        games.extend(_steam_risers(steam, set(twitch), youtube, vod_counts, previous, table))
    sellers_by_key = {normalize(info["name"]): info for info in (sellers or {}).values()}
    for game in games:  # fusion par clé normalisée : un jeu déjà présent porte les champs, jamais de doublon
        game.update(_sellers_signal(sellers_by_key.get(game["key"])))
    if table is not None and sellers:
        games.extend(_sellers_risers(sellers, {g["key"] for g in games}, youtube, vod_counts, previous, table))
    return games


def _steam_risers(
    steam: dict[str, dict[str, Any]],
    twitch_keys: set[str],
    youtube: dict[str, dict[str, Any]],
    vod_counts: dict[str, int],
    previous: list[dict[str, Any]],
    table: dict[str, object],
) -> list[dict[str, Any]]:
    """Jeux Steam qui montent sans être déjà dans la liste Twitch : nouveau dans le top, ou gain de rang
    >= ``steam_rank_gain_min``. Champs Twitch à null (jamais inventés), plafonné à ``steam_risers_max``.
    Rang ou semaine dernière inconnus : le jeu n'est pas retenu."""
    gain_min = int(table["steam_rank_gain_min"])  # type: ignore[call-overload]
    risers: list[dict[str, Any]] = []
    for appid, info in steam.items():
        key = normalize(info["name"])
        if key in twitch_keys:
            continue
        rank_gain, new_in_top = _rank_signal(info)
        if not (new_in_top or (rank_gain is not None and rank_gain >= gain_min)):
            continue
        steam_avg, steam_delta = _delta(
            info["players"], [p["steam"][appid]["players"] for p in previous if appid in p.get("steam", {})])
        risers.append({
            "key": key, "name": info["name"], "source": "steam", "twitch_match": False,
            "twitch_fr_viewers": None, "twitch_avg": None, "twitch_delta_pct": None,
            "steam_appid": appid, "steam_match": True, "steam_players": info["players"],
            "steam_avg": steam_avg, "steam_delta_pct": steam_delta,
            "steam_rank": info["rank"], "steam_rank_gain": rank_gain, "steam_new_in_top": new_in_top,
            "youtube_views_per_hour": youtube.get(key, {}).get("views_per_hour_sum"),
            "vod_count": vod_counts.get(key, 0),
            "baseline_days_available": len(previous),
        })
    # nouveaux dans le top d'abord (meilleur rang en tête), puis plus gros gains
    risers.sort(key=lambda g: (not g["steam_new_in_top"], g["steam_rank"] if g["steam_new_in_top"] else -g["steam_rank_gain"]))
    return risers[: int(table["steam_risers_max"])]  # type: ignore[call-overload]


# --------------------------------------------------------------------------
# Sorties de jeux IGDB (SPEC-4efa R13, R14)
# --------------------------------------------------------------------------


def _empty_releases() -> dict[str, Any]:
    return {"recent": [], "upcoming": [], "excluded_low_hypes": 0, "truncated": {"recent": 0, "upcoming": 0}}


_TREND_FIELDS = ("key", "name", "twitch_fr_viewers", "twitch_delta_pct", "steam_players", "steam_rank",
                 "steam_rank_gain", "steam_new_in_top", "steam_sellers_rank", "steam_sellers_gain", "steam_sellers_new",
                 "steam_players_now", "steam_followers", "steam_followers_gain_7d", "community")


def _index_games(games: list[dict[str, Any]], twitch_igdb: dict[str, str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """``(par clé, par igdb_id fourni par Twitch)`` : l'appariement d'une sortie avec un jeu du relevé."""
    by_key = {g["key"]: g for g in games}
    return by_key, {igdb_id: by_key[key] for key, igdb_id in twitch_igdb.items() if igdb_id and key in by_key}


def _classify_releases(games: list[dict[str, Any]], today: date, table: dict[str, object]) -> list[dict[str, Any]]:
    """Une entrée par jeu IGDB dont une date tombe dans la fenêtre J-``release_window_days`` .. J+``upcoming_days``
    (R13) : ``date`` = la plus ancienne date de la fenêtre ; ``portage`` = une date antérieure existe et une plateforme
    de la fenêtre n'y figure pas. Ni exclusion pour hypes, ni tendance, ni tri ici."""
    first = (today - timedelta(days=int(table["release_window_days"]))).isoformat()  # type: ignore[call-overload]
    last = (today + timedelta(days=int(table["upcoming_days"]))).isoformat()  # type: ignore[call-overload]
    entries: list[dict[str, Any]] = []
    for game in games:
        inside = sorted((d for d in game["release_dates"] if first <= d["date"] <= last), key=lambda d: d["date"])
        if not inside:
            continue
        earlier = [d for d in game["release_dates"] if d["date"] < first]
        earlier_platforms = {d["platform"] for d in earlier if d.get("platform")}
        portage = bool(earlier) and any(d["platform"] and d["platform"] not in earlier_platforms for d in inside)
        entries.append({
            "igdb_id": game["igdb_id"], "name": game["name"], "key": normalize(game["name"]), "slug": game.get("slug"),
            "url": game.get("url"), "hypes": game["hypes"], "cover_image_id": game.get("cover_image_id"),
            "steam_appid": game.get("steam_appid"), "first_release_date": game.get("first_release_date"),
            "date": inside[0]["date"], "human": inside[0].get("human"),
            "days": (date.fromisoformat(inside[0]["date"]) - today).days,
            "platforms": sorted({d["platform"] for d in inside if d.get("platform")}),
            "regions": sorted({d["region"] for d in inside if d.get("region")}),
            "statuses": sorted({d["status"] for d in inside if d.get("status")}),
            "portage": portage, "trend": None,
        })
    return entries


def _group_releases(entries: list[dict[str, Any]], games: list[dict[str, Any]], twitch_igdb: dict[str, str],
                    table: dict[str, object]) -> dict[str, Any]:
    """Attache la tendance (jeu du relevé du jour apparié par ``igdb_id`` Twitch puis par clé), écarte les jeux sous
    ``igdb_min_hypes`` sans tendance, trie par hypes et coupe : ``recent`` (days <= 0, ``igdb_recent_max``) et
    ``upcoming`` (days >= 1, ``igdb_upcoming_max``) (R13)."""
    by_key, by_igdb = _index_games(games, twitch_igdb)
    min_hypes = int(table["igdb_min_hypes"])  # type: ignore[call-overload]
    recent: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    excluded = 0
    for entry in entries:
        game = by_igdb.get(entry["igdb_id"]) or by_key.get(entry["key"])
        trend = {k: game.get(k) for k in _TREND_FIELDS} if game else None
        if entry["hypes"] < min_hypes and trend is None:
            excluded += 1
            continue
        (recent if entry["days"] <= 0 else upcoming).append({**entry, "trend": trend})
    recent.sort(key=lambda e: (-e["hypes"], -e["days"], e["name"]))
    upcoming.sort(key=lambda e: (-e["hypes"], e["date"], e["name"]))
    cap_recent, cap_upcoming = int(table["igdb_recent_max"]), int(table["igdb_upcoming_max"])  # type: ignore[call-overload]
    return {"recent": recent[:cap_recent], "upcoming": upcoming[:cap_upcoming], "excluded_low_hypes": excluded,
            "truncated": {"recent": max(0, len(recent) - cap_recent), "upcoming": max(0, len(upcoming) - cap_upcoming)}}


def _release_marker(game: dict[str, Any], igdb_id: str, recent: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Repère de sortie d'un jeu : par ``igdb_id`` d'abord, sinon par clé normalisée ; sorties récentes seulement."""
    match = next((e for e in recent if igdb_id and e["igdb_id"] == igdb_id), None) \
        or next((e for e in recent if e["key"] == game["key"]), None)
    if match is None:
        return None
    return {"igdb_id": match["igdb_id"], "name": match["name"], "date": match["date"],
            "days_since": -match["days"], "hypes": match["hypes"]}


# --------------------------------------------------------------------------
# Joueurs et abonnés Steam, communauté (SPEC-df51 R18, R19, R21)
# --------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _history_days(sdir: Path, today: date, baseline_days: int) -> list[tuple[str, dict[str, Any]]]:
    """Les fichiers d'historique datés de J-``baseline_days`` à J-1 qui existent (plus ancien d'abord) :
    un jour sans fichier est absent, jamais comblé."""
    days: list[tuple[str, dict[str, Any]]] = []
    for offset in range(baseline_days, 0, -1):
        day = (today - timedelta(days=offset)).isoformat()
        data = _read_json(sdir / "history" / f"{day}.json", None)
        if isinstance(data, dict):
            days.append((day, data))
    return days


def _lookup_appids(games: list[dict[str, Any]], steam_hist: dict[str, dict[str, Any]], snapshots: dict[str, int | None],
                   releases: dict[str, Any], *, source: str) -> list[str]:
    """Appids à relever, ordonnés et sans doublon (R18 pour ``steam_players``, R21 pour ``steam_followers``)."""
    game_appids = [str(g["steam_appid"]) for g in games if g.get("steam_appid")]
    ordered: list[str] = []
    if source == "steam_players":
        ordered = [a for a in game_appids if snapshots.get(a) is None]
        entries = releases["recent"]
    else:
        ordered = list(game_appids)
        entries = releases["recent"] + releases["upcoming"]
    ordered += [str(e["steam_appid"]) for e in entries if e.get("steam_appid") and str(e["steam_appid"]) not in game_appids]
    if source == "steam_followers":
        ordered += list(steam_hist)
    return list(dict.fromkeys(ordered))


def _error_text(exc: Exception) -> str:
    if isinstance(exc, (VeilleError, veille_sources.SourceError)):
        return str(exc)
    return f"{type(exc).__name__} : {exc}"


def _run_lookup(source: str, appids: list[str], collectors: dict[str, Collector], table: dict[str, object],
                now: datetime, deadline: Callable[[], float] | None = None) -> tuple[dict[str, Any], dict[str, int | None]]:
    """Source par appid (``steam_players``, ``steam_followers``) : la liste est coupée au plafond (le reste compté
    ``skipped``) ; plafond 0 ou rien à relever : ``skipped`` sans appel ; erreur : ``error``, aucune valeur."""
    status: dict[str, Any] = {"status": "ok", "at": now.isoformat(), "error": None, "counts": {}}
    cap = int(table["steam_players_lookups_max" if source == "steam_players" else "steam_followers_lookups_max"])  # type: ignore[call-overload]
    kept, cut = appids[:cap], max(0, len(appids) - cap)
    if not kept:
        status.update(status="skipped", counts={"requested": 0, "found": 0, "unknown": 0, "skipped": cut})
        return status, {}
    if deadline is not None and deadline() <= 0:
        return _not_started(now), {}
    key = "players" if source == "steam_players" else "followers"
    try:
        result = _run_source(source, collectors, table, kept, deadline=deadline)
        values = {str(a): v for a, v in result[key].items()}
        if any(v is not None and not _is_int(v) for v in values.values()):
            raise VeilleError(f"{source} : valeur non entière dans la réponse")
        found = sum(v is not None for v in values.values())
        stopped = int(result.get("deadline_stopped") or 0)
        limited = int(result.get("rate_limited", 0))
        status["counts"] = {"requested": len(kept), "found": found,
                            "unknown": max(0, len(kept) - found - stopped - limited),  # un appid limité n'est pas « inconnu »
                            "skipped": cut + int(result.get("skipped", 0))}
        if limited:  # ADR-ad2e : jamais « ok » muet, la lecture partielle se voit sur l'écran Veille
            status.update(status="partial", error=f"HTTP 429 (limite de Steam) : {limited} appid(s) non relevé(s) sur {len(kept)}")
            status["counts"]["rate_limited"] = limited
        _mark_deadline(status, source, result, table)
    except Exception as exc:  # une source en erreur ne bloque pas les autres, jamais avalée
        status.update(status="error", error=_error_text(exc), counts={})
        return status, {}
    return status, values


def _community(game: dict[str, Any], table: dict[str, object]) -> dict[str, Any]:
    """R19 : un jeu a une communauté dès qu'un seul seuil est atteint ; ``null`` n'atteint aucun seuil."""
    peak, now = game.get("steam_players"), game.get("steam_players_now")
    steam, kind = (peak, "peak") if peak is not None else ((now, "now") if now is not None else (None, None))
    values = {"steam": steam, "followers": game.get("steam_followers"), "twitch": game.get("twitch_fr_viewers"),
              "hypes": (game.get("release") or {}).get("hypes")}
    limits = {"steam": table["community_min_steam_players"], "followers": table["community_min_steam_followers"],
              "twitch": table["community_min_twitch_viewers"], "hypes": table["community_min_hypes"]}
    met = [name for name, value in values.items() if value is not None and value >= limits[name]]  # type: ignore[operator]
    return {"ok": bool(met), "steam_players": steam, "steam_kind": kind, "steam_followers": values["followers"],
            "twitch_fr_viewers": values["twitch"], "hypes": values["hypes"], "met": met}


def _enrich_games(games: list[dict[str, Any]], *, today: date, sdir: Path, previous: list[dict[str, Any]],
                  table: dict[str, object], snapshots: dict[str, int | None], followers: dict[str, int | None]) -> None:
    """Pose sur chaque jeu : instantané Steam, tendance des instantanés, abonnés, courbes tirées de l'historique,
    gain d'abonnés sur ``baseline_days`` jours et ``community`` (R18, R19, R21). Rien n'est estimé : un jour sans
    mesure reste absent, un dérivé sans base reste ``None``."""
    baseline = int(table["baseline_days"])  # type: ignore[call-overload]
    days = _history_days(sdir, today, baseline)
    then = next((data for day, data in days if day == (today - timedelta(days=baseline)).isoformat()), None)
    day = today.isoformat()
    for game in games:
        appid = str(game["steam_appid"]) if game.get("steam_appid") else None
        now = snapshots.get(appid) if appid else None
        game["steam_players_now"] = now
        now_values = [p["steam_now"][appid] for p in previous if appid and appid in (p.get("steam_now") or {})]
        game["steam_now_avg"], game["steam_now_delta_pct"] = _delta(now, now_values) if now is not None else (None, None)
        peak_history: list[dict[str, Any]] = []
        for when, data in [*days, (day, None)]:
            point_peak = game["steam_players"] if data is None else ((data.get("steam") or {}).get(appid) or {}).get("players")
            point_now = now if data is None else (data.get("steam_now") or {}).get(appid)
            for kind, value in (("peak", point_peak), ("now", point_now)):
                if appid and _is_int(value):
                    peak_history.append({"date": when, "kind": kind, "players": value})
        game["steam_players_history"] = peak_history
        count = followers.get(appid) if appid else None
        game["steam_followers"] = count
        before = ((then or {}).get("steam_followers") or {}).get(appid) if appid else None
        game["steam_followers_gain_7d"] = count - before if _is_int(count) and _is_int(before) else None
        game["steam_followers_history"] = [
            {"date": when, "followers": value} for when, value in
            [*((d, (data.get("steam_followers") or {}).get(appid)) for d, data in days), (day, count)]
            if appid and _is_int(value)]
        game["community"] = _community(game, table)


def _refresh_releases(releases: dict[str, Any], games: list[dict[str, Any]], twitch_igdb: dict[str, str],
                      snapshots: dict[str, int | None], followers: dict[str, int | None]) -> None:
    """Après les relevés par appid : ``trend`` reprend les champs des jeux enrichis ; une sortie sans jeu porte
    ``steam_players_now`` et ``steam_followers`` elle-même (R18, R21)."""
    by_key, by_igdb = _index_games(games, twitch_igdb)
    for entry in releases["recent"] + releases["upcoming"]:
        game = by_igdb.get(entry["igdb_id"]) or by_key.get(entry["key"])
        if game is not None:
            entry["trend"] = {k: game.get(k) for k in _TREND_FIELDS}
        else:
            appid = str(entry["steam_appid"]) if entry.get("steam_appid") else None
            entry["steam_players_now"] = snapshots.get(appid) if appid else None
            entry["steam_followers"] = followers.get(appid) if appid else None


# --------------------------------------------------------------------------
# Tendance sur 30 jours (SPEC-85a0 R23-R25)
# --------------------------------------------------------------------------

_SERIES = ("steam_reviews", "twitch_vods_fr", "twitch_viewers_fr", "steam_players_peak", "steam_players_now",
           "steam_followers")
_WEEKS = ((27, 21), (20, 14), (13, 7), (6, 0))  # s4, s3, s2, s1 : jours avant J0 (bornes comprises)


def _followed_games(games: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Jeux suivis (R23) : communauté ok ET au moins une candidate après le filtre de communauté, dans l'ordre de ``games``."""
    with_candidate = {c["game_key"] for c in candidates if c.get("game_key")}
    return [g for g in games if g["community"]["ok"] and g["key"] in with_candidate]


def _pct(value: int | None, base: int | None) -> int | None:
    return round((value - base) / base * 100) if value is not None and base else None


def _summary(points: list[dict[str, Any]], field: str, today: date, window_days: int) -> dict[str, Any]:
    """Résumé d'une série (R23) : moyenne par jour des jours mesurés de chaque semaine pleine (s4..s1), pic (le plus
    ancien en cas d'égalité), dernier point ; une semaine sans mesure est ``None``, jamais 0."""
    weeks: list[int | None] = []
    for older, newer in _WEEKS:
        low, high = (today - timedelta(days=older)).isoformat(), (today - timedelta(days=newer)).isoformat()
        values = [p[field] for p in points if low <= p["date"] <= high]
        weeks.append(round(sum(values) / len(values)) if values else None)
    peak: dict[str, Any] | None = None
    for point in points:  # points triés par date : ``>`` garde le plus ancien d'une égalité
        if peak is None or point[field] > peak["value"]:
            peak = {"date": point["date"], "value": point[field]}
    last = {"date": points[-1]["date"], "value": points[-1][field]} if points else None
    return {"weeks": weeks, "peak": peak, "last": last,
            "last_vs_peak_pct": _pct(last["value"], peak["value"]) if peak and last else None,
            "s1_vs_s2_pct": _pct(weeks[3], weeks[2]), "measured_days": len(points), "window_days": window_days}


def _serie(status: str, reason: str | None, since: str | None, points: list[dict[str, Any]], field: str,
           today: date, window_days: int) -> dict[str, Any]:
    clean = sorted((dict(p) for p in points), key=lambda p: p["date"])
    return {"status": status, "reason": reason, "since": since, "points": clean,
            "summary": _summary(clean, field, today, window_days)}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "since": None, "points": [], "summary": None}


def _check_reviews(values: dict[str, Any]) -> None:
    for appid, points in values.items():
        if points is not None and not (isinstance(points, list) and all(
                isinstance(p, dict) and isinstance(p.get("date"), str) and _is_int(p.get("value")) for p in points)):
            raise VeilleError(f"steam_reviews : série illisible pour l'appid {appid}")


def _check_vods(values: dict[str, Any]) -> None:
    for game_id, entry in values.items():
        if entry is not None and not (isinstance(entry, dict) and isinstance(entry.get("since"), str) and all(
                isinstance(p, dict) and isinstance(p.get("date"), str) and _is_int(p.get("vods")) and _is_int(p.get("views"))
                for p in entry.get("points") or [])):
            raise VeilleError(f"twitch_vods_30d : série illisible pour le jeu {game_id}")


def _run_trend_source(source: str, ids: list[str], cut: int, collectors: dict[str, Collector], table: dict[str, object],
                      now: datetime, *, twitch_error: bool,
                      deadline: Callable[[], float] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Source de tendance (R24, R25) : ``ids`` = appids (``steam_reviews``) ou ``game_id`` Twitch (``twitch_vods_30d``)
    des jeux suivis. Rien à relever (plafond 0, aucun id) : ``skipped`` sans appel ; source Twitch en erreur : jamais
    appelée ; collecteur en erreur : ``error``, aucune valeur ; 429 persistant : ``partial``."""
    status: dict[str, Any] = {"status": "ok", "at": now.isoformat(), "error": None, "counts": {}}
    empty = {"requested": 0, "found": 0, "unknown": 0, "skipped": cut, "rate_limited": 0}
    if source == "twitch_vods_30d" and twitch_error:
        status.update(status="skipped", error="source Twitch en erreur", counts=empty)
        return status, {}
    if not ids:
        status.update(status="skipped", counts=empty)
        return status, {}
    if deadline is not None and deadline() <= 0:
        return _not_started(now), {}
    key, check = ("histograms", _check_reviews) if source == "steam_reviews" else ("vods", _check_vods)
    try:
        result = _run_source(source, collectors, table, ids, deadline=deadline)
        values = {str(k): v for k, v in result[key].items()}
        check(values)
        found = sum(v is not None for v in values.values())
        limited = int(result.get("rate_limited", 0))
        stopped = int(result.get("deadline_stopped") or 0)
        status["counts"] = {"requested": len(ids), "found": found, "unknown": len(ids) - found - stopped,
                            "skipped": cut + int(result.get("skipped", 0)), "rate_limited": limited}
        if limited:  # ADR-ad2e : jamais « ok » muet
            unit = "appid(s)" if source == "steam_reviews" else "jeu(x)"
            host = "Steam" if source == "steam_reviews" else "Helix"
            status.update(status="partial", error=f"HTTP 429 ({host}) : {limited} {unit} non relevé(s) sur {len(ids)}")
        _mark_deadline(status, source, result, table)
    except Exception as exc:  # une source en erreur ne bloque pas les autres, jamais avalée
        status.update(status="error", error=_error_text(exc), counts={})
        return status, {}
    return status, values


def _history_window(sdir: Path, today: date, days: int) -> dict[str, dict[str, Any]]:
    """Fichiers d'historique des ``days``-1 jours avant J0 qui existent : un jour sans fichier est absent, jamais comblé."""
    found: dict[str, dict[str, Any]] = {}
    for offset in range(days - 1, 0, -1):
        day = (today - timedelta(days=offset)).isoformat()
        data = _read_json(sdir / "history" / f"{day}.json", None)
        if isinstance(data, dict):
            found[day] = data
    return found


def _build_trend(games: list[dict[str, Any]], followed: list[dict[str, Any]], reviews: dict[str, Any],
                 vods: dict[str, Any], table: dict[str, object], sources: dict[str, dict[str, Any]], sdir: Path,
                 today: date, twitch_hist: dict[str, dict[str, Any]]) -> None:
    """Pose ``trend_30d`` sur les jeux suivis (R23) d'après les deux sources de tendance déjà relevées ; les autres
    jeux gardent ``None``. Un jour sans mesure est absent des points, jamais zéro ni interpolé."""
    days = int(table["trend_days"])  # type: ignore[call-overload]
    first = (today - timedelta(days=days - 1)).isoformat()
    last_day = today.isoformat()
    twitch_error = sources["twitch"]["status"] == "error"
    history = _history_window(sdir, today, days)

    def in_window(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [p for p in points if first <= p["date"] <= last_day]

    def own(game: dict[str, Any], getter: Callable[[dict[str, Any]], Any], current: Any) -> dict[str, Any]:
        values = [(day, getter(data)) for day, data in history.items()] + [(last_day, current)]
        points = [{"date": day, "value": v} for day, v in values if _is_int(v)]
        return _serie("ok", None, first, points, "value", today, days)

    for game in followed:
        appid = str(game["steam_appid"]) if game.get("steam_appid") else None
        gid = game.get("twitch_id")
        key = game["key"]
        series: dict[str, dict[str, Any]] = {}
        if appid is None:
            series["steam_reviews"] = _unavailable("hors Steam")
        elif sources["steam_reviews"]["status"] in ("error", "skipped"):
            series["steam_reviews"] = _unavailable(str(sources["steam_reviews"]["error"]))
        elif appid not in reviews:
            series["steam_reviews"] = _unavailable("échéance atteinte avant le relevé de cet appid")
        elif reviews[appid] is None:
            series["steam_reviews"] = _unavailable("HTTP 429 Steam")
        else:
            series["steam_reviews"] = _serie("ok", None, first, in_window(reviews[appid]), "value", today, days)
        if twitch_error:
            series["twitch_vods_fr"] = series["twitch_viewers_fr"] = _unavailable("source Twitch en erreur")
        elif not gid:
            series["twitch_vods_fr"] = series["twitch_viewers_fr"] = _unavailable("hors Twitch FR")
        else:
            if sources["twitch_vods_30d"]["status"] in ("error", "skipped"):
                series["twitch_vods_fr"] = _unavailable(str(sources["twitch_vods_30d"]["error"]))
            elif str(gid) not in vods:
                series["twitch_vods_fr"] = _unavailable("échéance atteinte avant le relevé de ce jeu")
            elif vods[str(gid)] is None:
                series["twitch_vods_fr"] = _unavailable("HTTP 429 Helix")
            else:
                entry = vods[str(gid)]
                capped = entry["since"] > first  # plafond Twitch (500 VOD) : les jours antérieurs sont inconnus
                series["twitch_vods_fr"] = _serie(
                    "partial" if capped else "ok",
                    f"plafond Twitch 500 VOD : jours avant {entry['since']} inconnus" if capped else None,
                    entry["since"], in_window(entry.get("points") or []), "vods", today, days)
            series["twitch_viewers_fr"] = own(
                game, lambda d: ((d.get("twitch") or {}).get(key) or {}).get("viewers_fr"),
                (twitch_hist.get(key) or {}).get("viewers_fr"))
        for name, getter, current in (
                ("steam_players_peak", lambda d: (((d.get("steam") or {}).get(appid) or {}).get("players")), game.get("steam_players")),
                ("steam_players_now", lambda d: (d.get("steam_now") or {}).get(appid), game.get("steam_players_now")),
                ("steam_followers", lambda d: (d.get("steam_followers") or {}).get(appid), game.get("steam_followers"))):
            series[name] = _unavailable("hors Steam") if appid is None else own(game, getter, current)
        game["trend_30d"] = {"days": days, "since": first, "series": {name: series[name] for name in _SERIES}}


# --------------------------------------------------------------------------
# Collecte
# --------------------------------------------------------------------------


def _accepts_deadline(collector: Callable[..., Any]) -> bool:
    """Le collecteur déclare ``deadline`` (ou ``**kwargs``) : un faux collecteur sans lui reste appelable tel quel."""
    try:
        parameters = inspect.signature(collector).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is inspect.Parameter.VAR_KEYWORD or (
        p.name == "deadline" and p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD))
        for p in parameters)


def _run_source(source: str, collectors: dict[str, Collector], table: dict[str, object], *args: Any,
                deadline: Callable[[], float] | None = None) -> dict[str, Any]:
    """Résultat brut d'un collecteur (``args`` : liste d'appids des sources par appid) ; lève ``VeilleError``
    (clé absente) ou l'erreur du collecteur. ``deadline`` (secondes restantes) n'est passée qu'à un collecteur qui
    l'accepte (R29)."""
    for key in _REQUIRED_KEYS[source]:
        if not str(table[key]).strip():
            raise VeilleError(f"{key} absente : à saisir dans Réglages › Veille")
    if source not in collectors:
        raise VeilleError(f"aucun collecteur fourni pour la source {source}")
    collector = collectors[source]
    if deadline is not None and _accepts_deadline(collector):
        return collector(table, *args, deadline=deadline)  # type: ignore[call-arg]
    return collector(table, *args)


def _not_started(now: datetime) -> dict[str, Any]:
    """Statut d'une source que l'échéance a empêchée de commencer : ``skipped``, jamais une valeur (R29)."""
    return {"status": "skipped", "at": now.isoformat(), "error": _NOT_STARTED, "counts": {}}


def _mark_deadline(status: dict[str, Any], source: str, result: dict[str, Any], table: dict[str, object]) -> int:
    """Le collecteur s'est arrêté à l'échéance (``deadline_stopped`` = k > 0) : ``partial`` avec le compte de ce qui
    manque, jamais ``ok`` muet. Rend k."""
    stopped = int(result.get("deadline_stopped") or 0)
    if stopped > 0:
        message = f"échéance de {table['veille_deadline_s']} s atteinte : {stopped} {_UNITS[source]} non relevé(s)"
        status["status"] = "partial"
        status["error"] = f"{status['error']} ; {message}" if status.get("error") else message
        status["counts"]["deadline"] = stopped
    return stopped


def _cut_by_deadline(entry: dict[str, Any]) -> bool:
    return (entry["status"] == "skipped" and entry.get("error") == _NOT_STARTED) or (
        int((entry.get("counts") or {}).get("deadline") or 0) > 0)


def _run_lanes(*lanes: Callable[[], None]) -> None:
    """Un fil par voie (SPEC-85a0 R29) ; rend la main quand toutes ont fini (barrière de phase). Une erreur de
    source est déjà prise par la voie ; ce qui s'échappe d'un fil est un défaut du code, relevé ici, jamais avalé."""
    failures: list[BaseException] = []

    def guard(lane: Callable[[], None]) -> None:
        try:
            lane()
        except BaseException as exc:  # noqa: BLE001 - relevé après la jonction
            failures.append(exc)

    threads = [threading.Thread(target=guard, args=(lane,), name="veille-lane", daemon=True) for lane in lanes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        raise failures[0]


def _elapsed_clock(start: datetime) -> Callable[[], datetime]:
    """Horloge par défaut d'un relevé : ``start`` (le ``now`` fourni) puis le temps réel écoulé."""
    began = time.monotonic()
    return lambda: start + timedelta(seconds=time.monotonic() - began)


def _to_candidate(source: str, vod: dict[str, Any]) -> dict[str, Any]:
    game_name = vod.get("game_name")
    return {
        "id": f"{source}:{vod['video_id']}", "source": source, "video_id": vod["video_id"],
        "url": vod["url"], "title": vod.get("title"), "channel_name": vod.get("channel_name"),
        "game_key": normalize(game_name) if game_name else None, "game_name": game_name,
        "duration_s": vod["duration_s"], "published_at": vod["published_at"],
        "view_count": vod.get("view_count"), "thumbnail_url": vod.get("thumbnail_url"), "views_per_hour": vod.get("views_per_hour"),
        "signals": {}, "game_source": None,
        "tags": [str(t) for t in vod.get("tags") or []],
    }


def _deduce_game(candidate: dict[str, Any], known: dict[str, str], min_chars: int) -> None:
    """Jeu d'une VOD YouTube sans jeu : un nom de jeu connu du jour présent en mot(s) entier(s) dans le titre
    ou les tags. Plusieurs trouvés : le plus long gagne s'il contient tous les autres, sinon ambiguïté = aucun jeu.
    Aucun appel LLM, aucun nom inventé."""
    text = f" {' '.join(normalize(t) for t in [candidate.get('title') or '', *candidate['tags']])} "
    found = [key for key in known if len(key) >= min_chars and f" {key} " in text]
    if not found:
        return
    best = max(found, key=len)
    if any(f" {key} " not in f" {best} " for key in found):
        return
    candidate["game_key"], candidate["game_name"], candidate["game_source"] = best, known[best], "titre"


def _access_attempts(
    candidate: dict[str, Any], attempts: int, pause_s: float, timeout_s: float,
    access_check: Callable[[str, float], None], sleep: Callable[[float], None],
    deadline: Callable[[], float] | None = None,
) -> str:
    """Jusqu'à ``attempts`` essais séparés de ``pause_s`` (aucune pause après le dernier). Rend ``ok``,
    ``restricted`` (réservée aux abonnés : aucun essai suivant), ``unreachable`` (dernière erreur journalisée) ou
    ``deadline`` (échéance passée avant une pause ou un essai : ni pause ni essai de plus, R29)."""
    error: Exception | None = None
    for attempt in range(attempts):
        if deadline is not None and deadline() <= 0:
            return "deadline"
        if attempt:
            sleep(pause_s)
        try:
            access_check(candidate["url"], timeout_s)
            return "ok"
        except veille_sources.AccessRestricted:
            return "restricted"
        except Exception as exc:  # réseau, connexion fermée, délai : on réessaie
            error = exc
    log.warning("veille twitch : %s injoignable après %d essai(s) (%s)", candidate["url"], attempts,
                f"{type(error).__name__} : {error}"[:300])
    return "unreachable"


def _check_twitch_access(
    candidates: list[dict[str, Any]], games: list[dict[str, Any]], table: dict[str, object],
    access_check: Callable[[str, float], None], sleep: Callable[[float], None],
    deadline: Callable[[], float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Test d'accès par jeu (SPEC-85a0 R26) : dans l'ordre de ``games``, les VOD Twitch du jeu, des plus vues aux
    moins vues, jusqu'à ``max_vods_per_game`` accessibles ; les suivantes ne sont pas testées (``untested``).
    Réservée aux abonnés ou injoignable : écartée, comptée. Échéance passée (R29) : plus aucun essai, les VOD pas
    encore testées sont écartées et comptées ``deadline``, jamais gardées « non vérifiées ».
    Les jeux sont pris dans l'ordre de ``games`` (celui dont Claude se sert : les plus utiles d'abord) par au plus
    ``twitch_access_workers`` fils ; chaque jeu reste séquentiel (une VOD à la fois) : le résultat est celui du
    test séquentiel, seule l'échéance coupe, et elle coupe les derniers jeux de la liste.
    Rend (gardées, {restricted, unreachable, untested, deadline})."""
    wanted = int(table["max_vods_per_game"])  # type: ignore[call-overload]
    attempts = int(table["twitch_access_attempts"])  # type: ignore[call-overload]
    pause_s = float(table["twitch_access_retry_pause_s"])  # type: ignore[arg-type]
    timeout_s = float(table["http_timeout_s"])  # type: ignore[arg-type]
    workers = int(table["twitch_access_workers"])  # type: ignore[call-overload]

    def check_game(game: dict[str, Any]) -> tuple[dict[str, int], set[str]]:
        counts = {"restricted": 0, "unreachable": 0, "untested": 0, "deadline": 0}
        dropped: set[str] = set()
        vods = [c for c in candidates if c["source"] == "twitch" and c["game_key"] == game["key"]]
        vods.sort(key=lambda c: -(c["view_count"] or 0))  # tri stable : à vues égales, ordre de la source
        accessible = 0
        for candidate in vods:
            if accessible >= wanted:
                counts["untested"] += 1
                dropped.add(candidate["id"])
                continue
            outcome = _access_attempts(candidate, attempts, pause_s, timeout_s, access_check, sleep, deadline)
            if outcome == "ok":
                accessible += 1
            else:
                counts[outcome] += 1
                dropped.add(candidate["id"])
        return counts, dropped

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="veille-access") as pool:
        outcomes = list(pool.map(check_game, games))  # soumis dans l'ordre de ``games``, rendus dans le même ordre
    totals = {"restricted": 0, "unreachable": 0, "untested": 0, "deadline": 0}
    dropped_all: set[str] = set()
    for counts, dropped in outcomes:
        for name, number in counts.items():
            totals[name] += number
        dropped_all |= dropped
    return [c for c in candidates if c["id"] not in dropped_all], totals


def _parse_published(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"date sans fuseau : {value!r}")
    return parsed


def collect(
    now: datetime,
    *,
    collectors: dict[str, Collector] | None = None,
    config: Config | None = None,
    finalize: bool = True,
    access_check: Callable[[str, float], None] | None = None,
    access_sleep: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Un relevé (SPEC-bdd9 R2, R4, R5 ; SPEC-85a0 R29) : appelle les collecteurs injectés par voies parallèles, une
    par hôte, en trois phases séparées par une barrière (1 : twitch puis igdb / youtube / steam puis steam_fr ;
    2 : steam_players / steam_followers ; 3 : test d'accès / steam_reviews / twitch_vods_30d), écrit
    ``history/<date>.json`` et ``days/<date>.json`` et rend l'état du jour.
    Une source en erreur n'arrête pas les autres ; seuls un réglage invalide
    ou un fichier d'état illisible lèvent ``VeilleError``. ``finalize=False`` laisse
    ``finished_at`` à ``null`` (le choix de Claude suit, voir ``run_if_due``).
    ``access_check(url, timeout_s)`` teste l'accès des VOD Twitch (défaut :
    ``veille_sources.check_twitch_access``, yt-dlp sans téléchargement) : voir ``_check_twitch_access`` ;
    ``access_sleep(s)`` fait l'attente entre deux essais (défaut ``time.sleep``).
    L'échéance globale est ``now + veille_deadline_s`` sur ``clock`` (défaut : ``now`` puis le temps réel écoulé) :
    ``deadline_at`` et ``deadline_hit`` sont écrits dans l'état du jour."""
    config = config or load_config()
    collectors = veille_sources.default_collectors() if collectors is None else collectors
    table = settings(config)
    sdir = _state_dir(table)
    local_now = now.astimezone(ZoneInfo(str(table["timezone"])))
    today = local_now.date()
    day = today.isoformat()
    started_at = now.isoformat()
    read_clock = clock or _elapsed_clock(now)
    deadline_at = now + timedelta(seconds=int(table["veille_deadline_s"]))  # type: ignore[call-overload]

    def remaining() -> float:
        return (deadline_at - read_clock()).total_seconds()

    previous = [p for p in _load_history(sdir, today, int(table["baseline_days"]))]
    known = _seen_video_ids(sdir) | _queue_video_ids(config)
    workspace = Path(config.workspace_dir)

    sources: dict[str, dict[str, Any]] = {}
    twitch_hist: dict[str, dict[str, Any]] = {}
    steam_hist: dict[str, dict[str, Any]] = {}
    steam_concurrent: dict[str, int | None] = {}  # instantané du top 100 (R18), hors historique "steam"
    sellers_hist: dict[str, dict[str, Any]] = {}
    youtube_hist: dict[str, dict[str, Any]] = {}
    twitch_igdb: dict[str, str] = {}  # clé de jeu -> igdb_id fourni par Twitch (hors historique)
    igdb_games: list[dict[str, Any]] = []
    igdb_skipped = 0
    followers: dict[str, int | None] = {}
    source_vods: dict[str, list[dict[str, Any]]] = {}

    def fetch(source: str) -> None:
        """Une source de la phase 1 ; chaque source n'écrit que ses propres variables (voies sans partage)."""
        nonlocal twitch_hist, twitch_igdb, igdb_games, igdb_skipped, steam_hist, steam_concurrent, sellers_hist, youtube_hist
        if remaining() <= 0:  # une source qui n'a pas pu commencer : visible, jamais comblée
            sources[source] = _not_started(now)
            return
        status: dict[str, Any] = {"status": "ok", "at": now.isoformat(), "error": None, "counts": {}}
        try:
            result = _run_source(source, collectors, table, deadline=remaining)
            if source == "twitch":
                for game in result["games"]:
                    twitch_hist[normalize(game["name"])] = {
                        "name": game["name"], "viewers_fr": game["viewers_fr"],
                        "twitch_id": str(game["twitch_id"]) if game.get("twitch_id") else None}
                    twitch_igdb[normalize(game["name"])] = str(game.get("igdb_id") or "")
                vods = list(result["vods"])
                status["counts"] = {"games": len(twitch_hist), "vods": len(vods), "private": int(result.get("private_vods", 0))}
            elif source == "youtube":
                vods = list(result["videos"])
                for vod in vods:
                    if vod.get("game_name") and vod.get("views_per_hour") is not None:
                        entry = youtube_hist.setdefault(normalize(vod["game_name"]), {"views_per_hour_sum": 0})
                        entry["views_per_hour_sum"] += vod["views_per_hour"]
                status["counts"] = {"videos": len(vods)}
            elif source == "igdb":
                igdb_games = list(result["games"])
                igdb_skipped = int(result.get("skipped_rows", 0))
                vods = []
            elif source == "steam_fr":
                for game in result["games"]:
                    sellers_hist[str(game["appid"])] = {"name": game["name"], "rank": game["rank"],
                                                        "last_week_rank": game.get("last_week_rank")}
                vods = []
                status["counts"] = {"games": len(sellers_hist)}
            else:
                for game in result["games"]:
                    steam_hist[str(game["appid"])] = {"name": game["name"], "players": game["players"],
                                                      "rank": game.get("rank"), "last_week_rank": game.get("last_week_rank")}
                    steam_concurrent[str(game["appid"])] = game.get("concurrent")
                vods = []
                status["counts"] = {"games": len(steam_hist)}
                if result.get("unnamed"):
                    status["unnamed"] = list(result["unnamed"])
                    log.warning("veille steam : %d jeu(x) sans nom, ex. %s", len(status["unnamed"]), status["unnamed"][0]["reason"])
            _mark_deadline(status, source, result, table)
            for vod in vods:
                _parse_published(vod["published_at"])
                vod["video_id"], vod["url"], vod["duration_s"]  # champs obligatoires
            source_vods[source] = vods
        except Exception as exc:  # une source en erreur ne bloque pas les autres, jamais avalée
            status.update(status="error", error=_error_text(exc), counts={})
            if source == "twitch":
                twitch_hist, twitch_igdb = {}, {}
            elif source == "igdb":
                igdb_games, igdb_skipped = [], 0
            elif source == "steam":
                steam_hist, steam_concurrent = {}, {}
            elif source == "steam_fr":
                sellers_hist = {}
            else:
                youtube_hist = {}
            source_vods.pop(source, None)
        sources[source] = status

    def sequence(*names: str) -> Callable[[], None]:
        def lane() -> None:
            for name in names:
                fetch(name)
        return lane

    # Phase 1 : helix (twitch puis igdb), youtube, steam_api (steam puis steam_fr)
    _run_lanes(sequence("twitch", "igdb"), sequence("youtube"), sequence("steam", "steam_fr"))
    raw_vods: list[tuple[str, dict[str, Any]]] = [(src, vod) for src in SOURCES for vod in source_vods.get(src, [])]

    # Candidats (R5)
    excluded = {"too_short": 0, "too_old": 0, "already_known": 0, "no_community": 0,
                "access_restricted": 0, "access_unreachable": 0, "access_untested": 0, "access_deadline": 0}
    candidates: list[dict[str, Any]] = []
    max_age = timedelta(hours=float(table["vod_max_age_h"]))
    for source, vod in raw_vods:
        min_duration = table["youtube_min_duration_s" if source == "youtube" else "vod_min_duration_s"]
        if vod["duration_s"] < min_duration:
            excluded["too_short"] += 1
        elif now - _parse_published(vod["published_at"]) > max_age:
            excluded["too_old"] += 1
        elif (vod["video_id"] in known or _worker_video_id(vod) in known
              or (workspace / vod["video_id"]).exists()
              or (bool(_worker_video_id(vod)) and (workspace / _worker_video_id(vod)).exists())):
            excluded["already_known"] += 1
        else:
            candidates.append(_to_candidate(source, vod))

    known_games: dict[str, str] = {k: g["name"] for k, g in twitch_hist.items()}
    for entry in (*steam_hist.values(), *sellers_hist.values()):
        known_games.setdefault(normalize(entry["name"]), entry["name"])
    in_window = _classify_releases(igdb_games, today, table)
    for entry in in_window:
        if entry["hypes"] >= int(table["igdb_min_hypes"]):  # type: ignore[call-overload]
            known_games.setdefault(entry["key"], entry["name"])
    for candidate in candidates:
        if candidate["source"] == "youtube" and not candidate["game_key"]:
            _deduce_game(candidate, known_games, int(table["youtube_game_min_chars"]))  # type: ignore[call-overload]

    games = _build_games(twitch_hist, steam_hist, youtube_hist, {}, previous, table, sellers_hist)
    for game in games:
        game["twitch_id"] = twitch_hist.get(game["key"], {}).get("twitch_id")
        game["trend_30d"] = None
    releases = _group_releases(in_window, games, twitch_igdb, table)
    if sources["igdb"]["status"] in ("ok", "partial"):
        sources["igdb"]["counts"] |= {"rows": len(igdb_games), "recent": len(releases["recent"]),
                                      "upcoming": len(releases["upcoming"]), "skipped_rows": igdb_skipped}
    for game in games:
        game["release"] = _release_marker(game, twitch_igdb.get(game["key"], ""), releases["recent"])

    # Phase 2 : joueurs Steam à l'instant (steam_api) et abonnés (steamcommunity) par appid (R18, R21), puis communauté (R19)
    snapshots: dict[str, int | None] = {a: v for a, v in steam_concurrent.items() if v is not None}
    appid_lists = {source: _lookup_appids(games, steam_hist, steam_concurrent, releases, source=source)
                   for source in _APPID_SOURCES}
    lookups: dict[str, tuple[dict[str, Any], dict[str, int | None]]] = {}

    def look(source: str) -> Callable[[], None]:
        def lane() -> None:
            lookups[source] = _run_lookup(source, appid_lists[source], collectors, table, now, remaining)
        return lane

    _run_lanes(*(look(source) for source in _APPID_SOURCES))
    for source in _APPID_SOURCES:
        sources[source], values = lookups[source]
        if source == "steam_players":
            snapshots.update({a: v for a, v in values.items() if v is not None})
        else:
            followers = values
    _enrich_games(games, today=today, sdir=sdir, previous=previous, table=table, snapshots=snapshots, followers=followers)
    _refresh_releases(releases, games, twitch_igdb, snapshots, followers)

    by_key = {g["key"]: g for g in games}
    kept: list[dict[str, Any]] = []
    for candidate in candidates:  # un jeu sans monde, ou inconnu, n'a aucune VOD proposée (R19)
        game = by_key.get(candidate["game_key"]) if candidate["game_key"] else None
        if game is not None and game["community"]["ok"]:
            kept.append(candidate)
        else:
            excluded["no_community"] += 1
    candidates = kept
    followed_all = _followed_games(games, candidates)  # fixés avant le test d'accès (R23)
    followed = followed_all[: int(table["trend_games_max"])]  # type: ignore[call-overload]
    cut = len(followed_all) - len(followed)
    twitch_error = sources["twitch"]["status"] == "error"
    review_ids = list(dict.fromkeys(str(g["steam_appid"]) for g in followed if g.get("steam_appid")))
    vod_ids = list(dict.fromkeys(str(g["twitch_id"]) for g in followed if g.get("twitch_id")))

    # Phase 3 : usher (test d'accès par jeu), steam_store (avis), helix (VOD 30 jours)
    checked: dict[str, tuple[list[dict[str, Any]], dict[str, int]]] = {}
    trends: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def usher() -> None:
        if sources["twitch"]["status"] in ("ok", "partial"):
            checked["access"] = _check_twitch_access(
                candidates, games, table, access_check or veille_sources.check_twitch_access, access_sleep or time.sleep,
                remaining)

    def trend(source: str, ids: list[str]) -> Callable[[], None]:
        def lane() -> None:
            trends[source] = _run_trend_source(source, ids, cut, collectors, table, now, twitch_error=twitch_error,
                                               deadline=remaining)
        return lane

    _run_lanes(usher, trend("steam_reviews", review_ids), trend("twitch_vods_30d", vod_ids))
    if "access" in checked:
        candidates, access = checked["access"]
        stopped = access["deadline"]
        sources["twitch"]["counts"].update({k: v for k, v in access.items() if k != "deadline"})
        if stopped:
            message = f"échéance de {table['veille_deadline_s']} s atteinte : {stopped} VOD non testée(s)"
            counts = sources["twitch"]["counts"]
            counts["deadline"] = int(counts.get("deadline", 0)) + stopped
            sources["twitch"].update(status="partial", error=(
                f"{sources['twitch']['error']} ; {message}" if sources["twitch"]["error"] else message))
        excluded.update({f"access_{name}": number for name, number in access.items()})
    sources["steam_reviews"], reviews = trends["steam_reviews"]
    sources["twitch_vods_30d"], vods_30d = trends["twitch_vods_30d"]
    for game in games:
        game["vod_count"] = sum(1 for c in candidates if c["game_key"] == game["key"])
    for candidate in candidates:
        game = by_key.get(candidate["game_key"])
        release = game["release"] if game else (
            _release_marker({"key": candidate["game_key"]}, "", releases["recent"]) if candidate["game_key"] else None)
        candidate["signals"] = {"release_days_since": release["days_since"] if release else None}
        if game:
            candidate["signals"] |= {"twitch_delta_pct": game["twitch_delta_pct"], "steam_delta_pct": game["steam_delta_pct"],
                                    "steam_rank_gain": game["steam_rank_gain"], "steam_new_in_top": game["steam_new_in_top"],
                                    "steam_sellers_gain": game["steam_sellers_gain"], "steam_sellers_new": game["steam_sellers_new"]}

    sources = {name: sources[name] for name in SOURCES if name in sources}  # ordre stable, voies ou pas
    _build_trend(games, followed, reviews, vods_30d, table, sources, sdir, today, twitch_hist)

    _write(sdir / "history" / f"{day}.json", {
        "date": day, "at": now.isoformat(), "twitch": twitch_hist, "steam": steam_hist, "steam_fr": sellers_hist, "youtube": youtube_hist,
        "steam_now": snapshots, "steam_followers": {a: v for a, v in followers.items() if v is not None},
    })
    state = {
        "date": day, "started_at": started_at, "finished_at": now.isoformat() if finalize else None, "sources": sources,
        "games": games, "candidates": candidates, "excluded": excluded, "releases": releases,
        "deadline_at": deadline_at.isoformat(), "deadline_hit": any(_cut_by_deadline(e) for e in sources.values()),
        "llm": {"status": "skipped", "error": None, "model": None},
        "proposals": [], "skipped_note": "", "refresh_requested_at": None,
    }
    _write(sdir / "days" / f"{day}.json", state)
    _prune_history(sdir, today, int(table["history_days"]))
    return state


# --------------------------------------------------------------------------
# Choix de Claude (R6)
# --------------------------------------------------------------------------


def _schema(max_picks: int) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["picks", "skipped_note"],
        "additionalProperties": False,
        "properties": {
            "picks": {
                "type": "array", "maxItems": max_picks,
                "items": {
                    "type": "object", "required": ["candidate_id", "reason"], "additionalProperties": False,
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 240},
                    },
                },
            },
            "skipped_note": {"type": "string", "maxLength": 300},
        },
    }


def _check_picks(candidates: list[dict[str, Any]], max_per_game: int) -> Callable[[Any], None]:
    """Refuse un id inconnu ou en double, et plus de ``max_per_game`` picks d'un même jeu connu (R20)."""
    by_id = {c["id"]: c for c in candidates}

    def check(value: Any) -> None:
        seen: set[str] = set()
        per_game: dict[str, int] = {}
        for pick in value["picks"]:
            cid = pick["candidate_id"]
            if cid not in by_id:
                raise llm.SchemaError(f"candidate_id inconnu : {cid!r}")
            if cid in seen:
                raise llm.SchemaError(f"candidate_id en double : {cid!r}")
            seen.add(cid)
            key = by_id[cid].get("game_key")
            if key:
                per_game[key] = per_game.get(key, 0) + 1
                if per_game[key] > max_per_game:
                    raise llm.SchemaError(
                        f"plus de {max_per_game} VOD du jeu {by_id[cid].get('game_name') or key!r} (max_vods_per_game)")

    return check


def _fmt(value: Any) -> str:
    return "inconnu" if value is None else str(value)


def _or_insufficient(value: Any) -> str:
    """Dérivé de l'historique : ``None`` = pas assez de jours mesurés, jamais un chiffre estimé."""
    return "historique insuffisant" if value is None else str(value)


def _steam_players_label(community: dict[str, Any]) -> str:
    """Chiffre Steam d'un jeu et sa mesure : le pic du jour ou l'instantané (jamais mélangés)."""
    value, kind = community.get("steam_players"), community.get("steam_kind")
    if value is None or kind is None:
        return "inconnu"
    return f"{value} ({'pic' if kind == 'peak' else 'instantané'})"


def _release_line(entry: dict[str, Any], *, upcoming: bool) -> str:
    when = f"{entry['date']} J-{entry['days']}" if upcoming else f"J+{-entry['days']}"
    portage = " portage=oui" if entry.get("portage") else ""
    return (f"- {entry['name']} : {when} hypes={_fmt(entry.get('hypes'))} "
            f"plateformes={', '.join(entry.get('platforms') or [])}{portage}")


def _releases_block(day_state: dict[str, Any], table: dict[str, object]) -> list[str]:
    """Bloc « Sorties de jeux (IGDB) » du prompt (R15) ; source en erreur : indisponible avec l'erreur."""
    igdb = (day_state.get("sources") or {}).get("igdb")
    if not igdb or igdb.get("status") not in ("ok", "partial"):
        reason = igdb["error"] if igdb else "relevé sans source IGDB"
        return ["", f"Sorties de jeux : indisponibles ({reason})"]
    releases = day_state.get("releases") or _empty_releases()
    lines = ["", "Sorties de jeux (IGDB) :", "Sorties récentes :"]
    lines += [_release_line(e, upcoming=False) for e in releases["recent"]] or ["- aucune"]
    lines += ["Sorties à venir :"]
    lines += [_release_line(e, upcoming=True) for e in releases["upcoming"]] or ["- aucune"]
    lines.append(
        f"Un jeu sorti depuis 0 à {table['release_window_days']} jours est dans sa fenêtre de sortie : à qualité de "
        "gameplay égale, propose d'abord ses VOD ; une sortie à venir n'est pas un motif de choix aujourd'hui.")
    return lines


def _bilan_lines(table: dict[str, object]) -> list[str]:
    """Bilan des VOD choisies (``bilan.json``, écrit par ``clipper.learning``, SPEC-00db R8) ; illisible = erreur."""
    path = _state_dir(table) / "bilan.json"
    report = _read_json(path, None)
    if report is None:
        entries: list[dict[str, Any]] = []
    elif (isinstance(report, dict) and isinstance(report.get("entries"), list)
          and all(isinstance(e, dict) for e in report["entries"])):
        entries = report["entries"]
    else:
        raise VeilleError(f"fichier d'état de la veille illisible : {path.name} (objet avec une liste « entries » attendu)")
    if not entries:
        return ["", "Bilan des VOD choisies récemment : aucun (pas encore de résultats)"]
    lines = ["", "Bilan des VOD choisies récemment (vues à maturité, rang 0-1 dans le compte) :"]
    for e in entries:
        head = (f"- {str(e.get('title'))[:80]!r} jeu={str(_fmt(e.get('game_name')))[:60]} "
                f"chaîne={str(_fmt(e.get('channel_name')))[:60]}")
        if e.get("processing"):
            lines.append(f"{head} en traitement (clips pas encore tous produits)")
            continue
        clips = f"clips_produits={_fmt(e.get('clips_produced'))} clips_publiés={_fmt(e.get('clips_published'))}"
        if not e.get("missing"):
            result = f"rang_moyen={_fmt(e.get('views_percentile_mean'))} vues_max={_fmt(e.get('views_at_maturity_max'))}"
        elif e["missing"] == "no_clips":
            result = "aucune vue : aucun clip produit"
        else:
            result = f"vues à maturité inconnues ({e['missing']})"
        lines.append(f"{head} {clips} {result}")
    return lines


_TREND_LEGEND = (
    "tendance_30j : pour chaque jeu suivi, moyennes par jour sur 4 semaines pleines, s4 la plus ancienne → s1 les 7 "
    "derniers jours, ? = aucune mesure cette semaine ; jours_mesurés = jours avec une mesure sur la fenêtre ; un jour "
    "sans mesure est inconnu, pas zéro. avis_steam_par_jour = avis Steam écrits par jour (activité des joueurs) ; "
    "vod_twitch_fr_par_jour = VOD FR encore en ligne publiées ce jour (vues cumulées entre parenthèses).")
_TREND_RULE = (
    "Un jeu dont la tendance monte ou tient (s1 ≥ s2, dernier proche du pic) vaut mieux qu'un pic de sortie déjà "
    "retombé (dernier bien sous le pic, s1 < s2) ; une sortie récente sans courbe en montée n'est pas « ce qui monte ».")
_TREND_NAMES = (("steam_reviews", "avis_steam_par_jour"), ("twitch_vods_fr", "vod_twitch_fr_par_jour"),
                ("twitch_viewers_fr", "viewers_twitch_fr"), ("steam_players_peak", "joueurs_steam_pic"),
                ("steam_players_now", "joueurs_steam_instantane"), ("steam_followers", "abonnes_steam"))


def _trend_point(serie: dict[str, Any], name: str, point: dict[str, Any] | None) -> str:
    """``<date> (<valeur>)`` d'un pic ou dernier point ; ``inconnu`` s'il manque ; VOD : ``<n> VOD (<v> vues)``."""
    if point is None:
        return "inconnu"
    if name != "twitch_vods_fr":
        return f"{point['date']} ({point['value']})"
    views = next((p.get("views") for p in serie["points"] if p["date"] == point["date"]), None)
    return f"{point['date']} ({point['value']} VOD ({_fmt(views)} vues))"


def _pct_label(value: Any) -> str:
    return "inconnu" if value is None else f"{value}%"


def _trend_lines(trend: dict[str, Any] | None) -> list[str]:
    """Une ligne ``tendance_30j`` par série sous un jeu suivi (R27) ; un jeu non suivi n'en a aucune."""
    if not trend:
        return []
    lines: list[str] = []
    for name, label in _TREND_NAMES:
        serie = trend["series"][name]
        if serie["status"] == "unavailable":
            lines.append(f"  tendance_30j {label} : indisponible ({serie['reason']})")
            continue
        summary = serie["summary"]
        weeks = ", ".join("?" if w is None else str(w) for w in summary["weeks"])
        since = f" depuis={serie['since']} (plafond Twitch 500 VOD)" if serie["since"] and serie["since"] > trend["since"] else ""
        lines.append(
            f"  tendance_30j {label} : semaines=[{weeks}] pic={_trend_point(serie, name, summary['peak'])} "
            f"dernier={_trend_point(serie, name, summary['last'])} dernier_vs_pic={_pct_label(summary['last_vs_peak_pct'])} "
            f"s1_vs_s2={_pct_label(summary['s1_vs_s2_pct'])} jours_mesurés={summary['measured_days']}/{summary['window_days']}{since}")
    return lines


def _incomplete_lines(day_state: dict[str, Any], table: dict[str, object]) -> list[str]:
    """R29 : une ligne par source que l'échéance a coupée ou empêchée de commencer ; rien si le relevé est complet.
    Ce qui manque est inconnu, jamais nul : Claude choisit avec ce qui est relevé."""
    if not day_state.get("deadline_hit"):
        return []
    return [f"Relevé incomplet (échéance de {table['veille_deadline_s']} s) : {name} : {entry.get('error')}"
            for name, entry in (day_state.get("sources") or {}).items() if _cut_by_deadline(entry)]


def _prompt(day_state: dict[str, Any], table: dict[str, object]) -> str:
    taste = str(table["taste"]).strip() or "aucune préférence déclarée"
    lines = [
        "Tu choisis, pour un clippeur de streams, les VOD du jour dont tirer des clips courts pour TikTok.",
        f"Goûts de l'utilisateur : {taste}",
        f"Nombre maximum de VOD à proposer : {table['max_vods_per_day']} (zéro est une réponse valide).",
        "Choisis les VOD dont le gameplay se prête à des clips courts compréhensibles seuls ET qui collent aux "
        "goûts, en privilégiant ce qui monte, sans juger les personnes. Une donnée inconnue est inconnue : "
        "ne l'invente pas.",
        f"Au plus {table['max_vods_per_game']} VOD par jeu : varie les jeux.",
        *_incomplete_lines(day_state, table),
        "",
        "Jeux (viewers Twitch FR, joueurs Steam, variation vs moyenne des jours précédents, gain de rang Steam vs semaine dernière, rang et gain dans le top des ventes Steam du pays) :",
        "Deux mesures de joueurs Steam, jamais comparées entre elles : steam_players = pic du jour du top 100 "
        "(inconnu hors top 100) ; steam_players_now = instantané à l'heure du relevé. steam_abonnes = abonnés Steam ; "
        "communaute = au moins un seuil de communauté atteint (joueurs, abonnés, viewers Twitch FR ou hypes IGDB).",
        _TREND_LEGEND,
        _TREND_RULE,
    ]
    for game in day_state["games"]:
        lines.append(
            f"- {game['name']} : twitch_fr_viewers={_fmt(game['twitch_fr_viewers'])} "
            f"hors_twitch_fr={game.get('twitch_match') is False} "
            f"twitch_delta_pct={_fmt(game['twitch_delta_pct'])} steam_players={_fmt(game.get('steam_players'))} "
            f"steam_delta_pct={_fmt(game['steam_delta_pct'])} "
            f"steam_players_now={_fmt(game.get('steam_players_now'))} "
            f"steam_now_delta_pct={_or_insufficient(game.get('steam_now_delta_pct'))} "
            f"steam_abonnes={_fmt(game.get('steam_followers'))} "
            f"steam_abonnes_gain_7j={_or_insufficient(game.get('steam_followers_gain_7d'))} "
            f"communaute={'ok' if (game.get('community') or {}).get('ok') else 'insuffisante'} "
            f"steam_rank_gain_vs_last_week={_fmt(game.get('steam_rank_gain'))} "
            f"steam_new_in_top={_fmt(game.get('steam_new_in_top'))} "
            f"ventes_fr_rang={_fmt(game.get('steam_sellers_rank'))} "
            f"ventes_fr_gain_vs_semaine_derniere={_fmt(game.get('steam_sellers_gain'))} "
            f"ventes_fr_nouveau={_fmt(game.get('steam_sellers_new'))} "
            f"youtube_views_per_hour={_fmt(game.get('youtube_views_per_hour'))} vod_count={_fmt(game.get('vod_count'))} "
            f"sortie_j_plus={_fmt((game.get('release') or {}).get('days_since'))} "
            f"hypes_igdb={_fmt((game.get('release') or {}).get('hypes'))}")
        lines += _trend_lines(game.get("trend_30d"))
    lines += _releases_block(day_state, table)
    lines += _bilan_lines(table)
    lines += ["", "Candidats (VOD) :"]
    games_by_key = {g["key"]: g for g in day_state["games"]}
    for c in day_state["candidates"]:
        signals = c.get("signals") or {}
        community = (games_by_key.get(c.get("game_key")) or {}).get("community") or {}
        lines.append(
            f"- id={c['id']} source={c['source']} titre={c.get('title')!r} chaîne={c.get('channel_name')!r} "
            f"jeu={_fmt(c.get('game_name'))}{' (déduit du titre)' if c.get('game_source') == 'titre' else ''} durée_s={c['duration_s']} publiée={c['published_at']} "
            f"vues={_fmt(c.get('view_count'))} vues_par_heure={_fmt(c.get('views_per_hour'))} "
            f"twitch_delta_pct={_fmt(signals.get('twitch_delta_pct'))} "
            f"steam_delta_pct={_fmt(signals.get('steam_delta_pct'))} "
            f"steam_rank_gain_vs_last_week={_fmt(signals.get('steam_rank_gain'))} "
            f"steam_new_in_top={_fmt(signals.get('steam_new_in_top'))} "
            f"ventes_fr_gain_vs_semaine_derniere={_fmt(signals.get('steam_sellers_gain'))} "
            f"ventes_fr_nouveau={_fmt(signals.get('steam_sellers_new'))} "
            f"sortie_j_plus={_fmt(signals.get('release_days_since'))} "
            f"steam_players={_steam_players_label(community)} steam_abonnes={_fmt(community.get('steam_followers'))} "
            f"twitch_fr_viewers={_fmt(community.get('twitch_fr_viewers'))} hypes_igdb={_fmt(community.get('hypes'))}")
    return "\n".join(lines)


def _model_used(config: Config) -> str | None:
    """Modèle que ``llm.ask("veille")`` utilise vraiment ; ``None`` si la config LLM est elle-même invalide
    (l'appel échoue alors avec ce message, écrit dans ``llm.error``)."""
    try:
        return llm.model_for("veille", config)
    except llm.LLMError:
        return None


def decide(day_state: dict[str, Any], config: Config, now: datetime | None = None) -> dict[str, Any]:
    """Un seul appel texte à ``llm.ask("veille", ...)`` (R6) ; rend une copie de l'état avec ``llm``,
    ``proposals`` et ``skipped_note``. Aucun candidat : ``skipped``, Claude n'est pas appelé. Réponse
    refusée (ou erreur du backend) : ``llm.status = "error"`` avec le message, aucune proposition. Limite de
    session / réseau (``TransientLLMError``) : ``llm.status = "retry"`` avec ``retry_at`` (``now`` +
    ``llm_retry_delay_min``) et ``attempts`` ; au-delà de ``llm_retry_max`` reprises, ``"error"`` explicite.
    Chaque proposition porte un instantané de son candidat (``candidate``) : l'écran l'affiche encore
    une fois la VOD mise en file, quand elle n'est plus candidate."""
    table = settings(config)
    state = {**day_state}
    candidates = state["candidates"]
    model = _model_used(config)
    if not candidates:
        state.update(llm={"status": "skipped", "error": None, "model": None}, proposals=[], skipped_note="")
        return state
    try:
        answer = llm.ask("veille", _prompt(state, table), [], _schema(int(table["max_vods_per_day"])),
                         config=config, usage_log_path=_state_dir(table) / "llm_usage.jsonl",
                         check=_check_picks(candidates, int(table["max_vods_per_game"])))  # type: ignore[call-overload]
    except llm.TransientLLMError as exc:
        attempts = int(state.get("llm", {}).get("attempts") or 0) + 1
        if attempts > int(table["llm_retry_max"]):  # type: ignore[call-overload]
            message = f"{exc} (abandon après {attempts} tentatives)"
            log.error("veille : choix de Claude impossible : %s", message)
            state.update(llm={"status": "error", "error": message, "model": model}, proposals=[], skipped_note="")
            return state
        retry_at = ((now or datetime.now(timezone.utc))
                    + timedelta(minutes=int(table["llm_retry_delay_min"]))).isoformat()  # type: ignore[call-overload]
        log.warning("veille : choix de Claude reporté à %s (tentative %d) : %s", retry_at, attempts, exc)
        state.update(llm={"status": "retry", "error": str(exc), "model": model, "retry_at": retry_at,
                          "attempts": attempts}, proposals=[], skipped_note="")
        return state
    except llm.LLMError as exc:
        log.error("veille : choix de Claude refusé : %s", exc)
        state.update(llm={"status": "error", "error": str(exc), "model": model}, proposals=[], skipped_note="")
        return state
    by_id = {c["id"]: c for c in candidates}
    state["proposals"] = [
        {"candidate_id": pick["candidate_id"], "rank": rank, "reason": pick["reason"], "status": "proposed",
         "decided_at": None, "channel": None, "queue_entry_id": None, "candidate": by_id[pick["candidate_id"]]}
        for rank, pick in enumerate(answer["picks"], start=1)]
    state.update(llm={"status": "ok", "error": None, "model": model}, skipped_note=answer["skipped_note"])
    return state


# --------------------------------------------------------------------------
# Exécution par le worker (R7)
# --------------------------------------------------------------------------


def _day_path(sdir: Path, day: str) -> Path:
    return sdir / "days" / f"{day}.json"


def _state_lock(sdir: Path) -> Path:
    return sdir / "state"


def _read_day(sdir: Path, day: str) -> dict[str, Any] | None:
    return _read_json(_day_path(sdir, day), None)


def _due(now: datetime, table: dict[str, object], sdir: Path) -> bool:
    if (sdir / "refresh.json").exists():
        return True
    local = now.astimezone(ZoneInfo(str(table["timezone"])))
    hour, minute = str(table["run_at"]).split(":")
    if (local.hour, local.minute) < (int(hour), int(minute)):
        return False
    day = _read_day(sdir, local.date().isoformat())
    return day is None or day.get("finished_at") is None  # un relevé interrompu est repris


def run_if_due(
    now: datetime,
    config: Config,
    collectors: dict[str, Collector] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any] | None:
    """Relevé + choix de Claude si dû (R7) ; rend l'état du jour, ``None`` si rien n'était dû.
    ``enabled`` faux : rien n'est lu ni écrit. ``refresh.json`` est consommé avant de commencer ;
    un relevé rejoué le même jour efface puis remplace toute la liste des propositions du jour, décidées
    comprises (SPEC-8a45 R31) ; ``seen.json``, ``history/``, ``selection/``, ``bilan.json``, la file et les vidéos
    restent intacts."""
    if not config.section("veille").get("enabled"):
        return None
    table = settings(config)
    sdir = _state_dir(table)
    day = now.astimezone(ZoneInfo(str(table["timezone"]))).date().isoformat()
    if not (sdir / "refresh.json").exists():
        retry = _read_day(sdir, day)
        if retry is not None and retry.get("llm", {}).get("status") == "retry":
            return _retry_decide(now, config, sdir, day, retry)
    if not _due(now, table, sdir):
        return None
    refresh = _read_json(sdir / "refresh.json", None)
    (sdir / "refresh.json").unlink(missing_ok=True)
    requested_at = refresh.get("requested_at") if isinstance(refresh, dict) else None
    skeleton = {
        "date": day, "sources": {}, "games": [], "candidates": [], "excluded": {},
        "llm": {"status": "skipped", "error": None, "model": None}, "proposals": [], "skipped_note": ""}
    skeleton.update(started_at=now.isoformat(), finished_at=None, refresh_requested_at=requested_at)
    _write(_day_path(sdir, day), skeleton)  # l'écran voit « en cours » dès maintenant

    read_clock = clock or _elapsed_clock(now)  # la même horloge pour l'échéance et pour l'heure de fin
    try:
        state = collect(now, collectors=collectors, config=config, finalize=False, clock=read_clock)
    except Exception as exc:  # noqa: BLE001 : le jour finit en erreur, le relevé n'est pas rejoué à chaque tour (ADR-ad2e)
        log.exception("veille : le relevé a échoué")
        failed = {**skeleton, "llm": {"status": "error", "error": f"relevé interrompu : {exc}", "model": _model_used(config)},
                  "finished_at": read_clock().isoformat()}
        with channel_mod.file_lock(_state_lock(sdir)):
            _write(_day_path(sdir, day), failed)
        raise
    state["refresh_requested_at"] = requested_at
    state = _safe_decide(state, config, now)
    state["finished_at"] = read_clock().isoformat()  # vraie heure de fin, jamais celle du départ
    with channel_mod.file_lock(_state_lock(sdir)):
        _write(_day_path(sdir, day), state)
    return state


def _safe_decide(state: dict[str, Any], config: Config, now: datetime) -> dict[str, Any]:
    """``decide`` dont une exception inattendue est écrite dans l'état du jour (journal ERROR, aucun choix inventé,
    ADR-ad2e) : le jour est alors fini et le relevé n'est pas relancé à chaque tour du worker."""
    try:
        return decide(state, config, now)
    except Exception as exc:  # noqa: BLE001 : tout échec du choix finit le jour avec son erreur
        log.exception("veille : le choix de Claude a échoué")
        model = _model_used(config)
        return {**state, "llm": {"status": "error", "error": f"{type(exc).__name__} : {exc}", "model": model},
                "proposals": [], "skipped_note": ""}


def _retry_decide(now: datetime, config: Config, sdir: Path, day: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """Reprend le seul choix d'un jour en attente de ``retry_at`` (limite de session) ; ``None`` avant l'échéance."""
    retry_at = state["llm"].get("retry_at")
    if not retry_at or now < datetime.fromisoformat(retry_at):
        return None
    state = _safe_decide(state, config, now)
    with channel_mod.file_lock(_state_lock(sdir)):
        _write(_day_path(sdir, day), state)
    return state


def _snapshot(candidate: dict[str, Any]) -> dict[str, Any]:
    """Titre, jeu et chaîne gardés dans ``seen.json`` à la décision : le bilan les lit même si le jour est rejoué (R32)."""
    return {key: candidate.get(key) for key in ("source", "title", "game_name", "channel_name")}


def _update_proposal(
    config: Config, day: str, candidate_id: str,
    update: Callable[[dict[str, Any], dict[str, Any]], None],
) -> None:
    """Relit le jour sous verrou, applique ``update(proposition, seen)``, réécrit les deux fichiers."""
    sdir = _state_dir(settings(config))
    with channel_mod.file_lock(_state_lock(sdir)):
        state = _read_day(sdir, day)
        if state is None:
            raise VeilleError(f"aucun relevé de veille pour le {day}")
        proposal = next((p for p in state["proposals"] if p["candidate_id"] == candidate_id), None)
        if proposal is None:
            raise VeilleError(f"candidat inconnu : {candidate_id} (relevé du {day})")
        if proposal["status"] != "proposed":
            raise VeilleError(f"candidat déjà traité : {candidate_id} ({proposal['status']})")
        seen = _read_json(sdir / "seen.json", {"queued": [], "ignored": []})
        update(proposal, seen)
        _write(sdir / "seen.json", seen)
        _write(_day_path(sdir, day), state)


def clip(
    day: str,
    candidate_id: str,
    channel: str | None,
    short_clips: bool | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """Met la VOD proposée en file (``worker.enqueue``) ; rend l'entrée de file (R7)."""
    from clipper import worker  # import local : worker importe veille dans sa boucle

    config = config or load_config()
    result: dict[str, Any] = {}

    def update(proposal: dict[str, Any], seen: dict[str, Any]) -> None:
        candidate = proposal["candidate"]
        try:
            entry = worker.enqueue(candidate["url"], channel, "run", short_clips=short_clips, config=config)
        except worker.WorkerError as exc:
            raise VeilleError(f"mise en file impossible pour {candidate_id} : {exc}") from exc
        at = datetime.now(timezone.utc).isoformat()
        proposal.update(status="queued", decided_at=at, channel=channel, queue_entry_id=entry["id"])
        seen["queued"].append({"candidate_id": candidate_id, "video_id": entry["video_id"], "url": candidate["url"],
                               "date": day, "channel": channel, "queue_entry_id": entry["id"], "at": at,
                               **_snapshot(candidate)})
        result.update(entry)

    _update_proposal(config, day, candidate_id, update)
    return result


def ignore(day: str, candidate_id: str, config: Config | None = None) -> None:
    """Marque la proposition ignorée ; son ``video_id`` n'est plus jamais candidat (R7)."""
    config = config or load_config()

    def update(proposal: dict[str, Any], seen: dict[str, Any]) -> None:
        at = datetime.now(timezone.utc).isoformat()
        proposal.update(status="ignored", decided_at=at)
        seen["ignored"].append({"candidate_id": candidate_id, "video_id": proposal["candidate"]["video_id"],
                                "date": day, "at": at, **_snapshot(proposal["candidate"])})

    _update_proposal(config, day, candidate_id, update)


# --------------------------------------------------------------------------
# Meilleurs clips du jour (R7) : archivage réversible, output/ jamais touché
# --------------------------------------------------------------------------

_KEEP_PUBLISH_STATUSES = ("approved", "scheduled", "published")


def _selection_path(sdir: Path, day: str) -> Path:
    return sdir / "selection" / f"{day}.json"


def _series_key(clip_id: str, sidecar: dict[str, Any]) -> str:
    """Base commune des parties d'une série (même règle que clipper.publish), sinon le clip lui-même."""
    if sidecar.get("part") is not None and (sidecar.get("parts_total") or 1) > 1 and "-p" in clip_id:
        return clip_id.rsplit("-p", 1)[0]
    return clip_id


def _protected_clips(config: Config) -> set[tuple[str, str]]:
    """Clips dont l'entrée de publication est approuvée, programmée ou publiée (jamais archivés)."""
    folder = Path(str(config.section("publish")["state_dir"]))
    names = sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []
    try:
        return {(e["video_id"], e["clip_id"]) for name in names
                for e in publish.list_entries(name, state_dir=folder) if e["status"] in _KEEP_PUBLISH_STATUSES}
    except (publish.PublishError, ValueError) as exc:
        raise VeilleError(f"file de publication illisible : {exc}") from exc


def _video_clips(config: Config, video_id: str) -> list[dict[str, Any]]:
    folder = Path(config.output_dir) / video_id
    clips = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            sidecar = publish.read_sidecar(config.output_dir, video_id, path.stem)
            clips.append({"video_id": video_id, "clip_id": path.stem, "qa": sidecar["qa"]["status"],
                          "score": sidecar["score"], "series": _series_key(path.stem, sidecar)})
        except (publish.PublishError, ValueError, KeyError, TypeError) as exc:
            raise VeilleError(f"sidecar de clip illisible : {path} ({exc})") from exc
    return clips


def _finished_day(config: Config, video_id: str, tz: ZoneInfo) -> str | None:
    state = _read_json(Path(config.workspace_dir) / video_id / "pipeline.json", None)
    if not state or state.get("status") != "done" or not state.get("updated_at"):
        return None
    return datetime.fromisoformat(state["updated_at"]).astimezone(tz).date().isoformat()


def select_best(now: datetime, config: Config) -> None:
    """Recalcule ``selection/<jour>.json`` pour chaque jour où une VOD de la veille s'est terminée (R7)."""
    table = settings(config)
    sdir = _state_dir(table)
    tz = ZoneInfo(str(table["timezone"]))
    seen = _read_json(sdir / "seen.json", {"queued": [], "ignored": []})
    videos_by_day: dict[str, list[str]] = {}
    for entry in seen.get("queued", []):
        day = _finished_day(config, entry["video_id"], tz)
        if day and entry["video_id"] not in videos_by_day.setdefault(day, []):
            videos_by_day[day].append(entry["video_id"])
    if not videos_by_day:
        return
    protected = _protected_clips(config)
    best = int(table["best_clips_per_day"])
    for day, video_ids in sorted(videos_by_day.items()):
        units: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for video_id in video_ids:
            for item in _video_clips(config, video_id):
                units.setdefault((video_id, item["series"]), []).append(item)
        # une série compte pour un, au score de la série ; une partie rejetée écarte toute la série
        ranked = sorted(
            ((max(i["score"] for i in items), key, items) for key, items in units.items()
             if all(i["qa"] == "passed" for i in items)),
            key=lambda u: (-u[0], u[1]))
        with channel_mod.file_lock(_state_lock(sdir)):
            restored = (_read_json(_selection_path(sdir, day), {}) or {}).get("restored", [])
            restored_ids = {(r["video_id"], r["clip_id"]) for r in restored}
            kept: list[dict[str, Any]] = []
            archived: list[dict[str, Any]] = []
            for rank, (score, _key, items) in enumerate(ranked, start=1):
                rows = [{"video_id": i["video_id"], "clip_id": i["clip_id"], "score": score, "rank": rank}
                        for i in items]
                if rank <= best or any((i["video_id"], i["clip_id"]) in protected for i in items):
                    kept.extend(rows)
                else:
                    archived.extend(r for r in rows if (r["video_id"], r["clip_id"]) not in restored_ids)
            _write(_selection_path(sdir, day), {
                "date": day, "computed_at": now.isoformat(), "kept": kept, "archived": archived,
                "restored": restored})


def restore(video_id: str, clip_id: str, config: Config | None = None) -> None:
    """Sort un clip de ``archived`` et le met dans ``restored`` : il reste visible aux recalculs suivants (R7)."""
    config = config or load_config()
    sdir = _state_dir(settings(config))
    folder = sdir / "selection"
    with channel_mod.file_lock(_state_lock(sdir)):
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            selection = _read_json(path, {})
            match = [a for a in selection.get("archived", []) if a["video_id"] == video_id and a["clip_id"] == clip_id]
            if match:
                selection["archived"] = [a for a in selection["archived"] if a not in match]
                selection.setdefault("restored", []).append({
                    "video_id": video_id, "clip_id": clip_id, "restored_at": datetime.now(timezone.utc).isoformat()})
                _write(path, selection)
                return
    raise VeilleError(f"clip non archivé par la veille : {video_id}/{clip_id}")
