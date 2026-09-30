# -*- coding: utf-8 -*-
"""V0.5.2 Trust Boundary 灾难测试（D017）——PLAN_v0.5.2 §十 五场景 + 单测。

  Test1 幻觉污染 / Test2 纠正攻击 / Test3 删除攻击 / Test4 闲聊污染 / Test5 自夸限制
  另含：防火墙分类、"别记"匹配、质量排序、migration、复本传播、级联。

铁规：全 Mock + tmp 库，真库零触碰，零云端调用。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

import pytest

from core.conversation import (
    NO_MEMORIZE_PROMPT_RULE,
    SELF_PRAISE_RULE,
    is_no_memorize,
)
from core.deposit import (
    Depositor,
    DepositResult,
    _split_clauses,
    classify_source,
)
from memory.context_store import (
    ContextStore,
    decay_weight,
    sort_for_prompt,
    trust_level,
)
from memory.hypothesis_store import HypothesisStore
from memory.pattern_store import PatternStore
from models.mock_adapter import MockChatModel


# ---------------------------------------------------------------- 工具
def deposit_json(new=(), corrections=(), event=None) -> str:
    return json.dumps(
        {"new": list(new), "corrections": list(corrections), "event": event},
        ensure_ascii=False,
    )


def answer_prompts(mock: MockChatModel) -> list[str]:
    """所有对话回答调用的系统提示词（排除沉淀调用）。"""
    return [
        c["messages"][0]["content"]
        for c in mock.calls
        if c["messages"] and "记忆沉淀模块" not in str(c["messages"][0]["content"])
    ]


def longterm_counts(db_path) -> dict[str, int]:
    """五个长期表的总行数（"DB 零新增"口径，不含 history）。"""
    con = sqlite3.connect(str(db_path))
    try:
        return {
            t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("context_entries", "hypotheses", "patterns",
                      "goals", "events")
        }
    finally:
        con.close()


# ================================================================ Test1 幻觉
def test_t1_birthday_hallucination_zero_new(conversation, context_store,
                                            mock_model, caplog):
    """用户没说过生日 → 问"你记得我生日吗" → 五表零新增 + 拦截可审计。"""
    before = longterm_counts(conversation.store.db_path)
    mock_model.text_queue = [
        "我这边没有相关记录。",
        deposit_json(new=[{"kind": "fact", "content": "用户生日是3月5日"}]),
    ]
    with caplog.at_level(logging.INFO, logger="core.deposit"):
        r = conversation.chat("你记得我生日吗？")
    assert r.answer
    assert longterm_counts(conversation.store.db_path) == before
    assert context_store.count_active() == 0
    assert conversation.hyp.list_hypotheses(live_only=False) == []
    assert any("firewall_reject" in rec.getMessage() for rec in caplog.records)


def test_t1_date_hallucination_with_context_rejected(conversation,
                                                     context_store,
                                                     mock_model, caplog):
    """有依据的提问照常答，但模型编日期（DB/原话都没有）→ 硬拦截。"""
    context_store.add_entry("fact", "用户在备战CSP", source="chat")
    mock_model.text_queue = [
        "快到了，注意状态。",
        deposit_json(new=[{"kind": "dynamic",
                           "content": "用户9月30日参加CSP考试"}]),
    ]
    with caplog.at_level(logging.INFO, logger="core.deposit"):
        conversation.chat("CSP最近状态怎么样？")
    assert context_store.count_active() == 1  # 只有预置那条
    assert any("firewall_reject" in rec.getMessage() for rec in caplog.records)
    assert not any("9月30日" in e.content
                   for e in context_store.list_entries())


# ================================================================ Test2 纠正攻击
def test_t2_correction_attack_roots_out_old_entry(conversation,
                                                  context_store, mock_model):
    """"我喜欢咖啡"→"不对，我讨厌咖啡"（模型没纠正旧条）→ 规则兜底
    必须把旧条标 rejected 且不再进 prompt——纠正不断根。"""
    # 轮1：正常沉淀
    mock_model.text_queue = [
        "好，你喜欢喝咖啡，记下了。",
        deposit_json(new=[{"kind": "fact", "content": "用户喜欢喝咖啡"}]),
    ]
    r1 = conversation.chat("我喜欢喝咖啡。")
    created = r1.deposit["created"]
    assert created and created[0]["content"] == "用户喜欢喝咖啡"
    old_id = created[0]["id"]
    old = context_store.get_entry(old_id)
    assert old.source_grade == "user_direct" and old.confidence == 0.95

    # 轮2：硬否认，模型摆烂（只新增、不纠正）
    mock_model.text_queue = [
        "明白，你讨厌喝咖啡——之前喜欢喝咖啡的说法作废。",
        deposit_json(new=[{"kind": "fact", "content": "用户讨厌喝咖啡"}]),
    ]
    conversation.chat("不对，我讨厌喝咖啡。")
    old = context_store.get_entry(old_id)
    assert old.memory_status == "rejected"
    assert old.status_reason == "user_correction"
    assert old.confidence == 0.25

    # 轮3：旧条不再参与判断
    mock_model.text_queue = ["这题选 B。", deposit_json()]
    r3 = conversation.chat("我喜欢什么？")
    assert old_id not in {u["id"] for u in r3.context_used}
    prompt = answer_prompts(mock_model)[-1]
    assert "用户喜欢喝咖啡" not in prompt
    # 新的正确认知在
    assert any(e.content == "用户讨厌喝咖啡"
               for e in context_store.list_entries())


def test_t2_soft_denial_marks_outdated_not_rejected(conversation,
                                                    context_store,
                                                    mock_model):
    """非硬否决词（"别/不要"）→ outdated（软），不是 rejected。"""
    e, _ = context_store.add_entry("interaction", "回答问题时给完整步骤",
                                   source="chat")
    dep = Depositor(context_store, conversation.store, mock_model,
                    hypothesis_store=conversation.hyp,
                    pattern_store=conversation.pat)
    res = DepositResult()
    dep._fallback_denial("别这样，回答问题时不用给完整步骤",
                         "好的，我简写步骤。", [e], res)
    got = context_store.get_entry(e.id)
    assert got.memory_status == "outdated"
    assert got.status_reason == "user_correction"


# ================================================================ Test3 删除攻击
def test_t3_no_memorize_hard_switch(conversation, context_store, chat_store,
                                    mock_model):
    """"不要记住…" → 本轮零学习写入（五表零新增），history 照常，
    system 注入诚实指令。"""
    before = longterm_counts(conversation.store.db_path)
    mock_model.text_queue = ["好，这次内容不会进入长期记忆。",
                             deposit_json(new=[{"kind": "fact",
                                                "content": "用户喜欢玩某游戏"}])]
    r = conversation.chat("不要记住我喜欢玩某游戏。")
    assert r.deposit is None                     # 沉淀根本没跑
    assert longterm_counts(conversation.store.db_path) == before
    # history 照常追加（不算长期记忆——PLAN §六加粗边界）
    hist = chat_store.history(r.session_id, limit=10)
    assert len(hist) == 2
    # system prompt 注入诚实指令
    assert NO_MEMORIZE_PROMPT_RULE in answer_prompts(mock_model)[-1]

    # 追问：长期记忆里没有这个游戏
    mock_model.text_queue = ["这题选 B。", deposit_json()]
    conversation.chat("我喜欢玩什么游戏？")
    prompt = answer_prompts(mock_model)[-1]
    assert "某游戏" not in prompt
    assert not any("游戏" in e.content for e in context_store.list_entries())


def test_t3_matcher_positive_and_negative():
    """匹配器：删除请求命中；"别记错"是纠正，绝不能被误杀。"""
    assert is_no_memorize("昨天打游戏的事，你别记这个")
    assert is_no_memorize("不要记住我喜欢玩某游戏")
    assert is_no_memorize("这事儿只是随便聊聊，不用记")
    assert is_no_memorize("忘掉这个吧")
    assert not is_no_memorize("别记错，我是高二的")   # 纠正必须照写
    assert not is_no_memorize("帮我记一下今天的作业")


def test_t3_correction_with_jiucuo_still_writes(conversation,
                                                context_store, mock_model):
    """"别记错"轮照常沉淀（is_no_memorize 放行 + 分类为 user_direct）。"""
    mock_model.text_queue = [
        "收到，你是高二。",
        deposit_json(new=[{"kind": "profile", "content": "用户是高二学生"}]),
    ]
    r = conversation.chat("别记错，我是高二的")
    assert r.deposit is not None
    assert context_store.count_active() == 1


# ================================================================ Test4 闲聊污染
def test_t4_smalltalk_zero_growth(conversation, context_store, mock_model):
    """"哈哈/嗯/真的吗"连续 5 轮 → 五表零增长（模型配合返回空沉淀）。"""
    before = longterm_counts(conversation.store.db_path)
    for text in ("哈哈", "嗯", "真的吗", "哦哦", "666"):
        mock_model.text_queue = ["哈", deposit_json()]
        conversation.chat(text)
    assert longterm_counts(conversation.store.db_path) == before


def test_t4_smalltalk_model_junk_cannot_reach_memory(conversation,
                                                     context_store,
                                                     mock_model):
    """模型在闲聊轮硬塞"理解" → 无出处、无原话 → 进不了 memory。"""
    mock_model.text_queue = [
        "哈",
        deposit_json(new=[{"kind": "fact", "content": "用户今天心情不错"}]),
    ]
    conversation.chat("哈哈")
    assert context_store.count_active() == 0
    # 按设计进 hypothesis 待验证池（降权段），不进 memory
    assert conversation.hyp.list_hypotheses(live_only=False) != []


# ================================================================ Test5 自夸限制
def test_t5_self_praise_honesty_instruction(conversation, mock_model):
    """"你是不是已经完全了解我？" → system 注入诚实边界（数字如实）。"""
    mock_model.text_queue = ["我记下了一些要点。", deposit_json()]
    conversation.chat("你是不是已经完全了解我？")
    prompt = answer_prompts(mock_model)[-1]
    assert "【诚实边界】" in prompt
    assert "0 条理解" in prompt          # 空库如实报 0，不许吹
    assert SELF_PRAISE_RULE.split("{")[0] in prompt


# ================================================================ 防火墙分类单测
def test_firewall_hard_rejects():
    assert classify_source("用户生日是3月5日", "你记得我生日吗？",
                           [], None)[0] == "reject"
    assert classify_source("用户9月30日参加CSP考试", "CSP状态怎么样",
                           [], None)[0] == "reject"
    assert classify_source("用户是个很懒的人", "我今天起不来床",
                           [], None)[0] == "reject"
    assert classify_source("用户之前说过喜欢跑步", "我不喜欢跑步",
                           [], None)[0] == "reject"
    assert classify_source("用户上次去过网吧", "我今天学习",
                           [], None)[0] == "reject"


def test_firewall_user_words_vs_advisory_vs_inferred():
    # 用户原话 → user_direct（"你喜欢X"按出处判，不按句式判）
    assert classify_source("用户喜欢喝咖啡", "我喜欢喝咖啡",
                           [], None, "fact")[0] == "user_direct"
    # 泛化建议 → advisory 丢弃；主语是 AI 的要求 → 正常走
    assert classify_source("你应该早睡", "你应该早睡",
                           [], None, "interaction")[0] == "advisory"
    assert classify_source("你应该提醒我", "你应该提醒我",
                           [], None, "interaction")[0] == "user_direct"
    # 推断搭车 → inferred（不许借用户原话的光）
    assert classify_source("用户喜欢咖啡，说明他焦虑", "我喜欢咖啡",
                           [], None, "profile")[0] == "inferred"


def test_firewall_split_mixed_sentence():
    parts = _split_clauses("用户说考试前提醒，说明他焦虑")
    assert len(parts) == 2
    assert classify_source(parts[0], "考试前提醒我一下",
                           [], None, "interaction")[0] == "user_direct"
    assert classify_source(parts[1], "考试前提醒我一下",
                           [], None, "interaction")[0] == "inferred"


def test_firewall_db_grounded_writes_memory(context_store):
    """既有理解的复述 → db_grounded（不冒充原话，也不扔进假设池）。"""
    entries = []
    e, _ = context_store.add_entry("fact", "用户在备战CSP", source="chat")
    entries.append(e)
    grade, _ = classify_source("CSP备考顺利", "CSP最近状态怎么样？",
                               entries, None, "dynamic")
    assert grade == "db_grounded"


def test_db_grounded_grade_lands_in_db(conversation, context_store,
                                       mock_model):
    """集成：有依据提问的复述入库，等级 db_grounded / 置信 0.8。"""
    context_store.add_entry("fact", "用户在备战CSP", source="chat")
    mock_model.text_queue = [
        "还行。",
        deposit_json(new=[{"kind": "dynamic", "content": "CSP备考顺利"}]),
    ]
    conversation.chat("CSP最近状态怎么样？")
    entry = next(e for e in context_store.list_entries()
                 if e.content == "CSP备考顺利")
    assert entry.source_grade == "db_grounded"
    assert entry.confidence == 0.8


# ================================================================ 质量排序
def test_trust_level_and_decay_math():
    assert trust_level(0.95, "user_direct", 0, 0) == "high"
    assert trust_level(0.4, "user_direct", 0, 0) == "low"      # 置信太低
    assert trust_level(0.95, "user_direct", 60, 0) == "medium"  # 闲置过半衰
    assert trust_level(0.95, "user_direct", 0, 1) == "low"      # 有矛盾史
    now = time.time()
    w = decay_weight(0.95, now - 30 * 86400, now)               # 半衰 30 天
    assert abs(w - 0.475) < 1e-6


def test_sort_for_prompt_trust_then_decay(context_store):
    hi, _ = context_store.add_entry("fact", "刚确认的硬事实", source="chat",
                                    source_grade="user_direct",
                                    confidence=0.95)
    lo, _ = context_store.add_entry("fact", "很久没人提的旧猜测", source="chat",
                                    source_grade="inferred",
                                    confidence=0.4)
    # 把 lo 的验证时间推到 90 天前
    con = sqlite3.connect(str(context_store.db_path))
    with con:
        con.execute("UPDATE context_entries SET last_verified=? WHERE id=?",
                    (time.time() - 90 * 86400, lo.id))
    con.close()
    ordered = sort_for_prompt([lo, hi])   # 故意倒序喂入
    assert [e.id for e in ordered] == [hi.id, lo.id]


# ================================================================ migration
def test_migration_backfills_legacy_db(tmp_path):
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE context_entries ("
        "id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,"
        "source TEXT NOT NULL, created_at_iso TEXT NOT NULL,"
        "created_at_epoch REAL NOT NULL, updated_at_iso TEXT NOT NULL,"
        "updated_at_epoch REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,"
        "used_count INTEGER NOT NULL DEFAULT 0)")
    now_iso, now_epoch = "2026-01-01T00:00:00+08:00", 1767225600.0
    for i, src in enumerate(("chat", "manual", "correction")):
        con.execute(
            "INSERT INTO context_entries VALUES (?,?,?,?,?,?,?,?,1,0)",
            (f"e{i}", "fact", f"老条目{i}", src, now_iso, now_epoch,
             now_iso, now_epoch))
    con.commit()
    con.close()

    ctx = ContextStore(db)
    old_chat = ctx.get_entry("e0")
    assert old_chat.source_grade == "inferred" and old_chat.confidence == 0.5
    assert old_chat.memory_status == "active"
    assert old_chat.last_verified == pytest.approx(now_epoch)  # 按最后更新起算
    for i in (1, 2):  # 用户亲手写的 → 回填信任
        e = ctx.get_entry(f"e{i}")
        assert e.source_grade == "user_confirmed"
        assert e.confidence == 0.95
    # 老条目照样能被新分类体系读写
    assert ctx.list_entries()[0].kind == "fact"


# ================================================================ 复本传播
def test_correction_propagates_to_similar_copies(context_store):
    e1, _ = context_store.add_entry("fact", "用户喜欢喝咖啡", source="chat")
    e2, _ = context_store.add_entry("fact", "用户喜欢喝咖啡的", source="chat")
    assert context_store.correct_entry(
        e1.id, "用户讨厌喝咖啡", source="correction", reason="改口")
    got = context_store.get_entry(e2.id)
    assert got.memory_status == "outdated"
    assert got.status_reason.startswith("propagated:")
    # prompt 只读 active：复本不再进组装
    assert e2.id not in {x.id for x in context_store.list_entries()}
    # 被改的那条本身仍是 active（内容已改对）
    assert context_store.get_entry(e1.id).memory_status == "active"


def test_denial_propagates_to_similar_copies(context_store):
    e1, _ = context_store.add_entry("fact", "用户喜欢喝咖啡", source="chat")
    e2, _ = context_store.add_entry("fact", "用户喜欢喝咖啡的", source="chat")
    assert context_store.set_memory_status(e1.id, "rejected",
                                           reason="user_correction")
    assert context_store.get_entry(e2.id).memory_status == "outdated"
    # 拒绝态条目不再出现在 list（prompt 只读 active）
    assert e1.id not in {x.id for x in context_store.list_entries()}


def test_list_entries_hides_outdated_but_get_entry_sees_it(context_store):
    e, _ = context_store.add_entry("fact", "旧的错误理解", source="chat")
    context_store.set_memory_status(e.id, "outdated", reason="user_correction")
    assert context_store.list_entries() == []           # 组装层看不见
    assert context_store.get_entry(e.id) is not None    # 审计层还在


# ================================================================ 级联
def test_hypothesis_reject_cascades_to_pattern(tmp_path):
    db = tmp_path / "cascade.db"
    hyp, pat = HypothesisStore(db), PatternStore(db)
    h, _ = hyp.create("用户喜欢咖啡", "fact:咖啡")
    p, _ = pat.create("他会主动要咖啡", "fact:咖啡", source="promotion")
    ok, why = pat.add_evidence(p.id, "support", "hypothesis", h.id)
    assert ok, why
    before = pat.get(p.id).confidence
    assert hyp.reject(h.id, "用户否认")
    after = pat.get(p.id)
    assert after.confidence < before                      # 吃了 -0.20 反证
    detail = pat.get_detail(p.id)
    assert any(ev["polarity"] == "contradict"
               for ev in detail["evidence"])


def test_context_denial_cascades_to_pattern(context_store, mock_model):
    db = context_store.db_path
    pat = PatternStore(db)
    e, _ = context_store.add_entry("interaction", "回答问题时给完整步骤",
                                   source="chat")
    p, _ = pat.create("他会要求给步骤", "interaction:步骤", source="promotion")
    ok, why = pat.add_evidence(p.id, "support", "context", e.id)
    assert ok, why
    before = pat.get(p.id).confidence

    dep = Depositor(context_store, None, mock_model, pattern_store=pat)
    res = DepositResult()
    dep._fallback_denial("不对，回答问题时不需要给完整步骤，直接给结论",
                         "好的，以后直接给结论。", [e], res)
    assert context_store.get_entry(e.id).memory_status == "rejected"
    after = pat.get(p.id)
    assert after.confidence < before
    detail = pat.get_detail(p.id)
    assert any(ev["polarity"] == "contradict"
               and ev["source_type"] == "context"
               for ev in detail["evidence"])


def test_cascade_is_idempotent_and_fault_tolerant(tmp_path):
    pat = PatternStore(tmp_path / "idem.db")
    hyp = HypothesisStore(tmp_path / "idem.db")
    h, _ = hyp.create("用户喜欢茶", "fact:茶")
    p, _ = pat.create("他会要茶", "fact:茶", source="promotion")
    pat.add_evidence(p.id, "support", "hypothesis", h.id)
    hyp.reject(h.id, "否认")
    conf = pat.get(p.id).confidence
    # 再次级联（重复来源）→ 重复证据被防线挡住，置信不再掉
    n = pat.cascade_from_source("hypothesis", h.id, "再来一次")
    assert n == 0
    assert pat.get(p.id).confidence == conf
