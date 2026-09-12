"""Versioned local acknowledgement for the v0.2 Preview data notice."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

PILOT_NOTICE_VERSION = "2026-09-v0.2-preview.2"
PILOT_NOTICE_TEXT = (
    "内测期间会保留桌宠工坊的提示词、参考图片与生成结果，用于改进生成体验。"
)


class RolePilotNoticeStore:
    """Atomic, non-secret record; corrupted data never implies acceptance."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def accepted(self) -> bool:
        if not self.path.is_file() or self.path.is_symlink():
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(raw, dict)
            and set(raw) == {"schema_version", "notice_version", "accepted"}
            and raw["schema_version"] == 1
            and raw["notice_version"] == PILOT_NOTICE_VERSION
            and raw["accepted"] is True
        )

    def accept(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "notice_version": PILOT_NOTICE_VERSION,
                        "accepted": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
