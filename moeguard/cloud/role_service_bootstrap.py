"""Fail-closed client bootstrap for the private HTTPS role service."""

from __future__ import annotations

import json
import os
import re
import secrets
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from moeguard.cloud.role_service_http_client import HttpRoleServiceTransport

_TOKEN_RE = re.compile(r"mgr_[0-9a-f]{24}_[A-Za-z0-9_-]{32,128}")
_MAX_TOKEN_FILE_BYTES = 256
_MAX_PUBLIC_CONFIG_BYTES = 4096


@dataclass(frozen=True)
class ClientEnrollmentCredentials:
    """Random installation identity; never derived from hardware attributes."""

    enrollment_id: str
    enrollment_secret: str = field(repr=False)


def new_client_enrollment_credentials() -> ClientEnrollmentCredentials:
    return ClientEnrollmentCredentials(
        enrollment_id=f"eni_{secrets.token_hex(16)}",
        enrollment_secret=f"ens_{secrets.token_urlsafe(32)}",
    )


def https_role_service_origin(value: str) -> str:
    """Normalize a public role-service HTTPS origin without vendor coupling."""

    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("role service URL must be an HTTPS origin without credentials")
    return urllib.parse.urlunsplit(("https", parsed.netloc, "", "", ""))


def _read_token_file(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("role service token file path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise ValueError("role service token file must be a regular non-symlink file")
    try:
        with path.open("rb") as stream:
            payload = stream.read(_MAX_TOKEN_FILE_BYTES + 1)
    except OSError as exc:
        raise ValueError("role service token file is not readable") from exc
    if len(payload) > _MAX_TOKEN_FILE_BYTES:
        raise ValueError("role service token file is too large")
    try:
        token = payload.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("role service token file is invalid") from exc
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("role service token file is invalid")
    return token


def role_service_transport_from_environment(
    environ: Mapping[str, str] | None = None,
) -> HttpRoleServiceTransport | None:
    """Build an HTTPS transport only after an explicit client-side mode gate.

    The bearer value is read from an absolute file path and is never accepted
    directly from an environment variable, command-line argument, or app config.
    """

    values = os.environ if environ is None else environ
    mode = values.get("MOEGUARD_ROLE_SERVICE_MODE", "disabled").strip().lower()
    base_url = values.get("MOEGUARD_ROLE_SERVICE_URL", "").strip()
    token_file = values.get("MOEGUARD_ROLE_SERVICE_TOKEN_FILE", "").strip()
    if mode == "disabled":
        if base_url or token_file:
            raise ValueError(
                "role service URL/token file require explicit https client mode"
            )
        return None
    if mode != "https":
        raise ValueError("MOEGUARD_ROLE_SERVICE_MODE must be disabled or https")
    if not base_url or not token_file:
        raise ValueError("https client mode requires a service URL and token file")
    return HttpRoleServiceTransport(
        https_role_service_origin(base_url),
        _read_token_file(token_file),
        timeout=30.0,
    )


def role_service_origin_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read a public enrollment endpoint without requiring a developer token."""

    values = os.environ if environ is None else environ
    mode = values.get("MOEGUARD_ROLE_SERVICE_MODE", "disabled").strip().lower()
    base_url = values.get("MOEGUARD_ROLE_SERVICE_URL", "").strip()
    token_file = values.get("MOEGUARD_ROLE_SERVICE_TOKEN_FILE", "").strip()
    if mode == "disabled":
        if base_url or token_file:
            raise ValueError("role service values require explicit https client mode")
        return None
    if mode != "https" or not base_url:
        raise ValueError("role service enrollment requires an HTTPS service URL")
    return https_role_service_origin(base_url)


def role_service_origin_from_file(path: Path) -> str | None:
    """Load the non-secret public endpoint shipped beside a release build."""

    path = Path(path)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("role service public config path is unsafe")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError("role service public config is unreadable") from exc
    if not payload or len(payload) > _MAX_PUBLIC_CONFIG_BYTES:
        raise ValueError("role service public config is invalid")
    try:
        raw = json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("role service public config is invalid") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "service_origin"}:
        raise ValueError("role service public config schema is invalid")
    if raw["schema_version"] != 1 or not isinstance(raw["service_origin"], str):
        raise ValueError("role service public config version is unsupported")
    return https_role_service_origin(raw["service_origin"])
