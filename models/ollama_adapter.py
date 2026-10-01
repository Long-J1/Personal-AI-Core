"""Ollama 适配器：本机模型的 HTTP 客户端（/api/chat）。

注意：httpx 使用 trust_env=False，明确不走代理——本地服务不该被 HTTP_PROXY 拦。
"""
from __future__ import annotations

import logging

import httpx

from .base import BaseModelAdapter, ModelUnavailableError, ServiceCheck

log = logging.getLogger("models.ollama")


class OllamaAdapter(BaseModelAdapter):
    name = "ollama"

    def __init__(self, url: str, model: str, default_timeout: float = 180.0,
                 *, transport=None):
        self.url = url.rstrip("/")
        self.model = model
        self.default_timeout = default_timeout
        self._transport = transport  # 测试注入用（MockTransport），生产为 None

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

    def chat_stream(
        self,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        timeout: float | None = None,
    ):
        """NDJSON 流式：每行一个 message.content。拼起来 = chat()。"""
        import json as _json
        payload_messages = [dict(m) for m in messages]
        if images:
            for msg in reversed(payload_messages):
                if msg.get("role") == "user":
                    msg["images"] = list(images)
                    break
        body = {
            "model": self.model,
            "messages": payload_messages,
            "stream": True,
            "think": False,
            "options": {"temperature": 0.3, "num_predict": 512},
        }
        try:
            with httpx.Client(timeout=timeout or self.default_timeout,
                              trust_env=False).stream(
                "POST", f"{self.url}/api/chat", json=body,
            ) as resp:
                if resp.status_code == 404:
                    raise ModelUnavailableError(
                        f"模型不存在或服务无此路由：{self.model}")
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ModelUnavailableError(
                        f"Ollama 返回 {resp.status_code}") from exc
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        obj = _json.loads(line)
                    except ValueError:
                        continue
                    chunk = (obj.get("message") or {}).get("content") or ""
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"无法连接 Ollama（{self.url}）：{exc}"
            ) from exc

    def ping(self, timeout: float = 2.0) -> bool:
        """健康检查（/api/version）。"""
        try:
            return httpx.get(f"{self.url}/api/version", timeout=timeout, trust_env=False).status_code == 200
        except httpx.HTTPError:
            return False

    async def chat_stream_async(
        self,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        timeout: float | None = None,
    ):
        """异步流式（SSE 真链路用）：AsyncClient NDJSON。拼起来 = chat()。"""
        import json as _json
        payload_messages = [dict(m) for m in messages]
        if images:
            for msg in reversed(payload_messages):
                if msg.get("role") == "user":
                    msg["images"] = list(images)
                    break
        body = {
            "model": self.model,
            "messages": payload_messages,
            "stream": True,
            "think": False,
            "options": {"temperature": 0.3, "num_predict": 512},
        }
        client = httpx.AsyncClient(
            timeout=timeout or self.default_timeout, trust_env=False,
            transport=self._transport)
        async with client:
            try:
                async with client.stream(
                    "POST", f"{self.url}/api/chat", json=body,
                ) as resp:
                    if resp.status_code == 404:
                        raise ModelUnavailableError(
                            f"模型不存在或服务无此路由：{self.model}")
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise ModelUnavailableError(
                            f"Ollama 返回 {resp.status_code}") from exc
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            obj = _json.loads(line)
                        except ValueError:
                            continue
                        chunk = (obj.get("message") or {}).get("content") or ""
                        if chunk:
                            yield chunk
            except httpx.HTTPError as exc:
                raise ModelUnavailableError(
                    f"无法连接 Ollama（{self.url}）：{exc}"
                ) from exc

    def check_service(self, timeout: float = 5.0) -> ServiceCheck:
        """连接测试的结构化判定（模型设置界面 + status 缓存共用）。

        服务在不在（reachable）和本机装没装这个模型（model_ok）分开报，
        免得"端点通"被当成"模型可用"。
        """
        # Ollama 是本地清单，说不准就是没装 → 模型缺失算**失败**（既有语义）
        try:
            resp = httpx.get(f"{self.url}/api/version", timeout=timeout, trust_env=False)
        except httpx.HTTPError as exc:
            return ServiceCheck(reachable=False, model_ok=False,
                                message=f"无法连接 Ollama（{self.url}）：{exc}",
                                test_ok=False)
        if resp.status_code != 200:
            return ServiceCheck(reachable=False, model_ok=False,
                                message=f"Ollama 返回 {resp.status_code}",
                                test_ok=False)
        try:
            version = resp.json().get("version", "?")
        except ValueError:
            version = "?"
        try:
            tags = httpx.get(f"{self.url}/api/tags", timeout=timeout, trust_env=False).json()
            names = [t.get("name", "") for t in tags.get("models", [])]
        except (httpx.HTTPError, ValueError):
            # 清单读不到：服务是活的，具体模型无法判定 → 宽松放行（与既有行为一致）
            return ServiceCheck(reachable=True, model_ok=True,
                                message=f"Ollama {version} 在线（模型清单暂不可读）",
                                test_ok=True)
        if self.model and not any(
            n == self.model or n.startswith(self.model) for n in names
        ):
            return ServiceCheck(reachable=True, model_ok=False, names=tuple(names),
                                message=f"Ollama {version} 在线，但本机没有模型「{self.model}」",
                                test_ok=False)
        return ServiceCheck(reachable=True, model_ok=True, names=tuple(names),
                            message=f"Ollama {version} · {self.model}",
                            test_ok=True)

    def test_connection(self, timeout: float = 5.0) -> tuple[bool, str]:
        """连接测试（模型设置界面用）：服务在不在 + 模型装没装。

        保持既有语义：本机没装这个模型 = 失败（Ollama 是本地清单，说不准就是没有）。
        """
        c = self.check_service(timeout)
        return (c.test_ok, c.message)


# 别名：任务书里的 OllamaProvider 就是这个类（保留原名不破坏既有导入）
OllamaProvider = OllamaAdapter
