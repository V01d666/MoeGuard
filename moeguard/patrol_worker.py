"""Headless entry point for a host that owns the visible capture indicator.

The host must show the indicator before writing the start command, hold the
application lock, and keep stdin open for the lifetime of the patrol. Closing
stdin requests a stop and exits the worker. No standalone UI is launched.
"""

from __future__ import annotations

import os
import sys
from threading import Event, Thread
from typing import cast

from PySide6.QtCore import QCoreApplication, QTimer

from moeguard.app import MoeGuardApp
from moeguard.config import AppConfig
from moeguard.patrol_setup import enroll, settings
from moeguard.pet.feedback import FeedbackController
from moeguard.ui.tray import TrayIcon


def _status(value: str) -> None:
    print(value, flush=True)


class _HostFeedback:
    """Report state transitions without exposing camera or evidence data."""

    @staticmethod
    def show_message(*_args) -> None:
        _status("notice")

    @staticmethod
    def set_state(active: bool) -> None:
        if active:
            _status("active")

    @staticmethod
    def on_patrol_start() -> None:
        pass  # The legacy camera-ready path reports through set_state.

    @staticmethod
    def on_companion() -> None:
        pass  # The entry point reports the final state after stopping capture.

    @staticmethod
    def greet_return(had_incidents: bool) -> None:
        _status("return_with_evidence" if had_incidents else "return")

    @staticmethod
    def alert_stranger() -> None:
        _status("evidence")


class _Worker(MoeGuardApp):
    def _show_patrol_banner(self) -> None:
        pass  # The native host owns the indicator before capture is allowed.

    def _hide_patrol_banner(self) -> None:
        pass  # The native host removes the indicator after process exit.

    def start_capture(self, trigger: bytes, stopping: Event) -> bool:
        self._config = AppConfig.load()
        if not self._has_patrol_consent() or not self._config.evidence.show_patrol_banner:
            _status("setup_required")
            return False
        if trigger == b"lock\n" and not self._config.presence.auto_patrol_enabled:
            _status("disabled")
            return False
        self._load_patrol_runtime()
        if not self._can_start_patrol():
            _status("setup_required")
            return False
        if stopping.is_set():
            return False
        feedback = _HostFeedback()
        self._tray = cast(TrayIcon, feedback)
        self._feedback = cast(FeedbackController, feedback)
        self._wire_patrol_signals()
        if trigger == b"lock\n":
            self._on_lock_screen()
        else:
            self._patrol_trigger = "manual"
            self.state_machine.on_manual_start()
        return self.state_machine.is_patrolling

    def close_capture(self) -> None:
        if self._state_machine is not None and self._state_machine.is_patrolling:
            self._state_machine.on_manual_stop()
        self._stop_patrol_runtime()
        if self._db is not None:
            self._db.close()


def main() -> int:
    if sys.argv[1:] == ["--settings"]:
        return settings()
    if sys.argv[1:] == ["--enroll"]:
        return enroll()
    if sys.argv[1:]:
        return 2
    # Read unbuffered so the stop-waiting daemon does not hold Python's buffered
    # stdin lock when a camera failure causes an early exit.
    fd = sys.stdin.fileno()
    trigger = bytearray()
    while len(trigger) < 8:
        byte = os.read(fd, 1)
        trigger += byte
        if not byte or byte == b"\n":
            break
    if trigger not in (b"manual\n", b"lock\n"):
        return 2
    stopping = Event()

    def watch_parent() -> None:
        try:
            os.read(fd, 1)
        finally:
            stopping.set()

    Thread(target=watch_parent, daemon=True).start()
    app = QCoreApplication([])
    worker = _Worker()
    try:
        if worker.start_capture(bytes(trigger), stopping):
            timer = QTimer()

            def check_stop() -> None:
                if stopping.is_set() or not worker.state_machine.is_patrolling:
                    app.quit()

            timer.timeout.connect(check_stop)
            timer.start(50)
            app.exec()
            timer.stop()
            _status("stopping")
    finally:
        worker.close_capture()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
