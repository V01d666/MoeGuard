"""QPixmap 序列帧动画渲染引擎（T8 选定，M0-1 已验证，D38 交互动作设计）。

将帧动画控制逻辑从 PetWindow 中分离为独立控制器，
PetWindow 仅负责 paintEvent 绘制（通过 get_current_frame() 获取当前帧）。

关键决议：
- T8: QPixmap 序列自绘帧动画（放弃 Live2D），M0-1 已验证
- T1.6: 25帧@6fps 通过（4.5+/5），crossfade 不可用（闪烁），
  ping-pong 反向播放缓解循环跳帧感
- D38: 交互动作分类--
  - 状态切换类(点击/拖动/吸附)完全可行
  - 连续跟随类效果有限
  - 物理模拟类不可行
  - 9 个帧组: idle / notice / click_reaction / dragging /
    peek_left / peek_right / sit_down / patrol / welcome
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap

logger = logging.getLogger(__name__)

# T1.6 定稿默认帧率
_DEFAULT_FPS = 6


class PingPongSequence:
    """辅助类：给定帧列表 [0,1,2,...,N-1]，生成正序+倒序循环索引。

    T1.6: crossfade 不可用（闪烁），ping-pong 可缓解循环跳帧感。
    正序播完再倒序播放：[0,1,...,N-1,N-2,...,1]，然后循环。
    首尾各只出现一次，避免重复帧导致的视觉停顿。
    """

    def __init__(
        self,
        frame_count: int,
        *,
        start_hold_frames: int = 0,
        end_hold_frames: int = 0,
    ) -> None:
        start_hold_frames = max(0, start_hold_frames)
        end_hold_frames = max(0, end_hold_frames)
        if frame_count <= 0:
            self._indices: list[int] = []
        elif frame_count == 1:
            self._indices = [0] * (1 + start_hold_frames + end_hold_frames)
        else:
            # 正序 [0, 1, ..., N-1] + 倒序 [N-2, N-3, ..., 1]
            forward = list(range(frame_count))
            backward = list(range(frame_count - 2, 0, -1))
            self._indices = (
                [0] * start_hold_frames
                + forward
                + [frame_count - 1] * end_hold_frames
                + backward
            )

    @property
    def length(self) -> int:
        """循环序列长度。"""
        return len(self._indices)

    def at(self, pos: int) -> int:
        """取循环序列中指定位置的原始帧索引。"""
        if not self._indices:
            return 0
        return self._indices[pos % len(self._indices)]


class _ActionData:
    """单个动作的帧数据与播放参数。"""

    __slots__ = (
        "name",
        "frames",
        "fps",
        "loop",
        "ping_pong_seq",
    )

    def __init__(
        self,
        name: str,
        frames: list[QPixmap],
        fps: int,
        loop: bool,
    ) -> None:
        self.name = name
        self.frames = frames
        self.fps = fps
        self.loop = loop
        self.ping_pong_seq: PingPongSequence | None = None

    @property
    def frame_count(self) -> int:
        return len(self.frames)


class FrameAnimationController(QObject):
    """管理帧组加载、切换、播放控制。

    不继承 QWidget，作为 PetWindow 的渲染逻辑分离。
    PetWindow 通过 get_current_frame() 获取当前帧用于 paintEvent。

    信号:
        animation_changed: 当前动作名变化时发射。
    """

    animation_changed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._actions: dict[str, _ActionData] = {}
        self._current_action: str = ""
        self._current_data: _ActionData | None = None
        self._frame_index: int = 0  # 当前在播放序列中的位置
        self._is_playing: bool = False
        self._ping_pong: bool = False
        self._fade_frames: int = 0
        self._fade_remaining: int = 0
        self._prev_frames: list[QPixmap] = []  # 淡出用的旧帧组
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._playback_finished_callback: object | None = None

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #

    @property
    def current_action(self) -> str:
        """当前动作名（idle/notice/click_reaction 等）。"""
        return self._current_action

    @property
    def is_playing(self) -> bool:
        """是否正在播放。"""
        return self._is_playing

    # ------------------------------------------------------------------ #
    # 帧组加载
    # ------------------------------------------------------------------ #

    def load_action(
        self,
        action_name: str,
        frame_paths: list[str],
        fps: int = _DEFAULT_FPS,
        loop: bool = True,
        *,
        flip_horizontal: bool = False,
        flip_vertical: bool = False,
    ) -> None:
        """加载一个动作的帧序列。

        T1.6 定稿 25帧@6fps。

        Args:
            action_name: 动作名（如 idle / click_reaction / patrol）。
            frame_paths: 帧图片路径列表（PNG 透明背景）。
            fps: 帧率（默认 6）。
            loop: 是否循环播放。
            flip_horizontal: 加载时左右镜像；用于派生严格对称的边缘动作。
            flip_vertical: 加载时上下镜像；用于派生临时顶部探头动作。
        """
        frames: list[QPixmap] = []
        for p in frame_paths:
            if not p:
                continue
            if not Path(p).exists():
                logger.warning("帧文件不存在，跳过: %s", p)
                continue
            pm = QPixmap(p)
            if pm.isNull():
                logger.warning("帧加载失败（isNull）: %s", p)
                continue
            if flip_horizontal or flip_vertical:
                orientation = Qt.Orientation(0)
                if flip_horizontal:
                    orientation |= Qt.Horizontal
                if flip_vertical:
                    orientation |= Qt.Vertical
                pm = QPixmap.fromImage(
                    pm.toImage().flipped(orientation)
                )
            frames.append(pm)

        if not frames:
            logger.warning("动作 '%s' 无有效帧，路径数=%d", action_name, len(frame_paths))
            self._actions[action_name] = _ActionData(action_name, [], fps, loop)
            return

        data = _ActionData(action_name, frames, fps, loop)
        self._actions[action_name] = data
        logger.info(
            "加载动作 '%s': %d 帧 @%dfps, loop=%s",
            action_name,
            len(frames),
            fps,
            loop,
        )

    def clear_actions(self) -> None:
        """停止播放并清空已加载帧组，供运行时安全切换角色。"""
        self.stop()
        self._actions.clear()
        self._current_action = ""
        self._current_data = None
        self._frame_index = 0
        self._ping_pong = False
        self._playback_finished_callback = None

    def replace_actions_from(self, candidate: FrameAnimationController) -> None:
        """Atomically adopt a fully loaded candidate without changing this object.

        PetWindow and FeedbackController retain their reference to this
        controller. Callers must validate the candidate before committing it.
        """
        if not candidate.has_action("idle"):
            raise ValueError("candidate role has no usable idle action")
        candidate.stop()
        self.clear_actions()
        self._actions = dict(candidate._actions)

    # ------------------------------------------------------------------ #
    # 播放控制
    # ------------------------------------------------------------------ #

    def play(
        self,
        action_name: str,
        *,
        ping_pong: bool = False,
        fade_frames: int = 0,
        start_hold_frames: int = 0,
        end_hold_frames: int = 0,
    ) -> None:
        """切换到指定动作并播放。

        Args:
            action_name: 要播放的动作名。
            ping_pong: True 时正序播完再倒序播放，缓解循环跳帧感（T1.6）。
            fade_frames: >0 时新旧动作间做 N 帧淡入淡出（D38: 2~3帧）。
                         注意：T1.6 crossfade 不可用（闪烁），
                         此参数为预留，当前实现为简单切换+日志。
        """
        data = self._actions.get(action_name)
        if data is None or data.frame_count == 0:
            logger.warning(
                "动作 '%s' 未加载或无帧，fallback 到 idle", action_name
            )
            if action_name != "idle":
                self.play("idle", ping_pong=ping_pong, fade_frames=fade_frames)
            return

        # 保存旧帧组用于淡出（如果需要）
        if fade_frames > 0 and self._current_data is not None:
            self._prev_frames = list(self._current_data.frames)
            self._fade_frames = fade_frames
            self._fade_remaining = fade_frames
        else:
            self._prev_frames = []
            self._fade_frames = 0
            self._fade_remaining = 0

        # 切换动作
        old_action = self._current_action
        self._current_action = action_name
        self._current_data = data
        self._frame_index = 0
        self._ping_pong = ping_pong

        if ping_pong:
            data.ping_pong_seq = PingPongSequence(
                data.frame_count,
                start_hold_frames=start_hold_frames,
                end_hold_frames=end_hold_frames,
            )
        else:
            data.ping_pong_seq = None

        if old_action != action_name:
            self.animation_changed.emit(action_name)
            logger.debug("动画切换: %s -> %s", old_action, action_name)

        # 启动定时器
        interval = max(1, int(1000 / data.fps)) if data.fps > 0 else 167
        self._timer.setInterval(interval)
        self._timer.start()
        self._is_playing = True

    def stop(self) -> None:
        """停止播放。"""
        self._timer.stop()
        self._is_playing = False
        self._fade_remaining = 0
        self._prev_frames = []

    def tick(self) -> None:
        """QTimer 驱动的帧推进。

        内部更新帧索引，PetWindow 通过 get_current_frame() 获取当前帧后
        调用 update() 触发重绘。
        """
        if self._current_data is None or self._current_data.frame_count == 0:
            return

        data = self._current_data

        # 推进帧索引
        if self._ping_pong and data.ping_pong_seq is not None:
            seq_len = data.ping_pong_seq.length
            if seq_len > 0:
                self._frame_index = (self._frame_index + 1) % seq_len
        else:
            frame_count = data.frame_count
            next_idx = self._frame_index + 1
            if next_idx >= frame_count:
                if data.loop:
                    self._frame_index = 0
                else:
                    # 非循环：停在最后一帧
                    self._frame_index = frame_count - 1
                    self._timer.stop()
                    self._is_playing = False
                    logger.debug("动作 '%s' 播放完毕（非循环）", data.name)
                    return
            else:
                self._frame_index = next_idx

        # 淡入淡出帧计数递减
        if self._fade_remaining > 0:
            self._fade_remaining -= 1

    # ------------------------------------------------------------------ #
    # 帧获取
    # ------------------------------------------------------------------ #

    def get_current_frame(self) -> QPixmap | None:
        """供 PetWindow paintEvent 使用的当前帧。

        Returns:
            当前帧的 QPixmap，无帧时返回 None。
        """
        if self._current_data is None or self._current_data.frame_count == 0:
            return None

        data = self._current_data

        if self._ping_pong and data.ping_pong_seq is not None:
            raw_idx = data.ping_pong_seq.at(self._frame_index)
        else:
            raw_idx = self._frame_index % data.frame_count

        return data.frames[raw_idx]

    def available_actions(self) -> list[str]:
        """已加载的动作名列表。"""
        return list(self._actions.keys())

    def has_action(self, action_name: str) -> bool:
        """检查某动作是否已加载且有有效帧。"""
        data = self._actions.get(action_name)
        return data is not None and data.frame_count > 0
