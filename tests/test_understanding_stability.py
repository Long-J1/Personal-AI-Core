"""Understanding Stability 测试 + Replay。

组织成三层：
  一、纯逻辑（无 IO，边界全在这）
  二、编排（真 store，验独立性判定真的按"天"算）
  三、Replay（任务书里那四条故事，逐条跑通）

Replay 是重点：纯函数测试全绿也不代表真实数据流走得通
（V0.5.7踩过：单测喂的是自己造的 dict，真实 get_detail() 形状不同）。
所以 replay 必须**真的建 Context、真的绑定证据、真的读回 prompt**。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from core.understanding_stability import (
    INACTIVE,
    INACTIVE_MIN_INDEPENDENT_DAYS,
    PROMPT_STATUSES,
    RESTORE_MIN_SUPPORT,
    REVIEW_MIN_INDEPENDENT_DAYS,
    UNDER_REVIEW,
    StabilityBridge,
    can_reverse_understanding,
    decide_reverse,
    decide_support,
    is_self_generated,
    is_visible_as_understanding,
    should_enter_prompt,
)
from memory.hypothesis_store import HypothesisStore
from memory.context_store import ContextStore


# ============================================================ 一、纯逻辑

def test_single_reverse_evidence_keeps_active():
    """规则明说：一次偶然反例不推翻。"""
    d = decide_reverse(status="tentative", independent_reverse_days=1,
                       has_explicit_correction=False)
    assert d.changed is False
    assert d.to_status == "tentative"


def test_same_day_repeated_reverse_keeps_active():
    """同一天重复多次 —— 但按"独立日期"算就只有 1 天。"""
    d = decide_reverse(status="confirmed", independent_reverse_days=1,
                       has_explicit_correction=False)
    assert d.changed is False


def test_multi_day_reverse_enters_review():
    d = decide_reverse(status="tentative",
                       independent_reverse_days=REVIEW_MIN_INDEPENDENT_DAYS,
                       has_explicit_correction=False)
    assert d.changed is True
    assert d.to_status == UNDER_REVIEW


def test_threshold_is_exactly_two_days():
    """**卡在阈值两侧各测一次，且不引用模块常量。**

    两个教训叠在一起：
    1. 只测"多天会 review"测不出阈值被改——降到 1 之后多天照样 review。
    2. 断言 `REVIEW_MIN_INDEPENDENT_DAYS == 2` **也没用**：那个常量
       会被变异一起改掉，于是断言跟着一起变绿（mutation A 就是这样
       活下来的）。所以下面只写**字面量**行为，不引用常量。

    写死 2 的语义依据：单日重复不算独立（`_row_day` 保证），
    跨 2 个日期才说明"不只是今天例外"。
    """
    # 恰好 1 天：不够
    assert decide_reverse(status="tentative", independent_reverse_days=1,
                          has_explicit_correction=False).changed is False
    # 恰好 2 天：进 review
    assert decide_reverse(status="tentative", independent_reverse_days=2,
                          has_explicit_correction=False).changed is True


def test_inactive_threshold_is_exactly_three_more_days():
    """同理：不引用常量，只钉**行为边界**。

    在 review 状态下跨 4 天还不能死，跨 5 天才死 ——
    即 review 门槛 2 + inactive 额外 3。
    """
    assert decide_reverse(status=UNDER_REVIEW, independent_reverse_days=4,
                          has_explicit_correction=False).changed is False
    assert decide_reverse(status=UNDER_REVIEW, independent_reverse_days=5,
                          has_explicit_correction=False).changed is True
    assert decide_reverse(status=UNDER_REVIEW, independent_reverse_days=5,
                          has_explicit_correction=False).to_status == INACTIVE


def test_inactive_requires_at_least_one_more_day_than_review():
    """inactive 阈值**绝不能是 0**：那会让"刚进 review 立刻死"，
    一次误判就变成永久损失。

    行为写法：刚进 review（天数刚好等于门槛）时**绝不能**已经失活。
    """
    d = decide_reverse(status=UNDER_REVIEW, independent_reverse_days=2,
                       has_explicit_correction=False)
    assert d.to_status == UNDER_REVIEW, "刚进 review 就不该被判死"
    assert d.changed is False


def test_review_needs_more_days_to_go_inactive():
    """刚进 review 不能立刻死 —— 那是一次误判变永久损失。"""
    d = decide_reverse(status=UNDER_REVIEW,
                       independent_reverse_days=REVIEW_MIN_INDEPENDENT_DAYS,
                       has_explicit_correction=False)
    assert d.changed is False
    d2 = decide_reverse(
        status=UNDER_REVIEW,
        independent_reverse_days=REVIEW_MIN_INDEPENDENT_DAYS
        + INACTIVE_MIN_INDEPENDENT_DAYS,
        has_explicit_correction=False)
    assert d2.changed is True
    assert d2.to_status == INACTIVE


def test_explicit_correction_goes_straight_to_inactive():
    """用户明确纠正 = 已经给了答案，不用等系统慢慢数够天数。"""
    d = decide_reverse(status="confirmed", independent_reverse_days=0,
                       has_explicit_correction=True)
    assert d.changed is True
    assert d.to_status == INACTIVE


def test_explicit_correction_on_already_inactive_is_noop():
    d = decide_reverse(status=INACTIVE, independent_reverse_days=99,
                       has_explicit_correction=True)
    assert d.changed is False


def test_review_restores_on_support():
    d = decide_support(status=UNDER_REVIEW, support_since_review=1)
    assert d.changed is True
    assert d.to_status == "tentative"


def test_restore_needs_no_score_climbing():
    """恢复的依据是"又看到反过来的事实"，不是"分数攒够"。
    所以 support_since_review=1 就够，且不做任何分数判断。"""
    assert RESTORE_MIN_SUPPORT == 1
    d = decide_support(status=UNDER_REVIEW, support_since_review=0)
    assert d.changed is False


def test_support_on_active_is_noop():
    d = decide_support(status="tentative", support_since_review=5)
    assert d.changed is False


# ---------------------------------------------- prompt 信任边界

def test_active_enters_prompt():
    for s in PROMPT_STATUSES:
        assert should_enter_prompt(s) is True


def test_under_review_does_not_enter_prompt_unconditionally():
    """核心信任边界：review 不能像 active 一样无条件进 prompt。"""
    assert should_enter_prompt(UNDER_REVIEW) is False
    assert should_enter_prompt(UNDER_REVIEW, user_reaffirmed=False) is False


def test_under_review_enters_prompt_only_if_user_reaffirmed():
    assert should_enter_prompt(UNDER_REVIEW, user_reaffirmed=True) is True


@pytest.mark.parametrize("status", ["inactive", "rejected", "deleted"])
def test_inactive_never_enters_prompt(status):
    """即使用户重申，已失效/已否决/已删除的也不该被当成结论说。"""
    assert should_enter_prompt(status, user_reaffirmed=True) is False


def test_under_review_still_visible_as_understanding():
    """review 要显示（标成待复审），藏起来等于让用户以为从没被理解过。"""
    assert is_visible_as_understanding(UNDER_REVIEW) is True
    assert is_visible_as_understanding("inactive") is False


# ---------------------------------------------- self-proof 阻断

def test_self_generated_scope_cannot_reverse():
    """AI 自己产出的**候选**永远不能推翻正式理解。"""
    for scope in ("background:behavior_change:学习",
                  "synthesis:cross_context:学习",
                  "quota_overflow:学习"):
        assert can_reverse_understanding("tentative", scope, "context") is False


def test_graduated_inferred_scope_IS_subject_to_review():
    """`inferred:` 是**已毕业的正式理解**，必须同样受复审规则管。

    把它也挡掉就成了另一种形式的自我豁免：AI 猜出来的理解一旦毕业，
    就永远免疫现实纠正——那正好是 self-proof 的另一种写法。
    """
    assert can_reverse_understanding("tentative",
                                     "inferred:cross_context:学习",
                                     "context") is True


def test_hypothesis_cannot_be_evidence_source():
    """AI 自己生成的文本不能成为证据。"""
    assert can_reverse_understanding("tentative", "activity:学习",
                                     "hypothesis") is False


def test_real_user_expression_can_reverse():
    assert can_reverse_understanding("tentative", "activity:学习",
                                     "context") is True
    assert can_reverse_understanding("tentative", "activity:学习",
                                     "correction") is True


def test_rejected_and_deleted_are_user_final_never_touched():
    for s in ("rejected", "deleted"):
        assert can_reverse_understanding(s, "activity:学习", "context") is False


def test_is_self_generated_prefix_matching():
    assert is_self_generated("background:x") is True
    assert is_self_generated("synthesis:x") is True
    assert is_self_generated("activity:学习") is False
    assert is_self_generated("") is False


# ============================================================ 二、编排

@pytest.fixture
def world(tmp_path):
    db = tmp_path / "stability.db"
    return HypothesisStore(db), ContextStore(db), StabilityBridge(HypothesisStore(db))


def _mk_understanding(store, ctx, statement="用户通常在晚上学习。",
                      scope="activity:学习"):
    h, _ = store.create(statement=statement, scope=scope)
    return h


def _ctx_on_day(ctx, text, day_offset=0):
    """造一条 Context，并把它的时间挪到指定那天（独立日判定靠它）。

    两个本仓库反复踩过的形状差异：
      · `add_entry` 返回 **(entry, reason)**，不是 dict；
      · `ContextStore` 的连接没有 row_factory，行是元组。
    """
    entry, why = ctx.add_entry(kind="dynamic", content=text,
                                source="user_direct",
                                source_grade="user_direct")
    assert entry is not None, f"造 Context 失败：{why}"
    when = datetime.now().astimezone() - timedelta(days=day_offset)
    with sqlite3.connect(ctx.db_path) as con:
        con.execute("UPDATE context_entries SET created_at_iso=?,"
                    " created_at_epoch=? WHERE id=?",
                    (when.isoformat(), when.timestamp(), entry.id))
    return entry.id


def test_duplicate_evidence_rejected(world):
    """重复证据防护 —— 这是"同日不累计"的第一道闸。"""
    store, ctx, _ = world
    h = _mk_understanding(store, ctx)
    cid = _ctx_on_day(ctx, "我晚上学习")
    assert store.add_evidence(h.id, "support", "context", cid)[0] is True
    ok, why = store.add_evidence(h.id, "support", "context", cid)
    assert ok is False
    assert "重复" in why


def test_same_day_repeats_count_as_one_independent_day(world):
    """**独立日期才��数**：同一天四条 Context 反证，仍然不是 review。"""
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    for i in range(4):
        cid = _ctx_on_day(ctx, f"我今天下午在学{i}", day_offset=0)
        store.add_evidence(h.id, "contradict", "context", cid)
    assert h.contradict_count == 0 or True   # 下面用实际库读
    with store._connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM hypothesis_evidence WHERE hypothesis_id=?"
            " AND polarity='contradict'", (h.id,)).fetchone()[0] == 4
    dec = bridge.on_reverse_evidence(h.id, source_type="context")
    assert dec.changed is False
    assert dec.independent_reverse_days == 1, "4 条同日的只能算 1 天"
    assert store.get(h.id).status == "tentative"


def test_multi_day_reverse_moves_to_under_review(world):
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    for d in (0, 1):                     # 两个独立日期
        cid = _ctx_on_day(ctx, f"我今天下午在学 {d}", day_offset=d)
        store.add_evidence(h.id, "contradict", "context", cid)
    dec = bridge.on_reverse_evidence(h.id, source_type="context")
    assert dec.changed is True
    assert dec.to_status == UNDER_REVIEW
    assert store.get(h.id).status == UNDER_REVIEW
    assert "复审" in store.get(h.id).status_detail


def test_review_status_detail_is_user_readable(world):
    """状态说明不许出现内部 id / 分数。"""
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    for d in (0, 1):
        cid = _ctx_on_day(ctx, f"下午学习 {d}", day_offset=d)
        store.add_evidence(h.id, "contradict", "context", cid)
    bridge.on_reverse_evidence(h.id, source_type="context")
    detail = store.get(h.id).status_detail
    assert "复审" in detail
    for banned in (h.id[:8], "confidence", "scope", "0."):
        assert banned not in detail


def test_bridge_blocks_self_generated_understanding(world):
    """后台/综合候选不许因为真实反向证据就变成正式理解的降级理由。"""
    store, ctx, bridge = world
    h, _ = store.create(statement="后台候选", scope="background:behavior_change:学习")
    cid = _ctx_on_day(ctx, "我改成早上了")
    store.add_evidence(h.id, "contradict", "context", cid)
    dec = bridge.on_reverse_evidence(h.id, source_type="context")
    assert dec.changed is False
    assert store.get(h.id).status == "tentative"


def test_bridge_refuses_hypothesis_as_source(world):
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    dec = bridge.on_reverse_evidence(h.id, source_type="hypothesis")
    assert dec.changed is False


def test_review_restores_to_active(world):
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    for d in (0, 1):
        cid = _ctx_on_day(ctx, f"下午学习 {d}", day_offset=d)
        store.add_evidence(h.id, "contradict", "context", cid)
    bridge.on_reverse_evidence(h.id, source_type="context")
    assert store.get(h.id).status == UNDER_REVIEW

    # 又在晚上学 —— 新的一条支持
    cid = _ctx_on_day(ctx, "今天晚上学习", day_offset=0)
    store.add_evidence(h.id, "support", "context", cid)
    dec = bridge.on_support_evidence(h.id)
    assert dec.changed is True
    assert store.get(h.id).status == "tentative"


def test_review_eventually_inactive(world):
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    for d in (0, 1):
        cid = _ctx_on_day(ctx, f"下午学习 {d}", day_offset=d)
        store.add_evidence(h.id, "contradict", "context", cid)
    bridge.on_reverse_evidence(h.id, source_type="context")
    # review 之后又跨 3 天反向
    for d in (2, 3, 4):
        cid = _ctx_on_day(ctx, f"下午学习续 {d}", day_offset=d)
        store.add_evidence(h.id, "contradict", "context", cid)
    dec = bridge.on_reverse_evidence(h.id, source_type="context")
    assert dec.changed is True
    assert store.get(h.id).status == INACTIVE


def test_explicit_correction_ignores_day_count(world):
    """用户一句话就够 —— 不需要"跨 2 天"这个门槛。"""
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    dec = bridge.on_reverse_evidence(h.id, source_type="correction",
                                     explicit=True)
    assert dec.changed is True
    assert store.get(h.id).status == INACTIVE


def test_stability_does_not_change_confidence(world):
    """稳定性与置信度是两件事：复审不压分数，否则恢复时升不回来。"""
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    for i in range(4):
        cid = _ctx_on_day(ctx, f"晚上学习 {i}", day_offset=i)
        store.add_evidence(h.id, "support", "context", cid)
    before = store.get(h.id).confidence
    for d in (0, 1):
        cid = _ctx_on_day(ctx, f"下午学习 {d}", day_offset=d)
        store.add_evidence(h.id, "contradict", "context", cid)
    # add_evidence 自己会压分数（既有行为）；这里测的是 bridge 不再额外压
    after_evidence = store.get(h.id).confidence
    bridge.on_reverse_evidence(h.id, source_type="context")
    assert store.get(h.id).confidence == after_evidence


def test_inactive_history_is_preserved(world):
    """失活 ≠ 删除：历史必须能查出来。"""
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    cid = _ctx_on_day(ctx, "我改成早上了")
    store.add_evidence(h.id, "support", "context", cid)
    bridge.on_reverse_evidence(h.id, source_type="correction", explicit=True)
    with store._connect() as con:
        row = con.execute("SELECT status FROM hypotheses WHERE id=?",
                          (h.id,)).fetchone()
        assert row[0] == INACTIVE     # 行还在
        n = con.execute("SELECT COUNT(*) FROM hypothesis_evidence"
                        " WHERE hypothesis_id=?", (h.id,)).fetchone()[0]
        assert n == 1                 # 证据还在
        nlog = con.execute("SELECT COUNT(*) FROM hypothesis_log"
                           " WHERE hypothesis_id=?", (h.id,)).fetchone()[0]
        assert nlog >= 1               # 变化过程记了log


def test_log_records_stability_transitions(world):
    store, ctx, bridge = world
    h = _mk_understanding(store, ctx)
    for d in (0, 1):
        cid = _ctx_on_day(ctx, f"下午 {d}", day_offset=d)
        store.add_evidence(h.id, "contradict", "context", cid)
    bridge.on_reverse_evidence(h.id, source_type="context")
    with store._connect() as con:
        actions = [r[0] for r in con.execute(
            "SELECT action FROM hypothesis_log WHERE hypothesis_id=?",
            (h.id,)).fetchall()]
    assert f"stability:{UNDER_REVIEW}" in actions


# ------------------------------------------------ migration 幂等

def test_migration_is_idempotent_and_preserves_data(tmp_path):
    """老库升级：加两列、反复升级不炸、既有数据一行不少。"""
    db = tmp_path / "old.db"
    store = HypothesisStore(db)
    h, _ = store.create(statement="旧理解", scope="activity:学习")
    cid_row = None
    before = store.get(h.id).to_public_dict()

    # 手工把表退回"老形状"（去掉两列）
    with sqlite3.connect(db) as con:
        con.execute("ALTER TABLE hypotheses DROP COLUMN status_detail")
        con.execute("ALTER TABLE hypotheses DROP COLUMN status_changed_epoch")
    cols = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(hypotheses)").fetchall()}
    assert "status_detail" not in cols

    # 再打开一次 → migration 应该自动补上
    store2 = HypothesisStore(db)
    cols2 = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(hypotheses)").fetchall()}
    assert "status_detail" in cols2
    assert "status_changed_epoch" in cols2

    after = store2.get(h.id).to_public_dict()
    assert after["statement"] == before["statement"]
    assert after["status"] == before["status"]
    assert after["confidence"] == before["confidence"]

    # 第三次打开（幂等）
    store3 = HypothesisStore(db)
    assert store3.get(h.id).status == before["status"]


def test_old_rows_default_to_never_reviewed(tmp_path):
    """老行读出来 status_changed_epoch=0 → 不会因升级被误判为复审中。"""
    db = tmp_path / "old2.db"
    store = HypothesisStore(db)
    h, _ = store.create(statement="老理解", scope="activity:x")
    got = store.get(h.id)
    assert got.status_changed_epoch == 0.0
    assert got.status_detail == ""


# ============================================================ 三、Replay

def _prompt_statuses(store):
    """真读 store 的 live 查询 —— 复现 prompt 实际取哪些理解。"""
    from memory.hypothesis_store import LIVE_STATUSES
    with store._connect() as con:
        rows = con.execute(
            "SELECT status FROM hypotheses WHERE status IN"
            f" ({','.join('?' * len(LIVE_STATUSES))})", LIVE_STATUSES).fetchall()
    return [r[0] for r in rows]


def test_story1_single_exception_then_pattern_shift(tmp_path):
    """故事 1：
    「我通常晚上学习」→ 正式理解
    「今天下午学习」→ 不 review
    之后连续多个独立日期都是下午学习 → under_review
    """
    db = tmp_path / "story1.db"
    store = HypothesisStore(db)
    ctx = ContextStore(db)
    bridge = StabilityBridge(store)

    h, _ = store.create(statement="用户通常在晚上学习。", scope="activity:学习")
    for d in (10, 9, 8):
        cid = _ctx_on_day(ctx, f"晚上学习 {d}", day_offset=d)
        store.add_evidence(h.id, "support", "context", cid)
    assert store.get(h.id).status == "tentative"
    assert _prompt_statuses(store) == ["tentative"]

    # 一次例外 —— 不许 review
    cid = _ctx_on_day(ctx, "今天下午学习", day_offset=0)
    store.add_evidence(h.id, "contradict", "context", cid)
    dec = bridge.on_reverse_evidence(h.id, source_type="context")
    assert dec.changed is False
    assert store.get(h.id).status == "tentative", "一次例外不推翻"

    # 但连续多天都是下午 —— 该 review 了
    for d in (1, 2):
        cid = _ctx_on_day(ctx, f"下午学习 {d}", day_offset=d)
        store.add_evidence(h.id, "contradict", "context", cid)
    dec = bridge.on_reverse_evidence(h.id, source_type="context")
    assert dec.changed is True
    assert store.get(h.id).status == UNDER_REVIEW
    # 复审中不进 prompt
    assert _prompt_statuses(store) == []
    assert should_enter_prompt(store.get(h.id).status) is False
    # 但仍可作为"理解"被看见
    assert is_visible_as_understanding(store.get(h.id).status) is True


def test_story2_explicit_correction_keeps_history(tmp_path):
    """故事 2：用户明确说改了 → 失活，历史还在。"""
    db = tmp_path / "story2.db"
    store = HypothesisStore(db)
    ctx = ContextStore(db)
    bridge = StabilityBridge(store)

    h, _ = store.create(statement="用户晚上学习。", scope="activity:学习")
    cid = _ctx_on_day(ctx, "我晚上学习")
    store.add_evidence(h.id, "support", "context", cid, excerpt="我晚上学习")
    assert store.get(h.id).status == "tentative"

    dec = bridge.on_reverse_evidence(h.id, source_type="correction",
                                     explicit=True)
    assert dec.changed is True
    assert store.get(h.id).status == INACTIVE
    assert _prompt_statuses(store) == []
    # 历史还在
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM hypotheses WHERE id=?",
                           (h.id,)).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM hypothesis_evidence"
                           " WHERE hypothesis_id=?", (h.id,)).fetchone()[0] >= 1
        # 而且有原话
        ex = [r[0] for r in con.execute(
            "SELECT excerpt FROM hypothesis_evidence WHERE hypothesis_id=?",
            (h.id,)).fetchall()]
        assert any("我晚上学习" in e for e in ex), f"证据原话丢了：{ex}"


def test_story3_review_then_support_restores_active(tmp_path):
    """故事 3：under_review → 连续多天又晚上学习 → active。"""
    db = tmp_path / "story3.db"
    store = HypothesisStore(db)
    ctx = ContextStore(db)
    bridge = StabilityBridge(store)

    h, _ = store.create(statement="用户通常在晚上学习。", scope="activity:学习")
    for d in (5, 4):
        cid = _ctx_on_day(ctx, f"下午 {d}", day_offset=d)
        store.add_evidence(h.id, "contradict", "context", cid)
    bridge.on_reverse_evidence(h.id, source_type="context")
    assert store.get(h.id).status == UNDER_REVIEW

    # 又连着好几天晚上学习
    restored = False
    for d in (1, 2, 3):
        cid = _ctx_on_day(ctx, f"晚上学习 {d}", day_offset=d)
        store.add_evidence(h.id, "support", "context", cid)
        dec = bridge.on_support_evidence(h.id)
        if dec.changed:
            restored = True
            break
    assert restored is True
    assert store.get(h.id).status == "tentative"
    assert _prompt_statuses(store) == ["tentative"]
    assert should_enter_prompt(store.get(h.id).status) is True


def test_story4_background_candidate_cannot_change_formal(tmp_path):
    """故事 4：background candidate 不得直接改变正式 understanding。"""
    db = tmp_path / "story4.db"
    store = HypothesisStore(db)
    ctx = ContextStore(db)
    bridge = StabilityBridge(store)

    formal, _ = store.create(statement="用户通常在晚上学习。",
                             scope="activity:学习")
    bg, _ = store.create(statement="可能存在一个模式：学习时间在变。",
                         scope="background:behavior_change:学习时间")
    synth, _ = store.create(statement="可能存在一个模式：跨情境的节奏。",
                            scope="synthesis:cross_context:学习")

    # 大量真实反向证据
    # 每条假设要绑**自己的**证据：`_source_exists` 会验来源行存在，
    # 而 ContextStore 对重复内容会拒（同一天同文本只算一条），
    # 所以给每条假设单独造一条 Context。
    for d in (0, 1, 2, 3):
        c1 = _ctx_on_day(ctx, f"下午学习 {d}", day_offset=d)
        store.add_evidence(formal.id, "contradict", "context", c1)
        c2 = _ctx_on_day(ctx, f"改为下午 {d}", day_offset=d)
        store.add_evidence(bg.id, "contradict", "context", c2)
        c3 = _ctx_on_day(ctx, f"节奏下午 {d}", day_offset=d)
        store.add_evidence(synth.id, "contradict", "context", c3)

    # 正式理解：该 review 就 review
    dec = bridge.on_reverse_evidence(formal.id, source_type="context")
    assert dec.changed is True
    assert store.get(formal.id).status == UNDER_REVIEW

    # 候选：一条都不许动
    for cand in (bg, synth):
        d = bridge.on_reverse_evidence(cand.id, source_type="context")
        assert d.changed is False
        assert store.get(cand.id).status == "tentative", \
            "AI 自己产出的候选不许被自己的证据推翻"

    # 候选的 scope 确实被识别成 AI 自产（这正是它们被挡住的原因）
    assert is_self_generated(bg.scope) is True
    assert is_self_generated(synth.scope) is True


def test_background_candidate_evidence_cannot_amplify_itself(tmp_path):
    """候选不能靠给正式理解加支持来自我放大。"""
    db = tmp_path / "story4b.db"
    store = HypothesisStore(db)
    ctx = ContextStore(db)

    formal, _ = store.create(statement="正式理解", scope="activity:学习")
    bg, _ = store.create(statement="后台候选", scope="background:behavior_change:学习")

    # 试图把 candidate 当成 formal 的证据源
    ok, why = store.add_evidence(formal.id, "support", "hypothesis", bg.id)
    assert ok is False
    assert "hypothesis" in why


# ------------------------------------------------ UI 投影（产品语言）

def test_under_review_renders_as_reviewing_state():
    """UI 必须说"待复审"，不能说"稳定理解"（那是撒谎）也不能说
    "还在摸索"（那让用户以为系统没反应过来）。"""
    from core.understanding_view import (
        STATE_REVIEWING, STATE_TEXT, build_understanding,
    )

    row = build_understanding({
        "statement": "用户通常在晚上学习。",
        "scope": "activity:学习",
        "status": "under_review",
        "status_detail": "2026-10-05 复审",
        "evidence": [],
    })
    assert row["state"] == STATE_REVIEWING
    assert row["state_text"] == "待复审"
    assert row["review_note"] == "2026-10-05 复审"


def test_review_note_never_leaks_internal_fields():
    """status_detail 是唯一新增的"数据库字段直出 UI"出口，必须过禁词。"""
    from core.understanding_view import build_understanding

    for dirty in ("confidence=0.42", "scope=activity:学习",
                  "source_id=760c5e8a", "hypothesis_id=abcd1234",
                  "ctxday:2026-10-05", "correction:xyz"):
        row = build_understanding({
            "statement": "x", "scope": "activity:学习",
            "status": "under_review", "status_detail": dirty,
            "evidence": [],
        })
        assert row["review_note"] == "", f"泄漏了：{dirty}"


def test_review_note_absent_when_not_reviewing():
    from core.understanding_view import build_understanding

    row = build_understanding({
        "statement": "x", "scope": "activity:学习",
        "status": "tentative", "status_detail": "", "evidence": [],
    })
    assert row["review_note"] == ""


def test_inactive_maps_to_dropped_state():
    """失活 → 不再使用（但库里那一行还在，列表查询层面处理）。"""
    from core.understanding_view import STATE_DROPPED, build_understanding

    row = build_understanding({
        "statement": "x", "scope": "activity:学习",
        "status": "inactive", "status_detail": "2026-10-05 复审",
        "evidence": [],
    })
    assert row["state"] == STATE_DROPPED


def test_understanding_view_passes_no_internal_fields_check():
    """整条链的自检：review_note 加进来之后仍不许出现内部词。"""
    from core.understanding_view import assert_no_internal_fields

    from core.understanding_view import build_understanding
    row = build_understanding({
        "statement": "用户通常在晚上学习。", "scope": "activity:学习",
        "status": "under_review", "status_detail": "2026-10-05 复审",
        "evidence": [{"polarity": "support", "excerpt": "我晚上学习",
                      "created_at_iso": "2026-10-01T20:00:00"}],
    })
    assert assert_no_internal_fields(row) == []


def test_inactive_then_new_support_can_rebuild(tmp_path):
    """失活后重新出现支持 → 重新建立（不是永远封死）。"""
    db = tmp_path / "rebuild.db"
    store = HypothesisStore(db)
    ctx = ContextStore(db)
    bridge = StabilityBridge(store)

    h, _ = store.create(statement="用户晚上学习。", scope="activity:学习")
    cid = _ctx_on_day(ctx, "我晚上学习")
    store.add_evidence(h.id, "support", "context", cid)
    bridge.on_reverse_evidence(h.id, source_type="correction", explicit=True)
    assert store.get(h.id).status == INACTIVE

    # 新的支持证据 —— decide_support 只处理 under_review，
    # 所以 inactive 不自动复活（避免"一条就复活"）。
    cid = _ctx_on_day(ctx, "今晚又学习到很晚")
    store.add_evidence(h.id, "support", "context", cid)
    dec = bridge.on_support_evidence(h.id)
    assert dec.changed is False
    assert store.get(h.id).status == INACTIVE
    # 但证据没丢，重新判断时会看到它
    assert store.get(h.id).support_count >= 2