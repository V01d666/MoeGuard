"""安防核心层：摄像头采集、人脸识别、主人比对、证据录制。

face.py 和 owner.py 不依赖 PySide6，可在无 GUI 环境导入测试。
camera.py 和 evidence.py 需要 PySide6（QObject/Signal）。

为支持无 GUI 环境测试 face/owner，camera 和 evidence 采用懒导入：
直接 `from moeguard.security.face import FaceRecognizer` 时不会触发
PySide6 依赖，除非显式访问 CameraCapture / EvidenceRecorder。
"""

from moeguard.security.face import FaceRecognizer
from moeguard.security.owner import OwnerProfile

__all__ = ["CameraCapture", "EvidenceRecorder", "FaceRecognizer", "OwnerProfile"]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """懒导入需要 PySide6 的模块。"""
    if name == "CameraCapture":
        from moeguard.security.camera import CameraCapture
        return CameraCapture
    if name == "EvidenceRecorder":
        from moeguard.security.evidence import EvidenceRecorder
        return EvidenceRecorder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
