"""Etape action (SPEC-b0f3 R4-R9, ADR-4e57) : fixtures synthetiques, FakeBackend, CPU, sans reseau.

Fixture commune (duree 300 s, window 30 / step 15, reglages par defaut) :
- fond : un debut de plan tous les 30 s (5, 35, 65...) -> 1 coupe par fenetre, mediane 1 ;
- zone A : pics audio (10 dB) a 100, 105 ... 125, debuts de plan supplementaires a 102 ... 127 ;
  zone B : meme chose decalee de 105 s (pics 205 ... 230, plans 207 ... 232) ;
- pic isole a 20 s (12 dB) et pic faible a 50 s (3 dB, sous audio_peak_min_db).
Fenetres (debut) de score >= 0,6 : 90, 105, 120 (zone A) et 195, 210, 225 (zone B), soit
les passages [90, 150] et [195, 255] de score 1,0 (calcul a la main dans chaque test).
"""
from __future__ import annotations

import ast
import json
import logging
import re
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from clipper import llm
from clipper.config import Config
from clipper.llm.fake import FakeBackend

VIDEO_ID = "abcdefghijk"
DURATION = 300.0

ZONE_A_PEAKS = [100.0, 105.0, 110.0, 115.0, 120.0, 125.0]
ZONE_B_PEAKS = [205.0, 210.0, 215.0, 220.0, 225.0, 230.0]
FRAMES_A = [95.0, 102.0, 110.0, 115.0, 127.0, 139.0, 145.0]
FRAMES_B = [207.0, 220.0, 231.0, 244.0]
FRAMES_OUTSIDE = [10.0, 50.0]


def make_config(tmp_path, **sections):
    return Config(
        mode="review",
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "output",
        _sections=sections,
    )


def frame_name(t):
    return f"frames/f{int(t):04d}.jpg"


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def seed(video_dir, *, frames=FRAMES_A + FRAMES_B + FRAMES_OUTSIDE, peak_windows=True, speech=None):
    video_dir.mkdir(parents=True, exist_ok=True)
    peaks = [{"timecode": t, "relative_db": 10.0 + (5.0 if t == 110.0 else 0.0)} for t in ZONE_A_PEAKS + ZONE_B_PEAKS]
    peaks += [{"timecode": 20.0, "relative_db": 12.0}, {"timecode": 50.0, "relative_db": 3.0}]
    write_json(video_dir / "audio.json", {"peaks": sorted(peaks, key=lambda p: p["timecode"])})

    starts = [5.0 + 30 * k for k in range(10)]
    starts += [t + 2 for t in ZONE_A_PEAKS] + [t + 2 for t in ZONE_B_PEAKS]
    starts = sorted(starts)
    scene_list = [{"start": s, "end": e} for s, e in zip(starts, starts[1:] + [DURATION])]
    (video_dir / "frames").mkdir(exist_ok=True)
    frame_list = []
    for n, t in enumerate(sorted(frames)):
        cv2.imwrite(str(video_dir / frame_name(t)), np.full((18, 32, 3), 40 + n, dtype=np.uint8))
        frame_list.append({"path": frame_name(t), "timecode": t, "scene": n})
    scenes = {"scenes": scene_list, "frames": frame_list}
    if peak_windows:
        scenes["peak_windows"] = True
    write_json(video_dir / "scenes.json", scenes)

    segments = speech if speech is not None else [{"start": 92.0, "end": 98.0, "text": "il est la"}]
    write_json(video_dir / "transcript.json", {"segments": segments})
    write_json(video_dir / "meta.json", {"duration": DURATION})


@pytest.fixture
def video_dir(tmp_path):
    d = tmp_path / "workspace" / VIDEO_ID
    seed(d)
    return d


def describe_all(action_type="combat", intensity=7):
    def answer(request):
        frames = []
        for index_str, t_str in re.findall(r"Image (\d+) : ([\d.]+) s", request.prompt):
            frames.append(
                {
                    "index": int(index_str),
                    "description": f"image a {float(t_str):.1f}",
                    "action_type": action_type,
                    "intensity": intensity,
                }
            )
        return {"frames": frames}

    return answer


BIG_CAPS = {"enabled": True, "max_passages_per_hour": 60, "max_images_per_hour": 120}


def run_action(tmp_path, responses=None, *, force=False, **settings):
    from clipper.action import run

    fake = FakeBackend([describe_all()] if responses is None else responses)
    config = make_config(tmp_path, action={"enabled": True, **settings})
    with llm.use_backend(fake):
        path = run(VIDEO_ID, tmp_path / "workspace", config=config, force=force)
    return fake, path


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# (1) reglages et independance
# --------------------------------------------------------------------------


def test_config_defaults_are_the_r5_values():
    from clipper.action import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS == {
        "enabled": False,
        "window_seconds": 30,
        "step_seconds": 15,
        "audio_weight": 1.0,
        "audio_peaks_full": 3,
        "audio_peak_min_db": 6.0,
        "cuts_weight": 1.0,
        "cuts_ratio_full": 3.0,
        "min_score": 0.6,
        "max_passage_seconds": 90,
        "max_passages_per_hour": 12,
        "frames_per_passage": 4,
        "max_images_per_hour": 48,
        "batch_size": 8,
        "max_width": 768,
        "parallel": 4,
    }


@pytest.mark.parametrize(
    "key, value",
    [
        ("enabled", "yes"),
        ("window_seconds", 0),
        ("step_seconds", 0),
        ("step_seconds", 31),
        ("audio_weight", -1),
        ("cuts_weight", -0.5),
        ("audio_peaks_full", 0),
        ("cuts_ratio_full", 0),
        ("min_score", 0),
        ("min_score", 1.5),
        ("max_passage_seconds", 29),
        ("max_passages_per_hour", 0),
        ("frames_per_passage", 0),
        ("max_images_per_hour", 0),
        ("batch_size", 0),
        ("max_width", 0),
        ("parallel", 0),
        ("parallel", 1.5),
    ],
)
def test_invalid_setting_is_refused_naming_the_key(tmp_path, video_dir, key, value):
    from clipper.action import ActionError

    with pytest.raises(ActionError, match=key):
        run_action(tmp_path, **{key: value})
    assert not (video_dir / "action.json").exists()


def test_both_weights_zero_is_refused_naming_the_weights(tmp_path, video_dir):
    from clipper.action import ActionError

    with pytest.raises(ActionError, match="audio_weight"):
        run_action(tmp_path, audio_weight=0, cuts_weight=0)


def test_action_imports_no_other_step_nor_the_web(tmp_path):
    source = (Path(__file__).resolve().parent.parent / "clipper" / "action.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported |= {f"{base}.{alias.name}" if base == "clipper" else base for alias in node.names}
    clipper_modules = {name for name in imported if name.startswith("clipper")}
    assert clipper_modules <= {"clipper.llm", "clipper.montage", "clipper.config"}, clipper_modules
    assert not any("web" in name for name in clipper_modules)


# --------------------------------------------------------------------------
# (2) detection R6
# --------------------------------------------------------------------------


def test_two_concentrated_zones_give_exactly_two_passages(tmp_path, video_dir):
    _, path = run_action(tmp_path, **BIG_CAPS)
    result = load(path)

    passages = result["passages"]
    assert [(p["id"], p["start"], p["end"], p["duration"]) for p in passages] == [
        ("a0", 90.0, 150.0, 60.0),
        ("a1", 195.0, 255.0, 60.0),
    ]
    assert [p["score"] for p in passages] == [1.0, 1.0]
    a, b = (p["signals"] for p in passages)
    # fenetres 90 / 105 / 120 : 4 + 5 + 2 pics, 5 + 6 + 3 coupes (mediane 1), parole 6 s sur 30 dans la premiere
    assert (a["audio_peaks"], a["audio_peak_max_db"], a["scene_cuts"]) == (11, 15.0, 14)
    assert a["scene_cuts_ratio"] == pytest.approx(14 / 3, abs=1e-3)
    assert a["speech_ratio"] == pytest.approx(0.2 / 3, abs=1e-3)
    assert (b["audio_peaks"], b["audio_peak_max_db"], b["scene_cuts"]) == (11, 10.0, 14)
    assert b["speech_ratio"] == 0
    assert result["rejected"] == []


def test_a_peak_below_audio_peak_min_db_or_a_lone_peak_makes_no_passage(tmp_path, video_dir):
    _, path = run_action(tmp_path, **BIG_CAPS)
    assert all(not (0 <= p["start"] < 60) for p in load(path)["passages"])


def test_too_long_passage_is_cut_at_max_passage_seconds(tmp_path, video_dir):
    _, path = run_action(tmp_path, max_passage_seconds=30, **BIG_CAPS)
    passages = load(path)["passages"]

    zone_a = [p for p in passages if p["end"] <= 150]
    assert [(p["start"], p["end"]) for p in zone_a] == [(90.0, 120.0), (120.0, 150.0)]
    assert all(p["duration"] <= 30 for p in passages)
    first, second = zone_a
    assert (first["signals"]["audio_peaks"], first["signals"]["scene_cuts"]) == (4, 5)
    assert (second["signals"]["audio_peaks"], second["signals"]["scene_cuts"]) == (7, 9)


def test_passage_cap_per_hour_rejects_the_lowest_ranked_with_its_reason(tmp_path, video_dir):
    # ceil(12 x 300 / 3600) = 1 passage garde : a egalite de score, le plus tot
    _, path = run_action(tmp_path, enabled=True, max_images_per_hour=120)
    result = load(path)

    assert [(p["start"], p["end"]) for p in result["passages"]] == [(90.0, 150.0)]
    assert len(result["rejected"]) == 1
    rejected = result["rejected"][0]
    assert (rejected["start"], rejected["end"], rejected["score"]) == (195.0, 255.0, 1.0)
    assert rejected["reason"] == "plafond max_passages_per_hour"
    assert rejected["signals"]["audio_peaks"] == 11


def test_no_passage_writes_an_empty_result_without_calling_the_llm(tmp_path, video_dir, caplog):
    write_json(video_dir / "audio.json", {"peaks": []})
    fake = FakeBackend([])
    config = make_config(tmp_path, action={"enabled": True})
    from clipper.action import run

    with llm.use_backend(fake), caplog.at_level(logging.INFO, logger="clipper.action"):
        path = run(VIDEO_ID, tmp_path / "workspace", config=config)

    result = load(path)
    assert result["passages"] == [] and result["rejected"] == []
    assert fake.calls == []
    assert (result["llm_calls"], result["images_sent"]) == (0, 0)
    assert any(r.levelno == logging.INFO for r in caplog.records)


# --------------------------------------------------------------------------
# (3) images R7
# --------------------------------------------------------------------------


def test_frames_are_those_nearest_the_planned_instants(tmp_path, video_dir):
    _, path = run_action(tmp_path, **BIG_CAPS)
    a, b = load(path)["passages"]

    # instants prevus : 90 + 12k (102, 114, 126, 138) et 195 + 12k (207, 219, 231, 243)
    assert [f["timecode"] for f in a["frames"]] == [102.0, 115.0, 127.0, 139.0]
    assert [f["timecode"] for f in b["frames"]] == [207.0, 220.0, 231.0, 244.0]
    assert a["frames"][0]["path"] == "frames/f0102.jpg"
    assert a["frames_missing"] is False


def test_fewer_images_than_frames_per_passage_takes_what_exists(tmp_path, video_dir):
    _, path = run_action(tmp_path, frames_per_passage=8, **BIG_CAPS)
    a = load(path)["passages"][0]
    assert [f["timecode"] for f in a["frames"]] == [95.0, 102.0, 110.0, 115.0, 127.0, 139.0, 145.0]


def test_passage_without_image_is_flagged_without_any_extraction(tmp_path, monkeypatch, caplog):
    import subprocess

    d = tmp_path / "workspace" / VIDEO_ID
    seed(d, frames=FRAMES_A + FRAMES_OUTSIDE)  # aucune image dans le passage B

    def forbidden(*args, **kwargs):
        raise AssertionError("l'etape action ne lance aucun sous-processus")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with caplog.at_level(logging.WARNING, logger="clipper.action"):
        _, path = run_action(tmp_path, **BIG_CAPS)

    a, b = load(path)["passages"]
    assert (b["frames"], b["frames_missing"]) == ([], True)
    assert a["frames_missing"] is False
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_image_cap_per_hour_rejects_the_passage_that_would_exceed_it(tmp_path, video_dir):
    # ceil(48 x 300 / 3600) = 4 images : le premier passage prend les 4, le second est rejete
    _, path = run_action(tmp_path, enabled=True, max_passages_per_hour=60)
    result = load(path)

    assert [(p["start"], p["end"]) for p in result["passages"]] == [(90.0, 150.0)]
    assert [(r["start"], r["reason"]) for r in result["rejected"]] == [(195.0, "plafond max_images_per_hour")]
    assert result["images_sent"] == 4


# --------------------------------------------------------------------------
# (4) LLM R8
# --------------------------------------------------------------------------


def test_one_montage_per_batch_one_image_file_per_call_with_the_action_usage(tmp_path, video_dir):
    sizes = []

    def answer(request):
        sizes.append((len(request.images), request.images[0].name, request.images[0].is_file()))
        return describe_all()(request)

    fake, path = run_action(tmp_path, [answer], batch_size=3, **BIG_CAPS)

    assert len(fake.calls) == 3  # 8 images / 3
    assert {call.usage for call in fake.calls} == {"action"}
    assert all(count == 1 and is_file for count, _, is_file in sizes)
    assert sorted(name for _, name, _ in sizes) == ["00000_montage.jpg", "00001_montage.jpg", "00002_montage.jpg"]
    result = load(path)
    assert (result["llm_calls"], result["images_sent"]) == (3, 8)
    frame = result["passages"][0]["frames"][0]
    assert frame == {
        "timecode": 102.0, "path": "frames/f0102.jpg", "description": "image a 102.0",
        "action_type": "combat", "intensity": 7,
    }


def test_response_schema_accepts_only_the_declared_action_types(tmp_path, video_dir):
    fake, _ = run_action(tmp_path, batch_size=8, **BIG_CAPS)
    schema = fake.calls[0].schema
    item = schema["properties"]["frames"]["items"]["properties"]
    assert item["action_type"]["enum"] == [
        "combat", "clutch", "mort", "victoire", "defaite", "retournement", "sursaut",
        "exploration", "menu_ou_chargement", "webcam_ou_chat_seul", "autre",
    ]
    assert (item["intensity"]["minimum"], item["intensity"]["maximum"]) == (0, 10)
    assert item["description"]["maxLength"] == 300


def _missing_index(request):
    answer = describe_all()(request)
    answer["frames"] = answer["frames"][:-1] + [dict(answer["frames"][0])]  # index 0 deux fois, dernier absent
    return answer


def test_missing_or_duplicate_index_is_a_schema_error_and_nothing_is_written(tmp_path, video_dir):
    with pytest.raises(llm.SchemaError):
        run_action(tmp_path, [_missing_index], **BIG_CAPS)
    assert not (video_dir / "action.json").exists()


def test_llm_unavailable_writes_nothing_and_leaves_no_temporary_folder(tmp_path, video_dir):
    before = sorted(p.name for p in video_dir.rglob("*") if p.is_file() and "frames" in p.parts)
    with pytest.raises(llm.TransientLLMError):
        run_action(tmp_path, [llm.TransientLLMError("quota")], **BIG_CAPS)

    assert not (video_dir / "action.json").exists()
    assert sorted(p.name for p in video_dir.iterdir() if p.is_dir()) == ["frames"]
    assert sorted(p.name for p in video_dir.rglob("*") if p.is_file() and "frames" in p.parts) == before


def test_batches_run_in_parallel_up_to_the_setting(tmp_path, video_dir):
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def answer(request):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.15)
        with lock:
            state["active"] -= 1
        return describe_all()(request)

    run_action(tmp_path, [answer], batch_size=2, parallel=2, **BIG_CAPS)  # 4 lots
    assert state["peak"] == 2


def test_a_relaunch_resumes_from_action_partial_json(tmp_path, video_dir):
    def fail_on_late_frames(request):
        if "Image 0 : 207.0 s" in request.prompt or "Image 1 : 207.0 s" in request.prompt:
            raise llm.TransientLLMError("coupure")
        return describe_all()(request)

    with pytest.raises(llm.TransientLLMError):
        run_action(tmp_path, [fail_on_late_frames], batch_size=4, parallel=1, **BIG_CAPS)
    assert not (video_dir / "action.json").exists()
    assert (video_dir / "action_partial.json").exists()

    fake, path = run_action(tmp_path, [describe_all()], batch_size=4, parallel=1, **BIG_CAPS)
    assert len(fake.calls) == 1  # seul le lot qui avait echoue est redemande
    assert load(path)["llm_calls"] == 1  # media-M5 : les appels reels, pas les lots du plan
    assert path.exists()
    assert not (video_dir / "action_partial.json").exists()
    assert all(f["description"] for p in load(path)["passages"] for f in p["frames"])


# --------------------------------------------------------------------------
# (5) sortie R9, enabled, force, entrees
# --------------------------------------------------------------------------


def test_output_has_the_exact_r9_shape(tmp_path, video_dir):
    from clipper.action import CONFIG_DEFAULTS

    _, path = run_action(tmp_path, **BIG_CAPS)
    result = load(path)

    assert path == video_dir / "action.json"
    assert list(result) == ["video_id", "enabled", "settings", "passages", "rejected", "llm_calls", "images_sent"]
    assert result["video_id"] == VIDEO_ID and result["enabled"] is True
    assert set(result["settings"]) == set(CONFIG_DEFAULTS)
    assert result["settings"]["max_passages_per_hour"] == 60
    assert list(result["passages"][0]) == ["id", "start", "end", "duration", "score", "signals", "frames", "frames_missing"]
    assert list(result["passages"][0]["signals"]) == [
        "audio_peaks", "audio_peak_max_db", "scene_cuts", "scene_cuts_ratio", "speech_ratio",
    ]


def test_disabled_writes_the_empty_file_without_reading_inputs_or_calling_the_llm(tmp_path):
    from clipper.action import run

    (tmp_path / "workspace" / VIDEO_ID).mkdir(parents=True)  # ni audio.json ni scenes.json
    fake = FakeBackend([])
    with llm.use_backend(fake):
        path = run(VIDEO_ID, tmp_path / "workspace", config=make_config(tmp_path))

    assert load(path) == {
        "video_id": VIDEO_ID, "enabled": False, "passages": [], "rejected": [], "llm_calls": 0, "images_sent": 0,
    }
    assert fake.calls == []


def test_existing_result_is_not_redone_unless_forced(tmp_path, video_dir):
    _, path = run_action(tmp_path, **BIG_CAPS)
    first = path.read_bytes()
    write_json(video_dir / "audio.json", {"peaks": []})

    fake, path = run_action(tmp_path, **BIG_CAPS)
    assert path.read_bytes() == first and fake.calls == []

    _, path = run_action(tmp_path, force=True, **BIG_CAPS)
    assert load(path)["passages"] == []


def test_enabled_with_scenes_without_peak_windows_is_an_error_and_writes_nothing(tmp_path):
    from clipper.action import ActionError

    d = tmp_path / "workspace" / VIDEO_ID
    seed(d, peak_windows=False)
    with pytest.raises(ActionError, match=re.escape(
        "scenes.json produit sans fenêtres de pics : relancer scenes avec --force "
        "(style avec [action] enabled = true)"
    )):
        run_action(tmp_path, **BIG_CAPS)
    assert not (d / "action.json").exists()


@pytest.mark.parametrize("missing", ["audio.json", "scenes.json", "transcript.json", "meta.json"])
def test_missing_input_is_an_explicit_error_naming_the_file(tmp_path, video_dir, missing):
    from clipper.action import ActionError

    (video_dir / missing).unlink()
    with pytest.raises(ActionError, match=re.escape(missing)):
        run_action(tmp_path, **BIG_CAPS)
    assert not (video_dir / "action.json").exists()
