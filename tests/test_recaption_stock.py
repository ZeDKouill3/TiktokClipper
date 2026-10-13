"""tools/recaption_stock.py : regenerer caption+hashtags des clips prets jamais publies dont la legende cite le
mauvais jeu, sans toucher au reste (captions.json et sidecar output/<video>/<clip>.json)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from clipper import llm
from clipper.config import Config
from clipper.llm.fake import FakeBackend

TOOL = Path(__file__).resolve().parent.parent / "tools" / "recaption_stock.py"
spec = importlib.util.spec_from_file_location("recaption_stock", TOOL)
recaption_stock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recaption_stock)

VIDEO_ID = "v2895815199"
CHAPTERS = [{"title": "AION 2", "start_time": 0, "end_time": 1000}]
BAD = "Sur Dofus Touch, Youby4 enchaine les defis."
GOOD = "Sur AION 2, Youby4 enchaine les defis."


def _clip(i, caption, hashtags):
    return {
        "id": f"{i:02d}", "moment_id": i, "part": 1, "parts_total": 1, "start": 0.0, "end": 8.9, "duration": 8.9,
        "language": "fr", "title": f"Titre {i}", "caption": caption, "hashtags": hashtags,
        "hook_text": f"accroche {i}", "screen_title": f"Ecran {i}",
    }


def _transcript():
    words = [
        {"word": f" {t}", "start": float(i), "end": i + 0.9, "probability": 0.9}
        for i, t in enumerate(["alpha", "beta", "gamma", "delta"])
    ]
    return {
        "video_id": VIDEO_ID, "language": "fr", "language_probability": 0.99, "duration": 10.0, "model": "small",
        "vocab": [], "segments": [{"id": 0, "start": 0.0, "end": 3.9, "text": " alpha beta gamma delta", "words": words}],
    }


def _moment(i):
    return {
        "id": i, "start": 0.0, "end": 8.9, "duration": 8.9, "format": "multipart", "parts": [],
        "scores": {"hook": 8}, "bonus": {"total": 0.0}, "final_score": 80.0, "justification": "ok",
        "hook_text": "accroche",
    }


def _parts_record(i):
    return {
        "id": i, "start": 0.0, "end": 8.9, "duration": 8.9, "format": "multipart", "parts_total": 1,
        "proposed_cuts": [],
        "parts": [{"part": 1, "start": 0.0, "end": 8.9, "duration": 8.9, "hook_text": "a", "suspense": None}],
    }


def sidecar(clip, **extra):
    return {
        "video_id": VIDEO_ID, "clip_id": clip["id"], "start": clip["start"], "caption": clip["caption"],
        "hashtags": clip["hashtags"], "title": clip["title"], "screen_title": clip["screen_title"],
        "ready": True, "qa": {"status": "passed"}, **extra,
    }


def make_video(tmp_path, clips, sidecars, *, chapters=CHAPTERS, channel="twitch-gaming", title="Chill & Grind"):
    ws, out = tmp_path / "workspace", tmp_path / "output"
    d = ws / VIDEO_ID
    d.mkdir(parents=True)
    (d / "transcript.json").write_text(json.dumps(_transcript()), encoding="utf-8")
    meta = {"title": title, "channel": "Youby4"}
    if chapters is not None:
        meta["chapters"] = chapters
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    n = len(clips)
    (d / "moments.json").write_text(json.dumps(
        {"video_id": VIDEO_ID, "rubric": {}, "chunked": False, "moments": [_moment(i) for i in range(n)],
         "rejected": []}), encoding="utf-8")
    (d / "parts.json").write_text(json.dumps(
        {"video_id": VIDEO_ID, "rubric": {}, "moments": [_parts_record(i) for i in range(n)], "rejected": []}),
        encoding="utf-8")
    (d / "pipeline.json").write_text(json.dumps(
        {"video_id": VIDEO_ID, "channel": channel, "status": "done", "steps": {"captions": {"status": "done"}}}),
        encoding="utf-8")
    (d / "captions.json").write_text(
        json.dumps({"video_id": VIDEO_ID, "clips": clips}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / VIDEO_ID).mkdir(parents=True)
    for sc in sidecars:
        (out / VIDEO_ID / f"{sc['clip_id']}.json").write_text(json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8")
    return ws, out


def config(tmp_path):
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                  _sections={"captions": {"parallel": 1, "title_repair_attempts": 0}})


def answer(caption, hashtags=("#aion", "#nouveau")):
    return {"title": "Titre LLM", "caption": caption, "hashtags": list(hashtags), "hook_text": "accroche llm",
            "screen_title": "Ecran LLM"}


def run_recaption(ws, out, tmp_path, responses, *, dry_run=False):
    fake = FakeBackend(responses)
    with llm.use_backend(fake):
        report = recaption_stock.recaption_video(
            VIDEO_ID, workspace=ws, output_dir=out, config=config(tmp_path), backup_root=tmp_path / "backup",
            dry_run=dry_run)
    return fake, report


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def two_clips(tmp_path, **sidecar_extra_for_bad):
    c0, c1 = _clip(0, BAD, ["#dofus", "#old"]), _clip(1, GOOD, ["#aion", "#ok"])
    ws, out = make_video(tmp_path, [c0, c1], [sidecar(c0, **sidecar_extra_for_bad), sidecar(c1)])
    return ws, out


def test_detects_only_ready_unpublished_clips_with_the_wrong_game(tmp_path):
    ws, out = two_clips(tmp_path)

    assert [c["id"] for c in recaption_stock.find_wrong_game_clips(VIDEO_ID, workspace=ws, output_dir=out)] == ["00"]


@pytest.mark.parametrize("extra", [
    {"tiktok_post": {"id": "123", "account": "a"}},
    {"youtube_post": {"id": "yt1"}},
    {"ready": False},
    {"edited_at": "2026-10-12T10:00:00+00:00"},
])
def test_published_unready_or_hand_edited_clips_are_never_touched(tmp_path, extra):
    ws, out = two_clips(tmp_path, **extra)

    assert recaption_stock.find_wrong_game_clips(VIDEO_ID, workspace=ws, output_dir=out) == []


def test_clip_without_sidecar_is_not_a_stock_clip(tmp_path):
    c0 = _clip(0, BAD, ["#dofus"])
    ws, out = make_video(tmp_path, [c0], [])

    assert recaption_stock.find_wrong_game_clips(VIDEO_ID, workspace=ws, output_dir=out) == []


def test_video_without_chapters_has_nothing_to_fix(tmp_path):
    c0 = _clip(0, BAD, ["#dofus"])
    ws, out = make_video(tmp_path, [c0], [sidecar(c0)], chapters=None)

    assert recaption_stock.find_wrong_game_clips(VIDEO_ID, workspace=ws, output_dir=out) == []


def test_patch_rewrites_only_caption_and_hashtags_in_captions_and_sidecar(tmp_path):
    ws, out = two_clips(tmp_path)
    before_caps = read_json(ws / VIDEO_ID / "captions.json")["clips"]
    before_sc = read_json(out / VIDEO_ID / "00.json")

    fake, report = run_recaption(ws, out, tmp_path, [answer("Sur AION 2, Youby4 tente un craft.")] * 2)

    assert len(fake.calls) == 2  # la video est regeneree en entier dans un dossier temporaire
    caps = read_json(ws / VIDEO_ID / "captions.json")["clips"]
    assert caps[0]["caption"] == "Sur AION 2, Youby4 tente un craft."
    assert caps[0]["hashtags"] == ["#aion", "#nouveau"]
    for key in before_caps[0]:
        if key not in ("caption", "hashtags"):
            assert caps[0][key] == before_caps[0][key], key  # titre, accroche, screen_title intacts
    assert caps[1] == before_caps[1]  # clip conforme intact
    sc = read_json(out / VIDEO_ID / "00.json")
    assert sc["caption"] == caps[0]["caption"] and sc["hashtags"] == caps[0]["hashtags"]
    assert {k: v for k, v in sc.items() if k not in ("caption", "hashtags")} == \
           {k: v for k, v in before_sc.items() if k not in ("caption", "hashtags")}
    assert report["patched"] is True
    assert [c["id"] for c in report["clips"]] == ["00"]
    assert report["clips"][0]["old"] == BAD


def test_backup_holds_the_previous_captions_and_sidecar(tmp_path):
    ws, out = two_clips(tmp_path)

    _, report = run_recaption(ws, out, tmp_path, [answer("Sur AION 2, Youby4 tente un craft.")] * 2)

    backup = Path(report["backup"])
    assert read_json(backup / "captions.json")["clips"][0]["caption"] == BAD
    assert read_json(backup / "00.json")["caption"] == BAD


def test_dry_run_writes_nothing_but_reports_old_and_new(tmp_path):
    ws, out = two_clips(tmp_path)
    caps_before = (ws / VIDEO_ID / "captions.json").read_text(encoding="utf-8")
    sc_before = (out / VIDEO_ID / "00.json").read_text(encoding="utf-8")

    _, report = run_recaption(ws, out, tmp_path, [answer("Sur AION 2, Youby4 tente un craft.")] * 2, dry_run=True)

    assert (ws / VIDEO_ID / "captions.json").read_text(encoding="utf-8") == caps_before
    assert (out / VIDEO_ID / "00.json").read_text(encoding="utf-8") == sc_before
    assert not (tmp_path / "backup").exists()
    assert report["patched"] is False
    assert report["clips"][0]["new"] == "Sur AION 2, Youby4 tente un craft."


def test_nothing_to_fix_makes_no_llm_call(tmp_path):
    c1 = _clip(1, GOOD, ["#aion"])
    ws, out = make_video(tmp_path, [c1], [sidecar(c1)])

    fake, report = run_recaption(ws, out, tmp_path, [])

    assert fake.calls == []
    assert report["clips"] == [] and report["patched"] is False


def test_llm_answer_still_on_the_wrong_game_fails_and_leaves_everything_intact(tmp_path):
    ws, out = two_clips(tmp_path)
    caps_before = (ws / VIDEO_ID / "captions.json").read_text(encoding="utf-8")

    with pytest.raises(recaption_stock.RecaptionError, match=VIDEO_ID):
        run_recaption(ws, out, tmp_path, [answer(BAD)])

    assert (ws / VIDEO_ID / "captions.json").read_text(encoding="utf-8") == caps_before
    assert read_json(out / VIDEO_ID / "00.json")["caption"] == BAD


def test_published_clip_keeps_its_caption_even_when_the_wrong_game_is_cited(tmp_path):
    ws, out = two_clips(tmp_path, tiktok_post={"id": "999"})

    fake, report = run_recaption(ws, out, tmp_path, [])

    assert fake.calls == []
    assert read_json(ws / VIDEO_ID / "captions.json")["clips"][0]["caption"] == BAD


def test_select_videos_keeps_videos_with_captions_done_and_honours_only(tmp_path):
    ws, out = two_clips(tmp_path)

    assert recaption_stock.select_videos(ws, channel="twitch-gaming") == [VIDEO_ID]
    assert recaption_stock.select_videos(ws, channel="autre") == []
    assert recaption_stock.select_videos(ws, channel=None, only=[VIDEO_ID]) == [VIDEO_ID]
    with pytest.raises(recaption_stock.RecaptionError, match="inconnue"):
        recaption_stock.select_videos(ws, channel=None, only=["vinconnue"])
