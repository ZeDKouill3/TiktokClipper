"""veille.py : relevé quotidien des sujets chauds (TASK-87cd, SPEC-bdd9 R1, R2, R4, R5).

Collecteurs injectés, ``tmp_path`` pour ``state/`` : aucun test ne touche le réseau.
"""

from __future__ import annotations

import json
import threading
import time
from unittest import mock
from datetime import datetime, timedelta, timezone

import pytest

from clipper import veille, veille_sources
from clipper.config import Config, load_config

NOW = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)  # 10:00 à Paris
TODAY = "2026-10-06"


def _make_config(tmp_path, **veille_table) -> Config:
    table = {
        "state_dir": str(tmp_path / "state" / "veille"),
        "twitch_client_id": "cid",
        "twitch_client_secret": "csecret",
        "youtube_api_key": "ykey",
        "max_vods_per_game": 3,  # les tests d'avant R20 choisissent plusieurs VOD d'un même jeu ; R20 fixe 1 explicitement
        **veille_table,
    }
    return Config(
        mode="review",
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "output",
        _sections={
            "worker": {"queue_path": str(tmp_path / "state" / "queue.json")},
            "veille": table,
        },
    )


@pytest.fixture
def config(tmp_path):
    return _make_config(tmp_path)


@pytest.fixture(autouse=True)
def _no_real_access_check(monkeypatch):
    """Aucun test ne lance yt-dlp : le test d'accès par défaut est neutralisé (sauf test dédié)."""
    monkeypatch.setattr(veille_sources, "check_twitch_access", lambda url, timeout_s: None)
    monkeypatch.setattr(veille.time, "sleep", lambda s: None)  # jamais d'attente réelle entre deux essais


def _sdir(tmp_path):
    return tmp_path / "state" / "veille"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _vod(video_id, *, duration_s=7200, age_h=5, game="Jeu Alpha", source="twitch"):
    published = NOW - timedelta(hours=age_h)
    return {
        "video_id": video_id,
        "url": f"https://example.test/{video_id}",
        "title": f"Titre {video_id}",
        "channel_name": "streamer_a",
        "game_name": game,
        "duration_s": duration_s,
        "published_at": published.isoformat(),
        "view_count": 1000,
        "views_per_hour": 200.0,
    }


class Collector:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    def __call__(self, settings):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _collectors(*, viewers=1000, players=5000, vods=(), videos=()):
    return {
        "twitch": Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": viewers}], "vods": list(vods)}),
        "youtube": Collector({"videos": list(videos)}),
        "steam": Collector({"games": [{"appid": "42", "name": "Jeu  Alpha !", "players": players}]}),
        "steam_fr": Collector({"games": []}),
    }


def _write_history(tmp_path, date, *, viewers, players):
    path = _sdir(tmp_path) / "history" / f"{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "date": date, "at": f"{date}T08:00:00+00:00",
        "twitch": {"jeu alpha": {"name": "Jeu Alpha", "viewers_fr": viewers}},
        "steam": {"42": {"name": "Jeu Alpha", "players": players}},
        "youtube": {},
    }), encoding="utf-8")


# --- R1 : réglages ---------------------------------------------------------


def test_config_defaults_are_exactly_r1():
    assert veille.CONFIG_DEFAULTS == {
        "enabled": False, "run_at": "07:00", "timezone": "Europe/Paris", "language": "fr",
        "region": "FR", "taste": "", "max_vods_per_day": 3, "best_clips_per_day": 3,
        "baseline_days": 7, "history_days": 90, "rise_min_pct": 50, "vod_min_duration_s": 1800,
        "vod_max_age_h": 36, "twitch_top_games": 20, "twitch_vods_per_game": 10,
        "youtube_max_results": 50, "youtube_min_duration_s": 600, "steam_top": 100,
        "steam_name_lookups_max": 100, "twitch_access_attempts": 5, "twitch_access_retry_pause_s": 3.0, "twitch_access_workers": 4, "twitch_client_id": "", "twitch_client_secret": "", "youtube_api_key": "",
        "state_dir": "state/veille", "http_timeout_s": 20,
        "steam_rank_gain_min": 5, "steam_risers_max": 10, "steam_sellers_top": 50,
        "upcoming_days": 14, "release_window_days": 15, "igdb_min_hypes": 5, "igdb_recent_max": 12,
        "igdb_upcoming_max": 20, "igdb_pages_max": 4, "youtube_game_min_chars": 5,
        "steam_players_lookups_max": 30, "steam_followers_lookups_max": 50, "steam_followers_pause_s": 3.0,
        "steam_followers_retry_max": 3, "steam_followers_retry_wait_max_s": 60.0,
        "community_min_steam_players": 1000, "community_min_steam_followers": 10000,
        "community_min_twitch_viewers": 200, "community_min_hypes": 50, "max_vods_per_game": 1,
        "trend_days": 30, "trend_games_max": 40, "steam_reviews_pause_s": 2.0, "steam_reviews_retry_max": 3,
        "steam_reviews_retry_wait_max_s": 60.0, "twitch_history_pages_max": 5, "twitch_history_retry_max": 2,
        "twitch_history_retry_wait_max_s": 60.0, "veille_deadline_s": 480,
        "llm_retry_delay_min": 30, "llm_retry_max": 3,
    }


def test_load_config_accepts_veille_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[veille]\nenabled = true\nrun_at = "06:30"\n', encoding="utf-8")
    section = load_config(path).section("veille")
    assert section["enabled"] is True and section["run_at"] == "06:30"
    assert section["max_vods_per_day"] == 3


@pytest.mark.parametrize("table, key", [
    ({"max_vods_per_day": 0}, "max_vods_per_day"),
    ({"best_clips_per_day": 0}, "best_clips_per_day"),
    ({"baseline_days": 0}, "baseline_days"),
    ({"baseline_days": 10, "history_days": 9}, "history_days"),
    ({"run_at": "7h00"}, "run_at"),
    ({"run_at": "25:00"}, "run_at"),
    ({"community_min_steam_players": -1}, "community_min_steam_players"),
    ({"community_min_steam_followers": -1}, "community_min_steam_followers"),
    ({"community_min_twitch_viewers": -1}, "community_min_twitch_viewers"),
    ({"community_min_hypes": -1}, "community_min_hypes"),
    ({"steam_players_lookups_max": -1}, "steam_players_lookups_max"),
    ({"steam_followers_lookups_max": -1}, "steam_followers_lookups_max"),
    ({"steam_followers_pause_s": 0.1}, "steam_followers_pause_s"),
    ({"steam_followers_lookups_max": 61}, "steam_followers_lookups_max"),
    ({"steam_followers_pause_s": 61}, "steam_followers_pause_s"),
    ({"steam_followers_retry_max": -1}, "steam_followers_retry_max"),
    ({"steam_followers_retry_max": 11}, "steam_followers_retry_max"),
    ({"steam_followers_retry_wait_max_s": 0}, "steam_followers_retry_wait_max_s"),
    ({"steam_followers_retry_wait_max_s": 601}, "steam_followers_retry_wait_max_s"),
    ({"max_vods_per_game": 0}, "max_vods_per_game"),
    ({"trend_days": 6}, "trend_days"),
    ({"trend_days": 91}, "trend_days"),
    ({"trend_days": 30.0}, "trend_days"),
    ({"trend_games_max": -1}, "trend_games_max"),
    ({"steam_reviews_pause_s": 0.1}, "steam_reviews_pause_s"),
    ({"steam_reviews_pause_s": 61}, "steam_reviews_pause_s"),
    ({"steam_reviews_retry_max": -1}, "steam_reviews_retry_max"),
    ({"steam_reviews_retry_max": 11}, "steam_reviews_retry_max"),
    ({"steam_reviews_retry_wait_max_s": 0.5}, "steam_reviews_retry_wait_max_s"),
    ({"steam_reviews_retry_wait_max_s": 601}, "steam_reviews_retry_wait_max_s"),
    ({"twitch_history_pages_max": 0}, "twitch_history_pages_max"),
    ({"twitch_history_pages_max": 6}, "twitch_history_pages_max"),
    ({"twitch_history_retry_max": -1}, "twitch_history_retry_max"),
    ({"twitch_history_retry_max": 11}, "twitch_history_retry_max"),
    ({"twitch_history_retry_wait_max_s": 0}, "twitch_history_retry_wait_max_s"),
    ({"twitch_history_retry_wait_max_s": 601}, "twitch_history_retry_wait_max_s"),
    ({"veille_deadline_s": 59}, "veille_deadline_s"),
    ({"veille_deadline_s": 3601}, "veille_deadline_s"),
])
def test_invalid_settings_raise_naming_the_key(tmp_path, table, key):
    config = _make_config(tmp_path, **table)
    with pytest.raises(veille.VeilleError, match=key):
        veille.collect(NOW, collectors=_collectors(), config=config)


def test_history_days_below_trend_days_names_both_keys(tmp_path):
    config = _make_config(tmp_path, trend_days=40, history_days=39)
    with pytest.raises(veille.VeilleError, match=r"history_days.*trend_days|trend_days.*history_days"):
        veille.collect(NOW, collectors=_collectors(), config=config)


# --- R2 / R3 : fichiers, erreurs par source -------------------------------


def test_collect_writes_history_and_day_files(tmp_path, config):
    veille.collect(NOW, collectors=_collectors(), config=config)
    history = _read(_sdir(tmp_path) / "history" / f"{TODAY}.json")
    assert history["date"] == TODAY
    assert history["twitch"] == {"jeu alpha": {"name": "Jeu Alpha", "viewers_fr": 1000, "twitch_id": None}}
    assert history["steam"] == {"42": {"name": "Jeu  Alpha !", "players": 5000, "rank": None, "last_week_rank": None}}
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert day["date"] == TODAY and day["started_at"] and day["finished_at"]
    assert {s: day["sources"][s]["status"] for s in ("twitch", "youtube", "steam")} == {
        "twitch": "ok", "youtube": "ok", "steam": "ok"}
    assert day["sources"]["twitch"]["error"] is None


def test_failing_collector_is_error_others_ok_nothing_raised(tmp_path, config):
    collectors = _collectors()
    collectors["youtube"] = Collector(error=RuntimeError("HTTP 500 boom"))
    veille.collect(NOW, collectors=collectors, config=config)
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert day["sources"]["youtube"]["status"] == "error"
    assert "HTTP 500 boom" in day["sources"]["youtube"]["error"]
    assert day["sources"]["twitch"]["status"] == "ok"
    assert day["sources"]["steam"]["status"] == "ok"


@pytest.mark.parametrize("source, empty_key", [
    ("twitch", "twitch_client_id"),
    ("twitch", "twitch_client_secret"),
    ("youtube", "youtube_api_key"),
])
def test_missing_key_is_error_and_collector_not_called(tmp_path, source, empty_key):
    config = _make_config(tmp_path, **{empty_key: ""})
    collectors = _collectors()
    veille.collect(NOW, collectors=collectors, config=config)
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert day["sources"][source]["status"] == "error"
    assert empty_key in day["sources"][source]["error"]
    assert "Réglages › Veille" in day["sources"][source]["error"]
    assert collectors[source].calls == 0


def test_steam_needs_no_key(tmp_path, config):
    collectors = _collectors()
    veille.collect(NOW, collectors=collectors, config=config)
    assert collectors["steam"].calls == 1


def test_secrets_never_written_to_state(tmp_path, config):
    veille.collect(NOW, collectors=_collectors(vods=[_vod("v1")]), config=config)
    for path in _sdir(tmp_path).rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "csecret" not in text and "ykey" not in text


# --- R4 : montée ------------------------------------------------------------


def test_delta_vs_mean_of_three_previous_days(tmp_path, config):
    _write_history(tmp_path, "2026-10-03", viewers=400, players=1000)
    _write_history(tmp_path, "2026-10-04", viewers=500, players=2000)
    _write_history(tmp_path, "2026-10-05", viewers=600, players=3000)
    veille.collect(NOW, collectors=_collectors(viewers=1000, players=5000), config=config)
    game = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")["games"][0]
    assert game["key"] == "jeu alpha"
    assert game["twitch_avg"] == 500 and game["steam_avg"] == 2000
    assert game["twitch_delta_pct"] == round((1000 - 500) / 500 * 100) == 100
    assert game["steam_delta_pct"] == round((5000 - 2000) / 2000 * 100) == 150
    assert game["baseline_days_available"] == 3


@pytest.mark.parametrize("previous", [0, 1])
def test_not_enough_history_gives_null_delta(tmp_path, config, previous):
    for i in range(previous):
        _write_history(tmp_path, f"2026-10-0{4 + i}", viewers=500, players=2000)
    veille.collect(NOW, collectors=_collectors(), config=config)
    game = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")["games"][0]
    assert game["twitch_delta_pct"] is None and game["steam_delta_pct"] is None
    assert game["baseline_days_available"] == previous


def _rank_collectors(**steam):
    collectors = _collectors()
    collectors["steam"] = Collector({"games": [{"appid": "42", "name": "Jeu Alpha", "players": 5000, **steam}]})
    return collectors


def _alpha(tmp_path, config, **steam):
    veille.collect(NOW, collectors=_rank_collectors(**steam), config=config)
    return _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")


def test_steam_rank_gain_is_an_immediate_signal_without_history(tmp_path, config):
    day = _alpha(tmp_path, config, rank=3, last_week_rank=10)
    game = day["games"][0]
    assert game["baseline_days_available"] == 0 and game["steam_delta_pct"] is None
    assert game["steam_rank"] == 3 and game["steam_rank_gain"] == 7 and game["steam_new_in_top"] is False
    saved = _read(_sdir(tmp_path) / "history" / f"{TODAY}.json")["steam"]["42"]
    assert saved["rank"] == 3 and saved["last_week_rank"] == 10


def test_steam_absent_last_week_means_new_in_top_not_a_number(tmp_path, config):
    game = _alpha(tmp_path, config, rank=5, last_week_rank=0)["games"][0]
    assert game["steam_new_in_top"] is True and game["steam_rank_gain"] is None


def test_steam_unknown_last_week_rank_is_null_not_invented(tmp_path, config):
    game = _alpha(tmp_path, config)["games"][0]
    assert game["steam_new_in_top"] is None and game["steam_rank_gain"] is None


def test_steam_rank_signal_reaches_candidates_and_claude(tmp_path, config):
    collectors = _rank_collectors(rank=3, last_week_rank=10)
    collectors["twitch"] = Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": 10}], "vods": [_vod("v1")]})
    state = veille.collect(NOW, collectors=collectors, config=config)
    signals = state["candidates"][0]["signals"]
    assert signals["steam_rank_gain"] == 7 and signals["steam_new_in_top"] is False
    fake = FakeBackend([{"picks": [], "skipped_note": ""}])
    with llm.use_backend(fake):
        veille.decide(state, config)
    assert fake.calls[0].prompt.count("steam_rank_gain_vs_last_week=7") == 2
    assert "steam_new_in_top=False" in fake.calls[0].prompt


def _steam_only(tmp_path, config, games, twitch_error=True):
    """Relevé Twitch en erreur (ou sans jeu) et Steam ok."""
    collectors = _collectors()
    collectors["twitch"] = (Collector(error=RuntimeError("401")) if twitch_error
                            else Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": 10}], "vods": []}))
    collectors["steam"] = Collector({"games": games})
    veille.collect(NOW, collectors=collectors, config=config)
    return _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")


def _sg(appid, name, rank, last, players=1000):
    return {"appid": appid, "name": name, "players": players, "rank": rank, "last_week_rank": last}


def test_steam_risers_listed_without_twitch(tmp_path, config):
    day = _steam_only(tmp_path, config, [
        _sg("1", "Jeu Nouveau", 5, -1), _sg("2", "Jeu Bond", 10, 20), _sg("3", "Jeu Stable", 3, 4),
        _sg("4", "Jeu Inconnu", 7, None), _sg("5", "Jeu Seuil", 8, 13),
    ])
    assert day["sources"]["twitch"]["status"] == "error"
    by_name = {g["name"]: g for g in day["games"]}
    assert set(by_name) == {"Jeu Nouveau", "Jeu Bond", "Jeu Seuil"}
    new = by_name["Jeu Nouveau"]
    assert new["source"] == "steam" and new["twitch_match"] is False and new["steam_match"] is True
    assert new["twitch_fr_viewers"] is None and new["twitch_delta_pct"] is None
    assert new["steam_rank"] == 5 and new["steam_new_in_top"] is True and new["steam_rank_gain"] is None
    assert new["steam_appid"] == "1" and new["steam_players"] == 1000 and new["vod_count"] == 0
    assert by_name["Jeu Bond"]["steam_rank_gain"] == 10 and by_name["Jeu Bond"]["steam_new_in_top"] is False
    assert by_name["Jeu Seuil"]["steam_rank_gain"] == 5


def test_steam_risers_gain_threshold_and_cap_are_settings(tmp_path):
    games = [_sg(str(i), f"Jeu {i}", i, i + 3) for i in range(1, 4)] + [_sg("9", "Jeu Neuf", 50, 0)]
    day = _steam_only(tmp_path, _make_config(tmp_path, steam_rank_gain_min=3, steam_risers_max=2), games)
    assert len(day["games"]) == 2
    assert day["games"][0]["name"] == "Jeu Neuf"  # nouveau dans le top d'abord


def test_steam_riser_already_on_twitch_is_not_duplicated(tmp_path, config):
    day = _steam_only(tmp_path, config, [_sg("42", "Jeu Alpha", 5, 0)], twitch_error=False)
    assert [g["key"] for g in day["games"]] == ["jeu alpha"]
    assert day["games"][0]["twitch_match"] is True and day["games"][0]["source"] == "twitch"


def test_steam_risers_reach_claude_context(tmp_path, config):
    day = _steam_only(tmp_path, config, [_sg("1", "Jeu Nouveau", 5, -1)])
    fake = FakeBackend([{"picks": [], "skipped_note": ""}])
    day["candidates"] = [{"id": "c1", "source": "twitch", "title": "t", "channel_name": "c", "duration_s": 3600,
                          "published_at": NOW.isoformat()}]
    with llm.use_backend(fake):
        veille.decide(day, config)
    assert "- Jeu Nouveau : twitch_fr_viewers=inconnu" in fake.calls[0].prompt
    assert "steam_new_in_top=True" in fake.calls[0].prompt


def test_twitch_game_without_steam_app_is_not_zero(tmp_path, config):
    collectors = _collectors()
    collectors["twitch"] = Collector({"games": [{"name": "Jéu Béta", "viewers_fr": 10}], "vods": []})
    veille.collect(NOW, collectors=collectors, config=config)
    game = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")["games"][0]
    assert game["steam_match"] is False
    assert game["steam_appid"] is None and game["steam_players"] is None
    assert game["steam_avg"] is None and game["steam_delta_pct"] is None


def test_steam_match_ignores_case_accents_punctuation(tmp_path, config):
    collectors = _collectors()
    collectors["twitch"] = Collector({"games": [{"name": "JEU  alpha", "viewers_fr": 10}], "vods": []})
    collectors["steam"] = Collector({"games": [{"appid": "7", "name": "Jeu: Alpha!", "players": 99}]})
    veille.collect(NOW, collectors=collectors, config=config)
    game = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")["games"][0]
    assert game["steam_match"] is True and game["steam_appid"] == "7" and game["steam_players"] == 99


# --- R5 : candidats ---------------------------------------------------------


def test_candidates_filtered_and_counted(tmp_path, config):
    (tmp_path / "workspace" / "w1").mkdir(parents=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "queue.json").write_text(json.dumps([
        {"id": "q", "video_id": "q1", "url": "u", "channel": None, "action": "run", "status": "waiting"},
    ]), encoding="utf-8")
    (_sdir(tmp_path)).mkdir(parents=True)
    (_sdir(tmp_path) / "seen.json").write_text(json.dumps({
        "queued": [{"candidate_id": "twitch:s1", "video_id": "s1"}],
        "ignored": [{"candidate_id": "twitch:i1", "video_id": "i1"}],
    }), encoding="utf-8")
    vods = [
        _vod("ok1"), _vod("short", duration_s=60), _vod("old", age_h=100),
        _vod("w1"), _vod("q1"), _vod("s1"), _vod("i1"),
    ]
    veille.collect(NOW, collectors=_collectors(vods=vods), config=config)
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert [c["id"] for c in day["candidates"]] == ["twitch:ok1"]
    assert day["excluded"] == {"too_short": 1, "too_old": 1, "already_known": 4, "no_community": 0,
                                  "access_restricted": 0, "access_unreachable": 0, "access_untested": 0,
                                  "access_deadline": 0}
    candidate = day["candidates"][0]
    assert candidate["source"] == "twitch" and candidate["video_id"] == "ok1"
    assert candidate["game_key"] == "jeu alpha" and candidate["duration_s"] == 7200


def test_youtube_candidates_use_youtube_min_duration(tmp_path, config):
    videos = [_vod("yt1", duration_s=700), _vod("yt2", duration_s=100)]
    veille.collect(NOW, collectors=_collectors(videos=videos), config=config)
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert [c["id"] for c in day["candidates"]] == ["youtube:yt1"]
    assert day["excluded"]["too_short"] == 1


# --- rétention, fichiers illisibles ----------------------------------------


def test_old_history_files_are_pruned(tmp_path):
    config = _make_config(tmp_path, history_days=10, trend_days=7)  # history_days >= trend_days (R22)
    _write_history(tmp_path, "2026-09-20", viewers=1, players=1)  # 16 j : supprimé
    _write_history(tmp_path, "2026-09-30", viewers=1, players=1)  # 6 j : gardé
    veille.collect(NOW, collectors=_collectors(), config=config)
    names = sorted(p.name for p in (_sdir(tmp_path) / "history").glob("*.json"))
    assert names == ["2026-09-30.json", f"{TODAY}.json"]


@pytest.mark.parametrize("relpath", ["history/2026-10-05.json", "seen.json"])
def test_unreadable_state_file_raises_naming_it(tmp_path, config, relpath):
    path = _sdir(tmp_path) / relpath
    path.parent.mkdir(parents=True)
    path.write_text("{pas du json", encoding="utf-8")
    with pytest.raises(veille.VeilleError, match=path.name):
        veille.collect(NOW, collectors=_collectors(), config=config)


# ==========================================================================
# TASK-3225 : choix de Claude, exécution, actions, sélection (R6, R7)
# ==========================================================================

from clipper import llm  # noqa: E402
from clipper.llm.fake import FakeBackend  # noqa: E402

AFTER_RUN_AT = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)   # 08:00 à Paris
BEFORE_RUN_AT = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)  # 06:00 à Paris


def _day_state(*candidate_ids):
    return {
        "date": TODAY, "started_at": NOW.isoformat(), "finished_at": None, "sources": {},
        "games": [{"key": "jeu alpha", "name": "Jeu Alpha", "twitch_fr_viewers": 1000, "twitch_delta_pct": 120,
                   "steam_delta_pct": None}],
        "candidates": [{"id": cid, "source": "twitch", "video_id": cid.split(":")[1],
                        "url": f"https://youtu.be/{cid.split(':')[1]}",
                        "title": f"Titre {cid}", "channel_name": "streamer_a", "game_key": "jeu alpha",
                        "game_name": "Jeu Alpha", "duration_s": 7200, "published_at": NOW.isoformat(),
                        "view_count": 4321, "views_per_hour": 99.5,
                        "signals": {"twitch_delta_pct": 120, "steam_delta_pct": None}} for cid in candidate_ids],
        "excluded": {}, "llm": {"status": "skipped", "error": None, "model": None},
        "proposals": [], "skipped_note": "", "refresh_requested_at": None,
    }


def _picks(*ids, note=""):
    return {"picks": [{"candidate_id": i, "reason": f"raison {i}"} for i in ids], "skipped_note": note}


def test_decide_asks_claude_once_with_text_only_and_the_figures(tmp_path):
    config = _make_config(tmp_path, taste="j'aime les jeux de survie", max_vods_per_day=2)
    fake = FakeBackend([_picks("twitch:AAA", "twitch:BBB", note="le reste est trop long")])
    with llm.use_backend(fake):
        state = veille.decide(_day_state("twitch:AAA", "twitch:BBB", "twitch:CCC"), config)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.usage == "veille" and call.images == []
    for text in ("j'aime les jeux de survie", "2", "twitch:AAA", "twitch:CCC", "4321", "99.5", "120"):
        assert text in call.prompt
    assert state["llm"]["status"] == "ok" and state["llm"]["error"] is None
    assert [(p["candidate_id"], p["rank"], p["status"]) for p in state["proposals"]] == [
        ("twitch:AAA", 1, "proposed"), ("twitch:BBB", 2, "proposed")]
    assert state["proposals"][0]["reason"] == "raison twitch:AAA"
    assert state["proposals"][0]["channel"] is None and state["proposals"][0]["queue_entry_id"] is None
    assert state["skipped_note"] == "le reste est trop long"


def test_decide_writes_the_model_really_used_not_the_config_alias(tmp_path):
    config = _make_config(tmp_path)  # aucun [llm.usages.veille] : les défauts de clipper.llm (strong -> opus)
    fake = FakeBackend([_picks("twitch:AAA")])
    with llm.use_backend(fake):
        state = veille.decide(_day_state("twitch:AAA"), config)
    assert fake.calls[0].model == "opus"
    assert state["llm"]["model"] == "opus"


def test_decide_error_state_also_carries_the_resolved_model(tmp_path):
    config = _make_config(tmp_path)
    with llm.use_backend(FakeBackend([{"picks": "pas une liste"}] * 3)):
        state = veille.decide(_day_state("twitch:AAA"), config)
    assert state["llm"]["status"] == "error" and state["llm"]["model"] == "opus"


def test_decide_does_not_leak_into_the_usage_log_of_a_video_in_progress(tmp_path):
    config = _make_config(tmp_path)
    video_log = tmp_path / "workspace" / "VIDEO" / "llm_usage.jsonl"
    with llm.use_backend(FakeBackend([_picks("twitch:AAA")])), llm.usage_log(video_log):
        veille.decide(_day_state("twitch:AAA"), config)
    assert not video_log.exists()
    own = tmp_path / "state" / "veille" / "llm_usage.jsonl"
    assert [json.loads(line)["usage"] for line in own.read_text(encoding="utf-8").splitlines()] == ["veille"]


def test_decide_says_no_declared_preference_when_taste_is_empty(tmp_path):
    fake = FakeBackend([_picks()])
    with llm.use_backend(fake):
        state = veille.decide(_day_state("twitch:AAA"), _make_config(tmp_path))
    assert "aucune préférence déclarée" in fake.calls[0].prompt
    assert state["llm"]["status"] == "ok" and state["proposals"] == []


@pytest.mark.parametrize("answer", [
    _picks("twitch:ZZZ"),                       # id inconnu
    _picks("twitch:AAA", "twitch:AAA"),         # doublon
    _picks("twitch:AAA", "twitch:BBB"),         # plus que max_vods_per_day
    {"picks": [{"candidate_id": "twitch:AAA", "reason": ""}], "skipped_note": ""},  # raison vide
    {"picks": [{"candidate_id": "twitch:AAA", "reason": "x" * 241}], "skipped_note": ""},
    {"picks": [], "skipped_note": "n" * 301},
])
def test_decide_refused_answer_is_an_error_with_no_replacement(tmp_path, answer):
    config = _make_config(tmp_path, max_vods_per_day=1)
    fake = FakeBackend([answer])
    with llm.use_backend(fake):
        state = veille.decide(_day_state("twitch:AAA", "twitch:BBB"), config)
    assert state["llm"]["status"] == "error" and state["llm"]["error"]
    assert state["proposals"] == []


def test_decide_with_no_candidate_skips_without_calling_claude(tmp_path):
    fake = FakeBackend([_picks()])
    with llm.use_backend(fake):
        state = veille.decide(_day_state(), _make_config(tmp_path))
    assert fake.calls == [] and state["llm"]["status"] == "skipped" and state["proposals"] == []


# --- run_if_due -------------------------------------------------------------


def _yt(video_id):
    """VOD à URL youtu.be : worker.enqueue ne lance alors aucun fil de miniature (pas de réseau)."""
    return {**_vod(video_id), "url": f"https://youtu.be/{video_id}"}


def _run_collectors():
    return _collectors(vods=[_yt("AAA"), _yt("BBB")])


def test_run_if_due_disabled_writes_nothing(tmp_path):
    config = _make_config(tmp_path, enabled=False)
    fake = FakeBackend([_picks()])
    with llm.use_backend(fake):
        assert veille.run_if_due(AFTER_RUN_AT, config, _run_collectors()) is None
    assert not _sdir(tmp_path).exists() and fake.calls == []


def test_run_if_due_before_run_at_does_nothing(tmp_path):
    config = _make_config(tmp_path, enabled=True)
    fake = FakeBackend([_picks()])
    with llm.use_backend(fake):
        assert veille.run_if_due(BEFORE_RUN_AT, config, _run_collectors()) is None
    assert not _sdir(tmp_path).exists() and fake.calls == []


def test_run_if_due_after_run_at_collects_decides_and_stamps_both_times(tmp_path):
    config = _make_config(tmp_path, enabled=True)
    fake = FakeBackend([_picks("twitch:AAA")])
    with llm.use_backend(fake):
        veille.run_if_due(AFTER_RUN_AT, config, _run_collectors())
    state = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert state["started_at"] and state["finished_at"] and state["finished_at"] >= state["started_at"]
    assert state["llm"]["status"] == "ok"
    assert [p["candidate_id"] for p in state["proposals"]] == ["twitch:AAA"]
    assert len(state["candidates"]) == 2


def test_run_if_due_started_at_is_written_before_the_collectors_run(tmp_path):
    config = _make_config(tmp_path, enabled=True)
    seen = {}

    def twitch(settings):
        seen["day"] = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
        return {"games": [], "vods": []}

    collectors = {**_run_collectors(), "twitch": twitch}
    with llm.use_backend(FakeBackend([_picks()])):
        veille.run_if_due(AFTER_RUN_AT, config, collectors)
    assert seen["day"]["started_at"] and seen["day"]["finished_at"] is None


def test_run_if_due_does_not_run_twice_the_same_day(tmp_path):
    config = _make_config(tmp_path, enabled=True)
    collectors = _run_collectors()
    with llm.use_backend(FakeBackend([_picks()])):
        veille.run_if_due(AFTER_RUN_AT, config, collectors)
        assert veille.run_if_due(AFTER_RUN_AT + timedelta(hours=1), config, collectors) is None
    assert collectors["steam"].calls == 1


def _frozen_files(tmp_path):
    """Fichiers que le rejeu ne doit pas toucher (SPEC-8a45 R34) -> {chemin: octets}."""
    sdir = _sdir(tmp_path)
    other = (datetime.fromisoformat(TODAY) - timedelta(days=2)).date().isoformat()  # dans la fenêtre d'historique
    for rel, body in ((f"history/{other}.json", '{"old": 1}'), (f"selection/{other}.json", '{"sel": 1}'),
                      ("bilan.json", '{"entries": []}')):
        (sdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (sdir / rel).write_text(body, encoding="utf-8")
    paths = [sdir / "seen.json", sdir / "history" / f"{other}.json", sdir / "selection" / f"{other}.json",
             sdir / "bilan.json", tmp_path / "state" / "queue.json"]
    return {path: path.read_bytes() for path in paths}


def test_run_if_due_refresh_erases_the_day_list_decided_included_and_keeps_the_rest(tmp_path):
    config = _make_config(tmp_path, enabled=True, max_vods_per_day=3)
    collectors = _run_collectors()
    with llm.use_backend(FakeBackend([_picks("twitch:AAA", "twitch:BBB")])):
        veille.run_if_due(AFTER_RUN_AT, config, collectors)
    veille.clip(TODAY, "twitch:AAA", None, config=config)
    veille.ignore(TODAY, "twitch:BBB", config)
    before = _frozen_files(tmp_path)
    refresh = _sdir(tmp_path) / "refresh.json"
    refresh.write_text(json.dumps({"requested_at": AFTER_RUN_AT.isoformat()}), encoding="utf-8")
    during = {}
    result = {"games": [{"name": "Jeu Alpha", "viewers_fr": 1000}],
              "vods": [_yt("AAA"), _yt("BBB"), _yt("CCC"), _yt("DDD")]}

    def twitch(settings):
        during["day"] = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
        return result

    collectors["twitch"] = twitch
    with llm.use_backend(FakeBackend([_picks("twitch:CCC", "twitch:DDD")])):
        veille.run_if_due(AFTER_RUN_AT + timedelta(minutes=5), config, collectors)
    assert not refresh.exists()
    assert during["day"]["started_at"] and during["day"]["finished_at"] is None
    assert during["day"]["proposals"] == [] and during["day"]["candidates"] == []
    state = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert [(p["candidate_id"], p["status"]) for p in state["proposals"]] == [
        ("twitch:CCC", "proposed"), ("twitch:DDD", "proposed")]
    assert {"twitch:AAA", "twitch:BBB"}.isdisjoint({c["id"] for c in state["candidates"]})
    assert state["excluded"]["already_known"] == 2
    assert {path: path.read_bytes() for path in before} == before
    for candidate_id in ("twitch:AAA", "twitch:BBB"):
        with pytest.raises(veille.VeilleError, match="candidat inconnu"):
            veille.clip(TODAY, candidate_id, None, config=config)
        with pytest.raises(veille.VeilleError, match="candidat inconnu"):
            veille.ignore(TODAY, candidate_id, config)


def test_run_if_due_run_at_replay_of_an_unfinished_day_erases_its_proposals(tmp_path):
    config = _make_config(tmp_path, enabled=True)
    with llm.use_backend(FakeBackend([_picks("twitch:AAA", "twitch:BBB")])):
        veille.run_if_due(AFTER_RUN_AT, config, _run_collectors())
    veille.clip(TODAY, "twitch:AAA", None, config=config)
    day_path = _sdir(tmp_path) / "days" / f"{TODAY}.json"
    state = _read(day_path)
    state["finished_at"] = None  # relevé interrompu : repris par run_at
    day_path.write_text(json.dumps(state), encoding="utf-8")
    with llm.use_backend(FakeBackend([_picks("twitch:BBB")])):
        veille.run_if_due(AFTER_RUN_AT + timedelta(minutes=5), config, _run_collectors())
    assert [(p["candidate_id"], p["status"]) for p in _read(day_path)["proposals"]] == [("twitch:BBB", "proposed")]


def test_run_if_due_refresh_works_even_before_run_at_and_with_an_existing_day(tmp_path):
    config = _make_config(tmp_path, enabled=True)
    (_sdir(tmp_path) / "days").mkdir(parents=True)
    (_sdir(tmp_path) / "refresh.json").write_text(json.dumps({"requested_at": "x"}), encoding="utf-8")
    with llm.use_backend(FakeBackend([_picks()])):
        assert veille.run_if_due(BEFORE_RUN_AT, config, _run_collectors()) is not None
    assert not (_sdir(tmp_path) / "refresh.json").exists()


# --- clip / ignore ------------------------------------------------------------


def _decided(tmp_path, **table):
    config = _make_config(tmp_path, enabled=True, **table)
    with llm.use_backend(FakeBackend([_picks("twitch:AAA", "twitch:BBB")])):
        veille.run_if_due(AFTER_RUN_AT, config, _run_collectors())
    return config


def test_clip_enqueues_the_vod_and_records_it(tmp_path):
    config = _decided(tmp_path)
    entry = veille.clip(TODAY, "twitch:AAA", "ma_chaine", short_clips=True, config=config)
    queue = json.loads((tmp_path / "state" / "queue.json").read_text(encoding="utf-8"))
    assert len(queue) == 1 and queue[0]["id"] == entry["id"]
    assert queue[0]["url"] == "https://youtu.be/AAA"
    assert queue[0]["channel"] == "ma_chaine" and queue[0]["action"] == "run" and queue[0]["short_clips"] is True
    state = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    proposal = next(p for p in state["proposals"] if p["candidate_id"] == "twitch:AAA")
    assert proposal["status"] == "queued" and proposal["queue_entry_id"] == entry["id"]
    assert proposal["channel"] == "ma_chaine" and proposal["decided_at"]
    seen = _read(_sdir(tmp_path) / "seen.json")
    assert [(q["candidate_id"], q["video_id"], q["channel"], q["queue_entry_id"]) for q in seen["queued"]] == [
        ("twitch:AAA", entry["video_id"], "ma_chaine", entry["id"])]


def test_clip_and_ignore_copy_the_candidate_snapshot_into_seen(tmp_path):
    config = _decided(tmp_path)
    state = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    candidates = {p["candidate_id"]: p["candidate"] for p in state["proposals"]}
    veille.clip(TODAY, "twitch:AAA", None, config=config)
    veille.ignore(TODAY, "twitch:BBB", config)
    seen = _read(_sdir(tmp_path) / "seen.json")
    for entry, candidate_id in ((seen["queued"][0], "twitch:AAA"), (seen["ignored"][0], "twitch:BBB")):
        candidate = candidates[candidate_id]
        assert entry["candidate_id"] == candidate_id and entry["date"] == TODAY
        for field in ("source", "title", "game_name", "channel_name"):
            assert field in candidate
            assert entry[field] == candidate[field]


def test_clip_twice_or_unknown_candidate_raises(tmp_path):
    config = _decided(tmp_path)
    veille.clip(TODAY, "twitch:AAA", None, config=config)
    with pytest.raises(veille.VeilleError, match="twitch:AAA"):
        veille.clip(TODAY, "twitch:AAA", None, config=config)
    with pytest.raises(veille.VeilleError, match="twitch:ZZZ"):
        veille.clip(TODAY, "twitch:ZZZ", None, config=config)
    with pytest.raises(veille.VeilleError):
        veille.ignore(TODAY, "twitch:AAA", config)


def test_ignore_marks_ignored_and_the_next_collect_drops_the_candidate(tmp_path):
    config = _decided(tmp_path)
    veille.ignore(TODAY, "twitch:BBB", config)
    state = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert next(p for p in state["proposals"] if p["candidate_id"] == "twitch:BBB")["status"] == "ignored"
    assert _read(_sdir(tmp_path) / "seen.json")["ignored"][0]["video_id"] == "BBB"
    again = veille.collect(NOW, collectors=_run_collectors(), config=config)
    assert "twitch:BBB" not in {c["id"] for c in again["candidates"]}
    with pytest.raises(veille.VeilleError):
        veille.ignore(TODAY, "twitch:BBB", config)


# --- select_best / restore ------------------------------------------------------


def _clip_files(tmp_path, video_id, clips):
    """clips : {clip_id: (score, qa_status, extra)} -> output/<video_id>/<clip_id>.json."""
    folder = tmp_path / "output" / video_id
    folder.mkdir(parents=True, exist_ok=True)
    for clip_id, (score, qa, extra) in clips.items():
        (folder / f"{clip_id}.json").write_text(json.dumps(
            {"video_id": video_id, "clip_id": clip_id, "score": score, "qa": {"status": qa, "issues": []}, **extra}),
            encoding="utf-8")
        (folder / f"{clip_id}.mp4").write_bytes(b"mp4-" + clip_id.encode())


def _done_video(tmp_path, video_id, *, finished="2026-10-06T10:00:00+00:00", status="done"):
    path = tmp_path / "workspace" / video_id / "pipeline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"video_id": video_id, "status": status, "updated_at": finished}), encoding="utf-8")


def _seen(tmp_path, *video_ids):
    path = _sdir(tmp_path) / "seen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"queued": [
        {"candidate_id": f"twitch:{v}", "video_id": v, "url": f"https://youtu.be/{v}", "date": TODAY,
         "channel": None, "queue_entry_id": "e", "at": NOW.isoformat()} for v in video_ids], "ignored": []}),
        encoding="utf-8")


def _publish_entry(tmp_path, video_id, clip_id, status):
    path = tmp_path / "state" / "publish" / "ma_chaine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing.append({"video_id": video_id, "clip_id": clip_id, "series_id": None, "part": None, "status": status,
                     "slot_at": None, "decided_at": None, "published_at": None, "error": None})
    path.write_text(json.dumps(existing), encoding="utf-8")


def _sel_config(tmp_path, **table):
    config = _make_config(tmp_path, **table)
    config._sections["publish"] = {"state_dir": str(tmp_path / "state" / "publish")}
    return config


def _tree(tmp_path):
    return {p.relative_to(tmp_path / "output"): (p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted((tmp_path / "output").rglob("*")) if p.is_file()}


def test_select_best_keeps_the_best_scores_and_archives_the_rest(tmp_path):
    config = _sel_config(tmp_path, best_clips_per_day=3)
    _seen(tmp_path, "AAA", "BBB")
    _done_video(tmp_path, "AAA")
    _done_video(tmp_path, "BBB", finished="2026-10-06T12:00:00+00:00")
    _clip_files(tmp_path, "AAA", {"a1": (9.0, "passed", {}), "a2": (5.0, "passed", {}), "a3": (7.5, "passed", {}),
                                  "a4": (9.9, "rejected", {}),
                                  "s-p1": (6.0, "passed", {"part": 1, "parts_total": 2}),
                                  "s-p2": (6.0, "passed", {"part": 2, "parts_total": 2})})
    _clip_files(tmp_path, "BBB", {"b1": (8.0, "passed", {}), "b2": (1.0, "passed", {})})
    _clip_files(tmp_path, "OTHER", {"o1": (10.0, "passed", {})})  # vidéo hors veille
    before = _tree(tmp_path)
    veille.select_best(NOW + timedelta(hours=6), config)
    sel = _read(_sdir(tmp_path) / "selection" / f"{TODAY}.json")
    assert [(k["video_id"], k["clip_id"], k["rank"]) for k in sel["kept"]] == [
        ("AAA", "a1", 1), ("BBB", "b1", 2), ("AAA", "a3", 3)]
    archived = {(a["video_id"], a["clip_id"]) for a in sel["archived"]}
    assert archived == {("AAA", "a2"), ("AAA", "s-p1"), ("AAA", "s-p2"), ("BBB", "b2")}
    assert all(a["rank"] >= 4 for a in sel["archived"])
    assert ("AAA", "a4") not in archived and sel["restored"] == []
    assert not any(v == "OTHER" for v, _ in archived | {(k["video_id"], k["clip_id"]) for k in sel["kept"]})
    assert _tree(tmp_path) == before


def test_select_best_counts_a_series_as_one(tmp_path):
    config = _sel_config(tmp_path, best_clips_per_day=2)
    _seen(tmp_path, "AAA")
    _done_video(tmp_path, "AAA")
    _clip_files(tmp_path, "AAA", {"s-p1": (9.0, "passed", {"part": 1, "parts_total": 2}),
                                  "s-p2": (9.0, "passed", {"part": 2, "parts_total": 2}),
                                  "x": (8.0, "passed", {}), "y": (7.0, "passed", {})})
    veille.select_best(NOW + timedelta(hours=6), config)
    sel = _read(_sdir(tmp_path) / "selection" / f"{TODAY}.json")
    assert {k["clip_id"] for k in sel["kept"]} == {"s-p1", "s-p2", "x"}
    assert {a["clip_id"] for a in sel["archived"]} == {"y"}


@pytest.mark.parametrize("status", ["approved", "scheduled", "published"])
def test_select_best_never_archives_a_clip_in_the_publish_queue(tmp_path, status):
    config = _sel_config(tmp_path, best_clips_per_day=1)
    _seen(tmp_path, "AAA")
    _done_video(tmp_path, "AAA")
    _clip_files(tmp_path, "AAA", {"a1": (9.0, "passed", {}), "a2": (1.0, "passed", {})})
    _publish_entry(tmp_path, "AAA", "a2", status)
    veille.select_best(NOW + timedelta(hours=6), config)
    sel = _read(_sdir(tmp_path) / "selection" / f"{TODAY}.json")
    assert {k["clip_id"] for k in sel["kept"]} == {"a1", "a2"} and sel["archived"] == []


def test_select_best_ignores_unfinished_videos_and_groups_by_day(tmp_path):
    config = _sel_config(tmp_path, best_clips_per_day=1)
    _seen(tmp_path, "AAA", "BBB")
    _done_video(tmp_path, "AAA", status="running")
    _done_video(tmp_path, "BBB", finished="2026-10-05T10:00:00+00:00")
    _clip_files(tmp_path, "AAA", {"a1": (9.0, "passed", {})})
    _clip_files(tmp_path, "BBB", {"b1": (9.0, "passed", {}), "b2": (1.0, "passed", {})})
    veille.select_best(NOW + timedelta(hours=6), config)
    assert not (_sdir(tmp_path) / "selection" / f"{TODAY}.json").exists()
    sel = _read(_sdir(tmp_path) / "selection" / "2026-10-05.json")
    assert [k["clip_id"] for k in sel["kept"]] == ["b1"] and [a["clip_id"] for a in sel["archived"]] == ["b2"]


def test_restore_moves_an_archived_clip_to_restored_and_it_stays_after_recompute(tmp_path):
    config = _sel_config(tmp_path, best_clips_per_day=1)
    _seen(tmp_path, "AAA")
    _done_video(tmp_path, "AAA")
    _clip_files(tmp_path, "AAA", {"a1": (9.0, "passed", {}), "a2": (1.0, "passed", {})})
    veille.select_best(NOW, config)
    veille.restore("AAA", "a2", config)
    path = _sdir(tmp_path) / "selection" / f"{TODAY}.json"
    sel = _read(path)
    assert sel["archived"] == [] and [(r["video_id"], r["clip_id"]) for r in sel["restored"]] == [("AAA", "a2")]
    assert sel["restored"][0]["restored_at"]
    veille.select_best(NOW + timedelta(hours=1), config)
    sel = _read(path)
    assert sel["archived"] == [] and [r["clip_id"] for r in sel["restored"]] == ["a2"]
    with pytest.raises(veille.VeilleError):
        veille.restore("OTHER", "o1", config)


# --- TASK-2784 : top des ventes Steam du pays ----------------------------------


def _sellers_collectors(sellers, **twitch):
    collectors = _collectors()
    collectors["steam_fr"] = Collector({"games": sellers})
    if twitch:
        collectors["twitch"] = Collector(twitch)
    return collectors


def _seller(appid, name, rank, last):
    return {"appid": appid, "name": name, "rank": rank, "last_week_rank": last}


def test_sellers_merge_by_normalized_key_into_twitch_game_without_duplicate(tmp_path, config):
    state = veille.collect(NOW, collectors=_sellers_collectors([_seller("9", "JEU alpha", 4, 14)]), config=config)
    assert [g["key"] for g in state["games"]] == ["jeu alpha"]
    game = state["games"][0]
    assert game["steam_sellers_rank"] == 4 and game["steam_sellers_last_week_rank"] == 14
    assert game["steam_sellers_gain"] == 10 and game["steam_sellers_new"] is False
    assert state["sources"]["steam_fr"]["status"] == "ok" and state["sources"]["steam_fr"]["counts"] == {"games": 1}


def test_sellers_game_without_sellers_entry_has_null_fields(tmp_path, config):
    game = veille.collect(NOW, collectors=_collectors(), config=config)["games"][0]
    assert game["steam_sellers_rank"] is None and game["steam_sellers_gain"] is None and game["steam_sellers_new"] is None


def test_sellers_new_in_top_has_no_numeric_gain(tmp_path, config):
    game = veille.collect(NOW, collectors=_sellers_collectors([_seller("9", "Jeu Alpha", 4, 0)]), config=config)["games"][0]
    assert game["steam_sellers_new"] is True and game["steam_sellers_gain"] is None


def test_sellers_risers_are_added_without_twitch_and_small_gain_is_not(tmp_path, config):
    sellers = [_seller("1", "Montant", 8, 17), _seller("2", "Nouveau", 3, 0), _seller("3", "Stable", 1, 3),
               _seller("4", "Recule", 5, 2)]
    state = veille.collect(NOW, collectors=_sellers_collectors(sellers), config=config)
    added = {g["name"]: g for g in state["games"] if g["source"] == "steam_fr"}
    assert set(added) == {"Montant", "Nouveau"}
    assert added["Montant"]["twitch_match"] is False and added["Montant"]["twitch_fr_viewers"] is None
    assert added["Montant"]["steam_sellers_gain"] == 9 and added["Nouveau"]["steam_sellers_new"] is True
    assert added["Montant"]["steam_match"] is False and added["Montant"]["steam_players"] is None


def test_sellers_riser_already_a_world_steam_riser_is_not_duplicated(tmp_path, config):
    collectors = _collectors()
    collectors["steam"] = Collector({"games": [{"appid": "7", "name": "Montant", "players": 10, "rank": 2, "last_week_rank": 30}]})
    collectors["steam_fr"] = Collector({"games": [_seller("7", "Montant", 8, 17)]})
    games = veille.collect(NOW, collectors=collectors, config=config)["games"]
    assert [g["name"] for g in games].count("Montant") == 1
    montant = next(g for g in games if g["name"] == "Montant")
    assert montant["steam_rank_gain"] == 28 and montant["steam_sellers_gain"] == 9


def test_sellers_risers_are_capped_by_steam_risers_max(tmp_path):
    config = _make_config(tmp_path, steam_risers_max=1)
    sellers = [_seller("1", "A", 3, 0), _seller("2", "B", 4, 0)]
    games = veille.collect(NOW, collectors=_sellers_collectors(sellers), config=config)["games"]
    assert [g["name"] for g in games if g["source"] == "steam_fr"] == ["A"]


def test_sellers_error_is_named_and_other_sources_continue(tmp_path, config):
    collectors = _collectors()
    collectors["steam_fr"] = Collector(error=veille.veille_sources.SourceError("HTTP 500 boom"))
    state = veille.collect(NOW, collectors=collectors, config=config)
    assert state["sources"]["steam_fr"]["status"] == "error" and "HTTP 500 boom" in state["sources"]["steam_fr"]["error"]
    assert state["sources"]["steam"]["status"] == "ok" and state["sources"]["twitch"]["status"] == "ok"
    assert state["games"][0]["steam_sellers_rank"] is None


def test_sellers_saved_in_history_and_signals_reach_candidates_and_claude(tmp_path, config):
    collectors = _sellers_collectors([_seller("9", "Jeu Alpha", 4, 14)],
                                     games=[{"name": "Jeu Alpha", "viewers_fr": 10}], vods=[_vod("v1")])
    state = veille.collect(NOW, collectors=collectors, config=config)
    assert _read(_sdir(tmp_path) / "history" / f"{TODAY}.json")["steam_fr"]["9"] == {
        "name": "Jeu Alpha", "rank": 4, "last_week_rank": 14}
    signals = state["candidates"][0]["signals"]
    assert signals["steam_sellers_gain"] == 10 and signals["steam_sellers_new"] is False
    fake = FakeBackend([{"picks": [], "skipped_note": ""}])
    with llm.use_backend(fake):
        veille.decide(state, config)
    prompt = fake.calls[0].prompt
    assert prompt.count("ventes_fr_rang=4") == 1 and prompt.count("ventes_fr_gain_vs_semaine_derniere=10") == 2


def test_sellers_settings_defaults():
    assert veille.CONFIG_DEFAULTS["steam_sellers_top"] == 50


# --- TASK-a898 : compteur de VOD réservées, miniature conservée -------------


def test_private_vod_count_is_shown_in_the_twitch_source_detail(tmp_path, config):
    collectors = _collectors(vods=[_vod("ok1")])
    collectors["twitch"].result["private_vods"] = 3
    veille.collect(NOW, collectors=collectors, config=config)
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert day["sources"]["twitch"]["counts"] == {"games": 1, "vods": 1, "private": 3, "restricted": 0, "unreachable": 0, "untested": 0}


def test_candidate_carries_the_thumbnail_url_or_none(tmp_path, config):
    with_thumb = {**_vod("t1"), "thumbnail_url": "https://cdn.test/t1.jpg"}
    veille.collect(NOW, collectors=_collectors(vods=[with_thumb, _vod("t2")]), config=config)
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert {c["video_id"]: c["thumbnail_url"] for c in day["candidates"]} == {"t1": "https://cdn.test/t1.jpg", "t2": None}


# --- TASK-9495 : test d'accès des VOD Twitch avant de les proposer ------------


class FakeAccess:
    """Test d'accès factice : ``outcomes[video_id]`` = exception à lever (absent = accessible)."""

    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.urls = []

    def __call__(self, url, timeout_s):
        self.urls.append(url)
        outcome = self.outcomes.get(url.rsplit("/", 1)[-1])
        if outcome is not None:
            raise outcome


def _day(tmp_path):
    return _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")


def test_access_settings_defaults_and_legacy_key():
    assert veille.CONFIG_DEFAULTS["twitch_access_attempts"] == 5
    assert veille.CONFIG_DEFAULTS["twitch_access_retry_pause_s"] == 3.0
    assert "twitch_access_check_max" not in veille.CONFIG_DEFAULTS
    assert "twitch_access_check_max" in veille.LEGACY_KEYS


@pytest.mark.parametrize("key, value", [
    ("twitch_access_attempts", 0), ("twitch_access_attempts", 11), ("twitch_access_attempts", True),
    ("twitch_access_retry_pause_s", -0.1), ("twitch_access_retry_pause_s", 60.5)])
def test_access_settings_bounds_name_the_key(tmp_path, key, value):
    with pytest.raises(veille.VeilleError, match=key):
        veille.settings(_make_config(tmp_path, **{key: value}))


def test_access_settings_accept_the_bounds(tmp_path):
    veille.settings(_make_config(tmp_path, twitch_access_attempts=1, twitch_access_retry_pause_s=0))
    veille.settings(_make_config(tmp_path, twitch_access_attempts=10, twitch_access_retry_pause_s=60))


def test_subscriber_only_vod_is_dropped_and_counted(tmp_path, config):
    err = veille_sources.AccessRestricted("You must be logged into an account that has access to this subscriber-only content")
    access = FakeAccess({"sub": err})
    veille.collect(NOW, collectors=_collectors(vods=[_vod("ok"), _vod("sub")]), config=config, access_check=access)
    day = _day(tmp_path)
    assert [c["video_id"] for c in day["candidates"]] == ["ok"]
    assert day["sources"]["twitch"]["counts"]["restricted"] == 1
    assert day["sources"]["twitch"]["counts"]["private"] == 0


def test_accessible_vod_is_kept_and_has_no_unverified_field(tmp_path, config):
    veille.collect(NOW, collectors=_collectors(vods=[_vod("ok")]), config=config, access_check=FakeAccess())
    cand = _day(tmp_path)["candidates"][0]
    assert "access_unverified" not in cand
    counts = _day(tmp_path)["sources"]["twitch"]["counts"]
    assert (counts["restricted"], counts["unreachable"], counts["untested"]) == (0, 0, 0)


def _game_vods(n, game="Jeu Alpha"):
    """n VOD d'un même jeu, v0 la plus vue (vues décroissantes) : v0 est la première testée."""
    return [{**_vod(f"v{i}", game=game), "view_count": 1000 - i} for i in range(n)]


def _ids(access):
    return [u.rsplit("/", 1)[-1] for u in access.urls]


def _access_excluded(tmp_path):
    return {k: v for k, v in _day(tmp_path)["excluded"].items() if k.startswith("access_")}


def test_check_stops_after_one_accessible_vod_with_max_one(tmp_path):
    config = _make_config(tmp_path, max_vods_per_game=1)
    access = FakeAccess()
    veille.collect(NOW, collectors=_collectors(vods=_game_vods(11)), config=config, access_check=access)
    assert _ids(access) == ["v0"]
    day = _day(tmp_path)
    assert [c["video_id"] for c in day["candidates"]] == ["v0"]
    counts = day["sources"]["twitch"]["counts"]
    assert (counts["restricted"], counts["unreachable"], counts["untested"]) == (0, 0, 10)
    assert _access_excluded(tmp_path) == {"access_restricted": 0, "access_unreachable": 0, "access_untested": 10,
                                           "access_deadline": 0}


def test_most_viewed_restricted_then_second_accessible(tmp_path):
    config = _make_config(tmp_path, max_vods_per_game=1)
    access = FakeAccess({"v0": veille_sources.AccessRestricted("subscriber-only")})
    veille.collect(NOW, collectors=_collectors(vods=_game_vods(11)), config=config, access_check=access)
    assert _ids(access) == ["v0", "v1"]
    day = _day(tmp_path)
    assert [c["video_id"] for c in day["candidates"]] == ["v1"]
    counts = day["sources"]["twitch"]["counts"]
    assert (counts["restricted"], counts["unreachable"], counts["untested"]) == (1, 0, 9)
    assert _access_excluded(tmp_path) == {"access_restricted": 1, "access_unreachable": 0, "access_untested": 9,
                                           "access_deadline": 0}


def test_check_stops_after_two_accessible_vods_with_max_two(tmp_path):
    config = _make_config(tmp_path, max_vods_per_game=2)
    access = FakeAccess()
    veille.collect(NOW, collectors=_collectors(vods=_game_vods(11)), config=config, access_check=access)
    assert _ids(access) == ["v0", "v1"]
    assert [c["video_id"] for c in _day(tmp_path)["candidates"]] == ["v0", "v1"]
    assert _day(tmp_path)["sources"]["twitch"]["counts"]["untested"] == 9


def test_all_vods_dropped_means_all_tested_and_none_untested(tmp_path):
    config = _make_config(tmp_path, max_vods_per_game=1)
    err = veille_sources.AccessRestricted("subscriber-only")
    access = FakeAccess({f"v{i}": err for i in range(11)})
    veille.collect(NOW, collectors=_collectors(vods=_game_vods(11)), config=config, access_check=access)
    assert len(access.urls) == 11
    day = _day(tmp_path)
    assert day["candidates"] == []
    counts = day["sources"]["twitch"]["counts"]
    assert (counts["restricted"], counts["unreachable"], counts["untested"]) == (11, 0, 0)


def test_equal_views_keep_the_source_order(tmp_path):
    config = _make_config(tmp_path, max_vods_per_game=1)
    vods = [_vod("a"), _vod("b"), {**_vod("c"), "view_count": 5000}]
    access = FakeAccess({"c": veille_sources.AccessRestricted("sub")})
    veille.collect(NOW, collectors=_collectors(vods=vods), config=config, access_check=access)
    assert _ids(access) == ["c", "a"]


def test_games_are_tested_in_games_order(tmp_path):
    config = _make_config(tmp_path, max_vods_per_game=1)
    vods = [_vod("b1", game="Jeu Beta"), _vod("a1", game="Jeu Alpha")]
    collectors = _collectors(vods=vods)
    collectors["twitch"].result["games"] = [{"name": "Jeu Alpha", "viewers_fr": 1000}, {"name": "Jeu Beta", "viewers_fr": 900}]
    collectors["steam"].result["games"].append({"appid": "43", "name": "Jeu Beta", "players": 5000})
    access = FakeAccess()
    veille.collect(NOW, collectors=collectors, config=config, access_check=access)
    assert [g["key"] for g in _day(tmp_path)["games"]][:2] == ["jeu alpha", "jeu beta"]
    assert _ids(access) == ["a1", "b1"]


def test_unreachable_after_all_attempts_is_dropped_logged_and_next_vod_tested(tmp_path, caplog):
    config = _make_config(tmp_path, max_vods_per_game=1, twitch_access_attempts=3, twitch_access_retry_pause_s=7.0)
    access = FakeAccess({"v0": ConnectionResetError("WinError 10054 " + "x" * 500)})
    pauses = []
    with caplog.at_level("WARNING", logger="clipper.veille"):
        veille.collect(NOW, collectors=_collectors(vods=_game_vods(2)), config=config, access_check=access,
                       access_sleep=pauses.append)
    assert _ids(access) == ["v0", "v0", "v0", "v1"]
    assert pauses == [7.0, 7.0]  # N-1 pauses au plus par VOD, rien après le dernier essai
    day = _day(tmp_path)
    assert [c["video_id"] for c in day["candidates"]] == ["v1"]
    counts = day["sources"]["twitch"]["counts"]
    assert (counts["restricted"], counts["unreachable"], counts["untested"]) == (0, 1, 0)
    assert _access_excluded(tmp_path)["access_unreachable"] == 1
    message = next(r.getMessage() for r in caplog.records if "https://example.test/v0" in r.getMessage())
    assert "WinError 10054" in message and "x" * 300 not in message  # raison tronquée à 300 caractères


def test_restricted_stops_the_attempts_at_once(tmp_path):
    config = _make_config(tmp_path, max_vods_per_game=1, twitch_access_attempts=5)
    access = FakeAccess({"v0": veille_sources.AccessRestricted("sub")})
    pauses = []
    veille.collect(NOW, collectors=_collectors(vods=_game_vods(2)), config=config, access_check=access,
                   access_sleep=pauses.append)
    assert _ids(access) == ["v0", "v1"] and pauses == []


def test_k_failures_then_success_keeps_the_vod(tmp_path):
    config = _make_config(tmp_path, max_vods_per_game=1, twitch_access_attempts=4)
    calls = []

    def flaky(url, timeout_s):
        calls.append(url)
        if len(calls) <= 2:
            raise ConnectionResetError("WinError 10054")

    pauses = []
    veille.collect(NOW, collectors=_collectors(vods=_game_vods(2)), config=config, access_check=flaky,
                   access_sleep=pauses.append)
    assert len(calls) == 3 and len(pauses) == 2
    day = _day(tmp_path)
    assert [c["video_id"] for c in day["candidates"]] == ["v0"]
    counts = day["sources"]["twitch"]["counts"]
    assert (counts["restricted"], counts["unreachable"], counts["untested"]) == (0, 0, 1)


@pytest.mark.parametrize("bad", [0, 17, True, 2.5, "4"])
def test_access_workers_bounds_name_the_key(tmp_path, bad):
    with pytest.raises(veille.VeilleError, match="twitch_access_workers"):
        veille.settings(_make_config(tmp_path, twitch_access_workers=bad))


def test_access_workers_accept_the_bounds(tmp_path):
    veille.settings(_make_config(tmp_path, twitch_access_workers=1))
    veille.settings(_make_config(tmp_path, twitch_access_workers=16))


def _many_games(n, vods_per_game=2):
    """n jeux (g00 le plus regardé, donc le premier de ``games``), chacun avec ``vods_per_game`` VOD (vues décroissantes)."""
    names = [f"Jeu G{i:02d}" for i in range(n)]
    vods = [{**_vod(f"g{i:02d}v{j}", game=name), "view_count": 1000 - j} for i, name in enumerate(names)
            for j in range(vods_per_game)]
    collectors = _collectors(vods=vods)
    collectors["twitch"].result["games"] = [{"name": name, "viewers_fr": 5000 - i} for i, name in enumerate(names)]
    return collectors


def test_access_runs_games_in_parallel_but_never_more_than_the_workers_setting(tmp_path):
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def access(url, timeout_s):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        threading.Event().wait(0.03)  # time.sleep est neutralisé par la fixture autouse
        with lock:
            state["now"] -= 1

    veille.collect(NOW, collectors=_many_games(9, 1), access_check=access,
                   config=_make_config(tmp_path, max_vods_per_game=1, twitch_access_workers=3))
    assert state["max"] == 3


def test_access_with_one_worker_is_sequential(tmp_path):
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def access(url, timeout_s):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        threading.Event().wait(0.01)
        with lock:
            state["now"] -= 1

    veille.collect(NOW, collectors=_many_games(4, 1), access_check=access,
                   config=_make_config(tmp_path, max_vods_per_game=1, twitch_access_workers=1))
    assert state["max"] == 1


def test_parallel_access_gives_the_same_result_as_sequential(tmp_path):
    outcomes = {
        "g00v0": veille_sources.AccessRestricted("sub"),
        "g02v0": ConnectionResetError("WinError 10054"), "g02v1": ConnectionResetError("WinError 10054"),
        "g03v0": veille_sources.AccessRestricted("sub"), "g03v1": veille_sources.AccessRestricted("sub"),
        "g05v1": veille_sources.AccessRestricted("sub"),
    }
    results = []
    for workers in (1, 4):
        sub = tmp_path / f"w{workers}"
        veille.collect(NOW, collectors=_many_games(7), access_check=FakeAccess(outcomes), access_sleep=lambda s: None,
                       config=_make_config(sub, max_vods_per_game=1, twitch_access_attempts=2, twitch_access_workers=workers))
        day = _day(sub)
        results.append(([c["video_id"] for c in day["candidates"]], day["sources"]["twitch"]["counts"], _access_excluded(sub)))
    assert results[0] == results[1]
    assert results[0][0] == ["g00v1", "g01v0", "g04v0", "g05v0", "g06v0"]
    assert (results[0][1]["restricted"], results[0][1]["unreachable"], results[0][1]["untested"]) == (3, 2, 4)


def test_deadline_cuts_the_least_interesting_games_first_when_testing_in_parallel(tmp_path):
    clock = Clock()
    barrier = threading.Barrier(2)
    asked = []

    def access(url, timeout_s):
        asked.append(url.rsplit("/", 1)[-1])
        barrier.wait(timeout=5)  # les deux premiers jeux sont en vol ensemble
        clock.advance(100)  # l'échéance (60 s) est passée pour tout essai suivant

    state = veille.collect(NOW, collectors=_many_games(6, 1), access_check=access, clock=clock,
                           config=_make_config(tmp_path, max_vods_per_game=1, twitch_access_workers=2,
                                               veille_deadline_s=DEADLINE_S))
    assert sorted(asked) == ["g00v0", "g01v0"]
    assert sorted(c["video_id"] for c in state["candidates"]) == ["g00v0", "g01v0"]
    assert state["excluded"]["access_deadline"] == 4  # les 4 VOD des jeux les moins utiles : non testées, jamais présumées OK
    assert state["sources"]["twitch"]["counts"]["deadline"] == 4
    assert state["sources"]["twitch"]["status"] == "partial"


def test_legacy_access_check_max_in_config_is_accepted(tmp_path):
    veille.settings(_make_config(tmp_path, twitch_access_check_max=30))


def test_filtered_out_vods_are_not_tested(tmp_path, config):
    access = FakeAccess()
    veille.collect(NOW, collectors=_collectors(vods=[_vod("short", duration_s=60), _vod("ok")]), config=config, access_check=access)
    assert [u.rsplit("/", 1)[-1] for u in access.urls] == ["ok"]


def test_no_access_call_when_twitch_source_is_in_error(tmp_path, config):
    collectors = _collectors(vods=[_vod("ok")])
    collectors["twitch"] = Collector(error=RuntimeError("helix down"))
    access = FakeAccess()
    veille.collect(NOW, collectors=collectors, config=config, access_check=access)
    assert access.urls == []


def test_youtube_candidates_are_not_access_tested(tmp_path, config):
    access = FakeAccess()
    veille.collect(NOW, collectors=_collectors(videos=[_vod("yt1", source="youtube")]), config=config, access_check=access)
    assert access.urls == []
    assert "access_unverified" not in _day(tmp_path)["candidates"][0]


def test_default_access_check_goes_through_veille_sources(tmp_path, config, monkeypatch):
    seen = []
    monkeypatch.setattr(veille_sources, "check_twitch_access", lambda url, timeout_s: seen.append((url, timeout_s)))
    veille.collect(NOW, collectors=_collectors(vods=[_vod("ok")]), config=config)
    assert seen == [("https://example.test/ok", 20)]


# ==========================================================================
# TASK-3274 : source IGDB par jeux, sorties du jour, portage, tendance, repère J+N, prompt (SPEC-df51 R11 à R16)
# ==========================================================================

EMPTY_RELEASES = {"recent": [], "upcoming": [], "excluded_low_hypes": 0, "truncated": {"recent": 0, "upcoming": 0}}


def _line(date, platform="PC", region="Worldwide", status="Released"):
    return {"date": date, "ts": 0, "human": date, "platform": platform, "region": region, "status": status,
            "date_format": "YYYY-MM-DD"}


def _rel(igdb_id, name, date=None, *, hypes=10, platform="PC", region="Worldwide", status="Released", lines=None,
         cover="auto", steam_appid=None):
    """Un jeu rendu par le collecteur IGDB : une seule ligne datée, ou ``lines`` (liste de ``_line``)."""
    return {"igdb_id": str(igdb_id), "name": name, "slug": name.lower().replace(" ", "-"),
            "url": f"https://igdb.test/{igdb_id}", "hypes": hypes, "first_release_date": None,
            "cover_image_id": f"co{igdb_id}" if cover == "auto" else cover, "steam_appid": steam_appid,
            "release_dates": lines if lines is not None else [_line(date, platform, region, status)]}


def _with_igdb(games, *, skipped=0, **kwargs):
    collectors = _collectors(**kwargs)
    collectors["igdb"] = Collector({"games": list(games), "skipped_rows": skipped})
    return collectors


def _day(tmp_path):
    return _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")


def _names(entries):
    return [e["name"] for e in entries]


def test_igdb_legacy_releases_max_is_tolerated_in_config_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[veille]\nigdb_releases_max = 30\nigdb_min_hypes = 7\n", encoding="utf-8")
    section = load_config(path).section("veille")
    assert section["igdb_min_hypes"] == 7
    assert "igdb_releases_max" in veille.LEGACY_KEYS and "igdb_releases_max" not in veille.CONFIG_DEFAULTS
    state = veille.collect(NOW, collectors=_with_igdb([_rel(1, "Hytale", TODAY)]),
                           config=_make_config(tmp_path, igdb_releases_max=30))
    assert _names(state["releases"]["recent"]) == ["Hytale"]


@pytest.mark.parametrize("key, bad", [
    ("upcoming_days", 0), ("release_window_days", -1), ("igdb_min_hypes", 0), ("igdb_recent_max", 0),
    ("igdb_upcoming_max", 0), ("igdb_pages_max", 0), ("upcoming_days", True), ("igdb_pages_max", "4"),
    ("igdb_recent_max", True), ("igdb_upcoming_max", "20"),
])
def test_igdb_settings_out_of_bounds_raise_naming_the_key(tmp_path, key, bad):
    config = _make_config(tmp_path, **{key: bad})
    with pytest.raises(veille.VeilleError, match=key):
        veille.collect(NOW, collectors=_collectors(), config=config)


def test_release_window_zero_is_valid(tmp_path):
    state = veille.collect(NOW, collectors=_with_igdb([_rel(1, "Pile Aujourdhui", TODAY)]),
                           config=_make_config(tmp_path, release_window_days=0))
    assert _names(state["releases"]["recent"]) == ["Pile Aujourdhui"]


@pytest.mark.parametrize("empty_key", ["twitch_client_id", "twitch_client_secret"])
def test_igdb_missing_twitch_keys_is_error_and_collector_not_called(tmp_path, empty_key):
    config = _make_config(tmp_path, **{empty_key: ""})
    collectors = _with_igdb([_rel(1, "Hytale", TODAY)])
    state = veille.collect(NOW, collectors=collectors, config=config)
    assert state["sources"]["igdb"]["status"] == "error"
    assert state["sources"]["igdb"]["error"] == f"{empty_key} absente : à saisir dans Réglages › Veille"
    assert collectors["igdb"].calls == 0
    assert state["releases"] == EMPTY_RELEASES


def test_failing_igdb_leaves_empty_releases_no_marker_other_sources_and_history_intact(tmp_path):
    ok_dir, ko_dir = tmp_path / "ok", tmp_path / "ko"
    vods = [_vod("v1")]
    veille.collect(NOW, collectors=_with_igdb([_rel(1, "Jeu Alpha", TODAY)], vods=vods), config=_make_config(ok_dir))
    broken = _collectors(vods=vods)
    broken["igdb"] = Collector(error=veille_sources.SourceError("HTTP 500 igdb boom"))
    state = veille.collect(NOW, collectors=broken, config=_make_config(ko_dir))
    assert state["sources"]["igdb"]["status"] == "error" and "igdb boom" in state["sources"]["igdb"]["error"]
    assert state["releases"] == EMPTY_RELEASES
    assert all(g["release"] is None for g in state["games"])
    assert all(c["signals"]["release_days_since"] is None for c in state["candidates"])
    assert all(state["sources"][s]["status"] == "ok" for s in ("twitch", "youtube", "steam", "steam_fr"))
    history = lambda base: _read(base / "state" / "veille" / "history" / f"{TODAY}.json")  # noqa: E731
    assert history(ko_dir) == history(ok_dir)


def test_igdb_entry_has_every_field_and_dates_come_from_the_window_only(tmp_path):
    game = _rel(1, "Hytale", lines=[
        _line("2026-09-01", "PC", "Europe", "Beta"),                      # avant la fenêtre : ne compte pas
        _line("2026-10-04", "PS5", "Europe", "Released"),
        _line("2026-10-02", "PC", "Worldwide", "Early Access"),           # la plus ancienne dans la fenêtre
        _line("2026-10-04", "PC", "Europe", "Released"),
        _line("2026-10-30", "Xbox Series X|S", "Europe", "Released"),     # après la fenêtre : ne compte pas
    ], hypes=42, cover="abc123", steam_appid="620")
    game["first_release_date"] = 1788220800
    state = veille.collect(NOW, collectors=_with_igdb([game]), config=_make_config(tmp_path))
    (entry,) = state["releases"]["recent"]
    assert entry == {
        "igdb_id": "1", "name": "Hytale", "key": "hytale", "slug": "hytale", "url": "https://igdb.test/1",
        "hypes": 42, "cover_image_id": "abc123", "steam_appid": "620", "first_release_date": 1788220800,
        "date": "2026-10-02", "human": "2026-10-02", "days": -4, "platforms": ["PC", "PS5"],
        "regions": ["Europe", "Worldwide"], "statuses": ["Early Access", "Released"], "portage": True, "trend": None,
        "steam_players_now": None, "steam_followers": None}  # sans jeu dans le relevé : chiffres sur la sortie (R18, R21)
    assert state["releases"]["upcoming"] == []


def test_igdb_window_bounds_recent_and_upcoming(tmp_path):
    games = [_rel(1, "Moins16", "2026-09-20"), _rel(2, "Moins15", "2026-09-21"), _rel(3, "Zero", "2026-10-06"),
             _rel(4, "Plus1", "2026-10-07"), _rel(5, "Plus14", "2026-10-20"), _rel(6, "Plus15", "2026-10-21")]
    state = veille.collect(NOW, collectors=_with_igdb(games), config=_make_config(tmp_path))
    releases = state["releases"]
    assert sorted(_names(releases["recent"])) == ["Moins15", "Zero"]
    assert sorted(_names(releases["upcoming"])) == ["Plus1", "Plus14"]
    assert sorted(r["days"] for r in releases["upcoming"]) == [1, 14]
    assert releases["excluded_low_hypes"] == 0  # hors fenêtre n'est pas « écarté pour hypes »


def test_igdb_game_with_dates_only_outside_the_window_is_not_listed(tmp_path):
    game = _rel(1, "Hors", lines=[_line("2026-09-01"), _line("2026-10-30")])
    state = veille.collect(NOW, collectors=_with_igdb([game]), config=_make_config(tmp_path))
    assert state["releases"] == EMPTY_RELEASES


def test_igdb_earlier_date_outside_the_window_does_not_become_the_date(tmp_path):
    game = _rel(1, "Aion 2", lines=[_line("2025-11-19", "PC", "Asia"), _line("2026-10-08", "PC", "Worldwide")])
    state = veille.collect(NOW, collectors=_with_igdb([game]), config=_make_config(tmp_path))
    (entry,) = state["releases"]["upcoming"]
    assert entry["date"] == "2026-10-08" and entry["days"] == 2 and entry["regions"] == ["Worldwide"]


def test_igdb_portage_new_platform_after_an_earlier_release(tmp_path):
    witcher = _rel(1, "The Witcher 3", lines=[
        _line("2015-05-19", "PC (Microsoft Windows)"), _line("2015-05-19", "PlayStation 4"),
        _line("2026-10-10", "Nintendo Switch 2")])
    state = veille.collect(NOW, collectors=_with_igdb([witcher]), config=_make_config(tmp_path))
    (entry,) = state["releases"]["upcoming"]
    assert entry["portage"] is True and entry["platforms"] == ["Nintendo Switch 2"]


def test_igdb_portage_false_for_same_platform_other_region_or_without_earlier_date(tmp_path):
    aion = _rel(1, "Aion 2", lines=[_line("2025-11-19", "PC", "Asia"), _line("2026-10-08", "PC", "Worldwide")])
    fresh = _rel(2, "Tout Neuf", lines=[_line("2026-10-08", "PC"), _line("2026-10-08", "PS5")])
    old_same = _rel(3, "Meme Plateforme", lines=[_line("2026-09-01", "PS5"), _line("2026-10-08", "PS5", "Europe")])
    state = veille.collect(NOW, collectors=_with_igdb([aion, fresh, old_same]), config=_make_config(tmp_path))
    assert {e["name"]: e["portage"] for e in state["releases"]["upcoming"]} == {
        "Aion 2": False, "Tout Neuf": False, "Meme Plateforme": False}


def test_igdb_portage_true_when_one_window_platform_is_new(tmp_path):
    game = _rel(1, "Mixte", lines=[_line("2026-09-01", "PC"), _line("2026-10-08", "PC", "Europe"),
                                   _line("2026-10-08", "PS5", "Europe")])
    state = veille.collect(NOW, collectors=_with_igdb([game]), config=_make_config(tmp_path))
    assert state["releases"]["upcoming"][0]["portage"] is True


def test_igdb_sort_and_caps_recent_by_hypes_then_days_then_name(tmp_path):
    games = [
        _rel(1, "Vieux", "2026-10-01", hypes=500), _rel(2, "RecentBas", "2026-10-05", hypes=6),
        _rel(3, "RecentHaut", "2026-10-05", hypes=50), _rel(4, "EgalZ", "2026-10-04", hypes=20),
        _rel(5, "EgalA", "2026-10-04", hypes=20), _rel(6, "EgalProche", "2026-10-05", hypes=20),
    ]
    state = veille.collect(NOW, collectors=_with_igdb(games), config=_make_config(tmp_path, igdb_recent_max=5))
    assert _names(state["releases"]["recent"]) == ["Vieux", "RecentHaut", "EgalProche", "EgalA", "EgalZ"]
    assert state["releases"]["truncated"] == {"recent": 1, "upcoming": 0}  # RecentBas coupé


def test_igdb_sort_and_caps_upcoming_by_hypes_then_date_then_name(tmp_path):
    games = [
        _rel(1, "TardHaut", "2026-10-12", hypes=90), _rel(2, "TotBas", "2026-10-08", hypes=6),
        _rel(3, "TotHaut", "2026-10-08", hypes=9), _rel(4, "MemeHypeTard", "2026-10-11", hypes=9),
        _rel(5, "MemeHypeNomB", "2026-10-09", hypes=9), _rel(6, "MemeHypeNomA", "2026-10-09", hypes=9),
    ]
    state = veille.collect(NOW, collectors=_with_igdb(games), config=_make_config(tmp_path, igdb_upcoming_max=4))
    assert _names(state["releases"]["upcoming"]) == ["TardHaut", "TotHaut", "MemeHypeNomA", "MemeHypeNomB"]
    assert state["releases"]["truncated"] == {"recent": 0, "upcoming": 2}


def test_igdb_caps_are_separate_and_counts_are_written(tmp_path):
    recent = [_rel(i, f"R{i}", "2026-10-05", hypes=100 - i) for i in range(1, 6)]
    upcoming = [_rel(10 + i, f"U{i}", "2026-10-08", hypes=100 - i) for i in range(1, 4)]
    state = veille.collect(NOW, collectors=_with_igdb(recent + upcoming, skipped=2),
                           config=_make_config(tmp_path, igdb_recent_max=3, igdb_upcoming_max=2))
    assert _names(state["releases"]["recent"]) == ["R1", "R2", "R3"]
    assert _names(state["releases"]["upcoming"]) == ["U1", "U2"]
    assert state["releases"]["truncated"] == {"recent": 2, "upcoming": 1}
    assert state["sources"]["igdb"]["counts"] == {"rows": 8, "recent": 3, "upcoming": 2, "skipped_rows": 2}
    assert state["sources"]["igdb"]["status"] == "ok" and state["sources"]["igdb"]["error"] is None
    assert _day(tmp_path)["releases"] == state["releases"]  # écrit dans days/<date>.json


def _games_collectors(*, twitch_games, releases, vods=()):
    collectors = _with_igdb(releases, vods=vods)
    collectors["twitch"] = Collector({"games": twitch_games, "vods": list(vods)})
    return collectors


def test_igdb_min_hypes_drops_low_without_trend_and_counts(tmp_path):
    games = [_rel(1, "Gros", "2026-10-05", hypes=100), _rel(2, "Petit", "2026-10-05", hypes=4),
             _rel(3, "Pile", "2026-10-05", hypes=5), _rel(4, "AVenirPetit", "2026-10-08", hypes=1),
             _rel(5, "AVenirGros", "2026-10-08", hypes=5)]
    state = veille.collect(NOW, collectors=_with_igdb(games), config=_make_config(tmp_path))
    assert _names(state["releases"]["recent"]) == ["Gros", "Pile"]
    assert _names(state["releases"]["upcoming"]) == ["AVenirGros"]
    assert state["releases"]["excluded_low_hypes"] == 2  # Petit et AVenirPetit
    assert all(e["trend"] is None for e in state["releases"]["recent"] + state["releases"]["upcoming"])


def test_igdb_low_hypes_game_in_trend_is_kept_matched_by_twitch_igdb_id(tmp_path):
    twitch = [{"name": "AION II (titre Twitch)", "viewers_fr": 1200, "igdb_id": "900"},
              {"name": "Autre", "viewers_fr": 10, "igdb_id": ""}]
    games = [_rel(900, "Aion 2", "2026-10-06", hypes=3), _rel(901, "Inconnu", "2026-10-06", hypes=3)]
    state = veille.collect(NOW, collectors=_games_collectors(twitch_games=twitch, releases=games),
                           config=_make_config(tmp_path))
    (entry,) = state["releases"]["recent"]
    assert entry["name"] == "Aion 2" and entry["hypes"] == 3
    assert entry["trend"]["key"] == "aion ii titre twitch" and entry["trend"]["name"] == "AION II (titre Twitch)"
    assert entry["trend"]["twitch_fr_viewers"] == 1200
    assert state["releases"]["excluded_low_hypes"] == 1  # « Inconnu », ni Twitch ni Steam


def test_igdb_trend_matched_by_normalized_key_and_carries_exactly_the_documented_fields(tmp_path):
    twitch = [{"name": "Hytale", "viewers_fr": 800, "igdb_id": ""}]
    steam = {"games": [{"appid": "42", "name": "HYTALE!", "players": 5000, "rank": 7, "last_week_rank": 0}]}
    sellers = {"games": [{"appid": "42", "name": "Hytale", "rank": 3, "last_week_rank": 9}]}
    collectors = _games_collectors(twitch_games=twitch, releases=[_rel(5, "Hytale", "2026-10-05", hypes=2)])
    collectors["steam"], collectors["steam_fr"] = Collector(steam), Collector(sellers)
    state = veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path))
    (entry,) = state["releases"]["recent"]
    game = next(g for g in state["games"] if g["key"] == "hytale")
    fields = ("key", "name", "twitch_fr_viewers", "twitch_delta_pct", "steam_players", "steam_rank", "steam_rank_gain",
              "steam_new_in_top", "steam_sellers_rank", "steam_sellers_gain", "steam_sellers_new",
              "steam_players_now", "steam_followers", "steam_followers_gain_7d", "community")
    assert entry["trend"] == {k: game[k] for k in fields}
    assert entry["trend"]["twitch_fr_viewers"] == 800 and entry["trend"]["steam_players"] == 5000
    assert entry["trend"]["steam_sellers_rank"] == 3 and entry["trend"]["steam_new_in_top"] is True


def test_igdb_high_hypes_game_without_match_has_null_trend(tmp_path):
    state = veille.collect(NOW, collectors=_with_igdb([_rel(1, "Solo", "2026-10-05", hypes=80)]),
                           config=_make_config(tmp_path))
    assert state["releases"]["recent"][0]["trend"] is None


def test_igdb_min_hypes_setting_is_applied(tmp_path):
    games = [_rel(1, "Dix", "2026-10-05", hypes=10), _rel(2, "Vingt", "2026-10-05", hypes=20)]
    state = veille.collect(NOW, collectors=_with_igdb(games), config=_make_config(tmp_path, igdb_min_hypes=15))
    assert _names(state["releases"]["recent"]) == ["Vingt"] and state["releases"]["excluded_low_hypes"] == 1


def test_game_release_marker_matches_by_igdb_id_first_then_by_key_only_for_recent(tmp_path):
    games = [
        {"name": "Nom Twitch Different", "viewers_fr": 900, "igdb_id": "42"},   # par igdb_id (noms différents)
        {"name": "Hytale", "viewers_fr": 800, "igdb_id": ""},                   # par clé normalisée
        {"name": "Jeu Alpha", "viewers_fr": 700, "igdb_id": "7"},               # l'id gagne sur la clé
        {"name": "A Venir", "viewers_fr": 600, "igdb_id": "9"},                 # sortie à venir : pas de repère
        {"name": "Sans Sortie", "viewers_fr": 500},                             # champ igdb_id absent, aucune sortie
    ]
    releases = [_rel(42, "Autre Nom IGDB", "2026-10-03", hypes=33), _rel(5, "Hytale", "2026-10-05", hypes=77),
                _rel(7, "Pas Alpha", "2026-10-06", hypes=8), _rel(6, "Jeu Alpha", "2026-09-30"),
                _rel(9, "A Venir", "2026-10-09")]
    state = veille.collect(NOW, collectors=_games_collectors(twitch_games=games, releases=releases),
                           config=_make_config(tmp_path))
    by_name = {g["name"]: g for g in state["games"]}
    assert by_name["Nom Twitch Different"]["release"] == {
        "igdb_id": "42", "name": "Autre Nom IGDB", "date": "2026-10-03", "days_since": 3, "hypes": 33}
    assert by_name["Hytale"]["release"] == {
        "igdb_id": "5", "name": "Hytale", "date": "2026-10-05", "days_since": 1, "hypes": 77}
    assert by_name["Jeu Alpha"]["release"] == {
        "igdb_id": "7", "name": "Pas Alpha", "date": "2026-10-06", "days_since": 0, "hypes": 8}
    assert by_name["A Venir"]["release"] is None
    assert by_name["Sans Sortie"]["release"] is None


def test_candidates_copy_release_days_since_from_their_game(tmp_path):
    games = [{"name": "Hytale", "viewers_fr": 800, "igdb_id": "5"}, {"name": "Jeu Alpha", "viewers_fr": 700, "igdb_id": ""}]
    vods = [_vod("h1", game="Hytale"), _vod("a1", game="Jeu Alpha"), _vod("o1", game="Orphelin")]
    state = veille.collect(NOW, collectors=_games_collectors(
        twitch_games=games, vods=vods, releases=[_rel(5, "Hytale", "2026-10-03")]), config=_make_config(tmp_path))
    days = {c["video_id"]: c["signals"]["release_days_since"] for c in state["candidates"]}
    assert days == {"h1": 3, "a1": None}  # « Orphelin » n'a pas de jeu connu : écarté (R19)
    assert state["excluded"]["no_community"] == 1


def _prompt_state(*, releases=None, igdb=None, game_release=None, days_since=None):
    state = _day_state("twitch:AAA")
    state["games"][0].update(release=game_release, steam_players=None)
    state["candidates"][0]["signals"]["release_days_since"] = days_since
    state["releases"] = releases if releases is not None else EMPTY_RELEASES
    state["sources"] = {"igdb": igdb or {"status": "ok", "error": None, "counts": {}}}
    return state


def _decide_prompt(tmp_path, state, **table):
    fake = FakeBackend([_picks()])
    with llm.use_backend(fake):
        veille.decide(state, _make_config(tmp_path, **table))
    assert len(fake.calls) == 1  # un seul appel, comme avant
    return fake.calls[0].prompt


def test_prompt_carries_release_markers_on_game_and_candidate_lines(tmp_path):
    state = _prompt_state(game_release={"igdb_id": "5", "name": "Jeu Alpha", "date": "2026-10-03", "days_since": 3,
                                        "hypes": 77}, days_since=3)
    prompt = _decide_prompt(tmp_path, state)
    game_line = next(line for line in prompt.splitlines() if line.startswith("- Jeu Alpha :"))
    assert "sortie_j_plus=3" in game_line and "hypes_igdb=77" in game_line
    candidate_line = next(line for line in prompt.splitlines() if line.startswith("- id=twitch:AAA"))
    assert "sortie_j_plus=3" in candidate_line


def test_prompt_marks_unknown_release_as_inconnu(tmp_path):
    prompt = _decide_prompt(tmp_path, _prompt_state())
    game_line = next(line for line in prompt.splitlines() if line.startswith("- Jeu Alpha :"))
    assert "sortie_j_plus=inconnu" in game_line and "hypes_igdb=inconnu" in game_line
    candidate_line = next(line for line in prompt.splitlines() if line.startswith("- id=twitch:AAA"))
    assert "sortie_j_plus=inconnu" in candidate_line


def test_prompt_has_release_block_and_priority_instruction(tmp_path):
    releases = {"recent": [{"igdb_id": "5", "name": "Hytale", "days": -3, "hypes": 77, "date": "2026-10-03",
                            "platforms": ["PC", "PS5"]}],
                "upcoming": [{"igdb_id": "6", "name": "Gros Jeu", "days": 4, "hypes": None, "date": "2026-10-10",
                              "platforms": ["Xbox"]}],
                "excluded_low_hypes": 0, "truncated": {"recent": 0, "upcoming": 0}}
    prompt = _decide_prompt(tmp_path, _prompt_state(releases=releases), release_window_days=12)
    assert "Sorties de jeux (IGDB)" in prompt
    assert "Hytale" in prompt and "J+3" in prompt and "77" in prompt and "PC, PS5" in prompt
    assert "Gros Jeu" in prompt and "2026-10-10" in prompt and "J-4" in prompt and "Xbox" in prompt
    assert ("Un jeu sorti depuis 0 à 12 jours est dans sa fenêtre de sortie : à qualité de gameplay égale, "
            "propose d'abord ses VOD ; une sortie à venir n'est pas un motif de choix aujourd'hui.") in prompt


def test_prompt_release_line_says_portage_only_for_a_portage(tmp_path):
    releases = {"recent": [{"igdb_id": "5", "name": "Witcher Trois", "days": -3, "hypes": 77, "date": "2026-10-03",
                            "platforms": ["Nintendo Switch 2"], "portage": True},
                           {"igdb_id": "7", "name": "Jeu Neuf", "days": -1, "hypes": 20, "date": "2026-10-05",
                            "platforms": ["PC"], "portage": False}],
                "upcoming": [{"igdb_id": "6", "name": "Autre Portage", "days": 4, "hypes": 9, "date": "2026-10-10",
                              "platforms": ["Xbox"], "portage": True}],
                "excluded_low_hypes": 0, "truncated": {"recent": 0, "upcoming": 0}}
    lines = _decide_prompt(tmp_path, _prompt_state(releases=releases)).splitlines()
    assert "portage=oui" in next(line for line in lines if line.startswith("- Witcher Trois"))
    assert "portage=oui" in next(line for line in lines if line.startswith("- Autre Portage"))
    assert "portage" not in next(line for line in lines if line.startswith("- Jeu Neuf"))


def test_prompt_says_releases_unavailable_with_the_error(tmp_path):
    igdb = {"status": "error", "error": "HTTP 500 igdb boom", "counts": {}}
    prompt = _decide_prompt(tmp_path, _prompt_state(igdb=igdb))
    assert "Sorties de jeux : indisponibles (HTTP 500 igdb boom)" in prompt
    assert "Sorties de jeux (IGDB)" not in prompt


def test_decide_schema_check_and_call_count_unchanged_with_releases(tmp_path):
    fake = FakeBackend([_picks("twitch:AAA")])
    with llm.use_backend(fake):
        state = veille.decide(_prompt_state(), _make_config(tmp_path))
    assert len(fake.calls) == 1 and fake.calls[0].usage == "veille" and fake.calls[0].images == []
    assert [p["candidate_id"] for p in state["proposals"]] == ["twitch:AAA"]


# ==========================================================================
# TASK-4e7c : jeu d'une VOD YouTube déduit du titre et des tags (correspondance stricte)
# ==========================================================================


def _yt_game(video_id, title, *, tags=None):
    vod = _vod(video_id, game=None, source="youtube")
    vod["title"] = title
    if tags is not None:
        vod["tags"] = tags
    return vod


def _deduce(tmp_path, videos, *, steam=(), igdb=(), **table):
    """Candidats construits (jeu déduit compris), y compris ceux que la communauté écarte ensuite (R19) : un jeu
    Steam « nouveau dans le top » a des joueurs, donc une communauté ; les autres sont lus avant l'écart."""
    collectors = _with_igdb(igdb, videos=videos) if igdb else _collectors(videos=videos)
    collectors["steam"] = Collector({"games": [
        {"appid": str(100 + i), "name": n, "players": 2000, "rank": 1 + i, "last_week_rank": 0}
        for i, n in enumerate(steam)]})
    built: list[dict] = []
    real = veille._to_candidate

    def spy(source, vod):
        built.append(real(source, vod))
        return built[-1]

    with mock.patch.object(veille, "_to_candidate", spy):
        veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path, **table))
    return {c["video_id"]: c for c in built}


def test_title_with_known_game_gives_game_and_source(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "JE FINIS jeu ALPHA en 1h !!")])
    assert cands["y1"]["game_name"] == "Jeu Alpha" and cands["y1"]["game_key"] == "jeu alpha"
    assert cands["y1"]["game_source"] == "titre"


def test_tags_are_searched_too(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "Ma soirée", tags=["gaming", "Jéu  Alpha"])])
    assert cands["y1"]["game_name"] == "Jeu Alpha" and cands["y1"]["game_source"] == "titre"


def test_steam_only_game_is_known(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "Gros run sur Hollow Quest")], steam=["Hollow Quest"])
    assert cands["y1"]["game_name"] == "Hollow Quest"


def test_igdb_game_is_known(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "Premier avis Starfall Online")], steam=["Starfall Online"],
                    igdb=[_rel(7, "Starfall Online", "2026-10-05")])
    assert cands["y1"]["game_name"] == "Starfall Online"
    assert cands["y1"]["signals"]["release_days_since"] == 1


def test_no_match_keeps_game_unidentified(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "Vlog du dimanche")])
    assert cands["y1"]["game_name"] is None and cands["y1"]["game_key"] is None
    assert cands["y1"]["game_source"] is None


def test_whole_words_only(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "Les jeux alphabet pour enfants")])
    assert cands["y1"]["game_name"] is None


def test_longest_name_wins_when_it_contains_the_others(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "Minecraft Dungeons II : le test")],
                    steam=["Minecraft", "Minecraft Dungeons II"])
    assert cands["y1"]["game_name"] == "Minecraft Dungeons II"


def test_shorter_name_alone_still_matches(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "Minecraft hardcore")], steam=["Minecraft", "Minecraft Dungeons II"])
    assert cands["y1"]["game_name"] == "Minecraft"


def test_ambiguity_gives_no_game(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "Hollow Quest contre Starfall Online")],
                    steam=["Hollow Quest", "Starfall Online"])
    assert cands["y1"]["game_name"] is None and cands["y1"]["game_source"] is None


def test_short_names_ignored_and_threshold_is_a_setting(tmp_path):
    video = [_yt_game("y1", "Run de Rust ce soir")]
    assert _deduce(tmp_path, video, steam=["Rust"])["y1"]["game_name"] is None
    assert _deduce(tmp_path, video, steam=["Rust"], youtube_game_min_chars=4)["y1"]["game_name"] == "Rust"


def test_vod_with_a_game_is_untouched_and_twitch_never_deduced(tmp_path):
    yt = _vod("y1", game="Autre Jeu", source="youtube")
    yt["title"] = "Jeu Alpha"
    tw = _vod("t1", game=None)
    tw["title"] = "Jeu Alpha"
    cands = _deduce(tmp_path, [yt])
    assert cands["y1"]["game_name"] == "Autre Jeu" and cands["y1"]["game_source"] is None
    collectors = _collectors(vods=[tw])
    built: list[dict] = []
    real = veille._to_candidate
    with mock.patch.object(veille, "_to_candidate", lambda source, vod: built.append(real(source, vod)) or built[-1]):
        veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path))
    assert [c["game_name"] for c in built] == [None]  # écartée ensuite (R19), mais jamais déduite


def test_deduced_game_carries_trend_signals_and_reaches_claude(tmp_path):
    cands = _deduce(tmp_path, [_yt_game("y1", "Jeu Alpha tout le run")])
    assert "twitch_delta_pct" in cands["y1"]["signals"] and "steam_delta_pct" in cands["y1"]["signals"]
    state = _day(tmp_path)
    assert next(g for g in state["games"] if g["key"] == "jeu alpha")["vod_count"] == 1
    prompt = veille._prompt(state, veille.settings(_make_config(tmp_path)))
    assert "jeu=Jeu Alpha (déduit du titre)" in prompt


def test_no_llm_call_for_the_deduction(tmp_path):
    fake = FakeBackend([])
    with llm.use_backend(fake):
        _deduce(tmp_path, [_yt_game("y1", "Jeu Alpha")])
    assert fake.calls == []


# ==========================================================================
# TASK-82da : Steam officiel, communauté, diversité (SPEC-df51 R11, R15, R18 à R21)
# ==========================================================================


class Lookup:
    """Collecteur par appid : garde les appids de chaque appel et rend ``results`` (ou lève ``error``)."""

    def __init__(self, key, results=None, error=None, skipped=0, rate_limited=0):
        self.key, self.results, self.error, self.skipped = key, results or {}, error, skipped
        self.rate_limited = rate_limited
        self.calls: list[list[str]] = []
        self.settings: list[dict] = []

    def __call__(self, settings, appids):
        self.calls.append(list(appids))
        self.settings.append(settings)
        if self.error is not None:
            raise self.error
        out = {self.key: {a: self.results.get(a) for a in appids}, "skipped": self.skipped}
        if self.rate_limited:
            out["rate_limited"] = self.rate_limited
        return out


def _steam_row(appid, name, players, concurrent=None, **extra):
    return {"appid": appid, "name": name, "players": players, "concurrent": concurrent, **extra}


def _community_collectors(*, players=None, followers=None, releases=(), twitch=(("Jeu Alpha", 1000),), steam=None,
                          sellers=(), players_error=None, followers_error=None):
    """Scénario R18 : Twitch + top 100 Steam + ventes FR + IGDB, collecteurs par appid injectés."""
    collectors = _with_igdb(releases)
    collectors["twitch"] = Collector({"games": [{"name": n, "viewers_fr": v, "igdb_id": ""} for n, v in twitch], "vods": []})
    collectors["steam"] = Collector({"games": steam if steam is not None else [
        _steam_row("42", "Jeu Alpha", 5000, 1200), _steam_row("43", "Top Sans Live", 3000),
        _steam_row("44", "Autre Top", 2000, 800)]})
    collectors["steam_fr"] = Collector({"games": list(sellers)})
    collectors["steam_players"] = Lookup("players", players or {}, players_error)
    collectors["steam_followers"] = Lookup("followers", followers or {}, followers_error)
    return collectors


SELLER_50 = {"appid": "50", "name": "Vente Hors Top", "rank": 2, "last_week_rank": 0}


def _scenario(tmp_path, **over):
    kwargs = dict(
        players={"43": 2500, "50": None, "60": 777},
        followers={"42": 120000, "43": 5, "60": 9000, "70": 321, "44": 44000},
        twitch=(("Jeu Alpha", 1000), ("Top Sans Live", 500)), sellers=[SELLER_50],
        releases=[_rel(1, "Hytale", "2026-10-05", hypes=80, steam_appid="60"),
                  _rel(2, "Jeu Alpha", "2026-10-06", hypes=20, steam_appid="42"),
                  _rel(3, "Futur", "2026-10-09", hypes=30, steam_appid="70")])
    table = over.pop("table", {})
    kwargs.update(over)
    collectors = _community_collectors(**kwargs)
    state = veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path, **table))
    return state, collectors


def _game_of(state, key):
    return next(g for g in state["games"] if g["key"] == key)


def test_steam_players_collector_called_last_with_ordered_unique_unknown_snapshot_appids(tmp_path):
    state, collectors = _scenario(tmp_path)
    # jeux de games sans instantané (top 100 sans concurrent, puis ventes FR hors top), puis sortie récente sans jeu
    assert collectors["steam_players"].calls == [["43", "50", "60"]]
    assert state["sources"]["steam_players"]["status"] == "ok" and state["sources"]["steam_players"]["error"] is None
    assert state["sources"]["steam_players"]["counts"] == {"requested": 3, "found": 2, "unknown": 1, "skipped": 0}
    assert collectors["steam_players"].settings[0]["steam_players_lookups_max"] == 30
    assert list(state["sources"]) == list(veille.SOURCES)


def test_steam_players_now_is_snapshot_and_peak_stays_the_peak(tmp_path):
    state, _ = _scenario(tmp_path)
    alpha, tsl, vente = _game_of(state, "jeu alpha"), _game_of(state, "top sans live"), _game_of(state, "vente hors top")
    assert (alpha["steam_players"], alpha["steam_players_now"]) == (5000, 1200)   # concurrent du top 100, jamais relevé
    assert (tsl["steam_players"], tsl["steam_players_now"]) == (3000, 2500)       # pic du top 100 + instantané relevé
    assert (vente["steam_players"], vente["steam_players_now"]) == (None, None)   # 404 : inconnu, pas 0


def test_steam_players_now_on_trend_and_on_release_without_game(tmp_path):
    state, _ = _scenario(tmp_path)
    recent = {e["name"]: e for e in state["releases"]["recent"]}
    assert recent["Jeu Alpha"]["trend"]["steam_players_now"] == 1200
    assert recent["Hytale"]["trend"] is None and recent["Hytale"]["steam_players_now"] == 777
    assert state["releases"]["upcoming"][0]["steam_players_now"] is None  # « Futur » : pas relevé (sortie à venir)


def test_steam_players_source_error_sets_no_now_and_the_rest_continues(tmp_path):
    state, _ = _scenario(tmp_path, players_error=veille_sources.SourceError("HTTP 500 sur steam players"))
    assert state["sources"]["steam_players"]["status"] == "error" and "HTTP 500" in state["sources"]["steam_players"]["error"]
    assert _game_of(state, "top sans live")["steam_players_now"] is None
    assert state["releases"]["recent"][0]["steam_players_now"] is None
    assert all(state["sources"][s]["status"] == "ok" for s in ("twitch", "steam", "igdb", "steam_followers"))
    assert state["games"]


def test_steam_players_lookups_max_zero_is_skipped_without_call(tmp_path):
    state, collectors = _scenario(tmp_path, table={"steam_players_lookups_max": 0})
    assert collectors["steam_players"].calls == []
    assert state["sources"]["steam_players"]["status"] == "skipped" and state["sources"]["steam_players"]["error"] is None


def test_steam_players_list_is_cut_at_lookups_max_and_the_rest_counted_skipped(tmp_path):
    state, collectors = _scenario(tmp_path, table={"steam_players_lookups_max": 2}, players={"43": 2500, "50": 9})
    assert collectors["steam_players"].calls == [["43", "50"]]
    assert state["sources"]["steam_players"]["counts"] == {"requested": 2, "found": 2, "unknown": 0, "skipped": 1}


def _write_day(tmp_path, date, **sections):
    path = _sdir(tmp_path) / "history" / f"{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": date, "at": f"{date}T08:00:00+00:00", "twitch": {}, "steam": {}, "youtube": {},
                                **sections}), encoding="utf-8")


def test_history_file_keeps_every_snapshot_and_follower_count_of_the_day(tmp_path):
    _scenario(tmp_path)
    history = _read(_sdir(tmp_path) / "history" / f"{TODAY}.json")
    assert history["steam_now"] == {"42": 1200, "44": 800, "43": 2500, "60": 777}  # null jamais écrit
    assert history["steam_followers"] == {"42": 120000, "43": 5, "60": 9000, "70": 321, "44": 44000}
    assert history["steam"]["42"]["players"] == 5000 and "concurrent" not in history["steam"]["42"]


def test_players_history_and_now_trend_come_from_the_daily_files_without_interpolation(tmp_path):
    _write_day(tmp_path, "2026-10-03", steam={"42": {"name": "Jeu Alpha", "players": 1000}}, steam_now={"42": 300})
    _write_day(tmp_path, "2026-10-04", steam={"42": {"name": "Jeu Alpha", "players": 2000}}, steam_now={"42": 500})
    _write_day(tmp_path, "2026-10-05", steam={"42": {"name": "Jeu Alpha", "players": 3000}})
    state, _ = _scenario(tmp_path)
    alpha = _game_of(state, "jeu alpha")
    assert alpha["steam_players_history"] == [
        {"date": "2026-10-03", "kind": "peak", "players": 1000}, {"date": "2026-10-03", "kind": "now", "players": 300},
        {"date": "2026-10-04", "kind": "peak", "players": 2000}, {"date": "2026-10-04", "kind": "now", "players": 500},
        {"date": "2026-10-05", "kind": "peak", "players": 3000},
        {"date": TODAY, "kind": "peak", "players": 5000}, {"date": TODAY, "kind": "now", "players": 1200}]
    assert alpha["steam_avg"] == 2000 and alpha["steam_delta_pct"] == 150          # pics seulement
    assert alpha["steam_now_avg"] == 400 and alpha["steam_now_delta_pct"] == 200    # instantanés seulement : (1200-400)/400


def test_now_trend_is_null_without_previous_snapshot_day(tmp_path):
    _write_day(tmp_path, "2026-10-05", steam={"42": {"name": "Jeu Alpha", "players": 3000}})
    state, _ = _scenario(tmp_path)
    alpha = _game_of(state, "jeu alpha")
    assert alpha["steam_now_avg"] is None and alpha["steam_now_delta_pct"] is None
    assert [p["date"] for p in alpha["steam_players_history"]] == ["2026-10-05", TODAY, TODAY]


def test_steam_followers_collector_gets_ordered_unique_appids_and_values_land_everywhere(tmp_path):
    state, collectors = _scenario(tmp_path)
    # games (Jeu Alpha, Top Sans Live, Vente Hors Top), sorties recent puis upcoming hors games, reste du top 100
    assert collectors["steam_followers"].calls == [["42", "43", "50", "60", "70", "44"]]
    assert state["sources"]["steam_followers"]["counts"] == {"requested": 6, "found": 5, "unknown": 1, "skipped": 0}
    assert _game_of(state, "jeu alpha")["steam_followers"] == 120000
    assert _game_of(state, "vente hors top")["steam_followers"] is None
    recent = {e["name"]: e for e in state["releases"]["recent"]}
    assert recent["Jeu Alpha"]["trend"]["steam_followers"] == 120000
    assert recent["Hytale"]["steam_followers"] == 9000 and recent["Hytale"]["trend"] is None
    assert state["releases"]["upcoming"][0]["steam_followers"] == 321


def test_steam_followers_list_is_cut_at_lookups_max_and_zero_is_skipped(tmp_path):
    state, collectors = _scenario(tmp_path, table={"steam_followers_lookups_max": 4})
    assert collectors["steam_followers"].calls == [["42", "43", "50", "60"]]
    assert state["sources"]["steam_followers"]["counts"]["skipped"] == 2
    state, collectors = _scenario(tmp_path / "zero", table={"steam_followers_lookups_max": 0})
    assert collectors["steam_followers"].calls == [] and state["sources"]["steam_followers"]["status"] == "skipped"


def test_steam_followers_error_sets_no_follower_and_the_rest_continues(tmp_path):
    state, _ = _scenario(tmp_path, followers_error=veille_sources.SourceError("HTTP 503 memberslistxml"))
    assert state["sources"]["steam_followers"]["status"] == "error"
    assert all(g["steam_followers"] is None for g in state["games"])
    assert _read(_sdir(tmp_path) / "history" / f"{TODAY}.json")["steam_followers"] == {}
    assert state["sources"]["steam_players"]["status"] == "ok" and state["sources"]["igdb"]["status"] == "ok"


def test_steam_followers_rate_limited_is_a_visible_partial_status_and_the_rest_continues(tmp_path):
    collectors = _community_collectors(releases=[_rel(1, "Hytale", "2026-10-05", hypes=80, steam_appid="60")])
    collectors["steam_followers"] = Lookup("followers", {"42": 120000}, rate_limited=2)
    state = veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path))
    source = state["sources"]["steam_followers"]
    assert source["status"] == "partial" and "429" in source["error"]
    assert source["counts"]["rate_limited"] == 2 and source["counts"]["found"] == 1
    assert _game_of(state, "jeu alpha")["steam_followers"] == 120000  # les abonnés lus sont gardés
    assert state["sources"]["steam_players"]["status"] == "ok" and state["sources"]["igdb"]["status"] == "ok"


def test_steam_followers_without_rate_limit_stays_ok_with_no_rate_limited_count(tmp_path):
    state, _ = _scenario(tmp_path)
    assert state["sources"]["steam_followers"]["status"] == "ok"
    assert "rate_limited" not in state["sources"]["steam_followers"]["counts"]


def test_followers_gain_7d_from_the_exact_day_file_else_null(tmp_path):
    _write_day(tmp_path, "2026-09-29", steam_followers={"42": 100000})
    _write_day(tmp_path, "2026-10-02", steam_followers={"42": 110000, "43": 1})
    state, _ = _scenario(tmp_path)
    alpha = _game_of(state, "jeu alpha")
    assert alpha["steam_followers_gain_7d"] == 20000
    assert alpha["steam_followers_history"] == [
        {"date": "2026-09-29", "followers": 100000}, {"date": "2026-10-02", "followers": 110000},
        {"date": TODAY, "followers": 120000}]
    assert _game_of(state, "top sans live")["steam_followers_gain_7d"] is None  # appid absent du fichier J-7
    assert _game_of(state, "vente hors top")["steam_followers_gain_7d"] is None  # abonnés d'aujourd'hui null


@pytest.mark.parametrize("day", [None, "2026-09-30"])
def test_followers_gain_7d_is_null_without_the_exact_file(tmp_path, day):
    if day:
        _write_day(tmp_path, day, steam_followers={"42": 100000})  # J-6 seulement
    state, _ = _scenario(tmp_path)
    assert _game_of(state, "jeu alpha")["steam_followers_gain_7d"] is None


def test_followers_gain_7d_is_on_trend_too(tmp_path):
    _write_day(tmp_path, "2026-09-29", steam_followers={"42": 100000})
    state, _ = _scenario(tmp_path)
    recent = {e["name"]: e for e in state["releases"]["recent"]}
    assert recent["Jeu Alpha"]["trend"]["steam_followers_gain_7d"] == 20000
    assert recent["Jeu Alpha"]["trend"]["community"]["ok"] is True


# --- R19 : communauté -----------------------------------------------------


def _community_of(tmp_path, *, viewers=10, peak=10, followers=10, hypes=1, table=None):
    """Communauté du jeu « Jeu Alpha » : un seul chiffre à la fois dépasse un seuil."""
    steam = [_steam_row("42", "Jeu Alpha", peak, 5)]
    collectors = _community_collectors(
        twitch=(("Jeu Alpha", viewers),), steam=steam, followers={"42": followers},
        releases=[_rel(1, "Jeu Alpha", "2026-10-05", hypes=hypes)])
    state = veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path, **(table or {})))
    return _game_of(state, "jeu alpha")["community"]


@pytest.mark.parametrize("over, met", [
    ({"peak": 1000}, ["steam"]), ({"followers": 10000}, ["followers"]),
    ({"viewers": 200}, ["twitch"]), ({"hypes": 50}, ["hypes"]),
    ({"peak": 999, "followers": 9999, "viewers": 199, "hypes": 49}, []),
    ({"peak": 1000, "followers": 10000, "viewers": 200, "hypes": 50}, ["steam", "followers", "twitch", "hypes"]),
])
def test_community_ok_when_any_single_threshold_is_reached(tmp_path, over, met):
    community = _community_of(tmp_path, **over)
    assert community["met"] == met and community["ok"] is bool(met)


def test_community_carries_the_figures_and_the_kind_of_steam_number(tmp_path):
    assert _community_of(tmp_path, peak=1500, followers=20000, viewers=300, hypes=60) == {
        "ok": True, "steam_players": 1500, "steam_kind": "peak", "steam_followers": 20000, "twitch_fr_viewers": 300,
        "hypes": 60, "met": ["steam", "followers", "twitch", "hypes"]}


def test_community_uses_the_snapshot_when_the_peak_is_unknown(tmp_path):
    state, _ = _scenario(tmp_path, players={"50": 1500})
    vente = _game_of(state, "vente hors top")["community"]
    assert (vente["steam_players"], vente["steam_kind"], vente["ok"], vente["met"]) == (1500, "now", True, ["steam"])


def test_community_all_unknown_is_not_ok_even_with_zero_thresholds(tmp_path):
    zero = {f"community_min_{k}": 0 for k in ("steam_players", "steam_followers", "twitch_viewers", "hypes")}
    state, _ = _scenario(tmp_path, table=zero)
    assert _game_of(state, "vente hors top")["community"] == {
        "ok": False, "steam_players": None, "steam_kind": None, "steam_followers": None, "twitch_fr_viewers": None,
        "hypes": None, "met": []}
    assert _game_of(state, "jeu alpha")["community"]["ok"] is True  # un seuil à 0 est atteint par toute valeur connue


def test_community_zero_threshold_is_reached_by_a_known_zero(tmp_path):
    community = _community_of(tmp_path, viewers=0, peak=0, followers=0, hypes=0, table={
        "community_min_steam_players": 0, "community_min_steam_followers": 0, "community_min_twitch_viewers": 0,
        "community_min_hypes": 0, "igdb_min_hypes": 1})
    assert community["ok"] is True and "twitch" in community["met"]


# --- R19 : candidats sans communauté ---------------------------------------


def _vod_collectors(vods):
    collectors = _community_collectors(steam=[], releases=[])
    collectors["twitch"] = Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": 1000, "igdb_id": ""},
                                                {"name": "Jeu Froid", "viewers_fr": 5, "igdb_id": ""}], "vods": vods})
    return collectors


def test_candidates_of_a_game_without_community_or_without_game_are_dropped_and_counted(tmp_path):
    vods = [_vod("hot1", game="Jeu Alpha"), _vod("cold1", game="Jeu Froid"), _vod("cold2", game="Jeu Froid"),
            _vod("orphan", game="Orphelin")]
    state = veille.collect(NOW, collectors=_vod_collectors(vods), config=_make_config(tmp_path))
    assert [c["video_id"] for c in state["candidates"]] == ["hot1"]
    assert state["excluded"]["no_community"] == 3
    assert {g["key"] for g in state["games"]} == {"jeu alpha", "jeu froid"}  # tous les jeux restent
    assert _game_of(state, "jeu froid")["community"]["ok"] is False


def test_dropped_candidates_are_not_in_the_prompt_nor_access_tested(tmp_path):
    access = FakeAccess()
    vods = [_vod("hot1", game="Jeu Alpha"), _vod("cold1", game="Jeu Froid")]
    config = _make_config(tmp_path)
    state = veille.collect(NOW, collectors=_vod_collectors(vods), config=config, access_check=access)
    assert [u.rsplit("/", 1)[-1] for u in access.urls] == ["hot1"]
    fake = FakeBackend([_picks()])
    with llm.use_backend(fake):
        veille.decide(state, config)
    assert "twitch:hot1" in fake.calls[0].prompt and "cold1" not in fake.calls[0].prompt


# --- R15 : prompt ----------------------------------------------------------


def _hot_state(tmp_path, **table):
    _write_day(tmp_path, "2026-10-04", steam={"42": {"name": "Jeu Alpha", "players": 2000}}, steam_now={"42": 500})
    _write_day(tmp_path, "2026-10-05", steam={"42": {"name": "Jeu Alpha", "players": 3000}}, steam_now={"42": 300})
    _write_day(tmp_path, "2026-09-29", steam_followers={"42": 100000})
    collectors = _community_collectors(
        followers={"42": 120000}, releases=[_rel(2, "Jeu Alpha", "2026-10-06", hypes=20, steam_appid="42")])
    collectors["twitch"] = Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": 1000, "igdb_id": ""}],
                                      "vods": [_vod("hot1", game="Jeu Alpha")]})
    config = _make_config(tmp_path, **table)
    return veille.collect(NOW, collectors=collectors, config=config), config


def _prompt_of(state, config):
    fake = FakeBackend([_picks()])
    with llm.use_backend(fake):
        veille.decide(state, config)
    return fake.calls[0].prompt


def test_prompt_names_both_measures_and_gives_game_figures(tmp_path):
    state, config = _hot_state(tmp_path)
    prompt = _prompt_of(state, config)
    assert "pic du jour du top 100" in prompt and "instantané à l'heure du relevé" in prompt
    game_line = next(line for line in prompt.splitlines() if line.startswith("- Jeu Alpha :"))
    for text in ("steam_players=5000", "steam_players_now=1200", "steam_now_delta_pct=200", "steam_abonnes=120000",
                 "steam_abonnes_gain_7j=20000", "communaute=ok"):
        assert text in game_line


def test_prompt_says_insufficient_history_instead_of_a_figure(tmp_path):
    state, config = _hot_state(tmp_path)
    for game in state["games"]:
        game.update(steam_now_delta_pct=None, steam_followers_gain_7d=None, steam_players_now=None, steam_followers=None)
    game_line = next(line for line in _prompt_of(state, config).splitlines() if line.startswith("- Jeu Alpha :"))
    assert "steam_now_delta_pct=historique insuffisant" in game_line
    assert "steam_abonnes_gain_7j=historique insuffisant" in game_line
    assert "steam_players_now=inconnu" in game_line and "steam_abonnes=inconnu" in game_line


def test_prompt_candidate_line_has_community_figures_and_diversity_instruction(tmp_path):
    state, config = _hot_state(tmp_path, max_vods_per_game=1)
    prompt = _prompt_of(state, config)
    candidate_line = next(line for line in prompt.splitlines() if line.startswith("- id=twitch:hot1"))
    for text in ("steam_players=5000 (pic)", "steam_abonnes=120000", "twitch_fr_viewers=1000", "hypes_igdb=20"):
        assert text in candidate_line
    assert "Au plus 1 VOD par jeu : varie les jeux." in prompt
    state, config = _hot_state(tmp_path / "b", max_vods_per_game=2)
    assert "Au plus 2 VOD par jeu : varie les jeux." in _prompt_of(state, config)


def test_prompt_candidate_steam_players_says_snapshot_when_the_peak_is_unknown(tmp_path):
    state, config = _hot_state(tmp_path)
    for game in state["games"]:
        game["community"] = {**game["community"], "steam_players": 1200, "steam_kind": "now"}
    candidate_line = next(line for line in _prompt_of(state, config).splitlines() if line.startswith("- id=twitch:hot1"))
    assert "steam_players=1200 (instantané)" in candidate_line


# --- R20 : au plus max_vods_per_game VOD par jeu ----------------------------


def _three_vod_state():
    state = _day_state("twitch:AAA", "twitch:BBB", "twitch:CCC")
    state["candidates"][2].update(game_key="jeu beta", game_name="Jeu Beta")
    return state


def test_two_picks_of_the_same_game_are_refused_with_one_per_game_naming_the_game(tmp_path):
    config = _make_config(tmp_path, max_vods_per_day=3, max_vods_per_game=1)
    fake = FakeBackend([_picks("twitch:AAA", "twitch:BBB")])
    with llm.use_backend(fake):
        state = veille.decide(_three_vod_state(), config)
    assert state["llm"]["status"] == "error" and "Jeu Alpha" in state["llm"]["error"]
    assert state["proposals"] == []
    reference = FakeBackend([_picks("twitch:ZZZ")])  # refus classique (id inconnu) : même nombre d'appels
    with llm.use_backend(reference):
        veille.decide(_three_vod_state(), config)
    assert len(fake.calls) == len(reference.calls)


def test_picks_under_the_game_cap_are_accepted(tmp_path):
    config = _make_config(tmp_path, max_vods_per_day=3, max_vods_per_game=1)
    with llm.use_backend(FakeBackend([_picks("twitch:AAA", "twitch:CCC")])):
        state = veille.decide(_three_vod_state(), config)
    assert state["llm"]["status"] == "ok" and len(state["proposals"]) == 2
    config = _make_config(tmp_path, max_vods_per_day=3, max_vods_per_game=2)
    with llm.use_backend(FakeBackend([_picks("twitch:AAA", "twitch:BBB")])):
        state = veille.decide(_three_vod_state(), config)
    assert state["llm"]["status"] == "ok" and len(state["proposals"]) == 2


def test_picks_without_a_game_key_share_no_game_cap(tmp_path):
    state = _three_vod_state()
    for candidate in state["candidates"]:
        candidate.update(game_key=None, game_name=None)
    with llm.use_backend(FakeBackend([_picks("twitch:AAA", "twitch:BBB", "twitch:CCC")])):
        out = veille.decide(state, _make_config(tmp_path, max_vods_per_day=3, max_vods_per_game=1))
    assert out["llm"]["status"] == "ok" and len(out["proposals"]) == 3


# ==========================================================================
# TASK-9dac : bilan des VOD choisies donné à Claude (SPEC-00db R8)
# ==========================================================================

_BILAN_HEADER = "Bilan des VOD choisies récemment (vues à maturité, rang 0-1 dans le compte)"
_BILAN_NONE = "Bilan des VOD choisies récemment : aucun (pas encore de résultats)"


def _bilan_entry(**over):
    return {"picked_on": "2026-10-05", "candidate_id": "twitch:OLD1", "source": "twitch", "game_name": "Jeu Beta",
            "channel_name": "streamer_b", "title": "Ancienne VOD", "video_id": "OLD1", "clips_produced": 3,
            "processing": False, "clips_published": 2, "clips_mature": 2, "views_percentile_mean": 0.75, "views_at_maturity_max": 4200, "missing": None, **over}


def _write_bilan(tmp_path, entries):
    path = tmp_path / "state" / "veille" / "bilan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"computed_at": NOW.isoformat(), "days": 30, "entries": entries}), encoding="utf-8")


def _prompt_with_bilan(tmp_path):
    fake = FakeBackend([_picks()])
    with llm.use_backend(fake):
        veille.decide(_day_state("twitch:AAA"), _make_config(tmp_path))
    return fake.calls[0].prompt


def test_prompt_carries_one_line_per_bilan_entry_before_the_candidates(tmp_path):
    _write_bilan(tmp_path, [
        _bilan_entry(),
        _bilan_entry(candidate_id="twitch:NEW2", video_id="NEW2", title="Récente VOD", game_name="Jeu Gamma",
                     channel_name="streamer_c", clips_published=1, clips_mature=0, views_percentile_mean=None,
                     views_at_maturity_max=None, missing="immature")])
    prompt = _prompt_with_bilan(tmp_path)
    head, tail = prompt.split("Candidats (VOD) :")
    assert _BILAN_HEADER in head and _BILAN_NONE not in prompt
    block = head.split(_BILAN_HEADER, 1)[1].strip().splitlines()
    lines = [line for line in block if line.startswith("- ")]
    assert len(lines) == 2
    for text in ("Ancienne VOD", "Jeu Beta", "streamer_b", "0.75", "4200"):
        assert text in lines[0]
    for text in ("Récente VOD", "Jeu Gamma", "streamer_c", "immature"):
        assert text in lines[1]
    assert "4200" not in lines[1] and "None" not in lines[1]


def _bilan_lines_of(tmp_path, *entries):
    _write_bilan(tmp_path, list(entries))
    head = _prompt_with_bilan(tmp_path).split("Candidats (VOD) :")[0]
    return [line for line in head.split(_BILAN_HEADER, 1)[1].splitlines() if line.startswith("- ")]


def test_bilan_line_separates_produced_published_and_matured_clips(tmp_path):
    (line,) = _bilan_lines_of(tmp_path, _bilan_entry(clips_produced=23, clips_published=4, clips_mature=0,
                                                     views_percentile_mean=None, views_at_maturity_max=None,
                                                     missing="immature"))
    assert "clips_produits=23" in line and "clips_publiés=4" in line and "immature" in line
    assert "aucun" not in line and "None" not in line


def test_bilan_line_never_says_no_clip_for_a_vod_that_produced_some(tmp_path):
    (line,) = _bilan_lines_of(tmp_path, _bilan_entry(clips_produced=23, clips_published=0, clips_mature=0,
                                                     views_percentile_mean=None, views_at_maturity_max=None,
                                                     missing="not_published"))
    assert "clips_produits=23" in line and "clips_publiés=0" in line
    assert "no_clips" not in line and "aucun clip" not in line


def test_bilan_line_says_processing_for_a_vod_still_in_the_worker(tmp_path):
    (line,) = _bilan_lines_of(tmp_path, _bilan_entry(clips_produced=0, clips_published=0, clips_mature=0, processing=True,
                                                     views_percentile_mean=None, views_at_maturity_max=None,
                                                     missing="processing"))
    assert "en traitement" in line and "clips_produits" not in line and "clips_publiés" not in line


def test_bilan_line_gives_a_real_zero_for_a_vod_without_clip(tmp_path):
    (line,) = _bilan_lines_of(tmp_path, _bilan_entry(clips_produced=0, clips_published=0, clips_mature=0,
                                                     views_percentile_mean=None, views_at_maturity_max=None,
                                                     missing="no_clips"))
    assert "clips_produits=0" in line and "en traitement" not in line


def test_bilan_line_says_missing_for_an_entry_without_the_produced_count(tmp_path):
    entry = _bilan_entry(views_percentile_mean=None, views_at_maturity_max=None, missing="immature")
    del entry["clips_produced"]
    (line,) = _bilan_lines_of(tmp_path, entry)
    assert "clips_produits=inconnu" in line and "clips_produits=0" not in line


def test_prompt_says_no_bilan_without_file(tmp_path):
    prompt = _prompt_with_bilan(tmp_path)
    assert _BILAN_NONE in prompt and _BILAN_HEADER not in prompt
    assert prompt.index(_BILAN_NONE) < prompt.index("Candidats (VOD) :")


def test_prompt_says_no_bilan_when_the_file_has_no_entry(tmp_path):
    _write_bilan(tmp_path, [])
    assert _BILAN_NONE in _prompt_with_bilan(tmp_path)


def test_unreadable_bilan_is_a_veille_error_naming_the_file(tmp_path):
    path = tmp_path / "state" / "veille" / "bilan.json"
    path.parent.mkdir(parents=True)
    path.write_text("{pas du json", encoding="utf-8")
    fake = FakeBackend([_picks()])
    with llm.use_backend(fake), pytest.raises(veille.VeilleError, match="bilan.json"):
        veille.decide(_day_state("twitch:AAA"), _make_config(tmp_path))
    assert fake.calls == []


def test_bilan_without_entries_list_is_a_veille_error_naming_the_file(tmp_path):
    path = tmp_path / "state" / "veille" / "bilan.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"computed_at": "x"}), encoding="utf-8")
    with llm.use_backend(FakeBackend([_picks()])), pytest.raises(veille.VeilleError, match="bilan.json"):
        veille.decide(_day_state("twitch:AAA"), _make_config(tmp_path))


def test_bilan_block_size_is_bounded(tmp_path):
    _write_bilan(tmp_path, [_bilan_entry(title="T" * 5000, game_name="G" * 5000, channel_name="C" * 5000)
                            for _ in range(20)])
    prompt = _prompt_with_bilan(tmp_path)
    block = prompt.split(_BILAN_HEADER, 1)[1].split("Candidats (VOD) :")[0]
    assert len(block) < 20 * 400


# ==========================================================================
# TASK-97b6 : tendance sur 30 jours (SPEC-85a0 R22-R25, R27 prompt)
# ==========================================================================

SINCE = "2026-09-07"  # J-29 pour TODAY = 2026-10-06 et trend_days = 30


def _pt(date, value):
    return {"date": date, "value": value, "up": 0, "down": 0}


def _vpt(date, vods, views):
    return {"date": date, "vods": vods, "views": views}


def _write_full_history(tmp_path, date, *, twitch=None, steam=None, steam_now=None, followers=None):
    path = _sdir(tmp_path) / "history" / f"{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "date": date, "twitch": {"jeu alpha": {"name": "Jeu Alpha", "viewers_fr": twitch}} if twitch is not None else {},
        "steam": {"42": {"name": "Jeu Alpha", "players": steam}} if steam is not None else {},
        "steam_now": {"42": steam_now} if steam_now is not None else {},
        "steam_followers": {"42": followers} if followers is not None else {},
        "steam_fr": {}, "youtube": {}}), encoding="utf-8")


def _trend_collectors(*, reviews=None, vods=None, reviews_error=None, vods_error=None, twitch_error=None,
                      reviews_extra=None, vods_extra=None):
    """Scénario R23 : Alpha (steam 42) et Beta (hors Steam) suivis ; Delta sans candidate ; Gamma sans communauté."""
    collectors = _community_collectors(
        twitch=(("Jeu Alpha", 1000), ("Jeu Delta", 900), ("Jeu Beta", 500), ("Jeu Gamma", 10)),
        followers={"42": 20000}, players={})
    collectors["twitch"] = Collector({
        "games": [{"name": n, "viewers_fr": v, "igdb_id": "", "twitch_id": i}
                  for n, v, i in (("Jeu Alpha", 1000, "1"), ("Jeu Delta", 900, "4"), ("Jeu Beta", 500, "2"),
                                  ("Jeu Gamma", 10, "3"))],
        "vods": [_vod("a1", game="Jeu Alpha"), _vod("b1", game="Jeu Beta"), _vod("g1", game="Jeu Gamma")]},
        error=twitch_error)
    collectors["steam_reviews"] = Lookup("histograms", reviews if reviews is not None else {"42": []}, reviews_error,
                                         **(reviews_extra or {}))
    collectors["twitch_vods_30d"] = Lookup("vods", vods if vods is not None else {
        "1": {"since": SINCE, "points": []}, "2": {"since": SINCE, "points": []}}, vods_error, **(vods_extra or {}))
    return collectors


def _trend_state(tmp_path, collectors=None, **table):
    collectors = collectors or _trend_collectors()
    state = veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path, **table))
    return state, collectors


def _trend(state, key="jeu alpha"):
    return _game_of(state, key)["trend_30d"]


def test_twitch_id_is_set_on_games_and_written_in_the_history_file(tmp_path):
    state, _ = _trend_state(tmp_path)
    assert {g["key"]: g["twitch_id"] for g in state["games"]} == {
        "jeu alpha": "1", "jeu delta": "4", "jeu beta": "2", "jeu gamma": "3"}
    history = _read(_sdir(tmp_path) / "history" / f"{TODAY}.json")
    assert history["twitch"]["jeu alpha"]["twitch_id"] == "1"


def test_followed_games_are_community_ok_with_a_candidate_in_games_order(tmp_path):
    state, collectors = _trend_state(tmp_path)
    assert collectors["steam_reviews"].calls == [["42"]]  # Beta n'a pas d'appid ; Delta et Gamma ne sont pas suivis
    assert collectors["twitch_vods_30d"].calls == [["1", "2"]]
    assert _trend(state, "jeu alpha") is not None and _trend(state, "jeu beta") is not None
    assert _trend(state, "jeu delta") is None  # communauté ok mais aucune candidate
    assert _trend(state, "jeu gamma") is None  # candidate mais communauté insuffisante


def test_followed_games_are_fixed_before_the_access_check(tmp_path):
    def reject_all(url, timeout_s):
        raise veille_sources.AccessRestricted("réservée aux abonnés")

    collectors = _trend_collectors()
    state = veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path), access_check=reject_all)
    assert state["candidates"] == [] and collectors["steam_reviews"].calls == [["42"]]
    assert _trend(state, "jeu alpha") is not None  # un jeu dont toutes les VOD sont écartées reste suivi


def test_trend_games_max_cuts_in_games_order_and_counts_skipped_on_both_sources(tmp_path):
    state, collectors = _trend_state(tmp_path, trend_games_max=1)
    assert collectors["steam_reviews"].calls == [["42"]] and collectors["twitch_vods_30d"].calls == [["1"]]
    assert _trend(state, "jeu alpha") is not None and _trend(state, "jeu beta") is None
    assert state["sources"]["steam_reviews"]["counts"] == {
        "requested": 1, "found": 1, "unknown": 0, "skipped": 1, "rate_limited": 0}
    assert state["sources"]["twitch_vods_30d"]["counts"] == {
        "requested": 1, "found": 1, "unknown": 0, "skipped": 1, "rate_limited": 0}


def test_trend_games_max_zero_skips_both_sources_without_a_call(tmp_path):
    state, collectors = _trend_state(tmp_path, trend_games_max=0)
    assert collectors["steam_reviews"].calls == [] and collectors["twitch_vods_30d"].calls == []
    for source in ("steam_reviews", "twitch_vods_30d"):
        assert state["sources"][source]["status"] == "skipped"
        assert state["sources"][source]["counts"]["skipped"] == 2 and state["sources"][source]["counts"]["requested"] == 0
    assert all(g["trend_30d"] is None for g in state["games"])


def test_source_counts_requested_found_unknown_and_rate_limited(tmp_path):
    collectors = _trend_collectors(vods={"1": {"since": SINCE, "points": []}, "2": None},
                                   vods_extra={"rate_limited": 1})
    state, _ = _trend_state(tmp_path, collectors)
    reviews, vods = state["sources"]["steam_reviews"], state["sources"]["twitch_vods_30d"]
    assert reviews["status"] == "ok" and reviews["counts"] == {
        "requested": 1, "found": 1, "unknown": 0, "skipped": 0, "rate_limited": 0}
    assert vods["status"] == "partial" and "429" in vods["error"]
    assert vods["counts"] == {"requested": 2, "found": 1, "unknown": 1, "skipped": 0, "rate_limited": 1}
    assert _trend(state, "jeu beta")["series"]["twitch_vods_fr"]["status"] == "unavailable"
    assert _trend(state, "jeu beta")["series"]["twitch_vods_fr"]["reason"] == "HTTP 429 Helix"


def test_six_named_series_and_trend_envelope(tmp_path):
    state, _ = _trend_state(tmp_path)
    trend = _trend(state)
    assert trend["days"] == 30 and trend["since"] == SINCE
    assert list(trend["series"]) == ["steam_reviews", "twitch_vods_fr", "twitch_viewers_fr", "steam_players_peak",
                                     "steam_players_now", "steam_followers"]
    for serie in trend["series"].values():
        assert set(serie) == {"status", "reason", "since", "points", "summary"}


def test_own_series_read_history_files_with_holes_and_never_fill_them(tmp_path):
    _write_full_history(tmp_path, "2026-10-01", twitch=600, steam=3000, steam_now=900, followers=18000)
    _write_full_history(tmp_path, "2026-10-05", twitch=800, steam=4000, steam_now=1000, followers=19000)
    state, _ = _trend_state(tmp_path)
    series = _trend(state)["series"]
    assert series["twitch_viewers_fr"]["points"] == [
        {"date": "2026-10-01", "value": 600}, {"date": "2026-10-05", "value": 800}, {"date": "2026-10-06", "value": 1000}]
    assert [p["value"] for p in series["steam_players_peak"]["points"]] == [3000, 4000, 5000]
    assert [p["value"] for p in series["steam_players_now"]["points"]] == [900, 1000, 1200]
    assert [p["value"] for p in series["steam_followers"]["points"]] == [18000, 19000, 20000]
    assert series["steam_followers"]["status"] == "ok" and series["steam_followers"]["since"] == SINCE


def test_a_day_absent_from_every_source_is_absent_from_the_points_and_no_empty_week_is_zero(tmp_path):
    _write_full_history(tmp_path, "2026-10-05", twitch=800, steam=4000, steam_now=1000, followers=19000)
    state, _ = _trend_state(tmp_path)
    for serie in _trend(state)["series"].values():
        dates = [p["date"] for p in serie["points"]]
        assert "2026-10-02" not in dates and "2026-09-20" not in dates
        assert 0 not in serie["summary"]["weeks"]
    peak = _trend(state)["series"]["steam_players_peak"]["summary"]
    assert peak["weeks"] == [None, None, None, 4500]  # s1 = J-6..J0 : 4000 et 5000
    assert peak["measured_days"] == 2


def _alpha_reviews_summary(tmp_path, points):
    collectors = _trend_collectors(reviews={"42": points})
    state, _ = _trend_state(tmp_path, collectors)
    return _trend(state)["series"]["steam_reviews"]


def test_summary_weeks_peak_last_and_percentages_are_exact(tmp_path):
    points = [_pt("2026-09-09", 10), _pt("2026-09-10", 20), _pt("2026-09-16", 5), _pt("2026-09-23", 40),
              _pt("2026-09-24", 41), _pt("2026-09-25", 41), _pt("2026-09-30", 100), _pt("2026-10-05", 50)]
    serie = _alpha_reviews_summary(tmp_path, points)
    assert serie["points"] == points and serie["status"] == "ok" and serie["since"] == SINCE
    assert serie["summary"] == {
        "weeks": [15, 5, 41, 75], "peak": {"date": "2026-09-30", "value": 100},
        "last": {"date": "2026-10-05", "value": 50}, "last_vs_peak_pct": -50, "s1_vs_s2_pct": 83,
        "measured_days": 8, "window_days": 30}


@pytest.mark.parametrize("day, week", [("2026-09-09", 0), ("2026-09-15", 0), ("2026-09-16", 1), ("2026-09-22", 1),
                                       ("2026-09-23", 2), ("2026-09-29", 2), ("2026-09-30", 3), ("2026-10-06", 3)])
def test_summary_week_boundaries_are_j27_j21_j20_j14_j13_j7_j6_j0(tmp_path, day, week):
    serie = _alpha_reviews_summary(tmp_path, [_pt(day, 8)])
    assert serie["summary"]["weeks"] == [8 if i == week else None for i in range(4)]


def test_summary_days_before_j27_count_for_peak_but_not_for_weeks_and_days_outside_the_window_are_dropped(tmp_path):
    serie = _alpha_reviews_summary(tmp_path, [_pt("2026-09-01", 999), _pt("2026-09-08", 70), _pt("2026-10-06", 7)])
    assert [p["date"] for p in serie["points"]] == ["2026-09-08", "2026-10-06"]  # 09-01 est avant la fenêtre
    assert serie["summary"]["weeks"] == [None, None, None, 7]
    assert serie["summary"]["peak"] == {"date": "2026-09-08", "value": 70}


def test_summary_s1_vs_s2_is_null_when_s2_is_missing_or_zero_and_peak_zero_gives_null(tmp_path):
    assert _alpha_reviews_summary(tmp_path, [_pt("2026-10-05", 9)])["summary"]["s1_vs_s2_pct"] is None
    zero_s2 = _alpha_reviews_summary(tmp_path, [_pt("2026-09-25", 0), _pt("2026-10-05", 9)])["summary"]
    assert zero_s2["weeks"] == [None, None, 0, 9] and zero_s2["s1_vs_s2_pct"] is None
    flat = _alpha_reviews_summary(tmp_path, [_pt("2026-10-04", 0), _pt("2026-10-05", 0)])["summary"]
    assert flat["peak"]["value"] == 0 and flat["last_vs_peak_pct"] is None


def test_summary_without_any_point_is_an_ok_series_with_null_figures(tmp_path):
    serie = _alpha_reviews_summary(tmp_path, [])
    assert serie["status"] == "ok" and serie["points"] == []
    assert serie["summary"] == {"weeks": [None] * 4, "peak": None, "last": None, "last_vs_peak_pct": None,
                                "s1_vs_s2_pct": None, "measured_days": 0, "window_days": 30}


def test_twitch_vods_series_summary_is_on_vods_and_views_stay_in_the_points(tmp_path):
    points = [_vpt("2026-09-30", 1, 100), _vpt("2026-10-01", 3, 120), _vpt("2026-10-02", 0, 0)]
    collectors = _trend_collectors(vods={"1": {"since": SINCE, "points": points}, "2": None})
    state, _ = _trend_state(tmp_path, collectors)
    serie = _trend(state)["series"]["twitch_vods_fr"]
    assert serie["points"] == points
    assert serie["summary"]["weeks"] == [None, None, None, 1]  # moyenne de 1, 3 et 0 = 1,33 -> 1
    assert serie["summary"]["peak"] == {"date": "2026-10-01", "value": 3}
    assert serie["summary"]["last"] == {"date": "2026-10-02", "value": 0}


def test_twitch_capped_series_since_is_kept_and_marks_the_series_partial(tmp_path):
    points = [_vpt("2026-10-04", 2, 10)]
    collectors = _trend_collectors(vods={"1": {"since": "2026-10-04", "points": points}, "2": None})
    state, _ = _trend_state(tmp_path, collectors)
    serie = _trend(state)["series"]["twitch_vods_fr"]
    assert serie["since"] == "2026-10-04" and serie["status"] == "partial" and "500" in serie["reason"]


def test_unavailable_series_say_why(tmp_path):
    state, _ = _trend_state(tmp_path)
    beta = _trend(state, "jeu beta")["series"]  # ni appid Steam
    for name in ("steam_reviews", "steam_players_peak", "steam_players_now", "steam_followers"):
        assert beta[name]["status"] == "unavailable" and beta[name]["reason"] == "hors Steam"
        assert beta[name]["points"] == [] and beta[name]["summary"] is None and beta[name]["since"] is None
    assert beta["twitch_viewers_fr"]["status"] == "ok"


def test_game_without_twitch_id_has_unavailable_twitch_series(tmp_path):
    collectors = _trend_collectors()
    collectors["twitch"].result["games"][0].pop("twitch_id")
    state, _ = _trend_state(tmp_path, collectors)
    alpha = _trend(state)["series"]
    assert alpha["twitch_vods_fr"]["reason"] == "hors Twitch FR" and alpha["twitch_vods_fr"]["status"] == "unavailable"
    assert collectors["twitch_vods_30d"].calls == [["2"]]


def test_source_error_makes_its_series_unavailable_with_the_message_and_others_continue(tmp_path):
    error = veille_sources.SourceError("histogramme des avis Steam : format inattendu (endpoint non documenté) : x")
    state, collectors = _trend_state(tmp_path, _trend_collectors(reviews_error=error))
    assert state["sources"]["steam_reviews"]["status"] == "error"
    serie = _trend(state)["series"]["steam_reviews"]
    assert serie["status"] == "unavailable" and "format inattendu (endpoint non documenté)" in serie["reason"]
    assert _trend(state)["series"]["twitch_vods_fr"]["status"] == "ok"
    assert state["sources"]["twitch_vods_30d"]["status"] == "ok"


def test_twitch_vods_not_called_when_the_twitch_source_is_in_error(tmp_path):
    collectors = _trend_collectors(twitch_error=veille_sources.SourceError("HTTP 500"))
    collectors["youtube"] = Collector({"videos": [{**_vod("y1", game=None), "title": "Soirée Jeu Alpha"}]})
    collectors["steam"] = Collector({"games": [_steam_row("42", "Jeu Alpha", 5000, 1200, rank=1, last_week_rank=0)]})
    state, _ = _trend_state(tmp_path, collectors)
    assert collectors["twitch_vods_30d"].calls == []
    assert collectors["steam_reviews"].calls == [["42"]]
    assert _trend(state)["series"]["twitch_vods_fr"]["reason"] == "source Twitch en erreur"
    assert _trend(state)["series"]["twitch_vods_fr"]["status"] == "unavailable"


def test_trend_series_are_written_in_the_day_file_and_no_game_rule_exists(tmp_path):
    _trend_state(tmp_path)
    day = _day(tmp_path)
    assert day["games"][0]["trend_30d"]["series"]["steam_reviews"]["status"] == "ok"
    assert "steam_reviews" in day["sources"] and "twitch_vods_30d" in day["sources"]


# --- R27 : lignes tendance_30j du prompt -----------------------------------------------------


def _serie(*, weeks=(15, 5, 41, 75), peak=("2026-09-30", 100), last=("2026-10-05", 50), lvp=-50, s1s2=83, measured=8,
           since=SINCE, status="ok", reason=None):
    return {"status": status, "reason": reason, "since": since, "points": [], "summary": {
        "weeks": list(weeks), "peak": {"date": peak[0], "value": peak[1]} if peak else None,
        "last": {"date": last[0], "value": last[1]} if last else None, "last_vs_peak_pct": lvp, "s1_vs_s2_pct": s1s2,
        "measured_days": measured, "window_days": 30}}


def _gone(reason):
    return {"status": "unavailable", "reason": reason, "since": None, "points": [], "summary": None}


def _trend_prompt(tmp_path, series, *, trend=True):
    state = _day_state("twitch:AAA")
    state["games"][0]["trend_30d"] = {"days": 30, "since": SINCE, "series": series} if trend else None
    state["games"].append({"key": "jeu beta", "name": "Jeu Beta", "twitch_fr_viewers": 5, "twitch_delta_pct": None,
                           "steam_delta_pct": None, "trend_30d": None})
    return _decide_prompt(tmp_path, state)


def _lines(prompt):
    return [line for line in prompt.splitlines() if line.startswith("  tendance_30j ")]


def test_prompt_trend_lines_exact_form_one_per_series_in_order_under_the_followed_game(tmp_path):
    series = {
        "steam_reviews": _serie(),
        "twitch_vods_fr": _serie(weeks=(1, 2, 3, 4), peak=("2026-10-04", 3), last=("2026-10-06", 2), lvp=-33, s1s2=33,
                                 measured=30),
        "twitch_viewers_fr": _serie(weeks=(None, 5, 41, 75), peak=None, last=None, lvp=None, s1s2=None, measured=0),
        "steam_players_peak": _serie(since="2026-10-04", measured=3),
        "steam_players_now": _gone("hors Steam"),
        "steam_followers": _gone("HTTP 429 Steam"),
    }
    series["twitch_vods_fr"]["points"] = [_vpt("2026-10-04", 3, 120), _vpt("2026-10-06", 2, 77)]
    prompt = _trend_prompt(tmp_path, series)
    assert _lines(prompt) == [
        "  tendance_30j avis_steam_par_jour : semaines=[15, 5, 41, 75] pic=2026-09-30 (100) dernier=2026-10-05 (50) "
        "dernier_vs_pic=-50% s1_vs_s2=83% jours_mesurés=8/30",
        "  tendance_30j vod_twitch_fr_par_jour : semaines=[1, 2, 3, 4] pic=2026-10-04 (3 VOD (120 vues)) "
        "dernier=2026-10-06 (2 VOD (77 vues)) dernier_vs_pic=-33% s1_vs_s2=33% jours_mesurés=30/30",
        "  tendance_30j viewers_twitch_fr : semaines=[?, 5, 41, 75] pic=inconnu dernier=inconnu "
        "dernier_vs_pic=inconnu s1_vs_s2=inconnu jours_mesurés=0/30",
        "  tendance_30j joueurs_steam_pic : semaines=[15, 5, 41, 75] pic=2026-09-30 (100) dernier=2026-10-05 (50) "
        "dernier_vs_pic=-50% s1_vs_s2=83% jours_mesurés=3/30 depuis=2026-10-04 (plafond Twitch 500 VOD)",
        "  tendance_30j joueurs_steam_instantane : indisponible (hors Steam)",
        "  tendance_30j abonnes_steam : indisponible (HTTP 429 Steam)",
    ]
    lines = prompt.splitlines()
    alpha = next(i for i, line in enumerate(lines) if line.startswith("- Jeu Alpha :"))
    assert lines[alpha + 1: alpha + 7] == _lines(prompt)  # juste sous la ligne du jeu, dans l'ordre
    beta = next(i for i, line in enumerate(lines) if line.startswith("- Jeu Beta :"))
    assert not lines[beta + 1].startswith("  tendance_30j")  # jeu non suivi : aucune ligne


def test_prompt_game_without_trend_has_no_trend_line(tmp_path):
    assert _lines(_trend_prompt(tmp_path, {}, trend=False)) == []


def test_prompt_header_has_the_trend_legend_and_the_instruction(tmp_path):
    prompt = _trend_prompt(tmp_path, {name: _serie() for name in ("steam_reviews", "twitch_vods_fr", "twitch_viewers_fr", "steam_players_peak", "steam_players_now", "steam_followers")})
    assert ("tendance_30j : pour chaque jeu suivi, moyennes par jour sur 4 semaines pleines, s4 la plus ancienne → s1 "
            "les 7 derniers jours, ? = aucune mesure cette semaine ; jours_mesurés = jours avec une mesure sur la "
            "fenêtre ; un jour sans mesure est inconnu, pas zéro. avis_steam_par_jour = avis Steam écrits par jour "
            "(activité des joueurs) ; vod_twitch_fr_par_jour = VOD FR encore en ligne publiées ce jour (vues cumulées "
            "entre parenthèses).") in prompt
    assert ("Un jeu dont la tendance monte ou tient (s1 ≥ s2, dernier proche du pic) vaut mieux qu'un pic de sortie "
            "déjà retombé (dernier bien sous le pic, s1 < s2) ; une sortie récente sans courbe en montée n'est pas "
            "« ce qui monte ».") in prompt
    assert prompt.index("tendance_30j : pour chaque") < prompt.index("- Jeu Alpha :")


# ==========================================================================
# TASK-1d82 : voies parallèles par hôte, phases, échéance globale (SPEC-85a0 R29, R30)
# ==========================================================================

DEADLINE_S = 60  # plus petit veille_deadline_s admis


class Clock:
    """Horloge injectée : les collecteurs factices l'avancent, aucune attente réelle."""

    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Probe:
    """Enveloppe un collecteur factice : journalise début/fin (liste partagée, ajout atomique), peut se donner
    rendez-vous (``threading.Barrier``) avec les autres voies de sa phase, et rend ce que rend ``inner``."""

    def __init__(self, name, inner, log, *, barrier=None, pause=0.0):
        self.name, self.inner, self.log, self.barrier, self.pause = name, inner, log, barrier, pause

    def __call__(self, settings, *args, **_ignored):
        self.log.append(f"{self.name}:start")
        if self.barrier is not None:
            self.barrier.wait()  # BrokenBarrierError si l'autre voie ne tourne pas en même temps
        if self.pause:
            time.sleep(self.pause)
        result = self.inner(settings, *args)
        self.log.append(f"{self.name}:end")
        return result


def _barrier(parties):
    return threading.Barrier(parties, timeout=3)


def _probed(collectors, log, **options):
    """``options`` : nom de source -> kwargs de ``Probe`` (barrier, pause)."""
    for name, extra in options.items():
        collectors[name] = Probe(name, collectors[name], log, **extra)
    return collectors


def _logging_access(log, barrier=None):
    state = {"first": True}

    def access(url, timeout_s):
        log.append("usher:start")
        if barrier is not None and state["first"]:
            state["first"] = False
            barrier.wait()

    return access


def _full_collectors():
    """Les neuf sources, avec des jeux suivis (Alpha : appid 42, twitch_id 1) : toutes sont appelées."""
    collectors = _trend_collectors()
    collectors["igdb"] = Collector({"games": [], "skipped_rows": 0})
    collectors["steam"] = Collector({"games": [_steam_row("42", "Jeu Alpha", 5000)]})  # instantané inconnu : steam_players relève
    collectors["steam_players"] = Lookup("players", {})
    collectors["steam_followers"] = Lookup("followers", {"42": 20000})
    return collectors


def _run(tmp_path, collectors, *, access=None, clock=None, **table):
    return veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path, **table),
                          access_check=access or (lambda url, timeout_s: None), clock=clock)


def _first(log, event):
    return log.index(event)


def test_veille_deadline_s_default_and_bounds(tmp_path):
    assert veille.CONFIG_DEFAULTS["veille_deadline_s"] == 480
    veille.settings(_make_config(tmp_path, veille_deadline_s=60))
    veille.settings(_make_config(tmp_path, veille_deadline_s=3600))
    for bad in (59, 3601, True, 480.5, "480"):
        with pytest.raises(veille.VeilleError, match="veille_deadline_s"):
            veille.settings(_make_config(tmp_path, veille_deadline_s=bad))


def test_phase_one_lanes_helix_youtube_steam_api_overlap_but_each_lane_is_sequential(tmp_path):
    log: list[str] = []
    barrier = _barrier(3)  # un même rendez-vous à trois : sinon BrokenBarrierError = la source est en erreur
    collectors = _probed(_full_collectors(), log, twitch={"barrier": barrier}, youtube={"barrier": barrier},
                         steam={"barrier": barrier}, igdb={"pause": 0.02}, steam_fr={"pause": 0.02})
    state = _run(tmp_path, collectors)
    assert [state["sources"][s]["status"] for s in ("twitch", "youtube", "steam")] == ["ok", "ok", "ok"]
    assert _first(log, "twitch:end") < _first(log, "igdb:start")  # même voie helix : jamais en parallèle
    assert _first(log, "steam:end") < _first(log, "steam_fr:start")  # même voie steam_api


def test_phase_two_steam_players_and_steam_followers_overlap(tmp_path):
    log: list[str] = []
    barrier = _barrier(2)
    collectors = _probed(_full_collectors(), log, steam_players={"barrier": barrier}, steam_followers={"barrier": barrier})
    state = _run(tmp_path, collectors)
    assert state["sources"]["steam_players"]["status"] == "ok"
    assert state["sources"]["steam_followers"]["status"] == "ok"


def test_phase_three_access_test_steam_reviews_and_twitch_vods_overlap(tmp_path):
    log: list[str] = []
    barrier = _barrier(3)
    collectors = _probed(_full_collectors(), log, steam_reviews={"barrier": barrier}, twitch_vods_30d={"barrier": barrier})
    state = _run(tmp_path, collectors, access=_logging_access(log, barrier))
    assert state["sources"]["steam_reviews"]["status"] == "ok"
    assert state["sources"]["twitch_vods_30d"]["status"] == "ok"
    assert "usher:start" in log


def test_a_phase_starts_only_after_every_lane_of_the_previous_phase_has_ended(tmp_path):
    log: list[str] = []
    collectors = _probed(_full_collectors(), log, youtube={"pause": 0.15}, steam_fr={"pause": 0.05},
                         steam_followers={"pause": 0.15}, twitch={}, igdb={}, steam={}, steam_players={},
                         steam_reviews={}, twitch_vods_30d={})
    _run(tmp_path, collectors, access=_logging_access(log))
    phase1 = ("twitch:end", "igdb:end", "youtube:end", "steam:end", "steam_fr:end")
    phase2 = ("steam_players:end", "steam_followers:end")
    phase3_starts = ("usher:start", "steam_reviews:start", "twitch_vods_30d:start")
    assert max(_first(log, e) for e in phase1) < min(_first(log, "steam_players:start"), _first(log, "steam_followers:start"))
    assert max(_first(log, e) for e in phase2) < min(_first(log, e) for e in phase3_starts)


def test_an_exception_in_one_lane_is_that_source_in_error_other_lanes_and_phases_go_on_and_claude_is_called(tmp_path):
    collectors = _full_collectors()
    collectors["steam_followers"] = Lookup("followers", error=RuntimeError("boom steamcommunity"))
    collectors["twitch"] = Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": 1000, "igdb_id": "", "twitch_id": "1"}],
                                      "vods": [{**_vod("AAA"), "url": "https://youtu.be/AAA"}]})
    config = _make_config(tmp_path, enabled=True)
    fake = FakeBackend([_picks("twitch:AAA")])
    with llm.use_backend(fake):
        state = veille.run_if_due(AFTER_RUN_AT, config, collectors)
    assert state["sources"]["steam_followers"]["status"] == "error"
    assert "boom steamcommunity" in state["sources"]["steam_followers"]["error"]
    for ok in ("twitch", "youtube", "steam", "steam_fr", "steam_players", "steam_reviews", "twitch_vods_30d"):
        assert state["sources"][ok]["status"] == "ok", ok
    assert len(fake.calls) == 1 and state["llm"]["status"] == "ok"


# --- échéance ---------------------------------------------------------------


class Deadlined:
    """Collecteur par appid qui accepte ``deadline`` : note les secondes restantes vues, avance l'horloge, rend
    ``deadline_stopped``."""

    def __init__(self, key, clock, *, spend=0, stopped=0, values=None, extra=None):
        self.key, self.clock, self.spend, self.stopped = key, clock, spend, stopped
        self.values, self.extra = values or {}, extra or {}
        self.left_seen: list[float] = []
        self.calls: list[list[str]] = []

    def __call__(self, settings, appids, *, deadline):
        self.calls.append(list(appids))
        self.left_seen.append(deadline())
        self.clock.advance(self.spend)
        result = {self.key: {a: v for a, v in self.values.items() if a in appids}, "skipped": 0, **self.extra}
        if self.stopped:
            result["deadline_stopped"] = self.stopped
        return result


def test_deadline_is_started_at_plus_veille_deadline_s_on_the_injected_clock_and_collectors_receive_it(tmp_path):
    clock = Clock()
    followers = Deadlined("followers", clock, spend=25, values={"42": 20000})
    collectors = _full_collectors()
    collectors["steam_followers"] = followers
    state = _run(tmp_path, collectors, clock=clock, veille_deadline_s=DEADLINE_S)
    assert followers.left_seen == [60.0]  # horloge au départ : toute l'échéance reste
    assert state["deadline_at"] == (NOW + timedelta(seconds=DEADLINE_S)).isoformat()
    assert state["deadline_hit"] is False
    assert _day(tmp_path)["deadline_at"] == state["deadline_at"] and _day(tmp_path)["deadline_hit"] is False


def test_a_remaining_figure_follows_the_clock(tmp_path):
    clock = Clock()
    reviews = Deadlined("histograms", clock, values={"42": []})
    collectors = _full_collectors()
    collectors["steam_followers"] = Deadlined("followers", clock, spend=25, values={"42": 20000})
    collectors["steam_reviews"] = reviews
    _run(tmp_path, collectors, clock=clock, veille_deadline_s=DEADLINE_S)
    assert reviews.left_seen == [35.0]  # 60 s - les 25 s dépensés avant la phase 3


def test_collectors_that_do_not_accept_deadline_are_called_as_before(tmp_path):
    class Plain:
        calls = 0

        def __call__(self, settings, appids):
            Plain.calls += 1
            return {"followers": {"42": 20000}, "skipped": 0}

    collectors = _full_collectors()
    collectors["steam_followers"] = Plain()
    state = _run(tmp_path, collectors)
    assert Plain.calls == 1 and state["sources"]["steam_followers"]["status"] == "ok"


def test_a_collector_with_var_keywords_receives_deadline(tmp_path):
    seen = {}

    def youtube(settings, **kwargs):
        seen.update(kwargs)
        return {"videos": []}

    collectors = _full_collectors()
    collectors["youtube"] = youtube
    _run(tmp_path, collectors, veille_deadline_s=DEADLINE_S)
    assert callable(seen["deadline"]) and 0 < seen["deadline"]() <= DEADLINE_S


@pytest.mark.parametrize("source, key, unit, stopped", [
    ("steam_followers", "followers", "appid(s)", 3),
    ("steam_players", "players", "appid(s)", 2),
    ("steam_reviews", "histograms", "appid(s)", 1),
    ("twitch_vods_30d", "vods", "jeu(x)", 4)])
def test_a_collector_stopped_by_the_deadline_is_partial_with_the_exact_message_and_count(tmp_path, source, key, unit, stopped):
    clock = Clock()
    collectors = _full_collectors()
    collectors[source] = Deadlined(key, clock, stopped=stopped)
    state = _run(tmp_path, collectors, clock=clock, veille_deadline_s=DEADLINE_S)
    entry = state["sources"][source]
    assert entry["status"] == "partial"
    assert entry["error"] == f"échéance de 60 s atteinte : {stopped} {unit} non relevé(s)"
    assert entry["counts"]["deadline"] == stopped
    assert state["deadline_hit"] is True
    assert _day(tmp_path)["sources"][source]["status"] == "partial"


def test_stopped_trend_games_have_an_unavailable_series_saying_why_and_never_a_value(tmp_path):
    clock = Clock()
    collectors = _full_collectors()
    collectors["steam_reviews"] = Deadlined("histograms", clock, stopped=1)  # l'appid 42 n'a pas été relevé
    collectors["twitch_vods_30d"] = Deadlined("vods", clock, stopped=2)
    state = _run(tmp_path, collectors, clock=clock, veille_deadline_s=DEADLINE_S)
    series = _trend(state)["series"]
    assert series["steam_reviews"]["status"] == "unavailable" and series["steam_reviews"]["points"] == []
    assert "échéance" in series["steam_reviews"]["reason"]
    assert series["twitch_vods_fr"]["status"] == "unavailable" and "échéance" in series["twitch_vods_fr"]["reason"]


def test_a_source_whose_phase_never_started_is_skipped_and_not_called(tmp_path):
    clock = Clock()
    collectors = _full_collectors()
    inner = collectors["steam"]

    def slow_steam(settings, **_):
        clock.advance(100)  # l'échéance (60 s) passe pendant la phase 1
        return inner(settings)

    collectors["steam"] = slow_steam
    followers, players = collectors["steam_followers"], collectors["steam_players"]
    state = _run(tmp_path, collectors, clock=clock, veille_deadline_s=DEADLINE_S)
    for source in ("steam_players", "steam_followers", "steam_reviews", "twitch_vods_30d"):
        assert state["sources"][source]["status"] == "skipped", source
        assert state["sources"][source]["error"] == "échéance atteinte avant le début", source
    assert players.calls == [] and followers.calls == []
    assert collectors["steam_reviews"].calls == [] and collectors["twitch_vods_30d"].calls == []
    assert state["deadline_hit"] is True


def test_access_test_cut_by_the_deadline_drops_the_remaining_vods_as_access_deadline_and_never_keeps_them(tmp_path):
    clock = Clock()
    asked = []

    def access(url, timeout_s):
        asked.append(url)
        clock.advance(100)  # le premier essai dépasse l'échéance

    config_table = dict(max_vods_per_game=3, veille_deadline_s=DEADLINE_S)
    collectors = _collectors(vods=_game_vods(4))
    state = _run(tmp_path, collectors, access=access, clock=clock, **config_table)
    assert len(asked) == 1  # plus aucun essai une fois l'échéance passée
    assert [c["video_id"] for c in state["candidates"]] == ["v0"]  # v0 était testée et accessible
    assert state["excluded"]["access_deadline"] == 3
    assert state["excluded"]["access_untested"] == 0  # distinct de « non testée parce que le jeu est servi »
    assert state["sources"]["twitch"]["counts"]["deadline"] == 3
    assert state["deadline_hit"] is True
    assert state["sources"]["twitch"]["status"] == "partial"
    assert "3 VOD non testée(s)" in state["sources"]["twitch"]["error"]


def test_a_failing_attempt_after_the_deadline_makes_no_new_attempt_and_no_pause(tmp_path):
    clock = Clock()
    asked, slept = [], []

    def access(url, timeout_s):
        asked.append(url)
        clock.advance(100)
        raise OSError("connexion fermée")

    state = veille.collect(NOW, collectors=_collectors(vods=_game_vods(2)),
                           config=_make_config(tmp_path, max_vods_per_game=1, twitch_access_attempts=5,
                                               veille_deadline_s=DEADLINE_S),
                           access_check=access, access_sleep=slept.append, clock=clock)
    assert len(asked) == 1 and slept == []
    assert state["candidates"] == []
    assert state["excluded"]["access_unreachable"] == 0  # interrompue, pas injoignable
    assert state["excluded"]["access_deadline"] == 2  # la VOD interrompue et la suivante


def test_the_access_test_checks_the_deadline_before_every_vod_even_after_a_restricted_one(tmp_path):
    clock = Clock()
    asked = []

    def access(url, timeout_s):
        asked.append(url)
        clock.advance(100)
        raise veille_sources.AccessRestricted("subscriber-only")

    state = _run(tmp_path, _collectors(vods=_game_vods(3)), access=access, clock=clock, max_vods_per_game=1,
                 veille_deadline_s=DEADLINE_S)
    assert len(asked) == 1 and state["excluded"]["access_restricted"] == 1 and state["excluded"]["access_deadline"] == 2


def test_a_survey_finished_before_the_deadline_has_no_deadline_hit_and_no_partial(tmp_path):
    clock = Clock()
    collectors = _full_collectors()
    collectors["steam_followers"] = Deadlined("followers", clock, spend=5, values={"42": 20000})
    state = _run(tmp_path, collectors, clock=clock, veille_deadline_s=DEADLINE_S)
    assert state["deadline_hit"] is False
    assert all(s["status"] != "partial" for s in state["sources"].values())
    assert state["excluded"]["access_deadline"] == 0


def _incomplete_run(tmp_path, collectors=None):
    clock = Clock(AFTER_RUN_AT)
    collectors = collectors or _full_collectors()
    collectors["steam_followers"] = Deadlined("followers", clock, stopped=3, values={"42": 20000})
    collectors["twitch"] = Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": 1000, "igdb_id": "", "twitch_id": "1"}],
                                      "vods": [{**_vod("AAA"), "url": "https://youtu.be/AAA"}]})
    config = _make_config(tmp_path, enabled=True, veille_deadline_s=DEADLINE_S)
    fake = FakeBackend([_picks("twitch:AAA")])
    with llm.use_backend(fake):
        state = veille.run_if_due(AFTER_RUN_AT, config, collectors, clock=clock)
    return state, fake


def test_prompt_names_the_incomplete_sources_with_their_error_and_claude_is_still_called(tmp_path):
    state, fake = _incomplete_run(tmp_path)
    assert len(fake.calls) == 1 and state["llm"]["status"] == "ok"
    prompt = fake.calls[0].prompt
    assert ("Relevé incomplet (échéance de 60 s) : steam_followers : "
            "échéance de 60 s atteinte : 3 appid(s) non relevé(s)") in prompt
    assert state["deadline_hit"] is True and state["finished_at"]


def test_prompt_has_no_incomplete_notice_when_the_survey_is_complete(tmp_path):
    clock = Clock(AFTER_RUN_AT)
    collectors = _full_collectors()
    collectors["twitch"] = Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": 1000, "igdb_id": "", "twitch_id": "1"}],
                                      "vods": [{**_vod("AAA"), "url": "https://youtu.be/AAA"}]})
    fake = FakeBackend([_picks("twitch:AAA")])
    with llm.use_backend(fake):
        veille.run_if_due(AFTER_RUN_AT, _make_config(tmp_path, enabled=True), collectors, clock=clock)
    assert "Relevé incomplet" not in fake.calls[0].prompt


def test_prompt_lists_a_skipped_source_and_a_cut_access_test(tmp_path):
    state = _day_state("twitch:AAA")
    state.update(deadline_hit=True, deadline_at=NOW.isoformat(), sources={
        "steam_reviews": {"status": "skipped", "error": "échéance atteinte avant le début", "counts": {}},
        "twitch": {"status": "partial", "error": "échéance de 60 s atteinte : 5 VOD non testée(s)", "counts": {"deadline": 5}},
        "steam": {"status": "ok", "error": None, "counts": {}}})
    prompt = _decide_prompt(tmp_path, state, veille_deadline_s=DEADLINE_S)
    assert "Relevé incomplet (échéance de 60 s) : steam_reviews : échéance atteinte avant le début" in prompt
    assert "Relevé incomplet (échéance de 60 s) : twitch : échéance de 60 s atteinte : 5 VOD non testée(s)" in prompt
    assert "(échéance de 60 s) : steam :" not in prompt


def test_default_clock_never_trips_the_deadline_for_a_fixed_past_now(tmp_path):
    state = _run(tmp_path, _full_collectors())  # NOW est dans le passé : l'horloge par défaut part de ``now``
    assert state["deadline_hit"] is False and state["deadline_at"] == (NOW + timedelta(seconds=480)).isoformat()


# --- finished_at = vraie heure de fin du relevé (TASK-3f90) -------------------


def test_run_if_due_finished_at_is_the_real_end_of_the_survey_not_its_start(tmp_path):
    clock = Clock(AFTER_RUN_AT)
    collectors = _full_collectors()
    collectors["steam_followers"] = Deadlined("followers", clock, spend=25, values={"42": 20000})
    collectors["twitch"] = Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": 1000, "igdb_id": "", "twitch_id": "1"}],
                                      "vods": [{**_vod("AAA"), "url": "https://youtu.be/AAA"}]})
    config = _make_config(tmp_path, enabled=True, veille_deadline_s=DEADLINE_S)
    with llm.use_backend(FakeBackend([_picks("twitch:AAA")])):
        state = veille.run_if_due(AFTER_RUN_AT, config, collectors, clock=clock)
    assert state["started_at"] == AFTER_RUN_AT.isoformat()
    assert state["finished_at"] == (AFTER_RUN_AT + timedelta(seconds=25)).isoformat()
    saved = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert saved["finished_at"] == state["finished_at"] and saved["started_at"] == state["started_at"]


def test_run_if_due_without_injected_clock_uses_the_elapsed_real_time(tmp_path):
    config = _make_config(tmp_path, enabled=True)
    called = []

    def twitch(settings):
        called.append(True)
        threading.Event().wait(0.2)  # time.sleep est neutralisé par la fixture autouse
        return {"games": [], "vods": []}

    collectors = {**_run_collectors(), "twitch": twitch}
    with llm.use_backend(FakeBackend([_picks()])):
        state = veille.run_if_due(AFTER_RUN_AT, config, collectors)
    elapsed = datetime.fromisoformat(state["finished_at"]) - datetime.fromisoformat(state["started_at"])
    assert called and elapsed >= timedelta(seconds=0.1)


# --- TASK-0015 : VOD déjà en file sous id « v… », échec du choix, limite de session -----------------------


def _twitch_vod(numeric_id):
    return {**_vod(numeric_id), "url": f"https://www.twitch.tv/videos/{numeric_id}"}


def test_twitch_vod_already_queued_under_its_worker_id_is_not_proposed_again(tmp_path, config):
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "queue.json").write_text(json.dumps([
        {"id": "q", "video_id": "v2893407960", "url": "u", "channel": None, "action": "run", "status": "waiting"},
    ]), encoding="utf-8")
    veille.collect(NOW, collectors=_collectors(vods=[_twitch_vod("2893407960"), _twitch_vod("2893407961")]),
                   config=config)
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert [c["video_id"] for c in day["candidates"]] == ["2893407961"]
    assert day["excluded"]["already_known"] == 1


def test_twitch_vod_seen_under_its_worker_id_is_not_proposed_again(tmp_path, config):
    _sdir(tmp_path).mkdir(parents=True)
    (_sdir(tmp_path) / "seen.json").write_text(json.dumps({
        "queued": [{"candidate_id": "twitch:2893407960", "video_id": "v2893407960"}], "ignored": [],
    }), encoding="utf-8")
    veille.collect(NOW, collectors=_collectors(vods=[_twitch_vod("2893407960")]), config=config)
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert day["candidates"] == [] and day["excluded"]["already_known"] == 1


def test_a_decide_crash_is_written_once_and_the_collect_is_not_replayed(tmp_path, monkeypatch):
    config = _make_config(tmp_path, enabled=True)
    calls = {"decide": 0, "twitch": 0}
    collectors = _run_collectors()
    inner = collectors["twitch"]

    def twitch(settings):
        calls["twitch"] += 1
        return inner(settings)

    collectors["twitch"] = twitch

    def boom(state, config, now=None):
        calls["decide"] += 1
        raise veille.VeilleError("fichier d'état illisible")

    monkeypatch.setattr(veille, "decide", boom)
    state = veille.run_if_due(AFTER_RUN_AT, config, collectors)
    assert state["llm"]["status"] == "error" and "illisible" in state["llm"]["error"]
    assert state["proposals"] == [] and state["finished_at"]
    assert veille.run_if_due(AFTER_RUN_AT + timedelta(minutes=1), config, collectors) is None
    assert calls == {"decide": 1, "twitch": 1}
    assert _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")["llm"]["status"] == "error"


def _quota():
    return llm.TransientLLMError("limite de session atteinte (429)")


def test_a_session_limit_schedules_a_retry_of_the_choice_only(tmp_path):
    config = _make_config(tmp_path, enabled=True, llm_retry_delay_min=30)
    calls = {"twitch": 0}
    collectors = _run_collectors()
    inner = collectors["twitch"]

    def twitch(settings):
        calls["twitch"] += 1
        return inner(settings)

    collectors["twitch"] = twitch
    fake = FakeBackend([_quota(), _picks("twitch:AAA")])
    with llm.use_backend(fake):
        state = veille.run_if_due(AFTER_RUN_AT, config, collectors)
        assert state["llm"]["status"] == "retry" and state["llm"]["retry_at"]
        assert state["llm"]["retry_at"] == (AFTER_RUN_AT + timedelta(minutes=30)).isoformat()
        assert veille.run_if_due(AFTER_RUN_AT + timedelta(minutes=10), config, collectors) is None
        assert len(fake.calls) == 1
        state = veille.run_if_due(AFTER_RUN_AT + timedelta(minutes=31), config, collectors)
    assert calls["twitch"] == 1  # le relevé n'est pas refait
    assert state["llm"]["status"] == "ok"
    assert [p["candidate_id"] for p in state["proposals"]] == ["twitch:AAA"]
    assert len(state["candidates"]) == 2 and state["finished_at"]
    assert _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")["llm"]["status"] == "ok"


def test_session_limit_retries_are_bounded_then_the_failure_is_explicit(tmp_path):
    config = _make_config(tmp_path, enabled=True, llm_retry_delay_min=30, llm_retry_max=2)
    fake = FakeBackend([_quota(), _quota(), _quota(), _picks("twitch:AAA")])
    with llm.use_backend(fake):
        state = veille.run_if_due(AFTER_RUN_AT, config, _run_collectors())
        assert state["llm"]["status"] == "retry"
        state = veille.run_if_due(AFTER_RUN_AT + timedelta(minutes=31), config, _run_collectors())
        assert state["llm"]["status"] == "retry"
        state = veille.run_if_due(AFTER_RUN_AT + timedelta(minutes=62), config, _run_collectors())
        assert state["llm"]["status"] == "error" and "429" in state["llm"]["error"]
        assert state["proposals"] == []
        assert veille.run_if_due(AFTER_RUN_AT + timedelta(hours=5), config, _run_collectors()) is None
    assert len(fake.calls) == 3


@pytest.mark.parametrize("key,value", [("llm_retry_delay_min", 0), ("llm_retry_max", -1)])
def test_llm_retry_settings_are_validated(tmp_path, key, value):
    with pytest.raises(veille.VeilleError, match=key):
        veille.settings(_make_config(tmp_path, **{key: value}))


def test_veille_does_not_import_the_download_step():
    # ADR-ca9a : la veille est une bibliotheque, jamais l'import d'une etape (download) ;
    # lu dans le source, import local compris.
    import ast
    from pathlib import Path

    source = (Path(veille.__file__)).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)

    assert "clipper.download" not in imported


# --- TASK-3e7c : audit lot K (veille-I1, I2, M2, M3, M4) ---------------------


def test_twitch_vod_with_a_workspace_folder_under_its_worker_id_is_already_known(tmp_path, config):
    (tmp_path / "workspace" / "v2893407960").mkdir(parents=True)
    veille.collect(NOW, collectors=_collectors(vods=[_twitch_vod("2893407960"), _twitch_vod("2893407961")]),
                   config=config)
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert [c["video_id"] for c in day["candidates"]] == ["2893407961"]
    assert day["excluded"]["already_known"] == 1


def test_a_collect_crash_finishes_the_day_in_error_and_the_survey_is_not_replayed(tmp_path):
    config = _make_config(tmp_path, enabled=True)
    for offset in range(1, 8):  # J-7..J-1 lisibles, J-10 corrompu : _history_window échoue après les collecteurs
        day = (AFTER_RUN_AT - timedelta(days=offset)).date().isoformat()
        _write_history(tmp_path, day, viewers=900, players=4000)
    bad = _sdir(tmp_path) / "history" / "2026-09-26.json"
    bad.write_text("{corrompu", encoding="utf-8")
    collectors = _run_collectors()
    with llm.use_backend(FakeBackend([_picks()])):
        for _ in range(3):
            try:
                veille.run_if_due(AFTER_RUN_AT, config, collectors)
            except veille.VeilleError:
                pass
    assert collectors["twitch"].calls == 1
    day = _read(_sdir(tmp_path) / "days" / f"{TODAY}.json")
    assert day["finished_at"] and day["llm"]["status"] == "error" and "2026-09-26.json" in day["llm"]["error"]


def test_prompt_keeps_the_releases_of_a_partial_igdb_with_the_incomplete_note(tmp_path):
    releases = {"recent": [{"igdb_id": "7", "name": "Jeu Neuf", "days": -1, "hypes": 20, "date": "2026-10-05",
                            "platforms": ["PC"], "portage": False}],
                "upcoming": [], "excluded_low_hypes": 0, "truncated": {"recent": 0, "upcoming": 0}}
    igdb = {"status": "partial", "error": "échéance de 480 s atteinte : 1 page(s) non relevé(s)", "counts": {}}
    prompt = _decide_prompt(tmp_path, _prompt_state(releases=releases, igdb=igdb))
    assert "indisponibles" not in prompt and "- Jeu Neuf" in prompt


@pytest.mark.parametrize("key,value", [
    ("youtube_max_results", 0), ("youtube_max_results", 51), ("twitch_top_games", 0), ("twitch_top_games", 101),
    ("twitch_vods_per_game", 0), ("twitch_vods_per_game", 101), ("vod_min_duration_s", -1),
    ("youtube_min_duration_s", -1), ("vod_max_age_h", 0), ("http_timeout_s", 0), ("http_timeout_s", "20"),
    ("steam_top", 0), ("steam_sellers_top", 0), ("steam_name_lookups_max", -1), ("rise_min_pct", -1),
    ("youtube_max_results", True), ("steam_top", "100")])
def test_settings_reject_out_of_range_values_naming_the_key(tmp_path, key, value):
    with pytest.raises(veille.VeilleError, match=key):
        veille.settings(_make_config(tmp_path, **{key: value}))


def test_steam_rate_limited_appids_are_not_also_counted_unknown(tmp_path):
    collectors = _community_collectors(releases=[_rel(1, "Hytale", "2026-10-05", hypes=80, steam_appid="60")])
    collectors["steam_followers"] = Lookup("followers", {"42": 120000}, rate_limited=1)
    state = veille.collect(NOW, collectors=collectors, config=_make_config(tmp_path))
    counts = state["sources"]["steam_followers"]["counts"]
    assert counts["found"] == 1 and counts["rate_limited"] == 1
    assert counts["unknown"] == counts["requested"] - counts["found"] - counts["rate_limited"]
