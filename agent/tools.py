"""工具调用接口（任务书 §6：保留工具调用接口，先只实现安全、有限的工具）。

V0.1 只提供"查记忆"类只读工具；未来 Agent 编排在这里扩展，不动核心数据结构。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from memory.store import MemoryStore

log = logging.getLogger("agent.tools")


@dataclass
class ToolResult:
    ok: bool
    message: str
    data: Any = None


class Tool:
    """最小工具协议：名字 + 描述 + 执行函数（LLM 可读的声明）。"""

    def __init__(self, name: str, description: str, runner: Callable[..., ToolResult]):
        self.name = name
        self.description = description
        self._runner = runner

    def run(self, **kwargs: Any) -> ToolResult:
        try:
            result = self._runner(**kwargs)
            log.info("工具执行 %s(%s) → ok=%s", self.name, kwargs, result.ok)
            return result
        except Exception as exc:
            log.error("工具 %s 执行失败：%s", self.name, exc)
            return ToolResult(ok=False, message=f"工具执行失败：{exc}")

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_all(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]


def build_default_registry(store: MemoryStore) -> ToolRegistry:
    """V0.1 的安全工具集：只读检索记忆。"""

    def search_memories(keywords: list[str] | None = None, limit: int = 10) -> ToolResult:
        events = store.search_events(keywords or [], limit=max(1, min(limit, 50)))
        payload = [
            {
                "time": f"{e.date_label} {e.time_label}",
                "description": e.description,
                "scene": e.scene,
                "importance": e.importance,
            }
            for e in events
        ]
        return ToolResult(
            ok=True,
            message=f"找到 {len(payload)} 条记忆",
            data=payload,
        )

    def recent_memories(limit: int = 10) -> ToolResult:
        events = store.list_events(limit=max(1, min(limit, 50)))
        payload = [
            {"time": f"{e.date_label} {e.time_label}", "description": e.description}
            for e in events
        ]
        return ToolResult(ok=True, message=f"最近 {len(payload)} 条", data=payload)

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="search_memories",
            description="按关键词检索记忆库，返回匹配的事件列表（只读）。",
            runner=search_memories,
        )
    )
    registry.register(
        Tool(
            name="recent_memories",
            description="按时间倒序返回最近的事件（只读）。",
            runner=recent_memories,
        )
    )
    return registry
