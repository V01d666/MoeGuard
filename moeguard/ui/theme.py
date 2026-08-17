"""轻量 UI 主题层：集中定义颜色、字号、圆角、间距与共享 QSS。

设计约束（见 PR feat/ui-modernization）:
- 不引入任何第三方依赖，仅使用 Qt 内建机制（QSS / objectName / 动态属性）。
- 不打包字体：Windows 优先 Segoe UI，自动回退系统字体。
- 样式只在窗口创建时应用一次；动画逐帧路径（paintEvent / QTimer）禁止触碰本模块。
- 作用域全部通过 objectName（如 ``#onboardingBubble``）或对象自身的 stylesheet
  完成，避免通用 QLabel/QPushButton 规则误伤桌宠透明窗口与 QMessageBox 等系统控件。

调色方向：温暖、轻盈、干净 —— 暖白背景、深灰文字、低饱和蓝绿主色、
明确但不刺眼的安全红；圆角 6-10px（气泡可稍大），用边框与背景分层而不用阴影。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 颜色（暖白 / 低饱和蓝绿 / 安全红）
# ---------------------------------------------------------------------------

WINDOW_BG = "#FAF8F4"           # 暖白窗口背景
PANEL_BG = "#FFFFFF"            # 卡片 / 输入面板背景
SECTION_BG = "#F2F0EA"          # 柔和浅色分区背景
BORDER = "#E3DFD6"              # 常规边框
BORDER_STRONG = "#D5CFC3"       # 略深边框（悬停 / 分隔）

TEXT_PRIMARY = "#3A3A3A"        # 深灰主文字
TEXT_SECONDARY = "#7A766D"      # 次级说明文字

ACCENT = "#3E8E84"              # 低饱和蓝绿主色（普通强调按钮）
ACCENT_HOVER = "#35796F"
ACCENT_PRESSED = "#2D6A61"

DANGER = "#C04545"              # 危险操作（撤回同意 / 删除证据）
DANGER_HOVER = "#A93C3C"
DANGER_PRESSED = "#933333"

PATROL_BANNER_BG = "#9B1C1C"    # 值守提示：高对比安全红（摄像头使用中）
PATROL_BANNER_TEXT = "#FFFFFF"

BUBBLE_BG_RGBA = (255, 255, 255, 245)
BUBBLE_BORDER_RGBA = (213, 207, 195, 160)
BUBBLE_BG = f"rgba{BUBBLE_BG_RGBA}"        # 气泡背景（引导 / 消息共用）
BUBBLE_BORDER = f"rgba{BUBBLE_BORDER_RGBA}"    # 气泡边框
PREVIEW_PLACEHOLDER_BG = "#2E2C29"            # 摄像头预览占位（暖深灰）
PREVIEW_PLACEHOLDER_TEXT = "#9C978E"

# ---------------------------------------------------------------------------
# 尺寸（圆角 / 间距，单位 px）
# ---------------------------------------------------------------------------

RADIUS_SMALL = 6      # 输入控件 / 小按钮
RADIUS_MEDIUM = 8     # 按钮 / 分区卡片
RADIUS_LARGE = 12     # 消息气泡
RADIUS_XLARGE = 16    # 引导气泡

SPACE_SMALL = 6
SPACE_MEDIUM = 10
SPACE_LARGE = 16

CONTROL_MIN_HEIGHT = 26  # 输入控件 / 按钮最小高度（DPI 友好，不锁死固定高度）

# ---------------------------------------------------------------------------
# 字体：系统字体族，Windows 优先 Segoe UI，不打包字体文件
# ---------------------------------------------------------------------------

FONT_FAMILY = '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
FONT_SIZE_NORMAL = 13
FONT_SIZE_SMALL = 12
FONT_SIZE_TITLE = 15


def font_css(size: int = FONT_SIZE_NORMAL, *, bold: bool = False) -> str:
    """返回一段 font QSS 片段（family + size + 可选粗体）。"""
    weight = "bold" if bold else "normal"
    return f"font-family: {FONT_FAMILY}; font-size: {size}px; font-weight: {weight};"


# ---------------------------------------------------------------------------
# 共享按钮片段：普通 / 强调 / 危险三级
# ---------------------------------------------------------------------------


def button_qss(role: str = "normal") -> str:
    """生成单个按钮角色的 QSS（供对象级 stylesheet 使用）。

    role: "normal" | "accent" | "danger"
    """
    if role == "accent":
        bg, hover, pressed, fg = ACCENT, ACCENT_HOVER, ACCENT_PRESSED, "#FFFFFF"
        border = "none"
    elif role == "danger":
        bg, hover, pressed, fg = DANGER, DANGER_HOVER, DANGER_PRESSED, "#FFFFFF"
        border = "none"
    else:
        bg, hover, pressed, fg = PANEL_BG, SECTION_BG, BORDER, TEXT_PRIMARY
        border = f"1px solid {BORDER_STRONG}"
    return (
        "QPushButton {\n"
        f"    background: {bg}; color: {fg}; border: {border};\n"
        f"    border-radius: {RADIUS_MEDIUM}px; padding: 5px 14px;\n"
        f"    min-height: {CONTROL_MIN_HEIGHT}px; {font_css()}\n"
        "}\n"
        f"QPushButton:hover {{ background: {hover}; }}\n"
        f"QPushButton:pressed {{ background: {pressed}; }}\n"
        "QPushButton:disabled {\n"
        f"    background: {SECTION_BG}; color: {TEXT_SECONDARY}; border: {border};\n"
        "}\n"
    )


# ---------------------------------------------------------------------------
# 气泡（引导气泡与桌宠消息气泡共享规范）
# ---------------------------------------------------------------------------


def bubble_qss(radius: int = RADIUS_XLARGE) -> str:
    """气泡背景 QSS（应用于带 objectName 的容器）。"""
    return (
        f"background: {BUBBLE_BG}; border: 1px solid {BUBBLE_BORDER};\n"
        f"border-radius: {radius}px;"
    )


# ---------------------------------------------------------------------------
# 模态对话框（设置 / 证据）作用域 QSS
# ---------------------------------------------------------------------------


def dialog_qss(scope: str) -> str:
    """生成以 objectName 作用域限定的对话框整体 QSS。

    所有选择器都限定在 ``#{scope}`` 之下，不会泄漏到桌宠窗口、
    托盘菜单或 QMessageBox 等系统控件。

    scope: 对话框的 objectName（如 "settingsDialog" / "evidenceDialog"）。
    """
    return f"""
QDialog#{scope} {{
    background: {WINDOW_BG};
    {font_css()}
    color: {TEXT_PRIMARY};
}}
QDialog#{scope} QLabel {{
    color: {TEXT_PRIMARY};
    background: transparent;
}}
QDialog#{scope} QLabel[role="title"] {{
    {font_css(FONT_SIZE_TITLE, bold=True)}
    color: {TEXT_PRIMARY};
}}
QDialog#{scope} QLabel[role="hint"] {{
    color: {TEXT_SECONDARY};
    font-size: {FONT_SIZE_SMALL}px;
}}
QDialog#{scope} QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MEDIUM}px;
    background: {PANEL_BG};
    top: -1px;
}}
QDialog#{scope} QTabBar::tab {{
    background: {WINDOW_BG};
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: {RADIUS_MEDIUM}px;
    border-top-right-radius: {RADIUS_MEDIUM}px;
    padding: 6px 18px;
    margin-right: 4px;
}}
QDialog#{scope} QTabBar::tab:selected {{
    background: {PANEL_BG};
    color: {TEXT_PRIMARY};
    font-weight: bold;
}}
QDialog#{scope} QTabBar::tab:hover:!selected {{
    background: {SECTION_BG};
}}
QDialog#{scope} QFrame[role="section"] {{
    background: {SECTION_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MEDIUM}px;
}}
QDialog#{scope} QFrame[role="dangerSection"] {{
    background: #FBEFED;
    border: 1px solid #E8C6C3;
    border-radius: {RADIUS_MEDIUM}px;
}}
QDialog#{scope} QSpinBox,
QDialog#{scope} QDoubleSpinBox,
QDialog#{scope} QComboBox,
QDialog#{scope} QKeySequenceEdit {{
    background: {PANEL_BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SMALL}px;
    padding: 3px 6px;
    min-height: {CONTROL_MIN_HEIGHT}px;
    selection-background-color: {ACCENT};
}}
QDialog#{scope} QSpinBox:focus,
QDialog#{scope} QDoubleSpinBox:focus,
QDialog#{scope} QComboBox:focus,
QDialog#{scope} QKeySequenceEdit:focus {{
    border: 1px solid {ACCENT};
}}
QDialog#{scope} QComboBox QAbstractItemView {{
    background: {PANEL_BG};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}
QDialog#{scope} QCheckBox {{
    spacing: 6px;
}}
QDialog#{scope} QListWidget {{
    background: {PANEL_BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MEDIUM}px;
    padding: 4px;
    alternate-background-color: {WINDOW_BG};
}}
QDialog#{scope} QListWidget::item {{
    padding: 6px 8px;
    border-radius: {RADIUS_SMALL}px;
}}
QDialog#{scope} QListWidget::item:selected {{
    background: {ACCENT};
    color: #FFFFFF;
}}
QDialog#{scope} QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QDialog#{scope} QScrollBar::handle:vertical {{
    background: {BORDER_STRONG};
    border-radius: 5px;
    min-height: 24px;
}}
QDialog#{scope} QScrollBar::handle:vertical:hover {{
    background: {TEXT_SECONDARY};
}}
QDialog#{scope} QScrollBar::add-line:vertical,
QDialog#{scope} QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""
