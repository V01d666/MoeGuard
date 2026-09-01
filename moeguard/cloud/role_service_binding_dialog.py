"""Small first-use dialog for anonymous role-service enrollment."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from moeguard.cloud.role_service_binding import RoleServiceBindingManager
from moeguard.cloud.role_service_http_client import role_service_user_message
from moeguard.cloud.role_service_session import RoleServiceSession
from moeguard.ui import theme


class _BindingThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, manager: RoleServiceBindingManager, service_origin: str) -> None:
        super().__init__()
        self._manager = manager
        self._service_origin = service_origin

    def run(self) -> None:
        try:
            session = self._manager.bind(self._service_origin)
        except Exception as exc:  # noqa: BLE001 - sanitized at the UI boundary
            message = role_service_user_message(exc)
            if message == str(exc):
                message = "绑定未完成，请稍后重试。已有桌宠不受影响。"
            self.failed.emit(message)
            return
        self.succeeded.emit(session)


class RoleServiceBindingDialog(QDialog):
    """Require an explicit click before creating an anonymous server account."""

    def __init__(
        self,
        manager: RoleServiceBindingManager,
        service_origin: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("roleServiceBindingDialog")
        self.setWindowTitle("连接角色生成服务")
        self.setMinimumWidth(430)
        self.setStyleSheet(theme.dialog_qss("roleServiceBindingDialog"))
        self._manager = manager
        self._service_origin = service_origin
        self._thread: _BindingThread | None = None
        self.session: RoleServiceSession | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        title = QLabel("准备好后，再连接角色生成服务")
        title.setProperty("role", "title")
        layout.addWidget(title)

        explanation = QLabel(
            "萌卫会为这台电脑创建一份随机匿名凭据，用于保存生成次数和恢复任务。"
            "不需要邮箱，也不会读取硬件序列号；凭据仅以 Windows 当前用户保护的形式保存在本机。"
        )
        explanation.setWordWrap(True)
        explanation.setProperty("role", "hint")
        layout.addWidget(explanation)

        self.status_label = QLabel("连接操作只会在你点击下方按钮后开始。")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.buttons = QDialogButtonBox()
        self.bind_button = QPushButton("连接服务")
        self.bind_button.setStyleSheet(theme.button_qss("accent"))
        self.cancel_button = QPushButton("暂不连接")
        self.buttons.addButton(self.bind_button, QDialogButtonBox.AcceptRole)
        self.buttons.addButton(self.cancel_button, QDialogButtonBox.RejectRole)
        self.bind_button.clicked.connect(self._start_binding)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.buttons)

    def _start_binding(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        self.bind_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.status_label.setText("正在建立匿名连接，请稍候…")
        thread = _BindingThread(self._manager, self._service_origin)
        thread.succeeded.connect(self._binding_succeeded)
        thread.failed.connect(self._binding_failed)
        thread.finished.connect(self._thread_finished)
        self._thread = thread
        thread.start()

    def _binding_succeeded(self, session: object) -> None:
        if not isinstance(session, RoleServiceSession):
            self._binding_failed("绑定未完成，请稍后重试。已有桌宠不受影响。")
            return
        self.session = session
        self.status_label.setText("连接成功，正在打开桌宠工坊…")
        thread = self._thread
        if thread is not None:
            thread.wait(1000)
        self.accept()

    def _binding_failed(self, message: str) -> None:
        self.status_label.setText(message)
        self.bind_button.setEnabled(True)
        self.cancel_button.setEnabled(True)

    def _thread_finished(self) -> None:
        thread = self._thread
        if thread is not None:
            thread.deleteLater()
        self._thread = None

    def reject(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        super().reject()
