from __future__ import annotations

import gc
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import wave
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipper import llm
from clipper.config import Config
from clipper.gpu import Device
from clipper.llm.fake import FakeBackend

VIDEO_ID = "abcdefghijk"


# --------------------------------------------------------------------------
# Fakes : un WhisperModel factice qui imite faster_whisper (segments rendus
# par un generateur qui garde une reference au modele, comme le vrai).
# --------------------------------------------------------------------------


def _word(word, start, end, probability=0.9):
    return SimpleNamespace(word=word, start=start, end=end, probability=probability)


def _segment(id_, words):
    return SimpleNamespace(
        id=id_,
        start=words[0].start,
        end=words[-1].end,
        text="".join(w.word for w in words),
        words=words,
    )


def default_segments():
    return [
        _segment(1, [_word(" Salut", 0.0, 0.4, 0.95), _word(" Rokstar", 0.4, 0.9, 0.41)]),
        _segment(2, [_word(" GTA", 1.2, 1.5, 0.88), _word(" six", 1.5, 1.9, 0.7), _word(" arrive.", 1.9, 2.5, 0.93)]),
    ]


class FakeHfTokenizer:
    """Imite tokenizers.Tokenizer (attribut hf_tokenizer de WhisperModel) :
    un token par mot et par signe de ponctuation."""

    def encode(self, text, add_special_tokens=True):
        return SimpleNamespace(ids=[hash(t) % 50000 for t in re.findall(r"\w+|[^\w\s]", text)])


class FakeWhisperModel:
    def __init__(self, name, device, compute_type, segments, language="fr"):
        self.name = name
        self.device = device
        self.compute_type = compute_type
        self._segments = segments
        self._language = language
        self.transcribe_calls = []
        self.hf_tokenizer = FakeHfTokenizer()

    def transcribe(self, audio, **kwargs):
        self.transcribe_calls.append((audio, kwargs))
        model = self

        def gen():
            for seg in model._segments:
                yield seg

        info = SimpleNamespace(language=self._language, language_probability=0.97, duration=2.5)
        return gen(), info


class ModelFactory:
    """Records how the model was built and keeps only a weakref to it, so a
    test can tell whether the step released it."""

    def __init__(self, segments=None, language="fr", on_transcribe=None):
        self.segments = segments if segments is not None else default_segments()
        self.language = language
        self.on_transcribe = on_transcribe
        self.built = []  # (name, device, compute_type)
        self.ref = None
        self.audio_seen = None
        self.kwargs_seen = None

    def __call__(self, name, device, compute_type):
        self.built.append((name, device, compute_type))
        model = FakeWhisperModel(name, device, compute_type, self.segments, self.language)
        factory = self
        original = model.transcribe

        def transcribe(audio, **kwargs):
            factory.audio_seen = audio
            factory.kwargs_seen = kwargs
            if factory.on_transcribe is not None:
                factory.on_transcribe(audio)
            return original(audio, **kwargs)

        model.transcribe = transcribe
        self.ref = weakref.ref(model)
        return model

    def alive(self):
        gc.collect()
        return self.ref is not None and self.ref() is not None


def fake_extractor(video_path, audio_path):
    Path(audio_path).write_bytes(b"RIFF fake wav")


class FakePipeline:
    """Imite BatchedInferencePipeline (le vrai gere le batching en interne ;
    ici on verifie seulement le branchement et le batch_size transmis) :
    delegue tel quel au transcribe() du modele qu'elle enveloppe."""

    def __init__(self, model):
        self.model = model

    def transcribe(self, audio, **kwargs):
        return self.model.transcribe(audio, **kwargs)


def make_config(tmp_path, **transcribe):
    return Config(
        mode="review",
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "output",
        _sections={"transcribe": transcribe} if transcribe else {},
    )


@pytest.fixture
def video_dir(tmp_path):
    d = tmp_path / "workspace" / VIDEO_ID
    d.mkdir(parents=True)
    (d / f"{VIDEO_ID}.mp4").write_bytes(b"fake video")
    meta = {
        "video_id": VIDEO_ID,
        "title": "GTA 6 : tout sur le trailer de Rockstar",
        "description": "On parle de Vice City, Lucia et Jason avec Rockstar Games.",
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


@pytest.fixture
def cpu(monkeypatch):
    import clipper.transcribe as t

    monkeypatch.setattr(t, "get_device", lambda: Device(type="cpu", compute_type="int8"))


VOCAB = {"words": ["Rockstar", "Vice City", "Lucia", "Jason"]}
NO_FIX = {"corrections": []}


def run(tmp_path, factory, config=None, **kwargs):
    from clipper.transcribe import transcribe

    config = config or make_config(tmp_path)
    kwargs.setdefault("pipeline_factory", FakePipeline)
    return transcribe(
        VIDEO_ID,
        tmp_path / "workspace",
        config=config,
        model_factory=factory,
        audio_extractor=kwargs.pop("audio_extractor", fake_extractor),
        **kwargs,
    )


def read_transcript(video_dir):
    return json.loads((video_dir / "transcript.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# C2 : transcript.json, langue, segments, mots
# --------------------------------------------------------------------------


def test_writes_transcript_json_with_language_segments_and_words(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory)

    data = read_transcript(video_dir)
    assert data["language"] == "fr"
    assert [(s["start"], s["end"]) for s in data["segments"]] == [(0.0, 0.9), (1.2, 2.5)]
    assert data["segments"][0]["words"] == [
        {"word": " Salut", "start": 0.0, "end": 0.4, "probability": 0.95},
        {"word": " Rokstar", "start": 0.4, "end": 0.9, "probability": 0.41},
    ]
    assert data["segments"][1]["text"] == " GTA six arrive."
    assert factory.kwargs_seen["word_timestamps"] is True


def test_returns_the_transcript_path(tmp_path, video_dir, cpu):
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        path = run(tmp_path, ModelFactory())
    assert Path(path) == video_dir / "transcript.json"


# --------------------------------------------------------------------------
# C1 : extraction de l'audio (vrai ffmpeg)
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent du PATH")
def test_extracts_16k_mono_wav_with_ffmpeg_and_feeds_it_to_whisper(tmp_path, video_dir, cpu):
    from clipper.transcribe import extract_audio

    video = video_dir / f"{VIDEO_ID}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1",
         "-ac", "2", "-shortest", str(video)],
        check=True,
    )
    seen = {}

    def inspect(audio):
        with wave.open(str(audio), "rb") as w:
            seen["channels"] = w.getnchannels()
            seen["rate"] = w.getframerate()
            seen["seconds"] = w.getnframes() / w.getframerate()

    factory = ModelFactory(on_transcribe=inspect)
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory, audio_extractor=extract_audio)

    assert seen["channels"] == 1
    assert seen["rate"] == 16000
    assert 0.9 <= seen["seconds"] <= 1.1


def test_missing_video_is_an_error(tmp_path, video_dir, cpu):
    from clipper.transcribe import TranscribeError

    (video_dir / f"{VIDEO_ID}.mp4").unlink()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])), pytest.raises(TranscribeError):
        run(tmp_path, ModelFactory())


# --------------------------------------------------------------------------
# C3 : modele depuis la config, device depuis clipper.gpu
# --------------------------------------------------------------------------


def test_model_name_from_config_and_device_from_gpu(tmp_path, video_dir, monkeypatch):
    import clipper.transcribe as t

    monkeypatch.setattr(t, "get_device", lambda: Device(type="cuda", compute_type="float16"))
    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory, config=make_config(tmp_path, model="large-v3"))
    assert factory.built == [("large-v3", "cuda", "float16")]


def test_default_model_comes_from_config_defaults(tmp_path, video_dir, cpu):
    from clipper.transcribe import CONFIG_DEFAULTS

    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory)
    assert factory.built == [(CONFIG_DEFAULTS["model"], "cpu", "int8")]


def test_whisper_options_come_from_config(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory, config=make_config(tmp_path, language="fr", beam_size=2, vad_filter=False))
    assert factory.kwargs_seen["language"] == "fr"
    assert factory.kwargs_seen["beam_size"] == 2
    assert factory.kwargs_seen["vad_filter"] is False


# --------------------------------------------------------------------------
# C4 : le modele est libere en fin d'etape (et avant l'appel LLM de correction,
# qui peut etre un LLM local : jamais deux modeles lourds a la fois)
# --------------------------------------------------------------------------


def test_model_released_after_the_step(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory)
    assert not factory.alive()


def test_model_released_before_transcript_fix_call(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    alive_during_fix = []

    def fix(request):
        alive_during_fix.append(factory.alive())
        return NO_FIX

    with llm.use_backend(FakeBackend([VOCAB, fix])):
        run(tmp_path, factory)
    assert alive_during_fix == [False]


def test_model_released_when_transcription_fails(tmp_path, video_dir, cpu):
    def boom(audio):
        raise RuntimeError("whisper a plante")

    factory = ModelFactory(on_transcribe=boom)
    with llm.use_backend(FakeBackend([VOCAB])), pytest.raises(RuntimeError):
        run(tmp_path, factory)
    assert not factory.alive()


# --------------------------------------------------------------------------
# C5 : vocabulaire demande a clipper.llm (usage vocab) depuis titre et
# description, passe en initial_prompt seulement (plus de hotwords, TASK-913b)
# --------------------------------------------------------------------------


def test_vocab_asked_from_title_and_description_and_passed_to_whisper(tmp_path, video_dir, cpu):
    fake = FakeBackend([VOCAB, NO_FIX])
    factory = ModelFactory()
    with llm.use_backend(fake):
        run(tmp_path, factory)

    vocab_call = fake.calls[0]
    assert vocab_call.usage == "vocab"
    assert "GTA 6 : tout sur le trailer de Rockstar" in vocab_call.prompt
    assert "On parle de Vice City, Lucia et Jason avec Rockstar Games." in vocab_call.prompt
    assert vocab_call.images == []
    for name in ("Rockstar", "Vice City", "Lucia", "Jason"):
        assert name in factory.kwargs_seen["initial_prompt"]
    assert "hotwords" not in factory.kwargs_seen
    assert read_transcript(video_dir)["vocab"] == ["Rockstar", "Vice City", "Lucia", "Jason"]


def test_vocab_asked_before_the_model_is_loaded(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    built_at_vocab = []

    def vocab(request):
        built_at_vocab.append(len(factory.built))
        return VOCAB

    with llm.use_backend(FakeBackend([vocab, NO_FIX])):
        run(tmp_path, factory)
    assert built_at_vocab == [0]


def test_invalid_vocab_answer_is_a_failure_and_writes_nothing(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    with llm.use_backend(FakeBackend([{"mots": "pas le bon schema"}])), pytest.raises(llm.SchemaError):
        run(tmp_path, factory)
    assert not (video_dir / "transcript.json").exists()
    assert factory.built == []


def test_vocab_disabled_explicitly_in_config_skips_the_call(tmp_path, video_dir, cpu):
    fake = FakeBackend([NO_FIX])
    factory = ModelFactory()
    with llm.use_backend(fake):
        run(tmp_path, factory, config=make_config(tmp_path, vocab=False))
    assert [c.usage for c in fake.calls] == ["transcript_fix"]
    assert "initial_prompt" not in factory.kwargs_seen
    assert "hotwords" not in factory.kwargs_seen


# --------------------------------------------------------------------------
# TASK-b20f : le prompt passe a whisper ne depasse jamais la fenetre du
# decodeur (448 positions), quelle que soit la taille du vocabulaire. Avant
# le retrait de hotwords (TASK-913b), un vocabulaire long donnait 1 (sot_prev)
# + 223 (hotwords tronques) + 223 (initial_prompt tronque) + 3 (sot_sequence)
# = 450 > 448, RuntimeError "No position encodings are defined for positions
# >= 448" ; sans hotwords, seul initial_prompt occupe encore ce budget.
# --------------------------------------------------------------------------

WHISPER_POSITIONS = 448


def whisper_prompt_length(previous_tokens):
    """Longueur du prompt que faster-whisper 1.2.1 (WhisperModel.get_prompt)
    construit pour une fenetre : [sot_prev] + previous_tokens (223 derniers,
    initial_prompt pour la premiere fenetre puis texte deja transcrit) +
    sot_sequence (sot, langue, tache)."""
    half = WHISPER_POSITIONS // 2
    length = 0
    if previous_tokens:
        length += 1
        length += min(previous_tokens, half - 1)
    return length + 3


class WindowCheckingFactory(ModelFactory):
    """Modele factice qui echoue comme le vrai decodeur Whisper quand le
    prompt d'une fenetre ne laisse plus de position pour generer : premiere
    fenetre conditionnee par initial_prompt, suivantes par le texte deja
    transcrit (223 tokens sur une longue video)."""

    def __call__(self, name, device, compute_type):
        model = super().__call__(name, device, compute_type)
        inner = model.transcribe
        factory = self

        def transcribe(audio, **kwargs):
            initial = kwargs.get("initial_prompt")
            first = len(FakeHfTokenizer().encode(" " + initial.strip()).ids) if initial else 0
            factory.prompt_lengths = [
                whisper_prompt_length(first),
                whisper_prompt_length(WHISPER_POSITIONS // 2 - 1),
            ]
            for length in factory.prompt_lengths:
                if length >= WHISPER_POSITIONS:
                    raise RuntimeError(
                        "No position encodings are defined for positions >= 448, "
                        f"but got position {WHISPER_POSITIONS}"
                    )
            return inner(audio, **kwargs)

        model.transcribe = transcribe
        return model


def credits_vocab(n):
    """Un vocabulaire comme celui d'ivl0nxa3C7o : n noms propres de credits."""
    return {"words": [f"Prenom{i} NOM{i}-COMPOSE{i}" for i in range(n)]}


def test_long_vocab_never_overflows_the_whisper_decoder_window(tmp_path, video_dir, cpu):
    factory = WindowCheckingFactory()
    with llm.use_backend(FakeBackend([credits_vocab(58), NO_FIX])):
        run(tmp_path, factory)
    assert read_transcript(video_dir)["segments"]


@pytest.mark.parametrize("n", [0, 1, 20, 58, 100])
def test_prompt_fits_the_window_with_room_to_generate_whatever_the_vocab_size(tmp_path, video_dir, cpu, n):
    from clipper.transcribe import CONFIG_DEFAULTS

    factory = WindowCheckingFactory()
    with llm.use_backend(FakeBackend([credits_vocab(n), NO_FIX])):
        run(tmp_path, factory)
    tokenizer = FakeHfTokenizer()
    text = factory.kwargs_seen.get("initial_prompt") or ""
    assert len(tokenizer.encode(" " + text.strip()).ids) <= CONFIG_DEFAULTS["vocab_max_tokens"]
    assert "hotwords" not in factory.kwargs_seen
    # au moins une centaine de positions restent pour la transcription
    assert max(factory.prompt_lengths) <= WHISPER_POSITIONS - 100


def test_vocab_max_tokens_comes_from_config(tmp_path, video_dir, cpu):
    factory = WindowCheckingFactory()
    with llm.use_backend(FakeBackend([credits_vocab(58), NO_FIX])):
        run(tmp_path, factory, config=make_config(tmp_path, vocab_max_tokens=10))
    initial_prompt = factory.kwargs_seen["initial_prompt"]
    assert len(FakeHfTokenizer().encode(" " + initial_prompt).ids) <= 10
    assert initial_prompt == "Prenom0 NOM0-COMPOSE0, Prenom1 NOM1-COMPOSE1"
    assert "hotwords" not in factory.kwargs_seen


def test_shortened_vocab_is_logged_with_the_dropped_entries(tmp_path, video_dir, cpu, caplog):
    factory = WindowCheckingFactory()
    with caplog.at_level(logging.WARNING, logger="clipper.transcribe"):
        with llm.use_backend(FakeBackend([credits_vocab(58), NO_FIX])):
            run(tmp_path, factory, config=make_config(tmp_path, vocab_max_tokens=10))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0]
    assert "vocab_max_tokens" in message and "10" in message
    # les entrees ecartees sont nommees
    assert "Prenom2 NOM2-COMPOSE2" in message and "Prenom57 NOM57-COMPOSE57" in message


def test_short_vocab_is_passed_whole_without_warning(tmp_path, video_dir, cpu, caplog):
    factory = WindowCheckingFactory()
    with caplog.at_level(logging.WARNING, logger="clipper.transcribe"):
        with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
            run(tmp_path, factory)
    assert factory.kwargs_seen["initial_prompt"] == "Rockstar, Vice City, Lucia, Jason"
    assert "hotwords" not in factory.kwargs_seen
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_model_without_tokenizer_bounds_the_prompt_by_utf8_bytes_and_says_so(
    tmp_path, video_dir, cpu, caplog
):
    """Whisper tokenise en BPE sur les octets : un token couvre au moins un
    octet, donc le nombre d'octets UTF-8 majore le nombre de tokens. Un
    modele qui n'expose pas hf_tokenizer reste borne, et c'est journalise."""
    class NoTokenizerFactory(WindowCheckingFactory):
        def __call__(self, name, device, compute_type):
            model = super().__call__(name, device, compute_type)
            del model.hf_tokenizer
            return model

    factory = NoTokenizerFactory()
    with caplog.at_level(logging.WARNING, logger="clipper.transcribe"):
        with llm.use_backend(FakeBackend([{"words": ["Éric", "Zoé Lefèvre", "Anaïs"]}, NO_FIX])):
            run(tmp_path, factory, config=make_config(tmp_path, vocab_max_tokens=18))
    # " Éric, Zoé Lefèvre" = 21 octets > 18 ; " Éric Zoé Lefèvre" = 20 > 18
    assert factory.kwargs_seen["initial_prompt"] == "Éric"
    assert "hotwords" not in factory.kwargs_seen
    messages = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "hf_tokenizer" in messages
    assert "Zoé Lefèvre" in messages and "Anaïs" in messages


def test_full_vocab_is_kept_in_transcript_and_for_the_correction(tmp_path, video_dir, cpu):
    """Seul le prompt de whisper est borne : la correction par clipper.llm et
    transcript.json gardent tout le vocabulaire."""
    vocab = credits_vocab(58)
    fake = FakeBackend([vocab, NO_FIX])
    with llm.use_backend(fake):
        run(tmp_path, WindowCheckingFactory())
    assert read_transcript(video_dir)["vocab"] == vocab["words"]
    assert "Prenom57 NOM57-COMPOSE57" in fake.calls[1].prompt


@pytest.mark.parametrize("bound", [0, 121, 400])
def test_vocab_max_tokens_outside_the_window_is_refused_before_loading_the_model(
    tmp_path, video_dir, cpu, bound
):
    from clipper.transcribe import TranscribeError

    factory = WindowCheckingFactory()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])), pytest.raises(TranscribeError, match="vocab_max_tokens"):
        run(tmp_path, factory, config=make_config(tmp_path, vocab_max_tokens=bound))
    assert factory.built == []
    assert not (video_dir / "transcript.json").exists()


# --------------------------------------------------------------------------
# C6 : correction (usage transcript_fix) : texte des mots seulement
# --------------------------------------------------------------------------


def test_fix_changes_word_text_only(tmp_path, video_dir, cpu):
    fake = FakeBackend([
        VOCAB,
        {"corrections": [
            {"i": 1, "old": "Rokstar", "word": "Rockstar"},
            {"i": 3, "old": "six", "word": "6"},
        ]},
    ])
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory())

    fix_call = fake.calls[1]
    assert fix_call.usage == "transcript_fix"
    assert "Rokstar" in fix_call.prompt
    assert "Vice City" in fix_call.prompt  # le vocabulaire guide la correction

    data = read_transcript(video_dir)
    assert data["segments"] == [
        {
            "id": 1, "start": 0.0, "end": 0.9, "text": " Salut Rockstar",
            "words": [
                {"word": " Salut", "start": 0.0, "end": 0.4, "probability": 0.95},
                {"word": " Rockstar", "start": 0.4, "end": 0.9, "probability": 0.41},
            ],
        },
        {
            "id": 2, "start": 1.2, "end": 2.5, "text": " GTA 6 arrive.",
            "words": [
                {"word": " GTA", "start": 1.2, "end": 1.5, "probability": 0.88},
                {"word": " 6", "start": 1.5, "end": 1.9, "probability": 0.7},
                {"word": " arrive.", "start": 1.9, "end": 2.5, "probability": 0.93},
            ],
        },
    ]


def test_fix_prompt_is_compact_lines_not_json_segments_or_timestamps(tmp_path, video_dir, cpu):
    """Le texte envoye par tranche est un mot par ligne (index<TAB>mot), pas
    le JSON complet des segments ni leurs horodatages (cout de l'entree,
    TASK-2cac)."""
    fake = FakeBackend([VOCAB, {"corrections": []}])
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory())

    fix_call = fake.calls[1]
    assert "0\tSalut" in fix_call.prompt
    assert "1\tRokstar" in fix_call.prompt
    for verbose in ('"start"', '"end"', '"segments"', '"words"', '"probability"'):
        assert verbose not in fix_call.prompt


def test_fix_cannot_change_timecodes_or_word_count(tmp_path, video_dir, cpu):
    """Une reponse qui tente d'ajouter des champs (timecodes) ou de viser un
    mot inexistant est un echec, pas une donnee."""
    for bad in (
        {"corrections": [{"i": 1, "old": "Rokstar", "word": "Rockstar", "start": 9.9}]},
        {"corrections": [{"i": 5, "old": "en", "word": "en trop"}]},
        {"corrections": [{"i": -1, "old": "avant", "word": "avant"}]},
        {"corrections": [{"i": 1, "word": "Rockstar"}]},  # old manquant
        {"words": [" Salut", " Rockstar", " Games"]},
    ):
        (video_dir / "transcript.json").unlink(missing_ok=True)
        with llm.use_backend(FakeBackend([VOCAB, bad])), pytest.raises(llm.SchemaError):
            run(tmp_path, ModelFactory())
        assert not (video_dir / "transcript.json").exists()


def test_fix_word_that_would_merge_words_is_refused(tmp_path, video_dir, cpu):
    """Un mot corrige ne peut pas contenir d'espace interne : sinon il
    deviendrait deux mots pour les sous-titres sans timecode propre."""
    bad = {"corrections": [{"i": 0, "old": "Salut", "word": "Salut les"}]}
    with llm.use_backend(FakeBackend([VOCAB, bad])):
        with pytest.raises(llm.SchemaError):
            run(tmp_path, ModelFactory())
    assert not (video_dir / "transcript.json").exists()


def test_fix_correction_whose_old_does_not_match_the_actual_word_is_ignored_and_logged(
    tmp_path, video_dir, cpu
):
    """Une correction visant un mot different de celui reellement present a
    cet index (LLM decale, index hallucine...) ne correspond pas au texte,
    meme apres comparaison tolerante a la ponctuation : ignoree, jamais
    appliquee en silence, journalisee dans llm_refusals.jsonl (meme
    convention que captions.py), et l'etape reussit quand meme (ADR-ad2e) :
    une correction sans rapport, isolee, n'est pas une "majorite" de
    corrections refusees."""
    bad = {"corrections": [{"i": 1, "old": "Rockstar", "word": "Rockstars"}]}  # le mot 1 est " Rokstar"
    with llm.use_backend(FakeBackend([VOCAB, bad])):
        run(tmp_path, ModelFactory())

    data = read_transcript(video_dir)
    assert data["segments"][0]["words"][1]["word"] == " Rokstar"  # pas touche
    assert data["transcript_fix_refused"] == 1

    refusals_path = video_dir / "llm_refusals.jsonl"
    assert refusals_path.exists()
    entries = [json.loads(line) for line in refusals_path.read_text(encoding="utf-8").splitlines()]
    assert any(
        e["usage"] == "transcript_fix" and "Rokstar" in e["error"] and "Rockstar" in e["error"]
        for e in entries
    )


def test_fix_correction_matching_ignoring_attached_punctuation_and_case_is_applied(
    tmp_path, video_dir, cpu
):
    """``old`` compare au mot reellement present en ignorant la ponctuation
    collee et la casse : la correction s'applique en gardant la ponctuation
    d'origine autour du mot corrige (issue reelle 7VaA8XUKrAY, TASK-59e1)."""
    segments = [_segment(1, [_word(" Alstner,", 0.0, 0.4)])]
    fake = FakeBackend([VOCAB, {"corrections": [{"i": 0, "old": "alstner", "word": "Alstner"}]}])
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory(segments=segments))

    data = read_transcript(video_dir)
    assert data["segments"][0]["words"][0]["word"] == " Alstner,"
    assert data["transcript_fix_refused"] == 0


def test_fix_correction_carrying_attached_punctuation_does_not_double_it(tmp_path, video_dir, cpu):
    """Le prompt montre le mot avec sa ponctuation collee (_fix_chunk,
    ``f"{i}\\t{w['word'].strip()}"``) : le LLM renvoie donc naturellement
    ``word`` lui-meme ponctue (``"Alstner,"``). _apply_word_correction garde
    deja la ponctuation d'origine autour du mot : appliquer ``word`` sans en
    retirer son propre coeur double la ponctuation (Important 2, revue
    r-transcription)."""
    segments = [_segment(1, [_word(" Alsner,", 0.0, 0.4)])]
    fake = FakeBackend([VOCAB, {"corrections": [{"i": 0, "old": "Alsner,", "word": "Alstner,"}]}])
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory(segments=segments))

    data = read_transcript(video_dir)
    assert data["segments"][0]["words"][0]["word"] == " Alstner,"
    assert data["transcript_fix_refused"] == 0


def test_fix_correction_on_a_punctuation_only_word_is_refused_not_doubled(tmp_path, video_dir, cpu):
    """Un mot sans aucun caractere alphanumerique (" -", " ...") a un coeur normalise vide : la
    comparaison tolerante a la ponctuation (faite pour Alstner == Alstner,) declarerait alors
    n'importe quel ``old`` egal (les deux coeurs sont vides), la correction serait appliquee et son
    coeur insere apres l'ancienne ponctuation intacte au lieu de la remplacer, doublant la
    ponctuation (" -" corrige en " --" au lieu de " —", Mineur 3, revue r-transcription)."""
    segments = [_segment(1, [_word(" -", 0.0, 0.4)])]
    fake = FakeBackend([VOCAB, {"corrections": [{"i": 0, "old": "-", "word": "—"}]}])
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory(segments=segments))

    data = read_transcript(video_dir)
    assert data["segments"][0]["words"][0]["word"] == " -"  # pas touche, jamais double
    assert data["transcript_fix_refused"] == 1


def test_fix_majority_refused_chunks_are_not_cached_a_relaunch_asks_the_llm_again(
    tmp_path, video_dir, cpu
):
    """Une tranche dont les corrections sont majoritairement refusees
    (etape en echec explicite) ne doit jamais rester en cache : sinon une
    relance (sans force) relit le cache et echoue a l'identique, sans jamais
    redemander au LLM -- meme si celui-ci repondrait bien cette fois
    (Important 1, revue r-transcription)."""
    from clipper.transcribe import TranscribeError

    segments = _many_word_segments(4)  # fix_chunk_words=1 -> 4 tranches d'un mot

    def refuse_all(request):
        return {"corrections": [{"i": 0, "old": "mot-inexistant", "word": "X"}]}

    config = make_config(tmp_path, fix_chunk_words=1)
    fake = FakeBackend([VOCAB, refuse_all, refuse_all, refuse_all, refuse_all])
    with llm.use_backend(fake), pytest.raises(TranscribeError, match="corrections refusees"):
        run(tmp_path, ModelFactory(segments=segments), config=config)

    # 2e passage (relance sans force) : le LLM repondrait bien cette fois --
    # le cache ne doit pas rejouer les anciens refus a l'identique.
    retry = FakeBackend([{"corrections": []}] * 4)
    with llm.use_backend(retry):
        run(tmp_path, ModelFactory(segments=segments), config=config)
    assert len(retry.calls) == 4  # chaque tranche redemandee, aucune ne vient du cache
    assert read_transcript(video_dir)["transcript_fix_refused"] == 0


def test_fix_duplicate_identical_correction_is_ignored_without_counting_as_refused(
    tmp_path, video_dir, cpu
):
    """La deuxieme occurrence d'une correction identique ({old, word} memes)
    visant le meme index n'est qu'une repetition du modele, pas un refus :
    ``actual`` ne correspond plus apres la premiere application, ce qui la
    faisait compter comme refusee a tort (Mineur 4, revue r-transcription)."""
    segments = [_segment(1, [_word(" Alsner", 0.0, 0.4)])]
    dup = {"i": 0, "old": "Alsner", "word": "Alstner"}
    fake = FakeBackend([VOCAB, {"corrections": [dup, dup]}])
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory(segments=segments))

    data = read_transcript(video_dir)
    assert data["transcript_fix_refused"] == 0
    assert data["segments"][0]["words"][0]["word"] == " Alstner"


def test_fix_contradictory_duplicate_correction_is_a_schema_error(tmp_path, video_dir, cpu):
    """Deux corrections visant le meme index mais qui different (``word``
    different pour un meme ``old``) ne sont pas une repetition silencieuse :
    c'est contradictoire, donc une reponse hors schema (Mineur 4, revue
    r-transcription)."""
    segments = [_segment(1, [_word(" Alsner", 0.0, 0.4)])]
    answer = {"corrections": [
        {"i": 0, "old": "Alsner", "word": "Alstner"},
        {"i": 0, "old": "Alsner", "word": "Alston"},
    ]}
    with llm.use_backend(FakeBackend([VOCAB, answer])):
        with pytest.raises(llm.SchemaError):
            run(tmp_path, ModelFactory(segments=segments))
    assert not (video_dir / "transcript.json").exists()


def test_fix_majority_of_corrections_refused_is_an_explicit_failure(tmp_path, video_dir, cpu):
    """Si une part anormale des corrections d'une video est refusee (ici 3
    sur 4, une majorite d'au moins 2), la correction semble decalee dans son
    ensemble : erreur explicite, pas d'ignorer-et-continuer (ADR-ad2e). Les 4
    tranches partant en parallele (fix_parallel par defaut), la reponse est
    calculee depuis le contenu de sa propre requete plutot que depuis l'ordre
    d'arrivee (comme test_fix_parallel_result_matches_sequential_processing_
    regardless_of_scheduling)."""
    from clipper.transcribe import TranscribeError

    segments = _many_word_segments(4)  # fix_chunk_words=1 -> 4 tranches d'un mot

    def respond(request):
        line = next(l for l in request.prompt.splitlines() if "\t" in l)
        _, word_text = line.split("\t", 1)
        if word_text == "mot1":
            return {"corrections": [{"i": 0, "old": word_text, "word": "UN"}]}  # correspond, appliquee
        return {"corrections": [{"i": 0, "old": "mot-inexistant", "word": "X"}]}  # sans rapport, refusee

    fake = FakeBackend([VOCAB, respond, respond, respond, respond])
    with llm.use_backend(fake):
        with pytest.raises(TranscribeError, match="corrections refusees"):
            run(tmp_path, ModelFactory(segments=segments), config=make_config(tmp_path, fix_chunk_words=1))
    assert not (video_dir / "transcript.json").exists()


def test_fix_disabled_explicitly_in_config_skips_the_call(tmp_path, video_dir, cpu):
    fake = FakeBackend([VOCAB])
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory(), config=make_config(tmp_path, transcript_fix=False))
    assert [c.usage for c in fake.calls] == ["vocab"]
    assert read_transcript(video_dir)["segments"][0]["words"][1]["word"] == " Rokstar"


# --------------------------------------------------------------------------
# C7 : correction par tranches sur les longues videos
# --------------------------------------------------------------------------


def test_fix_runs_in_chunks_with_global_word_indexes_mapped_per_chunk(tmp_path, video_dir, cpu):
    segments = [
        _segment(i, [_word(f" mot{i}a", i * 1.0, i * 1.0 + 0.4), _word(f" mot{i}b", i * 1.0 + 0.4, i * 1.0 + 0.9)])
        for i in range(5)
    ]  # 10 mots, 2 par segment
    answers = [
        {"corrections": [{"i": 0, "old": "mot0a", "word": "UN"}]},     # tranche 1 : mots 0-3
        {"corrections": [{"i": 1, "old": "mot2b", "word": "DEUX"}]},   # tranche 2 : mots 4-7 -> mot 5
        {"corrections": [{"i": 1, "old": "mot4b", "word": "TROIS"}]},  # tranche 3 : mots 8-9 -> mot 9
    ]
    fake = FakeBackend([VOCAB, *answers])
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory(segments=segments), config=make_config(tmp_path, fix_chunk_words=4))

    assert [c.usage for c in fake.calls] == ["vocab"] + ["transcript_fix"] * 3
    words = [w["word"] for s in read_transcript(video_dir)["segments"] for w in s["words"]]
    assert words == [" UN", " mot0b", " mot1a", " mot1b", " mot2a", " DEUX", " mot3a", " mot3b", " mot4a", " TROIS"]
    # une tranche ne coupe jamais un segment
    assert "mot2a" in fake.calls[2].prompt and "mot2b" in fake.calls[2].prompt
    assert "mot2a" not in fake.calls[1].prompt


def test_transient_llm_error_during_fix_propagates_without_writing(tmp_path, video_dir, cpu):
    with llm.use_backend(FakeBackend([VOCAB, llm.TransientLLMError("quota")])):
        with pytest.raises(llm.TransientLLMError):
            run(tmp_path, ModelFactory())
    assert not (video_dir / "transcript.json").exists()


# --------------------------------------------------------------------------
# C12 : tranches de correction traitees en parallele (fix_parallel)
# --------------------------------------------------------------------------


def _many_word_segments(n):
    return [
        _segment(i, [_word(f" mot{i}", i * 1.0, i * 1.0 + 0.4)])
        for i in range(n)
    ]


class ConcurrencyTracker:
    """Mesure le chevauchement reel de plusieurs appels concurrents. Chaque
    appel bloque jusqu'a ce que ``expected`` appels soient simultanement
    actifs (ou un timeout genereux), au lieu d'un sleep fixe qui peut rater
    la fenetre de recouvrement sous forte charge CPU partagee -- le
    scheduling des threads n'est alors plus garanti dans un court delai fixe
    (cf. TASK-42a46cb23f78)."""

    def __init__(self, expected, timeout=10.0):
        self.lock = threading.Lock()
        self.current = 0
        self.peak = 0
        self._expected = expected
        self._reached = threading.Event()
        self._timeout = timeout

    def response(self, request):
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
            if self.current >= self._expected:
                self._reached.set()
        self._reached.wait(self._timeout)
        with self.lock:
            self.current -= 1
        return {"corrections": []}


def test_fix_chunks_run_concurrently_up_to_fix_parallel(tmp_path, video_dir, cpu):
    segments = _many_word_segments(12)  # fix_chunk_words=2 -> 6 tranches
    tracker = ConcurrencyTracker(expected=3)
    fake = FakeBackend([VOCAB] + [tracker.response] * 6)
    with llm.use_backend(fake):
        run(
            tmp_path,
            ModelFactory(segments=segments),
            config=make_config(tmp_path, fix_chunk_words=2, fix_parallel=3),
        )
    assert tracker.peak == 3


def test_fix_parallel_defaults_to_config_value_of_four(tmp_path, video_dir, cpu):
    segments = _many_word_segments(16)  # fix_chunk_words=2 -> 8 tranches
    tracker = ConcurrencyTracker(expected=4)
    fake = FakeBackend([VOCAB] + [tracker.response] * 8)
    with llm.use_backend(fake):
        run(
            tmp_path,
            ModelFactory(segments=segments),
            config=make_config(tmp_path, fix_chunk_words=2),
        )
    assert tracker.peak == 4


def test_fix_parallel_result_matches_sequential_processing_regardless_of_scheduling(
    tmp_path, video_dir, cpu
):
    """Chaque tranche est corrigee d'apres son propre contenu (pas d'apres
    l'ordre d'arrivee des reponses scriptees) : le resultat final est le
    meme que le traitement sequentiel, quel que soit l'ordre d'execution des
    threads."""
    segments = _many_word_segments(10)  # fix_chunk_words=2 -> 5 tranches

    def make_fix(chunk_index):
        def respond(request):
            time.sleep(0.01 * (5 - chunk_index))  # ordre d'arrivee inverse
            assert f"mot{chunk_index * 2}" in request.prompt
            return {"corrections": [{"i": 0, "old": f"mot{chunk_index * 2}", "word": f"CORRIGE{chunk_index}"}]}

        return respond

    fake = FakeBackend([VOCAB, *(make_fix(i) for i in range(5))])
    with llm.use_backend(fake):
        run(
            tmp_path,
            ModelFactory(segments=segments),
            config=make_config(tmp_path, fix_chunk_words=2, fix_parallel=5),
        )

    words = [w["word"] for s in read_transcript(video_dir)["segments"] for w in s["words"]]
    assert words == [
        " CORRIGE0", " mot1",
        " CORRIGE1", " mot3",
        " CORRIGE2", " mot5",
        " CORRIGE3", " mot7",
        " CORRIGE4", " mot9",
    ]


# --------------------------------------------------------------------------
# C14 : cache de prompt du prefixe commun (consignes + vocabulaire) entre
# tranches de correction (TASK-2cac, meme principe que clipper.jury/TASK-2852)
# --------------------------------------------------------------------------


def test_fix_chunks_share_an_identical_prompt_prefix_for_the_cache(tmp_path, video_dir, cpu):
    segments = _many_word_segments(12)  # fix_chunk_words=2 -> 6 tranches
    fake = FakeBackend([VOCAB] + [{"corrections": []}] * 6)
    with llm.use_backend(fake):
        run(
            tmp_path,
            ModelFactory(segments=segments),
            config=make_config(tmp_path, fix_chunk_words=2, fix_parallel=3),
        )
    fix_prompts = [c.prompt for c in fake.calls if c.usage == "transcript_fix"]
    assert len(fix_prompts) == 6

    def prefix(prompt):
        return prompt.split("0\t")[0]  # tout ce qui precede la 1re ligne (mot d'index 0)

    assert len({prefix(p) for p in fix_prompts}) == 1


def test_fix_chunks_send_the_first_chunk_alone_to_warm_the_prompt_cache(tmp_path, video_dir, cpu):
    # La 1re tranche part seule pour que le fournisseur du modele mette en
    # cache le prefixe commun (_fix_prefix) avant que les suivantes ne
    # partent en parallele et le relisent, au lieu de le reecrire chacune.
    # Mesure par horodatage, pas par une barriere qu'un envoi groupe en une
    # seule vague a une chance de croiser par coincidence.
    segments = _many_word_segments(12)  # fix_chunk_words=2 -> 6 tranches
    starts: dict[int, float] = {}
    finishes: dict[int, float] = {}
    lock = threading.Lock()
    counter = {"n": 0}

    def synced(request):
        with lock:
            index = counter["n"]
            counter["n"] += 1
            starts[index] = time.monotonic()
        if index == 0:
            time.sleep(0.2)
        with lock:
            finishes[index] = time.monotonic()
        return {"corrections": []}

    fake = FakeBackend([VOCAB] + [synced] * 6)
    with llm.use_backend(fake):
        run(
            tmp_path,
            ModelFactory(segments=segments),
            config=make_config(tmp_path, fix_chunk_words=2, fix_parallel=3),
        )

    assert counter["n"] == 6
    for index in range(1, 6):
        assert starts[index] >= finishes[0], index


# --------------------------------------------------------------------------
# C_usage_log : les appels LLM faits depuis les threads de correction sont
# journalises dans llm_usage.jsonl (TASK-b0fa) : llm.usage_log() fixe un
# chemin par defaut via une variable de module ordinaire, visible depuis un
# thread lance pendant le bloc (voir clipper.llm.usage_log).
# --------------------------------------------------------------------------


def test_llm_usage_log_captures_calls_made_from_fix_threads(tmp_path, video_dir, cpu):
    segments = _many_word_segments(8)  # fix_chunk_words=2 -> 4 tranches
    fake = FakeBackend([VOCAB] + [{"corrections": []}] * 4)
    usage_log_path = tmp_path / "llm_usage.jsonl"
    with llm.use_backend(fake), llm.usage_log(usage_log_path):
        run(
            tmp_path,
            ModelFactory(segments=segments),
            config=make_config(tmp_path, fix_chunk_words=2, fix_parallel=4),
        )

    lines = [json.loads(line) for line in usage_log_path.read_text(encoding="utf-8").splitlines()]
    usages = [line["usage"] for line in lines]
    assert usages.count("vocab") == 1
    assert usages.count("transcript_fix") == 4


def test_one_chunk_failure_fails_the_step_with_its_reason_others_may_run(
    tmp_path, video_dir, cpu
):
    from clipper.transcribe import TranscribeError

    segments = _many_word_segments(8)  # fix_chunk_words=2 -> 4 tranches

    def boom(request):
        raise TranscribeError("tranche corrompue")

    fake = FakeBackend([VOCAB, {"corrections": []}, boom, {"corrections": []}, {"corrections": []}])
    with llm.use_backend(fake):
        with pytest.raises(TranscribeError, match="tranche corrompue"):
            run(
                tmp_path,
                ModelFactory(segments=segments),
                config=make_config(tmp_path, fix_chunk_words=2, fix_parallel=4),
            )
    assert not (video_dir / "transcript.json").exists()


def test_first_definitive_chunk_failure_cancels_the_chunks_not_yet_started(tmp_path, video_dir, cpu):
    """Au premier echec definitif d'une tranche, les tranches pas encore
    lancees sont annulees plutot que toutes demandees avant que l'erreur ne
    remonte (Mineur 1, revue r-transcription) : sinon, avec de nombreuses
    tranches et ``claude -p`` qui ne repond plus, l'echec remonte bien plus
    tard que prevu (TASK-db6f)."""
    segments = _many_word_segments(10)
    calls: list[str] = []
    lock = threading.Lock()

    def respond(request):
        line = next(l for l in request.prompt.splitlines() if "\t" in l)
        with lock:
            calls.append(line)
        if line.endswith("mot1"):
            raise llm.LLMError("echec definitif tranche mot1")
        return {"corrections": []}

    fake = FakeBackend([VOCAB, respond])
    with llm.use_backend(fake), pytest.raises(llm.LLMError):
        run(tmp_path, ModelFactory(segments=segments),
            config=make_config(tmp_path, fix_chunk_words=1, fix_parallel=1))
    assert len(calls) < 10  # certaines tranches jamais demandees (annulees)


# --------------------------------------------------------------------------
# C13 : transcript_raw.json (resultat brut de whisper) et reprise sans
# relancer whisper apres un echec de la correction
# --------------------------------------------------------------------------


def read_raw(video_dir):
    return json.loads((video_dir / "transcript_raw.json").read_text(encoding="utf-8"))


def test_transcript_raw_json_written_before_correction(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory)

    raw = read_raw(video_dir)
    assert raw["language"] == "fr"
    assert raw["vocab"] == ["Rockstar", "Vice City", "Lucia", "Jason"]
    # le brut garde le mot non corrige, transcript.json aura la correction
    assert raw["segments"][0]["words"][1]["word"] == " Rokstar"


def test_retry_after_fix_failure_reuses_raw_and_does_not_rerun_whisper(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, llm.TransientLLMError("quota")])):
        with pytest.raises(llm.TransientLLMError):
            run(tmp_path, factory)
    assert len(factory.built) == 1
    assert (video_dir / "transcript_raw.json").exists()
    assert not (video_dir / "transcript.json").exists()

    fake_retry = FakeBackend([NO_FIX])
    with llm.use_backend(fake_retry):
        run(tmp_path, factory)

    assert len(factory.built) == 1  # whisper pas relance
    assert [c.usage for c in fake_retry.calls] == ["transcript_fix"]  # vocab pas redemande
    assert read_transcript(video_dir)["language"] == "fr"


def test_retry_reuses_raw_vocab_and_applies_correction(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, llm.TransientLLMError("quota")])):
        with pytest.raises(llm.TransientLLMError):
            run(tmp_path, factory)

    with llm.use_backend(FakeBackend([{"corrections": [{"i": 1, "old": "Rokstar", "word": "Rockstar"}]}])):
        run(tmp_path, factory)

    data = read_transcript(video_dir)
    assert data["vocab"] == ["Rockstar", "Vice City", "Lucia", "Jason"]
    assert data["segments"][0]["words"][1]["word"] == " Rockstar"


def test_force_redoes_whisper_even_if_raw_transcript_exists(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory)
    assert len(factory.built) == 1

    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory, force=True)
    assert len(factory.built) == 2


# --------------------------------------------------------------------------
# TASK-db6f : une tranche de correction bloquee ne fait plus tout
# recommencer (tranches reussies gardees, delai par tranche, 1 re-essai).
# --------------------------------------------------------------------------


def test_default_fix_timeout_s_is_360(tmp_path):
    from clipper.transcribe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["fix_timeout_s"] == 360


def test_fix_timeout_s_is_forwarded_to_llm_ask_without_changing_other_usages(tmp_path, video_dir, cpu):
    fake = FakeBackend([VOCAB, NO_FIX])
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory(), config=make_config(tmp_path, fix_timeout_s=42))

    vocab_call, fix_call = fake.calls
    assert vocab_call.usage == "vocab" and vocab_call.timeout is None
    assert fix_call.usage == "transcript_fix" and fix_call.timeout == 42


def test_transient_failure_on_one_chunk_is_retried_once_immediately_and_succeeds(
    tmp_path, video_dir, cpu
):
    segments = _many_word_segments(2)  # fix_chunk_words=1 -> 2 tranches
    attempts: dict[str, int] = {}
    lock = threading.Lock()

    def respond(request):
        line = next(l for l in request.prompt.splitlines() if "\t" in l)
        _, word_text = line.split("\t", 1)
        with lock:
            attempts[word_text] = attempts.get(word_text, 0) + 1
            n = attempts[word_text]
        if word_text == "mot0" and n == 1:
            raise llm.TransientLLMError("surcharge")
        return {"corrections": [{"i": 0, "old": word_text, "word": word_text.upper()}]}

    fake = FakeBackend([VOCAB, respond])
    config = make_config(tmp_path, fix_chunk_words=1)
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory(segments=segments), config=config)

    assert attempts["mot0"] == 2  # 1er essai transitoire + re-essai immediat, meme tranche
    assert attempts["mot1"] == 1  # l'autre tranche n'est touchee qu'une fois
    words = [w["word"] for s in read_transcript(video_dir)["segments"] for w in s["words"]]
    assert words == [" MOT0", " MOT1"]


def test_fix_chunk_result_is_cached_and_a_retried_pass_only_recalls_the_missing_chunk(
    tmp_path, video_dir, cpu
):
    """Une tranche en echec transitoire deux fois de suite (jamais relancee
    une 2e fois : l'etape echoue explicitement comme avant, ADR-ad2e) laisse
    les autres tranches, elles reussies, deja ecrites sous workspace/<id>/
    (preuve : nombre d'appels au fake backend du 2e passage)."""
    segments = _many_word_segments(4)  # fix_chunk_words=1 -> 4 tranches
    attempts: dict[str, int] = {}
    lock = threading.Lock()
    mot3_started = threading.Event()

    def respond(request):
        line = next(l for l in request.prompt.splitlines() if "\t" in l)
        _, word_text = line.split("\t", 1)
        with lock:
            attempts[word_text] = attempts.get(word_text, 0) + 1
        if word_text == "mot3":
            mot3_started.set()
        if word_text == "mot2":
            # attend que mot3 soit parti : sinon l'annulation des tranches en attente (cancel_futures)
            # peut l'annuler avant son demarrage, selon l'ordonnancement des threads (test instable).
            mot3_started.wait(timeout=5)
            raise llm.TransientLLMError("quota")  # echoue aux 2 essais (initial + re-essai)
        return {"corrections": [{"i": 0, "old": word_text, "word": word_text.upper()}]}

    fake = FakeBackend([VOCAB, respond])
    config = make_config(tmp_path, fix_chunk_words=1)
    factory1 = ModelFactory(segments=segments)
    with llm.use_backend(fake):
        with pytest.raises(llm.TransientLLMError):
            run(tmp_path, factory1, config=config)
    assert not (video_dir / "transcript.json").exists()
    assert attempts == {"mot0": 1, "mot1": 1, "mot2": 2, "mot3": 1}

    # 2e passage : whisper pas relance (raw reutilise), seule la tranche
    # manquante (mot2) est redemandee au fake -- les 3 autres, deja ecrites
    # sous workspace/<id>/fix_chunks/, ne generent aucun nouvel appel.
    fake2 = FakeBackend([lambda request: {"corrections": [{"i": 0, "old": "mot2", "word": "DEUX"}]}])
    factory2 = ModelFactory(segments=segments)
    with llm.use_backend(fake2):
        run(tmp_path, factory2, config=config)

    assert factory2.built == []  # whisper pas relance
    assert [c.usage for c in fake2.calls] == ["transcript_fix"]  # vocab et 3 tranches pas redemandes
    words = [w["word"] for s in read_transcript(video_dir)["segments"] for w in s["words"]]
    assert words == [" MOT0", " MOT1", " DEUX", " MOT3"]


def test_force_clears_the_fix_chunk_cache_so_a_full_redo_is_never_reused(tmp_path, video_dir, cpu):
    """``force`` refait whisper et le vocabulaire : une tranche de meme texte
    ne doit pas rejouer une correction calculee avant (contexte different),
    donc le cache de tranches est vide a chaque force (TASK-db6f)."""
    segments = _many_word_segments(1)
    config = make_config(tmp_path, fix_chunk_words=1)
    with llm.use_backend(FakeBackend([VOCAB, {"corrections": [{"i": 0, "old": "mot0", "word": "UN"}]}])):
        run(tmp_path, ModelFactory(segments=segments), config=config)
    assert read_transcript(video_dir)["segments"][0]["words"][0]["word"] == " UN"

    with llm.use_backend(FakeBackend([VOCAB, {"corrections": [{"i": 0, "old": "mot0", "word": "DEUX"}]}])):
        run(tmp_path, ModelFactory(segments=segments), config=config, force=True)
    assert read_transcript(video_dir)["segments"][0]["words"][0]["word"] == " DEUX"


# --------------------------------------------------------------------------
# C9 : cache par video (ADR-b16b)
# --------------------------------------------------------------------------


def test_existing_transcript_is_not_redone_unless_forced(tmp_path, video_dir, cpu):
    (video_dir / "transcript.json").write_text('{"language": "en", "segments": []}', encoding="utf-8")
    factory = ModelFactory()
    fake = FakeBackend([])
    with llm.use_backend(fake):
        path = run(tmp_path, factory)
    assert Path(path) == video_dir / "transcript.json"
    assert factory.built == [] and fake.calls == []

    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory, force=True)
    assert read_transcript(video_dir)["language"] == "fr"


def test_default_fix_chunk_words_and_fix_parallel(tmp_path):
    from clipper.transcribe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["fix_chunk_words"] == 3000
    assert CONFIG_DEFAULTS["fix_parallel"] == 4


def test_default_batch_size_is_8(tmp_path):
    from clipper.transcribe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["batch_size"] == 8


# --------------------------------------------------------------------------
# TASK-746b (banc docs/bench-whisper-vitesse.md) : BatchedInferencePipeline,
# batch_size de la config ; sequentiel (WhisperModel.transcribe direct) si
# batch_size <= 1.
# --------------------------------------------------------------------------


def test_batched_pipeline_wraps_the_model_with_batch_size_from_config(tmp_path, video_dir, cpu):
    factory = ModelFactory()
    built_with = []

    def pipeline_factory(model):
        built_with.append(model)
        return FakePipeline(model)

    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory, pipeline_factory=pipeline_factory)

    assert len(built_with) == 1
    assert factory.kwargs_seen["batch_size"] == 8


@pytest.mark.parametrize("batch_size", [0, 1])
def test_batch_size_at_most_1_runs_sequentially_without_a_pipeline(tmp_path, video_dir, cpu, batch_size):
    factory = ModelFactory()
    built_with = []

    def pipeline_factory(model):
        built_with.append(model)
        return FakePipeline(model)

    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(
            tmp_path, factory,
            config=make_config(tmp_path, batch_size=batch_size),
            pipeline_factory=pipeline_factory,
        )

    assert built_with == []
    assert "batch_size" not in factory.kwargs_seen


def test_config_section_is_accepted_by_clipper_config(tmp_path):
    from clipper.config import load_config

    (tmp_path / "config.toml").write_text('[transcribe]\nmodel = "tiny"\nvocab = false\n', encoding="utf-8")
    section = load_config(tmp_path / "config.toml").section("transcribe")
    assert section["model"] == "tiny"
    assert section["vocab"] is False
    assert section["transcript_fix"] is True


# --------------------------------------------------------------------------
# C11 : DLL cuBLAS/cuDNN des paquets pip nvidia rendues trouvables avant de
# charger faster-whisper quand le device est cuda sous Windows (TASK-f6c8) :
# ajouter les dossiers bin au PATH du processus, jamais os.add_dll_directory
# (mesure sur materiel reel : add_dll_directory seul ne suffit pas, voir le
# journal de la tache).
# --------------------------------------------------------------------------


@pytest.fixture
def fake_nvidia_bin_dirs(tmp_path):
    """Simule l'arborescence site-packages/nvidia/*/bin de nvidia-cublas-cu12
    et nvidia-cudnn-cu12, sans les vraies DLL ni le vrai GPU."""
    nvidia_dir = tmp_path / "site-packages" / "nvidia"
    cublas_bin = nvidia_dir / "cublas" / "bin"
    cudnn_bin = nvidia_dir / "cudnn" / "bin"
    cublas_bin.mkdir(parents=True)
    cudnn_bin.mkdir(parents=True)
    return nvidia_dir, [cublas_bin, cudnn_bin]


def _fake_find_spec(nvidia_dir):
    def find_spec(name):
        if name != "nvidia":
            return None
        return SimpleNamespace(submodule_search_locations=[str(nvidia_dir)])

    return find_spec


def test_the_cuda_dll_rule_lives_in_clipper_gpu_only():
    """media-M3 : la copie de la regle CUDA de transcribe.py etait morte (get_device()
    appelle deja clipper.gpu.ensure_cuda_dlls_on_path) ; une seule regle, dans clipper.gpu."""
    import clipper.transcribe as t

    assert not hasattr(t, "_make_cuda_dlls_discoverable")
    assert not hasattr(t, "_cuda_dll_dirs")


def test_the_cuda_device_is_passed_as_is_to_the_model(tmp_path, video_dir, monkeypatch):
    import clipper.transcribe as t

    monkeypatch.setattr(t, "get_device", lambda: Device(type="cuda", compute_type="float16"))
    monkeypatch.setenv("PATH", r"C:\Windows\System32")

    factory = ModelFactory()
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, factory)

    assert os.environ["PATH"] == r"C:\Windows\System32"
    assert factory.built == [("small", "cuda", "float16")]


def test_ffmpeg_binary_is_a_transcribe_setting(monkeypatch):
    """media-M4 : le binaire ffmpeg vient d'un reglage, comme download et audio."""
    import clipper.transcribe as t

    assert t.CONFIG_DEFAULTS["ffmpeg_bin"] == "ffmpeg"
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)

    monkeypatch.setattr(t.subprocess, "run", fake_run)
    t.extract_audio("v.mp4", "a.wav", ffmpeg_bin="mon-ffmpeg")

    assert seen[0][0] == "mon-ffmpeg"


def test_the_default_extractor_uses_the_configured_ffmpeg_binary(tmp_path, video_dir, cpu, monkeypatch):
    import clipper.transcribe as t

    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd[0])
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(t.subprocess, "run", fake_run)
    config = make_config(tmp_path, ffmpeg_bin="mon-ffmpeg", vocab=False)

    with pytest.raises(t.TranscribeError, match="mon-ffmpeg"):
        t.transcribe(VIDEO_ID, tmp_path / "workspace", config=config, model_factory=ModelFactory())

    assert seen == ["mon-ffmpeg"]


def test_the_batched_runner_is_released_before_the_memory_is_freed(tmp_path, video_dir, cpu, monkeypatch):
    """media-M4 : _free_memory() ne peut rien rendre tant que le runner (BatchedInferencePipeline)
    est encore reference par _run_whisper."""
    import clipper.transcribe as t

    refs = []

    def pipeline_factory(model):
        pipe = FakePipeline(model)
        refs.append(weakref.ref(pipe))
        return pipe

    alive_at_free = []

    def spy_free():
        gc.collect()
        alive_at_free.append(refs[0]() is not None)

    monkeypatch.setattr(t, "_free_memory", spy_free)
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, ModelFactory(), pipeline_factory=pipeline_factory)

    assert alive_at_free == [False]

# --------------------------------------------------------------------------
# C10 : integration optionnelle avec le vrai faster-whisper, modele tiny
# (telecharge au premier lancement) : CLIPPER_WHISPER_INTEGRATION=1 pytest
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("CLIPPER_WHISPER_INTEGRATION") != "1",
    reason="integration whisper : definir CLIPPER_WHISPER_INTEGRATION=1 (telecharge le modele tiny)",
)
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent du PATH")
def test_integration_tiny_model_transcribes_a_short_clip(tmp_path, video_dir):
    """Transcrit CLIPPER_WHISPER_CLIP (un court extrait parle) s'il est
    fourni, sinon quelques secondes generees par ffmpeg."""
    from clipper.transcribe import transcribe

    video = video_dir / f"{VIDEO_ID}.mp4"
    clip = os.environ.get("CLIPPER_WHISPER_CLIP")
    if clip:
        shutil.copyfile(clip, video)
    else:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-shortest", str(video)],
            check=True,
        )
    config = make_config(tmp_path, model="tiny", vocab=False, transcript_fix=False)
    transcribe(VIDEO_ID, tmp_path / "workspace", config=config)

    data = read_transcript(video_dir)
    assert isinstance(data["language"], str) and data["language"]
    for seg in data["segments"]:
        assert seg["start"] <= seg["end"]
        for w in seg["words"]:
            assert set(w) == {"word", "start", "end", "probability"}
    if clip:
        assert any(seg["words"] for seg in data["segments"])


# -- audio sur la ligne de temps du conteneur (TASK-4880) ---------------------

import shutil as _shutil
import subprocess as _subprocess
import wave as _wave

import pytest as _pytest

_needs_ffmpeg = _pytest.mark.skipif(_shutil.which("ffmpeg") is None, reason="ffmpeg absent")


def _make_gap_aac(path, gap: bool):
    """Sine 4 s en aac ; avec `gap`, pts decales de +2 s a T=2 (trou de 2 s, conteneur de 6 s)."""
    out = path.with_suffix(".mka")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
    if gap:
        cmd += ["-af", "asetpts='if(gte(PTS,2/TB),PTS+2/TB,PTS)'"]
    _subprocess.run(cmd + ["-c:a", "aac", "-f", "matroska", str(out)], check=True)
    return out


def _wav_info(path):
    with _wave.open(str(path), "rb") as w:
        n = w.getnframes()
        rate = w.getframerate()
        raw = w.readframes(n)
    import numpy as np
    samples = np.frombuffer(raw, dtype="<i2").astype(float)
    return n / rate, rate, samples


def _container_duration(path) -> float:
    out = _subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)


@_needs_ffmpeg
def test_extract_audio_without_gap_keeps_the_duration(tmp_path):
    from clipper.transcribe import extract_audio

    src = _make_gap_aac(tmp_path / "v", gap=False)
    extract_audio(src, tmp_path / "o.wav")

    dur, rate, _ = _wav_info(tmp_path / "o.wav")
    assert rate == 16000
    assert abs(dur - 4.0) < 0.1


@_needs_ffmpeg
def test_extract_audio_fills_a_pts_gap_with_silence(tmp_path):
    from clipper.transcribe import extract_audio

    src = _make_gap_aac(tmp_path / "v", gap=True)
    container = _container_duration(src)
    assert container > 5.9  # le fichier synthetique a bien un trou de pts de 2 s

    extract_audio(src, tmp_path / "o.wav")

    dur, rate, s = _wav_info(tmp_path / "o.wav")
    assert abs(dur - container) < 0.15
    # silence dans le trou (2.2 s .. 3.8 s), signal avant et apres
    gap_part = s[int(2.3 * rate):int(3.7 * rate)]
    assert abs(gap_part).max() < 200
    assert abs(s[int(0.5 * rate):int(1.5 * rate)]).max() > 2000
    assert abs(s[int(4.3 * rate):int(5.3 * rate)]).max() > 2000


# --------------------------------------------------------------------------
# TASK-a64a : mots geants inventes par whisper sur la musique
# --------------------------------------------------------------------------

GIANT = " " + "Tan" * 140  # 420 caracteres


def _giant_segments():
    return [
        _segment(1, [_word(" Salut", 0.0, 0.4), _word(GIANT, 0.4, 5.0, 0.91), _word(" tout", 5.0, 5.4)]),
        _segment(2, [_word(GIANT, 6.0, 9.0, 0.91)]),
        _segment(3, [_word(" anticonstitutionnellement", 10.0, 11.0)]),
    ]


def test_giant_word_is_removed_text_rebuilt_and_counted(tmp_path, video_dir, cpu, caplog):
    fake = FakeBackend([VOCAB, NO_FIX])
    caplog.set_level(logging.WARNING, logger="clipper.transcribe")
    with llm.use_backend(fake):
        run(tmp_path, ModelFactory(segments=_giant_segments()))
    data = read_transcript(video_dir)
    assert [s["id"] for s in data["segments"]] == [1, 3]
    assert data["segments"][0]["text"] == " Salut tout"
    assert [w["word"] for w in data["segments"][0]["words"]] == [" Salut", " tout"]
    assert data["hallucinated_words_removed"] == 2
    assert "TanTan" not in json.dumps(data)
    assert any("420" in r.getMessage() and "TanTan" in r.getMessage() for r in caplog.records)


def test_long_normal_word_is_kept_at_default(tmp_path, video_dir, cpu):
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, ModelFactory(segments=_giant_segments()))
    words = [w["word"] for s in read_transcript(video_dir)["segments"] for w in s["words"]]
    assert " anticonstitutionnellement" in words


def test_transcript_without_giant_word_reports_zero(tmp_path, video_dir, cpu):
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, ModelFactory())
    data = read_transcript(video_dir)
    assert data["hallucinated_words_removed"] == 0
    assert len(data["segments"]) == 2


def test_hallucination_word_max_chars_comes_from_config(tmp_path, video_dir, cpu):
    with llm.use_backend(FakeBackend([VOCAB, NO_FIX])):
        run(tmp_path, ModelFactory(segments=_giant_segments()),
            config=make_config(tmp_path, hallucination_word_max_chars=20))
    data = read_transcript(video_dir)
    assert data["hallucinated_words_removed"] == 3


@pytest.mark.parametrize("bad", [0, -1, 2.5, "40", True])
def test_invalid_hallucination_word_max_chars_is_an_error(tmp_path, video_dir, cpu, bad):
    from clipper.transcribe import TranscribeError

    factory = ModelFactory()
    with pytest.raises(TranscribeError, match="hallucination_word_max_chars"):
        run(tmp_path, factory, config=make_config(tmp_path, hallucination_word_max_chars=bad))
    assert factory.built == []
