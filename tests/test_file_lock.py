from __future__ import annotations

import errno
import multiprocessing
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from moeguard.utils import file_lock


def test_windows_lock_uses_nonblocking_first_byte_without_writing(tmp_path: Path, monkeypatch):
    calls = []

    def locking(fd, mode, count):
        calls.append((mode, count, os.lseek(fd, 0, os.SEEK_CUR)))

    monkeypatch.setattr(file_lock, "_WINDOWS", True, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=locking))
    path = tmp_path / "state.lock"
    with file_lock.FileLock(path) as owner:
        assert calls == [(2, 1, 0)]
        assert owner._fd is not None
        assert not os.get_inheritable(owner._fd)
        assert path.read_bytes() == b""
    assert path.exists()


def _hold_lock(path, ready, release):
    with file_lock.FileLock(path):
        ready.set()
        assert release.poll(15)
        assert release.recv() == "quit"


@pytest.mark.parametrize("crash", [False, True])
def test_native_lock_rejects_second_process_and_releases_on_exit(tmp_path: Path, crash: bool):
    # Runs the real backend on either POSIX or Windows (spawn, no fork inheritance).
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    stop_read, stop_write = context.Pipe(duplex=False)
    path = tmp_path / "state.lock"
    worker = context.Process(target=_hold_lock, args=(path, ready, stop_read))
    worker.start()
    try:
        assert ready.wait(10)
        before = path.stat().st_ino
        os.utime(path, (1, 1))  # Age must never override a live OS owner.
        contender = file_lock.FileLock(path)
        assert contender.acquire(blocking=False) is False
        assert contender.acquire(blocking=False) is False  # Failed acquire closes its fd.
        contender.release()
        if crash:
            worker.terminate()
        else:
            stop_write.send("quit")
        worker.join(5)
        assert not worker.is_alive()
        assert worker.exitcode != 0 if crash else worker.exitcode == 0
        assert contender.acquire(blocking=False) is True
        contender.release()
        assert path.stat().st_ino == before
        with file_lock.FileLock(path) as owner:
            assert file_lock.FileLock(path).acquire(blocking=False) is False
            with pytest.raises(RuntimeError, match="not reentrant"):
                owner.acquire(blocking=False)
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(5)
        stop_read.close()
        stop_write.close()


def test_contended_lock_has_a_bounded_wait_and_closes_descriptor(tmp_path, monkeypatch, caplog):
    now = [0.0]

    def busy(_fd):
        raise BlockingIOError(errno.EAGAIN, "held by another process")

    def advance(seconds):
        now[0] += seconds
        assert now[0] <= 30.01, "unbounded lock wait"

    monkeypatch.setattr(file_lock.FileLock, "_try_lock", staticmethod(busy))
    monkeypatch.setattr(file_lock.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(file_lock.time, "sleep", advance)
    lock = file_lock.FileLock(tmp_path / "state.lock")
    with pytest.raises(TimeoutError, match="state lock timed out"):
        lock.acquire()
    assert lock._fd is None
    assert "operation=wait_timeout" in caplog.text


@pytest.mark.parametrize("swap_during_open", [False, True])
def test_lock_refuses_symlink_without_touching_target(tmp_path, monkeypatch, swap_during_open):
    path = tmp_path / "state.lock"
    target = tmp_path / "user-data"
    target.write_bytes(b"must not change")
    # Preflight symlink support for both scheduling variants (Windows may need
    # developer mode or an elevated test process).
    try:
        path.symlink_to(target)
        path.unlink()
    except OSError:
        pytest.skip("symlink creation unavailable")
    if swap_during_open:
        path.write_bytes(b"")
        original_open = file_lock.os.open

        def swap_then_open(*args, **kwargs):
            path.unlink()
            path.symlink_to(target)
            return original_open(*args, **kwargs)

        monkeypatch.setattr(file_lock.os, "open", swap_then_open)
    else:
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation unavailable")
    lock = file_lock.FileLock(path)
    with pytest.raises(OSError):
        lock.acquire(blocking=False)
    assert lock._fd is None
    assert target.read_bytes() == b"must not change"
    assert path.is_symlink()


def test_lock_io_failure_is_not_retried_and_logs_no_exception_details(
    tmp_path, monkeypatch, caplog,
):
    def fail(_fd):
        raise OSError(errno.EIO, "private request detail")

    monkeypatch.setattr(file_lock.FileLock, "_try_lock", staticmethod(fail))
    lock = file_lock.FileLock(tmp_path / "state.lock")
    with pytest.raises(OSError) as error:
        lock.acquire()
    assert error.value.errno == errno.EIO
    assert lock._fd is None
    assert "operation=acquire errno=5" in caplog.text
    assert "private request detail" not in caplog.text
