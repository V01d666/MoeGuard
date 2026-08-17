"""主人注册与人脸比对。

管理主人多角度人脸特征（1 张起步 + 日常渐进补充），并据此判定
画面中出现的是主人还是陌生人。主人特征经加密后本地存储（见 storage）。

classify 逻辑：画面中有人脸但无人匹配主人 → 陌生人检测。
D15: 人脸识别结果不驱动状态转换，解锁事件才是权威退出信号。
"""

from __future__ import annotations

import logging

import numpy as np

from moeguard.config import SecurityConfig
from moeguard.security.face import FaceRecognizer

logger = logging.getLogger(__name__)


class OwnerProfile:
    """主人特征管理与主人/陌生人判定。"""

    def __init__(self, config: SecurityConfig, recognizer: FaceRecognizer) -> None:
        self._config = config
        self._recognizer = recognizer
        self._owner_embeddings: list[np.ndarray] = []  # 渐进补充的多角度样本

    def is_registered(self) -> bool:
        """主人是否已注册。"""
        return len(self._owner_embeddings) > 0

    def load(self, embeddings: list[np.ndarray]) -> None:
        """从加密存储恢复主人特征。"""
        self._owner_embeddings = [np.asarray(e, dtype=np.float32) for e in embeddings]

    def embeddings(self) -> list[np.ndarray]:
        """导出主人特征供加密存储。"""
        return list(self._owner_embeddings)

    def clear(self) -> None:
        """清除已注册的主人特征。"""
        self._owner_embeddings = []

    def register(self, frame: np.ndarray) -> bool:
        """从一帧注册主人特征（取画面中最大人脸）。成功返回 True。

        取 bbox 面积最大的人脸，提取 SFace 特征存入注册列表。
        """
        faces = self._recognizer.detect(frame)
        if not faces:
            logger.debug("register: no face detected")
            return False

        best = max(
            faces,
            key=lambda f: float(
                (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1])
            ),
        )
        self._owner_embeddings.append(best["embedding"])
        logger.info(
            "Owner registered: embedding #%d (confidence=%.3f)",
            len(self._owner_embeddings),
            best["confidence"],
        )
        return True

    def classify(self, frame: np.ndarray) -> tuple[bool, bool]:
        """判定画面中是否出现主人 / 陌生人。

        Returns:
            (owner_detected, stranger_detected)

            - owner_detected: 画面中至少一张人脸匹配主人
            - stranger_detected: 画面中至少一张人脸不匹配任何主人特征
              （即有人脸但无人匹配主人）
        """
        if not self.is_registered():
            return False, False

        faces = self._recognizer.detect(frame)
        if not faces:
            return False, False

        threshold = self._config.face_match_threshold
        owner = False
        stranger = False

        for f in faces:
            best_sim = max(
                self._recognizer.feature_match(f["embedding"], o)
                for o in self._owner_embeddings
            )
            if best_sim >= threshold:
                owner = True
            else:
                stranger = True

        return owner, stranger
