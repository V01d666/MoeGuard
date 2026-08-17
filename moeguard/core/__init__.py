"""核心调度层：状态机与在座/离座检测。"""

from moeguard.core.presence import LockScreenMonitor, PresenceDetector
from moeguard.core.state_machine import AppState, StateMachine

__all__ = ["AppState", "StateMachine", "LockScreenMonitor", "PresenceDetector"]
