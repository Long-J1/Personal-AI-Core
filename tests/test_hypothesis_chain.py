"""V0.3 H4：纠正联动全链（Mock 模型，确定性）。

Context/Interaction → Hypothesis → user correction
→ contradictory evidence → confidence 变化 → prompt 变化。
"""
from __future__ import annotations

import json

import pytest

from core.conversation import ConversationService
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel


def _deposit(new=None, corrections=None):
    return json.dumps({"new": new or [], "corrections": corrections or [],
                       "event": None}, ensure_ascii=False)


@pytest.fixture
def chain(tmp_path):
    db = tmp_path / "chain.db"
    store = MemoryStore(db)
    ctx = ContextStore(db)
    chat_store = ChatStore(db)
    e1, _ = ctx.add_entry("interaction", "考试前一天提醒我", source="chat")
    e2, _ = ctx.add_entry("interaction", "重要事情提前提醒我", source="chat")
    model = MockChatModel(text_queue=[
        "好的", _deposit(),          # chat1：回答 + 空沉淀
        "收到", _deposit(),          # chat2
        "明白", _deposit(corrections=[{  # chat3：纠正 #1（= e2）
            "ref": "#1", "content": "用户不需要提前提醒",
            "reason": "用户否决"}]),
        "嗯", _deposit(),            # chat4
    ])
    conv = ConversationService(store, ctx, model, chat_store=chat_store)
    hyp = HypothesisStore(db)
    return conv, model, hyp, ctx


def _system_of_call(model, idx):
    return model.calls[idx]["messages"][0]["content"]


def test_h4_induce_then_prompt_then_correct(chain):
    conv, model, hyp, ctx = chain

    # 1) 首轮对话后，归纳器从 2 条 interaction 提假设（R2）
    r1 = conv.chat("你好")
    assert hyp.count_live() == 1
    h = hyp.list_hypotheses()[0]
    assert h.scope == "interaction:提醒我" and h.status == "tentative"  # P0 标签
    # 首轮组装时假设尚未诞生（归纳跑在沉淀之后），空是正确的
    assert r1.to_dict()["hypotheses_used"] == []

    # 2) 第二轮 system prompt 里出现假设段（prompt 变化可断言）
    r2 = conv.chat("在吗")
    assert len(r2.to_dict()["hypotheses_used"]) == 1  # 回复携带假设元数据
    sys2 = _system_of_call(model, 2)
    assert "待验证假设" in sys2
    assert h.statement in sys2
    assert "不是事实" in sys2
    # approach 语义不回退：仍由 interaction 指令决定
    assert set(r2.approach) == {"考试前一天提醒我", "重要事情提前提醒我"}

    # 3) 第三轮显式否决 → 反证 + 置 0.25 + rejected
    conv.chat("不对，我一般不需要这种提醒")
    h = hyp.get(h.id)
    assert h.status == "rejected"
    assert h.confidence == pytest.approx(0.25)
    d = hyp.get_detail(h.id)
    assert any(e["polarity"] == "contradict" for e in d["evidence"])

    # 4) 第四轮 prompt 不再使用被否决的假设
    conv.chat("今天天气不错")
    sys4 = _system_of_call(model, 6)
    assert h.statement not in sys4
    assert "待验证假设" not in sys4


def test_h4_soft_correction_keeps_tentative(chain):
    """软纠正（无否决词）：-0.25，状态保持 tentative，仍进 prompt。"""
    conv, model, hyp, ctx = chain
    conv.chat("你好")
    h = hyp.list_hypotheses()[0]
    before = h.confidence
    # deposit 纠正 #1，但用户原文无否决词
    model.text_queue = [
        "明白", _deposit(corrections=[{
            "ref": "#1", "content": "用户希望提醒更简短",
            "reason": "补充偏好细节"}]),
    ]
    conv.chat("提醒可以简短一点")
    h = hyp.get(h.id)
    assert h.status == "tentative"
    assert h.confidence == pytest.approx(before - 0.25)


# ---------- bugfix 回归（2026-09-27 扫 bug） ----------

def test_empty_answer_is_model_error(store, context_store, chat_store):
    """空回答不是回答：按模型故障抛 ModelError（webapp 转 503）."""
    from models.base import ModelError
    model = MockChatModel(text_queue=["   "])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    sid = None
    try:
        conv.chat("你好")
        assert False, "空回答应该抛 ModelError"
    except ModelError:
        pass
    # 空回答不污染会话历史：唯一会话里没有任何消息
    import sqlite3
    con = sqlite3.connect(str(store.db_path))
    n = con.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
    con.close()
    assert n == 0


def test_deposit_exception_does_not_swallow_answer(
        store, context_store, chat_store, monkeypatch):
    """沉淀抛非 ModelError（如 sqlite 锁）也不许吞掉已生成的回答."""
    model = MockChatModel(text_queue=["这是回答"])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)

    def boom(*a, **k):
        raise RuntimeError("模拟 sqlite 锁超时")

    monkeypatch.setattr(conv.depositor, "deposit", boom)
    r = conv.chat("你好")
    assert r.answer == "这是回答"
    assert r.deposit is not None and "跳过" in (r.deposit.get("error") or "")


def test_apply_creates_records_truncated_content(store, context_store):
    """deposit 返回落库后的截断内容，前后端一致."""
    from core.config import Settings
    from core.deposit import Depositor
    settings = Settings(deposit_max_chars=10, deposit_max_new=3)
    dep = Depositor(context_store, store, MockChatModel(), settings=settings)
    from core.deposit import DepositResult
    res3 = DepositResult()
    dep._apply_creates(
        {"new": [{"kind": "fact", "content": "这是一段超过十个字的长内容"}]},
        res3)
    assert res3.created and len(res3.created[0]["content"]) <= 10
    row = context_store.get_entry(res3.created[0]["id"])
    assert row.content == res3.created[0]["content"]


def test_same_turn_reject_retracts_pending(chain):
    """同轮问出又否决：收回挂起，元数据置空，不留僵尸问题。"""
    conv, model, hyp, ctx = chain
    conv.chat("你好")
    conv.chat("在吗")
    r3 = conv.chat("不对，我一般不需要这种提醒")
    assert r3.to_dict()["verification_question"] is None
    assert hyp.get_pending() is None
    # 全库只有一个假设（没有改述复活的双胞胎）
    assert len(hyp.list_hypotheses(live_only=False)) == 1
    r4 = conv.chat("今天天气不错")
    assert r4.to_dict()["verification_question"] is None
