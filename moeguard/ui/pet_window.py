'桌宠主窗口：无边框、透明背景、置顶。'

from __future__ import annotations

import logging
from collections import OrderedDict

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap, QRegion, QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from moeguard.pet.frame_animation import FrameAnimationController
from moeguard.ui import theme

logger = logging.getLogger(__name__)

_EDGE_SNAP_MARGIN = 20
_DEFAULT_WIDTH = 200
_DEFAULT_HEIGHT = 300
_MIN_WIDTH = 100
_MIN_HEIGHT = 150
_MAX_WIDTH = 512
_MAX_HEIGHT = 768
_DEFAULT_FPS = 6
_BUBBLE_TIMEOUT_MS = 5000
_BUBBLE_CONTENT_GAP = 24
_SCREEN_EDGE_HEAD_REVEAL_PIXELS = 64
_STEALTH_KEY = "Ctrl+Shift+H"
_RENDER_CACHE_MAX_FRAMES = 32
_RENDER_CACHE_MAX_BYTES = 32 * 1024 * 1024


class _MessageBubble(QLabel):
    """Transparent top-level label with an explicitly painted rounded panel.

    On Windows a translucent top-level ``QLabel`` does not reliably retain its
    QSS background. Painting the panel ourselves keeps the native window fully
    transparent outside the rounded shape while leaving text layout to QLabel.
    """

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(*theme.BUBBLE_BORDER_RGBA), 1))
        painter.setBrush(QColor(*theme.BUBBLE_BG_RGBA))
        panel = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.drawRoundedRect(
            panel,
            theme.RADIUS_LARGE,
            theme.RADIUS_LARGE,
        )
        painter.end()
        super().paintEvent(event)


def edge_snap_position(
    surface: QRect,
    window_rect: QRect,
    content_rect: QRect,
    direction: str,
    reveal_fraction: float,
    *,
    reveal_pixels: int | None = None,
) -> QPoint:
    """Place visible character content against any rectangular interaction surface.

    The current caller passes the display work area. Future window-edge support
    can pass an application rectangle without changing this geometry contract.
    """
    if direction not in {"left", "right", "top", "bottom"}:
        raise ValueError(f"unknown edge direction: {direction}")
    content = content_rect if not content_rect.isEmpty() else QRect(
        0, 0, window_rect.width(), window_rect.height()
    )
    center_x = content.x() + content.width() / 2
    reveal = max(
        1,
        min(
            content.height(),
            int(reveal_pixels)
            if reveal_pixels is not None
            else round(content.height() * reveal_fraction),
        ),
    )
    if direction == "left":
        return QPoint(round(surface.left() - center_x), window_rect.y())
    if direction == "right":
        return QPoint(round(surface.right() + 1 - center_x), window_rect.y())
    if direction == "top":
        return QPoint(
            window_rect.x(),
            surface.top() + reveal - content.y() - content.height(),
        )
    return QPoint(
        window_rect.x(),
        surface.bottom() + 1 - reveal - content.y(),
    )


class PetWindow(QWidget):
    """透明置顶的桌宠窗口。

    集成 FrameAnimationController 进行帧动画渲染，
    支持拖动、边缘吸附、点击反应、老板键伪装、滚轮缩放。

    信号:
        edge_snapped: 屏幕边缘吸附方向（left/right/top/bottom）。
        clicked: 桌宠被点击（左键单击）。
        drag_started: 拖拽开始。
        drag_ended: 拖拽结束。
        animation_finished: 非循环动画播放完毕（动作名）。
        scale_changed: 缩放比例变化（新宽度, 新高度）。
        stealth_toggled: 伪装模式切换（True=进入伪装, False=恢复）。
    """

    edge_snapped = Signal(str)
    clicked = Signal()
    drag_started = Signal()
    drag_ended = Signal()
    animation_finished = Signal(str)
    scale_changed = Signal(int, int)
    stealth_toggled = Signal(bool)
    quit_requested = Signal()
    settings_requested = Signal()
    custom_role_workbench_requested = Signal()
    toggle_patrol_requested = Signal()

    def __init__(
        self,
        frame_controller: FrameAnimationController | None = None,
        fps: int = _DEFAULT_FPS,
        edge_reveal_fraction: float = 1 / 6,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._shutdown_started = False
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)


        self._fps = fps
        self._custom_role_workbench_available = False
        self._frame_controller = frame_controller or FrameAnimationController(self)
        self._render_cache: OrderedDict[int, tuple[QPixmap, QRect]] = OrderedDict()
        self._render_cache_bytes = 0
        self._render_cache_context: tuple[int, int, float] | None = None
        self._frame_controller.animation_changed.connect(self._on_animation_changed)
        self.set_edge_reveal_fraction(edge_reveal_fraction)
        self._edge_surface: QRect | None = None
        self._edge_direction: str | None = None
        self._edge_anchor_content_rect: QRect | None = None
        self._edge_anchor_reveal_pixels: int | None = None


        self._bubble: _MessageBubble | None = None


        self._bubble_anchor_center_ratio: float | None = None
        self._bubble_anchor_top_ratio: float | None = None
        self._visible_content_rect: QRect | None = None


        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.setInterval(_BUBBLE_TIMEOUT_MS)
        self._bubble_timer.timeout.connect(self.hide_message)


        self._paint_timer = QTimer(self)
        self._paint_timer.setInterval(int(1000 / fps))
        self._paint_timer.timeout.connect(self.update)


        self._drag_offset = None
        self._dragging = False

        self._drag_threshold = 10
        self._press_pos = None

        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(300)
        self._long_press_timer.timeout.connect(self._on_long_press)


        self._stealth_mode = False
        self._global_hotkey = None
        self._saved_geometry = None


        self._scale_persist_callback = None


    def showEvent(self, event) -> None:  # noqa: N802
        """窗口显示时启动重绘定时器。"""
        super().showEvent(event)
        self._paint_timer.start()


    @property
    def frame_controller(self) -> FrameAnimationController:
        """帧动画控制器。"""
        return self._frame_controller

    def set_animation(self, frame_paths: list[str], fps: int = _DEFAULT_FPS) -> None:
        """切换桌宠动作组（兼容旧接口）。

        Args:
            frame_paths: 帧图片路径列表。
            fps: 帧率。
        """
        self._fps = fps
        self._frame_controller.load_action("custom", frame_paths, fps=fps)
        self._frame_controller.play("custom", ping_pong=True)
        self._paint_timer.setInterval(int(1000 / fps))
        self._paint_timer.start()

    def set_playback_fps(self, fps: int) -> None:
        if type(fps) is not int or fps <= 0:
            raise ValueError("fps must be a positive integer")
        self._fps = fps
        self._paint_timer.setInterval(max(1, int(1000 / fps)))

    def _on_animation_changed(self, action_name: str) -> None:
        """动画切换回调。"""
        logger.debug("PetWindow 动画切换: %s", action_name)
        self._clear_render_cache()

    def set_edge_reveal_fraction(self, value: float) -> None:
        """Set how much character height remains visible at top/bottom edges."""
        if not 0.10 <= value <= 0.75:
            raise ValueError("edge_reveal_fraction must be between 0.10 and 0.75")
        self._edge_reveal_fraction = float(value)

    def _check_animation_finished(self) -> None:
        """检查非循环动画是否播放完毕。"""
        if (
            not self._frame_controller.is_playing
            and self._frame_controller.current_action
            and self._frame_controller.current_action != "idle"
        ):
            action = self._frame_controller.current_action
            self.animation_finished.emit(action)


    def show_message(self, text: str) -> None:
        """显示文字气泡（独立浮窗在桌宠上方，5 秒后自动消失）。"""
        if self._stealth_mode:
            return
        self.hide_message()

        self._bubble = _MessageBubble()


        self._bubble.setStyleSheet(
            "background: transparent; border: none;\n"
            f"color: {theme.TEXT_PRIMARY};\n"
            f"padding: 8px 12px;\n"
            f"{theme.font_css(13)}"
        )
        self._bubble.setWordWrap(True)
        self._bubble.setMaximumWidth(250)
        self._bubble.setText(text)
        self._bubble.adjustSize()


        self._capture_bubble_anchor()
        self._reposition_bubble()
        self._bubble.show()
        self._bubble_timer.start()

    def hide_message(self) -> None:
        """隐藏并销毁气泡浮窗。"""
        self._bubble_timer.stop()
        if self._bubble is not None:
            self._bubble.hide()
            self._bubble.deleteLater()
            self._bubble = None
        self._bubble_anchor_center_ratio = None
        self._bubble_anchor_top_ratio = None

    def _capture_bubble_anchor(self) -> None:
        """Capture one stable character anchor for the current action.

        The anchor is normalized to the pet window so wheel resizing remains
        proportional. It is captured once when the bubble is created and never
        refreshed for frame or action changes during that bubble's lifetime.
        """
        visible = self._current_content_rect()
        width = max(1, self.width())
        height = max(1, self.height())
        self._bubble_anchor_center_ratio = (
            visible.x() + visible.width() / 2
        ) / width
        self._bubble_anchor_top_ratio = visible.y() / height

    def _reposition_bubble(self) -> None:
        """按动作级稳定锚点居中，并维持固定的垂直间距。

        贴边时上方可能没有容身之处：桌宠吸附到屏幕顶端后，原本留给气泡的空间
        已经不存在，仍按上方摆放会把气泡推到屏幕外——用户在工作台说"桌宠正在
        说话"的那一刻什么也看不到。上方放不下就翻到下方，再把左右夹回屏幕内。
        """
        if self._bubble is None:
            return
        if (
            self._bubble_anchor_center_ratio is None
            or self._bubble_anchor_top_ratio is None
        ):
            self._capture_bubble_anchor()
        top_left = self.frameGeometry().topLeft()


        anchor_x = round(self.width() * self._bubble_anchor_center_ratio)
        anchor_top = round(self.height() * self._bubble_anchor_top_ratio)
        bx = top_left.x() + anchor_x - self._bubble.width() // 2
        by = (
            top_left.y()
            + anchor_top
            - _BUBBLE_CONTENT_GAP
            - self._bubble.height()
        )
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            bounds = screen.availableGeometry()
            if by < bounds.y():

                visible = self._current_content_rect()
                by = (
                    top_left.y()
                    + visible.y()
                    + visible.height()
                    + _BUBBLE_CONTENT_GAP
                )
                by = min(by, bounds.y() + bounds.height() - self._bubble.height())
                by = max(by, bounds.y())
            bx = max(bounds.x(), min(bx, bounds.x() + bounds.width() - self._bubble.width()))
        self._bubble.move(bx, by)

    def moveEvent(self, event) -> None:  # noqa: N802
        """桌宠移动时气泡跟随。"""
        super().moveEvent(event)
        self._reposition_bubble()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """桌宠缩放时让气泡继续锚定角色，而不是旧窗口尺寸。"""
        super().resizeEvent(event)
        if getattr(self, "_edge_surface", None) is not None:
            self._capture_surface_anchor()
            self._maintain_surface_snap()
        self._reposition_bubble()


    def toggle_stealth(self) -> None:
        """切换伪装模式。

        进入伪装：桌宠收缩为极简悬浮件（小圆点），隐藏所有气泡。
        退出伪装：恢复桌宠显示。
        """
        if not self._stealth_mode:
            self._enter_stealth()
        else:
            self._exit_stealth()

    def _enter_stealth(self) -> None:
        """进入伪装模式。"""
        self._stealth_mode = True
        self._saved_geometry = self.geometry()
        self.hide_message()
        self._paint_timer.stop()
        self._frame_controller.stop()

        self.resize(20, 20)
        self.update()
        self.stealth_toggled.emit(True)
        logger.info("进入伪装模式（老板键）")

    def _exit_stealth(self) -> None:
        """退出伪装模式。"""
        self._stealth_mode = False
        if self._saved_geometry is not None:
            self.setGeometry(self._saved_geometry)
            self._saved_geometry = None
        self._paint_timer.start()
        self._frame_controller.play("idle", ping_pong=True)
        self.update()
        self.stealth_toggled.emit(False)
        logger.info("退出伪装模式（老板键）")

    def register_stealth_hotkey(self, sequence: str = "Ctrl+Shift+H") -> None:
        """注册老板键全局热键。

        Win32 RegisterHotKey 系统级全局热键（无论焦点在哪都生效），
        注册失败时自动回退到 QShortcut（仅应用有焦点时生效）。

        Args:
            sequence: QKeySequence 字符串，如 "Ctrl+Shift+H"。
        """
        from moeguard.utils.global_hotkey import GlobalHotkey


        self.unregister_stealth_hotkey()

        self._global_hotkey = GlobalHotkey(self)
        self._global_hotkey.activated.connect(self.toggle_stealth)
        self._global_hotkey.register(sequence)

    def unregister_stealth_hotkey(self) -> None:
        """注销老板键热键。"""
        if self._global_hotkey is not None:
            self._global_hotkey.unregister()
            self._global_hotkey.deleteLater()
            self._global_hotkey = None


    def set_scale_persist_callback(self, callback) -> None:
        """设置缩放持久化回调。

        Args:
            callback: 接收 (width, height) 的回调函数。
        """
        self._scale_persist_callback = callback

    def _persist_scale(self) -> None:
        """持久化当前缩放尺寸。"""
        if self._scale_persist_callback is not None:
            self._scale_persist_callback(self.width(), self.height())
        self.scale_changed.emit(self.width(), self.height())


    def hide_pet(self) -> None:
        """完全免打扰：隐藏桌宠，程序后台运行。"""
        self.hide()
        self._paint_timer.stop()

    def show_pet(self) -> None:
        """退出完全免打扰：重新显示桌宠。"""
        self.show()
        self._paint_timer.start()

    def shutdown(self) -> None:
        """Stop timers and top-level helpers before the native Qt app exits."""
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._paint_timer.stop()
        self._bubble_timer.stop()
        self._long_press_timer.stop()
        self.unregister_stealth_hotkey()
        self.hide_message()
        self._clear_render_cache()
        self.hide()


    def _clear_render_cache(self) -> None:
        self._render_cache.clear()
        self._render_cache_bytes = 0
        self._render_cache_context = None

    def _rendered_frame(self, frame: QPixmap) -> tuple[QPixmap, QRect]:
        context = (self.width(), self.height(), self.devicePixelRatioF())
        if context != self._render_cache_context:
            self._clear_render_cache()
            self._render_cache_context = context
        key = frame.cacheKey()
        cached = self._render_cache.get(key)
        if cached is not None:
            self._render_cache.move_to_end(key)
            return cached
        scaled = frame.scaled(
            self.width(), self.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        bounds = QRegion(scaled.mask()).boundingRect()
        content = (bounds.translated(x, y) if not bounds.isEmpty()
                   else QRect(x, y, scaled.width(), scaled.height()))
        size = scaled.width() * scaled.height() * scaled.depth() // 8
        if size <= _RENDER_CACHE_MAX_BYTES:
            while self._render_cache and (
                len(self._render_cache) >= _RENDER_CACHE_MAX_FRAMES
                or self._render_cache_bytes + size > _RENDER_CACHE_MAX_BYTES
            ):
                old, _ = self._render_cache.popitem(last=False)[1]
                self._render_cache_bytes -= old.width() * old.height() * old.depth() // 8
            self._render_cache[key] = (scaled, content)
            self._render_cache_bytes += size
        return scaled, content


    def paintEvent(self, event) -> None:  # noqa: N802
        """绘制当前帧。"""
        if self._stealth_mode:

            with QPainter(self) as painter:
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setBrush(Qt.gray)
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(2, 2, self.width() - 4, self.height() - 4)
            return

        frame = self._frame_controller.get_current_frame()
        if frame is None or frame.isNull():
            return

        scaled, content = self._rendered_frame(frame)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        with QPainter(self) as painter:
            painter.drawPixmap(x, y, scaled)
        self._visible_content_rect = QRect(content)
        self._maintain_surface_snap()
        self._reposition_bubble()


        self._check_animation_finished()


    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragging = False
            self._long_press_timer.start()

    def _on_long_press(self) -> None:
        """长按超时：进入 dragging 状态（即使没有移动）。"""
        if self._drag_offset is not None and not self._dragging:
            self.clear_surface_snap()
            self._dragging = True
            self.drag_started.emit()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            current_pos = event.globalPosition().toPoint()

            if (
                self._press_pos is not None
                and not self._dragging
                and (current_pos - self._press_pos).manhattanLength() > self._drag_threshold
            ):
                self._long_press_timer.stop()
                self.clear_surface_snap()
                self._dragging = True
                self.drag_started.emit()
            if self._dragging:
                self.move(current_pos - self._drag_offset)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._long_press_timer.stop()
            if self._dragging:
                self._dragging = False
                self.drag_ended.emit()
                self._check_edge_snap()
            else:

                self.clicked.emit()
        self._drag_offset = None
        self._press_pos = None

    def _check_edge_snap(self) -> None:
        """拖动结束时检测显示器的固定上下左右四边。"""
        # 使用物理屏幕geometry；availableGeometry排除任务栏，但透明置顶窗口不在其边界裁切。
        screen = self.screen().geometry() if self.screen() else None
        if screen is None:
            return
        pos = self.pos()
        direction: str | None = None

        if pos.x() <= screen.left() + _EDGE_SNAP_MARGIN:
            direction = "left"
        elif pos.x() + self.width() >= screen.right() - _EDGE_SNAP_MARGIN:
            direction = "right"
        elif pos.y() <= screen.top() + _EDGE_SNAP_MARGIN:
            direction = "top"
        elif pos.y() + self.height() >= screen.bottom() - _EDGE_SNAP_MARGIN:
            direction = "bottom"

        if direction:
            self.edge_snapped.emit(direction)
            self.snap_to_surface(screen, direction)

    def snap_to_surface(self, surface: QRect, direction: str) -> None:
        'Attach to an arbitrary target rectangle using current visible content.'
        self._edge_surface = QRect(surface)
        self._edge_direction = direction
        self._capture_surface_anchor()
        self.move(
            edge_snap_position(
                self._edge_surface,
                self.geometry(),
                self._edge_anchor_content_rect or self.rect(),
                direction,
                self._edge_reveal_fraction,
                reveal_pixels=self._edge_anchor_reveal_pixels,
            )
        )

    def clear_surface_snap(self) -> None:
        """Stop edge anchoring before free dragging or a mode change."""
        self._edge_surface = None
        self._edge_direction = None
        self._edge_anchor_content_rect = None
        self._edge_anchor_reveal_pixels = None

    def _capture_surface_anchor(self) -> None:
        """Freeze one alpha bbox and vertical reveal amount for this snap.

        Generated animation frames often have slightly different transparent
        bounds. Recalculating the edge position from every frame makes the pet
        drift and can reveal most of a short frame. Screen top/bottom instead
        expose a fixed number of pixels from the captured character bbox.
        """
        content = self._current_content_rect()
        self._edge_anchor_content_rect = QRect(content)
        self._edge_anchor_reveal_pixels = (
            min(_SCREEN_EDGE_HEAD_REVEAL_PIXELS, max(1, content.height()))
            if self._edge_direction in {"top", "bottom"}
            else None
        )

    def _current_content_rect(self) -> QRect:
        """Measure the current frame's alpha bbox in window-local coordinates."""
        frame = self._frame_controller.get_current_frame()
        if frame is None or frame.isNull():
            return self._visible_content_rect or self.rect()
        return QRect(self._rendered_frame(frame)[1])

    def _maintain_surface_snap(self) -> None:
        """Keep the one captured snap anchor stable while frames animate."""
        if self._edge_surface is None or self._edge_direction is None:
            return
        if self._edge_anchor_content_rect is None:
            self._capture_surface_anchor()
        desired = edge_snap_position(
            self._edge_surface,
            self.geometry(),
            self._edge_anchor_content_rect or self.rect(),
            self._edge_direction,
            self._edge_reveal_fraction,
            reveal_pixels=self._edge_anchor_reveal_pixels,
        )
        if self.pos() != desired:
            self.move(desired)


    def set_custom_role_workbench_available(self, available: bool) -> None:
        '设置右键菜单是否显示 桌宠工坊直达入口。'
        self._custom_role_workbench_available = bool(available)

    def _build_context_menu(self):
        """构建可测试的桌宠快捷菜单。"""
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)


        if self._custom_role_workbench_available:
            act_custom_role = menu.addAction("桌宠工坊…")
            act_custom_role.triggered.connect(
                self.custom_role_workbench_requested.emit
            )

        act_settings = menu.addAction("设置")
        act_settings.triggered.connect(self.settings_requested.emit)

        menu.addSeparator()

        act_patrol = menu.addAction("进入值守 / 回到陪伴")
        act_patrol.triggered.connect(self.toggle_patrol_requested.emit)

        menu.addSeparator()

        act_quit = menu.addAction("退出萌卫")
        act_quit.triggered.connect(self.quit_requested.emit)

        return menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """右键弹出快捷菜单。"""
        menu = self._build_context_menu()

        menu.exec(event.globalPos())


    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        """滚轮缩放桌宠尺寸，并持久化。"""
        if self._stealth_mode:
            return

        delta = event.angleDelta().y()
        scale_factor = 1.1 if delta > 0 else 0.9

        new_w = int(self.width() * scale_factor)
        new_h = int(self.height() * scale_factor)


        new_w = max(_MIN_WIDTH, min(_MAX_WIDTH, new_w))
        new_h = max(_MIN_HEIGHT, min(_MAX_HEIGHT, new_h))

        if new_w != self.width() or new_h != self.height():
            self.resize(new_w, new_h)
            self._persist_scale()
            logger.debug("桌宠缩放: %dx%d", new_w, new_h)
