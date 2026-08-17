"""加密工具：保护主人人脸特征等敏感数据。

使用 Fernet 对称加密（cryptography 库）。密钥生成一次、本地保存、
权限收紧（0600）。注意：密钥本身的安全取决于设备安全，本方案仅保证
「数据落盘加密」，防止误拷贝泄露，不抵御拥有设备访问权限的攻击者。
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet


class CryptoBox:
    """本地敏感数据的对称加密盒。"""

    def __init__(self, key_path: Path) -> None:
        self._key_path = key_path
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        if self._key_path.exists():
            return self._key_path.read_bytes()
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self._key_path.write_bytes(key)
        try:
            os.chmod(self._key_path, 0o600)
        except OSError:
            # Windows 上 chmod 语义不同，忽略即可
            pass
        return key

    def encrypt(self, data: bytes) -> bytes:
        """加密字节串。"""
        return self._fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        """解密字节串。"""
        return self._fernet.decrypt(token)
