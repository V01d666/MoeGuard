"""Managed, versioned local library for validated custom role packages."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from moeguard.roles._validation import ROLE_ID_RE
from moeguard.roles.archive import extract_role_archive, verify_role_directory
from moeguard.roles.errors import ContractErrorCode, RoleContractError
from moeguard.roles.package import RolePackage
from moeguard.utils.paths import base_dir


@dataclass(frozen=True, order=True)
class PackageKey:
    role_id: str
    package_version: int

    def __post_init__(self) -> None:
        if not ROLE_ID_RE.fullmatch(self.role_id):
            raise RoleContractError(
                ContractErrorCode.INVALID_ID,
                "invalid role ID",
                path="package_key.role_id",
            )
        if (
            isinstance(self.package_version, bool)
            or not isinstance(self.package_version, int)
            or not 1 <= self.package_version <= 2_147_483_647
        ):
            raise RoleContractError(
                ContractErrorCode.INVALID_VALUE,
                "invalid package version",
                path="package_key.package_version",
            )

    def __str__(self) -> str:
        return f"{self.role_id}@{self.package_version}"


@dataclass(frozen=True)
class InstalledRole:
    key: PackageKey
    package: RolePackage
    root: Path


class RoleLibrary:
    """Own every mutable custom-role path under one injected library root."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or (base_dir() / "roles")).resolve()
        self.staging_root = self.root / ".staging"

    def ensure(self) -> None:
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def package_root(self, key: PackageKey) -> Path:
        return self.root / key.role_id / str(key.package_version)

    def cleanup_staging(self) -> int:
        self.ensure()
        removed = 0
        for child in self.staging_root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
                removed += 1
            elif child.exists() or child.is_symlink():
                child.unlink()
                removed += 1
        return removed

    def install(self, archive: Path) -> InstalledRole:
        self.ensure()
        staging = self.staging_root / uuid.uuid4().hex
        package = extract_role_archive(archive, staging)
        return self._commit_staging(staging, package)

    def install_directory(self, source: Path) -> InstalledRole:
        """Import a generated directory through the same managed-library gate."""
        source = Path(source)
        if source.is_symlink() or not source.is_dir():
            raise RoleContractError(
                ContractErrorCode.INVALID_PATH,
                "generated role root must be a regular directory",
                path=str(source),
            )
        try:
            package = RolePackage.from_json(
                (source / "role.json").read_text(encoding="utf-8")
            )
        except OSError as exc:
            raise RoleContractError(
                ContractErrorCode.NOT_FOUND,
                "generated role.json is not readable",
                path=str(source / "role.json"),
            ) from exc
        self.ensure()
        staging = self.staging_root / uuid.uuid4().hex
        try:
            staging.mkdir(exist_ok=False)
            shutil.copy2(source / "role.json", staging / "role.json")
            for _, action in package.actions:
                for frame in action.frames:
                    source_frame = source / frame.path
                    if source_frame.is_symlink() or not source_frame.is_file():
                        raise RoleContractError(
                            ContractErrorCode.INVALID_PATH,
                            "generated frame must be a regular file",
                            path=frame.path,
                        )
                    target_frame = staging / frame.path
                    target_frame.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_frame, target_frame)
            copied = verify_role_directory(staging)
        except Exception:
            if staging.is_dir():
                shutil.rmtree(staging)
            raise
        return self._commit_staging(staging, copied)

    def _commit_staging(self, staging: Path, package: RolePackage) -> InstalledRole:
        if not package.installable:
            shutil.rmtree(staging)
            raise RoleContractError(
                ContractErrorCode.INVALID_VALUE,
                "role package has not passed quality and rights review",
                path="role.json",
            )
        key = PackageKey(package.role_id, package.package_version)
        target = self.package_root(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(staging)
            raise RoleContractError(
                ContractErrorCode.ALREADY_EXISTS,
                f"role package {key} is already installed",
                path=str(target),
            )
        try:
            os.replace(staging, target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            if target.parent.is_dir() and not any(target.parent.iterdir()):
                target.parent.rmdir()
            raise
        return InstalledRole(key=key, package=package, root=target)

    def get(self, key: PackageKey) -> InstalledRole:
        root = self.package_root(key)
        if root.is_symlink() or not root.is_dir():
            raise RoleContractError(
                ContractErrorCode.NOT_FOUND,
                f"role package {key} is not installed",
                path=str(root),
            )
        package = verify_role_directory(root)
        if package.role_id != key.role_id or package.package_version != key.package_version:
            raise RoleContractError(
                ContractErrorCode.INVALID_VALUE,
                "installed package identity does not match its directory key",
                path=str(root),
            )
        return InstalledRole(key=key, package=package, root=root)

    def list(self) -> tuple[InstalledRole, ...]:
        if not self.root.is_dir():
            return ()
        installed: list[InstalledRole] = []
        for role_root in sorted(self.root.iterdir()):
            if not role_root.is_dir() or role_root.name.startswith("."):
                continue
            if not ROLE_ID_RE.fullmatch(role_root.name):
                continue
            for version_root in sorted(role_root.iterdir()):
                if not version_root.is_dir() or not version_root.name.isdecimal():
                    continue
                try:
                    key = PackageKey(role_root.name, int(version_root.name))
                    installed.append(self.get(key))
                except RoleContractError:
                    continue
        return tuple(sorted(installed, key=lambda item: item.key))

    def latest(self, role_id: str) -> InstalledRole:
        candidates = [item for item in self.list() if item.key.role_id == role_id]
        if not candidates:
            raise RoleContractError(
                ContractErrorCode.NOT_FOUND,
                f"role {role_id} is not installed",
                path=role_id,
            )
        return max(candidates, key=lambda item: item.key.package_version)

    def next_version(self, role_id: str) -> int:
        candidates = [
            item.key.package_version
            for item in self.list()
            if item.key.role_id == role_id
        ]
        return max(candidates, default=0) + 1

    def remove(self, key: PackageKey, *, active_key: PackageKey | None = None) -> None:
        if key == active_key:
            raise RoleContractError(
                ContractErrorCode.ACTIVE_ROLE,
                "cannot remove the active role before switching away",
                path=str(key),
            )
        target = self.package_root(key)
        if target.is_symlink() or not target.is_dir():
            raise RoleContractError(
                ContractErrorCode.NOT_FOUND,
                f"role package {key} is not installed",
                path=str(target),
            )
        shutil.rmtree(target)
        role_root = target.parent
        if role_root.is_dir() and not any(role_root.iterdir()):
            role_root.rmdir()
