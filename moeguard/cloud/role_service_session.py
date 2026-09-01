"""OS-protected local session storage for the public role-service client."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import sys
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from moeguard.cloud.role_service_http_client import HttpRoleServiceTransport

_MAX_SESSION_BYTES = 64 * 1024
_ENTROPY = b"MoeGuard/role-service/session/v1"
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_TOKEN_RE = re.compile(r"^mgr_[a-f0-9]{24}_[A-Za-z0-9_-]{32,128}$")
_ENROLLMENT_ID_RE = re.compile(r"^eni_[a-f0-9]{32}$")
_ENROLLMENT_SECRET_RE = re.compile(r"^ens_[A-Za-z0-9_-]{43,128}$")


class CredentialProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _input_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    return blob, buffer


class WindowsDpapiProtector:
    """Bind session secrets to the current Windows user without app-owned keys."""

    _UI_FORBIDDEN = 0x1

    @staticmethod
    def _libraries():
        if sys.platform != "win32":
            raise OSError("Windows credential protection is unavailable")
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        return crypt32, kernel32

    def protect(self, value: bytes) -> bytes:
        if not value:
            raise ValueError("credential payload must not be empty")
        crypt32, kernel32 = self._libraries()
        source, source_buffer = _input_blob(value)
        entropy, entropy_buffer = _input_blob(_ENTROPY)
        output = _DataBlob()
        if not crypt32.CryptProtectData(
            ctypes.byref(source),
            "MoeGuard role service session",
            ctypes.byref(entropy),
            None,
            None,
            self._UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            error = ctypes.get_last_error()
            raise OSError(
                error,
                "Windows credential protection failed",
            )
        del source_buffer, entropy_buffer
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)

    def unprotect(self, value: bytes) -> bytes:
        if not value:
            raise ValueError("protected credential payload must not be empty")
        crypt32, kernel32 = self._libraries()
        source, source_buffer = _input_blob(value)
        entropy, entropy_buffer = _input_blob(_ENTROPY)
        output = _DataBlob()
        description = ctypes.c_wchar_p()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            ctypes.byref(entropy),
            None,
            None,
            self._UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            error = ctypes.get_last_error()
            raise OSError(
                error,
                "Windows credential recovery failed",
            )
        del source_buffer, entropy_buffer
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)
            if description:
                kernel32.LocalFree(description)


@dataclass(frozen=True)
class RoleServiceSession:
    service_origin: str
    account_id: str
    expires_at: int
    bearer_token: str = field(repr=False)
    enrollment_id: str = field(repr=False)
    enrollment_secret: str = field(repr=False)

    def transport(self, *, timeout: float = 30.0) -> HttpRoleServiceTransport:
        return HttpRoleServiceTransport(
            self.service_origin, self.bearer_token, timeout=timeout
        )


class RoleServiceSessionStore:
    """Atomic encrypted session file with injectable protection for tests."""

    def __init__(self, path: Path, protector: CredentialProtector | None = None) -> None:
        self.path = Path(path)
        self.protector = protector or WindowsDpapiProtector()

    @staticmethod
    def _validate(session: RoleServiceSession) -> RoleServiceSession:
        parsed = urllib.parse.urlsplit(session.service_origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("role service session origin is invalid")
        if not _ACCOUNT_ID_RE.fullmatch(session.account_id):
            raise ValueError("role service session account is invalid")
        if not _TOKEN_RE.fullmatch(session.bearer_token):
            raise ValueError("role service session token is invalid")
        if not _ENROLLMENT_ID_RE.fullmatch(session.enrollment_id):
            raise ValueError("role service enrollment ID is invalid")
        if not _ENROLLMENT_SECRET_RE.fullmatch(session.enrollment_secret):
            raise ValueError("role service enrollment secret is invalid")
        if (
            isinstance(session.expires_at, bool)
            or not isinstance(session.expires_at, int)
            or session.expires_at <= 0
        ):
            raise ValueError("role service session expiry is invalid")
        return RoleServiceSession(
            service_origin=urllib.parse.urlunsplit(
                ("https", parsed.netloc, "", "", "")
            ),
            account_id=session.account_id,
            expires_at=session.expires_at,
            bearer_token=session.bearer_token,
            enrollment_id=session.enrollment_id,
            enrollment_secret=session.enrollment_secret,
        )

    @staticmethod
    def _secret_payload(session: RoleServiceSession) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "service_origin": session.service_origin,
                    "account_id": session.account_id,
                    "expires_at": session.expires_at,
                    "bearer_token": session.bearer_token,
                    "enrollment_id": session.enrollment_id,
                    "enrollment_secret": session.enrollment_secret,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()

    def save(self, session: RoleServiceSession) -> None:
        session = self._validate(session)
        protected = self.protector.protect(self._secret_payload(session))
        envelope = {
            "schema_version": 1,
            "protection": "windows-dpapi-current-user",
            "payload": base64.b64encode(protected).decode("ascii"),
        }
        payload = (json.dumps(envelope, sort_keys=True, indent=2) + "\n").encode()
        if len(payload) > _MAX_SESSION_BYTES:
            raise ValueError("protected role service session is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> RoleServiceSession | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("role service session path is unsafe")
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise ValueError("role service session is unreadable") from exc
        if not payload or len(payload) > _MAX_SESSION_BYTES:
            raise ValueError("role service session file is invalid")
        try:
            envelope = json.loads(payload.decode())
            if not isinstance(envelope, dict) or set(envelope) != {
                "schema_version",
                "protection",
                "payload",
            }:
                raise ValueError
            if envelope["schema_version"] != 1 or envelope["protection"] != (
                "windows-dpapi-current-user"
            ):
                raise ValueError
            protected = base64.b64decode(envelope["payload"], validate=True)
            raw = json.loads(self.protector.unprotect(protected).decode())
            if not isinstance(raw, dict) or set(raw) != {
                "schema_version",
                "service_origin",
                "account_id",
                "expires_at",
                "bearer_token",
                "enrollment_id",
                "enrollment_secret",
            }:
                raise ValueError
            if raw["schema_version"] != 1:
                raise ValueError
            if not all(
                isinstance(raw[key], str)
                for key in (
                    "service_origin",
                    "account_id",
                    "bearer_token",
                    "enrollment_id",
                    "enrollment_secret",
                )
            ):
                raise ValueError
            return self._validate(
                RoleServiceSession(
                    service_origin=raw["service_origin"],
                    account_id=raw["account_id"],
                    expires_at=raw["expires_at"],
                    bearer_token=raw["bearer_token"],
                    enrollment_id=raw["enrollment_id"],
                    enrollment_secret=raw["enrollment_secret"],
                )
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ValueError("role service session file is invalid") from exc

    def delete(self) -> None:
        if self.path.is_symlink():
            raise ValueError("role service session path is unsafe")
        self.path.unlink(missing_ok=True)
