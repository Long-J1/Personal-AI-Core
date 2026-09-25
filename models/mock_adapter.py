"""测试用 Mock 模型：不依赖网络/显卡，让 20 个闭环测试秒级、确定性地跑。

行为约定（与真实流程一一对应）：
- 带图调用（感知→理解）：按队列返回预置的"理解结果 JSON"；队列空则返回默认 JSON；
- 纯文本调用（回忆）：从提示词里抽取 "- [时间] 描述" 的记忆行，按模板拼成回答。
  这样测试断言的是"真的把记忆装进了上下文并产出回答"，而不是空壳。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import BaseModelAdapter

_DEFAULT_UNDERSTANDING: dict[str, Any] = {
    "scene": "未知场景",
    "activity": "未知活动",
    "objects": [],
    "description": "测试图片",
    "importance": 0.5,
    "facts": [],
}


class MockChatModel(BaseModelAdapter):
    name = "mock"

    def __init__(
        self,
        understanding_queue: list[str] | None = None,
        default_understanding: dict[str, Any] | None = None,
        fail_with: Exception | None = None,
    ):
        self.queue: list[str] = list(understanding_queue or [])
        self.default_understanding = default_understanding or _DEFAULT_UNDERSTANDING
        self.fail_with = fail_with
        self.calls: list[dict] = []

    def chat(
        self,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        timeout: float | None = None,
    ) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append({"images": bool(images), "messages": messages})

        if images:
            if self.queue:
                return self.queue.pop(0)
            return json.dumps(self.default_understanding, ensure_ascii=False)

        # 回忆：从提示词中提取记忆行 "- [日期 时间] ..."
        user_text = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        )
        lines = [ln for ln in user_text.splitlines() if ln.startswith("- [")]
        if not lines:
            return "我的记忆里没有找到相关事件。"
        body = "\n".join(ln[2:] for ln in lines)
        labels = re.findall(r"- \[([^\]]+)\]", user_text)[:5]
        return f"根据记忆回答：\n{body}\n依据：{'、'.join(labels)}"
