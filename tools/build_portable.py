"""Construit le zip d'amorcage Clipper-portable-<version>.zip (SPEC-54ed21c61caf
R1, ADR-e1dac9ba2284).

``build()`` assemble un dossier ``Clipper-portable-<version>/`` (wheel, uv.exe,
scripts installer/, icone, version.txt) puis le zippe. Jamais de Python, de
site-packages, de ffmpeg ni de modele dedans (R1) : seuls les fichiers listes
ici y entrent.

``fetch_uv`` est injectable pour les tests (aucun reseau par defaut, ADR-ad2e
: jamais de repli silencieux). La fabrique par defaut (``default_fetch_uv``)
telecharge uv.exe depuis la GitHub Release astral-sh/uv a une version
epinglee et verifie son sha256 ; un hash different est une erreur explicite,
jamais une installation partielle.

Pour renouveler la version de uv epinglee : choisir la nouvelle version sur
https://github.com/astral-sh/uv/releases, prendre le sha256 de
``uv-x86_64-pc-windows-msvc.zip`` dans l'asset ``.sha256`` correspondant (ou
``sha256.sum``), et mettre a jour UV_VERSION et UV_SHA256 ci-dessous.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INSTALLER_SRC = REPO_ROOT / "installer"
ICON_SRC = REPO_ROOT / "tools" / "clipper.ico"

# Fichiers d'installer/ places a la racine du zip (R1) : Installer.bat et
# Desinstaller.bat y vivent deja a plat dans le depot.
ROOT_INSTALLER_FILES = ["Installer.bat", "Desinstaller.bat"]

# Fichiers d'installer/ places sous installer/ dans le zip (R1). overrides.txt
# (I1, TASK-4f1d7d1ee341) : passe explicitement a 'uv pip install --override'
# a l'etape 3, sinon [tool.uv] override-dependencies de pyproject.toml n'est
# honore que si un pyproject.toml est trouve en remontant depuis le dossier
# courant -- jamais le cas chez l'utilisateur (Telechargements).
SUBDIR_INSTALLER_FILES = [
    "install.ps1",
    "desinstaller.ps1",
    "Clipper.bat.template",
    "PREMIER-CLIP.txt",
    "overrides.txt",
]

# uv 0.12.19, releve le 2026-10-03 (AGENTS.md) : asset Windows x86_64 et son
# sha256 publie par astral-sh/uv (fichier .sha256 de la release).
UV_VERSION = "0.12.19"
UV_ASSET_NAME = "uv-x86_64-pc-windows-msvc.zip"
UV_RELEASE_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{UV_ASSET_NAME}"
UV_SHA256 = "6dbb02d79e419522f1c500f0adb1cddcff0cda7d59b0d66ea7f5e3b4a1b2f5f0"


class BuildPortableError(Exception):
    """Construction du zip d'amorcage impossible (raison dans le message,
    ADR-ad2e : jamais de repli silencieux)."""


def _pyproject_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _wheel_version(wheel_path: Path) -> str:
    parts = wheel_path.name.split("-")
    if len(parts) < 2:
        raise BuildPortableError(f"nom de wheel invalide (version introuvable) : {wheel_path.name}")
    return parts[1]


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:  # reseau reel, jamais appele par defaut en test
        return response.read()


def default_fetch_uv(dest: Path, *, download: Callable[[str], bytes] = _http_get) -> None:
    """Telecharge uv.exe depuis la GitHub Release epinglee et verifie son
    sha256 avant de l'extraire vers ``dest``. ``download`` est injectable
    (jamais le vrai reseau dans un test qui tourne par defaut)."""
    archive_bytes = download(UV_RELEASE_URL)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    if digest != UV_SHA256:
        raise BuildPortableError(
            f"sha256 de {UV_ASSET_NAME} ({UV_VERSION}) ne correspond pas au hash epingle : "
            f"attendu {UV_SHA256}, obtenu {digest}"
        )
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        with archive.open("uv.exe") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def build(version: str, wheel_path: Path, out_dir: Path, fetch_uv: Callable[[Path], None]) -> Path:
    """Assemble ``Clipper-portable-<version>/`` puis le zippe en
    ``out_dir/Clipper-portable-<version>.zip`` (R1). Leve ``BuildPortableError``
    si ``version`` ne correspond pas a ``[project] version`` de pyproject.toml
    ou a la version portee par le nom de ``wheel_path``."""
    pyproject_version = _pyproject_version()
    if version != pyproject_version:
        raise BuildPortableError(
            f"version {version!r} ne correspond pas a [project] version de pyproject.toml "
            f"({pyproject_version!r})"
        )

    wheel_path = Path(wheel_path)
    wheel_version = _wheel_version(wheel_path)
    if wheel_version != version:
        raise BuildPortableError(
            f"la wheel {wheel_path.name} porte la version {wheel_version!r}, attendu {version!r}"
        )
    if not wheel_path.is_file():
        raise BuildPortableError(f"wheel introuvable : {wheel_path}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    root_name = f"Clipper-portable-{version}"
    zip_path = out_dir / f"{root_name}.zip"

    with tempfile.TemporaryDirectory(prefix="clipper-portable-") as tmp:
        staging = Path(tmp) / root_name
        installer_staging = staging / "installer"
        installer_staging.mkdir(parents=True)

        for name in ROOT_INSTALLER_FILES:
            shutil.copy2(INSTALLER_SRC / name, staging / name)
        for name in SUBDIR_INSTALLER_FILES:
            shutil.copy2(INSTALLER_SRC / name, installer_staging / name)
        shutil.copy2(ICON_SRC, installer_staging / "clipper.ico")

        fetch_uv(staging / "uv.exe")

        shutil.copy2(wheel_path, staging / wheel_path.name)
        (staging / "version.txt").write_text(version, encoding="utf-8")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in sorted(staging.rglob("*")):
                if file_path.is_file():
                    relative = file_path.relative_to(staging).as_posix()
                    archive.write(file_path, arcname=f"{root_name}/{relative}")

    return zip_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Construit Clipper-portable-<version>.zip (wheel + uv.exe + installer/)."
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("dist"),
        help="Dossier de sortie de la wheel et du zip (defaut : dist/).",
    )
    return parser.parse_args(argv)


def _find_wheel(out_dir: Path, version: str) -> Path:
    candidates = sorted(out_dir.glob(f"clipper-{version}-*.whl"))
    if not candidates:
        raise BuildPortableError(f"aucune wheel clipper-{version}-*.whl dans {out_dir} apres 'uv build --wheel'")
    return candidates[0]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    version = _pyproject_version()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(args.out_dir), str(REPO_ROOT)],
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        raise BuildPortableError(f"'uv build --wheel' a echoue (code {result.returncode})")

    wheel_path = _find_wheel(args.out_dir, version)
    zip_path = build(version, wheel_path, args.out_dir, default_fetch_uv)
    print(zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
