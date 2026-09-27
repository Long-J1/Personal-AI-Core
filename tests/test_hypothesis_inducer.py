"""V0.3 H2/H3：规则归纳器 + 反证衰减。

fixture 库，不碰真实数据库。
"""
from __future__ import annotations

import pytest

from core.inducer import Inducer
from memory.context_store import ContextStore
from memory.event import Event
from memory.hypothesis_store import HypothesisStore
from memory.store import MemoryStore


@pytest.fixture
def stores(tmp_path):
    db = tmp_path / "ind.db"
    return (MemoryStore(db), ContextStore(db), HypothesisStore(db))


def _event(activity, description):
    return Event(source="chat", scene="书房", activity=activity,
                 description=description, importance=0.6,
                 raw_understanding=None)


def _interaction(ctx, content):
    entry, skip = ctx.add_entry("interaction", content, source="chat")
    assert entry is not None, skip
    return entry


# ---------- H2：R1 活动重复 ----------

def test_r1_creates_hypothesis(stores):
    mem, ctx, hyp = stores
    for i in range(3):
        mem.add_event(_event("写作业", f"用户在写数学作业{i}"))
    r = Inducer(mem, ctx, hyp).induce_all()
    assert r.checked_events == 3
    assert len(r.created) == 1
    h = hyp.get(r.created[0])
    assert h.scope == "activity:写作业"
    assert "写作业" in h.statement and "3次" in h.statement
    assert h.status == "tentative"
    assert h.support_count == 3
    # 3 个独立事件源 → 不 cap：0.50 + 3×0.08 = 0.74
    assert h.confidence == pytest.approx(0.74)
    d = hyp.get_detail(h.id)
    assert {e["source_id"] for e in d["evidence"]} == {
        e.id for e in mem.list_events(limit=10)}
    assert d["independent_sources"] == 3


def test_r1_below_threshold(stores):
    mem, ctx, hyp = stores
    for i in range(2):
        mem.add_event(_event("写作业", f"作业{i}"))
    r = Inducer(mem, ctx, hyp).induce_all()
    assert r.created == [] and hyp.count_live() == 0


def test_r1_empty_activity_ignored(stores):
    mem, ctx, hyp = stores
    for i in range(4):
        mem.add_event(_event("", f"无活动事件{i}"))
    r = Inducer(mem, ctx, hyp).induce_all()
    assert r.created == []


def test_r1_dedup_links_new_only(stores):
    """跑两遍不建第二个；新增同类事件后只补证据。"""
    mem, ctx, hyp = stores
    for i in range(3):
        mem.add_event(_event("跑步", f"晨跑{i}"))
    ind = Inducer(mem, ctx, hyp)
    r1 = ind.induce_all()
    r2 = ind.induce_all()
    assert len(r1.created) == 1
    assert r2.created == [] and r2.linked == r1.created
    assert hyp.count_live() == 1
    mem.add_event(_event("跑步", "夜跑"))
    r3 = ind.induce_all()
    assert r3.created == []
    assert hyp.get(r1.created[0]).support_count == 4


# ---------- H2：R2 相处偏好主题 ----------

def test_r2_creates_hypothesis(stores):
    mem, ctx, hyp = stores
    _interaction(ctx, "考试前一天提醒我")
    _interaction(ctx, "重要事情提前提醒我")
    _interaction(ctx, "不喜欢被打扰")
    r = Inducer(mem, ctx, hyp).induce_all()
    assert r.checked_interactions == 3
    assert len(r.created) == 1
    h = hyp.get(r.created[0])
    assert h.scope == "interaction:提醒"
    assert "提醒" in h.statement and "2条" in h.statement
    assert h.support_count == 2
    # 同日两条 → 1 个独立源：0.50 + 0.16 = 0.66（未触 cap）
    assert h.confidence == pytest.approx(0.66)


def test_r2_single_no_hypothesis(stores):
    mem, ctx, hyp = stores
    _interaction(ctx, "考试前一天提醒我")
    r = Inducer(mem, ctx, hyp).induce_all()
    assert r.created == []


def test_r2_explicit_origins_count_separately(stores):
    """显式不同 origin 才算独立源（独立性定义的直接测试）。"""
    mem, ctx, hyp = stores
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    e1 = _interaction(ctx, "考试前提醒我")
    e2 = _interaction(ctx, "比赛前提醒我")
    hyp.add_evidence(h.id, "support", "context", e1.id,
                     origin="session:A", excerpt="a")
    hyp.add_evidence(h.id, "support", "context", e2.id,
                     origin="session:B", excerpt="b")
    assert hyp.get_detail(h.id)["independent_sources"] == 2


def test_rejected_scope_not_recreated(stores):
    """否决过的主题再现：不新建、不喂料、不打扰（尊重否决）。"""
    mem, ctx, hyp = stores
    for i in range(3):
        mem.add_event(_event("写作业", f"作业{i}"))
    ind = Inducer(mem, ctx, hyp)
    r1 = ind.induce_all()
    assert len(r1.created) == 1
    hyp.reject(r1.created[0], reason="不需要")
    mem.add_event(_event("写作业", "又有作业"))
    r2 = ind.induce_all()
    assert r2.created == [] and r2.linked == []
    assert r2.skipped == r1.created
    assert hyp.count_live() == 0
    h = hyp.get(r1.created[0])
    assert (h.status, h.support_count) == ("rejected", 3)


def test_paused_skips_induction(stores):
    mem, ctx, hyp = stores
    for i in range(3):
        mem.add_event(_event("写作业", f"作业{i}"))
    mem.paused = True
    try:
        r = Inducer(mem, ctx, hyp).induce_all()
        assert r.created == [] and hyp.count_live() == 0
    finally:
        mem.paused = False


# ---------- H3：反证衰减 ----------

def test_induced_then_contradicted(stores):
    mem, ctx, hyp = stores
    for i in range(3):
        mem.add_event(_event("写作业", f"作业{i}"))
    Inducer(mem, ctx, hyp).induce_all()
    h = hyp.list_hypotheses()[0]
    assert h.confidence == pytest.approx(0.74)
    entry, _ = ctx.add_entry("interaction", "这段时间别再提醒学习的事",
                             source="chat")
    ok, reason = hyp.add_evidence(h.id, "contradict", "correction", entry.id,
                                  excerpt="用户纠正")
    assert ok, reason
    h = hyp.get(h.id)
    assert h.confidence == pytest.approx(0.74 - 0.25)
    assert h.status == "tentative"  # 软反证不变状态
    assert hyp.reject(h.id, reason="不需要")
    assert hyp.get(h.id).confidence == pytest.approx(0.25)
