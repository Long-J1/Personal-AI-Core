# V0.5 Pattern 存储：数学 + 铁律 + 双来源 + pending（fixture 库）。
from __future__ import annotations

import pathlib

import pytest

from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.pattern_store import PatternStore


@pytest.fixture
def pat(tmp_path):
    return PatternStore(tmp_path / "p.db")


@pytest.fixture
def hyp(tmp_path):
    return HypothesisStore(tmp_path / "p.db")


@pytest.fixture
def ctx(tmp_path):
    return ContextStore(tmp_path / "p.db")


def test_create_defaults(pat):
    p, skip = pat.create("用户长期存在 deadline 前冲刺的规律",
                         "activity:冲刺", source="promotion")
    assert skip is None
    assert (p.confidence, p.status, p.source) == (0.60, "tentative",
                                                  "promotion")
    assert p.support_count == 0


def test_create_validation(pat):
    assert pat.create("", "scope:x")[0] is None
    assert pat.create("陈述", "")[0] is None
    assert pat.create("陈述", "scope:x", source="llm")[0] is None


def test_math_support_and_gate(pat, ctx):
    # 0.60 起；+0.06；独立源<2 锁 0.75
    p, _ = pat.create("长期规律", "interaction:提醒", source="promotion")
    e1, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    e2, _ = ctx.add_entry("interaction", "比赛前提醒我", source="chat")
    assert pat.add_evidence(p.id, "support", "context", e1.id)[0]
    assert pat.get(p.id).confidence == pytest.approx(0.66)
    assert pat.add_evidence(p.id, "support", "context", e2.id)[0]
    assert pat.get(p.id).confidence == pytest.approx(0.72)
    e3, _ = ctx.add_entry("interaction", "开会前提醒我", source="chat")
    assert pat.add_evidence(p.id, "support", "context", e3.id,
                            origin="ctxday:2099-01-01")[0]
    assert pat.get(p.id).confidence == pytest.approx(0.78)


def test_gate_cap_single_origin(pat, ctx):
    p, _ = pat.create("长期规律", "scope:x", source="promotion")
    for i in range(6):
        e, _ = ctx.add_entry("interaction", f"偏好{i}测试文本", source="chat")
        assert pat.add_evidence(p.id, "support", "context", e.id,
                                origin="batch:1")[0]
    # 0.60+6*0.06=0.96 cap 到 0.75
    assert pat.get(p.id).confidence == pytest.approx(0.75)


def test_contradict(pat, ctx):
    p, _ = pat.create("长期规律", "scope:x", source="promotion")
    e, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    pat.add_evidence(p.id, "support", "context", e.id)
    e2, _ = ctx.add_entry("interaction", "别再提醒了", source="chat")
    assert pat.add_evidence(p.id, "contradict", "correction", e2.id,
                            excerpt="用户纠正")[0]
    assert pat.get(p.id).confidence == pytest.approx(0.66 - 0.20)


def test_iron_rule(pat):
    # pattern 自身、单轮聊天原文永不能成为证据
    p, _ = pat.create("长期规律", "scope:x", source="promotion")
    p2, _ = pat.create("另一条", "scope:y", source="promotion")
    ok, _ = pat.add_evidence(p.id, "support", "pattern", p2.id)
    assert ok is False
    ok, _ = pat.add_evidence(p.id, "support", "chat", "some-msg-id")
    assert ok is False
    ok, _ = pat.add_evidence(p.id, "support", "context", "deadbeef" * 4)
    assert ok is False


def test_hypothesis_evidence_links(pat, hyp, ctx):
    # A 路径：confirmed 假设可作为证据
    h, _ = hyp.create("用户接受提前提醒", "interaction:提醒")
    e, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    hyp.add_evidence(h.id, "support", "context", e.id)
    hyp.confirm(h.id)
    p, _ = pat.create("长期提醒规律", "interaction:提醒", source="promotion")
    ok, why = pat.add_evidence(p.id, "support", "hypothesis", h.id,
                               excerpt=h.statement)
    assert ok, why
    assert pat.get_detail(p.id)["independent_sources"] == 1


def test_declare_from_user_needs_confirmation(pat):
    # B 路径：只建 tentative，不直出 confirmed；重复主题拒绝
    p, skip = pat.declare_from_user(
        "用户长期存在截止前冲刺的规律", "user:冲刺", "我发现我一到截止前就熬夜")
    assert skip is None and p.status == "tentative"
    assert p.support_count == 1  # 原话证据已入链
    d = pat.get_detail(p.id)
    assert d["evidence"][0]["source_type"] == "confirmation"
    p2, skip2 = pat.declare_from_user("另一条", "user:冲刺", "我总是这样")
    assert p2 is None and skip2 is not None


def test_confirm_reject_delete_clear_pending(pat):
    p, _ = pat.create("长期规律", "scope:x", source="promotion")
    pat.set_pending(p.id, "问题？")
    assert pat.confirm(p.id) is True
    assert pat.get(p.id).pending_question == ""
    p2, _ = pat.create("长期规律二", "scope:y", source="promotion")
    pat.set_pending(p2.id, "问题？")
    assert pat.reject(p2.id, reason="不对") is True
    assert pat.get(p2.id).status == "rejected"
    assert pat.get(p2.id).confidence == pytest.approx(0.25)
    assert pat.get(p2.id).pending_question == ""


def test_pending_singleton(pat):
    p1, _ = pat.create("规律一", "scope:a", source="promotion")
    p2, _ = pat.create("规律二", "scope:b", source="promotion")
    assert pat.set_pending(p1.id, "问一？") == (True, None)
    ok, why = pat.set_pending(p2.id, "问二？")
    assert ok is False
    assert pat.get_pending().id == p1.id
    assert pat.clear_pending(p1.id) is True
    assert pat.get_pending() is None


def test_delete_restore(pat):
    p, _ = pat.create("长期规律", "scope:x", source="promotion")
    assert pat.delete_entry(p.id) is True
    assert pat.get(p.id).status == "deleted"
    assert pat.restore_entry(p.id) is True
    assert pat.get(p.id).status == "tentative"
