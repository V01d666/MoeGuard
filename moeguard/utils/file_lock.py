"""OS-owned file locks; lock files persist and are never used as stale markers."""
from __future__ import annotations

import errno
import logging
import os
import stat
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_WINDOWS = sys.platform == "win32"


class FileLock:
    """Exclusive, non-reentrant lock released even if the process crashes.

    Each acquisition opens its own descriptor, so separate objects/threads also
    contend. Never unlink the file: replacing its inode splits the lock domain.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self, *, blocking: bool = True) -> bool:
        if self._fd is not None:
            raise RuntimeError("file lock is not reentrant")
        fd = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise OSError(errno.ELOOP, "unsafe state lock path")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            fd = os.open(self.path, flags, 0o600)
            opened, named = os.fstat(fd), self.path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(named.st_mode)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise OSError(errno.ELOOP, "unsafe state lock path")
            os.set_inheritable(fd, False)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            logger.error("state_lock_failed operation=open errno=%s", exc.errno)
            raise
        deadline = time.monotonic() + 30.0
        try:
            while True:
                try:
                    self._try_lock(fd)
                    self._fd = fd
                    return True
                except OSError as exc:
                    retryable = exc.errno == errno.EAGAIN or (
                        _WINDOWS and exc.errno == errno.EACCES
                        and getattr(exc, "winerror", 33) in (None, 33)
                    )
                    if not retryable:
                        logger.error("state_lock_failed operation=acquire errno=%s", exc.errno)
                        raise
                    if not blocking:
                        return False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.error("state_lock_failed operation=wait_timeout")
                        raise TimeoutError("state lock timed out") from None
                    time.sleep(min(.05, remaining))
        finally:
            if self._fd != fd:
                os.close(fd)

    @staticmethod
    def _try_lock(fd: int) -> None:
        if _WINDOWS:
            import msvcrt

            # Windows can lock a region past EOF. Do not initialize/truncate a
            # byte here: writing before acquiring would itself race the owner.
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
