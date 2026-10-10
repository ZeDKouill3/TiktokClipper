"""worker.py : file state/queue.json, processus enfant par video (TASK-bbe4).

Le vrai pipeline n'est jamais lance : ``tick()`` recoit un spawner injecte
(faux processus, ou un vrai script python trivial pour les tests de
terminaison / orphelin) au lieu de subprocess.Popen. Aucun reseau.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from clipper import channel as channel_mod
from clipper import pipeline, worker
from clipper.config import Config

VIDEO_A = "AAAAAAAAAAA"
VIDEO_B = "BBBBBBBBBBB"
URL_A = f"https://youtu.be/{VIDEO_A}"
URL_B = f"https://youtu.be/{VIDEO_B}"


@pytest.fixture(autouse=True)
def _cwd_hors_depot(tmp_path, monkeypatch):
    # TASK-0f21 : filet pour les tests qui construisent leur Config à la main (sans _config) : les défauts
    # d'état sont relatifs au cwd, donc chaque test démarre dans un dossier vide et ne touche jamais le state/ du dépôt.
    monkeypatch.chdir(tmp_path)


def _config(tmp_path, **worker_overrides) -> Config:
    # Tous les dossiers d'état sous tmp_path (TASK-0f21) : les défauts sont relatifs au cwd, donc un tick
    # lirait et écrirait le state/ du dépôt quand la suite tourne depuis lui.
    state = tmp_path / "state"
    return Config(
        mode="auto",
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "output",
        _sections={
            "worker": {"queue_path": str(state / "queue.json"), **worker_overrides},
            "learning": {"state_dir": str(state / "learning")},
            "tiktok": {"stats_dir": str(state / "stats" / "tiktok"), "events_path": str(state / "tiktok" / "events.json")},
            "veille": {"state_dir": str(state / "veille")},
            "watch": {"state_dir": str(state / "watch")},
            "publish": {"state_dir": str(state / "publish")},
            "outcomes": {"journal_path": str(state / "outcomes.jsonl")},
            "jury_calibration": {"weights_path": str(state / "jury_weights.json")},
            "accounts": {"state_file": str(state / "accounts.json")},
            "feedback": {"journal_path": str(state / "feedback.jsonl")},
            "repartition": {"state_dir": str(state / "repartition")},
        },
    )


def _queue(config: Config) -> list[dict]:
    path = worker._queue_path(config)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _write_queue(config: Config, entries: list[dict]) -> None:
    path = worker._queue_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


class FakeProcess:
    """Substitut de subprocess.Popen injecte comme spawner : pid fixe,
    poll() controle par le test, terminate()/kill() comptes."""

    def __init__(self, pid: int = 4242, alive_after_terminate: bool = False):
        self.pid = pid
        self._returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._alive_after_terminate = alive_after_terminate

    def poll(self):
        return self._returncode

    def finish(self, code: int = 0) -> None:
        self._returncode = code

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self._alive_after_terminate:
            self._returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self._returncode = -9


class FakeSpawner:
    def __init__(self, process: FakeProcess | None = None):
        self.calls: list[list[str]] = []
        self.process = process or FakeProcess()

    def __call__(self, cmd: list[str]):
        self.calls.append(cmd)
        return self.process


def _pipeline_state(video_id: str, config: Config, *, status: str = "running") -> dict:
    state = pipeline.new_state(video_id, f"https://youtu.be/{video_id}", config.mode)
    state["status"] = status
    pipeline.save_state(state, config=config)
    return state


# --------------------------------------------------------------------------
# (1) enqueue
# --------------------------------------------------------------------------


def test_enqueue_writes_entry_to_queue_json(tmp_path):
    config = _config(tmp_path)

    entry = worker.enqueue(URL_A, "ma_chaine", "run", ["render"], config=config)

    entries = _queue(config)
    assert entries == [entry]
    assert entry["video_id"] == VIDEO_A
    assert entry["url"] == URL_A
    assert entry["channel"] == "ma_chaine"
    assert entry["action"] == "run"
    assert entry["force_steps"] == ["render"]
    assert entry["status"] == "waiting"
    assert entry["pid"] is None
    assert entry["enqueued_at"]
    assert entry["id"]


def test_enqueue_refuses_duplicate_waiting_same_video_and_action(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)

    with pytest.raises(worker.WorkerError):
        worker.enqueue(URL_A, None, "run", config=config)

    assert len(_queue(config)) == 1


def test_enqueue_allows_same_video_different_action(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)

    worker.enqueue(URL_A, None, "render", config=config)

    assert len(_queue(config)) == 2


def test_enqueue_allows_duplicate_when_existing_entry_is_running(tmp_path):
    config = _config(tmp_path)
    entries = [{
        "id": "x", "video_id": VIDEO_A, "url": URL_A, "channel": None, "action": "run",
        "force_steps": [], "enqueued_at": "2026-01-01T00:00:00+00:00", "status": "running", "pid": 1,
    }]
    _write_queue(config, entries)

    worker.enqueue(URL_A, None, "run", config=config)

    assert len(_queue(config)) == 2


# --------------------------------------------------------------------------
# (1) move_to_front / remove
# --------------------------------------------------------------------------


def _entry(video_id: str, url: str, status: str = "waiting", pid=None) -> dict:
    return {
        "id": video_id, "video_id": video_id, "url": url, "channel": None, "action": "run",
        "force_steps": [], "enqueued_at": "2026-01-01T00:00:00+00:00", "status": status, "pid": pid,
    }


def test_move_to_front_reorders_waiting_without_touching_running(tmp_path):
    config = _config(tmp_path)
    running = _entry(VIDEO_A, URL_A, status="running", pid=99)
    b, c, d = _entry("B", "https://youtu.be/B"), _entry("C", "https://youtu.be/C"), _entry("D", "https://youtu.be/D")
    _write_queue(config, [running, b, c, d])

    worker.move_to_front("D", config=config)

    entries = _queue(config)
    assert entries[0]["video_id"] == VIDEO_A
    assert entries[0]["status"] == "running"
    assert [e["video_id"] for e in entries[1:]] == ["D", "B", "C"]


def test_move_to_front_raises_for_unknown_video(tmp_path):
    config = _config(tmp_path)
    _write_queue(config, [_entry("B", "https://youtu.be/B")])

    with pytest.raises(worker.WorkerError):
        worker.move_to_front("nope", config=config)


def test_remove_removes_waiting_without_touching_running(tmp_path):
    config = _config(tmp_path)
    running = _entry(VIDEO_A, URL_A, status="running", pid=99)
    b = _entry("B", "https://youtu.be/B")
    _write_queue(config, [running, b])

    worker.remove("B", config=config)

    entries = _queue(config)
    assert [e["video_id"] for e in entries] == [VIDEO_A]
    assert entries[0]["status"] == "running"


def test_remove_raises_when_nothing_waiting_matches(tmp_path):
    config = _config(tmp_path)
    _write_queue(config, [_entry(VIDEO_A, URL_A, status="running", pid=99)])

    with pytest.raises(worker.WorkerError):
        worker.remove(VIDEO_A, config=config)


# --------------------------------------------------------------------------
# (2) tick : lance la tete de file via le spawner
# --------------------------------------------------------------------------


def test_tick_launches_head_entry_with_exact_command_line_run_action(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, "ma_chaine", "run", ["parts", "render"], config=config)
    spawner = FakeSpawner()

    w = worker.Worker(config=config, spawner=spawner)
    w.tick()

    assert spawner.calls == [[
        sys.executable, "-m", "clipper",
        "--config", "presets/ma_chaine.toml",
        "run", "--force-step", "parts", "--force-step", "render",
        "--", URL_A,  # « -- » : un identifiant qui commence par « - » reste un positionnel (web-I6)
    ]]


def test_tick_launches_head_entry_with_exact_command_line_render_action(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(VIDEO_A, None, "render", config=config)
    spawner = FakeSpawner()

    w = worker.Worker(config=config, spawner=spawner)
    w.tick()

    assert spawner.calls == [[sys.executable, "-m", "clipper", "render", "--", VIDEO_A]]


def test_tick_records_pid_and_marks_entry_running(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)
    spawner = FakeSpawner(FakeProcess(pid=1234))

    worker.Worker(config=config, spawner=spawner).tick()

    entries = _queue(config)
    assert entries[0]["status"] == "running"
    assert entries[0]["pid"] == 1234


def test_tick_launches_nothing_else_while_child_alive(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)
    worker.enqueue(URL_B, None, "run", config=config)
    spawner = FakeSpawner()

    w = worker.Worker(config=config, spawner=spawner)
    w.tick()
    w.tick()
    w.tick()

    assert len(spawner.calls) == 1
    entries = _queue(config)
    assert [e["status"] for e in entries] == ["running", "waiting"]


def test_tick_removes_entry_and_starts_next_once_child_finishes(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)
    worker.enqueue(URL_B, None, "run", config=config)
    process_a = FakeProcess()
    spawner = FakeSpawner(process_a)

    w = worker.Worker(config=config, spawner=spawner)
    w.tick()
    process_a.finish(0)
    w.tick()

    entries = _queue(config)
    assert [e["video_id"] for e in entries] == [VIDEO_B]
    assert len(spawner.calls) == 2


# --------------------------------------------------------------------------
# TASK-6a1b : --config avant la sous-commande ; un enfant en echec reste visible
# --------------------------------------------------------------------------


def _parse_built(entry: dict):
    from clipper.__main__ import build_parser

    cmd = worker._build_command(entry)
    assert cmd[:3] == [sys.executable, "-m", "clipper"]
    return build_parser().parse_args(cmd[3:])


def _cmd_entry(action: str = "run", channel: str | None = "ma_chaine", force_steps=None) -> dict:
    return {
        "video_id": VIDEO_A, "url": URL_A if action == "run" else VIDEO_A, "channel": channel,
        "action": action, "force_steps": force_steps or [],
    }


def test_build_command_is_accepted_by_the_real_parser_with_channel(tmp_path):
    args = _parse_built(_cmd_entry("run", "ma_chaine"))
    assert (args.config, args.command, args.url) == ("presets/ma_chaine.toml", "run", URL_A)


def test_build_command_is_accepted_by_the_real_parser_without_channel(tmp_path):
    args = _parse_built(_cmd_entry("run", None))
    assert (args.config, args.command, args.url) == (None, "run", URL_A)


def test_build_command_render_with_channel_and_force_step_is_accepted(tmp_path):
    args = _parse_built(_cmd_entry("render", "ma_chaine", ["parts", "render"]))
    assert (args.config, args.command, args.video_id) == ("presets/ma_chaine.toml", "render", VIDEO_A)
    assert args.force_step == ["parts", "render"]


def test_build_command_run_without_channel_with_force_step_is_accepted(tmp_path):
    args = _parse_built(_cmd_entry("run", None, ["parts"]))
    assert (args.config, args.command, args.url, args.force_step) == (None, "run", URL_A, ["parts"])


def test_launch_head_syncs_the_channel_mode_into_the_preset_before_spawning(tmp_path):
    """Important 1 (revue r-transcription) : l'enfant lance par --config lit le mode de TETE du
    preset (clipper.__main__.load_config), jamais [channel].mode directement ; les deux doivent donc
    etre synchronises avant chaque lancement, sinon l'enfant tourne dans le mode global au lieu de
    celui de la chaine (et vice versa)."""
    presets = tmp_path / "presets"
    presets.mkdir()
    base = tmp_path / "config.toml"
    base.write_text('mode = "review"\n', encoding="utf-8")
    preset_path = presets / "ma_chaine.toml"
    preset_path.write_text('[channel]\nmode = "auto"\n', encoding="utf-8")
    config = Config(
        mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={
            "worker": {"queue_path": str(tmp_path / "state" / "queue.json")},
            "watch": {"presets_dir": str(presets), "base_config": str(base)},
        },
    )
    worker.enqueue(URL_A, "ma_chaine", "run", config=config)

    worker.Worker(config=config, spawner=FakeSpawner()).tick()

    from clipper.config import load_config

    assert load_config(preset_path, base=base).mode == "auto"


def test_launch_head_removes_a_stale_preset_mode_override_once_the_channel_inherits_the_global_default(tmp_path):
    """Sens inverse : [channel].mode redevient vide (herite du mode global) alors que le preset garde
    encore une tete 'mode' laissee par une synchronisation precedente (chaine alors en mode explicite) :
    cette tete doit disparaitre, sinon la chaine reste figee sur l'ancien mode au lieu de suivre le mode
    global courant."""
    presets = tmp_path / "presets"
    presets.mkdir()
    base = tmp_path / "config.toml"
    base.write_text('mode = "review"\n', encoding="utf-8")
    preset_path = presets / "ma_chaine.toml"
    preset_path.write_text('mode = "auto"\n\n[channel]\ntimezone = "UTC"\n', encoding="utf-8")
    config = Config(
        mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={
            "worker": {"queue_path": str(tmp_path / "state" / "queue.json")},
            "watch": {"presets_dir": str(presets), "base_config": str(base)},
        },
    )
    worker.enqueue(URL_A, "ma_chaine", "run", config=config)

    worker.Worker(config=config, spawner=FakeSpawner()).tick()

    from clipper.config import load_config

    assert load_config(preset_path, base=base).mode == "review"


class _LoggingSpawner(FakeSpawner):
    """Simule un enfant qui ecrit sa sortie d'erreur dans le journal du worker."""

    def __init__(self, config: Config, output: str):
        super().__init__()
        self._config, self._output = config, output

    def __call__(self, cmd):
        log_path = worker.log_path(VIDEO_A, self._config)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(self._output, encoding="utf-8")
        return super().__call__(cmd)


def _fail_child(config: Config, output: str, code: int = 2, *, queue_second: bool = False):
    worker.enqueue(URL_A, "ma_chaine", "run", config=config)
    if queue_second:
        worker.enqueue(URL_B, None, "run", config=config)
    spawner = _LoggingSpawner(config, output)
    w = worker.Worker(config=config, spawner=spawner)
    w.tick()
    spawner.process.finish(code)
    w.tick()
    return w, spawner


def test_failed_child_without_pipeline_state_creates_a_failed_state_with_code_and_stderr_tail(tmp_path):
    config = _config(tmp_path)
    output = "ligne 1\n" + "clipper: error: unrecognized arguments: --config presets/ma_chaine.toml\n"

    _fail_child(config, output, code=2)

    state = pipeline.load_state(VIDEO_A, config=config)
    assert state["status"] == "failed"
    assert "2" in state["reason"]
    assert "unrecognized arguments: --config presets/ma_chaine.toml" in state["reason"]
    assert state["source_url"] == URL_A
    assert state["channel"] == "ma_chaine"
    assert _queue(config) == []


def test_failed_child_reason_keeps_only_the_end_of_a_long_output(tmp_path):
    config = _config(tmp_path)
    output = "".join(f"ligne {i}\n" for i in range(500))

    _fail_child(config, output, code=1)

    reason = pipeline.load_state(VIDEO_A, config=config)["reason"]
    assert "ligne 499" in reason
    assert "ligne 0\n" not in reason
    assert len(reason) < 4000


def test_failed_child_output_is_kept_in_the_log_file(tmp_path):
    config = _config(tmp_path)

    _fail_child(config, "boum\n", code=3)

    log_file = worker.log_path(VIDEO_A, config)
    assert log_file == config.workspace_dir / VIDEO_A / "worker.log"
    assert log_file.read_text(encoding="utf-8") == "boum\n"
    assert str(log_file) in pipeline.load_state(VIDEO_A, config=config)["reason"]


def test_failed_child_overrides_a_stale_state_left_running(tmp_path):
    config = _config(tmp_path)
    _pipeline_state(VIDEO_A, config, status="running")

    _fail_child(config, "kaboom\n", code=1)

    state = pipeline.load_state(VIDEO_A, config=config)
    assert state["status"] == "failed"
    assert "kaboom" in state["reason"]


def test_failed_child_clears_dismissed_at_so_the_failure_shows(tmp_path):
    config = _config(tmp_path)
    state = pipeline.new_state(VIDEO_A, URL_A, "auto")
    state["dismissed_at"] = "2026-01-01T00:00:00+00:00"
    pipeline.save_state(state, config=config)

    _fail_child(config, "oups\n", code=1)

    assert "dismissed_at" not in pipeline.load_state(VIDEO_A, config=config)


def test_failed_child_keeps_the_reason_the_pipeline_wrote_itself(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)
    spawner = FakeSpawner()
    w = worker.Worker(config=config, spawner=spawner)
    w.tick()
    state = pipeline.new_state(VIDEO_A, URL_A, "auto")
    state.update(status="failed", reason="download : video privee")
    pipeline.save_state(state, config=config)  # l'enfant a ecrit son propre echec
    spawner.process.finish(1)
    w.tick()

    assert pipeline.load_state(VIDEO_A, config=config)["reason"] == "download : video privee"
    assert _queue(config) == []


def test_failed_child_does_not_block_the_next_cmd_entry(tmp_path):
    config = _config(tmp_path)

    _, spawner = _fail_child(config, "x\n", code=1, queue_second=True)

    assert len(spawner.calls) == 2
    assert [e["video_id"] for e in _queue(config)] == [VIDEO_B]


def test_child_exiting_zero_writes_no_failure(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)
    spawner = FakeSpawner()
    w = worker.Worker(config=config, spawner=spawner)
    w.tick()
    spawner.process.finish(0)
    w.tick()

    assert _queue(config) == []
    assert not (config.workspace_dir / VIDEO_A / "pipeline.json").exists()


def test_default_spawner_writes_child_output_to_the_log_file(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)
    w = worker.Worker(config=config)  # vrai subprocess.Popen
    w._spawn = lambda entry, cmd: w._popen_logged(entry, [sys.executable, "-c", "import sys; sys.stderr.write('fin triste'); sys.exit(7)"])
    w.tick()
    w._process.wait(timeout=30)
    w.tick()

    state = pipeline.load_state(VIDEO_A, config=config)
    assert state["status"] == "failed"
    assert "7" in state["reason"] and "fin triste" in state["reason"]
    assert "fin triste" in worker.log_path(VIDEO_A, config).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# (5) tick : reprend la file du pipeline (retry_at passe)
# --------------------------------------------------------------------------


def test_tick_calls_pipeline_process_queue_when_nothing_to_launch(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = []
    monkeypatch.setattr(pipeline, "process_queue", lambda *, config: calls.append(config))

    worker.Worker(config=config, spawner=FakeSpawner()).tick()

    assert calls == [config]


# --------------------------------------------------------------------------
# (3) cancel
# --------------------------------------------------------------------------


def test_cancel_kills_after_grace_if_still_alive(tmp_path, monkeypatch):
    config = _config(tmp_path, cancel_grace_s=0)
    _write_queue(config, [_entry(VIDEO_A, URL_A, status="running", pid=4242)])
    _pipeline_state(VIDEO_A, config, status="running")
    signals = []
    monkeypatch.setattr(worker, "_pid_alive", lambda pid: len(signals) < 2)  # survit a la demande d'arret
    monkeypatch.setattr(worker.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    worker.cancel(VIDEO_A, config=config)

    assert [pid for pid, _ in signals] == [4242, 4242]  # demande d'arret, puis kill apres le delai de grace
    assert pipeline.load_state(VIDEO_A, config=config)["status"] == "failed"


# --------------------------------------------------------------------------
# (4) demarrage : orphelin (pid mort) repasse waiting en tete
# --------------------------------------------------------------------------


def test_worker_startup_recovers_dead_orphan_to_waiting_front(tmp_path):
    import subprocess

    config = _config(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dead_pid = dead.pid

    orphan = _entry(VIDEO_A, URL_A, status="running", pid=dead_pid)
    other = _entry("B", "https://youtu.be/B", status="waiting")
    _write_queue(config, [other, orphan])

    worker.Worker(config=config, spawner=FakeSpawner()).startup()

    entries = _queue(config)
    assert entries[0]["video_id"] == VIDEO_A
    assert entries[0]["status"] == "waiting"
    assert entries[0]["pid"] is None
    assert entries[1]["video_id"] == "B"


# --------------------------------------------------------------------------
# (6) python -m clipper worker / serve
# --------------------------------------------------------------------------


def test_loop_ticks_and_sleeps_at_configured_interval(tmp_path, monkeypatch):
    config = _config(tmp_path, poll_interval_s=5)
    tick_calls = []
    monkeypatch.setattr(worker.Worker, "tick", lambda self: tick_calls.append(1))

    sleep_calls = []

    class _StopLoop(Exception):
        pass

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise _StopLoop

    monkeypatch.setattr(worker.time, "sleep", fake_sleep)

    w = worker.Worker(config=config, spawner=FakeSpawner())
    with pytest.raises(_StopLoop):
        w.loop()

    assert tick_calls == [1]
    assert sleep_calls == [5.0]


def test_main_worker_command_runs_worker_loop(tmp_path, monkeypatch):
    from clipper.__main__ import main

    config = _config(tmp_path)
    monkeypatch.setattr("clipper.__main__.load_config", lambda path="config.toml": config)

    built = {}

    class FakeWorker:
        def __init__(self, *, config):
            built["config"] = config

        def loop(self):
            built["looped"] = True

    monkeypatch.setattr("clipper.worker.Worker", FakeWorker)

    exit_code = main(["worker"])

    assert exit_code == 0
    assert built["config"] is config
    assert built["looped"] is True


def test_main_serve_launches_worker_subprocess_and_stops_it_at_exit(tmp_path, monkeypatch):
    from clipper.__main__ import main

    config = Config(mode="auto", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output")
    monkeypatch.setattr("clipper.__main__.load_config", lambda path="config.toml": config)
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)

    process = FakeProcess()
    spawner = FakeSpawner(process)
    monkeypatch.setattr("clipper.__main__._popen", spawner)

    exit_code = main(["serve"])

    assert exit_code == 0
    assert spawner.calls == [[sys.executable, "-m", "clipper", "worker"]]
    assert process.terminate_calls == 1


# --------------------------------------------------------------------------
# TASK-ded3 : verrou inter-processus sur state/queue.json
# --------------------------------------------------------------------------


def _enqueue_many(config, prefix, n):
    for i in range(n):
        worker.enqueue(f"{prefix}{i:02d}", None, "render", config=config)


def test_enqueue_from_two_processes_loses_no_entry(tmp_path):
    import multiprocessing

    config = _config(tmp_path)
    ctx = multiprocessing.get_context("fork" if sys.platform != "win32" else "spawn")
    procs = [ctx.Process(target=_enqueue_many, args=(config, prefix, 20)) for prefix in ("a", "b")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0

    ids = sorted(e["video_id"] for e in _queue(config))
    assert ids == sorted([f"a{i:02d}" for i in range(20)] + [f"b{i:02d}" for i in range(20)])


def test_queue_write_goes_through_a_temp_file_and_os_replace(tmp_path, monkeypatch):
    config = _config(tmp_path)
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (replaced.append((str(src), str(dst))), real_replace(src, dst))[1])

    worker.enqueue("AAAAAAAAAAA", None, "render", config=config)

    assert len(replaced) == 1
    src, dst = replaced[0]
    assert src != dst and dst == str(worker._queue_path(config))
    assert not os.path.exists(src)


# --------------------------------------------------------------------------
# (5) surveillance des chaines (TASK-7508, SPEC-fc0c §5)
# --------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402


class _WatchLister:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, source_url: str) -> list[dict]:
        self.calls.append(source_url)
        return [{"video_id": VIDEO_A, "url": URL_A, "title": "Direct", "duration_s": 7200,
                 "published_at": "2026-01-01T20:00:00+00:00"}]


def _watch_env(tmp_path, channels: dict[str, tuple[bool, int]]) -> Config:
    """Un preset par chaine ``nom -> (watch, watch_interval_s)``, un config.toml de base."""
    presets = tmp_path / "presets"
    presets.mkdir()
    base = tmp_path / "config.toml"
    base.write_text('mode = "review"\n', encoding="utf-8")
    for name, (on, interval) in channels.items():
        (presets / f"{name}.toml").write_text(
            f'[channel]\nsource_url = "https://example.test/{name}/videos"\nwatch = {str(on).lower()}\n'
            f"watch_interval_s = {interval}\nmode = \"review\"\n", encoding="utf-8")
    return Config(
        mode="review",
        workspace_dir=tmp_path / "workspace",
        output_dir=tmp_path / "output",
        _sections={
            "worker": {"queue_path": str(tmp_path / "state" / "queue.json")},
            "watch": {"state_dir": str(tmp_path / "state" / "watch"),
                      "presets_dir": str(presets), "base_config": str(base)},
        },
    )


def _write_watch_state(tmp_path, name: str, checked_at: datetime | None) -> None:
    path = tmp_path / "state" / "watch" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"checked_at": checked_at.isoformat() if checked_at else None,
                                "seen": [], "pending": [], "last_error": None}), encoding="utf-8")


def test_tick_checks_watched_channel_whose_interval_has_elapsed(tmp_path):
    config = _watch_env(tmp_path, {"ma_chaine": (True, 1800)})
    _write_watch_state(tmp_path, "ma_chaine", datetime.now(timezone.utc) - timedelta(seconds=1801))
    lister = _WatchLister()

    worker.Worker(config=config, spawner=FakeSpawner(), watch_lister=lister).tick()

    assert lister.calls == ["https://example.test/ma_chaine/videos"]
    state = json.loads((tmp_path / "state" / "watch" / "ma_chaine.json").read_text(encoding="utf-8"))
    assert [v["video_id"] for v in state["pending"]] == [VIDEO_A]


def test_tick_checks_a_watched_channel_never_checked(tmp_path):
    config = _watch_env(tmp_path, {"ma_chaine": (True, 1800)})
    lister = _WatchLister()

    worker.Worker(config=config, spawner=FakeSpawner(), watch_lister=lister).tick()

    assert len(lister.calls) == 1


def test_tick_skips_a_channel_checked_within_its_interval(tmp_path):
    config = _watch_env(tmp_path, {"ma_chaine": (True, 1800)})
    _write_watch_state(tmp_path, "ma_chaine", datetime.now(timezone.utc) - timedelta(seconds=600))
    lister = _WatchLister()

    worker.Worker(config=config, spawner=FakeSpawner(), watch_lister=lister).tick()

    assert lister.calls == []


def test_tick_skips_channels_with_watch_false(tmp_path):
    config = _watch_env(tmp_path, {"ma_chaine": (False, 1800)})
    lister = _WatchLister()

    worker.Worker(config=config, spawner=FakeSpawner(), watch_lister=lister).tick()

    assert lister.calls == []


def test_tick_checks_watched_channels_even_while_a_child_runs(tmp_path):
    config = _watch_env(tmp_path, {"ma_chaine": (True, 1800)})
    _write_queue(config, [{"id": "e1", "video_id": VIDEO_B, "url": URL_B, "channel": None, "action": "run",
                           "force_steps": [], "enqueued_at": "2026-01-01T00:00:00+00:00",
                           "status": "waiting", "pid": None}])
    lister = _WatchLister()
    w = worker.Worker(config=config, spawner=FakeSpawner(), watch_lister=lister)
    w.tick()  # lance l'enfant, premier check
    _write_watch_state(tmp_path, "ma_chaine", datetime.now(timezone.utc) - timedelta(seconds=1801))

    w.tick()  # l'enfant vit toujours

    assert len(lister.calls) == 2


def test_tick_logs_a_broken_preset_instead_of_crashing(tmp_path, caplog):
    config = _watch_env(tmp_path, {"ma_chaine": (True, 1800)})
    (tmp_path / "presets" / "cassee.toml").write_text("[channel\n", encoding="utf-8")
    lister = _WatchLister()

    with caplog.at_level("ERROR"):
        worker.Worker(config=config, spawner=FakeSpawner(), watch_lister=lister).tick()

    assert "cassee" in caplog.text


# --------------------------------------------------------------------------
# TASK-a40d : battement du worker (voyant « worker actif / arrêté » du tableau de bord)
# --------------------------------------------------------------------------


def _hb_config(tmp_path, **worker_overrides) -> Config:
    return _config(tmp_path, **worker_overrides)


def test_worker_defaults_declare_the_heartbeat_settings():
    assert worker.CONFIG_DEFAULTS["heartbeat_interval_s"] > 0
    assert worker.heartbeat_path(_config(Path("x"))) == Path("x") / "state" / "worker.json"


def test_tick_writes_a_heartbeat_with_pid_and_timestamp(tmp_path):
    config = _hb_config(tmp_path)

    worker.Worker(config=config, spawner=FakeSpawner(FakeProcess())).tick()

    beat = json.loads((tmp_path / "state" / "worker.json").read_text(encoding="utf-8"))
    assert beat["pid"] == os.getpid()
    assert datetime.fromisoformat(beat["at"]).tzinfo is not None


def test_heartbeat_is_rewritten_only_once_the_interval_has_passed(tmp_path, monkeypatch):
    config = _hb_config(tmp_path, heartbeat_interval_s=5)
    path = tmp_path / "state" / "worker.json"
    clock = {"t": 1000.0}
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock["t"])
    w = worker.Worker(config=config, spawner=FakeSpawner(FakeProcess()))

    w.tick()
    first = path.read_text(encoding="utf-8")
    path.unlink()
    clock["t"] += 2
    w.tick()
    assert not path.exists()
    clock["t"] += 4
    w.tick()
    assert path.exists() and first


def _locked_replace(monkeypatch, failures: int | None):
    """os.replace vers worker.json leve PermissionError ``failures`` fois (None : toujours), comme un lecteur Windows."""
    real = os.replace
    calls = {"n": 0}

    def fake(src, dst):
        if Path(dst).name == "worker.json" and (failures is None or calls["n"] < failures):
            calls["n"] += 1
            raise PermissionError(5, "Accès refusé", str(dst))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", fake)
    monkeypatch.setattr(channel_mod.time, "sleep", lambda s: None)
    return calls


def test_heartbeat_retries_while_a_reader_holds_worker_json(tmp_path, monkeypatch):
    config = _hb_config(tmp_path)
    calls = _locked_replace(monkeypatch, failures=3)

    worker.Worker(config=config, spawner=FakeSpawner(FakeProcess())).tick()

    assert calls["n"] == 3
    assert json.loads((tmp_path / "state" / "worker.json").read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_heartbeat_still_locked_skips_the_beat_without_killing_tick(tmp_path, monkeypatch, caplog):
    config = _hb_config(tmp_path)
    _locked_replace(monkeypatch, failures=None)

    with caplog.at_level(logging.WARNING, logger=worker.log.name):
        worker.Worker(config=config, spawner=FakeSpawner(FakeProcess())).tick()  # ne leve pas

    assert not (tmp_path / "state" / "worker.json").exists()
    assert not list((tmp_path / "state").glob("worker.json.*.tmp"))
    assert any("battement" in r.getMessage() for r in caplog.records)


def test_read_heartbeat_reports_active_stale_and_stopped(tmp_path):
    config = _hb_config(tmp_path, heartbeat_interval_s=5)
    now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

    assert worker.read_heartbeat(config, now=now)["state"] == "stopped"

    path = tmp_path / "state" / "worker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "at": (now - timedelta(seconds=4)).isoformat()}), encoding="utf-8")
    active = worker.read_heartbeat(config, now=now)
    assert active["state"] == "active" and active["pid"] == os.getpid() and active["age_s"] == 4

    path.write_text(json.dumps({"pid": os.getpid(), "at": (now - timedelta(seconds=60)).isoformat()}), encoding="utf-8")
    stale = worker.read_heartbeat(config, now=now)
    assert stale["state"] == "stale" and "périmé" in stale["reason"]
    assert stale["command"] == "python -m clipper worker"


def test_read_heartbeat_refuses_an_unreadable_file_in_french(tmp_path):
    config = _hb_config(tmp_path)
    path = tmp_path / "state" / "worker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{pas du json", encoding="utf-8")

    with pytest.raises(worker.WorkerError, match="battement"):
        worker.read_heartbeat(config)


def test_heartbeat_with_a_dead_pid_is_stopped_even_if_recent(tmp_path, monkeypatch):
    config = _hb_config(tmp_path)
    now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    path = tmp_path / "state" / "worker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 99999, "at": now.isoformat()}), encoding="utf-8")
    monkeypatch.setattr(worker, "_pid_alive", lambda pid: False)

    assert worker.read_heartbeat(config, now=now)["state"] == "stopped"


# --------------------------------------------------------------------------
# TASK-0b78 : le worker publie les entrees dues sur TikTok (SPEC-9225 R3, R4, R6)
# La publication est injectee (fausse) : aucun navigateur, aucun TikTok.
# --------------------------------------------------------------------------

import logging  # noqa: E402

from clipper import accounts as accounts_mod, browser, publish, tiktok  # noqa: E402

ACCOUNT = "ab12cd"
LINK = "https://example.invalid/@ma_chaine/video/7300000000000000001"
# creneaux reguliers d'un compte (SPEC-6076 R2) : tous les jours a 09:00, fuseau UTC
_WEEK = [{"day": d, "time": "09:00"} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")]


_CONNECTED = {"state": "connected", "checked_at": "2026-10-01T10:00:00+00:00", "expires_at": None}


class FakeLogin:
    """Remplace browser.login_state : connexion simulee par compte (defaut : connecte), jamais de cookie lu."""

    def __init__(self, **states):
        self.states, self.calls = states, []

    def __call__(self, account, *, config=None, now=None):
        self.calls.append(account)
        state = self.states.get(account, "connected")
        if isinstance(state, Exception):
            raise state
        return {"state": state, "checked_at": "2026-10-01T10:00:00+00:00", "expires_at": None}


class FakePublisher:
    """Remplace tiktok.publish : enregistre les appels, rend un resultat ou leve ``error``."""

    def __init__(self, error=None, state="published", during=None, scheduled_post_id="7300000000000000002"):
        self.calls, self.error, self.state, self.during = [], error, state, during
        self.scheduled_post_id = scheduled_post_id  # None : TikTok Studio n'a montre aucun post apres la programmation

    def __call__(self, clip, account, *, mode, schedule_at=None, config=None, on_tick=None, **kwargs):
        self.calls.append({"clip": clip, "account": account, "mode": mode, "schedule_at": schedule_at, "on_tick": on_tick,
                           **kwargs})
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        scheduled = mode == "scheduled"
        return {"post_url": None if scheduled else LINK,
                "post_id": self.scheduled_post_id if scheduled else "7300000000000000001",
                "state": "scheduled_on_tiktok" if scheduled else "published",
                "publish_at": (schedule_at if scheduled else datetime.now(timezone.utc)).isoformat(), "note": None}


def _pub_env(tmp_path, monkeypatch, *, tiktok_settings=None, channels=("ma_chaine",), slots=_WEEK):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text('mode = "review"\n', encoding="utf-8")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "accounts.json").write_text(
        json.dumps({"accounts": [
            {"id": ACCOUNT, "label": "A", "ready_to_publish": True, "login": _CONNECTED, "slots": slots, "timezone": "UTC"},
            {"id": "ef34ab", "label": "B", "ready_to_publish": True, "login": _CONNECTED, "slots": slots, "timezone": "UTC"}]}),
        encoding="utf-8")
    presets = tmp_path / "presets"
    presets.mkdir(exist_ok=True)
    for name in channels:  # un style n'a ni compte ni creneaux (SPEC-6076 R2)
        (presets / f"{name}.toml").write_text('[channel]\ntimezone = "UTC"\n', encoding="utf-8")
    return Config(
        mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={
            "worker": {"queue_path": str(tmp_path / "state" / "queue.json")},
            "watch": {"presets_dir": str(presets), "base_config": str(tmp_path / "config.toml")},
            # relevé périodique actif dans ces tests (24 h) : le défaut réel est 0 = coupé (SPEC-47e2 R4)
            "tiktok": {"stats_interval_h": 24, **(tiktok_settings or {})},
        })


def _seed(tmp_path, channel, clip_id, slot_at, *, status="scheduled", video_id="aaaaaaaaaaa", **extra):
    extra.setdefault("account", ACCOUNT)  # le compte est celui de la publication ; account=None : aucun compte choisi
    out = tmp_path / "output" / video_id
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{clip_id}.mp4").write_bytes(b"mp4")
    (out / f"{clip_id}.json").write_text(json.dumps({
        "video_id": video_id, "clip_id": clip_id, "caption": f"legende {clip_id}", "hashtags": ["#a", "#b"],
        "ready": True}), encoding="utf-8")
    path = tmp_path / "state" / "publish" / f"{channel}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entries.append({
        "video_id": video_id, "clip_id": clip_id, "series_id": None, "part": None, "status": status,
        "slot_at": slot_at.isoformat() if slot_at else None, "decided_at": "2026-01-01T00:00:00+00:00",
        "published_at": None, "error": None, **extra})
    path.write_text(json.dumps(entries), encoding="utf-8")


def _entries(tmp_path, channel="ma_chaine"):
    return json.loads((tmp_path / "state" / "publish" / f"{channel}.json").read_text(encoding="utf-8"))


def _pub_worker(config, publisher, login=None):
    return worker.Worker(config=config, spawner=FakeSpawner(), publisher=publisher, login_checker=login or FakeLogin())


def _recheck(account=ACCOUNT):
    """L'utilisateur clique « J'ai regle le probleme » apres un arret R4 : la case suit la connexion (SPEC-e500 R3)."""
    accounts_mod.clear_halt(Config(mode="review", workspace_dir=Path("workspace"), output_dir=Path("output")), account)


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def _frozen_now(monkeypatch, at: datetime | None = None) -> datetime:
    """Fige l'horloge lue par ``worker._publish_next``/``_publish_one`` sur un seul instant (TASK-b778469259de) :
    sans ca, un test qui seme des entrees avec ``now`` puis appelle ``tick()`` lit l'heure reelle une seconde
    fois (a quelques ms d'ecart) a l'interieur de ``worker.py`` ; un plafond par jour est compte par jour
    calendaire, et les deux lectures peuvent tomber de part et d'autre de minuit. Figer ``worker.datetime.now()``
    sur l'instant deja capture (ou sur ``at``, pour simuler une heure precise) retire cette course, quelle que
    soit l'heure reelle (y compris pile a minuit)."""
    at = at or datetime.now(timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at

    monkeypatch.setattr(worker, "datetime", _Frozen)
    return at


def _same_day_before(now: datetime, **kw) -> datetime:
    """``now - timedelta(**kw)``, sans jamais passer avant minuit du jour de ``now`` (TASK-b778469259de) :
    un ecart de plusieurs minutes « avant maintenant » retomberait la veille si ``now`` est tout pres de
    minuit, ce qui changerait le jour calendaire teste (et donc le resultat du plafond par jour) au lieu
    de rester un detail d'horaire sans incidence sur ce que le test verifie."""
    start_of_day = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
    return max(now - timedelta(**kw), start_of_day)


# Preuve TASK-b778469259de : le compte des tests ci-dessous a son fuseau a "UTC" (_pub_env), donc le plafond
# par jour est compte par jour calendaire UTC (pas Europe/Paris) ; la frontiere qui compte ici est minuit UTC.
# Chaque test parametre ci-dessous est rejoue a l'horloge reelle, puis a 23:58 et 00:02 UTC.
_DAY_BOUNDARY_CASES = [
    pytest.param(None, id="horloge-reelle"),
    pytest.param(datetime(2026, 6, 14, 23, 58, tzinfo=timezone.utc), id="23h58-utc"),
    pytest.param(datetime(2026, 6, 15, 0, 2, tzinfo=timezone.utc), id="00h02-utc"),
]


def test_tick_publishes_a_due_immediate_entry_with_mp4_caption_and_hashtags(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert len(pub.calls) == 1
    call = pub.calls[0]
    assert (call["account"], call["mode"], call["schedule_at"]) == (ACCOUNT, "immediate", None)
    assert call["clip"] == {"video_path": tmp_path / "output" / "aaaaaaaaaaa" / "01.mp4",
                            "caption": "legende 01", "hashtags": ["#a", "#b"]}
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "published" and entry["tiktok_state"] == "published"
    assert (entry["post_url"], entry["post_id"]) == (LINK, "7300000000000000001")
    sidecar = json.loads((tmp_path / "output" / "aaaaaaaaaaa" / "01.json").read_text(encoding="utf-8"))
    assert sidecar["tiktok_post"]["url"] == LINK and sidecar["tiktok_post"]["account"] == ACCOUNT
    assert callable(call["on_tick"])  # le battement du worker continue pendant la publication


def test_tick_leaves_an_immediate_entry_whose_slot_is_not_reached(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", datetime.now(timezone.utc) + timedelta(hours=2))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == [] and _entries(tmp_path)[0]["status"] == "scheduled"


def test_tick_ignores_entries_that_are_not_scheduled(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    for i, status in enumerate(("approved", "rejected", "published", "failed")):
        _seed(tmp_path, "ma_chaine", f"0{i}", _ago(minutes=1), status=status)
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []


def test_scheduled_mode_publishes_once_the_date_is_inside_the_schedule_window(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"publish_mode": "scheduled"})
    inside = datetime.now(timezone.utc) + timedelta(days=3)
    outside = datetime.now(timezone.utc) + timedelta(days=11)
    _seed(tmp_path, "ma_chaine", "01", inside)
    _seed(tmp_path, "ma_chaine", "02", outside)
    pub = FakePublisher()
    w = _pub_worker(config, pub)

    w.tick()
    w.tick()

    assert [(c["mode"], c["schedule_at"]) for c in pub.calls] == [("scheduled", inside.replace(microsecond=inside.microsecond))]
    first, second = _entries(tmp_path)
    assert first["status"] == "published" and first["tiktok_state"] == "scheduled_on_tiktok"
    assert first["tiktok_publish_at"] == inside.isoformat()
    assert second["status"] == "scheduled"  # hors fenetre : attend


def test_an_entry_publish_mode_overrides_the_config_mode(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)  # config : immediate
    slot = datetime.now(timezone.utc) + timedelta(days=2)
    _seed(tmp_path, "ma_chaine", "01", slot, publish_mode="scheduled")
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert [c["mode"] for c in pub.calls] == ["scheduled"]


def test_the_worker_publishes_one_entry_per_tick_one_account_at_a_time(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, channels=("ma_chaine", "autre"),
                      tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=3))
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=2))
    _seed(tmp_path, "autre", "03", _ago(minutes=1), video_id="bbbbbbbbbbb", account="ef34ab")
    pub = FakePublisher()
    w = _pub_worker(config, pub)

    w.tick()
    assert [c["clip"]["caption"] for c in pub.calls] == ["legende 01"]
    w.tick()
    w.tick()
    assert [c["clip"]["caption"] for c in pub.calls] == ["legende 01", "legende 02", "legende 03"]
    assert [c["account"] for c in pub.calls] == [ACCOUNT, ACCOUNT, "ef34ab"]


def test_an_entry_without_an_account_fails_explicitly_and_does_not_publish(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1), account=None)
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "failed"
    assert "aucun compte de publication choisi" in entry["error"]
    events = tiktok.read_events(config=config)
    assert events[-1]["level"] == "error" and "aucun compte de publication choisi" in events[-1]["reason"]


@pytest.mark.parametrize("code, reason", [
    ("captcha", "captcha détecté"),
    ("verification", "vérification de compte demandée"),
    ("login", "connexion expirée"),
    ("element_missing", "élément attendu absent après 30 s : caption_editor"),
    ("unexpected_page", "page inattendue : https://exemple.invalid/erreur"),
])
def test_r4_a_stop_fails_the_entry_with_reason_and_capture_halts_the_account_and_notifies(tmp_path, monkeypatch, code, reason):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=2))
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=1))
    capture = tmp_path / "state" / "browser" / ACCOUNT / "captures" / f"x-{code}.png"
    pub = FakePublisher(error=tiktok.TikTokStop(code, reason, capture))
    w = _pub_worker(config, pub)

    w.tick()

    first, second = _entries(tmp_path)
    assert first["status"] == "failed" and first["error"] == reason
    assert first["capture"] == str(capture) and first["halted"] is True
    assert second["status"] == "scheduled"  # remise en attente, rien de publie derriere
    event = tiktok.read_events(config=config)[-1]
    assert (event["level"], event["account"], event["reason"], event["capture"]) == ("error", ACCOUNT, reason, str(capture))
    assert (event["channel"], event["video_id"], event["clip_id"]) == ("ma_chaine", "aaaaaaaaaaa", "01")

    pub.error = None
    w.tick()  # compte arrete : l'entree suivante n'est pas tentee
    assert len(pub.calls) == 1

    publish.retry("aaaaaaaaaaa", "01", "ma_chaine")  # bouton Reessayer
    _recheck()  # l'arret a decoche la case : elle se recoche a la main
    w.tick()
    assert len(pub.calls) == 2 and _entries(tmp_path)[0]["status"] == "published"


CONTENT_CHECK_REASON = ("vérification de contenu TikTok jamais terminée pour ce clip : "
                        "choisis un autre clip ou réessaie plus tard")


def test_a_content_check_timeout_fails_the_clip_and_does_not_halt_the_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=2))
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=1))
    pub = FakePublisher(error=tiktok.TikTokStop("content_check", CONTENT_CHECK_REASON, None))
    w = _pub_worker(config, pub)

    w.tick()

    first, second = _entries(tmp_path)
    assert first["status"] == "failed" and first["error"] == CONTENT_CHECK_REASON
    assert first["halted"] is False
    assert second["status"] == "scheduled"
    event = tiktok.read_events(config=config)[-1]
    assert (event["level"], event["account"], event["reason"]) == ("error", ACCOUNT, CONTENT_CHECK_REASON)

    pub.error = None
    w.tick()  # le compte n'est pas arrêté : l'entrée due suivante part
    assert len(pub.calls) == 2
    assert _entries(tmp_path)[1]["status"] == "published"


def test_two_content_check_stops_in_a_row_on_two_clips_halt_the_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=3))
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=2))
    _seed(tmp_path, "ma_chaine", "03", _ago(minutes=1))
    pub = FakePublisher(error=tiktok.TikTokStop("content_check", CONTENT_CHECK_REASON, None))
    w = _pub_worker(config, pub)

    w.tick()  # 01 seul : le compte reste ouvert
    assert _entries(tmp_path)[0]["halted"] is False

    w.tick()  # 02 : deuxième clip de suite -> le problème vient du compte, il s'arrête
    first, second, third = _entries(tmp_path)
    assert second["status"] == "failed" and second["halted"] is True
    assert CONTENT_CHECK_REASON in second["error"] and "compte" in second["error"]

    w.tick()  # 03 n'est pas tenté
    assert len(pub.calls) == 2 and third["status"] == "scheduled"


def test_a_publication_between_two_content_check_stops_resets_the_streak(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=3))
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=2))
    _seed(tmp_path, "ma_chaine", "03", _ago(minutes=1))
    pub = FakePublisher(error=tiktok.TikTokStop("content_check", CONTENT_CHECK_REASON, None))
    w = _pub_worker(config, pub)

    w.tick()  # 01 : content_check
    pub.error = None
    w.tick()  # 02 publié : la suite est rompue
    pub.error = tiktok.TikTokStop("content_check", CONTENT_CHECK_REASON, None)
    w.tick()  # 03 : content_check isolé, le compte reste ouvert

    assert _entries(tmp_path)[2]["status"] == "failed" and _entries(tmp_path)[2]["halted"] is False


def test_a_browser_error_fails_and_halts_a_tiktok_error_only_fails_the_entry(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=2))
    w = _pub_worker(config, FakePublisher(error=browser.BrowserError("Chrome est introuvable")))
    w.tick()
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "failed" and "Chrome est introuvable" in entry["error"] and entry["halted"] is True

    publish.retry("aaaaaaaaaaa", "01", "ma_chaine")
    _recheck()
    w.publisher = FakePublisher(error=tiktok.TikTokError("programmation refusée : trop loin"))
    w.tick()
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "failed" and "trop loin" in entry["error"] and entry["halted"] is False


def test_a_resolved_account_halt_without_retrying_the_failed_entry_leaves_new_publications_waiting_with_a_reason(
    tmp_path, monkeypatch,
):
    """I1 (revue r-fable-publication) : « J'ai réglé le problème » efface l'arrêt du COMPTE (R4), pas
    celui de l'entrée restée 'failed'/'halted' (elle ne s'efface que par « Réessayer » ou son
    annulation) ; tant qu'elle n'est pas retentée, les publications suivantes du compte ne doivent pas
    rester bloquées en silence (ADR-ad2e) mais en attente avec une raison visible."""
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=2))
    w = _pub_worker(config, FakePublisher(error=tiktok.TikTokStop("captcha", "captcha détecté", None)))
    w.tick()
    assert _entries(tmp_path)[0]["status"] == "failed" and _entries(tmp_path)[0]["halted"] is True

    _recheck()  # l'utilisateur resout le captcha, « J'ai regle le probleme » (le compte seul, pas l'entree 01)
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=1))
    w.publisher = FakePublisher()
    w.tick()

    assert w.publisher.calls == []  # l'entree 01 en echec arrete encore le compte : rien ne part
    second = _entries(tmp_path)[1]
    assert second["status"] == "scheduled"
    assert second["waiting_reason"] and "aaaaaaaaaaa/01" in second["waiting_reason"]
    events = tiktok.read_events(config=config)
    assert events[-1]["level"] == "warn" and "aaaaaaaaaaa/01" in events[-1]["reason"]


def test_a_corrupt_publish_file_does_not_kill_the_worker_tick(tmp_path, monkeypatch, caplog):
    """M2 (revue r-fable-publication) : un fichier state/publish/<chaine>.json tronque levait un
    JSONDecodeError qui traversait _publish_due (jamais rattrapee) et tuait le worker ; desormais elle
    est journalisee une fois (ADR-ad2e) et ne bloque pas le tick suivant."""
    config = _pub_env(tmp_path, monkeypatch)
    path = tmp_path / "state" / "publish" / "ma_chaine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[{"not": "closed"', encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        worker.Worker(config=config, spawner=FakeSpawner()).tick()  # ne leve pas

    assert "publication TikTok impossible" in caplog.text


def test_an_invalid_slot_at_in_the_publish_file_does_not_kill_the_worker_tick(tmp_path, monkeypatch, caplog):
    """M2 (revue r-fable-publication) : un slot_at non ISO (jamais valide par _validate_entry) leve un
    ValueError dans _publish_next qui traversait _publish_due et tuait le worker."""
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    path = tmp_path / "state" / "publish" / "ma_chaine.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["slot_at"] = "2026-10-05 18:00 Paris"
    path.write_text(json.dumps(entries), encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        worker.Worker(config=config, spawner=FakeSpawner()).tick()  # ne leve pas

    assert "publication TikTok impossible" in caplog.text


def test_an_unexpected_error_is_logged_and_fails_the_entry_instead_of_killing_the_worker(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))

    with caplog.at_level(logging.ERROR):
        _pub_worker(config, FakePublisher(error=RuntimeError("boum"))).tick()

    entry = _entries(tmp_path)[0]
    assert entry["status"] == "failed" and "RuntimeError" in entry["error"] and "boum" in entry["error"]
    assert "boum" in caplog.text


@pytest.mark.parametrize("frozen_at", _DAY_BOUNDARY_CASES)
def test_r6_posts_per_day_cap_postpones_to_the_next_free_slot_and_logs_it(tmp_path, monkeypatch, caplog, frozen_at):
    config = _pub_env(tmp_path, monkeypatch)  # 1 post par jour, 480 min d'ecart
    now = _frozen_now(monkeypatch, frozen_at)  # TASK-b778469259de : un seul instant pour le seed et pour le tick
    _seed(tmp_path, "ma_chaine", "00", now - timedelta(seconds=1), status="published",
          tiktok_publish_at=(now - timedelta(seconds=1)).isoformat(), published_at=now.isoformat())
    _seed(tmp_path, "ma_chaine", "01", now - timedelta(minutes=1))
    pub = FakePublisher()

    with caplog.at_level(logging.WARNING):
        _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "01")
    assert entry["status"] == "scheduled"
    new_slot = datetime.fromisoformat(entry["slot_at"])
    assert new_slot > now and new_slot.date() != now.date()
    assert "plafond de 1 publication(s) par jour" in entry["postponed_reason"]
    assert "reporté" in caplog.text and "01" in caplog.text


def test_r6_min_gap_postpones_even_when_the_daily_cap_is_not_reached(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 9, "min_gap_minutes": 600})
    now = datetime.now(timezone.utc)
    _seed(tmp_path, "ma_chaine", "00", _ago(minutes=5), status="published",
          tiktok_publish_at=(now - timedelta(minutes=5)).isoformat(), published_at=now.isoformat())
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "01")
    assert "600 minutes" in entry["postponed_reason"]
    assert datetime.fromisoformat(entry["slot_at"]) >= now + timedelta(minutes=595)


@pytest.mark.parametrize("frozen_at", _DAY_BOUNDARY_CASES)
def test_r6_a_published_post_counts_for_the_account_across_channels(tmp_path, monkeypatch, frozen_at):
    config = _pub_env(tmp_path, monkeypatch)
    (tmp_path / "presets" / "autre.toml").write_text(
        '[channel]\ntimezone = "UTC"\n', encoding="utf-8")
    now = _frozen_now(monkeypatch, frozen_at)  # TASK-b778469259de : un seul instant pour le seed et pour le tick
    _seed(tmp_path, "autre", "00", _same_day_before(now, minutes=5), status="published", video_id="bbbbbbbbbbb",
          tiktok_publish_at=(now - timedelta(seconds=5)).isoformat(), published_at=now.isoformat())
    _seed(tmp_path, "ma_chaine", "01", _same_day_before(now, minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []


def test_a_postpone_error_on_one_due_entry_does_not_skip_the_other_due_entries(tmp_path, monkeypatch, caplog):
    # TASK-748696ea666f : postpone leve PublishError pour la 1re entree due (plafond sans creneau libre) ;
    # la 2e entree due, sur un autre compte, doit quand meme partir dans ce meme passage
    config = _pub_env(tmp_path, monkeypatch, channels=("ma_chaine", "autre"),
                      tiktok_settings={"max_posts_per_day": 1, "min_gap_minutes": 0})
    now = _frozen_now(monkeypatch, datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc))
    _seed(tmp_path, "ma_chaine", "00", now - timedelta(minutes=10), status="published",
          tiktok_publish_at=(now - timedelta(minutes=5)).isoformat(), published_at=now.isoformat())
    _seed(tmp_path, "ma_chaine", "01", now - timedelta(minutes=3))
    _seed(tmp_path, "autre", "02", now - timedelta(minutes=2), video_id="bbbbbbbbbbb", account="ef34ab")

    def no_free_slot(*args, **kwargs):
        raise publish.PublishError("aucun créneau libre avant la fin du plafond")

    monkeypatch.setattr(publish, "postpone", no_free_slot)
    pub = FakePublisher()

    with caplog.at_level(logging.WARNING):
        _pub_worker(config, pub).tick()

    assert [(c["account"], c["clip"]["caption"]) for c in pub.calls] == [("ef34ab", "legende 02")]
    waiting = next(e for e in _entries(tmp_path) if e["clip_id"] == "01")
    assert waiting["status"] == "scheduled"
    assert "aucun créneau libre" in waiting["waiting_reason"]
    assert "aucun créneau libre" in caplog.text and "01" in caplog.text


def test_a_broken_publish_file_is_logged_once_and_does_not_kill_the_worker(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    path = tmp_path / "state" / "publish" / "ma_chaine.json"
    path.parent.mkdir(parents=True)
    path.write_text('[{"video_id": "x"}]', encoding="utf-8")
    w = _pub_worker(config, FakePublisher())

    with caplog.at_level(logging.ERROR):
        w.tick()
        w.tick()

    assert caplog.text.count("publication TikTok impossible") == 1


# --------------------------------------------------------------------------
# Releve periodique des statistiques TikTok (SPEC-9225 R7)
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_stats_fetch(monkeypatch):
    """Un compte pret est releve par le worker (SPEC-86fe R4) : aucun test n'ouvre jamais le vrai navigateur,
    ceux du releve injectent leur propre ``stats_fetcher``."""
    monkeypatch.setattr(tiktok, "fetch_stats", lambda account, **kwargs: {"account": account})


def _write_snapshot(tmp_path, account, at, origin="full", posts=()):
    """Un releve de l'historique du compte (SPEC-86fe R2) : state/stats/tiktok/<compte>/<horodatage>.json."""
    path = tmp_path / "state" / "stats" / "tiktok" / account / f"{at.strftime('%Y%m%dT%H%M%S%f')}Z.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"account": account, "fetched_at": at.isoformat(), "origin": origin, "overview": None,
                                "posts": list(posts)}), encoding="utf-8")


class FakeStatsFetcher:
    """Remplace tiktok.fetch_stats : enregistre les appels, ecrit un releve ou leve ``error``."""

    def __init__(self, tmp_path, error=None):
        self.tmp, self.calls, self.error = tmp_path, [], error

    def __call__(self, account, *, config=None, on_tick=None, **kwargs):
        self.calls.append({"account": account, "on_tick": on_tick})
        if self.error is not None:
            raise self.error
        _write_snapshot(self.tmp, account, datetime.now(timezone.utc))
        return {"account": account}


def _stats_worker(config, fetcher):
    return worker.Worker(config=config, spawner=FakeSpawner(), publisher=FakePublisher(), stats_fetcher=fetcher,
                         login_checker=FakeLogin())


def _published_clip(tmp_path, clip_id, account=ACCOUNT, *, video_id="aaaaaaaaaaa", post_id="7300000000000000001"):
    out = tmp_path / "output" / video_id
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{clip_id}.json").write_text(json.dumps({
        "video_id": video_id, "clip_id": clip_id,
        "tiktok_post": {"url": LINK, "id": post_id, "state": "published", "account": account}}), encoding="utf-8")


def test_tick_fetches_the_stats_of_a_ready_account_then_waits_the_interval(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    fetcher = FakeStatsFetcher(tmp_path)
    w = _stats_worker(config, fetcher)

    w.tick()
    w.tick()

    assert [c["account"] for c in fetcher.calls] == [ACCOUNT]  # une fois : le releve est recent
    assert callable(fetcher.calls[0]["on_tick"])  # le battement du worker continue pendant le releve


def test_tick_never_fetches_stats_when_the_interval_is_zero(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"stats_interval_h": 0})
    fetcher = FakeStatsFetcher(tmp_path)
    w = _stats_worker(config, fetcher)

    for _ in range(3):
        w.tick()

    assert fetcher.calls == []  # releve seulement a l'usage (SPEC-47e2 R4) : le worker ne releve jamais


def test_a_negative_stats_interval_is_a_logged_error_and_no_fetch(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"stats_interval_h": -1})
    fetcher = FakeStatsFetcher(tmp_path)

    with caplog.at_level(logging.ERROR):
        _stats_worker(config, fetcher).tick()

    assert fetcher.calls == [] and "stats_interval_h" in caplog.text


def test_tick_refetches_once_the_configured_interval_has_passed(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"stats_interval_h": 2})
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    _write_snapshot(tmp_path, ACCOUNT, datetime.now(timezone.utc) - timedelta(hours=3))
    fetcher = FakeStatsFetcher(tmp_path)

    _stats_worker(config, fetcher).tick()

    assert len(fetcher.calls) == 1


def test_an_opportunistic_snapshot_does_not_postpone_the_periodic_fetch(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"stats_interval_h": 2})
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    _write_snapshot(tmp_path, ACCOUNT, datetime.now(timezone.utc) - timedelta(minutes=5), origin="opportunistic")
    fetcher = FakeStatsFetcher(tmp_path)

    _stats_worker(config, fetcher).tick()

    assert len(fetcher.calls) == 1


def test_tick_does_not_open_a_browser_for_an_account_that_is_not_ready_to_publish(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _set_account_state(tmp_path, ACCOUNT, ready_to_publish=False)
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    fetcher = FakeStatsFetcher(tmp_path)

    _stats_worker(config, fetcher).tick()

    assert fetcher.calls == []  # SPEC-86fe R4 : un compte non prêt n'est pas relevé


def test_a_ready_account_without_any_clipper_post_is_measured_too(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)  # aucun clip publie : la liste des posts vient de TikTok
    fetcher = FakeStatsFetcher(tmp_path)

    _stats_worker(config, fetcher).tick()

    assert [c["account"] for c in fetcher.calls] == [ACCOUNT]


def test_tick_skips_the_stats_of_an_account_halted_by_a_safe_stop(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    w = worker.Worker(config=config, spawner=FakeSpawner(),
                      publisher=FakePublisher(error=tiktok.TikTokStop("captcha", "captcha détecté", None)),
                      stats_fetcher=FakeStatsFetcher(tmp_path), login_checker=FakeLogin())

    w.tick()  # la publication s'arrete sur captcha : compte arrete
    w.tick()

    assert w.stats_fetcher.calls == []


def test_tick_does_not_fetch_stats_in_the_tick_that_drove_a_publication(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    fetcher = FakeStatsFetcher(tmp_path)
    pub = FakePublisher()
    w = worker.Worker(config=config, spawner=FakeSpawner(), publisher=pub, stats_fetcher=fetcher, login_checker=FakeLogin())

    w.tick()
    assert len(pub.calls) == 1 and fetcher.calls == []  # un seul pilotage du navigateur par iteration
    w.tick()
    assert len(fetcher.calls) == 1


def test_tick_fetches_one_account_per_iteration(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, channels=("ma_chaine", "autre"))
    fetcher = FakeStatsFetcher(tmp_path)
    w = _stats_worker(config, fetcher)

    w.tick()
    assert [c["account"] for c in fetcher.calls] == [ACCOUNT]
    w.tick()
    assert [c["account"] for c in fetcher.calls] == [ACCOUNT, "ef34ab"]


@pytest.mark.parametrize("error", [
    tiktok.TikTokStop("captcha", "captcha détecté : arrêt immédiat", None),
    browser.BrowserError("Chrome introuvable"),
    tiktok.TikTokError("réglage invalide"),
])
def test_a_failed_stats_fetch_is_logged_once_not_retried_every_tick_and_never_kills_the_worker(
        tmp_path, monkeypatch, caplog, error):
    config = _pub_env(tmp_path, monkeypatch)
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    fetcher = FakeStatsFetcher(tmp_path, error=error)
    w = _stats_worker(config, fetcher)

    with caplog.at_level(logging.ERROR):
        w.tick()
        w.tick()
        w.tick()

    assert len(fetcher.calls) == 1
    assert caplog.text.count("relevé des statistiques TikTok impossible") == 1
    assert str(error) in caplog.text


def test_an_unexpected_error_in_the_stats_fetch_is_logged_not_fatal(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    w = _stats_worker(config, FakeStatsFetcher(tmp_path, error=RuntimeError("boom")))

    with caplog.at_level(logging.ERROR):
        w.tick()

    assert "boom" in caplog.text


# --------------------------------------------------------------------------
# SPEC-00d1 R2-R4, R6 : comptes prets, connexion verifiee, compte choisi par publication
# --------------------------------------------------------------------------

OTHER = "ef34ab"


def _set_account_state(tmp_path, account, **fields):
    path = tmp_path / "state" / "accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    next(a for a in data["accounts"] if a["id"] == account).update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


def _account_state(tmp_path, account):
    data = json.loads((tmp_path / "state" / "accounts.json").read_text(encoding="utf-8"))
    return next(a for a in data["accounts"] if a["id"] == account)


def test_an_entry_whose_account_is_not_ready_is_not_attempted_and_waits_with_the_reason(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _set_account_state(tmp_path, ACCOUNT, ready_to_publish=False)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub, login = FakePublisher(), FakeLogin()

    with caplog.at_level(logging.WARNING):
        _pub_worker(config, pub, login).tick()

    assert pub.calls == [] and login.calls == []  # ni publication, ni lecture de cookies pour un compte non prêt
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "scheduled" and entry["error"] is None  # en attente, pas en échec
    assert "non prêt à publier" in entry["waiting_reason"] and "A" in entry["waiting_reason"]
    assert "Comptes > A > J'ai réglé le problème" in entry["waiting_reason"]  # plus de « coche prêt à publier » périmé
    assert "coche" not in entry["waiting_reason"]
    assert "non prêt à publier" in caplog.text
    event = tiktok.read_events(config=config)[-1]
    assert event["level"] == "warn" and event["account"] == ACCOUNT and event["clip_id"] == "01"


def test_a_not_ready_account_never_falls_back_to_another_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)  # le compte de la chaine (ab12cd) est prêt, l'autre non
    _set_account_state(tmp_path, OTHER, ready_to_publish=False)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1), account=OTHER)
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []  # ni l'autre compte, ni celui de la chaine
    assert "non prêt à publier" in _entries(tmp_path)[0]["waiting_reason"]


def test_the_account_chosen_for_the_publication_is_the_one_the_worker_uses(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1), account=OTHER)  # la chaine pointe ab12cd
    pub, login = FakePublisher(), FakeLogin()

    _pub_worker(config, pub, login).tick()

    assert [c["account"] for c in pub.calls] == [OTHER]
    assert login.calls == [OTHER]  # c'est la connexion de CE compte qui est verifiee
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "published" and entry["waiting_reason"] is None
    sidecar = json.loads((tmp_path / "output" / "aaaaaaaaaaa" / "01.json").read_text(encoding="utf-8"))
    assert sidecar["tiktok_post"]["account"] == OTHER


def test_an_entry_without_account_field_never_falls_back_to_a_style_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    path = tmp_path / "state" / "publish" / "ma_chaine.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    del entries[0]["account"]  # file d'avant R4 : aucun champ account
    path.write_text(json.dumps(entries), encoding="utf-8")
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    assert _entries(tmp_path)[0]["status"] == "failed"


@pytest.mark.parametrize("frozen_at", _DAY_BOUNDARY_CASES)
def test_a_capped_entry_is_postponed_to_a_slot_of_its_account_not_of_a_style(tmp_path, monkeypatch, frozen_at):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 1, "min_gap_minutes": 0},
                      slots=[{"day": d, "time": "09:00"} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")])
    now = _frozen_now(monkeypatch, frozen_at)  # TASK-b778469259de : un seul instant pour le seed et pour le tick
    _seed(tmp_path, "ma_chaine", "00", _same_day_before(now, minutes=5), status="published", video_id="bbbbbbbbbbb",
          tiktok_publish_at=(now - timedelta(seconds=5)).isoformat(), published_at=now.isoformat())
    _seed(tmp_path, "ma_chaine", "01", _same_day_before(now, minutes=1))  # plafond du jour atteint par 00
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "01")
    moved = datetime.fromisoformat(entry["slot_at"])
    assert moved > now and (moved.hour, moved.minute) == (9, 0)  # un creneau 09:00 du compte
    assert entry["postponed_reason"]


@pytest.mark.parametrize("frozen_at", _DAY_BOUNDARY_CASES)
def test_a_capped_entry_whose_account_has_no_slot_waits_with_an_explicit_reason(tmp_path, monkeypatch, frozen_at):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 1, "min_gap_minutes": 0}, slots=[])
    now = _frozen_now(monkeypatch, frozen_at)  # TASK-b778469259de : un seul instant pour le seed et pour le tick
    _seed(tmp_path, "ma_chaine", "00", _same_day_before(now, minutes=5), status="published", video_id="bbbbbbbbbbb",
          tiktok_publish_at=(now - timedelta(seconds=5)).isoformat(), published_at=now.isoformat())
    _seed(tmp_path, "ma_chaine", "01", _same_day_before(now, minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "01")
    assert entry["status"] == "scheduled" and "aucun créneau" in entry["waiting_reason"]


def test_the_daily_cap_is_counted_in_the_accounts_timezone_not_the_styles(tmp_path, monkeypatch):
    """Revue r-comptes 9 : check_limits/postpone comptent le jour dans le fuseau du COMPTE, jamais celui du
    style. Compte en Asia/Tokyo, style en America/Los_Angeles (17 h d'ecart) : deux posts separes de 22 h
    tombent le meme jour a Tokyo (plafond de 1/jour atteint) mais sur deux jours differents a Los Angeles
    (le plafond serait loupe si le fuseau du style etait pris a tort)."""
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 1, "min_gap_minutes": 0},
                      slots=[{"day": d, "time": "09:00"} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")])
    accounts_path = tmp_path / "state" / "accounts.json"
    data = json.loads(accounts_path.read_text(encoding="utf-8"))
    for account in data["accounts"]:
        if account["id"] == ACCOUNT:
            account["timezone"] = "Asia/Tokyo"
    accounts_path.write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "presets" / "ma_chaine.toml").write_text(
        '[channel]\ntimezone = "America/Los_Angeles"\n', encoding="utf-8")

    published_at = datetime(2026, 1, 1, 16, 0, tzinfo=timezone.utc)   # Tokyo 2026-01-02 01:00, LA 2026-01-01 08:00
    target_at = datetime(2026, 1, 2, 14, 0, tzinfo=timezone.utc)      # Tokyo 2026-01-02 23:00, LA 2026-01-02 06:00
    _seed(tmp_path, "ma_chaine", "00", published_at, status="published", video_id="bbbbbbbbbbb",
          tiktok_publish_at=published_at.isoformat(), published_at=published_at.isoformat())
    _seed(tmp_path, "ma_chaine", "01", target_at, publish_mode="scheduled")
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []  # meme jour a Tokyo (compte) : plafond atteint, publication reportee
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "01")
    assert entry["status"] == "scheduled" and entry["postponed_reason"]
    assert "plafond de 1 publication(s) par jour" in entry["postponed_reason"]


def test_an_entry_with_an_unknown_account_waits_with_the_reason(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1), account="supprime")
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    assert "introuvable" in _entries(tmp_path)[0]["waiting_reason"]


def test_a_waiting_entry_does_not_block_the_next_due_entry_of_a_ready_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _set_account_state(tmp_path, OTHER, ready_to_publish=False)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=3), account=OTHER)
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=2))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert [c["clip"]["caption"] for c in pub.calls] == ["legende 02"]
    first, second = _entries(tmp_path)
    assert first["status"] == "scheduled" and first["waiting_reason"] and second["status"] == "published"


def test_the_entry_is_attempted_again_once_the_account_is_ready_and_the_reason_is_cleared(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _set_account_state(tmp_path, ACCOUNT, ready_to_publish=False)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher()
    w = _pub_worker(config, pub)
    w.tick()
    assert pub.calls == [] and _entries(tmp_path)[0]["waiting_reason"]

    _recheck()
    w.tick()

    assert len(pub.calls) == 1 and _entries(tmp_path)[0]["status"] == "published"
    assert _entries(tmp_path)[0]["waiting_reason"] is None


def test_the_reason_is_logged_and_notified_once_not_at_every_tick(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _set_account_state(tmp_path, ACCOUNT, ready_to_publish=False)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    w = _pub_worker(config, FakePublisher())

    with caplog.at_level(logging.WARNING):
        w.tick()
        w.tick()
        w.tick()

    assert caplog.text.count("publication en attente") == 1
    assert len(tiktok.read_events(config=config)) == 1


def test_the_connection_is_verified_before_each_publication(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=2))
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=1))
    login = FakeLogin()
    w = _pub_worker(config, FakePublisher(), login)

    w.tick()
    w.tick()

    assert login.calls == [ACCOUNT, ACCOUNT]


def test_an_expired_session_before_publishing_unticks_ready_and_the_entry_waits(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub, FakeLogin(**{ACCOUNT: "expired"})).tick()

    assert pub.calls == []
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "scheduled" and "session TikTok expirée" in entry["waiting_reason"]
    assert "décoché" in entry["waiting_reason"]
    stored = _account_state(tmp_path, ACCOUNT)
    assert stored["ready_to_publish"] is False and "décoché automatiquement" in stored["ready_note"]
    assert stored["login"]["state"] == "expired"


def test_a_never_connected_profile_before_publishing_waits_without_publishing(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub, FakeLogin(**{ACCOUNT: "never"})).tick()

    assert pub.calls == [] and "non connecté à TikTok" in _entries(tmp_path)[0]["waiting_reason"]


def test_a_connection_that_cannot_be_verified_waits_with_the_error_and_nothing_is_published(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher()
    login = FakeLogin(**{ACCOUNT: browser.BrowserError("cookies du profil illisibles : ferme la fenêtre Chrome")})

    _pub_worker(config, pub, login).tick()

    assert pub.calls == []
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "scheduled" and "non vérifiable" in entry["waiting_reason"]
    assert "ferme la fenêtre Chrome" in entry["waiting_reason"]
    assert _account_state(tmp_path, ACCOUNT)["ready_to_publish"] is True  # pas de verification : pas de decochage


@pytest.mark.parametrize("error", [
    tiktok.TikTokStop("captcha", "captcha détecté", None),
    browser.BrowserError("Chrome est introuvable"),
    RuntimeError("boum"),
])
def test_an_r4_stop_unticks_ready_until_the_user_ticks_it_again(tmp_path, monkeypatch, caplog, error):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))

    with caplog.at_level(logging.WARNING):
        _pub_worker(config, FakePublisher(error=error)).tick()

    stored = _account_state(tmp_path, ACCOUNT)
    assert stored["ready_to_publish"] is False and "arrêt de publication" in stored["ready_note"]
    assert "prêt à publier" in caplog.text and "décoché" in caplog.text
    assert _account_state(tmp_path, OTHER)["ready_to_publish"] is True  # les autres comptes ne bougent pas


def test_an_error_that_does_not_halt_the_account_keeps_ready_ticked(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))

    _pub_worker(config, FakePublisher(error=tiktok.TikTokError("programmation refusée"))).tick()

    assert _account_state(tmp_path, ACCOUNT)["ready_to_publish"] is True
    assert _entries(tmp_path)[0]["failed_at"]  # l'echec est date


def test_a_halt_on_the_chosen_account_does_not_block_the_channel_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=2), account=OTHER)
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=1))
    pub = FakePublisher(error=tiktok.TikTokStop("captcha", "captcha détecté", None))
    w = _pub_worker(config, pub)

    w.tick()  # l'entree 01 (compte OTHER) s'arrete
    pub.error = None
    w.tick()

    assert [c["account"] for c in pub.calls] == [OTHER, ACCOUNT]
    assert _entries(tmp_path)[1]["status"] == "published"


# --------------------------------------------------------------------------
# SPEC-1ed3 R4 : publications pilotees depuis l'ecran Publication (entrees manuelles)
# --------------------------------------------------------------------------

NO_CHANNEL = "_sans_chaine"
_OPTIONS = {"visibility": "friends", "allow_comments": False, "allow_reuse": True, "ai_generated": True,
            "content_check": "wait"}


def _manual(tmp_path, clip_id, slot_at, *, channel=NO_CHANNEL, mode="immediate", account="ef34ab", options=None,
            **extra):
    _seed(tmp_path, channel, clip_id, slot_at, publish_mode=mode, account=account, manual=True,
          post_options=_OPTIONS if options is None else options, **extra)


def test_now_is_due_right_away_for_a_video_without_channel_and_transmits_the_post_options(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert len(pub.calls) == 1
    call = pub.calls[0]
    assert (call["account"], call["mode"], call["schedule_at"]) == ("ef34ab", "immediate", None)
    assert call["options"] == _OPTIONS  # reglages par post transmis a clipper.tiktok
    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "published" and entry["post_url"] == LINK and entry["in_progress_since"] is None


def test_an_entry_without_post_options_does_not_pass_options(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert "options" not in pub.calls[0]


def test_the_entry_is_in_progress_while_the_browser_is_driven(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc))
    seen = []
    pub = FakePublisher(during=lambda: seen.append(_entries(tmp_path, NO_CHANNEL)[0].get("in_progress_since")))

    _pub_worker(config, pub).tick()

    assert seen and seen[0]  # « en cours » : ni modifiable ni annulable pendant le pilotage


def test_a_failed_publication_clears_in_progress_and_keeps_the_reason(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc))
    pub = FakePublisher(error=tiktok.TikTokStop("captcha", "captcha détecté", None))

    _pub_worker(config, pub).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and entry["error"] == "captcha détecté" and entry["in_progress_since"] is None


def test_scheduled_inside_the_tiktok_window_is_scheduled_on_tiktok(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    when = datetime.now(timezone.utc) + timedelta(days=3)
    _manual(tmp_path, "01", when, mode="scheduled")
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert [(c["mode"], c["schedule_at"]) for c in pub.calls] == [("scheduled", when)]
    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "published" and entry["tiktok_state"] == "scheduled_on_tiktok"


def test_scheduled_beyond_the_window_is_kept_then_scheduled_once_the_date_enters_the_window(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"schedule_max_days": 1})
    far = datetime.now(timezone.utc) + timedelta(hours=30)
    _manual(tmp_path, "01", far, mode="scheduled")
    pub = FakePublisher()
    w = _pub_worker(config, pub)

    w.tick()
    assert pub.calls == [] and _entries(tmp_path, NO_CHANNEL)[0]["status"] == "scheduled"  # gardee par Clipper

    # le temps passe : la date entre dans la fenetre de TikTok (24 h)
    path = tmp_path / "state" / "publish" / f"{NO_CHANNEL}.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    near = datetime.now(timezone.utc) + timedelta(hours=20)
    entries[0]["slot_at"] = near.isoformat()
    path.write_text(json.dumps(entries), encoding="utf-8")
    w.tick()

    assert [(c["mode"], c["schedule_at"]) for c in pub.calls] == [("scheduled", near)]
    assert _entries(tmp_path, NO_CHANNEL)[0]["tiktok_state"] == "scheduled_on_tiktok"


@pytest.mark.parametrize("frozen_at", _DAY_BOUNDARY_CASES)
def test_a_manual_entry_over_the_account_cap_waits_with_the_reason_and_is_never_moved(tmp_path, monkeypatch, frozen_at):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 1, "min_gap_minutes": 0})
    slot = _frozen_now(monkeypatch, frozen_at)  # TASK-b778469259de : un seul instant pour le seed et pour le tick
    _seed(tmp_path, "ma_chaine", "00", slot - timedelta(seconds=5), status="published",
          published_at=(slot - timedelta(seconds=5)).isoformat(), account="ef34ab")
    _manual(tmp_path, "01", slot)
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "scheduled" and entry["slot_at"] == slot.isoformat()  # aucun report silencieux
    assert "plafond" in entry["waiting_reason"]


def test_a_manual_entry_of_an_account_not_ready_is_not_attempted(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc), account="inconnu")
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    assert "inconnu" in _entries(tmp_path, NO_CHANNEL)[0]["waiting_reason"]


def test_starting_the_worker_fails_an_entry_left_in_progress(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc), in_progress_since="2026-10-01T10:00:00+00:00")

    _pub_worker(config, FakePublisher()).startup()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and "interrompue" in entry["error"] and entry["in_progress_since"] is None


# --------------------------------------------------------------------------
# TASK-9776 : le worker publie sur YouTube les entrees d'un compte YouTube (SPEC-5e50 R2-R5)
# La publication est injectee (fausse) : aucun navigateur, aucun YouTube.
# --------------------------------------------------------------------------

from clipper import youtube  # noqa: E402

SHORT_URL = "https://youtube.com/shorts/OOOeOwbvu34"


class FakeYouTubePublisher:
    """Remplace youtube.publish : enregistre les appels, rend un resultat ou leve ``error``."""

    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, clip, account, *, mode, schedule_at=None, config=None, on_tick=None, **kwargs):
        self.calls.append({"clip": clip, "account": account, "mode": mode, "schedule_at": schedule_at,
                           "on_tick": on_tick, **kwargs})
        if self.error is not None:
            raise self.error
        scheduled = mode == "scheduled"
        return {"post_url": SHORT_URL, "post_id": "OOOeOwbvu34",
                "state": "scheduled_on_youtube" if scheduled else "published",
                "publish_at": (schedule_at if scheduled else datetime.now(timezone.utc)).isoformat(), "note": None}


def _youtube_env(tmp_path, monkeypatch, *, youtube_settings=None, tiktok_settings=None):
    """``_pub_env`` dont le compte ``ACCOUNT`` est un compte YouTube (l'autre reste TikTok)."""
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings=tiktok_settings)
    path = tmp_path / "state" / "accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for account in data["accounts"]:
        if account["id"] == ACCOUNT:
            account.update(service="youtube", login={**_CONNECTED, "channel": {"name": "Ma Chaîne", "id": "UCabc"}})
    path.write_text(json.dumps(data), encoding="utf-8")
    config._sections["youtube"] = dict(youtube_settings or {})
    return config


def _yt_worker(config, yt_publisher, tt_publisher=None, login=None, fetcher=None):
    return worker.Worker(config=config, spawner=FakeSpawner(), publisher=tt_publisher or FakePublisher(),
                         youtube_publisher=yt_publisher, login_checker=login or FakeLogin(),
                         stats_fetcher=fetcher or (lambda account, **kw: {"account": account}))


def test_a_youtube_account_entry_goes_through_clipper_youtube_not_tiktok(tmp_path, monkeypatch):
    config = _youtube_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    sidecar_path = tmp_path / "output" / "aaaaaaaaaaa" / "01.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar_path.write_text(json.dumps({**sidecar, "screen_title": "Le titre"}), encoding="utf-8")
    yt, tt, login = FakeYouTubePublisher(), FakePublisher(), FakeLogin()

    _yt_worker(config, yt, tt, login).tick()

    assert tt.calls == [] and len(yt.calls) == 1
    call = yt.calls[0]
    assert (call["account"], call["mode"], call["schedule_at"]) == (ACCOUNT, "immediate", None)
    assert call["clip"] == {"video_path": tmp_path / "output" / "aaaaaaaaaaa" / "01.mp4", "caption": "legende 01",
                            "hashtags": ["#a", "#b"], "screen_title": "Le titre"}
    assert callable(call["on_tick"]) and login.calls == []  # pas de lecture des cookies TikTok pour un compte YouTube
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "published" and entry["service"] == "youtube"
    assert (entry["post_url"], entry["post_id"], entry["tiktok_state"]) == (SHORT_URL, "OOOeOwbvu34", "published")
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["youtube_post"]["url"] == SHORT_URL and sidecar["youtube_post"]["account"] == ACCOUNT
    assert "tiktok_post" not in sidecar
    event = tiktok.read_events(config=config)[-1]
    assert event["level"] == "info" and SHORT_URL in event["reason"]


def test_a_tiktok_account_entry_still_goes_through_tiktok_in_the_same_run(tmp_path, monkeypatch):
    config = _youtube_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1), account="ef34ab")
    yt, tt = FakeYouTubePublisher(), FakePublisher()

    _yt_worker(config, yt, tt).tick()

    assert yt.calls == [] and len(tt.calls) == 1 and tt.calls[0]["account"] == "ef34ab"


def test_a_scheduled_youtube_entry_is_programmed_on_youtube_and_recorded(tmp_path, monkeypatch):
    config = _youtube_env(tmp_path, monkeypatch)
    slot = datetime.now(timezone.utc) + timedelta(days=20)  # hors fenetre TikTok (10 j), dans celle de YouTube (30 j)
    _seed(tmp_path, "ma_chaine", "01", slot, publish_mode="scheduled")
    yt = FakeYouTubePublisher()

    _yt_worker(config, yt).tick()

    assert len(yt.calls) == 1 and yt.calls[0]["mode"] == "scheduled" and yt.calls[0]["schedule_at"] == slot
    entry = _entries(tmp_path)[0]
    assert entry["tiktok_state"] == "scheduled_on_youtube" and entry["status"] == "published"
    assert datetime.fromisoformat(entry["tiktok_publish_at"]) == slot


def test_a_scheduled_youtube_entry_outside_the_youtube_window_waits(tmp_path, monkeypatch):
    config = _youtube_env(tmp_path, monkeypatch, youtube_settings={"schedule_max_days": 5})
    _seed(tmp_path, "ma_chaine", "01", datetime.now(timezone.utc) + timedelta(days=6), publish_mode="scheduled")
    yt = FakeYouTubePublisher()

    _yt_worker(config, yt).tick()

    assert yt.calls == [] and _entries(tmp_path)[0]["status"] == "scheduled"


def test_a_youtube_stop_fails_the_entry_with_reason_and_capture_halts_the_account_and_notifies(tmp_path, monkeypatch):
    config = _youtube_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=2))
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=1))
    capture = tmp_path / "state" / "browser" / ACCOUNT / "captures" / "x-captcha.png"
    yt = FakeYouTubePublisher(error=youtube.YouTubeStop("captcha", "captcha détecté : arrêt immédiat", capture))
    w = _yt_worker(config, yt)

    w.tick()
    w.tick()

    first, second = _entries(tmp_path)
    assert first["status"] == "failed" and first["error"] == "captcha détecté : arrêt immédiat"
    assert first["capture"] == str(capture) and first["halted"] is True
    assert second["status"] == "scheduled" and len(yt.calls) == 1  # compte arrete : rien derriere
    event = next(e for e in tiktok.read_events(config=config) if e["level"] == "error")
    assert (event["account"], event["capture"]) == (ACCOUNT, str(capture))


def test_a_youtube_setting_error_fails_the_entry_without_halting_the_account(tmp_path, monkeypatch):
    config = _youtube_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    yt = FakeYouTubePublisher(error=youtube.YouTubeError("titre manquant"))

    _yt_worker(config, yt).tick()

    entry = _entries(tmp_path)[0]
    assert entry["status"] == "failed" and entry["error"] == "titre manquant" and not entry["halted"]


@pytest.mark.parametrize("frozen_at", _DAY_BOUNDARY_CASES)
def test_the_youtube_daily_cap_postpones_and_logs_the_report(tmp_path, monkeypatch, caplog, frozen_at):
    config = _youtube_env(tmp_path, monkeypatch, youtube_settings={"max_posts_per_day": 1, "min_gap_minutes": 0})
    now = _frozen_now(monkeypatch, frozen_at)  # TASK-b778469259de : un seul instant pour le seed et pour le tick
    _seed(tmp_path, "ma_chaine", "00", now - timedelta(seconds=1), status="published",
          tiktok_publish_at=(now - timedelta(seconds=1)).isoformat(), published_at=now.isoformat())
    _seed(tmp_path, "ma_chaine", "01", now - timedelta(minutes=1))
    yt = FakeYouTubePublisher()

    with caplog.at_level(logging.WARNING):
        _yt_worker(config, yt).tick()

    assert yt.calls == []
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "01")
    assert entry["status"] == "scheduled" and datetime.fromisoformat(entry["slot_at"]) > now
    assert "plafond de 1 publication(s) par jour" in entry["postponed_reason"]
    assert "reporté" in caplog.text
    event = tiktok.read_events(config=config)[-1]
    assert event["level"] == "warn" and event["account"] == ACCOUNT and "plafond" in event["reason"]


def test_the_youtube_min_gap_comes_from_the_youtube_section_not_the_tiktok_one(tmp_path, monkeypatch):
    # TikTok : 1 par jour / 480 min ; YouTube : 3 par jour / 120 min par defaut -> un post d'il y a 3 h passe
    config = _youtube_env(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _seed(tmp_path, "ma_chaine", "00", _ago(hours=3), status="published",
          tiktok_publish_at=(now - timedelta(hours=3)).isoformat(), published_at=now.isoformat())
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    yt = FakeYouTubePublisher()

    _yt_worker(config, yt).tick()

    assert len(yt.calls) == 1


def test_the_worker_never_fetches_tiktok_stats_for_a_youtube_account(tmp_path, monkeypatch):
    config = _youtube_env(tmp_path, monkeypatch)
    fetcher = FakeStatsFetcher(tmp_path)

    _yt_worker(config, FakeYouTubePublisher(), fetcher=fetcher).tick()

    assert [c["account"] for c in fetcher.calls] == ["ef34ab"]  # le compte TikTok seulement


# --------------------------------------------------------------------------
# TASK-2456 (revues r-publication I1, I4, I5 et r-comptes 1) : annulation sans Worker neuf, reprises de
# demarrage hors du constructeur, prise en main atomique, ordre des parties d'une serie
# --------------------------------------------------------------------------

import subprocess  # noqa: E402

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _no_worker_built(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise AssertionError("un Worker a été construit hors de « clipper worker »")

    monkeypatch.setattr(worker.Worker, "__init__", refuse)


def test_building_a_worker_recovers_nothing_until_startup(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc) + timedelta(hours=1),
            in_progress_since="2026-10-01T10:00:00+00:00")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _write_queue(config, [_entry(VIDEO_B, URL_B, status="waiting"),
                          _entry(VIDEO_A, URL_A, status="running", pid=dead.pid)])

    w = _pub_worker(config, FakePublisher())

    assert _entries(tmp_path, NO_CHANNEL)[0]["in_progress_since"] == "2026-10-01T10:00:00+00:00"
    assert [e["status"] for e in _queue(config)] == ["waiting", "running"]

    w.startup()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and "interrompue" in entry["error"]
    assert [(e["video_id"], e["status"]) for e in _queue(config)] == [(VIDEO_A, "waiting"), (VIDEO_B, "waiting")]


def test_the_worker_loop_runs_the_startup_recovery_once_before_ticking(tmp_path, monkeypatch):
    config = _config(tmp_path, poll_interval_s=0)
    calls = []
    monkeypatch.setattr(worker.Worker, "startup", lambda self: calls.append("startup"), raising=False)
    monkeypatch.setattr(worker.Worker, "tick", lambda self: calls.append("tick"))

    class _Stop(Exception):
        pass

    def sleep(seconds):
        if calls.count("tick") >= 2:
            raise _Stop

    monkeypatch.setattr(worker.time, "sleep", sleep)
    with pytest.raises(_Stop):
        worker.Worker(config=config, spawner=FakeSpawner()).loop()

    assert calls == ["startup", "tick", "tick"]


def test_cancel_kills_the_child_by_the_pid_of_the_queue_without_building_a_worker(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc), in_progress_since="2026-10-03T10:00:00+00:00")
    child = subprocess.Popen(_SLEEPER)
    try:
        _write_queue(config, [_entry(VIDEO_A, URL_A, status="running", pid=child.pid),
                              _entry(VIDEO_B, URL_B, status="waiting")])
        _pipeline_state(VIDEO_A, config, status="running")
        _no_worker_built(monkeypatch)

        worker.cancel(VIDEO_A, config=config)

        assert child.wait(timeout=10) is not None  # l'enfant est arrete
    finally:
        if child.poll() is None:
            child.kill()
    assert [e["video_id"] for e in _queue(config)] == [VIDEO_B]
    state = pipeline.load_state(VIDEO_A, config=config)
    assert (state["status"], state["reason"]) == ("failed", "annulée par l'utilisateur")
    entry = _entries(tmp_path, NO_CHANNEL)[0]  # la publication en cours du vrai worker n'est pas touchee
    assert entry["status"] == "scheduled" and entry["in_progress_since"] == "2026-10-03T10:00:00+00:00"


def test_cancel_of_a_video_that_is_not_running_is_an_error(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)

    with pytest.raises(worker.WorkerError, match="aucune video en cours"):
        worker.cancel(VIDEO_A, config=config)


def test_the_real_worker_keeps_the_cancel_reason_when_it_sees_its_child_killed(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)
    _pipeline_state(VIDEO_A, config, status="running")
    children = []

    def spawner(cmd):
        children.append(subprocess.Popen(_SLEEPER))
        return children[-1]

    w = worker.Worker(config=config, spawner=spawner)
    try:
        w.tick()
        worker.cancel(VIDEO_A, config=config)
        children[0].wait(timeout=10)
        w.tick()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()

    assert _queue(config) == []
    state = pipeline.load_state(VIDEO_A, config=config)
    assert (state["status"], state["reason"]) == ("failed", "annulée par l'utilisateur")


def _edit_entry(tmp_path, channel, clip_id, **fields):
    path = tmp_path / "state" / "publish" / f"{channel}.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    for e in entries:
        if e["clip_id"] == clip_id:
            e.update(fields)
    path.write_text(json.dumps(entries), encoding="utf-8")


class _EditingLogin(FakeLogin):
    """Verification de connexion pendant laquelle l'utilisateur modifie la publication (ecran Publication)."""

    def __init__(self, edit):
        super().__init__()
        self.edit = edit

    def __call__(self, account, *, config=None, now=None):
        if not self.calls:
            self.edit()
        return super().__call__(account, config=config, now=now)


def test_an_account_changed_during_the_login_check_is_not_driven_with_the_old_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc), account="ef34ab")
    pub = FakePublisher()
    login = _EditingLogin(lambda: _edit_entry(tmp_path, NO_CHANNEL, "01", account=ACCOUNT))
    w = _pub_worker(config, pub, login)

    w.tick()

    assert pub.calls == []  # jamais l'ancien compte
    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "scheduled" and entry["account"] == ACCOUNT and not entry.get("in_progress_since")

    w.tick()  # au passage suivant : le nouveau compte, relu

    assert [c["account"] for c in pub.calls] == [ACCOUNT]
    assert _entries(tmp_path, NO_CHANNEL)[0]["status"] == "published"


def test_a_clip_rejected_during_the_login_check_is_never_driven(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc))
    pub = FakePublisher()
    login = _EditingLogin(lambda: _edit_entry(tmp_path, NO_CHANNEL, "01", status="rejected"))

    _pub_worker(config, pub, login).tick()

    assert pub.calls == []
    assert _entries(tmp_path, NO_CHANNEL)[0]["status"] == "rejected"


def test_options_changed_during_the_login_check_are_not_driven_with_the_old_options(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc))
    pub = FakePublisher()
    changed = {**_OPTIONS, "allow_comments": True}
    login = _EditingLogin(lambda: _edit_entry(tmp_path, NO_CHANNEL, "01", post_options=changed))
    w = _pub_worker(config, pub, login)

    w.tick()
    w.tick()

    assert [c["options"] for c in pub.calls] == [changed]


_NO_CAP = {"max_posts_per_day": 10, "min_gap_minutes": 0}


def _series_part(tmp_path, clip_id, part, slot_at, **extra):
    _seed(tmp_path, "ma_chaine", clip_id, slot_at, series_id="s1", **extra)
    _edit_entry(tmp_path, "ma_chaine", clip_id, part=part)


def test_part_two_waits_while_part_one_has_failed(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _series_part(tmp_path, "c01-p1", 1, _ago(hours=2), status="failed", error="mp4 introuvable")
    _series_part(tmp_path, "c01-p2", 2, _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "c01-p2")
    assert entry["status"] == "scheduled" and "partie 1 non publiée" in entry["waiting_reason"]


def test_part_two_waits_while_part_one_is_still_to_publish(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _series_part(tmp_path, "c01-p1", 1, datetime.now(timezone.utc) + timedelta(hours=3))
    _series_part(tmp_path, "c01-p2", 2, _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "c01-p2")
    assert "partie 1 non publiée" in entry["waiting_reason"]


def test_part_two_goes_once_part_one_is_published(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings=_NO_CAP)
    _series_part(tmp_path, "c01-p1", 1, _ago(hours=9), status="published", tiktok_state="published",
                 published_at=_ago(hours=9).isoformat(), tiktok_publish_at=_ago(hours=9).isoformat())
    _series_part(tmp_path, "c01-p2", 2, _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert len(pub.calls) == 1
    assert next(e for e in _entries(tmp_path) if e["clip_id"] == "c01-p2")["status"] == "published"


def test_part_two_waits_when_part_one_is_scheduled_on_the_service_after_it(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    later = datetime.now(timezone.utc) + timedelta(days=1)
    _series_part(tmp_path, "c01-p1", 1, later, status="published", tiktok_state="scheduled_on_tiktok",
                 published_at=_ago(minutes=5).isoformat(), tiktok_publish_at=later.isoformat())
    _series_part(tmp_path, "c01-p2", 2, _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "c01-p2")
    assert "partie 1 non publiée" in entry["waiting_reason"]


def test_part_two_goes_when_part_one_is_scheduled_on_the_service_before_it(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings=_NO_CAP)
    earlier = datetime.now(timezone.utc) + timedelta(hours=2)
    when = datetime.now(timezone.utc) + timedelta(hours=6)
    _series_part(tmp_path, "c01-p1", 1, earlier, status="published", tiktok_state="scheduled_on_tiktok",
                 published_at=_ago(minutes=5).isoformat(), tiktok_publish_at=earlier.isoformat())
    _series_part(tmp_path, "c01-p2", 2, when, publish_mode="scheduled", manual=True)
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert [(c["mode"], c["schedule_at"]) for c in pub.calls] == [("scheduled", when)]


# ---------- TASK-fc561e4dc7e9 : parts_together=False leve l'attente « partie N-1 non publiée » ----------


def test_part_two_with_parts_together_false_does_not_wait_for_part_one(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _series_part(tmp_path, "c01-p1", 1, _ago(hours=2), status="failed", error="mp4 introuvable")
    _series_part(tmp_path, "c01-p2", 2, _ago(minutes=1), parts_together=False)
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert len(pub.calls) == 1
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "c01-p2")
    assert entry["status"] == "published"


def test_part_two_with_parts_together_true_still_waits_for_part_one(tmp_path, monkeypatch):
    """ON explicite (pas seulement le champ absent) : l'attente reste."""
    config = _pub_env(tmp_path, monkeypatch)
    _series_part(tmp_path, "c01-p1", 1, _ago(hours=2), status="failed", error="mp4 introuvable")
    _series_part(tmp_path, "c01-p2", 2, _ago(minutes=1), parts_together=True)
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "c01-p2")
    assert "partie 1 non publiée" in entry["waiting_reason"]


# --------------------------------------------------------------------------
# TASK-bdd5 : vidéo interrompue (étape running sans processus)
# --------------------------------------------------------------------------

ORPHAN_ID = "Zk_wshsmq5w"  # cas réel : transcribe = running, file vide


def _orphan_state(config: Config, video_id: str = ORPHAN_ID, running: str = "transcribe") -> dict:
    """download done, ``running`` en cours, le reste pending, status running ; la file est vide."""
    state = pipeline.new_state(video_id, f"https://youtu.be/{video_id}", config.mode)
    state["status"] = "running"
    for name in pipeline.STEPS[:pipeline.STEPS.index(running)]:
        state["steps"][name].update(status="done", started_at="2026-10-04T10:00:00+00:00",
                                    finished_at="2026-10-04T10:01:00+00:00")
    state["steps"][running].update(status="running", started_at="2026-10-04T10:02:00+00:00")
    pipeline.save_state(state, config=config)
    return state


def _statuses(config: Config, video_id: str = ORPHAN_ID) -> dict[str, str]:
    return {n: s["status"] for n, s in pipeline.load_state(video_id, config=config)["steps"].items()}


def _write_busy_heartbeat(config: Config) -> None:
    path = worker.heartbeat_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat(), "busy": True}),
                    encoding="utf-8")


def test_a_running_video_without_a_running_queue_entry_is_interrupted(tmp_path):
    config = _config(tmp_path)
    state = _orphan_state(config)

    assert worker.is_interrupted(state, config) is True


def test_a_running_video_with_a_live_queue_entry_is_not_interrupted(tmp_path):
    config = _config(tmp_path)
    state = _orphan_state(config)
    _write_queue(config, [_entry(ORPHAN_ID, state["source_url"], status="running", pid=os.getpid())])

    assert worker.is_interrupted(state, config) is False


def test_a_running_entry_whose_process_is_dead_is_interrupted(tmp_path):
    config = _config(tmp_path)
    state = _orphan_state(config)
    dead = subprocess.Popen(_SLEEPER)
    dead.kill()
    dead.wait()
    _write_queue(config, [_entry(ORPHAN_ID, state["source_url"], status="running", pid=dead.pid)])

    assert worker.is_interrupted(state, config) is True


def test_a_video_the_live_worker_resumes_itself_is_not_interrupted(tmp_path):
    config = _config(tmp_path)
    state = _orphan_state(config)
    _write_busy_heartbeat(config)

    assert worker.is_interrupted(state, config) is False


def test_only_running_videos_can_be_interrupted(tmp_path):
    config = _config(tmp_path)
    state = _orphan_state(config)
    state["status"] = "failed"

    assert worker.is_interrupted(state, config) is False


def test_cancelling_an_interrupted_video_succeeds_and_keeps_the_finished_steps(tmp_path, caplog):
    config = _config(tmp_path)
    _orphan_state(config)

    with caplog.at_level("WARNING", logger="clipper.worker"):
        worker.cancel(ORPHAN_ID, config=config)

    state = pipeline.load_state(ORPHAN_ID, config=config)
    assert state["status"] == "failed" and "annulée par l'utilisateur" in state["reason"]
    assert "transcribe" in state["reason"]
    statuses = _statuses(config)
    assert statuses["download"] == "done" and statuses["transcribe"] == "pending"
    assert "running" not in statuses.values()
    assert any(ORPHAN_ID in r.getMessage() and "interrompu" in r.getMessage() for r in caplog.records)


def test_cancelling_a_video_that_really_runs_still_needs_a_process(tmp_path):
    config = _config(tmp_path)
    state = _orphan_state(config)
    _write_queue(config, [_entry(ORPHAN_ID, state["source_url"], status="waiting")])  # pas running : rien à tuer
    _write_busy_heartbeat(config)

    with pytest.raises(worker.WorkerError, match="aucune video en cours"):
        worker.cancel(ORPHAN_ID, config=config)

    assert pipeline.load_state(ORPHAN_ID, config=config)["status"] == "running"


def test_resume_requeues_the_interrupted_video_from_its_interrupted_step(tmp_path):
    config = _config(tmp_path)
    _orphan_state(config)

    entry = worker.resume(ORPHAN_ID, config=config)

    assert (entry["video_id"], entry["action"], entry["status"]) == (ORPHAN_ID, "run", "waiting")
    assert entry["url"] == f"https://youtu.be/{ORPHAN_ID}"
    assert [e["video_id"] for e in _queue(config)] == [ORPHAN_ID]
    statuses = _statuses(config)
    assert statuses["download"] == "done" and statuses["transcribe"] == "pending"  # download n'est pas refait
    assert entry["force_steps"] == []  # aucune étape terminée n'est forcée


def test_resume_after_the_review_gate_renders(tmp_path):
    config = _config(tmp_path)
    _orphan_state(config, running="reframe")

    entry = worker.resume(ORPHAN_ID, config=config)

    assert (entry["action"], entry["url"]) == ("render", ORPHAN_ID)


def test_resume_of_a_video_that_really_runs_is_refused(tmp_path):
    config = _config(tmp_path)
    state = _orphan_state(config)
    _write_queue(config, [_entry(ORPHAN_ID, state["source_url"], status="running", pid=os.getpid())])

    with pytest.raises(worker.WorkerError, match="en cours de traitement"):
        worker.resume(ORPHAN_ID, config=config)


def test_startup_marks_orphan_running_steps_interrupted_and_journals_it(tmp_path, caplog):
    config = _config(tmp_path)
    _orphan_state(config)
    _orphan_state(config, video_id=VIDEO_A)
    _write_queue(config, [_entry(VIDEO_A, URL_A, status="running", pid=os.getpid())])  # celle-ci vit vraiment
    w = worker.Worker(config=config, spawner=FakeSpawner())

    with caplog.at_level("WARNING", logger="clipper.worker"):
        w._recover_interrupted_videos()

    state = pipeline.load_state(ORPHAN_ID, config=config)
    assert state["status"] == "failed" and state["reason"] == "interrompue à l'étape transcribe"
    assert _statuses(config)["transcribe"] == "pending" and _statuses(config)["download"] == "done"
    assert pipeline.load_state(VIDEO_A, config=config)["status"] == "running"
    assert any(ORPHAN_ID in r.getMessage() and "interrompu" in r.getMessage() for r in caplog.records)


def test_startup_runs_the_interrupted_video_recovery(tmp_path):
    config = _config(tmp_path)
    _orphan_state(config)

    worker.Worker(config=config, spawner=FakeSpawner()).startup()

    assert pipeline.load_state(ORPHAN_ID, config=config)["status"] == "failed"


# --------------------------------------------------------------------------
# TASK-7dc5 : miniature des VOD non YouTube des l'ajout a la file
# --------------------------------------------------------------------------

TWITCH_URL_T = "https://www.twitch.tv/videos/2888230655"
TWITCH_ID_T = "v2888230655"
TWITCH_THUMB_T = "https://static-cdn.example.invalid/previews/v2888230655.jpg"


class _FakeYDL:
    """yt-dlp simule : jamais de reseau ; enregistre les options et les appels."""

    calls: list[tuple[dict, str, bool]] = []
    error: Exception | None = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        _FakeYDL.calls.append((self.opts, url, download))
        if _FakeYDL.error:
            raise _FakeYDL.error
        return {"id": TWITCH_ID_T, "thumbnail": TWITCH_THUMB_T}


@pytest.fixture
def fake_ydl(monkeypatch):
    import yt_dlp

    _FakeYDL.calls, _FakeYDL.error = [], None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    return _FakeYDL


def _join_thumbnail_threads() -> None:
    import threading

    for t in threading.enumerate():
        if t.name == "thumbnail-fetch":
            t.join(timeout=5)


def test_enqueue_twitch_writes_thumbnail_json_without_downloading(tmp_path, fake_ydl):
    config = _config(tmp_path)

    worker.enqueue(TWITCH_URL_T, None, "run", config=config)
    _join_thumbnail_threads()

    path = tmp_path / "workspace" / TWITCH_ID_T / "thumbnail.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"url": TWITCH_THUMB_T}
    opts, url, download = fake_ydl.calls[0]
    assert url == TWITCH_URL_T and download is False and opts["skip_download"] is True


def test_enqueue_twitch_survives_ytdlp_error_and_logs_it(tmp_path, fake_ydl, caplog):
    config = _config(tmp_path)
    fake_ydl.error = RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="clipper.worker"):
        entry = worker.enqueue(TWITCH_URL_T, None, "run", config=config)
        _join_thumbnail_threads()

    assert [e["video_id"] for e in _queue(config)] == [entry["video_id"]] == [TWITCH_ID_T]
    assert not (tmp_path / "workspace" / TWITCH_ID_T / "thumbnail.json").exists()
    assert "miniature indisponible" in caplog.text and "boom" in caplog.text


def test_enqueue_youtube_never_calls_ytdlp(tmp_path, fake_ydl):
    config = _config(tmp_path)

    worker.enqueue(URL_A, None, "run", config=config)
    _join_thumbnail_threads()

    assert fake_ydl.calls == []


# --------------------------------------------------------------------------
# TASK-9d24 : le style de la file est garde dans pipeline.json
# --------------------------------------------------------------------------


def test_tick_writes_queue_channel_into_new_pipeline_json(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, "twitch", "run", config=config)

    worker.Worker(config=config, spawner=FakeSpawner()).tick()

    assert pipeline.load_state(VIDEO_A, config=config)["channel"] == "twitch"


def test_tick_sets_channel_on_existing_state_without_channel(tmp_path):
    config = _config(tmp_path)
    _pipeline_state(VIDEO_A, config, status="failed")
    worker.enqueue(URL_A, "twitch", "run", config=config)

    worker.Worker(config=config, spawner=FakeSpawner()).tick()

    assert pipeline.load_state(VIDEO_A, config=config)["channel"] == "twitch"


def test_tick_never_overwrites_an_assigned_channel(tmp_path):
    config = _config(tmp_path)
    state = pipeline.new_state(VIDEO_A, URL_A, config.mode, channel="autre")
    pipeline.save_state(state, config=config)
    worker.enqueue(URL_A, "twitch", "run", config=config)

    worker.Worker(config=config, spawner=FakeSpawner()).tick()

    assert pipeline.load_state(VIDEO_A, config=config)["channel"] == "autre"


def test_tick_without_channel_keeps_state_channel_null(tmp_path):
    config = _config(tmp_path)
    _pipeline_state(VIDEO_A, config, status="failed")
    worker.enqueue(URL_A, None, "run", config=config)

    worker.Worker(config=config, spawner=FakeSpawner()).tick()

    assert pipeline.load_state(VIDEO_A, config=config)["channel"] is None


def test_retry_after_launch_keeps_the_style(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, "twitch", "run", config=config)
    spawner = FakeSpawner()
    w = worker.Worker(config=config, spawner=spawner)
    w.tick()
    state = pipeline.load_state(VIDEO_A, config=config)
    state.update(status="failed")
    state["steps"]["download"]["status"] = "done"
    pipeline.save_state(state, config=config)
    spawner.process.finish(1)
    w.tick()

    entry = worker.enqueue(URL_A, pipeline.load_state(VIDEO_A, config=config).get("channel"), "run", config=config)

    assert entry["channel"] == "twitch"


# --------------------------------------------------------------------------
# TASK-7f582251f6c5 : clip refuse par TikTok a la verification de contenu
# --------------------------------------------------------------------------

_REFUSED = "vérification de contenu : problème signalé par TikTok (contenu non conforme)"


def test_a_content_check_refusal_marks_the_entry_refused_keeps_the_account_ready_and_the_next_post_goes(
    tmp_path, monkeypatch, caplog,
):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"max_posts_per_day": 5, "min_gap_minutes": 0})
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=2))
    _seed(tmp_path, "ma_chaine", "02", _ago(minutes=1))
    capture = tmp_path / "state" / "browser" / ACCOUNT / "captures" / "x-refused.png"
    pub = FakePublisher(error=tiktok.TikTokStop("content_check_refused", _REFUSED, capture))
    w = _pub_worker(config, pub)

    with caplog.at_level(logging.WARNING):
        w.tick()

    first, second = _entries(tmp_path)
    assert first["status"] == "refused_by_platform" and first["error"] == _REFUSED
    assert first["capture"] == str(capture) and first["halted"] is False
    assert second["status"] == "scheduled"
    assert _REFUSED in caplog.text  # journalisé
    stored = _account_state(tmp_path, ACCOUNT)
    assert stored["ready_to_publish"] is True  # aucun arrêt de publication pour ce cas
    event = tiktok.read_events(config=config)[-1]
    assert (event["account"], event["clip_id"], event["capture"]) == (ACCOUNT, "01", str(capture))

    pub.error = None
    w.tick()  # la publication suivante du compte part normalement

    assert len(pub.calls) == 2 and _entries(tmp_path)[1]["status"] == "published"
    assert _entries(tmp_path)[0]["status"] == "refused_by_platform"  # jamais republié tout seul


def test_a_content_check_timeout_is_an_explicit_clip_failure_not_an_account_halt(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    stop = tiktok.TikTokStop("content_check", "vérification de contenu non terminée après 900 s", None)

    _pub_worker(config, FakePublisher(error=stop)).tick()

    entry = _entries(tmp_path)[0]
    assert entry["status"] == "failed" and entry["halted"] is False
    assert "vérification de contenu non terminée après 900 s" in entry["error"]
    assert _account_state(tmp_path, ACCOUNT)["ready_to_publish"] is True


def test_the_next_part_of_a_series_goes_when_the_previous_part_was_refused_by_tiktok(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings=_NO_CAP)
    _series_part(tmp_path, "c01-p1", 1, _ago(hours=2), status="refused_by_platform", error=_REFUSED)
    _series_part(tmp_path, "c01-p2", 2, _ago(minutes=1))
    pub = FakePublisher()

    with caplog.at_level(logging.INFO):
        _pub_worker(config, pub).tick()

    assert len(pub.calls) == 1
    assert next(e for e in _entries(tmp_path) if e["clip_id"] == "c01-p2")["status"] == "published"
    assert "partie 1 refusée par TikTok, série poursuivie" in caplog.text


def test_a_series_skips_every_refused_part_and_still_waits_for_an_unpublished_earlier_one(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings=_NO_CAP)
    _series_part(tmp_path, "c01-p1", 1, _ago(hours=3), status="failed", error="mp4 introuvable")
    _series_part(tmp_path, "c01-p2", 2, _ago(hours=2), status="refused_by_platform", error=_REFUSED)
    _series_part(tmp_path, "c01-p3", 3, _ago(minutes=1))
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []  # la partie 1 échouée (pas refusée) retient toujours la suite
    entry = next(e for e in _entries(tmp_path) if e["clip_id"] == "c01-p3")
    assert "partie 1 non publiée" in entry["waiting_reason"]


def _geo(country):
    from clipper import network
    network.reset()
    if country is None:
        def down(url):
            raise OSError("hors ligne")
        network.use_fetcher(down)
    else:
        network.use_fetcher(lambda url: {"ip": "5.6.7.8", "city": "X", "country": country, "org": "AS1"})


def test_unknown_country_makes_the_entry_wait_without_unticking_the_account_nor_opening_anything(tmp_path, monkeypatch):
    """I2 (revue nuit) : service de géolocalisation injoignable = attente + réessai, compte toujours prêt."""
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher()
    _geo(None)

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "scheduled" and not entry.get("in_progress_since")
    assert "pays de l'IP inconnu" in entry["waiting_reason"]
    assert _account_state(tmp_path, ACCOUNT)["ready_to_publish"] is True

    _geo("FR")  # le service revient : réessai au tour suivant
    _pub_worker(config, pub).tick()
    assert len(pub.calls) == 1 and _entries(tmp_path)[0]["status"] == "published"


def test_other_country_halts_the_entry_and_unticks_the_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher()
    _geo("GB")

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "failed" and entry["halted"] is True and "IP en Royaume-Uni" in entry["error"]
    assert _account_state(tmp_path, ACCOUNT)["ready_to_publish"] is False


def test_a_country_that_turns_unknown_inside_the_publisher_fails_without_halting(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub = FakePublisher(error=browser.BrowserUnavailable("pays de l'IP inconnu (hors ligne)"))

    _pub_worker(config, pub).tick()

    entry = _entries(tmp_path)[0]
    assert entry["status"] == "failed" and entry["halted"] is False
    assert _account_state(tmp_path, ACCOUNT)["ready_to_publish"] is True


# --------------------------------------------------------------------------
# Veille (TASK-3225, SPEC-bdd9 R7) : hook de la boucle, select_best apres un enfant
# --------------------------------------------------------------------------


def _veille_queue_entry() -> dict:
    return {"id": "e1", "video_id": VIDEO_A, "url": URL_A, "channel": None, "action": "run",
            "force_steps": [], "enqueued_at": "2026-01-01T00:00:00+00:00", "status": "waiting", "pid": None}


def _join_veille(w) -> None:
    thread = w._veille_thread
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()


def _blocking_run_if_due(monkeypatch):
    """run_if_due simule : signale son depart puis bloque jusqu'a ``release``."""
    from clipper import veille

    started, release, calls = threading.Event(), threading.Event(), []

    def run(now, cfg, collectors):
        calls.append(collectors)
        started.set()
        release.wait(timeout=10)

    monkeypatch.setattr(veille, "run_if_due", run)
    return started, release, calls


def test_tick_returns_while_the_veille_runs_in_a_daemon_thread(tmp_path, monkeypatch):
    started, release, _calls = _blocking_run_if_due(monkeypatch)
    _write_queue(_config(tmp_path), [_veille_queue_entry()])
    spawner = FakeSpawner(FakeProcess())
    w = worker.Worker(config=_config(tmp_path), spawner=spawner)
    try:
        w.tick()  # reviendrait jamais avant release si le releve etait synchrone
        assert started.wait(timeout=5)
        thread = w._veille_thread
        assert thread.is_alive() and thread.daemon and thread.name == "veille"
        assert spawner.calls, "l'enfant doit etre lance pendant le releve"
    finally:
        release.set()
        _join_veille(w)


def test_only_one_veille_thread_runs_at_a_time(tmp_path, monkeypatch):
    started, release, calls = _blocking_run_if_due(monkeypatch)
    w = worker.Worker(config=_config(tmp_path), spawner=FakeSpawner())
    try:
        w.tick()
        assert started.wait(timeout=5)
        first = w._veille_thread
        w.tick()
        w.tick()
        assert w._veille_thread is first and len(calls) == 1
    finally:
        release.set()
        _join_veille(w)


def test_an_unexpected_veille_error_is_logged_with_its_type_and_a_new_run_can_start(
        tmp_path, monkeypatch, caplog):
    from clipper import veille

    calls = []

    def run(now, cfg, collectors):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("panne inattendue")

    monkeypatch.setattr(veille, "run_if_due", run)
    w = worker.Worker(config=_config(tmp_path), spawner=FakeSpawner())
    with caplog.at_level("ERROR"):
        w.tick()
        _join_veille(w)
        w.tick()  # rejoint le fil mort, repart
        _join_veille(w)
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in caplog.records)
    assert len(calls) == 2


def test_tick_calls_veille_run_if_due_with_the_injected_collectors(tmp_path, monkeypatch):
    from clipper import veille

    config = _config(tmp_path)
    calls = []
    monkeypatch.setattr(veille, "run_if_due", lambda now, cfg, collectors: calls.append((now, cfg, collectors)))
    collectors = {"twitch": object()}
    w = worker.Worker(config=config, spawner=FakeSpawner(), veille_collectors=collectors)
    w.tick()
    _join_veille(w)
    assert len(calls) == 1 and calls[0][1] is config and calls[0][2] is collectors


def test_tick_logs_a_veille_error_once_and_keeps_going(tmp_path, monkeypatch, caplog):
    from clipper import veille

    config = _config(tmp_path)

    def boom(now, cfg, collectors):
        raise veille.VeilleError("réglage invalide")

    monkeypatch.setattr(veille, "run_if_due", boom)
    w = worker.Worker(config=config, spawner=FakeSpawner())
    with caplog.at_level("ERROR"):
        w.tick()
        _join_veille(w)
        w.tick()
        _join_veille(w)
    assert len([r for r in caplog.records if "réglage invalide" in r.getMessage()]) == 1


def test_finishing_a_child_process_calls_veille_select_best(tmp_path, monkeypatch):
    from clipper import veille

    config = _config(tmp_path)
    monkeypatch.setattr(veille, "run_if_due", lambda *a, **k: None)
    selected = []
    monkeypatch.setattr(veille, "select_best", lambda now, cfg: selected.append(cfg))
    _write_queue(config, [_veille_queue_entry()])
    process = FakeProcess()
    w = worker.Worker(config=config, spawner=FakeSpawner(process))
    w.tick()  # lance l'enfant
    assert selected == []
    process.finish(0)
    w.tick()
    assert selected == [config]


def test_a_select_best_error_is_logged_and_does_not_stop_the_worker(tmp_path, monkeypatch, caplog):
    from clipper import veille

    config = _config(tmp_path)
    monkeypatch.setattr(veille, "run_if_due", lambda *a, **k: None)

    def boom(now, cfg):
        raise veille.VeilleError("sélection impossible")

    monkeypatch.setattr(veille, "select_best", boom)
    _write_queue(config, [_veille_queue_entry()])
    process = FakeProcess()
    w = worker.Worker(config=config, spawner=FakeSpawner(process))
    w.tick()
    process.finish(0)
    with caplog.at_level("ERROR"):
        w.tick()
    assert any("sélection impossible" in r.getMessage() for r in caplog.records)
    assert w._process is None


# ---- apprentissage : rattachement puis versement (TASK-32ae, TASK-7136)

def _learning_config(tmp_path):
    config = _config(tmp_path)
    config._sections.update({
        "learning": {"state_dir": str(tmp_path / "learning")},
        "tiktok": {"stats_dir": str(tmp_path / "stats")},
        "outcomes": {"journal_path": str(tmp_path / "outcomes.jsonl")},
        "jury_calibration": {"weights_path": str(tmp_path / "weights.json")},
        "accounts": {"state_file": str(tmp_path / "accounts.json")},
        "publish": {"state_dir": str(tmp_path / "pub")},
    })
    return config


def _sync_json(config) -> dict:
    return json.loads((Path(config.section("learning")["state_dir"]) / "sync.json").read_text(encoding="utf-8"))


def test_tick_calls_run_if_due_every_turn_before_the_veille(tmp_path):
    calls = []

    def runner(now, *, config):
        calls.append("learning")
        return {"linked": [{"video_id": VIDEO_A, "clip_id": "c1", "post_id": "1"}], "synced": False}

    config = _learning_config(tmp_path)
    w = worker.Worker(config=config, spawner=FakeSpawner(), learning_runner=runner)
    w._veille_due = lambda: calls.append("veille")
    w.tick()
    w.tick()
    assert calls == ["learning", "veille", "learning", "veille"]


def test_the_default_runner_is_learning_run_if_due(tmp_path):
    from clipper import learning

    assert worker.Worker(config=_learning_config(tmp_path), spawner=FakeSpawner()).learning_runner is learning.run_if_due


def test_run_if_due_links_then_syncs_only_when_a_snapshot_is_newer_than_the_last_sync(tmp_path, monkeypatch):
    from clipper import learning, tiktok

    config = _learning_config(tmp_path)
    order = []
    real_link, real_sync = learning.link_if_due, learning.sync
    monkeypatch.setattr(learning, "link_if_due", lambda *a, **k: order.append("link") or real_link(*a, **k))
    monkeypatch.setattr(learning, "sync", lambda *a, **k: order.append("sync") or real_sync(*a, **k))
    tiktok.append_snapshot("acc", tiktok.get_settings(config), {
        "account": "acc", "fetched_at": "2026-10-01T10:00:00+00:00", "source": "tiktok_studio", "origin": "full",
        "overview": {}, "posts": []})
    w = worker.Worker(config=config, spawner=FakeSpawner())

    w.tick()
    w.tick()  # rien de plus récent que last_sync : le rattachement repasse, pas le versement
    assert order == ["link", "sync", "link"]
    tiktok.append_snapshot("acc", tiktok.get_settings(config), {
        "account": "acc", "fetched_at": "2999-01-01T00:00:00+00:00", "source": "tiktok_studio", "origin": "full",
        "overview": {}, "posts": []})
    w.tick()
    assert order == ["link", "sync", "link", "link", "sync"]


@pytest.mark.parametrize("error_name", ["LearningError", "CalibrationError"])
def test_a_learning_or_calibration_error_is_written_and_logged_once_without_stopping_the_worker(tmp_path, caplog, error_name):
    import logging

    from clipper import jury_calibration, learning

    error = {"LearningError": learning.LearningError, "CalibrationError": jury_calibration.CalibrationError}[error_name]

    def runner(now, *, config):
        exc = error("sidecar illisible (casse.json)")
        exc.where = "sync"
        raise exc

    config = _learning_config(tmp_path)
    w = worker.Worker(config=config, spawner=FakeSpawner(), learning_runner=runner)
    with caplog.at_level(logging.ERROR):
        w.tick()
        w.tick()
    assert caplog.text.count("casse.json") == 1
    last_error = _sync_json(config)["last_error"]
    assert set(last_error) == {"at", "where", "message"}
    assert last_error["where"] == "sync" and "casse.json" in last_error["message"]


def test_a_successful_sync_clears_last_error(tmp_path):
    from clipper import learning, tiktok

    config = _learning_config(tmp_path)
    learning.record_error(config, "sync", learning.LearningError("avant"))
    tiktok.append_snapshot("acc", tiktok.get_settings(config), {
        "account": "acc", "fetched_at": "2026-10-01T10:00:00+00:00", "source": "tiktok_studio", "origin": "full",
        "overview": {}, "posts": []})

    worker.Worker(config=config, spawner=FakeSpawner()).tick()

    assert _sync_json(config)["last_error"] is None


def test_learning_disabled_does_nothing(tmp_path):
    calls = []
    config = _config(tmp_path)
    config._sections["learning"] = {"enabled": False}
    worker.Worker(config=config, spawner=FakeSpawner(),
                  learning_runner=lambda now, *, config: calls.append(1) or {}).tick()
    assert calls == []


# ---- répartition du lendemain (TASK-0350d, SPEC-78dc R7) : appelée à chaque tour, après l'apprentissage

def test_tick_calls_repartition_every_turn_right_after_learning(tmp_path):
    calls = []
    w = worker.Worker(
        config=_learning_config(tmp_path), spawner=FakeSpawner(),
        learning_runner=lambda now, *, config: calls.append("learning") or {},
        repartition_runner=lambda now, *, config: calls.append("repartition"),
    )
    w._veille_due = lambda: None
    w.tick()
    w.tick()
    assert calls == ["learning", "repartition", "learning", "repartition"]


def test_the_default_repartition_runner_is_repartition_run_if_due(tmp_path):
    from clipper import repartition

    assert worker.Worker(config=_learning_config(tmp_path), spawner=FakeSpawner()).repartition_runner is repartition.run_if_due


@pytest.mark.parametrize("error_name", [
    "RepartitionError", "PublishError", "AccountsError", "TikTokError", "ConfigError", "OSError", "ValueError",
])
def test_a_repartition_error_never_leaves_tick_and_is_logged_once(tmp_path, caplog, error_name):
    import builtins
    import logging

    from clipper import accounts, publish, repartition, tiktok
    from clipper.config import ConfigError

    error_class = {
        "RepartitionError": repartition.RepartitionError, "PublishError": publish.PublishError,
        "AccountsError": accounts.AccountsError, "TikTokError": tiktok.TikTokError, "ConfigError": ConfigError,
        "OSError": builtins.OSError, "ValueError": builtins.ValueError,
    }[error_name]

    def runner(now, *, config):
        raise error_class("plan illisible (casse-plan.json)")

    w = worker.Worker(config=_learning_config(tmp_path), spawner=FakeSpawner(), repartition_runner=runner)
    w._veille_due = lambda: None
    with caplog.at_level(logging.ERROR):
        w.tick()  # ne doit jamais lever
        w.tick()
    assert caplog.text.count("casse-plan.json") == 1


def test_repartition_disabled_calls_nothing(tmp_path):
    calls = []
    config = _learning_config(tmp_path)
    config._sections["repartition"] = {"enabled": False}
    w = worker.Worker(config=config, spawner=FakeSpawner(), repartition_runner=lambda now, *, config: calls.append(1))
    w._veille_due = lambda: None
    w.tick()
    assert calls == []


def test_tick_ne_lit_ni_n_ecrit_jamais_le_state_du_dossier_courant(tmp_path, monkeypatch, caplog):
    # Régression TASK-0f21 : les chemins d'état par défaut sont relatifs au cwd ; un state/ piégé dans le cwd
    # (JSON cassé) ne doit être ni lu, ni modifié, ni journalisé en erreur par un tick de test standard.
    import logging

    trap = tmp_path / "depot"
    trapped = {
        trap / "state" / "learning" / "links.json": "{casse",
        trap / "state" / "stats" / "tiktok" / "acc" / "2026-10-01.json": "{casse",
        trap / "state" / "veille" / "bilan.json": "{casse",
    }
    for path, text in trapped.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    before = {path: path.read_bytes() for path in trapped}
    monkeypatch.chdir(trap)

    with caplog.at_level(logging.ERROR):
        worker.Worker(config=_config(tmp_path / "suite"), spawner=FakeSpawner()).tick()

    assert {path: path.read_bytes() for path in trapped} == before
    assert not (trap / "state" / "learning" / "sync.json").exists()
    assert "apprentissage impossible" not in caplog.text


# ---- pause manuelle d'un compte (SPEC-f348 R7.3, R7.5)


def _pause(config, account=ACCOUNT):
    accounts_mod.pause(config, account)


def test_an_entry_of_a_paused_account_is_not_attempted_and_waits_with_a_visible_reason(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _pause(config)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub, login = FakePublisher(), FakeLogin()
    worker_ = _pub_worker(config, pub, login)

    with caplog.at_level(logging.WARNING):
        worker_.tick()
        worker_.tick()

    assert pub.calls == [] and login.calls == []
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "scheduled" and entry["error"] is None
    assert "en pause (manuel)" in entry["waiting_reason"] and "compte A" in entry["waiting_reason"]
    assert "choisis un autre compte" in entry["waiting_reason"]
    assert caplog.text.count("en pause (manuel)") == 1  # un seul journal par raison
    events = [e for e in tiktok.read_events(config=config) if e.get("clip_id") == "01"]
    assert len(events) == 1 and "en pause (manuel)" in events[0]["reason"]


def test_a_paused_account_never_falls_back_to_another_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)  # la chaine pointe ab12cd, l'entree vise l'autre compte
    _pause(config, OTHER)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1), account=OTHER)
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    assert "en pause (manuel)" in _entries(tmp_path)[0]["waiting_reason"]


def test_after_resume_with_a_verified_connection_the_entry_is_attempted_on_the_next_tick(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _pause(config)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    pub, login = FakePublisher(), FakeLogin()
    worker_ = _pub_worker(config, pub, login)
    worker_.tick()
    assert pub.calls == []

    accounts_mod.record_login(config, ACCOUNT, _CONNECTED)  # l'ouverture de l'ecran Comptes revérifie la connexion
    accounts_mod.resume(config, ACCOUNT)
    worker_.tick()

    assert [c["account"] for c in pub.calls] == [ACCOUNT]
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "published" and entry["waiting_reason"] is None


def test_the_periodic_stats_fetch_still_covers_a_paused_connected_account(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"stats_interval_h": 2})
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    _pause(config)
    fetcher = FakeStatsFetcher(tmp_path)

    _stats_worker(config, fetcher).tick()

    assert [c["account"] for c in fetcher.calls] == [ACCOUNT]


def test_the_periodic_stats_fetch_skips_a_paused_account_with_an_expired_connection(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch, tiktok_settings={"stats_interval_h": 2})
    _set_account_state(tmp_path, "ef34ab", ready_to_publish=False)
    _pause(config)
    _set_account_state(tmp_path, ACCOUNT, login={"state": "expired", "checked_at": "2026-10-01T10:00:00+00:00",
                                                 "expires_at": None})
    fetcher = FakeStatsFetcher(tmp_path)

    _stats_worker(config, fetcher).tick()

    assert fetcher.calls == []


# ---- TASK-0c97 : un post parti est toujours trace ; pause revérifiée avant le post


def _lock_sidecar(monkeypatch, clip_id: str):
    """``os.replace`` vers le sidecar du clip leve toujours PermissionError (lecteur qui garde le fichier)."""
    real = os.replace

    def replace(src, dst, *args, **kwargs):
        if str(dst).endswith(f"{clip_id}.json"):
            raise PermissionError(13, "Acces refuse (simule)")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def test_a_sidecar_stuck_after_a_successful_post_leaves_the_entry_published_with_its_url(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    _lock_sidecar(monkeypatch, "01")
    pub = FakePublisher()
    worker_ = _pub_worker(config, pub)

    with caplog.at_level(logging.ERROR):
        worker_.tick()
        worker_.tick()

    entry = _entries(tmp_path)[0]
    assert entry["status"] == "published" and entry["post_url"] == LINK
    assert not entry.get("in_progress_since") and entry["error"] is None
    assert any(r.levelname == "ERROR" and LINK in r.getMessage() for r in caplog.records)
    assert len(pub.calls) == 1  # aucun second post
    with pytest.raises(publish.PublishError):  # « Réessayer » impossible : le post est parti
        publish.retry("aaaaaaaaaaa", "01", "ma_chaine", state_dir=tmp_path / "state" / "publish")
    assert len(pub.calls) == 1


def test_an_account_paused_between_takeover_and_post_gets_no_post(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _seed(tmp_path, "ma_chaine", "01", _ago(minutes=1))
    real = publish.mark_in_progress

    def take_then_pause(*args, **kwargs):
        result = real(*args, **kwargs)
        _pause(config)
        return result

    monkeypatch.setattr(publish, "mark_in_progress", take_then_pause)
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []
    entry = _entries(tmp_path)[0]
    assert entry["status"] == "scheduled" and not entry.get("in_progress_since")
    assert "en pause (manuel)" in entry["waiting_reason"]


# --------------------------------------------------------------------------
# TASK-3c1c : prechargement du download de la VOD suivante pendant le traitement CPU
# --------------------------------------------------------------------------

VIDEO_C = "CCCCCCCCCCC"
URL_C = f"https://youtu.be/{VIDEO_C}"


class SeqSpawner:
    """Un faux processus distinct par lancement (pid 7001, 7002...) : l'enfant, puis le prechargement."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, cmd: list[str]):
        self.calls.append(cmd)
        process = FakeProcess(pid=7000 + len(self.calls))
        self.processes.append(process)
        return process


def _mark_download_done(config: Config, video_id: str) -> None:
    state = pipeline.new_state(video_id, f"https://youtu.be/{video_id}", config.mode)
    state["steps"]["download"]["status"] = "done"
    pipeline.save_state(state, config=config)


def _prefetch_worker(tmp_path, *, waiting=(VIDEO_B, VIDEO_C), download_done=True, **overrides):
    """File A (lancee au 1er tick) puis les entrees ``waiting`` ; A a (ou non) son download fait."""
    config = _config(tmp_path, **{"prefetch_min_free_gb": 0, **overrides})
    _write_queue(config, [_entry(v, f"https://youtu.be/{v}") for v in (VIDEO_A, *waiting)])
    spawner = SeqSpawner()
    w = worker.Worker(config=config, spawner=spawner)
    w.tick()  # lance A
    if download_done:
        _mark_download_done(config, VIDEO_A)
    return config, w, spawner


def _by_video(config: Config) -> dict[str, dict]:
    return {e["video_id"]: e for e in _queue(config)}


def test_worker_defaults_declare_the_prefetch_settings():
    assert worker.CONFIG_DEFAULTS["prefetch_download"] is True
    assert worker.CONFIG_DEFAULTS["prefetch_min_free_gb"] == 60


def test_no_prefetch_while_the_current_video_has_not_finished_its_download(tmp_path):
    config, w, spawner = _prefetch_worker(tmp_path, download_done=False)

    w.tick()
    w.tick()

    assert len(spawner.calls) == 1  # seulement A
    assert "prefetch" not in _by_video(config)[VIDEO_B]


def test_prefetch_starts_the_download_alone_of_the_first_waiting_entry_once_the_current_download_is_done(tmp_path):
    config, w, spawner = _prefetch_worker(tmp_path)

    w.tick()

    assert spawner.calls[1] == [sys.executable, "-m", "clipper", "download", "--", URL_B]
    entries = _by_video(config)
    assert entries[VIDEO_B]["prefetch"] == "running"
    assert entries[VIDEO_B]["prefetch_pid"] == 7002
    assert entries[VIDEO_B]["status"] == "waiting"  # toujours en file, pas « running »
    assert entries[VIDEO_A]["status"] == "running"
    assert "prefetch" not in entries[VIDEO_C]


def test_prefetch_command_carries_the_channel_preset_before_the_subcommand(tmp_path):
    config = _config(tmp_path, prefetch_min_free_gb=0)
    queued_b = {**_entry(VIDEO_B, URL_B), "channel": "ma_chaine"}
    _write_queue(config, [_entry(VIDEO_A, URL_A), queued_b])
    spawner = SeqSpawner()
    w = worker.Worker(config=config, spawner=spawner)
    w.tick()
    _mark_download_done(config, VIDEO_A)

    w.tick()

    assert spawner.calls[1] == [sys.executable, "-m", "clipper", "--config", "presets/ma_chaine.toml", "download", "--", URL_B]


def test_prefetch_command_is_accepted_by_the_real_parser(tmp_path):
    from clipper.__main__ import build_parser

    cmd = worker._build_prefetch_command({**_entry(VIDEO_B, URL_B), "channel": "ma_chaine"})
    args = build_parser().parse_args(cmd[3:])

    assert (args.config, args.command, args.url) == ("presets/ma_chaine.toml", "download", URL_B)


def test_only_one_prefetch_at_a_time(tmp_path):
    config, w, spawner = _prefetch_worker(tmp_path)

    for _ in range(4):
        w.tick()

    assert len(spawner.calls) == 2  # A, puis le seul prechargement de B
    assert "prefetch" not in _by_video(config)[VIDEO_C]


def test_no_second_prefetch_after_the_first_one_finished(tmp_path):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()
    spawner.processes[1].finish(0)

    w.tick()
    w.tick()

    assert len(spawner.calls) == 2  # C n'est pas precharge : seul le premier waiting l'est
    entries = _by_video(config)
    assert entries[VIDEO_B]["prefetch"] == "done"
    assert entries[VIDEO_B]["prefetch_pid"] is None
    assert "prefetch" not in entries[VIDEO_C]


def test_prefetched_entry_is_launched_as_a_normal_run_and_loses_its_prefetch_fields(tmp_path):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()
    spawner.processes[1].finish(0)
    w.tick()
    spawner.processes[0].finish(0)  # A termine

    w.tick()

    assert spawner.calls[2] == [sys.executable, "-m", "clipper", "run", "--", URL_B]  # le run saute le download fait
    entry = _by_video(config)[VIDEO_B]
    assert entry["status"] == "running"
    assert "prefetch" not in entry and "prefetch_pid" not in entry


def test_current_video_finishing_while_its_successor_is_still_downloading_waits_for_the_download(tmp_path):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()  # prechargement de B en cours
    spawner.processes[0].finish(0)  # A termine avant la fin du download de B

    w.tick()
    w.tick()

    assert len(spawner.calls) == 2  # B n'est pas lance en `run` pendant qu'il se telecharge
    assert _by_video(config)[VIDEO_B]["status"] == "waiting"
    spawner.processes[1].finish(0)
    w.tick()
    assert spawner.calls[2] == [sys.executable, "-m", "clipper", "run", "--", URL_B]


def test_prefetch_failure_is_logged_visible_and_does_not_fail_the_current_video(tmp_path, caplog):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()
    _pipeline_state(VIDEO_B, config, status="pending")
    state = pipeline.load_state(VIDEO_B, config=config)  # ecrit par _keep_channel/le prechargement : etat de B
    state["steps"]["download"].update(status="failed", reason="RuntimeError: reseau coupe")
    state.update(status="failed", reason="download : RuntimeError: reseau coupe")
    pipeline.save_state(state, config=config)

    with caplog.at_level("ERROR"):
        spawner.processes[1].finish(1)
        w.tick()

    entries = _by_video(config)
    assert entries[VIDEO_B]["prefetch"] == "failed"
    assert entries[VIDEO_B]["status"] == "waiting"  # reste dans la file
    assert entries[VIDEO_A]["status"] == "running"  # la video en cours n'est pas touchee
    assert spawner.processes[0].poll() is None
    assert any(VIDEO_B in r.getMessage() and "pr" in r.getMessage() for r in caplog.records)
    assert pipeline.load_state(VIDEO_B, config=config)["steps"]["download"]["status"] == "failed"
    w.tick()
    assert len(spawner.calls) == 2  # pas de nouvel essai tant qu'elle n'est pas la video en cours


def test_prefetch_killed_without_writing_its_state_still_leaves_a_visible_failure(tmp_path):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()
    _pipeline_state(VIDEO_B, config, status="pending")
    state = pipeline.load_state(VIDEO_B, config=config)
    state["steps"]["download"]["status"] = "running"  # le processus est mort sans rien ecrire de plus
    pipeline.save_state(state, config=config)
    spawner.processes[1].finish(3)

    w.tick()

    after = pipeline.load_state(VIDEO_B, config=config)
    assert after["steps"]["download"]["status"] == "failed"
    assert "code 3" in after["steps"]["download"]["reason"]
    assert after["status"] == "failed"


def test_failed_prefetched_entry_retries_its_download_when_it_becomes_the_current_video(tmp_path):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()
    spawner.processes[1].finish(1)
    w.tick()
    spawner.processes[0].finish(0)

    w.tick()

    assert spawner.calls[2] == [sys.executable, "-m", "clipper", "run", "--", URL_B]
    assert "prefetch" not in _by_video(config)[VIDEO_B]


def test_prefetch_download_false_never_prefetches(tmp_path):
    config, w, spawner = _prefetch_worker(tmp_path, prefetch_download=False)

    for _ in range(3):
        w.tick()

    assert len(spawner.calls) == 1
    assert "prefetch" not in _by_video(config)[VIDEO_B]


def test_prefetch_skipped_under_the_free_disk_threshold_with_one_log_line(tmp_path, monkeypatch, caplog):
    config, w, spawner = _prefetch_worker(tmp_path, prefetch_min_free_gb=60)
    gb = 1024 ** 3
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(500 * gb, 440 * gb, 10 * gb))

    with caplog.at_level("INFO"):
        w.tick()
        w.tick()

    assert len(spawner.calls) == 1
    assert "prefetch" not in _by_video(config)[VIDEO_B]
    lines = [r.getMessage() for r in caplog.records if "pr" in r.getMessage() and VIDEO_B in r.getMessage()]
    assert len(lines) == 1 and "10" in lines[0] and "60" in lines[0]


def test_prefetch_runs_when_the_free_disk_is_above_the_threshold(tmp_path, monkeypatch):
    config, w, spawner = _prefetch_worker(tmp_path, prefetch_min_free_gb=60)
    gb = 1024 ** 3
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(500 * gb, 300 * gb, 200 * gb))

    w.tick()

    assert len(spawner.calls) == 2


def test_removing_a_prefetching_entry_terminates_its_process(tmp_path, monkeypatch):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()
    killed = []
    monkeypatch.setattr(worker, "_pid_alive", lambda pid: pid not in [p for p, _ in killed])
    monkeypatch.setattr(worker.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    worker.remove(VIDEO_B, config=config)

    assert [pid for pid, _ in killed] == [7002]
    assert VIDEO_B not in _by_video(config)
    spawner.processes[1].finish(-15)
    w.tick()  # le worker constate la mort du prechargement d'une entree disparue : rien d'autre n'est ecrit
    assert VIDEO_B not in _by_video(config)


def test_cancelling_a_prefetching_entry_terminates_its_process(tmp_path, monkeypatch):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()
    killed = []
    monkeypatch.setattr(worker, "_pid_alive", lambda pid: pid not in [p for p, _ in killed])
    monkeypatch.setattr(worker.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    worker.cancel(VIDEO_B, config=config)

    assert [pid for pid, _ in killed] == [7002]
    assert VIDEO_B not in _by_video(config)


def test_removing_an_entry_resets_the_download_step_left_running_by_the_killed_prefetch(tmp_path, monkeypatch):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()
    _pipeline_state(VIDEO_B, config, status="pending")
    state = pipeline.load_state(VIDEO_B, config=config)
    state["steps"]["download"]["status"] = "running"
    pipeline.save_state(state, config=config)
    monkeypatch.setattr(worker, "_pid_alive", lambda pid: False)

    worker.remove(VIDEO_B, config=config)

    assert pipeline.load_state(VIDEO_B, config=config)["steps"]["download"]["status"] == "pending"


def test_worker_shutdown_terminates_the_prefetch_process(tmp_path, monkeypatch):
    config, w, spawner = _prefetch_worker(tmp_path)
    w.tick()
    killed = []
    monkeypatch.setattr(worker, "_pid_alive", lambda pid: pid not in [p for p, _ in killed])
    monkeypatch.setattr(worker.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    w.shutdown()

    assert [pid for pid, _ in killed] == [7002]
    assert "prefetch" not in _by_video(config)[VIDEO_B]  # sera retente


def test_loop_shuts_the_prefetch_down_when_interrupted(tmp_path, monkeypatch):
    config = _config(tmp_path)
    w = worker.Worker(config=config, spawner=SeqSpawner())
    calls = []
    monkeypatch.setattr(w, "startup", lambda: None)
    monkeypatch.setattr(w, "shutdown", lambda: calls.append("shutdown"))

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(w, "tick", interrupted)
    with pytest.raises(KeyboardInterrupt):
        w.loop()

    assert calls == ["shutdown"]


def test_startup_terminates_a_prefetch_orphaned_by_a_previous_worker_and_clears_its_marker(tmp_path, monkeypatch):
    config = _config(tmp_path)
    b = {**_entry(VIDEO_B, URL_B), "prefetch": "running", "prefetch_pid": 8123}
    done = {**_entry(VIDEO_C, URL_C), "prefetch": "done", "prefetch_pid": None}
    _write_queue(config, [b, done])
    killed = []
    monkeypatch.setattr(worker, "_pid_alive", lambda pid: pid == 8123 and not killed)
    monkeypatch.setattr(worker.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    worker.Worker(config=config, spawner=SeqSpawner()).startup()

    assert [pid for pid, _ in killed] == [8123]
    entries = _by_video(config)
    assert "prefetch" not in entries[VIDEO_B] and "prefetch_pid" not in entries[VIDEO_B]
    assert entries[VIDEO_C]["prefetch"] == "done"  # un download deja fait reste un fait


def test_startup_clears_the_marker_of_a_prefetch_whose_process_is_gone(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _write_queue(config, [{**_entry(VIDEO_B, URL_B), "prefetch": "running", "prefetch_pid": 8123}])
    monkeypatch.setattr(worker, "_pid_alive", lambda pid: False)

    worker.Worker(config=config, spawner=SeqSpawner()).startup()

    assert "prefetch" not in _by_video(config)[VIDEO_B]


def test_main_download_command_runs_only_the_download_step(tmp_path, monkeypatch, capsys):
    from clipper.__main__ import main

    config = _config(tmp_path)
    monkeypatch.setattr("clipper.__main__.load_config", lambda path="config.toml": config)
    seen = {}
    monkeypatch.setattr(pipeline, "download_only", lambda url, *, config: seen.update(url=url, config=config))

    assert main(["download", URL_B]) == 0
    assert seen == {"url": URL_B, "config": config}
    assert f"{VIDEO_B} : download termine" in capsys.readouterr().out


# --------------------------------------------------------------------------
# TASK-6ef1 : le dossier des styles vient de [watch] presets_dir, jamais « presets/ » en dur
# --------------------------------------------------------------------------


def test_child_commands_use_the_configured_presets_dir(tmp_path):
    config = Config(mode="auto", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o",
                    _sections={"watch": {"presets_dir": "styles"}})
    entry = {**_cmd_entry("run", "ma_chaine"), "url": URL_A}

    run_cmd = worker._build_command(entry, config)
    prefetch_cmd = worker._build_prefetch_command(entry, config)

    assert run_cmd[3:5] == ["--config", "styles/ma_chaine.toml"]
    assert prefetch_cmd[3:5] == ["--config", "styles/ma_chaine.toml"]


# --- TASK-466e961c71bf : une programmation sans id de post n'est jamais « publiee » ---------------------------

def test_a_scheduled_flow_that_ends_without_a_post_id_is_not_recorded_as_published(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    when = datetime.now(timezone.utc) + timedelta(days=3)
    _manual(tmp_path, "01", when, mode="scheduled")
    pub = FakePublisher(scheduled_post_id=None)

    with caplog.at_level(logging.ERROR):
        _pub_worker(config, pub).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and entry["to_verify"] is True and entry["halted"] is False
    assert "à vérifier" in entry["error"] and "doublon" in entry["error"]
    assert entry.get("tiktok_state") is None and entry["in_progress_since"] is None
    assert "programmation à vérifier" in caplog.text
    sidecar = json.loads((tmp_path / "output" / "aaaaaaaaaaa" / "01.json").read_text(encoding="utf-8"))
    assert "tiktok_post" not in sidecar


def test_an_unconfirmed_schedule_is_never_rescheduled_by_itself(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc) + timedelta(days=3), mode="scheduled")
    pub = FakePublisher(scheduled_post_id=None)
    w = _pub_worker(config, pub)

    w.tick()
    w.tick()
    w.tick()

    assert len(pub.calls) == 1 and _entries(tmp_path, NO_CHANNEL)[0]["status"] == "failed"


def _scheduled_without_id(tmp_path, clip_id="01", published_at=None):
    _manual(tmp_path, clip_id, datetime.now(timezone.utc) + timedelta(days=1), mode="scheduled", status="published",
            tiktok_state="scheduled_on_tiktok", post_id=None, post_url=None,
            published_at=(published_at or _ago(hours=3)).isoformat())


def _reconcile_worker(config):
    return _pub_worker(config, FakePublisher())


def test_a_full_stats_snapshot_without_the_scheduled_post_flags_it_once(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _scheduled_without_id(tmp_path)
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        {"post_id": "7300000000000000009", "caption": "une autre legende", "posted_at": _ago(days=2).isoformat()}])
    w = _reconcile_worker(config)

    with caplog.at_level(logging.ERROR):
        w.tick()
        w.tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "published" and entry["missing_on_tiktok"] is True
    assert "absente du dernier relevé" in entry["post_note"]
    assert caplog.text.count("absente du dernier relevé") == 1


def test_a_snapshot_that_shows_the_scheduled_post_flags_nothing(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _scheduled_without_id(tmp_path)
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        {"post_id": "7300000000000000009", "caption": "legende 01 #a #b", "posted_at": _ago(hours=-20).isoformat()}])

    _reconcile_worker(config).tick()

    assert "missing_on_tiktok" not in _entries(tmp_path, NO_CHANNEL)[0]


def _to_verify(tmp_path, clip_id="01", failed_at=None):
    """Entrée « à vérifier » (TASK-466e961c71bf) : programmation partie, aucun post retrouvé, entrée failed."""
    _manual(tmp_path, clip_id, datetime.now(timezone.utc) + timedelta(days=1), mode="scheduled", status="failed",
            to_verify=True, halted=False, post_id=None, post_url=None, error="programmation à vérifier : ...",
            failed_at=(failed_at or _ago(hours=3)).isoformat())


_SHOWN = {"post_id": "7300000000000000009", "post_url": "https://www.tiktok.com/@a/video/7300000000000000009",
          "caption": "legende 01 #a #b", "posted_at": None}


def test_a_to_verify_entry_found_in_a_later_full_snapshot_becomes_scheduled_on_tiktok(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify(tmp_path)
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[_SHOWN])
    w = _reconcile_worker(config)

    with caplog.at_level(logging.INFO):
        w.tick()
        w.tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "published" and entry["tiktok_state"] == "scheduled_on_tiktok"
    assert entry["post_id"] == "7300000000000000009" and entry["post_url"] == _SHOWN["post_url"]
    assert "to_verify" not in entry or entry["to_verify"] is False
    assert entry["error"] is None and entry["halted"] is False
    assert caplog.text.count("retrouvée sur TikTok") == 1  # journal info une seule fois
    sidecar = json.loads((tmp_path / "output" / "aaaaaaaaaaa" / "01.json").read_text(encoding="utf-8"))
    assert sidecar["tiktok_post"]["id"] == "7300000000000000009"


def test_a_to_verify_entry_is_never_rescheduled_when_reconciled(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify(tmp_path)
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[_SHOWN])
    pub = FakePublisher()

    _pub_worker(config, pub).tick()

    assert pub.calls == []


def test_a_to_verify_entry_absent_from_the_later_snapshot_stays_to_verify(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify(tmp_path)
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[{**_SHOWN, "caption": "une autre legende"}])

    _reconcile_worker(config).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and entry["to_verify"] is True and not entry.get("post_id")


def test_a_to_verify_entry_is_unchanged_by_an_older_or_partial_snapshot(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify(tmp_path, failed_at=_ago(hours=3))
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=5), origin="full", posts=[_SHOWN])  # avant la programmation
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), origin="opportunistic", posts=[_SHOWN])  # releve partiel

    _reconcile_worker(config).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and entry["to_verify"] is True


def test_a_failed_entry_that_is_not_to_verify_is_never_reconciled(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify(tmp_path)
    path = tmp_path / "state" / "publish" / f"{NO_CHANNEL}.json"
    path.write_text(json.dumps([{**_entries(tmp_path, NO_CHANNEL)[0], "to_verify": False}]), encoding="utf-8")
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[_SHOWN])

    _reconcile_worker(config).tick()

    assert _entries(tmp_path, NO_CHANNEL)[0]["status"] == "failed"


def test_an_opportunistic_or_older_snapshot_flags_nothing(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _scheduled_without_id(tmp_path, published_at=_ago(hours=3))
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), origin="opportunistic")  # une partie de la liste seulement
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=5), origin="full")  # avant la programmation

    _reconcile_worker(config).tick()

    assert "missing_on_tiktok" not in _entries(tmp_path, NO_CHANNEL)[0]


# -- TASK-05b430f5416e : rapprochement par legende ET heure (deux posts a legende commune, 12:00 et 19:30)

_PARIS = ZoneInfo("Europe/Paris")
_NOON = datetime(2026, 10, 9, 12, 0, tzinfo=_PARIS)
_EVENING = datetime(2026, 10, 9, 19, 30, tzinfo=_PARIS)
_SHARED_CAPTION = "Sur Silent Hill: Townfall, XababTV s'arrête"  # legende de la sidecar (complete)
_SHARED_SHOWN = "Sur Silent Hill: Townfall, XababTV s'arr…"  # ce que TikTok Studio affiche (tronquee)


def _post(post_id, *, posted=None, caption=_SHARED_SHOWN):
    return {"post_id": post_id, "post_url": f"https://www.tiktok.com/@a/video/{post_id}", "caption": caption,
            "posted_at": posted}


def _same_caption(tmp_path, clip_ids):
    """Les sidecars des entrees partagent la meme legende (deux posts qui commencent pareil)."""
    for clip_id in clip_ids:
        path = tmp_path / "output" / "aaaaaaaaaaa" / f"{clip_id}.json"
        sidecar = json.loads(path.read_text(encoding="utf-8"))
        sidecar["caption"] = _SHARED_CAPTION
        path.write_text(json.dumps(sidecar), encoding="utf-8")


def _to_verify_at(tmp_path, clip_id, slot, failed_at=None):
    _manual(tmp_path, clip_id, slot, mode="scheduled", status="failed", to_verify=True, halted=False,
            post_id=None, post_url=None, error="programmation à vérifier : ...",
            failed_at=(failed_at or _ago(hours=3)).isoformat())


def _scheduled_at(tmp_path, clip_id, slot):
    _manual(tmp_path, clip_id, slot, mode="scheduled", status="published", tiktok_state="scheduled_on_tiktok",
            post_id=None, post_url=None, published_at=_ago(hours=3).isoformat())


def test_two_posts_with_the_same_caption_go_to_the_entry_of_their_own_slot(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify_at(tmp_path, "01", _NOON)
    _to_verify_at(tmp_path, "02", _EVENING)
    _same_caption(tmp_path, ["01", "02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000012", posted="2026-10-09T12:00:00"),
        _post("7300000000000000019", posted="2026-10-09T19:30:00")])

    _reconcile_worker(config).tick()

    by_clip = {e["clip_id"]: e for e in _entries(tmp_path, NO_CHANNEL)}
    assert by_clip["01"]["post_id"] == "7300000000000000012" and by_clip["01"]["status"] == "published"
    assert by_clip["02"]["post_id"] == "7300000000000000019" and by_clip["02"]["status"] == "published"


def test_same_caption_with_no_post_at_the_slot_concludes_nothing_and_logs_info_once(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify_at(tmp_path, "02", _EVENING)
    _same_caption(tmp_path, ["02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000012", posted="2026-10-09T12:00:00")])
    w = _reconcile_worker(config)

    with caplog.at_level(logging.INFO):
        w.tick()
        w.tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and entry["to_verify"] is True and not entry.get("post_id")
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "rien conclu" in r.getMessage()]
    assert len(infos) == 1


def test_two_posts_with_the_same_caption_and_slot_conclude_nothing_and_warn_once(tmp_path, monkeypatch, caplog):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify_at(tmp_path, "02", _EVENING)
    _same_caption(tmp_path, ["02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000019", posted="2026-10-09T19:30:00"),
        _post("7300000000000000020", posted="2026-10-09T19:30:00")])
    w = _reconcile_worker(config)

    with caplog.at_level(logging.INFO):
        w.tick()
        w.tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and entry["to_verify"] is True and not entry.get("post_id")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "rien conclu" in r.getMessage()]
    assert len(warnings) == 1


def test_a_single_caption_posted_at_another_hour_is_not_taken_for_the_entry(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify_at(tmp_path, "02", _EVENING)
    _same_caption(tmp_path, ["02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000012", posted="2026-10-09T12:00:00")])

    _reconcile_worker(config).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and entry["to_verify"] is True and not entry.get("post_id")


def test_a_single_caption_without_posted_time_keeps_the_current_rule(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify_at(tmp_path, "02", _EVENING)
    _same_caption(tmp_path, ["02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[_post("7300000000000000019", posted=None)])

    _reconcile_worker(config).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "published" and entry["post_id"] == "7300000000000000019"


def test_a_scheduled_entry_whose_caption_is_shown_only_at_another_hour_is_not_flagged_missing(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _scheduled_at(tmp_path, "02", _EVENING)
    _same_caption(tmp_path, ["02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000012", posted="2026-10-09T12:00:00")])

    _reconcile_worker(config).tick()

    assert "missing_on_tiktok" not in _entries(tmp_path, NO_CHANNEL)[0]


# --- TASK-5e6a1c00b06e : jamais de worker mort sur une exception inattendue ; rapprochement a la minute programmee ---


@pytest.mark.parametrize("error", [KeyError("x"), TypeError("x"), AttributeError("x")])
def test_an_unexpected_exception_under_the_learning_runner_never_leaves_tick(tmp_path, caplog, error):
    import logging

    def runner(now, *, config):
        raise error

    config = _learning_config(tmp_path)
    w = worker.Worker(config=config, spawner=FakeSpawner(), learning_runner=runner)
    w._veille_due = lambda: None
    with caplog.at_level(logging.ERROR):
        w.tick()
        w.tick()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR and "apprentissage" in r.getMessage()]) == 1
    last_error = _sync_json(config)["last_error"]
    assert last_error["where"] == "run_if_due" and type(error).__name__ in last_error["message"]


@pytest.mark.parametrize("error", [KeyError("x"), TypeError("x"), AttributeError("x")])
def test_an_unexpected_exception_under_the_repartition_runner_never_leaves_tick(tmp_path, caplog, error):
    import logging

    def runner(now, *, config):
        raise error

    w = worker.Worker(config=_learning_config(tmp_path), spawner=FakeSpawner(), repartition_runner=runner)
    w._veille_due = lambda: None
    with caplog.at_level(logging.ERROR):
        w.tick()
        w.tick()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR and "lendemain" in r.getMessage()]) == 1


def test_an_unexpected_exception_under_the_watch_never_leaves_tick(tmp_path, caplog, monkeypatch):
    import logging

    from clipper import watch

    def boom(*args, **kwargs):
        raise KeyError("video_id")

    monkeypatch.setattr(watch, "check", boom)
    config = _watch_env(tmp_path, {"ma_chaine": (True, 1800)})
    w = worker.Worker(config=config, spawner=FakeSpawner(), watch_lister=_WatchLister())
    with caplog.at_level(logging.ERROR):
        w.tick()
        w.tick()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR and "surveillance" in r.getMessage()]) == 1


_NINE_05 = datetime(2026, 10, 9, 9, 5, tzinfo=_PARIS)
_NINE_07 = datetime(2026, 10, 9, 9, 7, tzinfo=_PARIS)


def test_a_to_verify_entry_with_a_slot_rounded_by_tiktok_is_found_when_the_caption_is_unique(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify_at(tmp_path, "02", _NINE_07)
    _same_caption(tmp_path, ["02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000019", posted="2026-10-09T09:05:00")])

    _reconcile_worker(config).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "published" and entry["post_id"] == "7300000000000000019"


def test_a_to_verify_entry_with_the_recorded_effective_time_matches_exactly(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify_at(tmp_path, "02", _NINE_07)
    path = tmp_path / "state" / "publish" / f"{NO_CHANNEL}.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["tiktok_publish_at"] = _NINE_05.isoformat()
    path.write_text(json.dumps(entries), encoding="utf-8")
    _same_caption(tmp_path, ["02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000019", posted="2026-10-09T09:05:00")])

    _reconcile_worker(config).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "published"
    assert entry["tiktok_publish_at"] == _NINE_05.isoformat()  # l'heure effective est gardee, pas l'heure demandee


def test_a_to_verify_gap_wider_than_the_selector_step_is_not_accepted(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify_at(tmp_path, "02", _NINE_07)
    _same_caption(tmp_path, ["02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000019", posted="2026-10-09T09:00:00")])

    _reconcile_worker(config).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and entry["to_verify"] is True


def test_two_same_caption_posts_inside_the_five_minute_window_conclude_nothing(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _to_verify_at(tmp_path, "02", _NINE_07)
    _same_caption(tmp_path, ["02"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000019", posted="2026-10-09T09:05:00"),
        _post("7300000000000000020", posted="2026-10-09T09:10:00")])

    _reconcile_worker(config).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["status"] == "failed" and entry["to_verify"] is True and not entry.get("post_id")


def test_an_unconfirmed_schedule_records_the_effective_scheduled_time(tmp_path, monkeypatch):
    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", datetime.now(timezone.utc) + timedelta(days=3), mode="scheduled")
    pub = FakePublisher(scheduled_post_id=None)

    _pub_worker(config, pub).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert entry["to_verify"] is True
    assert entry["tiktok_publish_at"] == pub.calls[0]["schedule_at"].isoformat()


def test_a_scheduled_entry_is_compared_at_its_effective_time_not_the_requested_minute(tmp_path, monkeypatch, caplog):
    import logging

    config = _pub_env(tmp_path, monkeypatch)
    _manual(tmp_path, "01", _NINE_07, mode="scheduled", status="published", tiktok_state="scheduled_on_tiktok",
            post_id=None, post_url=None, published_at=_ago(hours=3).isoformat(),
            tiktok_publish_at=_NINE_05.isoformat())
    _same_caption(tmp_path, ["01"])
    _write_snapshot(tmp_path, "ef34ab", _ago(hours=1), posts=[
        _post("7300000000000000019", posted="2026-10-09T09:05:00")])

    with caplog.at_level(logging.INFO):
        _reconcile_worker(config).tick()

    entry = _entries(tmp_path, NO_CHANNEL)[0]
    assert "missing_on_tiktok" not in entry
    assert "rien conclu" not in caplog.text


# --------------------------------------------------------------------------
# TASK-4ca998e97789 (audit 10/10, lot A1) : une seule instance, une seule entree running,
# identite d'un processus = pid + heure de creation
# --------------------------------------------------------------------------


def _sleeper():
    return subprocess.Popen(_SLEEPER)


def _end(proc) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _running_entry(video_id: str, url: str, proc, *, created_at="real") -> dict:
    """Entree ``running`` pilotee par un vrai processus ; ``created_at="real"`` : son heure de creation,
    sinon la valeur donnee (une heure qui ne correspond pas = pid reattribue a un etranger)."""
    created = worker._process_created_at(proc.pid) if created_at == "real" else created_at
    return {**_entry(video_id, url, status="running", pid=proc.pid), "pid_created_at": created}


def _write_heartbeat(config: Config, pid: int, created_at) -> None:
    path = worker.heartbeat_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "at": datetime.now(timezone.utc).isoformat(), "busy": False,
                                "pid_created_at": created_at}), encoding="utf-8")


def test_process_created_at_identifies_a_process_and_process_alive_turns_false_at_its_death():
    proc = _sleeper()
    try:
        created = worker._process_created_at(proc.pid)
        assert isinstance(created, int) and created > 0
        assert worker._process_created_at(proc.pid) == created  # stable tant que le processus vit
        assert worker.process_alive(proc.pid, created) is True
        assert worker.process_alive(proc.pid, created - 1) is False  # meme pid, autre processus
        assert worker.process_alive(proc.pid, None) is True  # entree ancienne sans heure : existence seule
    finally:
        _end(proc)
    assert worker.process_alive(proc.pid, created) is False


def test_startup_and_tick_adopt_a_live_foreign_running_entry_and_launch_nothing(tmp_path):
    """coeur-I1 : serve relance (ou worker tombe) pendant qu'un enfant tourne encore : le nouveau worker
    n'en lance pas un second a cote, il surveille l'enfant survivant et retire son entree a sa mort."""
    config = _config(tmp_path)
    orphan = _sleeper()
    try:
        _write_queue(config, [_running_entry(VIDEO_A, URL_A, orphan), _entry(VIDEO_B, URL_B)])
        _pipeline_state(VIDEO_A, config, status="running")
        _pipeline_state(VIDEO_B, config, status="pending")
        spawner = FakeSpawner()
        w = worker.Worker(config=config, spawner=spawner)

        w.startup()
        w.tick()
        w.tick()

        assert spawner.calls == []  # rien lance tant que l'enfant d'avant vit (ADR-fb9b, ADR-35b7 §1)
        assert [(e["video_id"], e["status"]) for e in _queue(config)] == [(VIDEO_A, "running"), (VIDEO_B, "waiting")]
        assert w._entry is not None and w._entry["video_id"] == VIDEO_A  # entree adoptee
        assert worker.is_interrupted(pipeline.load_state(VIDEO_A, config=config), config) is False

        _end(orphan)
        w.tick()

        assert [(e["video_id"], e["status"]) for e in _queue(config)] == [(VIDEO_B, "running")]
        assert len(spawner.calls) == 1 and spawner.calls[0][-1] == URL_B
        assert pipeline.load_state(VIDEO_A, config=config)["status"] == "failed"  # mort sans ecrire : visible
    finally:
        _end(orphan)


def test_an_adopted_child_that_finishes_cleanly_keeps_the_state_it_wrote(tmp_path):
    config = _config(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    try:
        _write_queue(config, [_running_entry(VIDEO_A, URL_A, child)])
        _pipeline_state(VIDEO_A, config, status="running")
        w = worker.Worker(config=config, spawner=FakeSpawner())
        w.startup()
        w.tick()
        assert w._entry["video_id"] == VIDEO_A
        state = pipeline.load_state(VIDEO_A, config=config)
        state["status"] = "done"
        pipeline.save_state(state, config=config)  # l'enfant ecrit sa fin...
        child.wait()  # ... et quitte avec le code 0

        w.tick()

        assert _queue(config) == []
        assert pipeline.load_state(VIDEO_A, config=config)["status"] == "done"
    finally:
        _end(child)


def test_a_foreign_running_entry_whose_process_died_is_requeued_at_tick_time(tmp_path):
    config = _config(tmp_path)
    dead = _sleeper()
    created = worker._process_created_at(dead.pid)
    _end(dead)
    _write_queue(config, [_entry(VIDEO_B, URL_B),
                          {**_entry(VIDEO_A, URL_A, status="running", pid=dead.pid), "pid_created_at": created}])
    _pipeline_state(VIDEO_A, config, status="running")
    spawner = FakeSpawner()
    w = worker.Worker(config=config, spawner=spawner)

    w.tick()  # sans startup : la file a change depuis (autre worker mort)

    assert len(spawner.calls) == 1 and spawner.calls[0][-1] == URL_A  # reprise en tete (SPEC-74e9 §2.4)
    assert [(e["video_id"], e["status"]) for e in _queue(config)] == [(VIDEO_A, "running"), (VIDEO_B, "waiting")]


def test_second_worker_startup_refuses_with_the_pid_of_the_other(tmp_path):
    """coeur-I2 / publication-I2 : deux workers sur la meme file = deux enfants lourds ; le second refuse."""
    config = _config(tmp_path)
    other = _sleeper()
    try:
        _write_heartbeat(config, other.pid, worker._process_created_at(other.pid))

        with pytest.raises(worker.WorkerError, match=rf"un worker tourne déjà \(pid {other.pid},"):
            worker.Worker(config=config, spawner=FakeSpawner()).startup()
    finally:
        _end(other)


def test_startup_proceeds_when_the_heartbeat_pid_is_dead_or_reused(tmp_path):
    config = _config(tmp_path)
    stranger = _sleeper()
    try:
        _write_heartbeat(config, stranger.pid, worker._process_created_at(stranger.pid) - 1)  # pid reattribue
        worker.Worker(config=config, spawner=FakeSpawner()).startup()
        assert worker.read_heartbeat(config)["pid"] == os.getpid()  # la place est prise tout de suite
    finally:
        _end(stranger)
    dead = _sleeper()
    created = worker._process_created_at(dead.pid)
    _end(dead)
    _write_heartbeat(config, dead.pid, created)
    worker.Worker(config=config, spawner=FakeSpawner()).startup()


def test_read_heartbeat_reports_stopped_when_the_pid_belongs_to_another_process(tmp_path):
    config = _config(tmp_path)
    stranger = _sleeper()
    try:
        _write_heartbeat(config, stranger.pid, worker._process_created_at(stranger.pid) - 1)
        beat = worker.read_heartbeat(config)
        assert beat["state"] == "stopped" and str(stranger.pid) in beat["reason"]
        _write_heartbeat(config, stranger.pid, worker._process_created_at(stranger.pid))
        assert worker.read_heartbeat(config)["state"] == "active"
    finally:
        _end(stranger)


def test_the_heartbeat_carries_the_creation_time_of_the_worker_process(tmp_path):
    config = _config(tmp_path)
    worker.Worker(config=config, spawner=FakeSpawner()).tick()
    beat = json.loads(worker.heartbeat_path(config).read_text(encoding="utf-8"))
    assert beat["pid"] == os.getpid() and beat["pid_created_at"] == worker._process_created_at(os.getpid())


def test_main_worker_command_reports_a_worker_already_running(tmp_path, monkeypatch, capsys):
    from clipper.__main__ import main

    config = _config(tmp_path)
    monkeypatch.setattr("clipper.__main__.load_config", lambda path="config.toml": config)

    class FakeWorker:
        def __init__(self, *, config):
            pass

        def loop(self):
            raise worker.WorkerError("un worker tourne déjà (pid 4242) : arrête-le avant d'en lancer un autre")

    monkeypatch.setattr("clipper.worker.Worker", FakeWorker)

    assert main(["worker"]) == 1
    assert "un worker tourne déjà (pid 4242)" in capsys.readouterr().err


def test_launch_records_the_creation_time_and_launch_time_of_the_child_in_the_entry(tmp_path):
    config = _config(tmp_path)
    child = _sleeper()
    try:
        worker.enqueue(URL_A, None, "run", config=config)
        worker.Worker(config=config, spawner=FakeSpawner(FakeProcess(pid=child.pid))).tick()
        entry = _queue(config)[0]
        assert entry["status"] == "running" and entry["pid"] == child.pid
        assert entry["pid_created_at"] == worker._process_created_at(child.pid)
        assert datetime.fromisoformat(entry["launched_at"]) <= datetime.now(timezone.utc)
    finally:
        _end(child)


def test_cancel_does_not_kill_a_process_whose_creation_time_differs(tmp_path):
    """coeur-I3 : PC rallume, le pid de l'entree appartient maintenant a un autre processus : Annuler ne le
    tue pas, la video est bien « interrompue » et reprenable."""
    config = _config(tmp_path)
    stranger = _sleeper()
    try:
        _write_queue(config, [_running_entry(VIDEO_A, URL_A, stranger, created_at=12345)])
        state = _pipeline_state(VIDEO_A, config, status="running")
        assert worker.is_interrupted(state, config) is True

        worker.cancel(VIDEO_A, config=config)

        assert stranger.poll() is None  # l'etranger vit toujours
        assert _queue(config) == []
        assert pipeline.load_state(VIDEO_A, config=config)["status"] == "failed"
    finally:
        _end(stranger)


def test_cancel_kills_the_process_whose_creation_time_matches(tmp_path):
    config = _config(tmp_path, cancel_grace_s=2)
    child = _sleeper()
    try:
        _write_queue(config, [_running_entry(VIDEO_A, URL_A, child)])
        _pipeline_state(VIDEO_A, config, status="running")

        worker.cancel(VIDEO_A, config=config)

        assert child.wait(timeout=5) is not None
    finally:
        _end(child)


def test_resume_accepts_a_running_entry_whose_pid_was_reused(tmp_path):
    config = _config(tmp_path)
    stranger = _sleeper()
    try:
        _write_queue(config, [_running_entry(ORPHAN_ID, f"https://youtu.be/{ORPHAN_ID}", stranger, created_at=1)])
        _orphan_state(config)
        worker.resume(ORPHAN_ID, config=config)
        assert any(e["video_id"] == ORPHAN_ID and e["status"] == "waiting" for e in _queue(config))
    finally:
        _end(stranger)


def test_startup_requeues_a_running_entry_whose_pid_was_reused(tmp_path):
    config = _config(tmp_path)
    stranger = _sleeper()
    try:
        _write_queue(config, [_running_entry(VIDEO_A, URL_A, stranger, created_at=1)])
        worker.Worker(config=config, spawner=FakeSpawner()).startup()
        assert [(e["status"], e["pid"]) for e in _queue(config)] == [("waiting", None)]
        assert stranger.poll() is None
    finally:
        _end(stranger)


def test_move_to_front_keeps_every_running_entry(tmp_path):
    config = _config(tmp_path)
    _write_queue(config, [_entry(VIDEO_A, URL_A, status="running", pid=1), _entry(VIDEO_B, URL_B, status="running", pid=2),
                          _entry("C", "https://youtu.be/C"), _entry("D", "https://youtu.be/D")])

    worker.move_to_front("D", config=config)

    assert [e["video_id"] for e in _queue(config)] == [VIDEO_A, VIDEO_B, "D", "C"]


def test_publish_due_survives_an_unexpected_error_and_logs_it_once(tmp_path, monkeypatch, caplog):
    """publication-M1 : une exception hors liste dans le chemin de publication ne tue plus le worker."""
    config = _pub_env(tmp_path, monkeypatch)
    w = _pub_worker(config, FakePublisher())

    def boom(*args, **kwargs):
        raise RuntimeError("accounts.json : entrée qui n'est pas un objet")

    monkeypatch.setattr(worker.accounts_mod, "list_accounts", boom)
    with caplog.at_level("ERROR", logger="clipper.worker"):
        w.tick()
        w.tick()

    messages = [r.getMessage() for r in caplog.records
                if "publication" in r.getMessage() and "RuntimeError" in r.getMessage()]
    assert len(messages) == 1 and "entrée qui n'est pas un objet" in messages[0]


def test_cancel_resets_the_running_step_to_pending(tmp_path):
    """coeur-M1 : une video annulee n'a plus d'etape « en cours »."""
    config = _config(tmp_path)
    dead = _sleeper()
    created = worker._process_created_at(dead.pid)
    _end(dead)
    _write_queue(config, [{**_entry(VIDEO_A, URL_A, status="running", pid=dead.pid), "pid_created_at": created}])
    _orphan_state(config, video_id=VIDEO_A, running="render")

    worker.cancel(VIDEO_A, config=config)

    statuses = _statuses(config, VIDEO_A)
    assert statuses["render"] == "pending" and statuses["subtitles"] == "done"
    assert "running" not in statuses.values()
    assert pipeline.load_state(VIDEO_A, config=config)["status"] == "failed"


def test_a_child_crash_resets_the_running_step_to_pending(tmp_path):
    config = _config(tmp_path)
    worker.enqueue(URL_A, None, "run", config=config)
    process = FakeProcess()
    w = worker.Worker(config=config, spawner=FakeSpawner(process))
    w.tick()
    _orphan_state(config, video_id=VIDEO_A, running="reframe")  # l'enfant en etait la quand il est mort
    process.finish(-1073741819)

    w.tick()

    state = pipeline.load_state(VIDEO_A, config=config)
    statuses = _statuses(config, VIDEO_A)
    assert state["status"] == "failed" and "-1073741819" in state["reason"]
    assert statuses["reframe"] == "pending" and statuses["captions"] == "done"


def test_a_child_dying_at_once_after_keep_channel_reports_its_exit_code(tmp_path):
    """coeur-M2 : l'ecriture du style dans pipeline.json avant le lancement ne passe plus pour un etat ecrit
    par l'enfant ; l'ancienne raison d'echec ne masque plus le crash."""
    base = _config(tmp_path)
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "ma_chaine.toml").write_text('[channel]\ndisplay_name = "Ma chaine"\n', encoding="utf-8")
    (tmp_path / "config.toml").write_text('mode = "auto"\n', encoding="utf-8")
    config = Config(mode="auto", workspace_dir=base.workspace_dir, output_dir=base.output_dir,
                    _sections={**base._sections, "watch": {"state_dir": str(tmp_path / "state" / "watch"),
                                                           "presets_dir": str(presets),
                                                           "base_config": str(tmp_path / "config.toml")}})
    state = _pipeline_state(VIDEO_A, config, status="failed")
    state["reason"] = "ANCIEN ECHEC : transcribe : TransientLLMError quota"
    pipeline.save_state(state, config=config)
    _write_queue(config, [{**_entry(VIDEO_A, URL_A), "channel": "ma_chaine"}])
    process = FakeProcess()
    w = worker.Worker(config=config, spawner=FakeSpawner(process))
    w.tick()
    process.finish(1)

    w.tick()

    state = pipeline.load_state(VIDEO_A, config=config)
    assert state["status"] == "failed" and "code 1" in state["reason"]
    assert "ANCIEN ECHEC" not in state["reason"]


def test_build_command_separates_a_video_id_starting_with_a_dash(tmp_path):
    """web-I6 : un identifiant YouTube commencant par « - » est un positionnel, pas une option."""
    from clipper.__main__ import build_parser

    entry = {"video_id": "-wtIMTCHWuI", "url": "-wtIMTCHWuI", "channel": None, "action": "render",
             "force_steps": ["render"]}
    cmd = worker._build_command(entry)
    assert cmd[3:] == ["render", "--force-step", "render", "--", "-wtIMTCHWuI"]  # options avant « -- »
    args = build_parser().parse_args(cmd[3:])
    assert args.command == "render" and args.video_id == "-wtIMTCHWuI" and args.force_step == ["render"]

    run = worker._build_command({**entry, "action": "run", "url": "https://youtu.be/-wtIMTCHWuI", "force_steps": []})
    assert run[3:] == ["run", "--", "https://youtu.be/-wtIMTCHWuI"]
    assert build_parser().parse_args(run[3:]).url == "https://youtu.be/-wtIMTCHWuI"

    prefetch = worker._build_prefetch_command({**entry, "url": "https://youtu.be/-wtIMTCHWuI"})
    assert prefetch[3:] == ["download", "--", "https://youtu.be/-wtIMTCHWuI"]
    assert build_parser().parse_args(prefetch[3:]).url == "https://youtu.be/-wtIMTCHWuI"
