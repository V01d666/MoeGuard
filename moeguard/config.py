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
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)

# 发布基线固定 Python 3.12。保持静态导入，使 PyInstaller 能可靠收集
# 标准库 tomllib；动态 importlib 导入会绕过其模块分析。
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

    auto_patrol_enabled: bool = False  # 系统事件自动值守开关（默认关闭，用户主动启用）
    idle_threshold_sec: int = 300  # 键鼠空闲N秒判定离座（默认5分钟，避免接杯水误触发）
    return_confirm_sec: int = 3  # 值守中连续识别到主人N秒判定回座
    patrol_interval_sec: float = 1.0  # 值守模式人脸检测采样间隔


@dataclass(frozen=True)
class ConsentConfig:
    """值守采集的版本化、可撤回同意记录。"""

    CURRENT_PATROL_CONSENT_VERSION: ClassVar[str] = PATROL_CONSENT_VERSION
    patrol_consent_granted: bool = False
    patrol_consent_version: str = CURRENT_PATROL_CONSENT_VERSION


@dataclass(frozen=True)
class CameraConfig:
    """摄像头配置。

    M0-2 已验证常驻模式 1.00fps 精确达标
    （采帧 37.8ms + YuNet 5.6ms = 43.4ms/帧）。
    严禁每帧 open/close（M0-2 的根因，导致 0.5fps）。
    """

    device_index: int = 0  # 多摄像头时可选设备序号
    interval_ms: int = 1000  # 值守采样间隔（PRD §7.2: 1s/次检测）


@dataclass(frozen=True)
class EvidenceConfig:
    """陌生人证据记录配置。"""

    video_clip_sec: int = 10  # 单次事件短视频时长
    retention_days: int = 7  # 证据保留天数，超期自动清理
    blur_stranger_faces: bool = False  # 隐私模式：模糊陌生人脸（默认取证不模糊）
    show_patrol_banner: bool = True  # 值守中显示「安防值守中」屏幕标识（默认告知）
    motion_recording_enabled: bool = False  # 画面运动额外取证，默认关闭


@dataclass(frozen=True)
class SecurityConfig:
    """安防识别配置（D16: YuNet+SFace 替换 InsightFace）。

    SFace 余弦相似度阈值默认 0.363（OpenCV Zoo 官方推荐值）。
    D16-2 实测鸿沟极大（主人 0.76+ vs 陌生人 0.05-），提示当前阈值可能过松；
    但真实场景下阈值扫描归 T5（M1 后执行），此处做成可配置，不硬编码。
    """

    # 人脸模型文件名（ONNX，存 ~/.moeguard/models/）
    detector_model: str = "face_detection_yunet_2023mar.onnx"
    recognizer_model: str = "face_recognition_sface_2021dec.onnx"
    # SFace 余弦相似度阈值：>= 阈值判定为主人（D16-2 鸿沟 0.76+ vs 0.05-）
    face_match_threshold: float = 0.363
    stranger_cooldown_sec: int = 30  # 同一陌生人事件去冷静却，避免重复录制


@dataclass(frozen=True)
class DialogueConfig:
    """仅为私有 v2 实验/旧配置兼容保留的离线数据结构。

    基础版不构建对话 UI、不读取历史凭据，也不依赖 ``moeguard.cloud``。
    该类型不代表公开基础版提供任何云端能力。
    """

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_history_turns: int = 10
    hosted_mode: str = "关闭"
    system_prompt: str = ""


@dataclass(frozen=True)
class PetConfig:
    """桌宠形象与显示配置。

    PRD §5.6: 桌宠尺寸素材统一按最长边512存储，
    渲染时 scaled 到默认 200x300，用户可拖角缩放并持久化。
    """

    # 内置角色使用 package_version=0；受管自定义角色使用正整数版本。
    # assets_dir 仅保留旧 demo/开发兼容，不作为正式用户选择入口。
    role_id: str = "lumen"  # 当前内置角色；由 resources/roles 下的清单解析
    role_package_version: int = 0
    assets_dir: str = ""  # 自定义/实验角色包路径；非空时优先于内置角色
    fps: int = 6  # T1.6 定稿 25帧@6fps
    default_width: int = 200  # 默认显示宽度
    default_height: int = 300  # 默认显示高度
    saved_width: int = 200  # 用户缩放后持久化的宽度
    saved_height: int = 300  # 用户缩放后持久化的高度
    ping_pong: bool = True  # idle/patrol 等循环动作使用 ping-pong
    stealth_hotkey: str = "Ctrl+Shift+H"  # 老板键全局热键（QKeySequence 字符串）


@dataclass(frozen=True)
class ImageGenConfig:
    """仅为私有 v2 实验兼容保留，基础版不提供生成入口。"""

    resolution: str = "720P"
    prompt_extend: bool = False
    seed: int = 12345


@dataclass(frozen=True)
class AppConfig:
    """萌卫应用总配置。"""

    presence: PresenceConfig = field(default_factory=PresenceConfig)
    consent: ConsentConfig = field(default_factory=ConsentConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    dialogue: DialogueConfig = field(default_factory=DialogueConfig)
    pet: PetConfig = field(default_factory=PetConfig)
    image_gen: ImageGenConfig = field(default_factory=ImageGenConfig)

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
        # 延迟导入避免循环依赖
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
        config = cls()  # 默认值

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

        # 基础版忽略历史云端配置或凭据；升级遗留的 key 不会恢复可用，也
        # 不会在下一次保存时写回。云端实验代码不属于公开基础版。

        pet_data = data.get("pet", {})
        if pet_data:
            pet_values = {
                k: v for k, v in pet_data.items()
                if k in PetConfig.__dataclass_fields__
            }
            package_version = pet_values.get("role_package_version", 0)
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
            # 对话尚未属于 MVP，配置文件不得继续保存旧 API Key/端点/模型。
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
