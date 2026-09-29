"""V0.4 验证提问：搭便车，不拦路（Mock 模型，确定性，零云端调用）。

覆盖 PLAN Q1~Q7：
Q1 选择规则 · Q2 只提问不下结论 · Q3 确认链 · Q4 行为变化 ·
Q5 否决链 · Q6 沉默保持 · Q7 回归/migration。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from core.conversation import ConversationService
from core.verifier import (
    build_question,
    is_confirmation,
    is_denial,
    is_related,
    resolve_pending_answer,
    scope_label,
    select_candidate,
)
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel


def _deposit(new=None, corrections=None):
    return json.dumps({"new": new or [], "corrections": corrections or [],
                       "event": None}, ensure_ascii=False)


@pytest.fixture
def dbs(tmp_path):
    db = tmp_path / "verify.db"
    return (MemoryStore(db), ContextStore(db), HypothesisStore(db),
            ChatStore(db))


def _seed_remind(ctx):
    ctx.add_entry("interaction", "考试前一天提醒我", source="chat")
    ctx.add_entry("interaction", "重要事情提前提醒我", source="chat")


def _conv(store, ctx, chat_store, texts):
    model = MockChatModel(text_queue=list(texts))
    conv = ConversationService(store, ctx, model, chat_store=chat_store)
    return conv, model


def _system_of_call(model, idx):
    return model.calls[idx]["messages"][0]["content"]


# ---------- 存储层：migration 与 pending ----------

def test_migration_old_schema_keeps_data(tmp_path):
    """Q7：老库（无提问列）初始化不丢数据，新列可写。"""
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("""CREATE TABLE hypotheses (
      id TEXT PRIMARY KEY, statement TEXT, confidence REAL, status TEXT,
      scope TEXT, support_count INTEGER, contradict_count INTEGER,
      used_count INTEGER, created_at_iso TEXT, created_at_epoch REAL,
      updated_at_iso TEXT, updated_at_epoch REAL)""")
    con.execute("INSERT INTO hypotheses VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("h1", "陈述", 0.5, "tentative", "scope:x", 0, 0, 0,
                 "2026-01-01T00:00:00+08:00", 0,
                 "2026-01-01T00:00:00+08:00", 0))
    con.commit()
    con.close()
    hyp = HypothesisStore(p)
    h = hyp.get("h1")
    assert (h.statement, h.status) == ("陈述", "tentative")
    assert (h.asked_count, h.last_asked_epoch, h.pending_question) == (
        0, 0.0, "")
    assert hyp.set_pending("h1", "验证问题？") == (True, None)
    assert hyp.get_pending().id == "h1"


def test_pending_single_and_lifecycle(dbs):
    _, ctx, hyp, _ = dbs
    e, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    hyp.add_evidence(h.id, "support", "context", e.id)
    # 第二个假设挂不上（同时只挂一个）
    h2, _ = hyp.create("另一条", "scope:z")
    assert hyp.set_pending(h.id, "问题1？") == (True, None)
    ok, why = hyp.set_pending(h2.id, "问题2？")
    assert ok is False and "只挂一个" in (why or "")
    # 非 tentative 挂不上
    hyp.confirm(h2.id)
    assert hyp.set_pending(h2.id, "问题？")[0] is False
    # confirm/reject/delete 解决即清 pending
    hyp.confirm(h.id)
    assert hyp.get_pending() is None


def test_verification_confirm_keeps_confidence(dbs):
    """确认只改状态不动置信度，但回答进 support 证据链。"""
    _, ctx, hyp, _ = dbs
    e, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    hyp.add_evidence(h.id, "support", "context", e.id)
    before = hyp.get(h.id).confidence
    assert hyp.set_pending(h.id, "问题？") == (True, None)
    assert hyp.record_verification_confirm(h.id, "对的，就是这样") is True
    h = hyp.get(h.id)
    assert h.status == "confirmed"
    assert h.confidence == before  # 置信度不动
    assert h.support_count == 2
    assert h.pending_question == ""
    d = hyp.get_detail(h.id)
    ev = [x for x in d["evidence"]
          if x["detail"] and "verification" in str(x["detail"])]
    assert len(ev) == 1 and ev[0]["excerpt"] == "对的，就是这样"


def test_note_evasive_keeps_pending(dbs):
    _, ctx, hyp, _ = dbs
    e, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    hyp.add_evidence(h.id, "support", "context", e.id)
    hyp.set_pending(h.id, "问题？")
    before_last = hyp.get(h.id).last_asked_epoch
    assert hyp.note_evasive(h.id) is True
    h = hyp.get(h.id)
    assert h.pending_question == "问题？"  # 不清挂起
    assert h.last_asked_epoch >= before_last  # 刷新冷却
    assert h.support_count == 1 and h.contradict_count == 0  # 不记证据


# ---------- Q1 选择规则 ----------

def _eligible_hypo(hyp, ctx):
    """造一个符合 band 的假设：1 条支持 → 0.58，独立源 1。"""
    e, _ = ctx.add_entry("interaction", "考试前一天提醒我", source="chat")
    e2, _ = ctx.add_entry("interaction", "重要事情提前提醒我", source="chat")
    h, _ = hyp.create("用户在提醒方面有偏好", "interaction:提醒")
    hyp.add_evidence(h.id, "support", "context", e.id)
    hyp.add_evidence(h.id, "support", "context", e2.id)
    return hyp.get(h.id)


def test_q1_selects_in_band_related(dbs):
    _, ctx, hyp, _ = dbs
    h = _eligible_hypo(hyp, ctx)
    picked = select_candidate(hyp, "明天开会前提醒我", paused=False)
    assert picked is not None
    cand, q = picked
    assert cand.id == h.id
    assert q.hypothesis_id == h.id and q.confidence == h.confidence
    assert "提醒" in q.text and "66%" in q.text and "对吗？" in q.text
    assert "独立" in q.reason and "提醒" in q.reason
    assert len(q.text) <= 120


def test_q1_band_edges(dbs):
    """下界 0.50 可问；0.70 及以上不问。"""
    _, ctx, hyp, _ = dbs
    e, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    h, _ = hyp.create("低置信假设", "interaction:提醒")  # 0.50 无证据
    assert select_candidate(
        hyp, "提醒我", paused=False)[0].id == h.id
    # 顶到 0.74（3 独立源）：出 band，不问
    for i in range(3):
        ei, _ = ctx.add_entry("interaction", f"提醒事项{i}", source="chat")
        hyp.add_evidence(h.id, "support", "context", ei.id,
                         origin=f"sess:{i}")
    assert hyp.get(h.id).confidence >= 0.70
    assert select_candidate(hyp, "提醒我", paused=False) is None


def test_q1_two_independent_sources_not_asked(dbs):
    """独立源≥2 走自然积累，不问。"""
    _, ctx, hyp, _ = dbs
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    e1, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    e2, _ = ctx.add_entry("interaction", "比赛前提醒我", source="chat")
    hyp.add_evidence(h.id, "support", "context", e1.id, origin="sess:A")
    hyp.add_evidence(h.id, "support", "context", e2.id, origin="sess:B")
    assert hyp.get(h.id).confidence < 0.70  # band 内，但独立源够了
    assert select_candidate(hyp, "提醒我", paused=False) is None


def test_q1_silence_conditions(dbs):
    """挂起/冷却/问满/不相关/暂停 → 全不问（逐项精确断言）。"""
    store, ctx, hyp, _ = dbs
    h = _eligible_hypo(hyp, ctx)
    assert select_candidate(hyp, "今天天气不错", paused=False) is None
    assert select_candidate(hyp, "提醒我", paused=True) is None
    # 挂起中 → 不问；清掉后 → 冷却中 → 不问
    hyp.set_pending(h.id, "问题？")
    assert select_candidate(hyp, "提醒我", paused=False) is None
    hyp.clear_pending(h.id)
    assert select_candidate(hyp, "提醒我", paused=False) is None
    # 问满 3 次 → 即使冷却清零也不问（先把 h 否决出局，免得它被选中）
    hyp.reject(h.id, reason="不需要")
    e, _ = ctx.add_entry("interaction", "提醒我开会", source="chat")
    h2, _ = hyp.create("另一条提醒假设", "interaction:提醒")
    hyp.add_evidence(h2.id, "support", "context", e.id)
    for _ in range(3):
        hyp.set_pending(h2.id, "问题？")
        hyp.clear_pending(h2.id)
    assert hyp.get(h2.id).asked_count == 3
    con = sqlite3.connect(str(hyp.db_path))
    con.execute("UPDATE hypotheses SET last_asked_epoch=0 WHERE id=?",
                (h2.id,))
    con.commit()
    con.close()
    got = select_candidate(hyp, "提醒我开会", paused=False)
    assert got is None  # h 已否决，h2 问满 → 无人可问


def test_q1_keyword_boundaries():
    assert scope_label("interaction:提醒") == "提醒"
    assert scope_label("activity:跑步") == "跑步"
    assert scope_label("无冒号") == "无冒号"
    assert is_related("提醒我开会", "interaction:提醒") is True
    assert is_related("今天天气不错", "interaction:提醒") is False
    assert is_related("什么都聊", "x") is False  # 标签太短不匹配
    assert is_related("什么都聊", "") is False


def test_q1_classify_boundaries():
    assert is_denial("不对，我不需要") is True
    assert is_denial("不同意你的说法") is True
    assert is_denial("没有这回事") is True
    assert is_confirmation("对的。") is True
    assert is_confirmation("是这样，没错") is True
    assert is_confirmation("对吗？") is False  # 复述问题不算确认
    assert is_confirmation("嗯") is False
    assert is_confirmation("好") is False
    assert is_confirmation("是不是这样？") is False
    assert is_denial("是不是应该提醒？") is False


def test_build_question_long_label():
    from memory.hypothesis_store import Hypothesis
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    h = Hypothesis(id="x" * 32, statement="s", confidence=0.58,
                   status="tentative", scope="activity:" + "跑" * 100,
                   support_count=3, contradict_count=0, used_count=0,
                   created_at=now, updated_at=now)
    text, reason = build_question(h)
    assert len(text) <= 120 and text.endswith("对吗？")
    assert "58%" in text


# ---------- Q2/Q3/Q4/Q5/Q6 对话全链 ----------

def _flow(dbs, correct_text="对的，就是这样"):
    """标准三轮流：问 → 提问 → 确认/否决。返回 (conv, model, hyp, h)。"""
    store, ctx, hyp, chat_store = dbs
    _seed_remind(ctx)
    conv, model = _conv(store, ctx, chat_store, [
        "好的", _deposit(),
        "收到", _deposit(),
        "明白", _deposit(),
        "嗯", _deposit(),
        "稍等", _deposit(),
    ])
    return conv, model, hyp


def test_q2_ask_only_no_conclusion(dbs):
    """Q2：挂起后假设三围不动，元数据带来源三要素。"""
    conv, model, hyp = _flow(dbs)[:3]
    conv.chat("聊聊提醒的事")
    r2 = conv.chat("明天开会前提醒我")
    vq = r2.to_dict()["verification_question"]
    assert vq is not None
    assert set(vq) >= {"hypothesis_id", "text", "confidence", "reason"}
    h = hyp.get(vq["hypothesis_id"])
    assert (h.status, h.support_count, h.contradict_count) == (
        "tentative", 2, 0)
    assert h.confidence == pytest.approx(0.66)
    assert "待验证" in vq["text"] or "验证我的理解" in vq["text"]
    # 问题附在回答末尾独立成段
    assert r2.answer.rstrip().endswith("对吗？")
    assert r2.answer.count("多问一句") == 1
    # 沉淀没吃我们的问题（问不算答）
    dep_call = next(c for c in model.calls if "记忆沉淀模块" in str(
        c["messages"][0]["content"]))
    assert "多问一句" not in json.dumps(dep_call, ensure_ascii=False)


def test_q3_confirm_chain_and_q4_prompt_change(dbs):
    """Q3+Q4：确认→confirmed+证据+1+置信度不动；prompt 标记变化。"""
    conv, model, hyp = _flow(dbs)[:3]
    conv.chat("聊聊提醒的事")
    conv.chat("明天开会前提醒我")
    h = hyp.list_hypotheses()[0]

    def sys_of(i):
        return model.calls[i]["messages"][0]["content"]

    assert "[待验证·" in sys_of(2)
    r3 = conv.chat("对的，就是这样")
    h = hyp.get(h.id)
    assert h.status == "confirmed"
    assert h.confidence == pytest.approx(0.66)  # 确认不动置信度
    assert h.support_count == 3
    d = hyp.get_detail(h.id)
    ev = [x for x in d["evidence"] if "verification" in str(x["detail"])]
    assert len(ev) == 1 and ev[0]["excerpt"] == "对的，就是这样"
    # 本轮 prompt 已用上确认（解决在组装之前）
    assert "[用户已确认·" in sys_of(4)
    assert r3.to_dict()["verification_question"] is None  # 已解决，不再问
    # approach 语义不动
    assert set(r3.approach) == {"考试前一天提醒我", "重要事情提前提醒我"}


def test_q5_deny_chain(dbs):
    """Q5：否决→rejected+0.25+清挂起；下轮 prompt 不再含该假设。"""
    conv, model, hyp = _flow(dbs)[:3]
    conv.chat("聊聊提醒的事")
    conv.chat("明天开会前提醒我")
    h = hyp.list_hypotheses()[0]
    conv.chat("不对，我一般不需要这种提醒")
    h = hyp.get(h.id)
    assert (h.status, h.confidence) == ("rejected", 0.25)
    assert h.pending_question == ""
    conv.chat("今天天气不错")
    sys_last = model.calls[-2]["messages"][0]["content"]
    assert h.statement not in sys_last


def test_q6_silence_and_evasive(dbs):
    """Q6：不相关话题不问；回避不清挂起、不记证据。"""
    conv, model, hyp = _flow(dbs)[:3]
    conv.chat("聊聊提醒的事")
    r = conv.chat("今天天气不错")
    assert r.to_dict()["verification_question"] is None
    assert "多问一句" not in r.answer
    # 挂起一个问题后，用户回避
    conv.chat("明天开会前提醒我")
    h = hyp.list_hypotheses()[0]
    assert hyp.get_pending().id == h.id
    conv.chat("嗯")
    h = hyp.get(h.id)
    assert (h.status, h.support_count) == ("tentative", 2)
    assert hyp.get_pending().id == h.id  # 挂起保留
    # 暂停：不解决不问
    store = conv.store
    store.paused = True
    try:
        r = conv.chat("对的")
        assert r.to_dict()["verification_question"] is None
        assert hyp.get(h.id).status == "tentative"  # 暂停不学习
    finally:
        store.paused = False


def test_resolve_without_pending(dbs):
    from core.verifier import resolve_pending_answer
    _, _, hyp, _ = dbs
    assert resolve_pending_answer(hyp, "对的") is None
