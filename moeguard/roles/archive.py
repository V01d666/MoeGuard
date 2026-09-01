"""Safe inspection and extraction for untrusted ``.moeguard-role`` archives."""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from PIL import Image, UnidentifiedImageError

from moeguard.roles.errors import ContractErrorCode, RoleContractError
from moeguard.roles.package import RolePackage


@dataclass(frozen=True)
class ArchiveLimits:
    max_archive_bytes: int = 64 * 1024 * 1024
    max_members: int = 512
    max_member_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 128 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_canvas_pixels: int = 2048 * 2048
    max_total_pixels: int = 64 * 1024 * 1024


DEFAULT_LIMITS = ArchiveLimits()


def _error(code: ContractErrorCode, message: str, path: str = "$") -> None:
    raise RoleContractError(code, message, path=path)


def _safe_member_name(info: ZipInfo) -> str:
    name = info.filename
    if not name or "\\" in name or "\x00" in name:
        _error(ContractErrorCode.INVALID_PATH, "archive member path is invalid", name or "$")
    pure = PurePosixPath(name.rstrip("/"))
    if (
        not pure.parts
        or pure.is_absolute()
        or ":" in pure.parts[0]
        or ".." in pure.parts
        or "." in pure.parts
        or pure.as_posix() != name.rstrip("/")
    ):
        _error(ContractErrorCode.INVALID_PATH, "unsafe archive member path", name)
    unix_mode = info.external_attr >> 16
    if stat.S_ISLNK(unix_mode):
        _error(ContractErrorCode.UNSAFE_ARCHIVE, "symbolic links are not allowed", name)
    file_type = stat.S_IFMT(unix_mode)
    if info.create_system == 3 and file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        _error(ContractErrorCode.UNSAFE_ARCHIVE, "special files are not allowed", name)
    if info.flag_bits & 0x1:
        _error(ContractErrorCode.UNSAFE_ARCHIVE, "encrypted members are not allowed", name)
    if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
        _error(ContractErrorCode.UNSAFE_ARCHIVE, "unsupported compression method", name)
    return pure.as_posix() + ("/" if info.is_dir() else "")


def _inspect_zip(
    archive: Path,
    zip_file: ZipFile,
    limits: ArchiveLimits,
) -> tuple[RolePackage, tuple[ZipInfo, ...]]:
    if archive.suffix.lower() != ".moeguard-role":
        _error(
            ContractErrorCode.INVALID_VALUE,
            "role archive must use the .moeguard-role extension",
            str(archive),
        )
    if archive.stat().st_size > limits.max_archive_bytes:
        _error(ContractErrorCode.RESOURCE_LIMIT, "archive is larger than 64 MiB")
    infos = tuple(zip_file.infolist())
    if not infos or len(infos) > limits.max_members:
        _error(ContractErrorCode.RESOURCE_LIMIT, "archive member count is out of range")

    names: dict[str, ZipInfo] = {}
    total_bytes = 0
    for info in infos:
        name = _safe_member_name(info)
        collision_key = name.casefold()
        if collision_key in names:
            _error(
                ContractErrorCode.UNSAFE_ARCHIVE,
                f"duplicate or case-colliding member: {name}",
                name,
            )
        names[collision_key] = info
        if info.is_dir():
            continue
        if info.file_size > limits.max_member_bytes:
            _error(ContractErrorCode.RESOURCE_LIMIT, "archive member is too large", name)
        total_bytes += info.file_size
        if total_bytes > limits.max_total_bytes:
            _error(ContractErrorCode.RESOURCE_LIMIT, "archive expands beyond the total limit")
        if info.file_size >= 1024 * 1024:
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > limits.max_compression_ratio:
                _error(
                    ContractErrorCode.RESOURCE_LIMIT,
                    "archive member compression ratio is suspicious",
                    name,
                )

    manifest_info = names.get("role.json")
    if manifest_info is None or manifest_info.is_dir():
        _error(ContractErrorCode.MISSING_FIELD, "archive must contain role.json")
    if manifest_info.file_size > 1024 * 1024:
        _error(ContractErrorCode.RESOURCE_LIMIT, "role.json is too large", "role.json")
    try:
        manifest_text = zip_file.read(manifest_info).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RoleContractError(
            ContractErrorCode.INVALID_JSON,
            f"cannot read UTF-8 role.json: {exc}",
            path="role.json",
        ) from exc
    package = RolePackage.from_json(manifest_text)
    referenced_files = {
        frame.path for _, action in package.actions for frame in action.frames
    }
    expected_files = {"role.json", *referenced_files}
    expected_dirs = {"actions/"} | {
        f"actions/{action}/" for action, _ in package.actions
    }
    actual_files = {info.filename for info in infos if not info.is_dir()}
    actual_dirs = {info.filename for info in infos if info.is_dir()}
    if actual_files != expected_files:
        missing = sorted(expected_files.difference(actual_files))
        extra = sorted(actual_files.difference(expected_files))
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing[:5]))
        if extra:
            details.append("unexpected: " + ", ".join(extra[:5]))
        _error(
            ContractErrorCode.UNSAFE_ARCHIVE,
            "archive files do not match role.json (" + "; ".join(details) + ")",
        )
    unexpected_dirs = actual_dirs.difference(expected_dirs)
    if unexpected_dirs:
        _error(
            ContractErrorCode.UNSAFE_ARCHIVE,
            "unexpected directories: " + ", ".join(sorted(unexpected_dirs)[:5]),
        )
    return package, infos


def inspect_role_archive(
    archive: Path,
    *,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> RolePackage:
    """Validate archive metadata and its v2 manifest without writing to disk."""
    try:
        with ZipFile(archive, "r") as zip_file:
            package, _ = _inspect_zip(archive, zip_file, limits)
            return package
    except BadZipFile as exc:
        raise RoleContractError(
            ContractErrorCode.UNSAFE_ARCHIVE, "file is not a valid ZIP archive"
        ) from exc


def _verify_extracted_package(
    package: RolePackage,
    destination: Path,
    limits: ArchiveLimits,
) -> None:
    total_pixels = 0
    for action_name, action in package.actions:
        for frame in action.frames:
            path = destination / Path(frame.path)
            digest = sha256(path.read_bytes()).hexdigest()
            if digest != frame.sha256:
                _error(
                    ContractErrorCode.HASH_MISMATCH,
                    "frame hash does not match role.json",
                    frame.path,
                )
            try:
                with Image.open(path) as image:
                    width, height = image.size
                    mode = image.mode
                    if width * height > limits.max_canvas_pixels:
                        _error(
                            ContractErrorCode.RESOURCE_LIMIT,
                            "frame pixel count is too large",
                            frame.path,
                        )
                    if (width, height) != package.canvas:
                        _error(
                            ContractErrorCode.INVALID_IMAGE,
                            f"frame size {(width, height)} does not match {package.canvas}",
                            frame.path,
                        )
                    if mode != "RGBA":
                        _error(ContractErrorCode.INVALID_IMAGE, "frame must be RGBA", frame.path)
                    image.load()
            except (OSError, UnidentifiedImageError) as exc:
                raise RoleContractError(
                    ContractErrorCode.INVALID_IMAGE,
                    f"frame is not a readable PNG: {exc}",
                    path=frame.path,
                ) from exc
            total_pixels += package.canvas[0] * package.canvas[1]
            if total_pixels > limits.max_total_pixels:
                _error(
                    ContractErrorCode.RESOURCE_LIMIT,
                    "package contains too many decoded pixels",
                    action_name,
                )


def verify_role_directory(
    destination: Path,
    *,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> RolePackage:
    """Verify a native v2 directory after installation or before activation."""
    for path in destination.rglob("*"):
        if path.is_symlink():
            _error(
                ContractErrorCode.UNSAFE_ARCHIVE,
                "installed role must not contain symbolic links",
                str(path),
            )
    try:
        package = RolePackage.from_json(
            (destination / "role.json").read_text(encoding="utf-8")
        )
    except OSError as exc:
        raise RoleContractError(
            ContractErrorCode.NOT_FOUND,
            f"cannot read installed role.json: {exc}",
            path=str(destination / "role.json"),
        ) from exc
    referenced = {
        frame.path for _, action in package.actions for frame in action.frames
    }
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    expected = {"role.json", *referenced}
    if actual != expected:
        _error(
            ContractErrorCode.UNSAFE_ARCHIVE,
            "installed role files do not match role.json",
            str(destination),
        )
    _verify_extracted_package(package, destination, limits)
    return package


def extract_role_archive(
    archive: Path,
    destination: Path,
    *,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> RolePackage:
    """Extract a validated v2 package into a newly created destination."""
    if destination.exists():
        _error(
            ContractErrorCode.INVALID_VALUE,
            "extraction destination must not already exist",
            str(destination),
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        try:
            with ZipFile(archive, "r") as zip_file:
                package, infos = _inspect_zip(archive, zip_file, limits)
                for info in infos:
                    if info.is_dir():
                        continue
                    relative = PurePosixPath(info.filename)
                    target = destination.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with zip_file.open(info, "r") as source, target.open("xb") as output:
                        while chunk := source.read(1024 * 1024):
                            written += len(chunk)
                            if written > info.file_size or written > limits.max_member_bytes:
                                _error(
                                    ContractErrorCode.RESOURCE_LIMIT,
                                    "member exceeded its declared size",
                                    info.filename,
                                )
                            output.write(chunk)
                    if written != info.file_size:
                        _error(
                            ContractErrorCode.UNSAFE_ARCHIVE,
                            "member size did not match ZIP metadata",
                            info.filename,
                        )
        except BadZipFile as exc:
            raise RoleContractError(
                ContractErrorCode.UNSAFE_ARCHIVE, "file is not a valid ZIP archive"
            ) from exc
        return verify_role_directory(destination, limits=limits)
    except Exception:
        shutil.rmtree(destination)
        raise
