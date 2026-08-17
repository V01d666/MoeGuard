"""摄像头采集（线程安全常驻架构）。

PRD §7.2: 摄像头常驻模式，严禁每帧 open/close（M0-2 根因，导致 0.5fps）。
start 时 open，stop 时 release，期间 VideoCapture 常驻。
grab() 用于证据截图，与 QTimer 采集共享同一 VideoCapture，通过锁互斥。

摄像头只由后台采集线程读取；GUI 线程只接收 ``frame_ready`` 信号。
"""

from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class CameraCapture(QObject):
    """周期性采集摄像头帧，支持线程安全单帧抓取。

    摄像头在 start() 时打开、stop() 时释放，期间常驻。只有内部采集
    线程会调用 ``VideoCapture.read()``；证据录制通过 ``grab()`` 取得
    最新帧副本，因此不会和周期检测并发读同一个设备。
    """

    frame_ready = Signal(object)  # np.ndarray (BGR)
    camera_ready = Signal()  # 已收到首帧，摄像头实际可用
    camera_failed = Signal(str)  # 摄像头中断或连续读取失败
    capture_gap_detected = Signal(float)  # 恢复后检测到的采集时间线空洞（秒）

    def __init__(self) -> None:
        super().__init__()
        self._cap: cv2.VideoCapture | None = None
        self._lifecycle_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._capture_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._generation = 0
        self._last_error: str | None = None
        self._interval_sec = 1.0
        self._device_index: int = 0
        self._ready_emitted = False

    @staticmethod
    def available_device_indices(max_devices: int = 5) -> list[int]:
        """主动打开并探测摄像头序号；只能用于用户明确请求的硬件诊断。

        普通窗口初始化不得调用本方法，因为即开即关仍会点亮系统的摄像头
        隐私指示灯。设置页使用静态序号选择，实际设备在注册/值守时验证。
        """
        available: list[int] = []
        for index in range(max_devices):
            cap = cv2.VideoCapture(index)
            try:
                if cap.isOpened():
                    available.append(index)
            finally:
                cap.release()
        return available

    def start(self, device_index: int = 0, interval_ms: int = 1000) -> bool:
        """打开摄像头并开始周期采集，返回是否成功。

        Args:
            device_index: 摄像头设备序号（默认 0）。
            interval_ms: 采集间隔毫秒（默认 1000ms = 1fps）。
        """
        with self._lifecycle_lock:
            previous_thread = self._capture_thread
            if previous_thread is not None and previous_thread.is_alive():
                self._last_error = "摄像头驱动停止超时，请稍后重试"
                logger.error("Refusing camera restart while previous reader is alive")
                return False
            if previous_thread is not None:
                self._capture_thread = None
            if self._cap is not None:
                self._last_error = "摄像头已在使用中"
                logger.warning("Refusing duplicate camera start")
                return False

            cap = cv2.VideoCapture(device_index)
            if not cap.isOpened():
                self._last_error = "摄像头不可用或正被其他程序占用"
                logger.error("Failed to open camera device %d", device_index)
                cap.release()
                return False

            self._device_index = device_index
            self._interval_sec = max(interval_ms / 1000.0, 0.1)
            self._ready_emitted = False
            self._last_error = None
            self._generation += 1
            generation = self._generation
            stop_event = threading.Event()
            self._cap = cap
            self._stop_event = stop_event
            thread = threading.Thread(
                target=self._capture_loop,
                args=(cap, stop_event, generation, self._interval_sec),
                name="MoeGuardCamera",
                daemon=True,
            )
            self._capture_thread = thread
            thread.start()
        logger.info(
            "Camera started: device=%d, interval=%dms", device_index, interval_ms
        )
        return True

    def stop(self) -> None:
        """停止采集并释放摄像头。"""
        with self._lifecycle_lock:
            stop_event = self._stop_event
            thread = self._capture_thread
            cap = self._cap
            self._stop_event = None
            self._cap = None
            if stop_event is not None:
                stop_event.set()
        # 不等待可能卡死在驱动 read() 中的线程持锁；释放设备以唤醒它。
        if cap is not None:
            cap.release()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)
        with self._lifecycle_lock:
            if thread is not None and not thread.is_alive():
                if self._capture_thread is thread:
                    self._capture_thread = None
        with self._frame_lock:
            self._latest_frame = None
        logger.info("Camera stopped")

    def grab(self) -> np.ndarray | None:
        """单帧抓取（用于证据截图），失败返回 None。

        线程安全：返回后台采集线程保存的最新帧副本，不直接读设备。
        """
        with self._frame_lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    def is_opened(self) -> bool:
        """摄像头是否已打开。"""
        cap = self._cap
        return cap is not None and cap.isOpened()

    @property
    def last_error(self) -> str | None:
        """Last start rejection reason, suitable for a user-visible recovery hint."""
        return self._last_error

    @property
    def device_index(self) -> int:
        """当前设备序号。"""
        return self._device_index

    def _capture_loop(
        self,
        cap: cv2.VideoCapture,
        stop_event: threading.Event,
        generation: int,
        interval_sec: float,
    ) -> None:
        """后台唯一读者：持续抓帧，按检测间隔向 Qt 发射帧。"""
        last_emit = 0.0
        last_success = 0.0
        failures = 0
        ready_emitted = False
        # 证据录制需要较新帧；检测仍保持调用方设定的频率。
        read_interval = min(interval_sec, 0.1)

        try:
            while not stop_event.is_set():
                ok, frame = cap.read()
                # ``read()`` can return after stop() or after a blocked driver
                # call.  A retired generation must never publish a frame or
                # state signal into a later camera session.
                if stop_event.is_set() or not self._is_current_generation(
                    generation, cap
                ):
                    break

                now = time.monotonic()
                if ok and frame is not None:
                    failures = 0
                    if not ready_emitted:
                        ready_emitted = True
                        self._ready_emitted = True
                        self.camera_ready.emit()
                    if last_success:
                        gap = now - last_success
                        if gap > self._gap_threshold_sec():
                            logger.warning("Camera capture gap detected: %.1fs", gap)
                            self.capture_gap_detected.emit(gap)
                    last_success = now
                    with self._frame_lock:
                        self._latest_frame = frame
                    if now - last_emit >= interval_sec:
                        self.frame_ready.emit(frame.copy())
                        last_emit = now
                else:
                    failures += 1
                    if failures == 3:
                        # S0/睡眠恢复后有些驱动不会再给出成功帧，而是持续失败。
                        # 这种情况下同样要揭示上次正常采集后的时间线空洞，不能只
                        # 在“成功恢复”分支中提示。
                        if last_success:
                            gap = now - last_success
                            if gap > self._gap_threshold_sec():
                                logger.warning(
                                    "Camera capture gap before failure: %.1fs", gap
                                )
                                self.capture_gap_detected.emit(gap)
                        logger.error("Camera read failed repeatedly")
                        self.camera_failed.emit("摄像头读取失败或已断开")

                stop_event.wait(read_interval)
        finally:
            with self._lifecycle_lock:
                if self._capture_thread is threading.current_thread():
                    self._capture_thread = None

    def _is_current_generation(self, generation: int, cap: cv2.VideoCapture) -> bool:
        """Return whether a worker still owns the current camera session."""
        with self._lifecycle_lock:
            return self._generation == generation and self._cap is cap

    def _gap_threshold_sec(self) -> float:
        """恢复后判定为时间线空洞的最小间隔。"""
        return max(5.0, self._interval_sec * 3)
