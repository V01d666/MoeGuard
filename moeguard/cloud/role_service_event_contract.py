"""Strict, content-free Preview client-event contract shared by client and service."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from moeguard.roles.spec import OFFICIAL_ACTIONS

MAX_CLIENT_EVENT_BATCH_COUNT = 50
MAX_CLIENT_EVENT_BATCH_BYTES = 64 * 1024

_REMOTE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_APP_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}")

_SCREENS = frozenset({"settings", "pet_workshop", "role_preview", "runtime"})
_STAGES = frozenset(
    {"role_library", "create_identity", "select_animate", "preview_install", "recovery"}
)
_ENTRYPOINTS = frozenset({"settings", "tray", "startup", "workbench", "recovery"})
_OPERATIONS = frozenset(
    {"identity_candidates", "initial_package", "action_revision", "appearance_revision"}
)
_INPUT_KINDS = frozenset({"text", "image"})
_INTERRUPT_REASONS = frozenset(
    {
        "window_closed",
        "window_hidden",
        "application_exit",
        "network_error",
        "service_error",
        "user_cancelled",
    }
)
_CLIENT_ERROR_CODES = frozenset(
    {
        "network_unavailable",
        "service_timeout",
        "service_unavailable",
        "authentication_failed",
        "request_rejected",
        "request_too_large",
        "invalid_result",
        "hash_mismatch",
        "archive_invalid",
        "local_storage_failed",
        "task_failed",
        "task_not_found",
    }
)

_PROPERTY_LIMITS = {
    "candidate_count": (1, 4),
    "candidate_index": (0, 3),
    "action_count": (1, len(OFFICIAL_ACTIONS)),
    "duration_ms": (0, 7 * 24 * 60 * 60 * 1000),
    "retry_count": (0, 1000),
    "bytes_received": (0, 512 * 1024 * 1024),
    "display_scale_percent": (50, 400),
}

_COMMON_CONTEXT = frozenset({"screen", "stage", "entrypoint", "operation"})
_RUNTIME_CONTEXT = frozenset(
    {"windows_major", "architecture", "display_scale_percent"}
)


@dataclass(frozen=True)
class _EventRule:
    allowed: frozenset[str]
    required: frozenset[str] = frozenset()


def _rule(*allowed: str, required: tuple[str, ...] = ()) -> _EventRule:
    return _EventRule(
        allowed=_COMMON_CONTEXT.union(allowed),
        required=frozenset(required),
    )


_EVENT_RULES = {
    "preview_session_started": _rule(
        *_RUNTIME_CONTEXT, "foreground", required=("entrypoint",)
    ),
    "workbench_opened": _rule("foreground", required=("screen", "entrypoint")),
    "flow_started": _rule("input_kind", required=("operation", "input_kind")),
    "candidate_submit_clicked": _rule(
        "input_kind",
        "candidate_count",
        required=("operation", "input_kind", "candidate_count"),
    ),
    "candidate_gallery_rendered": _rule(
        "input_kind", "candidate_count", "duration_ms", "package_ready"
    ),
    "candidate_exposed": _rule(
        "input_kind",
        "candidate_count",
        "candidate_index",
        "foreground",
        required=("input_kind", "candidate_count", "candidate_index"),
    ),
    "candidate_selected": _rule(
        "input_kind",
        "candidate_count",
        "candidate_index",
        required=("input_kind", "candidate_count", "candidate_index"),
    ),
    "package_submit_clicked": _rule(
        "input_kind",
        "action_names",
        "action_count",
        "package_ready",
        required=("operation", "action_names", "action_count"),
    ),
    "result_verified": _rule("duration_ms", "bytes_received", "package_ready"),
    "preview_ready": _rule("duration_ms", "package_ready"),
    "install_succeeded": _rule("duration_ms", "package_ready"),
    "role_activated": _rule("package_ready"),
    "wait_entered": _rule("foreground"),
    "wait_heartbeat": _rule("foreground", "duration_ms"),
    "wait_interrupted": _rule(
        "foreground", "reason", "retry_count", required=("reason",)
    ),
    "wait_resumed": _rule("foreground", "duration_ms", "retry_count"),
    "task_recovery_started": _rule("retry_count"),
    "task_recovery_succeeded": _rule("duration_ms", "retry_count"),
    "task_recovery_failed": _rule(
        "duration_ms", "retry_count", "error_code", required=("error_code",)
    ),
    "download_started": _rule("retry_count"),
    "download_completed": _rule("duration_ms", "bytes_received", "retry_count"),
    "client_error_shown": _rule(
        "reason", "error_code", "retry_count", required=("error_code",)
    ),
}


def _uuid4(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{label} must be a canonical random UUIDv4")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} is outside the supported range")
    return value


def _enum(value: Any, label: str, choices: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{label} is unsupported")
    return value


def _property_value(name: str, value: Any) -> object:
    if name == "screen":
        return _enum(value, name, _SCREENS)
    if name == "stage":
        return _enum(value, name, _STAGES)
    if name == "entrypoint":
        return _enum(value, name, _ENTRYPOINTS)
    if name == "operation":
        return _enum(value, name, _OPERATIONS)
    if name == "input_kind":
        return _enum(value, name, _INPUT_KINDS)
    if name == "reason":
        return _enum(value, name, _INTERRUPT_REASONS)
    if name == "error_code":
        return _enum(value, name, _CLIENT_ERROR_CODES)
    if name == "windows_major":
        return _enum(value, name, frozenset({"10", "11", "unknown"}))
    if name == "architecture":
        return _enum(value, name, frozenset({"x86_64", "arm64", "unknown"}))
    if name in _PROPERTY_LIMITS:
        minimum, maximum = _PROPERTY_LIMITS[name]
        return _integer(value, name, minimum, maximum)
    if name in {"foreground", "package_ready"}:
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        return value
    if name == "action_names":
        if not isinstance(value, list) or not value:
            raise ValueError("action_names must be a non-empty array")
        if not all(isinstance(item, str) for item in value):
            raise ValueError("action_names must contain action IDs")
        if len(value) != len(set(value)) or set(value).difference(OFFICIAL_ACTIONS):
            raise ValueError("action_names contains duplicate or unsupported actions")
        return tuple(action for action in OFFICIAL_ACTIONS if action in value)
    raise ValueError(f"unknown client event property: {name}")


@dataclass(frozen=True)
class ClientEvent:
    event_id: str
    event_name: str
    client_at_ms: int
    sequence: int
    session_id: str
    journey_id: str
    remote_task_id: str
    app_version: str
    properties: tuple[tuple[str, object], ...]

    @classmethod
    def from_dict(cls, raw: Any) -> ClientEvent:
        required = {
            "event_id",
            "event_name",
            "client_at_ms",
            "sequence",
            "session_id",
            "journey_id",
            "app_version",
            "properties",
        }
        allowed = required | {"remote_task_id"}
        if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - allowed:
            raise ValueError("client event schema is invalid")
        event_name = raw["event_name"]
        if not isinstance(event_name, str) or event_name not in _EVENT_RULES:
            raise ValueError("client event name is unsupported")
        remote_task_id = raw.get("remote_task_id", "")
        if not isinstance(remote_task_id, str) or (
            remote_task_id and not _REMOTE_TASK_ID_RE.fullmatch(remote_task_id)
        ):
            raise ValueError("remote_task_id is invalid")
        app_version = raw["app_version"]
        if not isinstance(app_version, str) or not _APP_VERSION_RE.fullmatch(app_version):
            raise ValueError("app_version is invalid")
        properties = raw["properties"]
        rule = _EVENT_RULES[event_name]
        if not isinstance(properties, dict):
            raise ValueError("client event properties must be an object")
        if not all(isinstance(name, str) for name in properties):
            raise ValueError("client event property names must be strings")
        names = set(properties)
        if names - rule.allowed or not rule.required.issubset(names):
            raise ValueError("client event properties do not match the event contract")
        normalized = tuple(
            (name, _property_value(name, properties[name])) for name in sorted(names)
        )
        values = dict(normalized)
        if "candidate_index" in values and "candidate_count" in values:
            if int(values["candidate_index"]) >= int(values["candidate_count"]):
                raise ValueError("candidate_index must be smaller than candidate_count")
        if "action_names" in values and "action_count" in values:
            if len(values["action_names"]) != int(values["action_count"]):
                raise ValueError("action_count does not match action_names")
        return cls(
            event_id=_uuid4(raw["event_id"], "event_id"),
            event_name=event_name,
            client_at_ms=_integer(raw["client_at_ms"], "client_at_ms", 0, 2**63 - 1),
            sequence=_integer(raw["sequence"], "sequence", 0, 2_147_483_647),
            session_id=_uuid4(raw["session_id"], "session_id"),
            journey_id=_uuid4(raw["journey_id"], "journey_id"),
            remote_task_id=remote_task_id,
            app_version=app_version,
            properties=normalized,
        )

    def properties_json(self) -> str:
        payload = {
            name: list(value) if isinstance(value, tuple) else value
            for name, value in self.properties
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "event_id": self.event_id,
            "event_name": self.event_name,
            "client_at_ms": self.client_at_ms,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "journey_id": self.journey_id,
            "app_version": self.app_version,
            "properties": json.loads(self.properties_json()),
        }
        if self.remote_task_id:
            payload["remote_task_id"] = self.remote_task_id
        return payload

    def persisted_values(self) -> tuple[object, ...]:
        return (
            self.event_name,
            self.client_at_ms,
            self.sequence,
            self.session_id,
            self.journey_id,
            self.remote_task_id,
            self.app_version,
            self.properties_json(),
        )


def parse_client_event_batch(raw: Any) -> tuple[ClientEvent, ...]:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "events"}:
        raise ValueError("client event envelope schema is invalid")
    if isinstance(raw["schema_version"], bool) or raw["schema_version"] != 1:
        raise ValueError("client event envelope version is unsupported")
    events = raw["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_CLIENT_EVENT_BATCH_COUNT:
        raise ValueError("client event batch count is invalid")
    return tuple(ClientEvent.from_dict(event) for event in events)


def serialize_client_event_batch(events: tuple[ClientEvent, ...]) -> bytes:
    """Serialize one service-compatible batch and enforce the wire limits."""

    if not 1 <= len(events) <= MAX_CLIENT_EVENT_BATCH_COUNT:
        raise ValueError("client event batch count is invalid")
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "events": [event.to_dict() for event in events],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    if len(payload) > MAX_CLIENT_EVENT_BATCH_BYTES:
        raise ValueError("client event batch is too large")
    return payload
