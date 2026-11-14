"""Worker et serve : crochets d'exception et lignes démarré/arrêté dans le journal (TASK-5ee96)."""

from __future__ import annotations

import logging
import sys
import threading

import pytest

from clipper import journal
from clipper.__main__ import main
from clipper.config import Config


@pytest.fixture(autouse=True)
def _restore_hooks():
    saved = (sys.excepthook, threading.excepthook)
    yield
    sys.excepthook, threading.excepthook = saved


def _config(tmp_path) -> Config:
    return Config(mode="auto", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
                  _sections={"journal": {"enabled": False}})


def test_install_crash_hooks_log_critical_with_trace(caplog, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)  # l'ancien crochet : silencieux dans le test
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    journal.install_crash_hooks()
    try:
        raise RuntimeError("mort subite")
    except RuntimeError:
        exc_info = sys.exc_info()
    with caplog.at_level(logging.CRITICAL):
        sys.excepthook(*exc_info)
        threading.excepthook(threading.ExceptHookArgs((*exc_info, threading.current_thread())))
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL and r.exc_info]
    assert len(critical) == 2 and all("mort subite" in str(r.exc_info[1]) for r in critical)


def _patch_worker(monkeypatch, tmp_path, loop):
    config = _config(tmp_path)
    monkeypatch.setattr("clipper.__main__.load_config", lambda path="config.toml": config)

    class FakeWorker:
        def __init__(self, **kw):
            pass

        def loop(self):
            loop()

    monkeypatch.setattr("clipper.worker.Worker", FakeWorker)


def test_worker_logs_started_and_stopped_lines(tmp_path, monkeypatch, caplog):
    _patch_worker(monkeypatch, tmp_path, lambda: None)
    with caplog.at_level(logging.INFO):
        assert main(["worker"]) == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("worker démarré pid=") for m in messages)
    assert any(m.startswith("worker arrêté") and "code 0" in m for m in messages)
    assert sys.excepthook is not sys.__excepthook__


def test_worker_logs_stopped_on_ctrl_c(tmp_path, monkeypatch, caplog):
    def boom():
        raise KeyboardInterrupt

    _patch_worker(monkeypatch, tmp_path, boom)
    with caplog.at_level(logging.INFO), pytest.raises(KeyboardInterrupt):
        main(["worker"])
    assert any(r.getMessage().startswith("worker arrêté") and "Ctrl+C" in r.getMessage() for r in caplog.records)


def test_worker_nonzero_exit_is_logged_with_its_code(tmp_path, monkeypatch, caplog):
    from clipper import worker as worker_mod

    def fail():
        raise worker_mod.WorkerError("20 échecs consécutifs")

    _patch_worker(monkeypatch, tmp_path, fail)
    with caplog.at_level(logging.INFO):
        assert main(["worker"]) == 1
    assert any(r.getMessage().startswith("worker arrêté") and "code 1" in r.getMessage() for r in caplog.records)


def test_serve_logs_started_and_stopped_even_when_uvicorn_raises(tmp_path, monkeypatch, caplog):
    from clipper import worker as worker_mod

    config = _config(tmp_path)
    monkeypatch.setattr("clipper.__main__.load_config", lambda path="config.toml": config)

    def boom(app, host, port):
        raise OSError("port pris")

    monkeypatch.setattr("uvicorn.run", boom)

    class P:
        pid = 1

        def poll(self):
            return None

    monkeypatch.setattr("clipper.__main__._popen", lambda cmd, **kw: P())
    monkeypatch.setattr(worker_mod, "watch_spawned", lambda proc, cfg: None)
    monkeypatch.setattr(worker_mod, "terminate_tree", lambda pid, grace, **kw: None)
    with caplog.at_level(logging.INFO), pytest.raises(OSError):
        main(["serve"])
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("serve démarré pid=") for m in messages)
    assert any(m.startswith("serve arrêté") and "port pris" in m for m in messages)
