"""Etape parts : decoupage de chaque moment retenu en clip unique ou en
Partie 1/2/.../N qui se suivent, chacune finissant sur un suspense et
reprenant la fin de la precedente (SPEC-0eec, regle 3).

Entrees (workspace/<video_id>/) :
- moments.json (moments) : ``moments[].id, start, end, format, parts,
  hook_text, justification`` ; ``parts`` y est un decoupage indicatif,
  repris dans le prompt ;
- transcript.json (transcribe) : segments et mots horodates.

La grille est celle de l'etape moments, lue dans moments.json
(``rubric.path``, deja resolue, ``builtin:*`` compris) : une seule grille par
video, jamais une cle de [parts]. ``rubric.path`` absent ou fichier
introuvable : PartsError, jamais de repli sur rubric.toml (ADR-ad2e).

Sortie : workspace/<video_id>/parts.json

    {"video_id", "rubric": {"path", "durations"},
     "moments": [{"id", "start", "end", "duration", "format",
                  "parts_total", "proposed_cuts",
                  "parts": [{"part", "start", "end", "duration",
                             "overlap", "hook_text", "suspense"}]}],
     "rejected": [{"id", "start", "end", "duration", "reason"}]}

Phrases « du moment » : celles entierement comprises dans [start, end]
(marge 0,1 s) et celle qui contient ``start``, reduite a ses mots a partir
de ``start`` (moments fait commencer un clip au mot qui suit un connecteur
de tete, dans la phrase) ; la partie 1 porte donc la meme accroche que
moments.json.

Decision, bornes de [durations] de la grille, ``tolerance`` comprise
(la marge de recalage sur des frontieres de phrase) :
- moment ``source: action`` (SPEC-b0f3 R11 : passage sans parole, ou dont
  la parole deborde) : clip unique avec le ``hook_text`` de moments.json
  (parole ou image), aucune phrase requise, sans appel au LLM ; hors
  single_min..single_max il est rejete avec la raison ;
- duree dans single_min..single_max : clip unique, sans appel au LLM ;
  l'accroche est la premiere phrase du moment ou, sans phrase, le
  ``hook_text`` de moments.json ;
- sinon N parties de part_min..part_max, reprise comprise, N entre
  min_parts et max_parts : clipper.llm (usage ``parts``) choisit les N-1
  coupes, la ou une partie finit sur un suspense ; chaque coupe cut_k est
  recalee sur la fin de phrase la plus proche qui laisse toutes les parties,
  reprise comprise, dans les bornes (une coupe en fin de phrase n'est jamais
  dans un mot) ;
- la partie k+1 reprend avant cut_k (``resume_point``) : au debut de phrase
  de [cut_k - part_overlap_max, cut_k - part_overlap_min] le plus proche de
  cut_k - part_overlap_seconds ; a defaut, au debut de mot le plus proche
  dans cette fenetre ; a defaut, au dernier debut de mot avant cut_k
  (reprise courte) ; a defaut, au premier mot apres cut_k (reprise nulle,
  journalisee). Jamais au milieu d'un mot ni sur un silence de tete. La
  partie 1 commence au debut du moment, la derniere finit a sa fin ;
  ``overlap`` donne les secondes reprises (0 pour la partie 1) et
  ``hook_text`` le texte a partir du debut de la partie ;
- aucun N possible, aucune phrase a decouper, ou aucune suite de fins de
  phrase qui tienne les bornes : le moment va dans ``rejected`` avec la
  raison, jamais de decoupage de secours (ADR-ad2e).

Reponse LLM invalide ou Claude indisponible : l'erreur remonte, rien n'est
ecrit.

Les moments sont independants (chacun son propre appel LLM ``parts``) : ils
sont traites en parallele, au plus ``parallel`` a la fois (CONFIG_DEFAULTS,
defaut 4 ; 1 = sequentiel). La sortie (ordre de ``moments``/``rejected``)
est celle de moments.json, quel que soit l'ordre d'arrivee des reponses.
``parallel`` < 1 est refuse avec une erreur explicite.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper import llm

log = logging.getLogger(__name__)

CONFIG_DEFAULTS: dict[str, object] = {
    # Reprise (SPEC-0eec, regle 3) : la partie k+1 recommence environ
    # part_overlap_seconds s avant la fin de la partie k, de preference sur
    # un debut de phrase situe entre part_overlap_min et part_overlap_max s
    # avant la coupe.
    "part_overlap_seconds": 3,
    "part_overlap_min": 1,
    "part_overlap_max": 8,
    # Nombre de moments traites en parallele (chacun son appel LLM) ; 1 =
    # sequentiel, comme avant.
    "parallel": 4,
}

_DURATION_KEYS = ("single_min", "single_max", "part_min", "part_max", "min_parts", "max_parts", "tolerance")
_OVERLAP_KEYS = ("part_overlap_seconds", "part_overlap_min", "part_overlap_max")
_SENTENCE_END = (".", "!", "?", "…")
# Un moment est arrondi au dixieme (floor/ceil) autour de ses phrases.
_EDGE = 0.1
_EPS = 1e-6


class PartsError(Exception):
    """Entree manquante, grille (rubric.path de moments.json) ou reglages
    invalides."""


# --------------------------------------------------------------------------
# Grille et reglages
# --------------------------------------------------------------------------


def load_durations(path: str | Path) -> dict[str, float]:
    """Table [durations] de la grille ; cle manquante ou mal typee :
    PartsError qui la nomme."""
    path = Path(path)
    if not path.exists():
        raise PartsError(f"grille introuvable : {path}")
    try:
        with path.open("rb") as f:
            rubric = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise PartsError(f"{path} : TOML invalide : {exc}") from exc
    durations = rubric.get("durations")
    if not isinstance(durations, dict):
        raise PartsError(f"{path} : table [durations] manquante")
    for key in _DURATION_KEYS:
        value = durations.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise PartsError(f"{path} : [durations] {key} manquant ou invalide")
    if durations["part_min"] - durations["tolerance"] <= 0:
        raise PartsError(f"{path} : [durations] part_min doit depasser tolerance")
    if durations["max_parts"] < durations["min_parts"]:
        raise PartsError(f"{path} : [durations] max_parts doit valoir au moins min_parts")
    return {key: durations[key] for key in _DURATION_KEYS}


@dataclass(frozen=True)
class Overlap:
    """Reprise d'une partie sur la precedente, en secondes."""

    seconds: float
    min: float
    max: float


def overlap_settings(settings: dict[str, Any], d: dict[str, float]) -> Overlap:
    """Reglages part_overlap_* ; incoherents : PartsError qui les nomme."""
    values = []
    for key in _OVERLAP_KEYS:
        value = settings[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise PartsError(f"[parts] {key} doit etre un nombre >= 0, recu {value!r}")
        values.append(float(value))
    ov = Overlap(*values)
    if not ov.min <= ov.seconds <= ov.max:
        raise PartsError("[parts] il faut part_overlap_min <= part_overlap_seconds <= part_overlap_max")
    if ov.seconds >= d["part_min"] - d["tolerance"]:
        raise PartsError("[parts] part_overlap_seconds doit rester sous part_min - tolerance")
    return ov


def parallel_workers(settings: dict[str, Any]) -> int:
    """Nombre de moments traites en parallele ; < 1 : PartsError qui le nomme."""
    value = settings["parallel"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PartsError(f"[parts] parallel doit etre un entier >= 1, recu {value!r}")
    return value


def rubric_path_of(moments: dict[str, Any], moments_file: Path) -> Path:
    """Grille utilisee par l'etape moments : ``rubric.path`` de moments.json.
    Absent ou vide : PartsError (aucun repli sur rubric.toml)."""
    rubric = moments.get("rubric")
    path = rubric.get("path") if isinstance(rubric, dict) else None
    if not isinstance(path, str) or not path.strip():
        raise PartsError(
            f"{moments_file} : rubric.path absent, impossible de savoir quelle grille l'etape moments a "
            "utilisee ; relance l'etape moments (--force)"
        )
    return Path(path)


# --------------------------------------------------------------------------
# Phrases
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Sentence:
    """Une phrase et ses mots horodates (transcript.json) ; un segment sans
    mots horodates en a un seul, qui le couvre."""

    start: float
    end: float
    text: str
    words: tuple[Word, ...]


def _ends_sentence(word: str) -> bool:
    return word.strip().rstrip("\"'»)]").endswith(_SENTENCE_END)


def split_sentences(transcript: dict[str, Any]) -> list[Sentence]:
    """Phrases de la transcription, decoupees comme l'etape moments : un mot
    finissant par . ! ? ou … ferme une phrase, une fin de segment aussi."""
    out: list[Sentence] = []

    def flush(words: list[dict[str, Any]]) -> None:
        text = "".join(w["word"] for w in words).strip()
        if text:
            timed = tuple(Word(w["start"], w["end"], w["word"]) for w in words)
            out.append(Sentence(words[0]["start"], words[-1]["end"], text, timed))

    for seg in transcript.get("segments", []):
        words = seg.get("words") or []
        if not words:
            text = seg.get("text", "").strip()
            if text:
                out.append(Sentence(seg["start"], seg["end"], text, (Word(seg["start"], seg["end"], text),)))
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


def _inside(sents: list[Sentence], start: float, end: float) -> list[Sentence]:
    """Phrases du moment [start, end] : celles qui y tiennent entierement
    (marge _EDGE) et celle qui contient ``start`` (moments fait commencer un
    clip au mot qui suit un connecteur de tete, dans la phrase), reduite a ses
    mots a partir de ``start``. Une phrase qui deborde de ``end`` n'en est
    pas."""
    out: list[Sentence] = []
    for s in sents:
        if s.end > end + _EDGE + _EPS:
            continue
        if s.start >= start - _EDGE - _EPS:
            out.append(s)
        elif s.end > start + _EPS:
            words = tuple(w for w in s.words if w.start >= start - _EDGE - _EPS)
            text = "".join(w.text for w in words).strip()
            if words and text:
                out.append(Sentence(words[0].start, s.end, text, words))
    return out


# --------------------------------------------------------------------------
# Decoupage
# --------------------------------------------------------------------------


def part_count_range(duration: float, d: dict[str, float], overlap: float = 0.0) -> tuple[int, int]:
    """Nombres de parties (min, max) qui peuvent tenir ``duration`` quand
    chaque partie reprend ``overlap`` s de la precedente : N parties durent
    ensemble duration + (N - 1) x overlap ; bornes min_parts..max_parts ;
    min > max si aucun."""
    low, high = d["part_min"] - d["tolerance"], d["part_max"] + d["tolerance"]
    span = duration - overlap
    return (
        max(int(d["min_parts"]), math.ceil(span / (high - overlap) - _EPS)),
        min(int(d["max_parts"]), math.floor(span / (low - overlap) + _EPS)),
    )


def is_single(duration: float, d: dict[str, float]) -> bool:
    """Duree d'un clip unique : single_min..single_max, tolerance comprise."""
    tol = d["tolerance"]
    return d["single_min"] - tol - _EPS <= duration <= d["single_max"] + tol + _EPS


def accepts_duration(duration: float, d: dict[str, float], overlap: float = 0.0) -> bool:
    """Parts ne rejette pas ce moment pour sa duree : clip unique, ou un
    nombre de parties possible. Jamais plus strict que _normalize (moments)
    pour la meme grille."""
    if is_single(duration, d):
        return True
    low, high = part_count_range(duration, d, overlap)
    return low <= high


@dataclass(frozen=True)
class Resume:
    """Debut de la partie qui suit une coupe ; ``how`` : phrase, mot,
    courte ou nulle (le repli qui l'a donne)."""

    start: float
    overlap: float
    hook_text: str
    how: str


def resume_point(cut: float, sents: list[Sentence], lower: float, upper: float, ov: Overlap) -> Resume | None:
    """Ou commence la partie qui suit une coupe en ``cut`` (fin de phrase),
    parmi les mots qui commencent dans ]lower, upper[ :
    1. le debut de phrase de [cut - ov.max, cut - ov.min] le plus proche de
       cut - ov.seconds ;
    2. sinon le debut de mot le plus proche de cut - ov.seconds dans cette
       fenetre ;
    3. sinon le dernier debut de mot de ]cut - ov.min, cut[ (reprise courte) ;
    4. sinon le premier mot apres cut (reprise nulle).
    None s'il n'y a aucun mot apres la coupe."""
    target, lo, hi = cut - ov.seconds, cut - ov.max, cut - ov.min
    # (debut du mot, phrase, rang du mot dans la phrase)
    words = [(w.start, s, i) for s in sents for i, w in enumerate(s.words) if lower + _EPS < w.start < upper - _EPS]

    def resume(point: tuple[float, Sentence, int], how: str) -> Resume:
        start, sent, i = point
        hook = "".join(w.text for w in sent.words[i:]).strip()
        return Resume(start, max(0.0, round(cut - start, 2)), hook, how)

    def closest(points: list[tuple[float, Sentence, int]]) -> tuple[float, Sentence, int] | None:
        return min(points, key=lambda p: (abs(p[0] - target), -p[0]), default=None)

    in_window = [p for p in words if lo - _EPS <= p[0] <= hi + _EPS]
    best = closest([p for p in in_window if p[2] == 0])
    if best is not None:
        return resume(best, "phrase")
    best = closest(in_window)
    if best is not None:
        return resume(best, "mot")
    short = [p for p in words if hi + _EPS < p[0] < cut - _EPS]
    if short:
        return resume(max(short, key=lambda p: p[0]), "courte")
    after = [p for p in words if p[0] >= cut - _EPS]
    if after:
        return resume(min(after, key=lambda p: p[0]), "nulle")
    return None


def snap_cuts(
    start: float,
    end: float,
    candidates: list[float],
    resumes: list[float | None],
    proposed: list[float],
    low: float,
    high: float,
) -> list[float] | None:
    """Choisit len(proposed) coupes parmi ``candidates`` (fins de phrase,
    croissantes) telles que chaque partie dure entre ``low`` et ``high``, au
    plus pres des coupes proposees (somme des ecarts minimale) ; la partie
    qui suit la coupe candidates[i] commence en resumes[i], reprise comprise
    (None : pas de suite possible). None si aucune suite ne tient les
    bornes."""
    fits = lambda a, b: a is not None and low - _EPS <= b - a <= high + _EPS  # noqa: E731
    m = len(candidates)
    # cost[i], prev[j][i] : meilleure suite dont la j-ieme coupe est candidates[i].
    cost = [abs(c - proposed[0]) if fits(start, c) else math.inf for c in candidates]
    prev: list[list[int]] = [[-1] * m]
    for p in proposed[1:]:
        new, back = [math.inf] * m, [-1] * m
        for i, c in enumerate(candidates):
            for k in range(i):
                if cost[k] < new[i] and fits(resumes[k], c):
                    new[i], back[i] = cost[k], k
            new[i] += abs(c - p)
        cost = new
        prev.append(back)
    best = min(
        (i for i in range(m) if cost[i] < math.inf and fits(resumes[i], end)),
        key=lambda i: (cost[i], i),
        default=None,
    )
    if best is None:
        return None
    chosen = [best]
    for back in reversed(prev[1:]):
        chosen.append(back[chosen[-1]])
    return [candidates[i] for i in reversed(chosen)]


def response_schema(start: float, end: float, n_range: tuple[int, int]) -> dict[str, Any]:
    """Ce que le LLM renvoie pour un moment a couper en n_range parties."""
    return {
        "type": "object",
        "properties": {
            "cuts": {
                "type": "array",
                "minItems": n_range[0] - 1,
                "maxItems": n_range[1] - 1,
                "description": "Les coupes entre parties, dans l'ordre chronologique.",
                "items": {
                    "type": "object",
                    "properties": {
                        "at": {
                            "type": "number", "exclusiveMinimum": start, "exclusiveMaximum": end,
                            "description": "Instant de coupe en secondes : la fin d'une ligne de la transcription.",
                        },
                        "suspense": {
                            "type": "string", "minLength": 1, "maxLength": 400,
                            "description": "Pourquoi la partie qui finit ici laisse le spectateur en suspense.",
                        },
                    },
                    "required": ["at", "suspense"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["cuts"],
        "additionalProperties": False,
    }


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _prompt(
    moment: dict[str, Any], sents: list[Sentence], d: dict[str, float], n_range: tuple[int, int], ov: Overlap
) -> str:
    lines = "\n".join(f"[{_fmt(s.start)}-{_fmt(s.end)}] {s.text}" for s in sents)
    hint = ""
    if moment.get("parts"):
        spans = ", ".join(f"{_fmt(p['start'])}-{_fmt(p['end'])}" for p in moment["parts"])
        hint = f"Decoupage indicatif propose au tri des moments (a revoir librement) : {spans}\n"
    n_text = f"{n_range[0]}" if n_range[0] == n_range[1] else f"{n_range[0]} a {n_range[1]}"
    return (
        "Tu decoupes un moment d'une video longue en plusieurs parties pour TikTok "
        "(Partie 1, Partie 2, ...), publiees separement. Les parties se suivent : chaque partie "
        f"reprend environ {_fmt(ov.seconds)} s de la precedente (la Partie N recommence sur la fin de "
        "la Partie N-1, de preference sur sa derniere phrase, pour raccrocher le spectateur).\n\n"
        "## Regles\n"
        f"1. {n_text} parties, chacune de {_fmt(d['part_min'])} a {_fmt(d['part_max'])} s reprise comprise : "
        f"renvoie {n_range[0] - 1 if n_range[0] == n_range[1] else f'{n_range[0] - 1} a {n_range[1] - 1}'} coupe(s).\n"
        "2. Une coupe est la fin d'une ligne de la transcription : reprends son timecode de fin tel quel.\n"
        "3. Chaque partie sauf la derniere finit sur un suspense : question laissee ouverte, revelation "
        "imminente, conflit au sommet, phrase qui appelle la suite. Jamais au milieu d'une explication "
        "qui retombe, jamais apres la chute.\n"
        "4. La partie suivante repart sur une accroche : ses premieres lignes, reprise comprise, doivent "
        "donner envie sans avoir vu la partie precedente.\n"
        "5. La derniere partie garde la chute du moment.\n\n"
        "## Moment\n"
        f"De {_fmt(moment['start'])} a {_fmt(moment['end'])} s ({_fmt(moment['end'] - moment['start'])} s).\n"
        f"Accroche : {moment.get('hook_text', '')}\n"
        f"Pourquoi il a ete retenu : {moment.get('justification', '')}\n"
        f"{hint}\n"
        "## Transcription du moment ([debut-fin] en secondes)\n"
        f"{lines}\n"
    )


def _part(n: int, start: float, end: float, overlap: float, hook_text: str, suspense: str | None) -> dict[str, Any]:
    return {
        "part": n,
        "start": start,
        "end": end,
        "duration": round(end - start, 2),
        "overlap": overlap,
        "hook_text": hook_text,
        "suspense": suspense,
    }


def _split(
    moment: dict[str, Any], sents: list[Sentence], d: dict[str, float], ov: Overlap, config: Any
) -> tuple[dict[str, Any] | None, str | None]:
    """(decoupage, None) ou (None, raison du rejet)."""
    start, end = moment["start"], moment["end"]
    duration = end - start
    tol = d["tolerance"]
    record = {"id": moment["id"], "start": start, "end": end, "duration": round(duration, 2)}
    inside = _inside(sents, start, end)
    single = is_single(duration, d)

    def single_clip(hook_text: str) -> tuple[dict[str, Any], None]:
        return {**record, "format": "single", "parts_total": 1, "proposed_cuts": [],
                "parts": [_part(1, start, end, 0, hook_text, None)]}, None

    if moment.get("source") == "action":
        # Candidat d'action (SPEC-b0f3 R11) : un passage sans parole, ou dont
        # la parole deborde, est un clip unique dont l'accroche (parole ou
        # image) est celle de moments.json ; aucune phrase n'est requise.
        if not single:
            return None, (
                f"passage d'action de {duration:.1f} s hors clip unique "
                f"({_fmt(d['single_min'])}-{_fmt(d['single_max'])} s, tolerance {_fmt(tol)} s)"
            )
        hook_text = str(moment.get("hook_text") or "").strip()
        if not hook_text:
            return None, "passage d'action sans accroche dans moments.json"
        return single_clip(hook_text)

    if not inside:
        if single:
            hook_text = str(moment.get("hook_text") or "").strip()
            if not hook_text:
                return None, "aucune phrase de la transcription dans le moment ni accroche dans moments.json"
            return single_clip(hook_text)
        return None, "aucune phrase de la transcription dans le moment"

    if single:
        return single_clip(inside[0].text)

    n_range = part_count_range(duration, d, ov.seconds)
    if n_range[0] > n_range[1]:
        return None, (
            f"duree {duration:.1f} s : ni clip unique ({_fmt(d['single_min'])}-{_fmt(d['single_max'])} s) "
            f"ni {int(d['min_parts'])} a {int(d['max_parts'])} parties de "
            f"{_fmt(d['part_min'])}-{_fmt(d['part_max'])} s (tolerance {_fmt(tol)} s, "
            f"reprise de {_fmt(ov.seconds)} s comprise)"
        )

    answer = llm.ask("parts", _prompt(moment, inside, d, n_range, ov), [], response_schema(start, end, n_range),
                     config=config)
    proposals = sorted(answer["cuts"], key=lambda c: c["at"])
    candidates = [s.end for s in inside[:-1] if start + _EPS < s.end < end - _EPS]
    resumes = [resume_point(c, inside, start, end, ov) for c in candidates]
    cut_times = snap_cuts(
        start, end, candidates, [r.start if r else None for r in resumes],
        [c["at"] for c in proposals], d["part_min"] - tol, d["part_max"] + tol,
    )
    if cut_times is None:
        return None, (
            f"aucune suite de fins de phrase ne donne {len(proposals) + 1} parties de "
            f"{_fmt(d['part_min'])}-{_fmt(d['part_max'])} s (tolerance {_fmt(tol)} s, reprise comprise)"
        )
    resume_at = dict(zip(candidates, resumes))
    heads = [(start, 0, inside[0].text)]
    for n, cut in enumerate(cut_times, 2):
        r = resume_at[cut]
        if r.how == "nulle":
            log.warning(
                "moment %s : partie %d en reprise nulle, aucun debut de mot dans les %s s avant la coupe %s ; "
                "elle commence au premier mot apres, en %s",
                moment["id"], n, _fmt(ov.max), _fmt(cut), _fmt(r.start),
            )
        heads.append((r.start, r.overlap, r.hook_text))
    ends = [*cut_times, end]
    suspense = [c["suspense"] for c in proposals] + [None]
    parts = [
        _part(n, a, b, overlap, hook, suspense[n - 1])
        for n, ((a, overlap, hook), b) in enumerate(zip(heads, ends), 1)
    ]
    return {**record, "format": "multipart", "parts_total": len(parts),
            "proposed_cuts": [c["at"] for c in proposals], "parts": parts}, None


# --------------------------------------------------------------------------
# Etape
# --------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise PartsError(f"entree absente : {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _settings(config: Any) -> dict[str, Any]:
    if config is None:
        from clipper.config import load_config

        config = load_config()
    settings = {**CONFIG_DEFAULTS, **config.section("parts")}
    if "rubric_path" in settings:
        raise PartsError(
            "[parts] rubric_path n'existe plus : la grille est celle de l'etape moments "
            "([moments] rubric_path, relevee dans moments.json) ; supprime cette cle de la config"
        )
    return settings


def run(
    video_id: str,
    workspace_dir: str | Path = "workspace",
    *,
    config: Any = None,
    force: bool = False,
) -> Path:
    """Decoupe chaque moment de moments.json et ecrit
    workspace/<video_id>/parts.json, dont le chemin est renvoye. Un resultat
    deja present n'est pas refait, sauf ``force``. Les moments sont traites
    en parallele ([parts] parallel, defaut 4) ; l'echec de l'un fait remonter
    l'erreur sans rien ecrire."""
    video_dir = Path(workspace_dir) / video_id
    out = video_dir / "parts.json"
    if out.exists() and not force:
        return out

    moments = _read_json(video_dir / "moments.json")
    transcript = _read_json(video_dir / "transcript.json")
    settings = _settings(config)
    rubric_path = rubric_path_of(moments, video_dir / "moments.json")
    durations = load_durations(rubric_path)
    overlap = overlap_settings(settings, durations)
    workers = parallel_workers(settings)
    sents = split_sentences(transcript)

    def process(moment: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        return _split(moment, sents, durations, overlap, config)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        outcomes = list(executor.map(process, moments["moments"]))

    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for moment, (split, reason) in zip(moments["moments"], outcomes):
        if split is None:
            rejected.append({"id": moment["id"], "start": moment["start"], "end": moment["end"],
                             "duration": round(moment["end"] - moment["start"], 2), "reason": reason})
        else:
            kept.append(split)

    result = {
        "video_id": video_id,
        "rubric": {"path": str(rubric_path), "durations": durations},
        "moments": kept,
        "rejected": rejected,
    }
    video_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    return out
