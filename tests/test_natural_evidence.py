"""Natural Evidence / Change 信号（V0.5.6 Phase 1-3）。

覆盖三件事：
1. 判定——一句话是提问/否定/变化/支持/无信号，优先级必须稳定；
2. 安全门——话题不相关绝不落库（这是整个功能唯一的闸门）；
3. 落链——支持/否定/变化分别怎么写进 evidence，各自可逆性如何。

判定词汇表刻意保守：宁可漏判（系统沉默），不可错判（污染长期理解）。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from core.natural_bridge import apply_natural_signal
from core.natural_evidence import (
    MIN_RELATED_GRAMS,
    classify_natural_signal,
    is_related,
    related_gram_count,
)
from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.store import MemoryStore


@pytest.fixture
def hyp(tmp_path) -> HypothesisStore:
    return HypothesisStore(tmp_path / "test.db")


@pytest.fixture
def world(tmp_path):
    """一个完整的小世界：store + context + hypothesis，共用一个库。"""
    db = tmp_path / "test.db"
    return {
        "db": db,
        "store": MemoryStore(db),
        "ctx": ContextStore(db),
        "hyp": HypothesisStore(db),
    }


def make_hyp(hyp, statement="用户晚上学习效率更高",
             scope="dynamic:晚上学习效率"):
    h, _ = hyp.create(statement, scope)
    return h


def evidence_rows(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT hypothesis_id, polarity, source_type, source_id, excerpt"
            " FROM hypothesis_evidence").fetchall()
    finally:
        con.close()


def log_rows(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT action, hypothesis_id, detail FROM hypothesis_log").fetchall()
    finally:
        con.close()


# ============================================================ 判定

@pytest.mark.parametrize("text,expected", [
    # 自然支持：用户主动陈述
    ("对，我就是晚上学习效率最高", "support"),
    ("对啊，我晚上确实效率高", "support"),
    ("嗯对，我一直这样", "support"),
    ("我确实晚上效率更高", "support"),
    ("我就是这种情况", "support"),
    ("我一直都是这样", "support"),
    # 自然否定：复用既有显式否决词表
    ("不是，我早上效率更高", "deny"),
    ("其实不是这样", "deny"),
    ("不对，我最近已经变了", "deny"),
    ("错了", "deny"),
    # 变化：降权，不否决
    ("我改成早上学习了", "change"),
    ("我最近换成早上了", "change"),
    ("我现在开始早起了", "change"),
    ("我最近调整作息了", "change"),
    ("我不再像以前那样晚上学习了", "change"),
    # 疑问：不是表态
    ("是不是我晚上效率更高？", "question"),
    ("对吗？", "question"),
    # 闲聊：安静
    ("今天食堂红烧肉真难吃", "none"),
])
def test_classify_natural_signal(text, expected):
    assert classify_natural_signal(text).signal == expected


@pytest.mark.parametrize("text", ["对", "嗯", "嗯对", "好", "是", "是啊", "同意"])
def test_bare_affirmation_is_not_evidence(text):
    """光秃秃的附和没有实质内容——比漏判更糟的是随机确认一条假设。"""
    assert classify_natural_signal(text).signal == "none"


def test_question_beats_everything():
    """优先级第一：疑问。就算句子里有"对"，在提问就不是表态。"""
    for t in ("对不对，我晚上学习效率最高？", "我改成早上了，是不是？"):
        assert classify_natural_signal(t).signal == "question"


def test_denial_beats_change_and_support():
    """优先级第二：否定压过变化与支持。

    "对，不过不是这样" 同时含"对"和否定——必须按否定处理，
    否则会出现"用户明说了不是，系统反而记成支持"的反向学习。
    """
    assert classify_natural_signal("对，不过不是这样").signal == "deny"
    assert classify_natural_signal("不是，我改成早上了").signal == "deny"


def test_change_beats_support():
    """优先级第三：变化压过支持。"""
    assert classify_natural_signal("对，我改成早上了").signal == "change"


def test_signal_is_single_valued():
    """一个句子只能是一种语义，不能既是支持又是变化。"""
    for t in ("对，我就是晚上学习效率最高", "我改成早上了", "不是这样"):
        sig = classify_natural_signal(t)
        assert sig.signal in {"support", "deny", "change", "question", "none"}


def test_empty_text_is_none():
    assert classify_natural_signal("").signal == "none"
    assert classify_natural_signal(None).signal == "none"


# ============================================================ 话题匹配（安全门）

def test_related_requires_two_shared_grams():
    """单个共同词不算相关。"""
    assert related_gram_count("我晚上学习状态好", "dynamic:晚上学习效率") >= 2
    # 只有"晚上"一个共同词——不足以动一条假设
    assert related_gram_count("我喜欢晚上散步", "dynamic:晚上学习效率") < 2


def test_topic_drift_does_not_match():
    """场景六：话题漂移。用户说"对啊，我最近很喜欢晚上散步"，
    不得支持"晚上学习效率更高"。"""
    text = "对啊，我最近很喜欢晚上散步。"
    assert classify_natural_signal(text).signal == "support"
    assert not is_related(text, "dynamic:晚上学习效率")


def test_only_the_truly_related_hypothesis_is_hit(world):
    """场景七：三条假设同时存在，一句自然表达只能命中真正相关的那条。"""
    hyp = world["hyp"]
    h1 = make_hyp(hyp, "用户晚上学习效率更高", "dynamic:晚上学习效率")
    h2 = make_hyp(hyp, "用户喜欢晚上运动", "dynamic:喜欢晚上运动")
    h3 = make_hyp(hyp, "用户喜欢喝咖啡", "dynamic:喜欢咖啡")

    out = apply_natural_signal(world["ctx"], hyp,
                              "对，我晚上学习状态确实好。")
    assert out["hypo_ids"] == [h1.id]
    assert h2.id not in out["hypo_ids"] and h3.id not in out["hypo_ids"]
    # 确认错的那条，代价是不可逆的污染——这里一条证据都不能有
    for row in evidence_rows(world["db"]):
        assert row[0] in (h1.id,)


# ============================================================ 落链：支持

def test_natural_support_writes_evidence_without_promoting_status(world):
    """场景二：自然支持写证据、可追溯，但**不**把假设判成已确认。

    用户说一句"对"不等于这条理解已被验证，更不等于可以绕过
    Pattern 晋级门槛。
    """
    hyp = world["hyp"]
    h = make_hyp(hyp)
    before = hyp.get(h.id)

    out = apply_natural_signal(world["ctx"], hyp, "对，我就是晚上学习效率最高。")

    assert out["hypo_ids"] == [h.id]
    after = hyp.get(h.id)
    assert after.support_count == before.support_count + 1
    assert after.confidence == pytest.approx(before.confidence + 0.08)
    assert after.status == "tentative"      # 不因一句"对"就 confirmed


def test_natural_support_is_traceable_to_original_words(world):
    """可追溯：evidence → log → 用户原话。"""
    hyp = world["hyp"]
    h = make_hyp(hyp)
    apply_natural_signal(world["ctx"], hyp, "对，我就是晚上学习效率最高。")

    rows = evidence_rows(world["db"])
    assert len(rows) == 1
    polarity, source_type, source_id, excerpt = rows[0][1], rows[0][2], rows[0][3], rows[0][4]
    assert polarity == "support"
    assert source_type == "correction"
    assert excerpt == "对，我就是晚上学习效率最高。"

    # source_id 必须指向一条真实存在的 log 行
    con = sqlite3.connect(f"file:{world['db']}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT action, detail FROM hypothesis_log WHERE id=?",
            (source_id,)).fetchone()
    finally:
        con.close()
    assert row is not None
    assert json.loads(row[1])["channel"] == "natural"


def test_repeated_identical_natural_statement_does_not_inflate(world):
    """同一句话重复落库只算一次证据（既有去重索引天然生效）。"""
    hyp = world["hyp"]
    h = make_hyp(hyp)
    apply_natural_signal(world["ctx"], hyp, "对，我就是晚上学习效率最高。")
    first = hyp.get(h.id)
    apply_natural_signal(world["ctx"], hyp, "对，我就是晚上学习效率最高。")
    assert hyp.get(h.id).support_count == first.support_count


# ============================================================ 落链：否定

def test_strong_denial_rejects_hypothesis(world):
    """强相关的"不是"走既有 reject：置 0.25 + rejected + 可恢复。"""
    hyp = world["hyp"]
    h = make_hyp(hyp)
    before = hyp.get(h.id)

    out = apply_natural_signal(world["ctx"], hyp,
                               "不是，晚上学习效率这件事我完全不同意。")
    assert h.id in out["hypo_ids"]
    after = hyp.get(h.id)
    assert after.status == "rejected"
    assert after.confidence == pytest.approx(0.25)
    assert after.confidence < before.confidence


def test_weak_denial_only_downgrades_and_stays_reversible(world):
    """弱相关的"不是"只吃反证，不判死。

    否决是不可逆的（要 restore 才能回来）。用户换个说法（"早上效率更高"
    vs 假设写的是"晚上学习效率"）只共享"效率"一个词——证据不足以
    把一条可能仍然正确的假设永久判死。
    """
    hyp = world["hyp"]
    h = make_hyp(hyp)

    out = apply_natural_signal(world["ctx"], hyp,
                               "不是，我现在早上效率更高。")
    assert h.id in out["hypo_ids"]
    after = hyp.get(h.id)
    assert after.status == "tentative"       # 没判死
    assert after.contradict_count == 1
    assert after.confidence == pytest.approx(0.25)   # 0.50 - 0.25


# ============================================================ 落链：变化

def test_change_downgrades_hypothesis_but_never_kills_it(world):
    """场景四：用户变了 → 旧假设降权，但状态保留。"""
    hyp = world["hyp"]
    h = make_hyp(hyp)
    out = apply_natural_signal(world["ctx"], hyp, "我最近已经改成早上学习了。")
    assert h.id in out["hypo_ids"]
    after = hyp.get(h.id)
    assert after.status == "tentative"
    assert after.contradict_count == 1


def test_change_marks_old_context_outdated_and_keeps_history(world):
    """变化让旧理解失活，但一条历史都不删。

    active=False 是删除；outdated 才是"不再当现状、仍然可追溯"。
    """
    ctx, hyp = world["ctx"], world["hyp"]
    old, _ = ctx.add_entry(kind="dynamic", content="用户晚上十点后学习效率最高",
                           source="user_direct")
    new, _ = ctx.add_entry(kind="dynamic", content="最近改成早上学习",
                           source="user_direct")

    out = apply_natural_signal(ctx, hyp, "我最近已经改成早上学习了。",
                               exclude_entry_ids=[new.id])

    assert old.id in out["entry_ids"]
    assert ctx.get_entry(old.id).memory_status == "outdated"
    # 旧条目仍在库里（历史可追溯），只是不再参与 prompt 组装
    assert ctx.get_entry(old.id).active is True
    # 本轮刚写的那条不能被自己标成 outdated
    assert ctx.get_entry(new.id).memory_status == "active"


def test_change_does_not_mark_the_entry_created_this_turn(world):
    """用户就是在这条之上说"我改成早上了"——不能反过来把它自己废掉。"""
    ctx, hyp = world["ctx"], world["hyp"]
    fresh, _ = ctx.add_entry(kind="dynamic", content="改成早上学习",
                             source="user_direct")
    apply_natural_signal(ctx, hyp, "我最近改成早上学习了。",
                         exclude_entry_ids=[fresh.id])
    assert ctx.get_entry(fresh.id).memory_status == "active"


def test_old_context_entry_remains_traceable_after_change(world):
    """场景四的可追溯性要求：失活不等于消失。"""
    ctx, hyp = world["ctx"], world["hyp"]
    old, _ = ctx.add_entry(kind="dynamic", content="用户晚上十点后学习效率最高",
                           source="user_direct")
    apply_natural_signal(ctx, hyp, "我最近已经改成早上学习了。",
                         exclude_entry_ids=[])
    con = sqlite3.connect(f"file:{world['db']}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT content, memory_status, status_reason FROM context_entries"
            " WHERE id=?", (old.id,)).fetchone()
    finally:
        con.close()
    assert row is not None
    assert "晚上十点后" in row[0]
    assert "变了" in row[2]


# ============================================================ 安静

def test_chitchat_creates_nothing(world):
    """场景五：普通闲聊不得制造任何长期理解。"""
    hyp = world["hyp"]
    out = apply_natural_signal(world["ctx"], hyp,
                               "今天食堂红烧肉真难吃，我朋友说还行。")
    assert out["signal"] == "none"
    assert out["hypo_ids"] == [] and out["entry_ids"] == []
    assert evidence_rows(world["db"]) == []


def test_natural_signal_never_creates_a_hypothesis(world):
    """铁律：自然表达只能确认/否定/降权**已有**假设，永远不新建。

    否则用户随口一句话就能凭空造出长期理解。
    """
    hyp = world["hyp"]
    for text in ("对，我就是每天五点起床的人。",
                 "我确实特别擅长游泳。",
                 "不是，我从来不喝咖啡。",
                 "我改成每天五点起床了。"):
        apply_natural_signal(world["ctx"], hyp, text)
    assert hyp.list_hypotheses(live_only=True, limit=50) == []


def test_question_creates_nothing(world):
    """场景九：提问不得变成支持证据。"""
    hyp = world["hyp"]
    h = make_hyp(hyp)
    out = apply_natural_signal(world["ctx"], hyp, "是不是我晚上效率更高？")
    assert out["signal"] == "question"
    assert out["hypo_ids"] == []
    assert hyp.get(h.id).support_count == 0


# ============================================================ 生命周期保护

def test_rejected_hypothesis_is_not_revived_by_natural_support(world):
    hyp = world["hyp"]
    h = make_hyp(hyp)
    hyp.reject(h.id, "用户明确否决", channel="trust_ui")
    out = apply_natural_signal(world["ctx"], hyp, "对，我就是晚上学习效率最高。")
    assert out["hypo_ids"] == []
    assert hyp.get(h.id).status == "rejected"


def test_deleted_hypothesis_is_not_revived(world):
    hyp = world["hyp"]
    h = make_hyp(hyp)
    hyp.delete_entry(h.id)
    apply_natural_signal(world["ctx"], hyp, "对，我就是晚上学习效率最高。")
    assert hyp.get(h.id).status == "deleted"


def test_quota_overflow_hypothesis_is_off_limits(world):
    """quota_overflow 是 V0.5.3 设的安全边界：不可验证、不可晋级、
    不可自然确认、不可自动恢复。自然证据必须一并挡住。"""
    hyp = world["hyp"]
    h = make_hyp(hyp, "配额溢出转出来的分析材料",
                 "quota_overflow:晚上学习")
    for text in ("对，我就是晚上学习效率最高。",
                 "不是，晚上学习效率不是这样。",
                 "我改成早上了。"):
        out = apply_natural_signal(world["ctx"], hyp, text)
        assert out["hypo_ids"] == [], f"quota_overflow 不该被碰：{text}"
    assert hyp.get(h.id).confidence == pytest.approx(0.5)


# ============================================================ store 层契约（直连）

# 上一节全部走 apply_natural_signal，它自己也会过滤一层。直接打 store
# 才能证明**存储层自己**守住这些边界——两层保护里任何一层被删掉，
# 测试都应该立刻变红。


@pytest.mark.parametrize("signal", ["support", "deny", "deny_weak", "change"])
def test_store_refuses_quota_overflow_hypothesis(world, signal):
    """quota_overflow 是 V0.5.3 设的安全边界，直连 store 也必须拒绝。"""
    hyp = world["hyp"]
    h = make_hyp(hyp, "配额溢出转出来的分析材料", "quota_overflow:晚上学习")
    assert hyp.record_natural_evidence(h.id, "对，我就是晚上学习效率最高。",
                                       signal) is False
    assert hyp.get(h.id).confidence == pytest.approx(0.5)
    assert evidence_rows(world["db"]) == []


@pytest.mark.parametrize("signal", ["support", "deny", "deny_weak", "change"])
def test_store_refuses_rejected_hypothesis(world, signal):
    hyp = world["hyp"]
    h = make_hyp(hyp)
    hyp.reject(h.id, "用户明确否决", channel="trust_ui")
    assert hyp.record_natural_evidence(h.id, "对，我就是晚上学习效率最高。",
                                       signal) is False
    assert hyp.get(h.id).status == "rejected"


@pytest.mark.parametrize("signal", ["support", "deny", "deny_weak", "change"])
def test_store_refuses_deleted_hypothesis(world, signal):
    hyp = world["hyp"]
    h = make_hyp(hyp)
    hyp.delete_entry(h.id)
    assert hyp.record_natural_evidence(h.id, "对，我就是晚上学习效率最高。",
                                       signal) is False
    assert hyp.get(h.id).status == "deleted"


@pytest.mark.parametrize("signal", ["", "unknown", "verify", "promote"])
def test_store_refuses_unknown_signal(world, signal):
    """未知信号必须被拒——不能因为信号名拼错就默认当成某种证据。"""
    hyp = world["hyp"]
    h = make_hyp(hyp)
    assert hyp.record_natural_evidence(h.id, "随便一句", signal) is False
    assert evidence_rows(world["db"]) == []


def test_store_refuses_empty_user_text(world):
    hyp = world["hyp"]
    h = make_hyp(hyp)
    assert hyp.record_natural_evidence(h.id, "   ", "support") is False
    assert evidence_rows(world["db"]) == []


def test_bridge_also_refuses_quota_overflow(world):
    """桥接层自己也必须过滤（第二道防线）。"""
    hyp = world["hyp"]
    h = make_hyp(hyp, "配额溢出", "quota_overflow:晚上学习")
    out = apply_natural_signal(world["ctx"], hyp, "对，我就是晚上学习效率最高。")
    assert out["hypo_ids"] == []


def test_bridge_also_refuses_rejected(world):
    hyp = world["hyp"]
    h = make_hyp(hyp)
    hyp.reject(h.id, "否决", channel="trust_ui")
    out = apply_natural_signal(world["ctx"], hyp, "对，我就是晚上学习效率最高。")
    assert out["hypo_ids"] == []


# ============================================================ 韧性

def test_bridge_swallows_store_failures(world):
    """后台链路出问题绝不能掀翻用户对话。"""

    class Boom(HypothesisStore):
        def list_hypotheses(self, **kw):
            raise RuntimeError("假设库炸了")

    boom = Boom(world["db"])
    out = apply_natural_signal(world["ctx"], boom, "对，我就是晚上学习效率最高。")
    assert out["error"] is not None          # 记下来了
    assert out["hypo_ids"] == []            # 但没往上抛


def test_missing_context_store_is_not_fatal(world):
    """Context 侧缺席时，假设侧仍应能工作（不该整条链路崩）。"""
    hyp = world["hyp"]
    h = make_hyp(hyp)
    out = apply_natural_signal(None, hyp, "对，我就是晚上学习效率最高。")
    assert out["hypo_ids"] == [h.id]