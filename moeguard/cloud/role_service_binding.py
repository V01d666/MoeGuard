"""Recoverable anonymous binding for the public role-service client."""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from moeguard.cloud.role_service_bootstrap import (
    https_role_service_origin,
    new_client_enrollment_credentials,
)
from moeguard.cloud.role_service_http_client import (
    HttpRoleServiceTransport,
    RoleServiceEnrollment,
)
from moeguard.cloud.role_service_session import (
    CredentialProtector,
    RoleServiceSession,
    RoleServiceSessionStore,
    WindowsDpapiProtector,
)

_MAX_BINDING_BYTES = 64 * 1024
_ENROLLMENT_PREFIX = "eni_"
_ENROLLMENT_SECRET_PREFIX = "ens_"


@dataclass(frozen=True)
class RoleServiceEnrollmentIdentity:
    service_origin: str
    enrollment_id: str
    enrollment_secret: str = field(repr=False)


class RoleServiceEnrollmentStore:
    """Persist the retry identity before the first network request."""

    def __init__(self, path: Path, protector: CredentialProtector | None = None) -> None:
        self.path = Path(path)
        self.protector = protector or WindowsDpapiProtector()

    @staticmethod
    def _validate(identity: RoleServiceEnrollmentIdentity) -> RoleServiceEnrollmentIdentity:
        origin = https_role_service_origin(identity.service_origin)
        if (
            not identity.enrollment_id.startswith(_ENROLLMENT_PREFIX)
            or len(identity.enrollment_id) != 36
            or any(
                character not in "0123456789abcdef"
                for character in identity.enrollment_id[len(_ENROLLMENT_PREFIX) :]
            )
        ):
            raise ValueError("role service enrollment ID is invalid")
        secret = identity.enrollment_secret
        if (
            not secret.startswith(_ENROLLMENT_SECRET_PREFIX)
            or not 47 <= len(secret) <= 132
            or any(
                not (character.isascii() and (character.isalnum() or character in "_-"))
                for character in secret[len(_ENROLLMENT_SECRET_PREFIX) :]
            )
        ):
            raise ValueError("role service enrollment secret is invalid")
        return RoleServiceEnrollmentIdentity(origin, identity.enrollment_id, secret)

    def save(self, identity: RoleServiceEnrollmentIdentity) -> None:
        identity = self._validate(identity)
        raw = (
            json.dumps(
                {
                    "schema_version": 1,
                    "service_origin": identity.service_origin,
                    "enrollment_id": identity.enrollment_id,
                    "enrollment_secret": identity.enrollment_secret,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        protected = self.protector.protect(raw)
        envelope = {
            "schema_version": 1,
            "protection": "windows-dpapi-current-user",
            "payload": base64.b64encode(protected).decode("ascii"),
        }
        payload = (json.dumps(envelope, sort_keys=True, indent=2) + "\n").encode()
        if len(payload) > _MAX_BINDING_BYTES:
            raise ValueError("protected role service enrollment is too large")
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

    def load(self) -> RoleServiceEnrollmentIdentity | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("role service enrollment path is unsafe")
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise ValueError("role service enrollment is unreadable") from exc
        if not payload or len(payload) > _MAX_BINDING_BYTES:
            raise ValueError("role service enrollment file is invalid")
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
                "enrollment_id",
                "enrollment_secret",
            }:
                raise ValueError
            if raw["schema_version"] != 1 or not all(
                isinstance(raw[key], str)
                for key in (
                    "service_origin",
                    "enrollment_id",
                    "enrollment_secret",
                )
            ):
                raise ValueError
            return self._validate(
                RoleServiceEnrollmentIdentity(
                    raw["service_origin"],
                    raw["enrollment_id"],
                    raw["enrollment_secret"],
                )
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("role service enrollment file is invalid") from exc

    def delete(self) -> None:
        if self.path.is_symlink():
            raise ValueError("role service enrollment path is unsafe")
        self.path.unlink(missing_ok=True)


class RoleServiceBindingManager:
    """Create, recover and remove one anonymous role-service binding."""

    def __init__(
        self,
        session_store: RoleServiceSessionStore,
        enrollment_store: RoleServiceEnrollmentStore,
        *,
        enroll: Callable[[str, str, str], RoleServiceEnrollment] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session_store = session_store
        self.enrollment_store = enrollment_store
        self._enroll = enroll or HttpRoleServiceTransport.enroll
        self._clock = clock

    def current_session(self) -> RoleServiceSession | None:
        session = self.session_store.load()
        if session is None or session.expires_at <= int(self._clock()):
            return None
        return session

    def bind(self, service_origin: str) -> RoleServiceSession:
        origin = https_role_service_origin(service_origin)
        try:
            session = self.current_session()
        except (OSError, ValueError):
            self.session_store.delete()
            session = None
        if session is not None and session.service_origin == origin:
            return session

        identity = self.enrollment_store.load()
        if identity is None or identity.service_origin != origin:
            credentials = new_client_enrollment_credentials()
            identity = RoleServiceEnrollmentIdentity(
                origin,
                credentials.enrollment_id,
                credentials.enrollment_secret,
            )
            self.enrollment_store.save(identity)

        enrollment = self._enroll(
            origin,
            identity.enrollment_id,
            identity.enrollment_secret,
        )
        session = RoleServiceSession(
            service_origin=origin,
            account_id=enrollment.account_id,
            expires_at=enrollment.expires_at,
            bearer_token=enrollment.bearer_token,
            enrollment_id=identity.enrollment_id,
            enrollment_secret=identity.enrollment_secret,
        )
        self.session_store.save(session)
        return session

    def unbind(self) -> None:
        self.session_store.delete()
        self.enrollment_store.delete()
