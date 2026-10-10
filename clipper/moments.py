"""Etape moments : choix des meilleurs moments d'une video longue par
clipper.llm (usage ``moments``), selon la grille de SPEC-0eec (rubric.toml).

Entrees (workspace/<video_id>/) :
- meta.json (download) : titre, chapitres, heatmap, segments SponsorBlock ;
- transcript.json (transcribe) : segments et mots horodates ;
- audio.json (audio) : pics d'energie ;
- vision.json (vision), facultatif :
  {"frames": [{"timecode", "description", "striking": bool}]} ;
- ``examples`` : decisions humaines journalisees (feedback.examples), passees
  par clipper.pipeline, une etape n'important jamais une autre (ADR-b16b).

Sortie : workspace/<video_id>/moments.json

    {"video_id", "rubric": {"path", "weights", "min_score"}, "chunked",
     "selection": "single" | "jury",
     "jury": {"judges", "seed", "threshold", "debate_confidence_below",
              "min_confidence_weight", "quorum", "failed", "debated"},      # jury
     "exploration": {"share", "seed", "target", "chosen"},   # jury, part > 0
     "moments": [{"id", "start", "end", "duration", "format", "parts",
                  "scores", "bonus", "final_score", "justification",
                  "hook_text", "jury", "exploration"}],       # jury : si jury ;
                                                             # exploration : true
     "rejected": [{"start", "end", "reason", ...}],
     "rescored": {"source", "changed": [{"id", "start", "end", "hook_text",
                                          "before", "after"}]}}   # re-notation

Selection par le jury (ADR-ff87), en mode auto ou si [moments] selection =
"jury" : l'appel ``moments`` ne sert qu'a proposer une liste large de
candidats ; clipper.jury les note ensuite sur la meme grille, et ses notes
agregees (``scores``) remplacent celles du proposeur dans le score final
(bonus, ``min_score`` et non-chevauchement inchanges). ``jury`` de chaque
candidat garde les notes et la justification du proposeur, le score du jury,
sa confiance agregee (mediane des confiances finales des juges, SPEC-73d0),
son veto et sa trace (tours avec la confiance de chaque juge, revisions,
dissidences). Un veto rejette le
candidat avec sa raison, sans score final. Juge invalide : l'erreur remonte,
rien n'est ecrit (ADR-ad2e).

Exploration (ADR-1cf0, point 4), en selection par jury : en plus des retenus,
``exploration_share`` x leur nombre (arrondi au plus proche) clips sont pris
parmi les candidats notes non retenus ou le jury hesite le plus, pour
apprendre ce qu'il sous-estime. Dispersion d'un candidat = ecart entre le
score le plus haut et le plus bas des juges, chacun a son dernier tour
(``jury.trace.rounds``) ; egalites departagees par un tirage a graine fixe
(``exploration_seed``). Jamais un candidat vete ni rejete par la grille
(SponsorBlock, duree), jamais un chevauchement avec un retenu ou un autre
clip d'exploration. Ces clips portent ``exploration: true`` ; le bloc
``exploration`` dit combien etaient vises (``target``) et pris (``chosen``).
Part 0 : aucune exploration, sortie inchangee.

Candidats d'action (SPEC-b0f3 R10-R14) : avec [moments] candidates =
"transcript+action", chaque passage de action.json devient un candidat de
``source`` "action" (bornes recalees a moins de ``action_snap_seconds`` sur une
frontiere de phrase), note par le meme jury ou, en selection single, par un
appel de comparaison de plus ; moments.json porte alors ``source`` et le bloc
``action`` de chaque moment et rejet note.

Re-notation : si moments.json existe et que vision.json est plus recent,
l'etape (sans ``force``) ne rappelle pas le LLM ; elle recalcule le bonus
visuel, le score final, ``min_score`` et le non-chevauchement sur les
candidats deja notes, et liste dans ``rescored.changed`` ceux dont le score
ou le sort (retenu ou non) a change.

Le LLM ne fait que proposer des bornes et noter chaque critere de 0 a 10 ;
tout le reste est fait ici, de facon verifiable :
1. bornes recalees sur les frontieres de phrase les plus proches (debut du
   premier mot, fin du dernier : ni mot coupe ni silence en bord) ; si la
   premiere phrase s'ouvre sur des connecteurs qui supposent la phrase
   d'avant (``leading_connectors`` : donc, mais, du coup...), ils sont
   retires : le clip commence au mot qui suit et ``hook_text`` sans eux.
   Phrase reduite a ses connecteurs, ou duree hors bornes apres retrait :
   rejet motive. Fait avant le jury, qui note donc les bornes finales ;
2. rejet des moments qui chevauchent un segment SponsorBlock exclu ou dont
   la duree sort des bornes de la grille (regle 3) : single de
   ``single_min`` a ``single_max`` s, multipart (passage publie en serie par
   l'etape parts) de ``min_parts`` x ``part_min`` a ``max_parts`` x
   ``part_max`` s, a ``tolerance`` pres ;
3. score final = moyenne ponderee des notes x10 + bonus plafonne des signaux
   mesures (most replayed, pics audio, images marquantes) ;
4. rejet sous ``min_score``, puis non-chevauchement (regle 4) : un passage
   multipart qui atteint ``min_score`` passe avant tout clip single qui le
   chevauche, meme mieux note (le contenu du single y figure deja) ; entre
   deux candidats du meme format, le mieux note reste. Puis plafond souple
   (``max_moments_per_hour``, regle 4) : au plus ``ceil(max_moments_per_hour
   x duree de la video en heures)`` moments, jamais moins que
   ``min_moments_cap`` (plancher pour les videos courtes ; duree lue dans
   meta.json, jamais de valeur par defaut silencieuse), pris par score
   decroissant ; un moment a ``always_keep_score`` ou plus est retenu meme
   au-dela du plafond, et compte dedans. Chaque moment ecarte par le plafond
   est motive dans ``rejected``.

Transcription trop longue pour un appel (``max_transcript_chars``) : tranches
avec recouvrement, puis un tour de comparaison final qui re-note ensemble
tous les candidats, pour que les notes de tranches differentes soient
comparables (sauf avec le jury, qui note deja tous les candidats ensemble).
Reponse invalide ou Claude indisponible : l'erreur remonte, rien
n'est ecrit (ADR-ad2e).
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import math
import random
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper import jury, llm

log = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    # Qui note les moments : "single" (le proposeur seul) ou "jury".
    # Qui note les candidats du proposeur : "single" (le proposeur seul) ou
    # "jury" (clipper.jury, ADR-ff87). En mode auto, le jury note toujours.
    "selection": "single",
    # Grille de notation des moments : "builtin" (standard), "builtin:gaming" (gaming) ou un chemin de fichier.
    # Grille de notation (SPEC-0eec), relative au dossier courant. "builtin"
    # : grille standard embarquée dans le paquet (clipper/assets/rubric.toml),
    # "builtin:gaming" : grille gaming embarquée (rubric-gaming.toml, SPEC-9216 ;
    # "builtin:gaming-action" : grille d'action avec seuil éliminatoire [gate]
    # (rubric-gaming-action.toml, SPEC-b0f3 ;
    # "builtin:gaming-v2" : grille gaming v2 de l'experience A/B du jury
    # (rubric-gaming-v2.toml, avec le signal mesure speech_density ;
    # un "builtin:<nom>" inconnu est une erreur), utile
    # sans fichier local (ex. juste après installation de la wheel, avant
    # 'clipper init'). Toute autre valeur est un chemin utilisé tel quel ;
    # fichier absent = erreur explicite (voir resolve_rubric_path), jamais de
    # repli silencieux (ADR-ad2e).
    "rubric_path": "rubric.toml",
    # Au-dela, la transcription part en tranches (environ 4 caractères par
    # token : 400 000 caractères ~ 100k tokens).
    "max_transcript_chars": 400_000,
    "chunk_chars": 250_000,
    # Recouvrement entre deux tranches : une histoire à cheval reste entière
    # dans au moins une tranche.
    "chunk_overlap_seconds": 300,
    # Pics audio envoyés au LLM (les plus forts).
    "max_audio_peaks": 200,
    # Part des clips retenus choisie en exploration parmi les moments où le jury hésite ; 0 = aucune.
    # Sélection par jury : part des clips retenus ajoutée en exploration,
    # prise parmi les candidats où le jury hésite (ADR-1cf0). 0 : aucune.
    "exploration_share": 0.1,
    # Graine du tirage qui départage les candidats de même dispersion.
    "exploration_seed": 0,
    # Connecteurs de tête qui supposent la phrase d'avant : en tête de la
    # première phrase d'un candidat, ils sont retirés et le clip commence au
    # mot qui suit (casse et ponctuation ignorées ; locutions de plusieurs
    # mots comprises). [] : règle désactivée.
    "leading_connectors": [
        "donc", "mais", "et", "alors", "du coup", "en fait", "parce que", "sauf que",
        "par contre", "puis", "ensuite", "pourtant", "sinon", "car", "en plus",
        "d'ailleurs", "bon ben", "c'est pour ça que",
    ],
    # Clips courts : les clips durent entre short_min et short_max secondes et démarrent sur le moment fort ; choix possible vidéo par vidéo.
    "short_clips": False,
    # Durée minimale d'un clip court, en secondes.
    "short_min": 20,
    # Durée maximale d'un clip court, en secondes.
    "short_max": 45,
    # Candidats des moments : "transcript" (la transcription seule) ou "transcript+action" (avec les passages d'action de la vidéo).
    "candidates": "transcript",
    # Distance maximale en secondes pour caler un passage d'action sur le début ou la fin d'une phrase.
    "action_snap_seconds": 3,
    # Parole d'un passage d'action mesurée sur les mots horodatés : un mot sans
    # espace de plus de ce nombre de caractères est une hallucination de
    # whisper (musique) et est ignoré.
    "action_word_max_chars": 40,
    # Un passage qui contient de la parole retenue est rejeté si le premier mot
    # retenu arrive plus de ce nombre de secondes après son début.
    "action_max_silent_start_s": 5,
}

CANDIDATES = ("transcript", "transcript+action")
FORMATS = ("single", "multipart")
SELECTIONS = ("single", "jury")
_SENTENCE_END = (".", "!", "?", "…")
_EXAMPLE_TEXT_CHARS = 600


class MomentsError(Exception):
    """Entree manquante ou grille (rubric.toml) invalide."""


# --------------------------------------------------------------------------
# Grille
# --------------------------------------------------------------------------

_DURATION_KEYS = ("single_min", "single_max", "part_min", "part_max", "min_parts", "max_parts", "tolerance")
_BONUS_KEYS = ("max_total", "replayed", "audio_peaks", "audio_peaks_full", "visual")
# Signal mesure speech_density : facultatif, mais les trois cles vont ensemble ;
# absent, il est desactive et le bonus garde sa forme d'avant.
_SPEECH_KEYS = ("speech_density_min_wps", "speech_density_max_first_word_s", "speech_density_malus")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# Grilles embarquees (SPEC-9216 R1) : valeur de [moments] rubric_path -> fichier
# de clipper/assets. Toute autre valeur est un chemin de fichier.
_BUILTIN_RUBRICS = {
    "builtin": "rubric.toml",
    "builtin:gaming": "rubric-gaming.toml",
    "builtin:gaming-action": "rubric-gaming-action.toml",
    "builtin:gaming-v2": "rubric-gaming-v2.toml",
}


def resolve_rubric_path(value: str) -> Path:
    """Resout [moments] rubric_path : "builtin" -> grille standard embarquee
    (clipper/assets/rubric.toml), "builtin:gaming" -> grille gaming
    (clipper/assets/rubric-gaming.toml) ; un "builtin:<nom>" inconnu est une
    MomentsError qui liste les grilles disponibles, jamais un repli sur la
    grille standard (ADR-ad2e). Toute autre valeur est un chemin utilise tel
    quel, relatif au dossier courant si non absolu ; un fichier absent est une
    MomentsError explicite (load_rubric)."""
    if value in _BUILTIN_RUBRICS:
        return importlib.resources.files("clipper").joinpath("assets", _BUILTIN_RUBRICS[value])
    if value.startswith("builtin:"):
        available = ", ".join(f'"{name}"' for name in _BUILTIN_RUBRICS)
        raise MomentsError(f'grille embarquee inconnue : "{value}" (grilles disponibles : {available})')
    return Path(value)


def load_rubric(path: str | Path) -> dict[str, Any]:
    """Lit et valide rubric.toml ; toute cle manquante ou mal typee est une
    MomentsError qui la nomme."""
    path = Path(path)
    if not path.exists():
        raise MomentsError(f"grille introuvable : {path}")
    try:
        with path.open("rb") as f:
            rubric = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise MomentsError(f"{path} : TOML invalide : {exc}") from exc

    criteria = rubric.get("criteria")
    if not isinstance(criteria, dict) or not criteria:
        raise MomentsError(f"{path} : aucune table [criteria.<nom>]")
    for name, c in criteria.items():
        if not isinstance(c, dict) or not _number(c.get("weight")) or c["weight"] < 0:
            raise MomentsError(f"{path} : [criteria.{name}] weight manquant ou invalide")
        if not isinstance(c.get("question"), str) or not c["question"].strip():
            raise MomentsError(f"{path} : [criteria.{name}] question manquante")
    if sum(c["weight"] for c in criteria.values()) <= 0:
        raise MomentsError(f"{path} : la somme des poids doit etre positive")
    if not _number(rubric.get("min_score")):
        raise MomentsError(f"{path} : min_score manquant ou invalide")
    for key in ("max_moments_per_hour", "always_keep_score"):
        value = rubric.get(key)
        if not _number(value) or value <= 0:
            raise MomentsError(f"{path} : {key} manquant ou invalide (attendu : nombre > 0)")
    cap_floor = rubric.get("min_moments_cap")
    if not isinstance(cap_floor, int) or isinstance(cap_floor, bool) or cap_floor < 1:
        raise MomentsError(f"{path} : min_moments_cap manquant ou invalide (attendu : entier >= 1)")
    keywords = rubric.get("trend_keywords")
    if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
        raise MomentsError(f"{path} : trend_keywords doit etre une liste de chaines")
    for table, keys in (("durations", _DURATION_KEYS), ("bonus", _BONUS_KEYS)):
        values = rubric.get(table)
        if not isinstance(values, dict):
            raise MomentsError(f"{path} : table [{table}] manquante")
        for key in keys:
            if not _number(values.get(key)):
                raise MomentsError(f"{path} : [{table}] {key} manquant ou invalide")
    present = [k for k in _SPEECH_KEYS if k in rubric["bonus"]]
    if present:
        for key in _SPEECH_KEYS:
            value = rubric["bonus"].get(key)
            if not _number(value) or value < 0:
                raise MomentsError(
                    f"{path} : [bonus] {key} manquant ou invalide (speech_density : les trois cles "
                    f"{', '.join(_SPEECH_KEYS)} vont ensemble, nombres >= 0)"
                )
    categories = rubric.get("exclusions", {}).get("sponsorblock_categories")
    if not isinstance(categories, list):
        raise MomentsError(f"{path} : [exclusions] sponsorblock_categories manquant")
    if "gate" in rubric:
        _check_gate(path, rubric["gate"], criteria)
    return rubric


def _gate_note(path: Path, gate: dict[str, Any], key: str) -> None:
    value = gate[key]
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10:
        raise MomentsError(f"{path} : [gate] {key} invalide (attendu : entier de 0 a 10)")


def _check_gate(path: Path, gate: Any, criteria: dict[str, Any]) -> None:
    """Valide la table optionnelle [gate] (SPEC-b0f3 R3) : ``criterion`` (et
    ``unless_criterion``) nomme un critere de [criteria], ``min`` (et
    ``unless_min``) est un entier de 0 a 10, et les deux ``unless_*`` vont
    ensemble."""
    if not isinstance(gate, dict):
        raise MomentsError(f"{path} : [gate] doit etre une table")
    if gate.get("criterion") not in criteria:
        raise MomentsError(f"{path} : [gate] criterion manquant ou absent de [criteria] (criteres : {', '.join(criteria)})")
    if "min" not in gate:
        raise MomentsError(f"{path} : [gate] min manquant")
    _gate_note(path, gate, "min")
    if ("unless_criterion" in gate) != ("unless_min" in gate):
        missing = "unless_min" if "unless_criterion" in gate else "unless_criterion"
        raise MomentsError(f"{path} : [gate] {missing} manquant (unless_criterion et unless_min vont ensemble)")
    if "unless_criterion" in gate:
        if gate["unless_criterion"] not in criteria:
            raise MomentsError(
                f"{path} : [gate] unless_criterion absent de [criteria] (criteres : {', '.join(criteria)})"
            )
        _gate_note(path, gate, "unless_min")


# --------------------------------------------------------------------------
# Phrases et timecodes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Sentence:
    start: float
    end: float
    text: str
    # (debut, texte) de chaque mot ; vide si whisper n'a pas horodate les mots.
    words: tuple[tuple[float, str], ...] = ()


def _floor1(x: float) -> float:
    return math.floor(x * 10 + 1e-6) / 10


def _ceil1(x: float) -> float:
    return math.ceil(x * 10 - 1e-6) / 10


def _round2(x: float) -> float:
    """Borne publiee (SPEC-0eec regle 5) : centieme superieur, jamais en
    dessous de ``x`` (un connecteur retire ne recule jamais dedans)."""
    return math.ceil(x * 100 - 1e-6) / 100


def _span(start: float, end: float) -> str:
    return f"{_floor1(start):.1f}-{_ceil1(end):.1f}"


def _ends_sentence(word: str) -> bool:
    return word.strip().rstrip("\"'»)]").endswith(_SENTENCE_END)


def split_sentences(transcript: dict[str, Any]) -> list[Sentence]:
    """Phrases de la transcription : un mot finissant par . ! ? ou … ferme
    une phrase, une fin de segment aussi (pause detectee par whisper)."""
    out: list[Sentence] = []

    def flush(words: list[dict[str, Any]]) -> None:
        text = "".join(w["word"] for w in words).strip()
        if text:
            out.append(Sentence(words[0]["start"], words[-1]["end"], text, tuple((w["start"], w["word"]) for w in words)))

    for seg in transcript.get("segments", []):
        words = seg.get("words") or []
        if not words:
            if seg.get("text", "").strip():
                out.append(Sentence(seg["start"], seg["end"], seg["text"].strip()))
            continue
        current: list[dict[str, Any]] = []
        for word in words:
            current.append(word)
            if _ends_sentence(word["word"]):
                flush(current)
                current = []
        if current:
            flush(current)
    return out


def _token(word: str) -> str:
    """Un mot compare aux connecteurs : minuscules, apostrophe droite, sans
    ponctuation autour."""
    return re.sub(r"^[\W_]+|[\W_]+$", "", word.casefold().replace("’", "'"))


Connectors = list[tuple[str, tuple[str, ...]]]


def _connectors(settings: dict[str, Any]) -> Connectors:
    """(connecteur, ses mots) de ``leading_connectors``, locutions les plus
    longues d'abord ; un reglage invalide est une MomentsError."""
    value = settings["leading_connectors"]
    if not isinstance(value, list) or not all(isinstance(c, str) and c.split() for c in value):
        raise MomentsError(
            f"[moments] leading_connectors invalide : {value!r} (attendu : liste de connecteurs non vides)"
        )
    words = {tuple(_token(w) for w in c.split()) for c in value}
    return sorted(((" ".join(w), w) for w in words), key=lambda c: (-len(c[1]), c[0]))


def _leading_connectors(words: list[str], connectors: Connectors) -> tuple[list[str], int]:
    """(connecteurs de tete trouves, indice du premier mot qui les suit) ;
    la ponctuation seule entre eux est sautee. ([], 0) sans connecteur."""
    tokens = [_token(w) for w in words]
    found: list[str] = []
    i = 0
    while True:
        k = next((k for k in range(i, len(tokens)) if tokens[k]), len(tokens))
        match = next((c for c in connectors if tuple(tokens[k : k + len(c[1])]) == c[1]), None)
        if match is None:
            return found, (k if found else 0)
        found.append(match[0])
        i = k + len(match[1])


def _line(s: Sentence) -> str:
    return f"[{_span(s.start, s.end)}] {s.text}"


def _chunks(sents: list[Sentence], chunk_chars: int, overlap: float) -> list[list[Sentence]]:
    """Tranches d'environ ``chunk_chars`` caracteres ; chaque tranche reprend
    les ``overlap`` dernieres secondes de la precedente."""
    chunks: list[list[Sentence]] = []
    first = 0
    while first < len(sents):
        size, last = 0, first
        while last < len(sents) and (last == first or size + len(_line(sents[last])) + 1 <= chunk_chars):
            size += len(_line(sents[last])) + 1
            last += 1
        chunks.append(sents[first:last])
        if last >= len(sents):
            break
        cut = sents[last - 1].end - overlap
        nxt = next((k for k in range(first + 1, last) if sents[k].start >= cut), last)
        first = max(nxt, first + 1)
    return chunks


# --------------------------------------------------------------------------
# Prompt et schema de reponse
# --------------------------------------------------------------------------


def _scores_schema(rubric: dict[str, Any]) -> dict[str, Any]:
    criteria = rubric["criteria"]
    return {
        "type": "object",
        "description": "Note entiere de 0 a 10 par critere de la grille.",
        "properties": {
            name: {"type": "integer", "minimum": 0, "maximum": 10, "description": c["question"]}
            for name, c in criteria.items()
        },
        "required": list(criteria),
        "additionalProperties": False,
    }


def response_schema(rubric: dict[str, Any]) -> dict[str, Any]:
    """Ce que le LLM renvoie pour une transcription (ou une tranche)."""
    return {
        "type": "object",
        "properties": {
            "moments": {
                "type": "array",
                "description": "Tous les moments candidats, dans l'ordre chronologique.",
                "items": {
                    "type": "object",
                    "properties": {
                        "hook_text": {
                            "type": "string", "minLength": 1, "maxLength": 400,
                            "description": "La phrase d'accroche qui ouvre le clip, recopiee de la transcription.",
                        },
                        "start": {
                            "type": "number", "minimum": 0,
                            "description": "Debut en secondes : le debut de la ligne qui porte l'accroche.",
                        },
                        "end": {
                            "type": "number", "exclusiveMinimum": 0,
                            "description": "Fin en secondes : la fin d'une ligne, apres la chute.",
                        },
                        "format": {
                            "type": "string", "enum": list(FORMATS),
                            "description": "single : un clip court ; multipart : histoire longue en plusieurs parties.",
                        },
                        "part_breaks": {
                            "type": "array", "items": {"type": "number", "minimum": 0},
                            "description": "multipart : instants de coupe entre parties (fin d'une ligne) ; [] pour single.",
                        },
                        "justification": {
                            "type": "string", "minLength": 1, "maxLength": 500,
                            "description": "Une ou deux phrases : pourquoi ce moment marche en clip, ou ce qui le limite.",
                        },
                        "scores": _scores_schema(rubric),
                    },
                    "required": ["hook_text", "start", "end", "format", "part_breaks", "justification", "scores"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["moments"],
        "additionalProperties": False,
    }


def comparison_schema(rubric: dict[str, Any], n: int) -> dict[str, Any]:
    """Ce que le LLM renvoie au tour de comparaison : une nouvelle note pour
    chacun des ``n`` candidats, par id."""
    return {
        "type": "object",
        "properties": {
            "moments": {
                "type": "array",
                "minItems": n,
                "maxItems": n,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "minimum": 0, "maximum": n - 1},
                        "justification": {"type": "string", "minLength": 1, "maxLength": 500},
                        "scores": _scores_schema(rubric),
                    },
                    "required": ["id", "justification", "scores"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["moments"],
        "additionalProperties": False,
    }


def _num(x: float) -> str:
    """Un nombre de la grille tel qu'ecrit dans rubric.toml (60, pas 60.0)."""
    return f"{x:g}"


def _grid_text(rubric: dict[str, Any]) -> str:
    d = {k: _num(v) for k, v in rubric["durations"].items() if _number(v)}
    longest = _num(rubric["durations"]["max_parts"] * rubric["durations"]["part_max"])
    criteria = "\n".join(f"- {name} : {c['question']}" for name, c in rubric["criteria"].items())
    keywords = ", ".join(rubric["trend_keywords"]) or "(aucun)"
    return (
        "## Grille : une note entiere de 0 a 10 par critere\n"
        f"{criteria}\n"
        f"Mots-cles tendance (critere trend) : {keywords}\n\n"
        "Echelle : 0-2 absent, 3-4 faible, 5-6 correct, 7-8 fort, 9-10 exceptionnel (rare). "
        "Note chaque critere independamment et sans complaisance : le score final est calcule "
        "par le programme a partir de tes notes et les moments faibles sont elimines "
        "automatiquement ; une note gonflee fait publier un mauvais clip.\n\n"
        "## Regles de decoupe\n"
        "1. Le clip commence sur l'accroche (la phrase qui arrete le scroll), jamais sur la mise "
        "en contexte. start = debut d'une ligne de la transcription, end = fin d'une ligne : "
        "reprends les timecodes des lignes tels quels.\n"
        f"2. format \"single\" : une histoire complete de {d['single_min']} a {d['single_max']} s, "
        "publiee en un seul clip.\n"
        "   format \"multipart\" : un long passage fort (une affaire entiere, typiquement 5 a 15 min, "
        f"au plus {d['max_parts']} x {d['part_max']} = {longest} s) publie en serie de "
        f"{d['min_parts']} a {d['max_parts']} parties de {d['part_min']} a {d['part_max']} s qui se "
        "suivent, chacune finissant sur un suspense ; le decoupage final est fait par l'etape parts, "
        "part_breaks = les instants de coupe que tu suggeres (fin d'une ligne).\n"
        "   Une duree hors de ces bornes est rejetee. Un passage multipart qui atteint la note "
        "minimale passe avant les clips single qu'il contient.\n"
        "3. Aucun moment ne chevauche un segment SponsorBlock marque EXCLU.\n"
        "4. Pas de long silence au debut ni a la fin.\n"
        "5. Deux moments ne se recouvrent pas : entre deux decoupes concurrentes, garde la meilleure.\n"
    )


def _signals_text(
    meta: dict[str, Any],
    audio: dict[str, Any],
    vision: dict[str, Any] | None,
    rubric: dict[str, Any],
    settings: dict[str, Any],
    action: list[dict[str, Any]] | None = None,
) -> str:
    excluded = set(rubric["exclusions"]["sponsorblock_categories"])
    chapters = "\n".join(
        f"- [{_span(c['start_time'], c['end_time'])}] {c.get('title') or ''}" for c in meta.get("chapters") or []
    ) or "(aucun)"
    heatmap = " ".join(
        f"{h['start_time']:.0f}-{h['end_time']:.0f}:{h['value']:.2f}" for h in meta.get("heatmap") or []
    ) or "(absente)"
    sponsor = "\n".join(
        f"- [{_span(s['start_time'], s['end_time'])}] {s.get('category')}"
        + (" EXCLU" if s.get("category") in excluded else "")
        for s in meta.get("sponsorblock_segments") or []
    ) or "(aucun)"
    peaks = sorted(audio.get("peaks") or [], key=lambda p: -p["relative_db"])[: int(settings["max_audio_peaks"])]
    peaks_text = " ".join(
        f"{p['timecode']:.1f}(+{p['relative_db']:.1f}dB)" for p in sorted(peaks, key=lambda p: p["timecode"])
    ) or "(aucun)"
    frames = (vision or {}).get("frames") or []
    vision_text = "\n".join(
        f"- {f['timecode']:.1f} : {f.get('description', '')}" + (" (marquant)" if f.get("striking") else "")
        for f in frames
        if f.get("description")
    ) or "(pas de description d'images)"
    action_text = "" if action is None else (
        "\nPassages d'action detectes (bornes, score, types d'action ; des moments a proposer aussi) :\n"
        + ("\n".join(
            f"- [{_span(p['start'], p['end'])}] score {p['score']:.2f} : "
            + (", ".join(dict.fromkeys(f["action_type"] for f in p["frames"])) or "(aucune image)")
            for p in action
        ) or "(aucun)")
        + "\n"
    )
    return (
        "## Signaux mesures (des indices, pas des verites)\n"
        f"Chapitres :\n{chapters}\n\n"
        f"Most replayed (courbe YouTube, debut-fin:valeur de 0 a 1, plus haut = plus revu) :\n{heatmap}\n\n"
        f"Pics d'energie audio (rires, cris, reactions ; timecode et hauteur au-dessus du fond) :\n{peaks_text}\n\n"
        f"Segments SponsorBlock :\n{sponsor}\n\n"
        f"Descriptions d'images cles :\n{vision_text}\n"
        + action_text
    )


def _examples_text(examples: list[dict[str, Any]]) -> str:
    if not examples:
        return "## Decisions passees de l'humain\n(aucune pour l'instant)\n"

    def fmt(e: dict[str, Any]) -> str:
        m = e.get("moment") or {}
        text = (e.get("texte_moment") or "").strip().replace("\n", " ")[:_EXAMPLE_TEXT_CHARS]
        comment = f" (commentaire : {e['commentaire']})" if e.get("commentaire") else ""
        where = f"[{m['start']}-{m['end']}] " if "start" in m and "end" in m else ""
        return f"- {where}\"{text}\"{comment}"

    good = [fmt(e) for e in examples if e.get("decision") != "rejected"]
    bad = [fmt(e) for e in examples if e.get("decision") == "rejected"]
    return (
        "## Decisions passees de l'humain (d'autres videos : calibre-toi dessus)\n"
        "Retenus, a imiter :\n" + ("\n".join(good) or "(aucun)") + "\n"
        "Refuses, a eviter :\n" + ("\n".join(bad) or "(aucun)") + "\n"
    )


def _video_text(meta: dict[str, Any]) -> str:
    description = (meta.get("description") or "").strip()[:1500]
    return (
        "## Video\n"
        f"Titre : {meta.get('title') or ''}\n"
        f"Chaine : {meta.get('channel') or ''}\n"
        f"Duree : {meta.get('duration') or '?'} s\n"
        f"Description : {description}\n"
    )


_ROLE = (
    "Tu es monteur video, specialiste des clips verticaux courts (TikTok, Shorts, Reels) tires "
    "de videos longues : lives, podcasts, reportages. "
)


_SHORT_RULE = (
    "6. CLIP COURT : le clip demarre directement sur le moment fort, l'accroche tombe dans les 2 "
    "premieres secondes, sans mise en place ni preambule ; il reste comprehensible seul, sans le "
    "contexte d'avant.\n"
)


def _moments_prompt(
    context: str, rubric: dict[str, Any], lines: list[str], part: tuple[int, int] | None, short: bool = False
) -> str:
    if part is None:
        scope = "## Transcription complete (une ligne par phrase : [debut-fin] en secondes)\n"
    else:
        scope = (
            f"## Transcription, extrait {part[0]}/{part[1]} (une ligne par phrase : [debut-fin] en "
            "secondes). Les extraits se recouvrent ; ne propose que des moments entierement "
            "contenus dans celui-ci.\n"
        )
    return (
        _ROLE
        + "Repere dans la transcription TOUS les moments qui peuvent faire un clip autonome et "
        "viral, et note chacun selon la grille. Propose aussi ceux dont tu doutes (en general 5 a 30 "
        "pour une video de plusieurs heures) : le tri se fait apres, sur tes notes.\n\n"
        + _grid_text(rubric)
        + (_SHORT_RULE if short else "")
        + "\n"
        + context
        + "\n"
        + scope
        + "\n".join(lines)
        + "\n\nPour chaque moment : hook_text d'abord, puis start, end, format, part_breaks, "
        "justification et enfin les notes."
    )


def _text(c: dict[str, Any], sents: list[Sentence]) -> str:
    """Texte d'un candidat tel que le clip le dira : sa premiere phrase
    commence a ``hook_text`` (connecteurs de tete retires). Un candidat
    d'action porte sa matiere toute faite (SPEC-b0f3 R12)."""
    if "_material" in c:
        return c["_material"]
    return " ".join([c["hook_text"], *(s.text for s in sents[c["_first"] + 1 : c["_last"] + 1])])


def _comparison_prompt(context: str, rubric: dict[str, Any], candidates: list[dict[str, Any]], sents: list[Sentence]) -> str:
    blocks = []
    for n, c in enumerate(candidates):
        text = _text(c, sents)
        blocks.append(f"### id {n} [{_span(c['_start'], c['_end'])}] {c['format']}\n{text}")
    return (
        _ROLE
        + "La transcription etait trop longue pour un seul passage : ces candidats ont ete notes "
        "extrait par extrait. Compare-les maintenant entre eux et re-note chacun selon la grille, "
        "sur une echelle commune, en le jugeant sur son seul texte ci-dessous. Renvoie une note pour "
        "chaque id, sans en omettre.\n\n"
        + _grid_text(rubric)
        + "\n"
        + context
        + "\n## Candidats\n\n"
        + "\n\n".join(blocks)
    )


# --------------------------------------------------------------------------
# Traitement des candidats
# --------------------------------------------------------------------------


def _nearest(indices: range, target: float, key) -> int:
    return min(indices, key=lambda k: (abs(key(k) - target), k))


def _bounds_rejection(
    start: float, end: float, fmt: str, rubric: dict[str, Any], excluded: list[dict[str, Any]], cut: str = ""
) -> str | None:
    """Raison du rejet d'un candidat aux bornes finales : segment SponsorBlock
    exclu chevauche ou duree hors bornes de la grille (regles 2 et 3), None sinon."""
    for seg in excluded:
        if start < seg["end_time"] and seg["start_time"] < end:
            return (
                f"chevauche un segment SponsorBlock {seg.get('category')} "
                f"[{_span(seg['start_time'], seg['end_time'])}]"
            )

    d = rubric["durations"]
    tol = d["tolerance"]
    duration = end - start
    if fmt == "single":
        low, high = d["single_min"] - tol, d["single_max"] + tol
        bounds = f"{_num(d['single_min'])}-{_num(d['single_max'])} s"
    else:
        shortest, longest = d["min_parts"] * d["part_min"], d["max_parts"] * d["part_max"]
        low, high = shortest - tol, longest + tol
        bounds = f"{_num(shortest)}-{_num(longest)} s"
    if not low <= duration <= high:
        return f"duree {duration:.1f} s hors bornes {fmt} ({bounds}){cut}"
    return None


def _normalize(
    raw: dict[str, Any], sents: list[Sentence], rubric: dict[str, Any], excluded: list[dict[str, Any]],
    connectors: Connectors,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(candidat recale, None) ou (None, rejet motive)."""

    def reject(reason: str, start: float, end: float) -> tuple[None, dict[str, Any]]:
        return None, {
            "start": round(start, 1), "end": round(end, 1), "reason": reason,
            "justification": raw["justification"], "scores": raw["scores"],
        }

    if raw["end"] <= raw["start"]:
        return reject("bornes invalides (end <= start)", raw["start"], raw["end"])

    first = _nearest(range(len(sents)), raw["start"], lambda k: sents[k].start)
    last = _nearest(range(first, len(sents)), raw["end"], lambda k: sents[k].end)
    start, end = sents[first].start, sents[last].end
    hook_text, cut = sents[first].text, ""
    words = sents[first].words
    found, k = _leading_connectors([w for _, w in words] or sents[first].text.split(), connectors)
    if found:
        cut = " + ".join(f"« {c} »" for c in found)
        if not words:
            return reject(f"commence sur le connecteur {cut}, sans horodatage des mots pour le retirer", start, end)
        if k >= len(words):
            return reject(f"premiere phrase reduite au connecteur {cut}", start, end)
        start, hook_text = words[k][0], "".join(w for _, w in words[k:]).strip()
        cut = f" apres retrait du connecteur {cut}"

    reason = _bounds_rejection(start, end, raw["format"], rubric, excluded, cut)
    if reason is not None:
        return reject(reason, start, end)

    parts: list[dict[str, float]] = []
    if raw["format"] == "multipart" and raw["part_breaks"] and last > first:
        cuts = sorted({_nearest(range(first, last), b, lambda k: sents[k].end) for b in raw["part_breaks"]})
        bounds_idx = [first - 1, *cuts, last]
        parts = [
            {"start": _floor1(sents[a + 1].start), "end": _ceil1(sents[b].end)}
            for a, b in zip(bounds_idx, bounds_idx[1:])
        ]
        parts[0]["start"] = _floor1(start)

    return {
        "_first": first,
        "_last": last,
        "_start": start,
        "_end": end,
        "format": raw["format"],
        "parts": parts,
        "scores": raw["scores"],
        "justification": raw["justification"],
        "hook_text": hook_text,
    }, None


def _action_material(
    speech: str | None, signals: dict[str, Any], frames: list[dict[str, Any]]
) -> str:
    """Matiere d'un candidat d'action donnee aux noteurs (SPEC-b0f3 R12) :
    parole, signaux mesures, images decrites."""
    parole = f'Parole : "{speech}"' if speech else "Parole : (aucune)"
    mesures = (
        f"Signaux : {signals['audio_peaks']} pics audio (max +{signals['audio_peak_max_db']:.1f} dB), "
        f"{signals['scene_cuts']} changements de plan (x {signals['scene_cuts_ratio']:.1f} la médiane de la vidéo), "
        f"parole {signals['speech_ratio'] * 100:.0f} %"
    )
    images = " ; ".join(
        f"{f['timecode']:.1f} s : {f['description']} ({f['action_type']}, intensité {f['intensity']}/10)"
        for f in frames
    ) or "(aucune)"
    return f"{parole}\n{mesures}\nImages : {images}"


def _first_real_word(
    sents: list[Sentence], included: list[int], word_max_chars: int, since: float = 0.0,
) -> float | None:
    """Debut du premier mot retenu des phrases ``included`` : un mot sans
    espace de plus de ``word_max_chars`` caracteres est ignore, et un mot
    horodate avant ``since`` ne compte pas. Phrase sans horodatage des mots :
    ses mots comptent au debut de la phrase, ou a ``since`` si elle commence
    avant."""
    for k in included:
        sent = sents[k]
        if sent.words:
            for t, w in sent.words:
                if w.strip() and len(w.strip()) <= word_max_chars and t >= since - 1e-6:
                    return t
        elif any(len(w) <= word_max_chars for w in sent.text.split()):
            return max(sent.start, since)
    return None


def _window_words(sent: Sentence, start: float, end: float, word_max_chars: int) -> list[tuple[float, str]]:
    """Mots horodates de ``sent`` compris dans [start, end[ (mots geants exclus)."""
    return [
        (t, w) for t, w in sent.words
        if w.strip() and len(w.strip()) <= word_max_chars and start - 1e-6 <= t < end - 1e-6
    ]


def _action_candidate(
    p: dict[str, Any], sents: list[Sentence], rubric: dict[str, Any], excluded: list[dict[str, Any]],
    connectors: Connectors, snap: float, word_max_chars: int = 40, max_silent_start: float = 5,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(candidat d'action, None) ou (None, rejet motive) pour un passage
    d'action.json (SPEC-b0f3 R11) : bornes recalees sur la frontiere de phrase
    la plus proche si elle est a moins de ``snap`` s, sinon gardees ; connecteurs
    de tete retires ; SponsorBlock et duree comme pour un candidat de la
    transcription. La parole est mesuree sur les mots horodates (mots geants
    ignores) : un premier mot retenu apres ``max_silent_start`` s rejette le
    passage ; sans aucun mot retenu, regle R11 (accroche = image). Aucun appel
    LLM."""
    block = {
        "id": p["id"], "score": p["score"], "signals": p["signals"],
        "frames": [f["timecode"] for f in p["frames"]],
    }
    start, end = float(p["start"]), float(p["end"])

    def reject(reason: str) -> tuple[None, dict[str, Any]]:
        return None, {
            "start": _round2(start), "end": _round2(end), "reason": reason, "source": "action", "action": block,
        }

    first = _nearest(range(len(sents)), start, lambda k: sents[k].start)
    last = _nearest(range(len(sents)), end, lambda k: sents[k].end)
    snapped_start = abs(sents[first].start - start) <= snap
    if snapped_start:
        start = sents[first].start
    if abs(sents[last].end - end) <= snap:
        end = sents[last].end
    if end <= start:
        return reject("bornes invalides (end <= start)")

    included = [k for k in range(len(sents)) if sents[k].start >= start - 1e-6 and sents[k].end <= end + 1e-6]
    hook_text, cut, speech = "", "", None
    # toute phrase qui chevauche le passage compte pour la mesure de la parole
    # (commencee avant, ou commencee dedans et finie apres), sans changer l'accroche
    overlapping = [k for k in range(len(sents)) if sents[k].start < end - 1e-6 and sents[k].end > start + 1e-6]
    first_word = _first_real_word(sents, overlapping, word_max_chars, start)
    if first_word is not None and first_word - start > max_silent_start + 1e-6:
        reason = (
            f"la parole commence trop tard : premier mot a +{first_word - start:.1f} s du debut du passage "
            f"(maximum {max_silent_start:g} s)"
        )
        log.warning("passage d'action %s rejete : %s", p["id"], reason)
        return reject(reason)
    if included and first_word is None:
        included = []  # que des mots geants : passage sans parole
    if included:
        head = sents[included[0]]
        hook_text = head.text
        if snapped_start and included[0] == first:
            found, k = _leading_connectors([w for _, w in head.words] or head.text.split(), connectors)
            if found:
                cut = " + ".join(f"« {c} »" for c in found)
                if not head.words:
                    return reject(f"commence sur le connecteur {cut}, sans horodatage des mots pour le retirer")
                if k >= len(head.words):
                    return reject(f"premiere phrase reduite au connecteur {cut}")
                start, hook_text = head.words[k][0], "".join(w for _, w in head.words[k:]).strip()
                cut = f" apres retrait du connecteur {cut}"
        # parole : phrases incluses (texte entier, la premiere sans ses connecteurs de tete)
        # et, pour celles qui debordent, seulement leurs mots compris dans le passage
        parts_text = []
        for k in overlapping:
            if k == included[0]:
                parts_text.append(hook_text)
            elif k in included:
                parts_text.append(sents[k].text)
            else:
                parts_text.append("".join(w for _, w in _window_words(sents[k], start, end, word_max_chars)).strip())
        speech = " ".join(t for t in parts_text if t)
    else:
        # aucune phrase entierement incluse : les mots horodates compris dans le
        # passage (phrase debordante) donnent la parole et l'accroche
        pieces = [_window_words(sents[k], start, end, word_max_chars) for k in overlapping]
        pieces = [w for w in pieces if w]
        if pieces:
            hook_text = "".join(w for _, w in pieces[0]).strip()
            speech = " ".join("".join(w for _, w in piece).strip() for piece in pieces)
    if not hook_text:
        if not p["frames"]:
            return reject("passage sans parole ni image decrite : pas d'accroche possible")
        hook_text = max(p["frames"], key=lambda f: (f["intensity"], -f["timecode"]))["description"]

    reason = _bounds_rejection(start, end, "single", rubric, excluded, cut)
    if reason is not None:
        return reject(reason)
    return {
        "_start": start,
        "_end": end,
        "_source": "action",
        "_action": block,
        "_material": _action_material(speech, p["signals"], p["frames"]),
        "format": "single",
        "parts": [],
        "hook_text": hook_text,
        "justification": f"Passage d'action {p['id']} (score de detection {p['score']})",
    }, None


def _action_passages(video_dir: Path, settings: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Passages d'action.json si [moments] candidates = "transcript+action",
    None si "transcript" ; valeur inconnue, fichier absent ou etape desactivee :
    MomentsError (SPEC-b0f3 R10, jamais de repli sur la seule transcription)."""
    value = settings["candidates"]
    if value not in CANDIDATES:
        raise MomentsError(f"[moments] candidates invalide : {value!r} (attendu : {' | '.join(CANDIDATES)})")
    snap = settings["action_snap_seconds"]
    if not _number(snap) or snap < 0:
        raise MomentsError(f"[moments] action_snap_seconds invalide : {snap!r} (attendu : nombre >= 0)")
    for name in ("action_word_max_chars", "action_max_silent_start_s"):
        v = settings[name]
        if not _number(v) or v < 0:
            raise MomentsError(f"[moments] {name} invalide : {v!r} (attendu : nombre >= 0)")
    if value == "transcript":
        return None
    path = video_dir / "action.json"
    if not path.exists():
        raise MomentsError(f'[moments] candidates = "transcript+action" : {path} absent (lancer l\'etape action)')
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("enabled"):
        raise MomentsError(
            f'[moments] candidates = "transcript+action" : {path} produit avec [action] enabled = false '
            "(activer [action] enabled et relancer l'etape action avec --force)"
        )
    return data["passages"]


def _visual_bonus(start: float, end: float, vision: dict[str, Any] | None, rubric: dict[str, Any]) -> float:
    """Bonus des images marquantes : une image ``striking`` dans le moment suffit."""
    striking = any(
        f.get("striking") and start <= f["timecode"] <= end for f in (vision or {}).get("frames") or []
    )
    return float(rubric["bonus"]["visual"]) if striking else 0.0


def _require_timed_words(sents: list[Sentence]) -> None:
    """Transcript avec du texte mais aucun mot horodate : erreur explicite, jamais
    un malus speech_density sur chaque moment (ADR-ad2e)."""
    if sents and not any(s.words for s in sents):
        raise MomentsError(
            "speech_density : aucune phrase horodatee dans la transcription (mots absents) ; "
            "relancer transcribe ou retirer speech_density de la grille"
        )


def _untimed_overlap(start: float, end: float, sents: list[Sentence]) -> bool:
    """Vrai si une phrase recouvrant le moment n'a pas de mots horodates : la densite
    serait fausse (0 mot compte pour du silence)."""
    return any(not s.words for s in sents if s.start < end and s.end > start)


def _speech_density(start: float, end: float, sents: list[Sentence]) -> tuple[float, float]:
    """(mots par seconde, delai du premier mot en secondes) du moment, depuis
    les mots horodates des phrases ; zero mot : (0, duree)."""
    times = sorted(t for s in sents for t, _ in s.words if start <= t < end)
    duration = end - start
    if not times:
        return 0.0, duration
    return len(times) / duration, times[0] - start


def _speech_density_malus(
    start: float, end: float, sents: list[Sentence] | None, rubric: dict[str, Any]
) -> float | None:
    """Malus (<= 0) du signal speech_density, None si la grille ne le definit
    pas (signal desactive : aucune note ne change)."""
    b = rubric["bonus"]
    if "speech_density_malus" not in b:
        return None
    if sents is None:
        raise MomentsError("speech_density : phrases horodatees manquantes pour le calcul du bonus")
    wps, first = _speech_density(start, end, sents)
    sparse = wps < b["speech_density_min_wps"] or first > b["speech_density_max_first_word_s"]
    return -float(b["speech_density_malus"]) if sparse else 0.0


def _rescored_bonus_total(rubric: dict[str, Any], old: dict[str, float], visual: float) -> float:
    """Total du bonus apres nouvelle image marquante : replayed + audio + visual
    (+ speech_density quand la grille la definit), plafonne a max_total."""
    return min(
        float(rubric["bonus"]["max_total"]),
        old["replayed"] + old["audio_peaks"] + visual + old.get("speech_density", 0.0),
    )


def _bonus(
    start: float, end: float, meta: dict[str, Any], audio: dict[str, Any], vision: dict[str, Any] | None,
    rubric: dict[str, Any], sents: list[Sentence] | None = None,
) -> dict[str, float]:
    b = rubric["bonus"]
    duration = end - start
    covered = sum(
        max(0.0, min(end, h["end_time"]) - max(start, h["start_time"])) * h["value"]
        for h in meta.get("heatmap") or []
    )
    replayed = b["replayed"] * (covered / duration if duration > 0 else 0.0)
    n_peaks = sum(1 for p in audio.get("peaks") or [] if start <= p["timecode"] <= end)
    audio_bonus = b["audio_peaks"] * min(1.0, n_peaks / b["audio_peaks_full"]) if b["audio_peaks_full"] > 0 else 0.0
    visual = _visual_bonus(start, end, vision, rubric)
    speech = None
    untimed = False
    if "speech_density_malus" in b and sents is not None:
        _require_timed_words(sents)
        untimed = _untimed_overlap(start, end, sents)
    if not untimed:
        speech = _speech_density_malus(start, end, sents, rubric)
    total = min(float(b["max_total"]), replayed + audio_bonus + visual + (speech or 0.0))
    out = {
        "replayed": round(replayed, 2),
        "audio_peaks": round(audio_bonus, 2),
        "visual": round(visual, 2),
    }
    if speech is not None:
        out["speech_density"] = round(speech, 2)
    if untimed:
        out["speech_density_note"] = "phrases sans mots horodates : signal speech_density non applique"
    out["total"] = round(total, 2)
    return out


def final_score(scores: dict[str, float], rubric: dict[str, Any], bonus_total: float = 0.0) -> float:
    """Moyenne des notes ponderee par la grille, x10, plus le bonus ;
    borne a 100 et arrondie au dixieme."""
    criteria = rubric["criteria"]
    total_weight = sum(c["weight"] for c in criteria.values())
    base = sum(scores[name] * c["weight"] for name, c in criteria.items()) / total_weight * 10
    return round(min(100.0, base + bonus_total), 1)


def _overlaps(c: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((k for k in others if c["_start"] < k["_end"] and k["_start"] < c["_end"]), None)


def _cap(rubric: dict[str, Any], meta: dict[str, Any]) -> int:
    """Plafond souple (SPEC-4063 regle 4) : au plus ``max_moments_per_hour``
    moments par heure de video source, arrondi superieur, jamais moins que
    ``min_moments_cap`` (plancher pour les videos courtes ; 1 = ancien
    comportement, SPEC-0eec). La duree est lue dans meta.json ; absente ou
    invalide, une MomentsError (jamais de valeur par defaut silencieuse,
    ADR-ad2e)."""
    duration = meta.get("duration")
    if not _number(duration) or duration <= 0:
        raise MomentsError("meta.json : duration manquante ou invalide (necessaire au plafond de moments par heure)")
    hours = duration / 3600
    return max(rubric["min_moments_cap"], math.ceil(rubric["max_moments_per_hour"] * hours - 1e-9))


def _gate_rejection(c: dict[str, Any], rubric: dict[str, Any]) -> str | None:
    """Raison du rejet par le seuil eliminatoire [gate] (SPEC-b0f3 R3), ou
    None : grille sans [gate], note au critere >= ``min``, ou note a
    ``unless_criterion`` >= ``unless_min``."""
    gate = rubric.get("gate")
    if gate is None:
        return None
    note = c["scores"][gate["criterion"]]
    if note >= gate["min"]:
        return None
    if "unless_criterion" in gate and c["scores"][gate["unless_criterion"]] >= gate["unless_min"]:
        return None
    return f"{gate['criterion']} {_num(note)} < seuil éliminatoire {gate['min']} (grille)"


def _select(
    candidates: list[dict[str, Any]], rubric: dict[str, Any], meta: dict[str, Any],
    exploration: tuple[float, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """(retenus, rejets motives, bloc exploration ou None) : rejet sous
    ``min_score``, puis non-chevauchement (SPEC-0eec regle 4) : les passages
    multipart d'abord, si bien qu'un single qui chevauche un passage retenu
    est rejete meme mieux note ; entre deux candidats du meme format, le
    mieux note reste. Puis plafond souple (``_cap``) : les retenus au-dela du
    plafond sont ecartes par score decroissant, sauf ceux a
    ``always_keep_score`` ou plus (toujours gardes, et comptes dans le
    plafond). Avec ``exploration`` (part, graine), les clips d'exploration
    suivent les retenus (voir ``_explore``, qui peut piocher parmi les
    candidats ecartes par le plafond), jamais sur un retenu."""
    kept: list[dict[str, Any]] = []
    rejected: list[tuple[dict[str, Any], str]] = []
    gated: set[int] = set()  # eliminés par [gate] : jamais repêchés par l'exploration
    for c in sorted(candidates, key=lambda c: (c["format"] != "multipart", -c["final_score"], c["_start"])):
        gate_reason = _gate_rejection(c, rubric)
        if gate_reason is not None:
            rejected.append((c, gate_reason))
            gated.add(id(c))
            continue
        if c["final_score"] < rubric["min_score"]:
            rejected.append((c, f"score {c['final_score']} < min_score {rubric['min_score']}"))
            continue
        rival = _overlaps(c, kept)
        if rival is not None:
            span = _span(rival["_start"], rival["_end"])
            if rival["format"] == c["format"]:
                rejected.append((c, f"chevauche un moment mieux note [{span}]"))
            else:
                rejected.append((c, f"chevauche un passage en serie retenu [{span}], prioritaire sur un clip unique"))
            continue
        kept.append(c)

    cap = _cap(rubric, meta)
    always_keep_score = rubric["always_keep_score"]
    ranked = sorted(kept, key=lambda c: (-c["final_score"], c["_start"]))
    within_cap = {id(c) for i, c in enumerate(ranked) if i < cap or c["final_score"] >= always_keep_score}
    for c in kept:
        if id(c) not in within_cap:
            rejected.append((
                c,
                f"ecarte par le plafond de {cap} moments par heure de video "
                f"(max_moments_per_hour {rubric['max_moments_per_hour']}, "
                f"min_moments_cap {rubric['min_moments_cap']})",
            ))
    kept = [c for c in kept if id(c) in within_cap]

    info = None
    if exploration is not None:
        share, seed = exploration
        target, chosen = _explore([c for c, _ in rejected if id(c) not in gated], kept, share, seed)
        info = {"share": share, "seed": seed, "target": target, "chosen": len(chosen)}
        kept += chosen
    return kept, [{**_public(c), "reason": reason} for c, reason in rejected if not c.get("exploration")], info


def _dispersion(c: dict[str, Any]) -> float:
    """Ecart entre le score le plus haut et le plus bas des juges, chacun a
    son dernier tour dans la trace du jury."""
    latest: dict[str, float] = {}
    for rnd in c["jury"]["trace"]["rounds"]:
        latest.update({name: j["score"] for name, j in rnd["judges"].items()})
    return round(max(latest.values()) - min(latest.values()), 1)


def _explore(
    pool: list[dict[str, Any]], kept: list[dict[str, Any]], share: float, seed: int
) -> tuple[int, list[dict[str, Any]]]:
    """(nombre vise, clips d'exploration marques) : parmi ``pool`` (candidats
    notes non retenus), les plus disperses d'abord, egalites departagees par
    un tirage a graine fixe, sans chevaucher un retenu ni un autre clip
    d'exploration. Moins de candidats possibles que vise : on prend ce qu'il
    y a, et ``chosen`` < ``target`` le dit."""
    target = max(0, math.floor(share * len(kept) + 0.5))
    rng = random.Random(f"{seed}:exploration")
    draw = {id(c): rng.random() for c in sorted(pool, key=lambda c: c["_start"])}
    chosen: list[dict[str, Any]] = []
    for c in sorted(pool, key=lambda c: (-_dispersion(c), draw[id(c)])):
        if len(chosen) >= target:
            break
        if _overlaps(c, kept + chosen) is None:
            c["exploration"] = True
            chosen.append(c)
    return target, chosen


def _rubric_info(rubric_path: Path, rubric: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(rubric_path),
        "weights": {name: c["weight"] for name, c in rubric["criteria"].items()},
        "min_score": rubric["min_score"],
    }


def _origin(c: dict[str, Any]) -> dict[str, Any]:
    """``source`` d'un candidat (SPEC-b0f3 R14), avec son bloc ``action`` s'il
    vient d'un passage d'action ; vide hors [moments] candidates = "transcript+action"."""
    if "_source" not in c:
        return {}
    return {"source": c["_source"], **({"action": c["_action"]} if c["_source"] == "action" else {})}


def _public(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": _round2(c["_start"]),
        "end": _round2(c["_end"]),
        "duration": round(c["_end"] - c["_start"], 1),
        "format": c["format"],
        "parts": c["parts"],
        "scores": c["scores"],
        "bonus": c["bonus"],
        "final_score": c["final_score"],
        "justification": c["justification"],
        "hook_text": c["hook_text"],
        **({"jury": c["jury"]} if "jury" in c else {}),
        **({"exploration": True} if c.get("exploration") else {}),
        **_origin(c),
    }


def _log_jury_decisions(
    video_id: str, vetoed: list[dict[str, Any]], rejected_scored: list[dict[str, Any]], kept: list[dict[str, Any]],
) -> None:
    """Une ligne par candidat juge (done_criteria de TASK-8abc) : score final
    et decision (retenu, rejete avec sa raison, veto, ou exploration)."""
    for r in vetoed:
        log.info("%s : jury [%s-%s] veto : %s", video_id, r["start"], r["end"], r["reason"])
    for r in rejected_scored:
        log.info(
            "%s : jury [%s-%s] score %s -> rejete : %s",
            video_id, r["start"], r["end"], r.get("final_score"), r["reason"],
        )
    for c in kept:
        status = "exploration" if c.get("exploration") else "retenu"
        log.info(
            "%s : jury [%.1f-%.1f] score %.1f -> %s",
            video_id, c["_start"], c["_end"], c["final_score"], status,
        )


def _log_moments_summary(video_id: str, n_scored: int, n_kept: int, all_rejected: list[dict[str, Any]]) -> None:
    """Nombre de candidats, retenus, raison des rejets (done_criteria de
    TASK-8abc)."""
    reasons: dict[str, int] = {}
    for r in all_rejected:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    log.info(
        "%s : moments - %d candidats notes, %d retenus, %d rejetes %s",
        video_id, n_scored, n_kept, len(all_rejected), reasons,
    )


# --------------------------------------------------------------------------
# Jury
# --------------------------------------------------------------------------


def _judge(
    candidates: list[dict[str, Any]], sents: list[Sentence], rubric: dict[str, Any], context: str, config: Any
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fait noter les candidats par clipper.jury ; renvoie (deroulement du
    jury sans les candidats, candidats non vetes, rejets pour veto). Les
    notes agregees du jury remplacent celles du proposeur, gardees avec la
    trace du jury dans ``jury`` de chaque candidat."""
    items = [
        {
            "id": f"m{n}",
            "text": _text(c, sents),
            "context": f"[{_span(c['_start'], c['_end'])}] s, {c['format']}",
        }
        for n, c in enumerate(candidates)
    ]
    result = jury.deliberate(items, rubric, context=context, config=config)
    kept: list[dict[str, Any]] = []
    vetoed: list[dict[str, Any]] = []
    for c, verdict in zip(candidates, result["candidates"], strict=True):
        c["jury"] = {
            # Un candidat d'action n'a pas de note du proposeur (SPEC-b0f3 R13).
            "proposer": {"scores": c["scores"], "justification": c["justification"]} if "scores" in c else None,
            **{k: verdict[k] for k in ("score", "confidence", "veto", "debated", "trace")},
        }
        c["scores"] = verdict["scores"]
        if verdict["veto"] is None:
            kept.append(c)
            continue
        # Sans final_score : la re-notation apres vision ne le reprend pas.
        vetoed.append({
            "start": _round2(c["_start"]),
            "end": _round2(c["_end"]),
            "reason": f"veto du juge {verdict['veto']['judge']} : {verdict['veto']['reason']}",
            "format": c["format"],
            "hook_text": c["hook_text"],
            "justification": c["justification"],
            "scores": c["scores"],
            "jury": c["jury"],
            **_origin(c),
        })
    return {k: v for k, v in result.items() if k != "candidates"}, kept, vetoed


def _compare(
    candidates: list[dict[str, Any]], sents: list[Sentence], rubric: dict[str, Any], context: str, config: Any
) -> None:
    """Tour de comparaison : un appel ``moments`` qui note ensemble les
    candidats (notes et justification remplacees)."""
    def check(answer: dict[str, Any]) -> None:
        ids = [m["id"] for m in answer["moments"]]
        missing = sorted(set(range(len(candidates))) - set(ids))
        if missing or len(ids) != len(set(ids)):
            raise llm.SchemaError(f"tour de comparaison : ids manquants {missing} ou en double dans {ids}")

    answer = llm.ask(
        "moments",
        _comparison_prompt(context, rubric, candidates, sents),
        [],
        comparison_schema(rubric, len(candidates)),
        config=config,
        check=check,
    )
    for m in answer["moments"]:
        candidates[m["id"]]["scores"] = m["scores"]
        candidates[m["id"]]["justification"] = m["justification"]


# --------------------------------------------------------------------------
# Etape
# --------------------------------------------------------------------------


def _read_json(path: Path, optional: bool = False) -> Any:
    if not path.exists():
        if optional:
            return None
        raise MomentsError(f"entree absente : {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _settings(config: Any) -> dict[str, Any]:
    return {**CONFIG_DEFAULTS, **config.section("moments")}


def _selection(config: Any, settings: dict[str, Any]) -> str:
    """"jury" en mode auto (ADR-ff87) ou si [moments] selection = "jury" ;
    sinon "single"."""
    selection = settings["selection"]
    if selection not in SELECTIONS:
        raise MomentsError(f"[moments] selection invalide : {selection!r} (attendu : {' | '.join(SELECTIONS)})")
    return "jury" if config.mode == "auto" else selection


def _short_mode(settings: dict[str, Any], override: bool | None) -> tuple[bool, float, float]:
    """(actif, short_min, short_max) : ``override`` (option de la video) l'emporte
    sur [moments] short_clips (style) quand il est precise. Toute valeur invalide
    est une MomentsError (ADR-ad2e), les bornes comprises quand le mode est actif."""
    if override is not None and not isinstance(override, bool):
        raise MomentsError(f"short_clips invalide : {override!r} (attendu : true ou false)")
    style = settings["short_clips"]
    if not isinstance(style, bool):
        raise MomentsError(f"[moments] short_clips invalide : {style!r} (attendu : true ou false)")
    active = style if override is None else override
    low, high = settings["short_min"], settings["short_max"]
    if active:
        if not _number(low) or low <= 0:
            raise MomentsError(f"[moments] short_min invalide : {low!r} (attendu : nombre > 0)")
        if not _number(high) or high <= low:
            raise MomentsError(f"[moments] short_max invalide : {high!r} (attendu : nombre > short_min = {low:g})")
    return active, low, high


def _toml_key(key: str) -> str:
    return key if key.replace("_", "").replace("-", "").isalnum() else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise MomentsError(f"valeur de grille non ecrivable en TOML : {value!r}")


def _toml_dumps(table: dict[str, Any], prefix: str = "") -> str:
    out = [f"{_toml_key(k)} = {_toml_value(v)}\n" for k, v in table.items() if not isinstance(v, dict)]
    for k, v in table.items():
        if isinstance(v, dict):
            name = f"{prefix}{_toml_key(k)}"
            out.append(f"\n[{name}]\n" + _toml_dumps(v, f"{name}."))
    return "".join(out)


def _short_rubric(rubric: dict[str, Any], low: float, high: float) -> dict[str, Any]:
    """La grille avec les bornes de duree du clip unique et des parties de serie
    remplacees par short_min..short_max (le reste, tolerance et nombre de parties
    compris, est celui de la grille)."""
    durations = {**rubric["durations"], "single_min": low, "single_max": high, "part_min": low, "part_max": high}
    return {**rubric, "durations": durations}


def _exploration(settings: dict[str, Any]) -> tuple[float, int] | None:
    """(part, graine) de l'exploration, None si la part vaut 0 ; un reglage
    invalide est une MomentsError."""
    share, seed = settings["exploration_share"], settings["exploration_seed"]
    if not _number(share) or not 0 <= share <= 1:
        raise MomentsError(f"[moments] exploration_share invalide : {share!r} (attendu : nombre de 0 a 1)")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise MomentsError(f"[moments] exploration_seed invalide : {seed!r} (attendu : entier)")
    return (float(share), seed) if share > 0 else None


def run(
    video_id: str,
    workspace_dir: str | Path = "workspace",
    *,
    config: Any = None,
    force: bool = False,
    examples: list[dict[str, Any]] | None = None,
    short_clips: bool | None = None,
) -> Path:
    """Choisit les moments de la video et ecrit workspace/<video_id>/moments.json,
    dont le chemin est renvoye. Un resultat deja present n'est pas refait,
    sauf ``force`` ; si vision.json est plus recent que lui, il est seulement
    re-note, sans appel LLM (voir ``_rescore``). ``short_clips`` : choix de la
    video pour les clips courts (None = valeur du style, [moments] short_clips)."""
    if config is None:
        from clipper.config import load_config

        config = load_config()
    video_dir = Path(workspace_dir) / video_id
    out = video_dir / "moments.json"
    if out.exists() and not force:
        vision_path = video_dir / "vision.json"
        if vision_path.exists() and vision_path.stat().st_mtime_ns > out.stat().st_mtime_ns:
            return _rescore(video_dir, out, _settings(config))
        return out

    meta = _read_json(video_dir / "meta.json")
    transcript = _read_json(video_dir / "transcript.json")
    audio = _read_json(video_dir / "audio.json")
    vision = _read_json(video_dir / "vision.json", optional=True)
    settings = _settings(config)
    action_passages = _action_passages(video_dir, settings)
    selection = _selection(config, settings)
    exploration = _exploration(settings) if selection == "jury" else None
    connectors = _connectors(settings)
    rubric_path = resolve_rubric_path(settings["rubric_path"])
    rubric = load_rubric(rubric_path)
    short, short_min, short_max = _short_mode(settings, short_clips)
    effective_path = rubric_path
    if short:
        # L'etape parts relit ses bornes dans le fichier de grille que designe
        # moments.json : on lui donne une copie aux bornes courtes.
        rubric = _short_rubric(rubric, short_min, short_max)
        effective_path = video_dir / "rubric-short.toml"
    examples = list(examples or [])

    sents = split_sentences(transcript)
    if not sents:
        raise MomentsError(f"transcription vide : {video_dir / 'transcript.json'}")
    excluded_categories = set(rubric["exclusions"]["sponsorblock_categories"])
    excluded = [s for s in meta.get("sponsorblock_segments") or [] if s.get("category") in excluded_categories]

    context = _signals_text(meta, audio, vision, rubric, settings, action_passages) + "\n" + _examples_text(examples) + "\n" + _video_text(meta)
    schema = response_schema(rubric)
    lines = [_line(s) for s in sents]
    chunked = sum(len(line) + 1 for line in lines) > int(settings["max_transcript_chars"])
    if chunked:
        chunks = _chunks(sents, int(settings["chunk_chars"]), float(settings["chunk_overlap_seconds"]))
        raws = []
        for n, chunk in enumerate(chunks, 1):
            prompt = _moments_prompt(context, rubric, [_line(s) for s in chunk], (n, len(chunks)), short)
            raws += llm.ask("moments", prompt, [], schema, config=config)["moments"]
    else:
        raws = llm.ask("moments", _moments_prompt(context, rubric, lines, None, short), [], schema, config=config)["moments"]

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for raw in raws:
        candidate, rejection = _normalize(raw, sents, rubric, excluded, connectors)
        if rejection is not None:
            rejected.append(rejection)
        elif (candidate["_first"], candidate["_last"], candidate["format"]) not in seen:
            seen.add((candidate["_first"], candidate["_last"], candidate["format"]))
            candidates.append(candidate)
    candidates.sort(key=lambda c: c["_start"])
    action_candidates: list[dict[str, Any]] = []
    if action_passages is not None:
        for c in candidates:
            c["_source"] = "transcript"
        for r in rejected:
            r["source"] = "transcript"
        taken = {(_round2(c["_start"]), _round2(c["_end"])) for c in candidates}
        snap = float(settings["action_snap_seconds"])
        for p in action_passages:
            candidate, rejection = _action_candidate(
                p, sents, rubric, excluded, connectors, snap,
                float(settings["action_word_max_chars"]), float(settings["action_max_silent_start_s"]),
            )
            if rejection is not None:
                rejected.append(rejection)
                continue
            key = (_round2(candidate["_start"]), _round2(candidate["_end"]))
            if key not in taken:
                taken.add(key)
                action_candidates.append(candidate)
    scored_count = len(candidates) + len(action_candidates)

    jury_info = None
    vetoed: list[dict[str, Any]] = []
    if selection == "jury":
        # Le jury note tous les candidats ensemble (ceux d'action compris) :
        # pas de tour de comparaison.
        candidates = sorted(candidates + action_candidates, key=lambda c: c["_start"])
        jury_info, candidates, vetoed = _judge(candidates, sents, rubric, context, config)
        rejected += vetoed
    else:
        if chunked and candidates:
            _compare(candidates, sents, rubric, context, config)
        if action_candidates:
            # Pas de note du proposeur pour un candidat d'action : un appel de plus (SPEC-b0f3 R13).
            _compare(action_candidates, sents, rubric, context, config)
            candidates = sorted(candidates + action_candidates, key=lambda c: c["_start"])

    for c in candidates:
        c["bonus"] = _bonus(c["_start"], c["_end"], meta, audio, vision, rubric, sents)
        c["final_score"] = final_score(c["scores"], rubric, c["bonus"]["total"])

    kept, rejected_scored, exploration_info = _select(candidates, rubric, meta, exploration)

    if selection == "jury":
        _log_jury_decisions(video_id, vetoed, rejected_scored, kept)
    _log_moments_summary(video_id, scored_count, len(kept), rejected + rejected_scored)

    rubric_out = _rubric_info(rubric_path, rubric)
    if short:
        rubric_out = {**rubric_out, "path": str(effective_path), "source": str(rubric_path)}
        _write_text(effective_path, _toml_dumps(rubric))
    result = {
        "video_id": video_id,
        "rubric": rubric_out,
        "short_clips": short,
        **({"short_min": short_min, "short_max": short_max} if short else {}),
        "chunked": chunked,
        "selection": selection,
        **({"jury": jury_info} if jury_info is not None else {}),
        **({"exploration": exploration_info} if exploration_info is not None else {}),
        "moments": [{"id": n, **_public(c)} for n, c in enumerate(kept)],
        "rejected": rejected + rejected_scored,
    }
    _write(out, result)
    return out


def _write_text(out: Path, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)


def _write(out: Path, result: dict[str, Any]) -> None:
    _write_text(out, json.dumps(result, ensure_ascii=False, indent=2))


def _restore(
    entry: dict[str, Any], sents: list[Sentence], retained: bool, connectors: Connectors
) -> dict[str, Any]:
    """Candidat enregistre dans moments.json, avec ses bornes exactes
    retrouvees sur les phrases de la transcription (les bornes publiques sont
    arrondies au dixieme et creeraient de faux chevauchements). Le debut est
    celui de sa premiere phrase ou, connecteurs de tete retires, du mot qui
    les suit."""
    if entry.get("source") == "action":
        # Bornes de passage d'action : telles qu'enregistrees, pas de frontiere de phrase exigee.
        return {
            "_start": entry["start"],
            "_end": entry["end"],
            "_before": {"final_score": entry["final_score"], "retained": retained},
            "_source": "action",
            "_action": entry["action"],
            **{k: entry[k] for k in ("format", "parts", "scores", "bonus", "final_score", "justification", "hook_text")},
            **({"jury": entry["jury"]} if "jury" in entry else {}),
        }
    first = max((k for k in range(len(sents)) if sents[k].start <= entry["start"] + 1e-6), default=0)
    last = _nearest(range(first, len(sents)), entry["end"], lambda k: sents[k].end)
    start = sents[first].start
    found, k = _leading_connectors([w for _, w in sents[first].words], connectors)
    if found and k < len(sents[first].words) and _round2(sents[first].words[k][0]) == entry["start"]:
        start = sents[first].words[k][0]
    if (_round2(start), _round2(sents[last].end)) != (entry["start"], entry["end"]):
        raise MomentsError(
            f"moment [{entry['start']}-{entry['end']}] de moments.json hors des frontieres de phrase "
            "de transcript.json : re-notation impossible, relancer moments avec --force"
        )
    return {
        "_start": start,
        "_end": sents[last].end,
        "_before": {"final_score": entry["final_score"], "retained": retained},
        **{k: entry[k] for k in ("format", "parts", "scores", "bonus", "final_score", "justification", "hook_text")},
        **({"jury": entry["jury"]} if "jury" in entry else {}),
        **({"_source": entry["source"]} if "source" in entry else {}),
    }


def _short_rubric_keys(previous: dict[str, Any]) -> dict[str, Any]:
    """Grille aux bornes courtes d'un moments.json deja ecrit (``path`` du fichier
    copie, ``source`` de la grille d'origine) : une re-notation les garde."""
    rubric = previous.get("rubric") or {}
    return {k: rubric[k] for k in ("path", "source") if k in rubric} if "source" in rubric else {}


def _recorded_rubric_path(previous: dict[str, Any], moments_file: Path) -> Path:
    """Grille que l'etape moments a utilisee : ``rubric.path`` de moments.json,
    deja resolue. Absente ou introuvable : MomentsError, jamais de repli sur
    la grille du style (qui a pu changer depuis)."""
    rubric = previous.get("rubric")
    value = rubric.get("path") if isinstance(rubric, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise MomentsError(
            f"{moments_file} : rubric.path absent, impossible de savoir quelle grille a servi a noter ; "
            "relance l'etape moments (--force)"
        )
    path = Path(value)
    if not path.is_file():
        raise MomentsError(
            f"{moments_file} : la grille enregistree {path} est introuvable ; relance l'etape moments (--force)"
        )
    return path


def _rescore(video_dir: Path, out: Path, settings: dict[str, Any]) -> Path:
    """Re-notation apres vision, sans LLM : sur les candidats deja notes de
    moments.json (retenus, rejetes pour score ou chevauchement), recalcule le
    bonus visuel, le score final, le filtre min_score et le non-chevauchement.
    Notes par critere, bornes et justifications restent celles enregistrees ;
    les rejets de la grille (SponsorBlock, duree, bornes) sont gardes tels
    quels. En selection par jury, l'exploration est refaite sur le meme
    principe. ``rescored.changed`` liste les candidats dont le score ou le
    sort (retenu ou non) a change."""
    previous = _read_json(out)
    meta = _read_json(video_dir / "meta.json")
    vision = _read_json(video_dir / "vision.json")
    sents = split_sentences(_read_json(video_dir / "transcript.json"))
    connectors = _connectors(settings)
    rubric_path = _recorded_rubric_path(previous, out)
    rubric = load_rubric(rubric_path)

    candidates = [_restore(m, sents, True, connectors) for m in previous["moments"]]
    candidates += [_restore(r, sents, False, connectors) for r in previous["rejected"] if "final_score" in r]
    unscored = [r for r in previous["rejected"] if "final_score" not in r]
    for c in candidates:
        old = c["bonus"]
        visual = _visual_bonus(c["_start"], c["_end"], vision, rubric)
        if visual != old["visual"]:
            total = _rescored_bonus_total(rubric, old, visual)
            c["bonus"] = {**old, "visual": round(visual, 2), "total": round(total, 2)}
        c["final_score"] = final_score(c["scores"], rubric, c["bonus"]["total"])

    exploration = _exploration(settings) if previous.get("selection") == "jury" else None
    kept, rejected_scored, exploration_info = _select(candidates, rubric, meta, exploration)
    moments_out = [{"id": n, **_public(c)} for n, c in enumerate(kept)]
    ids = {id(c): n for n, c in enumerate(kept)}
    changed = []
    for c in sorted(candidates, key=lambda c: c["_start"]):
        after = {"final_score": c["final_score"], "retained": id(c) in ids}
        if after != c["_before"]:
            changed.append({
                "id": ids.get(id(c)),
                "start": _round2(c["_start"]),
                "end": _round2(c["_end"]),
                "hook_text": c["hook_text"],
                "before": c["_before"],
                "after": after,
            })

    _log_moments_summary(video_dir.name, len(candidates), len(kept), unscored + rejected_scored)

    result = {
        **{k: v for k, v in previous.items() if k != "exploration"},
        "rubric": {**_rubric_info(rubric_path, rubric), **_short_rubric_keys(previous)},
        **({"exploration": exploration_info} if exploration_info is not None else {}),
        "moments": moments_out,
        "rejected": unscored + rejected_scored,
        "rescored": {"source": "vision.json", "changed": changed},
    }
    _write(out, result)
    return out
