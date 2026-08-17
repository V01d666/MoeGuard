"""系统托盘：常驻后台、快捷操作入口。

菜单：手动值守切换 / 完全免打扰开关 / 设置 / 查看证据 / 退出。
桌宠应用关闭窗口不退出，通过托盘常驻。

M4 增强：
- 绑定 QSystemTrayIcon.activated 信号，左键双击切换桌宠可见性。
- 新增 tray_activated 信号，供 app 层接线。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

logger = logging.getLogger(__name__)


class TrayIcon(QObject):
    """系统托盘图标与菜单。

    信号:
        toggle_patrol: True=进入手动值守, False=回到陪伴
        toggle_disturb_free: 完全免打扰开关（隐藏桌宠）
        open_settings: 打开设置面板
        open_evidence: 查看证据
        quit_requested: 退出应用
        tray_activated: 托盘图标被激活（QSystemTrayIcon.ActivationReason）
    """

    toggle_patrol = Signal(bool)  # True=进入手动值守, False=回到陪伴
    toggle_disturb_free = Signal(bool)  # 完全免打扰开关（隐藏桌宠）
    open_settings = Signal()
    open_security_setup = Signal()
    open_evidence = Signal()
    quit_requested = Signal()
    tray_activated = Signal(int)  # QSystemTrayIcon.ActivationReason

    def __init__(self, icon: QIcon, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(icon, parent)
        self._tray.setToolTip("萌卫 MoeGuard")

        # 绑定 activated 信号 -> 转发 tray_activated + 左键双击处理
        self._tray.activated.connect(self._on_activated)

        menu = QMenu(parent)

        self._act_patrol = QAction("进入值守", menu)
        self._act_companion = QAction("回到陪伴", menu)

        self._act_disturb_free = QAction("完全免打扰", menu)
        self._act_disturb_free.setCheckable(True)

        self._act_settings = QAction("设置…", menu)
        self._act_security_setup = QAction("值守设置（风险告知与主人注册）…", menu)
        self._act_evidence = QAction("查看本地证据…", menu)
        self._act_quit = QAction("退出萌卫", menu)

        # Qt 原生标准图标（不引入图标包），保持 Windows 原生 QMenu 观感。
        style = menu.style()
        sp = style.StandardPixmap
        self._act_patrol.setIcon(style.standardIcon(sp.SP_MediaPlay))
        self._act_companion.setIcon(style.standardIcon(sp.SP_MediaPause))
        self._act_settings.setIcon(style.standardIcon(sp.SP_FileDialogDetailedView))
        self._act_evidence.setIcon(style.standardIcon(sp.SP_DirOpenIcon))
        self._act_quit.setIcon(style.standardIcon(sp.SP_DialogCloseButton))

        self._act_patrol.triggered.connect(lambda: self.toggle_patrol.emit(True))
        self._act_companion.triggered.connect(lambda: self.toggle_patrol.emit(False))
        self._act_disturb_free.toggled.connect(self.toggle_disturb_free.emit)
        self._act_settings.triggered.connect(self.open_settings)
        self._act_security_setup.triggered.connect(self.open_security_setup)
        self._act_evidence.triggered.connect(self.open_evidence)
        self._act_quit.triggered.connect(self.quit_requested)

        menu.addAction(self._act_patrol)
        menu.addAction(self._act_companion)
        menu.addSeparator()
        menu.addAction(self._act_disturb_free)
        menu.addSeparator()
        menu.addAction(self._act_settings)
        menu.addAction(self._act_security_setup)
        menu.addAction(self._act_evidence)
        menu.addSeparator()
        menu.addAction(self._act_quit)

        self._tray.setContextMenu(menu)

    def _on_activated(self, reason) -> None:
        """托盘图标激活事件处理。

        QSystemTrayIcon.ActivationReason 枚举值:
          Trigger=0, DoubleClick=1, MiddleClick=2, Context=3, Unknown=4

        DoubleClick(1) 和 Trigger(0) -> 发射 tray_activated 信号。
        app 层根据 reason 决定是否切换桌宠可见性。
        """
        reason_int = reason.value
        logger.info(
            "托盘激活: reason=%d "
            "(0=Unknown 1=Context 2=DoubleClick 3=Trigger 4=MiddleClick)",
            reason_int,
        )
        self.tray_activated.emit(reason_int)

    def show(self) -> None:
        self._tray.show()

    def set_state(self, on_patrol: bool) -> None:
        """根据当前是否值守，启用/禁用对应菜单项。"""
        self._act_patrol.setEnabled(not on_patrol)
        self._act_companion.setEnabled(on_patrol)

    def show_message(
        self, title: str, message: str, msecs: int = 3000
    ) -> None:
        """显示托盘气泡通知。"""
        self._tray.showMessage(title, message, QSystemTrayIcon.Information, msecs)
