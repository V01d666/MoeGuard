"""Opt-in self-update with a bundled source or explicit experiment override.

Check in the background, ask once, download in the background, and restart
only when the pet is idle -- never during patrol or a running workshop task.
Settings can trigger a manual retry; transient checks use bounded backoff.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox

from moeguard.updater import (
    FEED_ENV,
    UpdateError,
    UpdateOffer,
    Updater,
    bundled_feed,
    default_staging_dir,
)

logger = logging.getLogger(__name__)

DELAY_ENV = "MOEGUARD_UPDATE_CHECK_DELAY_SECONDS"
RETRY_ENV = "MOEGUARD_UPDATE_RETRY_SECONDS"
_DEFAULT_DELAY_SECONDS = 30
_DEFAULT_RETRY_SECONDS = 10 * 60


def _seconds(environ: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(environ.get(name, default))
    except ValueError:
        return default
    return value if 1 <= value <= 24 * 60 * 60 else default


def _background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="moeguard-update", daemon=True).start()


def _ask(offer: UpdateOffer) -> bool:
    megabytes = max(1, round(offer.download_bytes / (1024 * 1024)))
    box = QMessageBox()
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle("发现新版本")
    box.setText(
        f"萌卫 {offer.version} 可更新（约 {megabytes} MB）。\n"
        "下载后自动重启，桌宠和设置都会保留。"
    )
    update = box.addButton("更新", QMessageBox.AcceptRole)
    box.addButton("以后再说", QMessageBox.RejectRole)
    box.exec()
    return box.clickedButton() is update


class UpdateController(QObject):
    """Drive one check-ask-download-restart cycle per launch."""

    _checked = Signal(object)
    _downloaded = Signal(object)
    _failed = Signal(object)
    status_changed = Signal(str)

    def __init__(
        self,
        app: Any,
        updater: Updater,
        *,
        retry_ms: int = _DEFAULT_RETRY_SECONDS * 1000,
        ask: Callable[[UpdateOffer], bool] = _ask,
        runner: Callable[[Callable[[], None]], None] = _background,
        parent: QObject | None = None,
        network_retry_ms: tuple[int, ...] = (60_000, 300_000, 1_800_000),
        auto_check_enabled: bool = True,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._updater = updater
        self._retry_ms = retry_ms
        self._ask = ask
        self._runner = runner
        self._busy = False
        self._ready: UpdateOffer | None = None
        self._manual_requested = False
        self._network_retry_ms = network_retry_ms
        self._network_retries = 0
        self._auto_check_enabled = auto_check_enabled
        self._startup_delay_ms = _DEFAULT_DELAY_SECONDS * 1000
        self.status_text = "可手动检查萌卫更新。"
        self.state = "idle"
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.timeout.connect(self.check_now)
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.timeout.connect(self.apply_when_idle)
        self._checked.connect(self._on_checked)
        self._downloaded.connect(self._on_downloaded)
        self._failed.connect(self._on_failed)

    def start(self, delay_ms: int) -> None:
        self._startup_delay_ms = delay_ms
        try:
            failed_target = self._updater.consume_failed_apply()
        except UpdateError:
            logger.exception("could not recover the last update attempt")
            failed_target = "unknown"
        if failed_target:
            self.state = "apply_failed"
            self._set_status("上次更新未完成，当前版本可继续使用；解除占用后可手动重试。")
            # Configuration runs before the tray is created. Deliver the
            # notice after app.start(), when the event loop becomes ready.
            QTimer.singleShot(0, self._notify_failed_apply)
            return
        if self._auto_check_enabled:
            self._check_timer.start(delay_ms)
        else:
            self._set_status("自动检查已关闭，可手动检查更新。")

    def set_auto_check_enabled(self, enabled: bool) -> None:
        if enabled == self._auto_check_enabled:
            return
        self._auto_check_enabled = enabled
        if not enabled:
            if not self._manual_requested:
                self._check_timer.stop()
                if not self._busy and self.state in {"idle", "check_deferred", "retry_scheduled"}:
                    self.state = "idle"
                    self._set_status("自动检查已关闭，可手动检查更新。")
        elif not self._busy and self.state in {
            "idle", "check_deferred", "retry_scheduled", "failed"
        }:
            self._network_retries = 0
            self._check_timer.start(self._startup_delay_ms)

    def _set_status(self, message: str) -> None:
        self.status_text = message
        self.status_changed.emit(message)

    def _notify_failed_apply(self) -> None:
        self._notify(
            "上次更新未完成，已恢复原版本，本次启动不再提示更新。"
            "请关闭占用程序目录的窗口或程序，再到设置中点击检查更新重试。"
        )

    def _notify(self, message: str) -> None:
        notify = getattr(self._app, "notify_update", None)
        if callable(notify):
            notify("萌卫更新", message)

    def _run(self, work: Callable[[], Any], done: Signal) -> None:
        self._busy = True

        def body() -> None:
            try:
                result = work()
            except UpdateError as exc:
                self._failed.emit(exc)
                return
            except Exception:
                logger.exception("update step failed unexpectedly")
                self._failed.emit("unexpected failure")
                return
            done.emit(result)

        self._runner(body)

    def check_now(self, *, manual: bool = False) -> None:
        if not self._auto_check_enabled and not (manual or self._manual_requested):
            return
        if self._busy or self.state in {"prompting", "downloading", "apply_deferred", "applying"}:
            if manual:
                self._notify("更新正在进行，请稍候。")
            return
        if self.state == "apply_failed" and not manual:
            return
        self._check_timer.stop()
        if manual:
            self._network_retries = 0
        self._manual_requested = self._manual_requested or manual
        if self._app.update_blocker():
            self.state = "check_deferred"
            self._set_status("当前任务或值守结束后检查更新。")
            self._check_timer.start(self._retry_ms)
            return
        self.state = "checking"
        self._set_status("正在检查更新…")
        self._run(self._updater.check, self._checked)

    def _on_checked(self, offer: UpdateOffer | None) -> None:
        self._busy = False
        self._network_retries = 0
        if not self._auto_check_enabled and not self._manual_requested:
            self.state = "idle"
            self._set_status("自动检查已关闭，可手动检查更新。")
            return
        if offer is None:
            self.state = "up_to_date"
            self._set_status("当前已是最新版本。")
            if self._manual_requested:
                self._notify(self.status_text)
            self._manual_requested = False
            return
        self.state = "prompting"
        self._set_status(f"发现新版本 {offer.version}，等待确认。")
        if not self._ask(offer):
            self.state = "declined"
            self._set_status("已跳过本次更新，可随时手动检查。")
            self._manual_requested = False
            return
        self.state = "downloading"
        self._set_status(f"正在下载 {offer.version}…")

        def download() -> UpdateOffer:
            self._updater.download(offer)
            return offer

        self._run(download, self._downloaded)

    def _on_downloaded(self, offer: UpdateOffer) -> None:
        self._busy = False
        self._ready = offer
        self.apply_when_idle()

    def apply_when_idle(self) -> None:
        offer = self._ready
        if offer is None:
            return
        if self._app.update_blocker():
            if self.state != "apply_deferred":
                self._notify("新版本已下载，当前任务或值守结束后自动重启。")
            self.state = "apply_deferred"
            self._set_status("新版本已下载，当前任务或值守结束后自动重启。")
            self._apply_timer.start(self._retry_ms)
            return
        self._apply_timer.stop()
        self.state = "applying"
        self._set_status("正在安装更新，即将重启萌卫。")
        try:
            self._updater.apply_and_restart(offer)
        except Exception as exc:
            logger.exception("could not start the update installer")
            self._on_failed(exc if isinstance(exc, UpdateError) else "installer did not start")
            return
        self._app.quit()

    def _on_failed(self, reason: str | UpdateError) -> None:
        checking = self.state == "checking"
        self._busy = False
        self._ready = None
        self._apply_timer.stop()
        self.state = "failed"
        if checking and not self._auto_check_enabled and not self._manual_requested:
            self.state = "idle"
            self._set_status("自动检查已关闭，可手动检查更新。")
            return
        logger.warning("update failed: %s", reason)
        if checking and getattr(reason, "retryable", False):
            if self._auto_check_enabled and self._network_retries < len(self._network_retry_ms):
                delay = self._network_retry_ms[self._network_retries]
                self._network_retries += 1
                self.state = "retry_scheduled"
                self._check_timer.start(delay)
                self._set_status("暂时无法检查更新，将稍后重试；当前版本可继续使用。")
            else:
                self._set_status("暂时无法检查更新，可稍后点击检查更新重试。")
            if self._manual_requested:
                self._notify(self.status_text)
        else:
            detail = getattr(reason, "user_message", None)
            self._set_status(
                f"{detail} 当前版本可继续使用。" if detail
                else "更新失败，当前版本可继续使用；可点击检查更新重试。"
            )
            self._notify(self.status_text if detail else "更新失败，当前版本可继续使用。")
        self._manual_requested = False


def configure_self_update(
    app: Any,
    *,
    environ: Mapping[str, str] | None = None,
    updater_factory: Callable[..., Updater | None] = Updater.create,
    bundled_feed_path: Path | None = None,
) -> UpdateController | None:
    """Start the experimental updater when a feed is configured."""

    env = os.environ if environ is None else environ
    try:
        # An explicit empty override disables updates for the process. Normal
        # users receive the non-secret HTTPS source embedded by the builder.
        feed = env[FEED_ENV].strip() if FEED_ENV in env else bundled_feed(bundled_feed_path)
        if not feed:
            return None
        updater = updater_factory(feed, staging_dir=default_staging_dir())
    except UpdateError:
        logger.warning("update feed setting is invalid", exc_info=True)
        return None
    if updater is None:
        return None
    config = getattr(app, "_config", None)
    preferences = getattr(config, "updates", None)
    controller = UpdateController(
        app,
        updater,
        retry_ms=_seconds(env, RETRY_ENV, _DEFAULT_RETRY_SECONDS) * 1000,
        auto_check_enabled=preferences.auto_check_enabled if preferences is not None else True,
    )
    controller.start(_seconds(env, DELAY_ENV, _DEFAULT_DELAY_SECONDS) * 1000)
    return controller
