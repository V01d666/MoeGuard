"""Managed, versioned local library for validated custom role packages."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, replace
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
    name: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.package.display_name


def unique_role_name(name: str, occupied: set[str]) -> str:
    """Use a Windows-style suffix without changing the role's identity."""
    name = name.strip()
    if not name or len(name) > 80:
        raise ValueError("角色名称须为 1–80 个字符")
    used = {value.casefold() for value in occupied}
    if name.casefold() not in used:
        return name
    match = re.fullmatch(r"(.+?)[（(]([2-9][0-9]*|1[0-9]+)[）)]", name)
    base, number = (match[1], int(match[2]) + 1) if match else (name, 2)
    while True:
        suffix = f"（{number}）"
        candidate = base[:80 - len(suffix)].rstrip() + suffix
        if candidate.casefold() not in used:
            return candidate
        number += 1


class RoleLibrary:
    """Own every mutable custom-role path under one injected library root."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or (base_dir() / "roles")).resolve()
        self.staging_root = self.root / ".staging"

    def ensure(self) -> None:
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def _read_names(self) -> dict[str, str]:
        path = self.root / ".display-names.json"
        if not path.exists():
            return {}
        if path.is_symlink():
            raise ValueError("角色名称记录不能是符号链接")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("角色名称记录无效")
        names = value.get("names")
        if not isinstance(names, dict) or any(
            not isinstance(key, str) or not ROLE_ID_RE.fullmatch(key)
            or not isinstance(name, str) or not name.strip() or len(name) > 80
            for key, name in names.items()
        ):
            raise ValueError("角色名称记录无效")
        return names

    def _write_names(self, names: dict[str, str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / ".display-names.json"
        if target.is_symlink():
            raise ValueError("角色名称记录不能是符号链接")
        temporary = self.root / f".display-names-{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps({"schema_version": 1, "names": names}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _resolved_names(self) -> dict[str, str]:
        stored = self._read_names()
        latest: dict[str, tuple[int, str]] = {}
        if self.root.is_dir():
            for path in sorted(self.root.glob("*/*/role.json")):
                if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
                    continue
                try:
                    package = RolePackage.from_json(path.read_text(encoding="utf-8"))
                    if (package.role_id != path.parent.parent.name
                            or str(package.package_version) != path.parent.name):
                        continue
                except (OSError, UnicodeError, RoleContractError):
                    continue
                previous = latest.get(package.role_id)
                if previous is None or package.package_version > previous[0]:
                    latest[package.role_id] = (package.package_version, package.display_name)
        resolved: dict[str, str] = {}
        # Existing aliases win; unlabelled legacy roles are assigned deterministically.
        for role_id in sorted(latest, key=lambda key: (key not in stored, key)):
            wanted = stored.get(role_id, latest[role_id][1])
            resolved[role_id] = unique_role_name(wanted, set(resolved.values()))
        return resolved

    def suggest_name(self, name: str, *, role_id: str = "") -> str:
        names = self._resolved_names()
        return unique_role_name(name, {value for key, value in names.items() if key != role_id})

    def rename(self, key: PackageKey, name: str) -> InstalledRole:
        """Rename every version locally; immutable package/profile files stay intact."""
        installed = self._get_raw(key)
        names = self._resolved_names()
        names[key.role_id] = unique_role_name(
            name, {value for role_id, value in names.items() if role_id != key.role_id}
        )
        self._write_names(names)
        return replace(installed, name=names[key.role_id])

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

    def install_directory(
        self, source: Path, *, display_name: str | None = None,
    ) -> InstalledRole:
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
        return self._commit_staging(staging, copied, display_name=display_name)

    def _commit_staging(
        self, staging: Path, package: RolePackage, *, display_name: str | None = None,
    ) -> InstalledRole:
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
            names = self._resolved_names()
            if key.role_id not in names or display_name is not None:
                names[key.role_id] = unique_role_name(
                    package.display_name if display_name is None else display_name,
                    {value for role_id, value in names.items() if role_id != key.role_id},
                )
            self._write_names(names)
            os.replace(staging, target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            if target.parent.is_dir() and not any(target.parent.iterdir()):
                target.parent.rmdir()
            raise
        return InstalledRole(key=key, package=package, root=target, name=names[key.role_id])

    def get(self, key: PackageKey) -> InstalledRole:
        installed = self._get_raw(key)
        return replace(installed, name=self._resolved_names().get(key.role_id, ""))

    def _get_raw(self, key: PackageKey) -> InstalledRole:
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
                    installed.append(self._get_raw(key))
                except RoleContractError:
                    continue
        names = self._resolved_names()
        return tuple(
            replace(item, name=names.get(item.key.role_id, ""))
            for item in sorted(installed, key=lambda item: item.key)
        )

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
