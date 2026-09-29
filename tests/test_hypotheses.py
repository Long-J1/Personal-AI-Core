"""V0.3 H1：hypotheses 表 + 数据模型 + 置信度数学。

全部用 tmp 库（fixture 风格沿用 conftest），不碰真实数据库。
"""
from __future__ import annotations

import pytest

from memory.context_store import ContextStore
from memory.event import Event
from memory.hypothesis_store import (
    CORRECTION_SET,
    HIGH_CONF_CAP,
    INIT_CONFIDENCE,
    HypothesisStore,
)
from memory.store import MemoryStore


@pytest.fixture
def hyp(tmp_path):
    return HypothesisStore(tmp_path / "h.db")


@pytest.fixture
def ctx(tmp_path):
    return ContextStore(tmp_path / "h.db")


@pytest.fixture
def mem(tmp_path):
    return MemoryStore(tmp_path / "h.db")


def _ctx_entry(ctx, content="用户希望提前收到考试提醒", kind="interaction"):
    entry, skip = ctx.add_entry(kind, content, source="chat")
    assert entry is not None, skip
    return entry


def _event(mem, activity="写作业", description="用户在写作业"):
    e = Event(source="chat", scene="书房", activity=activity,
              description=description, importance=0.6,
              raw_understanding=None)
    mem.add_event(e)
    return e


def _hypo_with_ctx_support(hyp, ctx, n=1, day_shift=0):
    h, skip = hyp.create("用户可能接受重要事件的提前提醒", "interaction:提醒")
    assert h is not None, skip
    for i in range(n):
        e = _ctx_entry(ctx, f"用户希望提前收到考试提醒{i}")
        ok, reason = hyp.add_evidence(
            h.id, "support", "context", e.id, excerpt=e.content)
        assert ok, reason
    return hyp.get(h.id)


# ---------- 创建与校验 ----------

def test_create_defaults(hyp):
    h, skip = hyp.create("用户可能在深夜更高效", "dynamic:作息")
    assert skip is None
    assert h.confidence == INIT_CONFIDENCE == 0.50
    assert h.status == "tentative"
    assert h.support_count == 0 and h.contradict_count == 0


def test_create_validation(hyp):
    h, reason = hyp.create("", "scope:x")
    assert h is None and reason
    h, reason = hyp.create("陈述", "")
    assert h is None and reason


# ---------- 置信度数学 ----------

def test_support_delta(hyp, ctx):
    h = _hypo_with_ctx_support(hyp, ctx, n=1)
    # 1 个独立源：0.50 + 0.08 = 0.58（门控 cap 0.70 未触发）
    assert h.confidence == pytest.approx(0.58)
    assert h.support_count == 1


def test_gate_cap_single_origin(hyp, ctx):
    """单个独立源：5 条支持也冲不破 0.70。"""
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    for i in range(5):
        e = _ctx_entry(ctx, f"提醒偏好{i}")
        # 同 origin（同一天、同一批发现）→ 只算 1 个独立源
        ok, reason = hyp.add_evidence(
            h.id, "support", "context", e.id, origin="batch:1",
            excerpt=e.content)
        assert ok, reason
    h = hyp.get(h.id)
    assert h.confidence == pytest.approx(HIGH_CONF_CAP)  # 0.90 被 cap 到 0.70
    assert hyp.get_detail(h.id)["independent_sources"] == 1


def test_gate_release_two_origins(hyp, ctx):
    """2 个独立源：允许突破 0.70。"""
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    for i, org in enumerate(["batch:1", "batch:2", "batch:2", "batch:3"]):
        e = _ctx_entry(ctx, f"提醒偏好{i}")
        ok, reason = hyp.add_evidence(
            h.id, "support", "context", e.id, origin=org, excerpt=e.content)
        assert ok, reason
    h = hyp.get(h.id)
    # 0.50 + 4×0.08 = 0.82（3 个独立源，不 cap）
    assert h.confidence == pytest.approx(0.82)


def test_contradict_asymmetric(hyp, ctx):
    """反证权重（-0.25）≈ 3 倍支持（+0.08）。"""
    h = _hypo_with_ctx_support(hyp, ctx, n=2)  # 0.50+0.16=0.66
    base = h.confidence
    e = _ctx_entry(ctx, "用户说这次别提醒")
    ok, reason = hyp.add_evidence(
        h.id, "contradict", "correction", e.id, excerpt="用户纠正")
    assert ok, reason
    h = hyp.get(h.id)
    assert h.confidence == pytest.approx(base - 0.25)
    assert h.contradict_count == 1


def test_clamp_floor_ceiling(hyp, ctx):
    h, _ = hyp.create("陈述", "scope:x")
    e = _ctx_entry(ctx, "反证内容")
    for _ in range(10):
        hyp.add_evidence(h.id, "contradict", "correction", e.id,
                         origin=f"c:{_}", excerpt="x")
        # 同一来源去重后只有第一条生效，其余返回 False；conf 触底验证用新条目
    h = hyp.get(h.id)
    assert h.confidence >= 0.0
    # 上限：造足够多独立支持
    h2, _ = hyp.create("陈述2", "scope:y")
    for i in range(20):
        e2 = _ctx_entry(ctx, f"支持{i}")
        hyp.add_evidence(h2.id, "support", "context", e2.id,
                         origin=f"s:{i}", excerpt="x")
    assert hyp.get(h2.id).confidence == pytest.approx(1.0)


# ---------- 铁律 ----------

def test_hypothesis_cannot_be_evidence(hyp, ctx):
    h, _ = hyp.create("陈述", "scope:x")
    h2, _ = hyp.create("另一条假设", "scope:y")
    ok, reason = hyp.add_evidence(h.id, "support", "hypothesis", h2.id)
    assert ok is False
    assert "假设不能证明自己" in (reason or "")


def test_evidence_must_reference_real_rows(hyp):
    h, _ = hyp.create("陈述", "scope:x")
    ok, _ = hyp.add_evidence(h.id, "support", "context", "deadbeef" * 4)
    assert ok is False
    ok, _ = hyp.add_evidence(h.id, "support", "event", "deadbeef" * 4)
    assert ok is False
    ok, _ = hyp.add_evidence(h.id, "support", "bogus", "x" * 32)
    assert ok is False


def test_duplicate_evidence_rejected(hyp, ctx):
    h = _hypo_with_ctx_support(hyp, ctx, n=1)
    e_id = hyp.get_detail(h.id)["evidence"][0]["source_id"]
    ok, reason = hyp.add_evidence(h.id, "support", "context", e_id)
    assert ok is False and "重复" in (reason or "")


def test_deleted_rejects_evidence(hyp, ctx):
    h = _hypo_with_ctx_support(hyp, ctx, n=1)
    assert hyp.delete_entry(h.id) is True
    e = _ctx_entry(ctx, "新证据")
    ok, _ = hyp.add_evidence(h.id, "support", "context", e.id)
    assert ok is False


# ---------- 用户行为 ----------

def test_reject_sets_fixed_low(hyp, ctx):
    h = _hypo_with_ctx_support(hyp, ctx, n=2)  # 0.66
    assert hyp.reject(h.id, reason="我不需要这种提醒", channel="trust_ui")
    h = hyp.get(h.id)
    assert h.confidence == pytest.approx(CORRECTION_SET) == pytest.approx(0.25)
    assert h.status == "rejected"
    d = hyp.get_detail(h.id)
    assert any(e["polarity"] == "contradict" for e in d["evidence"])
    assert any(l["action"] == "reject" for l in d["history"])


def test_confirm_keeps_confidence(hyp, ctx):
    """确认只改状态，不动 confidence（两者不是一回事）。"""
    h = _hypo_with_ctx_support(hyp, ctx, n=2)
    before = h.confidence
    assert hyp.confirm(h.id) is True
    h = hyp.get(h.id)
    assert h.status == "confirmed"
    assert h.confidence == pytest.approx(before)


def test_correction_hit_soft(hyp, ctx):
    """软反证：被纠正的条目是证据 → -0.25，状态不动。"""
    h = _hypo_with_ctx_support(hyp, ctx, n=2)
    entry_id = hyp.get_detail(h.id)["evidence"][0]["source_id"]
    affected = hyp.record_correction_hit(
        entry_id, "补充一下，上次那个时间记错了", reason="时间纠正")
    assert affected == [h.id]
    h = hyp.get(h.id)
    assert h.status == "tentative"
    assert h.confidence == pytest.approx(0.66 - 0.25)


def test_correction_hit_explicit_denial(hyp, ctx):
    """显式否决：含否决词 → 置 0.25 + rejected。"""
    h = _hypo_with_ctx_support(hyp, ctx, n=2)
    entry_id = hyp.get_detail(h.id)["evidence"][0]["source_id"]
    affected = hyp.record_correction_hit(
        entry_id, "不对，我一般不需要这种提醒", reason="用户否决")
    assert affected == [h.id]
    h = hyp.get(h.id)
    assert h.status == "rejected"
    assert h.confidence == pytest.approx(0.25)


def test_no_resurrection_after_reject(hyp, ctx):
    """被否决后，新增支持只能从 0.25 慢慢爬，不许靠旧证据复活。"""
    h = _hypo_with_ctx_support(hyp, ctx, n=2)  # 0.66
    assert hyp.reject(h.id, reason="不需要")
    e = _ctx_entry(ctx, "之后又提了一次提醒偏好")
    ok, _ = hyp.add_evidence(h.id, "support", "context", e.id,
                             origin="batch:new", excerpt=e.content)
    assert ok
    assert hyp.get(h.id).confidence == pytest.approx(0.25 + 0.08)


# ---------- 可见性 / 生命周期 ----------

def test_list_for_prompt_filters(hyp, ctx):
    h1 = _hypo_with_ctx_support(hyp, ctx, n=1)
    h2, _ = hyp.create("待确认的假设", "scope:b")
    hyp.confirm(h2.id)
    h3, _ = hyp.create("被否决的假设", "scope:c")
    hyp.reject(h3.id, reason="不")
    h4, _ = hyp.create("已删除的假设", "scope:d")
    hyp.delete_entry(h4.id)
    ids = [h.id for h in hyp.list_for_prompt(limit=10)]
    assert h1.id in ids and h2.id in ids
    assert h3.id not in ids and h4.id not in ids


def test_delete_restore(hyp):
    h, _ = hyp.create("陈述", "scope:x")
    assert hyp.delete_entry(h.id) is True
    assert hyp.get(h.id).status == "deleted"
    assert hyp.restore_entry(h.id) is True
    assert hyp.get(h.id).status == "tentative"


def test_detail_traceability(hyp, ctx, mem):
    """证据能说出到底是哪一行支持的，历史能说出置信度怎么变的。"""
    h, _ = hyp.create("用户近期多次写作业，可能处在学习持续期", "activity:写作业")
    ev = _event(mem, activity="写作业", description="用户在写数学作业")
    ok, reason = hyp.add_evidence(h.id, "support", "event", ev.id,
                                  excerpt=ev.description)
    assert ok, reason
    d = hyp.get_detail(h.id)
    assert d["evidence"][0]["source_id"] == ev.id
    assert d["evidence"][0]["source_type"] == "event"
    assert d["independent_sources"] == 1
    actions = [l["action"] for l in d["history"]]
    assert actions[0] == "create" and "support" in actions
    confs = [(l["conf_before"], l["conf_after"]) for l in d["history"]]
    assert confs[0] == (None, 0.50)


def test_mark_used(hyp):
    h, _ = hyp.create("陈述", "scope:x")
    hyp.mark_used([h.id])
    assert hyp.get(h.id).used_count == 1


def test_deny_word_boundaries(hyp, ctx):
    """否决词边界：疑问/自述不算否决，真否决才升级。"""
    from memory.hypothesis_store import _is_explicit_denial
    assert _is_explicit_denial("不对，我一般不需要这种提醒") is True
    assert _is_explicit_denial("你错了") is True
    assert _is_explicit_denial("没有这回事") is True
    assert _is_explicit_denial("补充一下，上次那个时间记错了") is False
    assert _is_explicit_denial("是不是应该提前提醒？") is False
    assert _is_explicit_denial("有没有新的提醒？") is False
    # 端到端：自述笔误走软反证，状态不动
    h = _hypo_with_ctx_support(hyp, ctx, n=1)
    entry_id = hyp.get_detail(h.id)["evidence"][0]["source_id"]
    hyp.record_correction_hit(entry_id, "是不是记错了时间？")
    assert hyp.get(h.id).status == "tentative"


# ---------- bugfix 回归（2026-09-27 扫 bug） ----------

def test_origin_uses_source_row_date(hyp, ctx):
    """默认 origin 取来源行的发生日，不取写入日."""
    import sqlite3
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    e1 = _ctx_entry(ctx, "提醒偏好甲")
    e2 = _ctx_entry(ctx, "提醒偏好乙")
    # 把 e2 的创建时间拨到昨天（模拟不同天产生的两条理解）
    con = sqlite3.connect(str(ctx.db_path))
    con.execute(
        "UPDATE context_entries SET created_at_iso='2026-01-01T10:00:00+08:00'"
        " WHERE id=?", (e2.id,))
    con.commit()
    con.close()
    assert hyp.add_evidence(h.id, "support", "context", e1.id)[0]
    assert hyp.add_evidence(h.id, "support", "context", e2.id)[0]
    assert hyp.get_detail(h.id)["independent_sources"] == 2


def test_reject_idempotent(hyp, ctx):
    """重复否决不刷证据数（防双击膨胀）."""
    h = _hypo_with_ctx_support(hyp, ctx, n=1)
    assert hyp.reject(h.id, reason="不需要") is True
    assert hyp.reject(h.id, reason="还是不需要") is True
    h = hyp.get(h.id)
    assert h.contradict_count == 1
    assert h.confidence == pytest.approx(0.25)


def test_scope_truncation_dedup(hyp):
    """超长 scope 去重不 miss（写入与查找同一切断）."""
    long_scope = "activity:" + "写" * 200
    h, _ = hyp.create("长活动假设", long_scope)
    found = hyp.find_by_scope(long_scope)
    assert [x.id for x in found] == [h.id]


def test_bie_stripped_in_non_denials(hyp, ctx):
    """'别'只在真否决时生效：特别/别人不算，别提醒算."""
    from memory.hypothesis_store import _is_explicit_denial
    assert _is_explicit_denial("这很特别") is False
    assert _is_explicit_denial("别人也是这样") is False
    assert _is_explicit_denial("别再提醒我了") is True


def test_export_and_clear_all(hyp, ctx):
    h = _hypo_with_ctx_support(hyp, ctx, n=1)
    data = hyp.export_all()
    assert data["counts"]["hypotheses"] == 1
    assert data["hypotheses"][0]["id"] == h.id
    assert len(data["evidence"]) == 1
    assert hyp.clear_all() == 1
    assert hyp.count_live() == 0
    assert hyp.get_detail(h.id) is None


def test_support_coverage(stores):
    """support_coverage：{hypo_id: (status, {(type, id)})} 单查询。"""
    mem, ctx, hyp = stores
    e1 = _interaction(ctx, "考试前一天提醒我")
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    assert hyp.support_coverage() == {}
    hyp.add_evidence(h.id, "support", "context", e1.id)
    cov = hyp.support_coverage()
    assert cov == {h.id: ("tentative", {("context", e1.id)})}
    hyp.reject(h.id, reason="不需要")
    cov = hyp.support_coverage()
    assert cov[h.id][0] == "rejected"


def test_support_coverage(hyp, ctx):
    """support_coverage：{hypo_id: (status, {(type, id)})} 单查询。"""
    h, _ = hyp.create("用户可能接受提前提醒", "interaction:提醒")
    assert hyp.support_coverage() == {}
    e1 = _ctx_entry(ctx, "考试前一天提醒我")
    hyp.add_evidence(h.id, "support", "context", e1.id)
    cov = hyp.support_coverage()
    assert cov == {h.id: ("tentative", {("context", e1.id)})}
    hyp.reject(h.id, reason="不需要")
    cov = hyp.support_coverage()
    assert cov[h.id][0] == "rejected"
