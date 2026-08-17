"""主人人脸特征的加密本地存储。

主人特征向量经 CryptoBox 加密后落盘，启动时解密载入 OwnerProfile。
这是隐私红线的一部分：主人人脸特征不出设备、落盘加密。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

import numpy as np

from moeguard.utils.crypto import CryptoBox


class OwnerProfileStore:
    """加密保存/加载主人人脸特征向量。"""

    def __init__(self, store_path: Path, crypto: CryptoBox) -> None:
        self._path = store_path
        self._crypto = crypto
        self._last_load_error: str | None = None

    @property
    def last_load_error(self) -> str | None:
        """最近一次加载失败的原因，供应用层向用户解释并降级。"""
        return self._last_load_error

    def save(self, embeddings: list[np.ndarray]) -> None:
        """加密原子保存主人特征列表，写入失败不破坏已注册档案。"""
        payload = json.dumps([e.tolist() for e in embeddings]).encode("utf-8")
        encrypted = self._crypto.encrypt(payload)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise

    def load(self) -> list[np.ndarray]:
        """解密加载主人特征；坏档案会隔离并退回未注册状态。"""
        self._last_load_error = None
        if not self._path.exists():
            return []
        try:
            payload = self._crypto.decrypt(self._path.read_bytes())
            raw = json.loads(payload)
            if not isinstance(raw, list):
                raise ValueError("主人特征格式不是列表")
            embeddings = [np.asarray(value, dtype=np.float32) for value in raw]
            if any(
                embedding.ndim != 1
                or embedding.size < 16
                or not np.isfinite(embedding).all()
                for embedding in embeddings
            ):
                raise ValueError("主人特征维度或数值无效")
            return embeddings
        except Exception as exc:
            self._last_load_error = str(exc)
            quarantine = self._path.with_name(
                f"{self._path.name}.corrupt-{uuid.uuid4().hex[:8]}"
            )
            try:
                os.replace(self._path, quarantine)
            except OSError:
                # 无法隔离时仍 fail-closed；不得让损坏档案进入识别路径。
                pass
            return []

    def delete(self) -> int:
        """永久删除主档、隔离档和中断写入残留，全部成功后返回数量。"""
        candidates = {self._path}
        candidates.update(self._path.parent.glob(f"{self._path.name}.corrupt-*"))
        candidates.update(self._path.parent.glob(f".{self._path.name}.*"))

        deleted = 0
        for path in sorted(candidates, key=lambda item: item.name):
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_dir() and not path.is_symlink():
                raise IsADirectoryError(path)
            path.unlink()
            if path.exists() or path.is_symlink():
                raise OSError(f"owner profile still exists after deletion: {path}")
            deleted += 1
        return deleted
