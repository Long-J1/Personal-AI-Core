"""阶段1：Model Provider 架构（D009）。

覆盖：通用云端 Provider、连接测试、设置存储与 Key 脱敏、
工厂/注册表热切换、**切换 Provider 不丢 Context**、模型设置 API。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from interfaces.webapp import create_app
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.store import MemoryStore
from models.base import ModelUnavailableError
from models.factory import ModelRegistry, build_model
from models.mock_adapter import MockChatModel
from models.ollama_adapter import OllamaAdapter, OllamaProvider
from models.openai_provider import OpenAICompatibleProvider
from models.settings_store import (
    ModelSettingsStore,
    default_config,
    mask_key,
    public_view,
)

# ============================================================
# A. OpenAICompatibleProvider（通用，不绑定厂商）
# ============================================================

def _openai(handler=None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "https://api.example.com/v1",
        "test-model",
        "sk-secret-key-123",
        transport=httpx.MockTransport(handler or _ok_handler),
    )


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "你好"}}]})


def test_openai_chat_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return _ok_handler(request)

    p = _openai(handler=handler)
    out = p.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}])
    assert out == "你好"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-secret-key-123"
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["messages"][1]["content"] == "hi"


def test_openai_images_become_data_url_parts():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok_handler(request)

    p = _openai(handler=handler)
    p.chat([{"role": "user", "content": "看图"}], images=["AAA", "BBB"])
    content = captured["body"]["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,AAA"
    assert content[2]["image_url"]["url"] == "data:image/jpeg;base64,BBB"


def test_openai_connection_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    p = _openai(handler=handler)
    with pytest.raises(ModelUnavailableError, match="无法连接"):
        p.chat([{"role": "user", "content": "hi"}])


def test_openai_401_mentions_api_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    p = _openai(handler=handler)
    with pytest.raises(ModelUnavailableError, match="API Key"):
        p.chat([{"role": "user", "content": "hi"}])


def test_openai_without_key_sends_no_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return _ok_handler(request)

    p = OpenAICompatibleProvider(
        "https://api.example.com/v1", "m", api_key="",
        transport=httpx.MockTransport(handler),
    )
    p.chat([{"role": "user", "content": "hi"}])
    assert captured["auth"] is None


def test_openai_malformed_response_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": True})

    p = _openai(handler=handler)
    with pytest.raises(ModelUnavailableError, match="无法解析"):
        p.chat([{"role": "user", "content": "hi"}])


# ============================================================
# B. 连接测试（test_connection）
# ============================================================

def test_openai_test_connection_ok_and_model_listed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "test-model"}, {"id": "other"}]})

    ok, detail = _openai(handler=handler).test_connection()
    assert ok is True and "连接成功" in detail


def test_openai_test_connection_model_not_listed_warns():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "only-other"}]})

    ok, detail = _openai(handler=handler).test_connection()
    assert ok is True and "没有" in detail   # 可连通，但提示模型不在列表


def test_openai_test_connection_falls_back_to_chat_when_no_models_route():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url).endswith("/models"):
            return httpx.Response(404, text="no route")
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    ok, detail = _openai(handler=handler).test_connection()
    assert ok is True and "对话接口" in detail
    assert len(seen) == 2


def test_openai_test_connection_refused():
    p = OpenAICompatibleProvider(
        "http://127.0.0.1:9/v1", "m", "k",
        transport=httpx.MockTransport(
            lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused", request=req))
        ),
    )
    ok, detail = p.test_connection()
    assert ok is False and "无法连接" in detail


def test_ollama_test_connection_offline():
    ok, detail = OllamaAdapter("http://127.0.0.1:9", "some-model").test_connection(timeout=1.0)
    assert ok is False and "Ollama" in detail


def test_ollama_provider_alias():
    assert OllamaProvider is OllamaAdapter


# ============================================================
# C. 设置存储 + Key 脱敏
# ============================================================

def test_mask_key():
    assert mask_key("") == ""
    assert mask_key("short") == "*" * len("short")
    masked = mask_key("sk-abcdefghijkl")
    assert masked.startswith("sk-a") and masked.endswith("jkl")
    assert "efghij" not in masked


def test_settings_roundtrip_and_mask(tmp_path):
    st = ModelSettingsStore(tmp_path / "ms.json")
    cfg = st.update({
        "provider": "openai",
        "openai": {"base_url": "https://x/v1", "model": "m1", "api_key": "sk-abcdef123456"},
    })
    view = json.dumps(public_view(cfg), ensure_ascii=False)
    assert "sk-abcdef123456" not in view, "脱敏视图绝不能含明文 Key"
    assert public_view(cfg)["openai"]["api_key_set"] is True

    # 重新读盘：Key 还在（数据目录持久化）
    assert ModelSettingsStore(tmp_path / "ms.json").load()["openai"]["api_key"] == "sk-abcdef123456"


def test_api_key_patch_semantics(tmp_path):
    st = ModelSettingsStore(tmp_path / "ms.json")
    st.update({"openai": {"base_url": "https://x/v1", "model": "m", "api_key": "sk-key1"}})
    # 不带 api_key 字段 → 不修改
    cfg = st.update({"openai": {"model": "m2"}})
    assert cfg["openai"]["api_key"] == "sk-key1"
    # 空串 → 清除
    cfg = st.update({"openai": {"api_key": ""}})
    assert cfg["openai"]["api_key"] == ""


def test_update_validation(tmp_path):
    st = ModelSettingsStore(tmp_path / "ms.json")
    with pytest.raises(ValueError, match="Provider"):
        st.update({"provider": "azure"})
    with pytest.raises(ValueError, match="Base URL"):
        st.update({"provider": "openai", "openai": {"model": "m"}})
    with pytest.raises(ValueError, match="模型名"):
        st.update({"provider": "openai", "openai": {"base_url": "https://x/v1"}})


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    p = tmp_path / "ms.json"
    p.write_text("{broken json", encoding="utf-8")
    cfg = ModelSettingsStore(p).load()
    assert cfg == default_config()


def test_missing_file_returns_defaults(tmp_path):
    cfg = ModelSettingsStore(tmp_path / "nope.json").load()
    assert cfg["provider"] == "ollama"
    assert cfg["ollama"]["url"] and cfg["ollama"]["model"]


# ============================================================
# D. 工厂 + 注册表（热切换不碰数据）
# ============================================================

def test_factory_builds_each_provider(tmp_path):
    st = ModelSettingsStore(tmp_path / "ms.json")
    assert isinstance(build_model(st.load()), OllamaAdapter)
    cfg = st.update({"provider": "openai",
                     "openai": {"base_url": "https://x/v1", "model": "m", "api_key": "k"}})
    p = build_model(cfg)
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.base_url == "https://x/v1" and p.model == "m"


def test_registry_reload_switches_provider(tmp_path):
    st = ModelSettingsStore(tmp_path / "ms.json")
    reg = ModelRegistry(st)
    assert isinstance(reg.current, OllamaAdapter)

    st.update({"provider": "openai",
               "openai": {"base_url": "https://x/v1", "model": "m", "api_key": "sk-zz"}})
    reg.reload()
    assert isinstance(reg.current, OpenAICompatibleProvider)
    assert "sk-zz" not in json.dumps(reg.describe()), "describe 不得泄露 Key"


def test_registry_broken_config_degrades_not_crash(tmp_path):
    p = tmp_path / "ms.json"
    p.write_text(json.dumps({"provider": "openai",
                             "openai": {"base_url": "", "model": "", "api_key": ""}}),
                 encoding="utf-8")
    reg = ModelRegistry(ModelSettingsStore(p))   # 不许抛异常
    assert reg.describe()["provider"] == "unconfigured"
    assert "error" in reg.describe()
    with pytest.raises(ModelUnavailableError):
        reg.chat([{"role": "user", "content": "hi"}])
    assert reg.ping() is False


# ============================================================
# E. 模型设置 API（含 Key 不回显、切换不丢 Context）
# ============================================================

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    import core.pipeline as pm

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    store = MemoryStore(tmp_path / "api.db")
    registry = ModelRegistry(ModelSettingsStore(tmp_path / "ms.json"))
    app = create_app(store=store, model=registry,
                     context_store=ContextStore(tmp_path / "api.db"),
                     chat_store=ChatStore(tmp_path / "api.db"))
    return TestClient(app), store, tmp_path


def test_status_includes_provider(api_client):
    c, _, _ = api_client
    s = c.get("/api/status").json()
    assert s["provider"] == "ollama"


def test_get_settings_never_returns_raw_key(api_client):
    c, _, tmp = api_client
    r = c.put("/api/model/settings", json={
        "provider": "openai",
        "openai": {"base_url": "https://x/v1", "model": "m1", "api_key": "sk-secret-XYZ123"},
    })
    assert r.status_code == 200 and r.json()["ok"]

    for endpoint in ("/api/model/settings", "/api/status"):
        blob = json.dumps(c.get(endpoint).json(), ensure_ascii=False)
        assert "sk-secret-XYZ123" not in blob, f"{endpoint} 泄露了明文 Key"

    # Key 落盘在数据目录（不进 git：data/ 已在 .gitignore）
    assert "sk-secret-XYZ123" in (tmp / "ms.json").read_text(encoding="utf-8")


def test_put_settings_validation_400(api_client):
    c, _, _ = api_client
    assert c.put("/api/model/settings", json={"provider": "azure"}).status_code == 400
    r = c.put("/api/model/settings", json={"provider": "openai", "openai": {"model": "m"}})
    assert r.status_code == 400 and "Base URL" in r.json()["error"]


def test_switch_provider_preserves_context_and_events(api_client):
    """核心承诺：切换模型只换推理引擎，Personal Context / 事件 / 会话一个都不能少。"""
    c, store, _ = api_client
    ctx = ContextStore(store.db_path)
    entry, _ = ctx.add_entry("interaction", "回答要简短", source="chat")

    before = c.get("/api/context").json()
    assert before["total"] == 1

    # 切到云端 API
    r = c.put("/api/model/settings", json={
        "provider": "openai",
        "openai": {"base_url": "https://x/v1", "model": "cloud-model", "api_key": "k"},
    })
    assert r.status_code == 200
    assert r.json()["current"]["provider"] == "openai"

    after = c.get("/api/context").json()
    assert after["total"] == 1, "切换 Provider 丢了 Context！"
    assert after["items"][0]["content"] == "回答要简短"
    assert after["items"][0]["id"] == entry.id

    # 切回本地 Ollama
    r = c.put("/api/model/settings", json={"provider": "ollama"})
    assert r.json()["current"]["provider"] == "ollama"
    assert c.get("/api/context").json()["total"] == 1
    assert store.count_events() == 0   # 事件数没有被误动


def test_model_test_endpoint_saved_config_and_temp_config(api_client):
    c, _, _ = api_client
    # 临时配置（不落盘）：连不上的 Ollama 端口 → ok=False，服务不崩
    r = c.post("/api/model/test", json={"provider": "ollama",
                                        "ollama": {"url": "http://127.0.0.1:9", "model": "m"}})
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["detail"]
    # 测试不落盘
    assert c.get("/api/model/settings").json()["provider"] == "ollama"
    assert "127.0.0.1:9" not in json.dumps(c.get("/api/model/settings").json())


def test_model_test_endpoint_bad_config_message(api_client):
    c, _, _ = api_client
    r = c.post("/api/model/test", json={"provider": "openai", "openai": {"model": "m"}})
    assert r.json()["ok"] is False and "Base URL" in r.json()["detail"]


def test_injected_mock_model_app_still_works(tmp_path, monkeypatch):
    """依赖注入不被破坏：create_app(model=Mock) 照常工作（113 个旧测试的前提）。"""
    import core.pipeline as pm

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    store = MemoryStore(tmp_path / "mock.db")
    app = create_app(store=store, model=MockChatModel())
    c = TestClient(app)
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/model/settings").status_code == 200


# ============================================================
# Z. Auto 网络模式（直连优先，代理兜底；V0.5.4 发布后修复）
# ============================================================

def _openai_raw() -> OpenAICompatibleProvider:
    """无 transport（走 _do 的两轮逻辑；transport 由各测试注入）。"""
    return OpenAICompatibleProvider(
        "https://api.example.com/v1", "test-model", "sk-x")


def test_auto_direct_success_no_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _ok_handler(request)

    p = OpenAICompatibleProvider(
        "https://api.example.com/v1", "test-model", "sk-x",
        transport=httpx.MockTransport(handler))
    assert p.chat([{"role": "user", "content": "hi"}]) == "你好"
    assert len(calls) == 1  # 直连成功不重试


def test_auto_fallback_once_then_success(monkeypatch, caplog):
    import logging
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return _ok_handler(request)

    monkeypatch.setattr(
        OpenAICompatibleProvider, "_system_has_proxy",
        staticmethod(lambda: (True, "env:TEST_PROXY=1")))
    p = OpenAICompatibleProvider(
        "https://api.example.com/v1", "test-model", "sk-x",
        transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.INFO, logger="models.openai"):
        assert p.chat([{"role": "user", "content": "hi"}]) == "你好"
    assert len(calls) == 2  # 直连 1 + 代理 1，不多试
    assert any("retry via system proxy" in r.getMessage()
               for r in caplog.records)


def test_auto_no_proxy_clear_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(
        OpenAICompatibleProvider, "_system_has_proxy",
        staticmethod(lambda: (False, "")))
    p = OpenAICompatibleProvider(
        "https://api.example.com/v1", "test-model", "sk-x",
        transport=httpx.MockTransport(handler))
    with pytest.raises(ModelUnavailableError) as e:
        p.chat([{"role": "user", "content": "hi"}])
    assert "请检查网络" in str(e.value)
    assert "10061" not in str(e.value) and "WinError" not in str(e.value)


def test_auto_proxy_failure_clear_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[WinError 10061] boom", request=request)

    monkeypatch.setattr(
        OpenAICompatibleProvider, "_system_has_proxy",
        staticmethod(lambda: (True, "system:127.0.0.1:7892")))
    p = OpenAICompatibleProvider(
        "https://api.example.com/v1", "test-model", "sk-x",
        transport=httpx.MockTransport(handler))
    with pytest.raises(ModelUnavailableError) as e:
        p.chat([{"role": "user", "content": "hi"}])
    assert "系统代理也无法连接" in str(e.value)
    assert "127.0.0.1:7892" in str(e.value)


def test_auto_http_errors_no_retry():
    for status, expect in ((401, "鉴权失败"), (400, "400")):
        calls = []

        def handler(request: httpx.Request,
                    _s=status) -> httpx.Response:
            calls.append(1)
            return httpx.Response(_s, json={"error": "x"})

        p = OpenAICompatibleProvider(
            "https://api.example.com/v1", "test-model", "sk-x",
            transport=httpx.MockTransport(handler))
        ok, msg = p.test_connection()
        assert ok is False
        assert expect in msg
        assert len(calls) == 1  # 收到响应不换路


def test_auto_test_connection_uses_same_path():
    """test_connection 与 chat 走同一个 _do（同一 Provider 同一路径）。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    p = OpenAICompatibleProvider(
        "https://api.example.com/v1", "test-model", "sk-x",
        transport=httpx.MockTransport(handler))
    ok, msg = p.test_connection()
    assert ok is True and "连接成功" in msg
    assert calls and calls[0].endswith("/models")


# ============================================================
# S. 流式对话（第一阶段 Streaming）
# ============================================================

def _dep(new=(), corrections=(), event=None):
    import json as _j
    return _j.dumps({"new": list(new), "corrections": list(corrections),
                     "event": event}, ensure_ascii=False)


class StreamMock(MockChatModel):
    """chat_stream 按字分片，拼起来 = chat 全文。"""

    def chat_stream(self, messages, *, images=None, timeout=None):
        full = self.chat(messages, images=images, timeout=timeout)
        for ch in full:
            yield ch


def test_stream_deltas_concatenate_to_answer(store, context_store,
                                             chat_store):
    from core.conversation import ConversationService
    model = StreamMock(text_queue=[
        "收到。",
        _dep(new=[{"kind": "fact", "content": "用户是高二学生"}]),
    ])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    import asyncio as _asyncio

    async def _collect():
        return [e async for e in conv.stream_chat("我是高二学生")]

    events = _asyncio.run(_collect())
    deltas = [e["delta"] for e in events if "delta" in e]
    done = [e for e in events if e.get("done")]
    assert len(done) == 1
    assert "".join(deltas) == done[0]["reply"]["answer"]
    assert context_store.count_active() == 1  # 流结束后沉淀照常


def test_stream_empty_answer_errors(store, context_store, chat_store):
    from core.conversation import ConversationService

    class EmptyMock(MockChatModel):
        def chat_stream(self, messages, *, images=None, timeout=None):
            return
            yield

    conv = ConversationService(store, context_store, EmptyMock(),
                               chat_store=chat_store)
    import asyncio as _asyncio2

    async def _collect2():
        return [e async for e in conv.stream_chat("hi")]

    events = _asyncio2.run(_collect2())
    assert events and "error" in events[0]
    assert context_store.count_active() == 0


def test_stream_sse_endpoint(store, context_store, chat_store):
    from core.conversation import ConversationService
    from interfaces.webapp import create_app
    from fastapi.testclient import TestClient
    model = StreamMock(text_queue=["你好呀。", _dep()])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    c = TestClient(create_app(store=store, model=model,
                              context_store=context_store,
                              chat_store=chat_store,
                              hypothesis_store=conv.hyp,
                              pattern_store=conv.pat,
                              goal_store=conv.goal))
    # 用服务内 conversation（create_app 自建）走 SSE：只断言协议形态
    r = c.post("/api/conversation/stream", json={"message": "hi"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    body = r.text
    assert "data: " in body and '"done": true' in body


def test_provider_stream_parses_sse():
    import httpx
    from models.openai_provider import OpenAICompatibleProvider
    sse = ('data: {"choices": [{"delta": {"content": "你"}}]}\n\n'
           'data: {"choices": [{"delta": {"content": "好"}}]}\n\n'
           'data: [DONE]\n\n')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    p = OpenAICompatibleProvider(
        "https://api.example.com/v1", "m", "sk-x",
        transport=httpx.MockTransport(handler))
    assert "".join(p.chat_stream(
        [{"role": "user", "content": "hi"}])) == "你好"


def test_provider_stream_auth_failure():
    import httpx
    from models.base import ModelUnavailableError
    from models.openai_provider import OpenAICompatibleProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "x"})

    p = OpenAICompatibleProvider(
        "https://api.example.com/v1", "m", "sk-x",
        transport=httpx.MockTransport(handler))
    with pytest.raises(ModelUnavailableError):
        list(p.chat_stream([{"role": "user", "content": "hi"}]))


# ============================================================
# T. async 流式 provider（第一阶段 async 化）
# ============================================================

def test_async_openai_stream_chunks():
    import asyncio
    import httpx
    from models.openai_provider import OpenAICompatibleProvider
    sse = ('data: {"choices": [{"delta": {"content": "你"}}]}\n\n'
           'data: {"choices": [{"delta": {"content": "好呀"}}]}\n\n'
           'data: [DONE]\n\n')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    async def go():
        p = OpenAICompatibleProvider(
            "https://api.example.com/v1", "m", "sk-x",
            transport=httpx.MockTransport(handler))
        out = []
        async for ch in p.chat_stream_async(
                [{"role": "user", "content": "hi"}]):
            out.append(ch)
        return "".join(out)

    assert asyncio.run(go()) == "你好呀"


def test_async_openai_stream_auth_failure():
    import asyncio
    import httpx
    from models.base import ModelUnavailableError
    from models.openai_provider import OpenAICompatibleProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "x"})

    async def go():
        p = OpenAICompatibleProvider(
            "https://api.example.com/v1", "m", "sk-x",
            transport=httpx.MockTransport(handler))
        async for _ in p.chat_stream_async(
                [{"role": "user", "content": "hi"}]):
            pass

    with pytest.raises(ModelUnavailableError):
        asyncio.run(go())


def test_async_ollama_stream_parses_ndjson():
    import asyncio
    import httpx
    from models.ollama_adapter import OllamaAdapter
    body = ('{"message": {"content": "Hi"}}\n'
            '{"message": {"content": " there"}}\n'
            '{"done": true}\n')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    async def go():
        p = OllamaAdapter("http://127.0.0.1:11434", "m",
                          transport=httpx.MockTransport(handler))
        out = []
        async for ch in p.chat_stream_async(
                [{"role": "user", "content": "hi"}]):
            out.append(ch)
        return "".join(out)

    assert asyncio.run(go()) == "Hi there"


def test_async_long_chinese_stream_100_chunks():
    """长回答中文流式：100+ chunks，拼装正确（防攒批回归）。"""
    import asyncio
    import httpx
    from models.openai_provider import OpenAICompatibleProvider
    parts = ["测"] * 120
    sse = "".join(
        'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % c
        for c in parts) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    async def go():
        p = OpenAICompatibleProvider(
            "https://api.example.com/v1", "m", "sk-x",
            transport=httpx.MockTransport(handler))
        out = []
        async for ch in p.chat_stream_async(
                [{"role": "user", "content": "hi"}]):
            out.append(ch)
        return out

    got = asyncio.run(go())
    assert len(got) == 120
    assert "".join(got) == "测" * 120


def test_async_does_not_block_event_loop():
    """async 路径不阻塞 event loop（短 sleep 交错即证明可调度）。"""
    import asyncio

    async def go():
        from models.openai_provider import OpenAICompatibleProvider
        import httpx
        ticks = []

        async def ticker():
            for _ in range(5):
                ticks.append(1)
                await asyncio.sleep(0.01)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="data: [DONE]\n\n")

        async def consume():
            p = OpenAICompatibleProvider(
                "https://api.example.com/v1", "m", "sk-x",
                transport=httpx.MockTransport(handler))
            async for _ in p.chat_stream_async(
                    [{"role": "user", "content": "hi"}]):
                pass

        await asyncio.gather(consume(), ticker())
        return len(ticks)

    assert asyncio.run(go()) == 5


# ============================================================
# U. 异步沉淀（第二阶段：Memory 后台化）
# ============================================================

def test_deposit_runs_in_background(store, context_store, chat_store,
                                    mock_model):
    """用户请求结束后，沉淀在后台跑；请求本身不等待沉淀。"""
    import asyncio
    from core.conversation import ConversationService
    model = MockChatModel(text_queue=[
        "收到。",
        _dep(new=[{"kind": "fact", "content": "用户喜欢喝茶"}]),
    ])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    import time as _time
    t0 = _time.perf_counter()
    reply = conv.chat("我喜欢喝茶")
    elapsed = _time.perf_counter() - t0
    # 请求内不等待沉淀（沉淀是后台任务）
    assert elapsed < 5.0
    # 同步上下文中 asyncio.run() 已跑完沉淀，任务集合已清理
    assert len(conv._deposit_tasks) == 0


def test_deposit_dedup_same_message(store, context_store, chat_store,
                                     mock_model):
    """同一条消息不重复沉淀（去重集合）。"""
    import asyncio
    from core.conversation import ConversationService
    model = MockChatModel(text_queue=[
        "收到。", _dep(),
        "收到。", _dep(),
    ])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    conv.chat("我喜欢喝茶")
    conv.chat("我喜欢喝茶")  # 同一条消息
    # 去重集合里只有一条（同步上下文中 asyncio.run() 已跑完）
    assert len(conv._deposit_dedup) == 1


def test_deposit_failure_does_not_fail_chat(store, context_store,
                                             chat_store, mock_model):
    """后台沉淀失败不影响已完成的聊天。"""
    import asyncio
    from core.conversation import ConversationService

    class FailDepositMock(MockChatModel):
        def chat(self, messages, *, images=None, timeout=None):
            return "正常回答。"

    model = FailDepositMock(text_queue=["正常回答。"])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    # 手动让 deposit 抛异常
    orig = conv.depositor.deposit
    def boom(*a, **k):
        raise RuntimeError("模拟沉淀失败")
    conv.depositor.deposit = boom
    reply = conv.chat("hi")
    assert reply.answer == "正常回答。"
    # 同步上下文中 asyncio.run() 已跑完沉淀（失败只记日志），任务集合已清理
    assert len(conv._deposit_tasks) == 0
    conv.depositor.deposit = orig


def test_next_turn_waits_for_dep(store, context_store, chat_store,
                                      mock_model):
    """下一轮读取 memory 前，等待后台沉淀完成（防竞态）。"""
    import asyncio
    from core.conversation import ConversationService
    model = MockChatModel(text_queue=[
        "收到。",
        _dep(new=[{"kind": "fact", "content": "用户喜欢喝咖啡"}]),
        "收到。", _dep(),
    ])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    conv.chat("我喜欢喝咖啡")
    # 模拟下一轮：读取前应等待
    async def _read():
        await conv._await_deposit()
        return conv.ctx.count_active()
    n = asyncio.run(_read())
    assert n >= 1  # 沉淀已完成


def test_shutdown_cancels_pending_tasks(store, context_store,
                                         chat_store, mock_model):
    """应用退出：取消所有后台沉淀任务，不产生孤儿。"""
    import asyncio
    from core.conversation import ConversationService
    model = MockChatModel(text_queue=[
        "收到。", _dep(),
    ])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    conv.chat("hi")
    # 同步上下文中 asyncio.run() 已跑完沉淀；验证任务集合已清理
    assert len(conv._deposit_tasks) == 0
    asyncio.run(conv.shutdown())
    assert len(conv._deposit_tasks) == 0


def test_non_streaming_chat_not_degraded(store, context_store,
                                         chat_store, mock_model):
    """非流式 chat() 行为不变（向后兼容）。"""
    from core.conversation import ConversationService
    model = MockChatModel(text_queue=[
        "你好。",
        _dep(new=[{"kind": "fact", "content": "用户是高二学生"}]),
    ])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    reply = conv.chat("我是高二学生")
    assert reply.answer == "你好。"
    assert reply.deposit is None  # 后台执行，请求内不可见
    import time as _t; _t.sleep(12)  # 等待后台沉淀完成
    assert context_store.count_active() >= 1  # 后台已写入


def test_background_deposit_does_not_block_event_loop(store, context_store,
                                                      chat_store):
    """后台沉淀里的同步模型调用不能卡死 event loop（回归钉子）。

    SSE 端点里沉淀是 loop 上的 task；一旦有人把 _deposit_bg 改回同步调用，
    那 5~9s 的模型请求就会冻住整个 loop——心跳出现巨大空洞，本测试立刻红。
    """
    import asyncio
    import time as _time
    from core.conversation import ConversationService

    class SlowStream(StreamMock):
        """沉淀那一次调用慢（0.8s），模拟真实模型请求。"""

        def chat(self, messages, *, images=None, timeout=None):
            out = super().chat(messages, images=images, timeout=timeout)
            if "记忆沉淀" in messages[0]["content"]:
                _time.sleep(0.8)
            return out

    model = SlowStream(text_queue=["收到。",
                                   _dep(new=[{"kind": "fact",
                                              "content": "用户喜欢喝咖啡"}])])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)

    async def go():
        gaps = []
        last = time_ref()

        async def ticker():
            nonlocal last
            while True:
                await asyncio.sleep(0.02)
                now = time_ref()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(ticker())
        events = [e async for e in conv.stream_chat("我喜欢喝咖啡")]
        await conv._await_deposit()      # 等沉淀跑完再统计
        beat.cancel()
        return gaps, events

    import time as _t
    globals()["time_ref"] = _t.perf_counter
    gaps, events = asyncio.run(go())
    assert any("delta" in e for e in events)
    assert gaps, "心跳没跑起来"
    # 关键：没有任何一次心跳间隔接近沉淀的 0.8s
    # 沉淀同步调用时实测空洞 ~0.98s；这里留余量但仍能抓住回归。
    assert max(gaps) < 0.6, f"event loop 被沉淀卡住：最大心跳间隔 {max(gaps):.2f}s"


def test_threadpool_chat_waits_for_pending_deposit(store, context_store,
                                                    chat_store):
    """线程池里的 chat()（FastAPI 同步端点）也能等到后台沉淀。

    回归钉子：曾经用 asyncio.run 新建 loop 去 await 主 loop 的任务，
    必然 RuntimeError → 静默跳过 → 防竞态形同虚设。
    """
    import asyncio
    import threading
    import time as _time
    from core.conversation import ConversationService

    holder = {}

    class SlowFirstDep(MockChatModel):
        """只有第一次沉淀调用慢（1.2s）——线程里那一轮的沉淀必须快，
        否则它自己的耗时会把"chat 有没有等"这件事糊掉。"""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.slow_left = 1

        def chat(self, messages, *, images=None, timeout=None):
            out = super().chat(messages, images=images, timeout=timeout)
            if ("记忆沉淀" in messages[0]["content"]
                    and self.slow_left > 0):
                self.slow_left -= 1
                _time.sleep(1.2)
            return out

    # 模拟"常驻 event loop"（真实服务就是 uvicorn 那个 loop）：
    # _LoopThread 跑一个不退出的事件循环，等我们往里投递协程。
    class _LoopThread:
        def __init__(self):
            import queue
            self.ready = queue.Queue()
            self.stop = False
            self.loop = None

        def run(self):
            async def main():
                self.loop = asyncio.get_running_loop()
                self.ready.put(True)
                while not self.stop:
                    await asyncio.sleep(0.02)
                if holder.get("conv"):
                    await asyncio.gather(
                        *list(holder["conv"]._deposit_tasks),
                        return_exceptions=True)
            asyncio.run(main())

    lt = _LoopThread()
    th_loop = threading.Thread(target=lt.run, daemon=True)
    th_loop.start()
    lt.ready.get(timeout=10)

    # 队列顺序：先给"投递出去的那次沉淀"，再给线程里那一轮的两次调用
    m = SlowFirstDep(text_queue=[
        _dep(new=[{"kind": "fact", "content": "用户喜欢喝咖啡"}]),
        "收到。", _dep(),
    ])
    conv = ConversationService(store, context_store, m,
                               chat_store=chat_store)
    holder["conv"] = conv
    asyncio.run_coroutine_threadsafe(_schedule_bg(conv), lt.loop
                                     ).result(timeout=10)

    # 换一个线程（模拟 FastAPI 线程池）调用同步 chat()
    err = []
    th = threading.Thread(target=lambda: _safe_chat(conv, err), daemon=True)
    t0 = _time.perf_counter()
    th.start()
    th.join(timeout=30)
    waited = _time.perf_counter() - t0
    # 必须在停 loop 之前断言：停了 loop 再 gather 会替我们把沉淀等完，
    # 那样就测不出"chat() 自己有没有等"。
    n_now = context_store.count_active()
    lt.stop = True
    th_loop.join(timeout=10)

    assert not err, err
    assert n_now >= 1, "线程池里的 chat 没等到后台沉淀"
    assert waited >= 1.0, f"根本没等（{waited:.2f}s）"


async def _schedule_bg(conv):
    """在常驻 loop 上调度一次后台沉淀，并确认它此刻还没写完。"""
    import asyncio
    conv._schedule_deposit(
        sid="s1", message="我喜欢喝咖啡", model_answer="收到。",
        entries=[], hyps=[], pats=[], verification_out=None)
    await asyncio.sleep(0.05)
    assert conv._loop is asyncio.get_running_loop()
    assert conv.ctx.count_active() == 0


def _safe_chat(conv, err):
    try:
        conv.chat("下一轮")
    except Exception as exc:      # pragma: no cover - 失败要显形
        err.append(exc)
