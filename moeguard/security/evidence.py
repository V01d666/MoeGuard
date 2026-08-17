"""陌生人证据录制（触发式短视频 + 截图）。

T14: 编码器用 MJPG + AVI（零专利风险、零额外依赖、Windows 原生）。
D25: MVP 不做音频录制。
D20: 证据存 ~/.moeguard/evidence/（用户可见路径）。
30s 冷却去重（stranger_cooldown_sec），避免同一事件重复录制。
blur_stranger_faces 选项：隐私模式下模糊陌生人脸。
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QObject, Signal

from moeguard.config import EvidenceConfig, SecurityConfig
from moeguard.security.camera import CameraCapture
from moeguard.security.face import FaceRecognizer

logger = logging.getLogger(__name__)

# MJPG fourcc 编码器（T14: 零专利风险）
_FOURCC = cv2.VideoWriter_fourcc(*"MJPG")
_VIDEO_EXT = ".avi"
_SNAPSHOT_EXT = ".jpg"


@dataclass(frozen=True)
class EvidenceResult:
    """后台录制的结果；成功时 pending_dir 尚未对用户可见。"""

    ts: float
    event_uuid: str
    pending_dir: Path | None
    snapshot_path: Path | None
    video_path: Path | None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        if (
            self.error is not None
            or self.pending_dir is None
            or self.snapshot_path is None
            or self.video_path is None
        ):
            return False
        try:
            return (
                self.pending_dir.is_dir()
                and self.snapshot_path.is_file()
                and self.snapshot_path.stat().st_size > 0
                and self.video_path.is_file()
                and self.video_path.stat().st_size > 0
            )
        except OSError:
            return False


class EvidenceRecorder(QObject):
    """陌生人触发式证据录制。

    - start_recording(): 后台录制短视频（MJPG/AVI，无音频）
    - 先保存截图，再开始视频写入
    - 30s 冷却去重，避免重复录制
    - blur_stranger_faces: 隐私模式下模糊陌生人脸
    """

    def __init__(
        self,
        evidence_config: EvidenceConfig,
        security_config: SecurityConfig,
        recognizer: FaceRecognizer | None = None,
    ) -> None:
        super().__init__()
        self._config = evidence_config
        self._security_config = security_config
        self._recognizer = recognizer
        self._last_record_time: float = 0.0
        self._lock = threading.Lock()
        self._recording = False
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # 冷却判定
    # ------------------------------------------------------------------ #

    def is_in_cooldown(self, now: float | None = None) -> bool:
        """是否在冷却期内（避免重复录制）。"""
        now = now if now is not None else time.time()
        elapsed = now - self._last_record_time
        return elapsed < self._security_config.stranger_cooldown_sec

    def update_config(
        self,
        evidence_config: EvidenceConfig,
        security_config: SecurityConfig,
    ) -> None:
        """更新后续事件使用的设置；正在录制的事件保持原参数。"""
        with self._lock:
            self._config = evidence_config
            self._security_config = security_config

    def start_recording(
        self,
        camera: CameraCapture,
        evidence_root: Path,
        duration_sec: int | None = None,
    ) -> bool:
        """排队一次后台录制。

        冷却只在调用方把证据、数据库和计数全部提交后才生效。这样
        首帧、编码器、磁盘或取消失败不会消耗冷却，也不会产生半条事件。
        """
        with self._lock:
            if self._recording or self.is_in_cooldown():
                return False
            self._recording = True
            self._cancel_event.clear()
            recording_config = self._config
            event_uuid = uuid.uuid4().hex
        if duration_sec is None:
            duration_sec = recording_config.video_clip_sec

        with self._lock:
            thread = threading.Thread(
                target=self._record_async,
                args=(camera, evidence_root, duration_sec, recording_config, event_uuid),
                name="MoeGuardEvidence",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._recording = False
                raise
        return True

    def cancel(self) -> None:
        """请求后台录制尽快停止；不阻塞 GUI 线程。"""
        self._cancel_event.set()

    def cancel_and_wait(self, timeout: float = 5.0) -> bool:
        """请求取消并等待录制线程退出；返回是否已安全停止。"""
        self.cancel()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(max(0.0, timeout))
        return not thread.is_alive()

    def commit_event(self, now: float | None = None) -> None:
        """调用方完成 DB/目录提交后，原子地开始冷却。"""
        with self._lock:
            self._last_record_time = now if now is not None else time.time()
            self._recording = False

    def discard_event(self) -> None:
        """调用方未能提交事件时释放排队状态，不进入冷却。"""
        with self._lock:
            self._recording = False

    # ------------------------------------------------------------------ #
    # 视频录制
    # ------------------------------------------------------------------ #

    def _record_async(
        self,
        camera: CameraCapture,
        evidence_root: Path,
        duration_sec: int,
        evidence_config: EvidenceConfig,
        event_uuid: str,
    ) -> None:
        """后台线程主体：临时目录录制，结果交回 GUI 线程提交。"""
        ts = time.time()
        pending_dir: Path | None = None
        snapshot_path: Path | None = None
        video_path: Path | None = None
        writer: cv2.VideoWriter | None = None
        error: Exception | None = None
        try:
            pending_dir = evidence_root / ".pending" / event_uuid
            pending_dir.mkdir(parents=True, exist_ok=False)
            ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))

            # 截图优先：即使后续 60 秒视频仍在写入，首张证据已落盘。
            first_frame = camera.grab()
            if first_frame is None:
                raise RuntimeError("无法获取摄像头首帧")
            snapshot_path = pending_dir / f"snapshot_{ts_str}{_SNAPSHOT_EXT}"
            processed_first_frame = self._process_frame(first_frame, evidence_config)
            if not cv2.imwrite(str(snapshot_path), processed_first_frame):
                raise RuntimeError("截图写入失败")

            video_path = pending_dir / f"clip_{ts_str}{_VIDEO_EXT}"
            h, w = first_frame.shape[:2]
            writer = cv2.VideoWriter(str(video_path), _FOURCC, 10.0, (w, h))
            if not writer.isOpened():
                raise RuntimeError("无法创建 MJPG/AVI 视频")
            writer.write(self._process_frame(first_frame, evidence_config))

            deadline = time.monotonic() + max(duration_sec, 0)
            while time.monotonic() < deadline:
                if self._cancel_event.wait(0.1):
                    raise RuntimeError("录制已取消")
                frame = camera.grab()
                if frame is not None:
                    writer.write(self._process_frame(frame, evidence_config))

        except Exception as exc:
            error = exc
        finally:
            if writer is not None:
                try:
                    writer.release()
                except Exception as exc:
                    if error is None:
                        error = exc

        if error is None:
            try:
                if snapshot_path is None or video_path is None:
                    raise RuntimeError("证据输出路径未创建")
                self._validate_outputs(snapshot_path, video_path)
            except Exception as exc:
                error = exc

        try:
            if error is None:
                logger.info("Evidence video recorded to pending dir: %s", pending_dir)
                self.recording_finished.emit(
                    EvidenceResult(ts, event_uuid, pending_dir, snapshot_path, video_path)
                )
                return

            logger.warning("Evidence recording failed: %s", error)
            cleanup_error: Exception | None = None
            if pending_dir is not None and pending_dir.exists():
                try:
                    shutil.rmtree(pending_dir)
                except Exception as exc:
                    cleanup_error = exc
                    logger.exception("Failed to remove pending evidence %s", pending_dir)
            message = str(error)
            if cleanup_error is not None:
                message = f"{message}; 临时证据清理失败: {cleanup_error}"
            remaining_pending = (
                pending_dir if pending_dir is not None and pending_dir.exists() else None
            )
            self.recording_finished.emit(
                EvidenceResult(
                    ts,
                    event_uuid,
                    remaining_pending,
                    snapshot_path,
                    video_path,
                    message,
                )
            )
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    @staticmethod
    def _validate_outputs(snapshot_path: Path, video_path: Path) -> None:
        """在编码器释放后确认截图和视频均非空且视频首帧可读。"""
        if not snapshot_path.is_file() or snapshot_path.stat().st_size <= 0:
            raise RuntimeError("截图文件为空或不存在")
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise RuntimeError("视频文件为空或不存在")

        capture = cv2.VideoCapture(str(video_path))
        try:
            if not capture.isOpened():
                raise RuntimeError("视频文件无法重新打开")
            ok, frame = capture.read()
            if not ok or frame is None or frame.size == 0:
                raise RuntimeError("视频文件不包含可读取帧")
        finally:
            capture.release()

    # ------------------------------------------------------------------ #
    # 隐私处理
    # ------------------------------------------------------------------ #

    def _process_frame(
        self, frame: np.ndarray, evidence_config: EvidenceConfig
    ) -> np.ndarray:
        """处理帧：隐私模糊开启时，无法检测就拒绝留存。"""
        if not evidence_config.blur_stranger_faces:
            return frame
        if self._recognizer is None:
            raise RuntimeError("隐私模糊已启用，但人脸检测器不可用")
        return self._blur_faces(frame)

    def _blur_faces(self, frame: np.ndarray) -> np.ndarray:
        """检测帧中所有人脸并模糊处理（隐私模式）。"""
        try:
            faces = list(self._recognizer.detect(frame))
        except Exception as exc:
            raise RuntimeError("隐私模糊检测失败，拒绝保存原始画面") from exc

        if not faces:
            return self._pixelate_full_frame(frame)

        result = frame.copy()
        blurred_any = False
        for f in faces:
            x1, y1, x2, y2 = f["bbox"]
            x1_i, y1_i = int(max(0, x1)), int(max(0, y1))
            x2_i, y2_i = int(min(frame.shape[1], x2)), int(min(frame.shape[0], y2))
            if x2_i <= x1_i or y2_i <= y1_i:
                continue
            # 高斯模糊人脸区域
            face_region = result[y1_i:y2_i, x1_i:x2_i]
            if face_region.size > 0:
                blurred = cv2.GaussianBlur(face_region, (51, 51), 30)
                result[y1_i:y2_i, x1_i:x2_i] = blurred
                blurred_any = True
        return result if blurred_any else self._pixelate_full_frame(frame)

    @staticmethod
    def _pixelate_full_frame(frame: np.ndarray) -> np.ndarray:
        """检测不到有效人脸区域时整帧降采样，避免静默保存原始画面。"""
        height, width = frame.shape[:2]
        if height <= 0 or width <= 0:
            raise RuntimeError("隐私模糊收到空画面，拒绝保存")
        small = cv2.resize(
            frame,
            (max(1, width // 32), max(1, height // 32)),
            interpolation=cv2.INTER_AREA,
        )
        return cv2.resize(small, (width, height), interpolation=cv2.INTER_NEAREST)

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    def reset_cooldown(self) -> None:
        """重置冷却计时器（手动触发时使用）。"""
        self._last_record_time = 0.0

    recording_finished = Signal(object)  # EvidenceResult
