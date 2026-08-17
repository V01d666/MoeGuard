"""应用组装与生命周期管理。

MoeGuardApp 负责把桌宠形象、安防核心、状态机、UI 等子系统装配在一起，
并管理它们的启动顺序与退出时资源释放。

信号接线（M1+M2+M3）：
- lock_monitor.screen_locked -> state_machine.on_lock_screen
- lock_monitor.screen_unlocked -> state_machine.on_unlock
- state_machine.patrol_started -> camera.start + feedback.on_patrol_start +
  tray.set_state(True)
- state_machine.patrol_ended -> camera.stop + feedback.greet_return +
  tray.set_state(False) + db.log_patrol_session
- state_machine.stranger_detected -> evidence.record + db.log_incident +
  feedback.alert_stranger
- state_machine.owner_greeted -> feedback 欢迎预热（不改状态）
- camera.frame_ready -> 人脸检测 worker -> owner.classify -> 状态机事件
- tray.toggle_patrol -> state_machine.on_manual_start/on_manual_stop
- tray.quit_requested -> app.quit
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace as dc_replace
from datetime import datetime

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLabel

from moeguard.config import (
    PATROL_CONSENT_VERSION,
    AppConfig,
    DialogueConfig,
    PetConfig,
)
from moeguard.core.presence import LockScreenMonitor
from moeguard.core.state_machine import StateMachine
from moeguard.pet.feedback import FeedbackController
from moeguard.pet.frame_animation import FrameAnimationController
from moeguard.pet.role_assets import (
    RoleInteractionProfile,
    load_runtime_role_actions,
    resolve_role_root,
)
from moeguard.security.camera import CameraCapture
from moeguard.security.evidence import EvidenceRecorder, EvidenceResult
from moeguard.security.face import FaceRecognizer
from moeguard.security.motion import MotionDetector
from moeguard.security.owner import OwnerProfile
from moeguard.storage.db import Database
from moeguard.storage.evidence_store import EvidenceStore
from moeguard.storage.owner_profile import OwnerProfileStore
from moeguard.ui import theme
from moeguard.ui.evidence_dialog import EvidenceDialog
from moeguard.ui.onboarding import OnboardingBubble
from moeguard.ui.pet_window import PetWindow
from moeguard.ui.settings_dialog import SettingsDialog
from moeguard.ui.tray import TrayIcon
from moeguard.utils.crypto import CryptoBox
from moeguard.utils.logging import setup_logging
from moeguard.utils.paths import (
    CRYPTO_KEY_PATH,
    DB_PATH,
    EVIDENCE_DIR,
    OWNER_EMBEDDINGS_PATH,
    ensure_dirs,
)

logger = logging.getLogger(__name__)

class _FaceDetectWorker(QThread):
    """人脸检测工作线程（PRD §7.2: 摄像头采集与检测严禁主线程）。

    接收摄像头帧，在后台线程执行人脸检测与主人/陌生人分类，
    通过信号返回结果。
    """

    # (owner_detected, stranger_detected)
    classify_result = Signal(bool, bool)
    # 人脸检测/分类异常（M4.5 dogfood 埋点: 识别失败计数）
    classify_failed = Signal()
    motion_detected = Signal(float)  # 相邻帧中发生变化的像素比例

    def __init__(self, owner: OwnerProfile, parent=None) -> None:
        super().__init__(parent)
        self._owner = owner
        self._frame = None
        self._running = False
        self._motion_detector = MotionDetector()
        self._motion_recording_enabled = False

    def set_motion_recording_enabled(self, enabled: bool) -> None:
        """开关变更时重置基线，避免把切换瞬间当成运动。"""
        self._motion_recording_enabled = enabled
        self._motion_detector.reset()

    def reset_motion_baseline(self) -> None:
        """每次值守会话都从稳定首帧重新建立运动基线。"""
        self._motion_detector.reset()

    def process_frame(self, frame) -> None:
        """提交一帧进行检测（非阻塞，覆盖上一帧）。"""
        self._frame = frame
        if not self._running:
            self._running = True
            self.start()

    def run(self) -> None:  # noqa: C901
        """检测循环。"""
        while self._running:
            if self._frame is not None:
                frame = self._frame
                self._frame = None
                try:
                    if self._motion_recording_enabled:
                        moving, ratio = self._motion_detector.is_motion(frame)
                        if moving:
                            self.motion_detected.emit(ratio)
                    owner_detected, stranger_detected = self._owner.classify(frame)
                    self.classify_result.emit(owner_detected, stranger_detected)
                except Exception as exc:
                    logger.exception("人脸检测异常: %s", exc)
                    self.classify_failed.emit()
            else:
                self.msleep(50)

        self._running = False

    def stop(self) -> None:
        """停止检测线程。"""
        self._running = False
        self.wait(3000)


class _OwnerRegisterWorker(QThread):
    """后台线程：引导中注册主人脸（人脸检测 + 特征提取）。"""

    finished = Signal(bool)

    def __init__(self, owner, frame, parent=None) -> None:
        super().__init__(parent)
        self._owner = owner
        self._frame = frame

    def run(self) -> None:
        try:
            success = self._owner.register(self._frame)
            self.finished.emit(success)
        except Exception:
            self.finished.emit(False)


class MoeGuardApp(QObject):
    """萌卫应用主控。

    装配所有子系统，接线信号，管理生命周期。

    信号:
        ready: 应用启动完成。
    """

    ready = Signal()
    withdrawal_completed = Signal(bool, str)

    def __init__(self) -> None:
        super().__init__()
        self._config: AppConfig | None = None
        self._db: Database | None = None
        self._crypto: CryptoBox | None = None
        self._owner_store: OwnerProfileStore | None = None
        self._recognizer: FaceRecognizer | None = None
        self._owner: OwnerProfile | None = None
        self._camera: CameraCapture | None = None
        self._evidence: EvidenceRecorder | None = None
        self._evidence_store: EvidenceStore | None = None
        self._state_machine: StateMachine | None = None
        self._pet_window: PetWindow | None = None
        self._frame_controller: FrameAnimationController | None = None
        self._feedback: FeedbackController | None = None
        self._tray: TrayIcon | None = None
        self._lock_monitor: LockScreenMonitor | None = None
        self._face_worker: _FaceDetectWorker | None = None
        self._onboarding: OnboardingBubble | None = None
        self._pet_visible: bool = True
        self._patrol_start_time: float = 0.0
        self._patrol_trigger: str = ""
        self._patrol_active: bool = False
        self._patrol_start_pending: bool = False
        self._patrol_failure_reason: str | None = None
        self._patrol_banner: QLabel | None = None
        self._pending_evidence_kind: str | None = None
        self._pending_interruption_notice: str | None = None
        self._evidence_maintenance_timer: QTimer | None = None
        self._pending_maintenance_notice: str | None = None

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #

    @property
    def config(self) -> AppConfig:
        """当前配置。"""
        return self._config or AppConfig()

    @property
    def state_machine(self) -> StateMachine:
        """状态机。"""
        assert self._state_machine is not None
        return self._state_machine

    @property
    def pet_window(self) -> PetWindow:
        """桌宠窗口。"""
        assert self._pet_window is not None
        return self._pet_window

    def _persist_config(self, reason: str, *, notify: bool = True) -> bool:
        """原子保存配置，并在运行时失败时给出可见反馈。"""
        assert self._config is not None
        if AppConfig.save(self._config):
            return True

        logger.error("配置未保存（%s）", reason)
        if notify and self._tray is not None:
            self._tray.show_message(
                "配置未保存",
                f"{reason}仅在本次运行生效；请检查 ~/.moeguard 目录权限后重试。",
            )
        return False

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动应用：加载配置、恢复状态、进入陪伴模式。"""
        # 1. 加载配置
        self._config = AppConfig.load()
        logger.info("配置加载完成")

        # 2. 确保目录存在
        ensure_dirs()

        # 3. 数据库
        self._db = Database(DB_PATH)
        self._db.connect()
        logger.info("数据库已连接: %s", DB_PATH)

        # 4. 加密存储
        self._crypto = CryptoBox(CRYPTO_KEY_PATH)

        # 5. 主人特征存储
        self._owner_store = OwnerProfileStore(OWNER_EMBEDDINGS_PATH, self._crypto)

        # 6. 人脸识别器
        self._recognizer = FaceRecognizer(config=self._config.security)

        # 7. 主人档案
        self._owner = OwnerProfile(self._config.security, self._recognizer)
        owner_embeddings = self._owner_store.load()
        self._owner.load(owner_embeddings)
        if self._owner.is_registered():
            logger.info("主人特征已恢复（%d 个样本）", len(owner_embeddings))
        else:
            logger.info("主人未注册（首次启动或跳过引导）")

        # 8. 摄像头
        self._camera = CameraCapture()

        # 9. 证据录制
        self._evidence = EvidenceRecorder(
            evidence_config=self._config.evidence,
            security_config=self._config.security,
            recognizer=self._recognizer,
        )
        self._evidence_store = EvidenceStore(
            EVIDENCE_DIR, self._config.evidence.retention_days
        )
        self._run_evidence_maintenance()

        # 10. 状态机
        self._state_machine = StateMachine()

        # 11. 帧动画控制器 + 桌宠窗口
        self._frame_controller = FrameAnimationController()
        role_profile = self._load_pet_frames(self._config.pet)
        self._pet_window = PetWindow(
            self._frame_controller,
            fps=self._config.pet.fps,
            edge_reveal_fraction=role_profile.edge_reveal_fraction,
        )

        # 恢复缩放尺寸
        if self._config.pet.saved_width != self._config.pet.default_width:
            self._pet_window.resize(
                self._config.pet.saved_width, self._config.pet.saved_height
            )

        # 缩放持久化回调
        self._pet_window.set_scale_persist_callback(self._on_scale_changed)

        # 12. 反馈控制器
        self._feedback = FeedbackController(
            self._pet_window,
            self._frame_controller,
            click_lines=role_profile.click_lines,
        )

        # 13. 系统托盘（生成简约图标：蓝盾徽记）
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#3399dd"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, 28, 28)
        painter.end()
        self._tray = TrayIcon(QIcon(pixmap))
        self._tray.set_state(False)  # 初始：陪伴模式，"进入值守"可选，"回到陪伴"灰色

        # 14. 锁屏监听
        self._lock_monitor = LockScreenMonitor()
        app = QApplication.instance()
        if app is not None:
            self._lock_monitor.install(app)

        # 15. 人脸检测工作线程
        self._face_worker = _FaceDetectWorker(self._owner)
        self._face_worker.set_motion_recording_enabled(
            self._config.evidence.motion_recording_enabled
        )

        # 16. 接线信号
        self._wire_signals()

        # 17. 启动 idle 动画（先让桌宠可见，引导在桌宠之上弹出）
        self._feedback.on_companion()

        # 18. 首次启动 -> 显示引导（桌宠已在背后运行）
        if not self._owner.is_registered() or not self._has_patrol_consent():
            logger.info("首次启动，显示引导向导")
            self._show_onboarding()

        # 19. 显示桌宠窗口 + 托盘
        self._pet_window.show()
        self._tray.show()

        self._evidence_maintenance_timer = QTimer(self)
        self._evidence_maintenance_timer.timeout.connect(self._run_evidence_maintenance)
        self._evidence_maintenance_timer.start(6 * 60 * 60 * 1000)

        if self._owner_store.last_load_error is not None:
            self._tray.show_message(
                "主人特征无法读取",
                "已安全隔离损坏的本地档案；请重新完成主人注册后再启动值守。",
                8000,
            )
        if self._pending_maintenance_notice is not None:
            self._tray.show_message(
                "证据清理需要重试",
                self._pending_maintenance_notice,
                8000,
            )
            self._pending_maintenance_notice = None

        # 注册老板键
        self._pet_window.register_stealth_hotkey(
            self._config.pet.stealth_hotkey
        )

        self.ready.emit()
        logger.info("萌卫应用启动完成")

    def _run_evidence_maintenance(self) -> None:
        """清理陈旧临时/过期媒体并补偿孤立索引；每六小时执行一次。"""
        assert self._evidence_store is not None
        assert self._db is not None
        try:
            stale_pending = self._evidence_store.cleanup_stale_pending()
            expired_evidence = self._evidence_store.cleanup_expired_paths()
            removed_indices = self._db.delete_incidents_by_evidence(
                [str(path) for path in expired_evidence]
            )
            recovered_indices = self._db.delete_incidents_with_missing_evidence()
            if stale_pending or expired_evidence or recovered_indices:
                logger.info(
                    "证据维护：临时目录 %d 个、过期目录 %d 个、过期索引 %d 条、"
                    "补偿索引 %d 条",
                    len(stale_pending),
                    len(expired_evidence),
                    removed_indices,
                    recovered_indices,
                )
        except Exception as exc:
            logger.exception("证据保留期维护失败: %s", exc)
            message = "未能同步清理过期证据；将于下次维护重试。"
            if self._tray is not None:
                self._tray.show_message("证据清理需要重试", message, 8000)
            else:
                self._pending_maintenance_notice = message

    def _load_pet_frames(self, pet_config: PetConfig) -> RoleInteractionProfile:
        """加载桌宠帧素材。

        自定义 assets_dir 非空时优先加载；否则按 role_id 加载内置角色。
        每个动作子目录下包含编号 PNG 帧文件。

        Args:
            pet_config: 桌宠配置。
        """
        assert self._frame_controller is not None
        fc = self._frame_controller

        assets_root = resolve_role_root(pet_config.role_id, pet_config.assets_dir)
        if assets_root is None:
            logger.warning(
                "桌宠角色不可用: role_id=%s assets_dir=%s",
                pet_config.role_id,
                pet_config.assets_dir,
            )
            return RoleInteractionProfile()

        logger.info("加载桌宠角色 %s: %s", pet_config.role_id, assets_root)
        fc.clear_actions()
        return load_runtime_role_actions(fc, assets_root, fps=pet_config.fps)

    def _wire_signals(self) -> None:
        """接线所有子系统信号。"""
        assert self._lock_monitor is not None
        assert self._state_machine is not None
        assert self._camera is not None
        assert self._feedback is not None
        assert self._tray is not None
        assert self._pet_window is not None
        assert self._face_worker is not None

        # 锁屏 -> 状态机
        self._lock_monitor.screen_locked.connect(self._on_lock_screen)
        self._lock_monitor.screen_unlocked.connect(self._on_unlock)

        # 状态机 -> 子系统
        self._state_machine.patrol_started.connect(self._on_patrol_started)
        self._state_machine.patrol_ended.connect(self._on_patrol_ended)
        self._state_machine.stranger_detected.connect(self._on_stranger_detected)
        self._state_machine.owner_greeted.connect(self._on_owner_greeted)

        # 摄像头帧 -> 人脸检测
        self._camera.frame_ready.connect(self._face_worker.process_frame)
        self._camera.camera_ready.connect(self._on_camera_ready)
        self._camera.camera_failed.connect(self._on_camera_failed)
        self._camera.capture_gap_detected.connect(self._on_camera_capture_gap)
        self._face_worker.classify_result.connect(self._on_classify_result)
        self._face_worker.classify_failed.connect(self._on_classify_failed)
        self._face_worker.motion_detected.connect(self._on_motion_detected)

        # 后台证据录制完成后，统一在 GUI 线程提交目录、数据库与计数。
        assert self._evidence is not None
        self._evidence.recording_finished.connect(self._on_evidence_recording_finished)

        # 托盘 -> 状态机 / 应用
        self._tray.toggle_patrol.connect(self._on_tray_toggle_patrol)
        self._tray.quit_requested.connect(self.quit)
        self._tray.open_settings.connect(self._on_open_settings)
        self._tray.open_security_setup.connect(self._on_open_security_setup)
        self._tray.open_evidence.connect(self._on_open_evidence)
        self._tray.tray_activated.connect(self._on_tray_activated)

        # 桌宠交互 -> 反馈 + dogfood 埋点
        self._pet_window.clicked.connect(self._feedback.on_click)
        self._pet_window.clicked.connect(self._on_pet_clicked)
        self._pet_window.drag_started.connect(self._feedback.on_drag_start)
        self._pet_window.drag_ended.connect(self._feedback.on_drag_end)
        self._pet_window.edge_snapped.connect(self._feedback.on_edge_snap)
        self._pet_window.animation_finished.connect(self._on_animation_finished)
        self._pet_window.stealth_toggled.connect(self._on_stealth_toggled)

        # 桌宠右键菜单 -> 应用
        self._pet_window.quit_requested.connect(self.quit)
        self._pet_window.settings_requested.connect(self._on_open_settings)
        self._pet_window.toggle_patrol_requested.connect(self._on_pet_toggle_patrol)

        # 反馈消息 -> 桌宠气泡
        self._feedback.message.connect(self._pet_window.show_message)

    # ------------------------------------------------------------------ #
    # 信号处理
    # ------------------------------------------------------------------ #

    def _on_lock_screen(self) -> None:
        """锁屏事件 -> 进入值守。"""
        assert self._config is not None
        if not self._config.presence.auto_patrol_enabled:
            logger.info("自动值守未启用，锁屏后保持纯桌宠模式")
            return
        if not self._can_start_patrol():
            logger.warning("未完成同意或主人注册，忽略自动值守请求")
            return

        if not self._config.evidence.show_patrol_banner:
            logger.warning("采集状态提示被关闭，拒绝开始值守")
            self._tray.show_message("无法开始值守", "值守采集提示必须保持显示。")
            assert self._state_machine is not None
            self._state_machine.on_manual_stop()
            return
        self._patrol_trigger = "lock_screen"
        assert self._state_machine is not None
        self._state_machine.on_lock_screen()

    def _on_patrol_started(self) -> None:
        """进入值守：启动摄像头 + 反馈 + 托盘状态。"""
        assert self._config is not None
        assert self._camera is not None
        assert self._feedback is not None
        assert self._tray is not None

        if not self._can_start_patrol():
            logger.warning("未完成同意或主人注册，拒绝进入值守")
            self._tray.show_message("无法开始值守", "请先完成风险同意和主人注册。")
            assert self._state_machine is not None
            self._state_machine.on_manual_stop()
            return

        if not self._config.evidence.show_patrol_banner:
            logger.warning("采集状态提示被关闭，拒绝进入值守")
            self._tray.show_message("无法开始值守", "值守采集提示必须保持显示。")
            assert self._state_machine is not None
            self._state_machine.on_manual_stop()
            return

        self._patrol_start_time = time.time()
        self._patrol_failure_reason = None
        assert self._face_worker is not None
        self._face_worker.reset_motion_baseline()
        started = self._camera.start(
            device_index=self._config.camera.device_index,
            interval_ms=self._config.camera.interval_ms,
        )
        if not started:
            logger.error("摄像头无法启动，回滚值守状态")
            self._tray.show_message(
                "无法开始值守",
                self._camera.last_error or "摄像头不可用或正被其他程序占用。",
            )
            assert self._state_machine is not None
            self._state_machine.on_manual_stop()
            return
        self._patrol_start_pending = True
        logger.info("摄像头已打开，等待首帧确认值守（触发: %s）", self._patrol_trigger)

    def _on_camera_ready(self) -> None:
        """只有收到了首帧，才展示值守已启动和采集提示。"""
        assert self._state_machine is not None
        assert self._feedback is not None
        assert self._tray is not None
        if not self._patrol_start_pending or not self._state_machine.is_patrolling:
            return
        self._patrol_start_pending = False
        self._patrol_active = True
        self._feedback.on_patrol_start()
        self._tray.set_state(True)
        self._show_patrol_banner()
        if self._db is not None:
            self._db.bump_usage(self._today(), patrol=1)
        logger.info("值守已确认启动（收到摄像头首帧）")

    def _on_patrol_ended(self) -> None:
        """退出值守：停止摄像头 + 欢迎汇报 + 托盘状态 + 记录会话。"""
        assert self._camera is not None
        assert self._feedback is not None
        assert self._tray is not None
        assert self._state_machine is not None
        assert self._db is not None

        was_active = self._patrol_active
        failed_reason = self._patrol_failure_reason
        self._patrol_active = False
        self._patrol_start_pending = False
        self._patrol_failure_reason = None
        if self._evidence is not None:
            self._evidence.cancel()
        self._camera.stop()
        self._hide_patrol_banner()
        self._tray.set_state(False)

        if not was_active or failed_reason is not None:
            # 摄像头未真正可用或中途异常时，不能伪装成“完成了一次值守”。
            self._feedback.on_companion()
            logger.info("值守异常结束，不播放欢迎动画: %s", failed_reason or "未启动")
            return

        had_incidents = self._state_machine.incident_count > 0
        self._feedback.greet_return(had_incidents)

        # 记录值守会话
        end_ts = time.time()
        self._db.log_patrol_session(
            start_ts=self._patrol_start_time,
            end_ts=end_ts,
            incident_count=self._state_machine.incident_count,
            trigger=self._patrol_trigger or "manual",
        )
        # dogfood 埋点：值守时长
        self._db.bump_usage(
            self._today(),
            patrol_seconds=end_ts - self._patrol_start_time,
        )
        logger.info(
            "值守已结束（持续 %.1fs，事件 %d 次）",
            end_ts - self._patrol_start_time,
            self._state_machine.incident_count,
        )

    def _on_stranger_detected(self) -> None:
        """陌生人检测：后台录制；成功后才落 DB/目录/计数。"""
        self._start_evidence_recording("stranger")

    def _on_motion_detected(self, changed_ratio: float) -> None:
        """可选运动取证：不改变值守状态，也不声称这是活体识别。"""
        assert self._config is not None
        assert self._state_machine is not None
        if (
            self._config.evidence.motion_recording_enabled
            and self._state_machine.is_patrolling
        ):
            logger.info("检测到画面运动（变化比例 %.1f%%）", changed_ratio * 100)
            self._start_evidence_recording("motion")

    def _start_evidence_recording(self, kind: str) -> None:
        """排队一次证据录制；仅成功排队的事件才可在完成后提交。"""
        assert self._camera is not None
        assert self._evidence is not None
        assert self._evidence_store is not None
        if not self._evidence.start_recording(self._camera, EVIDENCE_DIR):
            logger.debug("%s 证据录制被冷却或已有任务抑制", kind)
            return
        self._pending_evidence_kind = kind

    def _discard_pending_evidence(self, pending_dir, context: str) -> bool:
        """严格清理未提交证据；失败时保留可见告警而非静默吞错。"""
        assert self._evidence_store is not None
        try:
            self._evidence_store.discard_pending(pending_dir)
            return True
        except Exception as exc:
            logger.exception("%s时清理未提交证据失败: %s", context, exc)
            if self._tray is not None:
                self._tray.show_message(
                    "临时证据清理失败",
                    "有未提交的本地证据未能删除；请退出后检查 ~/.moeguard/evidence/.pending。",
                    10000,
                )
            return False

    def _on_evidence_recording_finished(self, result: EvidenceResult) -> None:
        """后台证据写盘后，原子提交可见目录、DB 与所有计数。"""
        assert self._evidence is not None
        assert self._evidence_store is not None
        assert self._db is not None
        assert self._feedback is not None
        assert self._state_machine is not None
        kind = self._pending_evidence_kind or "stranger"
        self._pending_evidence_kind = None

        # 解锁/手动停止优先于录制完成：退出值守后的临时结果不得补写事件。
        if not self._state_machine.is_patrolling:
            self._discard_pending_evidence(result.pending_dir, "结束值守")
            self._evidence.discard_event()
            logger.info("值守已结束，丢弃未提交的后台证据")
            return

        if not result.succeeded:
            self._discard_pending_evidence(result.pending_dir, "录制失败")
            self._evidence.discard_event()
            logger.warning("陌生人证据未提交: %s", result.error)
            return

        event_dir = None
        committed = False
        try:
            assert result.pending_dir is not None
            event_dir = self._evidence_store.commit_pending(
                result.pending_dir, result.ts, result.event_uuid
            )
            incident_id, committed = self._db.commit_evidence_event(
                event_uuid=result.event_uuid,
                ts=result.ts,
                kind=kind,
                evidence=str(event_dir),
                summary=(
                    "值守中检测到陌生人"
                    if kind == "stranger"
                    else "值守中检测到画面运动"
                ),
                usage_date=self._today(),
            )
        except Exception as exc:
            logger.exception("提交陌生人事件失败: %s", exc)
            try:
                if event_dir is not None:
                    self._evidence_store.delete_event(event_dir)
                else:
                    self._discard_pending_evidence(result.pending_dir, "提交失败")
            except Exception as cleanup_exc:
                logger.exception("提交失败后的证据补偿删除失败: %s", cleanup_exc)
                if self._tray is not None:
                    self._tray.show_message(
                        "证据补偿删除失败",
                        "事件未提交，但本地媒体未能删除；请在证据文件夹中检查残留。",
                        10000,
                    )
            self._evidence.discard_event()
            return

        if not committed:
            self._evidence.discard_event()
            logger.info("忽略重复证据事件（id=%d）", incident_id)
            return

        self._state_machine.confirm_incident()
        self._evidence.commit_event()
        try:
            self._feedback.alert_stranger()
        except Exception:
            logger.exception("证据事件已提交，但提醒播放失败（id=%d）", incident_id)
        logger.warning("%s 事件已提交（id=%d）", kind, incident_id)

    def _on_camera_failed(self, reason: str) -> None:
        """摄像头中断时立即退出值守，禁止保留“正在值守”的假象。"""
        assert self._state_machine is not None
        assert self._tray is not None
        if self._state_machine.is_patrolling:
            logger.error("摄像头故障，结束值守: %s", reason)
            self._patrol_failure_reason = reason
            if self._pending_interruption_notice is None:
                self._pending_interruption_notice = f"值守已停止：{reason}"
            self._tray.show_message("值守已停止", f"{reason}。请检查摄像头后重试。")
            self._state_machine.on_manual_stop()

    def _on_camera_capture_gap(self, gap_seconds: float) -> None:
        """从睡眠/暂停恢复后提示：这段时间不应计为正常值守。"""
        logger.warning("值守采集出现 %.1f 秒时间线空洞，可能被系统暂停", gap_seconds)
        self._pending_interruption_notice = (
            f"值守曾中断约 {gap_seconds:.0f} 秒；该时段未计入正常值守。"
        )
        if self._tray is not None:
            self._tray.show_message(
                "值守可能中断",
                f"检测到约 {gap_seconds:.0f} 秒采集空洞；该时段不计为正常值守。",
                8000,
            )

    def _on_unlock(self) -> None:
        """解锁后恢复陪伴，并补显锁屏期间可能看不到的中断告知。"""
        assert self._state_machine is not None
        self._state_machine.on_unlock()
        if self._pending_interruption_notice is not None:
            assert self._feedback is not None
            self._feedback.message.emit(self._pending_interruption_notice)
            self._pending_interruption_notice = None

    def _show_patrol_banner(self) -> None:
        """显示独立于桌宠的采集提示；老板键不会隐藏它。"""
        if self._patrol_banner is None:
            from PySide6.QtCore import Qt

            banner = QLabel("● 萌卫正在使用摄像头值守", None)
            banner.setWindowFlags(
                Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            )
            # 高对比安全红：摄像头使用中的明确告知；样式取自主题层。
            banner.setStyleSheet(
                f"background: {theme.PATROL_BANNER_BG};"
                f" color: {theme.PATROL_BANNER_TEXT};"
                f" border-radius: {theme.RADIUS_MEDIUM}px;"
                f" padding: 8px 14px; {theme.font_css(13, bold=True)}"
            )
            self._patrol_banner = banner
        self._patrol_banner.adjustSize()
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self._patrol_banner.move(
                area.right() - self._patrol_banner.width() - 16,
                area.top() + 16,
            )
        self._patrol_banner.show()

    def _hide_patrol_banner(self) -> None:
        if self._patrol_banner is not None:
            self._patrol_banner.hide()

    def _on_stealth_toggled(self, enabled: bool) -> None:
        """老板键不得吞掉采集提示；值守中隐藏桌宠即停止采集。"""
        if enabled and self._state_machine is not None and self._state_machine.is_patrolling:
            logger.warning("老板键隐藏桌宠时停止值守，避免隐藏采集状态")
            self._state_machine.on_manual_stop()

    def _on_owner_greeted(self, is_first: bool) -> None:
        """主人识别（值守中）：欢迎预热，不改变状态。"""
        assert self._feedback is not None
        if is_first:
            logger.info("值守中识别到主人，欢迎预热")
            # 预热 welcome 动作（不弹气泡，等解锁后正式欢迎）

    def _on_classify_result(self, owner_detected: bool, stranger_detected: bool) -> None:
        """人脸分类结果 -> 状态机事件。"""
        assert self._state_machine is not None
        if stranger_detected:
            self._state_machine.on_stranger()
        if owner_detected:
            self._state_machine.on_owner_recognized()

    def _on_tray_toggle_patrol(self, start: bool) -> None:
        """托盘手动切换值守。"""
        assert self._state_machine is not None
        if start:
            if not self._can_start_patrol():
                assert self._tray is not None
                self._tray.show_message("无法开始值守", "请先完成风险同意和主人注册。")
                return
            self._patrol_trigger = "manual"
            self._state_machine.on_manual_start()
        else:
            self._state_machine.on_manual_stop()

    def _on_animation_finished(self, action_name: str) -> None:
        """非循环动画播放完毕，恢复到正确的底态动画。"""
        assert self._feedback is not None
        if self._feedback.on_animation_finished(action_name):
            return
        if action_name in ("click_reaction", "dragging", "welcome", "notice"):
            self._feedback.restore_animation()

    # ------------------------------------------------------------------ #
    # dogfood 埋点 handlers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _today() -> str:
        """返回当天日期字符串 'YYYY-MM-DD'（本地时间）。"""
        return datetime.now().strftime("%Y-%m-%d")

    def _on_pet_clicked(self) -> None:
        """桌宠点击 -> dogfood 埋点计数。"""
        if self._db is not None:
            self._db.bump_usage(self._today(), clicks=1)

    def _on_classify_failed(self) -> None:
        """人脸检测异常 -> dogfood 埋点计数。"""
        if self._db is not None:
            self._db.bump_usage(self._today(), recognize_errors=1)

    def _on_open_settings(self) -> None:
        """打开设置面板，用户确认后更新配置并持久化。"""
        assert self._config is not None
        logger.info("打开设置面板")

        dialog = SettingsDialog(self._config, parent=None)
        dialog.withdraw_patrol_consent_requested.connect(self._withdraw_patrol_consent)
        self.withdrawal_completed.connect(dialog.apply_withdrawal_result)
        dialog.manage_evidence_requested.connect(self._on_open_evidence_manager)
        try:
            accepted = dialog.exec() == SettingsDialog.Accepted
        finally:
            self.withdrawal_completed.disconnect(dialog.apply_withdrawal_result)
        if accepted:
            if dialog.patrol_consent_withdrawn:
                logger.info("撤回后忽略设置窗口中的旧值，保留已删除状态")
                return
            vals = dialog.values()
            self._apply_settings(vals)
            self._persist_config("设置修改")
            # 老板键变更后立即重新注册
            if "stealth_hotkey" in vals:
                assert self._pet_window is not None
                self._pet_window.register_stealth_hotkey(vals["stealth_hotkey"])
            logger.info("设置已保存")

    def _on_open_security_setup(self) -> None:
        """允许拒绝过引导的用户从托盘重新作出主动同意。"""
        assert self._state_machine is not None
        assert self._tray is not None
        if self._state_machine.is_patrolling:
            self._tray.show_message("请先结束值守", "回到陪伴模式后可重新进行值守设置。")
            return
        if self._onboarding is not None and self._onboarding.isVisible():
            return
        self._show_onboarding()

    def _on_open_evidence_manager(self) -> None:
        """从设置打开可撤销/可见的本地证据管理窗口。"""
        assert self._evidence_store is not None
        assert self._db is not None
        dialog = EvidenceDialog(self._evidence_store, self._db)
        dialog.exec()

    def _apply_settings(self, vals: dict) -> None:
        """将设置对话框返回的值应用到 AppConfig（创建新的 frozen dataclass）。"""
        assert self._config is not None

        old_role_source = (self._config.pet.role_id, self._config.pet.assets_dir)
        self._config = dc_replace(
            self._config,
            security=dc_replace(
                self._config.security,
                face_match_threshold=vals["face_match_threshold"],
                stranger_cooldown_sec=vals["stranger_cooldown_sec"],
            ),
            evidence=dc_replace(
                self._config.evidence,
                video_clip_sec=vals["video_clip_sec"],
                retention_days=vals["retention_days"],
                blur_stranger_faces=vals["blur_stranger_faces"],
                motion_recording_enabled=vals["motion_recording_enabled"],
            ),
            dialogue=DialogueConfig(),
            presence=dc_replace(
                self._config.presence,
                auto_patrol_enabled=vals["auto_patrol_enabled"],
                idle_threshold_sec=vals["idle_threshold_sec"],
                patrol_interval_sec=vals["patrol_interval_sec"],
            ),
            camera=dc_replace(
                self._config.camera,
                device_index=vals["device_index"],
            ),
            pet=dc_replace(
                self._config.pet,
                role_id=vals["role_id"],
                assets_dir=vals["assets_dir"],
                stealth_hotkey=vals["stealth_hotkey"],
            ),
        )
        if (vals["role_id"], vals["assets_dir"]) != old_role_source:
            profile = self._load_pet_frames(self._config.pet)
            assert self._pet_window is not None
            self._pet_window.set_edge_reveal_fraction(profile.edge_reveal_fraction)
            assert self._feedback is not None
            self._feedback.set_click_lines(profile.click_lines)
            self._feedback.restore_animation()
        assert self._face_worker is not None
        self._face_worker.set_motion_recording_enabled(
            vals["motion_recording_enabled"]
        )
        assert self._evidence is not None
        self._evidence.update_config(self._config.evidence, self._config.security)
        assert self._evidence_store is not None
        self._evidence_store.update_retention_days(
            self._config.evidence.retention_days
        )

    def _withdraw_patrol_consent(self) -> bool:
        """撤回同意并严格删除值守数据；通过信号回传可重试结果。"""
        assert self._config is not None
        assert self._owner is not None
        assert self._owner_store is not None
        assert self._evidence_store is not None
        assert self._db is not None

        if self._state_machine is not None and self._state_machine.is_patrolling:
            self._state_machine.on_manual_stop()
        recorder_stopped = True
        if self._evidence is not None:
            recorder_stopped = self._evidence.cancel_and_wait()

        self._owner.clear()
        self._config = dc_replace(
            self._config,
            consent=dc_replace(self._config.consent, patrol_consent_granted=False),
            presence=dc_replace(self._config.presence, auto_patrol_enabled=False),
        )
        errors: list[str] = []
        if not self._persist_config("撤回值守同意"):
            errors.append("撤回设置未能写入磁盘")

        deleted_owner = 0
        deleted_evidence = 0
        deleted_incidents = 0
        deleted_usage = 0
        try:
            deleted_owner = self._owner_store.delete()
        except Exception as exc:
            errors.append("主人特征未能全部删除")
            logger.exception("撤回同意时删除主人特征失败: %s", exc)

        evidence_deleted = False
        if not recorder_stopped:
            errors.append("后台证据录制未能及时停止")
            logger.error("撤回同意时后台证据录制停止超时")
        else:
            try:
                deleted_evidence = self._evidence_store.delete_all()
                evidence_deleted = True
            except Exception as exc:
                errors.append("证据文件未能全部删除")
                logger.exception("撤回同意时删除证据失败: %s", exc)

        try:
            if evidence_deleted:
                deleted_incidents = self._db.delete_all_incidents()
            else:
                deleted_incidents = self._db.delete_incidents_with_missing_evidence()
        except Exception as exc:
            errors.append("事件索引未能全部删除")
            logger.exception("撤回同意时删除事件索引失败: %s", exc)

        try:
            deleted_usage = self._db.delete_all_usage()
        except Exception as exc:
            errors.append("使用统计未能全部删除")
            logger.exception("撤回同意时删除使用统计失败: %s", exc)

        success = not errors
        if success:
            message = "已停止采集并删除主人特征、本地证据和相关记录。"
            if self._tray is not None:
                self._tray.show_message("值守同意已撤回", message)
        else:
            message = (
                "采集已停止，但部分设置或本地数据未能完成处理："
                f"{'、'.join(errors)}。请关闭占用文件的程序、检查目录权限后重试。"
            )
            if self._tray is not None:
                self._tray.show_message("撤回未完全完成", message, 10000)
        self.withdrawal_completed.emit(success, message)
        logger.info(
            "值守同意撤回结果 success=%s；主人档案 %d 个、证据目录 %d 个、"
            "事件索引 %d 条、使用统计 %d 条",
            success,
            deleted_owner,
            deleted_evidence,
            deleted_incidents,
            deleted_usage,
        )
        return success

    def _on_open_evidence(self) -> None:
        """用当前平台的文件管理器打开证据目录。"""
        import os
        import platform
        import subprocess

        try:
            EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            if platform.system() == "Windows":
                os.startfile(str(EVIDENCE_DIR))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(EVIDENCE_DIR)])  # noqa: S603, S607
            else:
                subprocess.Popen(["xdg-open", str(EVIDENCE_DIR)])  # noqa: S603, S607
        except Exception as exc:
            logger.warning("无法打开证据目录: %s", exc)
            if self._tray is not None:
                self._tray.show_message("无法打开证据", "请检查证据目录权限后重试。")

    def _on_tray_activated(self, reason: int) -> None:
        """托盘图标激活事件处理。

        Windows 上 QSystemTrayIcon.ActivationReason 实测值:
          Unknown=0, Context=1, DoubleClick=2, Trigger=3, MiddleClick=4

        左键单击或双击 -> 切换桌宠可见性。
        """
        logger.info("托盘激活: reason=%d", reason)
        # DoubleClick=2：仅左键双击切换，防止单击误触
        if reason == 2:
            self._toggle_pet_visibility()

    def _toggle_pet_visibility(self) -> None:
        """切换桌宠显示/隐藏。"""
        assert self._pet_window is not None
        if self._pet_visible:
            self._pet_window.hide_pet()
            self._pet_visible = False
            logger.info("桌宠已隐藏（托盘双击）")
        else:
            self._pet_window.show_pet()
            self._pet_visible = True
            logger.info("桌宠已显示（托盘双击）")

    def _on_pet_toggle_patrol(self) -> None:
        """桌宠右键切换值守/陪伴。"""
        assert self._state_machine is not None
        if self._state_machine.is_patrolling:
            self._state_machine.on_manual_stop()
        else:
            if not self._can_start_patrol():
                assert self._tray is not None
                self._tray.show_message("无法开始值守", "请先完成风险同意和主人注册。")
                return
            self._patrol_trigger = "manual"
            self._state_machine.on_manual_start()

    def _has_patrol_consent(self) -> bool:
        """当前配置是否含本版本的主动值守同意。"""
        return bool(
            self._config
            and self._config.consent.patrol_consent_granted
            and self._config.consent.patrol_consent_version == PATROL_CONSENT_VERSION
        )

    def _can_start_patrol(self) -> bool:
        """值守的最低前提：单独同意与可用主人档案缺一不可。"""
        return bool(
            self._has_patrol_consent()
            and self._owner is not None
            and self._owner.is_registered()
        )

    def _on_scale_changed(self, width: int, height: int) -> None:
        """桌宠缩放变化 -> 持久化到配置。"""
        assert self._config is not None

        self._config = dc_replace(
            self._config,
            pet=dc_replace(
                self._config.pet,
                saved_width=width,
                saved_height=height,
            ),
        )
        self._persist_config("桌宠尺寸")

    # ------------------------------------------------------------------ #
    # 陪伴模式
    # ------------------------------------------------------------------ #

    def _enter_companion_mode(self) -> None:
        """进入陪伴模式。"""
        assert self._feedback is not None
        self._feedback.on_companion()
        logger.info("进入陪伴模式")

    # ------------------------------------------------------------------ #
    # 引导
    # ------------------------------------------------------------------ #

    def _show_onboarding(self) -> None:
        """显示首次启动气泡式引导（悬浮在桌宠上方，与桌宠绑定移动）。"""
        assert self._pet_window is not None

        self._onboarding = OnboardingBubble(self._pet_window)
        self._onboarding.finished.connect(self._on_onboarding_finished)
        self._onboarding.consent_granted.connect(self._on_patrol_consent_granted)
        self._onboarding.consent_declined.connect(self._on_patrol_consent_declined)
        self._onboarding.preview_requested.connect(self._on_preview_requested)
        self._onboarding.register_owner.connect(self._on_register_owner_from_onboarding)
        self._onboarding.show()
        logger.info("气泡式引导已显示")

    def _on_preview_requested(self) -> None:
        """引导进入 Step 3：启动摄像头预览（不阻塞）。"""
        assert self._camera is not None
        assert self._config is not None

        logger.info("引导中：启动摄像头预览")

        if not self._camera.is_opened():
            self._camera.start(
                device_index=self._config.camera.device_index,
                interval_ms=200,  # 预览用高频采样
            )

        # 定时器持续刷新预览
        from PySide6.QtCore import QTimer
        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._tick_preview)
        self._preview_timer.start(100)  # 每 100ms 刷新预览

    def _tick_preview(self) -> None:
        """定时刷新摄像头预览帧。"""
        if self._camera is None or self._onboarding is None:
            return
        frame = self._camera.grab()
        if frame is not None:
            self._update_onboarding_preview(frame)

    def _on_register_owner_from_onboarding(self) -> None:
        """引导中拍一张：采集当前帧 + 后台注册主人脸（不阻塞 UI）。"""
        assert self._camera is not None

        logger.info("引导中：拍一张注册主人脸")

        # 摄像头已在运行，直接取最新帧；预览保持运行，失败后用户可立刻纠正。
        frame = self._camera.grab()
        if frame is None:
            logger.warning("主人脸注册失败：摄像头采集失败")
            if self._onboarding is not None:
                self._onboarding.registration_result(False, "摄像头不可用，请检查后重试。")
            return

        # 更新预览显示拍到的照片
        self._update_onboarding_preview(frame)

        # 在后台线程注册人脸，避免阻塞 UI
        worker = _OwnerRegisterWorker(self._owner, frame)
        worker.finished.connect(self._on_owner_register_done)
        worker.start()
        # 保存引用防止被 GC
        self._register_worker = worker

    def _on_owner_register_done(self, success: bool) -> None:
        """后台注册完成回调。"""
        if not success:
            logger.warning("主人脸注册失败：未检测到人脸或模型不可用")
            if self._onboarding is not None:
                self._onboarding.registration_result(False)
            return

        assert self._owner is not None
        assert self._owner_store is not None
        try:
            # 完成页只能在 worker 成功且本地持久化成功后显示。
            self._owner_store.save(self._owner.embeddings())
        except Exception as exc:
            logger.exception("主人特征保存失败: %s", exc)
            self._owner.clear()
            if self._onboarding is not None:
                self._onboarding.registration_result(False, "保存主人特征失败，请重试。")
            return
        logger.info("主人脸注册并持久化成功（引导，后台线程）")
        if hasattr(self, "_preview_timer") and self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None
        if self._onboarding is not None:
            self._onboarding.registration_result(True)

    def _update_onboarding_preview(self, frame) -> None:
        """更新引导气泡的摄像头预览。"""
        if self._onboarding is None:
            return
        try:
            import cv2
            from PySide6.QtGui import QImage, QPixmap

            # BGR -> RGB
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            bytes_per_line = ch * w
            q_img = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
            self._onboarding.update_preview(QPixmap.fromImage(q_img))
        except Exception as exc:
            logger.warning("更新引导预览失败: %s", exc)

    def _on_onboarding_finished(self, pet_path: str) -> None:
        """引导完成回调。

        Args:
            pet_path: 选定的形象路径（空=使用默认形象）。
        """
        assert self._owner is not None
        assert self._owner_store is not None

        # 停止引导期间临时启动的摄像头
        if self._camera is not None and self._camera.is_opened():
            # 仅在非值守状态下停止摄像头
            assert self._state_machine is not None
            if not self._state_machine.is_patrolling:
                self._camera.stop()

        self._enter_companion_mode()

    def _on_patrol_consent_granted(self) -> None:
        """保存当前版本的主动同意；只授予值守能力，不自动启动。"""
        assert self._config is not None
        self._config = dc_replace(
            self._config,
            consent=dc_replace(
                self._config.consent,
                patrol_consent_granted=True,
                patrol_consent_version=PATROL_CONSENT_VERSION,
            ),
            presence=dc_replace(self._config.presence, auto_patrol_enabled=False),
        )
        if self._persist_config("值守主动同意"):
            logger.info(
                "已保存值守主动同意（版本=%s）",
                self._config.consent.patrol_consent_version,
            )

    def _on_patrol_consent_declined(self) -> None:
        """拒绝同意后明确保持纯桌宠模式。"""
        assert self._config is not None
        self._config = dc_replace(
            self._config,
            consent=dc_replace(self._config.consent, patrol_consent_granted=False),
            presence=dc_replace(self._config.presence, auto_patrol_enabled=False),
        )
        self._persist_config("拒绝值守同意")
        logger.info("用户拒绝值守同意，保持纯桌宠模式")

    # ------------------------------------------------------------------ #
    # 退出
    # ------------------------------------------------------------------ #

    def quit(self) -> None:
        """优雅退出：释放摄像头、保存配置、关闭 db、释放资源。"""
        logger.info("正在退出萌卫...")

        recorder_stopped = True
        if self._evidence is not None:
            recorder_stopped = self._evidence.cancel_and_wait()
            if not recorder_stopped:
                logger.error("退出时后台证据录制未在时限内停止")
        if recorder_stopped and self._evidence_store is not None:
            try:
                removed_pending = self._evidence_store.delete_all_pending()
                if removed_pending:
                    logger.info("退出时删除未提交证据 %d 个", removed_pending)
            except Exception as exc:
                logger.exception("退出时清理未提交证据失败，将在下次维护重试: %s", exc)

        # 停止摄像头
        if self._camera is not None:
            self._camera.stop()

        # 停止人脸检测线程
        if self._face_worker is not None:
            self._face_worker.stop()

        # 停止锁屏监听
        if self._lock_monitor is not None:
            self._lock_monitor.stop()

        # 注销全局热键
        if self._pet_window is not None:
            self._pet_window.unregister_stealth_hotkey()

        # 停止帧动画
        if self._frame_controller is not None:
            self._frame_controller.stop()

        # 保存配置
        if self._config is not None:
            self._persist_config("退出前保存", notify=False)

        # 关闭数据库
        if self._db is not None:
            self._db.close()

        # 退出应用
        app = QApplication.instance()
        if app is not None:
            app.quit()

        logger.info("萌卫已退出")


def run(argv: list[str]) -> int:
    """创建 QApplication 并运行萌卫，返回退出码。"""
    setup_logging()
    app = QApplication(argv)
    app.setApplicationName("MoeGuard")
    app.setApplicationDisplayName("萌卫")
    # 桌宠/托盘应用：关闭窗口不应退出进程
    app.setQuitOnLastWindowClosed(False)

    moeguard = MoeGuardApp()
    moeguard.start()

    return app.exec()
