from __future__ import annotations

from pathlib import Path

import pytest

from moeguard.cloud.role_service_binding import (
    RoleServiceBindingManager,
    RoleServiceEnrollmentIdentity,
    RoleServiceEnrollmentStore,
)
from moeguard.cloud.role_service_http_client import RoleServiceEnrollment
from moeguard.cloud.role_service_session import RoleServiceSessionStore


class _TestProtector:
    def protect(self, value: bytes) -> bytes:
        return b"test-binding:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"test-binding:"):
            raise ValueError("invalid test ciphertext")
        return value.removeprefix(b"test-binding:")[::-1]


def _manager(
    tmp_path: Path,
    enroll,
) -> tuple[RoleServiceBindingManager, RoleServiceEnrollmentStore, RoleServiceSessionStore]:
    protector = _TestProtector()
    enrollment_store = RoleServiceEnrollmentStore(
        tmp_path / "enrollment.json", protector
    )
    session_store = RoleServiceSessionStore(tmp_path / "session.json", protector)
    return (
        RoleServiceBindingManager(
            session_store,
            enrollment_store,
            enroll=enroll,
            clock=lambda: 1_900_000_000,
        ),
        enrollment_store,
        session_store,
    )


def _enrollment() -> RoleServiceEnrollment:
    return RoleServiceEnrollment(
        account_id="client-" + "a" * 32,
        bearer_token="mgr_" + "b" * 24 + "_" + "C" * 43,
        expires_at=2_000_000_000,
    )


def test_enrollment_store_round_trip_hides_retry_credentials(tmp_path: Path) -> None:
    path = tmp_path / "enrollment.json"
    store = RoleServiceEnrollmentStore(path, _TestProtector())
    identity = RoleServiceEnrollmentIdentity(
        "https://roles.example/",
        "eni_" + "d" * 32,
        "ens_" + "E" * 43,
    )

    store.save(identity)
    loaded = store.load()

    assert loaded is not None
    assert loaded.service_origin == "https://roles.example"
    assert loaded.enrollment_id == identity.enrollment_id
    assert loaded.enrollment_secret == identity.enrollment_secret
    assert identity.enrollment_secret not in repr(loaded)
    assert identity.enrollment_id.encode() not in path.read_bytes()
    assert identity.enrollment_secret.encode() not in path.read_bytes()


def test_binding_persists_identity_before_network_and_reuses_it_after_failure(
    tmp_path: Path,
) -> None:
    attempts: list[tuple[str, str, str]] = []

    def enroll(origin: str, enrollment_id: str, enrollment_secret: str):
        attempts.append((origin, enrollment_id, enrollment_secret))
        if len(attempts) == 1:
            raise TimeoutError("simulated uncertain response")
        return _enrollment()

    manager, enrollment_store, session_store = _manager(tmp_path, enroll)

    with pytest.raises(TimeoutError):
        manager.bind("https://roles.example")
    persisted = enrollment_store.load()
    assert persisted is not None
    assert session_store.load() is None

    session = manager.bind("https://roles.example")

    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert session.enrollment_id == persisted.enrollment_id
    assert manager.current_session() == session


def test_binding_reuses_active_session_without_network(tmp_path: Path) -> None:
    calls = 0

    def enroll(_origin: str, _enrollment_id: str, _enrollment_secret: str):
        nonlocal calls
        calls += 1
        return _enrollment()

    manager, _enrollment_store, _session_store = _manager(tmp_path, enroll)

    first = manager.bind("https://roles.example")
    second = manager.bind("https://roles.example/")

    assert first == second
    assert calls == 1


def test_binding_unbind_removes_session_and_recovery_identity(tmp_path: Path) -> None:
    manager, enrollment_store, session_store = _manager(
        tmp_path,
        lambda *_args: _enrollment(),
    )
    manager.bind("https://roles.example")

    manager.unbind()
    manager.unbind()

    assert enrollment_store.load() is None
    assert session_store.load() is None


def test_explicit_rebind_discards_only_a_corrupt_short_lived_session(
    tmp_path: Path,
) -> None:
    manager, enrollment_store, session_store = _manager(
        tmp_path,
        lambda *_args: _enrollment(),
    )
    identity = RoleServiceEnrollmentIdentity(
        "https://roles.example",
        "eni_" + "d" * 32,
        "ens_" + "E" * 43,
    )
    enrollment_store.save(identity)
    session_store.path.write_text("corrupt", encoding="utf-8")

    session = manager.bind("https://roles.example")

    assert session.enrollment_id == identity.enrollment_id
    assert enrollment_store.load() == identity
    assert session_store.load() == session
