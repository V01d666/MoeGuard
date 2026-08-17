"""全局热键注册（Windows Win32 RegisterHotKey）。

D18 老板键/伪装模式：系统级全局热键，无论焦点在哪都生效。
在非 Windows 平台上回退到 QShortcut（仅应用内有焦点时生效）。

实现要点：
- RegisterHotKey 必须在 PeekMessageW 轮询线程内部调用
  （Win32 将 WM_HOTKEY 投递到注册线程的消息队列）。
- 注册失败（热键已占用）时自动回退到 QShortcut。

用法:
    hotkey = GlobalHotkey()
    hotkey.register("Ctrl+Shift+H")
    hotkey.activated.connect(callback)
    hotkey.unregister()
"""

from __future__ import annotations

import logging
import sys
from ctypes import wintypes

from PySide6.QtCore import QObject, Qt, Signal

logger = logging.getLogger(__name__)

# Win32 常量
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_WM_HOTKEY = 0x0312

# Qt 修饰键到 Win32 修饰键的映射
_QT_TO_WIN_MOD = {
    Qt.KeyboardModifier.ControlModifier: _MOD_CONTROL,
    Qt.KeyboardModifier.AltModifier: _MOD_ALT,
    Qt.KeyboardModifier.ShiftModifier: _MOD_SHIFT,
    Qt.KeyboardModifier.MetaModifier: _MOD_WIN,
}


class GlobalHotkey(QObject):
    """系统级全局热键（Windows RegisterHotKey）。

    RegisterHotKey 在后台轮询线程中调用，
    确保 WM_HOTKEY 消息投递到正确的线程消息队列。
    注册失败时自动回退到 QShortcut。
    """

    activated = Signal()
    registration_failed = Signal()  # 全局热键被占用，已回退 QShortcut
    fallback_requested = Signal(str)  # 从后台线程请求 GUI 线程创建 QShortcut

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._id: int | None = None
        self._key_seq: str = ""
        self._shortcut = None  # QShortcut fallback
        self._thread = None
        self._stop = False
        # QObject 本身属于 GUI 线程；保证 QShortcut 只在此线程创建。
        self.fallback_requested.connect(self._register_qshortcut)

    def register(self, sequence: str = "Ctrl+Shift+H", hotkey_id: int = 1) -> None:
        """注册全局热键。

        在 Windows 上启动后台线程调用 RegisterHotKey + PeekMessageW 轮询；
        失败或非 Windows 平台回退到 QShortcut（仅应用有焦点时生效）。

        Args:
            sequence: QKeySequence 字符串，如 "Ctrl+Shift+H"。
            hotkey_id: Win32 热键 ID（1~0xBFFF），用于注销时区分。
        """
        self._key_seq = sequence
        self._id = hotkey_id

        if sys.platform != "win32":
            logger.warning("非 Windows 平台，回退到 QShortcut")
            self._register_qshortcut(sequence)
            self.registration_failed.emit()
            return

        # 解析键序列
        from PySide6.QtGui import QKeySequence

        ks = QKeySequence(sequence)
        if ks.count() == 0:
            logger.warning("无效的快捷键序列 '%s'，回退到 QShortcut", sequence)
            self._register_qshortcut(sequence)
            self.registration_failed.emit()
            return

        key_comb = ks[0]
        qt_key = key_comb.key()
        qt_mods = key_comb.keyboardModifiers().value

        # 转换 Win32 修饰键
        win_mods = 0
        for qt_mod, win_mod in _QT_TO_WIN_MOD.items():
            if qt_mods & qt_mod.value:
                win_mods |= win_mod
        vk = int(qt_key) & 0xFF

        self._stop = False
        self._start_detection_thread(hotkey_id, win_mods, vk)

    def _register_qshortcut(self, sequence: str) -> None:
        """QShortcut 回退方案（仅应用内有焦点时生效）。"""
        from PySide6.QtGui import QKeySequence, QShortcut

        self._shortcut = QShortcut(QKeySequence(sequence), self.parent())
        self._shortcut.setContext(Qt.ApplicationShortcut)
        self._shortcut.activated.connect(self.activated.emit)
        logger.info("已注册 QShortcut 回退热键: %s", sequence)

    def _start_detection_thread(
        self, hotkey_id: int, win_mods: int, vk: int
    ) -> None:
        """启动后台线程：RegisterHotKey + PeekMessageW 轮询。

        RegisterHotKey 在此线程内调用，确保 WM_HOTKEY 投递到正确队列。
        """
        import threading

        def _detect_loop() -> None:
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]

            # 在线程内注册（修复：之前在主线程注册导致 WM_HOTKEY 投递到错误队列）
            result = user32.RegisterHotKey(None, hotkey_id, win_mods, vk)
            if not result:
                err = ctypes.get_last_error()
                logger.warning(
                    "RegisterHotKey 失败（错误码 %d），热键可能被占用。回退到 QShortcut",
                    err,
                )
                self._id = None
                self.fallback_requested.emit(self._key_seq)
                self.registration_failed.emit()
                return

            logger.info(
                "全局热键已注册（Win32 RegisterHotKey, id=%d, mods=0x%X, vk=0x%X）",
                hotkey_id, win_mods, vk,
            )

            msg = wintypes.MSG()
            while not self._stop and self._id is not None:
                if user32.PeekMessageW(
                    ctypes.byref(msg), None, _WM_HOTKEY, _WM_HOTKEY, 1,
                ):
                    if msg.message == _WM_HOTKEY and msg.wParam == self._id:
                        self.activated.emit()
                else:
                    import time
                    time.sleep(0.05)

        self._thread = threading.Thread(target=_detect_loop, daemon=True)
        self._thread.start()

    def unregister(self) -> None:
        """注销全局热键并停止轮询线程。"""
        self._stop = True
        self._id = None

        # 等待线程退出
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)

        if self._shortcut is not None:
            self._shortcut.deleteLater()
            self._shortcut = None
