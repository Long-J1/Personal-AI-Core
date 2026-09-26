"""OpenAI-compatible 云端模型适配器（通用 /chat/completions，不绑定任何一家厂商）。

- Base URL / API Key / 模型名全部可配（见 models/settings_store.py）
- trust_env=True（与 Ollama 相反）：云端请求可能需要走系统代理才能出网
- 图片按 OpenAI 规范转成 image_url 内容块（data URI），只挂到最后一条 user 消息
"""
from __future__ import annotations

import logging

import httpx

from .base import BaseModelAdapter, ModelUnavailableError

log = logging.getLogger("models.openai")


class OpenAICompatibleProvider(BaseModelAdapter):
    name = "openai"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        default_timeout: float = 180.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        self.default_timeout = default_timeout
        self._transport = transport  # 测试注入用（MockTransport），生产为 None

    # ---------- 内部 ----------
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(
            headers=self._headers(),
            timeout=timeout,
            transport=self._transport,
            trust_env=self._transport is None,  # 真实请求走系统代理；测试传输不走
        )

    @staticmethod
    def _attach_images(messages: list[dict], images: list[str]) -> list[dict]:
        """把图片转成 OpenAI 内容块，挂到最后一条 user 消息（对齐 Ollama 行为）。"""
        payload = [dict(m) for m in messages]
        last_user = None
        for i, m in enumerate(payload):
            if m.get("role") == "user":
                last_user = i
        if last_user is None:
            return payload
        content = payload[last_user].get("content") or ""
        parts: list[dict] = (
            list(content) if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        )
        parts += [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}}
            for img in images
        ]
        payload[last_user]["content"] = parts
        return payload

    # ---------- 主接口 ----------
    def chat(
        self,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        timeout: float | None = None,
    ) -> str:
        payload = self._attach_images(messages, images) if images else [dict(m) for m in messages]
        body = {
            "model": self.model,
            "messages": payload,
            "temperature": 0.3,
            "max_tokens": 512,
        }
        url = f"{self.base_url}/chat/completions"
        try:
            with self._client(timeout or self.default_timeout) as client:
                resp = client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"无法连接模型服务（{self.base_url}）：{exc}") from exc

        if resp.status_code in (401, 403):
            raise ModelUnavailableError(f"API Key 无效或未授权（{resp.status_code}）")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ModelUnavailableError(
                f"模型服务返回 {resp.status_code}：{resp.text[:300]}"
            ) from exc

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailableError(f"响应格式无法解析：{resp.text[:200]}") from exc
        log.debug("openai 完成 model=%s chars=%s", self.model, len(content))
        return content

    # ---------- 健康检查 / 连接测试（模型设置界面用） ----------
    def ping(self, timeout: float = 2.0) -> bool:
        """服务可达即算在线（部分兼容 API 没有 /models，404 不代表不可用）。"""
        try:
            with self._client(timeout) as client:
                client.get(f"{self.base_url}/models")
            return True
        except httpx.HTTPError:
            return False

    def test_connection(self, timeout: float = 8.0) -> tuple[bool, str]:
        # 1) 优先 GET /models（便宜）
        try:
            with self._client(timeout) as client:
                resp = client.get(f"{self.base_url}/models")
            if resp.status_code == 200:
                try:
                    names = [
                        m.get("id")
                        for m in resp.json().get("data", [])
                        if isinstance(m, dict)
                    ]
                except ValueError:
                    names = []
                if names:
                    if self.model and self.model not in names:
                        return True, (
                            f"连接成功，但服务列表里没有「{self.model}」"
                            f"（共 {len(names)} 个模型）。若该服务支持直接指定模型名，可忽略此提示。"
                        )
                    return True, f"连接成功（服务上有 {len(names)} 个可用模型）"
                return True, "连接成功"
            if resp.status_code in (401, 403):
                return False, f"鉴权失败（{resp.status_code}），请检查 API Key"
            if resp.status_code not in (404, 405):
                return False, f"服务返回 {resp.status_code}：{resp.text[:200]}"
        except httpx.HTTPError as exc:
            return False, f"无法连接 {self.base_url}：{exc}"

        # 2) 服务没有 /models 路由 → 用最小对话试探（1 token）
        try:
            with self._client(timeout) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                )
            if resp.status_code == 200:
                return True, "连接成功（该服务无 /models，已走对话接口探测）"
            if resp.status_code in (401, 403):
                return False, f"鉴权失败（{resp.status_code}），请检查 API Key"
            return False, f"服务返回 {resp.status_code}：{resp.text[:200]}"
        except httpx.HTTPError as exc:
            return False, f"无法连接 {self.base_url}：{exc}"
