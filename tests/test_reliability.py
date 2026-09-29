# V0.5.1 可靠性：规则兜底纠正 / 无依据提问门 / 数字守卫（Mock，零云端）。
from __future__ import annotations

import json

import pytest

from core.conversation import ConversationService
from memory.hypothesis_store import HypothesisStore
from models.mock_adapter import MockChatModel


def _deposit(new=None, corrections=None):
    return json.dumps({"new": new or [], "corrections": corrections or [],
                       "event": None}, ensure_ascii=False)


@pytest.fixture
def hyp(store):
    return HypothesisStore(store.db_path)


def test_deny_fallback_despite_garbage_json(store, context_store,
                                            chat_store, hyp):
    """审计复现：deposit JSON 非法 → 规则兜底仍让否认生效，当轮停止影响。"""
    e, _ = context_store.add_entry("interaction", "晚上学习效率高",
                                   source="chat")
    h, _ = hyp.create("用户晚上学习效率高", "interaction:晚上")
    assert hyp.add_evidence(h.id, "support", "context", e.id)[0]
    model = MockChatModel(text_queue=["晚上学习效率确实很高。",
                                      "这不是JSON{{{"])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    r = conv.chat("不对，我只是最近几天这样")
    assert r.deposit and r.deposit.get("error"), "本轮 deposit 必须真的失败"
    h = hyp.get(h.id)
    assert h.status == "rejected"
    assert h.confidence == pytest.approx(0.25)
    assert h.id not in [x.id for x in hyp.list_for_prompt(limit=10)]


def test_no_double_apply_when_deposit_succeeds(store, context_store,
                                               chat_store, hyp):
    """deposit 纠正成功时兜底不重复处罚（反证恰好一条）。"""
    e, _ = context_store.add_entry("interaction", "考试前一天提醒我",
                                   source="chat")
    h, _ = hyp.create("用户需要提前提醒", "interaction:提醒")
    hyp.add_evidence(h.id, "support", "context", e.id)
    model = MockChatModel(text_queue=[
        "明白",
        _deposit(corrections=[{"ref": "#1",
                               "content": "用户不需要提前提醒",
                               "reason": "用户否决"}]),
    ])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    conv.chat("不对，我不需要这种提醒")
    d = hyp.get_detail(h.id)
    assert len([x for x in d["evidence"]
                if x["polarity"] == "contradict"]) == 1


def test_ungrounded_question_creates_nothing(store, context_store,
                                             chat_store):
    """无依据提问（弟弟名字）：不新建理解，但纠正通道不受影响。"""
    e, _ = context_store.add_entry("fact", "用户是高二学生", source="chat")
    model = MockChatModel(text_queue=[
        "不知道，没听你提过。",
        _deposit(new=[{"kind": "fact", "content": "用户有个弟弟"}],
                 corrections=[{"ref": "#1", "content": "用户是高一学生",
                               "reason": "纠正年级"}]),
    ])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    r = conv.chat("我弟弟叫什么名字？")
    assert context_store.count_active() == 1  # 只剩被纠正的那条
    assert r.deposit["corrected"], "纠正必须照常生效"
    assert context_store.get_entry(e.id).content == "用户是高一学生"


def test_grounded_question_and_statement_normal(store, context_store,
                                                chat_store):
    """有依据的提问照常沉淀；陈述句不受门影响。"""
    context_store.add_entry("fact", "用户在备战CSP", source="chat")
    model = MockChatModel(text_queue=[
        "还行。",
        _deposit(new=[{"kind": "dynamic", "content": "CSP备考顺利"}]),
        "收到。",
        _deposit(new=[{"kind": "profile", "content": "用户是高二学生"}]),
    ])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    conv.chat("CSP最近状态怎么样？")
    conv.chat("我是高二学生")
    assert context_store.count_active() == 3


def test_fallback_no_match_no_crash(store, context_store, chat_store, hyp):
    """显式否决但本轮无关联条目：不崩，假设不动，可观测日志。"""
    e, _ = context_store.add_entry("interaction", "喜欢吃香菜", source="chat")
    h, _ = hyp.create("用户喜欢香菜", "interaction:香菜")
    hyp.add_evidence(h.id, "support", "context", e.id)
    model = MockChatModel(text_queue=["好的。", _deposit()])
    conv = ConversationService(store, context_store, model,
                               chat_store=chat_store)
    conv.chat("不对，完全不是这样")
    h = hyp.get(h.id)
    assert (h.status, h.confidence) == ("tentative", pytest.approx(0.58))


def test_number_guard(store, context_store):
    """冲突数字不合并：1号/2号、三天/五天各自独立；纯复述仍合并。"""
    a, _ = context_store.add_entry("fact", "用户喜欢喝1号咖啡", source="chat")
    b, skip = context_store.add_or_merge("fact", "用户喜欢喝2号咖啡")
    assert b is not None, skip
    c, _ = context_store.add_entry("interaction", "提前三天提醒我",
                                   source="chat")
    d, skip = context_store.add_or_merge("interaction", "提前五天提醒我")
    assert d is not None, skip
    e, _ = context_store.add_entry("interaction", "考试前一天提醒我",
                                   source="chat")
    f, skip = context_store.add_or_merge("interaction", "考试前一天提醒我哦")
    assert f is None and (skip or "").startswith("merged:")
    assert context_store.count_active() == 5
    assert a.id != b.id and c.id != d.id
