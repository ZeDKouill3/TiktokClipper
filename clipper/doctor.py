"""« clipper doctor » (SPEC-38f7 R7) : rapport de diagnostic avant un premier
clip, et pour l'utilisateur quand quelque chose ne marche pas.

Module utilitaire (ADR-b16b) : n'importe aucune etape du pipeline ni
clipper.web. ``clipper.browser`` et ``clipper.gpu`` ne sont pas des etapes
(modules isoles, meme regle qu'eux) : reutilises ici pour ne jamais dupliquer
la recherche de Chrome ni la detection du GPU.

``report()`` fait sa propre lecture de ``config.toml`` dans le dossier
courant (jamais le Config deja charge par ``clipper/__main__.py`` pour les
autres commandes) : un ``config.toml`` absent ou invalide est un point du
rapport comme un autre, pas un plantage avant d'avoir pu diagnostiquer le
reste. Toutes les sondes (``which``, lanceur de sous-processus, chemins) sont
injectables, pour des tests sans reseau ni binaire reel.

Un point requis pour un premier clip (ffmpeg, ffprobe, claude connecte,
config, modeles) en statut ``manquant`` fait echouer ``exit_code`` ; Chrome
et le GPU ne sont que des avertissements (R7)."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from clipper import __version__ as CLIPPER_VERSION
from clipper import browser, models
from clipper.config import Config, load_config
from clipper.gpu import Device, get_device

STATUS_OK = "ok"
STATUS_WARNING = "avertissement"
STATUS_MISSING = "manquant"

# Points dont le statut "manquant" fait echouer clipper doctor (R7) : les
# autres (chrome, gpu, data_dirs, python) ne sont que des avertissements.
REQUIRED_POINTS = {"ffmpeg", "ffprobe", "claude", "config", "mediapipe_model", "whisper_model"}


def _point(name: str, status: str, detail: str, fix: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


# --------------------------------------------------------------------------
# Sondes par defaut (reelles)
# --------------------------------------------------------------------------


def _default_nvidia_present() -> bool:
    return importlib.util.find_spec("nvidia") is not None


def _default_whisper_cache_probe(name: str) -> bool:
    """Sans reseau : vrai si le modele faster-whisper ``name`` est deja dans
    le cache Hugging Face local (``download_model(local_files_only=True)``,
    qui echoue sans le toucher si absent)."""
    from faster_whisper.utils import download_model

    try:
        download_model(name, local_files_only=True)
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------
# Points
# --------------------------------------------------------------------------


def _check_python() -> dict[str, Any]:
    return _point("python", STATUS_OK, f"Python {platform.python_version()} ; clipper {CLIPPER_VERSION}")


def _check_binary(which: Callable[[str], str | None], name: str) -> dict[str, Any]:
    found = which(name)
    if found:
        return _point(name, STATUS_OK, found)
    return _point(
        name, STATUS_MISSING, "introuvable dans le PATH",
        f"installe {name} et ajoute-le au PATH",
    )


def _check_claude(which: Callable[[str], str | None], run: Callable[..., Any]) -> dict[str, Any]:
    found = which("claude")
    if not found:
        return _point(
            "claude", STATUS_MISSING, "introuvable dans le PATH",
            "installe Claude Code (https://claude.ai/install.ps1) puis « claude auth login »",
        )
    # Sous Windows, Claude Code s'installe comme un raccourci npm .cmd : un
    # Popen direct sur "claude" ne le trouve pas (meme contournement que
    # clipper.llm.claude_cli.resolve_command, jamais recopie).
    from clipper.llm.claude_cli import resolve_command

    command = resolve_command("claude")
    try:
        proc = run([command, "auth", "status"], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return _point(
            "claude", STATUS_MISSING, f"{found} : « claude auth status » injoignable ({type(exc).__name__})",
            "claude auth login",
        )
    if proc.returncode == 0:
        return _point("claude", STATUS_OK, f"{found} : connecte")
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    suffix = f" ({text[0]})" if text else ""
    return _point("claude", STATUS_MISSING, f"{found} : non connecte{suffix}", "claude auth login")


def _check_chrome(chrome_finder: Callable[[Config | None], Path], config: Config | None) -> dict[str, Any]:
    try:
        path = chrome_finder(config)
    except browser.BrowserError as exc:
        return _point("chrome", STATUS_WARNING, "introuvable", str(exc))
    return _point("chrome", STATUS_OK, str(path))


def _check_gpu(device_factory: Callable[[], Device], nvidia_present: Callable[[], bool]) -> dict[str, Any]:
    device = device_factory()
    nvidia = nvidia_present()
    if device.type == "cuda":
        return _point("gpu", STATUS_OK, "GPU detecte (cuda)")
    detail = "CPU : transcription plus lente"
    if not nvidia:
        return _point(
            "gpu", STATUS_WARNING, detail + " (paquets nvidia-cublas-cu12/nvidia-cudnn-cu12 absents)",
            'installe l\'extra [cuda] : uv pip install -e ".[cuda]"',
        )
    return _point("gpu", STATUS_WARNING, detail)


def _check_nvenc(ffmpeg: str, run: Callable[..., Any]) -> dict[str, Any]:
    """Un encodage reel d'une image en h264_nvenc : « GPU detecte » vient de
    ctranslate2, pas de ffmpeg (build sans nvenc, pilote trop ancien pour son
    SDK NVENC : chaque rendu echouerait). Avertissement seulement."""
    fix = "mets a jour le pilote NVIDIA ou ffmpeg (build avec h264_nvenc) ; sinon le rendu echoue"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
        "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
    ]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 - sonde injoignable, jamais un plantage de doctor
        return _point("nvenc", STATUS_WARNING, f"encodage d'essai injoignable ({type(exc).__name__})", fix)
    if proc.returncode == 0:
        return _point("nvenc", STATUS_OK, "h264_nvenc encode une image")
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    reason = lines[-1] if lines else f"code {proc.returncode}"
    return _point("nvenc", STATUS_WARNING, f"h264_nvenc echoue : {reason}", fix)


def _check_mediapipe_model(config: Config | None, path_fn: Callable[[Config | None], Path]) -> dict[str, Any]:
    path = path_fn(config)
    if path.exists():
        return _point("mediapipe_model", STATUS_OK, str(path))
    return _point("mediapipe_model", STATUS_MISSING, f"{path} absent", "lance « clipper models prefetch »")


def _check_whisper_model(
    config: Config | None, probe: Callable[[str], bool], name_fn: Callable[[Config | None], str]
) -> dict[str, Any]:
    name = name_fn(config)
    if probe(name):
        return _point("whisper_model", STATUS_OK, f"modele {name!r} present")
    return _point("whisper_model", STATUS_MISSING, f"modele {name!r} absent", "lance « clipper models prefetch »")


def _check_config(cwd: Path) -> tuple[Config | None, dict[str, Any]]:
    path = cwd / "config.toml"
    if not path.is_file():
        return None, _point("config", STATUS_MISSING, f"{path} absent", "lance « clipper init »")
    try:
        config = load_config(path)
    except Exception as exc:  # noqa: BLE001 - config.toml illisible ou invalide, jamais un plantage de doctor
        return None, _point("config", STATUS_MISSING, f"{path} : {exc}", "corrige config.toml")
    return config, _point("config", STATUS_OK, str(path))


def _check_data_dirs(cwd: Path, config: Config | None, writable: Callable[[Path], bool]) -> dict[str, Any]:
    dirs = [config.workspace_dir, config.output_dir] if config is not None else [Path("workspace"), Path("output")]
    absolute = [d if d.is_absolute() else cwd / d for d in dirs]
    problems = [str(d) for d in absolute if not writable(d)]
    if problems:
        return _point(
            "data_dirs", STATUS_WARNING, f"non ecrivable(s) : {', '.join(problems)}",
            "verifie les droits d'ecriture de ces dossiers",
        )
    return _point("data_dirs", STATUS_OK, ", ".join(str(d) for d in absolute))


def _writable(path: Path) -> bool:
    probe = path if path.exists() else next((p for p in (path, *path.parents) if p.exists()), path)
    return os.access(probe, os.W_OK)


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------


def report(
    cwd: str | Path | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
    chrome_finder: Callable[[Config | None], Path] = browser.find_chrome,
    device_factory: Callable[[], Device] = get_device,
    nvidia_present: Callable[[], bool] = _default_nvidia_present,
    whisper_cache_probe: Callable[[str], bool] = _default_whisper_cache_probe,
    mediapipe_path_fn: Callable[[Config | None], Path] = models.mediapipe_model_path,
    whisper_name_fn: Callable[[Config | None], str] = models.whisper_model_name,
    writable: Callable[[Path], bool] = _writable,
) -> list[dict[str, Any]]:
    """Rapport de diagnostic : une entree par point (``name``, ``status`` :
    ``ok``/``avertissement``/``manquant``, ``detail``, ``fix`` ou ``None``)."""
    root = Path(cwd) if cwd is not None else Path.cwd()
    config, config_point = _check_config(root)
    ffmpeg_point = _check_binary(which, "ffmpeg")
    device = device_factory()
    gpu_point = _check_gpu(lambda: device, nvidia_present)
    # le rendu choisit h264_nvenc des que le device est cuda (render._encoder)
    nvenc_points = (
        [_check_nvenc(ffmpeg_point["detail"], run)]
        if device.type == "cuda" and ffmpeg_point["status"] == STATUS_OK else []
    )
    return [
        _check_python(),
        ffmpeg_point,
        _check_binary(which, "ffprobe"),
        _check_claude(which, run),
        _check_chrome(chrome_finder, config),
        gpu_point,
        *nvenc_points,
        _check_mediapipe_model(config, mediapipe_path_fn),
        _check_whisper_model(config, whisper_cache_probe, whisper_name_fn),
        config_point,
        _check_data_dirs(root, config, writable),
    ]


def exit_code(points: list[dict[str, Any]]) -> int:
    """0 si tout ce qui est requis pour un premier clip est present (R7),
    1 sinon ; Chrome et le GPU (jamais dans REQUIRED_POINTS) ne comptent
    pas."""
    missing = [p for p in points if p["name"] in REQUIRED_POINTS and p["status"] == STATUS_MISSING]
    return 1 if missing else 0


def format_text(points: list[dict[str, Any]]) -> str:
    """Une ligne par point : ``nom : statut -- detail`` puis ``  remede : ...``
    quand un remede est donne."""
    lines = []
    for p in points:
        line = f"{p['name']} : {p['status']} -- {p['detail']}"
        if p["fix"]:
            line += f"\n  remede : {p['fix']}"
        lines.append(line)
    return "\n".join(lines)
