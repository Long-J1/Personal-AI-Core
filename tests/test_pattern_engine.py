# V0.5 晋升引擎：confirmed 假设组 → tentative 规律（fixture 库）。
from __future__ import annotations

import pytest

from core.pattern_engine import PatternEngine
from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.pattern_store import PatternStore
from memory.store import MemoryStore


@pytest.fixture
def dbs(tmp_path):
    db = tmp_path / "promo.db"
    return (MemoryStore(db), ContextStore(db), HypothesisStore(db),
            PatternStore(db))


def _ctx_evidence(ctx, hyp, hid, text, origin):
    e, _ = ctx.add_entry("interaction", text, source="chat")
    assert hyp.add_evidence(hid, "support", "context", e.id,
                            origin=origin)[0]
    return e


def _confirmed_pair(mem, ctx, hyp):
    h1, _ = hyp.create("用户接受提前提醒甲", "interaction:提醒")
    _ctx_evidence(ctx, hyp, h1.id, "考试前提醒我甲", "sess:1")
    _ctx_evidence(ctx, hyp, h1.id, "比赛前提醒我甲", "sess:2")
    hyp.confirm(h1.id)
    h2, _ = hyp.create("用户接受提前提醒乙", "interaction:提醒")
    _ctx_evidence(ctx, hyp, h2.id, "开会前提醒我乙", "sess:3")
    hyp.confirm(h2.id)
    return h1, h2


def test_promote_creates_with_mean(dbs):
    mem, ctx, hyp, pat = dbs
    h1, h2 = _confirmed_pair(mem, ctx, hyp)
    created = PatternEngine(mem, hyp, pat).promote_all()
    assert len(created) == 1
    p = pat.get(created[0])
    assert p.status == "tentative"
    assert p.scope == "interaction:提醒"
    assert "次" not in p.statement and "条" not in p.statement  # P2：无计数
    mean = (hyp.get(h1.id).confidence + hyp.get(h2.id).confidence) / 2
    assert p.confidence == pytest.approx(mean)
    d = pat.get_detail(p.id)
    assert {e["source_id"] for e in d["evidence"] if
            e["source_type"] == "hypothesis"} == {h1.id, h2.id}
    assert any(l["action"] == "promote" for l in d["history"])


def test_promote_dedup_and_respect_reject(dbs):
    mem, ctx, hyp, pat = dbs
    _confirmed_pair(mem, ctx, hyp)
    eng = PatternEngine(mem, hyp, pat)
    first = eng.promote_all()
    assert eng.promote_all() == []  # 去重：已有 live 不重建
    assert pat.count_live() == 1
    pat.reject(first[0], reason="不对")
    h3, _ = hyp.create("用户接受提前提醒丙", "interaction:提醒")
    _ctx_evidence(ctx, hyp, h3.id, "上课前提醒我丙", "sess:4")
    hyp.confirm(h3.id)
    assert eng.promote_all() == []  # 尊重否决：不再打扰
    assert pat.count_live() == 0


def test_promote_below_threshold(dbs):
    """1 个 confirmed / 独立源不足 → 不晋升。"""
    mem, ctx, hyp, pat = dbs
    h, _ = hyp.create("单个确认假设", "interaction:提醒")
    _ctx_evidence(ctx, hyp, h.id, "考试前提醒我", "sess:1")
    hyp.confirm(h.id)
    assert PatternEngine(mem, hyp, pat).promote_all() == []
    assert pat.count_live() == 0


def test_promote_skips_paused(dbs):
    mem, ctx, hyp, pat = dbs
    _confirmed_pair(mem, ctx, hyp)
    mem.paused = True
    try:
        assert PatternEngine(mem, hyp, pat).promote_all() == []
    finally:
        mem.paused = False
