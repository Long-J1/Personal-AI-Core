"""llama.cpp 的 HTTP 端点测试（TestClient + 假 llama-server，零真实进程）。

重点在两条"出事了就是事故"的边界：

1. `/api/llamacpp/stop` **绝不**停掉不是 App 启动的进程
2. `/api/llamacpp/start` 已经有健康服务时**不重复加载** 11GB 模型
"""
from __future__ import annotations

import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from interfaces import webapp as W
from memory.store import MemoryStore
from models.llamacpp_adapter import LlamaCppServer
from models.mock_adapter import MockChatModel
from models.settings_store import ModelSettingsStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    import core.pipeline as pm
    from core.service_state import ServiceState

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    store = MemoryStore(tmp_path / "web.db")
    model = MockChatModel()
    # 关键：默认设置存储指向**真实用户数据目录**，必须换成 tmp，
    # 否则一次 save 就覆盖用户自己的模型配置。
    monkeypatch.setattr(W, "default_store",
                        lambda: ModelSettingsStore(tmp_path / "ms.json"))
    app = W.create_app(store=store, model=model, service_state=ServiceState())
    return TestClient(app), tmp_path


# ----------------------------------------------------------------- 扫盘

def test_scan_returns_exe_and_models(client, monkeypatch):
    c, tmp = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "find_exe", lambda *a, **k: r"D:\x\llama-server.exe")
    monkeypatch.setattr(lc, "find_gguf",
                        lambda *a, **k: [{"path": "a.gguf", "name": "a.gguf",
                                          "size_gb": 11.3}])
    r = c.get("/api/llamacpp/scan")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["exe_found"] is True
    assert d["models"][0]["name"] == "a.gguf"


def test_scan_says_not_found_instead_of_guessing(client, monkeypatch):
    """扫不到必须如实说扫不到——绝不猜个路径上去。"""
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "find_exe", lambda *a, **k: "")
    monkeypatch.setattr(lc, "find_gguf", lambda *a, **k: [])
    d = c.get("/api/llamacpp/scan").json()
    assert d["exe_found"] is False
    assert d["exe"] == "" and d["models"] == []


def test_scan_failure_does_not_break_service(client, monkeypatch):
    """扫盘炸了也不能掀翻服务。"""
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "find_exe",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    r = c.get("/api/llamacpp/scan")
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ----------------------------------------------------------------- 状态

def test_status_reports_external_server_not_managed(client, monkeypatch):
    """用户自己在命令行起的：running=True 但 managed=False。"""
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "health", lambda *a, **k: {"n_ctx": 4096})
    monkeypatch.setattr(lc, "server_models", lambda *a, **k: ["m.gguf"])
    d = c.get("/api/llamacpp/status").json()
    assert d["ok"] is True
    assert d["running"] is True
    assert d["managed"] is False
    assert d["ours"] is False


def test_status_when_server_down(client, monkeypatch):
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "health", lambda *a, **k: None)
    d = c.get("/api/llamacpp/status").json()
    assert d["running"] is False


def test_status_never_explodes_on_bad_config(client, tmp_path):
    """存了半截配置的 json 也不能 500。"""
    c, _ = client
    (tmp_path / "ms.json").write_text(
        json.dumps({"provider": "llamacpp", "llamacpp": {"port": "not-a-port"}}),
        encoding="utf-8")
    r = c.get("/api/llamacpp/status")
    assert r.status_code == 200


# ----------------------------------------------------------------- 停止

def test_stop_refuses_external_server(client, monkeypatch):
    """核心安全边界：不是 App 起的进程，停它等于制造事故。"""
    c, _ = client
    called = []
    monkeypatch.setattr(LlamaCppServer, "stop",
                        lambda self: called.append(1) or {"ok": True})
    d = c.post("/api/llamacpp/stop").json()
    assert d["ok"] is True
    assert called == [], "没登记过的进程一次都不许碰"
    assert "别处起" in d["detail"]


def test_stop_kills_only_our_own(client, monkeypatch):
    """登记过的（自己起的）要真能停掉。"""
    c, tmp = client
    ModelSettingsStore(tmp / "ms.json").update({
        "provider": "llamacpp",
        "llamacpp": {"base_url": "http://127.0.0.1:8080"}})
    stopped = []

    class _Fake(LlamaCppServer):
        def __init__(self):
            super().__init__(base_url="http://127.0.0.1:8080")
            self.ours = True

        def stop(self):
            stopped.append(1)
            return {"ok": True}

    import models.llamacpp_adapter as lc
    monkeypatch.setattr(lc, "LlamaCppServer", _Fake)
    # 登记到 app.state（生产代码也只认这一个地方）
    c.app.state.llamacpp_managed["http://127.0.0.1:8080"] = _Fake()
    d = c.post("/api/llamacpp/stop").json()
    assert d["ok"] is True
    assert stopped == [1]
    assert c.app.state.llamacpp_managed == {}, "停完要从登记表里摘掉"


# ----------------------------------------------------------------- 启动

def test_start_requires_exe_path(client, monkeypatch):
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "health", lambda *a, **k: None)
    d = c.post("/api/llamacpp/start", json={}).json()
    assert d["ok"] is False
    assert "llama-server.exe" in d["error"]


def test_start_requires_model_file(client, monkeypatch, tmp_path):
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "health", lambda *a, **k: None)
    ModelSettingsStore(tmp_path / "ms.json").update({
        "provider": "llamacpp",
        "llamacpp": {"base_url": "http://127.0.0.1:8080",
                     "exe": str(tmp_path / "llama-server.exe")}})
    d = c.post("/api/llamacpp/start", json={}).json()
    assert d["ok"] is False
    assert "gguf" in d["error"]


def test_start_reuses_existing_healthy_server(client, monkeypatch, tmp_path):
    """已经有健康服务 → 复用，绝不重复加载模型。"""
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "health", lambda *a, **k: {"n_ctx": 4096})
    monkeypatch.setattr(lc, "server_models", lambda *a, **k: ["m"])
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"MZ")
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    ModelSettingsStore(tmp_path / "ms.json").update({
        "provider": "llamacpp",
        "llamacpp": {"base_url": "http://127.0.0.1:8080",
                     "exe": str(exe), "model_path": str(gguf)}})
    import subprocess
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: pytest.fail("不该起新进程"))
    d = c.post("/api/llamacpp/start", json={}).json()
    assert d["ok"] is True
    assert d["reused"] is True


def test_start_does_not_persist_unsaved_params(client, monkeypatch, tmp_path):
    """带参启动只是"试一下"，不能顺手把配置存了。"""
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "health", lambda *a, **k: {"n_ctx": 1})
    monkeypatch.setattr(lc, "server_models", lambda *a, **k: [])
    d = c.post("/api/llamacpp/start", json={"exe": str(tmp_path / "x.exe"),
                                            "model_path": str(tmp_path / "y.gguf")}).json()
    assert d["ok"] is True
    saved = ModelSettingsStore(tmp_path / "ms.json").load()
    assert saved["llamacpp"]["exe"] == "", "带参启动不该落盘"


def test_start_rejects_bad_ctx_before_touching_process(client):
    """参数非法要在起进程**之前**就拒绝。"""
    c, _ = client
    d = c.post("/api/llamacpp/start", json={"ctx": 5}).json()
    assert d["ok"] is False
    assert "上下文" in d["error"]


def test_start_failure_does_not_500(client, monkeypatch, tmp_path):
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "health", lambda *a, **k: None)
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"MZ")
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    ModelSettingsStore(tmp_path / "ms.json").update({
        "provider": "llamacpp",
        "llamacpp": {"base_url": "http://127.0.0.1:8080",
                     "exe": str(exe), "model_path": str(gguf)}})
    monkeypatch.setattr(LlamaCppServer, "start",
                        lambda self, **k: {"ok": False,
                                           "error": "cudaMalloc failed"})
    r = c.post("/api/llamacpp/start", json={})
    assert r.status_code == 200
    assert r.json()["error"] == "cudaMalloc failed"


# ----------------------------------------------------------- 模型设置联通

def test_settings_accept_llamacpp_via_put(client):
    """UI 存设置要走得通。"""
    c, tmp = client
    r = c.put("/api/model/settings", json={
        "provider": "llamacpp",
        "llamacpp": {"base_url": "http://127.0.0.1:9090", "ctx": 8192}})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["provider"] == "llamacpp"
    assert d["llamacpp"]["ctx"] == 8192
    assert ModelSettingsStore(tmp / "ms.json").load()["llamacpp"]["ctx"] == 8192


def test_settings_reject_empty_base_url_via_put(client):
    c, _ = client
    r = c.put("/api/model/settings", json={
        "provider": "llamacpp", "llamacpp": {"base_url": ""}})
    assert r.status_code == 400
    assert "llama.cpp" in r.json()["error"]


def test_settings_get_never_leaks_api_key(client):
    c, _ = client
    c.put("/api/model/settings", json={
        "provider": "llamacpp",
        "llamacpp": {"base_url": "http://127.0.0.1:8080",
                     "api_key": "sk-super-secret-abcdef"}})
    r = c.get("/api/model/settings")
    assert "sk-super-secret-abcdef" not in r.text
    assert r.json()["llamacpp"]["api_key_set"] is True


def test_model_test_uses_llamacpp_adapter(client, monkeypatch):
    """连接测试要真的按 llamacpp 装配，而不是掉回 Ollama。"""
    c, _ = client
    from models import llamacpp_adapter as lc
    monkeypatch.setattr(lc, "health", lambda *a, **k: {"n_ctx": 1})
    seen = []

    def _post(payload, timeout=None):
        seen.append(payload)
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "好",
                                         "reasoning_content": ""},
                             "finish_reason": "stop"}]}
    monkeypatch.setattr(lc.LlamaCppAdapter, "_post",
                        lambda self, p, t=None: _post(p, t))
    d = c.post("/api/model/test", json={
        "provider": "llamacpp",
        "llamacpp": {"base_url": "http://127.0.0.1:8080"}}).json()
    assert d["provider"] == "llamacpp"
    assert d["ok"] is True


def test_status_endpoint_still_works_with_llamacpp_provider(client):
    """换成本地 provider 后，首页那个模型徽章不能 500。"""
    c, _ = client
    c.put("/api/model/settings", json={
        "provider": "llamacpp",
        "llamacpp": {"base_url": "http://127.0.0.1:8080"}})
    r = c.get("/api/status")
    assert r.status_code == 200