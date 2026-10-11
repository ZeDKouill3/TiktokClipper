from __future__ import annotations

import atexit
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from clipper import llm, qa
from clipper.config import Config
from clipper.llm.fake import FakeBackend

VIDEO_ID = "abcdefghijk"
CLIP_ID = "01"
TRANSCRIPT = "alors la tu vois il ouvre la porte et la surprise"

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe absents du PATH",
)

pytestmark = needs_ffmpeg


# --------------------------------------------------------------------------
# Fixtures : un clip rendu synthetique (mp4 + JSON au format de render.py)
# --------------------------------------------------------------------------


_MP4_CACHE: dict[tuple, Path] = {}
_MP4_CACHE_DIR = Path(tempfile.mkdtemp(prefix="clipper-test-mp4-"))
atexit.register(shutil.rmtree, _MP4_CACHE_DIR, ignore_errors=True)


def make_mp4(path, *, colors=("red",), seg=1.5, size="1080x1920", silence=0.0, audio=True):
    """Video synthetique, rendue une fois par processus pour des arguments
    donnes (TASK-8f7c) : chaque appel recoit une COPIE, le fichier du cache
    n'est jamais donne ni modifie."""
    key = (tuple(colors), seg, size, silence, audio)
    source = _MP4_CACHE.get(key)
    if source is None:
        name = hashlib.sha1(repr(key).encode()).hexdigest()[:16]
        source = _MP4_CACHE_DIR / f"{name}.mp4"
        _render_mp4(source, colors=colors, seg=seg, size=size, silence=silence, audio=audio)
        _MP4_CACHE[key] = source
    shutil.copyfile(source, path)
    return path


def _render_mp4(path, *, colors, seg, size, silence, audio):
    """Video synthetique : un plan de ``seg`` s par couleur (donc un
    changement de plan a chaque couleur), audio sinus precede de ``silence``
    secondes muettes."""
    duration = seg * len(colors)
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for c in colors:
        args += ["-f", "lavfi", "-i", f"color=c={c}:s={size}:r=30:d={seg}"]
    n = len(colors)
    filters = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    maps = ["-map", "[v]"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}"]
        delay = int(silence * 1000)
        filters += f";[{n}:a]atrim=0:{duration - silence},adelay={delay}|{delay},apad=whole_dur={duration}[a]"
        maps += ["-map", "[a]"]
    args += ["-filter_complex", filters, *maps, "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-t", str(duration)]
    if audio:
        args += ["-c:a", "aac", "-ar", "48000"]
    args.append(str(path))
    subprocess.run(args, check=True)
    return path


def make_mp4_with_black(path, *, black_seconds, segment_seconds=1.0, size="1080x1920"):
    """Video synthetique : un plan rouge, une plage noire de ``black_seconds``,
    un plan rouge, avec audio continu (pas de silence)."""
    total = round(segment_seconds * 2 + black_seconds, 3)
    args = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=red:s={size}:r=30:d={segment_seconds}",
        "-f", "lavfi", "-i", f"color=c=black:s={size}:r=30:d={black_seconds}",
        "-f", "lavfi", "-i", f"color=c=red:s={size}:r=30:d={segment_seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={total}",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-t", str(total),
        "-c:a", "aac", "-ar", "48000", str(path),
    ]
    subprocess.run(args, check=True)
    return total


def write_black_clip(output_dir, *, black_seconds, segment_seconds=1.0, clip_id=CLIP_ID, size="1080x1920"):
    d = output_dir / VIDEO_ID
    total = make_mp4_with_black(
        d / f"{clip_id}.mp4", black_seconds=black_seconds, segment_seconds=segment_seconds, size=size,
    )
    (d / f"{clip_id}.json").write_text(
        json.dumps(clip_json(duration=total, clip_id=clip_id)), encoding="utf-8"
    )
    return d / f"{clip_id}.json"


LETTERBOX_RECT = {"x": 0, "y": 440, "w": 1080, "h": 790}


def make_letterbox_mp4(path, *, black_seconds, segment_seconds=1.0, size="1080x1920", video_rect=LETTERBOX_RECT):
    """Video synthetique letterbox : fond blanc (encadre du titre d'ecran
    compris) sur tout le cadre, avec un rectangle rouge/noir/rouge dessine
    dans ``video_rect`` seulement -> le noir n'occupe qu'une fraction du
    cadre entier (la part blanche domine), seul un blackdetect restreint a
    ``video_rect`` peut le voir."""
    total = round(segment_seconds * 2 + black_seconds, 3)
    x, y, w, h = video_rect["x"], video_rect["y"], video_rect["w"], video_rect["h"]

    def seg(color, dur):
        return f"color=c=white:s={size}:r=30:d={dur},drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill"

    args = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", seg("red", segment_seconds),
        "-f", "lavfi", "-i", seg("black", black_seconds),
        "-f", "lavfi", "-i", seg("red", segment_seconds),
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={total}",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-t", str(total),
        "-c:a", "aac", "-ar", "48000", str(path),
    ]
    subprocess.run(args, check=True)
    return total


def write_letterbox_clip(
    output_dir, *, black_seconds, segment_seconds=1.0, clip_id=CLIP_ID, size="1080x1920", video_rect=LETTERBOX_RECT,
):
    d = output_dir / VIDEO_ID
    total = make_letterbox_mp4(
        d / f"{clip_id}.mp4", black_seconds=black_seconds, segment_seconds=segment_seconds,
        size=size, video_rect=video_rect,
    )
    (d / f"{clip_id}.json").write_text(
        json.dumps(clip_json(
            duration=total, clip_id=clip_id, layout="letterbox", video_rect=video_rect,
            screen_title="Il ouvre la porte",
        )),
        encoding="utf-8",
    )
    return d / f"{clip_id}.json"


def write_clip_letterbox(
    output_dir, clip_id=CLIP_ID, *, video_rect=LETTERBOX_RECT,
    screen_title="Il ouvre la porte, regarde bien", hook_text="ignore-moi", **mp4_kwargs
):
    """Clip letterbox sans video specifique (aucun controle blackdetect
    teste ici) : sert aux tests de prompt/schema envoyes a l'IA."""
    d = output_dir / VIDEO_ID
    colors = mp4_kwargs.get("colors", ("red",))
    duration = mp4_kwargs.get("seg", 1.5) * len(colors)
    make_mp4(d / f"{clip_id}.mp4", **mp4_kwargs)
    (d / f"{clip_id}.json").write_text(
        json.dumps(clip_json(
            duration=duration, clip_id=clip_id, layout="letterbox", video_rect=video_rect,
            screen_title=screen_title, hook_text=hook_text,
        )),
        encoding="utf-8",
    )
    return d / f"{clip_id}.json"


def clip_json(duration=3.0, **overrides):
    """Le sidecar tel que l'ecrit clipper/render.py (SPEC-6127)."""
    data = {
        "video_id": VIDEO_ID, "source_url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
        "source_title": "Une video", "clip_id": CLIP_ID, "part": 1, "parts_total": 1,
        "start": 10.0, "end": 10.0 + duration, "duration": duration, "language": "fr",
        "score": 80.0, "scores": {"hook": 8}, "reason": "ca marche", "hook_text": "Il ouvre la porte",
        "title": "Titre", "caption": "Legende", "hashtags": ["#un"], "transcript": TRANSCRIPT,
        "layout": "single", "qa": {"status": "skipped", "issues": []},
        "created_at": "2026-09-25T10:00:00+00:00",
    }
    data.update(overrides)
    return data


@pytest.fixture
def dirs(tmp_path):
    out = tmp_path / "output" / VIDEO_ID
    out.mkdir(parents=True)
    return tmp_path / "workspace", tmp_path / "output"


def write_clip(output_dir, clip_id=CLIP_ID, **mp4_kwargs):
    d = output_dir / VIDEO_ID
    colors = mp4_kwargs.get("colors", ("red",))
    duration = mp4_kwargs.get("seg", 1.5) * len(colors)
    make_mp4(d / f"{clip_id}.mp4", **mp4_kwargs)
    (d / f"{clip_id}.json").write_text(
        json.dumps(clip_json(duration=duration, clip_id=clip_id)), encoding="utf-8"
    )
    return d / f"{clip_id}.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def config(tmp_path, **qa_overrides):
    # parallel=1 par defaut dans les tests : deterministe (les reponses
    # scriptees en liste ordonnee restent affectees clip par clip dans
    # l'ordre) ; les tests de TASK-1366 qui veulent du parallelisme le
    # demandent explicitement.
    sections = {"parallel": 1, **qa_overrides}
    return Config(
        mode="auto", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={"qa": sections},
    )


def no_issue(_request=None):
    return {"issues": []}


def run(tmp_path, workspace, output, **kwargs):
    return qa.run(VIDEO_ID, workspace, output, config=config(tmp_path), **kwargs)


# --------------------------------------------------------------------------
# C1-C2 : images (debut, milieu, fin, une par changement de plan) et
# transcription envoyees a clipper.llm, usage qa
# --------------------------------------------------------------------------


def test_llm_receives_start_middle_end_frames_and_one_per_shot_change(tmp_path, dirs):
    workspace, output = dirs
    write_clip(output, colors=("red", "blue", "green"), seg=1.5)  # 2 changements de plan
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)

    assert len(fake.calls) == 1
    request = fake.calls[0]
    assert request.usage == "qa"
    # debut, milieu, fin + 2 changements de plan (a 1.5 s et 3.0 s)
    assert len(request.images) == 5
    for image in request.images:
        assert image.suffix == ".jpg"
        assert image.exists()
    # le prompt annonce chaque image jointe : « Image k : t=<s> s ... »
    timecodes = [float(t) for t in re.findall(r"Image \d+ : t=(\d+\.\d+) s", request.prompt)]
    assert len(timecodes) == 5
    assert timecodes[0] < 0.5
    assert any(abs(t - 2.25) < 0.3 for t in timecodes)
    assert timecodes[-1] > 4.0
    assert any(1.5 <= t < 2.0 for t in timecodes)
    assert any(3.0 <= t < 3.5 for t in timecodes)


def test_prompt_carries_clip_transcript_and_hook(tmp_path, dirs):
    workspace, output = dirs
    write_clip(output)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    assert TRANSCRIPT in fake.calls[0].prompt
    assert "Il ouvre la porte" in fake.calls[0].prompt


def test_schema_asks_for_the_five_defects_black_screen_excluded(tmp_path, dirs):
    workspace, output = dirs
    write_clip(output)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    enum = fake.calls[0].schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert set(enum) == {"incomprehensible", "face_cut", "subtitle_on_face", "starts_mid_sentence", "weak_hook"}
    assert "black_screen" not in qa.DEFECTS


# --------------------------------------------------------------------------
# C4-C5 : qa.status passed | rejected + issues dans le JSON ; rejete jamais pret
# --------------------------------------------------------------------------


def test_clean_clip_is_passed_and_ready(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    data = read(path)
    assert data["qa"]["status"] == "passed"
    assert data["qa"]["issues"] == []
    assert data["ready"] is True
    assert qa.is_ready(data) is True
    # le reste du sidecar est intact
    assert data["title"] == "Titre" and data["transcript"] == TRANSCRIPT


def test_llm_blocking_defect_rejects_clip_and_it_is_never_ready(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)
    answer = {"issues": [{"type": "incomprehensible", "detail": "on ne sait pas de qui il parle"}]}
    with llm.use_backend(FakeBackend([answer])):
        run(tmp_path, workspace, output)
    data = read(path)
    assert data["qa"]["status"] == "rejected"
    assert data["qa"]["issues"] == [{
        "type": "incomprehensible", "detail": "on ne sait pas de qui il parle",
        "source": "llm", "severity": "blocking",
    }]
    assert data["ready"] is False
    assert qa.is_ready(data) is False


def test_is_ready_false_for_rejected_even_if_ready_flag_forged():
    forged = clip_json(qa={"status": "rejected", "issues": [{"type": "weak_hook"}]}, ready=True)
    assert qa.is_ready(forged) is False
    assert qa.is_ready(clip_json()) is False  # skipped : pas controle, pas pret


def test_every_rendered_clip_of_the_video_is_checked(tmp_path, dirs):
    workspace, output = dirs
    a = write_clip(output, clip_id="01")
    b = write_clip(output, clip_id="02")
    bad = {"issues": [{"type": "incomprehensible", "detail": "histoire decousue"}]}
    with llm.use_backend(FakeBackend([no_issue, bad])):
        run(tmp_path, workspace, output)
    assert read(a)["qa"]["status"] == "passed"
    assert read(b)["qa"]["status"] == "rejected"


def test_invalid_llm_answer_raises_and_leaves_json_untouched(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)
    before = path.read_text(encoding="utf-8")
    with llm.use_backend(FakeBackend([{"issues": [{"type": "pas_un_defaut", "detail": "x"}]}])):
        with pytest.raises(llm.SchemaError):
            run(tmp_path, workspace, output)
    assert path.read_text(encoding="utf-8") == before


def test_llm_unavailable_raises_no_fallback_verdict(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)
    with llm.use_backend(FakeBackend([llm.TransientLLMError("quota")])):
        with pytest.raises(llm.TransientLLMError):
            run(tmp_path, workspace, output)
    assert read(path)["qa"]["status"] == "skipped"
    assert "ready" not in read(path)


def test_already_checked_clip_is_not_rechecked_unless_force(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    with llm.use_backend(FakeBackend([])) as fake:
        run(tmp_path, workspace, output)
    assert fake.calls == []
    bad = {"issues": [{"type": "incomprehensible", "detail": "decousu"}]}
    with llm.use_backend(FakeBackend([bad])):
        run(tmp_path, workspace, output, force=True)
    assert read(path)["qa"]["status"] == "rejected"


def test_missing_output_raises(tmp_path):
    with pytest.raises(qa.QAError):
        qa.run(VIDEO_ID, tmp_path / "workspace", tmp_path / "output", config=config(tmp_path))


def test_mp4_missing_for_json_raises(tmp_path, dirs):
    workspace, output = dirs
    (output / VIDEO_ID / "01.json").write_text(json.dumps(clip_json()), encoding="utf-8")
    with llm.use_backend(FakeBackend([])):
        with pytest.raises(qa.QAError):
            run(tmp_path, workspace, output)


# --------------------------------------------------------------------------
# C6 : verifications mecaniques locales (duree, resolution, silence initial)
# --------------------------------------------------------------------------


def local_types(path):
    return {i["type"] for i in read(path)["qa"]["issues"] if i["source"] == "local"}


def test_wrong_resolution_rejects_even_if_llm_passes(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output, size="720x1280")
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert read(path)["qa"]["status"] == "rejected"
    assert local_types(path) == {"resolution"}
    assert read(path)["ready"] is False


def test_leading_silence_over_one_second_rejects(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output, colors=("red", "blue"), seg=1.5, silence=1.5)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert read(path)["qa"]["status"] == "rejected"
    assert local_types(path) == {"leading_silence"}


def test_short_leading_silence_is_fine(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output, colors=("red", "blue"), seg=1.5, silence=0.5)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert read(path)["qa"]["status"] == "passed"


def test_no_audio_stream_counts_as_leading_silence(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output, audio=False)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert local_types(path) == {"leading_silence"}


def test_duration_mismatch_with_json_rejects(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)  # 1.5 s reels
    data = read(path)
    data["duration"] = 30.0
    path.write_text(json.dumps(data), encoding="utf-8")
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert local_types(path) == {"duration"}
    assert read(path)["qa"]["status"] == "rejected"


def test_local_and_llm_issues_are_combined(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output, size="720x1280")
    bad = {"issues": [{"type": "starts_mid_sentence", "detail": "commence par 'et donc'"}]}
    with llm.use_backend(FakeBackend([bad])):
        run(tmp_path, workspace, output)
    sources = sorted((i["source"], i["type"]) for i in read(path)["qa"]["issues"])
    assert sources == [("llm", "starts_mid_sentence"), ("local", "resolution")]


def test_local_thresholds_come_from_config(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output, colors=("red", "blue"), seg=1.5, silence=1.5)
    with llm.use_backend(FakeBackend([no_issue])):
        qa.run(VIDEO_ID, workspace, output, config=config(tmp_path, max_leading_silence=2.0))
    assert read(path)["qa"]["status"] == "passed"


# --------------------------------------------------------------------------
# TASK-2960 : ecran noir mesure localement (ffmpeg blackdetect), un fondu
# court de la source n'est pas un rejet
# --------------------------------------------------------------------------


def test_short_black_fade_under_threshold_is_not_rejected(tmp_path, dirs):
    workspace, output = dirs
    path = write_black_clip(output, black_seconds=0.4)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert "black_screen" not in local_types(path)
    assert read(path)["qa"]["status"] == "passed"


def test_black_segment_over_threshold_is_reported_locally(tmp_path, dirs):
    workspace, output = dirs
    path = write_black_clip(output, black_seconds=1.5)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert local_types(path) == {"black_screen"}
    # sous black_block_seconds (3 s) : avertissement, le clip reste pret (TASK-4eb4)
    assert read(path)["qa"]["status"] == "passed"
    assert read(path)["ready"] is True


def test_clip_without_black_frames_has_no_black_screen_issue(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output, colors=("red", "blue"), seg=1.0)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert "black_screen" not in local_types(path)
    assert read(path)["qa"]["status"] == "passed"


def test_black_min_seconds_is_configurable(tmp_path, dirs):
    workspace, output = dirs
    path = write_black_clip(output, black_seconds=1.5)
    with llm.use_backend(FakeBackend([no_issue])):
        qa.run(VIDEO_ID, workspace, output, config=config(tmp_path, black_min_seconds=2.0))
    assert "black_screen" not in local_types(path)
    assert read(path)["qa"]["status"] == "passed"


def test_ffmpeg_failure_on_black_detection_raises_no_fallback_verdict(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)
    before = path.read_text(encoding="utf-8")
    with llm.use_backend(FakeBackend([no_issue])):
        with pytest.raises(qa.QAError):
            run(tmp_path, workspace, output, ffmpeg_bin="ffmpeg-binaire-absent")
    assert path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# TASK-2fc8 : controles adaptes au format letterbox (SPEC-6127)
# --------------------------------------------------------------------------


def test_letterbox_schema_excludes_face_cut_and_subtitle_on_face(tmp_path, dirs):
    workspace, output = dirs
    write_clip_letterbox(output)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    enum = fake.calls[0].schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert set(enum) == {"incomprehensible", "starts_mid_sentence", "weak_hook"}


def test_letterbox_prompt_uses_screen_title_not_hook_and_describes_format(tmp_path, dirs):
    workspace, output = dirs
    write_clip_letterbox(output, screen_title="Alerte ceci va vous surprendre", hook_text="ignore-moi")
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    prompt = fake.calls[0].prompt
    assert "Alerte ceci va vous surprendre" in prompt
    assert "ignore-moi" not in prompt
    assert "encadre blanc" in prompt
    assert "video" in prompt.lower()
    assert "sous-titres" in prompt


def test_letterbox_without_video_rect_raises(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)  # layout "single" par defaut, sans video_rect
    data = read(path)
    data["layout"] = "letterbox"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with llm.use_backend(FakeBackend([])):
        with pytest.raises(qa.QAError):
            run(tmp_path, workspace, output)
    assert path.read_text(encoding="utf-8") == before


def test_letterbox_black_screen_measured_on_video_rect_only(tmp_path, dirs):
    workspace, output = dirs
    path = write_letterbox_clip(output, black_seconds=3.5)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert local_types(path) == {"black_screen"}
    assert read(path)["qa"]["status"] == "rejected"
    assert read(path)["ready"] is False


def test_letterbox_short_black_in_video_rect_is_not_rejected(tmp_path, dirs):
    workspace, output = dirs
    path = write_letterbox_clip(output, black_seconds=0.4)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert "black_screen" not in local_types(path)
    assert read(path)["qa"]["status"] == "passed"


# --------------------------------------------------------------------------
# TASK-4b1b : partie 2+ d'une serie, la reprise de ~3 s n'est pas un defaut
# (SPEC-1557 regle 3 : le recouvrement entre parties est voulu)
# --------------------------------------------------------------------------


def write_clip_part(output_dir, *, part, parts_total, clip_id=CLIP_ID, **mp4_kwargs):
    d = output_dir / VIDEO_ID
    colors = mp4_kwargs.get("colors", ("red",))
    duration = mp4_kwargs.get("seg", 1.5) * len(colors)
    make_mp4(d / f"{clip_id}.mp4", **mp4_kwargs)
    (d / f"{clip_id}.json").write_text(
        json.dumps(clip_json(duration=duration, clip_id=clip_id, part=part, parts_total=parts_total)),
        encoding="utf-8",
    )
    return d / f"{clip_id}.json"


def write_clip_letterbox_part(
    output_dir, *, part, parts_total, clip_id=CLIP_ID, video_rect=LETTERBOX_RECT,
    screen_title="La suite de l'histoire", hook_text="ignore-moi", **mp4_kwargs
):
    d = output_dir / VIDEO_ID
    colors = mp4_kwargs.get("colors", ("red",))
    duration = mp4_kwargs.get("seg", 1.5) * len(colors)
    make_mp4(d / f"{clip_id}.mp4", **mp4_kwargs)
    (d / f"{clip_id}.json").write_text(
        json.dumps(clip_json(
            duration=duration, clip_id=clip_id, layout="letterbox", video_rect=video_rect,
            screen_title=screen_title, hook_text=hook_text, part=part, parts_total=parts_total,
        )),
        encoding="utf-8",
    )
    return d / f"{clip_id}.json"


def test_series_part_schema_excludes_starts_mid_sentence(tmp_path, dirs):
    workspace, output = dirs
    write_clip_part(output, part=2, parts_total=3)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    enum = fake.calls[0].schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert "starts_mid_sentence" not in enum
    assert set(enum) == {"incomprehensible", "face_cut", "subtitle_on_face", "weak_hook"}


def test_series_part_prompt_mentions_continuation_and_drops_defect_from_list(tmp_path, dirs):
    workspace, output = dirs
    write_clip_part(output, part=2, parts_total=3)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    prompt = fake.calls[0].prompt
    assert "partie 2" in prompt.lower()
    assert "serie" in prompt.lower() or "série" in prompt.lower()
    assert "starts_mid_sentence" not in prompt


def test_first_part_of_series_still_asks_starts_mid_sentence(tmp_path, dirs):
    workspace, output = dirs
    write_clip_part(output, part=1, parts_total=3)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    enum = fake.calls[0].schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert "starts_mid_sentence" in enum
    assert "partie" not in fake.calls[0].prompt.lower()


def test_single_clip_still_asks_starts_mid_sentence(tmp_path, dirs):
    workspace, output = dirs
    write_clip(output)  # part=1, parts_total=1 par defaut (clip_json)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    enum = fake.calls[0].schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert "starts_mid_sentence" in enum


def test_letterbox_series_part_excludes_all_three(tmp_path, dirs):
    workspace, output = dirs
    write_clip_letterbox_part(output, part=2, parts_total=2)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    enum = fake.calls[0].schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert set(enum) == {"incomprehensible", "weak_hook"}


# --------------------------------------------------------------------------
# TASK-4eb4 : rejeter seulement un clip incomprehensible ou casse ; le reste
# devient un avertissement (severity = blocking | warning)
# --------------------------------------------------------------------------


def issues_by_type(path):
    return {i["type"]: i for i in read(path)["qa"]["issues"]}


def test_starts_mid_sentence_alone_passes_with_warning(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)
    answer = {"issues": [{"type": "starts_mid_sentence", "detail": "commence par 'et donc'"}]}
    with llm.use_backend(FakeBackend([answer])):
        run(tmp_path, workspace, output)
    data = read(path)
    assert data["qa"]["status"] == "passed"
    assert data["qa"]["issues"] == [{
        "type": "starts_mid_sentence", "detail": "commence par 'et donc'",
        "source": "llm", "severity": "warning",
    }]
    assert data["ready"] is True
    assert qa.is_ready(data) is True


@pytest.mark.parametrize("defect", ["weak_hook", "face_cut", "subtitle_on_face"])
def test_other_llm_defects_are_warnings(tmp_path, dirs, defect):
    workspace, output = dirs
    path = write_clip(output)
    with llm.use_backend(FakeBackend([{"issues": [{"type": defect, "detail": "vu image 1"}]}])):
        run(tmp_path, workspace, output)
    assert read(path)["qa"]["status"] == "passed"
    assert issues_by_type(path)[defect]["severity"] == "warning"


def test_incomprehensible_rejects(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output)
    answer = {"issues": [{"type": "incomprehensible", "detail": "on ne sait pas qui est 'il'"}]}
    with llm.use_backend(FakeBackend([answer])):
        run(tmp_path, workspace, output)
    data = read(path)
    assert data["qa"]["status"] == "rejected"
    assert data["qa"]["issues"] == [{
        "type": "incomprehensible", "detail": "on ne sait pas qui est 'il'",
        "source": "llm", "severity": "blocking",
    }]
    assert data["ready"] is False


def test_incomprehensible_is_asked_in_every_format(tmp_path, dirs):
    workspace, output = dirs
    write_clip_letterbox_part(output, part=2, parts_total=3)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    enum = fake.calls[0].schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert "incomprehensible" in enum


def test_prompt_says_only_incomprehensible_blocks_and_later_parts_may_assume_previous(tmp_path, dirs):
    workspace, output = dirs
    write_clip_part(output, part=2, parts_total=3)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    prompt = fake.calls[0].prompt
    assert "incomprehensible" in prompt
    assert "seul" in prompt.lower() and "bloquant" in prompt.lower()
    assert "parties precedentes" in prompt.lower()


def test_short_black_1_2s_passes_with_warning(tmp_path, dirs):
    workspace, output = dirs
    path = write_black_clip(output, black_seconds=1.2)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    data = read(path)
    assert data["qa"]["status"] == "passed"
    assert issues_by_type(path)["black_screen"]["severity"] == "warning"
    assert data["ready"] is True


def test_long_black_4s_rejects(tmp_path, dirs):
    workspace, output = dirs
    path = write_black_clip(output, black_seconds=4.0)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert read(path)["qa"]["status"] == "rejected"
    assert issues_by_type(path)["black_screen"]["severity"] == "blocking"


def test_black_block_seconds_is_configurable(tmp_path, dirs):
    workspace, output = dirs
    path = write_black_clip(output, black_seconds=1.5)
    with llm.use_backend(FakeBackend([no_issue])):
        qa.run(VIDEO_ID, workspace, output, config=config(tmp_path, black_block_seconds=1.2))
    assert read(path)["qa"]["status"] == "rejected"
    assert qa.CONFIG_DEFAULTS["black_block_seconds"] == 3.0


def test_wrong_resolution_is_blocking(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output, size="720x1280")
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert read(path)["qa"]["status"] == "rejected"
    assert issues_by_type(path)["resolution"]["severity"] == "blocking"


def test_duration_and_leading_silence_are_blocking(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip(output, audio=False)
    data = read(path)
    data["duration"] = 30.0
    path.write_text(json.dumps(data), encoding="utf-8")
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    found = issues_by_type(path)
    assert found["duration"]["severity"] == "blocking"
    assert found["leading_silence"]["severity"] == "blocking"
    assert read(path)["qa"]["status"] == "rejected"


def test_rejected_part_of_series_warns_the_other_parts(tmp_path, dirs):
    workspace, output = dirs
    p1 = write_clip_part(output, part=1, parts_total=3, clip_id="03-p1")
    p2 = write_clip_part(output, part=2, parts_total=3, clip_id="03-p2")
    p3 = write_clip_part(output, part=3, parts_total=3, clip_id="03-p3")
    solo = write_clip(output, clip_id="04")
    bad = {"issues": [{"type": "incomprehensible", "detail": "l'histoire ne se suit pas"}]}
    with llm.use_backend(FakeBackend([no_issue, bad, no_issue, no_issue])):
        run(tmp_path, workspace, output)
    assert read(p2)["qa"]["status"] == "rejected"
    assert [i["type"] for i in read(p2)["qa"]["issues"]] == ["incomprehensible"]
    for path in (p1, p3):
        data = read(path)
        assert data["qa"]["status"] == "passed"
        assert data["ready"] is True
        series = [i for i in data["qa"]["issues"] if i["type"] == "series_part_rejected"]
        assert len(series) == 1
        assert series[0]["severity"] == "warning"
        assert "03-p2" in series[0]["detail"]
    assert read(solo)["qa"]["issues"] == []


def test_series_warning_is_not_duplicated_on_rerun(tmp_path, dirs):
    workspace, output = dirs
    p1 = write_clip_part(output, part=1, parts_total=2, clip_id="03-p1")
    write_clip_part(output, part=2, parts_total=2, clip_id="03-p2")
    bad = {"issues": [{"type": "incomprehensible", "detail": "flou"}]}
    with llm.use_backend(FakeBackend([no_issue, bad])):
        run(tmp_path, workspace, output)
    with llm.use_backend(FakeBackend([])):
        run(tmp_path, workspace, output)
    types = [i["type"] for i in read(p1)["qa"]["issues"]]
    assert types == ["series_part_rejected"]


# --------------------------------------------------------------------------
# TASK-1366 : clips controles en parallele (au plus `parallel` a la fois)
# --------------------------------------------------------------------------


def test_parallel_config_default_is_four():
    assert qa.CONFIG_DEFAULTS["parallel"] == 4


def test_parallel_zero_is_refused(tmp_path, dirs):
    workspace, output = dirs
    write_clip(output)
    with llm.use_backend(FakeBackend([no_issue])) as fake:
        with pytest.raises(qa.QAError):
            qa.run(VIDEO_ID, workspace, output, config=config(tmp_path, parallel=0))
    assert fake.calls == []


class _ConcurrencyBackend:
    """Backend qui ne repond rien d'utile : il mesure combien d'appels a
    ``complete`` sont en cours simultanement (verrou + compteur), pour
    verifier le chevauchement des appels LLM selon ``parallel``. Chaque
    appel bloque jusqu'a ce que ``expected`` appels soient simultanement
    actifs (ou un timeout genereux), au lieu d'un sleep fixe qui peut rater
    la fenetre de recouvrement sous forte charge CPU partagee -- le
    scheduling des threads n'est alors plus garanti dans un court delai fixe
    (cf. TASK-42a46cb23f78)."""

    def __init__(self, expected: int = 1, timeout: float = 10.0):
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.calls = 0
        self._expected = expected
        self._reached = threading.Event()
        self._timeout = timeout

    def complete(self, request):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if self._active >= self._expected:
                self._reached.set()
        self._reached.wait(self._timeout)
        with self._lock:
            self._active -= 1
            self.calls += 1
        return json.dumps({"issues": []})


def _write_four_clips(output):
    for clip_id in ("01", "02", "03", "04"):
        write_clip(output, clip_id=clip_id)


def test_parallel_four_overlaps_llm_calls(tmp_path, dirs):
    workspace, output = dirs
    _write_four_clips(output)
    backend = _ConcurrencyBackend(expected=2)
    with llm.use_backend(backend):
        qa.run(VIDEO_ID, workspace, output, config=config(tmp_path, parallel=4))
    assert backend.calls == 4
    assert backend.max_active >= 2


def test_parallel_one_never_overlaps_llm_calls(tmp_path, dirs):
    workspace, output = dirs
    _write_four_clips(output)
    backend = _ConcurrencyBackend(expected=1)
    with llm.use_backend(backend):
        qa.run(VIDEO_ID, workspace, output, config=config(tmp_path, parallel=1))
    assert backend.calls == 4
    assert backend.max_active == 1


def _routed_answer(request):
    """Reponse deterministe d'apres le clip (nom du dossier d'images), pas
    d'apres l'ordre d'appel : les appels concurrents n'ont pas d'ordre fixe."""
    stem = request.images[0].parent.name
    if stem == "02":
        return {"issues": [{"type": "weak_hook", "detail": "accroche faible"}]}
    return {"issues": []}


def test_json_identical_between_parallel_one_and_four(tmp_path):
    results = {}
    for parallel in (1, 4):
        base = tmp_path / f"p{parallel}"
        output = base / "output"
        (output / VIDEO_ID).mkdir(parents=True)
        workspace = base / "workspace"
        for clip_id in ("01", "02", "03"):
            write_clip(output, clip_id=clip_id)
        with llm.use_backend(FakeBackend([_routed_answer])):
            qa.run(VIDEO_ID, workspace, output, config=config(base, parallel=parallel))
        results[parallel] = {
            clip_id: read(output / VIDEO_ID / f"{clip_id}.json") for clip_id in ("01", "02", "03")
        }
    assert results[1] == results[4]


def test_clip_llm_failure_raises_but_other_clips_are_written_and_no_series_warning(tmp_path, dirs):
    workspace, output = dirs
    p1 = write_clip_part(output, part=1, parts_total=3, clip_id="06-p1")
    p2 = write_clip_part(output, part=2, parts_total=3, clip_id="06-p2")
    p3 = write_clip_part(output, part=3, parts_total=3, clip_id="06-p3")

    def answer(request):
        stem = request.images[0].parent.name
        if stem == "06-p1":
            return {"issues": [{"type": "incomprehensible", "detail": "histoire decousue"}]}
        if stem == "06-p2":
            raise llm.TransientLLMError("quota")
        return {"issues": []}

    with llm.use_backend(FakeBackend([answer])):
        with pytest.raises(llm.TransientLLMError):
            qa.run(VIDEO_ID, workspace, output, config=config(tmp_path, parallel=4))

    assert read(p1)["qa"]["status"] == "rejected"
    assert read(p3)["qa"]["status"] == "passed"
    # _warn_series pas applique (le run a echoue) : p3 n'est pas averti du
    # rejet de p1 bien que p1 soit bien rejete.
    assert "series_part_rejected" not in [i["type"] for i in read(p3)["qa"]["issues"]]
    assert read(p2)["qa"]["status"] == "skipped"


# --------------------------------------------------------------------------
# TASK-9e0c : clip stream (SPEC-3a88) : facecam agrandie en haut
# (camera_rect), jeu en bas (video_rect), titre d'ecran permanent.
# --------------------------------------------------------------------------

STREAM_CAMERA_RECT = {"x": 0, "y": 440, "w": 1080, "h": 768}
STREAM_GAME_RECT = {"x": 0, "y": 1208, "w": 1080, "h": 712}


def write_stream_clip(output_dir, *, black_seconds=0.4, clip_id=CLIP_ID, **overrides):
    """Clip stream synthetique : fond blanc (titre d'ecran), camera rouge (noire
    ``black_seconds`` au milieu), jeu vert dessine en dessous."""
    d = output_dir / VIDEO_ID
    total = make_letterbox_mp4(d / f"{clip_id}.mp4", black_seconds=black_seconds, video_rect=STREAM_CAMERA_RECT)
    tmp = d / "cam.mp4"
    g = STREAM_GAME_RECT
    (d / f"{clip_id}.mp4").rename(tmp)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp),
         "-vf", f"drawbox=x={g['x']}:y={g['y']}:w={g['w']}:h={g['h']}:color=green:t=fill",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "copy",
         str(d / f"{clip_id}.mp4")],
        check=True,
    )
    tmp.unlink()
    data = clip_json(
        duration=total, clip_id=clip_id, layout="stream", camera_rect=dict(STREAM_CAMERA_RECT),
        video_rect=dict(STREAM_GAME_RECT), screen_title="Il ouvre la porte", hook_text="ignore-moi",
    )
    data.update(overrides)
    (d / f"{clip_id}.json").write_text(json.dumps(data), encoding="utf-8")
    return d / f"{clip_id}.json"


def test_valid_stream_clip_passes(tmp_path, dirs):
    workspace, output = dirs
    path = write_stream_clip(output)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    data = read(path)
    assert data["qa"] == {"status": "passed", "issues": []}
    assert qa.is_ready(data)


def test_stream_prompt_describes_the_format_uses_screen_title_and_keeps_face_defects(tmp_path, dirs):
    workspace, output = dirs
    write_stream_clip(output)
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        run(tmp_path, workspace, output)
    prompt = fake.calls[0].prompt
    assert "Il ouvre la porte" in prompt
    assert "ignore-moi" not in prompt
    assert "facecam" in prompt
    enum = fake.calls[0].schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    # le visage est a l'image en stream : visage coupe et sous-titre dessus restent demandes
    # (empty_webcam n'est demande qu'en stream_split, TASK-9957)
    assert set(enum) == set(qa.DEFECTS) - {"empty_webcam"}


def test_stream_black_screen_measured_on_the_camera_panel(tmp_path, dirs):
    workspace, output = dirs
    path = write_stream_clip(output, black_seconds=3.5)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert local_types(path) == {"black_screen"}
    assert read(path)["qa"]["status"] == "rejected"


@pytest.mark.parametrize("overrides", [
    {"camera_rect": None},
    {"video_rect": None},
    {"camera_rect": {"x": 0, "y": 440, "w": 1080, "h": 900}},  # recouvre le jeu
    {"video_rect": {"x": 0, "y": 1208, "w": 1080, "h": 800}},  # deborde du 1080x1920
])
def test_stream_with_missing_or_inconsistent_rects_raises(tmp_path, dirs, overrides):
    workspace, output = dirs
    path = write_stream_clip(output, **overrides)
    before = path.read_text(encoding="utf-8")
    with llm.use_backend(FakeBackend([])):
        with pytest.raises(qa.QAError, match="stream"):
            run(tmp_path, workspace, output)
    assert path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# TASK-e8f2 : clip stream_split (SPEC-76dc, webcam en haut, jeu en bas)
# controle selon son vrai format
# --------------------------------------------------------------------------

SPLIT_WEBCAM = {"x": 0, "y": 100, "w": 1080, "h": 700}
SPLIT_GAME = {"x": 0, "y": 900, "w": 1080, "h": 800}


def make_split_mp4(path, *, black_panel=None, black_seconds=3.5, segment_seconds=1.0, size="1080x1920"):
    """Fond blanc, deux panneaux rouges (webcam, jeu) ; le panneau nomme par
    ``black_panel`` ("webcam" | "game") passe au noir pendant ``black_seconds``."""
    total = round(segment_seconds * 2 + black_seconds, 3)
    panels = {"webcam": SPLIT_WEBCAM, "game": SPLIT_GAME}

    def seg(dur, black):
        boxes = ",".join(
            f"drawbox=x={r['x']}:y={r['y']}:w={r['w']}:h={r['h']}:"
            f"color={'black' if name == black else 'red'}:t=fill"
            for name, r in panels.items()
        )
        return f"color=c=white:s={size}:r=30:d={dur},{boxes}"

    args = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", seg(segment_seconds, None),
        "-f", "lavfi", "-i", seg(black_seconds, black_panel),
        "-f", "lavfi", "-i", seg(segment_seconds, None),
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={total}",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-t", str(total),
        "-c:a", "aac", "-ar", "48000", str(path),
    ]
    subprocess.run(args, check=True)
    return total


def write_split_clip(output_dir, *, black_panel=None, clip_id=CLIP_ID, **overrides):
    d = output_dir / VIDEO_ID
    total = make_split_mp4(d / f"{clip_id}.mp4", black_panel=black_panel)
    fields = {
        "layout": "stream_split", "webcam_rect": SPLIT_WEBCAM, "video_rect": SPLIT_GAME,
        "screen_title": "Titre d'ecran du split", "hook_text": "ignore-moi",
    }
    fields.update(overrides)
    fields = {k: v for k, v in fields.items() if v is not _MISSING}
    (d / f"{clip_id}.json").write_text(
        json.dumps(clip_json(duration=total, clip_id=clip_id, **fields)), encoding="utf-8",
    )
    return d / f"{clip_id}.json"


_MISSING = object()


def ask_prompt(tmp_path, workspace, output, cfg=None):
    fake = FakeBackend([no_issue])
    with llm.use_backend(fake):
        qa.run(VIDEO_ID, workspace, output, config=cfg or config(tmp_path))
    return fake.calls[0]


def test_split_prompt_has_format_section_without_phantom_hook(tmp_path, dirs):
    workspace, output = dirs
    write_split_clip(output)
    prompt = ask_prompt(tmp_path, workspace, output).prompt
    assert "## Format" in prompt
    assert "webcam en haut" in prompt
    assert "jeu en bas" in prompt
    assert "badge" in prompt
    assert "2 premieres secondes" not in prompt
    assert "ignore-moi" not in prompt
    assert "Titre d'ecran du split" in prompt


def test_split_prompt_without_title_does_not_mention_screen_title(tmp_path, dirs):
    workspace, output = dirs
    write_split_clip(output)
    cfg = Config(
        mode="auto", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={"qa": {"parallel": 1}, "render": {"title_enabled": False}},
    )
    prompt = ask_prompt(tmp_path, workspace, output, cfg).prompt
    assert "## Format" in prompt
    assert "Titre d'ecran du split" not in prompt
    assert "2 premieres secondes" not in prompt
    assert "titre d'ecran affiche" not in prompt.lower()


def test_split_schema_keeps_face_cut_and_subtitle_on_face(tmp_path, dirs):
    workspace, output = dirs
    write_split_clip(output)
    call = ask_prompt(tmp_path, workspace, output)
    enum = call.schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert set(enum) == set(qa.DEFECTS)


@pytest.mark.parametrize("panel", ["webcam", "game"])
def test_split_black_screen_measured_on_each_panel(tmp_path, dirs, panel):
    workspace, output = dirs
    path = write_split_clip(output, black_panel=panel)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert local_types(path) == {"black_screen"}
    assert read(path)["qa"]["status"] == "rejected"


def test_split_without_black_is_passed(tmp_path, dirs):
    workspace, output = dirs
    path = write_split_clip(output)
    with llm.use_backend(FakeBackend([no_issue])):
        run(tmp_path, workspace, output)
    assert read(path)["qa"]["status"] == "passed"


@pytest.mark.parametrize("missing", ["webcam_rect", "video_rect"])
def test_split_missing_rect_raises(tmp_path, dirs, missing):
    workspace, output = dirs
    path = write_split_clip(output, **{missing: _MISSING})
    before = path.read_text(encoding="utf-8")
    with llm.use_backend(FakeBackend([])):
        with pytest.raises(qa.QAError, match=missing):
            run(tmp_path, workspace, output)
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("rects", [
    {"webcam_rect": {"x": 0, "y": 100, "w": 1080, "h": 3000}},
    {"video_rect": {"x": 0, "y": 600, "w": 1080, "h": 800}},
    {"webcam_rect": {"x": 0, "y": 100, "w": 0, "h": 700}},
])
def test_split_invalid_rect_raises(tmp_path, dirs, rects):
    workspace, output = dirs
    write_split_clip(output, **rects)
    with llm.use_backend(FakeBackend([])):
        with pytest.raises(qa.QAError):
            run(tmp_path, workspace, output)


# Prompts figes d'avant TASK-e8f2 : letterbox, stream et crop ne changent pas.
_LETTERBOX_PROMPT_PARTS = (
    "## Format\n"
    "Clip letterbox : titre d'ecran sur encadre blanc en haut, video zoomee au centre "
    "(fond flou de la video autour), sous-titres dans la bande du bas. Le zoom rogne "
    "volontairement les bords et les sous-titres sont hors de l'image : normal, ne "
    "pas le signaler.\n\n",
    "Titre d'ecran affiche en permanence (accroche) : T\n",
)
_STREAM_PROMPT_PARTS = (
    "## Format\n"
    "Clip stream : titre d'ecran sur encadre blanc en haut, la facecam (webcam du "
    "createur) agrandie dessous, le jeu ou l'ecran en bas, sous-titres sur le jeu.\n\n",
    "Titre d'ecran affiche en permanence (accroche) : T\n",
)
_CROP_PROMPT_HOOK = "Texte d'accroche affiche les 2 premieres secondes : H\n"


def _prompt_for(**fields):
    clip = clip_json(duration=3.0, **fields)
    return qa._prompt(clip, [(Path("a.jpg"), 0.0, ["debut"])], letterbox=clip.get("layout") == "letterbox")


def test_letterbox_stream_crop_prompts_unchanged():
    letterbox = _prompt_for(layout="letterbox", screen_title="T", hook_text="H")
    assert all(part in letterbox for part in _LETTERBOX_PROMPT_PARTS)
    stream = _prompt_for(layout="stream", screen_title="T", hook_text="H")
    assert all(part in stream for part in _STREAM_PROMPT_PARTS)
    crop = _prompt_for(layout="crop", hook_text="H")
    assert _CROP_PROMPT_HOOK in crop
    assert "## Format" not in crop
    single = _prompt_for(layout="single", hook_text="H")
    assert _CROP_PROMPT_HOOK in single and "## Format" not in single


# --------------------------------------------------------------------------
# TASK-9957 : panneau webcam d'un clip stream_split sans webcam -> bloquant
# --------------------------------------------------------------------------


def _empty_webcam(fake_issue_detail="le panneau du haut montre l'interface du jeu"):
    return lambda call: {"issues": [{"type": "empty_webcam", "detail": fake_issue_detail}]}


def test_split_schema_and_prompt_ask_for_empty_webcam(tmp_path, dirs):
    workspace, output = dirs
    write_split_clip(output)
    call = ask_prompt(tmp_path, workspace, output)
    enum = call.schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert "empty_webcam" in enum
    assert "empty_webcam" in call.prompt
    assert "empty_webcam" in qa._BLOCKING_DEFECTS


@pytest.mark.parametrize("fields", [
    {"layout": "letterbox", "video_rect": {"x": 0, "y": 500, "w": 1080, "h": 900}},
    {"layout": "stream", "camera_rect": SPLIT_WEBCAM, "video_rect": SPLIT_GAME},
])
def test_empty_webcam_defect_only_asked_for_stream_split(fields):
    clip = clip_json(duration=3.0, screen_title="T", **fields)
    letterbox = clip["layout"] == "letterbox"
    schema = qa.response_schema(letterbox=letterbox, part=1)
    assert "empty_webcam" not in schema["properties"]["issues"]["items"]["properties"]["type"]["enum"]
    assert "empty_webcam" not in qa._prompt(clip, [(Path("a.jpg"), 0.0, ["debut"])], letterbox=letterbox)


def test_split_empty_webcam_blocks_and_rejects_the_clip(tmp_path, dirs):
    workspace, output = dirs
    path = write_split_clip(output)
    with llm.use_backend(FakeBackend([_empty_webcam()])):
        run(tmp_path, workspace, output)
    data = read(path)
    assert data["qa"]["status"] == "rejected"
    assert data["ready"] is False
    issue = data["qa"]["issues"][0]
    assert (issue["type"], issue["severity"], issue["source"]) == ("empty_webcam", "blocking", "llm")


# --------------------------------------------------------------------------
# TASK-0aff43d73607 (image-M4) : la QA lit ce que le rendu a dessine
# (sidecar title_shown), jamais la config courante.
# --------------------------------------------------------------------------


def test_split_prompt_follows_sidecar_title_shown_false_not_current_config(tmp_path, dirs):
    workspace, output = dirs
    write_split_clip(output, title_shown=False)
    prompt = ask_prompt(tmp_path, workspace, output).prompt  # config par defaut : title_enabled = true
    assert "Titre d'ecran du split" not in prompt
    assert "titre d'ecran affiche" not in prompt.lower()


def test_split_prompt_follows_sidecar_title_shown_true_not_current_config(tmp_path, dirs):
    workspace, output = dirs
    write_split_clip(output, title_shown=True)
    cfg = Config(
        mode="auto", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={"qa": {"parallel": 1}, "render": {"title_enabled": False}},
    )
    prompt = ask_prompt(tmp_path, workspace, output, cfg).prompt
    assert "Titre d'ecran du split" in prompt


def test_letterbox_prompt_without_title_shown_does_not_cite_a_title(tmp_path, dirs):
    workspace, output = dirs
    path = write_clip_letterbox(output, screen_title="Titre fantome")
    path.write_text(json.dumps({**read(path), "title_shown": False}), encoding="utf-8")
    prompt = ask_prompt(tmp_path, workspace, output).prompt
    assert "Titre fantome" not in prompt
