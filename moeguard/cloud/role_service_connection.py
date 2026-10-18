"""Background connection to the role service and the shared credit summary.

There is no "connect" step for the user.  One ``account_summary`` request both
proves the link and fetches the balance; it runs at startup, when a service
surface opens and the cached answer is stale, when the network comes back, and
after anything that changes the balance.  A failed attempt is retried only on
those events (plus a short bounded backoff where the OS cannot report network
changes), never on a timer of its own, so an offline user costs the service
nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from moeguard.cloud.role_service_binding import SessionTransport
from moeguard.cloud.role_service_http_client import (
    RoleServiceAccountSummary,
    RoleServiceConnectionError,
    RoleServiceHttpError,
)
from moeguard.cloud.role_service_session import RoleServiceSession

CREDIT_CACHE_SECONDS = 5 * 60
MIN_RETRY_SECONDS = 30
# Used only when Qt cannot watch reachability (no backend in this build).
FALLBACK_RETRY_SECONDS = (30, 120, 600)

STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_OFFLINE = "offline"


def account_summary_text(value: RoleServiceAccountSummary) -> str:
    flexible = value.available_flexible_units
    categorized = value.available_t2i_units + value.available_i2v_units + flexible
    if categorized < value.available_units:
        flexible += value.available_units - categorized
    text = f"立绘 {value.available_t2i_units} 次 · 动作 {value.available_i2v_units} 次"
    if flexible:
        text += f" · 通用 {flexible} 次"
    if value.reserved_units:
        text += f" · 任务占用 {value.reserved_units} 次"
    return text


class ServiceConnection(QObject):
    """One per app: the device session, link state and last known balance.

    ``changed`` carries ``(support ID, status line, offline)`` so plain Qt
    slots on any dialog can follow it; ``connected`` fires once, the first
    time any request (balance, mailbox, task) establishes the device session.
    """

    changed = Signal(str, str, bool)
    connected = Signal(object)
    _finished = Signal(object, object, int)
    _session_seen = Signal(object)

    def __init__(
        self,
        transport: SessionTransport,
        parent: QObject | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        watch_network: bool = True,
    ) -> None:
        super().__init__(parent)
        self.transport = transport
        self._clock = clock
        self.state = STATE_CONNECTING
        self.account_id = ""
        self.summary: RoleServiceAccountSummary | None = None
        self.error = ""
        self._busy = False
        self._generation = 0
        self._summary_at: float | None = None
        self._last_attempt: float | None = None
        self._announced = False
        self._started = False
        self._fallback_step = 0
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(lambda: self.refresh(force=True))
        self._finished.connect(self._complete)
        self._session_seen.connect(self._adopt_session)
        self._network = _reachability_backend() if watch_network else None
        if self._network is not None:
            # A bound slot of this QObject: Qt drops the connection when the
            # connection object is destroyed, so per-app instances never leak
            # into the process-wide reachability singleton.
            self._network.reachabilityChanged.connect(self._reachability_changed)
        transport._on_session = self._session_from_worker

    # -- public API -----------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def offline(self) -> bool:
        return self.state == STATE_OFFLINE

    @property
    def status(self) -> str:
        if self.state == STATE_CONNECTED:
            if self.summary is None:
                return "已连接"
            return f"已连接 · {account_summary_text(self.summary)}"
        if self.state == STATE_CONNECTING:
            return "正在连接服务…"
        return f"暂时连不上服务：{self.error}" if self.error else "暂时连不上服务"

    def start(self) -> None:
        """Begin once the app is ready; nothing touches the network before."""
        self._started = True
        self.refresh(force=True)

    def ensure_fresh(self) -> None:
        """Called when a surface that shows service state opens."""
        if self.state == STATE_CONNECTED and not self._summary_stale():
            return
        self.refresh(force=False)

    def retry(self) -> None:
        """The user pressed retry: always attempt, regardless of backoff."""
        self.refresh(force=True)

    def invalidate(self) -> None:
        """The balance changed (task finished, code redeemed): fetch it again."""
        self.refresh(force=True)

    def adopt_summary(self, summary: RoleServiceAccountSummary) -> None:
        """A redeem response already carries the new balance; no request."""
        self._started = True
        self._generation += 1
        self._busy = False
        self.summary = summary
        self._summary_at = self._clock()
        self.state = STATE_CONNECTED
        self.error = ""
        self._emit_changed()

    def _emit_changed(self) -> None:
        self.changed.emit(self.account_id, self.status, self.offline)

    def refresh(self, *, force: bool) -> None:
        if self._busy or not self._started:
            return
        now = self._clock()
        if (
            not force
            and self._last_attempt is not None
            and self.state != STATE_CONNECTED
            and now - self._last_attempt < MIN_RETRY_SECONDS
        ):
            return
        self._retry_timer.stop()
        self._busy = True
        self._last_attempt = now
        self._generation += 1
        generation = self._generation
        if self.state != STATE_CONNECTED:
            self.state = STATE_CONNECTING
        self._emit_changed()

        def work() -> None:
            try:
                result: object = self.transport.account_summary()
                failure: object = None
            except Exception as exc:  # noqa: BLE001 - sanitized below
                result, failure = None, exc
            try:
                self._finished.emit(result, failure, generation)
            except RuntimeError:
                pass  # The application exited while the request ran.

        threading.Thread(target=work, name="moeguard-service-connect", daemon=True).start()

    # -- internals ------------------------------------------------------

    def _summary_stale(self) -> bool:
        return (
            self._summary_at is None
            or self._clock() - self._summary_at >= CREDIT_CACHE_SECONDS
        )

    def _session_from_worker(self, session: RoleServiceSession) -> None:
        # Called on whichever thread made the request; hop to the GUI thread.
        try:
            self._session_seen.emit(session)
        except RuntimeError:
            pass

    @Slot(object)
    def _adopt_session(self, session: object) -> None:
        account_id = getattr(session, "account_id", "")
        if not account_id:
            return
        if account_id != self.account_id:
            self.account_id = account_id
            self._emit_changed()
        if not self._announced:
            self._announced = True
            self.connected.emit(session)

    @Slot(object, object, int)
    def _complete(self, result: object, failure: object, generation: int) -> None:
        if generation != self._generation:
            return
        self._busy = False
        if isinstance(result, RoleServiceAccountSummary):
            self.summary = result
            self._summary_at = self._clock()
            self.state = STATE_CONNECTED
            self.error = ""
            self._fallback_step = 0
            self._emit_changed()
            return
        self.state = STATE_OFFLINE
        self.error = _failure_text(failure)
        self._emit_changed()
        self._schedule_fallback_retry()

    def _schedule_fallback_retry(self) -> None:
        if self._network is not None:
            return  # The OS will tell us when the network comes back.
        if self._fallback_step >= len(FALLBACK_RETRY_SECONDS):
            return
        delay = FALLBACK_RETRY_SECONDS[self._fallback_step]
        self._fallback_step += 1
        self._retry_timer.start(delay * 1000)

    @Slot(object)
    def _reachability_changed(self, reachability: object) -> None:
        from PySide6.QtNetwork import QNetworkInformation

        if (
            reachability == QNetworkInformation.Reachability.Online
            and self.state == STATE_OFFLINE
        ):
            self.refresh(force=True)


def _failure_text(failure: object) -> str:
    if isinstance(failure, RoleServiceConnectionError):
        return "网络连不上生成服务"
    if isinstance(failure, RoleServiceHttpError):
        if failure.status == 429:
            return "请求较多，请稍后再试"
        if failure.status >= 500:
            return "生成服务暂时不可用"
        return "生成服务拒绝了这台电脑的连接"
    return "连接未完成"


def _reachability_backend() -> object | None:
    """The OS reachability watcher, when this Qt build ships a backend."""
    try:
        from PySide6.QtNetwork import QNetworkInformation
    except ImportError:
        return None
    try:
        if QNetworkInformation.instance() is None and not (
            QNetworkInformation.loadBackendByFeatures(
                QNetworkInformation.Feature.Reachability
            )
        ):
            return None
        return QNetworkInformation.instance()
    except (AttributeError, RuntimeError, TypeError):
        return None
