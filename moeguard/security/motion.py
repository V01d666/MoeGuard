"""低成本画面运动检测，用于可选的补充取证。

这不是活体检测：它只能发现画面发生了足够明显的变化，不能判断画面中的
人是否真实，也不能防止静止照片冒充主人。默认关闭，由上层在已完成值守
同意时显式启用。
"""

from __future__ import annotations

import threading

import cv2
import numpy as np


class MotionDetector:
    """以相邻采样帧的像素变化比例识别明显画面运动。"""

    def __init__(
        self,
        *,
        pixel_delta: int = 25,
        changed_ratio_threshold: float = 0.015,
    ) -> None:
        self._pixel_delta = pixel_delta
        self._changed_ratio_threshold = changed_ratio_threshold
        self._previous: np.ndarray | None = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        """丢弃基线；下一帧只建立基线而不触发事件。"""
        with self._lock:
            self._previous = None

    def observe(self, frame: np.ndarray) -> float:
        """记录一帧并返回变化比例；首帧返回 ``0.0``。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # 缩小并轻度模糊，降低摄像头压缩噪点、自动曝光微抖的影响。
        gray = cv2.resize(gray, (160, 120), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        with self._lock:
            previous = self._previous
            self._previous = gray

        if previous is None or previous.shape != gray.shape:
            return 0.0
        delta = cv2.absdiff(previous, gray)
        changed = np.count_nonzero(delta >= self._pixel_delta)
        return float(changed) / float(delta.size)

    def is_motion(self, frame: np.ndarray) -> tuple[bool, float]:
        """返回 ``(是否明显运动, 变化比例)``。"""
        ratio = self.observe(frame)
        return ratio >= self._changed_ratio_threshold, ratio
