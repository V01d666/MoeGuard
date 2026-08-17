"""本地数据存储层：SQLite、主人特征加密存储、证据文件管理。

采用惰性聚合（PEP 562）：各存储类首次访问时才导入，避免轻量模块
（如 evidence_store）被迫拉起 numpy / cryptography 等重依赖。
"""

__all__ = ["Database", "EvidenceStore", "OwnerProfileStore"]


def __getattr__(name: str):
    if name == "Database":
        from moeguard.storage.db import Database

        return Database
    if name == "EvidenceStore":
        from moeguard.storage.evidence_store import EvidenceStore

        return EvidenceStore
    if name == "OwnerProfileStore":
        from moeguard.storage.owner_profile import OwnerProfileStore

        return OwnerProfileStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
