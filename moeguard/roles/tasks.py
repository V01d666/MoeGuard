"""Provider-neutral, recoverable task journal for the custom-role workbench."""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import wraps
from hashlib import sha256
from pathlib import Path
from typing import Any

from moeguard.roles._validation import (
    ROLE_ID_RE,
    expect_bool,
    expect_choice,
    expect_int,
    expect_object,
    expect_sha256,
    expect_string,
    fail,
    reject_unknown,
    required,
)
from moeguard.roles.errors import ContractErrorCode, RoleContractError
from moeguard.roles.spec import OFFICIAL_ACTIONS

_OPERATIONS = {
    "identity_candidates",
    "initial_package",
    "action_revision",
    "appearance_revision",
}
_STATUSES = {
    "queued",
    "running",
    "cancel_requested",
    "cancelled",
    "failed",
    "succeeded",
}
_RECOVERABLE_STATUSES = {"queued", "running", "cancel_requested", "failed"}


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize one store instance; UI and worker threads share its journal."""

    @wraps(method)
    def wrapped(self: RoleTaskStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


def _canonical_actions(raw: Any, path: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        fail(ContractErrorCode.INVALID_TYPE, "must be an array of action IDs", path)
    if len(raw) != len(set(raw)):
        fail(ContractErrorCode.INVALID_VALUE, "action IDs must be unique", path)
    unknown = set(raw).difference(OFFICIAL_ACTIONS)
    if unknown:
        fail(
            ContractErrorCode.INVALID_VALUE,
            "unknown actions: " + ", ".join(sorted(unknown)),
            path,
        )
    return tuple(action for action in OFFICIAL_ACTIONS if action in raw)


@dataclass(frozen=True)
class RoleTaskSpec:
    """A retry-stable operation description with no provider secrets or prompts."""

    operation: str
    profile_id: str
    appearance_revision: int
    actions: tuple[str, ...] = ()
    package_version: int | None = None
    input_sha256: str = ""
    identity_sha256: str = ""
    schema_version: int = 1

    @classmethod
    def from_dict(cls, raw: Any) -> RoleTaskSpec:
        value = expect_object(raw, "$.spec")
        reject_unknown(
            value,
            {
                "schema_version",
                "operation",
                "profile_id",
                "appearance_revision",
                "actions",
                "package_version",
                "input_sha256",
                "identity_sha256",
            },
            "$.spec",
        )
        schema_version = expect_int(
            required(value, "schema_version", "$.spec"),
            "$.spec.schema_version",
            minimum=1,
            maximum=999,
        )
        if schema_version != 1:
            fail(
                ContractErrorCode.UNSUPPORTED_SCHEMA,
                f"RoleTaskSpec schema {schema_version} is not supported",
                "$.spec.schema_version",
            )
        profile_id = expect_string(
            required(value, "profile_id", "$.spec"),
            "$.spec.profile_id",
            minimum=3,
            maximum=48,
        )
        if not ROLE_ID_RE.fullmatch(profile_id):
            fail(ContractErrorCode.INVALID_ID, "invalid profile ID", "$.spec.profile_id")
        package_version_raw = value.get("package_version")
        package_version = (
            expect_int(
                package_version_raw,
                "$.spec.package_version",
                minimum=1,
                maximum=2_147_483_647,
            )
            if package_version_raw is not None
            else None
        )
        input_sha256_raw = value.get("input_sha256", "")
        identity_sha256_raw = value.get("identity_sha256", "")
        return cls(
            schema_version=1,
            operation=expect_choice(
                required(value, "operation", "$.spec"),
                _OPERATIONS,
                "$.spec.operation",
            ),
            profile_id=profile_id,
            appearance_revision=expect_int(
                required(value, "appearance_revision", "$.spec"),
                "$.spec.appearance_revision",
                minimum=1,
                maximum=2_147_483_647,
            ),
            actions=_canonical_actions(value.get("actions", []), "$.spec.actions"),
            package_version=package_version,
            input_sha256=(
                expect_sha256(input_sha256_raw, "$.spec.input_sha256")
                if input_sha256_raw
                else ""
            ),
            identity_sha256=(
                expect_sha256(identity_sha256_raw, "$.spec.identity_sha256")
                if identity_sha256_raw
                else ""
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "profile_id": self.profile_id,
            "appearance_revision": self.appearance_revision,
            "actions": list(self.actions),
        }
        if self.package_version is not None:
            value["package_version"] = self.package_version
        if self.input_sha256:
            value["input_sha256"] = self.input_sha256
        if self.identity_sha256:
            value["identity_sha256"] = self.identity_sha256
        return value

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(payload).hexdigest()


@dataclass(frozen=True)
class RoleTaskRecord:
    """One local task lifecycle; provider receipts stay in private server audit."""

    local_task_id: str
    idempotency_sha256: str
    spec: RoleTaskSpec
    status: str = "queued"
    attempt: int = 0
    progress: int = 0
    retryable: bool = False
    error_code: str = ""
    result_sha256: str = ""
    processed_callbacks: tuple[str, ...] = ()
    schema_version: int = 1

    @classmethod
    def from_dict(cls, raw: Any) -> RoleTaskRecord:
        value = expect_object(raw, "$")
        reject_unknown(
            value,
            {
                "schema_version",
                "local_task_id",
                "idempotency_sha256",
                "spec",
                "status",
                "attempt",
                "progress",
                "retryable",
                "error_code",
                "result_sha256",
                "processed_callbacks",
            },
            "$",
        )
        schema_version = expect_int(
            required(value, "schema_version", "$"),
            "$.schema_version",
            minimum=1,
            maximum=999,
        )
        if schema_version != 1:
            fail(
                ContractErrorCode.UNSUPPORTED_SCHEMA,
                f"RoleTaskRecord schema {schema_version} is not supported",
                "$.schema_version",
            )
        local_task_id = expect_sha256(
            required(value, "local_task_id", "$"), "$.local_task_id"
        )
        callbacks_raw = value.get("processed_callbacks", [])
        if not isinstance(callbacks_raw, list) or len(callbacks_raw) > 128:
            fail(
                ContractErrorCode.INVALID_TYPE,
                "processed callbacks must be an array with at most 128 entries",
                "$.processed_callbacks",
            )
        callbacks = tuple(
            expect_string(item, f"$.processed_callbacks[{index}]", minimum=1, maximum=128)
            for index, item in enumerate(callbacks_raw)
        )
        if len(callbacks) != len(set(callbacks)):
            fail(
                ContractErrorCode.INVALID_VALUE,
                "processed callbacks must be unique",
                "$.processed_callbacks",
            )
        record = cls(
            schema_version=1,
            local_task_id=local_task_id,
            idempotency_sha256=expect_sha256(
                required(value, "idempotency_sha256", "$"),
                "$.idempotency_sha256",
            ),
            spec=RoleTaskSpec.from_dict(required(value, "spec", "$")),
            status=expect_choice(
                required(value, "status", "$"), _STATUSES, "$.status"
            ),
            attempt=expect_int(
                required(value, "attempt", "$"),
                "$.attempt",
                minimum=0,
                maximum=1_000_000,
            ),
            progress=expect_int(
                required(value, "progress", "$"),
                "$.progress",
                minimum=0,
                maximum=100,
            ),
            retryable=expect_bool(
                required(value, "retryable", "$"), "$.retryable"
            ),
            error_code=expect_string(
                value.get("error_code", ""), "$.error_code", maximum=80
            ),
            result_sha256=(
                expect_sha256(value.get("result_sha256"), "$.result_sha256")
                if value.get("result_sha256")
                else ""
            ),
            processed_callbacks=callbacks,
        )
        record._validate_state()
        return record

    def _validate_state(self) -> None:
        if self.status == "queued" and (self.attempt or self.progress):
            fail(ContractErrorCode.INVALID_VALUE, "queued task has progress", "$.status")
        if self.status == "succeeded":
            if not self.result_sha256 or self.progress != 100:
                fail(
                    ContractErrorCode.INVALID_VALUE,
                    "succeeded task requires a result and 100 percent progress",
                    "$.status",
                )
        elif self.result_sha256:
            fail(
                ContractErrorCode.INVALID_VALUE,
                "only succeeded tasks may contain a result digest",
                "$.result_sha256",
            )
        if self.status != "failed" and (self.retryable or self.error_code):
            fail(
                ContractErrorCode.INVALID_VALUE,
                "only failed tasks may contain retry metadata",
                "$.status",
            )

    @classmethod
    def from_json(cls, text: str) -> RoleTaskRecord:
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise RoleContractError(
                ContractErrorCode.INVALID_JSON,
                f"invalid task JSON at line {exc.lineno} column {exc.colno}",
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "local_task_id": self.local_task_id,
            "idempotency_sha256": self.idempotency_sha256,
            "spec": self.spec.to_dict(),
            "status": self.status,
            "attempt": self.attempt,
            "progress": self.progress,
            "retryable": self.retryable,
            "processed_callbacks": list(self.processed_callbacks),
        }
        if self.error_code:
            value["error_code"] = self.error_code
        if self.result_sha256:
            value["result_sha256"] = self.result_sha256
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class RoleTaskStore:
    """Atomic local state machine with deterministic idempotency task IDs."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def _path(self, local_task_id: str) -> Path:
        local_task_id = expect_sha256(local_task_id, "local_task_id")
        return self.root / local_task_id[:2] / f"{local_task_id}.json"

    @_locked
    def _save(self, record: RoleTaskRecord) -> RoleTaskRecord:
        canonical = RoleTaskRecord.from_json(record.to_json())
        destination = self._path(canonical.local_task_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(canonical.to_json(), encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return canonical

    @_locked
    def _create(self, record: RoleTaskRecord) -> tuple[RoleTaskRecord, bool]:
        """Create the deterministic task path once without replacing a rival."""
        canonical = RoleTaskRecord.from_json(record.to_json())
        destination = self._path(canonical.local_task_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                destination,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return self.load(canonical.local_task_id), False
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical.to_json())
            stream.flush()
            os.fsync(stream.fileno())
        return canonical, True

    @_locked
    def create_or_load(
        self, spec: RoleTaskSpec, *, idempotency_key: str
    ) -> tuple[RoleTaskRecord, bool]:
        canonical_spec = RoleTaskSpec.from_dict(spec.to_dict())
        key = idempotency_key.strip()
        if not 8 <= len(key) <= 256:
            fail(
                ContractErrorCode.INVALID_VALUE,
                "idempotency key must contain 8-256 characters",
                "idempotency_key",
            )
        idempotency_digest = sha256(key.encode("utf-8")).hexdigest()
        local_task_id = idempotency_digest
        path = self._path(local_task_id)
        if path.exists():
            existing = self.load(local_task_id)
            if existing.spec.fingerprint() != canonical_spec.fingerprint():
                fail(
                    ContractErrorCode.ALREADY_EXISTS,
                    "idempotency key is already bound to a different operation",
                    "idempotency_key",
                )
            return existing, False
        created, was_created = self._create(
            RoleTaskRecord(
                local_task_id=local_task_id,
                idempotency_sha256=idempotency_digest,
                spec=canonical_spec,
            )
        )
        if created.spec.fingerprint() != canonical_spec.fingerprint():
            fail(
                ContractErrorCode.ALREADY_EXISTS,
                "idempotency key is already bound to a different operation",
                "idempotency_key",
            )
        return created, was_created

    @_locked
    def load(self, local_task_id: str) -> RoleTaskRecord:
        path = self._path(local_task_id)
        try:
            record = RoleTaskRecord.from_json(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RoleContractError(
                ContractErrorCode.NOT_FOUND, "task does not exist", path="task"
            ) from exc
        if record.local_task_id != local_task_id:
            fail(ContractErrorCode.INVALID_VALUE, "task ID/path mismatch", "$.local_task_id")
        return record

    @_locked
    def start_or_resume(self, local_task_id: str) -> RoleTaskRecord:
        record = self.load(local_task_id)
        if record.status == "running":
            return record
        if record.status == "queued":
            return self._save(replace(record, status="running", attempt=1))
        if record.status == "failed" and record.retryable:
            return self._save(
                replace(
                    record,
                    status="running",
                    attempt=record.attempt + 1,
                    retryable=False,
                    error_code="",
                )
            )
        fail(
            ContractErrorCode.INVALID_VALUE,
            f"task in {record.status} state cannot be resumed",
            "$.status",
        )

    @_locked
    def update_progress(self, local_task_id: str, progress: int) -> RoleTaskRecord:
        record = self.load(local_task_id)
        progress = expect_int(progress, "progress", minimum=0, maximum=99)
        if record.status != "running" or progress < record.progress:
            fail(
                ContractErrorCode.INVALID_VALUE,
                "progress must be monotonic for a running task",
                "progress",
            )
        return self._save(replace(record, progress=progress))

    @_locked
    def request_cancel(self, local_task_id: str) -> RoleTaskRecord:
        record = self.load(local_task_id)
        if record.status == "queued":
            return self._save(replace(record, status="cancelled"))
        if record.status == "running":
            return self._save(replace(record, status="cancel_requested"))
        return record

    @_locked
    def acknowledge_cancel(self, local_task_id: str) -> RoleTaskRecord:
        record = self.load(local_task_id)
        if record.status != "cancel_requested":
            fail(
                ContractErrorCode.INVALID_VALUE,
                "only a cancel-requested task can acknowledge cancellation",
                "$.status",
            )
        return self._save(replace(record, status="cancelled"))

    @_locked
    def fail_task(
        self, local_task_id: str, *, error_code: str, retryable: bool
    ) -> RoleTaskRecord:
        record = self.load(local_task_id)
        if record.status != "running":
            fail(
                ContractErrorCode.INVALID_VALUE,
                "only a running task can fail",
                "$.status",
            )
        error = expect_string(error_code, "error_code", minimum=1, maximum=80)
        return self._save(
            replace(
                record,
                status="failed",
                retryable=bool(retryable),
                error_code=error,
            )
        )

    @_locked
    def complete(
        self,
        local_task_id: str,
        *,
        callback_id: str,
        result_sha256: str,
    ) -> tuple[RoleTaskRecord, bool]:
        record = self.load(local_task_id)
        callback = expect_string(callback_id, "callback_id", minimum=1, maximum=128)
        result = expect_sha256(result_sha256, "result_sha256")
        if record.status == "succeeded":
            if record.result_sha256 != result:
                fail(
                    ContractErrorCode.HASH_MISMATCH,
                    "duplicate completion disagrees with the accepted result",
                    "result_sha256",
                )
            if callback in record.processed_callbacks:
                return record, False
            updated = self._save(
                replace(
                    record,
                    processed_callbacks=record.processed_callbacks + (callback,),
                )
            )
            return updated, False
        if record.status != "running":
            fail(
                ContractErrorCode.INVALID_VALUE,
                f"task in {record.status} state cannot complete",
                "$.status",
            )
        completed = self._save(
            replace(
                record,
                status="succeeded",
                progress=100,
                result_sha256=result,
                processed_callbacks=record.processed_callbacks + (callback,),
            )
        )
        return completed, True

    @_locked
    def list_recoverable(self, profile_id: str | None = None) -> tuple[RoleTaskRecord, ...]:
        if profile_id is not None and not ROLE_ID_RE.fullmatch(profile_id):
            fail(ContractErrorCode.INVALID_ID, "invalid profile ID", "profile_id")
        if not self.root.is_dir():
            return ()
        records = []
        for path in sorted(self.root.glob("[0-9a-f][0-9a-f]/*.json")):
            record = self.load(path.stem)
            if record.status not in _RECOVERABLE_STATUSES:
                continue
            if record.status == "failed" and not record.retryable:
                continue
            if profile_id is not None and record.spec.profile_id != profile_id:
                continue
            records.append(record)
        return tuple(records)


class RoleTaskArtifactStore:
    """Publish task output atomically before an idempotent completion callback."""

    def __init__(self, root: Path, task_store: RoleTaskStore) -> None:
        self.root = Path(root)
        self.task_store = task_store

    def _artifact_path(self, local_task_id: str) -> Path:
        local_task_id = expect_sha256(local_task_id, "local_task_id")
        return self.root / local_task_id[:2] / local_task_id

    def has_published(self, local_task_id: str) -> bool:
        """Return whether an immutable result tree was already published.

        This deliberately validates the complete tree instead of treating the
        directory name as proof.  A workbench may therefore recover the narrow
        crash window between atomic publication and completion journaling
        without issuing the provider request again.
        """
        destination = self._artifact_path(local_task_id)
        if not destination.is_dir():
            return False
        self._tree_digest(destination)
        return True

    @staticmethod
    def _tree_digest(root: Path) -> str:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            fail(
                ContractErrorCode.INVALID_PATH,
                "task artifact must be a regular directory",
                "artifact",
            )
        digest = sha256()
        entries = tuple(root.rglob("*"))
        if any(path.is_symlink() for path in entries):
            fail(
                ContractErrorCode.INVALID_PATH,
                "task artifacts cannot contain symbolic links",
                "artifact",
            )
        files = sorted(path for path in entries if path.is_file())
        if not files:
            fail(
                ContractErrorCode.INVALID_VALUE,
                "task artifact must contain at least one file",
                "artifact",
            )
        for path in files:
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            payload = path.read_bytes()
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
        return digest.hexdigest()

    def build(
        self,
        local_task_id: str,
        builder: Callable[[Path], None],
        *,
        retryable_on_failure: bool = True,
    ) -> Path:
        """Build once; an existing complete directory is reused after a crash."""
        record = self.task_store.load(local_task_id)
        if record.status == "cancel_requested":
            self.task_store.acknowledge_cancel(local_task_id)
            fail(ContractErrorCode.INVALID_VALUE, "task was cancelled", "$.status")
        if record.status == "cancelled":
            fail(ContractErrorCode.INVALID_VALUE, "task was cancelled", "$.status")
        if record.status == "succeeded":
            return self.result(local_task_id)
        running = self.task_store.start_or_resume(local_task_id)
        destination = self._artifact_path(local_task_id)
        if destination.is_dir():
            self._tree_digest(destination)
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
        try:
            builder(staging)
            self._tree_digest(staging)
            os.replace(staging, destination)
        except Exception:
            if staging.is_dir():
                shutil.rmtree(staging)
            current = self.task_store.load(running.local_task_id)
            if current.status == "running":
                self.task_store.fail_task(
                    running.local_task_id,
                    error_code="local_artifact_build_failed",
                    retryable=retryable_on_failure,
                )
            raise
        return destination

    def accept_completion(
        self, local_task_id: str, *, callback_id: str
    ) -> tuple[RoleTaskRecord, bool]:
        """Accept one result digest; duplicate callbacks have no second effect."""
        destination = self._artifact_path(local_task_id)
        digest = self._tree_digest(destination)
        return self.task_store.complete(
            local_task_id,
            callback_id=callback_id,
            result_sha256=digest,
        )

    def result(self, local_task_id: str) -> Path:
        record = self.task_store.load(local_task_id)
        if record.status != "succeeded":
            fail(
                ContractErrorCode.INVALID_VALUE,
                "task has no accepted result",
                "$.status",
            )
        destination = self._artifact_path(local_task_id)
        if self._tree_digest(destination) != record.result_sha256:
            fail(
                ContractErrorCode.HASH_MISMATCH,
                "task artifact changed after completion",
                "artifact",
            )
        return destination
