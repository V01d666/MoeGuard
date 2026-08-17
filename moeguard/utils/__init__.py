"""通用工具：加密、日志、数据路径。

采用惰性聚合（PEP 562）：``from moeguard.utils import paths`` 仅加载
轻量的 paths 子模块；CryptoBox / setup_logging 首次访问时才导入，
避免基础模块被迫拉起 cryptography 等重依赖。
"""

from moeguard.utils import paths

__all__ = ["CryptoBox", "setup_logging", "paths"]


def __getattr__(name: str):
    if name == "CryptoBox":
        from moeguard.utils.crypto import CryptoBox

        return CryptoBox
    if name == "setup_logging":
        from moeguard.utils.logging import setup_logging

        return setup_logging
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
