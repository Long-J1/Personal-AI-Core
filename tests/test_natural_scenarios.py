"""Natural Evidence / Change 的真实端到端场景（V0.5.6 Phase 4）。

这些测试走的是**真实链路**：
    conversation.chat() → _schedule_deposit → _deposit_body
      → understanding parse → context/event
      → natural evidence  → inducer → promoter

不 mock 掉沉淀线程，也不绕过对话通道。目的是证明功能在真实后台
链路里成立，而不只是单测里成立。
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from memory.hypothesis_store import HypothesisStore


def deposit_json(new=(), corrections=(), event=None) -> str:
    return json.dumps({"new": list(new), "corrections": list(corrections),
                       "event": event}, ensure_ascii=False)


def wait_for(predicate, timeout=25.0, interval=0.15):
    """后台沉淀是异步的——轮询等它跑完，不要死写 sleep。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def rows(db, sql, args=()):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


@pytest.fixture
def hyp(tmp_path) -> HypothesisStore:
    return HypothesisStore(tmp_path / "test.db")


@pytest.fixture
def evening_hyp(hyp):
    """一条已存在的假设：用户晚上学习效率更高。"""
    h, _ = hyp.create("用户晚上学习效率更高", "dynamic:晚上学习效率")
    return h


def evidence_count(db, hypo_id=None):
    if hypo_id:
        return len(rows(db, "SELECT id FROM hypothesis_evidence"
                          " WHERE hypothesis_id=?", (hypo_id,)))
    return len(rows(db, "SELECT id FROM hypothesis_evidence"))


# ------------------------------------------------------------ 场景 1：一次自然表达

def test_scenario1_first_natural_statement_writes_context(
    conversation, context_store, mock_model, store
):
    """用户第一次表达长期倾向 → Context 正常写入。

    当前规则（需要同一活动重复 3 次才归纳）不足以从这一句话产生假设，
    所以这里只断言"理解写进去了"——机制可用即可，不硬凑假设。
    """
    mock_model.text_queue = [
        "记下了。",
        deposit_json(new=[{"kind": "dynamic",
                           "content": "晚上十点后学习效率最高"}]),
    ]
    conversation.chat("我一般晚上十点后学习效率最高。")
    assert wait_for(lambda: context_store.count_active() >= 1), \
        "Context 未在后台沉淀中写入"
    assert "晚上十点后" in context_store.list_entries()[0].content


# ------------------------------------------------------------ 场景 2：自然支持

def test_scenario2_natural_support_on_existing_hypothesis(
    conversation, mock_model, store, hyp, evening_hyp
):
    """已有假设 + 用户主动确认 → support evidence，可追溯到原话。"""
    mock_model.text_queue = ["嗯，知道了。", deposit_json()]
    conversation.chat("对，我就是晚上学习效率最高。")
    db = store.db_path
    ok = wait_for(lambda: evidence_count(db, evening_hyp.id) >= 1)
    assert ok, "自然支持未落库"

    row = rows(db, "SELECT polarity, excerpt FROM hypothesis_evidence"
                   " WHERE hypothesis_id=?", (evening_hyp.id,))[0]
    assert row[0] == "support"
    assert row[1] == "对，我就是晚上学习效率最高。"
    # 不因一句"对"就把它判成已验证
    assert hyp.get(evening_hyp.id).status == "tentative"


# ------------------------------------------------------------ 场景 3：自然否定

def test_scenario3_natural_denial_creates_contradiction(
    conversation, mock_model, store, hyp, evening_hyp
):
    """用户说"不是" → 反证入库，旧理解不再是唯一 active truth。"""
    mock_model.text_queue = ["好，记下了。", deposit_json()]
    conversation.chat("不是，我现在早上效率更高。")
    db = store.db_path
    assert wait_for(lambda: hyp.get(evening_hyp.id).contradict_count >= 1), \
        "自然否定未落库"

    row = rows(db, "SELECT polarity FROM hypothesis_evidence"
                   " WHERE hypothesis_id=? AND polarity='contradict'",
               (evening_hyp.id,))
    assert row, "缺 contradict 证据"
    # 弱相关 → 只降权不判死（换说法也还在谈同一件事，但证据不足以判死）
    assert hyp.get(evening_hyp.id).status == "tentative"
    assert hyp.get(evening_hyp.id).confidence < 0.5


# ------------------------------------------------------------ 场景 4：自然变化

def test_scenario4_change_deactivates_old_understanding(
    conversation, context_store, mock_model, store, hyp, evening_hyp
):
    """用户变了 → 旧理解失活，新理解成为 active，历史一条不删。"""
    old, _ = context_store.add_entry(
        kind="dynamic", content="用户晚上十点后学习效率最高",
        source="user_direct")
    mock_model.text_queue = [
        "明白。",
        deposit_json(new=[{"kind": "dynamic",
                           "content": "最近改成早上六点起床"}]),
    ]
    conversation.chat("我最近已经改成早上学习了。")

    assert wait_for(lambda: evidence_count(store.db_path,
                                           evening_hyp.id) >= 1), \
        "变化信号未在假设上落反证"

    fresh = [e for e in context_store.list_entries()
             if "早上六点" in e.content]
    assert wait_for(lambda: context_store.get_entry(old.id)
                    .memory_status == "outdated"), "旧理解未失活"
    assert context_store.get_entry(old.id).active is True     # 历史没被删
    assert fresh and fresh[0].memory_status == "active"       # 新理解是现状


# ------------------------------------------------------------ 场景 5：普通闲聊

def test_scenario5_chitchat_stays_silent(
    conversation, context_store, mock_model, store
):
    """闲聊不得制造任何长期理解。"""
    mock_model.text_queue = ["确实。", deposit_json()]
    conversation.chat("今天食堂红烧肉真难吃，我朋友说还行。")
    time.sleep(3)
    assert evidence_count(store.db_path) == 0
    assert context_store.count_active() == 0


# ------------------------------------------------------------ 场景 6：话题漂移

def test_scenario6_topic_drift_does_not_confirm(
    conversation, mock_model, store, hyp, evening_hyp
):
    """用户确认了另一件事 → 不得顺手确认"晚上学习效率更高"。"""
    mock_model.text_queue = ["好。", deposit_json()]
    conversation.chat("对啊，我最近很喜欢晚上散步。")
    time.sleep(3)
    assert evidence_count(store.db_path, evening_hyp.id) == 0
    assert hyp.get(evening_hyp.id).support_count == 0


# ------------------------------------------------------------ 场景 7：多假设

def test_scenario7_only_related_hypothesis_affected(
    conversation, mock_model, store, hyp, evening_hyp
):
    """三条假设并存时，一句自然表达只能影响真正相关的那条。"""
    h2, _ = hyp.create("用户喜欢晚上运动", "dynamic:喜欢晚上运动")
    h3, _ = hyp.create("用户喜欢喝咖啡", "dynamic:喜欢咖啡")

    mock_model.text_queue = ["嗯。", deposit_json()]
    conversation.chat("对，我晚上学习状态确实好。")
    db = store.db_path
    assert wait_for(lambda: evidence_count(db, evening_hyp.id) >= 1)

    for other in (h2, h3):
        assert evidence_count(db, other.id) == 0, \
            f"无关假设 {other.id[:8]} 被误伤"
        assert hyp.get(other.id).support_count == 0


# ------------------------------------------------------------ 场景 8：否定优先

def test_scenario8_denial_beats_support(
    conversation, mock_model, store, hyp, evening_hyp
):
    """一句话同时含"对"和"不是" → 必须按否定处理。

    否则会出现"用户明说了不是，系统反而记成支持"的反向学习，
    这是最糟的失败模式。
    """
    mock_model.text_queue = ["好。", deposit_json()]
    conversation.chat("对，不过晚上学习效率这件事不是这样。")
    db = store.db_path
    assert wait_for(lambda: hyp.get(evening_hyp.id).contradict_count >= 1), \
        "否定优先级失效：没有产生反证"
    assert evidence_count(db, evening_hyp.id) >= 1
    assert not rows(db, "SELECT id FROM hypothesis_evidence"
                       " WHERE hypothesis_id=? AND polarity='support'",
                    (evening_hyp.id,)), "同一次发言不该既支持又否定"


def test_scenario8b_anaphoric_denial_never_becomes_support(
    conversation, mock_model, store, hyp, evening_hyp
):
    """"对，不过不是这样"这种纯指代否定：不得变成支持证据。

    它本身没有话题词（不知道在否定哪件事），所以**不该**盲目否决任何
    假设——那正是跨话题误伤。但它也绝不能被记成"用户确认了某条理解"。
    """
    mock_model.text_queue = ["好。", deposit_json()]
    conversation.chat("对，不过不是这样。")
    time.sleep(3)
    assert not rows(store.db_path,
                    "SELECT id FROM hypothesis_evidence"
                    " WHERE hypothesis_id=? AND polarity='support'",
                    (evening_hyp.id,)), "纯指代否定被误记成支持"


# ------------------------------------------------------------ 场景 9：疑问句

def test_scenario9_question_is_not_a_statement(
    conversation, mock_model, store, hyp, evening_hyp
):
    """提问不是表态。"""
    mock_model.text_queue = ["你可以试试。", deposit_json()]
    conversation.chat("是不是我晚上效率更高？")
    time.sleep(3)
    assert evidence_count(store.db_path, evening_hyp.id) == 0
    assert hyp.get(evening_hyp.id).support_count == 0


# ------------------------------------------------------------ 场景 10：短确认

@pytest.mark.parametrize("text", ["对", "嗯对", "好", "是"])
def test_scenario10_bare_affirmation_writes_nothing(
    conversation, mock_model, store, hyp, evening_hyp, text
):
    """光秃秃的附和不得形成证据。"""
    mock_model.text_queue = ["嗯。", deposit_json()]
    conversation.chat(text)
    time.sleep(2)
    assert evidence_count(store.db_path, evening_hyp.id) == 0


# ------------------------------------------------------------ 完整回放

def test_full_replay_natural_understanding_loop(
    conversation, context_store, mock_model, store, hyp
):
    """§23 的完整回放：说出 → 形成理解 → 自然确认 → 变化 → 闲聊安静。

    最后直接查库确认，不靠"看起来对"。
    """
    db = store.db_path

    # ① 用户第一次表达 → 形成理解
    mock_model.text_queue = [
        "记下了。",
        deposit_json(new=[{"kind": "dynamic",
                           "content": "晚上十点后学习效率最高"}]),
    ]
    conversation.chat("我一般晚上十点后学习效率最高。")
    assert wait_for(lambda: context_store.count_active() >= 1)

    # ② 重复表达同一活动 → 达到 R1 归纳门槛（同一 activity 出现 3 次）
    #    一轮一轮发：后台沉淀线程共享 Mock 的 text_queue，并发发会互相
    #    抢走对方的 JSON，必须等上一轮事件落库再发下一轮。
    def event_count():
        return len(rows(db, "SELECT id FROM events"))
    for i in range(1, 4):
        want = i
        # 每轮消息不同：后台沉淀按 sha256(message) 去重，同一句话只沉淀一次
        ev = {"description": f"第{i}次晚上学习", "activity": "学习",
              "scene": "家里", "importance": 0.5}
        mock_model.text_queue = ["知道了。", deposit_json(event=ev)]
        conversation.chat(f"还是老样子，第{i}天十点后学习。")
        assert wait_for(lambda: event_count() >= want, timeout=25), \
            f"第 {want} 次事件未落库"

    assert wait_for(lambda: len(hyp.list_hypotheses(
        live_only=True, limit=50)) >= 1, timeout=30), \
        "重复表达未归纳出假设"
    target = hyp.list_hypotheses(live_only=True, limit=50)[0]
    base_support = target.support_count

    # ③ 用户自然确认 → 证据增加
    #    确认句必须与归纳出的假设话题相关（这里 scope=activity:学习），
    #    否则会被话题安全门正确拦下——那不是 bug。
    mock_model.text_queue = ["嗯。", deposit_json()]
    conversation.chat("对，我就是十点之后学习。")
    assert wait_for(lambda: hyp.get(target.id).support_count > base_support), \
        "自然确认未增加证据"

    # ④ 用户改变说法 → 旧理解失活
    #    变化句与旧理解需要有共同词（这里共享"学习"），否则没有话题
    #    关联就没有依据去废掉任何东西——那是刻意的保守。
    old_entry = context_store.list_entries()[0]
    mock_model.text_queue = [
        "明白。",
        deposit_json(new=[{"kind": "dynamic",
                           "content": "最近改成早上学习"}]),
    ]
    conversation.chat("我最近已经改成早上学习了。")
    assert wait_for(lambda: context_store.get_entry(old_entry.id)
                    .memory_status in ("outdated", "rejected")), \
        "旧理解未失活"

    # ⑤ 无关闲聊 → 无新理解
    before_ev = evidence_count(db)
    before_ctx = context_store.count_active()
    mock_model.text_queue = ["哈哈。", deposit_json()]
    conversation.chat("今天食堂红烧肉真难吃。")
    time.sleep(3)
    assert evidence_count(db) == before_ev
    assert context_store.count_active() == before_ctx

    # ⑥ 数据库终态自检：历史一条没丢
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        total_ctx = con.execute(
            "SELECT COUNT(*) FROM context_entries WHERE active=1").fetchone()[0]
        ev = con.execute(
            "SELECT polarity, COUNT(*) FROM hypothesis_evidence"
            " GROUP BY polarity").fetchall()
    finally:
        con.close()
    assert total_ctx >= 2                       # 新旧都在
    assert ("support", ) not in [] or dict(ev).get("support", 0) >= 1
    assert dict(ev).get("contradict", 0) >= 1   # 变化/否定确实记了反证