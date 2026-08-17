"""桌宠反馈呈现：动作、表情、文字气泡。

根据应用状态与安防事件，驱动帧动画的帧组切换并输出台词。
MVP 阶段为「动画 + 文字气泡」；语音 TTS 为付费增强，后续接入。

设计要点：告警即情绪价值--「主人回来时口头汇报可疑情况」让安防告警
本身成为桌宠的「邀功」，而非冷冰冰的通知。

D38 动作分类（9 个帧组）：
  idle / notice / click_reaction / dragging /
  peek_left / peek_right / sit_down / patrol / welcome

关键约束：
- 如果某动作的帧未加载（路径为空），fallback 到 idle 并 log.warning。
- alert_stranger 静默克制，不弹气泡以免惊动陌生人。
- _base_mood 记忆"陪伴/值守"底态，交互动画结束后恢复正确底态而非硬编码 idle。
"""

from __future__ import annotations

import logging
import random
from enum import Enum, auto

from PySide6.QtCore import QObject, Signal

from moeguard.pet.frame_animation import FrameAnimationController

logger = logging.getLogger(__name__)

# 点击反馈台词库（M3 中期盲测第一优先级——每次点击随机一条）
_CLICK_LINES: list[str] = [
    "哎呀别戳我～",
    "干嘛呀～",
    "主人～有什么事呀？",
    "痒痒的～",
    "嘿嘿，我在的哦～",
    "唔…被发现了！",
    "诶嘿，好痒～",
    "主人～再戳一下嘛",
    "哼，不许乱戳！",
    "呼呀～干嘛呢？",
    "我在呢我在呢～",
    "欸？有好事要告诉我吗？",
]


def _random_click_line(lines: tuple[str, ...] = ()) -> str:
    """随机返回一条点击反馈台词。"""
    return random.choice(lines or _CLICK_LINES)


class PetMood(Enum):
    """桌宠情绪状态。"""

    IDLE = auto()  # 陪伴待机
    HAPPY = auto()  # 主人回来
    ALERT = auto()  # 陌生人出现（静默记录，克制）
    PATROL = auto()  # 值守中
    WELCOME = auto()  # 欢迎迎宾
    EDGE = auto()  # 贴边半免打扰


class FeedbackController(QObject):
    """根据状态/事件驱动桌宠反馈。

    通过 FrameAnimationController 切换帧组来表现不同情绪，
    通过 message 信号输出文字气泡内容。

    _base_mood 记忆陪伴/值守底态——交互动画（点击/拖拽）结束
    后恢复正确的底态动画，而非硬编码回 idle。

    信号:
        message: 文字气泡内容。
    """

    message = Signal(str)

    def __init__(
        self,
        pet_window,
        frame_controller: FrameAnimationController,
        *,
        click_lines: tuple[str, ...] = (),
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._pet = pet_window
        self._frames = frame_controller
        self._mood = PetMood.IDLE
        self._base_mood = PetMood.IDLE  # 陪伴/值守底态
        self._drag_active = False
        self._patrol_scan_next = "patrol_look_left"
        self._edge_direction: str | None = None
        self._click_lines = click_lines

    @property
    def mood(self) -> PetMood:
        """当前情绪。"""
        return self._mood

    def set_click_lines(self, lines: tuple[str, ...]) -> None:
        """Replace click dialogue when the active role changes."""
        self._click_lines = lines

    # ------------------------------------------------------------------ #
    # 状态机事件回调（设置底态）
    # ------------------------------------------------------------------ #

    def on_companion(self) -> None:
        """进入陪伴模式：底态=IDLE，播放 idle 帧组。"""
        self._clear_surface_snap()
        self._mood = PetMood.IDLE
        self._base_mood = PetMood.IDLE
        self._edge_direction = None
        self._play_action("idle", ping_pong=True)

    def on_patrol_start(self) -> None:
        """进入值守模式：底态=PATROL，播放 patrol 帧组 + 告知台词。"""
        self._clear_surface_snap()
        self._mood = PetMood.PATROL
        self._base_mood = PetMood.PATROL
        self._edge_direction = None
        self._start_patrol_visual()
        self.message.emit("主人放心去吧，这里交给我看守～")

    def alert_stranger(self) -> None:
        """检测到陌生人：ALERT -> 播放 alert 帧组。

        静默克制，不弹气泡以免惊动对方。
        D38: alert 动作帧组对应 notice（警觉注意）。
        """
        self._mood = PetMood.ALERT
        # alert 帧组在 D38 中对应 notice（警觉注意）
        # 优先用 alert，不存在则 fallback notice
        if self._frames.has_action("alert"):
            self._play_action("alert", ping_pong=False)
        else:
            self._play_action("notice", ping_pong=False)
        # 不 emit message，静默克制

    def greet_return(self, had_incidents: bool) -> None:
        """主人回座：WELCOME -> 播放 welcome 帧组。

        Args:
            had_incidents: 值守期间是否记录到陌生人事件。
        """
        self._mood = PetMood.WELCOME
        # welcome 是一次性过渡动画；结束后必须回到陪伴 idle，不能沿用值守底态。
        self._base_mood = PetMood.IDLE
        self._play_action("welcome", ping_pong=False)
        if had_incidents:
            self.message.emit("主人你回来啦～这是我发现的可疑情况，请主人过目～")
        else:
            self.message.emit("主人你回来啦～我看守得很认真哦，一切平安～")

    # ------------------------------------------------------------------ #
    # 交互动画结束后的恢复
    # ------------------------------------------------------------------ #

    def restore_animation(self) -> None:
        """交互动画播放完毕，恢复到当前底态（陪伴 idle / 值守 patrol）。

        与 on_companion()/on_patrol_start() 的区别：
        - 不改变底态（仅恢复动画）
        - 不弹消息气泡
        """
        if self._edge_direction is not None:
            self._play_edge_action(self._edge_direction)
        elif self._base_mood == PetMood.PATROL:
            self._mood = PetMood.PATROL
            self._start_patrol_visual()
        else:
            self._mood = PetMood.IDLE
            self._play_action("idle", ping_pong=True)

    # ------------------------------------------------------------------ #
    # 交互事件回调（D38 状态切换类）
    # ------------------------------------------------------------------ #

    def on_click(self) -> None:
        """点击触摸反应：播放 click_reaction + 随机气泡台词。

        动画播完后由 _on_animation_finished → restore_animation()
        恢复到正确的底态动画。
        """
        self._mood = PetMood.HAPPY
        self._play_action("click_reaction", ping_pong=False)
        self.message.emit(_random_click_line(self._click_lines))

    def on_drag_start(self) -> None:
        """拖拽开始：先一次性被抓起，再保持被拖拽的受力姿态。"""
        # 一旦重新抓起，旧贴边状态失效；松手后的新位置会再次发出 edge_snapped。
        self._edge_direction = None
        self._drag_active = True
        if self._frames.has_action("drag_pickup"):
            self._play_action("drag_pickup", ping_pong=False)
        else:
            self._play_action("dragging", ping_pong=False)

    def on_drag_end(self) -> None:
        """拖拽结束：恢复到底态动画。"""
        self._drag_active = False
        self.restore_animation()

    def on_edge_snap(self, direction: str) -> None:
        """贴边后切换到对应的半免打扰动作。

        ``peek_left/right`` 按用户看到的探头方向命名，因此左边缘消费
        ``peek_right``、右边缘消费 ``peek_left``。底边使用坐下；顶部使用
        坐下动作的上下镜像派生态，避免只露出悬空动画的下半身。
        """
        if direction not in {"left", "right", "top", "bottom"}:
            logger.warning("未知边缘吸附方向: %s", direction)
            return
        self._edge_direction = direction
        self._mood = PetMood.EDGE
        self._play_edge_action(direction)

    def on_animation_finished(self, action_name: str) -> bool:
        """Handle a private drag transition without treating it as a completed interaction."""
        if action_name == "drag_pickup" and self._drag_active:
            self._play_action("dragging", ping_pong=False)
            return True
        if (
            action_name in ("patrol_look_left", "patrol_look_right")
            and self._base_mood == PetMood.PATROL
            and self._frames.has_action("patrol_look_left")
            and self._frames.has_action("patrol_look_right")
        ):
            self._patrol_scan_next = (
                "patrol_look_right"
                if action_name == "patrol_look_left"
                else "patrol_look_left"
            )
            self._play_action(self._patrol_scan_next, ping_pong=False)
            return True
        return False

    def _start_patrol_visual(self) -> None:
        """Prefer authored left/right scans; retain the legacy patrol fallback."""
        if self._frames.has_action("patrol_look_left") and self._frames.has_action(
            "patrol_look_right"
        ):
            self._patrol_scan_next = "patrol_look_left"
            self._play_action(self._patrol_scan_next, ping_pong=False)
        else:
            self._play_action("patrol", ping_pong=False)

    def _play_edge_action(self, direction: str) -> None:
        """播放边缘状态；缺少正式边缘资产时沿用既有 idle fallback。"""
        action = {
            "left": "peek_right",
            "right": "peek_left",
            "top": "peek_top",
            "bottom": "sit_down",
        }[direction]
        if direction in {"left", "right"}:
            # 左右探头先在藏身位停留 1 秒，完整探出后再停留约 1.3 秒。
            # 这样不降低动作本身的帧率，既减少打扰，也保留一次探头的存在感。
            self._play_action(
                action,
                ping_pong=True,
                start_hold_frames=6,
                end_hold_frames=8,
            )
        else:
            self._play_action(action, ping_pong=True)

    def _clear_surface_snap(self) -> None:
        """Let the view forget desktop/app-surface anchoring on a mode change."""
        clear = getattr(self._pet, "clear_surface_snap", None)
        if callable(clear):
            clear()

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _play_action(
        self,
        action_name: str,
        *,
        ping_pong: bool = False,
        start_hold_frames: int = 0,
        end_hold_frames: int = 0,
    ) -> None:
        """播放指定动作，帧未加载时 fallback 到 idle。

        Args:
            action_name: 动作名。
            ping_pong: 是否 ping-pong 播放。
        """
        if self._frames.has_action(action_name):
            self._frames.play(
                action_name,
                ping_pong=ping_pong,
                start_hold_frames=start_hold_frames,
                end_hold_frames=end_hold_frames,
            )
            # 触发 PetWindow 重绘
            if hasattr(self._pet, "update"):
                self._pet.update()
        elif self._frames.has_action("idle") and action_name != "idle":
            logger.warning(
                "动作 '%s' 帧未加载，fallback 到 idle", action_name
            )
            self._frames.play("idle", ping_pong=True)
            if hasattr(self._pet, "update"):
                self._pet.update()
        else:
            logger.warning("动作 '%s' 和 idle 均未加载，无法播放", action_name)
