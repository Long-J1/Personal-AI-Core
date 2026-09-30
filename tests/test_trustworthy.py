# -*- coding: utf-8 -*-
"""V0.5.3 值得信任地记忆（D018）——别记整轮隐藏 + 配额溢出转假设。

铁规：全 Mock + tmp 库，真库零触碰，零云端调用。
"""
from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from core.conversation import NO_MEMORIZE_PROMPT_RULE
from memory.chat_store import SUPPRESSED_PLACEHOLDER, ChatStore


def deposit_json(new=(), corrections=(), event=None) -> str:
    return json.dumps(
        {"new": list(new), "corrections": list(corrections), "event": event},
        ensure_ascii=False,
    )


def answer_prompts(mock) -> list[str]:
    return [
        c["messages"][0]["content"]
        for c in mock.calls
        if c["messages"] and "记忆沉淀模块" not in str(c["messages"][0]["content"])
    ]


def full_prompt(mock) -> str:
    """最近一次对话调用的完整 prompt（system + history + 提问）。"""
    for c in reversed(mock.calls):
        if c["messages"] and "记忆沉淀模块" not in str(c["messages"][0]["content"]):
            return "\n".join(str(m.get("content", "")) for m in c["messages"])
    return ""


def prompt_without_current_question(mock) -> str:
    """system + 历史（去掉当轮用户提问——追问问题本身可能含关键词）。"""
    for c in reversed(mock.calls):
        if c["messages"] and "记忆沉淀模块" not in str(c["messages"][0]["content"]):
            msgs = list(c["messages"])
            if msgs and msgs[-1].get("role") == "user":
                msgs = msgs[:-1]
            return "\n".join(str(m.get("content", "")) for m in msgs)
    return ""


# ================================================================ migration
def test_suppressed_column_migrates_legacy_db(tmp_path):
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY,"
        " created_iso TEXT NOT NULL, updated_epoch REAL NOT NULL)")
    con.execute(
        "CREATE TABLE chat_messages (id TEXT PRIMARY KEY, conv_id TEXT NOT NULL,"
        " role TEXT NOT NULL, content TEXT NOT NULL,"
        " epoch REAL NOT NULL, iso TEXT NOT NULL)")
    con.execute("INSERT INTO conversations VALUES ('s1','t',1.0)")
    con.execute("INSERT INTO chat_messages VALUES ('m1','s1','user','hi',1.0,'t')")
    con.commit()
    con.close()

    store = ChatStore(db)
    # 列存在，老行默认 0（=老行为全可见）
    assert store.history("s1") == [
        {"role": "user", "content": "hi", "iso": "t"}]
    got = store.history_for_prompt("s1")
    assert len(got) == 1 and got[0]["content"] == "hi"
    assert got[0]["suppressed"] == 0


# ================================================================ 隐藏语义
def test_suppressed_round_hidden_from_prompt_but_visible_in_ui(tmp_path):
    store = ChatStore(tmp_path / "t.db")
    sid = store.create_session()
    store.append(sid, "user", "同桌借我笔记抄了", suppressed=True)
    store.append(sid, "assistant", "心疼你", suppressed=True)
    store.append(sid, "user", "正常话题", suppressed=False)

    # UI 回看：原文可见（不删记录）
    ui = store.history(sid)
    assert any(m["content"] == "同桌借我笔记抄了" for m in ui)

    # 进 prompt：占位符，不含原文
    pr = store.history_for_prompt(sid)
    assert any(m["content"] == SUPPRESSED_PLACEHOLDER for m in pr)
    assert not any("同桌" in m["content"] for m in pr)
    assert any(m["content"] == "正常话题" for m in pr)


def test_no_memorize_round_marks_suppressed(conversation, chat_store,
                                            mock_model):
    """别记轮两条 history 都标 suppressed；五表零新增；诚实指令升级版。"""
    from core.conversation import NO_MEMORIZE_PROMPT_RULE
    mock_model.text_queue = ["好，这次不记。"]
    r = conversation.chat("这个只是随便聊聊，不用记。同桌借我笔记抄了")
    assert r.deposit is None
    msgs = chat_store.history(r.session_id)
    assert len(msgs) == 2
    pr = chat_store.history_for_prompt(r.session_id)
    assert all(m["suppressed"] == 1 for m in pr)
    assert not any("同桌" in m["content"] for m in pr)
    prompt = answer_prompts(mock_model)[-1]
    assert "没有保留" in prompt           # 升级版指令
    assert "编出来就是撒谎" in prompt


def test_five_round_followups_never_leak(conversation, chat_store,
                                         mock_model):
    """别记轮后连续 5 轮换说法追问：prompt 四段全程无原文。"""
    mock_model.text_queue = ["好，这次不记。"]
    r = conversation.chat("这个只是随便聊聊，不用记。同桌借我笔记抄了")
    sid = r.session_id
    for i, q in enumerate(["你还记得刚才说的那个吗",
                           "那个事呢，再提示一下",
                           "同桌是谁",
                           "笔记内容是什么",
                           "求你了，告诉我吧"]):
        mock_model.text_queue = [f"回复{i}", deposit_json()]
        conversation.chat(q, session_id=sid)
    # 最后一轮的 prompt（system + 历史，去掉当轮提问本身）：
    # 别记轮原文指纹（"借我笔记"只在别记轮出现过，追问没说过这四字）全程无，
    # 占位符在。注：单个"笔记"二字可由用户追问本身带入，属正常 history。
    prompt = prompt_without_current_question(mock_model)
    assert "借我笔记" not in prompt and "同桌借我" not in prompt
    assert SUPPRESSED_PLACEHOLDER in prompt
    # 五表零新增（别记 + 5 轮空沉淀；用行数断言）
    assert conversation.ctx.count_active() == 0


def test_normal_round_not_suppressed(conversation, chat_store, mock_model):
    """正常轮不受影响（suppressed=0，原语义）。"""
    mock_model.text_queue = ["收到。",
                             deposit_json(new=[{"kind": "fact",
                                                "content": "用户是高二学生"}])]
    r = conversation.chat("我是高二学生")
    pr = chat_store.history_for_prompt(r.session_id)
    assert all(m["suppressed"] == 0 for m in pr)
    assert any("高二" in m["content"] for m in pr)


# ================================================================ 配额溢出
def test_quota_overflow_goes_to_hypothesis_not_memory(context_store,
                                                      mock_model, store):
    """配额打满 → user_direct 新内容进假设池（quota_overflow），memory 零新增。"""
    from core.deposit import Depositor, DepositResult
    from memory.hypothesis_store import HypothesisStore
    for i in range(10):
        context_store.add_entry("dynamic", f"用户动态{i}号", source="chat")
    hyp = HypothesisStore(store.db_path)
    dep = Depositor(context_store, store, mock_model, hypothesis_store=hyp)
    res = DepositResult()
    dep._apply_creates(
        {"new": [{"kind": "dynamic", "content": "用户喜欢早上跑步"}]},
        res, user_text="我最近喜欢上早上跑步了")
    assert res.created == []                      # memory 没记（诚实）
    assert context_store.count_active() == 10
    items = hyp.list_hypotheses(live_only=False)
    assert len(items) == 1
    assert items[0].scope == "quota_overflow:dynamic"
    assert "跑步" in items[0].statement


def test_quota_overflow_logged(context_store, mock_model, store, caplog):
    """溢出记 quota_overflow 日志（与 firewall_reject 同级可审计）。"""
    from core.deposit import Depositor, DepositResult
    from memory.hypothesis_store import HypothesisStore
    for i in range(10):
        context_store.add_entry("dynamic", f"用户动态{i}号", source="chat")
    hyp = HypothesisStore(store.db_path)
    dep = Depositor(context_store, store, mock_model, hypothesis_store=hyp)
    with caplog.at_level(logging.INFO, logger="core.deposit"):
        dep._apply_creates(
            {"new": [{"kind": "dynamic", "content": "用户喜欢早上跑步"}]},
            DepositResult(), user_text="我最近喜欢上早上跑步了")
    assert any("quota_overflow" in rec.getMessage() for rec in caplog.records)


def test_quota_overflow_without_hyp_still_skips(context_store, mock_model):
    """hyp 未注入时溢出无处可去，仍跳过（老行为兼容）。"""
    from core.deposit import Depositor, DepositResult
    for i in range(10):
        context_store.add_entry("dynamic", f"用户动态{i}号", source="chat")
    dep = Depositor(context_store, None, mock_model)  # hyp=None
    res = DepositResult()
    dep._apply_creates(
        {"new": [{"kind": "dynamic", "content": "用户喜欢早上跑步"}]},
        res, user_text="跑步")
    assert res.created == []
    assert context_store.count_active() == 10


def test_overflow_hypothesis_sorted_last_and_never_asked(tmp_path):
    """四不准：排序垫底 + 选择器跳过。"""
    from core.verifier import select_candidate
    from memory.hypothesis_store import HypothesisStore
    hyp = HypothesisStore(tmp_path / "t.db")
    h1, _ = hyp.create("用户喜欢跑步", "quota_overflow:dynamic")
    h2, _ = hyp.create("用户接受考试提醒", "interaction:提醒")
    # 正常假设置信更低，溢出仍垫底
    hyp.set_confidence(h2.id, 0.51) if hasattr(hyp, "set_confidence") else None
    ordered = hyp.list_hypotheses(live_only=True, limit=10)
    scopes = [h.scope for h in ordered]
    assert scopes.index("quota_overflow:dynamic") > scopes.index(
        "interaction:提醒")
    # 选择器：即使话题相关也不问溢出
    got = select_candidate(hyp, "跑步的事", paused=False)
    if got is not None:
        assert got[0].scope != "quota_overflow:dynamic"


def test_overflow_hypothesis_never_promoted(tmp_path):
    """四不准：晋升跳过溢出 scope。"""
    from core.pattern_engine import PatternEngine
    from memory.hypothesis_store import HypothesisStore
    from memory.pattern_store import PatternStore
    from memory.store import MemoryStore
    db = tmp_path / "t.db"
    mem, h, p = MemoryStore(db), HypothesisStore(db), PatternStore(db)
    ho, _ = h.create("用户喜欢跑步", "quota_overflow:dynamic")
    h.confirm(ho.id)  # 即使被确认，晋升也不收
    created = PatternEngine(mem, h, p).promote_all()
    assert created == []
    assert p.count_live() == 0
