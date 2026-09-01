"""Public-safe HTTPS client transport for the custom-role service.

This module intentionally contains no HTTP listener, worker, provider, credit,
or commerce implementation.  It is suitable for inclusion in the open-source
v0.2 client while the service-side application remains private operational
code.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from moeguard.cloud.role_service import (
    RoleServiceRequest,
    ServiceAssetRef,
    ServiceTaskSnapshot,
)

_JSON_MEDIA_TYPE = "application/json"
_ZIP_MEDIA_TYPE = "application/vnd.moeguard.result+zip"
_HTTP_USER_AGENT = "MoeGuard-Role-Service-Client/0.2"
_MAX_ARCHIVE_FILES = 2048
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_JSON_RESPONSE_BYTES = 1024 * 1024
_MAX_RESULT_RESPONSE_BYTES = 128 * 1024 * 1024
_MAX_ERROR_RESPONSE_BYTES = 64 * 1024
_DEFAULT_TRANSFER_TIMEOUT_SECONDS = 180.0
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

__all__ = (
    "HttpRoleServiceTransport",
    "RoleServiceAccountSummary",
    "RoleServiceEnrollment",
    "RoleServicePurchaseIntent",
    "RoleServiceConnectionError",
    "RoleServiceHttpError",
    "role_service_user_message",
)

_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_TOKEN_RE = re.compile(r"^mgr_[a-f0-9]{24}_[A-Za-z0-9_-]{32,128}$")


@dataclass(frozen=True)
class RoleServiceEnrollment:
    account_id: str
    bearer_token: str = field(repr=False)
    expires_at: int

    def transport(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transfer_timeout: float = _DEFAULT_TRANSFER_TIMEOUT_SECONDS,
    ) -> HttpRoleServiceTransport:
        return HttpRoleServiceTransport(
            base_url,
            self.bearer_token,
            timeout=timeout,
            transfer_timeout=transfer_timeout,
        )


@dataclass(frozen=True)
class RoleServiceAccountSummary:
    available_units: int
    reserved_units: int
    consumed_units: int
    available_t2i_units: int = 0
    available_i2v_units: int = 0
    available_flexible_units: int = 0


@dataclass(frozen=True)
class RoleServicePurchaseIntent:
    expires_at: int
    custom_order_id: str = field(repr=False)


class RoleServiceHttpError(RuntimeError):
    """Stable HTTP failure exposed to the desktop client."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


class RoleServiceConnectionError(RuntimeError):
    """Sanitized transport failure without host, token, or OS error details."""

    def __init__(self, code: str) -> None:
        if code not in {"service_timeout", "service_unreachable"}:
            raise ValueError("unknown role service connection error")
        self.code = code
        super().__init__(code)


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so an authenticated request never changes origin."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _urlopen_no_redirect(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(_RejectRedirectHandler()).open(
        request, timeout=timeout
    )


def _read_bounded(response: Any, limit: int) -> bytes:
    if limit <= 0:
        raise ValueError("role service response limit is invalid")
    get_header = getattr(response.headers, "get", None)
    content_length = get_header("Content-Length") if callable(get_header) else None
    if content_length is not None:
        try:
            declared = int(content_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("role service response length is invalid") from exc
        if declared < 0 or declared > limit:
            raise ValueError("role service response is too large")
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("role service response is too large")
    return payload


def role_service_user_message(error: object) -> str:
    """Return stable Chinese UX copy without echoing transport internals."""

    if isinstance(error, RoleServiceConnectionError):
        if error.code == "service_timeout":
            return "角色生成服务响应超时。请稍后恢复同一任务，不要重新提交。"
        return "暂时无法连接角色生成服务。请检查网络后恢复同一任务。"
    if isinstance(error, RoleServiceHttpError):
        if error.code == "invalid_enrollment":
            return "角色生成服务绑定信息已失效，请重新绑定。"
        if error.code == "enrollment_rate_limited":
            return "当前绑定请求较多，请稍后再试。"
        if error.status in {401, 403} or error.code == "invalid_token":
            return "角色生成服务连接已失效，请重新登录或绑定后再恢复任务。"
        if error.code == "insufficient_t2i_units":
            return "立绘生成次数不足，请兑换包含立绘生成次数的兑换码后恢复当前任务。"
        if error.code == "insufficient_i2v_units":
            return "动作生成次数不足，请兑换包含动作生成次数的兑换码后恢复当前任务。"
        if error.status == 402 or error.code == "insufficient_units":
            return "角色生成次数不足，请补充次数后恢复当前任务。"
        if error.code in {"invalid_credit_code", "credit_code_unavailable"}:
            return "兑换码无效、已使用或已停止发放，请检查后重试。"
        if error.code == "credit_campaign_limit_reached":
            return "当前账号已经领取过本次免费体验次数，不能重复兑换。"
        if error.code == "storage_quota_exceeded":
            return "角色素材暂存空间已满，请稍后重试或清理旧任务。"
        if error.status == 429 or error.code == "rate_limited":
            return "操作过于频繁，请稍后恢复同一任务。"
        if error.status >= 500:
            return "角色生成服务暂时不可用。请稍后恢复同一任务，不要重新提交。"
        return "角色生成服务拒绝了当前请求，请检查角色设定后重试。"
    return str(error)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _snapshot_from_dict(raw: Any) -> ServiceTaskSnapshot:
    if not isinstance(raw, dict) or set(raw) != {
        "remote_task_id",
        "spec_sha256",
        "status",
        "progress",
        "retryable",
        "error_code",
        "result_sha256",
    }:
        raise ValueError("HTTP service task snapshot schema is invalid")
    return ServiceTaskSnapshot(
        remote_task_id=str(raw["remote_task_id"]),
        spec_sha256=str(raw["spec_sha256"]),
        status=str(raw["status"]),
        progress=int(raw["progress"]),
        retryable=bool(raw["retryable"]),
        error_code=str(raw["error_code"]),
        result_sha256=str(raw["result_sha256"]),
    )


def _tree_digest(root: Path) -> str:
    digest = sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ValueError("result archive is empty")
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _extract_result_archive(payload: bytes, destination: Path) -> str:
    if destination.exists():
        raise ValueError("HTTP result destination must not already exist")
    staging = destination.with_name(destination.name + f".staging-{uuid.uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        total = 0
        names: set[str] = set()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_ARCHIVE_FILES:
                raise ValueError("HTTP result archive file count is invalid")
            for info in infos:
                if info.is_dir():
                    continue
                name = info.filename
                relative = PurePosixPath(name)
                mode = (info.external_attr >> 16) & 0xFFFF
                unsafe_part = any(
                    not part
                    or part in {".", ".."}
                    or "\x00" in part
                    or ":" in part
                    or part.endswith((" ", "."))
                    or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
                    for part in relative.parts
                )
                normalized_name = name.casefold()
                if (
                    not name
                    or "\\" in name
                    or relative.is_absolute()
                    or unsafe_part
                    or normalized_name in names
                    or (mode & 0o170000) == 0o120000
                ):
                    raise ValueError("HTTP result archive contains an unsafe path")
                names.add(normalized_name)
                total += info.file_size
                if total > _MAX_ARCHIVE_BYTES:
                    raise ValueError("HTTP result archive is too large")
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        digest = _tree_digest(staging)
        staging.replace(destination)
        return digest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


class HttpRoleServiceTransport:
    """Standard-library HTTPS client implementing the role-service transport."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        *,
        timeout: float = 30.0,
        transfer_timeout: float = _DEFAULT_TRANSFER_TIMEOUT_SECONDS,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("role service base URL must be absolute HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("role service base URL must not contain credentials or query")
        self.base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        if timeout <= 0 or transfer_timeout <= 0:
            raise ValueError("role service timeouts must be positive")
        self.timeout = float(timeout)
        self.transfer_timeout = float(transfer_timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        max_response_bytes: int = _MAX_JSON_RESPONSE_BYTES,
        timeout: float | None = None,
    ) -> tuple[bytes, Any]:
        request_headers = {"User-Agent": _HTTP_USER_AGENT}
        if authenticated:
            request_headers["Authorization"] = f"Bearer {self._bearer_token}"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            self.base_url + path,
            data=body if method != "GET" else None,
            headers=request_headers,
            method=method,
        )
        try:
            with _urlopen_no_redirect(
                request,
                timeout=self.timeout if timeout is None else timeout,
            ) as response:
                return _read_bounded(response, max_response_bytes), response.headers
        except urllib.error.HTTPError as exc:
            payload = exc.read(_MAX_ERROR_RESPONSE_BYTES + 1)
            if len(payload) > _MAX_ERROR_RESPONSE_BYTES:
                payload = b""
            try:
                raw = json.loads(payload.decode())
                error = raw["error"]
                code = str(error["code"])
                message = str(error["message"])
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
                code = "http_error"
                message = f"role service returned HTTP {exc.code}"
            raise RoleServiceHttpError(exc.code, code, message) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            code = "service_timeout" if isinstance(reason, TimeoutError) else "service_unreachable"
            raise RoleServiceConnectionError(code) from exc
        except OSError as exc:
            raise RoleServiceConnectionError("service_unreachable") from exc

    @staticmethod
    def _data(payload: bytes) -> Any:
        try:
            raw = json.loads(payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("role service returned invalid JSON") from exc
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "data"}:
            raise ValueError("role service JSON response schema is invalid")
        if raw["schema_version"] != 1:
            raise ValueError("role service JSON response version is unsupported")
        return raw["data"]

    @classmethod
    def enroll(
        cls,
        base_url: str,
        enrollment_id: str,
        enrollment_secret: str,
        *,
        timeout: float = 30.0,
    ) -> RoleServiceEnrollment:
        """Exchange an OS-protected anonymous enrollment for a service bearer."""

        client = cls(base_url, "", timeout=timeout)
        payload, _headers = client._request(
            "POST",
            "/v1/enrollments",
            body=_json_bytes(
                {
                    "schema_version": 1,
                    "enrollment_id": enrollment_id,
                    "enrollment_secret": enrollment_secret,
                }
            ),
            headers={"Content-Type": _JSON_MEDIA_TYPE},
            authenticated=False,
        )
        raw = cls._data(payload)
        if not isinstance(raw, dict) or set(raw) != {
            "account_id",
            "bearer_token",
            "expires_at",
        }:
            raise ValueError("role service enrollment response schema is invalid")
        account_id = str(raw["account_id"])
        bearer_token = str(raw["bearer_token"])
        expires_at = raw["expires_at"]
        if not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise ValueError("role service enrollment account is invalid")
        if not _TOKEN_RE.fullmatch(bearer_token):
            raise ValueError("role service enrollment token is invalid")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0:
            raise ValueError("role service enrollment expiry is invalid")
        return RoleServiceEnrollment(account_id, bearer_token, expires_at)

    def upload_asset(
        self,
        source: Path,
        *,
        purpose: str,
        media_type: str,
        expires_at: int,
    ) -> ServiceAssetRef:
        payload, _headers = self._request(
            "POST",
            "/v1/assets",
            body=Path(source).read_bytes(),
            headers={
                "Content-Type": media_type,
                "X-MoeGuard-Purpose": purpose,
                "X-MoeGuard-Expires-At": str(expires_at),
            },
            timeout=self.transfer_timeout,
        )
        return ServiceAssetRef.from_dict(self._data(payload))

    def account_summary(self) -> RoleServiceAccountSummary:
        payload, _headers = self._request("GET", "/v1/account")
        raw = self._data(payload)
        expected_fields = {
            "available_units",
            "reserved_units",
            "consumed_units",
            "available_t2i_units",
            "available_i2v_units",
            "available_flexible_units",
        }
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ValueError("role service account response schema is invalid")
        values = tuple(raw[key] for key in expected_fields)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ValueError("role service account balance is invalid")
        return RoleServiceAccountSummary(
            raw["available_units"],
            raw["reserved_units"],
            raw["consumed_units"],
            raw["available_t2i_units"],
            raw["available_i2v_units"],
            raw["available_flexible_units"],
        )

    def redeem_credit_code(self, code: str) -> RoleServiceAccountSummary:
        payload, _headers = self._request(
            "POST",
            "/v1/credits/redeem",
            body=_json_bytes({"schema_version": 1, "code": code}),
            headers={"Content-Type": _JSON_MEDIA_TYPE},
        )
        raw = self._data(payload)
        expected_fields = {
            "available_units",
            "reserved_units",
            "consumed_units",
            "available_t2i_units",
            "available_i2v_units",
            "available_flexible_units",
        }
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ValueError("role service credit redemption response is invalid")
        values = tuple(raw[key] for key in expected_fields)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ValueError("role service credit redemption balance is invalid")
        return RoleServiceAccountSummary(
            raw["available_units"],
            raw["reserved_units"],
            raw["consumed_units"],
            raw["available_t2i_units"],
            raw["available_i2v_units"],
            raw["available_flexible_units"],
        )

    def create_purchase_intent(self) -> RoleServicePurchaseIntent:
        payload, _headers = self._request(
            "POST",
            "/v1/commerce/purchase-intents",
            headers={"Content-Type": _JSON_MEDIA_TYPE},
        )
        raw = self._data(payload)
        if not isinstance(raw, dict) or set(raw) != {
            "custom_order_id",
            "expires_at",
        }:
            raise ValueError("role service purchase intent response schema is invalid")
        custom_order_id = raw["custom_order_id"]
        expires_at = raw["expires_at"]
        if (
            not isinstance(custom_order_id, str)
            or not 16 <= len(custom_order_id) <= 128
            or not re.fullmatch(r"[A-Za-z0-9_-]+", custom_order_id)
        ):
            raise ValueError("role service purchase intent is invalid")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0:
            raise ValueError("role service purchase intent expiry is invalid")
        return RoleServicePurchaseIntent(expires_at, custom_order_id)

    def submit(
        self, request: RoleServiceRequest, *, idempotency_sha256: str
    ) -> ServiceTaskSnapshot:
        payload, _headers = self._request(
            "POST",
            "/v1/tasks",
            body=_json_bytes({"schema_version": 1, "request": request.to_dict()}),
            headers={
                "Content-Type": _JSON_MEDIA_TYPE,
                "Idempotency-Key": idempotency_sha256,
            },
        )
        return _snapshot_from_dict(self._data(payload))

    def query(self, remote_task_id: str) -> ServiceTaskSnapshot:
        quoted = urllib.parse.quote(remote_task_id, safe="")
        payload, _headers = self._request("GET", f"/v1/tasks/{quoted}")
        return _snapshot_from_dict(self._data(payload))

    def cancel(self, remote_task_id: str) -> ServiceTaskSnapshot:
        quoted = urllib.parse.quote(remote_task_id, safe="")
        payload, _headers = self._request("POST", f"/v1/tasks/{quoted}/cancel")
        return _snapshot_from_dict(self._data(payload))

    def download_result(self, remote_task_id: str, destination: Path) -> str:
        quoted = urllib.parse.quote(remote_task_id, safe="")
        payload, headers = self._request(
            "GET",
            f"/v1/tasks/{quoted}/result",
            max_response_bytes=_MAX_RESULT_RESPONSE_BYTES,
            timeout=self.transfer_timeout,
        )
        if headers.get_content_type() != _ZIP_MEDIA_TYPE:
            raise ValueError("role service result media type is invalid")
        expected = headers.get("X-MoeGuard-Tree-SHA256", "")
        digest = _extract_result_archive(payload, Path(destination))
        if digest != expected:
            shutil.rmtree(destination, ignore_errors=True)
            raise ValueError("role service result archive hash changed in transit")
        return digest
