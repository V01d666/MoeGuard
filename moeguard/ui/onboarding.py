"""首次启动气泡式引导（M4 重写）。

将 QWizard 改为轻量气泡式引导，悬浮在桌宠头上。
透明无边框小窗口，定位在桌宠上方，显示引导文字 + 操作按钮。
不阻塞主线程，桌宠保持可见。

步骤（非 modal，每步一个气泡）:
  Step 1: 值守风险告知 + 主动同意 / 仅使用桌宠
  Step 2: 询问是否注册主人脸 + [好呀] [跳过]
  Step 3 (如果选好呀): 摄像头预览 + [拍一张] -> 等待注册结果
  Step 4: 完成提示 + [完成]
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from moeguard.ui import theme

logger = logging.getLogger(__name__)

# 气泡尺寸
_BUBBLE_WIDTH = 280
_BUBBLE_HEIGHT = 140
_BUBBLE_MARGIN = 10  # 气泡与桌宠顶部间距
_PREVIEW_W = 240  # 摄像头预览固定宽度
_PREVIEW_H = 180  # 摄像头预览固定高度

# 气泡 QSS：主题统一调色（暖白圆角 + 低饱和主色按钮 + 次要按钮）。
# 作用域限定在 #onboardingBubble，不会泄漏到桌宠或其他窗口。
_BUBBLE_QSS = f"""
QWidget#onboardingBubble {{
    {theme.bubble_qss(theme.RADIUS_XLARGE)}
}}
QWidget#onboardingBubble QLabel {{
    color: {theme.TEXT_PRIMARY};
    {theme.font_css(13)}
}}
QWidget#onboardingBubble QPushButton {{
    background: {theme.ACCENT};
    color: white;
    border: none;
    border-radius: {theme.RADIUS_MEDIUM}px;
    padding: 6px 16px;
    {theme.font_css(13)}
    min-width: 60px;
}}
QWidget#onboardingBubble QPushButton:hover {{
    background: {theme.ACCENT_HOVER};
}}
QWidget#onboardingBubble QPushButton:pressed {{
    background: {theme.ACCENT_PRESSED};
}}
QWidget#onboardingBubble QPushButton#skipBtn {{
    background: {theme.SECTION_BG};
    color: {theme.TEXT_SECONDARY};
    border: 1px solid {theme.BORDER};
}}
QWidget#onboardingBubble QPushButton#skipBtn:hover {{
    background: {theme.BORDER};
}}
QWidget#onboardingBubble QPushButton:disabled {{
    background: {theme.BORDER};
    color: {theme.TEXT_SECONDARY};
}}
QWidget#onboardingBubble QScrollArea {{
    background: transparent;
    border: none;
}}
QWidget#onboardingBubble QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px;
}}
QWidget#onboardingBubble QScrollBar::handle:vertical {{
    background: {theme.BORDER_STRONG};
    border-radius: 4px;
    min-height: 20px;
}}
QWidget#onboardingBubble QScrollBar::handle:vertical:hover {{
    background: {theme.TEXT_SECONDARY};
}}
QWidget#onboardingBubble QScrollBar::add-line:vertical,
QWidget#onboardingBubble QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""

# 引导文字
_STEP_TEXTS: dict[int, str] = {
    1: (
        "值守会使用摄像头并在本机保存陌生人证据；仅建议用于你自己的"
        "私密空间。它不是专业安防，设备睡眠或部分电池待机场景会中断。"
        "请先告知同空间的其他人。证据会按设置保留，且可随时撤回并删除。"
    ),
    2: "要不要让我记住你的脸？\n这样离开时我才知道该看守谁的家～",
    3: "看着摄像头哦~",
    4: "桌宠准备好啦！\n你可随时在设置中撤回值守同意并删除本地数据。",
}


class OnboardingBubble(QWidget):
    """气泡式引导窗口，悬浮在桌宠上方，与桌宠绑定移动。

    透明无边框小窗口，居中于桌宠上方，显示引导文字 + 操作按钮。
    拖动气泡会同步移动桌宠，拖动桌宠气泡跟随。

    信号:
        finished: 引导完成，参数为选定的形象路径（空=跳过）。
        register_owner: 请求注册主人脸（Step 3 拍照时发射）。
    """

    finished = Signal(str)  # 选定的形象路径（空=跳过）
    consent_granted = Signal()
    consent_declined = Signal()
    preview_requested = Signal()  # 请求启动摄像头预览（进入 Step 3 时）
    register_owner = Signal()  # 请求注册主人脸（拍照时）

    def __init__(
        self,
        pet_window: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pet = pet_window
        self.setObjectName("onboardingBubble")
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(_BUBBLE_WIDTH, _BUBBLE_HEIGHT)

        self.setStyleSheet(_BUBBLE_QSS)

        self._step = 0
        self._skipped_owner = False
        self._owner_registered = False
        self._registration_in_progress = False

        # 拖动状态
        self._drag_offset = None

        # 内部容器（承载 QSS 圆角背景）
        self._container = QWidget(self)
        self._container.setObjectName("onboardingBubble")
        self._container.setGeometry(0, 0, _BUBBLE_WIDTH, _BUBBLE_HEIGHT)
        self._container.setStyleSheet(_BUBBLE_QSS)

        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(20, 12, 20, 12)
        self._layout.setSpacing(6)

        # 引导文字：固定气泡尺寸内可滚动，避免风险告知被裁切。
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setFixedWidth(_BUBBLE_WIDTH - 52)
        self._text_scroll = QScrollArea()
        self._text_scroll.setWidget(self._label)
        self._text_scroll.setWidgetResizable(False)
        self._text_scroll.setFrameShape(QScrollArea.NoFrame)
        self._text_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._text_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._layout.addWidget(self._text_scroll)

        # 摄像头预览（Step 3 用）
        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setFixedSize(_PREVIEW_W, _PREVIEW_H)
        self._preview.setScaledContents(False)
        self._preview.setHidden(True)
        self._preview.setStyleSheet(
            f"background: {theme.PREVIEW_PLACEHOLDER_BG};"
            f" border-radius: {theme.RADIUS_MEDIUM}px;"
            f" color: {theme.PREVIEW_PLACEHOLDER_TEXT};"
        )
        self._layout.addWidget(self._preview)

        # 按钮区
        self._button_layout = QHBoxLayout()
        self._button_layout.setSpacing(8)
        self._layout.addLayout(self._button_layout)

        # Step 3 预览模式下插入的额外间距（其他步骤移除）
        self._step3_spacer = None

        self._buttons: list[QPushButton] = []

        # 定位：居中于桌宠上方
        self._sync_position()

        # 监听桌宠移动
        self._pet.installEventFilter(self)

        # 显示第一步
        self._show_step(1)

    # ------------------------------------------------------------------ #
    # 定位与绑定
    # ------------------------------------------------------------------ #

    def _sync_position(self) -> None:
        """气泡居中于桌宠上方：中心线与桌宠中心线对齐，
        气泡下边缘与桌宠上边缘保持 margin。"""
        pet_geo = self._pet.frameGeometry()
        bubble_x = pet_geo.center().x() - _BUBBLE_WIDTH // 2
        bubble_y = pet_geo.top() - self.height() - _BUBBLE_MARGIN
        self.move(max(0, bubble_x), max(0, bubble_y))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """监听桌宠移动事件，气泡跟随。"""
        from PySide6.QtCore import QEvent
        if obj is self._pet and event.type() == QEvent.Move:
            self._sync_position()
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """拖动气泡时同步移动桌宠。"""
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            current_pos = event.globalPosition().toPoint()
            new_bubble_pos = current_pos - self._drag_offset
            self.move(new_bubble_pos)
            # 同步移动桌宠
            pet_x = new_bubble_pos.x() + _BUBBLE_WIDTH // 2 - self._pet.width() // 2
            # Step 3 含摄像头预览，气泡高度会动态增大，不能继续使用
            # 默认的 140px 高度，否则拖动时桌宠会钻进预览框下方。
            pet_y = new_bubble_pos.y() + self.height() + _BUBBLE_MARGIN
            self._pet.move(max(0, pet_x), max(0, pet_y))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = None

    # ------------------------------------------------------------------ #
    # 步骤控制
    # ------------------------------------------------------------------ #

    def _clear_buttons(self) -> None:
        """清除当前所有按钮。"""
        for btn in self._buttons:
            self._button_layout.removeWidget(btn)
            # deleteLater() 要等事件循环处理；先隐藏可避免步骤切换时旧、新
            # 按钮短暂叠在同一位置。
            btn.hide()
            btn.deleteLater()
        self._buttons.clear()

    def _add_button(self, text: str, callback, is_skip: bool = False) -> QPushButton:
        """添加一个按钮。"""
        btn = QPushButton(text)
        if is_skip:
            btn.setObjectName("skipBtn")
        btn.clicked.connect(callback)
        self._button_layout.addWidget(btn)
        self._buttons.append(btn)
        return btn

    def _show_step(self, step: int) -> None:
        """显示指定步骤的气泡内容。"""
        self._step = step
        self._clear_buttons()

        # 非 Step 3 时移除预览模式下的额外间距
        if step != 3 and self._step3_spacer is not None:
            self._layout.removeItem(self._step3_spacer)
            self._step3_spacer = None

        text = _STEP_TEXTS.get(step, "")
        self._label.setText(text)
        # QScrollArea 不会为 word-wrap 标签自动计算超出视口的内容高度；
        # 显式按固定宽度测量全文，才能既保留完整内容又出现滚动条。
        content_width = _BUBBLE_WIDTH - 52
        content_height = self._label.fontMetrics().boundingRect(
            0,
            0,
            content_width,
            4096,
            Qt.TextWordWrap | Qt.AlignHCenter,
            text,
        ).height() + 8
        self._label.setFixedSize(content_width, max(32, content_height))
        self._text_scroll.setFixedHeight(min(68, max(32, content_height)))

        if step == 1:
            self._preview.setHidden(True)
            self._add_button("我已知悉，继续", self._on_consent_granted)
            self._add_button("仅使用桌宠", self._on_consent_declined, is_skip=True)
        elif step == 2:
            self._preview.setHidden(True)
            self._add_button("好呀", self._on_choose_register)
            self._add_button("跳过", self._on_skip_register, is_skip=True)
        elif step == 3:
            self._preview.setHidden(False)
            self._preview.setText("摄像头启动中...")
            self._add_button("拍一张", self._on_capture)
            # Step 3: 在预览和按钮之间插入额外间距，避免拥挤
            if hasattr(self, "_step3_spacer") and self._step3_spacer is not None:
                self._layout.removeItem(self._step3_spacer)
            from PySide6.QtWidgets import QSizePolicy, QSpacerItem
            self._step3_spacer = QSpacerItem(
                20, 20, QSizePolicy.Minimum, QSizePolicy.Fixed,
            )
            self._layout.insertSpacerItem(
                self._layout.indexOf(self._button_layout), self._step3_spacer,
            )
            # 动态计算气泡高度 = 边距 + label + preview + 间距 + 按钮 + 额外底部留白
            extra_bottom = 16
            _h = (
                24  # content margins (12 top + 12 bottom)
                + self._text_scroll.height()
                + 6   # label-preview spacing
                + _PREVIEW_H
                + 20  # spacer above buttons
                + self._button_layout.sizeHint().height()
                + extra_bottom
            )
            self.setFixedSize(_BUBBLE_WIDTH, _h)
            self._container.setGeometry(0, 0, _BUBBLE_WIDTH, _h)
            self._sync_position()
            self.preview_requested.emit()
        elif step == 4:
            self._preview.setHidden(True)
            # 恢复默认气泡高度
            self.setFixedSize(_BUBBLE_WIDTH, _BUBBLE_HEIGHT)
            self._container.setGeometry(0, 0, _BUBBLE_WIDTH, _BUBBLE_HEIGHT)
            self._sync_position()
            self._add_button("完成", self._on_finish)
        else:
            logger.warning("未知引导步骤: %d", step)
            return

        self.adjustSize()
        logger.debug("显示引导步骤 %d", step)

    def _next_step(self) -> None:
        """进入下一步（默认顺序）。"""
        next_step = self._step + 1
        if next_step > 4:
            self._on_finish()
        else:
            self._show_step(next_step)

    # ------------------------------------------------------------------ #
    # 步骤回调
    # ------------------------------------------------------------------ #

    def _on_consent_granted(self) -> None:
        """用户明确知悉风险后才允许进入注册/值守流程。"""
        self.consent_granted.emit()
        self._show_step(2)

    def _on_consent_declined(self) -> None:
        """拒绝采集：纯桌宠继续可用，不进入注册或自动值守。"""
        self.consent_declined.emit()
        self._on_finish()

    def _on_choose_register(self) -> None:
        """用户选择注册主人脸 -> 进入 Step 3。"""
        self._skipped_owner = False
        self._show_step(3)

    def _on_skip_register(self) -> None:
        """用户跳过注册 -> 直接进入 Step 4。"""
        self._skipped_owner = True
        self._show_step(4)

    def _on_capture(self) -> None:
        """拍一张 -> 请求注册主人脸。"""
        if self._registration_in_progress:
            return
        self._registration_in_progress = True
        # 发射信号，app 层负责摄像头采集 + 主人注册
        self.register_owner.emit()
        self._clear_buttons()
        waiting = self._add_button("正在注册…", lambda: None)
        waiting.setEnabled(False)

    def registration_result(self, success: bool, message: str = "") -> None:
        """仅当后台注册和持久化均成功后，才允许进入完成页。"""
        self._registration_in_progress = False
        if success:
            self._owner_registered = True
            self._show_step(4)
            return
        self._label.setText(message or "没有识别到清晰人脸，请调整光线后重试。")
        self._clear_buttons()
        self._add_button("重新拍一张", self._on_capture)

    def _on_finish(self) -> None:
        """引导完成。"""
        # 形象路径为空（MVP 使用默认形象）
        self._step = 4  # closeEvent 不再把已完成流程误报为“跳过”
        self.finished.emit("")
        self.close()

    # ------------------------------------------------------------------ #
    # 摄像头预览
    # ------------------------------------------------------------------ #

    def update_preview(self, frame_pixmap: QPixmap) -> None:
        """更新摄像头预览画面（app 层采集帧后调用）。

        Args:
            frame_pixmap: 摄像头帧的 QPixmap。
        """
        if self._step == 3:
            scaled = frame_pixmap.scaled(
                self._preview.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self._preview.setPixmap(scaled)

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭事件：如果尚未完成，发射 finished 空路径（视为跳过）。"""
        if self._step < 4:
            self.finished.emit("")
        super().closeEvent(event)
