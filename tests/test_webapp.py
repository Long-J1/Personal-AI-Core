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

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    store = MemoryStore(tmp_path / "web.db")
    model = MockChatModel(understanding_queue=[_UNDERSTANDING])
    return TestClient(create_app(store=store, model=model)), store, model


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
    assert data["deposit"]["created"]

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
    """H5-UI：首页含有独立假设区块（与④视觉分开），JS 接线存在。"""
    c, _, _ = client
    html = c.get("/").text
    assert "待验证假设" in html
    assert "hypoList" in html and "loadHypos" in html
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
