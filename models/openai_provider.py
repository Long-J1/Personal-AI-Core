"""OpenAI-compatible 云端模型适配器（通用 /chat/completions，不绑定任何一家厂商）。

- Base URL / API Key / 模型名全部可配（见 models/settings_store.py）
- trust_env=True（与 Ollama 相反）：云端请求可能需要走系统代理才能出网
- 图片按 OpenAI 规范转成 image_url 内容块（data URI），只挂到最后一条 user 消息
"""
from __future__ import annotations

import logging

import httpx

from .base import BaseModelAdapter, ModelUnavailableError, ServiceCheck

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

    # ---------- 网络模式：Auto（直连优先，代理兜底）----------
    # 国内云 API 直连可达，强制走系统代理反而引入单点故障
    # （代理进程不在即 WinError 10061）。直连 1 次 + 代理 1 次，不多试。
    @staticmethod
    def _system_has_proxy() -> tuple[bool, str]:
        """系统是否有代理配置。返回 (有无, 描述)；只读配置，不连网。"""
        import os
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy",
                    "http_proxy", "ALL_PROXY", "all_proxy"):
            val = (os.environ.get(var) or "").strip()
            if val:
                return True, f"env:{var}={val}"
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion"
                    r"\Internet Settings") as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if enabled and (server or "").strip():
                    return True, f"system:{server}"
        except Exception:
            pass
        return False, ""

    @staticmethod
    def _is_connect_failure(exc: BaseException) -> bool:
        """连接层失败（没收到 HTTP 响应）才值得换路重试；
        收到响应的 HTTP 错误（401/5xx 等）换路也没用。"""
        return isinstance(exc, httpx.ConnectError)

    def _client(self, timeout: float, use_proxy: bool) -> httpx.Client:
        return httpx.Client(
            headers=self._headers(),
            timeout=timeout,
            transport=self._transport,
            # 代理只在兜底轮开启；测试传输（Mock）永远不走代理
            trust_env=use_proxy and self._transport is None,
        )

    def _do(self, method: str, url: str, json_body: dict | None,
            timeout: float) -> httpx.Response:
        """Auto：直连 1 次 → 连接层失败且有代理 → 代理 1 次。"""
        try:
            with self._client(timeout, use_proxy=False) as client:
                return getattr(client, method)(
                    url, **({"json": json_body} if json_body is not None
                            else {}))
        except httpx.ConnectError as exc:
            has_proxy, desc = self._system_has_proxy()
            log.info("openai direct 失败（%s），代理配置：%s",
                     exc.__class__.__name__, desc or "无")
            if not has_proxy:
                raise ModelUnavailableError(
                    "无法连接云端 API，请检查网络。"
                    f"（直连失败：{exc.__class__.__name__}）") from exc
            log.info("direct failed → retry via system proxy [%s] %s",
                     self.name, self.base_url)
            try:
                with self._client(timeout, use_proxy=True) as client:
                    return getattr(client, method)(
                        url, **({"json": json_body} if json_body is not None
                                else {}))
            except httpx.ConnectError as exc2:
                raise ModelUnavailableError(
                    "云端 API 直连失败，系统代理也无法连接，"
                    "请检查代理软件或网络。"
                    f"（代理：{desc}；错误：{exc2}）") from exc2
            except httpx.HTTPError as exc2:
                raise ModelUnavailableError(
                    f"无法连接云端 API（代理模式）：{exc2}") from exc2
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"无法连接云端 API：{exc}") from exc

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
            resp = self._do("post", url, body,
                            timeout or self.default_timeout)
        except ModelUnavailableError:
            raise
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

    def chat_stream(
        self,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        timeout: float | None = None,
    ):
        """同步流式（保留：测试/兼容用）。真流式走 chat_stream_async。"""
        import json as _json
        payload = self._attach_images(messages, images) if images else [dict(m) for m in messages]
        body = {
            "model": self.model,
            "messages": payload,
            "temperature": 0.3,
            "max_tokens": 512,
            "stream": True,
        }
        url = f"{self.base_url}/chat/completions"
        timeout_v = timeout or self.default_timeout

        def _open(use_proxy: bool):
            return httpx.Client(
                headers=self._headers(),
                timeout=timeout_v,
                transport=self._transport,
                trust_env=use_proxy and self._transport is None,
            ).stream("POST", url, json=body)

        def _iter(cm) -> "object":
            buf = ""
            with cm as resp:
                if resp.status_code in (401, 403):
                    raise ModelUnavailableError(
                        f"API Key 无效或未授权（{resp.status_code}）")
                if resp.status_code != 200:
                    raise ModelUnavailableError(
                        f"模型服务返回 {resp.status_code}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8", "replace") \
                        if isinstance(line, bytes) else line
                    text = text.strip()
                    if not text.startswith("data:"):
                        continue
                    data = text[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = _json.loads(data)
                    except ValueError:
                        continue
                    try:
                        delta = obj["choices"][0].get("delta", {}) \
                            .get("content") or ""
                    except (KeyError, IndexError, TypeError):
                        continue
                    if delta:
                        buf += delta
                        yield delta

        try:
            yield from _iter(_open(False))
            return
        except httpx.ConnectError as exc:
            has_proxy, desc = self._system_has_proxy()
            log.info("openai stream direct 失败（%s），代理配置：%s",
                     exc.__class__.__name__, desc or "无")
            if not has_proxy:
                raise ModelUnavailableError(
                    "无法连接云端 API，请检查网络。"
                    f"（直连失败：{exc.__class__.__name__}）") from exc
            log.info("direct failed → retry via system proxy [%s] %s",
                     self.name, self.base_url)
            try:
                yield from _iter(_open(True))
                return
            except httpx.ConnectError as exc2:
                raise ModelUnavailableError(
                    "云端 API 直连失败，系统代理也无法连接，"
                    "请检查代理软件或网络。"
                    f"（代理：{desc}；错误：{exc2}）") from exc2
            except httpx.HTTPError as exc2:
                raise ModelUnavailableError(
                    f"无法连接云端 API（代理模式）：{exc2}") from exc2
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"无法连接云端 API：{exc}") from exc

    async def chat_stream_async(
        self,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        timeout: float | None = None,
    ):
        """异步流式（SSE 真链路用）：AsyncClient，无线程切换。

        与同步版同语义：直连 1 次 → 连接层失败且有代理 → 代理 1 次；
        HTTP 错误一经收到即抛。chunk 语义不变。
        """
        import json as _json
        payload = self._attach_images(messages, images) if images else [dict(m) for m in messages]
        body = {
            "model": self.model,
            "messages": payload,
            "temperature": 0.3,
            "max_tokens": 512,
            "stream": True,
        }
        url = f"{self.base_url}/chat/completions"
        timeout_v = timeout or self.default_timeout

        async def _aiter(use_proxy: bool):
            client = httpx.AsyncClient(
                headers=self._headers(),
                timeout=timeout_v,
                transport=self._transport,
                trust_env=use_proxy and self._transport is None,
            )
            async with client:
                async with client.stream("POST", url, json=body) as resp:
                    if resp.status_code in (401, 403):
                        raise ModelUnavailableError(
                            f"API Key 无效或未授权（{resp.status_code}）")
                    if resp.status_code != 200:
                        raise ModelUnavailableError(
                            f"模型服务返回 {resp.status_code}")
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        text = line.strip()
                        if not text.startswith("data:"):
                            continue
                        data = text[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = _json.loads(data)
                        except ValueError:
                            continue
                        try:
                            delta = obj["choices"][0].get("delta", {}) \
                                .get("content") or ""
                        except (KeyError, IndexError, TypeError):
                            continue
                        if delta:
                            yield delta

        try:
            async for chunk in _aiter(False):
                yield chunk
            return
        except httpx.ConnectError as exc:
            has_proxy, desc = self._system_has_proxy()
            log.info("openai stream direct 失败（%s），代理配置：%s",
                     exc.__class__.__name__, desc or "无")
            if not has_proxy:
                raise ModelUnavailableError(
                    "无法连接云端 API，请检查网络。"
                    f"（直连失败：{exc.__class__.__name__}）") from exc
            log.info("direct failed → retry via system proxy [%s] %s",
                     self.name, self.base_url)
            try:
                async for chunk in _aiter(True):
                    yield chunk
                return
            except httpx.ConnectError as exc2:
                raise ModelUnavailableError(
                    "云端 API 直连失败，系统代理也无法连接，"
                    "请检查代理软件或网络。"
                    f"（代理：{desc}；错误：{exc2}）") from exc2
            except httpx.HTTPError as exc2:
                raise ModelUnavailableError(
                    f"无法连接云端 API（代理模式）：{exc2}") from exc2
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"无法连接云端 API：{exc}") from exc

    # ---------- 健康检查 / 连接测试（模型设置界面用） ----------
    def ping(self, timeout: float = 2.0) -> bool:
        """服务可达即算在线（部分兼容 API 没有 /models，404 不代表不可用）。"""
        try:
            self._do("get", f"{self.base_url}/models", None, timeout)
            return True
        except (httpx.HTTPError, ModelUnavailableError):
            return False

    def check_service(self, timeout: float = 8.0) -> "ServiceCheck":
        """结构化连接判定：端点可达？当前配置的模型确实可用？

        这是**模型可用性的唯一判定来源**——test_connection 只是它的格式化外壳，
        status 缓存也读它的结果。不要在别处再写一套"模型在不在列表里"的判断。
        """
        # 1) 优先 GET /models（便宜，且不消耗推理 token）
        try:
            resp = self._do("get", f"{self.base_url}/models", None, timeout)
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
                        # 端点通、key 有效，但**当前模型不在这份清单里**：
                        # 这不是"健康"，状态缓存不能写 ok。
                        return ServiceCheck(
                            reachable=True, model_ok=False,
                            message=(f"连接成功，但服务列表里没有「{self.model}」"
                                     f"（共 {len(names)} 个模型）。"
                                     f"若该服务支持直接指定模型名，可忽略此提示。"),
                            names=tuple(names), test_ok=True)
                    return ServiceCheck(
                        reachable=True, model_ok=True,
                        message=f"连接成功（服务上有 {len(names)} 个可用模型）",
                        names=tuple(names), test_ok=True)
                # 服务没给清单：无法判定具体模型，保持宽松（与既有行为一致）
                return ServiceCheck(reachable=True, model_ok=True,
                                    message="连接成功", names=[], test_ok=True)
            if resp.status_code in (401, 403):
                return ServiceCheck(reachable=False, model_ok=False,
                                    message=f"鉴权失败（{resp.status_code}），请检查 API Key", test_ok=False)
            if resp.status_code not in (404, 405):
                return ServiceCheck(
                    reachable=False, model_ok=False,
                    message=f"服务返回 {resp.status_code}：{resp.text[:200]}", test_ok=False)
        except ModelUnavailableError as exc:
            return ServiceCheck(reachable=False, model_ok=False, message=str(exc), test_ok=False)
        except httpx.HTTPError as exc:
            return ServiceCheck(reachable=False, model_ok=False,
                                message=f"无法连接 {self.base_url}：{exc}", test_ok=False)

        # 2) 服务没有 /models 路由 → 用最小对话试探（1 token）
        try:
            resp = self._do(
                "post", f"{self.base_url}/chat/completions",
                {"model": self.model,
                 "messages": [{"role": "user", "content": "ping"}],
                 "max_tokens": 1},
                timeout,
            )
            if resp.status_code == 200:
                return ServiceCheck(
                    reachable=True, model_ok=True,
                    message="连接成功（该服务无 /models，已走对话接口探测）", test_ok=True)
            if resp.status_code in (401, 403):
                return ServiceCheck(reachable=False, model_ok=False,
                                    message=f"鉴权失败（{resp.status_code}），请检查 API Key", test_ok=False)
            return ServiceCheck(reachable=False, model_ok=False,
                                message=f"服务返回 {resp.status_code}：{resp.text[:200]}", test_ok=False)
        except ModelUnavailableError as exc:
            return ServiceCheck(reachable=False, model_ok=False, message=str(exc), test_ok=False)
        except httpx.HTTPError as exc:
            return ServiceCheck(reachable=False, model_ok=False,
                                message=f"无法连接 {self.base_url}：{exc}", test_ok=False)

    def test_connection(self, timeout: float = 8.0) -> tuple[bool, str]:
        # 保持既有语义：模型不在清单只是**警告**，端点通就算连上了
        # （OpenAI 兼容服务未必都维护这份清单，别把人吓住）。
        # 零额外请求：check_service 跑一次，test_ok 就是当年那个返回值。
        c = self.check_service(timeout)
        return (c.test_ok, c.message)
