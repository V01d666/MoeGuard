"""本地 SQLite 数据库。

存储安防事件记录（时间、类型、证据路径）等结构化数据。
人脸特征等高敏感数据走 OwnerProfileStore 加密存储，不放此处。

表结构：
- incidents:       陌生人事件记录（时间、类型、证据、摘要）
- patrol_sessions: 值守会话记录（开始/结束、事件数、触发方式）
- usage_daily:     M4.5 dogfood 日级使用统计（本地离线，零网络上报）
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 4


class Database:
    """萌卫本地数据库。"""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        assert self._conn is not None
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current > _SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema v{current} is newer than supported v{_SCHEMA_VERSION}"
            )

        migrations = (
            self._migrate_v1_core_tables,
            self._migrate_v2_incident_summary_and_indices,
            self._migrate_v3_usage_daily,
            self._migrate_v4_incident_event_uuid,
        )
        if current == _SCHEMA_VERSION:
            return

        try:
            self._conn.execute("BEGIN")
            while current < _SCHEMA_VERSION:
                migrations[current]()
                current += 1
                self._conn.execute(f"PRAGMA user_version = {current}")
            self._conn.commit()
            logger.info("Migrated local database schema to v%d", current)
        except Exception:
            self._conn.rollback()
            raise

    def _migrate_v1_core_tables(self) -> None:
        """创建最初的事件与值守会话表（对既有库幂等）。"""
        assert self._conn is not None
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL    NOT NULL,
                kind      TEXT    NOT NULL,   -- 'stranger'
                evidence  TEXT                -- 证据目录路径
            )
            """
        )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patrol_sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                start_ts        REAL    NOT NULL,
                end_ts          REAL    NOT NULL,
                incident_count  INTEGER NOT NULL DEFAULT 0,
                trigger         TEXT    NOT NULL   -- 'lock_screen' / 'manual' / 'idle'
            )
            """
        )

    def _migrate_v2_incident_summary_and_indices(self) -> None:
        """为旧 incidents 表补摘要列和查询索引。"""
        assert self._conn is not None
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(incidents)").fetchall()
        }
        if "summary" not in columns:
            self._conn.execute("ALTER TABLE incidents ADD COLUMN summary TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_ts ON incidents(ts)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_patrol_start_ts ON patrol_sessions(start_ts)"
        )

    def _migrate_v3_usage_daily(self) -> None:
        """加入本地 dogfood 日聚合表。"""
        assert self._conn is not None
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_daily (
                date                  TEXT PRIMARY KEY,  -- 'YYYY-MM-DD' (local)
                patrol_count          INTEGER NOT NULL DEFAULT 0,
                patrol_seconds        REAL    NOT NULL DEFAULT 0,
                click_count           INTEGER NOT NULL DEFAULT 0,
                incident_count        INTEGER NOT NULL DEFAULT 0,
                recognize_error_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )

    def _migrate_v4_incident_event_uuid(self) -> None:
        """Add a stable cross-media transaction key to newly recorded events."""
        assert self._conn is not None
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(incidents)").fetchall()
        }
        if "event_uuid" not in columns:
            self._conn.execute("ALTER TABLE incidents ADD COLUMN event_uuid TEXT")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_event_uuid "
            "ON incidents(event_uuid)"
        )

    @property
    def schema_version(self) -> int:
        """当前已连接数据库的 SQLite schema 版本。"""
        assert self._conn is not None
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # ------------------------------------------------------------------
    # incidents 表操作
    # ------------------------------------------------------------------
    def log_incident_atomic(
        self,
        ts: float,
        kind: str,
        evidence: str | None = None,
        summary: str | None = None,
    ) -> int:
        """在当前事务中记录一次安防事件，返回新插入行的 id。

        与 ``log_incident`` 不同，此方法不自动 commit，调用方必须
        在同事务中完成所有写入后统一 commit 或 rollback。
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "INSERT INTO incidents (ts, kind, evidence, summary) "
            "VALUES (?, ?, ?, ?)",
            (ts, kind, evidence, summary),
        )
        return int(cur.lastrowid) if cur.lastrowid is not None else 0

    def bump_usage_atomic(
        self,
        date: str,
        *,
        patrol: int = 0,
        patrol_seconds: float = 0.0,
        clicks: int = 0,
        incidents: int = 0,
        recognize_errors: int = 0,
    ) -> None:
        """在当前事务中增加使用计数（不自动 commit）。

        与 ``bump_usage`` 不同，此方法不自动 commit。
        """
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO usage_daily (date, patrol_count, patrol_seconds, "
            "click_count, incident_count, recognize_error_count) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "patrol_count          = patrol_count          + excluded.patrol_count, "
            "patrol_seconds        = patrol_seconds        + excluded.patrol_seconds, "
            "click_count           = click_count           + excluded.click_count, "
            "incident_count        = incident_count        + excluded.incident_count, "
            "recognize_error_count = recognize_error_count + excluded.recognize_error_count",
            (date, patrol, patrol_seconds, clicks, incidents, recognize_errors),
        )

    def begin_transaction(self) -> None:
        """显式开启事务（SQLite 自动 BEGIN，此方法用于语义清晰）。"""
        assert self._conn is not None
        self._conn.execute("BEGIN")

    def commit_transaction(self) -> None:
        """提交当前事务。"""
        assert self._conn is not None
        self._conn.commit()

    def rollback_transaction(self) -> None:
        """回滚当前事务。"""
        assert self._conn is not None
        self._conn.rollback()

    def log_incident(
        self,
        ts: float,
        kind: str,
        evidence: str | None = None,
        summary: str | None = None,
    ) -> int:
        """记录一次安防事件，返回新插入行的 id。

        Args:
            ts:      事件时间戳（Unix 秒）。
            kind:    事件类型，如 'stranger'。
            evidence: 证据目录路径（可选）。
            summary: 事件摘要（可选，用于值守汇报）。

        Returns:
            新插入事件的 id。
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "INSERT INTO incidents (ts, kind, evidence, summary) "
            "VALUES (?, ?, ?, ?)",
            (ts, kind, evidence, summary),
        )
        self._conn.commit()
        return int(cur.lastrowid) if cur.lastrowid is not None else 0

    def commit_evidence_event(
        self,
        *,
        event_uuid: str,
        ts: float,
        kind: str,
        evidence: str,
        summary: str,
        usage_date: str,
    ) -> tuple[int, bool]:
        """Atomically persist one evidence event and its daily counter.

        ``event_uuid`` also names the evidence directory.  A duplicate
        delivery returns the original event without another usage increment.
        """
        assert self._conn is not None
        existing = self._conn.execute(
            "SELECT id FROM incidents WHERE event_uuid = ?", (event_uuid,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"]), False

        try:
            self._conn.execute("BEGIN")
            cur = self._conn.execute(
                "INSERT INTO incidents (ts, kind, evidence, summary, event_uuid) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, kind, evidence, summary, event_uuid),
            )
            self._bump_usage(usage_date, incidents=1)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return int(cur.lastrowid) if cur.lastrowid is not None else 0, True

    def update_incident_evidence(self, incident_id: int, evidence_path: str) -> None:
        """更新事件的证据路径。

        在证据录制完成后回填路径用。

        Args:
            incident_id: 事件 id（log_incident 返回值）。
            evidence_path: 证据目录路径。
        """
        assert self._conn is not None
        self._conn.execute(
            "UPDATE incidents SET evidence = ? WHERE id = ?",
            (evidence_path, incident_id),
        )
        self._conn.commit()

    def update_incident_summary(self, incident_id: int, summary: str) -> None:
        """更新事件摘要。

        Args:
            incident_id: 事件 id。
            summary: 摘要文本。
        """
        assert self._conn is not None
        self._conn.execute(
            "UPDATE incidents SET summary = ? WHERE id = ?",
            (summary, incident_id),
        )
        self._conn.commit()

    def delete_all_incidents(self) -> int:
        """删除全部本地事件索引，返回删除行数（撤回同意时使用）。"""
        assert self._conn is not None
        cur = self._conn.execute("DELETE FROM incidents")
        self._conn.commit()
        return cur.rowcount

    def delete_incident(self, incident_id: int) -> bool:
        """删除单条事件索引，返回是否实际删除。"""
        assert self._conn is not None
        cur = self._conn.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def delete_incidents_by_evidence(self, evidence_paths: list[str]) -> int:
        """删除指定证据目录对应的事件索引，返回删除条数。"""
        if not evidence_paths:
            return 0
        assert self._conn is not None
        placeholders = ", ".join("?" for _ in evidence_paths)
        cur = self._conn.execute(
            f"DELETE FROM incidents WHERE evidence IN ({placeholders})",
            evidence_paths,
        )
        self._conn.commit()
        return cur.rowcount

    def delete_incidents_with_missing_evidence(self) -> int:
        """补偿删除已不存在证据目录的事件索引。

        文件清理与 SQLite 不能组成同一个原子事务；若目录删除后进程中断或
        索引删除失败，下一次维护会通过此方法收敛到“无媒体即无索引”。
        """
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT id, evidence FROM incidents WHERE evidence IS NOT NULL"
        ).fetchall()
        missing_ids = [int(row["id"]) for row in rows if not Path(row["evidence"]).exists()]
        if not missing_ids:
            return 0
        placeholders = ", ".join("?" for _ in missing_ids)
        cur = self._conn.execute(
            f"DELETE FROM incidents WHERE id IN ({placeholders})", missing_ids
        )
        self._conn.commit()
        return cur.rowcount

    def delete_all_usage(self) -> int:
        """删除全部本地使用统计，返回删除行数（撤回同意时使用）。"""
        assert self._conn is not None
        cur = self._conn.execute("DELETE FROM usage_daily")
        self._conn.commit()
        return cur.rowcount

    def incidents_since(self, ts: float) -> list[sqlite3.Row]:
        """查询某时间点之后的事件（用于回座汇报）。"""
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT * FROM incidents WHERE ts >= ? ORDER BY ts",
            (ts,),
        )
        return cur.fetchall()

    # ------------------------------------------------------------------
    # patrol_sessions 表操作
    # ------------------------------------------------------------------
    def log_patrol_session(
        self,
        start_ts: float,
        end_ts: float,
        incident_count: int,
        trigger: str,
    ) -> int:
        """记录一次值守会话，返回新插入行的 id。

        Args:
            start_ts:       值守开始时间戳。
            end_ts:         值守结束时间戳。
            incident_count: 本次值守中的陌生人事件数。
            trigger:        触发方式（'lock_screen' / 'manual' / 'idle'）。

        Returns:
            新插入会话的 id。
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "INSERT INTO patrol_sessions "
            "(start_ts, end_ts, incident_count, trigger) "
            "VALUES (?, ?, ?, ?)",
            (start_ts, end_ts, incident_count, trigger),
        )
        self._conn.commit()
        return int(cur.lastrowid) if cur.lastrowid is not None else 0

    def patrol_sessions_since(self, ts: float) -> list[sqlite3.Row]:
        """查询某时间点之后的值守会话记录。"""
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT * FROM patrol_sessions WHERE start_ts >= ? ORDER BY start_ts",
            (ts,),
        )
        return cur.fetchall()

    # ------------------------------------------------------------------
    # usage_daily 表操作（M4.5 dogfood 埋点）
    # ------------------------------------------------------------------
    def bump_usage(
        self,
        date: str,
        *,
        patrol: int = 0,
        patrol_seconds: float = 0.0,
        clicks: int = 0,
        incidents: int = 0,
        recognize_errors: int = 0,
    ) -> None:
        """增加指定日期的使用计数（upsert 语义，不存在则创建）。

        所有字段以增量方式累加，线程安全由 SQLite 自身保证。
        """
        assert self._conn is not None
        self._bump_usage(
            date,
            patrol=patrol,
            patrol_seconds=patrol_seconds,
            clicks=clicks,
            incidents=incidents,
            recognize_errors=recognize_errors,
        )
        self._conn.commit()

    def _bump_usage(
        self,
        date: str,
        *,
        patrol: int = 0,
        patrol_seconds: float = 0.0,
        clicks: int = 0,
        incidents: int = 0,
        recognize_errors: int = 0,
    ) -> None:
        """Apply a usage increment inside the caller's current transaction."""
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO usage_daily (date, patrol_count, patrol_seconds, "
            "click_count, incident_count, recognize_error_count) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "patrol_count          = patrol_count          + excluded.patrol_count, "
            "patrol_seconds        = patrol_seconds        + excluded.patrol_seconds, "
            "click_count           = click_count           + excluded.click_count, "
            "incident_count        = incident_count        + excluded.incident_count, "
            "recognize_error_count = recognize_error_count + excluded.recognize_error_count",
            (date, patrol, patrol_seconds, clicks, incidents, recognize_errors),
        )

    def daily_usage_since(self, ts: float) -> list[sqlite3.Row]:
        """查询指定时间戳之后（按日期首日）的日级使用统计。"""
        assert self._conn is not None
        from datetime import datetime

        start_date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        cur = self._conn.execute(
            "SELECT * FROM usage_daily WHERE date >= ? ORDER BY date",
            (start_date,),
        )
        return cur.fetchall()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
