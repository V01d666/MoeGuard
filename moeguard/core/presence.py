"""离座检测（系统事件）：键鼠空闲 / 屏幕锁屏 -> 自动值守。

陪伴模式摄像头关闭，离座检测**不依赖摄像头**，改用系统级事件，
彻底消除「被监控感」。仅在用户开启「系统事件自动值守」时启用。

值守中主人回座由安防层摄像头人脸识别判定（非本模块职责）。

锁屏/解锁监听：
- Windows: WTSRegisterSessionNotification + WM_WTSSESSION_CHANGE 消息
- macOS（预留）: NSDistributedNotificationCenter 监听
  com.apple.screenIsLocked / com.apple.screenIsUnlocked
- Linux/其他: 暂不支持，install() 优雅 no-op + warning
"""

from __future__ import annotations

import logging
import sys
import time

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, QTimer, Signal

from moeguard.config import PresenceConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Windows 锁屏/解锁消息常量
# ---------------------------------------------------------------------------
_WM_WTSSESSION_CHANGE = 0x02B1  # Windows 消息: 会话状态变化
_WTS_SESSION_LOCK = 0x7  # 会话锁定（锁屏）
_WTS_SESSION_UNLOCK = 0x8  # 会话解锁
# WTSRegisterSessionNotification 选项
_NOTIFY_FOR_THIS_SESSION = 0x0


class _WinSessionFilter(QAbstractNativeEventFilter):
    """Windows 会话事件过滤器：捕获 WM_WTSSESSION_CHANGE 消息。

    PySide6 的 nativeEventFilter 接收 (eventType, message)。
    在 Windows 上 message 是一个指向 MSG 结构体的 sip.voidptr / int。
    我们需要从中解析出 message 字段（WM_*）和 wParam。
    """

    def __init__(self, monitor: LockScreenMonitor) -> None:
        super().__init__()
        self._monitor = monitor

    def nativeEventFilter(self, eventType: bytes, message) -> bool:  # type: ignore[override]  # noqa: N802
        """过滤原生事件，捕获 WM_WTSSESSION_CHANGE。

        返回 True 表示拦截该消息，False 表示继续传递。
        """
        try:
            # eventType 在 Windows 上为 b"windows_generic_MSG"
            # message 是 MSG 结构体指针（sip.voidptr 或 int）
            import ctypes

            # 将 message 解释为 MSG 结构体指针
            # MSG: { HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam,
            #        DWORD time, POINT pt }
            class _MSG(ctypes.Structure):
                _fields_ = [
                    ("hwnd", ctypes.c_void_p),
                    ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_size_t),
                    ("lParam", ctypes.c_ssize_t),
                    ("time", ctypes.c_uint),
                    ("pt_x", ctypes.c_long),
                    ("pt_y", ctypes.c_long),
                ]

            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
            if msg.message == _WM_WTSSESSION_CHANGE:
                if msg.wParam == _WTS_SESSION_LOCK:
                    logger.debug("WTS_SESSION_LOCK 事件")
                    self._monitor.screen_locked.emit()
                elif msg.wParam == _WTS_SESSION_UNLOCK:
                    logger.debug("WTS_SESSION_UNLOCK 事件")
                    self._monitor.screen_unlocked.emit()
        except Exception:
            logger.exception("解析 Windows 会话消息失败")
        return False  # 不拦截，继续传递


class LockScreenMonitor(QObject):
    """锁屏/解锁事件监听器。

    平台支持：
    - Windows: WTSRegisterSessionNotification + WM_WTSSESSION_CHANGE
    - macOS: 预留（NSDistributedNotificationCenter），暂未实现
    - Linux/其他: 不支持，install() 优雅 no-op

    信号：
    - screen_locked:   屏幕锁定（进入值守触发）
    - screen_unlocked: 屏幕解锁（退出值守触发，D15 权威退出信号）
    """

    screen_locked = Signal()
    screen_unlocked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._filter: QAbstractNativeEventFilter | None = None
        self._installed = False
        self._is_windows = sys.platform == "win32"
        self._is_macos = sys.platform == "darwin"
        self._wts_registered = False

    def install(self, app: QObject) -> None:
        """安装锁屏/解锁监听。

        在 Windows 上通过 QAbstractNativeEventFilter 捕获
        WM_WTSSESSION_CHANGE 消息，并调用 WTSRegisterSessionNotification
        注册会话通知。

        在非 Windows 平台上优雅 no-op + warning。
        """
        if self._installed:
            logger.warning("LockScreenMonitor 已安装，重复调用忽略")
            return

        if self._is_windows:
            self._install_windows(app)
        elif self._is_macos:
            self._install_macos(app)
        else:
            logger.warning(
                "锁屏/解锁监听不支持平台 %s，install() no-op；"
                "自动值守将仅依赖键鼠空闲检测",
                sys.platform,
            )

        self._installed = True

    def _install_windows(self, app: QObject) -> None:
        """Windows 平台安装：注册 WTS 通知 + 事件过滤器。"""
        try:
            import ctypes

            wtsapi32 = ctypes.windll.wtsapi32  # type: ignore[attr-defined]

            # WTSRegisterSessionNotification(HWND, NOTIFY_FOR_THIS_SESSION)
            # 需要一个有效的 HWND 来接收 WM_WTSSESSION_CHANGE 消息。
            # 对于 QApplication，使用其顶层窗口的 winId，或者
            # 创建一个隐藏的接收窗口。
            # NOTIFY_FOR_THIS_SESSION = 0，仅接收当前会话事件。
            hwnd = 0  # NULL: 系统向调用线程的消息队列发送通知
            # 部分系统上 NULL 不被接受，尝试使用有效的 top-level HWND
            from PySide6.QtWidgets import QWidget
            tl_widgets = app.topLevelWidgets()
            if tl_widgets:
                hwnd = int(tl_widgets[0].winId())
            else:
                # 没有顶层 widget：创建一个隐藏接收窗口获取有效 HWND
                dummy = QWidget()
                hwnd = int(dummy.winId())

            result = wtsapi32.WTSRegisterSessionNotification(
                hwnd, _NOTIFY_FOR_THIS_SESSION
            )
            if not result:
                err = ctypes.get_last_error()
                # 错误码 0 在某些 Windows 版本上实际表示成功
                if err == 0:
                    logger.info("WTSRegisterSessionNotification 已注册（hwnd=%d）", hwnd)
                    self._wts_registered = True
                else:
                    logger.error(
                        "WTSRegisterSessionNotification 失败（错误码 %d），"
                        "锁屏值守将不可用，手动值守仍可正常使用", err
                    )
                    # 不 return，继续安装事件过滤器以便可能捕获消息
            else:
                self._wts_registered = True
                logger.info("Windows 锁屏/解锁监听已安装（hwnd=%d）", hwnd)

            # 安装原生事件过滤器（即使 WTS 注册失败也安装，以便可能捕获消息）
            self._filter = _WinSessionFilter(self)
            app.installNativeEventFilter(self._filter)

        except (AttributeError, OSError, ImportError) as exc:
            logger.error("Windows 锁屏监听安装失败: %s", exc)

    def _install_macos(self, app: QObject) -> None:
        """macOS 平台安装（预留，暂未实现）。

        应使用 NSDistributedNotificationCenter 监听：
        - com.apple.screenIsLocked
        - com.apple.screenIsUnlocked

        可通过 pyobjc（AppKit）或 ctypes 调用 CoreFoundation 实现。
        """
        logger.warning(
            "macOS 锁屏/解锁监听暂未实现，install() no-op；"
            "应使用 NSDistributedNotificationCenter 监听 "
            "com.apple.screenIsLocked / com.apple.screenIsUnlocked"
        )

    def stop(self) -> None:
        """停止监听并清理资源。"""
        if self._is_windows and self._wts_registered:
            try:
                import ctypes

                wtsapi32 = ctypes.windll.wtsapi32  # type: ignore[attr-defined]
                hwnd = 0xFFFF
                wtsapi32.WTSUnRegisterSessionNotification(hwnd)
            except (AttributeError, OSError, ImportError) as exc:
                logger.warning("WTSUnRegisterSessionNotification 失败: %s", exc)
            self._wts_registered = False

        if self._filter is not None:
            # QAbstractNativeEventFilter 由 app 管理，
            # 无需显式 remove（Qt 会在 app 析构时清理），
            # 但我们置空引用以便 GC
            self._filter = None

        self._installed = False
        logger.debug("LockScreenMonitor 已停止")


class PresenceDetector(QObject):
    """基于键鼠空闲的离座检测器（自动值守用）。

    安装到 QApplication 的事件过滤器上，记录最后活动时间；
    定时器检查空闲时长达阈值则发出 owner_away；检测到活动恢复
    则发出 owner_present。

    注意：这是键鼠空闲检测，与 LockScreenMonitor 互补。
    - LockScreenMonitor: 锁屏/解锁（系统级，即时触发）
    - PresenceDetector:  键鼠空闲（延迟触发，补充场景）
    """

    owner_present = Signal()  # 检测到用户活动恢复
    owner_away = Signal()  # 键鼠空闲达阈值

    _ACTIVITY_EVENTS = (
        QEvent.MouseButtonPress,
        QEvent.MouseButtonRelease,
        QEvent.MouseMove,
        QEvent.KeyPress,
        QEvent.Wheel,
    )

    def __init__(self, config: PresenceConfig) -> None:
        super().__init__()
        self._idle_threshold = float(config.idle_threshold_sec)
        self._last_activity: float = time.time()
        self._is_present: bool = True
        self._timer = QTimer(self)
        self._timer.setInterval(1000)  # 每秒检查一次空闲
        self._timer.timeout.connect(self._tick)
        self._app: QObject | None = None

    def install(self, app: QObject) -> None:
        """在 QApplication 上安装事件过滤器并启动检测。"""
        self._app = app
        app.installEventFilter(self)
        self._last_activity = time.time()
        self._timer.start()

    def stop(self) -> None:
        """停止检测并清理事件过滤器。"""
        self._timer.stop()
        if self._app is not None:
            self._app.removeEventFilter(self)
            self._app = None

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() in self._ACTIVITY_EVENTS:
            self._last_activity = time.time()
            if not self._is_present:
                self._is_present = True
                self.owner_present.emit()
        return False  # 不拦截事件

    def _tick(self) -> None:
        if self._is_present and (time.time() - self._last_activity) >= self._idle_threshold:
            self._is_present = False
            self.owner_away.emit()

    def reset(self) -> None:
        """重置（如手动切换状态后）。"""
        self._last_activity = time.time()
        self._is_present = True
