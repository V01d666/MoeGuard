"""Provider-neutral custom-role workbench for the v0.2 open client.

The UI owns editable drafts, recoverable task journals, previews, saving and
installation.  Production generation crosses only the injected role-service
HTTPS contract; direct provider access lives in an Internal-only module.
"""

from __future__ import annotations

import json
import platform
import random
import re
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from PIL import Image
from PySide6.QtCore import QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QMovie, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from moeguard import __version__
from moeguard.cloud.role_service import (
    ClientRuntimeInfo,
    RoleServiceBindingStore,
    RoleServiceClient,
    RoleServiceRequest,
    RoleServiceRequestStore,
    RoleServiceTransport,
)
from moeguard.cloud.role_service_http_client import (
    RoleServiceAccountSummary,
    role_service_user_message,
)
from moeguard.roles import (
    ActionRevision,
    InstalledRole,
    PackageKey,
    RoleLibrary,
    RoleTaskArtifactStore,
    RoleTaskSpec,
    RoleTaskStore,
    build_package_revision,
)
from moeguard.roles.drafts import RoleDraft, RoleDraftStore, normalized_identity_png
from moeguard.roles.errors import RoleContractError
from moeguard.roles.package import RolePackage, load_role_package
from moeguard.roles.profile import CharacterProfile, ProfileInput, VisualIdentity
from moeguard.roles.spec import (
    HEAD_BODY_RATIO_BY_SILHOUETTE,
    LOOP_ACTIONS,
    OFFICIAL_ACTIONS,
    half_head_reveal_fraction,
)
from moeguard.ui import theme
from moeguard.utils.paths import resource_path

_ACTIVITY_FACES = (
    "ฅ( ̳• ·̫ • ̳ฅ)",
    "(｡•̀ᴗ-)✧",
    "(๑•̀ㅂ•́)و✧",
    "૮₍ ˶ᵔ ᵕ ᵔ˶ ₎ა",
    "(づ｡◕‿‿◕｡)づ",
)

ProgressCallback = Callable[[str, int], None]
_ROLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,47}$")
_PRESENTATION_LABELS = (
    ("男性", "masculine"),
    ("女性", "feminine"),
    ("中性", "neutral"),
)
_SILHOUETTE_LABELS = (
    ("二头身", "super_chibi"),
    ("三头身", "chibi"),
    ("四头身", "petite"),
)
_ACTION_LABELS = {
    "idle": "待机",
    "notice": "警觉注意",
    "click_reaction": "点击反馈",
    "dragging": "拖动状态",
    "patrol": "值守巡查",
    "welcome": "主人返回",
    "peek_left": "向左探头",
    "peek_right": "向右探头",
    "sit_down": "底边坐下",
}
_WORKBENCH_ACTIONS = tuple(
    action for action in OFFICIAL_ACTIONS if action != "sit_down"
)
_TEXT_DETAIL_LIMIT = 480
_ACK_SETTINGS_KEY = "role_workbench/skip_result_confirmation"
_SERVICE_POLL_SECONDS = 1.0
_EDITABLE_ROLE_STATUS_ROLE = Qt.UserRole + 1
_EDITABLE_ROLE_REASON_ROLE = Qt.UserRole + 2
_INVALID_PACKAGE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DIRECTIONAL_OPPOSITES = {
    "peek_left": "peek_right",
    "peek_right": "peek_left",
}


def _account_summary_text(value: RoleServiceAccountSummary) -> str:
    flexible = value.available_flexible_units
    categorized = value.available_t2i_units + value.available_i2v_units + flexible
    if categorized < value.available_units:
        flexible += value.available_units - categorized
    available = (
        f"立绘生成 {value.available_t2i_units} 次"
        f" · 动作生成 {value.available_i2v_units} 次"
    )
    if flexible:
        available += f" · 通用 {flexible} 次"
    return (
        available
        + f" · 任务占用 {value.reserved_units} 次"
        + f" · 已使用 {value.consumed_units} 次"
    )


def _client_runtime_info() -> ClientRuntimeInfo:
    machine = platform.machine().strip().lower()
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, "unknown")
    windows_major = "unknown"
    if platform.system().lower() == "windows":
        candidate = platform.release().split(".", maxsplit=1)[0]
        if candidate.isdigit() and 1 <= len(candidate) <= 3:
            windows_major = candidate
    screen = QApplication.primaryScreen()
    scale = 100
    if screen is not None:
        scale = max(50, min(400, round(screen.devicePixelRatio() * 100)))
    return ClientRuntimeInfo(
        app_version=__version__,
        windows_major=windows_major,
        architecture=architecture,
        display_scale_percent=scale,
    )


def _write_package_identity(package_root: Path, request: WorkbenchRequest) -> None:
    manifest_path = package_root / "role.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["role_id"] = request.role_id
    manifest["display_name"] = request.display_name.strip()
    if manifest.get("schema_version") == 2 and isinstance(manifest.get("profile"), dict):
        manifest["profile"]["id"] = request.role_id
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _package_folder_name(display_name: str, fallback: str) -> str:
    value = _INVALID_PACKAGE_NAME_RE.sub("_", display_name).strip(" .")
    return value[:80].rstrip(" .") or fallback


def _new_role_id() -> str:
    """Return an opaque stable ID; the user-facing name stays independently editable."""
    return f"pet-{uuid.uuid4().hex[:16]}"


def _selected_action_tuple(actions: tuple[str, ...] | None) -> tuple[str, ...]:
    selected = tuple(OFFICIAL_ACTIONS) if actions is None else actions
    if len(selected) != len(set(selected)):
        raise ValueError("动作选择不能重复")
    unknown = set(selected).difference(OFFICIAL_ACTIONS)
    if unknown:
        raise ValueError("未知动作：" + "、".join(sorted(unknown)))
    if "idle" not in selected:
        raise ValueError("待机 idle 是桌宠运行所需的必选动作")
    return tuple(action for action in OFFICIAL_ACTIONS if action in selected)


def _make_v2_action_previews(
    package_root: Path,
    actions: tuple[str, ...],
    preview_root: Path | None = None,
) -> None:
    previews = preview_root or (package_root / "previews")
    previews.mkdir(parents=True, exist_ok=True)
    for action in actions:
        frames = sorted((package_root / "actions" / action).glob("*.png"))
        images = []
        for frame in frames:
            with Image.open(frame) as source:
                images.append(source.convert("RGBA"))
        if images:
            images[0].save(
                previews / f"{action}.gif",
                save_all=True,
                append_images=images[1:],
                duration=167,
                loop=0,
                disposal=2,
            )


def _normalize_uploaded_image(source: Path, output: Path) -> None:
    if source.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("输入图片不得超过 20 MB")
    try:
        with Image.open(source) as image:
            image.load()
            if min(image.size) < 240 or max(image.size) > 8000:
                raise ValueError("输入图片边长须在 240~8000 像素之间")
            image.convert("RGBA").save(output)
    except OSError as exc:
        raise ValueError(f"无法读取输入图片：{exc}") from exc


def _accept_generated_package(package_root: Path) -> RolePackage:
    """Persist the user's save/install confirmation in a native v2 package."""

    manifest_path = package_root / "role.json"
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if raw_manifest.get("schema_version") != 2:
        return load_role_package(package_root)
    package = RolePackage.from_dict(raw_manifest)
    manifest = package.to_dict()
    quality = dict(manifest["quality"])
    quality_actions = dict(quality["actions"])
    if any(dict(raw).get("status") == "rejected" for raw in quality_actions.values()):
        raise ValueError("角色包含有已拒绝动作，不能保存或安装")
    for action, raw in quality_actions.items():
        action_quality = dict(raw)
        action_quality["status"] = "accepted"
        if action in _DIRECTIONAL_OPPOSITES:
            direction = dict(action_quality["direction"])
            direction["status"] = "accepted"
            action_quality["direction"] = direction
        quality_actions[action] = action_quality
    quality["status"] = "accepted"
    quality["actions"] = quality_actions
    manifest["quality"] = quality
    manifest["rights"] = {
        "input_rights_confirmed": True,
        "output_accepted": True,
        "scope": "private-use",
    }
    accepted = RolePackage.from_dict(manifest)
    manifest_path.write_text(accepted.to_json(), encoding="utf-8")
    return accepted


@dataclass(frozen=True)
class WorkbenchBrief:
    """Small provider-neutral prompt input shared with Internal adapters."""

    brief_id: str
    presentation: str
    style_card: str
    silhouette: str
    detail: str
    anchors: tuple[str, str, str]

    def validate(self) -> None:
        if not self.brief_id:
            raise ValueError("brief_id is required")
        if self.presentation not in {"feminine", "masculine", "neutral"}:
            raise ValueError(f"unknown presentation: {self.presentation}")
        if self.style_card not in {
            "custom",
            "campus_fresh",
            "urban_cool",
            "bookish_gentle",
            "sporty_bright",
            "sweet_dark",
        }:
            raise ValueError(f"unknown style_card: {self.style_card}")
        if self.silhouette not in {"super_chibi", "chibi", "petite"}:
            raise ValueError(f"unknown silhouette: {self.silhouette}")
        if not all(self.anchors):
            raise ValueError("identity anchors must not be empty")
        if len(self.detail) > 800:
            raise ValueError("detail must be at most 800 characters")

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "presentation": self.presentation,
            "style_card": self.style_card,
            "silhouette": self.silhouette,
            "detail": self.detail,
            "anchors": list(self.anchors),
        }


@dataclass(frozen=True)
class WorkbenchRequest:
    """One user-readable request shared by text and image demo paths."""

    role_id: str
    display_name: str
    input_mode: str
    presentation: str
    style_card: str
    silhouette: str
    detail: str
    anchors: tuple[str, str, str]
    image_path: str = ""
    candidate_count: int = 2
    negative_detail: str = ""
    style_and_mood: str = ""
    palette: str = ""
    hair: str = ""
    eyes: str = ""
    face: str = ""
    clothing: str = ""
    accessories: str = ""
    special_features: str = ""

    def validate(self) -> None:
        if not _ROLE_ID_RE.fullmatch(self.role_id):
            raise ValueError("角色 ID 需为 3~48 位小写字母、数字或连字符")
        if not self.display_name.strip() or len(self.display_name.strip()) > 80:
            raise ValueError("角色名称 / 概念不能为空且最多 80 个字符")
        if self.input_mode not in {"text", "image"}:
            raise ValueError("未知的角色输入方式")
        if not 1 <= self.candidate_count <= 4:
            raise ValueError("身份候选数量必须在 1~4 张之间")
        if len(self.negative_detail.strip()) > 100:
            raise ValueError("避免出现的内容最多 100 个字符")
        if self.input_mode == "image":
            image = Path(self.image_path)
            if not image.is_file():
                raise ValueError("请选择一张可读取的角色图片")
            if image.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("输入图片不得超过 20 MB")
        self.to_brief().validate()

    def to_brief(self) -> WorkbenchBrief:
        return WorkbenchBrief(
            brief_id=self.role_id,
            presentation=self.presentation,
            style_card=self.style_card,
            silhouette=self.silhouette,
            detail=self.detail.strip(),
            anchors=tuple(anchor.strip() for anchor in self.anchors),
        )

    def to_profile(
        self,
        *,
        appearance_revision: int = 1,
        profile_input: ProfileInput | None = None,
    ) -> CharacterProfile:
        """Compile the user-facing form into the editable appearance contract."""
        self.validate()
        if profile_input is None:
            if self.input_mode == "text":
                profile_input = ProfileInput(kind="text")
            else:
                source = Path(self.image_path)
                with Image.open(source) as image:
                    media_type = {
                        "PNG": "image/png",
                        "JPEG": "image/jpeg",
                        "WEBP": "image/webp",
                    }.get(str(image.format).upper())
                if media_type is None:
                    raise ValueError("角色图片必须是 PNG、JPEG 或 WebP")
                profile_input = ProfileInput(
                    kind="image",
                    sha256=sha256(source.read_bytes()).hexdigest(),
                    media_type=media_type,
                )
        if profile_input.kind != self.input_mode:
            raise ValueError("角色档案输入类型与当前生成方式不一致")
        profile = CharacterProfile(
            profile_id=self.role_id,
            appearance_revision=appearance_revision,
            display_name=self.display_name.strip(),
            input=profile_input,
            visual=VisualIdentity(
                presentation=self.presentation,
                silhouette=self.silhouette,
                description=self.detail.strip(),
                negative_description=self.negative_detail.strip(),
                style_and_mood=self.style_and_mood.strip(),
                palette=self.palette.strip(),
                hair=self.hair.strip(),
                eyes=self.eyes.strip(),
                face=self.face.strip(),
                clothing=self.clothing.strip(),
                accessories=self.accessories.strip(),
                special_features=self.special_features.strip(),
                identity_anchors=tuple(
                    anchor.strip() for anchor in self.anchors if anchor.strip()
                ),
            ),
        )
        return CharacterProfile.from_dict(profile.to_dict())

    def private_snapshot(self) -> dict:
        """Return a session record without exposing an absolute input path."""
        value = {
            "schema_version": 1,
            "role_id": self.role_id,
            "display_name": self.display_name.strip(),
            "input_mode": self.input_mode,
            "brief": self.to_brief().to_dict(),
            "candidate_count": self.candidate_count,
            "negative_detail": self.negative_detail.strip(),
            "profile": self.to_profile().to_dict(),
        }
        if self.image_path:
            source = Path(self.image_path)
            value["input_image"] = {
                "name": source.name,
                "sha256": sha256(source.read_bytes()).hexdigest(),
            }
        return value

    def to_private_context(self) -> dict[str, Any]:
        """Serialize the editable local request for crash recovery.

        Unlike ``private_snapshot`` this local-only record retains every form
        field and the managed input path.  It is never part of the role package
        or provider request contract.
        """
        return {
            "role_id": self.role_id,
            "display_name": self.display_name,
            "input_mode": self.input_mode,
            "presentation": self.presentation,
            "style_card": self.style_card,
            "silhouette": self.silhouette,
            "detail": self.detail,
            "anchors": list(self.anchors),
            "image_path": self.image_path,
            "candidate_count": self.candidate_count,
            "negative_detail": self.negative_detail,
            "style_and_mood": self.style_and_mood,
            "palette": self.palette,
            "hair": self.hair,
            "eyes": self.eyes,
            "face": self.face,
            "clothing": self.clothing,
            "accessories": self.accessories,
            "special_features": self.special_features,
        }

    @classmethod
    def from_private_context(cls, value: Any) -> WorkbenchRequest:
        if not isinstance(value, dict):
            raise ValueError("工作台任务缺少可恢复的角色设定")
        try:
            anchors = tuple(str(item) for item in value["anchors"])
            request = cls(
                role_id=str(value["role_id"]),
                display_name=str(value["display_name"]),
                input_mode=str(value["input_mode"]),
                presentation=str(value["presentation"]),
                style_card=str(value["style_card"]),
                silhouette=str(value["silhouette"]),
                detail=str(value["detail"]),
                anchors=anchors,  # type: ignore[arg-type]
                image_path=str(value.get("image_path", "")),
                candidate_count=int(value.get("candidate_count", 2)),
                negative_detail=str(value.get("negative_detail", "")),
                style_and_mood=str(value.get("style_and_mood", "")),
                palette=str(value.get("palette", "")),
                hair=str(value.get("hair", "")),
                eyes=str(value.get("eyes", "")),
                face=str(value.get("face", "")),
                clothing=str(value.get("clothing", "")),
                accessories=str(value.get("accessories", "")),
                special_features=str(value.get("special_features", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("工作台任务的角色设定已损坏") from exc
        if len(request.anchors) != 3:
            raise ValueError("工作台任务必须保留三个内部身份锚点")
        request.validate()
        return request


@dataclass(frozen=True)
class CandidateResult:
    session_root: Path
    candidates: tuple[Path, ...]
    spent_cny: float


@dataclass(frozen=True)
class PackageResult:
    package_root: Path
    spent_cny: float


@dataclass(frozen=True)
class _WorkbenchTaskContext:
    """Local-only operation binding; provider-neutral task records stay clean."""

    operation: str
    result_kind: str
    request: WorkbenchRequest
    source_task_id: str = ""
    candidate_relative_path: str = ""
    actions: tuple[str, ...] = ()
    instruction: str = ""
    accepted_direction_sources: tuple[str, ...] = ()
    package_version: int | None = None
    appearance_revision: int = 1
    source_package: PackageKey | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": 1,
            "operation": self.operation,
            "result_kind": self.result_kind,
            "request": self.request.to_private_context(),
            "source_task_id": self.source_task_id,
            "candidate_relative_path": self.candidate_relative_path,
            "actions": list(self.actions),
            "instruction": self.instruction,
            "accepted_direction_sources": list(self.accepted_direction_sources),
            "appearance_revision": self.appearance_revision,
        }
        if self.package_version is not None:
            value["package_version"] = self.package_version
        if self.source_package is not None:
            value["source_package"] = {
                "role_id": self.source_package.role_id,
                "package_version": self.source_package.package_version,
            }
        return value

    @classmethod
    def from_dict(cls, value: Any) -> _WorkbenchTaskContext:
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("工作台任务上下文版本无效")
        source_raw = value.get("source_package")
        source_package = None
        if source_raw is not None:
            if not isinstance(source_raw, dict):
                raise ValueError("工作台任务的来源角色包无效")
            source_package = PackageKey(
                role_id=str(source_raw.get("role_id", "")),
                package_version=int(source_raw.get("package_version", 0)),
            )
        context = cls(
            operation=str(value.get("operation", "")),
            result_kind=str(value.get("result_kind", "")),
            request=WorkbenchRequest.from_private_context(value.get("request")),
            source_task_id=str(value.get("source_task_id", "")),
            candidate_relative_path=str(value.get("candidate_relative_path", "")),
            actions=tuple(str(item) for item in value.get("actions", [])),
            instruction=str(value.get("instruction", "")),
            accepted_direction_sources=tuple(
                str(item) for item in value.get("accepted_direction_sources", [])
            ),
            package_version=(
                int(value["package_version"])
                if value.get("package_version") is not None
                else None
            ),
            appearance_revision=int(value.get("appearance_revision", 1)),
            source_package=source_package,
        )
        if context.operation not in {
            "identity_candidates",
            "initial_package",
            "action_revision",
            "appearance_revision",
        }:
            raise ValueError("工作台任务操作无效")
        if context.result_kind not in {"candidates", "package"}:
            raise ValueError("工作台任务结果类型无效")
        if set(context.actions).difference(OFFICIAL_ACTIONS):
            raise ValueError("工作台任务包含未知动作")
        return context


class _WorkbenchTaskContextStore:
    """Atomic private bindings plus one pointer to the unfinished UI task."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._active_path = self.root / "active-task.json"

    def _context_path(self, local_task_id: str) -> Path:
        return self.root / "contexts" / f"{local_task_id}.json"

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, local_task_id: str, context: _WorkbenchTaskContext) -> None:
        payload = json.dumps(
            context.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        self._atomic_write(self._context_path(local_task_id), payload)

    def load(self, local_task_id: str) -> _WorkbenchTaskContext:
        try:
            raw = json.loads(
                self._context_path(local_task_id).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("工作台任务上下文缺失或损坏") from exc
        return _WorkbenchTaskContext.from_dict(raw)

    def set_active(self, local_task_id: str) -> None:
        self._atomic_write(
            self._active_path,
            json.dumps({"schema_version": 1, "local_task_id": local_task_id}) + "\n",
        )

    def active(self) -> str:
        if not self._active_path.is_file():
            return ""
        try:
            raw = json.loads(self._active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        value = raw.get("local_task_id", "") if isinstance(raw, dict) else ""
        return str(value)

    def clear_active(self, local_task_id: str) -> None:
        if self.active() == local_task_id:
            self._active_path.unlink(missing_ok=True)


class RoleWorkbenchBackend(Protocol):
    """Provider-neutral seam used by the UI and deterministic tests."""

    is_fake: bool
    storage_root: Path

    def prepare_candidates(
        self, request: WorkbenchRequest, progress: ProgressCallback
    ) -> CandidateResult: ...

    def generate_package(
        self,
        request: WorkbenchRequest,
        candidate_path: Path,
        session_root: Path,
        progress: ProgressCallback,
        actions: tuple[str, ...] | None = None,
    ) -> PackageResult: ...

    def generate_appearance_revision(
        self,
        request: WorkbenchRequest,
        candidate_path: Path,
        session_root: Path,
        package_version: int,
        appearance_revision: int,
        actions: tuple[str, ...],
        progress: ProgressCallback,
    ) -> PackageResult: ...

    def regenerate_actions(
        self,
        request: WorkbenchRequest,
        candidate_path: Path,
        session_root: Path,
        actions: tuple[str, ...],
        instruction: str,
        progress: ProgressCallback,
        *,
        accepted_direction_sources: frozenset[str] = frozenset(),
    ) -> PackageResult: ...

    def revise_installed_package(
        self,
        request: WorkbenchRequest,
        candidate_path: Path,
        source_root: Path,
        session_root: Path,
        package_version: int,
        actions: tuple[str, ...],
        instruction: str,
        progress: ProgressCallback,
        *,
        accepted_direction_sources: frozenset[str] = frozenset(),
    ) -> PackageResult: ...


class RemoteRoleWorkbenchBackend:
    """Storage-only backend for production HTTPS workbench sessions."""

    is_fake = False

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def storage_root(self) -> Path:
        return self._root

    @staticmethod
    def _remote_only(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("在线工作台任务必须通过角色服务提交")

    prepare_candidates = _remote_only
    generate_package = _remote_only
    generate_appearance_revision = _remote_only
    regenerate_actions = _remote_only
    revise_installed_package = _remote_only


class FakeRoleWorkbenchBackend:
    """Zero-cost backend that exercises the complete UI and runtime switch."""

    is_fake = True

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def storage_root(self) -> Path:
        return self._root

    def prepare_candidates(
        self, request: WorkbenchRequest, progress: ProgressCallback
    ) -> CandidateResult:
        request.validate()
        session = self._root / f"{request.role_id}-{uuid.uuid4().hex[:8]}"
        candidates = session / "candidates"
        candidates.mkdir(parents=True)
        progress("离线演示：正在准备身份候选…", 40)
        if request.input_mode == "image":
            output = candidates / "uploaded-identity.png"
            _normalize_uploaded_image(Path(request.image_path), output)
            paths = (output,)
        else:
            paths_list: list[Path] = []
            fixtures = ("lumen", "poppy", "rook", "lumen")
            for index, role_id in enumerate(
                fixtures[: request.candidate_count], start=1
            ):
                output = candidates / f"candidate-{index:02d}.png"
                shutil.copy2(resource_path("roles", role_id, "idle", "0001.png"), output)
                paths_list.append(output)
            paths = tuple(paths_list)
        progress("离线候选已准备好", 100)
        return CandidateResult(session, paths, 0.0)

    def generate_package(
        self,
        request: WorkbenchRequest,
        candidate_path: Path,
        session_root: Path,
        progress: ProgressCallback,
        actions: tuple[str, ...] | None = None,
    ) -> PackageResult:
        selected_actions = _selected_action_tuple(actions)
        progress(f"离线演示：正在组装 {len(selected_actions)} 个动作…", 35)
        package_root = session_root / "package"
        package_root.mkdir()
        source_root = resource_path("roles", "lumen")
        digest = sha256(normalized_identity_png(candidate_path)).hexdigest()
        manifest_actions: dict[str, dict[str, object]] = {}
        quality_actions: dict[str, dict[str, object]] = {}
        for action in selected_actions:
            source_action = source_root / action
            target_action = package_root / "actions" / action
            shutil.copytree(source_action, target_action)
            frames = sorted(target_action.glob("*.png"))
            manifest_actions[action] = {
                "loop": action in LOOP_ACTIONS,
                "frames": [
                    {
                        "path": f"actions/{action}/{frame.name}",
                        "sha256": sha256(frame.read_bytes()).hexdigest(),
                    }
                    for frame in frames
                ],
            }
            action_quality: dict[str, object] = {
                "status": "accepted",
                "source": "legacy",
                "processing": "raw",
                "metrics": {},
                "warnings": ["fake-backend-fixture"],
            }
            if action in _DIRECTIONAL_OPPOSITES:
                action_quality["direction"] = {
                    "expected": action.removeprefix("peek_"),
                    "status": "accepted",
                    "method": "manual-screen-review",
                }
            quality_actions[action] = action_quality
        head_body_ratio = HEAD_BODY_RATIO_BY_SILHOUETTE[request.silhouette]
        manifest = {
            "schema_version": 2,
            "role_id": request.role_id,
            "package_version": 1,
            "profile": {
                "id": request.role_id,
                "appearance_revision": 1,
                "identity_sha256": digest,
            },
            "display_name": request.display_name.strip(),
            "fps": 6,
            "canvas": [512, 512],
            "interaction": {
                "edge_reveal_fraction": half_head_reveal_fraction(head_body_ratio),
                "head_body_ratio": head_body_ratio,
                "peek_action_semantics": "viewer_direction",
            },
            "actions": manifest_actions,
            "quality": {"status": "accepted", "actions": quality_actions},
            "rights": {
                "input_rights_confirmed": True,
                "output_accepted": True,
                "scope": "private-use",
            },
            "dialogue": {"click_lines": ["离线演示角色已就绪。"]},
        }
        canonical = RolePackage.from_dict(manifest)
        (package_root / "role.json").write_text(canonical.to_json(), encoding="utf-8")
        _make_v2_action_previews(package_root, selected_actions)
        progress(f"离线 {len(selected_actions)} 动作包已完成", 100)
        return PackageResult(package_root, 0.0)

    def regenerate_actions(
        self,
        request: WorkbenchRequest,
        candidate_path: Path,
        session_root: Path,
        actions: tuple[str, ...],
        instruction: str,
        progress: ProgressCallback,
        *,
        accepted_direction_sources: frozenset[str] = frozenset(),
    ) -> PackageResult:
        del candidate_path, instruction, accepted_direction_sources
        if not actions:
            raise ValueError("请选择需要重新生成的动作")
        package_root = session_root / "package"
        if not package_root.is_dir():
            raise ValueError("当前任务还没有可修订的动作包")
        progress(f"离线演示：正在更新所选 {len(actions)} 个动作…", 45)
        _write_package_identity(package_root, request)
        manifest = json.loads((package_root / "role.json").read_text(encoding="utf-8"))
        _make_v2_action_previews(package_root, tuple(manifest.get("actions", {})))
        progress("离线动作修订已完成", 100)
        return PackageResult(package_root, 0.0)

    def generate_appearance_revision(
        self,
        request: WorkbenchRequest,
        candidate_path: Path,
        session_root: Path,
        package_version: int,
        appearance_revision: int,
        actions: tuple[str, ...],
        progress: ProgressCallback,
    ) -> PackageResult:
        request.validate()
        selected_actions = _selected_action_tuple(actions)
        if package_version < 1 or appearance_revision < 1:
            raise ValueError("角色包版本和形象修订必须为正整数")
        output_session = Path(session_root) / (
            f"appearance-{appearance_revision}-v{package_version}"
        )
        output_session.mkdir(parents=True, exist_ok=False)
        result = self.generate_package(
            request,
            candidate_path,
            output_session,
            progress,
            selected_actions,
        )
        manifest_path = result.package_root / "role.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["package_version"] = package_version
        manifest["profile"]["appearance_revision"] = appearance_revision
        canonical = RolePackage.from_dict(manifest)
        manifest_path.write_text(canonical.to_json(), encoding="utf-8")
        progress(
            f"离线新形象修订 r{appearance_revision} / v{package_version} 已完成",
            100,
        )
        return PackageResult(result.package_root, result.spent_cny)

    def revise_installed_package(
        self,
        request: WorkbenchRequest,
        candidate_path: Path,
        source_root: Path,
        session_root: Path,
        package_version: int,
        actions: tuple[str, ...],
        instruction: str,
        progress: ProgressCallback,
        *,
        accepted_direction_sources: frozenset[str] = frozenset(),
    ) -> PackageResult:
        del instruction
        request.validate()
        if not actions:
            raise ValueError("请选择需要新增或替换的动作")
        if len(actions) != len(set(actions)):
            raise ValueError("动作选择不能重复")
        unknown = set(actions).difference(OFFICIAL_ACTIONS)
        if unknown:
            raise ValueError("未知动作：" + "、".join(sorted(unknown)))
        source = RolePackage.from_json(
            (Path(source_root) / "role.json").read_text(encoding="utf-8")
        )
        if source.role_id != request.role_id:
            raise ValueError("当前档案与已安装角色不匹配")
        identity_digest = sha256(normalized_identity_png(candidate_path)).hexdigest()
        if identity_digest != source.identity_sha256:
            raise ValueError("受管身份图与已安装角色不匹配")

        progress(f"离线演示：正在准备 {len(actions)} 个动作修订…", 30)
        fixture_root = resource_path("roles", "lumen")
        revisions = []
        for action in actions:
            frames = tuple(sorted((fixture_root / action).glob("*.png")))
            source_action = _DIRECTIONAL_OPPOSITES.get(action)
            is_conjugate = source_action in accepted_direction_sources
            revisions.append(
                ActionRevision(
                    action=action,
                    frames=frames,
                    status="accepted",
                    source="conjugate" if is_conjugate else "generated",
                    source_action=source_action if is_conjugate else None,
                    direction_status=(
                        "accepted" if action in _DIRECTIONAL_OPPOSITES else None
                    ),
                    warnings=("fake-backend-fixture",),
                )
            )
        output = Path(session_root) / f"revision-{package_version}"
        build_package_revision(
            source_root,
            output,
            package_version=package_version,
            revisions=tuple(revisions),
            output_accepted=True,
        )
        revised = RolePackage.from_json(
            (output / "role.json").read_text(encoding="utf-8")
        )
        revised_actions = tuple(action for action, _ in revised.actions)
        _make_v2_action_previews(output, revised_actions)
        progress(
            f"离线新版本 v{package_version} 已完成，旧版本保持不变",
            100,
        )
        return PackageResult(output, 0.0)


class _BackendWorker(QThread):
    progress = Signal(str, int)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()

    def __init__(self, operation: Callable[[ProgressCallback], object]) -> None:
        super().__init__()
        self._operation = operation

    def run(self) -> None:
        def report(message: str, percent: int) -> None:
            if self.isInterruptionRequested():
                raise _BackendCancelled
            self.progress.emit(message, percent)

        try:
            result = self._operation(report)
        except _BackendCancelled:
            self.cancelled.emit()
            return
        except Exception as exc:
            self.failed.emit(exc)
            return
        if self.isInterruptionRequested():
            self.cancelled.emit()
            return
        self.succeeded.emit(result)


class _BackendCancelled(Exception):
    """A cooperative stop at a backend progress boundary."""


class RoleCreditDialog(QDialog):
    """Display server-authoritative generation balances and redeem one-use codes."""

    redeemed = Signal()

    def __init__(
        self,
        service_transport: RoleServiceTransport | None,
        parent: QWidget | None = None,
        *,
        unavailable_message: str = "",
    ) -> None:
        super().__init__(parent)
        self._service_transport = service_transport
        self._worker: _BackendWorker | None = None
        self.redeemed_successfully = False

        self.setObjectName("roleCreditDialog")
        self.setWindowTitle("生成次数与兑换码 · 萌卫")
        self.setMinimumWidth(520)
        self.setStyleSheet(theme.dialog_qss("roleCreditDialog"))

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        heading = QLabel("桌宠工坊生成次数")
        heading.setProperty("role", "title")
        root.addWidget(heading)

        hint = QLabel(
            unavailable_message.strip()
            or "兑换码可以分别包含立绘生成和动作生成次数；"
            "次数由萌卫生成服务保管，不会写入可修改的本地余额。"
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        root.addWidget(hint)

        balance_row = QHBoxLayout()
        self.balance_label = QLabel(
            "生成次数：点击刷新"
            if service_transport is not None
            else "生成次数：尚未连接生成服务"
        )
        self.balance_label.setWordWrap(True)
        self.balance_label.setProperty("role", "hint")
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self._refresh)
        balance_row.addWidget(self.balance_label, 1)
        balance_row.addWidget(self.refresh_button)
        root.addLayout(balance_row)

        redeem_row = QHBoxLayout()
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("输入一次性兑换码")
        self.code_edit.setClearButtonEnabled(True)
        self.code_edit.returnPressed.connect(self._redeem)
        self.redeem_button = QPushButton("兑换")
        self.redeem_button.setStyleSheet(theme.button_qss("accent"))
        self.redeem_button.clicked.connect(self._redeem)
        redeem_row.addWidget(self.code_edit, 1)
        redeem_row.addWidget(self.redeem_button)
        root.addLayout(redeem_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("role", "hint")
        root.addWidget(self.status_label)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.accept)
        close_row.addWidget(self.close_button)
        root.addLayout(close_row)

        available = service_transport is not None
        self.refresh_button.setEnabled(available)
        self.code_edit.setEnabled(available)
        self.redeem_button.setEnabled(
            available
            and callable(getattr(service_transport, "redeem_credit_code", None))
        )

    def _set_busy(self, busy: bool) -> None:
        available = self._service_transport is not None
        self.refresh_button.setEnabled(available and not busy)
        self.code_edit.setEnabled(available and not busy)
        self.redeem_button.setEnabled(
            available
            and not busy
            and callable(
                getattr(self._service_transport, "redeem_credit_code", None)
            )
        )
        self.close_button.setEnabled(not busy)

    def _start(
        self,
        operation: Callable[[ProgressCallback], object],
        success: Callable[[object], None],
    ) -> None:
        if self._worker is not None:
            return
        worker = _BackendWorker(operation)
        self._worker = worker
        worker.succeeded.connect(success)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        self._set_busy(True)
        worker.start()

    def _refresh(self, _checked: bool = False) -> None:
        query = getattr(self._service_transport, "account_summary", None)
        if not callable(query):
            return
        self.balance_label.setText("正在刷新生成次数…")
        self.status_label.setText("")
        self._start(lambda _progress: query(), self._show_balance)

    def _redeem(self, _checked: bool = False) -> None:
        redeem = getattr(self._service_transport, "redeem_credit_code", None)
        code = self.code_edit.text().strip()
        if not callable(redeem) or not code:
            if not code:
                self.status_label.setText("请输入兑换码。")
            return
        self.status_label.setText("正在兑换…")

        def success(value: object) -> None:
            self._show_balance(value)
            if not isinstance(value, RoleServiceAccountSummary):
                return
            self.code_edit.clear()
            self.redeemed_successfully = True
            self.status_label.setText("兑换成功，生成次数已经到账。")
            self.redeemed.emit()

        self._start(lambda _progress: redeem(code), success)

    def _show_balance(self, value: object) -> None:
        if not isinstance(value, RoleServiceAccountSummary):
            self._failed(ValueError("角色生成服务返回了无效次数信息"))
            return
        self.balance_label.setText(_account_summary_text(value))

    def _failed(self, error: object) -> None:
        message = role_service_user_message(error)
        self.status_label.setText(message)
        if self.balance_label.text() == "正在刷新生成次数…":
            self.balance_label.setText(f"次数刷新失败：{message}")

    def _finished(self) -> None:
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker is not None:
            worker.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            event.ignore()
            return
        super().closeEvent(event)


class RoleWorkbenchDialog(QDialog):
    """One-window workbench for creating and revising managed roles."""

    install_requested = Signal(object)
    binding_requested = Signal()
    unbinding_requested = Signal()

    def __init__(
        self,
        backend: RoleWorkbenchBackend,
        parent: QWidget | None = None,
        *,
        show_costs: bool = True,
        draft_store: RoleDraftStore | None = None,
        role_library: RoleLibrary | None = None,
        service_transport: RoleServiceTransport | None = None,
        local_service_executor: Callable[..., None] | None = None,
        generation_available: bool = True,
        generation_unavailable_message: str = "",
        binding_available: bool = False,
        service_unbinding_available: bool = False,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        # Capture Qt display information on the UI thread.  Persistent task
        # preparation runs in a worker and must never query QApplication.
        self._client_runtime = _client_runtime_info()
        self._show_costs = show_costs
        self._worker: _BackendWorker | None = None
        self._current_operation: Callable[[ProgressCallback], object] | None = None
        self._current_success: Callable[[object], None] | None = None
        self._refresh_account_after_worker = False
        self._retry_operation: Callable[[ProgressCallback], object] | None = None
        self._retry_success: Callable[[object], None] | None = None
        self._active_task_id = ""
        self._active_task_context: _WorkbenchTaskContext | None = None
        self._candidate_task_id = ""
        self._package_task_id = ""
        self._active_request: WorkbenchRequest | None = None
        self._candidate_result: CandidateResult | None = None
        self._package_result: PackageResult | None = None
        self._package_key: PackageKey | None = None
        self._result_saved = True
        self._editing_key: PackageKey | None = None
        self._appearance_source_key: PackageKey | None = None
        self._target_appearance_revision: int | None = None
        self._revision_pending_install = False
        self._preview_root: Path | None = None
        self._preview_movie: QMovie | None = None
        self._selected_image_path = ""
        self._settings = QSettings("MoeGuard", "CustomRoleDemo")
        task_state_root = backend.storage_root / "state"
        self._draft_store = draft_store or RoleDraftStore(task_state_root)
        self._task_store = RoleTaskStore(task_state_root / "tasks")
        self._task_artifacts = RoleTaskArtifactStore(
            task_state_root / "task-artifacts", self._task_store
        )
        self._task_contexts = _WorkbenchTaskContextStore(
            task_state_root / "workbench-tasks"
        )
        if local_service_executor is not None and service_transport is None:
            raise ValueError("本地服务执行器需要明确的服务 transport")
        self._service_transport = service_transport
        self._local_service_executor = local_service_executor
        self._remote_generation_consumes_units = bool(
            service_transport is not None and local_service_executor is None
        )
        self._generation_available = bool(generation_available)
        self._generation_unavailable_message = (
            generation_unavailable_message.strip()
            or "连接桌宠生成服务后即可生成新形象和动作。"
        )
        self._binding_available = bool(binding_available)
        self._service_unbinding_available = bool(service_unbinding_available)
        self._service_client = (
            RoleServiceClient(
                service_transport,
                self._task_store,
                self._task_artifacts,
                RoleServiceBindingStore(task_state_root / "service-bindings"),
                RoleServiceRequestStore(task_state_root / "service-requests"),
            )
            if service_transport is not None
            else None
        )
        self._role_library_is_explicit = role_library is not None
        self._role_library = role_library or RoleLibrary()

        self.setObjectName("roleWorkbenchDialog")
        self.setWindowTitle("桌宠工坊 · 萌卫")
        self.resize(1180, 760)
        self.setMinimumSize(900, 620)
        self.setStyleSheet(theme.dialog_qss("roleWorkbenchDialog"))

        root = QVBoxLayout(self)
        heading = QLabel("把一个角色设定，变成真正会动的 MoeGuard 桌宠")
        heading.setProperty("role", "title")
        root.addWidget(heading)
        if not self._generation_available:
            mode_text = (
                "离线管理模式：可以查看和管理本地桌宠；"
                "生成新形象或动作前需要连接生成服务。"
            )
        elif service_transport is not None and local_service_executor is None:
            mode_text = (
                "在线生成模式：任务经 MoeGuard 服务提交；本机不保存供应商密钥。"
            )
        elif local_service_executor is not None:
            mode_text = "服务联调模式：仅使用本地 fake，不会调用模型或产生费用。"
        elif backend.is_fake:
            mode_text = "离线演示模式：不会调用模型，也不会产生费用。"
        else:
            mode_text = (
                "内部直连模式：密钥只从系统环境读取；"
                "每次付费动作前都会再次确认。"
            )
        self.mode_hint = QLabel(mode_text)
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setProperty("role", "hint")
        root.addWidget(self.mode_hint)

        self.service_bar: QWidget | None = None
        self.bind_service_button: QPushButton | None = None
        if not self._generation_available:
            self.service_bar = QFrame()
            self.service_bar.setObjectName("roleServiceNotice")
            self.service_bar.setStyleSheet(
                "QFrame#roleServiceNotice {"
                f"background: {theme.SECTION_BG}; border: 1px solid {theme.BORDER_STRONG}; "
                "border-radius: 9px;"
                "}"
            )
            service_layout = QHBoxLayout(self.service_bar)
            service_layout.setContentsMargins(12, 8, 10, 8)
            service_layout.setSpacing(10)
            service_notice = QLabel(self._generation_unavailable_message)
            service_notice.setWordWrap(True)
            service_notice.setProperty("role", "hint")
            service_layout.addWidget(service_notice, 1)
            if self._binding_available:
                self.bind_service_button = QPushButton("连接生成服务")
                self.bind_service_button.setStyleSheet(theme.button_qss("accent"))
                self.bind_service_button.clicked.connect(self.binding_requested.emit)
                service_layout.addWidget(self.bind_service_button)
            root.addWidget(self.service_bar)

        self.account_bar: QWidget | None = None
        self.account_summary_label: QLabel | None = None
        self.account_refresh_button: QPushButton | None = None
        self.redeem_credit_button: QPushButton | None = None
        self.disconnect_service_button: QPushButton | None = None
        if callable(getattr(service_transport, "account_summary", None)):
            self.account_bar = QWidget()
            account_layout = QHBoxLayout(self.account_bar)
            account_layout.setContentsMargins(0, 0, 0, 0)
            account_layout.setSpacing(8)
            self.account_summary_label = QLabel("生成次数：点击刷新")
            self.account_summary_label.setProperty("role", "hint")
            self.account_refresh_button = QPushButton("刷新次数")
            self.account_refresh_button.clicked.connect(
                self._refresh_account_summary
            )
            account_layout.addWidget(self.account_summary_label)
            account_layout.addStretch(1)
            if callable(getattr(service_transport, "redeem_credit_code", None)):
                self.redeem_credit_button = QPushButton("兑换码…")
                self.redeem_credit_button.setToolTip(
                    "查看生成次数，或把一次性兑换码兑换到当前匿名连接"
                )
                self.redeem_credit_button.clicked.connect(self._open_credit_dialog)
                account_layout.addWidget(self.redeem_credit_button)
            account_layout.addWidget(self.account_refresh_button)
            if self._service_unbinding_available:
                self.disconnect_service_button = QPushButton("断开服务")
                self.disconnect_service_button.setToolTip(
                    "删除这台电脑保存的匿名连接凭据"
                )
                self.disconnect_service_button.clicked.connect(
                    self._request_service_unbinding
                )
                account_layout.addWidget(self.disconnect_service_button)
            root.addWidget(self.account_bar)

        self._ui_stage = 1
        self.stage_bar = QWidget()
        stage_layout = QHBoxLayout(self.stage_bar)
        stage_layout.setContentsMargins(0, 2, 0, 4)
        stage_layout.setSpacing(8)
        self.home_button = QPushButton("角色库")
        self.home_button.setToolTip("返回角色库，创建新角色或选择已有角色")
        self.home_button.setStyleSheet(theme.button_qss())
        self.home_button.clicked.connect(self._request_show_home)
        self.home_button.setVisible(False)
        stage_layout.addWidget(self.home_button)
        self.stage_labels: list[QLabel] = []
        for index, title in enumerate(("创建形象", "选择与动作", "预览安装"), start=1):
            badge = QLabel(f"{index}  {title}")
            badge.setAlignment(Qt.AlignCenter)
            badge.setMinimumHeight(34)
            badge.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.stage_labels.append(badge)
            stage_layout.addWidget(badge, 1)
            if index < 3:
                divider = QLabel("›")
                divider.setProperty("role", "hint")
                stage_layout.addWidget(divider)
        root.addWidget(self.stage_bar)

        self.workflow_body = QWidget()
        body = QHBoxLayout(self.workflow_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        root.addWidget(self.workflow_body, 1)
        left = QFrame()
        left.setObjectName("roleInputPanel")
        left.setMinimumWidth(450)
        left.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        left.setStyleSheet(
            "QFrame#roleInputPanel {"
            f"background: {theme.PANEL_BG}; border: 1px solid {theme.BORDER}; "
            "border-radius: 10px;"
            "}"
        )
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(12)
        left_layout.setAlignment(Qt.AlignTop)
        self.create_stage_panel = QWidget()
        create_stage_layout = QVBoxLayout(self.create_stage_panel)
        create_stage_layout.setContentsMargins(0, 0, 0, 0)
        create_stage_layout.setSpacing(12)
        left_layout.addWidget(self.create_stage_panel)
        right = QWidget()
        self.right_panel = right
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.input_scroll = QScrollArea()
        self.input_scroll.setObjectName("roleInputScroll")
        self.input_scroll.setWidgetResizable(True)
        self.input_scroll.setFrameShape(QFrame.NoFrame)
        self.input_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.input_scroll.setWidget(left)
        self.input_scroll.setMinimumWidth(470)
        body.addWidget(self.input_scroll, 5)
        body.addWidget(right, 6)

        mode_row = QWidget()
        self.role_mode_row = mode_row
        mode_layout = QHBoxLayout(mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(0)
        self.role_mode_label = QLabel("角色模式")
        self.role_mode_label.setMinimumWidth(76)
        mode_layout.addWidget(self.role_mode_label)
        self.role_mode_group = QButtonGroup(self)
        self.role_mode_group.setExclusive(True)
        self.new_role_mode = QToolButton()
        self.new_role_mode.setObjectName("newRoleMode")
        self.new_role_mode.setText("新建")
        self.new_role_mode.setCheckable(True)
        self.new_role_mode.setChecked(True)
        self.edit_role_mode = QToolButton()
        self.edit_role_mode.setObjectName("editRoleMode")
        self.edit_role_mode.setText("编辑")
        self.edit_role_mode.setCheckable(True)
        self.role_mode_group.addButton(self.new_role_mode)
        self.role_mode_group.addButton(self.edit_role_mode)
        segmented_base = (
            "QToolButton {"
            f"background: {theme.PANEL_BG}; color: {theme.TEXT_PRIMARY}; "
            f"border: 1px solid {theme.BORDER_STRONG}; padding: 6px 20px; "
            "font-weight: 600;"
            "}"
            f"QToolButton:checked {{ background: {theme.ACCENT}; color: white; }}"
            f"QToolButton:hover:!checked {{ background: {theme.SECTION_BG}; }}"
            f"QToolButton:disabled {{ color: {theme.TEXT_SECONDARY}; "
            f"background: {theme.SECTION_BG}; }}"
        )
        self.new_role_mode.setStyleSheet(
            segmented_base
            + "QToolButton#newRoleMode {"
            "border-top-left-radius: 10px; border-bottom-left-radius: 10px;"
            "}"
        )
        self.edit_role_mode.setStyleSheet(
            segmented_base
            + "QToolButton#editRoleMode {"
            "border-left: none; border-top-right-radius: 10px; "
            "border-bottom-right-radius: 10px;"
            "}"
        )
        for button in (self.new_role_mode, self.edit_role_mode):
            button.setFixedSize(92, 38)
            mode_layout.addWidget(button)
        mode_layout.addStretch(1)
        create_stage_layout.addWidget(mode_row)
        mode_row.setVisible(False)

        self.edit_role_controls = QWidget()
        edit_role_layout = QHBoxLayout(self.edit_role_controls)
        edit_role_layout.setContentsMargins(0, 0, 0, 0)
        edit_role_layout.setSpacing(8)
        existing_role_label = QLabel("已有角色")
        existing_role_label.setMinimumWidth(76)
        edit_role_layout.addWidget(existing_role_label)
        self.edit_role_selector = QComboBox()
        self.edit_role_selector.setFixedHeight(38)
        self.edit_role_selector.setStyleSheet(
            "QComboBox {"
            f"background: {theme.PANEL_BG}; color: {theme.TEXT_PRIMARY}; "
            f"border: 1px solid {theme.BORDER_STRONG}; border-radius: 8px; "
            "padding: 5px 32px 5px 10px;"
            "}"
            f"QComboBox:focus {{ border: 1px solid {theme.ACCENT}; }}"
            "QComboBox::drop-down { border: none; width: 28px; }"
        )
        self.open_role_button = QPushButton("载入编辑")
        self.open_role_button.setToolTip("载入已安装角色，继续添加或替换动作")
        self.open_role_button.clicked.connect(self._open_selected_role)
        self.appearance_button = QPushButton("重新设计形象")
        self.appearance_button.setToolTip("保留旧版本，创建新的可回滚形象版本")
        self.appearance_button.clicked.connect(self._start_selected_appearance_revision)
        for button in (self.open_role_button, self.appearance_button):
            button.setFixedHeight(38)
            button.setStyleSheet(theme.button_qss())
        self.edit_role_selector.currentIndexChanged.connect(
            self._edit_role_selection_changed
        )
        edit_role_layout.addWidget(self.edit_role_selector, 1)
        edit_role_layout.addWidget(self.open_role_button)
        edit_role_layout.addWidget(self.appearance_button)
        self.edit_role_controls.setVisible(False)
        create_stage_layout.addWidget(self.edit_role_controls)
        self.new_role_mode.toggled.connect(self._new_role_mode_changed)
        self.edit_role_mode.toggled.connect(self._edit_role_mode_changed)

        self.home_panel = QFrame()
        self.home_panel.setObjectName("roleHomePanel")
        self.home_panel.setStyleSheet(
            "QFrame#roleHomePanel {"
            f"background: {theme.PANEL_BG}; border: 1px solid {theme.BORDER}; "
            "border-radius: 12px;"
            "}"
        )
        home_layout = QVBoxLayout(self.home_panel)
        home_layout.setContentsMargins(24, 24, 24, 24)
        home_layout.addStretch(1)
        home_content = QWidget()
        home_content.setMaximumWidth(820)
        home_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        home_content_layout = QVBoxLayout(home_content)
        home_content_layout.setContentsMargins(0, 0, 0, 0)
        home_content_layout.setSpacing(14)
        home_title = QLabel("桌宠工坊")
        home_title.setProperty("role", "title")
        home_content_layout.addWidget(home_title)
        home_hint = QLabel(
            "创建一个新桌宠，或选择已经安装的角色继续补动作、替换动作和修改形象。"
        )
        home_hint.setWordWrap(True)
        home_hint.setProperty("role", "hint")
        home_content_layout.addWidget(home_hint)
        self.create_new_role_button = QPushButton("创建新角色")
        self.create_new_role_button.setMinimumHeight(44)
        self.create_new_role_button.setStyleSheet(theme.button_qss("accent"))
        self.create_new_role_button.clicked.connect(self._begin_new_role_from_home)
        home_content_layout.addWidget(self.create_new_role_button)
        continue_label = QLabel("继续编辑已有角色")
        continue_label.setProperty("role", "sectionTitle")
        home_content_layout.addWidget(continue_label)
        home_content_layout.addWidget(self.edit_role_controls)
        self.edit_role_hint = QLabel("")
        self.edit_role_hint.setWordWrap(True)
        self.edit_role_hint.setProperty("role", "hint")
        self.edit_role_hint.setVisible(False)
        home_content_layout.addWidget(self.edit_role_hint)
        home_layout.addWidget(home_content, 0, Qt.AlignHCenter)
        home_layout.addStretch(1)
        root.insertWidget(root.indexOf(self.stage_bar), self.home_panel, 1)
        self.home_panel.setVisible(False)

        common_form = QFormLayout()
        self.display_name = QLineEdit("我的桌宠")
        self.display_name.setMaxLength(80)
        self.display_name.setPlaceholderText("例如：DeepSeek 鲸鱼娘")
        self.role_id = QLineEdit(_new_role_id())
        self.role_id.setReadOnly(True)
        common_form.addRow("角色名称 / 概念", self.display_name)
        create_stage_layout.addLayout(common_form)
        self._refresh_editable_roles()
        concept_hint = QLabel("文字生成时，名称也会参与角色概念与立绘生成。")
        concept_hint.setWordWrap(True)
        concept_hint.setProperty("role", "hint")
        create_stage_layout.addWidget(concept_hint)

        self.input_tabs = QTabWidget()
        self.input_tabs.setDocumentMode(True)
        self.input_tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setSpacing(12)
        text_form = QFormLayout()
        text_form.setVerticalSpacing(10)

        presentation_row = QWidget()
        presentation_layout = QHBoxLayout(presentation_row)
        presentation_layout.setContentsMargins(0, 0, 0, 0)
        presentation_layout.setSpacing(16)
        self.presentation_group = QButtonGroup(self)
        self.presentation_group.setExclusive(True)
        for label, value in _PRESENTATION_LABELS:
            option = QCheckBox(label)
            option.setProperty("optionValue", value)
            self.presentation_group.addButton(option)
            presentation_layout.addWidget(option)
            if value == "feminine":
                option.setChecked(True)
            if value == "neutral":
                neutral_hint = QLabel("动物 / 机械体等")
                neutral_hint.setProperty("role", "hint")
                neutral_hint.setToolTip(
                    "用于生成动物、机械体等非人类性别外观的角色"
                )
                presentation_layout.addWidget(neutral_hint)
        presentation_layout.addStretch(1)
        text_form.addRow("性别展示", presentation_row)

        silhouette_row = QWidget()
        silhouette_layout = QHBoxLayout(silhouette_row)
        silhouette_layout.setContentsMargins(0, 0, 0, 0)
        silhouette_layout.setSpacing(18)
        self.silhouette_group = QButtonGroup(self)
        self.silhouette_group.setExclusive(True)
        for label, value in _SILHOUETTE_LABELS:
            option = QCheckBox(label)
            option.setProperty("optionValue", value)
            self.silhouette_group.addButton(option)
            silhouette_layout.addWidget(option)
            if value == "chibi":
                option.setChecked(True)
        silhouette_layout.addStretch(1)
        text_form.addRow("角色比例", silhouette_row)

        candidate_count_row = QWidget()
        candidate_count_layout = QHBoxLayout(candidate_count_row)
        candidate_count_layout.setContentsMargins(0, 0, 0, 0)
        self.candidate_count = QSlider(Qt.Horizontal)
        self.candidate_count.setRange(1, 4)
        self.candidate_count.setValue(2)
        self.candidate_count.setSingleStep(1)
        self.candidate_count.setPageStep(1)
        self.candidate_count.setTickPosition(QSlider.TicksBelow)
        self.candidate_count.setTickInterval(1)
        self.candidate_count_value = QLabel("当前 2 张")
        self.candidate_count_value.setMinimumWidth(72)
        self.candidate_count_value.setAlignment(Qt.AlignCenter)
        self.candidate_count_value.setStyleSheet(
            f"background: {theme.SECTION_BG}; color: {theme.ACCENT}; "
            f"border: 1px solid {theme.BORDER_STRONG}; border-radius: 6px; "
            "padding: 4px 8px; font-weight: 600;"
        )
        self.candidate_count.valueChanged.connect(
            lambda value: self.candidate_count_value.setText(f"当前 {value} 张")
        )
        candidate_count_layout.addWidget(QLabel("1"))
        candidate_count_layout.addWidget(self.candidate_count, 1)
        candidate_count_layout.addWidget(QLabel("4"))
        candidate_count_layout.addSpacing(10)
        candidate_count_layout.addWidget(self.candidate_count_value)
        text_form.addRow("候选数量", candidate_count_row)
        text_layout.addLayout(text_form)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("外观细节（可选）")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setArrowType(Qt.RightArrow)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.advanced_toggle.setStyleSheet(
            "QToolButton {"
            f"background: {theme.SECTION_BG}; color: {theme.ACCENT}; "
            f"border: 1px solid {theme.BORDER_STRONG};"
            "border-radius: 6px; padding: 7px 12px; font-weight: 600;"
            "}"
            f"QToolButton:hover {{ background: {theme.WINDOW_BG}; }}"
            f"QToolButton:checked {{ background: {theme.BORDER}; }}"
        )
        text_layout.addWidget(self.advanced_toggle)

        self.advanced_panel = QWidget()
        advanced_form = QFormLayout(self.advanced_panel)
        detail_specs = (
            ("style_text", "风格与气质", "清新校园、柔和水彩、未来机能"),
            ("palette", "主配色", "蓝白、低饱和粉紫、黑金点缀"),
            ("hair", "头发", "发型、长度、刘海、发色、渐变色"),
            ("eyes", "眼睛", "瞳色、眼型、大小、神态"),
            ("face", "脸部", "婴儿肥、瘦削、肤色、雀斑、妆容"),
            ("outfit", "服装", "款式、材质、花纹、鞋袜"),
            ("signature", "配饰与锚点", "发饰、帽子、眼镜、徽章、非对称配饰"),
            ("other_features", "特殊特征", "兽耳、角、尾巴、翅膀、机械部件"),
        )
        self._detail_editors: list[QLineEdit] = []
        for name, label, placeholder in detail_specs:
            editor = QLineEdit()
            editor.setMaxLength(60)
            editor.setPlaceholderText(placeholder)
            editor.textChanged.connect(self._update_detail_budget)
            setattr(self, name, editor)
            self._detail_editors.append(editor)
            advanced_form.addRow(label, editor)
        self.avoid = QLineEdit()
        self.avoid.setMaxLength(100)
        self.avoid.setPlaceholderText("不希望出现在身份立绘中的内容")
        self.avoid.textChanged.connect(self._update_detail_budget)
        advanced_form.addRow("避免出现", self.avoid)
        self.detail_budget = QLabel(f"描述长度 0/{_TEXT_DETAIL_LIMIT}")
        self.detail_budget.setProperty("role", "hint")
        advanced_form.addRow("", self.detail_budget)
        self.advanced_panel.setVisible(False)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        text_layout.addWidget(self.advanced_panel)

        image_page = QWidget()
        image_layout = QVBoxLayout(image_page)
        image_row = QHBoxLayout()
        self.image_path = QLineEdit()
        self.image_path.setReadOnly(True)
        self.image_path.setPlaceholderText("选择一张完整、清晰的二次元角色图")
        browse = QPushButton("选择图片…")
        browse.clicked.connect(self._choose_image)
        image_row.addWidget(self.image_path, 1)
        image_row.addWidget(browse)
        image_layout.addLayout(image_row)
        image_hint = QLabel(
            "建议上传单人、全身、静态姿势、头脚完整且背景简单的图片。"
            "尽量避免多人、复杂场景、大幅动作、遮挡和裁切。"
        )
        image_hint.setWordWrap(True)
        image_hint.setProperty("role", "hint")
        image_layout.addWidget(image_hint)
        self.must_preserve = QLineEdit()
        self.must_preserve.setMaxLength(60)
        self.must_preserve.setPlaceholderText(
            "必须保留或想微调的特征（可选，例如右侧马尾和蓝色蝴蝶结）"
        )
        image_layout.addWidget(self.must_preserve)

        self.input_tabs.addTab(text_page, "文字生成")
        self.input_tabs.addTab(image_page, "图片生成")
        self.input_tabs.currentChanged.connect(self._mode_changed)
        create_stage_layout.addWidget(self.input_tabs)
        create_stage_layout.addStretch(1)

        self.action_stage_panel = QWidget()
        self.action_stage_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        action_stage_layout = QVBoxLayout(self.action_stage_panel)
        action_stage_layout.setContentsMargins(0, 0, 0, 0)
        action_stage_layout.setSpacing(12)
        left_layout.addWidget(self.action_stage_panel)
        action_stage_nav = QHBoxLayout()
        self.back_to_creation_button = QToolButton()
        self.back_to_creation_button.setText("← 修改角色设定")
        self.back_to_creation_button.setStyleSheet(
            "QToolButton {"
            f"color: {theme.ACCENT}; background: transparent; border: none; "
            "padding: 4px 0; font-weight: 600;"
            "}"
            f"QToolButton:hover {{ color: {theme.ACCENT_HOVER}; }}"
        )
        self.back_to_creation_button.clicked.connect(self._return_to_creation)
        action_stage_nav.addWidget(self.back_to_creation_button)
        action_stage_nav.addStretch(1)
        action_stage_layout.addLayout(action_stage_nav)
        self.action_heading = QLabel("创建可运行桌宠")
        self.action_heading.setProperty("role", "sectionTitle")
        action_stage_layout.addWidget(self.action_heading)
        self.action_fallback_hint = QLabel(
            "待机是唯一必需动作。先生成它即可安装使用；"
            "点击、拖动和边缘探头等互动可以现在添加，也可安装后再补。"
        )
        self.action_fallback_hint.setWordWrap(True)
        self.action_fallback_hint.setProperty("role", "hint")
        action_stage_layout.addWidget(self.action_fallback_hint)

        self.action_options_toggle = QToolButton()
        self.action_options_toggle.setCheckable(True)
        self.action_options_toggle.setChecked(False)
        self.action_options_toggle.setArrowType(Qt.RightArrow)
        self.action_options_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.action_options_toggle.setStyleSheet(
            "QToolButton {"
            f"background: {theme.SECTION_BG}; color: {theme.ACCENT}; "
            f"border: 1px solid {theme.BORDER_STRONG}; "
            "border-radius: 7px; padding: 7px 12px; font-weight: 600;"
            "}"
            f"QToolButton:hover {{ background: {theme.WINDOW_BG}; }}"
        )
        self.action_options_toggle.toggled.connect(self._toggle_action_options)
        action_stage_layout.addWidget(self.action_options_toggle)

        self.action_options_panel = QWidget()
        self.action_checks: dict[str, QCheckBox] = {}
        action_grid = QGridLayout(self.action_options_panel)
        action_grid.setContentsMargins(2, 4, 2, 2)
        action_grid.setHorizontalSpacing(18)
        action_grid.setVerticalSpacing(10)
        for row, row_actions in enumerate((
            ("idle", "notice", "click_reaction"),
            ("dragging", "patrol", "welcome"),
            ("peek_left", "peek_right"),
        )):
            for column, action in enumerate(row_actions):
                option = QCheckBox(_ACTION_LABELS[action])
                option.setChecked(action == "idle")
                option.setEnabled(action != "idle")
                option.toggled.connect(self._update_generation_selection)
                self.action_checks[action] = option
                action_grid.addWidget(option, row, column, Qt.AlignLeft)
            action_grid.setRowStretch(row, 0)
        for column in range(3):
            action_grid.setColumnStretch(column, 1)
        self.action_options_panel.setVisible(False)
        action_stage_layout.addWidget(self.action_options_panel)
        self.action_credit_hint = QLabel("本次将使用 1 次动作生成次数")
        self.action_credit_hint.setProperty("role", "hint")
        action_stage_layout.addWidget(self.action_credit_hint)

        self.generation_panel = QWidget()
        generation_controls = QVBoxLayout(self.generation_panel)
        generation_controls.setContentsMargins(0, 0, 0, 0)
        generation_controls.setSpacing(8)
        main_action_row = QHBoxLayout()
        main_action_row.setSpacing(10)
        recovery_action_row = QHBoxLayout()
        recovery_action_row.setSpacing(10)
        self.prepare_button = QPushButton("生成身份候选")
        self.prepare_button.setStyleSheet(theme.button_qss("accent"))
        self.prepare_button.clicked.connect(self._prepare_candidates)
        self.generate_button = QPushButton("生成待机动作并创建桌宠")
        self.generate_button.setEnabled(False)
        self.generate_button.clicked.connect(self._generate_package)
        self.cancel_task_button = QPushButton("取消当前任务")
        self.cancel_task_button.setEnabled(False)
        self.cancel_task_button.clicked.connect(self._request_task_cancel)
        self.resume_task_button = QPushButton("恢复失败任务")
        self.resume_task_button.setEnabled(False)
        self.resume_task_button.clicked.connect(self._resume_failed_task)
        for button in (
            self.prepare_button,
            self.generate_button,
            self.cancel_task_button,
            self.resume_task_button,
        ):
            button.setFixedHeight(40)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if button is not self.prepare_button:
                button.setStyleSheet(theme.button_qss())
        main_action_row.addWidget(self.prepare_button, 1)
        main_action_row.addWidget(self.generate_button, 1)
        recovery_action_row.addWidget(self.cancel_task_button, 1)
        recovery_action_row.addWidget(self.resume_task_button, 1)
        generation_controls.addLayout(main_action_row)
        generation_controls.addLayout(recovery_action_row)
        left_layout.addWidget(self.generation_panel)

        self.activity_indicator = QLabel("")
        self.activity_indicator.setAlignment(Qt.AlignCenter)
        self.activity_indicator.setFixedHeight(28)
        self.activity_indicator.setStyleSheet("color: #3979b8; font-size: 16px;")
        self.activity_indicator.hide()
        self._activity_faces = list(_ACTIVITY_FACES)
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1600)
        self._activity_timer.timeout.connect(self._rotate_activity_indicator)
        self.status = QLabel("先填写角色设定，然后准备身份候选。")
        self.status.setWordWrap(True)
        left_layout.addWidget(self.activity_indicator)
        left_layout.addWidget(self.status)

        candidate_title = QLabel("身份候选")
        candidate_title.setProperty("role", "title")
        self.candidate_title = candidate_title
        right_layout.addWidget(candidate_title)
        self.candidates = QListWidget()
        self.candidates.setViewMode(QListView.IconMode)
        self.candidates.setIconSize(QSize(150, 170))
        self.candidates.setResizeMode(QListView.Adjust)
        self.candidates.setMaximumHeight(210)
        self.candidates.currentRowChanged.connect(self._candidate_selected)
        right_layout.addWidget(self.candidates)

        preview_column = QVBoxLayout()
        preview_title = QLabel("动作预览")
        preview_title.setProperty("role", "title")
        self.preview_title = preview_title
        preview_column.addWidget(preview_title)
        preview_row = QHBoxLayout()
        self.action_selector = QListWidget()
        self.action_selector.setMinimumWidth(175)
        self.action_selector.setMaximumWidth(220)
        self.action_selector.setVisible(False)
        self.action_selector.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.action_selector.currentItemChanged.connect(
            lambda current, _previous: self._show_action_preview_item(current)
        )
        self.action_selector.itemSelectionChanged.connect(
            self._update_regenerate_enabled
        )
        self.preview = QLabel("动作生成后可在这里连续预览")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(320, 240)
        self.preview.setStyleSheet("border: 1px solid #d9d9d9; border-radius: 8px;")
        preview_row.addWidget(self.action_selector)
        preview_row.addWidget(self.preview, 1)
        preview_column.addLayout(preview_row, 1)

        revision_row = QHBoxLayout()
        self.revision_instruction = QLineEdit()
        self.revision_instruction.setMaxLength(60)
        self.revision_instruction.setPlaceholderText("选中动作的修改要求（可选）")
        self.regenerate_button = QPushButton("重新生成所选动作")
        self.regenerate_button.setEnabled(False)
        self.regenerate_button.clicked.connect(self._regenerate_selected_actions)
        revision_row.addWidget(self.revision_instruction, 1)
        revision_row.addWidget(self.regenerate_button)
        preview_column.addLayout(revision_row)
        right_layout.addLayout(preview_column, 1)

        self.completion_panel = QWidget()
        completion_layout = QVBoxLayout(self.completion_panel)
        completion_layout.setContentsMargins(0, 0, 0, 0)
        completion_layout.setSpacing(8)
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.save_hint = QLabel("完成待机动作后，即可保存角色包或安装到萌卫。")
        self.save_hint.setProperty("role", "hint")
        self.save_hint.setAlignment(Qt.AlignRight)
        completion_layout.addWidget(self.save_hint)

        self.save_button = QPushButton("保存角色包")
        self.save_button.setEnabled(False)
        self.save_button.setStyleSheet(theme.button_qss())
        self.save_button.clicked.connect(self._save_package)
        self.install_button = QPushButton("保存并安装")
        self.install_button.setEnabled(False)
        self.install_button.setStyleSheet(theme.button_qss("accent"))
        self.install_button.clicked.connect(self._request_install)
        close_button = QPushButton("关闭")
        close_button.setStyleSheet(theme.button_qss())
        close_button.clicked.connect(self.close)
        self.close_button = close_button
        for column, button in enumerate(
            (self.save_button, self.install_button, self.close_button)
        ):
            button.setFixedHeight(40)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            bottom.addWidget(button, 1)
        completion_layout.addLayout(bottom)
        right_layout.addWidget(self.completion_panel)
        self._configure_action_picker(editing=False)
        self._update_action_emphasis()
        self._set_ui_stage(1)
        self._restore_pending_task()
        if (
            self._role_library_is_explicit
            and not self._active_task_id
            and self.edit_role_selector.count() > 1
        ):
            self._show_home()
        self._apply_generation_availability()

    def _generation_consumes_units(self) -> bool:
        return not self._backend.is_fake or self._remote_generation_consumes_units

    def _apply_generation_availability(self) -> None:
        """Gate only network generation while keeping the local workshop usable."""
        if self._generation_available:
            return
        disabled_reason = self._generation_unavailable_message
        for button in (
            self.prepare_button,
            self.generate_button,
            self.regenerate_button,
            self.resume_task_button,
        ):
            button.setEnabled(False)
            button.setToolTip(disabled_reason)

    def _set_ui_stage(self, stage: int) -> None:
        if stage not in {1, 2, 3}:
            raise ValueError("工作台阶段必须为 1、2 或 3")
        self._ui_stage = stage
        self.home_panel.setVisible(False)
        self.stage_bar.setVisible(True)
        self.workflow_body.setVisible(True)
        for index, label in enumerate(self.stage_labels, start=1):
            if index == stage:
                label.setStyleSheet(
                    f"background: {theme.ACCENT}; color: white; "
                    f"border: 1px solid {theme.ACCENT}; border-radius: 9px; "
                    "font-weight: 600; padding: 5px 10px;"
                )
            elif index < stage:
                label.setStyleSheet(
                    f"background: {theme.SECTION_BG}; color: {theme.ACCENT}; "
                    f"border: 1px solid {theme.BORDER_STRONG}; border-radius: 9px; "
                    "padding: 5px 10px;"
                )
            else:
                label.setStyleSheet(
                    f"background: {theme.PANEL_BG}; color: {theme.TEXT_SECONDARY}; "
                    f"border: 1px solid {theme.BORDER}; border-radius: 9px; "
                    "padding: 5px 10px;"
                )

        self.create_stage_panel.setVisible(stage == 1)
        self.action_stage_panel.setVisible(stage == 2)
        self.input_scroll.setVisible(stage < 3)
        self.right_panel.setVisible(stage > 1)
        self.candidate_title.setVisible(stage == 2)
        self.candidates.setVisible(stage == 2)
        self.preview_title.setVisible(stage > 1)
        self.completion_panel.setVisible(stage == 3)
        self.home_button.setVisible(self.edit_role_selector.count() > 1)
        self._update_task_control_visibility()

    def _show_home(self) -> None:
        if self._worker is not None or self.edit_role_selector.count() <= 1:
            return
        if not isinstance(self.edit_role_selector.currentData(), PackageKey):
            self.edit_role_selector.setCurrentIndex(1)
        self.home_panel.setVisible(True)
        self.edit_role_controls.setVisible(True)
        self.stage_bar.setVisible(False)
        self.workflow_body.setVisible(False)

    def _request_show_home(self) -> None:
        if not self._confirm_leave_unsaved_result("返回角色库"):
            return
        self._show_home()

    def _request_service_unbinding(self) -> None:
        if self._worker is not None:
            return
        if not self._confirm_leave_unsaved_result("断开生成服务"):
            return
        self.unbinding_requested.emit()

    def _begin_new_role_from_home(self) -> None:
        if not self._confirm_leave_unsaved_result("创建新角色"):
            return
        self.new_role_mode.blockSignals(True)
        self.edit_role_mode.blockSignals(True)
        self.new_role_mode.setChecked(True)
        self.edit_role_mode.setChecked(False)
        self.new_role_mode.blockSignals(False)
        self.edit_role_mode.blockSignals(False)
        self._reset_new_role()

    def _update_task_control_visibility(self) -> None:
        busy = self._worker is not None
        self.prepare_button.setVisible(self._ui_stage == 1)
        self.generate_button.setVisible(self._ui_stage == 2)
        self.cancel_task_button.setVisible(busy)
        self.resume_task_button.setVisible(
            not busy and self.resume_task_button.isEnabled()
        )
        self.generation_panel.setVisible(self._ui_stage < 3)

    def _return_to_creation(self) -> None:
        if self._worker is not None or self._ui_stage != 2:
            return
        answer = QMessageBox.question(
            self,
            "返回修改角色设定？",
            "当前身份候选会暂时保留；修改设定后必须重新生成候选，"
            "不会沿用与新设定不一致的形象。是否返回？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        self._set_ui_stage(1)
        self.status.setText("请修改角色设定；完成后重新生成身份候选。")

    def _refresh_editable_roles(self, selected: PackageKey | None = None) -> None:
        self.edit_role_selector.clear()
        self.edit_role_selector.addItem("新建角色", None)
        try:
            installed_roles = self._role_library.list()
        except OSError:
            installed_roles = ()
        for installed in installed_roles:
            if installed.package.source_schema_version != 2:
                continue
            editable, reason = self._role_editability(installed.key)
            self.edit_role_selector.addItem(
                f"{installed.package.display_name}（v{installed.key.package_version}）"
                + ("" if editable else " · 仅可运行"),
                installed.key,
            )
            index = self.edit_role_selector.count() - 1
            self.edit_role_selector.setItemData(
                index, editable, _EDITABLE_ROLE_STATUS_ROLE
            )
            self.edit_role_selector.setItemData(
                index,
                reason or "可继续补充动作、替换动作或重新设计形象。",
                Qt.ToolTipRole,
            )
            self.edit_role_selector.setItemData(
                index, reason, _EDITABLE_ROLE_REASON_ROLE
            )
        if selected is not None:
            index = next(
                (
                    item_index
                    for item_index in range(self.edit_role_selector.count())
                    if self.edit_role_selector.itemData(item_index) == selected
                ),
                -1,
            )
            if index >= 0:
                self.edit_role_selector.setCurrentIndex(index)
                self.edit_role_mode.setChecked(True)
                self.edit_role_controls.setVisible(True)
        self.edit_role_mode.setEnabled(self.edit_role_selector.count() > 1)
        self.home_button.setVisible(self.edit_role_selector.count() > 1)
        if self.edit_role_selector.count() <= 1:
            self.new_role_mode.setChecked(True)
            self.edit_role_controls.setVisible(False)
        self._edit_role_selection_changed(self.edit_role_selector.currentIndex())

    def _edit_role_selection_changed(self, _index: int) -> None:
        has_role = isinstance(self.edit_role_selector.currentData(), PackageKey)
        editable = has_role and bool(
            self.edit_role_selector.currentData(_EDITABLE_ROLE_STATUS_ROLE)
        )
        self.open_role_button.setEnabled(self._worker is None and editable)
        self.appearance_button.setEnabled(self._worker is None and editable)
        reason = str(
            self.edit_role_selector.currentData(_EDITABLE_ROLE_REASON_ROLE) or ""
        )
        self.edit_role_hint.setText(
            reason
            + (
                " 可继续在萌卫中使用；如需编辑，请从原始设定或参考图重新创建。"
                if reason
                else ""
            )
        )
        self.edit_role_hint.setVisible(has_role and not editable)

    def _new_role_mode_changed(self, checked: bool) -> None:
        if not checked:
            return
        if not self._confirm_leave_unsaved_result("新建角色"):
            self.edit_role_mode.setChecked(True)
            return
        self.edit_role_selector.setCurrentIndex(0)
        self.edit_role_controls.setVisible(False)
        self._reset_new_role()

    def _edit_role_mode_changed(self, checked: bool) -> None:
        if not checked:
            return
        if self.edit_role_selector.count() <= 1:
            self.new_role_mode.setChecked(True)
            return
        self.edit_role_controls.setVisible(True)
        if not isinstance(self.edit_role_selector.currentData(), PackageKey):
            self.edit_role_selector.setCurrentIndex(1)

    @staticmethod
    def _check_group_value(group: QButtonGroup, value: str) -> None:
        for button in group.buttons():
            if button.property("optionValue") == value:
                button.setChecked(True)
                return

    @staticmethod
    def _profile_request(profile: CharacterProfile, image_path: Path) -> WorkbenchRequest:
        anchors = list(profile.visual.identity_anchors[:3])
        defaults = (
            "consistent clearly defined face, hair and eyes",
            "consistent clearly defined outfit, proportions and palette",
            "consistent distinctive and asymmetric identity features",
        )
        anchors.extend(defaults[len(anchors) :])
        return WorkbenchRequest(
            role_id=profile.profile_id,
            display_name=profile.display_name,
            input_mode=profile.input.kind,
            presentation=profile.visual.presentation,
            style_card="custom",
            silhouette=profile.visual.silhouette,
            detail=profile.visual.description,
            anchors=tuple(anchors[:3]),
            image_path=str(image_path) if profile.input.kind == "image" else "",
            candidate_count=1,
            negative_detail=profile.visual.negative_description,
            style_and_mood=profile.visual.style_and_mood,
            palette=profile.visual.palette,
            hair=profile.visual.hair,
            eyes=profile.visual.eyes,
            face=profile.visual.face,
            clothing=profile.visual.clothing,
            accessories=profile.visual.accessories,
            special_features=profile.visual.special_features,
        )

    def _open_selected_role(self) -> None:
        if self._worker is not None:
            return
        selected = self.edit_role_selector.currentData()
        if not self._confirm_leave_unsaved_result("切换角色"):
            return
        if selected is None:
            self._reset_new_role()
            return
        if not isinstance(selected, PackageKey):
            return
        if not bool(self.edit_role_selector.currentData(_EDITABLE_ROLE_STATUS_ROLE)):
            QMessageBox.information(
                self,
                "这个角色暂不可编辑",
                str(
                    self.edit_role_selector.currentData(_EDITABLE_ROLE_REASON_ROLE)
                    or "这个角色缺少桌宠工坊的可编辑档案。"
                )
                + "\n\n角色仍可正常使用；如需编辑，请从原始设定或参考图重新创建。",
            )
            return
        try:
            self.open_installed_role(selected)
        except (OSError, RoleContractError, ValueError) as exc:
            QMessageBox.critical(
                self,
                "无法编辑这个角色",
                f"{exc}\n\n只有同时保留可编辑形象档案和受管身份图的 v2 角色可继续编辑。",
            )

    def _start_selected_appearance_revision(self) -> None:
        if self._worker is not None:
            return
        selected = self.edit_role_selector.currentData()
        if not isinstance(selected, PackageKey):
            return
        if not bool(self.edit_role_selector.currentData(_EDITABLE_ROLE_STATUS_ROLE)):
            QMessageBox.information(
                self,
                "这个角色暂不能重新设计",
                str(
                    self.edit_role_selector.currentData(_EDITABLE_ROLE_REASON_ROLE)
                    or "这个角色缺少桌宠工坊的可编辑档案。"
                )
                + "\n\n旧角色不会受到影响；可从原始设定或参考图创建新角色。",
            )
            return
        if not self._confirm_leave_unsaved_result("修改其它角色的形象"):
            return
        try:
            self.start_appearance_revision(selected)
        except (OSError, RoleContractError, ValueError) as exc:
            QMessageBox.critical(
                self,
                "无法修改这个角色的形象",
                f"{exc}\n\n旧版本不会受到影响。",
            )

    def _editable_role_context(
        self, key: PackageKey
    ) -> tuple[InstalledRole, CharacterProfile, Path, Path, WorkbenchRequest]:
        installed = self._role_library.get(key)
        package = installed.package
        if package.source_schema_version != 2 or not package.installable:
            raise ValueError("只能编辑已通过审查的 v2 角色包")
        try:
            profile = self._draft_store.load_profile_revision(
                key.role_id, package.appearance_revision
            )
        except RoleContractError as exc:
            raise ValueError(
                "缺少与这个版本匹配的桌宠工坊形象档案。"
            ) from exc
        identity_path = self._draft_store.identity_path(package.identity_sha256)
        if identity_path is None:
            raise ValueError("受管身份图缺失、已损坏或与角色包不匹配。")
        input_path = self._draft_store.input_path(profile.input)
        if profile.input.kind == "image" and input_path is None:
            raise ValueError("原始参考图缺失或已损坏。")
        request = self._profile_request(profile, input_path or identity_path)
        request.validate()
        return installed, profile, identity_path, input_path or identity_path, request

    def _role_editability(self, key: PackageKey) -> tuple[bool, str]:
        """Describe local edit capability without exposing contract internals."""
        try:
            self._editable_role_context(key)
        except (OSError, RoleContractError, ValueError):
            try:
                installed = self._role_library.get(key)
                package = installed.package
                self._draft_store.load_profile_revision(
                    key.role_id, package.appearance_revision
                )
            except (OSError, RoleContractError, ValueError):
                return False, "缺少与这个版本匹配的桌宠工坊形象档案。"
            if self._draft_store.identity_path(package.identity_sha256) is None:
                return False, "这个版本的受管身份图缺失或已损坏。"
            return False, "这个版本的可编辑资料不完整或已损坏。"
        return True, ""

    def _populate_request_fields(
        self, request: WorkbenchRequest, input_path: Path
    ) -> None:
        self.role_id.setText(request.role_id)
        self.display_name.setText(request.display_name)
        self._check_group_value(self.presentation_group, request.presentation)
        self._check_group_value(self.silhouette_group, request.silhouette)
        self.style_text.setText(request.style_and_mood)
        self.palette.setText(request.palette)
        self.hair.setText(request.hair)
        self.eyes.setText(request.eyes)
        self.face.setText(request.face)
        self.outfit.setText(request.clothing)
        self.signature.setText(request.accessories)
        self.other_features.setText(request.special_features)
        self.avoid.setText(request.negative_detail)
        self.must_preserve.setText(request.special_features)
        self._selected_image_path = (
            str(input_path) if request.input_mode == "image" else ""
        )
        self.image_path.setText(input_path.name if request.input_mode == "image" else "")
        self.input_tabs.setCurrentIndex(0 if request.input_mode == "text" else 1)

    def open_installed_role(self, key: PackageKey) -> None:
        """Open one managed v2 role without copying or mutating its package."""
        installed, _profile, identity_path, input_path, request = (
            self._editable_role_context(key)
        )
        package = installed.package

        self._editing_key = key
        self._candidate_task_id = ""
        self._package_task_id = ""
        self._appearance_source_key = None
        self._target_appearance_revision = None
        self._revision_pending_install = False
        self._package_key = key
        self._result_saved = True
        self._active_request = request
        self._package_result = PackageResult(installed.root, 0.0)
        self._populate_request_fields(request, input_path)
        self.role_id.setReadOnly(True)
        self.display_name.setReadOnly(True)
        self.input_tabs.setEnabled(False)
        self.prepare_button.setText("已锁定当前形象")
        self.prepare_button.setEnabled(False)

        existing_actions = {action for action, _ in package.actions}
        for action, option in self.action_checks.items():
            option.setChecked(False)
            option.setEnabled(True)
            mode = "替换" if action in existing_actions else "新增"
            option.setText(f"{_ACTION_LABELS[action]}（{mode}）")

        session = self._backend.storage_root / (
            f"{key.role_id}-edit-v{key.package_version}-{uuid.uuid4().hex[:8]}"
        )
        session.mkdir(parents=True, exist_ok=False)
        self._candidate_result = CandidateResult(session, (identity_path,), 0.0)
        self.candidates.clear()
        pixmap = QPixmap(str(identity_path))
        item = QListWidgetItem(QIcon(pixmap), "当前身份图")
        item.setData(Qt.UserRole, str(identity_path))
        self.candidates.addItem(item)
        self.candidates.setCurrentRow(0)

        self._preview_root = session / "source-previews"
        _make_v2_action_previews(
            installed.root,
            tuple(action for action, _ in package.actions),
            self._preview_root,
        )
        self._populate_package_actions(self._package_result, self._preview_root)
        self.save_button.setEnabled(True)
        self._configure_action_picker(editing=True)
        self._update_generation_selection()
        self._update_install_enabled()
        self._set_ui_stage(2)
        self.status.setText(
            f"正在编辑 {package.display_name} v{key.package_version}。"
            "勾选要新增或替换的动作；未勾选动作原样保留。"
        )

    def start_appearance_revision(self, key: PackageKey) -> None:
        """Start a new identity-bound revision while retaining the source package."""
        installed, profile, _identity_path, input_path, request = (
            self._editable_role_context(key)
        )
        target_revision = self._draft_store.next_appearance_revision(key.role_id)
        if target_revision <= profile.appearance_revision:
            raise ValueError("无法分配新的形象档案修订号")

        self._editing_key = None
        self._candidate_task_id = ""
        self._package_task_id = ""
        self._appearance_source_key = key
        self._target_appearance_revision = target_revision
        self._revision_pending_install = False
        self._package_key = None
        self._result_saved = True
        self._active_request = None
        self._candidate_result = None
        self._package_result = None
        self._preview_root = None
        self._populate_request_fields(request, input_path)
        self.role_id.setReadOnly(True)
        self.display_name.setReadOnly(False)
        self.input_tabs.setEnabled(True)
        self.prepare_button.setEnabled(self._generation_available)
        for action, option in self.action_checks.items():
            option.setText(f"{_ACTION_LABELS[action]}（新形象）")
            option.setChecked(action == "idle")
            option.setEnabled(action != "idle")
        self._configure_action_picker(editing=False)
        self.candidates.clear()
        self._clear_action_previews()
        self.preview.setPixmap(QPixmap())
        self.preview.setText("先生成并选择新的身份图")
        self.save_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self._set_ui_stage(1)
        self._mode_changed(self.input_tabs.currentIndex())
        self._update_generation_selection()
        self.status.setText(
            f"正在基于 {installed.package.display_name} v{key.package_version} "
            f"创建形象修订 r{target_revision}。新形象至少重新生成待机；"
            "旧角色包及其动作不会被复用或改写。"
        )

    def _reset_new_role(self) -> None:
        self._editing_key = None
        self._candidate_task_id = ""
        self._package_task_id = ""
        self._appearance_source_key = None
        self._target_appearance_revision = None
        self._revision_pending_install = False
        self._package_key = None
        self._result_saved = True
        self._active_request = None
        self._candidate_result = None
        self._package_result = None
        self._preview_root = None
        self.role_id.setReadOnly(True)
        self.role_id.setText(_new_role_id())
        self.display_name.setReadOnly(False)
        self.display_name.setText("我的桌宠")
        self.input_tabs.setEnabled(True)
        self.input_tabs.setCurrentIndex(0)
        self.prepare_button.setText("生成身份候选")
        self.prepare_button.setEnabled(self._generation_available)
        for action, option in self.action_checks.items():
            option.setText(_ACTION_LABELS[action])
            option.setChecked(action == "idle")
            option.setEnabled(action != "idle")
        self._configure_action_picker(editing=False)
        self.candidates.clear()
        self._clear_action_previews()
        self.preview.setPixmap(QPixmap())
        self.preview.setText("动作生成后可在这里连续预览")
        self.save_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self._set_ui_stage(1)
        self._update_generation_selection()
        self._update_action_emphasis()
        self.status.setText("先填写角色设定，然后准备身份候选。")

    def request(self) -> WorkbenchRequest:
        mode = "text" if self.input_tabs.currentIndex() == 0 else "image"
        if mode == "text":
            details = self._compiled_text_details()
            if self._detail_character_count() > _TEXT_DETAIL_LIMIT:
                raise ValueError(
                    f"外观细节总计不得超过 {_TEXT_DETAIL_LIMIT} 个字符"
                )
            anchors = self._text_identity_anchors()
            presentation = self._checked_option(self.presentation_group)
            silhouette = self._checked_option(self.silhouette_group)
            candidate_count = self.candidate_count.value()
        else:
            preserve = self.must_preserve.text().strip()
            details = ""
            anchors = (
                "keep the exact hair, eyes and face in the identity image",
                "keep the exact outfit, proportions and palette",
                (
                    f"must preserve: {preserve}"
                    if preserve
                    else "keep all distinctive and asymmetric identity features"
                ),
            )
            presentation = "neutral"
            silhouette = "petite"
            candidate_count = 1
        request = WorkbenchRequest(
            role_id=self.role_id.text().strip(),
            display_name=self.display_name.text().strip(),
            input_mode=mode,
            presentation=presentation,
            style_card="custom",
            silhouette=silhouette,
            detail=details,
            anchors=anchors,
            image_path=(self._selected_image_path or self.image_path.text()).strip(),
            candidate_count=candidate_count,
            negative_detail=self.avoid.text().strip() if mode == "text" else "",
            style_and_mood=self.style_text.text().strip() if mode == "text" else "",
            palette=self.palette.text().strip() if mode == "text" else "",
            hair=self.hair.text().strip() if mode == "text" else "",
            eyes=self.eyes.text().strip() if mode == "text" else "",
            face=self.face.text().strip() if mode == "text" else "",
            clothing=self.outfit.text().strip() if mode == "text" else "",
            accessories=self.signature.text().strip() if mode == "text" else "",
            special_features=(
                self.other_features.text().strip() if mode == "text" else preserve
            ),
        )
        request.validate()
        return request

    def _selected_actions(self) -> tuple[str, ...]:
        return tuple(
            action
            for action in _WORKBENCH_ACTIONS
            if self.action_checks[action].isChecked()
        )

    def _update_generation_selection(self) -> None:
        count = len(self._selected_actions())
        self._update_action_options_summary()
        if self._editing_key is not None:
            next_version = self._role_library.next_version(self._editing_key.role_id)
            self.generate_button.setText(f"生成 {count} 个动作并创建 v{next_version}")
            self.generate_button.setEnabled(
                self._generation_available
                and self._worker is None
                and self.candidates.currentItem() is not None
                and count > 0
                and not self._revision_pending_install
            )
            return
        if self._appearance_source_key is not None:
            next_version = self._role_library.next_version(
                self._appearance_source_key.role_id
            )
            self.generate_button.setText(
                f"生成新形象的 {count} 个动作并创建 v{next_version}"
            )
            self.generate_button.setEnabled(
                self._generation_available
                and self._worker is None
                and self.candidates.currentItem() is not None
                and count > 0
                and not self._revision_pending_install
            )
            return
        self.generate_button.setText(
            "生成待机动作并创建桌宠"
            if count == 1
            else f"生成待机和 {count - 1} 个互动动作"
        )

    @staticmethod
    def _checked_option(group: QButtonGroup) -> str:
        option = group.checkedButton()
        if option is None:
            raise ValueError("请选择一个角色选项")
        return str(option.property("optionValue"))

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.advanced_panel.setVisible(checked)

    def _toggle_action_options(self, checked: bool) -> None:
        self.action_options_toggle.setArrowType(
            Qt.DownArrow if checked else Qt.RightArrow
        )
        self.action_options_panel.setVisible(checked)

    def _configure_action_picker(self, *, editing: bool) -> None:
        self.action_heading.setText(
            "补充或替换动作" if editing else "创建可运行桌宠"
        )
        self.action_fallback_hint.setText(
            "勾选要新增或替换的动作；未勾选动作原样保留。"
            if editing
            else (
                "待机是唯一必需动作。先生成它即可安装使用；"
                "点击、拖动和边缘探头等互动可以现在添加，也可安装后再补。"
            )
        )
        self.action_checks["idle"].setVisible(editing)
        self.action_options_toggle.setChecked(editing)
        self._update_action_options_summary()

    def _update_action_options_summary(self) -> None:
        selected = len(self._selected_actions())
        if self._editing_key is not None:
            self.action_options_toggle.setText(f"选择动作（已选 {selected} 个）")
            self.action_credit_hint.setText(
                f"本次将使用 {selected} 次动作生成次数" if selected else "请选择至少一个动作"
            )
            return
        extras = max(0, selected - 1)
        suffix = f"（已选 {extras} 个）" if extras else "（可选）"
        self.action_options_toggle.setText(f"同时生成更多互动{suffix}")
        self.action_credit_hint.setText(f"本次将使用 {selected} 次动作生成次数")

    def _mode_changed(self, index: int) -> None:
        if index == 1 and self.advanced_toggle.isChecked():
            self.advanced_toggle.setChecked(False)
        prefix = "生成新形象候选" if self._appearance_source_key is not None else "生成身份候选"
        image_text = (
            "使用新的身份图"
            if self._appearance_source_key is not None
            else "使用这张身份图"
        )
        self.prepare_button.setText(prefix if index == 0 else image_text)

    def _detail_character_count(self) -> int:
        return sum(len(editor.text().strip()) for editor in self._detail_editors)

    def _update_detail_budget(self) -> None:
        used = self._detail_character_count()
        self.detail_budget.setText(f"描述长度 {used}/{_TEXT_DETAIL_LIMIT}")
        invalid = used > _TEXT_DETAIL_LIMIT
        self.detail_budget.setStyleSheet("color: #b42318;" if invalid else "")

    def _compiled_text_details(self) -> str:
        values = (
            ("Character name and concept", self.display_name.text()),
            ("Visual style and mood", self.style_text.text()),
            ("Main color palette", self.palette.text()),
            ("Hair", self.hair.text()),
            ("Eyes", self.eyes.text()),
            ("Face", self.face.text()),
            ("Outfit", self.outfit.text()),
            ("Signature accessories", self.signature.text()),
            ("Special features", self.other_features.text()),
        )
        detail = "; ".join(
            f"{label}: {value.strip()}" for label, value in values if value.strip()
        )
        return detail

    def _text_identity_anchors(self) -> tuple[str, str, str]:
        head = "consistent clearly defined face, hair and eyes"
        outfit = "consistent clearly defined outfit, proportions and palette"
        signature = self.signature.text().strip() or (
            "consistent distinctive and asymmetric identity features"
        )
        return head, outfit, signature

    def _choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择角色图片",
            "",
            "角色图片 (*.png *.jpg *.jpeg *.webp)",
        )
        if path:
            self._selected_image_path = path
            self.image_path.setText(Path(path).name)

    @staticmethod
    def _request_digest(request: WorkbenchRequest) -> str:
        payload = json.dumps(
            {
                "profile": request.to_profile().to_dict(),
                "candidate_count": request.candidate_count,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    @staticmethod
    def _write_task_result_metadata(
        destination: Path,
        *,
        result_kind: str,
        spent_cny: float,
        candidates: tuple[Path, ...] = (),
        package_root: Path | None = None,
    ) -> None:
        value: dict[str, Any] = {
            "schema_version": 1,
            "result_kind": result_kind,
            "spent_cny": float(spent_cny),
        }
        if candidates:
            value["candidates"] = [
                path.relative_to(destination).as_posix() for path in candidates
            ]
        if package_root is not None:
            value["package_root"] = package_root.relative_to(destination).as_posix()
        (destination / ".workbench-result.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _read_task_result(
        artifact_root: Path, context: _WorkbenchTaskContext
    ) -> CandidateResult | PackageResult:
        try:
            value = json.loads(
                (artifact_root / ".workbench-result.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("已发布的工作台任务结果缺少有效元数据") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("已发布的工作台任务结果版本无效")
        if value.get("result_kind") != context.result_kind:
            raise ValueError("工作台任务结果类型与任务上下文不一致")
        spent_cny = float(value.get("spent_cny", 0.0))
        if context.result_kind == "candidates":
            raw_candidates = value.get("candidates")
            if not isinstance(raw_candidates, list) or not raw_candidates:
                raise ValueError("身份候选任务没有可恢复的图片")
            resolved_root = artifact_root.resolve()
            candidates = tuple(
                (artifact_root / Path(str(relative))).resolve()
                for relative in raw_candidates
            )
            if any(
                not path.is_file() or not path.is_relative_to(resolved_root)
                for path in candidates
            ):
                raise ValueError("身份候选任务引用了无效文件")
            return CandidateResult(artifact_root, candidates, spent_cny)
        relative_root = Path(str(value.get("package_root", "")))
        resolved_root = artifact_root.resolve()
        package_root = (artifact_root / relative_root).resolve()
        if not package_root.is_relative_to(resolved_root) or not (
            package_root / "role.json"
        ).is_file():
            raise ValueError("角色包任务没有可恢复的结果")
        return PackageResult(package_root, spent_cny)

    def _build_task_artifact(
        self,
        context: _WorkbenchTaskContext,
        destination: Path,
        progress: ProgressCallback,
        *,
        identity_override: Path | None = None,
        source_package_override: Path | None = None,
    ) -> None:
        request = context.request
        if context.operation == "identity_candidates":
            generated = self._backend.prepare_candidates(request, progress)
            shutil.copytree(generated.session_root, destination)
            candidates = tuple(
                destination / path.relative_to(generated.session_root)
                for path in generated.candidates
            )
            self._write_task_result_metadata(
                destination,
                result_kind="candidates",
                spent_cny=generated.spent_cny,
                candidates=candidates,
            )
            return

        source_session: Path | None = None
        if identity_override is not None:
            destination.mkdir(parents=True, exist_ok=True)
            candidates_root = destination / "candidates"
            candidates_root.mkdir(exist_ok=True)
            candidate = candidates_root / "managed-identity.png"
            shutil.copy2(identity_override, candidate)
        elif context.source_task_id:
            source_session = self._task_artifacts.result(context.source_task_id)
            shutil.copytree(source_session, destination)
            candidate = destination / Path(context.candidate_relative_path)
        else:
            if context.source_package is None:
                raise ValueError("动作修订任务缺少来源角色包")
            destination.mkdir(parents=True)
            candidates_root = destination / "candidates"
            candidates_root.mkdir()
            candidate = candidates_root / "managed-identity.png"
            source = self._role_library.get(context.source_package)
            managed_identity = self._draft_store.identity_path(
                source.package.identity_sha256
            )
            if managed_identity is None:
                raise ValueError("动作修订任务的受管身份图已缺失或损坏")
            shutil.copy2(managed_identity, candidate)

        candidate = candidate.resolve()
        if not candidate.is_file() or not candidate.is_relative_to(
            destination.resolve()
        ):
            raise ValueError("工作台任务的身份候选路径无效")

        if context.operation == "initial_package":
            generated_package = self._backend.generate_package(
                request, candidate, destination, progress, context.actions
            )
        elif context.operation == "appearance_revision":
            if context.package_version is None:
                raise ValueError("形象修订任务缺少角色包版本")
            generated_package = self._backend.generate_appearance_revision(
                request,
                candidate,
                destination,
                context.package_version,
                context.appearance_revision,
                context.actions,
                progress,
            )
        elif context.operation == "action_revision" and context.source_package:
            if context.package_version is None:
                raise ValueError("动作修订任务缺少角色包版本")
            source_root = (
                source_package_override
                if source_package_override is not None
                else self._role_library.get(context.source_package).root
            )
            generated_package = self._backend.revise_installed_package(
                request,
                candidate,
                source_root,
                destination,
                context.package_version,
                context.actions,
                context.instruction,
                progress,
                accepted_direction_sources=frozenset(
                    context.accepted_direction_sources
                ),
            )
        elif context.operation == "action_revision":
            generated_package = self._backend.regenerate_actions(
                request,
                candidate,
                destination,
                context.actions,
                context.instruction,
                progress,
                accepted_direction_sources=frozenset(
                    context.accepted_direction_sources
                ),
            )
        else:
            raise ValueError("工作台任务操作无法执行")
        published_package_root = generated_package.package_root
        if not published_package_root.resolve().is_relative_to(destination.resolve()):
            if source_session is None or not published_package_root.resolve().is_relative_to(
                source_session.resolve()
            ):
                raise ValueError("工作台后端返回了任务目录之外的角色包")
            published_package_root = destination / published_package_root.relative_to(
                source_session
            )
        self._write_task_result_metadata(
            destination,
            result_kind="package",
            spent_cny=generated_package.spent_cny,
            package_root=published_package_root,
        )

    @staticmethod
    def _service_request_from_profile(
        envelope: RoleServiceRequest,
        *,
        image_path: Path | None,
    ) -> WorkbenchRequest:
        visual = envelope.profile.visual
        anchors = list(visual.identity_anchors[:3])
        defaults = (
            "consistent clearly defined face, hair and eyes",
            "consistent clearly defined outfit, proportions and palette",
            "keep all distinctive and asymmetric identity features",
        )
        while len(anchors) < 3:
            anchors.append(defaults[len(anchors)])
        request = WorkbenchRequest(
            role_id=envelope.profile.profile_id,
            display_name=envelope.profile.display_name,
            input_mode=envelope.profile.input.kind,
            presentation=visual.presentation,
            style_card="custom",
            silhouette=visual.silhouette,
            detail=visual.description,
            anchors=tuple(anchors),  # type: ignore[arg-type]
            image_path=str(image_path or ""),
            candidate_count=envelope.candidate_count,
            negative_detail=visual.negative_description,
            style_and_mood=visual.style_and_mood,
            palette=visual.palette,
            hair=visual.hair,
            eyes=visual.eyes,
            face=visual.face,
            clothing=visual.clothing,
            accessories=visual.accessories,
            special_features=visual.special_features,
        )
        request.validate()
        return request

    def _source_package_root(self, context: _WorkbenchTaskContext) -> Path:
        if context.source_package is not None:
            return self._role_library.get(context.source_package).root
        if not context.source_task_id:
            raise ValueError("动作修订任务缺少来源角色包")
        source_context = self._task_contexts.load(context.source_task_id)
        source_result = self._read_task_result(
            self._task_artifacts.result(context.source_task_id), source_context
        )
        if not isinstance(source_result, PackageResult):
            raise ValueError("动作修订任务的来源不是角色包")
        return source_result.package_root

    def _create_service_package_archive(
        self, local_task_id: str, source_root: Path
    ) -> Path:
        package = RolePackage.from_json(
            (source_root / "role.json").read_text(encoding="utf-8")
        )
        members = ["role.json"] + [
            frame.path for _action, definition in package.actions for frame in definition.frames
        ]
        output = (
            self._backend.storage_root
            / "state"
            / "service-uploads"
            / f"{local_task_id}.moeguard-role"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                for relative in members:
                    source = source_root / Path(relative)
                    info = ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, source.read_bytes())
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return output

    def _prepare_service_request(
        self, local_task_id: str, context: _WorkbenchTaskContext
    ) -> RoleServiceRequest:
        if self._service_client is None:
            raise ValueError("工作台服务客户端尚未配置")
        existing = self._service_client.request_store.load(local_task_id)
        if existing is not None:
            return existing
        record = self._task_store.load(local_task_id)
        profile = context.request.to_profile(
            appearance_revision=context.appearance_revision
        )
        expires_at = int(time.time()) + 7 * 24 * 60 * 60
        assets = []
        if profile.input.kind == "image":
            input_path = self._draft_store.input_path(profile.input)
            if input_path is None:
                raise ValueError("远端任务所需的受管参考图已缺失或损坏")
            input_asset = self._service_client.upload_asset(
                input_path,
                purpose="input_image",
                media_type=profile.input.media_type,
                expires_at=expires_at,
            )
            assets.append(input_asset)
            profile = replace(
                profile,
                input=ProfileInput(
                    kind="image",
                    sha256=input_asset.sha256,
                    media_type=input_asset.media_type,
                ),
            )
        if record.spec.operation != "identity_candidates":
            identity_path = self._draft_store.identity_path(record.spec.identity_sha256)
            if identity_path is None:
                raise ValueError("远端任务所需的受管身份图已缺失或损坏")
            assets.append(
                self._service_client.upload_asset(
                    identity_path,
                    purpose="identity_image",
                    media_type="image/png",
                    expires_at=expires_at,
                )
            )
        if record.spec.operation == "action_revision":
            archive = self._create_service_package_archive(
                local_task_id, self._source_package_root(context)
            )
            assets.append(
                self._service_client.upload_asset(
                    archive,
                    purpose="source_package",
                    media_type="application/vnd.moeguard.role+zip",
                    expires_at=expires_at,
                )
            )
        return self._service_client.request_store.save_once(
            local_task_id,
            RoleServiceRequest(
                spec=record.spec,
                profile=profile,
                candidate_count=context.request.candidate_count,
                assets=tuple(assets),
                revision_instruction=context.instruction,
                accepted_direction_sources=context.accepted_direction_sources,
                client_runtime=self._client_runtime,
            ),
        )

    def _execute_persistent_task(
        self, local_task_id: str, progress: ProgressCallback
    ) -> CandidateResult | PackageResult:
        context = self._task_contexts.load(local_task_id)
        record = self._task_store.load(local_task_id)
        if record.spec.operation != context.operation:
            raise ValueError("任务日志与工作台上下文操作不一致")
        if record.status == "cancel_requested":
            self._task_store.acknowledge_cancel(local_task_id)
            raise _BackendCancelled
        if record.status == "cancelled":
            raise _BackendCancelled

        if self._service_client is not None:
            return self._execute_service_task(local_task_id, context, progress)

        def journal_progress(message: str, percent: int) -> None:
            current = self._task_store.load(local_task_id)
            if current.status == "cancel_requested":
                raise _BackendCancelled
            if current.status == "running":
                next_progress = max(current.progress, min(99, max(0, percent)))
                self._task_store.update_progress(local_task_id, next_progress)
            progress(message, percent)

        artifact_root = self._task_artifacts.build(
            local_task_id,
            lambda destination: self._build_task_artifact(
                context, destination, journal_progress
            ),
        )
        self._task_artifacts.accept_completion(
            local_task_id,
            callback_id=f"workbench-complete-{local_task_id[:32]}",
        )
        return self._read_task_result(artifact_root, context)

    def _execute_service_task(
        self,
        local_task_id: str,
        context: _WorkbenchTaskContext,
        progress: ProgressCallback,
    ) -> CandidateResult | PackageResult:
        if self._service_client is None or self._service_transport is None:
            raise ValueError("工作台服务客户端尚未配置")
        service_request = self._prepare_service_request(local_task_id, context)
        if self._task_store.load(local_task_id).status == "cancel_requested":
            self._task_store.acknowledge_cancel(local_task_id)
            raise _BackendCancelled
        snapshot = self._service_client.ensure_submitted(
            local_task_id, service_request
        )
        progress(f"任务 {local_task_id[:8]}… 已提交，正在查询同一远端任务…", 2)

        if self._local_service_executor is not None and snapshot.status in {
            "queued",
            "running",
        }:
            self._local_service_executor(
                self,
                local_task_id,
                context,
                service_request,
                snapshot,
                progress,
            )

        while True:
            record = self._service_client.poll(local_task_id)
            if record.status == "succeeded":
                artifact_root = self._task_artifacts.result(local_task_id)
                return self._read_task_result(artifact_root, context)
            if record.status == "cancelled":
                raise _BackendCancelled
            if record.status == "failed":
                raise RuntimeError(
                    f"远端任务未完成：{record.error_code or 'service_task_failed'}"
                )
            progress(
                f"正在查询远端任务 {local_task_id[:8]}…；不会重复提交",
                record.progress,
            )
            time.sleep(_SERVICE_POLL_SECONDS)

    def _start_persistent_task(
        self, spec: RoleTaskSpec, context: _WorkbenchTaskContext
    ) -> None:
        if self._worker is not None:
            return
        record, _created = self._task_store.create_or_load(
            spec,
            idempotency_key=f"workbench-{uuid.uuid4().hex}",
        )
        self._task_contexts.save(record.local_task_id, context)
        self._task_contexts.set_active(record.local_task_id)
        self._active_task_id = record.local_task_id
        self._active_task_context = context
        self._refresh_account_after_worker = callable(
            getattr(self._service_transport, "account_summary", None)
        )
        self._start_worker(
            lambda progress: self._execute_persistent_task(
                record.local_task_id, progress
            ),
            lambda value: self._persistent_task_ready(
                record.local_task_id, context, value
            ),
        )

    def _persistent_task_ready(
        self,
        local_task_id: str,
        context: _WorkbenchTaskContext,
        value: object,
    ) -> None:
        if context.result_kind == "candidates":
            if not isinstance(value, CandidateResult):
                self._on_failure("持久化任务返回了无效的身份候选")
                return
            self._candidate_task_id = local_task_id
            self._package_task_id = ""
            self._candidates_ready(value, context.request)
        else:
            if not isinstance(value, PackageResult):
                self._on_failure("持久化任务返回了无效的角色包")
                return
            self._package_task_id = local_task_id
            self._active_request = context.request
            try:
                artifact_root = self._task_artifacts.result(local_task_id)
                relative_candidate = (
                    context.candidate_relative_path
                    or "candidates/managed-identity.png"
                )
                candidate = (artifact_root / Path(relative_candidate)).resolve()
                if candidate.is_file() and candidate.is_relative_to(
                    artifact_root.resolve()
                ):
                    self._candidate_result = CandidateResult(
                        artifact_root, (candidate,), value.spent_cny
                    )
                    self.candidates.clear()
                    item = QListWidgetItem(QIcon(QPixmap(str(candidate))), "当前身份图")
                    item.setData(Qt.UserRole, str(candidate))
                    self.candidates.addItem(item)
                    self.candidates.setCurrentRow(0)
            except RoleContractError:
                pass
            if context.source_package is not None:
                if context.operation == "appearance_revision":
                    self._appearance_source_key = context.source_package
                    self._editing_key = None
                    self._target_appearance_revision = context.appearance_revision
                else:
                    self._editing_key = context.source_package
            self._package_ready(value)
        self._task_contexts.clear_active(local_task_id)
        self._active_task_id = ""
        self._active_task_context = None

    def _can_resume_active_task(self) -> bool:
        if not self._active_task_id:
            return False
        if self._service_client is not None:
            try:
                if bool(
                    self._service_client.binding_store.load(self._active_task_id)
                    or self._service_client.request_store.load(self._active_task_id)
                ):
                    return True
                record = self._task_store.load(self._active_task_id)
                self._task_contexts.load(self._active_task_id)
                return record.status in {"queued", "running"} or (
                    record.status == "failed" and record.retryable
                )
            except (RoleContractError, ValueError):
                return False
        if self._backend.is_fake:
            return True
        try:
            return self._task_artifacts.has_published(self._active_task_id)
        except RoleContractError:
            return False

    def _restore_pending_task(self) -> None:
        local_task_id = self._task_contexts.active()
        if not local_task_id:
            return
        try:
            record = self._task_store.load(local_task_id)
            context = self._task_contexts.load(local_task_id)
        except (RoleContractError, ValueError):
            self._task_contexts.clear_active(local_task_id)
            self.status.setText("发现损坏的未完成任务记录；已停止恢复，未发起请求。")
            return
        if record.status == "cancel_requested":
            self._task_store.acknowledge_cancel(local_task_id)
            self._task_contexts.clear_active(local_task_id)
            self.status.setText("上次取消请求已确认；没有恢复或重提远程任务。")
            return
        if record.status == "cancelled" or (
            record.status == "failed" and not record.retryable
        ):
            self._task_contexts.clear_active(local_task_id)
            return

        self._active_task_id = local_task_id
        self._active_task_context = context
        self._set_ui_stage(1 if context.result_kind == "candidates" else 2)
        self._populate_request_fields(
            context.request, Path(context.request.image_path or ".")
        )
        for action, option in self.action_checks.items():
            option.setChecked(action in context.actions or (
                context.result_kind == "candidates" and action == "idle"
            ))
        if record.status == "succeeded":
            try:
                value = self._read_task_result(
                    self._task_artifacts.result(local_task_id), context
                )
                self._persistent_task_ready(local_task_id, context, value)
            except (RoleContractError, ValueError) as exc:
                self.status.setText(f"已完成任务的本地结果无法恢复：{exc}")
            return
        if self._can_resume_active_task():
            self.resume_task_button.setEnabled(self._generation_available)
            self.status.setText(
                f"发现未完成任务 {local_task_id[:8]}…；可从同一任务恢复，"
                "不会创建新的计费请求。"
            )
        else:
            self.resume_task_button.setEnabled(False)
            self.status.setText(
                f"发现远程任务 {local_task_id[:8]}…；当前客户端尚不能安全轮询"
                "该 provider task，因此已禁止重提。"
            )

    def _set_busy(self, busy: bool) -> None:
        if busy:
            random.shuffle(self._activity_faces)
            self._rotate_activity_indicator()
            self.activity_indicator.show()
            self._activity_timer.start()
        else:
            self._activity_timer.stop()
            self.activity_indicator.hide()
        if self.account_refresh_button is not None:
            self.account_refresh_button.setEnabled(not busy)
        if self.redeem_credit_button is not None:
            self.redeem_credit_button.setEnabled(not busy)
        if self.disconnect_service_button is not None:
            self.disconnect_service_button.setEnabled(not busy)
        self.new_role_mode.setEnabled(not busy)
        self.edit_role_mode.setEnabled(
            not busy and self.edit_role_selector.count() > 1
        )
        self.edit_role_selector.setEnabled(not busy)
        selected_role_editable = bool(
            self.edit_role_selector.currentData(_EDITABLE_ROLE_STATUS_ROLE)
        )
        self.open_role_button.setEnabled(
            not busy
            and isinstance(self.edit_role_selector.currentData(), PackageKey)
            and selected_role_editable
        )
        self.appearance_button.setEnabled(
            not busy
            and isinstance(self.edit_role_selector.currentData(), PackageKey)
            and selected_role_editable
        )
        self.prepare_button.setEnabled(
            self._generation_available and not busy and self._editing_key is None
        )
        self.cancel_task_button.setEnabled(busy)
        self.resume_task_button.setEnabled(
            self._generation_available
            and not busy
            and (
                self._can_resume_active_task()
                or (
                    self._backend.is_fake
                    and self._retry_operation is not None
                    and self._retry_success is not None
                )
            )
        )
        for action, option in self.action_checks.items():
            option.setEnabled(
                not busy
                and not self._revision_pending_install
                and (self._editing_key is not None or action != "idle")
            )
        self.generate_button.setEnabled(
            self._generation_available
            and not busy
            and self.candidates.currentItem() is not None
            and (self._editing_key is None or bool(self._selected_actions()))
            and not self._revision_pending_install
        )
        self.save_button.setEnabled(not busy and self._package_result is not None)
        self._update_regenerate_enabled()
        self._update_install_enabled()
        self._update_action_emphasis()
        self._update_task_control_visibility()

    def _update_action_emphasis(self) -> None:
        """Keep the next valid step visually unambiguous."""
        can_generate = (
            self._worker is None
            and self.candidates.currentItem() is not None
            and self._package_result is None
            and not self._revision_pending_install
        )
        self.prepare_button.setStyleSheet(
            theme.button_qss("accent" if not can_generate else "normal")
        )
        self.generate_button.setStyleSheet(
            theme.button_qss("accent" if can_generate else "normal")
        )
        package_ready = self._package_result is not None
        if package_ready:
            self.save_hint.setText("角色包已就绪：可以导出，也可以直接保存并安装。")
            self.save_button.setToolTip("将完整角色包导出到你选择的位置")
            self.install_button.setToolTip("保存角色包并立即切换为当前桌宠")
        else:
            self.save_hint.setText("完成待机动作后，即可保存角色包或安装到萌卫。")
            disabled_reason = "请先选择身份候选并生成至少一个待机动作"
            self.save_button.setToolTip(disabled_reason)
            self.install_button.setToolTip(disabled_reason)

    def _update_regenerate_enabled(self) -> None:
        selected_actions = {
            str(item.data(Qt.UserRole))
            for item in self.action_selector.selectedItems()
        }
        has_directional_action = bool(
            selected_actions.intersection(_DIRECTIONAL_OPPOSITES)
        )
        self.regenerate_button.setText(
            "确定性修复所选方向"
            if has_directional_action
            else "重新生成所选动作"
        )
        self.revision_instruction.setPlaceholderText(
            "可补充幅度或表情；左右方向由系统自动约束（可选）"
            if has_directional_action
            else "选中动作的修改要求（可选）"
        )
        self.regenerate_button.setEnabled(
            self._generation_available
            and self._worker is None
            and self._package_result is not None
            and bool(selected_actions)
            and not self._revision_pending_install
        )

    def _update_install_enabled(self) -> None:
        self.install_button.setEnabled(
            self._worker is None
            and self._package_result is not None
        )

    def _start_worker(
        self,
        operation: Callable[[ProgressCallback], object],
        success: Callable[[object], None],
        failure: Callable[[object], None] | None = None,
    ) -> None:
        if self._worker is not None:
            return
        self._current_operation = operation
        self._current_success = success
        worker = _BackendWorker(operation)
        self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.succeeded.connect(
            lambda value: self._on_operation_success(value, success)
        )
        worker.failed.connect(failure or self._on_failure)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(self._worker_finished)
        self._set_busy(True)
        worker.start()

    def _refresh_account_summary(
        self, _checked: bool = False, *, quiet: bool = False
    ) -> None:
        query = getattr(self._service_transport, "account_summary", None)
        if not callable(query) or self._worker is not None:
            return
        if self.account_summary_label is not None:
            self.account_summary_label.setText("正在刷新生成次数…")

        def operation(progress: ProgressCallback) -> object:
            if not quiet:
                progress("正在刷新生成次数…", 0)
            return query()

        def success(value: object) -> None:
            if not isinstance(value, RoleServiceAccountSummary):
                failure(ValueError("角色生成服务返回了无效次数信息"))
                return
            if self.account_summary_label is not None:
                self.account_summary_label.setText(_account_summary_text(value))

        def failure(error: object) -> None:
            if self.account_summary_label is not None:
                self.account_summary_label.setText(
                    f"次数刷新失败：{role_service_user_message(error)}"
                )

        self._start_worker(operation, success, failure)

    def _open_credit_dialog(self) -> None:
        if self._service_transport is None or self._worker is not None:
            return
        dialog = RoleCreditDialog(self._service_transport, self)
        dialog.exec()
        if dialog.redeemed_successfully:
            self.status.setText("兑换成功；生成次数已经到账。")
            self._refresh_account_summary(quiet=True)

    def _prepare_candidates(self) -> None:
        if not self._generation_available:
            return
        if self._editing_key is not None:
            return
        if not self._confirm_leave_unsaved_result("重新生成身份候选"):
            return
        try:
            request = self.request()
            draft = self._save_recoverable_draft(
                request,
                appearance_revision=self._target_appearance_revision or 1,
            )
            if request.input_mode == "image":
                managed_input = self._draft_store.input_path(draft.profile.input)
                if managed_input is None:
                    raise ValueError("无法保存受管参考图片")
                request = replace(request, image_path=str(managed_input))
                self._selected_image_path = str(managed_input)
        except (ValueError, RoleContractError) as exc:
            QMessageBox.warning(self, "角色设定还不完整", str(exc))
            return
        if self._generation_consumes_units() and request.input_mode == "text":
            count = request.candidate_count
            cost_text = (
                f"，预计成本约 ¥{count * 0.20:.2f}" if self._show_costs else ""
            )
            answer = QMessageBox.question(
                self,
                "确认生成身份候选？",
                f"将生成 {count} 张身份候选，使用 {count} 次立绘生成次数{cost_text}。是否继续？",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        self._candidate_result = None
        self._package_result = None
        self._package_key = None
        self._preview_root = None
        self._revision_pending_install = False
        self._active_request = None
        self.candidates.clear()
        self._clear_action_previews()
        self.preview.setText("正在准备身份候选…")
        context = _WorkbenchTaskContext(
            operation="identity_candidates",
            result_kind="candidates",
            request=request,
            appearance_revision=self._target_appearance_revision or 1,
        )
        self._start_persistent_task(
            RoleTaskSpec(
                operation="identity_candidates",
                profile_id=request.role_id,
                appearance_revision=context.appearance_revision,
                input_sha256=self._request_digest(request),
            ),
            context,
        )

    def _candidates_ready(self, value: object, request: WorkbenchRequest) -> None:
        if not isinstance(value, CandidateResult):
            self._on_failure("工作台后端返回了无效的候选结果")
            return
        self._candidate_result = value
        self._result_saved = False
        self._active_request = request
        for index, path in enumerate(value.candidates, start=1):
            pixmap = QPixmap(str(path))
            item = QListWidgetItem(QIcon(pixmap), f"候选 {index}")
            item.setData(Qt.UserRole, str(path))
            self.candidates.addItem(item)
        if self.candidates.count():
            self.candidates.setCurrentRow(0)
        self._set_ui_stage(2)
        suffix = (
            f"；当前任务累计 ¥{value.spent_cny:.2f}"
            if self._show_costs
            else ""
        )
        self._update_generation_selection()
        self.status.setText(
            f"身份候选已准备好{suffix}。下一步：确认选中的形象，"
            "然后生成待机动作；完成后即可保存或安装。"
        )

    def _candidate_selected(self, row: int) -> None:
        self.generate_button.setEnabled(
            self._generation_available
            and row >= 0
            and self._worker is None
            and (self._editing_key is None or bool(self._selected_actions()))
            and not self._revision_pending_install
        )
        item = self.candidates.item(row) if row >= 0 else None
        if item is None:
            self._update_action_emphasis()
            return
        pixmap = QPixmap(str(item.data(Qt.UserRole)))
        self.preview.setPixmap(
            pixmap.scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        self._update_generation_selection()
        self._update_action_emphasis()

    def _generate_package(self) -> None:
        if not self._generation_available:
            return
        if self._candidate_result is None or self.candidates.currentItem() is None:
            return
        if self._editing_key is not None:
            self._generate_installed_revision()
            return
        if self._appearance_source_key is not None:
            self._generate_appearance_revision()
            return
        try:
            request = self.request()
        except ValueError as exc:
            QMessageBox.warning(self, "角色设定已改变", str(exc))
            return
        if request != self._active_request:
            QMessageBox.warning(
                self,
                "角色设定已改变",
                "身份候选生成后设定发生了变化。请重新准备候选，避免动作与身份不一致。",
            )
            return
        actions = self._selected_actions()
        if self._generation_consumes_units():
            cost_line = (
                f"本轮预计成本约 ¥{len(actions) * 0.45:.2f}。\n"
                if self._show_costs
                else ""
            )
            answer = QMessageBox.warning(
                self,
                f"确认生成所选 {len(actions)} 个动作？",
                f"将生成 {len(actions)} 个桌宠动作，使用 {len(actions)} 次"
                "动作生成次数；只有待机是必选项。\n"
                f"{cost_line}"
                "任务可能持续数分钟；中断后必须恢复已有 task，不能直接重提。\n\n"
                "是否继续？",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        candidate = Path(str(self.candidates.currentItem().data(Qt.UserRole)))
        try:
            self._save_recoverable_draft(request, identity_path=candidate)
        except RoleContractError as exc:
            QMessageBox.critical(self, "无法保存角色草稿", str(exc))
            return
        if not self._candidate_task_id:
            QMessageBox.critical(
                self,
                "无法创建动作任务",
                "当前身份候选没有持久化任务来源，请重新准备身份候选。",
            )
            return
        try:
            candidate_relative = candidate.relative_to(
                self._candidate_result.session_root
            ).as_posix()
        except ValueError:
            QMessageBox.critical(self, "无法创建动作任务", "身份候选不属于当前任务")
            return
        self._package_result = None
        self._package_key = None
        self._preview_root = None
        self._revision_pending_install = False
        self._clear_action_previews()
        self.preview.setText(f"正在生成所选 {len(actions)} 个动作，请保持 MoeGuard 运行…")
        context = _WorkbenchTaskContext(
            operation="initial_package",
            result_kind="package",
            request=request,
            source_task_id=self._candidate_task_id,
            candidate_relative_path=candidate_relative,
            actions=actions,
            package_version=1,
        )
        self._start_persistent_task(
            RoleTaskSpec(
                operation="initial_package",
                profile_id=request.role_id,
                appearance_revision=1,
                actions=actions,
                package_version=1,
                input_sha256=self._request_digest(request),
                identity_sha256=sha256(
                    normalized_identity_png(candidate)
                ).hexdigest(),
            ),
            context,
        )

    def _generate_appearance_revision(self) -> None:
        if not self._generation_available:
            return
        if (
            self._appearance_source_key is None
            or self._target_appearance_revision is None
            or self._candidate_result is None
            or self._active_request is None
            or self.candidates.currentItem() is None
        ):
            return
        try:
            request = self.request()
        except ValueError as exc:
            QMessageBox.warning(self, "角色设定已改变", str(exc))
            return
        if request != self._active_request:
            QMessageBox.warning(
                self,
                "角色设定已改变",
                "身份候选生成后设定发生了变化。请重新准备候选，避免动作与身份不一致。",
            )
            return
        actions = self._selected_actions()
        if "idle" not in actions:
            return
        next_version = self._role_library.next_version(
            self._appearance_source_key.role_id
        )
        if self._generation_consumes_units():
            cost_line = (
                f"\n本轮预计成本约 ¥{len(actions) * 0.45:.2f}。"
                if self._show_costs
                else ""
            )
            answer = QMessageBox.warning(
                self,
                f"确认创建新形象 v{next_version}？",
                f"新形象将重新生成 {len(actions)} 个动作，使用 {len(actions)} 次"
                "动作生成次数，且至少包含待机。"
                f"{cost_line}\n旧形象及其动作会完整保留，可随时回滚。",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        candidate = Path(str(self.candidates.currentItem().data(Qt.UserRole)))
        try:
            self._save_recoverable_draft(
                request,
                identity_path=candidate,
                appearance_revision=self._target_appearance_revision,
            )
        except RoleContractError as exc:
            QMessageBox.critical(self, "无法保存新形象档案", str(exc))
            return
        self._package_result = None
        self._package_key = None
        self._preview_root = None
        self._clear_action_previews()
        self.preview.setText(
            f"正在生成新形象 r{self._target_appearance_revision} 的 "
            f"{len(actions)} 个动作并创建 v{next_version}…"
        )
        if not self._candidate_task_id:
            QMessageBox.critical(
                self,
                "无法创建形象修订任务",
                "当前身份候选没有持久化任务来源，请重新准备身份候选。",
            )
            return
        candidate_relative = candidate.relative_to(
            self._candidate_result.session_root
        ).as_posix()
        context = _WorkbenchTaskContext(
            operation="appearance_revision",
            result_kind="package",
            request=request,
            source_task_id=self._candidate_task_id,
            candidate_relative_path=candidate_relative,
            actions=actions,
            package_version=next_version,
            appearance_revision=self._target_appearance_revision or 1,
            source_package=self._appearance_source_key,
        )
        self._start_persistent_task(
            RoleTaskSpec(
                operation="appearance_revision",
                profile_id=request.role_id,
                appearance_revision=context.appearance_revision,
                actions=actions,
                package_version=next_version,
                input_sha256=self._request_digest(request),
                identity_sha256=sha256(
                    normalized_identity_png(candidate)
                ).hexdigest(),
            ),
            context,
        )

    def _generate_installed_revision(self) -> None:
        if not self._generation_available:
            return
        if (
            self._editing_key is None
            or self._candidate_result is None
            or self._active_request is None
            or self.candidates.currentItem() is None
        ):
            return
        actions = self._selected_actions()
        if not actions:
            return
        next_version = self._role_library.next_version(self._editing_key.role_id)
        source = self._role_library.get(self._editing_key)
        directional_targets = set(actions).intersection(_DIRECTIONAL_OPPOSITES)
        if len(directional_targets) > 1:
            QMessageBox.warning(
                self,
                "请逐个修复方向动作",
                "左右探头互为修复基准，请一次只选择其中一个方向。",
            )
            return
        accepted_direction_sources: frozenset[str] = frozenset()
        directional_source = ""
        if directional_targets:
            target_action = next(iter(directional_targets))
            directional_source = _DIRECTIONAL_OPPOSITES[target_action]
            source_actions = {action for action, _ in source.package.actions}
            quality_by_action = dict(source.package.quality.actions)
            source_quality = quality_by_action.get(directional_source)
            if (
                directional_source in source_actions
                and source_quality is not None
                and source_quality.status == "accepted"
                and source_quality.direction is not None
                and source_quality.direction.status == "accepted"
            ):
                accepted_direction_sources = frozenset({directional_source})
        if self._generation_consumes_units():
            cost_line = (
                f"\n本轮预计成本约 ¥{len(actions) * 0.45:.2f}。"
                if self._show_costs
                else ""
            )
            if directional_targets and accepted_direction_sources:
                target_action = next(iter(directional_targets))
                answer = QMessageBox.warning(
                    self,
                    f"确认确定性修复并创建 v{next_version}？",
                    f"将使用 {len(actions)} 次动作生成次数，并用当前已接受的"
                    f"「{_ACTION_LABELS[directional_source]}」"
                    f"作为基准，通过双镜像共轭生成"
                    f"「{_ACTION_LABELS[target_action]}」。{cost_line}\n"
                    "旧版本会继续保留；新方向仍需逐项预览。",
                    QMessageBox.Yes | QMessageBox.Cancel,
                    QMessageBox.Cancel,
                )
            else:
                direction_note = (
                    "\n当前没有已确认的反向动作，本次将直接生成所选方向；"
                    "结果仍需人工预览，之后另一侧可使用镜像共轭生成。"
                    if directional_targets
                    else ""
                )
                answer = QMessageBox.warning(
                    self,
                    f"确认创建 v{next_version}？",
                    f"将新增或替换 {len(actions)} 个动作，使用 {len(actions)} 次"
                    "动作生成次数；未勾选动作保持不变。"
                    f"{cost_line}{direction_note}\n旧版本会继续保留，可随时回滚。",
                    QMessageBox.Yes | QMessageBox.Cancel,
                    QMessageBox.Cancel,
                )
            if answer != QMessageBox.Yes:
                return
        instruction = self.revision_instruction.text().strip()
        self._package_key = None
        self._preview_root = None
        self._clear_action_previews()
        self.preview.setText(
            f"正在生成 {len(actions)} 个动作并创建 v{next_version}…"
        )
        context = _WorkbenchTaskContext(
            operation="action_revision",
            result_kind="package",
            request=self._active_request,
            actions=actions,
            instruction=instruction,
            accepted_direction_sources=tuple(
                sorted(accepted_direction_sources)
            ),
            package_version=next_version,
            appearance_revision=source.package.appearance_revision,
            source_package=self._editing_key,
        )
        self._start_persistent_task(
            RoleTaskSpec(
                operation="action_revision",
                profile_id=self._active_request.role_id,
                appearance_revision=source.package.appearance_revision,
                actions=actions,
                package_version=next_version,
                input_sha256=self._request_digest(self._active_request),
                identity_sha256=source.package.identity_sha256,
            ),
            context,
        )

    def _save_recoverable_draft(
        self,
        request: WorkbenchRequest,
        *,
        identity_path: Path | None = None,
        appearance_revision: int = 1,
    ) -> RoleDraft:
        profile_input = None
        if request.input_mode == "image":
            profile_input = self._draft_store.import_input_image(Path(request.image_path))
        profile = request.to_profile(
            profile_input=profile_input,
            appearance_revision=appearance_revision,
        )
        identity_sha256 = (
            self._draft_store.import_identity_image(identity_path)
            if identity_path is not None
            else ""
        )
        draft = RoleDraft(
            profile=profile,
            selected_actions=self._selected_actions(),
            identity_sha256=identity_sha256,
        )
        self._draft_store.save_draft(draft)
        if identity_sha256:
            self._draft_store.commit_profile_revision(profile)
        return draft

    def _package_ready(self, value: object) -> None:
        if not isinstance(value, PackageResult):
            self._on_failure("工作台后端返回了无效的角色包结果")
            return
        self._package_result = value
        self._package_key = None
        self._result_saved = False
        self._preview_root = value.package_root / "previews"
        actions = self._populate_package_actions(value, self._preview_root)
        self.save_button.setEnabled(True)
        self._update_regenerate_enabled()
        self._update_install_enabled()
        self._update_action_emphasis()
        self._set_ui_stage(3)
        suffix = (
            f"；当前任务累计 ¥{value.spent_cny:.2f}"
            if self._show_costs
            else ""
        )
        if self._editing_key is not None or self._appearance_source_key is not None:
            self._revision_pending_install = True
            package = RolePackage.from_json(
                (value.package_root / "role.json").read_text(encoding="utf-8")
            )
            if self._appearance_source_key is not None:
                self.status.setText(
                    f"新形象 r{package.appearance_revision} / "
                    f"v{package.package_version} 已完成{suffix}。"
                    "旧形象及动作未改动；请预览后保存或安装。"
                )
            else:
                self.status.setText(
                    f"新版本 v{package.package_version} 已完成{suffix}。"
                    "旧版本未改动；请预览后保存或安装。"
                )
        else:
            self.status.setText(
                f"所选 {len(actions)} 个动作已完成{suffix}。"
                "请逐项预览、保存或安装到真实桌宠窗口。"
            )
        if actions:
            self._show_action_preview(actions[0])

    def _populate_package_actions(
        self,
        value: PackageResult,
        previews: Path,
    ) -> tuple[str, ...]:
        self._clear_action_previews()
        package = RolePackage.from_json(
            (value.package_root / "role.json").read_text(encoding="utf-8")
        )
        present = {action for action, _ in package.actions}
        actions = tuple(
            action
            for action in _WORKBENCH_ACTIONS
            if action in present and (previews / f"{action}.gif").is_file()
        )
        for action in actions:
            item = QListWidgetItem(f"{_ACTION_LABELS.get(action, action)}  ·  {action}")
            item.setData(Qt.UserRole, action)
            self.action_selector.addItem(item)
        self.action_selector.setVisible(bool(actions))
        if self.action_selector.count():
            self.action_selector.setCurrentRow(0)
        return actions

    def _clear_action_previews(self) -> None:
        self.action_selector.clear()
        self.action_selector.setVisible(False)

    def _show_action_preview_item(self, item: QListWidgetItem | None) -> None:
        if item is not None:
            self._show_action_preview(str(item.data(Qt.UserRole)))

    def _show_action_preview(self, action: str) -> None:
        if not action or self._package_result is None or self._preview_root is None:
            return
        path = self._preview_root / f"{action}.gif"
        if not path.is_file():
            return
        movie = QMovie(str(path))
        movie.setScaledSize(QSize(240, 210))
        self._preview_movie = movie
        self.preview.setMovie(movie)
        movie.start()

    def _regenerate_selected_actions(self) -> None:
        if not self._generation_available:
            return
        if (
            self._package_result is None
            or self._candidate_result is None
            or self._active_request is None
        ):
            return
        actions = tuple(
            str(item.data(Qt.UserRole)) for item in self.action_selector.selectedItems()
        )
        if not actions:
            return
        if self._editing_key is not None:
            selected = set(actions)
            for action, option in self.action_checks.items():
                option.setChecked(action in selected)
            self._generate_installed_revision()
            return
        directional_targets = set(actions).intersection(_DIRECTIONAL_OPPOSITES)
        if len(directional_targets) > 1:
            QMessageBox.warning(
                self,
                "请逐个修复方向动作",
                "左右探头互为修复基准，请一次只选择其中一个方向。",
            )
            return
        package = RolePackage.from_json(
            (self._package_result.package_root / "role.json").read_text(
                encoding="utf-8"
            )
        )
        accepted_direction_sources: frozenset[str] = frozenset()
        if self._generation_consumes_units():
            cost_line = (
                f"，预计增加约 ¥{len(actions) * 0.45:.2f}"
                if self._show_costs
                else ""
            )
            if directional_targets:
                target_action = next(iter(directional_targets))
                source_action = _DIRECTIONAL_OPPOSITES[target_action]
                quality = dict(package.quality.actions).get(source_action)
                if (
                    quality is not None
                    and quality.status == "accepted"
                    and quality.direction is not None
                    and quality.direction.status == "accepted"
                ):
                    answer = QMessageBox.warning(
                        self,
                        "确认确定性修复方向？",
                        "将使用 1 次动作生成次数，并用当前"
                        f"「{_ACTION_LABELS[source_action]}」作为方向基准，"
                        f"通过双镜像共轭生成「{_ACTION_LABELS[target_action]}」"
                        f"{cost_line}。\n\n"
                        f"请确认当前「{_ACTION_LABELS[source_action]}」的方向和角色特征"
                        "可以接受。新结果仍需预览确认。",
                        QMessageBox.Yes | QMessageBox.Cancel,
                        QMessageBox.Cancel,
                    )
                    if answer == QMessageBox.Yes:
                        accepted_direction_sources = frozenset({source_action})
                else:
                    answer = QMessageBox.warning(
                        self,
                        "确认直接生成方向动作？",
                        "当前没有已确认的反向动作，将直接生成"
                        f"「{_ACTION_LABELS[target_action]}」并使用 1 次动作生成次数"
                        f"{cost_line}。\n\n结果仍需人工预览；接受后，另一侧可使用"
                        "镜像共轭生成。",
                        QMessageBox.Yes | QMessageBox.Cancel,
                        QMessageBox.Cancel,
                    )
            else:
                answer = QMessageBox.warning(
                    self,
                    "确认重新生成所选动作？",
                    f"将重新生成 {len(actions)} 个动作，使用 {len(actions)} 次"
                    f"动作生成次数{cost_line}。"
                    "新结果成功后才会归档旧动作，其他动作保持不变。",
                    QMessageBox.Yes | QMessageBox.Cancel,
                    QMessageBox.Cancel,
                )
            if answer != QMessageBox.Yes:
                return
        candidate = Path(str(self.candidates.currentItem().data(Qt.UserRole)))
        instruction = self.revision_instruction.text().strip()
        if not self._package_task_id:
            QMessageBox.critical(
                self,
                "无法创建动作修订任务",
                "当前角色包没有持久化任务来源，请重新生成角色包。",
            )
            return
        candidate_relative = candidate.relative_to(
            self._candidate_result.session_root
        ).as_posix()
        context = _WorkbenchTaskContext(
            operation="action_revision",
            result_kind="package",
            request=self._active_request,
            source_task_id=self._package_task_id,
            candidate_relative_path=candidate_relative,
            actions=actions,
            instruction=instruction,
            accepted_direction_sources=tuple(sorted(accepted_direction_sources)),
            package_version=package.package_version,
            appearance_revision=package.appearance_revision,
        )
        self._start_persistent_task(
            RoleTaskSpec(
                operation="action_revision",
                profile_id=self._active_request.role_id,
                appearance_revision=package.appearance_revision,
                actions=actions,
                package_version=package.package_version,
                input_sha256=self._request_digest(self._active_request),
                identity_sha256=package.identity_sha256,
            ),
            context,
        )

    def _save_package(self) -> None:
        if self._package_result is None or self._active_request is None:
            return
        if not self._confirm_result_use("保存角色包"):
            return
        parent = QFileDialog.getExistingDirectory(self, "选择角色包保存位置")
        if not parent:
            return
        if self._editing_key is None:
            _write_package_identity(self._package_result.package_root, self._active_request)
        folder_name = _package_folder_name(
            self._active_request.display_name,
            self._active_request.role_id,
        )
        destination = Path(parent) / folder_name
        suffix = 2
        while destination.exists():
            destination = Path(parent) / f"{folder_name}-{suffix}"
            suffix += 1
        try:
            _accept_generated_package(self._package_result.package_root)
            shutil.copytree(self._package_result.package_root, destination)
        except (OSError, ValueError, RoleContractError) as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.status.setText(f"角色包已保存到：{destination}")
        self._result_saved = True

    def _request_install(self) -> None:
        if self._package_result is None:
            return
        if not self._confirm_result_use("保存并安装角色"):
            return
        try:
            if self._package_key is None:
                if self._editing_key is None and self._active_request is not None:
                    _write_package_identity(
                        self._package_result.package_root,
                        self._active_request,
                    )
                _accept_generated_package(self._package_result.package_root)
                installed = self._role_library.install_directory(
                    self._package_result.package_root
                )
            else:
                installed = self._role_library.get(self._package_key)
        except (OSError, RoleContractError) as exc:
            QMessageBox.critical(
                self,
                "角色安装失败",
                f"{exc}\n\n旧版本和当前桌宠均未改动。",
            )
            return

        self._refresh_editable_roles(installed.key)
        self._result_saved = True
        self.open_installed_role(installed.key)
        self.status.setText(
            f"{installed.package.display_name} v{installed.key.package_version} "
            "已安全安装；旧版本仍可在设置中回滚。"
        )
        self.install_requested.emit(installed.key)

    def _confirm_result_use(self, action: str) -> bool:
        if self._settings.value(_ACK_SETTINGS_KEY, False, type=bool):
            return True
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle(f"确认{action}")
        dialog.setText("请确认输入内容不侵犯他人权益，并且生成结果符合你的预期。")
        dialog.setInformativeText(f"继续后将{action}。")
        dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        dialog.setDefaultButton(QMessageBox.Cancel)
        skip = QCheckBox("不再提示")
        dialog.setCheckBox(skip)
        accepted = dialog.exec() == QMessageBox.Yes
        if accepted and skip.isChecked():
            self._settings.setValue(_ACK_SETTINGS_KEY, True)
            self._settings.sync()
        return accepted

    def _confirm_leave_unsaved_result(self, action: str) -> bool:
        if (
            self._candidate_result is None and self._package_result is None
        ) or self._result_saved:
            return True
        answer = QMessageBox.question(
            self,
            "生成结果尚未保存",
            "当前角色结果还没有保存或安装。\n\n"
            f"继续{action}后不会自动回到这个预览；"
            "角色设定草稿仍会保留。是否继续？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return answer == QMessageBox.Yes

    def _on_progress(self, message: str, percent: int) -> None:
        del percent  # 供应商只返回阶段状态；不向用户展示虚构百分比。
        self.status.setText(message)

    def _rotate_activity_indicator(self) -> None:
        current = self.activity_indicator.text()
        choices = [face for face in self._activity_faces if face not in current]
        face = random.choice(choices or self._activity_faces)
        self.activity_indicator.setText(f"{face}  正在认真制作中…")

    def _on_failure(self, error: object) -> None:
        message = role_service_user_message(error)
        if self._active_task_id:
            self.resume_task_button.setEnabled(
                self._generation_available and self._can_resume_active_task()
            )
        elif (
            self._backend.is_fake
            and self._current_operation is not None
            and self._current_success is not None
        ):
            self._retry_operation = self._current_operation
            self._retry_success = self._current_success
            self.resume_task_button.setEnabled(self._generation_available)
        self.status.setText(f"任务未完成：{message}")
        QMessageBox.critical(
            self,
            "自定义角色任务未完成",
            f"{message}\n\n已落盘的 task、原视频和账本会保留；请先恢复，不要直接重提。",
        )

    def _on_operation_success(
        self, value: object, success: Callable[[object], None]
    ) -> None:
        self._retry_operation = None
        self._retry_success = None
        self.resume_task_button.setEnabled(False)
        success(value)

    def _resume_failed_task(self) -> None:
        if not self._generation_available:
            return
        if self._active_task_id and self._active_task_context is not None:
            if self._worker is not None or not self._can_resume_active_task():
                return
            local_task_id = self._active_task_id
            context = self._active_task_context
            self.resume_task_button.setEnabled(False)
            self.status.setText(
                f"正在恢复任务 {local_task_id[:8]}…；不会创建新的计费请求。"
            )
            self._refresh_account_after_worker = callable(
                getattr(self._service_transport, "account_summary", None)
            )
            self._start_worker(
                lambda progress: self._execute_persistent_task(
                    local_task_id, progress
                ),
                lambda value: self._persistent_task_ready(
                    local_task_id, context, value
                ),
            )
            return
        operation = self._retry_operation
        success = self._retry_success
        if (
            not self._backend.is_fake
            or operation is None
            or success is None
            or self._worker is not None
        ):
            return
        self.resume_task_button.setEnabled(False)
        self.status.setText("正在沿用同一离线任务恢复；不会创建新的计费请求…")
        self._start_worker(operation, success)

    def _request_task_cancel(self) -> None:
        if self._worker is None or not self._worker.isRunning():
            return
        if self._active_task_id:
            try:
                if self._service_client is not None:
                    binding = self._service_client.binding_store.load(
                        self._active_task_id
                    )
                    if binding is None:
                        self._task_store.request_cancel(self._active_task_id)
                    else:
                        self._service_client.cancel(self._active_task_id)
                else:
                    self._task_store.request_cancel(self._active_task_id)
            except (RoleContractError, RuntimeError, ValueError, OSError) as exc:
                self.status.setText(f"无法记录取消请求：{exc}")
        self._worker.requestInterruption()
        self.cancel_task_button.setEnabled(False)
        self.status.setText(
            "已请求取消；将在下一个安全进度点停止。"
            "若远程任务已经提交，现有 task 与原始结果会保留，不会自动重提。"
        )

    def _on_cancelled(self) -> None:
        if self._active_task_id:
            try:
                record = self._task_store.load(self._active_task_id)
                if record.status == "cancel_requested":
                    self._task_store.acknowledge_cancel(self._active_task_id)
            except RoleContractError:
                pass
            self._task_contexts.clear_active(self._active_task_id)
            self._active_task_id = ""
            self._active_task_context = None
        self._retry_operation = None
        self._retry_success = None
        self._candidate_result = None
        self._package_result = None
        self._package_key = None
        self._preview_root = None
        self.candidates.clear()
        self._clear_action_previews()
        self.save_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self._set_ui_stage(1)
        self.status.setText(
            "当前操作已在安全点取消；没有产生可保存或可安装的新结果。"
            "已提交的远程 task 若存在，必须走恢复流程，不能直接重提。"
        )

    def _worker_finished(self) -> None:
        worker = self._worker
        refresh_account = self._refresh_account_after_worker
        self._refresh_account_after_worker = False
        self._worker = None
        self._current_operation = None
        self._current_success = None
        self._set_busy(False)
        if worker is not None:
            worker.deleteLater()
        if refresh_account:
            self._refresh_account_summary(quiet=True)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(
                self,
                "任务仍在运行",
                "为避免丢失远程 task，请等待当前步骤完成后再关闭工作台。",
            )
            event.ignore()
            return
        if self.isVisible() and not self._confirm_leave_unsaved_result("关闭工作台"):
            event.ignore()
            return
        super().closeEvent(event)
