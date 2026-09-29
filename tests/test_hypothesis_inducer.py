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
    assert "写作业" in h.statement and "3次" not in h.statement  # P2：无计数
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
    assert h.scope == "interaction:提醒我"  # P0：最长公共子串标签
    assert "提醒" in h.statement and "2条" not in h.statement  # P2：无计数
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


# ---------- P0：主题分裂回归（审计 D1） ----------

def test_p0_mixed_themes_split(stores):
    """审计同款混杂数据（提醒/三天/简短）必须长出 >=2 个假设。"""
    from core.inducer import STOP_GRAMS
    mem, ctx, hyp = stores
    for t in ("考试前一天提醒我", "重要事情提前提醒我",
              "考试提前三天提醒我", "重要事情提前三天提醒我",
              "回答简短些", "简短优先"):
        _interaction(ctx, t)
    r = Inducer(mem, ctx, hyp).induce_all()
    assert len(r.created) >= 2, [h.scope for h in hyp.list_hypotheses()]
    scopes = {h.scope for h in hyp.list_hypotheses()}
    assert len(scopes) >= 2
    # 主题词永不在停用表（否则整个主题隐形）
    for w in ("提醒", "考试", "作业", "三天", "简短", "提前"):
        assert w not in STOP_GRAMS, w


def test_p0_single_theme_stays_one(stores):
    """单主题数据仍只出 1 个（防分裂过度）。"""
    mem, ctx, hyp = stores
    _interaction(ctx, "考试前一天提醒我")
    _interaction(ctx, "重要事情提前提醒我")
    r = Inducer(mem, ctx, hyp).induce_all()
    assert len(r.created) == 1


def test_p0_label_deterministic(stores, tmp_path):
    """同数据在两个独立库跑，scope 一致（排序确定性）。"""
    from core.inducer import Inducer as I2
    texts = ("考试提前三天提醒我", "重要事情提前三天提醒我")
    mem, ctx, hyp = stores
    for t in texts:
        _interaction(ctx, t)
    I2(mem, ctx, hyp).induce_all()
    first = sorted(h.scope for h in hyp.list_hypotheses())
    db2 = tmp_path / "ind2.db"
    mem2 = MemoryStore(db2)
    ctx2 = ContextStore(db2)
    hyp2 = HypothesisStore(db2)
    for t in texts:
        e, _ = ctx2.add_entry("interaction", t, source="chat")
        assert e is not None
    I2(mem2, ctx2, hyp2).induce_all()
    second = sorted(h.scope for h in hyp2.list_hypotheses())
    assert first == second and len(first) == 1


# ---------- 改述复活回归（纠正改写内容 → 不许建双胞胎） ----------

def test_relabel_does_not_revive(stores):
    """纠正改写条目内容 → R2 重打标签也不许建双胞胎（成员重叠去重）。"""
    mem, ctx, hyp = stores
    e1 = _interaction(ctx, "考试前一天提醒我")
    e2 = _interaction(ctx, "重要事情提前提醒我")
    ind = Inducer(mem, ctx, hyp)
    r1 = ind.induce_all()
    assert len(r1.created) == 1
    first_id = r1.created[0]
    # 模拟纠正改写 e2（内容变，id 不变）：新标签与旧 scope 不同
    assert ctx.correct_entry(e2.id, "用户不需要提前提醒", reason="用户否决")
    r2 = ind.induce_all()
    assert r2.created == [], [h.scope for h in hyp.list_hypotheses()]
    assert hyp.count_live() == 1
    assert hyp.get(first_id).status == "tentative"


def test_rejected_members_not_revived(stores):
    """成员归属被否决假设 → 换标签也不复活（尊重否决）。"""
    mem, ctx, hyp = stores
    e1 = _interaction(ctx, "考试前一天提醒我")
    e2 = _interaction(ctx, "重要事情提前提醒我")
    ind = Inducer(mem, ctx, hyp)
    r1 = ind.induce_all()
    hyp.reject(r1.created[0], reason="不需要")
    ctx.correct_entry(e2.id, "用户不需要提前提醒", reason="用户否决")
    r2 = ind.induce_all()
    assert r2.created == []
    assert r2.skipped == r1.created
    assert hyp.count_live() == 0
