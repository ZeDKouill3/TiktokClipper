"""Etape render : assemble le clip final par ffmpeg (SPEC-6127).

Entrees (workspace/<video_id>/, lues en JSON/.ass, jamais en important les
autres etapes - ADR-b16b) :
- <video_id>.mp4 : la video source ;
- captions.json (captions) : titre, legende, hashtags, accroche, start/end/
  duration/part/parts_total/language/moment_id de chaque clip ;
- moments.json (moments) : score, notes par critere et justification, par
  moment_id ;
- reframe/<clip_id>.json (reframe) : le plan de recadrage (plans, panneaux,
  rectangles source par intervalle de temps, layout) ;
- subtitles/<clip_id>.ass (subtitles) : les sous-titres deja positionnes
  (SPEC-6127 : jamais sur un visage, decide par l'etape subtitles) ;
- transcript.json (transcribe) : le texte prononce dans le clip ;
- meta.json (download) : titre de la video source (facultatif) et
  webpage_url, l'URL reelle de la source (obligatoire : RenderError sinon,
  jamais reconstruite en supposant YouTube - ADR-ad2e).

Sortie : output/<video_id>/<clip_id>.mp4 et output/<video_id>/<clip_id>.json
conformes a SPEC-6127. Le champ ``qa`` part a ``{"status": "skipped",
"issues": []}`` : l'etape qa (SPEC-6127) le remplace apres coup.

ffmpeg construit chaque clip plan par plan : l'entree source est
positionnee sur le debut du clip (-ss/-t avant -i, jamais decodee depuis 0),
trim du plan relatif a ce point,
canevas noir 1080x1920 (ou la taille de sortie de reframe), un panneau par
``crop`` (positions figees par intervalle, une expression ``if(lt(t,...))``
quand elles varient) eventuellement flou (fond, ``fallback_blur``) puis
``scale``, empile par ``overlay`` ; les plans sont mis bout a bout par
``concat``. Les sous-titres sont incrustes via le filtre ``ass`` (police
Poppins ExtraBold chargee depuis clipper/assets/fonts via ``fontsdir``,
chemin relatif au paquet - jamais code en dur pour une machine). L'accroche
et « Part N/M » sont incrustes par ``drawtext`` (texte lu depuis un fichier
temporaire, pour eviter tout souci d'echappement UTF-8). L'audio est
recadre puis normalise a -14 LUFS integres (``loudnorm``). L'encodeur video
est resolu via clipper.gpu (ADR-fb9b) : h264_nvenc si un GPU CUDA est
detecte, sinon libx264.

Format letterbox (SPEC-6127, ``layout = "letterbox"`` a la racine de
reframe/<clip_id>.json) : render lit ``text_zones`` (zones title/subtitles/
part en pixels de sortie, calculees par reframe) et
- dessine ``screen_title`` (captions.json) pendant tout le clip : texte noir
  Poppins ExtraBold et emoji en couleur sur un encadre blanc a coins
  arrondis, centre dans la zone title, son bas a ``title_lift`` px du bas de
  celle-ci. Le texte
  est coupe en segments texte / emoji par classe Unicode
  (Extended_Pictographic), passe a la ligne (2 lignes au plus) puis baisse
  de taille par paliers jusqu'a ce que l'encadre tienne ; sinon RenderError
  (jamais tronque ni debordant). Le titre est rasterise en PNG transparent
  (Pillow, taille de la zone title) puis incruste par ffmpeg (deuxieme
  entree, ``overlay`` sur toute la duree) ;
- n'affiche pas d'accroche de 2 s ;
- centre « Partie N » dans la zone part si parts_total > 1 (drawtext, taille
  en em comme Pillow, mesuree avec la vraie police, sinon RenderError) ;
- ecrit ``video_rect`` (le panneau main en pixels de sortie) dans le JSON.

Format stream (SPEC-3a88, ``layout = "stream"`` a la racine du plan) : meme
traitement du texte que letterbox (titre d'ecran dans ``text_zones.title``,
au-dessus de la camera ; « Partie N » dans la zone part ; sous-titres deja
places par subtitles dans la zone du jeu), panneaux ``camera`` (facecam
agrandie en haut) et ``gameplay`` (jeu en bas) rendus comme tout panneau.
Le JSON porte ``camera_rect`` (panneau camera) et ``video_rect`` (panneau
gameplay), en pixels de sortie.

Police emoji (``emoji_font``, vide = resolue par plateforme, voir
resolve_emoji_font) : Windows ``C:/Windows/Fonts/seguiemj.ttf`` ; Linux
``NotoColorEmoji.ttf`` (paquet fonts-noto-color-emoji), police bitmap qui ne
s'ouvre qu'a la taille 109 : chaque emoji est donc rasterise a
``emoji_raster_size`` (109) puis reduit a la taille du texte, sur toutes
les plateformes. Police absente = RenderError.

Appel a l'abonnement (SPEC-6a47, ``cta_enabled``, desactive par defaut :
rendu identique a SPEC-6127 sans configuration explicite). Letterbox et
stream seulement (``_TEXT_LAYOUTS``) ; ignore en crop (``cta`` reste ``false``
dans le JSON, pas une erreur : SPEC-6a47 fige ce format). ``cta_enabled``
sans ``cta_handle`` ou sans ``cta_text``, ou ``cta_seconds`` <= 0 ou >= la
duree du clip, est une erreur explicite (ADR-ad2e, jamais de CTA a moitie
active) :
- pseudo de chaine (``cta_handle``) sous l'encadre du titre, dans la meme
  bande floue du haut, pendant tout le clip : texte seul (pas d'encadre),
  mesure avec la vraie police (``layout_pseudo``), reduit par paliers. Le
  titre remonte de la hauteur ainsi reservee (``title_lift`` effectif plus
  grand), rendu ensuite comme d'habitude (``title_png``) puis le pseudo est
  dessine sur le meme PNG (``_draw_pseudo``) ;
- carte de fin (``cta_text``, defaut « Abonne-toi ! ») sur les
  ``cta_seconds`` dernieres secondes (defaut 2 s) : encadre blanc/texte noir
  centre dans ``text_zones.subtitles`` (``layout_cta_card``/``cta_card_png``,
  meme style que le titre mais centre, pas ancre en bas), incruste par-dessus
  les sous-titres via un overlay ffmpeg ``enable='gte(t,cutoff)'``. Les
  sous-titres de cette fenetre sont retires d'une copie du .ass
  (``truncate_ass_for_cta``) : le fichier ecrit par l'etape subtitles n'est
  jamais modifie (ADR-b16b, render n'importe pas subtitles.py).
"""

from __future__ import annotations

import bisect
import functools
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

from clipper.gpu import get_device

CONFIG_DEFAULTS: dict[str, object] = {
    # Cadence de sortie maximale du clip, en images par seconde.
    # Cadence de sortie (SPEC-6127) : imposée, quelle que soit la cadence source.
    "max_fps": 30,
    "crf": 20,
    "x264_preset": "medium",
    "nvenc_preset": "p5",
    "audio_bitrate": "192k",
    "loudnorm_i": -14.0,
    "loudnorm_tp": -1.5,
    "loudnorm_lra": 11.0,
    "hook_seconds": 2.0,
    "hook_font_size": 64,
    "hook_font_color": "white",
    "hook_margin_top": 100,
    "part_font_size": 48,
    "part_font_color": "white",
    "part_margin": 40,
    # Miniature JPEG d'un clip rendu (galerie de l'interface web) : une seule
    # image, largeur maximale en pixels, prise à thumbnail_seek secondes.
    "thumbnail_width": 360,
    "thumbnail_seek": 0.5,
    # Vignette d'une vidéo source (liste Vidéos, tableau de bord) : image prise
    # à cette fraction de la durée de la source (0.1 = 10 %).
    "video_thumbnail_seek_ratio": 0.1,
    "blur_radius": 20,
    "blur_power": 2,
    # Le fond flou (fallback_blur) est calculé sur une image réduite d'un
    # facteur blur_downscale puis remis à la taille de dest : boxblur sur
    # 1080x1920 en plein cadre est le coût dominant d'un rendu fallback_blur
    # (constat essai réel 2026-09-25, ~1200s CPU pour 40s de clip).
    "blur_downscale": 4,
    # Écart toléré (s) entre les bornes de captions.json et celles du plan
    # reframe : au-delà, les entrées sont jugées incohérentes.
    "start_end_tolerance": 0.15,
    # Taille de police du titre d'écran en format letterbox.
    # Format letterbox (SPEC-6127) : titre d'écran sur encadré blanc. Tailles
    # en pixels par em (même unité pour Pillow et drawtext).
    "title_font_size": 64,
    "title_font_size_min": 36,
    "title_font_size_step": 4,
    "title_line_height": 1.25,  # interligne, en em
    "title_emoji_scale": 0.9,  # hauteur de l'emoji, en em
    # Espace ajouté entre un segment texte et un emoji qui se suivent, en em
    # (compte dans la largeur mesurée de l'encadré).
    "title_emoji_gap": 0.25,
    "title_pad_x": 28,
    "title_pad_y": 16,
    "title_radius": 22,
    # Écart, en pixels, entre le titre d'écran et la vidéo.
    # Écart (px de sortie) entre le bas de l'encadré du titre et le bas de sa
    # zone (TASK-ea6e : trop collé à la vidéo sans lui).
    "title_lift": 40,
    # Police emoji couleur : "" = résolue par plateforme (resolve_emoji_font).
    "emoji_font": "",
    # Taille de rasterisation des emojis, réduits ensuite : NotoColorEmoji
    # (Linux) ne s'ouvre qu'à 109.
    "emoji_raster_size": 109,
    # « Partie N » en letterbox (taille : part_font_size, couleur : part_font_color).
    "part_border": 4,
    # Active l'appel à l'abonnement (pseudo de chaîne et carte de fin) ; désactivé par défaut.
    # Appel à l'abonnement (SPEC-6a47), désactivé par défaut : sans
    # configuration explicite, le rendu reste identique à SPEC-6127. Ne
    # s'applique qu'aux layouts letterbox/stream (_TEXT_LAYOUTS) ; le format
    # crop reste figé (ADR-ad2e : jamais appliqué en silence hors de ces deux
    # layouts, jamais non plus à moitié active sans cta_handle/cta_text).
    "cta_enabled": False,
    "cta_handle": "",
    "cta_seconds": 2.0,
    "cta_text": "Abonne-toi !",
    # Pseudo de chaîne, sous le titre d'écran, dans la même bande floue du
    # haut (texte discret, sans encadré) : tailles en pixels d'em.
    "cta_handle_font_size": 32,
    "cta_handle_font_size_min": 20,
    "cta_handle_font_size_step": 2,
    "cta_handle_font_color": "white",
    "cta_handle_outline": 3,
    # Écart (px) entre le bas de l'encadré du titre et le pseudo.
    "cta_handle_gap": 8,
    # Carte de fin (encadré blanc, texte noir, même style que le titre mais
    # centrée dans sa zone) : tailles en pixels d'em.
    "cta_card_font_size": 56,
    "cta_card_font_size_min": 32,
    "cta_card_font_size_step": 4,
    "cta_card_pad_x": 28,
    "cta_card_pad_y": 16,
    "cta_card_radius": 22,
    "cta_card_line_height": 1.25,
    # Affiche un titre d'écran sur le clip.
    # Titre d'écran (SPEC-76dc, nouveau réglage : jusqu'ici toujours dessiné).
    # Défaut True = comportement inchangé. False : aucun titre, sur aucun
    # layout ; text_zones.title n'est alors plus requis.
    "title_enabled": True,
    # Affiche le badge de chaîne (logo et nom) entre la webcam et le jeu ; agencement stream split seulement.
    # Badge de chaîne (SPEC-76dc, agencement stream split seulement : la
    # zone badge n'existe que dans reframe/<clip_id>.json en stream_split,
    # erreur explicite sinon). Remplace le pseudo texte de l'appel a
    # l'abonnement (cta_handle) sur ce clip quand les deux sont actifs,
    # sans affecter la carte de fin.
    "badge_enabled": False,
    # Chemin du logo (PNG) du badge de chaîne.
    # Chemin du logo (PNG), requis si badge_enabled ; fichier absent = erreur
    # explicite (ADR-ad2e).
    "badge_logo": "",
    # Nom affiché à droite du logo, requis si badge_enabled.
    "badge_name": "",
    # Côté (px) du carré qui contient le logo.
    "badge_logo_size": 100,
    # Le glyphe du logo est réduit de ce facteur à l'intérieur du carré
    # (marge visuelle autour de lui).
    "badge_glyph_scale": 0.65,
    "badge_font_size": 40,
    # Couleur de remplissage du carré du logo (jamais de noir par défaut) :
    # vide ("") = échantillonnée automatiquement au coin (0, 0) de l'image
    # du logo elle-meme (le fond du logo Twitch par ex. est déjà viole dans
    # le PNG). Toujours appliquée, quel que soit badge_background.
    "badge_logo_fill": "",
    # Fond du bandeau badge (couleur PIL, ex. "black") ou "none" : dans ce
    # cas aucun rectangle n'est dessiné derrière le nom (le carré du logo
    # garde toujours son propre remplissage, badge_logo_fill ci-dessus) ; le
    # nom reste lisible via badge_name_outline / badge_name_shadow_*
    # ci-dessous. Défaut "black" = bandeau plein, comportement SPEC-76dc
    # inchangé.
    "badge_background": "black",
    "badge_name_outline_color": "black",
    "badge_name_outline": 3,
    "badge_name_shadow_enabled": False,
    "badge_name_shadow_color": "black",
    "badge_name_shadow_offset": [2, 2],
}

FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
FONT_FILE = FONTS_DIR / "Poppins-ExtraBold.ttf"

# Polices emoji couleur cherchees quand [render] emoji_font est vide.
EMOJI_FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "win32": ("C:/Windows/Fonts/seguiemj.ttf",),
    "linux": (
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto-emoji/NotoColorEmoji.ttf",
    ),
}

_QA_DEFAULT: dict[str, Any] = {"status": "skipped", "issues": []}
# Mises en page a titre d'ecran permanent et zones de texte fixes (text_zones).
_TEXT_LAYOUTS = ("letterbox", "stream", "stream_split")
# Champ du sidecar JSON -> nom du panneau video, par layout (SPEC-6127,
# SPEC-3a88, SPEC-76dc) : lu par render() pour la qa.
_VIDEO_RECT_FIELDS: dict[str, dict[str, str]] = {
    "letterbox": {"video_rect": "main"},
    "stream": {"camera_rect": "camera", "video_rect": "gameplay"},
    "stream_split": {"webcam_rect": "webcam", "video_rect": "gameplay"},
}
_EDGE = 0.1
_EPS = 1e-6


class RenderError(Exception):
    """Entree manquante ou incoherente, ou echec de ffmpeg/ffprobe."""


# --------------------------------------------------------------------------
# Entrees
# --------------------------------------------------------------------------


def _settings(config: Any) -> dict[str, Any]:
    if config is None:
        from clipper.config import load_config

        config = load_config()
    return {**CONFIG_DEFAULTS, **config.section("render")}


def _read_json(path: Path, optional: bool = False) -> Any:
    if not path.exists():
        if optional:
            return None
        raise RenderError(f"entree absente : {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _find(items: list[dict[str, Any]], key: str, value: Any, where: str) -> dict[str, Any]:
    for item in items:
        if item[key] == value:
            return item
    raise RenderError(f"{key}={value!r} absent de {where}")


def _clip_transcript(transcript: dict[str, Any], start: float, end: float) -> str:
    words = [
        w
        for seg in transcript.get("segments", [])
        for w in (seg.get("words") or [])
        if w["start"] >= start - _EDGE - _EPS and w["end"] <= end + _EDGE + _EPS
    ]
    return "".join(w["word"] for w in words).strip()


# --------------------------------------------------------------------------
# Chemins dans le filtre ffmpeg (jamais un ':' de lettre de lecteur nu)
# --------------------------------------------------------------------------


def _filter_path(target: Path, cwd: Path) -> str:
    try:
        rel = os.path.relpath(target, cwd)
    except ValueError:
        return target.resolve().as_posix().replace(":", "\\:")
    return Path(rel).as_posix()


# --------------------------------------------------------------------------
# Letterbox : titre d'ecran (texte + emoji) rasterise par Pillow
# --------------------------------------------------------------------------

# Extended_Pictographic (Unicode emoji-data.txt), en intervalles fermes tries.
_PICTO_RANGES: tuple[tuple[int, int], ...] = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122),
    (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328),
    (0x2388, 0x2388), (0x23CF, 0x23CF), (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2),
    (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x2605),
    (0x2607, 0x2612), (0x2614, 0x2685), (0x2690, 0x2705), (0x2708, 0x2712), (0x2714, 0x2714),
    (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721), (0x2728, 0x2728), (0x2733, 0x2734),
    (0x2744, 0x2744), (0x2747, 0x2747), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755),
    (0x2757, 0x2757), (0x2763, 0x2767), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0),
    (0x27BF, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50),
    (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299),
    (0x1F000, 0x1F0FF), (0x1F10D, 0x1F10F), (0x1F12F, 0x1F12F), (0x1F16C, 0x1F171),
    (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F1AD, 0x1F1E5),
    (0x1F201, 0x1F20F), (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A),
    (0x1F23C, 0x1F23F), (0x1F249, 0x1F3FA), (0x1F400, 0x1F53D), (0x1F546, 0x1F64F),
    (0x1F680, 0x1F6FF), (0x1F774, 0x1F77F), (0x1F7D5, 0x1F7FF), (0x1F80C, 0x1F80F),
    (0x1F848, 0x1F84F), (0x1F85A, 0x1F85F), (0x1F888, 0x1F88F), (0x1F8AE, 0x1F8FF),
    (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945), (0x1F947, 0x1FAFF), (0x1FC00, 0x1FFFD),
)
_PICTO_STARTS = [lo for lo, _hi in _PICTO_RANGES]
_ZWJ = 0x200D


def _is_pictographic(cp: int) -> bool:
    i = bisect.bisect_right(_PICTO_STARTS, cp) - 1
    return i >= 0 and cp <= _PICTO_RANGES[i][1]


def _is_regional_indicator(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def _is_emoji_extender(cp: int) -> bool:
    """VS16, keycap, modificateur de teint, etiquettes (drapeaux de region)."""
    return cp in (0xFE0F, 0x20E3) or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F


def _emoji_start(cp: int) -> bool:
    return _is_pictographic(cp) or _is_regional_indicator(cp)


def split_segments(text: str) -> list[tuple[str, str]]:
    """Decoupe ``text`` en segments ``("text", ...)`` / ``("emoji", ...)`` par
    classe Unicode : un emoji est un point de code Extended_Pictographic (ou
    une paire d'indicateurs regionaux) suivi de ses extensions (VS16, teint,
    keycap, ZWJ + pictogramme)."""
    segments: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        cp = ord(text[i])
        j = i + 1
        if _emoji_start(cp):
            if _is_regional_indicator(cp) and j < n and _is_regional_indicator(ord(text[j])):
                j += 1
            while j < n:
                c = ord(text[j])
                if _is_emoji_extender(c):
                    j += 1
                elif c == _ZWJ and j + 1 < n and _emoji_start(ord(text[j + 1])):
                    j += 2
                else:
                    break
            segments.append(("emoji", text[i:j]))
        else:
            while j < n and not _emoji_start(ord(text[j])):
                j += 1
            segments.append(("text", text[i:j]))
        i = j
    return segments


def resolve_emoji_font(settings: dict[str, Any]) -> Path:
    """Police emoji couleur : ``emoji_font`` si renseigne, sinon la premiere
    police connue presente pour la plateforme (EMOJI_FONT_CANDIDATES).
    Absente = RenderError (jamais d'emoji en noir et blanc en silence)."""
    configured = str(settings.get("emoji_font") or "")
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise RenderError(f"police emoji absente : {path} (reglage [render] emoji_font)")
        return path
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    candidates = EMOJI_FONT_CANDIDATES.get(platform, ())
    for candidate in candidates:
        if Path(candidate).is_file():
            return Path(candidate)
    raise RenderError(
        f"police emoji couleur introuvable sur {sys.platform} (cherchee : {', '.join(candidates) or 'aucune connue'}) : "
        "installer NotoColorEmoji (Linux : paquet fonts-noto-color-emoji) ou renseigner [render] emoji_font"
    )


@functools.lru_cache(maxsize=None)
def _cmap(font_path: str) -> frozenset[int]:
    font = TTFont(font_path, lazy=True, fontNumber=0)
    try:
        return frozenset(font.getBestCmap() or {})
    finally:
        font.close()


@functools.lru_cache(maxsize=None)
def _text_font(size: int) -> ImageFont.FreeTypeFont:
    if not FONT_FILE.is_file():
        raise RenderError(f"police absente : {FONT_FILE}")
    return ImageFont.truetype(str(FONT_FILE), size)


@functools.lru_cache(maxsize=None)
def _emoji_raster(cluster: str, font_path: str, raster_size: int) -> Image.Image:
    """L'emoji ``cluster`` rasterise en couleur a ``raster_size`` et recadre
    sur ses pixels visibles."""
    cmap = _cmap(font_path)
    missing = [c for c in cluster if ord(c) not in cmap and ord(c) not in (0xFE0F, _ZWJ)]
    if missing:
        raise RenderError(f"emoji {cluster!r} absent de la police emoji {font_path}")
    try:
        font = ImageFont.truetype(font_path, raster_size)
    except OSError as exc:
        raise RenderError(f"police emoji illisible a la taille {raster_size} : {font_path} ({exc})") from exc
    canvas = Image.new("RGBA", (raster_size * 4, raster_size * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text(
        (raster_size // 2, raster_size * 3 // 2), cluster, font=font, embedded_color=True, anchor="ls"
    )
    bbox = canvas.getbbox()
    if bbox is None:
        raise RenderError(f"emoji {cluster!r} sans rendu avec {font_path}")
    return canvas.crop(bbox)


@dataclass
class TitleLayout:
    """Mise en page du titre d'ecran, en pixels de sortie."""

    font_size: int
    lines: list[str]
    box: tuple[int, int, int, int]  # encadre blanc (x0, y0, x1, y1)
    emoji_boxes: list[tuple[int, int, int, int]] = field(default_factory=list)
    # (kind, contenu, x, y) : texte ancre sur sa ligne de base, emoji par son coin haut-gauche.
    items: list[tuple[str, str, int, int]] = field(default_factory=list)


def _zone(zone: dict[str, Any]) -> tuple[int, int, int, int]:
    return int(zone["x0"]), int(zone["y0"]), int(zone["x1"]), int(zone["y1"])


def _step_sizes(start: int, low: int, step: int) -> list[int]:
    """Tailles de ``start`` a ``low`` par paliers de ``step``, ``low`` toujours
    inclus meme s'il n'est pas un multiple exact du pas."""
    step = max(1, step)
    sizes = list(range(start, low - 1, -step))
    if not sizes or sizes[-1] != low:
        sizes.append(low)
    return sizes


def _font_sizes(settings: dict[str, Any]) -> list[int]:
    return _step_sizes(
        int(settings["title_font_size"]), int(settings["title_font_size_min"]),
        int(settings["title_font_size_step"]),
    )


def _cta_handle_sizes(settings: dict[str, Any]) -> list[int]:
    return _step_sizes(
        int(settings["cta_handle_font_size"]), int(settings["cta_handle_font_size_min"]),
        int(settings["cta_handle_font_size_step"]),
    )


def _cta_card_sizes(settings: dict[str, Any]) -> list[int]:
    return _step_sizes(
        int(settings["cta_card_font_size"]), int(settings["cta_card_font_size_min"]),
        int(settings["cta_card_font_size_step"]),
    )


@dataclass
class PseudoLayout:
    """Mise en page du pseudo de chaine (texte seul, sans encadre), en pixels
    de sortie."""

    font_size: int
    width: float
    height: int
    top: int  # decalage (px) du haut du texte au-dessus de sa ligne de base


def layout_pseudo(text: str, zone_width: int, settings: dict[str, Any]) -> PseudoLayout:
    """Mise en page du pseudo de chaine (``cta_handle``) : une seule ligne,
    mesuree avec la vraie police, reduite par paliers (``cta_handle_font_size``
    a ``cta_handle_font_size_min``) jusqu'a tenir dans ``zone_width`` ;
    RenderError si meme la taille minimale deborde (ADR-ad2e : jamais tronque
    ni debordant en silence)."""
    if not text.strip():
        raise RenderError("pseudo de chaine vide (reglage [render] cta_handle)")
    if not FONT_FILE.is_file():
        raise RenderError(f"police absente : {FONT_FILE}")
    text_cmap = _cmap(str(FONT_FILE))
    missing = sorted({c for c in text if not c.isspace() and ord(c) not in text_cmap})
    if missing:
        raise RenderError(
            f"caractere(s) {''.join(missing)!r} du pseudo de chaine absent(s) de Poppins ExtraBold : {text!r}"
        )
    for size in _cta_handle_sizes(settings):
        font = _text_font(size)
        width = font.getlength(text)
        if width <= zone_width:
            _left, top, _right, bottom = font.getbbox(text, anchor="ls")
            return PseudoLayout(font_size=size, width=width, height=bottom - top, top=top)
    raise RenderError(
        f"pseudo de chaine trop long pour sa zone ({zone_width} px) meme a la taille "
        f"{settings['cta_handle_font_size_min']} (reglage [render] cta_handle) : {text!r}"
    )


def layout_title(text: str, zone: dict[str, Any], settings: dict[str, Any]) -> TitleLayout:
    """Mesure ``text`` avec les vraies polices et place l'encadre blanc dans
    ``zone`` : centre horizontalement, colle en bas. Une ligne, puis deux,
    puis une taille plus petite par paliers ; RenderError s'il ne tient pas a
    la taille minimale."""
    words = text.split()
    if not words:
        raise RenderError("titre d'ecran vide")
    segments_by_word = [split_segments(w) for w in words]
    if not FONT_FILE.is_file():
        raise RenderError(f"police absente : {FONT_FILE}")
    text_cmap = _cmap(str(FONT_FILE))
    for kind, seg in (s for segs in segments_by_word for s in segs):
        if kind == "text":
            missing = sorted({c for c in seg if ord(c) not in text_cmap})
            if missing:
                raise RenderError(f"caractere(s) {''.join(missing)!r} du titre absent(s) de Poppins ExtraBold : {text!r}")
    emoji_font = None
    if any(kind == "emoji" for segs in segments_by_word for kind, _ in segs):
        emoji_font = str(resolve_emoji_font(settings))
    raster_size = int(settings["emoji_raster_size"])

    title_lift = int(settings["title_lift"])
    if title_lift < 0:
        raise RenderError(f"title_lift doit etre >= 0, recu {title_lift} (reglage [render] title_lift)")

    zx0, zy0, zx1, zy1 = _zone(zone)
    zone_w, zone_h = zx1 - zx0, zy1 - zy0 - title_lift
    pad_x, pad_y = int(settings["title_pad_x"]), int(settings["title_pad_y"])

    for size in _font_sizes(settings):
        font = _text_font(size)
        emoji_h = max(1, round(size * float(settings["title_emoji_scale"])))

        def emoji_w(cluster: str) -> int:
            img = _emoji_raster(cluster, emoji_font, raster_size)
            return max(1, round(img.width * emoji_h / img.height))

        def line_segments(line_words: list[list[tuple[str, str]]]) -> list[tuple[str, str]]:
            segs: list[tuple[str, str]] = []
            for k, word_segs in enumerate(line_words):
                if k:
                    segs.append(("text", " "))
                segs.extend(word_segs)
            return segs

        gap = round(size * float(settings["title_emoji_gap"]))

        def transitions(segs: list[tuple[str, str]]) -> int:
            return sum(1 for (k1, _), (k2, _) in zip(segs, segs[1:]) if k1 != k2)

        def width(segs: list[tuple[str, str]]) -> float:
            return sum(font.getlength(s) if kind == "text" else emoji_w(s) for kind, s in segs) + gap * transitions(segs)

        def span_width(a: int, b: int) -> float:
            return width(line_segments(segments_by_word[a:b]))

        # Une ligne, puis la coupure en deux lignes la plus equilibree.
        n = len(words)
        candidates = [[(0, n)]]
        if n > 1:
            k = min(range(1, n), key=lambda k: max(span_width(0, k), span_width(k, n)))
            candidates.append([(0, k), (k, n)])
        line_h = round(size * float(settings["title_line_height"]))
        for spans in candidates:
            segs_per_line = [line_segments(segments_by_word[a:b]) for a, b in spans]
            widths = [width(s) for s in segs_per_line]
            box_w = math.ceil(max(widths)) + 2 * pad_x
            box_h = len(spans) * line_h + 2 * pad_y
            if box_w > zone_w or box_h > zone_h:
                continue
            bx0 = zx0 + (zone_w - box_w) // 2
            by0 = zy1 - title_lift - box_h
            cap = -font.getbbox("H", anchor="ls")[1]
            layout = TitleLayout(
                font_size=size,
                lines=[" ".join(words[a:b]) for a, b in spans],
                box=(bx0, by0, bx0 + box_w, by0 + box_h),
            )
            for i, segs in enumerate(segs_per_line):
                baseline = by0 + pad_y + i * line_h + round((line_h + cap) / 2)
                x = bx0 + (box_w - widths[i]) / 2
                for j, (kind, seg) in enumerate(segs):
                    if j and segs[j - 1][0] != kind:
                        x += gap
                    if kind == "text":
                        layout.items.append(("text", seg, round(x), baseline))
                        x += font.getlength(seg)
                    else:
                        w = emoji_w(seg)
                        top = round(baseline - cap / 2 - emoji_h / 2)
                        layout.items.append(("emoji", seg, round(x), top))
                        layout.emoji_boxes.append((round(x), top, round(x) + w, top + emoji_h))
                        x += w
            return layout
    raise RenderError(
        f"titre d'ecran trop long pour sa zone ({zone_w}x{zone_h} px) meme a la taille "
        f"{settings['title_font_size_min']} sur 2 lignes : {text!r}"
    )


def title_png(text: str, zone: dict[str, Any], settings: dict[str, Any], path: Path) -> TitleLayout:
    """Ecrit dans ``path`` le titre d'ecran en PNG transparent de la taille
    de ``zone`` (a incruster en (x0, y0) de la zone) ; renvoie sa mise en page."""
    layout = layout_title(text, zone, settings)
    zx0, zy0, zx1, zy1 = _zone(zone)
    img = Image.new("RGBA", (zx1 - zx0, zy1 - zy0), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bx0, by0, bx1, by1 = layout.box
    draw.rounded_rectangle(
        (bx0 - zx0, by0 - zy0, bx1 - zx0 - 1, by1 - zy0 - 1), radius=int(settings["title_radius"]), fill="white"
    )
    font = _text_font(layout.font_size)
    emojis = [item for item in layout.items if item[0] == "emoji"]
    if emojis:
        emoji_font = str(resolve_emoji_font(settings))
    for (_kind, content, x, y), box in zip(emojis, layout.emoji_boxes):
        raster = _emoji_raster(content, emoji_font, int(settings["emoji_raster_size"]))
        scaled = raster.resize((box[2] - box[0], box[3] - box[1]), Image.LANCZOS)
        img.alpha_composite(scaled, (x - zx0, y - zy0))
    for kind, content, x, y in layout.items:
        if kind == "text":
            draw.text((x - zx0, y - zy0), content, font=font, fill="black", anchor="ls")
    img.save(path, format="PNG")
    return layout


# --------------------------------------------------------------------------
# Carte de fin (SPEC-6a47) : encadre blanc/texte noir, style du titre mais
# centre (pas ancre en bas) dans sa zone ; pas d'emoji (cta_text est un texte
# de configuration, pas une reponse LLM soumise a la meme regle que le titre).
# --------------------------------------------------------------------------


def layout_cta_card(text: str, zone: dict[str, Any], settings: dict[str, Any]) -> TitleLayout:
    """Mise en page de la carte de fin (``cta_text``) : encadre blanc centre
    horizontalement et verticalement dans ``zone`` (contrairement au titre,
    qui est ancre en bas). Une ligne, puis deux, puis taille reduite par
    paliers (``cta_card_font_size`` a ``cta_card_font_size_min``) ; RenderError
    si ca ne tient toujours pas (ADR-ad2e)."""
    words = text.split()
    if not words:
        raise RenderError("texte de la carte de fin vide (reglage [render] cta_text)")
    if not FONT_FILE.is_file():
        raise RenderError(f"police absente : {FONT_FILE}")
    text_cmap = _cmap(str(FONT_FILE))
    missing = sorted({c for c in text if not c.isspace() and ord(c) not in text_cmap})
    if missing:
        raise RenderError(
            f"caractere(s) {''.join(missing)!r} de la carte de fin absent(s) de Poppins ExtraBold : {text!r}"
        )

    zx0, zy0, zx1, zy1 = _zone(zone)
    zone_w, zone_h = zx1 - zx0, zy1 - zy0
    pad_x, pad_y = int(settings["cta_card_pad_x"]), int(settings["cta_card_pad_y"])

    n = len(words)
    for size in _cta_card_sizes(settings):
        font = _text_font(size)
        line_h = round(size * float(settings["cta_card_line_height"]))
        candidates = [[(0, n)]]
        if n > 1:
            k = min(
                range(1, n),
                key=lambda k: max(font.getlength(" ".join(words[:k])), font.getlength(" ".join(words[k:]))),
            )
            candidates.append([(0, k), (k, n)])
        for spans in candidates:
            lines = [" ".join(words[a:b]) for a, b in spans]
            widths = [font.getlength(line) for line in lines]
            box_w = math.ceil(max(widths)) + 2 * pad_x
            box_h = len(spans) * line_h + 2 * pad_y
            if box_w > zone_w or box_h > zone_h:
                continue
            bx0 = zx0 + (zone_w - box_w) // 2
            by0 = zy0 + (zone_h - box_h) // 2
            cap = -font.getbbox("H", anchor="ls")[1]
            layout = TitleLayout(font_size=size, lines=lines, box=(bx0, by0, bx0 + box_w, by0 + box_h))
            for i, line in enumerate(lines):
                baseline = by0 + pad_y + i * line_h + round((line_h + cap) / 2)
                x = bx0 + (box_w - widths[i]) / 2
                layout.items.append(("text", line, round(x), baseline))
            return layout
    raise RenderError(
        f"carte de fin trop longue pour sa zone ({zone_w}x{zone_h} px) meme a la taille "
        f"{settings['cta_card_font_size_min']} sur 2 lignes (reglage [render] cta_text) : {text!r}"
    )


def cta_card_png(text: str, zone: dict[str, Any], settings: dict[str, Any], path: Path) -> TitleLayout:
    """Ecrit dans ``path`` la carte de fin en PNG transparent de la taille de
    ``zone`` ; renvoie sa mise en page."""
    layout = layout_cta_card(text, zone, settings)
    zx0, zy0, zx1, zy1 = _zone(zone)
    img = Image.new("RGBA", (zx1 - zx0, zy1 - zy0), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bx0, by0, bx1, by1 = layout.box
    draw.rounded_rectangle(
        (bx0 - zx0, by0 - zy0, bx1 - zx0 - 1, by1 - zy0 - 1), radius=int(settings["cta_card_radius"]), fill="white"
    )
    font = _text_font(layout.font_size)
    for _kind, content, x, y in layout.items:
        draw.text((x - zx0, y - zy0), content, font=font, fill="black", anchor="ls")
    img.save(path, format="PNG")
    return layout


_BADGE_GAP = 16  # px entre le carre du logo et le nom


def badge_png(logo_path: Path, name: str, zone: dict[str, Any], settings: dict[str, Any], path: Path) -> None:
    """Ecrit dans ``path`` le badge de chaine (SPEC-76dc, agencement stream
    split) : logo dans un carre de ``badge_logo_size`` rempli de
    ``badge_logo_fill`` (couleur echantillonnee au coin de l'image du logo
    par defaut, jamais de noir), glyphe reduit de ``badge_glyph_scale``
    (jamais deforme) centre dedans, nom mesure avec la vraie police. Le
    groupe logo + marge + nom est centre horizontalement sur le centre de
    ``zone``. ``badge_background`` ("black" par defaut) dessine un
    rectangle plein derriere tout le bandeau ; "none" ne dessine aucun
    rectangle derriere le nom (le carre du logo, lui, garde toujours son
    remplissage quel que soit ``badge_background``), le nom restant
    lisible via ``badge_name_outline``/``badge_name_shadow_*``."""
    if not logo_path.is_file():
        raise RenderError(f"logo du badge introuvable : {logo_path} (reglage [render] badge_logo)")
    if not name.strip():
        raise RenderError("[render] badge_enabled sans badge_name")
    if not FONT_FILE.is_file():
        raise RenderError(f"police absente : {FONT_FILE}")
    text_cmap = _cmap(str(FONT_FILE))
    missing = sorted({c for c in name if not c.isspace() and ord(c) not in text_cmap})
    if missing:
        raise RenderError(
            f"caractere(s) {''.join(missing)!r} du nom du badge absent(s) de Poppins ExtraBold : {name!r}"
        )

    zx0, zy0, zx1, zy1 = _zone(zone)
    zw, zh = zx1 - zx0, zy1 - zy0
    square = int(settings["badge_logo_size"])
    if square <= 0 or square > zw or square > zh:
        raise RenderError(
            f"[render] badge_logo_size ({square}) invalide pour badge_dest ({zw}x{zh})"
        )

    font_size = int(settings["badge_font_size"])
    font = _text_font(font_size)
    left, top, right, bottom = font.getbbox(name, anchor="ls")
    text_w = right - left
    content_w = square + _BADGE_GAP + text_w
    if content_w > zw:
        raise RenderError(
            f"badge_name {name!r} trop long pour badge_dest ({zw} px, logo {square}px + marge {_BADGE_GAP}px) "
            "(reglage [render] badge_name)"
        )
    group_x0 = (zw - content_w) // 2
    square_top = (zh - square) // 2

    background = str(settings["badge_background"]).strip()
    no_background = background.lower() == "none"
    img = Image.new("RGBA", (zw, zh), (0, 0, 0, 0) if no_background else background)
    draw = ImageDraw.Draw(img)

    logo = Image.open(logo_path).convert("RGBA")
    fill_setting = str(settings["badge_logo_fill"]).strip()
    if fill_setting:
        logo_fill: str | tuple[int, int, int, int] = fill_setting
    else:
        r, g, b, _a = logo.getpixel((0, 0))
        logo_fill = (r, g, b, 255)
    draw.rectangle(
        (group_x0, square_top, group_x0 + square - 1, square_top + square - 1), fill=logo_fill,
    )

    glyph_max = max(1, round(square * float(settings["badge_glyph_scale"])))
    ratio = logo.width / logo.height
    gw, gh = (glyph_max, max(1, round(glyph_max / ratio))) if ratio >= 1 else (max(1, round(glyph_max * ratio)), glyph_max)
    logo = logo.resize((gw, gh), Image.LANCZOS)
    img.alpha_composite(logo, (group_x0 + (square - gw) // 2, square_top + (square - gh) // 2))

    text_x = group_x0 + square + _BADGE_GAP
    baseline = (zh - (bottom - top)) // 2 - top
    if bool(settings["badge_name_shadow_enabled"]):
        offset = settings["badge_name_shadow_offset"]
        if (
            not isinstance(offset, (list, tuple)) or len(offset) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in offset)
        ):
            raise RenderError(f"[render] badge_name_shadow_offset invalide {offset!r} (attendu [x, y])")
        ox, oy = float(offset[0]), float(offset[1])
        draw.text(
            (text_x + ox, baseline + oy), name, font=font,
            fill=str(settings["badge_name_shadow_color"]), anchor="ls",
        )
    draw.text(
        (text_x, baseline), name, font=font, fill="white", anchor="ls",
        stroke_width=int(settings["badge_name_outline"]), stroke_fill=str(settings["badge_name_outline_color"]),
    )
    img.save(path, format="PNG")


def check_cta_handle_gap(settings: dict[str, Any]) -> None:
    """Ecart titre/pseudo (reglable par style depuis l'editeur d'agencement,
    TASK-3be3) : entier >= 0, sinon RenderError (jamais un pseudo colle
    dans l'encadre du titre en silence)."""
    gap = settings["cta_handle_gap"]
    if not isinstance(gap, int) or isinstance(gap, bool) or gap < 0:
        raise RenderError(f"[render] cta_handle_gap doit etre un entier >= 0, recu {gap!r}")


def _draw_pseudo(
    png_path: Path, zone: dict[str, Any], title_layout: TitleLayout, text: str,
    pseudo: PseudoLayout, settings: dict[str, Any],
) -> None:
    """Dessine le pseudo de chaine sous l'encadre du titre (``title_layout``),
    sur le PNG deja ecrit par ``title_png`` pour la meme ``zone`` : texte
    discret, sans encadre, colle au bas de l'espace reserve par ``render``
    (``title_lift`` effectif)."""
    zx0, zy0, zx1, _zy1 = _zone(zone)
    img = Image.open(png_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    font = _text_font(pseudo.font_size)
    x = zx0 + (zx1 - zx0 - pseudo.width) / 2
    gap = int(settings["cta_handle_gap"])
    _bx0, _by0, _bx1, by1 = title_layout.box
    baseline = by1 + gap - pseudo.top
    draw.text(
        (x - zx0, baseline - zy0), text, font=font, fill=str(settings["cta_handle_font_color"]),
        anchor="ls", stroke_width=int(settings["cta_handle_outline"]), stroke_fill="black",
    )
    img.save(png_path, format="PNG")


def _part_placement(text: str, zone: dict[str, Any], settings: dict[str, Any]) -> int:
    """Ligne de base (y, pixels de sortie) qui centre verticalement ``text``
    (encre + bordure, mesuree avec Poppins a part_font_size) dans ``zone`` ;
    RenderError s'il n'y tient pas."""
    zx0, zy0, zx1, zy1 = _zone(zone)
    size, border = int(settings["part_font_size"]), int(settings["part_border"])
    font = _text_font(size)
    left, top, right, bottom = font.getbbox(text, anchor="ls", stroke_width=border)
    advance = font.getlength(text) + 2 * border
    if max(right - left, advance) > zx1 - zx0 or bottom - top > zy1 - zy0:
        raise RenderError(
            f"« {text} » ne tient pas dans sa zone ({zx1 - zx0}x{zy1 - zy0} px) a la taille {size} "
            "(reglage [render] part_font_size)"
        )
    return zy0 + ((zy1 - zy0) - (bottom - top)) // 2 - top


# --------------------------------------------------------------------------
# Filtergraph : un plan de recadrage a la fois
# --------------------------------------------------------------------------


def _fmt_num(value: Any) -> str:
    return f"{value:.6f}" if isinstance(value, float) else str(value)


def _time_expr(rects: list[dict[str, Any]], plan_start: float, key: str) -> str:
    """Expression ffmpeg (en ``t``, secondes depuis le debut du plan) pour
    ``key`` (x/y/w/h) : une constante s'il n'y a qu'un rectangle, sinon une
    chaine de ``if(lt(t, fin_relative), valeur, ...)``."""
    if len(rects) == 1:
        return _fmt_num(rects[0][key])
    expr = _fmt_num(rects[-1][key])
    for rect in reversed(rects[:-1]):
        rel_end = rect["end"] - plan_start
        expr = f"if(lt(t,{rel_end:.6f}),{_fmt_num(rect[key])},{expr})"
    return expr


def _panel_filters(
    panel: dict[str, Any], base_ref: str, plan_start: float, label: str, settings: dict[str, Any]
) -> tuple[list[str], str, dict[str, int]]:
    rects = panel["rects"]
    x = _time_expr(rects, plan_start, "x")
    y = _time_expr(rects, plan_start, "y")
    w = _time_expr(rects, plan_start, "w")
    h = _time_expr(rects, plan_start, "h")
    lines = [f"[{base_ref}]crop=w='{w}':h='{h}':x='{x}':y='{y}'[{label}c]"]
    cur = f"{label}c"
    if panel.get("effect") == "blur":
        factor = settings["blur_downscale"]
        lines.append(f"[{cur}]scale=iw/{factor}:ih/{factor}[{label}r]")
        cur = f"{label}r"
        lines.append(f"[{cur}]boxblur={settings['blur_radius']}:{settings['blur_power']}[{label}b]")
        cur = f"{label}b"
    dest = panel["dest"]
    lines.append(f"[{cur}]scale={dest['w']}:{dest['h']}[{label}s]")
    return lines, f"{label}s", dest


def _plan_filters(plan: dict[str, Any], index: int, out_w: int, out_h: int, settings: dict[str, Any],
                  source_offset: float = 0.0) -> tuple[list[str], str]:
    """Filtres d'un plan ; ``source_offset`` est le point ou l'entree source
    a ete positionnee (``-ss``) : le trim s'exprime relativement a lui."""
    label = f"p{index}"
    duration = plan["end"] - plan["start"]
    start, end = plan["start"] - source_offset, plan["end"] - source_offset
    lines = [
        f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[{label}base]"
    ]
    panels = plan["panels"]
    n = len(panels)
    if n > 1:
        lines.append(f"[{label}base]split={n}" + "".join(f"[{label}base{i}]" for i in range(n)))
        bases = [f"{label}base{i}" for i in range(n)]
    else:
        bases = [f"{label}base"]

    lines.append(f"color=c=black:s={out_w}x{out_h}:d={duration:.6f}[{label}canvas]")
    cur = f"{label}canvas"
    for i, panel in enumerate(panels):
        panel_lines, scaled, dest = _panel_filters(panel, bases[i], plan["start"], f"{label}_{i}", settings)
        lines.extend(panel_lines)
        nxt = f"{label}ov{i}"
        lines.append(f"[{cur}][{scaled}]overlay=x={dest['x']}:y={dest['y']}[{nxt}]")
        cur = nxt
    return lines, cur


def _build_filter_complex(
    reframe_data: dict[str, Any],
    clip_start: float,
    clip_end: float,
    ass_path: Path,
    hook_path: Path | None,
    part_path: Path | None,
    scratch_dir: Path,
    settings: dict[str, Any],
    *,
    title_input: int | None = None,
    source_offset: float = 0.0,
    cta_input: int | None = None,
    cta_start: float | None = None,
    badge_input: int | None = None,
) -> tuple[str, str]:
    """Graphe ffmpeg du clip. En letterbox (``layout`` = letterbox, stream ou
    stream_split a la racine de reframe_data), ``title_input`` (absent si
    ``[render] title_enabled`` est faux) est l'index de l'entree ffmpeg du
    PNG de titre, incruste en haut-gauche de la zone title pour tout le clip ;
    pas d'accroche ; « Partie N » (``part_path``) centre dans la zone part.
    ``badge_input`` (SPEC-76dc) : index de l'entree ffmpeg du PNG du badge de
    chaine, incruste sur la zone badge pour tout le clip. ``cta_input``
    (SPEC-6a47) : index de l'entree ffmpeg du PNG de la carte de fin,
    incruste sur la zone subtitles a partir de ``cta_start`` (s, relatif au
    debut du clip) jusqu'a la fin. ``source_offset`` : point (s) ou l'entree
    source est positionnee par ``-ss`` ; trim et atrim sont relatifs a lui."""
    letterbox = reframe_data.get("layout") in _TEXT_LAYOUTS
    out_w = reframe_data["output"]["width"]
    out_h = reframe_data["output"]["height"]

    lines: list[str] = []
    plan_labels: list[str] = []
    for i, plan in enumerate(reframe_data["plans"]):
        plan_lines, final_label = _plan_filters(plan, i, out_w, out_h, settings, source_offset)
        lines.extend(plan_lines)
        plan_labels.append(final_label)

    if len(plan_labels) > 1:
        lines.append(
            "".join(f"[{lbl}]" for lbl in plan_labels) + f"concat=n={len(plan_labels)}:v=1:a=0[vraw]"
        )
        cur = "vraw"
    else:
        cur = plan_labels[0]

    ass_rel = _filter_path(ass_path, scratch_dir)
    fonts_rel = _filter_path(FONTS_DIR, scratch_dir)
    lines.append(f"[{cur}]ass='{ass_rel}':fontsdir='{fonts_rel}'[vsub]")
    cur = "vsub"
    font_rel = _filter_path(FONT_FILE, scratch_dir)

    if letterbox:
        zones = reframe_data["text_zones"]
        if title_input is not None:
            tx0, ty0, _tx1, _ty1 = _zone(zones["title"])
            lines.append(f"[{cur}][{title_input}:v]overlay=x={tx0}:y={ty0}:eof_action=repeat[vtitle]")
            cur = "vtitle"
        if badge_input is not None:
            bx0, by0, _bx1, _by1 = _zone(zones["badge"])
            lines.append(f"[{cur}][{badge_input}:v]overlay=x={bx0}:y={by0}:eof_action=repeat[vbadge]")
            cur = "vbadge"
        if part_path is not None:
            px0, _py0, px1, _py1 = _zone(zones["part"])
            baseline = _part_placement(part_path.read_text(encoding="utf-8"), zones["part"], settings)
            part_rel = _filter_path(part_path, scratch_dir)
            lines.append(
                f"[{cur}]drawtext=textfile='{part_rel}':fontfile='{font_rel}':"
                f"fontsize={settings['part_font_size']}:fontcolor={settings['part_font_color']}:"
                f"borderw={settings['part_border']}:bordercolor=black:"
                f"x={px0}+({px1 - px0}-text_w)/2:y={baseline}:y_align=baseline[vpart]"
            )
            cur = "vpart"
        if cta_input is not None:
            if cta_start is None:
                raise RenderError("carte de fin : cta_start absent pour l'entree ffmpeg cta_input")
            sx0, sy0, _sx1, _sy1 = _zone(zones["subtitles"])
            lines.append(
                f"[{cur}][{cta_input}:v]overlay=x={sx0}:y={sy0}:enable='gte(t,{cta_start:.6f})'[vcta]"
            )
            cur = "vcta"
        lines.append(_audio_filter(clip_start - source_offset, clip_end - source_offset, settings))
        return ";".join(lines), cur

    if hook_path is None:
        raise RenderError("texte d'accroche absent pour un rendu hors letterbox")
    hook_rel = _filter_path(hook_path, scratch_dir)
    lines.append(
        f"[{cur}]drawtext=textfile='{hook_rel}':fontfile='{font_rel}':"
        f"fontsize={settings['hook_font_size']}:fontcolor={settings['hook_font_color']}:"
        f"x=(w-text_w)/2:y={settings['hook_margin_top']}:"
        f"enable='lt(t,{settings['hook_seconds']})'[vhook]"
    )
    cur = "vhook"

    if part_path is not None:
        part_rel = _filter_path(part_path, scratch_dir)
        lines.append(
            f"[{cur}]drawtext=textfile='{part_rel}':fontfile='{font_rel}':"
            f"fontsize={settings['part_font_size']}:fontcolor={settings['part_font_color']}:"
            f"x=w-text_w-{settings['part_margin']}:y={settings['part_margin']}[vpart]"
        )
        cur = "vpart"

    lines.append(_audio_filter(clip_start - source_offset, clip_end - source_offset, settings))
    return ";".join(lines), cur


def _audio_filter(clip_start: float, clip_end: float, settings: dict[str, Any]) -> str:
    return (
        f"[0:a]atrim=start={clip_start:.6f}:end={clip_end:.6f},asetpts=PTS-STARTPTS,"
        f"loudnorm=I={settings['loudnorm_i']}:TP={settings['loudnorm_tp']}:LRA={settings['loudnorm_lra']}[aout]"
    )


# --------------------------------------------------------------------------
# Carte de fin (SPEC-6a47) : les sous-titres sont absents pendant les
# dernieres cta_seconds. On tronque une COPIE du .ass ecrit par l'etape
# subtitles (jamais l'original sur disque - ADR-b16b, render n'importe pas
# subtitles.py, ce parseur lui est donc propre) : un evenement qui deborde du
# point de coupure est raccourci a ce point, un evenement qui commence apres
# est retire.
# --------------------------------------------------------------------------

_ASS_DIALOGUE_PREFIX = "Dialogue:"


def _parse_ass_time(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _format_ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centis = round(seconds * 100)
    cs = centis % 100
    total_seconds = centis // 100
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    m = total_minutes % 60
    h = total_minutes // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def truncate_ass_for_cta(ass_text: str, cutoff: float) -> str:
    """``ass_text`` avec tout evenement Dialogue absent au-dela de ``cutoff``
    (secondes depuis le debut du clip, jamais negatif) : un evenement qui
    deborde est raccourci a ``cutoff``, un evenement qui commence a ou apres
    ``cutoff`` est retire."""
    out_lines = []
    for line in ass_text.splitlines():
        if line.startswith(_ASS_DIALOGUE_PREFIX):
            rest = line[len(_ASS_DIALOGUE_PREFIX):].lstrip()
            fields = rest.split(",", 9)
            start, end = _parse_ass_time(fields[1]), _parse_ass_time(fields[2])
            if start >= cutoff:
                continue
            if end > cutoff:
                fields[2] = _format_ass_time(cutoff)
            out_lines.append(f"{_ASS_DIALOGUE_PREFIX} " + ",".join(fields))
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if out_lines else "")


# --------------------------------------------------------------------------
# ffmpeg / ffprobe
# --------------------------------------------------------------------------


def _encoder(device_type: str, settings: dict[str, Any]) -> list[str]:
    if device_type == "cuda":
        return ["-c:v", "h264_nvenc", "-preset", str(settings["nvenc_preset"]), "-cq", str(settings["crf"])]
    return ["-c:v", "libx264", "-preset", str(settings["x264_preset"]), "-crf", str(settings["crf"])]


def _run_ffmpeg(
    ffmpeg_bin: str,
    source: Path,
    filter_complex: str,
    vout_label: str,
    target_fps: float,
    device_type: str,
    settings: dict[str, Any],
    scratch_dir: Path,
    out_path: Path,
    extra_inputs: tuple[Path, ...] = (),
    seek: float = 0.0,
    seek_duration: float | None = None,
) -> None:
    """``seek``/``seek_duration`` positionnent l'entree source (-ss/-t avant
    son -i) : ffmpeg saute directement au clip au lieu de decoder la source
    depuis 0 ; les entrees supplementaires (PNG du titre) ne sont pas
    decalees."""
    source_seek = ["-ss", f"{seek:.6f}"] if seek > 0 else []
    if seek_duration is not None:
        source_seek += ["-t", f"{seek_duration:.6f}"]
    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error",
        *source_seek,
        "-i", str(source.resolve()),
        *(arg for extra in extra_inputs for arg in ("-i", str(extra.resolve()))),
        "-filter_complex", filter_complex,
        "-map", f"[{vout_label}]", "-map", "[aout]",
        "-r", str(target_fps),
        *_encoder(device_type, settings),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", str(settings["audio_bitrate"]), "-ar", "48000",
        "-movflags", "+faststart",
        "-f", "mp4",
        str(out_path.resolve()),
    ]
    _exec_ffmpeg(cmd, scratch_dir, out_path)


def _exec_ffmpeg(cmd: list[str], cwd: Path, out_path: Path) -> None:
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RenderError(f"ffmpeg introuvable ({cmd[0]})") from exc
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg a echoue pour {out_path} : {proc.stderr.decode(errors='replace').strip()}")


def thumbnail(mp4: Path, target: Path, *, config: Any = None, ffmpeg_bin: str = "ffmpeg",
              seek: float | None = None) -> Path:
    """Extrait une seule image JPEG de ``mp4`` vers ``target`` (largeur
    ``[render] thumbnail_width`` au plus, jamais agrandie). Ecrit puis remplace
    atomiquement ; ``RenderError`` si le mp4 manque ou si ffmpeg echoue.
    ``seek`` (secondes) remplace ``[render] thumbnail_seek``."""
    mp4, target = Path(mp4), Path(target)
    if not mp4.is_file():
        raise RenderError(f"clip introuvable : {mp4}")
    settings = {**CONFIG_DEFAULTS, **(config.section("render") if config is not None else {})}
    width = int(settings["thumbnail_width"])
    seek = float(settings["thumbnail_seek"]) if seek is None else float(seek)
    if width < 1 or seek < 0:
        raise RenderError(f"[render] thumbnail_width >= 1 et thumbnail_seek >= 0 exiges, recu {width} et {seek}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error",
        "-ss", f"{seek:.6f}", "-i", str(mp4.resolve()),
        "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2",
        "-q:v", "4", "-f", "mjpeg", str(tmp.resolve()),
    ]
    try:
        _exec_ffmpeg(cmd, target.parent, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


# --------------------------------------------------------------------------
# Etape
# --------------------------------------------------------------------------


def render(
    video_id: str,
    clip_id: str,
    workspace_dir: str | Path = "workspace",
    output_dir: str | Path = "output",
    *,
    config: Any = None,
    force: bool = False,
    ffmpeg_bin: str = "ffmpeg",
) -> Path:
    """Rend le clip ``clip_id`` de ``video_id`` : ecrit
    output/<video_id>/<clip_id>.mp4 et .json (SPEC-6127), renvoie le chemin
    du .mp4. Une paire deja presente n'est pas refaite, sauf ``force``."""
    video_dir = Path(workspace_dir) / video_id
    out_dir = Path(output_dir) / video_id
    mp4_out = out_dir / f"{clip_id}.mp4"
    json_out = out_dir / f"{clip_id}.json"
    if mp4_out.exists() and json_out.exists() and not force:
        return mp4_out

    settings = _settings(config)
    cta_enabled = bool(settings["cta_enabled"])
    title_enabled = bool(settings["title_enabled"])
    badge_enabled = bool(settings["badge_enabled"])
    if cta_enabled:
        # ADR-ad2e : jamais de CTA a moitie active (verification independante
        # du clip, avant toute lecture de fichier).
        if not str(settings["cta_handle"]).strip():
            raise RenderError(
                "[render] cta_enabled sans cta_handle : renseigner cta_handle ou desactiver cta_enabled"
            )
        cta_seconds_setting = float(settings["cta_seconds"])
        if cta_seconds_setting <= 0:
            raise RenderError(
                f"[render] cta_seconds doit etre > 0, recu {cta_seconds_setting}"
            )
        check_cta_handle_gap(settings)
    if badge_enabled:
        # ADR-ad2e : jamais de badge a moitie active.
        if not str(settings["badge_logo"]).strip():
            raise RenderError("[render] badge_enabled sans badge_logo")
        if not str(settings["badge_name"]).strip():
            raise RenderError("[render] badge_enabled sans badge_name")

    source = video_dir / f"{video_id}.mp4"
    if not source.exists():
        raise RenderError(f"video absente : {source}")

    captions = _read_json(video_dir / "captions.json")
    clip = _find(captions["clips"], "id", clip_id, "captions.json")

    moments = _read_json(video_dir / "moments.json")
    moment = _find(moments["moments"], "id", clip["moment_id"], "moments.json")

    transcript = _read_json(video_dir / "transcript.json")
    meta = _read_json(video_dir / "meta.json", optional=True) or {}
    source_url = meta.get("webpage_url")
    if not source_url:
        # ADR-ad2e : jamais reconstruire une URL (ce serait une URL YouTube
        # supposee pour une source qui peut etre Twitch) ; on remonte l'echec.
        raise RenderError(
            f"meta.json de {video_id} n'a pas de webpage_url : retelecharger (download --force)"
        )
    reframe_data = _read_json(video_dir / "reframe" / f"{clip_id}.json")
    ass_path = video_dir / "subtitles" / f"{clip_id}.ass"
    if not ass_path.exists():
        raise RenderError(f"sous-titres absents : {ass_path}")

    tol = float(settings["start_end_tolerance"])
    if abs(reframe_data["start"] - clip["start"]) > tol or abs(reframe_data["end"] - clip["end"]) > tol:
        raise RenderError(
            f"bornes incoherentes pour {clip_id} : captions.json [{clip['start']}, {clip['end']}] "
            f"vs reframe.json [{reframe_data['start']}, {reframe_data['end']}]"
        )
    if not reframe_data["plans"]:
        raise RenderError(f"reframe.json de {clip_id} n'a aucun plan")

    screen_title = clip.get("screen_title")
    if not isinstance(screen_title, str) or not screen_title.strip():
        raise RenderError(
            f"screen_title absent de captions.json pour {clip_id} : relancer captions --force"
        )

    layout = reframe_data.get("layout")
    letterbox = layout in _TEXT_LAYOUTS
    rects: dict[str, dict[str, int]] = {}
    zones: dict[str, Any] | None = None
    if letterbox:
        zones = reframe_data.get("text_zones")
        if not isinstance(zones, dict) or not {"subtitles", "part"} <= set(zones):
            raise RenderError(
                f"reframe/{clip_id}.json est en {layout} sans text_zones (subtitles, part) : "
                "relancer reframe --force"
            )
        if title_enabled and "title" not in zones:
            raise RenderError(
                f"reframe/{clip_id}.json ({layout}) n'a pas de zone title : [render] title_enabled=true "
                "la requiert (la desactiver, ou pour l'agencement split remonter split_webcam_dest.y pour "
                "laisser de la place au-dessus de la webcam)"
            )
        if badge_enabled and "badge" not in zones:
            raise RenderError(
                f"reframe/{clip_id}.json ({layout}) n'a pas de zone badge : [render] badge_enabled "
                "requiert l'agencement stream split (SPEC-76dc)"
            )
        # champ du sidecar -> panneau (pixels de sortie), pour la qa
        for key, name in _VIDEO_RECT_FIELDS[layout].items():
            panel = [p for p in reframe_data["plans"][0]["panels"] if p.get("name") == name]
            if not panel:
                raise RenderError(
                    f"reframe/{clip_id}.json est en {layout} sans panneau {name} : relancer reframe --force"
                )
            rects[key] = {k: int(panel[0]["dest"][k]) for k in ("x", "y", "w", "h")}
    elif badge_enabled:
        raise RenderError(
            f"reframe/{clip_id}.json ({layout}) n'a pas de zone badge : [render] badge_enabled "
            "requiert l'agencement stream split (SPEC-76dc)"
        )

    cta_applies = letterbox and cta_enabled
    if cta_applies:
        if not str(settings["cta_text"]).strip():
            raise RenderError(
                "[render] cta_enabled sans cta_text : renseigner cta_text ou desactiver cta_enabled"
            )
        cta_seconds = float(settings["cta_seconds"])
        if cta_seconds >= float(clip["duration"]):
            raise RenderError(
                f"cta_seconds ({cta_seconds}) >= duree du clip {clip_id} ({clip['duration']}) : "
                "reduire [render] cta_seconds ou desactiver cta_enabled pour ce clip"
            )
    # SPEC-76dc : le badge remplace le pseudo texte quand les deux sont
    # actifs (la carte de fin n'est pas affectee). Sans titre ni badge, il
    # n'y a pas d'ancre pour le pseudo (ADR-ad2e : jamais devinee).
    handle_shown = cta_applies and not badge_enabled
    if handle_shown and not title_enabled:
        raise RenderError(
            "[render] cta_enabled avec cta_handle mais sans titre (title_enabled=false) et sans badge "
            "(badge_enabled=false) : le pseudo de chaine n'a pas d'ancre -- activer [render] badge_enabled, "
            "[render] title_enabled, ou vider cta_handle"
        )

    scratch_dir = video_dir / "render" / clip_id
    scratch_dir.mkdir(parents=True, exist_ok=True)
    tmp_out = mp4_out.with_suffix(".mp4.tmp")
    try:
        hook_path: Path | None = None
        part_path: Path | None = None
        extra_inputs: tuple[Path, ...] = ()
        ass_for_filter = ass_path
        cta_start_rel: float | None = None
        cta_input: int | None = None
        title_input: int | None = None
        badge_input: int | None = None
        if letterbox:
            # Titre d'ecran pendant tout le clip, pas d'accroche de 2 s (SPEC-6127,
            # letterbox comme stream) -- sauf title_enabled = false (SPEC-76dc).
            if title_enabled:
                title_zone = reframe_data["text_zones"]["title"]
                if handle_shown:
                    # Le pseudo se place sous l'encadre du titre : on reserve
                    # la place en remontant le titre (title_lift effectif plus
                    # grand que celui configure), sans toucher au rendu par
                    # defaut (SPEC-6a47).
                    zone_w = title_zone["x1"] - title_zone["x0"]
                    pseudo = layout_pseudo(str(settings["cta_handle"]), zone_w, settings)
                    reserved = int(settings["cta_handle_gap"]) + pseudo.height
                    title_settings = {**settings, "title_lift": int(settings["title_lift"]) + reserved}
                else:
                    title_settings = settings
                png = scratch_dir / "title.png"
                title_layout = title_png(screen_title, title_zone, title_settings, png)
                if handle_shown:
                    _draw_pseudo(png, title_zone, title_layout, str(settings["cta_handle"]), pseudo, settings)
                extra_inputs = extra_inputs + (png,)
                title_input = len(extra_inputs)
            if badge_enabled:
                badge_out = scratch_dir / "badge.png"
                badge_png(
                    Path(str(settings["badge_logo"])), str(settings["badge_name"]),
                    reframe_data["text_zones"]["badge"], settings, badge_out,
                )
                extra_inputs = extra_inputs + (badge_out,)
                badge_input = len(extra_inputs)
            if clip["parts_total"] > 1:
                part_path = scratch_dir / "part.txt"
                part_path.write_text(f"Partie {clip['part']}", encoding="utf-8")
            if cta_applies:
                card_png = scratch_dir / "cta_card.png"
                cta_card_png(str(settings["cta_text"]), reframe_data["text_zones"]["subtitles"], settings, card_png)
                extra_inputs = extra_inputs + (card_png,)
                cta_input = len(extra_inputs)
                cutoff = float(clip["duration"]) - float(settings["cta_seconds"])
                truncated = truncate_ass_for_cta(ass_path.read_text(encoding="utf-8"), cutoff)
                ass_for_filter = scratch_dir / "subtitles_cta.ass"
                ass_for_filter.write_text(truncated, encoding="utf-8")
                cta_start_rel = cutoff
        else:
            hook_path = scratch_dir / "hook.txt"
            hook_path.write_text(clip["hook_text"], encoding="utf-8")
            if clip["parts_total"] > 1:
                part_path = scratch_dir / "part.txt"
                part_path.write_text(f"Part {clip['part']}/{clip['parts_total']}", encoding="utf-8")

        # Positionnement sur le debut du clip (ou du premier plan, s'il
        # commence un peu avant) : jamais decoder la source depuis 0.
        seek = max(0.0, min([clip["start"]] + [p["start"] for p in reframe_data["plans"]]))
        seek_end = max([clip["end"]] + [p["end"] for p in reframe_data["plans"]])
        filter_complex, vout_label = _build_filter_complex(
            reframe_data, clip["start"], clip["end"], ass_for_filter, hook_path, part_path, scratch_dir, settings,
            title_input=title_input, source_offset=seek,
            cta_input=cta_input, cta_start=cta_start_rel, badge_input=badge_input,
        )

        target_fps = float(settings["max_fps"])

        device = get_device()

        out_dir.mkdir(parents=True, exist_ok=True)
        _run_ffmpeg(
            ffmpeg_bin, source, filter_complex, vout_label, target_fps, device.type, settings, scratch_dir, tmp_out,
            extra_inputs, seek=seek, seek_duration=seek_end - seek,
        )
        tmp_out.replace(mp4_out)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
        tmp_out.unlink(missing_ok=True)  # sortie partielle d'un ffmpeg en echec (rien apres un replace)

    data = {
        "video_id": video_id,
        "source_url": source_url,
        "source_title": meta.get("title") or "",
        "clip_id": clip_id,
        "part": clip["part"],
        "parts_total": clip["parts_total"],
        "start": clip["start"],
        "end": clip["end"],
        "duration": clip["duration"],
        "language": clip["language"],
        "score": moment["final_score"],
        "scores": moment["scores"],
        "reason": moment["justification"],
        "hook_text": clip["hook_text"],
        "screen_title": screen_title,
        "title": clip["title"],
        "caption": clip["caption"],
        "hashtags": clip["hashtags"],
        "transcript": _clip_transcript(transcript, clip["start"], clip["end"]),
        "layout": reframe_data["layout"],
        "cta": cta_applies,
        # ce qui a reellement ete dessine (la qa ne relit pas la config courante)
        "title_shown": letterbox and title_enabled,
        "badge_shown": letterbox and badge_enabled,
        "cta_handle_shown": handle_shown,
        "qa": dict(_QA_DEFAULT),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    data.update(rects)  # panneaux video en pixels de sortie, pour la qa (SPEC-6127, SPEC-3a88)
    tmp_json = json_out.with_suffix(".json.tmp")
    tmp_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_json.replace(json_out)

    return mp4_out
