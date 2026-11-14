"""Exercise the real run() boundary in spawned processes, without native windows."""
from __future__ import annotations

import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _run_app(root, ready, release, pipe, hold):
    from moeguard import app as app_module
    from moeguard.utils import paths

    def record(stage):
        with (root / "effects.txt").open("a", encoding="utf-8") as output:
            output.write(stage + "\n")

    class Application:
        aboutToQuit = SimpleNamespace(connect=lambda _callback: None)

        def __init__(self, _argv):
            record("qt")

        def __getattr__(self, _name):
            return lambda *_args: None

        def exec(self):
            ready.set()
            if hold:
                assert release.poll(15)
                assert release.recv() == "quit"
            return 0

    class Controller:
        def start(self):
            record("start")

        def shutdown(self):
            record("shutdown")

    with (
        patch.object(paths, "_BASE_DIR", root),
        patch.object(app_module, "setup_logging", lambda: record("logging")),
        patch.object(app_module, "QApplication", Application),
        patch.object(app_module, "application_icons", lambda: (None, None)),
        patch.object(app_module, "MoeGuardApp", Controller),
    ):
        pipe.send(app_module.run([], configure=lambda _app: record("configure")))
    pipe.close()


@pytest.mark.parametrize("crash", [False, True])
def test_duplicate_app_exits_before_any_shared_state_or_configuration(tmp_path: Path, crash: bool):
    # An old configuration does not need a migration or a preexisting lock file.
    config = tmp_path / "config.toml"
    config.write_text("[pet]\nscale = 1.0\n", encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    stop_read, stop_write = context.Pipe(duplex=False)
    pairs = [context.Pipe() for _ in range(2)]
    workers = [context.Process(
        target=_run_app, args=(tmp_path, ready, stop_read, child, index == 0),
    ) for index, (_, child) in enumerate(pairs)]
    try:
        workers[0].start()
        assert ready.wait(10)
        first_effects = (tmp_path / "effects.txt").read_bytes()
        workers[1].start()
        assert pairs[1][0].poll(10)
        assert pairs[1][0].recv() == 0
        workers[1].join(5)
        assert workers[1].exitcode == 0
        assert (tmp_path / "effects.txt").read_bytes() == first_effects
        assert first_effects.decode().splitlines() == ["logging", "qt", "configure", "start"]
        assert config.read_text(encoding="utf-8") == "[pet]\nscale = 1.0\n"
        if crash:
            workers[0].terminate()
        else:
            stop_write.send("quit")
            assert pairs[0][0].poll(10)
            assert pairs[0][0].recv() == 0
        workers[0].join(5)
        assert not workers[0].is_alive()
        assert workers[0].exitcode != 0 if crash else workers[0].exitcode == 0


        parent, child = context.Pipe()
        pairs.append((parent, child))
        restarted = context.Process(
            target=_run_app, args=(tmp_path, ready, stop_read, child, False),
        )
        workers.append(restarted)
        restarted.start()
        assert parent.poll(10)
        assert parent.recv() == 0
        restarted.join(5)
        assert restarted.exitcode == 0
        assert (tmp_path / "effects.txt").read_bytes() == first_effects * 2
    finally:
        for worker in workers:
            if worker.pid is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(5)
        for parent, child in pairs:
            parent.close()
            child.close()
        stop_read.close()
        stop_write.close()


def test_startup_lock_path_failure_stops_before_logging(tmp_path, monkeypatch, caplog):
    from moeguard import app as app_module

    blocker = tmp_path / "not-a-directory"
    blocker.write_bytes(b"old user data")
    monkeypatch.setattr(app_module, "base_dir", lambda: blocker)
    monkeypatch.setattr(app_module, "setup_logging", lambda: pytest.fail("shared state touched"))
    assert app_module.run([]) == 1
    assert "app_start_refused reason=state_lock_unavailable" in caplog.text
    assert "operation=open" in caplog.text
    assert blocker.read_bytes() == b"old user data"


def test_startup_exception_releases_lock(tmp_path, monkeypatch):
    from moeguard import app as app_module
    from moeguard.utils.file_lock import FileLock

    def fail():
        raise RuntimeError("startup failed")

    monkeypatch.setattr(app_module, "base_dir", lambda: tmp_path)
    monkeypatch.setattr(app_module, "setup_logging", fail)
    with pytest.raises(RuntimeError, match="startup failed"):
        app_module.run([])
    lock = FileLock(tmp_path / "application.lock")
    assert lock.acquire(blocking=False)
    lock.release()
