"""主动介入策略接口（任务书 §8）。

V0.1：不实现自动介入（永远沉默），但管线每产生一个事件都会调用 decide()，
未来在这里实现"事件重要性 / 用户状态 / 收益 vs 打扰"的判断流程，上层无需改动。
"""
from __future__ import annotations

from dataclasses import dataclass

from memory.event import Event


@dataclass
class InterventionDecision:
    intervene: bool = False
    channel: str | None = None          # 未来：push / voice / ui_toast...
    reason: str = "V0.1：默认沉默，仅后台记录"


class InterventionPolicy:
    """占位实现：按任务书 §8 的决策树，V0.1 一律选择"保持沉默 / 后台记录"。"""

    def decide(
        self,
        event: Event,
        *,
        user_active: bool = False,
        recent_interventions: int = 0,
    ) -> InterventionDecision:
        return InterventionDecision(
            intervene=False,
            channel=None,
            reason=f"V0.1 默认沉默，后台记录事件（重要度 {event.importance:.2f}）",
        )
