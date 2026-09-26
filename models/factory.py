"""模型工厂 + 热切换注册表（D009）。

- build_model(): 按配置装配 Provider，Core 不关心当前用的是哪一家
- ModelRegistry(): Core 各服务持有的"模型门面"——切换 Provider 只换门面后的对象，
  Conversation / Context / Memory 一概不动（换模型不换"它"）。
"""
from __future__ import annotations

import logging

from core.config import settings as app_settings

from .base import BaseModelAdapter, ModelUnavailableError
from .ollama_adapter import OllamaAdapter
from .openai_provider import OpenAICompatibleProvider
from .settings_store import ModelSettingsStore, default_store

log = logging.getLogger("models.factory")


def build_model(cfg: dict, *, timeout: float | None = None) -> BaseModelAdapter:
    """按配置装配一个 Provider；配置无效抛 ModelUnavailableError。"""
    provider = cfg.get("provider", "ollama")
    t = timeout or app_settings.understand_timeout
    if provider == "openai":
        o = cfg.get("openai", {})
        base = (o.get("base_url") or "").strip()
        if not base:
            raise ModelUnavailableError("云端 API 未配置 Base URL")
        model = (o.get("model") or "").strip()
        if not model:
            raise ModelUnavailableError("云端 API 未配置模型名")
        return OpenAICompatibleProvider(base, model, o.get("api_key", ""), t)
    o = cfg.get("ollama", {})
    url = (o.get("url") or "").strip() or app_settings.ollama_url
    model = (o.get("model") or "").strip() or app_settings.model
    return OllamaAdapter(url, model, t)


class _BrokenModel(BaseModelAdapter):
    """配置无效时的占位：不让 App 起不来，把错误显式暴露给状态/连接测试。"""

    name = "unconfigured"

    def __init__(self, reason: str):
        self.reason = reason
        self.model = "（配置无效）"

    def chat(self, messages, *, images=None, timeout=None) -> str:
        raise ModelUnavailableError(f"模型配置无效：{self.reason}")

    def ping(self, timeout: float = 2.0) -> bool:
        return False

    def test_connection(self, timeout: float = 8.0) -> tuple[bool, str]:
        return False, f"配置无效：{self.reason}"


class ModelRegistry(BaseModelAdapter):
    """模型门面：Core 只持有它；reload() 热切换 Provider，不触碰任何用户数据。"""

    name = "registry"

    def __init__(self, store: ModelSettingsStore | None = None):
        self._store = store or default_store()
        self._current = self._build()

    # ---------- 门面 ----------
    @property
    def settings_store(self) -> ModelSettingsStore:
        return self._store

    @property
    def current(self) -> BaseModelAdapter:
        return self._current

    def _build(self) -> BaseModelAdapter:
        try:
            return build_model(self._store.load())
        except Exception as exc:  # 配置坏了也不许 App 崩溃
            log.warning("模型配置无效，降级为不可用状态：%s", exc)
            return _BrokenModel(str(exc))

    def reload(self) -> BaseModelAdapter:
        """从存储重新装配（保存设置后调用）——只换推理引擎，不碰数据。"""
        self._current = self._build()
        return self._current

    def chat(self, messages, *, images=None, timeout=None) -> str:
        return self._current.chat(messages, images=images, timeout=timeout)

    def describe(self) -> dict:
        cur = self._current
        info = {"provider": cur.name, "model": getattr(cur, "model", "")}
        if isinstance(cur, _BrokenModel):
            info["error"] = cur.reason
        return info

    def __getattr__(self, item: str):
        """未定义属性（ping / test_connection / model 等）转发给当前 Provider。"""
        if item.startswith("_"):
            raise AttributeError(item)
        cur = self.__dict__.get("_current")
        if cur is None:
            raise AttributeError(item)
        return getattr(cur, item)
