"""Etape subtitles : sous-titres style CapCut en .ass, karaoke mot par mot,
mots d'emphase choisis par clipper.llm (usage emphasis).

Entrees : workspace/<video_id>/transcript.json (etape transcribe) et
l'intervalle [start, end] du clip (secondes, relatif au debut de la video).
Sortie  : workspace/<video_id>/subtitles/<clip_id>.ass, timecodes relatifs
au debut du clip (start devient 0).

Cette etape ne detecte aucun visage : l'appelant (clipper.pipeline) lui
donne, d'apres le plan de recadrage, les bandes a eviter plan par plan
(``avoid_zones`` : visages, SPEC-6127) et les bandes interdites
(``reserved_zones`` : l'accroche dessinee par render), au format
``[{"start", "end", "bands": [[haut, bas], ...]}]`` (temps en secondes de la
video, bandes en fraction 0..1 de la hauteur d'image).

Chaque evenement prend sa propre position (MarginV, texte aligne en bas),
choisie parmi des hauteurs candidates de la zone sure TikTok (``safe_zone`` :
hors de l'interface masquee en haut et en bas), de bas en haut, donc d'abord
dans le tiers inferieur de la zone : la premiere qui ne recouvre aucune bande
a eviter des plans ou il s'affiche. Sans position libre, la moins recouvrante
est prise et journalisee. Une bande interdite n'est jamais recouverte ; si
elle ne laisse aucune position, c'est une erreur.

Format letterbox (SPEC-6127) : l'appelant donne a la place ``text_zone``, la
zone ``{"x0", "y0", "x1", "y1"}`` (pixels de sortie) ou poser le texte, sous
l'image. Chaque groupe est mesure avec Pillow dans la vraie police (contour
compris) : trop large, il est coupe en deux lignes, puis en groupes plus
courts, puis un mot seul est reduit par paliers ; s'il ne tient toujours pas,
c'est une erreur. Chaque ligne est un evenement Dialogue distinct (jamais de
\\N : libass avance chaque \\N de Fontsize, soit win ascent + descent, sans
interligne reglable), aligne en haut au centre (\\an8), place a
``MarginV = y0 + letterbox_offset_y + i x pas``. Le .ass letterbox commence par
``; format: letterbox``.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper import llm

PLAY_RES_X = 1080
PLAY_RES_Y = 1920

FONT_FILE = Path(__file__).resolve().parent / "assets" / "fonts" / "Poppins-ExtraBold.ttf"
LETTERBOX_HEADER = "; format: letterbox"
SPLIT_HEADER = "; format: split"
_ZONE_KEYS = ("x0", "y0", "x1", "y1")

# Couleurs "amicales" acceptees par les reglages split_* (#RRGGBB ou un de
# ces noms), converties en ASS &H00BBGGRR& (voir _ass_color) : les autres
# couleurs du module (primary_color, emphasis_color...) restent au format
# ASS natif, deja utilise tel quel dans config.toml.
_NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0), "green": (0, 128, 0),
    "blue": (0, 0, 255), "yellow": (255, 255, 0), "orange": (255, 165, 0), "purple": (128, 0, 128),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "pink": (255, 192, 203), "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
}

log = logging.getLogger(__name__)

# Debut d'un jeton colle au mot precedent par la transcription (elision,
# inversion) : " m" + "'a", " viens" + "-tu".
_GLUED_PREFIXES = ("'", "\u2019", "-")

# Ponctuation isolee (TASK-4826) : faster-whisper rend parfois une
# ponctuation francaise precedee d'une espace comme un mot separe sans
# lettre ni chiffre (" ?", " !", " :", " ;", " \u00bb", "\u2026"). Espace devant, selon
# l'usage francais ; aucune (collee directement) pour le reste, dont "\u2026".
_PUNCT_SPACE_BEFORE = {"?": " ", "!": " ", ":": " ", ";": " ", "\u00bb": " "}

# Un mot dont le debut precede le debut du clip de plus que cette tolerance
# n'est jamais sous-titre (SPEC-0eec regle 5 : une borne de clip peut arrondir
# de quelques centiemes au-dessus du mot qu'elle garde, mais un mot entier
# d'avant le clip, comme un connecteur retire, ne doit jamais s'afficher).
_WORD_START_TOLERANCE = 0.05

CONFIG_DEFAULTS: dict[str, object] = {
    "font_name": "Poppins ExtraBold",
    "font_size": 96,
    "outline": 6,
    "min_words_per_group": 2,
    "max_words_per_group": 4,
    # Zone sûre [haut, bas] (fraction de la hauteur) où placer le texte :
    # l'interface TikTok masque le haut (~15 %) et le bas (~20 %).
    "safe_zone": [0.20, 0.78],
    # Écart (px) entre deux hauteurs candidates, de bas en haut de la zone sûre.
    "position_step": 16,
    # Hauteur (px) occupée par le texte (deux lignes et contour).
    "text_band_height": 260,
    "margin_left": 60,
    # Colonne des icônes TikTok (j'aime, commentaires, partage) à droite.
    "margin_right": 150,
    "primary_color": "&H00FFFFFF&",   # blanc : mots deja prononces
    "secondary_color": "&H0080FFFF&", # jaune clair : mots pas encore prononces
    "outline_color": "&H00000000&",   # noir
    "emphasis_color": "&H0000A5FF&",  # orange : mots d'emphase
    "emphasis": True,
    # Affiche les sous-titres en majuscules en format letterbox.
    # Format letterbox (SPEC-6127, maquette validée) : texte dans la zone
    # text_zones.subtitles du plan de recadrage.
    "letterbox_uppercase": True,
    # Tailles en pixels d'em (comme Pillow) ; le .ass reçoit la taille libass
    # (em x (usWinAscent + usWinDescent) / unitsPerEm).
    "letterbox_font_size": 68,
    "letterbox_min_font_size": 40,
    "letterbox_font_step": 4,
    # Pas entre deux lignes d'un groupe, en em.
    "letterbox_line_height": 1.15,
    "letterbox_outline": 7,
    "letterbox_max_words_per_group": 8,
    # Décalage, en pixels, entre la vidéo et la première ligne de sous-titres.
    # Décalage (px) entre le haut de la zone subtitles et la première ligne
    # (TASK-ea6e : les sous-titres étaient trop collés à la vidéo sans lui).
    "letterbox_offset_y": 28,
    # Nombre de clips dont les sous-titres sont générés en même temps ; 1 = un clip après l'autre.
    # Clips générés en même temps par l'étape subtitles de clipper.pipeline
    # (TASK-ce6e : un appel LLM d'emphase par clip) ; 1 = un clip après
    # l'autre. Lu par le pipeline, jamais passé à generate.
    "parallel": 4,
    # Durée maximale, en secondes, pendant laquelle des sous-titres restent à l'écran après le dernier mot.
    # TASK-9ee7 : rien à l'écran pendant les silences. hold_s : un groupe de
    # mots reste affiché au plus ce temps après la fin de son dernier mot
    # (borne par le début du groupe suivant, si plus proche). gap_s : un
    # écart de plus que ça avant le mot suivant coupe le groupe (rien
    # n'est affiché pendant l'écart). max_word_s : la fin d'un mot isolé
    # (faster-whisper l'étire parfois sur le silence qui suit, mesure
    # jusqu'à plus de 10 s) est bornée à ce temps depuis son début.
    "hold_s": 0.3,
    "gap_s": 0.6,
    "max_word_s": 1.5,
    # Police des sous-titres de l'agencement stream split.
    # Style de l'agencement stream split (SPEC-76dc, generate(style="split")) :
    # deux couleurs seulement (texte, mot en cours), jamais d'appel LLM
    # d'emphase (chaque mot est "en cours" à son propre instant). Couleurs au
    # format amical (#RRGGBB ou un nom, voir _NAMED_COLORS), pas le format
    # ASS natif des réglages ci-dessus.
    "split_font_name": "Poppins ExtraBold",
    "split_font_size": 80,
    "split_min_font_size": 44,
    "split_font_step": 4,
    "split_uppercase": True,
    "split_text_color": "white",
    "split_current_word_color": "#9146FF",
    "split_outline_color": "black",
    "split_outline": 10,
    # Ajoute une ombre au texte des sous-titres de l'agencement stream split ; désactivé par défaut.
    # Sans ombre par défaut (style de référence sans ombre, SPEC-76dc).
    "split_shadow_enabled": False,
    "split_shadow_color": "black",
    "split_shadow_offset": [2, 2],
}

EMPHASIS_PROMPT = (
    "Voici les mots d'un extrait de sous-titres pour un clip vertical style "
    "CapCut, un mot par ligne precede de son index. Choisis les quelques "
    "mots a mettre en valeur (chiffres, mots forts, accroche) dans une "
    "couleur distincte. Ne liste que les index a mettre en valeur."
)


class SubtitlesError(Exception):
    """Transcription absente ou intervalle sans mot."""


def _emphasis_schema(n_words: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "indices": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": max(n_words - 1, 0)},
                "maxItems": n_words,
            },
        },
        "required": ["indices"],
        "additionalProperties": False,
    }


def _settings(config: Any) -> dict[str, Any]:
    if config is None:
        from clipper.config import load_config

        config = load_config()
    return {**CONFIG_DEFAULTS, **config.section("subtitles")}


def _words_in_interval(transcript: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            if w["start"] < end and w["end"] > start and w["start"] >= start - _WORD_START_TOLERANCE:
                words.append(w)
    words.sort(key=lambda w: w["start"])
    return words


def _bound_word_ends(words: list[dict[str, Any]], max_word_s: float) -> None:
    """Borne en place (meme dicts que ``_units`` mute, TASK-9ee7) la fin d'un
    mot isole dont la duree propre depasse ``max_word_s`` : faster-whisper
    etire parfois la fin d'un mot sur le silence qui suit (mesure jusqu'a
    plus de 10 s sur v2887271276), ce qui gonflerait sa duree de karaoke et
    masquerait un ecart reel au decoupage par silence (_group_words)."""
    for w in words:
        if w["end"] - w["start"] > max_word_s:
            w["end"] = w["start"] + max_word_s


def _ask_emphasis(words: list[dict[str, Any]], config: Any) -> set[int]:
    if not words:
        return set()
    lines = "\n".join(f"{i}\t{w['word'].strip()}" for i, w in enumerate(words))
    prompt = f"{EMPHASIS_PROMPT}\n\n{lines}"
    answer = llm.ask("emphasis", prompt, [], _emphasis_schema(len(words)), config=config)
    return set(answer["indices"])


def _is_isolated_punct(text: str) -> bool:
    """Vrai si ``text`` n'a ni lettre ni chiffre (une ponctuation isolee,
    TASK-4826) : ' ?', ' !', ' :', ' ;', ' »', '…'..."""
    stripped = text.strip()
    return bool(stripped) and not any(c.isalnum() for c in stripped)


def _units(words: list[dict[str, Any]], gap_s: float) -> list[list[dict[str, Any]]]:
    """Mots au sens du regroupement : un jeton qui commence par une apostrophe
    ou un trait d'union colle ("'a", "-tu") reste avec le mot precedent, de
    meme qu'une ponctuation isolee (" ?", " !", " :", " ;", " »", TASK-4826),
    qui rejoint l'unite du mot d'avant (jamais la sienne propre : elle ne
    peut donc jamais ouvrir un groupe, une ligne ni un Dialogue) et est omise
    si rien ne la precede dans le clip. Rejoindre suppose un vrai enchainement :
    si l'ecart avant le jeton depasse ``gap_s`` (faster-whisper coupe parfois
    une elision en deux jetons avec un silence au milieu, TASK-c492), le jeton
    colle reste dans sa propre unite au lieu de rejoindre celle d'avant, et la
    ponctuation isolee est omise plutot que rattachee a travers le silence.
    Mutation en place (pas de copie) : l'identite du dict reste stable pour
    l'indexation par id() de l'emphase en aval (_render_letterbox)."""
    units: list[list[dict[str, Any]]] = []
    for w in words:
        prev_end = units[-1][-1]["end"] if units else None
        reachable = prev_end is not None and w["start"] - prev_end <= gap_s
        if _is_isolated_punct(w["word"]):
            if not reachable:
                continue
            stripped = w["word"].strip()
            w["word"] = _PUNCT_SPACE_BEFORE.get(stripped, "") + stripped
            units[-1].append(w)
        elif reachable and w["word"].startswith(_GLUED_PREFIXES):
            units[-1].append(w)
        else:
            units.append([w])
    return units


def _group_words(
    words: list[dict[str, Any]], min_size: int, max_size: int, gap_s: float
) -> list[list[dict[str, Any]]]:
    """Groupe les mots par lots de ``min_size`` a ``max_size``, sans jamais
    laisser un reliquat plus petit que ``min_size`` (sauf si l'intervalle
    entier en compte moins). Un mot et ses jetons colles comptent pour un.
    D'abord coupe en segments aux ecarts de plus de ``gap_s`` entre deux mots
    consecutifs (TASK-9ee7 : un silence ne doit jamais rester a l'interieur
    d'un groupe), chaque segment ensuite regroupe par lots comme ci-dessus."""
    units = _units(words, gap_s)
    groups: list[list[dict[str, Any]]] = []

    def batch(segment: list[list[dict[str, Any]]]) -> None:
        n = len(segment)
        i = 0
        while i < n:
            remaining = n - i
            take = min(max_size, remaining)
            if 0 < remaining - take < min_size:
                take = remaining - min_size
            groups.append([w for unit in segment[i : i + take] for w in unit])
            i += take

    segment: list[list[dict[str, Any]]] = []
    prev_end: float | None = None
    for unit in units:
        if prev_end is not None and unit[0]["start"] - prev_end > gap_s:
            batch(segment)
            segment = []
        segment.append(unit)
        prev_end = unit[-1]["end"]
    batch(segment)
    return groups


def _format_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centis = round(seconds * 100)
    cs = centis % 100
    total_seconds = centis // 100
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    m = total_minutes % 60
    h = total_minutes // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _candidates(settings: dict[str, Any]) -> list[tuple[int, int]]:
    """Bandes [haut, bas] (px) ou poser le texte dans la zone sure, de bas en
    haut (le tiers inferieur de la zone d'abord)."""
    band = int(settings["text_band_height"])
    step = int(settings["position_step"])
    safe_top, safe_bottom = settings["safe_zone"]
    lowest = math.floor(float(safe_bottom) * PLAY_RES_Y)
    highest = math.ceil(float(safe_top) * PLAY_RES_Y) + band
    if lowest < highest:
        raise SubtitlesError(
            f"zone sure {settings['safe_zone']} trop petite pour {band} px de texte"
        )
    bottoms = list(range(lowest, highest - 1, -step))
    if bottoms[-1] != highest:
        bottoms.append(highest)
    return [(b - band, b) for b in bottoms]


def _bands_at(zones: list[dict[str, Any]], start: float, end: float) -> list[tuple[float, float]]:
    """Bandes (px) des zones dont l'intervalle de temps recoupe [start, end]."""
    return [
        (top * PLAY_RES_Y, bottom * PLAY_RES_Y)
        for z in zones
        if z["start"] < end and z["end"] > start
        for top, bottom in z["bands"]
    ]


def _covered(band: tuple[int, int], zones: list[tuple[float, float]]) -> float:
    """Hauteur (px) de ``band`` recouverte par l'union de ``zones``."""
    covered = 0.0
    reach = float(band[0])
    for top, bottom in sorted(zones):
        top, bottom = max(top, reach), min(bottom, band[1])
        if bottom > top:
            covered += bottom - top
            reach = bottom
    return covered


def _position(
    start: float,
    end: float,
    candidates: list[tuple[int, int]],
    avoid_zones: list[dict[str, Any]],
    reserved_zones: list[dict[str, Any]],
    where: str,
) -> tuple[int, int]:
    """Bande [haut, bas] (px) d'un evenement affiche de ``start`` a ``end``
    (secondes de la video)."""
    reserved = _bands_at(reserved_zones, start, end)
    allowed = [c for c in candidates if _covered(c, reserved) == 0]
    if not allowed:
        raise SubtitlesError(
            f"{where} {start:.2f}-{end:.2f}s : aucune position de la zone sure hors des "
            f"bandes reservees {reserved}"
        )
    faces = _bands_at(avoid_zones, start, end)
    best = min(allowed, key=lambda c: _covered(c, faces))  # le plus bas a egalite
    covered = _covered(best, faces)
    if covered > 0:
        log.warning(
            "subtitles %s %.2f-%.2fs : aucune position libre de visage, la moins "
            "recouvrante est prise (%d-%d px, %.0f px recouverts)",
            where, start, end, best[0], best[1], covered,
        )
    return best


def _ass_color(spec: str) -> str:
    """``spec`` (``#RRGGBB`` ou un nom de ``_NAMED_COLORS``) converti au
    format ASS ``&H00BBGGRR&`` (alpha opaque). Une couleur inconnue est une
    erreur explicite (ADR-ad2e), jamais une supposition silencieuse."""
    spec = spec.strip()
    if spec.startswith("#"):
        hexpart = spec[1:]
        if len(hexpart) != 6 or any(c not in "0123456789abcdefABCDEF" for c in hexpart):
            raise SubtitlesError(f"couleur hexadecimale invalide : {spec!r} (attendu #RRGGBB)")
        r, g, b = (int(hexpart[i:i + 2], 16) for i in (0, 2, 4))
    else:
        key = spec.lower()
        if key not in _NAMED_COLORS:
            raise SubtitlesError(
                f"couleur inconnue : {spec!r} (attendu #RRGGBB ou : {', '.join(sorted(_NAMED_COLORS))})"
            )
        r, g, b = _NAMED_COLORS[key]
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def _karaoke_run(word: dict[str, Any], prev_end: float, emphasized: bool, emphasis_color: str,
                 text: str | None = None, reset: str = "{\\r}") -> str:
    """Un mot en karaoke. ``text`` remplace le texte du mot (majuscules,
    espace de tete retire) ; ``reset`` suit un mot d'emphase (retour au style,
    plus les surcharges de la ligne a garder)."""
    gap_cs = round(max(0.0, word["start"] - prev_end) * 100)
    dur_cs = max(1, round((word["end"] - word["start"]) * 100))
    text = word["word"] if text is None else text
    prefix = f"{{\\k{gap_cs}}}" if gap_cs > 0 else ""
    if emphasized:
        return f"{prefix}{{\\k{dur_cs}\\c{emphasis_color}}}{text}{reset}"
    return f"{prefix}{{\\k{dur_cs}}}{text}"


def _dialogue_line(
    group: list[dict[str, Any]],
    clip_start: float,
    emphasis: set[int],
    index: dict[int, int],
    settings: dict[str, Any],
    margin_v: int,
    display_end: float,
) -> str:
    start = group[0]["start"] - clip_start
    end = display_end - clip_start
    prev_end = group[0]["start"]
    runs = []
    for w in group:
        runs.append(_karaoke_run(w, prev_end, index[id(w)] in emphasis, str(settings["emphasis_color"])))
        prev_end = w["end"]
    text = "".join(runs)
    return (
        f"Dialogue: 0,{_format_timestamp(start)},{_format_timestamp(end)},"
        f"Default,,0,0,{margin_v},,{text}"
    )


def _render_ass(
    words: list[dict[str, Any]],
    clip_start: float,
    settings: dict[str, Any],
    emphasis: set[int],
    avoid_zones: list[dict[str, Any]],
    reserved_zones: list[dict[str, Any]],
    where: str,
) -> str:
    candidates = _candidates(settings)
    groups = _group_words(words, int(settings["min_words_per_group"]), int(settings["max_words_per_group"]),
                          float(settings["gap_s"]))
    index = {id(w): i for i, w in enumerate(words)}
    hold_s = float(settings["hold_s"])

    events = []
    for i, group in enumerate(groups):
        _, bottom = _position(group[0]["start"], group[-1]["end"], candidates,
                              avoid_zones, reserved_zones, where)
        display_end = group[-1]["end"] + hold_s
        if i + 1 < len(groups):
            display_end = min(display_end, groups[i + 1][0]["start"])
        events.append(_dialogue_line(group, clip_start, emphasis, index, settings, PLAY_RES_Y - bottom, display_end))

    style = (
        "Style: Default,{font},{size},{primary},{secondary},{outline_color},&H00000000,"
        "-1,0,0,0,100,100,0,0,1,{outline},0,2,{margin_l},{margin_r},{margin_v},1"
    ).format(
        font=settings["font_name"],
        size=settings["font_size"],
        primary=settings["primary_color"],
        secondary=settings["secondary_color"],
        outline_color=settings["outline_color"],
        outline=settings["outline"],
        margin_l=settings["margin_left"],
        margin_r=settings["margin_right"],
        margin_v=PLAY_RES_Y - candidates[0][1],
    )
    return _ass_document(style, events)


def _ass_document(style: str, events: list[str], header: str = "") -> str:
    return (
        header
        + "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {PLAY_RES_X}\n"
        f"PlayResY: {PLAY_RES_Y}\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events)
        + ("\n" if events else "")
    )


# --------------------------------------------------------------------------
# Format letterbox
# --------------------------------------------------------------------------


def _check_zone(zone: Any, where: str) -> dict[str, int]:
    """Zone {"x0", "y0", "x1", "y1"} en pixels entiers de la sortie 1080x1920,
    non vide ; sinon erreur explicite."""
    if not isinstance(zone, dict) or any(k not in zone for k in _ZONE_KEYS):
        raise SubtitlesError(f"{where} : zone de sous-titres absente ou incomplete {zone!r} "
                             f"(attendu {{{', '.join(_ZONE_KEYS)}}})")
    if any(not isinstance(zone[k], int) or isinstance(zone[k], bool) for k in _ZONE_KEYS):
        raise SubtitlesError(f"{where} : zone de sous-titres non entiere {zone!r}")
    x0, y0, x1, y1 = (zone[k] for k in _ZONE_KEYS)
    if not (0 <= x0 < x1 <= PLAY_RES_X and 0 <= y0 < y1 <= PLAY_RES_Y):
        raise SubtitlesError(f"{where} : zone de sous-titres incoherente {zone!r} "
                             f"(0 <= x0 < x1 <= {PLAY_RES_X}, 0 <= y0 < y1 <= {PLAY_RES_Y})")
    return {k: zone[k] for k in _ZONE_KEYS}


@functools.lru_cache(maxsize=None)
def _font_metrics(path: str) -> tuple[int, int, int]:
    """(unitsPerEm, usWinAscent, usWinDescent) de la police : libass
    dimensionne la police (Fontsize) sur win ascent + descent."""
    from fontTools.ttLib import TTFont

    font = TTFont(path, lazy=True)
    os2 = font["OS/2"]
    return font["head"].unitsPerEm, os2.usWinAscent, os2.usWinDescent


@functools.lru_cache(maxsize=None)
def _pil_font(path: str, size: int) -> Any:
    from PIL import ImageFont

    return ImageFont.truetype(path, size)


def libass_font_size(size: int, font_file: Path = FONT_FILE) -> int:
    """Fontsize du .ass pour une taille de ``size`` pixels d'em."""
    upm, ascent, descent = _font_metrics(str(font_file))
    return round(size * (ascent + descent) / upm)


@dataclass(frozen=True)
class _Style:
    """Style d'un texte positionne (letterbox ou split, SPEC-76dc) : ce que
    ``_render_positioned`` a besoin de savoir au-dela des mots et de la
    zone. La mesure (Pillow, libass) reste toujours sur ``FONT_FILE`` (seule
    police embarquee), quel que soit ``font_name`` (nom ASS, deja le cas du
    style letterbox existant)."""

    font_name: str
    font_size: int
    min_font_size: int
    font_step: int
    line_height: float
    offset: int
    outline: int
    outline_color: str
    uppercase: bool
    max_words_per_group: int
    primary_color: str
    secondary_color: str
    back_color: str
    shadow: int
    header: str


def _letterbox_style(settings: dict[str, Any]) -> _Style:
    return _Style(
        font_name=str(settings["font_name"]),
        font_size=int(settings["letterbox_font_size"]),
        min_font_size=int(settings["letterbox_min_font_size"]),
        font_step=int(settings["letterbox_font_step"]),
        line_height=float(settings["letterbox_line_height"]),
        offset=int(settings["letterbox_offset_y"]),
        outline=int(settings["letterbox_outline"]),
        outline_color=str(settings["outline_color"]),
        uppercase=bool(settings["letterbox_uppercase"]),
        max_words_per_group=int(settings["letterbox_max_words_per_group"]),
        primary_color=str(settings["primary_color"]),
        secondary_color=str(settings["secondary_color"]),
        back_color="&H00000000",
        shadow=0,
        header=LETTERBOX_HEADER,
    )


def _split_style(settings: dict[str, Any]) -> _Style:
    back_color, shadow = "&H00000000", 0
    if bool(settings["split_shadow_enabled"]):
        offset = settings["split_shadow_offset"]
        if (
            not isinstance(offset, (list, tuple)) or len(offset) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in offset)
        ):
            raise SubtitlesError(f"[subtitles] split_shadow_offset invalide {offset!r} (attendu [x, y])")
        # ASS ne connait qu'une distance d'ombre unique (Shadow), pas un
        # decalage (x, y) independant : approximee par la moyenne des deux.
        shadow = max(0, round((abs(float(offset[0])) + abs(float(offset[1]))) / 2))
        back_color = _ass_color(str(settings["split_shadow_color"]))
    text_color = _ass_color(str(settings["split_text_color"]))
    return _Style(
        font_name=str(settings["split_font_name"]),
        font_size=int(settings["split_font_size"]),
        min_font_size=int(settings["split_min_font_size"]),
        font_step=int(settings["split_font_step"]),
        line_height=float(settings["letterbox_line_height"]),
        offset=int(settings["letterbox_offset_y"]),
        outline=int(settings["split_outline"]),
        outline_color=_ass_color(str(settings["split_outline_color"])),
        uppercase=bool(settings["split_uppercase"]),
        max_words_per_group=int(settings["letterbox_max_words_per_group"]),
        primary_color=text_color,
        secondary_color=text_color,
        back_color=back_color,
        shadow=shadow,
        header=SPLIT_HEADER,
    )


class _Box:
    """Mesure du texte dans la zone, comme libass le dessine : ligne centree
    entre x0 et x1, ligne i en haut a y0 + i x pas, ligne de base a
    round(em x usWinAscent / unitsPerEm) sous ce haut."""

    def __init__(self, zone: dict[str, int], style: _Style, where: str = ""):
        self.zone = zone
        self.outline = style.outline
        self.line_height = style.line_height
        self.offset = style.offset
        zone_h = zone["y1"] - zone["y0"]
        if not (0 <= self.offset < zone_h):
            raise SubtitlesError(
                f"{where} : letterbox_offset_y doit etre dans [0, {zone_h}[ (hauteur de la zone), "
                f"recu {self.offset}"
            )
        self.upm, self.ascent, _ = _font_metrics(str(FONT_FILE))

    def step(self, size: int) -> int:
        return round(self.line_height * size)

    def fits(self, lines: list[str], size: int) -> bool:
        font = _pil_font(str(FONT_FILE), size)
        x0, y0, x1, y1 = (self.zone[k] for k in _ZONE_KEYS)
        o = self.outline
        for i, text in enumerate(lines):
            left, top, right, bottom = font.getbbox(text, anchor="ls")
            x = x0 + (x1 - x0 - font.getlength(text)) / 2
            baseline = y0 + self.offset + i * self.step(size) + round(size * self.ascent / self.upm)
            if x + left - o < x0 or x + right + o > x1:
                return False
            if baseline + top - o < y0 or baseline + bottom + o > y1:
                return False
        return True


def _unit_text(unit: list[dict[str, Any]], upper: bool) -> str:
    text = "".join(w["word"] for w in unit)
    return text.upper() if upper else text


def _line_text(units: list[list[dict[str, Any]]], upper: bool) -> str:
    return "".join(_unit_text(u, upper) for u in units).strip()


def _layout(units: list[list[dict[str, Any]]], size: int, box: _Box,
            upper: bool) -> list[list[list[dict[str, Any]]]] | None:
    """Une ligne si elle tient, sinon la coupe en deux lignes la plus
    equilibree qui tient ; None si aucune."""
    if box.fits([_line_text(units, upper)], size):
        return [units]
    font = _pil_font(str(FONT_FILE), size)
    best, best_width = None, math.inf
    for k in range(1, len(units)):
        lines = [_line_text(units[:k], upper), _line_text(units[k:], upper)]
        width = max(font.getlength(t) for t in lines)
        if width < best_width and box.fits(lines, size):
            best, best_width = [units[:k], units[k:]], width
    return best


def _sizes_below(size: int, style: _Style) -> list[int]:
    low = style.min_font_size
    sizes = list(range(size - style.font_step, low - 1, -style.font_step))
    if low < size and (not sizes or sizes[-1] != low):
        sizes.append(low)
    return sizes


_MAX_REPEAT = 3
_REPEAT_RUN = re.compile(r"(.)\1{%d,}" % _MAX_REPEAT, re.IGNORECASE)


def _shorten_repeats(text: str) -> str:
    """Affichage seulement : une suite de plus de 3 fois la meme lettre
    (« GRRRRRR... » transcrit par Whisper pour un cri) est ramenee a 3
    (« GRRR »). Une suite de 3 ou moins reste inchangee."""
    return _REPEAT_RUN.sub(lambda m: m.group(1) * _MAX_REPEAT, text)


def _display_words(words: list[dict[str, Any]], where: str) -> list[dict[str, Any]]:
    """Copies des mots pour l'affichage, suites de lettres repetees ramenees a
    3 ; le transcript d'origine n'est jamais modifie. Journalise le nombre de
    mots raccourcis."""
    shown = [dict(w) for w in words]
    count = 0
    for w in shown:
        short = _shorten_repeats(w["word"])
        if short != w["word"]:
            w["word"] = short
            count += 1
    if count:
        log.info("%s : %d mot(s) raccourci(s) (plus de %d fois la meme lettre, ramenee a %d)",
                 where, count, _MAX_REPEAT, _MAX_REPEAT)
    return shown


def _hard_cut(units: list[list[dict[str, Any]]], box: _Box, style: _Style,
              where: str) -> list[tuple[list[list[list[dict[str, Any]]]], int]]:
    """Mot seul qui ne tient pas meme a la taille minimale : coupure dure
    entre lettres, un trait d'union en fin de chaque morceau sauf le dernier,
    chaque morceau sur sa ligne a la taille minimale, le minutage du mot
    reparti a parts egales. Erreur seulement si pas un caractere ne tient."""
    upper = style.uppercase
    size = style.min_font_size
    text = _line_text(units, upper)
    first, last = units[0][0], units[-1][-1]
    chunks: list[str] = []
    rest = text
    while rest:
        if box.fits([rest], size):
            chunks.append(rest)
            break
        n = 0
        while n < len(rest) - 1 and box.fits([rest[: n + 1] + "-"], size):
            n += 1
        if n < 1:
            raise SubtitlesError(
                f"{where} : la zone de sous-titres {box.zone} est trop etroite pour un seul "
                f"caractere de {text!r} a la taille minimale {size}"
            )
        chunks.append(rest[:n] + "-")
        rest = rest[n:]
    log.info("%s : le mot %r ne tient pas a la taille minimale %d, coupe en %d morceaux",
             where, text, size, len(chunks))
    span = (last["end"] - first["start"]) / len(chunks)
    placements = []
    for i, chunk in enumerate(chunks):
        piece = {**first, "word": chunk, "start": first["start"] + i * span,
                 "end": first["start"] + (i + 1) * span, "_orig": first}
        placements.append(([[[piece]]], size))
    return placements


def _place(units: list[list[dict[str, Any]]], size: int, box: _Box, style: _Style,
           where: str) -> list[tuple[list[list[list[dict[str, Any]]]], int]]:
    """Groupe -> [(lignes, taille em), ...] : une ou deux lignes a la taille
    donnee, sinon deux groupes plus courts, sinon (mot seul) taille reduite
    par paliers, puis coupe dure avec traits d'union (_hard_cut)."""
    upper = style.uppercase
    lines = _layout(units, size, box, upper)
    if lines:
        return [(lines, size)]
    if len(units) > 1:
        half = (len(units) + 1) // 2
        return _place(units[:half], size, box, style, where) + _place(units[half:], size, box, style, where)
    for smaller in _sizes_below(size, style):
        if box.fits([_line_text(units, upper)], smaller):
            return [([units], smaller)]
    return _hard_cut(units, box, style, where)


def _render_positioned(
    words: list[dict[str, Any]],
    clip_start: float,
    settings: dict[str, Any],
    style: _Style,
    emphasis: set[int],
    emphasis_color: str,
    zone: dict[str, int],
    where: str,
) -> str:
    """Texte positionne dans ``zone`` (letterbox ou split, SPEC-76dc) : le
    mot d'indice dans ``emphasis`` est dessine dans ``emphasis_color``
    pendant sa propre duree (letterbox : les quelques mots choisis par le
    LLM ; split : chaque mot, son propre "mot en cours")."""
    box = _Box(zone, style, where)
    size = style.font_size
    words = _display_words(words, where)
    index = {id(w): i for i, w in enumerate(words)}
    margin_r = PLAY_RES_X - zone["x1"]
    hold_s = float(settings["hold_s"])

    placements: list[tuple[list[list[list[dict[str, Any]]]], int]] = []
    gap_s = float(settings["gap_s"])
    for group in _group_words(words, int(settings["min_words_per_group"]), style.max_words_per_group, gap_s):
        placements.extend(_place(_units(group, gap_s), size, box, style, where))
    # borne par le debut du placement suivant (TASK-9ee7 : un placement, pas
    # seulement un groupe, car un groupe trop large pour la zone est lui-meme
    # decoupe en plusieurs placements a des instants differents par _place).
    starts = [lines[0][0][0]["start"] for lines, _em in placements]

    events = []
    for pi, (lines, em) in enumerate(placements):
        first = lines[0][0][0]
        last = lines[-1][-1][-1]
        display_end = last["end"] + hold_s
        if pi + 1 < len(placements):
            display_end = min(display_end, starts[pi + 1])
        start = _format_timestamp(first["start"] - clip_start)
        end = _format_timestamp(display_end - clip_start)
        fs = "" if em == size else f"\\fs{libass_font_size(em)}"
        for i, line in enumerate(lines):
            # karaoke compte depuis le debut du groupe affiche
            prev_end = first["start"]
            runs = []
            for j, w in enumerate(word for unit in line for word in unit):
                text = w["word"].upper() if style.uppercase else w["word"]
                runs.append(_karaoke_run(w, prev_end, index[id(w.get("_orig", w))] in emphasis, emphasis_color,
                                         text=text.lstrip() if j == 0 else text,
                                         reset=f"{{\\r{fs}}}"))
                prev_end = w["end"]
            # layer = rang de la ligne : libass decale un evenement qui en
            # chevauche un autre du meme layer (detection de collisions)
            events.append(
                f"Dialogue: {i},{start},{end},Default,,{zone['x0']},{margin_r},"
                f"{zone['y0'] + box.offset + i * box.step(em)},,{{\\q2\\an8{fs}}}{''.join(runs)}"
            )

    style_line = (
        "Style: Default,{font},{size},{primary},{secondary},{outline_color},{back},"
        "0,0,0,0,100,100,0,0,1,{outline},{shadow},8,{margin_l},{margin_r},{margin_v},1"
    ).format(
        font=style.font_name,
        size=libass_font_size(size),
        primary=style.primary_color,
        secondary=style.secondary_color,
        outline_color=style.outline_color,
        back=style.back_color,
        outline=box.outline,
        shadow=style.shadow,
        margin_l=zone["x0"],
        margin_r=margin_r,
        margin_v=zone["y0"],
    )
    return _ass_document(style_line, events, header=style.header + "\n")


def _render_letterbox(
    words: list[dict[str, Any]],
    clip_start: float,
    settings: dict[str, Any],
    emphasis: set[int],
    zone: dict[str, int],
    where: str,
) -> str:
    return _render_positioned(
        words, clip_start, settings, _letterbox_style(settings), emphasis, str(settings["emphasis_color"]),
        zone, where,
    )


def _render_split(
    words: list[dict[str, Any]],
    clip_start: float,
    settings: dict[str, Any],
    zone: dict[str, int],
    where: str,
) -> str:
    """Style split (SPEC-76dc) : le mot en train d'etre prononce est mis en
    valeur (``split_current_word_color``) SEULEMENT pendant sa propre duree,
    les autres restant ``split_text_color`` (avant et apres) -- contrairement
    a l'emphase letterbox (``\\c`` statique pour toute la duree d'affichage
    du groupe), il faut donc deux calques ASS par ligne : le texte de base
    (toute la duree du groupe, ``split_text_color``) et, superpose par-dessus
    en ``\\pos`` a la meme place, un evenement par mot dont le Start/End ASS
    est la propre duree du mot (``split_current_word_color``). Jamais
    d'appel LLM (pas une emphase choisie, chaque mot a son tour)."""
    style = _split_style(settings)
    current_color = _ass_color(str(settings["split_current_word_color"]))
    box = _Box(zone, style, where)
    size = style.font_size
    zx0, _zy0, zx1, _zy1 = (zone[k] for k in _ZONE_KEYS)
    zone_w = zx1 - zx0
    margin_r = PLAY_RES_X - zx1
    hold_s = float(settings["hold_s"])
    words = _display_words(words, where)

    items: list[tuple[list[list[list[dict[str, Any]]]], int]] = []
    gap_s = float(settings["gap_s"])
    for group in _group_words(words, int(settings["min_words_per_group"]), style.max_words_per_group, gap_s):
        items.extend(_place(_units(group, gap_s), size, box, style, where))
    # borne (par ligne, la granularite deja affichee ici) par le debut de la
    # ligne suivante, toutes places/groupes confondus (TASK-9ee7).
    line_starts = [lu[0][0]["start"] for lines, _em in items for lu in lines]

    events: list[str] = []
    li = 0
    for lines, em in items:
        font = _pil_font(str(FONT_FILE), em)
        fs = "" if em == size else f"\\fs{libass_font_size(em)}"
        for i, line_units in enumerate(lines):
            unit_texts = [_unit_text(u, style.uppercase) for u in line_units]
            unit_texts[0] = unit_texts[0].lstrip()
            line_text_str = "".join(unit_texts)
            line_x0 = zx0 + (zone_w - font.getlength(line_text_str)) / 2
            top_y = zone["y0"] + box.offset + i * box.step(em)
            display_end = line_units[-1][-1]["end"] + hold_s
            if li + 1 < len(line_starts):
                display_end = min(display_end, line_starts[li + 1])
            g_start = _format_timestamp(line_units[0][0]["start"] - clip_start)
            g_end = _format_timestamp(display_end - clip_start)
            li += 1
            events.append(
                f"Dialogue: {2 * i},{g_start},{g_end},Default,,{zx0},{margin_r},{top_y},,"
                f"{{\\q2\\an7\\pos({line_x0:.2f},{top_y}){fs}}}{line_text_str}"
            )
            cursor = 0.0
            for unit, utext in zip(line_units, unit_texts):
                # le calque de surbrillance est un evenement ASS a part
                # (Text) pour chaque mot : un espace de tete y serait
                # rogne par libass (contrairement a la ligne de base, un
                # seul champ Text continu) -- avance separement, dessine
                # seulement le texte visible.
                stripped = utext.lstrip()
                cursor += font.getlength(utext) - font.getlength(stripped)
                u_start = _format_timestamp(unit[0]["start"] - clip_start)
                u_end = _format_timestamp(unit[-1]["end"] - clip_start)
                events.append(
                    f"Dialogue: {2 * i + 1},{u_start},{u_end},Default,,{zx0},{margin_r},{top_y},,"
                    f"{{\\q2\\an7\\pos({line_x0 + cursor:.2f},{top_y}){fs}\\c{current_color}}}{stripped}"
                )
                cursor += font.getlength(stripped)

    style_line = (
        "Style: Default,{font},{size},{primary},{secondary},{outline_color},{back},"
        "0,0,0,0,100,100,0,0,1,{outline},{shadow},7,{margin_l},{margin_r},{margin_v},1"
    ).format(
        font=style.font_name,
        size=libass_font_size(size),
        primary=style.primary_color,
        secondary=style.secondary_color,
        outline_color=style.outline_color,
        back=style.back_color,
        outline=box.outline,
        shadow=style.shadow,
        margin_l=zx0,
        margin_r=margin_r,
        margin_v=zone["y0"],
    )
    return _ass_document(style_line, events, header=SPLIT_HEADER + "\n")


# Aperçu du style (TASK-dd3f, SPEC-c100 E5) : meme mise en page que
# generate (_group_words / _place / _Box : memes tailles, memes coupures de
# ligne), dessinee avec Pillow sur un fond neutre au lieu d'un .ass, donc
# sans ffmpeg ni modele. Zone d'exemple : bande du bas d'un clip letterbox.
_PREVIEW_ZONE = {"x0": 60, "y0": 1340, "x1": PLAY_RES_X - 60, "y1": 1620}
_PREVIEW_BACKGROUND = (58, 60, 66)
_PREVIEW_WORD_S = 0.4


def _rgb_of_ass(color: str) -> tuple[int, int, int]:
    """Couleur ASS ``&H[AA]BBGGRR&`` -> (R, V, B) ; autre forme = erreur explicite."""
    spec = color.strip()
    digits = spec[2:-1] if spec.upper().startswith("&H") and spec.endswith("&") else ""
    if len(digits) not in (6, 8) or any(c not in "0123456789abcdefABCDEF" for c in digits):
        raise SubtitlesError(f"couleur ASS invalide : {color!r} (attendu &H00BBGGRR&)")
    b, g, r = (int(digits[-6:][i:i + 2], 16) for i in (0, 2, 4))
    return r, g, b


def _preview_words(text: str) -> list[dict[str, Any]]:
    tokens = text.split()
    if not tokens:
        raise SubtitlesError("texte d'apercu vide : saisir une phrase d'exemple")
    return [{"word": token if i == 0 else f" {token}", "start": i * _PREVIEW_WORD_S,
             "end": (i + 1) * _PREVIEW_WORD_S, "probability": 1.0} for i, token in enumerate(tokens)]


_PREVIEW_NUMBERS = {
    "letterbox": ("letterbox_font_size", "letterbox_min_font_size", "letterbox_font_step", "letterbox_line_height",
                  "letterbox_outline", "letterbox_max_words_per_group", "letterbox_offset_y"),
    "split": ("split_font_size", "split_min_font_size", "split_font_step", "letterbox_line_height",
              "split_outline", "letterbox_max_words_per_group", "letterbox_offset_y"),
}


def _check_preview_numbers(settings: dict[str, Any], style: str) -> None:
    for key in ("min_words_per_group", "gap_s", *_PREVIEW_NUMBERS[style]):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SubtitlesError(f"[subtitles] {key} doit etre un nombre, recu {value!r}")


def _preview_style(settings: dict[str, Any], style: str) -> tuple[_Style, tuple[int, int, int], tuple[int, int, int] | None]:
    """(style, couleur du mot mis en valeur, couleur d'ombre ou None)."""
    if style == "split":
        built = _split_style(settings)
        highlight = _rgb_of_ass(_ass_color(str(settings["split_current_word_color"])))
        shadow = _rgb_of_ass(built.back_color) if built.shadow else None
        return built, highlight, shadow
    built = _letterbox_style(settings)
    highlight = _rgb_of_ass(str(settings["emphasis_color"])) if settings["emphasis"] else None
    return built, highlight, None


def render_preview(
    config_section: dict[str, Any], text: str, size: tuple[int, int] = (PLAY_RES_X, PLAY_RES_Y), *,
    style: str = "letterbox",
) -> bytes:
    """PNG (bytes) de ``text`` dans le style effectif de ``config_section``
    (table [subtitles], CONFIG_DEFAULTS pour le reste) : police, couleurs,
    contour, ombre ; le mot en valeur (emphase letterbox, mot courant du
    style split) est dessine dans sa couleur. Ni ffmpeg ni modele ni LLM. Un
    reglage invalide est une SubtitlesError explicite (ADR-ad2e)."""
    import io

    from PIL import Image, ImageDraw

    if style not in _STYLES:
        raise SubtitlesError(f"style de sous-titres inconnu {style!r} (attendu : {' | '.join(_STYLES)})")
    settings = {**CONFIG_DEFAULTS, **config_section}
    words = _preview_words(text)
    _check_preview_numbers(settings, style)
    try:
        built, highlight, shadow = _preview_style(settings, style)
        text_rgb = _rgb_of_ass(built.primary_color)
        outline_rgb = _rgb_of_ass(built.outline_color)
        gap_s = float(settings["gap_s"])
        group = _group_words(words, int(settings["min_words_per_group"]), built.max_words_per_group, gap_s)[0]
        lines, em = _place(_units(group, gap_s), built.font_size, _Box(_PREVIEW_ZONE, built, "apercu"), built, "apercu")[0]
    except (TypeError, ValueError, KeyError) as exc:
        raise SubtitlesError(f"reglage de sous-titres invalide : {exc!r}") from exc
    except SubtitlesError:
        raise

    units = [u for line in lines for u in line]
    if highlight is None:
        marked = -1
    elif style == "split":
        marked = min(1, len(units) - 1)
    else:
        marked = max(range(len(units)), key=lambda i: len(_unit_text(units[i], built.uppercase).strip()))

    img = Image.new("RGB", (PLAY_RES_X, PLAY_RES_Y), _PREVIEW_BACKGROUND)
    draw = ImageDraw.Draw(img)
    font = _pil_font(str(FONT_FILE), em)
    box = _Box(_PREVIEW_ZONE, built, "apercu")
    zone = _PREVIEW_ZONE
    index = 0
    for i, line in enumerate(lines):
        texts = [_unit_text(u, built.uppercase) for u in line]
        texts[0] = texts[0].lstrip()
        x = zone["x0"] + (zone["x1"] - zone["x0"] - font.getlength("".join(texts))) / 2
        baseline = zone["y0"] + box.offset + i * box.step(em) + round(em * box.ascent / box.upm)
        for part in texts:
            fill = highlight if index == marked else text_rgb
            if shadow is not None:
                dx, dy = (float(v) for v in settings["split_shadow_offset"])
                draw.text((x + dx, baseline + dy), part, font=font, fill=shadow, anchor="ls",
                          stroke_width=built.outline, stroke_fill=shadow)
            draw.text((x, baseline), part, font=font, fill=fill, anchor="ls",
                      stroke_width=built.outline, stroke_fill=outline_rgb)
            x += font.getlength(part)
            index += 1
    if tuple(size) != (PLAY_RES_X, PLAY_RES_Y):
        img = img.resize((int(size[0]), int(size[1])), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _file_style(path: Path) -> str:
    """Format d'un .ass deja ecrit : "letterbox", "split" (SPEC-76dc) ou
    "recadre" (sans en-tete, format karaoke recadre)."""
    with path.open(encoding="utf-8") as f:
        first = f.readline().rstrip("\r\n")
    if first == LETTERBOX_HEADER:
        return "letterbox"
    if first == SPLIT_HEADER:
        return "split"
    return "recadre"


_STYLES = ("letterbox", "split")


def generate(
    video_id: str,
    clip_id: str,
    start: float,
    end: float,
    workspace_dir: str | Path = "workspace",
    *,
    config: Any = None,
    force: bool = False,
    avoid_zones: list[dict[str, Any]] | None = None,
    reserved_zones: list[dict[str, Any]] | None = None,
    text_zone: dict[str, int] | None = None,
    style: str = "letterbox",
) -> Path:
    """Genere workspace/<video_id>/subtitles/<clip_id>.ass pour [start, end]
    et renvoie ce chemin. Un .ass deja present n'est pas refait (ADR-b16b),
    sauf ``force`` ; s'il est d'un autre format que celui demande, c'est une
    erreur. ``avoid_zones`` (visages) et ``reserved_zones`` (accroche), ou
    ``text_zone`` (format letterbox ou split selon ``style``, SPEC-76dc) :
    voir la docstring du module. ``style`` n'a d'effet qu'avec ``text_zone`` ;
    le style split ne fait jamais appel a un LLM (chaque mot est "en cours"
    a son propre instant, pas une emphase choisie)."""
    where = f"{video_id}/{clip_id}"
    if style not in _STYLES:
        raise SubtitlesError(f"{where} : style de sous-titres inconnu {style!r} (attendu : {' | '.join(_STYLES)})")
    letterbox = text_zone is not None
    if letterbox:
        if avoid_zones or reserved_zones:
            raise SubtitlesError(f"{where} : text_zone (letterbox) exclut avoid_zones et reserved_zones")
        text_zone = _check_zone(text_zone, where)

    video_dir = Path(workspace_dir) / video_id
    out = video_dir / "subtitles" / f"{clip_id}.ass"
    wanted_style = style if letterbox else "recadre"
    if out.exists() and not force:
        found_style = _file_style(out)
        if found_style != wanted_style:
            raise SubtitlesError(
                f"{out} est au format {found_style}, format {wanted_style} demande : relancer avec --force"
            )
        return out

    transcript_file = video_dir / "transcript.json"
    if not transcript_file.exists():
        raise SubtitlesError(f"transcript.json absent : {transcript_file}")
    transcript = json.loads(transcript_file.read_text(encoding="utf-8"))

    settings = _settings(config)
    words = _words_in_interval(transcript, start, end)
    _bound_word_ends(words, float(settings["max_word_s"]))

    if letterbox and style == "split":
        ass_text = _render_split(words, start, settings, text_zone, where)
    else:
        emphasis = _ask_emphasis(words, config) if settings["emphasis"] else set()
        if letterbox:
            ass_text = _render_letterbox(words, start, settings, emphasis, text_zone, where)
        else:
            ass_text = _render_ass(words, start, settings, emphasis, avoid_zones or [],
                                   reserved_zones or [], where)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".ass.tmp")
    tmp.write_text(ass_text, encoding="utf-8")
    tmp.replace(out)
    return out
