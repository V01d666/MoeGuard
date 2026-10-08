"""Client mailbox privacy helpers and bounded local read markers.

Message bodies and diagnostics are never cached here. Read markers contain
only SHA-256 fingerprints and timestamps, expire after 30 days, and are capped
at 200 entries / 32 KiB. Opening the store prunes expired entries; clear() lets
the user remove the remaining metadata. No background polling is required.
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
import uuid
from collections.abc import Callable, Iterable
from hashlib import sha256
from pathlib import Path

from moeguard.cloud.role_service_mailbox_contract import MailboxDiagnostics
from moeguard.utils.version import installed_version

READ_MARKER_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_READ_MARKERS = 200
MAX_READ_MARKER_BYTES = 32 * 1024
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")


def mailbox_diagnostics(recent_error_codes: tuple[str, ...] = ()) -> MailboxDiagnostics:
    """Collect only version/major OS/architecture and caller-validated codes."""
    release = platform.release()
    major = release if re.fullmatch(r"[0-9]{1,3}", release) else "unknown"
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(platform.machine().lower(), "unknown")
    return MailboxDiagnostics(
        app_version=installed_version(),
        windows_major=major,
        architecture=architecture,
        recent_error_codes=recent_error_codes[-5:],
    )


def read_fingerprint(namespace: str, item_id: str, revision: int) -> str:
    """Keep account identity and message IDs out of the read-marker file."""
    return sha256(json.dumps([namespace, item_id, revision]).encode()).hexdigest()


class MailboxReadStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    def _write(self, seen: dict[str, int]) -> None:
        if self.path.is_symlink():
            raise ValueError("mailbox read marker path is unsafe")
        if not seen:
            self.path.unlink(missing_ok=True)
            return
        bounded = dict(
            sorted(seen.items(), key=lambda item: (item[1], item[0]))[-MAX_READ_MARKERS:]
        )
        payload = json.dumps({"schema_version": 1, "seen": bounded}, sort_keys=True).encode()
        if len(payload) > MAX_READ_MARKER_BYTES:
            raise ValueError("mailbox read markers are too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self) -> dict[str, int]:
        if self.path.is_symlink():
            raise ValueError("mailbox read marker path is unsafe")
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > MAX_READ_MARKER_BYTES:
            raise ValueError("mailbox read markers are too large")
        try:
            raw = json.loads(self.path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("mailbox read markers are invalid") from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "seen"}
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or not isinstance(raw["seen"], dict)
            or len(raw["seen"]) > MAX_READ_MARKERS
        ):
            raise ValueError("mailbox read marker schema is invalid")
        seen = raw["seen"]
        if any(
            not _DIGEST_RE.fullmatch(key) or type(stamp) is not int or stamp < 0
            for key, stamp in seen.items()
        ):
            raise ValueError("mailbox read marker value is invalid")
        now = int(self._clock())
        pruned = {
            key: stamp
            for key, stamp in seen.items()
            if now - READ_MARKER_RETENTION_SECONDS <= stamp <= now + 300
        }
        if pruned != seen:
            self._write(pruned)
        return pruned

    def seen(self) -> frozenset[str]:
        return frozenset(self._load())

    def mark_read(self, fingerprints: Iterable[str]) -> None:
        seen = self._load()
        now = max(0, int(self._clock()))
        for fingerprint in fingerprints:
            if not isinstance(fingerprint, str) or not _DIGEST_RE.fullmatch(fingerprint):
                raise ValueError("mailbox read fingerprint is invalid")
            seen[fingerprint] = now
        self._write(seen)

    def clear(self) -> None:
        # Unlinking the marker also recovers a corrupt file, without reading it.
        self.path.unlink(missing_ok=True)
