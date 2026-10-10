"""Construction de l'application FastAPI de clipper/web (voir le docstring
du paquet). Appelee par ``python -m clipper serve`` et par les tests
(clipper.web.app.create_app avec un pipeline/worker simules).

ADR-35b7 §1 : cette API n'appelle jamais pipeline.run/render dans son propre
processus ; le traitement passe toujours par la file (clipper.worker)."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time as time_mod
import tomllib
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from email.parser import BytesParser
from email.policy import HTTP as _EMAIL_HTTP
from typing import Any, AsyncIterator, Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse as _PlainJSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, StrictBool

from clipper import accounts as accounts_mod
from clipper import browser as browser_mod
from clipper import network as network_mod
from clipper import channel as channel_mod
from clipper import gpu as gpu_mod
from clipper import journal as journal_mod
from clipper import learning as learning_mod
from clipper import moments as moments_mod
from clipper import pipeline
from clipper import publish as publish_mod
from clipper import reframe as reframe_mod
from clipper import render as render_mod
from clipper import repartition as repartition_mod
from clipper import tiktok as tiktok_mod
from clipper import youtube as youtube_mod
from clipper import veille as veille_mod
from clipper import watch as watch_mod
from clipper import workspace as workspace_mod
from clipper import worker as worker_mod
from clipper.config import (
    DEFAULTS as _CONFIG_FLAT_DEFAULTS,
    VALID_MODES,
    Config,
    ConfigError,
    _defaults_documentation,
    _section_defaults,
    load_config,
    write_config,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Video/clip ids sont soit des ids YouTube (11 caracteres alphanumeriques),
# soit des clip_id du pipeline (ex. "03-p2") : jamais de '/' ni de '..' pour
# empecher toute traversee de chemin dans /media.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

_LOOPBACK_HOST = "127.0.0.1"
logger = logging.getLogger(__name__)

_PRESETS_DIR = "presets"
_PROTECTED_PREFIXES = ("/api", "/media")
_TOKEN_HEADER = "x-clipper-token"
_TOKEN_COOKIE = "clipper_token"


class WebConfigError(Exception):
    """[web] host hors bouclage sans jeton configure (ADR-35b7 §5, ADR-ad2e :
    jamais d'exposition silencieuse)."""


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(value: Any) -> Any:
    """Retire les sequences ANSI (couleurs de yt-dlp...) de toute chaine d'une
    structure JSON : l'interface affiche du texte, jamais des codes terminal."""
    if isinstance(value, str):
        return _ANSI_RE.sub("", value)
    if isinstance(value, list):
        return [_strip_ansi(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_ansi(v) for k, v in value.items()}
    return value


class JSONResponse(_PlainJSONResponse):
    """Toute reponse JSON de l'API (raisons d'echec, journal, erreurs) sort
    sans sequence ANSI."""

    def render(self, content: Any) -> bytes:
        return super().render(_strip_ansi(content))


def _safe_video_file(directory: Path, name: str) -> Path:
    if not _SAFE_ID.fullmatch(name):
        raise HTTPException(status_code=404, detail=f"identifiant invalide : {name!r}")
    path = directory / f"{name}.mp4"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"fichier introuvable : {path}")
    return path


def _list_states(config: Config) -> list[dict[str, Any]]:
    root = Path(config.workspace_dir)
    if not root.is_dir():
        return []
    states = []
    for path in sorted(root.glob(f"*/{pipeline.STATE_FILE}")):
        states.append(json.loads(path.read_text(encoding="utf-8")))
    return states


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


_PARIS = ZoneInfo("Europe/Paris")
_YOUTUBE_HOSTS = ("youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _paris(value: Any) -> str | None:
    """Une date ISO avec fuseau, ramenee en heure de Paris (zoneinfo : +02:00 l'ete, +01:00 l'hiver) ; None si
    absente. Toute heure affichee par la console est celle de Paris, jamais un decalage fixe."""
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when.astimezone(_PARIS).isoformat()


def _platform_thumbnail(config: Config, video_id: str, source_url: str | None) -> str | None:
    """Miniature de la plateforme d'une video (chargee par le navigateur, aucun traitement serveur) : YouTube se
    deduit de l'identifiant ; sinon l'URL donnee par yt-dlp, enregistree dans meta.json (telechargement
    termine) ou thumbnail.json (debut du telechargement). None si rien n'est connu."""
    host = urlparse(source_url or "").netloc.lower().removeprefix("www.")
    if host in _YOUTUBE_HOSTS and _SAFE_ID.fullmatch(video_id):
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    video_dir = Path(config.workspace_dir) / video_id
    for name, key in (("meta.json", "thumbnail"), ("thumbnail.json", "url")):
        path = video_dir / name
        if not path.is_file():
            continue
        try:
            url = _read_json(path).get(key)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(url, str) and url:
            return url
    return None


def _source_duration(video_dir: Path) -> tuple[float | None, str | None]:
    """Duree de la source (meta.json de l'etape download) ; sinon (None, raison)
    : une donnee introuvable est affichee, jamais remplacee par une valeur."""
    path = video_dir / "meta.json"
    if not path.exists():
        return None, f"{path} absent : le telechargement n'est pas termine"
    duration = _read_json(path).get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return None, f"duree de la source inconnue dans {path}"
    return duration, None


def _moment_transcript(video_dir: Path, start: float, end: float) -> tuple[str | None, str | None]:
    """Texte du moment via pipeline._moment_text (pas de logique dupliquee) ;
    sinon (None, raison)."""
    path = video_dir / "transcript.json"
    if not path.exists():
        return None, f"{path} absent : la transcription n'est pas faite"
    return pipeline._moment_text(video_dir, start, end), None
# Statuts valides d'une video (contrat pipeline.json) : un filtre hors de cette
# liste est une erreur, jamais une liste vide silencieuse (ADR-ad2e).
_VIDEO_STATUSES = ("pending", "running", "awaiting_review", "queued", "done", "failed")
# « interrompue » (TASK-bdd5) n'est jamais ecrit dans pipeline.json : l'API le deduit d'un statut « running »
# sans processus (worker.is_interrupted). Seul le filtre de la liste l'accepte en plus.
_VIDEO_FILTER_STATUSES = (*_VIDEO_STATUSES, "interrupted")


def _parse_ts(video_id: str, step: str, key: str, value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"horodatage illisible ({key}) pour l'etape {step} de {video_id} : {value!r}",
        ) from exc


def _step_durations(state: dict[str, Any]) -> dict[str, float | None]:
    """Duree (s) de chaque etape = finished_at - started_at ; None tant que
    l'etape n'a pas fini (aucune duree inventee)."""
    out: dict[str, float | None] = {}
    for name, step in (state.get("steps") or {}).items():
        started, finished = step.get("started_at"), step.get("finished_at")
        if started and finished:
            video_id = state.get("video_id", "?")
            delta = _parse_ts(video_id, name, "finished_at", finished) - _parse_ts(video_id, name, "started_at", started)
            out[name] = delta.total_seconds()
        else:
            out[name] = None
    return out


def _current_step(state: dict[str, Any]) -> str | None:
    steps = state.get("steps") or {}
    for name, step in steps.items():
        if step.get("status") == "running":
            return name
    for name, step in steps.items():
        if step.get("status") != "done":
            return name
    return None


def _pipeline_created_at(path: Path) -> tuple[datetime, str]:
    """Date de creation de ``path`` et sa source : ``pipeline_json_created``
    (date de naissance du fichier, ou ctime sous Windows) ; sur un systeme qui
    n'en expose pas, ``pipeline_json_mtime`` le dit au lieu de le taire."""
    info = path.stat()
    birth = getattr(info, "st_birthtime", None)
    if birth is None and sys.platform == "win32":
        birth = info.st_ctime
    if birth is not None:
        return datetime.fromtimestamp(birth, timezone.utc), "pipeline_json_created"
    return datetime.fromtimestamp(info.st_mtime, timezone.utc), "pipeline_json_mtime"


def _added_at(state: dict[str, Any], config: Config) -> tuple[str, str]:
    """Date d'ajout d'une video et sa source : ``enqueued_at`` si elle est
    passee par la file, sinon la date de creation de son pipeline.json."""
    if state.get("enqueued_at"):
        return state["enqueued_at"], "enqueued_at"
    when, source = _pipeline_created_at(Path(config.workspace_dir) / state["video_id"] / pipeline.STATE_FILE)
    return when.isoformat(), source


def _enrich(state: dict[str, Any], config: Config) -> dict[str, Any]:
    """Etat pipeline.json + titre (meta.json de download, sinon l'identifiant
    avec la raison), etape courante et duree par etape. Une video « running » sans processus (serveur ou PC
    arrete en plein traitement) est rapportee ``interrupted``, jamais « en cours » (TASK-bdd5)."""
    video_id = state["video_id"]
    out = dict(state)
    out["interrupted"] = False
    try:
        if worker_mod.is_interrupted(state, config):
            out.update(status="interrupted", interrupted=True,
                       reason=f"interrompue à l'étape {_current_step(state)} : plus aucun processus ne travaille dessus")
    except worker_mod.WorkerError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    meta_path = Path(config.workspace_dir) / video_id / "meta.json"
    title = None
    if meta_path.exists():
        title = _read_json(meta_path).get("title")
        reason = None if title else f"meta.json de {video_id} sans titre"
    else:
        reason = f"titre inconnu : meta.json absent pour {video_id} (telechargement pas encore fait)"
    out["title"] = title or video_id
    out["title_reason"] = reason
    out["current_step"] = _current_step(state)
    out["durations"] = _step_durations(state)
    out["platform_thumbnail"] = _platform_thumbnail(config, video_id, state.get("source_url"))
    out["purged"] = workspace_mod.is_purged(video_id, config.workspace_dir)
    return out


def _rubric_used(video_dir: Path) -> str | None:
    """Grille de notation utilisee par l'etape moments (rubric.path de
    moments.json, SPEC-9216 R4) ; None tant que l'etape n'a rien ecrit. En clips
    courts, ``rubric.path`` est la copie aux bornes courtes : la grille nommee est
    celle d'origine (``rubric.source``)."""
    path = video_dir / "moments.json"
    if not path.exists():
        return None
    rubric = _read_json(path).get("rubric") or {}
    return rubric.get("source") or rubric.get("path")


def _short_clips_used(video_dir: Path) -> dict[str, Any] | None:
    """Mode clips courts ecrit dans moments.json (TASK-4f5e) : {on, min, max}
    (bornes seulement si actif) ; None tant que l'etape n'a rien ecrit ou pour un
    moments.json d'avant ce reglage."""
    path = video_dir / "moments.json"
    if not path.exists():
        return None
    data = _read_json(path)
    if "short_clips" not in data:
        return None
    on = bool(data["short_clips"])
    return {"on": on, **({"min": data.get("short_min"), "max": data.get("short_max")} if on else {})}


def _matches(video: dict[str, Any], channel: str | None, status: str | None, q: str | None) -> bool:
    if channel is not None and video.get("channel") != channel:
        return False
    if status is not None and video.get("status") != status:
        return False
    if q:
        needle = q.lower()
        haystack = (video["video_id"], video["title"], video.get("source_url") or "")
        if not any(needle in text.lower() for text in haystack):
            return False
    return True


def _jury_confidence(scored: dict[str, Any]) -> tuple[int | float | None, dict[str, Any] | None]:
    """(confiance agrégée, dernière confiance de chaque juge) d'un moment de
    moments.json (SPEC-73d0 R4) ; (None, None) s'il n'est pas passé par le
    jury : aucune valeur n'est inventée."""
    jury = scored.get("jury")
    if not jury or "confidence" not in jury:
        return None, None
    latest: dict[str, Any] = {}
    for rnd in jury["trace"]["rounds"]:
        latest.update({name: j["confidence"] for name, j in rnd["judges"].items()})
    return jury["confidence"], latest


def _moments_jury_confidences(config: Config, video_id: str) -> dict[Any, tuple[Any, Any]]:
    """moment_id -> _jury_confidence, pour les moments de la vidéo qui en ont une."""
    path = Path(config.workspace_dir) / video_id / "moments.json"
    if not path.exists():
        return {}
    return {m["id"]: _jury_confidence(m) for m in _read_json(path)["moments"]}


def _jury_rounds(jury: dict[str, Any]) -> list[dict[str, Any]]:
    """Tours du jury, cumules : chaque tour donne l'etat de tous les juges
    (un juge absent du tour 2 garde ses notes du tour 1, ``revised`` False)."""
    latest: dict[str, Any] = {}
    out = []
    for rnd in jury["trace"]["rounds"]:
        latest = {**latest, **rnd["judges"]}
        out.append({"round": rnd["round"], "judges": {
            name: {**j, "revised": name in rnd["judges"]} for name, j in latest.items()}})
    return out


def _jury_reason(moment: dict[str, Any], reject_reason: str | None, threshold: Any) -> tuple[str, str]:
    """(categorie, raison) de la retenue ou du rejet d'un moment : lue dans
    moments.json, jamais devinee ; une raison de rejet inconnue reste « autre »."""
    if reject_reason is None:
        if moment.get("exploration"):
            return "exploration", "clip d'exploration : non retenu par la grille, pris pour apprendre ce que le jury sous-estime"
        return "retenu", f"score final {moment['final_score']} au moins égal au seuil {threshold}"
    if reject_reason.startswith("veto "):
        return "veto", reject_reason
    if "< min_score" in reject_reason:
        return "score", reject_reason
    if "plafond" in reject_reason:
        return "plafond", reject_reason
    return "autre", reject_reason


def _parts_rejected_reasons(config: Config, video_id: str) -> dict[Any, str]:
    """{id du moment: raison} des moments que l'etape parts a ecartes au decoupage ;
    vide tant que parts.json n'existe pas (etape pas encore faite)."""
    path = Path(config.workspace_dir) / video_id / "parts.json"
    if not path.exists():
        return {}
    try:
        rejected = _read_json(path).get("rejected") or []
        return {r["id"]: str(r["reason"]) for r in rejected}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=500, detail=f"parts.json illisible pour {video_id} : {exc!r}") from exc


def _jury_view(config: Config, video_id: str) -> dict[str, Any]:
    """Moments retenus puis non retenus avec le detail du jury (lecture seule)."""
    path = Path(config.workspace_dir) / video_id / "moments.json"
    empty: dict[str, Any] = {"video_id": video_id, "available": False, "reason": None, "moments": []}
    if not path.exists():
        return {**empty, "reason": f"moments.json absent pour {video_id} (étape Moments pas encore faite)"}
    try:
        data = _read_json(path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"moments.json illisible pour {video_id} : {exc}") from exc
    rubric = data.get("rubric") or {}
    if "weights" not in rubric:
        return {**empty, "reason": f"moments.json de {video_id} sans grille (rubric.weights)"}
    if not any("jury" in m for m in [*data.get("moments", []), *data.get("rejected", [])]):
        return {**empty, "reason": f"moments.json de {video_id} sans jury (selection : {data.get('selection', 'inconnue')})"}
    cut_rejected = _parts_rejected_reasons(config, video_id)
    threshold = rubric.get("min_score")
    rows = [(m, None, f"kept-{i}") for i, m in enumerate(data.get("moments", []))]
    rows += [(m, m.get("reason") or "rejeté sans raison dans moments.json", f"rejected-{i}")
             for i, m in enumerate(data.get("rejected", []))]
    moments = []
    for m, reject_reason, key in rows:
        jury = m.get("jury")
        if not jury:
            continue
        kind, reason = _jury_reason(m, reject_reason, threshold)
        cut_reason = cut_rejected.get(m.get("id")) if reject_reason is None else None
        if cut_reason is not None:
            kind, reason = "decoupage", cut_reason
        moments.append({
            "key": key, "id": m.get("id"), "retained": reject_reason is None,
            "start": m["start"], "end": m["end"], "format": m.get("format"),
            "scores": m.get("scores"), "final_score": m.get("final_score"),
            "jury_score": jury.get("score"), "confidence": jury.get("confidence"),
            "veto": jury.get("veto"), "debated": bool(jury.get("debated")),
            "proposer_scores": (jury.get("proposer") or {}).get("scores"),
            "rounds": _jury_rounds(jury),
            "reason_kind": kind, "reason": reason,
            "justification": m.get("justification"), "hook_text": m.get("hook_text"),
            **({"source": m["source"]} if "source" in m else {}),
            **({"cut_rejected": cut_reason} if cut_reason is not None else {}),
        })
    return {
        "video_id": video_id, "available": True, "reason": None,
        "criteria": [{"name": n, "weight": w} for n, w in rubric["weights"].items()],
        "threshold": threshold, "selection": data.get("selection"),
        "exploration": data.get("exploration"), "judges": (data.get("jury") or {}).get("judges", []),
        "moments": moments,
    }


def _list_moments(config: Config, video_id: str) -> list[dict[str, Any]]:
    video_dir = Path(config.workspace_dir) / video_id
    parts_path = video_dir / "parts.json"
    if not parts_path.exists():
        raise HTTPException(status_code=404, detail=f"pas de moments a valider pour {video_id}")

    parts_by_id = {m["id"]: m for m in _read_json(parts_path)["moments"]}
    moments_path = video_dir / "moments.json"
    scores_by_id = {m["id"]: m for m in _read_json(moments_path)["moments"]} if moments_path.exists() else {}
    review_path = video_dir / "review.json"
    decisions = _read_json(review_path)["decisions"] if review_path.exists() else {}

    source_duration, duration_error = _source_duration(video_dir)

    out = []
    for moment_id, part in sorted(parts_by_id.items()):
        scored = scores_by_id.get(moment_id, {})
        transcript, transcript_error = _moment_transcript(video_dir, part["start"], part["end"])
        confidence, judge_confidences = _jury_confidence(scored)
        out.append({
            "id": moment_id,
            "start": part["start"],
            "end": part["end"],
            "duration": part["duration"],
            "format": part["format"],
            "parts_total": part["parts_total"],
            "score": scored.get("final_score"),
            "justification": scored.get("justification"),
            "hook_text": scored.get("hook_text"),
            "confidence": confidence,
            "judge_confidences": judge_confidences,
            "decision": decisions.get(str(moment_id)),
            "preview_url": f"/media/source/{video_id}",
            "transcript": transcript,
            "transcript_error": transcript_error,
            "source_duration": source_duration,
            "source_duration_error": duration_error,
        })
    return out


def _validate_video_id(video_id: str) -> None:
    if not _SAFE_ID.fullmatch(video_id):
        raise HTTPException(status_code=400, detail=f"identifiant video invalide : {video_id!r}")


def _validate_channel_name(channel: str) -> None:
    if not channel_mod.NAME_RE.match(channel):
        raise HTTPException(status_code=400, detail=f"nom de style invalide : {channel!r}")


def _channel_of(video_id: str, config: Config) -> str | None:
    try:
        state = pipeline.load_state(video_id, config=config)
    except pipeline.PipelineError:
        return None
    return state.get("channel")


_SOURCE_PURGED = "source purgée : retélécharger (relancer depuis l'étape « download »)"


def _require_source(video_id: str, action: str, force_steps: list[str] | None, config: Config) -> None:
    """TASK-886a : une video purgee n'a plus sa source ; tout rendu ou etape qui en a besoin est refuse en
    clair (409), sauf si le retelechargement (etape download) fait partie de la relance."""
    if not workspace_mod.is_purged(video_id, config.workspace_dir) or "download" in (force_steps or []):
        return
    try:
        steps = pipeline.load_state(video_id, config=config)["steps"]
    except pipeline.PipelineError:
        return
    if action == "render" or any(st.get("status") != "done" for st in steps.values()) or force_steps:
        raise HTTPException(status_code=409, detail=f"{video_id} : {_SOURCE_PURGED}")


def _enqueue(url: str, channel: str | None, action: str, force_steps: list[str] | None,
             config: Config, short_clips: bool | None = None) -> JSONResponse:
    if _SAFE_ID.fullmatch(url):
        _require_source(url, action, force_steps, config)
    # Le choix de la video n'est transmis que s'il est precise (TASK-4f5e) : sans lui,
    # l'entree garde sa forme d'avant et la valeur du style s'applique.
    extra = {} if short_clips is None else {"short_clips": short_clips}
    try:
        entry = worker_mod.enqueue(url, channel, action, force_steps, config=config, **extra)
    except worker_mod.WorkerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(entry, status_code=202)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_path(config: Config) -> Path:
    return Path(config.section("worker")["queue_path"])


def _state_kind_and_id(path: Path, state_root: Path) -> tuple[str, str]:
    """'queue.json' directement sous state/ -> kind='queue' ; un fichier sous
    un sous-dossier (publish/<chaine>.json, watch/<chaine>.json) -> kind=nom
    du sous-dossier, id=nom de chaine."""
    rel = path.relative_to(state_root)
    if len(rel.parts) == 1:
        return path.stem, path.stem
    return rel.parts[0], path.stem


def _watched_state_roots(config: Config) -> list[tuple[Path, str | None]]:
    """Dossiers d'état surveillés, déclarés par les réglages : le dossier de ``[worker] queue_path`` (file,
    battement...) et, hors de lui, ``[publish]``, ``[watch]`` et ``[repartition]`` ``state_dir``. Le second membre est le
    genre d'événement des fichiers posés directement dans le dossier (None : déduit du chemin)."""
    base = Path(str(config.section("worker")["queue_path"])).parent
    roots: list[tuple[Path, str | None]] = [(base, None)]
    for kind in ("publish", "watch", "repartition"):
        directory = Path(str(config.section(kind)["state_dir"]))
        if directory.resolve() != (base / kind).resolve():  # déjà couvert, avec le même genre, par le dossier de la file
            roots.append((directory, kind))
    return roots


# Sous-dossiers de state/ dont les fichiers déclenchent un événement temps réel (ce que l'interface écoute :
# publish, watch, veille, tiktok...). Tout autre sous-dossier n'est jamais parcouru : state/browser/<compte>/ contient
# les profils Chrome persistants (~19 000 entrées), les parcourir bloquait la boucle (TASK-40f1).
_WATCHED_SUBDIRS = ("publish", "watch", "repartition", "veille", "learning", "stats", "tiktok", "youtube")


def _scan_watched(workspace_root: Path, state_roots: list[tuple[Path, str | None]]) -> list[tuple[Path, str, str]]:
    found: list[tuple[Path, str, str]] = []
    if workspace_root.is_dir():
        for p in workspace_root.glob(f"*/{pipeline.STATE_FILE}"):
            found.append((p, "video", p.parent.name))
    seen: set[Path] = set()

    def _add(p: Path, kind: str, id_: str) -> None:
        if (kind, id_) == ("worker", "worker"):  # battement du worker : réécrit en continu, lu par le polling du tableau de bord
            return
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            found.append((p, kind, id_))

    # Racines à genre fixe d'abord : un fichier qu'elles contiennent garde ce genre, même sous la racine de la file.
    for state_root, fixed_kind in sorted(state_roots, key=lambda root: root[1] is None):
        if not state_root.is_dir():
            continue
        if fixed_kind:  # dossier d'état nommé par les réglages : parcouru en entier
            for p in state_root.rglob("*.json"):
                _add(p, fixed_kind, p.stem)
            continue
        # Racine de la file (state/) : ses fichiers, puis seulement les sous-dossiers d'état nommés ci-dessus.
        for p in state_root.glob("*.json"):
            _add(p, *_state_kind_and_id(p, state_root))
        for name in _WATCHED_SUBDIRS:
            sub = state_root / name
            if sub.is_dir():
                for p in sub.rglob("*.json"):
                    _add(p, *_state_kind_and_id(p, state_root))
    return found


def _scan_watched_mtimes(workspace_root: Path, state_roots: list[tuple[Path, str | None]]) -> list[tuple[Path, str, str, float]]:
    """``_scan_watched`` + mtime de chaque fichier : tout le travail disque d'un tour, destiné à un thread."""
    out: list[tuple[Path, str, str, float]] = []
    for p, kind, id_ in _scan_watched(workspace_root, state_roots):
        try:
            out.append((p, kind, id_, p.stat().st_mtime))
        except FileNotFoundError:
            continue
    return out


# --------------------------------------------------------------------------
# Tableau de bord (SPEC-c100 E1, T2, T8) : lecture de fichiers seulement.
# Une donnee introuvable est null avec une cle <champ>_error en francais,
# jamais un 0 ou une liste vide muets (ADR-ad2e).
# --------------------------------------------------------------------------

_NEXT_PUBLICATIONS = 5
_COST_WEEK_DAYS = 7
_MIB = 1024 * 1024


def _fill(out: dict[str, Any], fields: tuple[str, ...], label: str, build) -> None:
    """Renseigne ``fields`` avec ``build()`` (un dict champ -> valeur) ; si la
    lecture echoue, chaque champ vaut null et ``<champ>_error`` dit pourquoi."""
    try:
        out.update(build())
    except (OSError, ValueError, KeyError, TypeError, publish_mod.PublishError) as exc:
        for name in fields:
            out[name] = None
            out[f"{name}_error"] = f"{label} illisible : {exc}"


def _dashboard_videos(config: Config) -> dict[str, Any]:
    states = _list_states(config)
    running = []
    for state in states:
        if state.get("status") != "running":
            continue
        enriched = _enrich(state, config)
        if enriched["interrupted"]:
            continue  # jamais « en cours » : l'ecran Videos propose Reprendre / Annuler
        step = _current_step(state)
        running.append({
            "video_id": state["video_id"], "title": enriched["title"], "channel": state.get("channel"),
            "source_url": state.get("source_url"), "platform_thumbnail": enriched["platform_thumbnail"],
            "step": step, "progress": state["steps"][step].get("progress") if step else None,
        })

    def problem(status: str) -> list[dict[str, Any]]:
        # Une video « retiree » (dismissed_at) sort des echecs et des compteurs.
        return [
            {"video_id": s["video_id"], "title": _enrich(s, config)["title"], "channel": s.get("channel"),
             "platform_thumbnail": _platform_thumbnail(config, s["video_id"], s.get("source_url")),
             "step": _current_step(s), "reason": s.get("reason"), "retry_at": s.get("retry_at")}
            for s in states if s.get("status") == status and not s.get("dismissed_at")
        ]

    return {"running": running, "failed": problem("failed"), "queued": problem("queued")}


def _dashboard_watch(config: Config) -> dict[str, Any]:
    pending: list[dict[str, Any]] = []
    watch_dir = Path(config.section("watch")["state_dir"])
    for path in sorted(watch_dir.glob("*.json")) if watch_dir.is_dir() else []:
        try:
            pending.extend({**vod, "channel": path.stem} for vod in _read_json(path)["pending"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path.name} : {exc}") from exc
    return {"watch_pending": pending}


def _dashboard_clips_to_review(config: Config) -> dict[str, Any]:
    """Même statut que l'écran Clips (``_clip_publish_status``) : un clip refusé
    par la QA ou pas prêt n'est jamais « à valider »."""
    try:
        count = sum(1 for _v, _c, sidecar, entry in _iter_clips(config, None, None)
                    if _clip_publish_status(sidecar, entry) == _TO_VALIDATE)
    except HTTPException as exc:
        raise ValueError(exc.detail) from exc
    return {"clips_to_review": count}


def _scheduled_entries(publish_dir: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(publish_dir.glob("*.json")) if publish_dir.is_dir() else []:
        for entry in _read_json(path):
            for field_name in ("video_id", "clip_id", "status", "slot_at"):
                if field_name not in entry:
                    raise ValueError(f"{path.name} : champ {field_name!r} absent d'une entree")
            if entry["status"] == "scheduled" and entry["slot_at"]:
                entries.append({**entry, "channel": path.stem})
    return entries


def _dashboard_worker(config: Config) -> dict[str, Any]:
    try:
        return {"worker": worker_mod.read_heartbeat(config)}
    except worker_mod.WorkerError as exc:
        raise ValueError(str(exc)) from exc


def _dashboard_next_publications(config: Config) -> dict[str, Any]:
    entries = _scheduled_entries(Path(config.section("publish")["state_dir"]))
    entries.sort(key=lambda e: datetime.fromisoformat(e["slot_at"]).astimezone(timezone.utc))
    entries = entries[:_NEXT_PUBLICATIONS]
    for entry in entries:
        sidecar = Path(config.output_dir) / entry["video_id"] / f"{entry['clip_id']}.json"
        entry["screen_title"] = _read_json(sidecar).get("screen_title") if sidecar.is_file() else None
        entry["slot_at_paris"] = _paris(entry["slot_at"])
    return {"next_publications": entries}


def _dashboard_llm_cost(config: Config) -> dict[str, Any]:
    """Somme de workspace/*/llm_usage.jsonl : ``today`` = jour calendaire local,
    ``week`` = les 7 derniers jours locaux (aujourd'hui compris), ``by_usage`` =
    cout de la semaine par usage ; les appels sans cout rapporte sont comptes
    a part (``unreported_calls``), jamais pour 0."""
    now = datetime.now(_PARIS)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=_COST_WEEK_DAYS - 1)
    cost: dict[str, Any] = {"today": 0.0, "week": 0.0, "by_usage": {}, "unreported_calls": 0}
    root = Path(config.workspace_dir)
    for path in sorted(root.glob("*/llm_usage.jsonl")) if root.is_dir() else []:
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
                # clipper.llm date chaque appel de "timestamp" ; "recorded_at" est la forme du contrat
                recorded = datetime.fromisoformat(entry.get("recorded_at") or entry["timestamp"]).astimezone(_PARIS)
                usage = entry["usage"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path.parent.name}/{path.name} ligne {number} : {exc}") from exc
            if recorded < week_start or recorded > now:
                continue
            amount = entry.get("cost_usd")
            if amount is None:
                cost["unreported_calls"] += 1
                continue
            cost["week"] += amount
            cost["by_usage"][usage] = cost["by_usage"].get(usage, 0.0) + amount
            if recorded >= today_start:
                cost["today"] += amount
    return {"llm_cost": cost}


def _dashboard_hardware() -> dict[str, Any]:
    try:
        device = gpu_mod.get_device().type
    except Exception as exc:  # get_device ne leve pas en pratique ; jamais de device invente
        return {"hardware": None, "hardware_error": f"device illisible : {exc}"}
    hardware: dict[str, Any] = {"device": device, "vram_used_mb": None}
    if device != "cpu":
        try:
            hardware["vram_used_mb"] = gpu_mod.vram_used_mb()
        except gpu_mod.GpuError as exc:  # nvidia-smi absent ou illisible : indisponible, avec la raison
            hardware["vram_used_mb_error"] = f"VRAM utilisée indisponible : {exc}"
    return {"hardware": hardware}


def _with_thumbnails(config: Config, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entrees de la file avec la miniature de plateforme de leur video (None si inconnue)."""
    return [{**e, "platform_thumbnail": _platform_thumbnail(config, e["video_id"], e.get("url"))} for e in entries]


def _dashboard_zero_views(config: Config) -> dict[str, Any]:
    """Alertes « 0 vue à 24 h » (TASK-974e) : lecture seule des relevés déjà faits ; une erreur reste visible."""
    try:
        return {"zero_views": learning_mod.zero_view_alerts(datetime.now(timezone.utc), config=config)}
    except (learning_mod.LearningError, tiktok_mod.TikTokError) as exc:
        return {"zero_views": None, "zero_views_error": f"alertes 0 vue illisibles : {exc}"}


def _dashboard(config: Config) -> dict[str, Any]:
    out: dict[str, Any] = {}
    _fill(out, ("running", "failed", "queued"), "etat des videos (workspace/*/pipeline.json)",
          lambda: _dashboard_videos(config))
    queue_path = _queue_path(config)
    _fill(out, ("queue",), f"file d'attente ({queue_path.name})",
          lambda: {"queue": _with_thumbnails(config, _read_json(queue_path) if queue_path.exists() else [])})
    _fill(out, ("watch_pending",), "surveillance (state/watch)", lambda: _dashboard_watch(config))
    _fill(out, ("clips_to_review",), "clips a valider", lambda: _dashboard_clips_to_review(config))
    _fill(out, ("worker",), "battement du worker (state/worker.json)", lambda: _dashboard_worker(config))
    _fill(out, ("next_publications",), "publications (state/publish)", lambda: _dashboard_next_publications(config))
    _fill(out, ("llm_cost",), "journal llm_usage.jsonl", lambda: _dashboard_llm_cost(config))
    out.update(_dashboard_zero_views(config))
    out.update(_dashboard_hardware())
    return out


class _SseHub:
    """Un seul scanner par configuration, partagé par tous les clients SSE : une tâche qui scrute hors de la boucle
    (thread) à chaque intervalle et distribue les événements à N abonnés ; arrêtée quand plus aucun client."""

    def __init__(self, config: Config) -> None:
        self.interval = float(config.section("web")["sse_poll_interval_s"])
        self.workspace_root = Path(config.workspace_dir)
        self.state_roots = _watched_state_roots(config)
        self.subscribers: set[asyncio.Queue[str]] = set()
        self.mtimes: dict[Path, float] = {}
        self.task: asyncio.Task | None = None
        self.ready: asyncio.Event = asyncio.Event()

    async def _scan(self) -> list[tuple[Path, str, str, float]]:
        return await asyncio.to_thread(_scan_watched_mtimes, self.workspace_root, self.state_roots)

    async def _run(self) -> None:
        try:
            for p, _kind, _id, mtime in await self._scan():
                self.mtimes[p] = mtime  # état déjà présent à la connexion : jamais d'événement
        finally:
            self.ready.set()
        while True:
            await asyncio.sleep(self.interval)
            for p, kind, id_, mtime in await self._scan():
                previous = self.mtimes.get(p)
                if previous is None or mtime > previous:
                    self.mtimes[p] = mtime
                    chunk = f"data: {json.dumps({'kind': kind, 'id': id_, 'at': _now_iso()}, ensure_ascii=False)}\n\n"
                    for queue in self.subscribers:
                        queue.put_nowait(chunk)

    async def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers.add(queue)
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self._run())
        await self.ready.wait()
        if self.task.done() and not self.task.cancelled():  # le scan a échoué : l'erreur remonte au client, pas de flux muet
            self.subscribers.discard(queue)
            self.task.result()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self.subscribers.discard(queue)
        if not self.subscribers and self.task is not None:
            self.task.cancel()
            self.task = None


_SSE_HUBS: dict[int, _SseHub] = {}


async def _event_stream(config: Config) -> AsyncIterator[str]:
    """Événements temps réel (ADR-35b7 §4) : workspace/*/pipeline.json et les fichiers d'état nommés de state/, par
    mtime, sans broker ; un événement {kind, id, at} par changement, jamais pour l'état déjà vu à la connexion.
    Le scan est partagé entre clients et tourne hors de la boucle asyncio."""
    hub = _SSE_HUBS.get(id(config))
    if hub is None:
        hub = _SSE_HUBS[id(config)] = _SseHub(config)
    queue = await hub.subscribe()
    try:
        while True:
            yield await queue.get()
    finally:
        hub.unsubscribe(queue)
        if not hub.subscribers:
            _SSE_HUBS.pop(id(config), None)


# --------------------------------------------------------------------------
# Ecran Clips (SPEC-c100 E4, T4 ; SPEC-74e9 §4.5). L'API ne touche jamais un
# mp4 ni un sidecar : le texte passe par publish.edit_caption, le rendu par la
# file (worker.enqueue). Les statuts de publication sont ceux de
# SPEC-74e9 §4 ; un clip absent du fichier de publication est « à valider ».
# --------------------------------------------------------------------------

_TO_VALIDATE = "à valider"
_NOT_READY = "not_ready"
_CLIP_STATUSES = (_TO_VALIDATE, _NOT_READY, *publish_mod.VALID_STATUSES)
_RERENDER_STEPS = ["render", "qa"]


def _validate_clip_id(clip_id: str) -> None:
    if not _SAFE_ID.fullmatch(clip_id):
        raise HTTPException(status_code=400, detail=f"identifiant de clip invalide : {clip_id!r}")


def _publish_dir(config: Config) -> Path:
    return Path(config.section("publish")["state_dir"])


def _publish_entries(config: Config, channel: str | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Entrees de state/publish/<chaine>.json par (video_id, clip_id) ; un
    fichier illisible leve une 500 en francais, jamais un statut invente."""
    path = _publish_dir(config) / f"{channel or publish_mod.NO_CHANNEL}.json"  # sans chaine : file _sans_chaine
    if not path.is_file():
        return {}
    try:
        entries = _read_json(path)
        return {(e["video_id"], e["clip_id"]): e for e in entries}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=500, detail=f"fichier de publication illisible ({path.name}) : {exc}") from exc


def _clip_publish_status(sidecar: dict[str, Any], entry: dict[str, Any] | None) -> str:
    """Statut d'un clip, seule source de l'écran Clips et du tableau de bord.
    Une entrée de publication dit son statut ; sinon un clip refusé par la QA
    est « rejected » (Refusés), un clip pas encore prêt est « not_ready », et
    seul un clip prêt et accepté par la QA est « à valider »."""
    if entry:
        return entry["status"]
    if (sidecar.get("qa") or {}).get("status") == "rejected":
        return "rejected"
    return _TO_VALIDATE if sidecar.get("ready") is True else _NOT_READY


def _tiktok_fields(entry: dict[str, Any] | None, video_id: str, clip_id: str) -> dict[str, Any]:
    """Statut TikTok d'une publication (SPEC-9225 R3, R4) : ``pending`` (en attente),
    ``scheduled_on_tiktok`` / ``scheduled_on_youtube`` (programmee cote service), ``published`` (avec lien si connu),
    ``failed`` (avec capture et raison) ; None sans entree ou refusee."""
    entry = entry or {}
    status = entry.get("status")
    if entry.get("in_progress_since") and status in ("approved", "scheduled"):
        tiktok_status = "in_progress"  # le worker pilote TikTok : ni modifiable ni annulable
    elif status in ("approved", "scheduled"):
        tiktok_status = "pending"
    elif status == "published":
        scheduled = entry.get("tiktok_state") in ("scheduled_on_tiktok", "scheduled_on_youtube")
        tiktok_status = entry["tiktok_state"] if scheduled else "published"
    elif status == "failed":
        tiktok_status = "failed"
    elif status == publish_mod.REFUSED_BY_PLATFORM:
        tiktok_status = publish_mod.REFUSED_BY_PLATFORM  # refuse a la verification de contenu : liste « Refusés par TikTok »
    elif status == publish_mod.REMOVED_FROM_PLATFORM:
        tiktok_status = publish_mod.REMOVED_FROM_PLATFORM  # supprime a la main de la plateforme : liste « Supprimés »
    else:
        tiktok_status = None
    scheduled_at = _publish_entry_instant(entry, "tiktok_publish_at") if status == "published" else None
    # « en ligne » : publie pour de bon, ou programme sur TikTok dont l'heure est passee (une programmee future n'est pas publiee)
    live = status == "published" and not (str(tiktok_status).startswith("scheduled_on_") and scheduled_at is not None
                                          and scheduled_at > _now_utc())
    return {
        "tiktok_status": tiktok_status, "tiktok_live": live,
        "slot_at_paris": _paris(entry.get("slot_at")), "published_at_paris": _paris(entry.get("published_at")),
        "tiktok_publish_at_paris": _paris(entry.get("tiktok_publish_at")),
        "post_url": entry.get("post_url"), "post_id": entry.get("post_id"), "post_note": entry.get("post_note"),
        "tiktok_publish_at": entry.get("tiktok_publish_at"), "postponed_reason": entry.get("postponed_reason"),
        "account": entry.get("account"), "waiting_reason": entry.get("waiting_reason"),
        "service": entry.get("service"),
        "publish_mode": entry.get("publish_mode"), "post_options": entry.get("post_options") or {},
        "editable": status in ("approved", "scheduled", "failed") and not entry.get("in_progress_since"),
        # programmation partie sans id de post (publication-I1) : ni glissable, ni « Repasser en attente »
        "to_verify": bool(status == "failed" and entry.get("to_verify")),
        "capture_url": f"/api/publish/{video_id}/{clip_id}/capture" if entry.get("capture") else None,
    }


def _clip_video_deleted(config: Config, video_id: str, clip_id: str) -> bool:
    """Vrai si le .mp4 du clip n'existe plus (clip publie dont la video a ete supprimee, TASK-f909)."""
    return not (Path(config.output_dir) / video_id / f"{clip_id}.mp4").is_file()


def _require_clip_video(config: Config, video_id: str, clip_id: str) -> None:
    if _clip_video_deleted(config, video_id, clip_id):
        raise HTTPException(status_code=409, detail=f"vidéo supprimée : le clip {video_id}/{clip_id} n'a plus son "
                            "fichier vidéo (seules ses infos et ses stats sont gardées)")


def _stats_history_changes(history: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Historique de la fiche : un releve dont vues, likes et commentaires sont identiques au releve garde
    precedent est omis ; le dernier releve est toujours garde. Ordre conserve, valeurs inconnues restent None."""
    if history is None:
        return None
    kept: list[dict[str, Any]] = []
    last = len(history) - 1
    for index, row in enumerate(history):
        if kept and index != last and _stats_values(row) == _stats_values(kept[-1]):
            continue
        kept.append(row)
    return kept


def _stats_values(row: dict[str, Any]) -> tuple[Any, Any, Any]:
    return row.get("views"), row.get("likes"), row.get("comments")


def _clip_view(sidecar: dict[str, Any], channel: str | None, entry: dict[str, Any] | None,
               jury: dict[Any, tuple[Any, Any]] | None = None, *, video_deleted: bool = False) -> dict[str, Any]:
    """``jury`` : moment_id -> confiance du jury (voir _moments_jury_confidences) ; ``video_deleted`` : le .mp4
    a ete supprime (clip publie), le sidecar reste."""
    qa = sidecar.get("qa") or {}
    jury_confidence, jury_judges = (jury or {}).get(sidecar.get("moment_id"), (None, None))
    video_id, clip_id = sidecar["video_id"], sidecar["clip_id"]
    clip = dict(sidecar)
    clip.update({
        "channel": channel,
        "description": sidecar.get("caption"),
        "video_url": f"/media/clip/{video_id}/{clip_id}",
        "thumbnail_url": f"/media/clip/{video_id}/{clip_id}/thumbnail",
        "video_deleted": video_deleted,
        "qa_status": qa.get("status"),
        "issues": qa.get("issues"),
        "publish_status": _clip_publish_status(sidecar, entry),
        "jury_confidence": jury_confidence,
        "jury_judge_confidences": jury_judges,
        "slot_at": entry.get("slot_at") if entry else None,
        "publish_error": entry.get("error") if entry else None,
        **_tiktok_fields(entry, video_id, clip_id),
    })
    return clip


def _read_clip_sidecar(config: Config, video_id: str, clip_id: str) -> dict[str, Any]:
    path = Path(config.output_dir) / video_id / f"{clip_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"clip introuvable : {video_id}/{clip_id}")
    try:
        return _read_json(path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"sidecar illisible ({path.name}) : {exc}") from exc


def _iter_clips(config: Config, channel: str | None, video_id: str | None
                ) -> Iterator[tuple[str, str | None, dict[str, Any], dict[str, Any] | None]]:
    """(video_id, chaîne, sidecar, entrée de publication) de chaque clip de output/."""
    root = Path(config.output_dir)
    if not root.is_dir():
        return
    entries_by_channel: dict[str | None, dict[tuple[str, str], dict[str, Any]]] = {}
    for video_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if video_id is not None and video_dir.name != video_id:
            continue
        video_channel = _channel_of(video_dir.name, config)
        if channel is not None and video_channel != channel:
            continue
        if video_channel not in entries_by_channel:
            entries_by_channel[video_channel] = _publish_entries(config, video_channel)
        entries = entries_by_channel[video_channel]
        for path in sorted(video_dir.glob("*.json")):
            sidecar = _read_clip_sidecar(config, video_dir.name, path.stem)
            yield video_dir.name, video_channel, sidecar, entries.get((video_dir.name, path.stem))


def _list_clip_views(config: Config, channel: str | None, video_id: str | None,
                     status: str | None, archived: bool = True) -> list[dict[str, Any]]:
    """``archived`` faux : les clips archivés par la veille sont masqués (SPEC-bdd9 R9)."""
    clips = []
    veille_status = _veille_clip_status(config)
    jury_by_video: dict[str, dict[Any, tuple[Any, Any]]] = {}
    for video, video_channel, sidecar, entry in _iter_clips(config, channel, video_id):
        if video not in jury_by_video:
            jury_by_video[video] = _moments_jury_confidences(config, video)
        clip = _clip_view(sidecar, video_channel, entry, jury_by_video[video],
                          video_deleted=_clip_video_deleted(config, video, sidecar["clip_id"]))
        clip["veille"] = veille_status.get((video, clip["clip_id"]))
        if clip["veille"] and clip["veille"]["status"] == "archived" and not archived:
            continue
        if status is None or clip["publish_status"] == status:
            clips.append(clip)
    clips.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)  # plus récents en haut (tri stable)
    return clips


# --------------------------------------------------------------------------
# Veille (SPEC-bdd9 R9, ADR-ca9a) : le web lit state/veille/ et depose des
# demandes (refresh.json, Clipper, Ignorer, Restaurer par clipper.veille) ;
# aucun collecteur ni LLM ici (ADR-09ad).
# --------------------------------------------------------------------------

_VEILLE_SECRETS = ("twitch_client_id", "twitch_client_secret", "youtube_api_key")
_VEILLE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _veille_table(config: Config) -> dict[str, Any]:
    try:
        return veille_mod.settings(config)
    except veille_mod.VeilleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _veille_dir(config: Config) -> Path:
    return Path(str(_veille_table(config)["state_dir"]))


def _veille_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return _read_json(path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"fichier d'état de la veille illisible ({path.name}) : {exc}") from exc


def _veille_days(sdir: Path) -> list[str]:
    folder = sdir / "days"
    return sorted(p.stem for p in folder.glob("*.json") if _VEILLE_DATE.fullmatch(p.stem)) if folder.is_dir() else []


def _veille_next_run_at(table: dict[str, Any], sdir: Path) -> str | None:
    """Prochain relevé : ``run_at`` aujourd'hui s'il n'a pas eu lieu (ou est repris), sinon demain."""
    if not table["enabled"]:
        return None
    tz = ZoneInfo(str(table["timezone"]))
    now = datetime.now(tz)
    hour, minute = (int(part) for part in str(table["run_at"]).split(":"))
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day = _veille_json(sdir / "days" / f"{now.date().isoformat()}.json", None)
    if day is not None and day.get("finished_at") is not None:
        slot += timedelta(days=1)
    return slot.isoformat()


def _veille_live_state(proposal: dict[str, Any], queue: list[dict[str, Any]], config: Config) -> dict[str, str]:
    """État réel d'une proposition mise en file, lu de state/queue.json et de workspace/<id>/pipeline.json
    (lecture seule, TASK-3f90) : jamais « en file » quand la vidéo n'est plus dans la file. Le candidat porte
    l'id de la source (Twitch : « 2894103366 ») et la file / le workspace l'id Clipper (« v2894103366 ») :
    l'entrée est retrouvée par ``queue_entry_id``, sinon par l'une des deux formes."""
    raw = str(proposal["candidate"]["video_id"])
    ids = (raw, raw if raw.startswith("v") else f"v{raw}")
    entry_id = proposal.get("queue_entry_id")
    entry = next((e for e in queue if entry_id and e.get("id") == entry_id), None) \
        or next((e for e in queue if e.get("video_id") in ids), None)
    if entry is not None:
        return {"state": "running", "label": "en cours"} if entry.get("status") == "running" \
            else {"state": "queued", "label": "en file"}
    workspace = Path(config.workspace_dir)
    video_id = next((i for i in reversed(ids) if (workspace / i / pipeline.STATE_FILE).exists()), raw)
    state = _veille_json(workspace / video_id / pipeline.STATE_FILE, None)
    status = state.get("status") if isinstance(state, dict) else None
    if status == "done":
        return {"state": "done", "label": "traitée"}
    if status == "awaiting_review":
        return {"state": "awaiting_review", "label": "à relire"}
    if status == "running":  # sans entrée de file : le worker la reprend lui-même, ou elle est orpheline
        try:
            interrupted = worker_mod.is_interrupted(state, config)
        except worker_mod.WorkerError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"state": "interrupted", "label": "interrompue"} if interrupted else {"state": "running", "label": "en cours"}
    if status == "failed":
        if str(state.get("reason") or "").startswith(worker_mod._CANCEL_REASON):
            return {"state": "cancelled", "label": "annulée"}
        return {"state": "failed", "label": "échouée"}
    return {"state": "withdrawn", "label": "retirée de la file"}  # ni dans la file, ni traitée


def _veille_with_live_states(day: dict[str, Any] | None, config: Config) -> dict[str, Any] | None:
    """Copie du relevé où chaque proposition ``queued`` porte ``live`` ; le fichier du jour n'est pas touché."""
    if not day or not any(p.get("status") == "queued" for p in day.get("proposals", [])):
        return day
    queue = _veille_json(_queue_path(config), [])
    proposals = [{**p, "live": _veille_live_state(p, queue, config)} if p.get("status") == "queued" else p
                 for p in day["proposals"]]
    return {**day, "proposals": proposals}


def _veille_view(config: Config, date_: str | None) -> dict[str, Any]:
    table = _veille_table(config)
    sdir = Path(str(table["state_dir"]))
    if date_ is None:
        days = _veille_days(sdir)
        today_ = datetime.now(ZoneInfo(str(table["timezone"]))).date().isoformat()
        date_ = today_ if today_ in days else (days[-1] if days else None)
    day = _veille_json(sdir / "days" / f"{date_}.json", None) if date_ else None
    selection = _veille_json(sdir / "selection" / f"{date_}.json", None) if date_ else None
    return {
        "date": date_, "day": _veille_with_live_states(day, config), "selection": selection, "enabled": bool(table["enabled"]),
        "running": bool(day and day.get("started_at") and not day.get("finished_at")),
        "next_run_at": _veille_next_run_at(table, sdir),
        "settings": {k: v for k, v in table.items() if k not in _VEILLE_SECRETS},
        **{f"{key}_set": bool(table[key]) for key in _VEILLE_SECRETS},
    }


def _veille_clip_status(config: Config) -> dict[tuple[str, str], dict[str, str]]:
    """(video_id, clip_id) -> {date, status} d'après selection/<date>.json ; un clip hors veille est absent."""
    folder = _veille_dir(config) / "selection"
    found: dict[tuple[str, str], dict[str, str]] = {}
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        selection = _veille_json(path, {})
        for status, rows in (("kept", selection.get("kept", [])), ("archived", selection.get("archived", [])),
                             ("restored", selection.get("restored", []))):
            for row in rows:
                found[(row["video_id"], row["clip_id"])] = {"date": str(selection.get("date", path.stem)), "status": status}
    return found


def _channel_names() -> list[str]:
    return channel_mod.list_channels(_PRESETS_DIR)


class _NoChannel(HTTPException):
    """409 « la vidéo n'a pas de chaîne » : la réponse dit aussi quelles chaînes existent, pour que la console
    propose d'en attribuer une sur place (POST /api/videos/<id>/channel)."""

    def __init__(self, detail: str, video_id: str, channels: list[str]) -> None:
        super().__init__(status_code=409, detail=detail)
        self.video_id, self.channels = video_id, channels


class VeilleClipBody(BaseModel):
    channel: str | None = None
    short_clips: StrictBool | None = None


class PurgeBody(BaseModel):
    clips: bool = False  # « clips aussi » : supprime aussi output/<video_id>/


class BulkApproveClip(BaseModel):
    video_id: str
    clip_id: str


class BulkApproveBody(BaseModel):
    clips: list[BulkApproveClip]
    account: str | None = None


class _BulkApproveRefused(Exception):
    """Approbation groupee refusee en bloc (TASK-e99b, ADR-ad2e) : au moins un clip de la selection
    (parties de serie comprises) est introuvable, deja publie ou deja refuse, ou pas pret ; aucun
    n'est approuve (``refused`` : un message par clip en cause)."""

    def __init__(self, refused: list[str]) -> None:
        super().__init__("sélection refusée, rien d'approuvé")
        self.refused = refused


def _require_channel(video_id: str, clip_id: str, config: Config, *, or_no_channel: bool = False) -> str:
    """La chaîne d'une vidéo (409 sans chaîne). ``or_no_channel`` : une vidéo sans chaîne rend la file
    ``_sans_chaine`` de ses publications pilotées (SPEC-1ed3 R3 : le compte suffit) — réessayer, capture."""
    channel = _channel_of(video_id, config)
    if channel is None:
        if or_no_channel:
            return publish_mod.NO_CHANNEL
        raise _NoChannel(
            f"la vidéo {video_id} n'a pas de style : publier {clip_id} demande un style (presets/<style>.toml)",
            video_id, _channel_names())
    return channel


def _enqueue_clip_render(video_id: str, config: Config) -> dict[str, Any]:
    _require_source(video_id, "render", list(_RERENDER_STEPS), config)
    try:
        return worker_mod.enqueue(video_id, _channel_of(video_id, config), "render", list(_RERENDER_STEPS), config=config)
    except worker_mod.WorkerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Ecran Chaines (SPEC-c100 E5, SPEC-74e9 §1) : un preset de chaine est un
# fichier presets/<nom>.toml ; l'API le lit/ecrit uniquement par
# clipper.channel (save_channel : relu et valide avant remplacement).
# --------------------------------------------------------------------------

_BASE_CONFIG = "config.toml"
# Sections du formulaire : [channel], agencement, titre/CTA/badge, sous-titres,
# moments/grille. Les autres tables d'un preset sont conservees telles quelles.
_CHANNEL_FORM_SECTIONS = ("channel", "reframe", "render", "subtitles", "moments", "action")
_NEXT_SLOTS = 10
_LOGO_MAX_BYTES = 5 * _MIB
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TYPE_NAMES = {bool: "booléen", int: "entier", float: "nombre", str: "texte", list: "liste"}


def _check_preset_types(preset: dict[str, Any]) -> None:
    """Chaque valeur d'une section a le type de son defaut dans CONFIG_DEFAULTS
    (booleen, entier, nombre, texte, liste) : erreur « [section] cle : ... »
    nommant le champ, jamais une conversion silencieuse (ADR-ad2e)."""
    for section, table in preset.items():
        if not isinstance(table, dict):
            continue
        defaults = _section_defaults(section)
        for key, value in table.items():
            if key not in defaults:
                continue  # cle inconnue : refusee par load_config, qui la nomme
            kind = next((t for t in (bool, int, float, str, list) if type(defaults[key]) is t), None)
            if kind is None:
                continue
            ok = (
                isinstance(value, bool) if kind is bool
                else isinstance(value, (int, float)) and not isinstance(value, bool) if kind is float
                else isinstance(value, kind) and not isinstance(value, bool)
            )
            if not ok:
                raise ConfigError(f"[{section}] {key} : {_TYPE_NAMES[kind]} attendu, reçu {value!r}")
    timezone_name = (preset.get("channel") or {}).get("timezone")
    if timezone_name:
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"[channel] timezone : fuseau horaire inconnu ({timezone_name!r})") from exc


def _validate_preset(name: str, preset: dict[str, Any]) -> None:
    """Valide ``preset`` comme save_channel puis load_channel le feront, dans un
    dossier temporaire : le fichier reel n'est touche que si tout passe
    (SPEC-74e9 1.5). load_channel ajoute les regles de [channel] (mode,
    creneaux) que save_channel ne controle pas."""
    _check_preset_types(preset)
    with tempfile.TemporaryDirectory() as tmp:
        channel_mod.save_channel(name, preset, presets_dir=tmp, base=_BASE_CONFIG)
        channel_mod.load_channel(name, presets_dir=tmp, base=_BASE_CONFIG)


def _check_channel_name(name: str) -> None:
    if not channel_mod.NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=f"nom de style invalide : {name!r} (attendu : lettres minuscules, chiffres, _ ou -, 1 à 40 caractères)",
        )


def _channel_preset_path(name: str) -> Path:
    _check_channel_name(name)
    path = Path(_PRESETS_DIR) / f"{name}.toml"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"style inconnu : {name!r}")
    return path


def _load_channel(name: str) -> tuple[Config, dict[str, Any]]:
    _channel_preset_path(name)
    try:
        return channel_mod.load_channel(name, presets_dir=_PRESETS_DIR, base=_BASE_CONFIG)
    except channel_mod.ChannelError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _channel_detail(name: str) -> dict[str, Any]:
    """Preset brut (ce qu'il redefinit), valeurs effectives (preset > config.toml
    > CONFIG_DEFAULTS) et documentation des defauts, section par section."""
    config, channel = _load_channel(name)
    path = _channel_preset_path(name)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw.get("channel"), dict):  # SPEC-6076 R2 : ni compte ni creneaux dans un style
            raw["channel"] = {k: v for k, v in raw["channel"].items() if k not in channel_mod.LEGACY_KEYS}
        effective = {s: channel if s == "channel" else config.section(s) for s in _CHANNEL_FORM_SECTIONS}
        defaults = {s: _defaults_documentation(s) for s in _CHANNEL_FORM_SECTIONS}
    except (OSError, tomllib.TOMLDecodeError, ConfigError) as exc:
        raise HTTPException(status_code=422, detail=f"preset illisible ({path.name}) : {exc}") from exc
    return {"name": name, "raw": raw, "effective": effective, "defaults": defaults,
            "rubric": _rubric_info(effective["moments"]["rubric_path"])}


def _rubric_info(value: str) -> dict[str, str]:
    """Libelle de la grille designee par une valeur de [moments] rubric_path :
    « Standard (<valeur>) », « Gaming (<valeur>) » ou « Gaming action (<valeur>) » pour une grille embarquee
    ou un fichier dont le contenu est identique a celle-ci, sinon « Fichier
    personnalise (<valeur>) » (kind « custom ») ; une valeur « builtin:... »
    inconnue est « invalid », jamais ramenee a la grille standard (ADR-ad2e)."""
    try:
        path = moments_mod.resolve_rubric_path(value)
    except moments_mod.MomentsError as exc:
        return {"value": value, "kind": "invalid", "label": str(exc)}
    if value in moments_mod._BUILTIN_RUBRICS:
        kind = {"builtin:gaming": "gaming", "builtin:gaming-action": "gaming-action"}.get(value, "standard")
    else:
        try:
            content = path.read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            return {"value": value, "kind": "custom", "label": f"Fichier personnalisé ({value}) : fichier introuvable"}
        kind = next(
            (name for name, builtin in (("standard", "builtin"), ("gaming", "builtin:gaming"), ("gaming-action", "builtin:gaming-action"))
             if moments_mod.resolve_rubric_path(builtin).read_bytes().replace(b"\r\n", b"\n") == content),
            "custom",
        )
    names = {"standard": "Standard", "gaming": "Gaming", "gaming-action": "Gaming action", "custom": "Fichier personnalisé"}
    return {"value": value, "kind": kind, "label": f"{names[kind]} ({value})"}


def _subspreview_config(name: str, draft: str | None) -> Config:
    """Config effective de la chaine pour l'apercu des sous-titres. Avec
    ``draft`` (JSON ``{"subtitles": {...}, "reframe": {...}}`` : les tables du
    formulaire pas encore enregistrees), le preset est recompose avec ces
    tables puis relu par save_channel/load_channel dans un dossier temporaire
    (heritage compris, comme a l'enregistrement) ; le fichier reel n'est
    jamais touche."""
    config, _channel = _load_channel(name)
    if not draft:
        return config
    try:
        tables = json.loads(draft)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"draft : JSON invalide ({exc})") from exc
    if not isinstance(tables, dict) or any(not isinstance(v, dict) for v in tables.values()):
        raise HTTPException(status_code=422, detail="draft : attendu un objet {section: {cle: valeur}}")
    if set(tables) - {"subtitles", "reframe"}:
        raise HTTPException(status_code=422, detail="draft : seules les sections subtitles et reframe sont admises")
    preset = tomllib.loads(_channel_preset_path(name).read_text(encoding="utf-8"))
    for section, table in tables.items():
        if table:
            preset[section] = table
        else:
            preset.pop(section, None)
    try:
        _check_preset_types(preset)
        with tempfile.TemporaryDirectory() as tmp:
            channel_mod.save_channel(name, preset, presets_dir=tmp, base=_BASE_CONFIG)
            return channel_mod.load_channel(name, presets_dir=tmp, base=_BASE_CONFIG)[0]
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _refuse_legacy_keys(preset: dict[str, Any]) -> None:
    """Un style n'a ni compte de publication ni creneaux (SPEC-6076 R2) : un corps qui en porte est refuse."""
    found = channel_mod.legacy_keys(preset.get("channel"))
    if found:
        raise HTTPException(
            status_code=422,
            detail=f"[channel] {' et '.join(found)} : n'existe plus dans un style (le compte de publication se "
                   "choisit à chaque publication, les créneaux se règlent sur le compte dans l'écran Comptes)")


def _save_channel_preset(name: str, preset: dict[str, Any]) -> None:
    if "channel" not in preset:
        preset = {**preset, "channel": {}}  # sans [channel], le preset ne serait plus une chaine
    try:
        _validate_preset(name, preset)
        channel_mod.save_channel(name, preset, presets_dir=_PRESETS_DIR, base=_BASE_CONFIG)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Ecran Reglages (SPEC-c100 E8, ADR-35b7 §2 et §5) : config.toml en formulaire.
# L'ecriture passe par config.write_config (relu par load_config avant le
# remplacement atomique) ; le jeton [web] token n'est jamais lu ni ecrit par
# l'interface : il se change dans le fichier, puis redemarrage.
# --------------------------------------------------------------------------

_SETTINGS_FLAT = tuple(_CONFIG_FLAT_DEFAULTS)
_SETTINGS_SECTIONS = ("llm", "web", "worker", "network", "veille")
_SETTINGS_TOKEN_MASK = "•" * 8
_SETTINGS_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'')


def _settings_read_raw() -> tuple[dict[str, Any], bool, str]:
    path = Path(_BASE_CONFIG)
    if not path.is_file():
        return {}, False, ""
    try:
        text = path.read_text(encoding="utf-8")
        return tomllib.loads(text), True, text
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"{_BASE_CONFIG} illisible : {exc}") from exc


def _settings_has_comments(text: str) -> bool:
    return any("#" in _SETTINGS_QUOTED.sub("", line) for line in text.splitlines())


def _settings_without_token(web: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in web.items() if k != "token"}


def _settings_veille_masked(veille: dict[str, Any]) -> dict[str, Any]:
    """[veille] sans la valeur des clés : seulement ``<clé>_set`` (SPEC-bdd9 R8)."""
    out = {k: v for k, v in veille.items() if k not in _VEILLE_SECRETS}
    out.update({f"{key}_set": bool(veille.get(key)) for key in _VEILLE_SECRETS})
    return out


def _settings_access(web_cfg: dict[str, Any], file_web: dict[str, Any]) -> dict[str, Any]:
    """Acces tel que le serveur en cours l'applique (lecture seule) : l'hote et
    le port ne changent qu'au redemarrage, le jeton n'est jamais renvoye.
    ``config_host`` / ``config_port`` sont ceux de config.toml, signales quand
    ils different de ceux du serveur (``serve --host/--port``)."""
    host, port, token = str(web_cfg["host"]), int(web_cfg["port"]), str(web_cfg["token"])
    command = f"python -m clipper serve --port {port}"
    if host != _LOOPBACK_HOST:
        command = f"python -m clipper serve --host {host} --port {port}"
    return {
        "host": host, "port": port, "loopback": host == _LOOPBACK_HOST,
        "token_set": bool(token), "token": _SETTINGS_TOKEN_MASK if token else None, "command": command,
        "config_host": str(file_web["host"]), "config_port": int(file_web["port"]),
        "differs_from_config": (host, port) != (str(file_web["host"]), int(file_web["port"])),
    }


def _settings_detail(running_web: dict[str, Any]) -> dict[str, Any]:
    """Valeurs effectives de config.toml (relu a chaque appel), brut du fichier
    et CONFIG_DEFAULTS commentes des sections du formulaire."""
    raw, exists, text = _settings_read_raw()
    try:
        config = load_config(_BASE_CONFIG) if exists else load_config()
        effective: dict[str, Any] = {"mode": config.mode, "workspace_dir": str(config.workspace_dir),
                                     "output_dir": str(config.output_dir)}
        defaults: dict[str, Any] = {
            "general": {k: {"default": v, "comment": "", "details": ""} for k, v in _CONFIG_FLAT_DEFAULTS.items()},
        }
        from clipper import llm as llm_mod

        for section in _SETTINGS_SECTIONS:
            # [llm] est fusionne en profondeur avec ses defauts par clipper.llm : meme vue ici
            effective[section] = llm_mod._settings(config) if section == "llm" else config.section(section)
            defaults[section] = _defaults_documentation(section)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=f"{_BASE_CONFIG} invalide : {exc}") from exc
    file_web = effective["web"]
    effective["web"] = _settings_without_token(file_web)
    defaults["web"].pop("token", None)
    effective["veille"] = _settings_veille_masked(effective["veille"])
    for key in _VEILLE_SECRETS:
        defaults["veille"].pop(key, None)
    if "web" in raw:
        raw = {**raw, "web": _settings_without_token(raw["web"])}
    if "veille" in raw:
        raw = {**raw, "veille": _settings_veille_masked(raw["veille"])}
    restart = any(file_web[k] != running_web[k] for k in ("host", "port", "token"))
    return {
        "path": _BASE_CONFIG, "exists": exists, "comments_lost": _settings_has_comments(text),
        "raw": raw, "effective": effective, "defaults": defaults,
        "modes": list(VALID_MODES), "backends": list(llm_mod._BACKENDS),
        "access": _settings_access(running_web, file_web), "restart_required": restart,
    }


def _settings_check_llm(llm: dict[str, Any]) -> None:
    from clipper import llm as llm_mod

    known = " | ".join(llm_mod._BACKENDS)

    def backend(value: Any, where: str) -> None:
        if value not in llm_mod._BACKENDS:
            raise ConfigError(f"[llm] {where} : backend inconnu {value!r} (attendu : {known})")

    if "backend" in llm:
        backend(llm["backend"], "backend")
    usages = llm.get("usages", {})
    if not isinstance(usages, dict):
        raise ConfigError("[llm] usages : table attendue")
    for usage, table in usages.items():
        if not isinstance(table, dict) or not all(isinstance(v, str) for v in table.values()):
            raise ConfigError(f"[llm] usages.{usage} : table de textes attendue (backend, model)")
        unknown = set(table) - {"backend", "model"}
        if unknown:
            raise ConfigError(f"[llm] usages.{usage} : cle(s) inconnue(s) {', '.join(sorted(unknown))}")
        if "backend" in table:
            backend(table["backend"], f"usages.{usage}.backend")
    for key, value in llm.items():
        if isinstance(_section_defaults("llm").get(key), dict) and key != "usages":
            models = value.get("models", {}) if isinstance(value, dict) else None
            if not isinstance(models, dict) or not all(isinstance(m, str) and m for m in models.values()):
                raise ConfigError(f"[llm] {key}.models : table niveau -> nom de modele (textes non vides) attendue")


def _settings_merge(raw: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Ce que le formulaire controle (mode, dossiers, [llm], [web], [worker])
    remplace le fichier ; le reste (autres sections) et le jeton sont conserves."""
    unknown = set(settings) - set(_SETTINGS_FLAT) - set(_SETTINGS_SECTIONS)
    if unknown:
        raise ConfigError(
            f"{', '.join(sorted(unknown))} : non modifiable depuis les réglages "
            f"(éditable : {', '.join((*_SETTINGS_FLAT, *_SETTINGS_SECTIONS))})"
        )
    for key in _SETTINGS_FLAT:
        if key in settings and not (isinstance(settings[key], str) and settings[key].strip()):
            raise ConfigError(f"{key} : texte non vide attendu, reçu {settings[key]!r}")
    for section in _SETTINGS_SECTIONS:
        if section in settings and not isinstance(settings[section], dict):
            raise ConfigError(f"[{section}] : table attendue")
    _check_preset_types({s: settings[s] for s in _SETTINGS_SECTIONS if s in settings})
    web = settings.get("web", {})
    if "token" in web:
        raise ConfigError("[web] token : le jeton ne se modifie pas depuis l'interface (fichier + redémarrage)")
    if "llm" in settings:
        _settings_check_llm(settings["llm"])
    country = settings.get("network", {}).get("expected_country")
    if country is not None and not (isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country)):
        raise ConfigError(f"[network] expected_country : code pays ISO à deux lettres attendu (FR, GB...), reçu {country!r}")
    port = web.get("port")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"[web] port : entre 1 et 65535 attendu, reçu {port!r}")
    data = {k: v for k, v in raw.items()}
    for key in _SETTINGS_FLAT:
        if key in settings:
            data[key] = settings[key]
    for section in _SETTINGS_SECTIONS:
        if section in settings:
            data[section] = dict(settings[section])
    token = raw.get("web", {}).get("token", "")
    if "web" in settings and token:
        data["web"]["token"] = token
    if "veille" in settings:  # les clés absentes du corps sont gardées (SPEC-bdd9 R8)
        for key in _VEILLE_SECRETS:
            if key not in settings["veille"] and key in raw.get("veille", {}):
                data["veille"][key] = raw["veille"][key]
    final_web = data.get("web", {})
    if final_web.get("host", _section_defaults("web")["host"]) != _LOOPBACK_HOST and not final_web.get("token"):
        raise ConfigError(
            "[web] host hors bouclage : un jeton ([web] token) est exigé, à écrire dans config.toml "
            "(sinon 'serve' refuserait de démarrer, ADR-35b7 §5)"
        )
    return data


class SettingsBody(BaseModel):
    settings: dict[str, Any]

# Editeur d'agencement stream split (SPEC-c100 E5, SPEC-76dc) : memes cles et
# memes validations que [reframe] (c'est reframe qui refuse, pas le JS).
# L'image cle est un fichier deja produit par l'etape scenes.
# --------------------------------------------------------------------------

_LAYOUT_KEYS = ("split_webcam_dest", "split_gameplay_dest", "badge_dest", "split_subtitle_dest")
_LAYOUT_KEYFRAME_DIRS = ("frames", "scenes")  # frames/ : sortie de clipper.scenes


def _layout_keyframes(config: Config, video_id: str) -> list[Path]:
    video_dir = Path(config.workspace_dir) / video_id
    return sorted(p for d in _LAYOUT_KEYFRAME_DIRS for p in (video_dir / d).glob("*.jpg") if p.is_file())


def _layout_keyframe_path(config: Config, name: str, video_id: str | None) -> Path:
    """Image cle du milieu d'une video de la chaine (celle demandee, sinon la
    premiere qui en a) : 404 en francais si aucune n'existe."""
    if video_id is not None:
        if not _SAFE_ID.fullmatch(video_id):
            raise HTTPException(status_code=404, detail=f"identifiant invalide : {video_id!r}")
        if _channel_of(video_id, config) != name:
            raise HTTPException(status_code=404, detail=f"la vidéo {video_id!r} n'appartient pas au style {name!r}")
        frames = _layout_keyframes(config, video_id)
        if not frames:
            raise HTTPException(status_code=404, detail=f"aucune image clé pour la vidéo {video_id!r} (étape scenes non faite)")
        return frames[len(frames) // 2]
    for state in sorted(_list_states(config), key=lambda s: s["video_id"]):
        if state.get("channel") == name and _SAFE_ID.fullmatch(state["video_id"]):
            frames = _layout_keyframes(config, state["video_id"])
            if frames:
                return frames[len(frames) // 2]
    raise HTTPException(
        status_code=404,
        detail=f"aucune image clé : aucune vidéo du style {name!r} n'a passé l'étape scenes",
    )


def _layout_view(name: str) -> dict[str, Any]:
    """Rectangles effectifs (preset > config.toml > defauts SPEC-76dc), defauts,
    canevas et zone sure, tels que reframe les lit."""
    config, _channel = _load_channel(name)
    reframe = config.section("reframe")
    defaults = _section_defaults("reframe")
    split = reframe["layout"] == "stream_auto" and reframe["stream_variant"] == "split"
    return {
        "name": name,
        # editeur a ouvrir : split (ci-dessous), letterbox (/layout/letterbox) ; crop n'en a pas
        "mode": "split" if split else "letterbox" if reframe["format"] == "letterbox" else "crop",
        **{key: reframe[key] for key in _LAYOUT_KEYS},
        "badge_enabled": bool(config.section("render")["badge_enabled"]),
        "defaults": {key: defaults[key] for key in _LAYOUT_KEYS},
        "canvas": {"w": reframe["output_width"], "h": reframe["output_height"]},
        "safe": {"left": reframe["safe_left"], "top": reframe["safe_top"],
                 "right": reframe["safe_right"], "bottom": reframe["safe_bottom"]},
    }


def _layout_check_geometry(name: str, preset: dict[str, Any]) -> None:
    """Fait relire ``preset`` (stream_variant force a "split") par reframe :
    load_config ne controle que les cles, c'est reframe._settings qui refuse un
    rectangle hors canevas, chevauchant ou hors zone sure. Son message est
    renvoye tel quel (ReframeError)."""
    candidate = {**preset, "reframe": {**preset["reframe"], "stream_variant": "split"}}
    _check_preset_types(candidate)
    with tempfile.TemporaryDirectory() as tmp:
        channel_mod.save_channel(name, candidate, presets_dir=tmp, base=_BASE_CONFIG)
        config, _channel = channel_mod.load_channel(name, presets_dir=tmp, base=_BASE_CONFIG)
    try:
        reframe_mod._settings(config)
    except reframe_mod.ReframeError as exc:
        raise ConfigError(str(exc)) from exc


def _layout_save(name: str, body: dict[str, Any]) -> None:
    """Ecrit les rectangles dans [reframe] du preset par save_channel, apres
    avoir verifie la geometrie comme reframe la verifiera au rendu : l'editeur
    ne laisse jamais passer un agencement que reframe refuserait."""
    given = {key: body[key] for key in _LAYOUT_KEYS if body.get(key) is not None}
    badge_enabled = body.get("badge_enabled")
    if not given and badge_enabled is None:
        raise HTTPException(
            status_code=422,
            detail=f"aucun rectangle à enregistrer (attendu : {', '.join(_LAYOUT_KEYS)}, ou badge_enabled)",
        )
    path = _channel_preset_path(name)
    try:
        preset = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"preset illisible ({path.name}) : {exc}") from exc
    preset["reframe"] = {**preset.get("reframe", {}), **given}
    if badge_enabled is not None:
        # seul [render] badge_enabled change : badge_dest et le reste du preset restent tels quels
        preset["render"] = {**preset.get("render", {}), "badge_enabled": badge_enabled}
    try:
        _layout_check_geometry(name, preset)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _save_channel_preset(name, preset)


# Editeur d'agencement letterbox (TASK-3be3) : reglages existants du rendu
# letterbox, par style, sur la base du « standard » (config.toml puis
# CONFIG_DEFAULTS, sans le preset). Le preset ne garde que ce qui differe du
# standard ; reframe et render refusent, leur message est renvoye tel quel.
_LETTERBOX_KEYS = {
    "letterbox_top": "reframe",
    "letterbox_zoom": "reframe",
    "letterbox_title_dest": "reframe",
    "letterbox_subtitle_dest": "reframe",
    "cta_handle_gap": "render",
}
# Source de reference pour verifier, a l'enregistrement, que la video nette
# ne recouvre pas une zone de texte (reframe le reverifie sur la vraie source).
_LETTERBOX_REFERENCE_SOURCE = (1920, 1080)


def _letterbox_standard() -> dict[str, Any]:
    base = Path(_BASE_CONFIG)
    config = load_config(base) if base.is_file() else load_config()
    return {key: config.section(section)[key] for key, section in _LETTERBOX_KEYS.items()}


def _letterbox_view(name: str) -> dict[str, Any]:
    config, _channel = _load_channel(name)
    preset = tomllib.loads(_channel_preset_path(name).read_text(encoding="utf-8"))
    reframe, render = config.section("reframe"), config.section("render")
    return {
        "name": name,
        "values": {key: config.section(section)[key] for key, section in _LETTERBOX_KEYS.items()},
        "standard": _letterbox_standard(),
        "overridden": [key for key, section in _LETTERBOX_KEYS.items() if key in preset.get(section, {})],
        "canvas": {"w": reframe["output_width"], "h": reframe["output_height"]},
        "safe": {"left": reframe["safe_left"], "top": reframe["safe_top"],
                 "right": reframe["safe_right"], "bottom": reframe["safe_bottom"]},
        "text_gap": reframe["text_gap"],
        "part_height": reframe["part_height"],
        "title_lift": render["title_lift"],
        "title_enabled": render["title_enabled"],
        "cta": {"enabled": render["cta_enabled"], "handle": render["cta_handle"],
                "font_size": render["cta_handle_font_size"]},
    }


def _letterbox_check(name: str, preset: dict[str, Any]) -> None:
    """Fait relire ``preset`` par reframe (zones dans le canevas et la zone
    sure, video nette dans le cadre sans recouvrir le texte pour la source de
    reference) et par render (ecart du pseudo) ; leur message tel quel."""
    _check_preset_types(preset)
    with tempfile.TemporaryDirectory() as tmp:
        channel_mod.save_channel(name, preset, presets_dir=tmp, base=_BASE_CONFIG)
        config, _channel = channel_mod.load_channel(name, presets_dir=tmp, base=_BASE_CONFIG)
    try:
        settings = reframe_mod._settings(config)
        reframe_mod._letterbox_geometry(*_LETTERBOX_REFERENCE_SOURCE, settings)
        render_mod.check_cta_handle_gap(render_mod._settings(config))
    except (reframe_mod.ReframeError, render_mod.RenderError) as exc:
        raise ConfigError(str(exc)) from exc


def _letterbox_write(name: str, body: dict[str, Any] | None) -> None:
    """``body`` (cles de _LETTERBOX_KEYS) : une valeur egale au standard est
    retiree du preset (de nouveau heritee), une autre y est ecrite ; None
    retire toutes les cles de l'editeur (« Revenir au standard »)."""
    path = _channel_preset_path(name)
    _load_channel(name)  # 404 si le style n'existe pas
    try:
        preset = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"preset illisible ({path.name}) : {exc}") from exc
    standard = _letterbox_standard()
    for key, section in _LETTERBOX_KEYS.items():
        table = dict(preset.get(section, {}))
        if body is None or (key in body and body[key] == standard[key]):
            table.pop(key, None)
        elif key in body:
            table[key] = body[key]
        else:
            continue
        if table:
            preset[section] = table
        else:
            preset.pop(section, None)
    try:
        _letterbox_check(name, preset)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _save_channel_preset(name, preset)


def _png_from_multipart(content_type: str, body: bytes) -> bytes:
    """Contenu du champ « file » d'un POST multipart (stdlib, sans dependance)."""
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(status_code=422, detail="envoi multipart/form-data attendu (champ « file », image PNG)")
    message = BytesParser(policy=_EMAIL_HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    for part in message.iter_parts() if message.is_multipart() else []:
        if part.get_param("name", header="content-disposition") == "file":
            data = part.get_payload(decode=True) or b""
            if not data.startswith(_PNG_SIGNATURE):
                raise HTTPException(status_code=422, detail="le logo doit être une image PNG")
            return data
    raise HTTPException(status_code=422, detail="champ « file » absent de l'envoi multipart")


# ----------------------------------------------------------------
# Statistiques (SPEC-c100 E7, TASK-7d86)
# ----------------------------------------------------------------

_STATS_STEP_ORDER = tuple(pipeline.STEPS)
# Valeur de ?channel= pour « Sans chaine » (video sans chaine) ; absent = toutes les chaines.
_STATS_NO_CHANNEL = "__none__"


def _stats_channel_ok(channel: str | None, wanted: str | None) -> bool:
    if wanted is None:
        return True
    return channel is None if wanted == _STATS_NO_CHANNEL else channel == wanted


def _stats_bound(name: str, value: str | None, *, end_of_day: bool) -> datetime | None:
    """Borne de periode « AAAA-MM-JJ » ou horodatage ISO 8601 (UTC si sans fuseau)."""
    if not value:
        return None
    try:
        bound = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"date invalide pour {name} : {value!r} (attendu AAAA-MM-JJ ou horodatage ISO 8601)",
        ) from exc
    if end_of_day and len(value) == 10:
        bound = bound.replace(hour=23, minute=59, second=59, microsecond=999999)
    return bound if bound.tzinfo else bound.replace(tzinfo=timezone.utc)


def _stats_period(since: str | None, until: str | None) -> tuple[datetime | None, datetime | None]:
    lower = _stats_bound("since", since, end_of_day=False)
    upper = _stats_bound("until", until, end_of_day=True)
    if lower is not None and upper is not None and lower > upper:
        raise HTTPException(status_code=422, detail=f"periode inversee : since={since!r} est apres until={until!r}")
    return lower, upper


def _stats_in_period(stamp: str | None, lower: datetime | None, upper: datetime | None, where: str) -> bool:
    if lower is None and upper is None:
        return True
    if not stamp:
        raise HTTPException(status_code=500, detail=f"horodatage absent ({where}) : impossible de le placer dans la periode")
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"horodatage illisible ({where}) : {stamp!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (lower is None or moment >= lower) and (upper is None or moment <= upper)


def _stats_read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    entries = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entries.append(json.loads(raw))
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=f"{path.name} ligne {number} illisible : {exc}") from exc
    return entries


def _stats_llm_cost(config: Config, lower: datetime | None, upper: datetime | None,
                    wanted: str | None = None) -> dict[str, Any]:
    """Couts de workspace/*/llm_usage.jsonl dans la periode : par video, par usage
    et par jour (UTC) ; les appels sans cout rapporte sont comptes a part."""
    cost: dict[str, Any] = {"total": 0.0, "unreported_calls": 0, "by_video": {}, "by_usage": {}, "by_day": {}}
    root = Path(config.workspace_dir)
    for path in sorted(root.glob("*/llm_usage.jsonl")) if root.is_dir() else []:
        video_id = path.parent.name
        if not _stats_channel_ok(_channel_of(video_id, config), wanted):
            continue
        for number, entry in enumerate(_stats_read_jsonl(path), start=1):
            where = f"{video_id}/{path.name} ligne {number}"
            stamp = entry.get("recorded_at") or entry.get("timestamp")
            if not _stats_in_period(stamp, lower, upper, where):
                continue
            try:
                usage = entry["usage"]
                day = datetime.fromisoformat(stamp).astimezone(timezone.utc).date().isoformat()
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=500, detail=f"{where} : {exc}") from exc
            video = cost["by_video"].setdefault(video_id, {"cost": 0.0, "calls": 0, "unreported_calls": 0})
            video["calls"] += 1
            amount = entry.get("cost_usd")
            if amount is None:
                video["unreported_calls"] += 1
                cost["unreported_calls"] += 1
                continue
            video["cost"] += amount
            cost["total"] += amount
            cost["by_usage"][usage] = cost["by_usage"].get(usage, 0.0) + amount
            cost["by_day"][day] = cost["by_day"].get(day, 0.0) + amount
    cost["by_day"] = dict(sorted(cost["by_day"].items()))
    return cost


def _stats_steps_and_counts(config: Config, lower: datetime | None, upper: datetime | None,
                            wanted: str | None = None) -> dict[str, Any]:
    """Videos dont ``updated_at`` tombe dans la periode : comptes par statut, et
    duree moyenne / derniere (fin la plus recente) de chaque etape sur les videos done."""
    counts = {status: 0 for status in _VIDEO_STATUSES}
    samples: dict[str, list[tuple[str, float]]] = {}
    for state in _list_states(config):
        video_id = state.get("video_id", "?")
        if not _stats_channel_ok(state.get("channel"), wanted):
            continue
        if not _stats_in_period(state.get("updated_at"), lower, upper, f"pipeline.json de {video_id}"):
            continue
        status = state.get("status")
        if status not in counts:
            raise HTTPException(status_code=500, detail=f"statut inconnu {status!r} dans le pipeline.json de {video_id}")
        counts[status] += 1
        if status != "done":
            continue
        for name, seconds in _step_durations(state).items():
            if seconds is not None:
                samples.setdefault(name, []).append((state["steps"][name]["finished_at"], seconds))
    steps = {}
    for name in sorted(samples, key=lambda n: _STATS_STEP_ORDER.index(n) if n in _STATS_STEP_ORDER else len(_STATS_STEP_ORDER)):
        durations = [seconds for _, seconds in samples[name]]
        steps[name] = {"mean_s": sum(durations) / len(durations),
                       "last_s": max(samples[name], key=lambda s: _parse_ts("?", name, "finished_at", s[0]))[1],
                       "count": len(durations)}
    return {"steps": steps, "counts": counts}


def _measures(config: Config, since: str | None, until: str | None, channel: str | None = None) -> dict[str, Any]:
    """Mesures internes de Clipper (couts du modele de langage, duree par etape, videos par statut), pour le
    Tableau de bord : elles ne viennent pas de TikTok et ne sont plus dans l'ecran Statistiques (SPEC-86fe R1)."""
    lower, upper = _stats_period(since, until)
    wanted = channel or None
    return {"period": {"since": since or None, "until": until or None}, "channel": wanted,
            "llm_cost": _stats_llm_cost(config, lower, upper, wanted),
            **_stats_steps_and_counts(config, lower, upper, wanted)}


# ----------------------------------------------------------------
# Statistiques TikTok par compte (SPEC-86fe) : uniquement le releve de TikTok Studio
# ----------------------------------------------------------------


def _stats_account_info(config: Config, account_id: str) -> dict[str, Any]:
    """Un compte de l'ecran Statistiques : son etat de releve ; ``HTTPException`` 404
    si le compte n'existe pas dans l'ecran Comptes. Un compte non pret n'est pas releve (R4) : la raison est dite."""
    found = next((a for a in _accounts_call(accounts_mod.list_accounts, config) if a["id"] == account_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail=f"compte introuvable : {account_id!r}")
    ready = _stats_relevable(found)
    reason = None if ready else ((accounts_mod.connection_blocked_reason(found) if found.get("paused_at") else None)
                                 or accounts_mod.ready_blocked_reason(found) or found.get("ready_note")
                                 or "le compte n'est pas coché « prêt à publier »")
    return {"account": account_id, "label": found.get("label") or account_id,
            "ready": ready, "not_ready_reason": reason, "paused_at": found.get("paused_at")}


def _stats_relevable(found: dict[str, Any]) -> bool:
    """Un compte est relevable s'il est « pret a publier » ou en pause manuelle avec une connexion verifiee et sans
    arret R4 : la pause n'arrete que la publication, pas les statistiques (SPEC-f348 R7.5)."""
    if found.get("ready_to_publish"):
        return True
    return bool(found.get("paused_at")) and accounts_mod.connection_blocked_reason(found) is None


def _stats_tiktok_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Lecture de l'historique des releves : un fichier illisible ou un parametre invalide est une erreur nette."""
    try:
        return fn(*args, **kwargs)
    except tiktok_mod.TikTokError as exc:
        message = str(exc)
        status = 404 if "introuvable" in message else 422 if ("invalide" in message or "inconnu" in message) else 500
        raise HTTPException(status_code=status, detail=message) from exc


class _StatsRefreshes:
    """Releves TikTok en cours, un par compte au plus (SPEC-47e2 R4) : ``claim`` rend faux quand un releve du
    compte tourne deja (bouton, ouverture de l'ecran, deux onglets) : on renvoie alors l'etat en cours, rien n'est
    relance. ``failed`` garde le dernier echec d'un releve en tache de fond (jamais perdu, ADR-ad2e)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._failed: dict[str, str] = {}

    def claim(self, account: str) -> bool:
        with self._lock:
            if account in self._running:
                return False
            self._running.add(account)
            self._failed.pop(account, None)
            return True

    def release(self, account: str, failure: str | None = None) -> None:
        with self._lock:
            self._running.discard(account)
            if failure is not None:
                self._failed[account] = failure

    def running(self, account: str) -> bool:
        with self._lock:
            return account in self._running

    def failure(self, account: str) -> str | None:
        with self._lock:
            return self._failed.get(account)


def _stats_body_account(raw: bytes, *, required: bool) -> str | None:
    """Corps ``{"account": id}`` des routes de releve : 422 s'il est invalide ; sans corps, ``None`` (sauf ``required``)."""
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="corps JSON invalide") from exc
    if not isinstance(body, dict) or set(body) - {"account"} or (required and "account" not in body):
        raise HTTPException(status_code=422, detail="corps invalide : {\"account\": id} " + ("attendu" if required else "ou aucun corps attendu"))
    try:
        return browser_mod.validate_account(body["account"]) if "account" in body else None
    except browser_mod.BrowserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _stats_refresh_body(raw: bytes) -> tuple[str | None, bool]:
    """Corps de ``POST /api/stats/tiktok/refresh`` : ``{"account": id, "full": bool}`` (``full`` facultatif : releve
    sans plafond du detail, « Releve complet »). 422 si ``full`` n'est pas un booleen."""
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="corps JSON invalide") from exc
    full = body.pop("full", False) if isinstance(body, dict) else False
    if not isinstance(full, bool):
        raise HTTPException(status_code=422, detail=f"full : un booléen est attendu, reçu {full!r}")
    return _stats_body_account(json.dumps(body).encode(), required=False), full


class ChannelBody(BaseModel):
    preset: dict[str, Any]


class ChannelCreateBody(BaseModel):
    name: str
    preset: dict[str, Any] | None = None


class ClipPatchBody(BaseModel):
    description: str | None = None
    hashtags: list[str] | None = None
    screen_title: str | None = None
    confirm: bool = False


class SubmitBody(BaseModel):
    url: str


class QueueBody(BaseModel):
    url: str
    channel: str | None = None
    action: str
    force_steps: list[str] = []
    short_clips: bool | None = None  # clips courts : None = valeur du style (TASK-4f5e)


class AssignChannelBody(BaseModel):
    channel: str


class RetryBody(BaseModel):
    from_step: str


class DecideBody(BaseModel):
    decision: str
    start: float | None = None
    end: float | None = None
    comment: str | None = None


class LayoutBody(BaseModel):
    split_webcam_dest: dict[str, Any] | None = None
    split_gameplay_dest: dict[str, Any] | None = None
    badge_dest: dict[str, Any] | None = None
    split_subtitle_dest: dict[str, Any] | None = None
    badge_enabled: StrictBool | None = None


class LetterboxLayoutBody(BaseModel):
    model_config = {"extra": "forbid"}

    letterbox_top: int | None = None
    letterbox_zoom: float | None = None
    letterbox_title_dest: dict[str, Any] | None = None
    letterbox_subtitle_dest: dict[str, Any] | None = None
    cta_handle_gap: int | None = None


# --------------------------------------------------------------------------
# Comptes (SPEC-6fa4) : carnet local, mots de passe dans le coffre de l'OS
# (clipper.accounts). Routes /api/accounts* reservees au PC (R3), sans CORS,
# ecritures en JSON seulement ; le corps est lu a la main pour qu'aucune
# erreur de validation ne recopie un mot de passe (R4).
# --------------------------------------------------------------------------

_ACCOUNTS_PREFIX = "/api/accounts"
_ACCOUNTS_HOSTS = ("127.0.0.1", "localhost")
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _is_accounts_path(path: str) -> bool:
    return path == _ACCOUNTS_PREFIX or path.startswith(_ACCOUNTS_PREFIX + "/")


def _is_loopback_client(host: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return (mapped or addr).is_loopback


_LOCAL_HOST_HEADER = re.compile(r"(?:127\.0\.0\.1|localhost)(?::\d{1,5})?", re.IGNORECASE)


def _is_local_host_header(value: str | None) -> bool:
    return bool(value) and _LOCAL_HOST_HEADER.fullmatch(value) is not None


def _accounts_guard(request: Request) -> JSONResponse | None:
    """Refus 403 hors PC (adresse non bouclage ou Host etranger, meme avec jeton), 415 sans JSON."""
    client = request.client.host if request.client else None
    if not _is_loopback_client(client):
        return JSONResponse(
            {"detail": "les comptes ne sont accessibles que depuis le PC qui héberge la console (adresse cliente hors bouclage)"},
            status_code=403,
        )
    if not _is_local_host_header(request.headers.get("host")):
        return JSONResponse(
            {"detail": "les comptes ne sont accessibles que via 127.0.0.1 ou localhost (en-tête Host refusé)"},
            status_code=403,
        )
    if request.method in _WRITE_METHODS and request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return JSONResponse({"detail": "corps JSON exigé (Content-Type: application/json)"}, status_code=415)
    return None


async def _accounts_json(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="corps JSON invalide") from exc


def _accounts_call(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except accounts_mod.AccountNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except accounts_mod.VaultUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except accounts_mod.AccountsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None


def _browser_state(account_id: str, config: Config | None = None) -> dict[str, Any]:
    """Etat du profil de navigateur d'un compte : absent / present + date (SPEC-9225 R1)."""
    try:
        return browser_mod.profile_status(account_id, config)
    except browser_mod.BrowserError as exc:
        return {"present": False, "modified_at": None, "error": str(exc)}


def _require_account(config: Config, account_id: str) -> None:
    known = {a["id"] for a in _accounts_call(accounts_mod.list_accounts, config)}
    if account_id not in known:
        raise HTTPException(status_code=404, detail=f"compte introuvable : {account_id!r}")


def _find_account(config: Config, account_id: str) -> dict[str, Any]:
    account = next((a for a in _accounts_call(accounts_mod.list_accounts, config) if a["id"] == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail=f"compte introuvable : {account_id!r}")
    return account


def _browser_login(config: Config, account_id: str, url: str | None) -> None:
    """« Se connecter » : Chrome normal sur le profil. Un compte YouTube s'ouvre sur YouTube Studio (R1) et
    est verifie a la fermeture de la fenetre (chaine visible = pret a publier)."""
    account = _find_account(config, account_id)
    youtube_account = account.get("service") == "youtube"
    try:
        if youtube_account and url is None:
            url = youtube_mod.studio_url()
        browser_mod.start_login(account_id, url, config=config, on_close=lambda: _verify_account(config, account))
    except (browser_mod.BrowserError, youtube_mod.YouTubeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _verify_login(config: Config, account: dict[str, Any]) -> dict[str, Any]:
    """Connexion d'un compte TikTok, lue dans les cookies de son profil sans naviguer (SPEC-00d1 R2) et
    enregistree ; une session expiree decoche « pret a publier » (R3) et le signale a la console.
    Une lecture impossible (profil verrouille...) garde l'etat connu et le dit dans ``login_error``.
    Un compte YouTube n'est jamais lu dans les cookies : sa connexion vient de la derniere verification
    de YouTube Studio (``_verify_youtube``), l'ecran ne lance pas de navigateur a chaque ouverture."""
    if account.get("service") == "youtube":
        return account
    try:
        observed = browser_mod.login_state(account["id"], config=config)
        result = accounts_mod.record_login(config, account["id"], observed)
    except (browser_mod.BrowserError, accounts_mod.AccountsError) as exc:  # jamais tout l'ecran : fable-comptes 8
        return {**account, "login_error": str(exc)}
    if result.pop("auto_unchecked"):
        _emit_unchecked(config, account["id"], result["ready_note"])
    return result


def _emit_unchecked(config: Config, account_id: str, note: str | None) -> None:
    try:
        tiktok_mod.emit_event({"level": "warn", "account": account_id, "channel": None, "video_id": None,
                               "clip_id": None, "reason": note, "capture": None}, config=config)
    except tiktok_mod.TikTokError as exc:
        logger.error("evenement « prêt à publier décoché » non écrit : %s", exc)


def _verify_youtube(config: Config, account: dict[str, Any]) -> dict[str, Any]:
    """Verification « pret a publier » d'un compte YouTube (SPEC-5e50 R1) : ouvre YouTube Studio sur le
    profil, enregistre la chaine vue (nom, identifiant) ou la raison du refus. Chrome/Playwright absent ou
    reglage invalide : l'erreur remonte (``BrowserError`` / ``YouTubeError``), rien n'est invente."""
    seen = youtube_mod.verify_login(account["id"], config=config)
    observed: dict[str, Any] = {"state": "connected" if seen["ready"] else "never", "channel": seen["channel"]}
    if not seen["ready"]:
        observed["reason"] = seen["reason"]
    result = accounts_mod.record_login(config, account["id"], observed)
    if result.pop("auto_unchecked"):
        _emit_unchecked(config, account["id"], result["ready_note"])
    result.pop("auto_checked", None)
    result["capture"] = seen.get("capture")
    return result


def _verify_account(config: Config, account: dict[str, Any]) -> dict[str, Any]:
    """A la fermeture de la fenetre de connexion : YouTube Studio pour un compte YouTube, les cookies pour TikTok."""
    if account.get("service") == "youtube":
        return _verify_youtube(config, account)
    return _verify_login(config, account)


def _account_overview(config: Config, account: dict[str, Any]) -> dict[str, Any]:
    """Un compte pour l'ecran Comptes (SPEC-00d1 R5) : connexion verifiee, case « pret a publier » avec la raison
    qui la grise, posts du jour / plafond, dernier echec de publication (avec capture)."""
    out = _verify_login(config, account)
    out["ready_blocked_reason"] = accounts_mod.ready_blocked_reason(out) if out.get("login_error") is None else \
        f"connexion non vérifiable : {out['login_error']}"
    scope = {"state_dir": _publish_dir(config), "presets_dir": _PRESETS_DIR, "base": _BASE_CONFIG}
    out.update(posts_today=None, max_posts_per_day=None, last_failure=None, publish_error=None)
    try:
        settings_of = youtube_mod.get_settings if out.get("service") == "youtube" else tiktok_mod.get_settings
        out["max_posts_per_day"] = settings_of(config)["max_posts_per_day"]
        tz = ZoneInfo(str(out["timezone"]))
        today = datetime.now(tz).date()
        out["posts_today"] = sum(1 for t in publish_mod.account_publish_times(out["id"], **scope)
                                 if t.astimezone(tz).date() == today)
        failure = publish_mod.last_failure(out["id"], **scope)
        if failure is not None:
            out["last_failure"] = {
                "channel": failure["channel"], "video_id": failure["video_id"], "clip_id": failure["clip_id"],
                "reason": failure.get("error"), "failed_at": failure.get("failed_at"),
                "capture_url": f"/api/publish/{failure['video_id']}/{failure['clip_id']}/capture" if failure.get("capture") else None,
            }
    except (publish_mod.PublishError, channel_mod.ChannelError, ConfigError, tiktok_mod.TikTokError,
            youtube_mod.YouTubeError) as exc:
        out["publish_error"] = str(exc)
    return out


def _accounts_overview(config: Config) -> list[dict[str, Any]]:
    return [_account_overview(config, a) for a in _accounts_call(accounts_mod.list_accounts, config)]


def _account_verify(config: Config, account_id: str) -> dict[str, Any]:
    account = _find_account(config, account_id)
    if account.get("service") != "youtube":
        raise HTTPException(status_code=409, detail="seul un compte YouTube se vérifie dans le navigateur : "
                            "la connexion TikTok se lit dans les cookies du profil à l'ouverture de l'écran")
    try:
        out = _verify_youtube(config, account)
    except (browser_mod.BrowserError, youtube_mod.YouTubeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    out["ready_blocked_reason"] = accounts_mod.ready_blocked_reason(out)
    return out


def _account_resolve(config: Config, account_id: str) -> dict[str, Any]:
    """« J'ai regle le probleme » (SPEC-e500 R3) : revérifie la connexion (R2), puis efface l'arret R4 en
    attente ; la case suit la connexion revérifiee. Cookies illisibles : 409, l'arret reste en attente."""
    account = next((a for a in _accounts_call(accounts_mod.list_accounts, config) if a["id"] == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail=f"compte introuvable : {account_id!r}")
    verified = _verify_login(config, account)
    if verified.get("login_error") is not None:
        raise HTTPException(status_code=409, detail=f"connexion non vérifiable : {verified['login_error']} "
                            "(l'arrêt reste en attente)")
    out = _accounts_call(accounts_mod.clear_halt, config, account_id)
    out.pop("auto_checked", None), out.pop("auto_unchecked", None)
    return out


def _account_pause(config: Config, account_id: str) -> dict[str, Any]:
    """Pause manuelle (SPEC-f348 R7.1) : la case se decoche, la console en est prevenue comme pour un decochage
    automatique ; la connexion et l'arret R4 ne bougent pas."""
    out = _accounts_call(accounts_mod.pause, config, account_id)
    if out.pop("auto_unchecked"):
        _emit_unchecked(config, account_id, out["ready_note"])
    out.pop("auto_checked", None)
    return out


def _account_resume(config: Config, account_id: str) -> dict[str, Any]:
    """Reprise (SPEC-f348 R7.2) : revérifie la connexion (R2) puis leve la pause ; la case suit R3. Cookies illisibles :
    409, la pause reste en place."""
    account = next((a for a in _accounts_call(accounts_mod.list_accounts, config) if a["id"] == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail=f"compte introuvable : {account_id!r}")
    verified = _verify_login(config, account)
    if verified.get("login_error") is not None:
        raise HTTPException(status_code=409, detail=f"connexion non vérifiable : {verified['login_error']} "
                            "(le compte reste en pause)")
    out = _accounts_call(accounts_mod.resume, config, account_id)
    out.pop("auto_checked", None), out.pop("auto_unchecked", None)
    return out


class _LimitRefused(Exception):
    """Plafond du compte depasse : 409 avec la raison et la prochaine heure possible (SPEC-1ed3 R4)."""

    def __init__(self, detail: str, next_at: str | None) -> None:
        super().__init__(detail)
        self.detail, self.next_at = detail, next_at


def _publication_settings(config: Config, service: str = "tiktok") -> dict[str, Any]:
    """Reglages [tiktok] (ou [youtube], SPEC-5e50 R5) valides : plafonds, fenetre de programmation, reglages par defaut
    d'un post."""
    try:
        return youtube_mod.get_settings(config) if service == "youtube" else tiktok_mod.get_settings(config)
    except (tiktok_mod.TikTokError, youtube_mod.YouTubeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _publish_accounts(config: Config) -> list[dict[str, Any]]:
    """Comptes proposes a la publication, des deux services : id, libelle, service, pret ou non (aucun secret,
    lisible hors du PC)."""
    return [{"id": a["id"], "label": a["label"], "ready_to_publish": a["ready_to_publish"],
             "paused_at": a.get("paused_at"), "service": a["service"], "service_label": accounts_mod.SERVICE_LABELS.get(a["service"], a["service"])}
            for a in _accounts_call(accounts_mod.list_accounts, config)]


def _account_schedule(config: Config, account_id: str) -> dict[str, Any]:
    """Creneaux reguliers (et fuseau) d'un compte de l'ecran Comptes (SPEC-6076 R2) : ``{"slots", "timezone"}``."""
    found = next((a for a in _accounts_call(accounts_mod.list_accounts, config) if a["id"] == account_id), None)
    if found is None:
        raise HTTPException(status_code=409, detail=f"compte inconnu : {account_id!r} (écran Comptes)")
    return accounts_mod.schedule_of(found)


def _account_service(config: Config, account_id: Any) -> str:
    """Service (``tiktok`` | ``youtube``) d'un compte de publication."""
    found = next((a for a in _publish_accounts(config) if a["id"] == account_id), None)
    if found is None:
        raise HTTPException(status_code=409, detail=f"compte inconnu : {account_id!r} (écran Comptes)")
    return found["service"]


def _require_ready_account(config: Config, account_id: Any) -> str:
    """Compte choisi pour une publication (SPEC-00d1 R4) : un compte pret a publier, sinon 409 explicite."""
    found = next((a for a in _publish_accounts(config) if a["id"] == account_id), None)
    if found is None:
        raise HTTPException(status_code=409, detail=f"compte inconnu : {account_id!r} (écran Comptes)")
    if found.get("paused_at"):
        raise HTTPException(status_code=409, detail=f"compte {found['label'] or account_id} en pause (manuel) depuis le "
                            f"{accounts_mod.paused_since(found)} : "
                            "recoche « Prêt à publier » dans l'écran Comptes pour reprendre, ou choisis un autre compte")
    if not found["ready_to_publish"]:
        raise HTTPException(status_code=409, detail=f"compte {found['label'] or account_id} non prêt à publier : "
                            "« prêt à publier » est automatique : connecte-le (Se connecter) ou règle l'arrêt en attente dans l'écran Comptes")
    return found["id"]


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    web_cfg = config.section("web")
    host = str(web_cfg["host"])
    token = str(web_cfg["token"])
    enforce_auth = host != _LOOPBACK_HOST
    if enforce_auth and not token:
        raise WebConfigError(
            f"[web] host={host!r} hors bouclage exige un jeton ([web] token) configure (ADR-35b7 §5)"
        )

    app = FastAPI(title="Clipper", default_response_class=JSONResponse)
    app.state.config = config
    global _PRESETS_DIR, _BASE_CONFIG  # dossier des styles et fichier de base : ceux des réglages [watch]
    watch_cfg = config.section("watch")
    _PRESETS_DIR, _BASE_CONFIG = str(watch_cfg["presets_dir"]), str(watch_cfg["base_config"])
    try:  # creneaux et compte d'un ancien style repris sur le compte (SPEC-6076 R2), journalise
        channel_mod.migrate_legacy_presets(config, presets_dir=_PRESETS_DIR, base=_BASE_CONFIG)
    except (channel_mod.ChannelError, ConfigError, OSError, ValueError) as exc:
        logger.error("migration des anciens styles impossible : %s", exc)
    stats_refreshes = _StatsRefreshes()

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        body: dict[str, Any] = {"detail": exc.detail}
        if isinstance(exc, _NoChannel):
            body.update(needs_channel=True, video_id=exc.video_id, channels=exc.channels)
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def _check_token(request: Request, call_next):
        if enforce_auth and request.url.path.startswith(_PROTECTED_PREFIXES):
            supplied = request.headers.get(_TOKEN_HEADER) or request.cookies.get(_TOKEN_COOKIE)
            if supplied != token:
                return JSONResponse({"detail": "jeton d'acces manquant ou invalide"}, status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def _accounts_only_local(request: Request, call_next):
        if _is_accounts_path(request.url.path):
            refusal = _accounts_guard(request)
            if refusal is not None:
                return refusal
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        return await call_next(request)

    @app.middleware("http")
    async def _revalidate_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # revalidation par ETag, 304 conserve
        return response

    _journal_exclude_paths = tuple(config.section("journal")["exclude_paths"])

    @app.middleware("http")
    async def _journal_requests(request: Request, call_next):
        """Chaque requete HTTP dans le journal global (TASK-8067), sauf les
        chemins exclus (statique, media) : methode, chemin, statut, duree ;
        une requete qui modifie (POST/PUT/PATCH/DELETE) ajoute un resume du
        corps avec mots de passe/cookies/jetons masques (clipper.journal).
        Ecrit via le logger normal : c'est le handler de journal installe
        par clipper.__main__ (independant de ce module) qui le persiste."""
        path = request.url.path
        excluded = any(path.startswith(prefix) for prefix in _journal_exclude_paths)
        full_path = f"{path}?{request.url.query}" if request.url.query else path
        body_summary = None
        if not excluded and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            body_summary = journal_mod.summarize_body(await request.body())
        started = time_mod.monotonic()
        response = await call_next(request)
        if not excluded:
            duration_ms = (time_mod.monotonic() - started) * 1000
            message = f"{request.method} {full_path} -> {response.status_code} ({duration_ms:.1f} ms)"
            if body_summary:
                message += f" corps={body_summary}"
            logger.info(message)
        return response

    @app.get("/")
    def index(request: Request) -> Response:
        page = STATIC_DIR / "index.html"
        info = page.stat()
        etag = '"' + hashlib.md5(f"{info.st_mtime}-{info.st_size}".encode()).hexdigest() + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return FileResponse(page, headers={"ETag": etag})

    @app.get("/api/dashboard")
    def dashboard() -> dict[str, Any]:
        return _dashboard(config)

    @app.get("/api/dashboard/worker")
    def dashboard_worker() -> dict[str, Any]:
        out: dict[str, Any] = {}
        _fill(out, ("worker",), "battement du worker (state/worker.json)", lambda: _dashboard_worker(config))
        return out

    # ----------------------------------------------------------------
    # File de traitement (SPEC-74e9 §2)
    # ----------------------------------------------------------------

    @app.post("/api/queue", status_code=202)
    def enqueue_video(body: QueueBody) -> JSONResponse:
        return _enqueue(body.url, body.channel, body.action, body.force_steps, config, body.short_clips)

    @app.get("/api/queue")
    def list_queue() -> list[dict[str, Any]]:
        path = _queue_path(config)
        if not path.exists():
            return []
        return _with_thumbnails(config, json.loads(path.read_text(encoding="utf-8")))

    @app.post("/api/queue/{video_id}/front")
    def queue_front(video_id: str) -> dict[str, Any]:
        _validate_video_id(video_id)
        try:
            worker_mod.move_to_front(video_id, config=config)
        except worker_mod.WorkerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"video_id": video_id, "moved": True}

    @app.delete("/api/queue/{video_id}")
    def queue_remove(video_id: str) -> dict[str, Any]:
        _validate_video_id(video_id)
        try:
            worker_mod.remove(video_id, config=config)
        except worker_mod.WorkerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"video_id": video_id, "removed": True}

    # ----------------------------------------------------------------
    # Videos : alias v1, etat, moments, revue, rendu, annulation, relance
    # ----------------------------------------------------------------

    @app.post("/api/videos", status_code=202)
    def submit_video(body: SubmitBody) -> JSONResponse:
        """Alias de POST /api/queue sans chaine (action 'run')."""
        return _enqueue(body.url, None, "run", None, config)

    @app.get("/api/videos")
    def list_videos(channel: str | None = None, status: str | None = None,
                    q: str | None = None) -> list[dict[str, Any]]:
        """Liste filtrable (chaine exacte, statut, texte sur video_id / titre /
        source_url), chaque video enrichie de son titre, de son etape courante
        et de la duree de chaque etape (SPEC-c100 E2)."""
        if status is not None and status not in _VIDEO_FILTER_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"statut inconnu : {status!r} (attendu : {', '.join(_VIDEO_FILTER_STATUSES)})",
            )
        videos = []
        for state in _list_states(config):
            video = _enrich(state, config)
            video["added_at"], video["added_at_source"] = _added_at(state, config)
            videos.append(video)
        videos.sort(key=lambda v: datetime.fromisoformat(v["added_at"]), reverse=True)  # la plus récemment ajoutée en haut
        return [v for v in videos if _matches(v, channel, status, q)]

    @app.get("/api/videos/{video_id}")
    def get_video(video_id: str) -> dict[str, Any]:
        try:
            state = pipeline.load_state(video_id, config=config)
        except pipeline.PipelineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        detail = _enrich(state, config)
        detail["clips"] = state.get("clips") or []
        detail["awaiting"] = state.get("awaiting") or []
        detail["rubric"] = _rubric_used(Path(config.workspace_dir) / video_id)
        detail["short_clips"] = _short_clips_used(Path(config.workspace_dir) / video_id)
        return detail

    @app.get("/api/videos/{video_id}/moments")
    def list_moments(video_id: str) -> list[dict[str, Any]]:
        return _list_moments(config, video_id)

    @app.get("/api/videos/{video_id}/jury")
    def get_jury(video_id: str) -> dict[str, Any]:
        """Detail du jury par moment pour le radar de la fiche video (lecture seule)."""
        _validate_video_id(video_id)
        return _jury_view(config, video_id)

    @app.post("/api/videos/{video_id}/moments/{moment_id}/decide")
    def decide_moment(video_id: str, moment_id: int, body: DecideBody) -> dict[str, Any]:
        try:
            return pipeline.decide(video_id, moment_id, body.decision, start=body.start, end=body.end,
                                   comment=body.comment, config=config)
        except pipeline.PipelineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/videos/{video_id}/render", status_code=202)
    def start_render(video_id: str) -> JSONResponse:
        """Remet la video en file, action 'render' (ADR-35b7 §1 : jamais
        pipeline.render dans ce processus)."""
        _validate_video_id(video_id)
        return _enqueue(video_id, _channel_of(video_id, config), "render", None, config)

    @app.post("/api/videos/{video_id}/cancel")
    def cancel_video(video_id: str) -> dict[str, Any]:
        _validate_video_id(video_id)
        try:
            worker_mod.cancel(video_id, config=config)  # par le pid de la file : jamais un Worker ici
        except worker_mod.WorkerError as exc:
            # rien en cours : 404 ; processus impossible a arreter : la video existe, 409 (revue fable-comptes 9)
            raise HTTPException(status_code=404 if "aucune video en cours" in str(exc) else 409,
                                detail=str(exc)) from exc
        return {"video_id": video_id, "cancelled": True}

    @app.post("/api/videos/{video_id}/resume", status_code=202)
    def resume_video(video_id: str) -> JSONResponse:
        """Remet une video interrompue dans la file ; elle repart de son etape interrompue (TASK-bdd5)."""
        _validate_video_id(video_id)
        try:
            entry = worker_mod.resume(video_id, config=config)
        except pipeline.PipelineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except worker_mod.WorkerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(entry, status_code=202)

    @app.post("/api/videos/{video_id}/retry", status_code=202)
    def retry_video(video_id: str, body: RetryBody) -> JSONResponse:
        _validate_video_id(video_id)
        if body.from_step not in pipeline.STEPS:
            raise HTTPException(status_code=400, detail=f"etape inconnue : {body.from_step!r}")
        if (Path(config.workspace_dir) / video_id / pipeline.STATE_FILE).is_file():
            pipeline.restore_video(video_id, config=config)  # relancer = ne plus etre « retiree »
        force_steps = list(pipeline.STEPS[pipeline.STEPS.index(body.from_step):])
        return _enqueue(video_id, _channel_of(video_id, config), "render", force_steps, config)

    @app.post("/api/videos/{video_id}/channel")
    def assign_channel(video_id: str, body: AssignChannelBody) -> dict[str, Any]:
        """Attribue une chaîne à une vidéo qui n'en a pas (fiche vidéo, erreur d'approbation) : pipeline.set_channel
        écrit pipeline.json et journalise."""
        _validate_video_id(video_id)
        _validate_channel_name(body.channel)
        try:
            return _enrich(pipeline.set_channel(video_id, body.channel, config=config, presets_dir=_PRESETS_DIR,
                                                 state_dir=_publish_dir(config)), config)
        except pipeline.PipelineError as exc:
            raise HTTPException(status_code=404 if "aucun etat" in str(exc) else 409, detail=str(exc)) from exc

    @app.post("/api/videos/{video_id}/dismiss")
    def dismiss_video(video_id: str) -> dict[str, Any]:
        """Sort la video des echecs et des compteurs sans effacer son dossier
        (``dismissed_at`` dans pipeline.json) ; reversible par /restore."""
        _validate_video_id(video_id)
        try:
            return _enrich(pipeline.dismiss_video(video_id, config=config), config)
        except pipeline.PipelineError as exc:
            raise HTTPException(status_code=404 if "aucun etat" in str(exc) else 409, detail=str(exc)) from exc

    @app.post("/api/videos/{video_id}/restore")
    def restore_video(video_id: str) -> dict[str, Any]:
        _validate_video_id(video_id)
        try:
            return _enrich(pipeline.restore_video(video_id, config=config), config)
        except pipeline.PipelineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ----------------------------------------------------------------
    # Purge disque (TASK-886a) : taille d'abord (GET), suppression ensuite (POST, confirmee cote ecran)
    # ----------------------------------------------------------------

    def _purge_call(fn, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except workspace_mod.PurgeRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _purge_plan(video_id: str, clips: bool) -> dict[str, Any]:
        """Tailles qui seraient liberees ; refuse (409) comme la purge elle-meme."""
        ws_root, queue = config.workspace_dir, _queue_path(config)
        if not (Path(ws_root) / video_id).is_dir():
            raise HTTPException(status_code=409, detail=f"dossier de la video introuvable : {Path(ws_root) / video_id}")
        reason = _purge_call(workspace_mod.busy_reason, video_id, ws_root, queue)
        if reason:
            raise HTTPException(status_code=409, detail=f"purge refusee : {reason}")
        heavy = workspace_mod.heavy_size(video_id, ws_root)
        clips_bytes = 0
        if clips:
            _purge_call(workspace_mod.check_clips_purgeable, video_id, _publish_dir(config))
            clips_bytes = workspace_mod.clips_size(video_id, config.output_dir)
        return {"video_id": video_id, "heavy_bytes": heavy, "clips_bytes": clips_bytes, "total_bytes": heavy + clips_bytes}

    @app.get("/api/disk")
    def disk_usage() -> dict[str, int]:
        return workspace_mod.disk_usage(config.workspace_dir, config.output_dir)

    @app.get("/api/purge-completed")
    def purge_completed_preview() -> dict[str, Any]:
        videos = []
        for state in _list_states(config):
            if state.get("status") != "done":
                continue
            try:
                plan = _purge_plan(state["video_id"], False)
            except HTTPException:
                continue  # en file ou illisible : hors de la purge groupee
            if plan["heavy_bytes"]:
                videos.append({"video_id": state["video_id"], "bytes": plan["heavy_bytes"]})
        return {"videos": videos, "total_bytes": sum(v["bytes"] for v in videos)}

    @app.post("/api/purge-completed")
    def purge_completed() -> dict[str, Any]:
        purged, skipped, freed = [], [], 0
        for item in purge_completed_preview()["videos"]:
            try:
                freed += workspace_mod.purge_heavy(item["video_id"], config.workspace_dir, queue_path=_queue_path(config))
                purged.append(item["video_id"])
            except workspace_mod.PurgeRefused as exc:
                skipped.append({"video_id": item["video_id"], "reason": str(exc)})
        logger.info("purge des videos terminees : %d video(s), %d octets liberes", len(purged), freed)
        return {"videos": purged, "skipped": skipped, "freed_bytes": freed}

    @app.get("/api/purge/{video_id}")
    def purge_preview(video_id: str, clips: bool = False) -> dict[str, Any]:
        _validate_video_id(video_id)
        return _purge_plan(video_id, clips)

    @app.post("/api/purge/{video_id}")
    def purge_video(video_id: str, body: PurgeBody) -> dict[str, Any]:
        _validate_video_id(video_id)
        if body.clips:
            _purge_call(workspace_mod.check_clips_purgeable, video_id, _publish_dir(config))  # avant toute suppression
        freed = _purge_call(workspace_mod.purge_heavy, video_id, config.workspace_dir, queue_path=_queue_path(config))
        clips_freed = (_purge_call(workspace_mod.purge_clips, video_id, config.output_dir, _publish_dir(config))
                       if body.clips else 0)
        return {"video_id": video_id, "freed_bytes": freed + clips_freed, "heavy_bytes": freed, "clips_bytes": clips_freed}

    @app.get("/api/videos/{video_id}/events")
    def video_events(video_id: str, since: str | None = None) -> list[dict[str, Any]]:
        _validate_video_id(video_id)
        path = Path(config.workspace_dir) / video_id / pipeline.EVENTS_FILE
        if not path.exists():
            return []
        events = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw:
                continue
            event = json.loads(raw)
            if since is not None and event["at"] <= since:
                continue
            events.append(event)
        return events

    @app.get("/api/videos/{video_id}/clips")
    def list_clips(video_id: str) -> list[dict[str, Any]]:
        out_dir = Path(config.output_dir) / video_id
        if not out_dir.is_dir():
            return []
        clips = []
        for path in sorted(out_dir.glob("*.json")):
            clip = _read_json(path)
            clip["video_url"] = f"/media/clip/{video_id}/{clip['clip_id']}"
            clip["thumbnail_url"] = f"/media/clip/{video_id}/{clip['clip_id']}/thumbnail"
            clips.append(clip)
        return clips

    # ----------------------------------------------------------------
    # Clips (SPEC-c100 E4)
    # ----------------------------------------------------------------

    @app.get("/api/clips")
    def list_all_clips(channel: str | None = None, video_id: str | None = None,
                       status: str | None = None, archived: int = 0) -> list[dict[str, Any]]:
        if video_id is not None:
            _validate_video_id(video_id)
        if status is not None and status not in _CLIP_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"statut inconnu : {status!r} (attendu : {', '.join(_CLIP_STATUSES)})",
            )
        return _list_clip_views(config, channel or None, video_id, status, archived=bool(archived))

    @app.get("/api/clips/{video_id}/{clip_id}/sheet")
    def clip_sheet(video_id: str, clip_id: str) -> dict[str, Any]:
        """Fiche d'un clip (lecture seule, rien n'est recalculé) : le clip (sidecar + publication + jury), la vidéo
        (lue si le .mp4 existe, sinon « vidéo supprimée, fiche conservée »), les relevés TikTok du post s'il y en a.
        Une donnée absente est ``None`` (« inconnu »), jamais 0 (ADR-ad2e)."""
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        sidecar = _read_clip_sidecar(config, video_id, clip_id)
        channel = _channel_of(video_id, config)
        entry = _publish_entries(config, channel).get((video_id, clip_id))
        deleted = _clip_video_deleted(config, video_id, clip_id)
        clip = _clip_view(sidecar, channel, entry, _moments_jury_confidences(config, video_id), video_deleted=deleted)
        clip["score"] = sidecar.get("score")
        if deleted:
            clip["video_url"] = clip["thumbnail_url"] = None
        source_url, start = sidecar.get("source_url"), sidecar.get("start")
        passage_url = None
        if source_url and start is not None:
            passage_url = f"{source_url}{'&' if '?' in source_url else '?'}t={int(start)}s"
        moments_path = Path(config.workspace_dir) / video_id / "moments.json"
        video = {
            "title": sidecar.get("source_title"), "source_url": source_url, "passage_url": passage_url,
            "deleted": deleted, "video_url": clip["video_url"],
            "note": "vidéo supprimée, fiche conservée" if deleted else None,
        }
        moment = next((m for m in _read_json(moments_path)["moments"] if m.get("id") == sidecar.get("moment_id")),
                      None) if moments_path.is_file() else None
        rounds = _jury_rounds(moment["jury"]) if moment and (moment.get("jury") or {}).get("trace") else None
        jury = {"confidence": clip["jury_confidence"], "judges": clip["jury_judge_confidences"], "rounds": rounds}
        stats = None
        post_id = entry.get("post_id") if entry else None
        if post_id and entry.get("account") and entry.get("service") != "youtube":
            try:
                detail = tiktok_mod.video_detail(entry["account"], str(post_id), config=config)
                stats = {"views": detail.get("views"), "likes": detail.get("likes"),
                         "history": _stats_history_changes(detail.get("history")),
                         "error": None}
            except tiktok_mod.TikTokError as exc:
                stats = {"views": None, "likes": None, "history": [], "error": str(exc)}
        return {"clip": clip, "video": video, "jury": jury, "stats": stats}

    def _decide(video_id: str, clip_id: str, action: str, **extra: Any) -> dict[str, Any]:
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        # Vidéo sans style : file _sans_chaine, le compte suffit (SPEC-6076 R2, SPEC-1ed3 R3).
        channel = _require_channel(video_id, clip_id, config, or_no_channel=True)
        kwargs: dict[str, Any] = {"output_dir": Path(config.output_dir), "state_dir": _publish_dir(config), **extra}
        if action == "approve":
            kwargs["presets_dir"] = _PRESETS_DIR
        try:
            return getattr(publish_mod, action)(video_id, clip_id, channel, **kwargs)
        except publish_mod.PublishError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except channel_mod.ChannelError as exc:  # style sans preset (supprime) : revue fable-comptes 7
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/clips/{video_id}/{clip_id}/approve")
    def approve_clip(video_id: str, clip_id: str, body: AccountBody | None = None) -> dict[str, Any]:
        """Valide le clip ; ``{"account": id}`` choisit le compte de publication parmi les comptes prets
        (SPEC-00d1 R4) ; sans compte, 409 explicite : un style n'en porte plus (SPEC-6076 R2). Les creneaux
        sont ceux du compte."""
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        if body is None or body.account is None:
            raise HTTPException(status_code=409, detail="compte de publication manquant : choisis un compte prêt à publier "
                                "(un style n'a plus de compte associé)")
        _require_clip_video(config, video_id, clip_id)
        account = _require_ready_account(config, body.account)
        return _decide(video_id, clip_id, "approve", account=account, schedule=_account_schedule(config, account))

    @app.post("/api/clips/{video_id}/{clip_id}/reject")
    def reject_clip(video_id: str, clip_id: str) -> dict[str, Any]:
        return _decide(video_id, clip_id, "reject")

    @app.post("/api/clips/approve")
    def approve_clips_bulk(body: BulkApproveBody) -> list[dict[str, Any]]:
        """Approuve plusieurs clips d'un coup pour un meme compte (bouton « Sélectionner » de
        l'écran Clips, TASK-e99b) : exactement la même décision que POST /api/clips/{v}/{c}/approve
        pour chacun (même compte, mêmes créneaux du compte). Cocher une partie de série entraîne
        toute la série (ordre des parties respecté, _require_previous_part de publish.approve).
        Validation de tous les clips (séries comprises) avant la première approbation, avec la règle
        même de publish.approve (publish.approval_refusal) : tout ou rien, aucune valeur de secours
        (ADR-ad2e). Une partie déjà publiée entraînée par sa série (pas cochée) est laissée telle quelle."""
        if not body.clips:
            raise HTTPException(status_code=400, detail="sélection vide : choisis au moins un clip")
        if not body.account:
            raise HTTPException(status_code=409, detail="compte de publication manquant : choisis un compte prêt à publier "
                                "(un style n'a plus de compte associé)")
        account = _require_ready_account(config, body.account)
        schedule = _account_schedule(config, account)

        seen: set[tuple[str, str]] = set()
        expanded: list[tuple[str, str]] = []
        refused: list[str] = []
        for item in body.clips:
            _validate_video_id(item.video_id)
            _validate_clip_id(item.clip_id)
            if (item.video_id, item.clip_id) in seen:
                continue
            try:
                series = publish_mod.series_clip_ids(item.video_id, item.clip_id, output_dir=config.output_dir)
            except publish_mod.PublishError as exc:
                refused.append(f"{item.video_id}/{item.clip_id} : {exc}")
                seen.add((item.video_id, item.clip_id))
                continue
            for clip_id in series:
                key = (item.video_id, clip_id)
                if key not in seen:
                    seen.add(key)
                    expanded.append(key)

        chosen = {(item.video_id, item.clip_id) for item in body.clips}
        to_approve: list[tuple[str, str]] = []
        channel_of_video: dict[str, str | None] = {}
        entries_by_channel: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        for video_id, clip_id in expanded:
            if video_id not in channel_of_video:
                try:
                    channel_of_video[video_id] = _require_channel(video_id, clip_id, config, or_no_channel=True)
                except HTTPException as exc:
                    channel_of_video[video_id] = None
                    refused.append(f"{video_id}/{clip_id} : {exc.detail}")
            channel = channel_of_video[video_id]
            if channel is None:
                continue
            if channel not in entries_by_channel:
                entries_by_channel[channel] = _publish_entries(config, channel)
            entry = entries_by_channel[channel].get((video_id, clip_id))
            if entry and entry["status"] == "published" and (video_id, clip_id) not in chosen:
                continue  # partie deja publiee entrainee par sa serie : laissee telle quelle (fable-comptes 5)
            if entry and entry["status"] in ("published", "rejected", publish_mod.REFUSED_BY_PLATFORM,
                                             publish_mod.REMOVED_FROM_PLATFORM):
                refused.append(f"{video_id}/{clip_id} : déjà {entry['status']}")
                continue
            refusal = publish_mod.approval_refusal(entry)  # planifie, en cours, formulaire (fable-comptes 2)
            if refusal is not None:
                refused.append(f"{video_id}/{clip_id} : {refusal}")
                continue
            sidecar = publish_mod.read_sidecar(config.output_dir, video_id, clip_id)
            if not sidecar.get("ready"):
                refused.append(f"{video_id}/{clip_id} : pas prêt pour publication")
                continue
            to_approve.append((video_id, clip_id))

        if refused:
            raise _BulkApproveRefused(refused)

        approved: list[dict[str, Any]] = []
        for video_id, clip_id in to_approve:
            try:
                approved.append(_decide(video_id, clip_id, "approve", account=account, schedule=schedule))
            except HTTPException as exc:  # change entre la validation et l'ecriture : le dire, jamais en silence
                done = ", ".join(f"{e['video_id']}/{e['clip_id']}" for e in approved) or "aucun"
                raise HTTPException(status_code=exc.status_code,
                                    detail=f"{exc.detail} ; déjà approuvés avant cet échec : {done}") from exc
        return approved

    @app.post("/api/clips/delete")
    def delete_clips_bulk(body: BulkApproveBody) -> dict[str, Any]:
        """Supprime les clips choisis (bouton « Supprimer la sélection » de l'écran Clips, TASK-2322) : simple
        suppression de fichiers par clipper.workspace.delete_clips (ADR-09ad). Tout ou rien PAR série : une partie
        choisie entraîne toute sa série, et une série dont une partie est programmée, en cours ou en attente est
        refusée entière (``refused`` : le clip choisi et la raison, jamais d'erreur silencieuse). Un clip publié
        garde son sidecar : seule sa vidéo est supprimée (``video_deleted``, TASK-f909)."""
        if not body.clips:
            raise HTTPException(status_code=400, detail="sélection vide : choisis au moins un clip")
        deleted: list[dict[str, str]] = []
        video_deleted: list[dict[str, str]] = []
        refused: list[dict[str, Any]] = []
        done: set[tuple[str, str]] = set()
        freed = 0
        for item in body.clips:
            _validate_video_id(item.video_id)
            _validate_clip_id(item.clip_id)
            if (item.video_id, item.clip_id) in done:
                continue  # deja supprime avec sa serie
            try:
                result = workspace_mod.delete_clips(item.video_id, [item.clip_id], config.output_dir, _publish_dir(config))
            except workspace_mod.PurgeRefused as exc:
                refused.append({"clip": {"video_id": item.video_id, "clip_id": item.clip_id}, "reason": str(exc)})
                continue
            freed += result["freed_bytes"]
            for clip_id in result["deleted"]:
                done.add((item.video_id, clip_id))
                deleted.append({"video_id": item.video_id, "clip_id": clip_id})
            for clip_id in result["video_deleted"]:
                done.add((item.video_id, clip_id))
                video_deleted.append({"video_id": item.video_id, "clip_id": clip_id})
        logger.info("suppression de clips : %d supprime(s), %d video(s) seule(s), %d refuse(s), %d octets liberes",
                    len(deleted), len(video_deleted), len(refused), freed)
        return {"deleted": deleted, "video_deleted": video_deleted, "refused": refused, "freed_bytes": freed}

    @app.post("/api/clips/{video_id}/{clip_id}/rerender", status_code=202)
    def rerender_clip(video_id: str, clip_id: str) -> JSONResponse:
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        _read_clip_sidecar(config, video_id, clip_id)
        _require_clip_video(config, video_id, clip_id)
        return JSONResponse(_enqueue_clip_render(video_id, config), status_code=202)

    @app.patch("/api/clips/{video_id}/{clip_id}")
    def edit_clip(video_id: str, clip_id: str, body: ClipPatchBody) -> JSONResponse:
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        if body.description is None and body.hashtags is None and body.screen_title is None:
            raise HTTPException(status_code=400, detail="rien à modifier : description, hashtags ou screen_title attendu")
        sidecar = _read_clip_sidecar(config, video_id, clip_id)
        retitle = body.screen_title is not None
        if retitle and body.screen_title == sidecar.get("screen_title"):
            raise HTTPException(status_code=400, detail="titre d'écran inchangé : rien à re-rendre")
        if retitle and not body.confirm:
            raise HTTPException(
                status_code=409,
                detail="confirmation requise (confirm: true) : changer le titre d'écran relance render puis qa pour ce clip",
            )
        if retitle:
            _require_clip_video(config, video_id, clip_id)
        channel = _require_channel(video_id, clip_id, config)
        if body.description is not None or body.hashtags is not None:
            description = body.description if body.description is not None else sidecar.get("caption")
            hashtags = body.hashtags if body.hashtags is not None else sidecar.get("hashtags")
            try:
                sidecar = publish_mod.edit_caption(
                    video_id, clip_id, channel, description, hashtags,
                    output_dir=Path(config.output_dir), state_dir=_publish_dir(config))
            except publish_mod.PublishError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        entry = _enqueue_clip_render(video_id, config) if retitle else None
        clip = _clip_view(sidecar, channel, _publish_entries(config, channel).get((video_id, clip_id)),
                          _moments_jury_confidences(config, video_id),
                          video_deleted=_clip_video_deleted(config, video_id, clip_id))
        return JSONResponse({"clip": clip, "rerender": entry}, status_code=202 if retitle else 200)

    # ----------------------------------------------------------------
    # Publication (SPEC-c100 E6, SPEC-74e9 §4) : tout passe par clipper.publish
    # ----------------------------------------------------------------

    @app.get("/api/publish")
    def publish_week(
        account: str | None = None, week: str | None = None,
        range_: str | None = Query(None, alias="range"),
    ) -> dict[str, Any]:
        """Calendrier et publications d'un compte TikTok (``account``), ou de tous les comptes sans ``account`` :
        un post publie via Clipper y figure quel que soit son style (ou l'absence de style). ``range``
        (day/week/month, defaut week) choisit la plage ; ``week`` ancre la plage (TASK-ad4d)."""
        return _publish_range_view(config, account or None, range_, week)

    def _publish_action(video_id: str, clip_id: str, action: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        # Toutes ces actions portent sur une entree DEJA DANS la file (y compris _sans_chaine) : le style n'est
        # jamais exige ici (revue r-comptes 4), contrairement a la creation (create_post/approve).
        channel = _require_channel(video_id, clip_id, config, or_no_channel=True)
        try:
            return getattr(publish_mod, action)(video_id, clip_id, channel, *args, state_dir=_publish_dir(config), **kwargs)
        except publish_mod.PublishError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except channel_mod.ChannelError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/publish/{video_id}/{clip_id}/move")
    def publish_move(video_id: str, clip_id: str, body: PublishMoveBody) -> dict[str, Any]:
        slot_at = _publish_parse_slot(body.slot_at)
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        channel = _require_channel(video_id, clip_id, config, or_no_channel=True)
        entry = _publish_entries(config, channel).get((video_id, clip_id))
        account = publish_mod.entry_account(entry) if entry else None
        schedule = _account_schedule(config, account) if account else None
        return _publish_action(video_id, clip_id, "move", slot_at, presets_dir=_PRESETS_DIR, base=_BASE_CONFIG,
                               schedule=schedule)

    @app.post("/api/publish/{video_id}/{clip_id}/published")
    def publish_mark_published(video_id: str, clip_id: str) -> dict[str, Any]:
        return _publish_action(video_id, clip_id, "mark_published")

    @app.post("/api/publish/{video_id}/{clip_id}/removed")
    def publish_mark_removed(video_id: str, clip_id: str, body: RemovedBody | None = None) -> dict[str, Any]:
        """« Supprimé de la plateforme » : l'utilisateur a supprimé le post de TikTok/YouTube a la main ; Clipper
        l'enregistre (raison facultative) sans rien faire sur la plateforme."""
        reason = (body.reason or "").strip() if body else ""
        return _publish_action(video_id, clip_id, "mark_removed_from_platform", reason or None,
                               output_dir=Path(config.output_dir))

    @app.post("/api/publish/{video_id}/{clip_id}/unschedule")
    def publish_unschedule(video_id: str, clip_id: str) -> dict[str, Any]:
        return _publish_action(video_id, clip_id, "unschedule")

    @app.post("/api/publish/{video_id}/{clip_id}/retry")
    def publish_retry(video_id: str, clip_id: str) -> dict[str, Any]:
        return _publish_action(video_id, clip_id, "retry")

    @app.get("/api/publish/accounts")
    def publish_accounts() -> dict[str, Any]:
        """Comptes proposes a la validation et a la programmation (SPEC-00d1 R4) : tous, avec leur case « pret a
        publier ». Aucun compte par defaut : un style n'en porte plus (SPEC-6076 R2), chaque publication choisit."""
        return {"accounts": _publish_accounts(config)}

    @app.post("/api/publish/{video_id}/{clip_id}/account")
    def publish_account(video_id: str, clip_id: str, body: AccountBody) -> dict[str, Any]:
        """Compte de publication d'une entree, parmi les comptes prets (SPEC-00d1 R4)."""
        return _publish_action(video_id, clip_id, "set_account", _require_ready_account(config, body.account))

    @app.post("/api/publish/{video_id}/{clip_id}/mode")
    def publish_mode(video_id: str, clip_id: str, body: PublishModeBody) -> dict[str, Any]:
        return _publish_action(video_id, clip_id, "set_mode", body.mode)

    # ----------------------------------------------------------------
    # Publication pilotee (SPEC-1ed3) : formulaire « Nouvelle publication », suivi, modification, annulation
    # ----------------------------------------------------------------

    def _publication_call(fn, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except publish_mod.LimitError as exc:
            raise _LimitRefused(str(exc), exc.next_at.isoformat() if exc.next_at else None) from exc
        except publish_mod.PublishError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except channel_mod.ChannelError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ConfigError, tiktok_mod.TikTokError, youtube_mod.YouTubeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.exception_handler(_LimitRefused)
    async def _publication_limit_handler(request: Request, exc: _LimitRefused) -> JSONResponse:
        # un plafond depasse rend aussi la prochaine heure possible ({"detail": texte, "next_at": date ISO})
        return JSONResponse({"detail": exc.detail, "next_at": exc.next_at}, status_code=409)

    @app.exception_handler(_BulkApproveRefused)
    async def _bulk_approve_refused_handler(request: Request, exc: _BulkApproveRefused) -> JSONResponse:
        return JSONResponse({"detail": "sélection refusée, rien d'approuvé", "refused": exc.refused}, status_code=409)

    def _publication_scope(service: str = "tiktok") -> dict[str, Any]:
        return {"output_dir": Path(config.output_dir), "state_dir": _publish_dir(config),
                "presets_dir": _PRESETS_DIR, "base": _BASE_CONFIG,
                "settings": _publication_settings(config, service), "service": service}

    def _publication_view(channel: str, entry: dict[str, Any]) -> dict[str, Any]:
        video_id, clip_id = entry["video_id"], entry["clip_id"]
        video_channel = None if channel == publish_mod.NO_CHANNEL else channel
        sidecar = _read_clip_sidecar(config, video_id, clip_id)
        return _clip_view(sidecar, video_channel, entry, video_deleted=_clip_video_deleted(config, video_id, clip_id))

    @app.get("/api/publications")
    def list_publications() -> dict[str, Any]:
        """Toutes les entrees de publication (chaines et videos sans chaine) avec leur statut, plus ce qu'il faut
        au formulaire : comptes (prets ou non), reglages par defaut, limites de programmation de TikTok."""
        settings = _publication_settings(config)
        yt_settings = _publication_settings(config, "youtube")
        rows = []
        try:
            found = publish_mod.all_entries(state_dir=_publish_dir(config), presets_dir=_PRESETS_DIR)
        except publish_mod.PublishError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        refused, removed = [], []
        for channel, entry in found:
            if entry["status"] == "rejected":
                continue
            try:
                view = _publication_view(channel, entry)
            except HTTPException:
                view = _publish_clip_view({}, channel, entry)
            # clips refuses par TikTok : liste a part (raison + capture), jamais dans la file ni sur le calendrier
            if entry["status"] == publish_mod.REFUSED_BY_PLATFORM:
                refused.append({**view, "error": entry.get("error"), "refused_at": entry.get("refused_at")})
            elif entry["status"] == publish_mod.REMOVED_FROM_PLATFORM:  # supprime de la plateforme : liste a part
                removed.append({**view, "removed_at": entry.get("removed_at"), "removed_reason": entry.get("removed_reason")})
            else:
                rows.append(view)
        refused.sort(key=lambda row: str(row.get("refused_at") or ""), reverse=True)
        removed.sort(key=lambda row: str(row.get("removed_at") or ""), reverse=True)
        def slot_instant(row: dict[str, Any]) -> float:
            # par instant, jamais par texte ISO : +00:00 (formulaire) et +02:00 (creneaux) (fable-publication M3)
            instant = _publish_entry_instant(row, "slot_at")
            return instant.timestamp() if instant is not None else float("-inf")

        rows.sort(key=slot_instant, reverse=True)
        return {
            "publications": rows,
            "refused_by_platform": refused,
            "removed_from_platform": removed,
            "accounts": _publish_accounts(config),
            "defaults": {
                "options": {key: settings[key] for key in ("visibility", "allow_comments", "allow_reuse",
                                                            "ai_generated", "content_check")},
                "schedule_max_days": settings["schedule_max_days"],
                "schedule_min_minutes": settings["schedule_min_minutes"],
                "youtube": {
                    "options": {key: yt_settings[key] for key in ("visibility", "made_for_kids")},
                    "schedule_max_days": yt_settings["schedule_max_days"],
                    "schedule_min_minutes": yt_settings["schedule_min_minutes"],
                },
            },
        }

    @app.post("/api/publications", status_code=201)
    def create_publication(body: PublicationBody) -> dict[str, Any]:
        _validate_video_id(body.video_id)
        _validate_clip_id(body.clip_id)
        account = _require_ready_account(config, body.account)
        publish_at = _publish_parse_slot(body.publish_at) if body.publish_at else None
        channel = _channel_of(body.video_id, config)
        entry = _publication_call(
            publish_mod.create_post, body.video_id, body.clip_id, channel, account=account, mode=body.mode,
            publish_at=publish_at, options=body.options, caption=body.description, hashtags=body.hashtags,
            schedule=_account_schedule(config, account), **_publication_scope(_account_service(config, account)))
        return _publication_view(channel or publish_mod.NO_CHANNEL, entry)

    @app.get("/api/publications/after-last")
    def publication_after_last(account: str, interval_hours: float) -> dict[str, Any]:
        """« Après la dernière programmation » (SPEC-1ed3) : la date proposee par « Nouvelle publication »,
        affichee avant validation ; les refus habituels (fenetre, avance minimale, plafonds) se verifient a
        la creation, comme toute publication programmee."""
        account = _require_ready_account(config, account)
        if not interval_hours > 0:
            raise HTTPException(status_code=422, detail=f"intervalle invalide : {interval_hours!r} (attendu : un nombre d'heures positif)")
        when = publish_mod.after_last_schedule(
            account, interval_hours, state_dir=_publish_dir(config), presets_dir=_PRESETS_DIR, base=_BASE_CONFIG)
        return {"publish_at": when.isoformat(), "publish_at_paris": _paris(when.isoformat())}

    @app.patch("/api/publications/{video_id}/{clip_id}")
    def update_publication(video_id: str, clip_id: str, body: PublicationPatchBody) -> dict[str, Any]:
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        sent = body.model_fields_set
        changes: dict[str, Any] = {}
        if "account" in sent and body.account is not None:
            changes["account"] = _require_ready_account(config, body.account)
        if "mode" in sent and body.mode is not None:
            changes["mode"] = body.mode
        if "publish_at" in sent:
            changes["publish_at"] = _publish_parse_slot(body.publish_at) if body.publish_at else None
        if "options" in sent and body.options is not None:
            changes["options"] = body.options
        channel = _channel_of(video_id, config)
        account = changes.get("account")
        if account is None:  # compte inchange : le service est celui du compte deja retenu par l'entree
            current = _publish_entries(config, channel or publish_mod.NO_CHANNEL).get((video_id, clip_id))
            account = current.get("account") if current else None
        try:
            service = _account_service(config, account) if account else "tiktok"
            schedule = _account_schedule(config, account) if account else None
        except HTTPException:
            if "account" in changes:
                raise
            service, schedule = "tiktok", None  # compte de l'entree disparu de l'ecran Comptes : sans effet sur la validation
        entry = _publication_call(
            publish_mod.update_post, video_id, clip_id, channel, caption=body.description, hashtags=body.hashtags,
            schedule=schedule, **changes, **_publication_scope(service))
        return _publication_view(channel or publish_mod.NO_CHANNEL, entry)

    @app.delete("/api/publications/{video_id}/{clip_id}", status_code=204)
    def cancel_publication(video_id: str, clip_id: str) -> Response:
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        _publication_call(publish_mod.cancel_post, video_id, clip_id, _channel_of(video_id, config),
                          state_dir=_publish_dir(config))
        return Response(status_code=204)

    # ----------------------------------------------------------------
    # Série programmée (TASK-5bbf, SPEC-1ed3, SPEC-6076 R3/R6) : choix auto
    # (N meilleurs clips) ou manuel (ordre choisi), apercu obligatoire puis
    # création tout ou rien. Tout le calcul (dates, choix des clips, refus)
    # passe par clipper.publish ; aucune logique ici au-dela de la mise en
    # forme de la reponse.
    # ----------------------------------------------------------------

    def _series_scope(service: str) -> dict[str, Any]:
        return {"workspace_dir": Path(config.workspace_dir), "output_dir": Path(config.output_dir),
                "state_dir": _publish_dir(config), "presets_dir": _PRESETS_DIR, "base": _BASE_CONFIG,
                "settings": _publication_settings(config, service)}

    @app.get("/api/publications/series/clips")
    def series_clips(style: str | None = None, account: str | None = None, together: bool = True) -> dict[str, Any]:
        """Clips disponibles pour le mode manuel du formulaire : une unite par serie (parties regroupees et
        triees), les clips deja valides (approuves, sans creneau) ET les clips prets jamais entres en file
        (``validated`` les distingue) ; aucune restriction de compte ici (TASK-16eeaccfaf09 : la restriction de
        compte ne vaut que pour le choix automatique, voir ``preview_series_endpoint``). ``together``=False
        (coche « Parties ensemble » decochee, TASK-fc561e4dc7e9) : chaque partie est sa propre unite. ``account``
        donne ``available`` : le nombre de posts disponibles en mode auto pour ce compte (le max du champ
        « Nombre de vidéos »), ``None`` sans compte."""
        scope = {"workspace_dir": Path(config.workspace_dir), "output_dir": Path(config.output_dir),
                 "state_dir": _publish_dir(config)}
        units = publish_mod.available_series_clips(style or None, together=together, **scope)
        out = _series_units_view(units)
        available = publish_mod.auto_series_capacity(style or None, account, together=together, **scope) if account else None
        return {"units": out, "default_interval_h": config.section("publish")["series_default_interval_h"],
                "available": available}

    def _series_units_view(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for unit in units:
            lead = _read_clip_sidecar(config, unit["video_id"], unit["clip_ids"][0])
            out.append({
                "video_id": unit["video_id"], "clip_id": unit["clip_ids"][0], "clip_ids": unit["clip_ids"],
                "channel": unit["channel"], "score": unit["score"], "parts_total": len(unit["clip_ids"]),
                "screen_title": lead.get("screen_title"), "title": lead.get("title"),
                "thumbnail_url": f"/media/clip/{unit['video_id']}/{unit['clip_ids'][0]}/thumbnail",
                "validated": unit["validated"],
            })
        return out

    def _series_view(item: dict[str, Any]) -> dict[str, Any]:
        sidecar = _read_clip_sidecar(config, item["video_id"], item["clip_id"])
        return {
            "video_id": item["video_id"], "clip_id": item["clip_id"], "channel": item["channel"],
            "score": item["score"], "screen_title": sidecar.get("screen_title"), "title": sidecar.get("title"),
            "thumbnail_url": f"/media/clip/{item['video_id']}/{item['clip_id']}/thumbnail",
            "part": sidecar.get("part"), "parts_total": sidecar.get("parts_total"),
            "publish_at": item["publish_at"], "publish_at_paris": _paris(item["publish_at"]),
            "refusal": item["refusal"],
        }

    def _series_kwargs(body: SeriesBody) -> dict[str, Any]:
        account = _require_ready_account(config, body.account)
        service = _account_service(config, account)
        selection = [(s.video_id, s.clip_id) for s in (body.selection or [])] if body.mode == "manual" else None
        # Coche « Heure par clip » : une date par clip (TASK-fa00f90a735a), validee clip par clip cote Python.
        clip_dates = (
            {(d.video_id, d.clip_id): _publish_parse_slot(d.publish_at) for d in body.clip_dates}
            if body.clip_dates is not None else None
        )
        return dict(
            mode=body.mode, style=body.style or None, account=account, service=service,
            interval_hours=body.interval_hours,
            start_at=_publish_parse_slot(body.start_at) if body.start_at is not None else None,
            count=body.count, selection=selection, clip_dates=clip_dates, together=body.parts_together,
            schedule=_account_schedule(config, account),
            **_series_scope(service),
        )

    @app.post("/api/publications/series/preview")
    def preview_series_endpoint(body: SeriesBody) -> dict[str, Any]:
        kwargs = _series_kwargs(body)
        preview = _publication_call(publish_mod.preview_series, **kwargs)
        return {**preview, "items": [_series_view(it) for it in preview["items"]]}

    @app.post("/api/publications/series", status_code=201)
    def create_series_endpoint(body: SeriesBody) -> dict[str, Any]:
        kwargs = _series_kwargs(body)
        created = _publication_call(publish_mod.create_series, **kwargs)
        return {"created": len(created)}

    # ----------------------------------------------------------------
    # Répartition automatique du lendemain (TASK-486c, SPEC-78dc R8) : lire, recalculer, modifier, valider le
    # plan. Le calcul est celui de clipper.repartition, les refus ceux de publish.preview_series, la création
    # celle de publish.create_series ; ici seulement la mise en forme, le verrou du fichier et les 409.
    # ----------------------------------------------------------------

    def _rep_call(fn, *args: Any, **kwargs: Any) -> Any:
        try:
            return _accounts_call(_publication_call, fn, *args, **kwargs)
        except repartition_mod.RepartitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def _rep_day(day: str | None) -> str:
        if day is None:  # demain, en heure de Paris
            return (datetime.now(_PARIS) + timedelta(days=1)).date().isoformat()
        try:
            return date.fromisoformat(day).isoformat()
        except ValueError:
            raise HTTPException(status_code=422, detail=f"jour invalide : {day!r} (AAAA-MM-JJ est attendu)") from None

    def _rep_editable(day: str, then: str) -> dict[str, Any]:
        """Plan ``proposed`` du jour (à lire sous le verrou du fichier) : 404 sans plan, 409 s'il est validé ou en erreur."""
        plan = _rep_call(repartition_mod.read_plan, day, config)
        if plan is None:
            raise HTTPException(status_code=404, detail=f"aucun plan pour le {day} : calcule-le d'abord (Recalculer)")
        if plan.get("status") == "validated":
            raise HTTPException(status_code=409, detail=f"le plan du {day} est déjà validé : {then}")
        if plan.get("status") != "proposed":
            message = (plan.get("error") or {}).get("message")
            raise HTTPException(status_code=409, detail=f"le plan du {day} est en erreur ({message}) : recalcule-le")
        return plan

    def _rep_refusals(account_id: str, lines: list[dict[str, Any]]) -> dict[tuple[str, str], str | None]:
        """``refusal`` de chaque ligne d'un compte, issu de ``publish.preview_series`` (mode manuel, une date par clip,
        chaque clip seul) : plafonds, avance minimale, créneau déjà pris. Une ligne dont le clip n'est plus
        choisissable est refusée seule, les autres restent prévisualisées ensemble."""
        if not lines:
            return {}
        try:
            schedule = _account_schedule(config, account_id)
        except HTTPException as exc:
            return {(line["video_id"], line["clip_id"]): str(exc.detail) for line in lines}
        common = dict(mode="manual", style=None, account=account_id, service="tiktok", together=False,
                      schedule=schedule, **_series_scope("tiktok"))

        def preview(subset: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return publish_mod.preview_series(
                selection=[(line["video_id"], line["clip_id"]) for line in subset],
                clip_dates={(line["video_id"], line["clip_id"]): _publish_parse_slot(line["slot_at"]) for line in subset},
                **common)["items"]

        out: dict[tuple[str, str], str | None] = {}
        try:
            items = preview(lines)
        except publish_mod.PublishError:
            keep = []
            for line in lines:
                try:
                    preview([line])
                    keep.append(line)
                except publish_mod.PublishError as exc:
                    out[(line["video_id"], line["clip_id"])] = str(exc)
            items = preview(keep) if keep else []
        out.update({(item["video_id"], item["clip_id"]): item["refusal"] for item in items})
        return out

    def _rep_clip_fields(video_id: str, clip_id: str) -> dict[str, Any]:
        try:
            sidecar = _read_clip_sidecar(config, video_id, clip_id)
        except HTTPException:  # clip supprimé depuis le calcul : la ligne reste visible, sans titre
            sidecar = {}
        return {"screen_title": sidecar.get("screen_title"), "title": sidecar.get("title"),
                "thumbnail_url": f"/media/clip/{video_id}/{clip_id}/thumbnail"}

    def _rep_view(plan: dict[str, Any] | None, day: str) -> dict[str, Any]:
        """Le fichier du jour avec, par ligne, titre, vignette, heure de Paris et ``refusal`` ; ``pool`` = les clips
        choisissables par ce compte pour remplacer une ligne, sous chaque compte (``pool_size`` = le nombre de R7)."""
        base: dict[str, Any] = {"day": day, "status": "absent", "accounts": []} if plan is None else dict(plan)
        base.update(enabled=config.section("repartition")["enabled"], pool_size=None if plan is None else plan.get("pool"))
        settings = _rep_call(repartition_mod.read_settings, config)
        world = repartition_mod.World(config)
        accounts_out = []
        made = {(m["account"], m["video_id"], m["clip_id"]) for m in base.get("created") or []}
        entries = [] if base["status"] == "validated" else _rep_call(
            publish_mod.all_entries, state_dir=_publish_dir(config), presets_dir=_PRESETS_DIR)
        for account in base.get("accounts") or []:
            account_id = account["account"]
            # une ligne déjà créée est en file : la prévisualiser la refuserait, c'est précisément ce qui est fait
            todo = [] if base["status"] == "validated" else [
                line for line in account["lines"] if (account_id, line["video_id"], line["clip_id"]) not in made]
            refusals = _rep_refusals(account_id, todo)
            for line in todo:  # les exclusions rejouées (source, file de traitement...) priment sur la prévisualisation
                problem = _rep_call(repartition_mod.line_error, world, account_id, day, video_id=line["video_id"],
                                    clip_id=line["clip_id"], slot_at=_publish_parse_slot(line["slot_at"]), entries=entries)
                if problem:
                    refusals[(line["video_id"], line["clip_id"])] = problem
            lines = [{**line, **_rep_clip_fields(line["video_id"], line["clip_id"]),
                      "publish_at_paris": _paris(line["slot_at"]),
                      "created": (account_id, line["video_id"], line["clip_id"]) in made,
                      "refusal": refusals.get((line["video_id"], line["clip_id"])),
                      "warning": line.get("warning")} for line in account["lines"]]
            pool = _series_units_view(repartition_mod.account_pool(account["account"], world=world, settings=settings))
            accounts_out.append({**account, "lines": lines, "pool": pool})
        base["accounts"] = accounts_out
        return base

    @app.get("/api/repartition")
    def get_repartition(day: str | None = None) -> dict[str, Any]:
        day = _rep_day(day)
        return _rep_view(_rep_call(repartition_mod.read_plan, day, config), day)

    @app.post("/api/repartition/compute")
    def compute_repartition(body: RepartitionComputeBody | None = None) -> dict[str, Any]:
        day = _rep_day(body.day if body else None)
        plan = _rep_call(repartition_mod.compute_plan, day, datetime.now(_PARIS), config=config, computed_by="web")
        return _rep_view(plan, day)

    def _rep_new_line(line: RepartitionLineBody, settings: dict[str, Any], world: Any, stats: tuple[Any, Any]) -> dict[str, Any]:
        """Une ligne redécrite depuis le clip (score, bonus, source, exploration, soir) : jamais depuis la page."""
        _validate_video_id(line.video_id)
        _validate_clip_id(line.clip_id)
        slot = _publish_parse_slot(line.slot_at).astimezone(_PARIS)
        sidecar = _read_clip_sidecar(config, line.video_id, line.clip_id)
        return repartition_mod.describe_line(world, settings, stats, video_id=line.video_id, clip_id=line.clip_id,
                                             slot_at=slot, score=sidecar.get("score"))

    def _rep_warnings(plan: dict[str, Any], day: str, settings: dict[str, Any], world: Any,
                      entries: list[tuple[str, dict[str, Any]]]) -> None:
        """``warning`` de chaque ligne (c'est le choix de l'utilisateur, jamais un refus) : plus de ``max_per_source``
        clips d'une même source par compte et par jour, publications déjà prévues comprises (R3) ; plus de
        ``exploration_per_day`` clips d'exploration par jour, ou un clip d'exploration le soir (R6)."""
        repartition_mod.line_warnings(plan, day, settings, world, entries)

    @app.put("/api/repartition/{day}")
    def put_repartition(day: str, body: RepartitionPutBody) -> dict[str, Any]:
        day = _rep_day(day)
        path = _rep_call(repartition_mod.plan_path, day, config)
        settings = _rep_call(repartition_mod.read_settings, config)
        with channel_mod.file_lock(path):
            plan = _rep_editable(day, "il n'est plus modifiable")
            by_id = {account["account"]: account for account in plan["accounts"]}
            sent = [account.account for account in body.accounts]
            unknown = [a for a in sent if a not in by_id]
            if unknown or len(set(sent)) != len(sent):
                raise HTTPException(status_code=422, detail=f"comptes du plan : {sorted(by_id)} ; reçus : {sent}")
            locked = {made["account"] for made in plan.get("created") or []} & set(sent)
            if locked:
                raise HTTPException(status_code=409, detail=f"publications déjà créées pour {sorted(locked)} : "
                                    "ces comptes ne sont plus modifiables (valide le plan pour terminer)")
            taken = {(line["video_id"], line["clip_id"]) for acc_id, account in by_id.items() if acc_id not in sent
                     for line in account["lines"]}
            for account in body.accounts:
                for line in account.lines:
                    if (line.video_id, line.clip_id) in taken:
                        raise HTTPException(status_code=422, detail=f"clip présent deux fois dans le plan : "
                                            f"{line.video_id}/{line.clip_id}")
                    taken.add((line.video_id, line.clip_id))
            world = repartition_mod.World(config)
            entries = _rep_call(publish_mod.all_entries, state_dir=_publish_dir(config), presets_dir=_PRESETS_DIR)
            for account in body.accounts:
                for line in account.lines:
                    _validate_video_id(line.video_id)
                    _validate_clip_id(line.clip_id)
                    _read_clip_sidecar(config, line.video_id, line.clip_id)  # clip inconnu : 404 avant tout autre refus
                    problem = _rep_call(repartition_mod.line_error, world, account.account, day, video_id=line.video_id,
                                        clip_id=line.clip_id, slot_at=_publish_parse_slot(line.slot_at), entries=entries)
                    if problem:
                        raise HTTPException(status_code=422, detail=problem)
            active = [a for a in _accounts_call(accounts_mod.list_accounts, config)
                      if a["service"] == "tiktok" and a["ready_to_publish"] and not a.get("paused_at")]
            stats = _rep_call(repartition_mod.source_stats, world, active, datetime.now(_PARIS), settings)
            for account in body.accounts:
                by_id[account.account]["lines"] = sorted(
                    (_rep_new_line(line, settings, world, stats) for line in account.lines),
                    key=lambda found: datetime.fromisoformat(found["slot_at"]))
            _rep_warnings(plan, day, settings, world, entries)
            plan["edited_at"] = _now_iso()
            channel_mod.atomic_write_json(path, plan)
        return _rep_view(plan, day)

    @app.post("/api/repartition/{day}/validate")
    def validate_repartition(day: str) -> Any:
        """Crée une entrée programmée par ligne, compte après compte (``create_series`` : tout ou rien par compte).
        Un compte refusé arrête la validation : les comptes déjà créés restent créés (``created``), le plan garde
        ``proposed`` avec ``last_error`` ; valider de nouveau ne recrée que les comptes restants."""
        day = _rep_day(day)
        path = _rep_call(repartition_mod.plan_path, day, config)
        with channel_mod.file_lock(path):
            plan = _rep_editable(day, "rien à créer de plus")
            if not any(account["lines"] for account in plan["accounts"]):
                raise HTTPException(status_code=409, detail=f"le plan du {day} n'a aucune ligne : rien à valider")
            created = list(plan.get("created") or [])
            done = {(made["account"], made["video_id"], made["clip_id"]) for made in created}
            failure: dict[str, Any] | None = None
            world = repartition_mod.World(config)
            entries = _rep_call(publish_mod.all_entries, state_dir=_publish_dir(config), presets_dir=_PRESETS_DIR)
            for account in plan["accounts"]:
                lines = [line for line in account["lines"]
                         if (account["account"], line["video_id"], line["clip_id"]) not in done]
                if not lines:
                    continue
                try:
                    account_id = _require_ready_account(config, account["account"])
                    for line in lines:  # exclusions rejouées à la validation : config ou file changées depuis le calcul
                        problem = _rep_call(repartition_mod.line_error, world, account_id, day,
                                            video_id=line["video_id"], clip_id=line["clip_id"],
                                            slot_at=_publish_parse_slot(line["slot_at"]), entries=entries)
                        if problem:
                            where = f"{line['video_id']}/{line['clip_id']}"
                            raise HTTPException(status_code=409,
                                                detail=problem if where in problem else f"{where} : {problem}")
                    _publication_call(
                        publish_mod.create_series, mode="manual", style=None, account=account_id, service="tiktok",
                        selection=[(line["video_id"], line["clip_id"]) for line in lines],
                        clip_dates={(line["video_id"], line["clip_id"]): _publish_parse_slot(line["slot_at"])
                                    for line in lines},
                        together=False, schedule=_account_schedule(config, account_id), **_series_scope("tiktok"))
                except (HTTPException, _LimitRefused) as exc:
                    failure = {"detail": exc.detail, "account": account["account"],
                               "next_at": getattr(exc, "next_at", None)}
                    break
                created.extend({"account": account["account"], "video_id": line["video_id"],
                                "clip_id": line["clip_id"]} for line in lines)
            plan["created"] = created
            if failure is not None:
                plan["last_error"] = failure["detail"]
                channel_mod.atomic_write_json(path, plan)
                return JSONResponse(status_code=409, content={**failure, "status": "proposed", "created": created})
            plan.update(status="validated", validated_at=datetime.now(_PARIS).isoformat())
            plan.pop("last_error", None)
            channel_mod.atomic_write_json(path, plan)
        return _rep_view(plan, day)

    @app.get("/api/publish/{video_id}/{clip_id}/capture")
    def publish_capture(video_id: str, clip_id: str) -> FileResponse:
        """Capture d'ecran d'un arret sur (SPEC-9225 R4) : seulement un .png sous
        state/browser/<compte>/captures/, jamais un chemin lu ailleurs."""
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        channel = _require_channel(video_id, clip_id, config, or_no_channel=True)
        entry = _publish_entries(config, channel).get((video_id, clip_id))
        captured = entry.get("capture") if entry else None
        if not captured:
            raise HTTPException(status_code=404, detail=f"aucune capture pour {video_id}/{clip_id}")
        path = Path(captured).resolve()
        root = browser_mod.profile_dir("x", config).parent.resolve()
        if path.suffix != ".png" or not path.is_file() or root not in path.parents or path.parent.name != "captures":
            raise HTTPException(status_code=404, detail=f"capture introuvable : {captured}")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/tiktok/events")
    def tiktok_events(since: str | None = None) -> list[dict[str, Any]]:
        """Notifications de publication (arrets sur, reports, succes) ecrites par le worker."""
        try:
            return tiktok_mod.read_events(since, config=config)
        except tiktok_mod.TikTokError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ----------------------------------------------------------------
    # Surveillance : VOD a confirmer (SPEC-74e9 §5.3)
    # ----------------------------------------------------------------

    @app.post("/api/watch/{channel}/{video_id}/confirm", status_code=202)
    def watch_confirm(channel: str, video_id: str) -> JSONResponse:
        _validate_channel_name(channel)
        _validate_video_id(video_id)
        try:
            entry = watch_mod.confirm(channel, video_id, config=config)
        except watch_mod.WatchError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(entry, status_code=202)

    @app.post("/api/watch/{channel}/{video_id}/ignore")
    def watch_ignore(channel: str, video_id: str) -> dict[str, Any]:
        _validate_channel_name(channel)
        _validate_video_id(video_id)
        try:
            watch_mod.ignore(channel, video_id, config=config)
        except watch_mod.WatchError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"channel": channel, "video_id": video_id, "ignored": True}

    # ----------------------------------------------------------------
    # Veille des sujets chauds (SPEC-bdd9 R9) : lecture + demandes seulement
    # ----------------------------------------------------------------

    @app.get("/api/veille")
    def get_veille() -> dict[str, Any]:
        return _veille_view(config, None)

    @app.post("/api/veille/refresh", status_code=202)
    def veille_refresh() -> dict[str, Any]:
        view = _veille_view(config, None)
        if not view["enabled"]:
            raise HTTPException(status_code=409, detail="la veille est désactivée : l'activer dans Réglages › Veille")
        if view["running"]:
            raise HTTPException(status_code=409, detail="un relevé de la veille est déjà en cours")
        request = {"requested_at": _now_iso()}
        path = _veille_dir(config) / "refresh.json"
        channel_mod.atomic_write_json(path, request)
        return request

    @app.get("/api/veille/{day}")
    def get_veille_day(day: str) -> dict[str, Any]:
        view = _veille_view(config, day) if _VEILLE_DATE.fullmatch(day) else None
        if view is None or view["day"] is None:
            raise HTTPException(status_code=404, detail=f"aucun relevé de veille pour le {day}")
        return view

    @app.post("/api/veille/clips/{video_id}/{clip_id}/restore")
    def veille_restore(video_id: str, clip_id: str) -> dict[str, Any]:
        _validate_video_id(video_id)
        _validate_clip_id(clip_id)
        try:
            veille_mod.restore(video_id, clip_id, config=config)
        except veille_mod.VeilleError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"video_id": video_id, "clip_id": clip_id, "restored": True}

    def _veille_decide(action: str, day: str, candidate_id: str, *args: Any) -> Any:
        try:
            return getattr(veille_mod, action)(day, candidate_id, *args, config=config)
        except veille_mod.VeilleError as exc:
            message = str(exc)
            status = 409 if "déjà traité" in message else 404 if ("inconnu" in message or "aucun relevé" in message) else 422
            raise HTTPException(status_code=status, detail=message) from exc

    @app.post("/api/veille/{day}/{candidate_id}/clip", status_code=202)
    def veille_clip(day: str, candidate_id: str, body: VeilleClipBody | None = None) -> JSONResponse:
        body = body or VeilleClipBody()
        if body.channel:
            _validate_channel_name(body.channel)
        entry = _veille_decide("clip", day, candidate_id, body.channel or None, body.short_clips)
        return JSONResponse(entry, status_code=202)

    @app.post("/api/veille/{day}/{candidate_id}/ignore")
    def veille_ignore(day: str, candidate_id: str) -> dict[str, Any]:
        _veille_decide("ignore", day, candidate_id)
        return {"date": day, "candidate_id": candidate_id, "ignored": True}

    # ----------------------------------------------------------------
    # Chaines (SPEC-74e9 §1) et temps reel (ADR-35b7 §4)
    # ----------------------------------------------------------------

    @app.get("/api/channels")
    def list_channels_route() -> list[str]:
        return channel_mod.list_channels(_PRESETS_DIR)

    @app.post("/api/channels", status_code=201)
    def create_channel(body: ChannelCreateBody) -> dict[str, Any]:
        _check_channel_name(body.name)
        if (Path(_PRESETS_DIR) / f"{body.name}.toml").exists():
            raise HTTPException(status_code=409, detail=f"le style {body.name!r} existe déjà")
        _refuse_legacy_keys(body.preset or {})
        Path(_PRESETS_DIR).mkdir(parents=True, exist_ok=True)
        _save_channel_preset(body.name, body.preset or {"channel": {}})
        return _channel_detail(body.name)

    @app.get("/api/rubric-label")
    def rubric_label(path: str) -> dict[str, str]:
        return _rubric_info(path)

    @app.get("/api/channels/{name}")
    def get_channel(name: str) -> dict[str, Any]:
        return _channel_detail(name)

    @app.put("/api/channels/{name}")
    def put_channel(name: str, body: ChannelBody) -> dict[str, Any]:
        _channel_preset_path(name)
        _refuse_legacy_keys(body.preset)
        _save_channel_preset(name, body.preset)
        return _channel_detail(name)

    @app.delete("/api/channels/{name}")
    def delete_channel_route(name: str, confirm: bool = False) -> dict[str, Any]:
        _check_channel_name(name)
        if not confirm:
            raise HTTPException(
                status_code=409,
                detail=f"confirmation requise (confirm=true) : supprimer le style {name!r} efface son preset",
            )
        try:
            entries = publish_mod.list_entries(name, state_dir=_publish_dir(config))
        except (publish_mod.PublishError, OSError, ValueError) as exc:  # fichier illisible : fable-comptes 4
            raise HTTPException(status_code=409, detail=f"file de publication du style {name!r} illisible "
                                f"({name}.json) : {exc}") from exc
        # Les entrees terminees restent dans state/publish/<style>.json : publish les lit toujours (plafonds du
        # compte, ecran Publication), meme sans preset (revue fable-comptes 4).
        pending = [e for e in entries if e["status"] in publish_mod.UNFINISHED_STATUSES]
        if pending:
            clips = ", ".join(f"{e['video_id']}/{e['clip_id']}" for e in pending)
            raise HTTPException(
                status_code=409,
                detail=(f"le style {name!r} a {len(pending)} publication(s) non terminée(s) ({clips}) : "
                        "annule-les ou attends leur fin avant de le supprimer"),
            )
        try:
            channel_mod.delete_channel(name, presets_dir=_PRESETS_DIR)
        except channel_mod.ChannelError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        Path(_PRESETS_DIR, f"{name}.png").unlink(missing_ok=True)
        return {"name": name, "deleted": True}

    @app.get("/api/channels/{name}/subtitles-preview")
    def channel_subtitles_preview(name: str, text: str = "", draft: str | None = None) -> Response:
        config = _subspreview_config(name, draft)
        try:
            png = pipeline.preview_subtitles(config, text)
        except pipeline.PipelineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/channels/{name}/logo")
    async def upload_channel_logo(name: str, request: Request) -> dict[str, Any]:
        _channel_preset_path(name)
        data = _png_from_multipart(request.headers.get("content-type", ""), await request.body())
        if len(data) > _LOGO_MAX_BYTES:
            raise HTTPException(status_code=422, detail=f"logo trop lourd (maximum {_LOGO_MAX_BYTES // _MIB} Mo)")
        target = Path(_PRESETS_DIR) / f"{name}.png"
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return {"name": name, "logo": f"{_PRESETS_DIR}/{name}.png"}

    # ----------------------------------------------------------------
    # Reglages (SPEC-c100 E8) : config.toml, relu a chaque requete
    # ----------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return _settings_detail(web_cfg)

    @app.put("/api/settings")
    def put_settings(body: SettingsBody) -> dict[str, Any]:
        nonlocal config
        raw, _exists, _text = _settings_read_raw()
        try:
            merged = _settings_merge(raw, body.settings)
            if "veille" in body.settings:  # mêmes bornes que la veille elle-même (VeilleError -> 400)
                try:
                    veille_mod.settings(Config(mode="review", workspace_dir=Path("."), output_dir=Path("."),
                                               _sections={"veille": merged["veille"]}))
                except veille_mod.VeilleError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            write_config(_BASE_CONFIG, merged)
            config = load_config(_BASE_CONFIG)  # les entrees de file suivantes lisent le nouveau mode/backend
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"valeur non enregistrable : {exc}") from exc
        app.state.config = config
        return _settings_detail(web_cfg)

    # ----------------------------------------------------------------
    # Journal (TASK-8067) : lecture seule des fichiers logs/journal-*.log
    # ----------------------------------------------------------------

    @app.get("/api/network")
    def get_network() -> dict[str, Any]:
        """Pays de l'IP publique (TASK-30cc) : ``ok`` True / False / None (inconnu), cache ``[network] cache_s``."""
        return network_mod.status(config)

    @app.get("/api/journal")
    def get_journal(lines: int = 200, q: str | None = None, level: str | None = None) -> dict[str, Any]:
        return journal_mod.tail(config, limit=lines, text=q, level=level)

    @app.get("/api/channels/{name}/keyframe")
    def channel_keyframe(name: str, video_id: str | None = None) -> FileResponse:
        _channel_preset_path(name)
        return FileResponse(_layout_keyframe_path(config, name, video_id), media_type="image/jpeg")

    @app.get("/api/channels/{name}/layout")
    def get_channel_layout(name: str) -> dict[str, Any]:
        return _layout_view(name)

    @app.put("/api/channels/{name}/layout")
    def put_channel_layout(name: str, body: LayoutBody) -> dict[str, Any]:
        _layout_save(name, body.model_dump())
        return _layout_view(name)

    @app.get("/api/channels/{name}/layout/letterbox")
    def get_channel_letterbox_layout(name: str) -> dict[str, Any]:
        return _letterbox_view(name)

    @app.put("/api/channels/{name}/layout/letterbox")
    def put_channel_letterbox_layout(name: str, body: LetterboxLayoutBody) -> dict[str, Any]:
        given = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        if not given:
            raise HTTPException(status_code=422, detail=f"aucun réglage à enregistrer (attendu : {', '.join(_LETTERBOX_KEYS)})")
        _letterbox_write(name, given)
        return _letterbox_view(name)

    @app.delete("/api/channels/{name}/layout/letterbox")
    def delete_channel_letterbox_layout(name: str) -> dict[str, Any]:
        _letterbox_write(name, None)
        return _letterbox_view(name)
    # ----------------------------------------------------------------
    # Apprentissage (ADR-c260, SPEC-00db R6-R7) : lecture de l'etat et decision humaine sur le coach.
    # Aucune route ne calcule ni n'appelle un LLM : le worker seul verse, recalibre et coache (ADR-09ad).
    # ----------------------------------------------------------------

    @app.get("/api/learning")
    def get_learning() -> dict[str, Any]:
        try:
            return learning_mod.status(config)
        except learning_mod.LearningError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _learning_decide(judge: str, version: int, status: str) -> dict[str, Any]:
        nonlocal config
        try:
            if judge in learning_mod.EXCLUDED_JUDGES:
                raise learning_mod.ProposalError(f"le juge {judge} n'est jamais coaché", 409)
            proposal = learning_mod.find_proposal(config, judge, version)
            if proposal["status"] != "proposed":
                raise learning_mod.ProposalError(f"proposition {judge} v{version} déjà {proposal['status']}", 409)
            comments_lost = False
            if status == "adopted":
                perspective = learning_mod.proposal_perspective(proposal["path"])
                raw, _exists, text = _settings_read_raw()
                comments_lost = _settings_has_comments(text)
                data = dict(raw)
                jury_table = dict(data.get("jury") or {})
                judges = dict(jury_table.get("judges") or {})
                judges[judge] = {**(judges.get(judge) or {}), "perspective": perspective}
                data["jury"] = {**jury_table, "judges": judges}
                write_config(_BASE_CONFIG, data)
                config = load_config(_BASE_CONFIG)  # les prochains jugements lisent la perspective adoptee
                app.state.config = config
            decided = learning_mod.decide_proposal(config, judge, version, status, by="web")
        except learning_mod.ProposalError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except (learning_mod.LearningError, ConfigError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {**decided, "comments_lost": comments_lost}

    @app.post("/api/learning/coach/{judge}/{version}/adopt")
    def adopt_coach_proposal(judge: str, version: int) -> dict[str, Any]:
        return _learning_decide(judge, version, "adopted")

    @app.post("/api/learning/coach/{judge}/{version}/refuse")
    def refuse_coach_proposal(judge: str, version: int) -> dict[str, Any]:
        return _learning_decide(judge, version, "refused")

    # ----------------------------------------------------------------
    # Statistiques (SPEC-c100 E7)
    # ----------------------------------------------------------------

    @app.get("/api/measures")
    def measures(since: str | None = None, until: str | None = None, channel: str | None = None) -> dict[str, Any]:
        """Mesures internes (couts LLM, durees d'etapes, videos par statut) : le Tableau de bord les affiche."""
        return _measures(config, since, until, channel)

    @app.get("/api/stats/tiktok")
    async def stats_tiktok_accounts() -> dict[str, Any]:
        """Les comptes de l'ecran Statistiques : etat du releve (date, nombre de releves, dernier arret sur),
        chaine liee, si le compte est pret a etre releve, si son releve est perime et s'il est en cours (SPEC-47e2 R3, R4)."""
        accounts = []
        for found in _accounts_call(accounts_mod.list_accounts, config):
            info = _stats_account_info(config, found["id"])
            history = _stats_tiktok_call(tiktok_mod.read_history, found["id"], config=config)
            full = [s for s in history if s.get("origin") == "full"]
            stale = info["ready"] and _stats_tiktok_call(tiktok_mod.stats_stale, found["id"], config=config)
            known = len(full[-1]["posts"]) if full else 0
            accounts.append({**info, "fetched_at": history[-1]["fetched_at"] if history else None,
                             "known_posts": known, "full_pages": 2 + 3 * known if known else None,
                             "last_full_at": full[-1]["fetched_at"] if full else None, "snapshots": len(history),
                             "error": _stats_tiktok_call(tiktok_mod.read_error, found["id"], config=config),
                             "stale": bool(stale), "refreshing": stats_refreshes.running(found["id"]),
                             "refresh_error": stats_refreshes.failure(found["id"])})
        return {"accounts": accounts}

    @app.get("/api/stats/tiktok/{account_id}")
    def stats_tiktok_overview(account_id: str, period: int = 28) -> dict[str, Any]:
        """Vue d'ensemble d'un compte sur ``period`` jours (7, 28, 60 ou 365) : 5 tuiles avec evolution, courbes par jour."""
        info = _stats_account_info(config, account_id)
        return {**info, **_stats_tiktok_call(tiktok_mod.account_overview, account_id, period, config=config)}

    @app.get("/api/stats/tiktok/{account_id}/videos")
    def stats_tiktok_videos(account_id: str, sort: str = "posted_at", dir: str = "desc", q: str = "") -> dict[str, Any]:
        """Toutes les videos du compte vues dans TikTok Studio, triables (``sort``, ``dir`` asc|desc) et filtrables (``q``)."""
        if dir not in ("asc", "desc"):
            raise HTTPException(status_code=422, detail=f"sens de tri invalide : {dir!r} (attendu : asc | desc)")
        info = _stats_account_info(config, account_id)
        videos = _stats_tiktok_call(tiktok_mod.list_videos, account_id, sort=sort, descending=dir == "desc",
                                    query=q, config=config)
        return {**info, "videos": videos}

    @app.get("/api/stats/tiktok/{account_id}/videos/{post_id}")
    def stats_tiktok_video(account_id: str, post_id: str) -> dict[str, Any]:
        """Fiche d'une video : chiffres cles, retention, spectateurs, engagement, liens TikTok / clip / video source."""
        info = _stats_account_info(config, account_id)
        return {**info, "video": _stats_tiktok_call(tiktok_mod.video_detail, account_id, post_id, config=config)}

    @app.post("/api/stats/tiktok/open")
    async def stats_tiktok_open(request: Request) -> dict[str, Any]:
        """Ouverture de l'ecran Statistiques pour ``{"account": id}`` (SPEC-47e2 R4a) : si le dernier releve du compte
        a plus de ``[tiktok] stats_stale_min`` minutes (jamais releve : perime ; 0 : jamais a l'ouverture), lance un
        releve en tache de fond et rend tout de suite ; l'ecran lit ``refreshing`` sur GET /api/stats/tiktok. Un compte
        non pret n'est pas releve (``reason``), un releve deja en cours n'est pas relance (``running``)."""
        account = _stats_body_account(await request.body(), required=True)
        info = _stats_account_info(config, account)
        if not info["ready"]:
            return {"account": account, "started": False, "running": False, "stale": False, "reason": info["not_ready_reason"]}
        stale = bool(_stats_tiktok_call(tiktok_mod.stats_stale, account, config=config))
        running = stats_refreshes.running(account)
        started = stale and not running and stats_refreshes.claim(account)
        if started:
            threading.Thread(target=_stats_background_fetch, args=(account,), name=f"stats-{account}", daemon=True).start()
        return {"account": account, "started": started, "running": started or running, "stale": stale, "reason": None}

    def _stats_background_fetch(account: str) -> None:
        failure = None
        try:
            tiktok_mod.fetch_stats(account, config=config)
        except Exception as exc:  # noqa: BLE001 - jamais perdu : journalise et affiche sur le compte
            failure = str(exc) if isinstance(exc, (tiktok_mod.TikTokError, browser_mod.BrowserError)) else f"{type(exc).__name__} : {exc}"
            logger.error("relevé des statistiques TikTok du compte %s impossible : %s", account, failure)
        finally:
            stats_refreshes.release(account, failure)

    @app.post("/api/stats/tiktok/refresh")
    async def stats_tiktok_refresh(request: Request) -> dict[str, Any]:
        """Releve a la demande (« Relever maintenant », SPEC-47e2 R4c ; ``"full": true`` = « Relevé complet », detail sans plafond) : un compte (``{"account": id}``) ou, sans corps,
        tous les comptes prets. Un compte non pret n'est pas releve (409 avec la raison). Un compte dont un releve
        tourne deja n'est pas relance : son entree vaut ``{"running": true}``. Ouvre le Chrome visible du profil sur
        cette machine ; un arret sur (R4 de SPEC-9225) est une 409 avec sa raison."""
        account, full = _stats_refresh_body(await request.body())
        wanted = [account] if account is not None else None
        known = {a["id"]: a for a in _accounts_call(accounts_mod.list_accounts, config)}
        for account in wanted or []:
            info = _stats_account_info(config, account)
            if not info["ready"]:
                raise HTTPException(status_code=409, detail=f"compte {info['label']} non prêt à publier, pas de relevé : "
                                                            f"{info['not_ready_reason']}")
        accounts = wanted if wanted is not None else [a for a, found in known.items() if _stats_relevable(found)]
        if not accounts:
            raise HTTPException(status_code=409, detail="aucun compte prêt à publier : rien à relever")
        done: dict[str, Any] = {}
        for account in accounts:
            if not stats_refreshes.claim(account):
                done[account] = {"running": True, "fetched_at": None, "posts": None}  # deja en cours : rien n'est relance
                continue
            try:
                options = {"full": True} if full else {}  # le releve normal garde le plafond du detail
                report = await run_in_threadpool(tiktok_mod.fetch_stats, account, config=config, **options)
            except (tiktok_mod.TikTokStop, browser_mod.BrowserError) as exc:
                raise HTTPException(status_code=409, detail=f"relevé du compte {account} arrêté : {exc}") from exc
            except tiktok_mod.TikTokError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            finally:
                stats_refreshes.release(account)
            done[account] = {"fetched_at": report["fetched_at"], "posts": len(report["posts"])}
        return {"accounts": done}

    # ----------------------------------------------------------------
    # Comptes (SPEC-6fa4)
    # ----------------------------------------------------------------

    @app.get("/api/accounts")
    async def accounts_list() -> list[dict[str, Any]]:
        listed = await run_in_threadpool(_accounts_overview, config)  # connexion verifiee a l'ouverture (R2)
        return [{**a, "browser": _browser_state(a["id"], config)} for a in listed]

    @app.put("/api/accounts/{account_id}/ready")
    async def accounts_ready(account_id: str) -> dict[str, Any]:
        raise HTTPException(
            status_code=405, headers={"Allow": "GET"},
            detail="« prêt à publier » est automatique (connexion TikTok vérifiée, aucun arrêt en attente) : "
                   "il ne se coche ni ne se décoche à la main ; clique sur « Se connecter » ou « J'ai réglé le problème », "
                   "ou mets le compte en pause / reprends-le (POST /api/accounts/{id}/pause et /resume)",
        )

    @app.post("/api/accounts/{account_id}/pause")
    async def accounts_pause(account_id: str) -> dict[str, Any]:
        return await run_in_threadpool(_account_pause, config, account_id)

    @app.post("/api/accounts/{account_id}/resume")
    async def accounts_resume(account_id: str) -> dict[str, Any]:
        return await run_in_threadpool(_account_resume, config, account_id)

    @app.post("/api/accounts/{account_id}/resolve")
    async def accounts_resolve(account_id: str) -> dict[str, Any]:
        return await run_in_threadpool(_account_resolve, config, account_id)

    @app.post("/api/accounts/{account_id}/verify")
    async def accounts_verify(account_id: str) -> dict[str, Any]:
        """Revérifie un compte YouTube (R1) : ouvre YouTube Studio sur son profil et enregistre la chaîne vue,
        ou la raison du refus. Compte TikTok : 409 (sa connexion se lit dans ses cookies, sans navigation)."""
        return await run_in_threadpool(_account_verify, config, account_id)

    @app.post("/api/accounts", status_code=201)
    async def accounts_add(request: Request) -> dict[str, Any]:
        return await run_in_threadpool(_accounts_call, accounts_mod.add_account, config, await _accounts_json(request))

    @app.post("/api/accounts/generate")
    async def accounts_generate(request: Request) -> dict[str, str]:
        body = await _accounts_json(request)
        if not isinstance(body, dict) or set(body) - {"length", "symbols", "avoid_ambiguous"}:
            raise HTTPException(status_code=422, detail="corps invalide : length, symbols, avoid_ambiguous attendus")
        if not all(isinstance(body.get(k, False), bool) for k in ("symbols", "avoid_ambiguous")):
            raise HTTPException(status_code=422, detail="symbols et avoid_ambiguous : un booléen est attendu")
        password = _accounts_call(
            accounts_mod.generate_password, config, body.get("length"),
            symbols=body.get("symbols", False), avoid_ambiguous=body.get("avoid_ambiguous", False),
        )
        return {"password": password}

    @app.put("/api/accounts/{account_id}")
    async def accounts_update(account_id: str, request: Request) -> dict[str, Any]:
        return await run_in_threadpool(
            _accounts_call, accounts_mod.update_account, config, account_id, await _accounts_json(request)
        )

    @app.delete("/api/accounts/{account_id}", status_code=204)
    async def accounts_delete(account_id: str) -> Response:
        await run_in_threadpool(_accounts_call, accounts_mod.delete_account, config, account_id)
        return Response(status_code=204)

    @app.get("/api/accounts/{account_id}/password")
    async def accounts_password(account_id: str) -> dict[str, str]:
        password = await run_in_threadpool(_accounts_call, accounts_mod.get_password, config, account_id)
        return {"password": password}

    @app.get("/api/accounts/{account_id}/browser")
    async def accounts_browser_state(account_id: str) -> dict[str, Any]:
        await run_in_threadpool(_require_account, config, account_id)
        return _browser_state(account_id, config)

    @app.post("/api/accounts/{account_id}/browser/login", status_code=202)
    async def accounts_browser_login(account_id: str, request: Request) -> dict[str, Any]:
        await run_in_threadpool(_require_account, config, account_id)
        raw = await request.body()
        body = await _accounts_json(request) if raw.strip() else {}
        if not isinstance(body, dict) or set(body) - {"url"}:
            raise HTTPException(status_code=422, detail="corps invalide : un objet {\"url\": ...} facultatif est attendu")
        url = body.get("url")
        if url is not None and not isinstance(url, str):
            raise HTTPException(status_code=422, detail="url : une chaîne est attendue")
        await run_in_threadpool(_browser_login, config, account_id, url)
        return {"account": account_id, "status": "opened"}

    @app.get("/api/events")
    def events_stream() -> StreamingResponse:
        return StreamingResponse(_event_stream(config), media_type="text/event-stream")

    # ----------------------------------------------------------------
    # Media
    # ----------------------------------------------------------------

    @app.get("/media/source/{video_id}")
    def media_source(video_id: str) -> FileResponse:
        if not _SAFE_ID.fullmatch(video_id):
            raise HTTPException(status_code=404, detail=f"identifiant invalide : {video_id!r}")
        path = _safe_video_file(Path(config.workspace_dir) / video_id, video_id)
        return FileResponse(path, media_type="video/mp4")

    @app.get("/media/source/{video_id}/thumbnail")
    def media_source_thumbnail(video_id: str) -> FileResponse:
        # Le web ne traite jamais de video (ADR-09ad) : pipeline.video_thumbnail extrait et met en cache.
        if not _SAFE_ID.fullmatch(video_id):
            raise HTTPException(status_code=404, detail=f"identifiant invalide : {video_id!r}")
        _safe_video_file(Path(config.workspace_dir) / video_id, video_id)  # 404 si la source n'existe pas
        try:
            path = pipeline.video_thumbnail(config, video_id)
        except pipeline.PipelineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/media/clip/{video_id}/{clip_id}")
    def media_clip(video_id: str, clip_id: str) -> FileResponse:
        if not _SAFE_ID.fullmatch(video_id):
            raise HTTPException(status_code=404, detail=f"identifiant invalide : {video_id!r}")
        path = _safe_video_file(Path(config.output_dir) / video_id, clip_id)
        return FileResponse(path, media_type="video/mp4")

    @app.get("/media/clip/{video_id}/{clip_id}/thumbnail")
    def media_clip_thumbnail(video_id: str, clip_id: str) -> FileResponse:
        # Le web ne traite jamais de video (ADR-09ad) : pipeline.clip_thumbnail extrait et met en cache.
        if not _SAFE_ID.fullmatch(video_id):
            raise HTTPException(status_code=404, detail=f"identifiant invalide : {video_id!r}")
        _safe_video_file(Path(config.output_dir) / video_id, clip_id)  # 404 si le clip n'existe pas
        try:
            path = pipeline.clip_thumbnail(config, video_id, clip_id)
        except pipeline.PipelineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    return app


# --------------------------------------------------------------------------
# Ecran Publication (SPEC-c100 E6, SPEC-74e9 §4) : vue d'une semaine de
# creneaux d'une chaine. Les creneaux viennent de channel.next_slots, les
# entrees de state/publish/<chaine>.json, les clips des sidecars ; l'API
# n'invente aucun statut (une entree sans sidecar est signalee `missing`).
# --------------------------------------------------------------------------


class PublishMoveBody(BaseModel):
    slot_at: str


class AccountBody(BaseModel):
    account: str | None = None


class RemovedBody(BaseModel):
    reason: str | None = None


class PublishModeBody(BaseModel):
    mode: str | None = None


class PublicationBody(BaseModel):
    """Formulaire « Nouvelle publication » (SPEC-1ed3 R1, R2)."""
    video_id: str
    clip_id: str
    account: str
    mode: str
    publish_at: str | None = None
    options: dict[str, Any] | None = None
    description: str | None = None
    hashtags: list[str] | None = None


class SeriesSelectionItem(BaseModel):
    video_id: str
    clip_id: str


class SeriesClipDate(BaseModel):
    video_id: str
    clip_id: str
    publish_at: str                             # date ISO avec fuseau (coche « Heure par clip »)


class SeriesBody(BaseModel):
    """Formulaire « Programmer une série » (TASK-5bbf, SPEC-1ed3, SPEC-6076 R3/R6)."""
    mode: str                                   # auto | manual
    style: str | None = None                    # None = tous les styles
    account: str
    interval_hours: int | None = None           # requis sans ``clip_dates``
    start_at: str | None = None                 # requis sans ``clip_dates``
    count: int | None = None                    # requis en mode auto
    selection: list[SeriesSelectionItem] | None = None  # requis en mode manuel, dans l'ordre choisi
    clip_dates: list[SeriesClipDate] | None = None      # coche « Heure par clip » (manuel) : une date par clip
    parts_together: bool = True                 # coche « Parties ensemble » (TASK-fc561e4dc7e9), ON par defaut


class RepartitionComputeBody(BaseModel):
    """« Recalculer » (SPEC-78dc R8) : le jour planifié, demain (Paris) par défaut."""
    day: str | None = None


class RepartitionLineBody(BaseModel):
    """Une ligne du plan telle que l'écran la renvoie : seuls le créneau et le clip comptent, le reste est relu
    depuis le clip (jamais pris dans la page)."""
    slot_at: str                                # date ISO avec fuseau
    video_id: str
    clip_id: str


class RepartitionAccountBody(BaseModel):
    account: str
    lines: list[RepartitionLineBody]


class RepartitionPutBody(BaseModel):
    accounts: list[RepartitionAccountBody]      # les comptes absents gardent leurs lignes


class PublicationPatchBody(BaseModel):
    account: str | None = None
    mode: str | None = None
    publish_at: str | None = None
    options: dict[str, Any] | None = None
    description: str | None = None
    hashtags: list[str] | None = None


def _publish_parse_slot(value: str) -> datetime:
    try:
        slot = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"slot_at invalide : {value!r} (attendu : date ISO avec fuseau)") from exc
    if slot.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"slot_at sans fuseau horaire : {value!r} (attendu : date ISO avec décalage, ex. +02:00)")
    return slot


_PUBLISH_RANGES = ("day", "week", "month")


def _publish_anchor_date(week: str | None, tz: ZoneInfo) -> date:
    """Jour ancrant la plage demandee : celui du parametre ``week`` (malgre son nom, SPEC-c100 ; il ancre
    jour et mois aussi), ou aujourd'hui s'il est absent."""
    if not week:
        return datetime.now(tz).date()
    try:
        return date.fromisoformat(week)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"paramètre week invalide : {week!r} (attendu : AAAA-MM-JJ)") from exc


def _publish_range_bounds(range_kind: str, anchor: date) -> tuple[date, date]:
    """(debut inclus, fin exclue) de la plage, en jours, pour ``range_kind`` (TASK-ad4d)."""
    if range_kind == "day":
        return anchor, anchor + timedelta(days=1)
    if range_kind == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=7)
    start = anchor.replace(day=1)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month


def _publish_date_span(start: date, end: date) -> list[date]:
    days, day = [], start
    while day < end:
        days.append(day)
        day += timedelta(days=1)
    return days


def _publish_entry_instant(entry: dict[str, Any], key: str) -> datetime | None:
    value = entry.get(key)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"fichier de publication illisible : {key} invalide pour {entry.get('video_id')}/{entry.get('clip_id')} ({value!r})",
        ) from exc


_PUBLISH_DEFAULT_TZ = "Europe/Paris"


def _publish_clip_view(clips: dict[tuple[str, str], dict[str, Any]], channel: str | None, entry: dict[str, Any]) -> dict[str, Any]:
    clip = clips.get((entry["video_id"], entry["clip_id"]))
    if clip is not None:
        return clip
    return {
        "video_id": entry["video_id"], "clip_id": entry["clip_id"], "channel": channel, "missing": True,
        "publish_status": entry["status"], "slot_at": entry.get("slot_at"), "publish_error": entry.get("error"),
        "screen_title": None, "description": None, "hashtags": [], "video_url": None, "thumbnail_url": None,
        **_tiktok_fields(entry, entry["video_id"], entry["clip_id"]),
    }


def _publish_account_entries(config: Config, account: str | None) -> list[tuple[str | None, dict[str, Any]]]:
    """(style du fichier ou None, entree) de toute la file de publication dont le compte est ``account`` (tous
    les comptes si None). Le compte d'une entree est celui enregistre dedans (SPEC-6076 R2) : un post d'une
    video sans style y figure donc aussi."""
    try:
        found = publish_mod.all_entries(state_dir=_publish_dir(config), presets_dir=_PRESETS_DIR)
    except publish_mod.PublishError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"fichier de publication illisible : {exc}") from exc
    rows: list[tuple[str | None, dict[str, Any]]] = []
    for name, entry in found:
        if account is not None and publish_mod.entry_account(entry) != account:
            continue
        rows.append((None if name == publish_mod.NO_CHANNEL else name, entry))
    return rows


def _publish_account_schedule(config: Config, account: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(creneaux du compte, raison sans creneau) : les creneaux appartiennent au compte (SPEC-6076 R2)."""
    if account is None:
        return None, "choisis un compte pour voir ses créneaux"
    schedule = _account_schedule(config, account)
    return schedule, None if schedule["slots"] else "aucun créneau défini pour ce compte (écran Comptes)"


def _publish_range_view(config: Config, account: str | None, range_param: str | None, week: str | None) -> dict[str, Any]:
    range_kind = range_param or "week"
    if range_kind not in _PUBLISH_RANGES:
        raise HTTPException(
            status_code=422,
            detail=f"paramètre range invalide : {range_kind!r} (attendu : day, week ou month)",
        )
    if account is not None and not any(a["id"] == account for a in _publish_accounts(config)):
        raise HTTPException(status_code=404, detail=f"compte inconnu : {account!r} (écran Comptes)")
    channel, reason = _publish_account_schedule(config, account)
    tz = ZoneInfo(str(channel["timezone"]) if channel else _PUBLISH_DEFAULT_TZ)
    anchor = _publish_anchor_date(week, tz)
    start_date, end_date = _publish_range_bounds(range_kind, anchor)
    start = datetime.combine(start_date, time(0, 0), tzinfo=tz)
    end = datetime.combine(end_date, time(0, 0), tzinfo=tz)
    # Créneaux réguliers = cibles de dépôt seulement en Jour et Semaine (TASK-ad4d) ; la vue Mois n'en a pas besoin.
    has_slot_grid = range_kind in ("day", "week")

    rows = _publish_account_entries(config, account)
    clips = {(c["video_id"], c["clip_id"]): c for c in _list_clip_views(config, None, None, None)}
    slots_def = channel["slots"] if (channel and has_slot_grid) else []
    by_slot: dict[datetime, dict[str, Any]] = {}
    unscheduled, done, off_slot = [], [], []
    by_day: dict[date, list[tuple[datetime, dict[str, Any]]]] = {d: [] for d in _publish_date_span(start_date, end_date)}
    slot_instants = {s for s in channel_mod.next_slots(channel, start - timedelta(microseconds=1), len(slots_def) + 1)
                     if s < end} if slots_def else set()
    for file_channel, entry in rows:
        slot = _publish_entry_instant(entry, "slot_at")
        published = _publish_entry_instant(entry, "published_at")
        if slot is not None and entry["status"] in ("scheduled", "published", "failed"):
            by_slot[slot] = entry
        if entry["status"] == "scheduled" and slot is not None and start <= slot < end and slot not in slot_instants:
            off_slot.append(_publish_clip_view(clips, file_channel, entry))  # publication manuelle entre les créneaux
        if entry["status"] == "approved" and slot is None:
            unscheduled.append(_publish_clip_view(clips, file_channel, entry))
        elif entry["status"] in ("published", "failed"):
            # le créneau (jamais réécrit après coup, SPEC-1ed3) place la case : un post programmé sur le service
            # (scheduled_on_tiktok / youtube) reste à sa date de direct, même décidé (published_at) une autre semaine.
            when = slot or published
            if when is not None and start <= when < end:
                done.append(_publish_clip_view(clips, file_channel, entry))
        # Case du jour (TASK-ad4d) : TOUTE publication datée de la plage y figure une fois, créneau occupé,
        # hors créneau, publiée ou en échec confondus ; une liste (jamais une clé par heure) ne perd aucune
        # publication quand deux tombent à la même minute.
        when_for_day = slot if (entry["status"] == "scheduled" and slot is not None) else None
        if entry["status"] in ("published", "failed"):
            when_for_day = slot or published
        if when_for_day is not None and start <= when_for_day < end:
            by_day[when_for_day.astimezone(tz).date()].append((when_for_day, _publish_clip_view(clips, file_channel, entry)))

    days = []
    for day in sorted(by_day):
        posts = [post for _, post in sorted(by_day[day], key=lambda pair: (pair[0], pair[1]["video_id"], pair[1]["clip_id"]))]
        days.append({"date": day.isoformat(), "count": len(posts), "posts": posts})

    slots = []
    if slots_def:
        seen: set[datetime] = set()
        by_key = {(e["video_id"], e["clip_id"]): f for f, e in rows}
        for slot in channel_mod.next_slots(channel, start - timedelta(microseconds=1), len(slots_def) + 1):
            if slot >= end or slot in seen:
                continue
            seen.add(slot)
            entry = by_slot.get(slot)
            slots.append({
                "slot_at": slot.isoformat(), "slot_at_paris": _paris(slot.isoformat()),
                "clip": _publish_clip_view(clips, by_key[(entry["video_id"], entry["clip_id"])], entry) if entry is not None else None,
                "free": entry is None,
            })
    range_start, range_end = start_date.isoformat(), (end_date - timedelta(days=1)).isoformat()
    return {
        "account": account, "timezone": str(tz.key), "range": range_kind,
        "range_start": range_start, "range_end": range_end,
        "week_start": range_start, "week_end": range_end,  # alias historique (comportement de week= inchangé)
        "days": days,
        "slots": slots, "unscheduled": unscheduled, "done": done, "off_slot": sorted(off_slot, key=lambda c: datetime.fromisoformat(c["slot_at"])),
        "accounts": _publish_accounts(config), "reason": reason,
    }
