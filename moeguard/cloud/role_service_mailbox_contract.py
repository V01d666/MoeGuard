"""Mailbox wire contract shared by the client and the role service.

The mailbox is the only channel between the maintainer and fully anonymous
users: a user can leave a plain-text message, the maintainer can reply, and
announcements can target client versions.  Everything here is pure validation
so both sides enforce the same limits.

Two directions, two parsing rules:

* **Requests are strict.**  The service rejects unknown keys, because the
  diagnostics block is a privacy gate and must never grow a free-form field.
  The service is always deployed before any client that sends a new key.
* **Responses are read by required subset.**  A client accepts any response
  that carries every field it needs and ignores the rest, so the service can
  add fields later without breaking installed clients.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

MAX_MESSAGE_CHARS = 500
MAX_REPLY_CHARS = 1000
MAX_ANNOUNCEMENT_TITLE_CHARS = 40
MAX_ANNOUNCEMENT_BODY_CHARS = 1000
DAILY_MESSAGE_LIMIT = 5
GLOBAL_DAILY_MESSAGE_LIMIT = 300
MESSAGE_WINDOW_SECONDS = 24 * 60 * 60
MESSAGE_LIST_LIMIT = 50
ANNOUNCEMENT_LIST_LIMIT = 20
MAX_MAILBOX_REQUEST_BYTES = 16 * 1024
MAX_RECENT_ERROR_CODES = 5

_CLIENT_MESSAGE_ID_RE = re.compile(r"^cm_[a-f0-9]{32}$")
_APP_VERSION_RE = re.compile(r"^(?:[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}|unknown)$")
_WINDOWS_MAJOR_RE = re.compile(r"^(?:[0-9]{1,3}|unknown)$")
_ARCHITECTURES = frozenset({"x86_64", "arm64", "unknown"})
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SERIES_RE = re.compile(r"^(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?\.x$")
# Direction overrides can make a message render differently from its bytes in
# the operator's terminal; nothing a user writes needs them.
_BIDI_CONTROLS = frozenset(
    chr(value) for value in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
)


def normalize_text(value: Any, *, max_chars: int, label: str = "text") -> str:
    """Return trimmed plain text, or raise ValueError.

    Newlines and tabs survive; every other control character is refused so an
    escape sequence can never reach the operator's terminal.
    """

    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError(f"{label} is empty")
    if len(text) > max_chars:
        raise ValueError(f"{label} is too long")
    for character in text:
        if character in "\n\t":
            continue
        if unicodedata.category(character) in {"Cc", "Cs"} or character in _BIDI_CONTROLS:
            raise ValueError(f"{label} contains a control character")
    return text


def valid_client_message_id(value: Any) -> str:
    if not isinstance(value, str) or not _CLIENT_MESSAGE_ID_RE.fullmatch(value):
        raise ValueError("client message ID is invalid")
    return value


@dataclass(frozen=True)
class MailboxDiagnostics:
    """What a user may choose to attach: never a path, prompt or image."""

    app_version: str = "unknown"
    windows_major: str = "unknown"
    architecture: str = "unknown"
    recent_error_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.app_version, str) or not _APP_VERSION_RE.fullmatch(
            self.app_version
        ):
            raise ValueError("diagnostic app version is invalid")
        if not isinstance(self.windows_major, str) or not _WINDOWS_MAJOR_RE.fullmatch(
            self.windows_major
        ):
            raise ValueError("diagnostic Windows version is invalid")
        if self.architecture not in _ARCHITECTURES:
            raise ValueError("diagnostic architecture is invalid")
        if not isinstance(self.recent_error_codes, tuple) or len(
            self.recent_error_codes
        ) > MAX_RECENT_ERROR_CODES:
            raise ValueError("diagnostic error codes are invalid")
        for code in self.recent_error_codes:
            if not isinstance(code, str) or not _ERROR_CODE_RE.fullmatch(code):
                raise ValueError("diagnostic error code is invalid")

    _KEYS = frozenset(
        {"app_version", "windows_major", "architecture", "recent_error_codes"}
    )

    @classmethod
    def from_dict(cls, raw: Any) -> MailboxDiagnostics:
        if not isinstance(raw, dict) or set(raw) != cls._KEYS:
            raise ValueError("diagnostics schema is invalid")
        codes = raw["recent_error_codes"]
        if not isinstance(codes, list):
            raise ValueError("diagnostic error codes are invalid")
        return cls(
            app_version=raw["app_version"],
            windows_major=raw["windows_major"],
            architecture=raw["architecture"],
            recent_error_codes=tuple(codes),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_version": self.app_version,
            "windows_major": self.windows_major,
            "architecture": self.architecture,
            "recent_error_codes": list(self.recent_error_codes),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True)


@dataclass(frozen=True)
class MailboxMessageRequest:
    client_message_id: str
    body: str
    diagnostics: MailboxDiagnostics | None = None

    _REQUIRED = frozenset({"schema_version", "client_message_id", "body"})
    _ALLOWED = _REQUIRED | {"diagnostics"}

    @classmethod
    def from_dict(cls, raw: Any) -> MailboxMessageRequest:
        if not isinstance(raw, dict) or not cls._REQUIRED <= set(raw) <= cls._ALLOWED:
            raise ValueError("mailbox message schema is invalid")
        if raw["schema_version"] != 1 or isinstance(raw["schema_version"], bool):
            raise ValueError("mailbox message version is unsupported")
        diagnostics = raw.get("diagnostics")
        return cls(
            client_message_id=valid_client_message_id(raw["client_message_id"]),
            body=normalize_text(raw["body"], max_chars=MAX_MESSAGE_CHARS, label="message"),
            diagnostics=(
                None if diagnostics is None else MailboxDiagnostics.from_dict(diagnostics)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "client_message_id": self.client_message_id,
            "body": self.body,
        }
        if self.diagnostics is not None:
            payload["diagnostics"] = self.diagnostics.to_dict()
        return payload


# -- versions ---------------------------------------------------------------


def parse_version(value: str) -> tuple[Any, ...] | None:
    """Return a SemVer 2.0 precedence key, or None when the text is not SemVer."""

    match = _SEMVER_RE.fullmatch(value or "")
    if match is None:
        return None
    major, minor, patch, prerelease = match.groups()
    core = (int(major), int(minor), int(patch))
    if prerelease is None:
        # A release outranks every pre-release of the same core version.
        return (*core, 1, ())
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in prerelease.split(".")
    )
    return (*core, 0, identifiers)


def valid_version_bound(value: str) -> str:
    if value and parse_version(value) is None:
        raise ValueError("version bound is not a semantic version")
    return value


def version_in_range(version: str, minimum: str = "", maximum: str = "") -> bool:
    """True when ``minimum <= version < maximum``; empty bounds are open.

    A version that cannot be parsed matches only a fully open range, so a
    malformed build never receives an announcement aimed at someone else.
    """

    if not minimum and not maximum:
        return True
    key = parse_version(version)
    if key is None:
        return False
    if minimum:
        lower = parse_version(minimum)
        if lower is None or key < lower:
            return False
    if maximum:
        upper = parse_version(maximum)
        if upper is None or key >= upper:
            return False
    return True


def series_bounds(series: str) -> tuple[str, str]:
    """Expand ``0.3.x`` to ``("0.3.0-0", "0.4.0-0")``, pre-releases included."""

    match = _SERIES_RE.fullmatch(series.strip())
    if match is None:
        raise ValueError("version series must look like 0.3.x or 1.x")
    major = int(match.group(1))
    if match.group(2) is None:
        return f"{major}.0.0-0", f"{major + 1}.0.0-0"
    minor = int(match.group(2))
    return f"{major}.{minor}.0-0", f"{major}.{minor + 1}.0-0"


# -- responses (read by required subset) ------------------------------------


def _require(raw: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not fields <= set(raw):
        raise ValueError(f"{label} schema is invalid")
    return raw


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True)
class MailboxPostReceipt:
    message_id: str
    created_at: int
    remaining_today: int

    @classmethod
    def from_response(cls, raw: Any) -> MailboxPostReceipt:
        data = _require(
            raw,
            frozenset({"message_id", "created_at", "remaining_today"}),
            "mailbox receipt",
        )
        return cls(
            message_id=str(data["message_id"]),
            created_at=_int(data["created_at"], "message time"),
            remaining_today=_int(data["remaining_today"], "remaining messages"),
        )


@dataclass(frozen=True)
class MailboxEntry:
    message_id: str
    direction: str
    body: str
    created_at: int

    @classmethod
    def from_response(cls, raw: Any) -> MailboxEntry:
        data = _require(
            raw,
            frozenset({"message_id", "direction", "body", "created_at"}),
            "mailbox message",
        )
        if data["direction"] not in {"user", "operator"}:
            raise ValueError("mailbox message direction is invalid")
        if not isinstance(data["body"], str):
            raise ValueError("mailbox message body is invalid")
        return cls(
            message_id=str(data["message_id"]),
            direction=str(data["direction"]),
            body=data["body"],
            created_at=_int(data["created_at"], "message time"),
        )


@dataclass(frozen=True)
class MailboxThread:
    messages: tuple[MailboxEntry, ...]
    max_body_chars: int
    daily_limit: int
    remaining_today: int

    @classmethod
    def from_response(cls, raw: Any) -> MailboxThread:
        data = _require(raw, frozenset({"messages", "limits"}), "mailbox")
        if not isinstance(data["messages"], list):
            raise ValueError("mailbox messages are invalid")
        limits = _require(
            data["limits"],
            frozenset({"max_body_chars", "daily_limit", "remaining_today"}),
            "mailbox limits",
        )
        return cls(
            messages=tuple(MailboxEntry.from_response(item) for item in data["messages"]),
            max_body_chars=_int(limits["max_body_chars"], "message limit"),
            daily_limit=_int(limits["daily_limit"], "daily limit"),
            remaining_today=_int(limits["remaining_today"], "remaining messages"),
        )


@dataclass(frozen=True)
class Announcement:
    announcement_id: str
    title: str
    body: str
    published_at: int
    expires_at: int
    min_version: str = ""
    max_version: str = ""

    @classmethod
    def from_response(cls, raw: Any) -> Announcement:
        data = _require(
            raw,
            frozenset(
                {
                    "announcement_id",
                    "title",
                    "body",
                    "published_at",
                    "expires_at",
                    "min_version",
                    "max_version",
                }
            ),
            "announcement",
        )
        for key in ("title", "body", "min_version", "max_version"):
            if not isinstance(data[key], str):
                raise ValueError("announcement text is invalid")
        return cls(
            announcement_id=str(data["announcement_id"]),
            title=data["title"],
            body=data["body"],
            published_at=_int(data["published_at"], "announcement time"),
            expires_at=_int(data["expires_at"], "announcement expiry"),
            min_version=data["min_version"],
            max_version=data["max_version"],
        )

    def applies_to(self, version: str) -> bool:
        return version_in_range(version, self.min_version, self.max_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "announcement_id": self.announcement_id,
            "title": self.title,
            "body": self.body,
            "published_at": self.published_at,
            "expires_at": self.expires_at,
            "min_version": self.min_version,
            "max_version": self.max_version,
        }


def parse_announcements(raw: Any) -> tuple[Announcement, ...]:
    data = _require(raw, frozenset({"announcements"}), "announcement list")
    if not isinstance(data["announcements"], list):
        raise ValueError("announcement list is invalid")
    return tuple(Announcement.from_response(item) for item in data["announcements"])
