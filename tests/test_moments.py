from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import pytest

from clipper import jury, llm
from clipper.config import Config
from clipper.llm.fake import FakeBackend

REPO = Path(__file__).resolve().parents[1]
VIDEO_ID = "abcdefghijk"

# Grille de test figee : les tests de calcul ne dependent pas des reglages
# que l'utilisateur peut changer dans le rubric.toml du depot.
# max_moments_per_hour et always_keep_score sont volontairement tres larges
# ici : les tests generaux (video de 500 s) ne portent pas sur le plafond,
# qui a ses propres tests plus bas avec CAP_RUBRIC.
TEST_RUBRIC = """
min_score = 60
max_moments_per_hour = 1000
always_keep_score = 1000
min_moments_cap = 1
trend_keywords = ["GTA 6", "Vice City"]

[criteria.hook]
weight = 3
question = "La 1re phrase arrete-t-elle le scroll ?"
[criteria.standalone]
weight = 3
question = "Comprehensible seul ?"
[criteria.payoff]
weight = 2
question = "Arc complet ?"
[criteria.emotion]
weight = 2
question = "Reaction forte ?"
[criteria.value]
weight = 2
question = "Info, leak, avis ?"
[criteria.trend]
weight = 1
question = "Mots-cles tendance ?"

[durations]
single_min = 20
single_max = 45
part_min = 60
part_max = 90
min_parts = 2
max_parts = 12
tolerance = 3

[bonus]
max_total = 6
replayed = 5
audio_peaks = 3
audio_peaks_full = 2
visual = 2

[exclusions]
sponsorblock_categories = ["sponsor", "intro", "outro", "selfpromo"]
"""

# Notes -> (9*3 + 8*3 + 7*2 + 6*2 + 5*2 + 0*1) / 13 * 10 = 66.9
GOOD = {"hook": 9, "standalone": 8, "payoff": 7, "emotion": 6, "value": 5, "trend": 0}
# (7*3 + 7*3 + 6*2 + 6*2 + 5*2 + 1*1) / 13 * 10 = 59.2
WEAK = {"hook": 7, "standalone": 7, "payoff": 6, "emotion": 6, "value": 5, "trend": 1}
# (7*3 + 7*3 + 6*2 + 6*2 + 5*2 + 2*1) / 13 * 10 = 60.0
BORDER = {"hook": 7, "standalone": 7, "payoff": 6, "emotion": 6, "value": 5, "trend": 2}


# --------------------------------------------------------------------------
# Fixtures : une video de 100 phrases de 5 mots ; la phrase k va de
# 5k + 0.25 s a 5k + 4.65 s (dernier mot), 0.6 s de pause entre deux.
# --------------------------------------------------------------------------


def sentence_start(k):
    return 5 * k + 0.25


def sentence_end(k):
    return 5 * k + 4.65


def make_transcript(n=100):
    segments = []
    for k in range(n):
        words = []
        for i in range(5):
            start = sentence_start(k) + i * 0.9
            text = f" mot{k}_{i}" + ("." if i == 4 else "")
            words.append({"word": text, "start": start, "end": start + 0.8, "probability": 0.9})
        segments.append(
            {
                "id": k,
                "start": words[0]["start"],
                "end": words[-1]["end"],
                "text": "".join(w["word"] for w in words),
                "words": words,
            }
        )
    return {"video_id": VIDEO_ID, "language": "fr", "duration": 500.0, "segments": segments}


META = {
    "video_id": VIDEO_ID,
    "title": "GTA 6 : on decortique le trailer",
    "description": "Live du soir",
    "duration": 500.0,
    "channel": "ChaineTest",
    "chapters": [
        {"start_time": 0.0, "end_time": 250.0, "title": "Intro et news"},
        {"start_time": 250.0, "end_time": 500.0, "title": "Analyse du trailer Vice City"},
    ],
    "heatmap": [
        {"start_time": 0.0, "end_time": 250.0, "value": 0.0},
        {"start_time": 250.0, "end_time": 500.0, "value": 0.87},
    ],
    "sponsorblock_segments": [
        {"start_time": 200.0, "end_time": 240.0, "category": "sponsor", "title": "Sponsor", "type": "skip"},
        {"start_time": 400.0, "end_time": 420.0, "category": "interaction", "title": "Interaction", "type": "skip"},
    ],
}

AUDIO = {"window_seconds": 1.0, "energy_db": [], "peaks": [{"timecode": 333.0, "relative_db": 9.5}]}


@pytest.fixture
def video_dir(tmp_path):
    d = tmp_path / "workspace" / VIDEO_ID
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    (d / "transcript.json").write_text(json.dumps(make_transcript()), encoding="utf-8")
    (d / "audio.json").write_text(json.dumps(AUDIO), encoding="utf-8")
    return d


@pytest.fixture
def rubric_path(tmp_path):
    p = tmp_path / "rubric.toml"
    p.write_text(TEST_RUBRIC, encoding="utf-8")
    return p


def make_config(tmp_path, rubric_path, mode="review", **moments):
    return Config(
        mode=mode,
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "output",
        _sections={"moments": {"rubric_path": str(rubric_path), **moments}},
    )


def moment(start, end, scores=GOOD, fmt="single", breaks=(), hook="accroche", why="ca marche"):
    return {
        "start": start,
        "end": end,
        "hook_text": hook,
        "justification": why,
        "format": fmt,
        "part_breaks": list(breaks),
        "scores": dict(scores),
    }


def run(tmp_path, rubric_path, responses, examples=None, config=None, **kwargs):
    from clipper.moments import run as run_moments

    fake = FakeBackend(responses)
    config = config or make_config(tmp_path, rubric_path)
    with llm.use_backend(fake):
        path = run_moments(VIDEO_ID, tmp_path / "workspace", config=config, examples=examples, **kwargs)
    return fake, path


def read_moments(video_dir):
    return json.loads((video_dir / "moments.json").read_text(encoding="utf-8"))


def spans(data):
    return [(m["start"], m["end"]) for m in data["moments"]]


# --------------------------------------------------------------------------
# rubric.toml du depot : conforme a SPEC-0eec
# --------------------------------------------------------------------------


def test_repo_rubric_matches_the_spec():
    from clipper.moments import load_rubric

    rubric = load_rubric(REPO / "rubric.toml")
    assert {name: c["weight"] for name, c in rubric["criteria"].items()} == {
        "hook": 3, "standalone": 3, "payoff": 2, "emotion": 2, "value": 2, "trend": 0,
    }
    assert sum(c["weight"] for c in rubric["criteria"].values()) > 0
    assert all(c["question"].strip() for c in rubric["criteria"].values())
    assert rubric["min_score"] == 60
    assert rubric["max_moments_per_hour"] == 6
    assert rubric["always_keep_score"] == 70
    assert rubric["min_moments_cap"] == 3
    d = rubric["durations"]
    assert (
        d["single_min"], d["single_max"], d["part_min"], d["part_max"], d["min_parts"], d["max_parts"], d["tolerance"]
    ) == (60, 120, 60, 120, 2, 12, 3)
    for keyword in ("GTA 6", "trailer", "date de sortie", "Vice City", "Lucia"):
        assert keyword in rubric["trend_keywords"]
    assert set(rubric["exclusions"]["sponsorblock_categories"]) == {"sponsor", "intro", "outro", "selfpromo"}
    assert {"max_total", "replayed", "audio_peaks", "visual"} <= set(rubric["bonus"])


def test_rubric_without_max_parts_is_refused(tmp_path):
    from clipper.moments import MomentsError, load_rubric

    p = tmp_path / "rubric.toml"
    p.write_text(TEST_RUBRIC.replace("max_parts = 12\n", ""), encoding="utf-8")
    with pytest.raises(MomentsError, match=r"\[durations\] max_parts manquant"):
        load_rubric(p)


@pytest.mark.parametrize("key", ["max_moments_per_hour", "always_keep_score", "min_moments_cap"])
def test_rubric_without_a_cap_setting_is_refused(tmp_path, key):
    from clipper.moments import MomentsError, load_rubric

    p = tmp_path / "rubric.toml"
    p.write_text(re.sub(rf"^{key} = .*\n", "", TEST_RUBRIC, flags=re.MULTILINE), encoding="utf-8")
    with pytest.raises(MomentsError, match=key):
        load_rubric(p)


@pytest.mark.parametrize("key", ["max_moments_per_hour", "always_keep_score", "min_moments_cap"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_rubric_with_a_non_positive_cap_setting_is_refused(tmp_path, key, value):
    from clipper.moments import MomentsError, load_rubric

    p = tmp_path / "rubric.toml"
    p.write_text(re.sub(rf"^{key} = .*$", f"{key} = {value}", TEST_RUBRIC, flags=re.MULTILINE), encoding="utf-8")
    with pytest.raises(MomentsError, match=key):
        load_rubric(p)


@pytest.mark.parametrize("value", ["1.5", "true", '"3"'])
def test_min_moments_cap_must_be_a_positive_integer(tmp_path, value):
    from clipper.moments import MomentsError, load_rubric

    p = tmp_path / "rubric.toml"
    p.write_text(
        re.sub(r"^min_moments_cap = .*$", f"min_moments_cap = {value}", TEST_RUBRIC, flags=re.MULTILINE),
        encoding="utf-8",
    )
    with pytest.raises(MomentsError, match="min_moments_cap"):
        load_rubric(p)


def test_moments_cites_spec_0eec():
    import clipper.moments

    assert "SPEC-0eec" in clipper.moments.__doc__
    assert "SPEC-1557" not in clipper.moments.__doc__ and "SPEC-53f3" not in clipper.moments.__doc__
    header = (REPO / "rubric.toml").read_text(encoding="utf-8").split("\n", 1)[0]
    assert "SPEC-0eec" in header


def test_rubric_missing_a_weight_is_refused(tmp_path):
    from clipper.moments import MomentsError, load_rubric

    p = tmp_path / "rubric.toml"
    p.write_text(TEST_RUBRIC.replace("weight = 1\n", ""), encoding="utf-8")
    with pytest.raises(MomentsError, match="trend"):
        load_rubric(p)


# --------------------------------------------------------------------------
# Ce qui part a clipper.llm (usage moments)
# --------------------------------------------------------------------------


def test_prompt_carries_transcript_and_every_signal(tmp_path, video_dir, rubric_path):
    (video_dir / "vision.json").write_text(
        json.dumps({"frames": [{"timecode": 312.0, "description": "Lucia braque une banque", "striking": True}]}),
        encoding="utf-8",
    )
    examples = [
        {"video_id": "x", "moment": {"start": 1, "end": 30}, "decision": "accepted",
         "texte_moment": "La date de sortie a fuite", "commentaire": "top"},
        {"video_id": "x", "moment": {"start": 50, "end": 80}, "decision": "rejected",
         "texte_moment": "Bon on regarde le chat", "commentaire": "trop mou"},
    ]
    fake, _ = run(tmp_path, rubric_path, [{"moments": []}], examples=examples)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.usage == "moments"
    prompt = call.prompt
    # transcription complete horodatee : premiere et derniere phrase, avec leurs timecodes
    assert "[0.2-4.7] mot0_0 mot0_1 mot0_2 mot0_3 mot0_4." in prompt
    assert "[495.2-499.7] mot99_0" in prompt
    assert all(f"mot{k}_0" in prompt for k in range(100))
    # chapitres, heatmap, pics audio, SponsorBlock, vision, feedback
    assert "Analyse du trailer Vice City" in prompt
    assert "0.87" in prompt
    assert "333.0" in prompt
    assert "sponsor" in prompt and "200.0" in prompt and "240.0" in prompt
    assert "Lucia braque une banque" in prompt
    assert "La date de sortie a fuite" in prompt and "trop mou" in prompt
    # grille et mots-cles tendance
    assert "La 1re phrase arrete-t-elle le scroll ?" in prompt
    assert "Vice City" in prompt
    # schema de reponse : notes par critere, pas de score final demande au LLM
    item = call.schema["properties"]["moments"]["items"]
    assert set(item["properties"]["scores"]["required"]) == {
        "hook", "standalone", "payoff", "emotion", "value", "trend",
    }
    assert "final_score" not in item["properties"]
    assert call.images == []


def test_no_vision_file_is_fine(tmp_path, video_dir, rubric_path):
    fake, _ = run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65)]}])
    assert len(read_moments(video_dir)["moments"]) == 1


# --------------------------------------------------------------------------
# moments.json conforme a la spec (regle 5)
# --------------------------------------------------------------------------


def test_writes_moments_json_with_spec_fields(tmp_path, video_dir, rubric_path):
    _, path = run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65, why="Revelation sur la date")]}])

    assert Path(path) == video_dir / "moments.json"
    data = read_moments(video_dir)
    assert data["video_id"] == VIDEO_ID
    [m] = data["moments"]
    assert (m["start"], m["end"]) == (10.25, 44.65)
    assert m["scores"] == GOOD
    assert m["final_score"] == 66.9
    assert m["justification"] == "Revelation sur la date"
    assert m["hook_text"] == "mot2_0 mot2_1 mot2_2 mot2_3 mot2_4."
    assert m["format"] == "single"
    assert m["parts"] == []


def test_multipart_story_lists_its_parts_on_sentence_ends(tmp_path, video_dir, rubric_path):
    # phrases 50..77 : 250.25 -> 389.65 (139.4 s), coupure demandee vers 320 s
    run(tmp_path, rubric_path, [{"moments": [moment(250.25, 389.65, fmt="multipart", breaks=[320.3])]}])

    [m] = read_moments(video_dir)["moments"]
    assert m["format"] == "multipart"
    assert [(p["start"], p["end"]) for p in m["parts"]] == [(250.2, 319.7), (320.2, 389.7)]


# --------------------------------------------------------------------------
# Score final calcule en Python
# --------------------------------------------------------------------------


def test_final_score_is_weighted_mean_of_criterion_notes(tmp_path, video_dir, rubric_path):
    run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65, scores=GOOD), moment(100.25, 134.65, scores=BORDER)]}])

    scores = sorted(m["final_score"] for m in read_moments(video_dir)["moments"])
    assert scores == [60.0, 66.9]


def test_zero_weight_criterion_is_scored_but_excluded_from_the_mean():
    # SPEC-0eec : trend a poids 0 dans le rubric.toml du depot. Le critere est
    # note (present dans "scores") mais ne compte pas dans la moyenne : le
    # score final ne bouge pas, quelle que soit sa note.
    from clipper.moments import final_score

    rubric = {"criteria": {
        "hook": {"weight": 3, "question": "?"},
        "standalone": {"weight": 3, "question": "?"},
        "trend": {"weight": 0, "question": "?"},
    }}
    without_trend = final_score({"hook": 8, "standalone": 6, "trend": 0}, rubric)
    with_a_high_trend = final_score({"hook": 8, "standalone": 6, "trend": 10}, rubric)

    assert without_trend == with_a_high_trend == round((8 * 3 + 6 * 3) / 6 * 10, 1)


def test_measured_signals_add_a_capped_bonus(tmp_path, video_dir, rubric_path):
    # 300.25 -> 334.65 : heatmap 0.87 partout (4.35), 1 pic audio sur 2 (1.5),
    # une image marquante (2) -> 7.85, plafonne a 6
    (video_dir / "vision.json").write_text(
        json.dumps({"frames": [{"timecode": 312.0, "description": "explosion", "striking": True}]}),
        encoding="utf-8",
    )
    run(tmp_path, rubric_path, [{"moments": [moment(300.25, 334.65, scores=GOOD)]}])

    [m] = read_moments(video_dir)["moments"]
    assert m["final_score"] == 72.9
    assert m["bonus"] == {"replayed": 4.35, "audio_peaks": 1.5, "visual": 2.0, "total": 6.0}


# --------------------------------------------------------------------------
# Regles de la spec
# --------------------------------------------------------------------------


def test_sponsorblock_segments_are_excluded(tmp_path, video_dir, rubric_path):
    run(
        tmp_path,
        rubric_path,
        [{"moments": [
            moment(190.25, 224.65),  # chevauche le sponsor 200-240
            moment(395.25, 429.65),  # chevauche une "interaction" : categorie non exclue
            moment(10.25, 44.65),
        ]}],
    )

    data = read_moments(video_dir)
    assert sorted(spans(data)) == [(10.25, 44.65), (395.25, 429.65)]
    assert any("sponsor" in r["reason"] for r in data["rejected"])


def test_bounds_are_snapped_to_sentence_boundaries(tmp_path, video_dir, rubric_path):
    # 11.7 est dans la phrase 2 (10.25-14.65) : debut le plus proche 10.25 ;
    # 43.1 est dans la phrase 8 (40.25-44.65) : fin la plus proche 44.65
    run(tmp_path, rubric_path, [{"moments": [moment(11.7, 43.1)]}])

    assert spans(read_moments(video_dir)) == [(10.25, 44.65)]


def test_sentence_boundaries_come_from_punctuation_inside_a_segment(tmp_path, video_dir, rubric_path):
    transcript = make_transcript()
    # la phrase 2 se coupe en deux apres son 3e mot : une phrase repart a 12.95
    seg = transcript["segments"][2]
    seg["words"][2]["word"] = " mot2_2?"
    (video_dir / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")

    run(tmp_path, rubric_path, [{"moments": [moment(12.2, 44.65)]}])

    [m] = read_moments(video_dir)["moments"]
    assert m["start"] == 12.95
    assert m["hook_text"] == "mot2_3 mot2_4."


def test_overlapping_moments_keep_the_best_scored(tmp_path, video_dir, rubric_path):
    better = {**GOOD, "hook": 10}
    run(
        tmp_path,
        rubric_path,
        [{"moments": [
            moment(10.25, 44.65, scores=GOOD),
            moment(30.25, 64.65, scores=better),   # recouvre le premier, mieux note
            moment(60.25, 94.65, scores=GOOD),     # recouvre le deuxieme
            moment(150.25, 184.65, scores=GOOD),   # isole
        ]}],
    )

    data = read_moments(video_dir)
    assert sorted(spans(data)) == [(30.25, 64.65), (150.25, 184.65)]
    assert sum("chevauche" in r["reason"] for r in data["rejected"]) == 2


def test_moments_under_min_score_are_dropped(tmp_path, video_dir, rubric_path):
    run(
        tmp_path,
        rubric_path,
        [{"moments": [moment(10.25, 44.65, scores=WEAK), moment(100.25, 134.65, scores=BORDER)]}],
    )

    data = read_moments(video_dir)
    assert spans(data) == [(100.25, 134.65)]
    [rejected] = data["rejected"]
    assert rejected["final_score"] == 59.2 and "min_score" in rejected["reason"]


def test_all_moments_above_min_score_are_kept_without_cap(tmp_path, video_dir, rubric_path):
    # i = 5 tomberait dans le sponsor 200-240
    many = [moment(sentence_start(8 * i), sentence_end(8 * i + 6)) for i in range(12) if i != 5]
    run(tmp_path, rubric_path, [{"moments": many}])
    assert len(read_moments(video_dir)["moments"]) == 11


def test_duration_outside_the_rubric_bounds_is_rejected(tmp_path, video_dir, rubric_path):
    run(
        tmp_path,
        rubric_path,
        [{"moments": [
            moment(10.25, 19.65),                       # single de 9.4 s
            moment(100.25, 174.65),                     # single de 74.4 s
            moment(250.25, 309.65, fmt="multipart"),    # multipart de 59.4 s
            moment(400.25, 434.65),
        ]}],
    )

    data = read_moments(video_dir)
    assert spans(data) == [(400.25, 434.65)]
    assert sum("duree" in r["reason"] for r in data["rejected"]) == 3


# --------------------------------------------------------------------------
# Echecs et cache
# --------------------------------------------------------------------------


def test_invalid_llm_answer_fails_and_writes_nothing(tmp_path, video_dir, rubric_path):
    with pytest.raises(llm.SchemaError):
        run(tmp_path, rubric_path, [{"moments": [{"start": 10.0}]}])
    assert not (video_dir / "moments.json").exists()


def test_missing_input_is_an_error(tmp_path, video_dir, rubric_path):
    from clipper.moments import MomentsError

    (video_dir / "audio.json").unlink()
    with pytest.raises(MomentsError, match="audio.json"):
        run(tmp_path, rubric_path, [])


def test_existing_result_is_not_recomputed_unless_forced(tmp_path, video_dir, rubric_path):
    run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65)]}])
    fake, _ = run(tmp_path, rubric_path, [])
    assert fake.calls == []

    fake, _ = run(tmp_path, rubric_path, [{"moments": []}], force=True)
    assert len(fake.calls) == 1
    assert read_moments(video_dir)["moments"] == []


# --------------------------------------------------------------------------
# Re-notation apres vision (TASK-e493) : moments.json present et vision.json
# plus recent -> bonus visuel, score final, min_score et chevauchement
# recalcules sur les candidats enregistres, sans aucun appel LLM.
# --------------------------------------------------------------------------


def _llm_forbidden(request):
    raise AssertionError(f"appel LLM {request.usage!r} pendant la re-notation")


def write_vision_after_moments(video_dir, frames):
    path = video_dir / "vision.json"
    path.write_text(json.dumps({"frames": frames}), encoding="utf-8")
    later = (video_dir / "moments.json").stat().st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(later, later))


def rescore(tmp_path, rubric_path, **kwargs):
    """Relance l'etape avec un FakeBackend qui echoue s'il est appele."""
    return run(tmp_path, rubric_path, [_llm_forbidden] * 5, **kwargs)


def striking(timecode):
    return {"timecode": timecode, "description": "explosion", "striking": True}


def scored(data):
    """Candidats notes (retenus, et rejetes pour score ou chevauchement), par debut."""
    notes = data["moments"] + [r for r in data["rejected"] if "final_score" in r]
    return sorted(notes, key=lambda m: m["start"])


def test_rescore_after_vision_makes_no_llm_call(tmp_path, video_dir, rubric_path):
    run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65, scores=WEAK), moment(100.25, 134.65)]}])
    write_vision_after_moments(video_dir, [striking(20.0)])

    fake, path = rescore(tmp_path, rubric_path)

    assert fake.calls == []
    assert path == video_dir / "moments.json"
    assert "rescored" in read_moments(video_dir)


def test_rescore_keeps_criterion_notes_bounds_and_justifications(tmp_path, video_dir, rubric_path):
    run(
        tmp_path,
        rubric_path,
        [{"moments": [
            moment(10.25, 44.65, scores=WEAK, hook="faible", why="un peu mou"),
            moment(100.25, 134.65, why="tres bon"),
            moment(250.25, 369.65, fmt="multipart", breaks=[309.65], why="arc complet"),
        ]}],
    )
    before = scored(read_moments(video_dir))
    write_vision_after_moments(video_dir, [striking(20.0), striking(110.0), striking(300.0)])

    rescore(tmp_path, rubric_path)

    after = scored(read_moments(video_dir))
    keys = ("start", "end", "duration", "format", "parts", "scores", "justification", "hook_text")
    assert len(after) == 3
    assert [{k: m[k] for k in keys} for m in after] == [{k: m[k] for k in keys} for m in before]


def test_rescore_adds_the_visual_bonus_and_recomputes_the_final_score(tmp_path, video_dir, rubric_path):
    # 100.25 -> 134.65, aucun signal : 66.9 ; image marquante : +2 -> 68.9
    run(tmp_path, rubric_path, [{"moments": [moment(100.25, 134.65)]}])
    write_vision_after_moments(video_dir, [striking(110.0)])

    rescore(tmp_path, rubric_path)

    [m] = read_moments(video_dir)["moments"]
    assert m["bonus"] == {"replayed": 0.0, "audio_peaks": 0.0, "visual": 2.0, "total": 2.0}
    assert m["final_score"] == 68.9


def test_rescore_keeps_the_measured_bonus_and_caps_the_total(tmp_path, video_dir, rubric_path):
    # 300.25 -> 334.65 : heatmap 4.35 + audio 1.5 = 5.85 -> 66.9 + 5.85 = 72.8 ;
    # + image marquante 2 -> 7.85, plafonne a 6 -> 72.9
    run(tmp_path, rubric_path, [{"moments": [moment(300.25, 334.65)]}])
    assert read_moments(video_dir)["moments"][0]["final_score"] == 72.8
    write_vision_after_moments(video_dir, [striking(312.0)])

    rescore(tmp_path, rubric_path)

    [m] = read_moments(video_dir)["moments"]
    assert m["bonus"] == {"replayed": 4.35, "audio_peaks": 1.5, "visual": 2.0, "total": 6.0}
    assert m["final_score"] == 72.9


def test_rescore_promotes_a_moment_rejected_for_score_and_says_what_changed(tmp_path, video_dir, rubric_path):
    # WEAK = 59.2 < 60 ; avec l'image marquante : 61.2, retenu.
    run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65, scores=WEAK), moment(100.25, 134.65)]}])
    assert spans(read_moments(video_dir)) == [(100.25, 134.65)]
    write_vision_after_moments(video_dir, [striking(20.0)])

    rescore(tmp_path, rubric_path)

    data = read_moments(video_dir)
    assert sorted(spans(data)) == [(10.25, 44.65), (100.25, 134.65)]
    assert not [r for r in data["rejected"] if "min_score" in r["reason"]]
    promoted = next(m for m in data["moments"] if m["start"] == 10.25)
    assert promoted["final_score"] == 61.2
    assert data["rescored"]["changed"] == [{
        "id": promoted["id"],
        "start": 10.25,
        "end": 44.65,
        "hook_text": promoted["hook_text"],
        "before": {"final_score": 59.2, "retained": False},
        "after": {"final_score": 61.2, "retained": True},
    }]


def test_rescore_without_striking_frame_in_a_moment_changes_nothing(tmp_path, video_dir, rubric_path):
    run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65, scores=WEAK), moment(100.25, 134.65)]}])
    first = read_moments(video_dir)
    write_vision_after_moments(video_dir, [striking(80.0), {"timecode": 20.0, "description": "decor", "striking": False}])

    rescore(tmp_path, rubric_path)

    data = read_moments(video_dir)
    assert data["rescored"]["changed"] == []
    assert data["moments"] == first["moments"]
    assert data["rejected"] == first["rejected"]


def test_rescore_recomputes_the_overlap_between_candidates(tmp_path, video_dir, rubric_path):
    # meme note : le premier garde la place, le second est rejete pour
    # chevauchement ; une image marquante dans le second seul le fait passer
    # devant (68.9 contre 66.9).
    run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65), moment(30.25, 64.65)]}])
    assert spans(read_moments(video_dir)) == [(10.25, 44.65)]
    write_vision_after_moments(video_dir, [striking(60.0)])

    rescore(tmp_path, rubric_path)

    data = read_moments(video_dir)
    assert spans(data) == [(30.25, 64.65)]
    [overlap] = [r for r in data["rejected"] if "chevauche" in r["reason"]]
    assert (overlap["start"], overlap["final_score"]) == (10.25, 66.9)
    assert {(c["start"], c["before"]["retained"], c["after"]["retained"]) for c in data["rescored"]["changed"]} == {
        (10.25, True, False), (30.25, False, True),
    }


def test_rescore_leaves_rubric_rejections_untouched(tmp_path, video_dir, rubric_path):
    run(
        tmp_path,
        rubric_path,
        [{"moments": [
            moment(190.25, 224.65),     # sponsor
            moment(10.25, 19.65),       # trop court
            moment(100.25, 134.65),
        ]}],
    )
    first = [r for r in read_moments(video_dir)["rejected"] if "final_score" not in r]
    assert len(first) == 2
    write_vision_after_moments(video_dir, [striking(15.0), striking(200.0)])

    rescore(tmp_path, rubric_path)

    assert [r for r in read_moments(video_dir)["rejected"] if "final_score" not in r] == first


def test_rescore_uses_exact_bounds_so_adjacent_moments_do_not_overlap(tmp_path, video_dir, rubric_path):
    # phrases presque collees (0.02 s d'ecart entre bornes publiees, au
    # centieme pres, regle 5) : la fin d'un moment et le debut du suivant
    # sont si proches qu'une re-derivation approximative (au lieu des bornes
    # exactes que _restore relit depuis transcript.json) risquerait de les
    # faire se chevaucher.
    transcript = make_transcript()
    for k, seg in enumerate(transcript["segments"]):
        for w in seg["words"]:
            w["start"] -= 0.58 * k
            w["end"] -= 0.58 * k
        seg["start"], seg["end"] = seg["words"][0]["start"], seg["words"][-1]["end"]
    (video_dir / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
    segs = transcript["segments"]
    run(tmp_path, rubric_path, [{"moments": [
        moment(segs[2]["start"], segs[8]["end"]), moment(segs[9]["start"], segs[15]["end"]),
    ]}])
    (a_start, a_end), (b_start, b_end) = sorted(spans(read_moments(video_dir)))
    assert 0 < b_start - a_end < 0.05, "le cas teste suppose des bornes publiques presque collees"
    write_vision_after_moments(video_dir, [striking(segs[3]["start"])])

    rescore(tmp_path, rubric_path)

    assert sorted(spans(read_moments(video_dir))) == [(a_start, a_end), (b_start, b_end)]


def test_rescore_refuses_bounds_that_are_not_sentence_boundaries(tmp_path, video_dir, rubric_path):
    from clipper.moments import MomentsError

    run(tmp_path, rubric_path, [{"moments": [moment(100.25, 134.65)]}])
    data = read_moments(video_dir)
    data["moments"][0]["start"] = 102.0
    (video_dir / "moments.json").write_text(json.dumps(data), encoding="utf-8")
    write_vision_after_moments(video_dir, [striking(110.0)])

    with pytest.raises(MomentsError, match="102.0"):
        rescore(tmp_path, rubric_path)


def test_older_vision_file_does_not_trigger_a_rescore(tmp_path, video_dir, rubric_path):
    (video_dir / "vision.json").write_text(json.dumps({"frames": [striking(110.0)]}), encoding="utf-8")
    run(tmp_path, rubric_path, [{"moments": [moment(100.25, 134.65)]}])
    before = (video_dir / "moments.json").read_bytes()

    fake, _ = rescore(tmp_path, rubric_path)

    assert fake.calls == []
    assert (video_dir / "moments.json").read_bytes() == before


def test_forced_run_after_vision_asks_the_llm_again(tmp_path, video_dir, rubric_path):
    run(tmp_path, rubric_path, [{"moments": [moment(100.25, 134.65)]}])
    write_vision_after_moments(video_dir, [striking(110.0)])

    fake, _ = run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65)]}], force=True)

    assert [c.usage for c in fake.calls] == ["moments"]
    data = read_moments(video_dir)
    assert spans(data) == [(10.25, 44.65)]
    assert "rescored" not in data


# --------------------------------------------------------------------------
# Transcription trop longue : tranches avec recouvrement puis comparaison
# --------------------------------------------------------------------------


def test_long_transcript_goes_in_overlapping_chunks_then_a_comparison_round(tmp_path, video_dir, rubric_path):
    config = make_config(tmp_path, rubric_path, max_transcript_chars=2000, chunk_chars=2000, chunk_overlap_seconds=30)

    def dispatch(request):
        if "Candidats" in request.prompt:
            # tour final : re-note tout ; le premier remonte au-dessus de min_score
            assert "id 0" in request.prompt and "id 1" in request.prompt
            return {"moments": [
                {"id": 0, "justification": "revu", "scores": GOOD},
                {"id": 1, "justification": "revu", "scores": WEAK},
            ]}
        # chaque tranche propose un moment dans sa propre portion
        if "[10.2-14.7]" in request.prompt:
            return {"moments": [moment(10.25, 44.65, scores=WEAK)]}
        if "[450.2-454.7]" in request.prompt:
            return {"moments": [moment(450.25, 484.65, scores=GOOD)]}
        return {"moments": []}

    from clipper.moments import run as run_moments

    fake = FakeBackend([dispatch] * 30)
    with llm.use_backend(fake):
        run_moments(VIDEO_ID, tmp_path / "workspace", config=config)

    chunk_calls = [c for c in fake.calls if "Candidats" not in c.prompt]
    final_calls = [c for c in fake.calls if "Candidats" in c.prompt]
    assert len(chunk_calls) >= 2
    assert len(final_calls) == 1
    # chaque phrase apparait dans au moins une tranche, et des tranches se recouvrent
    seen = [sum(f"mot{k}_0 " in c.prompt for c in chunk_calls) for k in range(100)]
    assert min(seen) >= 1 and max(seen) >= 2

    # les notes retenues sont celles du tour final, pas celles des tranches
    data = read_moments(video_dir)
    by_start = sorted(data["moments"], key=lambda m: m["start"])
    assert [(m["start"], m["scores"]) for m in by_start] == [(10.25, GOOD), (450.25, WEAK)]
    assert by_start[0]["final_score"] == 66.9
    assert data["chunked"] is True


def test_comparison_round_must_rescore_every_candidate(tmp_path, video_dir, rubric_path):
    from clipper.moments import run as run_moments

    config = make_config(tmp_path, rubric_path, max_transcript_chars=2000, chunk_chars=2000, chunk_overlap_seconds=30)

    def dispatch(request):
        if "Candidats" in request.prompt:
            # deux notes, mais l'id 1 manque (l'id 0 est en double)
            return {"moments": [
                {"id": 0, "justification": "x", "scores": GOOD},
                {"id": 0, "justification": "y", "scores": GOOD},
            ]}
        if "[10.2-14.7]" in request.prompt:
            return {"moments": [moment(10.25, 44.65)]}
        if "[450.2-454.7]" in request.prompt:
            return {"moments": [moment(450.25, 484.65)]}
        return {"moments": []}

    fake = FakeBackend([dispatch] * 30)
    with llm.use_backend(fake), pytest.raises(llm.SchemaError, match=r"manquants \[1\]"):
        run_moments(VIDEO_ID, tmp_path / "workspace", config=config)
    assert not (video_dir / "moments.json").exists()


# --------------------------------------------------------------------------
# Selection par le jury (TASK-8e2f, ADR-ff87) : en mode auto ou avec
# [moments] selection = "jury", le proposeur donne les candidats, le jury
# (clipper.jury, 5 juges par defaut) les note ; ses notes agregees remplacent
# celles du proposeur.
# --------------------------------------------------------------------------

JUDGES = {"retention", "spectateur", "monteur", "avocat", "conformite"}
# Candidat vu par un juge : "### C3\nContexte : ...\nTexte : « motK_0 ..." ;
# K, la premiere phrase du candidat, dit de quel moment il s'agit.
_JURY_BLOCK = re.compile(r"### (C\d+)\n(?:Contexte : [^\n]*\n)?Texte : « mot(\d+)_0 ")


def jury_notes(notes, vetoes=None, debate=None, confidence=None):
    """Reponse factice d'un juge. ``notes[k]`` : notes du candidat qui
    commence a la phrase k, soit une grille (tous les juges), soit
    {juge: grille} ; ``vetoes[k]`` : raison du veto de conformite ;
    ``debate[k]`` : {juge: grille} renvoye au tour 2 ; ``confidence`` :
    {(k, juge, tour): confiance}, 80 par defaut."""
    vetoes, debate, confidence = vetoes or {}, debate or {}, confidence or {}

    def answer(request):
        judge = request.usage.removeprefix("jury_")
        second_round = "## Debat" in request.prompt
        refs = {ref: int(k) for ref, k in _JURY_BLOCK.findall(request.prompt)}
        item = request.schema["properties"]["candidates"]["items"]["properties"]
        out = []
        for ref in item["ref"]["enum"]:
            k = refs[ref]
            grid = notes[k] if "hook" in notes[k] else notes[k][judge]
            if second_round and judge in debate.get(k, {}):
                grid = debate[k][judge]
            entry = {
                "ref": ref,
                "argument": f"{judge} sur {ref}",
                "scores": dict(grid),
                "confidence": confidence.get((k, judge, 2 if second_round else 1), 80),
            }
            if "veto" in item:
                entry["veto"] = k in vetoes
                entry["veto_reason"] = vetoes.get(k, "")
            out.append(entry)
        return {"candidates": out}

    return answer


def with_jury(proposal, judges):
    """Un seul script pour le proposeur (usage moments) et les juges."""

    def dispatch(request):
        if request.usage == "moments":
            return proposal(request) if callable(proposal) else proposal
        return judges(request)

    return [dispatch]


def auto_config(tmp_path, rubric_path, **moments):
    return make_config(tmp_path, rubric_path, mode="auto", **moments)


def test_auto_mode_has_the_jury_rate_the_proposed_candidates(tmp_path, video_dir, rubric_path):
    proposal = {"moments": [moment(10.25, 44.65, scores=WEAK), moment(100.25, 134.65, scores=GOOD)]}
    fake, _ = run(
        tmp_path, rubric_path, with_jury(proposal, jury_notes({2: GOOD, 20: WEAK})),
        config=auto_config(tmp_path, rubric_path),
    )

    assert fake.calls[0].usage == "moments"
    jury_calls = fake.calls[1:]
    assert {c.usage for c in jury_calls} == {f"jury_{j}" for j in JUDGES}
    assert len(jury_calls) == 5  # tous d'accord : pas de debat
    assert all("mot2_0" in c.prompt and "mot20_0" in c.prompt for c in jury_calls)
    assert all("La 1re phrase arrete-t-elle le scroll ?" in c.prompt for c in jury_calls)
    data = read_moments(video_dir)
    assert data["selection"] == "jury"
    [m] = data["moments"]
    assert (m["start"], m["scores"], m["final_score"]) == (10.25, GOOD, 66.9)
    [low] = [r for r in data["rejected"] if "min_score" in r["reason"]]
    assert (low["start"], low["final_score"]) == (100.25, 59.2)


def test_jury_median_replaces_the_proposer_notes(tmp_path, video_dir, rubric_path):
    # proposeur : GOOD (66.9) ; jury : mediane par critere de GOOD, GOOD,
    # WEAK, WEAK, BORDER = WEAK (59.2) -> sous min_score.
    split = {"retention": GOOD, "spectateur": GOOD, "monteur": WEAK, "avocat": WEAK, "conformite": BORDER}
    run(
        tmp_path, rubric_path, with_jury({"moments": [moment(10.25, 44.65, scores=GOOD)]}, jury_notes({2: split})),
        config=auto_config(tmp_path, rubric_path),
    )

    data = read_moments(video_dir)
    assert data["moments"] == []
    [r] = data["rejected"]
    assert (r["scores"], r["final_score"]) == (WEAK, 59.2)
    assert r["jury"]["proposer"]["scores"] == GOOD


def test_jury_notes_keep_bonus_min_score_and_overlap_rules(tmp_path, video_dir, rubric_path):
    better = {**GOOD, "hook": 10}
    proposal = {"moments": [
        moment(300.25, 334.65, scores=GOOD),   # bonus mesure 5.85
        moment(10.25, 44.65, scores=better),
        moment(30.25, 64.65, scores=GOOD),     # recouvre le precedent
    ]}
    # jury : WEAK (59.2) + 5.85 = 65.1, retenu grace au bonus ; le
    # chevauchement se decide sur les notes du jury (inversees).
    run(
        tmp_path, rubric_path, with_jury(proposal, jury_notes({60: WEAK, 2: GOOD, 6: better})),
        config=auto_config(tmp_path, rubric_path),
    )

    data = read_moments(video_dir)
    assert sorted(spans(data)) == [(30.25, 64.65), (300.25, 334.65)]
    bonus = next(m for m in data["moments"] if m["start"] == 300.25)
    assert bonus["bonus"] == {"replayed": 4.35, "audio_peaks": 1.5, "visual": 0.0, "total": 5.85}
    assert bonus["final_score"] == 65.1
    [overlap] = [r for r in data["rejected"] if "chevauche" in r["reason"]]
    assert (overlap["start"], overlap["final_score"]) == (10.25, 66.9)


def test_jury_veto_rejects_the_candidate_with_its_reason(tmp_path, video_dir, rubric_path):
    proposal = {"moments": [moment(10.25, 44.65), moment(100.25, 134.65)]}
    reason = "diffamation : accuse nommement un developpeur de vol"
    run(
        tmp_path, rubric_path, with_jury(proposal, jury_notes({2: GOOD, 20: GOOD}, vetoes={20: reason})),
        config=auto_config(tmp_path, rubric_path),
    )

    data = read_moments(video_dir)
    assert spans(data) == [(10.25, 44.65)]
    [vetoed] = [r for r in data["rejected"] if r["start"] == 100.25]
    assert "veto" in vetoed["reason"] and "conformite" in vetoed["reason"] and reason in vetoed["reason"]
    assert vetoed["jury"]["veto"] == {"judge": "conformite", "reason": reason}
    # pas de score final : la re-notation apres vision ne peut pas le reprendre
    assert "final_score" not in vetoed


def test_moments_json_holds_the_jury_trace_of_every_candidate(tmp_path, video_dir, rubric_path):
    # l'avocat descend le premier candidat (tout a 1) puis se laisse
    # convaincre au debat ; le second, rejete sous min_score, a aussi sa trace.
    ones = dict.fromkeys(GOOD, 1)
    notes = {2: {**dict.fromkeys(JUDGES, GOOD), "avocat": ones}, 20: WEAK}
    proposal = {"moments": [moment(10.25, 44.65, why="accroche forte"), moment(100.25, 134.65)]}
    fake, _ = run(
        tmp_path, rubric_path, with_jury(proposal, jury_notes(notes, debate={2: {"avocat": GOOD}})),
        config=auto_config(tmp_path, rubric_path),
    )

    assert len(fake.calls) == 1 + 5 + 5  # proposeur, tour 1, debat
    data = read_moments(video_dir)
    assert [j["name"] for j in data["jury"]["judges"]] == ["retention", "spectateur", "monteur", "avocat", "conformite"]
    assert data["jury"]["failed"] == []
    [m] = data["moments"]
    assert m["justification"] == "accroche forte"
    trace = m["jury"]
    assert trace["proposer"] == {"scores": GOOD, "justification": "accroche forte"}
    assert trace["debated"] is True and trace["veto"] is None and trace["score"] == 66.9
    [first, second] = trace["trace"]["rounds"]
    assert set(first["judges"]) == JUDGES
    assert first["judges"]["avocat"]["scores"] == ones
    assert first["judges"]["avocat"]["argument"].startswith("avocat sur C")
    assert second["judges"]["avocat"]["scores"] == GOOD
    assert {(r["judge"], r["criterion"], r["from"], r["to"]) for r in trace["trace"]["revisions"]} >= {
        ("avocat", "hook", 1, 9),
    }
    [low] = [r for r in data["rejected"] if "min_score" in r["reason"]]
    assert low["jury"]["debated"] is False
    assert set(low["jury"]["trace"]["rounds"][0]["judges"]) == JUDGES


def test_moments_json_keeps_each_judges_confidence_per_round_and_the_aggregate(tmp_path, video_dir, rubric_path):
    # avocat peu sur au tour 1 (30 < 40) : debat ; tour 2 : confiances 60..100.
    conf = {(2, "avocat", 1): 30}
    conf.update({(2, j, 2): c for j, c in zip(["retention", "spectateur", "monteur", "avocat", "conformite"],
                                              [60, 70, 80, 90, 100], strict=True)})
    proposal = {"moments": [moment(10.25, 44.65), moment(100.25, 134.65)]}
    notes = {2: GOOD, 20: GOOD}
    run(
        tmp_path, rubric_path, with_jury(proposal, jury_notes(notes, confidence=conf)),
        config=auto_config(tmp_path, rubric_path),
    )
    data = read_moments(video_dir)
    first, second = data["moments"][0], data["moments"][1]
    assert data["jury"]["debate_confidence_below"] == 40
    assert first["jury"]["debated"] is True
    assert first["jury"]["confidence"] == 80
    r1, r2 = first["jury"]["trace"]["rounds"]
    assert r1["judges"]["avocat"]["confidence"] == 30
    assert r1["judges"]["retention"]["confidence"] == 80
    assert r2["judges"]["avocat"]["confidence"] == 90
    assert second["jury"]["debated"] is False
    assert second["jury"]["confidence"] == 80


def test_review_mode_keeps_the_single_call_by_default(tmp_path, video_dir, rubric_path):
    fake, _ = run(tmp_path, rubric_path, [{"moments": [moment(10.25, 44.65)]}])

    assert [c.usage for c in fake.calls] == ["moments"]
    data = read_moments(video_dir)
    assert data["selection"] == "single"
    assert "jury" not in data and "jury" not in data["moments"][0]


def test_selection_jury_calls_the_jury_in_review_mode(tmp_path, video_dir, rubric_path):
    fake, _ = run(
        tmp_path, rubric_path, with_jury({"moments": [moment(10.25, 44.65)]}, jury_notes({2: WEAK})),
        config=make_config(tmp_path, rubric_path, selection="jury"),
    )

    assert {c.usage for c in fake.calls[1:]} == {f"jury_{j}" for j in JUDGES}
    data = read_moments(video_dir)
    assert data["selection"] == "jury"
    assert data["moments"] == []


def test_auto_mode_uses_the_jury_even_with_selection_single(tmp_path, video_dir, rubric_path):
    # ADR-ff87 : en mode auto, la selection passe toujours par le jury.
    fake, _ = run(
        tmp_path, rubric_path, with_jury({"moments": [moment(10.25, 44.65)]}, jury_notes({2: GOOD})),
        config=auto_config(tmp_path, rubric_path, selection="single"),
    )

    assert len([c for c in fake.calls if c.usage.startswith("jury_")]) == 5
    assert read_moments(video_dir)["selection"] == "jury"


def test_unknown_selection_is_refused(tmp_path, video_dir, rubric_path):
    from clipper.moments import MomentsError

    with pytest.raises(MomentsError, match="selection"):
        run(tmp_path, rubric_path, [{"moments": []}], config=make_config(tmp_path, rubric_path, selection="vote"))
    assert not (video_dir / "moments.json").exists()


def test_invalid_judge_answer_fails_and_writes_nothing(tmp_path, video_dir, rubric_path):
    good = jury_notes({2: GOOD})

    def judges(request):
        return {"candidates": []} if request.usage == "jury_monteur" else good(request)

    with pytest.raises(llm.SchemaError):
        run(
            tmp_path, rubric_path, with_jury({"moments": [moment(10.25, 44.65)]}, judges),
            config=auto_config(tmp_path, rubric_path),
        )
    assert not (video_dir / "moments.json").exists()


def test_long_transcript_with_the_jury_skips_the_comparison_round(tmp_path, video_dir, rubric_path):
    config = auto_config(tmp_path, rubric_path, max_transcript_chars=2000, chunk_chars=2000, chunk_overlap_seconds=30)

    def proposal(request):
        assert "id 0" not in request.prompt, "tour de comparaison appele alors que le jury note"
        if "[10.2-14.7]" in request.prompt:
            return {"moments": [moment(10.25, 44.65, scores=WEAK)]}
        if "[450.2-454.7]" in request.prompt:
            return {"moments": [moment(450.25, 484.65, scores=GOOD)]}
        return {"moments": []}

    fake, _ = run(tmp_path, rubric_path, with_jury(proposal, jury_notes({2: GOOD, 90: WEAK})), config=config)

    jury_calls = [c for c in fake.calls if c.usage != "moments"]
    assert len(jury_calls) == 5
    assert all("mot2_0" in c.prompt and "mot90_0" in c.prompt for c in jury_calls)
    data = read_moments(video_dir)
    assert data["chunked"] is True
    by_start = sorted(data["moments"], key=lambda m: m["start"])
    assert [(m["start"], m["scores"]) for m in by_start] == [(10.25, GOOD), (450.25, WEAK)]


def test_rescore_after_vision_keeps_the_jury_trace_and_the_veto(tmp_path, video_dir, rubric_path):
    config = auto_config(tmp_path, rubric_path)
    proposal = {"moments": [moment(10.25, 44.65), moment(100.25, 134.65)]}
    run(tmp_path, rubric_path, with_jury(proposal, jury_notes({2: GOOD, 20: GOOD}, vetoes={20: "droits"})), config=config)
    trace = read_moments(video_dir)["moments"][0]["jury"]
    write_vision_after_moments(video_dir, [striking(20.0), striking(110.0)])

    fake, _ = rescore(tmp_path, rubric_path, config=config)

    assert fake.calls == []
    data = read_moments(video_dir)
    [m] = data["moments"]
    assert (m["start"], m["final_score"]) == (10.25, 68.9)
    assert m["jury"] == trace
    [vetoed] = [r for r in data["rejected"] if r["start"] == 100.25]
    assert "veto" in vetoed["reason"]


# --------------------------------------------------------------------------
# Exploration (TASK-022d, ADR-1cf0 point 4) : en selection par jury, une part
# des clips retenus (defaut 10 %) est prise parmi les candidats non retenus
# ou les notes du jury divergent le plus, marques exploration: true.
# --------------------------------------------------------------------------

LOW = dict.fromkeys(GOOD, 3)    # 30.0
MID = dict.fromkeys(GOOD, 5)    # 50.0
TOP = dict.fromkeys(GOOD, 10)   # 100.0


def clip(k, scores=GOOD):
    """Candidat de 5 phrases (k a k+4, 24.4 s) : moment(5k+0.25, 5k+24.65)."""
    return moment(sentence_start(k), sentence_end(k + 4), scores=scores)


def split(high):
    """Le juge retention donne ``high``, les autres LOW : mediane LOW (rejete
    sous min_score), dispersion = score(high) - 30."""
    return {**dict.fromkeys(JUDGES, LOW), "retention": high}


def explored(data):
    return [m for m in data["moments"] if m.get("exploration") is True]


def run_jury(tmp_path, rubric_path, ks, notes, vetoes=None, debate=None, **moments):
    proposal = {"moments": [clip(k) for k in ks]}
    return run(
        tmp_path, rubric_path, with_jury(proposal, jury_notes(notes, vetoes=vetoes, debate=debate)),
        config=auto_config(tmp_path, rubric_path, **moments), force=True,
    )


def test_exploration_takes_the_rejected_candidate_where_the_jury_disagrees_most(tmp_path, video_dir, rubric_path):
    # 2 retenus x 0.5 = 1 clip d'exploration ; dispersions : 20 -> 0,
    # 30 -> 20, 50 -> 36.9, 60 -> 70.
    notes = {0: GOOD, 10: GOOD, 20: LOW, 30: split(MID), 50: split(GOOD), 60: split(TOP)}
    run_jury(tmp_path, rubric_path, list(notes), notes, exploration_share=0.5)

    data = read_moments(video_dir)
    assert [(m["start"], m.get("exploration")) for m in data["moments"]] == [
        (0.25, None), (50.25, None), (300.25, True),
    ]
    [x] = explored(data)
    assert x["scores"] == LOW and x["final_score"] < 60
    assert x["jury"]["trace"]["rounds"][0]["judges"]["retention"]["scores"] == TOP
    assert 300.25 not in [r["start"] for r in data["rejected"]]
    assert data["exploration"] == {"share": 0.5, "seed": 0, "target": 1, "chosen": 1}


def test_exploration_measures_the_dispersion_after_the_debate(tmp_path, video_dir, rubric_path):
    # 60 divergeait le plus au tour 1 (70) mais le debat a rapproche les
    # juges (0) ; 50 reste a 36.9.
    notes = {0: GOOD, 10: GOOD, 50: split(GOOD), 60: split(TOP)}
    run_jury(tmp_path, rubric_path, list(notes), notes, debate={60: {"retention": LOW}}, exploration_share=0.5)

    [x] = explored(read_moments(video_dir))
    assert x["start"] == 250.25


def test_exploration_never_takes_a_vetoed_candidate(tmp_path, video_dir, rubric_path):
    notes = {0: GOOD, 10: GOOD, 50: split(GOOD), 60: split(TOP)}
    run_jury(tmp_path, rubric_path, list(notes), notes, vetoes={60: "droits"}, exploration_share=0.5)

    data = read_moments(video_dir)
    [x] = explored(data)
    assert x["start"] == 250.25
    [vetoed] = [r for r in data["rejected"] if r["start"] == 300.25]
    assert "veto" in vetoed["reason"]


def test_exploration_never_overlaps_a_retained_clip_nor_another_exploration(tmp_path, video_dir, rubric_path):
    # dispersions : 2 -> 70 (chevauche le retenu 0), 60 -> 70, 62 -> 36.9
    # (chevauche 60), 50 -> 20. Deux clips d'exploration : 60 puis 50.
    notes = {0: GOOD, 10: GOOD, 2: split(TOP), 50: split(MID), 60: split(TOP), 62: split(GOOD)}
    run_jury(tmp_path, rubric_path, list(notes), notes, exploration_share=1.0)

    data = read_moments(video_dir)
    assert sorted(m["start"] for m in explored(data)) == [250.25, 300.25]
    rejected = {r["start"] for r in data["rejected"]}
    assert {10.25, 310.25} <= rejected


def test_exploration_ignores_sponsorblock_and_duration_rejections_and_says_when_it_falls_short(
    tmp_path, video_dir, rubric_path
):
    notes = {0: GOOD, 38: split(TOP), 60: split(TOP)}
    proposal = {"moments": [
        clip(0),
        clip(38),                                                  # SponsorBlock sponsor 200-240
        moment(sentence_start(60), sentence_end(70), scores=GOOD),  # 54.4 s : trop long pour single
    ]}
    run(
        tmp_path, rubric_path, with_jury(proposal, jury_notes(notes)),
        config=auto_config(tmp_path, rubric_path, exploration_share=1.0),
    )

    data = read_moments(video_dir)
    assert explored(data) == []
    assert spans(data) == [(0.25, 24.65)]
    assert data["exploration"] == {"share": 1.0, "seed": 0, "target": 1, "chosen": 0}


@pytest.mark.parametrize(
    ("share", "retained", "target"),
    [(None, 10, 1), (None, 4, 0), (0.25, 2, 1), (0.2, 2, 0), (0.15, 10, 2)],
)
def test_exploration_count_is_the_rounded_share_of_retained_clips(tmp_path, video_dir, rubric_path, share, retained, target):
    good = [0, 5, 10, 15, 20, 25, 30, 35, 48, 53][:retained]
    disputed = [58, 63, 68, 73, 78, 83, 88, 93]
    notes = {**dict.fromkeys(good, GOOD), **dict.fromkeys(disputed, split(TOP))}
    extra = {} if share is None else {"exploration_share": share}
    run_jury(tmp_path, rubric_path, list(notes), notes, **extra)

    data = read_moments(video_dir)
    assert len(data["moments"]) == retained + target
    assert len(explored(data)) == target
    assert data["exploration"]["share"] == (0.1 if share is None else share)
    assert data["exploration"]["target"] == target


def test_exploration_choice_is_deterministic_for_a_fixed_seed(tmp_path, video_dir, rubric_path):
    # 50, 60, 70 : meme dispersion (70) ; un seul clip d'exploration.
    notes = {0: GOOD, 10: GOOD, 50: split(TOP), 60: split(TOP), 70: split(TOP)}

    def chosen(seed):
        run_jury(tmp_path, rubric_path, list(notes), notes, exploration_share=0.5, exploration_seed=seed)
        [x] = explored(read_moments(video_dir))
        return x["start"]

    assert chosen(7) == chosen(7) == chosen(7)
    assert {chosen(seed) for seed in range(12)} == {250.25, 300.25, 350.25}
    assert read_moments(video_dir)["exploration"]["seed"] == 11


def test_exploration_share_zero_leaves_the_selection_unchanged(tmp_path, video_dir, rubric_path):
    notes = {0: GOOD, 10: GOOD, 50: split(GOOD), 60: split(TOP)}
    run_jury(tmp_path, rubric_path, list(notes), notes, exploration_share=0)

    data = read_moments(video_dir)
    assert "exploration" not in data
    assert spans(data) == [(0.25, 24.65), (50.25, 74.65)]
    assert all("exploration" not in m for m in data["moments"] + data["rejected"])
    assert sorted(r["start"] for r in data["rejected"]) == [250.25, 300.25]


def test_single_selection_has_no_exploration(tmp_path, video_dir, rubric_path):
    run(
        tmp_path, rubric_path, [{"moments": [clip(0), clip(10), clip(60, scores=LOW)]}],
        config=make_config(tmp_path, rubric_path, exploration_share=1.0),
    )

    data = read_moments(video_dir)
    assert "exploration" not in data
    assert explored(data) == [] and len(data["moments"]) == 2


@pytest.mark.parametrize("share", [-0.1, 1.5, "10%", True])
def test_invalid_exploration_share_is_refused(tmp_path, video_dir, rubric_path, share):
    from clipper.moments import MomentsError

    notes = {0: GOOD}
    with pytest.raises(MomentsError, match="exploration_share"):
        run_jury(tmp_path, rubric_path, list(notes), notes, exploration_share=share)
    assert not (video_dir / "moments.json").exists()


def test_rescore_after_vision_keeps_the_exploration(tmp_path, video_dir, rubric_path):
    notes = {0: GOOD, 10: GOOD, 50: split(GOOD), 60: split(TOP)}
    run_jury(tmp_path, rubric_path, list(notes), notes, exploration_share=0.5)
    write_vision_after_moments(video_dir, [striking(5.0)])

    fake, _ = rescore(tmp_path, rubric_path, config=auto_config(tmp_path, rubric_path, exploration_share=0.5))

    assert fake.calls == []
    data = read_moments(video_dir)
    assert [(m["start"], m.get("exploration")) for m in data["moments"]] == [
        (0.25, None), (50.25, None), (300.25, True),
    ]
    assert data["exploration"] == {"share": 0.5, "seed": 0, "target": 1, "chosen": 1}
    assert data["rescored"]["changed"][0]["start"] == 0.25


# --------------------------------------------------------------------------
# Connecteurs de tete (TASK-3170) : essai reel sZi-qJ-5ptA, clips rejetes par
# qa en starts_mid_sentence ("Donc deja il m'a menti...", "Mais quand on
# fait un zoom..."). Reculer d'une phrase mettrait la mise en contexte en
# tete (SPEC-1557 regle 1) : on retire les connecteurs, le clip commence au
# premier mot qui suit, une seule fois dans _normalize (avant le jury).
# --------------------------------------------------------------------------


def set_sentence(video_dir, k, text):
    """Reecrit la phrase k de transcript.json avec les mots de ``text``,
    repartis de sentence_start(k) a sentence_end(k) ; renvoie le debut de
    chaque mot."""
    transcript = json.loads((video_dir / "transcript.json").read_text(encoding="utf-8"))
    words = text.split()
    step = (sentence_end(k) - sentence_start(k)) / len(words)
    starts = [sentence_start(k) + i * step for i in range(len(words))]
    seg = transcript["segments"][k]
    seg["words"] = [
        {"word": " " + w, "start": s, "end": s + step - 0.1, "probability": 0.9} for w, s in zip(words, starts)
    ]
    seg["words"][-1]["end"] = sentence_end(k)
    seg["text"] = " " + text
    (video_dir / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
    return starts


def floor1(x):
    return math.floor(x * 10 + 1e-6) / 10


def round2(x):
    """Borne publiee (SPEC-1557 regle 5, TASK-3c0a) : centieme superieur."""
    return math.ceil(x * 100 - 1e-6) / 100


def test_leading_connector_is_cut_and_the_clip_starts_on_the_next_word(tmp_path, video_dir, rubric_path):
    starts = set_sentence(video_dir, 3, "Mais qui est vraiment X ?")

    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65)]}])

    data = read_moments(video_dir)
    [m] = data["moments"]
    assert (m["start"], m["end"]) == (round2(starts[1]), 44.65)
    assert m["hook_text"] == "qui est vraiment X ?"
    assert m["duration"] == round(44.65 - starts[1], 1)
    assert data["rejected"] == []


def test_several_leading_connectors_are_all_cut(tmp_path, video_dir, rubric_path):
    starts = set_sentence(video_dir, 3, "Du coup, en fait on part.")

    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65)]}])

    [m] = read_moments(video_dir)["moments"]
    assert m["start"] == round2(starts[4])
    assert m["hook_text"] == "on part."


@pytest.mark.parametrize("text", ["Donc.", "Mais, du coup...", "« Et donc ? »"])
def test_sentence_made_only_of_connectors_is_rejected_with_its_reason(tmp_path, video_dir, rubric_path, text):
    set_sentence(video_dir, 3, text)

    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65), moment(100.25, 134.65)]}])

    data = read_moments(video_dir)
    assert spans(data) == [(100.25, 134.65)]
    [rejected] = data["rejected"]
    assert rejected["start"] == 15.2
    assert "connecteur" in rejected["reason"]
    assert "final_score" not in rejected


def test_duration_out_of_bounds_after_the_cut_is_rejected_with_its_reason(tmp_path, video_dir, rubric_path):
    # phrases 3..6 : 15.25 -> 34.65 (19.4 s, dans 20-45 a 3 s pres) ; sans
    # "Alors du coup en fait" il reste 15.7 s < 17
    starts = set_sentence(video_dir, 3, "Alors du coup en fait voila.")
    assert 34.65 - starts[5] < 17

    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 34.65)]}])

    data = read_moments(video_dir)
    assert data["moments"] == []
    [rejected] = data["rejected"]
    assert "duree" in rejected["reason"] and "connecteur" in rejected["reason"]


def test_connector_in_the_middle_of_a_sentence_is_ignored(tmp_path, video_dir, rubric_path):
    set_sentence(video_dir, 3, "mot3_0 donc on part maintenant.")
    set_sentence(video_dir, 20, "Maison close et fermee depuis.")
    set_sentence(video_dir, 50, "Etienne, mais pourquoi donc ?")

    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65), moment(100.25, 134.65), moment(250.25, 284.65)]}])

    data = read_moments(video_dir)
    assert sorted(spans(data)) == [(15.25, 44.65), (100.25, 134.65), (250.25, 284.65)]
    assert sorted(m["hook_text"] for m in data["moments"]) == [
        "Etienne, mais pourquoi donc ?", "Maison close et fermee depuis.", "mot3_0 donc on part maintenant.",
    ]


@pytest.mark.parametrize(
    "text, first_kept",
    [
        ("DONC on part tout de suite.", 1),
        ("Mais, on part tout de suite.", 1),
        ("«Mais on part tout de suite.", 1),
        ("« Mais on part tout de suite.", 2),
        ("- Et on part tout de suite.", 2),
        ("…et on part tout de suite.", 1),
        ("EN FAIT, on part tout de suite.", 2),
        ("Parce que on part tout de suite.", 2),
        ("Sauf que on part tout de suite.", 2),
        ("Par contre, on part tout de suite.", 2),
        ("Alors, on part tout de suite !", 1),
    ],
)
def test_connectors_are_found_whatever_the_case_and_punctuation(tmp_path, video_dir, rubric_path, text, first_kept):
    starts = set_sentence(video_dir, 3, text)

    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65)]}])

    [m] = read_moments(video_dir)["moments"]
    assert m["start"] == round2(starts[first_kept])
    assert m["hook_text"] == " ".join(text.split()[first_kept:])


def test_multipart_first_part_starts_after_the_connector(tmp_path, video_dir, rubric_path):
    starts = set_sentence(video_dir, 50, "Du coup on decortique le trailer.")

    run(tmp_path, rubric_path, [{"moments": [moment(250.25, 389.65, fmt="multipart", breaks=[320.3])]}])

    [m] = read_moments(video_dir)["moments"]
    assert m["start"] == round2(starts[2])
    assert [(p["start"], p["end"]) for p in m["parts"]] == [(floor1(starts[2]), 319.7), (320.2, 389.7)]


def test_connector_list_is_a_setting(tmp_path, video_dir, rubric_path):
    from clipper.moments import CONFIG_DEFAULTS

    assert {"donc", "mais", "et", "alors", "du coup", "en fait", "parce que", "sauf que", "par contre"} <= set(
        CONFIG_DEFAULTS["leading_connectors"]
    )
    set_sentence(video_dir, 3, "Donc on part tout de suite.")
    starts = set_sentence(video_dir, 20, "Bref, on part tout de suite.")
    config = make_config(tmp_path, rubric_path, leading_connectors=["Bref"])

    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65), moment(100.25, 134.65)]}], config=config)

    # "donc" n'est plus dans la liste, "bref" y est
    assert sorted(spans(read_moments(video_dir))) == [(15.25, 44.65), (round2(starts[1]), 134.65)]


@pytest.mark.parametrize("value", ["donc", [""], ["donc", 3], None])
def test_invalid_connector_list_is_refused(tmp_path, video_dir, rubric_path, value):
    from clipper.moments import MomentsError

    with pytest.raises(MomentsError, match="leading_connectors"):
        run(
            tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65)]}],
            config=make_config(tmp_path, rubric_path, leading_connectors=value),
        )
    assert not (video_dir / "moments.json").exists()


def test_sentence_without_word_timings_opening_on_a_connector_is_rejected(tmp_path, video_dir, rubric_path):
    transcript = make_transcript()
    seg = transcript["segments"][3]
    seg["words"], seg["text"] = [], " Donc on part tout de suite."
    (video_dir / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")

    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65)]}])

    data = read_moments(video_dir)
    assert data["moments"] == []
    [rejected] = data["rejected"]
    assert "connecteur" in rejected["reason"] and "horodatage" in rejected["reason"]


def test_jury_judges_the_text_without_the_connector(tmp_path, video_dir, rubric_path):
    starts = set_sentence(video_dir, 3, "Donc mot3_0 mot3_2 mot3_3 mot3_4.")
    proposal = {"moments": [moment(15.25, 44.65)]}

    fake, _ = run(
        tmp_path, rubric_path, with_jury(proposal, jury_notes({3: GOOD})),
        config=auto_config(tmp_path, rubric_path),
    )

    jury_calls = [c for c in fake.calls if c.usage.startswith("jury_")]
    assert len(jury_calls) == 5
    assert all("Texte : « mot3_0 mot3_2 mot3_3 mot3_4. mot4_0 " in c.prompt for c in jury_calls)
    assert not any("Donc" in c.prompt for c in jury_calls)
    assert all(f"[{floor1(starts[1]):.1f}-44.7]" in c.prompt for c in jury_calls)
    [m] = read_moments(video_dir)["moments"]
    assert (m["start"], m["hook_text"]) == (round2(starts[1]), "mot3_0 mot3_2 mot3_3 mot3_4.")


def test_comparison_round_sees_the_text_without_the_connector(tmp_path, video_dir, rubric_path):
    set_sentence(video_dir, 90, "Mais mot90_1 mot90_2 mot90_3 mot90_4.")
    config = make_config(tmp_path, rubric_path, max_transcript_chars=2000, chunk_chars=2000, chunk_overlap_seconds=30)
    seen = []

    def llm_answer(request):
        if "## Candidats" in request.prompt:
            seen.append(request.prompt)
            return {"moments": [{"id": 0, "justification": "ok", "scores": GOOD}]}
        if "[450.2-454.7]" in request.prompt:
            return {"moments": [moment(450.25, 484.65)]}
        return {"moments": []}

    run(tmp_path, rubric_path, [llm_answer] * 10, config=config)

    [prompt] = seen
    assert "mot90_1 mot90_2" in prompt and "Mais" not in prompt.split("## Candidats")[1]


def test_rescore_keeps_a_moment_whose_connector_was_cut(tmp_path, video_dir, rubric_path):
    starts = set_sentence(video_dir, 3, "Mais qui est vraiment X ?")
    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65)]}])
    write_vision_after_moments(video_dir, [striking(20.0)])

    fake, _ = rescore(tmp_path, rubric_path)

    assert fake.calls == []
    [m] = read_moments(video_dir)["moments"]
    assert (m["start"], m["hook_text"], m["final_score"]) == (round2(starts[1]), "qui est vraiment X ?", 68.9)
    assert m["duration"] == round(44.65 - starts[1], 1)


# --------------------------------------------------------------------------
# TASK-3c0a : un connecteur retire colle au mot suivant (aucun silence entre
# les deux, comme "Donc" 2428.38-2428.54 puis "est" a 2428.54 sur sZi-qJ-5ptA)
# ne doit jamais laisser la fin du connecteur dans le clip publie : la borne
# de debut est au centieme (SPEC-1557 regle 5) et ne recule jamais dans le
# mot retire.
# --------------------------------------------------------------------------


def set_touching_words(video_dir, k, words):
    """Reecrit la phrase k de transcript.json avec des mots colles bout a
    bout (fin d'un mot = debut du suivant, comme un connecteur sans silence
    avant le mot qui le suit) : ``words`` est [(texte, debut, fin), ...]."""
    transcript = json.loads((video_dir / "transcript.json").read_text(encoding="utf-8"))
    seg = transcript["segments"][k]
    seg["words"] = [{"word": " " + w, "start": s, "end": e, "probability": 0.9} for w, s, e in words]
    seg["start"], seg["end"] = words[0][1], words[-1][2]
    seg["text"] = " " + " ".join(w for w, _, _ in words)
    (video_dir / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")


def test_connector_touching_the_next_word_never_recedes_into_it(tmp_path, video_dir, rubric_path):
    # "Donc" colle a "est" (aucun silence entre les deux, comme sur
    # sZi-qJ-5ptA) : un arrondi qui reculerait (dixieme, ou centieme
    # inferieur) publierait un debut a l'interieur du connecteur retire.
    set_touching_words(video_dir, 3, [
        ("Donc", 15.30, 15.46), ("est", 15.46, 15.62), ("ce", 15.62, 15.70),
        ("vraiment", 15.70, 16.00), ("normal?", 16.00, 16.40),
    ])

    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65)]}])

    [m] = read_moments(video_dir)["moments"]
    assert m["start"] == 15.46
    assert m["start"] >= 15.46, "ne doit jamais reculer dans la fin du connecteur retire (15.46)"
    assert m["hook_text"] == "est ce vraiment normal?"
    assert read_moments(video_dir)["rejected"] == []


def test_rescore_of_a_touching_connector_moment_raises_no_error(tmp_path, video_dir, rubric_path):
    # _restore doit retrouver au centieme, sans erreur, la borne publiee
    # d'un moment dont le connecteur colle au mot suivant : la re-notation
    # (bonus visuel, score) est attendue, mais jamais une MomentsError ni un
    # changement de bornes.
    set_touching_words(video_dir, 3, [
        ("Donc", 15.30, 15.46), ("est", 15.46, 15.62), ("ce", 15.62, 15.70),
        ("vraiment", 15.70, 16.00), ("normal?", 16.00, 16.40),
    ])
    run(tmp_path, rubric_path, [{"moments": [moment(15.25, 44.65)]}])
    before = read_moments(video_dir)
    write_vision_after_moments(video_dir, [striking(20.0)])

    fake, _ = rescore(tmp_path, rubric_path)

    assert fake.calls == []
    after = read_moments(video_dir)
    [m] = after["moments"]
    assert (m["start"], m["end"], m["hook_text"]) == (15.46, 44.65, "est ce vraiment normal?")
    [before_m] = before["moments"]
    assert (before_m["start"], before_m["end"]) == (m["start"], m["end"])
    assert m["final_score"] == 68.9  # bonus visuel applique par la re-notation


# --------------------------------------------------------------------------
# Integration optionnelle avec le vrai Claude (quota de l'utilisateur) :
# CLIPPER_CLAUDE_INTEGRATION=1 pytest tests/test_moments.py
# --------------------------------------------------------------------------

STORY = [
    "Attendez, attendez, je viens de recevoir un message de ma source chez Rockstar.",
    "Et franchement je ne sais pas si j'ai le droit de le dire en live.",
    "Bon, tant pis, je le dis.",
    "La date de sortie de GTA 6 aurait encore ete repoussee.",
    "Pas de quelques semaines, de six mois.",
    "Le chat est en train d'exploser, regardez-moi ca.",
    "Selon lui, c'est Vice City qui n'est pas pret, la carte est trop grande.",
    "Et Lucia aurait des missions entierement refaites.",
    "Alors vous allez me dire, encore une rumeur.",
    "Sauf que cette source m'avait donne le premier trailer trois jours avant tout le monde.",
    "Donc moi, j'y crois a quatre-vingts pour cent.",
    "Bref, on en reparle quand Rockstar confirme, mais preparez-vous.",
]


@pytest.mark.skipif(
    os.environ.get("CLIPPER_CLAUDE_INTEGRATION") != "1",
    reason="integration Claude : definir CLIPPER_CLAUDE_INTEGRATION=1 (consomme du quota)",
)
def test_real_claude_answers_the_schema_and_bounds_land_on_sentences(tmp_path, video_dir, rubric_path):
    from clipper.moments import run as run_moments

    segments = []
    for k, text in enumerate(STORY):
        words = text.split()
        step = 4.0 / len(words)
        ws = [
            {"word": " " + w, "start": 4.5 * k + i * step, "end": 4.5 * k + (i + 1) * step - 0.05, "probability": 0.9}
            for i, w in enumerate(words)
        ]
        segments.append({"id": k, "start": ws[0]["start"], "end": ws[-1]["end"], "text": text, "words": ws})
    (video_dir / "transcript.json").write_text(json.dumps({"segments": segments}), encoding="utf-8")
    (video_dir / "meta.json").write_text(json.dumps({**META, "chapters": [], "heatmap": [], "sponsorblock_segments": []}), encoding="utf-8")

    run_moments(VIDEO_ID, tmp_path / "workspace", config=make_config(tmp_path, rubric_path))

    data = read_moments(video_dir)
    starts = {math.floor(s["start"] * 10 + 1e-6) / 10 for s in segments}
    ends = {math.ceil(s["end"] * 10 - 1e-6) / 10 for s in segments}
    for m in data["moments"] + [r for r in data["rejected"] if "final_score" in r]:
        assert m["start"] in starts and m["end"] in ends


# --------------------------------------------------------------------------
# Clips de 60-120 s et longs passages en series (TASK-7758, SPEC-1557
# regles 3 et 4), sur la grille de la spec et une video de 2150 s (430
# phrases ; le sponsor exclu 200-240 est evite en partant de la phrase 60).
# --------------------------------------------------------------------------

SPEC_DURATIONS = """[durations]
single_min = 60
single_max = 120
part_min = 60
part_max = 120
min_parts = 2
max_parts = 12
tolerance = 3
"""
SPEC_RUBRIC = re.sub(r"\[durations\]\n(?:[a-z_]+ = \d+\n)+", SPEC_DURATIONS, TEST_RUBRIC)


@pytest.fixture
def spec_rubric(tmp_path):
    p = tmp_path / "spec_rubric.toml"
    p.write_text(SPEC_RUBRIC, encoding="utf-8")
    return p


@pytest.fixture
def long_video(video_dir):
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(430)), encoding="utf-8")
    return video_dir


def span_of(first, last, **kwargs):
    """Candidat des phrases first a last : 5 x (last - first) + 4.4 s."""
    return moment(sentence_start(first), sentence_end(last), **kwargs)


def test_spec_rubric_replaces_the_durations():
    assert "single_max = 120" in SPEC_RUBRIC and "single_max = 45" not in SPEC_RUBRIC


def test_prompt_describes_single_and_series_with_the_rubric_bounds(tmp_path, video_dir, spec_rubric):
    fake, _ = run(tmp_path, spec_rubric, [{"moments": []}])
    prompt = fake.calls[0].prompt
    assert "single" in prompt and "histoire complete de 60 a 120 s" in prompt
    assert "une affaire entiere, typiquement 5 a 15 min, au plus 12 x 120 = 1440 s" in prompt
    assert "serie de 2 a 12 parties de 60 a 120 s qui se suivent" in prompt
    assert "suspense" in prompt and "etape parts" in prompt


def test_prompt_bounds_are_read_from_the_rubric(tmp_path, video_dir, spec_rubric):
    spec_rubric.write_text(
        SPEC_RUBRIC.replace("single_min = 60", "single_min = 55").replace("max_parts = 12", "max_parts = 7")
        .replace("part_max = 120", "part_max = 110"),
        encoding="utf-8",
    )
    fake, _ = run(tmp_path, spec_rubric, [{"moments": []}])
    prompt = fake.calls[0].prompt
    assert "histoire complete de 55 a 120 s" in prompt
    assert "au plus 7 x 110 = 770 s" in prompt
    assert "serie de 2 a 7 parties de 60 a 110 s" in prompt


def test_single_of_50_s_is_rejected_and_of_90_s_kept(tmp_path, long_video, spec_rubric):
    run(tmp_path, spec_rubric, [{"moments": [span_of(60, 69), span_of(100, 117)]}])  # 49.4 s, 89.4 s

    data = read_moments(long_video)
    assert spans(data) == [(500.25, 589.65)]
    [r] = data["rejected"]
    assert r["start"] == 300.2 and "duree 49.4 s hors bornes single (60-120 s)" in r["reason"]


def test_multipart_of_10_min_is_kept(tmp_path, long_video, spec_rubric):
    run(tmp_path, spec_rubric, [{"moments": [span_of(60, 179, fmt="multipart")]}])  # 599.4 s

    [m] = read_moments(long_video)["moments"]
    assert (m["start"], m["end"], m["format"]) == (300.25, 899.65, "multipart")


@pytest.mark.parametrize(("last", "duration"), [(419, "1799.4"), (79, "99.4")])
def test_multipart_outside_min_parts_x_part_min_and_max_parts_x_part_max_is_rejected(
    tmp_path, long_video, spec_rubric, last, duration
):
    run(tmp_path, spec_rubric, [{"moments": [span_of(60, last, fmt="multipart")]}])  # 30 min, 100 s

    data = read_moments(long_video)
    assert data["moments"] == []
    [r] = data["rejected"]
    assert f"duree {duration} s hors bornes multipart (120-1440 s)" in r["reason"]


def test_single_inside_a_retained_passage_is_rejected_even_better_scored(tmp_path, long_video, spec_rubric):
    # passage 60 (BORDER, 60.0) et clip unique 100 (GOOD, 66.9) inclus dedans
    run(tmp_path, spec_rubric, [{"moments": [
        span_of(60, 179, fmt="multipart", scores=BORDER),
        span_of(100, 117, scores=GOOD),
    ]}])

    data = read_moments(long_video)
    assert [(m["start"], m["format"]) for m in data["moments"]] == [(300.25, "multipart")]
    [r] = data["rejected"]
    assert (r["start"], r["final_score"]) == (500.25, 66.9)
    assert "chevauche un passage en serie retenu [300.2-899.7]" in r["reason"]


def test_passage_under_min_score_leaves_the_single(tmp_path, long_video, spec_rubric):
    # LOW : 30.0 plus au plus 10 de bonus, sous min_score
    run(tmp_path, spec_rubric, [{"moments": [
        span_of(60, 179, fmt="multipart", scores=LOW),
        span_of(100, 117, scores=GOOD),
    ]}])

    data = read_moments(long_video)
    assert spans(data) == [(500.25, 589.65)]
    [r] = data["rejected"]
    assert r["start"] == 300.25 and "min_score" in r["reason"]


def test_two_overlapping_passages_keep_the_best_scored(tmp_path, long_video, spec_rubric):
    run(tmp_path, spec_rubric, [{"moments": [
        span_of(60, 179, fmt="multipart", scores=BORDER),
        span_of(150, 269, fmt="multipart", scores=GOOD),
    ]}])

    data = read_moments(long_video)
    assert spans(data) == [(750.25, 1349.65)]
    [r] = data["rejected"]
    assert "chevauche un moment mieux note [750.2-1349.7]" in r["reason"]


def test_exploration_never_brings_back_a_single_overlapping_a_retained_passage(tmp_path, long_video, spec_rubric):
    # le jury hesite fort sur le clip unique 100 (dispersion 70) : seul
    # candidat possible pour l'exploration, il chevauche le passage retenu.
    proposal = {"moments": [span_of(60, 179, fmt="multipart"), span_of(100, 117)]}
    run(
        tmp_path, spec_rubric, with_jury(proposal, jury_notes({60: GOOD, 100: split(TOP)})),
        config=auto_config(tmp_path, spec_rubric, exploration_share=1.0),
    )

    data = read_moments(long_video)
    assert [m["format"] for m in data["moments"]] == ["multipart"]
    assert explored(data) == []
    assert data["exploration"]["chosen"] == 0


# --------------------------------------------------------------------------
# TASK-7ae6 (SPEC-0eec regle 4, remplace SPEC-1557) : plafond souple du
# nombre de moments par heure de video source (duree lue dans meta.json),
# applique apres min_score et le non-chevauchement, par score decroissant ;
# un moment a always_keep_score ou plus est toujours retenu, meme au-dela du
# plafond, et compte dedans. Meme regle a la re-notation ; l'exploration peut
# piocher parmi les moments ecartes par le plafond.
# --------------------------------------------------------------------------

CAP_RUBRIC = re.sub(r"max_moments_per_hour = \d+", "max_moments_per_hour = 6", TEST_RUBRIC)
CAP_RUBRIC = re.sub(r"always_keep_score = \d+", "always_keep_score = 70", CAP_RUBRIC)


@pytest.fixture
def cap_rubric(tmp_path):
    p = tmp_path / "cap_rubric.toml"
    p.write_text(CAP_RUBRIC, encoding="utf-8")
    return p


def write_flat_signals(video_dir, duration):
    """meta.json et audio.json sans heatmap, SponsorBlock ni pic audio : le
    bonus reste toujours nul, pour un controle exact du score final dans les
    tests du plafond."""
    meta = {**META, "duration": duration, "heatmap": [], "sponsorblock_segments": [], "chapters": []}
    (video_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (video_dir / "audio.json").write_text(
        json.dumps({"window_seconds": 1.0, "energy_db": [], "peaks": []}), encoding="utf-8"
    )


def cap_score(value, trend, payoff=0, emotion=0):
    """Notes sur la grille de CAP_RUBRIC (poids hook 3, standalone 3, payoff
    2, emotion 2, value 2, trend 1) : hook et standalone fixes a 10, pour un
    score final (sans bonus, cf. ``write_flat_signals``) uniquement fonction
    de payoff/emotion/value/trend."""
    return {"hook": 10, "standalone": 10, "payoff": payoff, "emotion": emotion, "value": value, "trend": trend}


def spaced_candidates(n, scores):
    """n moments d'environ 34.4 s, espaces de 40 s (jamais chevauchants)."""
    return [moment(sentence_start(8 * i), sentence_end(8 * i + 6), scores=s) for i, s in zip(range(n), scores)]


def test_cap_keeps_the_best_scored_moments_and_rejects_the_rest_for_the_plafond(tmp_path, video_dir, cap_rubric):
    # video de 1 h, plafond = ceil(6 x 1) = 6 : 9 candidats non chevauchants,
    # 1 nettement au-dessus des autres et 8 entre 60.0 et 65.4 -> 6 retenus
    # (le plus haut compris), 3 rejetes pour le plafond.
    write_flat_signals(video_dir, 3600.0)
    others = [cap_score(9, t) for t in range(8)]
    top = cap_score(10, 10, payoff=5, emotion=5)
    run(tmp_path, cap_rubric, [{"moments": spaced_candidates(9, [top, *others])}])

    data = read_moments(video_dir)
    assert len(data["moments"]) == 6
    kept_scores = sorted((m["final_score"] for m in data["moments"]), reverse=True)
    assert kept_scores[0] == 84.6
    capped = [r for r in data["rejected"] if "plafond" in r["reason"]]
    assert len(capped) == 3
    assert all(r["final_score"] < kept_scores[-1] for r in capped)


def test_cap_still_keeps_every_moment_at_or_above_always_keep_score(tmp_path, video_dir, cap_rubric):
    # 8 candidats a 70 ou plus (always_keep_score) : tous retenus meme si le
    # plafond de la video (1 h -> 6) est depasse.
    write_flat_signals(video_dir, 3600.0)
    scores = [cap_score(v, 0, payoff=10, emotion=10) for v in range(8)]
    run(tmp_path, cap_rubric, [{"moments": spaced_candidates(8, scores)}])

    data = read_moments(video_dir)
    assert len(data["moments"]) == 8
    assert all(m["final_score"] >= 70 for m in data["moments"])
    assert not [r for r in data["rejected"] if "plafond" in r["reason"]]


def test_cap_is_the_hourly_rate_times_the_video_duration_in_hours(tmp_path, video_dir, cap_rubric):
    # video de 1 h 40 (6000 s) : plafond = ceil(6 x 100/60) = 10 -> sur 11
    # candidats sous always_keep_score, 10 retenus, 1 rejete pour le plafond.
    write_flat_signals(video_dir, 6000.0)
    scores = [cap_score(9, t) for t in range(10)] + [cap_score(9, 0)]
    run(tmp_path, cap_rubric, [{"moments": spaced_candidates(11, scores)}])

    data = read_moments(video_dir)
    assert len(data["moments"]) == 10
    assert len([r for r in data["rejected"] if "plafond" in r["reason"]]) == 1


def test_missing_video_duration_in_meta_is_an_error(tmp_path, video_dir, cap_rubric):
    from clipper.moments import MomentsError

    meta = {k: v for k, v in META.items() if k != "duration"}
    (video_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(MomentsError, match="duration"):
        run(tmp_path, cap_rubric, [{"moments": [moment(10.25, 44.65)]}])
    assert not (video_dir / "moments.json").exists()


def test_rescore_reapplies_the_plafond_after_a_score_change(tmp_path, video_dir, cap_rubric):
    write_flat_signals(video_dir, 3600.0)
    scores = [cap_score(9, t) for t in range(7)]
    run(tmp_path, cap_rubric, [{"moments": spaced_candidates(7, scores)}])
    data = read_moments(video_dir)
    assert len(data["moments"]) == 6
    lowest = next(r for r in data["rejected"] if "plafond" in r["reason"])
    assert lowest["final_score"] == 60.0
    write_vision_after_moments(video_dir, [striking((lowest["start"] + lowest["end"]) / 2)])

    fake, _ = rescore(tmp_path, cap_rubric)

    assert fake.calls == []
    after = read_moments(video_dir)
    assert len(after["moments"]) == 6
    assert lowest["start"] in [m["start"] for m in after["moments"]]
    newly_capped = [r for r in after["rejected"] if "plafond" in r["reason"]]
    assert len(newly_capped) == 1 and newly_capped[0]["start"] != lowest["start"]


FLOOR_RUBRIC = re.sub(r"min_moments_cap = \d+", "min_moments_cap = 3", CAP_RUBRIC)


@pytest.fixture
def floor_rubric(tmp_path):
    p = tmp_path / "floor_rubric.toml"
    p.write_text(FLOOR_RUBRIC, encoding="utf-8")
    return p


def test_short_video_keeps_at_least_min_moments_cap_moments(tmp_path, video_dir, floor_rubric):
    # video de 7,5 min (450 s) : ceil(6 x 0.125) = 1, mais min_moments_cap = 3
    # l'emporte ; 4 candidats au-dessus de min_score, non chevauchants -> les
    # 3 mieux notes retenus, le 4e (le moins bon) ecarte pour le plafond, qui
    # cite min_moments_cap dans sa raison.
    write_flat_signals(video_dir, 450.0)
    scores = [cap_score(9, t) for t in range(4)]
    run(tmp_path, floor_rubric, [{"moments": spaced_candidates(4, scores)}])

    data = read_moments(video_dir)
    assert len(data["moments"]) == 3
    kept_scores = sorted(m["final_score"] for m in data["moments"])
    [capped] = [r for r in data["rejected"] if "plafond" in r["reason"]]
    assert capped["final_score"] < kept_scores[0]
    assert "min_moments_cap" in capped["reason"]


def test_min_moments_cap_of_1_restores_the_former_behaviour(tmp_path, video_dir, cap_rubric):
    # meme scenario que ci-dessus, mais avec min_moments_cap = 1 (cap_rubric) :
    # le plafond redevient ceil(6 x 0.125) = 1, un seul moment retenu.
    write_flat_signals(video_dir, 450.0)
    scores = [cap_score(9, t) for t in range(4)]
    run(tmp_path, cap_rubric, [{"moments": spaced_candidates(4, scores)}])

    data = read_moments(video_dir)
    assert len(data["moments"]) == 1
    assert len([r for r in data["rejected"] if "plafond" in r["reason"]]) == 3


def test_floor_does_not_change_the_cap_for_a_long_video(tmp_path, video_dir, floor_rubric):
    # video de 2 h (7200 s) : ceil(6 x 2) = 12 > min_moments_cap (3), donc le
    # plafond reste 12, inchange par le plancher. 13 scores distincts, tous
    # sous always_keep_score (70), pour isoler l'effet du seul plafond ;
    # espaces de 30 s (au lieu de 40 s) pour tenir sur les 100 phrases de
    # video_dir malgre les 13 candidats.
    write_flat_signals(video_dir, 7200.0)
    scores = [cap_score(9, t) for t in range(11)] + [cap_score(9, 9, emotion=1), cap_score(9, 10, payoff=1)]
    candidates = [moment(sentence_start(6 * i), sentence_end(6 * i + 4), scores=s) for i, s in enumerate(scores)]
    run(tmp_path, floor_rubric, [{"moments": candidates}])

    data = read_moments(video_dir)
    assert len(data["moments"]) == 12
    assert len([r for r in data["rejected"] if "plafond" in r["reason"]]) == 1


def test_exploration_can_pick_a_moment_rejected_for_the_plafond(tmp_path, video_dir, cap_rubric):
    # video de 10 min : plafond = ceil(6 x 1/6) = 1, seul le meilleur des 2
    # candidats est retenu directement ; l'autre, ecarte par le plafond, est
    # repris par l'exploration (SPEC-0eec regle 4).
    (video_dir / "meta.json").write_text(json.dumps({**META, "duration": 600.0}), encoding="utf-8")
    proposal = {"moments": [clip(0), clip(10)]}
    run(
        tmp_path, cap_rubric, with_jury(proposal, jury_notes({0: TOP, 10: GOOD})),
        config=auto_config(tmp_path, cap_rubric, exploration_share=1.0),
    )

    data = read_moments(video_dir)
    by_start = sorted(data["moments"], key=lambda m: m["start"])
    assert [(m["start"], m.get("exploration")) for m in by_start] == [(0.25, None), (50.25, True)]
    assert not [r for r in data["rejected"] if r["start"] == 50.25]
    assert data["exploration"] == {"share": 1.0, "seed": 0, "target": 1, "chosen": 1}


# --------------------------------------------------------------------------
# Grilles embarquees : "builtin" et "builtin:gaming" (SPEC-9216 R1-R3)
# --------------------------------------------------------------------------


def test_resolve_rubric_path_builtin_is_the_standard_rubric_unchanged():
    from clipper import moments

    resolved = moments.resolve_rubric_path("builtin")

    assert resolved.read_text(encoding="utf-8") == (REPO / "clipper" / "assets" / "rubric.toml").read_text(encoding="utf-8")
    assert moments.load_rubric(resolved)["min_score"] == 60


def test_resolve_rubric_path_builtin_gaming_is_the_gaming_rubric():
    from clipper import moments

    resolved = moments.resolve_rubric_path("builtin:gaming")

    assert resolved.read_text(encoding="utf-8") == (REPO / "clipper" / "assets" / "rubric-gaming.toml").read_text(encoding="utf-8")


def test_resolve_rubric_path_unknown_builtin_lists_the_available_rubrics_without_fallback():
    from clipper import moments

    with pytest.raises(moments.MomentsError) as excinfo:
        moments.resolve_rubric_path("builtin:inconnue")

    message = str(excinfo.value)
    assert "builtin:inconnue" in message
    assert "builtin" in message and "builtin:gaming" in message  # grilles disponibles


def test_resolve_rubric_path_other_values_stay_file_paths():
    from clipper import moments

    assert moments.resolve_rubric_path("ma_grille.toml") == Path("ma_grille.toml")
    assert moments.resolve_rubric_path("builtin.toml") == Path("builtin.toml")


def test_gaming_rubric_has_exactly_the_r2_values():
    from clipper import moments

    standard = moments.load_rubric(moments.resolve_rubric_path("builtin"))
    gaming = moments.load_rubric(moments.resolve_rubric_path("builtin:gaming"))

    assert gaming["min_score"] == 45
    assert gaming["max_moments_per_hour"] == 6
    assert gaming["always_keep_score"] == 70
    assert {name: c["weight"] for name, c in gaming["criteria"].items()} == {
        "hook": 3, "standalone": 2, "payoff": 3, "emotion": 4, "value": 0, "trend": 0,
    }
    d = gaming["durations"]
    assert (d["single_min"], d["single_max"], d["part_min"], d["part_max"]) == (30, 90, 30, 90)
    assert gaming["trend_keywords"] == []
    # question du critere emotion propre au jeu
    question = gaming["criteria"]["emotion"]["question"]
    assert question != standard["criteria"]["emotion"]["question"]
    for word in ("rire", "cri", "sursaut", "peur", "rage", "victoire", "defaite", "retournement", "jeu", "chat"):
        assert word in question, word
    # tout le reste identique a la grille standard
    for table in ("bonus", "exclusions"):
        assert gaming[table] == standard[table], table
    for key in ("tolerance", "min_parts", "max_parts"):
        assert d[key] == standard["durations"][key], key
    for name in ("hook", "standalone", "payoff", "value", "trend"):
        assert gaming["criteria"][name]["question"] == standard["criteria"][name]["question"], name
    assert set(gaming["criteria"]) == set(standard["criteria"])


def test_gaming_rubric_is_commented_in_french_without_real_names():
    text = (REPO / "clipper" / "assets" / "rubric-gaming.toml").read_text(encoding="utf-8")

    assert text.lstrip().startswith("#")
    assert "grille" in text.lower()
    for name in ("GTA", "Rockstar", "Lucia", "Jason"):
        assert name not in text, name


def test_embedded_rubrics_are_in_the_installed_package_data():
    import importlib.resources

    assets = importlib.resources.files("clipper").joinpath("assets")
    for name in ("rubric.toml", "rubric-gaming.toml"):
        assert assets.joinpath(name).is_file(), name
    import tomllib

    package_data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    # le motif de pyproject.toml (glob recursif, fichiers directs compris) couvre la grille gaming
    package = REPO / "clipper"
    covered = {path for pattern in package_data["clipper"] for path in package.glob(pattern)}
    assert package / "assets" / "rubric-gaming.toml" in covered


def test_rubric_info_of_the_gaming_rubric_names_its_file_and_weights():
    from clipper import moments

    rubric_path = moments.resolve_rubric_path("builtin:gaming")
    info = moments._rubric_info(rubric_path, moments.load_rubric(rubric_path))

    assert info["path"].endswith("rubric-gaming.toml")
    assert info["min_score"] == 45
    assert info["weights"]["emotion"] == 4

# --------------------------------------------------------------------------
# Grille builtin:gaming-action et seuil eliminatoire [gate] (SPEC-b0f3 R1-R3)
# --------------------------------------------------------------------------

# Empreintes des grilles livrees avant la grille d'action : "builtin" et
# "builtin:gaming" ne changent pas d'un octet (SPEC-b0f3 R1).
_STANDARD_SHA256 = "355050bfd24ff4cf28876d3c410ab1653a1a102026ad6ba8712b86d21e93e0bf"
_GAMING_SHA256 = "682e451c2887ff9aa7afcf8dcb5b60c173f93ac6990939505709e3c9ad2c96a6"


def test_builtin_and_builtin_gaming_stay_identical_byte_for_byte():
    import hashlib

    from clipper import moments

    for value, digest in (("builtin", _STANDARD_SHA256), ("builtin:gaming", _GAMING_SHA256)):
        data = moments.resolve_rubric_path(value).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, value


def test_resolve_rubric_path_builtin_gaming_action_is_the_action_rubric():
    from clipper import moments

    resolved = moments.resolve_rubric_path("builtin:gaming-action")

    expected = REPO / "clipper" / "assets" / "rubric-gaming-action.toml"
    assert resolved.read_bytes() == expected.read_bytes()
    assert moments.load_rubric(resolved)["min_score"] == 50


def test_unknown_builtin_error_lists_the_three_rubrics():
    from clipper import moments

    with pytest.raises(moments.MomentsError) as excinfo:
        moments.resolve_rubric_path("builtin:inconnue")

    message = str(excinfo.value)
    for name in ("builtin", "builtin:gaming", "builtin:gaming-action"):
        assert f'"{name}"' in message, name


def test_gaming_action_rubric_is_in_the_installed_package_data():
    import importlib.resources
    import tomllib

    assert importlib.resources.files("clipper").joinpath("assets", "rubric-gaming-action.toml").is_file()
    package_data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    package = REPO / "clipper"
    covered = {path for pattern in package_data["clipper"] for path in package.glob(pattern)}
    assert package / "assets" / "rubric-gaming-action.toml" in covered


def test_gaming_action_rubric_has_exactly_the_r2_values():
    from clipper import moments

    gaming = moments.load_rubric(moments.resolve_rubric_path("builtin:gaming"))
    action = moments.load_rubric(moments.resolve_rubric_path("builtin:gaming-action"))

    assert action["min_score"] == 50
    assert action["max_moments_per_hour"] == 6
    assert action["always_keep_score"] == 70
    assert action["min_moments_cap"] == 3
    assert action["trend_keywords"] == []
    assert {name: c["weight"] for name, c in action["criteria"].items()} == {
        "action": 5, "emotion": 3, "hook": 2, "payoff": 2, "standalone": 1, "value": 0, "trend": 0,
    }
    assert action["criteria"]["action"]["question"] == (
        "Se passe-t-il quelque chose DANS LE JEU pendant le clip : combat, clutch, mort, victoire ou "
        "défaite, sursaut, retournement, moment de jeu spectaculaire ? 0-2 si rien ne se passe dans le "
        "jeu (menu, chargement, le streamer commente, donne son avis ou raconte sa vie)."
    )
    assert action["criteria"]["emotion"]["question"] == (
        "Réaction forte AU JEU : cri, rire, rage, peur, sursaut, soulagement, hurlement de victoire "
        "ou de défaite ?"
    )
    for name in ("hook", "payoff", "standalone", "value", "trend"):
        assert action["criteria"][name]["question"] == gaming["criteria"][name]["question"], name
    d = action["durations"]
    assert (d["single_min"], d["single_max"], d["part_min"], d["part_max"]) == (20, 90, 30, 90)
    assert (d["min_parts"], d["max_parts"], d["tolerance"]) == (2, 12, 3)
    for table in ("bonus", "exclusions"):
        assert action[table] == gaming[table], table
    assert action["gate"] == {"criterion": "action", "min": 4, "unless_criterion": "emotion", "unless_min": 8}


def test_gaming_action_rubric_is_commented_in_french_without_real_names():
    text = (REPO / "clipper" / "assets" / "rubric-gaming-action.toml").read_text(encoding="utf-8")

    assert text.lstrip().startswith("#")
    assert "grille" in text.lower()
    for name in ("GTA", "Rockstar", "Lucia", "Jason"):
        assert name not in text, name


def test_standard_and_gaming_rubrics_have_no_gate():
    from clipper import moments

    for value in ("builtin", "builtin:gaming"):
        assert "gate" not in moments.load_rubric(moments.resolve_rubric_path(value)), value


# --- load_rubric : validation de [gate] -----------------------------------


def _rubric_with_gate(tmp_path, gate_toml):
    p = tmp_path / "gated.toml"
    p.write_text(TEST_RUBRIC + "\n" + gate_toml, encoding="utf-8")
    return p


def test_load_rubric_without_gate_is_unchanged(rubric_path):
    from clipper.moments import load_rubric

    assert "gate" not in load_rubric(rubric_path)


def test_load_rubric_accepts_a_valid_gate(tmp_path):
    from clipper.moments import load_rubric

    p = _rubric_with_gate(tmp_path, '[gate]\ncriterion = "hook"\nmin = 4\nunless_criterion = "emotion"\nunless_min = 8\n')
    assert load_rubric(p)["gate"] == {"criterion": "hook", "min": 4, "unless_criterion": "emotion", "unless_min": 8}
    p = _rubric_with_gate(tmp_path, '[gate]\ncriterion = "hook"\nmin = 0\n')
    assert load_rubric(p)["gate"] == {"criterion": "hook", "min": 0}


@pytest.mark.parametrize(
    "gate_toml, key",
    [
        ('[gate]\nmin = 4\n', "criterion"),
        ('[gate]\ncriterion = "inconnu"\nmin = 4\n', "criterion"),
        ('[gate]\ncriterion = 3\nmin = 4\n', "criterion"),
        ('[gate]\ncriterion = "hook"\n', "min"),
        ('[gate]\ncriterion = "hook"\nmin = 11\n', "min"),
        ('[gate]\ncriterion = "hook"\nmin = -1\n', "min"),
        ('[gate]\ncriterion = "hook"\nmin = 4.5\n', "min"),
        ('[gate]\ncriterion = "hook"\nmin = true\n', "min"),
        ('[gate]\ncriterion = "hook"\nmin = "4"\n', "min"),
        ('[gate]\ncriterion = "hook"\nmin = 4\nunless_criterion = "emotion"\n', "unless_min"),
        ('[gate]\ncriterion = "hook"\nmin = 4\nunless_min = 8\n', "unless_criterion"),
        ('[gate]\ncriterion = "hook"\nmin = 4\nunless_criterion = "inconnu"\nunless_min = 8\n', "unless_criterion"),
        ('[gate]\ncriterion = "hook"\nmin = 4\nunless_criterion = "emotion"\nunless_min = 11\n', "unless_min"),
        ('[gate]\ncriterion = "hook"\nmin = 4\nunless_criterion = "emotion"\nunless_min = 7.5\n', "unless_min"),
    ],
)
def test_load_rubric_refuses_an_invalid_gate_naming_the_key(tmp_path, gate_toml, key):
    from clipper.moments import MomentsError, load_rubric

    with pytest.raises(MomentsError) as excinfo:
        load_rubric(_rubric_with_gate(tmp_path, gate_toml))
    assert f"[gate] {key}" in str(excinfo.value)


# --- etape moments : application de [gate] --------------------------------

GATE = '[gate]\ncriterion = "emotion"\nmin = 5\nunless_criterion = "value"\nunless_min = 8\n'
# Notes qui donnent un bon score final (> min_score) mais une emotion de 3.
LOW_EMOTION = {"hook": 9, "standalone": 9, "payoff": 9, "emotion": 3, "value": 5, "trend": 0}
LOW_EMOTION_SAVED = {**LOW_EMOTION, "value": 9}
GATE_REASON = "emotion 3 < seuil éliminatoire 5 (grille)"


def test_gate_rejects_a_candidate_below_min_before_min_score(tmp_path, video_dir):
    p = _rubric_with_gate(tmp_path, GATE)
    responses = [{"moments": [moment(0.25, 29.65, LOW_EMOTION, why="il parle"), moment(50.25, 79.65, GOOD)]}]

    run(tmp_path, p, responses)

    data = read_moments(video_dir)
    assert spans(data) == [(50.25, 79.65)]
    (rej,) = data["rejected"]
    assert (rej["start"], rej["end"]) == (0.25, 29.65)
    assert rej["reason"] == GATE_REASON
    assert rej["scores"] == LOW_EMOTION
    assert rej["justification"] == "il parle"
    assert rej["final_score"] >= 60  # aurait passe min_score : le seuil l'a rejete en premier


def test_gate_is_waived_by_unless_criterion(tmp_path, video_dir):
    p = _rubric_with_gate(tmp_path, GATE)

    run(tmp_path, p, [{"moments": [moment(0.25, 29.65, LOW_EMOTION_SAVED)]}])

    assert spans(read_moments(video_dir)) == [(0.25, 29.65)]


def test_gate_without_unless_has_no_exemption(tmp_path, video_dir):
    p = _rubric_with_gate(tmp_path, '[gate]\ncriterion = "emotion"\nmin = 5\n')

    run(tmp_path, p, [{"moments": [moment(0.25, 29.65, LOW_EMOTION_SAVED)]}])

    data = read_moments(video_dir)
    assert data["moments"] == []
    assert data["rejected"][0]["reason"] == GATE_REASON


def test_gate_applies_to_the_aggregated_jury_score(tmp_path, video_dir):
    p = _rubric_with_gate(tmp_path, GATE)
    # proposeur : GOOD (emotion 6, passe) ; jury : emotion 3 -> rejete.
    run(
        tmp_path, p, with_jury({"moments": [moment(10.25, 44.65, scores=GOOD)]}, jury_notes({2: LOW_EMOTION})),
        config=auto_config(tmp_path, p),
    )

    data = read_moments(video_dir)
    assert data["moments"] == []
    [r] = data["rejected"]
    assert r["reason"] == GATE_REASON
    assert r["scores"] == LOW_EMOTION
    assert r["jury"]["proposer"]["scores"] == GOOD


def test_rescore_reapplies_the_gate(tmp_path, video_dir):
    from clipper import moments as m

    p = _rubric_with_gate(tmp_path, GATE)
    run(tmp_path, p, [{"moments": [moment(0.25, 29.65, GOOD)]}])
    assert spans(read_moments(video_dir)) == [(0.25, 29.65)]
    # la grille se durcit : la re-notation apres vision doit rejeter le moment
    p.write_text(TEST_RUBRIC + '\n[gate]\ncriterion = "emotion"\nmin = 7\n', encoding="utf-8")
    (video_dir / "vision.json").write_text(json.dumps({"frames": []}), encoding="utf-8")

    m._rescore(video_dir, video_dir / "moments.json", {**m.CONFIG_DEFAULTS, "rubric_path": str(p)})

    data = read_moments(video_dir)
    assert data["moments"] == []
    assert data["rejected"][0]["reason"] == "emotion 6 < seuil éliminatoire 7 (grille)"


def test_rubric_without_gate_rejects_only_for_score(tmp_path, video_dir, rubric_path):
    responses = [{"moments": [moment(0.25, 29.65, GOOD), moment(50.25, 79.65, WEAK), moment(100.25, 129.65, LOW_EMOTION)]}]

    run(tmp_path, rubric_path, responses)

    data = read_moments(video_dir)
    assert sorted(spans(data)) == [(0.25, 29.65), (100.25, 129.65)]
    assert [r["reason"] for r in data["rejected"]] == ["score 59.2 < min_score 60"]


# --- TASK-fdb2 : grille enregistree a la re-notation, exploration et [gate] ---


def test_rescore_reads_the_rubric_recorded_in_moments_json_not_the_style_one(tmp_path, video_dir, rubric_path):
    from clipper import moments as m

    run(tmp_path, rubric_path, [{"moments": [moment(0.25, 29.65, GOOD)]}])
    (video_dir / "vision.json").write_text(json.dumps({"frames": []}), encoding="utf-8")
    # le style a change de grille depuis l'etape moments (criteres differents)
    settings = {**m.CONFIG_DEFAULTS, "rubric_path": "builtin:gaming-action"}

    m._rescore(video_dir, video_dir / "moments.json", settings)

    data = read_moments(video_dir)
    assert data["rubric"]["path"] == str(rubric_path)
    assert spans(data) == [(0.25, 29.65)]
    assert data["rescored"]["changed"] == []


def test_rescore_with_a_missing_recorded_rubric_raises_instead_of_using_the_style_one(tmp_path, video_dir):
    from clipper import moments as m

    p = tmp_path / "gone.toml"
    p.write_text(TEST_RUBRIC, encoding="utf-8")
    run(tmp_path, p, [{"moments": [moment(0.25, 29.65, GOOD)]}])
    (video_dir / "vision.json").write_text(json.dumps({"frames": []}), encoding="utf-8")
    p.unlink()
    other = tmp_path / "style.toml"
    other.write_text(TEST_RUBRIC, encoding="utf-8")

    with pytest.raises(m.MomentsError, match="gone.toml"):
        m._rescore(video_dir, video_dir / "moments.json", {**m.CONFIG_DEFAULTS, "rubric_path": str(other)})


def test_exploration_never_takes_a_candidate_eliminated_by_the_gate(tmp_path, video_dir):
    p = _rubric_with_gate(tmp_path, '[gate]\ncriterion = "emotion"\nmin = 5\n')
    # 60 : emotion mediane 3 < 5 (gate), forte dispersion ; 2 retenus -> 1 clip vise
    notes = {0: GOOD, 10: GOOD, 60: split(TOP)}
    run_jury(tmp_path, p, list(notes), notes, exploration_share=0.5)

    data = read_moments(video_dir)
    assert explored(data) == []
    assert data["exploration"]["target"] == 1 and data["exploration"]["chosen"] == 0
    [gated] = [r for r in data["rejected"] if r["start"] == 300.25]
    assert "seuil éliminatoire" in gated["reason"]


def test_exploration_still_takes_a_min_score_rejection_when_a_gate_exists(tmp_path, video_dir):
    p = _rubric_with_gate(tmp_path, '[gate]\ncriterion = "emotion"\nmin = 5\n')
    mid_split = {**dict.fromkeys(JUDGES, MID), "retention": TOP}  # emotion 5 : passe le gate, 50 < min_score
    notes = {0: GOOD, 10: GOOD, 50: mid_split, 60: split(TOP)}
    run_jury(tmp_path, p, list(notes), notes, exploration_share=0.5)

    [x] = explored(read_moments(video_dir))
    assert x["start"] == 250.25


# --------------------------------------------------------------------------
# Grille builtin:gaming-v2 et signal mesure speech_density (plan jury retention)
# --------------------------------------------------------------------------


def _words(first_at, n, per_second):
    """n mots horodates, le premier a ``first_at`` s, ``per_second`` mots par seconde."""
    return tuple((first_at + k / per_second, f" mot{k}") for k in range(n))


def _sents_for(words):
    from clipper import moments

    return [moments.Sentence(words[0][0], words[-1][0] + 0.2, "".join(w for _, w in words), words)]


def _density_bonus(rubric, start, end, words):
    from clipper import moments

    meta = {"duration": 500.0}
    return moments._bonus(start, end, meta, {"peaks": []}, None, rubric, sents=_sents_for(words))


def _v2_rubric():
    from clipper import moments

    return moments.load_rubric(moments.resolve_rubric_path("builtin:gaming-v2"))


def test_speech_density_gives_a_malus_for_a_sparse_candidate():
    # 0,5 mot/s sur 20 s : 10 mots, premier mot a 0 s
    bonus = _density_bonus(_v2_rubric(), 100.0, 120.0, _words(100.0, 10, 0.5))

    assert bonus["speech_density"] == -_v2_rubric()["bonus"]["speech_density_malus"]
    assert bonus["total"] == bonus["speech_density"]


def test_speech_density_gives_a_malus_when_the_first_word_comes_late():
    # dense (3 mots/s) mais premier mot a 3 s du debut
    bonus = _density_bonus(_v2_rubric(), 100.0, 120.0, _words(103.0, 51, 3.0))

    assert bonus["speech_density"] < 0


def test_speech_density_is_zero_for_a_dense_candidate_starting_on_a_word():
    # 3 mots/s, premier mot a 0 s
    bonus = _density_bonus(_v2_rubric(), 100.0, 120.0, _words(100.0, 60, 3.0))

    assert bonus["speech_density"] == 0
    assert bonus["total"] == 0


def test_speech_density_ignores_words_outside_the_candidate():
    # mots avant et apres : seuls ceux de [start, end[ comptent
    words = _words(90.0, 10, 1.0) + _words(100.0, 60, 3.0) + _words(125.0, 50, 5.0)
    bonus = _density_bonus(_v2_rubric(), 100.0, 120.0, words)

    assert bonus["speech_density"] == 0


def _untimed_sents(segments):
    """Phrases de segments sans words (transcript ancien ou mots retires) : split_sentences."""
    from clipper import moments

    return moments.split_sentences({"segments": segments})


def test_speech_density_without_timed_words_is_not_a_malus_but_a_note():
    # Moment dont les phrases ont du texte sans aucun mot horodate, dans un transcript qui a
    # ailleurs des mots : pas de -6 (0 mot/s fabrique), signal non applique, note lisible.
    from clipper import moments

    sents = _untimed_sents([
        {"start": 100.0, "end": 110.0, "text": " on joue une partie"},
        {"start": 110.0, "end": 120.0, "text": " la fin du moment"},
    ]) + _sents_for(_words(200.0, 60, 3.0))

    bonus = moments._bonus(100.0, 120.0, {"duration": 500.0}, {"peaks": []}, None, _v2_rubric(), sents=sents)

    assert "speech_density" not in bonus
    assert bonus["total"] == 0
    assert "speech_density_note" in bonus


def test_speech_density_raises_when_no_sentence_of_the_transcript_has_timed_words():
    # Transcript entier sans mots horodates : erreur explicite, jamais -6 sur chaque moment.
    from clipper import moments

    sents =_untimed_sents([{"start": 100.0, "end": 120.0, "text": " tout le texte sans mots"}])

    with pytest.raises(moments.MomentsError, match="speech_density"):
        moments._bonus(100.0, 120.0, {"duration": 500.0}, {"peaks": []}, None, _v2_rubric(), sents=sents)


def test_rubrics_without_speech_density_keys_do_not_change_their_bonus():
    from clipper import moments

    for name in ("builtin", "builtin:gaming", "builtin:gaming-action"):
        rubric = moments.load_rubric(moments.resolve_rubric_path(name))
        bonus = moments._bonus(
            100.0, 120.0, {"duration": 500.0}, {"peaks": []}, None, rubric, sents=_sents_for(_words(100.0, 10, 0.5)),
        )
        assert "speech_density" not in bonus, name
        assert bonus == {"replayed": 0.0, "audio_peaks": 0.0, "visual": 0.0, "total": 0.0}, name


def test_speech_density_keys_must_come_together_and_be_numbers(tmp_path):
    from clipper import moments

    text = (REPO / "clipper" / "assets" / "rubric-gaming.toml").read_text(encoding="utf-8")
    partial = tmp_path / "partial.toml"
    partial.write_text(text.replace("[bonus]\n", "[bonus]\nspeech_density_malus = 5\n"), encoding="utf-8")
    with pytest.raises(moments.MomentsError, match="speech_density"):
        moments.load_rubric(partial)


def test_builtin_gaming_v2_has_exactly_the_plan_values():
    from clipper import moments

    v1 = moments.load_rubric(moments.resolve_rubric_path("builtin:gaming"))
    v2 = _v2_rubric()

    assert {n: c["weight"] for n, c in v2["criteria"].items()} == {
        "hook": 3, "standalone": 2, "payoff": 3, "emotion": 2, "value": 2, "trend": 0,
    }
    assert v2["min_score"] == v1["min_score"] == 45
    assert (v2["durations"]["single_max"], v2["durations"]["part_max"]) == (60, 60)
    for key in ("single_min", "part_min", "min_parts", "max_parts", "tolerance"):
        assert v2["durations"][key] == v1["durations"][key], key
    for key in ("max_moments_per_hour", "always_keep_score", "min_moments_cap", "trend_keywords", "exclusions"):
        assert v2[key] == v1[key], key
    b = v2["bonus"]
    assert (b["speech_density_min_wps"], b["speech_density_max_first_word_s"]) == (1.5, 2)
    assert b["speech_density_malus"] > 0
    assert {k: b[k] for k in v1["bonus"]} == v1["bonus"]
    q = v2["criteria"]
    assert "ENTENDUE" in q["hook"]["question"] and "t = 0" in q["hook"]["question"]
    assert "DERNIÈRE phrase" in q["payoff"]["question"]
    assert "ne connaît ni le jeu ni le streamer" in q["standalone"]["question"]
    assert q["emotion"]["question"] == v1["criteria"]["emotion"]["question"]


def test_builtin_gaming_v2_is_in_the_unknown_builtin_listing_and_package_data():
    import importlib.resources

    from clipper import moments

    with pytest.raises(moments.MomentsError) as excinfo:
        moments.resolve_rubric_path("builtin:inconnue")
    assert '"builtin:gaming-v2"' in str(excinfo.value)
    assert importlib.resources.files("clipper").joinpath("assets", "rubric-gaming-v2.toml").is_file()


def test_the_existing_embedded_rubrics_stay_byte_identical_with_gaming_v2_added():
    import hashlib

    from clipper import moments

    for value, digest in (("builtin", _STANDARD_SHA256), ("builtin:gaming", _GAMING_SHA256)):
        assert hashlib.sha256(moments.resolve_rubric_path(value).read_bytes()).hexdigest() == digest, value


def test_rescore_keeps_the_speech_density_malus_in_the_total():
    from clipper import moments as m

    # Le total d'un bonus qui porte speech_density = replayed + audio + visual + speech_density (plafonne).
    old = {"replayed": 1.0, "audio_peaks": 2.0, "visual": 0.0, "speech_density": -6.0, "total": -3.0}
    assert m._rescored_bonus_total(_v2_rubric(), old, 2.0) == -1.0


def test_moments_json_judges_carry_the_sha_of_their_perspective(tmp_path, video_dir, rubric_path):
    # TASK-4e58554d15d3 : chaque juge de moments.json porte l'empreinte de sa perspective, jamais le texte
    proposal = {"moments": [moment(10.25, 44.65), moment(100.25, 134.65)]}
    notes = {2: dict.fromkeys(JUDGES, GOOD), 20: WEAK}
    run(tmp_path, rubric_path, with_jury(proposal, jury_notes(notes)), config=auto_config(tmp_path, rubric_path))

    judges = {j["name"]: j for j in read_moments(video_dir)["jury"]["judges"]}
    configured = jury.CONFIG_DEFAULTS["judges"]
    assert set(judges) == set(configured)
    for name, judge in judges.items():
        assert judge["perspective_sha"] == jury.perspective_sha(configured[name]["perspective"])
        assert "perspective" not in judge
    assert len({j["perspective_sha"] for j in judges.values()}) == len(judges)


def test_comparison_round_wrong_ids_are_sent_back_to_the_model_and_corrected(tmp_path, video_dir, rubric_path):
    # jury-I3 : ids manquants / en double = check= de llm.ask, donc reparable.
    from clipper.moments import run as run_moments

    config = make_config(tmp_path, rubric_path, max_transcript_chars=2000, chunk_chars=2000, chunk_overlap_seconds=30)
    compares = []

    def dispatch(request):
        if "Candidats" in request.prompt:
            compares.append(request.prompt)
            if len(compares) == 1:
                return {"moments": [
                    {"id": 0, "justification": "x", "scores": GOOD},
                    {"id": 0, "justification": "y", "scores": GOOD},
                ]}
            return {"moments": [
                {"id": 0, "justification": "x", "scores": GOOD},
                {"id": 1, "justification": "y", "scores": WEAK},
            ]}
        if "[10.2-14.7]" in request.prompt:
            return {"moments": [moment(10.25, 44.65)]}
        if "[450.2-454.7]" in request.prompt:
            return {"moments": [moment(450.25, 484.65)]}
        return {"moments": []}

    fake = FakeBackend([dispatch] * 30)
    with llm.use_backend(fake):
        run_moments(VIDEO_ID, tmp_path / "workspace", config=config)
    assert len(compares) == 2
    assert (video_dir / "moments.json").exists()


def test_same_passage_proposed_as_single_and_multipart_keeps_both_candidates(tmp_path, video_dir):
    # jury-M3 : la cle de dedoublonnage inclut le format ; le multipart
    # (prioritaire) ne disparait plus derriere un single moins bien note.
    rubric = tmp_path / "rubric140.toml"
    rubric.write_text(TEST_RUBRIC.replace("single_max = 45", "single_max = 140"), encoding="utf-8")
    proposal = {"moments": [
        moment(250.25, 389.65, scores=WEAK, fmt="single"),
        moment(250.25, 389.65, scores=GOOD, fmt="multipart", breaks=[319.65]),
    ]}
    run(tmp_path, rubric, [proposal])
    data = read_moments(video_dir)
    assert [(m["start"], m["end"], m["format"]) for m in data["moments"]] == [(250.25, 389.65, "multipart")]
    assert any(r["start"] == 250.25 and "serie" in r["reason"] for r in data["rejected"])
