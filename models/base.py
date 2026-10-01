"""模型适配层：所有对大模型的调用都经过这里，保证"可替换性"（任务书 §5）。

换模型 = 换一个 adapter 实现，上层（理解、回忆）完全不动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ModelError(Exception):
    """模型调用的基类错误。"""


class ModelUnavailableError(ModelError):
    """模型服务连不上 / 模型不存在。"""


@dataclass(frozen=True)
class ServiceCheck:
    """连接测试的结构化结果。

    `reachable` 与 `model_ok` 必须分开：**端点通 ≠ 当前配的模型能用**。
    端点可达但清单里没有那个模型时（实测智谱 /models 就没有 glm-4-flash），
    拿 reachable 当"健康"是骗人的徽章。

    刻意**不提供** `ok` 属性：各 Provider 的 `test_connection()` 对"模型不在
    清单"的既有约定不同（OpenAI 当警告仍返回 True，Ollama 当错误返回 False），
    那是用户可见行为，本次不动。统一的严格判定由调用方用
    `reachable and model_ok` 表达（status 缓存就是这么写的）。

    这是模型可用性的唯一判定来源；别在别处再写一套"模型在不在列表里"的判断。
    """
    reachable: bool          # 服务端点可达（含鉴权通过）
    model_ok: bool           # 当前配置的模型确实可用
    message: str             # 给人看的说明（test_connection 直接返回它）
    # 必填、无默认值：按本 Provider 既有约定，test_connection 会返回的 ok。
    # 刻意不给默认——漏写一个分支会静默变成"看起来正常"，那正是最危险的。
    test_ok: bool
    names: tuple = field(default_factory=tuple)   # 服务自报的模型清单（没有就空）

    @property
    def healthy(self) -> bool:
        """严格健康：端点通 **且** 当前模型可用。status 缓存只认这个。"""
        return bool(self.reachable and self.model_ok)


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

    def chat_stream(
        self,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        timeout: float | None = None,
    ):
        """流式对话：yield 内容片段（str），结束即完整。

        默认实现（向后兼容）：一次性调 chat，整体 yield 一次。
        真流式由各 provider 重写。调用方必须把所有片段拼起来
        得到完整文本（与 chat() 等价）。
        """
        yield self.chat(messages, images=images, timeout=timeout)
