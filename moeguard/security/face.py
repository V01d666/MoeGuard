"""人脸检测与特征提取（YuNet + SFace，纯本地 ONNX 推理）。

D16: 采用 OpenCV Zoo 的 YuNet（检测，MIT）+ SFace（识别，Apache-2.0）。
模型文件按需从 OpenCV Zoo GitHub 下载到 ~/.moeguard/models/，推理全程不出设备。

懒加载：首次调用 detect() 时才加载 ONNX 模型，避免启动卡顿。
det_size 默认 (320,320)，常驻值守模式更快；M0-2 测试用 (640,640) 精度更高。
"""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from moeguard.config import SecurityConfig
from moeguard.utils.paths import (
    bundled_model_path,
    model_path,
    quarantine_model_file,
    verify_model_file,
)

logger = logging.getLogger(__name__)

# OpenCV Zoo GitHub raw URLs（模型自动下载）
_MODEL_URLS: dict[str, str] = {
    "face_detection_yunet_2023mar.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
    "face_recognition_sface_2021dec.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_recognition_sface/face_recognition_sface_2021dec.onnx"
    ),
}

# 下载超时（秒）
_DOWNLOAD_TIMEOUT = 60


class FaceRecognizer:
    """基于 YuNet + SFace 的离线人脸检测与特征提取。

    YuNet 负责人脸检测（bbox + landmarks），SFace 负责特征提取与比对。
    两者均为 ONNX 模型，通过 cv2.FaceDetectorYN / cv2.FaceRecognizerSF 调用。
    """

    def __init__(
        self,
        config: SecurityConfig | None = None,
        det_size: tuple[int, int] = (320, 320),
        detector_score_threshold: float = 0.6,
        input_strategy: str = "native",
    ) -> None:
        if not 0.0 < detector_score_threshold <= 1.0:
            raise ValueError("detector_score_threshold must be in (0, 1]")
        if input_strategy not in {"native", "fixed_320"}:
            raise ValueError("input_strategy must be 'native' or 'fixed_320'")
        self._config = config or SecurityConfig()
        self._det_size = det_size
        self._detector_score_threshold = detector_score_threshold
        self._input_strategy = input_strategy
        self._detector: cv2.FaceDetectorYN | None = None
        self._recognizer: cv2.FaceRecognizerSF | None = None

    # ------------------------------------------------------------------ #
    # 模型加载（懒加载 + 自动下载）
    # ------------------------------------------------------------------ #

    def _ensure_loaded(self) -> None:
        """懒加载模型：首次调用时下载并初始化 YuNet + SFace。"""
        if self._detector is not None and self._recognizer is not None:
            return

        det_path = self._resolve_model(self._config.detector_model)
        rec_path = self._resolve_model(self._config.recognizer_model)

        w, h = self._det_size
        self._detector = cv2.FaceDetectorYN.create(
            str(det_path),
            "",
            (w, h),
            score_threshold=self._detector_score_threshold,
            nms_threshold=0.3,
            top_k=5000,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(str(rec_path), "")
        logger.info(
            "FaceRecognizer loaded: YuNet(%s) + SFace(%s), det_size=%s",
            det_path.name,
            rec_path.name,
            self._det_size,
        )

    def _resolve_model(self, filename: str) -> Path:
        """获取模型路径：随包模型优先，缓存与下载仅作后备。"""
        bundled = bundled_model_path(filename)
        if bundled is not None:
            return bundled

        path = model_path(filename)
        if path.exists():
            try:
                verify_model_file(filename, path)
                return path
            except Exception as exc:
                quarantined = quarantine_model_file(path)
                logger.warning(
                    "Cached model failed integrity check (%s); quarantined=%s",
                    exc,
                    quarantined,
                )
        self._download_model(filename, path)
        return path

    @staticmethod
    def _download_model(filename: str, dest: Path) -> None:
        """从 OpenCV Zoo GitHub 下载模型文件。"""
        url = _MODEL_URLS.get(filename)
        if url is None:
            raise FileNotFoundError(
                f"Unknown model '{filename}', no download URL available"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading model: %s -> %s", url, dest)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MoeGuard/1.0"})
            with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            verify_model_file(filename, tmp)
            os.replace(tmp, dest)
            logger.info("Model downloaded: %s (%d bytes)", dest.name, dest.stat().st_size)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------ #
    # 检测接口
    # ------------------------------------------------------------------ #

    def detect(self, frame: np.ndarray) -> list[dict]:
        """检测帧中所有人脸。

        Args:
            frame: BGR 图像 (H, W, 3)。

        Returns:
            每个人脸一个 dict，含：
              - "bbox": np.ndarray shape (4,) [x1, y1, x2, y2]（像素坐标）
              - "embedding": np.ndarray shape (128,) SFace 特征向量
              - "confidence": float 检测置信度
        """
        self._ensure_loaded()
        assert self._detector is not None
        assert self._recognizer is not None

        h, w = frame.shape[:2]
        detector_frame = frame
        if self._input_strategy == "native":
            # Production behavior: let YuNet see the original camera frame.
            self._detector.setInputSize((w, h))
        else:
            # T5-2 comparator: resize to a stable 320×320 input and map
            # YuNet landmarks back before using SFace on the original frame.
            detector_frame = cv2.resize(frame, self._det_size)
            self._detector.setInputSize(self._det_size)

        # YuNet 返回 (N, 15)：[x, y, w, h, lm_re_x, lm_re_y, ..., score]
        retval, faces = self._detector.detect(detector_frame)
        if faces is None or len(faces) == 0:
            return []

        results: list[dict] = []
        faces_arr = np.asarray(faces)
        for face_row in faces_arr:
            if self._input_strategy == "fixed_320":
                face_row = self._scale_face_row(face_row, w, h)
            x, y, fw, fh = face_row[:4]
            score = float(face_row[-1])
            bbox = np.array([x, y, x + fw, y + fh], dtype=np.float32)

            # SFace 需要先对齐裁剪再提取特征
            aligned = self._recognizer.alignCrop(frame, face_row.reshape(1, -1))
            embedding = self._recognizer.feature(aligned).flatten()

            results.append(
                {
                    "bbox": bbox,
                    "embedding": embedding,
                    "confidence": score,
                }
            )
        return results

    def _scale_face_row(
        self, face_row: np.ndarray, original_width: int, original_height: int
    ) -> np.ndarray:
        """Map a fixed-size YuNet row back to the original camera frame."""
        scaled = np.asarray(face_row, dtype=np.float32).copy()
        width, height = self._det_size
        scaled[[0, 2, 4, 6, 8, 10, 12]] *= original_width / width
        scaled[[1, 3, 5, 7, 9, 11, 13]] *= original_height / height
        return scaled

    # ------------------------------------------------------------------ #
    # 特征比对
    # ------------------------------------------------------------------ #

    @staticmethod
    def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """两个特征向量的余弦相似度。

        与 SFace match(FR_COSINE) 语义一致（OpenCV 5.0 直接返回余弦相似度）。
        """
        a_flat = a.flatten().astype(np.float32)
        b_flat = b.flatten().astype(np.float32)
        # 手动余弦相似度（与 SFace FR_COSINE 语义一致）
        dot = float(np.dot(a_flat, b_flat))
        norm = float(np.linalg.norm(a_flat) * np.linalg.norm(b_flat))
        if norm < 1e-12:
            return 0.0
        return dot / norm

    def feature_match(self, crop_feature: np.ndarray, registered_feature: np.ndarray) -> float:
        """使用 SFace match(FR_COSINE) 比对两个特征，返回余弦相似度。

        cv2.FaceRecognizerSF.match(FR_COSINE) 在 OpenCV 5.0 中直接返回余弦相似度
        （范围 [-1, 1]，越大越相似）。

        Args:
            crop_feature: 待比对特征 (128,)
            registered_feature: 已注册特征 (128,)

        Returns:
            余弦相似度 [-1, 1]，越大越相似
        """
        self._ensure_loaded()
        assert self._recognizer is not None

        f1 = crop_feature.reshape(1, -1).astype(np.float32)
        f2 = registered_feature.reshape(1, -1).astype(np.float32)
        # OpenCV 5.0: FR_COSINE 直接返回余弦相似度（非距离）
        return float(self._recognizer.match(f1, f2, cv2.FaceRecognizerSF_FR_COSINE))

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #

    def set_det_size(self, size: tuple[int, int]) -> None:
        """更新检测输入尺寸（下次 detect 生效）。"""
        self._det_size = size
        if self._detector is not None:
            self._detector.setInputSize(size)

    @property
    def det_size(self) -> tuple[int, int]:
        return self._det_size

    @property
    def is_loaded(self) -> bool:
        """模型是否已加载。"""
        return self._detector is not None and self._recognizer is not None
