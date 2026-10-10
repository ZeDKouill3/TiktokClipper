"""veille_sources.py : collecteurs réels sur transport injecté (TASK-97c6, SPEC-bdd9 R3).

Un faux transport rend des réponses JSON de la forme documentée : aucun réseau.
Les tests réels (un par source) sont sautés sans ``CLIPPER_REAL_NETWORK=1``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from clipper import veille, veille_sources
from clipper.config import Config

NOW = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
SECRET = "S3CR3T-client-secret"
YKEY = "Y-api-key-123"


def _settings(tmp_path, **over):
    table = dict(veille.CONFIG_DEFAULTS)
    table.update(state_dir=str(tmp_path / "state" / "veille"), twitch_client_id="cid",
                 twitch_client_secret=SECRET, youtube_api_key=YKEY, **over)
    return table


def _config(tmp_path, **over):
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                  _sections={"worker": {"queue_path": str(tmp_path / "state" / "queue.json")},
                             "veille": _settings(tmp_path, **over)})


class FakeHttp:
    """Transport factice : ``routes[(method, fin de chemin)]`` = liste de réponses ou callable."""

    def __init__(self, routes):
        self.routes = {k: (list(v) if isinstance(v, list) else v) for k, v in routes.items()}
        self.calls: list[dict] = []

    def __call__(self, method, url, *, params=None, headers=None, timeout_s=20, content=None):
        path = urlparse(url).path
        self.calls.append({"method": method, "url": url, "path": path, "params": dict(params or {}),
                           "headers": dict(headers or {}), "timeout_s": timeout_s, "content": content})
        for (m, suffix), reply in self.routes.items():
            if m == method and path.endswith(suffix):
                if callable(reply):
                    return reply(self.calls[-1])
                response = reply.pop(0) if len(reply) > 1 else reply[0]
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"route inattendue : {method} {url}")

    def to(self, suffix):
        return [c for c in self.calls if c["path"].endswith(suffix)]


def _token(token="tok1", expires_in=3600):
    return 200, {"access_token": token, "expires_in": expires_in, "token_type": "bearer"}


def _stream(game_id, name, viewers):
    return {"game_id": game_id, "game_name": name, "viewer_count": viewers, "language": "fr"}


def _twitch_routes(over=None):
    routes = {
        ("POST", "/oauth2/token"): [_token()],
        ("GET", "/helix/streams"): [
            (200, {"data": [_stream("1", "Jeu Alpha", 100), _stream("2", "Jeu Beta", 50)],
                   "pagination": {"cursor": "c1"}}),
            (200, {"data": [_stream("1", "Jeu Alpha", 30), _stream("", "", 999)], "pagination": {}}),
        ],
        ("GET", "/helix/games/top"): [(200, {"data": [{"id": "1", "name": "Jeu Alpha", "igdb_id": "777"}]})],
        ("GET", "/helix/videos"): [(200, {"data": [{
            "id": "v10", "url": "https://www.twitch.tv/videos/10", "title": "Soirée", "user_name": "streamer_a",
            "duration": "3h2m1s", "published_at": "2026-10-06T05:00:00Z", "view_count": 600,
            "type": "archive"}]})],
    }
    routes.update(over or {})
    return routes


def _twitch(tmp_path, http, **over):
    collector = veille_sources.default_collectors(http, clock=lambda: NOW)["twitch"]
    return collector(_settings(tmp_path, **over))


# -- durées -----------------------------------------------------------------


def test_twitch_duration_to_seconds():
    assert veille_sources.parse_twitch_duration("3h2m1s") == 10921
    assert veille_sources.parse_twitch_duration("45m") == 2700
    assert veille_sources.parse_twitch_duration("30s") == 30


def test_iso_duration_to_seconds():
    assert veille_sources.parse_iso_duration("PT1H2M3S") == 3723
    assert veille_sources.parse_iso_duration("PT15M") == 900


# -- (1) jeton Twitch -------------------------------------------------------


def test_token_via_client_credentials_cached_and_reused(tmp_path):
    http = FakeHttp(_twitch_routes())
    _twitch(tmp_path, http)
    _twitch(tmp_path, http)  # second relevé, même transport
    posts = http.to("/oauth2/token")
    assert len(posts) == 1
    assert posts[0]["params"] == {"client_id": "cid", "client_secret": SECRET, "grant_type": "client_credentials"}
    cached = json.loads((tmp_path / "state" / "veille" / "twitch_token.json").read_text(encoding="utf-8"))
    assert cached["access_token"] == "tok1"
    assert datetime.fromisoformat(cached["expires_at"]) == NOW + timedelta(seconds=3600)
    assert http.to("/helix/streams")[0]["headers"] == {"Client-Id": "cid", "Authorization": "Bearer tok1"}


def test_expired_cached_token_is_renewed(tmp_path):
    sdir = tmp_path / "state" / "veille"
    sdir.mkdir(parents=True)
    (sdir / "twitch_token.json").write_text(json.dumps(
        {"access_token": "old", "expires_at": (NOW - timedelta(seconds=1)).isoformat()}), encoding="utf-8")
    http = FakeHttp(_twitch_routes())
    _twitch(tmp_path, http)
    assert len(http.to("/oauth2/token")) == 1
    assert http.to("/helix/streams")[0]["headers"]["Authorization"] == "Bearer tok1"


def test_token_renewed_after_401(tmp_path):
    http = FakeHttp(_twitch_routes({
        ("POST", "/oauth2/token"): [_token("tok1"), _token("tok2")],
        ("GET", "/helix/streams"): [
            (401, {"error": "Unauthorized", "message": "Invalid OAuth token"}),
            (200, {"data": [_stream("1", "Jeu Alpha", 10)], "pagination": {}}),
        ],
    }))
    result = _twitch(tmp_path, http)
    assert len(http.to("/oauth2/token")) == 2
    assert [c["headers"]["Authorization"] for c in http.to("/helix/streams")] == ["Bearer tok1", "Bearer tok2"]
    assert result["games"] == [{"name": "Jeu Alpha", "viewers_fr": 10, "igdb_id": "777", "twitch_id": "1"}]


def test_secret_in_no_file_and_no_error_message(tmp_path):
    http = FakeHttp(_twitch_routes({
        ("GET", "/helix/streams"): [(500, {"message": f"boom {SECRET} {YKEY}"})]}))
    with pytest.raises(veille_sources.SourceError) as err:
        _twitch(tmp_path, http)
    assert SECRET not in str(err.value) and "tok1" not in str(err.value)
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8")


def test_secret_not_in_message_when_token_endpoint_fails(tmp_path):
    http = FakeHttp(_twitch_routes({("POST", "/oauth2/token"): [(400, {"message": f"invalid {SECRET}"})]}))
    with pytest.raises(veille_sources.SourceError) as err:
        _twitch(tmp_path, http)
    assert "HTTP 400" in str(err.value) and SECRET not in str(err.value)


def test_secret_not_in_message_when_transport_raises(tmp_path):
    http = FakeHttp(_twitch_routes({("POST", "/oauth2/token"): [RuntimeError(f"url ?client_secret={SECRET}")]}))
    with pytest.raises(veille_sources.SourceError) as err:
        _twitch(tmp_path, http)
    assert SECRET not in str(err.value)


# -- (2) Twitch : viewers et VOD --------------------------------------------


def test_viewers_fr_summed_over_cursor_pages(tmp_path):
    http = FakeHttp(_twitch_routes())
    result = _twitch(tmp_path, http)
    assert result["games"] == [{"name": "Jeu Alpha", "viewers_fr": 130, "igdb_id": "777", "twitch_id": "1"},
                               {"name": "Jeu Beta", "viewers_fr": 50, "igdb_id": "", "twitch_id": "2"}]  # absent de games/top : chaîne vide
    pages = http.to("/helix/streams")
    assert [p["params"].get("after") for p in pages] == [None, "c1"]
    assert all(p["params"]["language"] == "fr" and p["params"]["first"] == 100 for p in pages)
    assert http.to("/helix/games/top")[0]["params"] == {"first": 20}


def test_streams_pagination_stops_at_five_pages(tmp_path):
    http = FakeHttp(_twitch_routes({("GET", "/helix/streams"): lambda call: (
        200, {"data": [_stream("1", "Jeu Alpha", 1)], "pagination": {"cursor": "next"}})}))
    result = _twitch(tmp_path, http)
    assert len(http.to("/helix/streams")) == 5
    assert result["games"][0]["viewers_fr"] == 5


def test_vods_listed_for_top_games_with_documented_params(tmp_path):
    http = FakeHttp(_twitch_routes())
    result = _twitch(tmp_path, http, twitch_top_games=1, twitch_vods_per_game=7)
    calls = http.to("/helix/videos")
    assert len(calls) == 1  # un seul jeu (le plus regardé)
    assert calls[0]["params"] == {"game_id": "1", "language": "fr", "period": "day", "sort": "views",
                                  "type": "archive", "first": 7}
    assert result["vods"] == [{
        "video_id": "v10", "url": "https://www.twitch.tv/videos/10", "title": "Soirée",
        "channel_name": "streamer_a", "game_name": "Jeu Alpha", "duration_s": 10921,
        "published_at": "2026-10-06T05:00:00Z", "view_count": 600, "thumbnail_url": None, "views_per_hour": 200.0}]


# -- (3) YouTube ------------------------------------------------------------


def _yt_item(video_id="y1", duration="PT1H2M3S", views="3000", published="2026-10-06T04:00:00Z", **snippet):
    item = {"id": video_id, "snippet": {"title": "T", "channelTitle": "chaine_a", "publishedAt": published, **snippet},
            "contentDetails": {"duration": duration}, "statistics": {"viewCount": views}}
    if views is None:
        del item["statistics"]["viewCount"]
    return item


def _youtube(tmp_path, http, **over):
    return veille_sources.default_collectors(http, clock=lambda: NOW)["youtube"](_settings(tmp_path, **over))


def test_youtube_single_videos_list_call_and_fields(tmp_path):
    http = FakeHttp({("GET", "/youtube/v3/videos"): [(200, {"items": [_yt_item()]})]})
    result = _youtube(tmp_path, http, youtube_max_results=25, region="BE")
    assert len(http.calls) == 1
    assert "search" not in http.calls[0]["url"]
    assert http.calls[0]["params"] == {
        "chart": "mostPopular", "regionCode": "BE", "videoCategoryId": "20",
        "part": "snippet,statistics,contentDetails", "maxResults": 25, "key": YKEY}
    assert result == {"videos": [{
        "video_id": "y1", "url": "https://www.youtube.com/watch?v=y1", "title": "T", "channel_name": "chaine_a",
        "game_name": None, "duration_s": 3723, "published_at": "2026-10-06T04:00:00Z", "view_count": 3000,
        "thumbnail_url": None, "views_per_hour": 750.0, "tags": []}]}  # 4 h depuis la publication


def test_youtube_tags_are_relayed_as_found(tmp_path):
    http = FakeHttp({("GET", "/youtube/v3/videos"): [(200, {"items": [
        _yt_item("a", tags=["Minecraft", "survie"]), _yt_item("b")]})]})
    videos = _youtube(tmp_path, http)["videos"]
    assert [v["tags"] for v in videos] == [["Minecraft", "survie"], []]


def test_youtube_views_per_hour_floors_hours_at_one(tmp_path):
    http = FakeHttp({("GET", "/youtube/v3/videos"): [(200, {"items": [
        _yt_item(published=(NOW - timedelta(minutes=10)).isoformat(), views="500")]})]})
    assert _youtube(tmp_path, http)["videos"][0]["views_per_hour"] == 500.0


def test_youtube_hidden_view_count_is_null_and_live_skipped(tmp_path):
    http = FakeHttp({("GET", "/youtube/v3/videos"): [(200, {"items": [
        _yt_item("a", views=None), _yt_item("b", liveBroadcastContent="live")]})]})
    videos = _youtube(tmp_path, http)["videos"]
    assert [v["video_id"] for v in videos] == ["a"]
    assert videos[0]["view_count"] is None and videos[0]["views_per_hour"] is None


def test_youtube_key_not_in_error_url(tmp_path):
    http = FakeHttp({("GET", "/youtube/v3/videos"): [(403, {"error": {"message": f"quota {YKEY}"}})]})
    with pytest.raises(veille_sources.SourceError) as err:
        _youtube(tmp_path, http)
    text = str(err.value)
    assert "HTTP 403" in text and "/youtube/v3/videos" in text and "regionCode=FR" in text
    assert YKEY not in text and "key=" not in text


# -- (4) Steam --------------------------------------------------------------


def _steam_routes(applist_calls=None):
    return {
        ("GET", "/GetMostPlayedGames/v1/"): [(200, {"response": {"ranks": [
            {"rank": 1, "appid": 10, "last_week_rank": 4, "peak_in_game": 900},
            {"rank": 2, "appid": 20, "last_week_rank": 0, "peak_in_game": 500},
            {"rank": 3, "appid": 30, "last_week_rank": 3, "peak_in_game": 100},
            {"rank": 4, "appid": 99, "last_week_rank": 1, "peak_in_game": 50}]}})],
        ("GET", "/GetGamesByConcurrentPlayers/v1/"): [(200, {"response": {"ranks": [
            {"rank": 1, "appid": 10, "concurrent_in_game": 400, "peak_in_game": 900},
            {"rank": 2, "appid": 30, "concurrent_in_game": 60, "peak_in_game": 100}]}})],
        # GetAppList a été retiré par Valve : 404 s'il est appelé (défaut du 2026-10-06).
        ("GET", "/GetAppList/v2/"): [(404, "Method 'GetAppList' not found in interface 'ISteamApps'")],
        ("GET", "/api/appdetails"): _appdetails({"10": "Jeu Alpha", "20": "Jeu Beta", "30": "Gamma"}),
    }


def _appdetails(names):
    """Réponse store.steampowered.com/api/appdetails : ``{appid: {success, data: {name}}}``."""
    def reply(call):
        appid = call["params"]["appids"]
        if appid in names:
            return 200, {appid: {"success": True, "data": {"name": names[appid], "type": "game"}}}
        return 200, {appid: {"success": False}}
    return reply


def _steam(tmp_path, http, clock=lambda: NOW, **over):
    return veille_sources.default_collectors(http, clock=clock)["steam"](_settings(tmp_path, **over))


def test_steam_top_names_and_no_key_sent(tmp_path):
    http = FakeHttp(_steam_routes())
    result = _steam(tmp_path, http, steam_top=3)
    assert result == {"games": [
        {"appid": "10", "name": "Jeu Alpha", "players": 900, "concurrent": 400, "rank": 1, "last_week_rank": 4},
        {"appid": "20", "name": "Jeu Beta", "players": 500, "concurrent": None, "rank": 2, "last_week_rank": 0},
        {"appid": "30", "name": "Gamma", "players": 100, "concurrent": 60, "rank": 3, "last_week_rank": 3}], "unnamed": []}
    for call in http.calls:
        assert "key" not in call["params"] and "Authorization" not in call["headers"]


def test_steam_does_not_call_removed_getapplist(tmp_path):
    http = FakeHttp(_steam_routes())
    _steam(tmp_path, http, steam_top=3)
    assert http.to("/GetAppList/v2/") == []
    assert {c["params"]["appids"] for c in http.to("/api/appdetails")} == {"10", "20", "30"}
    assert all(c["params"]["filters"] == "basic" for c in http.to("/api/appdetails"))


def test_steam_game_without_name_is_not_reported_but_has_a_reason(tmp_path):
    result = _steam(tmp_path, FakeHttp(_steam_routes()), steam_top=100)
    assert "99" not in [g["appid"] for g in result["games"]]
    unnamed = {u["appid"]: u["reason"] for u in result["unnamed"]}
    assert set(unnamed) == {"99"} and "99" in unnamed["99"] and "appdetails" in unnamed["99"]


def test_steam_known_names_are_never_asked_again(tmp_path):
    http = FakeHttp(_steam_routes())
    _steam(tmp_path, http, steam_top=3)
    assert len(http.to("/api/appdetails")) == 3
    _steam(tmp_path, http, steam_top=3, clock=lambda: NOW + timedelta(days=1))
    assert len(http.to("/api/appdetails")) == 3
    cache = json.loads((tmp_path / "state" / "veille" / "steam_names.json").read_text(encoding="utf-8"))
    assert cache["names"] == {"10": "Jeu Alpha", "20": "Jeu Beta", "30": "Gamma"}


def test_steam_lookups_are_capped_and_the_rest_marked(tmp_path):
    http = FakeHttp(_steam_routes())
    result = _steam(tmp_path, http, steam_top=3, steam_name_lookups_max=2)
    assert len(http.to("/api/appdetails")) == 2
    assert [g["appid"] for g in result["games"]] == ["10", "20"]
    assert [u["appid"] for u in result["unnamed"]] == ["30"] and "limite" in result["unnamed"][0]["reason"]
    again = _steam(tmp_path, http, steam_top=3, steam_name_lookups_max=2)  # le reste se complète au relevé suivant
    assert [g["appid"] for g in again["games"]] == ["10", "20", "30"]


def test_steam_appdetails_http_error_marks_the_game_with_reason(tmp_path):
    routes = _steam_routes()
    routes[("GET", "/api/appdetails")] = [(429, "Too Many Requests")]
    result = _steam(tmp_path, FakeHttp(routes), steam_top=2)
    assert result["games"] == []
    assert all("HTTP 429" in u["reason"] for u in result["unnamed"]) and len(result["unnamed"]) == 2


def test_current_players_for_one_appid(tmp_path):
    http = FakeHttp({("GET", "/GetNumberOfCurrentPlayers/v1/"): [(200, {"response": {"player_count": 4242, "result": 1}})]})
    assert veille_sources.current_players(_settings(tmp_path), 440, http=http) == 4242
    assert http.calls[0]["params"] == {"appid": 440}


# -- (5) erreurs ------------------------------------------------------------


def test_non_2xx_error_has_code_url_and_snippet(tmp_path):
    http = FakeHttp(_steam_routes())
    http.routes[("GET", "/GetMostPlayedGames/v1/")] = [(403, {"message": "Forbidden" + "x" * 500})]
    with pytest.raises(veille_sources.SourceError) as err:
        _steam(tmp_path, http)
    text = str(err.value)
    assert "HTTP 403" in text and "GetMostPlayedGames/v1/" in text and "Forbidden" in text
    assert len(text) < 500


def test_unreadable_json_is_a_source_error(tmp_path):
    http = FakeHttp(_steam_routes())
    http.routes[("GET", "/GetMostPlayedGames/v1/")] = [(200, "<html>pas du json</html>")]
    with pytest.raises(veille_sources.SourceError, match="illisible.*pas du json"):
        _steam(tmp_path, http)


def test_missing_field_is_a_source_error(tmp_path):
    http = FakeHttp({("GET", "/youtube/v3/videos"): [(200, {"unexpected": []})]})
    with pytest.raises(veille_sources.SourceError, match="champ attendu absent.*items"):
        _youtube(tmp_path, http)


def test_default_transport_wraps_network_errors_without_params(monkeypatch):
    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError(f"refused {SECRET}")

    monkeypatch.setattr(httpx, "request", boom)
    with pytest.raises(veille_sources.SourceError) as err:
        veille_sources.default_http("GET", "https://example.test/x", params={"key": SECRET}, timeout_s=1)
    assert SECRET not in str(err.value) and "ConnectError" in str(err.value)


def test_collect_integration_source_error_ranged_as_status_error(tmp_path, monkeypatch):
    config = _config(tmp_path)
    routes = {**_twitch_routes(), **_steam_routes(),
              ("GET", "/youtube/v3/videos"): [(500, {"error": f"boom {YKEY}"})]}
    http = FakeHttp(routes)
    monkeypatch.setattr(veille_sources, "default_http", http)
    state = veille.collect(NOW, config=config)  # aucun collecteur injecté : les réels
    assert state["sources"]["youtube"]["status"] == "error"
    assert "HTTP 500" in state["sources"]["youtube"]["error"]
    assert state["sources"]["twitch"]["status"] == "ok" and state["sources"]["steam"]["status"] == "ok"
    for path in (tmp_path / "state").rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert SECRET not in text and YKEY not in text


# -- IGDB (TASK-3274, SPEC-df51 R12) -----------------------------------------

IGDB_URL = "https://api.igdb.com/v4/games"
WINDOW_START = 1789948800  # 2026-09-21T00:00Z = minuit UTC de J-15 (aujourd'hui Paris = 2026-10-06)
WINDOW_END = 1792540800    # 2026-10-21T00:00Z = minuit UTC de J+14+1


def _dated(date, **over):
    line = {"id": date, "date": date, "human": "x", "platform": {"name": "PC"},
            "release_region": {"region": "Worldwide"}, "status": {"name": "Released"},
            "date_format": {"format": "YYYY-MM-DD"}}
    line.update(over)
    return line


def _game(game_id, name, *dates, **over):
    game = {"id": game_id, "name": name, "slug": str(name).lower(), "url": f"https://igdb.test/{game_id}", "hypes": 12,
            "first_release_date": (dates or (1791244800,))[0], "cover": {"id": 5, "image_id": f"co{game_id}"},
            "external_games": [{"uid": "620", "external_game_source": 1}],
            "release_dates": [_dated(d) for d in (dates or (1791244800,))]}
    game.update(over)
    return game


class FakeClock:
    """Horloge injectée : ``sleep`` la fait avancer."""

    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def _igdb(tmp_path, http, clock=None, **over):
    clock = clock or FakeClock()
    collector = veille_sources.default_collectors(http, clock=clock, sleep=clock.sleep)["igdb"]
    return collector(_settings(tmp_path, **over))


def _igdb_routes(pages):
    return {("POST", "/oauth2/token"): [_token()], ("POST", "/v4/games"): [(200, p) for p in pages]}


def test_igdb_request_shape_headers_and_body(tmp_path):
    http = FakeHttp(_igdb_routes([[_game(1, "Hytale")]]))
    _igdb(tmp_path, http)
    (call,) = http.to("/v4/games")
    assert call["method"] == "POST" and call["url"] == IGDB_URL
    assert call["headers"] == {"Client-ID": "cid", "Authorization": "Bearer tok1", "Accept": "application/json"}
    body = call["content"]
    fields = re.search(r"fields ([^;]*);", body).group(1).split(",")
    for needed in ("name", "hypes", "first_release_date", "cover.image_id", "external_games.uid",
                   "external_games.external_game_source", "release_dates.date", "release_dates.platform.name"):
        assert needed in fields
    assert "category" not in body
    where = re.search(r"where ([^;]*);", body).group(1)
    assert where == f"release_dates.date >= {WINDOW_START} & release_dates.date < {WINDOW_END} & hypes >= 1"
    assert "sort hypes desc" in body and "limit 500" in body and "offset 0" in body


def test_igdb_shares_the_twitch_token_cache(tmp_path):
    http = FakeHttp({**_twitch_routes(), ("POST", "/v4/games"): [(200, [])]})
    _twitch(tmp_path, http)
    _igdb(tmp_path, http)
    assert len(http.to("/oauth2/token")) == 1  # même twitch_token.json
    assert http.to("/v4/games")[0]["headers"]["Authorization"] == "Bearer tok1"


def test_igdb_renews_token_once_on_401(tmp_path):
    http = FakeHttp({("POST", "/oauth2/token"): [_token("tok1"), _token("tok2")],
                     ("POST", "/v4/games"): [(401, {"message": "bad"}), (200, [_game(1, "Hytale")])]})
    result = _igdb(tmp_path, http)
    assert [c["headers"]["Authorization"] for c in http.to("/v4/games")] == ["Bearer tok1", "Bearer tok2"]
    assert len(result["games"]) == 1


def test_igdb_maps_games_with_all_their_dated_lines(tmp_path):
    game = _game(1, "Hytale", 1791244800, 1791331200 + 86399,
                 external_games=[{"uid": "10", "external_game_source": 5}, {"uid": "620", "external_game_source": 1},
                                 {"uid": "621", "external_game_source": 1}])
    game["release_dates"][1]["platform"] = {"name": "Nintendo Switch 2"}
    result = _igdb(tmp_path, FakeHttp(_igdb_routes([[game]])))
    assert result["skipped_rows"] == 0
    (got,) = result["games"]
    assert {k: v for k, v in got.items() if k != "release_dates"} == {
        "igdb_id": "1", "name": "Hytale", "slug": "hytale", "url": "https://igdb.test/1", "hypes": 12,
        "first_release_date": 1791244800, "cover_image_id": "co1", "steam_appid": "620"}
    assert got["release_dates"] == [
        {"date": "2026-10-06", "ts": 1791244800, "human": "x", "platform": "PC", "region": "Worldwide",
         "status": "Released", "date_format": "YYYY-MM-DD"},
        {"date": "2026-10-07", "ts": 1791331200 + 86399, "human": "x", "platform": "Nintendo Switch 2",
         "region": "Worldwide", "status": "Released", "date_format": "YYYY-MM-DD"}]  # jour UTC


def test_igdb_missing_optional_fields_are_null(tmp_path):
    bare = {"id": 2, "name": "Nu", "hypes": 3, "release_dates": [{"date": 1791244800}]}
    result = _igdb(tmp_path, FakeHttp(_igdb_routes([[bare]])))
    (got,) = result["games"]
    assert got["first_release_date"] is None and got["cover_image_id"] is None and got["steam_appid"] is None
    assert got["slug"] is None and got["url"] is None
    assert got["release_dates"] == [{"date": "2026-10-06", "ts": 1791244800, "human": None, "platform": None,
                                     "region": None, "status": None, "date_format": None}]


def test_igdb_steam_appid_ignores_other_sources_and_category(tmp_path):
    other_source = _game(3, "Autre", external_games=[{"uid": "9", "category": 1, "external_game_source": 5}])
    category_only = _game(4, "Vieux", external_games=[{"uid": "8", "category": 1}])
    result = _igdb(tmp_path, FakeHttp(_igdb_routes([[other_source, category_only]])))
    assert [g["steam_appid"] for g in result["games"]] == [None, None]


def test_igdb_counts_unusable_games_as_skipped(tmp_path):
    nameless = _game(4, "x")
    del nameless["name"]
    games = [
        _game(1, "Bon"),
        _game(2, "Sans Hype", hypes=None),
        _game(3, "Hype texte", hypes="12"),
        nameless,
        _game(5, "Sans Date", release_dates=[]),
        _game(6, "Date vide", release_dates=[{"human": "TBD"}]),
    ]
    result = _igdb(tmp_path, FakeHttp(_igdb_routes([games])))
    assert [g["name"] for g in result["games"]] == ["Bon"]
    assert result["skipped_rows"] == 5


def test_igdb_game_without_id_raises(tmp_path):
    game = _game(1, "Sans Id")
    del game["id"]
    with pytest.raises(veille_sources.SourceError, match="id"):
        _igdb(tmp_path, FakeHttp(_igdb_routes([[game]])))


def test_igdb_paginates_while_500_games_and_caps_pages(tmp_path):
    full = [_game(i, f"G{i}") for i in range(1, 501)]
    http = FakeHttp(_igdb_routes([full, full, [_game(9999, "Dernier")]]))
    result = _igdb(tmp_path, http)
    calls = http.to("/v4/games")
    assert [f"offset {o};" in c["content"] for c, o in zip(calls, (0, 500, 1000))] == [True] * 3 and len(calls) == 3
    assert len(result["games"]) == 1001
    capped = FakeHttp(_igdb_routes([full]))
    _igdb(tmp_path, capped, igdb_pages_max=3)
    assert len(capped.to("/v4/games")) == 3  # toujours 500 jeux : arrêt au plafond
    one = FakeHttp(_igdb_routes([full]))
    _igdb(tmp_path, one, igdb_pages_max=1)
    assert len(one.to("/v4/games")) == 1


def test_igdb_requests_are_spaced_by_250_ms(tmp_path):
    clock = FakeClock()
    full = [_game(i, f"G{i}") for i in range(1, 501)]
    stamps = []

    def reply(call):
        stamps.append(clock())
        return 200, full if len(stamps) < 3 else []

    http = FakeHttp({("POST", "/oauth2/token"): [_token()], ("POST", "/v4/games"): reply})
    _igdb(tmp_path, http, clock)
    assert len(stamps) == 3
    assert all((b - a) >= timedelta(milliseconds=250) for a, b in zip(stamps, stamps[1:]))


def test_igdb_429_names_the_rate_limit_without_secrets(tmp_path):
    http = FakeHttp({**_igdb_routes([]), ("POST", "/v4/games"): [(429, {"message": f"Too many {SECRET}"})]})
    with pytest.raises(veille_sources.SourceError, match="4 requêtes/s") as err:
        _igdb(tmp_path, http)
    assert "429" in str(err.value) and SECRET not in str(err.value) and "tok1" not in str(err.value)
    assert len(http.to("/v4/games")) == 1  # pas de réessai


def test_igdb_http_error_has_no_secret(tmp_path):
    http = FakeHttp({**_igdb_routes([]), ("POST", "/v4/games"): [(500, {"message": f"boom tok1 {SECRET}"})]})
    with pytest.raises(veille_sources.SourceError) as err:
        _igdb(tmp_path, http)
    assert "HTTP 500" in str(err.value) and "tok1" not in str(err.value) and SECRET not in str(err.value)


# -- (6) tests réels optionnels ---------------------------------------------

_REAL = os.environ.get("CLIPPER_REAL_NETWORK") == "1"
real_only = pytest.mark.skipif(not _REAL, reason="CLIPPER_REAL_NETWORK=1 requis (réseau réel)")


def _real_settings(tmp_path):
    return _settings(tmp_path, twitch_client_id=os.environ.get("TWITCH_CLIENT_ID", ""),
                     twitch_client_secret=os.environ.get("TWITCH_CLIENT_SECRET", ""),
                     youtube_api_key=os.environ.get("YOUTUBE_API_KEY", ""))


@real_only
def test_real_twitch(tmp_path):
    settings = _real_settings(tmp_path)
    if not settings["twitch_client_id"]:
        pytest.skip("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET absents")
    assert veille_sources.default_collectors()["twitch"](settings)["games"]


@real_only
def test_real_youtube(tmp_path):
    settings = _real_settings(tmp_path)
    if not settings["youtube_api_key"]:
        pytest.skip("YOUTUBE_API_KEY absente")
    assert "videos" in veille_sources.default_collectors()["youtube"](settings)


@real_only
def test_real_steam(tmp_path):
    # appdetails réel (sans clé) : les 5 premiers jeux du top doivent avoir un nom (GetAppList v2 est mort).
    result = veille_sources.default_collectors()["steam"](_settings(tmp_path, steam_top=5))
    assert result["games"] and all(g["name"] for g in result["games"])


# -- (7) Steam : top des ventes du pays (TASK-2784) --------------------------


def _sellers_reply(call):
    return 200, {"response": {"start_date": 1759708800, "ranks": [
        {"rank": 1, "appid": 1000, "item": {"name": "AION 2"}, "last_week_rank": 5, "consecutive_weeks": 2},
        {"rank": 5, "appid": 1091500, "item": {"name": "Cyberpunk 2077"}, "last_week_rank": 112, "consecutive_weeks": 1},
        {"rank": 8, "appid": 2000, "item": {"name": "Minecraft Dungeons II"}, "last_week_rank": 17},
        {"rank": 9, "appid": 3000, "item": {"name": "Jeu Neuf"}, "last_week_rank": 0},
        {"rank": 10, "appid": 4000, "item": {"name": "Sans Semaine"}}]}}


def _sellers(tmp_path, http, **over):
    return veille_sources.default_collectors(http)["steam_fr"](_settings(tmp_path, **over))


def test_steam_fr_reads_weekly_top_sellers_for_the_configured_country(tmp_path):
    http = FakeHttp({("GET", "/IStoreTopSellersService/GetWeeklyTopSellers/v1/"): _sellers_reply})
    result = _sellers(tmp_path, http, region="FR", language="french", steam_sellers_top=50)
    assert result["games"][:3] == [
        {"appid": "1000", "name": "AION 2", "rank": 1, "last_week_rank": 5},
        {"appid": "1091500", "name": "Cyberpunk 2077", "rank": 5, "last_week_rank": 112},
        {"appid": "2000", "name": "Minecraft Dungeons II", "rank": 8, "last_week_rank": 17}]
    assert result["games"][3]["last_week_rank"] == 0       # 0 : absent du top la semaine dernière
    assert result["games"][4]["last_week_rank"] == 0       # champ absent : traité comme nouveau (critère)
    (call,) = http.calls
    sent = json.loads(call["params"]["input_json"])
    assert sent["country_code"] == "FR" and sent["page_start"] == 0 and sent["page_count"] == 50
    assert sent["context"] == {"language": "french", "country_code": "FR"}
    assert sent["data_request"] == {"include_basic_info": False}
    assert "key" not in call["params"]


def test_steam_fr_does_not_call_appdetails_for_names(tmp_path):
    http = FakeHttp({("GET", "/GetWeeklyTopSellers/v1/"): _sellers_reply})
    _sellers(tmp_path, http)
    assert not http.to("/api/appdetails")


def test_steam_fr_empty_response_is_a_named_error(tmp_path):
    http = FakeHttp({("GET", "/GetWeeklyTopSellers/v1/"): [(200, {})]})
    with pytest.raises(veille_sources.SourceError, match="response.ranks"):
        _sellers(tmp_path, http)


def test_steam_fr_game_without_item_name_is_a_named_error(tmp_path):
    http = FakeHttp({("GET", "/GetWeeklyTopSellers/v1/"): [(200, {"response": {"ranks": [{"rank": 1, "appid": 1, "item": {}}]}})]})
    with pytest.raises(veille_sources.SourceError, match="item.name"):
        _sellers(tmp_path, http)


@real_only
def test_real_steam_fr(tmp_path):
    result = veille_sources.default_collectors()["steam_fr"](_settings(tmp_path, steam_sellers_top=5))
    assert len(result["games"]) == 5 and all(g["name"] and g["rank"] for g in result["games"])


# -- miniatures et VOD réservées aux abonnés (TASK-a898) ---------------------


def _video(video_id, **over):
    video = {"id": video_id, "url": f"https://www.twitch.tv/videos/{video_id}", "title": "T", "user_name": "s",
             "duration": "3h2m1s", "published_at": "2026-10-06T05:00:00Z", "view_count": 600, "type": "archive"}
    video.update(over)
    return video


def _twitch_vods(tmp_path, videos):
    http = FakeHttp(_twitch_routes({("GET", "/helix/videos"): [(200, {"data": videos})]}))
    return _twitch(tmp_path, http, twitch_top_games=1)


def test_twitch_thumbnail_size_placeholders_are_replaced_by_a_fixed_size(tmp_path):
    result = _twitch_vods(tmp_path, [_video("a", thumbnail_url="https://cdn.test/t/%{width}x%{height}.jpg")])
    assert result["vods"][0]["thumbnail_url"] == "https://cdn.test/t/640x360.jpg"


@pytest.mark.parametrize("thumb", ["", None, "https://vod-secure.twitch.tv/_404/404_processing_%{width}x%{height}.png"])
def test_twitch_vod_without_usable_thumbnail_has_none(tmp_path, thumb):
    video = _video("a", thumbnail_url=thumb)
    result = _twitch_vods(tmp_path, [video])
    assert result["vods"][0]["thumbnail_url"] is None


def test_twitch_vod_without_thumbnail_field_has_none(tmp_path):
    assert _twitch_vods(tmp_path, [_video("a")])["vods"][0]["thumbnail_url"] is None


def test_youtube_thumbnail_is_the_widest_available(tmp_path):
    thumbs = {"default": {"url": "https://i.test/d.jpg", "width": 120, "height": 90},
              "high": {"url": "https://i.test/h.jpg", "width": 480, "height": 360},
              "medium": {"url": "https://i.test/m.jpg", "width": 320, "height": 180}}
    http = FakeHttp({("GET", "/youtube/v3/videos"): [(200, {"items": [_yt_item(thumbnails=thumbs)]})]})
    assert _youtube(tmp_path, http)["videos"][0]["thumbnail_url"] == "https://i.test/h.jpg"


@pytest.mark.parametrize("snippet", [{}, {"thumbnails": {}}])
def test_youtube_without_thumbnails_has_none(tmp_path, snippet):
    http = FakeHttp({("GET", "/youtube/v3/videos"): [(200, {"items": [_yt_item(**snippet)]})]})
    assert _youtube(tmp_path, http)["videos"][0]["thumbnail_url"] is None


def test_twitch_non_public_vods_are_dropped_and_counted(tmp_path):
    result = _twitch_vods(tmp_path, [_video("pub", viewable="public"), _video("priv", viewable="private"),
                                     _video("noflag")])
    assert [v["video_id"] for v in result["vods"]] == ["pub", "noflag"]
    assert result["private_vods"] == 1


# --- TASK-9495 : test d'accès d'une VOD Twitch par yt-dlp (sans téléchargement) ---


class _FakeYdl:
    """yt-dlp simulé : enregistre les options et les appels, lève ``error`` si fourni."""

    instances = []

    def __init__(self, opts, error=None):
        self.opts, self.error, self.calls = opts, error, []
        _FakeYdl.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True, **kwargs):
        self.calls.append((url, download))
        if self.error:
            raise self.error
        return {"id": "1"}


def _ydl_with(error=None):
    _FakeYdl.instances.clear()
    return lambda opts: _FakeYdl(opts, error)


def test_check_twitch_access_extracts_without_downloading():
    veille_sources.check_twitch_access("https://www.twitch.tv/videos/1", 7, ydl_factory=_ydl_with())
    ydl = _FakeYdl.instances[0]
    assert ydl.calls == [("https://www.twitch.tv/videos/1", False)]
    assert ydl.opts["socket_timeout"] == 7 and ydl.opts["quiet"] is True


@pytest.mark.parametrize("message", [
    "ERROR: [twitch:vod] 1: You must be logged into an account that has access to this subscriber-only content",
    "This video is subscriber-only",
    "Subscribers only video",
])
def test_check_twitch_access_raises_restricted_for_subscriber_only(message):
    with pytest.raises(veille_sources.AccessRestricted):
        veille_sources.check_twitch_access("u", 5, ydl_factory=_ydl_with(Exception(message)))


@pytest.mark.parametrize("error", [
    ConnectionResetError("[WinError 10054] connexion fermée par l'hôte"),
    TimeoutError("timed out"),
    Exception("Unable to download webpage: HTTP Error 503"),
])
def test_check_twitch_access_lets_other_errors_through(error):
    with pytest.raises(Exception) as caught:
        veille_sources.check_twitch_access("u", 5, ydl_factory=_ydl_with(error))
    assert not isinstance(caught.value, veille_sources.AccessRestricted)


@real_only
def test_real_igdb_games_in_window(tmp_path):
    settings = _real_settings(tmp_path)
    if not settings["twitch_client_id"]:
        pytest.skip("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET absents")
    captured = []

    def spy(*args, **kwargs):
        captured.append(kwargs.get("content"))
        return veille_sources.default_http(*args, **kwargs)

    result = veille_sources.default_collectors(spy)["igdb"](settings)
    assert result["games"], "IGDB doit rendre au moins un jeu sur la fenêtre du jour"
    start, end = (int(x) for x in re.search(
        r"release_dates\.date >= (\d+) & release_dates\.date < (\d+)", captured[-1]).groups())
    assert any(g["cover_image_id"] for g in result["games"])
    for game in result["games"]:
        assert any(start <= d["ts"] < end for d in game["release_dates"])  # une date (secondes Unix) dans la fenêtre


# -- (8) TASK-82da : Steam officiel, joueurs simultanés, abonnés (SPEC-df51 R18, R21) ----------


def test_steam_second_call_reads_concurrent_ranking_without_changing_players(tmp_path):
    http = FakeHttp(_steam_routes())
    result = _steam(tmp_path, http, steam_top=3)
    assert len(http.to("/GetGamesByConcurrentPlayers/v1/")) == 1
    assert http.to("/GetGamesByConcurrentPlayers/v1/")[0]["url"] == (
        "https://api.steampowered.com/ISteamChartsService/GetGamesByConcurrentPlayers/v1/")
    assert [(g["appid"], g["players"], g["concurrent"]) for g in result["games"]] == [
        ("10", 900, 400), ("20", 500, None), ("30", 100, 60)]


def test_steam_concurrent_ranking_error_is_a_source_error(tmp_path):
    routes = _steam_routes()
    routes[("GET", "/GetGamesByConcurrentPlayers/v1/")] = [(500, "boom")]
    with pytest.raises(veille_sources.SourceError, match="HTTP 500.*GetGamesByConcurrentPlayers"):
        _steam(tmp_path, FakeHttp(routes), steam_top=3)


def _players_routes(counts):
    """GetNumberOfCurrentPlayers : counts[appid] = entier, None = 404, tuple = (statut, corps)."""
    def reply(call):
        value = counts[call["params"]["appid"]]
        if value is None:
            return 404, "Not Found"
        if isinstance(value, tuple):
            return value
        return 200, {"response": {"player_count": value, "result": 1}}
    return {("GET", "/GetNumberOfCurrentPlayers/v1/"): reply}


def _players(tmp_path, http, appids, **over):
    return veille_sources.default_collectors(http)["steam_players"](_settings(tmp_path, **over), appids)


def test_steam_players_one_get_per_appid_in_order_with_documented_params(tmp_path):
    http = FakeHttp(_players_routes({"3": 30, "1": 10, "2": 20}))
    result = _players(tmp_path, http, ["3", "1", "2"])
    assert result == {"players": {"3": 30, "1": 10, "2": 20}, "skipped": 0}
    assert [c["params"] for c in http.calls] == [{"appid": "3"}, {"appid": "1"}, {"appid": "2"}]
    assert all(c["url"] == "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
               for c in http.calls)


def test_steam_players_cap_counts_the_rest_as_skipped(tmp_path):
    http = FakeHttp(_players_routes({"1": 10, "2": 20, "3": 30}))
    result = _players(tmp_path, http, ["1", "2", "3"], steam_players_lookups_max=2)
    assert result == {"players": {"1": 10, "2": 20}, "skipped": 1}
    assert len(http.calls) == 2


def test_steam_players_cap_zero_makes_no_call(tmp_path):
    http = FakeHttp(_players_routes({"1": 10}))
    assert _players(tmp_path, http, ["1"], steam_players_lookups_max=0) == {"players": {}, "skipped": 1}
    assert http.calls == []


def test_steam_players_404_is_null_not_an_error(tmp_path):
    http = FakeHttp(_players_routes({"1": None, "2": 20}))
    assert _players(tmp_path, http, ["1", "2"]) == {"players": {"1": None, "2": 20}, "skipped": 0}


@pytest.mark.parametrize("reply", [(500, "boom"), (200, "<html>pas du json</html>"), (200, {"response": {"result": 1}}),
                                   (200, {"response": {"player_count": "beaucoup"}})])
def test_steam_players_other_failures_are_source_errors(tmp_path, reply):
    http = FakeHttp(_players_routes({"1": reply}))
    with pytest.raises(veille_sources.SourceError):
        _players(tmp_path, http, ["1"])


def _members(count):
    return ('<?xml version="1.0" encoding="UTF-8"?><memberList><groupID64>1</groupID64>'
            f"<memberCount>{count}</memberCount><members><steamID64>76561198000000001</steamID64></members></memberList>")


def _followers_routes(bodies):
    def reply(call):
        value = bodies[re.search(r"/games/(\d+)/", call["url"]).group(1)]
        return value if isinstance(value, tuple) else (200, value)
    return {("GET", "/memberslistxml/"): reply}


def _followers(tmp_path, http, appids, sleeps=None, **over):
    collectors = veille_sources.default_collectors(http, sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return collectors["steam_followers"](_settings(tmp_path, **over), appids)


def test_steam_followers_reads_member_count_only_with_user_agent_and_pause(tmp_path):
    http = FakeHttp(_followers_routes({"7": _members(124547), "8": _members(38763)}))
    sleeps: list[float] = []
    result = _followers(tmp_path, http, ["7", "8"], sleeps, steam_followers_pause_s=1.5)
    assert result == {"followers": {"7": 124547, "8": 38763}, "skipped": 0, "rate_limited": 0}
    assert [c["url"] for c in http.calls] == [
        "https://steamcommunity.com/games/7/memberslistxml/?xml=1", "https://steamcommunity.com/games/8/memberslistxml/?xml=1"]
    assert all("Clipper" in c["headers"]["User-Agent"] for c in http.calls)
    assert sleeps == [1.5]  # entre deux appels seulement


def test_steam_followers_page_without_tag_is_null(tmp_path):
    http = FakeHttp(_followers_routes({"7": "<!DOCTYPE html><html><title>Steam Community :: Error</title></html>"}))
    assert _followers(tmp_path, http, ["7"]) == {"followers": {"7": None}, "skipped": 0, "rate_limited": 0}


def test_steam_followers_cap_and_zero(tmp_path):
    http = FakeHttp(_followers_routes({"1": _members(1), "2": _members(2), "3": _members(3)}))
    assert _followers(tmp_path, http, ["1", "2", "3"], steam_followers_lookups_max=2) == {
        "followers": {"1": 1, "2": 2}, "skipped": 1, "rate_limited": 0}
    assert len(http.calls) == 2
    http = FakeHttp(_followers_routes({"1": _members(1)}))
    assert _followers(tmp_path, http, ["1"], steam_followers_lookups_max=0) == {"followers": {}, "skipped": 1, "rate_limited": 0}
    assert http.calls == []


@pytest.mark.parametrize("reply", [(503, "indisponible"), (404, "nope"), (200, "")])
def test_steam_followers_http_error_or_empty_body_is_a_source_error(tmp_path, reply):
    http = FakeHttp(_followers_routes({"1": reply}))
    with pytest.raises(veille_sources.SourceError):
        _followers(tmp_path, http, ["1"])


@real_only
def test_real_steam_players_ranking_and_followers(tmp_path):
    import httpx

    collectors = veille_sources.default_collectors()
    players = collectors["steam_players"](_settings(tmp_path), ["570"])["players"]["570"]
    assert isinstance(players, int) and players > 0
    url = "https://api.steampowered.com/ISteamChartsService/GetGamesByConcurrentPlayers/v1/"
    first = httpx.get(url, timeout=20).json()["response"]["ranks"][0]
    assert isinstance(first["concurrent_in_game"], int) and isinstance(first["peak_in_game"], int)
    followers = collectors["steam_followers"](_settings(tmp_path), ["570"])["followers"]["570"]
    assert isinstance(followers, int) and followers > 0


# --- TASK-cab5 : HTTP 429 sur memberslistxml -------------------------------------------------


def _seq_routes(replies):
    """memberslistxml : réponses successives par appid (la dernière se répète)."""
    seen: dict[str, int] = {}

    def reply(call):
        appid = re.search(r"/games/(\d+)/", call["url"]).group(1)
        i = seen.get(appid, 0)
        seen[appid] = i + 1
        items = replies[appid]
        value = items[min(i, len(items) - 1)]
        return value if isinstance(value, tuple) else (200, value)
    return {("GET", "/memberslistxml/"): reply}


def test_steam_followers_429_then_200_retries_same_appid_with_growing_capped_wait(tmp_path, caplog):
    http = FakeHttp(_seq_routes({"7": [(429, ""), (429, ""), _members(500)], "8": [_members(8)]}))
    sleeps: list[float] = []
    with caplog.at_level("WARNING"):
        result = _followers(tmp_path, http, ["7", "8"], sleeps, steam_followers_pause_s=2.0,
                            steam_followers_retry_max=3, steam_followers_retry_wait_max_s=30.0)
    assert result == {"followers": {"7": 500, "8": 8}, "skipped": 0, "rate_limited": 0}
    assert [c["url"].split("/")[4] for c in http.calls] == ["7", "7", "7", "8"]
    assert sleeps == [2.0, 4.0, 2.0]  # attente 429 : pause puis doublée ; puis la pause normale entre appids
    assert sum("429" in r.message for r in caplog.records) == 2  # chaque attente est journalisée


def test_steam_followers_persistent_429_keeps_read_values_and_marks_the_rest_rate_limited(tmp_path):
    http = FakeHttp(_seq_routes({"1": [_members(1)], "2": [(429, "")], "3": [_members(3)]}))
    sleeps: list[float] = []
    result = _followers(tmp_path, http, ["1", "2", "3"], sleeps, steam_followers_pause_s=2.0,
                        steam_followers_retry_max=2, steam_followers_retry_wait_max_s=30.0)
    assert result == {"followers": {"1": 1, "2": None, "3": None}, "skipped": 0, "rate_limited": 2}
    assert [c["url"].split("/")[4] for c in http.calls] == ["1", "2", "2", "2"]  # 1 essai + 2 réessais, puis arrêt


def test_steam_followers_retry_after_seconds_is_used_and_capped(tmp_path):
    http = FakeHttp(_seq_routes({"1": [(429, "", {"Retry-After": "7"}), (429, "", {"Retry-After": "900"}), _members(9)]}))
    sleeps: list[float] = []
    result = _followers(tmp_path, http, ["1"], sleeps, steam_followers_pause_s=2.0,
                        steam_followers_retry_max=3, steam_followers_retry_wait_max_s=30.0)
    assert result["followers"] == {"1": 9}
    assert sleeps == [7.0, 30.0]  # Retry-After lu ; plafonné au plafond d'attente


def test_steam_followers_retry_after_http_date_uses_injected_clock(tmp_path):
    from datetime import datetime, timezone
    http = FakeHttp(_seq_routes({"1": [(429, "", {"Retry-After": "Wed, 07 Oct 2026 12:00:12 GMT"}), _members(9)]}))
    sleeps: list[float] = []
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
    collectors = veille_sources.default_collectors(http, clock=lambda: now, sleep=sleeps.append)
    result = collectors["steam_followers"](_settings(tmp_path, steam_followers_pause_s=2.0), ["1"])
    assert result["followers"] == {"1": 9}
    assert sleeps == [12.0]


def test_steam_followers_no_429_has_no_rate_limited_count(tmp_path):
    http = FakeHttp(_followers_routes({"7": _members(5)}))
    assert _followers(tmp_path, http, ["7"])["rate_limited"] == 0


# --- TASK-97b6 (SPEC-85a0 R24) : histogramme des avis Steam ----------------------------------


def _ts(year, month, day, hour=12):
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


def _histogram(*recent, success=1):
    return {"success": success, "results": {"start_date": 1, "end_date": 2, "weeks": [], "rollups": [],
                                            "recent": list(recent)}}


def _day(ts, up, down):
    return {"date": ts, "recommendations_up": up, "recommendations_down": down}


def _reviews_routes(replies):
    """appreviewhistogram/<appid> : réponses successives par appid (la dernière se répète)."""
    seen: dict[str, int] = {}

    def reply(call):
        appid = call["path"].rsplit("/", 1)[1]
        i = seen.get(appid, 0)
        seen[appid] = i + 1
        items = replies[appid]
        value = items[min(i, len(items) - 1)]
        return value if isinstance(value, tuple) else (200, value)
    return {("GET", ""): reply}


def _reviews(tmp_path, http, appids, sleeps=None, **over):
    collectors = veille_sources.default_collectors(
        http, clock=lambda: NOW, sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return collectors["steam_reviews"](_settings(tmp_path, **over), appids)


def test_steam_reviews_exact_url_params_user_agent_sequential_with_pause(tmp_path):
    http = FakeHttp(_reviews_routes({"7": [_histogram(_day(_ts(2026, 10, 5), 10, 2))],
                                     "8": [_histogram(_day(_ts(2026, 10, 5), 4, 1))]}))
    sleeps: list[float] = []
    result = _reviews(tmp_path, http, ["7", "8"], sleeps, steam_reviews_pause_s=1.5)
    assert result == {"histograms": {"7": [{"date": "2026-10-05", "value": 12, "up": 10, "down": 2}],
                                     "8": [{"date": "2026-10-05", "value": 5, "up": 4, "down": 1}]},
                      "skipped": 0, "rate_limited": 0}
    assert [c["url"] for c in http.calls] == ["https://store.steampowered.com/appreviewhistogram/7",
                                              "https://store.steampowered.com/appreviewhistogram/8"]
    assert all(c["params"] == {"l": "english", "review_score_preference": 0} for c in http.calls)
    assert all("Clipper" in c["headers"]["User-Agent"] for c in http.calls)
    assert sleeps == [1.5]  # entre deux appels seulement


def test_steam_reviews_day_in_veille_timezone_and_same_day_entries_are_added(tmp_path):
    late = _ts(2026, 10, 5, 23)  # 23:00 UTC = 01:00 le 6 à Paris
    http = FakeHttp(_reviews_routes({"7": [_histogram(_day(late, 3, 1), _day(_ts(2026, 10, 6, 8), 2, 2),
                                                      _day(_ts(2026, 10, 4, 10), 1, 0))]}))
    points = _reviews(tmp_path, http, ["7"])["histograms"]["7"]
    assert points == [{"date": "2026-10-04", "value": 1, "up": 1, "down": 0},
                      {"date": "2026-10-06", "value": 8, "up": 5, "down": 3}]


def test_steam_reviews_empty_recent_is_an_ok_series_without_point(tmp_path):
    http = FakeHttp(_reviews_routes({"7": [_histogram()]}))
    assert _reviews(tmp_path, http, ["7"])["histograms"] == {"7": []}


@pytest.mark.parametrize("reply", [
    (200, _histogram(success=2)), (200, {"success": 1, "results": {}}), (200, {"success": 1}),
    (200, _histogram({"recommendations_up": 1, "recommendations_down": 1})),
    (200, _histogram({"date": 1759665600, "recommendations_up": "1", "recommendations_down": 1})),
    (200, _histogram({"date": 1759665600, "recommendations_up": 1, "recommendations_down": -1})),
    (200, _histogram({"date": 1759665600.5, "recommendations_up": 1, "recommendations_down": 1})),
    (200, "<html>pas du json</html>"), (500, "boom"), (404, {"success": 0})])
def test_steam_reviews_any_other_shape_is_a_source_error_naming_the_undocumented_endpoint(tmp_path, reply):
    http = FakeHttp(_reviews_routes({"7": [reply]}))
    with pytest.raises(veille_sources.SourceError, match=r"format inattendu \(endpoint non documenté\)") as err:
        _reviews(tmp_path, http, ["7"])
    assert "appreviewhistogram/7" in str(err.value)


def test_steam_reviews_429_then_200_waits_growing_capped_and_logs(tmp_path, caplog):
    http = FakeHttp(_reviews_routes({"7": [(429, ""), (429, "", {"Retry-After": "900"}),
                                           _histogram(_day(_ts(2026, 10, 5), 1, 1))],
                                     "8": [_histogram()]}))
    sleeps: list[float] = []
    with caplog.at_level("WARNING"):
        result = _reviews(tmp_path, http, ["7", "8"], sleeps, steam_reviews_pause_s=2.0,
                          steam_reviews_retry_max=3, steam_reviews_retry_wait_max_s=30.0)
    assert result == {"histograms": {"7": [{"date": "2026-10-05", "value": 2, "up": 1, "down": 1}], "8": []},
                      "skipped": 0, "rate_limited": 0}
    assert sleeps == [2.0, 30.0, 2.0]  # 2 x 2^0 ; Retry-After 900 plafonné à 30 ; puis la pause entre appids
    assert sum("429" in r.message for r in caplog.records) == 2


def test_steam_reviews_persistent_429_marks_the_rest_rate_limited(tmp_path):
    http = FakeHttp(_reviews_routes({"7": [_histogram()], "8": [(429, "")], "9": [_histogram()]}))
    result = _reviews(tmp_path, http, ["7", "8", "9"], steam_reviews_retry_max=2)
    assert result == {"histograms": {"7": [], "8": None, "9": None}, "skipped": 0, "rate_limited": 2}
    assert [c["url"].rsplit("/", 1)[1] for c in http.calls] == ["7", "8", "8", "8"]


# --- TASK-97b6 (SPEC-85a0 R25) : twitch_id et VOD Twitch sur un mois -------------------------


def test_twitch_collector_returns_the_helix_game_id(tmp_path):
    http = FakeHttp(_twitch_routes())
    result = _twitch(tmp_path, http)
    assert result["games"][0] == {"name": "Jeu Alpha", "viewers_fr": 130, "igdb_id": "777", "twitch_id": "1"}
    assert result["games"][1]["twitch_id"] == "2"


def _hvideo(day, view_count, hour=12, **over):
    video = {"id": f"v{day}-{hour}", "created_at": f"2026-10-{day:02d}T{hour:02d}:00:00Z", "view_count": view_count,
             "type": "archive"}
    video.update(over)
    return video


def _vod_routes(pages):
    """helix/videos : pages successives (la dernière se répète) ; ``(status, body, headers)`` tels quels."""
    seen = [0]

    def reply(call):
        item = pages[min(seen[0], len(pages) - 1)]
        seen[0] += 1
        return item
    return {("POST", "/oauth2/token"): [_token()], ("GET", "/helix/videos"): reply}


def _vods(tmp_path, http, game_ids, sleeps=None, **over):
    collectors = veille_sources.default_collectors(
        http, clock=lambda: NOW, sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return collectors["twitch_vods_30d"](_settings(tmp_path, **over), game_ids)


def test_twitch_vods_30d_exact_params_cursor_pagination_and_daily_aggregation(tmp_path):
    http = FakeHttp(_vod_routes([
        (200, {"data": [_hvideo(6, 100), _hvideo(5, 40, hour=20), _hvideo(5, 60, hour=2)], "pagination": {"cursor": "c1"}}),
        (200, {"data": [_hvideo(3, 7)], "pagination": {}})]))
    result = _vods(tmp_path, http, ["1"], trend_days=7)  # fenêtre : 2026-09-30 .. 2026-10-06
    calls = http.to("/helix/videos")
    assert calls[0]["params"] == {"game_id": "1", "language": "fr", "period": "month", "type": "archive",
                                  "sort": "time", "first": 100}
    assert calls[1]["params"] == {**calls[0]["params"], "after": "c1"}
    assert calls[0]["headers"] == {"Client-Id": "cid", "Authorization": "Bearer tok1"}
    points = result["vods"]["1"]["points"]
    assert result["vods"]["1"]["since"] == "2026-09-30"  # plus de curseur : toute la fenêtre est couverte
    assert points == [
        {"date": "2026-09-30", "vods": 0, "views": 0}, {"date": "2026-10-01", "vods": 0, "views": 0},
        {"date": "2026-10-02", "vods": 0, "views": 0}, {"date": "2026-10-03", "vods": 1, "views": 7},
        {"date": "2026-10-04", "vods": 0, "views": 0}, {"date": "2026-10-05", "vods": 2, "views": 100},
        {"date": "2026-10-06", "vods": 1, "views": 100}]
    assert result["skipped"] == 0 and result["rate_limited"] == 0


def test_twitch_vods_30d_day_is_in_veille_timezone_and_older_videos_are_ignored(tmp_path):
    http = FakeHttp(_vod_routes([(200, {"data": [
        {"id": "a", "created_at": "2026-10-05T23:30:00Z", "view_count": 5},  # 01:30 le 6 à Paris
        {"id": "b", "created_at": "2026-09-01T10:00:00Z", "view_count": 9}], "pagination": {}})]))
    points = _vods(tmp_path, http, ["1"], trend_days=7)["vods"]["1"]["points"]
    assert points[-1] == {"date": "2026-10-06", "vods": 1, "views": 5}
    assert [p["date"] for p in points][0] == "2026-09-30" and sum(p["vods"] for p in points) == 1


def test_twitch_vods_30d_cap_with_cursor_left_starts_at_the_oldest_video_and_leaves_older_days_absent(tmp_path):
    http = FakeHttp(_vod_routes([(200, {"data": [_hvideo(6, 10), _hvideo(4, 20)], "pagination": {"cursor": "more"}})]))
    result = _vods(tmp_path, http, ["1"], trend_days=7, twitch_history_pages_max=2)
    assert len(http.to("/helix/videos")) == 2  # pages bornées
    entry = result["vods"]["1"]
    assert entry["since"] == "2026-10-04"
    assert entry["points"] == [{"date": "2026-10-04", "vods": 2, "views": 40}, {"date": "2026-10-05", "vods": 0, "views": 0},
                               {"date": "2026-10-06", "vods": 2, "views": 20}]


def test_twitch_vods_30d_game_without_video_is_measured_zero_over_the_whole_window(tmp_path):
    http = FakeHttp(_vod_routes([(200, {"data": [], "pagination": {}})]))
    entry = _vods(tmp_path, http, ["1"], trend_days=7)["vods"]["1"]
    assert entry["since"] == "2026-09-30" and len(entry["points"]) == 7
    assert all(p["vods"] == 0 and p["views"] == 0 for p in entry["points"])


@pytest.mark.parametrize("video", [{"id": "x", "view_count": 1}, {"id": "x", "created_at": "2026-10-05T10:00:00Z"},
                                   {"id": "x", "created_at": "hier", "view_count": 1},
                                   {"id": "x", "created_at": "2026-10-05T10:00:00Z", "view_count": "9"}])
def test_twitch_vods_30d_missing_or_malformed_field_is_a_source_error(tmp_path, video):
    http = FakeHttp(_vod_routes([(200, {"data": [video], "pagination": {}})]))
    with pytest.raises(veille_sources.SourceError):
        _vods(tmp_path, http, ["1"])


def test_twitch_vods_30d_other_http_error_is_a_source_error(tmp_path):
    http = FakeHttp(_vod_routes([(500, "boom")]))
    with pytest.raises(veille_sources.SourceError):
        _vods(tmp_path, http, ["1"])


def test_twitch_vods_30d_429_waits_until_ratelimit_reset_then_retries_and_logs(tmp_path, caplog):
    reset = str(int(NOW.timestamp()) + 12)
    http = FakeHttp(_vod_routes([(429, "", {"Ratelimit-Reset": reset}), (200, {"data": [], "pagination": {}})]))
    sleeps: list[float] = []
    with caplog.at_level("WARNING"):
        result = _vods(tmp_path, http, ["1"], sleeps)
    assert sleeps == [12.0] and result["rate_limited"] == 0 and result["vods"]["1"] is not None
    assert any("429" in r.message for r in caplog.records)


def test_twitch_vods_30d_429_wait_falls_back_to_retry_after_then_one_second_and_is_capped(tmp_path):
    http = FakeHttp(_vod_routes([(429, "", {"Retry-After": "5"}), (429, ""), (429, "", {"Ratelimit-Reset": "99999999999"}),
                                 (200, {"data": [], "pagination": {}})]))
    sleeps: list[float] = []
    _vods(tmp_path, http, ["1"], sleeps, twitch_history_retry_max=3, twitch_history_retry_wait_max_s=30.0)
    assert sleeps == [5.0, 1.0, 30.0]


def test_twitch_vods_30d_persistent_429_makes_the_game_unknown_and_stops_reading_the_others(tmp_path):
    http = FakeHttp(_vod_routes([(200, {"data": [], "pagination": {}}), (429, "")]))
    result = _vods(tmp_path, http, ["1", "2", "3"], twitch_history_retry_max=2)
    assert result["vods"]["1"] is not None and result["vods"]["2"] is None and result["vods"]["3"] is None
    assert result["rate_limited"] == 2
    assert [c["params"]["game_id"] for c in http.to("/helix/videos")] == ["1", "2", "2", "2"]


@real_only
def test_real_steam_reviews_histogram_has_recent_days(tmp_path):
    recent = veille_sources.default_collectors()["steam_reviews"](_settings(tmp_path), ["570"])["histograms"]["570"]
    assert recent and recent[-1]["value"] > 0


# --- TASK-1d82 (SPEC-85a0 R29) : échéance globale `deadline` des collecteurs réels ---------------------------


class Budget:
    """``deadline`` injectée : secondes restantes, qu'un test épuise quand il veut (``spend``)."""

    def __init__(self, left=100.0):
        self.left = left

    def __call__(self):
        return self.left

    def spend(self):
        self.left = 0.0


def _after(budget, http, n):
    """Transport qui épuise le budget dès la ``n``-ième requête a répondu."""
    def wrapped(*args, **kwargs):
        reply = http(*args, **kwargs)
        if len(http.calls) >= n:
            budget.spend()
        return reply
    wrapped.calls = http.calls
    wrapped.to = http.to
    return wrapped


def test_steam_followers_stops_between_two_appids_without_request_or_pause_and_counts_what_is_left(tmp_path):
    budget, sleeps = Budget(), []
    http = FakeHttp(_followers_routes({str(i): _members(i) for i in range(1, 5)}))
    collectors = veille_sources.default_collectors(_after(budget, http, 2), sleep=sleeps.append)
    result = collectors["steam_followers"](_settings(tmp_path), ["1", "2", "3", "4"], deadline=budget)
    assert result == {"followers": {"1": 1, "2": 2}, "skipped": 0, "rate_limited": 0, "deadline_stopped": 2}
    assert len(http.calls) == 2
    assert sleeps == [3.0]  # seule la pause entre 1 et 2 : plus de pause une fois l'échéance passée


def test_steam_followers_deadline_during_a_429_wait_stops_before_sleeping(tmp_path):
    budget, sleeps = Budget(), []
    http = FakeHttp(_followers_routes({"1": (429, ""), "2": _members(2)}))
    collectors = veille_sources.default_collectors(_after(budget, http, 1), sleep=sleeps.append)
    result = collectors["steam_followers"](_settings(tmp_path), ["1", "2"], deadline=budget)
    assert result["followers"] == {} and result["deadline_stopped"] == 2
    assert len(http.calls) == 1 and sleeps == []


def test_steam_players_stops_between_two_appids(tmp_path):
    budget = Budget()
    http = FakeHttp(_players_routes({"1": 10, "2": 20, "3": 30}))
    collectors = veille_sources.default_collectors(_after(budget, http, 1))
    result = collectors["steam_players"](_settings(tmp_path), ["1", "2", "3"], deadline=budget)
    assert result == {"players": {"1": 10}, "skipped": 0, "deadline_stopped": 2}
    assert len(http.calls) == 1


def test_steam_reviews_stops_between_two_appids_without_request_or_pause(tmp_path):
    budget, sleeps = Budget(), []
    http = FakeHttp(_reviews_routes({a: [_histogram()] for a in "1234"}))
    collectors = veille_sources.default_collectors(_after(budget, http, 2), clock=lambda: NOW, sleep=sleeps.append)
    result = collectors["steam_reviews"](_settings(tmp_path), ["1", "2", "3", "4"], deadline=budget)
    assert result == {"histograms": {"1": [], "2": []}, "skipped": 0, "rate_limited": 0, "deadline_stopped": 2}
    assert len(http.calls) == 2 and sleeps == [2.0]


def test_twitch_vods_30d_stops_between_two_pages_and_drops_the_game_it_was_reading(tmp_path):
    budget = Budget()
    http = FakeHttp(_vod_routes([(200, {"data": [_hvideo(6, 100)], "pagination": {"cursor": "c1"}}),
                                 (200, {"data": [_hvideo(5, 1)], "pagination": {}})]))
    collectors = veille_sources.default_collectors(_after(budget, http, 2), clock=lambda: NOW, sleep=lambda s: None)
    result = collectors["twitch_vods_30d"](_settings(tmp_path), ["1", "2"], deadline=budget)
    assert result == {"vods": {}, "skipped": 0, "rate_limited": 0, "deadline_stopped": 2}  # jamais une série à demi lue
    assert len(http.to("/helix/videos")) == 1


def test_twitch_vods_30d_keeps_the_games_finished_before_the_deadline(tmp_path):
    budget = Budget()
    http = FakeHttp(_vod_routes([(200, {"data": [_hvideo(6, 100)], "pagination": {}}),
                                 (200, {"data": [_hvideo(5, 1)], "pagination": {}})]))
    collectors = veille_sources.default_collectors(_after(budget, http, 2), clock=lambda: NOW, sleep=lambda s: None)
    result = collectors["twitch_vods_30d"](_settings(tmp_path), ["1", "2", "3"], deadline=budget)
    assert sorted(result["vods"]) == ["1"] and result["deadline_stopped"] == 2
    assert len(http.to("/helix/videos")) == 1


def test_igdb_stops_before_the_next_page_and_before_its_pause(tmp_path):
    budget, clock = Budget(), FakeClock()
    full = [_game(i, f"G{i}") for i in range(1, 501)]
    http = FakeHttp({("POST", "/oauth2/token"): [_token()], ("POST", "/v4/games"): [(200, full)]})
    collectors = veille_sources.default_collectors(_after(budget, http, 2), clock=clock, sleep=clock.sleep)
    result = collectors["igdb"](_settings(tmp_path), deadline=budget)  # 1 appel jeton + 1 page, puis plus rien
    assert len(http.to("/v4/games")) == 1 and result["deadline_stopped"] == 1 and len(result["games"]) == 500
    assert clock.now == NOW  # pas d'attente d'espacement une fois l'échéance passée


def _expired():
    return Budget(0.0)


@pytest.mark.parametrize("source, args", [
    ("twitch", ()), ("youtube", ()), ("steam", ()), ("steam_fr", ()), ("igdb", ()),
    ("steam_players", (["1", "2"],)), ("steam_followers", (["1", "2"],)), ("steam_reviews", (["1", "2"],)),
    ("twitch_vods_30d", (["1", "2"],))])
def test_every_real_collector_makes_no_request_once_the_deadline_has_passed(tmp_path, source, args):
    http = FakeHttp({})  # toute requête fait échouer (route inattendue)
    collectors = veille_sources.default_collectors(http, clock=lambda: NOW, sleep=lambda s: None)
    result = collectors[source](_settings(tmp_path), *args, deadline=_expired())
    assert http.calls == []
    assert result["deadline_stopped"] >= 1


def test_twitch_stops_before_the_vod_requests_and_counts_the_games_not_read(tmp_path):
    budget = Budget()
    http = FakeHttp(_twitch_routes())
    collectors = veille_sources.default_collectors(_after(budget, http, 3), clock=lambda: NOW)  # jeton + 2 pages + top = 4
    result = collectors["twitch"](_settings(tmp_path), deadline=budget)
    assert http.to("/helix/videos") == []
    assert result["deadline_stopped"] >= 1 and result["vods"] == []


def test_collectors_without_deadline_still_return_the_exact_documented_shape(tmp_path):
    http = FakeHttp(_followers_routes({"7": _members(5)}))
    assert _followers(tmp_path, http, ["7"]) == {"followers": {"7": 5}, "skipped": 0, "rate_limited": 0}


def test_token_without_expires_in_never_leaks_the_access_token(tmp_path):
    body = {"access_token": "JETON-SECRET-XYZ", "token_type": "bearer"}
    http = FakeHttp(_twitch_routes({("POST", "/oauth2/token"): [(200, body)]}))
    with pytest.raises(veille_sources.SourceError) as err:
        _twitch(tmp_path, http)
    assert "expires_in" in str(err.value) and "JETON-SECRET-XYZ" not in str(err.value)
