"""TASK-5d43 : dependances web (FastAPI, uvicorn, httpx) et donnees du
paquet (polices sous clipper/assets/**) declarees dans pyproject.toml."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory) -> Path:
    """Un seul wheel par processus de test (TASK-8f7c) : construit hors ligne
    dans une copie isolee du paquet (setuptools ecrit build/ a cote du
    pyproject.toml), puis lu en lecture seule par les tests qui inspectent
    son contenu ou l'installent."""
    base = tmp_path_factory.mktemp("wheel")
    src = base / "src"
    shutil.copytree(ROOT / "clipper", src / "clipper")
    shutil.copy2(PYPROJECT, src / "pyproject.toml")

    out_dir = base / "dist"
    result = subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(out_dir), str(src)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(out_dir.glob("*.whl"))
    assert wheels, f"aucun wheel produit dans {out_dir}"
    return wheels[0]


def test_fastapi_and_uvicorn_are_project_dependencies():
    deps = _load_pyproject()["project"]["dependencies"]
    assert "fastapi" in deps
    assert "uvicorn" in deps


def test_httpx_is_in_the_test_extra():
    test_extra = _load_pyproject()["project"]["optional-dependencies"]["test"]
    assert "httpx" in test_extra


def test_opencv_override_is_preserved():
    """Le contournement single-OpenCV (mediapipe/scenedetect) ne doit pas
    disparaitre quand on ajoute les dependances web."""
    override = _load_pyproject()["tool"]["uv"]["override-dependencies"]
    assert override == ["opencv-python; sys_platform == 'never'"]


def test_import_fastapi_uvicorn_httpx():
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
    import uvicorn  # noqa: F401


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv absent du PATH")
def test_wheel_contains_the_font_assets(built_wheel):
    """Une installation non editable (uv pip install .) doit apporter
    clipper/assets/fonts/Poppins-ExtraBold.ttf : on construit le wheel hors
    ligne (cache uv local, aucun reseau) : le wheel de la session
    (fixture built_wheel) est inspecte, sans le reconstruire."""

    with zipfile.ZipFile(built_wheel) as zf:
        names = zf.namelist()
    assert "clipper/assets/fonts/Poppins-ExtraBold.ttf" in names
    assert "clipper/assets/fonts/OFL.txt" in names


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv absent du PATH")
def test_wheel_contains_the_web_static_assets(built_wheel):
    """TASK-4ed9 : une installation non editable (uv pip install .) doit
    apporter clipper/web/static/index.html, app.js et console.css, en plus des
    polices deja couvertes. Lit le wheel de la session (fixture built_wheel)."""

    with zipfile.ZipFile(built_wheel) as zf:
        names = zf.namelist()
    assert "clipper/web/static/index.html" in names
    assert "clipper/web/static/app.js" in names
    assert "clipper/web/static/console.css" in names


def test_console_script_clipper_is_declared():
    """TASK-c8a11756eb4e : la wheel doit fournir la commande 'clipper' en
    plus de 'python -m clipper'."""
    scripts = _load_pyproject()["project"]["scripts"]
    assert scripts["clipper"] == "clipper.__main__:main"


def test_embedded_rubric_matches_repo_rubric():
    """La grille embarquee (clipper/assets/rubric.toml) est une copie exacte
    de rubric.toml a la racine du depot : les deux doivent rester
    synchronises (utilisee par [moments] rubric_path = "builtin" et par
    'clipper init')."""
    embedded = ROOT / "clipper" / "assets" / "rubric.toml"
    repo = ROOT / "rubric.toml"
    assert embedded.read_text(encoding="utf-8") == repo.read_text(encoding="utf-8")


def test_embedded_config_example_matches_repo():
    embedded = ROOT / "clipper" / "assets" / "config.example.toml"
    repo = ROOT / "config.example.toml"
    assert embedded.read_text(encoding="utf-8") == repo.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv absent du PATH")
def test_wheel_contains_rubric_and_config_example_assets(built_wheel):
    """La grille par defaut et config.example.toml doivent etre embarquees
    dans la wheel (package data), pas seulement presentes dans le depot."""

    with zipfile.ZipFile(built_wheel) as zf:
        names = zf.namelist()
    assert "clipper/assets/rubric.toml" in names
    assert "clipper/assets/config.example.toml" in names


def test_cuda_extra_declares_cublas_and_cudnn():
    """TASK-f6c495f901c0 (SPEC-38f7 R4) : l'installeur doit pouvoir installer
    'clipper[cuda]' plutot que de deposer les paquets nvidia a la main."""
    extra = _load_pyproject()["project"]["optional-dependencies"]["cuda"]
    assert any(dep.startswith("nvidia-cublas-cu12") for dep in extra)
    assert any(dep.startswith("nvidia-cudnn-cu12") for dep in extra)


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_console_script(venv_dir: Path, name: str) -> Path:
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv absent du PATH")
def test_wheel_installed_alone_in_a_fresh_venv_runs_clipper_help_and_init(built_wheel, tmp_path):
    """TASK-c8a11756eb4e (critere principal) : installee SEULE dans un venv
    neuf hors depot (uv venv + uv pip install de la wheel), la wheel fournit
    la commande 'clipper' et 'clipper init' ecrit config.toml/rubric.toml
    dans le dossier courant, identiques a la grille et a l'exemple de config
    du depot."""
    venv_dir = tmp_path / "venv"
    venv_created = subprocess.run(
        ["uv", "venv", "--offline", str(venv_dir)],
        capture_output=True,
        text=True,
    )
    assert venv_created.returncode == 0, venv_created.stderr

    installed = subprocess.run(
        [
            "uv", "pip", "install", "--offline",
            "--python", str(_venv_python(venv_dir)),
            str(built_wheel),
        ],
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr

    clipper_bin = _venv_console_script(venv_dir, "clipper")
    assert clipper_bin.exists(), f"commande 'clipper' absente du venv : {clipper_bin}"

    help_result = subprocess.run([str(clipper_bin), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0, help_result.stderr
    assert "clipper" in help_result.stdout.lower()

    workdir = tmp_path / "projet"
    workdir.mkdir()
    init_result = subprocess.run([str(clipper_bin), "init"], capture_output=True, text=True, cwd=workdir)
    assert init_result.returncode == 0, init_result.stderr

    assert (workdir / "config.toml").read_text(encoding="utf-8") == (
        ROOT / "config.example.toml"
    ).read_text(encoding="utf-8")
    assert (workdir / "rubric.toml").read_text(encoding="utf-8") == (ROOT / "rubric.toml").read_text(
        encoding="utf-8"
    )

    init_again = subprocess.run([str(clipper_bin), "init"], capture_output=True, text=True, cwd=workdir)
    assert init_again.returncode == 1
    assert "config.toml" in init_again.stderr
