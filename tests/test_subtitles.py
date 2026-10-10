from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from clipper import llm
from clipper.config import Config
from clipper.llm.fake import FakeBackend
from clipper.subtitles import CONFIG_DEFAULTS

VIDEO_ID = "abcdefghijk"
CLIP_ID = "03"


def _word(word, start, end):
    return {"word": word, "start": start, "end": end, "probability": 0.9}


def default_words():
    # 8 mots, 0.0 -> 4.6s ; couvre l'intervalle [1.0, 4.0]
    return [
        _word(" Salut", 0.0, 0.4),
        _word(" tout", 0.5, 0.8),
        _word(" le", 0.8, 1.0),
        _word(" monde", 1.1, 1.6),
        _word(" bienvenue", 1.7, 2.3),
        _word(" sur", 2.4, 2.6),
        _word(" GTA", 2.7, 3.2),
        _word(" six.", 3.3, 4.6),
    ]


def make_transcript(words=None):
    return {
        "video_id": VIDEO_ID,
        "language": "fr",
        "segments": [{"id": 1, "start": words[0]["start"] if words else 0.0,
                      "end": words[-1]["end"] if words else 0.0,
                      "text": "", "words": words if words is not None else default_words()}],
    }


def make_config(tmp_path, **subtitles):
    return Config(
        mode="review",
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "output",
        _sections={"subtitles": subtitles} if subtitles else {},
    )


@pytest.fixture
def video_dir(tmp_path):
    d = tmp_path / "workspace" / VIDEO_ID
    d.mkdir(parents=True)
    (d / "transcript.json").write_text(json.dumps(make_transcript()), encoding="utf-8")
    return d


def run(tmp_path, config=None, **kwargs):
    from clipper.subtitles import generate

    config = config or make_config(tmp_path)
    return generate(
        VIDEO_ID, CLIP_ID,
        kwargs.pop("start", 1.0), kwargs.pop("end", 4.0),
        tmp_path / "workspace",
        config=config,
        **kwargs,
    )


NO_EMPHASIS = {"indices": []}


def parse_ass(path: Path) -> dict:
    """Minimal .ass parser: script info, style fields, dialogue events."""
    text = path.read_text(encoding="utf-8")
    info = dict(re.findall(r"^(PlayResX|PlayResY):\s*(\d+)", text, re.MULTILINE))
    style_line = re.search(r"^Format:\s*(.+)\nStyle:\s*(.+)$", text, re.MULTILINE)
    style_fields = [f.strip() for f in style_line.group(1).split(",")]
    style_values = [v.strip() for v in style_line.group(2).split(",")]
    style = dict(zip(style_fields, style_values))
    events = []
    for m in re.finditer(r"^Dialogue:\s*(.+)$", text, re.MULTILINE):
        parts = m.group(1).split(",", 9)
        events.append({
            "layer": parts[0], "start": parts[1], "end": parts[2],
            "style": parts[3], "margin_v": parts[7], "text": parts[9],
        })
    return {"info": info, "style": style, "events": events, "raw": text}


def to_seconds(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


# --------------------------------------------------------------------------
# C1 : .ass 1080x1920 depuis un intervalle + transcription mot par mot
# --------------------------------------------------------------------------


def test_generates_ass_file_at_1080x1920(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path)
    assert Path(path).exists()
    assert Path(path).suffix == ".ass"
    doc = parse_ass(Path(path))
    assert doc["info"]["PlayResX"] == "1080"
    assert doc["info"]["PlayResY"] == "1920"


def test_returns_the_ass_path(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path)
    assert Path(path) == tmp_path / "workspace" / VIDEO_ID / "subtitles" / f"{CLIP_ID}.ass"


# --------------------------------------------------------------------------
# C2 : groupes de 2 a 4 mots (un evenement Dialogue par groupe)
# --------------------------------------------------------------------------


def test_words_are_grouped_by_2_to_4_words_per_event(tmp_path, video_dir):
    # mots chevauchant [1.0, 4.0] (par > start, fin < end exclus aux bornes) :
    # "monde", "bienvenue", "sur", "GTA", "six." = 5 mots
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path)
    doc = parse_ass(Path(path))
    assert len(doc["events"]) >= 1
    total_words = 0
    for ev in doc["events"]:
        tokens = re.findall(r"\\k\d+(?:\\c[^}]*)?\}([^{]*)", ev["text"])
        n = len([t for t in tokens if t.strip()])
        assert 2 <= n <= 4
        total_words += n
    assert total_words == 5


# --------------------------------------------------------------------------
# TASK-9ee7 : rien a l'ecran pendant les silences. Constat reel sur
# v2887271276/00.ass : un groupe de 2 mots ("L'ÉCHAPPE. AH,") couvrait un
# Dialogue de 7.88 a 25.22s alors que le mot suivant ne commencait qu'a
# 25.04s (17.34s affiches pour 16.76s de vrai silence) -- _group_words()
# regroupait par nombre de mots sans jamais tenir compte de l'ecart
# temporel entre eux.
# --------------------------------------------------------------------------


def test_a_long_gap_produces_no_event_covering_the_silence(tmp_path, video_dir):
    # "Ah" a 1.0-1.3s puis "bon" seulement a 11.0s : sans le decoupage par
    # ecart, min_words_per_group=2 les aurait mis dans le meme groupe et le
    # Dialogue de base aurait couvert les 9.7s de silence entre les deux.
    words = [_word(" Ah", 1.0, 1.3), _word(" bon", 11.0, 11.3)]
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(words)), encoding="utf-8")
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=1.0, end=12.0)
    doc = parse_ass(Path(path))
    assert len(doc["events"]) == 2
    mid_silence = 5.0 - 1.0  # relatif au debut du clip, bien dans le silence
    for ev in doc["events"]:
        s, e = to_seconds(ev["start"]), to_seconds(ev["end"])
        assert not (s <= mid_silence <= e), (s, e)
    # "Ah" disparait vite apres sa fin (hold_s), pas jusqu'a "bon"
    assert to_seconds(doc["events"][0]["end"]) < (11.0 - 1.0)


def test_close_groups_touch_without_a_blank_gap(tmp_path, video_dir):
    # mots de default_words() groupes en [monde bienvenue sur] (fin 2.6) et
    # [GTA six.] (debut 2.7) : ecart naturel de 0.1s < hold_s (0.3s par
    # defaut) -- le premier groupe doit disparaitre exactement quand le
    # second apparait, jamais avant (pas de clignotement).
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=1.0, end=4.0)
    doc = parse_ass(Path(path))
    assert len(doc["events"]) == 2
    first, second = doc["events"]
    assert to_seconds(first["end"]) == pytest.approx(to_seconds(second["start"]), abs=0.011)


def test_abnormally_long_word_end_is_bounded_by_max_word_s(tmp_path, video_dir):
    # "fois" dure 13.8s dans la transcription (faster-whisper etire sa fin
    # sur le silence qui suit, mesure jusqu'a plus de 10s sur v2887271276) :
    # bornee a max_word_s, pas affichee/surlignee jusqu'a sa fin annoncee.
    words = [_word(" Wow", 1.0, 1.2), _word(" fois", 1.2, 15.0)]
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(words)), encoding="utf-8")
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=1.0, end=16.0)
    doc = parse_ass(Path(path))
    max_word_s = CONFIG_DEFAULTS["max_word_s"]
    hold_s = CONFIG_DEFAULTS["hold_s"]
    ks = [int(k) for k in re.findall(r"\\k(\d+)", doc["events"][-1]["text"])]
    assert ks[-1] == round(max_word_s * 100)
    assert to_seconds(doc["events"][-1]["end"]) == pytest.approx(
        (1.2 + max_word_s + hold_s) - 1.0, abs=0.011
    )


# --------------------------------------------------------------------------
# C6 : timecodes relatifs au debut du clip (start -> 0)
# --------------------------------------------------------------------------


def test_timecodes_are_relative_to_clip_start(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=1.0, end=4.0)
    doc = parse_ass(Path(path))
    first = doc["events"][0]
    # premier mot dans l'intervalle : "monde" a 1.1-1.6
    assert to_seconds(first["start"]) == pytest.approx(1.1 - 1.0, abs=0.011)
    last = doc["events"][-1]
    # dernier mot : "six." a 3.3-4.6, plus hold_s (TASK-9ee7) : rien apres
    # pour le capper, dernier groupe du clip
    hold_s = CONFIG_DEFAULTS["hold_s"]
    assert to_seconds(last["end"]) == pytest.approx(4.6 + hold_s - 1.0, abs=0.011)


# --------------------------------------------------------------------------
# TASK-3c0a (SPEC-1557 regle 5) : un mot dont le debut precede le debut du
# clip de plus de 0.05 s n'est jamais sous-titre (ex. reel : le connecteur
# "Donc" colle au mot suivant "est", clip 03/05 de sZi-qJ-5ptA affichait
# "DONC EST-CE NORMAL").
# --------------------------------------------------------------------------


def straddling_words(gap):
    """"Donc" chevauche la borne de debut du clip (commence ``gap`` s avant
    elle, finit apres) comme le ferait un connecteur mal arrondi (borne
    reculee dans le mot retire) ; "est" suit, entierement dans le clip."""
    return [
        _word(" Donc", 15.46 - gap, 15.62),
        _word(" est", 15.62, 15.78),
        _word(" normal?", 15.78, 16.10),
    ]


def test_word_straddling_the_clip_start_by_more_than_the_tolerance_is_not_subtitled(tmp_path, video_dir):
    # "Donc" commence 0.15 s avant le debut du clip (> 0.05 s, SPEC-1557
    # regle 5) : meme s'il deborde dedans (finit a 15.62 > 15.46), il ne
    # doit jamais s'afficher, comme sur le clip 03/05 de sZi-qJ-5ptA ou une
    # borne mal arrondie laissait entendre/afficher la fin de "Donc".
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(straddling_words(0.15))), encoding="utf-8")
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=15.46, end=16.10)
    doc = parse_ass(Path(path))
    text = "".join(ev["text"] for ev in doc["events"])
    assert "Donc" not in text
    first_word = re.search(r"\\k\d+(?:\\c[^}]*)?\}([^{]*)", doc["events"][0]["text"]).group(1).strip()
    assert first_word == "est"


def test_word_straddling_the_clip_start_within_the_tolerance_is_still_subtitled(tmp_path, video_dir):
    # "Donc" ne commence que 0.04 s avant le debut du clip (< 0.05 s) : la
    # tolerance couvre l'arrondi au centieme des bornes publiees (SPEC-1557
    # regle 5), il reste sous-titre.
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(straddling_words(0.04))), encoding="utf-8")
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=15.46, end=16.10)
    doc = parse_ass(Path(path))
    text = "".join(ev["text"] for ev in doc["events"])
    assert "Donc" in text


# --------------------------------------------------------------------------
# C3 : karaoke, mot courant surligne (tags \k par mot, duree = fin-debut)
# --------------------------------------------------------------------------


def test_each_word_has_a_karaoke_tag_sized_to_its_duration(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path)
    doc = parse_ass(Path(path))
    first_event = doc["events"][0]
    # "monde" 1.1-1.6 (0.5s = 50cs), "bienvenue" 1.7-2.3 (0.6s = 60cs)
    assert "\\k50}" in first_event["text"] and " monde" in first_event["text"]
    assert "\\k60}" in first_event["text"] and " bienvenue" in first_event["text"]


def test_karaoke_style_uses_distinct_primary_and_secondary_colours(tmp_path, video_dir):
    """SecondaryColour (mots pas encore prononces) != PrimaryColour (mot en
    cours / deja prononce) : c'est ce qui fait le surlignage karaoke."""
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path)
    doc = parse_ass(Path(path))
    assert doc["style"]["PrimaryColour"] != doc["style"]["SecondaryColour"]


# --------------------------------------------------------------------------
# C4 : gros texte avec contour (style : Fontsize eleve, Outline > 0)
# --------------------------------------------------------------------------


def test_style_has_large_font_and_outline(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path)
    doc = parse_ass(Path(path))
    assert int(doc["style"]["Fontsize"]) >= 60
    assert float(doc["style"]["Outline"]) > 0


def test_font_size_and_outline_come_from_config(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, config=make_config(tmp_path, font_size=120, outline=8))
    doc = parse_ass(Path(path))
    assert doc["style"]["Fontsize"] == "120"
    assert doc["style"]["Outline"] == "8"


# --------------------------------------------------------------------------
# C5 : mots d'emphase choisis par clipper.llm (usage emphasis), couleur
# distincte de la couleur normale
# --------------------------------------------------------------------------


def test_emphasis_words_asked_to_llm_and_coloured_distinctly(tmp_path, video_dir):
    # mots : 0 monde, 1 bienvenue, 2 sur, 3 GTA, 4 six.
    fake = FakeBackend([{"indices": [3]}])  # "GTA"
    with llm.use_backend(fake):
        path = run(tmp_path)

    call = fake.calls[0]
    assert call.usage == "emphasis"
    for w in ("monde", "bienvenue", "sur", "GTA", "six."):
        assert w in call.prompt

    doc = parse_ass(Path(path))
    emphasis_color = CONFIG_DEFAULTS["emphasis_color"]
    assert any(emphasis_color in ev["text"] and "GTA" in ev["text"] for ev in doc["events"])
    # les autres mots ne portent pas la couleur d'emphase
    assert not any(emphasis_color in ev["text"] and "monde" in ev["text"] for ev in doc["events"])


def test_emphasis_disabled_in_config_skips_the_llm_call(tmp_path, video_dir):
    fake = FakeBackend([])
    with llm.use_backend(fake):
        run(tmp_path, config=make_config(tmp_path, emphasis=False))
    assert fake.calls == []


def test_invalid_emphasis_answer_is_a_failure_and_writes_nothing(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([{"mots": "pas le bon schema"}])), pytest.raises(llm.SchemaError):
        run(tmp_path)
    assert not (tmp_path / "workspace" / VIDEO_ID / "subtitles" / f"{CLIP_ID}.ass").exists()


# --------------------------------------------------------------------------
# C7 (TASK-29cf) : position par evenement dans une zone sure TikTok, hors
# visages du plan ou le sous-titre s'affiche, jamais sur l'accroche.
# --------------------------------------------------------------------------

H = 1920
SAFE_TOP, SAFE_BOTTOM = 0.20 * H, 0.78 * H  # zone sure par defaut (px)


def event_bands(doc: dict) -> list[tuple[float, float]]:
    """Bande verticale [haut, bas] (px) occupee par chaque evenement : style
    aligne en bas (Alignment 2), le bas du texte est a PlayResY - MarginV de
    l'evenement, sur une hauteur text_band_height."""
    assert doc["style"]["Alignment"] == "2"
    band = int(CONFIG_DEFAULTS["text_band_height"])
    bands = []
    for ev in doc["events"]:
        margin_v = int(ev["margin_v"]) or int(doc["style"]["MarginV"])
        bottom = H - margin_v
        bands.append((bottom - band, bottom))
    return bands


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def px(zone: tuple[float, float]) -> tuple[float, float]:
    return (zone[0] * H, zone[1] * H)


def zones(*entries):
    """[(start, end, [(haut, bas), ...]), ...] -> format avoid_zones."""
    return [{"start": s, "end": e, "bands": [list(b) for b in bands]} for s, e, bands in entries]


def in_lower_third_of_safe_zone(band: tuple[float, float]) -> bool:
    centre = (band[0] + band[1]) / 2
    return centre >= SAFE_TOP + 2 * (SAFE_BOTTOM - SAFE_TOP) / 3 and band[1] <= SAFE_BOTTOM


def test_without_faces_every_event_sits_in_the_lower_third_of_the_safe_zone(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path)
    bands = event_bands(parse_ass(Path(path)))
    assert len(bands) == 2
    for b in bands:
        assert SAFE_TOP <= b[0] and b[1] <= SAFE_BOTTOM
        assert in_lower_third_of_safe_zone(b)


def test_style_keeps_the_right_margin_clear_for_tiktok_icons(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path)
    # colonne d'icones TikTok a droite : au moins ~11 % de la largeur (120 px)
    assert int(parse_ass(Path(path))["style"]["MarginR"]) >= 120

    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, config=make_config(tmp_path, margin_right=210), force=True)
    assert parse_ass(Path(path))["style"]["MarginR"] == "210"


def test_safe_zone_comes_from_config(tmp_path, video_dir):
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, config=make_config(tmp_path, safe_zone=[0.30, 0.60]))
    for b in event_bands(parse_ass(Path(path))):
        assert 0.30 * H <= b[0] and b[1] <= 0.60 * H


def test_face_in_the_lower_part_moves_subtitles_just_above_it_not_to_the_top(tmp_path, video_dir):
    # constat du clip 00 : visage qui touche le bas -> sous-titres tout en haut,
    # sous l'interface TikTok. Ils restent dans la zone sure, au-dessus du visage.
    face = (0.55, 0.80)
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, avoid_zones=zones((0.0, 10.0, [face])))
    for b in event_bands(parse_ass(Path(path))):
        assert SAFE_TOP <= b[0] and b[1] <= SAFE_BOTTOM
        assert overlap(b, px(face)) == 0
        # le plus bas possible au-dessus du visage : a moins d'un pas de lui
        assert b[1] >= px(face)[0] - 200


def test_each_event_takes_the_position_of_the_plan_it_is_shown_in(tmp_path, video_dir):
    # events : [monde bienvenue sur] 1.1-2.6, [GTA six.] 2.7-4.6
    face = (0.55, 0.80)
    avoid = zones((0.0, 2.65, [face]), (2.65, 10.0, []))
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, avoid_zones=avoid)
    first, second = event_bands(parse_ass(Path(path)))
    assert overlap(first, px(face)) == 0
    assert in_lower_third_of_safe_zone(second)
    assert first != second


def test_event_spanning_two_plans_avoids_the_faces_of_both(tmp_path, video_dir):
    upper, lower = (0.20, 0.45), (0.60, 0.80)
    # [monde bienvenue sur] 1.1-2.6 est a cheval sur les deux plans
    avoid = zones((0.0, 2.0, [upper]), (2.0, 10.0, [lower]))
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, avoid_zones=avoid)
    first = event_bands(parse_ass(Path(path)))[0]
    assert overlap(first, px(upper)) == 0 and overlap(first, px(lower)) == 0


def test_without_free_position_the_least_covering_one_is_taken_and_logged(tmp_path, video_dir, caplog):
    # visages de 20 % a 70 % : seule la bande 70-78 % (154 px) est libre, trop
    # petite pour le texte ; la position la plus basse est la moins recouvrante.
    face = (0.20, 0.70)
    with caplog.at_level("WARNING", logger="clipper.subtitles"), \
            llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, avoid_zones=zones((0.0, 10.0, [face])))
    for b in event_bands(parse_ass(Path(path))):
        assert b[1] == pytest.approx(SAFE_BOTTOM, abs=1)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings and CLIP_ID in warnings[0].getMessage()


def test_free_position_logs_nothing(tmp_path, video_dir, caplog):
    with caplog.at_level("WARNING", logger="clipper.subtitles"), \
            llm.use_backend(FakeBackend([NO_EMPHASIS])):
        run(tmp_path, avoid_zones=zones((0.0, 10.0, [(0.55, 0.80)])))
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_hook_band_is_never_covered_even_to_avoid_a_face(tmp_path, video_dir):
    # zone sure elargie jusqu'en haut par la config : l'accroche (0-12 %, les
    # 2 premieres secondes du clip) reste interdite, meme si le visage pousse
    # les sous-titres vers le haut.
    hook = (0.0, 0.12)
    face = (0.30, 0.78)
    config = make_config(tmp_path, safe_zone=[0.0, 0.78])
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, config=config, avoid_zones=zones((0.0, 10.0, [face])),
                   reserved_zones=zones((1.0, 3.0, [hook])))
    bands = event_bands(parse_ass(Path(path)))
    for b in bands:
        assert overlap(b, px(hook)) == 0
        assert overlap(b, px(face)) == 0


def test_hook_band_leaves_no_candidate_is_an_error(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    config = make_config(tmp_path, safe_zone=[0.0, 0.3])
    with llm.use_backend(FakeBackend([NO_EMPHASIS])), pytest.raises(SubtitlesError):
        run(tmp_path, config=config, reserved_zones=zones((1.0, 3.0, [(0.0, 0.3)])))


# Cas reels du recadrage (reframe/<clip_id>.json), passes par pipeline.avoid_zones.


def reframe_plan(layout, faces, panels, start=0.0, end=10.0):
    return {"output": {"width": 1080, "height": 1920},
            "plans": [{"index": 0, "start": start, "end": end, "layout": layout,
                       "faces": faces, "panels": panels}]}


def rect(x, y, w, h, start=0.0, end=10.0):
    return {"start": start, "end": end, "x": x, "y": y, "w": w, "h": h}


def run_with_plan(tmp_path, plan):
    from clipper.pipeline import avoid_zones

    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, avoid_zones=avoid_zones(plan))
    return event_bands(parse_ass(Path(path)))


def test_split_layout_face_in_camera_panel(tmp_path, video_dir):
    # webcam en haut (0-768 px), visage y 100..300 sur 400 -> 192..576 px
    face = {"id": 0, "first": 0.0, "last": 10.0, "box": [800, 100, 1000, 300], "retained": True}
    panels = [
        {"name": "camera", "dest": {"x": 0, "y": 0, "w": 1080, "h": 768}, "rects": [rect(700, 0, 400, 400)]},
        {"name": "gameplay", "dest": {"x": 0, "y": 768, "w": 1080, "h": 1152}, "rects": [rect(0, 0, 640, 1080)]},
    ]
    for b in run_with_plan(tmp_path, reframe_plan("split", [face], panels)):
        assert overlap(b, (192, 576)) == 0
        assert in_lower_third_of_safe_zone(b)


def test_blur_layout_face_in_main_band(tmp_path, video_dir):
    # fond flou + image entiere au centre (656..1264 px, echelle 608/1080) ;
    # visage y 200..700 source -> 768..1051 px.
    face = {"id": 0, "first": 0.0, "last": 10.0, "box": [700, 200, 1100, 700], "retained": True}
    panels = [
        {"name": "background", "effect": "blur", "dest": {"x": 0, "y": 0, "w": 1080, "h": 1920},
         "rects": [rect(0, 0, 1920, 1080)]},
        {"name": "main", "dest": {"x": 0, "y": 656, "w": 1080, "h": 608}, "rects": [rect(0, 0, 1920, 1080)]},
    ]
    for b in run_with_plan(tmp_path, reframe_plan("fallback_blur", [face], panels)):
        assert overlap(b, (768, 1051)) == 0
        assert in_lower_third_of_safe_zone(b)


def test_single_layout_face_low_in_frame(tmp_path, video_dir):
    # cadre 608x1080 plein ecran (echelle 1920/1080) ; visage y 700..1000
    # source -> 1244..1778 px : couvre le tiers inferieur de la zone sure.
    face = {"id": 0, "first": 0.0, "last": 10.0, "box": [700, 700, 900, 1000], "retained": True}
    panels = [{"name": "main", "dest": {"x": 0, "y": 0, "w": 1080, "h": 1920}, "rects": [rect(600, 0, 608, 1080)]}]
    for b in run_with_plan(tmp_path, reframe_plan("single", [face], panels)):
        assert overlap(b, (1244.4, 1777.8)) == 0
        assert SAFE_TOP <= b[0] and b[1] <= SAFE_BOTTOM


# --------------------------------------------------------------------------
# C8 (TASK-29cf) : apostrophe ou trait d'union colle rattache au mot precedent
# --------------------------------------------------------------------------


def event_tokens(ev: dict) -> list[str]:
    return [t for t in re.findall(r"\\k\d+(?:\\c[^}]*)?\}([^{]*)", ev["text"]) if t.strip()]


def test_glued_apostrophe_and_hyphen_tokens_stay_with_the_previous_word(tmp_path, video_dir):
    words = [
        # decoupe par jetons (4 par groupe) du clip 00 : "Donc deja il m" / "'a ..."
        _word(" Donc", 1.0, 1.1), _word(" déjà", 1.1, 1.2), _word(" il", 1.2, 1.3), _word(" m", 1.3, 1.4),
        _word("'a", 1.4, 1.5), _word(" menti,", 1.5, 1.8), _word(" il", 1.8, 1.9),
        _word(" n", 1.9, 2.0), _word("\u2019a", 2.0, 2.1), _word(" pas", 2.1, 2.2),
        _word(" dit", 2.2, 2.4), _word(" d", 2.4, 2.5), _word("'autrui", 2.5, 2.8),
        _word(" viens", 2.8, 3.0), _word("-tu", 3.0, 3.2), _word(" vraiment", 3.2, 3.6),
    ]
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(words)), encoding="utf-8")
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=1.0, end=4.0)
    events = parse_ass(Path(path))["events"]
    tokens = [event_tokens(ev) for ev in events]
    assert [t for ev in tokens for t in ev] == [w["word"] for w in words]
    for ev in tokens:
        assert not ev[0].startswith(("'", "\u2019", "-"))
    # un mot avec son elision compte pour un : 4 mots au plus par groupe
    for ev in tokens:
        assert len([t for t in ev if not t.startswith(("'", "\u2019", "-"))]) <= 4


# --------------------------------------------------------------------------
# TASK-c492 : un jeton colle (apostrophe/trait d'union) ou une ponctuation
# isolee separe du mot precedent par un vrai silence (ecart > gap_s) ne doit
# plus le rejoindre inconditionnellement -- _units ignorait gap_s, residu de
# TASK-9ee7 mesure sur v2887271276 clip 05 : " d" (80.80-81.74) et "'accord"
# (84.52-85.04) restaient dans la meme unite malgre 2.78s de vrai silence
# entre les deux (faster-whisper avait coupe l'elision en deux jetons avec
# une hesitation au milieu).
# --------------------------------------------------------------------------


def test_glued_token_separated_by_a_real_silence_stays_in_its_own_unit(tmp_path, video_dir):
    words = [
        _word(" d", 1.0, 1.2),
        _word("'accord", 2.0, 2.3),
        _word(" mais", 2.4, 2.6),
    ]
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(words)), encoding="utf-8")
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=1.0, end=3.0)
    events = parse_ass(Path(path))["events"]
    tokens = [event_tokens(ev) for ev in events]
    # " d" reste seul dans son evenement : rien n'est affiche pendant le
    # silence de 0.8s qui le separe de "'accord" (sinon l'evenement
    # s'etendrait sur tout l'ecart, comme observe sur le cas reel).
    assert tokens[0] == [" d"]
    assert any(ev == ["'accord", " mais"] for ev in tokens[1:])


def test_isolated_punctuation_separated_by_a_real_silence_is_omitted(tmp_path, video_dir):
    words = [
        _word(" mot1", 1.0, 1.2),
        _word(" ?", 2.0, 2.1),
        _word(" mot2", 2.2, 2.4),
    ]
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(words)), encoding="utf-8")
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=1.0, end=3.0)
    events = parse_ass(Path(path))["events"]
    all_tokens = [t for ev in events for t in event_tokens(ev)]
    assert "?" not in " ".join(all_tokens)
    assert all_tokens == [" mot1", " mot2"]


# --------------------------------------------------------------------------
# TASK-746b (banc docs/bench-whisper-vitesse.md) : le batching whisper rend
# une ponctuation et des majuscules internes moins riches (segments sans
# virgule ni majuscule de phrase) ; le format letterbox (SPEC-6127, format
# par defaut) ne doit pas en dependre pour decouper et afficher le texte.
# --------------------------------------------------------------------------


def low_punctuation_words():
    """Comme le rendrait BatchedInferencePipeline : aucune ponctuation ni
    majuscule interne, un seul point final absent (segment coupe en plein
    milieu d'une phrase, cas reel observe dans le banc)."""
    text = "salut tout le monde aujourd hui on va parler du prochain gta qui arrive bientot pour tous"
    raw = text.split(" ")
    words = []
    t = 0.0
    for i, w in enumerate(raw):
        prefix = "" if i == 0 else " "
        words.append(_word(prefix + w, t, t + 0.3))
        t += 0.35
    return words


def test_letterbox_low_punctuation_batched_text_is_grouped_and_uppercased(tmp_path, video_dir):
    words = low_punctuation_words()
    path = run_letterbox(tmp_path, video_dir, words=words)
    events = lb_events(path)
    assert events
    full_text = " ".join(line_text(ev).strip() for ev in events)
    assert full_text == " ".join(w["word"].strip() for w in words).upper()
    for ev in events:
        assert_ink_in_zone(ev, 120, ZONE)


# --------------------------------------------------------------------------
# C9 : rendu ffmpeg d'echantillon, test optionnel (saute par defaut)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("CLIPPER_SUBTITLES_INTEGRATION") != "1",
    reason="integration ffmpeg : definir CLIPPER_SUBTITLES_INTEGRATION=1",
)
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent du PATH")
def test_integration_ffmpeg_burns_the_generated_ass_into_a_sample_clip(tmp_path, video_dir):
    """Le .ass produit est un filtre ``ass=`` ffmpeg valide : brule-le sur un
    court echantillon genere par lavfi et verifie que la sortie existe."""
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        ass_path = run(tmp_path)

    sample = tmp_path / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=1",
         str(sample)],
        check=True,
    )

    out = tmp_path / "sample_subtitled.mp4"
    # cwd = dossier du .ass : evite les soucis d'echappement du ':' d'une
    # lettre de lecteur Windows dans le filtre ffmpeg.
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(sample.resolve()),
         "-vf", f"ass={Path(ass_path).name}", str(out.resolve())],
        cwd=Path(ass_path).parent, check=True,
    )

    assert out.exists() and out.stat().st_size > 0


# --------------------------------------------------------------------------
# Contraintes du depot : cache par resultat existant (ADR-b16b), erreur si
# transcript.json absent
# --------------------------------------------------------------------------


def test_existing_ass_is_not_regenerated_unless_forced(tmp_path, video_dir):
    fake = FakeBackend([NO_EMPHASIS])
    with llm.use_backend(fake):
        path = run(tmp_path)
    original = Path(path).read_text(encoding="utf-8")

    with llm.use_backend(FakeBackend([])):  # aucune reponse scriptee : ne doit pas etre appele
        path2 = run(tmp_path)
    assert Path(path2) == Path(path)
    assert Path(path2).read_text(encoding="utf-8") == original

    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        run(tmp_path, force=True)


def test_missing_transcript_is_an_error(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    (video_dir / "transcript.json").unlink()
    with llm.use_backend(FakeBackend([NO_EMPHASIS])), pytest.raises(SubtitlesError):
        run(tmp_path)


def test_config_section_is_accepted_by_clipper_config(tmp_path):
    from clipper.config import load_config

    (tmp_path / "config.toml").write_text(
        '[subtitles]\nfont_size = 110\nmax_words_per_group = 3\n', encoding="utf-8"
    )
    section = load_config(tmp_path / "config.toml").section("subtitles")
    assert section["font_size"] == 110
    assert section["max_words_per_group"] == 3
    assert section["min_words_per_group"] == 2


# --------------------------------------------------------------------------
# TASK-a62e (SPEC-6127) : format letterbox, sous-titres dans la zone
# text_zones.subtitles du plan de recadrage (bande floue sous l'image).
# --------------------------------------------------------------------------

FONT_FILE = Path(__file__).resolve().parent.parent / "clipper" / "assets" / "fonts" / "Poppins-ExtraBold.ttf"
ZONE = {"x0": 150, "y0": 1246, "x1": 930, "y1": 1448}  # zone subtitles par defaut (contrat commun)
# Poppins ExtraBold : usWinAscent 1135, usWinDescent 627, unitsPerEm 1000.
STEP_68 = 78        # round(1.15 * 68)
OUTLINE = 7         # letterbox_outline (maquette)


def mock_words():
    # « il n'a pas fait de garde à vue » : 8 mots, un seul groupe (maquette)
    return [
        _word(" il", 0.0, 0.2), _word(" n", 0.2, 0.3), _word("'a", 0.3, 0.4), _word(" pas", 0.4, 0.7),
        _word(" fait", 0.7, 1.0), _word(" de", 1.0, 1.2), _word(" garde", 1.2, 1.6),
        _word(" à", 1.6, 1.7), _word(" vue", 1.7, 2.1),
    ]


def write_words(video_dir, words):
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(words)), encoding="utf-8")


def run_letterbox(tmp_path, video_dir, words=None, zone=None, config=None, **kwargs):
    write_words(video_dir, words or mock_words())
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        return Path(run(tmp_path, config=config, start=0.0, end=10.0,
                        text_zone=dict(zone or ZONE), **kwargs))


def lb_events(path: Path) -> list[dict]:
    events = []
    for m in re.finditer(r"^Dialogue:\s*(.+)$", path.read_text(encoding="utf-8"), re.MULTILINE):
        p = m.group(1).split(",", 9)
        events.append({"layer": p[0], "start": p[1], "end": p[2], "margin_l": int(p[5]),
                       "margin_r": int(p[6]), "margin_v": int(p[7]), "text": p[9]})
    return events


def line_text(ev: dict) -> str:
    return re.sub(r"\{[^}]*\}", "", ev["text"])


def font_size_em(ev: dict, style_size: int) -> float:
    """Taille em d'une ligne : \\fs de l'evenement (unites libass) ou celle du
    style, ramenee en em (win ascent + descent = 1,762 em)."""
    m = re.search(r"\\fs(\d+)", ev["text"])
    return (int(m.group(1)) if m else style_size) / 1.762


def assert_ink_in_zone(ev: dict, style_size: int, zone: dict) -> None:
    """Encre de la ligne (Pillow, contour compris) placee comme libass :
    centree entre MarginL et 1080 - MarginR, ligne de base a MarginV +
    ascent Windows ; elle tient dans la zone."""
    from PIL import ImageFont

    em = font_size_em(ev, style_size)
    font = ImageFont.truetype(str(FONT_FILE), round(em))
    text = line_text(ev)
    left, top, right, bottom = font.getbbox(text, anchor="ls")
    width = zone["x1"] - zone["x0"]
    x = zone["x0"] + (width - font.getlength(text)) / 2
    baseline = ev["margin_v"] + round(em * 1135 / 1000)
    assert x + left - OUTLINE >= zone["x0"] and x + right + OUTLINE <= zone["x1"], text
    assert baseline + top - OUTLINE >= zone["y0"] and baseline + bottom + OUTLINE <= zone["y1"], text


def test_letterbox_ass_starts_with_a_format_comment_and_uses_libass_font_size(tmp_path, video_dir):
    path = run_letterbox(tmp_path, video_dir)
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "; format: letterbox"
    doc = parse_ass(path)
    # 68 px d'em -> round(68 * 1762 / 1000) = 120 en unites libass
    assert doc["style"]["Fontsize"] == "120"
    assert doc["style"]["Alignment"] == "8"
    assert doc["info"] == {"PlayResX": "1080", "PlayResY": "1920"}


def test_letterbox_two_lines_at_default_size_are_two_dialogues_in_the_default_zone(tmp_path, video_dir):
    path = run_letterbox(tmp_path, video_dir)
    raw = path.read_text(encoding="utf-8")
    assert "\\N" not in raw
    events = lb_events(path)
    assert [line_text(ev).strip() for ev in events] == ["IL N'A PAS FAIT", "DE GARDE À VUE"]
    first, second = events
    assert (first["start"], first["end"]) == (second["start"], second["end"])
    for ev in events:
        assert ev["text"].startswith("{\\q2\\an8}")
        assert (ev["margin_l"], ev["margin_r"]) == (150, 1080 - 930)
        assert "\\fs" not in ev["text"]
        assert_ink_in_zone(ev, 120, ZONE)
    offset = CONFIG_DEFAULTS["letterbox_offset_y"]
    assert [ev["margin_v"] for ev in events] == [1246 + offset, 1246 + offset + STEP_68]


def test_letterbox_subtitles_follow_the_band_chosen_by_the_style(tmp_path, video_dir):
    """Éditeur d'agencement letterbox (TASK-3be3) : [reframe]
    letterbox_subtitle_dest {x 180, y 1260, w 720, h 120} devient
    text_zones.subtitles, et le texte y est posé (marges, hauteur)."""
    zone = {"x0": 180, "y0": 1260, "x1": 900, "y1": 1380}
    path = run_letterbox(tmp_path, video_dir, zone=zone)

    events = lb_events(path)
    assert events
    offset = CONFIG_DEFAULTS["letterbox_offset_y"]
    for ev in events:
        assert (ev["margin_l"], ev["margin_r"]) == (180, 1080 - 900)
        assert_ink_in_zone(ev, 120, zone)
    assert events[0]["margin_v"] == 1260 + offset


def test_letterbox_second_line_karaoke_counts_from_the_group_start(tmp_path, video_dir):
    path = run_letterbox(tmp_path, video_dir)
    first, second = lb_events(path)
    ks = [int(k) for k in re.findall(r"\\k(\d+)", second["text"])]
    # « de » commence a 1.0 s, le groupe a 0.0 s : 100 cs d'attente d'abord
    assert ks[0] == 100
    assert sum(ks) == 210  # fin de « vue » a 2.1 s
    assert sum(int(k) for k in re.findall(r"\\k(\d+)", first["text"])) == 100


def test_letterbox_uppercase_comes_from_config(tmp_path, video_dir):
    path = run_letterbox(tmp_path, video_dir, config=make_config(tmp_path, letterbox_uppercase=False))
    assert [line_text(ev).strip() for ev in lb_events(path)] == ["il n'a pas fait", "de garde à vue"]


def test_letterbox_very_long_word_is_cut_or_reduced_and_stays_in_the_zone(tmp_path, video_dir):
    words = [_word(" c", 0.0, 0.1), _word("'est", 0.1, 0.3),
             _word(" anticonstitutionnellement", 0.3, 1.5), _word(" vrai", 1.5, 1.9)]
    path = run_letterbox(tmp_path, video_dir, words=words)
    events = lb_events(path)
    texts = [line_text(ev).strip() for ev in events]
    assert "ANTICONSTITUTIONNELLEMENT" in texts
    assert " ".join(texts) == "C'EST ANTICONSTITUTIONNELLEMENT VRAI"
    for ev in events:
        assert_ink_in_zone(ev, 120, ZONE)
    long_line = events[texts.index("ANTICONSTITUTIONNELLEMENT")]
    fs = re.search(r"\\fs(\d+)", long_line["text"])
    assert fs and int(fs.group(1)) < 120
    # les mots courts gardent la taille par defaut
    assert all("\\fs" not in ev["text"] for ev in events if ev is not long_line)


def test_letterbox_reduced_size_survives_an_emphasis_reset(tmp_path, video_dir):
    words = [_word(" anticonstitutionnellement", 0.0, 1.0), _word(" vrai", 1.0, 1.4)]
    write_words(video_dir, words)
    with llm.use_backend(FakeBackend([{"indices": [0]}])):
        path = Path(run(tmp_path, start=0.0, end=10.0, text_zone=dict(ZONE)))
    long_line = next(ev for ev in lb_events(path) if "ANTI" in ev["text"])
    size = re.search(r"\\fs(\d+)", long_line["text"]).group(1)
    # apres le mot d'emphase, \r remet le style : la taille reduite est redonnee
    assert f"{{\\r\\fs{size}}}" in long_line["text"]


def test_letterbox_repeated_letters_run_is_shortened_to_three_and_logged(tmp_path, video_dir, caplog):
    words = [_word(" G" + "R" * 100, 0.0, 1.0), _word(" ok", 1.0, 1.2)]
    with caplog.at_level("INFO", logger="clipper.subtitles"):
        path = run_letterbox(tmp_path, video_dir, words=words)
    texts = [line_text(ev).strip() for ev in lb_events(path)]
    assert texts == ["GRRR OK"]
    assert "R" * 4 not in "".join(texts)
    assert any("1 mot" in r.getMessage() for r in caplog.records)
    # le transcript d'origine n'est pas modifie
    saved = json.loads((video_dir / "transcript.json").read_text(encoding="utf-8"))
    assert saved["segments"][0]["words"][0]["word"] == " G" + "R" * 100


def test_letterbox_word_of_three_repeated_letters_is_unchanged(tmp_path, video_dir):
    words = [_word(" NOOO", 0.0, 0.5), _word(" ok", 0.5, 0.8)]
    path = run_letterbox(tmp_path, video_dir, words=words)
    assert " ".join(line_text(ev).strip() for ev in lb_events(path)) == "NOOO OK"


def test_letterbox_word_of_100_r_alone_fits_after_shortening(tmp_path, video_dir):
    words = [_word(" " + "R" * 100, 0.0, 1.0)]
    path = run_letterbox(tmp_path, video_dir, words=words)
    assert [line_text(ev).strip() for ev in lb_events(path)] == ["RRR"]


def test_letterbox_normal_word_is_unchanged(tmp_path, video_dir):
    path = run_letterbox(tmp_path, video_dir)
    assert [line_text(ev).strip() for ev in lb_events(path)] == ["IL N'A PAS FAIT", "DE GARDE À VUE"]


def test_letterbox_long_word_without_repetition_is_hard_cut_with_hyphens(tmp_path, video_dir, caplog):
    word = "abcdefghij" * 6  # 60 lettres, sans repetition
    words = [_word(" " + word, 0.0, 3.0), _word(" ok", 3.0, 3.2)]
    with caplog.at_level("INFO", logger="clipper.subtitles"):
        path = run_letterbox(tmp_path, video_dir, words=words)
    events = lb_events(path)
    texts = [line_text(ev).strip() for ev in events]
    pieces = texts[:-1]
    assert len(pieces) > 1 and texts[-1] == "OK"
    assert all(p.endswith("-") for p in pieces[:-1]) and not pieces[-1].endswith("-")
    assert "".join(p.rstrip("-") for p in pieces) == word.upper()
    for ev in events:
        assert_ink_in_zone(ev, 120, ZONE)
    # meme minutage : de 0 a 3 s, dans l'ordre
    starts = [to_seconds(ev["start"]) for ev in events[:-1]]
    assert starts == sorted(starts) and starts[0] == 0.0
    assert to_seconds(events[-1]["start"]) == pytest.approx(3.0, abs=0.02)
    assert any("coupe" in r.getMessage() for r in caplog.records)


def test_letterbox_zone_narrower_than_one_character_is_an_error(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    words = [_word(" " + "abcdefghij" * 6, 0.0, 1.0)]
    zone = {"x0": 500, "y0": 1246, "x1": 502, "y1": 1448}
    with pytest.raises(SubtitlesError):
        run_letterbox(tmp_path, video_dir, words=words, zone=zone)
    assert not (tmp_path / "workspace" / VIDEO_ID / "subtitles" / f"{CLIP_ID}.ass").exists()


@pytest.mark.parametrize("zone", [
    {"x0": 150, "y0": 1246, "x1": 930},                      # cle absente
    {"x0": 930, "y0": 1246, "x1": 150, "y1": 1448},          # x0 >= x1
    {"x0": 150, "y0": 1448, "x1": 930, "y1": 1246},          # y0 >= y1
    {"x0": 150, "y0": 1246, "x1": 1200, "y1": 1448},         # hors de 1080x1920
    {"x0": 150.5, "y0": 1246, "x1": 930, "y1": 1448},        # pas un entier
    {"x0": 150, "y0": 1246, "x1": 930, "y1": 1296},          # pas la place d'une ligne
])
def test_letterbox_absent_or_incoherent_zone_is_an_error(tmp_path, video_dir, zone):
    from clipper.subtitles import SubtitlesError

    with pytest.raises(SubtitlesError):
        run_letterbox(tmp_path, video_dir, zone=zone)
    assert not (tmp_path / "workspace" / VIDEO_ID / "subtitles" / f"{CLIP_ID}.ass").exists()


def test_existing_ass_of_another_format_is_not_reused_silently(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        run(tmp_path)  # format recadre
    with pytest.raises(SubtitlesError, match="--force"):
        run_letterbox(tmp_path, video_dir)
    path = run_letterbox(tmp_path, video_dir, force=True)
    assert path.read_text(encoding="utf-8").startswith("; format: letterbox")
    # le .ass letterbox est reutilise tel quel en letterbox...
    with llm.use_backend(FakeBackend([])):
        assert Path(run(tmp_path, start=0.0, end=10.0, text_zone=dict(ZONE))) == path
    # ...mais pas hors letterbox
    with llm.use_backend(FakeBackend([])), pytest.raises(SubtitlesError, match="--force"):
        run(tmp_path)


def test_letterbox_offset_y_default_is_28():
    assert CONFIG_DEFAULTS["letterbox_offset_y"] == 28


def test_letterbox_default_offset_keeps_two_lines_with_commas_and_descenders_at_default_size(tmp_path, video_dir):
    # virgules (va, quoi, oui) et jambages (garde, quoi -> q, pense -> p) :
    # le cas le plus expose a un debordement bas cause par le decalage.
    words = [_word(" ça", 0.0, 0.3), _word(" va,", 0.3, 0.6), _word(" quoi,", 0.6, 0.9),
             _word(" je", 0.9, 1.0), _word(" pense", 1.0, 1.3), _word(" que", 1.3, 1.4),
             _word(" oui,", 1.4, 1.8)]
    path = run_letterbox(tmp_path, video_dir, words=words)
    events = lb_events(path)
    assert len(events) == 2
    assert all("\\fs" not in ev["text"] for ev in events)
    for ev in events:
        assert_ink_in_zone(ev, 120, ZONE)


def test_letterbox_negative_offset_is_an_explicit_error(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    with pytest.raises(SubtitlesError):
        run_letterbox(tmp_path, video_dir, config=make_config(tmp_path, letterbox_offset_y=-1))
    assert not (tmp_path / "workspace" / VIDEO_ID / "subtitles" / f"{CLIP_ID}.ass").exists()


def test_letterbox_offset_at_or_beyond_the_zone_height_is_an_explicit_error(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    zone_h = ZONE["y1"] - ZONE["y0"]
    with pytest.raises(SubtitlesError):
        run_letterbox(tmp_path, video_dir, config=make_config(tmp_path, letterbox_offset_y=zone_h))
    assert not (tmp_path / "workspace" / VIDEO_ID / "subtitles" / f"{CLIP_ID}.ass").exists()


def test_letterbox_config_section_is_accepted_by_clipper_config(tmp_path):
    from clipper.config import load_config

    (tmp_path / "config.toml").write_text(
        "[subtitles]\nletterbox_font_size = 60\nletterbox_uppercase = false\n", encoding="utf-8"
    )
    section = load_config(tmp_path / "config.toml").section("subtitles")
    assert section["letterbox_font_size"] == 60
    assert section["letterbox_uppercase"] is False
    assert section["letterbox_line_height"] == 1.15
    assert "letterbox_min_font_size" in section


# --------------------------------------------------------------------------
# TASK-4826 (SPEC-6127) : une ponctuation isolee (" ?", " !", " :", " ;",
# " »", sans lettre ni chiffre) reste collee au mot d'avant, jamais en debut
# de ligne ni de Dialogue ; omise si elle est le tout premier mot du clip.
# Defaut constate sur sZi-qJ-5ptA : "? JE L'AI PRIS ET" / "? TU M'AS ACCUSE".
# --------------------------------------------------------------------------


def is_isolated_punct(token: str) -> bool:
    stripped = token.strip()
    return bool(stripped) and not any(c.isalnum() for c in stripped)


def punct_words(mark):
    """4 mots reels, la ponctuation isolee collee au 4e, puis 3 mots reels :
    8 unites au total. Avec l'ancien decoupage (la ponctuation compte pour
    une unite a part entiere), cela tombe pile sur une frontiere de groupe
    de 4 et la ponctuation devient le premier mot du 2e groupe -- bug reel."""
    words = [_word(f" mot{i}", i * 0.5, i * 0.5 + 0.3) for i in range(1, 5)]
    words.append(_word(f" {mark}", words[-1]["end"] + 0.05, words[-1]["end"] + 0.15))
    start = words[-1]["end"]
    words += [_word(f" suite{i}", start + i * 0.5, start + i * 0.5 + 0.3) for i in range(1, 4)]
    return words


@pytest.mark.parametrize("mark", ["?", "!", ":", ";", "»"])
def test_isolated_punctuation_never_starts_a_group_and_stays_glued_to_the_previous_word(
    tmp_path, video_dir, mark
):
    words = punct_words(mark)
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(words)), encoding="utf-8")
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=words[0]["start"], end=words[-1]["end"] + 1.0)
    doc = parse_ass(Path(path))
    found = False
    for ev in doc["events"]:
        tokens = event_tokens(ev)
        assert not is_isolated_punct(tokens[0])
        for i, t in enumerate(tokens):
            if is_isolated_punct(t):
                found = True
                assert i > 0
                assert tokens[i - 1] == " mot4"
    assert found


@pytest.mark.parametrize("mark", ["?", "!", ":"])
def test_isolated_punctuation_as_the_very_first_word_of_the_clip_is_omitted(tmp_path, video_dir, mark):
    words = [_word(f" {mark}", 1.0, 1.1)] + [
        _word(f" mot{i}", 1.1 + i * 0.3, 1.1 + i * 0.3 + 0.2) for i in range(1, 4)
    ]
    (video_dir / "transcript.json").write_text(json.dumps(make_transcript(words)), encoding="utf-8")
    with llm.use_backend(FakeBackend([NO_EMPHASIS])):
        path = run(tmp_path, start=1.0, end=words[-1]["end"] + 0.5)
    doc = parse_ass(Path(path))
    all_tokens = [t for ev in doc["events"] for t in event_tokens(ev)]
    assert not any(mark in t for t in all_tokens)
    assert all_tokens[0] == " mot1"


def test_word_and_isolated_question_mark_form_a_single_unit_with_a_space(tmp_path, video_dir):
    words = [_word(" accusé", 0.0, 0.4), _word(" ?", 0.4, 0.5)]
    path = run_letterbox(tmp_path, video_dir, words=words)
    events = lb_events(path)
    assert len(events) == 1
    assert line_text(events[0]).strip() == "ACCUSÉ ?"


def punct_words_letterbox(mark):
    """8 mots reels, la ponctuation isolee collee au 8e, puis 7 mots reels :
    sous l'ancien decoupage, la frontiere de groupe (letterbox_max_words_
    per_group = 8) tombe pile sur la ponctuation, qui ouvre le 2e groupe
    (donc sa propre ligne/Dialogue) -- bug reel (sZi-qJ-5ptA)."""
    words = [_word(f" mot{i}", i * 0.3, i * 0.3 + 0.2) for i in range(1, 9)]
    words.append(_word(f" {mark}", words[-1]["end"] + 0.05, words[-1]["end"] + 0.15))
    start = words[-1]["end"]
    words += [_word(f" suite{i}", start + i * 0.3, start + i * 0.3 + 0.2) for i in range(1, 8)]
    return words


@pytest.mark.parametrize("mark", ["?", "!", ":"])
def test_letterbox_isolated_punctuation_never_starts_a_line_and_stays_with_the_previous_word(
    tmp_path, video_dir, mark
):
    words = punct_words_letterbox(mark)
    path = run_letterbox(tmp_path, video_dir, words=words)
    events = lb_events(path)
    full_text = " ".join(line_text(ev).strip() for ev in events)
    for ev in events:
        text = line_text(ev).strip()
        assert text and not text.startswith((mark,))
    assert f"MOT8 {mark}" in full_text


# Preuve par rendu reel : ffmpeg + libass incrustent le .ass letterbox.


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent du PATH")
def test_real_libass_render_keeps_all_ink_in_the_zone_with_the_line_step(tmp_path, video_dir):
    from PIL import Image

    # jambages et virgules (Ç, virgules, Q) ; aucun accent au-dessus des
    # capitales, pour que le haut d'encre de chaque ligne soit celui des capitales
    words = [_word(" ça", 0.0, 0.3), _word(" va,", 0.3, 0.6), _word(" quoi,", 0.6, 0.9),
             _word(" je", 0.9, 1.0), _word(" pense", 1.0, 1.3), _word(" que", 1.3, 1.4),
             _word(" oui,", 1.4, 1.8)]
    ass = run_letterbox(tmp_path, video_dir, words=words)
    assert len(lb_events(ass)) == 2

    fonts = ass.parent / "fonts"
    fonts.mkdir()
    shutil.copyfile(FONT_FILE, fonts / FONT_FILE.name)
    frame = ass.parent / "frame.png"
    # cwd = dossier du .ass : chemins relatifs, pas de ':' de lecteur a echapper
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:size=1080x1920:rate=30:duration=1",
         "-vf", f"ass={ass.name}:fontsdir=fonts", "-frames:v", "1", frame.name],
        cwd=ass.parent, check=True,
    )
    img = Image.open(frame).convert("L")
    w, h = img.size
    pixels = img.load()
    ink = [(x, y) for y in range(h) for x in range(w) if pixels[x, y] > 128]
    assert ink, "aucun texte incruste"
    xs = [x for x, _ in ink]
    ys = sorted({y for _, y in ink})
    assert ZONE["x0"] <= min(xs) and max(xs) <= ZONE["x1"]
    assert ZONE["y0"] <= ys[0] and ys[-1] <= ZONE["y1"]
    # deux blocs de lignes d'encre separes : l'ecart entre leurs hauts = le pas
    runs = [ys[0]] + [b for a, b in zip(ys, ys[1:]) if b != a + 1]
    assert len(runs) == 2, runs
    assert abs((runs[1] - runs[0]) - STEP_68) <= 4


# --------------------------------------------------------------------------
# Agencement stream split (SPEC-76dc) : deux couleurs seulement (texte, mot
# en cours), jamais d'appel LLM d'emphase, style entierement reglable.
# --------------------------------------------------------------------------

SPLIT_ZONE = {"x0": 150, "y0": 710, "x1": 930, "y1": 860}  # split_subtitle_dest par defaut


def run_split(tmp_path, video_dir, words=None, zone=None, config=None, **kwargs):
    write_words(video_dir, words or mock_words())
    with llm.use_backend(FakeBackend([])):  # jamais d'appel LLM pour le style split
        return Path(run(tmp_path, config=config, start=0.0, end=10.0,
                        text_zone=dict(zone or SPLIT_ZONE), style="split", **kwargs))


def _split_layers(path: Path) -> tuple[list[dict], list[dict]]:
    """Evenements du style split : calque de base (ligne entiere, couleur du
    style, toute la duree du groupe -- layers pairs) et calque de
    surbrillance (un evenement par mot, sa propre duree seulement, superpose
    au meme endroit via ``\\pos`` -- layers impairs)."""
    events = lb_events(path)
    base = [ev for ev in events if int(ev["layer"]) % 2 == 0]
    highlights = [ev for ev in events if int(ev["layer"]) % 2 == 1]
    return base, highlights


def test_split_ass_starts_with_a_split_format_comment(tmp_path, video_dir):
    path = run_split(tmp_path, video_dir)
    assert path.read_text(encoding="utf-8").splitlines()[0] == "; format: split"
    doc = parse_ass(path)
    assert doc["info"] == {"PlayResX": "1080", "PlayResY": "1920"}
    assert doc["style"]["Alignment"] == "7"  # haut-gauche : positionne par \pos, pas par les marges


def test_split_never_calls_the_llm_for_emphasis(tmp_path, video_dir):
    # run_split() utilise FakeBackend([]) : un appel leverait AssertionError
    # (aucune reponse scriptee) si generate() en faisait un.
    path = run_split(tmp_path, video_dir)
    assert path.exists()


def test_split_base_line_is_plain_and_stays_visible_for_the_whole_group(tmp_path, video_dir):
    path = run_split(tmp_path, video_dir)
    base, _highlights = _split_layers(path)
    assert base  # au moins une ligne
    for ev in base:
        assert "\\c" not in ev["text"]  # couleur du style (primary), jamais de surcharge
        assert "\\pos" in ev["text"]
    # les 8 mots du texte de mock_words() sont tous representes une fois sur le calque de base
    assert "".join(line_text(ev) for ev in base).replace(" ", "") == "ILN'APASFAITDEGARDEÀVUE"


def test_split_current_word_color_only_covers_its_own_time_span(tmp_path, video_dir):
    from clipper.subtitles import _ass_color

    path = run_split(tmp_path, video_dir)
    base, highlights = _split_layers(path)
    current = _ass_color("#9146FF")  # defaut split_current_word_color
    # un evenement de surbrillance par mot (8 dans mock_words(), "n'a" fusionne)
    assert len(highlights) == 8
    for ev in highlights:
        assert f"\\c{current}" in ev["text"]
        assert "\\pos" in ev["text"]
    # chaque mot mis en valeur ne dure que sa propre fenetre, jamais celle de
    # toute la ligne (contrairement a l'emphase letterbox, statique)
    assert any(ev["end"] != base[0]["end"] for ev in highlights)
    # reconstitue le texte affiche a partir des seuls evenements de surbrillance
    text = " ".join(line_text(ev).strip() for ev in highlights)
    assert text == "IL N'A PAS FAIT DE GARDE À VUE"


def test_split_style_colors_and_outline_are_configurable(tmp_path, video_dir):
    config = make_config(
        tmp_path, split_text_color="#112233", split_current_word_color="#445566",
        split_outline_color="#778899", split_outline=3,
    )
    from clipper.subtitles import _ass_color

    path = run_split(tmp_path, video_dir, config=config)
    doc = parse_ass(path)
    assert doc["style"]["PrimaryColour"] == _ass_color("#112233")
    assert doc["style"]["SecondaryColour"] == _ass_color("#112233")
    assert doc["style"]["OutlineColour"] == _ass_color("#778899")
    assert doc["style"]["Outline"] == "3"
    _base, highlights = _split_layers(path)
    assert highlights and all(f"\\c{_ass_color('#445566')}" in ev["text"] for ev in highlights)


def test_split_uppercase_defaults_to_true(tmp_path, video_dir):
    path = run_split(tmp_path, video_dir)
    base, _highlights = _split_layers(path)
    text = "".join(line_text(ev) for ev in base)
    assert text == text.upper()


def test_split_uppercase_is_configurable(tmp_path, video_dir):
    path = run_split(tmp_path, video_dir, config=make_config(tmp_path, split_uppercase=False))
    _base, highlights = _split_layers(path)
    text = " ".join(line_text(ev).strip() for ev in highlights)
    assert text == "il n'a pas fait de garde à vue"


def test_split_unknown_color_is_an_explicit_error(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    with pytest.raises(SubtitlesError):
        run_split(tmp_path, video_dir, config=make_config(tmp_path, split_text_color="notacolor"))


def test_split_shadow_disabled_by_default_has_no_visible_shadow_style(tmp_path, video_dir):
    path = run_split(tmp_path, video_dir)
    doc = parse_ass(path)
    assert doc["style"]["Shadow"] == "0"


def test_split_shadow_enabled_sets_shadow_distance_and_color(tmp_path, video_dir):
    config = make_config(tmp_path, split_shadow_enabled=True, split_shadow_color="#010203",
                         split_shadow_offset=[4, 4])
    from clipper.subtitles import _ass_color

    path = run_split(tmp_path, video_dir, config=config)
    doc = parse_ass(path)
    assert doc["style"]["Shadow"] == "4"
    assert doc["style"]["BackColour"] == _ass_color("#010203")


def test_split_invalid_shadow_offset_is_an_explicit_error(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    config = make_config(tmp_path, split_shadow_enabled=True, split_shadow_offset=[1, 2, 3])
    with pytest.raises(SubtitlesError):
        run_split(tmp_path, video_dir, config=config)


def _parse_pos(ev: dict) -> tuple[float, float]:
    m = re.search(r"\\pos\(([-\d.]+),([-\d.]+)\)", ev["text"])
    assert m, ev["text"]
    return float(m.group(1)), float(m.group(2))


def test_split_position_is_the_configured_zone_and_text_stays_inside_it(tmp_path, video_dir):
    zone = {"x0": 200, "y0": 750, "x1": 880, "y1": 900}
    path = run_split(tmp_path, video_dir, zone=zone)
    base, highlights = _split_layers(path)
    for ev in base:
        assert (ev["margin_l"], ev["margin_r"]) == (200, 1080 - 880)
        assert_ink_in_zone(ev, int(CONFIG_DEFAULTS["split_font_size"]), zone)
    # chaque mot mis en valeur est positionne (\pos) dans la zone configuree
    for ev in base + highlights:
        x, y = _parse_pos(ev)
        assert zone["x0"] - 5 <= x <= zone["x1"]
        assert zone["y0"] <= y <= zone["y1"]


def test_split_font_size_is_configurable(tmp_path, video_dir):
    path = run_split(tmp_path, video_dir, config=make_config(tmp_path, split_font_size=50))
    doc = parse_ass(path)
    assert doc["style"]["Fontsize"] == str(round(50 * 1762 / 1000))


def test_split_ass_reused_in_split_but_not_in_letterbox_without_force(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    path = run_split(tmp_path, video_dir)
    with llm.use_backend(FakeBackend([])):
        assert Path(run(tmp_path, start=0.0, end=10.0, text_zone=dict(SPLIT_ZONE), style="split")) == path
    with pytest.raises(SubtitlesError, match="--force"):
        run_letterbox(tmp_path, video_dir, zone=SPLIT_ZONE)
    relaid = run_letterbox(tmp_path, video_dir, zone=SPLIT_ZONE, force=True)
    assert relaid.read_text(encoding="utf-8").startswith("; format: letterbox")


def test_generate_unknown_style_is_an_explicit_error(tmp_path, video_dir):
    from clipper.subtitles import SubtitlesError

    with pytest.raises(SubtitlesError, match="style"):
        run_letterbox(tmp_path, video_dir, style="diagonal")


# --------------------------------------------------------------------------
# Aperçu du style (TASK-dd3f, SPEC-c100 E5) : render_preview -> PNG
# --------------------------------------------------------------------------


def _preview_image(section, text="Salut tout le monde", **kwargs):
    import io

    from PIL import Image

    from clipper.subtitles import render_preview

    data = render_preview(section, text, **kwargs)
    assert isinstance(data, bytes) and data.startswith(b"\x89PNG\r\n\x1a\n")
    return Image.open(io.BytesIO(data)).convert("RGB")


def _count(img, rgb):
    raw = img.tobytes()
    return sum(1 for i in range(0, len(raw), 3) if tuple(raw[i:i + 3]) == rgb)


def test_preview_is_a_png_of_the_requested_size_without_ffmpeg(monkeypatch):
    monkeypatch.setenv("PATH", "")  # ni ffmpeg ni aucun binaire : Pillow seul
    assert _preview_image({}).size == (1080, 1920)
    assert _preview_image({}, size=(540, 960)).size == (540, 960)


def test_preview_letterbox_draws_text_outline_and_emphasis_colors():
    img = _preview_image({"primary_color": "&H00FFFFFF&", "outline_color": "&H000000FF&",
                          "emphasis_color": "&H0000FF00&"})
    assert _count(img, (255, 255, 255)) > 200   # texte blanc
    assert _count(img, (255, 0, 0)) > 200       # contour : ASS &H000000FF& = rouge
    assert _count(img, (0, 255, 0)) > 100       # mot d'emphase


def test_preview_letterbox_without_emphasis_has_no_emphasis_color():
    img = _preview_image({"emphasis": False, "emphasis_color": "&H0000FF00&"})
    assert _count(img, (0, 255, 0)) == 0


def test_preview_split_colors_current_word_and_shadow():
    split = {"split_text_color": "#FFFFFF", "split_current_word_color": "#9146FF",
             "split_outline_color": "#FF0000", "split_shadow_enabled": True,
             "split_shadow_color": "#00FF00", "split_shadow_offset": [6, 6]}
    img = _preview_image(split, style="split")
    assert _count(img, (255, 255, 255)) > 200
    assert _count(img, (0x91, 0x46, 0xFF)) > 100   # mot courant
    assert _count(img, (255, 0, 0)) > 200          # contour
    assert _count(img, (0, 255, 0)) > 50           # ombre
    off = _preview_image({**split, "split_shadow_enabled": False}, style="split")
    assert _count(off, (0, 255, 0)) == 0


def test_preview_follows_the_effective_style():
    small = _preview_image({"letterbox_font_size": 40})
    big = _preview_image({"letterbox_font_size": 68})
    assert _count(big, (255, 255, 255)) > _count(small, (255, 255, 255))


@pytest.mark.parametrize("section, style, match", [
    ({"split_text_color": "caca"}, "split", "couleur"),
    ({"split_outline": "gros"}, "split", "split_outline"),
    ({"letterbox_font_size": "x"}, "letterbox", "letterbox_font_size"),
    ({}, "diagonal", "style"),
])
def test_preview_invalid_style_is_an_explicit_error(section, style, match):
    from clipper.subtitles import SubtitlesError, render_preview

    with pytest.raises(SubtitlesError, match=match):
        render_preview(section, "Salut", style=style)


def test_preview_empty_text_is_an_explicit_error():
    from clipper.subtitles import SubtitlesError, render_preview

    with pytest.raises(SubtitlesError, match="texte"):
        render_preview({}, "   ")


# --------------------------------------------------------------------------
# TASK-0aff43d73607 (image-M3) : la regle « plus de 3 fois la meme lettre
# ramenee a 3 » vaut aussi en split, pas seulement en letterbox.
# --------------------------------------------------------------------------


def test_split_repeated_letters_run_is_shortened_to_three_like_letterbox(tmp_path, video_dir, caplog):
    words = [_word(" G" + "R" * 100, 0.0, 1.0), _word(" ok", 1.0, 1.2)]
    with caplog.at_level("INFO", logger="clipper.subtitles"):
        path = run_split(tmp_path, video_dir, words=words)
    base, highlights = _split_layers(path)
    texts = [line_text(ev).strip() for ev in base]
    assert texts == ["GRRR OK"]
    assert "R" * 4 not in "".join(line_text(ev) for ev in base + highlights)
    assert any("1 mot" in r.getMessage() for r in caplog.records)
    saved = json.loads((video_dir / "transcript.json").read_text(encoding="utf-8"))
    assert saved["segments"][0]["words"][0]["word"] == " G" + "R" * 100
