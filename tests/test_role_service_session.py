from __future__ import annotations

import sys
from pathlib import Path

import pytest

from moeguard.cloud.role_service_session import (
    RoleServiceSession,
    RoleServiceSessionStore,
    WindowsDpapiProtector,
)


class _TestProtector:
    def protect(self, value: bytes) -> bytes:
        return b"test-v1:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"test-v1:"):
            raise ValueError("invalid test ciphertext")
        return value.removeprefix(b"test-v1:")[::-1]


def _session() -> RoleServiceSession:
    return RoleServiceSession(
        service_origin="https://roles.example/",
        account_id="client-" + "a" * 32,
        expires_at=2_000_000_000,
        bearer_token="mgr_" + "b" * 24 + "_" + "C" * 43,
        enrollment_id="eni_" + "d" * 32,
        enrollment_secret="ens_" + "E" * 43,
    )


def test_session_store_round_trip_keeps_all_credentials_out_of_plaintext(
    tmp_path: Path,
) -> None:
    path = tmp_path / "role-service-session.json"
    store = RoleServiceSessionStore(path, _TestProtector())

    store.save(_session())
    loaded = store.load()

    assert loaded is not None
    assert loaded.service_origin == "https://roles.example"
    assert loaded.account_id == _session().account_id
    assert loaded.expires_at == _session().expires_at
    assert loaded.bearer_token == _session().bearer_token
    assert loaded.enrollment_id == _session().enrollment_id
    assert loaded.enrollment_secret == _session().enrollment_secret
    assert loaded.bearer_token not in repr(loaded)
    payload = path.read_bytes()
    assert loaded.bearer_token.encode() not in payload
    assert loaded.enrollment_id.encode() not in payload
    assert loaded.enrollment_secret.encode() not in payload


def test_session_store_rejects_unsafe_origin_and_corrupt_ciphertext(
    tmp_path: Path,
) -> None:
    path = tmp_path / "role-service-session.json"
    store = RoleServiceSessionStore(path, _TestProtector())

    with pytest.raises(ValueError, match="origin"):
        store.save(
            RoleServiceSession(
                **{
                    **_session().__dict__,
                    "service_origin": "https://user:secret@roles.example/private",
                }
            )
        )

    path.write_text(
        '{"schema_version":1,"protection":"windows-dpapi-current-user",'
        '"payload":"bm90LWNpcGhlcnRleHQ="}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid"):
        store.load()


def test_session_store_delete_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "role-service-session.json"
    store = RoleServiceSessionStore(path, _TestProtector())
    store.save(_session())

    store.delete()
    store.delete()

    assert store.load() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI only")
def test_windows_dpapi_round_trip_binds_ciphertext_to_current_user() -> None:
    protector = WindowsDpapiProtector()
    secret = b"moeguard-dpapi-session-test"

    try:
        protected = protector.protect(secret)
    except OSError as exc:
        if exc.errno == 2:
            pytest.skip("current process has no loaded Windows user profile")
        raise

    assert protected != secret
    assert secret not in protected
    assert protector.unprotect(protected) == secret
