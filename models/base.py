"""模型适配层：所有对大模型的调用都经过这里，保证"可替换性"（任务书 §5）。

换模型 = 换一个 adapter 实现，上层（理解、回忆）完全不动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ModelError(Exception):
    """模型调用的基类错误。"""


class ModelUnavailableError(ModelError):
    """模型服务连不上 / 模型不存在。"""


class BaseModelAdapter(ABC):
    """最小接口：一轮对话（可附图）。"""

    name: str = "base"

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """messages: [{"role": "system|user|assistant", "content": str}]
        images: base64 字符串列表（只附在最后一条 user 消息上）。"""

    def complete(self, system: str, user: str, *, timeout: float | None = None) -> str:
        return self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=timeout,
        )
