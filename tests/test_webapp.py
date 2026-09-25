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
    assert set(s) >= {"version", "model", "events", "paused", "model_service_ok"}
    assert s["events"] == 0
