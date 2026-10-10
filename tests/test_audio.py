from __future__ import annotations

import json
import math

import numpy as np
import pytest


def test_compute_energy_db_matches_expected_rms_for_constant_amplitude_windows():
    from clipper.audio import compute_energy_db

    sample_rate = 8000
    amplitude = 0.5
    # 3 windows of 1 s each, constant-amplitude sine wave throughout.
    t = np.arange(3 * sample_rate) / sample_rate
    samples = (amplitude * np.sin(2 * math.pi * 440 * t)).astype(np.float32)
    expected_rms = amplitude / math.sqrt(2)
    expected_db = 20 * math.log10(expected_rms)

    energy_db = compute_energy_db(samples, sample_rate, window_seconds=1.0)

    assert len(energy_db) == 3
    for db in energy_db:
        assert db == pytest.approx(expected_db, abs=0.1)


def _synthetic_signal_with_bursts(sample_rate: int, duration_s: int, burst_times: list[int]):
    """Quiet background noise with a short, much louder burst starting at
    each time in ``burst_times`` (seconds)."""
    rng = np.random.default_rng(0)
    samples = (rng.uniform(-1, 1, duration_s * sample_rate) * 0.01).astype(np.float32)
    burst_len = int(0.5 * sample_rate)
    for t in burst_times:
        start = t * sample_rate
        samples[start : start + burst_len] += 0.9 * np.sin(
            2 * math.pi * 220 * np.arange(burst_len) / sample_rate
        ).astype(np.float32)
    return samples


def test_analyze_finds_known_bursts_within_one_second():
    from clipper.audio import analyze

    sample_rate = 8000
    burst_times = [5, 15, 25]
    samples = _synthetic_signal_with_bursts(sample_rate, duration_s=30, burst_times=burst_times)

    result = analyze(samples, sample_rate)

    peak_times = [p["timecode"] for p in result["peaks"]]
    for expected_t in burst_times:
        assert any(abs(pt - expected_t) <= 1 for pt in peak_times), (
            f"aucun pic pres de t={expected_t}s parmi {peak_times}"
        )


def test_analyze_finds_no_peaks_in_pure_silence():
    from clipper.audio import analyze

    sample_rate = 8000
    rng = np.random.default_rng(1)
    samples = (rng.uniform(-1, 1, 20 * sample_rate) * 0.01).astype(np.float32)

    result = analyze(samples, sample_rate)

    assert result["peaks"] == []


def test_analyze_peak_has_timecode_and_relative_intensity_fields():
    from clipper.audio import analyze

    sample_rate = 8000
    samples = _synthetic_signal_with_bursts(sample_rate, duration_s=15, burst_times=[7])

    result = analyze(samples, sample_rate)

    assert len(result["peaks"]) >= 1
    peak = result["peaks"][0]
    assert set(peak) == {"timecode", "relative_db"}
    assert isinstance(peak["timecode"], (int, float))
    assert peak["relative_db"] > 0


def test_run_writes_audio_json_at_workspace_video_id(isolated_cwd):
    from clipper.audio import run

    sample_rate = 8000
    samples = _synthetic_signal_with_bursts(sample_rate, duration_s=10, burst_times=[3])
    workspace_dir = isolated_cwd / "workspace"
    seen_video_paths = []

    def fake_extractor(video_path, sr):
        seen_video_paths.append(video_path)
        assert sr == sample_rate
        return samples

    result = run(
        "abc123",
        workspace_dir=workspace_dir,
        sample_rate=sample_rate,
        extractor=fake_extractor,
    )

    assert seen_video_paths == [workspace_dir / "abc123" / "abc123.mp4"]
    out_file = workspace_dir / "abc123" / "audio.json"
    assert json.loads(out_file.read_text(encoding="utf-8")) == result
    assert "energy_db" in result
    assert "peaks" in result


def test_run_skips_extraction_when_audio_json_already_present(isolated_cwd):
    from clipper.audio import run

    workspace_dir = isolated_cwd / "workspace"
    video_dir = workspace_dir / "abc123"
    video_dir.mkdir(parents=True)
    existing = {"window_seconds": 1.0, "energy_db": [1.0], "peaks": []}
    (video_dir / "audio.json").write_text(json.dumps(existing), encoding="utf-8")

    def extractor_that_must_not_be_called(video_path, sr):
        raise AssertionError("l'extraction ne doit pas avoir lieu : audio.json existe deja")

    result = run(
        "abc123",
        workspace_dir=workspace_dir,
        extractor=extractor_that_must_not_be_called,
    )

    assert result == existing


def test_run_force_recomputes_even_if_audio_json_present(isolated_cwd):
    from clipper.audio import run

    workspace_dir = isolated_cwd / "workspace"
    video_dir = workspace_dir / "abc123"
    video_dir.mkdir(parents=True)
    (video_dir / "audio.json").write_text(json.dumps({"stale": True}), encoding="utf-8")

    sample_rate = 8000
    samples = _synthetic_signal_with_bursts(sample_rate, duration_s=5, burst_times=[])
    called = []

    def fake_extractor(video_path, sr):
        called.append(video_path)
        return samples

    result = run(
        "abc123",
        workspace_dir=workspace_dir,
        sample_rate=sample_rate,
        extractor=fake_extractor,
        force=True,
    )

    assert called
    assert "stale" not in result


def test_config_defaults_declares_expected_settings():
    from clipper.audio import CONFIG_DEFAULTS

    assert "sample_rate" in CONFIG_DEFAULTS
    assert "window_seconds" in CONFIG_DEFAULTS
    assert "median_window_seconds" in CONFIG_DEFAULTS
    assert "peak_threshold_db" in CONFIG_DEFAULTS


def test_config_section_audio_resolves_via_clipper_config(isolated_cwd):
    from clipper.config import load_config

    (isolated_cwd / "config.toml").write_text(
        "[audio]\nsample_rate = 22050\n", encoding="utf-8"
    )

    config = load_config(isolated_cwd / "config.toml")

    section = config.section("audio")
    assert section["sample_rate"] == 22050
    assert section["window_seconds"] == 1.0


# -- audio sur la ligne de temps du conteneur (TASK-4880) ---------------------

import shutil
import subprocess

_needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent")


def _gap_file(tmp_path, gap: bool):
    """Sine 4 s en aac ; avec `gap`, pts decales de +2 s a T=2 (conteneur de 6 s)."""
    out = tmp_path / "src.mka"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
    if gap:
        cmd += ["-af", "asetpts='if(gte(PTS,2/TB),PTS+2/TB,PTS)'"]
    subprocess.run(cmd + ["-c:a", "aac", "-f", "matroska", str(out)], check=True)
    return out


@_needs_ffmpeg
def test_extract_samples_without_gap_keeps_the_duration(tmp_path):
    from clipper.audio import _extract_samples_ffmpeg

    samples = _extract_samples_ffmpeg(_gap_file(tmp_path, False), 16000)
    assert abs(len(samples) / 16000 - 4.0) < 0.1


@_needs_ffmpeg
def test_extract_samples_fills_a_pts_gap_with_silence(tmp_path):
    from clipper.audio import _extract_samples_ffmpeg

    src = _gap_file(tmp_path, True)
    container = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
        check=True, capture_output=True, text=True).stdout.strip())
    assert container > 5.9

    s = _extract_samples_ffmpeg(src, 16000)

    assert abs(len(s) / 16000 - container) < 0.15
    assert np.abs(s[int(2.3 * 16000):int(3.7 * 16000)]).max() < 0.01
    assert np.abs(s[int(4.3 * 16000):int(5.3 * 16000)]).max() > 0.05


def _failing_replace(*args, **kwargs):
    raise OSError("coupure simulee pendant le remplacement")


def test_run_interrupted_write_leaves_no_audio_json(isolated_cwd, monkeypatch):
    from clipper import audio

    workspace_dir = isolated_cwd / "workspace"
    samples = _synthetic_signal_with_bursts(8000, duration_s=3, burst_times=[])
    monkeypatch.setattr("os.replace", _failing_replace)

    with pytest.raises(OSError):
        audio.run(
            "abc123", workspace_dir=workspace_dir, sample_rate=8000,
            extractor=lambda p, sr: samples,
        )

    assert not (workspace_dir / "abc123" / "audio.json").exists()


def test_run_interrupted_forced_write_keeps_previous_audio_json(isolated_cwd, monkeypatch):
    from clipper import audio

    workspace_dir = isolated_cwd / "workspace"
    video_dir = workspace_dir / "abc123"
    video_dir.mkdir(parents=True)
    (video_dir / "audio.json").write_text(json.dumps({"old": True}), encoding="utf-8")
    samples = _synthetic_signal_with_bursts(8000, duration_s=3, burst_times=[])
    monkeypatch.setattr("os.replace", _failing_replace)

    with pytest.raises(OSError):
        audio.run(
            "abc123", workspace_dir=workspace_dir, sample_rate=8000,
            extractor=lambda p, sr: samples, force=True,
        )

    assert json.loads((video_dir / "audio.json").read_text(encoding="utf-8")) == {"old": True}


def test_run_truncated_audio_json_raises_explicit_error_naming_file_and_force(isolated_cwd):
    from clipper.audio import AudioError, run

    workspace_dir = isolated_cwd / "workspace"
    video_dir = workspace_dir / "abc123"
    video_dir.mkdir(parents=True)
    (video_dir / "audio.json").write_text('{"window_seconds": 1.0, "energy_', encoding="utf-8")

    with pytest.raises(AudioError) as excinfo:
        run("abc123", workspace_dir=workspace_dir, extractor=lambda p, sr: None)

    message = str(excinfo.value)
    assert "audio.json" in message
    assert "--force" in message


# --- media-I4 : audio en flux, mémoire bornée à une fenêtre ---------------


def _old_energy_db(samples, sample_rate, window_seconds):
    """Ancienne implémentation (tableau complet), référence d'identité."""
    window_size = max(1, int(round(window_seconds * sample_rate)))
    n_windows = -(-len(samples) // window_size) if len(samples) else 0
    out = []
    for i in range(n_windows):
        chunk = samples[i * window_size : (i + 1) * window_size]
        rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))
        out.append(float(20 * np.log10(rms)) if rms > 0 else -120.0)
    return out


def test_run_streaming_extractor_writes_same_audio_json_as_full_array(isolated_cwd):
    from clipper.audio import analyze, run

    sample_rate = 8000
    samples = _synthetic_signal_with_bursts(sample_rate, duration_s=33, burst_times=[5, 15, 25])
    samples = samples[: len(samples) - 1234]  # dernière fenêtre incomplète
    window = sample_rate
    biggest = []

    def streaming_extractor(video_path, sr):
        # blocs de tailles irrégulières : le rechargement en fenêtres est de run
        pos = 0
        sizes = [window // 3, window * 2 + 7, window, 5]
        k = 0
        while pos < len(samples):
            size = sizes[k % len(sizes)]
            block = samples[pos : pos + size]
            biggest.append(len(block))
            yield block
            pos += size
            k += 1

    result = run(
        "s1",
        workspace_dir=isolated_cwd / "ws_stream",
        sample_rate=sample_rate,
        extractor=streaming_extractor,
    )

    expected = analyze(samples, sample_rate)
    assert result == expected
    assert result["energy_db"] == _old_energy_db(samples, sample_rate, 1.0)
    out = (isolated_cwd / "ws_stream" / "s1" / "audio.json").read_bytes()
    ref = (isolated_cwd / "ws_ref")
    run("s1", workspace_dir=ref, sample_rate=sample_rate, extractor=lambda p, sr: samples)
    assert out == (ref / "s1" / "audio.json").read_bytes()


def test_run_never_builds_an_array_larger_than_one_window(isolated_cwd, monkeypatch):
    from clipper import audio

    sample_rate = 8000
    window = sample_rate
    samples = _synthetic_signal_with_bursts(sample_rate, duration_s=20, burst_times=[7])
    seen = []
    real = audio.compute_energy_db

    def spy(chunk, sr, ws):
        seen.append(len(chunk))
        return real(chunk, sr, ws)

    monkeypatch.setattr(audio, "compute_energy_db", spy)

    def extractor(video_path, sr):
        for i in range(0, len(samples), window):
            yield samples[i : i + window]

    audio.run("s2", workspace_dir=isolated_cwd / "ws", sample_rate=sample_rate, extractor=extractor)

    assert seen and max(seen) <= window


@_needs_ffmpeg
def test_iter_samples_ffmpeg_yields_blocks_no_larger_than_requested(tmp_path):
    from clipper.audio import _extract_samples_ffmpeg, _iter_samples_ffmpeg

    src = _gap_file(tmp_path, False)
    blocks = list(_iter_samples_ffmpeg(src, 16000, block_samples=16000))

    assert blocks and max(len(b) for b in blocks) <= 16000
    assert all(b.dtype == np.float32 for b in blocks)
    assert np.array_equal(np.concatenate(blocks), _extract_samples_ffmpeg(src, 16000))


def test_iter_samples_ffmpeg_missing_binary_raises_audio_error(tmp_path):
    from clipper.audio import AudioError, _iter_samples_ffmpeg

    with pytest.raises(AudioError):
        list(_iter_samples_ffmpeg(tmp_path / "x.mp4", 16000, ffmpeg_bin="ffmpeg-introuvable-zz"))
