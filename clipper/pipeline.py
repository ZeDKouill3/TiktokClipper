"""Orchestrateur du pipeline clipper : seul module qui importe les etapes
(ADR-b16b). La CLI (``python -m clipper``) et l'interface web passent par lui.

Enchainement, par video (``STEPS``, dans l'ordre d'execution) :

    download, transcribe, audio, scenes, action, moments, vision, parts,
    captions, reframe, subtitles, render, qa

- ``audio`` tourne avant ``scenes`` (SPEC-b0f3 R4) : ``scenes`` ne decode les
  fenetres de pics hors parole que si ``[action] enabled`` (``peak_windows``,
  positionne ici seul, ADR-4e57) ; ``action`` (desactivee par defaut : fichier
  vide) detecte les passages d'action avant ``moments`` ;

- chaque etape saute d'elle-meme ce qui est deja fait (resultat present sous
  workspace/<video_id>/ ou output/<video_id>/), sauf ``force`` ;
- ``moments`` recoit ``examples=feedback.examples(k)`` ; apres ``vision``,
  moments est relance sans force : si vision.json est plus recent que
  moments.json, ses candidats sont re-notes (bonus des images marquantes)
  sans nouvel appel LLM ;
- un seul modele lourd en VRAM a la fois (ADR-fb9b) : les etapes tournent en
  sequence et chacune libere son modele (whisper dans transcribe, detecteur de
  visages dans reframe) avant de rendre la main ;
- ``reframe`` passe avant ``subtitles`` : les bandes a ne pas recouvrir
  (``avoid_zones``) se deduisent plan par plan des visages du plan de
  recadrage, et la bande de l'accroche (``hook_zones``) des reglages de
  render ; en format letterbox ou stream (``layout = "letterbox"`` ou
  ``"stream"`` a la racine du plan), subtitles recoit a la place la zone
  ``text_zones.subtitles`` du plan ;
- avec ``[reframe] layout = "stream_auto"`` (SPEC-3a88), la facecam est
  detectee une fois par video (``reframe.detect_facecam``) avant les clips ;
- ``subtitles`` genere jusqu'a ``parallel`` clips a la fois ([subtitles] de
  config.toml ; 1 = un clip apres l'autre), sans modele en VRAM ; reframe
  et render traitent leurs clips un par un ;
- un clip n'est pret que si ``qa.is_ready`` le dit.

Modes (``mode`` de config.toml, ADR-ad2e) :

- ``review`` : ``run`` s'arrete apres moments et parts (statut
  ``awaiting_review``) ; chaque moment de parts.json attend une decision
  humaine, donnee par ``decide`` (journalisee par clipper.feedback, et gardee
  dans workspace/<video_id>/review.json) ; ``render`` reprend : moments
  refuses retires de parts.json, bornes ajustees reportees dans moments.json
  (parts refait), puis captions .. qa. Sans decision pour chaque moment,
  ``render`` refuse.
- ``auto`` : ``run`` va jusqu'au bout. Une erreur transitoire (quota, reseau,
  surcharge : ``llm.TransientLLMError``, erreurs reseau) met la video en file
  d'attente (statut ``queued``, ``retry_at`` d'apres ``retry_delays``) ;
  le worker la remet en file a l'heure dite pour un enfant ``run|render
  --resume`` (la CLI ``clipper queue`` / ``process_queue`` la reprend dans
  le processus courant). Au-dela de ``max_attempts``,
  ou pour toute autre erreur : ``failed``. Aucune valeur de secours.

Etat par video : workspace/<video_id>/pipeline.json, reecrit a chaque
transition (lisible a tout moment, par exemple par l'interface web) :

    {
      "video_id": "abcdefghijk",
      "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
      "mode": "review" | "auto",
      "status": "pending" | "running" | "awaiting_review" | "queued"
                | "done" | "failed",
      "reason": null | "pourquoi failed / queued",
      "attempts": 0,              # echecs transitoires consecutifs
      "retry_at": null | "ISO 8601 UTC",   # queued seulement
      "awaiting": [0, 3],         # moments sans decision (awaiting_review)
      "updated_at": "ISO 8601 UTC",
      "steps": {                  # une entree par nom de STEPS, dans l'ordre
        "download": {"status": "pending" | "running" | "done" | "failed",
                     "reason": null | "Type: message de l'erreur",
                     "started_at": null | "ISO", "finished_at": null | "ISO"},
        ...
      },
      "clips": [                  # rempli quand status = done
        {"clip_id": "03-p2", "ready": true, "qa_status": "passed",
         "issues": [...], "mp4": "output/.../03-p2.mp4",
         "json": "output/.../03-p2.json"}
      ]
    }
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from clipper import (
    action,
    audio,
    captions,
    channel as channel_mod,
    download,
    feedback,
    llm,
    moments,
    parts,
    qa,
    reframe,
    render as render_step,
    scenes,
    subtitles,
    transcribe,
    vision,
)
from clipper.config import Config, ConfigError, load_config

log = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    # Echecs transitoires consecutifs avant de passer la video en failed.
    "max_attempts": 5,
    # Delais (s) avant re-essai, par echec ; le dernier sert au-dela.
    "retry_delays": [300, 900, 1800, 3600],
    # Decisions passees (clipper.feedback) donnees en exemples a moments.
    "feedback_examples": 10,
}

STEPS = (
    "download", "transcribe", "audio", "scenes", "action", "moments", "vision", "parts",
    "captions", "reframe", "subtitles", "render", "qa",
)
STEP_STATUSES = ("pending", "running", "done", "failed")
# Premiere etape qui suit la revue humaine en mode review.
_AFTER_REVIEW = "captions"

EXIT_QUEUED = 75  # EX_TEMPFAIL

STATE_FILE = "pipeline.json"
# Fichier de la vignette de la video source (thumbnails/), a cote de celles des clips.
SOURCE_THUMBNAIL = "_source.jpg"
# Statuts qu'on peut « retirer » des echecs du tableau de bord.
DISMISSIBLE_STATUSES = ("failed", "queued")
REVIEW_FILE = "review.json"
EVENTS_FILE = "events.jsonl"

# Etapes qui savent mesurer leur avancement clip par clip (reframe, render) ou
# par unite traitee, et recoivent donc un rappel progress(fraction, eta_s,
# message) dans step_options (SPEC-74e9 §3.1) : voir _ProgressWriter.
_PROGRESS_STEPS = ("transcribe", "subtitles", "reframe", "render")
# Ecriture de pipeline.json au plus une fois par ce delai par appel de progress.
_PROGRESS_MIN_INTERVAL_S = 2.0


class PipelineError(Exception):
    """Demande impossible en l'etat : video inconnue, decisions manquantes,
    decision invalide."""


# --------------------------------------------------------------------------
# Etat
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _video_dir(video_id: str, config: Config) -> Path:
    return Path(config.workspace_dir) / video_id


def new_state(video_id: str, source_url: str, mode: str, *, channel: str | None = None) -> dict[str, Any]:
    return {
        "video_id": video_id,
        "source_url": source_url,
        "channel": channel,
        "mode": mode,
        "status": "pending",
        "reason": None,
        "attempts": 0,
        "retry_at": None,
        "awaiting": [],
        "enqueued_at": _iso(_now()),
        "updated_at": _iso(_now()),
        "steps": {
            name: {"status": "pending", "reason": None, "started_at": None, "finished_at": None, "progress": None}
            for name in STEPS
        },
        "clips": [],
    }


# Remplacement atomique : ``channel.replace_retrying`` reessaie un fichier
# verrouille par un lecteur (Windows) puis releve l'erreur d'origine. Le
# fichier temporaire porte le pid et le thread : le worker et l'API web
# ecrivent les memes pipeline.json sans se voler leur ``.tmp``.
def _atomic_replace(tmp: Path, path: Path) -> None:
    try:
        channel_mod.replace_retrying(tmp, path)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise


def _tmp_for(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def save_state(state: dict[str, Any], *, config: Config | None = None) -> Path:
    config = config or load_config()
    path = _video_dir(state["video_id"], config) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _iso(_now())
    tmp = _tmp_for(path)
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    _atomic_replace(tmp, path)
    return path


def load_state(video_id: str, *, config: Config | None = None) -> dict[str, Any]:
    config = config or load_config()
    path = _video_dir(video_id, config) / STATE_FILE
    if not path.exists():
        raise PipelineError(f"aucun etat pour la video {video_id} ({path}) : lancer d'abord 'run <url>'")
    state = json.loads(path.read_text(encoding="utf-8"))
    steps = state.get("steps")
    if isinstance(steps, dict) and any(name not in steps for name in STEPS):
        # Etat ecrit avant l'ajout d'une etape (ex. action) : elle est simplement a faire.
        pending = {"status": "pending", "reason": None, "started_at": None, "finished_at": None, "progress": None}
        state["steps"] = {**{name: steps.get(name, dict(pending)) for name in STEPS},
                          **{name: step for name, step in steps.items() if name not in STEPS}}
    return state


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise PipelineError(f"fichier absent : {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    tmp = _tmp_for(path)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _atomic_replace(tmp, path)


# --------------------------------------------------------------------------
# Erreurs transitoires
# --------------------------------------------------------------------------


def _transient_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [
        llm.TransientLLMError, ConnectionError, TimeoutError, urllib.error.URLError,
    ]
    try:
        from yt_dlp.networking.exceptions import TransportError

        types.append(TransportError)
    except ImportError:
        pass
    return tuple(types)


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` et toutes les erreurs qu'elle enveloppe (cause, contexte,
    ``exc_info`` de yt-dlp), sans doublon ; on ne descend pas sous une
    ``LLMError`` non transitoire."""
    seen: set[int] = set()
    out: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        e = todo.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        out.append(e)
        if isinstance(e, llm.LLMError) and not isinstance(e, llm.TransientLLMError):
            continue  # erreur LLM definitive : ce qu'elle enveloppe ne compte jamais (ADR-ad2e)
        wrapped = getattr(e, "exc_info", None)
        if isinstance(wrapped, tuple) and len(wrapped) > 1 and isinstance(wrapped[1], BaseException):
            todo.append(wrapped[1])
        todo += [e.__cause__, e.__context__]
    return out


def is_subscriber_only(exc: BaseException) -> bool:
    """Vrai si la chaine contient l'``ExtractorError`` yt-dlp d'une VOD Twitch
    reservee aux abonnes : reessayer ne changera rien (TASK-e2a1)."""
    try:
        from yt_dlp.utils import ExtractorError
    except ImportError:
        return False
    return any(isinstance(e, ExtractorError) and "subscriber-only content" in str(e)
               for e in _chain(exc))


def is_transient(exc: BaseException) -> bool:
    """Vrai si l'erreur (ou une erreur qu'elle enveloppe : cause, contexte,
    ``exc_info`` de yt-dlp) peut disparaitre en reessayant plus tard. Une VOD
    reservee aux abonnes ne l'est jamais, meme si la chaine contient aussi une
    erreur reseau."""
    if is_subscriber_only(exc):
        return False
    types = _transient_types()
    for e in _chain(exc):
        if isinstance(e, llm.LLMError) and not isinstance(e, llm.TransientLLMError):
            continue
        if isinstance(e, types):
            return True
    return False


# --------------------------------------------------------------------------
# Etapes
# --------------------------------------------------------------------------


def _progress_gate(total: int) -> Any:
    """Ferme une fonction ``gate(i)`` (i de 1 a ``total``) qui dit si l'element
    ``i`` doit etre annonce a INFO : au plus toutes les 30 s ou tous les 10 %
    (toujours le dernier). Sert a la progression des boucles longues (clip par
    clip) sans inonder la console (done_criteria de TASK-8abc)."""
    last: dict[str, float] = {"i": 0, "t": time.monotonic()}
    step = max(1, math.ceil(total * 0.1)) if total else 1

    def gate(i: int) -> bool:
        now = time.monotonic()
        if i >= total or i - last["i"] >= step or now - last["t"] >= 30.0:
            last["i"], last["t"] = i, now
            return True
        return False

    return gate


class _ProgressWriter:
    """Rappel progress(fraction, eta_s, message) injecte dans les options
    d'une etape de _PROGRESS_STEPS : met a jour steps.<name>.progress de
    l'etat en memoire a chaque appel, mais ne reecrit pipeline.json que si au
    moins _PROGRESS_MIN_INTERVAL_S s se sont ecoulees depuis la derniere
    ecriture (toujours la derniere valeur connue, jamais une valeur perimee
    au-dela de ce delai)."""

    def __init__(self, run: "_Run", name: str):
        self._run = run
        self._name = name
        self._last_write: float | None = None

    def __call__(self, fraction: float, eta_s: float | None, message: str) -> None:
        state, config = self._run.state, self._run.config
        state["steps"][self._name]["progress"] = {"fraction": fraction, "eta_s": eta_s, "message": message}
        now = time.monotonic()
        if self._last_write is None or now - self._last_write >= _PROGRESS_MIN_INTERVAL_S:
            self._last_write = now
            save_state(state, config=config)


class _Run:
    """Contexte d'un passage du pipeline sur une video."""

    def __init__(self, state: dict[str, Any], config: Config, force: bool,
                 step_options: dict[str, dict[str, Any]] | None, *,
                 forced: Any = frozenset(), clip_filter: list[str] | None = None):
        self.state = state
        self.config = config
        self.force = force
        self.forced = frozenset(forced) if forced is not None else frozenset()
        self.clip_filter = set(clip_filter) if clip_filter is not None else None
        # Copie par etape : ne modifie jamais le dict step_options du caller.
        self.options = {name: dict(opts) for name, opts in (step_options or {}).items()}
        for name in _PROGRESS_STEPS:
            self.options.setdefault(name, {})
            self.options[name]["progress"] = _ProgressWriter(self, name)
        self.video_id = state["video_id"]
        self.ws = Path(config.workspace_dir)
        self.out = Path(config.output_dir)
        self.dir = self.ws / self.video_id
        self.current_step: str | None = None

    def opts(self, name: str) -> dict[str, Any]:
        return dict(self.options.get(name, {}))

    def _forced(self, name: str) -> bool:
        return self.force or name in self.forced

    def journal(self) -> str:
        return str(self.config.section("feedback")["journal_path"])

    def clips(self) -> list[dict[str, Any]]:
        return _read_json(self.dir / "captions.json")["clips"]

    def _target_clips(self) -> list[dict[str, Any]]:
        """``clips()`` restreint a ``clip_filter`` (render cible d'un seul
        clip, SPEC-74e9 §4.5) ; identique a ``clips()`` sinon. N'affecte que
        les boucles clip par clip (reframe/subtitles/render/qa), jamais le
        resume final (_summary), qui reste sur l'ensemble des clips."""
        clips = self.clips()
        if self.clip_filter is None:
            return clips
        return [c for c in clips if c["id"] in self.clip_filter]

    # -- une methode par etape -------------------------------------------

    def download(self) -> None:
        settings = self.config.section("download")
        download.download(self.state["source_url"], self.ws, **settings, **self.opts("download"))

    def transcribe(self) -> None:
        opts = self.opts("transcribe")
        opts.pop("progress", None)  # pas encore consomme par clipper.transcribe (mesure par minutes)
        transcribe.transcribe(self.video_id, self.ws, config=self.config, force=self._forced("transcribe"),
                              **opts)

    def scenes(self) -> None:
        settings = self.config.section("scenes")
        # peak_windows : positionne ici seul, d'apres [action] enabled (SPEC-b0f3 R4bis).
        peak_windows = bool(self.config.section("action")["enabled"])
        scenes.detect_scenes(self.dir / f"{self.video_id}.mp4", self.ws, self.video_id,
                             force=self._forced("scenes"), peak_windows=peak_windows,
                             **settings, **self.opts("scenes"))

    def audio(self) -> None:
        s = self.config.section("audio")
        audio.run(self.video_id, self.ws, force=self._forced("audio"), sample_rate=s["sample_rate"],
                  window_seconds=s["window_seconds"], median_window_seconds=s["median_window_seconds"],
                  threshold_db=s["peak_threshold_db"], **self.opts("audio"))

    def action(self) -> None:
        action.run(self.video_id, self.ws, config=self.config, force=self._forced("action"),
                   **self.opts("action"))

    def _moments(self, force: bool) -> None:
        k = int(self.config.section("pipeline")["feedback_examples"])
        examples = feedback.examples(k, path=self.journal())
        moments.run(self.video_id, self.ws, config=self.config, force=force, examples=examples,
                    **self.opts("moments"))

    def moments(self) -> None:
        # Moments refait : les ids sont renumerotes, les decisions de review.json (indexees par
        # moment) ne s'appliquent plus (Important 4, revue r-pipeline) : mises de cote.
        # Seulement s'il est vraiment refait (force, ou moments.json absent) : un passage qui
        # saute l'etape deja faite garde les decisions.
        forced = self._forced("moments")
        if self.config.mode == "review" and (forced or not (self.dir / "moments.json").exists()):
            _set_aside_review(self.dir)
        self._moments(forced)

    def vision(self) -> None:
        vision.run(self.video_id, self.ws, config=self.config, force=self._forced("vision"), **self.opts("vision"))
        # vision.json plus recent que moments.json : moments, relance sans
        # force, re-note ses candidats (bonus visuel) sans rappeler le LLM.
        self._moments(False)

    def parts(self) -> None:
        parts.run(self.video_id, self.ws, config=self.config, force=self._forced("parts"), **self.opts("parts"))

    def captions(self) -> None:
        force = self._forced("captions")
        changed = self.config.mode == "review" and _apply_review(self)
        before_ids = {c["id"] for c in self.clips()} if changed and (self.dir / "captions.json").exists() else None
        if changed:
            # parts.json a change (moment retire ou borne ajustee) : captions.json, qui liste les clips
            # lus par reframe/subtitles/render/qa, doit etre refait meme si captions etait deja 'done'
            # (Important 2, revue r-transcription), sinon le moment refuse est tout de meme rendu et
            # une borne ajustee est ignoree.
            force = True
            self.forced = self.forced | set(STEPS[STEPS.index(_AFTER_REVIEW):])
        captions.run(self.video_id, self.ws, config=self.config, force=force, **self.opts("captions"))
        if before_ids is not None:
            # Un clip qui disparait de captions.json (moment refuse, parts refait) laisse sinon son
            # ancienne sortie sur disque, proposable a la publication bien qu'absente de l'etat.
            stale = before_ids - {c["id"] for c in self.clips()}
            for clip_id in stale:
                out_dir = self.out / self.video_id
                (out_dir / f"{clip_id}.mp4").unlink(missing_ok=True)
                (out_dir / f"{clip_id}.json").unlink(missing_ok=True)

    def reframe(self) -> None:
        opts = self.opts("reframe")
        progress = opts.pop("progress", None)
        forced = self._forced("reframe")
        if self.config.section("reframe")["layout"] == "stream_auto":
            # Facecam detectee une fois pour toute la video (SPEC-3a88), avant les clips.
            reframe.detect_facecam(self.video_id, self.ws, config=self.config, force=forced,
                                   detector_factory=opts.get("detector_factory"))
        clips = self._target_clips()
        total = len(clips)
        gate = _progress_gate(total)
        t_start = time.monotonic()
        for i, clip in enumerate(clips, 1):
            t0 = time.monotonic()
            reframe.reframe(self.video_id, clip["id"], clip["start"], clip["end"], self.ws,
                            config=self.config, force=forced, **opts)
            elapsed = time.monotonic() - t0
            log.debug("%s : reframe clip %d/%d (%s) en %.1fs", self.video_id, i, total, clip["id"], elapsed)
            if gate(i):
                log.info("%s : reframe clip %d/%d (%s) en %.1fs", self.video_id, i, total, clip["id"], elapsed)
            if progress is not None and total:
                avg = (time.monotonic() - t_start) / i
                progress(i / total, avg * (total - i), f"clip {i}/{total} ({clip['id']})")

    def _subtitles_clip(self, clip: dict[str, Any]) -> None:
        plan = _read_json(self.dir / "reframe" / f"{clip['id']}.json")
        layout = plan.get("layout")
        if layout in ("letterbox", "stream"):
            zones = {"text_zone": subtitles_zone(plan, clip["id"])}
        elif layout == "stream_split":
            # SPEC-76dc : style a deux couleurs (mot en cours), jamais les
            # paliers d'emphase LLM du style letterbox.
            zones = {"text_zone": subtitles_zone(plan, clip["id"]), "style": "split"}
        else:
            zones = {"avoid_zones": avoid_zones(plan), "reserved_zones": hook_zones(clip, self.config)}
        opts = self.opts("subtitles")
        opts.pop("progress", None)
        subtitles.generate(self.video_id, clip["id"], clip["start"], clip["end"], self.ws,
                           config=self.config, force=self._forced("subtitles"), **zones, **opts)

    def subtitles(self) -> None:
        # Jusqu'a ``parallel`` clips a la fois (threads : le temps passe dans
        # l'appel LLM d'emphase, un sous-processus). Un clip en echec laisse
        # les autres aller au bout (leurs .ass restent ecrits), puis l'erreur
        # du premier clip en echec remonte (ADR-ad2e). reframe et render
        # restent sequentiels (GPU/NVENC, ADR-fb9b).
        parallel = self.config.section("subtitles")["parallel"]
        if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
            raise PipelineError(f"[subtitles] parallel doit etre un entier >= 1, recu {parallel!r}")
        progress = self.opts("subtitles").get("progress")
        clips = self._target_clips()
        total = len(clips)
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = [executor.submit(self._subtitles_clip, clip) for clip in clips]
        for i, future in enumerate(futures, 1):
            future.result()
            if progress is not None and total:
                progress(i / total, None, f"clip {i}/{total}")

    def render(self) -> None:
        opts = self.opts("render")
        progress = opts.pop("progress", None)
        forced = self._forced("render")
        clips = self._target_clips()
        total = len(clips)
        gate = _progress_gate(total)
        t_start = time.monotonic()
        for i, clip in enumerate(clips, 1):
            t0 = time.monotonic()
            render_step.render(self.video_id, clip["id"], self.ws, self.out, config=self.config,
                               force=forced, **opts)
            elapsed = time.monotonic() - t0
            log.debug("%s : render clip %d/%d (%s) en %.1fs", self.video_id, i, total, clip["id"], elapsed)
            if gate(i):
                log.info("%s : render clip %d/%d (%s) en %.1fs", self.video_id, i, total, clip["id"], elapsed)
            if progress is not None and total:
                avg = (time.monotonic() - t_start) / i
                progress(i / total, avg * (total - i), f"clip {i}/{total} ({clip['id']})")

    def qa(self) -> None:
        if not self.clips():
            return
        if self.clip_filter is not None:
            # Cible un seul clip (SPEC-74e9 §4.5, re-rendu apres edition du
            # titre d'ecran) : controle direct par qa.check_clip, jamais
            # qa.run qui parcourt tout output/<video_id>/.
            settings = self.config.section("qa")
            opts = self.opts("qa")
            for clip in self._target_clips():
                json_path = self.out / self.video_id / f"{clip['id']}.json"
                qa.check_clip(json_path, self.dir / "qa" / clip["id"], settings, config=self.config, **opts)
            return
        qa.run(self.video_id, self.ws, self.out, config=self.config, force=self._forced("qa"), **self.opts("qa"))


def preview_subtitles(config: Config, text: str) -> bytes:
    """PNG de ``text`` dans le style de sous-titres effectif de ``config``
    (TASK-dd3f, SPEC-c100 E5) : seul point d'entree de clipper.web (ADR-b16b).
    Le style est celui qu'un clip de cette config recevrait en agencement
    stream (split si [reframe] stream_variant = "split", sinon letterbox)."""
    variant = {**reframe.CONFIG_DEFAULTS, **config.section("reframe")}["stream_variant"]
    try:
        return subtitles.render_preview(
            {**subtitles.CONFIG_DEFAULTS, **config.section("subtitles")}, text,
            style="split" if variant == "split" else "letterbox",
        )
    except subtitles.SubtitlesError as exc:
        raise PipelineError(f"apercu des sous-titres impossible : {exc}") from exc


def clip_thumbnail(config: Config, video_id: str, clip_id: str) -> Path:
    """Miniature JPEG (<= ``[render] thumbnail_width`` px) du clip
    output/<video_id>/<clip_id>.mp4, en cache sous workspace/<video_id>/
    thumbnails/ : une seule extraction, reutilisee tant que le mp4 garde la
    meme date de modification (la miniature recoit celle du mp4). Seul point
    d'entree de clipper.web pour les vignettes (ADR-09ad, ADR-b16b)."""
    mp4 = Path(config.output_dir) / video_id / f"{clip_id}.mp4"
    if not mp4.is_file():
        raise PipelineError(f"clip introuvable : {mp4}")
    mtime_ns = mp4.stat().st_mtime_ns
    target = Path(config.workspace_dir) / video_id / "thumbnails" / f"{clip_id}.jpg"
    if target.is_file() and target.stat().st_mtime_ns == mtime_ns:
        return target
    try:
        render_step.thumbnail(mp4, target, config=config)
    except render_step.RenderError as exc:
        raise PipelineError(f"miniature du clip {video_id}/{clip_id} impossible : {exc}") from exc
    os.utime(target, ns=(mtime_ns, mtime_ns))
    return target


def video_thumbnail(config: Config, video_id: str) -> Path:
    """Vignette JPEG (<= ``[render] thumbnail_width`` px) de la video source
    workspace/<video_id>/<video_id>.mp4, prise a ``[render]
    video_thumbnail_seek_ratio`` de sa duree (meta.json), en cache sous
    workspace/<video_id>/thumbnails/ : une seule extraction, reutilisee tant
    que la source garde la meme date de modification. Meme schema que
    ``clip_thumbnail`` ; le web ne traite jamais de video (ADR-09ad)."""
    video_dir = Path(config.workspace_dir) / video_id
    mp4 = video_dir / f"{video_id}.mp4"
    if not mp4.is_file():
        raise PipelineError(f"video source introuvable : {mp4}")
    mtime_ns = mp4.stat().st_mtime_ns
    target = video_dir / "thumbnails" / SOURCE_THUMBNAIL
    if target.is_file() and target.stat().st_mtime_ns == mtime_ns:
        return target
    meta = video_dir / "meta.json"
    duration = _read_json(meta).get("duration") if meta.is_file() else None
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
        raise PipelineError(
            f"vignette de {video_id} impossible : duree de la source inconnue "
            f"({meta} absent ou sans duree valide)"
        )
    ratio = float({**render_step.CONFIG_DEFAULTS, **config.section("render")}["video_thumbnail_seek_ratio"])
    try:
        render_step.thumbnail(mp4, target, config=config, seek=duration * ratio)
    except render_step.RenderError as exc:
        raise PipelineError(f"vignette de la video {video_id} impossible : {exc}") from exc
    os.utime(target, ns=(mtime_ns, mtime_ns))
    return target


def subtitles_zone(plan: dict[str, Any], clip_id: str) -> dict[str, Any]:
    """Zone des sous-titres d'un plan de recadrage letterbox (SPEC-6127) ou
    stream (SPEC-3a88) :
    ``text_zones.subtitles`` a la racine du plan. Absente : erreur explicite
    (subtitles verifie ensuite sa coherence)."""
    text_zones = plan.get("text_zones")
    if not isinstance(text_zones, dict) or "subtitles" not in text_zones:
        raise PipelineError(
            f"plan de recadrage letterbox du clip {clip_id} sans text_zones.subtitles : "
            "relancer reframe --force"
        )
    return text_zones["subtitles"]


def avoid_zones(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Bandes verticales [haut, bas] (fraction de la hauteur de sortie)
    couvertes par les visages retenus (``retained: true``) du plan de
    recadrage (reframe/<clip_id>.json) : ``[{"start", "end", "bands"}]``,
    temps en secondes de la video. Un visage detecte mais non retenu (main,
    sac, torse, ecran...) ne bloque pas de place. Chaque visage retenu
    visible dans un panneau donne sa propre bande (deux visages eloignes ne
    bloquent pas l'espace entre eux) ; le fond flou ne compte pas. Les
    sous-titres ne recouvrent pas ces bandes (SPEC-6127). Un visage sans
    champ ``retained`` (ancien format de reframe) est une erreur explicite :
    jamais une supposition silencieuse (ADR-ad2e)."""
    out_h = float(plan["output"]["height"])
    zones = []
    for p in plan["plans"]:
        bands: list[list[float]] = []
        for face in p["faces"]:
            if "retained" not in face:
                raise PipelineError(
                    f"visage {face.get('id')!r} sans champ 'retained' dans le plan de recadrage "
                    "(ancien format) : relancer reframe --force"
                )
            if not face["retained"]:
                continue
            x0, y0, x1, y1 = face["box"]
            for panel in p["panels"]:
                if panel.get("effect") == "blur":
                    continue
                dest = panel["dest"]
                for r in panel["rects"]:
                    if face["last"] < r["start"] or face["first"] > r["end"]:
                        continue
                    iy0, iy1 = max(y0, r["y"]), min(y1, r["y"] + r["h"])
                    if max(x0, r["x"]) >= min(x1, r["x"] + r["w"]) or iy0 >= iy1:
                        continue
                    scale = dest["h"] / r["h"]
                    fy0 = dest["y"] + (iy0 - r["y"]) * scale
                    fy1 = dest["y"] + (iy1 - r["y"]) * scale
                    band = [max(0.0, fy0 / out_h), min(1.0, fy1 / out_h)]
                    if band not in bands:
                        bands.append(band)
        zones.append({"start": p["start"], "end": p["end"], "bands": bands})
    return zones


# Hauteur de ligne de l'accroche, en multiple de sa taille de police : marge
# pour les jambages et le contour du texte dessine par render (drawtext).
HOOK_LINE_HEIGHT = 1.5


def hook_zones(clip: dict[str, Any], config: Config) -> list[dict[str, Any]]:
    """Bande de l'accroche que render dessine en haut du clip (une ligne a
    ``hook_margin_top`` px, ``hook_seconds`` premieres secondes), au format de
    ``avoid_zones`` : les sous-titres ne la recouvrent jamais."""
    s = config.section("render")
    top = float(s["hook_margin_top"])
    bottom = top + HOOK_LINE_HEIGHT * float(s["hook_font_size"])
    return [{"start": clip["start"], "end": clip["start"] + float(s["hook_seconds"]),
             "bands": [[top / subtitles.PLAY_RES_Y, bottom / subtitles.PLAY_RES_Y]]}]


# --------------------------------------------------------------------------
# Revue humaine
# --------------------------------------------------------------------------


def _read_review(video_dir: Path) -> dict[str, Any]:
    path = video_dir / REVIEW_FILE
    if not path.exists():
        return {"decisions": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _set_aside_review(video_dir: Path) -> None:
    """Renomme review.json (horodate) : ses decisions visaient d'anciens moments."""
    path = video_dir / REVIEW_FILE
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    aside = path.with_name(f"{REVIEW_FILE}.{stamp}")
    n = 1
    while aside.exists():
        aside = path.with_name(f"{REVIEW_FILE}.{stamp}-{n}")
        n += 1
    try:
        channel_mod.replace_retrying(path, aside)  # pas de test d'existence prealable : un faux negatif laisserait le fichier perime
    except FileNotFoundError:
        return
    log.info("%s : moments refait, %s perime mis de cote (%s)", video_dir.name, REVIEW_FILE, aside.name)


def _undecided(video_dir: Path) -> list[int]:
    ids = [m["id"] for m in _read_json(video_dir / "parts.json")["moments"]]
    decisions = _read_review(video_dir)["decisions"]
    return [i for i in ids if str(i) not in decisions]


class _ReviewPending(Exception):
    """Levee par _apply_review : des moments de parts.json (refait) n'ont pas de decision
    humaine ; _advance_steps remet la video en awaiting_review."""


def _apply_review(run: _Run) -> bool:
    """Applique les decisions humaines avant captions : bornes ajustees dans
    moments.json (parts refait), moments refuses sortis de parts.json. Rend
    True si parts.json a change : l'appelant doit alors refaire captions.json
    (et les etapes suivantes) meme deja 'done' (Important 2, revue
    r-transcription)."""
    decisions = _read_review(run.dir)["decisions"]

    moments_path = run.dir / "moments.json"
    moments_data = _read_json(moments_path)
    adjusted = False
    for m in moments_data["moments"]:
        d = decisions.get(str(m["id"]))
        if d and d["decision"] == "adjusted" and (m["start"], m["end"]) != (d["start"], d["end"]):
            m["start"], m["end"] = d["start"], d["end"]
            m["duration"] = round(d["end"] - d["start"], 3)
            adjusted = True
    if adjusted:
        _write_json(moments_path, moments_data)
        parts.run(run.video_id, run.ws, config=run.config, force=True, **run.opts("parts"))
        # La re-decoupe peut garder un moment rejete au premier passage : l'humain ne l'a jamais
        # vu, il n'a aucune decision. Retour en attente de revue, jamais de rendu sans decision.
        if _undecided(run.dir):
            raise _ReviewPending

    parts_path = run.dir / "parts.json"
    parts_data = _read_json(parts_path)
    kept, refused = [], []
    for m in parts_data["moments"]:
        (refused if decisions[str(m["id"])]["decision"] == "rejected" else kept).append(m)
    if refused:
        parts_data["moments"] = kept
        parts_data["rejected"] = parts_data.get("rejected", []) + [
            {"id": m["id"], "start": m["start"], "end": m["end"], "duration": m["duration"],
             "reason": "refuse en revue humaine"}
            for m in refused
        ]
        _write_json(parts_path, parts_data)
    return adjusted or bool(refused)


def _moment_text(video_dir: Path, start: float, end: float) -> str:
    transcript = _read_json(video_dir / "transcript.json")
    words = [w["word"] for seg in transcript["segments"] for w in seg["words"]
             if w["start"] >= start - 1e-6 and w["end"] <= end + 1e-6]
    return "".join(words).strip()


def decide(
    video_id: str,
    moment_id: int,
    decision: str,
    *,
    start: float | None = None,
    end: float | None = None,
    comment: str | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """Enregistre la decision humaine sur un moment (accepted | rejected |
    adjusted, ce dernier avec ses nouvelles bornes) : journal clipper.feedback
    et workspace/<video_id>/review.json. Renvoie l'entree du journal."""
    config = config or load_config()
    if decision not in feedback.VALID_DECISIONS:
        raise PipelineError(f"decision invalide {decision!r} (attendu : {' | '.join(feedback.VALID_DECISIONS)})")
    if decision == "adjusted":
        if start is None or end is None or not end > start >= 0:
            raise PipelineError("une decision adjusted demande des bornes start < end (--start, --end)")
    elif start is not None or end is not None:
        raise PipelineError(f"bornes donnees pour une decision {decision} : seules les decisions adjusted en ont")

    video_dir = _video_dir(video_id, config)
    by_id = {m["id"]: m for m in _read_json(video_dir / "moments.json")["moments"]}
    if moment_id not in by_id:
        raise PipelineError(f"moment {moment_id} absent de {video_dir / 'moments.json'} (ids : {sorted(by_id)})")

    moment = dict(by_id[moment_id])
    if decision == "adjusted":
        moment.update(start=start, end=end, duration=round(end - start, 3))
    text = _moment_text(video_dir, moment["start"], moment["end"])
    entry = feedback.record(video_id, moment, decision, text, comment,
                            path=config.section("feedback")["journal_path"])

    review = _read_review(video_dir)
    review["decisions"][str(moment_id)] = {
        "decision": decision, "start": moment["start"], "end": moment["end"],
        "comment": comment, "at": entry["horodatage"],
    }
    _write_json(video_dir / REVIEW_FILE, review)

    try:
        state = load_state(video_id, config=config)
    except PipelineError:
        return entry
    if (video_dir / "parts.json").exists():
        state["awaiting"] = _undecided(video_dir)
        save_state(state, config=config)
    return entry


# --------------------------------------------------------------------------
# Enchainement
# --------------------------------------------------------------------------


def _zero_clip_reason(run: _Run) -> str | None:
    """Raison explicite (ADR-ad2e) quand la video finit sans aucun clip parce
    que l'etape moments n'a retenu aucun candidat : nombre de candidats notes,
    meilleur score, seuil ``min_score``. ``None`` si moments.json est absent
    ou a retenu au moins un moment (0 clip final vient alors d'ailleurs, ex.
    revue humaine, hors perimetre de cette raison)."""
    moments_path = run.dir / "moments.json"
    if not moments_path.exists():
        return None
    data = _read_json(moments_path)
    if data["moments"]:
        return None
    scored = [m for m in data["rejected"] if "final_score" in m]
    if not scored:
        return "aucun candidat retenu par moments, 0 clip"
    best = max(m["final_score"] for m in scored)
    min_score = data["rubric"]["min_score"]
    return f"{len(scored)} candidats, meilleur score {best} < min_score {min_score}, 0 clip"


def _summary(run: _Run) -> list[dict[str, Any]]:
    out = []
    for clip in run.clips():
        json_path = run.out / run.video_id / f"{clip['id']}.json"
        data = _read_json(json_path)
        out.append({
            "clip_id": clip["id"],
            "ready": qa.is_ready(data),
            "qa_status": data["qa"]["status"],
            "issues": data["qa"]["issues"],
            "mp4": str(json_path.with_suffix(".mp4")),
            "json": str(json_path),
        })
    return out


def _fail(run: _Run, name: str, exc: BaseException) -> dict[str, Any]:
    state, config = run.state, run.config
    reason = f"{type(exc).__name__}: {exc}"
    if is_subscriber_only(exc):
        reason = f"VOD reservee aux abonnes (echec definitif, aucun re-essai) : {reason}"
    step = state["steps"][name]
    step.update(status="failed", reason=reason, finished_at=_iso(_now()))
    step["progress"] = None
    settings = config.section("pipeline")
    if config.mode == "auto" and is_transient(exc):
        max_attempts = int(settings["max_attempts"])
        delays = list(settings["retry_delays"])
        if delays and max_attempts >= 1:
            state["attempts"] += 1
            if state["attempts"] < max_attempts:
                delay = delays[min(state["attempts"], len(delays)) - 1]
                state.update(status="queued", reason=f"{name} : {reason}",
                             retry_at=_iso(_now() + timedelta(seconds=float(delay))))
                log.warning("%s : %s en echec transitoire, re-essai a %s", run.video_id, name, state["retry_at"])
                save_state(state, config=config)
                return state
            reason = f"{reason} ({state['attempts']} echecs transitoires, max_attempts = {max_attempts})"
        else:
            # [pipeline] retry_delays = [] (ou max_attempts < 1) : pas de re-essai plutot qu'un IndexError
            # qui laissait l'etape 'running' pour toujours, sans raison journalisee (Mineur 2, revue
            # r-transcription, ADR-ad2e).
            reason = f"{reason} (aucun re-essai : [pipeline] retry_delays vide ou max_attempts < 1)"
    state.update(status="failed", reason=f"{name} : {reason}", retry_at=None)
    log.error("%s : etape %s en echec : %s", run.video_id, name, reason)
    save_state(state, config=config)
    return state


USAGE_LOG_FILE = "llm_usage.jsonl"


def _usage_summary(usage_log_path: Path) -> dict[str, dict[str, float | int]]:
    """Totaux par usage (appels, tokens, cout) accumules dans le journal de
    consommation de la video depuis son debut ; vide si aucun appel LLM
    n'a encore ete journalise. Une ligne tronquee (ecriture coupee) est
    journalisee et ignoree plutot que de lever (Mineur 1, revue
    r-transcription) : sinon l'exception sortait du ``finally`` de
    ``_advance`` et tuait le worker en cours de reprise."""
    totals: dict[str, dict[str, float | int]] = {}
    if not usage_log_path.exists():
        return totals
    for line in usage_log_path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            log.error("%s : ligne de consommation LLM illisible, ignoree : %s", usage_log_path, exc)
            continue
        bucket = totals.setdefault(entry["usage"], {
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0,
        })
        bucket["calls"] += 1
        for field_name in ("input_tokens", "output_tokens", "cache_read_tokens", "cost_usd"):
            value = entry.get(field_name)
            if value is not None:
                bucket[field_name] += value
    return totals


def _step_duration(step: dict[str, Any]) -> float:
    started, finished = step.get("started_at"), step.get("finished_at")
    if not started or not finished:
        return 0.0
    return round((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds(), 1)


def _log_run_summary(run: _Run, usage_summary: dict[str, dict[str, float | int]]) -> None:
    """Resume final d'un run termine (status done) : duree par etape, nombre
    de clips, statuts qa, cout LLM total et par usage, chemin de sortie
    (done_criteria de TASK-8abc)."""
    durations = {name: _step_duration(run.state["steps"][name]) for name in STEPS}
    clips = run.state.get("clips") or []
    qa_counts: dict[str, int] = {}
    for clip in clips:
        qa_counts[clip["qa_status"]] = qa_counts.get(clip["qa_status"], 0) + 1
    total_cost = sum(bucket.get("cost_usd") or 0.0 for bucket in usage_summary.values())
    log.info(
        "%s : termine - %d clip(s) %s, duree par etape %s, cout LLM total %.4f$ (%s), sortie %s",
        run.video_id, len(clips), qa_counts, durations, total_cost, usage_summary,
        run.out / run.video_id,
    )


class _EventsHandler(logging.Handler):
    """Ecrit chaque enregistrement INFO+ emis par le logger ``clipper`` (donc
    par toute etape, y compris les transitions deja journalisees par
    _advance_steps a ce niveau) comme une ligne JSON dans
    workspace/<video_id>/events.jsonl (SPEC-74e9 §3.2), jamais tronque."""

    def __init__(self, path: Path, run: _Run):
        super().__init__(level=logging.INFO)
        self.path = path
        self.run = run

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "at": _iso(_now()),
            "level": record.levelname,
            "step": self.run.current_step,
            "message": record.getMessage(),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@contextmanager
def _events_journal(path: Path, run: _Run):
    """Attache _EventsHandler au logger ``clipper`` pour la duree du passage.
    Force temporairement ce logger a INFO si son niveau effectif etait plus
    haut (ex. WARNING par defaut de la CLI sans -v) : sinon les
    enregistrements INFO n'atteindraient jamais le handler."""
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("clipper")
    handler = _EventsHandler(path, run)
    previous_level = logger.level
    raise_level = previous_level == logging.NOTSET or previous_level > logging.INFO
    if raise_level:
        logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        if raise_level:
            logger.setLevel(previous_level)


def _advance(run: _Run, *, through_review: bool) -> dict[str, Any]:
    """Enchaine les etapes restantes (voir _advance_steps) sous
    ``llm.usage_log`` : chaque appel LLM du passage (y compris ceux faits
    depuis un thread, ex. l'etape subtitles) est journalise dans
    workspace/<video_id>/llm_usage.jsonl ; un resume par usage (tokens, cout,
    cumules depuis le debut de la video) est journalise a la fin du passage,
    qu'il se termine en succes, en echec ou en attente de revue. Un run qui va
    jusqu'au bout (status done) ajoute un resume complet (voir
    ``_log_run_summary``). Sous ``_events_journal``, chaque enregistrement
    INFO+ de ce passage (y compris ceux-ci) devient une ligne d'events.jsonl."""
    usage_log_path = run.dir / USAGE_LOG_FILE
    events_path = run.dir / EVENTS_FILE
    with _events_journal(events_path, run), llm.usage_log(usage_log_path):
        try:
            return _advance_steps(run, through_review=through_review)
        finally:
            summary = _usage_summary(usage_log_path)
            if summary:
                log.info("%s : consommation LLM par usage %s", run.video_id, summary)
            if run.state.get("status") == "done":
                _log_run_summary(run, summary)


def _advance_steps(run: _Run, *, through_review: bool) -> dict[str, Any]:
    state, config = run.state, run.config
    state.update(status="running", reason=None, retry_at=None, mode=config.mode)
    save_state(state, config=config)

    for name in STEPS:
        if name == _AFTER_REVIEW and config.mode == "review":
            state["awaiting"] = _undecided(run.dir)
            reviewed = state["steps"][_AFTER_REVIEW]["status"] == "done"
            if state["awaiting"] or not (through_review or reviewed):
                state.update(status="awaiting_review", reason=None)
                save_state(state, config=config)
                if through_review:
                    raise PipelineError(
                        f"decisions manquantes pour les moments {state['awaiting']} de {run.video_id} : "
                        f"python -m clipper decide {run.video_id} <moment_id> accepted|rejected|adjusted"
                    )
                return state

        step = state["steps"][name]
        run.current_step = name
        # Une etape deja "done" et non forcee se saute d'elle-meme (ADR-b16b) :
        # elle garde la date et la duree de sa vraie premiere execution.
        skipped = step["status"] == "done" and not run._forced(name)
        if not skipped:
            step.update(status="running", reason=None, started_at=_iso(_now()), finished_at=None)
            save_state(state, config=config)
        log.info("%s : etape %s", run.video_id, name)
        t0 = time.monotonic()
        try:
            getattr(run, name)()
        except _ReviewPending:
            state["awaiting"] = _undecided(run.dir)
            step.update(status="pending", reason=None, started_at=None, finished_at=None)
            run.current_step = None
            reason = f"moments {state['awaiting']} sans decision apres re-decoupe, a decider"
            log.info("%s : %s", run.video_id, reason)
            state.update(status="awaiting_review", reason=reason)
            save_state(state, config=config)
            if through_review:
                raise PipelineError(
                    f"decisions manquantes pour les moments {state['awaiting']} de {run.video_id} : "
                    f"python -m clipper decide {run.video_id} <moment_id> accepted|rejected|adjusted"
                )
            return state
        except Exception as exc:  # noqa: BLE001 - toute erreur est journalisee dans l'etat
            log.debug("%s : %s", run.video_id, name, exc_info=True)
            if skipped:
                step["started_at"] = _iso(_now())  # l'echec date de ce passage, pas de la premiere execution
            result = _fail(run, name, exc)
            run.current_step = None
            return result
        elapsed = time.monotonic() - t0
        if not skipped:
            step.update(status="done", finished_at=_iso(_now()))
            step["progress"] = None
            # Une etape qui vient reellement de reussir remet le compteur
            # d'echecs transitoires a 0 : ce sont des echecs *consecutifs*
            # (Important 4, revue r-transcription), pas un cumul sur toute la
            # video. Une etape sautee (deja ``done`` avant ce passage) ne
            # prouve rien de nouveau, elle ne remet rien a 0.
            state["attempts"] = 0
            save_state(state, config=config)
        log.info("%s : etape %s terminee en %.1fs", run.video_id, name, elapsed)
        run.current_step = None

    clips = _summary(run)
    reason = _zero_clip_reason(run) if not clips else None
    if reason:
        log.info("%s : termine sans clip (%s)", run.video_id, reason)
    state.update(status="done", reason=reason, retry_at=None, attempts=0, clips=clips)
    save_state(state, config=config)
    return state


def _start(
    state: dict[str, Any], config: Config, force: bool,
    step_options: dict[str, dict[str, Any]] | None,
    *, force_steps: list[str] | None = None, clips: list[str] | None = None, manual: bool = True,
) -> _Run:
    """``force`` remet toutes les etapes a pending et les force toutes.
    ``force_steps`` (sans ``force``) ne remet a pending, et ne force, que
    l'etape nommee la plus en amont et toutes celles qui la suivent dans
    STEPS (SPEC-74e9 §3.3) : les precedentes restent ``done``. ``manual``
    (une relance ``run``/``render``, jamais ``process_queue``) remet
    ``attempts`` a 0 : une relance a la main part d'une ardoise propre, les
    echecs transitoires d'avant (quota de la nuit...) ne comptent plus pour
    ``max_attempts`` (Important 4, revue r-transcription)."""
    state.pop("dismissed_at", None)  # une relance reprend la video : elle n'est plus « retiree »
    if manual:
        state["attempts"] = 0
    if force:
        forced = set(STEPS)
        for step in state["steps"].values():
            step.update(status="pending", reason=None, started_at=None, finished_at=None, progress=None)
    elif force_steps:
        unknown = set(force_steps) - set(STEPS)
        if unknown:
            raise PipelineError(
                f"force_steps inconnue(s) : {', '.join(sorted(unknown))} (attendu parmi {', '.join(STEPS)})"
            )
        idx = min(STEPS.index(name) for name in force_steps)
        forced = set(STEPS[idx:])
        for name in forced:
            state["steps"][name].update(status="pending", reason=None, started_at=None, finished_at=None,
                                        progress=None)
    else:
        forced = set()
    return _Run(state, config, force, step_options, forced=forced, clip_filter=clips)


def _with_short_clips(
    step_options: dict[str, dict[str, Any]] | None, short_clips: bool | None
) -> dict[str, dict[str, Any]] | None:
    """Le choix « clips courts » de la video (TASK-4f5e) devient une option de l'etape
    moments ; None (non precise) ne change rien : valeur du style."""
    if short_clips is None:
        return step_options
    if not isinstance(short_clips, bool):
        raise PipelineError(f"short_clips invalide : {short_clips!r} (attendu : true ou false)")
    options = {name: dict(opts) for name, opts in (step_options or {}).items()}
    options.setdefault("moments", {})["short_clips"] = short_clips
    return options


def run(
    url: str,
    *,
    config: Config | None = None,
    force: bool = False,
    force_steps: list[str] | None = None,
    step_options: dict[str, dict[str, Any]] | None = None,
    channel: str | None = None,
    short_clips: bool | None = None,
    manual: bool = True,
) -> dict[str, Any]:
    """Traite la video ``url`` : jusqu'a la revue en mode review, jusqu'au
    bout en mode auto. Renvoie l'etat (voir le docstring du module).

    ``step_options`` : arguments supplementaires par etape (injection pour
    les tests, ex. ``{"download": {"ydl_factory": ...}}``). ``channel`` :
    chaine dont le preset a servi (SPEC-74e9 §3.1), gardee dans l'etat.
    ``short_clips`` : choix de la video pour les clips courts (None = valeur du style).
    ``manual`` : relance a la main (``attempts`` remis a 0) ; ``False`` (CLI
    ``--resume``, enfant lance par le worker pour une video ``queued``) garde
    ``attempts`` : la reprise automatique compte pour ``max_attempts`` (audit
    10/10, coeur-I5)."""
    step_options = _with_short_clips(step_options, short_clips)
    config = config or load_config()
    try:
        video_id = download.extract_video_id(url)
    except download.DownloadError as exc:
        raise PipelineError(str(exc)) from exc
    try:
        state = load_state(video_id, config=config)
    except PipelineError:
        state = new_state(video_id, url, config.mode, channel=channel)
    state["source_url"] = url
    if channel is not None:
        state["channel"] = channel
    return _advance(_start(state, config, force, step_options, force_steps=force_steps, manual=manual),
                    through_review=False)


def download_only(
    url: str,
    *,
    config: Config | None = None,
    channel: str | None = None,
    step_options: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Prechargement (TASK-3c1c) : execute l'etape ``download`` SEULE de ``url`` pendant que le worker traite
    une autre video. Etape deja ``done`` : rien n'est relance (ADR-b16b). La video n'est ni ``running`` ni
    ``done`` : seul son etape download change, le statut de la video reste ``pending``. Un echec marque l'etape
    et la video ``failed`` avec la raison (jamais une video faussement prete, ADR-ad2e) puis leve
    ``PipelineError`` ; le prochain ``run`` refait le download."""
    config = config or load_config()
    try:
        video_id = download.extract_video_id(url)
    except download.DownloadError as exc:
        raise PipelineError(str(exc)) from exc
    try:
        state = load_state(video_id, config=config)
    except PipelineError:
        state = new_state(video_id, url, config.mode, channel=channel)
    state["source_url"] = url
    if channel is not None:
        state["channel"] = channel
    step = state["steps"]["download"]
    if step["status"] == "done":
        return state
    run = _Run(state, config, False, step_options)
    step.update(status="running", reason=None, started_at=_iso(_now()), finished_at=None)
    save_state(state, config=config)
    t0 = time.monotonic()
    try:
        run.download()
    except Exception as exc:  # noqa: BLE001 - toute erreur est journalisee dans l'etat
        reason = f"{type(exc).__name__}: {exc}"
        step.update(status="failed", reason=reason, finished_at=_iso(_now()))
        state.update(status="failed", reason=f"download : {reason}", retry_at=None)
        log.error("%s : prechargement du download en echec : %s", video_id, reason)
        save_state(state, config=config)
        raise PipelineError(f"download de {video_id} en echec : {reason}") from exc
    step.update(status="done", finished_at=_iso(_now()))
    save_state(state, config=config)
    log.info("%s : download precharge en %.1fs", video_id, time.monotonic() - t0)
    return state


def render(
    video_id: str,
    *,
    config: Config | None = None,
    force: bool = False,
    force_steps: list[str] | None = None,
    step_options: dict[str, dict[str, Any]] | None = None,
    channel: str | None = None,
    clips: list[str] | None = None,
    short_clips: bool | None = None,
    manual: bool = True,
) -> dict[str, Any]:
    """Reprend une video deja lancee jusqu'au bout (captions .. qa) ; en mode
    review, exige une decision pour chaque moment (PipelineError sinon).
    ``clips`` restreint reframe/subtitles/render/qa a ces clip_id (render
    cible, SPEC-74e9 §4.5) ; le resume final reste sur tous les clips.
    ``short_clips`` et ``manual`` : voir ``run``."""
    step_options = _with_short_clips(step_options, short_clips)
    config = config or load_config()
    state = load_state(video_id, config=config)
    if channel is not None:
        state["channel"] = channel
    return _advance(
        _start(state, config, force, step_options, force_steps=force_steps, clips=clips, manual=manual),
        through_review=True,
    )


def dismiss_video(video_id: str, *, config: Config | None = None) -> dict[str, Any]:
    """Retire une video en echec ou en attente de reprise des echecs et des
    compteurs du tableau de bord : pose ``dismissed_at`` dans son pipeline.json
    (reversible par ``restore_video``). Le dossier workspace n'est pas touche."""
    config = config or load_config()
    state = load_state(video_id, config=config)
    if state.get("status") not in DISMISSIBLE_STATUSES:
        raise PipelineError(
            f"{video_id} est {state.get('status')!r} : seule une video en echec (failed) "
            "ou en attente de reprise (queued) peut etre retiree"
        )
    state["dismissed_at"] = _iso(_now())
    save_state(state, config=config)
    return state


def restore_video(video_id: str, *, config: Config | None = None) -> dict[str, Any]:
    """Annule ``dismiss_video`` : la video reapparait dans les echecs."""
    config = config or load_config()
    state = load_state(video_id, config=config)
    if state.pop("dismissed_at", None) is not None:
        save_state(state, config=config)
    return state


def set_channel(
    video_id: str, channel: str, *, config: Config | None = None, presets_dir: str | Path = "presets",
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Attribue ``channel`` a une video qui n'en a pas (console, fiche video) : ecrit ``channel`` dans son
    pipeline.json et le journalise. Refuse une chaine sans preset, une video en cours de traitement (le worker
    reecrit son etat), une video qui a deja une chaine (ses publications sont rangees par chaine) et une video
    qui a encore des publications non terminees dans la file « sans chaine » (revue r-comptes 4). Ses
    publications terminees (publiees, refusees) suivent la video dans la file du style (revue fable-comptes 1 :
    restees dans ``_sans_chaine.json``, un clip publie redevenait « a valider » et se republiait)."""
    from clipper import publish as publish_mod  # import tardif : publish n'est pas une etape du pipeline

    config = config or load_config()
    state = load_state(video_id, config=config)
    if channel not in channel_mod.list_channels(presets_dir):
        raise PipelineError(f"chaîne inconnue : {channel!r} (aucun preset presets/{channel}.toml avec une table [channel])")
    if state.get("status") == "running":
        raise PipelineError(f"{video_id} est en cours de traitement : attends la fin avant de lui attribuer une chaîne")
    current = state.get("channel")
    if current is not None:
        raise PipelineError(f"{video_id} a déjà la chaîne « {current} » : elle ne se change pas ici")
    state["channel"] = channel
    try:
        moved = publish_mod.adopt_video_entries(video_id, channel, state_dir=state_dir,
                                                commit=lambda: save_state(state, config=config))
    except publish_mod.PublishError as exc:
        raise PipelineError(str(exc)) from exc
    log.info("%s : chaîne « %s » attribuée depuis la console (%d publication(s) rattachée(s))",
             video_id, channel, len(moved))
    return state


def queued(*, config: Config | None = None) -> list[dict[str, Any]]:
    """Etats des videos en file d'attente, par retry_at croissant. Un
    ``pipeline.json`` tronque est journalise et ignore (Mineur 1, revue
    r-transcription) plutot que de faire lever cette fonction a chaque appel,
    ce qui bloquait la reprise de toutes les videos tant que le fichier
    n'etait pas repare a la main."""
    config = config or load_config()
    root = Path(config.workspace_dir)
    states = []
    for path in sorted(root.glob(f"*/{STATE_FILE}")) if root.is_dir() else []:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("%s : etat illisible, ignore de la reprise de file : %s", path, exc)
            continue
        if state["status"] == "queued" and not state.get("dismissed_at"):
            states.append(state)
    return sorted(states, key=lambda s: s["retry_at"])


def process_queue(
    *,
    config: Config | None = None,
    now: datetime | None = None,
    step_options: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Reprend chaque video en file dont ``retry_at`` est passe, dans CE
    processus ; renvoie leurs nouveaux etats. C'est la reprise de la CLI
    ``clipper queue`` : le worker, lui, ne l'appelle plus (audit 10/10,
    coeur-I5) et met chaque video due en file (``state/queue.json``) pour un
    enfant ``clipper run|render --resume``. Une video rattachee a une chaine repart avec le
    preset de cette chaine, son mode compris (SPEC-74e9 §1.3), pas avec la
    config globale : si la chaine a disparu ou son preset est invalide,
    l'erreur est journalisee, gardee dans ``reason`` et la video reste en
    attente (ADR-ad2e, aucun repli sur la config globale). Reprise sans
    decision humaine requise (``through_review=False``) : si le mode est
    devenu ``review`` entre la mise en file et la reprise, la video s'arrete
    proprement en ``awaiting_review`` au lieu de lever (Important 3, revue
    r-transcription) -- une reprise automatique ne doit jamais interrompre
    les videos suivantes de la file. Chaque reprise garde ``attempts`` tel
    quel (``manual=False``) : ce n'est pas une relance manuelle. Une erreur
    inattendue d'une video (etat ou journal illisible...) est journalisee et
    n'empeche jamais la reprise des videos suivantes (Mineur 1, revue
    r-transcription, ADR-ad2e)."""
    config = config or load_config()
    now = now or _now()
    out = []
    for state in queued(config=config):
        if datetime.fromisoformat(state["retry_at"]) > now:
            continue
        video_id = state.get("video_id")
        try:
            run_config = config
            if state.get("channel") is not None:
                try:
                    run_config = _channel_config(state["channel"], config)
                except (channel_mod.ChannelError, ConfigError) as exc:
                    reason = f"chaine {state['channel']!r} inutilisable a la reprise : {exc}"
                    if state.get("reason") != reason:
                        log.error("%s : %s", video_id, reason)
                        state["reason"] = reason
                        save_state(state, config=config)
                    continue
            out.append(_advance(
                _start(state, run_config, False, step_options, manual=False), through_review=False,
            ))
        except Exception:  # noqa: BLE001 - une video en echec inattendu ne bloque jamais les suivantes
            log.exception("%s : reprise en file interrompue par une erreur inattendue", video_id)
    return out


def _channel_config(name: str, config: Config) -> Config:
    """Config du preset de la chaine ``name``, dont le mode est celui de la
    chaine (``[channel].mode``, a defaut le mode global). Le preset est lu
    dans ``[watch] presets_dir`` sur ``[watch] base_config`` de ``config``,
    jamais dans ``presets/`` et ``config.toml`` du dossier courant (audit
    10/10, coeur-M3 : avec un dossier de styles configure ailleurs, toute
    video en file d'une chaine restait « chaine inconnue » pour toujours)."""
    watch = config.section("watch")
    preset_config, channel = channel_mod.load_channel(name, presets_dir=watch["presets_dir"], base=watch["base_config"])
    return dataclass_replace(preset_config, mode=str(channel["mode"]))


def watch_queue(*, config: Config | None = None, interval: float = 60.0) -> None:
    """Traite la file d'attente en boucle (jusqu'a interruption)."""
    config = config or load_config()
    while True:
        process_queue(config=config)
        time.sleep(interval)
