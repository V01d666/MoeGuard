"""Plain-text mailbox UI; message content lives only in this window's memory."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

from moeguard.cloud.role_service_http_client import RoleServiceHttpError
from moeguard.cloud.role_service_mailbox_client import (
    ANNOUNCEMENT_ID_RE,
    MailboxReadStore,
    mailbox_diagnostics,
    read_fingerprint,
    unread_mail,
)
from moeguard.cloud.role_service_mailbox_contract import (
    MAX_MESSAGE_CHARS,
    MailboxDiagnostics,
    MailboxMessageRequest,
)
from moeguard.ui import theme
from moeguard.utils.version import installed_version


def _error_text(error: Exception) -> str:
    # Never reflect the HTTP message, request text, token or local path.
    if isinstance(error, RoleServiceHttpError):
        if error.status == 429:
            return "留言或刷新频率达到上限，请稍后再试。"
        if error.status in {401, 403}:
            return "匿名连接暂时不可用，请稍后刷新。"
        if error.status == 404:
            return "此服务暂未提供信箱功能，本地桌宠可继续使用。"
    return "暂时无法连接信箱，请稍后刷新；本地桌宠可继续使用。"


class _MailJob(QObject):
    done = Signal(object)

    def start(self, work: Callable[[], object]) -> None:
        def run() -> None:
            try:
                result = work()
            except Exception as error:
                result = _error_text(error)
            try:
                self.done.emit(result)
            except RuntimeError:
                # The window/application may have closed during an HTTP call.
                pass

        threading.Thread(target=run, name="moeguard-mailbox", daemon=True).start()


class MailboxDialog(QDialog):
    unread_changed = Signal(int)

    def __init__(
        self,
        public_transport: Any | None,
        transport: Any | None,
        read_store: MailboxReadStore,
        *,
        account_id: str = "",
        diagnostics: Callable[[], MailboxDiagnostics] = mailbox_diagnostics,
        confirm_send: Callable[[MailboxMessageRequest], bool] | None = None,
        clock: Callable[[], float] = time.time,
        auto_refresh: bool = True,
        on_announcement_viewed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._public = public_transport
        self._transport = transport
        self._read_store = read_store
        self._account_id = account_id
        self._diagnostics = diagnostics
        self._confirm_send = confirm_send or self._preview_send
        self._clock = clock
        self._job: _MailJob | None = None
        self._refresh_after_connection = False
        self._pending: MailboxMessageRequest | None = None
        self._previewing = False
        self._remaining: int | None = None
        self._rows: dict[str, tuple[str, str, str]] = {}
        self._seen: frozenset[str] = frozenset()
        # Read receipts: row key -> server announcement ID (receipt-safe only).
        self._announcement_ids: dict[str, str] = {}
        self._on_announcement_viewed = on_announcement_viewed
        self.setObjectName("mailboxDialog")
        self.setWindowTitle("信箱 · 萌卫")
        self.resize(700, 650)
        self.setMinimumSize(540, 540)
        self.setStyleSheet(theme.dialog_qss("mailboxDialog"))
        root = QVBoxLayout(self)
        inbox_card = QFrame()
        inbox_card.setProperty("flatCard", True)
        inbox_layout = QVBoxLayout(inbox_card)
        inbox_layout.setContentsMargins(*theme.CARD_MARGINS)
        root.addWidget(inbox_card, 1)
        bar = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        bar.addWidget(self.status, 1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        bar.addWidget(self.refresh_button)
        inbox_layout.addLayout(bar)
        self.tabs = QTabWidget()
        self.inbox = QListWidget()
        self.announcements = QListWidget()
        self.tabs.addTab(self.inbox, "留言与回复")
        self.tabs.addTab(self.announcements, "公告")
        self.inbox.currentItemChanged.connect(self._select_item)
        self.announcements.currentItemChanged.connect(self._select_item)
        self.tabs.currentChanged.connect(self._show_current_tab)
        inbox_layout.addWidget(self.tabs, 1)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选中一条查看全文")
        self.detail.setMaximumHeight(140)
        inbox_layout.addWidget(self.detail)
        read_bar = QHBoxLayout()
        self.mark_all_button = QPushButton("全部标为已读")
        self.mark_all_button.clicked.connect(self._mark_all)
        read_bar.addWidget(self.mark_all_button)
        read_bar.addStretch(1)
        # Rarely needed (privacy clean-up / recovery): right-aligned and quiet.
        self.clear_read_button = QPushButton("清除已读记录")
        self.clear_read_button.setFlat(True)
        self.clear_read_button.setStyleSheet(
            f"QPushButton {{ color: {theme.TEXT_SECONDARY}; border: none; padding: 4px 6px; }}"
            f"QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; }}"
        )
        self.clear_read_button.clicked.connect(self._clear_read)
        read_bar.addWidget(self.clear_read_button)
        inbox_layout.addLayout(read_bar)
        compose_card = QFrame()
        compose_card.setProperty("flatCard", True)
        compose_layout = QVBoxLayout(compose_card)
        compose_layout.setContentsMargins(*theme.CARD_MARGINS)
        root.addWidget(compose_card)
        compose_layout.addWidget(QLabel("写留言"))
        notice = QLabel("请勿填写身份信息或密钥；关闭窗口会丢弃未发送内容。")
        notice.setWordWrap(True)
        compose_layout.addWidget(notice)
        self.composer = QPlainTextEdit()
        self.composer.setPlaceholderText("问题或建议（纯文字，最多 500 字）")
        self.composer.setMaximumHeight(120)
        self.composer.textChanged.connect(self._update_send_state)
        compose_layout.addWidget(self.composer)
        send_bar = QHBoxLayout()
        self.attach_diagnostics = QCheckBox("附带诊断（可选）")
        self.attach_diagnostics.setChecked(False)
        self.attach_diagnostics.setToolTip(
            "仅版本、Windows 主版本、架构和近期错误码，发送前可预览。"
        )
        send_bar.addWidget(self.attach_diagnostics)
        send_bar.addStretch(1)
        # The count belongs to the text and the send button, not the checkbox.
        self.counter = QLabel("0 / 500")
        send_bar.addWidget(self.counter)
        self.send_button = QPushButton("预览并发送")
        self.send_button.setStyleSheet(theme.button_qss("accent"))
        self.send_button.clicked.connect(self.send_message)
        send_bar.addWidget(self.send_button)
        compose_layout.addLayout(send_bar)
        if public_transport is None and transport is None:
            self.status.setText("此候选版尚未配置信箱服务，本地桌宠可继续使用。")
            self.refresh_button.setEnabled(False)
        elif transport is None:
            self.status.setText("正在连接服务；连接后可留言和接收回复。")
        self._update_send_state()
        if auto_refresh and (public_transport is not None or transport is not None):
            self.refresh()

    def has_unsent_message(self) -> bool:
        return bool(self.composer.toPlainText().strip()) or self._job is not None

    def set_connection(self, transport: Any, account_id: str) -> None:
        self._transport = transport
        self._account_id = account_id
        self._update_send_state()
        if self._job is not None:
            self._refresh_after_connection = True
        else:
            self.refresh()

    def _update_send_state(self) -> None:
        size = len(self.composer.toPlainText())
        self.counter.setText(f"{size} / {MAX_MESSAGE_CHARS}")
        self.send_button.setEnabled(
            self._transport is not None
            and self._job is None and not self._previewing
            and 0 < size <= MAX_MESSAGE_CHARS
            and self._remaining != 0
        )

    def _begin(self, work: Callable[[], object], callback: Callable[[object], None]) -> None:
        if self._job is not None:
            return
        job = _MailJob(self)
        self._job = job
        self.refresh_button.setEnabled(False)
        self.composer.setReadOnly(True)
        self.attach_diagnostics.setEnabled(False)
        self._update_send_state()
        job.done.connect(callback)
        job.start(work)

    def _finish(self) -> None:
        if self._job is not None:
            self._job.deleteLater()
            self._job = None
        self.refresh_button.setEnabled(self._public is not None or self._transport is not None)
        self.composer.setReadOnly(False)
        self.attach_diagnostics.setEnabled(True)
        self._update_send_state()

    def refresh(self) -> None:
        if self._job is not None or (self._public is None and self._transport is None):
            return
        self.status.setText("正在刷新…")
        public, transport = self._public, self._transport

        def load() -> object:
            result: dict[str, Any] = {"announcements": None, "thread": None, "errors": []}
            for key, source, method in (
                ("announcements", public, "announcements"),
                ("thread", transport, "mailbox_thread"),
            ):
                if source is not None:
                    try:
                        result[key] = getattr(source, method)()
                    except Exception as error:
                        result["errors"].append(_error_text(error))
            return result

        self._begin(load, self._loaded)

    @Slot(object)
    def _loaded(self, result: object) -> None:
        self._finish()
        if not isinstance(result, dict):
            self.status.setText(str(result))
            return
        try:
            self._seen = self._read_store.seen()
        except (OSError, ValueError):
            self._seen = frozenset()
            result["errors"].append("本机已读记录不可用，可清除后重试。")
        announcements = result["announcements"]
        thread = result["thread"]
        # Failed refreshes keep the prior in-memory view and never mark it read.
        if announcements is not None:
            self._replace_rows(self.announcements)
            now = int(self._clock())
            for item in announcements:
                if (
                    item.applies_to(installed_version())
                    and item.published_at <= now < item.expires_at
                ):
                    key = read_fingerprint("announcements", item.announcement_id, item.published_at)
                    if ANNOUNCEMENT_ID_RE.fullmatch(item.announcement_id):
                        self._announcement_ids[key] = item.announcement_id
                    self._add_row(self.announcements, key, item.title, item.body, True)
        if thread is not None:
            self._replace_rows(self.inbox)
            self._remaining = thread.remaining_today
            for item in thread.messages:
                key = read_fingerprint(self._account_id, item.message_id, item.created_at)
                operator = item.direction == "operator"
                stamp = time.strftime("%m-%d %H:%M", time.localtime(item.created_at))
                title = ("作者回复" if operator else "我的留言") + f" · {stamp}"
                self._add_row(self.inbox, key, title, item.body, operator)
        self._refresh_unread()
        if self._refresh_after_connection:
            self._refresh_after_connection = False
            QTimer.singleShot(0, self.refresh)
        quota = "" if self._remaining is None else f"今日还可留言 {self._remaining} 次。"
        self.status.setText(" ".join(result["errors"]) or ("已刷新。" + quota))
        self._update_send_state()

    def _replace_rows(self, widget: QListWidget) -> None:
        for index in range(widget.count()):
            self._rows.pop(widget.item(index).data(Qt.UserRole), None)
        widget.clear()

    def _add_row(self, widget: QListWidget, key: str, title: str, body: str, unread: bool) -> None:
        self._rows[key] = (title, body, "incoming" if unread else "outgoing")
        row = QListWidgetItem(title)
        row.setData(Qt.UserRole, key)
        widget.addItem(row)

    def _refresh_unread(self) -> None:
        total = 0
        for index, widget in enumerate((self.inbox, self.announcements)):
            count = 0
            for position in range(widget.count()):
                row = widget.item(position)
                key = row.data(Qt.UserRole)
                title, _body, direction = self._rows[key]
                unread = direction == "incoming" and key not in self._seen
                count += int(unread)
                # Unread is shown by weight, not by a text prefix.
                font = row.font()
                font.setBold(unread)
                row.setFont(font)
                row.setText(title)
            label = "留言与回复" if index == 0 else "公告"
            self.tabs.setTabText(index, label + (f" ({count})" if count else ""))
            total += count
        self.unread_changed.emit(total)

    def _mark(self, keys: tuple[str, ...]) -> None:
        try:
            self._read_store.mark_read(keys)
            self._seen = self._read_store.seen()
        except (OSError, ValueError):
            self.status.setText("无法保存已读记录；可清除记录后重试。")
            return
        self._refresh_unread()

    def _select_item(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        if current is not None:
            key = current.data(Qt.UserRole)
            if key in self._rows:
                self.detail.setPlainText(self._rows[key][1])
                if self._rows[key][2] == "incoming":
                    first_view = key not in self._seen
                    self._mark((key,))
                    announcement_id = self._announcement_ids.get(key, "")
                    if first_view and announcement_id and self._on_announcement_viewed:
                        try:
                            self._on_announcement_viewed(announcement_id)
                        except Exception:  # noqa: BLE001 - a receipt never breaks reading
                            pass

    def _mark_all(self) -> None:
        self._mark(tuple(self._rows))

    def _show_current_tab(self, index: int) -> None:
        widget = self.inbox if index == 0 else self.announcements
        current = widget.currentItem()
        if current is None:
            self.detail.clear()
        else:
            self._select_item(current, None)

    def _clear_read(self) -> None:
        try:
            self._read_store.clear()
        except OSError:
            self.status.setText("无法清除已读记录，请稍后重试。")
            return
        self._seen = frozenset()
        self._refresh_unread()

    def _preview_send(self, request: MailboxMessageRequest) -> bool:
        preview = QDialog(self)
        preview.setWindowTitle("发送前预览")
        preview.resize(560, 420)
        layout = QVBoxLayout(preview)
        layout.addWidget(QLabel("以下内容会发送给作者；取消不会发送。"))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        detail = request.body
        if request.diagnostics is not None:
            data = request.diagnostics
            detail += (
                f"\n\n诊断：版本 {data.app_version}；Windows {data.windows_major}；"
                f"架构 {data.architecture}；错误码 {', '.join(data.recent_error_codes) or '无'}"
            )
        text.setPlainText(detail)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("发送")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(preview.accept)
        buttons.rejected.connect(preview.reject)
        layout.addWidget(buttons)
        return preview.exec() == QDialog.Accepted

    def send_message(self) -> None:
        if self._job is not None or self._transport is None or self._previewing:
            return
        try:
            candidate = MailboxMessageRequest.from_dict(
                {
                    "schema_version": 1,
                    "client_message_id": "cm_" + uuid.uuid4().hex,
                    "body": self.composer.toPlainText(),
                    **(
                        {"diagnostics": self._diagnostics().to_dict()}
                        if self.attach_diagnostics.isChecked()
                        else {}
                    ),
                }
            )
        except ValueError:
            self.status.setText("请填写 1–500 字纯文字；诊断不能包含路径或其他资料。")
            return
        if (
            self._pending is not None
            and self._pending.body == candidate.body
            and self._pending.diagnostics == candidate.diagnostics
        ):
            candidate = self._pending
        self._previewing = True
        self._update_send_state()
        try:
            confirmed = self._confirm_send(candidate)
        finally:
            self._previewing = False
            self._update_send_state()
        if not confirmed:
            return
        self._pending = candidate
        transport = self._transport
        self.status.setText("正在发送…")
        self._begin(lambda: transport.post_mailbox_message(candidate), self._sent)

    @Slot(object)
    def _sent(self, result: object) -> None:
        self._finish()
        if isinstance(result, str):
            self.status.setText(result)
            return
        self._pending = None
        self._remaining = result.remaining_today
        self.composer.clear()
        self.attach_diagnostics.setChecked(False)
        self.status.setText(f"已发送，今日还可留言 {self._remaining} 次。")
        self.refresh()


class MailboxWatcher(QObject):
    """Background check for unread announcements and author replies (v0.4.1).

    Runs at startup and every ``period_ms``; reads only the local read store
    and never marks anything read.  ``new_mail`` fires once per item, using a
    second marker store, so the tray notice is not repeated on every check.
    Failures are silent: the mailbox itself still works when opened.
    """

    unread_changed = Signal(int)
    new_mail = Signal(object)  # tuple[UnreadMail, ...]

    def __init__(
        self,
        public_transport: Any | None,
        read_store: MailboxReadStore,
        notified_store: MailboxReadStore,
        *,
        thread_source: Callable[[], tuple[Any | None, str]] = lambda: (None, ""),
        clock: Callable[[], float] = time.time,
        version: Callable[[], str] = installed_version,
        min_interval_s: int = 600,
        period_ms: int = 6 * 60 * 60 * 1000,
    ) -> None:
        super().__init__()
        self._public = public_transport
        self._read_store = read_store
        self._notified_store = notified_store
        self._thread_source = thread_source
        self._clock = clock
        self._version = version
        self._min_interval_s = min_interval_s
        self._last_check: float | None = None
        self._job: _MailJob | None = None
        self._job_account_id = ""
        self.has_thread = False
        self._timer = QTimer(self)
        self._timer.setInterval(period_ms)
        self._timer.timeout.connect(lambda: self.check(force=True))

    def start(self) -> None:
        self.check(force=True)
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def check(self, *, force: bool = False) -> bool:
        """Start one background fetch; False when skipped (busy or too soon)."""
        if self._job is not None:
            return False
        now = self._clock()
        if (
            not force
            and self._last_check is not None
            and now - self._last_check < self._min_interval_s
        ):
            return False
        try:
            transport, account_id = self._thread_source()
        except Exception:  # noqa: BLE001
            transport, account_id = None, ""
        if not account_id:
            transport = None
        public = self._public
        if public is None and transport is None:
            return False
        self._last_check = now

        def load() -> object:
            result: dict[str, Any] = {"announcements": None, "thread": None}
            for key, source, method in (
                ("announcements", public, "announcements"),
                ("thread", transport, "mailbox_thread"),
            ):
                if source is not None:
                    try:
                        result[key] = getattr(source, method)()
                    except Exception:  # noqa: BLE001 - offline is normal
                        pass
            return result

        job = _MailJob(self)
        self._job = job
        self._job_account_id = account_id
        # A bound slot (not a lambda): the worker thread's emit is queued to
        # this object's thread, like MailboxDialog._loaded.
        job.done.connect(self._loaded)
        job.start(load)
        return True

    @Slot(object)
    def _loaded(self, result: object) -> None:
        account_id = self._job_account_id
        if self._job is not None:
            self._job.deleteLater()
            self._job = None
        if not isinstance(result, dict):
            return
        if result.get("announcements") is None and result.get("thread") is None:
            return
        if result.get("thread") is not None:
            self.has_thread = True
        try:
            seen = self._read_store.seen()
        except (OSError, ValueError):
            seen = frozenset()
        items = unread_mail(
            result.get("announcements"),
            result.get("thread"),
            seen,
            account_id=account_id,
            version=self._version(),
            now=int(self._clock()),
        )
        self.unread_changed.emit(len(items))
        try:
            notified = self._notified_store.seen()
        except (OSError, ValueError):
            notified = frozenset()
        fresh = tuple(item for item in items if item.key not in notified)
        if not fresh:
            return
        try:
            self._notified_store.mark_read(item.key for item in fresh)
        except (OSError, ValueError):
            pass  # worst case the notice repeats at the next check
        self.new_mail.emit(fresh)
