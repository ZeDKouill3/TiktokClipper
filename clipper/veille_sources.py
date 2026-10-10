"""Collecteurs réels de la veille : Twitch Helix, YouTube Data API v3, Steam Web API
(ADR-ca9a, SPEC-bdd9 R3).

Bibliothèque : aucun import de ``clipper.web`` ni d'une étape, aucun import de
``clipper.veille`` (c'est elle qui importe ce module). Toutes les requêtes
passent par un transport injectable ::

    http(method, url, *, params, headers, timeout_s) -> (status, body)

``body`` est le JSON décodé, ou le texte brut si la réponse n'est pas du JSON
(le collecteur lève alors une ``SourceError``). Le transport par défaut
(``default_http``) s'appuie sur httpx.

Les collecteurs rendent les formes décrites dans ``clipper.veille``. Une
``SourceError`` porte le code HTTP, l'URL sans paramètre secret et le début de
la réponse ; aucun secret (clé API, secret client, jeton) n'apparaît dans un
message ni dans un fichier d'état (R3).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
import yt_dlp

from clipper import channel as channel_mod

log = logging.getLogger(__name__)
Http = Callable[..., "tuple[int, Any]"]
Clock = Callable[[], datetime]
Deadline = Callable[[], float]  # secondes restantes avant l'échéance globale du relevé (SPEC-85a0 R29)

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API = "https://api.twitch.tv/helix"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
IGDB_GAMES_URL = "https://api.igdb.com/v4/games"
IGDB_STEAM_SOURCE = 1  # external_games.external_game_source de Steam
IGDB_PAGE_SIZE = 500
IGDB_MIN_INTERVAL_S = 0.25  # 4 requêtes/s (doc IGDB #rate-limits)
STEAM_API = "https://api.steampowered.com"
STEAM_SELLERS_URL = f"{STEAM_API}/IStoreTopSellersService/GetWeeklyTopSellers/v1/"
# Code de langue [veille] language -> nom de langue attendu par l'API magasin (autres valeurs passées telles quelles).
_STEAM_LANGUAGES = {"fr": "french", "en": "english", "de": "german", "es": "spanish", "it": "italian", "pt": "portuguese"}
STEAM_CONCURRENT_URL = f"{STEAM_API}/ISteamChartsService/GetGamesByConcurrentPlayers/v1/"
STEAM_PLAYERS_URL = f"{STEAM_API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
STEAM_MEMBERS_URL = "https://steamcommunity.com/games/{appid}/memberslistxml/?xml=1"
STEAM_USER_AGENT = "Clipper/1.0 (veille ; lit seulement memberCount)"
_MEMBER_COUNT = re.compile(r"<memberCount>\s*(\d+)\s*</memberCount>")
STEAM_REVIEWS_URL = "https://store.steampowered.com/appreviewhistogram/{appid}"
STEAM_REVIEWS_USER_AGENT = "Clipper/1.0 (veille ; lit seulement l'histogramme des avis)"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"  # GetAppList v2 retiré par Valve (404)

STREAM_PAGES_MAX = 5
_TOKEN_MARGIN = timedelta(seconds=60)
_SECRET_PARAMS = {"key", "client_secret", "access_token"}
_SNIPPET_CHARS = 200

_TWITCH_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_ISO_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


class SourceError(Exception):
    """Une source n'a pas pu être relevée (HTTP, JSON, champ absent)."""


class RateLimited(SourceError):
    """HTTP 429 : ``retry_after`` est l'en-tête ``Retry-After`` brut (secondes ou date HTTP), ``reset`` l'en-tête
    ``Ratelimit-Reset`` brut (Helix : instant Unix), ``None`` s'ils manquent."""

    def __init__(self, message: str, retry_after: str | None = None, reset: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.reset = reset


class AccessRestricted(Exception):
    """La VOD est réservée aux abonnés (refus d'accès de yt-dlp), pas une panne réseau."""


# Messages yt-dlp d'un contenu réservé ; tout autre message (réseau, délai...) n'en est pas un.
_RESTRICTED = re.compile(r"subscriber[- ]?only|subscribers[- ]only|sub[- ]only|logged into an account that has access", re.IGNORECASE)


def check_twitch_access(url: str, timeout_s: float, *, ydl_factory: Callable[[dict[str, Any]], Any] | None = None) -> None:
    """Teste l'accès d'une VOD Twitch par yt-dlp, sans rien télécharger (``download=False``).
    Rend ``None`` si elle est lisible, lève ``AccessRestricted`` si elle est réservée aux abonnés ;
    toute autre erreur (réseau, connexion fermée, délai) remonte telle quelle à l'appelant."""
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": timeout_s, "skip_download": True}
    try:
        with (ydl_factory or yt_dlp.YoutubeDL)(opts) as ydl:
            ydl.extract_info(url, download=False)
    except Exception as exc:
        if _RESTRICTED.search(str(exc)):
            raise AccessRestricted(str(exc)) from exc
        raise


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _over(deadline: Deadline | None) -> bool:
    """L'échéance globale est passée : le collecteur s'arrête avant sa prochaine requête, pause ou essai (R29)."""
    return deadline is not None and deadline() <= 0


def _stopped(result: dict[str, Any], count: int) -> dict[str, Any]:
    """``deadline_stopped`` = éléments non relevés ; la clé n'existe que si l'échéance a coupé le relevé."""
    if count > 0:
        result["deadline_stopped"] = count
    return result


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def default_http(method: str, url: str, *, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None, timeout_s: float = 20,
                 content: str | None = None) -> tuple[int, Any] | tuple[int, Any, dict[str, str]]:
    """Transport httpx. Une erreur réseau devient une ``SourceError`` sans paramètres.
    ``content`` : corps texte de la requête (IGDB, Apicalypse)."""
    try:
        response = httpx.request(method, url, params=params, headers=headers, content=content, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise SourceError(f"requête impossible sur {url} ({type(exc).__name__})") from None
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    if response.status_code == 429:  # en-têtes joints : seulement pour un 429
        kept = {name: response.headers[name] for name in ("Retry-After", "Ratelimit-Reset") if response.headers.get(name)}
        if kept:
            return response.status_code, body, kept
    return response.status_code, body


def _safe_url(url: str, params: dict[str, Any] | None) -> str:
    public = {k: v for k, v in (params or {}).items() if k not in _SECRET_PARAMS}
    return f"{url}?{urlencode(public)}" if public else url


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


class _Client:
    """Appelle le transport et traduit tout échec en ``SourceError`` expurgée."""

    def __init__(self, http: Http, timeout_s: float, secrets: tuple[str, ...]):
        self.http, self.timeout_s, self.secrets = http, timeout_s, secrets

    def request(self, method: str, url: str, params: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None, *, accept_401: bool = False,
                content: str | None = None, on_429: str | None = None, accept: tuple[int, ...] = (),
                text: bool = False) -> tuple[int, Any]:
        """``(status, body JSON)`` ; 401 rendu tel quel seulement si ``accept_401`` ; 429 : ``SourceError(on_429)`` ;
        un statut de ``accept`` est rendu tel quel ; ``text`` : le corps est un texte non vide (XML), pas du JSON."""
        shown = _safe_url(url, params)
        extra = {"content": content} if content is not None else {}  # un transport sans corps n'a rien à recevoir
        try:
            reply = self.http(method, url, params=params, headers=headers, timeout_s=self.timeout_s, **extra)
            status, body = reply[0], reply[1]
            reply_headers = reply[2] if len(reply) > 2 else {}  # un transport peut joindre les en-têtes de réponse
        except SourceError:
            raise
        except Exception as exc:  # le transport injecté peut lever n'importe quoi
            raise SourceError(_redact(f"requête impossible sur {shown} ({type(exc).__name__} : {exc})",
                                      self.secrets)) from None
        if accept_401 and status == 401:
            return status, body
        if on_429 and status == 429:
            raise SourceError(on_429)
        snippet = _redact(str(body)[:_SNIPPET_CHARS], self.secrets)
        if status in accept:
            return status, body
        if status == 429:
            retry_after = next((v for k, v in reply_headers.items() if k.lower() == "retry-after"), None)
            reset = next((v for k, v in reply_headers.items() if k.lower() == "ratelimit-reset"), None)
            raise RateLimited(f"HTTP 429 sur {shown} : {snippet}", retry_after, reset)
        if not 200 <= status < 300:
            raise SourceError(f"HTTP {status} sur {shown} : {snippet}")
        if text:
            if not isinstance(body, str) or not body.strip():
                raise SourceError(f"HTTP {status} sur {shown} : corps vide ou illisible : {snippet}")
            return status, body
        if not isinstance(body, (dict, list)):
            raise SourceError(f"HTTP {status} sur {shown} : réponse JSON illisible : {snippet}")
        return status, body

    def fail(self, url: str, params: dict[str, Any] | None, body: Any, what: str) -> SourceError:
        snippet = _redact(str(body)[:_SNIPPET_CHARS], self.secrets)
        return SourceError(f"champ attendu absent ({what}) sur {_safe_url(url, params)} : {snippet}")


def _field(client: _Client, url: str, params: dict[str, Any] | None, body: Any, *path: str) -> Any:
    cur = body
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            raise client.fail(url, params, body, ".".join(path))
        cur = cur[key]
    return cur


# --------------------------------------------------------------------------
# Durées et dates
# --------------------------------------------------------------------------


def parse_twitch_duration(text: str) -> int:
    """« 3h2m1s » → 10921, « 45m » → 2700."""
    match = _TWITCH_DURATION.match(text or "")
    if not match or not text:
        raise SourceError(f"durée Twitch illisible : {text!r}")
    h, m, s = (int(g or 0) for g in match.groups())
    return h * 3600 + m * 60 + s


def parse_iso_duration(text: str) -> int:
    """ISO 8601 « PT1H2M3S » → 3723."""
    match = _ISO_DURATION.match(text or "")
    if not match:
        raise SourceError(f"durée ISO 8601 illisible : {text!r}")
    d, h, m, s = (int(g or 0) for g in match.groups())
    return d * 86400 + h * 3600 + m * 60 + s


TWITCH_THUMB_SIZE = ("640", "360")


def _twitch_thumbnail(raw: Any) -> str | None:
    """URL de miniature d'une VOD Twitch à taille fixe ; ``None`` si absente, vide ou en cours de traitement."""
    if not isinstance(raw, str) or not raw or "404_processing" in raw:
        return None
    return raw.replace("%{width}", TWITCH_THUMB_SIZE[0]).replace("%{height}", TWITCH_THUMB_SIZE[1])


def _youtube_thumbnail(thumbnails: Any) -> str | None:
    """La plus grande miniature de ``snippet.thumbnails`` ; ``None`` si aucune."""
    if not isinstance(thumbnails, dict):
        return None
    sized = [t for t in thumbnails.values() if isinstance(t, dict) and t.get("url")]
    if not sized:
        return None
    return str(max(sized, key=lambda t: int(t.get("width") or 0))["url"])


def _views_per_hour(view_count: int | None, published_at: str, now: datetime) -> float | None:
    if view_count is None:
        return None
    hours = (now - datetime.fromisoformat(published_at)).total_seconds() / 3600
    return view_count / max(1.0, hours)


def _read_json(path: Path) -> Any:
    """Cache illisible ou absent : ``None`` (le cache se refait, ce n'est pas une donnée)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sdir(settings: dict[str, object]) -> Path:
    return Path(str(settings["state_dir"]))


def _client(settings: dict[str, object], http: Http, secrets: tuple[str, ...]) -> _Client:
    return _Client(http, float(settings["http_timeout_s"]), secrets)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Twitch
# --------------------------------------------------------------------------


class _Twitch:
    def __init__(self, settings: dict[str, object], http: Http, clock: Clock):
        self.settings, self.clock = settings, clock
        self.client_id = str(settings["twitch_client_id"])
        self.secret = str(settings["twitch_client_secret"])
        self.client = _client(settings, http, (self.secret,))
        self.token_path = _sdir(settings) / "twitch_token.json"
        self.token: str | None = None

    def _cached_token(self) -> str | None:
        data = _read_json(self.token_path)
        try:
            if datetime.fromisoformat(data["expires_at"]) - _TOKEN_MARGIN > self.clock():
                return str(data["access_token"])
        except (KeyError, TypeError, ValueError):
            pass
        return None

    def _fetch_token(self) -> str:
        params = {"client_id": self.client_id, "client_secret": self.secret, "grant_type": "client_credentials"}
        _, body = self.client.request("POST", TWITCH_TOKEN_URL, params)
        token = str(_field(self.client, TWITCH_TOKEN_URL, params, body, "access_token"))
        self.client.secrets += (token,)  # avant de lire expires_in : son absence cite le corps, jeton compris
        expires_in = int(_field(self.client, TWITCH_TOKEN_URL, params, body, "expires_in"))
        path = self.token_path
        with channel_mod.file_lock(path):
            channel_mod.atomic_write_json(path, {
                "access_token": token, "expires_at": (self.clock() + timedelta(seconds=expires_in)).isoformat()})
        return token

    def ensure_token(self) -> str:
        if self.token is None:
            self.token = self._cached_token() or self._fetch_token()
            self.client.secrets += (self.token,)
        return self.token

    def call(self, method: str, url: str, params: dict[str, Any] | None, headers: dict[str, str],
             **kwargs: Any) -> Any:
        """Requête avec le jeton Twitch ; un 401 renouvelle le jeton une fois, puis on réessaie."""
        for attempt in (1, 2):
            status, body = self.client.request(
                method, url, params, {**headers, "Authorization": f"Bearer {self.ensure_token()}"},
                accept_401=attempt == 1, **kwargs)
            if status != 401:
                break
            self.token = None  # jeton refusé : un seul renouvellement
            self.token_path.unlink(missing_ok=True)
        return body

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{TWITCH_API}/{path}"
        body = self.call("GET", url, params, {"Client-Id": self.client_id})
        if not isinstance(body, dict):
            raise self.client.fail(url, params, body, "objet JSON")
        return body


def _twitch_collector(http: Http, clock: Clock) -> Callable[..., dict[str, Any]]:
    def collect(settings: dict[str, object], *, deadline: Deadline | None = None) -> dict[str, Any]:
        api = _Twitch(settings, http, clock)
        language = str(settings["language"])
        now = clock()
        stopped = 0  # éléments non relevés à l'échéance (une page ou un jeu chacun)

        viewers: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        for _ in range(STREAM_PAGES_MAX):
            if _over(deadline):
                stopped += 1  # au moins une page de plus restait à lire
                break
            params: dict[str, Any] = {"language": language, "first": 100}
            if cursor:
                params["after"] = cursor
            body = api.get("streams", params)
            for stream in _field(api.client, f"{TWITCH_API}/streams", params, body, "data"):
                game_id = stream.get("game_id")
                if not game_id:
                    continue
                entry = viewers.setdefault(str(game_id), {"name": stream.get("game_name"), "viewers_fr": 0})
                entry["viewers_fr"] += int(stream.get("viewer_count", 0))
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor:
                break

        top_params = {"first": int(settings["twitch_top_games"])}  # type: ignore[call-overload]
        if _over(deadline):
            stopped += 1
            top_games: list[dict[str, Any]] = []
        else:
            top = api.get("games/top", top_params)
            top_games = [g for g in _field(api.client, f"{TWITCH_API}/games/top", top_params, top, "data")
                         if "id" in g and "name" in g]
        names = {str(g["id"]): g["name"] for g in top_games}
        igdb_ids = {str(g["id"]): str(g.get("igdb_id") or "") for g in top_games}  # vide : Twitch n'a pas l'id IGDB
        for game_id, entry in viewers.items():
            entry["name"] = entry["name"] or names.get(game_id)
        named = {gid: e for gid, e in viewers.items() if e["name"]}  # un jeu sans nom n'est pas relevé
        ranked = sorted(named.items(), key=lambda kv: (-kv[1]["viewers_fr"], kv[1]["name"]))

        vods: list[dict[str, Any]] = []
        private_vods = 0
        queried = ranked[: int(settings["twitch_top_games"])]  # type: ignore[call-overload]
        for index, (game_id, entry) in enumerate(queried):
            if _over(deadline):
                stopped += len(queried) - index
                break
            params = {"game_id": game_id, "language": language, "period": "day", "sort": "views",
                      "type": "archive", "first": int(settings["twitch_vods_per_game"])}  # type: ignore[call-overload]
            body = api.get("videos", params)
            for video in _field(api.client, f"{TWITCH_API}/videos", params, body, "data"):
                where = f"{TWITCH_API}/videos"
                for key in ("id", "url", "duration", "published_at"):
                    if key not in video:
                        raise api.client.fail(where, params, video, f"data[].{key}")
                if video.get("viewable", "public") != "public":
                    private_vods += 1  # réservée aux abonnés ou privée : jamais proposée
                    continue
                view_count = video.get("view_count")
                vods.append({
                    "video_id": str(video["id"]), "url": video["url"], "title": video.get("title"),
                    "channel_name": video.get("user_name"), "game_name": entry["name"],
                    "duration_s": parse_twitch_duration(video["duration"]),
                    "published_at": video["published_at"], "view_count": view_count,
                    "thumbnail_url": _twitch_thumbnail(video.get("thumbnail_url")),
                    "views_per_hour": _views_per_hour(view_count, video["published_at"], now),
                })
        return _stopped({"games": [{"name": e["name"], "viewers_fr": e["viewers_fr"], "igdb_id": igdb_ids.get(gid, ""),
                                    "twitch_id": gid} for gid, e in ranked], "vods": vods,
                         "private_vods": private_vods}, stopped)

    return collect


# --------------------------------------------------------------------------
# IGDB (ADR-798c, ADR-0944, SPEC-df51 R12)
# --------------------------------------------------------------------------


class _Pacer:
    """Espace les requêtes IGDB d'au moins ``IGDB_MIN_INTERVAL_S`` (horloge et attente injectées)."""

    def __init__(self, clock: Clock, sleep: Callable[[float], None]):
        self.clock, self.sleep, self.last = clock, sleep, None

    def wait(self) -> None:
        if self.last is not None:
            remaining = IGDB_MIN_INTERVAL_S - (self.clock() - self.last).total_seconds()
            if remaining > 0:
                self.sleep(remaining)

    def mark(self) -> None:
        self.last = self.clock()


def _midnight_utc(day: Any) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _igdb_dated(line: Any) -> dict[str, Any] | None:
    """Une ligne ``release_dates`` d'un jeu ; ``None`` si elle n'a pas de date (ligne « TBD »)."""
    ts = line.get("date") if isinstance(line, dict) else None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None

    def sub(key: str, field: str) -> Any:
        value = line.get(key)
        return value.get(field) if isinstance(value, dict) else None

    return {"date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"), "ts": ts,
            "human": line.get("human"), "platform": sub("platform", "name"), "region": sub("release_region", "region"),
            "status": sub("status", "name"), "date_format": sub("date_format", "format")}


def _steam_appid(game: dict[str, Any]) -> str | None:
    """``uid`` de la première ``external_games`` dont la source vaut Steam (1) ; ``category`` n'est jamais lu."""
    for external in game.get("external_games") or []:
        if isinstance(external, dict) and external.get("external_game_source") == IGDB_STEAM_SOURCE \
                and external.get("uid") not in (None, ""):
            return str(external["uid"])
    return None


def _igdb_game(client: _Client, game: Any) -> dict[str, Any] | None:
    """Un jeu IGDB ; ``None`` s'il est inexploitable (sans nom, sans hypes entier, sans ligne datée)."""
    if not isinstance(game, dict):
        return None
    if "id" not in game:
        raise client.fail(IGDB_GAMES_URL, None, game, "id")
    hypes = game.get("hypes")
    dated = [d for d in (_igdb_dated(line) for line in game.get("release_dates") or []) if d is not None]
    if not game.get("name") or isinstance(hypes, bool) or not isinstance(hypes, int) or not dated:
        return None
    cover = game.get("cover")
    return {
        "igdb_id": str(game["id"]), "name": game["name"], "slug": game.get("slug"), "url": game.get("url"),
        "hypes": hypes, "first_release_date": game.get("first_release_date"),
        "cover_image_id": cover.get("image_id") if isinstance(cover, dict) else None,
        "steam_appid": _steam_appid(game), "release_dates": dated,
    }


def _igdb_collector(http: Http, clock: Clock, sleep: Callable[[float], None]) -> Callable[..., dict[str, Any]]:
    def collect(settings: dict[str, object], *, deadline: Deadline | None = None) -> dict[str, Any]:
        if _over(deadline):
            return _stopped({"games": [], "skipped_rows": 0}, 1)  # aucune requête, pas même le jeton
        api = _Twitch(settings, http, clock)
        pacer = _Pacer(clock, sleep)
        today = clock().astimezone(ZoneInfo(str(settings["timezone"]))).date()
        start = _midnight_utc(today - timedelta(days=int(settings["release_window_days"])))  # type: ignore[call-overload]
        end = _midnight_utc(today + timedelta(days=int(settings["upcoming_days"]) + 1))  # type: ignore[call-overload]
        headers = {"Client-ID": api.client_id, "Accept": "application/json"}
        too_many = "IGDB : limite de 4 requêtes/s dépassée (HTTP 429)"
        games: list[dict[str, Any]] = []
        skipped = 0
        stopped = 0
        for page in range(int(settings["igdb_pages_max"])):  # type: ignore[call-overload]
            if page and _over(deadline):
                stopped += 1  # au moins une page de plus restait à lire
                break
            body = (
                "fields name,slug,url,hypes,first_release_date,cover.image_id,external_games.uid,"
                "external_games.external_game_source,release_dates.date,release_dates.human,release_dates.platform.name,"
                "release_dates.release_region.region,release_dates.status.name,release_dates.date_format.format; "
                f"where release_dates.date >= {start} & release_dates.date < {end} & hypes >= 1; sort hypes desc; "
                f"limit {IGDB_PAGE_SIZE}; offset {page * IGDB_PAGE_SIZE};")
            pacer.wait()
            rows = api.call("POST", IGDB_GAMES_URL, None, headers, content=body, on_429=too_many)
            pacer.mark()
            if not isinstance(rows, list):
                raise api.client.fail(IGDB_GAMES_URL, None, rows, "tableau JSON")
            for row in rows:
                game = _igdb_game(api.client, row)
                if game is None:
                    skipped += 1
                else:
                    games.append(game)
            if len(rows) < IGDB_PAGE_SIZE:
                break
        return _stopped({"games": games, "skipped_rows": skipped}, stopped)

    return collect


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------


def _youtube_collector(http: Http, clock: Clock) -> Callable[..., dict[str, Any]]:
    def collect(settings: dict[str, object], *, deadline: Deadline | None = None) -> dict[str, Any]:
        if _over(deadline):
            return _stopped({"videos": []}, 1)
        api_key = str(settings["youtube_api_key"])
        client = _client(settings, http, (api_key,))
        params = {"chart": "mostPopular", "regionCode": str(settings["region"]), "videoCategoryId": "20",
                  "part": "snippet,statistics,contentDetails",
                  "maxResults": int(settings["youtube_max_results"]), "key": api_key}  # type: ignore[call-overload]
        _, body = client.request("GET", YOUTUBE_VIDEOS_URL, params)
        now = clock()
        videos: list[dict[str, Any]] = []
        for item in _field(client, YOUTUBE_VIDEOS_URL, params, body, "items"):
            video_id = _field(client, YOUTUBE_VIDEOS_URL, params, item, "id")
            snippet = _field(client, YOUTUBE_VIDEOS_URL, params, item, "snippet")
            published_at = _field(client, YOUTUBE_VIDEOS_URL, params, snippet, "publishedAt")
            duration = _field(client, YOUTUBE_VIDEOS_URL, params, item, "contentDetails", "duration")
            if snippet.get("liveBroadcastContent") in ("live", "upcoming"):
                continue  # un direct n'est pas une VOD
            raw_views = (item.get("statistics") or {}).get("viewCount")  # masqué : null, jamais 0
            view_count = int(raw_views) if raw_views is not None else None
            videos.append({
                "video_id": str(video_id), "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": snippet.get("title"), "channel_name": snippet.get("channelTitle"),
                "game_name": None, "duration_s": parse_iso_duration(duration),
                "published_at": published_at, "view_count": view_count,
                "thumbnail_url": _youtube_thumbnail(snippet.get("thumbnails")),
                "views_per_hour": _views_per_hour(view_count, published_at, now),
                "tags": [str(t) for t in snippet.get("tags") or []],
            })
        return {"videos": videos}

    return collect


# --------------------------------------------------------------------------
# Steam
# --------------------------------------------------------------------------


def _steam_name(client: _Client, appid: str) -> str:
    """Nom d'une app par l'API magasin (sans clé) ; ``SourceError`` dit pourquoi il manque."""
    params = {"appids": appid, "filters": "basic"}
    _, body = client.request("GET", STEAM_APPDETAILS_URL, params)
    entry = body.get(appid) if isinstance(body, dict) else None
    if not isinstance(entry, dict) or not entry.get("success"):
        raise SourceError(f"nom introuvable : appdetails ne connaît pas l'app {appid} (success=false)")
    name = (entry.get("data") or {}).get("name")
    if not isinstance(name, str) or not name:
        raise client.fail(STEAM_APPDETAILS_URL, params, body, f"{appid}.data.name")
    return name


def _steam_collector(http: Http, clock: Clock) -> Callable[..., dict[str, Any]]:
    def collect(settings: dict[str, object], *, deadline: Deadline | None = None) -> dict[str, Any]:
        if _over(deadline):
            return _stopped({"games": [], "unnamed": []}, 1)
        client = _client(settings, http, ())
        url = f"{STEAM_API}/ISteamChartsService/GetMostPlayedGames/v1/"
        _, body = client.request("GET", url, {})
        ranks = _field(client, url, {}, body, "response", "ranks")
        if _over(deadline):
            return _stopped({"games": [], "unnamed": []}, 1)
        _, live = client.request("GET", STEAM_CONCURRENT_URL, {})  # joueurs simultanés (ADR-05a4)
        concurrent: dict[str, int | None] = {}
        for row in _field(client, STEAM_CONCURRENT_URL, {}, live, "response", "ranks"):
            now = _field(client, STEAM_CONCURRENT_URL, {}, row, "concurrent_in_game")
            concurrent[str(_field(client, STEAM_CONCURRENT_URL, {}, row, "appid"))] =                 now if isinstance(now, int) and not isinstance(now, bool) else None
        path = _sdir(settings) / "steam_names.json"  # un nom connu ne se redemande jamais
        cached = _read_json(path)
        names: dict[str, str] = dict(cached["names"]) if isinstance(cached, dict) and isinstance(cached.get("names"), dict) else {}
        lookups_left = int(settings["steam_name_lookups_max"])  # type: ignore[call-overload]
        games: list[dict[str, Any]] = []
        unnamed: list[dict[str, str]] = []
        learned = False
        stopped = 0
        top = ranks[: int(settings["steam_top"])]  # type: ignore[call-overload]
        for index, rank in enumerate(top):
            appid = str(_field(client, url, {}, rank, "appid"))
            players = _field(client, url, {}, rank, "peak_in_game")  # pic du jour : seul chiffre du classement
            place = rank.get("rank")
            last_week = rank.get("last_week_rank")  # 0 : absent du top la semaine dernière ; champ absent : inconnu
            if appid not in names:
                if lookups_left <= 0:
                    unnamed.append({"appid": appid, "reason": f"nom non demandé : limite de {int(settings['steam_name_lookups_max'])} requêtes appdetails par relevé atteinte"})  # type: ignore[call-overload]
                    continue
                if _over(deadline):
                    stopped = len(top) - index  # le jeu courant et les suivants restent à relever
                    break
                lookups_left -= 1
                try:
                    names[appid] = _steam_name(client, appid)
                    learned = True
                except SourceError as exc:  # sans nom, pas de correspondance Twitch : non relevé, raison gardée
                    unnamed.append({"appid": appid, "reason": str(exc)})
                    continue
            games.append({"appid": appid, "name": names[appid], "players": players,
                          "concurrent": concurrent.get(appid),
                          "rank": place if isinstance(place, int) else None,
                          "last_week_rank": last_week if isinstance(last_week, int) else None})
        if learned:
            with channel_mod.file_lock(path):
                channel_mod.atomic_write_json(path, {"names": names})
        return _stopped({"games": games, "unnamed": unnamed}, stopped)

    return collect


def _steam_sellers_collector(http: Http, clock: Clock) -> Callable[..., dict[str, Any]]:
    """Top des ventes de la semaine du pays ``[veille] region`` (sans clé) ; noms fournis par l'API elle-même."""
    def collect(settings: dict[str, object], *, deadline: Deadline | None = None) -> dict[str, Any]:
        if _over(deadline):
            return _stopped({"games": []}, 1)
        client = _client(settings, http, ())
        country = str(settings["region"]).upper()
        language = _STEAM_LANGUAGES.get(str(settings["language"]).lower(), str(settings["language"]))
        params = {"input_json": json.dumps({
            "country_code": country, "page_start": 0, "page_count": int(settings["steam_sellers_top"]),  # type: ignore[call-overload]
            "context": {"language": language, "country_code": country},
            "data_request": {"include_basic_info": False}}, separators=(",", ":"))}
        _, body = client.request("GET", STEAM_SELLERS_URL, params)
        games: list[dict[str, Any]] = []
        for rank in _field(client, STEAM_SELLERS_URL, params, body, "response", "ranks"):
            name = _field(client, STEAM_SELLERS_URL, params, rank, "item", "name")
            last_week = rank.get("last_week_rank")  # absent ou 0 : pas dans le top la semaine dernière
            games.append({"appid": str(_field(client, STEAM_SELLERS_URL, params, rank, "appid")), "name": name,
                          "rank": _field(client, STEAM_SELLERS_URL, params, rank, "rank"),
                          "last_week_rank": last_week if isinstance(last_week, int) else 0})
        return {"games": games}

    return collect


def current_players(settings: dict[str, object], appid: str | int, *, http: Http | None = None) -> int:
    """Joueurs en ce moment d'une app (``GetNumberOfCurrentPlayers``, sans clé)."""
    client = _client(settings, http or default_http, ())
    url = f"{STEAM_API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
    params = {"appid": appid}
    _, body = client.request("GET", url, params)
    return int(_field(client, url, params, body, "response", "player_count"))


def _steam_players_collector(http: Http) -> Callable[..., dict[str, Any]]:
    """Joueurs à l'instant par appid (``GetNumberOfCurrentPlayers``, sans clé), un appel par appid, dans l'ordre reçu,
    au plus ``steam_players_lookups_max`` ; 404 = appid inconnu = ``None`` ; autre échec = ``SourceError`` (R18)."""
    def collect(settings: dict[str, object], appids: list[str], *, deadline: Deadline | None = None) -> dict[str, Any]:
        client = _client(settings, http, ())
        cap = int(settings["steam_players_lookups_max"])  # type: ignore[call-overload]
        players: dict[str, int | None] = {}
        kept = appids[:cap]
        for index, appid in enumerate(kept):
            if _over(deadline):
                return _stopped({"players": players, "skipped": max(0, len(appids) - cap)}, len(kept) - index)
            params = {"appid": appid}
            status, body = client.request("GET", STEAM_PLAYERS_URL, params, accept=(404,))
            if status == 404:
                players[appid] = None
                continue
            count = _field(client, STEAM_PLAYERS_URL, params, body, "response", "player_count")
            if isinstance(count, bool) or not isinstance(count, int):
                raise client.fail(STEAM_PLAYERS_URL, params, body, "response.player_count (entier)")
            players[appid] = count
        return {"players": players, "skipped": max(0, len(appids) - cap)}

    return collect


def _retry_after_s(raw: str | None, clock: Clock) -> float:
    """``Retry-After`` en secondes (entier ou date HTTP) ; absent ou illisible : 0 (le délai croissant décide)."""
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(raw) - clock()).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _steam_followers_collector(http: Http, clock: Clock, sleep: Callable[[float], None]
                               ) -> Callable[..., dict[str, Any]]:
    """Abonnés Steam par appid : ``<memberCount>`` de la page XML publique ``memberslistxml`` et rien d'autre ; un appel
    par appid, séquentiel, ``steam_followers_pause_s`` entre deux ; page sans la balise = ``None`` (R21).
    HTTP 429 : attente croissante (pause, doublée à chaque essai, ``Retry-After`` si plus long, plafonnée à
    ``steam_followers_retry_wait_max_s``), au plus ``steam_followers_retry_max`` réessais du même appid, chaque attente
    journalisée. Essais épuisés : on s'arrête, les appids restants valent ``None`` et sont comptés ``rate_limited``."""
    def collect(settings: dict[str, object], appids: list[str], *, deadline: Deadline | None = None) -> dict[str, Any]:
        client = _client(settings, http, ())
        cap = int(settings["steam_followers_lookups_max"])  # type: ignore[call-overload]
        pause = float(settings["steam_followers_pause_s"])  # type: ignore[arg-type]
        retries = int(settings["steam_followers_retry_max"])  # type: ignore[call-overload]
        wait_max = float(settings["steam_followers_retry_wait_max_s"])  # type: ignore[arg-type]
        followers: dict[str, int | None] = {}
        kept = appids[:cap]
        for index, appid in enumerate(kept):
            if _over(deadline):  # avant la pause comme avant la requête
                return _stopped({"followers": followers, "skipped": max(0, len(appids) - cap), "rate_limited": 0},
                                len(kept) - index)
            if index:
                sleep(pause)
            attempt = 0
            while True:
                if _over(deadline):
                    return _stopped({"followers": followers, "skipped": max(0, len(appids) - cap), "rate_limited": 0},
                                    len(kept) - index)
                try:
                    _, body = client.request("GET", STEAM_MEMBERS_URL.format(appid=appid), None,
                                             {"User-Agent": STEAM_USER_AGENT}, text=True)
                    break
                except RateLimited as exc:
                    if attempt >= retries:
                        rest = kept[index:]
                        log.warning("veille steam_followers : HTTP 429 persistant sur l'appid %s après %d réessai(s) ; "
                                    "%d appid(s) non relevé(s)", appid, retries, len(rest))
                        followers.update({a: None for a in rest})
                        return {"followers": followers, "skipped": max(0, len(appids) - cap), "rate_limited": len(rest)}
                    wait = min(wait_max, max(pause * 2 ** attempt, _retry_after_s(exc.retry_after, clock)))
                    attempt += 1
                    if _over(deadline):  # jamais d'attente d'un 429 au-delà de l'échéance
                        return _stopped({"followers": followers, "skipped": max(0, len(appids) - cap),
                                         "rate_limited": 0}, len(kept) - index)
                    log.warning("veille steam_followers : HTTP 429 sur l'appid %s, attente %.1f s (réessai %d/%d)",
                                appid, wait, attempt, retries)
                    sleep(wait)
            found = _MEMBER_COUNT.search(body)
            followers[appid] = int(found.group(1)) if found else None
        return {"followers": followers, "skipped": max(0, len(appids) - cap), "rate_limited": 0}

    return collect


def _reviews_unexpected(detail: str, url: str) -> SourceError:
    return SourceError(f"histogramme des avis Steam : format inattendu (endpoint non documenté) : {detail}, {url}")


def _review_points(body: Any, url: str, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Points ``{date, value, up, down}`` de ``results.recent`` ; toute autre forme est une ``SourceError``."""
    success = body.get("success") if isinstance(body, dict) else None
    if isinstance(success, bool) or success != 1:
        raise _reviews_unexpected(f"success != 1 ({str(body)[:_SNIPPET_CHARS]})", url)
    results = body.get("results")
    recent = results.get("recent") if isinstance(results, dict) else None
    if not isinstance(recent, list):
        raise _reviews_unexpected("results.recent absent", url)
    days: dict[str, dict[str, int]] = {}
    for entry in recent:
        fields = [entry.get(k) if isinstance(entry, dict) else None
                  for k in ("date", "recommendations_up", "recommendations_down")]
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in fields):
            raise _reviews_unexpected(f"entrée mal formée {str(entry)[:_SNIPPET_CHARS]}", url)
        ts, up, down = fields
        day = days.setdefault(datetime.fromtimestamp(ts, tz).date().isoformat(), {"up": 0, "down": 0})
        day["up"] += up
        day["down"] += down
    return [{"date": d, "value": v["up"] + v["down"], "up": v["up"], "down": v["down"]} for d, v in sorted(days.items())]


def _steam_reviews_collector(http: Http, clock: Clock, sleep: Callable[[float], None]
                             ) -> Callable[..., dict[str, Any]]:
    """Histogramme des avis Steam par appid (``appreviewhistogram``, endpoint non documenté, lu strictement, SPEC-85a0
    R24) : un appel par appid, séquentiel, ``steam_reviews_pause_s`` entre deux ; ``results.recent`` seulement.
    HTTP 429 : attente croissante (``Retry-After`` si plus long, plafonnée), réessais bornés ; épuisés : les appids
    restants valent ``None``, comptés ``rate_limited``."""
    def collect(settings: dict[str, object], appids: list[str], *, deadline: Deadline | None = None) -> dict[str, Any]:
        client = _client(settings, http, ())
        tz = ZoneInfo(str(settings["timezone"]))
        pause = float(settings["steam_reviews_pause_s"])  # type: ignore[arg-type]
        retries = int(settings["steam_reviews_retry_max"])  # type: ignore[call-overload]
        wait_max = float(settings["steam_reviews_retry_wait_max_s"])  # type: ignore[arg-type]
        histograms: dict[str, list[dict[str, Any]] | None] = {}
        for index, appid in enumerate(appids):
            if _over(deadline):  # avant la pause comme avant la requête
                return _stopped({"histograms": histograms, "skipped": 0, "rate_limited": 0}, len(appids) - index)
            if index:
                sleep(pause)
            url = STEAM_REVIEWS_URL.format(appid=appid)
            params = {"l": "english", "review_score_preference": 0}
            attempt = 0
            while True:
                if _over(deadline):
                    return _stopped({"histograms": histograms, "skipped": 0, "rate_limited": 0}, len(appids) - index)
                try:
                    _, body = client.request("GET", url, params, {"User-Agent": STEAM_REVIEWS_USER_AGENT})
                    break
                except RateLimited as exc:
                    if attempt >= retries:
                        rest = appids[index:]
                        log.warning("veille steam_reviews : HTTP 429 persistant sur l'appid %s après %d réessai(s) ; "
                                    "%d appid(s) non relevé(s)", appid, retries, len(rest))
                        histograms.update({a: None for a in rest})
                        return {"histograms": histograms, "skipped": 0, "rate_limited": len(rest)}
                    wait = min(wait_max, max(pause * 2 ** attempt, _retry_after_s(exc.retry_after, clock)))
                    attempt += 1
                    if _over(deadline):  # jamais d'attente d'un 429 au-delà de l'échéance
                        return _stopped({"histograms": histograms, "skipped": 0, "rate_limited": 0}, len(appids) - index)
                    log.warning("veille steam_reviews : HTTP 429 sur l'appid %s, attente %.1f s (réessai %d/%d)",
                                appid, wait, attempt, retries)
                    sleep(wait)
                except SourceError as exc:
                    raise _reviews_unexpected(str(exc), url) from None
            histograms[appid] = _review_points(body, f"{url}?{urlencode(params)}", tz)
        return {"histograms": histograms, "skipped": 0, "rate_limited": 0}

    return collect


def _helix_wait_s(exc: RateLimited, wait_max: float, clock: Clock) -> float:
    """Attente d'un 429 Helix : jusqu'à ``Ratelimit-Reset`` (instant Unix), sinon ``Retry-After``, sinon 1 s ; plafonnée."""
    if exc.reset is not None:
        try:
            return min(wait_max, max(0.0, float(exc.reset) - clock().timestamp()))
        except ValueError:
            pass
    if exc.retry_after is not None:
        return min(wait_max, _retry_after_s(exc.retry_after, clock))
    return min(wait_max, 1.0)


def _twitch_vods_collector(http: Http, clock: Clock, sleep: Callable[[float], None]
                           ) -> Callable[..., dict[str, Any]]:
    """VOD FR d'un mois par jeu (Helix Get Videos, ``period=month``, SPEC-85a0 R25) : par jour local, ``vods`` et
    ``views`` ; pages par curseur, au plus ``twitch_history_pages_max`` (Twitch coupe à 500 vidéos). ``since`` = jour de
    la plus ancienne vidéo rendue si un curseur reste (les jours antérieurs sont inconnus, absents), sinon le premier
    jour de la fenêtre (un jour sans VOD vaut alors 0 : mesuré). HTTP 429 : attente jusqu'à ``Ratelimit-Reset``,
    réessais bornés ; épuisés : le jeu et les suivants valent ``None``, comptés ``rate_limited``."""
    def collect(settings: dict[str, object], game_ids: list[str], *, deadline: Deadline | None = None) -> dict[str, Any]:
        if _over(deadline):
            return _stopped({"vods": {}, "skipped": 0, "rate_limited": 0}, len(game_ids))
        api = _Twitch(settings, http, clock)
        tz = ZoneInfo(str(settings["timezone"]))
        today = clock().astimezone(tz).date()
        first = today - timedelta(days=int(settings["trend_days"]) - 1)  # type: ignore[call-overload]
        pages_max = int(settings["twitch_history_pages_max"])  # type: ignore[call-overload]
        retries = int(settings["twitch_history_retry_max"])  # type: ignore[call-overload]
        wait_max = float(settings["twitch_history_retry_wait_max_s"])  # type: ignore[arg-type]
        url = f"{TWITCH_API}/videos"
        result: dict[str, dict[str, Any] | None] = {}
        for index, game_id in enumerate(game_ids):
            days: dict[date, list[int]] = {}
            oldest: date | None = None
            cursor: str | None = None
            for _ in range(pages_max):
                if _over(deadline):  # le jeu en cours est à demi lu : écarté, jamais une série tronquée
                    return _stopped({"vods": result, "skipped": 0, "rate_limited": 0}, len(game_ids) - index)
                params: dict[str, Any] = {"game_id": game_id, "language": str(settings["language"]), "period": "month",
                                          "type": "archive", "sort": "time", "first": 100}
                if cursor:
                    params["after"] = cursor
                attempt = 0
                while True:
                    try:
                        body = api.get("videos", params)
                        break
                    except RateLimited as exc:
                        if attempt >= retries:
                            rest = game_ids[index:]
                            log.warning("veille twitch_vods_30d : HTTP 429 persistant sur le jeu %s après %d réessai(s) ; "
                                        "%d jeu(x) non relevé(s)", game_id, retries, len(rest))
                            result.update({g: None for g in rest})
                            return {"vods": result, "skipped": 0, "rate_limited": len(rest)}
                        wait = _helix_wait_s(exc, wait_max, clock)
                        attempt += 1
                        if _over(deadline):
                            return _stopped({"vods": result, "skipped": 0, "rate_limited": 0}, len(game_ids) - index)
                        log.warning("veille twitch_vods_30d : HTTP 429 sur le jeu %s, attente %.1f s (réessai %d/%d)",
                                    game_id, wait, attempt, retries)
                        sleep(wait)
                for video in _field(api.client, url, params, body, "data"):
                    created = _field(api.client, url, params, video, "created_at")
                    views = _field(api.client, url, params, video, "view_count")
                    try:
                        day = datetime.fromisoformat(str(created)).astimezone(tz).date()
                    except ValueError:
                        raise api.client.fail(url, params, video, "created_at (RFC 3339)") from None
                    if isinstance(views, bool) or not isinstance(views, int):
                        raise api.client.fail(url, params, video, "view_count (entier)")
                    oldest = day if oldest is None else min(oldest, day)
                    if day >= first:
                        count = days.setdefault(day, [0, 0])
                        count[0] += 1
                        count[1] += views
                cursor = (body.get("pagination") or {}).get("cursor")
                if not cursor:
                    break
            since = max(first, oldest) if cursor and oldest is not None else first
            points = []
            for offset in range((today - since).days + 1):
                day = since + timedelta(days=offset)
                vods, views = days.get(day, [0, 0])
                points.append({"date": day.isoformat(), "vods": vods, "views": views})
            result[game_id] = {"since": since.isoformat(), "points": points}
        return {"vods": result, "skipped": 0, "rate_limited": 0}

    return collect


def default_collectors(http: Http | None = None, clock: Clock | None = None,
                       sleep: Callable[[float], None] | None = None) -> dict[str, Callable[..., dict[str, Any]]]:
    """Les collecteurs réels, sur le transport (l'horloge et l'attente) donnés."""
    http = http or default_http
    clock = clock or _now
    return {"twitch": _twitch_collector(http, clock), "youtube": _youtube_collector(http, clock),
            "steam": _steam_collector(http, clock), "steam_fr": _steam_sellers_collector(http, clock),
            "igdb": _igdb_collector(http, clock, sleep or time.sleep),
            "steam_players": _steam_players_collector(http),
            "steam_followers": _steam_followers_collector(http, clock, sleep or time.sleep),
            "steam_reviews": _steam_reviews_collector(http, clock, sleep or time.sleep),
            "twitch_vods_30d": _twitch_vods_collector(http, clock, sleep or time.sleep)}
