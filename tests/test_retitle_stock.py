"""tools/retitle_stock.py : retitrer le stock en « promesse » sans toucher au reste du sidecar captions."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from clipper import llm
from clipper.config import Config
from clipper.llm.fake import FakeBackend

TOOL = Path(__file__).resolve().parent.parent / "tools" / "retitle_stock.py"
spec = importlib.util.spec_from_file_location("retitle_stock", TOOL)
retitle_stock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retitle_stock)

VIDEO_ID = "v2895815199"

OLD_CLIPS = [
    {
        "id": "00", "moment_id": 0, "part": 1, "parts_total": 1, "start": 0.0, "end": 8.9, "duration": 8.9,
        "language": "fr", "title": "Ancien titre un", "caption": "Legende publiee un\nCTA",
        "hashtags": ["#aion", "#un"], "hook_text": "accroche un", "screen_title": "« Ancienne citation un »",
    },
    {
        "id": "01", "moment_id": 1, "part": 1, "parts_total": 1, "start": 0.0, "end": 8.9, "duration": 8.9,
        "language": "fr", "title": "Ancien titre deux", "caption": "Legende publiee deux",
        "hashtags": ["#aion", "#deux"], "hook_text": "accroche deux", "screen_title": "« Ancienne citation deux »",
    },
]


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


def make_video(workspace: Path, video_id=VIDEO_ID, *, channel="twitch-gaming", title="Stream AION 2 drops",
               captions_status="done", clips=None, has_captions=True):
    d = workspace / video_id
    d.mkdir(parents=True)
    (d / "transcript.json").write_text(json.dumps(_transcript()), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"title": title, "channel": "x"}), encoding="utf-8")
    (d / "moments.json").write_text(json.dumps(
        {"video_id": video_id, "rubric": {}, "chunked": False, "moments": [_moment(0), _moment(1)], "rejected": []}
    ), encoding="utf-8")
    (d / "parts.json").write_text(json.dumps(
        {"video_id": video_id, "rubric": {}, "moments": [_parts_record(0), _parts_record(1)], "rejected": []}
    ), encoding="utf-8")
    (d / "pipeline.json").write_text(json.dumps(
        {"video_id": video_id, "channel": channel, "status": "done", "steps": {"captions": {"status": captions_status}}}
    ), encoding="utf-8")
    if has_captions:
        (d / "captions.json").write_text(
            json.dumps({"video_id": video_id, "clips": clips if clips is not None else OLD_CLIPS},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def config(tmp_path, **captions):
    captions = {"screen_title_style": "promesse", "parallel": 1, **captions}
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                  _sections={"captions": captions})


def answer(screen_title):
    return {
        "title": "Titre different", "caption": "Legende DIFFERENTE du LLM", "hashtags": ["#autre", "#tags"],
        "hook_text": "accroche autre", "screen_title": screen_title,
    }


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "workspace").mkdir()
    return tmp_path / "workspace"


def run_retitle(ws, tmp_path, responses, *, dry_run=False, cfg=None, video_id=VIDEO_ID):
    fake = FakeBackend(responses)
    with llm.use_backend(fake):
        report = retitle_stock.retitle_video(
            video_id, workspace=ws, config=cfg or config(tmp_path), backup_root=tmp_path / "backup",
            dry_run=dry_run,
        )
    return fake, report


def read_clips(ws, video_id=VIDEO_ID):
    return json.loads((ws / video_id / "captions.json").read_text(encoding="utf-8"))["clips"]


def test_patch_changes_only_screen_title(ws, tmp_path):
    make_video(ws)
    _, report = run_retitle(ws, tmp_path, [answer("Le boss tombe-t-il ?"), answer("Un drop rarissime ?")])
    new = read_clips(ws)
    assert [c["screen_title"] for c in new] == ["Le boss tombe-t-il ?", "Un drop rarissime ?"]
    for old_clip, new_clip in zip(OLD_CLIPS, new):
        assert {k: v for k, v in new_clip.items() if k != "screen_title"} == \
               {k: v for k, v in old_clip.items() if k != "screen_title"}
    assert new[0]["caption"] == "Legende publiee un\nCTA"
    assert new[0]["hashtags"] == ["#aion", "#un"]
    assert [(c["id"], c["old"], c["new"]) for c in report["clips"]] == [
        ("00", "« Ancienne citation un »", "Le boss tombe-t-il ?"),
        ("01", "« Ancienne citation deux »", "Un drop rarissime ?"),
    ]


def test_backup_created_before_patch(ws, tmp_path):
    d = make_video(ws)
    before = (d / "captions.json").read_bytes()
    run_retitle(ws, tmp_path, [answer("Le boss tombe-t-il ?"), answer("Un drop rarissime ?")])
    backups = list((tmp_path / "backup").glob(f"{VIDEO_ID}-*/captions.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before


def test_dry_run_writes_nothing(ws, tmp_path):
    d = make_video(ws)
    before = (d / "captions.json").read_bytes()
    _, report = run_retitle(ws, tmp_path, [answer("Le boss tombe-t-il ?"), answer("Un drop rarissime ?")], dry_run=True)
    assert (d / "captions.json").read_bytes() == before
    assert not (tmp_path / "backup").exists()
    assert report["patched"] is False
    assert report["clips"][0]["new"] == "Le boss tombe-t-il ?"


def test_missing_clip_id_is_loud_and_video_untouched(ws, tmp_path):
    # captions.json réel a un clip « 02 » que la régénération ne produit pas
    extra = {**OLD_CLIPS[1], "id": "02", "moment_id": 2}
    d = make_video(ws, clips=[*OLD_CLIPS, extra])
    before = (d / "captions.json").read_bytes()
    with pytest.raises(retitle_stock.RetitleError, match="02"):
        run_retitle(ws, tmp_path, [answer("Le boss tombe-t-il ?"), answer("Un drop rarissime ?")])
    assert (d / "captions.json").read_bytes() == before
    assert not (tmp_path / "backup").exists()


def test_extra_regenerated_clip_id_is_loud(ws, tmp_path):
    d = make_video(ws, clips=[OLD_CLIPS[0]])
    before = (d / "captions.json").read_bytes()
    with pytest.raises(retitle_stock.RetitleError, match="01"):
        run_retitle(ws, tmp_path, [answer("Le boss tombe-t-il ?"), answer("Un drop rarissime ?")])
    assert (d / "captions.json").read_bytes() == before


def test_llm_failure_is_named_and_video_untouched(ws, tmp_path):
    d = make_video(ws)
    before = (d / "captions.json").read_bytes()
    # Titre avec emoji : refusé par la validation, plus de réponses valides
    bad = answer("Boss \U0001F525")
    with pytest.raises(retitle_stock.RetitleError, match=VIDEO_ID):
        run_retitle(ws, tmp_path, [bad] * 6)
    assert (d / "captions.json").read_bytes() == before


def _fake_run(seen):
    """captions.run simule : releve le style de la config recue, ecrit des titres neufs dans le dossier temporaire."""
    def run(video_id, workspace, *, config, force):
        seen.append(retitle_stock.captions.screen_title_style(config.section("captions")))
        path = Path(workspace) / video_id / "captions.json"
        clips = [{**c, "screen_title": f"neuf {c['id']}"} for c in OLD_CLIPS]
        path.write_text(json.dumps({"video_id": video_id, "clips": clips}), encoding="utf-8")
        return path
    return run


@pytest.mark.parametrize("style", ["citation", "promesse", "promesse_emoji"])
def test_style_option_overrides_the_preset_style_for_the_generation(ws, tmp_path, monkeypatch, style):
    make_video(ws)
    seen = []
    monkeypatch.setattr(retitle_stock.captions, "run", _fake_run(seen))

    retitle_stock.retitle_video(VIDEO_ID, workspace=ws, config=config(tmp_path, screen_title_style="citation"
                                if style != "citation" else "promesse"),
                                backup_root=tmp_path / "backup", style=style)

    assert seen == [style]


def test_default_style_is_promesse_whatever_the_preset_says(ws, tmp_path, monkeypatch):
    make_video(ws)
    seen = []
    monkeypatch.setattr(retitle_stock.captions, "run", _fake_run(seen))

    retitle_stock.retitle_video(VIDEO_ID, workspace=ws, config=config(tmp_path, screen_title_style="citation"),
                                backup_root=tmp_path / "backup")

    assert seen == ["promesse"]


def test_unknown_style_is_a_named_error_and_nothing_is_touched(ws, tmp_path, monkeypatch):
    d = make_video(ws)
    before = (d / "captions.json").read_bytes()
    monkeypatch.setattr(retitle_stock.captions, "run", _fake_run([]))

    with pytest.raises(retitle_stock.RetitleError, match="chaotique"):
        retitle_stock.retitle_video(VIDEO_ID, workspace=ws, config=config(tmp_path), backup_root=tmp_path / "backup",
                                    style="chaotique")
    assert (d / "captions.json").read_bytes() == before


def test_patch_records_the_style_in_pipeline_json_so_the_render_draws_it(ws, tmp_path, monkeypatch):
    d = make_video(ws)
    monkeypatch.setattr(retitle_stock.captions, "run", _fake_run([]))

    report = retitle_stock.retitle_video(VIDEO_ID, workspace=ws, config=config(tmp_path),
                                         backup_root=tmp_path / "backup", style="promesse_emoji")

    state = json.loads((d / "pipeline.json").read_text(encoding="utf-8"))
    assert state["title_style"] == "promesse_emoji" and state["channel"] == "twitch-gaming"
    assert report["style"] == "promesse_emoji"
    assert (Path(report["backup"]) / "pipeline.json").exists()


def test_dry_run_leaves_pipeline_json_untouched(ws, tmp_path, monkeypatch):
    d = make_video(ws)
    before = (d / "pipeline.json").read_bytes()
    monkeypatch.setattr(retitle_stock.captions, "run", _fake_run([]))

    retitle_stock.retitle_video(VIDEO_ID, workspace=ws, config=config(tmp_path), backup_root=tmp_path / "backup",
                                dry_run=True, style="citation")

    assert (d / "pipeline.json").read_bytes() == before


def test_cli_style_option_is_parsed_and_validated():
    assert retitle_stock.parse_args(["--style", "citation"]).style == "citation"
    assert retitle_stock.parse_args([]).style == "promesse"
    with pytest.raises(SystemExit):
        retitle_stock.parse_args(["--style", "chaotique"])


def test_guard_rejects_any_other_field_change():
    patched = [dict(c) for c in OLD_CLIPS]
    patched[0] = {**patched[0], "screen_title": "Autre ?", "hashtags": ["#x"]}
    with pytest.raises(retitle_stock.RetitleError, match="hashtags"):
        retitle_stock.assert_only_screen_title_changed(OLD_CLIPS, patched)
    patched[0] = {**OLD_CLIPS[0], "screen_title": "Autre ?"}
    retitle_stock.assert_only_screen_title_changed(OLD_CLIPS, patched)  # seul screen_title : accepté


def test_original_inputs_not_modified_by_generation(ws, tmp_path):
    d = make_video(ws)
    names = sorted(p.name for p in d.iterdir())
    run_retitle(ws, tmp_path, [answer("Le boss tombe-t-il ?"), answer("Un drop rarissime ?")], dry_run=True)
    assert sorted(p.name for p in d.iterdir()) == names  # pas de llm_refusals.jsonl & co laissés


def test_select_videos(ws):
    make_video(ws, "aion_ok")
    make_video(ws, "other_game", title="Valorant ranked", clips=[{**OLD_CLIPS[0], "hashtags": ["#valo"]}])
    make_video(ws, "classic_ch", channel="classic")
    make_video(ws, "no_captions", captions_status="pending", has_captions=False)
    make_video(ws, "hashtag_aion", title="Stream", clips=[{**OLD_CLIPS[0], "hashtags": ["#AION2"]}])
    assert retitle_stock.select_videos(ws, channel="twitch-gaming", game="aion") == ["aion_ok", "hashtag_aion"]
    assert retitle_stock.select_videos(ws, channel="twitch-gaming", game="aion", only=["aion_ok"]) == ["aion_ok"]
    with pytest.raises(retitle_stock.RetitleError, match="inconnu"):
        retitle_stock.select_videos(ws, channel="twitch-gaming", game="aion", only=["nope"])


def test_render_command_forces_reframe_not_captions():
    cmd = retitle_stock.render_command("abc123")
    assert "--force-step reframe" in cmd and "abc123" in cmd
    assert "captions" not in cmd
    call = retitle_stock.api_call("abc123", base_url="http://127.0.0.1:8000")
    assert call["url"] == "http://127.0.0.1:8000/api/videos/abc123/retry"
    assert call["json"] == {"from_step": "reframe"}


# --------------------------------------------------------------------------
# TASK-ee48b6241ea4 : --max-chars ne retitre que les titres plus longs que N
# --------------------------------------------------------------------------


def make_sidecars(tmp_path, *, style="citation", extra=None):
    out = tmp_path / "output" / VIDEO_ID
    out.mkdir(parents=True)
    for clip in OLD_CLIPS:
        data = {"video_id": VIDEO_ID, "clip_id": clip["id"], "screen_title": clip["screen_title"],
                "screen_title_style": style, "ready": True, **((extra or {}).get(clip["id"]) or {})}
        (out / f"{clip['id']}.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path / "output"


def run_max_chars(ws, tmp_path, responses, *, max_chars=25, dry_run=False, output_dir=None):
    fake = FakeBackend(responses)
    with llm.use_backend(fake):
        report = retitle_stock.retitle_video(
            VIDEO_ID, workspace=ws, config=config(tmp_path), backup_root=tmp_path / "backup", dry_run=dry_run,
            max_chars=max_chars, output_dir=output_dir)
    return fake, report


def test_max_chars_only_retitles_clips_with_a_longer_title(ws, tmp_path):
    make_video(ws)
    out = make_sidecars(tmp_path)  # titres de 24 et 26 caracteres

    fake, report = run_max_chars(ws, tmp_path, [answer("Titre court"), answer("Autre court")], output_dir=out)

    new = read_clips(ws)
    assert new[0]["screen_title"] == OLD_CLIPS[0]["screen_title"]  # 24 <= 25 : intact
    assert new[1]["screen_title"] == "Autre court"
    assert [c["id"] for c in report["clips"]] == ["01"]
    for old_clip, new_clip in zip(OLD_CLIPS, new):
        assert {k: v for k, v in new_clip.items() if k != "screen_title"} == \
               {k: v for k, v in old_clip.items() if k != "screen_title"}


def test_max_chars_keeps_the_clip_style_and_states_the_limit_to_the_generation(ws, tmp_path):
    make_video(ws)
    out = make_sidecars(tmp_path, style="citation")  # le preset de config() dit « promesse »

    fake, _ = run_max_chars(ws, tmp_path, [answer("Titre court"), answer("Autre court")], output_dir=out)

    prompt = fake.calls[0].prompt
    assert "phrase reellement prononcee" in prompt  # style citation du clip, pas celui du preset
    assert "25 caracteres" in prompt


def test_max_chars_uses_pipeline_title_style_first(ws, tmp_path):
    d = make_video(ws)
    state = json.loads((d / "pipeline.json").read_text(encoding="utf-8"))
    (d / "pipeline.json").write_text(json.dumps({**state, "title_style": "promesse"}), encoding="utf-8")
    out = make_sidecars(tmp_path, style="citation")

    fake, _ = run_max_chars(ws, tmp_path, [answer("Titre court"), answer("Autre court")], output_dir=out)

    assert "phrase reellement prononcee" not in fake.calls[0].prompt


def test_max_chars_never_touches_a_published_clip(ws, tmp_path):
    d = make_video(ws)
    before = (d / "captions.json").read_bytes()
    out = make_sidecars(tmp_path, extra={"01": {"tiktok_post": {"id": "123"}}})

    fake, report = run_max_chars(ws, tmp_path, [], output_dir=out)

    assert fake.calls == []
    assert report["clips"] == [] and report["patched"] is False
    assert (d / "captions.json").read_bytes() == before


def test_max_chars_generated_title_over_the_limit_fails_and_leaves_the_video_untouched(ws, tmp_path):
    d = make_video(ws)
    before = (d / "captions.json").read_bytes()
    out = make_sidecars(tmp_path)

    with pytest.raises(retitle_stock.RetitleError, match=VIDEO_ID):
        run_max_chars(ws, tmp_path, [answer("Un titre bien trop long pour le plafond")] * 4, output_dir=out)
    assert (d / "captions.json").read_bytes() == before


def test_max_chars_needs_the_output_dir_and_a_positive_limit(ws, tmp_path):
    make_video(ws)
    with pytest.raises(retitle_stock.RetitleError, match="output"):
        run_max_chars(ws, tmp_path, [], output_dir=None)
    out = make_sidecars(tmp_path)
    with pytest.raises(retitle_stock.RetitleError, match="max-chars"):
        run_max_chars(ws, tmp_path, [], max_chars=0, output_dir=out)


def test_cli_max_chars_option_is_parsed():
    assert retitle_stock.parse_args(["--max-chars", "35"]).max_chars == 35
    assert retitle_stock.parse_args([]).max_chars is None
