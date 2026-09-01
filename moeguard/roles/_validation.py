"""Small strict-validation helpers without adding a JSON Schema dependency."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from moeguard.roles.errors import ContractErrorCode, RoleContractError

ROLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,47}$")
FIELD_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def fail(code: ContractErrorCode, message: str, path: str) -> None:
    raise RoleContractError(code, message, path=path)


def expect_object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(ContractErrorCode.INVALID_TYPE, "must be an object", path)
    if not all(isinstance(key, str) for key in value):
        fail(ContractErrorCode.INVALID_TYPE, "object keys must be strings", path)
    return value


def reject_unknown(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if not unknown:
        return
    private = [
        key
        for key in unknown
        if any(
            token in key.lower()
            for token in ("api_key", "token", "task_id", "signed_url", "prompt", "seed")
        )
    ]
    if private:
        fail(
            ContractErrorCode.PRIVATE_FIELD,
            "private generation fields are not allowed: " + ", ".join(private),
            path,
        )
    fail(
        ContractErrorCode.UNKNOWN_FIELD,
        "unknown fields: " + ", ".join(unknown),
        path,
    )


def required(value: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in value:
        fail(ContractErrorCode.MISSING_FIELD, f"missing required field {key}", path)
    return value[key]


def expect_string(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        fail(ContractErrorCode.INVALID_TYPE, "must be a string", path)
    normalized = value.strip()
    if len(normalized) < minimum or len(normalized) > maximum:
        fail(
            ContractErrorCode.INVALID_VALUE,
            f"length must be between {minimum} and {maximum}",
            path,
        )
    return normalized


def expect_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        fail(ContractErrorCode.INVALID_TYPE, "must be a boolean", path)
    return value


def expect_int(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(ContractErrorCode.INVALID_TYPE, "must be an integer", path)
    if not minimum <= value <= maximum:
        fail(
            ContractErrorCode.INVALID_VALUE,
            f"must be between {minimum} and {maximum}",
            path,
        )
    return value


def expect_number(value: Any, path: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(ContractErrorCode.INVALID_TYPE, "must be a number", path)
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        fail(
            ContractErrorCode.INVALID_VALUE,
            f"must be a finite number between {minimum} and {maximum}",
            path,
        )
    return number


def expect_choice(value: Any, choices: set[str], path: str) -> str:
    result = expect_string(value, path, minimum=1, maximum=80)
    if result not in choices:
        fail(
            ContractErrorCode.INVALID_VALUE,
            "must be one of: " + ", ".join(sorted(choices)),
            path,
        )
    return result


def expect_sha256(value: Any, path: str) -> str:
    result = expect_string(value, path, minimum=64, maximum=64).lower()
    if not SHA256_RE.fullmatch(result):
        fail(ContractErrorCode.INVALID_VALUE, "must be a lowercase SHA-256 digest", path)
    return result
