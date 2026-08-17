"""设置对话框：仅暴露 MVP 已交付的本地安防与通用配置。

AI 对话与形象生成代码保留在 v2 实验层，但没有 MVP UI、凭据或提交入口。

视觉层（feat/ui-modernization）：
- 主题来自 moeguard.ui.theme，以 objectName 作用域限定，不影响其他窗口。
- 分区卡片区分“识别与证据参数 / 数据与隐私（危险区）”等层级；
  危险操作使用明确的危险样式并保留二次确认。
- 仅做视觉与布局整理：不改任何配置字段、默认值、信号或保存语义。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from moeguard.config import AppConfig
from moeguard.pet.role_assets import discover_bundled_roles
from moeguard.ui import theme

logger = logging.getLogger(__name__)


def _make_section(
    title: str, hint: str = "", *, danger: bool = False,
) -> tuple[QFrame, QFormLayout]:
    """构建一个分区卡片（圆角浅色背景 + 标题 + 可选说明 + 表单）。

    返回 (section_frame, form_layout)。调用方把控件加入 form_layout，
    再把 section_frame 加入页布局。
    """
    frame = QFrame()
    frame.setProperty("role", "dangerSection" if danger else "section")
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(14, 10, 14, 12)
    outer.setSpacing(6)

    title_label = QLabel(title)
    title_label.setProperty("role", "title")
    if danger:
        title_label.setStyleSheet(f"color: {theme.DANGER};")
    outer.addWidget(title_label)

    if hint:
        hint_label = QLabel(hint)
        hint_label.setProperty("role", "hint")
        hint_label.setWordWrap(True)
        outer.addWidget(hint_label)

    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(8)
    outer.addLayout(form)
    return frame, form


class SettingsDialog(QDialog):
    """萌卫设置对话框（安防与通用两标签页）。

    标签页 1 安防: 人脸阈值 / 陌生人冷却 / 证据视频时长 / 保留天数 / 隐私模式
    标签页 2 通用: 离座判定 / 采样间隔 / 摄像头序号
    """

    withdraw_patrol_consent_requested = Signal()
    manage_evidence_requested = Signal()

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle("萌卫设置")
        self.setMinimumWidth(520)
        self.setStyleSheet(theme.dialog_qss("settingsDialog"))

        self._config = config
        self._patrol_consent_withdrawn = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        self._build_security_tab()
        # D63/D70：AI 对话和形象生成均未进入 MVP，不得造成“可用”的错觉。
        self._build_general_tab()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Ok).setStyleSheet(theme.button_qss("accent"))
        buttons.button(QDialogButtonBox.Cancel).setStyleSheet(theme.button_qss("normal"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ #
    # 标签页 1: 安防
    # ------------------------------------------------------------------ #

    def _build_security_tab(self) -> None:
        sec = self._config.security
        ev = self._config.evidence

        tab = QWidget()
        page = QVBoxLayout(tab)
        page.setContentsMargins(12, 12, 12, 12)
        page.setSpacing(10)

        # 分区 1：识别与证据参数
        param_frame, form = _make_section(
            "识别与证据",
            "控制值守时如何识别陌生人、保存多少证据。所有数据仅保存在本机。",
        )

        self.face_threshold = QDoubleSpinBox()
        self.face_threshold.setRange(0.1, 0.9)
        self.face_threshold.setSingleStep(0.01)
        self.face_threshold.setDecimals(3)
        self.face_threshold.setValue(sec.face_match_threshold)
        self.face_threshold.setToolTip("越高越严格：减少误认主人，但可能更常把主人当陌生人。")
        form.addRow("人脸匹配阈值", self.face_threshold)

        self.stranger_cooldown = QSpinBox()
        self.stranger_cooldown.setRange(5, 300)
        self.stranger_cooldown.setSuffix(" 秒")
        self.stranger_cooldown.setValue(sec.stranger_cooldown_sec)
        form.addRow("陌生人冷却时间", self.stranger_cooldown)

        self.video_clip_sec = QSpinBox()
        self.video_clip_sec.setRange(5, 60)
        self.video_clip_sec.setSuffix(" 秒")
        self.video_clip_sec.setValue(ev.video_clip_sec)
        form.addRow("证据视频时长", self.video_clip_sec)

        self.retention_days = QSpinBox()
        self.retention_days.setRange(1, 90)
        self.retention_days.setSuffix(" 天")
        self.retention_days.setValue(ev.retention_days)
        form.addRow("证据保留天数", self.retention_days)

        self.blur_stranger = QCheckBox("模糊陌生人脸（隐私模式）")
        self.blur_stranger.setChecked(ev.blur_stranger_faces)
        form.addRow("", self.blur_stranger)

        self.motion_recording_enabled = QCheckBox("画面运动时额外留存证据")
        self.motion_recording_enabled.setChecked(ev.motion_recording_enabled)
        self.motion_recording_enabled.setToolTip(
            "默认关闭。会对画面中的人或物体运动额外保存截图和短视频；"
            "不是活体检测，静止照片仍可能被误认为主人。"
        )
        form.addRow("补充取证", self.motion_recording_enabled)

        self.manage_evidence_button = QPushButton("管理本地证据…")
        self.manage_evidence_button.setStyleSheet(theme.button_qss("normal"))
        self.manage_evidence_button.clicked.connect(self.manage_evidence_requested.emit)
        form.addRow("证据", self.manage_evidence_button)

        page.addWidget(param_frame)

        # 分区 2：数据与隐私（危险区，视觉独立）
        danger_frame, danger_form = _make_section(
            "数据与隐私",
            "撤回同意后值守立即停止，本机的主人特征与全部证据将被永久删除。",
            danger=True,
        )

        self.withdraw_patrol_consent_button = QPushButton(
            "撤回值守同意并删除本地数据…"
        )
        self.withdraw_patrol_consent_button.setObjectName("dangerButton")
        self.withdraw_patrol_consent_button.setStyleSheet(theme.button_qss("danger"))
        self.withdraw_patrol_consent_button.clicked.connect(
            self._confirm_withdraw_patrol_consent
        )
        danger_form.addRow(self.withdraw_patrol_consent_button)

        page.addWidget(danger_frame)
        page.addStretch(1)

        self._tabs.addTab(tab, "安防")

    # ------------------------------------------------------------------ #
    # 标签页 2: 通用
    # ------------------------------------------------------------------ #

    def _build_general_tab(self) -> None:
        pres = self._config.presence
        cam = self._config.camera
        pet = self._config.pet

        tab = QWidget()
        page = QVBoxLayout(tab)
        page.setContentsMargins(12, 12, 12, 12)
        page.setSpacing(10)

        # 分区 1：内置桌宠角色。角色包由清单自动发现，新增正式角色无需再改 UI。
        role_frame, role_form = _make_section(
            "桌宠外观",
            "切换已经随萌卫安装的完整角色；自定义角色导入将在后续版本开放。",
        )
        self.role_selector = QComboBox()
        bundled_roles = discover_bundled_roles()
        available_ids = {role.role_id for role in bundled_roles}
        for role in bundled_roles:
            self.role_selector.addItem(role.display_name, role.role_id)
        if pet.assets_dir:
            self.role_selector.addItem("自定义角色包（当前）", "__custom__")
            selected_role = self.role_selector.findData("__custom__")
        elif pet.role_id not in available_ids:
            self.role_selector.addItem(f"{pet.role_id}（当前不可用）", pet.role_id)
            selected_role = self.role_selector.findData(pet.role_id)
        else:
            selected_role = self.role_selector.findData(pet.role_id)
        self.role_selector.setCurrentIndex(max(0, selected_role))
        role_form.addRow("当前角色", self.role_selector)
        page.addWidget(role_frame)

        # 分区 2：值守触发
        patrol_frame, patrol_form = _make_section(
            "值守触发",
            "决定什么时候进入值守；自动值守默认关闭，需先完成风险告知和主人注册。",
        )

        self.away_threshold = QSpinBox()
        self.away_threshold.setRange(30, 600)
        self.away_threshold.setSuffix(" 秒")
        self.away_threshold.setValue(pres.idle_threshold_sec)
        patrol_form.addRow("离座判定时长", self.away_threshold)

        self.auto_patrol_enabled = QCheckBox("锁屏后自动开始值守")
        self.auto_patrol_enabled.setChecked(pres.auto_patrol_enabled)
        self.auto_patrol_enabled.setToolTip("须先完成风险告知和主人注册；默认关闭。")
        patrol_form.addRow("自动值守", self.auto_patrol_enabled)

        self.patrol_interval = QDoubleSpinBox()
        self.patrol_interval.setRange(0.5, 5.0)
        self.patrol_interval.setSingleStep(0.1)
        self.patrol_interval.setSuffix(" 秒")
        self.patrol_interval.setValue(pres.patrol_interval_sec)
        patrol_form.addRow("值守采样间隔", self.patrol_interval)

        page.addWidget(patrol_frame)

        # 分区 3：设备与快捷键。设置页不得为枚举设备而打开摄像头；
        # 实际可用性只在主人注册或用户明确开始值守时验证。
        device_frame, device_form = _make_section(
            "设备与快捷键",
            "此页面不会探测或打开摄像头；所选设备仅在注册或开始值守时验证。",
        )

        self.camera_index = QComboBox()
        camera_indices = sorted({*range(5), cam.device_index})
        for index in camera_indices:
            self.camera_index.addItem(f"摄像头 {index}", index)
        selected = self.camera_index.findData(cam.device_index)
        self.camera_index.setCurrentIndex(max(0, selected))
        device_form.addRow("摄像头", self.camera_index)

        self.stealth_hotkey_edit = QKeySequenceEdit()
        self.stealth_hotkey_edit.setKeySequence(
            QKeySequence(pet.stealth_hotkey)
        )
        device_form.addRow("老板键（全局隐藏/恢复）", self.stealth_hotkey_edit)

        page.addWidget(device_frame)
        page.addStretch(1)

        self._tabs.addTab(tab, "通用")

    # ------------------------------------------------------------------ #
    # 破坏性操作
    # ------------------------------------------------------------------ #

    def _confirm_withdraw_patrol_consent(self) -> None:
        """二次确认后才请求应用删除人脸、证据和同意记录。"""
        result = QMessageBox.warning(
            self,
            "撤回值守同意？",
            "这会立即停止采集，并永久删除本机的主人特征与全部证据。\n"
            "此操作无法撤销。",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if result != QMessageBox.Yes:
            return
        self._patrol_consent_withdrawn = True
        self.withdraw_patrol_consent_button.setEnabled(False)
        self.withdraw_patrol_consent_button.setText("正在撤回并删除…")
        self.withdraw_patrol_consent_requested.emit()

    def apply_withdrawal_result(self, success: bool, message: str) -> None:
        """仅按应用层实际结果显示成功；失败时保留可重试入口。"""
        if success:
            self.withdraw_patrol_consent_button.setEnabled(False)
            self.withdraw_patrol_consent_button.setText("已撤回并删除本地数据")
            QMessageBox.information(self, "已撤回", message)
            return

        self.withdraw_patrol_consent_button.setEnabled(True)
        self.withdraw_patrol_consent_button.setText("重试删除残留数据…")
        QMessageBox.critical(self, "撤回未完全完成", message)

    @property
    def patrol_consent_withdrawn(self) -> bool:
        """撤回后拒绝用当前窗口中的旧值覆盖已保存的安全配置。"""
        return self._patrol_consent_withdrawn

    # ------------------------------------------------------------------ #
    # 返回所有配置值
    # ------------------------------------------------------------------ #

    def values(self) -> dict:
        """返回所有配置值（用于更新 AppConfig）。

        返回扁平字典，key 对应各子配置 dataclass 字段名。
        """
        selected_role = str(self.role_selector.currentData())
        keep_custom = selected_role == "__custom__"
        return {
            # 安防
            "face_match_threshold": round(self.face_threshold.value(), 3),
            "stranger_cooldown_sec": self.stranger_cooldown.value(),
            "video_clip_sec": self.video_clip_sec.value(),
            "retention_days": self.retention_days.value(),
            "blur_stranger_faces": self.blur_stranger.isChecked(),
            "motion_recording_enabled": self.motion_recording_enabled.isChecked(),
            # 通用
            "idle_threshold_sec": self.away_threshold.value(),
            "auto_patrol_enabled": self.auto_patrol_enabled.isChecked(),
            "patrol_interval_sec": round(self.patrol_interval.value(), 1),
            "device_index": int(self.camera_index.currentData()),
            "role_id": self._config.pet.role_id if keep_custom else selected_role,
            "assets_dir": self._config.pet.assets_dir if keep_custom else "",
            # 老板键
            "stealth_hotkey": self.stealth_hotkey_edit.keySequence().toString(),
        }
