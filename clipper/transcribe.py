"""Etape transcribe : faster-whisper mot par mot, vocabulaire et correction
des noms propres par clipper.llm.

Entrees : workspace/<video_id>/<video_id>.mp4 et meta.json (etape download).
Sortie  : workspace/<video_id>/transcript.json (et un intermediaire,
transcript_raw.json, garde avant la correction)

    {"video_id", "language", "language_probability", "duration", "model",
     "vocab": [...], "transcript_fix_refused": <int>,
     "segments": [{"id", "start", "end", "text",
                   "words": [{"word", "start", "end", "probability"}]}]}

Deroulement :
1. usage ``vocab`` : noms propres tires du titre et de la description,
   passes a whisper en initial_prompt (avant de charger le modele : jamais
   un LLM local et whisper en meme temps, ADR-fb9b), raccourcis a
   ``vocab_max_tokens`` tokens pour tenir dans la fenetre du decodeur
   (journalise ; transcript.json et la correction gardent tout) ;
2. extraction de l'audio (ffmpeg, wav 16 kHz mono), transcription via
   ``BatchedInferencePipeline`` (``batch_size`` de la config, defaut 8 ;
   ~4,9x plus rapide qu'un ``WhisperModel.transcribe`` sequentiel sur une
   video reelle, sans perte de mots mais ponctuation/majuscules internes
   moins riches, banc docs/bench-whisper-vitesse.md), ou sequentiel si
   ``batch_size <= 1`` ; puis liberation du modele et ecriture du resultat
   brut dans transcript_raw.json avant la correction ;
3. usage ``transcript_fix`` par tranches de ``fix_chunk_words`` mots : le
   prompt d'une tranche est un bloc commun (consignes + vocabulaire,
   ``_fix_prefix``, identique octet pour octet d'une tranche a l'autre)
   suivi de son seul contenu (index + mot par ligne, jamais le JSON complet
   des segments ni leurs horodatages). La premiere tranche part seule pour
   que le fournisseur du modele mette ce bloc commun en cache, puis jusqu'a
   ``fix_parallel`` tranches suivantes partent en meme temps (threads : les
   appels clipper.llm sont des sous-processus) et le relisent au lieu de le
   reecrire chacune (meme principe que clipper.jury, TASK-2852). La reponse
   ne liste que des corrections {i, old, word} par index de mot (ancien
   texte, nouveau texte), donc ni le nombre de mots ni leurs timecodes ne
   peuvent changer. ``old`` est compare au mot reellement present a cet index
   en ignorant la ponctuation collee et la casse (``Alstner`` == ``Alstner,``) ;
   la correction s'applique alors en gardant la ponctuation d'origine autour
   du mot. Une correction dont ``old`` ne correspond toujours pas (index
   decale, mot hallucine...) est ignoree et journalisee dans
   llm_refusals.jsonl (compteur et exemples), jamais appliquee en silence,
   sans faire echouer l'etape (ADR-ad2e) ; un compteur
   ``transcript_fix_refused`` est aussi ecrit dans transcript.json. Si une
   part anormale des corrections d'une video est refusee (majorite stricte,
   au moins 2), c'est en revanche un echec explicite : la correction semble
   decalee dans son ensemble, pas seulement pour un mot isole.

   Chaque tranche reussie est ecrite tout de suite dans
   workspace/<id>/fix_chunks/<hash>.json (cle = hash du texte de la tranche
   et de ``_FIX_CACHE_VERSION``) : un nouveau passage de l'etape (apres
   l'echec d'une autre tranche) ne redemande que les tranches manquantes, au
   lieu de tout refaire (TASK-db6f). Le delai d'un appel de correction est
   ``fix_timeout_s`` (defaut 360 s), propre a cet usage ([llm] timeout global
   inchange pour les autres usages). Une tranche en echec transitoire
   (TransientLLMError : quota, reseau, timeout...) est relancee une fois tout
   de suite, seule ; au second echec l'etape echoue explicitement comme
   avant, jamais de texte non corrige substitue en silence (ADR-ad2e). Un
   ``force`` videant transcript_raw.json (whisper et vocabulaire refaits)
   vide aussi ce cache de tranches, pour ne jamais reutiliser une correction
   calculee sur un texte ou un vocabulaire differents.

Si transcript_raw.json existe deja (retour apres un echec de la correction),
il est reutilise et whisper n'est pas relance, sauf ``force``.

Claude indisponible : l'erreur remonte, rien n'est ecrit (ADR-ad2e). Seul
``vocab = false`` / ``transcript_fix = false`` dans [transcribe] saute ces
appels.
"""

from __future__ import annotations

import functools
import gc
import hashlib
import json
import logging
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clipper import llm
from clipper.gpu import get_device

CONFIG_DEFAULTS: dict[str, object] = {
    # Binaire ffmpeg de l'extraction audio (nom dans le PATH ou chemin complet).
    "ffmpeg_bin": "ffmpeg",
    # Taille du modele faster-whisper (tiny, base, small, medium, large-v3...).
    "model": "small",
    # Langue forcee ("fr", "en"...) ; absente = detection automatique.
    "language": None,
    "beam_size": 5,
    "vad_filter": True,
    # Taille de lot BatchedInferencePipeline (banc TASK-be65 : ~4,9x plus
    # rapide que le sequentiel sur une video reelle, sans perte de mots,
    # docs/bench-whisper-vitesse.md). <= 1 = sequentiel (WhisperModel.transcribe
    # direct, comportement d'avant ce reglage).
    "batch_size": 8,
    # Vocabulaire de noms propres demande a clipper.llm (usage vocab).
    "vocab": True,
    # Tokens maximum du vocabulaire passe a whisper en initial_prompt :
    # au-dela, les dernieres entrees sont ecartees et c'est journalise. Le
    # decodeur a 448 positions, que faster-whisper remplit avec 1 (sot_prev)
    # + 223 tokens de contexte (initial_prompt) + tokens speciaux : la borne
    # est refusee au-dela de _VOCAB_TOKENS_CEILING (100 positions laissees a
    # la transcription de chaque fenetre).
    "vocab_max_tokens": 100,
    # Correction des mots par clipper.llm (usage transcript_fix).
    "transcript_fix": True,
    # Nombre de mots vises par tranche de correction (une tranche ne coupe
    # jamais un segment).
    "fix_chunk_words": 3000,
    # Nombre de tranches de correction traitees en meme temps (appels
    # clipper.llm en sous-processus, pas de cout CPU Python).
    "fix_parallel": 4,
    # Delai (s) d'un appel clipper.llm pour une seule tranche de correction
    # (usage transcript_fix) : surcharge le timeout global du backend pour cet
    # appel precis, les autres usages (vocab, moments...) gardent leur propre
    # reglage (TASK-db6f : perdre au plus une tranche et le detecter en
    # quelques minutes, pas tout recommencer apres le timeout global de 900 s).
    "fix_timeout_s": 360,
    # Longueur (caracteres, sans espaces) au-dela de laquelle un « mot » whisper
    # est une hallucination sur la musique (ex. « Tantantan... » de 420
    # caracteres) : il est retire, journalise et compte (TASK-a64a).
    "hallucination_word_max_chars": 40,
}

log = logging.getLogger(__name__)

# Fenetre du decodeur Whisper et prompt construit par faster-whisper
# (WhisperModel.get_prompt, 1.2.1) : [sot_prev] + texte precedent
# (initial_prompt puis transcription deja faite, 223 derniers tokens) +
# sot, langue, tache (+ no_timestamps). TASK-b20f (avant le retrait de
# hotwords, TASK-913b) : sans borne, un vocabulaire long donnait 1 + 223
# (hotwords) + 223 (initial_prompt) + 3 = 450 > 448.
_WHISPER_POSITIONS = 448
_WHISPER_CONTEXT_TOKENS = _WHISPER_POSITIONS // 2 - 1
_WHISPER_SPECIAL_TOKENS = 5
_MIN_GENERATION_TOKENS = 100
_VOCAB_TOKENS_CEILING = (
    _WHISPER_POSITIONS - _WHISPER_SPECIAL_TOKENS - _WHISPER_CONTEXT_TOKENS - _MIN_GENERATION_TOKENS
)

VOCAB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "words": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 100},
    },
    "required": ["words"],
    "additionalProperties": False,
}


class TranscribeError(Exception):
    """Entree manquante, ffmpeg en echec ou correction inapplicable."""


def extract_audio(video_path: str | Path, audio_path: str | Path, ffmpeg_bin: str = "ffmpeg") -> None:
    """Extrait la piste audio de la video en wav PCM 16 kHz mono."""
    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error", "-i", str(video_path),
        "-vn", "-af", "aresample=async=1:first_pts=0",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise TranscribeError(f"ffmpeg introuvable ({ffmpeg_bin})") from exc
    except subprocess.CalledProcessError as exc:
        raise TranscribeError(f"ffmpeg a echoue sur {video_path} : {exc.stderr.strip()}") from exc


def _whisper_model(name: str, device: str, compute_type: str) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(name, device=device, compute_type=compute_type)


def _batched_pipeline(model: Any) -> Any:
    from faster_whisper import BatchedInferencePipeline

    return BatchedInferencePipeline(model)


def _settings(config: Any) -> dict[str, Any]:
    if config is None:
        from clipper.config import load_config

        config = load_config()
    return {**CONFIG_DEFAULTS, **config.section("transcribe")}


def _check_hallucination_word_max_chars(settings: dict[str, Any]) -> int:
    bound = settings["hallucination_word_max_chars"]
    if not isinstance(bound, int) or isinstance(bound, bool) or bound < 1:
        raise TranscribeError(
            f"[transcribe] hallucination_word_max_chars = {bound!r} : attendu un entier >= 1"
        )
    return bound


def _drop_hallucinated_words(segments: list[dict[str, Any]], max_chars: int) -> int:
    """Retire de ``segments`` (en place) les mots plus longs que ``max_chars``
    caracteres (espaces retires) ; le texte du segment est reconstruit, un
    segment sans mot restant disparait. Chaque retrait est journalise.
    Renvoie le nombre de mots retires."""
    removed = 0
    kept_segments = []
    for seg in segments:
        words = []
        dropped = False
        for w in seg["words"]:
            core = "".join(w["word"].split())
            if len(core) > max_chars:
                removed += 1
                dropped = True
                log.warning(
                    "mot whisper retire (hallucination) a %.2f s : %d caracteres, debut %r",
                    w["start"], len(core), core[:40],
                )
            else:
                words.append(w)
        if not words and seg["words"]:
            continue
        if dropped:
            seg["words"] = words
            seg["text"] = "".join(w["word"] for w in words)
        kept_segments.append(seg)
    segments[:] = kept_segments
    return removed


def _ask_vocab(meta: dict[str, Any], config: Any) -> list[str]:
    prompt = (
        "Voici le titre et la description d'une video YouTube qui va etre "
        "transcrite automatiquement par Whisper. Donne la liste des noms "
        "propres, marques, titres de jeux, pseudos et termes rares que Whisper "
        "risque de mal orthographier, ecrits exactement comme il faut.\n\n"
        f"Titre : {meta.get('title') or ''}\n\n"
        f"Description :\n{meta.get('description') or ''}"
    )
    answer = llm.ask("vocab", prompt, [], VOCAB_SCHEMA, config=config)
    return [w.strip() for w in answer["words"] if w.strip()]


def _free_memory() -> None:
    """Rend la memoire d'un modele dont l'appelant a lache les references
    (ADR-fb9b)."""
    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _check_vocab_max_tokens(settings: dict[str, Any]) -> None:
    bound = settings["vocab_max_tokens"]
    if not isinstance(bound, int) or isinstance(bound, bool) or not 1 <= bound <= _VOCAB_TOKENS_CEILING:
        raise TranscribeError(
            f"[transcribe] vocab_max_tokens = {bound!r} : attendu un entier entre 1 et "
            f"{_VOCAB_TOKENS_CEILING} (fenetre de {_WHISPER_POSITIONS} positions du decodeur "
            f"Whisper, dont {_WHISPER_CONTEXT_TOKENS} de contexte et {_MIN_GENERATION_TOKENS} "
            "gardees pour la transcription) ; pour ne pas passer de vocabulaire : vocab = false"
        )


def _whisper_vocab(model: Any, vocab: list[str], max_tokens: int) -> list[str]:
    """Plus longue tete du vocabulaire (entrees entieres, dans l'ordre) dont
    initial_prompt tient dans ``max_tokens`` tokens du tokenizer du modele,
    comptes comme faster-whisper les encode. Un modele sans ``hf_tokenizer``
    (WhisperModel en a toujours un) est borne par le nombre d'octets UTF-8,
    qui majore les tokens d'un BPE sur octets ; c'est journalise. Les
    entrees ecartees sont journalisees (ADR-ad2e)."""
    hf_tokenizer = getattr(model, "hf_tokenizer", None)

    def tokens(text: str) -> int:
        if hf_tokenizer is None:
            return len((" " + text).encode("utf-8"))
        return len(hf_tokenizer.encode(" " + text, add_special_tokens=False).ids)

    if vocab and hf_tokenizer is None:
        log.warning(
            "modele whisper sans hf_tokenizer : tokens du vocabulaire majores par ses octets UTF-8"
        )

    kept = len(vocab)
    while kept and tokens(", ".join(vocab[:kept])) > max_tokens:
        kept -= 1
    if kept < len(vocab):
        log.warning(
            "vocabulaire whisper raccourci a %d/%d entrees (vocab_max_tokens = %d, fenetre du "
            "decodeur) ; ecartees : %s",
            kept, len(vocab), max_tokens, ", ".join(vocab[kept:]),
        )
    return vocab[:kept]


def _progress_gate(total: float) -> Any:
    """Ferme une fonction ``gate(done)`` (secondes d'audio traitees, sur
    ``total``) qui dit si cet avancement doit etre annonce a INFO : au plus
    toutes les 30 s ou tous les 10 % (toujours l'arrivee). Done_criteria de
    TASK-8abc."""
    last: dict[str, float] = {"done": 0.0, "t": time.monotonic()}
    step = total * 0.1 if total else 0.0

    def gate(done: float) -> bool:
        now = time.monotonic()
        if done >= total - 1e-6 or done - last["done"] >= step or now - last["t"] >= 30.0:
            last["done"], last["t"] = done, now
            return True
        return False

    return gate


def _log_whisper_progress(video_id: str, seg_end: float, total_duration: float, start_wall: float, gate: Any) -> None:
    elapsed_wall = max(1e-9, time.monotonic() - start_wall)
    rtf = seg_end / elapsed_wall
    log.debug(
        "%s : transcription %.1f/%.1f min (facteur temps reel %.1fx)",
        video_id, seg_end / 60.0, total_duration / 60.0, rtf,
    )
    if gate(seg_end):
        log.info(
            "%s : transcription %.1f/%.1f min (facteur temps reel %.1fx)",
            video_id, seg_end / 60.0, total_duration / 60.0, rtf,
        )


def _run_whisper(
    video_id: str,
    model_factory: Callable[[str, str, str], Any],
    audio_path: Path,
    settings: dict[str, Any],
    vocab: list[str],
    pipeline_factory: Callable[[Any], Any] = _batched_pipeline,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    device = get_device()
    options: dict[str, Any] = {
        "word_timestamps": True,
        "beam_size": settings["beam_size"],
        "vad_filter": settings["vad_filter"],
        "language": settings["language"],
    }
    runner = None
    model = model_factory(settings["model"], device.type, device.compute_type)
    try:
        prompt_vocab = _whisper_vocab(model, vocab, int(settings["vocab_max_tokens"]))
        if prompt_vocab:
            options["initial_prompt"] = ", ".join(prompt_vocab)
        batch_size = int(settings["batch_size"])
        if batch_size > 1:
            runner = pipeline_factory(model)
            raw_segments, info = runner.transcribe(str(audio_path), batch_size=batch_size, **options)
        else:
            raw_segments, info = model.transcribe(str(audio_path), **options)
        total_duration = float(info.duration or 0.0)
        gate = _progress_gate(total_duration)
        start_wall = time.monotonic()
        segments = []
        for seg in raw_segments:
            segments.append(_segment_dict(seg))
            if total_duration > 0:
                _log_whisper_progress(video_id, segments[-1]["end"], total_duration, start_wall, gate)
        header = {
            "language": info.language,
            "language_probability": info.language_probability,
            "duration": info.duration,
        }
    finally:
        raw_segments = runner = model = None
        _free_memory()
    return segments, header


def _segment_dict(seg: Any) -> dict[str, Any]:
    words = [
        {"word": w.word, "start": w.start, "end": w.end, "probability": w.probability}
        for w in (seg.words or [])
    ]
    return {"id": seg.id, "start": seg.start, "end": seg.end, "text": seg.text, "words": words}


def _chunks(segments: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    """Regroupe les segments en tranches d'environ ``size`` mots."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    count = 0
    for seg in segments:
        n = len(seg["words"])
        if current and count + n > size:
            chunks.append(current)
            current, count = [], 0
        current.append(seg)
        count += n
    if current:
        chunks.append(current)
    return chunks


def _fix_schema(n_words: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "corrections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "integer", "minimum": 0, "maximum": n_words - 1},
                        "old": {"type": "string", "minLength": 1},
                        "word": {"type": "string", "minLength": 1},
                    },
                    "required": ["i", "old", "word"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["corrections"],
        "additionalProperties": False,
    }


def _check_correction_shape(answer: dict[str, Any]) -> None:
    """Controle passe a llm.ask (comme clipper.captions) : ce que le schema ne
    peut pas exprimer, un mot corrige ne peut pas contenir d'espace interne
    (il deviendrait deux mots pour les sous-titres, sans timecode propre).
    Renvoye au modele pour reparation comme une reponse hors schema. La
    correspondance ``old`` / mot reellement present a cet index est
    controlee apres coup, tolerante a la ponctuation (_normalized_core,
    appelee depuis _fix_chunk) : ce n'est pas une raison de redemander au
    modele, une correction qui ne correspond toujours pas est ignoree et
    journalisee (ADR-ad2e). Deux corrections visant le meme index ({old,
    word} identiques) sont une repetition du modele, pas une erreur : la
    seconde est ignoree en silence par _fix_chunk (ADR-ad2e, ce n'est pas un
    refus). Si elles different (meme index, old ou word different), c'est
    contradictoire : reponse hors schema, renvoyee au modele pour reparation."""
    seen: dict[int, tuple[str, str]] = {}
    for correction in answer["corrections"]:
        new = correction["word"].strip()
        if not new or any(c.isspace() for c in new):
            raise llm.SchemaError(
                f"correction refusee pour le mot {correction['i']} : {correction['word']!r} "
                "(un mot doit rester un seul mot)"
            )
        key = (correction["old"], correction["word"])
        prior = seen.get(correction["i"])
        if prior is not None and prior != key:
            raise llm.SchemaError(
                f"corrections contradictoires pour le mot {correction['i']} : "
                f"{prior!r} puis {key!r}"
            )
        seen[correction["i"]] = key


def _word_core_bounds(word: str) -> tuple[int, int]:
    """Bornes du coeur alphanumerique d'un mot whisper : espace de tete et
    ponctuation collee (debut ou fin) exclus, ponctuation interne (ex. un
    trait d'union) gardee."""
    start = 0
    while start < len(word) and not word[start].isalnum():
        start += 1
    end = len(word)
    while end > start and not word[end - 1].isalnum():
        end -= 1
    return start, end


def _normalized_core(word: str) -> str:
    """Coeur du mot, casse ignoree, pour une comparaison tolerante a la
    ponctuation collee (``Alstner`` == ``Alstner,``)."""
    start, end = _word_core_bounds(word)
    return word[start:end].casefold()


def _apply_word_correction(original: str, new_core: str) -> str:
    """Remplace le coeur alphanumerique de ``original`` par ``new_core`` en
    gardant l'espace de tete et la ponctuation collee d'origine."""
    start, end = _word_core_bounds(original)
    return original[:start] + new_core + original[end:]


class _FixStats:
    """Compteurs de corrections partages entre les tranches (executees en
    threads, cf. _fix_chunks) : appliquees vs refusees apres la comparaison
    tolerante de _fix_chunk (_normalized_core)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.applied = 0
        self.refused = 0

    def record(self, *, applied: bool) -> None:
        with self._lock:
            if applied:
                self.applied += 1
            else:
                self.refused += 1


# Une correction refusee isolee est ignoree (ADR-ad2e) ; en dessous de ce
# compte, ce n'est jamais une "majorite" au sens de done_criteria, quel que
# soit le ratio (1 refus sur 1 tentative ne doit pas faire echouer l'etape).
_MIN_REFUSALS_FOR_MAJORITY_FAILURE = 2


def _check_refusal_ratio(stats: _FixStats) -> None:
    total = stats.applied + stats.refused
    if stats.refused >= _MIN_REFUSALS_FOR_MAJORITY_FAILURE and stats.refused * 2 > total:
        raise TranscribeError(
            f"trop de corrections refusees ({stats.refused}/{total}) : la correction "
            "semble decalee dans son ensemble (index, vocabulaire...), etape en echec (ADR-ad2e)"
        )


_refusal_log_lock = threading.Lock()


def _log_ignored_correction(log_path: Path, *, index: int, old: str, actual: str, attempted: str) -> None:
    """Journalise (llm_refusals.jsonl, meme convention que clipper.captions)
    une correction dont ``old`` ne correspond toujours pas au mot reellement
    present a cet index apres comparaison tolerante : ignoree, jamais
    appliquee en silence (ADR-ad2e)."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "usage": "transcript_fix",
        "index": index,
        "old": old,
        "actual": actual,
        "word": attempted,
        "error": (
            f"correction ignoree pour le mot {index} : le modele visait {old!r}, "
            f"le texte a cet index est {actual!r}"
        ),
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _refusal_log_lock:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)


def _fix_prefix(vocab: list[str]) -> str:
    """Bloc commun a toutes les tranches de correction d'une meme video
    (consignes + vocabulaire) : aucune donnee propre a une tranche, pour
    qu'il soit identique octet pour octet d'une tranche a l'autre et que le
    fournisseur du modele puisse relire son cache de prompt au lieu de le
    reecrire a chaque tranche (voir _fix_chunks ; meme principe que
    clipper.jury, TASK-2852)."""
    return (
        "Voici un extrait de transcription automatique, un mot par ligne, "
        "precede de son index. Corrige uniquement l'orthographe des mots mal "
        "reconnus (surtout les noms propres). Ne fusionne, ne coupe, n'ajoute "
        "et ne supprime aucun mot : une correction remplace exactement un mot "
        "par un seul mot, sans espace. Ne liste que les mots a changer, "
        "chacun avec son index, le mot original exact (old) et le mot "
        "corrige (word).\n\n"
        f"Vocabulaire attendu : {', '.join(vocab) if vocab else '(aucun)'}\n\n"
    )


# Version du gabarit de prompt (_fix_prefix) et du schema (_fix_schema) d'une
# tranche de correction : entre dans la cle de cache (_fix_cache_path). A
# augmenter si l'un des deux change de forme, pour qu'un ancien fichier
# fix_chunks/<hash>.json ne soit jamais relu comme s'il repondait au nouveau
# gabarit (TASK-db6f).
_FIX_CACHE_VERSION = 1


def _fix_cache_dir(video_dir: Path) -> Path:
    return video_dir / "fix_chunks"


def _fix_cache_path(video_dir: Path, chunk_text: str) -> Path:
    """Fichier de cache d'une tranche de correction, par le contenu reel
    envoye (texte de la tranche, independant du vocabulaire) et la version du
    gabarit/schema : deux tranches de contenu identique partagent le meme
    fichier (meme correction attendue), et un gabarit different ne retrouve
    jamais un cache ecrit par l'ancien."""
    key = hashlib.sha256(f"{_FIX_CACHE_VERSION}\n{chunk_text}".encode("utf-8")).hexdigest()
    return _fix_cache_dir(video_dir) / f"{key}.json"


def _load_cached_fix(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _store_cached_fix(path: Path, answer: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _ask_fix(prompt: str, n_words: int, config: Any, log_path: Path, timeout: float) -> dict[str, Any]:
    """Un appel transcript_fix, relance une fois tout de suite si l'erreur est
    transitoire (quota, reseau, timeout...) ; au second echec transitoire,
    comme a tout autre echec, l'exception remonte telle quelle et fait
    echouer l'etape -- jamais de texte non corrige substitue en silence
    (ADR-ad2e). ``timeout`` est propre a cet appel ([transcribe]
    fix_timeout_s), le timeout global du backend pour les autres usages
    n'est pas touche."""
    kwargs: dict[str, Any] = dict(
        config=config, check=_check_correction_shape, log_path=log_path, timeout=timeout,
    )
    try:
        return llm.ask("transcript_fix", prompt, [], _fix_schema(n_words), **kwargs)
    except llm.TransientLLMError:
        return llm.ask("transcript_fix", prompt, [], _fix_schema(n_words), **kwargs)


def _fix_chunk(
    chunk: list[dict[str, Any]], vocab: list[str], config: Any, log_path: Path, stats: _FixStats,
    video_dir: Path, timeout: float,
) -> None:
    words = [w for seg in chunk for w in seg["words"]]
    if not words:
        return
    lines = "\n".join(f"{i}\t{w['word'].strip()}" for i, w in enumerate(words))
    cache_path = _fix_cache_path(video_dir, lines)
    answer = _load_cached_fix(cache_path)
    if answer is None:
        prompt = _fix_prefix(vocab) + lines
        answer = _ask_fix(prompt, len(words), config, log_path, timeout)
        _store_cached_fix(cache_path, answer)
    seen_indexes: set[int] = set()
    for correction in answer["corrections"]:
        if correction["i"] in seen_indexes:
            continue  # doublon identique (_check_correction_shape) : deja applique, pas un refus
        seen_indexes.add(correction["i"])
        word = words[correction["i"]]
        old = correction["old"].strip()
        actual = word["word"].strip()
        # Un mot sans coeur alphanumerique (ponctuation seule : "-", "...") a un coeur normalise
        # vide ; la comparaison tolerante (faite pour Alstner == Alstner,) declarerait alors
        # n'importe quel 'old' egal (les deux coeurs sont vides), la correction serait appliquee
        # et son coeur insere apres l'ancienne ponctuation intacte ("--"/"...") au lieu de la
        # remplacer (Mineur 3, revue r-transcription) : refusee d'office, ce n'est pas un mot.
        if not _normalized_core(actual) or _normalized_core(old) != _normalized_core(actual):
            stats.record(applied=False)
            _log_ignored_correction(
                log_path, index=correction["i"], old=old, actual=actual, attempted=correction["word"]
            )
            continue
        stats.record(applied=True)
        new = correction["word"].strip()
        core_start, core_end = _word_core_bounds(new)
        new_core = new[core_start:core_end] or new
        word["word"] = _apply_word_correction(word["word"], new_core)
    for seg in chunk:
        if seg["words"]:
            seg["text"] = "".join(w["word"] for w in seg["words"])


def _fix_chunks(
    chunks: list[list[dict[str, Any]]], vocab: list[str], config: Any, parallel: int, log_path: Path,
    stats: _FixStats, video_dir: Path, timeout: float,
) -> None:
    """Corrige les tranches en 2 vagues : la premiere seule, pour que le
    fournisseur du modele mette en cache le bloc commun (_fix_prefix) qu'elle
    partage octet pour octet avec les suivantes ; puis jusqu'a ``parallel``
    tranches suivantes en meme temps (threads : les appels clipper.llm sont
    des sous-processus, pas du calcul CPU Python), qui relisent ce cache au
    lieu de le reecrire chacune (meme principe que clipper.jury, TASK-2852).
    Chaque tranche porte des segments distincts, donc les threads n'ecrivent
    jamais dans la meme structure. Une tranche en echec (schema, transitoire)
    fait echouer l'etape avec sa raison (ADR-ad2e) ; les autres deja lancees
    terminent avant que l'exception ne remonte. Une fois toutes les tranches
    traitees, une part anormale de corrections refusees (_check_refusal_ratio)
    est elle aussi un echec explicite."""
    if not chunks:
        return
    _fix_chunk(chunks[0], vocab, config, log_path, stats, video_dir, timeout)
    rest = chunks[1:]
    if rest:
        executor = ThreadPoolExecutor(max_workers=max(1, min(parallel, len(rest))))
        try:
            futures = [
                executor.submit(_fix_chunk, chunk, vocab, config, log_path, stats, video_dir, timeout)
                for chunk in rest
            ]
            for future in futures:
                future.result()
        except BaseException:
            # Premier echec definitif : les tranches pas encore lancees sont
            # annulees plutot que toutes demandees avant que l'erreur ne
            # remonte (Mineur 1, revue r-transcription).
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
    try:
        _check_refusal_ratio(stats)
    except TranscribeError:
        # Une majorite de corrections refusees indique une correction
        # decalee dans son ensemble : garder les tranches en cache les
        # rejouerait a l'identique sans jamais redemander au LLM (Important
        # 1, revue r-transcription).
        shutil.rmtree(_fix_cache_dir(video_dir), ignore_errors=True)
        raise


def transcribe(
    video_id: str,
    workspace_dir: str | Path = "workspace",
    *,
    config: Any = None,
    force: bool = False,
    model_factory: Callable[[str, str, str], Any] = _whisper_model,
    audio_extractor: Callable[[Path, Path], None] | None = None,
    pipeline_factory: Callable[[Any], Any] = _batched_pipeline,
) -> Path:
    """Transcrit workspace/<video_id>/<video_id>.mp4 dans transcript.json et
    renvoie ce chemin. Un transcript deja present n'est pas refait, sauf
    ``force``. Le resultat brut de whisper est garde dans
    transcript_raw.json avant la correction : une relance apres un echec de
    la correction reutilise ce fichier au lieu de refaire whisper."""
    video_dir = Path(workspace_dir) / video_id
    out = video_dir / "transcript.json"
    if out.exists() and not force:
        return out

    settings = _settings(config)
    _check_vocab_max_tokens(settings)
    max_word_chars = _check_hallucination_word_max_chars(settings)
    raw_path = video_dir / "transcript_raw.json"

    if raw_path.exists() and not force:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        header = {k: raw[k] for k in ("language", "language_probability", "duration")}
        model_name = raw["model"]
        vocab = raw["vocab"]
        segments = raw["segments"]
    else:
        video = video_dir / f"{video_id}.mp4"
        meta_file = video_dir / "meta.json"
        if not video.exists():
            raise TranscribeError(f"video absente : {video}")
        if not meta_file.exists():
            raise TranscribeError(f"meta.json absent : {meta_file}")
        meta = json.loads(meta_file.read_text(encoding="utf-8"))

        fix_cache_dir = _fix_cache_dir(video_dir)
        if fix_cache_dir.exists():
            shutil.rmtree(fix_cache_dir)

        vocab = _ask_vocab(meta, config) if settings["vocab"] else []

        audio = video_dir / "transcribe_audio.wav"
        if audio_extractor is None:
            audio_extractor = functools.partial(extract_audio, ffmpeg_bin=str(settings["ffmpeg_bin"]))
        try:
            audio_extractor(video, audio)
            segments, header = _run_whisper(video_id, model_factory, audio, settings, vocab, pipeline_factory)
        finally:
            audio.unlink(missing_ok=True)

        model_name = settings["model"]
        raw = {"video_id": video_id, **header, "model": model_name, "vocab": vocab, "segments": segments}
        tmp_raw = raw_path.with_suffix(".json.tmp")
        tmp_raw.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_raw.replace(raw_path)

    hallucinated = _drop_hallucinated_words(segments, max_word_chars)

    fix_stats = _FixStats()
    if settings["transcript_fix"]:
        chunks = _chunks(segments, int(settings["fix_chunk_words"]))
        log_path = video_dir / "llm_refusals.jsonl"
        _fix_chunks(
            chunks, vocab, config, int(settings["fix_parallel"]), log_path, fix_stats,
            video_dir, float(settings["fix_timeout_s"]),
        )

    transcript = {
        "video_id": video_id,
        **header,
        "model": model_name,
        "vocab": vocab,
        "segments": segments,
        "transcript_fix_refused": fix_stats.refused,
        "hallucinated_words_removed": hallucinated,
    }
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    return out
