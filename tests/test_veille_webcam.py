"""veille.py : contrôle webcam sur les miniatures avant le choix de Claude (SPEC-fefc, complète R6 de SPEC-bdd9).

FakeBackend + miniatures fausses injectées : aucun test ne touche le réseau ni le vrai Claude.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest

from clipper import llm, veille
from clipper.config import Config
from clipper.llm.fake import FakeBackend

NOW = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
AFTER_RUN_AT = datetime(2026, 10, 6, 8, 30, tzinfo=timezone.utc)
TODAY = "2026-10-06"
THUMB_W, THUMB_H = 160, 90


def _config(tmp_path, **veille_table) -> Config:
    table = {"state_dir": str(tmp_path / "state" / "veille"), "twitch_client_id": "cid",
             "twitch_client_secret": "csecret", "youtube_api_key": "ykey", **veille_table}
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                  _sections={"worker": {"queue_path": str(tmp_path / "state" / "queue.json")}, "veille": table})


def _jpeg(color=(40, 120, 200)) -> bytes:
    image = np.full((THUMB_H, THUMB_W, 3), color, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", image)
    assert ok
    return buf.tobytes()


def _state(*ids, thumbs=True):
    candidates = [{"id": f"twitch:{i}", "source": "twitch", "video_id": i, "url": f"https://example.test/{i}",
                   "title": f"Titre {i}", "channel_name": "streamer_a", "game_key": "jeu alpha", "game_name": "Jeu Alpha",
                   "duration_s": 7200, "published_at": NOW.isoformat(), "view_count": 10, "views_per_hour": 1.0,
                   "thumbnail_url": f"https://cdn.test/{i}.jpg" if thumbs else None,
                   "signals": {}} for i in ids]
    return {"date": TODAY, "started_at": NOW.isoformat(), "finished_at": None, "sources": {}, "games": [],
            "candidates": candidates, "excluded": {}, "llm": {"status": "skipped", "error": None, "model": None},
            "proposals": [], "skipped_note": "", "refresh_requested_at": None}


class Fetch:
    """Faux téléchargeur de miniatures : url -> octets (ou exception)."""

    def __init__(self, table=None, default=None):
        self.table, self.default, self.urls = table or {}, default if default is not None else _jpeg(), []

    def __call__(self, url, timeout_s):
        self.urls.append(url)
        value = self.table.get(url, self.default)
        if isinstance(value, Exception):
            raise value
        return value


def _answer(*verdicts):
    return {"items": [{"n": n, "webcam": v, "reason": f"raison {n}"} for n, v in enumerate(verdicts, start=1)]}


def _check(tmp_path, state, answer, *, fetch=None, deadline=None, **table):
    config = _config(tmp_path, **table)
    fake = FakeBackend([answer])
    fetch = fetch or Fetch()
    with llm.use_backend(fake):
        out = veille.check_webcams(state, config, fetch=fetch, deadline=deadline)
    return out, fake, fetch


# --- réglage ----------------------------------------------------------------


def test_webcam_check_max_defaults_to_30_and_is_validated(tmp_path):
    assert veille.CONFIG_DEFAULTS["webcam_check_max"] == 30
    for bad in (-1, True, "3", 1.5):
        with pytest.raises(veille.VeilleError, match="webcam_check_max"):
            veille.settings(_config(tmp_path, webcam_check_max=bad))
    assert veille.settings(_config(tmp_path, webcam_check_max=0))["webcam_check_max"] == 0


# --- planche et appel ----------------------------------------------------------


def test_one_numbered_board_and_one_veille_webcam_call_with_a_still_image(tmp_path):
    state = _state("A", "B", "C")
    out, fake, fetch = _check(tmp_path, state, _answer("oui", "oui", "oui"))
    assert fetch.urls == ["https://cdn.test/A.jpg", "https://cdn.test/B.jpg", "https://cdn.test/C.jpg"] or \
        sorted(fetch.urls) == ["https://cdn.test/A.jpg", "https://cdn.test/B.jpg", "https://cdn.test/C.jpg"]
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.usage == "veille_webcam" and len(call.images) == 1
    board = cv2.imread(str(call.images[0]))
    assert board is not None
    # trois miniatures côte à côte ou en grille, plus une bande de numéro hors de l'image : plus haute qu'une miniature
    assert board.shape[0] > THUMB_H and board.shape[1] >= 3 * THUMB_W
    assert "webcam" in call.prompt.lower() and "doute" in call.prompt and "mini-carte" in call.prompt
    assert out["llm_webcam"]["status"] == "ok"


def test_board_is_stored_only_under_the_veille_state_dir(tmp_path):
    out, fake, _ = _check(tmp_path, _state("A", "B"), _answer("oui", "oui"))
    board = fake.calls[0].images[0]
    assert (tmp_path / "state" / "veille") in board.parents


def test_candidates_are_checked_in_presentation_order_up_to_webcam_check_max(tmp_path):
    out, fake, fetch = _check(tmp_path, _state("A", "B", "C", "D"), _answer("oui", "oui"), webcam_check_max=2)
    assert sorted(fetch.urls) == ["https://cdn.test/A.jpg", "https://cdn.test/B.jpg"]
    by_id = {c["id"]: c["webcam"] for c in out["candidates"]}
    assert by_id == {"twitch:A": "oui", "twitch:B": "oui", "twitch:C": "inconnue", "twitch:D": "inconnue"}


# --- effets -----------------------------------------------------------------------


def test_non_removes_the_candidate_and_lists_it_with_the_reason(tmp_path):
    answer = {"items": [{"n": 1, "webcam": "oui", "reason": "visage en bas à gauche"},
                        {"n": 2, "webcam": "non", "reason": "seulement une mini-carte"},
                        {"n": 3, "webcam": "doute", "reason": "incrustation floue"}]}
    out, _, _ = _check(tmp_path, _state("A", "B", "C"), answer)
    assert [c["id"] for c in out["candidates"]] == ["twitch:A", "twitch:C"]
    assert {c["id"]: c["webcam"] for c in out["candidates"]} == {"twitch:A": "oui", "twitch:C": "doute"}
    assert out["excluded"]["no_webcam"] == 1
    assert out["webcam_excluded"] == [{"candidate_id": "twitch:B", "title": "Titre B",
                                       "reason": "pas de webcam sur la miniature : seulement une mini-carte"}]
    assert out["llm_webcam"]["status"] == "ok" and out["llm_webcam"]["error"] is None


def test_a_removed_candidate_is_not_shown_to_claude_for_the_choice(tmp_path):
    state = _state("A", "B")
    out, _, _ = _check(tmp_path, state, _answer("non", "oui"))
    fake = FakeBackend([{"picks": [], "skipped_note": ""}])
    with llm.use_backend(fake):
        veille.decide(out, _config(tmp_path))
    assert "twitch:A" not in fake.calls[0].prompt and "twitch:B" in fake.calls[0].prompt
    assert "webcam=oui" in fake.calls[0].prompt


def test_missing_placeholder_or_unreadable_thumbnail_keeps_the_candidate_as_inconnue(tmp_path):
    state = _state("A", "B", "C", "D")
    state["candidates"][1]["thumbnail_url"] = None  # absente
    state["candidates"][2]["thumbnail_url"] = "https://cdn.test/_404/404_processing_320x180.jpg"  # placeholder Twitch
    fetch = Fetch({"https://cdn.test/D.jpg": b"pas une image"})  # illisible
    out, fake, fetch = _check(tmp_path, state, _answer("oui"), fetch=fetch)
    assert {c["id"]: c["webcam"] for c in out["candidates"]} == {
        "twitch:A": "oui", "twitch:B": "inconnue", "twitch:C": "inconnue", "twitch:D": "inconnue"}
    assert "https://cdn.test/_404/404_processing_320x180.jpg" not in fetch.urls  # jamais téléchargé
    assert out["llm_webcam"]["status"] == "ok"


def test_one_failed_download_only_makes_that_candidate_inconnue(tmp_path):
    fetch = Fetch({"https://cdn.test/B.jpg": TimeoutError("délai")})
    out, _, _ = _check(tmp_path, _state("A", "B"), _answer("non"), fetch=fetch)
    assert {c["id"]: c["webcam"] for c in out["candidates"]} == {"twitch:B": "inconnue"}
    assert out["excluded"]["no_webcam"] == 1 and out["llm_webcam"]["status"] == "ok"


def test_no_thumbnail_to_check_means_no_call(tmp_path):
    out, fake, fetch = _check(tmp_path, _state("A", thumbs=False), _answer("oui"))
    assert fake.calls == [] and fetch.urls == []
    assert out["candidates"][0]["webcam"] == "inconnue" and out["llm_webcam"]["status"] == "skipped"


def test_setting_zero_disables_the_check_without_download_or_call(tmp_path):
    out, fake, fetch = _check(tmp_path, _state("A", "B"), _answer("non", "non"), webcam_check_max=0)
    assert fake.calls == [] and fetch.urls == []
    assert len(out["candidates"]) == 2 and out["llm_webcam"]["status"] == "skipped"
    assert all(c["webcam"] == "inconnue" for c in out["candidates"])


# --- échecs : jamais de repli silencieux (ADR-ad2e) ------------------------------------------


def test_llm_failure_is_written_logged_and_keeps_every_candidate(tmp_path, caplog):
    config = _config(tmp_path)
    fake = FakeBackend([llm.LLMError("claude a refusé")])
    with llm.use_backend(fake), caplog.at_level(logging.ERROR, logger="clipper.veille"):
        out = veille.check_webcams(_state("A", "B"), config, fetch=Fetch())
    assert out["llm_webcam"]["status"] == "error" and "claude a refusé" in out["llm_webcam"]["error"]
    assert [c["webcam"] for c in out["candidates"]] == ["inconnue", "inconnue"]
    assert out["excluded"].get("no_webcam", 0) == 0 and out.get("webcam_excluded", []) == []
    assert any("webcam" in r.getMessage() and "claude a refusé" in r.getMessage() for r in caplog.records)


def test_answer_missing_a_number_is_refused_as_an_error(tmp_path):
    out, _, _ = _check(tmp_path, _state("A", "B"), _answer("non"))  # n=2 absent, même après réparation
    assert out["llm_webcam"]["status"] == "error"
    assert [c["id"] for c in out["candidates"]] == ["twitch:A", "twitch:B"]


@pytest.mark.parametrize("items", [
    [{"n": 1, "webcam": "oui", "reason": "x"}, {"n": 1, "webcam": "non", "reason": "x"}],  # doublon
    [{"n": 1, "webcam": "oui", "reason": "x"}, {"n": 3, "webcam": "oui", "reason": "x"}],  # numéro inexistant
    [{"n": 1, "webcam": "peut-être", "reason": "x"}, {"n": 2, "webcam": "oui", "reason": "x"}],  # hors énumération
])
def test_invalid_answers_are_errors(tmp_path, items):
    out, _, _ = _check(tmp_path, _state("A", "B"), {"items": items})
    assert out["llm_webcam"]["status"] == "error" and len(out["candidates"]) == 2


def test_all_downloads_failing_is_an_error_without_llm_call(tmp_path):
    fetch = Fetch(default=OSError("réseau coupé"))
    out, fake, _ = _check(tmp_path, _state("A", "B"), _answer("non", "non"), fetch=fetch)
    assert fake.calls == []
    assert out["llm_webcam"]["status"] == "error" and "réseau coupé" in out["llm_webcam"]["error"]
    assert [c["webcam"] for c in out["candidates"]] == ["inconnue", "inconnue"]


# --- échéance globale ---------------------------------------------------------------------


def test_deadline_already_passed_keeps_all_candidates_with_status_deadline(tmp_path):
    out, fake, fetch = _check(tmp_path, _state("A", "B"), _answer("non", "non"), deadline=lambda: 0.0)
    assert fake.calls == [] and fetch.urls == []
    assert out["llm_webcam"]["status"] == "deadline"
    assert [c["webcam"] for c in out["candidates"]] == ["inconnue", "inconnue"]


def test_deadline_reached_after_the_downloads_skips_the_call(tmp_path):
    remaining = iter([100.0, 100.0, 0.0, 0.0, 0.0])
    out, fake, _ = _check(tmp_path, _state("A", "B"), _answer("non", "non"), deadline=lambda: next(remaining, 0.0))
    assert fake.calls == [] and out["llm_webcam"]["status"] == "deadline"
    assert len(out["candidates"]) == 2


# --- chaîne complète : run_if_due ---------------------------------------------------------------


class _Collector:
    def __init__(self, result):
        self.result = result

    def __call__(self, settings):
        return self.result


def _vod(video_id):
    return {"video_id": video_id, "url": f"https://youtu.be/{video_id}", "title": f"Titre {video_id}",
            "channel_name": "streamer_a", "game_name": "Jeu Alpha", "duration_s": 7200,
            "published_at": (NOW - timedelta(hours=5)).isoformat(), "view_count": 1000, "views_per_hour": 200.0,
            "thumbnail_url": f"https://cdn.test/{video_id}.jpg"}


def _collectors():
    return {"twitch": _Collector({"games": [{"name": "Jeu Alpha", "viewers_fr": 1000}], "vods": [_vod("AAA"), _vod("BBB")]}),
            "youtube": _Collector({"videos": []}),
            "steam": _Collector({"games": [{"appid": "42", "name": "Jeu  Alpha !", "players": 5000}]}),
            "steam_fr": _Collector({"games": []})}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    from clipper import veille_sources

    monkeypatch.setattr(veille_sources, "check_twitch_access", lambda url, timeout_s: None)
    monkeypatch.setattr(veille, "_fetch_thumbnail", Fetch())


def _by_usage(request):
    if request.usage == "veille_webcam":
        return _answer("non", "oui")
    return {"picks": [{"candidate_id": "twitch:BBB", "reason": "ok"}], "skipped_note": ""}


def test_run_if_due_checks_webcams_before_the_choice_and_writes_the_day_file(tmp_path):
    config = _config(tmp_path, enabled=True, max_vods_per_game=3)
    fake = FakeBackend([_by_usage])
    with llm.use_backend(fake):
        veille.run_if_due(AFTER_RUN_AT, config, _collectors())
    assert [c.usage for c in fake.calls] == ["veille_webcam", "veille"]
    choice_prompt = fake.calls[1].prompt
    assert "twitch:AAA" not in choice_prompt and "twitch:BBB" in choice_prompt
    day = json.loads((tmp_path / "state" / "veille" / "days" / f"{TODAY}.json").read_text(encoding="utf-8"))
    assert day["llm_webcam"]["status"] == "ok" and day["excluded"]["no_webcam"] == 1
    assert [c["id"] for c in day["candidates"]] == ["twitch:BBB"]
    assert day["proposals"][0]["candidate"]["webcam"] == "oui"
    assert day["webcam_excluded"][0]["candidate_id"] == "twitch:AAA"


def test_run_if_due_webcam_failure_never_blocks_the_choice(tmp_path):
    config = _config(tmp_path, enabled=True, max_vods_per_game=3)

    def answer(request):
        if request.usage == "veille_webcam":
            raise llm.LLMError("panne webcam")
        return {"picks": [{"candidate_id": "twitch:AAA", "reason": "ok"}], "skipped_note": ""}

    with llm.use_backend(FakeBackend([answer])):
        veille.run_if_due(AFTER_RUN_AT, config, _collectors())
    day = json.loads((tmp_path / "state" / "veille" / "days" / f"{TODAY}.json").read_text(encoding="utf-8"))
    assert day["llm_webcam"]["status"] == "error" and day["llm"]["status"] == "ok"
    assert len(day["candidates"]) == 2 and day["proposals"][0]["candidate"]["webcam"] == "inconnue"
