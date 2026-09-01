"""Versioned local acknowledgement for the v0.2 Preview data notice."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

PILOT_NOTICE_VERSION = "2026-09-v0.2-preview.1"
PILOT_NOTICE_TEXT = (
    "内测期间，为改进生成效果，你提交的角色描述、参考图、生成结果和必要的"
    "运行信息会暂存在我们的海外服务器，最长 30 天。请勿上传真人或私密内容。"
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
