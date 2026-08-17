"""桌宠形象层：QPixmap 帧动画渲染与反馈呈现。

T8 选定 QPixmap 序列自绘方案（M0-1 已验证），Live2D 已放弃。

模块导出：
- FrameAnimationController: 帧动画渲染引擎（与 PetWindow 渲染逻辑分离）
- PingPongSequence: ping-pong 循环索引辅助类
- FeedbackController: 状态机信号驱动帧组切换 + 台词
- PetMood: 桌宠情绪枚举
- PetWindow: 透明置顶桌宠窗口
"""

from moeguard.pet.feedback import FeedbackController, PetMood
from moeguard.pet.frame_animation import FrameAnimationController, PingPongSequence

__all__ = [
    "FeedbackController",
    "FrameAnimationController",
    "PetMood",
    "PingPongSequence",
]
