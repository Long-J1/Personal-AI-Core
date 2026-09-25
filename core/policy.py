"""判断是否值得记忆（任务书 §4 第4步、§7"什么值得记"）。

V0.1 策略：默认全记（先把数据攒起来），阈值可通过配置调高；
未来在这里接入更聪明的取舍逻辑，接口不变。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from memory.event import Event

log = logging.getLogger("core.policy")


@dataclass
class PolicyDecision:
    remember: bool
    reason: str


class MemoryPolicy:
    def __init__(self, min_importance: float = 0.0):
        self.min_importance = min_importance

    def should_remember(
        self, event: Event, *, paused: bool, parse_ok: bool
    ) -> PolicyDecision:
        if paused:
            return PolicyDecision(False, "隐私暂停中，不记录")
        if not parse_ok:
            # 保守原则：理解失败的原文也记下来，避免丢记忆（可事后清理）。
            # 放在重要度阈值之前——原始文本的重要性评分本来就是默认值，不可信。
            return PolicyDecision(True, "理解未结构化，按原文保守记忆")
        if event.importance < self.min_importance:
            return PolicyDecision(
                False,
                f"重要度 {event.importance:.2f} 低于阈值 {self.min_importance:.2f}",
            )
        return PolicyDecision(
            True,
            f"通过（重要度 {event.importance:.2f}）",
        )
