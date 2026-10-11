"""TASK-9623bdba2126 (SPEC-54ed21c61caf R1, ADR-e1dac9ba2284) : construction
du zip d'amorcage par tools/build_portable.py. Tout tourne sans reseau : le
telechargement de uv.exe est injecte (fetch_uv), la wheel est fausse (zip
vide au bon nom) sauf quand le contenu precis importe peu, et les fichiers
installer/ reels du depot sont utilises tels quels (lecture locale, jamais le
reseau)."""

from __future__ import annotations

import hashlib
import io
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import build_portable  # noqa: E402

PYPROJECT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

EXPECTED_ENTRIES = {
    "Installer.bat",
    "Desinstaller.bat",
    "installer/install.ps1",
    "installer/desinstaller.ps1",
    "installer/Clipper.bat.template",
    "installer/PREMIER-CLIP.txt",
    "installer/overrides.txt",
    "installer/clipper.ico",
    "uv.exe",
    "version.txt",
}


def _fake_wheel(tmp_path: Path, version: str, name: str = "clipper") -> Path:
    """Une fausse wheel (zip vide, nom correct) : le contenu ne compte pas
    pour ce module, seul le nom (qui porte la version) est inspecte."""
    wheel_path = tmp_path / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w"):
        pass
    return wheel_path


def _fake_fetch_uv(content: bytes = b"\x00"):
    def fetch_uv(dest: Path) -> None:
        dest.write_bytes(content)

    return fetch_uv


# --------------------------------------------------------------------------
# (1) + (2) build() assemble puis zippe ; contenu exact de la liste R1.
# --------------------------------------------------------------------------


def test_build_returns_path_to_the_expected_zip_name(tmp_path):
    wheel = _fake_wheel(tmp_path, PYPROJECT_VERSION)
    out_dir = tmp_path / "dist"

    zip_path = build_portable.build(PYPROJECT_VERSION, wheel, out_dir, _fake_fetch_uv())

    assert zip_path == out_dir / f"Clipper-portable-{PYPROJECT_VERSION}.zip"
    assert zip_path.is_file()


def test_zip_root_contains_exactly_the_r1_file_list(tmp_path):
    wheel = _fake_wheel(tmp_path, PYPROJECT_VERSION)
    out_dir = tmp_path / "dist"

    zip_path = build_portable.build(PYPROJECT_VERSION, wheel, out_dir, _fake_fetch_uv())

    root = f"Clipper-portable-{PYPROJECT_VERSION}"
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())

    expected = {f"{root}/{entry}" for entry in EXPECTED_ENTRIES} | {f"{root}/{wheel.name}"}
    assert names == expected


# --------------------------------------------------------------------------
# (3) version.txt = [project] version de pyproject, wheel doit porter la
# meme version, sinon erreur explicite (ADR-ad2e : jamais de repli silencieux).
# --------------------------------------------------------------------------


def test_version_mismatch_with_pyproject_raises_explicit_error(tmp_path):
    wrong_version = PYPROJECT_VERSION + "-nope"
    wheel = _fake_wheel(tmp_path, wrong_version)
    out_dir = tmp_path / "dist"

    with pytest.raises(build_portable.BuildPortableError, match="pyproject"):
        build_portable.build(wrong_version, wheel, out_dir, _fake_fetch_uv())


def test_wheel_version_mismatch_with_version_param_raises_explicit_error(tmp_path):
    wheel = _fake_wheel(tmp_path, "9.9.9")
    out_dir = tmp_path / "dist"

    with pytest.raises(build_portable.BuildPortableError, match="wheel"):
        build_portable.build(PYPROJECT_VERSION, wheel, out_dir, _fake_fetch_uv())


def test_version_txt_content_matches_version(tmp_path):
    wheel = _fake_wheel(tmp_path, PYPROJECT_VERSION)
    out_dir = tmp_path / "dist"

    zip_path = build_portable.build(PYPROJECT_VERSION, wheel, out_dir, _fake_fetch_uv())

    root = f"Clipper-portable-{PYPROJECT_VERSION}"
    with zipfile.ZipFile(zip_path) as archive:
        content = archive.read(f"{root}/version.txt").decode("utf-8")
    assert content == PYPROJECT_VERSION


# --------------------------------------------------------------------------
# fetch_uv par defaut : telechargement injectable, sha256 verifie, echec
# explicite si le hash differe. Le telechargeur HTTP reel n'est jamais
# appele dans ces tests : on injecte un faux "download".
# --------------------------------------------------------------------------


def test_default_fetch_uv_extracts_uv_exe_when_sha256_matches(tmp_path):
    payload = b"contenu factice de uv.exe"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("uv.exe", payload)
    archive_bytes = buffer.getvalue()

    real_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    dest = tmp_path / "uv.exe"

    def fake_download(url: str) -> bytes:
        assert url == build_portable.UV_RELEASE_URL
        return archive_bytes

    # Le hash epingle ne correspondra pas forcement au contenu factice choisi
    # ici : on verifie seulement le chemin de succes en pointant le hash
    # attendu sur celui de notre propre archive factice, sans toucher au
    # reseau ni a la constante reelle du module.
    original_sha256 = build_portable.UV_SHA256
    build_portable.UV_SHA256 = real_sha256
    try:
        build_portable.default_fetch_uv(dest, download=fake_download)
    finally:
        build_portable.UV_SHA256 = original_sha256

    assert dest.read_bytes() == payload


def test_default_fetch_uv_raises_explicit_error_on_sha256_mismatch(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("uv.exe", b"peu importe")
    archive_bytes = buffer.getvalue()

    dest = tmp_path / "uv.exe"

    def fake_download(url: str) -> bytes:
        return archive_bytes

    with pytest.raises(build_portable.BuildPortableError, match="sha256"):
        build_portable.default_fetch_uv(dest, download=fake_download)

    assert not dest.exists()


# --------------------------------------------------------------------------
# (4) analyse d'arguments de la ligne de commande seulement (jamais
# l'execution reelle de 'uv build', qui a besoin du reseau/du disque reel).
# --------------------------------------------------------------------------


def test_parse_args_defaults_out_dir_to_dist():
    args = build_portable.parse_args([])
    assert args.out_dir == Path("dist")


def test_parse_args_accepts_custom_out_dir():
    args = build_portable.parse_args(["--out-dir", "autre-dossier"])
    assert args.out_dir == Path("autre-dossier")


# --------------------------------------------------------------------------
# (5) zip construit avec les vrais fichiers installer/ et un faux uv.exe de
# 1 octet fait moins de 5 Mo (rien de lourd n'y est inclus).
# --------------------------------------------------------------------------


def test_zip_with_real_installer_files_and_tiny_uv_is_under_5mb(tmp_path):
    wheel = _fake_wheel(tmp_path, PYPROJECT_VERSION)
    out_dir = tmp_path / "dist"

    zip_path = build_portable.build(PYPROJECT_VERSION, wheel, out_dir, _fake_fetch_uv(b"u"))

    assert zip_path.stat().st_size < 5 * 1024 * 1024
