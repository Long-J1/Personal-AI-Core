# V0.5 Reflection：digest 数字可推导 + 至多1问 + 失败静默（fixture 库）。
from __future__ import annotations

import time

import pytest

from core.reflection import build_weekly_digest
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.goal_store import GoalStore
from memory.hypothesis_store import HypothesisStore
from memory.pattern_store import PatternStore
from memory.store import MemoryStore


@pytest.fixture
def all_stores(tmp_path):
    db = tmp_path / "digest.db"
    return (MemoryStore(db), ContextStore(db), HypothesisStore(db),
            PatternStore(db), GoalStore(db))


def test_digest_empty(all_stores):
    store, ctx, hyp, pat, goal = all_stores
    d = build_weekly_digest(store, ctx, hyp, pat, goal)
    assert d["stats"] == {"events": 0, "entries": 0, "hyp_created": 0,
                          "hyp_confirmed": 0, "hyp_rejected": 0,
                          "hyp_asked": 0, "hyp_live": 0, "pat_created": 0,
                          "pat_confirmed": 0, "patterns_live": 0,
                          "goals_active": 0}
    assert len(d["lines"]) == 3 and d["question"] == ""


def test_digest_numbers_derivable(all_stores):
    """每一行数字 = DB count（无幻觉）。"""
    store, ctx, hyp, pat, goal = all_stores
    from memory.event import Event
    store.add_event(Event(source="chat", scene="s", activity="a",
                          description="事件一", importance=0.5,
                          raw_understanding=None))
    ctx.add_entry("fact", "用户喜欢猫", source="chat")
    h, _ = hyp.create("用户接受提醒", "interaction:提醒")
    e, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    hyp.add_evidence(h.id, "support", "context", e.id)
    hyp.confirm(h.id)
    h2, _ = hyp.create("另一条", "scope:z")
    hyp.reject(h2.id, reason="不对")
    p, _ = pat.create("长期规律", "scope:x", source="promotion")
    g, _ = goal.propose("每天跑步", "我想成为每天跑步的人")
    goal.confirm(g.id)
    d = build_weekly_digest(store, ctx, hyp, pat, goal)
    s = d["stats"]
    assert (s["events"], s["entries"]) == (1, 2)
    assert (s["hyp_created"], s["hyp_confirmed"], s["hyp_rejected"]) == (
        2, 1, 1)
    assert (s["pat_created"], s["patterns_live"], s["goals_active"]) == (
        1, 1, 1)
    assert "1 件" in d["lines"][0] and "2 条" in d["lines"][0]
    # 否决优先出 gentle 问句（至多1个）
    assert "否决" in d["question"]


def test_digest_question_priority(all_stores):
    store, ctx, hyp, pat, goal = all_stores
    # pending > 新规律：先挂一个假设问题
    h, _ = hyp.create("用户接受提醒", "interaction:提醒")
    hyp.set_pending(h.id, "问题？")
    d = build_weekly_digest(store, ctx, hyp, pat, goal)
    assert "还没回" in d["question"]
    hyp.clear_pending(h.id)
    hyp.reject(h.id, reason="不对")
    p, _ = pat.create("长期规律", "scope:x", source="promotion")
    d = build_weekly_digest(store, ctx, hyp, pat, goal)
    assert "否决" in d["question"]  # 否决优先于新规律


def test_digest_failure_silent(all_stores, monkeypatch):
    """某段统计炸了 → 默认值，其他行照常（失败静默）。"""
    store, ctx, hyp, pat, goal = all_stores
    ctx.add_entry("fact", "用户喜欢猫", source="chat")

    def boom(*a, **k):
        raise RuntimeError("磁盘坏了")

    monkeypatch.setattr(store, "count_events_since", boom)
    d = build_weekly_digest(store, ctx, hyp, pat, goal)
    assert d["stats"]["events"] == 0
    assert len(d["lines"]) == 3
