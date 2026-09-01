"""统一数据路径管理。

所有用户数据存 ~/.moeguard/，严禁写程序运行目录（PRD §7.2）。
子目录：owner/ · evidence/ · models/ · logs/ · config.toml · moeguard.db
"""

from __future__ import annotations

import hashlib
import os
import uuid
from functools import cache
from pathlib import Path

# 基础目录
_BASE_DIR = Path.home() / ".moeguard"

# 各子目录/文件路径
CONFIG_PATH = _BASE_DIR / "config.toml"
DB_PATH = _BASE_DIR / "moeguard.db"
LOG_DIR = _BASE_DIR / "logs"

OWNER_DIR = _BASE_DIR / "owner"          # 主人特征加密存储
OWNER_EMBEDDINGS_PATH = OWNER_DIR / "owner_embeddings.enc"
CRYPTO_KEY_PATH = OWNER_DIR / ".crypto_key"

EVIDENCE_DIR = _BASE_DIR / "evidence"    # 用户可见路径（D20）
MODELS_DIR = _BASE_DIR / "models"        # ONNX 模型缓存
ROLES_DIR = _BASE_DIR / "roles"          # 受管自定义角色库
ROLE_WORKBENCH_DIR = _BASE_DIR / "role-workbench"  # 可恢复形象档案与草稿

# 随包模型的 SHA-256。内部 RC 与源码运行均通过同一份资源校验，防止
# 损坏或被替换的模型在无感知情况下参与人脸判定。
_BUNDLED_MODEL_HASHES: dict[str, str] = {
    "face_detection_yunet_2023mar.onnx": (
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
    ),
    "face_recognition_sface_2021dec.onnx": (
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
    ),
}


def ensure_dirs() -> None:
    """创建所有需要的目录（幂等）。"""
    for d in (
        _BASE_DIR,
        OWNER_DIR,
        EVIDENCE_DIR,
        MODELS_DIR,
        LOG_DIR,
        ROLES_DIR,
        ROLE_WORKBENCH_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def model_path(name: str) -> Path:
    """获取模型缓存目录下的完整路径。"""
    return MODELS_DIR / name


def resource_path(*parts: str) -> Path:
    """返回源码或 PyInstaller ``--onedir`` 包中的只读资源路径。

    PyInstaller 将 ``resources`` 放在 ``_internal/resources``；源码和 wheel
    安装都把它放在 ``moeguard`` 包的同级目录。用户数据仍一律保存在
    ``~/.moeguard``，不写入包目录。
    """
    return Path(__file__).resolve().parents[2] / "resources" / Path(*parts)


@cache
def bundled_model_path(name: str) -> Path | None:
    """返回校验过 hash 的随包模型；资源不存在时返回 ``None``。"""
    expected = _BUNDLED_MODEL_HASHES.get(name)
    if expected is None:
        return None

    path = resource_path("models", name)
    if not path.is_file():
        return None

    verify_model_file(name, path)
    return path


def verify_model_file(name: str, path: Path) -> None:
    """按内置 allowlist 校验任意来源的模型文件。"""
    expected = _BUNDLED_MODEL_HASHES.get(name)
    if expected is None:
        raise ValueError(f"Unknown face model: {name}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError(f"Model integrity check failed: {name}")


def quarantine_model_file(path: Path) -> Path | None:
    """把篡改或截断的缓存隔离，避免下次启动再次误用。"""
    if not path.exists():
        return None
    quarantined = path.with_name(f"{path.name}.invalid-{uuid.uuid4().hex[:8]}")
    try:
        os.replace(path, quarantined)
    except OSError:
        return None
    return quarantined


def evidence_dir() -> Path:
    """证据目录（用户可见，D20）。"""
    return EVIDENCE_DIR


def owner_dir() -> Path:
    """主人特征存储目录。"""
    return OWNER_DIR


def base_dir() -> Path:
    """基础数据目录 ~/.moeguard/。"""
    return _BASE_DIR
