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

    def test_connection(self, timeout: float = 5.0) -> tuple[bool, str]:
        """连接测试（模型设置界面用）：服务在不在 + 模型装没装。"""
        try:
            resp = httpx.get(f"{self.url}/api/version", timeout=timeout, trust_env=False)
        except httpx.HTTPError as exc:
            return False, f"无法连接 Ollama（{self.url}）：{exc}"
        if resp.status_code != 200:
            return False, f"Ollama 返回 {resp.status_code}"
        try:
            version = resp.json().get("version", "?")
        except ValueError:
            version = "?"
        try:
            tags = httpx.get(f"{self.url}/api/tags", timeout=timeout, trust_env=False).json()
            names = [t.get("name", "") for t in tags.get("models", [])]
        except (httpx.HTTPError, ValueError):
            return True, f"Ollama {version} 在线（模型清单暂不可读）"
        if self.model and not any(
            n == self.model or n.startswith(self.model) for n in names
        ):
            return False, f"Ollama {version} 在线，但本机没有模型「{self.model}」"
        return True, f"Ollama {version} · {self.model}"


# 别名：任务书里的 OllamaProvider 就是这个类（保留原名不破坏既有导入）
OllamaProvider = OllamaAdapter
