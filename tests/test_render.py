from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from clipper.config import Config

VIDEO_ID = "abcdefghijk"
CLIP_ID = "03"
SRC_W, SRC_H = 1920, 1080
OUT_W, OUT_H = 1080, 1920
CROP_W = 608  # cadre 9:16 pleine hauteur dans une source 1920x1080

no_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent du PATH")
no_ffprobe = pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe absent du PATH")


def make_config(**render_settings):
    return Config(mode="review", workspace_dir=Path("workspace"), output_dir=Path("output"),
                  _sections={"render": render_settings} if render_settings else {})


# --------------------------------------------------------------------------
# Fixtures : arborescence workspace/<video_id>/ realiste (sorties reelles des
# etapes dont depend render, jamais importees - ADR-b16b).
# --------------------------------------------------------------------------


def _captions_json(**overrides):
    clip = {
        "id": CLIP_ID,
        "moment_id": 3,
        "part": 1,
        "parts_total": 1,
        "start": 1.0,
        "end": 3.5,
        "duration": 2.5,
        "language": "fr",
        "title": "Titre du clip",
        "caption": "Une legende qui donne envie",
        "hashtags": ["#gta6", "#trailer"],
        "hook_text": "Attends de voir ca",
        "screen_title": "Il m'a menti en garde à vue",
    }
    clip.update(overrides)
    return {"video_id": VIDEO_ID, "clips": [clip]}


def _moments_json():
    return {
        "video_id": VIDEO_ID,
        "rubric": {"path": "rubric.toml", "weights": {}, "min_score": 60},
        "moments": [
            {
                "id": 3,
                "start": 1.0,
                "end": 3.5,
                "duration": 2.5,
                "format": "single",
                "parts": [],
                "scores": {"hook": 8, "standalone": 7, "payoff": 6, "emotion": 5, "value": 6, "trend": 9},
                "bonus": 4.0,
                "final_score": 78.5,
                "justification": "Revelation choc sur GTA 6",
                "hook_text": "Attends de voir ca",
            }
        ],
        "rejected": [],
    }


def _transcript_json():
    words = [
        {"word": "Attends ", "start": 1.0, "end": 1.4},
        {"word": "de ", "start": 1.4, "end": 1.6},
        {"word": "voir ", "start": 1.6, "end": 1.9},
        {"word": "ca.", "start": 1.9, "end": 2.2},
        {"word": "Incroyable.", "start": 2.5, "end": 3.4},
    ]
    return {
        "language": "fr",
        "segments": [
            {"start": 1.0, "end": 2.2, "text": "Attends de voir ca.", "words": words[:4]},
            {"start": 2.5, "end": 3.4, "text": "Incroyable.", "words": words[4:]},
        ],
    }


def _meta_json(**overrides):
    meta = {
        "video_id": VIDEO_ID,
        "title": "Une video source",
        "webpage_url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
    }
    meta.update(overrides)
    return meta


def _panel(name, x, y, w, h, dest, effect=None, start=1.0, end=3.5):
    panel = {"name": name, "dest": dest, "rects": [{"start": start, "end": end, "x": x, "y": y, "w": w, "h": h}]}
    if effect:
        panel["effect"] = effect
    return panel


def _reframe_json_single_plan():
    panels = [_panel("main", 656, 0, CROP_W, SRC_H, {"x": 0, "y": 0, "w": OUT_W, "h": OUT_H})]
    plan = {
        "index": 0, "start": 1.0, "end": 3.5, "image": "reframe/03/plan_000.jpg",
        "llm": {"layout": "single", "camera": None, "face": None, "reason": "plan centre"},
        "layout": "single", "reason": None, "faces": [], "panels": panels,
    }
    return {
        "video_id": VIDEO_ID, "clip_id": CLIP_ID, "start": 1.0, "end": 3.5,
        "source": {"width": SRC_W, "height": SRC_H}, "output": {"width": OUT_W, "height": OUT_H},
        "layout": "single", "plans": [plan],
    }


def _reframe_json_two_plans():
    """1er plan facecam_gameplay (camera fixe + gameplay qui bouge), 2e plan
    fallback_blur (fond floute + image entiere) : exerce crop/scale, pile
    facecam/gameplay et fond flou dans le meme clip."""
    cam_h = round(OUT_H * 0.4)
    game_h = OUT_H - cam_h
    plan0 = {
        "index": 0, "start": 1.0, "end": 2.2, "image": "x", "llm": {}, "layout": "facecam_gameplay",
        "reason": None, "faces": [],
        "panels": [
            {"name": "camera", "dest": {"x": 0, "y": 0, "w": OUT_W, "h": cam_h},
             "rects": [{"start": 1.0, "end": 2.2, "x": 700, "y": 50, "w": 400, "h": 300}]},
            {"name": "gameplay", "dest": {"x": 0, "y": cam_h, "w": OUT_W, "h": game_h},
             "rects": [
                 {"start": 1.0, "end": 1.6, "x": 0, "y": 0, "w": 960, "h": SRC_H},
                 {"start": 1.6, "end": 2.2, "x": 960, "y": 0, "w": 960, "h": SRC_H},
             ]},
        ],
    }
    plan1 = {
        "index": 1, "start": 2.2, "end": 3.5, "image": "x", "llm": {}, "layout": "fallback_blur",
        "reason": "aucun cadre ne garde les visages entiers", "faces": [],
        "panels": [
            {"name": "background", "effect": "blur", "dest": {"x": 0, "y": 0, "w": OUT_W, "h": OUT_H},
             "rects": [{"start": 2.2, "end": 3.5, "x": 0, "y": 0, "w": SRC_W, "h": SRC_H}]},
            {"name": "main", "dest": {"x": 0, "y": 391, "w": OUT_W, "h": 608},
             "rects": [{"start": 2.2, "end": 3.5, "x": 0, "y": 0, "w": SRC_W, "h": SRC_H}]},
        ],
    }
    return {
        "video_id": VIDEO_ID, "clip_id": CLIP_ID, "start": 1.0, "end": 3.5,
        "source": {"width": SRC_W, "height": SRC_H}, "output": {"width": OUT_W, "height": OUT_H},
        "layout": "facecam_gameplay", "plans": [plan0, plan1],
    }


ASS_TEXT = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Poppins ExtraBold,96,&H00FFFFFF&,&H0080FFFF&,&H00000000&,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,2,40,40,160,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\k100}Attends
"""


@pytest.fixture
def video_dir(tmp_path):
    d = tmp_path / "workspace" / VIDEO_ID
    d.mkdir(parents=True)
    (d / "captions.json").write_text(json.dumps(_captions_json()), encoding="utf-8")
    (d / "moments.json").write_text(json.dumps(_moments_json()), encoding="utf-8")
    (d / "transcript.json").write_text(json.dumps(_transcript_json()), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(_meta_json()), encoding="utf-8")
    (d / "reframe").mkdir()
    (d / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(_reframe_json_single_plan()), encoding="utf-8")
    (d / "subtitles").mkdir()
    (d / "subtitles" / f"{CLIP_ID}.ass").write_text(ASS_TEXT, encoding="utf-8")
    return d


@pytest.fixture
def synthetic_source(video_dir):
    video = video_dir / f"{VIDEO_ID}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={SRC_W}x{SRC_H}:rate=25:duration=5",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=5",
         "-ac", "2", "-shortest", str(video)],
        check=True,
    )
    return video


@pytest.fixture
def cpu_device(fake_ctranslate2):
    fake_ctranslate2(cuda_device_count=0)


def _ffprobe_json(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        stdout=subprocess.PIPE, check=True,
    )
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------
# C6/C11 : rendu ffmpeg reel, verifie par ffprobe (done_criteria)
# --------------------------------------------------------------------------


@no_ffmpeg
@no_ffprobe
def test_render_writes_1080x1920_h264_aac_mp4_matching_clip_duration(tmp_path, video_dir, synthetic_source, cpu_device):
    from clipper.render import render

    out = render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
                 config=make_config())

    assert out == tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.mp4"
    assert out.exists()
    probe = _ffprobe_json(out)
    streams = {s["codec_type"]: s for s in probe["streams"]}
    assert streams["video"]["width"] == OUT_W
    assert streams["video"]["height"] == OUT_H
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"
    assert float(probe["format"]["duration"]) == pytest.approx(2.5, abs=0.1)


@no_ffmpeg
@no_ffprobe
def test_render_converts_a_25fps_source_to_30fps_output(tmp_path, video_dir, synthetic_source, cpu_device):
    """SPEC-6127 exige 30 i/s en sortie ; la source synthetique est a 25 i/s
    (constat de l'essai reel du 2026-09-25) : render doit convertir, pas
    garder la cadence source."""
    from clipper.render import render

    out = render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
                 config=make_config())

    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(out)],
        stdout=subprocess.PIPE, check=True,
    )
    assert proc.stdout.decode().strip() == "30/1"


def _reframe_json_full_frame_blur(duration):
    """Un seul plan, un seul panneau fond flou couvrant tout le cadre de
    sortie : maximise la part du boxblur dans le cout du rendu, pour que le
    ratio avant/apres reste mesurable sur une video courte."""
    plan = {
        "index": 0, "start": 0.0, "end": duration, "image": "x", "llm": {}, "layout": "fallback_blur",
        "reason": "aucun cadre ne garde les visages entiers", "faces": [],
        "panels": [
            {"name": "background", "effect": "blur", "dest": {"x": 0, "y": 0, "w": OUT_W, "h": OUT_H},
             "rects": [{"start": 0.0, "end": duration, "x": 0, "y": 0, "w": SRC_W, "h": SRC_H}]},
        ],
    }
    return {
        "video_id": VIDEO_ID, "clip_id": CLIP_ID, "start": 0.0, "end": duration,
        "source": {"width": SRC_W, "height": SRC_H}, "output": {"width": OUT_W, "height": OUT_H},
        "layout": "fallback_blur", "plans": [plan],
    }


def test_render_writes_conforming_output_when_a_panel_uses_fallback_blur(tmp_path, cpu_device):
    """Structure du graphe ffmpeg pour fallback_blur (sans mesure de temps,
    instable sous charge - voir le test optionnel CLIPPER_BENCH ci-dessous) :
    _build_filter_complex reduit avant boxblur puis agrandit vers dest, et le
    label de sortie chaine correctement jusqu'a l'accroche/sous-titres."""
    from clipper.render import CONFIG_DEFAULTS, _build_filter_complex

    duration = 2.5
    reframe_data = _reframe_json_full_frame_blur(duration)
    hook_path = tmp_path / "hook.txt"
    hook_path.write_text("x", encoding="utf-8")
    ass_path = tmp_path / "sub.ass"
    ass_path.write_text(ASS_TEXT, encoding="utf-8")

    filt, label = _build_filter_complex(
        reframe_data, 0.0, duration, ass_path, hook_path, None, tmp_path, CONFIG_DEFAULTS
    )

    factor = CONFIG_DEFAULTS["blur_downscale"]
    assert f"scale=iw/{factor}:ih/{factor}" in filt
    assert filt.index(f"scale=iw/{factor}:ih/{factor}") < filt.index("boxblur")
    assert filt.index("boxblur") < filt.rindex(f"scale={OUT_W}:{OUT_H}")
    assert label == "vhook"


# --------------------------------------------------------------------------
# Vitesse (mesure de temps reelle, instable sous charge machine) : optionnel,
# saute par defaut, active par CLIPPER_BENCH=1 (consigne orchestrateur
# 2026-09-28 : pas d'assertion de temps reel dans la suite par defaut).
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("CLIPPER_BENCH") != "1",
    reason="mesure de vitesse reelle, instable sous charge : definir CLIPPER_BENCH=1",
)
@no_ffmpeg
@no_ffprobe
def test_render_fallback_blur_is_at_least_3x_faster_than_full_resolution_blur(tmp_path, cpu_device):
    """Constat essai reel 2026-09-25 : ~1200s CPU pour 40s de clip en
    fallback_blur, contre ~45s sans flou, a cause du boxblur plein cadre
    1080x1920. blur_downscale=1 (pas de reduction) rejoue ce cout ; le
    reglage par defaut doit rendre au moins 3 fois plus vite, a parametres
    egaux par ailleurs. Mesure en ratio (jamais un temps absolu), meilleur de
    plusieurs essais (le pire cas est un pic de charge de la machine, jamais
    un rendu plus rapide que sa vraie duree), pour rester robuste."""
    import time

    from clipper.render import render

    duration = 8.0
    d = tmp_path / "workspace" / VIDEO_ID
    d.mkdir(parents=True)
    (d / "captions.json").write_text(
        json.dumps(_captions_json(start=0.0, end=duration, duration=duration)), encoding="utf-8"
    )
    (d / "moments.json").write_text(json.dumps(_moments_json()), encoding="utf-8")
    (d / "transcript.json").write_text(json.dumps(_transcript_json()), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(_meta_json()), encoding="utf-8")
    (d / "reframe").mkdir()
    (d / "reframe" / f"{CLIP_ID}.json").write_text(
        json.dumps(_reframe_json_full_frame_blur(duration)), encoding="utf-8"
    )
    (d / "subtitles").mkdir()
    (d / "subtitles" / f"{CLIP_ID}.ass").write_text(ASS_TEXT, encoding="utf-8")
    video = d / f"{VIDEO_ID}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={SRC_W}x{SRC_H}:rate=25:duration={duration}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}",
         "-ac", "2", "-shortest", str(video)],
        check=True,
    )

    repeats = 3
    baseline_times = []
    fast_times = []
    for i in range(repeats):
        start = time.perf_counter()
        render(
            VIDEO_ID, CLIP_ID, workspace_dir=d.parent, output_dir=tmp_path / f"output_baseline{i}",
            config=make_config(blur_downscale=1),
        )
        baseline_times.append(time.perf_counter() - start)

        start = time.perf_counter()
        render(
            VIDEO_ID, CLIP_ID, workspace_dir=d.parent, output_dir=tmp_path / f"output_fast{i}",
            config=make_config(),
        )
        fast_times.append(time.perf_counter() - start)

    # min() plutot que la moyenne : un pic de charge ne peut que ralentir un
    # essai, jamais l'accelerer, donc le minimum approxime le cout reel.
    assert min(fast_times) * 3 <= min(baseline_times)


@no_ffmpeg
@no_ffprobe
def test_render_applies_facecam_gameplay_then_fallback_blur_panels(tmp_path, video_dir, synthetic_source, cpu_device):
    """Deux plans, layouts differents (facecam/gameplay puis fond flou) :
    exerce crop/scale, pile facecam/gameplay et fond flou dans un seul rendu."""
    from clipper.render import render

    (video_dir / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(_reframe_json_two_plans()), encoding="utf-8")

    out = render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
                 config=make_config())

    probe = _ffprobe_json(out)
    streams = {s["codec_type"]: s for s in probe["streams"]}
    assert streams["video"]["width"] == OUT_W
    assert streams["video"]["height"] == OUT_H
    assert streams["video"]["codec_name"] == "h264"
    assert float(probe["format"]["duration"]) == pytest.approx(2.5, abs=0.1)


# --------------------------------------------------------------------------
# JSON de sortie (SPEC-6127)
# --------------------------------------------------------------------------


@no_ffmpeg
@no_ffprobe
def test_render_writes_json_sidecar_conforming_to_spec_350f(tmp_path, video_dir, synthetic_source, cpu_device):
    from clipper.render import render

    render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output", config=make_config())

    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))

    required = {
        "video_id", "source_url", "source_title", "clip_id", "part", "parts_total",
        "start", "end", "duration", "language", "score", "scores", "reason", "hook_text",
        "title", "caption", "hashtags", "transcript", "layout", "qa", "created_at",
    }
    assert required <= set(data)
    assert data["video_id"] == VIDEO_ID
    assert data["clip_id"] == CLIP_ID
    assert data["part"] == 1
    assert data["parts_total"] == 1
    assert data["start"] == 1.0
    assert data["end"] == 3.5
    assert data["duration"] == 2.5
    assert data["language"] == "fr"
    assert data["score"] == 78.5
    assert data["scores"] == {"hook": 8, "standalone": 7, "payoff": 6, "emotion": 5, "value": 6, "trend": 9}
    assert data["reason"] == "Revelation choc sur GTA 6"
    assert data["hook_text"] == "Attends de voir ca"
    assert data["title"] == "Titre du clip"
    assert data["caption"] == "Une legende qui donne envie"
    assert data["hashtags"] == ["#gta6", "#trailer"]
    assert data["layout"] == "single"
    assert data["source_title"] == "Une video source"
    assert data["source_url"] == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert data["qa"] == {"status": "skipped", "issues": []}
    assert data["transcript"] == "Attends de voir ca.Incroyable."
    assert data["created_at"]  # horodatage ISO 8601 non vide


@no_ffmpeg
@no_ffprobe
def test_render_uses_meta_webpage_url_as_source_url_never_reconstructed(
    tmp_path, video_dir, synthetic_source, cpu_device
):
    from clipper.render import render

    twitch_url = "https://www.twitch.tv/videos/2887271276"
    (video_dir / "meta.json").write_text(
        json.dumps(_meta_json(video_id=VIDEO_ID, webpage_url=twitch_url)), encoding="utf-8"
    )

    render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output", config=make_config())

    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["source_url"] == twitch_url
    assert "youtube.com" not in data["source_url"]


def test_render_raises_when_meta_json_has_no_webpage_url(tmp_path, video_dir, synthetic_source, cpu_device):
    from clipper.render import render, RenderError

    meta = _meta_json()
    del meta["webpage_url"]
    (video_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(RenderError):
        render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
               config=make_config())


# --------------------------------------------------------------------------
# Cache par resultat existant (ADR-b16b)
# --------------------------------------------------------------------------


@no_ffmpeg
@no_ffprobe
def test_render_skips_ffmpeg_when_output_already_present(tmp_path, video_dir, synthetic_source, cpu_device):
    from clipper.render import render

    out_dir = tmp_path / "output"
    first = render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=out_dir, config=make_config())
    original_bytes = first.read_bytes()

    second = render(
        VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=out_dir, config=make_config(),
        ffmpeg_bin="ffmpeg-does-not-exist",
    )

    assert second == first
    assert second.read_bytes() == original_bytes


@no_ffmpeg
@no_ffprobe
def test_render_force_redoes_the_render_even_if_output_present(tmp_path, video_dir, synthetic_source, cpu_device):
    from clipper.render import render

    out_dir = tmp_path / "output"
    render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=out_dir, config=make_config())

    with pytest.raises(Exception):
        render(
            VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=out_dir, config=make_config(),
            force=True, ffmpeg_bin="ffmpeg-does-not-exist",
        )


# --------------------------------------------------------------------------
# Entrees manquantes ou incoherentes : jamais de repli silencieux (ADR-ad2e)
# --------------------------------------------------------------------------


def test_missing_source_video_is_an_error(tmp_path, video_dir):
    from clipper.render import RenderError, render

    with pytest.raises(RenderError, match=f"{VIDEO_ID}.mp4"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
               config=make_config())


def test_missing_captions_json_is_an_error(tmp_path, video_dir):
    from clipper.render import RenderError, render

    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    (video_dir / "captions.json").unlink()
    with pytest.raises(RenderError, match="captions.json"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
               config=make_config())


def test_missing_reframe_json_is_an_error(tmp_path, video_dir):
    from clipper.render import RenderError, render

    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    (video_dir / "reframe" / f"{CLIP_ID}.json").unlink()
    with pytest.raises(RenderError, match="entree absente"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
               config=make_config())


def test_missing_subtitles_ass_is_an_error(tmp_path, video_dir):
    from clipper.render import RenderError, render

    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    (video_dir / "subtitles" / f"{CLIP_ID}.ass").unlink()
    with pytest.raises(RenderError, match="sous-titres absents"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
               config=make_config())


def test_clip_id_absent_from_captions_is_an_error(tmp_path, video_dir):
    from clipper.render import RenderError, render

    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="captions.json"):
        render(VIDEO_ID, "99", workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
               config=make_config())


def test_start_end_mismatch_between_captions_and_reframe_is_an_error(tmp_path, video_dir):
    from clipper.render import RenderError, render

    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    reframe = _reframe_json_single_plan()
    reframe["start"] = 9.0
    reframe["end"] = 20.0
    (video_dir / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(reframe), encoding="utf-8")
    with pytest.raises(RenderError, match="incoherentes"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
               config=make_config())


# --------------------------------------------------------------------------
# Encodeur video : NVENC si CUDA, sinon libx264 (ADR-fb9b)
# --------------------------------------------------------------------------


def test_encoder_selects_nvenc_when_device_is_cuda():
    from clipper.render import CONFIG_DEFAULTS, _encoder

    args = _encoder("cuda", CONFIG_DEFAULTS)
    assert args[:2] == ["-c:v", "h264_nvenc"]


def test_encoder_selects_libx264_when_device_is_cpu():
    from clipper.render import CONFIG_DEFAULTS, _encoder

    args = _encoder("cpu", CONFIG_DEFAULTS)
    assert args[:2] == ["-c:v", "libx264"]


# --------------------------------------------------------------------------
# Construction du filtergraph : accroche 2s, Part N/M, crop/scale, loudnorm
# --------------------------------------------------------------------------


def test_time_expr_is_a_plain_number_for_a_single_rect():
    from clipper.render import _time_expr

    rects = [{"start": 1.0, "end": 3.5, "x": 656, "y": 0, "w": 608, "h": 1080}]
    assert _time_expr(rects, 1.0, "x") == "656"


def test_time_expr_builds_an_if_chain_relative_to_plan_start_for_several_rects():
    from clipper.render import _time_expr

    rects = [
        {"start": 1.0, "end": 1.6, "x": 0, "y": 0, "w": 960, "h": 1080},
        {"start": 1.6, "end": 2.2, "x": 960, "y": 0, "w": 960, "h": 1080},
    ]
    expr = _time_expr(rects, 1.0, "x")
    assert expr == "if(lt(t,0.600000),0,960)"


def test_build_filter_complex_shows_hook_only_during_configured_seconds(tmp_path, video_dir):
    from clipper.render import CONFIG_DEFAULTS, _build_filter_complex

    reframe_data = _reframe_json_single_plan()
    hook_path = tmp_path / "hook.txt"
    hook_path.write_text("Attends de voir ca", encoding="utf-8")
    ass_path = video_dir / "subtitles" / f"{CLIP_ID}.ass"

    filt, _label = _build_filter_complex(
        reframe_data, 1.0, 3.5, ass_path, hook_path, None, tmp_path, CONFIG_DEFAULTS
    )

    assert "drawtext=textfile='hook.txt'" in filt
    assert f"enable='lt(t,{CONFIG_DEFAULTS['hook_seconds']})'" in filt
    assert "part.txt" not in filt


def test_build_filter_complex_includes_part_label_when_multipart(tmp_path, video_dir):
    from clipper.render import CONFIG_DEFAULTS, _build_filter_complex

    reframe_data = _reframe_json_single_plan()
    hook_path = tmp_path / "hook.txt"
    hook_path.write_text("Attends", encoding="utf-8")
    part_path = tmp_path / "part.txt"
    part_path.write_text("Part 2/3", encoding="utf-8")
    ass_path = video_dir / "subtitles" / f"{CLIP_ID}.ass"

    filt, _label = _build_filter_complex(
        reframe_data, 1.0, 3.5, ass_path, hook_path, part_path, tmp_path, CONFIG_DEFAULTS
    )

    assert "drawtext=textfile='part.txt'" in filt


def test_build_filter_complex_normalizes_loudness_to_configured_lufs(tmp_path, video_dir):
    from clipper.render import CONFIG_DEFAULTS, _build_filter_complex

    reframe_data = _reframe_json_single_plan()
    hook_path = tmp_path / "hook.txt"
    hook_path.write_text("x", encoding="utf-8")
    ass_path = video_dir / "subtitles" / f"{CLIP_ID}.ass"

    filt, _label = _build_filter_complex(
        reframe_data, 1.0, 3.5, ass_path, hook_path, None, tmp_path, CONFIG_DEFAULTS
    )

    assert "loudnorm=I=-14.0" in filt


def test_filter_path_is_relative_when_target_and_cwd_share_a_drive(video_dir):
    import os

    from clipper.render import _filter_path

    ass_path = video_dir / "subtitles" / f"{CLIP_ID}.ass"
    scratch = video_dir / "render" / CLIP_ID  # meme arborescence, meme lecteur
    result = _filter_path(ass_path, scratch)
    assert ":" not in result
    assert result == Path(os.path.relpath(ass_path, scratch)).as_posix()


def test_filter_path_escapes_the_drive_colon_when_relpath_is_impossible(tmp_path, monkeypatch):
    from clipper.render import FONTS_DIR, _filter_path

    def _raise(*_args, **_kwargs):
        raise ValueError("cross-device")

    monkeypatch.setattr("clipper.render.os.path.relpath", _raise)
    fonts_result = _filter_path(FONTS_DIR, tmp_path)
    assert fonts_result == FONTS_DIR.resolve().as_posix().replace(":", "\\:")
    assert fonts_result.endswith("clipper/assets/fonts")


def test_build_filter_complex_references_ass_via_fontsdir_option(tmp_path, video_dir):
    from clipper.render import CONFIG_DEFAULTS, _build_filter_complex

    reframe_data = _reframe_json_single_plan()
    hook_path = tmp_path / "hook.txt"
    hook_path.write_text("x", encoding="utf-8")
    ass_path = video_dir / "subtitles" / f"{CLIP_ID}.ass"

    filt, _label = _build_filter_complex(
        reframe_data, 1.0, 3.5, ass_path, hook_path, None, tmp_path, CONFIG_DEFAULTS
    )

    assert "ass=" in filt
    assert "fontsdir=" in filt
    assert "assets/fonts" in filt


def test_panel_filters_applies_boxblur_for_blur_effect(tmp_path):
    from clipper.render import CONFIG_DEFAULTS, _panel_filters

    panel = {
        "name": "background", "effect": "blur", "dest": {"x": 0, "y": 0, "w": OUT_W, "h": OUT_H},
        "rects": [{"start": 1.0, "end": 3.5, "x": 0, "y": 0, "w": SRC_W, "h": SRC_H}],
    }
    lines, _scaled, _dest = _panel_filters(panel, "base", 1.0, "lbl", CONFIG_DEFAULTS)
    assert any("boxblur" in line for line in lines)


def test_panel_filters_has_no_boxblur_without_effect(tmp_path):
    from clipper.render import CONFIG_DEFAULTS, _panel_filters

    panel = {
        "name": "main", "dest": {"x": 0, "y": 0, "w": OUT_W, "h": OUT_H},
        "rects": [{"start": 1.0, "end": 3.5, "x": 656, "y": 0, "w": CROP_W, "h": SRC_H}],
    }
    lines, _scaled, _dest = _panel_filters(panel, "base", 1.0, "lbl", CONFIG_DEFAULTS)
    assert not any("boxblur" in line for line in lines)


def test_panel_filters_downscales_before_boxblur_then_upscales_to_dest(tmp_path):
    """Constat essai reel 2026-09-25 : le boxblur plein cadre est le cout
    dominant du fallback_blur. Le flou est calcule a resolution reduite
    (facteur CONFIG_DEFAULTS['blur_downscale']) puis la sortie est remise a
    la taille de dest, pour un flou beaucoup moins couteux sans changer la
    taille finale du panneau."""
    from clipper.render import CONFIG_DEFAULTS, _panel_filters

    panel = {
        "name": "background", "effect": "blur", "dest": {"x": 0, "y": 0, "w": OUT_W, "h": OUT_H},
        "rects": [{"start": 1.0, "end": 3.5, "x": 0, "y": 0, "w": SRC_W, "h": SRC_H}],
    }
    lines, scaled_label, dest = _panel_filters(panel, "base", 1.0, "lbl", CONFIG_DEFAULTS)

    factor = CONFIG_DEFAULTS["blur_downscale"]
    reduce_idx = next(i for i, line in enumerate(lines) if f"scale=iw/{factor}:ih/{factor}" in line)
    blur_idx = next(i for i, line in enumerate(lines) if "boxblur" in line)
    final_idx = next(i for i, line in enumerate(lines) if f"scale={dest['w']}:{dest['h']}" in line)
    assert reduce_idx < blur_idx < final_idx
    assert scaled_label.endswith("s")


def test_panel_filters_blur_downscale_factor_is_configurable(tmp_path):
    from clipper.render import CONFIG_DEFAULTS, _panel_filters

    panel = {
        "name": "background", "effect": "blur", "dest": {"x": 0, "y": 0, "w": OUT_W, "h": OUT_H},
        "rects": [{"start": 1.0, "end": 3.5, "x": 0, "y": 0, "w": SRC_W, "h": SRC_H}],
    }
    settings = {**CONFIG_DEFAULTS, "blur_downscale": 2}
    lines, _scaled, _dest = _panel_filters(panel, "base", 1.0, "lbl", settings)
    assert any("scale=iw/2:ih/2" in line for line in lines)


def test_plan_filters_splits_source_once_per_panel(tmp_path):
    from clipper.render import CONFIG_DEFAULTS, _plan_filters

    plan = _reframe_json_two_plans()["plans"][0]  # facecam_gameplay : 2 panneaux
    lines, _final = _plan_filters(plan, 0, OUT_W, OUT_H, CONFIG_DEFAULTS)
    assert any(line.startswith("[p0base]split=2") for line in lines)


# --------------------------------------------------------------------------
# Config (ADR-b16b)
# --------------------------------------------------------------------------


def test_config_section_render_resolves_via_clipper_config(tmp_path):
    from clipper.config import load_config

    (tmp_path / "config.toml").write_text("[render]\nmax_fps = 24\n", encoding="utf-8")

    config = load_config(tmp_path / "config.toml")

    section = config.section("render")
    assert section["max_fps"] == 24
    assert section["crf"] == 20


def test_config_defaults_declares_expected_settings():
    from clipper.render import CONFIG_DEFAULTS

    for key in ("max_fps", "crf", "loudnorm_i", "hook_seconds", "blur_radius"):
        assert key in CONFIG_DEFAULTS


# --------------------------------------------------------------------------
# Format letterbox (SPEC-6127, TASK-b7f4) : titre d'ecran sur encadre blanc,
# « Partie N » dessous, pas d'accroche de 2 s, sidecar avec video_rect.
# --------------------------------------------------------------------------

TITLE_ZONE = {"x0": 150, "y0": 160, "x1": 930, "y1": 424}
SUBTITLES_ZONE = {"x0": 150, "y0": 1246, "x1": 930, "y1": 1448}
PART_ZONE = {"x0": 150, "y0": 1464, "x1": 930, "y1": 1520}
VIDEO_RECT = {"x": 0, "y": 440, "w": 1080, "h": 790}


def _reframe_json_letterbox():
    """Plan letterbox tel que l'ecrit reframe (contrat commun SPEC-6127, valeurs
    par defaut pour une source 1920x1080)."""
    panels = [
        _panel("background", 0, 0, SRC_W, SRC_H, {"x": 0, "y": 0, "w": OUT_W, "h": OUT_H}, effect="blur"),
        _panel("main", 222, 0, 1476, SRC_H, dict(VIDEO_RECT)),
    ]
    plan = {
        "index": 0, "start": 1.0, "end": 3.5, "image": None, "llm": None, "layout": "letterbox",
        "reason": None, "faces": [], "panels": panels,
    }
    return {
        "video_id": VIDEO_ID, "clip_id": CLIP_ID, "start": 1.0, "end": 3.5,
        "source": {"width": SRC_W, "height": SRC_H}, "output": {"width": OUT_W, "height": OUT_H},
        "layout": "letterbox", "format": "letterbox",
        "text_zones": {"title": dict(TITLE_ZONE), "subtitles": dict(SUBTITLES_ZONE), "part": dict(PART_ZONE)},
        "plans": [plan],
    }


def _emoji_font_available():
    from clipper.render import CONFIG_DEFAULTS, RenderError, resolve_emoji_font

    try:
        resolve_emoji_font(CONFIG_DEFAULTS)
    except RenderError:
        return False
    return True


no_emoji_font = pytest.mark.skipif(not _emoji_font_available(), reason="police emoji couleur absente")


def _assert_box_inside(box, zone):
    x0, y0, x1, y1 = box
    assert zone["x0"] <= x0 < x1 <= zone["x1"], (box, zone)
    assert zone["y0"] <= y0 < y1 <= zone["y1"], (box, zone)


@pytest.fixture
def letterbox_dir(video_dir):
    (video_dir / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(_reframe_json_letterbox()), encoding="utf-8")
    return video_dir


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Remplace l'execution de ffmpeg : note la commande et le contenu du
    dossier de travail, ecrit un mp4 factice, pour verifier entrees et sidecar
    sans encoder."""
    calls = []

    def run(cmd, cwd, out_path):
        calls.append({"cmd": list(cmd), "scratch": sorted(p.name for p in Path(cwd).iterdir())})
        Path(out_path).write_bytes(b"mp4")

    monkeypatch.setattr("clipper.render._exec_ffmpeg", run)
    return calls


# --- (1) titre : mise en page mesuree avec la vraie police ---------------------


def test_short_title_fits_on_one_line_centered_at_the_bottom_of_the_title_zone():
    from clipper.render import CONFIG_DEFAULTS, layout_title

    lay = layout_title("Il m'a menti", TITLE_ZONE, CONFIG_DEFAULTS)

    assert lay.lines == ["Il m'a menti"]
    assert lay.font_size == CONFIG_DEFAULTS["title_font_size"]
    _assert_box_inside(lay.box, TITLE_ZONE)
    x0, _y0, x1, y1 = lay.box
    assert y1 == TITLE_ZONE["y1"] - CONFIG_DEFAULTS["title_lift"]
    assert abs((x0 + x1) / 2 - (TITLE_ZONE["x0"] + TITLE_ZONE["x1"]) / 2) <= 1


def test_title_lift_default_is_40():
    from clipper.render import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["title_lift"] == 40


def test_title_lift_reduces_the_effective_zone_height_forcing_a_smaller_font_size():
    from clipper.render import CONFIG_DEFAULTS, layout_title

    # A 64 px, l'encadre d'une ligne fait 112 px de haut (80 + 2x16) : cette
    # zone n'en offre que 111 une fois title_lift (40) deduit de ses 151 px.
    zone = {"x0": 150, "y0": 0, "x1": 930, "y1": 151}
    lay = layout_title("Il m'a menti", zone, CONFIG_DEFAULTS)

    assert lay.font_size < CONFIG_DEFAULTS["title_font_size"]
    _assert_box_inside(lay.box, zone)
    _x0, _y0, _x1, y1 = lay.box
    assert y1 == zone["y1"] - CONFIG_DEFAULTS["title_lift"]


def test_title_lift_can_turn_a_title_that_would_fit_into_a_render_error():
    from clipper.render import CONFIG_DEFAULTS, RenderError, layout_title

    # A la taille minimale (36 px), l'encadre d'une ligne fait 77 px de haut
    # (45 + 2x16) : sans title_lift, 116 px de zone suffiraient largement ;
    # avec title_lift (40), il n'en reste que 76.
    zone = {"x0": 150, "y0": 0, "x1": 930, "y1": 116}
    with pytest.raises(RenderError, match="titre"):
        layout_title("Il m'a menti", zone, CONFIG_DEFAULTS)


def test_negative_title_lift_is_an_explicit_error():
    from clipper.render import CONFIG_DEFAULTS, RenderError, layout_title

    settings = {**CONFIG_DEFAULTS, "title_lift": -1}
    with pytest.raises(RenderError, match="title_lift"):
        layout_title("Il m'a menti", TITLE_ZONE, settings)


def test_six_long_words_wrap_on_two_lines_and_the_box_stays_in_the_zone():
    from clipper.render import CONFIG_DEFAULTS, layout_title

    title = "Pourquoi personne ne comprend vraiment cette histoire"
    lay = layout_title(title, TITLE_ZONE, CONFIG_DEFAULTS)

    assert len(lay.lines) == 2
    assert " ".join(lay.lines) == title
    _assert_box_inside(lay.box, TITLE_ZONE)
    assert lay.font_size >= CONFIG_DEFAULTS["title_font_size_min"]


def test_title_that_cannot_fit_at_minimum_size_is_an_explicit_error():
    from clipper.render import CONFIG_DEFAULTS, RenderError, layout_title

    title = " ".join(["Anticonstitutionnellement"] * 6)
    with pytest.raises(RenderError, match="titre"):
        layout_title(title, TITLE_ZONE, CONFIG_DEFAULTS)


def test_title_size_steps_down_until_the_box_fits():
    from clipper.render import CONFIG_DEFAULTS, layout_title

    zone = {"x0": 290, "y0": 160, "x1": 790, "y1": 424}  # 500 px de large
    lay = layout_title("Il m'a menti en garde à vue", zone, CONFIG_DEFAULTS)

    assert lay.font_size < CONFIG_DEFAULTS["title_font_size"]
    _assert_box_inside(lay.box, zone)


def test_title_character_missing_from_poppins_is_an_explicit_error():
    from clipper.render import CONFIG_DEFAULTS, RenderError, layout_title

    with pytest.raises(RenderError, match="Poppins"):
        layout_title("Titre 漢字", TITLE_ZONE, CONFIG_DEFAULTS)


def test_title_is_split_into_text_and_emoji_segments_by_unicode_class():
    from clipper.render import split_segments

    assert split_segments("garde à vue 🚨") == [("text", "garde à vue "), ("emoji", "🚨")]
    assert split_segments("Je t'❤️ fort") == [("text", "Je t'"), ("emoji", "❤️"), ("text", " fort")]
    assert split_segments("Bravo 👍🏽!") == [("text", "Bravo "), ("emoji", "👍🏽"), ("text", "!")]


def test_missing_emoji_font_is_an_explicit_error(tmp_path):
    from clipper.render import CONFIG_DEFAULTS, RenderError, layout_title

    settings = {**CONFIG_DEFAULTS, "emoji_font": str(tmp_path / "absente.ttf")}
    with pytest.raises(RenderError, match="police emoji"):
        layout_title("Il m'a menti 🚨", TITLE_ZONE, settings)


@no_emoji_font
def test_title_png_is_transparent_with_a_colored_emoji_where_the_layout_puts_it(tmp_path):
    from PIL import Image

    from clipper.render import CONFIG_DEFAULTS, title_png

    png = tmp_path / "title.png"
    lay = title_png("Il m'a menti en garde à vue 🚨", TITLE_ZONE, CONFIG_DEFAULTS, png)

    img = Image.open(png)
    assert img.mode == "RGBA"
    assert img.size == (TITLE_ZONE["x1"] - TITLE_ZONE["x0"], TITLE_ZONE["y1"] - TITLE_ZONE["y0"])
    assert img.getpixel((0, 0))[3] == 0  # hors encadre : transparent
    assert len(lay.emoji_boxes) == 1
    _assert_box_inside(lay.emoji_boxes[0], TITLE_ZONE)

    def colored(px):
        r, g, b, a = px
        return a > 200 and max(r, g, b) - min(r, g, b) > 80

    ox, oy = TITLE_ZONE["x0"], TITLE_ZONE["y0"]
    ex0, ey0, ex1, ey1 = (v - o for v, o in zip(lay.emoji_boxes[0], (ox, oy, ox, oy)))
    inside = outside = 0
    for y in range(img.height):
        for x in range(img.width):
            if colored(img.getpixel((x, y))):
                if ex0 <= x < ex1 and ey0 <= y < ey1:
                    inside += 1
                else:
                    outside += 1
    assert inside > 50
    assert outside == 0  # texte noir sur blanc : aucune couleur hors de l'emoji
    # encadre blanc opaque au bord gauche, a mi-hauteur
    bx0, by0, _bx1, by1 = lay.box
    assert img.getpixel((bx0 - ox + 4, (by0 + by1) // 2 - oy)) == (255, 255, 255, 255)


@no_emoji_font
def test_title_emoji_gap_separates_text_and_emoji_and_counts_in_the_box_width():
    from PIL import ImageFont

    from clipper.render import CONFIG_DEFAULTS, FONT_FILE, layout_title

    assert CONFIG_DEFAULTS["title_emoji_gap"] == 0.25
    title = "Il m'a menti 🚨"  # une ligne : l'emoji compte dans la largeur de l'encadre
    lay = layout_title(title, TITLE_ZONE, CONFIG_DEFAULTS)
    assert len(lay.lines) == 1
    flush = layout_title(title, TITLE_ZONE, {**CONFIG_DEFAULTS, "title_emoji_gap": 0})
    assert lay.font_size == flush.font_size == 64
    font = ImageFont.truetype(str(FONT_FILE), 64)

    def gap(layout):
        i = next(k for k, item in enumerate(layout.items) if item[0] == "emoji")
        _kind, text, x, _y = layout.items[i - 1]
        return layout.items[i][2] - (x + font.getlength(text))

    assert gap(lay) == pytest.approx(16, abs=1)  # 0,25 em a 64 px
    assert gap(flush) == pytest.approx(0, abs=1)
    # l'ecart est compte dans la largeur de l'encadre, qui tient toujours dans la zone
    assert (lay.box[2] - lay.box[0]) - (flush.box[2] - flush.box[0]) == pytest.approx(16, abs=1)
    _assert_box_inside(lay.box, TITLE_ZONE)


def test_resolve_emoji_font_uses_the_configured_path_when_it_exists(tmp_path):
    from clipper.render import CONFIG_DEFAULTS, resolve_emoji_font

    font = tmp_path / "emoji.ttf"
    font.write_bytes(b"x")
    assert resolve_emoji_font({**CONFIG_DEFAULTS, "emoji_font": str(font)}) == font


# --- filtre ffmpeg en letterbox --------------------------------------------------


def _letterbox_filter(tmp_path, video_dir, part_path=None):
    from clipper.render import CONFIG_DEFAULTS, _build_filter_complex

    ass_path = video_dir / "subtitles" / f"{CLIP_ID}.ass"
    return _build_filter_complex(
        _reframe_json_letterbox(), 1.0, 3.5, ass_path, None, part_path, tmp_path, CONFIG_DEFAULTS,
        title_input=1,
    )


def test_letterbox_filter_overlays_the_title_png_on_the_title_zone_without_hook(tmp_path, video_dir):
    filt, label = _letterbox_filter(tmp_path, video_dir)

    overlay = next(f for f in filt.split(";") if "[1:v]overlay" in f)
    assert f"[1:v]overlay=x={TITLE_ZONE['x0']}:y={TITLE_ZONE['y0']}" in overlay
    assert "enable=" not in overlay  # tout le clip
    assert "drawtext" not in filt  # ni accroche, ni Partie (clip unique)
    assert "lt(t," not in filt
    assert overlay.endswith(f"[{label}]")


def test_letterbox_filter_centers_partie_in_the_part_zone_when_multipart(tmp_path, video_dir):
    part_path = tmp_path / "part.txt"
    part_path.write_text("Partie 2", encoding="utf-8")

    filt, label = _letterbox_filter(tmp_path, video_dir, part_path=part_path)

    part = next(f for f in filt.split(";") if "part.txt" in f)
    assert "drawtext=textfile='part.txt'" in part
    assert f"x={PART_ZONE['x0']}+({PART_ZONE['x1'] - PART_ZONE['x0']}-text_w)/2" in part
    assert "y_align=baseline" in part
    assert "enable=" not in part  # tout le clip
    assert filt.index("[1:v]overlay") < filt.index("part.txt")
    assert part.endswith(f"[{label}]")
    assert sum("drawtext" in f for f in filt.split(";")) == 1  # pas d'accroche


# --- render en letterbox : entrees, sidecar ------------------------------------


def test_letterbox_render_passes_the_title_png_as_second_input_and_writes_video_rect(
    tmp_path, letterbox_dir, fake_ffmpeg, cpu_device
):
    from clipper.render import render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=make_config())

    cmd = fake_ffmpeg[0]["cmd"]
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert len(inputs) == 2
    assert inputs[1].endswith("title.png")
    assert "title.png" in fake_ffmpeg[0]["scratch"]
    assert "hook.txt" not in fake_ffmpeg[0]["scratch"]
    assert "part.txt" not in fake_ffmpeg[0]["scratch"]

    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["layout"] == "letterbox"
    assert data["video_rect"] == VIDEO_RECT
    assert data["screen_title"] == "Il m'a menti en garde à vue"


def test_letterbox_render_writes_partie_n_when_multipart(tmp_path, letterbox_dir, fake_ffmpeg, cpu_device):
    from clipper.render import render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    (letterbox_dir / "captions.json").write_text(
        json.dumps(_captions_json(part=2, parts_total=3)), encoding="utf-8")

    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=make_config())

    assert "part.txt" in fake_ffmpeg[0]["scratch"]
    filt = fake_ffmpeg[0]["cmd"][fake_ffmpeg[0]["cmd"].index("-filter_complex") + 1]
    assert "drawtext=textfile='part.txt'" in filt


def test_letterbox_partie_text_is_partie_n(tmp_path, letterbox_dir, monkeypatch, cpu_device):
    from clipper.render import render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    (letterbox_dir / "captions.json").write_text(
        json.dumps(_captions_json(part=2, parts_total=3)), encoding="utf-8")
    seen = []

    def run(cmd, cwd, out_path):
        seen.append((Path(cwd) / "part.txt").read_text(encoding="utf-8"))
        Path(out_path).write_bytes(b"mp4")

    monkeypatch.setattr("clipper.render._exec_ffmpeg", run)
    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=make_config())

    assert seen == ["Partie 2"]


def test_letterbox_partie_too_big_for_its_zone_is_an_explicit_error(tmp_path, letterbox_dir, fake_ffmpeg, cpu_device):
    from clipper.render import RenderError, render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    (letterbox_dir / "captions.json").write_text(
        json.dumps(_captions_json(part=2, parts_total=3)), encoding="utf-8")
    with pytest.raises(RenderError, match="Partie"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
               config=make_config(part_font_size=120))
    assert fake_ffmpeg == []


def test_letterbox_without_text_zones_asks_to_rerun_reframe(tmp_path, letterbox_dir, fake_ffmpeg):
    from clipper.render import RenderError, render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    reframe = _reframe_json_letterbox()
    del reframe["text_zones"]
    (letterbox_dir / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(reframe), encoding="utf-8")
    with pytest.raises(RenderError, match="reframe"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
               config=make_config())
    assert fake_ffmpeg == []


@pytest.mark.parametrize("fixture", ["video_dir", "letterbox_dir"])
def test_missing_screen_title_asks_to_rerun_captions_force(tmp_path, fixture, request, fake_ffmpeg):
    from clipper.render import RenderError, render

    d = request.getfixturevalue(fixture)
    (d / f"{VIDEO_ID}.mp4").write_bytes(b"")
    captions = _captions_json()
    del captions["clips"][0]["screen_title"]
    (d / "captions.json").write_text(json.dumps(captions), encoding="utf-8")
    with pytest.raises(RenderError, match="captions --force"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=d.parent, output_dir=tmp_path / "output", config=make_config())
    assert fake_ffmpeg == []


def test_crop_sidecar_also_carries_screen_title(tmp_path, video_dir, fake_ffmpeg, cpu_device):
    from clipper.render import render

    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output", config=make_config())

    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["screen_title"] == "Il m'a menti en garde à vue"
    assert data["layout"] == "single"
    assert "video_rect" not in data
    cmd = fake_ffmpeg[0]["cmd"]
    assert cmd.count("-i") == 1  # hors letterbox : rendu inchange, pas de PNG de titre
    assert "hook.txt" in fake_ffmpeg[0]["scratch"]


# --- rendu ffmpeg reel d'un plan letterbox synthetique ---------------------------


@no_ffmpeg
@no_ffprobe
def test_render_letterbox_real_ffmpeg_draws_the_white_title_box_and_partie(
    tmp_path, letterbox_dir, synthetic_source, cpu_device
):
    from PIL import Image

    from clipper.render import CONFIG_DEFAULTS, layout_title, render

    (letterbox_dir / "captions.json").write_text(
        json.dumps(_captions_json(part=1, parts_total=2)), encoding="utf-8")
    out = render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
                 config=make_config(x264_preset="ultrafast"))

    probe = _ffprobe_json(out)
    streams = {s["codec_type"]: s for s in probe["streams"]}
    assert (streams["video"]["width"], streams["video"]["height"]) == (OUT_W, OUT_H)
    assert float(probe["format"]["duration"]) == pytest.approx(2.5, abs=0.1)

    lay = layout_title("Il m'a menti en garde à vue", TITLE_ZONE, CONFIG_DEFAULTS)
    bx0, by0, _bx1, by1 = lay.box
    for t in (0.2, 2.3):  # debut et fin du clip : titre et Partie tout du long
        frame = tmp_path / f"frame_{t}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(out),
                        "-frames:v", "1", str(frame)], check=True)
        img = Image.open(frame).convert("RGB")
        r, g, b = img.getpixel((bx0 + 6, (by0 + by1) // 2))
        assert min(r, g, b) > 225, (t, (r, g, b))  # bord de l'encadre blanc
        # Partie : du blanc (texte) dans la zone part
        white = sum(
            1 for y in range(PART_ZONE["y0"], PART_ZONE["y1"]) for x in range(PART_ZONE["x0"], PART_ZONE["x1"], 2)
            if min(img.getpixel((x, y))) > 235
        )
        assert white > 30, t


# --------------------------------------------------------------------------
# TASK-032d : ffmpeg se positionne sur le debut du clip (-ss avant -i) au lieu
# de decoder la source depuis 0 ; trim/atrim relatifs a ce point.
# --------------------------------------------------------------------------


def _source_input_index(cmd):
    return next(i for i, a in enumerate(cmd) if a == "-i" and not cmd[i + 1].endswith(".png"))


def test_render_seeks_the_source_input_before_decoding(tmp_path, letterbox_dir, fake_ffmpeg, cpu_device):
    from clipper.render import render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=make_config())

    cmd = fake_ffmpeg[0]["cmd"]
    src = _source_input_index(cmd)
    before_src = cmd[:src]
    assert "-ss" in before_src and "-t" in before_src
    assert float(before_src[before_src.index("-ss") + 1]) == pytest.approx(1.0)
    assert float(before_src[before_src.index("-t") + 1]) == pytest.approx(2.5)
    # le PNG du titre n'est pas decale
    png = next(i for i, a in enumerate(cmd) if a == "-i" and cmd[i + 1].endswith("title.png"))
    assert "-ss" not in cmd[src + 2:png]


def test_render_trims_relative_to_the_seek_point(tmp_path, letterbox_dir, fake_ffmpeg, cpu_device):
    from clipper.render import render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=make_config())

    cmd = fake_ffmpeg[0]["cmd"]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:v]trim=start=0.000000:end=2.500000" in graph
    assert "[0:a]atrim=start=0.000000:end=2.500000" in graph
    assert "start=1.000000" not in graph


def _timed_color_source(path, duration):
    """Source 1920x1080 rouge avant 9 s, bleue de 9 a 11 s, verte apres."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"color=c=red:s={SRC_W}x{SRC_H}:r=25:d=9,format=yuv420p[a];"
         f"color=c=blue:s={SRC_W}x{SRC_H}:r=25:d=2,format=yuv420p[b];"
         f"color=c=green:s={SRC_W}x{SRC_H}:r=25:d={duration - 11},format=yuv420p[c];"
         "[a][b][c]concat=n=3:v=1:a=0",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}",
         "-ac", "2", "-shortest", str(path)],
        check=True,
    )


def _frame_center_rgb(mp4, t):
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(mp4), "-frames:v", "1",
         "-vf", "crop=2:2:539:1189", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, check=True,
    )
    return tuple(proc.stdout[:3])


@no_ffmpeg
@no_ffprobe
def test_real_render_of_a_clip_far_into_the_source_shows_the_right_frames(tmp_path, video_dir, cpu_device):
    from clipper.render import render

    _timed_color_source(video_dir / f"{VIDEO_ID}.mp4", 14)
    (video_dir / "captions.json").write_text(
        json.dumps(_captions_json(start=9.0, end=11.0, duration=2.0)), encoding="utf-8")
    moments = _moments_json()
    moments["moments"][0].update(start=9.0, end=11.0, duration=2.0)
    (video_dir / "moments.json").write_text(json.dumps(moments), encoding="utf-8")
    reframe = _reframe_json_single_plan()
    reframe.update(start=9.0, end=11.0)
    reframe["plans"][0].update(start=9.0, end=11.0)
    for panel in reframe["plans"][0]["panels"]:
        for rect in panel["rects"]:
            rect.update(start=9.0, end=11.0)
    (video_dir / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(reframe), encoding="utf-8")

    out = render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
                 config=make_config())

    assert float(_ffprobe_json(out)["format"]["duration"]) == pytest.approx(2.0, abs=0.1)
    for t in (0.0, 1.0, 1.9):
        r, g, b = _frame_center_rgb(out, t)
        assert b > 200 and r < 60 and g < 60, f"image a t={t} : {(r, g, b)} (bleu attendu)"


# --------------------------------------------------------------------------
# Format stream (SPEC-3a88, TASK-9e0c) : facecam agrandie en haut, jeu en
# bas, titre d'ecran et sous-titres dans leurs zones, hors du visage.
# --------------------------------------------------------------------------

STREAM_TITLE_ZONE = {"x0": 150, "y0": 160, "x1": 930, "y1": 424}
STREAM_SUBTITLES_ZONE = {"x0": 150, "y0": 1224, "x1": 930, "y1": 1448}
CAMERA_RECT = {"x": 0, "y": 440, "w": 1080, "h": 768}
GAMEPLAY_RECT = {"x": 0, "y": 1208, "w": 1080, "h": 712}
# Source 1920x1080 : facecam 526x296 en haut a gauche, le reste est le jeu.
FACECAM = (0, 0, 526, 296)


def _reframe_json_stream():
    """Plan stream tel que l'ecrit reframe : un seul plan fige, fond flou,
    camera (rectangle de la facecam) puis jeu (hors facecam)."""
    panels = [
        _panel("background", 0, 0, SRC_W, SRC_H, {"x": 0, "y": 0, "w": OUT_W, "h": OUT_H}, effect="blur"),
        _panel("camera", 44, 0, 420, 298, dict(CAMERA_RECT)),
        _panel("gameplay", 606, 84, 1314, 866, dict(GAMEPLAY_RECT)),
    ]
    plan = {
        "index": 0, "start": 1.0, "end": 3.5, "image": None, "llm": None, "layout": "stream",
        "reason": None, "faces": [], "panels": panels,
    }
    return {
        "video_id": VIDEO_ID, "clip_id": CLIP_ID, "start": 1.0, "end": 3.5,
        "source": {"width": SRC_W, "height": SRC_H}, "output": {"width": OUT_W, "height": OUT_H},
        "layout": "stream", "format": "letterbox", "layout_mode": "stream_auto", "layout_reason": None,
        "facecam": {"x": 44, "y": 0, "w": 420, "h": 298},
        "text_zones": {"title": dict(STREAM_TITLE_ZONE), "subtitles": dict(STREAM_SUBTITLES_ZONE),
                       "part": dict(PART_ZONE)},
        "plans": [plan],
    }


@pytest.fixture
def stream_dir(video_dir):
    (video_dir / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(_reframe_json_stream()), encoding="utf-8")
    return video_dir


def test_stream_filter_scales_facecam_on_top_and_game_below_with_the_title(tmp_path, video_dir):
    from clipper.render import CONFIG_DEFAULTS, _build_filter_complex

    filt, label = _build_filter_complex(
        _reframe_json_stream(), 1.0, 3.5, video_dir / "subtitles" / f"{CLIP_ID}.ass", None, None, tmp_path,
        CONFIG_DEFAULTS, title_input=1,
    )
    parts = filt.split(";")
    assert "color=c=black:s=1080x1920" in filt
    # camera : rectangle fige de la facecam, agrandi en 1080x768 a y=440
    assert any("crop=w='420':h='298':x='44':y='0'" in f for f in parts)
    assert any(f.endswith("scale=1080:768[p0_1s]") for f in parts)
    assert any("overlay=x=0:y=440" in f for f in parts)
    # jeu : hors facecam, en 1080x712 sous la camera
    assert any("crop=w='1314':h='866':x='606':y='84'" in f for f in parts)
    assert any(f.endswith("scale=1080:712[p0_2s]") for f in parts)
    assert any("overlay=x=0:y=1208" in f for f in parts)
    assert "lt(t," not in filt  # aucun suivi : rectangles constants
    # titre d'ecran sur toute la duree, pas d'accroche
    assert f"[1:v]overlay=x={STREAM_TITLE_ZONE['x0']}:y={STREAM_TITLE_ZONE['y0']}" in filt
    assert "drawtext" not in filt


def test_stream_render_writes_layout_stream_and_both_rects_in_the_sidecar(
    tmp_path, stream_dir, fake_ffmpeg, cpu_device
):
    from clipper.render import render

    (stream_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=stream_dir.parent, output_dir=tmp_path / "output",
           config=make_config())

    cmd = fake_ffmpeg[0]["cmd"]
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert inputs[1].endswith("title.png")
    assert "hook.txt" not in fake_ffmpeg[0]["scratch"]
    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["layout"] == "stream"
    assert data["camera_rect"] == CAMERA_RECT
    assert data["video_rect"] == GAMEPLAY_RECT
    assert data["screen_title"] == "Il m'a menti en garde à vue"


@pytest.mark.parametrize("missing", ["camera", "gameplay"])
def test_stream_without_camera_or_gameplay_panel_asks_to_rerun_reframe(tmp_path, stream_dir, fake_ffmpeg, missing):
    from clipper.render import RenderError, render

    (stream_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    reframe = _reframe_json_stream()
    reframe["plans"][0]["panels"] = [p for p in reframe["plans"][0]["panels"] if p["name"] != missing]
    (stream_dir / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(reframe), encoding="utf-8")
    with pytest.raises(RenderError, match=missing):
        render(VIDEO_ID, CLIP_ID, workspace_dir=stream_dir.parent, output_dir=tmp_path / "output",
               config=make_config())
    assert fake_ffmpeg == []


@no_ffmpeg
@no_ffprobe
def test_render_stream_real_ffmpeg_gives_1080x1920_with_facecam_on_top_and_game_below(
    tmp_path, stream_dir, cpu_device
):
    from PIL import Image

    from clipper.render import render

    x0, y0, x1, y1 = FACECAM
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"color=c=green:s={SRC_W}x{SRC_H}:r=25:d=5,drawbox=x={x0}:y={y0}:w={x1 - x0}:h={y1 - y0}:color=red:t=fill",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=5",
         "-ac", "2", "-shortest", str(stream_dir / f"{VIDEO_ID}.mp4")],
        check=True,
    )
    out = render(VIDEO_ID, CLIP_ID, workspace_dir=stream_dir.parent, output_dir=tmp_path / "output",
                 config=make_config(x264_preset="ultrafast"))

    probe = _ffprobe_json(out)
    streams = {s["codec_type"]: s for s in probe["streams"]}
    assert (streams["video"]["width"], streams["video"]["height"]) == (OUT_W, OUT_H)
    assert float(probe["format"]["duration"]) == pytest.approx(2.5, abs=0.1)

    frame = tmp_path / "frame.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2.0", "-i", str(out),
                    "-frames:v", "1", str(frame)], check=True)
    img = Image.open(frame).convert("RGB")

    def is_red(p):
        return p[0] > 180 and p[1] < 80 and p[2] < 80

    def is_green(p):
        return p[1] > 90 and p[0] < 80 and p[2] < 80

    # zone haute : la facecam (rouge) agrandie sur toute la largeur
    for x in (100, 540, 980):
        for y in (CAMERA_RECT["y"] + 60, CAMERA_RECT["y"] + 384, CAMERA_RECT["y"] + 700):
            assert is_red(img.getpixel((x, y))), (x, y, img.getpixel((x, y)))
    # zone basse : le jeu (vert), sans rien de la facecam
    for x in (100, 540, 980):
        for y in (GAMEPLAY_RECT["y"] + 60, GAMEPLAY_RECT["y"] + 356, GAMEPLAY_RECT["y"] + 650):
            assert is_green(img.getpixel((x, y))), (x, y, img.getpixel((x, y)))
    # titre d'ecran : encadre blanc au-dessus de la camera
    from clipper.render import CONFIG_DEFAULTS, layout_title

    bx0, by0, _bx1, by1 = layout_title("Il m'a menti en garde à vue", STREAM_TITLE_ZONE, CONFIG_DEFAULTS).box
    assert by1 <= CAMERA_RECT["y"]
    assert min(img.getpixel((bx0 + 6, (by0 + by1) // 2))) > 225


# --------------------------------------------------------------------------
# Appel a l'abonnement (SPEC-6a47) : pseudo de chaine sous le titre + carte
# de fin, desactive par defaut. Letterbox et stream seulement (crop reste
# fige) ; jamais de CTA a moitie active (ADR-ad2e).
# --------------------------------------------------------------------------


def _cta_config(**overrides):
    settings = {"cta_enabled": True, "cta_handle": "twitch.tv/exemple", "cta_seconds": 1.0}
    settings.update(overrides)
    return make_config(**settings)


# --- (0) config : desactive par defaut ------------------------------------


def test_cta_config_defaults_are_present_and_disabled():
    from clipper.render import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["cta_enabled"] is False
    assert CONFIG_DEFAULTS["cta_handle"] == ""
    assert CONFIG_DEFAULTS["cta_text"] == "Abonne-toi !"
    assert CONFIG_DEFAULTS["cta_seconds"] == 2.0
    for key in ("cta_handle_font_size", "cta_handle_font_size_min", "cta_handle_gap",
                "cta_card_font_size", "cta_card_font_size_min", "cta_card_radius"):
        assert key in CONFIG_DEFAULTS


def test_cta_field_is_false_by_default_in_the_sidecar(tmp_path, letterbox_dir, fake_ffmpeg, cpu_device):
    from clipper.render import render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=make_config())

    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["cta"] is False
    cmd = fake_ffmpeg[0]["cmd"]
    assert cmd.count("-i") == 2  # source + title.png : rendu inchange
    assert "cta_card.png" not in fake_ffmpeg[0]["scratch"]
    assert "gte(t," not in cmd[cmd.index("-filter_complex") + 1]


# --- (1) validations explicites, jamais de CTA a moitie active -------------


def test_cta_enabled_without_handle_is_an_explicit_error(tmp_path, video_dir, fake_ffmpeg):
    from clipper.render import RenderError, render

    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="cta_handle"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
               config=make_config(cta_enabled=True))
    assert fake_ffmpeg == []


@pytest.mark.parametrize("cta_seconds", [0, -1.0])
def test_cta_seconds_not_positive_is_an_explicit_error(tmp_path, video_dir, fake_ffmpeg, cta_seconds):
    from clipper.render import RenderError, render

    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="cta_seconds"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
               config=_cta_config(cta_seconds=cta_seconds))
    assert fake_ffmpeg == []


def test_cta_text_empty_on_a_letterbox_clip_is_an_explicit_error(tmp_path, letterbox_dir, fake_ffmpeg, cpu_device):
    from clipper.render import RenderError, render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="cta_text"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
               config=_cta_config(cta_text=""))
    assert fake_ffmpeg == []


def test_cta_seconds_at_least_clip_duration_is_an_explicit_error(tmp_path, letterbox_dir, fake_ffmpeg, cpu_device):
    from clipper.render import RenderError, render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="cta_seconds"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
               config=_cta_config(cta_seconds=2.5))  # == duration du clip (2.5 s)
    assert fake_ffmpeg == []


# --- (2) crop : le format reste fige, cta ignore sans erreur ---------------


def test_cta_enabled_on_a_non_text_layout_is_not_applied(tmp_path, video_dir, fake_ffmpeg, cpu_device):
    from clipper.render import render

    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output",
           config=_cta_config())

    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["cta"] is False
    assert "cta_card.png" not in fake_ffmpeg[0]["scratch"]


# --- (3) pseudo de chaine : mesure avec la vraie police ---------------------


def test_layout_pseudo_fits_a_short_handle_at_the_configured_size():
    from clipper.render import CONFIG_DEFAULTS, layout_pseudo

    lay = layout_pseudo("twitch.tv/exemple", 780, CONFIG_DEFAULTS)

    assert lay.font_size == CONFIG_DEFAULTS["cta_handle_font_size"]
    assert 0 < lay.width <= 780
    assert lay.height > 0


def test_layout_pseudo_steps_down_the_font_size_for_a_narrow_zone():
    from clipper.render import CONFIG_DEFAULTS, layout_pseudo

    lay = layout_pseudo("twitch.tv/exemple", 220, CONFIG_DEFAULTS)

    assert lay.font_size < CONFIG_DEFAULTS["cta_handle_font_size"]
    assert lay.width <= 220


def test_layout_pseudo_too_long_even_at_minimum_size_is_an_explicit_error():
    from clipper.render import CONFIG_DEFAULTS, RenderError, layout_pseudo

    with pytest.raises(RenderError, match="pseudo"):
        layout_pseudo("twitch.tv/" + "exemple" * 20, 100, CONFIG_DEFAULTS)


def test_layout_pseudo_empty_is_an_explicit_error():
    from clipper.render import CONFIG_DEFAULTS, RenderError, layout_pseudo

    with pytest.raises(RenderError, match="vide"):
        layout_pseudo("   ", 780, CONFIG_DEFAULTS)


# --- (4) titre remonte pour laisser la place au pseudo ---------------------


def test_screen_title_box_moves_up_to_make_room_for_the_pseudo_when_cta_applies(
    tmp_path, letterbox_dir, cpu_device
):
    """Le PNG du titre est produit avec un title_lift effectif plus grand
    quand le CTA s'applique : son encadre remonte, laissant un espace libre
    sous lui pour le pseudo, sans jamais deborder de la zone (ADR-ad2e)."""
    from clipper.render import CONFIG_DEFAULTS, layout_title

    zone_w = TITLE_ZONE["x1"] - TITLE_ZONE["x0"]
    from clipper.render import layout_pseudo

    pseudo = layout_pseudo("twitch.tv/exemple", zone_w, CONFIG_DEFAULTS)
    reserved = int(CONFIG_DEFAULTS["cta_handle_gap"]) + pseudo.height
    effective = {**CONFIG_DEFAULTS, "title_lift": int(CONFIG_DEFAULTS["title_lift"]) + reserved}

    plain = layout_title("Il m'a menti en garde à vue", TITLE_ZONE, CONFIG_DEFAULTS)
    lifted = layout_title("Il m'a menti en garde à vue", TITLE_ZONE, effective)

    assert lifted.box[3] < plain.box[3]  # l'encadre remonte (moins de by1)
    _assert_box_inside(lifted.box, TITLE_ZONE)
    # l'espace libere est bien celui reserve pour le pseudo
    assert plain.box[3] - lifted.box[3] == reserved


def test_render_draws_the_pseudo_handle_under_the_title_box(tmp_path, letterbox_dir, monkeypatch, cpu_device):
    from PIL import Image

    from clipper.render import render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    seen = {}

    def run(cmd, cwd, out_path):
        seen["png"] = Image.open(Path(cwd) / "title.png").convert("RGBA").copy()
        Path(out_path).write_bytes(b"mp4")

    monkeypatch.setattr("clipper.render._exec_ffmpeg", run)
    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=_cta_config())

    img = seen["png"]
    # sous l'encadre blanc du titre (remonte), une ligne de pixels non
    # transparents (le pseudo, dessine avec un contour noir sur fond blanc).
    from clipper.render import CONFIG_DEFAULTS, layout_pseudo, layout_title

    zone_w = TITLE_ZONE["x1"] - TITLE_ZONE["x0"]
    pseudo = layout_pseudo("twitch.tv/exemple", zone_w, CONFIG_DEFAULTS)
    reserved = int(CONFIG_DEFAULTS["cta_handle_gap"]) + pseudo.height
    effective = {**CONFIG_DEFAULTS, "title_lift": int(CONFIG_DEFAULTS["title_lift"]) + reserved}
    title_layout = layout_title("Il m'a menti en garde à vue", TITLE_ZONE, effective)

    ox, oy = TITLE_ZONE["x0"], TITLE_ZONE["y0"]
    y_row = title_layout.box[3] - oy + int(CONFIG_DEFAULTS["cta_handle_gap"]) + pseudo.height // 2
    opaque = sum(1 for x in range(img.width) if img.getpixel((x, y_row))[3] > 0)
    assert opaque > 5, "aucun pixel du pseudo dessine sous l'encadre du titre"
    _assert_box_inside(title_layout.box, TITLE_ZONE)  # le titre remonte reste dans la zone


# --- Éditeur d'agencement letterbox (TASK-3be3) : réglages par style --------


def _render_title_png(tmp_path, letterbox_dir, monkeypatch, config, reframe=None):
    from PIL import Image

    from clipper.render import render

    if reframe is not None:
        (letterbox_dir / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(reframe), encoding="utf-8")
    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    seen = {}

    def run(cmd, cwd, out_path):
        seen["png"] = Image.open(Path(cwd) / "title.png").convert("RGBA").copy()
        seen["cmd"] = list(cmd)
        Path(out_path).write_bytes(b"mp4")

    monkeypatch.setattr("clipper.render._exec_ffmpeg", run)
    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=config, force=True)
    return seen


def _lowest_white_row(img):
    """Bas de l'encadre blanc du titre : derniere ligne largement blanche
    (le pseudo, blanc lui aussi, est bien plus etroit)."""
    white = (255, 255, 255, 255)
    rows = [y for y in range(img.height) if sum(img.getpixel((x, y)) == white for x in range(img.width)) > 400]
    return max(rows)


def test_default_letterbox_settings_keep_the_same_zones_and_ffmpeg_filters(tmp_path, video_dir):
    """Sans réglage de style ({} pour les zones de texte), reframe calcule les
    mêmes zones qu'avant l'éditeur, donc le même filtergraph."""
    from clipper import reframe
    from clipper.config import Config

    settings = reframe._settings(Config(mode="review", workspace_dir="w", output_dir="o", _sections={}))
    zones = reframe._letterbox_geometry(1920, 1080, settings)["zones"]
    assert zones == {"title": (150, 160, 930, 424), "subtitles": (150, 1246, 930, 1448), "part": (150, 1464, 930, 1520)}

    part_path = tmp_path / "part.txt"
    part_path.write_text("Partie 2", encoding="utf-8")
    filt, _label = _letterbox_filter(tmp_path, video_dir, part_path=part_path)
    assert "[vsub][1:v]overlay=x=150:y=160:eof_action=repeat[vtitle]" in filt
    assert "x=150+(780-text_w)/2" in filt


def test_render_places_the_title_in_the_title_zone_chosen_by_the_style(
    tmp_path, letterbox_dir, monkeypatch, cpu_device
):
    reframe = _reframe_json_letterbox()
    reframe["text_zones"]["title"] = {"x0": 200, "y0": 240, "x1": 880, "y1": 400}

    seen = _render_title_png(tmp_path, letterbox_dir, monkeypatch, make_config(), reframe)

    assert seen["png"].size == (680, 160)
    graph = seen["cmd"][seen["cmd"].index("-filter_complex") + 1]
    assert "[1:v]overlay=x=200:y=240:" in graph
    # encadre colle en bas de la zone, a title_lift (40) du bas
    assert _lowest_white_row(seen["png"]) == 160 - 40 - 1


def test_render_cta_handle_gap_of_the_style_moves_the_title_box_up(
    tmp_path, letterbox_dir, monkeypatch, cpu_device
):
    default = _render_title_png(tmp_path, letterbox_dir, monkeypatch, _cta_config())
    wider = _render_title_png(tmp_path, letterbox_dir, monkeypatch, _cta_config(cta_handle_gap=30))

    assert _lowest_white_row(default["png"]) - _lowest_white_row(wider["png"]) == 22


def test_negative_cta_handle_gap_is_an_explicit_error(tmp_path, letterbox_dir, fake_ffmpeg, cpu_device):
    from clipper.render import RenderError, render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="cta_handle_gap"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
               config=_cta_config(cta_handle_gap=-4))


# --- (5) carte de fin : encadre centre, mesure avec la vraie police --------


def test_layout_cta_card_centers_a_short_text_in_its_zone():
    from clipper.render import CONFIG_DEFAULTS, layout_cta_card

    lay = layout_cta_card("Abonne-toi !", SUBTITLES_ZONE, CONFIG_DEFAULTS)

    assert lay.lines == ["Abonne-toi !"]
    _assert_box_inside(lay.box, SUBTITLES_ZONE)
    x0, y0, x1, y1 = lay.box
    zx0, zy0, zx1, zy1 = SUBTITLES_ZONE["x0"], SUBTITLES_ZONE["y0"], SUBTITLES_ZONE["x1"], SUBTITLES_ZONE["y1"]
    assert abs((x0 + x1) / 2 - (zx0 + zx1) / 2) <= 1
    assert abs((y0 + y1) / 2 - (zy0 + zy1) / 2) <= 1  # centre verticalement (pas ancre en bas)


def test_layout_cta_card_wraps_two_lines_when_needed():
    from clipper.render import CONFIG_DEFAULTS, layout_cta_card

    zone = {"x0": 150, "y0": 1246, "x1": 500, "y1": 1448}  # 350 px de large
    lay = layout_cta_card("Abonne-toi vite maintenant", zone, CONFIG_DEFAULTS)

    assert len(lay.lines) == 2
    _assert_box_inside(lay.box, zone)


def test_layout_cta_card_too_long_even_at_minimum_size_is_an_explicit_error():
    from clipper.render import CONFIG_DEFAULTS, RenderError, layout_cta_card

    text = " ".join(["Anticonstitutionnellement"] * 6)
    with pytest.raises(RenderError, match="carte de fin"):
        layout_cta_card(text, SUBTITLES_ZONE, CONFIG_DEFAULTS)


def test_cta_card_png_is_transparent_with_a_white_box_and_black_text(tmp_path):
    from PIL import Image

    from clipper.render import CONFIG_DEFAULTS, cta_card_png

    png = tmp_path / "cta_card.png"
    lay = cta_card_png("Abonne-toi !", SUBTITLES_ZONE, CONFIG_DEFAULTS, png)

    img = Image.open(png)
    assert img.mode == "RGBA"
    assert img.size == (SUBTITLES_ZONE["x1"] - SUBTITLES_ZONE["x0"], SUBTITLES_ZONE["y1"] - SUBTITLES_ZONE["y0"])
    assert img.getpixel((0, 0))[3] == 0  # hors encadre : transparent
    ox, oy = SUBTITLES_ZONE["x0"], SUBTITLES_ZONE["y0"]
    bx0, by0, bx1, by1 = lay.box
    assert img.getpixel((bx0 - ox + 4, (by0 + by1) // 2 - oy)) == (255, 255, 255, 255)


# --- (6) sous-titres tronques pendant la carte de fin -----------------------


def test_truncate_ass_for_cta_shortens_an_event_that_overlaps_the_cutoff():
    from clipper.render import truncate_ass_for_cta

    ass = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,Bonjour\n"
    )
    out = truncate_ass_for_cta(ass, cutoff=1.5)
    assert "0:00:00.50,0:00:01.50" in out
    assert "Bonjour" in out


def test_truncate_ass_for_cta_drops_an_event_that_starts_after_the_cutoff():
    from clipper.render import truncate_ass_for_cta

    ass = (
        "[Events]\n"
        "Dialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,Trop tard\n"
    )
    out = truncate_ass_for_cta(ass, cutoff=1.5)
    assert "Trop tard" not in out


def test_truncate_ass_for_cta_keeps_an_event_entirely_before_the_cutoff_unchanged():
    from clipper.render import truncate_ass_for_cta

    line = "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Salut"
    out = truncate_ass_for_cta(f"[Events]\n{line}\n", cutoff=1.5)
    assert line in out


def test_truncate_ass_for_cta_preserves_non_dialogue_lines():
    from clipper.render import truncate_ass_for_cta

    ass = "[Script Info]\nScriptType: v4.00+\n\n[Events]\nDialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,x\n"
    out = truncate_ass_for_cta(ass, cutoff=1.0)
    assert "[Script Info]" in out
    assert "ScriptType: v4.00+" in out


# --- (7) filtre ffmpeg : carte de fin par-dessus les sous-titres -----------


def test_build_filter_complex_overlays_the_cta_card_after_the_cutoff(tmp_path, video_dir):
    from clipper.render import CONFIG_DEFAULTS, _build_filter_complex

    ass_path = video_dir / "subtitles" / f"{CLIP_ID}.ass"
    filt, label = _build_filter_complex(
        _reframe_json_letterbox(), 1.0, 3.5, ass_path, None, None, tmp_path, CONFIG_DEFAULTS,
        title_input=1, cta_input=2, cta_start=1.5,
    )

    overlay = next(f for f in filt.split(";") if "[2:v]overlay" in f)
    assert f"[2:v]overlay=x={SUBTITLES_ZONE['x0']}:y={SUBTITLES_ZONE['y0']}" in overlay
    assert "enable='gte(t,1.500000)'" in overlay
    assert overlay.endswith(f"[{label}]")


def test_build_filter_complex_without_cta_input_has_no_overlay_of_the_card(tmp_path, video_dir):
    from clipper.render import CONFIG_DEFAULTS, RenderError, _build_filter_complex

    ass_path = video_dir / "subtitles" / f"{CLIP_ID}.ass"
    filt, _label = _build_filter_complex(
        _reframe_json_letterbox(), 1.0, 3.5, ass_path, None, None, tmp_path, CONFIG_DEFAULTS, title_input=1,
    )
    assert "gte(t," not in filt

    with pytest.raises(RenderError, match="cta_start"):
        _build_filter_complex(
            _reframe_json_letterbox(), 1.0, 3.5, ass_path, None, None, tmp_path, CONFIG_DEFAULTS,
            title_input=1, cta_input=2,
        )


# --- (8) render() integration : letterbox et stream -------------------------


def test_render_letterbox_with_cta_writes_three_inputs_truncated_ass_and_cta_true(
    tmp_path, letterbox_dir, fake_ffmpeg, cpu_device
):
    from clipper.render import render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=_cta_config())

    cmd = fake_ffmpeg[0]["cmd"]
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert len(inputs) == 3
    assert inputs[1].endswith("title.png")
    assert inputs[2].endswith("cta_card.png")
    assert "cta_card.png" in fake_ffmpeg[0]["scratch"]

    filt = cmd[cmd.index("-filter_complex") + 1]
    assert "[2:v]overlay" in filt
    # cutoff = duree (2.5 s) - cta_seconds (1.0 s, _cta_config)
    assert "enable='gte(t,1.500000)'" in filt

    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["cta"] is True


def test_render_stream_with_cta_still_writes_both_rects(tmp_path, stream_dir, fake_ffmpeg, cpu_device):
    from clipper.render import render

    (stream_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=stream_dir.parent, output_dir=tmp_path / "output",
           config=_cta_config())

    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["cta"] is True
    assert data["camera_rect"] == CAMERA_RECT
    assert data["video_rect"] == GAMEPLAY_RECT
    cmd = fake_ffmpeg[0]["cmd"]
    assert cmd.count("-i") == 3


# --- (9) rendu ffmpeg reel : carte de fin visible seulement en fin de clip -

def _whiteness(img, zone):
    return sum(
        1 for y in range(zone["y0"], zone["y1"], 2) for x in range(zone["x0"], zone["x1"], 2)
        if min(img.getpixel((x, y))) > 235
    )


@no_ffmpeg
@no_ffprobe
def test_render_letterbox_real_ffmpeg_with_cta_shows_the_end_card_only_after_the_cutoff(
    tmp_path, letterbox_dir, synthetic_source, cpu_device
):
    from PIL import Image

    from clipper.render import render

    out = render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
                 config=_cta_config(x264_preset="ultrafast"))  # duree 2.5 s, cta_seconds 1.0 -> cutoff 1.5 s

    def frame(t):
        path = tmp_path / f"frame_{t}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(out),
                        "-frames:v", "1", str(path)], check=True)
        return Image.open(path).convert("RGB")

    before = _whiteness(frame(1.0), SUBTITLES_ZONE)
    after = _whiteness(frame(2.2), SUBTITLES_ZONE)
    assert after > before + 20, (before, after)  # l'encadre blanc n'apparait qu'apres le cutoff


@no_ffmpeg
@no_ffprobe
def test_render_stream_real_ffmpeg_with_cta_shows_the_end_card_only_after_the_cutoff(
    tmp_path, stream_dir, cpu_device
):
    from PIL import Image

    from clipper.render import render

    x0, y0, x1, y1 = FACECAM
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"color=c=green:s={SRC_W}x{SRC_H}:r=25:d=5,drawbox=x={x0}:y={y0}:w={x1 - x0}:h={y1 - y0}:color=red:t=fill",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=5",
         "-ac", "2", "-shortest", str(stream_dir / f"{VIDEO_ID}.mp4")],
        check=True,
    )
    out = render(VIDEO_ID, CLIP_ID, workspace_dir=stream_dir.parent, output_dir=tmp_path / "output",
                 config=_cta_config(x264_preset="ultrafast"))  # duree 2.5 s, cta_seconds 1.0 -> cutoff 1.5 s

    def frame(t):
        path = tmp_path / f"frame_{t}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(out),
                        "-frames:v", "1", str(path)], check=True)
        return Image.open(path).convert("RGB")

    before = _whiteness(frame(1.0), STREAM_SUBTITLES_ZONE)
    after = _whiteness(frame(2.2), STREAM_SUBTITLES_ZONE)
    assert after > before + 20, (before, after)


# --- (10) « Partie N » continue de s'afficher normalement avec le CTA ------


def test_render_multipart_with_cta_still_draws_partie_n_during_the_end_card(
    tmp_path, letterbox_dir, fake_ffmpeg, cpu_device
):
    from clipper.render import render

    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    (letterbox_dir / "captions.json").write_text(
        json.dumps(_captions_json(part=2, parts_total=3)), encoding="utf-8")

    render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
           config=_cta_config())

    assert "part.txt" in fake_ffmpeg[0]["scratch"]
    assert "cta_card.png" in fake_ffmpeg[0]["scratch"]
    filt = fake_ffmpeg[0]["cmd"][fake_ffmpeg[0]["cmd"].index("-filter_complex") + 1]
    part = next(f for f in filt.split(";") if "part.txt" in f)
    assert "drawtext=textfile='part.txt'" in part
    assert "enable=" not in part  # Partie N jamais masquee, tout le clip
    assert "[2:v]overlay" in filt  # la carte de fin s'ajoute, sans remplacer Partie N

    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["cta"] is True


# --------------------------------------------------------------------------
# Agencement stream split (SPEC-76dc) : webcam en haut, jeu en bas, badge de
# chaine optionnel, titre desactivable (title_enabled, nouveau reglage).
# --------------------------------------------------------------------------

SPLIT_BADGE_ZONE = {"x0": 330, "y0": 590, "x1": 750, "y1": 690}
SPLIT_SUBTITLES_ZONE = {"x0": 150, "y0": 710, "x1": 930, "y1": 860}
WEBCAM_RECT = {"x": 20, "y": 0, "w": 1040, "h": 640}
SPLIT_GAMEPLAY_RECT = {"x": 0, "y": 640, "w": 1080, "h": 1280}


def _reframe_json_stream_split(with_title=False):
    """Plan stream_split tel que l'ecrit reframe (SPEC-76dc) : webcam en
    haut, jeu en bas, aucun ne se chevauche ; pas de zone title par defaut
    (split_webcam_dest.y = 0 ne laisse pas de place au-dessus)."""
    panels = [
        _panel("webcam", 1400, 40, 500, 308, dict(WEBCAM_RECT)),
        _panel("gameplay", 0, 0, 911, 1080, dict(SPLIT_GAMEPLAY_RECT)),
    ]
    plan = {
        "index": 0, "start": 1.0, "end": 3.5, "image": None, "llm": None, "layout": "stream_split",
        "reason": None, "faces": [], "panels": panels,
    }
    zones = {"badge": dict(SPLIT_BADGE_ZONE), "subtitles": dict(SPLIT_SUBTITLES_ZONE), "part": dict(PART_ZONE)}
    if with_title:
        zones["title"] = dict(STREAM_TITLE_ZONE)
    return {
        "video_id": VIDEO_ID, "clip_id": CLIP_ID, "start": 1.0, "end": 3.5,
        "source": {"width": SRC_W, "height": SRC_H}, "output": {"width": OUT_W, "height": OUT_H},
        "layout": "stream_split", "format": "letterbox", "layout_mode": "stream_auto", "layout_reason": None,
        "facecam": {"x": 1400, "y": 40, "w": 500, "h": 308},
        "text_zones": zones,
        "plans": [plan],
    }


@pytest.fixture
def stream_split_dir(video_dir):
    (video_dir / "reframe" / f"{CLIP_ID}.json").write_text(
        json.dumps(_reframe_json_stream_split()), encoding="utf-8")
    return video_dir


def test_split_render_defaults_are_present_and_unchanged_by_default():
    from clipper.render import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["title_enabled"] is True
    assert CONFIG_DEFAULTS["badge_enabled"] is False
    assert CONFIG_DEFAULTS["badge_logo"] == ""
    assert CONFIG_DEFAULTS["badge_name"] == ""
    assert CONFIG_DEFAULTS["badge_logo_size"] == 100
    assert CONFIG_DEFAULTS["badge_glyph_scale"] == 0.65
    assert CONFIG_DEFAULTS["badge_logo_fill"] == ""
    assert CONFIG_DEFAULTS["badge_background"] == "black"
    assert CONFIG_DEFAULTS["badge_name_outline_color"] == "black"
    assert CONFIG_DEFAULTS["badge_name_outline"] == 3
    assert CONFIG_DEFAULTS["badge_name_shadow_enabled"] is False
    assert CONFIG_DEFAULTS["badge_name_shadow_color"] == "black"
    assert CONFIG_DEFAULTS["badge_name_shadow_offset"] == [2, 2]


def test_stream_split_filter_overlays_webcam_and_gameplay_without_a_title(tmp_path, video_dir):
    from clipper.render import CONFIG_DEFAULTS, _build_filter_complex

    filt, _label = _build_filter_complex(
        _reframe_json_stream_split(), 1.0, 3.5, video_dir / "subtitles" / f"{CLIP_ID}.ass", None, None, tmp_path,
        CONFIG_DEFAULTS,
    )
    parts = filt.split(";")
    assert any("crop=w='500':h='308':x='1400':y='40'" in f for f in parts)
    assert any(f.endswith("scale=1040:640[p0_0s]") for f in parts)
    assert any("overlay=x=20:y=0" in f for f in parts)
    assert any("crop=w='911':h='1080':x='0':y='0'" in f for f in parts)
    assert any(f.endswith("scale=1080:1280[p0_1s]") for f in parts)
    assert any("overlay=x=0:y=640" in f for f in parts)
    assert "[1:v]overlay" not in filt  # pas de titre sans title_input


def test_stream_split_render_without_title_zone_and_title_disabled_succeeds(
    tmp_path, stream_split_dir, fake_ffmpeg, cpu_device
):
    from clipper.render import render

    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
           config=make_config(title_enabled=False))

    cmd = fake_ffmpeg[0]["cmd"]
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert not any(i.endswith("title.png") for i in inputs)
    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["layout"] == "stream_split"
    assert data["webcam_rect"] == WEBCAM_RECT
    assert data["video_rect"] == SPLIT_GAMEPLAY_RECT


def test_stream_split_title_enabled_without_a_title_zone_is_an_explicit_error(
    tmp_path, stream_split_dir, fake_ffmpeg
):
    from clipper.render import RenderError, render

    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="title"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
               config=make_config())  # title_enabled par defaut = true
    assert fake_ffmpeg == []


def test_stream_split_render_with_title_enabled_and_a_title_zone_draws_it(
    tmp_path, video_dir, fake_ffmpeg, cpu_device
):
    from clipper.render import render

    (video_dir / "reframe" / f"{CLIP_ID}.json").write_text(
        json.dumps(_reframe_json_stream_split(with_title=True)), encoding="utf-8")
    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output", config=make_config())

    cmd = fake_ffmpeg[0]["cmd"]
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert inputs[1].endswith("title.png")


@pytest.mark.parametrize("missing", ["webcam", "gameplay"])
def test_stream_split_without_webcam_or_gameplay_panel_asks_to_rerun_reframe(
    tmp_path, stream_split_dir, fake_ffmpeg, missing
):
    from clipper.render import RenderError, render

    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    reframe = _reframe_json_stream_split()
    reframe["plans"][0]["panels"] = [p for p in reframe["plans"][0]["panels"] if p["name"] != missing]
    (stream_split_dir / "reframe" / f"{CLIP_ID}.json").write_text(json.dumps(reframe), encoding="utf-8")
    with pytest.raises(RenderError, match=missing):
        render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
               config=make_config(title_enabled=False))
    assert fake_ffmpeg == []


# --------------------------------------------------------------------------
# Badge de chaine (SPEC-76dc) : logo + nom sur fond noir, requiert une zone
# badge (donc l'agencement stream split), jamais a moitie active.
# --------------------------------------------------------------------------


def _write_logo(path, size=(64, 64)):
    from PIL import Image

    Image.new("RGBA", size, (255, 0, 0, 255)).save(path)


def _write_two_tone_logo(path, size=(64, 64), edge=(145, 70, 255, 255), center=(255, 255, 255, 255)):
    """Logo avec une couleur de bord distincte du centre, comme le vrai logo
    Twitch (fond viole, glyphe blanc) : permet de verifier l'echantillonnage
    de badge_logo_fill."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", size, edge)
    draw = ImageDraw.Draw(img)
    w, h = size
    pad = w // 4
    draw.rectangle((pad, pad, w - pad, h - pad), fill=center)
    img.save(path)


def test_badge_enabled_without_logo_is_an_explicit_error(tmp_path, stream_split_dir, fake_ffmpeg):
    from clipper.render import RenderError, render

    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="badge_logo"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
               config=make_config(title_enabled=False, badge_enabled=True, badge_name="Exemple"))
    assert fake_ffmpeg == []


def test_badge_enabled_without_name_is_an_explicit_error(tmp_path, stream_split_dir, fake_ffmpeg):
    from clipper.render import RenderError, render

    logo = tmp_path / "logo.png"
    _write_logo(logo)
    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="badge_name"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
               config=make_config(title_enabled=False, badge_enabled=True, badge_logo=str(logo)))
    assert fake_ffmpeg == []


def test_badge_enabled_with_a_missing_logo_file_is_an_explicit_error(tmp_path, stream_split_dir, fake_ffmpeg):
    from clipper.render import RenderError, render

    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="introuvable"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
               config=make_config(title_enabled=False, badge_enabled=True,
                                  badge_logo=str(tmp_path / "absent.png"), badge_name="Exemple"))
    assert fake_ffmpeg == []


def test_badge_enabled_on_a_layout_without_a_badge_zone_is_an_explicit_error(
    tmp_path, letterbox_dir, fake_ffmpeg
):
    from clipper.render import RenderError, render

    logo = tmp_path / "logo.png"
    _write_logo(logo)
    (letterbox_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="badge"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=letterbox_dir.parent, output_dir=tmp_path / "output",
               config=make_config(badge_enabled=True, badge_logo=str(logo), badge_name="Exemple"))
    assert fake_ffmpeg == []


def test_badge_enabled_draws_the_logo_and_name_and_is_included_as_an_ffmpeg_input(
    tmp_path, stream_split_dir, fake_ffmpeg, cpu_device
):
    from clipper.render import render

    logo = tmp_path / "logo.png"
    _write_logo(logo)
    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
           config=make_config(title_enabled=False, badge_enabled=True, badge_logo=str(logo),
                              badge_name="Exemple"))

    cmd = fake_ffmpeg[0]["cmd"]
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert any(i.endswith("badge.png") for i in inputs)
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert f"overlay=x={SPLIT_BADGE_ZONE['x0']}:y={SPLIT_BADGE_ZONE['y0']}" in filt


def test_badge_disabled_draws_no_badge_even_with_a_logo_and_name_configured(
    tmp_path, stream_split_dir, fake_ffmpeg, cpu_device, monkeypatch
):
    from clipper import render as render_mod

    drawn = []
    monkeypatch.setattr(render_mod, "badge_png", lambda *a, **k: drawn.append(a))
    logo = tmp_path / "logo.png"
    _write_logo(logo)
    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render_mod.render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
                      config=make_config(title_enabled=False, badge_enabled=False, badge_logo=str(logo),
                                         badge_name="ma_chaine"))

    assert drawn == []
    cmd = fake_ffmpeg[0]["cmd"]
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert not any(i.endswith("badge.png") for i in inputs)
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert f"overlay=x={SPLIT_BADGE_ZONE['x0']}:y={SPLIT_BADGE_ZONE['y0']}" not in filt


def test_badge_png_draws_the_filled_square_logo_and_the_measured_name(tmp_path):
    from PIL import Image

    from clipper.render import CONFIG_DEFAULTS, badge_png

    logo = tmp_path / "logo.png"
    _write_logo(logo, size=(64, 64))
    out = tmp_path / "badge.png"
    zone = {"x0": 0, "y0": 0, "x1": 420, "y1": 100}
    badge_png(logo, "Exemple", zone, CONFIG_DEFAULTS, out)

    img = Image.open(out).convert("RGBA")
    assert img.size == (420, 100)
    # fond noir opaque loin du groupe (coin haut-gauche, defaut inchange)
    assert img.getpixel((2, 2))[:3] == (0, 0, 0)
    # le glyphe (logo rouge, echantillonne aussi pour le carre) est bien
    # present quelque part dans l'image (le groupe est centre)
    reds = [img.getpixel((x, y)) for x in range(420) for y in range(100) if img.getpixel((x, y))[0] > 200]
    assert reds
    # du texte (blanc) present quelque part dans l'image
    whites = [
        img.getpixel((x, y)) for x in range(420) for y in range(100)
        if img.getpixel((x, y))[:3] == (255, 255, 255)
    ]
    assert whites


def _content_x_range(img, zw, zh, background_rgb=None):
    """Etendue horizontale (min, max) des pixels de contenu. Sans fond plat
    (``background_rgb=None``), tout pixel non transparent est du contenu
    (mode 'none' : le seul fond restant, le carre du logo, EST le contenu) ;
    avec un fond plat opaque, seuls les pixels qui en different comptent."""
    xs = []
    for x in range(zw):
        for y in range(zh):
            r, g, b, a = img.getpixel((x, y))
            if a == 0:
                continue
            if background_rgb is not None and (r, g, b) == background_rgb and a == 255:
                continue
            xs.append(x)
    assert xs, "aucun pixel de contenu trouve"
    return min(xs), max(xs)


@pytest.mark.parametrize("badge_background", ["black", "none"])
def test_badge_png_group_is_centered_on_the_zone(tmp_path, badge_background):
    from PIL import Image

    from clipper.render import CONFIG_DEFAULTS, badge_png

    logo = tmp_path / "logo.png"
    _write_logo(logo, size=(64, 64))
    out = tmp_path / "badge.png"
    zone = {"x0": 0, "y0": 0, "x1": 420, "y1": 100}
    # badge_glyph_scale=1 : le glyphe remplit tout le carre du logo, sans
    # marge invisible -- sinon en fond plein (noir) cette marge (glyphe <
    # carre) fausse la mesure par pixels (le carre est indetectable sur
    # fond de meme couleur), qui ne verifierait plus le meme groupe que
    # celui reellement centre par l'implementation.
    settings = dict(CONFIG_DEFAULTS, badge_background=badge_background, badge_glyph_scale=1.0)
    badge_png(logo, "Exemple", zone, settings, out)

    img = Image.open(out).convert("RGBA")
    background_rgb = (0, 0, 0) if badge_background != "none" else None
    x_min, x_max = _content_x_range(img, 420, 100, background_rgb)
    content_center = (x_min + x_max + 1) / 2
    assert abs(content_center - 420 / 2) <= 1


def test_badge_png_none_background_has_no_rectangle_behind_the_name(tmp_path):
    from PIL import Image

    from clipper.render import CONFIG_DEFAULTS, badge_png

    logo = tmp_path / "logo.png"
    _write_logo(logo, size=(64, 64))
    out = tmp_path / "badge.png"
    zone = {"x0": 0, "y0": 0, "x1": 420, "y1": 100}
    settings = dict(CONFIG_DEFAULTS, badge_background="none")
    badge_png(logo, "Exemple", zone, settings, out)

    img = Image.open(out).convert("RGBA")
    # un coin loin du groupe reste transparent : pas de rectangle plein
    assert img.getpixel((2, 2))[3] == 0
    assert img.getpixel((417, 97))[3] == 0
    # le nom (blanc) reste dessine quelque part
    whites = [
        img.getpixel((x, y)) for x in range(420) for y in range(100)
        if img.getpixel((x, y))[:3] == (255, 255, 255) and img.getpixel((x, y))[3] > 0
    ]
    assert whites


@pytest.mark.parametrize("badge_background", ["black", "none"])
def test_badge_png_logo_square_is_filled_with_the_sampled_edge_color_by_default(tmp_path, badge_background):
    from PIL import Image

    from clipper.render import CONFIG_DEFAULTS, badge_png

    logo = tmp_path / "logo.png"
    edge = (145, 70, 255, 255)
    _write_two_tone_logo(logo, edge=edge)
    out = tmp_path / "badge.png"
    zone = {"x0": 0, "y0": 0, "x1": 420, "y1": 100}
    settings = dict(CONFIG_DEFAULTS, badge_background=badge_background)
    badge_png(logo, "Exemple", zone, settings, out)

    img = Image.open(out).convert("RGBA")
    background_rgb = (0, 0, 0) if badge_background != "none" else None
    x_min, _x_max = _content_x_range(img, 420, 100, background_rgb)
    square = int(CONFIG_DEFAULTS["badge_logo_size"])
    square_top = (100 - square) // 2
    # coin du carre (badge_logo_size = zone height ici -> square_top = 0) :
    # echantillonne au coin (0, 0) du logo, jamais de noir.
    assert img.getpixel((x_min, square_top)) == edge


def test_badge_png_logo_fill_can_be_overridden(tmp_path):
    from PIL import Image

    from clipper.render import CONFIG_DEFAULTS, badge_png

    logo = tmp_path / "logo.png"
    _write_two_tone_logo(logo, edge=(145, 70, 255, 255))
    out = tmp_path / "badge.png"
    zone = {"x0": 0, "y0": 0, "x1": 420, "y1": 100}
    settings = dict(CONFIG_DEFAULTS, badge_logo_fill="#112233")
    badge_png(logo, "Exemple", zone, settings, out)

    img = Image.open(out).convert("RGBA")
    x_min, _x_max = _content_x_range(img, 420, 100, (0, 0, 0))
    assert img.getpixel((x_min, 0)) == (0x11, 0x22, 0x33, 255)


def test_badge_png_default_background_fills_the_whole_zone_opaque(tmp_path):
    from PIL import Image

    from clipper.render import CONFIG_DEFAULTS, badge_png

    logo = tmp_path / "logo.png"
    _write_logo(logo, size=(64, 64))
    out = tmp_path / "badge.png"
    zone = {"x0": 0, "y0": 0, "x1": 420, "y1": 100}
    badge_png(logo, "Exemple", zone, CONFIG_DEFAULTS, out)

    img = Image.open(out).convert("RGBA")
    corners = [(0, 0), (419, 0), (0, 99), (419, 99)]
    for xy in corners:
        assert img.getpixel(xy) == (0, 0, 0, 255)


def test_badge_png_invalid_shadow_offset_is_an_explicit_error(tmp_path):
    from clipper.render import CONFIG_DEFAULTS, RenderError, badge_png

    logo = tmp_path / "logo.png"
    _write_logo(logo, size=(64, 64))
    zone = {"x0": 0, "y0": 0, "x1": 420, "y1": 100}
    settings = dict(CONFIG_DEFAULTS, badge_name_shadow_enabled=True, badge_name_shadow_offset=[1, 2, 3])
    with pytest.raises(RenderError, match="badge_name_shadow_offset"):
        badge_png(logo, "Exemple", zone, settings, tmp_path / "badge.png")


def test_badge_png_missing_logo_file_is_an_explicit_error(tmp_path):
    from clipper.render import CONFIG_DEFAULTS, RenderError, badge_png

    zone = {"x0": 0, "y0": 0, "x1": 420, "y1": 100}
    with pytest.raises(RenderError, match="introuvable"):
        badge_png(tmp_path / "absent.png", "Exemple", zone, CONFIG_DEFAULTS, tmp_path / "badge.png")


def test_badge_png_name_too_long_is_an_explicit_error(tmp_path):
    from clipper.render import CONFIG_DEFAULTS, RenderError, badge_png

    logo = tmp_path / "logo.png"
    _write_logo(logo)
    zone = {"x0": 0, "y0": 0, "x1": 150, "y1": 100}  # trop etroit pour le nom
    with pytest.raises(RenderError):
        badge_png(logo, "Un nom de chaine bien trop long pour ce bandeau", zone, CONFIG_DEFAULTS,
                  tmp_path / "badge.png")


def test_badge_replaces_the_cta_handle_pseudo_when_both_are_enabled(tmp_path, stream_split_dir, fake_ffmpeg):
    from clipper.render import render

    logo = tmp_path / "logo.png"
    _write_logo(logo)
    (stream_split_dir / "reframe" / f"{CLIP_ID}.json").write_text(
        json.dumps(_reframe_json_stream_split(with_title=True)), encoding="utf-8")
    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    # pseudo bien trop long pour la zone title -> echouerait si badge ne le
    # remplacait pas (SPEC-76dc : le badge remplace le pseudo, jamais les deux).
    long_handle = "twitch.tv/" + "x" * 200
    render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
           config=make_config(cta_enabled=True, cta_handle=long_handle, cta_seconds=1.0,
                              badge_enabled=True, badge_logo=str(logo), badge_name="Exemple"))
    cmd = fake_ffmpeg[0]["cmd"]
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert any(i.endswith("badge.png") for i in inputs)
    data = json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))
    assert data["cta"] is True  # la carte de fin n'est pas affectee


def test_cta_handle_without_a_pseudo_anchor_is_an_explicit_error(tmp_path, stream_split_dir, fake_ffmpeg):
    from clipper.render import RenderError, render

    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    with pytest.raises(RenderError, match="ancre"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
               config=make_config(title_enabled=False, cta_enabled=True, cta_handle="twitch.tv/exemple",
                                  cta_seconds=1.0))
    assert fake_ffmpeg == []


# --------------------------------------------------------------------------
# Miniature d'un clip rendu (TASK-dc9d)
# --------------------------------------------------------------------------


def test_thumbnail_defaults_live_in_render_config_defaults():
    from clipper.render import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["thumbnail_width"] == 360
    assert CONFIG_DEFAULTS["thumbnail_seek"] == 0.5


def test_thumbnail_builds_one_frame_jpeg_command_and_writes_atomically(tmp_path, monkeypatch):
    from clipper import render as render_step

    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"mp4")
    seen = []

    def fake_exec(cmd, cwd, out_path):
        seen.append((list(cmd), Path(out_path)))
        Path(out_path).write_bytes(b"\xff\xd8jpeg")

    monkeypatch.setattr(render_step, "_exec_ffmpeg", fake_exec)
    target = tmp_path / "t" / "clip.jpg"

    render_step.thumbnail(mp4, target)

    cmd, out_path = seen[0]
    assert target.read_bytes().startswith(b"\xff\xd8") and out_path != target  # tmp puis remplacement
    assert cmd[0] == "ffmpeg" and cmd[cmd.index("-ss") + 1] == "0.500000"
    assert cmd[cmd.index("-frames:v") + 1] == "1" and "scale='min(360,iw)':-2" in cmd[cmd.index("-vf") + 1]
    assert not list(target.parent.glob("*.tmp"))


def test_thumbnail_missing_mp4_is_a_render_error(tmp_path):
    from clipper import render as render_step

    with pytest.raises(render_step.RenderError, match="introuvable"):
        render_step.thumbnail(tmp_path / "absent.mp4", tmp_path / "t.jpg")


def test_thumbnail_seek_argument_overrides_the_configured_seek(tmp_path, monkeypatch):
    from clipper import render as render_step

    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"mp4")
    seen = []
    monkeypatch.setattr(render_step, "_exec_ffmpeg", lambda cmd, cwd, out: (seen.append(list(cmd)), Path(out).write_bytes(b"\xff\xd8")))

    render_step.thumbnail(mp4, tmp_path / "t.jpg", seek=12.5)

    assert seen[0][seen[0].index("-ss") + 1] == "12.500000"


def test_video_thumbnail_seek_ratio_default_is_ten_percent():
    from clipper.render import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["video_thumbnail_seek_ratio"] == 0.1


# --------------------------------------------------------------------------
# TASK-0aff43d73607 (image-M1, image-M4) : pas de .mp4.tmp orphelin quand
# ffmpeg echoue ; le sidecar dit ce qui a vraiment ete dessine.
# --------------------------------------------------------------------------


def test_failing_ffmpeg_leaves_no_mp4_tmp_in_output(tmp_path, video_dir, monkeypatch, cpu_device):
    from clipper.render import RenderError, render

    def failing(cmd, cwd, out_path):
        Path(out_path).write_bytes(b"partiel")  # ffmpeg a commence a ecrire puis echoue
        raise RenderError("ffmpeg a echoue")

    monkeypatch.setattr("clipper.render._exec_ffmpeg", failing)
    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    out_dir = tmp_path / "output"
    with pytest.raises(RenderError, match="ffmpeg a echoue"):
        render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=out_dir, config=make_config())
    assert list((out_dir / VIDEO_ID).iterdir()) == []


def _sidecar(tmp_path):
    return json.loads((tmp_path / "output" / VIDEO_ID / f"{CLIP_ID}.json").read_text(encoding="utf-8"))


def test_split_sidecar_records_title_shown_false_when_title_disabled(
    tmp_path, stream_split_dir, fake_ffmpeg, cpu_device
):
    from clipper.render import render

    (stream_split_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=stream_split_dir.parent, output_dir=tmp_path / "output",
           config=make_config(title_enabled=False))
    data = _sidecar(tmp_path)
    assert data["title_shown"] is False
    assert data["badge_shown"] is False
    assert data["cta_handle_shown"] is False


def test_split_sidecar_records_title_shown_true_when_title_drawn(tmp_path, video_dir, fake_ffmpeg, cpu_device):
    from clipper.render import render

    (video_dir / "reframe" / f"{CLIP_ID}.json").write_text(
        json.dumps(_reframe_json_stream_split(with_title=True)), encoding="utf-8")
    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"")
    render(VIDEO_ID, CLIP_ID, workspace_dir=video_dir.parent, output_dir=tmp_path / "output", config=make_config())
    data = _sidecar(tmp_path)
    assert data["title_shown"] is True
    assert data["badge_shown"] is False
