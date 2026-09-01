"""Public-safe role-service client configuration coverage."""

from __future__ import annotations

import json
import struct
import threading
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import moeguard.cloud.role_service_http_client as http_client
from moeguard.cloud.role_service import validate_role_service_image
from moeguard.cloud.role_service_bootstrap import (
    role_service_origin_from_environment,
    role_service_origin_from_file,
    role_service_transport_from_environment,
)
from moeguard.cloud.role_service_http_client import (
    HttpRoleServiceTransport,
    RoleServiceConnectionError,
    RoleServiceHttpError,
    role_service_user_message,
)
from moeguard.roles import ContractErrorCode, RoleContractError


def _token() -> str:
    return "mgr_" + "a" * 24 + "_" + "B" * 43


def test_public_role_service_client_is_disabled_by_default() -> None:
    assert role_service_transport_from_environment({}) is None
    assert role_service_origin_from_environment({}) is None


def test_public_enrollment_origin_does_not_require_a_developer_token() -> None:
    assert (
        role_service_origin_from_environment(
            {
                "MOEGUARD_ROLE_SERVICE_MODE": "https",
                "MOEGUARD_ROLE_SERVICE_URL": "https://roles.example/",
            }
        )
        == "https://roles.example"
    )


def test_public_release_endpoint_config_is_optional_and_cloud_neutral(
    tmp_path: Path,
) -> None:
    path = tmp_path / "role-service.json"
    assert role_service_origin_from_file(path) is None
    path.write_text(
        '{"schema_version":1,"service_origin":"https://roles.example/"}\n',
        encoding="utf-8",
    )

    assert role_service_origin_from_file(path) == "https://roles.example"


def test_preview_tracks_public_service_origin_and_role_enabled_entrypoints() -> None:
    root = Path(__file__).resolve().parents[1]

    assert role_service_origin_from_file(root / "resources" / "role-service.json") == (
        "https://api.moeproject.net"
    )
    assert 'moeguard = "moeguard.role_main:main"' in (root / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "python -m moeguard.role_main" in (root / "start_moeguard.bat").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"schema_version":2,"service_origin":"https://roles.example"}',
        '{"schema_version":1,"service_origin":"http://roles.example"}',
        '{"schema_version":1,"service_origin":"https://user:secret@roles.example"}',
    ],
)
def test_public_release_endpoint_config_fails_closed(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "role-service.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError):
        role_service_origin_from_file(path)


@pytest.mark.parametrize(
    "values",
    [
        {"MOEGUARD_ROLE_SERVICE_URL": "https://roles.example"},
        {"MOEGUARD_ROLE_SERVICE_TOKEN_FILE": "C:/private/token"},
        {
            "MOEGUARD_ROLE_SERVICE_MODE": "unexpected",
            "MOEGUARD_ROLE_SERVICE_URL": "https://roles.example",
            "MOEGUARD_ROLE_SERVICE_TOKEN_FILE": "C:/private/token",
        },
    ],
)
def test_public_role_service_client_requires_explicit_https_mode(
    values: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        role_service_transport_from_environment(values)


def test_public_role_service_client_reads_token_file_without_disclosure(
    tmp_path: Path,
) -> None:
    secret = _token()
    token_file = tmp_path / "private-role-service.token"
    token_file.write_text(secret + "\n", encoding="ascii")

    transport = role_service_transport_from_environment(
        {
            "MOEGUARD_ROLE_SERVICE_MODE": "https",
            "MOEGUARD_ROLE_SERVICE_URL": "https://roles.example/",
            "MOEGUARD_ROLE_SERVICE_TOKEN_FILE": str(token_file.resolve()),
        }
    )

    assert transport is not None
    assert transport.base_url == "https://roles.example"
    assert transport._bearer_token == secret
    assert secret not in repr(transport)
    assert str(token_file) not in repr(transport)


@pytest.mark.parametrize(
    "url",
    [
        "http://roles.example",
        "https://user:password@roles.example",
        "https://roles.example/private",
        "https://roles.example?token=value",
    ],
)
def test_public_role_service_client_rejects_unsafe_urls(tmp_path: Path, url: str) -> None:
    token_file = tmp_path / "role-service.token"
    token_file.write_text(_token(), encoding="ascii")
    with pytest.raises(ValueError) as blocked:
        role_service_transport_from_environment(
            {
                "MOEGUARD_ROLE_SERVICE_MODE": "https",
                "MOEGUARD_ROLE_SERVICE_URL": url,
                "MOEGUARD_ROLE_SERVICE_TOKEN_FILE": str(token_file.resolve()),
            }
        )
    assert _token() not in str(blocked.value)
    assert str(token_file) not in str(blocked.value)


def test_public_role_service_client_sanitizes_network_failure(monkeypatch) -> None:
    secret = _token()
    transport = HttpRoleServiceTransport("https://private-role-service.example", secret)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("private-hostname-and-os-details")

    monkeypatch.setattr(http_client, "_urlopen_no_redirect", fail)
    with pytest.raises(RoleServiceConnectionError) as blocked:
        transport.query("task-1")
    assert blocked.value.code == "service_unreachable"
    assert secret not in str(blocked.value)
    assert "private-hostname" not in str(blocked.value)


def test_public_role_service_client_uses_explicit_cloudflare_safe_user_agent(
    monkeypatch,
) -> None:
    captured = None

    def respond(request, *, timeout: float):
        nonlocal captured
        captured = request
        assert timeout == 30.0
        return _JsonResponse(
            {
                "available_units": 0,
                "reserved_units": 0,
                "consumed_units": 0,
                "available_t2i_units": 0,
                "available_i2v_units": 0,
                "available_flexible_units": 0,
            }
        )

    monkeypatch.setattr(http_client, "_urlopen_no_redirect", respond)
    HttpRoleServiceTransport("https://roles.example", _token()).account_summary()

    assert captured is not None
    assert captured.get_header("User-agent") == "MoeGuard-Role-Service-Client/0.2"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RoleServiceHttpError(401, "invalid_token", "private server detail"),
            "连接已失效",
        ),
        (
            RoleServiceHttpError(402, "insufficient_units", "private server detail"),
            "生成次数不足",
        ),
        (
            RoleServiceHttpError(402, "insufficient_t2i_units", "private server detail"),
            "兑换包含立绘生成次数的兑换码",
        ),
        (
            RoleServiceHttpError(402, "insufficient_i2v_units", "private server detail"),
            "兑换包含动作生成次数的兑换码",
        ),
        (
            RoleServiceHttpError(429, "rate_limited", "private server detail"),
            "操作过于频繁",
        ),
        (
            RoleServiceHttpError(401, "invalid_enrollment", "private server detail"),
            "绑定信息已失效",
        ),
        (
            RoleServiceHttpError(429, "enrollment_rate_limited", "private server detail"),
            "绑定请求较多",
        ),
        (
            RoleServiceHttpError(409, "credit_code_unavailable", "private server detail"),
            "兑换码无效",
        ),
        (
            RoleServiceHttpError(409, "credit_campaign_limit_reached", "private server detail"),
            "已经领取过本次免费体验次数",
        ),
        (
            RoleServiceConnectionError("service_timeout"),
            "响应超时",
        ),
    ],
)
def test_public_role_service_errors_have_stable_user_copy(error: Exception, expected: str) -> None:
    message = role_service_user_message(error)
    assert expected in message
    assert "private server detail" not in message
    assert "T2I" not in message
    assert "I2V" not in message


class _JsonResponse:
    def __init__(self, data: dict) -> None:
        self._payload = json.dumps(
            {"schema_version": 1, "data": data}, separators=(",", ":")
        ).encode()
        self.headers = object()

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._payload if limit < 0 else self._payload[:limit]


def test_asset_transfers_use_long_timeout_while_metadata_stays_short(
    tmp_path: Path, monkeypatch
) -> None:
    observed: list[tuple[str, float]] = []
    source = tmp_path / "identity.png"
    source.write_bytes(b"x")

    def respond(request, *, timeout: float):
        observed.append((request.full_url, timeout))
        if request.full_url.endswith("/v1/assets"):
            return _JsonResponse(
                {
                    "sha256": "a" * 64,
                    "media_type": "image/png",
                    "purpose": "identity_image",
                    "size_bytes": 1,
                    "expires_at": 2_000_000_000,
                }
            )
        return _JsonResponse(
            {
                "available_units": 0,
                "reserved_units": 0,
                "consumed_units": 0,
                "available_t2i_units": 0,
                "available_i2v_units": 0,
                "available_flexible_units": 0,
            }
        )

    monkeypatch.setattr(http_client, "_urlopen_no_redirect", respond)
    transport = HttpRoleServiceTransport(
        "https://roles.example",
        _token(),
        timeout=30,
        transfer_timeout=180,
    )
    transport.upload_asset(
        source,
        purpose="identity_image",
        media_type="image/png",
        expires_at=2_000_000_000,
    )
    transport.account_summary()

    assert observed == [
        ("https://roles.example/v1/assets", 180.0),
        ("https://roles.example/v1/account", 30.0),
    ]


def test_result_download_uses_long_transfer_timeout(tmp_path: Path, monkeypatch) -> None:
    transport = HttpRoleServiceTransport(
        "https://roles.example",
        _token(),
        timeout=30,
        transfer_timeout=180,
    )

    def capture(_method: str, _path: str, **kwargs: object):
        assert kwargs["timeout"] == 180.0
        raise RuntimeError("captured")

    monkeypatch.setattr(transport, "_request", capture)
    with pytest.raises(RuntimeError, match="captured"):
        transport.download_result("task-1", tmp_path / "result")


def test_public_client_reads_balance_and_creates_opaque_purchase_intent(
    monkeypatch,
) -> None:
    paths: list[str] = []

    def respond(request, *, timeout: float):
        paths.append(request.full_url)
        assert timeout == 30.0
        if request.full_url.endswith("/v1/account"):
            return _JsonResponse(
                {
                    "available_units": 4,
                    "reserved_units": 1,
                    "consumed_units": 7,
                    "available_t2i_units": 1,
                    "available_i2v_units": 2,
                    "available_flexible_units": 1,
                }
            )
        return _JsonResponse(
            {
                "custom_order_id": "purchase_" + "a" * 32,
                "expires_at": 2_000_000_000,
            }
        )

    monkeypatch.setattr(http_client, "_urlopen_no_redirect", respond)
    transport = HttpRoleServiceTransport("https://roles.example", _token())

    balance = transport.account_summary()
    intent = transport.create_purchase_intent()

    assert balance.available_units == 4
    assert balance.reserved_units == 1
    assert balance.consumed_units == 7
    assert balance.available_t2i_units == 1
    assert balance.available_i2v_units == 2
    assert balance.available_flexible_units == 1
    assert intent.custom_order_id not in repr(intent)
    assert paths == [
        "https://roles.example/v1/account",
        "https://roles.example/v1/commerce/purchase-intents",
    ]


def test_authenticated_client_rejects_redirects_before_forwarding_bearer() -> None:
    request = urllib.request.Request(
        "https://roles.example/v1/account",
        headers={"Authorization": "Bearer " + _token()},
    )
    redirected = http_client._RejectRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://other.example/collect",
    )

    assert redirected is None


def test_cross_origin_redirect_never_reaches_second_server_with_bearer() -> None:
    received_authorization: list[str | None] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received_authorization.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return None

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{sink.server_port}/collect",
            )
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return None

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (sink, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        transport = HttpRoleServiceTransport(
            f"http://127.0.0.1:{redirect.server_port}", _token()
        )
        with pytest.raises(RoleServiceHttpError) as blocked:
            transport.query("task-1")
        assert blocked.value.status == 302
        assert received_authorization == []
    finally:
        redirect.shutdown()
        sink.shutdown()
        redirect.server_close()
        sink.server_close()
        for thread in threads:
            thread.join(timeout=2)


class _OversizedResponse:
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    @staticmethod
    def read(limit: int = -1) -> bytes:
        payload = b"x" * (1024 * 1024 + 1)
        return payload if limit < 0 else payload[:limit]


def test_public_client_rejects_oversized_json_before_parsing(monkeypatch) -> None:
    monkeypatch.setattr(
        http_client,
        "_urlopen_no_redirect",
        lambda *_args, **_kwargs: _OversizedResponse(),
    )

    with pytest.raises(ValueError, match="response is too large"):
        HttpRoleServiceTransport("https://roles.example", _token()).account_summary()


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def test_candidate_image_rejects_decompression_scale_dimensions(tmp_path: Path) -> None:
    width = height = 8192
    raw = (b"\x00" + b"\x00" * (width // 8)) * height
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + _png_chunk(b"IEND", b"")
    )
    candidate = tmp_path / "oversized.png"
    candidate.write_bytes(payload)

    with pytest.raises(RoleContractError) as blocked:
        validate_role_service_image(candidate, "image/png")

    assert blocked.value.code == ContractErrorCode.RESOURCE_LIMIT
