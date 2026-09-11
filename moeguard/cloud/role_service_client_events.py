"""Protected, bounded and non-blocking Preview client-event outbox."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from moeguard.cloud.role_service_event_contract import (
    MAX_CLIENT_EVENT_BATCH_COUNT,
    ClientEvent,
    serialize_client_event_batch,
)
from moeguard.cloud.role_service_http_client import RoleServiceClientEventReceipt
from moeguard.cloud.role_service_session import CredentialProtector, WindowsDpapiProtector

MAX_CLIENT_EVENT_OUTBOX_EVENTS = 2_000
MAX_CLIENT_EVENT_OUTBOX_BYTES = 1024 * 1024
MAX_CLIENT_EVENT_AGE_MS = 7 * 24 * 60 * 60 * 1000
INITIAL_RETRY_DELAY_MS = 5_000
MAX_RETRY_DELAY_MS = 60 * 60 * 1000
_MAX_RETRY_ATTEMPT = 20
_PROTECTION_LABEL = "windows-dpapi-current-user"


class ClientEventTransport(Protocol):
    def submit_client_events(
        self, events: tuple[ClientEvent, ...]
    ) -> RoleServiceClientEventReceipt: ...


@dataclass(frozen=True)
class ClientEventFlushResult:
    attempted_count: int
    accepted_count: int
    duplicate_count: int
    pending_count: int
    deferred: bool = False
    error_code: str = ""


@dataclass(frozen=True)
class _QueuedEvent:
    queued_at_ms: int
    event: ClientEvent

    def to_dict(self) -> dict[str, object]:
        return {"queued_at_ms": self.queued_at_ms, "event": self.event.to_dict()}


@dataclass(frozen=True)
class _OutboxState:
    events: tuple[_QueuedEvent, ...] = ()
    retry_attempt: int = 0
    next_attempt_at_ms: int = 0


class ClientEventOutboxStore:
    """Atomic DPAPI-protected queue with fixed count, size and age limits."""

    def __init__(
        self,
        path: Path,
        protector: CredentialProtector | None = None,
        *,
        clock: Callable[[], float] = time.time,
        max_events: int = MAX_CLIENT_EVENT_OUTBOX_EVENTS,
        max_bytes: int = MAX_CLIENT_EVENT_OUTBOX_BYTES,
        max_age_ms: int = MAX_CLIENT_EVENT_AGE_MS,
    ) -> None:
        if max_events <= 0 or max_bytes <= 0 or max_age_ms <= 0:
            raise ValueError("client event outbox limits must be positive")
        self.path = Path(path)
        self.protector = protector or WindowsDpapiProtector()
        self._clock = clock
        self._max_events = min(max_events, MAX_CLIENT_EVENT_OUTBOX_EVENTS)
        self._max_bytes = min(max_bytes, MAX_CLIENT_EVENT_OUTBOX_BYTES)
        self._max_age_ms = min(max_age_ms, MAX_CLIENT_EVENT_AGE_MS)
        self._lock = threading.RLock()

    def _now_ms(self) -> int:
        return max(0, int(round(self._clock() * 1000)))

    @staticmethod
    def _validate_integer(value: object, *, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("client event outbox integer is invalid")
        if not minimum <= value <= maximum:
            raise ValueError("client event outbox integer is outside its range")
        return value

    def _encode(self, state: _OutboxState) -> bytes:
        raw = (
            json.dumps(
                {
                    "schema_version": 1,
                    "events": [item.to_dict() for item in state.events],
                    "retry_attempt": state.retry_attempt,
                    "next_attempt_at_ms": state.next_attempt_at_ms,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        protected = self.protector.protect(raw)
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "protection": _PROTECTION_LABEL,
                    "payload": base64.b64encode(protected).decode("ascii"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()

    def _decode(self, payload: bytes) -> _OutboxState:
        try:
            envelope = json.loads(payload.decode())
            if not isinstance(envelope, dict) or set(envelope) != {
                "schema_version",
                "protection",
                "payload",
            }:
                raise ValueError
            if envelope["schema_version"] != 1 or envelope["protection"] != (
                _PROTECTION_LABEL
            ):
                raise ValueError
            protected = base64.b64decode(envelope["payload"], validate=True)
            raw = json.loads(self.protector.unprotect(protected).decode())
            if not isinstance(raw, dict) or set(raw) != {
                "schema_version",
                "events",
                "retry_attempt",
                "next_attempt_at_ms",
            }:
                raise ValueError
            if raw["schema_version"] != 1 or not isinstance(raw["events"], list):
                raise ValueError
            if len(raw["events"]) > self._max_events:
                raise ValueError
            queued: list[_QueuedEvent] = []
            event_ids: set[str] = set()
            for item in raw["events"]:
                if not isinstance(item, dict) or set(item) != {"queued_at_ms", "event"}:
                    raise ValueError
                queued_at_ms = self._validate_integer(
                    item["queued_at_ms"], minimum=0, maximum=2**63 - 1
                )
                event = ClientEvent.from_dict(item["event"])
                if event.event_id in event_ids:
                    raise ValueError
                event_ids.add(event.event_id)
                queued.append(_QueuedEvent(queued_at_ms, event))
            return _OutboxState(
                events=tuple(queued),
                retry_attempt=self._validate_integer(
                    raw["retry_attempt"], minimum=0, maximum=_MAX_RETRY_ATTEMPT
                ),
                next_attempt_at_ms=self._validate_integer(
                    raw["next_attempt_at_ms"], minimum=0, maximum=2**63 - 1
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("client event outbox file is invalid") from exc

    def _load(self) -> _OutboxState:
        if not self.path.exists():
            return _OutboxState()
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("client event outbox path is unsafe")
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise ValueError("client event outbox is unreadable") from exc
        if not payload or len(payload) > self._max_bytes:
            raise ValueError("client event outbox file is invalid")
        return self._decode(payload)

    def _prune(self, state: _OutboxState, now_ms: int) -> _OutboxState:
        oldest = max(0, now_ms - self._max_age_ms)
        retained = tuple(item for item in state.events if item.queued_at_ms >= oldest)
        if retained == state.events:
            return state
        return _OutboxState(retained, state.retry_attempt, state.next_attempt_at_ms)

    def _write(self, state: _OutboxState) -> bool:
        if self.path.is_symlink():
            raise ValueError("client event outbox path is unsafe")
        events = state.events[-self._max_events :]
        candidate = _OutboxState(events, state.retry_attempt, state.next_attempt_at_ms)
        if not events:
            self.path.unlink(missing_ok=True)
            return True
        payload = self._encode(candidate)
        while len(payload) > self._max_bytes and candidate.events:
            candidate = _OutboxState(
                candidate.events[1:],
                candidate.retry_attempt,
                candidate.next_attempt_at_ms,
            )
            if candidate.events:
                payload = self._encode(candidate)
        if not candidate.events:
            self.path.unlink(missing_ok=True)
            return False
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
        return True

    def enqueue(self, event: ClientEvent) -> bool:
        with self._lock:
            now_ms = self._now_ms()
            state = self._prune(self._load(), now_ms)
            events = (*state.events, _QueuedEvent(now_ms, event))
            state = _OutboxState(
                events[-self._max_events :],
                state.retry_attempt,
                state.next_attempt_at_ms,
            )
            if not self._write(state):
                return False
            return any(item.event.event_id == event.event_id for item in state.events)

    def pending_count(self) -> int:
        with self._lock:
            now_ms = self._now_ms()
            state = self._load()
            pruned = self._prune(state, now_ms)
            if pruned != state:
                self._write(pruned)
            return len(pruned.events)

    def due_batch(self) -> tuple[ClientEvent, ...]:
        with self._lock:
            now_ms = self._now_ms()
            state = self._load()
            pruned = self._prune(state, now_ms)
            if pruned != state:
                self._write(pruned)
            if not pruned.events or now_ms < pruned.next_attempt_at_ms:
                return ()
            selected: list[ClientEvent] = []
            for item in pruned.events[:MAX_CLIENT_EVENT_BATCH_COUNT]:
                proposed = (*selected, item.event)
                try:
                    serialize_client_event_batch(proposed)
                except ValueError:
                    break
                selected.append(item.event)
            return tuple(selected)

    def acknowledge(self, event_ids: frozenset[str]) -> None:
        with self._lock:
            state = self._prune(self._load(), self._now_ms())
            remaining = tuple(
                item for item in state.events if item.event.event_id not in event_ids
            )
            self._write(_OutboxState(remaining, 0, 0))

    def defer_failure(self) -> None:
        with self._lock:
            state = self._prune(self._load(), self._now_ms())
            if not state.events:
                self._write(state)
                return
            attempt = min(_MAX_RETRY_ATTEMPT, state.retry_attempt + 1)
            delay = min(
                MAX_RETRY_DELAY_MS,
                INITIAL_RETRY_DELAY_MS * (2 ** (attempt - 1)),
            )
            self._write(
                _OutboxState(
                    state.events,
                    attempt,
                    self._now_ms() + delay,
                )
            )


class PreviewClientEventTracker:
    """Build valid anonymous events and keep telemetry off the product path."""

    def __init__(
        self,
        outbox: ClientEventOutboxStore,
        *,
        app_version: str,
        enabled: bool | Callable[[], bool],
        clock: Callable[[], float] = time.time,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.outbox = outbox
        self.app_version = app_version
        self._enabled = enabled
        self._clock = clock
        self._uuid_factory = uuid_factory
        self.session_id = str(uuid_factory())
        self.journey_id = str(uuid_factory())
        self._sequence = 0
        self._identity_lock = threading.RLock()

    def enabled(self) -> bool:
        try:
            return bool(self._enabled() if callable(self._enabled) else self._enabled)
        except Exception:
            return False

    def start_journey(self) -> str:
        with self._identity_lock:
            self.journey_id = str(self._uuid_factory())
            return self.journey_id

    def record(
        self,
        event_name: str,
        *,
        remote_task_id: str = "",
        properties: dict[str, object] | None = None,
    ) -> bool:
        if not self.enabled():
            return False
        try:
            with self._identity_lock:
                sequence = self._sequence
                self._sequence += 1
                event = ClientEvent.from_dict(
                    {
                        "event_id": str(self._uuid_factory()),
                        "event_name": event_name,
                        "client_at_ms": max(0, int(round(self._clock() * 1000))),
                        "sequence": sequence,
                        "session_id": self.session_id,
                        "journey_id": self.journey_id,
                        "remote_task_id": remote_task_id,
                        "app_version": self.app_version,
                        "properties": dict(properties or {}),
                    }
                )
                return self.outbox.enqueue(event)
        except Exception:
            return False

    def flush(self, transport: ClientEventTransport) -> ClientEventFlushResult:
        if not self.enabled():
            return ClientEventFlushResult(0, 0, 0, 0, error_code="disabled")
        try:
            batch = self.outbox.due_batch()
        except Exception:
            return ClientEventFlushResult(0, 0, 0, 0, error_code="outbox_unavailable")
        if not batch:
            try:
                pending = self.outbox.pending_count()
            except Exception:
                pending = 0
            return ClientEventFlushResult(0, 0, 0, pending, deferred=bool(pending))
        try:
            receipt = transport.submit_client_events(batch)
        except Exception:
            try:
                self.outbox.defer_failure()
                pending = self.outbox.pending_count()
            except Exception:
                pending = len(batch)
            return ClientEventFlushResult(
                len(batch), 0, 0, pending, deferred=True, error_code="delivery_failed"
            )
        try:
            self.outbox.acknowledge(frozenset(event.event_id for event in batch))
            pending = self.outbox.pending_count()
        except Exception:
            pending = len(batch)
            return ClientEventFlushResult(
                len(batch),
                receipt.accepted_count,
                receipt.duplicate_count,
                pending,
                deferred=True,
                error_code="ack_failed",
            )
        return ClientEventFlushResult(
            len(batch),
            receipt.accepted_count,
            receipt.duplicate_count,
            pending,
        )


class PreviewClientEventReporter:
    """Record first, then deliver from one daemon thread without blocking Qt."""

    def __init__(
        self,
        tracker: PreviewClientEventTracker,
        transport: ClientEventTransport,
    ) -> None:
        self.tracker = tracker
        self.transport = transport
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def start_journey(self) -> str:
        return self.tracker.start_journey()

    def record(
        self,
        event_name: str,
        *,
        remote_task_id: str = "",
        properties: dict[str, object] | None = None,
    ) -> bool:
        recorded = self.tracker.record(
            event_name,
            remote_task_id=remote_task_id,
            properties=properties,
        )
        if recorded:
            self.flush_async()
        return recorded

    def flush_async(self) -> bool:
        """Start one best-effort delivery loop; retry timing stays in the outbox."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            thread = threading.Thread(
                target=self._flush_worker,
                name="moeguard-client-events",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except RuntimeError:
                self._thread = None
                return False
            return True

    def _flush_worker(self) -> None:
        try:
            while True:
                result = self.tracker.flush(self.transport)
                if result.error_code or result.deferred or result.pending_count == 0:
                    break
        finally:
            with self._lock:
                self._thread = None
            try:
                due = bool(self.tracker.outbox.due_batch())
            except Exception:
                due = False
            if due:
                self.flush_async()

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Testing/controlled-shutdown helper; normal UI code never waits."""

        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, timeout))
        return not thread.is_alive()
