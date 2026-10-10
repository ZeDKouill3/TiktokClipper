"""Worker separe du serveur HTTP (ADR-35b7 §1) : boucle sur la file
``state/queue.json`` (SPEC-74e9 §2) et lance chaque video dans un processus
enfant ``python -m clipper <action> <url|id>``, un seul a la fois
(ADR-fb9b). N'importe que clipper.pipeline, clipper.channel et
clipper.config : jamais clipper.web, jamais une etape (ADR-b16b). Il pousse aussi
les publications dues de state/publish/<chaine>.json vers TikTok, une a la fois
(SPEC-9225 R3), par clipper.tiktok (ADR-1a58) : seul module qui parle a TikTok.

Entree de file (SPEC-74e9 §2.1) : {id, video_id, url, channel | null,
action ("run" | "render"), force_steps, enqueued_at, status ("waiting" |
"running"), pid | null}. Une entree ``running`` porte aussi ``pid_created_at``
(heure de creation du processus, voir ``process_alive`` : un pid reattribue a un
autre processus n'est pas « vivant ») et ``launched_at``.

Une seule instance par file (audit 10/10, lot A1) : ``startup`` refuse de
demarrer si le battement ``worker.json`` designe un autre processus vivant ; et
une seule entree ``running`` : un enfant d'un worker precedent encore vivant est
adopte (surveille jusqu'a sa fin), jamais double.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import tomllib

from clipper import accounts as accounts_mod
from clipper import browser, network
from clipper import channel as channel_mod
from clipper import jury_calibration, learning, repartition
from clipper import publish as publish_mod
from clipper import tiktok, youtube
from clipper.config import Config, ConfigError, load_config

log = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    "poll_interval_s": 2,
    "cancel_grace_s": 10,
    "queue_path": "state/queue.json",
    # Battement du worker (voyant « worker actif / arrêté » de l'interface web) :
    # fichier d'état {pid, at} (worker.json, à côté de la file) réécrit au plus
    # toutes les heartbeat_interval_s secondes ; l'interface le juge périmé après
    # trois intervalles sans battement.
    "heartbeat_interval_s": 5,
    # Télécharge à l'avance la vidéo suivante de la file pendant que la vidéo en cours est traitée.
    "prefetch_download": True,
    # Espace libre minimal du disque (en Go) pour télécharger la vidéo suivante à l'avance.
    "prefetch_min_free_gb": 60,
}

WORKER_COMMAND = "python -m clipper worker"
HEARTBEAT_FILE = "worker.json"
_STALE_AFTER_BEATS = 3

_CANCEL_REASON = "annulée par l'utilisateur"
LOG_FILE = "worker.log"
_LOG_TAIL_LINES = 20
_LOG_TAIL_CHARS = 2000


def log_path(video_id: str, config: Config) -> Path:
    """Journal de la sortie (stdout + stderr) du processus enfant d'une vidéo :
    ``workspace/<video_id>/worker.log``, réécrit à chaque lancement."""
    return Path(config.workspace_dir) / video_id / LOG_FILE


def _log_tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(journal illisible : {exc})"
    tail = "\n".join(text.strip().splitlines()[-_LOG_TAIL_LINES:])[-_LOG_TAIL_CHARS:]
    return tail or "(aucune sortie)"


def heartbeat_path(config: Config) -> Path:
    return _queue_path(config).with_name(HEARTBEAT_FILE)


def read_heartbeat(config: Config, now: datetime | None = None) -> dict[str, Any]:
    """État du worker d'après son battement : ``active`` (battement récent, pid
    vivant), ``stale`` (battement périmé : plus de trois intervalles) ou
    ``stopped`` (aucun battement, ou pid mort). Toujours la commande pour le
    lancer. Un fichier illisible lève ``WorkerError`` : jamais un état inventé."""
    section = config.section("worker")
    path = heartbeat_path(config)
    out: dict[str, Any] = {"command": WORKER_COMMAND, "pid": None, "at": None, "age_s": None}
    if not path.is_file():
        return {**out, "state": "stopped", "reason": f"aucun battement ({path}) : le worker n'a jamais tourné ici"}
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        pid, at = int(beat["pid"]), datetime.fromisoformat(beat["at"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WorkerError(f"battement du worker illisible ({path}) : {exc}") from exc
    if at.tzinfo is None:
        raise WorkerError(f"battement du worker illisible ({path}) : horodatage sans fuseau")
    age = (now or datetime.now(timezone.utc)) - at
    age_s = age.total_seconds()
    out.update(pid=pid, at=beat["at"], age_s=age_s)
    if not process_alive(pid, beat.get("pid_created_at")):  # pid reattribue a un autre processus : worker arrete
        return {**out, "state": "stopped", "reason": f"le processus {pid} du worker n'existe plus"}
    if age_s > float(section["heartbeat_interval_s"]) * _STALE_AFTER_BEATS:
        return {**out, "state": "stale", "reason": f"battement périmé : dernier il y a {int(age_s)} s"}
    return {**out, "state": "active", "reason": None}


def _caption_shown(wanted: str, shown: Any) -> bool:
    """Meme regle que ``tiktok.find_post_link`` : la legende relevee (parfois tronquee) et la voulue, l'une
    commence par l'autre."""
    text = tiktok._squash(shown).rstrip("….").rstrip() if isinstance(shown, str) else ""
    return bool(text and wanted and (wanted.startswith(text) or text.startswith(wanted)))


_PARIS = ZoneInfo("Europe/Paris")
_SELECTOR_STEP_MIN = 5  # pas du selecteur de minutes de TikTok (tiktok.schedule_later) : ecart maximal accepte


def _slot_paris(slot_at: Any) -> datetime | None:
    """Creneau d'une entree en heure de Paris naive, a la minute (comme ``posted_at`` du releve) ; ``None`` si
    absent, sans fuseau ou illisible : l'heure n'est alors pas connue."""
    if not isinstance(slot_at, str):
        return None
    try:
        slot = datetime.fromisoformat(slot_at)
    except ValueError:
        return None
    if slot.tzinfo is None:
        return None
    return slot.astimezone(_PARIS).replace(tzinfo=None, second=0, microsecond=0)


def _posted_paris(post: dict[str, Any]) -> datetime | None:
    """Date de publication d'un post du releve, heure de Paris naive a la minute ; ``None`` si inconnue."""
    stamp = post.get("posted_at")
    if not isinstance(stamp, str):
        return None
    try:
        posted = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if posted.tzinfo is not None:
        posted = posted.astimezone(_PARIS).replace(tzinfo=None)
    return posted.replace(second=0, microsecond=0)


class WorkerError(Exception):
    """Operation de file impossible en l'etat : entree inconnue, doublon en
    attente."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_path(config: Config) -> Path:
    return Path(config.section("worker")["queue_path"])


def _read_queue(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _write_queue(path: Path, entries: list[dict[str, Any]]) -> None:
    channel_mod.atomic_write_json(path, entries)


def _locked(path: Path):
    """Verrou inter-processus sur la file : tout cycle lecture-modification-
    ecriture de state/queue.json se fait dedans (le worker et l'API web sont
    deux processus, ADR-35b7)."""
    return channel_mod.file_lock(path)


def enqueue(
    url: str,
    channel: str | None,
    action: str,
    force_steps: list[str] | None = None,
    *,
    config: Config | None = None,
    short_clips: bool | None = None,
) -> dict[str, Any]:
    """Ajoute une entree a la file (SPEC-74e9 §2.1), ecriture atomique.
    ``short_clips`` (TASK-4f5e) : choix de la video pour les clips courts ;
    None = non precise, l'entree n'a alors pas le champ (valeur du style).
    ``url`` est l'URL source pour ``action="run"``, le video_id pour
    ``action="render"`` (deja lance, pas d'URL a resoudre). Refuse un
    doublon deja ``waiting`` pour le meme video_id et la meme action
    (SPEC-74e9 §2.2)."""
    if short_clips is not None and not isinstance(short_clips, bool):
        raise WorkerError(f"short_clips invalide : {short_clips!r} (attendu : true ou false)")
    if action == "run":
        # clipper.download est une etape (ADR-b16b) : le worker n'importe
        # que clipper.pipeline, qui l'importe deja pour l'enchainement des
        # etapes (extract_video_id est un simple parsing d'URL, aucun
        # reseau).
        from clipper import pipeline

        video_id = pipeline.download.extract_video_id(url)
    else:
        video_id = url

    config = config or load_config()
    path = _queue_path(config)
    entry = {
        "id": uuid.uuid4().hex,
        "video_id": video_id,
        "url": url,
        "channel": channel,
        "action": action,
        "force_steps": list(force_steps or []),
        "enqueued_at": _now_iso(),
        "status": "waiting",
        "pid": None,
        **({} if short_clips is None else {"short_clips": short_clips}),
    }
    with _locked(path):
        entries = _read_queue(path)
        for existing in entries:
            if existing["video_id"] == video_id and existing["action"] == action and existing["status"] == "waiting":
                raise WorkerError(f"deja en file d'attente : {video_id} ({action})")
        entries.append(entry)
        _write_queue(path, entries)
    if action == "run":
        _start_thumbnail_fetch(url, config)
    return entry


def _start_thumbnail_fetch(url: str, config: Config) -> threading.Thread | None:
    """URL non YouTube : recupere la miniature (metadonnees yt-dlp, sans telecharger) dans un fil
    d'arriere-plan qui ne bloque pas l'ajout. Un echec est journalise, jamais une miniature inventee."""
    from clipper import pipeline

    download = pipeline.download
    if download.is_youtube_url(url):
        return None

    def run() -> None:
        try:
            download.fetch_thumbnail(url, config.workspace_dir)
        except Exception:
            log.exception("miniature indisponible pour %s", url)

    thread = threading.Thread(target=run, name="thumbnail-fetch", daemon=True)
    thread.start()
    return thread


def move_to_front(video_id: str, *, config: Config | None = None) -> None:
    """Passe l'entree ``waiting`` de ``video_id`` en tete des entrees en
    attente, sans toucher l'entree ``running`` (SPEC-74e9 §2.2)."""
    config = config or load_config()
    path = _queue_path(config)
    with _locked(path):
        entries = _read_queue(path)

        running: list[dict[str, Any]] = []  # toutes conservees (coeur-I1 : jamais une entree perdue)
        target = None
        rest: list[dict[str, Any]] = []
        for entry in entries:
            if entry["status"] == "running":
                running.append(entry)
            elif target is None and entry["video_id"] == video_id and entry["status"] == "waiting":
                target = entry
            else:
                rest.append(entry)

        if target is None:
            raise WorkerError(f"aucune entree en attente pour {video_id!r}")

        reordered = running + [target] + rest
        _write_queue(path, reordered)


def remove(video_id: str, *, config: Config | None = None) -> None:
    """Retire l'entree ``waiting`` de ``video_id``, sans toucher l'entree
    ``running`` (SPEC-74e9 §2.2)."""
    config = config or load_config()
    path = _queue_path(config)
    with _locked(path):
        entries = _read_queue(path)
        remaining = [e for e in entries if not (e["video_id"] == video_id and e["status"] == "waiting")]
        if len(remaining) == len(entries):
            raise WorkerError(f"aucune entree en attente pour {video_id!r}")
        _write_queue(path, remaining)
    prefetching = [e for e in entries if e["video_id"] == video_id and e["status"] == "waiting"
                   and e.get("prefetch") == "running"]
    for entry in prefetching:  # retrait d'une entree en prechargement : son processus s'arrete avec elle
        _terminate_pid(entry.get("prefetch_pid"), float(config.section("worker")["cancel_grace_s"]),
                       created_at=entry.get("prefetch_pid_created_at"))
        _reset_download_step(video_id, config)


_INTERRUPTED_REASON = "interrompue"


def _entry_process_alive(entry: dict[str, Any]) -> bool:
    """Le processus d'une entree ``running`` vit-il encore ? Pid ET heure de creation (coeur-I3 : apres un
    redemarrage du PC, le pid d'une entree appartient vite a un autre processus)."""
    return process_alive(entry.get("pid"), entry.get("pid_created_at"))


def _live_in_queue(video_id: str, config: Config) -> bool:
    """Vrai si la file a une entrée ``running`` pour ``video_id`` dont le processus existe encore."""
    entries = _read_queue(_queue_path(config))  # lecture seule : l'ecriture de la file est atomique
    return any(e["video_id"] == video_id and e["status"] == "running" and _entry_process_alive(e) for e in entries)


def _worker_busy_inline(config: Config) -> bool:
    """Vrai si le worker est vivant et reprend lui-même des vidéos en file (battement ``busy``) : leur étape
    ``running`` n'a alors pas d'entrée dans la file sans être orpheline."""
    path = heartbeat_path(config)
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        return bool(beat.get("busy")) and process_alive(int(beat["pid"]), beat.get("pid_created_at"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WorkerError(f"battement du worker illisible ({path}) : {exc}") from exc


def is_interrupted(state: dict[str, Any], config: Config) -> bool:
    """Une vidéo ``running`` dont aucun processus ne travaille (pas d'entrée ``running`` vivante dans la file, et
    le worker n'est pas en train de la reprendre lui-même) est interrompue : serveur ou PC arrêté en plein
    traitement (TASK-bdd5)."""
    if state.get("status") != "running":
        return False
    return not _live_in_queue(state["video_id"], config) and not _worker_busy_inline(config)


def mark_interrupted(video_id: str, config: Config, *, cancelled: bool = False) -> str | None:
    """Étape orpheline ``running`` -> ``pending`` (les étapes terminées restent ``done``), vidéo ``failed`` avec
    ``reason``, journalisé. Renvoie l'étape interrompue (None si aucune)."""
    from clipper import pipeline

    state = pipeline.load_state(video_id, config=config)
    orphan = _pending_orphan_step(state)
    detail = f"{_INTERRUPTED_REASON} à l'étape {orphan}" if orphan else _INTERRUPTED_REASON
    state.update(status="failed", reason=f"{_CANCEL_REASON} ({detail})" if cancelled else detail, retry_at=None)
    pipeline.save_state(state, config=config)
    log.warning("%s : traitement interrompu (%s), plus aucun processus ne travaille dessus", video_id, detail)
    return orphan


def _pending_orphan_step(state: dict[str, Any]) -> str | None:
    """L'étape restée ``running`` d'une vidéo dont plus aucun processus ne travaille repasse ``pending`` (les
    étapes terminées restent ``done``) : jamais une étape « en cours » sous une vidéo ``failed`` (coeur-M1).
    Renvoie l'étape remise, None si aucune."""
    orphan = next((n for n, st in state["steps"].items() if st.get("status") == "running"), None)
    if orphan is not None:
        state["steps"][orphan].update(status="pending", reason=None, started_at=None, finished_at=None, progress=None)
    return orphan


def resume(video_id: str, *, config: Config | None = None) -> dict[str, Any]:
    """Remet une vidéo interrompue dans la file ; elle repart de sa première étape non terminée (les étapes
    ``done`` ne sont pas refaites, ADR-b16b). Avant la revue : ``run`` sur l'URL source ; à partir de la revue :
    ``render``."""
    from clipper import pipeline

    config = config or load_config()
    state = pipeline.load_state(video_id, config=config)
    if state.get("status") == "running" and not is_interrupted(state, config):
        raise WorkerError(f"{video_id} est en cours de traitement : rien à reprendre")
    first = next((n for n in pipeline.STEPS if state["steps"][n]["status"] != "done"), None)
    if first is None:
        raise WorkerError(f"{video_id} : toutes les étapes sont terminées, rien à reprendre")
    if pipeline.STEPS.index(first) >= pipeline.STEPS.index(pipeline._AFTER_REVIEW):
        action, target = "render", video_id
    else:
        if not state.get("source_url"):
            raise WorkerError(f"{video_id} : pipeline.json sans source_url, impossible de reprendre à l'étape {first}")
        action, target = "run", state["source_url"]
    if state.get("status") == "running":
        mark_interrupted(video_id, config)
    state = pipeline.load_state(video_id, config=config)
    state.pop("dismissed_at", None)
    pipeline.save_state(state, config=config)
    return enqueue(target, state.get("channel"), action, config=config)


def cancel(video_id: str, *, config: Config | None = None) -> None:
    """Annule la video en cours (SPEC-74e9 §2.3) depuis n'importe quel processus (API web), sans construire de
    ``Worker`` : l'enfant est arrete par le pid lu dans la file (``cancel_grace_s`` puis kill), l'entree quitte
    la file et ``pipeline.json`` passe ``failed``. Le vrai worker, en voyant son enfant termine, garde cette
    raison (ecrite apres le lancement). Rien d'autre n'est touche : ni les publications, ni les reprises."""
    config = config or load_config()
    path = _queue_path(config)
    with _locked(path):
        entry = next((e for e in _read_queue(path) if e["video_id"] == video_id and e["status"] == "running"), None)
    if entry is None:
        with _locked(path):
            prefetching = any(e["video_id"] == video_id and e["status"] == "waiting" and e.get("prefetch") == "running"
                              for e in _read_queue(path))
        if prefetching:  # entree en attente dont seul le download tourne : annuler = la retirer, processus arrete
            remove(video_id, config=config)
            return
        _cancel_interrupted(video_id, config)
        return

    # seul le processus dont l'heure de creation correspond est arrete : un pid reattribue (PC redemarre) designe
    # un processus etranger que Clipper ne touche pas (coeur-I3)
    _terminate_pid(entry["pid"], float(config.section("worker")["cancel_grace_s"]),
                   created_at=entry.get("pid_created_at"))
    with _locked(path):
        _write_queue(path, [e for e in _read_queue(path) if e["id"] != entry["id"]])

    from clipper import pipeline

    try:
        state = pipeline.load_state(video_id, config=config)
    except pipeline.PipelineError:
        state = pipeline.new_state(video_id, entry["url"], config.mode, channel=entry.get("channel"))
    state.pop("dismissed_at", None)
    _pending_orphan_step(state)
    state.update(status="failed", reason=_CANCEL_REASON, retry_at=None)
    pipeline.save_state(state, config=config)


def _cancel_interrupted(video_id: str, config: Config) -> None:
    """Annuler une vidéo interrompue (``running`` sans processus) : l'étape orpheline repasse ``pending``, la vidéo
    ``failed`` « annulée », journalisé ; les étapes terminées sont conservées."""
    from clipper import pipeline

    try:
        state = pipeline.load_state(video_id, config=config)
    except pipeline.PipelineError:
        state = None
    if state is None or not is_interrupted(state, config):
        raise WorkerError(f"aucune video en cours pour {video_id!r}")
    mark_interrupted(video_id, config, cancelled=True)


def _kill_descendants(pid: int) -> None:
    """Tue l'arbre de ``pid`` (ffmpeg, yt-dlp, ``claude -p`` lances par l'enfant : audit 10/10, coeur-I6) : un
    simple arret du pid les laisse orphelins et vivants. Windows : ``taskkill /T /F`` ; ailleurs : le groupe du
    processus quand il en est le chef (lance par ``_popen_logged`` en nouvelle session). Un echec est journalise,
    l'arret du pid lui-meme reste fait par l'appelant."""
    if sys.platform == "win32":
        import subprocess

        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        if result.returncode != 0 and _pid_alive(pid):
            log.error("processus %s : taskkill /T a échoué (code %s) : %s", pid, result.returncode,
                      result.stderr.decode("utf-8", "replace").strip())
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
    except OSError as exc:
        log.warning("processus %s : groupe non arrêté : %s", pid, exc)


def terminate_tree(pid: int | None, grace: float, *, created_at: int | None = None) -> None:
    """Arrete ``pid`` et tout son arbre de processus (``serve`` l'utilise pour son worker enfant)."""
    _terminate_pid(pid, grace, created_at=created_at)


def _terminate_pid(pid: int | None, grace: float, *, created_at: int | None = None) -> None:
    """Arrete le processus ``pid`` et son arbre de descendants s'il vit encore : arret, ``grace`` secondes pour
    que le pid disparaisse, puis kill de nouveau. ``created_at`` (heure de creation enregistree au
    lancement) : un processus qui a herite du pid mais pas de cette heure n'est pas le notre, rien n'est envoye
    (coeur-I3)."""
    if not process_alive(pid, created_at):
        return
    _kill_descendants(pid)
    try:
        os.kill(pid, signal.SIGTERM)  # Windows : TerminateProcess
    except OSError as exc:
        if _pid_alive(pid):
            raise WorkerError(f"processus {pid} impossible à arrêter : {exc}") from exc
        return
    deadline = time.monotonic() + grace
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_alive(pid):
        try:
            _kill_descendants(pid)
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError as exc:
            log.error("processus %s : kill impossible après %g s : %s", pid, grace, exc)


def _group_kwargs() -> dict[str, Any]:
    """Hors Windows, l'enfant est chef de son propre groupe : ``_kill_tree`` peut alors tuer tout l'arbre."""
    return {} if sys.platform == "win32" else {"start_new_session": True}


_STILL_ACTIVE = 259


def _pid_alive(pid: int | None) -> bool:
    """Un pid dont le processus a deja quitte (mort avant le redemarrage du
    worker) doit etre detecte meme si le handle du kernel Windows vit
    encore (ex. un Popen non ferme dans le meme processus python) :
    OpenProcess reussit alors, seul GetExitCodeProcess dit si c'est
    STILL_ACTIVE."""
    if pid is None:
        return False
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and exit_code.value == _STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_created_at(pid: int | None) -> int | None:
    """Identite d'un processus au-dela de son pid : son heure de creation, entier opaque propre a la plateforme
    (Windows : FILETIME de ``GetProcessTimes`` ; Linux : ``starttime`` de ``/proc/<pid>/stat``), stable tant
    que le processus vit et differente pour tout processus qui heriterait du meme pid apres un redemarrage.
    None si le processus n'existe pas, ou si la plateforme ne la donne pas (macOS) : l'identite se reduit
    alors a l'existence du pid, comme avant l'audit."""
    if pid is None:
        return None
    if sys.platform == "win32":
        import ctypes.wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            times = [ctypes.wintypes.FILETIME() for _ in range(4)]  # creation, exit, kernel, user
            ok = kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))
            if not ok:
                return None
            return (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    # champs apres le nom « (comm) » : state=3, ..., starttime=22 ; le nom peut contenir des parentheses
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        return int(fields[22 - 3])
    except (IndexError, ValueError):
        return None


def process_alive(pid: int | None, created_at: int | None) -> bool:
    """Le processus ``pid`` enregistre avec ``created_at`` vit-il encore ? Faux si le pid n'existe plus, ou s'il
    appartient maintenant a un autre processus (heure de creation differente). ``created_at`` None (entree
    ecrite avant cet enregistrement) : existence du pid seule."""
    if not _pid_alive(pid):
        return False
    if created_at is None:
        return True
    return _process_created_at(pid) == created_at


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
EXIT_UNKNOWN = "inconnu"  # code de sortie d'un processus adopte que la plateforme ne permet pas de lire


class _AdoptedProcess:
    """Enfant d'un worker precedent encore vivant (coeur-I1) : surveille par pid et heure de creation jusqu'a sa
    fin, jamais double par un second enfant. Meme surface que ``subprocess.Popen`` pour ``tick`` : ``pid`` et
    ``poll()`` (None tant qu'il vit, puis son code de sortie ; Windows : lu sur un handle garde ouvert des
    l'adoption ; ailleurs ``EXIT_UNKNOWN``, et c'est ``pipeline.json`` qui dit si l'enfant a ecrit sa fin)."""

    def __init__(self, pid: int, created_at: int | None) -> None:
        self.pid = pid
        self._created_at = created_at
        self._handle = None
        if sys.platform == "win32":
            handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            self._handle = handle or None

    def poll(self) -> Any:
        if process_alive(self.pid, self._created_at):
            return None
        code: Any = EXIT_UNKNOWN
        if self._handle:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(self._handle, ctypes.byref(exit_code))
            if ok and exit_code.value != _STILL_ACTIVE:
                code = int(exit_code.value)
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
        return code


def _requeue_dead_running(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Les entrees ``running`` dont le processus est mort repassent ``waiting`` en tete de file, dans leur ordre
    (SPEC-74e9 §2.4), sans pid ni heure de lancement ; ``entries`` est modifiee en place. Renvoie, pour chacune,
    ``{video_id, pid}`` tels qu'ils etaient."""
    dead = [e for e in entries if e["status"] == "running" and not _entry_process_alive(e)]
    seen = [{"video_id": e["video_id"], "pid": e["pid"]} for e in dead]  # pour le journal : le pid avant remise
    for entry in dead:
        entries.remove(entry)
    for entry in reversed(dead):
        entry["status"] = "waiting"
        entry["pid"] = None
        for field in _LAUNCH_FIELDS:
            entry.pop(field, None)
        entries.insert(0, entry)
    return seen


def _reset_download_step(video_id: str, config: Config) -> None:
    """Etape download laissee ``running`` par un prechargement tue (retrait, arret du worker) : elle repasse
    ``pending`` (le prochain ``run`` la refait), jamais « en cours » sans processus."""
    from clipper import pipeline

    try:
        state = pipeline.load_state(video_id, config=config)
    except pipeline.PipelineError:
        return
    step = state["steps"]["download"]
    if step.get("status") == "running":
        step.update(status="pending", reason=None, started_at=None, finished_at=None, progress=None)
        pipeline.save_state(state, config=config)


_PREFETCH_FIELDS = ("prefetch", "prefetch_pid", "prefetch_pid_created_at")
_LAUNCH_FIELDS = ("pid_created_at", "launched_at")  # ecrits avec le pid au lancement, retires avec lui


def _preset_arg(entry: dict[str, Any], config: Config | None) -> str:
    """Fichier de style de la chaine de l'entree, dans le dossier ``[watch] presets_dir``."""
    from clipper import watch as watch_mod  # watch importe worker : import tardif

    presets_dir = (config.section("watch") if config is not None else watch_mod.CONFIG_DEFAULTS)["presets_dir"]
    return (Path(str(presets_dir)) / f"{entry['channel']}.toml").as_posix()


def _build_prefetch_command(entry: dict[str, Any], config: Config | None = None) -> list[str]:
    """Commande du prechargement : l'etape download SEULE (``python -m clipper download <url>``)."""
    cmd = [sys.executable, "-m", "clipper"]
    if entry.get("channel"):
        cmd += ["--config", _preset_arg(entry, config)]
    return cmd + ["download", "--", entry["url"]]


def _build_command(entry: dict[str, Any], config: Config | None = None) -> list[str]:
    # --config est une option globale du parseur : avant la sous-commande.
    cmd = [sys.executable, "-m", "clipper"]
    if entry.get("channel"):
        cmd += ["--config", _preset_arg(entry, config)]
    # « -- » : un identifiant YouTube peut commencer par « - » (web-I6), argparse le lirait comme une option.
    # Les options de la sous-commande passent avant lui.
    cmd += [entry["action"]]
    for step in entry.get("force_steps") or []:
        cmd += ["--force-step", step]
    if "short_clips" in entry:  # absent : valeur du style (anciennes entrees)
        cmd.append("--short-clips" if entry["short_clips"] else "--no-short-clips")
    return cmd + ["--", entry["url"] if entry["action"] == "run" else entry["video_id"]]


class Worker:
    """Boucle sur ``state/queue.json``, un enfant a la fois (ADR-fb9b).
    ``spawner`` (defaut ``subprocess.Popen``) est injecte dans les tests."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        spawner: Callable[[list[str]], Any] | None = None,
        watch_lister: Callable[[str], list[dict[str, Any]]] | None = None,
        publisher: Callable[..., dict[str, Any]] | None = None,
        stats_fetcher: Callable[..., dict[str, Any]] | None = None,
        learning_runner: Callable[..., dict[str, Any]] | None = None,
        repartition_runner: Callable[..., Any] | None = None,
        login_checker: Callable[..., dict[str, Any]] | None = None,
        youtube_publisher: Callable[..., dict[str, Any]] | None = None,
        veille_collectors: dict[str, Callable[..., dict[str, Any]]] | None = None,
    ) -> None:
        self._popen = None
        if spawner is None:
            import subprocess

            self._popen = subprocess.Popen
        self.config = config or load_config()
        self.spawner = spawner
        self._log_handle: Any | None = None
        self._launched_at: datetime | None = None
        self.watch_lister = watch_lister
        self._logged_watch_errors: set[str] = set()
        self.veille_collectors = veille_collectors  # None : les collecteurs reels de clipper.veille_sources
        self._logged_veille_errors: set[str] = set()
        self._veille_thread: threading.Thread | None = None  # releve en cours (un seul a la fois)
        self.publisher = publisher or tiktok.publish
        self.youtube_publisher = youtube_publisher or youtube.publish  # compte YouTube (SPEC-5e50 R2)
        self._logged_publish_errors: set[str] = set()
        self._logged_reconcile: set[str] = set()  # rapprochement indecis : journal une seule fois
        self.stats_fetcher = stats_fetcher or tiktok.fetch_stats
        self.learning_runner = learning_runner or learning.run_if_due  # rattachement puis versement apres releve
        self._logged_learning_errors: set[str] = set()
        self.repartition_runner = repartition_runner or repartition.run_if_due  # plan du lendemain (SPEC-78dc R7)
        self._logged_repartition_errors: set[str] = set()
        self.login_checker = login_checker or browser.login_state  # connexion verifiee avant chaque publication
        self._stats_attempts: dict[str, datetime] = {}
        self._content_check_last: dict[str, tuple[str, str]] = {}  # compte -> dernier clip en content_check (streak)
        self._logged_stats_errors: set[str] = set()
        self._path = _queue_path(self.config)
        self._process: Any | None = None
        self._entry: dict[str, Any] | None = None
        self._last_beat: float | None = None
        self._prefetch_process: Any | None = None  # un seul prechargement a la fois
        self._prefetch_entry_id: str | None = None
        self._prefetch_log_handle: Any | None = None
        self._low_disk_logged: set[str] = set()
        self._pid_created_at = _process_created_at(os.getpid())  # identite de ce worker dans le battement

    def startup(self) -> None:
        """Reprises de demarrage du vrai worker (``clipper worker``, appelees par ``loop`` seulement) : refus si un
        autre worker tourne sur cette file (coeur-I2), place prise par un premier battement, puis orphelins de la
        file, publications interrompues, migration des anciens styles. Jamais dans le constructeur : un autre
        processus qui construirait un Worker passerait en echec la publication que le worker pilote."""
        self._refuse_second_instance()
        self._beat(force=True)
        self._recover_orphans()
        self._recover_prefetch()
        self._recover_interrupted_videos()
        self._recover_interrupted_publications()
        self._migrate_legacy_presets()

    def _refuse_second_instance(self) -> None:
        """Un seul worker par file (ADR-fb9b, ADR-35b7 §1) : si ``worker.json`` designe un autre processus encore
        vivant (meme a battement perime : il travaille peut-etre), ce worker refuse de demarrer, avec le pid de
        l'autre (ADR-ad2e : jamais deux boucles en silence). Un battement illisible est journalise et ignore : le
        premier battement de ce worker le remplace."""
        try:
            beat = read_heartbeat(self.config)
        except WorkerError as exc:
            log.error("%s : ignoré, remplacé par le battement de ce worker", exc)
            return
        if beat["state"] == "stopped" or beat["pid"] == os.getpid():
            return
        raise WorkerError(
            f"un worker tourne déjà (pid {beat['pid']}, dernier battement il y a {int(beat['age_s'])} s) : "
            "arrête-le avant d'en lancer un autre (« clipper serve » lance déjà le sien)")

    def _migrate_legacy_presets(self) -> None:
        """Au demarrage : creneaux et compte d'un ancien style repris sur le compte (SPEC-6076 R2), journalise."""
        try:
            watch = self.config.section("watch")
            channel_mod.migrate_legacy_presets(self.config, presets_dir=watch["presets_dir"], base=watch["base_config"])
        except (channel_mod.ChannelError, ConfigError, OSError, ValueError) as exc:
            log.error("migration des anciens styles impossible : %s", exc)

    def _recover_interrupted_videos(self) -> None:
        """Au démarrage, une étape restée ``running`` sans entrée vivante dans la file (serveur ou PC arrêté
        pendant le traitement) est marquée interrompue et journalisée (TASK-bdd5) : jamais laissée « en cours »."""
        from clipper import pipeline

        root = Path(self.config.workspace_dir)
        for path in sorted(root.glob(f"*/{pipeline.STATE_FILE}")) if root.is_dir() else []:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if state.get("status") == "running" and not _live_in_queue(state["video_id"], self.config):
                    mark_interrupted(state["video_id"], self.config)
            except (OSError, ValueError, KeyError, pipeline.PipelineError) as exc:
                log.error("reprise des vidéos interrompues : %s illisible : %s", path, exc)

    def _recover_interrupted_publications(self) -> None:
        """Une publication restee « en cours » d'un worker arrete en plein pilotage devient un echec explicite
        et reessayable (SPEC-1ed3 R5), jamais bloquee en « en cours »."""
        try:
            watch = self.config.section("watch")
            state_dir = self.config.section("publish")["state_dir"]
            for name in [*channel_mod.list_channels(watch["presets_dir"]), publish_mod.NO_CHANNEL]:
                if publish_mod.fail_interrupted(name, state_dir=state_dir):
                    log.warning("%s : publication interrompue par l'arrêt du worker, passée en échec", name)
        except (publish_mod.PublishError, channel_mod.ChannelError, ConfigError, OSError, ValueError) as exc:
            log.error("reprise des publications interrompues impossible : %s", exc)

    def _beat(self, *, busy: bool = False, force: bool = False) -> None:
        """Écrit ``{pid, at, busy}`` dans ``heartbeat_path`` si ``heartbeat_interval_s``
        s'est écoulé depuis le dernier battement (écriture atomique). ``busy`` : le worker reprend lui-même des
        vidéos en file (pas d'entrée ``running`` pour elles) ; ``force`` écrit sans attendre l'intervalle."""
        section = self.config.section("worker")
        now = time.monotonic()
        if not force and self._last_beat is not None and now - self._last_beat < float(section["heartbeat_interval_s"]):
            return
        path = heartbeat_path(self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat(), "busy": busy,
                                   "pid_created_at": self._pid_created_at}), encoding="utf-8")
        try:
            channel_mod.replace_retrying(tmp, path)  # un lecteur (API web) peut tenir worker.json ouvert sous Windows
        except PermissionError as exc:
            # 10/10 : cette erreur tuait la boucle du worker. Un battement manqué n'est qu'un retard : journalisé,
            # le suivant réessaie au prochain tick (_last_beat inchangé).
            tmp.unlink(missing_ok=True)
            log.warning("battement du worker non écrit (%s verrouillé par un lecteur) : %s ; réessai au prochain tick",
                        path, exc)
            return
        self._last_beat = now

    def _recover_orphans(self) -> None:
        """Au demarrage, une entree ``running`` dont le processus est mort (worker precedent tombe, ou pid
        reattribue apres un redemarrage du PC) repasse ``waiting`` en tete (SPEC-74e9 §2.4). Une entree
        ``running`` dont l'enfant vit encore est laissee : ``_launch_head`` l'adopte au premier tick."""
        with _locked(self._path):
            entries = _read_queue(self._path)
            if _requeue_dead_running(entries):
                _write_queue(self._path, entries)

    def _recover_prefetch(self) -> None:
        """Au demarrage, un prechargement ``running`` laisse par un worker arrete : son processus, s'il vit encore,
        est termine (aucun orphelin) puis le marqueur disparait ; l'entree sera de nouveau prechargee ou, a son
        tour de tete, son download refait par le ``run``."""
        grace = float(self.config.section("worker")["cancel_grace_s"])
        stale: list[dict[str, Any]] = []
        with _locked(self._path):
            entries = _read_queue(self._path)
            for entry in entries:
                if entry["status"] == "waiting" and entry.get("prefetch") == "running":
                    stale.append(dict(entry))
                    for field in _PREFETCH_FIELDS:
                        entry.pop(field, None)
            if stale:
                _write_queue(self._path, entries)
        for entry in stale:
            _terminate_pid(entry.get("prefetch_pid"), grace, created_at=entry.get("prefetch_pid_created_at"))
            _reset_download_step(entry["video_id"], self.config)
            log.warning("%s : prechargement du download interrompu par l'arret du worker, abandonne", entry["video_id"])

    def tick(self) -> None:
        """Une iteration : termine l'entree si l'enfant courant a fini,
        sinon lance la tete de file si aucun enfant ne vit, sinon reprend
        les videos ``queued`` dont ``retry_at`` est passe (SPEC-74e9
        §2.3-2.4). La surveillance des chaines echues passe d'abord, enfant
        en cours ou non (SPEC-74e9 §5.1). Chaque itération bat d'abord (voyant
        de l'interface web)."""
        self._beat()
        self._watch_channels()
        if not self._publish_due():
            self._stats_due()
        self._learning_due()
        self._repartition_due()
        self._reconcile_scheduled()
        self._veille_due()

        self._prefetch_collect()
        if self._process is not None:
            if self._process.poll() is None:
                self._prefetch_start_if_due()
                return
            self._finish_current()
            self._veille_select_best()

        if self._head_is_prefetching():
            return  # son download tourne deja : le `run` attend la fin plutot que de le doubler
        if self._launch_head():
            return

        from clipper import pipeline

        busy = bool(pipeline.queued(config=self.config))
        if busy:
            self._beat(busy=True, force=True)  # les reprises tournent dans ce processus : pas « interrompues »
        try:
            pipeline.process_queue(config=self.config)
        except Exception:  # noqa: BLE001 - jamais un worker mort (Mineur 1, revue r-transcription) :
            # la file reste reprise au tick suivant, l'exception est seulement journalisee.
            log.exception("reprise de la file de pipeline interrompue par une erreur inattendue")
        finally:
            if busy:
                self._beat(force=True)

    def _watch_channels(self) -> None:
        """Appelle ``watch.check`` pour chaque chaine ``watch = true`` dont
        ``checked_at + watch_interval_s`` est passe. Un preset ou un etat
        illisible est journalise une fois (ADR-ad2e : jamais ignore en
        silence) et ne tue pas le worker."""
        from clipper import watch

        section = self.config.section("watch")
        now = datetime.now(timezone.utc)
        try:
            names = channel_mod.list_channels(section["presets_dir"])
            for name in names:
                _config, settings = channel_mod.load_channel(
                    name, presets_dir=section["presets_dir"], base=section["base_config"])
                if not settings["watch"]:
                    continue
                if watch.is_due(name, settings["watch_interval_s"], now, config=self.config):
                    watch.check(name, now, lister=self.watch_lister, config=self.config)
        except Exception as exc:  # noqa: BLE001 - jamais un worker mort : l'echec est journalise une fois
            message = str(exc) if isinstance(exc, (channel_mod.ChannelError, ConfigError, watch.WatchError)) \
                else f"{type(exc).__name__} : {exc}"
            if message not in self._logged_watch_errors:
                self._logged_watch_errors.add(message)
                log.error("surveillance des chaines impossible : %s", message)

    def _log_veille_error(self, exc: Exception) -> None:
        message = str(exc)
        if message not in self._logged_veille_errors:
            self._logged_veille_errors.add(message)
            log.error("veille impossible : %s", message)

    def _veille_due(self) -> None:
        """Releve quotidien ou « Rafraichir » de la veille (SPEC-bdd9 R7), dans ce processus seulement.
        Le releve tourne dans un fil daemon, un seul a la fois, et ne bloque jamais la boucle
        (SPEC-85a0 R26 bis, ADR-6e21) : tant qu'il vit, ce tick ne fait rien ; fini, il est rejoint
        ici puis un nouveau peut partir."""
        thread = self._veille_thread
        if thread is not None:
            if thread.is_alive():
                return
            thread.join()
            self._veille_thread = None
        self._veille_thread = threading.Thread(target=self._veille_run, name="veille", daemon=True)
        self._veille_thread.start()

    def _veille_run(self) -> None:
        """Corps du fil : une erreur de reglage ou d'etat est journalisee une fois, toute autre
        avec sa trace (ADR-ad2e : jamais avalee) ; dans les deux cas le worker continue."""
        from clipper import veille

        try:
            veille.run_if_due(datetime.now(timezone.utc), self.config, self.veille_collectors)
        except (veille.VeilleError, ConfigError) as exc:
            self._log_veille_error(exc)
        except Exception:  # noqa: BLE001 - jamais un fil de releve qui meurt en silence
            log.exception("releve de veille interrompu par une erreur inattendue")

    def _veille_select_best(self) -> None:
        """Apres chaque fin de processus enfant : recalcule les meilleurs clips du jour (SPEC-bdd9 R7)."""
        from clipper import veille

        try:
            veille.select_best(datetime.now(timezone.utc), self.config)
        except (veille.VeilleError, ConfigError) as exc:
            self._log_veille_error(exc)

    # ------------------------------------------------------------ publication TikTok

    def _publish_due(self) -> bool:
        """Une publication TikTok due par iteration (SPEC-9225 R3) ; vrai si une tentative a eu lieu.
        Une file, un preset ou un reglage illisible est journalise une fois (ADR-ad2e) et ne tue
        pas le worker. ``OSError``/``ValueError`` (dont ``JSONDecodeError``) couvrent un fichier de
        publication tronque ou un ``slot_at`` mal forme (M2, revue r-fable-publication) ; toute autre
        exception (``accounts.json`` dont une entree n'est pas un objet, compte supprime entre deux
        lectures...) est journalisee avec son type, jamais propagee a ``loop`` (publication-M1)."""
        try:
            return self._publish_next()
        except Exception as exc:  # noqa: BLE001 - jamais un worker mort : l'echec est journalise une fois
            message = str(exc) if isinstance(
                exc, (publish_mod.PublishError, channel_mod.ChannelError, ConfigError, tiktok.TikTokError,
                      youtube.YouTubeError, accounts_mod.AccountsError, OSError, ValueError)
            ) else f"{type(exc).__name__} : {exc}"
            if message not in self._logged_publish_errors:
                self._logged_publish_errors.add(message)
                log.error("publication TikTok impossible : %s", message)
        return False

    def _stats_due(self) -> None:
        """Releve periodique des statistiques (SPEC-47e2 R4) : coupe quand ``stats_interval_h`` vaut 0 (defaut :
        le releve se fait a l'usage, pas en fond) ; sinon un compte par iteration, jamais dans l'iteration
        qui a pilote une publication (un seul pilotage du navigateur a la fois), seulement pour un compte
        « pret a publier » (donc ni deconnecte ni arrete par R4 de SPEC-9225). La liste des posts vient de TikTok :
        un compte sans clip publie par Clipper est releve aussi. Un echec est journalise une fois et n'est pas
        retente avant ``stats_interval_h`` (ADR-ad2e : jamais silencieux, jamais en boucle)."""
        now = datetime.now(timezone.utc)
        try:
            settings = tiktok.get_settings(self.config)
            watch = self.config.section("watch")
            scope = {"state_dir": self.config.section("publish")["state_dir"],
                     "presets_dir": watch["presets_dir"], "base": watch["base_config"]}
            if float(settings["stats_interval_h"]) == 0:
                return
            wait = timedelta(hours=float(settings["stats_interval_h"]))
            for found in accounts_mod.list_accounts(self.config):
                account = found["id"]
                relevable = found["ready_to_publish"] or (
                    found.get("paused_at") and accounts_mod.connection_blocked_reason(found) is None)  # SPEC-f348 R7.5
                if not relevable or found.get("service") == "youtube":
                    continue  # les statistiques YouTube viennent d'une autre tache : jamais releves par la page TikTok
                tried = self._stats_attempts.get(account)
                if tried is not None and now - tried < wait:
                    continue
                if publish_mod.halted_account(account, **scope) is not None:
                    continue
                if not tiktok.stats_due(account, config=self.config, now=now):
                    continue
                self._stats_attempts[account] = now
                self.stats_fetcher(account, config=self.config, on_tick=self._beat)
                log.info("%s : statistiques TikTok relevées", account)
                return
        except Exception as exc:  # noqa: BLE001 - jamais un worker mort : l'echec est journalise une fois
            message = f"{type(exc).__name__} : {exc}" if not isinstance(
                exc, (tiktok.TikTokError, browser.BrowserError, publish_mod.PublishError, channel_mod.ChannelError,
                      accounts_mod.AccountsError, ConfigError)) else str(exc)
            if message not in self._logged_stats_errors:
                self._logged_stats_errors.add(message)
                log.error("relevé des statistiques TikTok impossible : %s", message)

    def _learning_due(self) -> None:
        """Boucle d'apprentissage apres releve (ADR-c260, SPEC-00db R4) : rattache les posts TikTok aux clips, puis
        verse les statistiques et recalibre le jury (``learning.run_if_due``), dans ce processus seulement ; coupe
        par ``[learning] enabled = false``. Une erreur est ecrite dans ``sync.json.last_error`` et journalisee une
        fois, sans arreter le worker (ADR-ad2e)."""
        try:
            if not self.config.section("learning")["enabled"]:
                return
            done = self.learning_runner(datetime.now(timezone.utc), config=self.config)
            for linked in (done or {}).get("linked", []):
                log.info("%s/%s : rattaché au post TikTok %s", linked["video_id"], linked["clip_id"], linked["post_id"])
        except Exception as exc:  # noqa: BLE001 - jamais un worker mort : l'echec est journalise une fois
            message = str(exc) if isinstance(
                exc, (learning.LearningError, jury_calibration.CalibrationError, tiktok.TikTokError,
                      publish_mod.PublishError, channel_mod.ChannelError, ConfigError, OSError, ValueError)
            ) else f"{type(exc).__name__} : {exc}"
            try:
                learning.record_error(self.config, getattr(exc, "where", "run_if_due"), learning.LearningError(message))
            except Exception as write_exc:  # noqa: BLE001 - l'ecriture de l'erreur ne tue jamais le worker
                log.error("erreur d'apprentissage non écrite dans sync.json : %s", write_exc)
            if message not in self._logged_learning_errors:
                self._logged_learning_errors.add(message)
                log.error("apprentissage impossible : %s", message)

    def _repartition_due(self) -> None:
        """Plan du lendemain (SPEC-78dc R7) : ``repartition.run_if_due`` à chaque tour, après l'apprentissage ; coupé
        par ``[repartition] enabled = false``. L'erreur est écrite dans le fichier du jour par la bibliothèque et
        journalisée une seule fois ici, jamais propagée hors du tour (ADR-ad2e)."""
        try:
            if not self.config.section("repartition")["enabled"]:
                return
            self.repartition_runner(datetime.now(timezone.utc), config=self.config)
        except Exception as exc:  # noqa: BLE001 - jamais un worker mort : l'echec est journalise une fois
            message = str(exc) if isinstance(
                exc, (repartition.RepartitionError, publish_mod.PublishError, accounts_mod.AccountsError,
                      tiktok.TikTokError, ConfigError, OSError, ValueError)) else f"{type(exc).__name__} : {exc}"
            if message not in self._logged_repartition_errors:
                self._logged_repartition_errors.add(message)
                log.error("répartition du lendemain impossible : %s", message)

    def _reconcile_scheduled(self) -> None:
        """Rapprochement au releve : une entree ``scheduled_on_tiktok`` sans id de post (meme apres le rattachement
        de ``learning``) que le releve COMPLET du compte, posterieur a sa programmation, ne montre pas, est signalee
        (``missing_on_tiktok``, note visible dans l'ecran Publication) et journalisee une seule fois. Jamais
        reprogrammee ni modifiee autrement (ADR-ad2e : visible, pas devinee)."""
        try:
            state_dir = self.config.section("publish")["state_dir"]
            watch = self.config.section("watch")
            posts: dict[str, dict[str, dict[str, Any]]] = {}
            last_full: dict[str, datetime | None] = {}
            for name in [*channel_mod.list_channels(watch["presets_dir"]), publish_mod.NO_CHANNEL]:
                for entry in publish_mod.list_entries(name, state_dir=state_dir):
                    if entry["status"] == "failed" and entry.get("to_verify"):
                        self._reconcile_to_verify(entry, name, state_dir)
                        continue
                    if (entry["status"] != "published" or entry.get("tiktok_state") != "scheduled_on_tiktok"
                            or entry.get("post_id") or entry.get("missing_on_tiktok")):
                        continue
                    account = publish_mod.entry_account(entry)
                    if not account:
                        continue
                    if account not in posts:
                        history = tiktok.read_history(account, config=self.config)
                        posts[account] = tiktok.merged_posts(history)
                        fulls = [tiktok._naive_utc(s["fetched_at"]) for s in history if s.get("origin") == "full"]
                        last_full[account] = max(fulls) if fulls else None
                    done_at, seen_at = tiktok._naive_utc(entry.get("published_at")), last_full[account]
                    if seen_at is None or done_at is None or seen_at <= done_at:
                        continue  # aucun releve complet posterieur a la programmation : rien a conclure
                    sidecar = publish_mod.read_sidecar(self.config.output_dir, entry["video_id"], entry["clip_id"])
                    wanted = tiktok._squash(" ".join([str(sidecar.get("caption") or ""),
                                                      *map(str, sidecar.get("hashtags") or [])]))
                    where = f"{entry['video_id']}/{entry['clip_id']}"
                    state, _ = self._caption_match(wanted, entry.get("tiktok_publish_at") or entry.get("slot_at"),
                                              posts[account].values(), where)
                    if state != "absent":  # trouvee, ou indecise (rien n'est conclu : pas de drapeau)
                        continue
                    note = ("programmée sur TikTok mais absente du dernier relevé de TikTok Studio : "
                            "à vérifier à la main (peut-être jamais programmée)")
                    if publish_mod.flag_missing_on_tiktok(entry["video_id"], entry["clip_id"], name, note,
                                                          state_dir=state_dir):
                        log.error("%s/%s : %s", entry["video_id"], entry["clip_id"], note)
                        tiktok.emit_event({"level": "error", "account": account, "channel": name,
                                           "video_id": entry["video_id"], "clip_id": entry["clip_id"],
                                           "reason": note, "capture": None}, config=self.config)
        except Exception as exc:  # noqa: BLE001 - jamais un worker mort : l'echec est journalise une fois
            message = f"rapprochement des programmations impossible : {type(exc).__name__} : {exc}"
            if message not in self._logged_publish_errors:
                self._logged_publish_errors.add(message)
                log.error(message)

    def _reconcile_to_verify(self, entry: dict[str, Any], name: str, state_dir: str | Path) -> None:
        """Une entree ``failed`` « a verifier » (programmation sans id de post retrouve) dont la legende (meme regle
        que ``find_post_link``) apparait dans un releve COMPLET du compte posterieur a l'echec repasse en
        ``scheduled_on_tiktok`` avec l'id et l'adresse du releve, journal info une fois. Jamais reprogrammee."""
        account = publish_mod.entry_account(entry)
        failed_at = tiktok._naive_utc(entry.get("failed_at"))
        if not account or failed_at is None:
            return
        history = [s for s in tiktok.read_history(account, config=self.config)
                   if s.get("origin") == "full" and tiktok._naive_utc(s["fetched_at"]) > failed_at]
        if not history:
            return  # aucun releve complet posterieur : rien a conclure
        sidecar = publish_mod.read_sidecar(self.config.output_dir, entry["video_id"], entry["clip_id"])
        wanted = tiktok._squash(" ".join([str(sidecar.get("caption") or ""), *map(str, sidecar.get("hashtags") or [])]))
        where = f"{entry['video_id']}/{entry['clip_id']}"
        effective = entry.get("tiktok_publish_at")  # heure reellement programmee, connue : comparaison exacte
        state, post = self._caption_match(wanted, effective or entry.get("slot_at"),
                                          [p for p in tiktok.merged_posts(history).values() if p.get("post_id")], where,
                                          tolerance_min=0 if effective else _SELECTOR_STEP_MIN)
        if state != "found" or post is None:
            return  # absente, ou indecise : rien n'est conclu (ADR-ad2e), « à vérifier » reste
        note = "programmation retrouvée dans le relevé de TikTok Studio (rapprochement automatique)"
        if publish_mod.resolve_to_verify(
                entry["video_id"], entry["clip_id"], name, post_id=str(post["post_id"]),
                post_url=post.get("post_url"), account=account, note=note, state_dir=state_dir,
                output_dir=self.config.output_dir):
            log.info("%s/%s : programmation retrouvée sur TikTok (post %s) : « à vérifier » levé",
                     entry["video_id"], entry["clip_id"], post["post_id"])

    def _caption_match(self, wanted: str, slot_at: Any, posts: Iterable[dict[str, Any]],
                       where: str, tolerance_min: int = 0) -> tuple[str, dict[str, Any] | None]:
        """Rapprochement d'une entree a un post du releve : la legende (meme regle que ``find_post_link``) et, des
        que l'heure est connue, la date de publication egale au creneau a la minute (heure de Paris). Rend
        ``("absent", None)`` si aucun post ne porte la legende, ``("found", post)`` si un seul post verifie les
        deux (ou si la legende est unique et son heure inconnue : comportement d'origine), ``("undecided", None)``
        sinon : rien n'est conclu, journal une seule fois (jamais deviner, ADR-ad2e)."""
        candidates = [post for post in posts if _caption_shown(wanted, post.get("caption"))]
        if not candidates:
            return "absent", None
        slot = _slot_paris(slot_at)
        if len(candidates) == 1 and (slot is None or _posted_paris(candidates[0]) is None):
            return "found", candidates[0]
        tolerance = timedelta(minutes=tolerance_min)
        matches = [post for post in candidates if slot is not None and _posted_paris(post) is not None
                   and abs(_posted_paris(post) - slot) <= tolerance]
        if len(matches) == 1:
            return "found", matches[0]
        if matches:
            message = f"{where} : plusieurs posts portent la légende à l'heure du créneau : rien conclu, à vérifier à la main"
            if message not in self._logged_reconcile:
                self._logged_reconcile.add(message)
                log.warning(message)
        else:
            message = f"{where} : légende partagée sans post à l'heure du créneau : rien conclu, à vérifier à la main"
            if message not in self._logged_reconcile:
                self._logged_reconcile.add(message)
                log.info(message)
        return "undecided", None

    def _service_settings(self, service: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Reglages [tiktok] ou [youtube] du service d'un compte, lus (et valides) une fois par passage."""
        if service not in cache:
            cache[service] = youtube.get_settings(self.config) if service == "youtube" else tiktok.get_settings(self.config)
        return cache[service]

    def _publish_next(self) -> bool:
        now = datetime.now(timezone.utc)
        services = {a["id"]: a.get("service") or "tiktok" for a in accounts_mod.list_accounts(self.config)}
        cache: dict[str, dict[str, Any]] = {}
        watch = self.config.section("watch")
        paths = {"state_dir": self.config.section("publish")["state_dir"],
                 "presets_dir": watch["presets_dir"], "base": watch["base_config"]}
        due = []
        # la file des videos sans chaine (SPEC-1ed3 R3) est lue comme celle d'une chaine sans creneau ni compte
        for name in [*channel_mod.list_channels(paths["presets_dir"]), publish_mod.NO_CHANNEL]:
            for entry in publish_mod.list_entries(name, state_dir=paths["state_dir"]):
                if entry["status"] != "scheduled" or not entry["slot_at"]:
                    continue
                slot = datetime.fromisoformat(entry["slot_at"])
                account = publish_mod.entry_account(entry)
                service = services.get(account, "tiktok")  # compte inconnu : _publish_one l'explique (R4)
                settings = self._service_settings(service, cache)
                mode = entry.get("publish_mode") or str(settings["publish_mode"])
                window = timedelta(days=float(settings["schedule_max_days"])) if mode == "scheduled" else timedelta(0)
                if slot - window <= now:  # immediat : creneau atteint ; programme : date dans la fenetre TikTok
                    due.append((slot, name, entry, mode, service, settings))
        due.sort(key=lambda d: (d[0], d[1], d[2]["clip_id"]))
        for slot, name, entry, mode, service, settings in due:
            if self._publish_one(slot, name, entry, mode, settings, paths, now, service):
                return True
        return False

    def _publish_one(self, slot: datetime, name: str, entry: dict[str, Any], mode: str,
                     settings: dict[str, Any], paths: dict[str, Any], now: datetime, service: str = "tiktok") -> bool:
        """Vrai si la tentative de publication a eu lieu (reussie ou en echec) : fin de l'iteration. ``service`` et
        ``settings`` : ceux du compte de l'entree (SPEC-5e50 : plafonds et module de publication par service)."""
        video_id, clip_id = entry["video_id"], entry["clip_id"]
        label = publish_mod.SERVICE_LABELS[service]
        account = publish_mod.entry_account(entry)  # jamais un autre compte en repli (SPEC-6076 R2)
        where = {"channel": name, "video_id": video_id, "clip_id": clip_id}
        if not account:
            self._fail(entry, name,
                       "aucun compte de publication choisi : modifie la publication et choisis un compte prêt à publier",
                       halted=False, account=None, state_dir=paths["state_dir"])
            return False
        target = slot if mode == "scheduled" else now
        waiting_for = self._previous_part_missing(entry, name, target, paths["state_dir"])
        if waiting_for is not None:
            self._wait(entry, name, account, waiting_for, paths["state_dir"])
            return False
        if not self._account_ready(entry, name, account, paths["state_dir"]):
            return False
        account_row = next(a for a in accounts_mod.list_accounts(self.config) if a["id"] == account)
        schedule = accounts_mod.schedule_of(account_row)
        scope = {"state_dir": paths["state_dir"], "presets_dir": paths["presets_dir"], "base": paths["base"]}
        halt = publish_mod.halted_account(account, **scope)
        if halt is not None:
            # arret sur en cours (R4) : rien ne part avant « Reessayer » (ou l'annulation) de l'entree
            # en echec qui a arrete le compte ; la raison reste visible plutot qu'un blocage silencieux
            # (I1, revue r-fable-publication).
            self._wait(entry, name, account,
                       f"compte arrêté par l'échec de {halt['video_id']}/{halt['clip_id']} "
                       f"({halt['error']}) : réessaie-la ou annule-la", paths["state_dir"])
            return False

        times = publish_mod.account_publish_times(account, **scope)
        # Plafonds par compte (SPEC-6076 R6) : le fuseau est celui du COMPTE, pas du style (revue r-comptes 9).
        tz = ZoneInfo(str(schedule["timezone"]))
        blocked = tiktok.check_limits(times, slot if mode == "scheduled" else now, settings, tz)
        if blocked is not None and entry.get("manual"):
            # publication pilotee depuis l'ecran Publication : le plafond a deja ete verifie au formulaire ; s'il
            # est depasse depuis, l'entree attend avec la raison, jamais reportee ni deplacee (SPEC-1ed3 R4)
            self._wait(entry, name, account, f"{blocked} : la publication attend, modifie son heure ou annule-la",
                       paths["state_dir"])
            return False
        if blocked is not None:
            if not schedule["slots"]:  # rien a reporter sans creneau sur le compte (SPEC-6076 R2)
                self._wait(entry, name, account, f"{blocked} : le compte n'a aucun créneau pour reporter la publication "
                           "(Comptes > Créneaux), modifie son heure ou annule-la", paths["state_dir"])
                return False
            try:
                moved = publish_mod.postpone(
                    video_id, clip_id, name, blocked, now=now, schedule=schedule,
                    allowed=lambda candidate: tiktok.check_limits(times, candidate, settings, tz), **scope)
            except publish_mod.PublishError as exc:
                # report impossible (ex. plafond sans creneau libre) : cette entree attend avec la raison ; les
                # autres entrees dues de ce passage sont tentees (TASK-748696ea666f), rien n'est avale
                log.warning("%s/%s : report impossible : %s", video_id, clip_id, exc)
                self._wait(entry, name, account, str(exc), paths["state_dir"])
                return False
            log.warning("%s/%s : %s", video_id, clip_id, moved["postponed_reason"])
            tiktok.emit_event({"level": "warn", "account": account, **where, "reason": moved["postponed_reason"],
                               "capture": None}, config=self.config)
            return False

        if service == "tiktok" and not self._connected(entry, name, account, paths["state_dir"]):
            return False  # YouTube : la connexion est « prete a publier » (derniere verification) ; Studio arrete sur Google
        try:
            network.require_expected_country(self.config)
        except network.NetworkUnknown as exc:
            # service de geolocalisation injoignable : condition transitoire, l'entree attend et sera retentee ;
            # le compte reste « pret a publier » et le navigateur n'est pas ouvert (I2, revue nuit)
            self._wait(entry, name, account, str(exc), paths["state_dir"])
            return False
        except network.NetworkError as exc:  # IP dans un autre pays : arret sur (R4)
            self._fail(entry, name, str(exc), halted=True, account=account, state_dir=paths["state_dir"])
            return True
        if entry.get("waiting_reason"):
            publish_mod.set_waiting_reason(video_id, clip_id, name, None, state_dir=paths["state_dir"])
        try:
            # prise en main atomique : l'entree a pu changer (compte, date, reglages, refus) pendant la verification
            # de connexion ; sinon elle n'est pas pilotee et sera relue au prochain passage
            publish_mod.mark_in_progress(video_id, clip_id, name, expected=entry, state_dir=paths["state_dir"])
        except publish_mod.PublishError as exc:
            log.warning("%s/%s : non pilotée : %s", video_id, clip_id, exc)
            return False
        paused = next((a for a in accounts_mod.list_accounts(self.config) if a["id"] == account), {}).get("paused_at")
        if paused:  # pause posee entre la verification et la prise en main : aucun post (TASK-0c97)
            reason = (f"compte {account} mis en pause (manuel) avant le post : recoche « Prêt à publier » dans "
                      "Comptes pour reprendre, ou choisis un autre compte")
            publish_mod.release_in_progress(video_id, clip_id, name, reason, state_dir=paths["state_dir"])
            log.warning("%s/%s : publication en attente : %s", video_id, clip_id, reason)
            return False
        prev_content_check = self._content_check_last.pop(account, None)  # toute autre issue remet la suite à zéro
        try:
            payload = youtube.clip_payload if service == "youtube" else tiktok.clip_payload
            clip = payload(publish_mod.read_sidecar(self.config.output_dir, video_id, clip_id), self.config.output_dir)
            extra = {"options": entry["post_options"]} if entry.get("post_options") else {}
            publisher = self.youtube_publisher if service == "youtube" else self.publisher
            result = publisher(clip, account, mode=mode, schedule_at=slot if mode == "scheduled" else None,
                               config=self.config, on_tick=self._beat, **extra)
        except tiktok.TikTokStop as stop:
            if stop.code == "content_check_refused":
                self._refused(entry, name, stop.reason, account=account, capture=stop.capture,
                              state_dir=paths["state_dir"])
            elif stop.code == "content_check":
                # vérification jamais terminée : le clip échoue seul ; deux clips de suite (même compte) = le compte
                key = (video_id, clip_id)
                self._content_check_last[account] = key
                halted = prev_content_check is not None and prev_content_check != key
                reason = stop.reason
                if halted:
                    reason = (f"{stop.reason} ; deux clips de suite sans vérification de contenu : "
                              "le problème vient sans doute du compte, compte arrêté")
                    self._content_check_last.pop(account, None)
                self._fail(entry, name, reason, halted=halted, account=account, capture=stop.capture,
                           state_dir=paths["state_dir"])
            else:
                self._fail(entry, name, stop.reason, halted=True, account=account, capture=stop.capture,
                           state_dir=paths["state_dir"])
        except youtube.YouTubeStop as stop:
            self._fail(entry, name, stop.reason, halted=True, account=account, capture=stop.capture,
                       state_dir=paths["state_dir"])
        except browser.BrowserUnavailable as exc:  # pays devenu inconnu pendant la prise en main : echec reessayable
            self._fail(entry, name, str(exc), halted=False, account=account, state_dir=paths["state_dir"])
        except browser.BrowserError as exc:
            self._fail(entry, name, str(exc), halted=True, account=account, state_dir=paths["state_dir"])
        except (tiktok.TikTokError, youtube.YouTubeError, publish_mod.PublishError) as exc:
            self._fail(entry, name, str(exc), halted=False, account=account, state_dir=paths["state_dir"])
        except Exception as exc:  # noqa: BLE001 - jamais un worker mort : l'entree echoue, avec la raison
            log.exception("%s/%s : erreur inattendue pendant la publication", video_id, clip_id)
            self._fail(entry, name, f"erreur inattendue : {type(exc).__name__} : {exc}", halted=True,
                       account=account, state_dir=paths["state_dir"])
        else:
            if service == "tiktok" and result["state"] == "scheduled_on_tiktok" and not result["post_id"]:
                # aucune preuve que le post programmé existe : jamais « publiée » (ADR-ad2e), ni reprogrammée seule
                self._unconfirmed(entry, name, account, result["note"], paths["state_dir"], result.get("publish_at"))
                return True
            publish_mod.mark_published(
                video_id, clip_id, name, state_dir=paths["state_dir"], output_dir=self.config.output_dir,
                tiktok_state=result["state"], post_url=result["post_url"], post_id=result["post_id"],
                publish_at=result["publish_at"], post_note=result["note"], account=account, service=service)
            done = f"programmée sur {label}" if result["state"].startswith("scheduled_on_") else "publiée"
            log.info("%s/%s : %s (%s)", video_id, clip_id, done, result["post_url"] or result["note"])
            tiktok.emit_event({"level": "info", "account": account, **where,
                               "reason": f"{done} : {result['post_url'] or result['note']}", "capture": None},
                              config=self.config)
        return True

    @staticmethod
    def _previous_part_missing(entry: dict[str, Any], channel: str, target: datetime,
                               state_dir: str | Path) -> str | None:
        """Une serie part entiere et dans l'ordre : la partie N>1 attend que la partie N-1 soit publiee, ou
        programmee sur le service a une date qui ne passe pas apres ``target`` (date visee de la partie N).
        Rend la raison de l'attente, None si la partie peut partir. ``parts_together`` explicitement False
        (coche « Parties ensemble » decochee, TASK-fc561e4dc7e9) leve cette attente : la partie est
        independante de ses soeurs."""
        series_id, part = entry.get("series_id"), entry.get("part")
        if not series_id or not isinstance(part, int) or part <= 1:
            return None
        if entry.get("parts_together") is False:
            return None
        siblings = [e for e in publish_mod.list_entries(channel, state_dir=state_dir)
                    if e["video_id"] == entry["video_id"] and e.get("series_id") == series_id]
        number = part - 1
        # une partie refusee par TikTok (verification de contenu) sort de la serie : la serie continue sans elle
        while number >= 1:
            found = next((e for e in siblings if e.get("part") == number), None)
            if found is None or found["status"] != publish_mod.REFUSED_BY_PLATFORM:
                break
            log.info("%s/%s : partie %d refusée par TikTok, série poursuivie", entry["video_id"], found["clip_id"], number)
            number -= 1
        if number < 1:
            return None
        previous = next((e for e in siblings if e.get("part") == number), None)
        reason = f"partie {number} non publiée"
        if previous is None:
            return f"{reason} (absente de la file) : la série part entière et dans l'ordre"
        if previous["status"] != "published":
            return f"{reason} (statut {previous['status']}) : la série part entière et dans l'ordre"
        if str(previous.get("tiktok_state") or "").startswith("scheduled_on_"):
            at = previous.get("tiktok_publish_at")
            if not at or datetime.fromisoformat(at) > target:
                return f"{reason} : programmée sur le service le {at or 'date inconnue'}, après cette partie"
        return None

    def _wait(self, entry: dict[str, Any], channel: str, account: str, reason: str, state_dir: str | Path) -> None:
        """Entree non tentee (SPEC-00d1 R4) : elle reste ``scheduled`` avec la raison visible ; journal et
        evenement console une seule fois par raison."""
        if publish_mod.set_waiting_reason(entry["video_id"], entry["clip_id"], channel, reason, state_dir=state_dir):
            log.warning("%s/%s : publication en attente : %s", entry["video_id"], entry["clip_id"], reason)
            tiktok.emit_event({"level": "warn", "account": account, "channel": channel, "video_id": entry["video_id"],
                               "clip_id": entry["clip_id"], "reason": reason, "capture": None}, config=self.config)

    def _account_ready(self, entry: dict[str, Any], channel: str, account: str, state_dir: str | Path) -> bool:
        """Le compte de l'entree est-il « pret a publier » (R3, R4) ? Sinon l'entree n'est pas tentee."""
        known = {a["id"]: a for a in accounts_mod.list_accounts(self.config)}
        found = known.get(account)
        if found is None:
            reason = f"compte {account} introuvable dans l'écran Comptes : choisis un autre compte pour cette publication"
        elif found.get("paused_at"):
            label = found["label"] or account
            reason = (f"compte {label} en pause (manuel) depuis le {accounts_mod.paused_since(found)} : recoche "
                      "« Prêt à publier » dans Comptes pour reprendre, ou choisis un autre compte")
        elif not found["ready_to_publish"]:
            why = f" ({found['ready_note']})" if found.get("ready_note") else ""
            label = found["label"] or account
            reason = (f"compte {label} non prêt à publier{why} : Comptes > {label} > J'ai réglé le problème "
                      "(la case « prêt à publier » est automatique), ou choisis un autre compte")
        else:
            return True
        self._wait(entry, channel, account, reason, state_dir)
        return False

    def _connected(self, entry: dict[str, Any], channel: str, account: str, state_dir: str | Path) -> bool:
        """Connexion TikTok du profil verifiee avant chaque publication (R2) ; une session expiree decoche
        « pret a publier » (R3) et l'entree reste en attente avec la raison."""
        try:
            observed = self.login_checker(account, config=self.config)
            result = accounts_mod.record_login(self.config, account, observed)
        except (browser.BrowserError, accounts_mod.AccountsError) as exc:
            self._wait(entry, channel, account, f"connexion du compte {account} non vérifiable : {exc}", state_dir)
            return False
        if result["login"]["state"] == "connected":
            return True
        reason = f"compte {account} non connecté à TikTok : {accounts_mod.ready_blocked_reason(result)}"
        if result["auto_unchecked"]:
            reason += " (« prêt à publier » décoché)"
        self._wait(entry, channel, account, reason, state_dir)
        return False

    def _refused(self, entry: dict[str, Any], channel: str, reason: str, *, account: str | None,
                 state_dir: str | Path, capture: Path | None = None) -> None:
        """Probleme signale par TikTok a la verification de contenu (TASK-7f582251f6c5) : rien n'est publie, l'entree
        passe en ``refused_by_platform`` (raison + capture), journal et evenement console ; contrairement a ``_fail``,
        le compte n'est PAS arrete (« pret a publier » reste coche) et la publication suivante part normalement."""
        video_id, clip_id = entry["video_id"], entry["clip_id"]
        log.error("%s/%s : refusé par TikTok : %s", video_id, clip_id, reason)
        publish_mod.mark_refused_by_platform(video_id, clip_id, channel, reason, capture=capture, state_dir=state_dir)
        tiktok.emit_event({"level": "error", "account": account, "channel": channel, "video_id": video_id,
                           "clip_id": clip_id, "reason": f"refusé par TikTok : {reason}",
                           "capture": str(capture) if capture else None}, config=self.config)

    def _unconfirmed(self, entry: dict[str, Any], channel: str, account: str, note: str | None,
                     state_dir: str | Path, publish_at: str | None = None) -> None:
        """Programmation TikTok sans id de post retrouve : entree ``failed`` « a verifier » (compte non arrete,
        aucune reprogrammation automatique : le post a pu partir), journal et evenement console."""
        video_id, clip_id = entry["video_id"], entry["clip_id"]
        reason = ("programmation à vérifier : aucun post retrouvé sur TikTok après la programmation"
                  + (f" ({note})" if note else "")
                  + " ; vérifie TikTok Studio avant de réessayer (risque de doublon)")
        log.error("%s/%s : %s", video_id, clip_id, reason)
        publish_mod.mark_failed(video_id, clip_id, channel, reason, halted=False, to_verify=True, state_dir=state_dir,
                                publish_at=publish_at)
        tiktok.emit_event({"level": "error", "account": account, "channel": channel, "video_id": video_id,
                           "clip_id": clip_id, "reason": reason, "capture": None}, config=self.config)

    def _fail(self, entry: dict[str, Any], channel: str, reason: str, *, halted: bool, account: str | None,
              state_dir: str | Path, capture: Path | None = None) -> None:
        """Entree ``failed`` (reessayable), journal et evenement console (SPEC-9225 R4). Un arret R4 decoche
        aussi « pret a publier » du compte, jusqu'a ce que l'utilisateur le recoche (SPEC-00d1 R3)."""
        video_id, clip_id = entry["video_id"], entry["clip_id"]
        log.error("%s/%s : publication en échec : %s", video_id, clip_id, reason)
        publish_mod.mark_failed(video_id, clip_id, channel, reason, capture=capture, halted=halted, state_dir=state_dir)
        if halted and account:
            try:
                accounts_mod.uncheck_ready(self.config, account, f"arrêt de publication : {reason}")
            except accounts_mod.AccountsError as exc:
                log.error("compte %s : « prêt à publier » non décoché : %s", account, exc)
        tiktok.emit_event({"level": "error", "account": account, "channel": channel, "video_id": video_id,
                           "clip_id": clip_id, "reason": reason, "capture": str(capture) if capture else None},
                          config=self.config)

    def _sync_channel_mode(self, name: str) -> None:
        """L'enfant lance par ``--config presets/<chaine>.toml`` lit le mode
        de TETE du preset (``__main__.load_config``), jamais ``[channel].mode``
        directement : les deux sont donc tenus synchronises avant chaque
        lancement, sinon l'enfant tourne dans le mode global au lieu de celui
        de la chaine (Important 1, revue r-transcription). ``[channel].mode``
        explicite est copie en tete ; vide (herite du mode global), toute
        tete laissee par une synchronisation precedente est retiree, sinon la
        chaine resterait figee sur cet ancien mode au lieu de suivre le mode
        global courant (la tete, si elle restait, masquerait tout changement
        de ``config.toml``). Un preset ou un reglage illisible est journalise
        une fois et ne bloque jamais le lancement (ADR-ad2e : visible,
        jamais silencieux ni fatal)."""
        watch = self.config.section("watch")
        presets_dir, base = watch["presets_dir"], watch["base_config"]
        path = Path(presets_dir) / f"{name}.toml"
        try:
            with path.open("rb") as f:
                raw = tomllib.load(f)
            channel_mod.load_channel(name, presets_dir=presets_dir, base=base)  # valide le preset
        except (channel_mod.ChannelError, ConfigError, OSError, tomllib.TOMLDecodeError) as exc:
            log.error("%s : synchronisation du mode de chaîne impossible : %s", name, exc)
            return
        explicit = str((raw.get("channel") or {}).get("mode") or "")
        if explicit:
            if raw.get("mode") == explicit:
                return
            data = {**raw, "mode": explicit}
        elif "mode" in raw:
            data = {k: v for k, v in raw.items() if k != "mode"}
        else:
            return
        try:
            channel_mod.save_channel(name, data, presets_dir=presets_dir, base=base)
        except (channel_mod.ChannelError, ConfigError, OSError) as exc:
            log.error("%s : synchronisation du mode de chaîne impossible : %s", name, exc)

    def _launch_head(self) -> bool:
        """Lance la tete de file ``waiting`` ; le cycle relecture-lancement-
        ecriture est sous verrou, la file ayant pu changer (API web) depuis
        le dernier tick. Faux si rien n'attend. Avant tout lancement, une
        entree ``running`` etrangere (enfant d'un worker precedent, coeur-I1)
        est adoptee si son processus vit encore (rien d'autre n'est lance
        tant qu'il tourne : un seul enfant, ADR-fb9b), ou remise ``waiting``
        en tete s'il est mort (SPEC-74e9 §2.4)."""
        with _locked(self._path):
            entries = _read_queue(self._path)
            running = [e for e in entries if e["status"] == "running"]
            if running:
                alive = [e for e in running if _entry_process_alive(e)]
                if alive:
                    self._adopt(alive[0])
                    for extra in alive[1:]:
                        log.error("%s : deuxième entrée en cours (pid %s) à côté de %s : non surveillée, elle sera "
                                  "reprise à la mort de son processus", extra["video_id"], extra["pid"],
                                  alive[0]["video_id"])
                    return True
                requeued = _requeue_dead_running(entries)
                _write_queue(self._path, entries)
                for dead in requeued:
                    log.warning("%s : processus %s d'un worker précédent terminé sans retirer son entrée : "
                                "remise en tête de file", dead["video_id"], dead["pid"])
                    self._mark_interrupted_if_running(dead["video_id"])
            entry = next((e for e in entries if e["status"] == "waiting"), None)
            if entry is None:
                return False
            if entry.get("channel"):
                self._sync_channel_mode(entry["channel"])
            self._keep_channel(entry)
            # apres _keep_channel : son ecriture de pipeline.json ne doit pas passer pour un etat ecrit par
            # l'enfant (coeur-M2 : un enfant mort tout de suite gardait l'ancienne raison d'echec)
            self._launched_at = datetime.now(timezone.utc)
            process = self._spawn(entry, _build_command(entry, self.config))
            entry["status"] = "running"
            entry["pid"] = process.pid
            entry["pid_created_at"] = _process_created_at(process.pid)
            entry["launched_at"] = self._launched_at.isoformat()
            for field in _PREFETCH_FIELDS:
                entry.pop(field, None)  # le `run` saute un download fait ou refait un download en echec
            _write_queue(self._path, entries)
        self._process = process
        self._entry = entry
        return True

    def _adopt(self, entry: dict[str, Any]) -> None:
        """Surveille l'enfant encore vivant d'un worker precedent comme s'il etait le notre : son entree quitte la
        file a sa mort (``_finish_current``), et rien d'autre n'est lance d'ici la."""
        self._entry = entry
        self._process = _AdoptedProcess(entry["pid"], entry.get("pid_created_at"))
        launched = entry.get("launched_at")
        self._launched_at = datetime.fromisoformat(launched) if launched else None
        log.warning("%s : processus %s d'un worker précédent encore en cours : adopté et surveillé, aucun autre "
                    "enfant n'est lancé avant sa fin", entry["video_id"], entry["pid"])

    def _mark_interrupted_if_running(self, video_id: str) -> None:
        from clipper import pipeline

        try:
            if pipeline.load_state(video_id, config=self.config).get("status") == "running":
                mark_interrupted(video_id, self.config)
        except (pipeline.PipelineError, OSError, ValueError, KeyError) as exc:
            log.error("%s : état illisible après la mort de son processus : %s", video_id, exc)

    def _keep_channel(self, entry: dict[str, Any]) -> None:
        """Ecrit le style de l'entree dans ``pipeline.json`` avant le lancement (TASK-9d24) : l'enfant
        (``clipper run|render``) ne le connait que par ``--config`` et ne l'ecrit pas, et une relance
        (``_channel_of``, ``enqueue_resume``) le relit depuis l'etat. Un style deja attribue est conserve ;
        une entree sans style n'en invente pas."""
        from clipper import pipeline

        channel = entry.get("channel")
        if not channel:
            return
        try:
            state = pipeline.load_state(entry["video_id"], config=self.config)
        except pipeline.PipelineError:
            state = pipeline.new_state(entry["video_id"], entry["url"], self.config.mode, channel=channel)
        else:
            if state.get("channel"):
                return
            state["channel"] = channel
        pipeline.save_state(state, config=self.config)

    def _spawn(self, entry: dict[str, Any], cmd: list[str]) -> Any:
        if self.spawner is not None:
            return self.spawner(cmd)
        return self._popen_logged(entry, cmd)

    def _popen_logged(self, entry: dict[str, Any], cmd: list[str]) -> Any:
        """Lance l'enfant avec stdout et stderr dans son journal (jamais perdus)."""
        import subprocess

        path = log_path(entry["video_id"], self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "wb")
        try:
            process = self._popen(cmd, stdout=handle, stderr=subprocess.STDOUT, **_group_kwargs())
        except BaseException:
            handle.close()
            raise
        self._log_handle = handle
        return process

    def _close_log(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def _record_child_failure(self, entry: dict[str, Any], code: int) -> None:
        """L'enfant a quitté avec un code non nul : la vidéo passe ``failed`` dans
        ``pipeline.json`` (créé au besoin) avec le code et la fin du journal, sauf
        si l'enfant a lui-même écrit son échec (failed/queued) depuis son lancement
        (ADR-ad2e : jamais une disparition silencieuse)."""
        from clipper import pipeline

        video_id = entry["video_id"]
        try:
            state = pipeline.load_state(video_id, config=self.config)
        except pipeline.PipelineError:
            state = pipeline.new_state(video_id, entry["url"], self.config.mode, channel=entry.get("channel"))
        else:
            if code == EXIT_UNKNOWN and state.get("status") != "running":
                # processus adopte dont le code n'est pas lisible : l'etat dit s'il a ecrit sa fin
                log.warning("%s : processus adopté terminé (code inconnu), état écrit par l'enfant conservé : %s",
                            video_id, state.get("status"))
                return
            written = state.get("updated_at")
            since_launch = self._launched_at is None or (written and datetime.fromisoformat(written) >= self._launched_at)
            if state.get("status") in ("failed", "queued") and written and since_launch:
                log.error("%s : processus enfant terminé avec le code %s (état écrit par l'enfant conservé)", video_id, code)
                return
        path = log_path(video_id, self.config)
        reason = f"le processus enfant s'est terminé avec le code {code} (journal : {path}) : {_log_tail(path)}"
        state.pop("dismissed_at", None)
        _pending_orphan_step(state)
        state.update(status="failed", reason=reason, retry_at=None)
        pipeline.save_state(state, config=self.config)
        log.error("%s : %s", video_id, reason)

    def _finish_current(self) -> None:
        entry = self._entry
        video_id = entry["video_id"]
        self._close_log()
        code = self._process.poll()
        try:
            if code:
                self._record_child_failure(entry, code)
        finally:
            with _locked(self._path):
                entries = _read_queue(self._path)
                entries = [e for e in entries if not (e["video_id"] == video_id and e["status"] == "running")]
                _write_queue(self._path, entries)
            self._process = None
            self._entry = None

    # ------------------------------------------------------------ prechargement (TASK-3c1c)

    def _head_is_prefetching(self) -> bool:
        if self._prefetch_process is None:
            return False
        entry = next((e for e in _read_queue(self._path) if e["status"] == "waiting"), None)
        return entry is not None and entry["id"] == self._prefetch_entry_id

    def _download_done(self, video_id: str) -> bool:
        from clipper import pipeline

        try:
            return pipeline.load_state(video_id, config=self.config)["steps"]["download"]["status"] == "done"
        except pipeline.PipelineError:
            return False

    def _free_gb(self) -> float:
        probe = Path(self.config.workspace_dir)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free / 1024 ** 3

    def _prefetch_start_if_due(self) -> None:
        """Le download de la video en cours est fait : lance le download SEUL de la premiere entree ``waiting``
        (au plus un a la fois, jamais retente tant qu'elle n'est pas la video en cours)."""
        section = self.config.section("worker")
        if not section["prefetch_download"] or self._prefetch_process is not None or self._entry is None:
            return
        if not self._download_done(self._entry["video_id"]):
            return
        minimum = float(section["prefetch_min_free_gb"])
        with _locked(self._path):
            entries = _read_queue(self._path)
            entry = next((e for e in entries if e["status"] == "waiting"), None)
            if entry is None or entry["action"] != "run" or "prefetch" in entry:
                return
            free = self._free_gb()
            if free < minimum:
                if entry["id"] not in self._low_disk_logged:
                    self._low_disk_logged.add(entry["id"])
                    log.info("%s : pas de prechargement du download, %.0f Go libres sur le disque du workspace "
                             "(seuil prefetch_min_free_gb = %g)", entry["video_id"], free, minimum)
                return
            if entry.get("channel"):
                self._sync_channel_mode(entry["channel"])
            self._keep_channel(entry)
            process = self._spawn_prefetch(entry, _build_prefetch_command(entry, self.config))
            entry["prefetch"] = "running"
            entry["prefetch_pid"] = process.pid
            entry["prefetch_pid_created_at"] = _process_created_at(process.pid)
            _write_queue(self._path, entries)
        self._prefetch_process = process
        self._prefetch_entry_id = entry["id"]
        log.info("%s : prechargement du download (pid %s) pendant le traitement de %s",
                 entry["video_id"], process.pid, self._entry["video_id"])

    def _spawn_prefetch(self, entry: dict[str, Any], cmd: list[str]) -> Any:
        if self.spawner is not None:
            return self.spawner(cmd)
        import subprocess

        path = log_path(entry["video_id"], self.config).with_name("prefetch.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "wb")
        try:
            process = self._popen(cmd, stdout=handle, stderr=subprocess.STDOUT, **_group_kwargs())
        except BaseException:
            handle.close()
            raise
        self._prefetch_log_handle = handle
        return process

    def _prefetch_collect(self) -> None:
        """Prechargement termine : l'entree (si elle est toujours en file) passe ``done`` ou ``failed`` ; un echec
        est journalise et laisse l'etat de la video prechargee en echec visible, sans toucher la video en cours."""
        process = self._prefetch_process
        if process is None:
            return
        code = process.poll()
        if code is None:
            return
        entry_id = self._prefetch_entry_id
        if self._prefetch_log_handle is not None:
            self._prefetch_log_handle.close()
            self._prefetch_log_handle = None
        self._prefetch_process = None
        self._prefetch_entry_id = None
        with _locked(self._path):
            entries = _read_queue(self._path)
            entry = next((e for e in entries if e["id"] == entry_id), None)
            if entry is None:
                return  # retiree ou annulee pendant le download : rien d'autre a ecrire
            entry["prefetch"] = "done" if code == 0 else "failed"
            entry["prefetch_pid"] = None
            entry.pop("prefetch_pid_created_at", None)
            _write_queue(self._path, entries)
        if code != 0:
            self._record_prefetch_failure(entry, code)

    def _record_prefetch_failure(self, entry: dict[str, Any], code: int) -> None:
        from clipper import pipeline

        video_id = entry["video_id"]
        try:
            state = pipeline.load_state(video_id, config=self.config)
        except pipeline.PipelineError:
            state = pipeline.new_state(video_id, entry["url"], self.config.mode, channel=entry.get("channel"))
        step = state["steps"]["download"]
        if step.get("status") != "failed":  # le processus est mort sans ecrire son echec
            path = log_path(video_id, self.config).with_name("prefetch.log")
            reason = f"le prechargement s'est termine avec le code {code} (journal : {path}) : {_log_tail(path)}"
            step.update(status="failed", reason=reason, finished_at=_now_iso())
            state.update(status="failed", reason=f"download : {reason}", retry_at=None)
            pipeline.save_state(state, config=self.config)
        log.error("%s : prechargement du download en echec (code %s) : %s", video_id, code, step.get("reason"))

    def shutdown(self) -> None:
        """Arret du worker : le processus de prechargement est termine avec lui (aucun orphelin) et son marqueur
        retire de l'entree ; l'etape download laissee ``running`` repasse ``pending``."""
        process = self._prefetch_process
        if process is None:
            return
        entry_id = self._prefetch_entry_id
        _terminate_pid(process.pid, float(self.config.section("worker")["cancel_grace_s"]))
        if self._prefetch_log_handle is not None:
            self._prefetch_log_handle.close()
            self._prefetch_log_handle = None
        self._prefetch_process = None
        self._prefetch_entry_id = None
        with _locked(self._path):
            entries = _read_queue(self._path)
            entry = next((e for e in entries if e["id"] == entry_id), None)
            if entry is not None:
                for field in _PREFETCH_FIELDS:
                    entry.pop(field, None)
                _write_queue(self._path, entries)
        if entry is not None:
            _reset_download_step(entry["video_id"], self.config)

    def loop(self) -> None:
        """Boucle jusqu'a interruption, a l'intervalle ``poll_interval_s``
        de CONFIG_DEFAULTS (SPEC-74e9 §2.3)."""
        interval = float(self.config.section("worker")["poll_interval_s"])
        self.startup()
        try:
            while True:
                self.tick()
                time.sleep(interval)
        finally:
            self.shutdown()
