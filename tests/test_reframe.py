from __future__ import annotations

import atexit
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pytest

from clipper import llm
from clipper.config import Config
from clipper.gpu import Device
from clipper.llm.fake import FakeBackend

VIDEO_ID = "abcdefghijk"
W, H = 1920, 1080
# Largeur d'un cadre 9:16 pleine hauteur dans une source 1920x1080.
CROP_W = 608


# --------------------------------------------------------------------------
# Fixtures : video, detecteur et reponses LLM factices. Les visages sont des
# boites englobantes synthetiques, fonction du temps (secondes, absolues).
# --------------------------------------------------------------------------


class FakeVideo:
    """Frame source factice : des images noires aux temps demandes. Garde le
    temps courant pour que le detecteur factice sache quoi renvoyer."""

    def __init__(self, width=W, height=H):
        self.width = width
        self.height = height
        self.current_t = None
        self.requested: list[float] = []

    def __call__(self, video_path, times):
        assert Path(video_path).exists()
        for t in times:
            self.requested.append(t)
            self.current_t = t
            yield t, np.zeros((self.height, self.width, 3), dtype=np.uint8)


class FakeDetector:
    def __init__(self, video, boxes_fn):
        self.video = video
        self.boxes_fn = boxes_fn
        self.closed = False

    def detect(self, frame):
        assert not self.closed, "detecteur utilise apres close()"
        assert frame.shape[:2] == (self.video.height, self.video.width)
        return [(*box, 0.9) for box in self.boxes_fn(self.video.current_t)]

    def close(self):
        self.closed = True


class FakeDetectorFactory:
    def __init__(self, video, boxes_fn):
        self.video = video
        self.boxes_fn = boxes_fn
        self.built: list[tuple[dict, Device]] = []
        self.detectors: list[FakeDetector] = []

    def __call__(self, settings, device):
        self.built.append((settings, device))
        detector = FakeDetector(self.video, self.boxes_fn)
        self.detectors.append(detector)
        return detector


def static(*boxes):
    return lambda t: list(boxes)


def make_config(tmp_path, **reframe):
    # Le format par defaut de CONFIG_DEFAULTS est devenu "letterbox" (SPEC-6127) ;
    # ces tests exercent le format "crop" (code actuel, inchange), sauf demande
    # explicite d'un autre format.
    reframe.setdefault("format", "crop")
    return Config(
        mode="review",
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "output",
        _sections={"reframe": reframe},
    )


@pytest.fixture
def video_dir(tmp_path):
    d = tmp_path / "workspace" / VIDEO_ID
    d.mkdir(parents=True)
    (d / f"{VIDEO_ID}.mp4").write_bytes(b"not really a video")
    write_scenes(d, [(0.0, 100.0)])
    return d


def write_scenes(video_dir, scenes):
    (video_dir / "scenes.json").write_text(
        json.dumps({"scenes": [{"start": s, "end": e} for s, e in scenes], "frames": []}),
        encoding="utf-8",
    )


def single(face, ignore=None):
    answer = {"layout": "single", "camera": None, "face": face, "reason": "r"}
    if ignore is not None:
        answer["ignore"] = list(ignore)
    return answer


def facecam(x, y, w, h):
    return {"layout": "facecam_gameplay", "camera": {"x": x, "y": y, "w": w, "h": h}, "face": None, "reason": "r"}


def run(tmp_path, boxes_fn, responses, *, start=10.0, end=14.0, clip_id="01", video=None, force=False, **reframe):
    from clipper.reframe import reframe as do_reframe

    video = video or FakeVideo()
    factory = FakeDetectorFactory(video, boxes_fn)
    fake = FakeBackend(responses)
    with llm.use_backend(fake):
        out = do_reframe(
            VIDEO_ID,
            clip_id,
            start,
            end,
            tmp_path / "workspace",
            config=make_config(tmp_path, **reframe),
            force=force,
            detector_factory=factory,
            frame_source=video,
        )
    return out, factory, fake


def load(out):
    return json.loads(Path(out).read_text(encoding="utf-8"))


def rects(plan, panel="main"):
    [p] = [p for p in plan["panels"] if p["name"] == panel]
    return p["rects"]


def contains(rect, box, eps=1e-6):
    x0, y0, x1, y1 = box
    return (
        rect["x"] <= x0 + eps
        and rect["y"] <= y0 + eps
        and x1 - eps <= rect["x"] + rect["w"]
        and y1 - eps <= rect["y"] + rect["h"]
    )


def cuts(rect, box):
    """Le cadre coupe le visage : il le touche sans le contenir entierement."""
    x0, y0, x1, y1 = box
    rx0, ry0, rx1, ry1 = rect["x"], rect["y"], rect["x"] + rect["w"], rect["y"] + rect["h"]
    intersects = x0 < rx1 and rx0 < x1 and y0 < ry1 and ry0 < y1
    return intersects and not contains(rect, box)


def times_in(rect, n=5):
    """Instants a verifier sur l'intervalle d'un rectangle, bornes comprises."""
    return [rect["start"] + (rect["end"] - rect["start"]) * k / (n - 1) for k in range(n)]


def assert_covers(rect_list, start, end):
    assert rect_list[0]["start"] == pytest.approx(start)
    assert rect_list[-1]["end"] == pytest.approx(end)
    for a, b in zip(rect_list, rect_list[1:]):
        assert a["end"] == pytest.approx(b["start"])


# --------------------------------------------------------------------------
# Sortie et plans
# --------------------------------------------------------------------------


def test_writes_workspace_reframe_clip_json(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)], clip_id="03-p2")

    assert Path(out) == video_dir / "reframe" / "03-p2.json"
    data = load(out)
    assert data["video_id"] == VIDEO_ID
    assert data["clip_id"] == "03-p2"
    assert (data["start"], data["end"]) == (10.0, 14.0)
    assert data["source"] == {"width": W, "height": H}
    assert data["output"] == {"width": 1080, "height": 1920}
    assert data["layout"] == "single"


def test_one_llm_call_per_plan_of_the_clip_with_an_annotated_image(tmp_path, video_dir):
    write_scenes(video_dir, [(0.0, 12.0), (12.0, 13.0), (13.0, 30.0)])
    out, _, fake = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)] * 3, start=10.0, end=14.0)

    data = load(out)
    assert [(p["start"], p["end"]) for p in data["plans"]] == [(10.0, 12.0), (12.0, 13.0), (13.0, 14.0)]
    assert len(fake.calls) == 3
    for call, plan in zip(fake.calls, data["plans"]):
        assert call.usage == "layout"
        [image] = call.images
        assert image.suffix == ".jpg" and image.exists()
        assert image.read_bytes()[:2] == b"\xff\xd8"  # JPEG
        assert (video_dir / plan["image"]) == image
        assert "#0" in call.prompt  # identifiant du visage annote sur l'image
    # Les rectangles couvrent le clip sans trou, plan apres plan.
    all_rects = [r for p in data["plans"] for r in rects(p)]
    assert_covers(all_rects, 10.0, 14.0)


def test_annotated_image_draws_the_detected_faces(tmp_path, video_dir):
    import cv2

    out, _, fake = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)])
    image = cv2.imread(str(fake.calls[0].images[0]))
    assert image is not None
    # Une image noire en entree : seuls les traces d'annotation sont non nuls.
    assert image.sum() > 0


def test_existing_result_is_not_recomputed_without_force(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)])
    out2, factory, fake = run(tmp_path, static((1100, 300, 1300, 500)), [])
    assert out2 == out
    assert factory.built == [] and fake.calls == []

    out3, factory, fake = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)], force=True)
    assert len(fake.calls) == 1


def test_missing_scenes_json_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    (video_dir / "scenes.json").unlink()
    with pytest.raises(ReframeError):
        run(tmp_path, static(), [single(None)])


# --------------------------------------------------------------------------
# Detecteur, device et liberation du modele (ADR-fb9b)
# --------------------------------------------------------------------------


def test_detector_gets_device_from_clipper_gpu_and_is_closed_before_llm(tmp_path, video_dir, monkeypatch):
    import clipper.reframe as reframe_mod

    monkeypatch.setattr(reframe_mod, "get_device", lambda: Device(type="cuda", compute_type="float16"))
    seen = {}

    def answer(request):
        seen["closed"] = [d.closed for d in factory_ref[0].detectors]
        return single(0)

    factory_ref = []
    video = FakeVideo()
    from clipper.reframe import reframe as do_reframe

    factory = FakeDetectorFactory(video, static((1100, 300, 1300, 500)))
    factory_ref.append(factory)
    with llm.use_backend(FakeBackend([answer])):
        do_reframe(VIDEO_ID, "01", 10.0, 14.0, tmp_path / "workspace",
                   config=make_config(tmp_path), detector_factory=factory, frame_source=video)

    [(settings, device)] = factory.built
    assert device.type == "cuda"
    assert settings["detector"] == "mediapipe"
    assert seen["closed"] == [True]


def test_detector_is_closed_even_when_detection_fails(tmp_path, video_dir):
    def boom(t):
        raise RuntimeError("detection en echec")

    from clipper.reframe import reframe as do_reframe

    video = FakeVideo()
    factory = FakeDetectorFactory(video, boom)
    with llm.use_backend(FakeBackend([])), pytest.raises(RuntimeError, match="detection en echec"):
        do_reframe(VIDEO_ID, "01", 10.0, 14.0, tmp_path / "workspace",
                   config=make_config(tmp_path), detector_factory=factory, frame_source=video)
    assert [d.closed for d in factory.detectors] == [True]


def test_unknown_detector_in_config_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError, reframe as do_reframe

    with llm.use_backend(FakeBackend([])), pytest.raises(ReframeError, match="yolo"):
        do_reframe(VIDEO_ID, "01", 10.0, 14.0, tmp_path / "workspace",
                   config=make_config(tmp_path, detector="yolo"), frame_source=FakeVideo())


def test_reframe_is_configurable_through_config_toml(tmp_path):
    from clipper.config import load_config

    (tmp_path / "config.toml").write_text('[reframe]\nsample_fps = 3.0\nfallback = "blur"\n', encoding="utf-8")
    section = load_config(tmp_path / "config.toml").section("reframe")
    assert section["sample_fps"] == 3.0
    assert section["fallback"] == "blur"
    assert section["detector"] == "mediapipe"


# --------------------------------------------------------------------------
# Reponse LLM : validee avant usage (ADR-b1c1)
# --------------------------------------------------------------------------


def test_facecam_without_camera_is_a_schema_error_and_nothing_is_written(tmp_path, video_dir):
    bad = {"layout": "facecam_gameplay", "camera": None, "face": None, "reason": "r"}
    with pytest.raises(llm.SchemaError):
        run(tmp_path, static((1100, 300, 1300, 500)), [bad])
    assert not (video_dir / "reframe" / "01.json").exists()


def test_single_with_unknown_face_is_a_schema_error(tmp_path, video_dir):
    with pytest.raises(llm.SchemaError):
        run(tmp_path, static((1100, 300, 1300, 500)), [single(7)])


def test_unknown_layout_is_a_schema_error(tmp_path, video_dir):
    with pytest.raises(llm.SchemaError):
        run(tmp_path, static((1100, 300, 1300, 500)), [{"layout": "split", "camera": None, "face": None, "reason": "r"}])


def test_camera_outside_the_image_is_a_schema_error(tmp_path, video_dir):
    with pytest.raises(llm.SchemaError):
        run(tmp_path, static((1500, 800, 1600, 900)), [facecam(0.8, 0.7, 0.5, 0.3)])


def test_llm_failure_propagates_and_nothing_is_written(tmp_path, video_dir):
    with pytest.raises(llm.TransientLLMError):
        run(tmp_path, static((1100, 300, 1300, 500)), [llm.TransientLLMError("quota")])
    assert not (video_dir / "reframe" / "01.json").exists()


# --------------------------------------------------------------------------
# single : suivi du visage, cadre 9:16 plein ecran
# --------------------------------------------------------------------------


def test_single_static_face_gives_a_centered_full_height_crop(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)])
    [plan] = load(out)["plans"]
    assert plan["layout"] == "single"
    assert plan["reason"] is None
    [r] = rects(plan)  # visage immobile : un seul rectangle pour tout le plan
    assert (r["x"], r["y"], r["w"], r["h"]) == (896, 0, CROP_W, 1080)
    assert (r["start"], r["end"]) == (10.0, 14.0)
    [panel] = plan["panels"]
    assert panel["dest"] == {"x": 0, "y": 0, "w": 1080, "h": 1920}


def test_single_face_at_the_edge_clamps_the_crop_inside_the_frame(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static((1750, 300, 1910, 480)), [single(0)])
    [r] = rects(load(out)["plans"][0])
    assert (r["x"], r["w"]) == (W - CROP_W, CROP_W)


def test_single_jittering_face_does_not_move_the_crop(tmp_path, video_dir):
    def jitter(t):
        dx = 15 if int(t * 5) % 2 else -15
        return [(860 + dx, 300, 1060 + dx, 500)]

    out, _, _ = run(tmp_path, jitter, [single(0)])
    plan = load(out)["plans"][0]
    assert len({r["x"] for r in rects(plan)}) == 1
    for r in rects(plan):
        for t in times_in(r):
            assert contains(r, jitter(t)[0])


def test_single_moving_face_is_followed_smoothly_and_never_cut(tmp_path, video_dir):
    def moving(t):
        x = 300 + (t - 10.0) * 275  # 300 -> 1400 en 4 s
        return [(x, 300, x + 200, 500)]

    out, _, _ = run(tmp_path, moving, [single(0)])
    plan = load(out)["plans"][0]
    rs = rects(plan)
    assert len(rs) > 3
    assert_covers(rs, 10.0, 14.0)
    xs = [r["x"] for r in rs]
    assert xs == sorted(xs)  # suivi lisse : pas d'aller-retour
    assert max(b - a for a, b in zip(xs, xs[1:])) <= 120
    for r in rs:
        assert (r["w"], r["h"]) == (CROP_W, 1080)
        for t in times_in(r):
            assert contains(r, moving(t)[0]), (r, t)


def test_single_fast_face_stays_whole_between_analysed_frames(tmp_path, video_dir):
    # 700 px/s : 70 px parcourus entre une image analysee et le bord de son
    # intervalle, bien plus que la marge ; le cadre doit l'anticiper.
    def fast(t):
        x = 100 + (t - 10.0) * 700
        return [(x, 300, x + 150, 450)]

    out, _, _ = run(tmp_path, fast, [single(0)], start=10.0, end=12.0, face_margin=0.0)
    for r in rects(load(out)["plans"][0]):
        for t in times_in(r, n=9):
            assert contains(r, fast(t)[0]), (r, t)


def test_face_margin_keeps_room_around_a_face_pushed_to_the_crop_edge(tmp_path, video_dir):
    # Le visage 1 (a droite) force le bord droit du cadre pres du visage 0.
    a, b = (700, 300, 900, 500), (1000, 300, 1200, 500)
    out, _, _ = run(tmp_path, static(a, b), [single(0)], face_margin=0.15)
    [r] = rects(load(out)["plans"][0])
    x0, x1 = r["x"], r["x"] + r["w"]
    # a entier dedans, marge comprise ; b entier dehors ou entier dedans,
    # marge comprise dans les deux cas.
    assert x0 <= 700 - 30 and x1 >= 900 + 30
    assert x1 <= 1000 - 30 or (x0 <= 1000 - 30 and x1 >= 1200 + 30)


def test_single_face_missed_by_the_detector_on_some_frames_stays_in_frame(tmp_path, video_dir):
    # Visage large (48 px de jeu dans le cadre) qui se deplace, rate
    # pendant 0.8 s : le cadre doit suivre sa position interpolee.
    def moving(t):
        x = 100 + (t - 10.0) * 300
        return [(x, 300, x + 500, 800)]

    def flaky(t):
        return [] if 11.2 < t < 12.0 else moving(t)

    out, _, _ = run(tmp_path, flaky, [single(0)], face_margin=0.0)
    for r in rects(load(out)["plans"][0]):
        for t in times_in(r):
            assert contains(r, moving(t)[0]), (r, t)


def test_single_other_face_is_never_cut(tmp_path, video_dir):
    # Suivre le visage 0 en le centrant couperait le visage 1 (a sa droite).
    boxes = static((700, 300, 900, 500), (1000, 300, 1250, 550))
    out, _, _ = run(tmp_path, boxes, [single(0)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "single"
    for r in rects(plan):
        assert contains(r, (700, 300, 900, 500))
        for box in boxes(0):
            assert not cuts(r, box)


def test_single_without_face_centers_the_crop(tmp_path, video_dir):
    out, _, fake = run(tmp_path, static(), [single(None)])
    plan = load(out)["plans"][0]
    [r] = rects(plan)
    assert (r["x"], r["w"]) == (656, CROP_W)
    assert plan["faces"] == []
    assert len(fake.calls) == 1


def test_single_with_hallucinated_face_on_an_empty_plan_is_not_a_schema_error(tmp_path, video_dir):
    # TASK-0d30 : sur un plan sans visage detecte (ids vide), le modele
    # peut repondre face=0 alors qu'aucun id n'existe (smoke reel du
    # 2026-09-30, reason="Aucun visage detecte..." mais face=0 quand meme).
    # single() ignore de toute facon ``face`` quand plan.tracks est vide
    # (aucun id ne peut y correspondre) : ce n'est pas une reponse a
    # rejeter, la question n'a simplement pas de bonne reponse a verifier.
    out, _, fake = run(tmp_path, static(), [single(0)])
    plan = load(out)["plans"][0]
    [r] = rects(plan)
    assert (r["x"], r["w"]) == (656, CROP_W)
    assert plan["faces"] == []
    assert len(fake.calls) == 1


def test_brief_false_detection_is_ignored(tmp_path, video_dir):
    face = (1100, 300, 1300, 500)

    def boxes(t):
        # Une seule image avec un faux visage a cheval sur le bord gauche du cadre.
        return [face, (850, 300, 950, 400)] if 12.0 <= t < 12.2 else [face]

    out, _, _ = run(tmp_path, boxes, [single(0)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "single"
    assert len(plan["faces"]) == 1


# --------------------------------------------------------------------------
# facecam_gameplay : camera en haut, jeu en bas
# --------------------------------------------------------------------------


def test_facecam_gameplay_stacks_camera_over_gameplay(tmp_path, video_dir):
    face = (1560, 800, 1680, 940)
    out, _, _ = run(tmp_path, static(face), [facecam(0.75, 0.7, 0.25, 0.3)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "facecam_gameplay"
    panels = {p["name"]: p for p in plan["panels"]}
    assert set(panels) == {"camera", "gameplay"}
    assert panels["camera"]["dest"] == {"x": 0, "y": 0, "w": 1080, "h": 768}
    assert panels["gameplay"]["dest"] == {"x": 0, "y": 768, "w": 1080, "h": 1152}

    zone = (1440, 756, 1920, 1080)
    for r in panels["camera"]["rects"]:
        assert contains(r, zone)
        assert contains(r, face)
        assert r["x"] >= 0 and r["y"] >= 0 and r["x"] + r["w"] <= W and r["y"] + r["h"] <= H
        assert r["w"] / r["h"] == pytest.approx(1080 / 768, rel=0.01)
    for r in panels["gameplay"]["rects"]:
        assert (r["w"], r["h"]) == (1012, 1080)
        assert not cuts(r, face)
    assert_covers(panels["camera"]["rects"], 10.0, 14.0)
    assert_covers(panels["gameplay"]["rects"], 10.0, 14.0)


def test_facecam_face_straddling_the_camera_zone_is_kept_whole(tmp_path, video_dir):
    face = (1400, 780, 1560, 960)  # centre dans la zone camera, bord gauche dehors
    out, _, _ = run(tmp_path, static(face), [facecam(0.75, 0.7, 0.25, 0.3)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "facecam_gameplay"
    [r] = rects(plan, "camera")
    assert contains(r, face)
    assert r["w"] / r["h"] == pytest.approx(1080 / 768, rel=0.01)


def test_facecam_panel_that_would_cut_another_face_falls_back(tmp_path, video_dir):
    # Un visage du jeu, hors zone camera, a cheval sur le bord du panneau camera.
    face = (1300, 800, 1500, 1000)
    out, _, _ = run(tmp_path, static(face), [facecam(0.75, 0.7, 0.25, 0.3)])
    plan = load(out)["plans"][0]
    assert plan["layout"] != "facecam_gameplay"
    assert plan["reason"]
    for panel in plan["panels"]:
        for r in panel["rects"]:
            assert not cuts(r, face)


def test_facecam_ratio_is_configurable(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static((1560, 800, 1680, 940)), [facecam(0.75, 0.7, 0.25, 0.3)],
                    facecam_height_ratio=0.5)
    panels = {p["name"]: p for p in load(out)["plans"][0]["panels"]}
    assert panels["camera"]["dest"]["h"] == 960
    assert panels["gameplay"]["dest"] == {"x": 0, "y": 960, "w": 1080, "h": 960}


# --------------------------------------------------------------------------
# Replis : visages impossibles a garder entiers dans un seul cadre 9:16
# --------------------------------------------------------------------------


def test_face_too_wide_for_the_frame_falls_back_to_blur(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static((500, 100, 1300, 1000)), [single(0)])
    data = load(out)
    plan = data["plans"][0]
    assert plan["layout"] == "fallback_blur"
    assert plan["reason"]
    assert data["layout"] == "fallback_blur"
    panels = {p["name"]: p for p in plan["panels"]}
    assert panels["background"]["effect"] == "blur"
    assert panels["background"]["dest"] == {"x": 0, "y": 0, "w": 1080, "h": 1920}
    [bg] = panels["background"]["rects"]
    [main] = panels["main"]["rects"]
    for r in (bg, main):
        assert (r["x"], r["y"], r["w"], r["h"]) == (0, 0, W, H)
    assert panels["main"]["dest"] == {"x": 0, "y": 656, "w": 1080, "h": 608}


def test_two_faces_that_cannot_share_a_frame_fall_back_to_split(tmp_path, video_dir):
    # Garder le visage 0 entier impose au bord droit du cadre de tomber
    # dans le visage 1 : impossible sans couper l'un des deux.
    a, b = (100, 300, 300, 500), (350, 250, 800, 700)
    out, _, _ = run(tmp_path, static(a, b), [single(0)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "split"
    assert plan["reason"]
    panels = {p["name"]: p for p in plan["panels"]}
    assert panels["top"]["dest"] == {"x": 0, "y": 0, "w": 1080, "h": 960}
    assert panels["bottom"]["dest"] == {"x": 0, "y": 960, "w": 1080, "h": 960}
    for r in panels["top"]["rects"]:
        assert contains(r, a) and not cuts(r, b)
    for r in panels["bottom"]["rects"]:
        assert contains(r, b) and not cuts(r, a)
        assert r["w"] / r["h"] == pytest.approx(1080 / 960, rel=0.01)


# --------------------------------------------------------------------------
# Doublons, pistes fragmentees et visages retenus. Donnees synthetiques
# reprenant un essai reel (source 1920x1080, 5 images/s) : le detecteur
# plein cadre + tuiles rend deux boites quasi identiques par visage, une
# fausse detection (torse) clignote sous le visage avec des trous de plus
# d'une seconde, et une piste fantome dure 0,4 s (3 images).
# --------------------------------------------------------------------------


def test_duplicate_detections_within_a_frame_are_one_face(tmp_path, video_dir):
    face, dup = (1100, 300, 1300, 500), (1090, 310, 1290, 510)  # IoU ~0.82
    out, _, _ = run(tmp_path, static(face, dup), [single(0)])
    plan = load(out)["plans"][0]
    assert len(plan["faces"]) == 1
    assert plan["layout"] == "single"


def test_duplicate_iou_threshold_is_configurable(tmp_path, video_dir):
    face, dup = (1100, 300, 1300, 500), (1090, 310, 1290, 510)
    out, _, _ = run(tmp_path, static(face, dup), [single(0)], duplicate_iou=0.9)
    assert len(load(out)["plans"][0]["faces"]) == 2


def test_track_fragments_following_each_other_in_space_are_one_face(tmp_path, video_dir):
    # Meme visage, perdu 1,8 s (plus que le trou tolere par le suivi).
    box = (1100, 300, 1300, 500)
    out, _, _ = run(tmp_path, lambda t: [] if 11.0 < t < 12.6 else [box], [single(0)])
    [face] = load(out)["plans"][0]["faces"]
    assert face["first"] == pytest.approx(10.1)
    assert face["last"] == pytest.approx(13.9)


def test_flickering_false_detection_under_the_face_is_not_protected(tmp_path, video_dir):
    # Essai reel, plan 0 : visage detecte sur toutes les images (en double),
    # torse detecte sur 8 images sur 20 en deux salves separees de 1,8 s ;
    # le torse, plus large que le cadre une fois ajoute au visage, rendait
    # le suivi impossible (split avec deux fois le plan entier).
    face, dup = (520, 160, 820, 460), (505, 175, 800, 470)
    torso = (330, 450, 850, 990)

    def boxes(t):
        flicker = 10.4 < t < 11.0 or 12.6 < t < 13.6
        return [face, dup] + ([torso] if flicker else [])

    # Numerotes de gauche a droite : #0 = torse, #1 = visage.
    out, _, _ = run(tmp_path, boxes, [single(1)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "single", plan["reason"]
    assert len(plan["faces"]) == 2  # visage (doublon fusionne) + torse (salves fusionnees)
    assert {f["id"]: f["retained"] for f in plan["faces"]} == {0: False, 1: True}
    assert plan["faces"][1]["box"][1] < 300  # le visage, pas le torse
    for r in rects(plan):
        assert (r["w"], r["h"]) == (CROP_W, 1080)
        for t in times_in(r):
            assert contains(r, face)


def test_ghost_track_of_0_4_s_is_not_protected(tmp_path, video_dir):
    # Essai reel, plan 4 : une piste de 3 images (0,4 s) passe le filtre
    # min_track_seconds et, inevitable, bloquait le suivi du visage.
    face, ghost = (1100, 300, 1300, 500), (750, 600, 1100, 950)
    out, _, _ = run(tmp_path, lambda t: [face, ghost] if 12.0 < t < 12.6 else [face], [single(1)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "single", plan["reason"]
    faces = {f["id"]: f for f in plan["faces"]}
    assert faces[1]["retained"] and not faces[0]["retained"]
    assert faces[0]["last"] - faces[0]["first"] == pytest.approx(0.4)
    for r in rects(plan):
        assert contains(r, face)


def test_small_face_is_not_protected(tmp_path, video_dir):
    # Un visage de 30 px (2,8 % de la hauteur) au bord du cadre centre : il
    # ne deplace plus le cadre.
    face, tiny = (1100, 300, 1300, 500), (1500, 300, 1530, 330)
    out, _, _ = run(tmp_path, static(face, tiny), [single(0)])
    plan = load(out)["plans"][0]
    [r] = rects(plan)
    assert (r["x"], r["w"]) == (896, CROP_W)
    assert [f["retained"] for f in plan["faces"]] == [True, False]


def test_retention_thresholds_are_configurable(tmp_path, video_dir):
    face, tiny = (1100, 300, 1300, 500), (1500, 300, 1530, 330)
    out, _, _ = run(tmp_path, static(face, tiny), [single(0)], min_face_height=0.02)
    assert [f["retained"] for f in load(out)["plans"][0]["faces"]] == [True, True]

    box = (1100, 300, 1300, 500)
    out, _, _ = run(tmp_path, lambda t: [box] if t < 11.0 else [], [single(0)], force=True,
                    min_face_presence=0.1)
    assert [f["retained"] for f in load(out)["plans"][0]["faces"]] == [True]


def test_split_is_never_made_of_one_face_and_its_duplicate(tmp_path, video_dir):
    # Visage trop large pour le cadre, detecte en double : un seul visage
    # retenu, donc pas d'ecran partage (il y mettrait deux fois la meme
    # personne) mais le fond flou.
    face, dup = (500, 100, 1300, 1000), (510, 110, 1290, 990)
    out, _, _ = run(tmp_path, static(face, dup), [single(0)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "fallback_blur"
    assert "split impossible" in plan["reason"]


def test_split_panels_each_frame_their_own_face(tmp_path, video_dir):
    # Deux visages distincts retenus que nul cadre 9:16 pleine hauteur ne
    # garde entiers : chaque panneau est cadre sur son visage, a sa taille
    # (split_face_height), et n'y met pas l'autre quand c'est possible.
    a, b = (300, 20, 450, 170), (350, 600, 1000, 1060)
    out, _, _ = run(tmp_path, static(a, b), [single(0)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "split"
    panels = {p["name"]: p for p in plan["panels"]}
    [top] = panels["top"]["rects"]
    [bottom] = panels["bottom"]["rects"]
    assert contains(top, a)
    assert not cuts(top, b) and not contains(top, b)  # b entierement dehors
    assert top["h"] < H  # zoome sur son visage
    assert (a[3] - a[1]) / top["h"] == pytest.approx(0.35, abs=0.01)
    assert contains(bottom, b) and not cuts(bottom, a)
    for r in (top, bottom):
        assert r["w"] / r["h"] == pytest.approx(1080 / 960, rel=0.01)
        assert r["x"] >= 0 and r["y"] >= 0 and r["x"] + r["w"] <= W and r["y"] + r["h"] <= H


def test_fallback_blur_is_used_when_configured(tmp_path, video_dir):
    a, b = (100, 300, 300, 500), (350, 250, 800, 700)
    out, _, _ = run(tmp_path, static(a, b), [single(0)], fallback="blur")
    assert load(out)["plans"][0]["layout"] == "fallback_blur"


def test_facecam_whose_face_cannot_fit_the_camera_panel_falls_back(tmp_path, video_dir):
    # Le visage deborde largement de la zone camera annoncee : le panneau
    # camera (1080x768) ne peut pas le contenir sans couper un autre visage.
    out, _, _ = run(tmp_path, static((200, 50, 1900, 1050)), [facecam(0.75, 0.7, 0.25, 0.3)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "fallback_blur"
    assert plan["reason"]


def test_clip_layout_is_the_one_covering_most_of_the_clip(tmp_path, video_dir):
    write_scenes(video_dir, [(0.0, 11.0), (11.0, 30.0)])
    out, _, _ = run(tmp_path, static((1100, 300, 1300, 500)), [facecam(0.75, 0.7, 0.25, 0.3), single(0)])
    data = load(out)
    assert [p["layout"] for p in data["plans"]] == ["facecam_gameplay", "single"]
    assert data["layout"] == "single"


# --------------------------------------------------------------------------
# Gros plans et causes de repli de l'essai reel sZi-qJ-5ptA (1920x1080,
# 5 images/s). Detections mediapipe relevees image par image et rejouees :
# les visages suivis tenaient dans 608 px mais pas avec la marge sur la zone
# balayee (camera portee).
# --------------------------------------------------------------------------

# Clip 06, plan 2 : un seul visage, retenu, 430 a 490 px, camera qui bouge
# (180 px en 0,6 s au debut) ; zone balayee + marge 0.15 jusqu'a 722 px.
REAL_06_2 = [
    [(353, 405, 779, 831)], [(360, 316, 819, 774)], [(402, 295, 884, 777)],
    [(541, 313, 988, 760)], [(556, 304, 1009, 757)], [(535, 301, 993, 759)],
    [(525, 301, 982, 758)], [(510, 306, 967, 763)], [(514, 327, 943, 756)],
    [(475, 288, 912, 725), (720, 426, 1000, 706)], [(451, 305, 917, 771)],
    [(468, 295, 917, 744), (720, 410, 981, 671)], [(493, 314, 949, 770)],
    [(502, 315, 948, 761)], [(512, 337, 953, 778)], [(485, 310, 936, 761)],
    [(481, 405, 916, 840)], [(465, 300, 931, 766)], [(467, 405, 901, 839)],
    [(436, 308, 910, 781)], [(411, 291, 904, 784)], [(408, 302, 885, 779)],
    [(410, 309, 899, 798)],
]

# Clip 00, plan 1, de 696,73 a 699,35 s : visage retenu de 480 a 550 px qui
# traverse l'image (520 px en 2 s), zone balayee + marge jusqu'a 787 px ;
# fausses detections a gauche sur les 3 dernieres images.
REAL_00_1 = [
    [(548, 86, 1032, 570)], [(543, 123, 1043, 623)], [(606, 132, 1095, 621)],
    [(624, 133, 1147, 656)], [(722, 138, 1240, 656)], [(771, 127, 1317, 673)],
    [(800, 83, 1332, 615)], [(837, 133, 1338, 634)], [(859, 141, 1362, 644)],
    [(889, 120, 1397, 628)], [(985, 152, 1413, 580)],
    [(1050, 120, 1493, 563), (176, 288, 469, 581)],
    [(1074, 175, 1470, 571), (61, 405, 641, 985)],
    [(1081, 172, 1441, 532), (320, 409, 635, 724)],
]


def replay(frames, start=10.0):
    """Detections image par image, une image analysee toutes les 0,2 s a
    partir de ``start`` (sample_fps = 5) ; renvoie aussi la fin du plan."""

    def boxes(t):
        return list(frames[round((t - start - 0.1) / 0.2)])

    return boxes, start + 0.2 * len(frames)


def analysed_times(start, end, fps=5.0):
    count = int((end - start) * fps + 1e-9)
    return [start + (k + 0.5) * (end - start) / count for k in range(count)]


def rect_at(rs, t):
    [r] = [r for r in rs if r["start"] <= t < r["end"]]
    return r


def assert_single_keeps_face_whole(plan, face_at, start, end):
    """single, sans repli, et le visage suivi entier dans le cadre a chaque
    image analysee."""
    assert plan["layout"] == "single", plan["reason"]
    assert plan["reason"] is None
    rs = rects(plan)
    assert_covers(rs, start, end)
    for t in analysed_times(start, end):
        box = face_at(t)
        if box is not None:
            r = rect_at(rs, t)
            assert (r["w"], r["h"]) == (CROP_W, 1080)
            assert contains(r, box), (t, r, box)


# (1) Visage suivi retenu : marge reduite puis boite instantanee, jamais rogne.


@pytest.mark.parametrize("frames", [REAL_06_2, REAL_00_1], ids=["06-2", "00-1"])
def test_real_close_up_with_moving_camera_is_framed_single(tmp_path, video_dir, frames):
    boxes, end = replay(frames)
    out, _, _ = run(tmp_path, boxes, [single(None)], start=10.0, end=end)
    [face] = [f for f in load(out)["plans"][0]["faces"] if f["retained"]]
    out, _, _ = run(tmp_path, boxes, [single(face["id"])], start=10.0, end=end, force=True)
    # Le visage suivi : la boite la plus haute de chaque image (les fausses
    # detections sont dessous).
    topmost = lambda t: min(boxes(t), key=lambda b: b[1])  # noqa: E731
    assert_single_keeps_face_whole(load(out)["plans"][0], topmost, 10.0, end)


def test_close_up_of_486_px_with_camera_moving_is_framed_single(tmp_path, video_dir):
    # Clip 10, plan 3 : visage de 486 px ; la camera oscille de +-60 px, la
    # zone balayee + marge 0.15 depasse le cadre de 608 px.
    def face(t):
        x = 777 + 60 * math.sin(math.pi * (t - 10.0))
        return (x, 112, x + 486, 601)

    out, _, _ = run(tmp_path, lambda t: [face(t)], [single(0)])
    assert_single_keeps_face_whole(load(out)["plans"][0], face, 10.0, 14.0)


def test_close_up_keeps_the_largest_margin_that_fits(tmp_path, video_dir):
    # Visage immobile de 486 px : 632 px avec la marge 0.15, il tient avec
    # une marge reduite, gardee aussi grande que possible (0.1).
    face = (777, 112, 1263, 601)
    out, _, _ = run(tmp_path, static(face), [single(0)])
    plan = load(out)["plans"][0]
    assert_single_keeps_face_whole(plan, lambda t: face, 10.0, 14.0)
    [r] = rects(plan)
    assert r["x"] <= 777 - 0.1 * 486 and r["x"] + r["w"] >= 1263 + 0.1 * 486


def test_face_wider_than_the_frame_is_never_cropped(tmp_path, video_dir):
    # 625 px pour un cadre de 608 : aucun palier ne le garde entier, repli
    # (SPEC-6127 : aucun visage coupe) ; la raison nomme le dernier palier.
    out, _, _ = run(tmp_path, static((455, 300, 1080, 925)), [single(0)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "fallback_blur"
    assert "dernier palier : boite instantanee" in plan["reason"]


def test_other_retained_face_is_never_cut_by_a_degraded_frame(tmp_path, video_dir):
    # Gros plan de 600 px (boite instantanee seulement) et un visage retenu
    # juste a droite : le cadre garde le gros plan et laisse l'autre entier
    # dehors, marge 0.15 comprise.
    a, b = (500, 240, 1100, 840), (1130, 300, 1330, 500)
    out, _, _ = run(tmp_path, static(a, b), [single(0)])
    plan = load(out)["plans"][0]
    assert plan["layout"] == "single", plan["reason"]
    assert [f["retained"] for f in plan["faces"]] == [True, True]
    for r in rects(plan):
        assert contains(r, a)
        assert not cuts(r, (1130 - 30, 300 - 30, 1330 + 30, 500 + 30))


def test_two_retained_faces_inevitably_cut_fall_back_to_blur(tmp_path, video_dir):
    # Gros plan de 600 px chevauche par un autre visage retenu : aucun
    # palier ne cadre l'un sans couper l'autre, ni l'ecran partage.
    a, b = (300, 200, 900, 800), (850, 250, 1600, 850)
    out, _, _ = run(tmp_path, static(a, b), [single(0)])
    plan = load(out)["plans"][0]
    assert [f["retained"] for f in plan["faces"]] == [True, True]
    assert plan["layout"] == "fallback_blur"
    assert "dernier palier : boite instantanee" in plan["reason"]
    assert "split impossible" in plan["reason"]


# (2) Le LLM ne suit qu'un visage retenu.


def test_prompt_says_which_faces_are_retained(tmp_path, video_dir):
    face, tiny = (1100, 300, 1300, 500), (1500, 300, 1530, 330)
    _, _, fake = run(tmp_path, static(face, tiny), [single(0)])
    prompt = fake.calls[0].prompt
    [line0] = [line for line in prompt.splitlines() if line.startswith("- #0 ")]
    [line1] = [line for line in prompt.splitlines() if line.startswith("- #1 ")]
    assert "non retenu" not in line0 and "retenu" in line0
    assert "non retenu" in line1


def test_llm_choosing_a_face_not_retained_is_a_schema_error(tmp_path, video_dir):
    # #1 : visage de 30 px, non retenu ; le suivre est une reponse invalide.
    face, tiny = (1100, 300, 1300, 500), (1500, 300, 1530, 330)
    with pytest.raises(llm.SchemaError, match="#1"):
        run(tmp_path, static(face, tiny), [single(1)])
    assert not (video_dir / "reframe" / "01.json").exists()


# (3) Faux positifs designes par le LLM (main, objet) : ni retenus ni proteges.


def test_ignored_detection_no_longer_constrains_the_frame(tmp_path, video_dir):
    # Une main retenue (#0) a gauche du visage (#1), a cheval sur le cadre
    # centre sur lui : sans ignore, le cadre se decale pour la garder
    # entiere ; avec ignore, il est centre sur le visage.
    hand, face = (700, 600, 900, 800), (1000, 300, 1200, 500)
    out, _, _ = run(tmp_path, static(hand, face), [single(1)])
    [r] = rects(load(out)["plans"][0])
    assert r["x"] != 796

    out, _, _ = run(tmp_path, static(hand, face), [single(1, ignore=[0])], force=True)
    plan = load(out)["plans"][0]
    assert plan["layout"] == "single"
    assert {f["id"]: f["retained"] for f in plan["faces"]} == {0: False, 1: True}
    [r] = rects(plan)
    assert (r["x"], r["w"]) == (796, CROP_W)


def test_ignore_must_name_detected_faces_and_never_the_followed_one(tmp_path, video_dir):
    face = (1100, 300, 1300, 500)
    with pytest.raises(llm.SchemaError):
        run(tmp_path, static(face), [single(0, ignore=[5])])
    with pytest.raises(llm.SchemaError):
        run(tmp_path, static(face), [single(0, ignore=[0])])


# (4) Presence mesuree sur la duree de la piste.


def test_face_entering_mid_plan_is_retained(tmp_path, video_dir):
    # Visage vu de 12 a 14 s sur un plan de 10 a 14 s : moitie du plan, mais
    # toute la duree de sa piste.
    late = (500, 300, 700, 500)
    out, _, _ = run(tmp_path, lambda t: [late] if t > 12.0 else [], [single(0)])
    [face] = load(out)["plans"][0]["faces"]
    assert face["retained"]


# Splits trop courts et plans parasites.


def test_split_shorter_than_split_min_seconds_falls_back_to_blur(tmp_path, video_dir):
    a, b = (100, 300, 300, 500), (350, 250, 800, 700)
    out, _, _ = run(tmp_path, static(a, b), [single(0)], start=10.0, end=11.0)
    plan = load(out)["plans"][0]
    assert [f["retained"] for f in plan["faces"]] == [True, True]
    assert plan["layout"] == "fallback_blur"
    assert "split_min_seconds" in plan["reason"]

    # Configurable : 1 s suffit a un split si split_min_seconds le permet.
    out, _, _ = run(tmp_path, static(a, b), [single(0)], start=10.0, end=11.0, force=True,
                    split_min_seconds=1.0)
    assert load(out)["plans"][0]["layout"] == "split"


def test_plan_shorter_than_min_plan_seconds_is_merged_into_its_neighbour(tmp_path, video_dir):
    write_scenes(video_dir, [(0.0, 12.0), (12.0, 12.08), (12.08, 30.0)])
    out, _, fake = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)] * 2, start=10.0, end=14.0)
    data = load(out)
    assert [(p["start"], p["end"]) for p in data["plans"]] == [(10.0, 12.08), (12.08, 14.0)]
    assert [p["index"] for p in data["plans"]] == [0, 1]
    assert len(fake.calls) == 2


def test_clip_edge_sliver_is_merged_into_the_next_plan(tmp_path, video_dir):
    write_scenes(video_dir, [(0.0, 10.1), (10.1, 30.0)])
    out, _, fake = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)], start=10.0, end=14.0)
    assert [(p["start"], p["end"]) for p in load(out)["plans"]] == [(10.0, 14.0)]
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------
# Detecteur mediapipe : modele .tflite, jamais telecharge par les tests
# --------------------------------------------------------------------------


def test_mediapipe_model_missing_without_url_is_an_error(tmp_path):
    from clipper.reframe import CONFIG_DEFAULTS, ReframeError, ensure_mediapipe_model

    settings = {**CONFIG_DEFAULTS, "model_path": str(tmp_path / "absent.tflite"), "model_url": ""}
    with pytest.raises(ReframeError, match="absent.tflite"):
        ensure_mediapipe_model(settings)


def test_mediapipe_model_missing_is_fetched_once_from_model_url(tmp_path):
    from clipper.reframe import CONFIG_DEFAULTS, ensure_mediapipe_model

    fetched = []

    def fetch(url, dest):
        fetched.append(url)
        Path(dest).write_bytes(b"tflite")

    target = tmp_path / "models" / "face.tflite"
    settings = {**CONFIG_DEFAULTS, "model_path": str(target), "model_url": "https://example.invalid/face.tflite"}
    assert ensure_mediapipe_model(settings, fetch=fetch) == target
    assert ensure_mediapipe_model(settings, fetch=fetch) == target
    assert fetched == ["https://example.invalid/face.tflite"]
    assert target.read_bytes() == b"tflite"


@pytest.mark.skipif(
    os.environ.get("CLIPPER_REAL_MODELS") != "1",
    reason="vrai modele mediapipe : CLIPPER_REAL_MODELS=1 (telecharge le .tflite si absent)",
)
def test_real_mediapipe_detector_runs_on_cpu():
    from clipper.reframe import CONFIG_DEFAULTS, mediapipe_detector

    detector = mediapipe_detector(dict(CONFIG_DEFAULTS), Device(type="cpu", compute_type="int8"))
    try:
        assert detector.detect(np.zeros((H, W, 3), dtype=np.uint8)) == []
    finally:
        detector.close()


# --------------------------------------------------------------------------
# Format letterbox (SPEC-6127) : zoom fixe, fond flou, aucun visage suivi,
# aucun appel LLM.
# --------------------------------------------------------------------------


def letterbox_plan(data):
    [plan] = data["plans"]
    return plan


def letterbox_panels(plan):
    return {p["name"]: p for p in plan["panels"]}


def test_letterbox_default_geometry_for_a_1920x1080_source(tmp_path, video_dir):
    out, factory, fake = run(tmp_path, static(), [], format="letterbox")

    data = load(out)
    assert data["video_id"] == VIDEO_ID
    assert data["clip_id"] == "01"
    assert (data["start"], data["end"]) == (10.0, 14.0)
    assert data["source"] == {"width": W, "height": H}
    assert data["output"] == {"width": 1080, "height": 1920}
    assert data["layout"] == "letterbox"
    assert data["format"] == "letterbox"
    assert data["text_zones"] == {
        "title": {"x0": 150, "y0": 160, "x1": 930, "y1": 424},
        "subtitles": {"x0": 150, "y0": 1246, "x1": 930, "y1": 1448},
        "part": {"x0": 150, "y0": 1464, "x1": 930, "y1": 1520},
    }

    plan = letterbox_plan(data)
    assert plan["index"] == 0
    assert (plan["start"], plan["end"]) == (10.0, 14.0)
    assert plan["image"] is None
    assert plan["llm"] is None
    assert plan["layout"] == "letterbox"
    assert plan["reason"] is None
    assert plan["faces"] == []
    assert [p["name"] for p in plan["panels"]] == ["background", "main"]

    panels = letterbox_panels(plan)
    assert panels["background"]["effect"] == "blur"
    assert panels["background"]["dest"] == {"x": 0, "y": 0, "w": 1080, "h": 1920}
    [bg] = panels["background"]["rects"]
    assert (bg["start"], bg["end"], bg["x"], bg["y"], bg["w"], bg["h"]) == (10.0, 14.0, 0, 0, W, H)

    assert panels["main"]["dest"] == {"x": 0, "y": 440, "w": 1080, "h": 790}
    [main] = panels["main"]["rects"]
    assert (main["start"], main["end"], main["x"], main["y"], main["w"], main["h"]) == (10.0, 14.0, 222, 0, 1476, 1080)

    # Aucune detection de visage, aucun detecteur construit, aucun appel LLM.
    assert factory.built == []
    assert fake.calls == []


def test_letterbox_window_for_a_1280x720_source(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static(), [], format="letterbox", video=FakeVideo(width=1280, height=720))

    plan = letterbox_plan(load(out))
    panels = letterbox_panels(plan)
    assert panels["main"]["dest"] == {"x": 0, "y": 440, "w": 1080, "h": 790}
    [main] = panels["main"]["rects"]
    assert (main["x"], main["y"], main["w"], main["h"]) == (148, 0, 984, 720)


def test_letterbox_zones_that_do_not_fit_are_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    with pytest.raises(ReframeError):
        run(tmp_path, static(), [], format="letterbox", video=FakeVideo(width=1440, height=1080))


def test_letterbox_zoom_below_one_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    with pytest.raises(ReframeError, match="zoom"):
        run(tmp_path, static(), [], format="letterbox", letterbox_zoom=0.9)


def test_unknown_reframe_format_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    with pytest.raises(ReframeError, match="vertical"):
        run(tmp_path, static(), [], format="vertical")


# --------------------------------------------------------------------------
# Éditeur d'agencement letterbox (TASK-3be3) : zones du titre d'écran et des
# sous-titres réglables par style, {} = zones déduites comme avant.
# --------------------------------------------------------------------------


def test_letterbox_text_dests_default_to_empty_so_the_default_render_is_unchanged():
    from clipper.reframe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["letterbox_title_dest"] == {}
    assert CONFIG_DEFAULTS["letterbox_subtitle_dest"] == {}
    assert CONFIG_DEFAULTS["letterbox_top"] == 440 and CONFIG_DEFAULTS["letterbox_zoom"] == 1.3


def test_letterbox_explicit_title_dest_becomes_the_title_zone(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static(), [], format="letterbox",
                    letterbox_title_dest={"x": 200, "y": 240, "w": 680, "h": 150})

    zones = load(out)["text_zones"]
    assert zones["title"] == {"x0": 200, "y0": 240, "x1": 880, "y1": 390}
    assert zones["subtitles"] == {"x0": 150, "y0": 1246, "x1": 930, "y1": 1448}   # inchangée


def test_letterbox_explicit_subtitle_dest_becomes_the_subtitles_zone(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static(), [], format="letterbox",
                    letterbox_subtitle_dest={"x": 180, "y": 1260, "w": 720, "h": 120})

    zones = load(out)["text_zones"]
    assert zones["subtitles"] == {"x0": 180, "y0": 1260, "x1": 900, "y1": 1380}
    assert zones["title"] == {"x0": 150, "y0": 160, "x1": 930, "y1": 424}          # inchangée


def test_letterbox_top_and_zoom_move_and_size_the_sharp_video(tmp_path, video_dir):
    out, _, _ = run(tmp_path, static(), [], format="letterbox", letterbox_top=400, letterbox_zoom=1.5)

    data = load(out)
    main = letterbox_panels(letterbox_plan(data))["main"]
    assert main["dest"] == {"x": 0, "y": 400, "w": 1080, "h": 910}   # 1080 * 1080 / 1280
    assert data["text_zones"]["title"] == {"x0": 150, "y0": 160, "x1": 930, "y1": 384}


@pytest.mark.parametrize("key, rect, fragment", [
    ("letterbox_title_dest", {"x": 150, "y": 160, "w": 1000, "h": 100}, "deborde du canevas"),
    ("letterbox_title_dest", {"x": 100, "y": 200, "w": 400, "h": 100}, "zone sure"),
    ("letterbox_subtitle_dest", {"x": 150, "y": 1300, "w": 780, "h": 300}, "zone sure"),
    ("letterbox_subtitle_dest", {"x": 150, "y": 1400, "w": 780, "h": 100}, "Partie"),
    ("letterbox_title_dest", {"x": 150, "y": 160, "w": 780}, "rectangle"),
])
def test_letterbox_text_dest_outside_the_frame_or_safe_zone_is_refused_at_load(key, rect, fragment):
    from clipper.reframe import ReframeError, _settings

    config = Config(mode="review", workspace_dir="workspace", output_dir="output", _sections={"reframe": {key: rect}})
    with pytest.raises(ReframeError, match=fragment) as err:
        _settings(config)
    assert key in str(err.value)


def test_letterbox_title_and_subtitle_dests_must_not_overlap():
    from clipper.reframe import ReframeError, _settings

    config = Config(mode="review", workspace_dir="workspace", output_dir="output", _sections={"reframe": {
        "letterbox_title_dest": {"x": 150, "y": 200, "w": 780, "h": 300},
        "letterbox_subtitle_dest": {"x": 150, "y": 400, "w": 780, "h": 200},
    }})
    with pytest.raises(ReframeError, match="se chevauchent"):
        _settings(config)


def test_letterbox_explicit_zone_over_the_video_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    with pytest.raises(ReframeError, match="chevauche le panneau video"):
        run(tmp_path, static(), [], format="letterbox",
            letterbox_title_dest={"x": 150, "y": 300, "w": 780, "h": 200})


def test_letterbox_video_leaving_the_frame_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    with pytest.raises(ReframeError, match="sort du cadre"):
        run(tmp_path, static(), [], format="letterbox", letterbox_top=1200,
            letterbox_title_dest={"x": 150, "y": 160, "w": 780, "h": 300},
            letterbox_subtitle_dest={"x": 150, "y": 600, "w": 780, "h": 200})


def test_letterbox_top_outside_the_canvas_is_refused_at_load():
    from clipper.reframe import ReframeError, _settings

    with pytest.raises(ReframeError, match="letterbox_top"):
        _settings(Config(mode="review", workspace_dir="workspace", output_dir="output", _sections={"reframe": {"letterbox_top": -10}}))


def test_letterbox_does_not_require_scenes_json(tmp_path, video_dir):
    (video_dir / "scenes.json").unlink()
    out, _, _ = run(tmp_path, static(), [], format="letterbox")
    assert load(out)["layout"] == "letterbox"


def test_existing_crop_plan_with_letterbox_config_is_an_error_without_force(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    out, _, _ = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)])  # format="crop" (par defaut du test)
    assert "format" not in load(out)  # ancien format crop : pas de champ "format"

    with pytest.raises(ReframeError, match="crop"):
        run(tmp_path, static(), [], format="letterbox")

    out2, factory, fake = run(tmp_path, static(), [], format="letterbox", force=True)
    assert out2 == out
    assert load(out2)["format"] == "letterbox"
    assert factory.built == [] and fake.calls == []


def test_existing_letterbox_plan_with_crop_config_is_an_error_without_force(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    out, _, _ = run(tmp_path, static(), [], format="letterbox")

    with pytest.raises(ReframeError, match="letterbox"):
        run(tmp_path, static((1100, 300, 1300, 500)), [single(0)], format="crop")

    out2, _, fake = run(tmp_path, static((1100, 300, 1300, 500)), [single(0)], format="crop", force=True)
    assert out2 == out
    assert len(fake.calls) == 1
    assert "format" not in load(out2)


# --------------------------------------------------------------------------
# Format stream (SPEC-8257, succede a SPEC-3a88/TASK-9e0c) : facecam fixe
# agrandie en haut, jeu en bas ; localisation une fois par video sur les
# images cles de scenes.json, choix par clip tout ou rien, aucun suivi.
# --------------------------------------------------------------------------

import cv2  # noqa: E402

# Facecam typique (SMYVmdpRMow) : ~526x296 en haut a gauche d'un 1920x1080,
# visage d'environ 130x150 dedans.
CAM_FACE = (190, 70, 320, 220)


class PixelFaceDetector:
    """Detecteur simule : un "visage" est le rectangle blanc dessine dans
    l'image cle synthetique (boite englobante des pixels clairs)."""

    def __init__(self):
        self.closed = False
        self.frames = 0

    def detect(self, frame):
        assert not self.closed, "detecteur utilise apres close()"
        self.frames += 1
        x, y, w, h = cv2.boundingRect((frame[:, :, 0] > 128).astype(np.uint8))
        return [(float(x), float(y), float(x + w), float(y + h), 0.9)] if w and h else []

    def close(self):
        self.closed = True


class PixelDetectorFactory:
    def __init__(self):
        self.built: list[tuple[dict, Device]] = []
        self.detectors: list[PixelFaceDetector] = []

    def __call__(self, settings, device):
        self.built.append((settings, device))
        detector = PixelFaceDetector()
        self.detectors.append(detector)
        return detector



def encode_png(image):
    """PNG sans perte, compression la plus rapide : memes pixels qu'a un
    niveau eleve, l'encodage au niveau 9 pesait lourd dans la duree des
    tests (TASK-4633)."""
    return cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1])[1].tobytes()


def coarse_noise(rng, low, high, height, width, cell=16):
    """Fond bruite par cellules de ``cell`` px, decalees au hasard a chaque
    image (aucune arete fixe d'une image a l'autre) : meme variation entre
    images qu'un bruit par pixel, mais un PNG ~10 fois plus leger (le bruit
    pixel par pixel pesait ~4,7 Mo par image 1920x1080, TASK-20f1)."""
    rows, cols = -(-height // cell) + 1, -(-width // cell) + 1
    small = rng.integers(low, high, size=(rows, cols, 3), dtype=np.uint8)
    big = np.repeat(np.repeat(small, cell, axis=0), cell, axis=1)
    dy, dx = (int(v) for v in rng.integers(0, cell, size=2))
    return big[dy:dy + height, dx:dx + width].copy()


def write_keyframes(video_dir, faces, *, scenes=((0.0, 100.0),), width=W, height=H):
    """scenes.json avec une image cle par entree de ``faces`` : (temps, boite
    du visage ou None). Images synthetiques noires, visage en blanc."""
    frames_dir = video_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    frames = []
    encoded: dict = {}  # une image encodee par boite distincte
    for k, (t, box) in enumerate(faces):
        if box not in encoded:
            image = np.zeros((height, width, 3), dtype=np.uint8)
            if box is not None:
                x0, y0, x1, y1 = box
                image[y0:y1, x0:x1] = 255
            encoded[box] = encode_png(image)
        name = f"scene0000_{k:03d}.png"
        (frames_dir / name).write_bytes(encoded[box])
        frames.append({"path": f"frames/{name}", "timecode": t, "scene": 0})
    (video_dir / "scenes.json").write_text(
        json.dumps({"scenes": [{"start": s, "end": e} for s, e in scenes], "frames": frames}),
        encoding="utf-8",
    )


def pattern(n, present, box=CAM_FACE, t0=0.5, step=1.0):
    """``n`` images cles a t0, t0+step..., visage present sur les ``present``
    premieres (legerement bouge, sous la tolerance), absent sur les autres."""
    out = []
    for k in range(n):
        dx = (k % 3) - 1  # +-1 px : meme position a la tolerance pres
        b = (box[0] + dx, box[1] - dx, box[2] + dx, box[3] - dx)
        out.append((t0 + k * step, b if k < present else None))
    return out


def stream_config(tmp_path, **reframe):
    reframe.setdefault("format", "letterbox")
    reframe.setdefault("layout", "stream_auto")
    return make_config(tmp_path, **reframe)


def run_stream(tmp_path, *, start=0.0, end=20.0, clip_id="01", factory=None, force=False, answers=None, **reframe):
    from clipper.reframe import reframe as do_reframe

    factory = factory or PixelDetectorFactory()
    fake = FakeBackend(answers or [{"webcam": 1, "reason": "r"}])
    with llm.use_backend(fake):
        out = do_reframe(
            VIDEO_ID, clip_id, start, end, tmp_path / "workspace",
            config=stream_config(tmp_path, **reframe), force=force,
            detector_factory=factory, frame_source=FakeVideo(),
        )
    return out, factory


def detect(tmp_path, factory=None, force=False, answers=None, **reframe):
    from clipper.reframe import detect_facecam

    factory = factory or PixelDetectorFactory()
    with llm.use_backend(FakeBackend(answers or [{"webcam": 1, "reason": "r"}])):
        path = detect_facecam(VIDEO_ID, tmp_path / "workspace", config=stream_config(tmp_path, **reframe),
                              force=force, detector_factory=factory)
    return path, factory


def period0(path):
    """Premiere (et souvent seule) periode de facecam.json."""
    return load(path)["periods"][0]


def rect_box(r):
    return (r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"])


def test_stable_facecam_on_90_percent_of_keyframes_gives_a_fixed_rectangle(tmp_path, video_dir):
    write_keyframes(video_dir, pattern(20, 18))
    path, factory = detect(tmp_path)

    assert path == video_dir / "facecam.json"
    data = period0(path)
    assert data["reason"] is None
    rect = data["facecam"]
    assert set(rect) == {"x", "y", "w", "h"}
    # le visage est entier dans le rectangle, qui fait moins d'un quart de l'image
    assert contains(rect, CAM_FACE)
    assert rect["w"] * rect["h"] < W * H / 4
    # rectangle au format du panneau camera (1080 x 40 % de 1920)
    assert rect["w"] / rect["h"] == pytest.approx(1080 / 768, rel=0.02)
    # detecteur construit une fois avec le device de clipper.gpu, puis ferme
    assert len(factory.built) == 1
    assert isinstance(factory.built[0][1], Device)
    assert factory.detectors[0].closed
    # 1 appel pour l'image echantillonnee (une par minute), puis 5 par image
    # candidate (20 images cles < facecam_candidate_frames) : l'image entiere
    # et les 4 coins agrandis (TASK-c7e682a88189, petites facecams).
    assert factory.detectors[0].frames == 1 + 20 * 5


def test_face_seen_on_a_single_board_image_is_not_a_candidate_and_the_absence_is_motivated(tmp_path, video_dir):
    # un visage vu sur 1 seule des 8 images de la planche (facecam_candidate_min_frames = 2)
    write_keyframes(video_dir, pattern(20, 1))
    path, factory = detect(tmp_path)

    data = period0(path)
    assert data["facecam"] is None
    assert data["candidates"] == []
    assert "aucun rectangle candidat" in data["reason"]
    assert factory.detectors[0].closed


def test_face_moving_beyond_the_tolerance_is_not_a_facecam(tmp_path, video_dir):
    faces = [(0.5 + k, (100 + 60 * k, 70, 230 + 60 * k, 220)) for k in range(20)]
    write_keyframes(video_dir, faces)
    data = period0(detect(tmp_path, facecam_cluster_ratio=0)[0])
    assert data["facecam"] is None
    assert data["reason"]


def test_facecam_tolerance_is_configurable(tmp_path, video_dir):
    # visage qui derive de 60 px par image cle : hors tolerance par defaut
    # (40 px, aucune paire ne se recolle), regroupe par une tolerance large.
    faces = [(0.5 + k, (100 + 60 * k, 70, 230 + 60 * k, 220)) for k in range(20)]
    write_keyframes(video_dir, faces)
    assert period0(detect(tmp_path, facecam_tolerance=40, facecam_cluster_ratio=0)[0])["facecam"] is None
    assert period0(detect(tmp_path, facecam_tolerance=2000, facecam_cluster_ratio=0, force=True)[0])["facecam"] is not None


def test_facecam_candidate_min_frames_is_configurable(tmp_path, video_dir):
    write_keyframes(video_dir, pattern(20, 1))  # visage sur 1 image de la planche
    assert period0(detect(tmp_path)[0])["facecam"] is None  # sous le defaut (2 images)
    assert period0(detect(tmp_path, force=True, facecam_candidate_min_frames=1)[0])["facecam"] is not None


def test_stable_face_whose_zone_exceeds_a_quarter_of_the_image_is_not_a_facecam(tmp_path, video_dir):
    # un presentateur plein cadre : visage stable, mais pas une incrustation
    write_keyframes(video_dir, pattern(20, 20, box=(760, 240, 1160, 740)))
    data = period0(detect(tmp_path)[0])
    assert data["facecam"] is None
    assert data["candidates"] == []
    assert any("quart" in r["reason"] for r in data["rejected"])  # ecarte, avec la raison


# --------------------------------------------------------------------------
# Bords reels de l'incrustation (TASK-6404) : le rectangle facecam est cale
# sur le cadre reel de l'incrustation (bords nets, constants sur la plupart
# des images cles) autour du visage stable, pas sur le seul visage.
# --------------------------------------------------------------------------

# Incrustation typique (SMYVmdpRMow) : panneau ~526x296 en haut a gauche
# d'un 1920x1080, visage d'environ 130x150 dedans avec une bonne marge.
PANEL = (100, 30, 626, 326)


def write_panel_keyframes(
    video_dir, panel, faces, *, panel_color=90, scenes=((0.0, 100.0),), width=W, height=H, seed=0
):
    """Images cles synthetiques : panneau ``panel`` (x0, y0, x1, y1) gris fixe
    (incrustation, bords nets et constants) contenant un visage blanc plus
    petit et legerement mobile (voir ``pattern``), sur un fond BRUITE qui
    varie a chaque image cle (mais reproductible, ``seed``) -- verifie que
    les bords de l'incrustation sont trouves parce qu'ils sont constants
    d'une image a l'autre, pas parce que le fond serait simplement uniforme.
    ``panel_color`` reste sous le seuil (128) du detecteur factice : seul le
    visage blanc est detecte comme visage."""
    rng = np.random.default_rng(seed)
    frames_dir = video_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    px0, py0, px1, py1 = panel
    frames = []
    for k, (t, box) in enumerate(faces):
        image = coarse_noise(rng, 0, 40, height, width)
        image[py0:py1, px0:px1] = panel_color
        if box is not None:
            x0, y0, x1, y1 = box
            image[y0:y1, x0:x1] = 255
        name = f"scene0000_{k:03d}.png"
        (frames_dir / name).write_bytes(encode_png(image))
        frames.append({"path": f"frames/{name}", "timecode": t, "scene": 0})
    (video_dir / "scenes.json").write_text(
        json.dumps({"scenes": [{"start": s, "end": e} for s, e in scenes], "frames": frames}),
        encoding="utf-8",
    )


def test_incrustation_edges_used_when_present_cover_the_panel(tmp_path, video_dir):
    write_panel_keyframes(video_dir, PANEL, pattern(20, 20))
    data = period0(detect(tmp_path)[0])

    assert data["reason"] is None
    assert data["edge_reason"] is None  # bords trouves, pas de repli
    rect = data["facecam"]
    px0, py0, px1, py1 = PANEL
    # couvre l'incrustation entiere, a quelques px pres
    assert contains(rect, PANEL, eps=3)
    # largeur (axe non etire par la mise au format du panneau camera) calee
    # sur les bords reels du panneau, pas sur une valeur bien plus grande
    assert abs(rect["x"] - px0) <= 3
    assert abs((rect["x"] + rect["w"]) - px1) <= 3
    assert rect["w"] / rect["h"] == pytest.approx(1080 / 768, rel=0.02)


def test_incrustation_edges_missing_falls_back_to_face_rect_with_logged_reason(tmp_path, video_dir, caplog):
    # meme fixture que les tests de facecam "simples" : aucune incrustation
    # dessinee autour du visage, donc aucun bord net a trouver.
    write_keyframes(video_dir, pattern(20, 18))
    with caplog.at_level("INFO", logger="clipper.reframe"):
        data = period0(detect(tmp_path)[0])

    assert data["reason"] is None  # le facecam existe quand meme (repli)
    assert data["edge_reason"]  # mais la raison du repli est journalisee
    assert data["edge_reason"] in caplog.text
    rect = data["facecam"]
    # repli identique a l'ancien calcul : rectangle centre sur le visage,
    # au format du panneau camera, visage entier dedans.
    assert contains(rect, CAM_FACE)
    assert rect["w"] / rect["h"] == pytest.approx(1080 / 768, rel=0.02)


# Meme incrustation que PANEL, decalee pour toucher le bord droit de l'image
# (x1 = W), respectivement le coin haut-droit (x1 = W et y0 = 0) : le bord
# d'image compte comme bord valide de l'incrustation quand les autres cotes
# sont nets (TASK-6519).
PANEL_AT_RIGHT_EDGE = (W - 526, 30, W, 326)
FACE_AT_RIGHT_EDGE = (W - 526 + 90, 70, W - 526 + 220, 220)
PANEL_AT_TOP_RIGHT_CORNER = (W - 526, 0, W, 296)
FACE_AT_TOP_RIGHT_CORNER = (W - 526 + 90, 40, W - 526 + 220, 190)


def test_incrustation_stuck_to_the_right_edge_uses_the_image_border_as_that_edge(tmp_path, video_dir):
    write_panel_keyframes(video_dir, PANEL_AT_RIGHT_EDGE, pattern(20, 20, box=FACE_AT_RIGHT_EDGE))
    data = period0(detect(tmp_path)[0])

    assert data["reason"] is None
    assert data["edge_reason"] is None  # bord droit = bord d'image, pas un repli
    rect = data["facecam"]
    px0, py0, px1, py1 = PANEL_AT_RIGHT_EDGE
    assert contains(rect, PANEL_AT_RIGHT_EDGE, eps=3)
    assert abs(rect["x"] - px0) <= 3
    assert abs((rect["x"] + rect["w"]) - px1) <= 3
    assert rect["w"] / rect["h"] == pytest.approx(1080 / 768, rel=0.02)


def test_incrustation_stuck_to_two_edges_in_a_corner_uses_the_image_border_for_both(tmp_path, video_dir):
    write_panel_keyframes(video_dir, PANEL_AT_TOP_RIGHT_CORNER, pattern(20, 20, box=FACE_AT_TOP_RIGHT_CORNER))
    data = period0(detect(tmp_path)[0])

    assert data["reason"] is None
    assert data["edge_reason"] is None  # bords droit et haut = bords d'image, pas un repli
    rect = data["facecam"]
    px0, py0, px1, py1 = PANEL_AT_TOP_RIGHT_CORNER
    # couvre toute l'incrustation, y compris le coin haut-droit
    assert contains(rect, PANEL_AT_TOP_RIGHT_CORNER, eps=3)
    assert abs(rect["x"] - px0) <= 3
    assert abs((rect["x"] + rect["w"]) - px1) <= 3
    assert rect["y"] <= py0 + 3
    assert rect["w"] / rect["h"] == pytest.approx(1080 / 768, rel=0.02)


def test_incrustation_edges_are_excluded_from_the_game_window(tmp_path, video_dir):
    write_panel_keyframes(video_dir, PANEL, pattern(20, 20))
    data = load(run_stream(tmp_path)[0])

    facecam = data["facecam"]
    px0, py0, px1, py1 = PANEL
    # le rectangle facecam couvre le panneau reel, pas juste le visage
    assert contains(facecam, PANEL, eps=3)
    [g] = rects(data["plans"][0], "gameplay")
    assert g["x"] >= facecam["x"] + facecam["w"] or g["y"] >= facecam["y"] + facecam["h"]


# --------------------------------------------------------------------------
# Petites facecams en coin (TASK-c7e682a88189) : trop reduites dans l'image
# entiere, vues dans une vignette de coin agrandie (coordonnees ramenees a
# l'image source).
# --------------------------------------------------------------------------


class RelativeSizeFaceDetector:
    """Detecteur factice : un rectangle blanc n'est vu que s'il occupe au
    moins ``min_ratio`` de la largeur de l'image recue -- simule un visage
    trop petit pour etre vu dans l'image entiere, mais assez grand une fois
    une vignette de coin agrandie."""

    def __init__(self, min_ratio):
        self.min_ratio = min_ratio
        self.closed = False
        self.frames = 0

    def detect(self, frame):
        assert not self.closed, "detecteur utilise apres close()"
        self.frames += 1
        h, w = frame.shape[:2]
        x, y, bw, bh = cv2.boundingRect((frame[:, :, 0] > 128).astype(np.uint8))
        if not bw or not bh or bw < self.min_ratio * w:
            return []
        return [(float(x), float(y), float(x + bw), float(y + bh), 0.9)]

    def close(self):
        self.closed = True


class RelativeSizeDetectorFactory:
    def __init__(self, min_ratio):
        self.min_ratio = min_ratio
        self.built: list[tuple[dict, Device]] = []
        self.detectors: list[RelativeSizeFaceDetector] = []

    def __call__(self, settings, device):
        self.built.append((settings, device))
        detector = RelativeSizeFaceDetector(self.min_ratio)
        self.detectors.append(detector)
        return detector


def test_small_facecam_only_visible_in_an_enlarged_corner_vignette_gives_a_top_right_facecam(tmp_path, video_dir):
    # Visage 160x160 en haut a droite d'une image 1920x1080 (8,3 % de la
    # largeur) : sous le seuil du detecteur factice sur l'image entiere, mais
    # au-dessus une fois la vignette de coin (facecam_corner_size = 30 %)
    # agrandie (facecam_corner_zoom = 2).
    small_face = (1700, 60, 1860, 220)
    write_keyframes(video_dir, pattern(20, 20, box=small_face))
    factory = RelativeSizeDetectorFactory(min_ratio=0.12)

    path, factory = detect(tmp_path, factory=factory)

    data = period0(path)
    assert data["reason"] is None
    rect = data["facecam"]
    assert rect is not None
    assert rect["x"] > W / 2  # en haut a droite de l'image source
    assert rect["y"] < H / 2
    assert contains(rect, small_face)
    assert factory.detectors[0].closed


def test_face_too_small_even_in_corner_vignettes_keeps_the_reason(tmp_path, video_dir):
    # Meme visage, mais un detecteur si exigeant qu'il ne le voit ni dans
    # l'image entiere ni dans une vignette de coin agrandie : la raison
    # d'absence reste celle d'avant (aucun repli silencieux, ADR-ad2e).
    small_face = (1700, 60, 1860, 220)
    write_keyframes(video_dir, pattern(20, 20, box=small_face))
    factory = RelativeSizeDetectorFactory(min_ratio=0.95)

    path, factory = detect(tmp_path, factory=factory)

    data = period0(path)
    assert data["facecam"] is None
    assert "aucun rectangle candidat" in data["reason"]


# --------------------------------------------------------------------------
# Echantillonnage (TASK-493f184c4ce1, TASK-5745) : le nombre d'appels au
# detecteur ne depend pas du nombre d'images cles de scenes.json (248 s sur
# 2326 images cles avant la borne) mais de facecam_period_step et de
# facecam_board_frames.
# --------------------------------------------------------------------------


def test_detector_calls_are_bounded_by_the_period_step_and_the_board_size(tmp_path, video_dir):
    # 120 images cles (10 s d'ecart), une image echantillonnee tous les 120 s :
    # 10 appels pour reperer les periodes, puis 5 par image candidate (24).
    write_timeline(video_dir, every(10.0, 120, "game"))
    path, factory = detect(tmp_path, facecam_period_step=120.0)
    assert factory.detectors[0].frames == 10 + 24 * 5
    assert len(load(path)["keyframes"]) == 120  # toutes les images cles restent listees


def test_board_frame_count_is_configurable(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "game"))
    path, factory = detect(tmp_path, facecam_board_frames=4)
    assert factory.detectors[0].frames == 20 + 24 * 5  # la planche n'en montre que 4
    board = cv2.imread(str(video_dir / period0(path)["board"]))
    assert board.shape[1] == 4 * 480 and board.shape[0] == 270  # 4 images sur une ligne


def test_facecam_detection_is_cached_per_video(tmp_path, video_dir):
    write_keyframes(video_dir, pattern(40, 40))
    factory = PixelDetectorFactory()
    run_stream(tmp_path, start=0.0, end=20.0, clip_id="01", factory=factory)
    # detection + controle du clip (rectangle sur le seul visage, TASK-9957)
    assert len(factory.built) == 2
    run_stream(tmp_path, start=20.0, end=40.0, clip_id="02", factory=factory)
    assert len(factory.built) == 3  # seul le controle du clip : la detection de la video n'est pas refaite
    detect(tmp_path, factory=factory, force=True)
    assert len(factory.built) == 4


# --------------------------------------------------------------------------
# Presence de la facecam par clip (SPEC-8257 regle 2, succede a la regle 2 de
# SPEC-3a88) : le format stream d'un clip se decide desormais sur la
# presence et la vivacite du rectangle lui-meme (contenu non noir, bords
# retrouves, non fige), plus sur la detection d'un visage dedans -- qui ne
# sert plus qu'a LOCALISER la facecam une fois par video (regle 1). VOD
# VOD Twitch de test (streameuse) : webcam visible tout du long, visage detecte
# sur 14 % des images cles globales mais 0 % par clip (jeu sombre, casque).
# --------------------------------------------------------------------------

# Panneau au format exact du panneau camera de sortie (1080 / (1920 * 0.4) =
# 1080/768 = 45/32) : le rectangle localise colle exactement sur ses bords
# reels (aucun agrandissement par _size_camera_rect), loin des vignettes de
# coin (facecam_corner_size = 30 %).
STREAM_PANEL = (700, 350, 1240, 734)  # 540 x 384
STREAM_FACE = (
    STREAM_PANEL[0] + 90, STREAM_PANEL[1] + 40, STREAM_PANEL[0] + 220, STREAM_PANEL[1] + 190,
)


def write_stream_clip_fixture(video_dir, clip_specs, *, panel=STREAM_PANEL, face=STREAM_FACE, seed=0):
    """Images cles synthetiques : un segment de localisation (100 premieres
    secondes, hors de tout clip teste ici ; visage stable dans ``panel``,
    bords nets et constants -- meme esprit que ``write_panel_keyframes``)
    suivi du segment du clip teste (``clip_specs``, liste de (temps, mode)),
    SANS aucun visage dessine dedans : verifie que le choix du format par
    clip ne depend plus de la detection d'un visage (SPEC-8257 regle 2),
    seulement du contenu du rectangle deja localise. ``mode`` :
    - "live" : contenu du panneau qui varie a chaque image (bruit
      reproductible, simule une webcam active sans visage visible) ;
    - "black" : panneau peint en noir uni (camera coupee ou masquee) ;
    - "frozen" : panneau identique a l'image cle precedente (ecran de pause,
      BRB)."""
    rng = np.random.default_rng(seed)
    frames_dir = video_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    px0, py0, px1, py1 = panel
    frames: list[dict] = []
    index = 0

    def write_frame(t, patch, face_box):
        nonlocal index
        image = coarse_noise(rng, 0, 40, H, W)
        image[py0:py1, px0:px1] = patch
        if face_box is not None:
            x0, y0, x1, y1 = face_box
            image[y0:y1, x0:x1] = 255
        name = f"scene0000_{index:03d}.png"
        (frames_dir / name).write_bytes(encode_png(image))
        frames.append({"path": f"frames/{name}", "timecode": t, "scene": 0})
        index += 1
        return image[py0:py1, px0:px1].copy()

    for t, box in pattern(20, 20, box=face, t0=100.5):
        write_frame(t, 90, box)

    prev_patch = None
    for t, mode in clip_specs:
        if mode == "black":
            patch = 0
        elif mode == "frozen" and prev_patch is not None:
            patch = prev_patch
        else:
            patch = coarse_noise(rng, 60, 121, py1 - py0, px1 - px0, cell=6)
        prev_patch = write_frame(t, patch, None)

    (video_dir / "scenes.json").write_text(
        json.dumps({"scenes": [{"start": 0.0, "end": 200.0}], "frames": frames}),
        encoding="utf-8",
    )


def test_clip_with_a_live_but_faceless_facecam_stays_stream(tmp_path, video_dir):
    # 20 images cles du clip, rectangle present et vivant partout, aucun
    # visage dedans (jeu sombre, casque) : reste en stream (SPEC-8257 regle
    # 2, contrairement a l'ancienne regle basee sur la detection du visage).
    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)])
    out, _ = run_stream(tmp_path)

    data = load(out)
    assert data["layout"] == "stream"
    [plan] = data["plans"]
    assert plan["layout"] == "stream"
    facecam = period0(video_dir / "facecam.json")["facecam"]
    assert rect_box(facecam) == STREAM_PANEL  # bords reels retrouves exactement (aspect deja au format camera)
    assert data["facecam"] == facecam
    assert data["facecam_decision"] == {"period": 0, "candidate": 1, "reason": "r"}
    panels = {p["name"]: p for p in plan["panels"]}
    [cam] = panels["camera"]["rects"]
    assert {k: cam[k] for k in "xywh"} == facecam


def test_clip_with_a_black_facecam_stays_letterbox_with_a_logged_reason(tmp_path, video_dir, caplog):
    write_stream_clip_fixture(video_dir, [(0.5 + k, "black") for k in range(20)])
    with caplog.at_level("INFO", logger="clipper.reframe"):
        out, _ = run_stream(tmp_path)

    data = load(out)
    assert data["layout"] == "letterbox"
    assert [p["name"] for p in data["plans"][0]["panels"]] == ["background", "main"]
    assert "0/20" in data["layout_reason"]
    assert data["layout_reason"] in caplog.text


def test_clip_with_a_frozen_facecam_stays_letterbox(tmp_path, video_dir):
    # ecran de pause / BRB : contenu clair (pas noir) mais fige des la
    # deuxieme image cle -- ne satisfait pas le critere "vivant".
    specs = [(0.5, "live")] + [(1.5 + k, "frozen") for k in range(19)]
    write_stream_clip_fixture(video_dir, specs)
    out, _ = run_stream(tmp_path)

    data = load(out)
    assert data["layout"] == "letterbox"
    assert "fige" in data["layout_reason"] or "figee" in data["layout_reason"]


def test_video_without_facecam_stays_letterbox_with_a_logged_reason(tmp_path, video_dir, caplog):
    write_keyframes(video_dir, pattern(20, 0))  # jamais de visage : pas de facecam a localiser
    with caplog.at_level("INFO", logger="clipper.reframe"):
        out, _ = run_stream(tmp_path, start=0.0, end=20.0)

    data = load(out)
    assert data["layout"] == "letterbox"
    assert data["layout_reason"]
    assert period0(video_dir / "facecam.json")["reason"] in data["layout_reason"]
    assert data["layout_reason"] in caplog.text


def test_stream_plan_is_one_fixed_plan_over_the_whole_clip(tmp_path, video_dir):
    # plusieurs coupes de scene dans le clip et visage qui faiblit sur 3
    # images : un seul plan, un seul rectangle par panneau, sans zoom ni suivi
    faces = pattern(20, 20)
    faces[4] = (faces[4][0], None)
    faces[9] = (faces[9][0], (400, 500, 600, 700))
    faces[15] = (faces[15][0], None)
    write_keyframes(video_dir, faces, scenes=((0.0, 5.0), (5.0, 12.0), (12.0, 100.0)))
    out, _ = run_stream(tmp_path, start=1.0, end=19.0)

    data = load(out)
    assert data["layout"] == "stream"
    [plan] = data["plans"]
    assert (plan["start"], plan["end"]) == (1.0, 19.0)
    for panel in plan["panels"]:
        [r] = panel["rects"]
        assert (r["start"], r["end"]) == (1.0, 19.0)


def test_stream_geometry_camera_on_top_game_below_texts_off_the_face(tmp_path, video_dir):
    write_keyframes(video_dir, pattern(20, 20))
    data = load(run_stream(tmp_path)[0])

    assert data["output"] == {"width": 1080, "height": 1920}
    assert data["source"] == {"width": W, "height": H}
    panels = {p["name"]: p for p in data["plans"][0]["panels"]}
    assert [p["name"] for p in data["plans"][0]["panels"]] == ["background", "camera", "gameplay"]
    assert panels["background"]["effect"] == "blur"
    assert panels["background"]["dest"] == {"x": 0, "y": 0, "w": 1080, "h": 1920}

    cam, game = panels["camera"]["dest"], panels["gameplay"]["dest"]
    assert cam["x"] == 0 and cam["w"] == 1080
    assert cam["h"] == 768  # 40 % de 1920
    assert game["x"] == 0 and game["w"] == 1080
    assert game["y"] == cam["y"] + cam["h"]
    assert game["y"] + game["h"] == 1920

    # jeu : le centre de l'image hors facecam, au format de son panneau
    [g] = panels["gameplay"]["rects"]
    facecam = data["facecam"]
    assert g["x"] >= facecam["x"] + facecam["w"] or g["y"] >= facecam["y"] + facecam["h"]
    assert g["w"] / g["h"] == pytest.approx(game["w"] / game["h"], rel=0.02)
    assert 0 <= g["x"] and g["x"] + g["w"] <= W and 0 <= g["y"] and g["y"] + g["h"] <= H

    # titre au-dessus de la camera, sous-titres dans la zone du jeu, jamais sur le visage
    zones = data["text_zones"]
    assert set(zones) == {"title", "subtitles", "part"}
    assert zones["title"]["y1"] <= cam["y"]
    assert zones["subtitles"]["y0"] >= cam["y"] + cam["h"]
    for z in zones.values():
        assert 150 <= z["x0"] < z["x1"] <= 930 and 160 <= z["y0"] < z["y1"] <= 1520


def test_stream_camera_ratio_is_configurable(tmp_path, video_dir):
    write_keyframes(video_dir, pattern(20, 20))
    data = load(run_stream(tmp_path, stream_camera_ratio=0.35)[0])
    cam = {p["name"]: p for p in data["plans"][0]["panels"]}["camera"]["dest"]
    assert cam["h"] == 672


def test_default_layout_is_letterbox_without_facecam_detection(tmp_path, video_dir):
    from clipper.reframe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["layout"] == "letterbox"
    assert CONFIG_DEFAULTS["facecam_period_step"] == 60.0
    assert "facecam_localize_min_share" not in CONFIG_DEFAULTS and "facecam_max_keyframes" not in CONFIG_DEFAULTS
    assert CONFIG_DEFAULTS["facecam_clip_min_share"] == 0.8
    write_keyframes(video_dir, pattern(20, 20))
    out, factory, fake = run(tmp_path, static(), [], format="letterbox", start=0.0, end=20.0)
    assert load(out)["layout"] == "letterbox"
    assert "layout_reason" not in load(out)
    assert factory.built == []
    assert not (video_dir / "facecam.json").exists()


def test_unknown_layout_or_stream_with_crop_format_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    write_keyframes(video_dir, pattern(20, 20))
    with pytest.raises(ReframeError, match="layout"):
        run_stream(tmp_path, layout="stream")
    with pytest.raises(ReframeError, match="crop"):
        run_stream(tmp_path, format="crop")


def test_existing_letterbox_plan_with_stream_auto_config_is_an_error_without_force(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    write_keyframes(video_dir, pattern(20, 20))
    run(tmp_path, static(), [], format="letterbox", start=0.0, end=20.0)
    with pytest.raises(ReframeError, match="stream_auto"):
        run_stream(tmp_path)
    out, _ = run_stream(tmp_path, force=True)
    assert load(out)["layout"] == "stream"


# --------------------------------------------------------------------------
# Agencement stream split (SPEC-76dc) : webcam en haut, jeu en bas, badge et
# sous-titres a deux couleurs. stream_variant n'intervient qu'apres les
# regles 1/2 de SPEC-8257 (choix stream/letterbox), jamais de LLM.
# --------------------------------------------------------------------------


def test_stream_variant_defaults_to_top_unchanged(tmp_path, video_dir):
    from clipper.reframe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["stream_variant"] == "top"
    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)])
    out, _ = run_stream(tmp_path)
    data = load(out)
    assert data["layout"] == "stream"  # jamais "stream_split" sans le demander


def test_unknown_stream_variant_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    write_keyframes(video_dir, pattern(20, 20))
    with pytest.raises(ReframeError, match="stream_variant"):
        run_stream(tmp_path, stream_variant="diagonal")


@pytest.mark.parametrize(
    "name,override",
    [
        ("split_webcam_dest", {"x": 20, "y": 0, "w": 1040, "h": 2000}),  # deborde du canevas 1920 de haut
        ("badge_dest", {"x": 0, "y": 0, "w": 100, "h": 100}),  # hors zone sure (y < safe_top)
        ("split_subtitle_dest", {"x": 0, "y": 0, "w": 100, "h": 100}),  # hors zone sure
    ],
)
def test_invalid_split_geometry_is_an_error_at_config_load(tmp_path, video_dir, name, override):
    from clipper.reframe import ReframeError

    write_keyframes(video_dir, pattern(20, 20))
    with pytest.raises(ReframeError):
        run_stream(tmp_path, stream_variant="split", **{name: override})


def test_split_webcam_and_gameplay_dests_overlapping_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    write_keyframes(video_dir, pattern(20, 20))
    with pytest.raises(ReframeError, match="chevauchent"):
        run_stream(
            tmp_path, stream_variant="split",
            split_webcam_dest={"x": 0, "y": 0, "w": 1080, "h": 700},
            split_gameplay_dest={"x": 0, "y": 640, "w": 1080, "h": 1280},
        )


def test_badge_and_subtitle_dests_overlapping_is_an_error(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    write_keyframes(video_dir, pattern(20, 20))
    with pytest.raises(ReframeError, match="chevauchent"):
        run_stream(
            tmp_path, stream_variant="split",
            badge_dest={"x": 150, "y": 700, "w": 420, "h": 100},
            split_subtitle_dest={"x": 150, "y": 710, "w": 780, "h": 150},
        )


def test_split_layout_crops_webcam_and_gameplay_without_deformation(tmp_path, video_dir):
    # STREAM_PANEL (aspect 1080/768) rogne au ratio par defaut du split
    # (1040/640) : jamais etire (aucune deformation, ratio dest respecte).
    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)])
    out, _ = run_stream(tmp_path, stream_variant="split")

    data = load(out)
    assert data["layout"] == "stream_split"
    [plan] = data["plans"]
    assert plan["layout"] == "stream_split"
    panels = {p["name"]: p for p in plan["panels"]}
    assert set(panels) == {"webcam", "gameplay"}

    webcam_dest, game_dest = panels["webcam"]["dest"], panels["gameplay"]["dest"]
    assert webcam_dest == {"x": 20, "y": 0, "w": 1040, "h": 640}
    assert game_dest == {"x": 0, "y": 640, "w": 1080, "h": 1280}
    # les deux dest tiennent dans le canevas et ne se chevauchent pas
    assert webcam_dest["y"] + webcam_dest["h"] <= game_dest["y"]

    [wcam] = panels["webcam"]["rects"]
    [game] = panels["gameplay"]["rects"]
    assert wcam["w"] / wcam["h"] == pytest.approx(webcam_dest["w"] / webcam_dest["h"], rel=0.02)
    assert game["w"] / game["h"] == pytest.approx(game_dest["w"] / game_dest["h"], rel=0.02)
    # jamais hors du cadre source
    assert 0 <= wcam["x"] and wcam["x"] + wcam["w"] <= W and 0 <= wcam["y"] and wcam["y"] + wcam["h"] <= H
    assert 0 <= game["x"] and game["x"] + game["w"] <= W and 0 <= game["y"] and game["y"] + game["h"] <= H
    # rectangle source webcam centre sur le rectangle localise (facecam.json)
    facecam = data["facecam"]
    fcx = facecam["x"] + facecam["w"] / 2
    wcx = wcam["x"] + wcam["w"] / 2
    assert wcx == pytest.approx(fcx, abs=1.0)

    for (start, end) in ((wcam["start"], wcam["end"]), (game["start"], game["end"])):
        assert (start, end) == (0.0, 20.0)


def test_split_gameplay_excludes_the_webcam_source_column_when_it_fits(tmp_path, video_dir):
    # Webcam collee au bord gauche : assez de place a droite pour exclure sa
    # colonne entierement de la fenetre de jeu (SPEC-76dc).
    panel = (0, 300, 400, 700)
    face = (panel[0] + 90, panel[1] + 40, panel[0] + 220, panel[1] + 190)
    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)], panel=panel, face=face)
    out, _ = run_stream(tmp_path, stream_variant="split")

    data = load(out)
    plan = data["plans"][0]
    panels = {p["name"]: p for p in plan["panels"]}
    [wcam] = panels["webcam"]["rects"]
    [game] = panels["gameplay"]["rects"]
    assert game["x"] >= wcam["x"] + wcam["w"]  # jeu entierement a droite de la webcam
    assert plan["reason"] is None  # exclusion reussie, pas de repli a noter


def test_split_gameplay_falls_back_to_a_centered_crop_when_it_cannot_exclude_the_webcam(tmp_path, video_dir):
    # STREAM_PANEL est trop central : ni a gauche ni a droite assez de place
    # pour la fenetre de jeu (911 px) sans recouvrir la webcam -- repli
    # centre, note dans le plan (repli silencieux accepte, SPEC-76dc).
    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)])
    out, _ = run_stream(tmp_path, stream_variant="split")

    data = load(out)
    plan = data["plans"][0]
    panels = {p["name"]: p for p in plan["panels"]}
    [game] = panels["gameplay"]["rects"]
    assert game["x"] == pytest.approx((W - game["w"]) / 2, abs=1)
    assert plan["reason"] is not None
    assert "webcam" in plan["reason"]


def test_split_text_zones_have_subtitles_badge_and_part_within_the_safe_zone(tmp_path, video_dir):
    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)])
    data = load(run_stream(tmp_path, stream_variant="split")[0])

    zones = data["text_zones"]
    assert {"badge", "subtitles", "part"} <= set(zones)
    for name in ("badge", "subtitles", "part"):
        z = zones[name]
        assert 150 <= z["x0"] < z["x1"] <= 930 and 160 <= z["y0"] < z["y1"] <= 1520
    # badge et sous-titres ne se recouvrent jamais
    badge, subs = zones["badge"], zones["subtitles"]
    assert badge["y1"] <= subs["y0"] or subs["y1"] <= badge["y0"] or badge["x1"] <= subs["x0"] or subs["x1"] <= badge["x0"]


def test_split_title_zone_absent_by_default_webcam_leaves_no_room_above(tmp_path, video_dir):
    # split_webcam_dest.y = 0 par defaut : pas de place pour un titre
    # au-dessus, reframe omet la zone (render.py, pas reframe.py, decidera
    # si c'est une erreur selon [render] title_enabled).
    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)])
    data = load(run_stream(tmp_path, stream_variant="split")[0])
    assert "title" not in data["text_zones"]


def test_split_title_zone_present_when_webcam_leaves_room_above(tmp_path, video_dir):
    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)])
    data = load(run_stream(
        tmp_path, stream_variant="split",
        split_webcam_dest={"x": 20, "y": 300, "w": 1040, "h": 640},
        split_gameplay_dest={"x": 0, "y": 940, "w": 1080, "h": 980},
    )[0])
    assert "title" in data["text_zones"]
    z = data["text_zones"]["title"]
    assert z["y1"] <= 300 - 16  # text_gap par defaut


def test_split_layout_json_field_is_stream_split(tmp_path, video_dir):
    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)])
    data = load(run_stream(tmp_path, stream_variant="split")[0])
    assert data["layout"] == "stream_split"
    assert data["plans"][0]["layout"] == "stream_split"


def test_split_reframe_cached_with_a_different_stream_variant_is_an_error_without_force(tmp_path, video_dir):
    from clipper.reframe import ReframeError

    write_stream_clip_fixture(video_dir, [(0.5 + k, "live") for k in range(20)])
    run_stream(tmp_path, stream_variant="top")
    with pytest.raises(ReframeError, match="stream_variant"):
        run_stream(tmp_path, stream_variant="split")
    out, _ = run_stream(tmp_path, stream_variant="split", force=True)
    assert load(out)["layout"] == "stream_split"


# --------------------------------------------------------------------------
# Webcam par periode du stream (TASK-5745) : periodes reperees en local
# (Just Chatting plein ecran puis jeu), rectangles candidats numerotes
# dessines sur une planche, Claude (usage "facecam") repond un numero ou
# "aucun", chaque clip prend le rectangle de la periode de son debut.
# --------------------------------------------------------------------------

CHAT_FACE = (760, 240, 1160, 740)  # grand visage plein ecran (500 px = 46 % de H)
PANEL2 = (1380, 600, 1920, 984)  # 540 x 384, colle au bord droit : autre incrustation
FACE2 = (1470, 640, 1600, 790)


_TIMELINE_CACHE: dict = {}
_TIMELINE_CACHE_DIR: Path | None = None


def write_timeline(video_dir, specs, *, seed=0):
    """Copie dans ``video_dir`` la timeline de ``specs`` (voir
    _write_timeline_uncached), fabriquee une seule fois par processus. Le
    cache ne sert que de source de copies : aucun test ne lit ni n'ecrit
    dedans, donc un test ne peut pas corrompre la timeline d'un autre."""
    global _TIMELINE_CACHE_DIR
    key = (seed, tuple(tuple(spec) for spec in specs))
    if key not in _TIMELINE_CACHE:
        if _TIMELINE_CACHE_DIR is None:
            _TIMELINE_CACHE_DIR = Path(tempfile.mkdtemp(prefix="timeline-cache-"))
            atexit.register(shutil.rmtree, _TIMELINE_CACHE_DIR, ignore_errors=True)
        source = _TIMELINE_CACHE_DIR / str(len(_TIMELINE_CACHE))
        source.mkdir()
        _write_timeline_uncached(source, specs, seed=seed)
        _TIMELINE_CACHE[key] = source
    source = _TIMELINE_CACHE[key]
    frames_dir = video_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    for frame in (source / "frames").iterdir():
        shutil.copyfile(frame, frames_dir / frame.name)
    shutil.copyfile(source / "scenes.json", video_dir / "scenes.json")


def _write_timeline_uncached(video_dir, specs, *, seed=0):
    """scenes.json + images cles synthetiques. ``specs`` : liste de (temps,
    mode) ; mode : "chat" (grand visage blanc plein ecran), "game" (jeu
    bruite + panneau STREAM_PANEL avec un visage), "game2" (idem avec
    PANEL2), "live" (panneau STREAM_PANEL sans visage, contenu qui change),
    "static" (panneau STREAM_PANEL fixe, jamais en mouvement), "plain"
    (jeu bruite sans rien)."""
    rng = np.random.default_rng(seed)
    frames_dir = video_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    frames = []
    static_patch = coarse_noise(rng, 60, 121, 384, 540, cell=6)
    for k, (t, mode) in enumerate(specs):
        image = coarse_noise(rng, 0, 40, H, W)
        dx = (k % 3) - 1
        if mode == "chat":
            x0, y0, x1, y1 = CHAT_FACE
            image[y0:y1, x0:x1] = 255
        elif mode in ("game", "game2"):
            panel, face = (STREAM_PANEL, STREAM_FACE) if mode == "game" else (PANEL2, FACE2)
            px0, py0, px1, py1 = panel
            image[py0:py1, px0:px1] = 90
            fx0, fy0, fx1, fy1 = face
            image[fy0 - dx:fy1 - dx, fx0 + dx:fx1 + dx] = 255
        elif mode == "live":
            px0, py0, px1, py1 = STREAM_PANEL
            image[py0:py1, px0:px1] = coarse_noise(rng, 60, 121, py1 - py0, px1 - px0, cell=6)
        elif mode == "static":
            px0, py0, px1, py1 = STREAM_PANEL
            image[py0:py1, px0:px1] = static_patch
        name = f"scene0000_{k:03d}.png"
        (frames_dir / name).write_bytes(encode_png(image))
        frames.append({"path": f"frames/{name}", "timecode": t, "scene": 0})
    end = max(t for t, _ in specs) + 10.0
    (video_dir / "scenes.json").write_text(
        json.dumps({"scenes": [{"start": 0.0, "end": end}], "frames": frames}), encoding="utf-8"
    )


def every(step, count, mode, t0=0.0):
    return [(t0 + k * step, mode) for k in range(count)]


def webcam_answer(number, reason="r"):
    return {"webcam": number, "reason": reason}


def run_detect(tmp_path, responses, *, force=False, **reframe):
    """detect_facecam avec un Claude factice ; renvoie (chemin, FakeBackend)."""
    from clipper.reframe import detect_facecam

    fake = FakeBackend(responses)
    with llm.use_backend(fake):
        path = detect_facecam(
            VIDEO_ID, tmp_path / "workspace", config=stream_config(tmp_path, **reframe),
            force=force, detector_factory=PixelDetectorFactory(),
        )
    return path, fake


def test_just_chatting_then_game_gives_two_periods_cut_at_the_first_game_keyframe(tmp_path, video_dir):
    write_timeline(video_dir, [(10.0 * k, "chat" if k < 60 else "game") for k in range(120)])
    path, fake = run_detect(tmp_path, [webcam_answer(1)])

    periods = load(path)["periods"]
    assert [(p["start"], p["end"]) for p in periods] == [(0.0, 600.0), (600.0, 1190.0)]
    # le grand visage plein ecran n'est pas un candidat (plus d'un quart de l'image) :
    # pas de planche pour la 1re periode, une seule pour la 2e
    assert periods[0]["candidates"] == [] and periods[0]["board"] is None
    assert periods[1]["board"] is not None
    assert len(fake.calls) == 1


def test_a_transition_that_is_not_clean_gives_a_single_period(tmp_path, video_dir):
    # du jeu, puis un grand visage au milieu du stream : pas un Just Chatting
    # suivi du jeu, la majorite des images de la planche decide
    specs = [(10.0 * k, "chat" if 60 <= k < 90 else "game") for k in range(120)]
    write_timeline(video_dir, specs)
    path, fake = run_detect(tmp_path, [webcam_answer(1)])

    periods = load(path)["periods"]
    assert len(periods) == 1
    assert periods[0]["start"] == 0.0
    assert "nette" in periods[0]["transition"]
    assert len(fake.calls) == 1


def test_a_big_face_coming_back_later_in_the_game_stays_in_the_second_period(tmp_path, video_dir):
    # Just Chatting au debut, jeu, puis une pause face camera plus tard : 2 periodes seulement
    specs = [(10.0 * k, "chat" if k < 30 or 80 <= k < 100 else "game") for k in range(120)]
    write_timeline(video_dir, specs)
    path, _ = run_detect(tmp_path, [webcam_answer(1)])
    assert [(p["start"], p["end"]) for p in load(path)["periods"]] == [(0.0, 300.0), (300.0, 1190.0)]


def test_a_faceless_intro_before_the_just_chatting_does_not_hide_the_transition(tmp_path, video_dir):
    specs = [(10.0 * k, "plain" if k < 6 else "chat" if k < 60 else "game") for k in range(120)]
    write_timeline(video_dir, specs)
    path, _ = run_detect(tmp_path, [webcam_answer(1)])
    assert [(p["start"], p["end"]) for p in load(path)["periods"]] == [(0.0, 600.0), (600.0, 1190.0)]


def test_a_stream_without_just_chatting_is_a_single_period(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "game"))
    path, fake = run_detect(tmp_path, [webcam_answer(1)])
    [period] = load(path)["periods"]
    assert period["facecam"] is not None
    assert len(fake.calls) == 1


def test_claude_gets_one_board_per_period_and_never_coordinates(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "game"))
    path, fake = run_detect(tmp_path, [webcam_answer(1)])

    [call] = fake.calls
    assert call.usage == "facecam"
    [image, zoom] = call.images  # planche + vignettes (TASK-a769)
    assert image.exists() and zoom.exists()
    assert str(image).startswith(str(tmp_path / "workspace" / VIDEO_ID))
    board = cv2.imread(str(image))
    assert board is not None and board.shape[1] <= 2400  # une seule planche, taille bornee
    assert set(call.schema["properties"]) == {"webcam", "reason"}
    assert set(call.schema["required"]) == {"webcam", "reason"}
    assert "numero" in call.prompt.lower()
    assert "coordonn" in call.prompt.lower()  # le prompt interdit les coordonnees


def test_candidates_are_numbered_from_one_and_written_with_the_answer(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "game"))
    path, _ = run_detect(tmp_path, [webcam_answer(1, "c'est la webcam")])

    [period] = load(path)["periods"]
    ids = [c["id"] for c in period["candidates"]]
    assert ids == list(range(1, len(ids) + 1)) and ids
    assert period["answer"] == {"webcam": 1, "reason": "c'est la webcam"}
    chosen = [c for c in period["candidates"] if c["id"] == 1][0]
    assert period["facecam"] == chosen["rect"]
    assert contains(period["facecam"], STREAM_FACE)
    assert period["facecam"]["w"] / period["facecam"]["h"] == pytest.approx(1080 / 768, rel=0.02)


def test_a_webcam_without_any_face_is_still_a_candidate_when_it_moves(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "live"))
    path, fake = run_detect(tmp_path, [webcam_answer(1)])

    [period] = load(path)["periods"]
    assert contains(period["facecam"], STREAM_PANEL, eps=3)  # cadre retrouve a 2-3 px pres
    assert period["facecam"]["w"] <= (STREAM_PANEL[2] - STREAM_PANEL[0]) + 6
    assert all(c["kind"] == "cadre" for c in period["candidates"])


def test_a_framed_rectangle_that_never_moves_is_not_a_candidate(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "static"))
    path, fake = run_detect(tmp_path, [webcam_answer(1)])

    [period] = load(path)["periods"]
    assert period["candidates"] == []
    assert period["facecam"] is None
    assert period["answer"] is None
    assert period["reason"]
    assert fake.calls == []  # rien a numeroter : pas d'appel a Claude


def test_none_answer_gives_no_webcam_for_the_period_with_claudes_reason(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "live"))  # sans visage stable (TASK-a769)
    path, _ = run_detect(tmp_path, [webcam_answer(None, "c'est un widget de chat")])

    [period] = load(path)["periods"]
    assert period["facecam"] is None
    assert "widget de chat" in period["reason"]


def test_an_answer_naming_an_unknown_rectangle_is_an_explicit_failure(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "game"))
    with pytest.raises(llm.SchemaError):
        run_detect(tmp_path, [webcam_answer(42)])
    assert not (video_dir / "facecam.json").exists()  # rien d'ecrit a moitie


def test_each_clip_takes_the_rectangle_of_the_period_that_contains_its_start(tmp_path, video_dir):
    write_timeline(video_dir, [(10.0 * k, "chat" if k < 20 else "game") for k in range(120)])
    answers = [webcam_answer(1)]  # seule la periode de jeu a une planche
    out, _ = run_stream(tmp_path, start=400.0, end=430.0, answers=answers)
    data = load(out)
    assert data["layout"] == "stream"
    assert data["facecam_decision"]["period"] == 1
    assert data["facecam_decision"]["candidate"] == 1
    # clip qui commence pendant le Just Chatting : periode 0, "aucun" -> letterbox
    out0, _ = run_stream(tmp_path, start=20.0, end=50.0, clip_id="02", answers=answers)
    data0 = load(out0)
    assert data0["layout"] == "letterbox"
    decision = data0["facecam_decision"]
    assert (decision["period"], decision["candidate"]) == (0, None)
    assert "aucun rectangle candidat" in decision["reason"]
    assert decision["reason"] in data0["layout_reason"]


def test_period_without_webcam_keeps_the_letterbox_rule_with_the_decision_written(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "live"))  # sans visage stable (TASK-a769)
    out, _ = run_stream(tmp_path, start=100.0, end=130.0, answers=[webcam_answer(None, "pas de webcam ici")])
    data = load(out)
    assert data["layout"] == "letterbox"
    assert "pas de webcam ici" in data["layout_reason"]
    assert data["facecam_decision"]["candidate"] is None


def test_nothing_is_remembered_from_one_stream_to_another(tmp_path, video_dir):
    from clipper.reframe import detect_facecam

    write_timeline(video_dir, every(10.0, 120, "live"))
    path, _ = run_detect(tmp_path, [webcam_answer(1)])
    other = tmp_path / "workspace" / "zzzzzzzzzzz"
    other.mkdir()
    (other / "zzzzzzzzzzz.mp4").write_bytes(b"x")
    write_timeline(other, every(10.0, 120, "live"))

    fake2 = FakeBackend([webcam_answer(None)])
    with llm.use_backend(fake2):
        p2 = detect_facecam("zzzzzzzzzzz", tmp_path / "workspace", config=stream_config(tmp_path),
                            detector_factory=PixelDetectorFactory())
    assert len(fake2.calls) == 1  # il redemande, il ne reprend pas la reponse de l'autre
    assert load(p2)["periods"][0]["facecam"] is None
    assert load(path)["periods"][0]["facecam"] is not None


# --------------------------------------------------------------------------
# Vraie passe de controle (optionnelle, jamais lancee par defaut) : le vrai
# Claude et le vrai detecteur mediapipe sur les copies de travail des deux VOD
# reelles (constats du 2026-10-05). Sautee sauf CLIPPER_CLAUDE_INTEGRATION=1
# avec `claude` dans le PATH. Entrees en lecture seule (liens physiques vers
# workspace/<vid>/frames, copie de scenes.json) ; tout est ecrit sous
# research/facecam-5745/<vid>/, jamais dans workspace/, output/ ni state/.
# Cout attendu : environ 0,05 $ par VOD (1 a 3 appels vision), journalise dans
# research/facecam-5745/<vid>/llm_usage.jsonl.
#   $env:CLIPPER_CLAUDE_INTEGRATION = "1"
#   $env:CLIPPER_REAL_WORKSPACE = "E:\...\workspace"      (defaut : workspace)
#   $env:CLIPPER_REAL_RESEARCH = "E:\...\research"        (defaut : research)
#   python -m pytest -q tests/test_reframe.py -k real_vod
# --------------------------------------------------------------------------

REAL_VODS = ("v2887364910", "v2888230655")


@pytest.mark.skipif(
    os.environ.get("CLIPPER_CLAUDE_INTEGRATION") != "1" or shutil.which("claude") is None,
    reason="vrai Claude : CLIPPER_CLAUDE_INTEGRATION=1 et `claude` dans le PATH",
)
@pytest.mark.parametrize("vod", REAL_VODS)
def test_real_vod_webcam_by_period_with_the_real_claude(vod):
    from clipper.reframe import detect_facecam

    source = Path(os.environ.get("CLIPPER_REAL_WORKSPACE", "workspace")) / vod
    if not (source / "scenes.json").exists():
        pytest.skip(f"VOD reelle absente : {source}")
    root = Path(os.environ.get("CLIPPER_REAL_RESEARCH", "research")) / "facecam-5745"
    work = root / vod
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "scenes.json", work / "scenes.json")
    if not (work / "frames").exists():
        try:
            shutil.copytree(source / "frames", work / "frames", copy_function=os.link)
        except OSError:
            shutil.copytree(source / "frames", work / "frames")
    usage = work / "llm_usage.jsonl"
    usage.unlink(missing_ok=True)

    config = Config(
        mode="review", workspace_dir=root, output_dir=root,
        _sections={"reframe": {"format": "letterbox", "layout": "stream_auto"}},
    )
    with llm.usage_log(usage):
        path = detect_facecam(vod, root, config=config, force=True)

    periods = load(path)["periods"]
    assert periods
    for period in periods:
        assert period["candidates"] or period["answer"] is None  # rien a numeroter : pas d'appel
        if period["answer"] is not None:
            assert period["answer"]["webcam"] in [None] + [c["id"] for c in period["candidates"]]
        assert (period["facecam"] is None) == (period["reason"] is not None)
    # chacun de ces deux streams a une vraie webcam pendant le jeu
    assert any(p["facecam"] is not None for p in periods), [p["reason"] for p in periods]
    calls = [json.loads(line) for line in usage.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(calls) == sum(p["answer"] is not None for p in periods)
    print(f"{vod}: {len(calls)} appel(s), cout {sum((c.get('cost_usd') or 0.0) for c in calls):.4f} $")


# --------------------------------------------------------------------------
# TASK-baa8d32f7076 : candidats de webcam (revue Fable nuit I1, M1, M2)
# --------------------------------------------------------------------------


class _NoFaces:
    def detect(self, image):
        return []


def _candidate_setup(monkeypatch, boxes, face_support=None, face_box_index=0):
    """Detecteurs simules : ``boxes`` = cadres nets (x0, y0, x1, y1) a support
    plein, et, si ``face_support``, un visage dans ``boxes[face_box_index]``."""
    from clipper import reframe

    settings = dict(reframe.CONFIG_DEFAULTS)
    images = [np.zeros((720, 1280, 3), dtype=np.uint8) for _ in range(4)]
    frames = [{"kind": "cadre", "box": b, "support": len(images), "edge_reason": None} for b in boxes]
    monkeypatch.setattr(reframe, "_frame_candidates", lambda counts, n, grays, s: (frames, []))
    if face_support is None:
        monkeypatch.setattr(reframe, "_face_clusters", lambda *a, **k: [])
    else:
        x0, y0, x1, y1 = boxes[face_box_index]
        monkeypatch.setattr(
            reframe, "_face_clusters", lambda *a, **k: [((x0 + 20.0, y0 + 10.0, x1 - 20.0, y1 - 10.0), face_support)]
        )

        def fake_rect(counts, n, face, width, height, s):
            rect, _ = reframe._size_camera_rect((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0, width, height, s)
            return rect, None, None

        monkeypatch.setattr(reframe, "_facecam_rect", fake_rect)
    return reframe, images, settings


def test_face_plus_frame_candidate_is_never_evicted_before_pure_frames(monkeypatch):
    boxes = [(40 + 130 * i, 40, 160 + 130 * i, 130) for i in range(9)]  # 1 webcam + 8 cadres de HUD
    reframe, images, settings = _candidate_setup(monkeypatch, boxes, face_support=2)

    candidates, rejected = reframe._period_candidates(images, _NoFaces(), settings)

    assert len(candidates) == settings["facecam_candidate_max"]
    both = [c for c in candidates if c["kind"] == "visage+cadre"]
    assert len(both) == 1
    assert both[0]["support"] == len(images)  # le support du cadre, pas celui du visage


def test_candidates_cut_by_the_maximum_are_written_in_rejected_with_a_reason(monkeypatch):
    boxes = [(40 + 130 * i, 40, 160 + 130 * i, 130) for i in range(9)]
    reframe, images, settings = _candidate_setup(monkeypatch, boxes)

    candidates, rejected = reframe._period_candidates(images, _NoFaces(), settings)

    assert len(candidates) == 8
    cut = [r for r in rejected if "facecam_candidate_max" in r["reason"]]
    assert len(cut) == 1
    assert cut[0]["kind"] == "cadre"
    assert cut[0]["box"] and "8" in cut[0]["reason"]


def test_prompt_does_not_claim_the_face_is_seen_on_all_frames(monkeypatch):
    boxes = [(40, 40, 200, 130)]
    reframe, images, settings = _candidate_setup(monkeypatch, boxes, face_support=2)

    candidates, _ = reframe._period_candidates(images, _NoFaces(), settings)
    prompt = reframe._facecam_prompt(candidates, 0, 60)

    assert candidates[0]["kind"] == "visage+cadre"
    assert "visage detecte sur 2 image(s) cle(s)" in prompt
    assert f"cadre net sur {len(images)} image(s)" in prompt


def test_two_nested_frames_without_a_face_are_not_labelled_face_plus_frame(monkeypatch):
    boxes = [(40, 40, 200, 130), (44, 44, 204, 134)]  # bordure externe + interne
    reframe, images, settings = _candidate_setup(monkeypatch, boxes)

    candidates, _ = reframe._period_candidates(images, _NoFaces(), settings)

    assert [c["kind"] for c in candidates] == ["cadre"]
    prompt = reframe._facecam_prompt(candidates, 0, 60)
    assert "rectangle visage" not in prompt.split("Rectangles candidats :")[1].split("Reponds")[0]


def test_period_candidates_keeps_gray_frames_as_uint8(monkeypatch):
    from clipper import reframe

    seen = []
    real = reframe._frame_candidates

    def spy(counts, n, grays, s):
        seen.extend(g.dtype for g in grays)
        return real(counts, n, grays, s)

    monkeypatch.setattr(reframe, "_frame_candidates", spy)
    images = [np.zeros((720, 1280, 3), dtype=np.uint8) for _ in range(3)]
    reframe._period_candidates(images, _NoFaces(), dict(reframe.CONFIG_DEFAULTS))
    assert seen and all(d == np.uint8 for d in seen)


def test_rect_moves_gives_the_same_answer_on_uint8_and_float_grays():
    from clipper import reframe

    settings = dict(reframe.CONFIG_DEFAULTS)
    rng = np.random.default_rng(0)
    grays8 = [rng.integers(0, 256, (200, 300), dtype=np.uint8) for _ in range(4)]
    still8 = [grays8[0].copy() for _ in range(4)]
    rect = (20, 20, 200, 120)
    assert reframe._rect_moves(grays8, rect, settings) is True
    assert reframe._rect_moves([g.astype(np.float64) for g in grays8], rect, settings) is True
    assert reframe._rect_moves(still8, rect, settings) is False


# --- TASK-53e3 : bords reels decales de quelques px du rectangle choisi ---

_OFFSET_RECT = {"x": 32, "y": 360, "w": 336, "h": 238}


def _offset_frame_image(phase: float, *, framed: bool) -> np.ndarray:
    """Image 1920x1080 : fond plat sombre ; webcam au contenu lisse (gradient
    faible, il change a chaque image) dont les vrais bords sont decales de
    quelques px du rectangle choisi (gauche +6, droit -5, haut +5, bas -2,
    comme TheGuill v2887364910) quand ``framed``."""
    image = np.full((1080, 1920, 3), 30, dtype=np.uint8)
    r = _OFFSET_RECT
    if framed:
        x0, x1 = r["x"] + 6, r["x"] + r["w"] - 5
        y0, y1 = r["y"] + 5, r["y"] + r["h"] - 2
    else:
        x0, x1, y0, y1 = r["x"], r["x"] + r["w"], r["y"], r["y"] + r["h"]
    xs = np.arange(x0, x1)[None, :]
    ys = np.arange(y0, y1)[:, None]
    wave = 120 + 30 * np.sin(xs / 16.0 + phase) * np.cos(ys / 20.0 - phase)
    image[y0:y1, x0:x1] = np.clip(wave, 0, 255).astype(np.uint8)[..., None]
    return image


def _offset_facecam(n: int) -> dict:
    return {
        "index": 1,
        "facecam": dict(_OFFSET_RECT),
        "reason": None,
        "edge_reason": None,
        "keyframes": [{"timecode": float(i), "path": f"k{i}.jpg"} for i in range(n)],
    }


def _offset_reader(framed: bool):
    def read(path: str) -> np.ndarray:
        return _offset_frame_image(float(Path(path).stem[1:]) * 1.7, framed=framed)

    return read


def test_clip_facecam_keeps_webcam_whose_real_edges_are_a_few_px_off_the_rect(tmp_path):
    from clipper import reframe

    settings = dict(reframe.CONFIG_DEFAULTS)
    rect, reason = reframe._clip_facecam(
        _offset_facecam(8), 0.0, 7.0, settings, tmp_path, image_reader=_offset_reader(True)
    )
    assert reason is None
    assert rect == _OFFSET_RECT


def test_clip_facecam_still_rejects_a_rect_with_no_edge_anywhere_near(tmp_path):
    from clipper import reframe

    settings = dict(reframe.CONFIG_DEFAULTS)
    facecam = _offset_facecam(8)
    far = {"x": 600, "y": 100, "w": 336, "h": 238}  # fond plat, aucun bord proche
    facecam["facecam"] = far

    def read(path: str) -> np.ndarray:
        image = _offset_frame_image(float(Path(path).stem[1:]) * 1.7, framed=True)
        image[100:338, 600:936] = 120  # contenu non noir mais sans bord ni mouvement lisible
        image[100:338, 600:936] += (np.arange(600, 936)[None, :, None] % 7).astype(np.uint8)
        return image

    rect, reason = reframe._clip_facecam(facecam, 0.0, 7.0, settings, tmp_path, image_reader=read)
    assert rect is None
    assert reason is not None


# --------------------------------------------------------------------------
# Recalage des bords du rectangle choisi sur la vraie incrustation (TASK-893d)
# : Claude choisit le bon candidat, mais son rectangle est decale (visage
# centre faute de bords retrouves) ou englobe la bordure noire / la bande de
# l'overlay. Mesure reelle (VOD de test) : gauche +35 droit +26 haut -8
# bas -42 px ; TheGuill v2887364910 +-6 px de bordure noire par cote.
# --------------------------------------------------------------------------

# Webcam reelle (non au format du panneau camera, comme la VOD de test : 430 x 300).
REAL_CAM = (700, 300, 1130, 600)
# Rectangle candidat au format du panneau camera mais decale de la webcam.
SHIFTED_RECT = {"x": 660, "y": 290, "w": 540, "h": 384}


def refine_images(n=12, *, cam=REAL_CAM, seed=3):
    """Images synthetiques : fond sombre qui change a chaque image (jeu),
    webcam plus claire et fixe en ``cam`` (x0, y0, x1, y1), bruit leger."""
    rng = np.random.default_rng(seed)
    images = []
    for _ in range(n):
        img = coarse_noise(rng, 0, 60, H, W)
        x0, y0, x1, y1 = cam
        img[y0:y1, x0:x1] = rng.integers(180, 190, size=(y1 - y0, x1 - x0, 3), dtype=np.uint8)
        images.append(img)
    return images


def refine(images, rect=SHIFTED_RECT, face=None, **overrides):
    from clipper.reframe import CONFIG_DEFAULTS, _refine_rect

    return _refine_rect(images, dict(rect), face, {**CONFIG_DEFAULTS, **overrides})


def test_refine_snaps_each_edge_on_the_real_webcam_and_keeps_the_panel_aspect():
    refined, reason = refine(refine_images())
    assert refined is not None, reason
    x0, y0, x1, y1 = rect_box(refined)
    cx0, cy0, cx1, cy1 = REAL_CAM
    # couvre toute la vraie webcam, a 2 px pres ...
    assert x0 <= cx0 + 2 and y0 <= cy0 + 2 and x1 >= cx1 - 2 and y1 >= cy1 - 2
    # ... agrandie au minimum pour le format du panneau camera (ici en hauteur),
    # donc la largeur colle a la webcam : plus de bande d'overlay a gauche/droite
    assert refined["w"] <= (cx1 - cx0) + 4
    assert refined["w"] / refined["h"] == pytest.approx(1080 / 768, rel=0.02)
    # centree sur la vraie webcam
    assert (x0 + x1) / 2 == pytest.approx((cx0 + cx1) / 2, abs=3)
    assert (y0 + y1) / 2 == pytest.approx((cy0 + cy1) / 2, abs=3)
    for side in ("gauche", "droit", "haut", "bas"):
        assert side in reason


def test_refine_never_moves_an_edge_beyond_the_margin_and_says_why():
    # bord gauche de la vraie webcam a 400 px du rectangle : hors marge (20 % de la largeur)
    far = (SHIFTED_RECT["x"] + 400, 300, 1300, 600)
    refined, reason = refine(refine_images(cam=far))
    cx0 = far[0]
    if refined is not None:
        assert refined["x"] < cx0  # le bord gauche n'a pas saute jusqu'a la webcam
    assert "gauche" in reason and "introuvable" in reason


def test_refine_margin_is_configurable():
    refined, _ = refine(refine_images(), facecam_refine_margin_ratio=0.01)
    assert refined is None  # decalages de 40 px et plus : tous hors d'une marge de ~5 px


def test_refine_without_any_reliable_edge_changes_nothing_and_journals_it():
    rng = np.random.default_rng(1)
    images = [coarse_noise(rng, 0, 60, H, W) for _ in range(12)]
    refined, reason = refine(images)
    assert refined is None
    assert reason and "introuvable" in reason


def test_refine_does_not_cut_the_stable_face():
    # un visage tres clair colle au bord gauche de la webcam ne doit pas servir de bord
    images = refine_images()
    for img in images:
        img[400:520, 700:760] = 255
    refined, reason = refine(images, face=(700.0, 400.0, 760.0, 520.0))
    if refined is not None:
        assert refined["x"] <= 700
    assert "visage" in reason or refined is not None


def write_noise_keyframes(video_dir, n=24, cam=REAL_CAM):
    frames_dir = video_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    frames = []
    for k, img in enumerate(refine_images(n, cam=cam)):
        name = f"scene0000_{k:03d}.png"
        (frames_dir / name).write_bytes(encode_png(img))
        frames.append({"path": f"frames/{name}", "timecode": 0.5 + k, "scene": 0})
    (video_dir / "scenes.json").write_text(
        json.dumps({"scenes": [{"start": 0.0, "end": 100.0}], "frames": frames}), encoding="utf-8"
    )


def test_detect_facecam_writes_refined_and_original_rectangles(tmp_path, video_dir, monkeypatch):
    from clipper import reframe as reframe_mod

    write_noise_keyframes(video_dir)
    candidate = {
        "id": 1, "kind": "visage", "rect": dict(SHIFTED_RECT), "support": 20, "face_support": 20,
        "edge_reason": "bords introuvables : rectangle centre sur le visage conserve", "face": None,
    }
    monkeypatch.setattr(reframe_mod, "_period_candidates", lambda images, detector, settings: ([candidate], []))
    path, _ = detect(tmp_path)

    period = period0(path)
    assert period["candidate_rect"] == SHIFTED_RECT  # rectangle d'origine conserve
    refined = period["refined_rect"]
    assert refined is not None and refined != SHIFTED_RECT
    assert period["facecam"] == refined  # c'est lui que les clips utiliseront
    assert period["refine_reason"]
    x0, y0, x1, y1 = rect_box(refined)
    assert x0 <= REAL_CAM[0] + 2 and x1 >= REAL_CAM[2] - 2 and y0 <= REAL_CAM[1] + 2 and y1 >= REAL_CAM[3] - 2
    assert refined["w"] <= REAL_CAM[2] - REAL_CAM[0] + 4


# --------------------------------------------------------------------------
# TASK-9957 : rectangle ancre sur le seul visage (edge_reason non nul, aucun
# bord reel a retrouver) : le contenu d'un jeu qui bouge passait pour une
# webcam vivante (ni noir, ni fige). Il faut un visage dans le rectangle sur
# facecam_clip_face_min_share des images cles du clip, sinon letterbox.
# Mesure reelle (mediapipe) : clips faux 0/N, clips sains 64 % a 100 %.
# --------------------------------------------------------------------------


def _write_noise_keyframes(video_dir, faces, *, seed=0):
    """Comme ``write_keyframes`` mais le fond est un bruit vivant (< 128 : le
    detecteur factice ne le prend pas pour un visage), jamais noir ni fige."""
    rng = np.random.default_rng(seed)
    frames_dir = video_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    frames = []
    for k, (t, box) in enumerate(faces):
        image = coarse_noise(rng, 40, 120, H, W)
        if box is not None:
            x0, y0, x1, y1 = box
            image[y0:y1, x0:x1] = 255
        name = f"scene0000_{k:03d}.png"
        (frames_dir / name).write_bytes(encode_png(image))
        frames.append({"path": f"frames/{name}", "timecode": t, "scene": 0})
    (video_dir / "scenes.json").write_text(
        json.dumps({"scenes": [{"start": 0.0, "end": 200.0}], "frames": frames}), encoding="utf-8",
    )


def _face_only_facecam(tmp_path, video_dir):
    """Localise la webcam sur le seul visage (aucun bord reel) puis renvoie sa
    periode : edge_reason non nul."""
    write_keyframes(video_dir, pattern(20, 20))
    path, _ = detect(tmp_path)
    period = period0(path)
    assert period["edge_reason"]
    return period["facecam"]


def test_face_only_facecam_with_a_face_in_every_clip_frame_stays_stream(tmp_path, video_dir):
    _face_only_facecam(tmp_path, video_dir)
    _write_noise_keyframes(video_dir, pattern(20, 20))
    out, factory = run_stream(tmp_path, stream_variant="split")
    assert load(out)["layout"] == "stream_split"
    assert all(d.closed for d in factory.detectors)


def test_face_only_facecam_without_webcam_in_the_scene_goes_letterbox(tmp_path, video_dir, caplog):
    # scene sans webcam : le "rectangle" montre l'interface du jeu (contenu vivant, aucun visage)
    _face_only_facecam(tmp_path, video_dir)
    _write_noise_keyframes(video_dir, pattern(20, 0))
    with caplog.at_level("INFO", logger="clipper.reframe"):
        out, factory = run_stream(tmp_path, stream_variant="split")
    data = load(out)
    assert data["layout"] == "letterbox"
    assert "visage" in data["layout_reason"] and "0/20" in data["layout_reason"]
    assert data["layout_reason"] in caplog.text
    assert all(d.closed for d in factory.detectors)


def test_face_only_facecam_moved_during_the_clip_goes_letterbox(tmp_path, video_dir):
    # webcam deplacee (ecran de pause) : le visage est ailleurs, hors du rectangle localise
    _face_only_facecam(tmp_path, video_dir)
    moved = (1500, 700, 1630, 850)
    _write_noise_keyframes(video_dir, pattern(20, 20, box=moved))
    out, _ = run_stream(tmp_path, stream_variant="split")
    assert load(out)["layout"] == "letterbox"


def test_face_only_facecam_visible_on_a_majority_of_frames_stays_stream(tmp_path, video_dir):
    _face_only_facecam(tmp_path, video_dir)
    _write_noise_keyframes(video_dir, pattern(20, 13))  # 65 % >= facecam_clip_face_min_share
    out, _ = run_stream(tmp_path)
    assert load(out)["layout"] == "stream"


def test_face_min_share_is_a_setting(tmp_path, video_dir):
    from clipper.reframe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["facecam_clip_face_min_share"] == 0.5
    _face_only_facecam(tmp_path, video_dir)
    _write_noise_keyframes(video_dir, pattern(20, 13))
    out, _ = run_stream(tmp_path, facecam_clip_face_min_share=0.8)
    assert load(out)["layout"] == "letterbox"


def test_clip_facecam_face_only_without_detector_is_an_explicit_error(tmp_path):
    from clipper import reframe

    facecam = _offset_facecam(4)
    facecam["edge_reason"] = "bords introuvables"
    with pytest.raises(reframe.ReframeError, match="detecteur"):
        reframe._clip_facecam(
            facecam, 0.0, 3.0, dict(reframe.CONFIG_DEFAULTS), tmp_path, image_reader=_offset_reader(False)
        )


# --------------------------------------------------------------------------
# TASK-495c : un cadre sans aucun visage (bannière de sponsor animée) n'est
# pas retenu comme webcam quand un candidat visage stable existe.
# --------------------------------------------------------------------------


class _FaceInFull:
    """Detecteur : un visage fixe sur l'image entiere (pas sur les vignettes de coin)."""

    def __init__(self, box):
        self.box = box

    def detect(self, image):
        return [(*self.box, 0.9)] if image.shape[1] == 1280 else []


def test_frame_candidate_counts_the_keyframes_with_a_face_inside_it(monkeypatch):
    boxes = [(40, 40, 400, 330), (700, 400, 1000, 600)]  # bannière sans visage, cadre avec visage
    reframe, images, settings = _candidate_setup(monkeypatch, boxes)

    candidates, _ = reframe._period_candidates(images, _FaceInFull((780, 440, 900, 560)), settings)

    by_y = {c["rect"]["y"] < 300: c for c in candidates}
    assert by_y[True]["kind"] == "cadre" and by_y[True]["face_support"] == 0
    assert by_y[False]["face_support"] == len(images)


def test_prompt_tells_claude_when_a_frame_candidate_has_no_face(monkeypatch):
    reframe, images, settings = _candidate_setup(monkeypatch, [(40, 40, 400, 330)])

    candidates, _ = reframe._period_candidates(images, _NoFaces(), settings)
    prompt = reframe._facecam_prompt(candidates, 0, 60)

    assert "aucun visage" in prompt


def _sponsor_banner_period(monkeypatch, video_dir, *, banner_face=0, extra_face=False):
    """Une periode ou le candidat 1 est un cadre (bannière) et le 2 le vrai visage."""
    from clipper import reframe

    write_timeline(video_dir, every(10.0, 120, "game"))
    real = reframe._period_candidates

    def crafted(images, detector, settings):
        candidates, rejected = real(images, detector, settings)
        face = next(c for c in candidates if c["face_support"])
        banner = dict(face, id=1, kind="cadre", rect={"x": 10, "y": 10, "w": 216, "h": 154},
                      face_support=banner_face, edge_reason=None, face=None)
        face = dict(face, id=2)
        out = [banner, face]
        if extra_face:
            out.append(dict(face, id=3, rect={"x": 1500, "y": 800, "w": 300, "h": 213}))
        return out, rejected

    monkeypatch.setattr(reframe, "_period_candidates", crafted)


def test_a_faceless_frame_is_replaced_by_the_stable_face_candidate(tmp_path, video_dir, monkeypatch, caplog):
    _sponsor_banner_period(monkeypatch, video_dir)
    with caplog.at_level("WARNING"):
        path, _ = run_detect(tmp_path, [webcam_answer(1, "cadre present partout")])

    [period] = load(path)["periods"]
    assert contains(period["facecam"], STREAM_FACE)
    assert period["candidate_rect"] != {"x": 10, "y": 10, "w": 216, "h": 154}
    assert period["answer"]["webcam"] == 1  # reponse de Claude gardee telle quelle
    assert period["override"]["from"] == 1 and period["override"]["to"] == 2
    assert "aucun visage" in caplog.text


def test_a_faceless_frame_with_two_stable_face_candidates_is_an_explicit_error(tmp_path, video_dir, monkeypatch):
    from clipper.reframe import ReframeError

    _sponsor_banner_period(monkeypatch, video_dir, extra_face=True)
    with pytest.raises(ReframeError, match="plusieurs"):
        run_detect(tmp_path, [webcam_answer(1)])
    assert not (tmp_path / "workspace" / VIDEO_ID / "facecam.json").exists()


def test_a_frame_with_a_face_is_kept_even_if_a_face_candidate_exists(tmp_path, video_dir, monkeypatch):
    _sponsor_banner_period(monkeypatch, video_dir, banner_face=100)
    path, _ = run_detect(tmp_path, [webcam_answer(1)])

    [period] = load(path)["periods"]
    assert period["candidate_rect"] == {"x": 10, "y": 10, "w": 216, "h": 154}
    assert period.get("override") is None


def test_a_faceless_frame_stays_the_webcam_when_no_stable_face_candidate_exists(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "live"))
    path, _ = run_detect(tmp_path, [webcam_answer(1)])

    [period] = load(path)["periods"]
    assert contains(period["facecam"], STREAM_PANEL, eps=3)
    assert period.get("override") is None


def test_face_stable_share_is_a_setting():
    from clipper.reframe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["facecam_face_stable_share"] == 0.8


# --------------------------------------------------------------------------
# TASK-a769 : Claude voit le contenu de chaque candidat (vignettes, numeros
# hors du rectangle) et « aucune webcam » contredit par un visage stable n'est
# pas accepte en silence.
# --------------------------------------------------------------------------


def _null_period(monkeypatch, video_dir, *, face_support=None, extra_face=False):
    """Candidat 1 : cadre (banniere), 2 : visage ; face_support force le visage du 2."""
    from clipper import reframe

    write_timeline(video_dir, every(10.0, 120, "game"))
    real = reframe._period_candidates

    def crafted(images, detector, settings):
        candidates, rejected = real(images, detector, settings)
        face = next(c for c in candidates if c["face_support"])
        banner = dict(face, id=1, kind="cadre", rect={"x": 10, "y": 10, "w": 216, "h": 154},
                      face_support=0, edge_reason=None, face=None)
        face = dict(face, id=2)
        if face_support is not None:
            face["face_support"] = face_support
        out = [banner, face]
        if extra_face:
            out.append(dict(face, id=3, rect={"x": 1500, "y": 800, "w": 300, "h": 213}))
        return out, rejected

    monkeypatch.setattr(reframe, "_period_candidates", crafted)


def test_null_answer_with_one_stable_face_on_a_single_period_is_trusted_and_logged(
    tmp_path, video_dir, monkeypatch, caplog
):
    """Audit 10/10 image-I1 (TASK-cee612eefa16) : une seule periode ne prouve
    aucune persistance, meme pour un unique candidat visage (mascotte d'un
    ecran d'attente, cas Hellraiser) : Claude fait foi, le candidat ecarte est
    journalise, rien n'est renverse."""
    _null_period(monkeypatch, video_dir)
    with caplog.at_level("WARNING"):
        path, _ = run_detect(tmp_path, [webcam_answer(None, "decor")])

    [period] = load(path)["periods"]
    assert period["facecam"] is None and period.get("override") is None
    assert period["answer"]["webcam"] is None  # reponse de Claude gardee telle quelle
    assert "aucune webcam" in period["reason"] and "une seule periode" in period["reason"]
    assert "[2]" in period["reason"]  # le candidat ecarte est nomme
    assert "ecarte" in caplog.text and "une seule periode" in caplog.text


def test_null_answer_with_two_stable_faces_on_a_single_period_is_trusted_and_logged(
    tmp_path, video_dir, monkeypatch, caplog
):
    """TASK-c5b2e09ab02e : une seule periode ne prouve aucune persistance (ecran
    d'attente dessine dont les nuages passent pour des visages) : Claude fait foi,
    rien ne bloque la video, la decision est journalisee."""
    _null_period(monkeypatch, video_dir, extra_face=True)
    with caplog.at_level("WARNING"):
        path, _ = run_detect(tmp_path, [webcam_answer(None, "ecran d'attente dessine")])

    [period] = load(path)["periods"]
    assert period["facecam"] is None and period.get("override") is None
    assert "aucune webcam" in period["reason"] and "une seule periode" in period["reason"]
    assert "ecarte" in caplog.text and "une seule periode" in caplog.text


def test_null_answer_with_two_persistent_faces_over_several_periods_is_an_explicit_error(
    tmp_path, video_dir, monkeypatch
):
    from clipper.reframe import ReframeError

    twice = [MENUS[0], MENUS[0]]  # memes rectangles dans les deux periodes : persistants
    _menu_faces_period(monkeypatch, video_dir, menus_per_period=[twice[0][:1], twice[1][:1]])
    with pytest.raises(ReframeError, match="plusieurs"):
        run_detect(tmp_path, [webcam_answer(None)] * 2)
    assert not (tmp_path / "workspace" / VIDEO_ID / "facecam.json").exists()


def test_null_answer_without_a_stable_face_stays_letterbox(tmp_path, video_dir, monkeypatch):
    _null_period(monkeypatch, video_dir, face_support=1)  # visage vu sur 1 image : pas stable
    path, _ = run_detect(tmp_path, [webcam_answer(None)])

    [period] = load(path)["periods"]
    assert period["facecam"] is None and period.get("override") is None
    assert "aucune webcam" in period["reason"]


def test_null_answer_with_no_candidate_at_all_stays_letterbox(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "live"))
    path, _ = run_detect(tmp_path, [webcam_answer(None)])
    [period] = load(path)["periods"]
    assert period.get("override") is None


def test_board_numbers_are_drawn_outside_the_candidate_rectangle(tmp_path):
    from clipper import reframe

    settings = dict(reframe.CONFIG_DEFAULTS)
    image = np.full((720, 1280, 3), 90, dtype=np.uint8)
    rect = {"x": 100, "y": 400, "w": 300, "h": 214}
    candidates = [{"id": 1, "rect": rect}]
    path = tmp_path / "board.jpg"
    reframe._draw_board([image], [0.0], candidates, path, {**settings, "jpeg_quality": 100})

    board = cv2.imread(str(path))
    scale = int(settings["facecam_board_tile_width"]) / 1280
    x0, y0 = round(rect["x"] * scale), round(rect["y"] * scale)
    x1, y1 = round((rect["x"] + rect["w"]) * scale), round((rect["y"] + rect["h"]) * scale)
    inner = board[y0 + 6:y1 - 5, x0 + 6:x1 - 5].astype(int)
    assert np.abs(inner - 90).max() <= 12  # contenu intact, ni chiffre ni trait dedans


def test_zoom_sheet_has_one_row_per_candidate_with_its_content(tmp_path):
    from clipper import reframe

    settings = dict(reframe.CONFIG_DEFAULTS)
    image = np.full((720, 1280, 3), 40, dtype=np.uint8)
    image[420:600, 120:380] = (0, 255, 0)  # contenu du candidat 1 : vert vif
    candidates = [
        {"id": 1, "rect": {"x": 100, "y": 400, "w": 300, "h": 214}},
        {"id": 2, "rect": {"x": 900, "y": 50, "w": 300, "h": 214}},
    ]
    path = tmp_path / "zoom.jpg"
    reframe._draw_zoom([image, image, image], candidates, path, settings)

    sheet = cv2.imread(str(path))
    rows = len(candidates)
    assert sheet.shape[0] % rows == 0
    row_h = sheet.shape[0] // rows
    assert (sheet[:row_h, :, 1].astype(int) - sheet[:row_h, :, 0]).max() > 150  # vert dans la ligne 1
    assert (sheet[row_h:, 80:, 1].astype(int) - sheet[row_h:, 80:, 0]).max() < 60  # pas dans la ligne 2


def test_prompt_states_the_face_count_of_face_candidates_explicitly():
    from clipper import reframe

    candidates = [
        {"id": 6, "kind": "visage", "support": 23, "face_support": 23},
        {"id": 1, "kind": "cadre", "support": 24, "face_support": 0},
    ]
    prompt = reframe._facecam_prompt(candidates, 0, 60)
    assert "visage detecte sur 23 image(s) cle(s)" in prompt
    assert "aucun visage" in prompt


def test_claude_receives_the_board_and_the_zoom_sheet(tmp_path, video_dir):
    write_timeline(video_dir, every(10.0, 120, "live"))
    _, fake = run_detect(tmp_path, [webcam_answer(1)])

    names = [p.name for p in fake.calls[0].images]
    assert names == ["period_0.jpg", "period_0_zoom.jpg"]


# --------------------------------------------------------------------------
# TASK-0cb1 : le visage du controle par clip est cherche dans le recadrage
# agrandi du rectangle, pas sur l'image entiere (mesure reelle v2894178473 :
# 2/41 sur l'image entiere, 39/41 sur le recadrage).
# --------------------------------------------------------------------------


class _SizeSensitiveDetector:
    """Detecteur factice courte portee : ne voit un visage (pixels clairs)
    que s'il fait au moins 15 % de la hauteur de l'image qu'on lui donne."""

    def __init__(self):
        self.closed = False
        self.shapes: list[tuple[int, ...]] = []

    def detect(self, frame):
        assert not self.closed
        self.shapes.append(frame.shape)
        x, y, w, h = cv2.boundingRect((frame[:, :, 0] > 200).astype(np.uint8))
        if not (w and h) or h < 0.15 * frame.shape[0]:
            return []
        return [(float(x), float(y), float(x + w), float(y + h), 0.9)]


_CROP_RECT = {"x": 250, "y": 830, "w": 264, "h": 188}


def _crop_facecam(n=10):
    return {
        "index": 0,
        "facecam": dict(_CROP_RECT),
        "reason": None,
        "edge_reason": "bords introuvables : rectangle centre sur le visage conserve",
        "keyframes": [{"timecode": float(i), "path": f"k{i}.jpg"} for i in range(n)],
    }


def _crop_reader(face_box):
    def read(path: str) -> np.ndarray:
        image = coarse_noise(np.random.default_rng(int(Path(path).stem[1:])), 40, 120, 1080, 1920)
        if face_box is not None:
            x0, y0, x1, y1 = face_box
            image[y0:y1, x0:x1] = 255
        return image

    return read


def test_clip_facecam_finds_a_small_face_through_the_enlarged_crop(tmp_path):
    from clipper import reframe

    face = (350, 860, 410, 940)  # 60x80 px sur 1080 : 7 % de l'image, 43 % du rectangle
    detector = _SizeSensitiveDetector()
    assert detector.detect(_crop_reader(face)("k0.jpg")) == []  # image entiere : rate
    rect, reason = reframe._clip_facecam(
        _crop_facecam(), 0.0, 9.0, dict(reframe.CONFIG_DEFAULTS), tmp_path,
        image_reader=_crop_reader(face), detector=detector,
    )
    assert rect == _CROP_RECT and reason is None
    assert any(shape[0] < 1080 for shape in detector.shapes)  # le recadrage a ete passe


def test_clip_facecam_crop_without_a_face_still_goes_letterbox(tmp_path):
    from clipper import reframe

    rect, reason = reframe._clip_facecam(
        _crop_facecam(), 0.0, 9.0, dict(reframe.CONFIG_DEFAULTS), tmp_path,
        image_reader=_crop_reader(None), detector=_SizeSensitiveDetector(),
    )
    assert rect is None and "sans visage" in reason


def test_clip_facecam_face_outside_the_rect_margin_is_not_found(tmp_path):
    from clipper import reframe

    far = (1400, 300, 1560, 520)  # grand visage ailleurs (webcam deplacee)
    rect, reason = reframe._clip_facecam(
        _crop_facecam(), 0.0, 9.0, dict(reframe.CONFIG_DEFAULTS), tmp_path,
        image_reader=_crop_reader(far), detector=_SizeSensitiveDetector(),
    )
    assert rect is None and "sans visage" in reason


def test_clip_facecam_crop_settings_exist():
    from clipper.reframe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["facecam_clip_face_margin"] == 0.25
    assert CONFIG_DEFAULTS["facecam_clip_face_crop_height"] == 720


def test_clip_facecam_face_cut_by_the_rect_edge_goes_letterbox(tmp_path):
    from clipper import reframe

    # webcam deplacee : le visage depasse du rectangle (60 % seulement dedans)
    cut = (440, 860, 540, 960)  # x 440..540, le rectangle s'arrete a 514
    rect, reason = reframe._clip_facecam(
        _crop_facecam(), 0.0, 9.0, dict(reframe.CONFIG_DEFAULTS), tmp_path,
        image_reader=_crop_reader(cut), detector=_SizeSensitiveDetector(),
    )
    assert rect is None and "sans visage" in reason


def test_clip_facecam_min_inside_is_a_setting():
    from clipper.reframe import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["facecam_clip_face_min_inside"] == 0.9


@pytest.mark.skipif(
    os.environ.get("CLIPPER_REAL_MODELS") != "1"
    or not (Path(os.environ.get("CLIPPER_REAL_WORKSPACE", "workspace")) / "v2894178473" / "facecam.json").exists(),
    reason="vrai mediapipe et workspace/v2894178473 : CLIPPER_REAL_MODELS=1",
)
def test_real_aion_clip_webcam_face_found_in_the_enlarged_crop():
    from clipper import gpu, reframe

    video_dir = Path(os.environ.get("CLIPPER_REAL_WORKSPACE", "workspace")) / "v2894178473"
    data = json.loads((video_dir / "facecam.json").read_text(encoding="utf-8"))
    period = data["periods"][0]
    facecam = {
        "index": period["index"], "facecam": period["facecam"], "reason": None,
        "edge_reason": period.get("edge_reason") or "visage seul", "keyframes": data["keyframes"],
    }
    settings = dict(reframe.CONFIG_DEFAULTS)
    detector = reframe.mediapipe_detector(settings, gpu.get_device())
    try:
        rect, reason = reframe._clip_facecam(facecam, 0.0, 600.0, settings, video_dir, detector=detector)
    finally:
        detector.close()
    assert rect == period["facecam"], reason


# --------------------------------------------------------------------------
# TASK-5979 : « aucune webcam » de Claude n'est ecartee que par un unique
# candidat visage stable ET persistant sur la video ; des visages de menus qui
# n'apparaissent que par moments ne contredisent pas Claude.
# --------------------------------------------------------------------------


def _menu_faces_period(monkeypatch, video_dir, *, menus_per_period, persistent=True):
    """Deux periodes (chat puis jeu). Candidat 1 : visage de la webcam, meme
    rectangle dans les deux periodes si ``persistent`` ; ``menus_per_period`` :
    rectangles de visages de menus ajoutes a chaque periode (differents de l'une
    a l'autre). Tous sont stables dans leur periode."""
    from clipper import reframe

    write_timeline(video_dir, [(10.0 * k, "chat" if k < 60 else "game") for k in range(120)])
    real = reframe._period_candidates
    calls = []

    def crafted(images, detector, settings):
        candidates, rejected = real(images, detector, settings)
        index = len(calls)
        calls.append(index)
        panel = {"x": 700, "y": 350, "w": 540, "h": 384}  # STREAM_PANEL, contient STREAM_FACE
        if index == 0:
            rect = panel if persistent else {"x": 300, "y": 300, "w": 300, "h": 213}
            face = {"kind": "visage", "rect": rect, "support": 24, "face_support": 24,
                    "edge_reason": None, "face": [10.0, 10.0, 50.0, 50.0]}
        else:
            face = dict(candidates[0], rect=panel)
        out = [dict(face, id=1)]
        for n, rect in enumerate(menus_per_period[index], start=2):
            out.append(dict(face, id=n, rect=dict(rect)))
        return out, rejected

    monkeypatch.setattr(reframe, "_period_candidates", crafted)


MENUS = [
    [{"x": 1500, "y": 100 * k, "w": 300, "h": 213} for k in range(1, 4)],
    [{"x": 100 * k, "y": 800, "w": 300, "h": 213} for k in range(1, 4)],
]


def test_null_answer_with_menu_faces_that_do_not_persist_stays_letterbox(
    tmp_path, video_dir, monkeypatch, caplog
):
    _menu_faces_period(monkeypatch, video_dir, menus_per_period=[MENUS[0], MENUS[1]], persistent=False)
    with caplog.at_level("WARNING"):
        path, _ = run_detect(tmp_path, [webcam_answer(None, "personnages de menus")] * 2)

    for period in load(path)["periods"]:
        assert period["facecam"] is None and period.get("override") is None
        assert "aucune webcam" in period["reason"]
    assert "ecarte" in caplog.text


def test_null_answer_keeps_the_one_persistent_face_among_non_persistent_ones(tmp_path, video_dir, monkeypatch):
    _menu_faces_period(monkeypatch, video_dir, menus_per_period=[MENUS[0], MENUS[1]])
    path, _ = run_detect(tmp_path, [webcam_answer(None, "decor")] * 2)

    periods = load(path)["periods"]
    assert [p["override"]["to"] for p in periods] == [1, 1]
    assert contains(periods[1]["facecam"], STREAM_FACE)


# --------------------------------------------------------------------------
# Audit 10/10, lot G (TASK-cee612eefa16) : reframe, periode unique.
# --------------------------------------------------------------------------

_CLOUD = {"id": 4, "kind": "visage", "rect": {"x": 100, "y": 100, "w": 200, "h": 142}, "support": 24,
          "face_support": 24, "edge_reason": "bords introuvables", "face": [150, 130, 250, 220]}
_HUD = {"id": 1, "kind": "cadre", "rect": {"x": 1500, "y": 800, "w": 300, "h": 213}, "support": 24,
        "face_support": 0, "edge_reason": None, "face": None}


def test_stable_face_over_null_never_overrides_on_a_single_period():
    """image-I1 : un unique visage stable sur une periode unique ne contredit
    pas Claude (il est rendu comme ecarte) ; sur plusieurs periodes, la
    persistance reste prouvable et l'override possible."""
    from clipper import reframe

    s = dict(reframe.CONFIG_DEFAULTS)
    better, dropped = reframe._stable_face_over_null([_HUD, _CLOUD], s, periods=[[_HUD, _CLOUD]])
    assert better is None and [c["id"] for c in dropped] == [4]
    better, dropped = reframe._stable_face_over_null([_HUD, _CLOUD], s, periods=())
    assert better is None and [c["id"] for c in dropped] == [4]
    better, dropped = reframe._stable_face_over_null([_HUD, _CLOUD], s, periods=[[_HUD, _CLOUD], [_CLOUD]])
    assert better is _CLOUD and dropped == []


def _stream_settings():
    from clipper import reframe

    return dict(reframe.CONFIG_DEFAULTS)


def test_a_small_rounded_rectangle_from_facecam_json_is_not_refused_by_reframe_stream(tmp_path):
    """image-M2 : 112x78 (arrondi pair de chaque cote par _size_camera_rect,
    petite webcam recalee) a un ratio a 2,1 % du panneau 1080x768 : le controle
    de _reframe_stream tolere l'erreur d'arrondi au lieu d'un 2 % fixe."""
    from clipper import reframe

    s = _stream_settings()
    facecam = {"source": {"width": 1920, "height": 1080}}
    rect = {"x": 100, "y": 100, "w": 112, "h": 78}
    path = reframe._reframe_stream("v", "01", 0.0, 10.0, tmp_path / "plan.json", facecam, rect, s)
    plan = load(path)
    assert plan["facecam"] == rect
    [camera] = [p for p in plan["plans"][0]["panels"] if p["name"] == "camera"]
    assert camera["rects"][0]["w"] == 112 and camera["rects"][0]["h"] == 78


def test_a_rectangle_sized_for_another_camera_panel_is_still_refused_by_reframe_stream(tmp_path):
    """La tolerance d'arrondi ne couvre pas un vrai changement de panneau :
    540x480 (ratio 1,125, stream_camera_ratio = 0,5) contre 1080x768."""
    from clipper import reframe
    from clipper.reframe import ReframeError

    s = _stream_settings()
    facecam = {"source": {"width": 1920, "height": 1080}}
    with pytest.raises(ReframeError, match="autre panneau camera"):
        reframe._reframe_stream("v", "01", 0.0, 10.0, tmp_path / "plan.json", facecam,
                                {"x": 100, "y": 100, "w": 540, "h": 480}, s)


def test_every_rectangle_sized_for_the_camera_panel_passes_the_stream_ratio_check():
    """image-M2 : aucun rectangle produit par _size_camera_rect (quelle que soit
    la taille de la zone a contenir) n'est refuse par le controle de ratio de
    _reframe_stream (audit : 18 tailles refusees, 112x78 la plus grande)."""
    from clipper import reframe

    s = _stream_settings()
    cam_w, cam_h, _ = reframe._camera_size(s)
    aspect = cam_w / cam_h
    refused = set()
    for bh in range(16, 400):
        for bw in (bh * aspect, bh * aspect * 1.3, bh * aspect * 0.8):
            rect, _ = reframe._size_camera_rect(960, 540, bw, bh, 1920, 1080, s)
            if rect is None:
                continue
            x, y, w, h = rect
            try:
                reframe._check_camera_rect_ratio({"x": x, "y": y, "w": w, "h": h}, s)
            except reframe.ReframeError:
                refused.add((w, h))
    assert refused == set()


def test_size_camera_rect_derives_the_height_from_the_rounded_width():
    """image-M2 : l'arrondi pair de w et de h est fait ensemble (h derive de w),
    pas separement : le rectangle garde le format du panneau camera a l'arrondi pres."""
    from clipper import reframe

    s = _stream_settings()
    cam_w, cam_h, _ = reframe._camera_size(s)
    aspect = cam_w / cam_h
    rect, _ = reframe._size_camera_rect(960, 540, 111.5, 79.3, 1920, 1080, s)
    x, y, w, h = rect
    assert w == 112 and h == reframe._even(w / aspect) == 80


def test_force_recompute_empties_the_facecam_boards_folder_first(tmp_path, video_dir):
    """image-M5 : les planches period_N.jpg d'un ancien calcul (plus de
    periodes) ne survivent pas a un recalcul : facecam/ ne contient que les
    planches referencees par facecam.json."""
    write_timeline(video_dir, every(10.0, 120, "live"))
    boards = video_dir / "facecam"
    boards.mkdir(parents=True)
    (boards / "period_7.jpg").write_bytes(b"ancien")
    (boards / "period_7_zoom.jpg").write_bytes(b"ancien")

    run_detect(tmp_path, [webcam_answer(1)])

    assert sorted(p.name for p in boards.iterdir()) == ["period_0.jpg", "period_0_zoom.jpg"]
