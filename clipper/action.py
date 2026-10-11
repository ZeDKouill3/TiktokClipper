"""Etape action : passages d'action des VOD gaming (SPEC-b0f3 R4-R9, ADR-4e57).

Detection deterministe (aucun LLM) a partir de signaux deja mesures par les
etapes precedentes, puis description par clipper.llm (usage ``action``,
modele rapide) d'images fixes que scenes a deja extraites : cette etape
n'extrait jamais d'image et ne lance aucun sous-processus.

Entrees (workspace/<video_id>/) : audio.json (pics), scenes.json (plans et
images cles, produit avec ``peak_windows`` quand cette etape est activee),
transcript.json (segments de parole), meta.json (``duration``).

Sortie : workspace/<video_id>/action.json (format : SPEC-b0f3 R9)

    {"video_id", "enabled", "settings", "passages": [{"id", "start", "end",
     "duration", "score", "signals", "frames": [{"timecode", "path",
     "description", "action_type", "intensity"}], "frames_missing"}],
     "rejected": [{"start", "end", "score", "signals", "reason"}],
     "llm_calls", "images_sent"}

Detection : des fenetres de ``window_seconds`` tous les ``step_seconds``
recoivent un score (pics audio et densite de changements de plan, ponderes) ;
celles de score >= ``min_score`` qui se touchent fusionnent en passages, coupes
a ``max_passage_seconds``, plafonnes par heure de VOD. Les images (au plus
``frames_per_passage`` par passage, ``max_images_per_hour`` au total) partent
au LLM par lots : une planche (clipper.montage) par appel, jusqu'a
``parallel`` lots a la fois, chaque lot reussi etant garde dans
action_partial.json pour la relance. Reponse invalide ou LLM indisponible :
l'erreur remonte, action.json n'est pas ecrit (ADR-ad2e).

Desactivee (``enabled = false``, le defaut), l'etape ecrit un resultat vide
sans lire d'entree ni appeler le LLM.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from clipper import llm
from clipper import montage as montage_lib

log = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    # Étape désactivée par défaut : action.json vide, le découpage en plans ignore audio.json.
    "enabled": False,
    # Fenêtre d'analyse (s) et pas entre deux fenêtres.
    "window_seconds": 30,
    "step_seconds": 15,
    # Poids des deux signaux dans le score d'une fenêtre.
    "audio_weight": 1.0,
    # Pics audio d'une fenêtre qui valent un signal complet (1.0).
    "audio_peaks_full": 3,
    # Seuil (dB au-dessus du fond) sous lequel un pic audio ne compte pas.
    "audio_peak_min_db": 6.0,
    "cuts_weight": 1.0,
    # Densité de plans (multiple de la médiane de la vidéo) qui vaut un signal complet.
    "cuts_ratio_full": 3.0,
    # Score minimal d'une fenêtre pour entrer dans un passage.
    "min_score": 0.6,
    "max_passage_seconds": 90,
    # Plafonds par heure de VOD : passages et images envoyées au LLM.
    "max_passages_per_hour": 12,
    "frames_per_passage": 4,
    "max_images_per_hour": 48,
    # Images par planche (un appel LLM par planche) et largeur max de chacune.
    "batch_size": 8,
    "max_width": 768,
    # Lots traités en même temps.
    "parallel": 4,
}

ACTION_TYPES = (
    "combat", "clutch", "mort", "victoire", "defaite", "retournement", "sursaut",
    "exploration", "menu_ou_chargement", "webcam_ou_chat_seul", "autre",
)

NO_PEAK_WINDOWS_MESSAGE = (
    "scenes.json produit sans fenêtres de pics : relancer scenes avec --force "
    "(style avec [action] enabled = true)"
)

REASON_PASSAGES_CAP = "plafond max_passages_per_hour"
REASON_IMAGES_CAP = "plafond max_images_per_hour"

_INT_KEYS_MIN_ONE = (
    "max_passages_per_hour", "frames_per_passage", "max_images_per_hour", "batch_size", "max_width", "parallel",
)


class ActionError(Exception):
    """Entree manquante ou invalide, ou reglage [action] hors bornes."""


# --------------------------------------------------------------------------
# Reglages
# --------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _settings(config: Any) -> dict[str, Any]:
    if config is None:
        from clipper.config import load_config

        config = load_config()
    settings = {**CONFIG_DEFAULTS, **config.section("action")}
    _validate(settings)
    return settings


def _validate(s: dict[str, Any]) -> None:
    def refuse(key: str, rule: str) -> None:
        raise ActionError(f"[action] {key} {rule} (recu {s[key]!r})")

    if not isinstance(s["enabled"], bool):
        refuse("enabled", "doit etre true ou false")
    for key in ("window_seconds", "step_seconds", "audio_weight", "cuts_weight", "audio_peaks_full",
                "audio_peak_min_db", "cuts_ratio_full", "min_score", "max_passage_seconds"):
        if not _is_number(s[key]):
            refuse(key, "doit etre un nombre")
    for key in _INT_KEYS_MIN_ONE:
        if not isinstance(s[key], int) or isinstance(s[key], bool) or s[key] < 1:
            refuse(key, "doit etre un entier >= 1")
    if s["window_seconds"] <= 0:
        refuse("window_seconds", "doit etre > 0")
    if not 0 < s["step_seconds"] <= s["window_seconds"]:
        refuse("step_seconds", "doit etre > 0 et <= window_seconds")
    for key in ("audio_weight", "cuts_weight"):
        if s[key] < 0:
            refuse(key, "doit etre >= 0")
    if s["audio_weight"] + s["cuts_weight"] <= 0:
        raise ActionError("[action] audio_weight et cuts_weight : leur somme doit etre > 0")
    for key in ("audio_peaks_full", "cuts_ratio_full"):
        if s[key] <= 0:
            refuse(key, "doit etre > 0")
    if not 0 < s["min_score"] <= 1:
        refuse("min_score", "doit etre > 0 et <= 1")
    if s["max_passage_seconds"] < s["window_seconds"]:
        refuse("max_passage_seconds", "doit etre >= window_seconds")


# --------------------------------------------------------------------------
# Entrees
# --------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise ActionError(f"entree absente : {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Detection R6
# --------------------------------------------------------------------------


def _windows(duration: float, audio: dict[str, Any], scenes: dict[str, Any], transcript: dict[str, Any],
             s: dict[str, Any]) -> list[dict[str, Any]]:
    window, step = float(s["window_seconds"]), float(s["step_seconds"])
    min_db = float(s["audio_peak_min_db"])
    peaks = [p for p in audio.get("peaks") or [] if float(p["relative_db"]) >= min_db]
    cut_starts = [float(sc["start"]) for sc in scenes.get("scenes") or []]
    segments = transcript.get("segments") or []

    raw: list[dict[str, Any]] = []
    index = 0
    while index * step < duration:
        start = index * step
        end = min(start + window, duration)
        in_window = [p for p in peaks if start <= float(p["timecode"]) < start + window]
        speech = sum(
            max(0.0, min(float(seg["end"]), start + window) - max(float(seg["start"]), start)) for seg in segments
        )
        raw.append({
            "start": start,
            "end": end,
            "audio_peaks": len(in_window),
            "audio_peak_max_db": max((float(p["relative_db"]) for p in in_window), default=0.0),
            "scene_cuts": sum(1 for c in cut_starts if start <= c < start + window),
            "speech_ratio": min(1.0, speech / window),
        })
        index += 1

    median = statistics.median(w["scene_cuts"] for w in raw) if raw else 0
    for w in raw:
        w["scene_cuts_ratio"] = w["scene_cuts"] / median if median else float(w["scene_cuts"])
        audio_part = min(1.0, w["audio_peaks"] / float(s["audio_peaks_full"]))
        cuts_part = min(1.0, w["scene_cuts_ratio"] / float(s["cuts_ratio_full"]))
        weights = float(s["audio_weight"]) + float(s["cuts_weight"])
        w["score"] = (float(s["audio_weight"]) * audio_part + float(s["cuts_weight"]) * cuts_part) / weights
    return raw


def _merge_runs(windows: list[dict[str, Any]], min_score: float) -> list[list[dict[str, Any]]]:
    """Fenetres de score suffisant qui se chevauchent ou se touchent, par groupe."""
    runs: list[list[dict[str, Any]]] = []
    for w in windows:
        if w["score"] < min_score:
            continue
        if runs and w["start"] <= runs[-1][-1]["end"]:
            runs[-1].append(w)
        else:
            runs.append([w])
    return runs


def _split_run(run: list[dict[str, Any]], max_seconds: float) -> list[tuple[float, float, list[dict[str, Any]]]]:
    """Un groupe de fenetres en tranches de ``max_seconds`` au plus, alignees sur
    ses fenetres les mieux notees (a egalite, la plus tot). Une fenetre appartient
    a la tranche qui contient son milieu ; les tranches ne se chevauchent pas."""
    lo_all, hi_all = run[0]["start"], run[-1]["end"]
    if hi_all - lo_all <= max_seconds:
        return [(lo_all, hi_all, run)]
    chunks: list[tuple[float, float]] = []
    remaining = list(run)
    while remaining:
        best = max(remaining, key=lambda w: (w["score"], -w["start"]))
        mid = (best["start"] + best["end"]) / 2
        below = [hi for lo, hi in chunks if hi <= mid]
        above = [lo for lo, hi in chunks if lo >= mid]
        free_lo = max(below, default=lo_all)
        free_hi = min(above, default=hi_all)
        lo, hi = max(best["start"], free_lo), min(best["end"], free_hi)
        extra = max_seconds - (hi - lo)
        lo, hi = max(free_lo, lo - extra / 2), min(free_hi, hi + extra / 2)
        extra = max_seconds - (hi - lo)
        if extra > 1e-9:
            lo, hi = max(free_lo, lo - extra), min(free_hi, hi + extra)
        chunks.append((lo, hi))
        remaining = [w for w in remaining if not any(c_lo <= (w["start"] + w["end"]) / 2 < c_hi for c_lo, c_hi in chunks)]
    out = []
    for lo, hi in sorted(chunks):
        members = [w for w in run if lo <= (w["start"] + w["end"]) / 2 < hi]
        out.append((lo, hi, members or [max(run, key=lambda w: w["score"])]))
    return out


def _passage(lo: float, hi: float, members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "start": round(lo, 2),
        "end": round(hi, 2),
        "duration": round(hi - lo, 2),
        "score": round(max(w["score"] for w in members), 4),
        "_rank": max(w["score"] for w in members),
        "signals": {
            "audio_peaks": sum(w["audio_peaks"] for w in members),
            "audio_peak_max_db": max(w["audio_peak_max_db"] for w in members),
            "scene_cuts": sum(w["scene_cuts"] for w in members),
            "scene_cuts_ratio": round(statistics.fmean(w["scene_cuts_ratio"] for w in members), 4),
            "speech_ratio": round(statistics.fmean(w["speech_ratio"] for w in members), 4),
        },
    }


def _detect(duration: float, audio: dict[str, Any], scenes: dict[str, Any], transcript: dict[str, Any],
            s: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    windows = _windows(duration, audio, scenes, transcript, s)
    passages: list[dict[str, Any]] = []
    for run in _merge_runs(windows, float(s["min_score"])):
        for lo, hi, members in _split_run(run, float(s["max_passage_seconds"])):
            passages.append(_passage(lo, hi, members))
    ranked = sorted(passages, key=lambda p: (-p["_rank"], p["start"]))
    cap = math.ceil(int(s["max_passages_per_hour"]) * duration / 3600)
    kept, rejected = ranked[:cap], []
    for p in ranked[cap:]:
        rejected.append(_rejection(p, REASON_PASSAGES_CAP))
    return kept, rejected


def _rejection(passage: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "start": passage["start"], "end": passage["end"], "score": passage["score"],
        "signals": passage["signals"], "reason": reason,
    }


# --------------------------------------------------------------------------
# Images R7
# --------------------------------------------------------------------------


def _pick_frames(passage: dict[str, Any], frames: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    inside = [f for f in frames if passage["start"] <= float(f["timecode"]) <= passage["end"]]
    chosen: list[dict[str, Any]] = []
    for k in range(1, count + 1):
        free = [f for f in inside if f not in chosen]
        if not free:
            break
        target = passage["start"] + k * passage["duration"] / (count + 1)
        chosen.append(min(free, key=lambda f: (abs(float(f["timecode"]) - target), float(f["timecode"]))))
    return sorted(chosen, key=lambda f: float(f["timecode"]))


def _attach_frames(kept: list[dict[str, Any]], rejected: list[dict[str, Any]], frames: list[dict[str, Any]],
                   duration: float, s: dict[str, Any]) -> list[dict[str, Any]]:
    """Images des passages pris dans l'ordre du classement, sous le plafond
    d'images ; le passage qui le ferait depasser et les suivants sont rejetes."""
    cap = math.ceil(int(s["max_images_per_hour"]) * duration / 3600)
    used = 0
    accepted: list[dict[str, Any]] = []
    for position, passage in enumerate(kept):
        picked = _pick_frames(passage, frames, int(s["frames_per_passage"]))
        if used + len(picked) > cap:
            for later in kept[position:]:
                rejected.append(_rejection(later, REASON_IMAGES_CAP))
            break
        used += len(picked)
        passage["_picked"] = picked
        accepted.append(passage)
    return accepted


# --------------------------------------------------------------------------
# Description par le LLM R8
# --------------------------------------------------------------------------


def _response_schema(n: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "frames": {
                "type": "array",
                "minItems": n,
                "maxItems": n,
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 0, "maximum": n - 1},
                        "description": {
                            "type": "string", "minLength": 1, "maxLength": 300,
                            "description": "Ce qui se passe dans le jeu, en une phrase concrete.",
                        },
                        "action_type": {"type": "string", "enum": list(ACTION_TYPES)},
                        "intensity": {
                            "type": "integer", "minimum": 0, "maximum": 10,
                            "description": "0 : rien ne se passe ; 10 : action tres intense.",
                        },
                    },
                    "required": ["index", "description", "action_type", "intensity"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["frames"],
        "additionalProperties": False,
    }


def _prompt(batch: list[dict[str, Any]]) -> str:
    listing = "\n".join(f"Image {n} : {f['timecode']:.1f} s" for n, f in enumerate(batch))
    return (
        "Tu aides un monteur de clips verticaux courts tires de VOD de jeux video (streams). La planche "
        f"jointe est une grille de {len(batch)} images extraites de la video, cote a cote dans cet ordre, "
        "chacune legendee sur l'image avec son index et son timecode :\n"
        f"{listing}\n\n"
        "Pour chaque image (index = son numero) : description = ce qui se passe DANS LE JEU, en une phrase "
        "concrete (combat, mort, victoire, menu, chargement, streamer seul a la webcam...) ; action_type = "
        f"le type d'action parmi {', '.join(ACTION_TYPES)} ; intensity = entier de 0 (rien ne se passe) a 10 "
        "(action tres intense). Sois exigeant : un menu, un chargement ou la seule webcam valent 0 ou 1."
    )


def _load_partial(path: Path) -> dict[int, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(k): v for k, v in (data.get("batches") or {}).items()}


def _describe(frames: list[dict[str, Any]], video_dir: Path, s: dict[str, Any], config: Any,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Description de chaque image, par ``path``, et nombre d'appels LLM reellement faits :
    un lot (une planche) par appel, reprise par action_partial.json (un lot repris n'est pas
    un appel), dossier des planches supprime en fin."""
    batch_size = int(s["batch_size"])
    batches = [frames[first : first + batch_size] for first in range(0, len(frames), batch_size)]
    partial_path = video_dir / "action_partial.json"
    results: list[list[dict[str, Any]] | None] = [None] * len(batches)
    for n, saved in _load_partial(partial_path).items():
        if n < len(batches) and saved.get("paths") == [f["path"] for f in batches[n]]:
            results[n] = saved["frames"]
    lock = threading.Lock()

    def save() -> None:
        batches_out = {
            str(n): {"paths": [f["path"] for f in batches[n]], "frames": r}
            for n, r in enumerate(results) if r is not None
        }
        _write_json(partial_path, {"batches": batches_out})

    montage_dir = video_dir / "action_montage_tmp"

    def process(n: int) -> None:
        batch = batches[n]
        try:
            board = montage_lib.montage(batch, video_dir, montage_dir, int(s["max_width"]), n)
        except montage_lib.MontageError as exc:
            raise ActionError(str(exc)) from exc
        answer = llm.ask("action", _prompt(batch), [board], _response_schema(len(batch)), config=config)
        indices = [item["index"] for item in answer["frames"]]
        if sorted(indices) != list(range(len(batch))):
            raise llm.SchemaError(f"action : index attendus 0..{len(batch) - 1}, recus {indices}")
        described = [
            {
                "timecode": batch[item["index"]]["timecode"],
                "path": batch[item["index"]]["path"],
                "description": item["description"],
                "action_type": item["action_type"],
                "intensity": item["intensity"],
            }
            for item in sorted(answer["frames"], key=lambda item: item["index"])
        ]
        with lock:
            results[n] = described
            save()

    pending = [n for n in range(len(batches)) if results[n] is None]
    if pending:
        montage_dir.mkdir(exist_ok=True)
    try:
        errors: dict[int, Exception] = {}
        if pending:
            with ThreadPoolExecutor(max_workers=max(1, int(s["parallel"]))) as executor:
                futures = {executor.submit(process, n): n for n in pending}
                for future in as_completed(futures):
                    n = futures[future]
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001 - la premiere (dans l'ordre des lots) est relevee
                        errors[n] = exc
                        continue
                    log.debug("action : lot %d/%d decrit", n + 1, len(batches))
        if errors:
            raise errors[next(n for n in pending if n in errors)]
    finally:
        if montage_dir.exists():
            for leftover in montage_dir.iterdir():
                leftover.unlink()
            montage_dir.rmdir()

    return {item["path"]: item for batch_result in results for item in (batch_result or [])}, len(pending)


# --------------------------------------------------------------------------
# Etape
# --------------------------------------------------------------------------


def run(
    video_id: str,
    workspace_dir: str | Path = "workspace",
    *,
    config: Any = None,
    force: bool = False,
) -> Path:
    """Detecte et decrit les passages d'action, ecrit workspace/<video_id>/action.json
    et renvoie son chemin. Un resultat deja present n'est pas refait, sauf ``force``."""
    video_dir = Path(workspace_dir) / video_id
    out = video_dir / "action.json"
    settings = _settings(config)
    if out.exists() and not force:
        return out

    if not settings["enabled"]:
        video_dir.mkdir(parents=True, exist_ok=True)
        _write_json(out, {
            "video_id": video_id, "enabled": False, "passages": [], "rejected": [], "llm_calls": 0, "images_sent": 0,
        })
        return out

    audio = _read_json(video_dir / "audio.json")
    scenes = _read_json(video_dir / "scenes.json")
    transcript = _read_json(video_dir / "transcript.json")
    meta = _read_json(video_dir / "meta.json")
    if scenes.get("peak_windows") is not True:
        raise ActionError(NO_PEAK_WINDOWS_MESSAGE)
    duration = float(meta.get("duration") or 0)
    if duration <= 0:
        raise ActionError(f"{video_dir / 'meta.json'} : duration absente ou nulle")

    kept, rejected = _detect(duration, audio, scenes, transcript, settings)
    kept = _attach_frames(kept, rejected, scenes.get("frames") or [], duration, settings)
    kept.sort(key=lambda p: p["start"])

    frames_to_describe: dict[str, dict[str, Any]] = {}
    for passage in kept:
        for f in passage["_picked"]:
            frames_to_describe.setdefault(f["path"], f)
            if not (video_dir / f["path"]).is_file():
                raise ActionError(f"image cle introuvable : {video_dir / f['path']}")
    ordered = sorted(frames_to_describe.values(), key=lambda f: float(f["timecode"]))

    if not kept:
        log.info("%s : aucun passage d'action (score >= %s)", video_id, settings["min_score"])
    descriptions, llm_calls = _describe(ordered, video_dir, settings, config) if ordered else ({}, 0)

    passages = []
    for n, passage in enumerate(kept):
        picked = passage.pop("_picked")
        passage.pop("_rank")
        missing = not picked
        if missing:
            log.warning("%s : passage %.2f-%.2f s sans image dans scenes.json (frames_missing)",
                        video_id, passage["start"], passage["end"])
        passages.append({
            "id": f"a{n}",
            "start": passage["start"], "end": passage["end"], "duration": passage["duration"],
            "score": passage["score"], "signals": passage["signals"],
            "frames": [descriptions[f["path"]] for f in picked],
            "frames_missing": missing,
        })

    video_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out, {
        "video_id": video_id,
        "enabled": True,
        "settings": {key: settings[key] for key in CONFIG_DEFAULTS},
        "passages": passages,
        "rejected": rejected,
        "llm_calls": llm_calls,
        "images_sent": len(ordered),
    })
    (video_dir / "action_partial.json").unlink(missing_ok=True)
    return out
