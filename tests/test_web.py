"""Tests de clipper.web (TASK-634e).

Routes testees avec le TestClient FastAPI et un pipeline simule (jamais le
vrai pipeline.run/render/decide) : voir clipper/web/app.py pour le contrat.
Aucun test n'utilise le reseau ni un vrai LLM.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper.config import Config
from clipper.web import app as web_app
from clipper.web import create_app

VIDEO_ID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"



def _node_run(script: str, *args: str) -> str:
    """Execute un script node depuis un fichier temporaire : ``node -e <script>`` depasse la longueur
    maximale d'une ligne de commande sous Windows (WinError 206) quand le script embarque un ecran entier.
    ``process.argv[1]`` designe le premier argument, comme avec ``-e``."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write("process.argv.splice(1, 1);" + chr(10) + script)
        path = handle.name
    try:
        return subprocess.run(["node", path, *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout
    finally:
        Path(path).unlink(missing_ok=True)

def _busy_worker(tmp_path) -> None:
    """Un worker vivant qui traite des videos (battement « busy », pid de ce processus) : leurs etapes « running »
    sans entree dans la file ne sont pas orphelines (TASK-bdd5)."""
    _write_json(tmp_path / "state" / "worker.json",
                {"pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat(), "busy": True})


def make_config(tmp_path) -> Config:
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output")


def client(tmp_path) -> TestClient:
    return TestClient(create_app(config=make_config(tmp_path)))


# --------------------------------------------------------------------------
# B : page statique
# --------------------------------------------------------------------------


def test_serve_static_index_page(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert '<html lang="fr"' in resp.text
    assert 'id="app"' in resp.text


# --------------------------------------------------------------------------
# C : POST /api/videos est un alias de POST /api/queue sans chaine (ADR-4f6e
# §1 : jamais pipeline.run dans ce processus, worker.enqueue a la place)
# --------------------------------------------------------------------------


def test_submit_url_enqueues_via_worker(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    calls = []

    def fake_enqueue(url, channel, action, force_steps, *, config=None):
        calls.append((url, channel, action, force_steps))
        return {"id": "e1", "video_id": VIDEO_ID, "url": url, "channel": channel,
                "action": action, "force_steps": force_steps or [], "status": "waiting"}

    monkeypatch.setattr(worker, "enqueue", fake_enqueue)

    resp = client(tmp_path).post("/api/videos", json={"url": URL})

    assert resp.status_code == 202
    assert resp.json()["video_id"] == VIDEO_ID
    assert calls == [(URL, None, "run", None)]


def test_submit_missing_url_is_a_validation_error(tmp_path, isolated_cwd):
    resp = client(tmp_path).post("/api/videos", json={})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# D : GET /api/videos liste workspace/*/pipeline.json
# --------------------------------------------------------------------------


def _write_state(tmp_path, video_id, **overrides):
    from clipper import pipeline

    state = pipeline.new_state(video_id, f"https://www.youtube.com/watch?v={video_id}", "review")
    state.update(overrides)
    video_dir = tmp_path / "workspace" / video_id
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "pipeline.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def test_list_videos_reads_pipeline_json_from_workspace(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa", status="running")
    _write_state(tmp_path, "bbbbbbbbbbb", status="done")

    resp = client(tmp_path).get("/api/videos")

    assert resp.status_code == 200
    ids = [v["video_id"] for v in resp.json()]
    assert sorted(ids) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_list_videos_empty_workspace_is_an_empty_list(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/api/videos")
    assert resp.status_code == 200
    assert resp.json() == []


# --------------------------------------------------------------------------
# E : GET /api/videos/{id} via pipeline.load_state
# --------------------------------------------------------------------------


def test_get_video_detail_uses_pipeline_load_state(tmp_path, isolated_cwd, monkeypatch):
    _busy_worker(tmp_path)
    from clipper import pipeline

    state = {"video_id": VIDEO_ID, "status": "running", "steps": {}}

    def fake_load_state(video_id, *, config=None):
        assert video_id == VIDEO_ID
        return state

    monkeypatch.setattr(pipeline, "load_state", fake_load_state)

    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert {k: body[k] for k in state} == state
    assert body["clips"] == [] and body["awaiting"] == [] and body["durations"] == {}


def test_get_video_detail_unknown_video_is_404(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline

    def fake_load_state(video_id, *, config=None):
        raise pipeline.PipelineError(f"aucun etat pour la video {video_id}")

    monkeypatch.setattr(pipeline, "load_state", fake_load_state)

    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}")

    assert resp.status_code == 404
    assert VIDEO_ID in resp.json()["detail"]


# --------------------------------------------------------------------------
# F : GET /api/videos/{id}/moments fusionne moments.json + parts.json + review.json
# --------------------------------------------------------------------------


MOMENTS_JSON = {
    "video_id": VIDEO_ID,
    "moments": [
        {"id": 0, "start": 2.0, "end": 26.0, "duration": 24.0, "format": "single", "parts": [],
         "scores": {"hook": 9}, "bonus": {"total": 1.0}, "final_score": 91.0,
         "justification": "Annonce forte", "hook_text": "GTA six arrive vraiment."},
        {"id": 1, "start": 40.0, "end": 60.0, "duration": 20.0, "format": "single", "parts": [],
         "scores": {"hook": 7}, "bonus": {"total": 0.0}, "final_score": 70.0,
         "justification": "Correct", "hook_text": "Autre moment."},
    ],
    "rejected": [],
}
PARTS_JSON = {
    "video_id": VIDEO_ID,
    "moments": [
        {"id": 0, "start": 2.0, "end": 26.0, "duration": 24.0, "format": "single", "parts_total": 1,
         "proposed_cuts": [], "parts": [{"part": 1, "start": 2.0, "end": 26.0, "duration": 24.0,
                                          "hook_text": "GTA six arrive vraiment.", "suspense": None}]},
        {"id": 1, "start": 40.0, "end": 60.0, "duration": 20.0, "format": "single", "parts_total": 1,
         "proposed_cuts": [], "parts": [{"part": 1, "start": 40.0, "end": 60.0, "duration": 20.0,
                                          "hook_text": "Autre moment.", "suspense": None}]},
    ],
    "rejected": [],
}


def _write_moments_fixtures(tmp_path, review=None):
    video_dir = tmp_path / "workspace" / VIDEO_ID
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "moments.json").write_text(json.dumps(MOMENTS_JSON), encoding="utf-8")
    (video_dir / "parts.json").write_text(json.dumps(PARTS_JSON), encoding="utf-8")
    if review is not None:
        (video_dir / "review.json").write_text(json.dumps(review), encoding="utf-8")


def test_list_moments_merges_score_justification_and_preview(tmp_path, isolated_cwd):
    _write_moments_fixtures(tmp_path)

    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/moments")

    assert resp.status_code == 200
    moments = resp.json()
    assert [m["id"] for m in moments] == [0, 1]
    first = moments[0]
    assert first["start"] == 2.0 and first["end"] == 26.0
    assert first["score"] == 91.0
    assert first["justification"] == "Annonce forte"
    assert first["hook_text"] == "GTA six arrive vraiment."
    assert first["decision"] is None
    assert first["preview_url"] == f"/media/source/{VIDEO_ID}"


JURY_VERDICT = {
    "score": 71.0, "confidence": 60, "veto": None, "debated": True,
    "trace": {"rounds": [
        {"round": 1, "judges": {
            "retention": {"scores": {"hook": 9}, "score": 90.0, "argument": "a", "confidence": 90},
            "avocat": {"scores": {"hook": 5}, "score": 50.0, "argument": "b", "confidence": 30}}},
        {"round": 2, "judges": {
            "avocat": {"scores": {"hook": 6}, "score": 60.0, "argument": "c", "confidence": 55}}},
    ], "revisions": [], "dissent": []},
}


def _with_jury_moments(tmp_path):
    _write_moments_fixtures(tmp_path)
    data = json.loads(json.dumps(MOMENTS_JSON))
    data["moments"][0]["jury"] = JURY_VERDICT
    (tmp_path / "workspace" / VIDEO_ID / "moments.json").write_text(json.dumps(data), encoding="utf-8")


def test_list_moments_exposes_the_jury_confidence_aggregate_and_per_judge(tmp_path, isolated_cwd):
    _with_jury_moments(tmp_path)

    moments = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/moments").json()

    first, second = moments
    assert first["confidence"] == 60
    # derniere confiance de chaque juge : tour 2 si le juge a reevalue, sinon tour 1
    assert first["judge_confidences"] == {"retention": 90, "avocat": 55}
    assert second["confidence"] is None and second["judge_confidences"] is None


def test_get_clips_exposes_the_jury_confidence_of_the_clips_moment(tmp_path, isolated_cwd):
    video_dir = tmp_path / "workspace" / CLIPS_VIDEO
    video_dir.mkdir(parents=True)
    moments = {"video_id": CLIPS_VIDEO, "moments": [
        {"id": 0, "final_score": 80.0, "jury": {**JURY_VERDICT, "confidence": 72}},
        {"id": 1, "final_score": 70.0},
    ]}
    (video_dir / "moments.json").write_text(json.dumps(moments), encoding="utf-8")
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01", moment_id=0))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("02", moment_id=1))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("03"))

    clips = {c["clip_id"]: c for c in client(tmp_path).get("/api/clips", params={"video_id": CLIPS_VIDEO}).json()}

    assert clips["01"]["jury_confidence"] == 72
    assert clips["01"]["jury_judge_confidences"] == {"retention": 90, "avocat": 55}
    assert clips["02"]["jury_confidence"] is None  # moment sans jury : pas de valeur inventee
    assert clips["03"]["jury_confidence"] is None  # sidecar sans moment_id


def test_get_clips_without_moments_json_has_no_jury_confidence(tmp_path, isolated_cwd):
    _clips_setup(tmp_path)

    clips = client(tmp_path).get("/api/clips", params={"video_id": CLIPS_VIDEO}).json()

    assert all(c["jury_confidence"] is None for c in clips)


def test_review_screen_shows_the_jury_confidence():
    js = _review_js()

    assert "confidence" in js and "judge_confidences" in js
    assert "Confiance" in js


def test_clip_sheet_shows_the_jury_confidence():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")

    assert "jury_confidence" in js and "jury_judge_confidences" in js
    assert "Confiance du jury" in js


def test_list_moments_reports_existing_decisions(tmp_path, isolated_cwd):
    review = {"decisions": {"0": {"decision": "adjusted", "start": 4.0, "end": 26.0,
                                   "comment": "debut plus net", "at": "2026-01-01T00:00:00+00:00"}}}
    _write_moments_fixtures(tmp_path, review=review)

    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/moments")

    moments = {m["id"]: m for m in resp.json()}
    assert moments[0]["decision"] == review["decisions"]["0"]
    assert moments[1]["decision"] is None


def test_list_moments_unknown_video_is_404(tmp_path, isolated_cwd):
    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/moments")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# G : POST /api/videos/{id}/moments/{mid}/decide appelle pipeline.decide
# --------------------------------------------------------------------------


def test_decide_calls_pipeline_decide(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline

    calls = []

    def fake_decide(video_id, moment_id, decision, *, start=None, end=None, comment=None, config=None):
        calls.append((video_id, moment_id, decision, start, end, comment))
        return {"video_id": video_id, "decision": decision}

    monkeypatch.setattr(pipeline, "decide", fake_decide)

    resp = client(tmp_path).post(
        f"/api/videos/{VIDEO_ID}/moments/0/decide",
        json={"decision": "adjusted", "start": 4.0, "end": 26.0, "comment": "debut plus net"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"video_id": VIDEO_ID, "decision": "adjusted"}
    assert calls == [(VIDEO_ID, 0, "adjusted", 4.0, 26.0, "debut plus net")]


def test_decide_accepted_needs_no_bounds(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline

    calls = []
    monkeypatch.setattr(
        pipeline, "decide",
        lambda video_id, moment_id, decision, *, start=None, end=None, comment=None, config=None:
            calls.append((video_id, moment_id, decision, start, end, comment)) or {"decision": decision},
    )

    resp = client(tmp_path).post(f"/api/videos/{VIDEO_ID}/moments/0/decide", json={"decision": "accepted"})

    assert resp.status_code == 200
    assert calls == [(VIDEO_ID, 0, "accepted", None, None, None)]


def test_decide_invalid_decision_is_400(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline

    def fake_decide(video_id, moment_id, decision, *, start=None, end=None, comment=None, config=None):
        raise pipeline.PipelineError(f"decision invalide {decision!r}")

    monkeypatch.setattr(pipeline, "decide", fake_decide)

    resp = client(tmp_path).post(f"/api/videos/{VIDEO_ID}/moments/0/decide", json={"decision": "bogus"})

    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


# --------------------------------------------------------------------------
# H : POST /api/videos/{id}/render remet en file (action 'render') via
# worker.enqueue, jamais pipeline.render dans ce processus (ADR-4f6e §1)
# --------------------------------------------------------------------------


def test_render_enqueues_render_action(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    calls = []

    def fake_enqueue(url, channel, action, force_steps, *, config=None):
        calls.append((url, channel, action, force_steps))
        return {"id": "e1", "video_id": VIDEO_ID, "channel": channel, "action": action,
                "force_steps": force_steps or [], "status": "waiting"}

    monkeypatch.setattr(worker, "enqueue", fake_enqueue)

    resp = client(tmp_path).post(f"/api/videos/{VIDEO_ID}/render")

    assert resp.status_code == 202
    assert resp.json()["video_id"] == VIDEO_ID
    assert calls == [(VIDEO_ID, None, "render", None)]


def test_render_passes_the_video_channel(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline, worker

    _write_state(tmp_path, VIDEO_ID, channel="une_chaine")
    calls = []
    monkeypatch.setattr(
        worker, "enqueue",
        lambda url, channel, action, force_steps, *, config=None:
            calls.append((url, channel, action, force_steps)) or
            {"id": "e1", "video_id": VIDEO_ID, "channel": channel, "action": action,
             "force_steps": force_steps or [], "status": "waiting"},
    )

    resp = client(tmp_path).post(f"/api/videos/{VIDEO_ID}/render")

    assert resp.status_code == 202
    assert calls == [(VIDEO_ID, "une_chaine", "render", None)]


# --------------------------------------------------------------------------
# I : GET /api/videos/{id}/clips lit output/<id>/*.json
# --------------------------------------------------------------------------


CLIP_JSON = {
    "video_id": VIDEO_ID, "source_url": URL, "source_title": "GTA 6 : le trailer",
    "clip_id": "03-p2", "part": 2, "parts_total": 2, "start": 2.0, "end": 26.0, "duration": 24.0,
    "language": "fr", "score": 91.0, "scores": {"hook": 9}, "reason": "Annonce forte",
    "hook_text": "GTA six arrive vraiment.", "title": "GTA 6 arrive", "caption": "Il arrive vraiment",
    "hashtags": ["#gta6"], "transcript": "GTA six arrive vraiment.", "layout": "single",
    "qa": {"status": "passed", "issues": []}, "created_at": "2026-01-01T00:00:00+00:00",
}


def test_list_clips_reads_output_json(tmp_path, isolated_cwd):
    out_dir = tmp_path / "output" / VIDEO_ID
    out_dir.mkdir(parents=True)
    (out_dir / f"{CLIP_JSON['clip_id']}.json").write_text(json.dumps(CLIP_JSON), encoding="utf-8")
    (out_dir / f"{CLIP_JSON['clip_id']}.mp4").write_bytes(b"fake-mp4")

    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/clips")

    assert resp.status_code == 200
    clips = resp.json()
    assert len(clips) == 1
    clip = clips[0]
    assert clip["title"] == "GTA 6 arrive"
    assert clip["caption"] == "Il arrive vraiment"
    assert clip["hashtags"] == ["#gta6"]
    assert clip["video_url"] == f"/media/clip/{VIDEO_ID}/{CLIP_JSON['clip_id']}"


def test_list_clips_no_output_yet_is_an_empty_list(tmp_path, isolated_cwd):
    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/clips")
    assert resp.status_code == 200
    assert resp.json() == []


# --------------------------------------------------------------------------
# J : /media/source/{id} et /media/clip/{id}/{clip_id} servent les mp4
# --------------------------------------------------------------------------


def test_media_source_serves_the_source_video(tmp_path, isolated_cwd):
    video_dir = tmp_path / "workspace" / VIDEO_ID
    video_dir.mkdir(parents=True)
    (video_dir / f"{VIDEO_ID}.mp4").write_bytes(b"source-bytes")

    resp = client(tmp_path).get(f"/media/source/{VIDEO_ID}")

    assert resp.status_code == 200
    assert resp.content == b"source-bytes"
    assert resp.headers["content-type"] == "video/mp4"


def test_media_source_missing_file_is_404(tmp_path, isolated_cwd):
    resp = client(tmp_path).get(f"/media/source/{VIDEO_ID}")
    assert resp.status_code == 404


def test_media_source_rejects_path_traversal(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/media/source/..")
    assert resp.status_code == 404


def test_media_clip_serves_the_rendered_clip(tmp_path, isolated_cwd):
    out_dir = tmp_path / "output" / VIDEO_ID
    out_dir.mkdir(parents=True)
    clip_id = "03-p2"
    (out_dir / f"{clip_id}.mp4").write_bytes(b"clip-bytes")

    resp = client(tmp_path).get(f"/media/clip/{VIDEO_ID}/{clip_id}")

    assert resp.status_code == 200
    assert resp.content == b"clip-bytes"
    assert resp.headers["content-type"] == "video/mp4"


def test_media_clip_rejects_path_traversal(tmp_path, isolated_cwd):
    resp = client(tmp_path).get(f"/media/clip/{VIDEO_ID}/..%2F..%2Fsecret")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# K : aucune logique de traitement dans clipper/web (ADR-09ad)
# --------------------------------------------------------------------------


_ALLOWED_CLIPPER_IMPORTS = {
    "clipper", "clipper.pipeline", "clipper.config", "clipper.web", "clipper.web.app",
    "clipper.channel", "clipper.worker", "clipper.publish", "clipper.watch", "clipper.gpu",
}


def test_web_module_only_imports_pipeline_and_config():
    import ast

    root = Path(__file__).resolve().parent.parent / "clipper" / "web"
    offenders = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue
            for name in names:
                if name and name.startswith("clipper") and name not in _ALLOWED_CLIPPER_IMPORTS:
                    offenders.append(f"{path.name} importe {name}")
    assert offenders == []


# --------------------------------------------------------------------------
# A : 'python -m clipper serve' lance uvicorn sur 127.0.0.1
# --------------------------------------------------------------------------


def test_cli_serve_runs_uvicorn_on_localhost(tmp_path, isolated_cwd, monkeypatch):
    import uvicorn

    from clipper.__main__ import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))

    assert main(["serve"]) == 0

    assert len(calls) == 1
    app, kwargs = calls[0]
    assert app.title == "Clipper"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8000


def test_cli_serve_port_is_configurable(tmp_path, isolated_cwd, monkeypatch):
    import uvicorn

    from clipper.__main__ import main

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))

    assert main(["serve", "--port", "9001"]) == 0

    assert calls[0][1]["host"] == "127.0.0.1"
    assert calls[0][1]["port"] == 9001


def test_logo_is_served_as_standalone_svg(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/static/logo.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    import xml.etree.ElementTree as ET

    root = ET.fromstring(resp.text)
    assert root.tag.endswith("svg")
    assert root.get("viewBox")
    # autonome : aucune ressource ni police externe
    assert "href" not in resp.text and "font" not in resp.text


def test_index_declares_logo_as_icon_and_shows_it_in_the_brand(tmp_path, isolated_cwd):
    html = client(tmp_path).get("/").text
    assert '<link rel="icon" type="image/svg+xml" href="/static/logo.svg">' in html
    brand = html[html.index('<a class="brand"'):html.index("</a>", html.index('<a class="brand"'))]
    assert 'src="/static/logo.svg"' in brand
    assert 'alt=""' in brand
    assert "Clipper" in brand


# --------------------------------------------------------------------------
# L : file de traitement (/api/queue) - SPEC-fc0c §2
# --------------------------------------------------------------------------


def test_queue_post_calls_worker_enqueue(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    calls = []

    def fake_enqueue(url, channel, action, force_steps, *, config=None):
        calls.append((url, channel, action, force_steps))
        return {"id": "e1", "video_id": VIDEO_ID, "url": url, "channel": channel,
                "action": action, "force_steps": force_steps, "status": "waiting"}

    monkeypatch.setattr(worker, "enqueue", fake_enqueue)

    resp = client(tmp_path).post("/api/queue", json={
        "url": URL, "channel": "une_chaine", "action": "run", "force_steps": ["render"],
    })

    assert resp.status_code == 202
    assert resp.json()["video_id"] == VIDEO_ID
    assert calls == [(URL, "une_chaine", "run", ["render"])]


def test_queue_post_duplicate_is_409_with_french_detail(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    def fake_enqueue(url, channel, action, force_steps, *, config=None):
        raise worker.WorkerError(f"deja en file d'attente : {VIDEO_ID} ({action})")

    monkeypatch.setattr(worker, "enqueue", fake_enqueue)

    resp = client(tmp_path).post(
        "/api/queue", json={"url": URL, "channel": None, "action": "run", "force_steps": []}
    )

    assert resp.status_code == 409
    assert "file d'attente" in resp.json()["detail"]


def test_queue_get_lists_entries(tmp_path, isolated_cwd):
    queue_path = tmp_path / "state" / "queue.json"
    queue_path.parent.mkdir(parents=True)
    entries = [{"id": "e1", "video_id": VIDEO_ID, "url": URL, "channel": None, "action": "run",
                "force_steps": [], "enqueued_at": "2026-01-01T00:00:00+00:00", "status": "waiting", "pid": None}]
    queue_path.write_text(json.dumps(entries), encoding="utf-8")

    resp = client(tmp_path).get("/api/queue")

    assert resp.status_code == 200
    assert [{k: v for k, v in e.items() if k != "platform_thumbnail"} for e in resp.json()] == entries


def test_queue_get_empty_is_an_empty_list(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/api/queue")
    assert resp.status_code == 200
    assert resp.json() == []


def test_queue_front_calls_worker_move_to_front(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    calls = []
    monkeypatch.setattr(worker, "move_to_front", lambda video_id, *, config=None: calls.append(video_id))

    resp = client(tmp_path).post(f"/api/queue/{VIDEO_ID}/front")

    assert resp.status_code == 200
    assert calls == [VIDEO_ID]


def test_queue_front_unknown_entry_is_404(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    def fake(video_id, *, config=None):
        raise worker.WorkerError(f"aucune entree en attente pour {video_id!r}")

    monkeypatch.setattr(worker, "move_to_front", fake)

    resp = client(tmp_path).post(f"/api/queue/{VIDEO_ID}/front")

    assert resp.status_code == 404


def test_queue_delete_calls_worker_remove(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    calls = []
    monkeypatch.setattr(worker, "remove", lambda video_id, *, config=None: calls.append(video_id))

    resp = client(tmp_path).delete(f"/api/queue/{VIDEO_ID}")

    assert resp.status_code == 200
    assert calls == [VIDEO_ID]


def test_queue_delete_unknown_entry_is_404(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    def fake(video_id, *, config=None):
        raise worker.WorkerError(f"aucune entree en attente pour {video_id!r}")

    monkeypatch.setattr(worker, "remove", fake)

    resp = client(tmp_path).delete(f"/api/queue/{VIDEO_ID}")

    assert resp.status_code == 404


# --------------------------------------------------------------------------
# M : annulation, relance, journal par video - SPEC-fc0c §2.3, §3.2
# --------------------------------------------------------------------------


def test_cancel_kills_the_running_child_without_failing_the_publication_being_driven(tmp_path, isolated_cwd,
                                                                                      monkeypatch):
    """Revues r-publication I1 / r-comptes 1 : l'API ne construit jamais de Worker (ses reprises de demarrage
    passaient en echec la publication pilotee par le vrai worker) ; l'enfant est arrete par son pid."""
    import sys

    from clipper import pipeline, worker

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write_json(tmp_path / "state" / "queue.json", [{
            "id": "q1", "video_id": VIDEO_ID, "url": URL, "channel": None, "action": "run", "force_steps": [],
            "enqueued_at": "2026-10-03T10:00:00+00:00", "status": "running", "pid": child.pid}])
        pipeline.save_state({**pipeline.new_state(VIDEO_ID, URL, "review"), "status": "running"},
                            config=make_config(tmp_path))
        driven = {**_publish_entry("autrevideo1", "01", "scheduled", "2026-10-03T10:00:00+00:00"),
                  "account": "ab12cd", "in_progress_since": "2026-10-03T10:00:00+00:00"}
        _write_json(tmp_path / "state" / "publish" / "_sans_chaine.json", [driven])

        def refuse(self, *args, **kwargs):
            raise AssertionError("l'API web a construit un Worker")

        monkeypatch.setattr(worker.Worker, "__init__", refuse)

        resp = client(tmp_path).post(f"/api/videos/{VIDEO_ID}/cancel")

        assert resp.status_code == 200, resp.text
        assert child.wait(timeout=10) is not None
    finally:
        if child.poll() is None:
            child.kill()
    publications = json.loads((tmp_path / "state" / "publish" / "_sans_chaine.json").read_text(encoding="utf-8"))
    assert publications == [driven]  # la publication en cours n'est pas passee en echec
    assert json.loads((tmp_path / "state" / "queue.json").read_text(encoding="utf-8")) == []
    state = pipeline.load_state(VIDEO_ID, config=make_config(tmp_path))
    assert (state["status"], state["reason"]) == ("failed", "annulée par l'utilisateur")


def test_cancel_not_running_is_404(tmp_path, isolated_cwd):
    resp = client(tmp_path).post(f"/api/videos/{VIDEO_ID}/cancel")

    assert resp.status_code == 404


def test_retry_enqueues_render_with_force_steps_from_step(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline, worker

    calls = []
    monkeypatch.setattr(
        worker, "enqueue",
        lambda url, channel, action, force_steps, *, config=None:
            calls.append((url, channel, action, force_steps)) or
            {"id": "e1", "video_id": VIDEO_ID, "channel": channel, "action": action,
             "force_steps": force_steps, "status": "waiting"},
    )

    resp = client(tmp_path).post(f"/api/videos/{VIDEO_ID}/retry", json={"from_step": "reframe"})

    assert resp.status_code == 202
    expected_force_steps = list(pipeline.STEPS[pipeline.STEPS.index("reframe"):])
    assert calls == [(VIDEO_ID, None, "render", expected_force_steps)]
    assert "reframe" in resp.json()["force_steps"]


def test_retry_unknown_step_is_400(tmp_path, isolated_cwd):
    resp = client(tmp_path).post(f"/api/videos/{VIDEO_ID}/retry", json={"from_step": "bogus"})
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


def test_video_events_reads_events_jsonl(tmp_path, isolated_cwd):
    video_dir = tmp_path / "workspace" / VIDEO_ID
    video_dir.mkdir(parents=True)
    lines = [
        {"at": "2026-01-01T00:00:00+00:00", "level": "INFO", "step": "download", "message": "demarre"},
        {"at": "2026-01-01T00:01:00+00:00", "level": "INFO", "step": "download", "message": "termine"},
    ]
    (video_dir / "events.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/events")

    assert resp.status_code == 200
    assert resp.json() == lines


def test_video_events_since_filters_older_lines(tmp_path, isolated_cwd):
    video_dir = tmp_path / "workspace" / VIDEO_ID
    video_dir.mkdir(parents=True)
    lines = [
        {"at": "2026-01-01T00:00:00+00:00", "level": "INFO", "step": "download", "message": "demarre"},
        {"at": "2026-01-01T00:01:00+00:00", "level": "INFO", "step": "download", "message": "termine"},
    ]
    (video_dir / "events.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/events", params={"since": "2026-01-01T00:00:00+00:00"})

    assert resp.status_code == 200
    assert resp.json() == [lines[1]]


def test_video_events_no_journal_yet_is_an_empty_list(tmp_path, isolated_cwd):
    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/events")
    assert resp.status_code == 200
    assert resp.json() == []


def test_video_events_rejects_path_traversal(tmp_path, isolated_cwd):
    # "..%2F..%2Fsecret" (comme test_media_clip_rejects_path_traversal) : pas
    # de normalisation cote client httpx (le "/" reste encode), contrairement
    # a un ".." nu qui serait resolu avant l'envoi et, ici, retomberait sur
    # /api/events (flux SSE infini) au lieu d'exercer la validation serveur.
    resp = client(tmp_path).get("/api/videos/..%2F..%2Fsecret/events")
    assert resp.status_code in (400, 404)


def test_cancel_rejects_path_traversal(tmp_path, isolated_cwd):
    resp = client(tmp_path).post("/api/videos/..%2F..%2Fsecret/cancel")
    assert resp.status_code in (400, 404)


# --------------------------------------------------------------------------
# N : GET /api/channels - channel.list_channels (SPEC-fc0c §1)
# --------------------------------------------------------------------------


def test_channels_lists_presets_with_channel_table(tmp_path, isolated_cwd):
    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    (presets_dir / "une_chaine.toml").write_text('[channel]\ndisplay_name = "Une chaine"\n', encoding="utf-8")
    (presets_dir / "sans_channel.toml").write_text('mode = "auto"\n', encoding="utf-8")

    resp = client(tmp_path).get("/api/channels")

    assert resp.status_code == 200
    assert resp.json() == ["une_chaine"]


def test_channels_no_presets_dir_is_an_empty_list(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/api/channels")
    assert resp.status_code == 200
    assert resp.json() == []


# --------------------------------------------------------------------------
# O : GET /api/events - flux SSE par mtime (ADR-4f6e §4)
# --------------------------------------------------------------------------


def _sse_config(tmp_path) -> Config:
    return Config(
        mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={"web": {"host": "127.0.0.1", "port": 8000, "token": "", "sse_poll_interval_s": 0.05}},
    )


def test_events_route_is_declared_as_sse(tmp_path, isolated_cwd):
    # La reponse HTTP infinie de ce flux ne peut pas etre lue de bout en bout
    # ici : le TestClient de ce venv bloque indefiniment sur un corps qui ne
    # se termine jamais (reproduit hors pytest avec un generateur minimal,
    # voir ank log). Le contrat "flux SSE" est donc verifie par la route
    # elle-meme (media_type) ; le comportement "un evenement par mtime
    # changee" est prouve directement sur le generateur ci-dessous.
    app = create_app(config=_sse_config(tmp_path))
    route = next(r for r in app.router.routes if getattr(r, "path", None) == "/api/events")
    assert "GET" in route.methods


def test_event_stream_generator_emits_on_file_mtime_change(tmp_path, isolated_cwd):
    import asyncio

    from clipper.web.app import _event_stream

    config = _sse_config(tmp_path)

    async def _run() -> str:
        agen = _event_stream(config).__aiter__()

        async def _touch_queue_file_soon() -> None:
            await asyncio.sleep(0.15)
            state_dir = tmp_path / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "queue.json").write_text("[]", encoding="utf-8")

        asyncio.create_task(_touch_queue_file_soon())
        return await asyncio.wait_for(agen.__anext__(), timeout=2.0)

    chunk = asyncio.run(_run())

    assert chunk.startswith("data: ")
    event = json.loads(chunk[len("data: "):].strip())
    assert event["kind"] == "queue"
    assert "id" in event and "at" in event


def test_event_stream_generator_ignores_files_present_before_connecting(tmp_path, isolated_cwd):
    import asyncio

    from clipper.web.app import _event_stream

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "queue.json").write_text("[]", encoding="utf-8")
    config = _sse_config(tmp_path)

    async def _run() -> bool:
        agen = _event_stream(config).__aiter__()
        try:
            await asyncio.wait_for(agen.__anext__(), timeout=0.3)
            return True
        except asyncio.TimeoutError:
            return False

    got_event = asyncio.run(_run())
    assert got_event is False


def _browser_profile_tree(state_dir: Path, files: int) -> None:
    root = state_dir / "browser" / "compte" / "Default"
    root.mkdir(parents=True)
    for i in range(files):
        (root / f"f{i}.json").write_text("{}", encoding="utf-8")


def test_scan_watched_never_visits_the_browser_profiles(tmp_path, monkeypatch, isolated_cwd):
    from clipper.web import app as web_app

    state = tmp_path / "state"
    _browser_profile_tree(state, 50)
    (state / "queue.json").write_text("[]", encoding="utf-8")
    (state / "publish").mkdir()
    (state / "publish" / "chaine.json").write_text("{}", encoding="utf-8")
    visited: list[Path] = []
    real_rglob = Path.rglob
    real_glob = Path.glob

    def spy_rglob(self, pattern):
        for p in real_rglob(self, pattern):
            visited.append(p)
            yield p

    def spy_glob(self, pattern):
        for p in real_glob(self, pattern):
            visited.append(p)
            yield p

    monkeypatch.setattr(Path, "rglob", spy_rglob)
    monkeypatch.setattr(Path, "glob", spy_glob)

    found = web_app._scan_watched(tmp_path / "workspace", [(state, None)])

    assert {(k, i) for _, k, i in found} == {("queue", "queue"), ("publish", "chaine")}
    assert visited and not any("browser" in p.parts for p in visited)


def test_event_stream_ignores_unnamed_state_folders_but_keeps_named_events(tmp_path, isolated_cwd):
    import asyncio

    from clipper.web.app import _event_stream

    state = tmp_path / "state"
    _browser_profile_tree(state, 5)
    config = _sse_config(tmp_path)

    async def _run() -> dict:
        agen = _event_stream(config).__aiter__()

        async def _touch() -> None:
            await asyncio.sleep(0.15)
            (state / "browser" / "compte" / "Default" / "nouveau.json").write_text("{}", encoding="utf-8")
            (state / "veille").mkdir()
            (state / "veille" / "jour.json").write_text("{}", encoding="utf-8")

        asyncio.create_task(_touch())
        chunk = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
        await agen.aclose()
        return json.loads(chunk[len("data: "):])

    assert asyncio.run(_run())["kind"] == "veille"  # jamais un événement « browser »


def test_two_sse_clients_trigger_a_single_scan_per_interval(tmp_path, monkeypatch, isolated_cwd):
    import asyncio

    from clipper.web import app as web_app

    config = _sse_config(tmp_path)
    config._sections["web"]["sse_poll_interval_s"] = 0.3
    scans: list[int] = []
    real = web_app._scan_watched

    def counting(*args, **kwargs):
        scans.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(web_app, "_scan_watched", counting)

    async def _run() -> tuple[str, str]:
        a, b = web_app._event_stream(config).__aiter__(), web_app._event_stream(config).__aiter__()
        ta = asyncio.create_task(a.__anext__())
        tb = asyncio.create_task(b.__anext__())
        await asyncio.sleep(0.1)  # les deux sont abonnés, baseline faite
        baseline = len(scans)
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state" / "queue.json").write_text("[]", encoding="utf-8")
        ra, rb = await asyncio.wait_for(asyncio.gather(ta, tb), timeout=3.0)
        assert baseline == 1  # un seul scan de départ pour deux clients
        await a.aclose()
        await b.aclose()
        assert web_app._SSE_HUBS == {}  # plus aucun client : scanner arrêté et retiré
        return ra, rb

    ra, rb = asyncio.run(_run())
    assert json.loads(ra[len("data: "):])["kind"] == "queue" and ra == rb


def test_scan_tour_is_at_least_10x_faster_without_browser_profiles(tmp_path, isolated_cwd):
    import time

    from clipper.web import app as web_app

    state = tmp_path / "state"
    _browser_profile_tree(state, 19000)
    (state / "queue.json").write_text("[]", encoding="utf-8")
    for i in range(50):
        (state / "publish").mkdir(exist_ok=True)
        (state / "publish" / f"c{i}.json").write_text("{}", encoding="utf-8")

    def tour_old() -> float:  # ancien comportement : rglob de toute la racine
        t = time.perf_counter()
        list(state.rglob("*.json"))
        return time.perf_counter() - t

    def tour_new() -> float:
        t = time.perf_counter()
        web_app._scan_watched(tmp_path / "workspace", [(state, None)])
        return time.perf_counter() - t

    old = min(tour_old() for _ in range(3))
    new = min(tour_new() for _ in range(3))
    print(f"MESURE tour scan : avant {old * 1000:.1f} ms, apres {new * 1000:.1f} ms")
    assert old >= 10 * new


# --------------------------------------------------------------------------
# P : jeton d'acces (ADR-4f6e §5)
# --------------------------------------------------------------------------


def _config_with_web(tmp_path, **web) -> Config:
    return Config(
        mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={"web": web},
    )


def test_create_app_refuses_non_loopback_host_without_token(tmp_path, isolated_cwd):
    from clipper.web.app import WebConfigError

    config = _config_with_web(tmp_path, host="0.0.0.0", token="")

    with pytest.raises(WebConfigError):
        create_app(config=config)


def test_create_app_accepts_non_loopback_host_with_token(tmp_path, isolated_cwd):
    config = _config_with_web(tmp_path, host="0.0.0.0", token="secret")
    create_app(config=config)


def test_api_requires_token_on_non_loopback_host(tmp_path, isolated_cwd):
    config = _config_with_web(tmp_path, host="0.0.0.0", token="secret")
    test_client = TestClient(create_app(config=config))

    resp = test_client.get("/api/videos")

    assert resp.status_code == 401
    assert "detail" in resp.json()


def test_api_accepts_token_via_header(tmp_path, isolated_cwd):
    config = _config_with_web(tmp_path, host="0.0.0.0", token="secret")
    test_client = TestClient(create_app(config=config))

    resp = test_client.get("/api/videos", headers={"X-Clipper-Token": "secret"})

    assert resp.status_code == 200


def test_api_accepts_token_via_cookie(tmp_path, isolated_cwd):
    config = _config_with_web(tmp_path, host="0.0.0.0", token="secret")
    test_client = TestClient(create_app(config=config))
    test_client.cookies.set("clipper_token", "secret")

    resp = test_client.get("/api/videos")

    assert resp.status_code == 200


def test_media_also_requires_token_on_non_loopback_host(tmp_path, isolated_cwd):
    config = _config_with_web(tmp_path, host="0.0.0.0", token="secret")
    test_client = TestClient(create_app(config=config))

    resp = test_client.get(f"/media/source/{VIDEO_ID}")

    assert resp.status_code == 401


def test_loopback_host_requires_no_token(tmp_path, isolated_cwd):
    config = _config_with_web(tmp_path, host="127.0.0.1", token="")
    test_client = TestClient(create_app(config=config))

    resp = test_client.get("/api/videos")

    assert resp.status_code == 200


# --------------------------------------------------------------------------
# M : coquille de la page v2 (SPEC-c100 T2..T8, TASK-f753) - verifications
# statiques des fichiers servis ; le JS n'est pas execute ici.
# --------------------------------------------------------------------------

import re
from html.parser import HTMLParser

SCREENS = ["dashboard", "videos", "review", "clips", "channels", "publish", "stats", "settings"]
TAB_SCREENS = ["dashboard", "veille", "review", "clips", "publish"]  # Veille remplace Vidéos dans la barre basse (SPEC-bdd9 R10)
STATIC = Path(__file__).resolve().parent.parent / "clipper" / "web" / "static"


def served(tmp_path, path: str) -> str:
    resp = client(tmp_path).get(path)
    assert resp.status_code == 200, path
    return resp.text


def test_index_has_navigation_to_the_eight_screens(tmp_path, isolated_cwd):
    html = served(tmp_path, "/")
    nav = html[html.index('<nav class="nav"'):html.index("</nav>", html.index('<nav class="nav"'))]
    for screen in SCREENS:
        assert f'href="#/{"styles" if screen == "channels" else screen}"' in nav, screen   # écran « Styles » : #/styles
        assert f'data-screen="{screen}"' in nav, screen
        assert f'id="screen-{screen}"' in html, screen


def test_index_has_a_mobile_tab_bar(tmp_path, isolated_cwd):
    html = served(tmp_path, "/")
    assert 'name="viewport"' in html
    tabbar = html[html.index('<nav class="tabbar"'):html.index("</nav>", html.index('<nav class="tabbar"'))]
    for screen in TAB_SCREENS:
        assert f'href="#/{screen}"' in tabbar, screen
    css = served(tmp_path, "/static/style.css")
    assert ".tabbar" in css and "@media (max-width: 900px)" in css
    assert "44px" in css  # zones de toucher (T6)


def test_app_js_listens_to_sse_and_reloads_the_targeted_object(tmp_path, isolated_cwd):
    js = served(tmp_path, "/static/app.js")
    assert 'new EventSource("/api/events")' in js
    for needle in ("JSON.parse(", "event.kind", "event.id", "/api/videos/${", "setInterval(", "POLL_MS = 5000"):
        assert needle in js, needle
    html = served(tmp_path, "/")
    assert 'id="conn-banner"' in html and "connexion perdue" in html.lower()
    assert "conn-banner" in js and "onerror" in js


def test_app_js_notifies_on_video_status_changes(tmp_path, isolated_cwd):
    js = served(tmp_path, "/static/app.js")
    for status in ("done", "failed", "awaiting_review", "queued"):
        assert status in js
    assert "Notification" in js


def _api_calls(js: str) -> list[tuple[str, str]]:
    calls = []
    for m in re.finditer(r"""api\(\s*["`](/api[^"`?]*)[^"`]*["`]\s*(?:,\s*(?:\{\s*method:\s*"(\w+)"|jsonBody\(\s*"(\w+)"))?""", js):
        calls.append((m.group(2) or m.group(3) or "GET", re.sub(r"\$\{[^}]*\}", "x", m.group(1))))
    return calls


def test_every_route_called_by_app_js_exists_in_the_app(tmp_path, isolated_cwd):
    app = create_app(config=make_config(tmp_path))
    js = "".join(p.read_text(encoding="utf-8") for p in sorted(STATIC.rglob("*.js")))
    calls = _api_calls(js)
    assert len(calls) >= 6, calls
    missing = []
    for method, path in calls:
        ok = any(
            getattr(route, "path_regex", None) is not None
            and route.path_regex.match(path)
            and method in (getattr(route, "methods", None) or set())
            for route in app.routes
        )
        if not ok:
            missing.append(f"{method} {path}")
    assert missing == []


def test_toasts_with_undo_and_confirm_modal(tmp_path, isolated_cwd):
    js = "".join(served(tmp_path, f"/static/{n}") for n in ("ui.js", "app.js"))
    assert "function toast(" in js and "data-undo" in js and "Annuler" in js
    assert "UNDO_MS = 5000" in js
    assert "function confirmDialog(" in js and 'role", "dialog"' in js
    html = served(tmp_path, "/")
    assert 'id="toasts"' in html and 'id="overlay"' in html
    css = served(tmp_path, "/static/style.css")
    assert ".toast" in css and ".modal" in css


def test_theme_follows_system_with_remembered_toggle(tmp_path, isolated_cwd):
    html = served(tmp_path, "/")
    js = served(tmp_path, "/static/app.js")
    assert 'id="btn-theme"' in html
    assert "prefers-color-scheme" in html and "prefers-color-scheme" in js
    assert "localStorage" in html and "localStorage" in js
    assert "data-theme" in html or "dataset.theme" in html
    css = served(tmp_path, "/static/style.css")
    assert ':root[data-theme="light"]' in css and ':root[data-theme="dark"]' in css


def test_token_page_sets_cookie_and_replays_on_401(tmp_path, isolated_cwd):
    html = served(tmp_path, "/")
    js = served(tmp_path, "/static/app.js")
    assert 'id="token-view"' in html and 'id="token-form"' in html and 'type="password"' in html
    assert "clipper_token" in js and "document.cookie" in js
    assert "401" in js and "askToken(" in js
    # rejoue la requete apres saisie du jeton
    assert js.count("fetch(") >= 1 and "return api(" in js


class _TextAndLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text: list[str] = []
        self.urls: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        for k, v in attrs:
            if k in ("src", "href", "srcset", "action") and v:
                self.urls.append(v)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.text.append(data.strip())


def test_no_external_resource_in_index_and_css(tmp_path, isolated_cwd):
    html = served(tmp_path, "/")
    parser = _TextAndLinks()
    parser.feed(html)
    external = [u for u in parser.urls if re.match(r"(?i)(https?:)?//", u)]
    assert external == []
    for name in ("style.css", "fonts.css"):
        css = served(tmp_path, f"/static/{name}")
        assert not re.search(r"(?i)https?://|@import|url\(\s*['\"]?//", css), name
    # les polices sont des fichiers locaux servis, sous licence libre citee
    fonts = served(tmp_path, "/static/fonts.css")
    files = re.findall(r"url\(([^)]+\.woff2)\)", fonts)
    assert files
    for f in files:
        assert f.startswith("/static/fonts/")
        assert client(tmp_path).get(f).status_code == 200, f
    licences = served(tmp_path, "/static/fonts/LICENSES.txt")
    for name in ("Barlow", "JetBrains Mono", "OFL"):
        assert name in licences


def test_visible_strings_are_french_and_no_real_names(tmp_path, isolated_cwd):
    html = served(tmp_path, "/")
    parser = _TextAndLinks()
    parser.feed(html)
    visible = " ".join(parser.text)
    for english in ("Loading", "Submit", "Cancel", "Dashboard", "Settings", "Save", "Search", "Connection lost"):
        assert english not in visible, english
    assert "Tableau de bord" in visible and "Réglages" in visible and "Styles" in visible
    forbidden = ("contre-pied", "contrepied", "amelia", "zedk", "nicoc", "twitch.tv/", "youtube.com/@")
    for path in sorted(STATIC.rglob("*")):
        if path.suffix in {".html", ".css", ".js", ".txt", ".svg"}:
            text = path.read_text(encoding="utf-8").lower()
            for word in forbidden:
                assert word not in text, f"{word!r} dans {path.name}"
    assert "ma_chaine" in served(tmp_path, "/static/screens.js")


def test_loading_skeletons_and_empty_states(tmp_path, isolated_cwd):
    html = served(tmp_path, "/")
    assert 'class="skeleton' in html
    css = served(tmp_path, "/static/style.css")
    assert ".skeleton" in css
    screens = served(tmp_path, "/static/screens.js")
    assert "Ajoute une vidéo" in screens and "Crée un style" in screens


def test_static_assets_are_served(tmp_path, isolated_cwd):
    for name in ("app.js", "ui.js", "icons.js", "screens.js", "style.css", "fonts.css", "logo.svg"):
        assert client(tmp_path).get(f"/static/{name}").status_code == 200, name
    assert not (STATIC / "data.js").exists()


# --------------------------------------------------------------------------
# TASK-157b : ecran Videos (liste filtrable, fiche, durees, journal)
# --------------------------------------------------------------------------


def _step(started, finished, status="done", **extra):
    return {"status": status, "reason": None, "started_at": started, "finished_at": finished, **extra}


def _write_meta(tmp_path, video_id, title):
    meta = tmp_path / "workspace" / video_id / "meta.json"
    meta.write_text(json.dumps({"video_id": video_id, "title": title}), encoding="utf-8")


def _seed_videos(tmp_path):
    steps_a = {
        "download": _step("2026-09-30T10:00:00+00:00", "2026-09-30T10:00:42+00:00"),
        "transcribe": _step("2026-09-30T10:00:42+00:00", None, status="running",
                            progress={"fraction": 0.4, "eta_s": 90.0, "message": "segment 4/10"}),
    }
    _write_state(tmp_path, "aaaaaaaaaaa", status="running", channel="ma_chaine", steps=steps_a)
    _write_meta(tmp_path, "aaaaaaaaaaa", "Grosse partie du soir")
    _write_state(tmp_path, "bbbbbbbbbbb", status="failed", channel=None, reason="Erreur: reseau coupe")
    _write_state(tmp_path, "ccccccccccc", status="done", channel="autre_chaine",
                 source_url="https://example.org/video/XYZ")


def test_list_videos_enriches_each_video_with_title_duration_and_current_step(tmp_path, isolated_cwd):
    _busy_worker(tmp_path)
    _seed_videos(tmp_path)

    by_id = {v["video_id"]: v for v in client(tmp_path).get("/api/videos").json()}

    a = by_id["aaaaaaaaaaa"]
    assert a["channel"] == "ma_chaine" and a["status"] == "running" and a["reason"] is None
    assert a["title"] == "Grosse partie du soir"
    assert a["current_step"] == "transcribe"
    assert a["durations"]["download"] == 42.0
    assert a["durations"]["transcribe"] is None  # pas finie : pas de duree inventee
    b = by_id["bbbbbbbbbbb"]
    assert b["channel"] is None and b["reason"] == "Erreur: reseau coupe"
    assert b["current_step"] == "download"  # premiere etape non terminee
    # sans meta.json : l'identifiant sert de titre, et la raison est dite
    assert b["title"] == "bbbbbbbbbbb"
    assert "meta.json" in b["title_reason"]
    assert "title_reason" not in a or a["title_reason"] is None


def test_list_videos_filters_by_channel(tmp_path, isolated_cwd):
    _seed_videos(tmp_path)
    resp = client(tmp_path).get("/api/videos", params={"channel": "ma_chaine"})
    assert [v["video_id"] for v in resp.json()] == ["aaaaaaaaaaa"]


def test_list_videos_filters_by_status(tmp_path, isolated_cwd):
    _seed_videos(tmp_path)
    resp = client(tmp_path).get("/api/videos", params={"status": "failed"})
    assert [v["video_id"] for v in resp.json()] == ["bbbbbbbbbbb"]


def test_list_videos_unknown_status_is_400_in_french(tmp_path, isolated_cwd):
    _seed_videos(tmp_path)
    resp = client(tmp_path).get("/api/videos", params={"status": "bizarre"})
    assert resp.status_code == 400
    assert "statut" in resp.json()["detail"]


@pytest.mark.parametrize("q, expected", [
    ("AAAAAA", ["aaaaaaaaaaa"]),              # video_id, insensible a la casse
    ("grosse partie", ["aaaaaaaaaaa"]),       # titre de meta.json
    ("example.org/video", ["ccccccccccc"]),   # source_url
    ("introuvable", []),
])
def test_list_videos_text_filter_matches_id_title_and_source_url(tmp_path, isolated_cwd, q, expected):
    _seed_videos(tmp_path)
    resp = client(tmp_path).get("/api/videos", params={"q": q})
    assert sorted(v["video_id"] for v in resp.json()) == expected


def test_list_videos_filters_combine(tmp_path, isolated_cwd):
    _seed_videos(tmp_path)
    resp = client(tmp_path).get("/api/videos", params={"channel": "ma_chaine", "status": "done"})
    assert resp.json() == []


def test_get_video_detail_includes_clips_awaiting_title_and_durations(tmp_path, isolated_cwd):
    clips = [{"clip_id": "03-p2", "ready": True, "qa_status": "passed", "issues": [],
              "mp4": "output/x/03-p2.mp4", "json": "output/x/03-p2.json"}]
    steps = {"download": _step("2026-09-30T10:00:00+00:00", "2026-09-30T10:01:30+00:00")}
    _write_state(tmp_path, VIDEO_ID, status="awaiting_review", awaiting=[0, 3], clips=clips, steps=steps)
    _write_meta(tmp_path, VIDEO_ID, "Titre de la source")

    body = client(tmp_path).get(f"/api/videos/{VIDEO_ID}").json()

    assert body["clips"] == clips
    assert body["awaiting"] == [0, 3]
    assert body["durations"]["download"] == 90.0
    assert body["title"] == "Titre de la source"
    assert body["video_id"] == VIDEO_ID and body["steps"]["download"]["status"] == "done"


# --- ecran videos de la page -------------------------------------------------

VIDEOS_JS = STATIC / "screens" / "videos.js"


def _videos_js(tmp_path) -> str:
    return served(tmp_path, "/static/screens/videos.js")


def test_index_loads_the_videos_screen_script_after_the_shell_screens(tmp_path, isolated_cwd):
    html = served(tmp_path, "/")
    assert html.index("/static/screens.js") < html.index("/static/screens/videos.js") < html.index("/static/app.js")


def test_videos_screen_has_add_form_with_channel_selector_and_action(tmp_path, isolated_cwd):
    js = _videos_js(tmp_path)
    for needle in ('name="url"', 'name="channel"', 'name="action"', "Sans style", 'api("/api/channels"',
                   'value="run"', 'value="render"', '"/api/queue"', 'method: "POST"'):
        assert needle in js, needle


def test_videos_screen_lists_with_server_side_filters(tmp_path, isolated_cwd):
    js = _videos_js(tmp_path)
    assert "/api/videos?" in js
    for needle in ("channel", "status", "q"):
        assert f'.set("{needle}"' in js or f"{needle}:" in js or f'"{needle}"' in js, needle
    assert 'name="q"' in js and 'name="filter-channel"' in js and 'name="filter-status"' in js


def test_videos_screen_detail_has_12_step_frise_log_and_actions(tmp_path, isolated_cwd):
    js = _videos_js(tmp_path)
    for needle in ("class=\"frise\"", "fstep", "/api/videos/${", "/events", "clipper:event",
                   "/retry", "/cancel", "from_step", "confirmDialog(",
                   "Relancer depuis cette étape", "Annuler le traitement", "#/review/", "#/clips/",
                   'class="log"', "STEP_LABELS"):
        assert needle in js, needle
    # la frise est faite des 12 etapes du pipeline, dans l'ordre
    from clipper import pipeline
    app_text = "".join(p.read_text(encoding="utf-8") for p in STATIC.glob("*.js"))
    for step in pipeline.STEPS:
        assert f"{step}:" in app_text, step


def test_videos_screen_confirms_before_retry_and_cancel(tmp_path, isolated_cwd):
    js = _videos_js(tmp_path)
    for route in ("/retry", "/cancel"):
        idx = js.index(route)
        before = js[max(0, idx - 600):idx]
        assert "confirmDialog(" in before, route


def test_videos_screen_has_its_own_css_section(tmp_path, isolated_cwd):
    css = served(tmp_path, "/static/style.css")
    assert "/* ---------- Ecran Videos (TASK-157b) ---------- */" in css
# E1 : GET /api/dashboard (TASK-aad3, SPEC-c100 E1, T2, T8)
# --------------------------------------------------------------------------

import sys  # noqa: E402
import types  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "clipper" / "web" / "static"


def _dashboard(tmp_path) -> dict:
    resp = client(tmp_path).get("/api/dashboard")
    assert resp.status_code == 200
    return resp.json()


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_sidecar(tmp_path, video_id, clip_id, **fields):
    sidecar = {"clip_id": clip_id, "ready": True, "screen_title": f"Titre {clip_id}"}
    sidecar.update(fields)
    _write_json(tmp_path / "output" / video_id / f"{clip_id}.json", sidecar)


def _publish_entry(video_id, clip_id, status, slot_at):
    return {"video_id": video_id, "clip_id": clip_id, "series_id": None, "part": None, "status": status,
            "slot_at": slot_at, "decided_at": "2026-01-01T00:00:00+00:00", "published_at": None, "error": None}


def _usage_line(usage, cost, when, **extra):
    return {"recorded_at": when.isoformat(), "usage": usage, "model": "m", "input_tokens": 10,
            "output_tokens": 5, "cache_read_tokens": 0, "cost_usd": cost, "duration_s": 1.0, **extra}


def _write_usage(tmp_path, video_id, lines):
    path = tmp_path / "workspace" / video_id / "llm_usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def test_dashboard_empty_workspace_has_explicit_empty_sections(tmp_path, isolated_cwd):
    data = _dashboard(tmp_path)

    assert data["running"] == [] and data["queue"] == [] and data["failed"] == [] and data["queued"] == []
    assert data["watch_pending"] == []
    assert data["clips_to_review"] == 0
    assert data["next_publications"] == []
    assert data["llm_cost"]["today"] == 0 and data["llm_cost"]["week"] == 0 and data["llm_cost"]["by_usage"] == {}
    assert not [k for k in data if k.endswith("_error")]


def test_dashboard_running_videos_expose_current_step_and_progress(tmp_path, isolated_cwd):
    _busy_worker(tmp_path)
    progress = {"fraction": 0.4, "eta_s": 90.0, "message": "segment 3/8"}
    state = _write_state(tmp_path, "aaaaaaaaaaa", status="running", channel="ma_chaine")
    state["steps"]["download"]["status"] = "done"
    state["steps"]["transcribe"].update(status="running", progress=progress)
    _write_json(tmp_path / "workspace" / "aaaaaaaaaaa" / "pipeline.json", state)
    _write_state(tmp_path, "bbbbbbbbbbb", status="done")

    running = _dashboard(tmp_path)["running"]

    assert [v["video_id"] for v in running] == ["aaaaaaaaaaa"]
    assert running[0]["step"] == "transcribe"
    assert running[0]["progress"] == progress
    assert running[0]["channel"] == "ma_chaine"


def test_dashboard_queue_lists_queue_json_entries(tmp_path, isolated_cwd):
    entries = [{"id": "e1", "video_id": VIDEO_ID, "url": URL, "channel": None, "action": "run",
                "force_steps": [], "enqueued_at": "2026-01-01T00:00:00+00:00", "status": "waiting", "pid": None}]
    _write_json(tmp_path / "state" / "queue.json", entries)

    queue = _dashboard(tmp_path)["queue"]
    assert [{k: v for k, v in e.items() if k != "platform_thumbnail"} for e in queue] == entries
    assert queue[0]["platform_thumbnail"].endswith(f"/{VIDEO_ID}/hqdefault.jpg")


def test_dashboard_failed_and_queued_carry_reason_and_retry_at(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa", status="failed", reason="ffmpeg a echoue", retry_at=None)
    _write_state(tmp_path, "bbbbbbbbbbb", status="queued", reason="quota LLM atteint",
                 retry_at="2026-01-02T08:00:00+00:00")
    _write_state(tmp_path, "ccccccccccc", status="done")

    data = _dashboard(tmp_path)

    assert [(v["video_id"], v["reason"], v["retry_at"]) for v in data["failed"]] == [
        ("aaaaaaaaaaa", "ffmpeg a echoue", None)]
    assert [(v["video_id"], v["reason"], v["retry_at"]) for v in data["queued"]] == [
        ("bbbbbbbbbbb", "quota LLM atteint", "2026-01-02T08:00:00+00:00")]


def test_dashboard_watch_pending_gathers_vods_to_confirm(tmp_path, isolated_cwd):
    vod = {"video_id": "ddddddddddd", "url": "https://example.test/v/1", "title": "Direct du soir",
           "duration_s": 7200, "published_at": "2026-01-01T20:00:00+00:00", "found_at": "2026-01-02T01:00:00+00:00"}
    _write_json(tmp_path / "state" / "watch" / "ma_chaine.json",
                {"checked_at": "2026-01-02T01:00:00+00:00", "seen": [], "pending": [vod], "last_error": None})
    _write_json(tmp_path / "state" / "watch" / "autre.json",
                {"checked_at": "2026-01-02T01:00:00+00:00", "seen": [], "pending": [], "last_error": None})

    pending = _dashboard(tmp_path)["watch_pending"]

    assert pending == [{**vod, "channel": "ma_chaine"}]


def test_dashboard_unreadable_watch_file_is_null_with_a_french_error(tmp_path, isolated_cwd):
    (tmp_path / "state" / "watch").mkdir(parents=True)
    (tmp_path / "state" / "watch" / "ma_chaine.json").write_text("{pas du json", encoding="utf-8")

    data = _dashboard(tmp_path)

    assert data["watch_pending"] is None
    assert "ma_chaine.json" in data["watch_pending_error"]


def test_dashboard_counts_ready_clips_absent_from_publish_state(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa", channel="ma_chaine")
    _write_state(tmp_path, "bbbbbbbbbbb", channel=None)
    _write_sidecar(tmp_path, "aaaaaaaaaaa", "01")
    _write_sidecar(tmp_path, "aaaaaaaaaaa", "02")
    _write_sidecar(tmp_path, "aaaaaaaaaaa", "03", ready=False)
    _write_sidecar(tmp_path, "bbbbbbbbbbb", "01")
    _write_json(tmp_path / "state" / "publish" / "ma_chaine.json",
                [_publish_entry("aaaaaaaaaaa", "01", "approved", None)])

    assert _dashboard(tmp_path)["clips_to_review"] == 2  # aaaa/02 et bbbb/01


def test_dashboard_invalid_publish_entry_is_null_with_a_french_error(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa", channel="ma_chaine")
    _write_sidecar(tmp_path, "aaaaaaaaaaa", "01")
    _write_json(tmp_path / "state" / "publish" / "ma_chaine.json", [{"video_id": "aaaaaaaaaaa"}])

    data = _dashboard(tmp_path)

    assert data["clips_to_review"] is None
    assert data["clips_to_review_error"]
    assert data["next_publications"] is None
    assert data["next_publications_error"]


def test_dashboard_next_publications_are_the_five_earliest_scheduled_across_channels(tmp_path, isolated_cwd):
    base = datetime(2030, 1, 1, 12, 0)
    for i in range(4):
        _write_sidecar(tmp_path, "aaaaaaaaaaa", f"{i:02d}")
    entries_a = [_publish_entry("aaaaaaaaaaa", f"{i:02d}", "scheduled", (base + timedelta(days=2 * i)).isoformat())
                 for i in range(4)]
    entries_a.append(_publish_entry("aaaaaaaaaaa", "99", "approved", None))
    entries_a.append(_publish_entry("aaaaaaaaaaa", "98", "published", base.isoformat()))
    entries_b = [_publish_entry("bbbbbbbbbbb", f"{i:02d}", "scheduled", (base + timedelta(days=2 * i + 1)).isoformat())
                 for i in range(4)]
    _write_json(tmp_path / "state" / "publish" / "ma_chaine.json", entries_a)
    _write_json(tmp_path / "state" / "publish" / "autre.json", entries_b)

    nxt = _dashboard(tmp_path)["next_publications"]

    assert len(nxt) == 5
    assert [e["slot_at"] for e in nxt] == [(base + timedelta(days=d)).isoformat() for d in range(5)]
    assert [e["channel"] for e in nxt] == ["ma_chaine", "autre", "ma_chaine", "autre", "ma_chaine"]
    assert nxt[0]["screen_title"] == "Titre 00"
    assert all(e["status"] == "scheduled" for e in nxt)


def test_dashboard_llm_cost_sums_usage_journals_by_window_and_usage(tmp_path, isolated_cwd):
    now = datetime.now().astimezone()
    _write_usage(tmp_path, "aaaaaaaaaaa", [
        _usage_line("moments", 0.25, now),
        _usage_line("vision", 0.10, now),
        _usage_line("moments", 0.50, now - timedelta(days=3)),
        _usage_line("moments", 9.00, now - timedelta(days=30)),  # hors fenetre
    ])
    _write_usage(tmp_path, "bbbbbbbbbbb", [_usage_line("moments", 0.15, now)])

    cost = _dashboard(tmp_path)["llm_cost"]

    assert cost["today"] == pytest.approx(0.50)
    assert cost["week"] == pytest.approx(1.00)
    assert cost["by_usage"] == {"moments": pytest.approx(0.90), "vision": pytest.approx(0.10)}


def test_dashboard_llm_cost_window_uses_the_paris_timezone(tmp_path, isolated_cwd):
    """Revue r-comptes 10 : « aujourd'hui »/« 7 jours » suivent Europe/Paris (regle du projet), jamais
    l'heure du PC — ici une machine en UK (GMT/BST, une heure derriere Paris en hiver comme en ete)."""
    from zoneinfo import ZoneInfo

    paris_midnight = datetime.now(ZoneInfo("Europe/Paris")).replace(hour=0, minute=0, second=0, microsecond=0)
    _write_usage(tmp_path, "aaaaaaaaaaa", [
        _usage_line("moments", 1.0, paris_midnight + timedelta(seconds=1)),
        _usage_line("moments", 2.0, paris_midnight - timedelta(seconds=1)),  # veille a Paris
    ])

    cost = _dashboard(tmp_path)["llm_cost"]

    assert cost["today"] == pytest.approx(1.0)
    assert cost["week"] == pytest.approx(3.0)


def test_dashboard_llm_cost_reports_calls_with_unknown_cost(tmp_path, isolated_cwd):
    now = datetime.now().astimezone()
    _write_usage(tmp_path, "aaaaaaaaaaa", [_usage_line("moments", None, now), _usage_line("moments", 0.2, now)])

    cost = _dashboard(tmp_path)["llm_cost"]

    assert cost["today"] == pytest.approx(0.2)
    assert cost["unreported_calls"] == 1


def test_dashboard_unreadable_usage_journal_is_null_with_a_french_error(tmp_path, isolated_cwd):
    path = tmp_path / "workspace" / "aaaaaaaaaaa" / "llm_usage.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("pas du json\n", encoding="utf-8")

    data = _dashboard(tmp_path)

    assert data["llm_cost"] is None
    assert "llm_usage.jsonl" in data["llm_cost_error"]


def test_dashboard_hardware_is_cpu_without_vram_or_error(tmp_path, isolated_cwd, monkeypatch):
    from clipper import gpu

    monkeypatch.setattr(gpu, "get_device", lambda: gpu.Device(type="cpu", compute_type="int8"))

    data = _dashboard(tmp_path)

    assert data["hardware"] == {"device": "cpu", "vram_used_mb": None}
    assert "hardware_error" not in data


def test_dashboard_hardware_reads_vram_used_on_cuda_through_clipper_gpu(tmp_path, isolated_cwd, monkeypatch):
    from clipper import gpu

    monkeypatch.setattr(gpu, "get_device", lambda: gpu.Device(type="cuda", compute_type="float16"))
    monkeypatch.setattr(gpu, "vram_used_mb", lambda: 5 * 1024)
    monkeypatch.setitem(sys.modules, "torch", None)  # le panneau n'importe plus torch

    assert _dashboard(tmp_path)["hardware"] == {"device": "cuda", "vram_used_mb": 5 * 1024}


def test_dashboard_hardware_vram_unknown_is_null_with_the_reason(tmp_path, isolated_cwd, monkeypatch):
    from clipper import gpu

    def unavailable():
        raise gpu.GpuError("nvidia-smi introuvable dans le PATH")

    monkeypatch.setattr(gpu, "get_device", lambda: gpu.Device(type="cuda", compute_type="float16"))
    monkeypatch.setattr(gpu, "vram_used_mb", unavailable)
    monkeypatch.setitem(sys.modules, "torch", None)

    hw = _dashboard(tmp_path)["hardware"]

    assert hw["device"] == "cuda"
    assert hw["vram_used_mb"] is None
    assert "nvidia-smi introuvable" in hw["vram_used_mb_error"]
    assert "No module named" not in hw["vram_used_mb_error"] and "torch" not in hw["vram_used_mb_error"]


def test_web_app_never_imports_torch():
    assert "torch" not in (Path(__file__).resolve().parent.parent / "clipper" / "web" / "app.py").read_text(encoding="utf-8")


def test_dashboard_screen_is_wired_with_every_section_and_empty_state():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "screens" / "dashboard.js").read_text(encoding="utf-8")

    assert "/static/screens/dashboard.js" in page
    assert page.index("/static/screens.js") < page.index("/static/screens/dashboard.js")
    # une section par bloc de donnees (data-section=<cle>), chacune avec un etat vide explicite
    assert 'data-section="${key}"' in js
    for section in ("running", "queue", "failed", "queued", "watch_pending", "clips_to_review",
                    "next_publications", "llm_cost", "hardware"):
        assert f'dashSection("{section}"' in js, section
    for empty in ("Aucune vidéo en cours", "La file est vide", "Aucun échec", "Aucune vidéo en attente de reprise",
                  "Aucune VOD à confirmer", "Aucun clip à valider", "Aucune publication programmée",
                  "Aucun appel au modèle", "CPU"):
        assert empty in js, empty
    # mise a jour sur evenement SSE, donnees lues sur /api/dashboard, erreurs affichees
    assert "/api/dashboard" in js
    assert "clipper:event" in js
    assert "_error" in js


# --------------------------------------------------------------------------
# Revue des moments v2 (TASK-6e75) : API enrichie, ecran review, rendu en file
# --------------------------------------------------------------------------


TRANSCRIPT_JSON = {
    "segments": [
        {"start": 2.0, "end": 12.0, "words": [
            {"word": " GTA", "start": 2.0, "end": 3.0},
            {"word": " six", "start": 3.0, "end": 4.0},
            {"word": " arrive", "start": 4.0, "end": 12.0},
        ]},
        {"start": 40.0, "end": 60.0, "words": [
            {"word": " Autre", "start": 40.0, "end": 50.0},
            {"word": " moment", "start": 50.0, "end": 60.0},
        ]},
        {"start": 100.0, "end": 101.0, "words": [{"word": " hors", "start": 100.0, "end": 101.0}]},
    ],
}


def _write_review_sources(tmp_path, *, transcript=True, meta=True):
    _write_moments_fixtures(tmp_path)
    video_dir = tmp_path / "workspace" / VIDEO_ID
    if transcript:
        (video_dir / "transcript.json").write_text(json.dumps(TRANSCRIPT_JSON), encoding="utf-8")
    if meta:
        (video_dir / "meta.json").write_text(json.dumps({"video_id": VIDEO_ID, "duration": 3600}), encoding="utf-8")


def test_list_moments_carries_the_transcript_of_each_moment(tmp_path, isolated_cwd):
    _write_review_sources(tmp_path)

    moments = {m["id"]: m for m in client(tmp_path).get(f"/api/videos/{VIDEO_ID}/moments").json()}

    assert moments[0]["transcript"] == "GTA six arrive"
    assert moments[1]["transcript"] == "Autre moment"


def test_list_moments_carries_the_source_duration(tmp_path, isolated_cwd):
    _write_review_sources(tmp_path)

    moments = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/moments").json()

    assert all(m["source_duration"] == 3600 for m in moments)


def test_list_moments_transcript_reuses_the_pipeline_helper(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline

    _write_review_sources(tmp_path)
    calls = []

    def fake_moment_text(video_dir, start, end):
        calls.append((video_dir.name, start, end))
        return f"texte {start}-{end}"

    monkeypatch.setattr(pipeline, "_moment_text", fake_moment_text)

    moments = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/moments").json()

    assert calls == [(VIDEO_ID, 2.0, 26.0), (VIDEO_ID, 40.0, 60.0)]
    assert moments[0]["transcript"] == "texte 2.0-26.0"


def test_list_moments_missing_transcript_is_reported_not_hidden(tmp_path, isolated_cwd):
    _write_review_sources(tmp_path, transcript=False)

    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/moments")

    assert resp.status_code == 200
    first = resp.json()[0]
    assert first["transcript"] is None
    assert "transcript.json" in first["transcript_error"]


def test_list_moments_missing_source_duration_is_reported_not_hidden(tmp_path, isolated_cwd):
    _write_review_sources(tmp_path, meta=False)

    first = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/moments").json()[0]

    assert first["source_duration"] is None
    assert "meta.json" in first["source_duration_error"]


def test_render_route_does_not_use_background_tasks():
    source = (Path(__file__).resolve().parent.parent / "clipper" / "web" / "app.py").read_text(encoding="utf-8")
    assert "BackgroundTasks" not in source
    assert "background_tasks" not in source


def _review_js() -> str:
    return (STATIC / "screens" / "review.js").read_text(encoding="utf-8")


def test_review_screen_is_wired_after_screens_js():
    page = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "/static/screens/review.js" in page
    assert page.index("/static/screens.js") < page.index("/static/screens/review.js")
    assert "Screens.review" in _review_js()


def test_review_screen_has_a_player_locked_on_the_selected_moment():
    js = _review_js()

    assert "<video" in js
    assert "preview_url" in js
    assert "currentTime" in js
    assert "timeupdate" in js  # le lecteur reste dans [debut, fin] du moment


def test_review_screen_has_a_timeline_with_handles_bound_to_numeric_fields():
    js = _review_js()

    assert 'data-handle="start"' in js and 'data-handle="end"' in js
    assert "pointerdown" in js
    assert 'type="number"' in js
    assert 'data-field="start"' in js and 'data-field="end"' in js
    assert "source_duration" in js


def test_review_screen_shows_the_jury_justification_and_transcript():
    js = _review_js()

    assert "justification" in js
    assert "transcript" in js


def test_review_screen_sends_decisions_to_decide_with_an_undo_toast():
    js = _review_js()

    assert "/decide" in js
    for decision in ("accepted", "rejected", "adjusted"):
        assert decision in js
    assert "undo:" in js  # toast 'Annuler'
    assert "previous" in js  # renvoie la decision precedente


def test_review_screen_has_keyboard_shortcuts_documented_in_the_screen():
    js = _review_js()

    assert 'addEventListener("keydown"' in js
    for key in ('"a"', '"r"', '"j"', '"k"', '" "'):
        assert key in js, key
    assert "<kbd>A</kbd>" in js and "<kbd>R</kbd>" in js
    assert "<kbd>J</kbd>" in js and "<kbd>K</kbd>" in js
    assert "<kbd>Espace</kbd>" in js


def test_review_screen_render_button_is_blocked_while_moments_await_a_decision():
    js = _review_js()

    assert "awaiting" in js
    assert "/api/videos/${" in js or "/api/videos/" in js
    assert "/render" in js
    assert "disabled" in js
    assert "sans décision" in js  # la raison affichee
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".review-layout" in css
# Ecran Clips (TASK-3b9c) : GET /api/clips, approve/reject, PATCH, rerender
# (publish et worker simules ; l'API ne touche jamais un mp4 ni un sidecar)
# --------------------------------------------------------------------------

CLIPS_VIDEO = "clipsvideo01"
READY = "ab12cd"   # compte pret (SPEC-00d1 R4) ; defini ici (avant _bulk_body) pour servir de defaut
SPARE = "ef34ab"   # second compte, pas forcement pret selon le test


def _clip_sidecar(clip_id, *, part=1, parts_total=1, ready=True, qa_status="passed", issues=None, **extra):
    return {
        "video_id": CLIPS_VIDEO, "clip_id": clip_id, "part": part, "parts_total": parts_total,
        "screen_title": f"Titre {clip_id}", "title": f"Titre {clip_id}", "caption": f"Description {clip_id}",
        "hashtags": ["#ma_chaine"], "ready": ready, "layout": "letterbox", "duration": 24.0, "score": 80.0,
        "qa": {"status": qa_status, "issues": issues or []}, **extra,
    }


def _write_clip(tmp_path, video_id, sidecar):
    out_dir = tmp_path / "output" / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{sidecar['clip_id']}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    (out_dir / f"{sidecar['clip_id']}.mp4").write_bytes(b"fake-mp4")


def _write_publish(tmp_path, channel, entries):
    path = tmp_path / "state" / "publish" / f"{channel}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _entry(clip_id, status, video_id=CLIPS_VIDEO, **extra):
    return {"video_id": video_id, "clip_id": clip_id, "series_id": None, "part": None, "status": status,
            "slot_at": None, "decided_at": None, "published_at": None, "error": None, "account": "ab12cd", **extra}


def _clips_setup(tmp_path):
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_state(tmp_path, "othervideo01", channel="autre")
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01"))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("02", qa_status="rejected", ready=False, issues=["sous-titres hors zone"]))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("03"))
    _write_clip(tmp_path, "othervideo01", {**_clip_sidecar("01"), "video_id": "othervideo01"})
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "scheduled", slot_at="2026-10-02T18:00:00+00:00"),
        _entry("03", "failed", error="quota depasse"),
    ])


def test_get_clips_returns_sidecar_url_qa_and_publish_status(tmp_path, isolated_cwd):
    _clips_setup(tmp_path)

    resp = client(tmp_path).get("/api/clips", params={"video_id": CLIPS_VIDEO})

    assert resp.status_code == 200
    clips = {c["clip_id"]: c for c in resp.json()}
    assert sorted(clips) == ["01", "02", "03"]
    first = clips["01"]
    assert first["video_url"] == f"/media/clip/{CLIPS_VIDEO}/01"
    assert first["screen_title"] == "Titre 01"
    assert first["description"] == "Description 01"
    assert first["hashtags"] == ["#ma_chaine"]
    assert first["part"] == 1 and first["parts_total"] == 1
    assert first["channel"] == "ma_chaine"
    assert first["qa_status"] == "passed" and first["issues"] == []
    assert first["publish_status"] == "scheduled"
    assert first["slot_at"] == "2026-10-02T18:00:00+00:00"
    assert clips["02"]["qa_status"] == "rejected"
    assert clips["02"]["issues"] == ["sous-titres hors zone"]
    assert clips["02"]["publish_status"] == "rejected"               # refusé par la QA : jamais « à valider »
    assert clips["03"]["publish_status"] == "failed"
    assert clips["03"]["publish_error"] == "quota depasse"


def test_get_clips_filters_by_channel_video_and_status(tmp_path, isolated_cwd):
    _clips_setup(tmp_path)
    c = client(tmp_path)

    by_channel = c.get("/api/clips", params={"channel": "autre"}).json()
    assert [(x["video_id"], x["clip_id"]) for x in by_channel] == [("othervideo01", "01")]
    assert len(c.get("/api/clips").json()) == 4
    by_status = c.get("/api/clips", params={"status": "à valider"}).json()
    assert {(x["video_id"], x["clip_id"]) for x in by_status} == {("othervideo01", "01")}
    assert [x["clip_id"] for x in c.get("/api/clips", params={"status": "failed", "video_id": CLIPS_VIDEO}).json()] == ["03"]


def test_get_clips_rejects_an_unknown_status_and_an_unsafe_video_id(tmp_path, isolated_cwd):
    c = client(tmp_path)
    bad = c.get("/api/clips", params={"status": "bogus"})
    assert bad.status_code == 400 and "bogus" in bad.json()["detail"]
    assert c.get("/api/clips", params={"video_id": "../x"}).status_code == 400


def test_get_clips_empty_output_is_an_empty_list(tmp_path, isolated_cwd):
    assert client(tmp_path).get("/api/clips").json() == []


def test_get_clips_corrupt_publish_file_is_a_french_error_not_a_silent_status(tmp_path, isolated_cwd):
    _clips_setup(tmp_path)
    (tmp_path / "state" / "publish" / "ma_chaine.json").write_text("{pas du json", encoding="utf-8")

    resp = client(tmp_path).get("/api/clips")

    assert resp.status_code == 500
    assert "ma_chaine.json" in resp.json()["detail"]


def test_approve_and_reject_call_publish(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _clips_setup(tmp_path)
    _accounts_state(tmp_path)
    calls = []
    monkeypatch.setattr(publish, "approve", lambda *a, **kw: calls.append(("approve", a, kw)) or _entry("01", "scheduled"))
    monkeypatch.setattr(publish, "reject", lambda *a, **kw: calls.append(("reject", a, kw)) or _entry("01", "rejected"))
    c = client(tmp_path)

    ok = c.post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": READY})
    no = c.post(f"/api/clips/{CLIPS_VIDEO}/01/reject")

    assert ok.status_code == 200 and ok.json()["status"] == "scheduled"
    assert no.status_code == 200 and no.json()["status"] == "rejected"
    assert [(name, args) for name, args, _ in calls] == [
        ("approve", (CLIPS_VIDEO, "01", "ma_chaine")), ("reject", (CLIPS_VIDEO, "01", "ma_chaine"))]
    assert calls[0][2]["output_dir"] == tmp_path / "output"
    assert calls[0][2]["account"] == READY  # le compte choisi, pas celui d'un style
    assert calls[0][2]["schedule"] == {"slots": [{"day": "mon", "time": "18:30"}, {"day": "thu", "time": "12:00"}],
                                       "timezone": "Europe/Paris"}  # les creneaux du compte


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_approve_reject_publish_error_is_409_with_detail(tmp_path, isolated_cwd, monkeypatch, action):
    from clipper import publish

    _clips_setup(tmp_path)
    _accounts_state(tmp_path)

    def boom(*a, **kw):
        raise publish.PublishError("clip non pret pour publication : x/02")

    monkeypatch.setattr(publish, action, boom)

    resp = client(tmp_path).post(f"/api/clips/{CLIPS_VIDEO}/02/{action}", json={"account": READY})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "clip non pret pour publication : x/02"


def test_approve_a_clip_of_a_video_without_channel_is_409(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _write_state(tmp_path, CLIPS_VIDEO)
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01"))
    monkeypatch.setattr(publish, "approve", lambda *a, **kw: pytest.fail("publish.approve ne doit pas etre appele"))

    resp = client(tmp_path).post(f"/api/clips/{CLIPS_VIDEO}/01/approve")

    assert resp.status_code == 409
    assert "style" in resp.json()["detail"]


def test_patch_caption_calls_edit_caption_and_never_writes_the_sidecar(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish, worker

    _clips_setup(tmp_path)
    sidecar_path = tmp_path / "output" / CLIPS_VIDEO / "02.json"
    before = sidecar_path.read_bytes()
    calls = []

    def fake_edit(video_id, clip_id, channel, description, hashtags, **kw):
        calls.append((video_id, clip_id, channel, description, hashtags, kw))
        return {**_clip_sidecar("02"), "caption": description, "hashtags": hashtags, "edited_at": "2026-10-01T00:00:00+00:00"}

    monkeypatch.setattr(publish, "edit_caption", fake_edit)
    monkeypatch.setattr(worker, "enqueue", lambda *a, **kw: pytest.fail("pas de re-rendu pour un simple texte"))

    resp = client(tmp_path).patch(f"/api/clips/{CLIPS_VIDEO}/02", json={"description": "Nouveau texte", "hashtags": ["#a", "#b"]})

    assert resp.status_code == 200
    assert calls[0][:5] == (CLIPS_VIDEO, "02", "ma_chaine", "Nouveau texte", ["#a", "#b"])
    body = resp.json()
    assert body["clip"]["description"] == "Nouveau texte"
    assert body["clip"]["hashtags"] == ["#a", "#b"]
    assert body["rerender"] is None
    assert sidecar_path.read_bytes() == before


def test_patch_publish_error_is_409(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _clips_setup(tmp_path)

    def boom(*a, **kw):
        raise publish.PublishError("edition refusee pour x/01 : statut 'scheduled'")

    monkeypatch.setattr(publish, "edit_caption", boom)

    resp = client(tmp_path).patch(f"/api/clips/{CLIPS_VIDEO}/01", json={"description": "x", "hashtags": []})

    assert resp.status_code == 409
    assert "scheduled" in resp.json()["detail"]


def test_patch_with_nothing_to_change_is_400(tmp_path, isolated_cwd):
    _clips_setup(tmp_path)
    assert client(tmp_path).patch(f"/api/clips/{CLIPS_VIDEO}/01", json={}).status_code == 400


def test_patch_screen_title_enqueues_a_targeted_render_after_confirmation(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    _clips_setup(tmp_path)
    calls = []

    def fake_enqueue(url, channel, action, force_steps, *, config=None):
        calls.append((url, channel, action, force_steps))
        return {"id": "e1", "video_id": url, "channel": channel, "action": action,
                "force_steps": force_steps, "status": "waiting"}

    monkeypatch.setattr(worker, "enqueue", fake_enqueue)
    c = client(tmp_path)

    refused = c.patch(f"/api/clips/{CLIPS_VIDEO}/01", json={"screen_title": "Nouveau titre"})
    assert refused.status_code == 409 and "confirm" in refused.json()["detail"]
    assert calls == []

    resp = c.patch(f"/api/clips/{CLIPS_VIDEO}/01", json={"screen_title": "Nouveau titre", "confirm": True})

    assert resp.status_code == 202
    assert calls == [(CLIPS_VIDEO, "ma_chaine", "render", ["render", "qa"])]
    assert resp.json()["rerender"]["action"] == "render"


def test_patch_caption_and_screen_title_does_both(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish, worker

    _clips_setup(tmp_path)
    edits, queued = [], []
    monkeypatch.setattr(publish, "edit_caption",
                        lambda *a, **kw: edits.append(a) or {**_clip_sidecar("01"), "caption": a[3], "hashtags": a[4]})
    monkeypatch.setattr(worker, "enqueue", lambda url, channel, action, force_steps, *, config=None:
                        queued.append((url, action, force_steps)) or {"id": "e", "video_id": url, "action": action})

    resp = client(tmp_path).patch(f"/api/clips/{CLIPS_VIDEO}/01", json={
        "description": "d", "hashtags": ["#x"], "screen_title": "T", "confirm": True})

    assert resp.status_code == 202
    assert edits == [(CLIPS_VIDEO, "01", "ma_chaine", "d", ["#x"])]
    assert queued == [(CLIPS_VIDEO, "render", ["render", "qa"])]


def test_patch_same_screen_title_does_not_rerender(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    _clips_setup(tmp_path)
    monkeypatch.setattr(worker, "enqueue", lambda *a, **kw: pytest.fail("titre inchange : pas de re-rendu"))

    resp = client(tmp_path).patch(f"/api/clips/{CLIPS_VIDEO}/01", json={"screen_title": "Titre 01", "confirm": True})

    assert resp.status_code == 400
    assert "inchangé" in resp.json()["detail"]


def test_rerender_enqueues_render_and_qa_for_the_clip(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    _clips_setup(tmp_path)
    calls = []
    monkeypatch.setattr(worker, "enqueue", lambda url, channel, action, force_steps, *, config=None:
                        calls.append((url, channel, action, force_steps)) or
                        {"id": "e1", "video_id": url, "action": action, "force_steps": force_steps})

    resp = client(tmp_path).post(f"/api/clips/{CLIPS_VIDEO}/01/rerender")

    assert resp.status_code == 202
    assert calls == [(CLIPS_VIDEO, "ma_chaine", "render", ["render", "qa"])]


def test_rerender_already_queued_is_409(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    _clips_setup(tmp_path)

    def boom(*a, **kw):
        raise worker.WorkerError("deja en file d'attente : x (render)")

    monkeypatch.setattr(worker, "enqueue", boom)

    resp = client(tmp_path).post(f"/api/clips/{CLIPS_VIDEO}/01/rerender")

    assert resp.status_code == 409 and "deja en file" in resp.json()["detail"]


def test_clip_routes_reject_unsafe_ids(tmp_path, isolated_cwd):
    c = client(tmp_path)
    assert c.post(f"/api/clips/{CLIPS_VIDEO}/..%2Fx/approve").status_code in (400, 404)
    assert c.post("/api/clips/bad.id/01/approve").status_code == 400


def test_clip_media_route_still_serves_the_mp4(tmp_path, isolated_cwd):
    _clips_setup(tmp_path)
    resp = client(tmp_path).get(f"/media/clip/{CLIPS_VIDEO}/01")
    assert resp.status_code == 200 and resp.content == b"fake-mp4"


def test_clips_screen_is_wired_with_gallery_sidecar_and_actions():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")

    assert "/static/screens/clips.js" in page
    assert page.index("/static/screens.js") < page.index("/static/screens/clips.js")
    assert "Screens.clips" in js
    assert "/api/clips" in js
    assert "<video" in js and "controls" in js           # lecteur
    assert "clip-poster" in js and "aspect" in (STATIC / "style.css").read_text(encoding="utf-8")
    for label in ("Titre d'écran", "Description", "Hashtags", "Contrôle qualité", "Partie"):
        assert label in js, label
    for action in ("/approve", "/reject", "/rerender", "PATCH"):
        assert action in js, action
    assert "toute la série" in js                        # refus d'une partie
    assert "download" in js and "video_url" in js        # lien de telechargement
    assert "copyText" in js                              # description + hashtags
    assert "undo" in js                                  # toast « Annuler »
    assert "confirm: true" in js                         # re-rendu confirmé
    assert "Aucun clip" in js                            # etat vide


# --------------------------------------------------------------------------
# Approbation groupee (TASK-e99b) : POST /api/clips/approve
# --------------------------------------------------------------------------


def _bulk_body(clips, account=READY):
    return {"clips": [{"video_id": v, "clip_id": c} for v, c in clips], "account": account}


def test_bulk_approve_applies_the_same_decision_as_the_single_endpoint_for_each_clip(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _clips_setup(tmp_path)
    # 01 sans entree (planifie, approve le refuserait : la pre-validation aussi, revue fable-comptes 2)
    _write_publish(tmp_path, "ma_chaine", [_entry("03", "failed", error="quota depasse")])
    _accounts_state(tmp_path)
    calls = []
    monkeypatch.setattr(publish, "approve", lambda *a, **kw: calls.append((a, kw)) or _entry(a[1], "scheduled"))

    resp = client(tmp_path).post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01"), (CLIPS_VIDEO, "03")]))

    assert resp.status_code == 200
    assert [c["clip_id"] for c in resp.json()] == ["01", "03"]
    assert [a for a, _ in calls] == [(CLIPS_VIDEO, "01", "ma_chaine"), (CLIPS_VIDEO, "03", "ma_chaine")]
    for _, kw in calls:
        assert kw["account"] == READY                    # le compte choisi, le meme pour chaque clip
        assert kw["output_dir"] == tmp_path / "output"
        assert kw["schedule"] == {"slots": [{"day": "mon", "time": "18:30"}, {"day": "thu", "time": "12:00"}],
                                   "timezone": "Europe/Paris"}  # les creneaux du compte, les memes pour chaque clip


def test_bulk_approve_without_a_ready_account_refuses_and_approves_nothing(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _clips_setup(tmp_path)
    _accounts_state(tmp_path)
    monkeypatch.setattr(publish, "approve", lambda *a, **kw: pytest.fail("publish.approve ne doit pas etre appele"))
    c = client(tmp_path)

    missing = c.post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01")], account=None))
    not_ready = c.post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01")], account=SPARE))
    unknown = c.post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01")], account="fantome"))

    assert missing.status_code == 409 and "compte de publication manquant" in missing.json()["detail"]
    assert not_ready.status_code == 409 and "non prêt à publier" in not_ready.json()["detail"]
    assert unknown.status_code == 409 and "compte inconnu" in unknown.json()["detail"]


def test_bulk_approve_an_unknown_clip_refuses_the_whole_batch(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _clips_setup(tmp_path)
    _accounts_state(tmp_path)
    monkeypatch.setattr(publish, "approve", lambda *a, **kw: pytest.fail("publish.approve ne doit pas etre appele"))
    state_path = tmp_path / "state" / "publish" / "ma_chaine.json"
    before = state_path.read_text(encoding="utf-8")

    resp = client(tmp_path).post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01"), (CLIPS_VIDEO, "no-such")]))

    assert resp.status_code == 409
    assert any("no-such" in r for r in resp.json()["refused"])
    assert state_path.read_text(encoding="utf-8") == before  # tout ou rien : rien approuve


def test_bulk_approve_an_already_published_clip_refuses_the_whole_batch(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01"))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("04"))
    _write_publish(tmp_path, "ma_chaine", [_entry("04", "published")])
    _accounts_state(tmp_path)
    monkeypatch.setattr(publish, "approve", lambda *a, **kw: pytest.fail("publish.approve ne doit pas etre appele"))
    state_path = tmp_path / "state" / "publish" / "ma_chaine.json"
    before = state_path.read_text(encoding="utf-8")

    resp = client(tmp_path).post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01"), (CLIPS_VIDEO, "04")]))

    assert resp.status_code == 409
    assert any("04" in r and "published" in r for r in resp.json()["refused"])
    assert state_path.read_text(encoding="utf-8") == before


def test_bulk_approve_an_already_rejected_clip_refuses_the_whole_batch(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01"))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("04"))
    _write_publish(tmp_path, "ma_chaine", [_entry("04", "rejected")])
    _accounts_state(tmp_path)
    monkeypatch.setattr(publish, "approve", lambda *a, **kw: pytest.fail("publish.approve ne doit pas etre appele"))
    state_path = tmp_path / "state" / "publish" / "ma_chaine.json"
    before = state_path.read_text(encoding="utf-8")

    resp = client(tmp_path).post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01"), (CLIPS_VIDEO, "04")]))

    assert resp.status_code == 409
    assert any("04" in r and "rejected" in r for r in resp.json()["refused"])
    assert state_path.read_text(encoding="utf-8") == before


def test_bulk_approve_a_not_ready_clip_refuses_the_whole_batch(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01"))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("04", ready=False))
    _accounts_state(tmp_path)
    monkeypatch.setattr(publish, "approve", lambda *a, **kw: pytest.fail("publish.approve ne doit pas etre appele"))

    resp = client(tmp_path).post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01"), (CLIPS_VIDEO, "04")]))

    assert resp.status_code == 409
    assert any("04" in r for r in resp.json()["refused"])


def test_clips_screen_has_a_multi_select_bulk_approve_bar():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")

    assert "Sélectionner" in js
    assert "/api/clips/approve" in js
    assert "Approuver la sélection" in js
    assert "Annuler" in js
    assert "toute sa série" in js or "toute la série" in js  # message : cocher une partie coche la serie


def test_bulk_approve_selecting_one_part_approves_the_whole_series_in_part_order(tmp_path, isolated_cwd):
    (tmp_path / "config.toml").write_text('mode = "review"\n', encoding="utf-8")
    (tmp_path / "presets").mkdir(exist_ok=True)
    (tmp_path / "presets" / "ma_chaine.toml").write_text('[channel]\n', encoding="utf-8")
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01-p1", part=1, parts_total=3))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01-p2", part=2, parts_total=3))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01-p3", part=3, parts_total=3))
    _accounts_state(tmp_path)

    resp = client(tmp_path).post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01-p2")]))  # une seule partie choisie

    assert resp.status_code == 200
    approved = resp.json()
    assert [c["clip_id"] for c in approved] == ["01-p1", "01-p2", "01-p3"]  # toute la serie, dans l'ordre
    assert all(c["status"] in ("approved", "scheduled") for c in approved)
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert {e["clip_id"] for e in entries} == {"01-p1", "01-p2", "01-p3"}


# --------------------------------------------------------------------------
# Revue Fable (TASK-4c3d) : fable-comptes 1 a 9, fable-publication I2, I3, M1, M3, M4
# --------------------------------------------------------------------------


def _fable_setup(tmp_path, channel="ma_chaine", clips=("01", "02", "03")):
    (tmp_path / "config.toml").write_text('mode = "review"\n', encoding="utf-8")
    (tmp_path / "presets").mkdir(exist_ok=True)
    (tmp_path / "presets" / "ma_chaine.toml").write_text("[channel]\n", encoding="utf-8")
    _write_state(tmp_path, CLIPS_VIDEO, channel=channel, status="done")
    for clip_id in clips:
        _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar(clip_id))
    _accounts_state(tmp_path)


def _publish_file(tmp_path, channel):
    path = tmp_path / "state" / "publish" / f"{channel}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def test_assigning_a_style_never_lets_a_published_clip_be_approved_again(tmp_path, isolated_cwd):
    """fable-comptes 1 (CRITIQUE) : apres « Attribuer un style », le clip publie sans style s'affichait « à valider »
    et « Approuver » le remettait en file : second post. Ses publications suivent la video, intactes."""
    _fable_setup(tmp_path, channel=None, clips=("01", "02"))
    published = _entry("01", "published", slot_at="2026-10-01T18:00:00+02:00", published_at="2026-10-01T16:00:04+00:00",
                       tiktok_state="published", post_url="https://example.invalid/@x/video/1", manual=True)
    _write_publish(tmp_path, "_sans_chaine", [published, _entry("02", "rejected")])
    c = client(tmp_path)

    assert c.post(f"/api/videos/{CLIPS_VIDEO}/channel", json={"channel": "ma_chaine"}).status_code == 200

    statuses = {x["clip_id"]: x["publish_status"] for x in c.get("/api/clips", params={"video_id": CLIPS_VIDEO}).json()}
    assert statuses == {"01": "published", "02": "rejected"}
    assert c.post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": READY}).status_code == 409
    assert c.post(f"/api/clips/{CLIPS_VIDEO}/02/approve", json={"account": READY}).status_code == 409
    assert [(e["clip_id"], e["status"]) for e in _publish_file(tmp_path, "ma_chaine")] == [("01", "published"),
                                                                                           ("02", "rejected")]
    assert _publish_file(tmp_path, "ma_chaine")[0] == published
    pubs = [(p["clip_id"], p["publish_status"]) for p in c.get("/api/publications").json()["publications"]]
    assert pubs == [("01", "published")]  # un seul ecran, une seule verite


@pytest.mark.parametrize("blocking", [
    {"status": "scheduled", "slot_at": "2026-10-12T18:30:00+02:00"},
    {"status": "scheduled", "slot_at": "2026-10-12T18:30:00+02:00", "in_progress_since": "2026-10-03T10:00:00+00:00"},
    {"status": "failed", "manual": True, "publish_mode": "immediate", "post_options": {"visibility": "friends"}},
])
def test_bulk_approve_is_all_or_nothing_when_approve_would_refuse_one_clip(tmp_path, isolated_cwd, blocking):
    """fable-comptes 2 / fable-publication M1 : la pre-validation ne refusait que published/rejected ; 01 etait
    approuve puis 409 sur 02 (planifie, en cours de pilotage, ou publication du formulaire)."""
    _fable_setup(tmp_path)
    _write_publish(tmp_path, "ma_chaine", [_entry("02", **blocking)])
    state_path = tmp_path / "state" / "publish" / "ma_chaine.json"
    before = state_path.read_text(encoding="utf-8")

    resp = client(tmp_path).post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01"), (CLIPS_VIDEO, "02"),
                                                                        (CLIPS_VIDEO, "03")]))

    assert resp.status_code == 409
    assert resp.json()["detail"] == "sélection refusée, rien d'approuvé"
    assert any(f"{CLIPS_VIDEO}/02" in r for r in resp.json()["refused"])
    assert state_path.read_text(encoding="utf-8") == before  # 01 n'a pas ete approuve avant le refus


def test_bulk_approve_skips_an_already_published_part_pulled_in_by_its_series(tmp_path, isolated_cwd):
    """fable-comptes 5 : cocher la partie 2 entrainait la partie 1 deja publiee, et tout etait refuse ; la partie
    publiee entrainee par la serie est laissee telle quelle, les suivantes s'approuvent."""
    _fable_setup(tmp_path, clips=())
    for part in (1, 2, 3):
        _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar(f"01-p{part}", part=part, parts_total=3))
    published = _entry("01-p1", "published", series_id=f"{CLIPS_VIDEO}:01", part=1,
                       slot_at="2026-10-01T18:30:00+02:00", published_at="2026-10-01T16:30:04+00:00")
    _write_publish(tmp_path, "ma_chaine", [published])

    resp = client(tmp_path).post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01-p2")]))

    assert resp.status_code == 200, resp.text
    assert [c["clip_id"] for c in resp.json()] == ["01-p2", "01-p3"]
    entries = {e["clip_id"]: e for e in _publish_file(tmp_path, "ma_chaine")}
    assert entries["01-p1"] == published
    assert entries["01-p2"]["status"] in ("approved", "scheduled")


def test_clip_drawer_offers_approve_only_to_a_clip_to_validate_or_approved():
    """fable-comptes 3 / fable-publication I3 : « Approuver » etait affiche quel que soit le statut."""
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")
    line = next(raw for raw in js.splitlines() if "data-approve>" in raw)

    assert 'publish_status === "à valider"' in line and 'publish_status === "approved"' in line


def test_deleting_a_style_keeps_its_publications_visible_and_counted(tmp_path, isolated_cwd):
    """fable-comptes 4 : apres suppression du style, ses publications publiees disparaissaient de l'ecran
    Publication (et des plafonds du compte, voir test_publish)."""
    _fable_setup(tmp_path, clips=("01",))
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "published", slot_at="2026-10-01T18:00:00+02:00",
                                                  published_at="2026-10-01T16:00:04+00:00")])
    c = client(tmp_path)

    assert c.delete("/api/channels/ma_chaine", params={"confirm": "true"}).status_code == 200

    pubs = [(p["clip_id"], p["publish_status"]) for p in c.get("/api/publications").json()["publications"]]
    assert pubs == [("01", "published")]


def test_deleting_a_style_with_an_unreadable_publish_file_is_an_explicit_409(tmp_path, isolated_cwd):
    """fable-comptes 4 : fichier de publication invalide -> 500 ; c'est une erreur explicite qui le nomme."""
    _fable_setup(tmp_path, clips=())
    (tmp_path / "state" / "publish").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "publish" / "ma_chaine.json").write_text("{pas du json", encoding="utf-8")

    resp = TestClient(create_app(config=make_config(tmp_path)), raise_server_exceptions=False).delete(
        "/api/channels/ma_chaine", params={"confirm": "true"})

    assert resp.status_code == 409 and "ma_chaine.json" in resp.json()["detail"]
    assert (tmp_path / "presets" / "ma_chaine.toml").exists()


def test_approving_a_clip_whose_style_preset_is_gone_is_a_404(tmp_path, isolated_cwd):
    """fable-comptes 7 : ChannelError non attrapee dans _decide -> 500."""
    _fable_setup(tmp_path, channel="disparu", clips=("01",))

    resp = TestClient(create_app(config=make_config(tmp_path)), raise_server_exceptions=False).post(
        f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": READY})

    assert resp.status_code == 404 and "disparu" in resp.json()["detail"]


def test_accounts_list_shows_a_write_error_on_the_account_instead_of_a_500(tmp_path, isolated_cwd, monkeypatch):
    """fable-comptes 8 : une AccountsError de record_login faisait tomber tout l'ecran Comptes."""
    from clipper import accounts, browser

    _browser_setup(tmp_path)
    monkeypatch.setattr(browser, "login_state", lambda account_id, config=None: {"state": "never"})

    def failing(config, account_id, observed):
        raise accounts.AccountsError("écriture du fichier des comptes impossible : state/accounts.json")

    monkeypatch.setattr(accounts, "record_login", failing)
    c = TestClient(create_app(config=make_config(tmp_path)), base_url="http://127.0.0.1:8000",
                   client=("127.0.0.1", 50000), raise_server_exceptions=False)

    resp = c.get("/api/accounts")

    assert resp.status_code == 200
    assert "écriture du fichier des comptes impossible" in resp.json()[0]["login_error"]


def test_cancel_a_process_that_cannot_be_stopped_is_a_409(tmp_path, isolated_cwd, monkeypatch):
    """fable-comptes 9 : « processus impossible à arrêter » rendait 404 « aucune video en cours »."""
    from clipper import worker

    def stuck(video_id, *, config=None):
        raise worker.WorkerError("processus 4242 impossible à arrêter : [WinError 5] Accès refusé")

    monkeypatch.setattr(worker, "cancel", stuck)

    resp = client(tmp_path).post(f"/api/videos/{VIDEO_ID}/cancel")

    assert resp.status_code == 409 and "impossible à arrêter" in resp.json()["detail"]


def test_declare_published_is_refused_while_the_worker_drives_the_entry(tmp_path, isolated_cwd):
    """fable-publication I2 : la route et le bouton « Déclarer publié » ignoraient le pilotage en cours."""
    _fable_setup(tmp_path, clips=("01",))
    driven = _entry("01", "scheduled", slot_at="2026-10-05T18:30:00+02:00",
                    in_progress_since="2026-10-03T10:00:00+00:00")
    _write_publish(tmp_path, "ma_chaine", [driven])

    resp = client(tmp_path).post(f"/api/publish/{CLIPS_VIDEO}/01/published")

    assert resp.status_code == 409 and "en cours" in resp.json()["detail"]
    assert _publish_file(tmp_path, "ma_chaine") == [driven]
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    line = next(raw for raw in js.splitlines() if "data-published " in raw)
    assert "inProgress" in line


def test_publications_are_sorted_by_instant_not_by_iso_text(tmp_path, isolated_cwd):
    """fable-publication M3 : 16:30+00:00 (18:30 Paris) etait classe apres 18:00+02:00."""
    _fable_setup(tmp_path, clips=("01", "02"))
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "scheduled", slot_at="2026-10-05T16:30:00+00:00"),  # 18:30 a Paris
        _entry("02", "scheduled", slot_at="2026-10-05T18:00:00+02:00"),  # 18:00 a Paris
    ])

    rows = client(tmp_path).get("/api/publications").json()["publications"]

    assert [r["clip_id"] for r in rows] == ["01", "02"]  # le plus tard d'abord


def test_set_mode_route_is_refused_while_the_worker_drives_the_entry(tmp_path, isolated_cwd):
    """fable-publication M4 : POST /mode acceptait une entree en cours de pilotage."""
    _fable_setup(tmp_path, clips=("01",))
    driven = _entry("01", "scheduled", slot_at="2026-10-05T18:30:00+02:00",
                    in_progress_since="2026-10-03T10:00:00+00:00", publish_mode="immediate")
    _write_publish(tmp_path, "ma_chaine", [driven])

    resp = client(tmp_path).post(f"/api/publish/{CLIPS_VIDEO}/01/mode", json={"mode": "scheduled"})

    assert resp.status_code == 409
    assert _publish_file(tmp_path, "ma_chaine") == [driven]


# --------------------------------------------------------------------------
# Ecran Chaines (SPEC-c100 E5, SPEC-fc0c §1) : API par chaine + page
# --------------------------------------------------------------------------

CH = "ma_chaine"
_CH_PRESET = (
    '[channel]\ndisplay_name = "Ma chaîne"\nwatch = true\n'
    '\n[reframe]\nletterbox_zoom = 1.5\n'
)


def _channels_setup(tmp_path, preset=_CH_PRESET, name=CH):
    (tmp_path / "config.toml").write_text('mode = "review"\n[render]\ncrf = 18\n', encoding="utf-8")
    (tmp_path / "presets").mkdir(exist_ok=True)
    (tmp_path / "presets" / f"{name}.toml").write_text(preset, encoding="utf-8")


def test_get_channel_returns_raw_effective_and_documented_defaults(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    data = client(tmp_path).get(f"/api/channels/{CH}").json()

    assert data["name"] == CH
    # ce que le preset redéfinit, tel quel (rien d'hérité)
    assert data["raw"]["channel"]["display_name"] == "Ma chaîne"
    assert data["raw"]["reframe"] == {"letterbox_zoom": 1.5}
    assert "render" not in data["raw"]
    # valeurs effectives : preset > config.toml > CONFIG_DEFAULTS
    assert data["effective"]["reframe"]["letterbox_zoom"] == 1.5
    assert data["effective"]["render"]["crf"] == 18          # hérité de config.toml
    assert data["effective"]["render"]["max_fps"] == 30       # hérité de CONFIG_DEFAULTS
    assert data["effective"]["channel"]["mode"] == "review"   # mode global
    assert data["effective"]["channel"]["timezone"] == "Europe/Paris"
    # les sections du formulaire sont toutes là
    for section in ("channel", "reframe", "render", "subtitles", "moments"):
        assert section in data["defaults"], section
    # défaut + commentaire de la ligne précédente dans le source
    fps = data["defaults"]["render"]["max_fps"]
    assert fps["default"] == 30 and "Cadence de sortie" in fps["comment"]
    assert data["defaults"]["render"]["crf"]["comment"] == ""     # pas de commentaire : vide, pas inventé
    assert data["defaults"]["channel"]["watch_interval_s"]["default"] == 1800


def test_get_channel_comments_come_from_source_without_importing_values(tmp_path, isolated_cwd):
    from clipper.web import app as web_app

    comments = web_app._defaults_documentation("moments")
    assert "Grille de notation" in comments["rubric_path"]["comment"]
    assert "jamais de repli" in comments["rubric_path"]["details"]    # reste du bloc de commentaires, replié
    assert comments["rubric_path"]["default"] == "rubric.toml"


def test_get_channel_unknown_is_404_and_invalid_name_422(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    assert c.get("/api/channels/autre").status_code == 404
    assert "autre" in c.get("/api/channels/autre").json()["detail"]
    assert c.get("/api/channels/Bad.Name").status_code == 422


def test_get_channel_without_config_toml_says_so(tmp_path, isolated_cwd):
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / f"{CH}.toml").write_text("[channel]\n", encoding="utf-8")
    resp = client(tmp_path).get(f"/api/channels/{CH}")
    assert resp.status_code == 422 and "config.toml" in resp.json()["detail"]


def test_put_channel_saves_through_save_channel(tmp_path, isolated_cwd, monkeypatch):
    _channels_setup(tmp_path)
    from clipper import channel as channel_mod

    calls = []
    real = channel_mod.save_channel

    def spy(name, data, **kwargs):
        calls.append((name, data, kwargs))
        return real(name, data, **kwargs)

    monkeypatch.setattr(channel_mod, "save_channel", spy)
    preset = {"channel": {"display_name": "Autre", "mode": "auto"}, "render": {"crf": 22}}
    resp = client(tmp_path).put(f"/api/channels/{CH}", json={"preset": preset})

    assert resp.status_code == 200, resp.text
    assert any(n == CH and d == preset and k["presets_dir"] == "presets" for n, d, k in calls)
    assert resp.json()["raw"] == preset
    assert resp.json()["effective"]["channel"]["mode"] == "auto"
    assert "crf = 22" in (tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8")


@pytest.mark.parametrize("preset, section, key", [
    ({"reframe": {"zzz": 1}}, "reframe", "zzz"),                            # clé inconnue (ConfigError)
    ({"channel": {"mode": "turbo"}}, "channel", "mode"),                    # mode invalide
    ({"channel": {"watch_interval_s": "vite"}}, "channel", "watch_interval_s"),   # mauvais type
    ({"channel": {"timezone": "Mars/Olympus"}}, "channel", "timezone"),
    ({"render": {"crf": True}}, "render", "crf"),                           # bool n'est pas un entier
])
def test_put_channel_invalid_is_422_naming_section_and_key_and_keeps_the_file(
    tmp_path, isolated_cwd, preset, section, key
):
    _channels_setup(tmp_path)
    path = tmp_path / "presets" / f"{CH}.toml"
    before = path.read_text(encoding="utf-8")

    resp = client(tmp_path).put(f"/api/channels/{CH}", json={"preset": preset})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert f"[{section}]" in detail and key in detail, detail
    assert path.read_text(encoding="utf-8") == before


def test_put_channel_config_error_from_save_channel_is_422(tmp_path, isolated_cwd, monkeypatch):
    _channels_setup(tmp_path)
    from clipper import channel as channel_mod
    from clipper.config import ConfigError

    def boom(*args, **kwargs):
        raise ConfigError("cle(s) inconnue(s) dans la section [render]: zzz")

    monkeypatch.setattr(channel_mod, "save_channel", boom)
    resp = client(tmp_path).put(f"/api/channels/{CH}", json={"preset": {"channel": {}}})
    assert resp.status_code == 422 and "[render]" in resp.json()["detail"] and "zzz" in resp.json()["detail"]


def test_put_channel_unknown_is_404_and_keeps_the_channel_table(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    assert c.put("/api/channels/autre", json={"preset": {"channel": {}}}).status_code == 404
    # un preset sans [channel] n'est plus une chaîne : le serveur garde la table
    assert c.put(f"/api/channels/{CH}", json={"preset": {"render": {"crf": 20}}}).status_code == 200
    assert c.get("/api/channels").json() == [CH]


def test_post_channel_creates_a_preset(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    resp = c.post("/api/channels", json={"name": "nouvelle", "preset": {"channel": {"source_url": "https://exemple.test/c"}}})

    assert resp.status_code == 201, resp.text
    assert resp.json()["raw"]["channel"]["source_url"] == "https://exemple.test/c"
    assert (tmp_path / "presets" / "nouvelle.toml").is_file()
    assert c.get("/api/channels").json() == [CH, "nouvelle"]
    # sans preset : une chaîne vide mais valide (la table [channel] existe)
    assert c.post("/api/channels", json={"name": "vide"}).status_code == 201
    assert "vide" in c.get("/api/channels").json()


def test_post_channel_invalid_name_is_422_existing_is_409_bad_preset_422(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    for bad in ("Ma Chaine", "../x", "", "a" * 41):
        resp = c.post("/api/channels", json={"name": bad})
        assert resp.status_code == 422, bad
        assert "nom" in resp.json()["detail"]
    assert c.post("/api/channels", json={"name": CH}).status_code == 409
    resp = c.post("/api/channels", json={"name": "autre", "preset": {"render": {"zzz": 1}}})
    assert resp.status_code == 422 and "zzz" in resp.json()["detail"]
    assert not (tmp_path / "presets" / "autre.toml").exists()


def test_delete_channel_requires_confirm(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    path = tmp_path / "presets" / f"{CH}.toml"

    refused = c.delete(f"/api/channels/{CH}")
    assert refused.status_code == 409 and "confirm" in refused.json()["detail"]
    assert c.delete(f"/api/channels/{CH}?confirm=false").status_code == 409
    assert path.exists()

    ok = c.delete(f"/api/channels/{CH}?confirm=true")
    assert ok.status_code == 200 and ok.json() == {"name": CH, "deleted": True}
    assert not path.exists()
    assert c.delete(f"/api/channels/{CH}?confirm=true").status_code == 404


def test_delete_channel_refuses_when_unfinished_publications_exist(tmp_path, isolated_cwd):
    """Revue r-comptes 7 : supprimer un style dont la file a des entrees non terminees -> 409 qui les liste
    (jamais abandonnees en silence) ; une entree terminee (published/rejected) ne bloque pas."""
    _channels_setup(tmp_path)
    _write_publish(tmp_path, CH, [
        _entry("01", "scheduled", slot_at="2026-10-05T18:30:00+02:00"),
        _entry("02", "published", published_at="2026-10-01T10:00:00+00:00"),
    ])
    c = client(tmp_path)
    path = tmp_path / "presets" / f"{CH}.toml"

    refused = c.delete(f"/api/channels/{CH}?confirm=true")

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert "non terminée" in detail and f"{CLIPS_VIDEO}/01" in detail
    assert f"{CLIPS_VIDEO}/02" not in detail  # publie : termine, ne bloque pas, pas liste
    assert path.exists()

    # une fois la seule entree non terminee refusee/annulee, la suppression passe
    _write_publish(tmp_path, CH, [
        _entry("01", "rejected"),
        _entry("02", "published", published_at="2026-10-01T10:00:00+00:00"),
    ])
    ok = c.delete(f"/api/channels/{CH}?confirm=true")
    assert ok.status_code == 200 and not path.exists()


def test_a_style_has_no_slots_route_any_more(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)

    assert client(tmp_path).get(f"/api/channels/{CH}/slots").status_code in (404, 405)  # les creneaux sont ceux du compte


_LEGACY_PRESET = (
    '[channel]\ndisplay_name = "Ma chaîne"\ntiktok_account = "ab12cd"\n'
    '[[channel.slots]]\nday = "mon"\ntime = "18:30"\n\n[reframe]\nletterbox_zoom = 1.5\n'
)


def _legacy_setup(tmp_path):
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "accounts.json").write_text(
        json.dumps({"accounts": [{"id": "ab12cd", "label": "Compte exemple"}]}), encoding="utf-8")
    _channels_setup(tmp_path, _LEGACY_PRESET)


def test_the_style_api_neither_returns_nor_accepts_an_account_or_slots(tmp_path, isolated_cwd):
    _legacy_setup(tmp_path)
    c = client(tmp_path)  # create_app migre le preset d'avant : creneaux sur le compte, cles retirees du fichier

    detail = c.get(f"/api/channels/{CH}").json()
    assert "tiktok_account" not in detail["raw"]["channel"] and "slots" not in detail["raw"]["channel"]
    assert "tiktok_account" not in detail["effective"]["channel"] and "slots" not in detail["effective"]["channel"]
    assert "tiktok_account" not in detail["defaults"]["channel"] and "slots" not in detail["defaults"]["channel"]

    for legacy in ({"tiktok_account": "ab12cd"}, {"slots": [{"day": "mon", "time": "18:30"}]}):
        preset = {"channel": {"display_name": "Ma chaîne", **legacy}}
        put = c.put(f"/api/channels/{CH}", json={"preset": preset})
        assert put.status_code == 422 and "n'existe plus dans un style" in put.json()["detail"]
        post = c.post("/api/channels", json={"name": "autre", "preset": preset})
        assert post.status_code == 422 and not (tmp_path / "presets" / "autre.toml").exists()


def test_starting_the_console_migrates_a_legacy_preset_onto_its_account(tmp_path, isolated_cwd):
    _legacy_setup(tmp_path)

    client(tmp_path)

    account = json.loads((tmp_path / "state" / "accounts.json").read_text(encoding="utf-8"))["accounts"][0]
    assert account["slots"] == [{"day": "mon", "time": "18:30"}]
    text = (tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8")
    assert "tiktok_account" not in text and "slots" not in text and 'display_name = "Ma chaîne"' in text


def _multipart(filename: str, content: bytes, field: str = "file") -> tuple[bytes, str]:
    boundary = "----clipperTest"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_channel_logo_multipart_upload_writes_presets_png(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    body, ctype = _multipart("logo.png", _PNG)
    resp = client(tmp_path).post(f"/api/channels/{CH}/logo", content=body, headers={"content-type": ctype})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"name": CH, "logo": f"presets/{CH}.png"}
    assert (tmp_path / "presets" / f"{CH}.png").read_bytes() == _PNG


def test_channel_logo_refuses_non_png_and_missing_file(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    body, ctype = _multipart("logo.png", b"GIF89a-pas-un-png")
    resp = c.post(f"/api/channels/{CH}/logo", content=body, headers={"content-type": ctype})
    assert resp.status_code == 422 and "PNG" in resp.json()["detail"]
    body, ctype = _multipart("logo.png", _PNG, field="autre")
    assert c.post(f"/api/channels/{CH}/logo", content=body, headers={"content-type": ctype}).status_code == 422
    assert c.post(f"/api/channels/{CH}/logo", content=b"x", headers={"content-type": "text/plain"}).status_code == 422
    body, ctype = _multipart("logo.png", _PNG)
    assert c.post("/api/channels/autre/logo", content=body, headers={"content-type": ctype}).status_code == 404
    assert not (tmp_path / "presets" / f"{CH}.png").exists()


def test_channels_screen_is_wired_with_list_form_inheritance_and_toast():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "screens" / "channels.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert "/static/screens/channels.js" in page
    assert page.index("/static/screens.js") < page.index("/static/screens/channels.js")
    assert "Screens.channels" in js
    # liste : nom, source, surveillance, mode, prochains créneaux
    for label in ("source_url", "Surveillance", "Mode"):
        assert label in js, label
    for gone in ("Prochains créneaux", "/slots", "tiktok_account", "Compte TikTok", "data-chan-add-slot"):
        assert gone not in js, gone                    # ni compte ni créneaux dans un style (SPEC-6076 R2)
    # formulaire par sections
    for section in ("channel", "reframe", "render", "subtitles", "moments"):
        assert f'"{section}"' in js, section
    for title in ("Agencement", "Titre", "Sous-titres", "Grille"):
        assert title in js, title
    # valeur héritée grisée + « redéfinir », erreur au champ, enregistrement avec toast
    assert "redéfinir" in js and "inherited" in js and "data-redefine" in js
    assert "field-error" in js
    assert "jsonBody(\"PUT\"" in js and "jsonBody(\"POST\"" in js
    assert 'method: "DELETE"' in js and "confirm=true" in js and "confirmDialog" in js
    assert "toast(" in js and "Style enregistré" in js
    assert "Aucun style" in js                       # état vide
    assert "FormData" in js and "/logo" in js          # envoi du logo
    assert ".chan-" in css and ".inherited" in css
# Surveillance : VOD à confirmer (TASK-7508, SPEC-fc0c §5, SPEC-c100)
# --------------------------------------------------------------------------

WATCH_VOD_A = "ddddddddddd"
WATCH_VOD_B = "eeeeeeeeeee"


def _watch_setup(tmp_path) -> None:
    vods = [{"video_id": v, "url": f"https://youtu.be/{v}", "title": f"Direct {v}", "duration_s": 7200,
             "published_at": "2026-01-01T20:00:00+00:00", "found_at": "2026-01-02T01:00:00+00:00"}
            for v in (WATCH_VOD_A, WATCH_VOD_B)]
    _write_json(tmp_path / "state" / "watch" / "ma_chaine.json",
                {"checked_at": "2026-01-02T01:00:00+00:00", "seen": [], "pending": vods, "last_error": None})


def _watch_state(tmp_path) -> dict:
    return json.loads((tmp_path / "state" / "watch" / "ma_chaine.json").read_text(encoding="utf-8"))


def test_watch_confirm_enqueues_the_vod_and_removes_it_from_pending(tmp_path, isolated_cwd):
    _watch_setup(tmp_path)

    resp = client(tmp_path).post(f"/api/watch/ma_chaine/{WATCH_VOD_A}/confirm")

    assert resp.status_code == 202
    assert resp.json()["video_id"] == WATCH_VOD_A
    queue = json.loads((tmp_path / "state" / "queue.json").read_text(encoding="utf-8"))
    assert [(e["video_id"], e["action"], e["channel"]) for e in queue] == [(WATCH_VOD_A, "run", "ma_chaine")]
    state = _watch_state(tmp_path)
    assert [v["video_id"] for v in state["pending"]] == [WATCH_VOD_B]
    assert WATCH_VOD_A in state["seen"]
    assert [v["video_id"] for v in _dashboard(tmp_path)["watch_pending"]] == [WATCH_VOD_B]


def test_watch_ignore_marks_the_vod_seen_without_enqueueing(tmp_path, isolated_cwd):
    _watch_setup(tmp_path)

    resp = client(tmp_path).post(f"/api/watch/ma_chaine/{WATCH_VOD_B}/ignore")

    assert resp.status_code == 200
    assert not (tmp_path / "state" / "queue.json").exists()
    state = _watch_state(tmp_path)
    assert [v["video_id"] for v in state["pending"]] == [WATCH_VOD_A]
    assert state["seen"] == [WATCH_VOD_B]


@pytest.mark.parametrize("action", ["confirm", "ignore"])
def test_watch_unknown_vod_is_a_404_with_a_french_detail(tmp_path, isolated_cwd, action):
    _watch_setup(tmp_path)

    resp = client(tmp_path).post(f"/api/watch/ma_chaine/zzzzzzzzzzz/{action}")

    assert resp.status_code == 404
    assert "zzzzzzzzzzz" in resp.json()["detail"]
    assert len(_watch_state(tmp_path)["pending"]) == 2


@pytest.mark.parametrize("action", ["confirm", "ignore"])
def test_watch_invalid_channel_name_is_a_400(tmp_path, isolated_cwd, action):
    resp = client(tmp_path).post(f"/api/watch/Ma%20Chaine/{WATCH_VOD_A}/{action}")
    assert resp.status_code == 400


@pytest.mark.parametrize("action", ["confirm", "ignore"])
def test_watch_channel_without_state_file_is_a_404(tmp_path, isolated_cwd, action):
    resp = client(tmp_path).post(f"/api/watch/ma_chaine/{WATCH_VOD_A}/{action}")
    assert resp.status_code == 404


def test_watch_confirm_with_a_vod_already_waiting_in_the_queue_is_not_an_error(tmp_path, isolated_cwd):
    _watch_setup(tmp_path)
    c = client(tmp_path)
    c.post("/api/queue", json={"url": f"https://youtu.be/{WATCH_VOD_A}", "action": "run"})

    resp = c.post(f"/api/watch/ma_chaine/{WATCH_VOD_A}/confirm")

    assert resp.status_code == 202
    assert len(json.loads((tmp_path / "state" / "queue.json").read_text(encoding="utf-8"))) == 1


def test_dashboard_vod_section_is_wired_to_confirm_and_ignore():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    watch_js = (STATIC / "screens" / "watch.js").read_text(encoding="utf-8")
    dash_js = (STATIC / "screens" / "dashboard.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert "/static/screens/watch.js" in page
    assert page.index("/static/screens.js") < page.index("/static/screens/watch.js") < page.index("/static/app.js")
    assert "/confirm" in watch_js and "/ignore" in watch_js
    assert 'method: "POST"' in watch_js
    assert "Confirmer" in watch_js and "Ignorer" in watch_js and "Voir la VOD" in watch_js
    assert "confirmDialog" in watch_js                   # ignorer : confirmation
    assert "toastError" in watch_js                      # erreur affichée, jamais avalée
    assert "watchVodRow" in dash_js and "VOD à confirmer" in dash_js
    assert "TASK-7508" in css


# --------------------------------------------------------------------------
# Ecran Publication (TASK-503d, SPEC-c100 E6, SPEC-fc0c §4) : GET /api/publish,
# move / published / unschedule (publish simulé ou state/ temporaire)
# --------------------------------------------------------------------------

PUB_WEEK = "2026-10-05"                       # lundi ; créneaux : lun 18:30, jeu 12:00 (Europe/Paris, +02:00)
PUB_MON = "2026-10-05T18:30:00+02:00"
PUB_THU = "2026-10-08T12:00:00+02:00"
PUB_NEXT_MON = "2026-10-12T18:30:00+02:00"


def _publish_setup(tmp_path, entries=None, *, slots=True):
    (tmp_path / "state").mkdir(exist_ok=True)
    # les creneaux sont ceux du compte (SPEC-6076 R2) ; le style n'a ni compte ni creneaux
    account = {"id": "ab12cd", "label": "Compte exemple", "platform": "TikTok",
               "slots": [{"day": "mon", "time": "18:30"}, {"day": "thu", "time": "12:00"}] if slots else [],
               "timezone": "Europe/Paris"}
    (tmp_path / "state" / "accounts.json").write_text(json.dumps({"accounts": [account]}), encoding="utf-8")
    _channels_setup(tmp_path, '[channel]\ndisplay_name = "Ma chaîne"\n')
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    for clip_id in ("01", "02", "03", "04", "05", "06"):
        _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar(clip_id))
    _write_publish(tmp_path, "ma_chaine", entries if entries is not None else [])


def _get_publish(tmp_path, **params):
    params = {"account": "ab12cd", "week": PUB_WEEK, **params}
    return client(tmp_path).get("/api/publish", params=params)


def test_get_publish_returns_week_slots_with_clip_or_free(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_THU)])

    resp = _get_publish(tmp_path)

    assert resp.status_code == 200
    data = resp.json()
    assert data["account"] == "ab12cd" and "channel" not in data           # les créneaux sont ceux du compte, plus d'un style
    assert data["timezone"] == "Europe/Paris"
    assert data["week_start"] == "2026-10-05" and data["week_end"] == "2026-10-11"
    assert [s["slot_at"] for s in data["slots"]] == [PUB_MON, PUB_THU]
    mon, thu = data["slots"]
    assert mon["clip"] is None and mon["free"] is True
    assert thu["free"] is False
    assert (thu["clip"]["clip_id"], thu["clip"]["publish_status"]) == ("01", "scheduled")
    assert thu["clip"]["video_url"] == f"/media/clip/{CLIPS_VIDEO}/01"      # télécharger
    assert thu["clip"]["description"] == "Description 01" and thu["clip"]["hashtags"] == ["#ma_chaine"]


def test_get_publish_lists_approved_without_slot_and_published_failed_of_the_week(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [
        _entry("01", "approved"),
        _entry("02", "published", slot_at=PUB_MON, published_at="2026-10-05T18:40:00+02:00"),
        _entry("03", "failed", slot_at=PUB_THU, error="quota depasse"),
        _entry("04", "published", slot_at="2026-09-28T18:30:00+02:00", published_at="2026-09-28T19:00:00+02:00"),
        _entry("05", "rejected"),
    ])

    data = _get_publish(tmp_path).json()

    assert [c["clip_id"] for c in data["unscheduled"]] == ["01"]
    assert sorted((c["clip_id"], c["publish_status"]) for c in data["done"]) == [("02", "published"), ("03", "failed")]
    assert [s["clip"]["clip_id"] for s in data["slots"]] == ["02", "03"]     # leur créneau les affiche
    assert next(c for c in data["done"] if c["clip_id"] == "03")["publish_error"] == "quota depasse"


def test_get_publish_places_a_service_scheduled_post_in_the_week_of_its_slot_not_its_decision(tmp_path, isolated_cwd):
    # programme vendredi (PUB_MON de la semaine courante) un post dont le créneau choisi est la semaine suivante
    # (PUB_NEXT_MON) : la décision (published_at) et le direct (slot_at / tiktok_publish_at) sont dans des
    # semaines différentes, la case doit suivre le direct, pas la décision.
    _publish_setup(tmp_path, [
        _entry("01", "published", slot_at=PUB_NEXT_MON, published_at=PUB_MON,
               tiktok_state="scheduled_on_tiktok", tiktok_publish_at=PUB_NEXT_MON),
    ])

    this_week = _get_publish(tmp_path).json()
    next_week = _get_publish(tmp_path, week="2026-10-14").json()

    assert this_week["done"] == []
    assert [c["clip_id"] for c in next_week["done"]] == ["01"]


def test_get_publish_week_is_taken_from_the_requested_week_and_defaults_to_this_one(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_NEXT_MON)])

    this = _get_publish(tmp_path).json()
    nxt = _get_publish(tmp_path, week="2026-10-14").json()                 # un mercredi : on tombe sur sa semaine
    now = client(tmp_path).get("/api/publish", params={"account": "ab12cd"}).json()

    assert all(s["clip"] is None for s in this["slots"])
    assert nxt["week_start"] == "2026-10-12"
    assert nxt["slots"][0]["slot_at"] == PUB_NEXT_MON and nxt["slots"][0]["clip"]["clip_id"] == "01"
    assert now["week_start"] <= now["week_end"] and len(now["slots"]) == 2


def test_get_publish_channel_without_slots_says_why(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "approved")], slots=False)

    data = _get_publish(tmp_path).json()

    assert data["slots"] == [] and "créneau" in data["reason"]
    assert [c["clip_id"] for c in data["unscheduled"]] == ["01"]            # « sans créneau »


def test_get_publish_errors_are_french(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    c = client(tmp_path)

    assert c.get("/api/publish").status_code == 200                         # sans compte : « tous les comptes »
    unknown = c.get("/api/publish", params={"account": "inconnu"})
    assert unknown.status_code == 404 and "compte" in unknown.json()["detail"]
    assert c.get("/api/publish", params={"account": "Bad Name"}).status_code in (400, 404, 422)
    bad = c.get("/api/publish", params={"account": "ab12cd", "week": "demain"})
    assert bad.status_code == 400 and "week" in bad.json()["detail"]


def test_get_publish_corrupt_publish_file_is_a_500_not_an_empty_week(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    (tmp_path / "state" / "publish" / "ma_chaine.json").write_text("{pas du json", encoding="utf-8")

    resp = _get_publish(tmp_path)

    assert resp.status_code == 500 and "publication" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Calendrier : vues Jour / Semaine / Mois (TASK-ad4d) : parametre ``range``
# explicite (day/week/month, 422 sinon), bornes calculees en Europe/Paris
# cote Python, regroupement par jour (``days``) sans jamais perdre une
# publication meme a la meme minute.
# --------------------------------------------------------------------------


def test_get_publish_rejects_invalid_range(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)

    resp = _get_publish(tmp_path, range="annee")

    assert resp.status_code == 422 and "range" in resp.json()["detail"]


def test_get_publish_defaults_to_week_range_with_bounds_unchanged(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_THU)])

    data = _get_publish(tmp_path).json()

    assert data["range"] == "week"
    assert data["range_start"] == data["week_start"] == "2026-10-05"
    assert data["range_end"] == data["week_end"] == "2026-10-11"
    assert len(data["days"]) == 7
    thu_box = data["days"][3]
    assert thu_box["date"] == "2026-10-08" and thu_box["count"] == 1
    assert thu_box["posts"][0]["clip_id"] == "01"


def test_get_publish_day_range_bounds_and_keeps_slot_grid(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_THU)])

    data = _get_publish(tmp_path, range="day", week="2026-10-08").json()

    assert data["range"] == "day"
    assert data["range_start"] == data["range_end"] == "2026-10-08"
    assert len(data["days"]) == 1
    assert data["days"][0]["date"] == "2026-10-08" and data["days"][0]["count"] == 1
    assert [s["slot_at"] for s in data["slots"]] == [PUB_THU]     # cible de depot : creneau du jour conserve
    assert data["slots"][0]["free"] is False


def test_get_publish_month_range_has_every_day_and_no_slot_grid(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_THU)])

    data = _get_publish(tmp_path, range="month", week="2026-10-15").json()

    assert data["range"] == "month"
    assert data["range_start"] == "2026-10-01" and data["range_end"] == "2026-10-31"
    assert len(data["days"]) == 31
    assert data["slots"] == []                                    # pas de cible de depot en vue Mois
    thu_box = next(d for d in data["days"] if d["date"] == "2026-10-08")
    assert thu_box["count"] == 1


def test_get_publish_day_groups_every_publication_without_losing_same_minute_duplicates(tmp_path, isolated_cwd):
    extra = [
        _entry("07", "scheduled", slot_at="2026-10-08T20:00:00+02:00"),
        _entry("08", "scheduled", slot_at="2026-10-08T20:00:00+02:00"),      # meme minute que 07
    ] + [
        _entry(str(10 + i), "published" if i % 2 == 0 else "scheduled",
               slot_at=f"2026-10-08T{6 + i:02d}:00:00+02:00",
               published_at=f"2026-10-08T{6 + i:02d}:05:00+02:00" if i % 2 == 0 else None)
        for i in range(10)
    ]
    _publish_setup(tmp_path, extra)

    data = _get_publish(tmp_path, range="day", week="2026-10-08").json()

    day = data["days"][0]
    assert day["date"] == "2026-10-08" and day["count"] == 12
    ids = [p["clip_id"] for p in day["posts"]]
    assert len(ids) == 12 and len(set(ids)) == 12                # aucune ecrasee, meme a la meme minute
    assert ids.count("07") == 1 and ids.count("08") == 1
    times = [p["slot_at_paris"] or p["published_at_paris"] for p in day["posts"]]
    assert times == sorted(times)                                 # triees par heure de Paris


def test_publish_move_calls_publish_move_with_the_slot(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _publish_setup(tmp_path)
    calls = []
    monkeypatch.setattr(publish, "move", lambda *a, **kw: calls.append((a, kw)) or _entry("01", "scheduled", slot_at=PUB_THU))

    resp = client(tmp_path).post(f"/api/publish/{CLIPS_VIDEO}/01/move", json={"slot_at": PUB_THU})

    assert resp.status_code == 200 and resp.json()["slot_at"] == PUB_THU
    (video_id, clip_id, channel, slot), kw = calls[0]
    assert (video_id, clip_id, channel) == (CLIPS_VIDEO, "01", "ma_chaine")
    assert slot.isoformat() == PUB_THU
    assert kw["state_dir"] == Path("state/publish") and kw["presets_dir"] == "presets"


def test_publish_move_conflict_is_409_with_detail(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _publish_setup(tmp_path)

    def boom(*a, **kw):
        raise publish.PublishError("creneau deja pris pour x/02 : y")

    monkeypatch.setattr(publish, "move", boom)

    resp = client(tmp_path).post(f"/api/publish/{CLIPS_VIDEO}/02/move", json={"slot_at": PUB_THU})

    assert resp.status_code == 409 and resp.json()["detail"] == "creneau deja pris pour x/02 : y"


def test_publish_move_validates_input(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _publish_setup(tmp_path)
    monkeypatch.setattr(publish, "move", lambda *a, **kw: pytest.fail("publish.move ne doit pas être appelé"))
    c = client(tmp_path)

    naive = c.post(f"/api/publish/{CLIPS_VIDEO}/01/move", json={"slot_at": "2026-10-08T12:00:00"})
    junk = c.post(f"/api/publish/{CLIPS_VIDEO}/01/move", json={"slot_at": "jeudi midi"})
    unsafe = c.post(f"/api/publish/{CLIPS_VIDEO}/..%2Fx/move", json={"slot_at": PUB_THU})

    assert naive.status_code == 422 and "fuseau" in naive.json()["detail"]
    assert junk.status_code == 422 and "slot_at" in junk.json()["detail"]
    assert unsafe.status_code in (400, 404)


def test_publish_move_on_a_video_with_no_entry_anywhere_is_refused(tmp_path, isolated_cwd):
    # "othervideo01" n'a ni style ni entree dans _sans_chaine, donc aucun compte a retrouver : plus refusee
    # pour « pas de style » (revue r-comptes 4), mais pour son vrai motif (aucun compte, donc aucun creneau).
    _publish_setup(tmp_path)

    resp = client(tmp_path).post("/api/publish/othervideo01/01/move", json={"slot_at": PUB_THU})

    assert resp.status_code == 409 and "aucun créneau" in resp.json()["detail"]


def test_publish_move_end_to_end_on_a_temporary_state(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_MON), _entry("02", "approved")])
    c = client(tmp_path)

    ok = c.post(f"/api/publish/{CLIPS_VIDEO}/02/move", json={"slot_at": PUB_THU})
    taken = c.post(f"/api/publish/{CLIPS_VIDEO}/02/move", json={"slot_at": PUB_MON})

    assert ok.status_code == 200 and ok.json()["status"] == "scheduled"
    assert taken.status_code == 409 and "pris" in taken.json()["detail"]
    saved = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert {e["clip_id"]: e["slot_at"] for e in saved} == {"01": PUB_MON, "02": PUB_THU}


def test_publish_published_and_unschedule_call_publish(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish

    _publish_setup(tmp_path)
    calls = []
    monkeypatch.setattr(publish, "mark_published", lambda *a, **kw: calls.append(("mark_published", a, kw)) or _entry("01", "published"))
    monkeypatch.setattr(publish, "unschedule", lambda *a, **kw: calls.append(("unschedule", a, kw)) or _entry("01", "approved"))
    c = client(tmp_path)

    done = c.post(f"/api/publish/{CLIPS_VIDEO}/01/published")
    back = c.post(f"/api/publish/{CLIPS_VIDEO}/01/unschedule")

    assert done.status_code == 200 and done.json()["status"] == "published"
    assert back.status_code == 200 and back.json()["status"] == "approved"
    assert [(n, a) for n, a, _ in calls] == [
        ("mark_published", (CLIPS_VIDEO, "01", "ma_chaine")), ("unschedule", (CLIPS_VIDEO, "01", "ma_chaine"))]
    assert all(kw["state_dir"] == Path("state/publish") for _, _, kw in calls)


@pytest.mark.parametrize("action", ["published", "unschedule"])
def test_publish_published_unschedule_error_is_409_with_detail(tmp_path, isolated_cwd, monkeypatch, action):
    from clipper import publish

    _publish_setup(tmp_path)

    def boom(*a, **kw):
        raise publish.PublishError("clip absent de la file de publication : x/06")

    monkeypatch.setattr(publish, "mark_published" if action == "published" else "unschedule", boom)

    resp = client(tmp_path).post(f"/api/publish/{CLIPS_VIDEO}/06/{action}")

    assert resp.status_code == 409 and resp.json()["detail"] == "clip absent de la file de publication : x/06"


def test_publish_published_and_unschedule_end_to_end(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_MON), _entry("02", "scheduled", slot_at=PUB_THU)])
    c = client(tmp_path)

    assert c.post(f"/api/publish/{CLIPS_VIDEO}/01/published").json()["status"] == "published"
    back = c.post(f"/api/publish/{CLIPS_VIDEO}/02/unschedule").json()
    again = c.post(f"/api/publish/{CLIPS_VIDEO}/01/published")             # déjà publié : refusé

    assert back["status"] == "approved" and back["slot_at"] is None
    assert again.status_code == 409


SECOND = "ef34gh"                              # 2e compte TikTok, lié à aucun style
PUB_NOCHAN_VIDEO = "nochannel001"


def _two_accounts_setup(tmp_path):
    """Compte ab12cd avec des créneaux ; compte ef34gh sans créneau ; une vidéo sans style publiée sur ef34gh."""
    _publish_setup(tmp_path, [
        _entry("01", "published", slot_at=PUB_MON, published_at="2026-10-05T18:40:00+02:00", account="ab12cd"),
        _entry("02", "failed", slot_at=PUB_THU, error="quota depasse", account="ab12cd"),
    ])
    (tmp_path / "state" / "accounts.json").write_text(json.dumps({"accounts": [
        {"id": "ab12cd", "label": "Compte exemple", "platform": "TikTok",
         "slots": [{"day": "mon", "time": "18:30"}, {"day": "thu", "time": "12:00"}], "timezone": "Europe/Paris"},
        {"id": SECOND, "label": "second_compte", "platform": "TikTok"}]}), encoding="utf-8")
    _write_state(tmp_path, PUB_NOCHAN_VIDEO)                                  # aucune chaîne
    _write_clip(tmp_path, PUB_NOCHAN_VIDEO, {**_clip_sidecar("01"), "video_id": PUB_NOCHAN_VIDEO})
    _write_clip(tmp_path, PUB_NOCHAN_VIDEO, {**_clip_sidecar("02"), "video_id": PUB_NOCHAN_VIDEO})
    _write_publish(tmp_path, "_sans_chaine", [
        _entry("01", "published", video_id=PUB_NOCHAN_VIDEO, slot_at=PUB_MON,
               published_at="2026-10-06T09:00:00+02:00", account=SECOND),
        _entry("02", "scheduled", video_id=PUB_NOCHAN_VIDEO, slot_at=PUB_THU, account=SECOND),
    ])


def _ids(clips):
    return sorted((c["video_id"], c["clip_id"]) for c in clips)


def test_a_post_published_on_an_account_without_style_shows_in_that_account_and_in_all(tmp_path, isolated_cwd):
    _two_accounts_setup(tmp_path)

    second = _get_publish(tmp_path, account=SECOND).json()
    everyone = client(tmp_path).get("/api/publish", params={"week": PUB_WEEK}).json()   # pas de compte : tous

    assert _ids(second["done"]) == [(PUB_NOCHAN_VIDEO, "01")]
    assert _ids(second["off_slot"]) == [(PUB_NOCHAN_VIDEO, "02")]             # programmé, sans créneau de style
    assert second["account"] == SECOND and second["slots"] == []
    assert "créneau" in second["reason"] and "Comptes" in second["reason"]   # le compte n'a pas de créneau : l'écran Comptes en règle
    assert _ids(everyone["done"]) == [(CLIPS_VIDEO, "01"), (CLIPS_VIDEO, "02"), (PUB_NOCHAN_VIDEO, "01")]
    assert everyone["account"] is None
    assert {c["account"] for c in everyone["done"]} == {"ab12cd", SECOND}


def test_the_publish_view_of_an_account_excludes_the_posts_of_another_account(tmp_path, isolated_cwd):
    _two_accounts_setup(tmp_path)

    a = _get_publish(tmp_path, account="ab12cd").json()
    b = _get_publish(tmp_path, account=SECOND).json()

    assert _ids(a["done"]) == [(CLIPS_VIDEO, "01"), (CLIPS_VIDEO, "02")]
    assert all(c["video_id"] == CLIPS_VIDEO for c in a["done"])
    assert all(c["video_id"] != CLIPS_VIDEO for c in [*b["done"], *b["off_slot"], *b["unscheduled"]])
    assert [s["clip"]["clip_id"] for s in a["slots"] if s["clip"]] == ["01", "02"]
    assert _ids(b["done"]) == [(PUB_NOCHAN_VIDEO, "01")]


@pytest.mark.parametrize("frozen_at", [
    pytest.param(None, id="horloge-reelle"),
    pytest.param(datetime(2026, 8, 2, 21, 58, tzinfo=timezone.utc), id="23h58-paris-ete"),   # dimanche 23:58 Paris
    pytest.param(datetime(2026, 8, 2, 22, 2, tzinfo=timezone.utc), id="00h02-paris-ete"),    # lundi 00:02 Paris
])
def test_a_post_created_through_the_form_on_a_second_account_appears_in_its_publish_view(
        tmp_path, isolated_cwd, monkeypatch, frozen_at):
    """Figer l'horloge du serveur (TASK-b778469259de) : la publication immediate est horodatee par
    ``clipper.publish.create_post`` (datetime.now() interne, jamais reçu de ``now`` depuis la route),
    et ``monday`` etait recalcule juste apres par une deuxieme lecture reelle ; les deux pouvaient
    tomber de part et d'autre de minuit (Europe/Paris, le fuseau par defaut de la semaine publiee),
    faisant disparaitre l'entree de la semaine interrogee. ``monday`` doit aussi etre calcule dans
    ce meme fuseau (et non en UTC) : c'est celui que ``_publish_range_view`` utilise pour grouper."""
    from clipper import publish as publish_mod
    from zoneinfo import ZoneInfo

    _publications_setup(tmp_path)
    _write_state(tmp_path, PUB_NOCHAN_VIDEO)
    _write_clip(tmp_path, PUB_NOCHAN_VIDEO, {**_clip_sidecar("01"), "video_id": PUB_NOCHAN_VIDEO})
    c = _pub_client(tmp_path)
    now = frozen_at or _dt.now(_tz.utc)

    class _Frozen(_dt):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now

    monkeypatch.setattr(web_app, "datetime", _Frozen)
    monkeypatch.setattr(publish_mod, "datetime", _Frozen)
    created = c.post("/api/publications", json={
        "video_id": PUB_NOCHAN_VIDEO, "clip_id": "01", "account": SPARE, "mode": "immediate"})

    assert created.status_code == 201, created.text
    local = now.astimezone(ZoneInfo("Europe/Paris"))
    monday = (local - _td(days=local.weekday())).date().isoformat()
    for account in (SPARE, ""):
        view = c.get("/api/publish", params={"account": account, "week": monday}).json()
        rows = [*view["done"], *view["off_slot"], *view["unscheduled"]]
        assert (PUB_NOCHAN_VIDEO, "01") in _ids(rows), account
    other = c.get("/api/publish", params={"account": READY, "week": monday}).json()
    assert (PUB_NOCHAN_VIDEO, "01") not in _ids([*other["done"], *other["off_slot"], *other["unscheduled"]])


def test_publish_actions_work_on_a_video_without_a_style(tmp_path, isolated_cwd):
    """Revue r-comptes 4 : move, unschedule, published, mode et compte marchent sur une publication d'une
    video sans style (entree localisee dans _sans_chaine.json, son vrai fichier) : aucune de ces actions
    n'exige de style, seule la creation en exige un (ou un compte, SPEC-1ed3 R3)."""
    _publications_setup(tmp_path)
    _write_state(tmp_path, PUB_NOCHAN_VIDEO)
    for clip_id in ("01", "02", "03", "04", "05"):
        _write_clip(tmp_path, PUB_NOCHAN_VIDEO, _clip_sidecar(clip_id, video_id=PUB_NOCHAN_VIDEO))
    _write_publish(tmp_path, "_sans_chaine", [
        _entry("01", "scheduled", video_id=PUB_NOCHAN_VIDEO, slot_at=PUB_MON, account=READY),
        _entry("02", "scheduled", video_id=PUB_NOCHAN_VIDEO, slot_at=PUB_THU, account=READY),
        _entry("03", "scheduled", video_id=PUB_NOCHAN_VIDEO, slot_at=PUB_NEXT_MON, account=READY),
        _entry("04", "scheduled", video_id=PUB_NOCHAN_VIDEO, slot_at="2026-10-15T12:00:00+02:00", account=READY),
        _entry("05", "scheduled", video_id=PUB_NOCHAN_VIDEO, slot_at="2026-10-19T18:30:00+02:00", account=READY),
    ])
    c = client(tmp_path)

    moved = c.post(f"/api/publish/{PUB_NOCHAN_VIDEO}/01/move", json={"slot_at": "2026-10-01T12:00:00+02:00"})
    unscheduled = c.post(f"/api/publish/{PUB_NOCHAN_VIDEO}/02/unschedule")
    published = c.post(f"/api/publish/{PUB_NOCHAN_VIDEO}/03/published")
    mode = c.post(f"/api/publish/{PUB_NOCHAN_VIDEO}/04/mode", json={"mode": "immediate"})
    account = c.post(f"/api/publish/{PUB_NOCHAN_VIDEO}/05/account", json={"account": SPARE})

    assert moved.status_code == 200, moved.text
    assert unscheduled.status_code == 200 and unscheduled.json()["status"] == "approved", unscheduled.text
    assert published.status_code == 200 and published.json()["status"] == "published", published.text
    assert mode.status_code == 200 and mode.json()["publish_mode"] == "immediate", mode.text
    assert account.status_code == 200 and account.json()["account"] == SPARE, account.text


def test_get_publish_corrupt_file_of_a_styleless_video_is_a_500(tmp_path, isolated_cwd):
    _two_accounts_setup(tmp_path)
    (tmp_path / "state" / "publish" / "_sans_chaine.json").write_text("{pas du json", encoding="utf-8")

    resp = client(tmp_path).get("/api/publish", params={"week": PUB_WEEK})

    assert resp.status_code == 500 and "publication" in resp.json()["detail"]


def test_publish_screen_is_wired_with_calendar_queue_and_actions():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert "/static/screens/publish.js" in page
    assert page.index("/static/screens.js") < page.index("/static/screens/publish.js") < page.index("/static/app.js")
    assert "Screens.publish" in js
    assert "/api/publish" in js and "/move" in js and "/published" in js and "/unschedule" in js
    assert "pub-account" in js and "<select" in js                         # sélecteur de compte TikTok
    assert "Tous les comptes" in js and "account=" in js                    # « tous » + un compte par filtre
    assert "publish?channel=" not in js                                    # plus aucun filtre par style
    assert "week" in js and "Semaine précédente" in js and "Semaine suivante" in js
    assert "cal-c" in js and "data-slot-at" in js and "slot_at" in js      # calendrier hebdomadaire des créneaux
    assert "unscheduled" in js and "En attente" in js                       # liste unique (validés sans date + en cours/échecs)
    for ev in ("dragstart", "dragover", "drop"):                            # glisser-déposer à la souris
        assert ev in js, ev
    for ev in ("touchstart", "touchmove", "touchend"):                      # et au toucher
        assert ev in js, ev
    for label in ("Planifié", "Publié", "Échec", "Approuvé"):               # badges de statut
        assert label in js, label
    assert "download" in js and "video_url" in js                          # télécharger
    assert "copyText" in js                                                # copier la description
    assert "Déclarer publié" in js and "confirmDialog" in js                # confirmation
    assert "Repasser en attente" in js
    assert "undo" in js                                                    # toast « Annuler »
    assert "toastError" in js                                              # un conflit 409 s'affiche
    assert "Aucune publication en attente" in js                           # état vide
    for sel in (".cal", ".cal-c", ".post", "TASK-503d"):
        assert sel in css, sel


def test_publish_calendar_css_scales_day_boxes_and_never_overflows_the_page():
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    for sel in (".cal-day", ".cal-day.compact", ".cal-day.mini", ".cal-month", ".pub-day-list", ".cal-mini-row", ".cal-month-count"):
        assert sel in css, sel
    assert ".cal-day.dense" not in css                                  # palier retire (TASK-460904227d2f)
    assert ".cal-more" not in css                                       # plus de « +N autres » (TASK-39bbe20b3284)
    body = css[css.index(".cal-day {"):css.index(".cal-day {") + css[css.index(".cal-day {"):].index("}")]
    assert "overflow-y" not in body and "max-height" not in body        # la case s'agrandit, ni defilement ni coupure
    assert "max-height" not in css[css.index(".cal-month .cal-day.small"):].split("\n", 1)[0]
    assert "overflow-y" not in css[css.index("@media (max-width: 900px) {\n  .cal-day"):]


# --------------------------------------------------------------------------
# Ecran Reglages (SPEC-c100 E8, ADR-4f6e §2 et §5) : config.toml en formulaire
# --------------------------------------------------------------------------

import tomllib  # noqa: E402

_SET_TOML = (
    '# commentaire a perdre\nmode = "review"\n'
    '[llm]\nbackend = "claude-cli"\n'
    '[web]\nport = 8123\ntoken = "secret-tres-long"\n'
    '[render]\ncrf = 18\n'
)


def _settings_setup(tmp_path, text=_SET_TOML):
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")
    return tmp_path / "config.toml"


def sclient(tmp_path) -> TestClient:
    """Serveur démarré avec la config lue dans config.toml (comme 'serve')."""
    from clipper.config import load_config

    path = tmp_path / "config.toml"
    return TestClient(create_app(config=load_config(path) if path.exists() else make_config(tmp_path)))


def test_get_settings_returns_effective_values_and_documented_defaults_per_section(tmp_path, isolated_cwd):
    _settings_setup(tmp_path)
    data = sclient(tmp_path).get("/api/settings").json()

    assert data["raw"]["mode"] == "review" and data["raw"]["web"]["port"] == 8123
    assert data["effective"]["mode"] == "review"
    assert data["effective"]["workspace_dir"] == "workspace"          # défaut de config.DEFAULTS
    assert data["effective"]["web"]["port"] == 8123
    assert data["effective"]["web"]["sse_poll_interval_s"] == 1.0     # défaut du module
    assert data["effective"]["worker"]["poll_interval_s"] == 2
    assert data["effective"]["llm"]["backend"] == "claude-cli"
    assert data["effective"]["llm"]["usages"]["moments"] == {"model": "strong"}
    for section in ("llm", "web", "worker"):
        assert section in data["defaults"], section
    # même mécanisme que l'écran Chaînes : défaut + commentaire du source
    assert data["defaults"]["web"]["port"]["default"] == 8000
    assert "Hôte et port" in data["defaults"]["web"]["host"]["comment"]
    assert "réponse refusée" in data["defaults"]["llm"]["repair_attempts"]["comment"]
    assert set(data["backends"]) == {"claude-cli", "claude-api", "ollama"}
    assert data["modes"] == ["review", "auto"]
    assert data["comments_lost"] is True                              # le fichier contient un commentaire


def test_get_settings_never_returns_the_token(tmp_path, isolated_cwd):
    _settings_setup(tmp_path)
    resp = sclient(tmp_path).get("/api/settings")
    assert "secret-tres-long" not in resp.text
    access = resp.json()["access"]
    assert access["host"] == "127.0.0.1" and access["port"] == 8123
    assert access["token_set"] is True and access["token"] != "secret-tres-long" and set(access["token"]) == {"•"}
    assert access["command"] == "python -m clipper serve --port 8123"


def test_get_settings_without_config_toml_shows_defaults_and_no_comment_warning(tmp_path, isolated_cwd):
    data = sclient(tmp_path).get("/api/settings").json()
    assert data["exists"] is False and data["comments_lost"] is False
    assert data["effective"]["mode"] == "review" and data["effective"]["web"]["port"] == 8000
    assert data["access"]["token_set"] is False and data["access"]["token"] is None


def test_put_settings_writes_through_write_config_keeping_other_sections_and_the_token(
        tmp_path, isolated_cwd, monkeypatch):
    path = _settings_setup(tmp_path)
    from clipper import config as config_mod
    from clipper.web import app as web_app

    calls = []
    real = config_mod.write_config
    monkeypatch.setattr(web_app, "write_config", lambda *a, **k: (calls.append((a, k)), real(*a, **k))[1])

    body = {"settings": {
        "mode": "auto", "output_dir": "sorties",
        "llm": {"backend": "ollama", "repair_attempts": 2, "usages": {"moments": {"model": "fast"}}},
        "web": {"port": 9001, "host": "127.0.0.1", "sse_poll_interval_s": 2.0},
        "worker": {"poll_interval_s": 5},
    }}
    resp = sclient(tmp_path).put("/api/settings", json=body)

    assert resp.status_code == 200, resp.text
    assert len(calls) == 1 and Path(calls[0][0][0]) == Path("config.toml")
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written["mode"] == "auto" and written["output_dir"] == "sorties"
    assert written["llm"]["backend"] == "ollama" and written["llm"]["usages"] == {"moments": {"model": "fast"}}
    assert written["web"]["port"] == 9001
    assert written["web"]["token"] == "secret-tres-long"             # le jeton n'est jamais touché
    assert written["render"] == {"crf": 18}                          # section hors formulaire conservée
    assert "commentaire" not in path.read_text(encoding="utf-8")     # commentaires perdus (ADR-4f6e §2)
    assert resp.json()["effective"]["mode"] == "auto" and resp.json()["comments_lost"] is False


@pytest.mark.parametrize("settings, fragment", [
    ({"mode": "manuel"}, "mode"),
    ({"workspace_dir": 5}, "workspace_dir"),
    ({"llm": {"backend": "inconnu"}}, "inconnu"),
    ({"llm": {"usages": {"moments": {"backend": "nope"}}}}, "nope"),
    ({"llm": {"repair_attempts": "deux"}}, "repair_attempts"),
    ({"web": {"port": "x"}}, "port"),
    ({"web": {"port": 70000}}, "port"),
    ({"web": {"zzz": 1}}, "zzz"),
    ({"worker": {"queue_path": 3}}, "queue_path"),
    ({"web": {"host": "0.0.0.0", "token": "autre"}}, "jeton"),
    ({"web": {"host": "0.0.0.0"}}, "jeton"),    # hors bouclage sans jeton : serve refuserait de démarrer
    ({"render": {"crf": 1}}, "render"),         # section hors formulaire : pas éditable ici
])
def test_put_settings_refused_value_is_422_with_detail_and_keeps_the_file(
        tmp_path, isolated_cwd, settings, fragment):
    path = _settings_setup(tmp_path, _SET_TOML.replace('token = "secret-tres-long"\n', ""))
    before = path.read_bytes()

    resp = sclient(tmp_path).put("/api/settings", json={"settings": settings})

    assert resp.status_code == 422
    assert fragment in resp.json()["detail"]
    assert path.read_bytes() == before                               # fichier intact
    assert not list(tmp_path.glob("config.toml.*"))                  # pas de .tmp qui traîne


def test_put_settings_load_config_refusal_is_422_and_keeps_the_file(tmp_path, isolated_cwd):
    path = _settings_setup(tmp_path)
    before = path.read_bytes()
    resp = sclient(tmp_path).put("/api/settings", json={"settings": {"mode": "auto", "llm": {"zzz": 1}}})
    assert resp.status_code == 422 and path.read_bytes() == before


def test_put_settings_mode_and_backend_are_read_by_the_next_queue_entries(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    _settings_setup(tmp_path)
    seen = []

    def fake_enqueue(url, channel, action, force_steps, *, config=None):
        seen.append((config.mode, config.section("llm")["backend"]))
        return {"id": "e1", "video_id": VIDEO_ID, "url": url, "channel": channel, "action": action,
                "force_steps": [], "status": "waiting"}

    monkeypatch.setattr(worker, "enqueue", fake_enqueue)
    c = sclient(tmp_path)
    c.post("/api/queue", json={"url": URL, "action": "run"})
    assert c.put("/api/settings", json={"settings": {"mode": "auto", "llm": {"backend": "ollama"}}}).status_code == 200
    c.post("/api/queue", json={"url": URL, "action": "run"})

    assert seen == [("review", "claude-cli"), ("auto", "ollama")]


def test_put_settings_web_changes_ask_for_a_restart_and_do_not_change_the_running_access(tmp_path, isolated_cwd):
    _settings_setup(tmp_path)
    c = sclient(tmp_path)
    assert c.get("/api/settings").json()["restart_required"] is False
    data = c.put("/api/settings", json={"settings": {"web": {"port": 9001}}}).json()
    assert data["restart_required"] is True
    assert data["access"]["port"] == 8123                            # l'écoute en cours ne change pas


def test_settings_screen_is_wired_with_sections_access_and_comment_warning():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "screens" / "settings.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert "/static/screens/settings.js" in page
    assert page.index("/static/screens.js") < page.index("/static/screens/settings.js") < page.index("/static/app.js")
    assert "Screens.settings" in js and 'api("/api/settings"' in js
    assert 'jsonBody("PUT"' in js and "toast(" in js
    # formulaire par sections
    for title in ("Général", "Dossiers", "LLM", "Serveur web", "Worker", "Accès"):
        assert title in js, title
    for key in ("mode", "workspace_dir", "output_dir", "backend", "repair_attempts", "usages", "models"):
        assert key in js, key
    # section Accès en lecture seule : hôte, port, jeton masqué, commande serve
    assert "token_set" in js and "access.command" in js and "access.host" in js and "access.port" in js
    assert "redémarrage" in js
    # avertissement avant la première écriture
    assert "commentaires du fichier perdus" in js and "comments_lost" in js
    assert "field-error" in js and "set-" in css
# Éditeur d'agencement stream split (SPEC-c100 E5, SPEC-76dc) : API + écran
# --------------------------------------------------------------------------

_LAYOUT_KEYS = ("split_webcam_dest", "split_gameplay_dest", "badge_dest", "split_subtitle_dest")
_LAYOUT_DEFAULTS = {
    "split_webcam_dest": {"x": 20, "y": 0, "w": 1040, "h": 640},
    "split_gameplay_dest": {"x": 0, "y": 640, "w": 1080, "h": 1280},
    "badge_dest": {"x": 330, "y": 590, "w": 420, "h": 100},
    "split_subtitle_dest": {"x": 150, "y": 710, "w": 780, "h": 150},
}
import tomllib  # noqa: E402

_JPEG = b"\xff\xd8\xff\xe0layout-test-jpeg"


def _layout_keyframe(tmp_path, video_id, channel=CH, names=("scene0000_000.jpg",), folder="frames"):
    _write_state(tmp_path, video_id, channel=channel)
    frames = tmp_path / "workspace" / video_id / folder
    frames.mkdir(parents=True, exist_ok=True)
    for name in names:
        (frames / name).write_bytes(_JPEG + name.encode())


def test_layout_keyframe_returns_a_jpeg_of_the_requested_video(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    _layout_keyframe(tmp_path, "aaaaaaaaaaa")
    resp = client(tmp_path).get(f"/api/channels/{CH}/keyframe", params={"video_id": "aaaaaaaaaaa"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content.startswith(_JPEG)


def test_layout_keyframe_without_video_id_takes_a_video_of_the_channel(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    _layout_keyframe(tmp_path, "bbbbbbbbbbb", channel="autre_chaine")
    _layout_keyframe(tmp_path, "aaaaaaaaaaa", folder="scenes", names=("k1.jpg",))
    resp = client(tmp_path).get(f"/api/channels/{CH}/keyframe")
    assert resp.status_code == 200 and resp.content.endswith(b"k1.jpg")


def test_layout_keyframe_404_in_french_when_no_video_has_keyframes(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    _write_state(tmp_path, "aaaaaaaaaaa", channel=CH)          # vidéo sans images clés
    _layout_keyframe(tmp_path, "bbbbbbbbbbb", channel="autre_chaine")
    c = client(tmp_path)
    resp = c.get(f"/api/channels/{CH}/keyframe")
    assert resp.status_code == 404
    assert "image clé" in resp.json()["detail"] and CH in resp.json()["detail"]
    resp = c.get(f"/api/channels/{CH}/keyframe", params={"video_id": "aaaaaaaaaaa"})
    assert resp.status_code == 404 and "aaaaaaaaaaa" in resp.json()["detail"]
    # une vidéo d'une autre chaîne n'est pas servie pour celle-ci
    resp = c.get(f"/api/channels/{CH}/keyframe", params={"video_id": "bbbbbbbbbbb"})
    assert resp.status_code == 404 and "bbbbbbbbbbb" in resp.json()["detail"]


def test_layout_keyframe_refuses_unsafe_video_id_and_unknown_channel(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    assert c.get(f"/api/channels/{CH}/keyframe", params={"video_id": "../x"}).status_code == 404
    assert c.get("/api/channels/inconnue/keyframe").status_code == 404


def test_get_layout_returns_spec_defaults(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    data = client(tmp_path).get(f"/api/channels/{CH}/layout").json()
    for key in _LAYOUT_KEYS:
        assert data[key] == _LAYOUT_DEFAULTS[key], key
    assert data["defaults"] == _LAYOUT_DEFAULTS
    assert data["canvas"] == {"w": 1080, "h": 1920}
    assert data["safe"] == {"left": 150, "top": 160, "right": 930, "bottom": 1520}


def test_get_layout_returns_the_preset_values(tmp_path, isolated_cwd):
    _channels_setup(tmp_path, _CH_PRESET + 'split_webcam_dest = {x = 0, y = 0, w = 1080, h = 700}\n')
    data = client(tmp_path).get(f"/api/channels/{CH}/layout").json()
    assert data["split_webcam_dest"] == {"x": 0, "y": 0, "w": 1080, "h": 700}
    assert data["badge_dest"] == _LAYOUT_DEFAULTS["badge_dest"]


def test_get_layout_unknown_channel_is_404(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    assert client(tmp_path).get("/api/channels/inconnue/layout").status_code == 404


def test_put_layout_writes_the_keys_in_reframe_through_save_channel(tmp_path, isolated_cwd, monkeypatch):
    _channels_setup(tmp_path)
    from clipper import channel as channel_mod

    calls = []
    real = channel_mod.save_channel

    def spy(name, data, **kwargs):
        calls.append((name, data, kwargs))
        return real(name, data, **kwargs)

    monkeypatch.setattr(channel_mod, "save_channel", spy)
    layout = {
        "split_webcam_dest": {"x": 0, "y": 0, "w": 1080, "h": 700},
        "split_gameplay_dest": {"x": 0, "y": 700, "w": 1080, "h": 1220},
        "badge_dest": {"x": 330, "y": 640, "w": 420, "h": 100},
        "split_subtitle_dest": {"x": 150, "y": 760, "w": 780, "h": 150},
    }
    resp = client(tmp_path).put(f"/api/channels/{CH}/layout", json=layout)

    assert resp.status_code == 200, resp.text
    for key in _LAYOUT_KEYS:
        assert resp.json()[key] == layout[key]
    assert any(n == CH and k["presets_dir"] == "presets" and d["reframe"]["badge_dest"] == layout["badge_dest"]
               for n, d, k in calls)
    saved = tomllib.loads((tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8"))
    assert saved["reframe"]["letterbox_zoom"] == 1.5                    # le reste du preset est conservé
    assert saved["channel"]["display_name"] == "Ma chaîne"
    for key in _LAYOUT_KEYS:
        assert saved["reframe"][key] == layout[key]
    assert client(tmp_path).get(f"/api/channels/{CH}/layout").json()["badge_dest"] == layout["badge_dest"]


@pytest.mark.parametrize("key, rect, fragment", [
    ("split_webcam_dest", {"x": 100, "y": 0, "w": 1040, "h": 640}, "deborde"),               # sort du canevas (1140 > 1080)
    ("split_gameplay_dest", {"x": 0, "y": 600, "w": 1080, "h": 1320}, "se chevauchent"),      # chevauche la webcam
    ("badge_dest", {"x": 100, "y": 590, "w": 420, "h": 100}, "zone sure"),                    # hors zone sûre
])
def test_put_layout_invalid_is_422_with_the_load_config_detail_and_keeps_the_file(
    tmp_path, isolated_cwd, key, rect, fragment
):
    _channels_setup(tmp_path)
    path = tmp_path / "presets" / f"{CH}.toml"
    before = path.read_text(encoding="utf-8")

    resp = client(tmp_path).put(f"/api/channels/{CH}/layout", json={key: rect})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert key in detail and fragment in detail, detail
    # même texte que celui de reframe (qui refuse, load_config ne contrôlant que les clés)
    from clipper import reframe
    from clipper.config import load_config

    candidate = tmp_path / "candidate.toml"
    candidate.write_text(
        '[reframe]\nstream_variant = "split"\n'
        + f"{key} = {{x = {rect['x']}, y = {rect['y']}, w = {rect['w']}, h = {rect['h']}}}\n",
        encoding="utf-8",
    )
    with pytest.raises(reframe.ReframeError) as err:
        reframe._settings(load_config(candidate))
    assert detail == str(err.value)
    assert path.read_text(encoding="utf-8") == before


def test_put_layout_without_any_key_or_with_bad_rect_is_422(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    resp = c.put(f"/api/channels/{CH}/layout", json={})
    assert resp.status_code == 422 and "split_webcam_dest" in resp.json()["detail"]
    resp = c.put(f"/api/channels/{CH}/layout", json={"badge_dest": {"x": 1, "y": 2}})
    assert resp.status_code == 422 and "badge_dest" in resp.json()["detail"]
    assert c.put("/api/channels/inconnue/layout", json={"badge_dest": _LAYOUT_DEFAULTS["badge_dest"]}).status_code == 404


# --- Badge de style retirable (TASK-818c7566591f) ---------------------------------

_BADGE_PRESET = _CH_PRESET + '\n[render]\nbadge_enabled = true\nbadge_logo = "logo.png"\nbadge_name = "ma_chaine"\ncrf = 20\n'


def test_get_layout_reports_badge_enabled_effective_value(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    assert client(tmp_path).get(f"/api/channels/{CH}/layout").json()["badge_enabled"] is False
    _channels_setup(tmp_path, _BADGE_PRESET)
    assert client(tmp_path).get(f"/api/channels/{CH}/layout").json()["badge_enabled"] is True


def test_put_layout_badge_enabled_false_writes_only_that_render_key(tmp_path, isolated_cwd):
    _channels_setup(tmp_path, _BADGE_PRESET)
    before = tomllib.loads((tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8"))

    resp = client(tmp_path).put(f"/api/channels/{CH}/layout", json={"badge_enabled": False})

    assert resp.status_code == 200, resp.text
    assert resp.json()["badge_enabled"] is False
    saved = tomllib.loads((tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8"))
    assert saved["render"]["badge_enabled"] is False
    expected = {**before, "render": {**before["render"], "badge_enabled": False}}
    assert saved == expected                                            # aucun autre réglage modifié


def test_put_layout_badge_enabled_back_to_true_keeps_badge_dest(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    badge = {"x": 330, "y": 640, "w": 420, "h": 100}
    resp = c.put(f"/api/channels/{CH}/layout", json={
        "split_webcam_dest": {"x": 0, "y": 0, "w": 1080, "h": 700},
        "split_gameplay_dest": {"x": 0, "y": 700, "w": 1080, "h": 1220},
        "badge_dest": badge, "split_subtitle_dest": {"x": 150, "y": 760, "w": 780, "h": 150},
        "badge_enabled": False})
    assert resp.status_code == 200, resp.text
    assert c.put(f"/api/channels/{CH}/layout", json={"badge_enabled": True}).status_code == 200
    saved = tomllib.loads((tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8"))
    assert saved["render"]["badge_enabled"] is True
    assert saved["reframe"]["badge_dest"] == badge                      # position enregistrée conservée
    assert c.get(f"/api/channels/{CH}/layout").json()["badge_dest"] == badge


def test_put_layout_badge_enabled_must_be_a_boolean(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    resp = client(tmp_path).put(f"/api/channels/{CH}/layout", json={"badge_enabled": "peut-être"})
    assert resp.status_code == 422


def test_layout_js_has_a_badge_toggle_that_hides_the_zone_and_saves_badge_enabled():
    js = (STATIC / "screens" / "layout.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "Afficher le badge" in js and "data-ly-badge" in js and "badge_enabled" in js
    assert ".ly-zone.off" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_layout_js_badge_zone_is_hidden_from_the_stage_when_disabled():
    out = _run_js([("screens/layout.js", ["LY_ZONES", "LY_SAMPLE", "lyZoneInner", "lyBadgeOn", "lyStageHtml"])], """(() => {
      const mk = (on) => { globalThis.lyUi = { name: 'ma_chaine', frame: null, selected: 'badge_dest', safe: false,
        data: { safe: { left: 150, top: 160, right: 930, bottom: 1520 } }, draft: { badge_enabled: on } };
        return lyStageHtml(); };
      const off = mk(false), on = mk(true);
      const cls = (h) => (h.match(/class="ly-zone ly-z-badge[^"]*"/) || [''])[0];
      return { off: cls(off), on: cls(on), offCount: (off.match(/ly-zone /g) || []).length };
    })()""")
    assert " off" in out["off"] and " off" not in out["on"]
    assert out["offCount"] == 4                                          # la zone reste dans le DOM : rien n'est perdu


# --- Éditeur d'agencement letterbox (TASK-3be3) -----------------------------

_LB_KEYS = ("letterbox_top", "letterbox_zoom", "letterbox_title_dest", "letterbox_subtitle_dest", "cta_handle_gap")
_LB_URL = f"/api/channels/{CH}/layout/letterbox"


def _preset_of(tmp_path):
    return tomllib.loads((tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8"))


def test_get_layout_names_the_mode_of_the_style(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    assert client(tmp_path).get(f"/api/channels/{CH}/layout").json()["mode"] == "letterbox"
    _channels_setup(tmp_path, _CH_PRESET + 'layout = "stream_auto"\nstream_variant = "split"\n')
    assert client(tmp_path).get(f"/api/channels/{CH}/layout").json()["mode"] == "split"
    _channels_setup(tmp_path, _CH_PRESET + 'format = "crop"\n')
    assert client(tmp_path).get(f"/api/channels/{CH}/layout").json()["mode"] == "crop"


def test_get_letterbox_layout_gives_values_standard_and_overrides(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    (tmp_path / "config.toml").write_text('mode = "review"\n[reframe]\nletterbox_top = 460\n', encoding="utf-8")

    data = client(tmp_path).get(_LB_URL).json()

    assert data["values"] == {"letterbox_top": 460, "letterbox_zoom": 1.5, "letterbox_title_dest": {},
                              "letterbox_subtitle_dest": {}, "cta_handle_gap": 8}
    # standard = config.toml puis défauts, sans le preset
    assert data["standard"] == {"letterbox_top": 460, "letterbox_zoom": 1.3, "letterbox_title_dest": {},
                                "letterbox_subtitle_dest": {}, "cta_handle_gap": 8}
    assert data["overridden"] == ["letterbox_zoom"]
    assert data["canvas"] == {"w": 1080, "h": 1920}
    assert data["safe"] == {"left": 150, "top": 160, "right": 930, "bottom": 1520}
    assert (data["text_gap"], data["part_height"], data["title_lift"]) == (16, 56, 40)
    assert data["cta"] == {"enabled": False, "handle": "", "font_size": 32}
    assert data["title_enabled"] is True


def test_put_letterbox_layout_writes_only_the_values_that_differ_from_the_standard(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    title = {"x": 200, "y": 200, "w": 680, "h": 200}

    resp = client(tmp_path).put(_LB_URL, json={"letterbox_top": 470, "letterbox_title_dest": title})

    assert resp.status_code == 200, resp.text
    saved = _preset_of(tmp_path)
    assert saved["reframe"] == {"letterbox_zoom": 1.5, "letterbox_top": 470, "letterbox_title_dest": title}
    assert "render" not in saved                                        # cta_handle_gap non envoyé : hérité
    assert saved["channel"]["display_name"] == "Ma chaîne"
    assert resp.json()["values"]["letterbox_top"] == 470
    assert sorted(resp.json()["overridden"]) == ["letterbox_title_dest", "letterbox_top", "letterbox_zoom"]


def test_put_letterbox_layout_value_back_to_standard_removes_the_override(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)

    resp = client(tmp_path).put(_LB_URL, json={"letterbox_zoom": 1.3, "cta_handle_gap": 14})

    assert resp.status_code == 200, resp.text
    saved = _preset_of(tmp_path)
    assert "letterbox_zoom" not in saved.get("reframe", {})            # égal au standard : plus surchargé
    assert saved["render"] == {"cta_handle_gap": 14}                    # [render] du preset
    assert resp.json()["overridden"] == ["cta_handle_gap"]


def test_delete_letterbox_layout_goes_back_to_the_standard(tmp_path, isolated_cwd):
    _channels_setup(tmp_path, _CH_PRESET + 'letterbox_top = 480\nstream_exclude_margin = 60\n'
                    '\n[render]\ncta_handle_gap = 20\ncrf = 20\n')

    resp = client(tmp_path).delete(_LB_URL)

    assert resp.status_code == 200, resp.text
    saved = _preset_of(tmp_path)
    assert saved["reframe"] == {"stream_exclude_margin": 60}            # le reste du preset est gardé
    assert saved["render"] == {"crf": 20}
    assert resp.json()["overridden"] == []
    assert resp.json()["values"]["letterbox_zoom"] == 1.3


@pytest.mark.parametrize("body, fragment", [
    ({"letterbox_title_dest": {"x": 150, "y": 160, "w": 1000, "h": 100}}, "letterbox_title_dest deborde du canevas"),
    ({"letterbox_subtitle_dest": {"x": 100, "y": 1250, "w": 700, "h": 100}}, "letterbox_subtitle_dest hors de la zone sure"),
    ({"letterbox_title_dest": {"x": 150, "y": 300, "w": 780, "h": 300}}, "chevauche le panneau video"),
    ({"letterbox_top": 1300}, "sort du cadre"),
    ({"cta_handle_gap": -2}, "cta_handle_gap"),
])
def test_put_letterbox_layout_invalid_is_422_with_the_module_message_and_keeps_the_file(
    tmp_path, isolated_cwd, body, fragment
):
    _channels_setup(tmp_path)
    path = tmp_path / "presets" / f"{CH}.toml"
    before = path.read_text(encoding="utf-8")

    resp = client(tmp_path).put(_LB_URL, json=body)

    assert resp.status_code == 422, resp.text
    assert fragment in resp.json()["detail"], resp.json()["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_put_letterbox_layout_refuses_unknown_keys_and_empty_body(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    assert c.put(_LB_URL, json={"split_webcam_dest": {"x": 0, "y": 0, "w": 10, "h": 10}}).status_code == 422
    resp = c.put(_LB_URL, json={})
    assert resp.status_code == 422 and "letterbox_top" in resp.json()["detail"]
    assert c.put("/api/channels/inconnue/layout/letterbox", json={"letterbox_top": 400}).status_code == 404


def test_letterbox_layout_editor_is_reachable_from_a_letterbox_style_and_wired():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    channels = (STATIC / "screens" / "channels.js").read_text(encoding="utf-8")
    js = (STATIC / "screens" / "layout-letterbox.js").read_text(encoding="utf-8")
    layout = (STATIC / "screens" / "layout.js").read_text(encoding="utf-8")

    assert page.index("/static/screens/layout.js") < page.index("/static/screens/layout-letterbox.js")
    # bouton visible pour un style letterbox comme pour un style stream split
    assert "isLetterbox" in channels and "isSplit || isLetterbox" in channels
    assert 'data.mode === "letterbox"' in layout and "lbOpen(" in layout
    for key in _LB_KEYS:
        assert key in js, key
    for label in ("Vidéo", "Titre d'écran", "Sous-titres", "Pseudo"):
        assert label in js, label
    assert "Revenir au standard" in js and '"DELETE"' in js and 'jsonBody("PUT"' in js
    assert "/layout/letterbox" in js and "lyScale(" in js and "pointerdown" in js and "setPointerCapture" in js
    assert "standard" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_letterbox_editor_geometry_matches_the_reframe_defaults():
    """Les rectangles affichés par l'éditeur (zones déduites) sont ceux que
    reframe calcule pour une source 1920x1080 (text_zones par défaut)."""
    js = (STATIC / "screens" / "layout-letterbox.js").read_text(encoding="utf-8")
    start = js.index("/* GEOMETRIE */")
    end = js.index("/* FIN GEOMETRIE */")
    script = js[start:end] + """
const view = {canvas: {w: 1080, h: 1920}, safe: {left: 150, top: 160, right: 930, bottom: 1520},
              text_gap: 16, part_height: 56};
const v = {letterbox_top: 440, letterbox_zoom: 1.3, letterbox_title_dest: {}, letterbox_subtitle_dest: {}};
const out = lbRects(view, v, {w: 1920, h: 1080});
const v2 = Object.assign({}, v, {letterbox_title_dest: {x: 200, y: 200, w: 600, h: 150}});
const back = lbFromVideoRect(view, {x: 0, y: 400, w: 1080, h: 910}, {w: 1920, h: 1080});
console.log(JSON.stringify([out, lbRects(view, v2, {w: 1920, h: 1080}).title, back]));
"""
    out, title, back = json.loads(_node_run(script))
    assert out["video"] == {"x": 0, "y": 440, "w": 1080, "h": 790}
    assert out["title"] == {"x": 150, "y": 160, "w": 780, "h": 264}
    assert out["subtitles"] == {"x": 150, "y": 1246, "w": 780, "h": 202}
    assert out["part"] == {"x": 150, "y": 1464, "w": 780, "h": 56}
    assert title == {"x": 200, "y": 200, "w": 600, "h": 150}
    assert back == {"letterbox_top": 400, "letterbox_zoom": 1.5}


def test_layout_editor_screen_is_wired_with_canvas_zones_handles_and_actions():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "screens" / "layout.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    channels = (STATIC / "screens" / "channels.js").read_text(encoding="utf-8")

    assert "/static/screens/layout.js" in page
    assert page.index("/static/screens/channels.js") < page.index("/static/screens/layout.js")
    assert "/layout" in channels and "Éditeur d'agencement" in channels      # accès depuis la chaîne
    # canevas 1080x1920 mis à l'échelle, quatre zones, image clé
    assert "1080" in js and "1920" in js and "scale(" in js
    for key in _LAYOUT_KEYS:
        assert key in js, key
    for label in ("Webcam", "Jeu", "Badge", "Sous-titres"):
        assert label in js, label
    assert "/keyframe" in js and "Aucune image clé" in js
    # poignées, souris et toucher
    assert "pointerdown" in js and "pointermove" in js and "setPointerCapture" in js and "hdl" in js
    assert "touch-action" in css
    # valeurs {x,y,w,h} éditables, réinitialiser, enregistrer (erreur du serveur affichée)
    assert 'data-k="x"' in js or 'data-k="${' in js
    assert "Réinitialiser aux défauts" in js and "Enregistrer" in js
    assert 'jsonBody("PUT"' in js and "/layout" in js and "Agencement enregistré" in js
    assert "Screens.channels" in js
    for selector in (".ly-stage", ".ly-zone", ".ly-hdl"):
        assert selector in css, selector
# Aperçu du style des sous-titres (TASK-dd3f, SPEC-c100 E5)
# --------------------------------------------------------------------------


def test_subtitles_preview_returns_a_png_rendered_by_the_pipeline(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline

    _channels_setup(tmp_path)
    seen = []
    monkeypatch.setattr(pipeline, "preview_subtitles",
                        lambda config, text: seen.append((config.section("subtitles"), text)) or b"\x89PNG\r\n\x1a\nxx")

    resp = client(tmp_path).get(f"/api/channels/{CH}/subtitles-preview", params={"text": "Salut à tous"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(b"\x89PNG")
    assert seen and seen[0][1] == "Salut à tous"


def test_subtitles_preview_renders_a_real_png_with_the_preset_style(tmp_path, isolated_cwd):
    _channels_setup(tmp_path, preset=_CH_PRESET + '\n[subtitles]\nletterbox_font_size = 50\n')
    resp = client(tmp_path).get(f"/api/channels/{CH}/subtitles-preview", params={"text": "Salut"})
    assert resp.status_code == 200 and resp.content.startswith(b"\x89PNG")


def test_subtitles_preview_with_an_unsaved_draft_uses_the_draft_style(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline

    _channels_setup(tmp_path)
    seen = []
    monkeypatch.setattr(pipeline, "preview_subtitles",
                        lambda config, text: seen.append((config.section("subtitles"), config.section("reframe"))) or b"\x89PNG\r\n\x1a\n")
    draft = json.dumps({"subtitles": {"letterbox_outline": 11}, "reframe": {"stream_variant": "split"}})

    resp = client(tmp_path).get(f"/api/channels/{CH}/subtitles-preview", params={"text": "Salut", "draft": draft})

    assert resp.status_code == 200
    assert seen[0][0]["letterbox_outline"] == 11 and seen[0][1]["stream_variant"] == "split"
    # le preset enregistré n'a pas bougé
    assert "letterbox_outline" not in (tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8")


def test_subtitles_preview_invalid_style_is_422_with_detail(tmp_path, isolated_cwd):
    _channels_setup(tmp_path, preset=_CH_PRESET + '\n[subtitles]\nsplit_text_color = "caca"\n')
    c = client(tmp_path)
    # le style effectif est celui du format split : la couleur invalide est rendue
    draft = json.dumps({"reframe": {"stream_variant": "split"}, "subtitles": {"split_text_color": "caca"}})
    resp = c.get(f"/api/channels/{CH}/subtitles-preview", params={"text": "Salut", "draft": draft})
    assert resp.status_code == 422
    assert "couleur" in resp.json()["detail"]


def test_subtitles_preview_unknown_channel_404_and_bad_draft_422(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)
    assert c.get("/api/channels/inconnue/subtitles-preview", params={"text": "x"}).status_code == 404
    bad = c.get(f"/api/channels/{CH}/subtitles-preview", params={"text": "x", "draft": "{pas du json"})
    assert bad.status_code == 422 and "draft" in bad.json()["detail"]
    empty = c.get(f"/api/channels/{CH}/subtitles-preview", params={"text": "  "})
    assert empty.status_code == 422 and "texte" in empty.json()["detail"]


def test_channels_screen_previews_subtitles_style_with_a_300ms_debounce():
    js = (STATIC / "screens" / "channels.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    shot = (STATIC / "screens" / "chan-subtitles-preview.js")
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    prev = shot.read_text(encoding="utf-8")

    assert "/static/screens/chan-subtitles-preview.js" in page
    assert page.index("/static/screens/channels.js") < page.index("/static/screens/chan-subtitles-preview.js")
    assert "/subtitles-preview" in prev and "PREVIEW_DELAY_MS = 300" in prev
    assert "setTimeout" in prev and "clearTimeout" in prev
    assert "draft: JSON.stringify" in prev   # brouillon non enregistré envoyé tel quel
    assert ".chan-preview" in css
    assert "chSubsPreview" in js          # point d'accroche dans l'écran chaînes
# TASK-7d86 puis SPEC-86fe : mesures internes de Clipper (GET /api/measures), affichees par le Tableau de bord
# --------------------------------------------------------------------------

STATS_A, STATS_B, STATS_C, STATS_D = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"


def _stats_jsonl(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _stats_seed(tmp_path) -> None:
    def steps(download, render, finished):
        return {
            "download": _step(f"{finished}T10:00:00+00:00", f"{finished}T10:00:{download:02d}+00:00"),
            "render": _step(f"{finished}T10:05:00+00:00", f"{finished}T10:{5 + render // 60:02d}:{render % 60:02d}+00:00"),
        }
    _write_state(tmp_path, STATS_A, status="done", updated_at="2026-09-10T12:00:00+00:00",
                 steps=steps(40, 100, "2026-09-10"))
    _write_state(tmp_path, STATS_B, status="done", updated_at="2026-08-01T12:00:00+00:00",
                 steps=steps(20, 200, "2026-08-01"))
    _write_state(tmp_path, STATS_C, status="failed", updated_at="2026-09-12T12:00:00+00:00")
    _write_state(tmp_path, STATS_D, status="running", updated_at="2026-09-15T12:00:00+00:00")
    _write_sidecar(tmp_path, STATS_A, "01", moment_id=1, created_at="2026-09-10T12:00:00+00:00",
                   qa={"status": "passed", "issues": []})
    _write_sidecar(tmp_path, STATS_A, "02", moment_id=2, created_at="2026-09-20T12:00:00+00:00",
                   qa={"status": "rejected", "issues": ["sous-titres hors cadre"]})
    _write_sidecar(tmp_path, STATS_B, "01", moment_id=1, created_at="2026-08-01T12:00:00+00:00",
                   qa={"status": "passed", "issues": []})
    _stats_jsonl(tmp_path / "state" / "outcomes.jsonl", [
        {"kind": "result", "video_id": STATS_A, "clip_id": "01", "moment_id": 1,
         "qa": {"status": "passed", "issues": []}, "human_decision": "approved",
         "recorded_at": "2026-09-11T08:00:00+00:00"},
        {"kind": "stats", "video_id": None, "clip_id": "02", "moment_id": None,
         "stats": {"views": 900, "retention_3s": 0.5, "watched_full": 0.2, "shares": 3, "date": "2026-09-22"},
         "recorded_at": "2026-09-22T08:00:00+00:00"},
        {"kind": "stats", "video_id": None, "clip_id": "02", "moment_id": None,
         "stats": {"views": 1200, "retention_3s": 0.61, "watched_full": 0.22, "shares": 14, "date": "2026-09-25"},
         "recorded_at": "2026-09-25T08:00:00+00:00"},
        {"kind": "stats", "video_id": None, "clip_id": "01", "moment_id": None,
         "stats": {"views": 50, "retention_3s": 0.4, "watched_full": 0.1, "shares": 0, "date": "2026-09-25"},
         "recorded_at": "2026-09-25T08:00:00+00:00"},
    ])
    _stats_jsonl(tmp_path / "state" / "feedback.jsonl", [
        {"video_id": STATS_A, "moment": {"id": 2}, "texte_moment": "t", "decision": "adjusted",
         "commentaire": None, "horodatage": "2026-09-19T08:00:00+00:00"},
        {"video_id": STATS_A, "moment": {"id": 1}, "texte_moment": "t", "decision": "rejected",
         "commentaire": None, "horodatage": "2026-09-09T08:00:00+00:00"},
    ])

    def usage(usage_name, cost, when):
        return {"recorded_at": f"{when}T12:00:00+00:00", "usage": usage_name, "model": "m",
                "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cost_usd": cost, "duration_s": 1.0}
    _stats_jsonl(tmp_path / "workspace" / STATS_A / "llm_usage.jsonl", [
        usage("moments", 0.5, "2026-09-10"), usage("jury", 0.25, "2026-09-10"),
        usage("moments", 1.0, "2026-09-20"), usage("jury", None, "2026-09-20"),
    ])
    _stats_jsonl(tmp_path / "workspace" / STATS_B / "llm_usage.jsonl", [usage("moments", 2.0, "2026-08-01")])


def _stats(tmp_path, query=""):
    resp = client(tmp_path).get(f"/api/measures{query}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_stats_empty_state_has_explicit_empty_blocks(tmp_path, isolated_cwd):
    data = _stats(tmp_path)

    assert "clips" not in data and "stats_unmatched" not in data and "tiktok_stats" not in data  # plus rien de TikTok ni des clips ici
    assert data["llm_cost"] == {"total": 0.0, "unreported_calls": 0, "by_video": {}, "by_usage": {}, "by_day": {}}
    assert data["steps"] == {}
    assert data["counts"] == {s: 0 for s in ("pending", "running", "awaiting_review", "queued", "done", "failed")}




def test_stats_without_period_covers_everything(tmp_path, isolated_cwd):
    _stats_seed(tmp_path)

    data = _stats(tmp_path)

    assert data["llm_cost"]["total"] == pytest.approx(3.75)


def test_stats_llm_cost_per_video_usage_and_day(tmp_path, isolated_cwd):
    _stats_seed(tmp_path)

    cost = _stats(tmp_path, "?since=2026-09-01&until=2026-09-30")["llm_cost"]

    assert cost["total"] == pytest.approx(1.75)
    assert cost["unreported_calls"] == 1                              # jamais compté pour 0
    assert cost["by_usage"] == {"moments": pytest.approx(1.5), "jury": pytest.approx(0.25)}
    assert cost["by_day"] == {"2026-09-10": pytest.approx(0.75), "2026-09-20": pytest.approx(1.0)}
    assert list(cost["by_video"]) == [STATS_A]
    assert cost["by_video"][STATS_A] == {"cost": pytest.approx(1.75), "calls": 4, "unreported_calls": 1}


def test_stats_steps_mean_and_last_duration_on_done_videos(tmp_path, isolated_cwd):
    _stats_seed(tmp_path)

    steps = _stats(tmp_path)["steps"]

    assert steps["download"] == {"mean_s": pytest.approx(30.0), "last_s": pytest.approx(40.0), "count": 2}
    assert steps["render"] == {"mean_s": pytest.approx(150.0), "last_s": pytest.approx(100.0), "count": 2}
    assert "transcribe" not in steps                                   # étape jamais terminée : pas de durée inventée
    period = _stats(tmp_path, "?since=2026-09-01")["steps"]
    assert period["download"]["count"] == 1 and period["download"]["mean_s"] == pytest.approx(40.0)


def test_stats_counts_videos_by_status(tmp_path, isolated_cwd):
    _stats_seed(tmp_path)

    counts = _stats(tmp_path)["counts"]
    assert counts == {"pending": 0, "running": 1, "awaiting_review": 0, "queued": 0, "done": 2, "failed": 1}
    assert _stats(tmp_path, "?until=2026-08-31")["counts"]["done"] == 1


@pytest.mark.parametrize("query", ["?since=hier", "?until=2026-13-45", "?since=2026-09-30&until=2026-09-01"])
def test_stats_invalid_period_is_a_422_with_detail(tmp_path, isolated_cwd, query):
    resp = client(tmp_path).get(f"/api/measures{query}")
    assert resp.status_code == 422
    assert resp.json()["detail"]
















def test_style_css_braces_are_balanced():
    # Une accolade manquante avale tout le CSS qui suit (écrans Surveillance, Statistiques).
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(encoding="utf-8"), flags=re.S)
    assert css.count("{") == css.count("}")


# --------------------------------------------------------------------------
# Acces distant de bout en bout (SPEC-c100 T5, T7 ; ADR-4f6e §5)
# --------------------------------------------------------------------------


def _serve_setup(tmp_path, monkeypatch, web_toml: str = ""):
    import uvicorn

    from clipper import __main__ as cli

    (tmp_path / "config.toml").write_text(
        f'mode = "review"\n{web_toml}',
        encoding="utf-8",
    )
    runs: list[tuple] = []
    spawned: list[list[str]] = []

    class _Proc:
        def terminate(self) -> None:
            spawned.append(["terminate"])

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append((app, kw)))
    monkeypatch.setattr(cli, "_popen", lambda cmd, *a, **kw: spawned.append(cmd) or _Proc())
    return cli, runs, spawned


def test_cli_serve_host_without_token_refuses_to_start_and_names_the_key(
    tmp_path, isolated_cwd, monkeypatch, capsys
):
    cli, runs, spawned = _serve_setup(tmp_path, monkeypatch)

    assert cli.main(["serve", "--host", "0.0.0.0"]) == 1

    err = capsys.readouterr().err
    assert "[web] token" in err and "0.0.0.0" in err and "jeton" in err
    assert runs == [] and spawned == []  # ni serveur ni worker lancés


def test_cli_serve_host_from_config_without_token_also_refuses(tmp_path, isolated_cwd, monkeypatch, capsys):
    cli, runs, _ = _serve_setup(tmp_path, monkeypatch, '[web]\nhost = "0.0.0.0"\n')

    assert cli.main(["serve"]) == 1
    assert "[web] token" in capsys.readouterr().err
    assert runs == []


def test_cli_serve_host_with_token_runs_uvicorn_on_the_requested_host(tmp_path, isolated_cwd, monkeypatch):
    cli, runs, _ = _serve_setup(tmp_path, monkeypatch, '[web]\ntoken = "secret-de-test"\n')

    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9100"]) == 0

    app, kwargs = runs[0]
    assert kwargs["host"] == "0.0.0.0" and kwargs["port"] == 9100
    # l'application appliquée est bien protégée par le jeton
    assert TestClient(app).get("/api/queue").status_code == 401


def test_cli_serve_host_loopback_needs_no_token(tmp_path, isolated_cwd, monkeypatch):
    cli, runs, _ = _serve_setup(tmp_path, monkeypatch)

    assert cli.main(["serve", "--host", "127.0.0.1"]) == 0
    assert runs[0][1]["host"] == "127.0.0.1"


def test_page_served_without_cookie_shows_the_token_entry_and_api_is_401(tmp_path, isolated_cwd):
    config = _config_with_web(tmp_path, host="0.0.0.0", token="secret")
    test_client = TestClient(create_app(config=config))

    page = test_client.get("/")
    assert page.status_code == 200
    assert 'id="token-view"' in page.text and 'id="token-form"' in page.text
    assert test_client.get("/api/videos").status_code == 401
    assert test_client.get("/api/videos", headers={"x-clipper-token": "secret"}).status_code == 200


def test_browser_notification_permission_is_asked_only_from_the_local_setting(tmp_path, isolated_cwd):
    js = served(tmp_path, "/static/app.js")
    assert js.count("requestPermission(") == 1
    start = js.index("async function toggleNotifications")
    assert js.index("requestPermission(") > start  # dans le basculement du réglage
    boot = js[js.index("(function boot()"):]
    assert "requestPermission" not in boot  # jamais au chargement
    assert "localStorage" in js and "clipper-notifications" in js  # réglage local au navigateur
    # on ne notifie que si le réglage est actif ET la permission accordée
    assert 'notificationsOn() && typeof Notification !== "undefined" && Notification.permission === "granted"' in js
    for status in ("done", "failed", "awaiting_review", "queued"):
        assert f"{status}: {{" in js, status


_ROOT = Path(__file__).resolve().parent.parent


def _console_section() -> str:
    guide = (_ROOT / "docs" / "GUIDE.md").read_text(encoding="utf-8")
    start = guide.index("## Console de gestion")
    end = guide.find("\n## ", start + 1)
    return guide[start:end if end != -1 else None]


def test_guide_console_section_covers_screens_queue_presets_state_watch_and_remote_access():
    section = _console_section()
    for screen in ("Accueil", "Vidéos", "Revue", "Clips", "Styles", "Publication", "Statistiques", "Réglages"):
        assert screen in section, screen
    assert "Les 8 écrans" in section
    for needle in (
        "state/queue.json", "state/watch/", "state/publish/",   # file et state/
        "surcouche", "presets/ma_chaine.toml",                  # presets en surcouche + exemple
        "watch = true", "à confirmer",                          # surveillance
        "[web] token", "--host", "401",                         # accès distant par jeton
        "Réseau local seulement", "Pas de TLS", "reverse proxy TLS",   # limites
        "notifications du navigateur",
    ):
        assert needle in section, needle


def test_readme_and_changelog_mention_the_console_v2():
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Console de gestion web (v2)" in readme and "--host 0.0.0.0" in readme
    assert "Console de gestion web v2" in changelog and "[web] token" in changelog


def test_console_docs_example_preset_is_valid(tmp_path, isolated_cwd):
    import re

    from clipper import channel

    section = _console_section()
    block = re.search(r"```toml\n(\[channel\].*?)```", section, re.S).group(1)
    (tmp_path / "config.toml").write_text('mode = "review"\n', encoding="utf-8")
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "ma_chaine.toml").write_text(block, encoding="utf-8")

    _config, chan = channel.load_channel("ma_chaine")

    assert chan["display_name"] == "ma_chaine" and chan["watch"] is True


# --------------------------------------------------------------------------
# Corrections console v2 (TASK-dc9d)
# --------------------------------------------------------------------------

ANSI_ERROR = "\x1b[0;31mERROR:\x1b[0m \x1b[1mvideo indisponible\x1b[0m"
ANSI_CLEAN = "ERROR: video indisponible"


def test_api_strips_ansi_sequences_from_failure_reasons_and_journal(tmp_path, isolated_cwd):
    state = _write_state(tmp_path, VIDEO_ID, status="failed", reason=ANSI_ERROR)
    state["steps"]["download"].update(status="failed", reason=ANSI_ERROR)
    (tmp_path / "workspace" / VIDEO_ID / "pipeline.json").write_text(json.dumps(state), encoding="utf-8")
    (tmp_path / "workspace" / VIDEO_ID / "events.jsonl").write_text(json.dumps(
        {"at": "2026-01-01T10:00:00+00:00", "level": "ERROR", "step": "download", "message": ANSI_ERROR}) + "\n",
        encoding="utf-8")
    c = client(tmp_path)

    video = c.get(f"/api/videos/{VIDEO_ID}")
    events = c.get(f"/api/videos/{VIDEO_ID}/events")
    dashboard = c.get("/api/dashboard")
    listing = c.get("/api/videos")

    for resp in (video, events, dashboard, listing):
        assert resp.status_code == 200
        assert "\x1b" not in resp.text and "\\u001b" not in resp.text
    assert video.json()["reason"] == ANSI_CLEAN
    assert events.json()[0]["message"] == ANSI_CLEAN
    assert dashboard.json()["failed"][0]["reason"] == ANSI_CLEAN


def test_api_strips_ansi_sequences_from_error_details(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline

    def boom(*a, **k):
        raise pipeline.PipelineError(ANSI_ERROR)

    monkeypatch.setattr(pipeline, "load_state", boom)

    resp = client(tmp_path).get(f"/api/videos/{VIDEO_ID}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == ANSI_CLEAN


def test_strip_ansi_keeps_plain_text_and_handles_nested_json():
    from clipper.web.app import _strip_ansi

    assert _strip_ansi({"a": ["\x1b[31mx\x1b[0m", 3, None], "b": "é ça [0m reste"}) == {
        "a": ["x", 3, None], "b": "é ça [0m reste"}


THUMB_VIDEO = "abcdefghijk"


def test_media_clip_thumbnail_route_serves_the_pipeline_thumbnail(tmp_path, isolated_cwd, monkeypatch):
    from clipper import pipeline, render

    out_dir = tmp_path / "output" / THUMB_VIDEO
    out_dir.mkdir(parents=True)
    (out_dir / "01.mp4").write_bytes(b"mp4")
    calls = []

    def fake_exec(cmd, cwd, out_path):
        calls.append(cmd)
        Path(out_path).write_bytes(b"\xff\xd8jpeg-bytes")

    monkeypatch.setattr(render, "_exec_ffmpeg", fake_exec)
    c = client(tmp_path)

    first = c.get(f"/media/clip/{THUMB_VIDEO}/01/thumbnail")
    second = c.get(f"/media/clip/{THUMB_VIDEO}/01/thumbnail")

    assert first.status_code == 200 and first.content == b"\xff\xd8jpeg-bytes"
    assert first.headers["content-type"] == "image/jpeg"
    assert "max-age" in first.headers["cache-control"]
    assert second.content == first.content and len(calls) == 1
    assert pipeline.clip_thumbnail(make_config(tmp_path), THUMB_VIDEO, "01").is_file()


def test_media_clip_thumbnail_route_errors_are_explicit(tmp_path, isolated_cwd, monkeypatch):
    from clipper import render

    c = client(tmp_path)
    assert c.get(f"/media/clip/{THUMB_VIDEO}/..%2Fx/thumbnail").status_code == 404
    assert c.get("/media/clip/../01/thumbnail").status_code in (404, 422)
    missing = c.get(f"/media/clip/{THUMB_VIDEO}/99/thumbnail")
    assert missing.status_code == 404 and "introuvable" in missing.json()["detail"]

    out_dir = tmp_path / "output" / THUMB_VIDEO
    out_dir.mkdir(parents=True)
    (out_dir / "01.mp4").write_bytes(b"mp4")

    def boom(cmd, cwd, out_path):
        raise render.RenderError("ffmpeg introuvable (ffmpeg)")

    monkeypatch.setattr(render, "_exec_ffmpeg", boom)
    failed = c.get(f"/media/clip/{THUMB_VIDEO}/01/thumbnail")
    assert failed.status_code == 422 and "miniature" in failed.json()["detail"]


def test_clips_gallery_uses_lazy_thumbnails_and_no_video_tag_in_the_grid():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")
    card = js[js.index("function clipCard"):js.index("function clipsEmpty")]

    assert '<img loading="lazy"' in card and "thumbnail_url" in card
    assert "<video" not in card
    # la vidéo n'est chargée qu'à l'ouverture d'un clip (fiche)
    drawer = js[js.index("function clipDrawerHtml"):]
    assert "<video" in drawer
    assert "CLIPS_PAGE_SIZE = 24" in js and "Afficher plus" in js


def test_clip_views_expose_the_thumbnail_url(tmp_path, isolated_cwd):
    _clips_setup(tmp_path)

    clips = {c["clip_id"]: c for c in client(tmp_path).get("/api/clips", params={"video_id": CLIPS_VIDEO}).json()}

    assert clips["01"]["thumbnail_url"] == f"/media/clip/{CLIPS_VIDEO}/01/thumbnail"


def test_new_channel_button_opens_a_defined_and_visible_modal_panel():
    import re

    channels = (STATIC / "screens" / "channels.js").read_text(encoding="utf-8")
    ui = (STATIC / "ui.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    page = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "[data-chan-new]" in channels and "onclick = chOpenNew" in channels
    call = re.search(r'function chOpenNew\(\) \{\s*openPanel\("([^"]+)"', channels)
    assert call, "chOpenNew doit appeler openPanel"
    assert "function openPanel(" in ui
    assert page.index("/static/ui.js") < page.index("/static/screens/channels.js")
    # classe CSS du panneau : définie, positionnée au-dessus du fond, visible une fois .show posé
    classes = call.group(1).split()
    assert "modal" in classes
    rule = re.search(r"^\.modal \{([^}]*)\}", css, re.M).group(1)
    shown = re.search(r"^\.modal\.show \{([^}]*)\}", css, re.M).group(1)
    assert "position: fixed" in rule and "opacity: 0" in rule and "opacity: 1" in shown
    z = lambda sel: int(re.search(rf"^{re.escape(sel)} \{{[^}}]*z-index: (\d+)", css, re.M).group(1))
    assert z(".modal") > z(".overlay")
    assert "classList.add(\"show\")" in ui
    # le panneau ne dépend pas du seul requestAnimationFrame (suspendu hors écran) pour devenir visible
    panel = ui[ui.index("function openPanel("):ui.index("document.addEventListener(\"keydown\"")]
    assert "setTimeout(reveal" in panel and "try { onOpen(el); }" in panel


# --------------------------------------------------------------------------
# TASK-a40d : corrections du deuxième tour de la console v2
# --------------------------------------------------------------------------

def _write_full_sidecar(tmp_path, video_id, clip_id, **fields):
    """Sidecar tel que le rend le pipeline : il porte son video_id (SPEC-6a47)."""
    sidecar = {"video_id": video_id, "clip_id": clip_id, "ready": True, "screen_title": f"Titre {clip_id}", **fields}
    _write_json(tmp_path / "output" / video_id / f"{clip_id}.json", sidecar)


_QA_ISSUE = {"type": "black_frames", "detail": "image noire de 2 s", "source": "local", "severity": "blocking"}


def _round2_clips(tmp_path):
    """3 clips d'une vidéo avec chaîne, dont 1 refusé par la QA : 2 à valider."""
    _write_state(tmp_path, "aaaaaaaaaaa", channel="ma_chaine")
    _write_full_sidecar(tmp_path, "aaaaaaaaaaa", "01", qa={"status": "passed", "issues": []})
    _write_full_sidecar(tmp_path, "aaaaaaaaaaa", "02", qa={"status": "passed", "issues": []})
    _write_full_sidecar(tmp_path, "aaaaaaaaaaa", "03", ready=False, qa={"status": "rejected", "issues": [_QA_ISSUE]})








def test_qa_rejected_clip_is_neither_counted_nor_listed_as_to_validate(tmp_path, isolated_cwd):
    _round2_clips(tmp_path)
    c = client(tmp_path)

    to_validate = c.get("/api/clips", params={"status": "à valider"}).json()
    rejected = c.get("/api/clips", params={"status": "rejected"}).json()

    assert [x["clip_id"] for x in to_validate] == ["01", "02"]
    assert [x["clip_id"] for x in rejected] == ["03"]
    assert rejected[0]["qa_status"] == "rejected"
    assert len(c.get("/api/clips").json()) == 3


def test_dashboard_and_clips_page_count_the_same_clips_to_validate(tmp_path, isolated_cwd):
    _round2_clips(tmp_path)
    _write_state(tmp_path, "bbbbbbbbbbb", channel=None)
    _write_full_sidecar(tmp_path, "bbbbbbbbbbb", "01")
    c = client(tmp_path)

    on_page = len(c.get("/api/clips", params={"status": "à valider"}).json())

    assert _dashboard(tmp_path)["clips_to_review"] == on_page == 3


def test_clip_not_ready_and_not_rejected_is_not_to_validate_either(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa", channel="ma_chaine")
    _write_full_sidecar(tmp_path, "aaaaaaaaaaa", "01", ready=False)
    c = client(tmp_path)

    assert c.get("/api/clips", params={"status": "à valider"}).json() == []
    assert _dashboard(tmp_path)["clips_to_review"] == 0
    assert c.get("/api/clips").json()[0]["publish_status"] == "not_ready"


def test_clips_screen_labels_every_server_status_including_not_ready():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")

    assert "not_ready" in js


def test_accounts_ready_box_is_clickable_and_drives_pause_and_resume():
    js = (STATIC / "screens" / "accounts.js").read_text(encoding="utf-8")
    box = js[js.index("function accReadyBox"):js.index("function accSlots")]
    handler = js[js.index("$$(\"[data-acc-ready]\""):js.index("$$(\"[data-acc-resolve]\"")]

    assert "aria-readonly" not in box and "aria-readonly" not in js
    assert "En pause (manuel) depuis le" in box
    assert "paused_at" in box and "accFmtDate(a.paused_at)" in box  # fmtParis via accFmtDate
    assert "/pause" in js and "/resume" in js
    assert "accPause(" in handler and "accResume(" in handler and "accBrowserLogin(" in handler
    assert handler.index("ready_to_publish") < handler.index("paused_at") < handler.index("accBrowserLogin(")
    assert "Compte en pause" in js and "Toujours pas prêt" in js and "Compte prêt à publier" in js


def test_clips_and_publish_keep_ready_filter_and_label_paused_accounts():
    clips = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")
    publish = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")

    assert clips.count("filter((a) => a.ready_to_publish)") >= 2
    assert publish.count("filter((a) => a.ready_to_publish)") >= 2
    assert "a.ready_to_publish || a.id === c.account" in publish
    assert "(en pause)" in publish and "a.paused_at" in publish
    assert "(non prêt à publier)" in publish


def test_guide_and_changelog_describe_manual_account_pause():
    guide = (Path(__file__).resolve().parent.parent / "docs" / "GUIDE.md").read_text(encoding="utf-8")
    changelog = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    # [Non publié] puis la version en cours (0.6.0) : l'entrée suit la version qui la publie.
    unreleased = changelog[changelog.index("Non publié"):changelog.index("## [0.5.3]")]

    assert "pause" in guide.lower() and "reprise" in guide.lower() and "reprendre" in guide.lower()
    assert "restent en attente" in guide and "côté plateforme" in guide and "boucle suivante" in guide
    assert "pause manuelle" in unreleased.lower()


def _serve_like_config(tmp_path, port):
    """Config lue dans config.toml puis surchargée comme le fait 'serve --port'."""
    import dataclasses

    from clipper.config import load_config

    config = load_config(tmp_path / "config.toml")
    sections = {**config._sections, "web": {**config._sections.get("web", {}), "port": port}}
    return dataclasses.replace(config, _sections=sections)


def test_settings_access_shows_the_real_port_and_flags_the_difference_with_config_toml(tmp_path, isolated_cwd):
    _settings_setup(tmp_path, '[web]\nport = 8000\n')
    app_client = TestClient(create_app(config=_serve_like_config(tmp_path, 8765)))

    data = app_client.get("/api/settings").json()

    access = data["access"]
    assert access["port"] == 8765 and "--port 8765" in access["command"]
    assert access["config_port"] == 8000 and access["config_host"] == "127.0.0.1"
    assert access["differs_from_config"] is True
    assert data["restart_required"] is True                          # config.toml dit 8000, l'écoute en cours 8765


def test_settings_access_does_not_flag_anything_when_serve_matches_config_toml(tmp_path, isolated_cwd):
    _settings_setup(tmp_path, '[web]\nport = 8000\n')

    access = sclient(tmp_path).get("/api/settings").json()["access"]

    assert access["port"] == 8000 and access["differs_from_config"] is False


def test_cli_serve_hands_the_real_port_to_the_app(tmp_path, isolated_cwd, monkeypatch):
    cli, runs, _ = _serve_setup(tmp_path, monkeypatch, '[web]\nport = 8000\n')

    assert cli.main(["serve", "--port", "8765"]) == 0

    app, kwargs = runs[0]
    assert kwargs["port"] == 8765
    access = TestClient(app).get("/api/settings").json()["access"]
    assert access["port"] == 8765 and access["config_port"] == 8000 and access["differs_from_config"] is True


def test_settings_screen_shows_the_real_access_and_the_difference():
    js = (STATIC / "screens" / "settings.js").read_text(encoding="utf-8")

    assert "differs_from_config" in js and "config_port" in js


def test_settings_access_shows_a_single_notice_when_the_running_server_differs_from_config_toml():
    js = (STATIC / "screens" / "settings.js").read_text(encoding="utf-8")
    start = js.index("function setAccess()")
    body = js[start:js.index("\n}\n", start)]

    # l'avis « redémarrage » n'est affiché que si l'avis d'écart (hôte/port) ne l'est pas déjà
    assert "restart_required && !access.differs_from_config" in body
    # l'avis d'écart dit les deux valeurs et quand le fichier s'appliquera
    assert "access.host" in body and "access.port" in body and "config_host" in body and "config_port" in body
    assert "au prochain « serve » lancé sans --host/--port" in body
    assert "un redémarrage de « serve » est nécessaire" in body  # avis conservé pour un jeton seul


def _fresh_heartbeat(tmp_path, **fields):
    beat = {"pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat(), **fields}
    _write_json(tmp_path / "state" / "worker.json", beat)
    return beat


def test_dashboard_exposes_a_worker_stopped_with_the_command_when_no_heartbeat(tmp_path, isolated_cwd):
    worker = _dashboard(tmp_path)["worker"]

    assert worker["state"] == "stopped"
    assert worker["command"] == "python -m clipper worker"
    assert worker["reason"]


def test_dashboard_exposes_a_worker_active_with_pid_and_age(tmp_path, isolated_cwd):
    beat = _fresh_heartbeat(tmp_path)

    worker = _dashboard(tmp_path)["worker"]

    assert worker["state"] == "active" and worker["pid"] == beat["pid"]
    assert worker["age_s"] < 30


def test_dashboard_worker_with_a_stale_heartbeat_is_not_active(tmp_path, isolated_cwd):
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_json(tmp_path / "state" / "worker.json", {"pid": os.getpid(), "at": old.isoformat()})

    worker = _dashboard(tmp_path)["worker"]

    assert worker["state"] == "stale"
    assert worker["command"] == "python -m clipper worker"


def test_dashboard_unreadable_heartbeat_is_null_with_a_french_error(tmp_path, isolated_cwd):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "worker.json").write_text("{pas du json", encoding="utf-8")

    data = _dashboard(tmp_path)

    assert data["worker"] is None and "worker" in data["worker_error"]


def test_dashboard_screen_shows_the_worker_light_and_the_command_when_not_active():
    js = (STATIC / "screens" / "dashboard.js").read_text(encoding="utf-8")

    assert "data.worker" in js and "worker_error" in js
    assert "worker actif" in js and "worker arrêté" in js
    assert "worker.command" in js and '"active"' in js


def test_heartbeat_file_does_not_flood_the_event_stream(tmp_path, isolated_cwd):
    from clipper.web.app import _scan_watched

    _fresh_heartbeat(tmp_path)

    kinds = {kind for _, kind, _ in _scan_watched(tmp_path / "workspace", [(tmp_path / "state", None)])}

    assert "worker" not in kinds


_UNACCENTED = (
    "reponse", "refusee", "hote", "ecoute", "defaut", "parametre", "frequence", "modele", "ecran", "meme",
    "memes", "etape", "regle", "regles", "reglage", "reglages", "deja", "apres", "cle", "cles", "camera",
    "video", "videos", "schema", "tete", "boite", "echec", "ecart", "resultat", "resultats", "selection",
    "duree", "donnee", "donnees", "detecteur", "detection", "systeme", "memoire", "premiere", "derniere",
    "entiere", "centree", "elargie", "reduite", "reduit", "reduits", "desactive", "desactivee", "generes",
    "journalisee", "reel", "reelle", "reellement", "bornee", "scene", "scenes", "apparait", "recoit",
    "telecharge", "telechargement", "appliquee", "precedent", "precedente", "presence", "pensees", "tolere",
    "tolerance", "echantillonnee", "equirepartis", "etiree", "evite", "fenetre", "unite", "serie", "sure",
    "cote", "cotes", "plutot", "ecartees", "elargi", "deborde", "defauts", "derriere",
)


def _exposed_comments(tmp_path) -> dict[str, str]:
    c = sclient(tmp_path)
    found = {}
    for section, keys in c.get("/api/settings").json()["defaults"].items():
        for key, doc in keys.items():
            found[f"settings/{section}.{key}"] = doc["comment"]
    return found


def test_help_texts_exposed_by_the_api_are_accented_french(tmp_path, isolated_cwd):
    import re

    from clipper.web import app as web_app

    comments = _exposed_comments(tmp_path)
    for section in web_app._CHANNEL_FORM_SECTIONS:
        for key, doc in web_app._defaults_documentation(section).items():
            comments[f"channel/{section}.{key}"] = doc["comment"]
    assert any(comments.values())
    words = list(_UNACCENTED)
    offenders = []
    for where, comment in comments.items():
        # hors identifiants : « scenes.json », « video_id », `nom_de_cle` ne sont pas du texte
        prose = re.sub(r"[\w./-]*[_./][\w./-]*", " ", comment)
        for word in words:
            if re.search(rf"(?<![\w-]){word}(?![\w-])", prose, re.IGNORECASE):
                offenders.append(f"{where} : « {word} »")
    assert offenders == []


def test_videos_are_listed_most_recently_added_first(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa", enqueued_at="2026-09-01T10:00:00+00:00")
    _write_state(tmp_path, "ccccccccccc", enqueued_at="2026-09-03T10:00:00+00:00")
    _write_state(tmp_path, "bbbbbbbbbbb", enqueued_at="2026-09-02T10:00:00+00:00")

    ids = [v["video_id"] for v in client(tmp_path).get("/api/videos").json()]

    assert ids == ["ccccccccccc", "bbbbbbbbbbb", "aaaaaaaaaaa"]


def _drop_enqueued_at(tmp_path, video_id):
    path = tmp_path / "workspace" / video_id / "pipeline.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    del state["enqueued_at"]
    path.write_text(json.dumps(state), encoding="utf-8")


def test_videos_without_enqueued_at_are_sorted_by_pipeline_json_creation_date(tmp_path, isolated_cwd, monkeypatch):
    _write_state(tmp_path, "aaaaaaaaaaa")
    _write_state(tmp_path, "bbbbbbbbbbb")
    _write_state(tmp_path, "ccccccccccc", enqueued_at="2026-09-02T10:00:00+00:00")
    for video_id in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        _drop_enqueued_at(tmp_path, video_id)
    created = {"aaaaaaaaaaa": "2026-09-03T10:00:00+00:00", "bbbbbbbbbbb": "2026-09-01T10:00:00+00:00"}
    monkeypatch.setattr(
        web_app, "_pipeline_created_at",
        lambda path: (datetime.fromisoformat(created[path.parent.name]), "pipeline_json_created"),
    )

    videos = client(tmp_path).get("/api/videos").json()

    assert [v["video_id"] for v in videos] == ["aaaaaaaaaaa", "ccccccccccc", "bbbbbbbbbbb"]
    by_id = {v["video_id"]: v for v in videos}
    assert by_id["ccccccccccc"]["added_at"] == "2026-09-02T10:00:00+00:00"
    assert by_id["ccccccccccc"]["added_at_source"] == "enqueued_at"
    assert by_id["aaaaaaaaaaa"]["added_at"] == "2026-09-03T10:00:00+00:00"
    assert by_id["aaaaaaaaaaa"]["added_at_source"] == "pipeline_json_created"
    assert all(v["added_at"] for v in videos)


def test_pipeline_created_at_reads_the_real_file(tmp_path):
    path = tmp_path / "pipeline.json"
    path.write_text("{}", encoding="utf-8")

    when, source = web_app._pipeline_created_at(path)

    assert when.tzinfo is not None and source in {"pipeline_json_created", "pipeline_json_mtime"}
    assert abs((datetime.now(timezone.utc) - when).total_seconds()) < 60


def test_static_files_are_always_revalidated(tmp_path, isolated_cwd):
    c = client(tmp_path)
    for url in ("/", "/static/app.js", "/static/screens/videos.js"):
        resp = c.get(url)
        assert resp.status_code == 200, url
        assert resp.headers["cache-control"] == "no-cache", url
        etag = resp.headers["etag"]
        again = c.get(url, headers={"If-None-Match": etag})
        assert again.status_code == 304, url
        assert again.headers["cache-control"] == "no-cache", url


def test_no_retired_adr_or_spec_citation_remains_under_clipper():
    root = Path(web_app.__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".woff", ".woff2", ".png", ".ico"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offenders += [f"{path.relative_to(root)} : {old}" for old in ("ADR-4f6e", "SPEC-fc0c") if old in text]
    assert offenders == []


def test_videos_screen_keeps_the_server_order(tmp_path):
    js = (STATIC / "screens" / "videos.js").read_text(encoding="utf-8")

    assert ".sort(" not in js






# --------------------------------------------------------------------------
# TASK-c0ef : console v2, quatrieme tour
# --------------------------------------------------------------------------


def _static(*parts) -> str:
    return STATIC.joinpath(*parts).read_text(encoding="utf-8")


def test_settings_section_links_scroll_instead_of_being_read_as_a_screen():
    settings = _static("screens", "settings.js")
    app = _static("app.js")

    # Les liens gardent leur ancre, mais un clic fait defiler sans changer de hash...
    assert 'href="#set-${id}"' in settings
    assert "scrollIntoView" in settings and "preventDefault()" in settings
    assert "data-set-nav" in settings
    # ...et le routeur n'interprete jamais « #set-xxx » comme un ecran (retour au tableau de bord).
    route = app[app.index("function route()"):app.index("function renderCurrent")]
    assert 'startsWith("set-")' in route and "scrollIntoView" in route


def _write_meta_title(tmp_path, video_id, title):
    meta = tmp_path / "workspace" / video_id
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "meta.json").write_text(json.dumps({"video_id": video_id, "title": title}), encoding="utf-8")


def test_dashboard_problem_rows_carry_the_video_title_or_the_id(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa", status="failed", reason="ffmpeg a echoue")
    _write_state(tmp_path, "bbbbbbbbbbb", status="queued", reason="quota", retry_at="2026-01-02T08:00:00+00:00")
    _write_meta_title(tmp_path, "aaaaaaaaaaa", "Mon direct du soir")

    data = _dashboard(tmp_path)

    assert data["failed"][0]["title"] == "Mon direct du soir"
    assert data["queued"][0]["title"] == "bbbbbbbbbbb"  # pas de titre : l'identifiant


def test_dismiss_removes_a_failed_video_from_the_dashboard_without_deleting_its_workspace(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa", status="failed", reason="ffmpeg a echoue")
    (tmp_path / "workspace" / "aaaaaaaaaaa" / "keep.txt").write_text("x", encoding="utf-8")
    c = client(tmp_path)

    resp = c.post("/api/videos/aaaaaaaaaaa/dismiss")

    assert resp.status_code == 200 and resp.json()["dismissed_at"]
    assert _dashboard(tmp_path)["failed"] == []
    assert (tmp_path / "workspace" / "aaaaaaaaaaa" / "keep.txt").read_text(encoding="utf-8") == "x"
    detail = c.get("/api/videos/aaaaaaaaaaa").json()
    assert detail["dismissed_at"] and detail["status"] == "failed"  # l'etat reste lisible, explicite

    back = c.post("/api/videos/aaaaaaaaaaa/restore")
    assert back.status_code == 200 and "dismissed_at" not in back.json()
    assert [v["video_id"] for v in _dashboard(tmp_path)["failed"]] == ["aaaaaaaaaaa"]


def test_dismiss_also_clears_a_queued_video_from_the_counters(tmp_path, isolated_cwd):
    _write_state(tmp_path, "bbbbbbbbbbb", status="queued", reason="quota", retry_at="2026-01-02T08:00:00+00:00")

    assert client(tmp_path).post("/api/videos/bbbbbbbbbbb/dismiss").status_code == 200

    assert _dashboard(tmp_path)["queued"] == []


def test_dismiss_errors_are_explicit(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa", status="running")
    c = client(tmp_path)

    running = c.post("/api/videos/aaaaaaaaaaa/dismiss")
    assert running.status_code == 409 and "echec" in running.json()["detail"]
    missing = c.post("/api/videos/zzzzzzzzzzz/dismiss")
    assert missing.status_code == 404 and "aucun etat" in missing.json()["detail"]
    assert c.post("/api/videos/zzzzzzzzzzz/restore").status_code == 404
    assert c.post("/api/videos/..%2Fx/dismiss").status_code in (404, 422)


def test_dismissed_video_is_requeued_by_a_retry_and_reappears(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    _write_state(tmp_path, "aaaaaaaaaaa", status="failed", reason="ffmpeg a echoue")
    c = client(tmp_path)
    c.post("/api/videos/aaaaaaaaaaa/dismiss")
    monkeypatch.setattr(worker, "enqueue", lambda url, channel, action, force_steps, *, config=None: {
        "id": "e1", "video_id": "aaaaaaaaaaa", "url": url, "channel": channel, "action": action,
        "force_steps": force_steps or [], "status": "waiting"})

    assert c.post("/api/videos/aaaaaaaaaaa/retry", json={"from_step": "download"}).status_code == 202

    assert "dismissed_at" not in c.get("/api/videos/aaaaaaaaaaa").json()


def test_dashboard_problem_rows_link_to_the_video_sheet_and_offer_retry_and_dismiss():
    dash = _static("screens", "dashboard.js")
    row = dash[dash.index("function dashProblemRow"):dash.index("function dashPublicationRow")]

    assert "#/videos/${encodeURIComponent(video.video_id)}" in row and 'href="#/videos"' not in row
    assert "video.title" in row
    assert "data-retry-video" in row and "Relancer" in row
    assert "data-dismiss-video" in row and "Retirer" in row
    assert "videoThumb(" in row
    assert "/api/videos/${encodeURIComponent(id)}/retry" in _static("app.js") or "/retry" in _static("app.js")
    assert "/dismiss" in _static("app.js")


def test_video_sheet_offers_the_same_retry_and_dismiss_actions():
    videos = _static("screens", "videos.js")

    assert "data-retry-video" in videos and "data-dismiss-video" in videos
    assert "wireActions(view)" in videos  # mêmes gestionnaires (app.js) que le tableau de bord
    assert "Relancer" in videos and "Retirer" in videos and "Rétablir" in videos


def test_media_source_thumbnail_route_serves_the_pipeline_thumbnail(tmp_path, isolated_cwd, monkeypatch):
    from clipper import render

    _write_state(tmp_path, THUMB_VIDEO)
    _write_meta_duration = tmp_path / "workspace" / THUMB_VIDEO
    (_write_meta_duration / "meta.json").write_text(json.dumps({"video_id": THUMB_VIDEO, "duration": 100}), encoding="utf-8")
    (_write_meta_duration / f"{THUMB_VIDEO}.mp4").write_bytes(b"mp4")
    calls = []

    def fake_exec(cmd, cwd, out_path):
        calls.append(cmd)
        Path(out_path).write_bytes(b"\xff\xd8vthumb")

    monkeypatch.setattr(render, "_exec_ffmpeg", fake_exec)
    c = client(tmp_path)

    first = c.get(f"/media/source/{THUMB_VIDEO}/thumbnail")
    second = c.get(f"/media/source/{THUMB_VIDEO}/thumbnail")

    assert first.status_code == 200 and first.content == b"\xff\xd8vthumb"
    assert first.headers["content-type"] == "image/jpeg" and "max-age" in first.headers["cache-control"]
    assert second.content == first.content and len(calls) == 1
    assert calls[0][calls[0].index("-ss") + 1] == "10.000000"


def test_media_source_thumbnail_route_errors_are_explicit(tmp_path, isolated_cwd, monkeypatch):
    from clipper import render

    c = client(tmp_path)
    missing = c.get(f"/media/source/{THUMB_VIDEO}/thumbnail")
    assert missing.status_code == 404 and "introuvable" in missing.json()["detail"]
    assert c.get("/media/source/..%2Fx/thumbnail").status_code == 404

    video_dir = tmp_path / "workspace" / THUMB_VIDEO
    video_dir.mkdir(parents=True)
    (video_dir / f"{THUMB_VIDEO}.mp4").write_bytes(b"mp4")
    (video_dir / "meta.json").write_text(json.dumps({"duration": 100}), encoding="utf-8")

    def boom(cmd, cwd, out_path):
        raise render.RenderError("ffmpeg introuvable (ffmpeg)")

    monkeypatch.setattr(render, "_exec_ffmpeg", boom)
    failed = c.get(f"/media/source/{THUMB_VIDEO}/thumbnail")
    assert failed.status_code == 422 and "vignette" in failed.json()["detail"]


def test_video_thumbnails_are_lazy_images_with_a_neutral_fallback_everywhere():
    helper = _static("screens.js")
    assert "function videoThumb(" in helper
    thumb = helper[helper.index("function videoThumb("):]
    assert '<img loading="lazy"' in thumb and "/media/source/" in thumb and "/thumbnail" in thumb
    assert "pas d'image" in helper
    assert '"error"' in helper and "true" in helper  # l'echec de chargement remplace l'image par la vignette neutre

    for name, marker in (("dashboard.js", "function dashRunningRow"), ("dashboard.js", "function dashProblemRow")):
        src = _static("screens", name)
        assert "videoThumb(" in src[src.index(marker):src.index(marker) + 1800], marker
    assert "videoThumb(" in _static("screens.js")[_static("screens.js").index("function queueRow"):_static("screens.js").index("const addVideoButton")]
    videos = _static("screens", "videos.js")
    assert "videoThumb(" in videos[videos.index("function listRow"):videos.index("function paintList")]
    assert "videoThumb(" in videos[videos.index("function paintDetail"):videos.index("function wireDetail")]


def _stats_channels(tmp_path) -> None:
    _stats_seed(tmp_path)
    for video_id, channel in ((STATS_A, "ma_chaine"), (STATS_B, "autre")):
        path = tmp_path / "workspace" / video_id / "pipeline.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["channel"] = channel
        path.write_text(json.dumps(state), encoding="utf-8")


def test_stats_channel_filter_applies_to_every_block(tmp_path, isolated_cwd):
    _stats_channels(tmp_path)

    data = _stats(tmp_path, "?channel=ma_chaine")

    assert data["channel"] == "ma_chaine"
    assert set(data["llm_cost"]["by_video"]) == {STATS_A}
    assert data["llm_cost"]["total"] == pytest.approx(1.75)
    assert data["counts"] == {"pending": 0, "running": 0, "awaiting_review": 0, "queued": 0, "done": 1, "failed": 0}
    assert data["steps"]["download"]["mean_s"] == pytest.approx(40)


def test_stats_channel_filter_other_channel_and_no_channel(tmp_path, isolated_cwd):
    _stats_channels(tmp_path)

    other = _stats(tmp_path, "?channel=autre")
    assert other["llm_cost"]["total"] == pytest.approx(2.0) and other["counts"]["done"] == 1

    none = _stats(tmp_path, "?channel=__none__")
    assert none["llm_cost"]["by_video"] == {}
    assert none["counts"]["failed"] == 1 and none["counts"]["running"] == 1 and none["counts"]["done"] == 0


def test_stats_without_channel_parameter_still_covers_everything(tmp_path, isolated_cwd):
    _stats_channels(tmp_path)

    data = _stats(tmp_path)

    assert data["channel"] is None
    assert data["counts"]["done"] == 2






# --------------------------------------------------------------------------
# Grille de notation par chaîne (SPEC-9216 R4)
# --------------------------------------------------------------------------


def test_put_channel_writes_the_builtin_gaming_rubric_in_the_preset(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)

    resp = client(tmp_path).put(f"/api/channels/{CH}", json={"preset": {
        "channel": {"display_name": "Ma chaîne"}, "moments": {"rubric_path": "builtin:gaming"},
    }})

    assert resp.status_code == 200, resp.text
    saved = (tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8")
    assert 'rubric_path = "builtin:gaming"' in saved
    body = client(tmp_path).get(f"/api/channels/{CH}").json()
    assert body["raw"]["moments"] == {"rubric_path": "builtin:gaming"}
    assert body["effective"]["moments"]["rubric_path"] == "builtin:gaming"   # grille en vigueur


def test_get_channel_effective_rubric_is_inherited_without_a_preset_value(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)

    body = client(tmp_path).get(f"/api/channels/{CH}").json()

    assert "moments" not in body["raw"]
    assert body["effective"]["moments"]["rubric_path"] == "rubric.toml"      # défaut du module
    assert body["defaults"]["moments"]["rubric_path"]["default"] == "rubric.toml"
    assert "builtin:gaming" in body["defaults"]["moments"]["rubric_path"]["comment"]


def test_get_video_detail_reports_the_rubric_used_by_moments_json(tmp_path, isolated_cwd):
    _write_state(tmp_path, VIDEO_ID, status="done", steps={})
    _write_json(tmp_path / "workspace" / VIDEO_ID / "moments.json", {
        "video_id": VIDEO_ID, "moments": [],
        "rubric": {"path": "/x/clipper/assets/rubric-gaming.toml", "weights": {"emotion": 4}, "min_score": 45},
    })

    body = client(tmp_path).get(f"/api/videos/{VIDEO_ID}").json()

    assert body["rubric"] == "/x/clipper/assets/rubric-gaming.toml"


def test_get_video_detail_without_moments_json_has_no_rubric(tmp_path, isolated_cwd):
    _write_state(tmp_path, VIDEO_ID, status="running", steps={})

    assert client(tmp_path).get(f"/api/videos/{VIDEO_ID}").json()["rubric"] is None


def test_channels_form_offers_the_rubric_choice_standard_gaming_or_custom_file():
    js = (STATIC / "screens" / "channels.js").read_text(encoding="utf-8")

    assert "Grille de notation" in js
    for label in ("Standard", "Gaming", "Fichier personnalisé"):
        assert label in js, label
    for value in ("builtin", "builtin:gaming", "rubric_path"):
        assert f'"{value}"' in js, value
    assert "Grille en vigueur" in js                       # la grille effective est affichée
    assert 'kind === "rubric"' in js or '"rubric"' in js   # contrôle dédié, pas un champ texte brut


def test_video_sheet_shows_the_rubric_used():
    js = (STATIC / "screens" / "videos.js").read_text(encoding="utf-8")

    assert "video.rubric" in js and "Grille" in js


# --------------------------------------------------------------------------
# TASK-6a1b : un enfant du worker qui meurt apparait dans Echecs
# --------------------------------------------------------------------------


def test_dashboard_lists_a_video_whose_worker_child_died(tmp_path, isolated_cwd):
    from clipper import worker

    class _Proc:
        pid = 4242
        returncode = None

        def poll(self):
            return self.returncode

    proc = _Proc()
    config = make_config(tmp_path)

    def spawner(cmd):
        log = worker.log_path(VIDEO_ID, config)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("clipper: error: unrecognized arguments: --config presets/ma_chaine.toml\n", encoding="utf-8")
        return proc

    c = client(tmp_path)
    resp = c.post("/api/queue", json={"url": f"https://youtu.be/{VIDEO_ID}", "channel": None, "action": "run"})
    assert resp.status_code == 202
    w = worker.Worker(config=config, spawner=spawner)
    w.tick()
    proc.returncode = 2
    w.tick()

    assert c.get("/api/queue").json() == []
    failed = c.get("/api/dashboard").json()["failed"]
    assert [f["video_id"] for f in failed] == [VIDEO_ID]
    assert "2" in failed[0]["reason"]
    assert "unrecognized arguments" in failed[0]["reason"]
    video = c.get(f"/api/videos/{VIDEO_ID}").json()
    assert video["status"] == "failed"


# --------------------------------------------------------------------------
# TASK-e522 : navigateur par compte (SPEC-9225 R1) : etat du profil, « Se connecter »,
# compte TikTok relie a la chaine. Aucun navigateur reel : clipper.browser est simule.
# --------------------------------------------------------------------------

BROWSER_ACCOUNT = "ab12cd"


def _browser_setup(tmp_path):
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "accounts.json").write_text(
        json.dumps({"accounts": [{"id": BROWSER_ACCOUNT, "label": "Compte exemple", "platform": "TikTok",
                                  "username": "", "notes": "", "has_password": False}]}), encoding="utf-8")


def local_client(tmp_path) -> TestClient:
    return TestClient(create_app(config=make_config(tmp_path)), base_url="http://127.0.0.1:8000",
                      client=("127.0.0.1", 50000))


def test_accounts_list_carries_the_browser_profile_state(tmp_path, isolated_cwd):
    _browser_setup(tmp_path)
    c = local_client(tmp_path)

    absent = c.get("/api/accounts").json()[0]
    assert absent["browser"] == {"present": False, "modified_at": None}

    profile = tmp_path / "state" / "browser" / BROWSER_ACCOUNT
    profile.mkdir(parents=True)
    (profile / "Local State").write_text("{}", encoding="utf-8")
    present = c.get("/api/accounts").json()[0]
    assert present["browser"]["present"] is True and present["browser"]["modified_at"]


def test_get_account_browser_state_and_404(tmp_path, isolated_cwd):
    _browser_setup(tmp_path)
    c = local_client(tmp_path)

    assert c.get(f"/api/accounts/{BROWSER_ACCOUNT}/browser").json() == {"present": False, "modified_at": None}
    assert c.get("/api/accounts/inconnu/browser").status_code == 404


def test_browser_login_route_opens_the_profile(tmp_path, isolated_cwd, monkeypatch):
    from clipper import browser

    _browser_setup(tmp_path)
    calls = []
    monkeypatch.setattr(browser, "start_login", lambda account, url=None, **kw: calls.append((account, url)))
    c = local_client(tmp_path)

    resp = c.post(f"/api/accounts/{BROWSER_ACCOUNT}/browser/login", json={})
    assert resp.status_code == 202 and resp.json()["account"] == BROWSER_ACCOUNT
    resp = c.post(f"/api/accounts/{BROWSER_ACCOUNT}/browser/login", json={"url": "https://www.youtube.com"})
    assert resp.status_code == 202

    assert calls == [(BROWSER_ACCOUNT, None), (BROWSER_ACCOUNT, "https://www.youtube.com")]


def test_browser_login_route_unknown_account_and_bad_body(tmp_path, isolated_cwd, monkeypatch):
    from clipper import browser

    _browser_setup(tmp_path)
    monkeypatch.setattr(browser, "start_login", lambda *a, **k: pytest.fail("ne doit pas s'ouvrir"))
    c = local_client(tmp_path)

    assert c.post("/api/accounts/inconnu/browser/login", json={}).status_code == 404
    assert c.post(f"/api/accounts/{BROWSER_ACCOUNT}/browser/login", json={"url": 3}).status_code == 422
    assert c.post(f"/api/accounts/{BROWSER_ACCOUNT}/browser/login", json={"autre": 1}).status_code == 422
    assert c.post(f"/api/accounts/{BROWSER_ACCOUNT}/browser/login", json=[]).status_code == 422


def test_browser_login_route_reports_browser_error_without_fallback(tmp_path, isolated_cwd, monkeypatch):
    from clipper import browser

    _browser_setup(tmp_path)

    def boom(*a, **k):
        raise browser.BrowserError("Chrome est introuvable : playwright install chrome")

    monkeypatch.setattr(browser, "start_login", boom)

    resp = local_client(tmp_path).post(f"/api/accounts/{BROWSER_ACCOUNT}/browser/login", json={})

    assert resp.status_code == 409
    assert "playwright install chrome" in resp.json()["detail"]


def test_browser_routes_keep_the_local_only_protections(tmp_path, isolated_cwd, monkeypatch):
    from clipper import browser

    _browser_setup(tmp_path)
    monkeypatch.setattr(browser, "start_login", lambda *a, **k: pytest.fail("ne doit pas s'ouvrir"))
    path = f"/api/accounts/{BROWSER_ACCOUNT}/browser/login"

    remote = TestClient(create_app(config=make_config(tmp_path)), base_url="http://127.0.0.1:8000",
                        client=("192.168.1.20", 50000))
    assert remote.post(path, json={}).status_code == 403
    assert remote.get(f"/api/accounts/{BROWSER_ACCOUNT}/browser").status_code == 403

    foreign = TestClient(create_app(config=make_config(tmp_path)), client=("127.0.0.1", 50000))
    assert foreign.post(path, json={}, headers={"host": "evil.example.com"}).status_code == 403

    assert local_client(tmp_path).post(path, content="{}", headers={"content-type": "text/plain"}).status_code == 415


def test_accounts_screen_has_the_browser_login_button_and_profile_state():
    js = (STATIC / "screens" / "accounts.js").read_text(encoding="utf-8")

    assert "Se connecter dans le navigateur" in js
    assert "/browser/login" in js and "data-acc-browser-login" in js
    assert "a.browser" in js and "Profil" in js and "absent" in js and "présent" in js
    assert "modified_at" in js  # date du profil


def test_channel_form_has_no_account_and_no_slots_field():
    js = (STATIC / "screens" / "channels.js").read_text(encoding="utf-8")

    for gone in ("tiktok_account", "chAccountEditor", "chSlotsEditor", "data-slot-add", "Aucun compte"):
        assert gone not in js, gone



def test_render_current_leaves_the_dom_alone_when_the_html_is_unchanged():
    js = _static("app.js")
    start = js.index("function renderCurrent()")
    block = js[start:js.index("\n}\n", start)]

    assert "guardBodyHtml(body)" in block                        # le HTML calculé est comparé au précédent
    guard = js[js.index("function guardBodyHtml"):]
    guard = guard[:guard.index("\n}\n")]
    assert "innerHTML" in guard and "===" in guard and "return;" in guard   # identique : le DOM n'est pas touché


def test_worker_event_only_refreshes_the_worker_light():
    js = _static("app.js")
    start = js.index("async function onServerEvent")
    block = js[start:js.index("\n}\n", start)]

    assert 'event.kind === "worker"' in block
    branch = block[block.index('event.kind === "worker"'):]
    branch = branch[:branch.index("return;") + len("return;")]
    assert "clipper:worker" in branch                            # événement dédié, pas « clipper:event »
    assert "renderCurrent" not in branch and "updateCounts" not in branch
    dash = _static("screens", "dashboard.js")
    assert 'addEventListener("clipper:worker"' in dash
    assert "/api/dashboard/worker" in dash
    assert 'data-section="worker"' in dash                       # seule la section du worker est mise à jour


def test_dashboard_worker_route_returns_only_the_worker_heartbeat(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/api/dashboard/worker")

    assert resp.status_code == 200
    assert set(resp.json()) <= {"worker", "worker_error"}
    assert "worker" in resp.json()


def test_every_thumbnail_reserves_its_size_so_loading_never_shifts_the_layout():
    import re

    for name in ("screens.js", "screens/clips.js", "screens/publish.js"):
        js = _static(*name.split("/"))
        thumbs = [m for m in re.findall(r"<img [^>]*thumbnail[^>]*>", js)]
        assert thumbs, name
        for tag in thumbs:
            assert "width=" in tag and "height=" in tag, f"{name} : {tag}"
    css = _static("style.css")
    for selector in (".job-thumb", ".mini-clip", ".clip-poster"):
        rule = re.search(rf"^{re.escape(selector)} \{{[^}}]*\}}|^{re.escape(selector)} \{{\n[^}}]*\}}", css, re.MULTILINE)
        assert rule and "aspect-ratio" in rule.group(0), selector


def test_new_channel_form_offers_the_standard_and_stream_gaming_models():
    js = _static("screens", "channels.js")

    assert "Standard" in js and "Stream gaming" in js
    assert 'name="model"' in js
    start = js.index("const CHAN_MODELS")
    models = js[start:js.index("];", start)]
    for needle in ('"builtin:gaming"', '"stream_auto"', '"split"', "rubric_path", "stream_variant", "layout"):
        assert needle in models, needle
    assert "CHAN_MODELS" in js[js.index("function chOpenNew"):]    # le modèle choisi part dans le preset


def test_post_channel_with_the_stream_gaming_model_writes_rubric_and_stream_layout(tmp_path, isolated_cwd):
    preset = {
        "channel": {},
        "moments": {"rubric_path": "builtin:gaming"},
        "reframe": {"layout": "stream_auto", "stream_variant": "split"},
    }

    _channels_setup(tmp_path)
    resp = client(tmp_path).post("/api/channels", json={"name": "stream", "preset": preset})

    assert resp.status_code == 201, resp.text
    saved = (tmp_path / "presets" / "stream.toml").read_text(encoding="utf-8")
    assert 'rubric_path = "builtin:gaming"' in saved
    assert 'layout = "stream_auto"' in saved and 'stream_variant = "split"' in saved
    effective = resp.json()["effective"]
    assert effective["moments"]["rubric_path"] == "builtin:gaming"
    assert effective["reframe"]["layout"] == "stream_auto"


def test_post_channel_with_the_standard_model_writes_nothing_more(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    resp = client(tmp_path).post("/api/channels", json={"name": "standard", "preset": {"channel": {}}})

    assert resp.status_code == 201, resp.text
    assert resp.json()["raw"] == {"channel": {}}


def test_channel_form_shows_the_rubric_at_the_top_of_the_channel_section():
    js = _static("screens", "channels.js")
    start = js.index("function chSectionHtml")
    block = js[start:js.index("\n}\n", start)]

    assert 'spec.section === "channel"' in block
    assert 'chField("moments", "rubric_path"' in block
    assert block.index('chField("moments", "rubric_path"') < block.index("main.map")   # avant les autres champs
    # un seul contrôle de grille : la section repliée ne la répète pas
    assert 'k !== "rubric_path"' in block or "rubric_path" in block[block.index("const main"):block.index("const rest")]


def _rubric_asset(name: str) -> bytes:
    from importlib import resources

    return resources.files("clipper").joinpath("assets", name).read_bytes()


def test_channel_rubric_label_for_a_file_identical_to_the_standard_grid(tmp_path, isolated_cwd):
    _channels_setup(tmp_path, preset=_CH_PRESET + '\n[moments]\nrubric_path = "ma_grille.toml"\n')
    (tmp_path / "ma_grille.toml").write_bytes(_rubric_asset("rubric.toml").replace(b"\n", b"\r\n"))

    rubric = client(tmp_path).get(f"/api/channels/{CH}").json()["rubric"]

    assert rubric["label"] == "Standard (ma_grille.toml)"
    assert rubric["kind"] == "standard"


def test_channel_rubric_label_for_a_file_identical_to_the_gaming_grid(tmp_path, isolated_cwd):
    _channels_setup(tmp_path, preset=_CH_PRESET + '\n[moments]\nrubric_path = "ma_grille.toml"\n')
    (tmp_path / "ma_grille.toml").write_bytes(_rubric_asset("rubric-gaming.toml"))

    rubric = client(tmp_path).get(f"/api/channels/{CH}").json()["rubric"]

    assert rubric["label"] == "Gaming (ma_grille.toml)"
    assert rubric["kind"] == "gaming"


def test_channel_rubric_label_default_value_and_builtin_values(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    (tmp_path / "rubric.toml").write_bytes(_rubric_asset("rubric.toml"))
    c = client(tmp_path)

    assert c.get(f"/api/channels/{CH}").json()["rubric"]["label"] == "Standard (rubric.toml)"
    assert c.get("/api/rubric-label", params={"path": "builtin"}).json()["label"] == "Standard (builtin)"
    assert c.get("/api/rubric-label", params={"path": "builtin:gaming"}).json()["label"] == "Gaming (builtin:gaming)"


def test_channel_rubric_label_stays_custom_for_a_different_or_missing_file(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    (tmp_path / "autre.toml").write_text("# ma grille à moi\n", encoding="utf-8")
    c = client(tmp_path)

    different = c.get("/api/rubric-label", params={"path": "autre.toml"}).json()
    missing = c.get("/api/rubric-label", params={"path": "absente.toml"}).json()
    unknown = c.get("/api/rubric-label", params={"path": "builtin:inconnue"}).json()

    assert different == {"value": "autre.toml", "kind": "custom", "label": "Fichier personnalisé (autre.toml)"}
    assert missing["kind"] == "custom" and "introuvable" in missing["label"]
    assert unknown["kind"] == "invalid" and "builtin:inconnue" in unknown["label"]


def test_channels_screen_labels_the_rubric_from_the_server():
    js = _static("screens", "channels.js")

    assert "/api/rubric-label" in js
    assert "detail.rubric" in js or "ed.detail.rubric" in js


_TECH_REF = None


def _all_help(tmp_path) -> dict[str, dict]:
    from clipper.web import app as web_app

    docs = {}
    for section in web_app._CHANNEL_FORM_SECTIONS:
        for key, doc in web_app._defaults_documentation(section).items():
            docs[f"channel/{section}.{key}"] = doc
    c = sclient(tmp_path)
    for section, keys in c.get("/api/settings").json()["defaults"].items():
        for key, doc in keys.items():
            docs[f"settings/{section}.{key}"] = doc
    for key, doc in c.get(f"/api/channels/{CH}").json()["defaults"]["moments"].items() if False else []:
        docs[key] = doc
    return docs


def test_help_main_text_is_a_simple_sentence_without_technical_references(tmp_path, isolated_cwd):
    import re

    docs = _all_help(tmp_path)
    assert any(d["comment"] for d in docs.values())
    offenders = []
    for where, doc in docs.items():
        text = doc["comment"]
        if re.search(r"SPEC-|ADR-|TASK-|\b[a-z][a-z0-9]*_[a-z0-9_]+\(", text):
            offenders.append(f"{where} : {text}")
        if text and text.count(". ") + text.count(" ? ") > 0 and len(re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Ý«\"])", text)) > 1:
            offenders.append(f"{where} : plusieurs phrases dans le texte principal : {text}")
    assert offenders == []


def test_help_details_keep_the_rest_of_the_comment_for_a_folded_block(tmp_path, isolated_cwd):
    docs = _all_help(tmp_path)

    assert all("details" in d for d in docs.values())
    assert any(d["details"] for d in docs.values())
    rubric = docs["channel/moments.rubric_path"]
    assert "builtin:gaming" in rubric["comment"]           # l'aide simple dit déjà quoi saisir
    assert rubric["comment"].count(".") <= 2
    js = _static("screens", "channels.js") + _static("screens", "settings.js")
    assert js.count("<details class=\"chan-help\"") + js.count("<details class=\"set-help\"") >= 2


# --------------------------------------------------------------------------
# TASK-0b78 : statut de chaque publication TikTok dans la console (SPEC-9225 R3, R4)
# --------------------------------------------------------------------------

_CAPTURE = "state/browser/ab12cd/captures/20261005T100000-captcha.png"
_POST = "https://example.invalid/@ma_chaine/video/7300000000000000001"


def _tiktok_entries():
    return [
        _entry("01", "scheduled", slot_at=PUB_THU, postponed_reason="plafond de 1 publication(s) par jour atteint le 2026-10-05"),
        _entry("02", "published", slot_at=PUB_MON, published_at="2026-10-05T18:40:00+02:00", tiktok_state="scheduled_on_tiktok",
               tiktok_publish_at=PUB_MON, post_url=None, post_id=None, post_note="post programmé : son adresse publique n'existe pas encore"),
        _entry("03", "published", slot_at=PUB_MON, published_at="2026-10-05T18:40:00+02:00", tiktok_state="published",
               tiktok_publish_at="2026-10-05T18:40:00+02:00", post_url=_POST, post_id="7300000000000000001"),
        _entry("04", "failed", slot_at="2026-10-06T10:00:00+02:00", error="captcha détecté", capture=_CAPTURE, halted=True),
        _entry("05", "approved"),
    ]


def test_get_publish_gives_each_publication_its_tiktok_status_link_and_capture(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, _tiktok_entries())

    data = _get_publish(tmp_path).json()
    clips = {c["clip_id"]: c for c in [*data["unscheduled"], *data["done"], *(s["clip"] for s in data["slots"] if s["clip"])]}

    assert clips["01"]["tiktok_status"] == "pending"
    assert "plafond" in clips["01"]["postponed_reason"]
    assert clips["02"]["tiktok_status"] == "scheduled_on_tiktok"
    assert clips["03"]["tiktok_status"] == "published" and clips["03"]["post_url"] == _POST
    assert clips["04"]["tiktok_status"] == "failed" and clips["04"]["publish_error"] == "captcha détecté"
    assert clips["04"]["capture_url"] == f"/api/publish/{CLIPS_VIDEO}/04/capture"
    assert clips["03"]["capture_url"] is None
    assert clips["05"]["tiktok_status"] == "pending"


def test_capture_is_served_from_the_browser_captures_folder_only(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [
        _entry("04", "failed", error="captcha", capture=_CAPTURE),
        _entry("05", "failed", error="x", capture="../../../etc/passwd"),
        _entry("06", "failed", error="x", capture="state/accounts.json"),
        _entry("01", "failed", error="x"),
    ])
    shot = tmp_path / _CAPTURE
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG fake")
    c = client(tmp_path)

    ok = c.get(f"/api/publish/{CLIPS_VIDEO}/04/capture")
    assert ok.status_code == 200 and ok.content == b"\x89PNG fake" and ok.headers["content-type"] == "image/png"
    assert c.get(f"/api/publish/{CLIPS_VIDEO}/05/capture").status_code == 404   # hors de state/browser/*/captures
    assert c.get(f"/api/publish/{CLIPS_VIDEO}/06/capture").status_code == 404
    assert c.get(f"/api/publish/{CLIPS_VIDEO}/01/capture").status_code == 404   # pas de capture
    assert c.get(f"/api/publish/{CLIPS_VIDEO}/..%2Fx/capture").status_code in (400, 404)


def test_retry_puts_a_failed_publication_back_in_the_queue(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("04", "failed", slot_at=PUB_THU, error="captcha", capture=_CAPTURE, halted=True)])

    resp = client(tmp_path).post(f"/api/publish/{CLIPS_VIDEO}/04/retry")

    assert resp.status_code == 200
    assert resp.json()["status"] == "scheduled" and resp.json()["error"] is None
    saved = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert saved[0]["status"] == "scheduled" and saved[0]["halted"] is False


def test_retry_of_an_entry_that_did_not_fail_is_a_409(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_THU)])
    resp = client(tmp_path).post(f"/api/publish/{CLIPS_VIDEO}/01/retry")
    assert resp.status_code == 409 and "failed" in resp.json()["detail"]


def test_publish_mode_of_one_entry_can_be_set(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_THU)])
    c = client(tmp_path)

    ok = c.post(f"/api/publish/{CLIPS_VIDEO}/01/mode", json={"mode": "scheduled"})
    bad = c.post(f"/api/publish/{CLIPS_VIDEO}/01/mode", json={"mode": "demain"})

    assert ok.status_code == 200 and ok.json()["publish_mode"] == "scheduled"
    assert bad.status_code == 409 and "mode" in bad.json()["detail"]


def test_tiktok_events_are_listed_since_a_date(tmp_path, isolated_cwd):
    from clipper import tiktok

    tiktok.emit_event({"level": "error", "reason": "captcha détecté"}, now=datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))
    tiktok.emit_event({"level": "info", "reason": "publiée"}, now=datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc))
    c = client(tmp_path)

    assert [e["reason"] for e in c.get("/api/tiktok/events").json()] == ["captcha détecté", "publiée"]
    assert [e["reason"] for e in c.get("/api/tiktok/events", params={"since": "2026-10-05T10:00:00+00:00"}).json()] == ["publiée"]


def test_corrupt_tiktok_events_file_is_a_500_in_french(tmp_path, isolated_cwd):
    path = tmp_path / "state" / "tiktok" / "events.json"
    path.parent.mkdir(parents=True)
    path.write_text("{pas du json", encoding="utf-8")
    resp = client(tmp_path).get("/api/tiktok/events")
    assert resp.status_code == 500 and "événements" in resp.json()["detail"]


def test_console_shows_tiktok_status_retry_button_and_notifications():
    static = Path(__file__).resolve().parent.parent / "clipper" / "web" / "static"
    publish_js = (static / "screens" / "publish.js").read_text(encoding="utf-8")
    app_js = (static / "app.js").read_text(encoding="utf-8")
    for label in ("En attente", "Programmée sur TikTok", "Publiée", "Échec", "Réessayer", "/retry", "capture_url", "post_url"):
        assert label in publish_js
    assert "/api/tiktok/events" in app_js and 'event.kind === "tiktok"' in app_js


# --------------------------------------------------------------------------
# Statistiques TikTok par compte, a partir du releve de TikTok Studio (SPEC-86fe)
# --------------------------------------------------------------------------

from clipper import browser as browser_mod  # noqa: E402
from clipper import tiktok as tiktok_mod  # noqa: E402

TT_ACCOUNT, TT_OTHER = "ab12cd", "ef34ab"
TT_ID_A, TT_ID_B, TT_ID_C = "7300000000000000001", "7300000000000000002", "7300000000000000003"
_TT_TILES = ("views", "profile_views", "likes", "comments", "shares")


def _tt_accounts(tmp_path, *, ready=(TT_ACCOUNT,), channel=True):
    """Deux comptes TikTok (ecran Comptes), ``ready`` ceux « prets a publier » ; le premier est lie a la chaine ma_chaine."""
    rows = [{"id": TT_ACCOUNT, "label": "Compte exemple", "platform": "TikTok"},
            {"id": TT_OTHER, "label": "Autre compte", "platform": "TikTok"}]
    for row in rows:
        row["ready_to_publish"] = row["id"] in ready
        row["login"] = {"state": "connected" if row["id"] in ready else "expired",
                        "checked_at": "2026-10-01T10:00:00+00:00", "expires_at": None}
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "accounts.json").write_text(json.dumps({"accounts": rows}), encoding="utf-8")
    if channel:
        _channels_setup(tmp_path, f'[channel]\ndisplay_name = "Ma chaîne"\ntiktok_account = "{TT_ACCOUNT}"\n')


def _tt_snapshot(tmp_path, day, *, account=TT_ACCOUNT, views=100, posts=(), origin="full", hour=12):
    """Un releve de l'historique (state/stats/tiktok/<compte>/) du ``day`` octobre 2026 ; ``views`` : tuile « vues » 7 jours."""
    stamp = f"2026-10-{day:02d}T{hour:02d}:00:00+00:00"
    overview = None if origin != "full" else {
        str(n): {key: {"value": views * (n // 7) + i, "change_pct": 4.5 if key == "views" else None}
                 for i, key in enumerate(_TT_TILES)} for n in (7, 28, 60, 365)}
    _write_json(tmp_path / "state" / "stats" / "tiktok" / account / f"202610{day:02d}T{hour:02d}0000000000Z.json",
                {"account": account, "fetched_at": stamp, "source": "tiktok_studio", "origin": origin,
                 "overview": overview, "posts": list(posts)})


def _tt_post(post_id, caption, **fields):
    return {"post_id": post_id, "post_url": f"https://example.invalid/@ma_chaine/video/{post_id}", "caption": caption,
            "posted_at": "2026-09-30T14:05:00", "posted_at_text": "2026-09-30 14:05", "visibility": "public",
            "views": 1200, "likes": 85, "comments": 7, "shares": 12, "avg_watch_s": 12.0, "watched_full": 0.23,
            "retention_curve": [{"t_s": 0.0, "share": 1.0}, {"t_s": 5.0, "share": 0.5}],
            "viewers": {"total": 1200, "types": [{"label": "Nouveaux", "value": 0.6}], "age": None, "gender": None,
                        "locations": None},
            "engagement": {"shares": 12, "likes_over_time": None, "comment_words": [{"label": "génial", "value": 5}]},
            **fields}


def _tt_get(tmp_path, path, **params):
    return client(tmp_path).get(f"/api/stats/tiktok{path}", params=params)


def test_the_stats_accounts_list_says_which_account_is_ready_and_what_the_last_fetch_was(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path, ready=(TT_ACCOUNT,))
    _tt_snapshot(tmp_path, 1, posts=[_tt_post(TT_ID_A, "Un")])
    _tt_snapshot(tmp_path, 2, posts=[_tt_post(TT_ID_A, "Un")], origin="opportunistic", hour=9)

    resp = _tt_get(tmp_path, "")

    assert resp.status_code == 200, resp.text
    accounts = {a["account"]: a for a in resp.json()["accounts"]}
    ready, other = accounts[TT_ACCOUNT], accounts[TT_OTHER]
    assert ready["ready"] is True and ready["not_ready_reason"] is None and ready["label"] == "Compte exemple"
    assert "channel" not in ready  # plus de style lié à un compte (SPEC-6076 R2)
    assert ready["snapshots"] == 2 and ready["fetched_at"] == "2026-10-02T09:00:00+00:00"
    assert ready["last_full_at"] == "2026-10-01T12:00:00+00:00" and ready["error"] is None
    assert other["ready"] is False and "expirée" in other["not_ready_reason"]  # le compte non pret dit pourquoi
    assert other["snapshots"] == 0 and other["fetched_at"] is None


def test_the_stats_accounts_list_carries_the_last_safe_stop(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    _tt_snapshot(tmp_path, 1)
    error = {"at": "2026-10-02T08:00:00+00:00", "code": "captcha", "reason": "captcha détecté", "capture": None}
    _write_json(tmp_path / "state" / "stats" / "tiktok" / TT_ACCOUNT / "20261002T080000000000Z.error.json", error)

    account = next(a for a in _tt_get(tmp_path, "").json()["accounts"] if a["account"] == TT_ACCOUNT)

    assert account["error"] == error and account["last_full_at"] == "2026-10-01T12:00:00+00:00"


def test_the_overview_gives_five_tiles_with_evolution_and_the_daily_curves(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    _tt_snapshot(tmp_path, 1, views=100)
    _tt_snapshot(tmp_path, 8, views=130)

    data = _tt_get(tmp_path, f"/{TT_ACCOUNT}", period=7).json()

    assert data["account"] == TT_ACCOUNT and "channel" not in data and data["ready"] is True
    assert data["period"] == 7 and data["last_full_at"] == "2026-10-08T12:00:00+00:00"
    assert list(data["tiles"]) == list(_TT_TILES)
    assert data["tiles"]["views"] == {"value": 130, "change_pct": 4.5, "history_change_pct": 30.0}
    series = data["series"]["views"]
    assert series["labels"][0] == "2026-10-02" and series["labels"][-1] == "2026-10-08"
    assert series["values"][-1] == 130 and series["values"].count(None) == 6
    assert series["previous"] == [None] * 6 + [100]  # le 8/10 se compare au releve du 1/10 : la periode d'avant, decalee de 7 jours


def test_the_overview_defaults_to_28_days_and_refuses_another_period(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    _tt_snapshot(tmp_path, 1)

    assert _tt_get(tmp_path, f"/{TT_ACCOUNT}").json()["period"] == 28
    bad = _tt_get(tmp_path, f"/{TT_ACCOUNT}", period=14)
    assert bad.status_code == 422 and "période" in bad.json()["detail"]
    assert _tt_get(tmp_path, f"/{TT_ACCOUNT}", period="x").status_code == 422


def test_the_overview_of_an_account_never_fetched_is_an_explicit_empty_state(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)

    data = _tt_get(tmp_path, f"/{TT_OTHER}").json()

    assert data["tiles"] is None and data["series"] is None and data["snapshots"] == 0 and data["fetched_at"] is None
    assert data["ready"] is False and data["not_ready_reason"]


def test_an_unknown_account_is_a_404_on_every_stats_route(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    for path in ("/inconnu", "/inconnu/videos", f"/inconnu/videos/{TT_ID_A}"):
        assert _tt_get(tmp_path, path).status_code == 404


def test_the_video_list_comes_from_the_report_sorted_searched_and_linked_to_clips(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    _tt_snapshot(tmp_path, 1, posts=[
        _tt_post(TT_ID_A, "Clip Clipper", views=500, posted_at="2026-09-29T10:00:00"),
        _tt_post(TT_ID_B, "Publié à la main", views=900, posted_at="2026-09-30T10:00:00"),
        _tt_post(TT_ID_C, "Encore en traitement", views=None, posted_at="2026-10-01T10:00:00")])
    _write_sidecar(tmp_path, "aaaaaaaaaaa", "01", tiktok_post={"url": None, "id": TT_ID_A, "state": "published",
                                                                "account": TT_ACCOUNT})

    data = _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos").json()

    assert [v["post_id"] for v in data["videos"]] == [TT_ID_C, TT_ID_B, TT_ID_A]  # date, la plus recente en premier
    by_id = {v["post_id"]: v for v in data["videos"]}
    assert by_id[TT_ID_A]["clip"] == {"video_id": "aaaaaaaaaaa", "clip_id": "01"} and by_id[TT_ID_A]["outside_clipper"] is False
    assert by_id[TT_ID_B]["clip"] is None and by_id[TT_ID_B]["outside_clipper"] is True  # « publié hors Clipper »
    assert by_id[TT_ID_C]["views"] is None and by_id[TT_ID_C]["processing"] is True
    assert data["account"] == TT_ACCOUNT and "channel" not in data
    ids = lambda **params: [v["post_id"] for v in _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos", **params).json()["videos"]]
    assert ids(sort="views", dir="desc") == [TT_ID_B, TT_ID_A, TT_ID_C]  # sans valeur : en dernier
    assert ids(sort="views", dir="asc") == [TT_ID_A, TT_ID_B, TT_ID_C]
    assert ids(q="main") == [TT_ID_B] and ids(q="TRAITEMENT") == [TT_ID_C] and ids(q="zzz") == []


def test_the_video_list_refuses_an_unknown_sort_or_direction(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    assert _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos", sort="couleur").status_code == 422
    assert _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos", dir="haut").status_code == 422


def test_the_video_sheet_gives_figures_tabs_data_links_and_history(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    _tt_snapshot(tmp_path, 1, posts=[_tt_post(TT_ID_A, "Clip Clipper", views=10)])
    _tt_snapshot(tmp_path, 2, posts=[_tt_post(TT_ID_A, "Clip Clipper", views=25)])
    _write_sidecar(tmp_path, "aaaaaaaaaaa", "01", tiktok_post={"url": None, "id": TT_ID_A, "state": "published",
                                                                "account": TT_ACCOUNT})

    resp = _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos/{TT_ID_A}")

    assert resp.status_code == 200, resp.text
    video = resp.json()["video"]
    assert video["post_id"] == TT_ID_A and video["views"] == 25 and video["post_url"].endswith(TT_ID_A)
    assert video["clip"] == {"video_id": "aaaaaaaaaaa", "clip_id": "01"} and video["outside_clipper"] is False
    assert video["viewers"]["types"] == [{"label": "Nouveaux", "value": 0.6}] and video["viewers"]["age"] is None
    assert video["engagement"]["comment_words"] == [{"label": "génial", "value": 5}]
    assert video["retention_curve"][1] == {"t_s": 5.0, "share": 0.5}
    assert [h["views"] for h in video["history"]] == [10, 25]
    assert _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos/999").status_code == 404


def test_a_post_published_outside_clipper_has_a_sheet_marked_as_such(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    _tt_snapshot(tmp_path, 1, posts=[_tt_post(TT_ID_B, "À la main")])

    video = _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos/{TT_ID_B}").json()["video"]

    assert video["clip"] is None and video["outside_clipper"] is True


def test_a_corrupt_history_file_is_an_explicit_500_not_an_empty_screen(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    path = tmp_path / "state" / "stats" / "tiktok" / TT_ACCOUNT / "20261001T120000000000Z.json"
    path.parent.mkdir(parents=True)
    path.write_text("{pas du json", encoding="utf-8")

    for route in ("", f"/{TT_ACCOUNT}", f"/{TT_ACCOUNT}/videos"):
        resp = _tt_get(tmp_path, route)
        assert resp.status_code == 500 and "illisible" in resp.json()["detail"]


def test_the_csv_import_is_gone(tmp_path, isolated_cwd):
    resp = client(tmp_path).post("/api/stats/import", files={"file": ("stats.csv", b"clip_id,views\n01,10\n", "text/csv")})
    assert resp.status_code in (404, 405)


class FakeFetch:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, account, *, config=None, **kwargs):
        self.calls.append(account)
        if self.error is not None:
            raise self.error
        return {"account": account, "fetched_at": "2026-09-27T08:00:00+00:00", "posts": [{}, {}], "overview": {}}


def test_refresh_route_fetches_the_given_ready_account(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path)
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)

    resp = client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT})

    assert resp.status_code == 200, resp.text
    assert fetch.calls == [TT_ACCOUNT]
    assert resp.json() == {"accounts": {TT_ACCOUNT: {"fetched_at": "2026-09-27T08:00:00+00:00", "posts": 2}}}


def test_refresh_route_does_not_fetch_an_account_that_is_not_ready_and_says_why(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path, ready=(TT_ACCOUNT,))
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)

    resp = client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": TT_OTHER})

    assert resp.status_code == 409 and fetch.calls == []
    assert "non prêt" in resp.json()["detail"] and "expirée" in resp.json()["detail"]
    assert client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": "inconnu"}).status_code == 404


def test_refresh_route_without_body_fetches_every_ready_account_only(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path, ready=(TT_ACCOUNT, TT_OTHER))
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)
    assert client(tmp_path).post("/api/stats/tiktok/refresh").status_code == 200
    assert fetch.calls == [TT_ACCOUNT, TT_OTHER]

    _tt_accounts(tmp_path, ready=(TT_OTHER,))
    fetch.calls.clear()
    client(tmp_path).post("/api/stats/tiktok/refresh")
    assert fetch.calls == [TT_OTHER]


def test_refresh_route_with_no_ready_account_says_so(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path, ready=())
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)

    resp = client(tmp_path).post("/api/stats/tiktok/refresh")

    assert resp.status_code == 409 and "prêt" in resp.json()["detail"] and fetch.calls == []


@pytest.mark.parametrize("error,status,words", [
    (tiktok_mod.TikTokStop("captcha", "captcha détecté : arrêt immédiat", None), 409, "captcha"),
    (browser_mod.BrowserError("Chrome introuvable"), 409, "Chrome"),
    (tiktok_mod.TikTokError("réglage invalide"), 422, "réglage"),
])
def test_refresh_route_reports_a_safe_stop_or_error_in_french(tmp_path, isolated_cwd, monkeypatch, error, status, words):
    _tt_accounts(tmp_path)
    monkeypatch.setattr(tiktok_mod, "fetch_stats", FakeFetch(error=error))

    resp = client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT})

    assert resp.status_code == status and words in resp.json()["detail"]


def test_refresh_route_refuses_an_invalid_body_or_account(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path)
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)

    assert client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": "../x"}).status_code == 422
    assert client(tmp_path).post("/api/stats/tiktok/refresh", json={"autre": 1}).status_code == 422
    assert fetch.calls == []



def test_refresh_route_runs_a_full_fetch_without_cap_only_when_asked(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path)
    seen = []

    def fetch(account, *, config=None, **kwargs):
        seen.append(kwargs)
        return {"account": account, "fetched_at": "2026-09-27T08:00:00+00:00", "posts": [{}], "overview": {}}

    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)

    assert client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT}).status_code == 200
    assert client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT, "full": True}).status_code == 200
    assert client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT, "full": False}).status_code == 200

    assert seen == [{}, {"full": True}, {}]  # le releve normal garde le plafond


def test_refresh_route_refuses_a_full_flag_that_is_not_a_boolean(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path)
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)

    resp = client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT, "full": "oui"})

    assert resp.status_code == 422 and "full" in resp.json()["detail"] and fetch.calls == []


def test_the_stats_accounts_list_gives_the_page_count_of_a_full_fetch(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path, ready=(TT_ACCOUNT,))
    _tt_snapshot(tmp_path, 1, posts=[_tt_post(TT_ID_A, "Un"), _tt_post(TT_ID_B, "Deux")])

    accounts = {a["account"]: a for a in _tt_get(tmp_path, "").json()["accounts"]}

    # 2 pages d'ensemble (analyse du compte, liste des Publications) + 3 par post connu (analyse, spectateurs, engagement)
    assert accounts[TT_ACCOUNT]["known_posts"] == 2 and accounts[TT_ACCOUNT]["full_pages"] == 8
    assert accounts[TT_OTHER]["known_posts"] == 0 and accounts[TT_OTHER]["full_pages"] is None  # jamais releve : inconnu


def test_the_stats_overview_accepts_365_days(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    _tt_snapshot(tmp_path, 1, posts=[_tt_post(TT_ID_A, "Un")])

    data = _tt_get(tmp_path, f"/{TT_ACCOUNT}", period=365).json()

    assert data["period"] == 365 and len(data["series"]["views"]["labels"]) == 365


def test_the_stats_screen_has_a_full_fetch_button_with_a_confirmation_saying_the_page_count():
    js = _static("screens", "stats.js")
    assert "data-stats-full" in js and "Relevé complet" in js and "confirmDialog" in js
    assert "full: true" in js
    said = _stats_js_run(["statsFullConfirmBody"], 'statsFullConfirmBody({full_pages: 1234, known_posts: 410})')
    assert "1 234" in said.replace(" ", " ").replace(" ", " ") and "pages" in said
    unknown = _stats_js_run(["statsFullConfirmBody"], 'statsFullConfirmBody({full_pages: null, known_posts: 0})')
    assert "pages" in unknown and "inconnu" in unknown.lower()


# --------------------------------------------------------------------------
# SPEC-00d1 R4, R5 : compte choisi par publication, calendrier, validation
# --------------------------------------------------------------------------


def _accounts_state(tmp_path, *, ready=(READY,)):
    rows = [{"id": READY, "label": "Compte exemple", "platform": "TikTok"},
            {"id": SPARE, "label": "Autre compte", "platform": "TikTok"}]
    for row in rows:
        row["ready_to_publish"] = row["id"] in ready
        row["slots"] = [{"day": "mon", "time": "18:30"}, {"day": "thu", "time": "12:00"}]  # creneaux du compte (SPEC-6076 R2)
        row["timezone"] = "Europe/Paris"
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "accounts.json").write_text(json.dumps({"accounts": rows}), encoding="utf-8")


def test_publish_accounts_lists_every_account_with_its_flag_and_no_default(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    _accounts_state(tmp_path, ready=(READY,))

    data = client(tmp_path).get("/api/publish/accounts").json()

    assert "default" not in data  # un style n'a plus de compte par defaut (SPEC-6076 R2) : chaque publication choisit
    assert data["accounts"] == [
        {"id": READY, "label": "Compte exemple", "ready_to_publish": True, "paused_at": None, "service": "tiktok",
         "service_label": "TikTok"},
        {"id": SPARE, "label": "Autre compte", "ready_to_publish": False, "paused_at": None, "service": "tiktok",
         "service_label": "TikTok"}]


def test_publish_accounts_never_carry_a_secret_and_work_from_a_remote_console(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    _accounts_state(tmp_path)
    remote = TestClient(create_app(config=make_config(tmp_path)), base_url="http://exemple.invalid", client=("203.0.113.5", 1))

    resp = remote.get("/api/publish/accounts", params={"channel": "ma_chaine"})

    assert resp.status_code == 200
    assert set(resp.json()["accounts"][0]) == {"id", "label", "ready_to_publish", "paused_at", "service", "service_label"}


def test_approve_with_a_ready_account_records_it_in_the_publication_entry(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    _accounts_state(tmp_path, ready=(READY, SPARE))

    resp = client(tmp_path).post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": SPARE})

    assert resp.status_code == 200 and resp.json()["account"] == SPARE
    entry = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))[0]
    assert entry["account"] == SPARE


def test_approve_without_an_account_is_a_409_and_approves_nothing(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    _accounts_state(tmp_path, ready=(READY,))

    for kwargs in ({}, {"json": {}}, {"json": {"account": None}}):
        resp = client(tmp_path).post(f"/api/clips/{CLIPS_VIDEO}/01/approve", **kwargs)
        assert resp.status_code == 409 and "compte de publication manquant" in resp.json()["detail"]
    assert json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8")) == []


def test_approve_schedules_on_the_next_slot_of_the_chosen_account(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    _accounts_state(tmp_path, ready=(READY, SPARE))
    rows = json.loads((tmp_path / "state" / "accounts.json").read_text(encoding="utf-8"))
    rows["accounts"][1]["slots"] = [{"day": "wed", "time": "07:00"}]  # SPARE : un seul creneau, le mercredi
    (tmp_path / "state" / "accounts.json").write_text(json.dumps(rows), encoding="utf-8")

    resp = client(tmp_path).post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": SPARE})

    assert resp.status_code == 200 and resp.json()["account"] == SPARE
    slot = datetime.fromisoformat(resp.json()["slot_at"]).astimezone(ZoneInfo("Europe/Paris"))
    assert (slot.strftime("%a %H:%M")) == "Wed 07:00"


def test_approve_refuses_an_account_that_is_not_ready_or_unknown(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    _accounts_state(tmp_path, ready=(READY,))
    c = client(tmp_path)

    not_ready = c.post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": SPARE})
    unknown = c.post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": "fantome"})

    assert not_ready.status_code == 409 and "non prêt à publier" in not_ready.json()["detail"]
    assert unknown.status_code == 409 and "compte inconnu" in unknown.json()["detail"]
    assert json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8")) == []  # rien d'approuvé


def test_publish_account_route_changes_the_account_among_the_ready_ones(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_THU, account=READY)])
    _accounts_state(tmp_path, ready=(READY, SPARE))
    c = client(tmp_path)

    resp = c.post(f"/api/publish/{CLIPS_VIDEO}/01/account", json={"account": SPARE})

    assert resp.status_code == 200 and resp.json()["account"] == SPARE
    moved = _get_publish(tmp_path, account=SPARE).json()                     # le post suit son nouveau compte
    assert [s["clip"]["account"] for s in moved["slots"] if s["clip"]] == [SPARE]  # sur un creneau du nouveau compte
    assert moved["off_slot"] == []
    assert _get_publish(tmp_path).json()["slots"][1]["clip"] is None


def test_publish_account_route_refuses_a_not_ready_account_a_published_entry_and_a_bad_body(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_THU, account=READY),
                              _entry("02", "published", published_at="2026-10-01T10:00:00+00:00", account=READY)])
    _accounts_state(tmp_path, ready=(READY,))
    c = client(tmp_path)

    refused = c.post(f"/api/publish/{CLIPS_VIDEO}/01/account", json={"account": SPARE})
    published = c.post(f"/api/publish/{CLIPS_VIDEO}/02/account", json={"account": READY})
    missing = c.post(f"/api/publish/{CLIPS_VIDEO}/01/account", json={})

    assert refused.status_code == 409 and "non prêt à publier" in refused.json()["detail"]
    assert published.status_code == 409 and "changement de compte refusé" in published.json()["detail"]
    assert missing.status_code == 409  # aucun compte donné : refusé, jamais « aucun compte »
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert entries[0]["account"] == READY


def test_the_publication_calendar_shows_the_account_of_each_post_and_why_it_waits(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [
        _entry("01", "scheduled", slot_at=PUB_THU, account=SPARE,
               waiting_reason="compte Autre compte non prêt à publier : coche « prêt à publier »"),
        _entry("02", "approved", account=READY)])
    _accounts_state(tmp_path, ready=(READY,))

    data = _get_publish(tmp_path).json()
    spare = _get_publish(tmp_path, account=SPARE).json()

    assert data["unscheduled"][0]["account"] == READY and data["unscheduled"][0]["waiting_reason"] is None
    assert data["account"] == READY and "channel" not in data
    assert [a["id"] for a in data["accounts"]] == [READY, SPARE]  # libellés et état, pour l'affichage
    waiting = next(s["clip"] for s in spare["slots"] if s["clip"])  # le post est sur un créneau du compte SPARE
    assert waiting["account"] == SPARE and "non prêt à publier" in waiting["waiting_reason"]


def test_the_clips_list_carries_the_account_of_the_entry(tmp_path, isolated_cwd):
    _clips_setup(tmp_path)
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "scheduled", slot_at="2026-10-02T18:00:00+00:00", account=SPARE)])

    clips = {c["clip_id"]: c for c in client(tmp_path).get("/api/clips", params={"video_id": CLIPS_VIDEO}).json()}

    assert clips["01"]["account"] == SPARE and clips["03"]["account"] is None


def test_clips_drawer_picks_the_publication_account_among_the_ready_ones():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")

    assert "/api/publish/accounts" in js and "clip-account" in js and "Compte de publication" in js
    assert "ready_to_publish" in js                                      # seuls les comptes prêts sont proposés
    assert "out.default" not in js and "Compte du style" not in js       # aucun compte de style (SPEC-6076 R2)
    assert "Choisis un compte" in js                                     # choix obligatoire, jamais prérempli
    assert 'jsonBody("POST", { account: chosen })' in js                  # le choix part avec l'approbation


def test_publish_screen_shows_and_changes_the_account_of_each_post():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")

    assert "pub-acct" in js and "pubAccountLabel" in js                   # compte affiché sur chaque post
    assert "/account" in js and "data-pub-account" in js and "Compte de publication" in js
    assert "waiting_reason" in js and "En attente" in js                  # raison visible quand l'entrée n'est pas tentée
    assert "ready_to_publish" in js                                       # seuls les comptes prêts sont choisissables


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_every_static_javascript_file_parses():
    # Une fusion automatique a déjà laissé un JS tronqué (écran Chaînes vide) sans qu'aucun test ne le voie.
    files = sorted(STATIC.rglob("*.js"))
    assert files
    bad = []
    for f in files:
        run = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, encoding="utf-8")
        if run.returncode != 0:
            bad.append(f"{f.name}: {run.stderr.strip().splitlines()[-1] if run.stderr.strip() else run.returncode}")
    assert not bad, bad


# --------------------------------------------------------------------------
# SPEC-1ed3 : publication pilotee depuis l'ecran Publication (API + ecran)
# --------------------------------------------------------------------------

from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402


def _pub_client(tmp_path, **tiktok_settings) -> TestClient:
    config = Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                    _sections={"tiktok": {"max_posts_per_day": 5, "min_gap_minutes": 0, **tiktok_settings}})
    return TestClient(create_app(config=config))


def _publications_setup(tmp_path, *, ready=(READY, SPARE), slots=False):
    _publish_setup(tmp_path, slots=slots)
    _accounts_state(tmp_path, ready=ready)


def _soon(**kw) -> str:
    return (_dt.now(_tz.utc) + _td(**kw)).replace(microsecond=0).isoformat()


def _publications(tmp_path, c=None):
    return (c or _pub_client(tmp_path)).get("/api/publications").json()["publications"]


def test_creating_a_post_now_approves_implicitly_and_records_account_mode_and_settings(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    options = {"visibility": "friends", "allow_comments": False, "allow_reuse": True, "ai_generated": True,
               "content_check": "wait"}

    resp = _pub_client(tmp_path).post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": SPARE, "mode": "immediate", "options": options})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["publish_status"] == "scheduled" and body["account"] == SPARE and body["publish_mode"] == "immediate"
    entry = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))[0]
    assert entry["post_options"] == options and entry["slot_at"] is not None  # due tout de suite, sans creneau de chaine
    clips = {c["clip_id"]: c for c in client(tmp_path).get("/api/clips", params={"video_id": CLIPS_VIDEO}).json()}
    assert clips["01"]["publish_status"] == "scheduled"  # plus « a valider » : approuve par le formulaire


def test_creating_a_scheduled_post_keeps_the_date(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    when = _soon(days=3)

    resp = _pub_client(tmp_path).post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "scheduled", "publish_at": when})

    assert resp.status_code == 201, resp.text
    assert resp.json()["slot_at"] == when and resp.json()["publish_mode"] == "scheduled"


def test_a_video_without_channel_is_publishable_through_the_form(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    _write_state(tmp_path, "nochannel001")
    _write_clip(tmp_path, "nochannel001", {**_clip_sidecar("01"), "video_id": "nochannel001"})

    resp = _pub_client(tmp_path).post("/api/publications", json={
        "video_id": "nochannel001", "clip_id": "01", "account": READY, "mode": "immediate"})

    assert resp.status_code == 201, resp.text
    assert (tmp_path / "state" / "publish" / "_sans_chaine.json").is_file()
    clips = {c["video_id"]: c for c in client(tmp_path).get("/api/clips", params={"video_id": "nochannel001"}).json()}
    assert clips["nochannel001"]["publish_status"] == "scheduled" and clips["nochannel001"]["account"] == READY


def test_private_and_scheduled_is_refused_in_french(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)

    resp = _pub_client(tmp_path).post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "scheduled", "publish_at": _soon(days=3),
        "options": {"visibility": "private"}})

    assert resp.status_code == 409 and "privée" in resp.json()["detail"]
    assert _publications(tmp_path) == []


def test_a_daily_cap_overrun_is_refused_with_the_reason_and_the_next_possible_time(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    c = _pub_client(tmp_path, max_posts_per_day=1, min_gap_minutes=0)
    assert c.post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "immediate"}).status_code == 201

    resp = c.post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "03", "account": READY, "mode": "immediate"})

    assert resp.status_code == 409
    assert "plafond de 1 publication" in resp.json()["detail"] and "prochaine heure possible" in resp.json()["detail"]
    next_at = _dt.fromisoformat(resp.json()["next_at"])
    assert next_at > _dt.now(_tz.utc)
    assert [p["clip_id"] for p in _publications(tmp_path, c)] == ["01"]  # rien n'est ecrit
    # 2 h apres l'heure possible (meme jour) : juste avant minuit, next_at = 00:00 tombe sous l'avance
    # minimale de programmation et le test echouait selon l'heure.
    ok = c.post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "03", "account": READY, "mode": "scheduled",
        "publish_at": (next_at + timedelta(hours=2)).isoformat()})
    assert ok.status_code == 201, ok.text


def test_an_account_not_ready_or_unknown_is_refused(tmp_path, isolated_cwd):
    _publications_setup(tmp_path, ready=(READY,))
    c = _pub_client(tmp_path)

    for account in (SPARE, "zzzzzz"):
        resp = c.post("/api/publications", json={
            "video_id": CLIPS_VIDEO, "clip_id": "01", "account": account, "mode": "immediate"})
        assert resp.status_code == 409
    assert _publications(tmp_path) == []


# --------------------------------------------------------------------------
# TASK-5c00d0c09c98 : « Après la dernière programmation + N h » (SPEC-1ed3)
# --------------------------------------------------------------------------


def test_publication_after_last_adds_the_interval_to_the_accounts_last_future_publication(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    when = _soon(days=2)
    c = _pub_client(tmp_path)
    created = c.post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "scheduled", "publish_at": when})
    assert created.status_code == 201, created.text

    resp = c.get("/api/publications/after-last", params={"account": READY, "interval_hours": 3})

    assert resp.status_code == 200, resp.text
    expected = (_dt.fromisoformat(when) + _td(hours=3)).isoformat()
    assert resp.json()["publish_at"] == expected


def test_publication_after_last_falls_back_to_now_without_any_future_publication(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)

    resp = _pub_client(tmp_path).get("/api/publications/after-last", params={"account": READY, "interval_hours": 4})

    assert resp.status_code == 200, resp.text
    at = _dt.fromisoformat(resp.json()["publish_at"])
    now = _dt.now(_tz.utc)
    assert now + _td(hours=3, minutes=55) < at < now + _td(hours=4, minutes=5)


def test_publication_after_last_refuses_an_unknown_account_or_a_non_positive_interval(tmp_path, isolated_cwd):
    _publications_setup(tmp_path, ready=(READY,))
    c = _pub_client(tmp_path)

    unready = c.get("/api/publications/after-last", params={"account": SPARE, "interval_hours": 2})
    unknown = c.get("/api/publications/after-last", params={"account": "zzzzzz", "interval_hours": 2})
    zero = c.get("/api/publications/after-last", params={"account": READY, "interval_hours": 0})
    negative = c.get("/api/publications/after-last", params={"account": READY, "interval_hours": -1})

    assert unready.status_code == 409 and unknown.status_code == 409
    assert zero.status_code == 422 and "intervalle" in zero.json()["detail"]
    assert negative.status_code == 422


@pytest.mark.parametrize("body, status", [
    ({"clip_id": "01", "account": READY, "mode": "immediate"}, 422),
    ({"video_id": CLIPS_VIDEO, "clip_id": "../x", "account": READY, "mode": "immediate"}, 400),
    ({"video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "demain"}, 409),
    ({"video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "scheduled"}, 409),
    ({"video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "scheduled", "publish_at": "demain"}, 422),
    ({"video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "scheduled", "publish_at": "2030-01-01T10:00:00"}, 422),
    ({"video_id": CLIPS_VIDEO, "clip_id": "99", "account": READY, "mode": "immediate"}, 409),
    ({"video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "immediate", "options": {"visibility": "x"}}, 409),
])
def test_invalid_publication_requests_are_refused(tmp_path, isolated_cwd, body, status):
    _publications_setup(tmp_path)

    resp = _pub_client(tmp_path).post("/api/publications", json=body)

    assert resp.status_code == status, resp.text
    assert _publications(tmp_path) == []


def test_a_publication_can_be_edited_until_it_is_in_progress_or_published(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    c = _pub_client(tmp_path)
    c.post("/api/publications", json={"video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "immediate"})
    when = _soon(days=4)

    resp = c.patch(f"/api/publications/{CLIPS_VIDEO}/01", json={
        "account": SPARE, "mode": "scheduled", "publish_at": when, "options": {"ai_generated": True},
        "description": "Nouvelle description", "hashtags": ["#a"]})

    assert resp.status_code == 200, resp.text
    row = _publications(tmp_path, c)[0]
    assert (row["account"], row["publish_mode"], row["slot_at"]) == (SPARE, "scheduled", when)
    assert row["post_options"] == {"ai_generated": True} and row["description"] == "Nouvelle description"
    assert row["editable"] is True

    path = tmp_path / "state" / "publish" / "ma_chaine.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["in_progress_since"] = _soon()
    path.write_text(json.dumps(entries), encoding="utf-8")
    assert c.patch(f"/api/publications/{CLIPS_VIDEO}/01", json={"account": READY}).status_code == 409
    assert c.delete(f"/api/publications/{CLIPS_VIDEO}/01").status_code == 409
    assert _publications(tmp_path, c)[0]["tiktok_status"] == "in_progress" and _publications(tmp_path, c)[0]["editable"] is False


def test_a_publication_can_be_cancelled_and_the_clip_is_to_validate_again(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    c = _pub_client(tmp_path)
    c.post("/api/publications", json={"video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY, "mode": "immediate"})

    resp = c.delete(f"/api/publications/{CLIPS_VIDEO}/01")

    assert resp.status_code == 204
    assert _publications(tmp_path, c) == []
    clips = {x["clip_id"]: x for x in client(tmp_path).get("/api/clips", params={"video_id": CLIPS_VIDEO}).json()}
    assert clips["01"]["publish_status"] == "à valider"
    assert c.delete(f"/api/publications/{CLIPS_VIDEO}/01").status_code == 409


def test_a_published_publication_cannot_be_edited_or_cancelled(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "published", published_at="2026-10-01T10:00:00+00:00",
                                                  account=READY, post_url="https://exemple.invalid/video/1")])
    c = _pub_client(tmp_path)

    assert c.patch(f"/api/publications/{CLIPS_VIDEO}/01", json={"account": SPARE}).status_code == 409
    assert c.delete(f"/api/publications/{CLIPS_VIDEO}/01").status_code == 409
    assert _publications(tmp_path, c)[0]["editable"] is False


def test_the_list_shows_the_status_of_each_entry(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "scheduled", slot_at=_soon(hours=1), account=READY, publish_mode="immediate"),
        _entry("03", "scheduled", slot_at=_soon(hours=2), account=READY, in_progress_since=_soon()),
        _entry("04", "published", account=READY, tiktok_state="scheduled_on_tiktok",
               tiktok_publish_at=_soon(days=2)),
        _entry("05", "published", account=READY, post_url="https://exemple.invalid/video/1"),
        _entry("06", "failed", account=READY, error="captcha détecté", capture=str(tmp_path / "x.png")),
        _entry("02", "rejected"),
    ])

    rows = {p["clip_id"]: p for p in _publications(tmp_path)}

    assert {k: v["tiktok_status"] for k, v in rows.items()} == {
        "01": "pending", "03": "in_progress", "04": "scheduled_on_tiktok", "05": "published", "06": "failed"}
    assert rows["05"]["post_url"] == "https://exemple.invalid/video/1"
    assert rows["06"]["publish_error"] == "captcha détecté" and rows["06"]["capture_url"]
    assert rows["01"]["thumbnail_url"] and rows["01"]["screen_title"] == "Titre 01" and rows["01"]["score"] == 80.0


def test_the_list_carries_the_defaults_of_the_form_and_the_ready_accounts(tmp_path, isolated_cwd):
    _publications_setup(tmp_path, ready=(READY,))

    data = _pub_client(tmp_path, visibility="friends", allow_comments=False).get("/api/publications").json()

    assert data["defaults"]["options"] == {"visibility": "friends", "allow_comments": False, "allow_reuse": True,
                                           "ai_generated": False, "content_check": "off"}
    assert data["defaults"]["schedule_max_days"] == 10 and data["defaults"]["schedule_min_minutes"] == 15
    assert [a["id"] for a in data["accounts"] if a["ready_to_publish"]] == [READY]


def test_a_failed_entry_of_a_video_without_channel_can_be_retried_and_shows_its_capture(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    _write_state(tmp_path, "nochannel001")
    _write_clip(tmp_path, "nochannel001", {**_clip_sidecar("01"), "video_id": "nochannel001"})
    capture = tmp_path / "state" / "browser" / READY / "captures" / "x-captcha.png"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"\x89PNG")
    _write_publish(tmp_path, "_sans_chaine", [
        _entry("01", "failed", video_id="nochannel001", account=READY, slot_at=_soon(), error="captcha détecté",
               capture=str(capture))])
    c = _pub_client(tmp_path)

    assert c.get("/api/publish/nochannel001/01/capture").status_code in (200, 404)  # route atteinte sans 409 « chaine »
    resp = c.post("/api/publish/nochannel001/01/retry")

    assert resp.status_code == 200 and resp.json()["status"] == "scheduled"


def test_publication_form_markup_and_wiring_are_in_the_publish_screen():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")

    assert "Nouvelle publication" in js and "data-pub-new" in js and "pubOpenForm" in js
    assert "/api/publications" in js and '"POST"' in js and "next_at" in js
    for marker in ("pub-form-search", "pub-form-video", "pub-form-channel", "pub-form-account", "pub-form-when",
                   "pub-form-at", "pub-form-caption", "pub-form-tags", "pub-form-visibility", "pub-form-comments",
                   "pub-form-reuse", "pub-form-ai", "pub-form-check"):
        assert marker in js, marker
    assert "Maintenant" in js and "Programmer" in js and "score" in js and "thumbnail_url" in js
    assert "Contenu généré par IA" in js and "Vérification de contenu" in js and "Toi uniquement" in js
    # le statut de chaque entree, modifiable / annulable
    for label in ("En attente", "En cours", "Programmée sur TikTok", "Publiée", "Échec"):
        assert label in js, label
    assert "in_progress" in js and "capture_url" in js and "post_url" in js and '"DELETE"' in js and '"PATCH"' in js


def test_clips_screen_publish_now_opens_the_prefilled_form():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")

    assert "Publier maintenant" in js and "data-publish-now" in js and "pubOpenForm" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_the_form_lists_only_clips_to_validate_or_approved_newest_first():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    start = js.index("const pubKey")
    start_key = js[start:js.index("\n", start) + 1]
    fn = js[js.index("function pubFormClips"):js.index("\n}\n", js.index("function pubFormClips")) + 3]
    clips = [
        {"video_id": "v1", "clip_id": "01", "ready": True, "publish_status": "à valider", "created_at": "2026-09-30T10:00:00"},
        {"video_id": "v1", "clip_id": "02", "ready": True, "publish_status": "rejected", "created_at": "2026-10-01T10:00:00"},
        {"video_id": "v2", "clip_id": "01", "ready": True, "publish_status": "approved", "created_at": "2026-10-01T09:00:00"},
        {"video_id": "v2", "clip_id": "02", "ready": True, "publish_status": "published", "created_at": "2026-10-01T12:00:00"},
        {"video_id": "v2", "clip_id": "03", "ready": True, "publish_status": "scheduled", "created_at": "2026-10-01T13:00:00"},
        {"video_id": "v3", "clip_id": "01", "ready": False, "publish_status": "not_ready", "created_at": "2026-10-02T10:00:00"},
    ]
    script = start_key + fn + f"\nconsole.log(JSON.stringify(pubFormClips({json.dumps(clips)}).map(pubKey)));"
    out = _node_run(script)
    assert json.loads(out) == ["v2/01", "v1/01"]  # plus recents en haut ; ni refuses, ni publies, ni deja en file


# --------------------------------------------------------------------------
# Fiche vidéo : diagramme en étoile du jury par moment (TASK-3a08)
# --------------------------------------------------------------------------

JURY_VID = "jury1234567"
_CRITERIA = {"hook": 3, "retention": 2, "clarte": 1, "meme": 0}


def _jr_judge(scores, confidence, argument="ok", veto=None):
    out = {"scores": scores, "score": round(sum(scores.values()) / len(scores) * 10, 1),
           "argument": argument, "confidence": confidence}
    if veto:
        out.update({"veto": True, "veto_reason": veto})
    return out


def _jr_moment(**over):
    base = {
        "start": 10.0, "end": 40.0, "duration": 30.0, "format": "single", "parts": [],
        "scores": {"hook": 8, "retention": 7, "clarte": 6, "meme": 5}, "bonus": {"total": 0.0},
        "final_score": 71.0, "justification": "Bon moment", "hook_text": "Regarde ça",
        "jury": {
            "proposer": {"scores": {"hook": 9, "retention": 9, "clarte": 9, "meme": 9}, "justification": "p"},
            "score": 71.0, "confidence": 62, "veto": None, "debated": True,
            "trace": {"rounds": [
                {"round": 1, "judges": {
                    "retention": _jr_judge({"hook": 9, "retention": 8, "clarte": 6, "meme": 5}, 90),
                    "avocat": _jr_judge({"hook": 5, "retention": 4, "clarte": 6, "meme": 5}, 30)}},
                {"round": 2, "judges": {
                    "avocat": _jr_judge({"hook": 7, "retention": 6, "clarte": 6, "meme": 5}, 55)}},
            ], "revisions": [], "dissent": []},
        },
    }
    base.update(over)
    return base


def _write_jury_moments(tmp_path, **over):
    data = {
        "video_id": JURY_VID, "rubric": {"path": "rubric.toml", "weights": _CRITERIA, "min_score": 60},
        "chunked": False, "selection": "jury",
        "jury": {"judges": [{"name": "retention", "veto": False}, {"name": "avocat", "veto": False}],
                 "threshold": 20, "debated": ["m0"]},
        "exploration": {"share": 0.1, "seed": 0, "target": 1, "chosen": 1},
        "moments": [
            {"id": 0, **_jr_moment()},
            {"id": 1, **_jr_moment(start=100.0, end=130.0, final_score=55.0, exploration=True)},
        ],
        "rejected": [
            {**_jr_moment(start=200.0, end=230.0, final_score=48.0),
             "reason": "score 48.0 < min_score 60"},
            {**_jr_moment(start=300.0, end=330.0, final_score=75.0),
             "reason": "ecarte par le plafond de 1 moments par heure de video (max_moments_per_hour 1)"},
            {**_jr_moment(start=400.0, end=430.0, final_score=None, jury={
                **_jr_moment()["jury"], "veto": {"judge": "conformite", "reason": "propos haineux"}}),
             "reason": "veto du juge conformite : propos haineux"},
        ],
        **over,
    }
    data["rejected"][2].pop("final_score")
    _write_json(tmp_path / "workspace" / JURY_VID / "moments.json", data)


def _jury(tmp_path, video_id=JURY_VID):
    resp = client(tmp_path).get(f"/api/videos/{video_id}/jury")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_api_jury_lists_retained_moments_first_then_rejected_with_the_rubric(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)

    body = _jury(tmp_path)

    assert body["available"] is True and body["reason"] is None
    assert body["criteria"] == [{"name": n, "weight": w} for n, w in _CRITERIA.items()]
    assert body["threshold"] == 60
    assert body["selection"] == "jury"
    assert body["exploration"] == {"share": 0.1, "seed": 0, "target": 1, "chosen": 1}
    assert [m["retained"] for m in body["moments"]] == [True, True, False, False, False]
    assert [m["start"] for m in body["moments"]] == [10.0, 100.0, 200.0, 300.0, 400.0]
    assert len({m["key"] for m in body["moments"]}) == 5


def test_api_jury_moment_carries_scores_judges_rounds_and_confidence(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)

    first = _jury(tmp_path)["moments"][0]

    assert first["scores"] == {"hook": 8, "retention": 7, "clarte": 6, "meme": 5}  # note retenue (médiane pondérée)
    assert first["final_score"] == 71.0 and first["jury_score"] == 71.0
    assert first["confidence"] == 62
    assert first["debated"] is True
    assert first["veto"] is None
    assert first["justification"] == "Bon moment" and first["hook_text"] == "Regarde ça"
    assert first["proposer_scores"]["hook"] == 9
    before, after = first["rounds"]
    assert (before["round"], after["round"]) == (1, 2)
    assert before["judges"]["avocat"]["scores"]["hook"] == 5 and before["judges"]["avocat"]["confidence"] == 30
    # après débat : le juge qui a révisé change, l'autre garde ses notes du tour 1
    assert after["judges"]["avocat"]["scores"]["hook"] == 7 and after["judges"]["avocat"]["confidence"] == 55
    assert after["judges"]["avocat"]["revised"] is True
    assert after["judges"]["retention"]["scores"]["hook"] == 9 and after["judges"]["retention"]["revised"] is False
    assert before["judges"]["retention"]["revised"] is True


def test_api_jury_reasons_for_retention_and_rejection(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)

    moments = _jury(tmp_path)["moments"]

    assert [m["reason_kind"] for m in moments] == ["retenu", "exploration", "score", "plafond", "veto"]
    assert "71.0" in moments[0]["reason"] and "60" in moments[0]["reason"]
    assert "exploration" in moments[1]["reason"]
    assert moments[2]["reason"] == "score 48.0 < min_score 60"
    assert "plafond" in moments[3]["reason"]
    assert moments[4]["veto"] == {"judge": "conformite", "reason": "propos haineux"}
    assert moments[4]["final_score"] is None


def test_api_jury_single_round_moment_has_one_round_and_no_debate(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)
    path = tmp_path / "workspace" / JURY_VID / "moments.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["moments"][0]["jury"]["trace"]["rounds"] = data["moments"][0]["jury"]["trace"]["rounds"][:1]
    data["moments"][0]["jury"]["debated"] = False
    path.write_text(json.dumps(data), encoding="utf-8")

    first = _jury(tmp_path)["moments"][0]

    assert first["debated"] is False and len(first["rounds"]) == 1


def test_api_jury_without_moments_json_is_an_explicit_unavailable_answer(tmp_path, isolated_cwd):
    body = _jury(tmp_path)

    assert body["available"] is False
    assert "moments.json" in body["reason"]
    assert body["moments"] == []


def test_api_jury_without_jury_in_moments_json_is_explicit(tmp_path, isolated_cwd):
    _write_json(tmp_path / "workspace" / JURY_VID / "moments.json", {
        "video_id": JURY_VID, "rubric": {"path": "rubric.toml", "weights": _CRITERIA, "min_score": 60},
        "selection": "single", "moments": [{"id": 0, "start": 1.0, "end": 9.0, "scores": {}, "final_score": 70.0}],
        "rejected": []})

    body = _jury(tmp_path)

    assert body["available"] is False
    assert "jury" in body["reason"] and "single" in body["reason"]
    assert body["moments"] == []


def test_api_jury_rejects_an_invalid_video_id(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/api/videos/a%20b/jury")
    assert resp.status_code in (400, 404)


def test_api_jury_is_read_only(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)
    path = tmp_path / "workspace" / JURY_VID / "moments.json"
    before = path.read_bytes()

    assert client(tmp_path).post(f"/api/videos/{JURY_VID}/jury").status_code == 405
    _jury(tmp_path)

    assert path.read_bytes() == before


def test_api_jury_corrupted_moments_json_is_an_explicit_error(tmp_path, isolated_cwd):
    (tmp_path / "workspace" / JURY_VID).mkdir(parents=True)
    (tmp_path / "workspace" / JURY_VID / "moments.json").write_text("{pas du json", encoding="utf-8")

    resp = client(tmp_path).get(f"/api/videos/{JURY_VID}/jury")

    assert resp.status_code == 500 or resp.json().get("available") is False
    assert "moments.json" in json.dumps(resp.json())


# ---- statique : radar en SVG inline ----


def _radar_js() -> str:
    return (STATIC / "screens" / "jury-radar.js").read_text(encoding="utf-8")


@pytest.fixture()
def jury_payload(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)
    return _jury(tmp_path)


def _run_radar(payload, moment_index=0, round_index=None):
    script = _radar_js() + (
        "\nconst data = JSON.parse(process.argv[1]);"
        f"\nprocess.stdout.write(juryRadarSvg(data, data.moments[{moment_index}], {json.dumps(round_index)}));"
    )
    return _node_run(script, json.dumps(payload))


def test_radar_js_is_wired_in_the_page_before_videos_and_has_no_external_library():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = _radar_js()

    assert html.index("jury-radar.js") < html.index("screens/videos.js")
    assert "import " not in js and "require(" not in js and "http" not in js
    videos = (STATIC / "screens" / "videos.js").read_text(encoding="utf-8")
    assert "/jury`" in videos and "juryPanelHtml" in videos and "data-jr-round" in videos


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_node_check_of_every_static_script():
    for js in sorted(STATIC.rglob("*.js")):
        subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, encoding="utf-8", check=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_radar_svg_structure_for_a_two_round_moment(jury_payload):
    import re
    svg = _run_radar(jury_payload, 0)

    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert 'role="img"' in svg and "<title>" in svg
    assert len(re.findall(r'class="jr-axis[ "]', svg)) == 4                 # une branche par critère
    assert len(re.findall(r'class="jr-axis jr-zero"', svg)) == 1            # poids 0 grisé
    assert len(re.findall(r'class="jr-judge ', svg)) == 2                   # un polygone fin par juge
    assert len(re.findall(r'class="jr-retained"', svg)) == 1                # un polygone épais retenu
    for label in ("hook", "retention", "clarte", "meme"):
        assert re.search(rf'<text class="jr-label[^>]*>{label} ', svg)
    assert "poids 3" in svg and "poids 0" in svg
    # opacité du trait selon la confiance : après débat l'avocat est à 55, le juge retention à 90
    opacities = {m[0]: float(m[1]) for m in re.findall(r'data-judge="([^"]+)"[^>]*stroke-opacity="([0-9.]+)"', svg)}
    assert opacities["retention"] > opacities["avocat"]
    assert 0 < opacities["avocat"] < 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_radar_before_and_after_debate_differ_for_the_revising_judge(jury_payload):
    import re
    before = _run_radar(jury_payload, 0, 0)
    after = _run_radar(jury_payload, 0, 1)

    def points(svg, judge):
        return re.search(rf'data-judge="{judge}"[^>]*points="([^"]+)"', svg).group(1)

    assert points(before, "avocat") != points(after, "avocat")
    assert points(before, "retention") == points(after, "retention")
    # une valeur de 10 sur l'axe haut touche le rayon, 0 reste au centre : coordonnées finies
    assert "NaN" not in before + after and "undefined" not in before + after


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_radar_panel_shows_veto_threshold_reason_and_toggle(jury_payload):
    script = _radar_js() + (
        "\nconst data = JSON.parse(process.argv[1]);"
        "\nconst ui = {key: null, round: null};"
        "\nprocess.stdout.write(JSON.stringify([juryPanelHtml(data, ui), juryPanelHtml(data, {key: data.moments[4].key, round: null})]));"
    )
    out = _node_run(script, json.dumps(jury_payload))
    retained, vetoed = json.loads(out)

    assert "data-jr-round" in retained                      # interrupteur avant / après débat (il y a eu débat)
    assert "Avant débat" in retained and "Après débat" in retained
    assert "seuil" in retained.lower() and "71" in retained and "60" in retained
    assert "Bon moment" in retained and "Regarde ça" in retained   # justification + phrase d'accroche
    assert "retention" in retained and "avocat" in retained       # légende des juges
    assert "jr-veto" not in retained
    assert "jr-veto" in vetoed and "propos haineux" in vetoed and "conformite" in vetoed


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_radar_panel_without_debate_has_no_toggle_and_unavailable_is_explicit(jury_payload):
    jury_payload["moments"][0]["debated"] = False
    jury_payload["moments"][0]["rounds"] = jury_payload["moments"][0]["rounds"][:1]
    script = _radar_js() + (
        "\nconst data = JSON.parse(process.argv[1]);"
        "\nprocess.stdout.write(JSON.stringify([juryPanelHtml(data, {key: null, round: null}),"
        " juryPanelHtml({available: false, reason: 'moments.json absent', moments: []}, {key: null, round: null})]));"
    )
    out = _node_run(script, json.dumps(jury_payload))
    plain, unavailable = json.loads(out)

    assert "data-jr-round" not in plain
    assert "moments.json absent" in unavailable and "<svg" not in unavailable


def test_radar_css_is_themed_and_responsive():
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert ".jr-retained" in css and ".jr-zero" in css
    assert css.count("--jr-c0:") == 2                     # défini en sombre et en clair
    assert css.index("--jr-c0:", css.index('[data-theme="light"]')) > 0
    assert "@media (max-width: 900px)" in css[css.index(".vjury"):]  # mobile


# --------------------------------------------------------------------------
# TASK-c32b (sixième tour de la console)
# --------------------------------------------------------------------------

TWITCH_ID = "v2887271276"
TWITCH_URL = "https://www.twitch.tv/videos/2887271276"
TWITCH_THUMB = "https://static-cdn.example.invalid/previews/v2887271276.jpg"


def test_a_youtube_video_gets_its_platform_thumbnail_from_its_id_without_any_file(tmp_path, isolated_cwd):
    _write_state(tmp_path, "aaaaaaaaaaa")

    video = client(tmp_path).get("/api/videos").json()[0]

    assert video["platform_thumbnail"] == "https://i.ytimg.com/vi/aaaaaaaaaaa/hqdefault.jpg"


def test_a_twitch_video_uses_the_thumbnail_recorded_by_the_download(tmp_path, isolated_cwd):
    _write_state(tmp_path, TWITCH_ID, source_url=TWITCH_URL)
    _write_json(tmp_path / "workspace" / TWITCH_ID / "thumbnail.json", {"url": TWITCH_THUMB})
    c = client(tmp_path)

    assert c.get(f"/api/videos/{TWITCH_ID}").json()["platform_thumbnail"] == TWITCH_THUMB
    # meta.json (téléchargement terminé) prime sur le relevé du début du téléchargement
    _write_json(tmp_path / "workspace" / TWITCH_ID / "meta.json", {"title": "VOD", "thumbnail": TWITCH_THUMB + "?final"})
    assert c.get(f"/api/videos/{TWITCH_ID}").json()["platform_thumbnail"] == TWITCH_THUMB + "?final"


def test_a_twitch_video_without_any_recorded_thumbnail_has_none_not_a_made_up_one(tmp_path, isolated_cwd):
    _write_state(tmp_path, TWITCH_ID, source_url=TWITCH_URL)

    assert client(tmp_path).get("/api/videos").json()[0]["platform_thumbnail"] is None


def test_queue_entries_and_dashboard_rows_carry_the_platform_thumbnail(tmp_path, isolated_cwd):
    _busy_worker(tmp_path)
    entries = [{"id": "e1", "video_id": VIDEO_ID, "url": URL, "channel": None, "action": "run", "force_steps": [],
                "enqueued_at": "2026-01-01T00:00:00+00:00", "status": "waiting", "pid": None},
               {"id": "e2", "video_id": TWITCH_ID, "url": TWITCH_URL, "channel": None, "action": "run", "force_steps": [],
                "enqueued_at": "2026-01-01T00:00:01+00:00", "status": "waiting", "pid": None}]
    _write_json(tmp_path / "state" / "queue.json", entries)
    _write_state(tmp_path, "bbbbbbbbbbb", status="failed", reason="403")
    _write_state(tmp_path, "ccccccccccc", status="running")
    c = client(tmp_path)

    queue = c.get("/api/queue").json()
    data = c.get("/api/dashboard").json()

    assert [e["platform_thumbnail"] for e in queue] == [f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg", None]
    assert [(e["video_id"], e["id"]) for e in data["queue"]] == [(VIDEO_ID, "e1"), (TWITCH_ID, "e2")]
    assert data["queue"][0]["platform_thumbnail"].endswith(f"/{VIDEO_ID}/hqdefault.jpg")
    assert data["failed"][0]["platform_thumbnail"] == "https://i.ytimg.com/vi/bbbbbbbbbbb/hqdefault.jpg"
    assert data["running"][0]["platform_thumbnail"] == "https://i.ytimg.com/vi/ccccccccccc/hqdefault.jpg"


def test_dashboard_running_card_carries_the_title_of_the_video(tmp_path, isolated_cwd):
    _busy_worker(tmp_path)
    _write_state(tmp_path, "aaaaaaaaaaa", status="running")
    _write_state(tmp_path, "bbbbbbbbbbb", status="running")
    _write_json(tmp_path / "workspace" / "aaaaaaaaaaa" / "meta.json", {"title": "Mon titre de vidéo"})

    running = {v["video_id"]: v for v in _dashboard(tmp_path)["running"]}

    assert running["aaaaaaaaaaa"]["title"] == "Mon titre de vidéo"
    assert running["bbbbbbbbbbb"]["title"] == "bbbbbbbbbbb"  # sans meta.json : l'identifiant, jamais un titre inventé


def test_clips_are_listed_newest_first_by_sidecar_creation_date(tmp_path, isolated_cwd):
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_state(tmp_path, "othervideo01", channel="autre")
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01", created_at="2026-09-30T10:00:00+00:00"))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("02", created_at="2026-10-01T10:00:00+00:00"))
    other = _clip_sidecar("01", created_at="2026-10-01T18:00:00+00:00")
    other["video_id"] = "othervideo01"
    _write_clip(tmp_path, "othervideo01", other)

    clips = client(tmp_path).get("/api/clips").json()

    assert [(c["video_id"], c["clip_id"]) for c in clips] == [("othervideo01", "01"), (CLIPS_VIDEO, "02"), (CLIPS_VIDEO, "01")]
    only = client(tmp_path).get("/api/clips", params={"video_id": CLIPS_VIDEO}).json()
    assert [c["clip_id"] for c in only] == ["02", "01"]  # filtre par vidéo : seulement cette vidéo


def test_approving_a_clip_of_a_video_without_style_uses_the_account_only(tmp_path, isolated_cwd):
    # SPEC-6076 R2 / SPEC-1ed3 R3 : un style ne porte plus de compte ni de créneaux, le compte suffit ;
    # l'entrée va dans la file _sans_chaine (lue par le worker), seule ou en groupe.
    _channels_setup(tmp_path)
    _write_state(tmp_path, CLIPS_VIDEO)
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01"))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("02"))
    _accounts_state(tmp_path, ready=(READY,))

    one = client(tmp_path).post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": READY})
    bulk = client(tmp_path).post("/api/clips/approve", json={"account": READY, "clips": [{"video_id": CLIPS_VIDEO, "clip_id": "02"}]})

    assert one.status_code == 200, one.text
    assert bulk.status_code == 200, bulk.text
    entries = json.loads((tmp_path / "state" / "publish" / "_sans_chaine.json").read_text(encoding="utf-8"))
    assert sorted((e["clip_id"], e["account"]) for e in entries) == [("01", READY), ("02", READY)]


def test_assigning_a_channel_to_a_video_writes_pipeline_json_and_unblocks_approval(tmp_path, isolated_cwd, caplog):
    import logging

    _channels_setup(tmp_path)
    _write_state(tmp_path, CLIPS_VIDEO, status="done")
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("01"))
    c = client(tmp_path)

    with caplog.at_level(logging.INFO, logger="clipper.pipeline"):
        resp = c.post(f"/api/videos/{CLIPS_VIDEO}/channel", json={"channel": CH})

    assert resp.status_code == 200 and resp.json()["channel"] == CH
    assert json.loads((tmp_path / "workspace" / CLIPS_VIDEO / "pipeline.json").read_text(encoding="utf-8"))["channel"] == CH
    assert any(CH in r.getMessage() for r in caplog.records)
    assert c.get(f"/api/clips", params={"video_id": CLIPS_VIDEO}).json()[0]["channel"] == CH


def test_assigning_a_channel_refuses_an_unknown_channel_and_a_bad_name(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    _write_state(tmp_path, CLIPS_VIDEO, status="done")
    c = client(tmp_path)

    assert c.post(f"/api/videos/{CLIPS_VIDEO}/channel", json={"channel": "fantome"}).status_code == 409
    assert c.post(f"/api/videos/{CLIPS_VIDEO}/channel", json={"channel": "../x"}).status_code == 400
    assert c.post("/api/videos/zzzzzzzzzzz/channel", json={"channel": CH}).status_code == 404


def test_publication_times_are_given_in_europe_paris_in_summer_and_in_winter(tmp_path, isolated_cwd):
    _publications_setup(tmp_path)
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "scheduled", slot_at="2026-07-14T10:30:00+00:00", account=READY, publish_mode="scheduled"),
        _entry("02", "scheduled", slot_at="2026-01-14T10:30:00+00:00", account=READY, publish_mode="scheduled"),
        _entry("03", "published", account=READY, published_at="2026-07-14T08:00:00+00:00",
               tiktok_state="scheduled_on_tiktok", tiktok_publish_at="2026-07-15T10:30:00Z"),
    ])

    rows = {p["clip_id"]: p for p in _publications(tmp_path)}

    assert rows["01"]["slot_at_paris"] == "2026-07-14T12:30:00+02:00"   # été : UTC+2
    assert rows["02"]["slot_at_paris"] == "2026-01-14T11:30:00+01:00"   # hiver : UTC+1
    assert rows["03"]["tiktok_publish_at_paris"] == "2026-07-15T12:30:00+02:00"
    assert rows["03"]["published_at_paris"] == "2026-07-14T10:00:00+02:00"
    assert rows["01"]["published_at_paris"] is None


def test_a_post_scheduled_on_tiktok_is_not_live_before_its_time_and_is_after(tmp_path, isolated_cwd, monkeypatch):
    _publications_setup(tmp_path)
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "published", account=READY, tiktok_state="scheduled_on_tiktok", tiktok_publish_at="2026-10-02T10:30:00+00:00"),
        _entry("02", "published", account=READY, post_url="https://exemple.invalid/video/1"),
    ])
    before = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(web_app, "_now_utc", lambda: before)
    rows = {p["clip_id"]: p for p in _publications(tmp_path)}
    assert rows["01"]["tiktok_status"] == "scheduled_on_tiktok" and rows["01"]["tiktok_live"] is False
    assert rows["02"]["tiktok_live"] is True

    monkeypatch.setattr(web_app, "_now_utc", lambda: datetime(2026, 10, 2, 10, 30, tzinfo=timezone.utc))
    assert {p["clip_id"]: p for p in _publications(tmp_path)}["01"]["tiktok_live"] is True


def test_dashboard_next_publications_give_their_time_in_europe_paris(tmp_path, isolated_cwd):
    _write_sidecar(tmp_path, "aaaaaaaaaaa", "01")
    _write_json(tmp_path / "state" / "publish" / "ma_chaine.json",
                [_publish_entry("aaaaaaaaaaa", "01", "scheduled", "2030-01-15T10:30:00+00:00")])

    assert _dashboard(tmp_path)["next_publications"][0]["slot_at_paris"] == "2030-01-15T11:30:00+01:00"


def test_week_view_lists_manual_posts_that_sit_between_the_channel_slots(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [
        _entry("01", "scheduled", slot_at=PUB_THU, publish_mode="scheduled"),
        _entry("02", "scheduled", slot_at="2026-10-07T12:15:00+00:00", publish_mode="scheduled", account="ab12cd"),
        _entry("03", "scheduled", slot_at="2026-10-14T12:15:00+00:00", publish_mode="scheduled"),
    ])

    data = _get_publish(tmp_path).json()

    assert [c["clip_id"] for c in data["off_slot"]] == ["02"]          # ni sur un créneau, ni d'une autre semaine
    assert data["off_slot"][0]["slot_at_paris"] == "2026-10-07T14:15:00+02:00"
    assert [s["clip"]["clip_id"] for s in data["slots"] if s["clip"]] == ["01"]


# --- TASK-c32b : aides pour exécuter une fonction des scripts statiques sous node ---------------------------


def _js_def(source: str, name: str) -> str:
    """Texte de `function name(...) {...}` ou de `const name = ...;` dans un script statique."""
    for marker in (f"function {name}(", f"const {name} = "):
        start = source.find(marker)
        if start >= 0:
            break
    else:
        raise AssertionError(f"{name} introuvable")
    if marker.startswith("function"):
        return source[start:source.index("\n}\n", start) + 3]
    end = start
    while True:
        end = source.index(";\n", end) + 2
        chunk = source[start:end]
        if chunk.count("(") == chunk.count(")") and chunk.count("{") == chunk.count("}") and chunk.count("`") % 2 == 0:
            return chunk


_JS_PRELUDE = ("const esc = (s) => String(s == null ? '' : s).replace(/[&<>\"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\n"
               "const fr = (n, d) => Number(n).toLocaleString('fr-FR', {minimumFractionDigits: d || 0, maximumFractionDigits: d || 0});\n"
               "const icon = (n) => `<i ${n}>`;\n")


def _run_js(files_and_names, expr, preamble=""):
    """Évalue `expr` (JSON.stringify de son résultat) avec les définitions extraites des scripts statiques."""
    parts = [_JS_PRELUDE, preamble]
    for relative, names in files_and_names:
        source = (STATIC / relative).read_text(encoding="utf-8")
        parts += [_js_def(source, n) for n in names]
    script = "\n".join(parts) + f"\nconsole.log(JSON.stringify({expr}));"
    out = _node_run(script)
    return json.loads(out)


_NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")


@_NODE
def test_video_thumb_tries_the_local_image_then_the_platform_one_then_the_neutral_placeholder():
    out = _run_js([("screens.js", ["videoThumb", "thumbFallback"])], """(() => {
      const html = videoThumb('v1', 'https://i.ytimg.com/vi/v1/hqdefault.jpg');
      const img = { dataset: { platform: 'https://i.ytimg.com/vi/v1/hqdefault.jpg' }, src: '/media/source/v1/thumbnail' };
      const first = thumbFallback(img), afterFirst = img.src, second = thumbFallback(img);
      return { html, bare: videoThumb('v2'), first, afterFirst, second };
    })()""")
    assert 'src="/media/source/v1/thumbnail"' in out["html"] and 'data-platform="https://i.ytimg.com/vi/v1/hqdefault.jpg"' in out["html"]
    assert "data-platform" not in out["bare"] and "pas d'image" in out["bare"]
    assert (out["first"], out["afterFirst"], out["second"]) == ("platform", "https://i.ytimg.com/vi/v1/hqdefault.jpg", "none")


def test_every_video_thumb_caller_passes_the_platform_thumbnail():
    for relative in ("screens/dashboard.js", "screens/videos.js", "screens.js"):
        js = (STATIC / relative).read_text(encoding="utf-8")
        calls = [line for line in js.splitlines() if "videoThumb(" in line and "function videoThumb" not in line]
        assert calls and all("platform_thumbnail" in line for line in calls), (relative, calls)


@_NODE
def test_progress_bar_is_indeterminate_and_visible_without_a_fraction():
    out = _run_js([("screens.js", ["progressBar"])], "[progressBar(null), progressBar(40)]")
    assert "indeterminate" in out[0] and "aria-valuenow" not in out[0] and "<i></i>" in out[0]
    assert 'aria-valuenow="40"' in out[1] and "width:40%" in out[1] and "indeterminate" not in out[1]


def test_dashboard_running_card_shows_the_video_title_and_the_bar_has_a_width():
    js = (STATIC / "screens" / "dashboard.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "esc(video.title || video.video_id)" in js and "progressBar(pct)" in js
    assert re.search(r"\.job-prog \.bar \{[^}]*width: \d+px", css)            # la colonne « auto » donnait 0 px
    assert ".bar.indeterminate > i" in css and "@keyframes indeterminate" in css


def test_thumbnail_boxes_stay_16_9_and_do_not_reuse_the_empty_state_class():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    js = (STATIC / "screens.js").read_text(encoding="utf-8")
    assert ".job-thumb.no-img img" in css and ".job-thumb.empty" not in css   # `.empty` est l'état vide : 48 px de marge, vignette carrée
    assert 'classList.add("no-img")' in js and 'classList.add("empty")' not in js
    assert re.search(r"\.job-thumb \{[^}]*align-self: start", css)


@_NODE
def test_clips_screen_filters_by_video_from_the_address_and_the_filter_can_be_removed():
    clips = [{"video_id": "v1", "clip_id": "01", "publish_status": "approved", "channel": "a"},
             {"video_id": "v2", "clip_id": "01", "publish_status": "à valider", "channel": "a"},
             {"video_id": "v2", "clip_id": "02", "publish_status": "published", "channel": "b"}]
    out = _run_js([("screens/clips.js", ["clipsHashVideo", "clipsFiltered"])], f"""(() => {{
      const all = {json.dumps(clips)};
      const ids = (ui) => clipsFiltered(all, ui).map((c) => c.video_id + '/' + c.clip_id);
      globalThis.location = {{ hash: '#/clips/v2' }};
      const fromLink = clipsHashVideo();
      globalThis.location = {{ hash: '#/clips' }};
      return {{ fromLink, bare: clipsHashVideo(),
        v2: ids({{ filter: 'all', channel: '', video: 'v2' }}), none: ids({{ filter: 'all', channel: '', video: '' }}),
        v2Status: ids({{ filter: 'published', channel: '', video: 'v2' }}), v2Chan: ids({{ filter: 'all', channel: 'a', video: 'v2' }}) }};
    }})()""")
    assert out["fromLink"] == "v2" and out["bare"] == ""
    assert out["v2"] == ["v2/01", "v2/02"]                                     # #/clips/v2 : seulement cette vidéo
    assert out["none"] == ["v1/01", "v2/01", "v2/02"]                          # filtre retiré : tout, dans l'ordre reçu (plus récents en haut)
    assert out["v2Status"] == ["v2/02"] and out["v2Chan"] == ["v2/01"]


def test_clips_screen_has_a_video_dropdown_a_removable_chip_and_follows_the_address():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")
    assert 'id="clips-video"' in js and "data-clear-video" in js
    assert "clipsSetVideo" in js and 'location.hash = video ? `#/clips/${encodeURIComponent(video)}` : "#/clips"' in js
    assert "clipsUi.hashVideo" in js and 'clipsUi.filter = "all"' in js          # le lien « Voir les N clips » montre tous les statuts
    route = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'split(/[/?]/)[0]' in route                                           # #/clips/<id> reste l'écran Clips


@_NODE
def test_assign_channel_dialog_lists_the_channels_and_posts_through_the_api():
    out = _run_js([("app.js", ["assignChannelHtml"])], "assignChannelHtml('SRzMOzqN-ZM', ['ma_chaine', 'autre'])")
    assert 'id="assign-form"' in out and "SRzMOzqN-ZM" in out
    assert '<option value="ma_chaine">ma_chaine</option>' in out and '<option value="autre">autre</option>' in out
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/api/videos/${encodeURIComponent(videoId)}/channel" in app and 'jsonBody("POST", { channel })' in app


def test_video_page_offers_assign_channel_and_the_approval_error_offers_it_too():
    videos = (STATIC / "screens" / "videos.js").read_text(encoding="utf-8")
    ui = (STATIC / "ui.js").read_text(encoding="utf-8")
    assert "data-assign-channel" in videos and "Attribuer un style" in videos and "!video.channel" in videos
    assert "openAssignChannel(videoId)" in videos or "openAssignChannel(video.video_id)" in videos
    # l'erreur 409 « pas de chaîne » (needs_channel) devient un toast avec le bouton de choix
    assert "needs_channel" in ui and "openAssignChannel(err.body.video_id, err.body.channels)" in ui
    assert "failure.body = payload" in (STATIC / "app.js").read_text(encoding="utf-8")


def test_channel_card_has_direct_actions_and_channel_fields_are_editable_without_redefine():
    js = (STATIC / "screens" / "channels.js").read_text(encoding="utf-8")
    for marker in ("data-chan-queue", "Mettre une vidéo en file pour ce style", "openAddVideo(b.dataset.chanQueue)"):
        assert marker in js
    for gone in ("data-chan-add-slot", "data-chan-account", "chSaveChannelKey", "chPresetWithChannel", "chSlotsWith"):
        assert gone not in js, gone
    # [channel] : jamais « redéfinir » ni champ grisé (disabled) ; les autres sections gardent l'héritage
    assert "const chIsDirect = (section) => section === \"channel\"" in js
    assert "const redefined = direct || key in raw" in js and "const state = direct ? \"\"" in js
    assert "chEnsureDraft" in js
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "async function openAddVideo(channel)" in app and '${c === channel ? " selected" : ""}' in app


@_NODE
def test_a_channel_field_is_rendered_enabled_without_redefine_while_other_sections_stay_inherited():
    detail = {"raw": {}, "effective": {"channel": {"mode": "review", "display_name": "x"}, "render": {"crf": 18}}}
    out = _run_js([("screens/channels.js", ["chIsDirect", "chSame", "chKind", "chField", "chControl", "chRubricEditor",
                                           "chRubricLabel"])], f"""(() => {{
      const ed = {{ draft: {{ channel: {{}}, render: {{}} }}, detail: {json.dumps(detail)} }};
      globalThis.CHAN_DAYS = []; globalThis.CHAN_MODES = [['review', 'review'], ['auto', 'auto']]; globalThis.CHAN_RUBRICS = [];
      globalThis.CHAN_RUBRIC_CUSTOM = 'custom';
      return {{ mode: chField('channel', 'mode', {{ default: 'review' }}, ed), crf: chField('render', 'crf', {{ default: 23 }}, ed) }};
    }})()""")
    assert "data-redefine" not in out["mode"] and " disabled" not in out["mode"] and "direct" in out["mode"]
    assert "data-redefine" in out["crf"] and " disabled" in out["crf"] and "inherited" in out["crf"]


def test_week_slots_carry_their_time_in_europe_paris(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [])

    slots = _get_publish(tmp_path).json()["slots"]

    assert [s["slot_at_paris"] for s in slots] == [PUB_MON, PUB_THU]  # été : +02:00, jamais un décalage fixe


# --- Écran Publication (TASK-c32b points 6, 8, 10, 11) : le fichier entier est évalué sous node avec des bouchons ---------

_PUB_STUBS = """
globalThis.document = { addEventListener() {} };
const Screens = {}; let currentScreen = null; const store = {};
const $ = () => null; const $$ = () => [];
const emptyState = (i, t, x) => `<empty>${t} ${x}</empty>`;
const renderCurrent = () => {}; const api = async () => ({}); const toast = () => {}; const toastError = () => {};
"""


def _run_publish(expr):
    source = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    script = _JS_PRELUDE + _PUB_STUBS + source + f"\nconsole.log(JSON.stringify({expr}));"
    out = _node_run(script)
    return json.loads(out)


@_NODE
def test_publication_screen_formats_every_time_in_europe_paris_summer_and_winter():
    out = _run_publish("""({
      summer: pubLocalInput('2026-07-14T10:30:00+00:00'), winter: pubLocalInput('2026-01-14T10:30:00+00:00'),
      summerBack: pubParisInstant('2026-07-14T12:30').toISOString(), winterBack: pubParisInstant('2026-01-14T11:30').toISOString(),
      whenSummer: pubWhen('2026-10-02T10:30:00+00:00'), whenWinter: pubWhen('2026-01-02T10:30:00+00:00'),
      dstDay: pubParisInstant('2026-03-29T12:00').toISOString(),
    })""")
    assert out["summer"] == "2026-07-14T12:30" and out["winter"] == "2026-01-14T11:30"          # UTC+2 l'été, UTC+1 l'hiver
    assert out["summerBack"] == "2026-07-14T10:30:00.000Z" and out["winterBack"] == "2026-01-14T10:30:00.000Z"
    assert "12:30" in out["whenSummer"] and "11:30" in out["whenWinter"]                       # jamais le décalage du navigateur
    assert out["dstDay"] == "2026-03-29T10:00:00.000Z"                                         # jour du passage à l'heure d'été


@_NODE
def test_a_post_scheduled_on_tiktok_is_not_shown_published_before_its_time():
    out = _run_publish("""(() => {
      const base = { publish_status: 'published', tiktok_status: 'scheduled_on_tiktok', tiktok_publish_at: '2026-10-02T10:30:00+00:00',
        slot_at_paris: '2026-10-01T22:55:00+02:00', published_at_paris: '2026-10-01T22:55:00+02:00' };
      const future = { ...base, tiktok_live: false }, past = { ...base, tiktok_live: true };
      return { futureChip: pubChip(future), pastChip: pubChip(past), futureLine: pubDoneLine(future), pastLine: pubDoneLine(past) };
    })()""")
    assert "Programmée sur TikTok" in out["futureChip"] and "Publiée" not in out["futureChip"]
    assert "Publiée" in out["pastChip"]
    assert out["futureLine"].startswith("programmée sur TikTok, en ligne le") and "12:30" in out["futureLine"]   # 10:30 UTC = 12:30 à Paris
    assert out["futureLine"].count("publié") == 0
    assert out["pastLine"].startswith("publié · ")


@_NODE
def test_finished_posts_leave_the_ongoing_list_and_an_approved_one_without_moment_says_why():
    out = _run_publish("""({
      ongoing: ['approved', 'scheduled', 'failed', 'published'].map((s) => pubIsOngoing({ publish_status: s })),
      old: pubNeedsMoment({ publish_status: 'approved', publish_mode: null, slot_at: null }),
      planned: pubNeedsMoment({ publish_status: 'approved', publish_mode: 'immediate', slot_at: '2026-10-01T10:00:00+00:00' }),
      row: pubPostRow({ video_id: 'v', clip_id: '01', publish_status: 'approved', publish_mode: null, slot_at: null, editable: true, screen_title: 'T' }),
    })""")
    assert out["ongoing"] == [True, True, True, False]            # publiée / programmée sur TikTok : plus dans la liste en cours
    assert out["old"] is True and out["planned"] is False
    assert "choisis Maintenant ou une date (Modifier)" in out["row"]


def _week_days(posts_by_date):
    """``days`` d'une semaine 2026-10-05..2026-10-11 (TASK-ad4d) : une entree par date, vide sauf override."""
    out = []
    for i in range(7):
        from datetime import date, timedelta
        ymd = (date(2026, 10, 5) + timedelta(days=i)).isoformat()
        posts = posts_by_date.get(ymd, [])
        out.append({"date": ymd, "count": len(posts), "posts": posts})
    return out


@_NODE
def test_publication_layout_always_shows_the_calendar_with_a_single_pending_list():
    done_clip = {"video_id": "v", "clip_id": "03", "publish_status": "published", "screen_title": "Publié", "service": "tiktok",
                 "slot_at": "2026-10-06T09:00:00+00:00", "slot_at_paris": "2026-10-06T11:00:00+02:00",
                 "published_at_paris": "2026-10-06T11:00:00+02:00", "video_url": "/m", "thumbnail_url": "/t"}
    manual_clip = {"video_id": "v", "clip_id": "02", "publish_status": "scheduled", "screen_title": "Manuel", "service": "tiktok",
                   "slot_at": "2026-10-07T12:15:00+00:00", "slot_at_paris": "2026-10-07T14:15:00+02:00",
                   "video_url": "/m", "thumbnail_url": "/t"}
    week = {"account": "ab12cd", "channel": "ma_chaine", "timezone": "Europe/Paris", "range": "week",
            "range_start": "2026-10-05", "range_end": "2026-10-11", "week_start": "2026-10-05", "week_end": "2026-10-11",
            "slots": [{"slot_at": "2026-10-05T18:30:00+02:00", "slot_at_paris": "2026-10-05T18:30:00+02:00", "clip": None, "free": True}],
            "unscheduled": [{"video_id": "v", "clip_id": "01", "publish_status": "approved", "screen_title": "T", "video_url": "/m", "thumbnail_url": "/t"}],
            "done": [done_clip],
            "off_slot": [manual_clip],
            "days": _week_days({"2026-10-06": [done_clip], "2026-10-07": [manual_clip]}),
            "accounts": [{"id": "ab12cd", "label": "Compte exemple", "ready_to_publish": True},
                         {"id": "ef34ab", "label": "second_compte", "ready_to_publish": True}], "reason": None}
    empty = {**week, "slots": [], "off_slot": [], "done": [], "days": _week_days({}), "reason": "aucun créneau défini dans [channel].slots"}
    out = _run_publish(f"""(() => {{
      pubUi.account = 'ab12cd';
      const withSlots = pubLayoutHtml({json.dumps(week)}, pubPostsSection());
      const noSlots = pubLayoutHtml({json.dumps(empty)}, pubPostsSection());
      const loading = pubLayoutHtml(null, pubPostsSection());
      return {{ withSlots, noSlots, loading }};
    }})()""")
    html = out["withSlots"]
    grid = html[html.index('class="grid g-side pub-grid"'):]
    assert grid.index("data-pub-new") < grid.index('id="pub-cal"')                  # Nouvelle publication à gauche, calendrier à droite, dans la même grille
    assert "À publier" not in html and "En attente" in html                         # une seule liste (plus de section « À publier » en double)
    assert "14:15" in grid and "Manuel" in grid                                      # une publication manuelle entre les créneaux est au calendrier, à l'heure de Paris
    assert "11:00" in grid and "Publié" in grid and "TikTok" in grid                 # un publié/échec de la semaine est aussi au calendrier (heure, service), pas dans une liste à part
    assert 'id="pub-cal"' in out["noSlots"]                                          # le calendrier reste affiché même sans compte ni créneau régulier
    assert out["noSlots"].lower().count("aucun créneau") == 1                        # le message d'indication n'apparaît qu'une fois
    assert "data-pub-new" in out["noSlots"] and "data-pub-new" in out["loading"]     # « Nouvelle publication » même sans créneau ni calendrier chargé
    assert 'id="pub-account"' in html and "Tous les comptes" in html                 # sélecteur : tous les comptes + un par compte
    assert '<option value="ab12cd" selected>Compte exemple</option>' in html and "second_compte" in html


# --------------------------------------------------------------------------
# Calendrier : vues Jour / Semaine / Mois côté JS (TASK-ad4d)
# --------------------------------------------------------------------------


def test_publish_calendar_has_a_day_week_month_switch_defaulting_to_week():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")

    assert 'range: "week"' in js                                       # Semaine par défaut, retenue dans pubUi (session)
    assert "data-range" in js and "pubUi.range" in js
    for label in ('"Jour"', '"Semaine"', '"Mois"'):
        assert label in js, label


@_NODE
def test_publish_box_density_class_scales_with_post_count():
    # Paliers (TASK-460904227d2f) : normal <= 4, compact <= 10, mini au-dela.
    out = _run_publish("[0, 1, 4, 5, 10, 12, 20].map(pubBoxClass)")
    assert out == ["", "", "", "compact", "compact", "mini", "mini"]


def _busy_week(ymd, n, free=None):
    posts = [{"video_id": "v", "clip_id": f"{i:02d}", "publish_status": "scheduled", "screen_title": f"Clip {i}",
              "service": "tiktok", "slot_at": f"{ymd}T{i % 24:02d}:00:00+00:00", "slot_at_paris": f"{ymd}T{i % 24:02d}:00:00+02:00",
              "video_url": "/m", "thumbnail_url": "/t"} for i in range(n)]
    return {"week_start": "2026-10-05", "week_end": "2026-10-11", "slots": [free] if free else [], "days": _week_days({ymd: posts})}


@_NODE
@pytest.mark.parametrize("n", [20, 40])
def test_publish_week_box_renders_every_post_of_a_busy_day_without_any_more_link(n):
    # Demande utilisateur 2026-10-04 (TASK-39bbe20b3284) : tout est visible en Semaine, la case s'agrandit.
    ymd = "2026-10-11"

    out = _run_publish(f"pubCalendarWeek({json.dumps(_busy_week(ymd, n))})")

    assert "cal-day mini" in out                               # le palier mini reste pour garder la case lisible
    assert out.count("cal-mini-row") == n                      # aucune coupure : une boite par publication
    assert "autres" not in out and "data-pub-more" not in out and "cal-more" not in out


@_NODE
def test_publish_week_box_mini_tier_keeps_free_slots_as_full_drop_targets():
    # Un creneau libre dans un jour charge reste une cible de depot normale (.cal-c[data-slot-at]) : seules
    # les publications se tronquent en lignes minuscules, jamais les creneaux libres (TASK-460904227d2f).
    ymd = "2026-10-11"
    posts = [{"video_id": "v", "clip_id": f"{i:02d}", "publish_status": "scheduled", "screen_title": f"Clip {i}",
              "service": "tiktok", "slot_at": f"2026-10-11T{i:02d}:00:00+00:00", "slot_at_paris": f"2026-10-11T{i:02d}:00:00+02:00",
              "video_url": "/m", "thumbnail_url": "/t"} for i in range(12)]
    free = {"slot_at": "2026-10-11T22:00:00+02:00", "slot_at_paris": "2026-10-11T22:00:00+02:00", "clip": None, "free": True}
    week = {"week_start": "2026-10-05", "week_end": "2026-10-11", "slots": [free], "days": _week_days({ymd: posts})}

    out = _run_publish(f"pubCalendarWeek({json.dumps(week)})")

    assert 'class="cal-c slot free' in out and 'data-slot-at="2026-10-11T22:00:00+02:00"' in out
    assert out.count("cal-mini-row") == 12                     # les 12 posts rendus, le creneau libre en plus


@_NODE
def test_publish_week_box_compact_tier_keeps_full_post_cards_without_truncation():
    ymd = "2026-10-08"
    posts = [{"video_id": "v", "clip_id": f"{i:02d}", "publish_status": "scheduled", "screen_title": f"Clip {i}",
              "service": "tiktok", "slot_at": f"2026-10-08T{i:02d}:00:00+00:00", "slot_at_paris": f"2026-10-08T{i:02d}:00:00+02:00",
              "video_url": "/m", "thumbnail_url": "/t"} for i in range(6)]
    week = {"week_start": "2026-10-05", "week_end": "2026-10-11", "slots": [], "days": _week_days({ymd: posts})}

    out = _run_publish(f"pubCalendarWeek({json.dumps(week)})")

    assert "cal-day compact" in out and "cal-mini-row" not in out
    assert out.count('data-post="v/0') == 6                    # les 6 posts rendus, aucun tronque
    assert "autres" not in out


@_NODE
def test_publish_month_box_shows_only_the_day_number_and_the_video_count_linking_to_day_view():
    posts = lambda ymd, n: [{"video_id": "v", "clip_id": f"{i:02d}", "publish_status": "scheduled", "screen_title": f"Clip {i}",
                             "service": "tiktok", "slot_at": f"{ymd}T10:00:00+00:00", "slot_at_paris": f"{ymd}T12:00:00+02:00",
                             "video_url": "/m", "thumbnail_url": "/t"} for i in range(n)]
    counts = {"2026-10-06": 0, "2026-10-07": 1, "2026-10-08": 17}
    days = [{"date": f"2026-10-{d:02d}", "count": counts.get(f"2026-10-{d:02d}", 0),
             "posts": posts(f"2026-10-{d:02d}", counts.get(f"2026-10-{d:02d}", 0))} for d in range(1, 32)]
    month = {"range": "month", "range_start": "2026-10-01", "range_end": "2026-10-31", "slots": [], "days": days}

    out = _run_publish(f"""(() => {{
      const html = pubCalendarMonth({json.dumps(month)});
      const cell = (ymd) => html.split('<div class="cal-day ').slice(1).find((c) => c.includes('data-date="' + ymd + '"'));
      return {{ html, zero: cell("2026-10-06"), one: cell("2026-10-07"), many: cell("2026-10-08") }};
    }})()""")

    html = out["html"]
    assert "cal-mini-row" not in html and 'class="post' not in html and "data-post=" not in html   # plus aucune boite de publication
    assert "autres" not in html and "cal-more" not in html
    assert "17 vidéos" in out["many"] and 'data-pub-day="2026-10-08"' in out["many"]               # clic -> vue Jour
    assert "1 vidéo<" in out["one"] and "1 vidéos" not in out["one"] and 'data-pub-day="2026-10-07"' in out["one"]
    assert "vidéo" not in out["zero"] and "cal-day-count" not in out["zero"]                       # rien si 0
    assert ">8<" in out["many"] and ">6<" in out["zero"]                                           # le numero du jour reste


def test_publish_week_calendar_has_no_leading_empty_hour_column():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "cal-row-label" not in js and "cal-row-label" not in css        # reste de l'ancienne grille horaire


@_NODE
def test_publish_week_view_renders_exactly_seven_day_headers_and_boxes():
    week = {"week_start": "2026-10-05", "week_end": "2026-10-11", "slots": [], "days": _week_days({})}

    out = _run_publish(f"pubCalendarWeek({json.dumps(week)})")

    assert out.count('class="cal-h') == 7                      # pas de 8e cellule d'en-tete vide (colonne d'heure)
    assert out.count("data-date=") == 7


def test_publish_legend_dots_use_three_distinct_colors_reused_from_the_calendar_boxes():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    toolbar = js[js.index("function pubToolbar"):js.index("const PUB_HELP")]
    assert '<i class="pub-leg info">' in toolbar
    assert '<i class="pub-leg ok">' in toolbar
    assert '<i class="pub-leg bad">' in toolbar
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".pub-leg.info { background: var(--info)" in css
    assert ".pub-leg.ok { background: var(--ok)" in css
    assert ".pub-leg.bad { background: var(--bad)" in css


@_NODE
def test_publish_shift_month_stays_on_the_first_and_rolls_over_the_year():
    out = _run_publish("""({
      next: pubShiftMonth('2026-01-31', 1),
      prev: pubShiftMonth('2026-01-15', -1),
      rollover: pubShiftMonth('2026-12-05', 1),
    })""")
    assert out["next"] == "2026-02-01"      # jamais le 31 janvier + 1 mois ne deborde sur mars
    assert out["prev"] == "2025-12-01"
    assert out["rollover"] == "2027-01-01"  # franchit l'annee


@_NODE
def test_publish_range_label_reads_server_bounds_without_recomputing_dates():
    out = _run_publish("""({
      day: pubRangeLabel({ range: 'day', range_start: '2026-10-08' }),
      week: pubRangeLabel({ range: 'week', week_start: '2026-10-05', week_end: '2026-10-11' }),
      month: pubRangeLabel({ range: 'month', range_start: '2026-10-01' }),
      none: pubRangeLabel(null),
    })""")
    assert "8" in out["day"] and "octobre" in out["day"] and "2026" in out["day"]
    assert "2026" in out["week"] and "oct" in out["week"].lower()
    assert "octobre" in out["month"] and "2026" in out["month"] and "8" not in out["month"]
    assert out["none"] == ""


@_NODE
def test_publish_day_view_lists_time_account_service_title_and_status():
    clip = {"video_id": "v", "clip_id": "01", "publish_status": "scheduled", "screen_title": "Titre", "service": "tiktok",
            "account": "ab12cd", "slot_at": "2026-10-08T10:00:00+00:00", "slot_at_paris": "2026-10-08T12:00:00+02:00",
            "video_url": "/m", "thumbnail_url": "/t"}
    day = {"range": "day", "range_start": "2026-10-08", "range_end": "2026-10-08",
           "slots": [{"slot_at": "2026-10-08T16:00:00+02:00", "slot_at_paris": "2026-10-08T16:00:00+02:00", "clip": None, "free": True}],
           "days": [{"date": "2026-10-08", "count": 1, "posts": [clip]}]}

    out = _run_publish(f"pubCalendarDay({json.dumps(day)})")

    assert "12:00" in out and "Titre" in out and "Planifié" in out          # heure, titre, statut
    assert "data-post-account" in out                                      # compte
    assert "data-slot-at" in out and "16:00" in out                        # créneau libre : toujours une cible de dépôt en Jour


@_NODE
def test_publish_month_view_has_no_drop_targets_and_pads_leading_and_trailing_days():
    days = [{"date": f"2026-10-{d:02d}", "count": 0, "posts": []} for d in range(1, 32)]
    month = {"range": "month", "range_start": "2026-10-01", "range_end": "2026-10-31", "slots": [], "days": days}

    out = _run_publish(f"pubCalendarMonth({json.dumps(month)})")

    assert "data-slot-at" not in out                                       # pas de cible de dépôt en vue Mois (SPEC-c100)
    assert out.count('class="cal-day blank"') == 4                         # 1er octobre 2026 = jeudi : 3 avant, 1 après (31 jours)
    assert out.count('data-date="2026-10-') == 31


def test_publication_help_texts_describe_optional_slots_and_the_new_publication_flow():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    help_text = js[js.index("const PUB_HELP"):js.index("/* Zone principale")]
    assert "Nouvelle publication" in help_text and "facultatifs" in help_text
    for stale in ("télécharge, copie", "Marquer publié", "créneaux obligatoires", "télécharger + copier"):
        assert stale not in js, stale
    assert "Glisse un clip sur un créneau libre du calendrier pour le planifier" not in js


# --------------------------------------------------------------------------
# TASK-5c00d0c09c98 : « Après la dernière programmation + N h » dans « Nouvelle publication »
# --------------------------------------------------------------------------


@_NODE
def test_new_publication_form_offers_after_last_schedule_with_the_computed_date():
    base = {
        "clips": [], "accounts": [{"id": "ab12cd", "label": "Compte", "service": "tiktok", "ready_to_publish": True}],
        "options": {}, "ytOptions": {}, "minMinutes": 10, "maxDays": 10, "ytMinMinutes": 10, "ytMaxDays": 10,
        "caption": "", "tags": "", "edit": None, "selected": "",
    }
    out = _run_publish(f"""(() => {{
      const idle = pubFormHtml({json.dumps({**base, "mode": "immediate", "intervalH": 4})});
      const chosen = pubFormHtml({json.dumps({**base, "mode": "after_last", "intervalH": 3, "afterAt": None})});
      const computed = pubFormHtml({json.dumps(
        {**base, "mode": "after_last", "intervalH": 3,
         "afterAt": "2026-10-08T14:00:00+02:00"})});
      return {{ idle, chosen, computed }};
    }})()""")
    interval_tag = lambda html: re.search(r'<input[^>]*id="pub-form-interval"[^>]*>', html).group(0)
    assert 'value="after_last"' in out["idle"] and "Après la dernière programmation" in out["idle"]
    assert 'value="3"' in interval_tag(out["chosen"])
    assert "hidden" not in interval_tag(out["chosen"])
    assert "hidden" in interval_tag(out["idle"])
    assert "jeudi" in out["computed"] and "14:00" in out["computed"]              # date calculée affichée avant validation


def test_publish_js_computes_the_after_last_date_server_side_and_submits_it_as_a_scheduled_post():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    assert "/api/publications/after-last" in js and "interval_hours" in js
    assert '"after_last"' in js
    body = js[js.index("function pubFormBody"):js.index("async function pubFormSubmit")]
    assert 'mode: mode === "after_last" ? "scheduled" : mode' in body
    assert "f.afterAt" in body
    submit = js[js.index("async function pubFormSubmit"):js.index("async function pubOpenForm")]
    assert "afterAt" in submit                                                  # refus explicite si la date n'est pas encore calculée


@_NODE
def test_publish_now_and_the_renamed_declaration_of_a_post_made_outside_clipper():
    out = _run_publish("""({
      approved: pubDetailHtml({ video_id: 'v', clip_id: '01', publish_status: 'approved', screen_title: 'T', video_url: '/m', hashtags: [] }),
      scheduled: pubDetailHtml({ video_id: 'v', clip_id: '01', publish_status: 'scheduled', screen_title: 'T', video_url: '/m', hashtags: [] }),
    })""")
    assert "data-publish-now" in out["approved"] and "Publier maintenant" in out["approved"]
    assert "data-publish-now" not in out["scheduled"]
    assert "Déclarer publié (hors Clipper)" in out["scheduled"] and "Marquer publié" not in out["scheduled"]
    assert "déjà publié toi-même, hors de Clipper" in out["scheduled"]                # expliqué
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    assert "pubOpenForm({ video_id, clip_id })" in js and "Déclarer ce clip comme publié ?" in js


def test_no_static_script_formats_a_time_without_the_europe_paris_zone():
    """Toutes les heures affichées sont celles de Paris : aucun toLocale*String sans fuseau, aucun décalage fixe."""
    for path in sorted(STATIC.rglob("*.js")):
        js = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith(("//", "/*", "*")))
        for match in re.finditer(r"toLocale(?:Time|Date)?String\(([^;]*)", js):
            line = match.group(0).split("\n")[0]
            if "minimumFractionDigits" in line or "timeZone" in line:
                continue
            assert False, f"{path.name} : heure sans fuseau : {line[:120]}"
        assert "getTimezoneOffset" not in js and "+01:00" not in js and "+02:00" not in js, path.name
    assert 'const CLIPPER_TZ = "Europe/Paris"' in (STATIC / "ui.js").read_text(encoding="utf-8")


def test_every_panel_screen_keeps_its_content_off_the_edges():
    """Un écran dont [data-body] garde le cadre d'un panneau a une marge intérieure ; sinon le cadre est retiré
    (les panneaux sont alors à l'intérieur) ou ses lignes portent leur propre marge (liste des vidéos)."""
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    css = "\n".join(p.read_text(encoding="utf-8") for p in [STATIC / "style.css", *sorted((STATIC / "screens").glob("*.css"))])
    own_margin = {"videos"}  # .vadd, .vfilters et .job portent 16-24 px de marge
    screens = re.findall(r'<section class="screen" id="screen-(\w+)".*?<div class="([^"]*)" data-body', page, re.S)
    assert {name for name, _ in screens} == {"dashboard", "veille", "videos", "review", "clips", "clip", "publish", "channels", "stats", "accounts", "settings", "journal"}
    for name, classes in screens:
        if "panel" not in classes.split() or name in own_margin:
            continue
        rules = re.findall(r"(?:#screen-%s|\.screen\[id=\"screen-%s\"\]) \[data-body\] \{([^}]*)\}" % (name, name), css)
        assert any("padding" in r or "background: none" in r for r in rules), f"screen-{name} : contenu collé aux bords"




# --------------------------------------------------------------------------
# Ecran Statistiques TikTok (SPEC-86fe R3, R1) : interface
# --------------------------------------------------------------------------


def _js_function(source: str, name: str) -> str:
    """Le texte d'une fonction ou constante fleche de premier niveau (accolades equilibrees)."""
    start = source.index(f"function {name}(")
    depth, i = 0, source.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        i += 1
        if depth == 0:
            return source[start:i]


def _stats_js_run(functions: list[str], expression: str) -> object:
    stats = _static("screens", "stats.js")
    script = (
        'const fr = (n, d) => Number(n).toLocaleString("fr-FR", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });\n'
        'const esc = (s) => String(s == null ? "" : s);\n'
        'const location = { hash: process.argv[1] };\n'
        + "\n".join(_js_function(stats, name) for name in functions)
        + f"\nprocess.stdout.write(JSON.stringify({expression}));"
    )
    return json.loads(_node_run(script, "#/stats"))


def test_the_stats_screen_follows_the_mockup_account_period_scan_tabs_and_video_sheet():
    js, page = _static("screens", "stats.js"), _static("index.html")

    assert "/static/screens/stats.js" in page
    assert page.index("/static/screens.js") < page.index("/static/screens/stats.js") < page.index("/static/app.js")
    assert "Screens.stats" in js
    for route in ("/api/stats/tiktok", "/videos", "/api/stats/tiktok/refresh", "?period="):
        assert route in js
    for text in ("Compte TikTok", "Période", "Dernier relevé", "Relever maintenant",
                 "Vue d'ensemble", "Vidéos", "Spectateurs", "Engagement", "Ouvrir sur TikTok", "Voir le clip dans Clipper",
                 "Vidéo source", "publié hors Clipper", "Publié hors Clipper", "Compte non prêt à publier",
                 "Filtrer par légende", "Taux de rétention", "dès 100 vues", "Mots les plus utilisés dans les commentaires",
                 "J'aime dans le temps", "Jour sans relevé"):
        assert text in js, text
    for key in ("data-stats-account", "data-stats-period", "data-stats-metric", "data-stats-scan", "data-stats-sort",
                "data-stats-q", "data-stats-open", "data-stats-vtab", "data-stats-tiles", "data-stats-notready"):
        assert key in js, key
    assert "toastError" in js and "target=\"_blank\" rel=\"noopener noreferrer\"" in js
    for period in ("7", "28", "60", "365"):
        assert period in js[js.index("STATS_PERIODS"):js.index("STATS_PERIODS") + 40]
    for label in ("Vues de vidéo", "Vues du profil", "J'aime", "Commentaires", "Partages"):
        assert f'"{label}"' in js


def test_the_stats_screen_no_longer_has_the_csv_import_nor_the_internal_measures():
    js, page, css = _static("screens", "stats.js"), _static("index.html"), _static("style.css")
    everything = "".join(p.read_text(encoding="utf-8") for p in sorted(STATIC.rglob("*.js"))) + page

    assert "/api/stats/import" not in everything and "data-stats-import" not in everything
    assert "Importer un CSV" not in everything and "data-stats-file" not in everything
    for internal in ("llm_cost", "by_usage", "by_video", "by_day", ".steps", ".counts", "/api/measures", "Coûts du modèle de langage",
                     "Durée par étape", "Vidéos par statut"):
        assert internal not in js, internal
    assert "Résultats par clip" not in js and "stats_unmatched" not in js
    assert ".stats-import" not in css


def test_the_dashboard_now_carries_the_internal_measures():
    js = _static("screens", "dashboard.js")

    assert "/api/measures" in js and "dashMeasuresSection()" in js and "wireMeasures(body)" in js
    for block in ("Coûts du modèle de langage", "Durée par étape", "Vidéos par statut", "data-block=\"llm\"",
                  "data-block=\"steps\"", "data-block=\"counts\"", "Mesures internes"):
        assert block in js, block
    for field in ("llm_cost", "by_video", "by_usage", "by_day", "steps", "counts"):
        assert field in js
    assert "data-measures-channel" in js and "data-measures-preset" in js and "Tous les styles" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_stats_evolutions_are_signed_percentages_and_null_is_a_dash():
    out = _stats_js_run(["statsDelta"], "[statsDelta(12.5), statsDelta(-3), statsDelta(0), statsDelta(null)]")

    assert out[0] == {"cls": "up", "text": "▲ +12,5 %"}
    assert out[1] == {"cls": "down", "text": "▼ -3,0 %"}
    assert out[2] == {"cls": "flat", "text": "■ 0,0 %"}
    assert out[3] == {"cls": "flat", "text": "—"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_stats_durations_dates_and_missing_values_are_never_a_zero():
    out = _stats_js_run(["statsDuration", "statsPostedAt"], (
        '[statsDuration(65), statsDuration(0), statsDuration(null), statsPostedAt("2026-09-30T14:05:00", "x"), '
        'statsPostedAt("2026-09-30T00:00:00", null), statsPostedAt(null, "hier"), statsPostedAt(null, null)]'))

    assert out == ["1:05", "0:00", "—", "30/09 · 14:05", "30/09 · 00:00", "hier", "—"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_stats_curve_breaks_the_line_on_a_day_without_a_relevé_never_inventing_a_value():
    out = _stats_js_run(["statsLinePaths", "statsNiceMax"], (
        "[statsLinePaths([1, 2, null, 4], (i) => i * 10, (v) => 100 - v),"
        " statsLinePaths([null, null], (i) => i, (v) => v), statsLinePaths([5], (i) => 0, (v) => v),"
        " statsNiceMax(0), statsNiceMax(1180), statsNiceMax(7)]"))

    assert out[0] == "M0.0 99.0L10.0 98.0M30.0 96.0"  # le jour sans valeur coupe la ligne en deux segments
    assert out[1] == "" and out[2] == "M0.0 5.0"
    assert out[3] == 1 and out[4] >= 1180 and out[5] >= 7  # jamais un maximum nul : l'axe reste lisible


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_stats_addresses_carry_account_tab_and_video():
    script = (
        'const location = { hash: process.argv[1] };\n'
        + "\n".join(_js_function(_static("screens", "stats.js"), n) for n in ("statsRoute", "statsHref"))
        + '\nconst r = (h) => { location.hash = h; return statsRoute(); };'
        + '\nprocess.stdout.write(JSON.stringify([r("#/stats"), r("#/stats/ab12cd"), r("#/stats/ab12cd/videos"),'
        + ' r("#/stats/ab12cd/videos/730?x=1"), statsHref("ab12cd", "overview"), statsHref("ab12cd", "videos"),'
        + ' statsHref("ab12cd", "videos", "730")]));'
    )
    out = json.loads(_node_run(script, "#/stats"))

    assert out[0] == {"account": "", "tab": "overview", "post": ""}
    assert out[1] == {"account": "ab12cd", "tab": "overview", "post": ""}
    assert out[2] == {"account": "ab12cd", "tab": "videos", "post": ""}
    assert out[3] == {"account": "ab12cd", "tab": "videos", "post": "730"}
    assert out[4:] == ["#/stats/ab12cd", "#/stats/ab12cd/videos", "#/stats/ab12cd/videos/730"]


def test_stats_css_carries_the_mockup_layout():
    css = _static("style.css")

    for selector in (".ctl", ".linked", ".scan", ".kpi-btn", ".delta", ".chart .ln", ".chart .ln.prev", ".gapband",
                     ".ptable", ".thumb-p", ".figs", ".src-row", ".poster-big", ".video-sheet", ".dlinks", ".sort-m"):
        assert selector in css, selector
    assert "@media (max-width: 720px)" in css and ".ptable thead { display: none; }" in css  # liste en cartes sur mobile


# --------------------------------------------------------------------------
# Radar : « retenu puis écarté au découpage » + calendrier lisible (TASK-fc6f)
# --------------------------------------------------------------------------

_CUT_REASON = "duree 41.8 s : ni clip unique (60-120 s) ni 2 a 12 parties"


def _write_parts_rejected(tmp_path, rejected):
    _write_json(tmp_path / "workspace" / JURY_VID / "parts.json",
                {"video_id": JURY_VID, "moments": [], "rejected": rejected})


def test_api_jury_marks_the_moment_rejected_by_parts_with_its_reason(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)
    _write_parts_rejected(tmp_path, [{"id": 0, "start": 10.0, "end": 40.0, "duration": 30.0, "reason": _CUT_REASON}])

    moments = _jury(tmp_path)["moments"]

    assert moments[0]["cut_rejected"] == _CUT_REASON
    assert moments[0]["reason_kind"] == "decoupage"
    assert moments[0]["retained"] is True                      # retenu par le jury : le fait reste visible
    assert "cut_rejected" not in moments[1] and moments[1]["reason_kind"] == "exploration"


def test_api_jury_without_parts_json_is_unchanged(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)

    moments = _jury(tmp_path)["moments"]

    assert all("cut_rejected" not in m for m in moments)      # sortie inchangée : aucune clé nouvelle
    assert [m["reason_kind"] for m in moments] == ["retenu", "exploration", "score", "plafond", "veto"]


def test_api_jury_ignores_parts_rejected_ids_of_moments_not_retained(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)
    _write_parts_rejected(tmp_path, [{"id": 99, "start": 0, "end": 1, "duration": 1, "reason": "x"}])

    assert all("cut_rejected" not in m for m in _jury(tmp_path)["moments"])


def test_api_jury_unreadable_parts_json_is_an_explicit_error(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)
    path = tmp_path / "workspace" / JURY_VID / "parts.json"
    path.write_text("{pas du json", encoding="utf-8")

    resp = client(tmp_path).get(f"/api/videos/{JURY_VID}/jury")

    assert resp.status_code == 500 and "parts.json" in resp.json()["detail"]


def test_radar_panel_shows_cut_rejected_distinct_from_a_real_retained(tmp_path, isolated_cwd):
    _write_jury_moments(tmp_path)
    _write_parts_rejected(tmp_path, [{"id": 0, "start": 10.0, "end": 40.0, "duration": 30.0, "reason": _CUT_REASON}])
    payload = _jury(tmp_path)
    js = _radar_js()
    assert "écarté au découpage" in js

    script = js + (
        "\nconst data = JSON.parse(process.argv[1]);"
        "\nprocess.stdout.write(juryPanelHtml(data, { key: data.moments[0].key, round: null }));"
    )
    html = _node_run(script, json.dumps(payload))

    assert "Retenu par le jury, écarté au découpage" in html
    assert _CUT_REASON in html
    assert html.count("écarté au découpage") >= 2              # liste et détail


def test_calendar_card_has_full_title_attribute_and_two_line_clamp():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    card = js[js.index("function pubPost"):js.index("function pubCalendar")]
    assert 'title="${esc(pubTitle(c))}"' in card               # titre complet en info-bulle sur la carte
    import re
    rules = re.findall(r"\.cal \.post \.pt\s*\{([^}]*)\}", css)
    assert rules, "règle .cal .post .pt absente de style.css"
    assert "-webkit-line-clamp: 2" in rules[-1] and "overflow-wrap" in rules[-1]
    assert "line-clamp: 2" in rules[-1].replace("-webkit-line-clamp", "")


def test_calendar_card_layout_gives_the_title_the_width():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    import re
    post = " ".join(re.findall(r"\.cal \.post\s*\{([^}]*)\}", css))
    assert "flex-wrap: wrap" in post                           # la pastille de compte passe à la ligne
    assert re.search(r"\.cal \.post \.mini-clip\s*\{[^}]*display: none", css)  # pas de miniature de 18 px
    assert re.search(r"\.cal \.post \.pt\s*\{[^}]*flex: 1 1 100%", css)


# --------------------------------------------------------------------------
# Statistiques TikTok : releve seulement a l'usage (SPEC-47e2 R4), posts supprimes
# --------------------------------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402
from datetime import timedelta  # noqa: E402


class SlowFetch(FakeFetch):
    """Un releve qui dure : bloque jusqu'a ``release()`` ; ``started`` dit qu'il a commence."""

    def __init__(self, error=None):
        super().__init__(error)
        self.started, self.gate = threading.Event(), threading.Event()

    def __call__(self, account, *, config=None, **kwargs):
        self.started.set()
        assert self.gate.wait(10), "le releve n'a jamais ete libere"
        return super().__call__(account, config=config, **kwargs)

    def release(self):
        self.gate.set()


def _tt_fresh_snapshot(tmp_path, *, minutes_ago, account=TT_ACCOUNT, posts=()):
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    _write_json(tmp_path / "state" / "stats" / "tiktok" / account / f"{at.strftime('%Y%m%dT%H%M%S%f')}Z.json",
                {"account": account, "fetched_at": at.isoformat(), "source": "tiktok_studio", "origin": "full",
                 "overview": None, "posts": list(posts)})


def _tt_client(tmp_path, *, stale_min=None):
    """Un seul client (donc une seule application) : le verrou des releves vit dans l'application."""
    sections = {} if stale_min is None else {"tiktok": {"stats_stale_min": stale_min}}
    return TestClient(create_app(config=Config(mode="review", workspace_dir=tmp_path / "workspace",
                                               output_dir=tmp_path / "output", _sections=sections)))


def _tt_open(web, account=TT_ACCOUNT):
    return web.post("/api/stats/tiktok/open", json={"account": account})


def _tt_account_state(web, account=TT_ACCOUNT):
    return {a["account"]: a for a in web.get("/api/stats/tiktok").json()["accounts"]}[account]


def _tt_wait_idle(web, account=TT_ACCOUNT):
    for _ in range(100):
        found = _tt_account_state(web, account)
        if not found["refreshing"]:
            return found
        time.sleep(0.05)
    raise AssertionError("le releve en tache de fond ne se termine pas")


def test_opening_the_stats_screen_starts_a_background_fetch_when_the_last_one_is_stale(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path)
    _tt_fresh_snapshot(tmp_path, minutes_ago=61)
    fetch = SlowFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)
    web = _tt_client(tmp_path)

    resp = _tt_open(web)

    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] is True and resp.json()["running"] is True and resp.json()["stale"] is True
    assert fetch.started.wait(5)  # en tache de fond : la reponse n'a pas attendu la fin
    assert _tt_account_state(web)["refreshing"] is True
    fetch.release()
    assert _tt_wait_idle(web)["refreshing"] is False
    assert fetch.calls == [TT_ACCOUNT]


def test_opening_the_stats_screen_with_a_fresh_snapshot_fetches_nothing(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path)
    _tt_fresh_snapshot(tmp_path, minutes_ago=30)
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)

    resp = _tt_open(_tt_client(tmp_path))

    assert resp.status_code == 200 and resp.json() == {
        "account": TT_ACCOUNT, "started": False, "running": False, "stale": False, "reason": None}
    assert fetch.calls == []


def test_a_never_fetched_account_is_stale_and_stats_stale_min_zero_disables_the_opening_fetch(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path)
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)

    assert _tt_open(_tt_client(tmp_path, stale_min=0)).json()["started"] is False and fetch.calls == []
    web = _tt_client(tmp_path)
    assert _tt_open(web).json()["started"] is True  # jamais releve : perime
    _tt_wait_idle(web)
    assert fetch.calls == [TT_ACCOUNT]


def test_two_simultaneous_opens_start_a_single_fetch_and_the_second_gets_the_running_state(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path)
    fetch = SlowFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)
    web = _tt_client(tmp_path)

    first = _tt_open(web)
    assert fetch.started.wait(5)
    second = _tt_open(web)
    manual = web.post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT})  # « Relever maintenant » aussi

    assert first.json()["started"] is True
    assert second.status_code == 200 and second.json()["running"] is True and second.json()["started"] is False
    assert manual.status_code == 200 and manual.json()["accounts"][TT_ACCOUNT]["running"] is True
    fetch.release()
    _tt_wait_idle(web)
    assert fetch.calls == [TT_ACCOUNT]  # un seul releve pour les trois demandes


def test_the_lock_is_per_account_and_released_once_the_fetch_is_over(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path, ready=(TT_ACCOUNT, TT_OTHER))
    fetch = SlowFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)
    web = _tt_client(tmp_path)

    assert _tt_open(web, TT_ACCOUNT).json()["started"] is True
    assert _tt_open(web, TT_OTHER).json()["started"] is True
    fetch.release()
    _tt_wait_idle(web, TT_ACCOUNT)
    _tt_wait_idle(web, TT_OTHER)
    assert sorted(fetch.calls) == [TT_ACCOUNT, TT_OTHER]

    sync = web.post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT})  # verrou libere : relance possible
    assert sync.status_code == 200 and sync.json()["accounts"][TT_ACCOUNT]["posts"] == 2
    assert fetch.calls.count(TT_ACCOUNT) == 2


def test_a_failed_manual_fetch_frees_the_lock(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path)
    fetch = FakeFetch(error=tiktok_mod.TikTokStop("captcha", "captcha détecté", None))
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)
    web = _tt_client(tmp_path)

    assert web.post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT}).status_code == 409
    assert web.post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT}).status_code == 409  # relance, pas « en cours »
    assert fetch.calls == [TT_ACCOUNT, TT_ACCOUNT] and _tt_account_state(web)["refreshing"] is False


def test_opening_the_stats_screen_of_an_account_not_ready_fetches_nothing_and_says_why(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path, ready=(TT_ACCOUNT,))
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)
    web = _tt_client(tmp_path)

    resp = _tt_open(web, TT_OTHER)

    assert resp.status_code == 200 and resp.json()["started"] is False and "expirée" in resp.json()["reason"]
    assert fetch.calls == []
    assert _tt_open(web, "inconnu").status_code == 404
    assert web.post("/api/stats/tiktok/open", json={"autre": 1}).status_code == 422
    assert web.post("/api/stats/tiktok/open").status_code == 422
    assert web.post("/api/stats/tiktok/open", json={"account": "../x"}).status_code == 422


@pytest.mark.parametrize("error,words", [
    (tiktok_mod.TikTokStop("captcha", "captcha détecté : arrêt immédiat", None), "captcha"),
    (tiktok_mod.TikTokError("réglage invalide"), "réglage"),
    (RuntimeError("boom"), "boom"),
])
def test_a_background_fetch_that_fails_is_reported_on_the_account_and_frees_the_lock(tmp_path, isolated_cwd, monkeypatch, error, words, caplog):
    _tt_accounts(tmp_path)
    monkeypatch.setattr(tiktok_mod, "fetch_stats", FakeFetch(error=error))
    web = _tt_client(tmp_path)

    with caplog.at_level("ERROR"):
        assert _tt_open(web).json()["started"] is True
        found = _tt_wait_idle(web)

    assert words in found["refresh_error"] and found["refreshing"] is False
    assert words in caplog.text  # journalise (ADR-ad2e)
    assert _tt_open(web).json()["started"] is True  # verrou libere : une nouvelle ouverture relance
    _tt_wait_idle(web)


def test_the_accounts_list_says_whether_the_snapshot_is_stale(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    _tt_fresh_snapshot(tmp_path, minutes_ago=90)

    accounts = {a["account"]: a for a in _tt_get(tmp_path, "").json()["accounts"]}

    assert accounts[TT_ACCOUNT]["stale"] is True and accounts[TT_ACCOUNT]["refreshing"] is False
    assert accounts[TT_ACCOUNT]["refresh_error"] is None


def test_a_post_deleted_on_tiktok_disappears_from_the_videos_and_the_sheet_but_not_from_the_history(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path)
    _tt_snapshot(tmp_path, 1, posts=[_tt_post(TT_ID_A, "Un"), _tt_post(TT_ID_B, "Deux")])
    _tt_snapshot(tmp_path, 2, posts=[_tt_post(TT_ID_A, "Un")])

    videos = _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos").json()["videos"]

    assert [v["post_id"] for v in videos] == [TT_ID_A]
    assert _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos/{TT_ID_B}").status_code == 404
    assert _tt_get(tmp_path, f"/{TT_ACCOUNT}/videos/{TT_ID_A}").status_code == 200
    assert len(list((tmp_path / "state" / "stats" / "tiktok" / TT_ACCOUNT).glob("*.json"))) == 2  # historique intact


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_stats_count_restricted_videos_among_those_online_and_never_guess_unknown_ones():
    out = _stats_js_run(["statsRestriction"], (
        'statsRestriction([{fyf_eligible: false}, {fyf_eligible: true}, {fyf_eligible: true}, '
        '{fyf_eligible: null}, {}])'))
    assert out == {"restricted": 1, "online": 3}  # programmee (null) ou jamais relevee : ni restreinte ni en ligne
    assert _stats_js_run(["statsRestriction"], "statsRestriction([])") == {"restricted": 0, "online": 0}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_stats_restricted_chip_and_account_summary_are_rendered():
    js = _static("screens", "stats.js")
    assert "Restreinte : pas dans Pour toi" in js and "en ligne</span>" in js
    assert "fyf_eligible === false" in js and "fyf_notice" in js  # pastille par post + texte de TikTok dans la fiche
    chip = _stats_js_run(["statsRestrictedChip"], '(globalThis.icon = () => "", statsRestrictedChip({fyf_eligible: false}))')
    assert "Restreinte : pas dans Pour toi" in chip
    assert _stats_js_run(["statsRestrictedChip"], '(globalThis.icon = () => "", statsRestrictedChip({fyf_eligible: true}))') == ""
    assert _stats_js_run(["statsRestrictedChip"], '(globalThis.icon = () => "", statsRestrictedChip({fyf_eligible: null}))') == ""
    one = _stats_js_run(["statsRestriction", "statsRestrictionSummary"], 'statsRestrictionSummary([{fyf_eligible: false}, {fyf_eligible: true}])')
    assert "1 vidéo restreinte sur 2 en ligne" in one
    many = _stats_js_run(["statsRestriction", "statsRestrictionSummary"], 'statsRestrictionSummary([{fyf_eligible: false}, {fyf_eligible: false}, {fyf_eligible: true}])')
    assert "2 vidéos restreintes sur 3 en ligne" in many
    assert "0 vidéo restreinte sur 1 en ligne" in _stats_js_run(["statsRestriction", "statsRestrictionSummary"], 'statsRestrictionSummary([{fyf_eligible: true}])')
    assert _stats_js_run(["statsRestriction", "statsRestrictionSummary"], 'statsRestrictionSummary([{fyf_eligible: null}, {}])') == ""  # rien de relevé


def test_the_stats_screen_triggers_the_opening_fetch_and_shows_the_running_state():
    js = (Path(__file__).resolve().parent.parent / "clipper" / "web" / "static" / "screens" / "stats.js").read_text(encoding="utf-8")
    assert "/api/stats/tiktok/open" in js and "refreshing" in js and "Relevé en cours" in js
    assert "Relever maintenant" in js and "/api/stats/tiktok/refresh" in js


def test_the_modified_stats_script_is_valid_javascript():
    if shutil.which("node") is None:
        pytest.skip("node absent du PATH")
    path = Path(__file__).resolve().parent.parent / "clipper" / "web" / "static" / "screens" / "stats.js"
    done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr


# --------------------------------------------------------------------------
# TASK-0247 : « Chaîne » devient « Style » dans ce que l'utilisateur voit ; #/chaines redirige
# --------------------------------------------------------------------------

_NODE = shutil.which("node")


def _strip_comments(text: str, suffix: str) -> str:
    if suffix == ".html":
        return re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if suffix == ".js":
        text = re.sub(r"(?m)(^|[\s;,)}{])//[^\n]*", r"\1", text)
    return text


def test_no_visible_chaine_wording_is_left_in_the_console():
    allowed = ("ma_chaine", "_sans_chaine", "(chaines|channels)")            # exemple neutre, identifiants, ancienne URL
    found = []
    for path in sorted(STATIC.rglob("*")):
        if path.suffix not in (".js", ".html", ".css"):
            continue
        text = _strip_comments(path.read_text(encoding="utf-8"), path.suffix)
        for token in allowed:
            text = text.replace(token, "")
        found += [f"{path.relative_to(STATIC)}: {m.group(0)!r}" for m in re.finditer(r"(?i).{0,25}(?<![a-z])cha[iî]nes?.{0,25}", text)]
    assert not found, "libellés « chaîne » restants :\n" + "\n".join(found)


def test_the_menu_says_styles_and_the_styles_screen_has_a_styles_url():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    channels_js = (STATIC / "screens" / "channels.js").read_text(encoding="utf-8")

    assert re.search(r'<a href="#/styles"[^>]*data-screen="channels"[^>]*>.*?<span>Styles</span>', page)
    assert 'data-title="Styles"' in page
    assert "#/styles" in channels_js and "#/channels" not in channels_js


@pytest.mark.skipif(_NODE is None, reason="node absent du PATH")
def test_the_old_chaines_and_channels_urls_redirect_to_styles(tmp_path):
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    start, end = app_js.index("/* >>> hash-legacy */"), app_js.index("/* <<< hash-legacy */")
    script = tmp_path / "hash_legacy.js"
    script.write_text(
        app_js[start:end]
        + "\nconst cases = {'#/chaines': '#/styles', '#/chaines/ma_chaine': '#/styles/ma_chaine',"
          " '#/channels': '#/styles', '#/channels/ma_chaine/layout': '#/styles/ma_chaine/layout',"
          " '#/styles': null, '#/publish': null, '': null};\n"
          "const bad = Object.entries(cases).filter(([k, v]) => legacyHash(k) !== v);\n"
          "console.log(JSON.stringify(bad));\n", encoding="utf-8")

    out = subprocess.run([_NODE, str(script)], capture_output=True, text=True, encoding="utf-8", check=True)

    assert json.loads(out.stdout) == []
    assert "legacyHash(location.hash)" in app_js and "SCREEN_IDS" in app_js


@pytest.mark.skipif(_NODE is None, reason="node absent du PATH")
@pytest.mark.parametrize("path", sorted(STATIC.rglob("*.js")), ids=lambda p: p.name)
def test_every_console_script_passes_node_check(path):
    out = subprocess.run([_NODE, "--check", str(path)], capture_output=True, text=True, encoding="utf-8")

    assert out.returncode == 0, out.stderr


def test_a_tiktok_account_without_style_is_listed_and_readable_in_the_stats_screen(tmp_path, isolated_cwd):
    _tt_accounts(tmp_path, channel=False)                                     # aucun preset : le compte n'est lié à aucun style
    web = _tt_client(tmp_path)

    found = _tt_account_state(web)
    overview = web.get(f"/api/stats/tiktok/{TT_ACCOUNT}")

    assert "channel" not in found and found["account"] == TT_ACCOUNT        # présent, pas masqué faute de style
    assert overview.status_code == 200 and "channel" not in overview.json()


# --------------------------------------------------------------------------
# TASK-9776 : l'ecran Publication propose les comptes YouTube et leurs reglages (SPEC-5e50 R2)
# --------------------------------------------------------------------------

YT_ACCOUNT = "cd56ef"


def _youtube_accounts_state(tmp_path, *, yt_ready=True):
    """READY (TikTok) et YT_ACCOUNT (YouTube) : deux services dans la meme liste de comptes."""
    rows = [{"id": READY, "label": "Compte exemple", "platform": "TikTok", "ready_to_publish": True},
            {"id": YT_ACCOUNT, "label": "ma_chaine", "service": "youtube", "ready_to_publish": yt_ready,
             "login": {"state": "connected", "checked_at": "2026-10-03T10:00:00+00:00", "expires_at": None,
                       "channel": {"name": "Ma Chaîne", "id": "UCabcdefghijklmnopqrstuv"}}}]
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "accounts.json").write_text(json.dumps({"accounts": rows}), encoding="utf-8")


def _yt_setup(tmp_path, **kwargs):
    _publish_setup(tmp_path)
    _youtube_accounts_state(tmp_path, **kwargs)


def test_the_publication_screen_lists_the_accounts_of_both_services_and_the_youtube_settings(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)

    data = _pub_client(tmp_path).get("/api/publications").json()

    accounts = {a["id"]: a for a in data["accounts"]}
    assert (accounts[READY]["service"], accounts[READY]["service_label"]) == ("tiktok", "TikTok")
    assert (accounts[YT_ACCOUNT]["service"], accounts[YT_ACCOUNT]["service_label"]) == ("youtube", "YouTube")
    assert accounts[YT_ACCOUNT]["ready_to_publish"] is True
    yt = data["defaults"]["youtube"]
    assert yt["options"] == {"visibility": "public", "made_for_kids": False}  # publique, pas pour les enfants
    assert yt["schedule_min_minutes"] >= 0 and yt["schedule_max_days"] >= 1
    assert data["defaults"]["options"]["visibility"] == "public" and "allow_comments" in data["defaults"]["options"]


def test_the_youtube_defaults_follow_the_youtube_section(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)
    config = Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                    _sections={"youtube": {"visibility": "unlisted", "made_for_kids": True}})

    data = TestClient(create_app(config=config)).get("/api/publications").json()

    assert data["defaults"]["youtube"]["options"] == {"visibility": "unlisted", "made_for_kids": True}


def test_an_invalid_youtube_setting_is_an_explicit_422_on_the_publication_screen(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)
    config = Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                    _sections={"youtube": {"visibility": "secrète"}})

    resp = TestClient(create_app(config=config)).get("/api/publications")

    assert resp.status_code == 422 and "visibility" in resp.json()["detail"]


def test_a_youtube_publication_is_created_with_the_youtube_options_and_service(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)
    options = {"title": "Mon titre", "visibility": "unlisted", "made_for_kids": False}

    resp = _pub_client(tmp_path).post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": YT_ACCOUNT, "mode": "immediate", "options": options})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["account"] == YT_ACCOUNT and body["service"] == "youtube" and body["post_options"] == options
    entry = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))[0]
    assert entry["service"] == "youtube" and entry["post_options"] == options


def test_a_scheduled_youtube_publication_keeps_the_date(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)
    when = _soon(days=3)

    resp = _pub_client(tmp_path).post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": YT_ACCOUNT, "mode": "scheduled", "publish_at": when})

    assert resp.status_code == 201, resp.text
    assert resp.json()["slot_at"] == when and resp.json()["publish_mode"] == "scheduled"


@pytest.mark.parametrize("options,status,word", [
    ({"allow_comments": False}, 409, "allow_comments"),   # reglage TikTok sur un compte YouTube
    ({"visibility": "friends"}, 409, "visibility"),       # visibilite TikTok
    ({"title": " "}, 409, "title"),
    ({"made_for_kids": "non"}, 409, "made_for_kids"),
])
def test_invalid_youtube_options_are_refused_with_the_reason(tmp_path, isolated_cwd, options, status, word):
    _yt_setup(tmp_path)

    resp = _pub_client(tmp_path).post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": YT_ACCOUNT, "mode": "immediate", "options": options})

    assert resp.status_code == status and word in resp.json()["detail"]
    assert _publications(tmp_path) == []


def test_a_private_youtube_publication_cannot_be_scheduled(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)

    resp = _pub_client(tmp_path).post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": YT_ACCOUNT, "mode": "scheduled",
        "publish_at": _soon(days=2), "options": {"visibility": "private"}})

    assert resp.status_code == 409 and "programm" in resp.json()["detail"]


def test_a_youtube_account_not_ready_is_refused_like_a_tiktok_one(tmp_path, isolated_cwd):
    _yt_setup(tmp_path, yt_ready=False)

    resp = _pub_client(tmp_path).post("/api/publications", json={
        "video_id": CLIPS_VIDEO, "clip_id": "01", "account": YT_ACCOUNT, "mode": "immediate"})

    assert resp.status_code == 409 and "non prêt à publier" in resp.json()["detail"]


def test_a_youtube_publication_uses_the_youtube_caps(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)
    c = _pub_client(tmp_path, max_posts_per_day=1, min_gap_minutes=480)  # plafonds TikTok serres : sans effet sur YouTube
    first = c.post("/api/publications", json={"video_id": CLIPS_VIDEO, "clip_id": "01", "account": YT_ACCOUNT, "mode": "immediate"})
    later = c.post("/api/publications", json={"video_id": CLIPS_VIDEO, "clip_id": "02", "account": YT_ACCOUNT,
                                              "mode": "scheduled", "publish_at": _soon(hours=5)})
    refused = c.post("/api/publications", json={"video_id": CLIPS_VIDEO, "clip_id": "03", "account": YT_ACCOUNT,
                                                "mode": "scheduled", "publish_at": _soon(hours=1)})

    assert first.status_code == 201 and later.status_code == 201, (first.text, later.text)
    assert refused.status_code == 409 and "120 minutes" in refused.json()["detail"] and refused.json()["next_at"]


def test_editing_a_youtube_publication_keeps_the_youtube_validation(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)
    c = _pub_client(tmp_path)
    c.post("/api/publications", json={"video_id": CLIPS_VIDEO, "clip_id": "01", "account": YT_ACCOUNT, "mode": "immediate"})

    ok = c.patch(f"/api/publications/{CLIPS_VIDEO}/01", json={"options": {"visibility": "private", "title": "T"}})
    bad = c.patch(f"/api/publications/{CLIPS_VIDEO}/01", json={"options": {"allow_comments": False}})

    assert ok.status_code == 200, ok.text
    assert ok.json()["post_options"] == {"visibility": "private", "title": "T"} and ok.json()["service"] == "youtube"
    assert bad.status_code == 409 and "allow_comments" in bad.json()["detail"]


def test_a_publication_scheduled_on_youtube_shows_its_status_url_and_service(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)
    url = "https://youtube.com/shorts/OOOeOwbvu34"
    when = _soon(days=2)
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "published", account=YT_ACCOUNT, service="youtube", tiktok_state="scheduled_on_youtube",
               tiktok_publish_at=when, post_url=url, post_id="OOOeOwbvu34", published_at=_soon())])

    row = _publications(tmp_path)[0]

    assert row["tiktok_status"] == "scheduled_on_youtube" and row["tiktok_live"] is False
    assert (row["post_url"], row["post_id"], row["service"]) == (url, "OOOeOwbvu34", "youtube")


def test_a_published_youtube_short_is_live_with_its_url(tmp_path, isolated_cwd):
    _yt_setup(tmp_path)
    url = "https://youtube.com/shorts/OOOeOwbvu34"
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "published", account=YT_ACCOUNT, service="youtube", tiktok_state="published",
               tiktok_publish_at=_soon(minutes=-5), post_url=url, post_id="OOOeOwbvu34", published_at=_soon(minutes=-5))])

    row = _publications(tmp_path)[0]

    assert row["tiktok_status"] == "published" and row["tiktok_live"] is True and row["post_url"] == url


def test_the_publication_form_has_the_youtube_settings_and_the_service_in_the_account_label():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")

    for marker in ("pub-form-youtube", "pub-form-yt-title", "pub-form-yt-visibility", "pub-form-yt-kids",
                   "pub-form-tiktok", "pubFormService", "pubAccountText", "data.defaults.youtube"):
        assert marker in js, marker
    for label in ("Titre YouTube", "Non répertoriée", "Conçue pour les enfants", "Programmée sur YouTube", "#Shorts"):
        assert label in js, label


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_the_account_label_carries_the_service_and_a_youtube_schedule_is_shown_as_such():
    out = _run_publish("""(() => {
      const yt = { publish_status: 'published', service: 'youtube', tiktok_status: 'scheduled_on_youtube', tiktok_live: false,
        tiktok_publish_at: '2026-10-02T10:30:00+00:00', published_at_paris: '2026-10-01T22:55:00+02:00', slot_at_paris: '2026-10-01T22:55:00+02:00' };
      return {
        yt: pubAccountText({ id: 'a', label: 'ma_chaine', service: 'youtube', service_label: 'YouTube' }),
        tt: pubAccountText({ id: 'b', label: 'Compte exemple', service: 'tiktok' }),
        bare: pubAccountText({ id: 'c', label: 'Sans service' }),
        chip: pubChip(yt), line: pubDoneLine(yt), live: pubChip({ ...yt, tiktok_live: true }),
        hintYt: pubWhenHint({ minMinutes: 15, maxDays: 10, ytMinMinutes: 15, ytMaxDays: 30 }, 'youtube'),
      };
    })()""")
    assert out["yt"] == "ma_chaine (YouTube)" and out["tt"] == "Compte exemple (TikTok)" and out["bare"] == "Sans service"
    assert "Programmée sur YouTube" in out["chip"] and "Publiée" not in out["chip"]
    assert out["line"].startswith("programmée sur YouTube, en ligne le") and "Publiée" in out["live"]
    assert "YouTube programme la vidéo" in out["hintYt"] and "30 jours" in out["hintYt"]


# --- SPEC-6076 R2 : l'écran Comptes porte les créneaux (éditeur ajout / suppression, relu pour l'enregistrement) -----


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_the_accounts_screen_renders_reads_and_summarises_the_slots_of_an_account():
    names = ["accDayLabel", "accSlots", "accSlotsEditor", "accReadSlots"]
    out = _run_js([("screens/accounts.js", names)], """(() => {
      globalThis.ACC_DAYS = [['mon', 'Lundi'], ['fri', 'Vendredi']]; globalThis.ACC_DEFAULT_TZ = 'Europe/Paris';
      const slots = [{ day: 'mon', time: '18:30' }, { day: 'fri', time: '09:00' }];
      const rows = slots.map((s) => ({ querySelector: (q) => ({ value: q.includes('day') ? s.day : s.time }) }));
      globalThis.$$ = () => rows; globalThis.$ = (q, row) => row.querySelector(q);
      return { editor: accSlotsEditor(slots), empty: accSlotsEditor([]), read: accReadSlots({}),
        summary: accSlots({ slots, timezone: 'UTC' }), none: accSlots({ slots: [] }) };
    })()""")
    assert out["editor"].count('data-slot-del') == 2 and out["editor"].count("data-slot-day") == 2
    assert 'value="18:30"' in out["editor"] and 'value="09:00"' in out["editor"] and "data-slot-add" in out["editor"]
    assert "Aucun créneau" in out["empty"] and "data-slot-add" in out["empty"]
    assert out["read"] == [{"day": "mon", "time": "18:30"}, {"day": "fri", "time": "09:00"}]
    assert "Lundi 18:30" in out["summary"] and "Vendredi 09:00" in out["summary"] and "(UTC)" in out["summary"]
    assert "aucun" in out["none"]


# --------------------------------------------------------------------------
# TASK-5bbf : série programmée (SPEC-1ed3, SPEC-6076 R3/R6) : GET .../series/clips,
# POST .../series/preview, POST .../series (création tout ou rien)
# --------------------------------------------------------------------------


def _series_client(tmp_path, ready=(READY, SPARE), approved_account=READY, **tiktok_settings):
    """TASK-16eeaccfaf09 : le mode auto ne prend que des clips deja valides (approuves, sans creneau) pour le
    compte choisi ; les 6 clips de ``_publish_setup`` sont donc approuves pour ``approved_account`` par defaut
    (``None`` pour les tests qui veulent des clips prets mais non valides)."""
    _publish_setup(tmp_path)
    _accounts_state(tmp_path, ready=ready)
    if approved_account is not None:
        _write_publish(tmp_path, "ma_chaine", [
            _entry(cid, "approved", account=approved_account) for cid in ("01", "02", "03", "04", "05", "06")
        ])
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                 _sections={"tiktok": {"max_posts_per_day": 10, "min_gap_minutes": 0, **tiktok_settings}})


def _series_scored(tmp_path, scores):
    """Re-ecrit le score de chaque clip existant (``_publish_setup`` les met tous a 80)."""
    for clip_id, score in scores.items():
        path = tmp_path / "output" / CLIPS_VIDEO / f"{clip_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["score"] = score
        path.write_text(json.dumps(data), encoding="utf-8")


def test_series_clips_endpoint_groups_series_and_gives_the_default_interval(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    c = TestClient(create_app(config=config))

    resp = c.get("/api/publications/series/clips")

    assert resp.status_code == 200
    data = resp.json()
    assert {u["clip_id"] for u in data["units"]} == {"01", "02", "03", "04", "05", "06"}
    assert data["default_interval_h"] == 4
    assert all(u["parts_total"] == 1 for u in data["units"])


def test_series_preview_picks_the_best_clips_shows_their_paris_dates_and_is_ok(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    _series_scored(tmp_path, {"01": 95, "02": 90, "03": 50, "04": 40, "05": 30, "06": 20})
    c = TestClient(create_app(config=config))
    start = _soon(hours=1)

    resp = c.post("/api/publications/series/preview", json={
        "mode": "auto", "account": READY, "interval_hours": 2, "start_at": start, "count": 2})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [it["clip_id"] for it in data["items"]] == ["01", "02"]
    assert data["ok"] is True and data["insufficient"] is False
    assert all(it["publish_at_paris"] for it in data["items"])
    # rien cree : les clips restent 'approved' (deja valides avant l'appel), un aperçu ne programme rien
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert {e["status"] for e in entries} == {"approved"}


def test_series_create_schedules_the_n_best_clips_two_hours_apart(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    _series_scored(tmp_path, {"01": 95, "02": 90, "03": 50, "04": 40, "05": 30, "06": 20})
    c = TestClient(create_app(config=config))
    start = _soon(hours=1)

    resp = c.post("/api/publications/series", json={
        "mode": "auto", "account": READY, "interval_hours": 2, "start_at": start, "count": 3})

    assert resp.status_code == 201, resp.text
    assert resp.json() == {"created": 3}
    all_entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    entries = sorted((e for e in all_entries if e["status"] == "scheduled"), key=lambda e: e["slot_at"])
    assert [e["clip_id"] for e in entries] == ["01", "02", "03"]
    assert [e["account"] for e in entries] == [READY, READY, READY]
    assert entries[1]["slot_at"] == (_dt.fromisoformat(start) + _td(hours=2)).isoformat()
    # les clips 04-06, restes 'approved' (non choisis par le score), ne sont pas touches
    assert {e["clip_id"]: e["status"] for e in all_entries if e["clip_id"] not in ("01", "02", "03")} == {
        "04": "approved", "05": "approved", "06": "approved"}


def test_series_preview_reports_insufficient_clips_without_creating_anything(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    c = TestClient(create_app(config=config))

    resp = c.post("/api/publications/series/preview", json={
        "mode": "auto", "account": READY, "interval_hours": 1, "start_at": _soon(hours=1), "count": 50})

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False and data["insufficient"] is True and data["available"] == 6
    assert "6" in data["insufficient_reason"] and "50" in data["insufficient_reason"]


def test_series_create_is_all_or_nothing_on_a_cap_violation(tmp_path, isolated_cwd):
    # écart minimal de 2 h pour une série toutes les heures : violation quelle que soit l'heure du test
    # (un plafond « par jour » échouait vers minuit, quand les 2 dates tombent sur 2 jours différents).
    config = _series_client(tmp_path, min_gap_minutes=120)
    c = TestClient(create_app(config=config))

    resp = c.post("/api/publications/series", json={
        "mode": "auto", "account": READY, "interval_hours": 1, "start_at": _soon(hours=1), "count": 2})

    assert resp.status_code == 409
    # tout ou rien : les clips restent 'approved' (deja valides avant l'appel), rien n'est passe en 'scheduled'
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert {e["status"] for e in entries} == {"approved"}


def test_series_create_refuses_an_account_that_is_not_ready(tmp_path, isolated_cwd):
    config = _series_client(tmp_path, ready=(READY,))
    c = TestClient(create_app(config=config))

    resp = c.post("/api/publications/series", json={
        "mode": "auto", "account": SPARE, "interval_hours": 1, "start_at": _soon(hours=1), "count": 1})

    assert resp.status_code == 409 and "prêt" in resp.json()["detail"]


def test_series_preview_manual_mode_follows_the_chosen_order(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    c = TestClient(create_app(config=config))
    start = _soon(hours=1)

    resp = c.post("/api/publications/series/preview", json={
        "mode": "manual", "account": READY, "interval_hours": 1, "start_at": start,
        "selection": [{"video_id": CLIPS_VIDEO, "clip_id": "05"}, {"video_id": CLIPS_VIDEO, "clip_id": "02"}]})

    assert resp.status_code == 200, resp.text
    assert [it["clip_id"] for it in resp.json()["items"]] == ["05", "02"]


def test_series_preview_filters_by_style(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    _write_state(tmp_path, "othervideo01", channel="autre")
    _write_clip(tmp_path, "othervideo01", {**_clip_sidecar("01"), "video_id": "othervideo01", "score": 99})
    c = TestClient(create_app(config=config))

    resp = c.post("/api/publications/series/preview", json={
        "mode": "auto", "style": "ma_chaine", "account": READY, "interval_hours": 1,
        "start_at": _soon(hours=1), "count": 10})

    assert resp.status_code == 200
    assert all(it["video_id"] == CLIPS_VIDEO for it in resp.json()["items"])


# ---------- TASK-16eeaccfaf09 : le mode auto ne pioche que dans les clips valides ----------


def test_series_preview_auto_never_takes_a_ready_but_unvalidated_clip(tmp_path, isolated_cwd):
    config = _series_client(tmp_path, approved_account=None)  # clips prets, aucun valide
    c = TestClient(create_app(config=config))

    resp = c.post("/api/publications/series/preview", json={
        "mode": "auto", "account": READY, "interval_hours": 1, "start_at": _soon(hours=1), "count": 1})

    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == [] and data["available"] == 0
    assert "valide d'abord des clips dans l'écran Clips" in data["insufficient_reason"]


def test_series_preview_auto_never_takes_a_clip_validated_for_another_account(tmp_path, isolated_cwd):
    config = _series_client(tmp_path, approved_account=SPARE)  # valide, mais pour l'autre compte
    c = TestClient(create_app(config=config))

    resp = c.post("/api/publications/series/preview", json={
        "mode": "auto", "account": READY, "interval_hours": 1, "start_at": _soon(hours=1), "count": 1})

    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == [] and data["available"] == 0


def test_series_clips_endpoint_marks_validated_clips_and_still_lists_unvalidated_ones(tmp_path, isolated_cwd):
    config = _series_client(tmp_path, approved_account=None)  # tous prets, aucun valide au depart
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "approved", account=READY)])  # seul "01" est valide
    c = TestClient(create_app(config=config))

    resp = c.get("/api/publications/series/clips")

    assert resp.status_code == 200
    validated = {u["clip_id"]: u["validated"] for u in resp.json()["units"]}
    assert validated["01"] is True
    assert validated["02"] is False


# ---------- TASK-fc561e4dc7e9 : max du champ « Nombre de vidéos » + coche « Parties ensemble » ----------


def test_series_clips_endpoint_returns_available_posts_for_the_given_account(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)  # 6 clips approuves pour READY (_series_client : approved_account=READY)
    c = TestClient(create_app(config=config))

    with_account = c.get("/api/publications/series/clips", params={"account": READY})
    without_account = c.get("/api/publications/series/clips")

    assert with_account.status_code == 200
    assert with_account.json()["available"] == 6
    assert without_account.json()["available"] is None  # pas de compte : le max ne se calcule pas


def test_series_clips_endpoint_available_is_zero_without_any_validated_clip(tmp_path, isolated_cwd):
    config = _series_client(tmp_path, approved_account=None)  # tous prets, aucun valide
    c = TestClient(create_app(config=config))

    resp = c.get("/api/publications/series/clips", params={"account": READY})

    assert resp.json()["available"] == 0


def test_series_clips_endpoint_together_false_lists_each_part_separately(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("multi-p1", part=1, parts_total=2))
    _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar("multi-p2", part=2, parts_total=2))
    c = TestClient(create_app(config=config))

    grouped = c.get("/api/publications/series/clips").json()["units"]
    apart = c.get("/api/publications/series/clips", params={"together": False}).json()["units"]

    assert ["multi-p1", "multi-p2"] in [u["clip_ids"] for u in grouped]
    assert ["multi-p1"] in [u["clip_ids"] for u in apart]
    assert ["multi-p2"] in [u["clip_ids"] for u in apart]


def test_series_preview_and_create_pass_parts_together_false_through_to_entries(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    c = TestClient(create_app(config=config))
    start = _soon(hours=1)
    body = {"mode": "auto", "account": READY, "interval_hours": 1, "start_at": start, "count": 2,
            "parts_together": False}

    preview = c.post("/api/publications/series/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["ok"] is True

    created = c.post("/api/publications/series", json=body)
    assert created.status_code == 201, created.text
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    scheduled = [e for e in entries if e["status"] == "scheduled"]
    assert len(scheduled) == 2 and all(e["parts_together"] is False for e in scheduled)


def test_series_create_default_tags_entries_parts_together_true(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    c = TestClient(create_app(config=config))

    resp = c.post("/api/publications/series", json={
        "mode": "auto", "account": READY, "interval_hours": 1, "start_at": _soon(hours=1), "count": 1})

    assert resp.status_code == 201, resp.text
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    scheduled = [e for e in entries if e["status"] == "scheduled"]
    assert len(scheduled) == 1 and scheduled[0]["parts_together"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_every_static_javascript_file_parses_including_the_series_form():
    # couvert aussi par test_every_static_javascript_file_parses (toute l'arborescence) ; conserve ici pour
    # que l'echec pointe directement vers l'ecran Publication si publish.js casse.
    run = subprocess.run(["node", "--check", str(STATIC / "screens" / "publish.js")],
                        capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, run.stderr


def test_publish_screen_has_a_schedule_series_button_next_to_new_publication():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    assert "data-pub-series" in js and "Programmer une série" in js
    assert "/api/publications/series/preview" in js and "/api/publications/series" in js
    assert "pubOpenSeriesForm" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_series_default_start_is_the_next_full_paris_hour_plus_one():
    out = _run_publish("""(() => ({
      exact: pubSeriesDefaultStart('2026-10-01T12:00:00Z'),      // Paris 14:00 (ete) -> 16:00
      mid: pubSeriesDefaultStart('2026-10-01T12:23:00Z'),        // Paris 14:23 -> 16:00
    }))()""")
    assert out["exact"] == "2026-10-01T16:00" and out["mid"] == "2026-10-01T16:00"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_series_manual_toggle_adds_or_removes_a_whole_unit_in_selection_order():
    out = _run_publish("""(() => {
      const a = { video_id: 'v1', clip_ids: ['a-p1', 'a-p2'] };
      const b = { video_id: 'v1', clip_ids: ['b'] };
      let sel = pubSeriesToggleUnit([], a);
      sel = pubSeriesToggleUnit(sel, b);
      const withBoth = sel.map((u) => u.clip_ids);
      sel = pubSeriesToggleUnit(sel, a);  // decoche a : b reste seul
      return { withBoth, afterUncheckA: sel.map((u) => u.clip_ids), countBoth: pubSeriesPostCount([a, b]) };
    })()""")
    assert out["withBoth"] == [["a-p1", "a-p2"], ["b"]]
    assert out["afterUncheckA"] == [["b"]]
    assert out["countBoth"] == 3


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_series_count_clamps_to_the_available_max():
    out = _run_publish("""(() => ({
      over: pubSeriesClampCount(10, 3),
      ok: pubSeriesClampCount(2, 3),
      zero: pubSeriesClampCount(5, 0),
      unknown: pubSeriesClampCount(5, null),
      belowOne: pubSeriesClampCount(0, 5),
    }))()""")
    assert out == {"over": 3, "ok": 2, "zero": 0, "unknown": 5, "belowOne": 1}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_series_count_note_explains_the_max_or_the_disabled_field():
    out = _run_publish("""(() => ({
      disabled: pubSeriesCountNote(3, 0),
      clamped: pubSeriesCountNote(3, 3),
      normal: pubSeriesCountNote(2, 5),
      unknownMax: pubSeriesCountNote(2, null),
    }))()""")
    assert "valide d'abord des clips" in out["disabled"]
    assert "Maximum disponible" in out["clamped"]
    assert "seront choisies" in out["normal"] and "seront choisies" in out["unknownMax"]


def test_series_form_has_a_parts_together_checkbox_on_by_default():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    assert 'id="pubs-together"' in js and "Parties ensemble" in js
    assert "parts_together" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_series_manual_reorder_moves_a_unit_up_or_down():
    out = _run_publish("""(() => {
      const u = (id) => ({ video_id: 'v1', clip_ids: [id] });
      const sel = [u('a'), u('b'), u('c')];
      return {
        up: pubSeriesMoveUnit(sel, 2, -1).map((x) => x.clip_ids[0]),
        down: pubSeriesMoveUnit(sel, 0, 1).map((x) => x.clip_ids[0]),
        clampTop: pubSeriesMoveUnit(sel, 0, -1).map((x) => x.clip_ids[0]),
        clampBottom: pubSeriesMoveUnit(sel, 2, 1).map((x) => x.clip_ids[0]),
      };
    })()""")
    assert out["up"] == ["a", "c", "b"]
    assert out["down"] == ["b", "a", "c"]
    assert out["clampTop"] == ["a", "b", "c"]
    assert out["clampBottom"] == ["a", "b", "c"]


# --------------------------------------------------------------------------
# TASK-8c4818a974fa : coche « À la suite de la dernière programmation » dans le formulaire « Programmer
# une série » : réutilise publish.after_last_schedule via /api/publications/after-last (déjà servi pour
# « Nouvelle publication », TASK-5c00), jamais de calcul de date dans le JS.
# --------------------------------------------------------------------------


_PUBS_FORM_BASE = {
    "mode": "auto", "style": "", "units": [], "accounts": [{"id": "ab12cd", "label": "Compte", "ready_to_publish": True}],
    "start": "2026-10-08T16:00", "intervalH": 3, "count": 1, "selected": [], "preview": None,
    "together": True, "available": None,
}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_series_form_after_last_checkbox_is_unchecked_by_default_with_start_enabled():
    out = _run_publish(f"pubSeriesFormHtml({json.dumps(_PUBS_FORM_BASE)})")
    start_tag = re.search(r'<input[^>]*id="pubs-start"[^>]*>', out).group(0)
    checkbox_tag = re.search(r'<input[^>]*id="pubs-after-last"[^>]*>', out).group(0)
    assert "disabled" not in start_tag
    assert 'value="2026-10-08T16:00"' in start_tag                      # décochée : la date saisie reste affichée
    assert "checked" not in checkbox_tag
    assert "À la suite de la dernière programmation" in out


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_series_form_after_last_checked_disables_start_and_shows_the_computed_paris_date():
    body = {**_PUBS_FORM_BASE, "afterLast": True,
            "afterAt": "2026-10-08T12:00:00+00:00", "afterAtParis": "2026-10-08T14:00:00+02:00"}
    out = _run_publish(f"pubSeriesFormHtml({json.dumps(body)})")
    start_tag = re.search(r'<input[^>]*id="pubs-start"[^>]*>', out).group(0)
    checkbox_tag = re.search(r'<input[^>]*id="pubs-after-last"[^>]*>', out).group(0)
    hint_span = re.search(r'<span[^>]*id="pubs-after-last-hint"[^>]*>([^<]*)</span>', out)
    assert " disabled" in start_tag
    assert 'value="2026-10-08T14:00"' in start_tag                      # heure de Paris calculée, pas l'instant UTC brut
    assert " checked" in checkbox_tag
    assert "hidden" not in re.search(r'<span[^>]*id="pubs-after-last-hint"[^>]*>', out).group(0)
    assert "8 octobre" in hint_span.group(1) and "14:00" in hint_span.group(1) and "3 h" in hint_span.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_series_form_after_last_hint_asks_for_an_account_and_interval_before_any_computed_date():
    out = _run_publish(f"pubSeriesFormHtml({json.dumps({**_PUBS_FORM_BASE, 'afterLast': True})})")
    hint_span = re.search(r'<span[^>]*id="pubs-after-last-hint"[^>]*>([^<]*)</span>', out)
    assert "Choisis un compte et un intervalle" in hint_span.group(1)


def test_series_after_last_reuses_the_existing_after_last_endpoint_server_side():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    refresh = js[js.index("async function pubSeriesRefreshAfterLast"):js.index("function pubSeriesWire")]
    assert "/api/publications/after-last" in refresh and "interval_hours" in refresh
    assert "publish_at_paris" in refresh and "res.publish_at" in refresh


def test_series_after_last_checkbox_recomputes_when_account_or_interval_changes():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    assert 'id="pubs-after-last"' in js and "afterLastEl.onchange" in js
    wire = js[js.index("function pubSeriesWire"):js.index("function pubSeriesRerender")]
    assert "f.afterLast" in wire
    assert wire.count("pubSeriesRefreshAfterLast(f, d)") >= 3           # coche, intervalle, compte


def test_series_body_uses_the_server_computed_after_last_date_not_a_js_recalculation():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    body = js[js.index("function pubSeriesBody"):js.index("function pubSeriesClampCount")]
    assert 'afterLast ? (f.afterAt || "")' in body
    assert "pubParisInstant(startLocal).toISOString()" in body          # décochée : comportement actuel inchangé


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_series_open_form_starts_with_the_after_last_checkbox_unchecked():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    open_form = js[js.index("async function pubOpenSeriesForm"):]
    assert "afterLast: false" in open_form and "afterAt: null" in open_form


# --------------------------------------------------------------------------
# Journal (TASK-8067) : middleware de journalisation des requetes + GET /api/journal.
# Le middleware journalise via le logger normal (clipper.web.app) ; c'est le
# handler de clipper.journal, installe par clipper.__main__ independamment de
# ce module, qui le persiste dans logs/ (teste dans test_journal.py).
# --------------------------------------------------------------------------


def test_get_request_is_journaled_with_method_path_status_and_duration(tmp_path, isolated_cwd, caplog):
    with caplog.at_level(logging.INFO, logger="clipper.web.app"):
        resp = client(tmp_path).get("/api/dashboard")

    assert resp.status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("GET" in l and "/api/dashboard" in l and "200" in l and "ms" in l for l in lines)
    assert not any("corps=" in l for l in lines)  # GET n'est jamais une action qui modifie


def test_a_request_with_a_query_string_is_journaled_with_it(tmp_path, isolated_cwd, caplog):
    with caplog.at_level(logging.INFO, logger="clipper.web.app"):
        client(tmp_path).get("/api/journal", params={"level": "WARNING"})

    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("/api/journal?level=WARNING" in l for l in lines), lines


def test_excluded_paths_are_never_journaled(tmp_path, isolated_cwd, caplog):
    with caplog.at_level(logging.INFO, logger="clipper.web.app"):
        client(tmp_path).get("/static/style.css")

    assert [r for r in caplog.records if r.name == "clipper.web.app"] == []


def test_mutating_request_logs_a_body_summary_with_secrets_masked(tmp_path, isolated_cwd, monkeypatch, caplog):
    from clipper import worker

    monkeypatch.setattr(
        worker, "enqueue",
        lambda url, channel, action, force_steps, *, config=None: {
            "id": "e1", "video_id": VIDEO_ID, "url": url, "channel": channel,
            "action": action, "force_steps": force_steps or [], "status": "waiting",
        },
    )

    with caplog.at_level(logging.INFO, logger="clipper.web.app"):
        client(tmp_path).post("/api/queue", json={"url": URL, "password": "s3cret"})

    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    body_lines = [l for l in lines if "corps=" in l]
    assert body_lines, lines
    assert "s3cret" not in body_lines[0]
    assert "***" in body_lines[0]


def test_get_journal_endpoint_returns_recent_lines(tmp_path, isolated_cwd):
    from clipper import journal

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    today = journal._today()
    record_info = logging.LogRecord("clipper.test", logging.INFO, "", 0, "premier evenement", None, None)
    record_warn = logging.LogRecord("clipper.test", logging.WARNING, "", 0, "deuxieme evenement important", None, None)
    content = journal.format_line(record_info, "run[1]") + journal.format_line(record_warn, "run[2]")
    (logs_dir / f"journal-{today.isoformat()}.log").write_text(content, encoding="utf-8")

    resp = client(tmp_path).get("/api/journal")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    messages = [l["message"] for l in body["lines"]]
    assert "premier evenement" in messages and "deuxieme evenement important" in messages


def test_get_journal_endpoint_filters_by_level_and_text(tmp_path, isolated_cwd):
    from clipper import journal

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    today = journal._today()
    record_info = logging.LogRecord("clipper.test", logging.INFO, "", 0, "premier evenement", None, None)
    record_warn = logging.LogRecord("clipper.test", logging.WARNING, "", 0, "deuxieme evenement important", None, None)
    content = journal.format_line(record_info, "run[1]") + journal.format_line(record_warn, "run[2]")
    (logs_dir / f"journal-{today.isoformat()}.log").write_text(content, encoding="utf-8")
    c = client(tmp_path)

    by_level = c.get("/api/journal", params={"level": "WARNING"}).json()
    by_text = c.get("/api/journal", params={"q": "premier"}).json()

    assert [l["message"] for l in by_level["lines"]] == ["deuxieme evenement important"]
    assert [l["message"] for l in by_text["lines"]] == ["premier evenement"]


def test_get_journal_endpoint_reports_unavailable_without_a_logs_directory(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/api/journal")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["lines"] == []
    assert body["reason"]


# --------------------------------------------------------------------------
# TASK-bdd5 : vidéo interrompue (étape running sans processus)
# --------------------------------------------------------------------------

ORPHAN_ID = "Zk_wshsmq5w"


def _write_orphan(tmp_path, video_id=ORPHAN_ID):
    """Cas réel 2026-10-04 : download done, transcribe running, file vide."""
    from clipper import pipeline

    state = pipeline.new_state(video_id, f"https://youtu.be/{video_id}", "review")
    state["status"] = "running"
    state["steps"]["download"].update(status="done")
    state["steps"]["transcribe"].update(status="running", started_at="2026-10-04T10:02:00+00:00")
    pipeline.save_state(state, config=make_config(tmp_path))
    return state


def _orphan_statuses(tmp_path) -> dict:
    from clipper import pipeline

    return {n: s["status"] for n, s in pipeline.load_state(ORPHAN_ID, config=make_config(tmp_path))["steps"].items()}


def test_a_running_step_without_a_process_is_reported_interrupted_never_running(tmp_path, isolated_cwd):
    _write_orphan(tmp_path)

    listed = client(tmp_path).get("/api/videos").json()
    detail = client(tmp_path).get(f"/api/videos/{ORPHAN_ID}").json()

    assert [(v["video_id"], v["status"], v["interrupted"]) for v in listed] == [(ORPHAN_ID, "interrupted", True)]
    assert detail["status"] == "interrupted" and "transcribe" in detail["reason"]
    assert [v["video_id"] for v in client(tmp_path).get("/api/videos?status=interrupted").json()] == [ORPHAN_ID]
    assert client(tmp_path).get("/api/videos?status=running").json() == []
    assert client(tmp_path).get("/api/dashboard").json()["running"] == []


def test_a_running_step_with_a_live_process_stays_running(tmp_path, isolated_cwd):
    state = _write_orphan(tmp_path)
    _write_json(tmp_path / "state" / "queue.json", [{
        "id": "q1", "video_id": ORPHAN_ID, "url": state["source_url"], "channel": None, "action": "run",
        "force_steps": [], "enqueued_at": "2026-10-04T10:00:00+00:00", "status": "running", "pid": os.getpid()}])

    video = client(tmp_path).get(f"/api/videos/{ORPHAN_ID}").json()

    assert (video["status"], video["interrupted"]) == ("running", False)


def test_cancelling_an_interrupted_video_works_and_keeps_the_finished_steps(tmp_path, isolated_cwd):
    _write_orphan(tmp_path)

    resp = client(tmp_path).post(f"/api/videos/{ORPHAN_ID}/cancel")

    assert resp.status_code == 200, resp.text
    assert _orphan_statuses(tmp_path)["download"] == "done"
    assert _orphan_statuses(tmp_path)["transcribe"] == "pending"
    assert client(tmp_path).get(f"/api/videos/{ORPHAN_ID}").json()["status"] == "failed"


def test_resuming_an_interrupted_video_requeues_it_without_redoing_done_steps(tmp_path, isolated_cwd):
    _write_orphan(tmp_path)

    resp = client(tmp_path).post(f"/api/videos/{ORPHAN_ID}/resume")

    assert resp.status_code == 202, resp.text
    queue = json.loads((tmp_path / "state" / "queue.json").read_text(encoding="utf-8"))
    assert [(e["video_id"], e["action"], e["status"], e["force_steps"]) for e in queue] == [
        (ORPHAN_ID, "run", "waiting", [])]
    assert _orphan_statuses(tmp_path)["download"] == "done"


def test_resuming_a_video_that_really_runs_is_a_409(tmp_path, isolated_cwd):
    state = _write_orphan(tmp_path)
    _write_json(tmp_path / "state" / "queue.json", [{
        "id": "q1", "video_id": ORPHAN_ID, "url": state["source_url"], "channel": None, "action": "run",
        "force_steps": [], "enqueued_at": "2026-10-04T10:00:00+00:00", "status": "running", "pid": os.getpid()}])

    assert client(tmp_path).post(f"/api/videos/{ORPHAN_ID}/resume").status_code == 409


def test_resuming_an_unknown_video_is_a_404(tmp_path, isolated_cwd):
    assert client(tmp_path).post(f"/api/videos/{ORPHAN_ID}/resume").status_code == 404


def test_videos_screen_offers_resume_and_cancel_on_an_interrupted_video(tmp_path, isolated_cwd):
    js = _videos_js(tmp_path)

    assert 'video.status === "interrupted"' in js
    assert "/resume" in js and "data-resume-video" in js
    assert "/cancel" in js
    idx = js.index("/resume")
    assert "confirmDialog(" in js[max(0, idx - 800):idx]


def test_purge_completed_shows_calculation_then_purge_state_on_the_button(tmp_path, isolated_cwd):
    js = _videos_js(tmp_path)

    start = js.index("async function purgeCompleted")
    body = js[start:js.index("/* ---------- Fiche d'une video", start)]
    assert "Calcul de l'espace libérable…" in body
    assert "Purge en cours…" in body
    assert body.index("Calcul de l'espace libérable…") < body.index('api("/api/purge-completed")')
    assert body.index('api("/api/purge-completed", { method: "POST" })') > body.index("confirmDialog(")
    assert body.index("Purge en cours…") > body.index("confirmDialog(")


def test_purge_completed_disables_the_button_and_refuses_a_second_run(tmp_path, isolated_cwd):
    js = _videos_js(tmp_path)

    start = js.index("async function purgeCompleted")
    body = js[start:js.index("/* ---------- Fiche d'une video", start)]
    assert "if (purgeBusy) return;" in body
    assert "btn.disabled = true" in body
    assert "btn.disabled = false" in body
    assert body.count("finally") == 1 and "purgeBusy = false" in body[body.index("finally"):]


# --------------------------------------------------------------------------
# Purge des videos (TASK-886a)
# --------------------------------------------------------------------------


def _purge_video(tmp_path, video_id="aaaaaaaaaaa", status="done", heavy=100):
    _write_state(tmp_path, video_id, status=status)
    d = tmp_path / "workspace" / video_id
    (d / f"{video_id}.mp4").write_bytes(b"x" * heavy)
    (d / "frames").mkdir()
    (d / "frames" / "f.jpg").write_bytes(b"x" * 10)
    (d / "meta.json").write_text(json.dumps({"title": "Ma vidéo"}), encoding="utf-8")
    return d


def _purge_clip(tmp_path, video_id="aaaaaaaaaaa", clip_id="01-p1"):
    out = tmp_path / "output" / video_id
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{clip_id}.mp4").write_bytes(b"y" * 50)
    (out / f"{clip_id}.json").write_text(json.dumps({"video_id": video_id, "clip_id": clip_id}), encoding="utf-8")
    return out


def test_purge_preview_shows_the_size_and_deletes_nothing(tmp_path, isolated_cwd):
    d = _purge_video(tmp_path)

    resp = client(tmp_path).get("/api/purge/aaaaaaaaaaa")

    assert resp.status_code == 200
    assert resp.json()["heavy_bytes"] == 110 and resp.json()["total_bytes"] == 110
    assert (d / "aaaaaaaaaaa.mp4").is_file()


def test_purge_video_frees_heavy_files_and_keeps_the_video_listed_with_its_clips(tmp_path, isolated_cwd):
    d = _purge_video(tmp_path)
    _purge_clip(tmp_path)
    c = client(tmp_path)

    resp = c.post("/api/purge/aaaaaaaaaaa", json={})

    assert resp.status_code == 200 and resp.json()["freed_bytes"] == 110
    assert not (d / "aaaaaaaaaaa.mp4").exists()
    listed = c.get("/api/videos").json()
    assert [v["video_id"] for v in listed] == ["aaaaaaaaaaa"]
    assert listed[0]["status"] == "done" and listed[0]["purged"] is True and listed[0]["title"] == "Ma vidéo"
    assert (tmp_path / "output" / "aaaaaaaaaaa" / "01-p1.mp4").is_file()
    assert [x["clip_id"] for x in c.get("/api/videos/aaaaaaaaaaa/clips").json()] == ["01-p1"]


def test_purge_video_refuses_a_running_video_with_409(tmp_path, isolated_cwd):
    d = _purge_video(tmp_path, status="running")
    _busy_worker(tmp_path)

    resp = client(tmp_path).post("/api/purge/aaaaaaaaaaa", json={})

    assert resp.status_code == 409 and "en cours" in resp.json()["detail"]
    assert (d / "aaaaaaaaaaa.mp4").is_file()


def test_purge_video_with_clips_option_deletes_output(tmp_path, isolated_cwd):
    _purge_video(tmp_path)
    out = _purge_clip(tmp_path)
    c = client(tmp_path)

    preview = c.get("/api/purge/aaaaaaaaaaa?clips=true").json()
    assert preview["clips_bytes"] > 50 and preview["total_bytes"] == preview["heavy_bytes"] + preview["clips_bytes"]
    assert c.post("/api/purge/aaaaaaaaaaa", json={"clips": True}).status_code == 200

    assert not out.exists()


def test_purge_clips_refused_when_a_clip_has_a_pending_publication_names_the_clip(tmp_path, isolated_cwd):
    d = _purge_video(tmp_path)
    _purge_clip(tmp_path, clip_id="02-p2")
    pub = tmp_path / "state" / "publish"
    pub.mkdir(parents=True)
    (pub / "style.json").write_text(json.dumps(
        [{"video_id": "aaaaaaaaaaa", "clip_id": "02-p2", "status": "scheduled"}]), encoding="utf-8")
    c = client(tmp_path)

    resp = c.post("/api/purge/aaaaaaaaaaa", json={"clips": True})
    assert resp.status_code == 409 and "02-p2" in resp.json()["detail"]
    assert c.get("/api/purge/aaaaaaaaaaa?clips=true").status_code == 409

    assert (d / "aaaaaaaaaaa.mp4").is_file()  # rien n'a ete purge a moitie
    assert (tmp_path / "output" / "aaaaaaaaaaa" / "02-p2.mp4").is_file()


def test_purge_completed_previews_then_purges_only_done_videos(tmp_path, isolated_cwd):
    done = _purge_video(tmp_path, "aaaaaaaaaaa", status="done", heavy=100)
    failed = _purge_video(tmp_path, "bbbbbbbbbbb", status="failed", heavy=200)
    _purge_video(tmp_path, "ccccccccccc", status="done", heavy=300)
    c = client(tmp_path)

    preview = c.get("/api/purge-completed").json()
    assert sorted(v["video_id"] for v in preview["videos"]) == ["aaaaaaaaaaa", "ccccccccccc"]
    assert preview["total_bytes"] == 110 + 310
    assert (done / "aaaaaaaaaaa.mp4").is_file()

    resp = c.post("/api/purge-completed")

    assert resp.status_code == 200 and resp.json()["freed_bytes"] == 420
    assert not (done / "aaaaaaaaaaa.mp4").exists()
    assert (failed / "bbbbbbbbbbb.mp4").is_file()


def test_disk_endpoint_reports_workspace_and_output_totals(tmp_path, isolated_cwd):
    _purge_video(tmp_path)
    _purge_clip(tmp_path)

    body = client(tmp_path).get("/api/disk").json()

    assert body["workspace_bytes"] > 110 and body["output_bytes"] > 50


def test_rerun_of_a_purged_video_says_source_purged_instead_of_crashing(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    _purge_video(tmp_path)
    c = client(tmp_path)
    c.post("/api/purge/aaaaaaaaaaa", json={})
    monkeypatch.setattr(worker, "enqueue", lambda url, channel, action, force_steps, *, config=None: {
        "id": "e1", "video_id": "aaaaaaaaaaa", "url": url, "channel": channel, "action": action,
        "force_steps": force_steps or [], "status": "waiting"})

    for resp in (c.post("/api/videos/aaaaaaaaaaa/render"),
                 c.post("/api/videos/aaaaaaaaaaa/retry", json={"from_step": "render"})):
        assert resp.status_code == 409
        assert "source purgée : retélécharger" in resp.json()["detail"]
    assert c.post("/api/videos/aaaaaaaaaaa/retry", json={"from_step": "download"}).status_code == 202


def test_purge_endpoints_validate_the_video_id(tmp_path, isolated_cwd):
    c = client(tmp_path)

    assert c.post("/api/purge/zzzzzzzzzzz", json={}).status_code == 409  # dossier introuvable, explicite
    assert c.get("/api/purge/..%2Fx").status_code in (400, 404, 422)


def test_videos_screen_has_purge_buttons_confirmations_and_disk_usage():
    js = _static("screens", "videos.js")

    assert "Purger les vidéos terminées" in js and "data-purge-video" in js and "data-purge-completed" in js
    assert "/api/purge/" in js and "/api/purge-completed" in js and "/api/disk" in js
    assert "confirmDialog" in js[js.index("async function purgeVideo"):]
    assert "purged" in js and "source purgée" in js
    if shutil.which("node"):
        subprocess.run(["node", "--check", str(Path(web_app.__file__).parent / "static" / "screens" / "videos.js")],
                       check=True)


# --------------------------------------------------------------------------
# TASK-30cc : pays de l'IP publique (/api/network, pastille de la barre du haut, réglage)
# --------------------------------------------------------------------------


@pytest.fixture
def geo(monkeypatch):
    from clipper import network as network_mod

    network_mod.reset()
    state = {"answer": {"ip": "1.2.3.4", "city": "Paris", "country": "FR", "org": "AS51207 Free Mobile SAS"}, "calls": 0}

    def fetch(url):
        state["calls"] += 1
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return state["answer"]

    network_mod.use_fetcher(fetch)
    yield state
    network_mod.use_fetcher(None)
    network_mod.reset()


def test_api_network_reports_expected_country(tmp_path, isolated_cwd, geo):
    data = client(tmp_path).get("/api/network").json()
    assert data["ok"] is True and data["country"] == "FR" and data["city"] == "Paris"
    assert data["ip"] == "1.2.3.4" and data["isp"] == "AS51207 Free Mobile SAS"
    assert data["expected_country"] == "FR"


def test_api_network_flags_other_country_and_respects_the_cache(tmp_path, isolated_cwd, geo):
    geo["answer"] = {"ip": "5.6.7.8", "city": "London", "country": "GB", "org": "AS1 BT"}
    c = client(tmp_path)
    first = c.get("/api/network").json()
    c.get("/api/network")
    assert first["ok"] is False and first["country_name"] == "Royaume-Uni"
    assert geo["calls"] == 1


def test_api_network_unknown_when_service_is_down(tmp_path, isolated_cwd, geo):
    geo["answer"] = OSError("coupé")
    resp = client(tmp_path).get("/api/network")
    assert resp.status_code == 200
    assert resp.json()["ok"] is None and "coupé" in resp.json()["error"]


def test_settings_edit_expected_country(tmp_path, isolated_cwd):
    path = _settings_setup(tmp_path)
    c = sclient(tmp_path)
    assert c.get("/api/settings").json()["effective"]["network"]["expected_country"] == "FR"
    resp = c.put("/api/settings", json={"settings": {"network": {"expected_country": "BE"}}})
    assert resp.status_code == 200, resp.text
    assert tomllib.loads(path.read_text(encoding="utf-8"))["network"]["expected_country"] == "BE"
    assert resp.json()["effective"]["network"]["expected_country"] == "BE"


@pytest.mark.parametrize("value", ["", "FRA", "F", "1A", 5])
def test_settings_refuses_a_bad_country_code(tmp_path, isolated_cwd, value):
    path = _settings_setup(tmp_path)
    before = path.read_text(encoding="utf-8")
    resp = sclient(tmp_path).put("/api/settings", json={"settings": {"network": {"expected_country": value}}})
    assert resp.status_code == 422 and "expected_country" in resp.json()["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_topbar_has_a_network_pill_wired_to_the_api_every_60s(tmp_path, isolated_cwd):
    html = served(tmp_path, "/")
    top = html[html.index('<header class="topbar"'):html.index("</header>")]
    assert 'id="net-pill"' in top
    js = served(tmp_path, "/static/app.js")
    assert "/api/network" in js and "60000" in js and "visibilitychange" in js
    assert "IP hors " in js and "pays inconnu" in js
    assert ".net-pill" in served(tmp_path, "/static/style.css")


def test_settings_screen_has_a_country_choice(tmp_path, isolated_cwd):
    js = served(tmp_path, "/static/screens/settings.js")
    assert "network.expected_country" in js and "[network]" in js


# --------------------------------------------------------------------------
# TASK-7f582251f6c5 : liste « Refusés par TikTok »
# --------------------------------------------------------------------------


def test_a_clip_refused_by_tiktok_is_not_available_and_is_listed_apart_with_its_reason(tmp_path, isolated_cwd):
    _fable_setup(tmp_path, clips=("01", "02"))
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "refused_by_platform", slot_at=None, halted=False,
                                                  error="vérification de contenu : problème signalé par TikTok",
                                                  refused_at="2026-10-05T20:00:00+00:00")])
    c = client(tmp_path)

    clips = {x["clip_id"]: x for x in c.get("/api/clips", params={"video_id": CLIPS_VIDEO}).json()}
    assert clips["01"]["publish_status"] == "refused_by_platform"  # plus « à valider » : hors des clips disponibles
    assert clips["02"]["publish_status"] == "à valider"
    assert c.post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": READY}).status_code == 409
    bulk = c.post("/api/clips/approve", json=_bulk_body([(CLIPS_VIDEO, "01")]))
    assert bulk.status_code == 409 and "refused_by_platform" in bulk.json()["refused"][0]

    body = c.get("/api/publications").json()
    assert body["publications"] == []  # ni dans la file ni sur le calendrier
    refused = body["refused_by_platform"]
    assert [(r["clip_id"], r["error"]) for r in refused] == [
        ("01", "vérification de contenu : problème signalé par TikTok")]
    assert refused[0]["tiktok_status"] == "refused_by_platform" and refused[0]["editable"] is False


def test_the_screens_list_the_clips_refused_by_tiktok():
    publish_js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    clips_js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")

    assert "refused_by_platform" in publish_js and "Refusés par TikTok" in publish_js
    assert '["refused_by_platform", "Refusés par TikTok"]' in clips_js


# --------------------------------------------------------------------------
# Alerte réseau au clic Publier / Valider (TASK-120a)
# --------------------------------------------------------------------------

def _net_guard_src() -> str:
    js = (STATIC / "ui.js").read_text(encoding="utf-8")
    start = js.index("let netLast = null;")
    return js[start:js.index("/* ---------- Presse-papiers", start)]


def _net_script(net, body: str, dialog_answer: str = "true") -> str:
    """Évalue la garde réseau d'ui.js avec netAlertDialog remplacée : la réponse de la fenêtre est fixée."""
    return (
        "const esc = (s) => String(s);\n"
        "let dialogs = [];\n"
        + _net_guard_src().replace("function netAlertDialog(", "function netAlertDialogReal(")
        + f"\nfunction netAlertDialog(net, mode) {{ dialogs.push(mode); return Promise.resolve({dialog_answer}); }}\n"
        "let apiCalls = [];\n"
        "async function api(url) { apiCalls.push(url); return " + json.dumps(net) + "; }\n"
        + body
    )


_NET_BAD = {"ok": False, "country": "US", "expected_country_name": "France", "block_browser": False}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
@pytest.mark.parametrize("net,expect_dialog,expect_pass", [
    ({"ok": False, "block_browser": False}, "ask", True),
    ({"ok": False, "block_browser": True}, "block", False),
    ({"ok": True}, None, True),
    ({"ok": None}, None, True),
])
def test_net_guard_opens_a_dialog_only_when_ip_country_is_wrong(net, expect_dialog, expect_pass):
    script = _net_script(net, "setNetLast(" + json.dumps(net) + ");\n"
                         "netGuard().then((r) => console.log(JSON.stringify([r, dialogs, apiCalls])));")
    # block : la réponse de la fenêtre est « fermée » (false) ; ask : « Continuer » (true)
    script = script.replace("Promise.resolve(true)", "Promise.resolve(mode !== 'block')")
    result, dialogs, calls = json.loads(_node_run(script))
    assert dialogs == ([expect_dialog] if expect_dialog else [])
    assert result is expect_pass
    assert calls == []  # une seule source : la valeur déjà relevée, pas de nouvel appel


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_net_guard_cancel_returns_false_and_unknown_state_fetches_once():
    script = _net_script(_NET_BAD, "netGuard().then((a) => netGuard().then((b) => "
                         "console.log(JSON.stringify([a, b, dialogs, apiCalls]))));", dialog_answer="false")
    a, b, dialogs, calls = json.loads(_node_run(script))
    assert (a, b) == (False, False) and dialogs == ["ask", "ask"]
    assert calls == ["/api/network"]  # état absent : relevé une seule fois


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
@pytest.mark.parametrize("ok,answer,expect_called", [(False, "false", False), (False, "true", True), (True, "false", True)])
def test_net_guard_decides_whether_publishing_actions_run(ok, answer, expect_called):
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    start = js.index("async function pubRetry(c)")
    fn = js[start:js.index("\n}\n", start) + 3]
    net = {"ok": ok, "block_browser": False}
    body = (
        "const pubEnc = (s) => s; const pubTitle = () => 't'; const toast = () => {}; const toastError = () => {};\n"
        "async function pubLoad() {}\n" + fn.replace("await api(", "await postApi(") +
        "\nasync function postApi(u) { posted.push(u); }\nlet posted = [];\n"
        "setNetLast(" + json.dumps(net) + ");\n"
        "pubRetry({video_id: 'v', clip_id: 'c'}).then((r) => console.log(JSON.stringify([r, posted, dialogs])));"
    )
    r, posted, dialogs = json.loads(_node_run(_net_script(net, body, answer)))
    assert (len(posted) == 1) is expect_called and r is expect_called
    assert dialogs == ([] if ok else ["ask"])


def test_every_publishing_action_goes_through_the_same_net_guard():
    publish = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    clips = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")

    def guarded(js: str, fn_start: str, call: str) -> bool:
        body = js[js.index(fn_start):]
        return 0 <= body.index("await netGuard()") < body.index(call)

    assert guarded(publish, "async function pubFormSubmit", 'api("/api/publications", jsonBody("POST"')   # Nouvelle publication / Publier
    assert guarded(publish, "async function pubSeriesSubmit", "/api/publications/series")                  # Série programmée
    assert guarded(publish, "async function pubRetry", "/retry")                                           # Réessayer
    assert guarded(clips, "async function clipsApproveSelection", "/api/clips/approve")                    # sélection
    assert guarded(clips, "if (approve) approve.onclick", 'clipUrl(c, "/approve")')                        # un clip
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "setNetLast(net)" in app[app.index("function paintNetwork"):app.index("async function refreshNetwork")]


# ---------- TASK-fa00f90a735a : coche « Heure par clip » (clip_dates) ----------


def _clip_dates_body(account, dates, **extra):
    """Corps manuel avec une date par clip ; ni intervalle ni debut (ignores par l'API en mode par clip)."""
    return {
        "mode": "manual", "account": account,
        "selection": [{"video_id": CLIPS_VIDEO, "clip_id": cid} for cid in dates],
        "clip_dates": [{"video_id": CLIPS_VIDEO, "clip_id": cid, "publish_at": at} for cid, at in dates.items()],
        **extra,
    }


def test_series_preview_per_clip_dates_shows_each_date_and_refuses_clip_by_clip(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    c = TestClient(create_app(config=config))
    late, same = _soon(hours=9), _soon(hours=3)

    resp = c.post("/api/publications/series/preview", json=_clip_dates_body(
        READY, {"05": late, "02": same, "03": same}))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [(it["clip_id"], it["publish_at"]) for it in data["items"]] == [
        ("05", _dt.fromisoformat(late).isoformat()), ("02", _dt.fromisoformat(same).isoformat()),
        ("03", _dt.fromisoformat(same).isoformat())]
    assert [bool(it["refusal"]) for it in data["items"]] == [False, False, True]
    assert "même heure" in data["items"][2]["refusal"]
    assert all(it["publish_at_paris"] for it in data["items"]) and data["ok"] is False


def test_series_create_per_clip_dates_schedules_each_clip_at_its_own_date(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    c = TestClient(create_app(config=config))
    first, second = _soon(hours=9), _soon(hours=3)

    resp = c.post("/api/publications/series", json=_clip_dates_body(READY, {"05": first, "02": second}))

    assert resp.status_code == 201, resp.text
    assert resp.json() == {"created": 2}
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    slots = {e["clip_id"]: e["slot_at"] for e in entries if e["status"] == "scheduled"}
    assert slots == {"05": _dt.fromisoformat(first).isoformat(), "02": _dt.fromisoformat(second).isoformat()}


def test_series_create_per_clip_dates_is_refused_whole_when_one_date_is_refused(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    c = TestClient(create_app(config=config))
    same = _soon(hours=3)

    resp = c.post("/api/publications/series", json=_clip_dates_body(READY, {"05": same, "02": same}))

    assert resp.status_code == 409 and f"{CLIPS_VIDEO}/02" in resp.json()["detail"]
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert {e["status"] for e in entries} == {"approved"}


def test_series_preview_without_clip_dates_still_needs_the_start_and_the_interval(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    c = TestClient(create_app(config=config))

    resp = c.post("/api/publications/series/preview", json={
        "mode": "manual", "account": READY, "selection": [{"video_id": CLIPS_VIDEO, "clip_id": "02"}]})

    assert resp.status_code in (409, 422) and "début" in resp.text


# --------------------------------------------------------------------------
# Apprentissage (3/4) : GET /api/learning, Adopter / Refuser, section de l'ecran Statistiques (TASK-c108, SPEC-00db R6-R7)
# --------------------------------------------------------------------------

import tomllib  # noqa: E402

_LEARN_TOML = (
    '# commentaire a perdre\nmode = "review"\n'
    '[web]\nport = 8123\ntoken = "secret-tres-long"\n'
    '[render]\ncrf = 18\n'
)
_PROPOSED = "NEUVE perspective de {judge} : plus de nuance sur la chute."


def _learning_state(tmp_path, *, toml=_LEARN_TOML) -> None:
    """Etat du coach sous le cwd (isolated_cwd) : retention proposee, spectateur adoptee, avocat refusee, monteur rejetee."""
    (tmp_path / "config.toml").write_text(toml, encoding="utf-8")
    state = tmp_path / "state" / "learning"
    state.mkdir(parents=True)
    entries = []
    for judge, version, status in (("retention", 1, "proposed"), ("spectateur", 1, "adopted"), ("avocat", 2, "refused")):
        md = tmp_path / "prompts" / "jury" / judge / f"v{version}.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(f"# {judge} v{version}\n\n{_PROPOSED.format(judge=judge)}\n\n## Justification\nmieux\n\n"
                      "## Metrique (erreur)\n- avant : 0.5000\n- apres : 0.2000\n- cas rejoues : 5\n", encoding="utf-8")
        entries.append({"judge": judge, "accepted": True, "reason": None, "version": version, "path": str(md),
                        "metric": {"before": 0.5, "after": 0.2, "cases": 5}, "status": status,
                        "decided_at": None if status == "proposed" else "2026-10-09T10:00:00+00:00",
                        "decided_by": None if status == "proposed" else "web"})
    entries.append({"judge": "monteur", "accepted": False, "reason": "ne predit pas mieux en rejeu", "version": None,
                    "path": None, "metric": None, "status": "rejected", "decided_at": None, "decided_by": None})
    (state / "coach.json").write_text(json.dumps({"last_run": "2026-10-08T10:00:00+00:00", "runs": [
        {"at": "2026-10-08T10:00:00+00:00", "cases": 12, "judges": entries}]}), encoding="utf-8")
    (state / "sync.json").write_text(json.dumps({
        "last_sync": "2026-10-08T09:00:00+00:00", "last_error": {"at": "2026-10-08T09:30:00+00:00", "where": "coach", "message": "boom"},
        "results": [], "scored": [], "excluded": [{"video_id": "V", "clip_id": "01", "account": "compte_a", "reason": "immature"}],
        "accounts": {}, "calibration": None}), encoding="utf-8")
    (state / "links.json").write_text(json.dumps({
        "last_run": {}, "counts": {"compte_a": {"linked": 3, "none": 1, "ambiguous": 0}},
        "unlinked": [{"video_id": "V", "clip_id": "02", "account": "compte_a", "reason": "none"}]}), encoding="utf-8")
    (tmp_path / "state" / "jury_weights.json").write_text(json.dumps({"judges": {
        "retention": {"weight": 1.2, "agreement": 0.4, "clips": 8, "reason": None}}}), encoding="utf-8")


def _learning_client(tmp_path, monkeypatch):
    """Serveur dont tout appel LLM ou calcul d'apprentissage ferait echouer le test (R7)."""
    from clipper import jury_coach, learning, llm
    from clipper.llm.fake import FakeBackend

    fake = FakeBackend([{"unexpected": True}])
    forbidden = []
    for name in ("sync", "run_if_due", "coach_if_due", "link_posts", "link_if_due"):
        monkeypatch.setattr(learning, name, lambda *a, _n=name, **k: forbidden.append(_n))
    monkeypatch.setattr(jury_coach, "propose", lambda *a, **k: forbidden.append("propose"))
    from clipper.config import load_config
    app = create_app(config=load_config(tmp_path / "config.toml"))
    return TestClient(app), fake, forbidden


def test_get_learning_returns_state_weights_and_every_proposal_with_both_perspectives(tmp_path, isolated_cwd, monkeypatch):
    from clipper import jury, llm

    _learning_state(tmp_path)
    c, fake, forbidden = _learning_client(tmp_path, monkeypatch)
    with llm.use_backend(fake):
        data = c.get("/api/learning").json()

    assert set(data) == {"enabled", "links", "sync", "weights", "coach", "retention", "breakdown"} and data["enabled"] is True
    assert set(data["breakdown"]) == {"n", "min_n", "skipped", "groups"} and set(data["breakdown"]["groups"]) == {"game", "streamer", "hour", "account"}
    assert data["links"]["counts"]["compte_a"]["linked"] == 3 and data["sync"]["last_error"]["message"] == "boom"
    assert data["weights"]["judges"]["retention"]["weight"] == 1.2
    by_judge = {p["judge"]: p for p in data["coach"]}
    assert set(by_judge) == {"retention", "spectateur", "avocat"}  # la proposition rejetee n'est pas adoptable
    retention = by_judge["retention"]
    assert retention["status"] == "proposed" and retention["version"] == 1 and retention["decided_at"] is None
    assert retention["metric"] == {"before": 0.5, "after": 0.2, "cases": 5}
    assert retention["perspective_proposed"] == _PROPOSED.format(judge="retention")
    assert retention["perspective_current"] == jury.CONFIG_DEFAULTS["judges"]["retention"]["perspective"]
    assert by_judge["spectateur"]["status"] == "adopted" and by_judge["avocat"]["status"] == "refused"
    assert fake.calls == [] and forbidden == []


def test_get_learning_without_any_state_is_empty(tmp_path, isolated_cwd, monkeypatch):
    (tmp_path / "config.toml").write_text(_LEARN_TOML, encoding="utf-8")
    c, fake, forbidden = _learning_client(tmp_path, monkeypatch)

    data = c.get("/api/learning").json()

    assert data["weights"] is None and data["coach"] == [] and data["sync"]["last_sync"] is None and forbidden == []


def test_adopt_writes_the_perspective_in_config_toml_and_marks_the_proposal(tmp_path, isolated_cwd, monkeypatch):
    from clipper import config as config_mod
    from clipper.web import app as web_app

    _learning_state(tmp_path)
    c, fake, forbidden = _learning_client(tmp_path, monkeypatch)
    calls = []
    real = config_mod.write_config
    monkeypatch.setattr(web_app, "write_config", lambda *a, **k: (calls.append(a), real(*a, **k))[1])

    resp = c.post("/api/learning/coach/retention/1/adopt")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "adopted" and body["decided_by"] == "web" and body["decided_at"] and body["comments_lost"] is True
    assert len(calls) == 1
    written = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert written["jury"]["judges"]["retention"]["perspective"] == _PROPOSED.format(judge="retention")
    assert written["render"] == {"crf": 18} and written["web"]["token"] == "secret-tres-long" and written["mode"] == "review"
    after = c.get("/api/learning").json()["coach"]
    retention = next(p for p in after if p["judge"] == "retention")
    assert retention["status"] == "adopted" and retention["perspective_current"] == _PROPOSED.format(judge="retention")
    assert forbidden == []


def test_adopt_keeps_the_other_keys_of_the_judge_table(tmp_path, isolated_cwd, monkeypatch):
    _learning_state(tmp_path, toml=_LEARN_TOML + '[jury.judges.retention]\nmodel = "fast"\nperspective = "ancienne"\n')
    c, _fake, _forbidden = _learning_client(tmp_path, monkeypatch)

    assert c.post("/api/learning/coach/retention/1/adopt").status_code == 200

    judge = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["jury"]["judges"]["retention"]
    assert judge == {"model": "fast", "perspective": _PROPOSED.format(judge="retention")}


def test_refuse_marks_the_proposal_and_leaves_config_toml_alone(tmp_path, isolated_cwd, monkeypatch):
    _learning_state(tmp_path)
    c, _fake, _forbidden = _learning_client(tmp_path, monkeypatch)
    before = (tmp_path / "config.toml").read_bytes()

    resp = c.post("/api/learning/coach/retention/1/refuse")

    assert resp.status_code == 200 and resp.json()["status"] == "refused" and resp.json()["decided_by"] == "web"
    assert (tmp_path / "config.toml").read_bytes() == before


@pytest.mark.parametrize("verb", ["adopt", "refuse"])
def test_unknown_proposal_is_404(tmp_path, isolated_cwd, monkeypatch, verb):
    _learning_state(tmp_path)
    c, _fake, _forbidden = _learning_client(tmp_path, monkeypatch)
    assert c.post(f"/api/learning/coach/retention/7/{verb}").status_code == 404
    assert c.post(f"/api/learning/coach/inconnu/1/{verb}").status_code == 404


@pytest.mark.parametrize("judge, version", [("spectateur", 1), ("avocat", 2)])
def test_already_decided_proposal_is_409_and_config_toml_is_not_touched(tmp_path, isolated_cwd, monkeypatch, judge, version):
    _learning_state(tmp_path)
    c, _fake, _forbidden = _learning_client(tmp_path, monkeypatch)
    before = (tmp_path / "config.toml").read_bytes()

    assert c.post(f"/api/learning/coach/{judge}/{version}/adopt").status_code == 409
    assert c.post(f"/api/learning/coach/{judge}/{version}/refuse").status_code == 409
    assert (tmp_path / "config.toml").read_bytes() == before


def test_the_conformity_judge_is_never_adopted(tmp_path, isolated_cwd, monkeypatch):
    _learning_state(tmp_path)
    c, _fake, _forbidden = _learning_client(tmp_path, monkeypatch)
    assert c.post("/api/learning/coach/conformite/1/adopt").status_code == 409
    assert c.post("/api/learning/coach/conformite/1/refuse").status_code == 409


def test_adopting_twice_is_refused_the_second_time(tmp_path, isolated_cwd, monkeypatch):
    _learning_state(tmp_path)
    c, _fake, _forbidden = _learning_client(tmp_path, monkeypatch)
    assert c.post("/api/learning/coach/retention/1/adopt").status_code == 200
    assert c.post("/api/learning/coach/retention/1/adopt").status_code == 409


def _learning_screen_run(expression: str, data: dict) -> object:
    """Charge TOUT stats.js dans node (globales de l'interface simulees) puis evalue ``expression`` sur ``data``."""
    script = (
        'const fr = (n, d) => Number(n).toLocaleString("fr-FR", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });\n'
        'const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;");\n'
        'const fmtParis = (iso) => "PARIS " + iso;\nconst Screens = {};\nlet currentScreen = "";\n'
        'const icon = () => "";\nconst $ = () => null;\nconst $$ = () => [];\nconst api = async () => ({});\n'
        'const toast = () => {};\nconst toastError = () => {};\nconst confirmDialog = async () => true;\n'
        'const emptyState = () => "";\nconst renderCurrent = () => {};\nconst location = { hash: "#/stats" };\n'
        'const document = { addEventListener() {} };\nconst window = { addEventListener() {} };\n'
        + _static("screens", "stats.js")
        + '\nconst data = JSON.parse(process.argv[1]);\n'
        + f"process.stdout.write(JSON.stringify({expression}));"
    )
    return json.loads(_node_run(script, json.dumps(data)))


_FABRICATED = {
    "enabled": True,
    "links": {"counts": {"compte_a": {"linked": 3}}, "unlinked": [{"reason": "none"}, {"reason": "ambiguous"}, {"reason": "none"}]},
    "sync": {"last_sync": "2026-10-08T09:00:00+00:00", "last_error": {"at": "2026-10-08T09:30:00+00:00", "where": "coach", "message": "boom <b>"},
             "excluded": [{"account": "compte_b", "reason": "account_below_min"}, {"account": "compte_b", "reason": "account_below_min"}]},
    "weights": {"judges": {"retention": {"weight": 1.2, "agreement": 0.4, "clips": 8, "reason": None},
                           "conformite": {"weight": 1.0, "agreement": None, "clips": 8, "reason": "fixed"}}},
    "coach": [
        {"judge": "retention", "version": 1, "metric": {"before": 0.5, "after": 0.2, "cases": 5}, "status": "proposed",
         "perspective_current": "PERSPECTIVE EN PLACE", "perspective_proposed": "PERSPECTIVE PROPOSEE", "decided_at": None, "error": None},
        {"judge": "avocat", "version": 2, "metric": {"before": 0.4, "after": 0.3, "cases": 5}, "status": "refused",
         "perspective_current": "A", "perspective_proposed": "B", "decided_at": "2026-10-09T10:00:00+00:00", "error": None},
    ],
}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_the_stats_screen_renders_the_three_learning_blocks_on_a_fabricated_answer():
    html = _learning_screen_run("(statsUi.learning = data, statsLearningSection())", _FABRICATED)

    assert "Apprentissage" in html and "data-learning-state" in html and "data-learning-weights" in html and "data-learning-coach" in html
    # etat : dernier versement, erreur en rouge (echappee), reliés / non reliés par raison, exclus avec la raison
    assert "PARIS 2026-10-08T09:00:00+00:00" in html and 'class="reason bad" data-learning-error' in html and "boom &lt;b>" in html
    assert "<strong>3</strong>" in html and "<strong>2</strong> · aucun post du relevé ne correspond" in html
    assert "plusieurs posts correspondent" in html and "compte_b · compte sous le minimum de posts à vues" in html
    # poids : accord, cas, raison
    assert "retention" in html and "1,20" in html and "0,40" in html and "poids fixe" in html
    # coach : metrique avant -> apres, les deux perspectives, boutons seulement sur « proposee »
    assert "0,500 → 0,200 sur 5 cas" in html and "PERSPECTIVE EN PLACE" in html and "PERSPECTIVE PROPOSEE" in html
    assert 'data-learning-adopt="retention/1"' in html and 'data-learning-refuse="retention/1"' in html
    assert "data-learning-adopt=\"avocat/2\"" not in html and "Refusée" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_the_learning_section_says_so_when_nothing_is_known_yet():
    empty = {"enabled": False, "links": {"counts": {}, "unlinked": []}, "sync": {"last_sync": None, "last_error": None, "excluded": []},
             "weights": None, "coach": []}
    html = _learning_screen_run("(statsUi.learning = data, statsLearningSection())", empty)

    assert "Apprentissage désactivé" in html and "jamais" in html and "Pas encore de poids" in html and "Aucune proposition" in html
    assert "data-learning-error" not in html and "data-learning-adopt" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_a_failed_learning_read_is_shown_not_hidden():
    html = _learning_screen_run('(statsUi.learningError = new Error("illisible"), statsLearningSection())', _FABRICATED)
    assert "Lecture impossible" in html and "illisible" in html


_BREAKDOWN = {
    "n": 9, "min_n": 5, "skipped": 1,
    "groups": {
        "game": [
            {"key": "Zelda", "label": "Zelda", "n": 6, "median_views": 12345, "median_pct_watched": 0.456,
             "best": {"video_id": "v111", "clip_id": "03", "views": 99000}, "few": False},
            {"key": "inconnu", "label": "jeu inconnu", "n": 3, "median_views": 40, "median_pct_watched": None,
             "best": {"video_id": "v222", "clip_id": "01", "views": 77}, "few": True},
        ],
        "streamer": [{"key": "Alice <b>", "label": "Alice <b>", "n": 9, "median_views": 777, "median_pct_watched": None,
                      "best": {"video_id": "v111", "clip_id": "03", "views": 99000}, "few": False}],
        "hour": [{"key": "21", "label": "21 h", "n": 9, "median_views": 555, "median_pct_watched": None,
                  "best": {"video_id": "v111", "clip_id": "03", "views": 99000}, "few": False}],
        "account": [{"key": "compte_a", "label": "compte_a", "n": 9, "median_views": 333, "median_pct_watched": None,
                     "best": {"video_id": "v111", "clip_id": "03", "views": 99000}, "few": False}],
    },
}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_the_stats_screen_renders_the_four_breakdown_tables_with_few_and_best_clip_link():
    html = _learning_screen_run("(statsUi.learning = { ...data, breakdown: " + json.dumps(_BREAKDOWN) + " }, statsLearningSection())",
                                _FABRICATED)

    assert "Ce qui marche" in html
    for name in ("game", "streamer", "hour", "account"):
        assert f'data-breakdown="{name}"' in html
    for title in ("Jeu", "Streamer", "Heure", "Compte"):
        assert f">{title}<" in html
    # lignes telles que rendues par le serveur : n, vues médianes, part vue médiane, libellés échappés
    assert "Zelda" in html and "45,6 %" in html and ("12 345" in html or "12 345" in html)
    assert "21 h" in html and "compte_a" in html and "Alice &lt;b>" in html
    # meilleur clip : lien vers la fiche clip
    assert 'href="#/clips/v111"' in html and 'href="#/clips/v222"' in html
    # « trop peu pour conclure » seulement sur la ligne few, jamais masquée
    assert html.count("trop peu pour conclure") == 1 and "jeu inconnu" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
def test_the_breakdown_section_says_no_mature_stats_when_empty():
    empty = {"n": 0, "min_n": 5, "skipped": 0, "groups": {"game": [], "streamer": [], "hour": [], "account": []}}
    html = _learning_screen_run("(statsUi.learning = { ...data, breakdown: " + json.dumps(empty) + " }, statsLearningSection())",
                                _FABRICATED)

    assert "Ce qui marche" in html and "aucun relevé mûr" in html and "trop peu pour conclure" not in html


def test_the_breakdown_block_computes_no_median_in_the_page():
    js = _static("screens", "stats.js")
    block = js[js.index("function learningBreakdownBlock"):js.index("function statsLearningSection")]
    assert "median(" not in block.replace("median_views", "").replace("median_pct_watched", "")
    assert "sort(" not in block and "reduce(" not in block and "Math." not in block


def test_the_stats_screen_reads_learning_and_wires_adopt_and_refuse():
    js = _static("screens", "stats.js")
    assert '"/api/learning"' in js and "/api/learning/coach/" in js
    assert "data-learning-adopt" in js and "data-learning-refuse" in js and "statsLearningSection()" in js


# --------------------------------------------------------------------------
# Jury action 5/6 (SPEC-b0f3 R16) : grille « Gaming action », [moments] candidates,
# table [action], source de chaque moment dans la fiche vidéo
# --------------------------------------------------------------------------


def test_channels_form_offers_the_gaming_action_rubric():
    js = _static("screens", "channels.js")

    assert '["builtin:gaming-action", "Gaming action"]' in js
    for label in ("Standard", "Gaming", "Fichier personnalisé"):
        assert label in js, label
    assert 'info.kind' in js and '"gaming-action"]' in js    # libellé serveur repris pour un fichier identique


def test_rubric_label_for_the_builtin_gaming_action_value(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)

    info = client(tmp_path).get("/api/rubric-label", params={"path": "builtin:gaming-action"}).json()

    assert info["kind"] == "gaming-action"
    assert info["label"] == "Gaming action (builtin:gaming-action)"


def test_channel_rubric_label_for_a_file_identical_to_the_gaming_action_grid(tmp_path, isolated_cwd):
    _channels_setup(tmp_path, preset=_CH_PRESET + '\n[moments]\nrubric_path = "ma_grille.toml"\n')
    (tmp_path / "ma_grille.toml").write_bytes(_rubric_asset("rubric-gaming-action.toml"))

    rubric = client(tmp_path).get(f"/api/channels/{CH}").json()["rubric"]

    assert rubric == {"value": "ma_grille.toml", "kind": "gaming-action", "label": "Gaming action (ma_grille.toml)"}


def test_standard_and_gaming_rubric_labels_are_unchanged(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)

    assert c.get("/api/rubric-label", params={"path": "builtin"}).json()["label"] == "Standard (builtin)"
    gaming = c.get("/api/rubric-label", params={"path": "builtin:gaming"}).json()
    assert (gaming["kind"], gaming["label"]) == ("gaming", "Gaming (builtin:gaming)")


def test_channel_detail_documents_candidates_snap_and_the_action_section(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)

    body = client(tmp_path).get(f"/api/channels/{CH}").json()

    moments = body["defaults"]["moments"]
    assert moments["candidates"]["default"] == "transcript"
    assert moments["action_snap_seconds"]["default"] == 3
    action = body["defaults"]["action"]
    assert action["enabled"]["default"] is False
    assert action["window_seconds"]["default"] == 30 and action["window_seconds"]["comment"]
    assert body["effective"]["action"]["enabled"] is False


def test_channels_form_has_the_action_section_and_the_candidates_choice():
    js = _static("screens", "channels.js")

    assert 'section: "action"' in js and "Action (passages de jeu)" in js
    assert '"transcript+action"' in js and '"transcript"' in js
    assert 'key === "candidates"' in js


def test_put_channel_writes_moments_candidates_and_action_enabled_then_get_reads_them(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)
    c = client(tmp_path)

    resp = c.put(f"/api/channels/{CH}", json={"preset": {
        "channel": {"display_name": "Ma chaîne"},
        "moments": {"rubric_path": "builtin:gaming-action", "candidates": "transcript+action", "action_snap_seconds": 5},
        "action": {"enabled": True},
    }})

    assert resp.status_code == 200, resp.text
    saved = (tmp_path / "presets" / f"{CH}.toml").read_text(encoding="utf-8")
    assert 'candidates = "transcript+action"' in saved and "enabled = true" in saved
    body = c.get(f"/api/channels/{CH}").json()
    assert body["raw"]["moments"] == {
        "rubric_path": "builtin:gaming-action", "candidates": "transcript+action", "action_snap_seconds": 5}
    assert body["raw"]["action"] == {"enabled": True}
    assert body["effective"]["action"]["enabled"] is True
    assert body["rubric"]["kind"] == "gaming-action"


def test_saving_from_the_console_keeps_every_key_of_the_moments_and_action_tables(tmp_path, isolated_cwd):
    _channels_setup(tmp_path, preset=_CH_PRESET + (
        '\n[moments]\ncandidates = "transcript+action"\naction_snap_seconds = 2\n'
        '\n[action]\nenabled = true\nwindow_seconds = 20\nmin_score = 0.5\n'))
    c = client(tmp_path)
    raw = c.get(f"/api/channels/{CH}").json()["raw"]

    # la console renvoie le brut relu (sections du formulaire = brouillon issu du brut)
    assert c.put(f"/api/channels/{CH}", json={"preset": raw}).status_code == 200

    again = c.get(f"/api/channels/{CH}").json()["raw"]
    assert again["moments"] == {"candidates": "transcript+action", "action_snap_seconds": 2}
    assert again["action"] == {"enabled": True, "window_seconds": 20, "min_score": 0.5}


def test_put_channel_refuses_a_bad_action_value_naming_the_field(tmp_path, isolated_cwd):
    _channels_setup(tmp_path)

    resp = client(tmp_path).put(f"/api/channels/{CH}", json={"preset": {
        "channel": {"display_name": "Ma chaîne"}, "action": {"enabled": "oui"}}})

    assert resp.status_code == 422
    assert "[action] enabled" in resp.text


def test_channel_form_sections_include_action():
    from clipper.web import app as web_app

    assert "action" in web_app._CHANNEL_FORM_SECTIONS


def _jury_moment(i, source=None):
    jury = {"score": 70, "confidence": 80, "debated": False, "proposer": {"scores": {"emotion": 7}},
            "trace": {"rounds": [{"round": 1, "judges": {"a": {"scores": {"emotion": 7}, "confidence": 80}}}]}}
    return {"id": i, "start": 10.0 * i, "end": 10.0 * i + 30, "format": "letterbox", "scores": {"emotion": 7},
            "final_score": 70, "justification": "j", "hook_text": "h", "jury": jury,
            **({"source": source} if source else {})}


def test_video_jury_view_carries_the_source_of_each_moment_when_moments_json_has_it(tmp_path, isolated_cwd):
    _write_state(tmp_path, VIDEO_ID, status="done", steps={})
    _write_json(tmp_path / "workspace" / VIDEO_ID / "moments.json", {
        "video_id": VIDEO_ID, "selection": "jury", "jury": {"judges": ["a"]},
        "rubric": {"path": "r.toml", "weights": {"emotion": 4}, "min_score": 45},
        "moments": [_jury_moment(1, "transcript"), _jury_moment(2, "action")], "rejected": []})

    moments = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/jury").json()["moments"]

    assert [m["source"] for m in moments] == ["transcript", "action"]


def test_video_jury_view_has_no_source_key_for_an_old_moments_json(tmp_path, isolated_cwd):
    _write_state(tmp_path, VIDEO_ID, status="done", steps={})
    _write_json(tmp_path / "workspace" / VIDEO_ID / "moments.json", {
        "video_id": VIDEO_ID, "selection": "jury", "jury": {"judges": ["a"]},
        "rubric": {"path": "r.toml", "weights": {"emotion": 4}, "min_score": 45},
        "moments": [_jury_moment(1)], "rejected": []})

    moments = client(tmp_path).get(f"/api/videos/{VIDEO_ID}/jury").json()["moments"]

    assert "source" not in moments[0]


def test_video_sheet_shows_the_moment_source_only_when_present():
    js = _static("screens", "jury-radar.js")

    assert "m.source" in js and "passage d'action" in js and "transcription" in js


def test_clips_selection_bar_has_select_all_and_clear_buttons():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")
    assert "Tout sélectionner" in js and "data-clips-sel-all" in js
    assert "Vider la sélection" in js and "data-clips-sel-clear" in js


def test_clips_select_all_takes_every_filtered_clip_and_whole_series(tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent du PATH")
    harness = tmp_path / "h.js"
    harness.write_text("""
const fs = require("fs"), vm = require("vm");
const ctx = vm.createContext({ console, location: { hash: "" }, document: { addEventListener() {} }, window: { addEventListener() {} }, Screens: {} });
const src = fs.readFileSync(process.argv[2], "utf8").replace(/^const clipsUi = /m, "var clipsUi = ");
vm.runInContext(src, ctx);
const data = [
  { video_id: "v1", clip_id: "00", parts_total: 1, part: 1, publish_status: "à valider", channel: "a" },
  { video_id: "v1", clip_id: "01-p1", parts_total: 2, part: 1, publish_status: "à valider", channel: "a" },
  { video_id: "v1", clip_id: "01-p2", parts_total: 2, part: 2, publish_status: "approuvé", channel: "a" },
  { video_id: "v2", clip_id: "00", parts_total: 1, part: 1, publish_status: "à valider", channel: "b" },
  { video_id: "v3", clip_id: "00", parts_total: 1, part: 1, publish_status: "publié", channel: "a" },
];
ctx.clipsUi.data = data;
const keys = vm.runInContext("(d, ui) => Array.from(clipsAllKeys(d, ui)).sort()", ctx)(data, { filter: "à valider", channel: "a", video: "" });
process.stdout.write(JSON.stringify(keys));
""", encoding="utf-8")
    done = subprocess.run([node, str(harness), str(STATIC / "screens" / "clips.js")], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    # filtre « à valider » + style a : 00 et 01-p1 ; 01-p2 (approuvé) vient avec sa série ; v2 (style b) et v3 (publié) exclus ;
    # clipsAllKeys ne dépend pas de la page affichée (clipsUi.shown n'intervient pas).
    assert json.loads(done.stdout) == ["v1/00", "v1/01-p1", "v1/01-p2"]


def test_step_labels_follow_the_pipeline_order_and_name_every_step():
    js = (STATIC / "screens.js").read_text(encoding="utf-8")
    block = js[js.index("const STEP_LABELS = {"):js.index("};", js.index("const STEP_LABELS = {"))]
    keys = re.findall(r"\b([a-z_]+): \"", block)
    from clipper import pipeline
    assert keys == list(pipeline.STEPS)  # videos.js affiche les étapes dans cet ordre
# --------------------------------------------------------------------------
# Pause manuelle d'un compte (SPEC-f348 R3 c, R7.3-R7.5)
# --------------------------------------------------------------------------

PAUSED_AT = "2026-10-07T09:30:00+00:00"


def _pause_in_file(tmp_path, account_id, *, connected=True):
    """Pose une pause manuelle dans state/accounts.json (la case est decochee, la connexion reste verifiee)."""
    path = tmp_path / "state" / "accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data["accounts"]:
        if row["id"] == account_id:
            row.update(paused_at=PAUSED_AT, ready_to_publish=False)
            row["login"] = {"state": "connected" if connected else "expired",
                            "checked_at": "2026-10-07T08:00:00+00:00", "expires_at": None}
    path.write_text(json.dumps(data), encoding="utf-8")


def test_publish_accounts_expose_paused_at_and_a_paused_account_is_not_ready(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    _accounts_state(tmp_path, ready=(READY, SPARE))
    _pause_in_file(tmp_path, SPARE)

    rows = {a["id"]: a for a in client(tmp_path).get("/api/publish/accounts").json()["accounts"]}

    assert rows[SPARE]["paused_at"] == PAUSED_AT and rows[SPARE]["ready_to_publish"] is False
    assert rows[READY]["paused_at"] is None and rows[READY]["ready_to_publish"] is True


def test_approving_with_a_paused_account_is_a_409_saying_paused_and_approves_nothing(tmp_path, isolated_cwd):
    _publish_setup(tmp_path)
    _accounts_state(tmp_path, ready=(READY, SPARE))
    _pause_in_file(tmp_path, SPARE)

    resp = client(tmp_path).post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": SPARE})

    assert resp.status_code == 409 and "en pause" in resp.json()["detail"]
    assert json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8")) == []


def test_changing_the_account_of_a_publication_to_a_paused_one_is_a_409(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [_entry("01", "scheduled", slot_at=PUB_THU, account=READY)])
    _accounts_state(tmp_path, ready=(READY, SPARE))
    _pause_in_file(tmp_path, SPARE)
    c = client(tmp_path)

    moved = c.post(f"/api/publish/{CLIPS_VIDEO}/01/account", json={"account": SPARE})
    patched = c.patch(f"/api/publications/{CLIPS_VIDEO}/01", json={"account": SPARE})

    assert moved.status_code == 409 and "en pause" in moved.json()["detail"]
    assert patched.status_code == 409 and "en pause" in patched.json()["detail"]
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert entries[0]["account"] == READY


def test_scheduling_a_post_or_a_series_with_a_paused_account_is_a_409(tmp_path, isolated_cwd):
    config = _series_client(tmp_path)
    _pause_in_file(tmp_path, READY)
    c = TestClient(create_app(config=config))

    post = c.post("/api/publications", json={"video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY,
                                              "mode": "immediate"})
    series = c.post("/api/publications/series", json={
        "mode": "auto", "account": READY, "interval_hours": 2, "start_at": _soon(hours=1), "count": 2})

    assert post.status_code == 409 and "en pause" in post.json()["detail"]
    assert series.status_code == 409 and "en pause" in series.json()["detail"]
    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert {e["status"] for e in entries} == {"approved"}  # rien de programmé


def test_a_paused_connected_account_is_still_fetched_for_stats(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path, ready=(TT_ACCOUNT, TT_OTHER))
    _pause_in_file(tmp_path, TT_ACCOUNT)
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)
    c = client(tmp_path)

    listed = {a["account"]: a for a in c.get("/api/stats/tiktok").json()["accounts"]}
    one = c.post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT})
    everyone = c.post("/api/stats/tiktok/refresh")

    assert listed[TT_ACCOUNT]["ready"] is True and listed[TT_ACCOUNT]["paused_at"] == PAUSED_AT
    assert one.status_code == 200, one.text
    assert everyone.status_code == 200 and fetch.calls == [TT_ACCOUNT, TT_ACCOUNT, TT_OTHER]


def test_a_paused_account_with_an_expired_connection_is_not_fetched_for_stats(tmp_path, isolated_cwd, monkeypatch):
    _tt_accounts(tmp_path, ready=(TT_ACCOUNT, TT_OTHER))
    _pause_in_file(tmp_path, TT_ACCOUNT, connected=False)
    fetch = FakeFetch()
    monkeypatch.setattr(tiktok_mod, "fetch_stats", fetch)

    resp = client(tmp_path).post("/api/stats/tiktok/refresh", json={"account": TT_ACCOUNT})

    assert resp.status_code == 409 and "expirée" in resp.json()["detail"] and fetch.calls == []
# --------------------------------------------------------------------------
# Suppression des clips sélectionnés (TASK-2322) : POST /api/clips/delete
# --------------------------------------------------------------------------


def _delete_part(tmp_path, clip_id, part, total, video_id="aaaaaaaaaaa"):
    out = _purge_clip(tmp_path, video_id=video_id, clip_id=clip_id)
    (out / f"{clip_id}.json").write_text(json.dumps(
        {"video_id": video_id, "clip_id": clip_id, "part": part, "parts_total": total}), encoding="utf-8")


def _delete_publish(tmp_path, entries):
    pub = tmp_path / "state" / "publish"
    pub.mkdir(parents=True, exist_ok=True)
    (pub / "style.json").write_text(json.dumps(entries), encoding="utf-8")


def _delete_body(*pairs):
    return {"clips": [{"video_id": v, "clip_id": c} for v, c in pairs]}


def test_clips_delete_removes_chosen_clips_and_reports_freed_bytes(tmp_path, isolated_cwd):
    out = _purge_clip(tmp_path, clip_id="01-p1")
    _purge_clip(tmp_path, clip_id="02-p1")

    resp = client(tmp_path).post("/api/clips/delete", json=_delete_body(("aaaaaaaaaaa", "01-p1")))

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == [{"video_id": "aaaaaaaaaaa", "clip_id": "01-p1"}]
    assert body["refused"] == []
    assert body["freed_bytes"] > 50
    assert not (out / "01-p1.mp4").exists() and not (out / "01-p1.json").exists()
    assert (out / "02-p1.mp4").is_file() and (out / "02-p1.json").is_file()


def test_clips_delete_published_clip_keeps_its_sidecar_and_reports_video_deleted(tmp_path, isolated_cwd):
    out = _purge_clip(tmp_path, clip_id="02-p1")
    (out / "02-p1.jpg").write_bytes(b"z" * 10)
    sidecar = (out / "02-p1.json").read_bytes()
    _delete_publish(tmp_path, [{"video_id": "aaaaaaaaaaa", "clip_id": "02-p1", "status": "published"}])

    resp = client(tmp_path).post("/api/clips/delete", json=_delete_body(("aaaaaaaaaaa", "02-p1")))

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == [] and body["refused"] == []
    assert body["video_deleted"] == [{"video_id": "aaaaaaaaaaa", "clip_id": "02-p1"}]
    assert body["freed_bytes"] == 60
    assert [p.name for p in out.iterdir()] == ["02-p1.json"] and (out / "02-p1.json").read_bytes() == sidecar


def test_clips_delete_mixed_series_reports_each_part_in_its_own_list(tmp_path, isolated_cwd):
    for n in (1, 2):
        _delete_part(tmp_path, f"01-p{n}", n, 2)
    _delete_publish(tmp_path, [{"video_id": "aaaaaaaaaaa", "clip_id": "01-p2", "status": "published"}])

    body = client(tmp_path).post("/api/clips/delete", json=_delete_body(("aaaaaaaaaaa", "01-p1"))).json()

    assert body["deleted"] == [{"video_id": "aaaaaaaaaaa", "clip_id": "01-p1"}]
    assert body["video_deleted"] == [{"video_id": "aaaaaaaaaaa", "clip_id": "01-p2"}]
    assert [p.name for p in (tmp_path / "output" / "aaaaaaaaaaa").iterdir()] == ["01-p2.json"]


def _published_without_video(tmp_path):
    _clips_setup(tmp_path)
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "published", published_at="2026-10-02T18:00:00+00:00", post_id="123",
               post_url="https://tiktok.example/123"),
        _entry("03", "failed", error="quota depasse"),
    ])
    (tmp_path / "output" / CLIPS_VIDEO / "01.mp4").unlink()


def test_get_clips_marks_a_published_clip_whose_video_was_deleted(tmp_path, isolated_cwd):
    _published_without_video(tmp_path)

    resp = client(tmp_path).get("/api/clips", params={"video_id": CLIPS_VIDEO})

    assert resp.status_code == 200
    clips = {c["clip_id"]: c for c in resp.json()}
    assert clips["01"]["video_deleted"] is True and clips["01"]["publish_status"] == "published"
    assert clips["01"]["post_id"] == "123"
    assert clips["02"]["video_deleted"] is False


def test_media_of_a_clip_without_video_is_a_clean_404(tmp_path, isolated_cwd):
    _published_without_video(tmp_path)
    c = client(tmp_path)

    assert c.get(f"/media/clip/{CLIPS_VIDEO}/01").status_code == 404
    assert c.get(f"/media/clip/{CLIPS_VIDEO}/01/thumbnail").status_code == 404


def test_publication_screens_survive_a_published_clip_without_video(tmp_path, isolated_cwd):
    _published_without_video(tmp_path)
    c = client(tmp_path)

    for url in ("/api/publish", "/api/stats/tiktok"):
        assert c.get(url).status_code == 200, url


def test_rerender_and_approve_of_a_clip_without_video_are_refused_clearly(tmp_path, isolated_cwd, monkeypatch):
    from clipper import worker

    _published_without_video(tmp_path)
    monkeypatch.setattr(worker, "enqueue", lambda *a, **kw: pytest.fail("rerender ne doit pas etre mis en file"))
    c = client(tmp_path)

    rerender = c.post(f"/api/clips/{CLIPS_VIDEO}/01/rerender")
    approve = c.post(f"/api/clips/{CLIPS_VIDEO}/01/approve", json={"account": "ab12cd"})
    retitle = c.patch(f"/api/clips/{CLIPS_VIDEO}/01", json={"screen_title": "Autre", "confirm": True})

    for resp in (rerender, approve, retitle):
        assert resp.status_code == 409 and "vidéo supprimée" in resp.json()["detail"]


@pytest.mark.parametrize("status", ["scheduled", "approved"])
def test_clips_delete_refuses_scheduled_or_pending_clips_with_reason(tmp_path, isolated_cwd, status):
    out = _purge_clip(tmp_path, clip_id="02-p1")
    _delete_publish(tmp_path, [{"video_id": "aaaaaaaaaaa", "clip_id": "02-p1", "status": status}])

    resp = client(tmp_path).post("/api/clips/delete", json=_delete_body(("aaaaaaaaaaa", "02-p1")))

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == [] and body["freed_bytes"] == 0
    assert body["refused"][0]["clip"] == {"video_id": "aaaaaaaaaaa", "clip_id": "02-p1"}
    assert "02-p1" in body["refused"][0]["reason"]
    assert (out / "02-p1.mp4").is_file()


def test_clips_delete_is_all_or_nothing_per_series_but_other_clips_still_go(tmp_path, isolated_cwd):
    out = _purge_clip(tmp_path, clip_id="09-p1")
    for n in (1, 2):
        _delete_part(tmp_path, f"01-p{n}", n, 2)
    _delete_publish(tmp_path, [{"video_id": "aaaaaaaaaaa", "clip_id": "01-p2", "status": "scheduled"}])

    resp = client(tmp_path).post("/api/clips/delete", json=_delete_body(("aaaaaaaaaaa", "01-p1"), ("aaaaaaaaaaa", "09-p1")))

    body = resp.json()
    assert [c["clip_id"] for c in body["deleted"]] == ["09-p1"]
    assert [r["clip"]["clip_id"] for r in body["refused"]] == ["01-p1"]
    assert (out / "01-p1.mp4").is_file() and (out / "01-p2.mp4").is_file()
    assert not (out / "09-p1.mp4").exists()


def test_clips_delete_one_chosen_part_deletes_the_whole_series_once(tmp_path, isolated_cwd):
    out = tmp_path / "output" / "aaaaaaaaaaa"
    for n in (1, 2, 3):
        _delete_part(tmp_path, f"01-p{n}", n, 3)

    resp = client(tmp_path).post("/api/clips/delete", json=_delete_body(("aaaaaaaaaaa", "01-p2"), ("aaaaaaaaaaa", "01-p3")))

    assert [c["clip_id"] for c in resp.json()["deleted"]] == ["01-p1", "01-p2", "01-p3"]
    assert list(out.glob("*")) == []


def test_clips_delete_unknown_clip_is_refused_explicitly_not_silently(tmp_path, isolated_cwd):
    _purge_clip(tmp_path, clip_id="01-p1")

    body = client(tmp_path).post("/api/clips/delete", json=_delete_body(("aaaaaaaaaaa", "zz"))).json()

    assert body["deleted"] == [] and "zz" in body["refused"][0]["reason"]


def test_clips_delete_empty_selection_and_bad_ids_are_400_or_422(tmp_path, isolated_cwd):
    c = client(tmp_path)

    assert c.post("/api/clips/delete", json={"clips": []}).status_code == 400
    assert c.post("/api/clips/delete", json=_delete_body(("../x", "01"))).status_code in (400, 404, 422)


def test_clips_selection_bar_has_a_danger_delete_button_with_confirmation_and_toast():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")
    assert "Supprimer la sélection" in js and "data-clips-sel-delete" in js and "btn-bad" in js
    assert "/api/clips/delete" in js
    assert "Irréversible" in js and "confirmDialog" in js


def test_clips_js_explains_published_clips_keep_their_stats_and_handles_video_deleted():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")
    assert "Les clips publiés gardent leurs infos (stats), seule la vidéo est supprimée." in js
    assert "video_deleted" in js and "Vidéo supprimée" in js


# --------------------------------------------------------------------------
# TASK-5a7b750462c4 : « Supprimé de la plateforme »
# --------------------------------------------------------------------------


def _removed_setup(tmp_path):
    _fable_setup(tmp_path, clips=("01", "02", "03"))
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "published", tiktok_state="scheduled_on_tiktok", post_id="71", tiktok_publish_at="2026-10-12T18:00:00+00:00",
               published_at="2026-10-08T08:00:00+00:00"),
        _entry("02", "published", tiktok_state="published", post_id="72", published_at="2026-10-07T08:00:00+00:00"),
        _entry("03", "approved"),
    ])
    return client(tmp_path)


@pytest.mark.parametrize("clip", ["01", "02"])
def test_post_removed_marks_a_published_entry_and_lists_it_apart(tmp_path, isolated_cwd, clip):
    c = _removed_setup(tmp_path)

    resp = c.post(f"/api/publish/{CLIPS_VIDEO}/{clip}/removed", json={"reason": "mal cadré"})

    assert resp.status_code == 200 and resp.json()["status"] == "removed_from_platform"
    assert next(e for e in _publish_file(tmp_path, "ma_chaine") if e["clip_id"] == clip)["removed_reason"] == "mal cadré"
    body = c.get("/api/publications").json()
    assert clip not in [p["clip_id"] for p in body["publications"]]  # ni dans la file ni au calendrier
    removed = body["removed_from_platform"]
    assert [(r["clip_id"], r["removed_reason"]) for r in removed] == [(clip, "mal cadré")]
    assert removed[0]["tiktok_status"] == "removed_from_platform" and removed[0]["editable"] is False
    assert c.post(f"/api/clips/{CLIPS_VIDEO}/{clip}/approve", json={"account": READY}).status_code == 409


def test_post_removed_without_body_and_refusals_are_409(tmp_path, isolated_cwd):
    c = _removed_setup(tmp_path)

    assert c.post(f"/api/publish/{CLIPS_VIDEO}/01/removed").status_code == 200  # raison facultative
    refused = c.post(f"/api/publish/{CLIPS_VIDEO}/03/removed")  # approuvée, pas publiée
    again = c.post(f"/api/publish/{CLIPS_VIDEO}/01/removed")

    assert refused.status_code == 409 and "approved" in refused.json()["detail"]
    assert again.status_code == 409


def test_post_removed_is_refused_while_the_worker_drives_the_entry(tmp_path, isolated_cwd):
    _fable_setup(tmp_path, clips=("01",))
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "scheduled", slot_at="2026-10-02T18:00:00+00:00",
                                                  in_progress_since="2026-10-08T08:00:00+00:00")])

    resp = client(tmp_path).post(f"/api/publish/{CLIPS_VIDEO}/01/removed")

    assert resp.status_code == 409 and "en cours" in resp.json()["detail"]


def test_publish_screen_has_the_removed_from_platform_button_with_confirmation():
    js = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")

    assert "/removed" in js and "Supprimé de la plateforme" in js and "data-removed" in js
    assert "removed_from_platform" in js
    assert "confirmDialog" in js[js.index("async function pubMarkRemoved"):js.index("async function pubMarkRemoved") + 900]


# --------------------------------------------------------------------------
# Alerte « 0 vue à 24 h » sur le tableau de bord (TASK-974e) : lecture seule, erreur visible
# --------------------------------------------------------------------------


def test_dashboard_carries_the_zero_view_alerts(tmp_path, monkeypatch):
    from clipper import learning

    alerts = {"hours": 24.0, "no_reading": [], "accounts": [{"account": "compte_a", "level": "post", "posts": [
        {"video_id": "VVVVVVVVVVV", "clip_id": "01", "post_id": "7000000000000000101", "posted_at": "2026-10-08T09:00:00+00:00",
         "views": 0, "read_at": "2026-10-10T12:00:00+00:00"}]}]}
    monkeypatch.setattr(learning, "zero_view_alerts", lambda now, **kwargs: alerts)

    assert _dashboard(tmp_path)["zero_views"] == alerts


def test_dashboard_zero_view_error_is_visible_and_the_rest_stays(tmp_path, monkeypatch):
    from clipper import learning

    def broken(now, **kwargs):
        raise learning.LearningError("[learning] zero_view_alert_hours invalide : 0")

    monkeypatch.setattr(learning, "zero_view_alerts", broken)
    data = _dashboard(tmp_path)

    assert data["zero_views"] is None
    assert "zero_view_alert_hours invalide" in data["zero_views_error"]
    assert "queued" in data and "running" in data


# --------------------------------------------------------------------------
# Fiche par clip (TASK-fa7619ccf83a) : GET /api/clips/<video>/<clip>/sheet, lecture seule
# --------------------------------------------------------------------------

SHEET_URL = f"https://www.youtube.com/watch?v={CLIPS_VIDEO}"


def _sheet_sidecar(clip_id="01", **extra):
    return _clip_sidecar(clip_id, source_url=SHEET_URL, source_title="Le passage source", start=12.5, end=36.5,
                         **extra)


def _sheet(tmp_path, clip_id="01"):
    return client(tmp_path).get(f"/api/clips/{CLIPS_VIDEO}/{clip_id}/sheet")


def test_clip_sheet_gathers_the_clip_video_source_publication_and_jury(tmp_path, isolated_cwd):
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _sheet_sidecar("01", scores={"hook": 9}, reason="Annonce forte"))
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "published", published_at="2026-10-02T16:00:00+00:00",
               post_url="https://www.tiktok.com/@ab12cd/video/1", post_id="1"),
    ])

    resp = _sheet(tmp_path)

    assert resp.status_code == 200
    sheet = resp.json()
    assert sheet["clip"]["title"] == "Titre 01"
    assert sheet["clip"]["scores"] == {"hook": 9}
    assert sheet["clip"]["reason"] == "Annonce forte"
    assert sheet["video"]["title"] == "Le passage source"
    assert sheet["video"]["deleted"] is False
    assert sheet["video"]["video_url"] == f"/media/clip/{CLIPS_VIDEO}/01"
    assert sheet["video"]["passage_url"] == f"{SHEET_URL}&t=12s"
    assert sheet["clip"]["publish_status"] == "published"
    assert sheet["clip"]["post_url"] == "https://www.tiktok.com/@ab12cd/video/1"
    assert sheet["clip"]["published_at_paris"] == "2026-10-02T18:00:00+02:00"
    assert sheet["clip"]["account"] == "ab12cd"
    assert "jury_confidence" in sheet["clip"]


def test_clip_sheet_with_deleted_video_keeps_the_sheet_and_says_so(tmp_path, isolated_cwd):
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _sheet_sidecar("01"))
    (tmp_path / "output" / CLIPS_VIDEO / "01.mp4").unlink()
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "published", post_url="https://www.tiktok.com/@x/video/2")])

    resp = _sheet(tmp_path)

    assert resp.status_code == 200
    video = resp.json()["video"]
    assert video["deleted"] is True
    assert video["video_url"] is None
    assert video["note"] == "vidéo supprimée, fiche conservée"
    assert resp.json()["clip"]["post_url"] == "https://www.tiktok.com/@x/video/2"


def test_clip_sheet_never_published_clip_has_no_invented_publication(tmp_path, isolated_cwd):
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    sidecar = _sheet_sidecar("01")
    del sidecar["score"]
    _write_clip(tmp_path, CLIPS_VIDEO, sidecar)

    sheet = _sheet(tmp_path).json()

    assert sheet["clip"]["publish_status"] == "à valider"
    assert sheet["clip"]["post_url"] is None
    assert sheet["clip"]["published_at_paris"] is None
    assert sheet["clip"]["score"] is None  # absente = inconnue, jamais 0 (ADR-ad2e)
    assert sheet["stats"] is None


def test_clip_sheet_removed_from_platform_keeps_its_post_link(tmp_path, isolated_cwd):
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _sheet_sidecar("01"))
    _write_publish(tmp_path, "ma_chaine", [
        _entry("01", "removed_from_platform", post_url="https://www.tiktok.com/@ab12cd/video/3"),
    ])

    clip = _sheet(tmp_path).json()["clip"]

    assert clip["publish_status"] == "removed_from_platform"
    assert clip["tiktok_status"] == "removed_from_platform"
    assert clip["post_url"] == "https://www.tiktok.com/@ab12cd/video/3"


def test_clip_sheet_reports_stats_error_instead_of_zero_views(tmp_path, isolated_cwd, monkeypatch):
    from clipper.tiktok import TikTokError

    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _sheet_sidecar("01"))
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "published", post_id="77")])

    def no_history(account, post_id, *, config=None):
        raise TikTokError(f"vidéo {post_id} introuvable dans les relevés du compte {account}")

    monkeypatch.setattr("clipper.tiktok.video_detail", no_history)

    stats = _sheet(tmp_path).json()["stats"]

    assert stats["views"] is None
    assert "introuvable" in stats["error"]


def test_clip_sheet_unknown_clip_is_404(tmp_path, isolated_cwd):
    resp = _sheet(tmp_path, clip_id="99")

    assert resp.status_code == 404
    assert "clip introuvable" in resp.json()["detail"]


def test_clips_screen_links_each_clip_to_its_sheet():
    js = (STATIC / "screens" / "clips.js").read_text(encoding="utf-8")

    assert "#/clip/" in js


def test_clip_sheet_screen_is_served_and_routed(tmp_path, isolated_cwd):
    resp = client(tmp_path).get("/static/screens/clip.js")
    index = client(tmp_path).get("/").text

    assert resp.status_code == 200
    assert "screens/clip.js" in index
    assert '"clip"' in (STATIC / "app.js").read_text(encoding="utf-8")


def test_clip_sheet_gives_the_jury_rounds_with_each_judge_argument(tmp_path, isolated_cwd):
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _sheet_sidecar("01", moment_id="m1"))
    moment = {"id": "m1", "jury": {"confidence": 72, "trace": {"rounds": [
        {"round": 1, "judges": {"claude": {"score": 8, "confidence": 72, "argument": "Hook fort"}}}]}}}
    _write_json(tmp_path / "workspace" / CLIPS_VIDEO / "moments.json", {"moments": [moment]})

    jury = _sheet(tmp_path).json()["jury"]

    assert jury["confidence"] == 72
    assert jury["rounds"][0]["round"] == 1
    assert jury["rounds"][0]["judges"]["claude"]["argument"] == "Hook fort"


def test_clip_sheet_without_moments_json_has_unknown_jury(tmp_path, isolated_cwd):
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _sheet_sidecar("01"))

    jury = _sheet(tmp_path).json()["jury"]

    assert jury == {"confidence": None, "judges": None, "rounds": None}


# --------------------------------------------------------------------------
# Fiche clip lisible (TASK-d8af48198d84) : issues en objets, dates de Paris, compte par son label,
# libellés qui ne se coupent pas au milieu d'un mot. Tests sans réseau : la fiche est rendue par node.
# --------------------------------------------------------------------------

_NODE_SHEET = pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")

_SHEET_JS_NAMES = (
    ("screens/clip.js", ["sheetVal", "sheetRow", "sheetLink", "clipSheetVideoHtml", "clipSheetJuryHtml",
                         "clipSheetScoresHtml", "clipSheetStatsHtml", "clipSheetDate", "clipSheetAccountHtml",
                         "clipSheetIssuesHtml", "clipSheetHtml"]),
    ("screens/clips.js", ["CLIP_STATUS", "clipStatus", "clipSeconds"]),
    ("ui.js", ["CLIPPER_TZ", "fmtParis"]),
)
_SHEET_JS_FIXTURE = """
const sheetFixture = (over = {}) => ({
  clip: Object.assign({ clip_id: '01', video_id: 'v1', title: 'Titre 01', publish_status: 'published',
    qa_status: 'passed', issues: [], slot_at_paris: null, published_at_paris: null, account: null,
    post_url: null, scores: null, start: null, end: null, duration: null, layout: null, caption: null,
    score: null, reason: null, hook_text: null, screen_title: 'Titre 01', publish_error: null }, over.clip || {}),
  video: { title: 'Source', deleted: false, video_url: '/media/clip/v1/01', passage_url: null, source_url: null },
  jury: { confidence: null, judges: null, rounds: null },
  stats: null,
});
"""


def _sheet_html(sheet, accounts):
    out = _run_js(_SHEET_JS_NAMES, f"clipSheetHtml(sheetFixture({json.dumps(sheet)}), {json.dumps(accounts)})",
                  preamble=_SHEET_JS_FIXTURE)
    return out


@_NODE_SHEET
def test_clip_sheet_reads_qa_issues_as_objects_never_object_object():
    html = _sheet_html({"clip": {"qa_status": "passed", "issues": [
        {"type": "sous-titres", "detail": "hors zone", "severity": "warning", "source": "qa"},
    ]}}, [])

    assert "[object Object]" not in html
    assert "sous-titres" in html and "hors zone" in html and "warning" in html


@_NODE_SHEET
def test_clip_sheet_qa_without_issues_has_no_empty_list_of_problems():
    html = _sheet_html({"clip": {"qa_status": "passed", "issues": []}}, [])

    assert "passed" in html
    assert "[object Object]" not in html


@_NODE_SHEET
def test_clip_sheet_shows_slot_and_publication_dates_in_paris_short_french_format():
    html = _sheet_html({"clip": {"slot_at_paris": "2026-10-08T09:00:00+02:00",
                                 "published_at_paris": "2026-10-08T08:07:46.763778+02:00"}}, [])

    assert "8 oct." in html and "09:00" in html and "08:07" in html
    assert "2026-10-08T" not in html and "763778" not in html


@_NODE_SHEET
def test_clip_sheet_dates_that_are_missing_say_inconnu():
    html = _sheet_html({"clip": {"slot_at_paris": None, "published_at_paris": None}}, [])

    assert html.count("inconnu") >= 2


@_NODE_SHEET
def test_clip_sheet_account_shows_its_label_with_the_id_in_secondary():
    html = _sheet_html({"clip": {"account": "ab12cd"}}, [{"id": "ab12cd", "label": "ClipperFou"}])

    assert "ClipperFou" in html and "ab12cd" in html
    assert html.index("ClipperFou") < html.index("ab12cd")


@_NODE_SHEET
def test_clip_sheet_account_that_no_longer_exists_says_inconnu_not_its_id_alone():
    html = _sheet_html({"clip": {"account": "ab12cd"}}, [])

    assert "ab12cd" in html
    assert "inconnu" in html
    assert "ClipperFou" not in html


@_NODE_SHEET
def test_clip_sheet_without_account_says_inconnu():
    html = _sheet_html({"clip": {"account": None}}, [{"id": "ab12cd", "label": "ClipperFou"}])

    assert "ClipperFou" not in html


def test_clip_sheet_loads_the_accounts_list_to_name_the_account():
    js = (STATIC / "screens" / "clip.js").read_text(encoding="utf-8")

    assert 'api("/api/accounts")' in js


def test_clip_sheet_row_labels_never_break_inside_a_word():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    label_rule = css[css.index(".sheet-row > span:first-child"):]
    label_rule = label_rule[:label_rule.index("}")]

    assert "overflow-wrap: normal" in label_rule
    assert "flex: 0 0 auto" in label_rule


def test_clip_sheet_root_does_not_reuse_the_veille_sheet_class():
    # « .sheet » est le style de la fiche Veille (veille.css) : la fiche clip a sa propre classe, sinon elle
    # se range en deux colonnes étroites (TASK-d8af48198d84).
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'class="clip-sheet" data-body' in html
    assert ".clip-sheet" not in (STATIC / "screens" / "veille.css").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# TASK-6ef1 : dossiers lus dans les réglages, plus de « presets » / « state » en dur
# --------------------------------------------------------------------------


def test_event_stream_watches_the_configured_publish_state_dir(tmp_path, isolated_cwd):
    import asyncio

    from clipper.web.app import _event_stream

    pub = tmp_path / "ailleurs" / "pub"
    config = Config(
        mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={"web": {"host": "127.0.0.1", "port": 8000, "token": "", "sse_poll_interval_s": 0.05},
                   "publish": {"state_dir": str(pub)}},
    )

    async def _run() -> str:
        agen = _event_stream(config).__aiter__()

        async def _touch_soon() -> None:
            await asyncio.sleep(0.15)
            pub.mkdir(parents=True, exist_ok=True)
            (pub / "ma_chaine.json").write_text("{}", encoding="utf-8")

        asyncio.create_task(_touch_soon())
        return await asyncio.wait_for(agen.__anext__(), timeout=2.0)

    event = json.loads((asyncio.run(_run()))[len("data: "):].strip())

    assert (event["kind"], event["id"]) == ("publish", "ma_chaine")


def test_channels_route_lists_the_configured_presets_dir(tmp_path, isolated_cwd):
    from clipper import channel as channel_mod
    from clipper.web import app as app_mod

    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "ma_chaine.toml").write_text('[channel]\nname = "ma_chaine"\n', encoding="utf-8")
    config = Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                    _sections={"watch": {"presets_dir": str(styles), "base_config": str(tmp_path / "base.toml")}})
    try:
        app = create_app(config=config)
        assert app_mod._PRESETS_DIR == str(styles)
        assert app_mod._BASE_CONFIG == str(tmp_path / "base.toml")
        assert channel_mod.list_channels(app_mod._PRESETS_DIR) == ["ma_chaine"]
    finally:
        app_mod._PRESETS_DIR, app_mod._BASE_CONFIG = "presets", "config.toml"


def test_learning_api_with_a_truncated_outcomes_journal_is_422_naming_the_file(tmp_path, isolated_cwd, monkeypatch):
    (tmp_path / "config.toml").write_text(_LEARN_TOML, encoding="utf-8")
    journal = tmp_path / "state" / "outcomes.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"kind": "stats", "video_id": "VVVVVVVVVVV"\n', encoding="utf-8")
    c, fake, forbidden = _learning_client(tmp_path, monkeypatch)

    resp = c.get("/api/learning")

    assert resp.status_code == 422 and "outcomes.jsonl" in resp.json()["detail"]


def _watch_config(tmp_path, **publish_section) -> Config:
    state = tmp_path / "state"
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                  _sections={"worker": {"queue_path": str(state / "queue.json")},
                             "publish": publish_section})


def test_publish_state_dir_spelled_absolute_emits_each_file_once(tmp_path, isolated_cwd):
    from clipper.web.app import _scan_watched, _watched_state_roots

    state = tmp_path / "state"
    (state / "publish").mkdir(parents=True)
    (state / "publish" / "chaine.json").write_text("{}", encoding="utf-8")
    spelled = str(state / ".." / "state" / "publish")  # même dossier, écrit autrement
    config = _watch_config(tmp_path, state_dir=spelled)

    found = _scan_watched(tmp_path / "workspace", _watched_state_roots(config))

    assert [(kind, id_) for _, kind, id_ in found] == [("publish", "chaine")]


def test_publish_state_dir_outside_the_queue_folder_emits_one_element_with_its_fixed_kind(tmp_path, isolated_cwd):
    from clipper.web.app import _scan_watched, _watched_state_roots

    pub2 = tmp_path / "state" / "pub2"
    pub2.mkdir(parents=True)
    (pub2 / "chaine.json").write_text("{}", encoding="utf-8")
    config = _watch_config(tmp_path, state_dir=str(pub2))

    found = _scan_watched(tmp_path / "workspace", _watched_state_roots(config))

    assert [(kind, id_) for _, kind, id_ in found] == [("publish", "chaine")]

# --------------------------------------------------------------------------
# TASK-28dac454afef : fiche clip lisible (historique sans doublons, « Envoyé à TikTok le », valeurs qui débordent plus)
# Tests sans réseau : TikTok est remplacé par un faux relevé, la fiche est rendue par node.
# --------------------------------------------------------------------------


def _history_row(fetched_at, views, likes, comments):
    return {"fetched_at": fetched_at, "views": views, "likes": likes, "comments": comments}


def test_clip_sheet_history_keeps_one_row_per_change_and_always_the_last(tmp_path, isolated_cwd, monkeypatch):
    # 6 relevés : 3 identiques consécutifs (06:06 à 06:12), puis une hausse, puis deux relevés identiques
    # (le dernier doit rester). Attendu : 06:06, 06:20, 06:30, 06:40 : 4 lignes, ordre chronologique.
    history = [
        _history_row("2026-10-07T06:06:00+00:00", 1056, 40, 3),
        _history_row("2026-10-07T06:09:00+00:00", 1056, 40, 3),
        _history_row("2026-10-07T06:12:00+00:00", 1056, 40, 3),
        _history_row("2026-10-07T06:20:00+00:00", 1100, 41, 3),
        _history_row("2026-10-07T06:30:00+00:00", 1200, 45, 4),
        _history_row("2026-10-07T06:40:00+00:00", 1200, 45, 4),
    ]
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _sheet_sidecar("01"))
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "published", post_id="77")])
    monkeypatch.setattr("clipper.tiktok.video_detail", lambda account, post_id, *, config=None: {
        "views": 1200, "likes": 45, "history": history})

    stats = _sheet(tmp_path).json()["stats"]

    assert [row["fetched_at"] for row in stats["history"]] == [
        "2026-10-07T06:06:00+00:00", "2026-10-07T06:20:00+00:00",
        "2026-10-07T06:30:00+00:00", "2026-10-07T06:40:00+00:00"]
    assert stats["history"][-1]["views"] == 1200 and stats["history"][-1]["comments"] == 4


def test_clip_sheet_history_keeps_unknown_values_as_null_never_invented(tmp_path, isolated_cwd, monkeypatch):
    history = [
        _history_row("2026-10-07T06:06:00+00:00", None, None, None),
        _history_row("2026-10-07T06:09:00+00:00", None, None, None),
        _history_row("2026-10-07T06:12:00+00:00", 5, None, None),
    ]
    _write_state(tmp_path, CLIPS_VIDEO, channel="ma_chaine")
    _write_clip(tmp_path, CLIPS_VIDEO, _sheet_sidecar("01"))
    _write_publish(tmp_path, "ma_chaine", [_entry("01", "published", post_id="77")])
    monkeypatch.setattr("clipper.tiktok.video_detail", lambda account, post_id, *, config=None: {
        "views": 5, "likes": None, "history": history})

    stats = _sheet(tmp_path).json()["stats"]

    assert [row["fetched_at"] for row in stats["history"]] == [
        "2026-10-07T06:06:00+00:00", "2026-10-07T06:12:00+00:00"]
    assert stats["history"][0]["views"] is None and stats["history"][0]["likes"] is None


def test_clip_sheet_names_the_publication_date_as_sent_to_tiktok_static():
    js = (STATIC / "screens" / "clip.js").read_text(encoding="utf-8")

    assert "Envoyé à TikTok le" in js
    assert "Publié le" not in js
    assert "Créneau" in js


@_NODE_SHEET
def test_clip_sheet_shows_sent_to_tiktok_not_published_and_keeps_the_slot():
    html = _sheet_html({"clip": {"slot_at_paris": "2026-10-07T21:00:00+02:00",
                                 "published_at_paris": "2026-10-06T20:35:00+02:00"}}, [])

    assert "Envoyé à TikTok le" in html
    assert "Publié le" not in html
    assert "Créneau" in html and "7 oct." in html and "6 oct." in html


def test_clip_sheet_grid_panels_can_shrink_so_values_never_overflow_the_panel():
    # Un panneau de grille garde min-width auto par défaut : son contenu le fait déborder du bord droit.
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    panel_rule = css[css.index(".sheet-grid > *"):]
    panel_rule = panel_rule[:panel_rule.index("}")]

    assert "min-width: 0" in panel_rule
    assert "overflow-wrap: anywhere" in css[css.index(".sheet-row {"):].split("}")[0]


# --------------------------------------------------------------------------
# TASK-486c : répartition automatique (3/5), SPEC-78dc R8 : GET / compute / PUT / validate
# + événement SSE. Plans écrits à la main (R7), clips de _publish_setup, jamais le réseau.
# --------------------------------------------------------------------------

_REP_PARIS = ZoneInfo("Europe/Paris")


def _rep_day(days: int = 2) -> str:
    return (_dt.now(_REP_PARIS) + _td(days=days)).date().isoformat()


def _rep_slot(day: str, hhmm: str) -> str:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=_REP_PARIS).isoformat()


def _rep_line(day: str, hhmm: str, clip: str, **extra) -> dict:
    return {"slot_at": _rep_slot(day, hhmm), "video_id": CLIPS_VIDEO, "clip_id": clip, "score": 80.0, "bonus": 0,
            "bonus_reason": "no_stats", "adjusted": 80.0, "source_key": f"vod:{CLIPS_VIDEO}", "game_name": None,
            "source_from": "vod", "exploration": False, "prime": False, **extra}


def _rep_put_line(day: str, hhmm: str, clip: str) -> dict:
    return {"slot_at": _rep_slot(day, hhmm), "video_id": CLIPS_VIDEO, "clip_id": clip}


def _rep_path(tmp_path, day: str) -> Path:
    return tmp_path / "state" / "repartition" / f"{day}.json"


def _rep_write(tmp_path, day: str, accounts, status: str = "proposed", **extra) -> None:
    plan = {"day": day, "computed_at": "2026-10-09T20:00:00+02:00", "computed_by": "worker", "status": status,
            "accounts": [{"account": acc, "label": acc, "slots": [], "lines": lines, "notes": []}
                         for acc, lines in accounts],
            "pool": 6, "excluded": [], "notes": [], "validated_at": None, "created": [], **extra}
    path = _rep_path(tmp_path, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan), encoding="utf-8")


def _rep_read(tmp_path, day: str) -> dict:
    return json.loads(_rep_path(tmp_path, day).read_text(encoding="utf-8"))


def _rep_client(tmp_path, ready=(READY, SPARE), *, repartition=None, **tiktok_settings) -> TestClient:
    _publish_setup(tmp_path, slots=False)
    _accounts_state(tmp_path, ready=ready)
    config = Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                    _sections={"tiktok": {"max_posts_per_day": 10, "min_gap_minutes": 0, **tiktok_settings},
                               **({"repartition": repartition} if repartition else {})})
    return TestClient(create_app(config=config))


def _rep_pause(tmp_path, account: str) -> None:
    path = tmp_path / "state" / "accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data["accounts"]:
        if row["id"] == account:
            row["paused_at"] = "2026-10-08T10:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")


def _rep_moments(tmp_path, *exploration_ids: int) -> None:
    moments = [{"id": i, "exploration": True} for i in exploration_ids]
    (tmp_path / "workspace" / CLIPS_VIDEO / "moments.json").write_text(json.dumps({"moments": moments}), encoding="utf-8")


def _rep_scheduled(tmp_path) -> list[dict]:
    path = tmp_path / "state" / "publish" / "ma_chaine.json"
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return sorted((e for e in entries if e["status"] == "scheduled"), key=lambda e: (e["account"], e["slot_at"]))


def _rep_two_accounts(tmp_path, day: str) -> None:
    _rep_write(tmp_path, day, [
        (READY, [_rep_line(day, "10:00", "01"), _rep_line(day, "13:00", "02")]),
        (SPARE, [_rep_line(day, "10:30", "03"), _rep_line(day, "13:30", "04")])])


def _rep_past() -> str:
    return (_dt.now(_REP_PARIS) - _td(hours=2)).replace(microsecond=0).isoformat()


def test_repartition_get_describes_each_line_and_gives_the_pool(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "10:00", "01"), _rep_line(day, "13:00", "02")])])

    resp = c.get("/api/repartition", params={"day": day})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["day"] == day and data["status"] == "proposed" and data["computed_by"] == "worker"
    first, second = data["accounts"][0]["lines"]
    assert first["screen_title"] == "Titre 01" and first["thumbnail_url"] == f"/media/clip/{CLIPS_VIDEO}/01/thumbnail"
    assert first["publish_at_paris"] == _rep_slot(day, "10:00") and first["refusal"] is None
    assert second["clip_id"] == "02" and second["refusal"] is None
    pool = data["accounts"][0]["pool"]  # le vivier est rendu sous chaque compte (TASK-757c7728eb9b)
    assert {u["clip_id"] for u in pool} == {"01", "02", "03", "04", "05", "06"}
    assert {"screen_title", "thumbnail_url", "score", "channel"} <= set(pool[0])
    assert data["pool_size"] == 6


def test_repartition_get_gives_each_line_its_own_refusal_from_preview_series(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [{**_rep_line(day, "10:00", "01"), "slot_at": _rep_past()},
                                        _rep_line(day, "13:00", "02")])])

    lines = c.get("/api/repartition", params={"day": day}).json()["accounts"][0]["lines"]

    assert lines[0]["refusal"] and lines[1]["refusal"] is None


def test_repartition_get_refuses_a_slot_that_breaks_the_account_gap(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path, min_gap_minutes=180), _rep_day()
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "10:00", "01"), _rep_line(day, "11:00", "02")])])

    lines = c.get("/api/repartition", params={"day": day}).json()["accounts"][0]["lines"]

    assert lines[0]["refusal"] is None and "180" in lines[1]["refusal"]


def test_repartition_get_defaults_to_tomorrow_in_paris_and_says_when_there_is_no_plan(tmp_path, isolated_cwd):
    c = _rep_client(tmp_path)
    tomorrow = _rep_day(1)

    empty = c.get("/api/repartition")
    _rep_write(tmp_path, tomorrow, [(READY, [_rep_line(tomorrow, "10:00", "01")])])
    found = c.get("/api/repartition")

    assert empty.status_code == 200 and empty.json()["day"] == tomorrow and empty.json()["status"] == "absent"
    assert empty.json()["accounts"] == [] and empty.json()["enabled"] is True
    assert found.json()["status"] == "proposed" and found.json()["accounts"][0]["lines"][0]["clip_id"] == "01"


def test_repartition_get_rejects_a_day_that_is_not_a_date(tmp_path, isolated_cwd):
    resp = _rep_client(tmp_path).get("/api/repartition", params={"day": "demain"})

    assert resp.status_code == 422 and "AAAA-MM-JJ" in resp.json()["detail"]


def test_repartition_get_shows_an_error_plan_with_its_message(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    path = _rep_path(tmp_path, day)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"day": day, "computed_at": "2026-10-09T20:00:00+02:00", "computed_by": "worker",
                                "status": "error", "error": {"type": "PublishError", "message": "boum"}}), encoding="utf-8")

    data = c.get("/api/repartition", params={"day": day}).json()

    assert data["status"] == "error" and data["error"]["message"] == "boum" and data["accounts"] == []


def test_repartition_get_on_a_validated_plan_does_not_preview_lines_already_created(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "10:00", "01")])], status="validated",
               validated_at="2026-10-09T21:00:00+02:00", created=[{"account": READY, "video_id": CLIPS_VIDEO, "clip_id": "01"}])
    assert c.post("/api/publications", json={"video_id": CLIPS_VIDEO, "clip_id": "01", "account": READY,
                                             "mode": "scheduled", "publish_at": _rep_slot(day, "10:00")}).status_code == 201

    data = c.get("/api/repartition", params={"day": day}).json()

    assert data["status"] == "validated" and data["accounts"][0]["lines"][0]["refusal"] is None
    assert data["accounts"][0]["lines"][0]["screen_title"] == "Titre 01"


def test_repartition_compute_writes_a_web_plan_and_refuses_a_validated_one(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()

    resp = c.post("/api/repartition/compute", json={"day": day})

    assert resp.status_code == 200, resp.text
    assert resp.json()["computed_by"] == "web" and resp.json()["status"] == "proposed"
    assert [a["account"] for a in resp.json()["accounts"]] == [READY, SPARE]
    assert _rep_read(tmp_path, day)["computed_by"] == "web"
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "10:00", "01")])], status="validated")
    before = _rep_path(tmp_path, day).read_text(encoding="utf-8")

    refused = c.post("/api/repartition/compute", json={"day": day})

    assert refused.status_code == 409 and "validé" in refused.json()["detail"]
    assert _rep_path(tmp_path, day).read_text(encoding="utf-8") == before


def test_repartition_compute_replaces_a_proposed_plan_the_user_had_edited(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "09:07", "06")])])

    resp = c.post("/api/repartition/compute", json={"day": day})

    assert resp.status_code == 200
    assert "09:07" not in json.dumps(_rep_read(tmp_path, day)) and resp.json()["computed_by"] == "web"


def test_repartition_put_changes_a_clip_an_hour_and_removes_a_line(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)

    resp = c.put(f"/api/repartition/{day}", json={"accounts": [
        {"account": READY, "lines": [_rep_put_line(day, "10:00", "05"), _rep_put_line(day, "15:15", "02")]},
        {"account": SPARE, "lines": [_rep_put_line(day, "10:30", "03")]}]})

    assert resp.status_code == 200, resp.text
    ready, spare = _rep_read(tmp_path, day)["accounts"]
    assert [(line["clip_id"], line["slot_at"]) for line in ready["lines"]] == [
        ("05", _rep_slot(day, "10:00")), ("02", _rep_slot(day, "15:15"))]
    assert [line["clip_id"] for line in spare["lines"]] == ["03"]
    assert [line["clip_id"] for line in resp.json()["accounts"][0]["lines"]] == ["05", "02"]
    assert resp.json()["accounts"][0]["lines"][0]["screen_title"] == "Titre 05"
    assert _rep_read(tmp_path, day)["status"] == "proposed"


def test_repartition_put_leaves_an_account_that_is_not_in_the_body_untouched(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)

    resp = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": []}]})

    assert resp.status_code == 200
    ready, spare = _rep_read(tmp_path, day)["accounts"]
    assert ready["lines"] == [] and [line["clip_id"] for line in spare["lines"]] == ["03", "04"]


def test_repartition_put_describes_the_line_again_from_the_clip_not_from_the_page(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _series_scored(tmp_path, {"05": 91})
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "10:00", "01")])])

    c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [
        {**_rep_put_line(day, "19:00", "05"), "score": 1, "bonus": 99, "exploration": True, "prime": False}]}]})

    line = _rep_read(tmp_path, day)["accounts"][0]["lines"][0]
    assert line["score"] == 91 and line["bonus"] == 0 and line["bonus_reason"] == "no_stats" and line["adjusted"] == 91
    assert line["exploration"] is False and line["prime"] is True
    assert line["source_key"] == f"vod:{CLIPS_VIDEO}" and line["source_from"] == "vod" and line["game_name"] is None


def test_repartition_put_accepts_a_third_clip_of_one_source_with_a_visible_warning(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [])])

    resp = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [
        _rep_put_line(day, "09:00", "01"), _rep_put_line(day, "12:00", "02"), _rep_put_line(day, "15:00", "03")]}]})

    assert resp.status_code == 200, resp.text
    lines = resp.json()["accounts"][0]["lines"]
    assert [line["warning"] for line in lines[:2]] == [None, None]
    assert "2" in lines[2]["warning"] and "source" in lines[2]["warning"]
    assert _rep_read(tmp_path, day)["accounts"][0]["lines"][2]["warning"] == lines[2]["warning"]


def test_repartition_put_counts_the_posts_already_planned_that_day_for_the_source_cap(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [])])
    assert c.post("/api/publications", json={"video_id": CLIPS_VIDEO, "clip_id": "06", "account": READY,
                                             "mode": "scheduled", "publish_at": _rep_slot(day, "08:00")}).status_code == 201

    lines = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [
        _rep_put_line(day, "11:00", "01"), _rep_put_line(day, "14:00", "02")]}]}).json()["accounts"][0]["lines"]

    assert lines[0]["warning"] is None and "source" in lines[1]["warning"]


def test_repartition_put_warns_about_a_second_exploration_clip_and_an_evening_one(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_moments(tmp_path, 1, 2, 3)
    _rep_write(tmp_path, day, [(READY, []), (SPARE, [])])

    resp = c.put(f"/api/repartition/{day}", json={"accounts": [
        {"account": READY, "lines": [_rep_put_line(day, "10:00", "01"), _rep_put_line(day, "12:00", "04")]},
        {"account": SPARE, "lines": [_rep_put_line(day, "11:00", "02"), _rep_put_line(day, "19:00", "03")]}]})

    assert resp.status_code == 200, resp.text
    ready, spare = resp.json()["accounts"]
    assert ready["lines"][0]["exploration"] is True and ready["lines"][0]["warning"] is None
    assert ready["lines"][1]["exploration"] is False and ready["lines"][1]["warning"] is None
    assert spare["lines"][0]["exploration"] is True and "exploration" in spare["lines"][0]["warning"]
    assert spare["lines"][1]["prime"] is True and "exploration" in spare["lines"][1]["warning"]


def test_repartition_put_keeps_the_refusal_of_a_line_that_breaks_a_cap(tmp_path, isolated_cwd):
    c = _rep_client(tmp_path)
    day = _dt.now(_REP_PARIS).date().isoformat()  # un créneau passé n'est sur le jour du plan que si le plan est celui d'aujourd'hui
    _rep_write(tmp_path, day, [(READY, [])])

    resp = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [
        {"slot_at": _rep_slot(day, "00:00"), "video_id": CLIPS_VIDEO, "clip_id": "01"}]}]})

    assert resp.status_code == 200 and resp.json()["accounts"][0]["lines"][0]["refusal"]


def test_repartition_put_is_refused_on_a_validated_plan(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "10:00", "01")])], status="validated")
    before = _rep_path(tmp_path, day).read_text(encoding="utf-8")

    resp = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": []}]})

    assert resp.status_code == 409 and "validé" in resp.json()["detail"]
    assert _rep_path(tmp_path, day).read_text(encoding="utf-8") == before


@pytest.mark.parametrize("body,status", [
    ({"accounts": [{"account": "inconnu", "lines": []}]}, 422),
    ({"accounts": [{"account": READY, "lines": [
        {"slot_at": "2026-10-12T10:00:00", "video_id": CLIPS_VIDEO, "clip_id": "01"}]}]}, 422),
    ({"accounts": [{"account": READY, "lines": [
        {"slot_at": "2026-10-12T10:00:00+02:00", "video_id": CLIPS_VIDEO, "clip_id": "99"}]}]}, 404),
])
def test_repartition_put_refuses_a_malformed_body(tmp_path, isolated_cwd, body, status):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "10:00", "01")])])
    before = _rep_path(tmp_path, day).read_text(encoding="utf-8")

    resp = c.put(f"/api/repartition/{day}", json=body)

    assert resp.status_code == status and _rep_path(tmp_path, day).read_text(encoding="utf-8") == before


def test_repartition_put_refuses_the_same_clip_twice_and_an_unknown_day(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, []), (SPARE, [])])

    twice = c.put(f"/api/repartition/{day}", json={"accounts": [
        {"account": READY, "lines": [_rep_put_line(day, "10:00", "01")]},
        {"account": SPARE, "lines": [_rep_put_line(day, "11:00", "01")]}]})
    absent = c.put(f"/api/repartition/{_rep_day(5)}", json={"accounts": []})

    assert twice.status_code == 422 and "01" in twice.json()["detail"]
    assert absent.status_code == 404


def test_repartition_validate_creates_exactly_one_scheduled_entry_per_line(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)

    resp = c.post(f"/api/repartition/{day}/validate")

    assert resp.status_code == 200, resp.text
    entries = _rep_scheduled(tmp_path)
    assert [(e["account"], e["clip_id"], e["publish_mode"]) for e in entries] == [
        (READY, "01", "scheduled"), (READY, "02", "scheduled"), (SPARE, "03", "scheduled"), (SPARE, "04", "scheduled")]
    assert [datetime.fromisoformat(e["slot_at"]) for e in entries] == [
        datetime.fromisoformat(_rep_slot(day, hhmm)) for hhmm in ("10:00", "13:00", "10:30", "13:30")]
    plan = _rep_read(tmp_path, day)
    assert plan["status"] == "validated" and plan["validated_at"] and len(plan["created"]) == 4
    assert plan["created"][0] == {"account": READY, "video_id": CLIPS_VIDEO, "clip_id": "01"}
    assert resp.json()["status"] == "validated" and len(resp.json()["created"]) == 4


def test_repartition_validate_twice_does_not_create_a_second_set(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)
    c.post(f"/api/repartition/{day}/validate")

    again = c.post(f"/api/repartition/{day}/validate")

    assert again.status_code == 409 and "validé" in again.json()["detail"] and len(_rep_scheduled(tmp_path)) == 4


def test_repartition_validate_a_paused_account_is_refused_explicitly_and_creates_nothing_for_it(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)
    _rep_pause(tmp_path, SPARE)

    resp = c.post(f"/api/repartition/{day}/validate")

    assert resp.status_code == 409 and "pause" in resp.json()["detail"]
    assert [e["account"] for e in _rep_scheduled(tmp_path)] == [READY, READY]
    plan = _rep_read(tmp_path, day)
    assert plan["status"] == "proposed" and "pause" in plan["last_error"]
    assert [x["clip_id"] for x in plan["created"]] == ["01", "02"]
    assert [x["clip_id"] for x in resp.json()["created"]] == ["01", "02"]


def test_repartition_validate_failing_on_the_second_account_cancels_that_account_only(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish as publish_lib

    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)
    real, calls = publish_lib.create_post, []

    def flaky(*args, **kwargs):
        calls.append(kwargs.get("account"))
        if len(calls) == 4:  # 2 lignes du premier compte, 1re ligne du second, echec sur sa 2e
            raise publish_lib.PublishError("TikTok indisponible")
        return real(*args, **kwargs)

    monkeypatch.setattr(publish_lib, "create_post", flaky)

    resp = c.post(f"/api/repartition/{day}/validate")

    assert resp.status_code == 409 and "TikTok indisponible" in resp.json()["detail"]
    assert [(e["account"], e["clip_id"]) for e in _rep_scheduled(tmp_path)] == [(READY, "01"), (READY, "02")]
    plan = _rep_read(tmp_path, day)
    assert plan["status"] == "proposed" and "TikTok indisponible" in plan["last_error"]
    assert [x["clip_id"] for x in plan["created"]] == ["01", "02"] and plan["validated_at"] is None
    monkeypatch.setattr(publish_lib, "create_post", real)

    retry = c.post(f"/api/repartition/{day}/validate")

    assert retry.status_code == 200, retry.text
    assert [(e["account"], e["clip_id"]) for e in _rep_scheduled(tmp_path)] == [
        (READY, "01"), (READY, "02"), (SPARE, "03"), (SPARE, "04")]
    final = _rep_read(tmp_path, day)
    assert final["status"] == "validated" and len(final["created"]) == 4 and final.get("last_error") is None


def test_repartition_validate_refuses_an_account_whose_line_breaks_a_cap_and_creates_nothing_for_it(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "10:00", "01")]),
                               (SPARE, [{**_rep_line(day, "10:30", "03"), "slot_at": _rep_past()},
                                        _rep_line(day, "13:30", "04")])])

    resp = c.post(f"/api/repartition/{day}/validate")

    assert resp.status_code == 409 and "03" in resp.json()["detail"]
    assert [(e["account"], e["clip_id"]) for e in _rep_scheduled(tmp_path)] == [(READY, "01")]
    assert _rep_read(tmp_path, day)["status"] == "proposed"


def test_repartition_validate_never_creates_an_immediate_publication(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)

    c.post(f"/api/repartition/{day}/validate")

    entries = json.loads((tmp_path / "state" / "publish" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert entries and {e["publish_mode"] for e in entries} == {"scheduled"}
    assert {e["status"] for e in entries} == {"scheduled"}


def test_repartition_validate_refuses_an_absent_or_error_plan(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    absent = c.post(f"/api/repartition/{day}/validate")
    path = _rep_path(tmp_path, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"day": day, "status": "error", "error": {"type": "X", "message": "boum"}}), encoding="utf-8")

    erreur = c.post(f"/api/repartition/{day}/validate")

    assert absent.status_code == 404
    assert erreur.status_code == 409 and "boum" in erreur.json()["detail"]


def test_repartition_state_dir_is_watched_and_emits_one_repartition_event_per_file(tmp_path, isolated_cwd):
    from clipper.web.app import _scan_watched, _watched_state_roots

    plans = tmp_path / "state" / "repartition"
    plans.mkdir(parents=True)
    (plans / "2026-10-11.json").write_text("{}", encoding="utf-8")
    config = Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                    _sections={"worker": {"queue_path": str(tmp_path / "state" / "queue.json")}})

    found = _scan_watched(tmp_path / "workspace", _watched_state_roots(config))

    assert [(kind, id_) for _, kind, id_ in found] == [("repartition", "2026-10-11")]


def test_repartition_state_dir_elsewhere_keeps_its_kind(tmp_path, isolated_cwd):
    from clipper.web.app import _scan_watched, _watched_state_roots

    plans = tmp_path / "ailleurs"
    plans.mkdir()
    (plans / "2026-10-11.json").write_text("{}", encoding="utf-8")
    config = Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                    _sections={"worker": {"queue_path": str(tmp_path / "state" / "queue.json")},
                               "repartition": {"state_dir": str(plans)}})

    found = _scan_watched(tmp_path / "workspace", _watched_state_roots(config))

    assert [(kind, id_) for _, kind, id_ in found] == [("repartition", "2026-10-11")]


def test_repartition_put_refuses_an_account_whose_publications_are_already_created(tmp_path, isolated_cwd, monkeypatch):
    from clipper import publish as publish_lib

    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)
    real, calls = publish_lib.create_post, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise publish_lib.PublishError("TikTok indisponible")
        return real(*args, **kwargs)

    monkeypatch.setattr(publish_lib, "create_post", flaky)
    assert c.post(f"/api/repartition/{day}/validate").status_code == 409

    resp = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": []}]})

    assert resp.status_code == 409 and READY in resp.json()["detail"]
    assert [x["clip_id"] for x in _rep_read(tmp_path, day)["accounts"][0]["lines"]] == ["01", "02"]


def test_repartition_validate_refuses_a_plan_without_any_line(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, []), (SPARE, [])])

    resp = c.post(f"/api/repartition/{day}/validate")

    assert resp.status_code == 409 and "aucune ligne" in resp.json()["detail"]
    assert _rep_read(tmp_path, day)["status"] == "proposed" and _rep_scheduled(tmp_path) == []


# --------------------------------------------------------------------------
# Écran Publication : section « Plan de demain » (TASK-b645, SPEC-78dc R9)
# --------------------------------------------------------------------------

_REP_NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")
_REP_DAY = "2026-10-10"

_REP_STUBS = """
globalThis.__handlers = {};
globalThis.document = { addEventListener(name, fn) { (globalThis.__handlers[name] = globalThis.__handlers[name] || []).push(fn); } };
const Screens = {}; let currentScreen = "publish";
const emptyState = (i, t, x) => `<empty>${t} ${x}</empty>`;
const calls = [], toasts = [], errors = []; let renders = 0, netOk = true, hold = false, release = null;
const RESP = %RESP%;
const renderCurrent = () => { renders++; };
const jsonBody = (method, payload) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
const toast = (o) => toasts.push(o); const toastError = (t, e) => errors.push([t, e.message]);
const netGuard = async () => netOk;
const api = (url, opts) => {
  const method = (opts && opts.method) || "GET";
  calls.push({ url, method, body: opts && opts.body ? JSON.parse(opts.body) : null });
  const answer = () => { const r = RESP[method + " " + url]; if (r && r.__error) throw new Error(r.__error); return r === undefined ? {} : r; };
  if (hold && method !== "GET") { hold = false; return new Promise((res, rej) => { release = () => { try { res(answer()); } catch (e) { rej(e); } }; }); }
  return Promise.resolve().then(answer);
};
// Faux DOM : $$("[data-x-y]") rend un element par balise portant cet attribut, avec son dataset et ses gestionnaires.
const __els = {};
const $$ = (sel) => {
  if (__els[sel]) return __els[sel];
  const attr = sel.slice(1, -1), key = attr.slice(5).replace(/-(\\w)/g, (m, c) => c.toUpperCase());
  const re = new RegExp("<(\\\\w+)([^>]*?) " + attr + "(?:=\\"([^\\"]*)\\")?([^>]*)>", "g");
  const found = []; let m;
  while ((m = re.exec(globalThis.__html))) found.push({ tag: m[1], dataset: { [key]: m[3] === undefined ? "" : m[3] }, value: "", onclick: null, onchange: null });
  return (__els[sel] = found);
};
const $ = (sel) => $$(sel)[0] || null;
"""

_REP_ACC = "acc_a"
_REP_UNIT = {"video_id": "vid00000001", "clip_id": "01", "clip_ids": ["01"], "channel": "ma_chaine", "score": 80.0,
             "parts_total": 1, "screen_title": "Pool un", "title": "t", "validated": True,
             "thumbnail_url": "/media/clip/vid00000001/01/thumbnail"}


def _rep_pline(hhmm, clip, **extra):
    base = {"slot_at": f"{_REP_DAY}T{hhmm}:00+02:00", "publish_at_paris": f"{_REP_DAY}T{hhmm}:00+02:00",
            "video_id": "vid00000001", "clip_id": clip, "screen_title": f"Titre {clip}",
            "thumbnail_url": f"/media/clip/vid00000001/{clip}/thumbnail", "score": 72.5, "bonus": 1.5,
            "bonus_reason": "4 posts, médiane 12 000 vues, référence 7 500", "adjusted": 74.0,
            "source_key": "jeu:foo", "game_name": "Foo", "source_from": "veille", "exploration": False,
            "prime": False, "refusal": None, "warning": None}
    base.update(extra)
    return base


def _rep_plan(lines=None, **top):
    lines = lines if lines is not None else [_rep_pline("10:00", "01"), _rep_pline("19:00", "02", prime=True)]
    plan = {"day": _REP_DAY, "status": "proposed", "enabled": True, "computed_by": "worker",
            "computed_at": "2026-10-09T20:00:05+02:00", "validated_at": None, "created": [], "excluded": [],
            "pool_size": 3, "notes": ["aucun relevé récent"],
            "accounts": [{"account": _REP_ACC, "label": "Compte A", "notes": ["1 créneau(x) sans clip : vivier insuffisant"],
                          "pool": [_REP_UNIT, {**_REP_UNIT, "clip_id": "09", "clip_ids": ["09"], "screen_title": "Pool neuf"}],
                          "slots": [{"slot_at": f"{_REP_DAY}T10:00:00+02:00", "prime": False},
                                    {"slot_at": f"{_REP_DAY}T19:00:00+02:00", "prime": True},
                                    {"slot_at": f"{_REP_DAY}T21:00:00+02:00", "prime": True}],
                          "lines": lines}]}
    plan.update(top)
    return plan


def _rep_js(expr, plan=None, responses=None, setup=""):
    """Évalue publish.js sous node (bouchons ci-dessus) avec ``pubRep.data = plan`` puis ``expr``."""
    source = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    stubs = _REP_STUBS.replace("%RESP%", json.dumps(responses or {}))
    script = (_JS_PRELUDE + stubs + source + f"\npubRep.data = {json.dumps(plan)}; pubRep.day = {json.dumps(_REP_DAY)};\n"
              "globalThis.__html = ''; const __render = () => (globalThis.__html = repHtml());\n"
              + setup + f"\n(async () => {{ const out = await ({expr}); console.log(JSON.stringify(out)); }})();")
    return json.loads(_node_run(script))


@_REP_NODE
def test_plan_section_renders_one_row_per_slot_with_its_details():
    html = _rep_js("repHtml()", _rep_plan(lines=[
        _rep_pline("10:00", "01"),
        _rep_pline("19:00", "02", prime=True, exploration=True, game_name=None, source_from="vod", source_key="vod:vid00000001"),
    ]))

    assert "Plan de demain" in html and "10 octobre" in html
    assert html.count("data-rep-row") == 3                                  # 2 lignes + le créneau de 21:00 sans clip
    assert "10:00" in html and "19:00" in html and "21:00" in html           # heure de Paris
    assert "/media/clip/vid00000001/01/thumbnail" in html and "Titre 01" in html
    assert "72,5" in html and "+1,5" in html and 'title="4 posts, médiane 12 000 vues, référence 7 500"' in html
    assert "Foo" in html and "jeu inconnu : VOD" in html
    assert html.count("exploration</span>") == 1 and html.count("soir</span>") == 1
    assert "aucun clip disponible" in html and "Compte A" in html
    assert "aucun relevé récent" in html and "vivier insuffisant" in html     # notes telles quelles
    assert "proposé" in html


@_REP_NODE
def test_plan_section_shows_refusal_in_red_and_warning_in_orange():
    html = _rep_js("repHtml()", _rep_plan(lines=[
        _rep_pline("10:00", "01", refusal="créneau trop proche"),
        _rep_pline("19:00", "02", warning="plus de 2 clips de la même source"),
    ]))

    assert re.search(r'class="[^"]*\bbad\b[^"]*"[^>]*>[^<]*créneau trop proche', html)
    assert re.search(r'class="[^"]*\bwarn\b[^"]*"[^>]*>[^<]*plus de 2 clips de la même source', html)


@_REP_NODE
def test_plan_section_states_validated_error_disabled_and_absent():
    validated = _rep_js("repHtml()", _rep_plan(status="validated", validated_at="2026-10-09T21:10:00+02:00"))
    assert "validé le" in validated and "21:10" in validated
    assert re.search(r"data-rep-validate[^>]*disabled", validated)
    assert "data-rep-remove" not in validated and "data-rep-time" not in validated   # plus modifiable

    failed = _rep_js("repHtml()", _rep_plan(status="error", accounts=[], error={"message": "grille impossible"}))
    assert "erreur" in failed and "grille impossible" in failed and "data-rep-compute" in failed

    off = _rep_js("repHtml()", _rep_plan(enabled=False))
    assert "désactivé" in off and "data-rep-compute" not in off and "data-rep-validate" not in off

    absent = _rep_js("repHtml()", {"day": _REP_DAY, "status": "absent", "enabled": True, "accounts": []})
    assert "Aucun plan" in absent and "data-rep-compute" in absent
    assert re.search(r"data-rep-validate[^>]*disabled", absent)

    loading = _rep_js("repHtml()", None)
    assert "Plan de demain" in loading


@_REP_NODE
def test_validate_button_needs_at_least_one_acceptable_line():
    ok = _rep_js("repHtml()", _rep_plan(lines=[_rep_pline("10:00", "01", refusal="non"), _rep_pline("19:00", "02")]))
    assert not re.search(r"data-rep-validate[^>]*disabled", ok)

    refused = _rep_js("repHtml()", _rep_plan(lines=[_rep_pline("10:00", "01", refusal="non")]))
    assert re.search(r"data-rep-validate[^>]*disabled", refused)

    empty = _rep_js("repHtml()", _rep_plan(lines=[]))
    assert re.search(r"data-rep-validate[^>]*disabled", empty)


@_REP_NODE
def test_remove_button_puts_the_account_without_that_line():
    answer = _rep_plan(lines=[_rep_pline("19:00", "02")])
    calls = _rep_js("(async () => { __render(); repWire({}); $$('[data-rep-remove]')[0].onclick(); await pubRep.busyPromise; return calls; })()",
                    _rep_plan(), {f"PUT /api/repartition/{_REP_DAY}": answer})

    put = [c for c in calls if c["method"] == "PUT"]
    assert len(put) == 1 and put[0]["url"] == f"/api/repartition/{_REP_DAY}"
    assert put[0]["body"] == {"accounts": [{"account": _REP_ACC, "lines": [
        {"slot_at": f"{_REP_DAY}T19:00:00+02:00", "video_id": "vid00000001", "clip_id": "02"}]}]}


@_REP_NODE
def test_change_hour_puts_the_new_paris_instant_and_change_clip_puts_the_pool_clip():
    out = _rep_js("""(async () => {
      __render(); repWire({});
      const time = $$('[data-rep-time]')[1]; time.value = '2026-10-10T14:30'; time.onchange();
      await pubRep.busyPromise;
      const clip = $$('[data-rep-clip]')[0]; clip.value = 'vid00000001/09'; clip.onchange();
      await pubRep.busyPromise;
      return calls.filter((c) => c.method === 'PUT').map((c) => c.body.accounts[0].lines);
    })()""", _rep_plan(), {f"PUT /api/repartition/{_REP_DAY}": _rep_plan()})

    assert out[0] == [{"slot_at": f"{_REP_DAY}T10:00:00+02:00", "video_id": "vid00000001", "clip_id": "01"},
                      {"slot_at": f"{_REP_DAY}T12:30:00.000Z", "video_id": "vid00000001", "clip_id": "02"}]  # 14:30 Paris, été
    assert out[1][0] == {"slot_at": f"{_REP_DAY}T10:00:00+02:00", "video_id": "vid00000001", "clip_id": "09"}
    assert out[1][1]["clip_id"] == "02"


@_REP_NODE
def test_a_slot_without_clip_offers_the_pool_and_puts_the_chosen_clip():
    out = _rep_js("""(async () => {
      __render(); repWire({});
      const clip = $$('[data-rep-clip]')[2]; clip.value = 'vid00000001/09'; clip.onchange();
      await pubRep.busyPromise;
      return calls.filter((c) => c.method === 'PUT').map((c) => c.body.accounts[0].lines);
    })()""", _rep_plan(), {f"PUT /api/repartition/{_REP_DAY}": _rep_plan()})

    assert [x["clip_id"] for x in out[0]] == ["01", "02", "09"]
    assert out[0][2]["slot_at"] == f"{_REP_DAY}T21:00:00+02:00"


@_REP_NODE
def test_recalculate_posts_compute_and_validate_posts_validate_then_toasts_the_count():
    done = _rep_plan(status="validated", created=[{"account": _REP_ACC, "video_id": "v", "clip_id": "01"},
                                                  {"account": _REP_ACC, "video_id": "v", "clip_id": "02"}])
    out = _rep_js("""(async () => {
      __render(); repWire({});
      $$('[data-rep-compute]')[0].onclick(); await pubRep.busyPromise;
      __render(); delete __els['[data-rep-validate]']; repWire({});
      $$('[data-rep-validate]')[0].onclick(); await pubRep.busyPromise;
      return { calls: calls.filter((c) => c.method !== 'GET'), toasts, errors, status: pubRep.data.status };
    })()""", _rep_plan(), {"POST /api/repartition/compute": _rep_plan(),
                           f"POST /api/repartition/{_REP_DAY}/validate": done})

    assert [(c["method"], c["url"], c["body"]) for c in out["calls"]] == [
        ("POST", "/api/repartition/compute", {"day": _REP_DAY}),
        ("POST", f"/api/repartition/{_REP_DAY}/validate", None)]
    assert [t["title"] for t in out["toasts"]] == ["2 publications créées"] and out["errors"] == []
    assert out["status"] == "validated"


@_REP_NODE
def test_a_refused_validation_shows_the_error_and_keeps_the_plan():
    out = _rep_js("""(async () => {
      __render(); repWire({});
      $$('[data-rep-validate]')[0].onclick(); await pubRep.busyPromise;
      return { toasts, errors, status: pubRep.data.status };
    })()""", _rep_plan(), {f"POST /api/repartition/{_REP_DAY}/validate": {"__error": "compte en pause"},
                                         "GET /api/repartition": _rep_plan()})

    assert out["toasts"] == [] and out["errors"][0][1] == "compte en pause" and out["status"] == "proposed"


@_REP_NODE
def test_each_request_shows_a_wait_state_on_the_disabled_buttons():
    out = _rep_js("""(async () => {
      __render(); repWire({});
      hold = true;
      $$('[data-rep-compute]')[0].onclick(); await new Promise((r) => setTimeout(r, 0));
      const during = repHtml();
      release(); await pubRep.busyPromise;
      return { during, after: repHtml() };
    })()""", _rep_plan(), {"POST /api/repartition/compute": _rep_plan()})

    assert re.search(r"data-rep-compute[^>]*disabled[^>]*>[^<]*Calcul en cours", out["during"])
    assert re.search(r"data-rep-validate[^>]*disabled", out["during"])
    assert "Calcul en cours" not in out["after"] and "Recalculer" in out["after"]
    assert not re.search(r"data-rep-compute[^>]*disabled", out["after"])


@_REP_NODE
def test_validate_waits_with_its_own_label_and_respects_the_network_guard():
    out = _rep_js("""(async () => {
      __render(); repWire({});
      hold = true;
      $$('[data-rep-validate]')[0].onclick(); await new Promise((r) => setTimeout(r, 0));
      const during = repHtml();
      release(); await pubRep.busyPromise;
      netOk = false; calls.length = 0;
      $$('[data-rep-validate]')[0].onclick(); await pubRep.busyPromise;
      return { during, blocked: calls.filter((c) => c.method !== 'GET') };
    })()""", _rep_plan(), {f"POST /api/repartition/{_REP_DAY}/validate": _rep_plan(status="proposed")})

    assert re.search(r"data-rep-validate[^>]*disabled[^>]*>[^<]*Validation en cours", out["during"])
    assert out["blocked"] == []


@_REP_NODE
def test_a_repartition_event_reloads_the_plan_and_the_section_sits_above_the_waiting_list():
    out = _rep_js("""(async () => {
      for (const fn of globalThis.__handlers['clipper:event']) fn({ detail: { kind: 'repartition' } });
      await pubRep.loading;
      return calls.filter((c) => c.url.startsWith('/api/repartition')).map((c) => c.url);
    })()""", None, {"GET /api/repartition": _rep_plan()}, setup="pubRep.at = 0;")
    assert out and out[0].startswith("/api/repartition")

    source = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    view = source[source.index("function pubView("):]
    assert view.index("repHtml()") < view.index("pubPostsSection()")


def test_plan_section_computes_no_rule_in_the_page():
    source = (STATIC / "screens" / "publish.js").read_text(encoding="utf-8")
    start = source.index("/* ---------- Plan de demain")
    block = source[start:source.index("/* ---------- fin Plan de demain", start)]

    for rule in ("max_per_source", "posts_per_day", "exploration_per_day", "bonus_points", "max_posts_per_day",
                 "min_gap_minutes", "default_grid", "prime_start", "adjusted +", "score +", "bonus *"):
        assert rule not in block, rule


# --------------------------------------------------------------------------
# TASK-757c7728eb9b : répartition web — vivier par compte (I2), créneau hors jour (M2), séries en
# plusieurs parties (M4), avertissements sans double compte après validation partielle (M3).
# --------------------------------------------------------------------------


def _rep_account_pool(data: dict, account: str) -> set[str]:
    row = next(a for a in data["accounts"] if a["account"] == account)
    return {clip for unit in row["pool"] for clip in unit["clip_ids"]}


def _rep_approved_for(tmp_path, clip_id: str, account: str) -> None:
    _write_publish(tmp_path, "ma_chaine", [_entry(clip_id, "approved", account=account, slot_at=None)])


def test_repartition_pool_is_per_account_and_hides_a_clip_validated_for_another_account(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_approved_for(tmp_path, "01", SPARE)
    _rep_write(tmp_path, day, [(READY, []), (SPARE, [])])

    data = c.get("/api/repartition", params={"day": day}).json()

    assert "01" not in _rep_account_pool(data, READY)
    assert "01" in _rep_account_pool(data, SPARE)
    assert {"02", "03"} <= _rep_account_pool(data, READY)


def test_repartition_put_refuses_a_clip_validated_for_another_account_and_leaves_the_file(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_approved_for(tmp_path, "01", SPARE)
    _rep_write(tmp_path, day, [(READY, []), (SPARE, [])])
    before = _rep_path(tmp_path, day).read_text(encoding="utf-8")

    put = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [_rep_put_line(day, "10:00", "01")]}]})

    assert put.status_code == 422, put.text
    assert SPARE in put.json()["detail"] and "01" in put.json()["detail"]
    assert _rep_path(tmp_path, day).read_text(encoding="utf-8") == before


def test_repartition_pool_hides_the_clips_of_an_excluded_source(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path, repartition={"excluded_sources": ["ma_chaine"]}), _rep_day()
    _rep_write(tmp_path, day, [(READY, [])])

    data = c.get("/api/repartition", params={"day": day}).json()

    assert _rep_account_pool(data, READY) == set()


def test_repartition_pool_hides_the_clips_of_a_video_in_the_processing_queue(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _write_json(tmp_path / "state" / "queue.json", [{"video_id": CLIPS_VIDEO, "action": "run", "status": "waiting"}])
    _rep_write(tmp_path, day, [(READY, [])])

    data = c.get("/api/repartition", params={"day": day}).json()

    assert _rep_account_pool(data, READY) == set()


def test_repartition_put_refuses_a_clip_of_an_excluded_source_and_leaves_the_file(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path, repartition={"excluded_sources": ["ma_chaine"]}), _rep_day()
    _rep_write(tmp_path, day, [(READY, [])])
    before = _rep_path(tmp_path, day).read_text(encoding="utf-8")

    put = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [_rep_put_line(day, "10:00", "01")]}]})

    assert put.status_code == 422, put.text
    assert "excluded_source" in put.json()["detail"]
    assert _rep_path(tmp_path, day).read_text(encoding="utf-8") == before


def test_repartition_put_refuses_a_clip_of_a_video_in_the_processing_queue_and_leaves_the_file(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _write_json(tmp_path / "state" / "queue.json", [{"video_id": CLIPS_VIDEO, "action": "run", "status": "waiting"}])
    _rep_write(tmp_path, day, [(READY, [])])
    before = _rep_path(tmp_path, day).read_text(encoding="utf-8")

    put = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [_rep_put_line(day, "10:00", "01")]}]})

    assert put.status_code == 422, put.text
    assert "in_processing_queue" in put.json()["detail"]
    assert _rep_path(tmp_path, day).read_text(encoding="utf-8") == before


def test_repartition_put_refuses_a_slot_on_another_day_and_leaves_the_file(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    other = (datetime.fromisoformat(day) + _td(days=3)).date().isoformat()
    _rep_write(tmp_path, day, [(READY, [])])
    before = _rep_path(tmp_path, day).read_text(encoding="utf-8")

    put = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [_rep_put_line(other, "10:00", "01")]}]})

    assert put.status_code == 422, put.text
    assert day in put.json()["detail"]
    assert _rep_path(tmp_path, day).read_text(encoding="utf-8") == before


def test_repartition_put_judges_the_slot_day_in_paris_time(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [])])
    # 22:30 UTC la veille = 00:30 Paris le jour du plan (été) : accepté ; 23:30 le jour du plan en Paris = 21:30 UTC : accepté aussi
    late = datetime.fromisoformat(f"{day}T23:30:00").replace(tzinfo=_REP_PARIS).isoformat()
    ok = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [
        {"slot_at": late, "video_id": CLIPS_VIDEO, "clip_id": "01"}]}]})
    assert ok.status_code == 200, ok.text


def _rep_multi_part(tmp_path) -> None:
    for clip_id, part in (("07", 1), ("08", 2)):
        _write_clip(tmp_path, CLIPS_VIDEO, _clip_sidecar(clip_id, part=part, parts_total=2, series_id="s1"))


def test_repartition_pool_never_offers_the_parts_of_a_multi_part_series(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_multi_part(tmp_path)
    _rep_write(tmp_path, day, [(READY, []), (SPARE, [])])

    data = c.get("/api/repartition", params={"day": day}).json()

    for account in (READY, SPARE):
        pool = _rep_account_pool(data, account)
        assert not ({"07", "08"} & pool) and "01" in pool


def test_repartition_put_refuses_a_part_of_a_multi_part_series(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_multi_part(tmp_path)
    _rep_write(tmp_path, day, [(READY, [])])
    before = _rep_path(tmp_path, day).read_text(encoding="utf-8")

    put = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": READY, "lines": [_rep_put_line(day, "10:00", "07")]}]})

    assert put.status_code == 422, put.text
    assert "multi_part_series" in put.json()["detail"]
    assert _rep_path(tmp_path, day).read_text(encoding="utf-8") == before


def test_repartition_warnings_do_not_count_twice_the_lines_of_an_account_already_created(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_write(tmp_path, day, [(READY, [_rep_line(day, "10:00", "01"), _rep_line(day, "13:00", "02")]),
                               (SPARE, [_rep_line(day, "11:00", "03")])])
    _rep_pause(tmp_path, SPARE)
    assert c.post(f"/api/repartition/{day}/validate").status_code == 409
    accounts_path = tmp_path / "state" / "accounts.json"
    data = json.loads(accounts_path.read_text(encoding="utf-8"))
    for row in data["accounts"]:
        row["paused_at"] = None
    accounts_path.write_text(json.dumps(data), encoding="utf-8")
    assert [x["account"] for x in _rep_read(tmp_path, day)["created"]] == [READY, READY]

    put = c.put(f"/api/repartition/{day}", json={"accounts": [{"account": SPARE, "lines": [_rep_put_line(day, "11:00", "03")]}]})

    assert put.status_code == 200, put.text
    ready_lines = next(a for a in put.json()["accounts"] if a["account"] == READY)["lines"]
    assert [line["warning"] for line in ready_lines] == [None, None]


@_REP_NODE
def test_plan_section_offers_the_pool_of_each_account_and_bounds_the_time_to_the_plan_day():
    other = {"account": "acc_b", "label": "Compte B", "slots": [], "notes": [], "lines": [_rep_pline("11:00", "03")],
             "pool": [{**_REP_UNIT, "clip_id": "42", "clip_ids": ["42"], "screen_title": "Pool de B"}]}
    plan = _rep_plan()
    plan["accounts"].append(other)
    html = _rep_js("repHtml()", plan)

    first, second = html.split("Compte B")
    assert "Pool neuf" in first and "Pool de B" not in first      # chaque compte propose son vivier
    assert "Pool de B" in second and "Pool neuf" not in second
    assert f'min="{_REP_DAY}T00:00"' in html and f'max="{_REP_DAY}T23:59"' in html


# --------------------------------------------------------------------------
# TASK-14699025fdd6 (audit 10/10, publication-I1) : une programmation « à vérifier » (post peut-être déjà
# programmé sur TikTok) est signalée à l'écran : ni glissable sur le calendrier, ni « Repasser en attente »
# --------------------------------------------------------------------------


def test_get_publish_exposes_to_verify_on_a_failed_entry(tmp_path, isolated_cwd):
    _publish_setup(tmp_path, [
        _entry("01", "failed", slot_at=PUB_THU, error="programmation à vérifier : aucun post retrouvé", to_verify=True),
        _entry("02", "failed", slot_at=PUB_MON, error="captcha détecté"),
        _entry("03", "scheduled", slot_at=PUB_NEXT_MON),
    ])

    data = _get_publish(tmp_path).json()

    done = {c["clip_id"]: c for c in data["done"]}
    assert done["01"]["to_verify"] is True
    assert done["02"]["to_verify"] is False
    assert all(c["to_verify"] is False for c in data["unscheduled"] + [s["clip"] for s in data["slots"] if s["clip"]]
               if c["clip_id"] != "01")


_PUB_JS_STUBS = """
const pubUi = { data: null }; const pubPosts = { data: null };
const pubChip = (c) => `<span class="chip">${esc(c.publish_status)}</span>`;
const pubSlotLabel = (iso) => String(iso);
const pubAccountField = (c) => "";
const pubServiceName = (c) => "TikTok";
"""


def _pub_js(expr):
    return _run_js([("screens/publish.js", ["pubAccountLabel", "pubKey", "pubTitle", "pubPost", "pubDetailHtml"])],
                   expr, preamble=_PUB_JS_STUBS)


@_NODE_SHEET
def test_publish_screen_makes_a_to_verify_post_not_draggable_and_without_the_unschedule_button():
    out = _pub_js("""(() => {
      const base = { video_id: 'v1', clip_id: '01', publish_status: 'failed', tiktok_status: 'failed', account: 'ab12cd',
        slot_at: '2026-10-08T12:00:00+02:00', publish_error: 'programmation à vérifier', description: '', hashtags: [],
        video_url: '/media/clip/v1/01', thumbnail_url: '/media/clip/v1/01/thumb', screen_title: 'Titre 01' };
      const plain = Object.assign({}, base, { to_verify: false, publish_error: 'captcha détecté' });
      const verify = Object.assign({}, base, { to_verify: true });
      return { plainPost: pubPost(plain), verifyPost: pubPost(verify),
               plainDetail: pubDetailHtml(plain), verifyDetail: pubDetailHtml(verify) };
    })()""")
    assert 'draggable="true"' in out["plainPost"]
    assert 'draggable="false"' in out["verifyPost"]
    assert "data-unschedule" in out["plainDetail"] and "data-retry" in out["plainDetail"]
    assert "data-unschedule" not in out["verifyDetail"]
    assert "data-retry" in out["verifyDetail"]                 # « Réessayer » reste la seule sortie
    assert "TikTok Studio" in out["verifyDetail"]              # l'aide dit de contrôler TikTok Studio d'abord
    assert "repasse le clip en attente" not in out["verifyDetail"]


# --- TASK-abc31eb1723e (audit 10/10, lot C) : la validation rejoue les exclusions

def _rep_exclude_after_compute(tmp_path, day: str):
    """Plan calculé sans exclusion, puis ``ma_chaine`` ajoutée à ``excluded_sources`` : le client voit la nouvelle liste."""
    _rep_two_accounts(tmp_path, day)
    return _rep_client(tmp_path, repartition={"excluded_sources": ["ma_chaine"]})


def test_repartition_get_shows_a_refusal_on_a_line_whose_source_was_excluded_after_the_compute(tmp_path, isolated_cwd):
    day = _rep_day()
    c = _rep_exclude_after_compute(tmp_path, day)

    data = c.get("/api/repartition", params={"day": day}).json()

    refusals = [line["refusal"] for account in data["accounts"] for line in account["lines"]]
    assert len(refusals) == 4 and all(r and "excluded_source" in r for r in refusals), refusals


def test_repartition_validate_refuses_a_line_whose_source_was_excluded_after_the_compute(tmp_path, isolated_cwd):
    day = _rep_day()
    c = _rep_exclude_after_compute(tmp_path, day)

    resp = c.post(f"/api/repartition/{day}/validate")

    assert resp.status_code == 409, resp.text
    assert "excluded_source" in resp.json()["detail"] and resp.json()["created"] == []
    assert _rep_scheduled(tmp_path) == []
    plan = _rep_read(tmp_path, day)
    assert plan["status"] == "proposed" and plan["created"] == [] and "excluded_source" in plan["last_error"]


def test_repartition_validate_refuses_a_line_of_a_video_put_back_in_the_queue(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)
    _write_json(tmp_path / "state" / "queue.json", [{"video_id": CLIPS_VIDEO, "action": "run", "status": "waiting"}])

    resp = c.post(f"/api/repartition/{day}/validate")

    assert resp.status_code == 409 and "in_processing_queue" in resp.json()["detail"], resp.text
    assert _rep_scheduled(tmp_path) == []


def test_repartition_get_does_not_refuse_the_lines_already_created_by_a_partial_validation(tmp_path, isolated_cwd):
    c, day = _rep_client(tmp_path), _rep_day()
    _rep_two_accounts(tmp_path, day)
    _rep_pause(tmp_path, SPARE)
    assert c.post(f"/api/repartition/{day}/validate").status_code == 409  # READY créé, SPARE refusé

    data = c.get("/api/repartition", params={"day": day}).json()

    ready, spare = data["accounts"]
    assert [(line["created"], line["refusal"]) for line in ready["lines"]] == [(True, None), (True, None)]
    assert [line["created"] for line in spare["lines"]] == [False, False]


# --------------------------------------------------------------------------
# Audit 10/10 lot D : garde locale de l'API (web-I1, web-I2, web-M1, web-M2, web-M3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.test"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "null"},
])
def test_lot_d_state_changing_request_from_foreign_origin_is_refused(tmp_path, isolated_cwd, headers):
    """web-I1 : un POST « simple » d'une page tierce ne passe pas (403), meme sans corps."""
    resp = client(tmp_path).post(f"/api/clips/{VIDEO_ID}/01/reject", headers=headers)
    assert resp.status_code == 403
    assert "origine" in resp.json()["detail"].lower()


def test_lot_d_same_origin_and_originless_requests_still_pass(tmp_path, isolated_cwd):
    c = client(tmp_path)
    ok = c.post(f"/api/clips/{VIDEO_ID}/01/reject",
                headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code != 403
    assert c.post(f"/api/clips/{VIDEO_ID}/01/reject").status_code != 403
    # une lecture avec une origine etrangere n'est pas une requete modifiante (pas de CORS : illisible)
    assert c.get("/api/videos", headers={"Origin": "https://evil.test"}).status_code == 200


def _loopback_client(tmp_path) -> TestClient:
    """Navigateur local : adresse cliente de bouclage (la garde Host ne vise que lui, le rebinding DNS)."""
    return TestClient(create_app(config=make_config(tmp_path)), client=("127.0.0.1", 50000))


@pytest.mark.parametrize("host", ["evil.test", "evil.test:8000", "192.168.1.5:8000", "[::2]:8000"])
def test_lot_d_foreign_host_header_is_refused_on_api_and_media(tmp_path, isolated_cwd, host):
    """web-I1 : rebinding DNS, le Host n'est pas celui du PC."""
    c = _loopback_client(tmp_path)
    assert c.get("/api/videos", headers={"Host": host}).status_code == 403
    assert c.get("/media/clip/abcdefghijk/01", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "[::1]"])
def test_lot_d_local_host_headers_pass(tmp_path, isolated_cwd, host):
    """web-M3 : [::1] est local."""
    assert _loopback_client(tmp_path).get("/api/videos", headers={"Host": host}).status_code == 200


def test_lot_d_accounts_guard_accepts_ipv6_loopback_host():
    assert web_app._is_local_host_header("[::1]:8000")
    assert web_app._is_local_host_header("[::1]")
    assert not web_app._is_local_host_header("[::2]:8000")


@pytest.mark.parametrize("path", [
    "/api/videos/..%5Cx",
    "/api/videos/..%5Cx/moments",
    "/api/videos/..%5Cx/clips",
])
def test_lot_d_backslash_video_id_is_a_400(tmp_path, isolated_cwd, path):
    """web-I2 : `%5C` ne sort plus du workspace."""
    resp = client(tmp_path).get(path)
    assert resp.status_code == 400


def test_lot_d_decide_with_backslash_video_id_writes_nothing(tmp_path, isolated_cwd):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    resp = client(tmp_path).post("/api/videos/..%5Celsewhere/moments/1/decide", json={"decision": "accepted"})
    assert resp.status_code == 400
    assert not (outside / "review.json").exists()


def test_lot_d_empty_pipeline_json_does_not_break_the_video_list(tmp_path, isolated_cwd, caplog):
    """web-M1 : un fichier d'etat vide est journalise et ecarte, les autres videos restent listees."""
    _write_state(tmp_path, VIDEO_ID)
    broken = tmp_path / "workspace" / "zzzzzzzzzzz"
    broken.mkdir(parents=True)
    (broken / "pipeline.json").write_text("", encoding="utf-8")
    c = client(tmp_path)
    with caplog.at_level(logging.WARNING):
        resp = c.get("/api/videos")
    assert resp.status_code == 200
    assert [v["video_id"] for v in resp.json()] == [VIDEO_ID]
    assert "zzzzzzzzzzz" in caplog.text
    assert c.get("/api/measures").status_code == 200


def test_lot_d_stats_bounds_are_paris_days():
    """web-M2 : « 2026-10-10 » couvre le 10 octobre heure de Paris, pas UTC."""
    paris = ZoneInfo("Europe/Paris")
    lower = web_app._stats_bound("since", "2026-10-10", end_of_day=False)
    upper = web_app._stats_bound("until", "2026-10-10", end_of_day=True)
    call = datetime.fromisoformat("2026-10-10T00:30:00+02:00")
    assert lower <= call <= upper
    assert lower.utcoffset() == paris.utcoffset(datetime(2026, 10, 10))
    late = datetime.fromisoformat("2026-10-10T23:30:00+02:00")
    assert late <= upper
    assert not (datetime.fromisoformat("2026-10-11T00:30:00+02:00") <= upper)
    # horodatage ISO sans fuseau : Paris aussi
    assert web_app._stats_bound("since", "2026-10-10T08:00:00", end_of_day=False).utcoffset() == paris.utcoffset(datetime(2026, 10, 10))


def test_lot_d_llm_cost_by_day_uses_paris_days(tmp_path, isolated_cwd):
    video = tmp_path / "workspace" / VIDEO_ID
    video.mkdir(parents=True)
    (video / "llm_usage.jsonl").write_text(json.dumps(
        {"recorded_at": "2026-10-09T22:30:00+00:00", "usage": "moments", "cost_usd": 0.5}) + chr(10), encoding="utf-8")
    cost = web_app._stats_llm_cost(make_config(tmp_path), None, None)
    assert cost["by_day"] == {"2026-10-10": 0.5}
