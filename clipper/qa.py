"""Etape qa : controle qualite de chaque clip rendu (SPEC-6127), avis de
clipper.llm (usage ``qa``) complete de verifications mecaniques locales.

Entrees : output/<video_id>/<clip_id>.mp4 et output/<video_id>/<clip_id>.json
tels qu'ecrits par l'etape render (lus sur disque, jamais en important
render - ADR-b16b).

Pour chaque clip :
- images fixes extraites du .mp4 par OpenCV (debut, milieu, fin, et une juste
  apres chaque changement de plan, detecte par ecart d'histogramme HSV entre
  images successives), ecrites en JPEG sous workspace/<video_id>/qa/<clip_id>/ ;
- l'IA recoit ces images (jamais la video ni l'audio - ADR-b1c1), le texte
  d'accroche (ou, en letterbox, le titre d'ecran ``screen_title``, affiche en
  permanence) et la transcription du clip (champ ``transcript`` du JSON), et
  liste les defauts parmi : clip incomprehensible sans ce qui le precede
  dans la video (incomprehensible, seul defaut bloquant), visage coupe
  (face_cut), sous-titre sur un visage (subtitle_on_face), debut en milieu de
  phrase (starts_mid_sentence), accroche faible (weak_hook) ; ces quatre-la
  ne sont que des avertissements. En letterbox (``layout`` = "letterbox" dans
  le JSON), face_cut et subtitle_on_face ne sont plus demandes : le zoom fixe
  rogne volontairement les bords et les sous-titres sont hors de l'image ;
  partie 2+ d'une serie (``part`` >= 2 dans le JSON), starts_mid_sentence
  n'est plus demande : la reprise d'environ 3 s de la partie precedente est
  voulue (SPEC-0eec regle 3) ;
- verifications locales par ffprobe/ffmpeg : duree reelle vs ``duration`` du
  JSON, resolution attendue (1080x1920), silence initial superieur au seuil
  (1 s par defaut ; pas de piste audio = silence), ecran noir (black_screen)
  detecte par ffmpeg blackdetect sur le mp4 rendu (en letterbox, mesure
  seulement sur ``video_rect`` : l'encadre blanc du titre d'ecran empecherait
  sinon toute detection) : signale si une plage noire dure au moins
  ``black_min_seconds`` (1 s par defaut), bloquant seulement a partir de
  ``black_block_seconds`` (3 s par defaut) ; seuils de pixel
  ``black_pixel_threshold`` et de part d'image noire ``black_picture_ratio``
  aussi reglables). Un clip letterbox sans ``video_rect`` est une erreur
  explicite (render l'ecrit toujours en letterbox).
- clip stream (``layout`` = "stream", SPEC-3a88 : facecam agrandie en haut,
  jeu en bas) : ``camera_rect`` et ``video_rect`` obligatoires, dans
  l'image attendue et sans se recouvrir, sinon erreur explicite ; ecran noir
  mesure sur ``camera_rect`` (aucun texte ne la recouvre) ; prompt avec le
  titre d'ecran permanent ; face_cut et subtitle_on_face restent demandes
  (le visage est a l'image).
- clip stream_split (``layout`` = "stream_split", SPEC-76dc : webcam en haut,
  jeu en bas, badge de chaine optionnel entre les deux) : ``webcam_rect`` et
  ``video_rect`` obligatoires, valides comme pour stream, sinon erreur
  explicite (jamais l'image entiere) ; ecran noir mesure sur chacun des deux
  panneaux ; prompt avec une section ``## Format`` et, seulement si
  ``[render] title_enabled``, le titre d'ecran (jamais un texte d'accroche
  des 2 premieres secondes, qui n'est pas dessine) ; face_cut et
  subtitle_on_face restent demandes, comme en stream (la webcam montre le
  visage, les sous-titres peuvent le recouvrir selon leur position).

Sortie : le JSON du clip est mis a jour en place :

    "qa": {"status": "passed" | "rejected",
           "issues": [{"type", "detail", "source": "llm" | "local",
                       "severity": "blocking" | "warning"}]},
    "ready": true | false

Un clip rejete casse toute sa serie (les parties se suivent) : ``rejected``
seulement s'il y a au moins un defaut ``blocking`` (clip incomprehensible, ou
techniquement casse : duree, resolution, silence initial, ecran noir long) ;
les ``warning`` restent dans ``issues`` et le clip reste pret. Apres le
controle de toutes les parties d'une serie (clip_id ``<moment>-p<part>``,
SPEC-6127), chaque autre partie d'une serie dont une partie est rejetee
recoit un avertissement ``series_part_rejected`` qui la nomme. ``ready`` n'est
vrai que pour un clip ``passed`` (voir ``is_ready``, a utiliser par tout
consommateur). Reponse invalide ou Claude indisponible : l'erreur remonte et
le JSON n'est pas touche, aucun verdict de secours (ADR-ad2e). Un clip deja
controle (passed/rejected) n'est pas recontrole, sauf ``force``.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from clipper import llm

log = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    "expected_width": 1080,
    "expected_height": 1920,
    # Ecart tolere (s) entre la duree reelle du .mp4 et ``duration`` du JSON.
    "duration_tolerance": 0.5,
    # Silence initial (s) au-dela duquel le clip est rejete.
    "max_leading_silence": 1.0,
    # Niveau (dBFS, RMS par fenetre) sous lequel l'audio compte comme silence.
    "silence_threshold_db": -45.0,
    "silence_window": 0.02,
    # Correlation d'histogramme HSV sous laquelle deux images successives
    # appartiennent a deux plans differents.
    "shot_change_threshold": 0.5,
    # Decalage (s) des images prises apres le debut du clip et apres une coupe.
    "frame_offset": 0.1,
    # Deux images plus proches que cet ecart (s) sont fusionnees.
    "frame_min_gap": 0.3,
    # Au-dela, les changements de plan retenus sont repartis uniformement.
    "max_shot_frames": 10,
    "frame_width": 540,
    "jpeg_quality": 85,
    # Duree minimale (s) d'une plage noire (ffmpeg blackdetect) pour la
    # signaler (avertissement) ; un fondu court de la source ne doit pas suffire.
    "black_min_seconds": 1.0,
    # Duree (s) a partir de laquelle une plage noire rejette le clip ; entre
    # black_min_seconds et ce seuil, ce n'est qu'un avertissement (une
    # transition de documentaire ne doit pas casser une serie).
    "black_block_seconds": 3.0,
    # Luminance (0-1) sous laquelle un pixel compte comme noir (blackdetect pix_th).
    "black_pixel_threshold": 0.10,
    # Part de pixels noirs (0-1) au-dela de laquelle une image compte comme
    # noire (blackdetect pic_th).
    "black_picture_ratio": 0.98,
    # Nombre de clips controles en meme temps au plus (1 = sequentiel comme
    # avant TASK-1366). Une valeur < 1 est une erreur explicite (ADR-ad2e).
    "parallel": 4,
}

DEFECTS: dict[str, str] = {
    "incomprehensible": (
        "sans ce qui precede dans la video, le spectateur ne comprend pas de qui ou de quoi "
        "il s'agit, ou l'histoire du clip ne se suit pas"
    ),
    "face_cut": "un visage est coupe par le bord du cadre",
    "subtitle_on_face": "un sous-titre ou un texte incruste recouvre un visage",
    "starts_mid_sentence": "le clip commence au milieu d'une phrase",
    "weak_hook": "l'accroche (texte affiche et premiers mots) ne donne pas envie de rester",
    "empty_webcam": (
        "clip stream_split seulement : le panneau webcam du haut ne montre ni visage ni webcam sur "
        "la majorite des images (interface du jeu, bras de micro, decor, ecran de pause)"
    ),
}

# Seul un clip incomprehensible est rejete par l'IA : les autres defauts
# signales sont des avertissements (un clip rejete casse toute sa serie).
_BLOCKING_DEFECTS = {"incomprehensible", "empty_webcam"}

BLOCKING, WARNING = "blocking", "warning"

SERIES_PART_REJECTED = "series_part_rejected"

# En letterbox, le zoom fixe rogne volontairement les bords et les
# sous-titres sont hors de l'image (bande floue du bas) : ces deux defauts
# ne sont plus demandes a l'IA (SPEC-6127).
_LETTERBOX_EXCLUDED_DEFECTS = {"face_cut", "subtitle_on_face"}

# Partie 2+ d'une serie : la reprise d'environ 3 s de la fin de la partie
# precedente est voulue, ce n'est pas un debut en milieu de phrase
# (SPEC-0eec regle 3). Seule la partie 1 (ou un clip unique) commence
# vraiment sur l'accroche.
_SERIES_EXCLUDED_DEFECTS = {"starts_mid_sentence"}

# Un panneau webcam vide n'existe qu'en stream_split : ailleurs, ce defaut
# n'est pas demande (TASK-9957).
_NON_SPLIT_EXCLUDED_DEFECTS = {"empty_webcam"}


def _excluded_defects(letterbox: bool, part: int, split: bool = False) -> set[str]:
    excluded = set()
    if not split:
        excluded |= _NON_SPLIT_EXCLUDED_DEFECTS
    if letterbox:
        excluded |= _LETTERBOX_EXCLUDED_DEFECTS
    if part >= 2:
        excluded |= _SERIES_EXCLUDED_DEFECTS
    return excluded


_CHECKED = ("passed", "rejected")


class QAError(Exception):
    """Clip rendu absent ou illisible, ou echec de ffmpeg/ffprobe/OpenCV."""


# --------------------------------------------------------------------------
# Lecture de l'etat
# --------------------------------------------------------------------------


def is_ready(clip: dict[str, Any]) -> bool:
    """Un clip n'est pret a publier que s'il a passe le controle qualite
    (SPEC-6127 : un clip rejete ne l'est jamais, quel que soit ``ready``)."""
    return clip.get("qa", {}).get("status") == "passed" and clip.get("ready") is True


# --------------------------------------------------------------------------
# ffprobe / ffmpeg
# --------------------------------------------------------------------------


def _probe(mp4: Path, ffprobe_bin: str) -> dict[str, Any]:
    cmd = [
        ffprobe_bin, "-v", "error", "-show_entries", "stream=codec_type,width,height",
        "-show_entries", "format=duration", "-of", "json", str(mp4),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise QAError(f"ffprobe introuvable ({ffprobe_bin})") from exc
    if proc.returncode != 0:
        raise QAError(f"ffprobe a echoue sur {mp4} : {proc.stderr.decode(errors='replace').strip()}")
    return json.loads(proc.stdout)


def _leading_silence(mp4: Path, settings: dict[str, Any], ffmpeg_bin: str) -> float:
    """Secondes de silence au debut de la piste audio (decodee en mono
    16 kHz sur les premieres secondes seulement)."""
    limit = float(settings["max_leading_silence"]) + 1.0
    rate = 16000
    cmd = [
        ffmpeg_bin, "-v", "error", "-t", f"{limit:.3f}", "-i", str(mp4),
        "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise QAError(f"ffmpeg introuvable ({ffmpeg_bin})") from exc
    if proc.returncode != 0:
        raise QAError(f"ffmpeg a echoue sur {mp4} : {proc.stderr.decode(errors='replace').strip()}")
    samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float64) / 32768.0
    win = max(1, int(rate * float(settings["silence_window"])))
    threshold = 10 ** (float(settings["silence_threshold_db"]) / 20)
    for i in range(0, len(samples) - win + 1, win):
        rms = math.sqrt(float(np.mean(samples[i:i + win] ** 2)))
        if rms > threshold:
            return i / rate
    return len(samples) / rate


_BLACKDETECT_RE = re.compile(
    r"black_start:(?P<start>[\d.]+) black_end:(?P<end>[\d.]+) black_duration:(?P<duration>[\d.]+)"
)


def _black_segments(
    mp4: Path, settings: dict[str, Any], ffmpeg_bin: str,
    crop: tuple[int, int, int, int] | list[tuple[int, int, int, int]] | None = None,
) -> list[dict[str, Any]]:
    """Plages noires du mp4 rendu (ffmpeg blackdetect), au moins
    ``black_min_seconds`` chacune ; une transition de montage courte dans la
    source ne doit pas en produire. Bloquante a partir de
    ``black_block_seconds``, avertissement en dessous. En letterbox, ``crop`` restreint la
    mesure a ``video_rect`` (x, y, w, h) : sinon l'encadre blanc du titre
    d'ecran empeche toute detection (SPEC-6127). Une liste de rectangles (stream_split,
    SPEC-76dc) mesure chaque panneau separement : un panneau noir seul, noye dans
    l'autre, serait sinon invisible."""
    if isinstance(crop, list):
        found: list[dict[str, Any]] = []
        for rect in crop:
            found += _black_segments(mp4, settings, ffmpeg_bin, crop=rect)
        return found
    vf = (
        f"blackdetect=d={float(settings['black_min_seconds'])}"
        f":pic_th={float(settings['black_picture_ratio'])}"
        f":pix_th={float(settings['black_pixel_threshold'])}"
    )
    if crop is not None:
        x, y, w, h = crop
        vf = f"crop={w}:{h}:{x}:{y}," + vf
    cmd = [
        ffmpeg_bin, "-v", "info", "-i", str(mp4),
        "-vf", vf,
        "-an", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise QAError(f"ffmpeg introuvable ({ffmpeg_bin})") from exc
    if proc.returncode != 0:
        raise QAError(f"ffmpeg a echoue sur {mp4} : {proc.stderr.decode(errors='replace').strip()}")
    stderr = proc.stderr.decode(errors="replace")
    block = float(settings["black_block_seconds"])
    return [
        {
            "type": "black_screen",
            "detail": f"ecran noir de {float(m['duration']):.2f} s a partir de {float(m['start']):.2f} s",
            "source": "local",
            "severity": BLOCKING if float(m["duration"]) >= block else WARNING,
        }
        for m in _BLACKDETECT_RE.finditer(stderr)
    ]


def _rect(clip: dict[str, Any], key: str) -> tuple[int, int, int, int]:
    rect = clip.get(key)
    if not isinstance(rect, dict) or not {"x", "y", "w", "h"} <= set(rect):
        raise QAError(
            f"clip {clip.get('clip_id')} en layout {clip.get('layout')} sans {key} valide : relancer render --force"
        )
    try:
        return tuple(int(rect[k]) for k in ("x", "y", "w", "h"))  # type: ignore[return-value]
    except (TypeError, ValueError) as exc:
        raise QAError(f"clip {clip.get('clip_id')} : {key} invalide {rect!r}") from exc


def _panel_rects(
    clip: dict[str, Any], settings: dict[str, Any], camera_key: str,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Les deux panneaux d'un clip stream (``camera_rect``) ou stream_split
    (``webcam_rect``) -- webcam et ``video_rect`` (jeu) -- valides, dans
    l'image attendue, sans se recouvrir ; sinon erreur explicite."""
    layout = clip.get("layout")
    want_w, want_h = int(settings["expected_width"]), int(settings["expected_height"])
    camera, game = _rect(clip, camera_key), _rect(clip, "video_rect")
    for key, (x, y, w, h) in ((camera_key, camera), ("video_rect", game)):
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > want_w or y + h > want_h:
            raise QAError(f"clip {layout} {clip.get('clip_id')} : {key} {(x, y, w, h)} hors de {want_w}x{want_h}")
    (cx, cy, cw, ch), (gx, gy, gw, gh) = camera, game
    if cx < gx + gw and gx < cx + cw and cy < gy + gh and gy < cy + ch:
        raise QAError(f"clip {layout} {clip.get('clip_id')} : {camera_key} {camera} recouvre video_rect {game}")
    return camera, game


def _stream_rect(clip: dict[str, Any], settings: dict[str, Any]) -> tuple[int, int, int, int]:
    """Clip stream (SPEC-3a88) : ``camera_rect`` (facecam) et ``video_rect``
    (jeu) valides ; renvoie ``camera_rect``, ou l'ecran noir se mesure
    (aucun texte ne la recouvre)."""
    return _panel_rects(clip, settings, "camera_rect")[0]


def _split_rects(clip: dict[str, Any], settings: dict[str, Any]) -> list[tuple[int, int, int, int]]:
    """Clip stream_split (SPEC-76dc) : ``webcam_rect`` et ``video_rect``
    valides ; l'ecran noir se mesure sur chacun des deux panneaux (le badge
    et le titre n'y sont pas)."""
    return list(_panel_rects(clip, settings, "webcam_rect"))


def _video_rect(clip: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """``video_rect`` (panneau main, pixels de sortie) pour un clip letterbox,
    ou ``None`` hors letterbox. Un clip letterbox sans ``video_rect`` valide
    est une erreur explicite (render l'ecrit toujours en letterbox -
    SPEC-6127) : jamais mesurer l'ecran noir sur l'image entiere, l'encadre
    blanc du titre d'ecran empecherait toute detection."""
    if clip.get("layout") != "letterbox":
        return None
    rect = clip.get("video_rect")
    if not isinstance(rect, dict) or not {"x", "y", "w", "h"} <= set(rect):
        raise QAError(
            f"clip {clip.get('clip_id')} en layout letterbox sans video_rect valide : relancer render --force"
        )
    try:
        return tuple(int(rect[k]) for k in ("x", "y", "w", "h"))
    except (TypeError, ValueError) as exc:
        raise QAError(f"clip {clip.get('clip_id')} : video_rect invalide {rect!r}") from exc


def _local_issues(
    mp4: Path, clip: dict[str, Any], settings: dict[str, Any], ffmpeg_bin: str, ffprobe_bin: str,
    crop: tuple[int, int, int, int] | list[tuple[int, int, int, int]] | None = None,
) -> list[dict[str, Any]]:
    info = _probe(mp4, ffprobe_bin)
    streams = info.get("streams", [])
    issues: list[dict[str, Any]] = []

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    want_w, want_h = int(settings["expected_width"]), int(settings["expected_height"])
    if video is None:
        issues.append({"type": "resolution", "detail": "aucune piste video", "source": "local",
                       "severity": BLOCKING})
    elif (video.get("width"), video.get("height")) != (want_w, want_h):
        issues.append({
            "type": "resolution",
            "detail": f"{video.get('width')}x{video.get('height')} au lieu de {want_w}x{want_h}",
            "source": "local",
            "severity": BLOCKING,
        })

    actual = float(info.get("format", {}).get("duration", 0.0))
    expected = float(clip["duration"])
    if abs(actual - expected) > float(settings["duration_tolerance"]):
        issues.append({
            "type": "duration",
            "detail": f"duree reelle {actual:.2f} s, {expected:.2f} s annoncee dans le JSON",
            "source": "local",
            "severity": BLOCKING,
        })

    issues += _black_segments(mp4, settings, ffmpeg_bin, crop=crop)

    max_silence = float(settings["max_leading_silence"])
    if not any(s.get("codec_type") == "audio" for s in streams):
        issues.append({"type": "leading_silence", "detail": "aucune piste audio", "source": "local",
                       "severity": BLOCKING})
    else:
        silence = _leading_silence(mp4, settings, ffmpeg_bin)
        if silence > max_silence:
            issues.append({
                "type": "leading_silence",
                "detail": f"{silence:.2f} s de silence au debut (max {max_silence:.2f} s)",
                "source": "local",
                "severity": BLOCKING,
            })
    return issues


# --------------------------------------------------------------------------
# Images fixes : debut, milieu, fin, une par changement de plan
# --------------------------------------------------------------------------


def _hist(frame: np.ndarray) -> np.ndarray:
    small = cv2.resize(frame, (64, 64))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
    return cv2.normalize(hist, hist)


def _open(mp4: Path) -> cv2.VideoCapture:
    capture = cv2.VideoCapture(str(mp4))
    if not capture.isOpened():
        raise QAError(f"OpenCV ne peut pas lire {mp4}")
    return capture


def _scan(mp4: Path, threshold: float) -> tuple[int, float, list[int]]:
    """(nombre d'images, fps, indices des premieres images de chaque nouveau plan)."""
    capture = _open(mp4)
    try:
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        cuts: list[int] = []
        prev = None
        n = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            hist = _hist(frame)
            if prev is not None and cv2.compareHist(prev, hist, cv2.HISTCMP_CORREL) < threshold:
                cuts.append(n)
            prev = hist
            n += 1
    finally:
        capture.release()
    if n == 0:
        raise QAError(f"aucune image lisible dans {mp4}")
    return n, fps, cuts


def _spread(items: list[int], k: int) -> list[int]:
    """Au plus ``k`` elements de ``items``, repartis uniformement."""
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def _targets(n: int, fps: float, cuts: list[int], settings: dict[str, Any]) -> list[tuple[int, list[str]]]:
    """Indices d'images a extraire, tries, avec leurs etiquettes ; deux
    cibles trop proches sont fusionnees."""
    offset = int(round(float(settings["frame_offset"]) * fps))
    raw: list[tuple[int, str]] = [(min(offset, n - 1), "debut"), (n // 2, "milieu"), (n - 1, "fin")]
    for cut in _spread(cuts, int(settings["max_shot_frames"])):
        raw.append((min(cut + offset, n - 1), "changement de plan"))
    raw.sort()
    gap = float(settings["frame_min_gap"]) * fps
    merged: list[tuple[int, list[str]]] = []
    for index, label in raw:
        if merged and index - merged[-1][0] < gap:
            if label not in merged[-1][1]:
                merged[-1][1].append(label)
            continue
        merged.append((index, [label]))
    return merged


def _extract_frames(mp4: Path, dest: Path, settings: dict[str, Any]) -> list[tuple[Path, float, list[str]]]:
    n, fps, cuts = _scan(mp4, float(settings["shot_change_threshold"]))
    targets = dict(_targets(n, fps, cuts, settings))
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.jpg"):
        old.unlink()
    width = int(settings["frame_width"])
    frames: list[tuple[Path, float, list[str]]] = []
    capture = _open(mp4)
    try:
        index = 0
        while len(frames) < len(targets):
            ok, frame = capture.read()
            if not ok:
                break
            if index in targets:
                h, w = frame.shape[:2]
                if w > width:
                    frame = cv2.resize(frame, (width, round(h * width / w)))
                path = dest / f"{len(frames) + 1:02d}.jpg"
                if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, int(settings["jpeg_quality"])]):
                    raise QAError(f"ecriture de {path} impossible")
                frames.append((path, index / fps, targets[index]))
            index += 1
    finally:
        capture.release()
    if len(frames) < len(targets):
        raise QAError(f"{mp4} : {len(frames)} images lues sur {len(targets)} attendues")
    return frames


# --------------------------------------------------------------------------
# Schema et prompt
# --------------------------------------------------------------------------


def response_schema(letterbox: bool = False, part: int = 1, split: bool = False) -> dict[str, Any]:
    """Ce que le LLM renvoie pour un clip : la liste de ses defauts (vide si
    le clip est bon). En letterbox, face_cut et subtitle_on_face sont hors
    enum : le zoom fixe rogne volontairement les bords et les sous-titres
    sont hors de l'image (SPEC-6127). Partie 2+ d'une serie (``part``) :
    starts_mid_sentence est hors enum, la reprise de la partie precedente
    est voulue (SPEC-0eec)."""
    excluded = _excluded_defects(letterbox, part, split)
    defect_types = [d for d in DEFECTS if d not in excluded]
    return {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": defect_types},
                        "detail": {
                            "type": "string", "minLength": 1,
                            "description": "Ce qui est vu, et sur quelle image.",
                        },
                    },
                    "required": ["type", "detail"],
                    "additionalProperties": False,
                },
                "description": "Defauts constates ; liste vide si le clip est publiable.",
            },
        },
        "required": ["issues"],
        "additionalProperties": False,
    }


def _prompt(
    clip: dict[str, Any], frames: list[tuple[Path, float, list[str]]], letterbox: bool = False,
    title_shown: bool = True,
) -> str:
    part = int(clip.get("part", 1))
    excluded = _excluded_defects(letterbox, part, clip.get("layout") == "stream_split")
    defect_keys = [d for d in DEFECTS if d not in excluded]
    defects = "\n".join(f"- {key} : {DEFECTS[key]}" for key in defect_keys)
    images = "\n".join(
        f"Image {k} : t={t:.2f} s ({', '.join(labels)})" for k, (_, t, labels) in enumerate(frames, 1)
    )
    if clip.get("layout") == "stream_split":
        # SPEC-76dc : webcam en haut, jeu en bas, badge de chaine optionnel a leur jonction ;
        # le titre d'ecran n'est cite que s'il a ete dessine (sidecar title_shown).
        format_line = (
            "## Format\n"
            "Clip stream_split : webcam en haut (celle du createur), jeu en bas, un badge de chaine "
            "(logo et nom sur fond noir) eventuel entre les deux, sous-titres selon le reglage. "
            "Aucun texte d'accroche n'est affiche au debut du clip.\n\n"
        )
        hook_line = (
            f"Titre d'ecran affiche en permanence (accroche) : {clip.get('screen_title', '')}\n"
            if title_shown else ""
        )
    elif clip.get("layout") == "stream":
        format_line = (
            "## Format\n"
            "Clip stream : titre d'ecran sur encadre blanc en haut, la facecam (webcam du "
            "createur) agrandie dessous, le jeu ou l'ecran en bas, sous-titres sur le jeu.\n\n"
        )
        hook_line = (
            f"Titre d'ecran affiche en permanence (accroche) : {clip.get('screen_title', '')}\n"
            if title_shown else ""
        )
    elif letterbox:
        format_line = (
            "## Format\n"
            "Clip letterbox : titre d'ecran sur encadre blanc en haut, video zoomee au centre "
            "(fond flou de la video autour), sous-titres dans la bande du bas. Le zoom rogne "
            "volontairement les bords et les sous-titres sont hors de l'image : normal, ne "
            "pas le signaler.\n\n"
        )
        hook_line = (
            f"Titre d'ecran affiche en permanence (accroche) : {clip.get('screen_title', '')}\n"
            if title_shown else ""
        )
    else:
        format_line = ""
        hook_line = f"Texte d'accroche affiche les 2 premieres secondes : {clip.get('hook_text', '')}\n"
    if part >= 2:
        series_line = (
            f"## Serie\n"
            f"Ce clip est la partie {part} d'une serie de {int(clip.get('parts_total', part))} parties qui "
            "se suivent. Il reprend volontairement les quelques dernieres secondes de la partie precedente "
            "(recouvrement voulu, SPEC-0eec) : ne pas le signaler comme un debut en milieu de phrase. "
            "Le spectateur a vu les parties precedentes : ce qui y a ete presente (personnes, contexte) "
            "peut etre suppose connu, le clip n'est pas incomprehensible pour ca.\n\n"
        )
    else:
        series_line = ""
    if clip.get("layout") == "stream_split":
        blocking_line = (
            "Seuls incomprehensible et empty_webcam sont bloquants : le clip est alors rejete, et avec lui "
            "toute sa serie. Ne les signale que si c'est net. "
        )
    else:
        blocking_line = (
            "Seul incomprehensible est bloquant : le clip est alors rejete, et avec lui toute sa serie. "
            "Ne le signale que si c'est net. "
        )
    return (
        "Tu fais le controle qualite d'un clip vertical TikTok deja rendu, avant publication.\n"
        "Tu vois des images fixes extraites du clip et sa transcription ; signale uniquement "
        "les defauts reellement visibles ou lisibles, parmi :\n"
        f"{defects}\n\n"
        "Un clip sans defaut rend une liste vide.\n"
        f"{blocking_line}"
        "Les autres defauts ne sont que des avertissements "
        "(le clip reste publiable) : en cas de doute franc, signale-les.\n\n"
        f"{format_line}"
        f"{series_line}"
        "## Clip\n"
        f"Duree : {float(clip['duration']):.1f} s ; langue : {clip.get('language') or '?'}\n"
        f"{hook_line}"
        f"Titre : {clip.get('title', '')}\n\n"
        "## Images jointes, dans cet ordre\n"
        f"{images}\n\n"
        "## Transcription du clip\n"
        f"{clip.get('transcript', '')}\n"
    )


# --------------------------------------------------------------------------
# Etape
# --------------------------------------------------------------------------


def _settings(config: Any) -> dict[str, Any]:
    if config is None:
        from clipper.config import load_config

        config = load_config()
    return {**CONFIG_DEFAULTS, **config.section("qa")}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _progress_gate(total: int) -> Any:
    """Ferme une fonction ``gate(i)`` (i de 1 a ``total``) qui dit si le clip
    ``i`` doit etre annonce a INFO : au plus toutes les 30 s ou tous les 10 %
    (toujours le dernier). Done_criteria de TASK-8abc."""
    last: dict[str, float] = {"i": 0, "t": time.monotonic()}
    step = max(1, math.ceil(total * 0.1)) if total else 1

    def gate(i: int) -> bool:
        now = time.monotonic()
        if i >= total or i - last["i"] >= step or now - last["t"] >= 30.0:
            last["i"], last["t"] = i, now
            return True
        return False

    return gate


def check_clip(
    json_path: Path,
    frames_dir: Path,
    settings: dict[str, Any],
    *,
    config: Any = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
) -> dict[str, Any]:
    """Controle un clip rendu et reecrit son JSON avec ``qa`` et ``ready`` ;
    renvoie le champ ``qa``."""
    clip = json.loads(json_path.read_text(encoding="utf-8"))
    letterbox = clip.get("layout") == "letterbox"
    layout = clip.get("layout")
    if layout == "stream":
        crop = _stream_rect(clip, settings)
    elif layout == "stream_split":
        crop = _split_rects(clip, settings)
    else:
        crop = _video_rect(clip)
    mp4 = json_path.with_suffix(".mp4")
    if not mp4.exists():
        raise QAError(f"video du clip absente : {mp4}")
    # Ce que le rendu a dessine (sidecar title_shown). Un sidecar anterieur a ce
    # champ n'a que la config : relue seulement pour le split, comme avant.
    if "title_shown" in clip:
        title_shown = bool(clip["title_shown"])
    elif layout == "stream_split":
        if config is None:
            from clipper.config import load_config

            config = load_config()
        title_shown = bool(config.section("render")["title_enabled"])
    else:
        title_shown = True

    issues = _local_issues(mp4, clip, settings, ffmpeg_bin, ffprobe_bin, crop=crop)
    frames = _extract_frames(mp4, frames_dir, settings)
    answer = llm.ask(
        "qa", _prompt(clip, frames, letterbox=letterbox, title_shown=title_shown), [p for p, _, _ in frames],
        response_schema(letterbox=letterbox, part=int(clip.get("part", 1)), split=layout == "stream_split"),
        config=config,
    )
    issues = [
        {**issue, "source": "llm", "severity": BLOCKING if issue["type"] in _BLOCKING_DEFECTS else WARNING}
        for issue in answer["issues"]
    ] + issues

    status = "rejected" if any(i["severity"] == BLOCKING for i in issues) else "passed"
    clip["qa"] = {"status": status, "issues": issues}
    clip["ready"] = status == "passed"
    _write_json(json_path, clip)
    return clip["qa"]


_PART_ID_RE = re.compile(r"^(?P<moment>.+)-p(?P<part>\d+)$")


def _warn_series(clips: list[Path]) -> None:
    """Une partie rejetee casse sa serie : chaque autre partie controlee du
    meme moment (clip_id ``<moment>-p<part>``, SPEC-6127) recoit un
    avertissement ``series_part_rejected`` par partie rejetee, qui la nomme.
    Recalcule a chaque passage (les anciens avertissements sont retires),
    donc jamais duplique ; le statut des autres parties ne change pas."""
    series: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for json_path in clips:
        match = _PART_ID_RE.match(json_path.stem)
        if match is None:
            continue
        clip = json.loads(json_path.read_text(encoding="utf-8"))
        if clip.get("qa", {}).get("status") in _CHECKED:
            series.setdefault(match["moment"], []).append((json_path, clip))
    for parts in series.values():
        rejected = [path.stem for path, clip in parts if clip["qa"]["status"] == "rejected"]
        for json_path, clip in parts:
            kept = [i for i in clip["qa"]["issues"] if i["type"] != SERIES_PART_REJECTED]
            warnings = [
                {
                    "type": SERIES_PART_REJECTED,
                    "detail": f"la partie {other} de la serie est rejetee",
                    "source": "local",
                    "severity": WARNING,
                }
                for other in rejected if other != json_path.stem
            ]
            if kept + warnings != clip["qa"]["issues"]:
                clip["qa"]["issues"] = kept + warnings
                _write_json(json_path, clip)


def run(
    video_id: str,
    workspace_dir: str | Path = "workspace",
    output_dir: str | Path = "output",
    *,
    config: Any = None,
    force: bool = False,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
) -> Path:
    """Controle chaque clip rendu de output/<video_id>/ et met a jour son
    JSON ; renvoie ce dossier. Un clip deja controle n'est pas refait, sauf
    ``force``. Au plus ``parallel`` clips sont controles en meme temps
    (reglage ``[qa] parallel``, defaut 4 ; valeur < 1 refusee). Si un clip
    echoue, les autres clips en cours de controle vont au bout et gardent
    leur JSON ecrit, puis l'erreur d'origine remonte (ADR-ad2e) ; dans ce
    cas, ``_warn_series`` n'est pas applique. Sinon, une fois toutes les
    parties controlees, une partie rejetee est signalee aux autres parties
    de sa serie (avertissement)."""
    out_dir = Path(output_dir) / video_id
    clips = sorted(out_dir.glob("*.json")) if out_dir.is_dir() else []
    if not clips:
        raise QAError(f"aucun clip rendu dans {out_dir}")
    settings = _settings(config)
    parallel = int(settings["parallel"])
    if parallel < 1:
        raise QAError(f"[qa] parallel doit etre >= 1, recu {parallel}")
    frames_root = Path(workspace_dir) / video_id / "qa"

    pending = []
    for json_path in clips:
        clip = json.loads(json_path.read_text(encoding="utf-8"))
        if not force and clip.get("qa", {}).get("status") in _CHECKED:
            continue
        pending.append(json_path)

    errors: dict[Path, Exception] = {}
    total = len(pending)
    gate = _progress_gate(total)
    done_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {
            pool.submit(
                check_clip, json_path, frames_root / json_path.stem, settings,
                config=config, ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin,
            ): json_path
            for json_path in pending
        }
        starts = {future: time.monotonic() for future in futures}
        for future in concurrent.futures.as_completed(futures):
            json_path = futures[future]
            try:
                future.result()
            except Exception as exc:  # aucune perte silencieuse (ADR-ad2e)
                errors[json_path] = exc
                continue
            elapsed = time.monotonic() - starts[future]
            done_count += 1
            log.debug("qa clip %d/%d (%s) en %.1fs", done_count, total, json_path.stem, elapsed)
            if gate(done_count):
                log.info("qa clip %d/%d (%s) en %.1fs", done_count, total, json_path.stem, elapsed)

    if errors:
        first = next(json_path for json_path in pending if json_path in errors)
        raise errors[first]

    _warn_series(clips)
    return out_dir
