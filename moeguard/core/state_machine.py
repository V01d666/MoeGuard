"""陪伴 ⇄ 值守 状态机（D15 修正版）。

这是萌卫的中枢：订阅锁屏/解锁、键鼠空闲、手动操作等事件，驱动
COMPANION（陪伴）与 PATROL（值守）之间的切换，并向桌宠、安防子系统
广播状态变化。

关键决议 D15：**系统解锁事件是唯一权威退出信号**。
- 人脸识别结果（陌生人/主人）**不驱动状态转换**，只触发对应副作用
  （证据录制 / 欢迎预热）。
- 解锁即退出值守，不论人脸识别结果如何。
- 主人在值守中被识别 → owner_greeted 信号（标记欢迎预热），不退出。
- 陌生人在值守中被检测 → stranger_detected 信号（驱动证据录制），不改变状态。
"""

from __future__ import annotations

import logging
import time
from enum import Enum, auto

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class AppState(Enum):
    """应用主状态。"""

    COMPANION = auto()  # 陪伴模式：主人在座，桌宠互动，摄像头关闭
    PATROL = auto()  # 值守模式：主人离座，安防激活，摄像头开启


class StateMachine(QObject):
    """管理陪伴与值守之间的状态转换。

    信号说明：
    - state_changed:    状态切换（COMPANION ⇄ PATROL）
    - patrol_started:   进入值守（安防子系统应启动摄像头）
    - patrol_ended:     退出值守（安防子系统应停止摄像头 + 触发汇报）
    - stranger_detected: 陌生人事件（安防子系统应录制证据）
    - owner_greeted:    主人识别（仅标记欢迎预热，不退出值守）

    D15: 解锁（on_unlock）是唯一权威退出信号；
         人脸识别结果不驱动状态转换。
    """

    state_changed = Signal(object)  # AppState
    patrol_started = Signal()  # 进入值守（启动摄像头）
    patrol_ended = Signal()  # 退出值守（停止摄像头）
    stranger_detected = Signal()  # 陌生人事件（驱动证据录制）
    owner_greeted = Signal(bool)  # 主人识别（仅标记欢迎预热，不退出值守）

    def __init__(self) -> None:
        super().__init__()
        self._state: AppState = AppState.COMPANION
        self._patrol_start_time: float | None = None
        self._incident_count: int = 0
        self._owner_greeted_this_session: bool = False

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def state(self) -> AppState:
        """当前状态。"""
        return self._state

    @property
    def is_patrolling(self) -> bool:
        """是否处于值守模式。"""
        return self._state is AppState.PATROL

    @property
    def patrol_duration(self) -> float:
        """值守持续时间（秒）；非值守状态返回 0。"""
        if self._patrol_start_time is None:
            return 0.0
        return time.time() - self._patrol_start_time

    @property
    def incident_count(self) -> int:
        """当前值守会话的陌生人事件计数。"""
        return self._incident_count

    # ------------------------------------------------------------------
    # 进入值守的入口（锁屏 / 手动）
    # ------------------------------------------------------------------
    def on_lock_screen(self) -> None:
        """锁屏事件 → 进入值守（D3: 锁屏自动值守，免费默认）。

        仅 COMPANION → PATROL 有效；已在值守则忽略。
        """
        if self._state is not AppState.COMPANION:
            logger.debug("on_lock_screen: 已在值守模式，忽略")
            return
        self._enter_patrol(trigger="lock_screen")

    def on_manual_start(self) -> None:
        """手动启动值守（用户托盘菜单主动操作）。

        仅 COMPANION → PATROL 有效。
        """
        if self._state is not AppState.COMPANION:
            logger.debug("on_manual_start: 已在值守模式，忽略")
            return
        self._enter_patrol(trigger="manual")

    def _enter_patrol(self, *, trigger: str) -> None:
        """进入值守模式的内部统一路径。"""
        self._state = AppState.PATROL
        self._patrol_start_time = time.time()
        self.reset_incidents()
        logger.info("进入值守模式（触发: %s）", trigger)
        self.state_changed.emit(self._state)
        self.patrol_started.emit()

    # ------------------------------------------------------------------
    # 退出值守的入口（解锁 / 手动）
    # ------------------------------------------------------------------
    def on_unlock(self) -> None:
        """解锁事件 → 退出值守（D15: 唯一权威退出信号）。

        仅 PATROL → COMPANION 有效。
        注意：不论人脸识别结果如何，解锁就退出。
        """
        if self._state is not AppState.PATROL:
            logger.debug("on_unlock: 不在值守模式，忽略")
            return
        self._exit_patrol()

    def on_manual_stop(self) -> None:
        """手动退出值守（用户托盘菜单主动操作）。

        仅 PATROL → COMPANION 有效。
        """
        if self._state is not AppState.PATROL:
            logger.debug("on_manual_stop: 不在值守模式，忽略")
            return
        self._exit_patrol()

    def _exit_patrol(self) -> None:
        """退出值守模式的内部统一路径。"""
        duration = self.patrol_duration
        logger.info(
            "退出值守模式（持续 %.1fs，事件 %d 次）",
            duration,
            self._incident_count,
        )
        self._state = AppState.COMPANION
        self._patrol_start_time = None
        self.state_changed.emit(self._state)
        self.patrol_ended.emit()

    # ------------------------------------------------------------------
    # 值守中的检测事件（D15: 不驱动状态转换）
    # ------------------------------------------------------------------
    def on_stranger(self) -> None:
        """陌生人检测事件（值守中有效）。

        D15: 不改变状态，仅 emit stranger_detected。事件计数必须等到
        证据目录和数据库都提交成功后由 ``confirm_incident`` 增加，避免
        冷却、写盘失败或取消造成账实不一致。
        """
        if self._state is not AppState.PATROL:
            logger.debug("on_stranger: 不在值守模式，忽略")
            return
        logger.warning("检测到陌生人，等待证据提交")
        self.stranger_detected.emit()

    def confirm_incident(self) -> None:
        """在证据和数据库原子提交后确认本次值守事件。"""
        if self._state is not AppState.PATROL:
            logger.debug("confirm_incident: 已退出值守，忽略")
            return
        self._incident_count += 1
        logger.warning("陌生人事件已提交（第 %d 次）", self._incident_count)

    def on_owner_recognized(self) -> None:
        """主人识别事件（D15: 不驱动状态转换）。

        仅在 PATROL 状态有效，emit owner_greeted 标记「欢迎预热」。
        **不退出值守**——退出值守的唯一权威信号是解锁（on_unlock）。
        owner_greeted 的 bool 参数表示是否为本次值守首次识别（True=首次预热）。
        """
        if self._state is not AppState.PATROL:
            logger.debug("on_owner_recognized: 不在值守模式，忽略")
            return
        is_first = not self._owner_greeted_this_session
        self._owner_greeted_this_session = True
        logger.info("值守中识别到主人（欢迎预热，不退出值守，首次=%s）", is_first)
        self.owner_greeted.emit(is_first)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def reset_incidents(self) -> None:
        """重置事件计数与欢迎标记（每次进入值守时调用）。"""
        self._incident_count = 0
        self._owner_greeted_this_session = False
