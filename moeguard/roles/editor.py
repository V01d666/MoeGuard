"""Immutable RolePackage revisions for adding or replacing individual actions."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from PIL import Image

from moeguard.roles._validation import FIELD_ID_RE
from moeguard.roles.archive import verify_role_directory
from moeguard.roles.errors import ContractErrorCode, RoleContractError
from moeguard.roles.package import RolePackage
from moeguard.roles.spec import LOOP_ACTIONS, OFFICIAL_ACTIONS

_ACTION_SOURCES = {"generated", "mirrored", "conjugate", "local-derived"}
_PROCESSING_LEVELS = {"raw", "soft", "strong"}
_REVIEW_STATUSES = {"pending", "accepted", "rejected"}
_DIRECTIONAL_ACTIONS = {"peek_left": "left", "peek_right": "right"}


def _invalid(message: str, path: str) -> None:
    raise RoleContractError(ContractErrorCode.INVALID_VALUE, message, path=path)


@dataclass(frozen=True)
class ActionRevision:
    """One generated or locally derived official action ready for a new package."""

    action: str
    frames: tuple[Path, ...]
    status: str = "pending"
    source: str = "generated"
    processing: str = "raw"
    source_action: str | None = None
    direction_status: str | None = None
    metrics: tuple[tuple[str, bool | int | float], ...] = ()
    warnings: tuple[str, ...] = ()

    def quality_dict(self) -> dict[str, object]:
        path = f"revisions.{self.action}"
        if self.action not in OFFICIAL_ACTIONS:
            _invalid("only official v0.2 actions can be revised", path)
        if self.status not in _REVIEW_STATUSES:
            _invalid("invalid action review status", f"{path}.status")
        if self.source not in _ACTION_SOURCES:
            _invalid("invalid action source", f"{path}.source")
        if self.processing not in _PROCESSING_LEVELS:
            _invalid("invalid processing level", f"{path}.processing")
        if not self.frames:
            _invalid("action revision requires frames", f"{path}.frames")
        value: dict[str, object] = {
            "status": self.status,
            "source": self.source,
            "processing": self.processing,
            "metrics": dict(self.metrics),
            "warnings": list(self.warnings),
        }
        if self.source_action is not None:
            value["source_action"] = self.source_action
        if self.action in _DIRECTIONAL_ACTIONS:
            if self.direction_status is None:
                _invalid(
                    "directional action revision requires manual review state",
                    f"{path}.direction_status",
                )
            value["direction"] = {
                "expected": _DIRECTIONAL_ACTIONS[self.action],
                "status": self.direction_status,
                "method": "manual-screen-review",
            }
        elif self.direction_status is not None:
            _invalid(
                "direction review is only valid for peek actions",
                f"{path}.direction_status",
            )
        return value


def _validate_revision_frames(revision: ActionRevision, canvas: tuple[int, int]) -> None:
    expected = OFFICIAL_ACTIONS[revision.action]
    if len(revision.frames) != expected:
        _invalid(
            f"{revision.action} requires exactly {expected} frames",
            f"revisions.{revision.action}.frames",
        )
    for index, frame in enumerate(revision.frames):
        path = Path(frame)
        if path.is_symlink() or not path.is_file():
            raise RoleContractError(
                ContractErrorCode.INVALID_PATH,
                "revision frame must be a regular file",
                path=f"revisions.{revision.action}.frames[{index}]",
            )
        try:
            with Image.open(path) as image:
                if image.mode != "RGBA" or image.size != canvas:
                    raise RoleContractError(
                        ContractErrorCode.INVALID_IMAGE,
                        f"frame must be RGBA with canvas {canvas}",
                        path=f"revisions.{revision.action}.frames[{index}]",
                    )
                image.verify()
        except RoleContractError:
            raise
        except (OSError, ValueError) as exc:
            raise RoleContractError(
                ContractErrorCode.INVALID_IMAGE,
                f"cannot decode revision frame: {exc}",
                path=f"revisions.{revision.action}.frames[{index}]",
            ) from exc


def _copy_existing_actions(
    source_root: Path,
    staging: Path,
    package: RolePackage,
    replaced: set[str],
) -> None:
    for action, package_action in package.actions:
        if action in replaced:
            continue
        for frame in package_action.frames:
            source = source_root / frame.path
            if source.is_symlink() or not source.is_file():
                raise RoleContractError(
                    ContractErrorCode.INVALID_PATH,
                    "source package frame must be a regular file",
                    path=frame.path,
                )
            destination = staging / frame.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def build_package_revision(
    source_root: Path,
    output_root: Path,
    *,
    package_version: int,
    revisions: tuple[ActionRevision, ...],
    output_accepted: bool = False,
) -> RolePackage:
    """Create one new package version without mutating the accepted source.

    The result contains only runtime contract files. Preview media, provider
    receipts, prompts, input images and task metadata are never copied.
    """
    source_root = Path(source_root).resolve()
    output_root = Path(output_root).resolve()
    if output_root.exists():
        raise RoleContractError(
            ContractErrorCode.ALREADY_EXISTS,
            "package revision output already exists",
            path=str(output_root),
        )
    if output_root.is_relative_to(source_root):
        raise RoleContractError(
            ContractErrorCode.INVALID_PATH,
            "package revision output cannot be inside its source package",
            path=str(output_root),
        )
    source = verify_role_directory(source_root)
    if source.source_schema_version != 2:
        _invalid("only native v2 packages can be edited", "source.schema_version")
    if not source.installable:
        _invalid("source package must be installable", "source.quality")
    if (
        isinstance(package_version, bool)
        or not isinstance(package_version, int)
        or package_version <= source.package_version
        or package_version > 2_147_483_647
    ):
        _invalid(
            "new package_version must be greater than the source version",
            "package_version",
        )
    if not revisions:
        _invalid("at least one action revision is required", "revisions")
    names = [revision.action for revision in revisions]
    if len(names) != len(set(names)):
        _invalid("action revisions must be unique", "revisions")
    for revision in revisions:
        if not FIELD_ID_RE.fullmatch(revision.action):
            _invalid("invalid action ID", f"revisions.{revision.action}")
        revision.quality_dict()
        _validate_revision_frames(revision, source.canvas)

    manifest = source.to_dict()
    manifest["package_version"] = package_version
    actions = dict(manifest["actions"])
    quality = dict(manifest["quality"])
    quality_actions = dict(quality["actions"])
    replaced = set(names)
    staging = output_root.parent / f".{output_root.name}.staging-{uuid.uuid4().hex}"
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(exist_ok=False)
    try:
        _copy_existing_actions(source_root, staging, source, replaced)
        for revision in revisions:
            frame_entries = []
            for index, source_frame in enumerate(revision.frames, start=1):
                relative = Path("actions") / revision.action / f"{index:04d}.png"
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_frame, destination)
                frame_entries.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": sha256(destination.read_bytes()).hexdigest(),
                    }
                )
            actions[revision.action] = {
                "loop": revision.action in LOOP_ACTIONS,
                "frames": frame_entries,
            }
            quality_actions[revision.action] = revision.quality_dict()
        statuses = {
            str(action_quality["status"])
            for action_quality in quality_actions.values()
        }
        quality["status"] = (
            "rejected"
            if "rejected" in statuses
            else "accepted"
            if statuses == {"accepted"}
            else "pending"
        )
        quality["actions"] = quality_actions
        manifest["actions"] = actions
        manifest["quality"] = quality
        manifest["rights"] = {
            **dict(manifest["rights"]),
            "output_accepted": output_accepted,
        }
        candidate = RolePackage.from_dict(manifest)
        (staging / "role.json").write_text(candidate.to_json(), encoding="utf-8")
        verified = verify_role_directory(staging)
        os.replace(staging, output_root)
        return verified
    except Exception:
        if staging.is_dir():
            shutil.rmtree(staging)
        raise
