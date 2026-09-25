"""Ollama 适配器：本机模型的 HTTP 客户端（/api/chat）。

注意：httpx 使用 trust_env=False，明确不走代理——本地服务不该被 HTTP_PROXY 拦。
"""
from __future__ import annotations

import logging

import httpx

from .base import BaseModelAdapter, ModelUnavailableError

log = logging.getLogger("models.ollama")


class OllamaAdapter(BaseModelAdapter):
    name = "ollama"

    def __init__(self, url: str, model: str, default_timeout: float = 180.0):
        self.url = url.rstrip("/")
        self.model = model
        self.default_timeout = default_timeout

    def chat(
        self,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        timeout: float | None = None,
    ) -> str:
        payload_messages = [dict(m) for m in messages]
        if images:
            # Ollama 要求图片挂在消息上：附到最后一条 user 消息
            for msg in reversed(payload_messages):
                if msg.get("role") == "user":
                    msg["images"] = list(images)
                    break

        try:
            resp = httpx.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "messages": payload_messages,
                    "stream": False,
                    # 1) 关思考：本机模型的 thinking 阶段会吃光 token 预算，
                    #    造成"回答为空"和"沉淀 JSON 被截断"两种病（V0.2 验收实测，D008 补记）；
                    #    结构化提取/回忆/日常对话都不需要长思考，直接出答案更快更稳。
                    # 2) num_predict 显式给足 512：关掉思考后全是正文预算，
                    #    回答和沉淀 JSON 都够用。
                    "think": False,
                    "options": {"temperature": 0.3, "num_predict": 512},
                },
                timeout=timeout or self.default_timeout,
                trust_env=False,
            )
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"无法连接 Ollama（{self.url}）：{exc}"
            ) from exc

        if resp.status_code == 404:
            body = resp.text[:200]
            raise ModelUnavailableError(f"模型不存在或服务无此路由：{self.model} | {body}")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ModelUnavailableError(
                f"Ollama 返回 {resp.status_code}：{resp.text[:300]}"
            ) from exc

        data = resp.json()
        content = (data.get("message") or {}).get("content", "")
        log.debug(
            "ollama 完成 done=%s eval=%s 耗时=%.1fs",
            data.get("done_reason"),
            data.get("eval_count"),
            (data.get("total_duration") or 0) / 1e9,
        )
        return content

    def ping(self, timeout: float = 2.0) -> bool:
        """健康检查（/api/version）。"""
        try:
            return httpx.get(f"{self.url}/api/version", timeout=timeout, trust_env=False).status_code == 200
        except httpx.HTTPError:
            return False
