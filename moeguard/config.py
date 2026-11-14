"""应用配置：默认值与用户可调项。

所有可调参数集中在此，便于后续从用户配置文件
(~/.moeguard/config.toml，见 utils/paths.config_path) 覆盖。

config.toml 持久化：
- load(): 从 ~/.moeguard/config.toml 读取（发布基线 Python 3.12，使用内置
  tomllib），逐项合并到默认 dataclass。
- save(): 用 dataclasses.asdict + tomli_w 写入 toml。
- 对 frozen dataclass 的处理：通过 dataclasses.replace 创建新实例。
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)

# tomllib须静态导入，动态导入会使PyInstaller漏收标准库模块。
_toml_reader = tomllib

# 任何会改变值守采集范围、保留期或风险说明的知会文本变更，都必须提升此版本。
# 旧配置缺失该字段时不可以默认继承为当前版本，必须重新取得主动同意。
PATROL_CONSENT_VERSION = "2026-08-m4.7"

try:
    import tomli_w as _toml_writer
except ImportError:
    _toml_writer = None  # type: ignore[assignment]


@dataclass(frozen=True)
class PresenceConfig:
    """离座检测配置（全部属于免费本地能力）。

    陪伴模式摄像头关闭、不做检测；手动值守和显式启用后的锁屏/空闲
    自动值守均免费。系统事件只判定离座，值守中才按授权打开摄像头。
    """

    auto_patrol_enabled: bool = False
    idle_threshold_sec: int = 300
    return_confirm_sec: int = 3
    patrol_interval_sec: float = 1.0


@dataclass(frozen=True)
class ConsentConfig:
    """值守采集的版本化、可撤回同意记录。"""

    CURRENT_PATROL_CONSENT_VERSION: ClassVar[str] = PATROL_CONSENT_VERSION
    patrol_consent_granted: bool = False
    patrol_consent_version: str = CURRENT_PATROL_CONSENT_VERSION


@dataclass(frozen=True)
class OnboardingConfig:
    '首次引导是否看过；与值守同意分开保存，互不推断。'

    basic_done: bool = False


@dataclass(frozen=True)
class CameraConfig:
    '摄像头配置。'

    device_index: int = 0  # 多摄像头时可选设备序号
    interval_ms: int = 1000


@dataclass(frozen=True)
class EvidenceConfig:
    """陌生人证据记录配置。"""

    video_clip_sec: int = 10
    retention_days: int = 7
    blur_stranger_faces: bool = False  # 隐私模式：模糊陌生人脸（默认取证不模糊）
    show_patrol_banner: bool = True
    motion_recording_enabled: bool = False


@dataclass(frozen=True)
class SecurityConfig:
    '安防识别配置。'


    detector_model: str = "face_detection_yunet_2023mar.onnx"
    recognizer_model: str = "face_recognition_sface_2021dec.onnx"

    face_match_threshold: float = 0.363
    stranger_cooldown_sec: int = 30


@dataclass(frozen=True)
class DialogueConfig:
    '仅为私有 v2 实验/旧配置兼容保留的离线数据结构。'

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_history_turns: int = 10
    hosted_mode: str = "关闭"
    system_prompt: str = ""


@dataclass(frozen=True)
class PetConfig:
    '桌宠形象与显示配置。'


    role_id: str = "lumen"
    role_package_version: int = 0
    assets_dir: str = ""
    fps: int = 6
    fps_overrides: dict[str, int] = field(default_factory=dict)
    default_width: int = 200
    default_height: int = 300
    saved_width: int = 200
    saved_height: int = 300
    ping_pong: bool = True
    stealth_hotkey: str = "Ctrl+Shift+H"

    @property
    def fps_key(self) -> str:
        if self.role_package_version > 0:
            return f"managed:{self.role_id}"
        if self.assets_dir:
            path = os.path.normcase(os.path.abspath(self.assets_dir))
            return "directory:" + sha256(os.fsencode(path)).hexdigest()
        return f"builtin:{self.role_id}"

    @property
    def fps_override(self) -> int | None:
        value = self.fps_overrides.get(self.fps_key)
        return value if type(value) is int and 1 <= value <= 60 else None


@dataclass(frozen=True)
class ImageGenConfig:
    """仅为私有 v2 实验兼容保留，基础版不提供生成入口。"""

    resolution: str = "720P"
    prompt_extend: bool = False
    seed: int = 12345


@dataclass(frozen=True)
class UpdateConfig:
    """更新检查偏好；关闭自动检查后仍可手动检查。"""

    auto_check_enabled: bool = True


@dataclass(frozen=True)
class AppConfig:
    """萌卫应用总配置。"""

    presence: PresenceConfig = field(default_factory=PresenceConfig)
    consent: ConsentConfig = field(default_factory=ConsentConfig)
    onboarding: OnboardingConfig = field(default_factory=OnboardingConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    dialogue: DialogueConfig = field(default_factory=DialogueConfig)
    pet: PetConfig = field(default_factory=PetConfig)
    image_gen: ImageGenConfig = field(default_factory=ImageGenConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        """加载配置：默认值 + 用户配置文件覆盖。

        从 ~/.moeguard/config.toml 读取（发布基线 Python 3.12 内置 tomllib），
        逐项校验并合并到对应 dataclass。
        文件不存在或解析失败时返回默认值。

        Args:
            path: 配置文件路径，默认使用 CONFIG_PATH。

        Returns:
            合并后的 AppConfig 实例。
        """

        if path is None:
            from moeguard.utils.paths import CONFIG_PATH
            path = CONFIG_PATH

        if not path.exists():
            logger.debug("配置文件不存在，使用默认值: %s", path)
            return cls()


        try:
            with open(path, "rb") as f:
                raw = _toml_reader.load(f)
        except Exception as exc:
            logger.warning("读取配置文件失败，使用默认值: %s", exc)
            return cls()

        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, data: dict) -> AppConfig:
        """从字典构建 AppConfig，逐子配置合并。

        Args:
            data: tomllib 解析后的字典。

        Returns:
            合并后的 AppConfig 实例。
        """
        config = cls()

        presence_data = data.get("presence", {})
        if presence_data:
            config = replace(
                config,
                presence=PresenceConfig(**{
                    k: v for k, v in presence_data.items()
                    if k in PresenceConfig.__dataclass_fields__
                }),
            )

        consent_data = data.get("consent", {})
        if consent_data:
            consent_values = {
                k: v for k, v in consent_data.items()
                if k in ConsentConfig.__dataclass_fields__
            }
            if (
                consent_values.get("patrol_consent_granted")
                and "patrol_consent_version" not in consent_values
            ):
                # 旧版配置的 true 没有可验证的告知文本版本，不能继续授权采集。
                consent_values["patrol_consent_version"] = ""
            config = replace(
                config,
                consent=ConsentConfig(**consent_values),
            )


        onboarding_data = data.get("onboarding")
        basic_done = (
            onboarding_data.get("basic_done") if isinstance(onboarding_data, dict) else None
        )
        config = replace(
            config,
            onboarding=OnboardingConfig(
                basic_done=basic_done if isinstance(basic_done, bool) else True
            ),
        )

        camera_data = data.get("camera", {})
        if camera_data:
            config = replace(
                config,
                camera=CameraConfig(**{
                    k: v for k, v in camera_data.items()
                    if k in CameraConfig.__dataclass_fields__
                }),
            )

        evidence_data = data.get("evidence", {})
        if evidence_data:
            config = replace(
                config,
                evidence=EvidenceConfig(**{
                    k: v for k, v in evidence_data.items()
                    if k in EvidenceConfig.__dataclass_fields__
                }),
            )

        security_data = data.get("security", {})
        if security_data:
            config = replace(
                config,
                security=SecurityConfig(**{
                    k: v for k, v in security_data.items()
                    if k in SecurityConfig.__dataclass_fields__
                }),
            )


        pet_data = data.get("pet", {})
        if pet_data:
            pet_values = {
                k: v for k, v in pet_data.items()
                if k in PetConfig.__dataclass_fields__
            }
            package_version = pet_values.get("role_package_version", 0)
            overrides = pet_values.get("fps_overrides", {})
            pet_values["fps_overrides"] = {
                key: value for key, value in overrides.items()
                if isinstance(key, str) and key and type(value) is int and 1 <= value <= 60
            } if isinstance(overrides, dict) else {}
            if (
                isinstance(package_version, bool)
                or not isinstance(package_version, int)
                or package_version < 0
            ):
                logger.warning("忽略无效的角色包版本配置: %r", package_version)
                pet_values["role_package_version"] = 0
            config = replace(
                config,
                pet=PetConfig(**pet_values),
            )

        img_data = data.get("image_gen", {})
        if img_data:
            config = replace(
                config,
                image_gen=ImageGenConfig(**{
                    k: v for k, v in img_data.items()
                    if k in ImageGenConfig.__dataclass_fields__
                }),
            )

        updates_data = data.get("updates", {})
        if isinstance(updates_data, dict) and "auto_check_enabled" in updates_data:
            enabled = updates_data["auto_check_enabled"]
            if isinstance(enabled, bool):
                config = replace(config, updates=UpdateConfig(auto_check_enabled=enabled))
            else:
                logger.warning("忽略无效的自动检查更新配置")

        return config

    @staticmethod
    def save(config: AppConfig, path: Path | None = None) -> bool:
        """保存配置到 toml 文件。

        使用 dataclasses.asdict + tomli_w 写入，并通过同目录临时文件原子替换。
        对 frozen dataclass：asdict 可正常工作（只读访问字段）。

        Args:
            config: 要保存的 AppConfig 实例。
            path: 目标路径，默认使用 CONFIG_PATH。
        """
        if path is None:
            from moeguard.utils.paths import CONFIG_PATH
            path = CONFIG_PATH

        if _toml_writer is None:
            logger.warning("tomli_w 不可用，无法保存配置文件")
            return False

        tmp_path: Path | None = None
        try:
            data = asdict(config)

            data.pop("dialogue", None)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as f:
                tmp_path = Path(f.name)
                _toml_writer.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            logger.info("配置已保存: %s", path)
            return True
        except Exception as exc:
            logger.error("保存配置文件失败: %s", exc)
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            return False
