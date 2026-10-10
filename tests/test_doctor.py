"""TASK-c68fe39d5bcf : clipper/doctor.py (SPEC-38f7 R7). Toutes les sondes
sont simulees : jamais de reseau, de vrai binaire ni de vrai modele."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from clipper import browser, doctor
from clipper.gpu import Device


def _which_all_present(name: str) -> str | None:
    return f"/usr/bin/{name}"


def _run_connected(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 0, stdout="logged in\n", stderr="")


def _run_not_connected(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 1, stdout="not logged in\n", stderr="")


def _chrome_found(config):
    return Path("C:/chrome.exe")


def _chrome_absent(config):
    raise browser.BrowserError("Chrome est introuvable : installe Google Chrome")


def _cuda_device() -> Device:
    return Device(type="cuda", compute_type="float16")


def _cpu_device() -> Device:
    return Device(type="cpu", compute_type="int8")


def _happy_kwargs(tmp_path: Path) -> dict:
    model_path = tmp_path / "blaze_face_short_range.tflite"
    model_path.write_bytes(b"fake")
    return dict(
        which=_which_all_present,
        run=_run_connected,
        chrome_finder=_chrome_found,
        device_factory=_cuda_device,
        nvidia_present=lambda: True,
        whisper_cache_probe=lambda name: True,
        mediapipe_path_fn=lambda config: model_path,
        whisper_name_fn=lambda config: "small",
        writable=lambda path: True,
    )


def _write_config(cwd: Path) -> None:
    (cwd / "config.toml").write_text('mode = "review"\n', encoding="utf-8")


def _point(points: list[dict], name: str) -> dict:
    return next(p for p in points if p["name"] == name)


# --------------------------------------------------------------------------
# tout present -> 0
# --------------------------------------------------------------------------


def test_report_everything_present_gives_exit_code_zero(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)

    points = doctor.report(isolated_cwd, **_happy_kwargs(tmp_path))

    assert doctor.exit_code(points) == 0
    assert all(p["status"] != doctor.STATUS_MISSING for p in points)


def test_report_has_one_point_per_documented_check(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)

    points = doctor.report(isolated_cwd, **_happy_kwargs(tmp_path))

    names = {p["name"] for p in points}
    assert names == {
        "python", "ffmpeg", "ffprobe", "claude", "chrome", "gpu", "nvenc",
        "mediapipe_model", "whisper_model", "config", "data_dirs",
    }  # nvenc : seulement quand le device est cuda (image-I3)


# --------------------------------------------------------------------------
# claude non connecte -> 1, remede "claude auth login"
# --------------------------------------------------------------------------


def test_report_claude_not_connected_is_missing_with_login_remedy(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["run"] = _run_not_connected

    points = doctor.report(isolated_cwd, **kwargs)

    claude = _point(points, "claude")
    assert claude["status"] == doctor.STATUS_MISSING
    assert claude["fix"] == "claude auth login"
    assert doctor.exit_code(points) == 1


def test_report_claude_absent_from_path_is_missing(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["which"] = lambda name: None if name == "claude" else f"/usr/bin/{name}"

    points = doctor.report(isolated_cwd, **kwargs)

    claude = _point(points, "claude")
    assert claude["status"] == doctor.STATUS_MISSING
    assert doctor.exit_code(points) == 1


# --------------------------------------------------------------------------
# Chrome absent -> 0, avertissement
# --------------------------------------------------------------------------


def test_report_chrome_absent_is_warning_only_exit_code_zero(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["chrome_finder"] = _chrome_absent

    points = doctor.report(isolated_cwd, **kwargs)

    chrome = _point(points, "chrome")
    assert chrome["status"] == doctor.STATUS_WARNING
    assert "Chrome" in chrome["fix"]
    assert doctor.exit_code(points) == 0


# --------------------------------------------------------------------------
# GPU absent -> avertissement seul, jamais un echec
# --------------------------------------------------------------------------


def test_report_gpu_on_cpu_is_warning_only_never_fails(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["device_factory"] = _cpu_device
    kwargs["nvidia_present"] = lambda: False

    points = doctor.report(isolated_cwd, **kwargs)

    gpu = _point(points, "gpu")
    assert gpu["status"] == doctor.STATUS_WARNING
    assert doctor.exit_code(points) == 0


# --------------------------------------------------------------------------
# ffmpeg / ffprobe / modeles / config : requis
# --------------------------------------------------------------------------


def test_report_ffmpeg_missing_fails(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["which"] = lambda name: None if name == "ffmpeg" else f"/usr/bin/{name}"

    points = doctor.report(isolated_cwd, **kwargs)

    assert _point(points, "ffmpeg")["status"] == doctor.STATUS_MISSING
    assert doctor.exit_code(points) == 1


def test_report_mediapipe_model_absent_gives_prefetch_remedy_and_fails(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["mediapipe_path_fn"] = lambda config: tmp_path / "absent.tflite"

    points = doctor.report(isolated_cwd, **kwargs)

    point = _point(points, "mediapipe_model")
    assert point["status"] == doctor.STATUS_MISSING
    assert "clipper models prefetch" in point["fix"]
    assert doctor.exit_code(points) == 1


def test_report_whisper_model_absent_gives_prefetch_remedy_and_fails(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["whisper_cache_probe"] = lambda name: False

    points = doctor.report(isolated_cwd, **kwargs)

    point = _point(points, "whisper_model")
    assert point["status"] == doctor.STATUS_MISSING
    assert "clipper models prefetch" in point["fix"]
    assert doctor.exit_code(points) == 1


def test_report_config_absent_gives_init_remedy_and_fails(isolated_cwd, tmp_path):
    points = doctor.report(isolated_cwd, **_happy_kwargs(tmp_path))  # pas de config.toml ecrit

    point = _point(points, "config")
    assert point["status"] == doctor.STATUS_MISSING
    assert "clipper init" in point["fix"]
    assert doctor.exit_code(points) == 1


def test_report_config_invalid_is_missing_with_the_config_error(isolated_cwd, tmp_path):
    (isolated_cwd / "config.toml").write_text("bogus_top_level_key = 1\n", encoding="utf-8")

    points = doctor.report(isolated_cwd, **_happy_kwargs(tmp_path))

    point = _point(points, "config")
    assert point["status"] == doctor.STATUS_MISSING
    assert "bogus_top_level_key" in point["detail"]
    assert doctor.exit_code(points) == 1


# --------------------------------------------------------------------------
# JSON (format_text / CLI)
# --------------------------------------------------------------------------


def test_format_text_has_one_line_per_point_and_a_remedy_line_when_present(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["chrome_finder"] = _chrome_absent

    points = doctor.report(isolated_cwd, **kwargs)
    text = doctor.format_text(points)

    for point in points:
        assert point["name"] in text
    assert "remede" in text  # le point chrome en a un


def test_points_are_json_serializable(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)

    points = doctor.report(isolated_cwd, **_happy_kwargs(tmp_path))

    decoded = json.loads(json.dumps(points, ensure_ascii=False))
    assert decoded == points


# --------------------------------------------------------------------------
# CLI : « clipper doctor [--json] »
# --------------------------------------------------------------------------


def test_cli_doctor_text_exit_code_matches_report(isolated_cwd, tmp_path, monkeypatch, capsys):
    _write_config(isolated_cwd)
    original_report = doctor.report
    monkeypatch.setattr(doctor, "report", lambda: original_report(isolated_cwd, **_happy_kwargs(tmp_path)))

    from clipper.__main__ import main

    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "ffmpeg" in out and "claude" in out


def test_cli_doctor_json_is_valid_and_reflects_a_failure(isolated_cwd, tmp_path, monkeypatch, capsys):
    kwargs = _happy_kwargs(tmp_path)
    kwargs["which"] = lambda name: None if name == "claude" else f"/usr/bin/{name}"
    _write_config(isolated_cwd)
    original_report = doctor.report
    monkeypatch.setattr(doctor, "report", lambda: original_report(isolated_cwd, **kwargs))

    from clipper.__main__ import main

    exit_code = main(["doctor", "--json"])

    out = capsys.readouterr().out
    points = json.loads(out)
    assert any(p["name"] == "claude" and p["status"] == doctor.STATUS_MISSING for p in points)
    assert exit_code == 1


# --------------------------------------------------------------------------
# TASK-0aff43d73607 (image-I3) : point « nvenc » quand le device est cuda,
# verifie cote ffmpeg (un encodage d'une image), jamais sur la foi de ctranslate2.
# --------------------------------------------------------------------------


def _run_nvenc(returncode, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if "h264_nvenc" in cmd:
            return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)
        return _run_connected(cmd, **kwargs)

    run.calls = calls
    return run


def test_report_nvenc_ok_when_cuda_and_ffmpeg_encodes_one_frame(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["run"] = _run_nvenc(0)

    points = doctor.report(isolated_cwd, **kwargs)

    assert _point(points, "nvenc")["status"] == doctor.STATUS_OK
    cmd = next(c for c in kwargs["run"].calls if "h264_nvenc" in c)
    assert cmd[0] == "/usr/bin/ffmpeg"


def test_report_nvenc_failure_is_a_warning_naming_ffmpeg_error_never_a_failure(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)
    kwargs["run"] = _run_nvenc(1, "Driver does not support the required nvenc API version\n")

    points = doctor.report(isolated_cwd, **kwargs)

    nvenc = _point(points, "nvenc")
    assert nvenc["status"] == doctor.STATUS_WARNING
    assert "Driver does not support" in nvenc["detail"]
    assert nvenc["fix"]
    assert doctor.exit_code(points) == 0


def test_report_nvenc_probe_unreachable_is_a_warning(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    kwargs = _happy_kwargs(tmp_path)

    def run(cmd, **kwargs_):
        if "h264_nvenc" in cmd:
            raise subprocess.TimeoutExpired(cmd, 20)
        return _run_connected(cmd, **kwargs_)

    kwargs["run"] = run
    points = doctor.report(isolated_cwd, **kwargs)

    assert _point(points, "nvenc")["status"] == doctor.STATUS_WARNING


def test_report_has_no_nvenc_point_on_cpu_or_without_ffmpeg(isolated_cwd, tmp_path):
    _write_config(isolated_cwd)
    cpu = _happy_kwargs(tmp_path)
    cpu["device_factory"] = _cpu_device
    assert "nvenc" not in {p["name"] for p in doctor.report(isolated_cwd, **cpu)}

    no_ffmpeg = _happy_kwargs(tmp_path)
    no_ffmpeg["which"] = lambda name: None if name == "ffmpeg" else f"/usr/bin/{name}"
    assert "nvenc" not in {p["name"] for p in doctor.report(isolated_cwd, **no_ffmpeg)}
