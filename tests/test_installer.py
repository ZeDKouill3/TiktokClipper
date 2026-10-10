"""TASK-b82ee001ec52 : scripts de l'installeur portable (SPEC-38f7761891f6
R2, R3, R4, R6, R8 ; ADR-e1dac9ba2284). Tous les tests lancent
installer/install.ps1 et installer/desinstaller.ps1 avec ``--dry-run`` via
``powershell -NoProfile -ExecutionPolicy Bypass`` sur des dossiers
temporaires : aucun reseau, aucun telechargement, aucun vrai binaire ffmpeg
ni modele. Le decodeur GPU est exerce avec un ``nvidia-smi`` simule (script
place en tete du PATH), jamais le vrai.

``--dry-run`` traverse le meme code de decision que l'installation reelle
(une fonction par etape qui recoit ``-DryRun``) : ces tests prouvent donc le
plan affiche, pas une implementation separee."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SRC = REPO_ROOT / "installer"

POWERSHELL = shutil.which("powershell")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="powershell absent du PATH")

NEW_VERSION = "2.0.0"
OLDER_VERSION = "1.0.0"
NEWER_VERSION = "3.0.0"


# --------------------------------------------------------------------------
# Fixtures (locales a ce fichier, jamais dans tests/conftest.py)
# --------------------------------------------------------------------------


@pytest.fixture
def installer_dir(tmp_path: Path) -> Path:
    """Copie installer/** dans un dossier temporaire avec un version.txt a
    cote (meme disposition que le zip, SPEC-38f7 R1), sans jamais toucher au
    depot."""
    dest = tmp_path / "installer"
    dest.mkdir()
    for name in (
        "install.ps1",
        "desinstaller.ps1",
        "Clipper.bat.template",
        "PREMIER-CLIP.txt",
        "overrides.txt",
        "Installer.bat",
        "Desinstaller.bat",
    ):
        shutil.copy(INSTALLER_SRC / name, dest / name)
    (tmp_path / "version.txt").write_text(NEW_VERSION, encoding="utf-8")
    return tmp_path


@pytest.fixture
def zip_layout_dir(tmp_path: Path) -> Path:
    """Disposition exacte du zip (SPEC-38f7761891f6 R1) : Installer.bat et
    Desinstaller.bat a la racine, le reste sous installer/ ; contrairement a
    ``installer_dir`` ci-dessus (tout a plat dans un seul dossier), c'est la
    seule disposition qui exerce le chemin relatif que les .bat calculent
    reellement a l'execution (``%~dp0``)."""
    root = tmp_path / f"Clipper-portable-{NEW_VERSION}"
    sub = root / "installer"
    sub.mkdir(parents=True)
    for name in ("Installer.bat", "Desinstaller.bat"):
        shutil.copy(INSTALLER_SRC / name, root / name)
    for name in ("install.ps1", "desinstaller.ps1", "Clipper.bat.template", "PREMIER-CLIP.txt", "overrides.txt"):
        shutil.copy(INSTALLER_SRC / name, sub / name)
    # Icone factice (contenu sans importance ici) : meme disposition que le
    # vrai zip (tools/build_portable.py copie clipper.ico sous installer/).
    (sub / "clipper.ico").write_bytes(b"\x00\x01\x02\x03")
    (root / "version.txt").write_text(NEW_VERSION, encoding="utf-8")
    return root


@pytest.fixture
def fake_nvidia_smi(tmp_path: Path) -> Path:
    """Dossier contenant un nvidia-smi.bat simule (sortie non vide, code 0),
    a placer en tete du PATH : jamais le vrai nvidia-smi."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "nvidia-smi.bat"
    script.write_text("@echo off\r\necho GPU 0: Fake NVIDIA GPU, 8192 MiB\r\n", encoding="utf-8")
    return bin_dir


@pytest.fixture
def listening_port():
    """Ouvre un vrai socket local sur 127.0.0.1 (port ephemere choisi par
    l'OS, jamais 8000 en dur : des tests xdist paralleles ou un vrai
    ``clipper serve`` deja lance sur la machine ne doivent jamais se
    percuter) pour simuler une console Clipper en cours d'execution."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        yield port
    finally:
        sock.close()


def _free_port() -> int:
    """Un port ephemere probablement libre (l'OS l'attribue puis on
    referme tout de suite) : evite de dependre du port 8000 reel, qui peut
    deja etre occupe sur la machine par une vraie console Clipper."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _env_with_prepended_path(extra_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(extra_dir) + os.pathsep + env.get("PATH", "")
    return env


def _env_without_command(name: str) -> dict[str, str]:
    """Environnement sans aucun repertoire du PATH contenant ``name``
    (jamais le vrai nvidia-smi dans le test « sans GPU »)."""
    env = dict(os.environ)
    found = shutil.which(name)
    if found:
        found_dir = os.path.normcase(os.path.normpath(str(Path(found).parent)))
        parts = [
            part
            for part in env.get("PATH", "").split(os.pathsep)
            if os.path.normcase(os.path.normpath(part or ".")) != found_dir
        ]
        env["PATH"] = os.pathsep.join(parts)
    return env


def run_install(
    root: Path,
    args: list[str],
    env: dict[str, str] | None = None,
    port: int | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    install_ps1 = root / "installer" / "install.ps1"
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(install_ps1)]
    if port is not None:
        cmd += ["-Port", str(port)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(cwd) if cwd else None, timeout=120)


def run_step9_launcher(tmp_path: Path, root: Path, *, data_name: str = "data") -> tuple[subprocess.CompletedProcess, Path, Path]:
    """Dot-source install.ps1 (-NoAutoRun, point d'injection pour les tests)
    puis appelle Invoke-Step9-Launcher directement : les etapes 2, 3, 5 et 8
    ont besoin du reseau (python, uv, claude, modeles) et ne sont donc
    jamais traversees par un test qui tourne par defaut."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    data_dir = tmp_path / data_name
    template_path = root / "installer" / "Clipper.bat.template"
    script_path = tmp_path / "_invoke_step9.ps1"
    script_path.write_text(
        "\n".join(
            [
                f". '{root / 'installer' / 'install.ps1'}' -NoAutoRun",
                (
                    f"Invoke-Step9-Launcher -App '{app_dir}' -Data '{data_dir}' "
                    f"-TemplatePath '{template_path}' -Root '{root}' -SansRaccourci -DryRun:$false"
                ),
                "Write-Host 'STEP9_DONE'",
            ]
        ),
        # BOM obligatoire : sans lui, PowerShell 5.1 relit un .ps1 UTF-8 en
        # ANSI (page de code systeme), ce qui corrompt tout caractere
        # accentue passe ici (--data) avant meme qu'il atteigne install.ps1.
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, app_dir, data_dir


def run_desinstaller(
    root: Path,
    args: list[str],
    env: dict[str, str] | None = None,
    port: int | None = None,
    bureau: Path | None = None,
) -> subprocess.CompletedProcess:
    desinstaller_ps1 = root / "installer" / "desinstaller.ps1"
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(desinstaller_ps1)]
    if port is not None:
        cmd += ["-Port", str(port)]
    if bureau is not None:
        cmd += ["-Bureau", str(bureau)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)


def run_bat(bat_path: Path, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = ["cmd", "/c", str(bat_path), *args]
    return subprocess.run(cmd, cwd=str(bat_path.parent), capture_output=True, text=True, env=env, timeout=120)


def _dir_snapshot(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {str(p.relative_to(path)) for p in path.rglob("*")}


# --------------------------------------------------------------------------
# (1) les fichiers existent, et install.ps1 / desinstaller.ps1 sont du
# PowerShell 5.1 (jamais &&, ??, ni operateur ternaire).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Installer.bat",
        "install.ps1",
        "Desinstaller.bat",
        "desinstaller.ps1",
        "Clipper.bat.template",
        "PREMIER-CLIP.txt",
        "overrides.txt",
    ],
)
def test_installer_files_exist(name: str) -> None:
    assert (INSTALLER_SRC / name).is_file()


def _strip_powershell_comments(text: str) -> str:
    """Retire les blocs ``<# ... #>`` et les commentaires ``# ...`` avant de
    chercher une syntaxe PowerShell 7 : la prose des scripts decrit cette
    meme contrainte (« pas de &&, ni ?? ») et declencherait un faux positif
    si on la scannait telle quelle."""
    without_blocks = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    lines = [line.split("#", 1)[0] for line in without_blocks.splitlines()]
    return "\n".join(lines)


@pytest.mark.parametrize("name", ["install.ps1", "desinstaller.ps1"])
def test_scripts_avoid_powershell7_only_syntax(name: str) -> None:
    code = _strip_powershell_comments((INSTALLER_SRC / name).read_text(encoding="utf-8"))
    assert "&&" not in code
    assert "??" not in code


# --------------------------------------------------------------------------
# (2) --dry-run n'ecrit rien, ne telecharge rien ; les 11 etapes s'affichent
# dans l'ordre.
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    before = _dir_snapshot(installer_dir)

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert not app_dir.exists()
    assert not data_dir.exists()
    assert _dir_snapshot(installer_dir) == before


def test_dry_run_shows_eleven_steps_in_order(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    positions = [result.stdout.find(f"[{n}/11]") for n in range(1, 12)]
    assert all(p != -1 for p in positions), result.stdout
    assert positions == sorted(positions)


def test_dry_run_shows_resolved_paths(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert str(app_dir) in result.stdout
    assert str(data_dir) in result.stdout


# --------------------------------------------------------------------------
# (6) options incompatibles : message explicite, code non nul.
# --------------------------------------------------------------------------


def test_installer_bat_wrapper_finds_install_ps1_under_installer_subdir(zip_layout_dir: Path) -> None:
    app_dir = zip_layout_dir / "app"
    data_dir = zip_layout_dir / "data"

    result = run_bat(
        zip_layout_dir / "Installer.bat",
        ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "n'existe pas" not in result.stdout
    assert "[1/11]" in result.stdout


def test_desinstaller_bat_wrapper_finds_desinstaller_ps1_under_installer_subdir(zip_layout_dir: Path) -> None:
    app_dir = zip_layout_dir / "app"
    app_dir.mkdir()
    # I6 : une installation sans marqueur (install.json/version.txt) est
    # refusee ; ce test verifie seulement le branchement .bat -> .ps1, pas
    # ce refus (couvert par test_i6_...), donc un marqueur minimal est pose.
    (app_dir / "version.txt").write_text(NEW_VERSION, encoding="utf-8")

    result = run_bat(zip_layout_dir / "Desinstaller.bat", ["--app", str(app_dir), "--dry-run"])

    # Pas d'assertion sur returncode/stdout complet : Desinstaller.bat ne
    # transmet jamais -Port (toujours le vrai port 8000, par conception,
    # desinstaller.ps1), donc le resultat depend de ce qui tourne reellement
    # sur la machine. Seul le branchement .bat -> installer/desinstaller.ps1
    # est en jeu ici : si le chemin etait faux, PowerShell echouerait avant
    # meme d'atteindre une ligne ecrite par le script.
    combined = result.stdout + result.stderr
    assert "n'existe pas" not in combined
    assert "desinstaller.ps1" not in combined.lower() or "parametre -file" not in combined.lower()


def test_cpu_and_cuda_together_is_rejected(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(
        installer_dir,
        ["--app", str(app_dir), "--data", str(data_dir), "--cpu", "--cuda", "--dry-run"],
    )

    assert result.returncode != 0
    assert "incompatible" in result.stdout.lower()
    assert not app_dir.exists()


# --------------------------------------------------------------------------
# (3) decision CPU / CUDA.
# --------------------------------------------------------------------------


def test_gpu_decision_without_nvidia_smi_is_cpu(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    env = _env_without_command("nvidia-smi")

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CPU" in result.stdout
    assert "[cuda]" not in result.stdout


def test_gpu_decision_with_simulated_nvidia_smi_is_cuda(installer_dir: Path, fake_nvidia_smi: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    env = _env_with_prepended_path(fake_nvidia_smi)

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[cuda]" in result.stdout


def test_cpu_flag_overrides_simulated_nvidia_smi(installer_dir: Path, fake_nvidia_smi: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    env = _env_with_prepended_path(fake_nvidia_smi)

    result = run_install(
        installer_dir,
        ["--app", str(app_dir), "--data", str(data_dir), "--cpu", "--dry-run"],
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[cuda]" not in result.stdout
    assert "CPU" in result.stdout


# --------------------------------------------------------------------------
# (3) premiere installation / mise a jour / refus de retrogradation.
# --------------------------------------------------------------------------


def test_fresh_app_is_first_install(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "premiere installation" in result.stdout


def test_older_installed_version_is_an_update_with_venv_removal(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    (app_dir / "version.txt").write_text(OLDER_VERSION, encoding="utf-8")

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "mise a jour" in result.stdout
    assert ".venv" in result.stdout
    assert "supprime" in result.stdout


def test_equal_installed_version_is_an_update(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    (app_dir / "version.txt").write_text(NEW_VERSION, encoding="utf-8")

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "mise a jour" in result.stdout


def test_newer_installed_version_refuses_downgrade(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    (app_dir / "version.txt").write_text(NEWER_VERSION, encoding="utf-8")
    before = _dir_snapshot(app_dir)

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode != 0
    assert "plus ancienne" in result.stdout.lower() or "retrograd" in result.stdout.lower()
    assert _dir_snapshot(app_dir) == before


# --------------------------------------------------------------------------
# (3) data existant avec config.toml : aucune ligne d'ecriture sous data
# sauf PREMIER-CLIP.txt, clipper init non appele.
# --------------------------------------------------------------------------


def test_existing_data_with_config_skips_init(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text("[pipeline]\n", encoding="utf-8")
    before = _dir_snapshot(data_dir)

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "clipper init sera execute" not in result.stdout
    assert "clipper init non appele" in result.stdout
    assert "PREMIER-CLIP.txt" in result.stdout
    assert _dir_snapshot(data_dir) == before


def test_missing_data_config_calls_init(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "clipper init sera execute" in result.stdout


# --------------------------------------------------------------------------
# (4) Clipper.bat.template rempli, affiche en --dry-run.
# --------------------------------------------------------------------------


def test_clipper_bat_preview_has_path_and_launch_logic(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    stdout = result.stdout
    assert str(app_dir) + "\\ffmpeg\\bin" in stdout
    assert str(app_dir) + "\\.venv\\Scripts" in stdout
    assert "Clipper serve" in stdout
    assert "clipper.exe" in stdout
    assert "127.0.0.1:8000" in stdout
    # Paire entiere quotee (audit 10/10 M4), voir test_a1010_m4_*.
    assert f'set "DATA={data_dir}"' in stdout
    assert 'cd /d "%DATA%"' in stdout


# --------------------------------------------------------------------------
# (TASK-5d378e43fda0) ffmpeg epingle sur une version figee : l'URL ne
# contient ni "latest" ni "master", et le --dry-run l'affiche telle quelle.
# --------------------------------------------------------------------------


def test_sans_raccourci_skips_desktop_shortcut_mention(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(
        installer_dir,
        ["--app", str(app_dir), "--data", str(data_dir), "--sans-raccourci", "--dry-run"],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Bureau" not in result.stdout
    assert "Clipper.lnk" not in result.stdout


def test_without_sans_raccourci_mentions_desktop_shortcut(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Bureau" in result.stdout
    assert "Clipper.lnk" in result.stdout


def test_pip_install_targets_app_venv_explicitly() -> None:
    """Regression (TASK-a093c293ea3f, trouve par le vrai passage) : sans
    --python explicite, 'uv pip install' remonte les dossiers (ou lit
    VIRTUAL_ENV) pour choisir un venv, et peut tomber sur un .venv de
    developpement ambiant totalement different de celui que l'on vient de
    creer sous $App -- observe reellement (clipper installe dans le venv du
    depot, jamais dans app\\.venv)."""
    source = _strip_powershell_comments((INSTALLER_SRC / "install.ps1").read_text(encoding="utf-8"))
    match = re.search(r"&\s*\$Uv pip install[^\r\n]*", source)
    assert match, "commande '& $Uv pip install' introuvable dans install.ps1"
    assert "--python" in match.group(0), match.group(0)
    assert "venvDir" in match.group(0) or "venvPython" in match.group(0)


def test_finish_step_writes_app_version_file() -> None:
    """Regression (TASK-a093c293ea3f, trouve par le vrai passage) : sans
    cette ecriture, Step1-Prepare ne voit jamais app\\version.txt et croit
    toujours etre a la premiere installation ; une relance ne supprime alors
    pas l'ancien .venv et 'uv venv' echoue -- deja observe reellement (une
    mise a jour sur un app existant plantait a chaque fois)."""
    source = _strip_powershell_comments((INSTALLER_SRC / "install.ps1").read_text(encoding="utf-8"))
    match = re.search(r"function Invoke-Step11-Finish\b.*?\n}\n", source, re.DOTALL)
    assert match, "Invoke-Step11-Finish introuvable dans install.ps1"
    body = match.group(0)
    assert re.search(r'Set-Content\s+-Path\s+\(Join-Path\s+\$App\s+"version\.txt"\)', body), body


def test_ffmpeg_url_is_pinned_and_shown_in_dry_run(installer_dir: Path) -> None:
    source = (INSTALLER_SRC / "install.ps1").read_text(encoding="utf-8")
    match = re.search(r'\$FFMPEG_URL\s*=\s*"([^"]+)"', source)
    assert match, "constante $FFMPEG_URL introuvable dans install.ps1"
    url = match.group(1)
    assert "latest" not in url.lower()
    assert "master" not in url.lower()

    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert url in result.stdout


# --------------------------------------------------------------------------
# (5) desinstaller.ps1 --dry-run.
# --------------------------------------------------------------------------


def test_desinstaller_dry_run_lists_app_and_shortcut_not_data_by_default(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    (app_dir / "install.json").write_text(
        f'{{"app": "{app_dir.as_posix()}", "data": "{data_dir.as_posix()}", "version": "{NEW_VERSION}"}}',
        encoding="utf-8",
    )

    result = run_desinstaller(installer_dir, ["--app", str(app_dir), "--dry-run"], port=_free_port())

    assert result.returncode == 0, result.stdout + result.stderr
    assert str(app_dir) in result.stdout
    assert "Clipper.lnk" in result.stdout
    assert str(data_dir) not in result.stdout
    assert app_dir.exists()


def test_desinstaller_dry_run_lists_data_with_donnees(installer_dir: Path) -> None:
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    (app_dir / "install.json").write_text(
        f'{{"app": "{app_dir.as_posix()}", "data": "{data_dir.as_posix()}", "version": "{NEW_VERSION}"}}',
        encoding="utf-8",
    )

    result = run_desinstaller(
        installer_dir, ["--app", str(app_dir), "--donnees", "--dry-run"], port=_free_port()
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert data_dir.as_posix() in result.stdout


def test_desinstaller_refuses_when_console_port_listens(installer_dir: Path, listening_port: int) -> None:
    app_dir = installer_dir / "app"
    app_dir.mkdir()

    result = run_desinstaller(
        installer_dir, ["--app", str(app_dir), "--dry-run"], port=listening_port
    )

    assert result.returncode != 0
    assert str(listening_port) in result.stdout
    assert app_dir.exists()


# --------------------------------------------------------------------------
# (5bis) desinstaller.ps1 : pointeur %LOCALAPPDATA%\Clipper\install.json
# (TASK-8cb4893794e7). LOCALAPPDATA est redirige vers tmp_path, jamais le vrai.
# --------------------------------------------------------------------------


def _pointer_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_localappdata = tmp_path / "localappdata"
    pointer_dir = fake_localappdata / "Clipper"
    pointer_dir.mkdir(parents=True)
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(fake_localappdata)
    return env, pointer_dir, pointer_dir / "install.json"


def _make_app_under(pointer_dir: Path) -> Path:
    app_dir = pointer_dir / "app"
    app_dir.mkdir()
    (app_dir / "install.json").write_text(
        json.dumps({"app": str(app_dir), "data": str(pointer_dir / "data"), "version": NEW_VERSION}),
        encoding="utf-8",
    )
    return app_dir


def test_pointer_for_same_app_is_listed_removed_in_dry_run(tmp_path: Path, installer_dir: Path) -> None:
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = _make_app_under(pointer_dir)
    pointer.write_text(json.dumps({"app": str(app_dir), "version": NEW_VERSION}), encoding="utf-8")

    result = run_desinstaller(installer_dir, ["--app", str(app_dir), "--dry-run"], env=env, port=_free_port())

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{pointer} sera supprime" in result.stdout
    assert pointer.exists()


def test_pointer_for_same_app_matches_case_insensitively(tmp_path: Path, installer_dir: Path) -> None:
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = _make_app_under(pointer_dir)
    pointer.write_text(json.dumps({"app": str(app_dir).upper(), "version": NEW_VERSION}), encoding="utf-8")

    result = run_desinstaller(installer_dir, ["--app", str(app_dir), "--dry-run"], env=env, port=_free_port())

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{pointer} sera supprime" in result.stdout


def test_pointer_for_other_app_is_kept_with_reason(tmp_path: Path, installer_dir: Path) -> None:
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = _make_app_under(pointer_dir)
    other_app = tmp_path / "autre-app"
    pointer.write_text(json.dumps({"app": str(other_app), "version": NEW_VERSION}), encoding="utf-8")

    result = run_desinstaller(installer_dir, ["--app", str(app_dir), "--dry-run"], env=env, port=_free_port())

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{pointer} laisse" in result.stdout
    assert "autre installation" in result.stdout
    assert pointer.exists()


def test_unreadable_pointer_gets_explicit_message_and_is_kept(tmp_path: Path, installer_dir: Path) -> None:
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = _make_app_under(pointer_dir)
    pointer.write_text("{pas du json", encoding="utf-8")

    result = run_desinstaller(installer_dir, ["--app", str(app_dir), "--dry-run"], env=env, port=_free_port())

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{pointer} laisse" in result.stdout
    assert "illisible" in result.stdout
    assert pointer.exists()


def test_pointer_with_invalid_path_chars_is_kept_with_reason_not_crash(tmp_path: Path, installer_dir: Path) -> None:
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = _make_app_under(pointer_dir)
    pointer.write_text(json.dumps({"app": "C:\\a|b<x>", "version": NEW_VERSION}), encoding="utf-8")

    result = run_desinstaller(installer_dir, ["--app", str(app_dir), "--dry-run"], env=env, port=_free_port())

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{pointer} laisse (champ app illisible (C:\\a|b<x>))" in result.stdout
    assert "remede" in result.stdout
    assert pointer.exists()


def test_no_pointer_means_nothing_listed_for_pointer(tmp_path: Path, installer_dir: Path) -> None:
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = _make_app_under(pointer_dir)

    result = run_desinstaller(installer_dir, ["--app", str(app_dir), "--dry-run"], env=env, port=_free_port())

    assert result.returncode == 0, result.stdout + result.stderr
    assert str(pointer) not in result.stdout


def test_real_run_removes_same_app_pointer_and_empty_clipper_dir(tmp_path: Path, installer_dir: Path) -> None:
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = _make_app_under(pointer_dir)
    bureau = tmp_path / "bureau"
    bureau.mkdir()
    pointer.write_text(json.dumps({"app": str(app_dir), "version": NEW_VERSION}), encoding="utf-8")

    result = run_desinstaller(
        installer_dir, ["--app", str(app_dir)], env=env, port=_free_port(), bureau=bureau
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not app_dir.exists()
    assert not pointer.exists()
    assert not pointer_dir.exists()


def test_real_run_keeps_clipper_dir_when_not_empty(tmp_path: Path, installer_dir: Path) -> None:
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = _make_app_under(pointer_dir)
    bureau = tmp_path / "bureau"
    bureau.mkdir()
    (pointer_dir / "reste.txt").write_text("autre chose", encoding="utf-8")
    pointer.write_text(json.dumps({"app": str(app_dir), "version": NEW_VERSION}), encoding="utf-8")

    result = run_desinstaller(
        installer_dir, ["--app", str(app_dir)], env=env, port=_free_port(), bureau=bureau
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not pointer.exists()
    assert (pointer_dir / "reste.txt").exists()


def test_real_run_keeps_pointer_of_other_app(tmp_path: Path, installer_dir: Path) -> None:
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = _make_app_under(pointer_dir)
    bureau = tmp_path / "bureau"
    bureau.mkdir()
    other_app = tmp_path / "autre-app"
    pointer.write_text(json.dumps({"app": str(other_app), "version": NEW_VERSION}), encoding="utf-8")

    result = run_desinstaller(
        installer_dir, ["--app", str(app_dir)], env=env, port=_free_port(), bureau=bureau
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert pointer.exists()
    assert not app_dir.exists()


# --------------------------------------------------------------------------
# (7) PREMIER-CLIP.txt : francais, dit comment faire un premier clip.
# --------------------------------------------------------------------------


def test_premier_clip_txt_is_french_and_actionable() -> None:
    text = (INSTALLER_SRC / "PREMIER-CLIP.txt").read_text(encoding="utf-8")
    assert len(text.strip()) > 0
    assert "clip" in text.lower()
    assert "console" in text.lower()


# --------------------------------------------------------------------------
# TASK-4f1d7d1ee341 : les 16 points confirmes de la relecture Fable
# (research/reviews/installeur.md, C1-C3, I1-I7, M1-M6).
# --------------------------------------------------------------------------


REDUCED_PATH = "C:\\Windows\\System32;C:\\Windows;C:\\Windows\\System32\\WindowsPowerShell\\v1.0"


def _env_with_reduced_path() -> dict[str, str]:
    """PATH d'un PC sans outil de developpement (System32 seul, meme
    disposition que research/reviews/scratch-installeur/path_vierge.ps1) :
    ni uv ni clipper n'y sont reperables."""
    env = dict(os.environ)
    env["PATH"] = REDUCED_PATH
    return env


def test_c1_uv_called_by_full_path_not_bare(installer_dir: Path) -> None:
    """C1 : sans uv.exe a cote d'Installer.bat (jamais dans ce fixture) et
    sans uv sur le PATH, l'etape 2 doit echouer par un message rouge
    explicite (Fail), jamais par l'exception PowerShell brute que produirait
    un appel a nu '& uv' ('Le terme «uv» n'est pas reconnu...')."""
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(
        installer_dir, ["--app", str(app_dir), "--data", str(data_dir)], env=_env_with_reduced_path()
    )

    assert result.returncode != 0
    assert "uv.exe" in result.stdout
    assert "n'est pas reconnu" not in result.stdout
    assert not (app_dir / ".venv").exists()


def test_c2_clipper_called_by_full_path_not_bare() -> None:
    """C2 : Invoke-Step7-Data et Invoke-Step8-Models appellent clipper via
    un chemin complet sous $App\\.venv\\Scripts, jamais '& clipper' a nu
    (qui, avant l'etape 10, resoudrait au mieux un clipper ambiant d'un
    autre venv, au pire rien du tout sur un PC cible)."""
    source = _strip_powershell_comments((INSTALLER_SRC / "install.ps1").read_text(encoding="utf-8"))
    for fn_name in ("Invoke-Step7-Data", "Invoke-Step8-Models"):
        match = re.search(rf"function {fn_name}\b.*?\n}}\n", source, re.DOTALL)
        assert match, f"{fn_name} introuvable dans install.ps1"
        body = match.group(0)
        assert re.search(r'\$clipper\s*=\s*Join-Path\s+\$App\s+"\.venv\\Scripts\\clipper\.exe"', body), body
        assert "& $clipper" in body, body
        assert "& clipper " not in body, body


def test_c3_venv_removed_even_on_interrupted_fresh_install(installer_dir: Path) -> None:
    """C3 : une premiere installation interrompue apres l'etape 3 (donc
    app\\.venv existe, mais app\\version.txt n'a jamais ete ecrit, seulement
    a l'etape 11) doit quand meme voir .venv supprime a la relance, sinon
    'uv venv' echoue dessus a chaque nouvelle tentative."""
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    venv_dir = app_dir / ".venv"
    venv_dir.mkdir()
    (venv_dir / "marker.txt").write_text("installation precedente interrompue", encoding="utf-8")
    # Audit 10/10 M6 : uv.exe, la wheel et overrides.txt sont controles avant
    # la suppression de .venv (prevol), donc le zip doit etre "complet" ici
    # pour atteindre cette suppression : un faux uv.exe (where.exe, qui sort
    # en code 1 sur 'python install 3.11', donc Fail a l'etape 2 sans
    # reseau) et une wheel vide a cote de version.txt.
    shutil.copy(Path(os.environ["SystemRoot"]) / "System32" / "where.exe", installer_dir / "uv.exe")
    (installer_dir / f"clipper-{NEW_VERSION}-py3-none-any.whl").write_bytes(b"")

    result = run_install(
        installer_dir, ["--app", str(app_dir), "--data", str(data_dir)], env=_env_with_reduced_path(), port=_free_port()
    )

    assert result.returncode != 0  # echoue plus tard, a l'etape 2 (faux uv en echec)
    assert "premiere installation" in result.stdout
    assert "uv python install 3.11" in result.stdout
    assert not venv_dir.exists()


def test_i1_opencv_override_shipped_in_zip_and_verified() -> None:
    """I1 : l'override OpenCV est un fichier livre dans le zip
    (installer/overrides.txt, jamais un pyproject.toml ambiant trouve en
    remontant depuis le dossier courant) et passe explicitement en
    --override, puis l'environnement est verifie (un seul paquet
    opencv*.dist-info et 'import cv2' fonctionne) plutot que suppose
    correct."""
    overrides_content = (INSTALLER_SRC / "overrides.txt").read_text(encoding="utf-8")
    assert "opencv-python" in overrides_content

    source = _strip_powershell_comments((INSTALLER_SRC / "install.ps1").read_text(encoding="utf-8"))
    match = re.search(r"function Invoke-Step3-Venv\b.*?\n}\n", source, re.DOTALL)
    assert match, "Invoke-Step3-Venv introuvable dans install.ps1"
    body = match.group(0)
    assert re.search(r'Join-Path\s+\$Root\s+"installer\\overrides\.txt"', body), body
    assert "--override" in body, body
    assert "opencv*.dist-info" in body, body
    assert "import cv2" in body, body


def test_i2_launcher_written_with_oem_encoding_for_accented_data_path(tmp_path: Path, zip_layout_dir: Path) -> None:
    """I2 : Clipper.bat est ecrit dans la page de code OEM (celle que lit
    cmd.exe), jamais la page ANSI par defaut de Set-Content en PowerShell
    5.1 : un chemin de donnees accentue doit survivre a l'aller-retour."""
    accent = "\u00e9"
    result, app_dir, data_dir = run_step9_launcher(
        tmp_path, zip_layout_dir, data_name=f"d{accent}sir{accent}-donnees"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    launcher_path = app_dir / "Clipper.bat"
    assert launcher_path.is_file()
    oem_code_page = ctypes.windll.kernel32.GetOEMCP()
    decoded = launcher_path.read_bytes().decode(f"cp{oem_code_page}")
    assert str(data_dir) in decoded, decoded


def test_i3_update_refuses_when_console_port_listens_before_removing_venv(
    installer_dir: Path, listening_port: int
) -> None:
    """I3 : en mise a jour, le port de la console est controle avant toute
    suppression de .venv (R6) ; sans ce controle, une console laissee
    ouverte se retrouve avec un .venv a moitie supprime sous elle."""
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    (app_dir / "version.txt").write_text(OLDER_VERSION, encoding="utf-8")
    venv_dir = app_dir / ".venv"
    venv_dir.mkdir()
    (venv_dir / "marker.txt").write_text("venv existant", encoding="utf-8")

    result = run_install(
        installer_dir, ["--app", str(app_dir), "--data", str(data_dir)], port=listening_port
    )

    assert result.returncode != 0
    assert str(listening_port) in result.stdout
    assert venv_dir.exists()


@pytest.mark.parametrize("bat_name", ["Installer.bat", "Desinstaller.bat"])
def test_i4_bat_pauses_on_error_for_double_click_visibility(bat_name: str) -> None:
    """I4 : en double-clic, cmd fermerait la fenetre (et le message rouge
    avec elle) en moins d'une seconde a la fin du script ; une pause
    conditionnee a l'echec le garde visible, sans jamais gener un appel
    reussi depuis un terminal ou un test."""
    text = (INSTALLER_SRC / bat_name).read_text(encoding="utf-8")
    assert "if not %EXIT_CODE%==0 pause" in text, text


def test_i5_rereads_install_json_pointer_when_app_and_data_not_given(installer_dir: Path, tmp_path: Path) -> None:
    """I5 : sans --app ni --data, une relance relit le pointeur laisse par
    une installation precedente plutot que de retomber sur les chemins par
    defaut (vides) et de donner l'impression que les clips sont perdus."""
    fake_localappdata = tmp_path / "localappdata"
    pointer_dir = fake_localappdata / "Clipper"
    pointer_dir.mkdir(parents=True)
    custom_app = str(tmp_path / "CustomApp")
    custom_data = str(tmp_path / "CustomData")
    (pointer_dir / "install.json").write_text(
        json.dumps({"app": custom_app, "data": custom_data, "version": NEW_VERSION}),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(fake_localappdata)

    result = run_install(installer_dir, ["--dry-run"], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"app={custom_app}" in result.stdout
    assert f"data={custom_data}" in result.stdout


def test_i5_finish_step_writes_pointer_install_json() -> None:
    """I5 (ecriture) : Step11-Finish ecrit aussi le pointeur sous
    %LOCALAPPDATA%\\Clipper\\install.json, a cote de app\\install.json,
    pour que la relecture ci-dessus fonctionne meme avec --app personnalise."""
    source = _strip_powershell_comments((INSTALLER_SRC / "install.ps1").read_text(encoding="utf-8"))
    match = re.search(r"function Invoke-Step11-Finish\b.*?\n}\n", source, re.DOTALL)
    assert match, "Invoke-Step11-Finish introuvable dans install.ps1"
    body = match.group(0)
    assert 'Join-Path $env:LOCALAPPDATA "Clipper"' in body, body
    assert body.count("install.json") >= 2, body


def test_i6_desinstaller_refuses_when_app_has_no_install_marker(installer_dir: Path) -> None:
    """I6 : un dossier $App sans install.json ni version.txt n'est pas une
    installation Clipper connue ; avant ce correctif, Desinstaller.bat
    affichait "Desinstallation terminee." en vert sans rien supprimer (ou,
    avec un --app errone, supprimait n'importe quel dossier sans verifier)."""
    app_dir = installer_dir / "app"
    app_dir.mkdir()
    (app_dir / "unrelated.txt").write_text("rien a voir", encoding="utf-8")

    result = run_desinstaller(installer_dir, ["--app", str(app_dir), "--dry-run"], port=_free_port())

    assert result.returncode != 0
    assert "install.json" in result.stdout
    assert app_dir.exists()


def test_i7_desinstaller_copied_into_app(tmp_path: Path, zip_layout_dir: Path) -> None:
    """I7 : Desinstaller.bat et desinstaller.ps1 sont copies dans app a
    l'etape 9, sinon ils disparaissent avec le dossier dezippe (R2, docs)."""
    result, app_dir, _data_dir = run_step9_launcher(tmp_path, zip_layout_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (app_dir / "Desinstaller.bat").is_file()
    assert (app_dir / "installer" / "desinstaller.ps1").is_file()


def test_m1_icon_copied_into_app_and_shortcut_points_there(tmp_path: Path, zip_layout_dir: Path) -> None:
    """M1 : clipper.ico est copie dans app (le raccourci pointe sur cette
    copie, pas sur le zip dezippe, qui peut disparaitre apres l'installation
    sans casser l'icone du raccourci)."""
    result, app_dir, _data_dir = run_step9_launcher(tmp_path, zip_layout_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (app_dir / "clipper.ico").is_file()

    source = _strip_powershell_comments((INSTALLER_SRC / "install.ps1").read_text(encoding="utf-8"))
    match = re.search(r"function Invoke-Step9-Launcher\b.*?\n}\n", source, re.DOTALL)
    assert match, "Invoke-Step9-Launcher introuvable dans install.ps1"
    body = match.group(0)
    assert re.search(r"\$lnk\.IconLocation\s*=\s*\"\$appIconPath,0\"", body), body


def test_m2_relative_app_and_data_resolved_to_absolute(installer_dir: Path) -> None:
    """M2 : --app/--data relatifs sont resolus en absolu avant d'etre
    ecrits dans le lanceur ou install.json, sinon le lanceur et le
    raccourci ne fonctionnent plus lances d'un autre dossier."""
    cwd = installer_dir

    result = run_install(installer_dir, ["--app", "rel-app", "--data", "rel-data", "--dry-run"], cwd=cwd)

    assert result.returncode == 0, result.stdout + result.stderr
    expected_app = str((cwd / "rel-app").resolve())
    expected_data = str((cwd / "rel-data").resolve())
    assert f"app={expected_app}" in result.stdout
    assert f"data={expected_data}" in result.stdout
    assert "app=rel-app" not in result.stdout
    assert "data=rel-data" not in result.stdout


def test_m3_app_option_without_value_fails_cleanly(installer_dir: Path) -> None:
    """M3 : --app (ou --data) sans valeur apres doit produire un message
    rouge explicite, jamais l'exception PowerShell brute ('Impossible de
    lier l'argument au parametre') que leve un index hors bornes sur
    $RawArgs."""
    result = run_install(installer_dir, ["--app"])

    assert result.returncode != 0
    assert "sans valeur" in result.stdout.lower()
    assert "impossible de lier" not in (result.stdout + result.stderr).lower()


def test_m4_cache_dir_set_before_step2_python_install() -> None:
    """M4 : UV_CACHE_DIR est pose avant le premier appel a uv (etape 2),
    sinon 'uv python install' ecrit l'archive CPython telechargee dans
    %LOCALAPPDATA%\\uv\\cache, jamais nettoyee par l'etape 11."""
    source = _strip_powershell_comments((INSTALLER_SRC / "install.ps1").read_text(encoding="utf-8"))
    match = re.search(r"function Invoke-Step2-Python\b.*?\n}\n", source, re.DOTALL)
    assert match, "Invoke-Step2-Python introuvable dans install.ps1"
    body = match.group(0)
    cache_pos = body.find("UV_CACHE_DIR")
    call_pos = body.find("$Uv python install 3.11")
    assert cache_pos != -1, body
    assert call_pos != -1, body
    assert cache_pos < call_pos, body


def test_m5_premier_clip_not_rewritten_when_already_present(installer_dir: Path) -> None:
    """M5 : PREMIER-CLIP.txt n'est recopie que s'il manque (premiere
    installation ou fichier disparu), jamais a chaque mise a jour : R6 et
    la doc disent que data n'est jamais ecrit sans raison."""
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text("[pipeline]\n", encoding="utf-8")
    (data_dir / "PREMIER-CLIP.txt").write_text("deja la, ne pas toucher", encoding="utf-8")

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PREMIER-CLIP.txt sera ecrit" not in result.stdout
    assert "deja present" in result.stdout


def test_m6_premier_clip_txt_does_not_mention_phantom_doctor_menu() -> None:
    """M6 : PREMIER-CLIP.txt ne renvoie plus vers un bouton "Reglages" de la
    console qui n'existe pas (clipper/web/ n'a aucun appel a doctor)."""
    text = (INSTALLER_SRC / "PREMIER-CLIP.txt").read_text(encoding="utf-8")
    assert "Reglages" not in text, text


# --------------------------------------------------------------------------
# Audit complet du 10/10, lot M (TASK-b2235d04aafd ; research/reviews/
# audit-1010/installeur.md, contre-verif.md) : I1-I5, M1-M6. Chaque test a
# ete vu ROUGE sur le code d'avant correctif. LOCALAPPDATA est toujours
# redirige vers tmp_path (jamais le vrai), le Bureau vers -Bureau, le port
# vers -Port.
# --------------------------------------------------------------------------


def _copy_desinstaller_into_app(app_dir: Path, installer_dir: Path) -> Path:
    """Reproduit la copie de l'etape 9 (I7) : app\\Desinstaller.bat et
    app\\installer\\desinstaller.ps1, exactement comme sur une installation
    reelle (le seul Desinstaller.bat que l'utilisateur a encore une fois le
    zip supprime, INSTALLATION.md)."""
    (app_dir / "installer").mkdir(parents=True, exist_ok=True)
    shutil.copy(installer_dir / "installer" / "Desinstaller.bat", app_dir / "Desinstaller.bat")
    shutil.copy(installer_dir / "installer" / "desinstaller.ps1", app_dir / "installer" / "desinstaller.ps1")
    return app_dir / "Desinstaller.bat"


def _fake_installed_app(app_dir: Path, data_dir: Path) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "install.json").write_text(
        json.dumps({"app": str(app_dir), "data": str(data_dir), "version": NEW_VERSION}),
        encoding="utf-8",
    )
    (app_dir / "version.txt").write_text(NEW_VERSION, encoding="utf-8")
    (app_dir / ".venv").mkdir(exist_ok=True)
    (app_dir / ".venv" / "marker.txt").write_text("venv", encoding="utf-8")


def test_a1010_i1_desinstaller_bat_launched_from_app_removes_app_shortcut_and_pointer(
    tmp_path: Path, installer_dir: Path
) -> None:
    """I1 : double-clic sur app\\Desinstaller.bat (le chemin documente) donne
    a cmd puis a powershell le dossier app comme dossier courant ;
    Remove-Item refusait alors de supprimer app (« en cours d'utilisation »)
    et le script s'arretait avant le raccourci et le pointeur."""
    env, pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = pointer_dir / "app"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text("[pipeline]\n", encoding="utf-8")
    _fake_installed_app(app_dir, data_dir)
    bat = _copy_desinstaller_into_app(app_dir, installer_dir)
    pointer.write_text(json.dumps({"app": str(app_dir), "data": str(data_dir)}), encoding="utf-8")
    bureau = tmp_path / "bureau"
    bureau.mkdir()
    (bureau / "Clipper.lnk").write_bytes(b"lnk")

    result = subprocess.run(
        ["cmd", "/c", str(bat), "-Port", str(_free_port()), "-Bureau", str(bureau), "--app", str(app_dir)],
        cwd=str(app_dir),
        env=env,
        input="N\n",
        capture_output=True,
        text=True,
        timeout=120,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "en cours d'utilisation" not in combined
    assert not app_dir.exists(), combined
    assert not (bureau / "Clipper.lnk").exists()
    assert not pointer.exists()
    assert (data_dir / "config.toml").exists()  # reponse N : donnees conservees


def test_a1010_i2_desinstaller_without_app_option_finds_install_next_to_its_script(
    tmp_path: Path, installer_dir: Path
) -> None:
    """I2 : app\\Desinstaller.bat copie dans un --app personnalise, lance
    sans --app, doit retrouver SA propre installation (install.json a cote
    du script) au lieu de chercher sous %LOCALAPPDATA%\\Clipper\\app."""
    env, _pointer_dir, _pointer = _pointer_env(tmp_path)
    app_dir = tmp_path / "perso" / "Clipper"
    data_dir = tmp_path / "perso" / "clips"
    _fake_installed_app(app_dir, data_dir)
    _copy_desinstaller_into_app(app_dir, installer_dir)

    result = run_desinstaller(app_dir, ["--dry-run"], env=env, port=_free_port())

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{app_dir} sera supprime" in result.stdout


def test_a1010_i2_desinstaller_without_app_option_falls_back_to_pointer(
    tmp_path: Path, installer_dir: Path
) -> None:
    """I2 (suite) : le Desinstaller.bat du zip (aucun install.json a cote)
    lance sans --app lit le pointeur %LOCALAPPDATA%\\Clipper\\install.json
    (ecrit exactement pour ca par l'etape 11) avant le dossier par defaut."""
    env, _pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = tmp_path / "perso" / "Clipper"
    data_dir = tmp_path / "perso" / "clips"
    _fake_installed_app(app_dir, data_dir)
    pointer.write_text(json.dumps({"app": str(app_dir), "data": str(data_dir)}), encoding="utf-8")

    result = run_desinstaller(installer_dir, ["--dry-run"], env=env, port=_free_port())

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{app_dir} sera supprime" in result.stdout


def test_a1010_i3_app_only_rereads_data_from_app_install_json(tmp_path: Path, installer_dir: Path) -> None:
    """I3 : --app seul (sans --data) sur une installation existante doit
    reprendre le data de <app>\\install.json (R2 « relues comme defauts »),
    jamais retomber sur Documents\\Clipper vide (bibliotheque « perdue »)."""
    env, _pointer_dir, _pointer = _pointer_env(tmp_path)
    app_dir = tmp_path / "perso" / "app"
    data_dir = tmp_path / "perso" / "clips"
    _fake_installed_app(app_dir, data_dir)
    (app_dir / "version.txt").write_text(OLDER_VERSION, encoding="utf-8")

    result = run_install(installer_dir, ["--dry-run", "--app", str(app_dir)], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"data={data_dir}" in result.stdout
    assert "Documents" not in result.stdout.split("[2/11]")[0]


def test_a1010_i3_data_only_rereads_app_from_pointer(tmp_path: Path, installer_dir: Path) -> None:
    """I3 (suite) : --data seul reprend le app du pointeur, pas le dossier
    par defaut (qui serait une seconde installation vide)."""
    env, _pointer_dir, pointer = _pointer_env(tmp_path)
    app_dir = tmp_path / "perso" / "app"
    data_dir = tmp_path / "perso" / "clips"
    new_data = tmp_path / "nouveaux-clips"
    _fake_installed_app(app_dir, data_dir)
    pointer.write_text(json.dumps({"app": str(app_dir), "data": str(data_dir)}), encoding="utf-8")

    result = run_install(installer_dir, ["--dry-run", "--data", str(new_data)], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"app={app_dir}" in result.stdout
    assert f"data={new_data}" in result.stdout


@pytest.mark.parametrize("data_rel", ["", "data"])
def test_a1010_i4_data_equal_to_or_under_app_is_refused(installer_dir: Path, data_rel: str) -> None:
    """I4 : data == app ou data sous app : la desinstallation supprime app
    sans condition, donc les donnees partiraient malgre la reponse N
    (INSTALLATION.md « les donnees sont conservees par defaut »)."""
    app_dir = installer_dir / "x"
    data_dir = app_dir / data_rel if data_rel else app_dir

    result = run_install(installer_dir, ["--dry-run", "--app", str(app_dir), "--data", str(data_dir)])

    assert result.returncode != 0, result.stdout + result.stderr
    assert "[installer] ERREUR" in result.stdout
    assert str(app_dir) in result.stdout
    assert str(data_dir) in result.stdout
    assert "[1/11]" not in result.stdout


def test_a1010_i4_data_beside_app_is_still_accepted(installer_dir: Path) -> None:
    """I4 (garde-fou du garde-fou) : un data voisin dont le nom commence
    par celui de app (C:\\x et C:\\xy) n'est pas « sous app »."""
    app_dir = installer_dir / "x"
    data_dir = installer_dir / "xy"

    result = run_install(installer_dir, ["--dry-run", "--app", str(app_dir), "--data", str(data_dir)])

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def failing_nvidia_smi(tmp_path: Path) -> Path:
    """nvidia-smi simule en ECHEC (pilote casse, GPU retire, VM) : message
    non vide, code de sortie 9, exactement ce qu'ecrit le vrai binaire dans
    ce cas."""
    bin_dir = tmp_path / "fakebin-ko"
    bin_dir.mkdir()
    script = bin_dir / "nvidia-smi.bat"
    script.write_text(
        "@echo off\r\n"
        "echo NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\r\n"
        "exit /b 9\r\n",
        encoding="utf-8",
    )
    return bin_dir


def test_a1010_i5_failing_nvidia_smi_means_cpu(installer_dir: Path, failing_nvidia_smi: Path) -> None:
    """I5 : un nvidia-smi present mais en echec n'est pas un GPU detecte
    (R4 : jamais 2 Go telecharges sans GPU) ; seule une sortie non vide
    avec code 0 vaut detection."""
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    env = _env_with_prepended_path(failing_nvidia_smi)

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir), "--dry-run"], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[cuda]" not in result.stdout
    assert "CPU" in result.stdout


@pytest.mark.parametrize("content", ["{not json", '{"app": "C:\\\\a|b<x>"}'])
def test_a1010_m1_corrupt_pointer_fails_cleanly_in_installer(
    tmp_path: Path, installer_dir: Path, content: str
) -> None:
    """M1 : un pointeur illisible ou au chemin invalide donnait une exception
    .NET brute (ConvertFrom-Json / Test-Path) ; attendu : Fail nommant le
    fichier et un remede (ADR-ad2e)."""
    env, _pointer_dir, pointer = _pointer_env(tmp_path)
    pointer.write_text(content, encoding="utf-8")

    result = run_install(installer_dir, ["--dry-run"], env=env)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "[installer] ERREUR" in result.stdout
    assert str(pointer) in result.stdout
    assert "remede" in result.stdout
    assert "ConvertFrom-Json :" not in combined
    assert "Test-Path :" not in combined
    assert "Join-Path :" not in combined


def test_a1010_m1_corrupt_app_install_json_fails_cleanly_in_desinstaller(
    tmp_path: Path, installer_dir: Path
) -> None:
    """M1 (desinstalleur) : <app>\\install.json illisible = Fail nommant le
    fichier, jamais l'exception brute de ConvertFrom-Json (observee pendant
    la preuve A : « Sequence d'echappement non reconnue »)."""
    env, _pointer_dir, _pointer = _pointer_env(tmp_path)
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "install.json").write_text('{"app": "C:\\x\\y"', encoding="utf-8")

    result = run_desinstaller(installer_dir, ["--app", str(app_dir), "--dry-run"], env=env, port=_free_port())

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "[desinstaller] ERREUR" in result.stdout
    assert str(app_dir / "install.json") in result.stdout
    assert "ConvertFrom-Json :" not in combined
    assert app_dir.exists()


def test_a1010_m2_trailing_backslash_quote_is_trimmed(installer_dir: Path) -> None:
    """M2 : Installer.bat --data "C:\\Mes Docs\\" (antislash final, completion
    PowerShell) : powershell -File lit \\" comme un guillemet echappe et le
    chemin se termine par un guillemet, accepte en --dry-run et casse a
    New-Item en reel."""
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "Mes Docs"

    result = run_install(installer_dir, ["--dry-run", "--app", str(app_dir), "--data", str(data_dir) + '\\"'])

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"data={data_dir}" in result.stdout
    assert f'data={data_dir}"' not in result.stdout


def test_a1010_m2_invalid_path_chars_are_refused(installer_dir: Path) -> None:
    """M2 (suite) : un caractere interdit dans --app/--data est refuse par
    Fail avant la premiere etape, jamais une exception .NET plus tard."""
    app_dir = installer_dir / "app"

    result = run_install(installer_dir, ["--dry-run", "--app", str(app_dir), "--data", "C:\\a|b<x>"])

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "[installer] ERREUR" in result.stdout
    assert "C:\\a|b<x>" in result.stdout
    assert "Test-Path :" not in combined
    assert "Join-Path :" not in combined


def test_a1010_m3_desinstaller_app_option_without_value_fails_cleanly(installer_dir: Path) -> None:
    """M3 : --app sans valeur dans desinstaller.ps1 : meme garde que
    install.ps1 (message rouge « sans valeur »), jamais « Impossible de lier
    l'argument au parametre »."""
    result = run_desinstaller(installer_dir, ["--app"], port=_free_port())

    combined = (result.stdout + result.stderr).lower()
    assert result.returncode != 0
    assert "sans valeur" in combined
    assert "impossible de lier" not in combined


def test_a1010_m4_launcher_template_quotes_set_pairs() -> None:
    """M4 : set APP=C:\\A&B\\app non quote fait executer « B\\app » par cmd et
    tronque APP ; la paire entiere doit etre quotee (set "APP=...")."""
    text = (INSTALLER_SRC / "Clipper.bat.template").read_text(encoding="utf-8")
    assert re.search(r'^set "APP=__APP__"\s*$', text, re.MULTILINE), text
    assert re.search(r'^set "DATA=__DATA__"\s*$', text, re.MULTILINE), text
    assert not re.search(r"^set (APP|DATA|PATH)=", text, re.MULTILINE), text


def test_a1010_m4_launcher_with_ampersand_in_path_keeps_app_and_data(tmp_path: Path) -> None:
    """M4 (preuve cmd) : le gabarit rempli avec un chemin contenant « & »
    doit laisser APP et DATA intacts dans cmd. Seules les lignes set du
    gabarit sont executees ici (jamais netstat/start)."""
    template = (INSTALLER_SRC / "Clipper.bat.template").read_text(encoding="utf-8")
    app = str(tmp_path / "A&B" / "app")
    data = str(tmp_path / "A&B" / "data")
    set_lines = [line for line in template.splitlines() if line.strip().lower().startswith("set ")]
    assert set_lines, template
    script = tmp_path / "probe.bat"
    # Expansion retardee (!APP!) pour l'affichage : avec %APP%, c'est la
    # ligne echo elle-meme que le & couperait, quel que soit le gabarit.
    script.write_text(
        "@echo off\r\nsetlocal enabledelayedexpansion\r\n"
        + "\r\n".join(line.replace("__APP__", app).replace("__DATA__", data) for line in set_lines)
        + "\r\necho APP=[!APP!]\r\necho DATA=[!DATA!]\r\n",
        encoding="utf-8",
    )

    result = subprocess.run(["cmd", "/c", str(script)], capture_output=True, text=True, timeout=30)

    combined = result.stdout + result.stderr
    assert f"APP=[{app}]" in combined, combined
    assert f"DATA=[{data}]" in combined, combined
    assert "n'est pas reconnu" not in combined, combined


def test_a1010_m5_fresh_install_with_venv_and_console_port_refuses_before_removing_venv(
    installer_dir: Path, listening_port: int
) -> None:
    """M5 : premiere installation interrompue (pas de version.txt) mais
    console deja lancee : le controle du port (R6) doit avoir lieu des que
    .venv existe, pas seulement en mise a jour, sinon Remove-Item .venv
    tombe sur python.exe verrouille."""
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    venv_dir = app_dir / ".venv"
    venv_dir.mkdir()
    (venv_dir / "marker.txt").write_text("venv existant", encoding="utf-8")

    result = run_install(installer_dir, ["--app", str(app_dir), "--data", str(data_dir)], port=listening_port)

    assert result.returncode != 0
    assert str(listening_port) in result.stdout
    assert venv_dir.exists()


def test_a1010_m6_update_keeps_venv_when_uv_exe_missing(installer_dir: Path) -> None:
    """M6 : en mise a jour, uv.exe, la wheel et overrides.txt (aucun reseau)
    sont controles AVANT la suppression de .venv ; un zip incomplet ne doit
    jamais detruire l'installation existante."""
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    (app_dir / "version.txt").write_text(OLDER_VERSION, encoding="utf-8")
    venv_dir = app_dir / ".venv"
    venv_dir.mkdir()
    (venv_dir / "marker.txt").write_text("venv existant", encoding="utf-8")

    result = run_install(
        installer_dir, ["--app", str(app_dir), "--data", str(data_dir)], env=_env_with_reduced_path(), port=_free_port()
    )

    assert result.returncode != 0
    assert "uv.exe" in result.stdout
    assert venv_dir.exists(), result.stdout


def test_a1010_m6_update_keeps_venv_when_wheel_missing(installer_dir: Path) -> None:
    """M6 (suite) : uv.exe present mais wheel absente : meme prevol, .venv
    intact."""
    (installer_dir / "uv.exe").write_bytes(b"MZ")
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"
    app_dir.mkdir()
    (app_dir / "version.txt").write_text(OLDER_VERSION, encoding="utf-8")
    venv_dir = app_dir / ".venv"
    venv_dir.mkdir()
    (venv_dir / "marker.txt").write_text("venv existant", encoding="utf-8")

    result = run_install(
        installer_dir, ["--app", str(app_dir), "--data", str(data_dir)], env=_env_with_reduced_path(), port=_free_port()
    )

    assert result.returncode != 0
    assert "wheel" in result.stdout
    assert venv_dir.exists(), result.stdout


def test_a1010_m6_dry_run_announces_missing_zip_pieces(installer_dir: Path) -> None:
    """M6 (dry-run) : le prevol est aussi affiche en --dry-run (uv.exe absent
    du fixture) sans faire echouer le plan."""
    app_dir = installer_dir / "app"
    data_dir = installer_dir / "data"

    result = run_install(installer_dir, ["--dry-run", "--app", str(app_dir), "--data", str(data_dir)])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "uv.exe" in result.stdout
    assert "s'arreterait" in result.stdout
