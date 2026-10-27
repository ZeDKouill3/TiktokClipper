from __future__ import annotations

import subprocess
import sys


def test_cli_config_explicit_and_missing_fails_naming_the_path(isolated_cwd):
    result = subprocess.run(
        [sys.executable, "-m", "clipper", "--config", "definitely_absent_xyz.toml", "status", "somefakeid"],
        capture_output=True,
        text=True,
        cwd=isolated_cwd,
    )

    assert result.returncode == 1
    assert "definitely_absent_xyz.toml" in result.stderr

def _run_serve(monkeypatch, argv):
    import uvicorn

    from clipper import __main__ as cli
    from clipper import web

    launched = []

    class FakeProc:
        pid = 4242

        def terminate(self):
            pass

    from clipper import worker

    monkeypatch.setattr(worker, "terminate_tree", lambda pid, grace, **kw: None)  # jamais de vrai taskkill

    monkeypatch.setattr(cli, "_popen", lambda cmd: launched.append(cmd) or FakeProc())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(web, "create_app", lambda config: object())
    assert cli.main(argv) == 0
    return launched


def test_serve_passes_its_config_to_the_worker_child(isolated_cwd, monkeypatch):
    cfg = isolated_cwd / "autre.toml"
    cfg.write_text("", encoding="utf-8")
    (isolated_cwd / "config.toml").write_text("", encoding="utf-8")

    launched = _run_serve(monkeypatch, ["--config", str(cfg), "serve"])

    assert launched == [[sys.executable, "-m", "clipper", "--config", str(cfg), "worker"]]


def test_serve_without_config_launches_the_plain_worker_child(isolated_cwd, monkeypatch):
    launched = _run_serve(monkeypatch, ["serve"])

    assert launched == [[sys.executable, "-m", "clipper", "worker"]]



def test_worker_command_hands_the_loaded_config_file_and_its_base_to_the_worker(isolated_cwd, monkeypatch):
    from clipper import __main__ as cli
    from clipper import worker

    cfg = isolated_cwd / "autre.toml"
    cfg.write_text("", encoding="utf-8")
    (isolated_cwd / "config.toml").write_text("", encoding="utf-8")
    seen = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def loop(self):
            pass

    monkeypatch.setattr(worker, "Worker", FakeWorker)

    assert cli.main(["--config", str(cfg), "worker"]) == 0
    assert str(seen["config_path"]) == str(cfg)
    assert str(seen["config_base"]) == "config.toml"


def test_worker_command_without_config_watches_config_toml_without_base(isolated_cwd, monkeypatch):
    from clipper import __main__ as cli
    from clipper import worker

    seen = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def loop(self):
            pass

    monkeypatch.setattr(worker, "Worker", FakeWorker)

    assert cli.main(["worker"]) == 0
    assert str(seen["config_path"]) == "config.toml"
    assert seen.get("config_base") is None


def test_drain_command_raises_the_request_and_returns_0_once_idle(isolated_cwd, capsys):
    from clipper import __main__ as cli
    from clipper import worker
    from clipper.config import load_config

    assert cli.main(["drain", "--wait", "5"]) == 0

    assert worker.drain_status(load_config())["draining"] is True  # reste posee : le worker qui demarre la leve
    assert "attente" in capsys.readouterr().out


def test_drain_command_times_out_with_a_non_zero_code_and_kills_nothing(isolated_cwd, monkeypatch, capsys):
    import time

    import pytest

    from clipper import __main__ as cli
    from clipper import worker

    busy = {"draining": True, "publishing": {"video_id": "aaaaaaaaaaa", "clip_id": "01", "account": "ab12cd"},
            "stats": None, "idle": False}
    monkeypatch.setattr(worker, "drain_status", lambda config: busy)
    monkeypatch.setattr(worker, "terminate_tree", lambda *a, **k: pytest.fail("clipper drain ne tue jamais rien"))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ticks = iter(range(0, 1000, 2))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))

    code = cli.main(["drain", "--wait", "10"])

    assert code != 0
    err = capsys.readouterr().err
    assert "aaaaaaaaaaa/01" in err and "ab12cd" in err


def test_drain_command_without_wait_only_raises_the_request(isolated_cwd, monkeypatch):
    from clipper import __main__ as cli
    from clipper import worker

    monkeypatch.setattr(worker, "drain_status", lambda config: {
        "draining": True, "publishing": {"video_id": "v", "clip_id": "01", "account": "a"}, "stats": None,
        "idle": False})

    assert cli.main(["drain"]) == 0
