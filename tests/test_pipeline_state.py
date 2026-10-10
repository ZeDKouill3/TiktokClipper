"""Regression pour TASK-2562 : ecriture atomique de l'etat robuste face a un
PermissionError transitoire sur os.replace (Windows, fichier lu par un
autre processus au meme instant : CLI de progression, interface web).

Ne depend pas de la plateforme : os.replace est monkeypatche pour lever
PermissionError a volonte, sans avoir a reproduire un vrai verrou de fichier.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from clipper import channel, llm, pipeline
from clipper.config import Config
from clipper.llm.fake import FakeBackend

VIDEO_ID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def _config(tmp_path: Path) -> Config:
    return Config(mode="auto", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output")


def _patch_noop_steps(monkeypatch, *, except_name=None, except_fn=None):
    """Remplace chaque etape de pipeline._Run par un no-op (sauf
    ``except_name``, remplacee par ``except_fn``) : prouve l'orchestration de
    pipeline.py (etat, progression, journal) sans reseau, GPU, ffmpeg ni vrai
    Claude (done_criteria : "avec des etapes simulees"). ``_summary`` est
    egalement neutralisee : aucune etape simulee n'ecrit captions.json."""
    for name in pipeline.STEPS:
        fn = except_fn if name == except_name else (lambda self: None)
        monkeypatch.setattr(pipeline._Run, name, fn)
    monkeypatch.setattr(pipeline, "_summary", lambda run: [])


def _flaky_replace(fail_times: int | None):
    """Remplace os.replace : leve PermissionError `fail_times` fois (ou
    indefiniment si None) avant de retomber sur le vrai replace."""

    real_replace = os.replace
    calls = {"n": 0}

    def fake_replace(src, target):
        calls["n"] += 1
        if fail_times is None or calls["n"] <= fail_times:
            raise PermissionError(13, "Acces refuse (simule)")
        return real_replace(src, target)

    return fake_replace, calls


def test_save_state_retries_on_transient_permission_error(tmp_path, monkeypatch):
    fake_replace, calls = _flaky_replace(fail_times=2)
    monkeypatch.setattr(os, "replace", fake_replace)
    sleeps = []
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: sleeps.append(s))

    config = _config(tmp_path)
    state = pipeline.new_state(VIDEO_ID, URL, "auto")

    path = pipeline.save_state(state, config=config)

    assert calls["n"] == 3
    assert sleeps == [channel._REPLACE_DELAY_S, channel._REPLACE_DELAY_S]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["video_id"] == VIDEO_ID


def test_save_state_raises_after_persistent_permission_error(tmp_path, monkeypatch):
    fake_replace, calls = _flaky_replace(fail_times=None)
    monkeypatch.setattr(os, "replace", fake_replace)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)

    config = _config(tmp_path)
    state = pipeline.new_state(VIDEO_ID, URL, "auto")

    with pytest.raises(PermissionError):
        pipeline.save_state(state, config=config)

    assert calls["n"] == channel._REPLACE_ATTEMPTS
    # Pas de perte silencieuse : le fichier final n'existe pas, seul le
    # fichier temporaire (donnee non publiee) est present.
    final = tmp_path / "workspace" / VIDEO_ID / pipeline.STATE_FILE
    assert not final.exists()
    assert not list(final.parent.glob("*.tmp"))


def test_set_aside_review_retries_then_raises_and_keeps_the_file(tmp_path, monkeypatch):
    video_dir = tmp_path / "v"
    video_dir.mkdir()
    (video_dir / pipeline.REVIEW_FILE).write_text("{}", encoding="utf-8")
    fake_replace, calls = _flaky_replace(fail_times=2)
    monkeypatch.setattr(os, "replace", fake_replace)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)

    pipeline._set_aside_review(video_dir)

    assert calls["n"] == 3
    assert not (video_dir / pipeline.REVIEW_FILE).exists()
    assert len(list(video_dir.glob("review.json.*"))) == 1

    (video_dir / pipeline.REVIEW_FILE).write_text("{}", encoding="utf-8")
    fake_replace, calls = _flaky_replace(fail_times=None)
    monkeypatch.setattr(os, "replace", fake_replace)
    with pytest.raises(PermissionError):
        pipeline._set_aside_review(video_dir)
    assert (video_dir / pipeline.REVIEW_FILE).exists()  # jamais perdu en silence


# --------------------------------------------------------------------------
# TASK-d2348b14bd09 : channel, enqueued_at, progress intra-etape, journal
# events.jsonl, relance ciblee (force_steps), preset --config en surcouche.
# --------------------------------------------------------------------------


def test_new_state_has_channel_enqueued_at_and_null_progress_per_step():
    from datetime import datetime

    state = pipeline.new_state(VIDEO_ID, URL, "auto")
    assert state["channel"] is None
    datetime.fromisoformat(state["enqueued_at"])  # ISO valide, ne leve pas
    for name in pipeline.STEPS:
        assert state["steps"][name]["progress"] is None

    named = pipeline.new_state(VIDEO_ID, URL, "auto", channel="ma_chaine")
    assert named["channel"] == "ma_chaine"


def test_run_and_render_accept_a_channel_kwarg(tmp_path, monkeypatch):
    _patch_noop_steps(monkeypatch)
    config = _config(tmp_path)

    with llm.use_backend(FakeBackend([])):
        state = pipeline.run(URL, config=config, channel="ma_chaine")
    assert state["channel"] == "ma_chaine"
    assert pipeline.load_state(VIDEO_ID, config=config)["channel"] == "ma_chaine"

    with llm.use_backend(FakeBackend([])):
        again = pipeline.render(VIDEO_ID, config=config, channel="autre_chaine")
    assert again["channel"] == "autre_chaine"

    with llm.use_backend(FakeBackend([])):
        unchanged = pipeline.render(VIDEO_ID, config=config)
    assert unchanged["channel"] == "autre_chaine"  # pas de channel= : inchange


def test_progress_callback_updates_pipeline_json_throttled_to_the_last_value(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock["t"])
    config = _config(tmp_path)
    seen_on_disk = []

    def fake_transcribe(self):
        cb = self.opts("transcribe")["progress"]
        cb(0.1, 50.0, "debut")
        seen_on_disk.append(pipeline.load_state(VIDEO_ID, config=config)["steps"]["transcribe"]["progress"])
        clock["t"] += 0.5  # < 2s depuis la 1re ecriture : pas de nouvelle ecriture disque
        cb(0.4, 30.0, "proche")
        seen_on_disk.append(pipeline.load_state(VIDEO_ID, config=config)["steps"]["transcribe"]["progress"])
        clock["t"] += 2.5  # >= 2s depuis la 1re ecriture : nouvelle ecriture
        cb(0.8, 5.0, "presque fini")
        seen_on_disk.append(pipeline.load_state(VIDEO_ID, config=config)["steps"]["transcribe"]["progress"])

    _patch_noop_steps(monkeypatch, except_name="transcribe", except_fn=fake_transcribe)

    with llm.use_backend(FakeBackend([])):
        state = pipeline.run(URL, config=config)

    assert state["status"] == "done", state
    assert state["steps"]["transcribe"]["progress"] is None  # remis a null a la fin de l'etape
    assert seen_on_disk[0] == {"fraction": 0.1, "eta_s": 50.0, "message": "debut"}
    assert seen_on_disk[1] == seen_on_disk[0]  # disque pas reecrit : encore l'ancienne valeur
    assert seen_on_disk[2] == {"fraction": 0.8, "eta_s": 5.0, "message": "presque fini"}


def test_progress_is_reset_to_null_when_the_step_fails(tmp_path, monkeypatch):
    config = _config(tmp_path)

    def failing_render(self):
        cb = self.opts("render")["progress"]
        cb(0.5, 10.0, "en cours")
        raise RuntimeError("echec simule")

    _patch_noop_steps(monkeypatch, except_name="render", except_fn=failing_render)

    with llm.use_backend(FakeBackend([])):
        state = pipeline.run(URL, config=config)

    assert state["status"] == "failed", state
    assert state["steps"]["render"]["status"] == "failed"
    assert state["steps"]["render"]["progress"] is None


def test_events_journal_gets_info_logs_and_state_transitions_never_truncated(tmp_path, monkeypatch):
    config = _config(tmp_path)

    def fake_moments(self):
        logging.getLogger("clipper.moments").info("travail simule des moments")
        logging.getLogger("clipper.moments").debug("jamais journalise : sous INFO")

    _patch_noop_steps(monkeypatch, except_name="moments", except_fn=fake_moments)

    with llm.use_backend(FakeBackend([])):
        state = pipeline.run(URL, config=config)

    assert state["status"] == "done", state
    events_path = tmp_path / "workspace" / VIDEO_ID / pipeline.EVENTS_FILE
    lines = [json.loads(l) for l in events_path.read_text(encoding="utf-8").splitlines()]
    assert lines, "aucun evenement journalise"
    for entry in lines:
        assert set(entry) == {"at", "level", "step", "message"}
        assert entry["level"] in ("INFO", "WARNING", "ERROR")
    messages = [e["message"] for e in lines]
    assert any("travail simule des moments" in m for m in messages)
    assert not any("jamais journalise" in m for m in messages)
    moments_events = [e for e in lines if e["step"] == "moments"]
    assert any("etape moments" in e["message"] for e in moments_events)
    assert any("travail simule des moments" in e["message"] for e in moments_events)

    # Jamais tronque : un 2e passage (render) ajoute des lignes, n'efface rien.
    before = len(lines)
    _patch_noop_steps(monkeypatch)
    with llm.use_backend(FakeBackend([])):
        pipeline.render(VIDEO_ID, config=config)
    after = [json.loads(l) for l in events_path.read_text(encoding="utf-8").splitlines()]
    assert len(after) > before
    assert after[:before] == lines


def test_force_steps_resets_that_step_and_following_to_pending_keeps_earlier_done(tmp_path):
    config = _config(tmp_path)
    state = pipeline.new_state(VIDEO_ID, URL, "auto")
    for name in pipeline.STEPS:
        state["steps"][name]["status"] = "done"

    run = pipeline._start(state, config, False, None, force_steps=["reframe"])

    idx = pipeline.STEPS.index("reframe")
    for name in pipeline.STEPS[:idx]:
        assert state["steps"][name]["status"] == "done", name
        assert not run._forced(name), name
    for name in pipeline.STEPS[idx:]:
        assert state["steps"][name]["status"] == "pending", name
        assert run._forced(name), name


def test_force_steps_with_an_unknown_step_name_is_a_clear_error(tmp_path):
    config = _config(tmp_path)
    state = pipeline.new_state(VIDEO_ID, URL, "auto")
    with pytest.raises(pipeline.PipelineError, match="inconnue_etape"):
        pipeline._start(state, config, False, None, force_steps=["inconnue_etape"])


# --------------------------------------------------------------------------
# TASK-ded3 : process_queue relance avec le preset de la chaine (SPEC-fc0c §1.3)
# --------------------------------------------------------------------------


def _queued_state(config: Config, channel: str | None) -> None:
    state = pipeline.new_state(VIDEO_ID, URL, "auto", channel=channel)
    state.update(status="queued", reason="quota", retry_at="2000-01-01T00:00:00+00:00")
    pipeline.save_state(state, config=config)


def _record_start(monkeypatch):
    seen = []

    def fake_start(state, config, force, step_options, **kw):
        seen.append(config)
        return object()

    monkeypatch.setattr(pipeline, "_start", fake_start)
    monkeypatch.setattr(pipeline, "_advance", lambda run, *, through_review: {"video_id": VIDEO_ID})
    return seen


def test_process_queue_resumes_with_the_channel_preset_mode(tmp_path, monkeypatch):
    from clipper import channel as channel_mod

    config = _config(tmp_path)  # mode global : auto
    _queued_state(config, "ma_chaine")
    preset_config = Config(mode="auto", workspace_dir=config.workspace_dir, output_dir=config.output_dir)
    monkeypatch.setattr(
        channel_mod, "load_channel",
        lambda name, **kw: (preset_config, {"mode": "review"}) if name == "ma_chaine" else pytest.fail(name),
    )
    seen = _record_start(monkeypatch)

    pipeline.process_queue(config=config)

    assert [c.mode for c in seen] == ["review"]  # le mode de la chaine, pas le global


def test_process_queue_without_channel_keeps_the_global_config(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _queued_state(config, None)
    seen = _record_start(monkeypatch)

    pipeline.process_queue(config=config)

    assert seen == [config]


def test_process_queue_reads_the_presets_dir_of_the_config_not_the_cwd(tmp_path, monkeypatch):
    """coeur-M3 (audit 10/10) : ``_channel_config`` lisait ``presets/`` et ``config.toml`` du cwd, jamais
    ``[watch] presets_dir``/``base_config`` : une vidéo en file d'une chaîne restait en attente pour toujours
    (« chaîne inconnue ») dès que le dossier de styles était configuré ailleurs."""
    presets = tmp_path / "styles"
    presets.mkdir()
    (presets / "ma_chaine.toml").write_text('[channel]\ndisplay_name = "Ma chaine"\nmode = "review"\n', encoding="utf-8")
    base = tmp_path / "config.toml"
    base.write_text('mode = "auto"\n', encoding="utf-8")
    config = Config(mode="auto", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                    _sections={"watch": {"presets_dir": str(presets), "base_config": str(base)}})
    _queued_state(config, "ma_chaine")
    seen = _record_start(monkeypatch)
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # aucun presets/ ni config.toml ici (worker lancé depuis Documents\\Clipper)

    pipeline.process_queue(config=config)

    assert [c.mode for c in seen] == ["review"]
    assert pipeline.load_state(VIDEO_ID, config=config)["reason"] == "quota"  # jamais « chaîne inconnue »


def test_a_manual_run_resets_attempts_and_a_resume_keeps_them(tmp_path, monkeypatch):
    """``run``/``render`` avec ``manual=False`` (option ``--resume`` de la CLI, reprise automatique lancée par le
    worker) gardent ``attempts`` : un échec transitoire de plus compte pour ``max_attempts``."""
    config = _config(tmp_path)

    def transient(self):
        raise ConnectionError("réseau")

    _patch_noop_steps(monkeypatch, except_name="download", except_fn=transient)
    for manual, expected in ((False, 3), (True, 1)):
        state = pipeline.new_state(VIDEO_ID, URL, "auto")
        state.update(status="queued", attempts=2, retry_at="2000-01-01T00:00:00+00:00")
        pipeline.save_state(state, config=config)

        out = pipeline.run(URL, config=config, manual=manual)

        assert (out["status"], out["attempts"]) == ("queued", expected), manual

    state = pipeline.new_state(VIDEO_ID, URL, "auto")
    state.update(status="queued", attempts=2, retry_at="2000-01-01T00:00:00+00:00")
    pipeline.save_state(state, config=config)
    assert pipeline.render(VIDEO_ID, config=config, manual=False)["attempts"] == 3


def test_process_queue_vanished_channel_logs_and_keeps_the_video_waiting(tmp_path, monkeypatch, caplog):
    config = _config(tmp_path)
    _queued_state(config, "disparue")
    seen = _record_start(monkeypatch)
    monkeypatch.chdir(tmp_path)  # aucun presets/disparue.toml ici

    with caplog.at_level(logging.ERROR, logger="clipper"):
        out = pipeline.process_queue(config=config)

    assert out == [] and seen == []  # jamais de repli sur la config globale
    assert "disparue" in caplog.text
    state = pipeline.load_state(VIDEO_ID, config=config)
    assert state["status"] == "queued"
    assert "disparue" in state["reason"]


def test_set_aside_review_is_stable_with_a_concurrent_reader_30_times(tmp_path):
    """Un lecteur qui rouvre review.json en boucle (antivirus, API web) ne fait
    ni echouer ni sauter la mise de cote : 30 tours, jamais de review.json
    perime a la fin."""
    import threading

    video_dir = tmp_path / "v"
    video_dir.mkdir()
    review = video_dir / pipeline.REVIEW_FILE
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                review.read_bytes()
            except OSError:
                pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for _ in range(30):
            review.write_text("{}", encoding="utf-8")
            pipeline._set_aside_review(video_dir)
            assert not review.exists()
            for aside in video_dir.glob("review.json.*"):
                aside.unlink()
    finally:
        stop.set()
        thread.join()
