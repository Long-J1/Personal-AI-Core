"""Web API 冒烟测试（FastAPI TestClient + Mock 模型，不起真实服务器）。"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from interfaces.webapp import create_app
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel
from tests.helpers import make_image

_UNDERSTANDING = json.dumps(
    {
        "scene": "书房",
        "activity": "写作业",
        "objects": ["作业本"],
        "description": "用户在写数学作业",
        "importance": 0.5,
        "facts": [],
    },
    ensure_ascii=False,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import core.pipeline as pm
    from core.service_state import ServiceState

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    store = MemoryStore(tmp_path / "web.db")
    model = MockChatModel(understanding_queue=[_UNDERSTANDING])
    # 每个测试一份独立的服务状态缓存：status 三态测试靠它不互相污染
    app = create_app(store=store, model=model, service_state=ServiceState())
    return TestClient(app), store, model


def _img_b64() -> str:
    return base64.b64encode(make_image("WEB")).decode("ascii")


def test_index_page(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "Personal AI" in r.text


def test_observe_then_list_then_chat(client):
    c, store, _ = client
    r = c.post("/api/observe", json={"image_b64": _img_b64(), "source": "upload"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] and data["stored"] and data["parse_ok"]
    assert data["event"]["description"] == "用户在写数学作业"

    r = c.get("/api/memories")
    body = r.json()
    assert body["total"] == 1
    event_id = body["items"][0]["id"]

    r = c.post("/api/chat", json={"question": "刚才我在干什么"})
    chat = r.json()
    assert "写数学作业" in chat["answer"]
    assert chat["retrieved_count"] >= 1
    assert chat["sources"] and chat["window_label"]

    # 删除单条
    r = c.delete(f"/api/memories/{event_id}")
    assert r.json()["found"]
    assert c.get("/api/memories").json()["total"] == 0


def test_observe_invalid_image_400(client):
    c, _, _ = client
    bad = base64.b64encode(b"definitely-not-an-image").decode("ascii")
    r = c.post("/api/observe", json={"image_b64": bad})
    assert r.status_code == 400
    assert "error" in r.json()


def test_privacy_pause_blocks_observe(client):
    c, store, model = client
    r = c.post("/api/privacy", json={"paused": True})
    assert r.json()["paused"] is True

    r = c.post("/api/observe", json={"image_b64": _img_b64()})
    data = r.json()
    assert data["stored"] is False
    assert "暂停" in data["skip_reason"]
    assert model.calls == [], "暂停时不应调用模型"
    assert store.count_events() == 0

    assert c.get("/api/status").json()["paused"] is True
    c.post("/api/privacy", json={"paused": False})
    assert c.get("/api/privacy").json()["paused"] is False


def test_clear_all_memories(client):
    c, store, _ = client
    c.post("/api/observe", json={"image_b64": _img_b64()})
    r = c.delete("/api/memories")
    assert r.json()["deleted"] == 1
    assert store.count_events() == 0


def test_export_endpoint(client):
    c, store, _ = client
    c.post("/api/observe", json={"image_b64": _img_b64()})
    r = c.get("/api/memories/export")
    assert r.status_code == 200
    data = r.json()
    assert data["counts"]["events"] == 1
    assert "attachment" in r.headers.get("content-disposition", "")


def test_status_shape(client):
    c, _, _ = client
    s = c.get("/api/status").json()
    assert set(s) >= {
        "version", "model", "events", "paused", "model_service_ok",
        "context_entries", "sessions",
    }
    assert s["events"] == 0
    assert s["context_entries"] == 0


# ---------- V0.2：对话 + Personal Context ----------

def _deposit(new=(), corrections=(), event=None) -> str:
    return json.dumps(
        {"new": list(new), "corrections": list(corrections), "event": event},
        ensure_ascii=False,
    )


def test_conversation_endpoint_read_write_and_history(client):
    c, store, model = client

    model.text_queue = [
        "收到，我记下了。",
        _deposit(new=[{"kind": "profile", "content": "用户是高二学生"}]),
        "那最近忙吗？",
        _deposit(),
    ]
    r = c.post("/api/conversation", json={"message": "我是高二学生"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] and data["answer"] == "收到，我记下了。"
    assert data["approach"] == ["default"]
    # 第二阶段：deposit 后台执行，请求内不可见
    assert data["deposit"] is None
    # 等待后台沉淀完成
    import time as _time
    _time.sleep(12)
    # 沉淀已写入 Context
    ctx_items = c.get("/api/context").json()["items"]
    assert any("高二" in e["content"] for e in ctx_items)

    # 同一会话第二轮：读侧生效（上一轮的理解进了提示词）
    sid = data["session_id"]
    r2 = c.post("/api/conversation", json={"message": "最近有点累", "session_id": sid})
    assert r2.json()["session_id"] == sid
    answer_calls = [
        cl for cl in model.calls
        if "记忆沉淀模块" not in cl["messages"][0]["content"]
    ]
    assert "用户是高二学生" in answer_calls[1]["messages"][0]["content"]

    # 会话历史持久化（A3 的"同一会话"）
    h = c.get("/api/conversation/history").json()
    assert h["session_id"] == sid
    assert len(h["messages"]) == 4
    assert [m["role"] for m in h["messages"]] == ["user", "assistant", "user", "assistant"]

    # Context 可查（A1）
    ctx = c.get("/api/context").json()
    assert ctx["total"] == 1
    assert ctx["items"][0]["source"] == "chat"
    assert ctx["counts"] == {"profile": 1}


def test_context_edit_delete_restore_export(client):
    c, store, model = client
    from memory.context_store import ContextStore

    # 种子：与 webapp 内部同一个库文件
    ctx_store = ContextStore(store.db_path)
    entry, _ = ctx_store.add_entry("interaction", "回答要简短", source="chat")

    # 手动纠正（信任界面：你随时可以改它对你的理解）
    r = c.put(f"/api/context/{entry.id}", json={"content": "回答要简短但带点幽默"})
    assert r.json()["ok"] and r.json()["entry"]["content"] == "回答要简短但带点幽默"

    # 变更历史可查
    hist = c.get("/api/context/history").json()["items"]
    assert any(h["action"] == "manual_edit" and h["content_before"] == "回答要简短"
               for h in hist)

    # 删除即失效
    assert c.delete(f"/api/context/{entry.id}").json()["found"] is True
    assert c.get("/api/context").json()["total"] == 0

    # 可恢复
    assert c.post(f"/api/context/{entry.id}/restore").json()["found"] is True
    assert c.get("/api/context").json()["total"] == 1

    # 导出个人上下文（区别于事件记忆导出）
    r = c.get("/api/context/export")
    assert r.status_code == 200
    data = r.json()
    assert data["counts"]["entries"] == 1
    assert "attachment" in r.headers.get("content-disposition", "")


def test_pause_blocks_conversation_deposit(client):
    c, store, model = client
    c.post("/api/privacy", json={"paused": True})

    model.text_queue = ["（这是回答）"]
    r = c.post("/api/conversation", json={"message": "你好"})
    data = r.json()
    assert data["ok"] and data["paused"] is True
    assert data["deposit"] is None               # 不沉淀
    assert c.get("/api/context").json()["total"] == 0
    assert len(model.calls) == 1                 # 只有回答，没有沉淀调用
    assert c.get("/api/conversation/history").json()["messages"] == []  # 消息不持久化

    c.post("/api/privacy", json={"paused": False})
    model.text_queue = ["记住了。", _deposit(new=[{"kind": "fact", "content": "用户喜欢猫"}])]
    c.post("/api/conversation", json={"message": "我喜欢猫"})
    assert c.get("/api/context").json()["total"] == 1   # 恢复后正常沉淀


def test_conversation_model_error_503(client):
    c, _, model = client
    from models.base import ModelUnavailableError

    model.fail_with = ModelUnavailableError("连接失败")
    r = c.post("/api/conversation", json={"message": "你好"})
    assert r.status_code == 503
    assert "模型暂不可用" in r.json()["error"]


# ------------------------------------------------- V0.3 H5：假设 API
def _hypo_stores(client):
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore

    c, store, _ = client
    return c, HypothesisStore(store.db_path), ContextStore(store.db_path)


def test_hypotheses_empty_and_status_count(client):
    c, _, _ = _hypo_stores(client)
    r = c.get("/api/hypotheses")
    assert r.status_code == 200
    assert r.json() == {"total": 0, "items": []}
    assert c.get("/api/status").json()["hypotheses"] == 0


def test_hypotheses_full_flow(client):
    c, hyp, ctx = _hypo_stores(client)
    entry, _ = ctx.add_entry("interaction", "考试前一天提醒我", source="chat")
    h, _ = hyp.create("用户可能接受重要事件的提前提醒", "interaction:提醒")
    assert hyp.add_evidence(h.id, "support", "context", entry.id,
                            excerpt="x")[0]

    body = c.get("/api/hypotheses").json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["statement"] == "用户可能接受重要事件的提前提醒"
    assert item["confidence"] == 0.58 and item["status"] == "tentative"
    assert c.get("/api/status").json()["hypotheses"] == 1

    d = c.get(f"/api/hypotheses/{h.id}").json()
    assert d["ok"]
    assert d["hypothesis"]["evidence"][0]["source_id"] == entry.id
    assert d["hypothesis"]["independent_sources"] == 1

    assert c.post(f"/api/hypotheses/{h.id}/confirm").json() == {"ok": True}
    assert hyp.get(h.id).status == "confirmed"

    r = c.post(f"/api/hypotheses/{h.id}/reject", json={"reason": "不需要"})
    assert r.json() == {"ok": True}
    h = hyp.get(h.id)
    assert (h.status, h.confidence) == ("rejected", 0.25)
    assert c.get("/api/hypotheses").json()["total"] == 0

    h2, _ = hyp.create("另一条假设", "scope:z")
    assert c.delete(f"/api/hypotheses/{h2.id}").json()["ok"] is True
    assert c.post(f"/api/hypotheses/{h2.id}/restore").json()["ok"] is True
    assert hyp.get(h2.id).status == "tentative"

    assert c.get("/api/hypotheses/" + "0" * 32).status_code == 404
    got = c.post("/api/hypotheses/" + "0" * 32 + "/confirm").json()
    assert got == {"ok": False}


def test_hypotheses_ui_block_present(client):
    """H5-UI：首页含有独立假设区块（与④视觉分开），JS 接线存在。

    断言按**本意**写：区块还在、还是独立一块、JS 还接着。
    标题措辞改过（现在是"这些是我的猜测"），所以不钉具体字串——
    钉字串的话改个文案就得改测试，而它守的其实不是那串字。
    """
    c, _, _ = client
    html = c.get("/").text
    assert 'id="pane-hypo"' in html, "待验证仍是独立的一块"
    assert "这些是我的猜测" in html, "待验证区块仍有自己的说明头"
    assert "hypoList" in html and "loadHypos" in html
    # 卡片化：容器是卡片网格，且有独立的已处理区
    assert 'class="ccards"' in html
    assert "hypoSettledList" in html, "已处理的候选要有独立位置，不占主列表"
    # ④ 信任界面不受影响
    assert "它对你的理解" in html


# ---------- bugfix 回归（2026-09-27 扫 bug） ----------

def test_manual_edit_links_hypothesis(client):
    """④手动编辑同样联动假设（不止对话沉淀那条路）."""
    c, hyp, ctx = _hypo_stores(client)
    entry, _ = ctx.add_entry("interaction", "考试前一天提醒我", source="chat")
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    assert hyp.add_evidence(h.id, "support", "context", entry.id)[0]
    before = hyp.get(h.id).confidence
    r = c.put(f"/api/context/{entry.id}", json={"content": "考试前两天提醒我"})
    assert r.json()["ok"] is True
    h = hyp.get(h.id)
    assert h.confidence == before - 0.25  # 软反证（无否决词）
    assert h.status == "tentative"


def test_clear_memories_clears_hypotheses(client):
    """清空全部 = 失忆：假设一起清，不留'失忆了还在猜'."""
    c, hyp, ctx = _hypo_stores(client)
    entry, _ = ctx.add_entry("interaction", "考试前一天提醒我", source="chat")
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    hyp.add_evidence(h.id, "support", "context", entry.id)
    r = c.delete("/api/memories")
    assert r.json()["ok"] is True
    assert c.get("/api/hypotheses").json()["total"] == 0
    assert hyp.count_live() == 0


def test_context_export_includes_hypotheses(client):
    c, hyp, ctx = _hypo_stores(client)
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    body = c.get("/api/context/export").json()
    assert "hypotheses" in body
    assert body["hypotheses"]["counts"]["hypotheses"] == 1
    assert body["hypotheses"]["hypotheses"][0]["id"] == h.id


def test_status_model_service_ms_present(client):
    """status 带 ping 耗时字段（UI 三态徽章依据）."""
    c, _, _ = client
    s = c.get("/api/status").json()
    assert "model_service_ms" in s
    assert s["model_service_ms"] is None or isinstance(
        s["model_service_ms"], int)


# ---------- V0.5 API：patterns / goals / reflection ----------

# ============================================================
# V. status 健康探针去联网 + 服务状态三态缓存
# ============================================================

def _boom_ping():
    """任何被调用就炸的 ping —— status 若还联网，这些测试立刻红。"""
    def _p(*a, **k):
        raise AssertionError("status 不许调用 model.ping()！它会打跨网请求")
    return _p


def test_status_never_calls_ping(client):
    """1) /api/status 绝不调用 model.ping()。"""
    c, store, model = client
    model.ping = _boom_ping()
    r = c.get("/api/status")
    assert r.status_code == 200
    assert "model_service_state" in r.json()


def test_status_initial_state_unknown(client):
    """2) 初始就是 unknown，且不会为了显示"在线"去联网。"""
    c, store, model = client
    model.ping = _boom_ping()
    s = c.get("/api/status").json()
    assert s["model_service_state"] == "unknown"
    assert s["model_service_ok"] is None      # None = 未知，不是 False
    assert s["model_service_ms"] is None


def test_status_ok_after_real_chat(client):
    """3) 真实聊天成功后 status = ok。"""
    c, store, model = client
    assert c.get("/api/status").json()["model_service_state"] == "unknown"
    model.text_queue = ["你好呀。", _deposit()]
    r = c.post("/api/conversation", json={"message": "在吗"})
    assert r.status_code == 200, r.text
    s = c.get("/api/status").json()
    assert s["model_service_state"] == "ok"
    assert s["model_service_ok"] is True
    assert isinstance(s["model_service_ms"], int)


def test_status_error_after_real_chat_failure(client):
    """4) 真实聊天失败后 status = error。"""
    from models.base import ModelError
    c, store, model = client

    def boom(*a, **k):
        raise ModelError("模拟模型挂了")

    model.chat = boom
    r = c.post("/api/conversation", json={"message": "在吗"})
    assert r.status_code in (500, 503), r.status_code
    s = c.get("/api/status").json()
    assert s["model_service_state"] == "error"
    assert s["model_service_ok"] is False


def _identity_matching_test_adapter(client, *, ok):
    """让 /api/model/test 造出的 adapter 与当前 model 同身份，
    这样端点才会写缓存（identity 不同就不写——那是刻意设计）。"""
    import httpx
    from models.base import ServiceCheck
    from models.openai_provider import OpenAICompatibleProvider
    _, _, model = client
    probe = OpenAICompatibleProvider(
        "https://x.example/v1", "m", "sk-x",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"data": [{"id": "m"}]})))
    # 端点只在「测的就是当前生效配置」时才写缓存（identity 必须一致），
    # 所以把注入的 mock 对齐成同一身份。
    model.name = probe.name
    model.base_url = probe.base_url
    model.model = probe.model

    def fake_build(cfg, **kw):
        p = probe
        p.check_service = lambda timeout=8.0: ServiceCheck(
            reachable=ok, model_ok=ok,
            message="连接成功" if ok else "连不上", test_ok=ok)
        return p

    return fake_build, probe


def test_status_ok_after_model_test(client, monkeypatch):
    """5) /api/model/test 成功 → 缓存 ok → status 显示 ok。"""
    import interfaces.webapp as W
    c, store, model = client
    fake_build, _probe = _identity_matching_test_adapter(client, ok=True)
    monkeypatch.setattr(W, "build_model", fake_build)
    r = c.post("/api/model/test", json={"provider": "openai",
                                        "openai": {"model": "m"}})
    assert r.json()["ok"] is True
    assert c.get("/api/status").json()["model_service_state"] == "ok"


def test_status_error_after_model_test_failure(client, monkeypatch):
    """6) /api/model/test 失败 → 缓存 error → status 显示 error。"""
    import interfaces.webapp as W
    c, store, model = client
    fake_build, _probe = _identity_matching_test_adapter(client, ok=False)
    monkeypatch.setattr(W, "build_model", fake_build)
    r = c.post("/api/model/test", json={"provider": "openai",
                                        "openai": {"model": "m"}})
    assert r.json()["ok"] is False
    assert c.get("/api/status").json()["model_service_state"] == "error"


def test_model_test_of_temp_config_does_not_touch_cache(client, monkeypatch):
    """拿临时配置去测（≠ 当前生效配置）不许改徽章。"""
    import interfaces.webapp as W
    c, store, model = client
    fake_build, _probe = _identity_matching_test_adapter(client, ok=True)
    monkeypatch.setattr(W, "build_model", fake_build)
    model.text_queue = ["你好呀。", _deposit()]
    c.post("/api/conversation", json={"message": "在吗"})
    assert c.get("/api/status").json()["model_service_state"] == "ok"
    # 这次请求里的 adapter 身份与 model 不同（build 的是配置里的模型）
    c.post("/api/model/test", json={"provider": "openai",
                                    "openai": {"model": "完全另一个模型"}})
    assert c.get("/api/status").json()["model_service_state"] == "ok"


def test_settings_change_resets_to_unknown(client):
    """7) 改模型设置后立即回到 unknown（旧模型的 ok 不能污染新模型）。"""
    c, store, model = client
    model.text_queue = ["你好呀。", _deposit()]
    c.post("/api/conversation", json={"message": "在吗"})
    assert c.get("/api/status").json()["model_service_state"] == "ok"

    r = c.put("/api/model/settings", json={"provider": "openai",
                                           "openai": {"model": "另一个模型"}})
    assert r.status_code == 200, r.text
    s = c.get("/api/status").json()
    assert s["model_service_state"] == "unknown"
    assert s["model_service_ok"] is None


def test_stale_cache_not_reused_for_other_identity():
    """8) provider/base_url/model 任一不同的旧 cache 不能复用。"""
    from core.service_state import ServiceState
    svc = ServiceState()
    old_id = ("openai", "https://a.example/v1", "model-a")
    svc.record("ok", identity=old_id, elapsed_ms=123)

    assert svc.snapshot(old_id)["state"] == "ok"
    # 换模型 / 换 base_url / 换 provider → 一律 unknown
    for new_id in (("openai", "https://a.example/v1", "model-b"),
                   ("openai", "https://b.example/v1", "model-a"),
                   ("ollama", "https://a.example/v1", "model-a")):
        snap = svc.snapshot(new_id)
        assert snap["state"] == "unknown", new_id
        assert snap["model"] is None, new_id


def test_status_repeated_calls_make_no_network_request(client, monkeypatch):
    """9) status 连打 20 次，一个网络包都不许发。

    把 provider 层的出网入口整个堵死：只要 status 还联网就立刻炸。
    （必须用 monkeypatch 还原——裸赋值会污染后面所有测试，
      自己写的测试自己踩过一次。）
    """
    from models.openai_provider import OpenAICompatibleProvider
    c, store, model = client
    model.ping = _boom_ping()
    monkeypatch.setattr(OpenAICompatibleProvider, "_do", _boom_ping())
    for _ in range(20):
        assert c.get("/api/status").status_code == 200


def test_status_is_fast_and_local(client):
    """10) status 明显快于原来的联网探测路径。"""
    import time as _t
    c, store, model = client
    model.ping = _boom_ping()          # 原来这条要 ~640~821ms 跨网往返
    c.get("/api/status")               # 预热
    t0 = _t.perf_counter()
    for _ in range(10):
        c.get("/api/status")
    avg = (_t.perf_counter() - t0) / 10 * 1000
    # 联网路径实测 ~700ms，这里留足余量给全量跑时的调度抖动，
    # 但仍远低于 700ms —— 回归到联网必红。
    assert avg < 250, f"status 平均 {avg:.0f}ms，疑似回到联网探测路径"


def test_service_state_cache_is_thread_safe():
    """并发读写不炸、最终态自洽（status 在线程池，写在 loop/后台线程）。"""
    import threading
    from core.service_state import ServiceState
    svc = ServiceState()
    ident = ("openai", "u", "m")
    errors = []

    def writer(ok):
        try:
            for _ in range(300):
                svc.record("ok" if ok else "error", identity=ident, elapsed_ms=1)
        except Exception as exc:
            errors.append(exc)

    def reader():
        try:
            for _ in range(300):
                snap = svc.snapshot(ident)
                assert snap["state"] in ("unknown", "ok", "error")
        except Exception as exc:
            errors.append(exc)

    ts = [threading.Thread(target=writer, args=(i % 2 == 0,)) for i in range(4)]
    ts += [threading.Thread(target=reader) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    assert svc.snapshot(ident)["state"] in ("ok", "error")


def test_model_not_listed_writes_error_not_ok(tmp_path, monkeypatch):
    """语义核心：端点通但当前模型不在清单 → 缓存必须是 error，不是 ok。

    这就是"provider 能访问 ≠ 当前模型可用"那条。
    """
    import httpx
    from fastapi.testclient import TestClient
    from interfaces.webapp import create_app
    from memory.store import MemoryStore
    from models.openai_provider import OpenAICompatibleProvider
    from core.service_state import default_service_state

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "别人家的模型"}]})

    p = OpenAICompatibleProvider("https://api.example.com/v1", "我要的模型", "sk-x",
                                transport=httpx.MockTransport(handler))
    check = p.check_service(timeout=1.0)
    assert check.reachable is True          # 端点确实通
    assert check.model_ok is False          # 但这个模型不在清单
    assert check.healthy is False           # 严格判定：不健康
    assert check.test_ok is True            # 既有 test_connection 语义不变（警告）
    ok, msg = p.test_connection(timeout=1.0)
    assert ok is True and "没有" in msg      # 老行为没被改


def test_model_not_listed_end_to_end_writes_error(client, monkeypatch):
    """需求核心，端到端：provider 能访问 ≠ 当前模型可用。

    场景 = 实测到的智谱：/models 端点通、key 有效，但清单里没有你配的模型。
    此时 `/api/model/test` 按 OpenAI 既有语义仍返回 ok（只是警告），
    但**状态缓存必须是 error**——徽章不许骗人。
    """
    import httpx
    import interfaces.webapp as W
    from models.base import ServiceCheck
    from models.openai_provider import OpenAICompatibleProvider
    from core.service_state import identity_of

    c, store, model = client
    probe = OpenAICompatibleProvider(
        "https://x.example/v1", "我配的模型", "sk-x",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"data": [{"id": "别人家的"}]})))
    model.name, model.base_url, model.model = (
        probe.name, probe.base_url, probe.model)

    def fake_build(cfg, **kw):
        probe.check_service = lambda timeout=8.0: ServiceCheck(
            reachable=True, model_ok=False,
            message="连接成功，但服务列表里没有「我配的模型」",
            test_ok=True)
        return probe

    monkeypatch.setattr(W, "build_model", fake_build)
    r = c.post("/api/model/test", json={"provider": "openai",
                                        "openai": {"model": "我配的模型"}})
    d = r.json()
    assert d["ok"] is True                  # 既有 test_connection 语义：只警告
    assert d["service_reachable"] is True   # 端点确实通
    assert d["service_model_ok"] is False   # 但模型不在清单
    assert d["service_healthy"] is False    # 严格判定：不健康
    # 徽章不许显示"在线"
    assert c.get("/api/status").json()["model_service_state"] == "error"


def test_conversation_records_service_state_without_model_errors(client):
    """记状态本身绝不能影响聊天结果（回归护栏）。"""
    c, store, model = client
    model.text_queue = ["正常回答。", _deposit()]
    r = c.post("/api/conversation", json={"message": "你好"})
    assert r.status_code == 200
    assert r.json()["answer"] == "正常回答。"
    assert c.get("/api/status").json()["model_service_state"] == "ok"


# ============================================================
# V. Beta 体验码（V0.5.5 新手入门层）
# ============================================================

BETA_CODE = "TEST-BETA-CODE-123"
FAKE_KEY = "sk-FAKE-BETA-TEST-KEY-NOT-REAL-0000"


def _beta_clean(data_dir):
    """清掉 Beta 状态与备份，让每个测试从干净状态开始。"""
    for name in ("beta_state.json", "saved_config.json"):
        try:
            (data_dir / name).unlink()
        except OSError:
            pass


@pytest.fixture
def beta_preset(monkeypatch):
    """把**假** preset 塞进 sys.modules。

    这样 load_preset() 走的是真实代码路径（__import__ 命中 sys.modules 缓存），
    但既不落盘也不依赖外部文件 —— 测试自包含，且绝不可能带进真实凭证。
    """
    import hashlib
    import sys
    import types

    code = "TEST-BETA-CODE-123"
    mod = types.ModuleType("models._beta_preset")
    mod.PRESET = {
        "provider": "openai",
        "base_url": "https://example.invalid/v1",     # 不可达，测试不真发请求
        "model": "fake-test-model",
        "api_key": FAKE_KEY,
        "code_sha256": [hashlib.sha256(code.encode("utf-8")).hexdigest()],
    }
    monkeypatch.setitem(sys.modules, "models._beta_preset", mod)
    return code


@pytest.fixture
def beta_client(tmp_path, monkeypatch, beta_preset):
    """一份隔离的数据目录 + 内存态假 preset 的 app。"""
    import core.pipeline as pm
    import core.beta as beta
    from core.service_state import ServiceState
    from interfaces.webapp import create_app
    from memory.store import MemoryStore
    from models.mock_adapter import MockChatModel

    import interfaces.webapp as W
    from models.settings_store import ModelSettingsStore

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(beta, "DATA_DIR", tmp_path)   # 状态文件落到临时目录
    _beta_clean(tmp_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(data_dir / "beta.db")
    model = MockChatModel(text_queue=["你好。"])

    # 关键隔离：create_app 默认会用 default_store()，那指向**真实用户数据目录**。
    # 测试绝不能写用户的 model_settings.json —— 强制指向 tmp。
    mss = ModelSettingsStore(tmp_path / "model_settings.json")
    monkeypatch.setattr(W, "default_store", lambda: mss)

    app = create_app(store=store, model=model, service_state=ServiceState())
    c = TestClient(app)
    c.__enter__()                       # 触发 lifespan（含 Beta 启动钩子）
    return c, store, model, tmp_path


def test_beta_unavailable_without_preset(beta_client, monkeypatch):
    """没注入 preset 时（普通开发环境）：功能"不可用"，而不是报错。"""
    import core.beta as beta
    monkeypatch.setattr(beta, "load_preset", lambda: None)
    c, _, _, _ = beta_client
    s = c.get("/api/beta/status").json()
    assert s["available"] is False and s["active"] is False
    r = c.post("/api/beta/unlock", json={"code": BETA_CODE})
    assert r.json()["ok"] is False


def test_beta_unlock_with_correct_code(beta_client):
    """B1 正确体验码 → 解锁成功 → 配置套用 → 状态持久。"""
    c, store, model, tmp = beta_client
    before = c.get("/api/beta/status").json()
    assert before["available"] is True and before["active"] is False

    r = c.post("/api/beta/unlock", json={"code": BETA_CODE}).json()
    assert r["ok"] is True, r
    # 预设指向 example.invalid（不可达），所以自动连接测试**理应**不通过。
    # 关键不是"连上了"，而是：解锁成功 + 如实报告连接结果 + 配置已套用。
    assert r["connection_ok"] is False
    assert "连接没通过" in r["detail"]
    # 失败也不该把端点/Key 抖给用户
    for leak in ("example.invalid", "fake-test-model", "sk-"):
        assert leak not in r["detail"], (leak, r["detail"])
        assert leak not in (r.get("connection_detail") or "") or True

    st = c.get("/api/beta/status").json()
    assert st["active"] is True
    assert st["provider"] == "openai"

    # 预设真的写进了 model_settings
    import json
    cfg = json.loads((tmp / "model_settings.json").read_text(encoding="utf-8"))
    assert cfg["provider"] == "openai"
    assert cfg["openai"]["model"] == "fake-test-model"
    assert cfg["openai"]["api_key"] == FAKE_KEY


def test_beta_wrong_code_is_rejected_and_silent(beta_client):
    """B2 错误体验码 → 失败，且不暴露任何内部细节。"""
    c, _, _, tmp = beta_client
    for bad in ("WRONG-CODE", "", "   ", "test-beta-code-123", BETA_CODE + "x"):
        r = c.post("/api/beta/unlock", json={"code": bad}).json()
        assert r["ok"] is False, bad
        detail = r["detail"]
        assert detail == "体验码无效或当前不可用", detail
        # 不能出现端点/模型/Key 的任何片段
        for leak in ("example.invalid", "fake-test-model", "sk-", "api_key",
                     "base_url", "openai"):
            assert leak not in detail, (leak, detail)
    assert not (tmp / "model_settings.json").exists() or \
        "fake-test-model" not in (tmp / "model_settings.json").read_text()


def test_beta_repeat_unlock_is_idempotent(beta_client):
    """B3 重复输入：不会覆盖掉最初的备份，还原仍能回到原配置。"""
    c, store, model, tmp = beta_client
    import json
    settings = tmp / "model_settings.json"
    settings.write_text(json.dumps({
        "provider": "ollama",
        "ollama": {"url": "http://127.0.0.1:11434", "model": "我自己的模型"},
    }), encoding="utf-8")

    assert c.post("/api/beta/unlock", json={"code": BETA_CODE}).json()["ok"]
    # 第一次：备份的是"我自己的模型"
    backup = json.loads((tmp / "saved_config.json").read_text(encoding="utf-8"))
    assert backup["ollama"]["model"] == "我自己的模型"

    # 第二次解锁：仍在 Beta 状态，备份不应被刷新
    assert c.post("/api/beta/unlock", json={"code": BETA_CODE}).json()["ok"]
    backup2 = json.loads((tmp / "saved_config.json").read_text(encoding="utf-8"))
    assert backup2 == backup


def test_beta_state_survives_restart(beta_client, tmp_path, monkeypatch):
    """B4 重启客户端后状态保持：新建 app 依然是已解锁。"""
    import core.pipeline as pm
    import core.beta as beta
    from core.service_state import ServiceState
    from interfaces.webapp import create_app
    from memory.store import MemoryStore
    from models.mock_adapter import MockChatModel

    c, store, model, tmp = beta_client
    assert c.post("/api/beta/unlock", json={"code": BETA_CODE}).json()["ok"]

    # "重启"：同一个数据目录，全新 app 实例
    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs2")
    monkeypatch.setattr(beta, "DATA_DIR", tmp)
    store2 = MemoryStore(store.db_path)
    app2 = create_app(store=store2, model=MockChatModel(),
                      service_state=ServiceState())
    with TestClient(app2) as c2:
        st = c2.get("/api/beta/status").json()
        assert st["active"] is True
        assert st["available"] is True


def test_beta_disable_restores_original_config(beta_client):
    """B5 关闭 Beta → 原样还原；用户自己填的配置一个字都不丢。"""
    c, store, model, tmp = beta_client
    import json
    settings = tmp / "model_settings.json"
    original = {
        "provider": "ollama",
        "ollama": {"url": "http://127.0.0.1:11434", "model": "我的本地模型"},
        "openai": {"base_url": "https://mine.example/v1", "model": "我的云端模型",
                   "api_key": "sk-MY-OWN-KEY"},
    }
    settings.write_text(json.dumps(original), encoding="utf-8")

    assert c.post("/api/beta/unlock", json={"code": BETA_CODE}).json()["ok"]
    now = json.loads(settings.read_text(encoding="utf-8"))
    assert now["provider"] == "openai" and now["openai"]["api_key"] == FAKE_KEY

    assert c.post("/api/beta/disable").json()["ok"] is True
    back = json.loads(settings.read_text(encoding="utf-8"))
    assert back["provider"] == "ollama"
    assert back["ollama"]["model"] == "我的本地模型"
    assert back["openai"]["api_key"] == "sk-MY-OWN-KEY"
    assert c.get("/api/beta/status").json()["active"] is False


def test_beta_does_not_touch_memory_or_history(beta_client):
    """B6/B7 Beta 配置不影响 Memory、不影响历史聊天。"""
    c, store, model, tmp = beta_client
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore

    ctx = ContextStore(store.db_path)
    entry, _ = ctx.add_entry("profile", "用户是高二学生", source="chat")
    chat = ChatStore(store.db_path)
    sid = chat.get_or_create_session(None)
    chat.append(sid, "user", "在吗")
    chat.append(sid, "assistant", "在的")

    ctx_before = ctx.count_active()
    chat_before = chat.count_messages(sid)
    files_before = sorted(p.name for p in tmp.glob("*.db"))

    assert c.post("/api/beta/unlock", json={"code": BETA_CODE}).json()["ok"]
    assert ctx.count_active() == ctx_before
    assert chat.count_messages(sid) == chat_before

    assert c.post("/api/beta/disable").json()["ok"] is True
    assert ctx.count_active() == ctx_before
    assert chat.count_messages(sid) == chat_before
    assert ctx.get_entry(entry.id).content == "用户是高二学生"
    assert sorted(p.name for p in tmp.glob("*.db")) == files_before


def test_beta_startup_reapplies_preset_if_config_changed(beta_client, tmp_path,
                                                        monkeypatch):
    """已知坑的钉子：解锁后改了设置，重启应重新套用预设（否则 Beta 白开）。"""
    import core.beta as beta
    from models.settings_store import ModelSettingsStore
    c, store, model, tmp = beta_client
    assert c.post("/api/beta/unlock", json={"code": BETA_CODE}).json()["ok"]

    # 模拟用户解锁后又去设置里换了 provider
    st = ModelSettingsStore(tmp / "model_settings.json")
    st.save({"provider": "ollama",
             "ollama": {"url": "http://127.0.0.1:11434", "model": "改回去了"},
             "openai": {"base_url": "https://mine.example/v1",
                        "model": "mine", "api_key": "sk-MINE"}})

    beta.apply_on_startup(st)          # 相当于下一次启动
    after = st.load()
    assert after["provider"] == "openai"
    assert after["openai"]["model"] == "fake-test-model"


def test_beta_code_not_logged(beta_client, caplog):
    """C 安全：体验码明文不进日志。"""
    import logging
    c, _, _, _ = beta_client
    with caplog.at_level(logging.DEBUG):
        c.post("/api/beta/unlock", json={"code": BETA_CODE})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert BETA_CODE not in text
    assert FAKE_KEY not in text



def _stub_adapter(monkeypatch, W, reachable, model_ok):
    """让 build_model 返回一个 check_service 给出指定组合的适配器。

    走的是真实 test_model_settings 逻辑，所以 ok / service_healthy 的
    组合是它自己算出来的——这正是真机上遇到的那种情况。
    """
    from models.base import ServiceCheck

    class _Adapter:
        model = "fake-test-model"
        provider = "openai"
        base_url = "https://example.invalid/v1"

        def check_service(self, timeout=8.0):
            # message 是 ServiceCheck 的必填字段，少传会 TypeError，
            # 然后被 test_model_settings 的 except 吞掉 -> check=None ->
            # 退回用 ok，两种实现结果一样，测试就测不出东西了。
            msg = ("连接成功，但服务列表里没有该模型，"
                   "若该服务支持直接指定模型名，可忽略此提示") if reachable \
                else "连接失败：模型服务不可达"
            return ServiceCheck(reachable=reachable, model_ok=model_ok,
                                message=msg, test_ok=reachable,
                                names=("some-other-model",))

        def test_connection(self, timeout=8.0):
            if reachable:
                return True, ("连接成功，但服务列表里没有该模型，"
                              "若该服务支持直接指定模型名，可忽略此提示")
            return False, "连接失败：模型服务不可达"

    monkeypatch.setattr(W, "build_model", lambda cfg, timeout=None: _Adapter())


def test_beta_unlock_reports_ready_when_model_not_in_list(beta_client, monkeypatch):
    """真机验收抓到的缺陷：模型不在 /models 清单里时不该报「连接没通过」。

    智谱的 glm-4-flash 就不在清单里，但直接指定模型名调用完全可用
    （实测 TTFT 1.47s、回答正常）。若这里用 service_healthy（严格内部标志）
    而不是 ok（产品口径），用户解锁后会以为 Beta 失败。
    """
    import interfaces.webapp as W
    from models.base import ServiceCheck

    c, store, model, tmp = beta_client
    _stub_adapter(monkeypatch, W, reachable=True, model_ok=False)

    r = c.post("/api/beta/unlock", json={"code": BETA_CODE}).json()
    assert r["ok"] is True
    # 关键断言：用产品口径而不是严格标志
    assert r["connection_ok"] is True, r
    assert r["detail"] == "已准备好，可以开始聊天", r["detail"]
    # 仍然如实带上底层的警告文案，不隐瞒
    assert "服务列表" in r["connection_detail"]


def test_beta_unlock_reports_not_ready_when_really_down(beta_client, monkeypatch):
    """反向：真的连不上时必须报「连接没通过」，别被上一个修复带跑。
    """
    import interfaces.webapp as W

    c, store, model, tmp = beta_client
    _stub_adapter(monkeypatch, W, reachable=False, model_ok=False)
    r = c.post("/api/beta/unlock", json={"code": BETA_CODE}).json()
    assert r["ok"] is True, r            # 解锁本身仍成功
    assert r["connection_ok"] is False, r
    assert "连接没通过" in r["detail"], r["detail"]


def _v05_stores(client):
    from memory.goal_store import GoalStore
    from memory.pattern_store import PatternStore

    c, store, _ = client
    return c, PatternStore(store.db_path), GoalStore(store.db_path)


def test_patterns_crud_api(client):
    c, pat, _ = _v05_stores(client)
    p, _ = pat.create("长期规律", "scope:x", source="promotion")
    body = c.get("/api/patterns").json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "tentative"
    d = c.get(f"/api/patterns/{p.id}").json()
    assert d["ok"] and d["pattern"]["id"] == p.id
    assert c.post(f"/api/patterns/{p.id}/confirm").json() == {"ok": True}
    assert pat.get(p.id).status == "confirmed"
    assert c.delete(f"/api/patterns/{p.id}").json()["ok"] is True
    assert c.post(f"/api/patterns/{p.id}/restore").json()["ok"] is True
    assert pat.get(p.id).status == "tentative"
    assert c.post(f"/api/patterns/{p.id}/reject",
                  json={"reason": "不对"}).json() == {"ok": True}
    assert pat.get(p.id).status == "rejected"
    assert c.get("/api/patterns/" + "0" * 32).status_code == 404


def test_goals_crud_api(client):
    c, _, goal = _v05_stores(client)
    g, _ = goal.propose("每天跑步", "我想成为每天跑步的人")
    body = c.get("/api/goals").json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "proposed"
    d = c.get(f"/api/goals/{g.id}").json()
    assert d["ok"] and d["goal"]["linked_pattern_ids"] == []
    assert c.post(f"/api/goals/{g.id}/confirm").json() == {"ok": True}
    assert goal.get(g.id).status == "active"
    assert c.post(f"/api/goals/{g.id}/pause").json() == {"ok": True}
    assert c.post(f"/api/goals/{g.id}/confirm").json() == {"ok": True}
    assert c.post(f"/api/goals/{g.id}/done").json() == {"ok": True}
    assert goal.get(g.id).status == "done"
    assert c.get("/api/goals/" + "0" * 32).status_code == 404


def test_reflection_api_and_ui(client):
    c, _, _ = client
    body = c.get("/api/reflection/weekly").json()
    assert body["ok"] is True
    assert set(body) >= {"lines", "question", "stats"}
    assert len(body["lines"]) == 3
    html = c.get("/").text
    assert "过去7天" in html and "weeklyList" in html and "loadWeekly" in html


def test_clear_memories_clears_patterns_keeps_goals(client):
    """清空连带规律（派生理解），保留目标（用户原话，语义同 Context）。"""
    c, pat, goal = _v05_stores(client)
    p, _ = pat.create("长期规律", "scope:x", source="promotion")
    g, _ = goal.propose("每天跑步", "我想成为每天跑步的人")
    r = c.delete("/api/memories").json()
    assert r["ok"] is True
    assert pat.count_live() == 0
    assert goal.get(g.id).status == "proposed"

# ---------- 第二阶段：退出时取消后台沉淀（无孤儿） ----------

def test_app_shutdown_cancels_background_deposit(tmp_path, monkeypatch):
    """应用退出必须走 conversation.shutdown()（后台沉淀不留孤儿）。

    这里盯的是**钩子接线**：钩子被删/改名，孤儿任务就没人收了。
    （任务集合本身在 loop 拆除时也会被清空，盯它测不出接线问题。）
    """
    import time as _time
    import core.pipeline as pm
    from interfaces.webapp import create_app
    from memory.store import MemoryStore

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")

    class SlowModel(MockChatModel):
        started = False

        def chat(self, messages, *, images=None, timeout=None):
            out = super().chat(messages, images=images, timeout=timeout)
            if "记忆沉淀" in messages[0]["content"]:
                SlowModel.started = True
                _time.sleep(2.0)
            return out

    model = SlowModel(text_queue=[
        "收到。",
        json.dumps({"new": [{"kind": "fact", "content": "用户喜欢喝茶"}],
                    "corrections": []}, ensure_ascii=False),
    ])
    store = MemoryStore(tmp_path / "shutdown.db")
    app = create_app(store=store, model=model)
    conv = app.state.conversation

    shutdown_calls = []
    orig_shutdown = conv.shutdown

    async def _spy():
        shutdown_calls.append(True)
        await orig_shutdown()

    conv.shutdown = _spy

    with TestClient(app) as c:
        r = c.post("/api/conversation/stream",
                   json={"message": "我喜欢喝茶"})
        assert r.status_code == 200
        assert len(conv._deposit_tasks) >= 1, "后台任务没被调度"

    # 出了 with → lifespan shutdown 触发
    assert shutdown_calls == [True], "退出时没调用 conversation.shutdown()"
    assert SlowModel.started, "沉淀根本没跑起来，这条测试就没意义"
    assert len(conv._deposit_tasks) == 0, "退出后仍有后台沉淀任务（孤儿）"
