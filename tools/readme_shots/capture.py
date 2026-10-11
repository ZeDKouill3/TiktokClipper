"""Captures et animations du README (TASK-dbb2) : crée un espace de démonstration TEMPORAIRE (demo_data.py), lance
``clipper serve`` dessus, capture la console avec Playwright (Chromium headless) puis écrit les trois GIF dans
``docs/assets/readme/``. Tout le reste est supprimé à la fin ; le vrai ``workspace/``, ``output/``, ``state/`` et
``presets/`` ne sont jamais lus ni écrits (le serveur tourne avec pour dossier courant le dossier temporaire).

    python tools/readme_shots/capture.py            # régénère les 3 GIF de docs/assets/readme/ (les captures fixes *-light.webp sont faites à la main, jamais écrasées)
    CLIPPER_SHOTS_CHROMIUM=/chemin/chrome python tools/readme_shots/capture.py   # Chromium déjà installé

Prérequis : ``uv pip install -e ".[test]"``, ``python -m playwright install chromium``, ffmpeg dans le PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import demo_data  # noqa: E402

REPO_ROOT = HERE.parent.parent

CONFIG_DEFAULTS: dict[str, object] = {
    "output_dir": "docs/assets/readme",   # relatif à la racine du dépôt
    "viewport": (1440, 900),
    "image_max_kb": 400,                   # poids maximal d'une image
    "image_qualities": (92, 86, 78, 70),   # qualités WebP essayées dans l'ordre ; au-delà, erreur explicite
    "gif_max_mb": 2.0,                     # poids maximal d'un GIF
    "gif_width": 960,
    "gif_fps": 2,
    "gif_colors": (96, 64, 48),            # palettes essayées dans l'ordre
    "startup_timeout_s": 60,
    "settle_ms": 900,                      # attente après chaque navigation (rendu, polices, SSE)
    "chromium_env": "CLIPPER_SHOTS_CHROMIUM",  # variable : chemin d'un Chromium déjà installé (sinon celui de Playwright)
}

PROGRESS_VIDEO = "demo0003"
JURY_VIDEO = "demo0001"


class CaptureError(Exception):
    """Dossier de démonstration refusé, serveur introuvable, image ou GIF trop lourd."""


# ---------------------------------------------------------------- garde-fous

def guard_demo_root(root: Path) -> Path:
    """Le dossier de démonstration doit être sous le dossier temporaire du système et hors du dépôt : le script ne
    sert jamais un workspace/state réel."""
    resolved = Path(root).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if temp not in resolved.parents:
        raise CaptureError(f"dossier de démonstration refusé (hors du dossier temporaire {temp}) : {root}")
    if REPO_ROOT.resolve() in (resolved, *resolved.parents):
        raise CaptureError(f"dossier de démonstration refusé (dans le dépôt) : {root}")
    return resolved


def _settings(overrides: dict[str, object] | None) -> dict[str, Any]:
    unknown = set(overrides or {}) - set(CONFIG_DEFAULTS)
    if unknown:
        raise CaptureError(f"réglages inconnus : {sorted(unknown)}")
    return {**CONFIG_DEFAULTS, **(overrides or {})}


# ---------------------------------------------------------------- images et GIF

def optimize_image(png: bytes, target: Path, settings: dict[str, Any]) -> int:
    """Écrit ``png`` en WebP sous ``image_max_kb`` (qualités décroissantes) ; rend le poids en octets. Erreur
    explicite si même la plus basse qualité dépasse le plafond."""
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    limit = int(settings["image_max_kb"]) * 1024
    for quality in settings["image_qualities"]:
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=int(quality), method=6)
        if buffer.tell() <= limit:
            target.write_bytes(buffer.getvalue())
            return buffer.tell()
    raise CaptureError(f"{target.name} dépasse {settings['image_max_kb']} Ko même à la qualité "
                       f"{settings['image_qualities'][-1]} : réduis la vue ou le viewport")


def frames_to_gif(frames: Path, target: Path, settings: dict[str, Any]) -> int:
    """Assemble ``frames/f%03d.png`` en GIF (ffmpeg, palette adaptée) sous ``gif_max_mb`` ; rend le poids en octets."""
    limit = int(float(settings["gif_max_mb"]) * 1024 * 1024)
    for colors in settings["gif_colors"]:
        graph = (f"fps={settings['gif_fps']},scale={settings['gif_width']}:-1:flags=lanczos,split[a][b];"
                 f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4")
        command = [demo_data._ffmpeg(), "-y", "-loglevel", "error", "-framerate", str(settings["gif_fps"]),
                   "-i", str(frames / "f%03d.png"), "-filter_complex", graph, "-loop", "0", str(target)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise CaptureError(f"ffmpeg a échoué pour {target.name} : {result.stderr.strip()[-300:]}")
        if target.stat().st_size <= limit:
            return target.stat().st_size
    raise CaptureError(f"{target.name} dépasse {settings['gif_max_mb']} Mo même à {settings['gif_colors'][-1]} couleurs : "
                       "moins d'images ou une vue plus petite")


# ---------------------------------------------------------------- serveur de démonstration

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(root: Path, port: int, settings: dict[str, Any]) -> subprocess.Popen:
    """``clipper serve`` (via serve_demo.py : coffre en mémoire) avec ``root`` pour dossier courant."""
    guard_demo_root(root)
    log = open(root / "serve.log", "w", encoding="utf-8")
    options: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "serve_demo.py"), str(demo_data.CONFIG_DEFAULTS["account"]),
         demo_data.PASSWORD_PLACEHOLDER, str(port)],
        cwd=str(root), stdout=log, stderr=subprocess.STDOUT, encoding="utf-8", **options)
    deadline = time.monotonic() + float(settings["startup_timeout_s"])
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise CaptureError(f"clipper serve s'est arrêté au démarrage (voir {root / 'serve.log'})")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard", timeout=2) as response:
                if response.status == 200:
                    return proc
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    stop_server(proc)
    raise CaptureError(f"clipper serve ne répond pas après {settings['startup_timeout_s']} s")


def stop_server(proc: subprocess.Popen) -> None:
    """Arrête le serveur et le worker qu'il a lancé (tout l'arbre de processus)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------- pilotage de la page

class Shooter:
    def __init__(self, browser: Any, base: str, settings: dict[str, Any], work: Path, out: Path) -> None:
        self.browser, self.base, self.settings, self.work, self.out = browser, base, settings, work, out
        self.written: list[Path] = []
        self.errors: list[str] = []

    def page(self, theme: str) -> tuple[Any, Any]:
        width, height = self.settings["viewport"]
        context = self.browser.new_context(viewport={"width": width, "height": height}, locale="fr-FR",
                                           timezone_id="Europe/Paris", device_scale_factor=1)
        context.add_init_script(f"try {{ localStorage.setItem('clipper-theme', '{theme}'); }} catch (e) {{}}")
        page = context.new_page()
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        return context, page

    def goto(self, page: Any, route: str) -> None:
        page.goto(f"{self.base}/#{route}")
        page.reload()  # un changement de hash seul ne recharge pas l'écran : on repart d'une page neuve
        self.settle(page)

    def settle(self, page: Any, extra_ms: int = 0) -> None:
        page.wait_for_function("[...document.querySelectorAll('.skeleton')].every((el) => el.offsetParent === null)", timeout=20000)
        page.wait_for_timeout(int(self.settings["settle_ms"]) + extra_ms)


def _scroll_to(page: Any, selector: str) -> None:
    page.add_style_tag(content=f"{selector} {{ scroll-margin-top: 84px; }}")  # sous la barre du haut
    page.evaluate("(sel) => document.querySelector(sel).scrollIntoView({block: 'start'})", selector)
    page.wait_for_timeout(300)


# ---------------------------------------------------------------- GIF

class GifRecorder:
    def __init__(self, shooter: Shooter, name: str) -> None:
        self.shooter, self.name = shooter, name
        self.frames = shooter.work / f"frames-{name}"
        self.frames.mkdir(parents=True)
        self.count = 0

    def frame(self, page: Any, hold: int = 1) -> None:
        png = page.screenshot(type="png")
        for _ in range(hold):
            self.count += 1
            (self.frames / f"f{self.count:03d}.png").write_bytes(png)

    def finish(self) -> None:
        target = self.shooter.out / f"{self.name}.gif"
        size = frames_to_gif(self.frames, target, self.shooter.settings)
        self.shooter.written.append(target)
        print(f"  {target.name} ({size // 1024} Ko, {self.count} images)")


def _write_pipeline(root: Path, video_id: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Réécrit pipeline.json de la démo (dossier temporaire seulement) : la console le voit par SSE."""
    path = guard_demo_root(root) / "workspace" / video_id / "pipeline.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    mutate(state)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_event(root: Path, video_id: str, step: str, message: str) -> None:
    path = guard_demo_root(root) / "workspace" / video_id / "events.jsonl"
    event = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "level": "info", "step": step, "message": message}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def gif_live_progress(shooter: Shooter, root: Path) -> None:
    """La vidéo en cours avance : la transcription finit, les étapes suivantes se succèdent, le trait de la frise suit."""
    context, page = shooter.page("dark")
    try:
        shooter.goto(page, f"/videos/{PROGRESS_VIDEO}")
        rec = GifRecorder(shooter, "progression-en-direct")
        rec.frame(page, 2)
        now = datetime.now().astimezone()

        def advance(done: int, fraction: float | None, message: str | None) -> Callable[[dict[str, Any]], None]:
            def mutate(state: dict[str, Any]) -> None:
                stamp = lambda seconds: (now + timedelta(seconds=seconds)).isoformat(timespec="seconds")  # noqa: E731
                for index, name in enumerate(demo_data.STEPS):
                    step = state["steps"][name]
                    if index < done:
                        if step["status"] != "done":
                            step.update(status="done", started_at=step["started_at"] or stamp(-60), finished_at=stamp(index),
                                        progress=None)
                    elif index == done:
                        step.update(status="running", started_at=step["started_at"] or stamp(0), finished_at=None,
                                    progress=None if fraction is None else {"fraction": fraction, "eta_s": 300 * (1 - fraction),
                                                                            "message": message})
                state["updated_at"] = stamp(0)
            return mutate

        for done, fraction, message in ((1, 0.62, "62 % de l'audio transcrit"), (1, 0.85, "85 % de l'audio transcrit"),
                                        (2, 0.2, "analyse des plans"), (2, 0.7, "analyse des plans"),
                                        (3, 0.4, "mesure du volume"), (4, 0.3, "notation des moments"),
                                        (4, 0.8, "jury : débat en cours"), (5, 0.5, "images clés")):
            _write_pipeline(root, PROGRESS_VIDEO, advance(done, fraction, message))
            _append_event(root, PROGRESS_VIDEO, demo_data.STEPS[done], message)
            page.wait_for_timeout(2200)  # intervalle de scrutation SSE + rendu
            rec.frame(page)
        rec.frame(page, 1)
        rec.finish()
    finally:
        context.close()


def gif_jury_radar(shooter: Shooter) -> None:
    """Ouverture du radar : fiche d'une vidéo terminée, étape Moments, choix d'un moment, avant / après débat."""
    context, page = shooter.page("dark")
    try:
        shooter.goto(page, f"/videos/{JURY_VIDEO}")
        rec = GifRecorder(shooter, "radar-du-jury")
        rec.frame(page, 2)
        page.click('[data-step="moments"]')
        page.wait_for_selector(".vjury svg", timeout=20000)
        _scroll_to(page, ".vjury")
        page.wait_for_timeout(900)
        rec.frame(page, 2)
        picks = page.locator("[data-jr-pick]")
        if picks.count() < 3:
            raise CaptureError("le radar propose moins de 3 moments : démonstration incohérente")
        for index in (1, 2):
            picks.nth(index).click()
            page.wait_for_timeout(700)
            rec.frame(page, 2)
            toggles = page.locator("[data-jr-round]")
            if toggles.count() > 1:
                toggles.nth(0).click()
                page.wait_for_timeout(600)
                rec.frame(page)
                toggles.nth(1).click()
                page.wait_for_timeout(600)
                rec.frame(page, 2)
        rec.finish()
    finally:
        context.close()


def gif_new_publication(shooter: Shooter) -> None:
    """Formulaire « Nouvelle publication » : choix du clip, programmation à une date, légende. Jamais validé."""
    context, page = shooter.page("dark")
    try:
        shooter.goto(page, "/publish")
        rec = GifRecorder(shooter, "nouvelle-publication")
        rec.frame(page, 2)
        page.click("[data-pub-new]")
        page.wait_for_selector("#pub-form-clips [data-pubf-clip]", timeout=20000)
        page.wait_for_timeout(600)
        rec.frame(page, 2)
        page.locator("[data-pubf-clip]").first.click()
        page.wait_for_timeout(500)
        rec.frame(page, 2)
        page.check("#pub-form-later")
        when = (datetime.now() + timedelta(days=3)).replace(hour=18, minute=0, second=0, microsecond=0)
        page.fill("#pub-form-at", when.strftime("%Y-%m-%dT%H:%M"))
        page.wait_for_timeout(500)
        rec.frame(page, 2)
        page.evaluate("document.querySelector('.modal-body').scrollTo({top: 99999})")
        page.wait_for_timeout(500)
        rec.frame(page, 3)
        rec.finish()
    finally:
        context.close()


# ---------------------------------------------------------------- orchestration

def run(overrides: dict[str, object] | None = None) -> list[Path]:
    """Régénère les trois GIF (les captures fixes light sont faites à la main) ; rend les fichiers écrits. Tout ce qui est temporaire est supprimé."""
    from playwright.sync_api import sync_playwright

    settings = _settings(overrides)
    out = (REPO_ROOT / str(settings["output_dir"])).resolve()
    root = Path(tempfile.mkdtemp(prefix="clipper-readme-shots-"))
    guard_demo_root(root)
    proc: subprocess.Popen | None = None
    written: list[Path] = []
    try:
        print(f"espace de démonstration : {root}")
        demo_data.build_demo(root)
        port = _free_port()
        proc = start_server(root, port, settings)
        staging = root / "_sortie"
        staging.mkdir()
        chromium = os.environ.get(str(settings["chromium_env"]))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**({"executable_path": chromium} if chromium else {}))
            try:
                shooter = Shooter(browser, f"http://127.0.0.1:{port}", settings, root, staging)
                print("animations")
                gif_jury_radar(shooter)
                gif_new_publication(shooter)
                gif_live_progress(shooter, root)  # en dernier : elle modifie l'état de la vidéo en cours
                written = shooter.written
                if shooter.errors:
                    raise CaptureError("erreurs JavaScript dans la console : " + " | ".join(sorted(set(shooter.errors))))
            finally:
                browser.close()
        out.mkdir(parents=True, exist_ok=True)
        final = []
        for source in written:
            target = out / source.name
            shutil.copyfile(source, target)
            final.append(target)
        return final
    finally:
        if proc is not None:
            stop_server(proc)
        shutil.rmtree(root, ignore_errors=True)
        print("espace de démonstration supprimé")


if __name__ == "__main__":
    try:
        files = run()
    except (CaptureError, demo_data.DemoError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"{len(files)} fichiers dans {CONFIG_DEFAULTS['output_dir']}")
