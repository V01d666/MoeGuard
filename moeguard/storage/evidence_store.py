"""证据文件管理与过期清理。

每个陌生人事件一个目录 evidence/<时间戳>/，含截图与短视频。
按 retention_days 自动清理超期目录，控制磁盘占用。

目录命名：evidence/<时间戳>_<事件id>/（event_id 关联 db.incident_id，
便于事件回溯）。无 event_id 时退化为 evidence/<时间戳>/。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

DEFAULT_STALE_PENDING_SECONDS = 60 * 60


class EvidenceStore:
    """管理 evidence/ 下的证据目录与过期清理。"""

    def __init__(self, base_dir: Path, retention_days: int) -> None:
        self._base = base_dir
        self._retention_days = retention_days
        self._base.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        """证据根目录，供 UI 始终提供可见的本地入口。"""
        return self._base

    def update_retention_days(self, retention_days: int) -> None:
        """更新后续清理操作采用的保留期。"""
        self._retention_days = retention_days

    def create_event_dir(
        self,
        ts: float | None = None,
        event_id: int | None = None,
    ) -> Path:
        """为一次事件创建并返回证据目录。

        Args:
            ts:       事件时间戳（Unix 秒），默认当前时间。
            event_id: 事件 id（关联 db.incident_id），用于目录命名回溯。

        Returns:
            创建的证据目录 Path。
        """
        ts = ts if ts is not None else time.time()
        dir_name = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
        if event_id is not None:
            dir_name = f"{dir_name}_{event_id}"
        event_dir = self._base / dir_name
        event_dir.mkdir(parents=True, exist_ok=True)
        return event_dir

    def commit_pending(self, pending_dir: Path, ts: float, event_uuid: str) -> Path:
        """把后台已完整写入的临时目录原子提交为可见证据目录。

        ``pending_dir`` 必须位于 ``.pending`` 下。重命名在同一文件系统
        内是原子操作，失败时调用方不应写入数据库或计数。
        """
        pending_root = self._base / ".pending"
        if pending_dir.parent != pending_root or pending_dir.name != event_uuid:
            raise ValueError("invalid pending evidence directory")
        ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
        final_dir = self._base / f"{ts_str}_{event_uuid}"
        # A duplicate GUI delivery may arrive after the original callback has
        # moved the directory.  The stable UUID makes that retry idempotent.
        if final_dir.is_dir() and not pending_dir.exists():
            return final_dir
        if not pending_dir.is_dir():
            raise FileNotFoundError(pending_dir)
        if final_dir.exists():
            raise FileExistsError(final_dir)
        pending_dir.replace(final_dir)
        return final_dir

    @staticmethod
    def _remove_path(path: Path) -> bool:
        """严格删除路径；不存在返回 ``False``，失败则向调用方抛错。"""
        if not path.exists() and not path.is_symlink():
            return False
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        if path.exists() or path.is_symlink():
            raise OSError(f"path still exists after deletion: {path}")
        return True

    def discard_pending(self, pending_dir: Path | None) -> bool:
        """严格删除单个未提交录制目录，失败时由调用方显示并安排重试。"""
        if pending_dir is None:
            return False
        pending_root = self._base / ".pending"
        if pending_dir.parent != pending_root:
            raise ValueError("invalid pending evidence directory")
        return self._remove_path(pending_dir)

    def delete_event(self, event_dir: Path) -> bool:
        """严格删除单个已提交证据目录，返回是否实际删除。"""
        if event_dir.parent != self._base or event_dir.name == ".pending":
            raise ValueError("invalid evidence directory")
        if event_dir.exists() and not event_dir.is_dir():
            raise NotADirectoryError(event_dir)
        return self._remove_path(event_dir)

    def delete_all(self) -> int:
        """严格删除全部已提交和未提交证据，全部成功后返回事件数。"""
        events = self.list_events()
        for event_dir in events:
            self.delete_event(event_dir)
        self.delete_all_pending()
        return len(events)

    def list_pending(self) -> list[Path]:
        """列出全部未提交条目；它们不作为可见事件展示。"""
        pending_root = self._base / ".pending"
        if not pending_root.exists():
            return []
        return sorted(pending_root.iterdir(), key=lambda path: path.name)

    def delete_all_pending(self) -> int:
        """严格删除整个未提交目录，返回其中原有条目数。"""
        pending_root = self._base / ".pending"
        pending_count = len(self.list_pending())
        self._remove_path(pending_root)
        return pending_count

    def cleanup_stale_pending(
        self,
        now: float | None = None,
        max_age_seconds: float = DEFAULT_STALE_PENDING_SECONDS,
    ) -> list[Path]:
        """清理超过安全录制窗口的未提交条目并返回已删除路径。"""
        now = now if now is not None else time.time()
        cutoff = now - max(0.0, max_age_seconds)
        deleted: list[Path] = []
        for pending_path in self.list_pending():
            if pending_path.stat().st_mtime > cutoff:
                continue
            self._remove_path(pending_path)
            deleted.append(pending_path)
        return deleted

    def list_events(self) -> list[Path]:
        """列出所有事件目录（按名称排序，即时间顺序）。

        Returns:
            事件目录 Path 列表，不存在的返回空列表。
        """
        if not self._base.exists():
            return []
        return sorted(
            [p for p in self._base.iterdir() if p.is_dir() and p.name != ".pending"],
            key=lambda p: p.name,
        )

    def cleanup_expired(self, now: float | None = None) -> int:
        """删除超过保留期的证据目录，返回清理数量。"""
        return len(self.cleanup_expired_paths(now))

    def cleanup_expired_paths(self, now: float | None = None) -> list[Path]:
        """删除并返回过期的已提交证据目录，供调用方同步删除事件索引。"""
        now = now if now is not None else time.time()
        cutoff = now - self._retention_days * 86400
        expired = [
            directory for directory in self.list_events()
            if directory.stat().st_mtime < cutoff
        ]
        deleted: list[Path] = []
        for directory in expired:
            self._remove_path(directory)
            deleted.append(directory)
        return deleted
