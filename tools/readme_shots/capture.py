"""Captures et animations du README (TASK-dbb2, refaites pour la console de la clôture U14) : crée un espace de
démonstration TEMPORAIRE (demo_data.py), lance ``clipper serve`` dessus, puis, avec Playwright (Chromium headless) :

* prend les 12 captures fixes ``*-light.webp`` (thème clair) ;
* FILME la console (``record_video``) pour les 3 animations ``*.webp`` : curseur et halo de clic visibles, vraies
  couleurs, 15 images par seconde, sortie en WebP animé sous un plafond de poids réglable (``anim_max_mb``).

Tout le reste est supprimé à la fin ; le vrai ``workspace/``, ``output/``, ``state/`` et ``presets/`` ne sont jamais
lus ni écrits (le serveur tourne avec pour dossier courant le dossier temporaire, sans réseau : l'IP publique est
simulée, aucun relevé TikTok n'est lancé).

    python tools/readme_shots/capture.py            # régénère les 12 captures et les 3 animations de docs/assets/readme/
    CLIPPER_SHOTS_CHROMIUM=/chemin/chrome python tools/readme_shots/capture.py   # Chromium déjà installé

Test hors CI (jamais lancé par la suite) : le script lui-même, de bout en bout, sur la machine de développement ;
il refuse tout dossier de démonstration hors du dossier temporaire du système, et ``tests/test_readme_assets.py``
vérifie ses gardes-fous, la conversion vidéo -> WebP animé et le poids des fichiers livrés.

Prérequis : ``uv pip install -e ".[test]"``, ``python -m playwright install chromium``, ffmpeg (avec libwebp) dans le PATH.
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
    "theme": "light",                      # thème de toutes les images et animations
    "image_max_kb": 400,                   # poids maximal d'une image fixe
    "image_qualities": (92, 86, 78, 70),   # qualités WebP essayées dans l'ordre ; au-delà, erreur explicite
    "anim_max_mb": 3.0,                    # poids maximal de CHAQUE animation WebP
    "anim_width": 960,                     # largeur des animations (hauteur proportionnelle)
    "anim_fps": 15,                        # images par seconde des animations : de 12 à 15
    "anim_qualities": (80, 70, 60, 50, 40),  # qualités WebP essayées dans l'ordre ; au-delà, erreur explicite
    "startup_timeout_s": 60,
    "settle_ms": 900,                      # attente après chaque navigation (rendu, polices, SSE)
    "glide_steps": 26,                     # positions intermédiaires du curseur entre deux clics
    "chromium_env": "CLIPPER_SHOTS_CHROMIUM",  # variable : chemin d'un Chromium déjà installé (sinon celui de Playwright)
}

PROGRESS_VIDEO = "demo0003"
JURY_VIDEO = "demo0001"
STILL_NAMES = ("tableau-de-bord", "veille", "veille-selection", "videos", "video-fiche", "radar-jury", "clips",
               "clip-fiche", "publication", "stats-ensemble", "stats-video", "stats-retention")
ANIM_NAMES = ("progression-en-direct", "radar-du-jury", "nouvelle-publication")


class CaptureError(Exception):
    """Dossier de démonstration refusé, serveur introuvable, image ou animation trop lourde."""


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
    settings = {**CONFIG_DEFAULTS, **(overrides or {})}
    if not 12 <= int(settings["anim_fps"]) <= 15:
        raise CaptureError(f"anim_fps vaut {settings['anim_fps']} : les animations tournent de 12 à 15 images par seconde")
    return settings


# ---------------------------------------------------------------- images et animations

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


def video_seconds(video: Path) -> float:
    """Durée réelle de la vidéo enregistrée (le conteneur de Playwright n'en porte pas) : ffmpeg la relit jusqu'au bout."""
    import re

    result = subprocess.run([demo_data._ffmpeg(), "-i", str(video), "-f", "null", "-"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if result.returncode != 0 or not times:
        raise CaptureError(f"durée de {video.name} illisible : {result.stderr.strip()[-200:]}")
    hours, minutes, seconds = times[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def video_to_webp(video: Path, target: Path, settings: dict[str, Any], start_s: float = 0.0) -> int:
    """Convertit la vidéo enregistrée par Playwright en WebP animé (ffmpeg, libwebp_anim) : ``anim_fps`` images par
    seconde, largeur ``anim_width``, couleurs réelles (pas de palette), à partir de ``start_s`` (le début blanc de
    l'enregistrement est coupé). Qualités décroissantes jusqu'à passer sous ``anim_max_mb`` ; rend le poids en octets."""
    limit = int(float(settings["anim_max_mb"]) * 1024 * 1024)
    graph = f"fps={int(settings['anim_fps'])},scale={int(settings['anim_width'])}:-2:flags=lanczos"
    for quality in settings["anim_qualities"]:
        command = [demo_data._ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{max(0.0, start_s):.2f}", "-i", str(video),
                   "-vf", graph, "-an", "-c:v", "libwebp_anim", "-lossless", "0", "-quality", str(int(quality)),
                   "-compression_level", "5", "-loop", "0", str(target)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise CaptureError(f"ffmpeg a échoué pour {target.name} : {result.stderr.strip()[-300:]}")
        if target.stat().st_size <= limit:
            return target.stat().st_size
    target.unlink(missing_ok=True)
    raise CaptureError(f"{target.name} dépasse {settings['anim_max_mb']} Mo même à la qualité {settings['anim_qualities'][-1]} : "
                       "scène plus courte, anim_width plus petit ou anim_max_mb plus grand")


# ---------------------------------------------------------------- serveur de démonstration

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_standin() -> subprocess.Popen:
    """Processus de remplacement du traitement d'une vidéo « en cours » : la console ne dit « en cours » que si la file
    cite un processus vivant (sinon « interrompue »). Il ne fait rien et meurt avec le script."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def register_running(root: Path, video_id: str, standin: subprocess.Popen) -> None:
    """Écrit dans la file de la démonstration (dossier temporaire seulement) l'entrée ``running`` de ``video_id``,
    portée par le processus de remplacement ; à faire AVANT le démarrage du serveur (son worker verrait sinon une
    vidéo orpheline et la déclarerait interrompue)."""
    from clipper import worker

    guard_demo_root(root)
    state = json.loads((root / "workspace" / video_id / "pipeline.json").read_text(encoding="utf-8"))
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    entry = {"id": "demo-" + video_id, "video_id": video_id, "url": state["source_url"], "channel": state["channel"],
             "action": "run", "force_steps": [], "enqueued_at": now, "status": "running", "pid": standin.pid,
             "pid_created_at": worker._process_created_at(standin.pid), "launched_at": now}
    path = root / "state" / "queue.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([entry], ensure_ascii=False, indent=2), encoding="utf-8")


def start_server(root: Path, port: int, settings: dict[str, Any]) -> subprocess.Popen:
    """``clipper serve`` (via serve_demo.py : coffre en mémoire, IP simulée) avec ``root`` pour dossier courant."""
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

# Curseur et halo de clic : l'enregistrement vidéo de Chromium ne dessine pas la souris. Un élément posé sur la page
# suit les événements ``mousemove`` ; chaque ``mousedown`` lance un halo qui s'élargit en s'estompant.
CURSOR_SCRIPT = """
(() => {
  const make = () => {
    if (document.getElementById('demo-cursor')) return;
    const cursor = document.createElement('div');
    cursor.id = 'demo-cursor';
    cursor.style.cssText = 'position:fixed;left:0;top:0;width:24px;height:24px;z-index:2147483647;pointer-events:none;transform:translate(-80px,-80px)';
    cursor.innerHTML = '<svg viewBox="0 0 24 24" width="24" height="24"><path d="M4 2l0 17.5 4.4-4.2 3 6.7 2.9-1.3-3-6.6 6.1-.3z" fill="#16150f" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(cursor);
    addEventListener('mousemove', (e) => { cursor.style.transform = `translate(${e.clientX}px,${e.clientY}px)`; }, true);
    addEventListener('mousedown', (e) => {
      const halo = document.createElement('div');
      halo.style.cssText = `position:fixed;left:${e.clientX}px;top:${e.clientY}px;width:46px;height:46px;margin:-23px 0 0 -23px;border-radius:50%;z-index:2147483646;pointer-events:none;background:rgba(194,95,0,.35);border:2px solid rgba(194,95,0,.8)`;
      document.documentElement.appendChild(halo);
      halo.animate([{transform: 'scale(.35)', opacity: .95}, {transform: 'scale(1.5)', opacity: 0}], {duration: 520, easing: 'ease-out'}).onfinish = () => halo.remove();
    }, true);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', make); else make();
})();
"""


class Shooter:
    def __init__(self, browser: Any, base: str, settings: dict[str, Any], work: Path, out: Path) -> None:
        self.browser, self.base, self.settings, self.work, self.out = browser, base, settings, work, out
        self.written: list[Path] = []
        self.errors: list[str] = []
        self.mouse = (720.0, 450.0)

    def page(self, *, record: str | None = None) -> tuple[Any, Any]:
        """Contexte + page au thème voulu ; ``record`` = nom de l'animation : la vidéo est enregistrée et le curseur dessiné."""
        width, height = self.settings["viewport"]
        options: dict[str, Any] = {"viewport": {"width": width, "height": height}, "locale": "fr-FR",
                                   "timezone_id": "Europe/Paris", "device_scale_factor": 1}
        if record:
            folder = self.work / f"video-{record}"
            folder.mkdir(parents=True, exist_ok=True)
            options.update(record_video_dir=str(folder), record_video_size={"width": width, "height": height})
        context = self.browser.new_context(**options)
        context.add_init_script(f"try {{ localStorage.setItem('clipper-theme', '{self.settings['theme']}'); }} catch (e) {{}}")
        if record:
            context.add_init_script(CURSOR_SCRIPT)
        page = context.new_page()
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.mouse = (width / 2, height / 2)
        return context, page

    def goto(self, page: Any, route: str) -> None:
        page.goto(f"{self.base}/#{route}")
        page.reload()  # un changement de hash seul ne recharge pas l'écran : on repart d'une page neuve
        self.settle(page)

    def settle(self, page: Any, extra_ms: int = 0) -> None:
        page.wait_for_function("[...document.querySelectorAll('.skeleton')].every((el) => el.offsetParent === null)", timeout=20000)
        page.wait_for_timeout(int(self.settings["settle_ms"]) + extra_ms)

    def shot(self, page: Any, name: str) -> None:
        target = self.out / f"{name}-{self.settings['theme']}.webp"
        size = optimize_image(page.screenshot(type="png"), target, self.settings)
        self.written.append(target)
        print(f"  {target.name} ({size // 1024} Ko)")

    # -- souris
    def glide(self, page: Any, target: Any, *, click: bool = True, pause_ms: int = 260) -> None:
        """Déplace le curseur (positions intermédiaires, accélération douce) jusqu'au centre de ``target`` puis clique."""
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        if box is None:
            raise CaptureError("élément introuvable ou invisible pour le curseur")
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        x0, y0 = self.mouse
        steps = int(self.settings["glide_steps"])
        for i in range(1, steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)
            page.mouse.move(x0 + (x - x0) * ease, y0 + (y - y0) * ease)
            page.wait_for_timeout(16)
        self.mouse = (x, y)
        page.wait_for_timeout(pause_ms)
        if click:
            page.mouse.down()
            page.wait_for_timeout(70)
            page.mouse.up()

    def scroll_smooth(self, page: Any, selector: str, margin: int = 84) -> None:
        page.add_style_tag(content=f"{selector} {{ scroll-margin-top: {margin}px; }}")  # sous la barre du haut
        page.evaluate("(sel) => document.querySelector(sel).scrollIntoView({block: 'start', behavior: 'smooth'})", selector)
        page.wait_for_timeout(900)


def _scroll_to(page: Any, selector: str) -> None:
    page.add_style_tag(content=f"{selector} {{ scroll-margin-top: 84px; }}")  # sous la barre du haut
    page.evaluate("(sel) => document.querySelector(sel).scrollIntoView({block: 'start', behavior: 'instant'})", selector)
    page.wait_for_timeout(300)


# ---------------------------------------------------------------- captures fixes

def still_shots(shooter: Shooter) -> None:
    """Un écran par image, dans le thème des réglages : les mêmes données de démonstration que les animations."""
    context, page = shooter.page()
    try:
        account = str(demo_data.CONFIG_DEFAULTS["account"])
        shooter.goto(page, "/dashboard")
        shooter.shot(page, "tableau-de-bord")

        shooter.goto(page, "/veille")
        shooter.shot(page, "veille")
        _scroll_to(page, "#veille-montent")  # « Ce qui monte » puis la sélection du jour
        shooter.settle(page)
        shooter.shot(page, "veille-selection")

        shooter.goto(page, "/videos")
        shooter.shot(page, "videos")

        shooter.goto(page, f"/videos/{PROGRESS_VIDEO}")  # fiche d'une vidéo en cours : frise et trait animé
        shooter.shot(page, "video-fiche")

        shooter.goto(page, f"/videos/{JURY_VIDEO}")
        page.click('[data-step="moments"]')
        page.wait_for_selector(".vjury svg", timeout=20000)
        _scroll_to(page, ".vjury")
        shooter.settle(page)
        shooter.shot(page, "radar-jury")

        shooter.goto(page, "/clips")
        page.click('[data-filter="all"]')
        shooter.settle(page)
        shooter.shot(page, "clips")

        shooter.goto(page, f"/clip/{JURY_VIDEO}/{demo_data.CLIPS[0][0]}")
        shooter.settle(page, 600)
        shooter.shot(page, "clip-fiche")

        shooter.goto(page, "/publish")
        shooter.shot(page, "publication")

        shooter.goto(page, f"/stats/{account}")
        shooter.settle(page, 600)
        shooter.shot(page, "stats-ensemble")
        _scroll_to(page, "[data-learning-state]")
        shooter.settle(page)
        shooter.shot(page, "stats-retention")

        shooter.goto(page, f"/stats/{account}/videos/7300000000000000001")
        shooter.settle(page, 600)
        shooter.shot(page, "stats-video")
    finally:
        context.close()


# ---------------------------------------------------------------- animations (vidéo -> WebP animé)

class Film:
    """Une animation : un contexte qui enregistre la vidéo ; ``start`` marque la fin du chargement, ``finish`` ferme le
    contexte, coupe le début (chargement, page blanche) en comptant depuis la FIN de la vidéo la durée réellement jouée
    depuis ``start``, convertit en WebP animé et rend le poids."""

    def __init__(self, shooter: Shooter, name: str) -> None:
        self.shooter, self.name = shooter, name
        self.context, self.page = shooter.page(record=name)
        self.started: float | None = None  # horloge au moment de start()

    def start(self) -> None:
        self.shooter.mouse = (self.shooter.settings["viewport"][0] / 2, self.shooter.settings["viewport"][1] / 2)
        self.page.mouse.move(*self.shooter.mouse)
        self.page.wait_for_timeout(250)
        self.started = time.monotonic()

    def finish(self) -> None:
        if self.started is None:
            raise CaptureError(f"{self.name} : start() n'a jamais été appelé")
        self.page.wait_for_timeout(900)
        played = time.monotonic() - self.started
        video = self.page.video
        self.context.close()
        source = Path(video.path())
        target = self.shooter.out / f"{self.name}.webp"
        total = video_seconds(source)
        start_s = total - played - 0.2  # la fin de la vidéo est la fermeture du contexte
        size = video_to_webp(source, target, self.shooter.settings, start_s=start_s)
        self.shooter.written.append(target)
        print(f"  {target.name} ({size // 1024} Ko, {played:.1f} s jouées, vidéo de {total:.1f} s)")


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


def film_live_progress(shooter: Shooter, root: Path) -> None:
    """La vidéo en cours avance : la transcription finit, les étapes suivantes se succèdent, le trait de la frise suit."""
    film = Film(shooter, "progression-en-direct")
    try:
        shooter.goto(film.page, f"/videos/{PROGRESS_VIDEO}")
        film.start()
        film.page.wait_for_timeout(1200)
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
                                        (2, 0.4, "mesure du volume"), (3, 0.2, "analyse des plans"),
                                        (3, 0.7, "analyse des plans"), (4, 0.5, "pics d'action"),
                                        (5, 0.3, "notation des moments"), (5, 0.8, "jury : débat en cours"),
                                        (6, 0.5, "images clés")):
            _write_pipeline(root, PROGRESS_VIDEO, advance(done, fraction, message))
            _append_event(root, PROGRESS_VIDEO, demo_data.STEPS[done], message)
            film.page.wait_for_timeout(2000)  # intervalle de scrutation SSE + rendu
        film.finish()
    except BaseException:
        film.context.close()
        raise


def film_jury_radar(shooter: Shooter) -> None:
    """Ouverture du radar : fiche d'une vidéo terminée, étape Moments, choix d'un moment, avant / après débat."""
    film = Film(shooter, "radar-du-jury")
    page = film.page
    try:
        shooter.goto(page, f"/videos/{JURY_VIDEO}")
        film.start()
        page.wait_for_timeout(900)
        shooter.glide(page, page.locator('[data-step="moments"]'))
        page.wait_for_selector(".vjury svg", timeout=20000)
        page.wait_for_timeout(500)
        shooter.scroll_smooth(page, ".vjury")
        page.wait_for_timeout(900)
        picks = page.locator("[data-jr-pick]")
        if picks.count() < 3:
            raise CaptureError("le radar propose moins de 3 moments : démonstration incohérente")
        for index in (1, 2):
            shooter.glide(page, picks.nth(index))
            page.wait_for_timeout(1100)
            toggles = page.locator("[data-jr-round]")
            if toggles.count() > 1:
                shooter.glide(page, toggles.nth(0))
                page.wait_for_timeout(1000)
                shooter.glide(page, toggles.nth(1))
                page.wait_for_timeout(1100)
        film.finish()
    except BaseException:
        film.context.close()
        raise


def film_new_publication(shooter: Shooter) -> None:
    """Formulaire « Nouvelle publication » : choix du clip, programmation à une date, légende. Jamais validé."""
    film = Film(shooter, "nouvelle-publication")
    page = film.page
    try:
        shooter.goto(page, "/publish")
        film.start()
        page.wait_for_timeout(1500)
        shooter.glide(page, page.locator("[data-pub-new]").first)
        page.wait_for_selector("#pub-form-clips [data-pubf-clip]", timeout=20000)
        page.wait_for_timeout(1600)
        shooter.glide(page, page.locator("[data-pubf-clip]").first)
        page.wait_for_timeout(1500)
        shooter.glide(page, page.locator("#pub-form-later"))
        when = (datetime.now() + timedelta(days=3)).replace(hour=18, minute=0, second=0, microsecond=0)
        page.fill("#pub-form-at", when.strftime("%Y-%m-%dT%H:%M"))
        page.wait_for_timeout(1800)
        page.evaluate("(() => { const box = document.querySelector('dialog [data-pubf-body], dialog section, .modal-body');"
                      " if (box) box.scrollTo({top: 99999, behavior: 'smooth'}); })()")
        page.wait_for_timeout(2200)
        film.finish()
    except BaseException:
        film.context.close()
        raise


# ---------------------------------------------------------------- orchestration

def run(overrides: dict[str, object] | None = None) -> list[Path]:
    """Régénère les captures fixes et les animations ; rend les fichiers écrits. Tout ce qui est temporaire est supprimé."""
    from playwright.sync_api import sync_playwright

    settings = _settings(overrides)
    out = (REPO_ROOT / str(settings["output_dir"])).resolve()
    root = Path(tempfile.mkdtemp(prefix="clipper-readme-shots-"))
    guard_demo_root(root)
    proc: subprocess.Popen | None = None
    standin: subprocess.Popen | None = None
    written: list[Path] = []
    try:
        print(f"espace de démonstration : {root}")
        demo_data.build_demo(root)
        standin = start_standin()
        register_running(root, PROGRESS_VIDEO, standin)
        port = _free_port()
        proc = start_server(root, port, settings)
        staging = root / "_sortie"
        staging.mkdir()
        chromium = os.environ.get(str(settings["chromium_env"]))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**({"executable_path": chromium} if chromium else {}))
            try:
                shooter = Shooter(browser, f"http://127.0.0.1:{port}", settings, root, staging)
                print("captures fixes")
                still_shots(shooter)
                print("animations")
                film_jury_radar(shooter)
                film_new_publication(shooter)
                film_live_progress(shooter, root)  # en dernier : elle modifie l'état de la vidéo en cours
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
        if standin is not None:
            standin.kill()
        shutil.rmtree(root, ignore_errors=True)
        print("espace de démonstration supprimé")


if __name__ == "__main__":
    try:
        files = run()
    except (CaptureError, demo_data.DemoError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"{len(files)} fichiers dans {CONFIG_DEFAULTS['output_dir']}")
