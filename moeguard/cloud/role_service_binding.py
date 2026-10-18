"""Recoverable anonymous binding for the public role-service client."""

from __future__ import annotations

import base64
import json
import os
import threading
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
    RoleServiceHttpError,
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
    """Create and silently renew this device's one anonymous binding.

    The support ID is the server account, and the server maps one enrollment
    identity to one account forever, so the identity is never deleted here:
    a token that expires or is rejected is renewed with the same identity and
    the user keeps their ID and their remaining generations.
    """

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
        self._binding_lock = threading.RLock()
        # Written by v0.3.0's "disconnect" button, which v0.4.0 removed.  A
        # stale marker must not keep a user offline, so it is only cleared.
        self._legacy_disconnect_marker = session_store.path.with_name(
            "automatic-connection-disabled"
        )

    def current_session(self) -> RoleServiceSession | None:
        session = self.session_store.load()
        if session is None or session.expires_at <= int(self._clock()):
            return None
        return session

    def bind(self, service_origin: str) -> RoleServiceSession:
        """Return a usable session, enrolling only when none is stored.

        A stored, unexpired session costs no network request.
        """
        with self._binding_lock:
            session = self._bind(service_origin)
            if not self._legacy_disconnect_marker.is_symlink():
                self._legacy_disconnect_marker.unlink(missing_ok=True)
            return session

    def renew(self, service_origin: str, rejected_token: str) -> RoleServiceSession:
        """Replace a token the service rejected, keeping the same identity.

        Concurrent callers that hit the same rejection renew once: whoever
        holds the lock second finds a different token already stored.
        """
        with self._binding_lock:
            try:
                stored = self.current_session()
            except (OSError, ValueError):
                stored = None
            if stored is not None and stored.bearer_token != rejected_token:
                return stored
            # Secure the identity before dropping the session that carries it.
            self._recovery_identity(https_role_service_origin(service_origin))
            self.session_store.delete()
            return self.bind(service_origin)

    def _recovery_identity(self, origin: str) -> RoleServiceEnrollmentIdentity | None:
        identity = self.enrollment_store.load()
        if identity is not None and identity.service_origin == origin:
            return identity
        # The session file carries the same identity.  Recovering it keeps
        # the support ID if only the enrollment file was lost.
        try:
            stored = self.session_store.load()
        except (OSError, ValueError):
            return None
        if stored is None or stored.service_origin != origin:
            return None
        identity = RoleServiceEnrollmentIdentity(
            origin, stored.enrollment_id, stored.enrollment_secret
        )
        self.enrollment_store.save(identity)
        return identity

    def _bind(self, service_origin: str) -> RoleServiceSession:
        origin = https_role_service_origin(service_origin)
        try:
            session = self.current_session()
        except (OSError, ValueError):
            self.session_store.delete()
            session = None
        if session is not None and session.service_origin == origin:
            return session

        identity = self._recovery_identity(origin)
        if identity is None:
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


def _is_rejected_token(error: Exception) -> bool:
    return isinstance(error, RoleServiceHttpError) and error.code == "invalid_token"


class SessionTransport:
    """The role-service transport for this device, connecting on demand.

    Every call obtains the device session first (no network while a stored
    session is valid) and, if the service rejects the token, renews it once
    with the same identity and repeats the call.  Authentication is checked
    before any request is processed, so the repeat cannot double a side
    effect.  This is what lets the workshop generate without a separate
    "connect" step and survive a token expiring while the app is open.
    """

    def __init__(
        self,
        manager: RoleServiceBindingManager,
        service_origin: str,
        *,
        timeout: float = 30.0,
        on_session: Callable[[RoleServiceSession], None] | None = None,
    ) -> None:
        self.manager = manager
        self.service_origin = https_role_service_origin(service_origin)
        self.timeout = timeout
        self._on_session = on_session

    def session(self) -> RoleServiceSession:
        session = self.manager.bind(self.service_origin)
        if self._on_session is not None:
            self._on_session(session)
        return session

    def _call(self, name: str, *args: object, **kwargs: object) -> object:
        session = self.session()
        try:
            return getattr(session.transport(timeout=self.timeout), name)(*args, **kwargs)
        except RoleServiceHttpError as error:
            if not _is_rejected_token(error):
                raise
        renewed = self.manager.renew(self.service_origin, session.bearer_token)
        if self._on_session is not None:
            self._on_session(renewed)
        return getattr(renewed.transport(timeout=self.timeout), name)(*args, **kwargs)

    def announcements(self):
        return HttpRoleServiceTransport(
            self.service_origin, "", timeout=self.timeout
        ).announcements()

    def upload_asset(self, source, **kwargs):
        return self._call("upload_asset", source, **kwargs)

    def preflight(self, *, timeout: float) -> None:
        self._call("preflight", timeout=timeout)

    def account_summary(self):
        return self._call("account_summary")

    def redeem_credit_code(self, code: str):
        return self._call("redeem_credit_code", code)

    def submit_client_events(self, events):
        return self._call("submit_client_events", events)

    def mailbox_thread(self):
        return self._call("mailbox_thread")

    def post_mailbox_message(self, message):
        return self._call("post_mailbox_message", message)

    def create_purchase_intent(self):
        return self._call("create_purchase_intent")

    def submit(self, request, *, idempotency_sha256: str):
        return self._call("submit", request, idempotency_sha256=idempotency_sha256)

    def query(self, remote_task_id: str):
        return self._call("query", remote_task_id)

    def cancel(self, remote_task_id: str):
        return self._call("cancel", remote_task_id)

    def download_result(self, remote_task_id: str, destination: Path) -> str:
        return self._call("download_result", remote_task_id, destination)
