from __future__ import annotations

import json
import threading
import time

import pytest

from clipper import llm
from clipper.config import Config
from clipper.llm.fake import FakeBackend

VIDEO_ID = "abcdefghijk"


# --------------------------------------------------------------------------
# Transcription de test : deux phrases de 4 mots, 0.0-3.9 s puis 5.0-8.9 s.
# --------------------------------------------------------------------------


def make_transcript(language="fr"):
    def words(base, tokens):
        return [
            {"word": f" {tok}", "start": round(base + i, 2), "end": round(base + i + 0.9, 2), "probability": 0.9}
            for i, tok in enumerate(tokens)
        ]

    segments = [
        {"id": 0, "start": 0.0, "end": 3.9, "text": " alpha beta gamma delta",
         "words": words(0.0, ["alpha", "beta", "gamma", "delta"])},
        {"id": 1, "start": 5.0, "end": 8.9, "text": " epsilon zeta eta theta",
         "words": words(5.0, ["epsilon", "zeta", "eta", "theta"])},
    ]
    return {
        "video_id": VIDEO_ID, "language": language, "language_probability": 0.99,
        "duration": 10.0, "model": "small", "vocab": [], "segments": segments,
    }


def moment(id_, justification="ca marche", hook_text="accroche du moment"):
    """Un moment tel que l'ecrit clipper/moments.py dans moments.json."""
    return {
        "id": id_, "start": 0.0, "end": 8.9, "duration": 8.9, "format": "multipart", "parts": [],
        "scores": {"hook": 8}, "bonus": {"replayed": 0.0, "audio_peaks": 0.0, "visual": 0.0, "total": 0.0},
        "final_score": 80.0, "justification": justification, "hook_text": hook_text,
    }


def moments_json(*moments):
    return {"video_id": VIDEO_ID, "rubric": {}, "chunked": False, "moments": list(moments), "rejected": []}


def part(n, start, end, hook_text="accroche", suspense=None):
    return {"part": n, "start": start, "end": end, "duration": round(end - start, 2),
            "hook_text": hook_text, "suspense": suspense}


def parts_record(id_, fmt, parts_total, parts):
    return {"id": id_, "start": parts[0]["start"], "end": parts[-1]["end"],
            "duration": round(parts[-1]["end"] - parts[0]["start"], 2), "format": fmt,
            "parts_total": parts_total, "proposed_cuts": [], "parts": parts}


def parts_json(*moments, rejected=()):
    return {"video_id": VIDEO_ID, "rubric": {"path": "rubric.toml", "durations": {}},
            "moments": list(moments), "rejected": list(rejected)}


@pytest.fixture
def workspace(tmp_path):
    d = tmp_path / "workspace" / VIDEO_ID
    d.mkdir(parents=True)
    (d / "transcript.json").write_text(json.dumps(make_transcript()), encoding="utf-8")
    return tmp_path / "workspace"


def write_moments(workspace, *moments):
    (workspace / VIDEO_ID / "moments.json").write_text(json.dumps(moments_json(*moments)), encoding="utf-8")


def write_parts(workspace, *moments, rejected=()):
    (workspace / VIDEO_ID / "parts.json").write_text(
        json.dumps(parts_json(*moments, rejected=rejected)), encoding="utf-8"
    )


def write_meta(workspace, title="Une video"):
    (workspace / VIDEO_ID / "meta.json").write_text(json.dumps({"title": title}), encoding="utf-8")


def make_config(tmp_path, **captions_overrides):
    return Config(
        mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={"captions": captions_overrides} if captions_overrides else {},
    )


def answer(title="Titre choc", caption="Une legende qui donne envie.", hashtags=("#un", "#deux"),
           hook_text="quatre mots pour accrocher", screen_title="Info precise du clip",
           include_screen_title=True, include_title=True):
    result = {"caption": caption, "hashtags": list(hashtags), "hook_text": hook_text}
    if include_title:
        result["title"] = title
    if include_screen_title:
        result["screen_title"] = screen_title
    return result


def run(workspace, config, responses, **kwargs):
    from clipper.captions import run as run_captions

    fake = FakeBackend(responses)
    with llm.use_backend(fake):
        path = run_captions(VIDEO_ID, workspace, config=config, **kwargs)
    return fake, path


def read_captions(workspace):
    return json.loads((workspace / VIDEO_ID / "captions.json").read_text(encoding="utf-8"))


def by_id(data, id_):
    return next(c for c in data["clips"] if c["id"] == id_)


# --------------------------------------------------------------------------
# Un clip par partie
# --------------------------------------------------------------------------


def test_single_clip_gets_title_caption_hashtags_and_hook_text_from_the_llm(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, path = run(workspace, make_config(tmp_path), [answer()])

    assert path == workspace / VIDEO_ID / "captions.json"
    assert [c.usage for c in fake.calls] == ["captions"]
    clip = by_id(read_captions(workspace), "00")
    assert clip["title"] == "Titre choc"
    assert clip["caption"] == "Une legende qui donne envie."
    assert clip["hashtags"] == ["#un", "#deux"]
    assert clip["hook_text"] == "quatre mots pour accrocher"
    assert clip["screen_title"] == "Info precise du clip"
    assert clip["moment_id"] == 0 and clip["part"] == 1 and clip["parts_total"] == 1
    assert clip["start"] == 0.0 and clip["end"] == 3.9
    assert clip["language"] == "fr"


def test_multipart_moment_produces_one_clip_per_part_with_context(workspace, tmp_path):
    write_moments(workspace, moment(0, justification="histoire en 2 actes"))
    write_parts(
        workspace, parts_record(0, "multipart", 2, [part(1, 0.0, 3.9, suspense="a suivre"), part(2, 5.0, 8.9)])
    )

    fake, _ = run(
        workspace, make_config(tmp_path),
        [answer(title="Grosse histoire"), answer(include_title=False, include_screen_title=False)],
    )

    data = read_captions(workspace)
    assert [c["id"] for c in data["clips"]] == ["00-p1", "00-p2"]
    assert [c["title"] for c in data["clips"]] == ["Grosse histoire (Partie 1)", "Grosse histoire (Partie 2)"]
    assert all(c["parts_total"] == 2 for c in data["clips"])
    assert [c["part"] for c in data["clips"]] == [1, 2]
    assert len(fake.calls) == 2
    assert "1/2" in fake.calls[0].prompt
    assert "2/2" in fake.calls[1].prompt


def test_prompt_carries_language_video_title_justification_and_part_text(workspace, tmp_path):
    write_moments(workspace, moment(0, justification="parce que oui"))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    write_meta(workspace, title="Ma super video")

    fake, _ = run(workspace, make_config(tmp_path), [answer()])

    prompt = fake.calls[0].prompt
    assert "fr" in prompt
    assert "Ma super video" in prompt
    assert "parce que oui" in prompt
    assert "alpha" in prompt and "beta" in prompt and "gamma" in prompt and "delta" in prompt
    assert "epsilon" not in prompt


def test_meta_json_is_optional(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, path = run(workspace, make_config(tmp_path), [answer()])

    assert path.exists()
    assert fake.calls[0].usage == "captions"


def test_no_kept_moment_writes_empty_clips_without_calling_the_llm(workspace, tmp_path):
    write_moments(workspace)
    write_parts(workspace, rejected=[{"id": 0, "start": 0.0, "end": 1.0, "duration": 1.0, "reason": "trop court"}])

    fake, _ = run(workspace, make_config(tmp_path), [])

    assert read_captions(workspace)["clips"] == []
    assert fake.calls == []


# --------------------------------------------------------------------------
# Validation de la reponse (SPEC-6127) : longueurs, hashtags, doublons
# --------------------------------------------------------------------------


def test_hashtag_without_hash_prefix_is_a_failure_and_writes_nothing(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(llm.SchemaError, match="#"):
        run(workspace, make_config(tmp_path), [answer(hashtags=["sansdiese"])] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


@pytest.mark.parametrize("bad", ["#deux mots", "#a,#b", "#a, #b", "#a#b ", "#a\t"])
def test_hashtag_with_a_space_or_a_comma_is_a_failure_and_writes_nothing(workspace, tmp_path, bad):
    """jury-M4 : un hashtag avec espace ou virgule n'en est pas un (TikTok le coupe)."""
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(llm.SchemaError, match="espace ou virgule"):
        run(workspace, make_config(tmp_path), [answer(hashtags=[bad])] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_duplicate_hashtags_case_insensitive_is_a_failure(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(llm.SchemaError, match="double"):
        run(workspace, make_config(tmp_path), [answer(hashtags=["#Viral", "#viral"])] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_hook_text_over_the_word_limit_is_a_failure(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    nine_words = "un deux trois quatre cinq six sept huit neuf"

    with pytest.raises(llm.SchemaError, match="9 mots"):
        run(workspace, make_config(tmp_path), [answer(hook_text=nine_words)] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_refused_hook_text_is_sent_back_to_the_llm_with_the_error_and_repaired(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    ten_words = "un deux trois quatre cinq six sept huit neuf dix"

    fake, _ = run(workspace, make_config(tmp_path),
                  [answer(hook_text=ten_words), answer(hook_text="trois mots courts")])

    assert len(fake.calls) == 2
    assert "texte d'accroche de 10 mots, il en faut 6 au plus : supprime 4 mots" in fake.calls[1].prompt
    assert "1. un" in fake.calls[1].prompt and "10. dix" in fake.calls[1].prompt
    assert by_id(read_captions(workspace), "00")["hook_text"] == "trois mots courts"


def test_refused_hashtags_are_repaired_the_same_way(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(workspace, make_config(tmp_path),
                  [answer(hashtags=["#Viral", "#viral"]), answer(hashtags=["#viral", "#drole"])])

    assert "hashtag en double : '#viral'" in fake.calls[1].prompt
    assert by_id(read_captions(workspace), "00")["hashtags"] == ["#viral", "#drole"]


def test_hook_text_exactly_at_the_word_limit_is_accepted(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    eight_words = "un deux trois quatre cinq six sept huit"

    run(workspace, make_config(tmp_path), [answer(hook_text=eight_words)])

    assert by_id(read_captions(workspace), "00")["hook_text"] == eight_words


def test_hook_text_with_isolated_punctuation_is_counted_by_real_words(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    eight_words_with_colon = "La nouvelle mode : ils se volent entre eux"

    run(workspace, make_config(tmp_path), [answer(hook_text=eight_words_with_colon)])

    assert by_id(read_captions(workspace), "00")["hook_text"] == eight_words_with_colon


def test_hook_text_apostrophe_and_hyphen_words_count_as_one_each(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    six_words_various_punctuation = "l'heure du crime ; vraiment ? — peut-être … non !"

    run(workspace, make_config(tmp_path), [answer(hook_text=six_words_various_punctuation)])

    assert by_id(read_captions(workspace), "00")["hook_text"] == six_words_various_punctuation


def test_hook_words_max_is_configurable(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    three_words = "un deux trois"

    with pytest.raises(llm.SchemaError, match="2 au plus"):
        run(workspace, make_config(tmp_path, hook_words_max=2), [answer(hook_text=three_words)] * 2)


def test_schema_rejects_a_response_missing_a_required_field(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    bad = {"title": "t", "caption": "c", "hashtags": ["#a"]}  # hook_text manquant

    with pytest.raises(llm.SchemaError):
        run(workspace, make_config(tmp_path), [bad, bad])
    assert not (workspace / VIDEO_ID / "captions.json").exists()


# --------------------------------------------------------------------------
# Echecs, cache, entrees manquantes (ADR-ad2e : jamais de repli silencieux)
# --------------------------------------------------------------------------


def test_llm_unavailable_propagates_and_writes_nothing(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(llm.TransientLLMError):
        run(workspace, make_config(tmp_path), [llm.TransientLLMError("quota")])
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_second_part_failing_still_writes_nothing_for_the_first(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(
        workspace, parts_record(0, "multipart", 2, [part(1, 0.0, 3.9), part(2, 5.0, 8.9)])
    )

    with pytest.raises(llm.TransientLLMError):
        run(workspace, make_config(tmp_path), [answer(), llm.TransientLLMError("quota")])
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_existing_result_is_not_redone_unless_forced(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    config = make_config(tmp_path)
    run(workspace, config, [answer()])

    fake, _ = run(workspace, config, [])
    assert fake.calls == []

    fake, _ = run(workspace, config, [answer(title="Refait")], force=True)
    assert len(fake.calls) == 1
    assert by_id(read_captions(workspace), "00")["title"] == "Refait"


def test_missing_parts_json_is_an_error(workspace, tmp_path):
    from clipper.captions import CaptionsError

    write_moments(workspace, moment(0))
    with pytest.raises(CaptionsError, match="parts.json"):
        run(workspace, make_config(tmp_path), [])


def test_missing_moments_json_is_an_error(workspace, tmp_path):
    from clipper.captions import CaptionsError

    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    with pytest.raises(CaptionsError, match="moments.json"):
        run(workspace, make_config(tmp_path), [])


def test_part_moment_missing_from_moments_json_is_an_error(workspace, tmp_path):
    from clipper.captions import CaptionsError

    write_moments(workspace)  # aucun moment
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(CaptionsError, match="moments.json"):
        run(workspace, make_config(tmp_path), [])


def test_default_config_values():
    from clipper.captions import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["hashtags_max"] == 8
    assert CONFIG_DEFAULTS["hook_words_max"] == 8
    assert CONFIG_DEFAULTS["title_max_chars"] == 100
    assert CONFIG_DEFAULTS["caption_max_chars"] == 300
    assert CONFIG_DEFAULTS["screen_title_words_max"] == 6
    assert CONFIG_DEFAULTS["screen_title_allow_emoji"] is False
    assert CONFIG_DEFAULTS["screen_title_forbidden_words"] == [
        "pur", "total", "explose", "choc", "incroyable", "fou", "dingue",
        "glaçant", "assourdissant", "dévoilé",
    ]
    assert CONFIG_DEFAULTS["parallel"] == 4


# --------------------------------------------------------------------------
# screen_title (SPEC-6a86) : mots, ton sobre (sans emoji ni superlatif par
# defaut), option emoji
# --------------------------------------------------------------------------


def test_screen_title_sober_answer_without_emoji_is_kept_by_default(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(workspace, make_config(tmp_path), [answer(screen_title="C'est des salopards")])

    assert len(fake.calls) == 1
    assert by_id(read_captions(workspace), "00")["screen_title"] == "C'est des salopards"


def test_screen_title_with_an_emoji_is_refused_by_default_then_fails(workspace, tmp_path):
    from clipper.captions import run as run_captions

    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    fake = FakeBackend([answer(screen_title="Info precise ici \U0001F525")] * 2)

    with llm.use_backend(fake), pytest.raises(llm.SchemaError, match="aucun emoji"):
        run_captions(VIDEO_ID, workspace, config=make_config(tmp_path))

    assert len(fake.calls) == 4  # 1 + title_repair_attempts par defaut (TASK-3268)
    assert "aucun emoji" in fake.calls[1].prompt
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_screen_title_with_a_forbidden_word_is_refused_by_default_then_fails(workspace, tmp_path):
    from clipper.captions import run as run_captions

    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    fake = FakeBackend([answer(screen_title="C'est vraiment fou")] * 2)

    with llm.use_backend(fake), pytest.raises(llm.SchemaError, match="mot interdit") as exc_info:
        run_captions(VIDEO_ID, workspace, config=make_config(tmp_path))

    assert "'fou'" in str(exc_info.value)
    assert len(fake.calls) == 4  # 1 + title_repair_attempts par defaut (TASK-3268)
    assert "mot interdit" in fake.calls[1].prompt
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_screen_title_forbidden_word_is_case_and_accent_insensitive(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(llm.SchemaError, match="mot interdit"):
        run(workspace, make_config(tmp_path), [answer(screen_title="Ambiance GLAÇANT ce soir")] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_screen_title_forbidden_word_matches_whole_word_only(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    run(workspace, make_config(tmp_path), [answer(screen_title="Ce type est choquant")])

    assert by_id(read_captions(workspace), "00")["screen_title"] == "Ce type est choquant"


def test_screen_title_forbidden_words_list_is_configurable(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(llm.SchemaError, match="mot interdit"):
        run(
            workspace, make_config(tmp_path, screen_title_forbidden_words=["salopards"]),
            [answer(screen_title="C'est des salopards")] * 2,
        )
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_screen_title_with_seven_words_is_a_failure(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    seven_words = "un deux trois quatre cinq six sept"

    with pytest.raises(llm.SchemaError, match="7 mots"):
        run(workspace, make_config(tmp_path), [answer(screen_title=seven_words)] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_screen_title_words_max_is_configurable(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    three_words = "un deux trois \U0001F525"

    with pytest.raises(llm.SchemaError, match="2 au plus"):
        run(
            workspace, make_config(tmp_path, screen_title_words_max=2, screen_title_allow_emoji=True),
            [answer(screen_title=three_words)] * 2,
        )


# --------------------------------------------------------------------------
# screen_title_allow_emoji=True (SPEC-6a86) : emoji redevenu possible, mais
# jamais obligatoire, au plus un, memes regles ZWJ/drapeau qu'avant
# --------------------------------------------------------------------------


def test_screen_title_allow_emoji_accepts_a_single_emoji(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    run(
        workspace, make_config(tmp_path, screen_title_allow_emoji=True),
        [answer(screen_title="Attention arnaque \U0001F525")],
    )

    assert by_id(read_captions(workspace), "00")["screen_title"] == "Attention arnaque \U0001F525"


def test_screen_title_allow_emoji_still_accepts_no_emoji_at_all(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    run(
        workspace, make_config(tmp_path, screen_title_allow_emoji=True),
        [answer(screen_title="C'est des salopards")],
    )

    assert by_id(read_captions(workspace), "00")["screen_title"] == "C'est des salopards"


def test_screen_title_allow_emoji_still_refuses_two_emojis(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    two_emojis = "Arnaque totale \U0001F525\U0001F389"

    with pytest.raises(llm.SchemaError, match="au plus un emoji"):
        run(
            workspace, make_config(tmp_path, screen_title_allow_emoji=True),
            [answer(screen_title=two_emojis)] * 2,
        )
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_screen_title_allow_emoji_followed_by_variation_selector_is_accepted(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    emoji_vs16 = "Depart imminent \U0001F680️"

    run(workspace, make_config(tmp_path, screen_title_allow_emoji=True), [answer(screen_title=emoji_vs16)])

    assert by_id(read_captions(workspace), "00")["screen_title"] == emoji_vs16


def test_screen_title_allow_emoji_with_skin_tone_modifier_is_accepted(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    emoji_skin_tone = "Bien joue \U0001F44D\U0001F3FD"

    run(workspace, make_config(tmp_path, screen_title_allow_emoji=True), [answer(screen_title=emoji_skin_tone)])

    assert by_id(read_captions(workspace), "00")["screen_title"] == emoji_skin_tone


def test_screen_title_zwj_sequence_is_refused_even_when_emoji_allowed(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    zwj_family = "En famille " + "\U0001F468‍\U0001F469‍\U0001F467"

    with pytest.raises(llm.SchemaError, match="ZWJ"):
        run(
            workspace, make_config(tmp_path, screen_title_allow_emoji=True),
            [answer(screen_title=zwj_family)] * 2,
        )
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_screen_title_flag_is_refused_even_when_emoji_allowed(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    flag = "Exclusif France " + "\U0001F1EB\U0001F1F7"

    with pytest.raises(llm.SchemaError, match="drapeau"):
        run(
            workspace, make_config(tmp_path, screen_title_allow_emoji=True),
            [answer(screen_title=flag)] * 2,
        )
    assert not (workspace / VIDEO_ID / "captions.json").exists()


# --------------------------------------------------------------------------
# screen_title unique par moment multipart (TASK-4078, SPEC-6127)
# --------------------------------------------------------------------------


def test_multipart_screen_title_is_requested_only_once_and_shared_across_parts(workspace, tmp_path):
    write_moments(workspace, moment(0, justification="histoire en 3 actes"))
    write_parts(
        workspace,
        parts_record(0, "multipart", 3, [
            part(1, 0.0, 1.0), part(2, 5.0, 6.0), part(3, 7.0, 8.0),
        ]),
    )

    fake, _ = run(
        workspace, make_config(tmp_path),
        [
            answer(title="Grosse histoire"),
            answer(include_title=False, include_screen_title=False),
            answer(include_title=False, include_screen_title=False),
        ],
    )

    data = read_captions(workspace)
    assert [c["screen_title"] for c in data["clips"]] == ["Info precise du clip"] * 3
    assert len(fake.calls) == 3


def test_multipart_only_the_first_calls_schema_requires_screen_title(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(
        workspace,
        parts_record(0, "multipart", 3, [
            part(1, 0.0, 1.0), part(2, 5.0, 6.0), part(3, 7.0, 8.0),
        ]),
    )

    fake, _ = run(
        workspace, make_config(tmp_path),
        [
            answer(),
            answer(include_title=False, include_screen_title=False),
            answer(include_title=False, include_screen_title=False),
        ],
    )

    assert "screen_title" in fake.calls[0].schema["properties"]
    assert "screen_title" in fake.calls[0].schema["required"]
    assert "screen_title" not in fake.calls[1].schema["properties"]
    assert "screen_title" not in fake.calls[1].schema["required"]
    assert "screen_title" not in fake.calls[2].schema["properties"]
    assert "screen_title" not in fake.calls[2].schema["required"]


def test_multipart_later_parts_prompt_cites_the_chosen_screen_title_as_context(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(
        workspace,
        parts_record(0, "multipart", 2, [part(1, 0.0, 1.0), part(2, 5.0, 6.0)]),
    )

    fake, _ = run(
        workspace, make_config(tmp_path),
        [
            answer(),
            answer(include_title=False, include_screen_title=False),
        ],
    )

    assert "Info precise du clip" in fake.calls[1].prompt


def test_multipart_first_part_failing_writes_nothing(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(
        workspace,
        parts_record(0, "multipart", 2, [part(1, 0.0, 1.0), part(2, 5.0, 6.0)]),
    )

    with pytest.raises(llm.TransientLLMError):
        run(workspace, make_config(tmp_path), [llm.TransientLLMError("quota"), answer()])
    assert not (workspace / VIDEO_ID / "captions.json").exists()


# --------------------------------------------------------------------------
# title unique par moment multipart, suffixe " (Partie N)" (TASK-cb3a)
# --------------------------------------------------------------------------


def test_multipart_title_is_requested_once_and_every_part_gets_the_suffix(workspace, tmp_path):
    write_moments(workspace, moment(0, justification="histoire en 3 actes"))
    write_parts(
        workspace,
        parts_record(0, "multipart", 3, [
            part(1, 0.0, 1.0), part(2, 5.0, 6.0), part(3, 7.0, 8.0),
        ]),
    )

    fake, _ = run(
        workspace, make_config(tmp_path),
        [
            answer(title="Grosse histoire"),
            answer(include_title=False, include_screen_title=False),
            answer(include_title=False, include_screen_title=False),
        ],
    )

    data = read_captions(workspace)
    assert [c["title"] for c in data["clips"]] == [
        "Grosse histoire (Partie 1)", "Grosse histoire (Partie 2)", "Grosse histoire (Partie 3)",
    ]
    assert len(fake.calls) == 3


def test_multipart_only_the_first_call_schema_requires_title(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(
        workspace,
        parts_record(0, "multipart", 3, [
            part(1, 0.0, 1.0), part(2, 5.0, 6.0), part(3, 7.0, 8.0),
        ]),
    )

    fake, _ = run(
        workspace, make_config(tmp_path),
        [
            answer(),
            answer(include_title=False, include_screen_title=False),
            answer(include_title=False, include_screen_title=False),
        ],
    )

    assert "title" in fake.calls[0].schema["properties"]
    assert "title" in fake.calls[0].schema["required"]
    assert "title" not in fake.calls[1].schema["properties"]
    assert "title" not in fake.calls[1].schema["required"]
    assert "title" not in fake.calls[2].schema["properties"]
    assert "title" not in fake.calls[2].schema["required"]


def test_multipart_later_parts_prompt_cites_the_chosen_title_as_context(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(
        workspace,
        parts_record(0, "multipart", 2, [part(1, 0.0, 1.0), part(2, 5.0, 6.0)]),
    )

    fake, _ = run(
        workspace, make_config(tmp_path),
        [
            answer(title="Grosse histoire"),
            answer(include_title=False, include_screen_title=False),
        ],
    )

    assert "Grosse histoire" in fake.calls[1].prompt


def test_single_clip_title_has_no_suffix(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    run(workspace, make_config(tmp_path), [answer(title="Titre unique")])

    assert by_id(read_captions(workspace), "00")["title"] == "Titre unique"


def test_prompt_states_the_exact_word_counting_rule_with_examples(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(workspace, make_config(tmp_path), [answer()])

    prompt = fake.calls[0].prompt
    assert "à" in prompt and "n'ai" in prompt and "l'égorger" in prompt
    assert "hook_text" in prompt and "screen_title" in prompt
    assert "ne comptent pas" in prompt


def test_prompt_states_the_sober_screen_title_rule_by_default(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(workspace, make_config(tmp_path), [answer()])

    prompt = fake.calls[0].prompt
    assert "aucun emoji" in prompt
    assert "choc" in prompt and "incroyable" in prompt


def test_prompt_does_not_forbid_emoji_when_the_option_is_enabled(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(workspace, make_config(tmp_path, screen_title_allow_emoji=True), [answer()])

    prompt = fake.calls[0].prompt
    assert "au plus un emoji" in prompt


def _has_emoji_char(text):
    """Vrai si un caractere du bloc emoji usuel (U+1F300-U+1FAFF, couvre les
    emojis clickbait typiques : 🔥😡💊🧩😱💀👻🧟😂🪓) apparait dans ``text``."""
    return any(0x1F300 <= ord(c) <= 0x1FAFF for c in text)


def test_no_literal_emoji_in_prompt_or_schema_by_default(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(workspace, make_config(tmp_path), [answer()])

    prompt = fake.calls[0].prompt
    schema_text = json.dumps(fake.calls[0].schema, ensure_ascii=False)
    assert not _has_emoji_char(prompt), prompt
    assert not _has_emoji_char(schema_text), schema_text


def test_schema_screen_title_description_forbids_emoji_by_default():
    from clipper.captions import CONFIG_DEFAULTS, response_schema

    schema = response_schema(CONFIG_DEFAULTS)

    description = schema["properties"]["screen_title"]["description"]
    assert "exactement un emoji" not in description
    assert "aucun" in description.lower() and "emoji" in description.lower()


def test_schema_screen_title_description_allows_one_emoji_when_enabled():
    from clipper.captions import CONFIG_DEFAULTS, response_schema

    schema = response_schema({**CONFIG_DEFAULTS, "screen_title_allow_emoji": True})

    description = schema["properties"]["screen_title"]["description"]
    assert "au plus un emoji" in description


def test_hook_text_schema_error_lists_the_counted_words_numbered(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    nine_words = "un deux trois quatre cinq six sept huit neuf"

    with pytest.raises(llm.SchemaError) as exc_info:
        run(workspace, make_config(tmp_path), [answer(hook_text=nine_words)] * 2)

    message = str(exc_info.value)
    for i, word in enumerate(nine_words.split(), start=1):
        assert f"{i}. {word}" in message


def test_screen_title_schema_error_lists_the_counted_words_numbered(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    seven_words = "un deux trois quatre cinq six sept \U0001F525"

    with pytest.raises(llm.SchemaError) as exc_info:
        run(workspace, make_config(tmp_path), [answer(screen_title=seven_words)] * 2)

    message = str(exc_info.value)
    for i, word in enumerate(["un", "deux", "trois", "quatre", "cinq", "six", "sept"], start=1):
        assert f"{i}. {word}" in message


def test_refused_answer_is_logged_to_llm_refusals_jsonl_and_repaired(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    nine_words = "un deux trois quatre cinq six sept huit neuf"
    eight_words = "un deux trois quatre cinq six sept huit"

    run(workspace, make_config(tmp_path), [answer(hook_text=nine_words), answer(hook_text=eight_words)])

    log_path = workspace / VIDEO_ID / "llm_refusals.jsonl"
    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["usage"] == "captions"
    assert "9 mots" in lines[0]["error"]
    assert lines[-1].get("accepted") is True
    assert by_id(read_captions(workspace), "00")["hook_text"] == eight_words


def test_multipart_title_max_chars_requested_accounts_for_the_longest_partie_suffix(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(
        workspace,
        parts_record(0, "multipart", 3, [
            part(1, 0.0, 1.0), part(2, 5.0, 6.0), part(3, 7.0, 8.0),
        ]),
    )
    base_title = "123456789"  # 9 caracteres ; " (Partie 3)" = 11 -> total 20 = title_max_chars

    fake, _ = run(
        workspace, make_config(tmp_path, title_max_chars=20),
        [
            answer(title=base_title),
            answer(include_title=False, include_screen_title=False),
            answer(include_title=False, include_screen_title=False),
        ],
    )

    assert fake.calls[0].schema["properties"]["title"]["maxLength"] == 9
    data = read_captions(workspace)
    assert data["clips"][0]["title"] == "123456789 (Partie 1)"
    assert all(len(c["title"]) <= 20 for c in data["clips"])


# --------------------------------------------------------------------------
# Parallelisation des moments (TASK-ed18)
# --------------------------------------------------------------------------


class ConcurrencyBackend:
    """Backend de test qui journalise le chevauchement des appels concurrents
    et sert toujours la meme reponse (``response``, un dict JSON-able) ;
    ``delays`` donne le temps de pause (s) du n-ieme appel a demarrer (le
    dernier de la liste sert pour les appels suivants), sinon 0.05 s fixe."""

    def __init__(self, response, delays=None):
        self.response = response
        self.delays = list(delays) if delays is not None else None
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list = []

    def complete(self, request):
        with self.lock:
            index = len(self.calls)
            self.calls.append(request)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if self.delays:
            delay = self.delays[min(index, len(self.delays) - 1)]
        else:
            delay = 0.05
        time.sleep(delay)
        with self.lock:
            self.active -= 1
        return json.dumps(self.response)


class FailOnceBackend:
    """Backend de test qui echoue exactement au n-ieme appel a atteindre le
    verrou (``fail_after``, 1-indexe), quel que soit le moment concerne."""

    def __init__(self, response, fail_after):
        self.response = response
        self.fail_after = fail_after
        self.lock = threading.Lock()
        self.count = 0

    def complete(self, request):
        with self.lock:
            self.count += 1
            should_fail = self.count == self.fail_after
        if should_fail:
            raise llm.TransientLLMError("quota")
        return json.dumps(self.response)


class MomentAwareBackend:
    """Backend de test qui suit le chevauchement des appels par moment (les
    ``markers``, une chaine distinctive par moment cherchee dans le prompt) :
    ``max_active`` par marqueur doit rester a 1 (parties sequentielles), meme
    si plusieurs moments tournent en parallele. La reponse est adaptee au
    schema demande (title/screen_title requis ou non selon la partie)."""

    def __init__(self, markers):
        self.markers = list(markers)
        self.lock = threading.Lock()
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.calls: list = []

    def _marker(self, prompt):
        found = [m for m in self.markers if m in prompt]
        assert len(found) == 1, f"marqueur de moment introuvable ou ambigu dans le prompt : {found}"
        return found[0]

    def complete(self, request):
        key = self._marker(request.prompt)
        with self.lock:
            self.calls.append(request)
            self.active[key] = self.active.get(key, 0) + 1
            self.max_active[key] = max(self.max_active.get(key, 0), self.active[key])
        time.sleep(0.05)
        with self.lock:
            self.active[key] -= 1
        include_title = "title" in request.schema["properties"]
        include_screen_title = "screen_title" in request.schema["properties"]
        return json.dumps(answer(include_title=include_title, include_screen_title=include_screen_title))


def run_with_backend(workspace, config, backend, **kwargs):
    from clipper.captions import run as run_captions

    with llm.use_backend(backend):
        path = run_captions(VIDEO_ID, workspace, config=config, **kwargs)
    return path


def test_parallel_below_1_is_refused(workspace, tmp_path):
    from clipper.captions import CaptionsError

    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(CaptionsError, match="parallel"):
        run(workspace, make_config(tmp_path, parallel=0), [])


def test_moments_overlap_when_parallel_is_4(workspace, tmp_path):
    ids = [10, 11, 12, 13]
    write_moments(workspace, *(moment(i) for i in ids))
    write_parts(workspace, *(parts_record(i, "single", 1, [part(1, 0.0, 3.9)]) for i in ids))
    backend = ConcurrencyBackend(answer())

    run_with_backend(workspace, make_config(tmp_path, parallel=4), backend)

    assert len(backend.calls) == 4
    assert backend.max_active >= 2


def test_moments_never_overlap_when_parallel_is_1(workspace, tmp_path):
    ids = [20, 21, 22, 23]
    write_moments(workspace, *(moment(i) for i in ids))
    write_parts(workspace, *(parts_record(i, "single", 1, [part(1, 0.0, 3.9)]) for i in ids))
    backend = ConcurrencyBackend(answer())

    run_with_backend(workspace, make_config(tmp_path, parallel=1), backend)

    assert len(backend.calls) == 4
    assert backend.max_active == 1


def test_parts_of_the_same_moment_never_overlap_and_keep_order_even_with_parallel_moments(workspace, tmp_path):
    write_moments(
        workspace,
        moment(0, justification="histoire 0"),
        moment(1, justification="histoire 1"),
    )
    write_parts(
        workspace,
        parts_record(0, "multipart", 2, [part(1, 0.0, 3.9), part(2, 5.0, 8.9)]),
        parts_record(1, "multipart", 2, [part(1, 0.0, 3.9), part(2, 5.0, 8.9)]),
    )
    backend = MomentAwareBackend(["histoire 0", "histoire 1"])

    run_with_backend(workspace, make_config(tmp_path, parallel=4), backend)

    assert len(backend.calls) == 4
    assert backend.max_active["histoire 0"] == 1
    assert backend.max_active["histoire 1"] == 1
    data = read_captions(workspace)
    assert [c["id"] for c in data["clips"]] == ["00-p1", "00-p2", "01-p1", "01-p2"]


def test_output_is_identical_between_parallel_1_and_4_even_out_of_order(workspace, tmp_path):
    ids = [30, 31, 32, 33]
    write_moments(workspace, *(moment(i) for i in ids))
    write_parts(workspace, *(parts_record(i, "single", 1, [part(1, 0.0, 3.9)]) for i in ids))

    backend_seq = ConcurrencyBackend(answer())
    run_with_backend(workspace, make_config(tmp_path, parallel=1), backend_seq)
    sequential = read_captions(workspace)

    # Le premier appel a demarrer est le plus lent, le dernier le plus
    # rapide : les reponses arrivent dans le desordre.
    backend_par = ConcurrencyBackend(answer(), delays=[0.2, 0.15, 0.1, 0.05])
    run_with_backend(workspace, make_config(tmp_path, parallel=4), backend_par, force=True)
    parallel = read_captions(workspace)

    assert sequential == parallel
    assert [c["id"] for c in parallel["clips"]] == ["30", "31", "32", "33"]


def test_a_failing_moment_propagates_and_writes_nothing_in_parallel(workspace, tmp_path):
    ids = [40, 41, 42, 43]
    write_moments(workspace, *(moment(i) for i in ids))
    write_parts(workspace, *(parts_record(i, "single", 1, [part(1, 0.0, 3.9)]) for i in ids))
    backend = FailOnceBackend(answer(), fail_after=2)

    with pytest.raises(llm.TransientLLMError):
        run_with_backend(workspace, make_config(tmp_path, parallel=4), backend)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


# --------------------------------------------------------------------------
# Appel a l'abonnement (SPEC-6a47) : cta_line/cta_hashtags, vide par defaut.
# --------------------------------------------------------------------------


def test_cta_line_and_hashtags_are_empty_by_default():
    from clipper.captions import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["cta_line"] == ""
    assert CONFIG_DEFAULTS["cta_hashtags"] == []


def test_default_config_leaves_caption_and_hashtags_unchanged(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    run(workspace, make_config(tmp_path), [answer(caption="Une legende.", hashtags=("#un", "#deux"))])

    clip = by_id(read_captions(workspace), "00")
    assert clip["caption"] == "Une legende."
    assert clip["hashtags"] == ["#un", "#deux"]


def test_cta_line_is_appended_to_every_clips_caption(workspace, tmp_path):
    write_moments(workspace, moment(0), moment(1))
    write_parts(
        workspace,
        parts_record(0, "single", 1, [part(1, 0.0, 3.9)]),
        parts_record(1, "single", 1, [part(1, 0.0, 3.9)]),
    )

    run(workspace, make_config(tmp_path, cta_line="Abonne-toi sur Twitch pour plus de lives !"),
        [answer(caption="Une legende."), answer(caption="Une autre legende.")])

    data = read_captions(workspace)
    assert by_id(data, "00")["caption"] == "Une legende.\nAbonne-toi sur Twitch pour plus de lives !"
    assert by_id(data, "01")["caption"] == "Une autre legende.\nAbonne-toi sur Twitch pour plus de lives !"


def test_cta_hashtags_are_appended_without_duplicating_existing_ones(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    run(workspace, make_config(tmp_path, cta_hashtags=["#Twitch", "#horreur"]),
        [answer(hashtags=("#gta6", "#twitch"))])  # #twitch de l'IA duplique #Twitch (casse ignoree)

    clip = by_id(read_captions(workspace), "00")
    assert clip["hashtags"] == ["#gta6", "#twitch", "#horreur"]


def test_cta_hashtag_without_a_leading_hash_is_an_explicit_error(workspace, tmp_path):
    from clipper.captions import CaptionsError

    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(CaptionsError, match="cta_hashtags"):
        run(workspace, make_config(tmp_path, cta_hashtags=["horreur"]), [answer()])
    assert not (workspace / VIDEO_ID / "captions.json").exists()


@pytest.mark.parametrize("bad", ["#deux mots", "#a,#b"])
def test_cta_hashtag_with_a_space_or_a_comma_is_an_explicit_error(workspace, tmp_path, bad):
    from clipper.captions import CaptionsError

    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(CaptionsError, match="espace ou virgule"):
        run(workspace, make_config(tmp_path, cta_hashtags=[bad]), [answer()])


# --------------------------------------------------------------------------
# caption/hook_text sobres par defaut (TASK-a844), comme screen_title
# --------------------------------------------------------------------------


def test_caption_allow_emoji_defaults_to_false():
    from clipper.captions import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["caption_allow_emoji"] is False


def test_schema_caption_and_hook_text_descriptions_forbid_emoji_by_default():
    from clipper.captions import CONFIG_DEFAULTS, response_schema

    schema = response_schema(CONFIG_DEFAULTS)

    caption_desc = schema["properties"]["caption"]["description"].lower()
    hook_text_desc = schema["properties"]["hook_text"]["description"].lower()
    assert "emoji" in caption_desc and "aucun" in caption_desc
    assert "emoji" in hook_text_desc and "aucun" in hook_text_desc


def test_schema_caption_and_hook_text_descriptions_allow_two_emojis_when_enabled():
    from clipper.captions import CONFIG_DEFAULTS, response_schema

    schema = response_schema({**CONFIG_DEFAULTS, "caption_allow_emoji": True})

    caption_desc = schema["properties"]["caption"]["description"].lower()
    hook_text_desc = schema["properties"]["hook_text"]["description"].lower()
    assert "2 emoji" in caption_desc
    assert "2 emoji" in hook_text_desc


def test_prompt_states_the_sober_caption_rule_by_default(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(workspace, make_config(tmp_path), [answer()])

    prompt = fake.calls[0].prompt
    assert "caption" in prompt and "hook_text" in prompt
    assert "clickbait" in prompt


def test_caption_with_an_emoji_is_refused_by_default_then_fails(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(llm.SchemaError, match="aucun emoji"):
        run(workspace, make_config(tmp_path), [answer(caption="Une legende qui donne envie \U0001F525")] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_hook_text_with_an_emoji_is_refused_by_default_then_fails(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(llm.SchemaError, match="aucun emoji"):
        run(workspace, make_config(tmp_path), [answer(hook_text="regarde ca \U0001F525")] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_refused_caption_emoji_is_sent_back_to_the_llm_with_the_error_and_repaired(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(
        workspace, make_config(tmp_path),
        [answer(caption="Une legende \U0001F525"), answer(caption="Une legende sobre.")],
    )

    assert len(fake.calls) == 2
    assert "aucun emoji" in fake.calls[1].prompt
    assert by_id(read_captions(workspace), "00")["caption"] == "Une legende sobre."


def test_caption_allow_emoji_accepts_up_to_two_emojis(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    two_emojis = "Une legende qui donne envie \U0001F525\U0001F389"

    run(workspace, make_config(tmp_path, caption_allow_emoji=True), [answer(caption=two_emojis)])

    assert by_id(read_captions(workspace), "00")["caption"] == two_emojis


def test_caption_allow_emoji_still_accepts_no_emoji_at_all(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    run(
        workspace, make_config(tmp_path, caption_allow_emoji=True),
        [answer(caption="Une legende sobre.")],
    )

    assert by_id(read_captions(workspace), "00")["caption"] == "Une legende sobre."


def test_caption_allow_emoji_still_refuses_three_emojis(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    three_emojis = "Une legende \U0001F525\U0001F389\U0001F600"

    with pytest.raises(llm.SchemaError, match="au plus 2 emoji"):
        run(workspace, make_config(tmp_path, caption_allow_emoji=True), [answer(caption=three_emojis)] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_hook_text_allow_emoji_still_refuses_three_emojis(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    three_emojis = "regarde ca \U0001F525\U0001F389\U0001F600"

    with pytest.raises(llm.SchemaError, match="au plus 2 emoji"):
        run(workspace, make_config(tmp_path, caption_allow_emoji=True), [answer(hook_text=three_emojis)] * 2)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_cta_line_with_an_emoji_is_not_checked_even_when_caption_allow_emoji_is_false(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    run(
        workspace, make_config(tmp_path, cta_line="Abonne-toi \U0001F525"),
        [answer(caption="Une legende sobre.")],
    )

    assert by_id(read_captions(workspace), "00")["caption"] == "Une legende sobre.\nAbonne-toi \U0001F525"


def test_caption_and_hook_text_emoji_control_leaves_hashtags_unchanged(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    run(
        workspace, make_config(tmp_path, caption_allow_emoji=True),
        [answer(hashtags=["#un", "#deux"])],
    )

    assert by_id(read_captions(workspace), "00")["hashtags"] == ["#un", "#deux"]


# --------------------------------------------------------------------------
# TASK-35a1 : viser plus court que la limite (cible < max), validation = max
# --------------------------------------------------------------------------


def test_config_defaults_carry_word_targets_below_the_maxima():
    from clipper.captions import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["hook_words_target"] == 6
    assert CONFIG_DEFAULTS["hook_words_target"] < CONFIG_DEFAULTS["hook_words_max"]
    assert CONFIG_DEFAULTS["screen_title_words_target"] == 5
    assert CONFIG_DEFAULTS["screen_title_words_target"] < CONFIG_DEFAULTS["screen_title_words_max"]


def test_schema_descriptions_ask_for_the_target_and_not_to_copy_a_long_quote():
    from clipper.captions import CONFIG_DEFAULTS, response_schema

    props = response_schema(CONFIG_DEFAULTS)["properties"]

    hook = props["hook_text"]["description"]
    assert "6 mots" in hook and "recopie" in hook.lower()
    title = props["screen_title"]["description"]
    assert "5 mots" in title and "recopie" in title.lower()


def test_prompt_asks_for_the_target_and_not_to_copy_a_long_quote(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(workspace, make_config(tmp_path), [answer()])

    prompt = fake.calls[0].prompt
    assert "viser 6 mots" in prompt
    assert "viser 5 mots" in prompt
    assert "ne recopie pas une citation" in prompt


def test_hook_correction_message_gives_exact_excess_and_target(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    nine_words = "Il a sorti son flingue, il nous a pointés"

    fake, _ = run(workspace, make_config(tmp_path),
                  [answer(hook_text=nine_words), answer(hook_text="Il a sorti son flingue")])

    assert "9 mots, il en faut 6 au plus : supprime 3 mots" in fake.calls[1].prompt


def test_screen_title_correction_message_gives_exact_excess_and_target(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    seven_words = "un deux trois quatre cinq six sept"

    fake, _ = run(workspace, make_config(tmp_path),
                  [answer(screen_title=seven_words), answer(screen_title="un deux trois")])

    assert "7 mots, il en faut 5 au plus : supprime 2 mots" in fake.calls[1].prompt


def test_fake_backend_answering_nine_words_then_six_passes_without_truncation(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    nine_words = "un deux trois quatre cinq six sept huit neuf"
    six_words = "un deux trois quatre cinq six"

    fake, _ = run(workspace, make_config(tmp_path),
                  [answer(hook_text=nine_words), answer(hook_text=six_words)])

    assert len(fake.calls) == 2
    assert by_id(read_captions(workspace), "00")["hook_text"] == six_words


def test_target_above_max_is_capped_to_max(workspace, tmp_path):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    with pytest.raises(llm.SchemaError, match="3 mots, il en faut 2 au plus : supprime 1 mot"):
        run(workspace, make_config(tmp_path, hook_words_max=2),
            [answer(hook_text="un deux trois")] * 2)


# --------------------------------------------------------------------------
# TASK-b8a0 : la legende commence par les mots-cles de recherche
# --------------------------------------------------------------------------


def _write_meta_full(workspace, **meta):
    (workspace / VIDEO_ID / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _single_prompt(workspace, tmp_path, **captions_overrides):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    fake, _ = run(workspace, make_config(tmp_path, **captions_overrides), [answer()])
    return fake.calls[0].prompt


def test_caption_keywords_first_defaults_to_true():
    from clipper.captions import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["caption_keywords_first"] is True


def test_prompt_requires_game_and_streamer_keywords_in_the_first_sentence(workspace, tmp_path):
    _write_meta_full(workspace, title="Minecraft hardcore jour 3", channel="Leila")

    prompt = _single_prompt(workspace, tmp_path)

    assert "PREMIERE phrase" in prompt
    assert "nom du jeu" in prompt
    assert "Chaine / streamer de la source : Leila" in prompt
    assert "pas une liste de mots-cles" in prompt


def test_prompt_says_the_streamer_is_unknown_when_meta_has_no_channel(workspace, tmp_path):
    write_meta(workspace, title="Minecraft hardcore jour 3")

    prompt = _single_prompt(workspace, tmp_path)

    assert "Chaine / streamer de la source : inconnu" in prompt
    assert "n'invente aucun nom de streamer" in prompt


def test_prompt_tells_claude_not_to_invent_an_unknown_game(workspace, tmp_path):
    write_meta(workspace, title="Une video")

    prompt = _single_prompt(workspace, tmp_path)

    assert "si le jeu n'est identifiable ni dans le titre, ni dans la transcription, ni dans le contexte" in prompt
    assert "n'invente aucun nom de jeu" in prompt


def test_keywords_first_false_gives_the_exact_previous_prompt(workspace, tmp_path):
    _write_meta_full(workspace, title="Minecraft hardcore jour 3", channel="Leila")
    off = _single_prompt(workspace, tmp_path, caption_keywords_first=False)

    assert "PREMIERE phrase" not in off
    assert "Chaine / streamer" not in off
    assert "nom du jeu" not in off
    # la consigne 'caption' est celle d'avant, mot pour mot
    assert (
        "caption : la legende publiee sous le clip, qui donne envie de regarder en entier ; "
        "ton sobre, aucun mot d'emphase clickbait ; aucun emoji.\n4. hashtags"
    ) in off
    assert "Video source : Minecraft hardcore jour 3\nPourquoi" in off


# --------------------------------------------------------------------------
# Essais de reparation du titre d'ecran (TASK-3268) : un titre trop long
# n'arrete plus la video apres une seule reparation
# --------------------------------------------------------------------------

SEVEN_WORDS = "un deux trois quatre cinq six sept"
FIVE_WORDS = "un deux trois quatre cinq"


def _single_moment(workspace):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))


def test_title_repair_attempts_default_is_three():
    from clipper.captions import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["title_repair_attempts"] == 3


def test_screen_title_refused_twice_then_repaired_at_the_third_attempt(workspace, tmp_path):
    _single_moment(workspace)

    fake, _ = run(workspace, make_config(tmp_path),
                  [answer(screen_title=SEVEN_WORDS), answer(screen_title=SEVEN_WORDS),
                   answer(screen_title=FIVE_WORDS)])

    assert len(fake.calls) == 3
    assert read_captions(workspace)["clips"][0]["screen_title"] == FIVE_WORDS


def test_screen_title_still_refused_fails_explicitly_after_one_plus_title_repair_attempts(workspace, tmp_path):
    from clipper.captions import run as run_captions

    _single_moment(workspace)
    fake = FakeBackend([answer(screen_title=SEVEN_WORDS)])

    with llm.use_backend(fake), pytest.raises(llm.SchemaError, match="7 mots"):
        run_captions(VIDEO_ID, workspace, config=make_config(tmp_path, title_repair_attempts=2))

    assert len(fake.calls) == 3
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_title_repair_attempts_default_gives_four_answers_before_failing(workspace, tmp_path):
    from clipper.captions import run as run_captions

    _single_moment(workspace)
    fake = FakeBackend([answer(screen_title=SEVEN_WORDS)])

    with llm.use_backend(fake), pytest.raises(llm.SchemaError, match="7 mots"):
        run_captions(VIDEO_ID, workspace, config=make_config(tmp_path))

    assert len(fake.calls) == 4


def test_title_repair_attempts_overrides_llm_repair_attempts_for_captions(workspace, tmp_path):
    _single_moment(workspace)
    config = Config(
        mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={"llm": {"repair_attempts": 0}},
    )

    fake, _ = run(workspace, config,
                  [answer(screen_title=SEVEN_WORDS), answer(screen_title=SEVEN_WORDS),
                   answer(screen_title=FIVE_WORDS)])

    assert len(fake.calls) == 3
    assert read_captions(workspace)["clips"][0]["screen_title"] == FIVE_WORDS


def test_negative_title_repair_attempts_is_refused_explicitly(workspace, tmp_path):
    from clipper.captions import CaptionsError, run as run_captions

    _single_moment(workspace)
    fake = FakeBackend([answer()])

    with llm.use_backend(fake), pytest.raises(CaptionsError, match="title_repair_attempts"):
        run_captions(VIDEO_ID, workspace, config=make_config(tmp_path, title_repair_attempts=-1))

    assert fake.calls == []


# --------------------------------------------------------------------------
# [captions] title_repair_attempts : validé en tête d'étape (entier >= 0)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [2.7, "abc", True, -1])
def test_invalid_title_repair_attempts_is_an_error_naming_the_setting_before_any_llm_call(workspace, tmp_path, value):
    from clipper.captions import CaptionsError, run as run_captions

    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))
    fake = FakeBackend([answer()])

    with llm.use_backend(fake), pytest.raises(CaptionsError, match="title_repair_attempts"):
        run_captions(VIDEO_ID, workspace, config=make_config(tmp_path, title_repair_attempts=value))

    assert fake.calls == []


@pytest.mark.parametrize("value", [0, 3])
def test_valid_title_repair_attempts_is_accepted(workspace, tmp_path, value):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))

    fake, _ = run(workspace, make_config(tmp_path, title_repair_attempts=value), [answer()])

    assert len(fake.calls) == 1
    assert by_id(read_captions(workspace), "00")["title"] == "Titre choc"


# --------------------------------------------------------------------------
# Termes a risque de moderation TikTok (TASK-bb165541918f)
# --------------------------------------------------------------------------


def _single(workspace):
    write_moments(workspace, moment(0))
    write_parts(workspace, parts_record(0, "single", 1, [part(1, 0.0, 3.9)]))


def test_moderation_risk_terms_default_list():
    from clipper.captions import CONFIG_DEFAULTS

    terms = CONFIG_DEFAULTS["moderation_risk_terms"]
    for expected in ("ban", "bannissement", "triche", "cheat", "hack", "macro", "exploit", "contourner"):
        assert expected in terms
    assert "bot" not in terms  # « bot » = voie du bas en LoL, trop courant


def test_hashtag_with_a_risk_term_is_refused_then_a_clean_answer_is_accepted(workspace, tmp_path):
    _single(workspace)

    fake, _ = run(workspace, make_config(tmp_path),
                  [answer(hashtags=["#Ban", "#jeu"]), answer(hashtags=["#jeu", "#drole"])])

    assert "#Ban" in fake.calls[1].prompt
    assert by_id(read_captions(workspace), "00")["hashtags"] == ["#jeu", "#drole"]


def test_caption_with_a_risk_term_is_refused_then_repaired(workspace, tmp_path):
    _single(workspace)

    fake, _ = run(workspace, make_config(tmp_path),
                  [answer(caption="Les bannissements arrivent."), answer(caption="Il explique sa methode.")])

    assert "bannissements" in fake.calls[1].prompt
    assert by_id(read_captions(workspace), "00")["caption"] == "Il explique sa methode."


@pytest.mark.parametrize("caption", [
    "Il evite les MACROS.", "Un joueur BANNI hier.", "Des logiciels externes interdits.",
    "Un logiciel tiers est present.", "Il a ete BANNÎ hier.", "Il veut contourner la regle.",
])
def test_risk_terms_match_case_and_accent_insensitive_whole_words(workspace, tmp_path, caption):
    _single(workspace)

    with pytest.raises(llm.SchemaError, match="moderation"):
        run(workspace, make_config(tmp_path), [answer(caption=caption)] * 4)


def test_risk_terms_are_whole_words_only(workspace, tmp_path):
    _single(workspace)

    run(workspace, make_config(tmp_path),
        [answer(caption="Une bande de banquiers et un abandon.", hashtags=["#bandit", "#botanique"])])

    assert by_id(read_captions(workspace), "00")["hashtags"] == ["#bandit", "#botanique"]


def test_persistent_risk_term_fails_without_silent_replacement(workspace, tmp_path):
    _single(workspace)

    with pytest.raises(llm.SchemaError, match="moderation"):
        run(workspace, make_config(tmp_path), [answer(hashtags=["#Ban"])] * 6)
    assert not (workspace / VIDEO_ID / "captions.json").exists()


def test_risk_terms_list_is_configurable(workspace, tmp_path):
    _single(workspace)

    run(workspace, make_config(tmp_path, moderation_risk_terms=["pomme"]),
        [answer(caption="Il evite les macros.", hashtags=["#Ban"])])
    assert by_id(read_captions(workspace), "00")["hashtags"] == ["#Ban"]


def test_custom_risk_term_is_refused(workspace, tmp_path):
    _single(workspace)

    with pytest.raises(llm.SchemaError, match="moderation"):
        run(workspace, make_config(tmp_path, moderation_risk_terms=["pomme"]),
            [answer(caption="Une pomme rouge.")] * 6)


def test_screen_title_is_not_checked_for_risk_terms(workspace, tmp_path):
    _single(workspace)

    run(workspace, make_config(tmp_path), [answer(screen_title="Pas de macros ici")])
    assert by_id(read_captions(workspace), "00")["screen_title"] == "Pas de macros ici"


def test_prompt_asks_to_avoid_risk_terms_and_their_hashtags(workspace, tmp_path):
    _single(workspace)

    fake, _ = run(workspace, make_config(tmp_path), [answer()])

    assert "bannissement" in fake.calls[0].prompt and "macro" in fake.calls[0].prompt
