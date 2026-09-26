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
