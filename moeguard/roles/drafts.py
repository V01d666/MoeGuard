"""Recoverable, provider-neutral drafts for the custom-role workbench."""

from __future__ import annotations

import io
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from moeguard.roles._validation import (
    ROLE_ID_RE,
    expect_int,
    expect_object,
    expect_sha256,
    fail,
    reject_unknown,
    required,
)
from moeguard.roles.errors import ContractErrorCode, RoleContractError
from moeguard.roles.profile import CharacterProfile, ProfileInput
from moeguard.roles.spec import OFFICIAL_ACTIONS, REQUIRED_ACTIONS

_INPUT_LIMIT_BYTES = 20 * 1024 * 1024
_INPUT_LIMIT_PIXELS = 4096 * 4096
_MEDIA_BY_FORMAT = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
}


def normalized_identity_png(source: Path) -> bytes:
    """Return the canonical managed PNG bytes used by profile/package binding."""
    try:
        with Image.open(source) as image:
            image.load()
            if image.width < 64 or image.height < 64:
                fail(
                    ContractErrorCode.RESOURCE_LIMIT,
                    "identity image must be at least 64x64",
                    "identity",
                )
            if image.width * image.height > _INPUT_LIMIT_PIXELS:
                fail(
                    ContractErrorCode.RESOURCE_LIMIT,
                    "identity image exceeds 16 megapixels",
                    "identity",
                )
            normalized = image.convert("RGBA")
    except RoleContractError:
        raise
    except (
        OSError,
        UnidentifiedImageError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        raise RoleContractError(
            ContractErrorCode.INVALID_IMAGE,
            f"cannot decode identity image: {exc}",
            path="identity",
        ) from exc
    output = io.BytesIO()
    normalized.save(output, format="PNG")
    return output.getvalue()


def _canonical_actions(raw: Any, path: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        fail(ContractErrorCode.INVALID_TYPE, "must be an array", path)
    if not all(isinstance(action, str) for action in raw):
        fail(ContractErrorCode.INVALID_TYPE, "action IDs must be strings", path)
    if len(raw) != len(set(raw)):
        fail(ContractErrorCode.INVALID_VALUE, "action IDs must be unique", path)
    unknown = set(raw).difference(OFFICIAL_ACTIONS)
    if unknown:
        fail(
            ContractErrorCode.INVALID_VALUE,
            "unknown workbench actions: " + ", ".join(sorted(unknown)),
            path,
        )
    missing = set(REQUIRED_ACTIONS).difference(raw)
    if missing:
        fail(
            ContractErrorCode.INCOMPLETE_ACTIONS,
            "draft must include idle",
            path,
        )
    return tuple(action for action in OFFICIAL_ACTIONS if action in raw)


@dataclass(frozen=True)
class RoleDraft:
    """Editable local state; provider task IDs and prompts are never stored here."""

    profile: CharacterProfile
    selected_actions: tuple[str, ...]
    identity_sha256: str = ""
    schema_version: int = 1

    @classmethod
    def from_dict(cls, raw: Any) -> RoleDraft:
        value = expect_object(raw, "$")
        reject_unknown(
            value,
            {"schema_version", "profile", "selected_actions", "identity_sha256"},
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
                f"RoleDraft schema {schema_version} is not supported",
                "$.schema_version",
            )
        identity_raw = value.get("identity_sha256", "")
        identity_sha256 = (
            expect_sha256(identity_raw, "$.identity_sha256") if identity_raw else ""
        )
        return cls(
            profile=CharacterProfile.from_dict(required(value, "profile", "$")),
            selected_actions=_canonical_actions(
                required(value, "selected_actions", "$"), "$.selected_actions"
            ),
            identity_sha256=identity_sha256,
        )

    @classmethod
    def from_json(cls, text: str) -> RoleDraft:
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise RoleContractError(
                ContractErrorCode.INVALID_JSON,
                f"invalid draft JSON at line {exc.lineno} column {exc.colno}",
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "profile": self.profile.to_dict(),
            "selected_actions": list(self.selected_actions),
        }
        if self.identity_sha256:
            value["identity_sha256"] = self.identity_sha256
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class RoleDraftStore:
    """Atomic draft/profile storage with content-addressed image inputs."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.drafts_root = self.root / "drafts"
        self.profiles_root = self.root / "profiles"
        self.inputs_root = self.root / "inputs"
        self.identities_root = self.root / "identities"

    @staticmethod
    def _validate_profile_id(profile_id: str) -> None:
        if not isinstance(profile_id, str) or not ROLE_ID_RE.fullmatch(profile_id):
            fail(ContractErrorCode.INVALID_ID, "invalid profile ID", "profile_id")

    def import_input_image(self, source: Path) -> ProfileInput:
        source = Path(source)
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise RoleContractError(
                ContractErrorCode.NOT_FOUND, "input image is not readable", path="input"
            ) from exc
        if not source.is_file() or source.is_symlink():
            fail(ContractErrorCode.INVALID_IMAGE, "input must be a regular file", "input")
        if size > _INPUT_LIMIT_BYTES:
            fail(ContractErrorCode.RESOURCE_LIMIT, "input image exceeds 20 MB", "input")
        try:
            with Image.open(source) as image:
                image_format = image.format
                width, height = image.size
                image.verify()
        except (
            OSError,
            UnidentifiedImageError,
            ValueError,
            Image.DecompressionBombError,
        ) as exc:
            raise RoleContractError(
                ContractErrorCode.INVALID_IMAGE, f"cannot decode input image: {exc}", path="input"
            ) from exc
        media = _MEDIA_BY_FORMAT.get(str(image_format).upper())
        if media is None:
            fail(
                ContractErrorCode.INVALID_IMAGE,
                "input image must be PNG, JPEG, or WebP",
                "input",
            )
        if width < 64 or height < 64 or width * height > _INPUT_LIMIT_PIXELS:
            fail(
                ContractErrorCode.RESOURCE_LIMIT,
                "input dimensions must be at least 64x64 and at most 16 megapixels",
                "input",
            )
        # Re-encode every user reference into a managed PNG.  This removes
        # EXIF/XMP/ICC metadata and prevents local paths, camera identifiers or
        # location tags from crossing the role-service boundary.
        payload = normalized_identity_png(source)
        if len(payload) > _INPUT_LIMIT_BYTES:
            fail(
                ContractErrorCode.RESOURCE_LIMIT,
                "normalized input image exceeds 20 MB",
                "input",
            )
        digest = sha256(payload).hexdigest()
        media_type, suffix = "image/png", ".png"
        destination = self.inputs_root / f"{digest}{suffix}"
        self.inputs_root.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(payload)
                if sha256(temporary.read_bytes()).hexdigest() != digest:
                    fail(ContractErrorCode.HASH_MISMATCH, "input copy hash changed", "input")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return ProfileInput(kind="image", sha256=digest, media_type=media_type)

    def import_identity_image(self, source: Path) -> str:
        """Normalize one accepted identity into a path-free managed PNG."""
        payload = normalized_identity_png(Path(source))
        self.identities_root.mkdir(parents=True, exist_ok=True)
        temporary = self.identities_root / f".identity.{uuid.uuid4().hex}.tmp"
        try:
            digest = sha256(payload).hexdigest()
            temporary.write_bytes(payload)
            destination = self.identities_root / f"{digest}.png"
            if destination.exists():
                if sha256(destination.read_bytes()).hexdigest() != digest:
                    fail(
                        ContractErrorCode.HASH_MISMATCH,
                        "managed identity hash changed",
                        "identity",
                    )
            else:
                os.replace(temporary, destination)
            return digest
        finally:
            temporary.unlink(missing_ok=True)

    def identity_path(self, digest: str) -> Path | None:
        try:
            digest = expect_sha256(digest, "identity_sha256")
        except RoleContractError:
            return None
        path = self.identities_root / f"{digest}.png"
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != digest:
            return None
        return path

    def input_path(self, profile_input: ProfileInput) -> Path | None:
        if profile_input.kind != "image":
            return None
        suffix = {value[0]: value[1] for value in _MEDIA_BY_FORMAT.values()}[
            profile_input.media_type
        ]
        path = self.inputs_root / f"{profile_input.sha256}{suffix}"
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != profile_input.sha256:
            return None
        return path

    def save_draft(self, draft: RoleDraft) -> Path:
        canonical = RoleDraft.from_json(draft.to_json())
        if canonical.profile.input.kind == "image" and self.input_path(
            canonical.profile.input
        ) is None:
            fail(
                ContractErrorCode.NOT_FOUND,
                "managed input image is missing or corrupt",
                "$.profile.input",
            )
        if canonical.identity_sha256 and self.identity_path(
            canonical.identity_sha256
        ) is None:
            fail(
                ContractErrorCode.NOT_FOUND,
                "managed identity image is missing or corrupt",
                "$.identity_sha256",
            )
        self.drafts_root.mkdir(parents=True, exist_ok=True)
        destination = self.drafts_root / f"{canonical.profile.profile_id}.json"
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(canonical.to_json(), encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def load_draft(self, profile_id: str) -> RoleDraft:
        self._validate_profile_id(profile_id)
        path = self.drafts_root / f"{profile_id}.json"
        try:
            draft = RoleDraft.from_json(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RoleContractError(
                ContractErrorCode.NOT_FOUND, "draft does not exist", path="draft"
            ) from exc
        if draft.profile.profile_id != profile_id:
            fail(ContractErrorCode.INVALID_VALUE, "draft profile ID mismatch", "$.profile")
        return draft

    def load_profile_revision(
        self, profile_id: str, appearance_revision: int
    ) -> CharacterProfile:
        """Load one immutable committed appearance profile revision."""
        self._validate_profile_id(profile_id)
        if (
            isinstance(appearance_revision, bool)
            or not isinstance(appearance_revision, int)
            or appearance_revision < 1
        ):
            fail(
                ContractErrorCode.INVALID_VALUE,
                "appearance revision must be a positive integer",
                "appearance_revision",
            )
        path = (
            self.profiles_root
            / profile_id
            / str(appearance_revision)
            / "profile.json"
        )
        try:
            profile = CharacterProfile.from_json(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RoleContractError(
                ContractErrorCode.NOT_FOUND,
                "profile revision does not exist",
                path="profile",
            ) from exc
        if (
            profile.profile_id != profile_id
            or profile.appearance_revision != appearance_revision
        ):
            fail(
                ContractErrorCode.INVALID_VALUE,
                "committed profile revision does not match its storage path",
                "profile",
            )
        return profile

    def next_appearance_revision(self, profile_id: str) -> int:
        """Allocate after the highest valid committed revision directory."""
        self._validate_profile_id(profile_id)
        profile_root = self.profiles_root / profile_id
        if not profile_root.is_dir():
            return 1
        revisions = [
            int(path.name)
            for path in profile_root.iterdir()
            if path.is_dir()
            and path.name.isascii()
            and path.name.isdigit()
            and int(path.name) >= 1
            and (path / "profile.json").is_file()
        ]
        return max(revisions, default=0) + 1

    def list_drafts(self) -> tuple[RoleDraft, ...]:
        if not self.drafts_root.is_dir():
            return ()
        drafts = [
            self.load_draft(path.stem)
            for path in sorted(self.drafts_root.glob("*.json"))
            if path.is_file()
        ]
        return tuple(sorted(drafts, key=lambda item: item.profile.profile_id))

    def commit_profile_revision(self, profile: CharacterProfile) -> Path:
        canonical = CharacterProfile.from_json(profile.to_json())
        if canonical.input.kind == "image" and self.input_path(canonical.input) is None:
            fail(
                ContractErrorCode.NOT_FOUND,
                "managed input image is missing or corrupt",
                "$.input",
            )
        profile_root = self.profiles_root / canonical.profile_id
        revision_root = profile_root / str(canonical.appearance_revision)
        destination = revision_root / "profile.json"
        payload = canonical.to_json()
        if destination.exists():
            if destination.read_text(encoding="utf-8") == payload:
                return destination
            fail(
                ContractErrorCode.ALREADY_EXISTS,
                "appearance revision already exists with different content",
                "$.appearance_revision",
            )
        profile_root.mkdir(parents=True, exist_ok=True)
        staging = profile_root / f".staging-{canonical.appearance_revision}-{uuid.uuid4().hex}"
        staging.mkdir(exist_ok=False)
        try:
            (staging / "profile.json").write_text(payload, encoding="utf-8")
            os.replace(staging, revision_root)
        except Exception:
            if staging.is_dir():
                shutil.rmtree(staging)
            raise
        return destination
