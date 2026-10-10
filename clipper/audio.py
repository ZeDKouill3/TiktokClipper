from __future__ import annotations

import functools
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Iterator

import numpy as np

from clipper.channel import atomic_write_json

CONFIG_DEFAULTS: dict[str, object] = {
    "sample_rate": 16000,
    "window_seconds": 1.0,
    "median_window_seconds": 15.0,
    "peak_threshold_db": 6.0,
}

_SILENCE_FLOOR_DB = -120.0


class AudioError(Exception):
    """ffmpeg couldn't decode the video's audio track."""


def compute_energy_db(samples: np.ndarray, sample_rate: int, window_seconds: float) -> list[float]:
    """RMS energy in dB for each non-overlapping window of ``window_seconds``.

    A silent window (rms == 0) is floored at _SILENCE_FLOOR_DB instead of
    producing -inf.
    """
    window_size = max(1, int(round(window_seconds * sample_rate)))
    n_windows = -(-len(samples) // window_size) if len(samples) else 0

    energy_db: list[float] = []
    for i in range(n_windows):
        chunk = samples[i * window_size : (i + 1) * window_size]
        rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))
        db = 20 * np.log10(rms) if rms > 0 else _SILENCE_FLOOR_DB
        energy_db.append(float(db))
    return energy_db


def find_peaks(
    energy_db: list[float],
    window_seconds: float,
    median_window_seconds: float,
    threshold_db: float,
) -> list[dict[str, float]]:
    """Bursts standing out above their local baseline.

    The baseline at window ``i`` is the median energy over a window of
    ``median_window_seconds`` centered on ``i``. A run of consecutive windows
    whose energy exceeds that baseline by ``threshold_db`` is one peak,
    reported at its loudest window (so a single burst spanning several
    windows isn't reported multiple times).
    """
    n = len(energy_db)
    if n == 0:
        return []

    radius = max(1, round(median_window_seconds / window_seconds / 2))
    relative_db = []
    for i in range(n):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        baseline = float(np.median(energy_db[lo:hi]))
        relative_db.append(energy_db[i] - baseline)

    peaks: list[dict[str, float]] = []
    i = 0
    while i < n:
        if relative_db[i] >= threshold_db:
            j = i
            while j < n and relative_db[j] >= threshold_db:
                j += 1
            best = max(range(i, j), key=lambda k: relative_db[k])
            peaks.append(
                {
                    "timecode": best * window_seconds,
                    "relative_db": relative_db[best],
                }
            )
            i = j
        else:
            i += 1
    return peaks


def compute_energy_db_stream(
    blocks: Iterable[np.ndarray], sample_rate: int, window_seconds: float
) -> list[float]:
    """Même résultat que ``compute_energy_db`` sur la concaténation des blocs,
    mais sans jamais tenir plus d'une fenêtre en mémoire (media-I4)."""
    window_size = max(1, int(round(window_seconds * sample_rate)))
    energy_db: list[float] = []
    pending: list[np.ndarray] = []
    pending_len = 0
    for block in blocks:
        pos = 0
        while pos < len(block):
            take = min(window_size - pending_len, len(block) - pos)
            pending.append(block[pos : pos + take])
            pending_len += take
            pos += take
            if pending_len == window_size:
                chunk = pending[0] if len(pending) == 1 else np.concatenate(pending)
                energy_db.extend(compute_energy_db(chunk, sample_rate, window_seconds))
                pending, pending_len = [], 0
    if pending_len:
        energy_db.extend(compute_energy_db(np.concatenate(pending), sample_rate, window_seconds))
    return energy_db


def _result(energy_db: list[float], window_seconds: float, median_window_seconds: float,
            threshold_db: float) -> dict[str, object]:
    peaks = find_peaks(energy_db, window_seconds, median_window_seconds, threshold_db)
    return {
        "window_seconds": window_seconds,
        "energy_db": energy_db,
        "peaks": peaks,
    }


def analyze(
    samples: np.ndarray,
    sample_rate: int,
    *,
    window_seconds: float = CONFIG_DEFAULTS["window_seconds"],
    median_window_seconds: float = CONFIG_DEFAULTS["median_window_seconds"],
    threshold_db: float = CONFIG_DEFAULTS["peak_threshold_db"],
) -> dict[str, object]:
    energy_db = compute_energy_db(samples, sample_rate, window_seconds)
    return _result(energy_db, window_seconds, median_window_seconds, threshold_db)


def analyze_stream(
    blocks: Iterable[np.ndarray],
    sample_rate: int,
    *,
    window_seconds: float = CONFIG_DEFAULTS["window_seconds"],
    median_window_seconds: float = CONFIG_DEFAULTS["median_window_seconds"],
    threshold_db: float = CONFIG_DEFAULTS["peak_threshold_db"],
) -> dict[str, object]:
    energy_db = compute_energy_db_stream(blocks, sample_rate, window_seconds)
    return _result(energy_db, window_seconds, median_window_seconds, threshold_db)


def _ffmpeg_cmd(video_path: Path, sample_rate: int, ffmpeg_bin: str) -> list[str]:
    return [
        ffmpeg_bin,
        "-i", str(video_path),
        "-vn",
        # ligne de temps du conteneur : silence dans un trou de pts (TASK-4880)
        "-af", "aresample=async=1:first_pts=0",
        "-f", "s16le",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-loglevel", "error",
        "pipe:1",
    ]


def _iter_samples_ffmpeg(
    video_path: Path,
    sample_rate: int,
    ffmpeg_bin: str = "ffmpeg",
    block_samples: int | None = None,
) -> Iterator[np.ndarray]:
    """Décode la piste audio en blocs float32 mono dans [-1, 1], au plus
    ``block_samples`` échantillons à la fois (une fenêtre par défaut)."""
    if block_samples is None:
        block_samples = max(1, int(round(CONFIG_DEFAULTS["window_seconds"] * sample_rate)))
    cmd = _ffmpeg_cmd(video_path, sample_rate, ffmpeg_bin)
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        except FileNotFoundError as exc:
            raise AudioError(f"ffmpeg introuvable ({ffmpeg_bin})") from exc
        try:
            nbytes = block_samples * 2
            leftover = b""
            while True:
                data = proc.stdout.read(nbytes - len(leftover))
                if not data:
                    break
                data = leftover + data
                usable = len(data) - (len(data) % 2)
                leftover = data[usable:]
                if usable:
                    yield np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0
            proc.stdout.close()
            returncode = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if returncode != 0:
            err.seek(0)
            raise AudioError(
                f"ffmpeg a echoue sur {video_path}: {err.read().decode(errors='replace').strip()}"
            )


def _extract_samples_ffmpeg(
    video_path: Path, sample_rate: int, ffmpeg_bin: str = "ffmpeg"
) -> np.ndarray:
    """Decode the video's audio track to mono float32 samples in [-1, 1]
    (tableau complet : pour les tests et les petits fichiers ; l'étape utilise
    ``_iter_samples_ffmpeg``)."""
    blocks = list(_iter_samples_ffmpeg(video_path, sample_rate, ffmpeg_bin, block_samples=sample_rate * 60))
    return np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)


def _read_existing(path: Path) -> dict[str, object]:
    """Relit audio.json ; un fichier tronque (arret pendant une ecriture
    ancienne) donne une erreur explicite, jamais une reparation silencieuse."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AudioError(
            f"{path} est illisible ({exc}) : refais l'etape audio avec --force"
        ) from exc


def run(
    video_id: str,
    workspace_dir: str | Path = "workspace",
    *,
    force: bool = False,
    sample_rate: int = CONFIG_DEFAULTS["sample_rate"],
    window_seconds: float = CONFIG_DEFAULTS["window_seconds"],
    median_window_seconds: float = CONFIG_DEFAULTS["median_window_seconds"],
    threshold_db: float = CONFIG_DEFAULTS["peak_threshold_db"],
    ffmpeg_bin: str = "ffmpeg",
    extractor: Callable[[Path, int], np.ndarray | Iterable[np.ndarray]] | None = None,
) -> dict[str, object]:
    """Étape audio (ADR-b16b) : lit workspace/<video_id>/<video_id>.mp4, écrit
    workspace/<video_id>/audio.json ; ne se relance pas si ce fichier existe
    déjà, sauf force=True."""
    video_dir = Path(workspace_dir) / video_id
    out_file = video_dir / "audio.json"
    if out_file.exists() and not force:
        return _read_existing(out_file)

    if extractor is None:
        extractor = functools.partial(_iter_samples_ffmpeg, ffmpeg_bin=ffmpeg_bin)

    video_path = video_dir / f"{video_id}.mp4"
    samples = extractor(video_path, sample_rate)
    if isinstance(samples, np.ndarray):
        samples = [samples]
    result = analyze_stream(
        samples,
        sample_rate,
        window_seconds=window_seconds,
        median_window_seconds=median_window_seconds,
        threshold_db=threshold_db,
    )

    video_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_file, result)
    return result
