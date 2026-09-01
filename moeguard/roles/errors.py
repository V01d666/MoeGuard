"""Stable error codes shared by role profile and package contracts."""

from __future__ import annotations

from enum import StrEnum


class ContractErrorCode(StrEnum):
    INVALID_JSON = "invalid_json"
    INVALID_TYPE = "invalid_type"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    INVALID_VALUE = "invalid_value"
    INVALID_ID = "invalid_id"
    INVALID_PATH = "invalid_path"
    PRIVATE_FIELD = "private_field"
    INCOMPLETE_ACTIONS = "incomplete_actions"
    HASH_MISMATCH = "hash_mismatch"
    UNSAFE_ARCHIVE = "unsafe_archive"
    RESOURCE_LIMIT = "resource_limit"
    INVALID_IMAGE = "invalid_image"
    ALREADY_EXISTS = "already_exists"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    ACTIVE_ROLE = "active_role"


class RoleContractError(ValueError):
    """A user-safe validation failure with a stable machine-readable code."""

    def __init__(
        self,
        code: ContractErrorCode,
        message: str,
        *,
        path: str = "$",
    ) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code.value} at {path}: {message}")
