"""Provider-neutral client/service seam for recoverable custom-role tasks.

The production transport will eventually speak HTTPS.  This module freezes
the client semantics first: one local task binds to one opaque service task,
polling never resubmits, and a completed result is published locally before
the local journal accepts completion.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, UnidentifiedImageError

from moeguard.roles import (
    CharacterProfile,
    ContractErrorCode,
    RoleContractError,
    RolePackage,
    RoleTaskArtifactStore,
    RoleTaskRecord,
    RoleTaskSpec,
    RoleTaskStore,
    verify_role_directory,
)

_SERVICE_STATUSES = {
    "queued",
    "running",
    "cancel_requested",
    "cancelled",
    "failed",
    "succeeded",
}
_REMOTE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ASSET_PURPOSES = {"input_image", "identity_image", "source_package"}
_ASSET_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/vnd.moeguard.role+zip",
}
_IMAGE_FORMAT_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
_MAX_ASSET_BYTES = 512 * 1024 * 1024
_MAX_IMAGE_EDGE = 4096
_MAX_IMAGE_PIXELS = 4096 * 4096


def validate_role_service_image(path: Path, media_type: str) -> None:
    """Validate image identity and decoded dimensions before Qt ever sees it."""

    try:
        with Image.open(path) as image:
            detected = _IMAGE_FORMAT_MEDIA_TYPES.get(str(image.format).upper())
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > _MAX_IMAGE_EDGE
                or height > _MAX_IMAGE_EDGE
                or width * height > _MAX_IMAGE_PIXELS
            ):
                raise RoleContractError(
                    ContractErrorCode.RESOURCE_LIMIT,
                    "service image dimensions are too large",
                    path="service.asset.dimensions",
                )
            image.verify()
    except RoleContractError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise RoleContractError(
            ContractErrorCode.INVALID_IMAGE,
            "service input is not a readable image",
            path="service.asset",
        ) from exc
    if detected != media_type:
        raise RoleContractError(
            ContractErrorCode.INVALID_IMAGE,
            "service image media type does not match its content",
            path="service.asset.media_type",
        )


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_value(value: str, *, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _safe_result_relative(value: Any, *, path: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RoleContractError(
            ContractErrorCode.INVALID_PATH,
            "service result path must be a non-empty POSIX relative path",
            path=path,
        )
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RoleContractError(
            ContractErrorCode.INVALID_PATH,
            "service result path must stay inside the result root",
            path=path,
        )
    return relative


def sanitize_role_service_result(source: Path, destination: Path) -> None:
    """Publish only user-facing candidates or a verified native role package."""
    source = Path(source).resolve()
    destination = Path(destination)
    if destination.exists():
        raise ValueError("service result destination must not already exist")
    try:
        raw = json.loads(
            (source / ".workbench-result.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RoleContractError(
            ContractErrorCode.INVALID_JSON,
            "service result metadata is missing or invalid",
            path="service.result",
        ) from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise RoleContractError(
            ContractErrorCode.UNSUPPORTED_SCHEMA,
            "service result metadata version is unsupported",
            path="service.result.schema_version",
        )
    result_kind = raw.get("result_kind")
    destination.mkdir(parents=True)
    try:
        if result_kind == "candidates":
            candidates = raw.get("candidates")
            if not isinstance(candidates, list) or not 1 <= len(candidates) <= 4:
                raise RoleContractError(
                    ContractErrorCode.INVALID_VALUE,
                    "service result must contain 1-4 candidates",
                    path="service.result.candidates",
                )
            target_root = destination / "candidates"
            target_root.mkdir()
            published: list[str] = []
            for index, value in enumerate(candidates, start=1):
                relative = _safe_result_relative(
                    value, path=f"service.result.candidates[{index - 1}]"
                )
                candidate = (source / relative).resolve()
                if not candidate.is_relative_to(source) or not candidate.is_file():
                    raise RoleContractError(
                        ContractErrorCode.NOT_FOUND,
                        "service candidate is missing",
                        path=f"service.result.candidates[{index - 1}]",
                    )
                validate_role_service_image(candidate, "image/png")
                target = target_root / f"candidate-{index:02d}.png"
                shutil.copy2(candidate, target)
                published.append(target.relative_to(destination).as_posix())
            metadata = {
                "schema_version": 1,
                "result_kind": "candidates",
                "spent_cny": 0.0,
                "candidates": published,
            }
        elif result_kind == "package":
            relative = _safe_result_relative(
                raw.get("package_root"), path="service.result.package_root"
            )
            package_root = (source / relative).resolve()
            if not package_root.is_relative_to(source):
                raise RoleContractError(
                    ContractErrorCode.INVALID_PATH,
                    "service package root escapes the result",
                    path="service.result.package_root",
                )
            package = RolePackage.from_json(
                (package_root / "role.json").read_text(encoding="utf-8")
            )
            target_root = destination / "package"
            target_root.mkdir()
            shutil.copy2(package_root / "role.json", target_root / "role.json")
            for _action, definition in package.actions:
                for frame in definition.frames:
                    source_frame = package_root / Path(frame.path)
                    resolved_frame = source_frame.resolve()
                    if (
                        not resolved_frame.is_relative_to(package_root)
                        or not source_frame.is_file()
                        or source_frame.is_symlink()
                        or sha256(source_frame.read_bytes()).hexdigest() != frame.sha256
                    ):
                        raise RoleContractError(
                            ContractErrorCode.HASH_MISMATCH,
                            "service package frame is missing or corrupt",
                            path=frame.path,
                        )
                    target_frame = target_root / Path(frame.path)
                    target_frame.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_frame, target_frame)
            verify_role_directory(target_root)
            previews = target_root / "previews"
            previews.mkdir()
            for action, definition in package.actions:
                images = []
                for frame in definition.frames:
                    with Image.open(target_root / Path(frame.path)) as image:
                        images.append(image.convert("RGBA"))
                images[0].save(
                    previews / f"{action}.gif",
                    save_all=True,
                    append_images=images[1:],
                    duration=167,
                    loop=0,
                    disposal=2,
                )
            metadata = {
                "schema_version": 1,
                "result_kind": "package",
                "spent_cny": 0.0,
                "package_root": "package",
            }
        else:
            raise RoleContractError(
                ContractErrorCode.INVALID_VALUE,
                "service result kind is unsupported",
                path="service.result.result_kind",
            )
        (destination / ".workbench-result.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


@dataclass(frozen=True)
class ServiceAssetRef:
    """Path-free reference to one client-uploaded service input."""

    sha256: str
    media_type: str
    purpose: str
    size_bytes: int
    expires_at: int

    def __post_init__(self) -> None:
        _sha256_value(self.sha256, label="service asset hash")
        if self.media_type not in _ASSET_MEDIA_TYPES:
            raise ValueError("unsupported service asset media type")
        if self.purpose not in _ASSET_PURPOSES:
            raise ValueError("unsupported service asset purpose")
        if isinstance(self.size_bytes, bool) or not 1 <= self.size_bytes <= _MAX_ASSET_BYTES:
            raise ValueError("service asset size is outside the supported range")
        if isinstance(self.expires_at, bool) or self.expires_at <= 0:
            raise ValueError("service asset expiry must be a positive Unix timestamp")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "media_type": self.media_type,
            "purpose": self.purpose,
            "size_bytes": self.size_bytes,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ServiceAssetRef:
        if not isinstance(raw, dict) or set(raw) != {
            "sha256",
            "media_type",
            "purpose",
            "size_bytes",
            "expires_at",
        }:
            raise ValueError("service asset reference schema is invalid")
        return cls(
            sha256=str(raw["sha256"]),
            media_type=str(raw["media_type"]),
            purpose=str(raw["purpose"]),
            size_bytes=int(raw["size_bytes"]),
            expires_at=int(raw["expires_at"]),
        )


@dataclass(frozen=True)
class ClientRuntimeInfo:
    """Small allowlisted Preview context; never a stable device fingerprint."""

    app_version: str = "unknown"
    windows_major: str = "unknown"
    architecture: str = "unknown"
    display_scale_percent: int = 100
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("client runtime schema is unsupported")
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}|unknown", self.app_version):
            raise ValueError("client app version is invalid")
        if not re.fullmatch(r"[0-9]{1,3}|unknown", self.windows_major):
            raise ValueError("client Windows major version is invalid")
        if self.architecture not in {"x86_64", "arm64", "unknown"}:
            raise ValueError("client architecture is invalid")
        if (
            isinstance(self.display_scale_percent, bool)
            or not 50 <= self.display_scale_percent <= 400
        ):
            raise ValueError("client display scale is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "windows_major": self.windows_major,
            "architecture": self.architecture,
            "display_scale_percent": self.display_scale_percent,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ClientRuntimeInfo:
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "app_version",
            "windows_major",
            "architecture",
            "display_scale_percent",
        }:
            raise ValueError("client runtime schema is invalid")
        return cls(
            schema_version=raw["schema_version"],
            app_version=str(raw["app_version"]),
            windows_major=str(raw["windows_major"]),
            architecture=str(raw["architecture"]),
            display_scale_percent=raw["display_scale_percent"],
        )


@dataclass(frozen=True)
class RoleServiceRequest:
    """Safe request envelope sent to the MoeGuard generation service.

    It deliberately contains no filesystem path, provider prompt, credential,
    signed URL, provider task ID, seed, or raw provider response.
    """

    spec: RoleTaskSpec
    profile: CharacterProfile
    candidate_count: int
    assets: tuple[ServiceAssetRef, ...] = ()
    revision_instruction: str = ""
    accepted_direction_sources: tuple[str, ...] = ()
    client_runtime: ClientRuntimeInfo = ClientRuntimeInfo()
    schema_version: int = 1

    def __post_init__(self) -> None:
        canonical = type(self).from_dict(self.to_dict(), _validated=True)
        if canonical != self:
            raise ValueError("service request is not canonical")

    @classmethod
    def from_dict(
        cls, raw: Any, *, _validated: bool = False
    ) -> RoleServiceRequest:
        if not isinstance(raw, dict) or frozenset(raw) not in {
            frozenset(
                {
                    "schema_version",
                    "spec",
                    "profile",
                    "candidate_count",
                    "assets",
                    "revision_instruction",
                    "accepted_direction_sources",
                }
            ),
            frozenset(
                {
                    "schema_version",
                    "spec",
                    "profile",
                    "candidate_count",
                    "assets",
                    "revision_instruction",
                    "accepted_direction_sources",
                    "client_runtime",
                }
            ),
        }:
            raise ValueError("role service request schema is invalid")
        if raw.get("schema_version") != 1:
            raise ValueError("role service request version is unsupported")
        spec = RoleTaskSpec.from_dict(raw.get("spec"))
        profile = CharacterProfile.from_dict(raw.get("profile"))
        if spec.profile_id != profile.profile_id:
            raise ValueError("service request profile ID disagrees with task spec")
        if spec.appearance_revision != profile.appearance_revision:
            raise ValueError("service request appearance revision disagrees with task spec")
        candidate_count = raw.get("candidate_count")
        if isinstance(candidate_count, bool) or not isinstance(candidate_count, int):
            raise ValueError("service candidate count must be an integer")
        if not 1 <= candidate_count <= 4:
            raise ValueError("service candidate count must be 1-4")
        instruction = str(raw.get("revision_instruction", ""))
        if len(instruction) > 480:
            raise ValueError("service revision instruction exceeds 480 characters")
        assets_raw = raw.get("assets")
        if not isinstance(assets_raw, list):
            raise ValueError("service assets must be an array")
        assets = tuple(ServiceAssetRef.from_dict(item) for item in assets_raw)
        purpose_pairs = {(item.purpose, item.sha256) for item in assets}
        if len(purpose_pairs) != len(assets):
            raise ValueError("service asset references must be unique")
        directions_raw = raw.get("accepted_direction_sources")
        if not isinstance(directions_raw, list):
            raise ValueError("accepted direction sources must be an array")
        directions = tuple(str(item) for item in directions_raw)
        if len(directions) != len(set(directions)) or set(directions).difference(
            {"peek_left", "peek_right"}
        ):
            raise ValueError("accepted direction sources are invalid")
        runtime = ClientRuntimeInfo.from_dict(
            raw.get("client_runtime", ClientRuntimeInfo().to_dict())
        )
        input_assets = [item for item in assets if item.purpose == "input_image"]
        if profile.input.kind == "image":
            if len(input_assets) != 1:
                raise ValueError("image profile requires exactly one input image asset")
            input_asset = input_assets[0]
            if (
                input_asset.sha256 != profile.input.sha256
                or input_asset.media_type != profile.input.media_type
            ):
                raise ValueError("input image asset disagrees with appearance profile")
        elif input_assets:
            raise ValueError("text profile must not include an input image asset")
        identity_assets = [item for item in assets if item.purpose == "identity_image"]
        source_assets = [item for item in assets if item.purpose == "source_package"]
        if spec.operation == "identity_candidates":
            if identity_assets:
                raise ValueError("candidate task must not include an identity image")
        else:
            if len(identity_assets) != 1:
                raise ValueError("package task requires exactly one identity image")
            if identity_assets[0].sha256 != spec.identity_sha256:
                raise ValueError("identity image asset disagrees with task spec")
        if spec.operation == "action_revision":
            if len(source_assets) != 1:
                raise ValueError("action revision requires exactly one source package")
        elif source_assets:
            raise ValueError("only action revision may include a source package")
        request = object.__new__(cls)
        object.__setattr__(request, "spec", spec)
        object.__setattr__(request, "profile", profile)
        object.__setattr__(request, "candidate_count", candidate_count)
        object.__setattr__(request, "assets", assets)
        object.__setattr__(request, "revision_instruction", instruction)
        object.__setattr__(request, "accepted_direction_sources", directions)
        object.__setattr__(request, "client_runtime", runtime)
        object.__setattr__(request, "schema_version", 1)
        return request

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "spec": self.spec.to_dict(),
            "profile": self.profile.to_dict(),
            "candidate_count": self.candidate_count,
            "assets": [item.to_dict() for item in self.assets],
            "revision_instruction": self.revision_instruction,
            "accepted_direction_sources": list(self.accepted_direction_sources),
            "client_runtime": self.client_runtime.to_dict(),
        }

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(payload).hexdigest()


@dataclass(frozen=True)
class ServiceTaskSnapshot:
    """Minimal task state returned by a service transport."""

    remote_task_id: str
    spec_sha256: str
    status: str
    progress: int = 0
    retryable: bool = False
    error_code: str = ""
    result_sha256: str = ""

    def __post_init__(self) -> None:
        if not _REMOTE_TASK_ID_RE.fullmatch(self.remote_task_id):
            raise ValueError("remote task ID contains unsafe characters")
        if len(self.spec_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.spec_sha256
        ):
            raise ValueError("service task spec hash must be lowercase SHA-256")
        if self.status not in _SERVICE_STATUSES:
            raise ValueError("unknown service task status")
        if isinstance(self.progress, bool) or not 0 <= self.progress <= 100:
            raise ValueError("service task progress must be 0-100")
        if self.status == "succeeded":
            if self.progress != 100 or len(self.result_sha256) != 64:
                raise ValueError("succeeded service task requires a result hash")
        elif self.result_sha256:
            raise ValueError("unfinished service task cannot expose a result hash")
        if self.status == "failed":
            if not self.error_code:
                raise ValueError("failed service task requires an error code")
        elif self.retryable or self.error_code:
            raise ValueError("only failed service tasks may carry retry metadata")


class RoleServiceTransport(Protocol):
    """Transport boundary; implementations may use HTTP, IPC, or a fake store."""

    def upload_asset(
        self,
        source: Path,
        *,
        purpose: str,
        media_type: str,
        expires_at: int,
    ) -> ServiceAssetRef: ...

    def submit(
        self, request: RoleServiceRequest, *, idempotency_sha256: str
    ) -> ServiceTaskSnapshot: ...

    def query(self, remote_task_id: str) -> ServiceTaskSnapshot: ...

    def cancel(self, remote_task_id: str) -> ServiceTaskSnapshot: ...

    def download_result(self, remote_task_id: str, destination: Path) -> str: ...


@dataclass(frozen=True)
class RoleServiceBinding:
    """Private local mapping; never include this in a role package."""

    local_task_id: str
    remote_task_id: str
    spec_sha256: str
    request_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "local_task_id": self.local_task_id,
            "remote_task_id": self.remote_task_id,
            "spec_sha256": self.spec_sha256,
            "request_sha256": self.request_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> RoleServiceBinding:
        if not isinstance(raw, dict) or raw.get("schema_version") != 2:
            raise ValueError("service binding schema is invalid")
        binding = cls(
            local_task_id=str(raw.get("local_task_id", "")),
            remote_task_id=str(raw.get("remote_task_id", "")),
            spec_sha256=str(raw.get("spec_sha256", "")),
            request_sha256=str(raw.get("request_sha256", "")),
        )
        ServiceTaskSnapshot(
            remote_task_id=binding.remote_task_id,
            spec_sha256=binding.spec_sha256,
            status="queued",
        )
        if len(binding.local_task_id) != 64 or any(
            character not in "0123456789abcdef"
            for character in binding.local_task_id
        ):
            raise ValueError("local task ID must be lowercase SHA-256")
        _sha256_value(binding.request_sha256, label="service request hash")
        return binding


class RoleServiceBindingStore:
    """Atomic private store for local-to-remote task identity."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def _path(self, local_task_id: str) -> Path:
        if len(local_task_id) != 64 or any(
            character not in "0123456789abcdef" for character in local_task_id
        ):
            raise ValueError("invalid local task ID")
        return self.root / local_task_id[:2] / f"{local_task_id}.json"

    def load(self, local_task_id: str) -> RoleServiceBinding | None:
        with self._lock:
            path = self._path(local_task_id)
            if not path.is_file():
                return None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("service task binding is missing or corrupt") from exc
            binding = RoleServiceBinding.from_dict(raw)
            if binding.local_task_id != local_task_id:
                raise ValueError("service task binding ID/path mismatch")
            return binding

    def save_once(self, binding: RoleServiceBinding) -> RoleServiceBinding:
        canonical = RoleServiceBinding.from_dict(binding.to_dict())
        with self._lock:
            existing = self.load(canonical.local_task_id)
            if existing is not None:
                if existing != canonical:
                    raise ValueError("local task is already bound to another service task")
                return existing
            _atomic_write(
                self._path(canonical.local_task_id),
                json.dumps(
                    canonical.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n",
            )
            return canonical


class RoleServiceRequestStore:
    """Atomic private cache of the exact path-free request used for retries."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def _path(self, local_task_id: str) -> Path:
        _sha256_value(local_task_id, label="local task ID")
        return self.root / local_task_id[:2] / f"{local_task_id}.json"

    def load(self, local_task_id: str) -> RoleServiceRequest | None:
        with self._lock:
            path = self._path(local_task_id)
            if not path.is_file():
                return None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("service request cache is missing or corrupt") from exc
            return RoleServiceRequest.from_dict(raw)

    def save_once(
        self, local_task_id: str, request: RoleServiceRequest
    ) -> RoleServiceRequest:
        canonical = RoleServiceRequest.from_dict(request.to_dict())
        with self._lock:
            existing = self.load(local_task_id)
            if existing is not None:
                if existing != canonical:
                    raise ValueError("local task already has another service request")
                return existing
            _atomic_write(
                self._path(local_task_id),
                json.dumps(
                    canonical.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n",
            )
            return canonical


class RoleServiceClient:
    """Synchronize service state into one local task without resubmission."""

    def __init__(
        self,
        transport: RoleServiceTransport,
        task_store: RoleTaskStore,
        artifact_store: RoleTaskArtifactStore,
        binding_store: RoleServiceBindingStore,
        request_store: RoleServiceRequestStore | None = None,
    ) -> None:
        self.transport = transport
        self.task_store = task_store
        self.artifact_store = artifact_store
        self.binding_store = binding_store
        self.request_store = request_store or RoleServiceRequestStore(
            binding_store.root.parent / "service-requests"
        )

    @staticmethod
    def _validate_snapshot(
        snapshot: ServiceTaskSnapshot,
        *,
        remote_task_id: str | None,
        spec_sha256: str,
    ) -> None:
        if remote_task_id is not None and snapshot.remote_task_id != remote_task_id:
            raise ValueError("service returned a different remote task ID")
        if snapshot.spec_sha256 != spec_sha256:
            raise ValueError("service task spec hash does not match the local task")

    def upload_asset(
        self,
        source: Path,
        *,
        purpose: str,
        media_type: str,
        expires_at: int,
    ) -> ServiceAssetRef:
        return self.transport.upload_asset(
            source,
            purpose=purpose,
            media_type=media_type,
            expires_at=expires_at,
        )

    def ensure_submitted(
        self, local_task_id: str, request: RoleServiceRequest | None = None
    ) -> ServiceTaskSnapshot:
        record = self.task_store.load(local_task_id)
        if request is not None:
            request = self.request_store.save_once(local_task_id, request)
        else:
            request = self.request_store.load(local_task_id)
            if request is None:
                raise ValueError("local task has no prepared service request")
        if request.spec != record.spec:
            raise ValueError("service request task spec disagrees with local journal")
        request_sha256 = request.fingerprint()
        binding = self.binding_store.load(local_task_id)
        if binding is not None:
            if binding.request_sha256 != request_sha256:
                raise ValueError("local task is bound to another service request")
            self._validate_snapshot(
                snapshot := self.transport.query(binding.remote_task_id),
                remote_task_id=binding.remote_task_id,
                spec_sha256=record.spec.fingerprint(),
            )
            return snapshot
        if record.status == "queued":
            record = self.task_store.start_or_resume(local_task_id)
        if record.status != "running":
            raise ValueError(f"local task in {record.status} state cannot be submitted")
        snapshot = self.transport.submit(
            request, idempotency_sha256=record.idempotency_sha256
        )
        self._validate_snapshot(
            snapshot,
            remote_task_id=None,
            spec_sha256=record.spec.fingerprint(),
        )
        self.binding_store.save_once(
            RoleServiceBinding(
                local_task_id=local_task_id,
                remote_task_id=snapshot.remote_task_id,
                spec_sha256=record.spec.fingerprint(),
                request_sha256=request_sha256,
            )
        )
        return snapshot

    def poll(
        self, local_task_id: str, request: RoleServiceRequest | None = None
    ) -> RoleTaskRecord:
        snapshot = self.ensure_submitted(local_task_id, request)
        record = self.task_store.load(local_task_id)
        if snapshot.status in {"queued", "running", "cancel_requested"}:
            if record.status == "running":
                self.task_store.update_progress(
                    local_task_id,
                    max(record.progress, min(snapshot.progress, 99)),
                )
            return self.task_store.load(local_task_id)
        if snapshot.status == "cancelled":
            if record.status == "running":
                record = self.task_store.request_cancel(local_task_id)
            if record.status == "cancel_requested":
                return self.task_store.acknowledge_cancel(local_task_id)
            return record
        if snapshot.status == "failed":
            if record.status == "running":
                return self.task_store.fail_task(
                    local_task_id,
                    error_code=snapshot.error_code,
                    retryable=snapshot.retryable,
                )
            return record
        if snapshot.status != "succeeded":
            raise ValueError("service returned an unsupported terminal state")

        def download(destination: Path) -> None:
            received = self.transport.download_result(
                snapshot.remote_task_id, destination
            )
            if received != snapshot.result_sha256:
                raise RoleContractError(
                    ContractErrorCode.HASH_MISMATCH,
                    "downloaded service result disagrees with task snapshot",
                    path="service.result_sha256",
                )

        self.artifact_store.build(local_task_id, download)
        completed, _applied = self.artifact_store.accept_completion(
            local_task_id,
            callback_id=(
                "service-result-"
                + sha256(snapshot.remote_task_id.encode("utf-8")).hexdigest()
            ),
        )
        if completed.result_sha256 != snapshot.result_sha256:
            raise RoleContractError(
                ContractErrorCode.HASH_MISMATCH,
                "local result tree disagrees with service result",
                path="service.result_sha256",
            )
        return completed

    def cancel(
        self, local_task_id: str, request: RoleServiceRequest | None = None
    ) -> RoleTaskRecord:
        snapshot = self.ensure_submitted(local_task_id, request)
        local = self.task_store.request_cancel(local_task_id)
        remote = self.transport.cancel(snapshot.remote_task_id)
        self._validate_snapshot(
            remote,
            remote_task_id=snapshot.remote_task_id,
            spec_sha256=local.spec.fingerprint(),
        )
        if remote.status == "cancelled" and local.status == "cancel_requested":
            return self.task_store.acknowledge_cancel(local_task_id)
        return self.task_store.load(local_task_id)
