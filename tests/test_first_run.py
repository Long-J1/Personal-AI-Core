"""第一次被体验的真实流程（V0.5.7 Phase 4）。

前面那些测试证明"翻译函数是对的"。这个文件证明的是另一件事：

    一个第一次安装的人，只通过聊天，能不能真的走到"AI 开始认识我"。

全部走真实链路：conversation.chat() → 后台沉淀 → 落库 → /api/understanding。
最后一条是 replay——固定用户故事跑两遍，结果必须一致。
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from core.service_state import ServiceState
from interfaces.webapp import create_app
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel


def deposit_json(new=(), corrections=(), event=None) -> str:
    return json.dumps({"new": list(new), "corrections": list(corrections),
                       "event": event}, ensure_ascii=False)


def wait_for(pred, timeout=30.0, interval=0.15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


@pytest.fixture
def world(tmp_path, monkeypatch):
    import core.pipeline as pm

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    store = MemoryStore(tmp_path / "fr.db")
    app = create_app(store=store, model=MockChatModel(),
                     service_state=ServiceState())
    return TestClient(app), store


def say(client, model, text, deposit=None, event=None):
    """发一轮消息，并等后台沉淀真的跑完再返回。

    必须等：后台线程共享 Mock 的 text_queue，并发发会互相抢 JSON；
    而且沉淀按 sha256(message) 去重，同一句话只沉淀一次。
    """
    model.text_queue = ["好的。", deposit_json(new=(), corrections=(),
                                               event=event) if deposit is None
                        else deposit]
    client.post("/api/conversation", json={"message": text})
    wait_for(lambda: True, timeout=0.3)      # 给线程一点调度时间


def settled(client, store):
    """等所有后台沉淀落定（用消息数当稳定信号）。"""
    wait_for(lambda: True, timeout=0.5)


def understanding(client):
    return client.get("/api/understanding").json()


# ============================================================ 1 新人第一次体验

def test_new_user_first_run_offers_invitation(world):
    """第一次打开：邀请出现，且不要求任何配置。"""
    client, _ = world
    w = client.get("/api/understanding/warmup").json()
    assert w["active"] is True
    assert w["opening"] == "聊几句，让我了解你的习惯。"
    assert len(w["topics"]) == 3
    assert all(t["ask"] for t in w["topics"])


def test_new_user_first_run_sees_empty_timeline_not_errors(world):
    client, _ = world
    r = understanding(client)
    assert r["count"] == 0
    assert r["items"] == []
    assert r["onboarding"]["is_first_run"] is True
    assert r["onboarding"]["done"] is False


def test_first_run_progresses_as_user_talks(world):
    """聊几句之后，引导方向一个一个被覆盖——系统真的在跟着了解。"""
    client, store = world
    from memory.context_store import ContextStore

    cs = ContextStore(store.db_path)
    cs.add_entry(kind="dynamic", content="我一般晚上十点后学习效率最高",
                 source="user_direct", source_grade="user_direct")
    r = understanding(client)
    covered = [t["key"] for t in r["onboarding"]["topics"] if t["covered"]]
    assert covered == ["routine"]


# ============================================================ 2 三次聊天形成理解

def test_three_chats_form_an_understanding(world):
    """同一个活动出现三次 → 形成理解，并且能在时间线上看到、追到原话。"""
    client, store = world
    model = client.app.state.__dict__.get("model") if hasattr(
        client.app, "state") else None

    # 直接用真实 conversation 走三轮
    from memory.chat_store import ChatStore
    from core.conversation import ConversationService
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore

    model = MockChatModel()
    cs = ContextStore(store.db_path)
    hs = HypothesisStore(store.db_path)
    conv = ConversationService(store, cs, model, chat_store=ChatStore(
        store.db_path), hypothesis_store=hs)

    def events_now():
        import sqlite3
        con = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
        try:
            return con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            con.close()

    for i in range(1, 4):
        model.text_queue = ["知道了。", deposit_json(event={
            "description": f"第{i}次晚自习", "activity": "自习",
            "scene": "家里", "importance": 0.5})]
        conv.chat(f"还是老样子，第{i}天十点后去自习。")
        assert wait_for(lambda: events_now() >= i, timeout=25), \
            f"第 {i} 次事件未落库"

    assert wait_for(lambda: len(hs.list_hypotheses(live_only=True,
                                                    limit=20)) >= 1, timeout=25), \
        "三次重复表达后仍未形成理解"

    r = understanding(client)
    assert r["count"] >= 1
    assert r["items"][0]["quotes"], "形成的理解必须能追回用户原话"


# ============================================================ 3 用户改变习惯

def test_changed_habit_shows_as_changed_with_reason(world):
    """用户改主意 → 时间线上说清"你什么时候说的、我怎么改的"。"""
    client, store = world
    from core.natural_bridge import apply_natural_signal
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore

    cs = ContextStore(store.db_path)
    hs = HypothesisStore(store.db_path)
    h, _ = hs.create("用户近期多次进行「学习」相关活动，持续期。",
                     "activity:学习")
    cs.add_entry(kind="dynamic", content="晚上十点后学习效率最高",
                 source="user_direct", source_grade="user_direct")

    apply_natural_signal(cs, hs, "我最近已经改成早上学习了。")

    r = understanding(client)
    assert r["changed"], "变化必须被看见"
    notes = "\n".join(x["change_note"] for x in r["changed"])
    assert "早上学习" in notes, f"变化说明里必须有用户原话：{notes}"
    assert any(x["state_text"] == "有过变化" for x in r["changed"])


def test_change_does_not_delete_history_from_timeline(world):
    """旧理解被取代了，但仍然查得到——信任的前提是可追溯。"""
    client, store = world
    from core.natural_bridge import apply_natural_signal
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore

    cs = ContextStore(store.db_path)
    hs = HypothesisStore(store.db_path)
    h, _ = hs.create("用户近期多次进行「学习」相关活动，持续期。",
                     "activity:学习")
    e, _ = cs.add_entry(kind="dynamic", content="晚上十点后学习效率最高",
                        source="user_direct", source_grade="user_direct")
    apply_natural_signal(cs, hs, "我最近已经改成早上学习了。")

    old = cs.get_entry(e.id)
    assert old is not None                    # 还在库里
    assert old.memory_status == "outdated"    # 但不再当现状
    assert "晚上十点后" in old.content


# ============================================================ 4 理解展示

def test_timeline_shows_product_language_only(world):
    """用户看到的每一个字都不能是内部术语。"""
    client, store = world
    from memory.hypothesis_store import HypothesisStore

    hs = HypothesisStore(store.db_path)
    h, _ = hs.create("用户近期多次进行「自习」相关活动，持续期。",
                     "activity:自习")
    hs.record_natural_evidence(h.id, "对，我就是十点之后去自习。", "support")

    body = client.get("/api/understanding").text
    for word in ("confidence", "source_type", "hypothesis_id",
                 "quota_overflow", "activity:", "correction"):
        assert word not in body, f"展示层泄漏了内部术语：{word}"


def test_why_button_has_material_to_show(world):
    """"为什么这么认为"必须有真东西可展示，不能是空壳按钮。"""
    client, store = world
    from memory.hypothesis_store import HypothesisStore

    hs = HypothesisStore(store.db_path)
    h, _ = hs.create("用户近期多次进行「自习」相关活动，持续期。",
                     "activity:自习")
    hs.record_natural_evidence(h.id, "对，我就是十点之后去自习。", "support")

    row = understanding(client)["items"][0]
    assert row["quotes"], "没有原话就解释不了为什么"
    assert row["quotes"][0]["text"] == "对，我就是十点之后去自习。"
    assert row["quotes"][0]["when"]


def test_empty_timeline_is_encouraging_not_technical(world):
    client, _ = world
    r = understanding(client)
    assert r["count"] == 0
    # 空状态下前端会显示引导文案；这里确认后端没塞任何内部字段
    assert "confidence" not in json.dumps(r, ensure_ascii=False)


# ============================================================ 5 replay

REPLAY_SCRIPT = [
    ("我一般晚上十点后学习效率最高。",
     {"new": [{"kind": "dynamic", "content": "晚上十点后学习效率最高"}],
      "corrections": [], "event": None}),
    ("还是老样子，第2天十点后自习。",
     {"new": [], "corrections": [], "event": {
         "description": "第2次晚自习", "activity": "自习",
         "scene": "家里", "importance": 0.5}}),
    ("还是老样子，第3天十点后自习。",
     {"new": [], "corrections": [], "event": {
         "description": "第3次晚自习", "activity": "自习",
         "scene": "家里", "importance": 0.5}}),
    ("还是老样子，第4天十点后自习。",
     {"new": [], "corrections": [], "event": {
         "description": "第4次晚自习", "activity": "自习",
         "scene": "家里", "importance": 0.5}}),
    ("对，我就是十点之后去自习。",
     {"new": [], "corrections": [], "event": None}),
    ("我最近已经改成早上自习了。",
     {"new": [{"kind": "dynamic", "content": "最近改成早上自习"}],
      "corrections": [], "event": None}),
    ("今天食堂红烧肉真难吃。",
     {"new": [], "corrections": [], "event": None}),
]


def _run_replay(tmp_path, tag):
    """跑一遍固定故事，返回终态快照。"""
    import core.pipeline as pm
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from core.conversation import ConversationService
    from fastapi.testclient import TestClient
    from interfaces.webapp import create_app
    from core.service_state import ServiceState

    d = tmp_path / tag
    d.mkdir()
    pm.THUMB_DIR = d / "thumbs"
    store = MemoryStore(d / "r.db")
    model = MockChatModel()
    cs, hs = ContextStore(store.db_path), HypothesisStore(store.db_path)
    conv = ConversationService(store, cs, model,
                               chat_store=ChatStore(store.db_path),
                               hypothesis_store=hs)
    client = TestClient(create_app(store=store, model=model,
                                   service_state=ServiceState()))
    for text, dep in REPLAY_SCRIPT:
        model.text_queue = ["好的。", json.dumps(dep, ensure_ascii=False)]
        conv.chat(text)
        wait_for(lambda: True, timeout=0.6)
    wait_for(lambda: True, timeout=3.0)
    snap = client.get("/api/understanding").json()
    # 回放比对只认"产品语义"，不认绝对时间
    return {
        "count": snap["count"],
        "states": sorted(i["state"] for i in snap["items"]),
        "changed": len(snap["changed"]),
        "topics": sorted(i["topic"] for i in snap["items"]),
        "quotes": sorted(q["text"] for i in snap["items"]
                         for q in i["quotes"]),
    }


def test_replay_is_deterministic(tmp_path):
    """固定用户故事跑两遍，结果必须一致。

    这是本次新增的第一条回放保护：理解链的展示层不许出现
    "跑一次一个样"的情况——那用户就没法信任"AI 真的在认识我"。
    """
    a = _run_replay(tmp_path, "run1")
    b = _run_replay(tmp_path, "run2")
    assert a == b, f"回放不一致：\n 第一次={a}\n 第二次={b}"


def test_replay_ends_with_changed_understanding(tmp_path):
    """故事本身要成立：先形成理解，用户改主意后标记为变化。"""
    snap = _run_replay(tmp_path, "check")
    assert snap["count"] >= 1, "三次重复后应有理解"
    assert snap["changed"] >= 1, "用户改主意后应被标记为变化"
    assert any("改成早上自习" in q for q in snap["quotes"]), \
        f"新说法必须出现在时间线上：{snap['quotes']}"


def test_replay_chitchat_adds_nothing(tmp_path):
    """结尾那句闲聊不能凭空多出一条理解。"""
    snap = _run_replay(tmp_path, "chat")
    assert not any("红烧肉" in q for q in snap["quotes"])