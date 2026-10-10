from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from clipper import llm
from clipper.config import Config
from clipper.llm.fake import FakeBackend
from clipper.vision import LABEL_HEIGHT

VIDEO_ID = "abcdefghijk"

# Fenetres +/-10 s : moment retenu [100-130] -> [90-140] ; rejet rattrapable
# [300-330] -> [290-340] (score 59 < min_score 60, +2 de bonus visuel = 61).
TIMECODES = [50.0, 89.0, 91.0, 115.0, 139.0, 141.0, 200.0, 295.0, 335.0, 345.0]

FRAME_WIDTH = 800
FRAME_HEIGHT = 450


def frame_name(t):
    return f"frames/f{int(t):04d}.jpg"


def image_size(path):
    img = cv2.imread(str(path))
    h, w = img.shape[:2]
    return w, h


def seed_scenes(video_dir, timecodes=TIMECODES, width=FRAME_WIDTH, height=FRAME_HEIGHT):
    (video_dir / "frames").mkdir(exist_ok=True)
    frames = []
    for n, t in enumerate(timecodes):
        img = np.zeros((height, width, 3), dtype=np.uint8)
        cv2.imwrite(str(video_dir / frame_name(t)), img)
        frames.append({"path": frame_name(t), "timecode": t, "scene": n})
    (video_dir / "scenes.json").write_text(
        json.dumps({"scenes": [{"start": 0.0, "end": 500.0}], "frames": frames}), encoding="utf-8"
    )


def seed_colored_scenes(video_dir, timecodes=TIMECODES, width=FRAME_WIDTH, height=FRAME_HEIGHT):
    """Comme seed_scenes, mais chaque image (ordre croissant de timecode) a
    une couleur unie distincte : sert a verifier par pixel que la planche
    contient bien chaque image selectionnee, dans l'ordre. Renvoie
    {timecode: couleur}."""
    (video_dir / "frames").mkdir(exist_ok=True)
    frames = []
    colors = {}
    for n, t in enumerate(sorted(timecodes)):
        color = (20 + 15 * n, 60 + 15 * n, 90 + 15 * n)  # reste < 256 (jusqu'a n=9)
        img = np.full((height, width, 3), color, dtype=np.uint8)
        cv2.imwrite(str(video_dir / frame_name(t)), img)
        frames.append({"path": frame_name(t), "timecode": t, "scene": n})
        colors[t] = color
    (video_dir / "scenes.json").write_text(
        json.dumps({"scenes": [{"start": 0.0, "end": 500.0}], "frames": frames}), encoding="utf-8"
    )
    return colors


def seed_rubric(tmp_path, visual=2, max_total=6):
    path = tmp_path / "rubric.toml"
    path.write_text(f"[bonus]\nvisual = {visual}\nmax_total = {max_total}\n", encoding="utf-8")
    return path


def make_moments(tmp_path, *, min_score=60, moments=None, rejected=None):
    """moments.json realiste : un moment retenu et un rejet rattrapable par
    le bonus visuel (score 59 + visual 2 = 61 >= min_score 60), comme le
    produirait clipper.moments (rubric.path/min_score, final_score, bonus)."""
    rubric_path = seed_rubric(tmp_path)
    if moments is None:
        moments = [{"id": 0, "start": 100.0, "end": 130.0, "hook_text": "accroche"}]
    if rejected is None:
        rejected = [
            {
                "start": 300.0, "end": 330.0, "reason": "score 59.0 < min_score 60",
                "final_score": 59.0,
                "bonus": {"replayed": 0.0, "audio_peaks": 0.0, "visual": 0.0, "total": 0.0},
            }
        ]
    return {
        "video_id": VIDEO_ID,
        "rubric": {"path": str(rubric_path), "min_score": min_score},
        "moments": moments,
        "rejected": rejected,
    }


def write_moments(video_dir, tmp_path, **overrides):
    data = make_moments(tmp_path, **overrides)
    (video_dir / "moments.json").write_text(json.dumps(data), encoding="utf-8")
    return data


@pytest.fixture
def video_dir(tmp_path):
    d = tmp_path / "workspace" / VIDEO_ID
    d.mkdir(parents=True)
    write_moments(d, tmp_path)
    seed_scenes(d)
    return d


def make_config(tmp_path, **sections):
    return Config(
        mode="review",
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "output",
        _sections=sections,
    )


def describe_all(striking_at=(), tags=("plan large",), record=None):
    """Reponse fake : decrit chaque image du lot, marquante si son timecode
    (lu dans le prompt, la planche etant un fichier image le prompt reste la
    seule source lisible par le test) est dans ``striking_at``. Si ``record``
    est fourni, y ajoute la taille (largeur, hauteur) de la planche recue :
    le fichier temporaire est supprime des la fin de l'etape, donc lire sa
    taille doit se faire pendant l'appel."""

    def answer(request):
        if record is not None:
            record.append(image_size(request.images[0]))
        frames = []
        for index_str, t_str in re.findall(r"Image (\d+) : ([\d.]+) s", request.prompt):
            t = float(t_str)
            frames.append(
                {
                    "index": int(index_str),
                    "description": f"image a {t:.1f}",
                    "tags": list(tags),
                    "striking": t in striking_at,
                }
            )
        return {"frames": frames}

    return answer


def run_vision(tmp_path, responses, **settings):
    from clipper.vision import run

    fake = FakeBackend(responses)
    config = make_config(tmp_path, vision=settings) if settings else make_config(tmp_path)
    with llm.use_backend(fake):
        path = run(VIDEO_ID, tmp_path / "workspace", config=config)
    return fake, path


def prompt_timecodes(fake):
    """Timecodes listes dans les prompts envoyes, tous appels confondus."""
    return [
        float(t)
        for call in fake.calls
        for _, t in re.findall(r"Image (\d+) : ([\d.]+) s", call.prompt)
    ]


# --------------------------------------------------------------------------
# Selection des candidats : moments retenus + rejets rattrapables seulement
# --------------------------------------------------------------------------


def test_kept_moment_and_rescuable_rejected_windows_are_sent(tmp_path, video_dir):
    fake, _ = run_vision(tmp_path, [describe_all()] * 5)

    assert sorted(prompt_timecodes(fake)) == [91.0, 115.0, 139.0, 295.0, 335.0]


def test_no_candidate_window_means_no_llm_call(tmp_path, video_dir):
    write_moments(video_dir, tmp_path, moments=[], rejected=[])

    fake, path = run_vision(tmp_path, [])

    assert fake.calls == []
    assert json.loads(path.read_text(encoding="utf-8"))["frames"] == []


def test_rejection_without_bounds_opens_no_window(tmp_path, video_dir):
    write_moments(
        video_dir, tmp_path,
        rejected=[{
            "reason": "bornes absentes", "final_score": 59.0,
            "bonus": {"total": 0.0, "visual": 0.0},
        }],
    )

    fake, _ = run_vision(tmp_path, [describe_all()] * 3)

    assert sorted(prompt_timecodes(fake)) == [91.0, 115.0, 139.0]


def test_rejected_candidate_below_min_score_even_with_visual_bonus_gets_no_window(tmp_path, video_dir):
    write_moments(
        video_dir, tmp_path,
        rejected=[{
            "start": 300.0, "end": 330.0, "reason": "score 50.0 < min_score 60",
            "final_score": 50.0,
            "bonus": {"total": 0.0, "visual": 0.0},
        }],
    )

    fake, _ = run_vision(tmp_path, [describe_all()] * 3)

    assert sorted(prompt_timecodes(fake)) == [91.0, 115.0, 139.0]


def test_rejected_candidate_already_at_bonus_max_total_gets_no_window(tmp_path, video_dir):
    write_moments(
        video_dir, tmp_path,
        rejected=[{
            "start": 300.0, "end": 330.0, "reason": "score 59.0 < min_score 60",
            "final_score": 59.0,
            "bonus": {"total": 6.0, "visual": 0.0},  # deja au max_total (6) : pas de place pour le visuel
        }],
    )

    fake, _ = run_vision(tmp_path, [describe_all()] * 3)

    assert sorted(prompt_timecodes(fake)) == [91.0, 115.0, 139.0]


def test_rejected_candidate_without_score_data_gets_no_window(tmp_path, video_dir):
    write_moments(
        video_dir, tmp_path,
        rejected=[{
            "start": 300.0, "end": 330.0,
            "reason": "chevauche un segment SponsorBlock sponsor [300.0-330.0]",
        }],
    )

    fake, _ = run_vision(tmp_path, [describe_all()] * 3)

    assert sorted(prompt_timecodes(fake)) == [91.0, 115.0, 139.0]


# --------------------------------------------------------------------------
# Appel LLM : usage vision, images jointes, par lots
# --------------------------------------------------------------------------


def test_frames_are_described_by_llm_vision_in_batches(tmp_path, video_dir):
    sizes = []
    fake, _ = run_vision(tmp_path, [describe_all(record=sizes)] * 3, batch_size=2, parallel=1)

    assert [c.usage for c in fake.calls] == ["vision", "vision", "vision"]
    assert [len(c.images) for c in fake.calls] == [1, 1, 1]  # une seule planche par appel
    assert len(sizes) == 3  # chaque planche existait bien au moment de l'appel
    assert "115.0 s" in fake.calls[0].prompt


def test_each_call_sends_a_single_montage_image_for_the_whole_batch(tmp_path, video_dir):
    fake, _ = run_vision(tmp_path, [describe_all()], batch_size=8)

    assert len(fake.calls) == 1
    assert len(fake.calls[0].images) == 1


def test_montage_contains_every_selected_frame_labeled_in_timecode_order(tmp_path, video_dir):
    """La planche contient bien chaque image du lot (verifie par une couleur
    unie distincte par image, echantillonnee sous la bande de legende), dans
    l'ordre des timecodes, avec une legende dessinee au-dessus de chacune
    (bande de tete non uniformement noire)."""
    colors = seed_colored_scenes(video_dir)
    captured = []

    def capture(request):
        img = cv2.imread(str(request.images[0]))
        h, w = img.shape[:2]
        n = len(re.findall(r"Image \d+ : [\d.]+ s", request.prompt))
        cell_w = w // n
        for i in range(n):
            label_region = img[0:LABEL_HEIGHT, i * cell_w : (i + 1) * cell_w]
            assert label_region.max() > 200, f"pas de legende dessinee pour l'image {i}"
            x, y = i * cell_w + cell_w // 2, h - 5
            captured.append(tuple(int(c) for c in img[y, x]))
        return describe_all()(request)

    fake, _ = run_vision(tmp_path, [capture])

    # tolerance : la planche est ecrite en JPEG (compression avec pertes)
    expected = [colors[t] for t in (91.0, 115.0, 139.0, 295.0, 335.0)]
    assert len(captured) == len(expected)
    for got, exp in zip(captured, expected):
        assert all(abs(g - e) <= 6 for g, e in zip(got, exp)), (captured, expected)


def test_answer_missing_a_frame_is_a_failure_and_writes_nothing(tmp_path, video_dir):
    short = {"frames": [{"index": 0, "description": "x", "tags": [], "striking": False}]}

    with pytest.raises(llm.SchemaError):
        run_vision(tmp_path, [short], batch_size=2)
    assert not (video_dir / "vision.json").exists()


def test_answer_with_duplicate_index_is_a_failure(tmp_path, video_dir):
    dup = {"frames": [{"index": 0, "description": "x", "tags": [], "striking": False}] * 2}

    with pytest.raises(llm.SchemaError, match="index"):
        run_vision(tmp_path, [dup], batch_size=2)
    assert not (video_dir / "vision.json").exists()


def test_llm_unavailable_propagates_and_writes_nothing(tmp_path, video_dir):
    with pytest.raises(llm.TransientLLMError):
        run_vision(tmp_path, [llm.TransientLLMError("quota")])
    assert not (video_dir / "vision.json").exists()


# --------------------------------------------------------------------------
# Sortie : vision.json
# --------------------------------------------------------------------------


def test_writes_vision_json_with_timecode_description_tags(tmp_path, video_dir):
    _, path = run_vision(tmp_path, [describe_all(striking_at=(115.0,), tags=("explosion", "foule"))] * 5)

    assert path == video_dir / "vision.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["frames"][:2] == [
        {"timecode": 91.0, "path": "frames/f0091.jpg", "description": "image a 91.0",
         "tags": ["explosion", "foule"], "striking": False},
        {"timecode": 115.0, "path": "frames/f0115.jpg", "description": "image a 115.0",
         "tags": ["explosion", "foule"], "striking": True},
    ]
    assert [f["timecode"] for f in data["frames"]] == [91.0, 115.0, 139.0, 295.0, 335.0]


def test_existing_result_is_not_recomputed_unless_forced(tmp_path, video_dir):
    from clipper.vision import run

    (video_dir / "vision.json").write_text('{"frames": []}', encoding="utf-8")
    fake = FakeBackend([])
    with llm.use_backend(fake):
        run(VIDEO_ID, tmp_path / "workspace", config=make_config(tmp_path))
    assert fake.calls == []

    fake = FakeBackend([describe_all()] * 5)
    with llm.use_backend(fake):
        run(VIDEO_ID, tmp_path / "workspace", config=make_config(tmp_path), force=True)
    assert len(json.loads((video_dir / "vision.json").read_text(encoding="utf-8"))["frames"]) == 5


def test_missing_input_is_an_error(tmp_path, video_dir):
    from clipper.vision import VisionError

    (video_dir / "moments.json").unlink()
    with pytest.raises(VisionError, match="moments.json"):
        run_vision(tmp_path, [])


def test_missing_frame_file_is_an_error(tmp_path, video_dir):
    from clipper.vision import VisionError

    (video_dir / "frames" / "f0115.jpg").unlink()
    with pytest.raises(VisionError, match="f0115.jpg"):
        run_vision(tmp_path, [describe_all()] * 5)


def test_vision_section_is_configurable(tmp_path):
    config = make_config(tmp_path)
    section = config.section("vision")
    assert section["window_seconds"] == 10
    assert section["batch_size"] == 8
    assert section["max_width"] == 768
    assert section["parallel"] == 4


# --------------------------------------------------------------------------
# Images reduites : max_width, proportions conservees, originaux intacts,
# dossier temporaire supprime en fin d'etape
# --------------------------------------------------------------------------


def test_montage_cells_are_resized_to_max_width_preserving_aspect_ratio(tmp_path, video_dir):
    sizes = []
    run_vision(tmp_path, [describe_all(record=sizes)], max_width=200)

    assert len(sizes) == 1  # une seule planche, les 5 images selectionnees cote a cote
    w, h = sizes[0]
    assert w == 200 * 5
    assert abs((h - LABEL_HEIGHT) / 200 - FRAME_HEIGHT / FRAME_WIDTH) < 0.01


def test_montage_cells_are_not_upscaled_past_original_width(tmp_path, video_dir):
    sizes = []
    run_vision(tmp_path, [describe_all(record=sizes)], max_width=4000)

    assert len(sizes) == 1
    w, h = sizes[0]
    assert w == FRAME_WIDTH * 5
    assert h == FRAME_HEIGHT + LABEL_HEIGHT


def test_original_frame_files_are_untouched_after_resize(tmp_path, video_dir):
    frames_dir = video_dir / "frames"
    before = {p.name: image_size(p) for p in frames_dir.glob("*.jpg")}

    run_vision(tmp_path, [describe_all()] * 5, max_width=100)

    after = {p.name: image_size(p) for p in frames_dir.glob("*.jpg")}
    assert after == before


def test_temporary_resize_folder_is_removed_after_a_successful_step(tmp_path, video_dir):
    before = set(video_dir.iterdir())

    run_vision(tmp_path, [describe_all()] * 5)

    after = set(video_dir.iterdir())
    assert after - before == {video_dir / "vision.json"}


def test_temporary_resize_folder_is_removed_even_if_a_batch_fails(tmp_path, video_dir):
    before = set(video_dir.iterdir())

    with pytest.raises(llm.TransientLLMError):
        run_vision(tmp_path, [llm.TransientLLMError("quota")])

    after = set(video_dir.iterdir())
    assert after - before <= {video_dir / "vision_partial.json"}


# --------------------------------------------------------------------------
# Lots traites en parallele (parallel), meme resultat que sequentiel
# --------------------------------------------------------------------------


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
        return {
            "frames": [
                {"index": n, "description": "x", "tags": [], "striking": False}
                for n in range(len(request.images))
            ]
        }


def test_batches_run_concurrently_up_to_parallel_setting(tmp_path, video_dir):
    tracker = ConcurrencyTracker(expected=3)
    fake, _ = run_vision(tmp_path, [tracker.response] * 5, batch_size=1, parallel=3)

    assert tracker.peak == 3
    assert len(fake.calls) == 5


def test_parallel_defaults_to_four(tmp_path, video_dir):
    tracker = ConcurrencyTracker(expected=4)
    fake, _ = run_vision(tmp_path, [tracker.response] * 5, batch_size=1)

    assert tracker.peak == 4


def test_vision_json_order_matches_sequential_regardless_of_batch_completion_order(tmp_path, video_dir):
    """Chaque lot est decrit d'apres son propre contenu (le timecode lu dans
    sa propre planche/prompt, pas d'apres l'ordre d'arrivee des reponses
    scriptees ni l'ordre d'appel du backend) : le resultat final reste trie
    par timecode, quel que soit l'ordre d'execution des threads."""

    def respond(request):
        t = float(re.search(r"Image \d+ : ([\d.]+) s", request.prompt).group(1))
        time.sleep(0.01 * (400.0 - t) / 100)  # ordre d'arrivee inverse au timecode
        return {"frames": [{"index": 0, "description": f"lot{t:.1f}", "tags": [], "striking": False}]}

    fake, path = run_vision(tmp_path, [respond], batch_size=1, parallel=5)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert [f["timecode"] for f in data["frames"]] == [91.0, 115.0, 139.0, 295.0, 335.0]
    assert [f["description"] for f in data["frames"]] == [
        f"lot{t:.1f}" for t in (91.0, 115.0, 139.0, 295.0, 335.0)
    ]


# --------------------------------------------------------------------------
# Reprise : vision_partial.json enregistre au fil de l'eau, un lot deja
# decrit n'est pas redemande apres une relance
# --------------------------------------------------------------------------


def test_partial_file_stays_valid_after_concurrent_batch_writes(tmp_path, video_dir):
    # le lot qui echoue est choisi par son propre contenu (timecode 139.0,
    # 3e des 5 lots par ordre de timecode), pas par l'ordre d'appel du
    # backend : construire la planche est desormais fait dans chaque thread,
    # donc cet ordre n'est plus deterministe.
    def respond(request):
        if "139.0 s" in request.prompt:
            raise llm.TransientLLMError("quota")
        return describe_all()(request)

    with pytest.raises(llm.TransientLLMError):
        run_vision(tmp_path, [respond], batch_size=1, parallel=4)

    partial = json.loads((video_dir / "vision_partial.json").read_text(encoding="utf-8"))
    assert sorted(int(k) for k in partial["batches"]) == [0, 1, 3, 4]
    assert len(partial["batches"]["0"]["frames"]) == 1


def test_retry_after_batch_failure_does_not_redescribe_completed_batches(tmp_path, video_dir):
    def respond(request):
        if "115.0 s" in request.prompt:
            raise llm.TransientLLMError("quota")
        return describe_all()(request)

    with pytest.raises(llm.TransientLLMError):
        run_vision(tmp_path, [respond], batch_size=1, parallel=4)
    partial_before = json.loads((video_dir / "vision_partial.json").read_text(encoding="utf-8"))
    assert sorted(int(k) for k in partial_before["batches"]) == [0, 2, 3, 4]

    fake, path = run_vision(tmp_path, [describe_all()], batch_size=1, parallel=4)

    assert [c.usage for c in fake.calls] == ["vision"]  # seul le lot rate est redemande
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [f["timecode"] for f in data["frames"]] == [91.0, 115.0, 139.0, 295.0, 335.0]
    assert not (video_dir / "vision_partial.json").exists()


def test_batch_failure_fails_the_step_with_its_reason(tmp_path, video_dir):
    with pytest.raises(llm.TransientLLMError, match="quota du lot 2"):
        run_vision(
            tmp_path,
            [describe_all(), llm.TransientLLMError("quota du lot 2"), describe_all(), describe_all(), describe_all()],
            batch_size=1, parallel=4,
        )
    assert not (video_dir / "vision.json").exists()


# --------------------------------------------------------------------------
# moments relance avec vision.json : les notes peuvent changer
# --------------------------------------------------------------------------

RUBRIC = """
min_score = 60
max_moments_per_hour = 1000
always_keep_score = 1000
min_moments_cap = 1
trend_keywords = []

[criteria.hook]
weight = 3
question = "Accroche ?"
[criteria.standalone]
weight = 3
question = "Autonome ?"
[criteria.payoff]
weight = 2
question = "Chute ?"
[criteria.emotion]
weight = 2
question = "Reaction forte ?"
[criteria.value]
weight = 2
question = "Info ?"
[criteria.trend]
weight = 1
question = "Tendance ?"

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
sponsorblock_categories = ["sponsor"]
"""


def seed_moments_inputs(tmp_path, video_dir):
    segments = []
    for k in range(100):
        start = 5 * k + 0.25
        words = [
            {"word": f" mot{k}_{i}" + ("." if i == 4 else ""), "start": start + i * 0.9,
             "end": start + i * 0.9 + 0.8, "probability": 0.9}
            for i in range(5)
        ]
        segments.append({"id": k, "start": words[0]["start"], "end": words[-1]["end"],
                         "text": "".join(w["word"] for w in words), "words": words})
    (video_dir / "transcript.json").write_text(json.dumps({"segments": segments}), encoding="utf-8")
    (video_dir / "meta.json").write_text(json.dumps({"title": "t", "duration": 500.0}), encoding="utf-8")
    (video_dir / "audio.json").write_text(json.dumps({"peaks": []}), encoding="utf-8")
    rubric = tmp_path / "rubric.toml"
    rubric.write_text(RUBRIC, encoding="utf-8")
    return rubric


def emotion_from_prompt(request):
    """Fake moments : emotion 9 si une image marquante est decrite dans le
    prompt, 5 sinon."""
    emotion = 9 if "(marquant)" in request.prompt else 5
    return {
        "moments": [
            {
                "hook_text": "accroche", "start": 100.25, "end": 129.65, "format": "single",
                "part_breaks": [], "justification": "ca marche",
                "scores": {"hook": 9, "standalone": 8, "payoff": 7, "emotion": emotion, "value": 5, "trend": 0},
            }
        ]
    }


def test_rerun_moments_revises_emotion_from_a_striking_description(tmp_path):
    from clipper.moments import run as run_moments
    from clipper.vision import run as run_vision_step

    video_dir = tmp_path / "workspace" / VIDEO_ID
    video_dir.mkdir(parents=True)
    seed_scenes(video_dir)
    rubric = seed_moments_inputs(tmp_path, video_dir)
    config = make_config(tmp_path, moments={"rubric_path": str(rubric)})
    workspace = tmp_path / "workspace"

    with llm.use_backend(FakeBackend([emotion_from_prompt])):
        run_moments(VIDEO_ID, workspace, config=config)
    before = json.loads((video_dir / "moments.json").read_text(encoding="utf-8"))
    assert before["moments"][0]["scores"]["emotion"] == 5

    with llm.use_backend(FakeBackend([describe_all(striking_at=(115.0,))] * 5)):
        run_vision_step(VIDEO_ID, workspace, config=config)

    with llm.use_backend(FakeBackend([emotion_from_prompt])) as fake:
        run_moments(VIDEO_ID, workspace, config=config, force=True)
    assert "image a 115.0 (marquant)" in fake.calls[0].prompt
    after = json.loads((video_dir / "moments.json").read_text(encoding="utf-8"))
    assert after["moments"][0]["scores"]["emotion"] == 9
    assert after["moments"][0]["bonus"]["visual"] == 2


# --------------------------------------------------------------------------
# Integration reelle (VLM ou Claude) : sautee par defaut
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("CLIPPER_CLAUDE_INTEGRATION") != "1",
    reason="integration Claude : definir CLIPPER_CLAUDE_INTEGRATION=1 (consomme du quota)",
)
def test_real_llm_describes_a_real_frame(tmp_path, video_dir):
    from clipper.vision import run

    img = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(img, "BOOM", (180, 200), cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 0, 255), 8)
    for t in TIMECODES:
        cv2.imwrite(str(video_dir / frame_name(t)), img)

    path = run(VIDEO_ID, tmp_path / "workspace", config=make_config(tmp_path))

    frames = json.loads(Path(path).read_text(encoding="utf-8"))["frames"]
    assert len(frames) == 5
    assert all(f["description"].strip() for f in frames)


# --------------------------------------------------------------------------
# Revue r-pipeline 08/10 (Important 3) : un lot sauve n'est repris que s'il
# porte les memes images ; --force ignore et supprime vision_partial.json.
# --------------------------------------------------------------------------


def _fail_on(timecode):
    def respond(request):
        if f"{timecode} s" in request.prompt:
            raise llm.TransientLLMError("quota")
        return describe_all()(request)

    return respond


def test_saved_batch_records_the_image_paths_it_covers(tmp_path, video_dir):
    with pytest.raises(llm.TransientLLMError):
        run_vision(tmp_path, [_fail_on("115.0")], batch_size=1, parallel=4)

    partial = json.loads((video_dir / "vision_partial.json").read_text(encoding="utf-8"))
    assert partial["batches"]["0"]["paths"] == ["frames/f0091.jpg"]
    assert partial["batches"]["0"]["frames"][0]["timecode"] == 91.0


def test_saved_batch_with_other_images_is_recomputed(tmp_path, video_dir):
    with pytest.raises(llm.TransientLLMError):
        run_vision(tmp_path, [_fail_on("115.0")], batch_size=1, parallel=4)
    # moments refait : les fenetres changent, le lot 0 ne couvre plus la meme image
    write_moments(video_dir, tmp_path, moments=[{"id": 0, "start": 40.0, "end": 60.0, "hook_text": "x"}], rejected=[])

    fake, path = run_vision(tmp_path, [describe_all()], batch_size=1, parallel=4)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert [f["timecode"] for f in data["frames"]] == [50.0]
    assert [f["description"] for f in data["frames"]] == ["image a 50.0"]
    assert len(fake.calls) == 1


def test_saved_batch_with_the_same_images_is_reused(tmp_path, video_dir):
    with pytest.raises(llm.TransientLLMError):
        run_vision(tmp_path, [_fail_on("115.0")], batch_size=1, parallel=4)

    fake, _ = run_vision(tmp_path, [describe_all()], batch_size=1, parallel=4)

    assert len(fake.calls) == 1  # seul le lot rate


def test_saved_batch_in_the_old_format_is_recomputed(tmp_path, video_dir):
    # format d'avant TASK-732f : liste de frames sans chemins -> jamais reprise, sans erreur
    (video_dir / "vision_partial.json").write_text(
        json.dumps({"batches": {"0": [{"timecode": 91.0, "description": "vieux"}]}}), encoding="utf-8")

    fake, _ = run_vision(tmp_path, [describe_all()], batch_size=1, parallel=4)

    assert len(fake.calls) == 5


def test_force_ignores_and_deletes_the_partial_file(tmp_path, video_dir):
    from clipper.vision import run

    with pytest.raises(llm.TransientLLMError):
        run_vision(tmp_path, [_fail_on("115.0")], batch_size=1, parallel=4)
    assert (video_dir / "vision_partial.json").exists()

    fake = FakeBackend([describe_all()])
    with llm.use_backend(fake):
        run(VIDEO_ID, tmp_path / "workspace", config=make_config(tmp_path, vision={"batch_size": 1, "parallel": 4}),
            force=True)

    assert len(fake.calls) == 5  # les 5 lots redemandes
    assert not (video_dir / "vision_partial.json").exists()


def test_force_deletes_the_partial_file_even_if_the_rerun_fails(tmp_path, video_dir):
    from clipper.vision import run

    with pytest.raises(llm.TransientLLMError):
        run_vision(tmp_path, [_fail_on("115.0")], batch_size=1, parallel=4)

    fake = FakeBackend([_fail_on("91.0")])
    with llm.use_backend(fake), pytest.raises(llm.TransientLLMError):
        run(VIDEO_ID, tmp_path / "workspace", config=make_config(tmp_path, vision={"batch_size": 1, "parallel": 1}),
            force=True)

    partial = json.loads((video_dir / "vision_partial.json").read_text(encoding="utf-8"))
    assert all(len(b["paths"]) == 1 for b in partial["batches"].values())
    assert len(fake.calls) >= 1


def test_answer_with_wrong_indexes_is_sent_back_to_the_model_and_corrected(tmp_path, video_dir):
    # jury-I3 : le controle des index est un check= de llm.ask, donc reparable.
    good = describe_all()
    calls = []

    def flaky(request):
        calls.append(request.prompt)
        answer = good(request)
        if len(calls) == 1:
            # bon nombre d'images (schema OK) mais l'index 0 deux fois
            answer["frames"] = [dict(answer["frames"][0]) for _ in answer["frames"]]
        return answer

    fake, path = run_vision(tmp_path, [flaky] * 6, batch_size=2, parallel=1)
    assert path.exists()
    assert len(calls) >= 2 and calls[1].startswith(calls[0])
