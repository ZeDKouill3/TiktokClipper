"""TASK-a093c293ea3f (SPEC-54ed21c61caf R9, ADR-e1dac9ba2284) : le seul test
reel de l'installeur portable. Jamais lance par defaut ni en CI (ADR-ad2e :
aucun repli silencieux sur un test qui aurait du tourner - il est saute
explicitement, pas masque).

Saute sauf si ``CLIPPER_INSTALLER_REAL=1`` est positionne explicitement, et
seulement si ``powershell`` et ``uv`` sont dans le PATH. Quand il tourne :
construit le vrai zip (vrai telechargement de uv.exe), l'installe dans un
dossier temporaire sous ``research/installer-real/`` (jamais
``%LOCALAPPDATA%\\Clipper``, jamais le Bureau reel : ``--sans-raccourci``),
en CPU ; verifie le contenu de app/ et data/, puis ``clipper doctor``, puis
une mise a jour sur le meme ``app`` (data inchange), puis la desinstallation
complete. Les .bat et ``clipper doctor`` tournent avec un PATH reduit a
System32 + PowerShell (TASK-4f1d7d1ee341, ``_reduced_path_env``) : ni uv, ni
clipper, ni ``.venv`` du depot n'y sont reperables, pour ne jamais masquer un
appel a nu comme l'avait fait TASK-a093 (uv/.venv du PC de dev en tete du
PATH, cwd sous le depot pour l'override OpenCV).

Compte plusieurs minutes et environ 700 Mo telecharges (Python, ffmpeg,
modeles) : ne jamais lancer en parallele d'un autre travail reseau/CPU lourd
sur la meme machine."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import build_portable  # noqa: E402

POWERSHELL = shutil.which("powershell")
UV = shutil.which("uv")
REAL_ENABLED = os.environ.get("CLIPPER_INSTALLER_REAL") == "1"

pytestmark = pytest.mark.skipif(
    not REAL_ENABLED or POWERSHELL is None or UV is None,
    reason=(
        "test reel optionnel : positionne CLIPPER_INSTALLER_REAL=1 "
        "explicitement (et verifie que powershell et uv sont dans le PATH) "
        "pour le lancer ; jamais saute en CI silencieusement"
    ),
)

# Jamais %LOCALAPPDATA%\Clipper ni le Bureau reel : un dossier de travail
# sous research/ (gitignore), a cote du depot.
WORK_ROOT = REPO_ROOT / "research" / "installer-real"

# L'installation complete (python, venv, ffmpeg, modeles) prend plusieurs
# minutes sur un reseau normal ; generous mais borne pour ne jamais pendre
# indefiniment si une etape reste bloquee.
INSTALL_TIMEOUT = 1800
DOCTOR_TIMEOUT = 120
UNINSTALL_TIMEOUT = 60


# TASK-4f1d7d1ee341 (research/reviews/installeur.md, intro) : le test reel
# de TASK-a093 tournait avec le PATH complet du PC de dev (uv global,
# .venv\Scripts du depot en tete), ce qui masquait C1/C2 (uv/clipper
# appeles a nu, resolus par accident sur le mauvais binaire au lieu
# d'echouer). Reduit ici a System32 + PowerShell (ni uv, ni clipper, ni
# .venv du depot) dans les sous-processus qui executent les .bat et
# 'clipper doctor' : seul uv.exe du zip et le clipper.exe installe doivent
# fonctionner.
REDUCED_PATH = r"C:\Windows\System32;C:\Windows;C:\Windows\System32\WindowsPowerShell\v1.0"


def _reduced_path_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = REDUCED_PATH
    return env


def _run_bat(bat_path: Path, args: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    # cmd.exe /c explicite (jamais shell=True) : CreateProcess seul
    # (shell=False, sans passer par cmd) ne sait pas lancer un .bat, qui
    # n'est pas un executable Win32 mais depend de l'association de fichier.
    cmd = ["cmd", "/c", str(bat_path), *args]
    # encoding/errors explicites (TASK-4f1d7d1ee341) : la sortie de 'claude'
    # (install officiel, jamais exerce avant un PATH reduit qui le force a
    # s'installer ici) contient des octets hors de la page de code par
    # defaut (cp1252 sur ce PC), qui faisaient planter le thread lecteur de
    # subprocess (UnicodeDecodeError) et laissaient stdout/stderr a None.
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=_reduced_path_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_installer_real_full_cycle() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=WORK_ROOT, prefix="run-"))
    started = time.monotonic()
    succeeded = False
    try:
        # ------------------------------------------------------------------
        # (2) construction du zip par tools.build_portable.build, vrai
        # telechargement de uv.exe (default_fetch_uv).
        # ------------------------------------------------------------------
        dist_dir = work / "dist"
        dist_dir.mkdir(parents=True)
        version = build_portable._pyproject_version()
        built = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(dist_dir), str(REPO_ROOT)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        wheel_path = build_portable._find_wheel(dist_dir, version)
        zip_path = build_portable.build(version, wheel_path, dist_dir, build_portable.default_fetch_uv)
        assert zip_path.is_file()

        # ------------------------------------------------------------------
        # dezippage dans un dossier temporaire.
        # ------------------------------------------------------------------
        extracted = work / "extracted"
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extracted)
        portable_root = extracted / f"Clipper-portable-{version}"
        assert portable_root.is_dir()

        app_dir = work / "app"
        data_dir = work / "data"
        installer_bat = portable_root / "Installer.bat"
        desinstaller_bat = portable_root / "Desinstaller.bat"

        # ------------------------------------------------------------------
        # Installer.bat --app <tmp>\app --data <tmp>\data --cpu
        # --sans-console --sans-raccourci (jamais le Bureau reel, note (3)).
        # ------------------------------------------------------------------
        install_result = _run_bat(
            installer_bat,
            ["--app", str(app_dir), "--data", str(data_dir), "--cpu", "--sans-console", "--sans-raccourci"],
            cwd=portable_root,
            timeout=INSTALL_TIMEOUT,
        )
        assert install_result.returncode == 0, install_result.stdout + install_result.stderr

        clipper_exe = app_dir / ".venv" / "Scripts" / "clipper.exe"
        ffmpeg_exe = app_dir / "ffmpeg" / "bin" / "ffmpeg.exe"
        ffprobe_exe = app_dir / "ffmpeg" / "bin" / "ffprobe.exe"
        clipper_bat = app_dir / "Clipper.bat"
        install_json_path = app_dir / "install.json"
        config_path = data_dir / "config.toml"
        rubric_path = data_dir / "rubric.toml"

        assert clipper_exe.is_file()
        assert ffmpeg_exe.is_file()
        assert ffprobe_exe.is_file()
        assert clipper_bat.is_file()
        install_info = json.loads(install_json_path.read_text(encoding="utf-8"))
        assert install_info["cuda"] is False
        assert config_path.is_file()
        assert rubric_path.is_file()

        # ------------------------------------------------------------------
        # clipper doctor depuis data, avec le PATH du lanceur (R10 : meme
        # ordre que Clipper.bat.template, ffmpeg\bin puis .venv\Scripts).
        # Un seul paquet OpenCV dans le venv (override-dependencies honore).
        # ------------------------------------------------------------------
        # Base = l'environnement reel (pas le PATH reduit des .bat ci-dessus) :
        # ce controle verifie la composition du PATH du lanceur (R10), pas
        # l'absence de uv/clipper/.venv du depot -- un Clipper.bat reel, lui,
        # tourne dans un process frais qui a bien le PATH utilisateur a jour
        # (claude y compris, poste par son installeur officiel au registre).
        doctor_env = dict(os.environ)
        doctor_env["PATH"] = os.pathsep.join(
            [str(app_dir / "ffmpeg" / "bin"), str(app_dir / ".venv" / "Scripts"), doctor_env.get("PATH", "")]
        )
        # Chemin complet (jamais 'clipper' a nu, meme esprit que C1/C2) :
        # CreateProcess resout un nom non qualifie via le PATH du PROCESS
        # PARENT (celui du test), pas celui du dict env= passe au sous-
        # processus, donc un PATH reduit uniquement dans env= ne suffit pas
        # a le retrouver (FileNotFoundError observe sur un run reel).
        doctor_result = subprocess.run(
            [str(app_dir / ".venv" / "Scripts" / "clipper.exe"), "doctor"],
            cwd=str(data_dir),
            env=doctor_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCTOR_TIMEOUT,
        )
        assert doctor_result.returncode == 0, doctor_result.stdout + doctor_result.stderr

        site_packages = app_dir / ".venv" / "Lib" / "site-packages"
        opencv_dist_infos = sorted(p.name for p in site_packages.glob("opencv*.dist-info"))
        assert len(opencv_dist_infos) == 1, f"paquets OpenCV installes : {opencv_dist_infos}"

        # ------------------------------------------------------------------
        # Mise a jour : relance Installer.bat sur le meme app ; data jamais
        # touche (config.toml identique, meme contenu et meme date, R6).
        # ------------------------------------------------------------------
        config_before = config_path.read_bytes()
        mtime_before = config_path.stat().st_mtime_ns

        update_result = _run_bat(
            installer_bat,
            ["--app", str(app_dir), "--data", str(data_dir), "--cpu", "--sans-console", "--sans-raccourci"],
            cwd=portable_root,
            timeout=INSTALL_TIMEOUT,
        )
        assert update_result.returncode == 0, update_result.stdout + update_result.stderr
        assert "mise a jour" in (update_result.stdout + update_result.stderr).lower()

        assert config_path.read_bytes() == config_before
        assert config_path.stat().st_mtime_ns == mtime_before

        # ------------------------------------------------------------------
        # Desinstaller.bat --donnees : app et data disparaissent. Audit 10/10
        # I1/I2 : c'est la copie app\Desinstaller.bat (etape 9) qui est
        # lancee, avec app pour dossier courant et sans --app, exactement le
        # double-clic documente (INSTALLATION.md) ; avant, le test lancait
        # celui du zip depuis le zip et ne voyait jamais le refus "en cours
        # d'utilisation" ni la mauvaise resolution de app.
        # ------------------------------------------------------------------
        app_desinstaller_bat = app_dir / "Desinstaller.bat"
        assert app_desinstaller_bat.is_file()
        uninstall_result = _run_bat(
            app_desinstaller_bat,
            ["--donnees"],
            cwd=app_dir,
            timeout=UNINSTALL_TIMEOUT,
        )
        assert uninstall_result.returncode == 0, uninstall_result.stdout + uninstall_result.stderr
        assert "en cours d'utilisation" not in uninstall_result.stdout + uninstall_result.stderr
        assert not app_dir.exists()
        assert not data_dir.exists()
        succeeded = True
    finally:
        duration = time.monotonic() - started
        print(f"\n[test_installer_real] duree totale : {duration:.1f} s, dossier de travail : {work}")
        # Un echec garde le dossier pour inspection (jamais nettoye en
        # silence) ; seul un passage complet le supprime.
        if succeeded:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"[test_installer_real] echec : dossier conserve pour inspection : {work}")
