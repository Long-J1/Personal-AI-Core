"""Background Reasoning Lifecycle（V0.5.9）。

V0.5.8 的候选只会停在 tentative——用户点"对"了它也不长进。
V0.5.9 把它变成一条真生命周期：

    pending → accepted → confirmed（毕业成正式理解）
             → rejected（用户否定）
    pending/accepted → expired（长期无支持）

外加：语义去重、idle 整理、成长靠**真实**用户证据。

贯穿全文件的判据：**候选的每一步都必须由用户的真实行为推动，
不能由 AI 自己推着走。**
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime

import pytest

from core.background_bridge import list_background_candidates
from core.background_reasoning import BACKGROUND_PREFIX, Candidate
from core.idle_worker import IdleWorker
from core.natural_bridge import apply_natural_signal
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.store import MemoryStore

DAY = 86400.0
NOW = 1_800_000_000.0


def seed(store, n=6, days=(1, 2, 3, 4, 5, 6), activity="学习",
         desc="早上六点起来写数学作业"):
    for i in range(n):
        d = days[i % len(days)]
        store.add_event(_event(NOW - d * DAY, desc, activity))


def _event(ts, description, activity):
    from memory.event import Event
    return Event(id=uuid.uuid4().hex,
                 created_at=datetime.fromtimestamp(ts),
                 source="chat", scene="家里", activity=activity,
                 objects=[], description=description, importance=0.5,
                 facts=[])


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "t.db"
    return {"db": db, "store": MemoryStore(db), "ctx": ContextStore(db),
            "hyp": HypothesisStore(db)}


def make_candidate(reason="最近 7 天有 6 次记录是「学习」，占 6/6。只是推测。",
                   topic="学习", scope=None, kind="focus_shift"):
    return Candidate(type=kind, topic=topic,
                     scope=scope or f"{BACKGROUND_PREFIX}{kind}:{topic}",
                     confidence=0.40, reason=reason,
                     evidence=("早上六点起来写数学作业",))


# ============================================================ 1 生命周期

def test_new_candidate_is_pending_not_tentative(world):
    """pending 才有意义：tentative 是"系统自己的猜测"，
    pending 明确表达"在等用户裁决"。"""
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    assert c is not None
    assert c.status == "pending"
    assert hyp.get(c.id).status == "pending"


def test_pending_is_not_live(world):
    """pending 不进 LIVE_STATUSES——它在"用户还没说话"的范畴里。"""
    from memory.hypothesis_store import LIVE_STATUSES
    assert "pending" not in LIVE_STATUSES
    assert "accepted" not in LIVE_STATUSES
    assert "expired" not in LIVE_STATUSES


def test_user_correct_moves_pending_to_accepted(world):
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    assert hyp.record_background_verdict(c.id, "correct", "\u5bf9\u7684") is True
    h = hyp.get(c.id)
    assert h.status == "accepted"
    assert h.support_count == 1


def test_user_wrong_moves_to_rejected(world):
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    assert hyp.record_background_verdict(c.id, "wrong", "\u4e0d\u5bf9") is True
    assert hyp.get(c.id).status == "rejected"


def test_accepted_never_becomes_confirmed_by_one_verdict(world):
    """认可一次 ≠ 事实。用户点一下不构成系统能拿它当事实的证据量。"""
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    hyp.record_background_verdict(c.id, "correct")
    assert hyp.get(c.id).status == "accepted"
    assert hyp.get(c.id).confidence < 0.5


def test_unknown_verdict_is_refused(world):
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    assert hyp.record_background_verdict(c.id, "maybe") is False
    assert hyp.get(c.id).status == "pending"


# ============================================================ 2 后续证据积累

def test_accepted_candidate_grows_from_real_natural_evidence(world):
    """核心能力：accepted 候选靠**真实**用户证据长大。

    这条路径是它唯一的长大方式——AI 不推着它走。
    """
    hyp, ctx = world["hyp"], world["ctx"]
    c = hyp.create_background_candidate(make_candidate(
        reason="最近 7 天有 6 次记录是「学习」，占 6/6。只是推测。",
        topic="学习", kind="focus_shift"))
    hyp.record_background_verdict(c.id, "correct")
    before = hyp.get(c.id)

    apply_natural_signal(ctx, hyp, "\u5bf9\uff0c\u6211\u786e\u5b9e\u6bcf\u5929\u90fd\u5728\u5b66\u4e60\u3002")

    after = hyp.get(c.id)
    assert after.support_count > before.support_count, "accepted 候选吃不到真实证据"
    assert after.confidence > before.confidence
    assert after.status == "accepted", "积累证据不该直接跳 confirmed"


def test_pending_candidate_does_not_take_natural_evidence(world):
    """还没人裁决的猜测不许被一句"对"顺手确认。"""
    hyp, ctx = world["hyp"], world["ctx"]
    c = hyp.create_background_candidate(make_candidate())
    apply_natural_signal(ctx, hyp, "\u5bf9\uff0c\u6211\u786e\u5b9e\u6bcf\u5929\u90fd\u5728\u5b66\u4e60\u3002")
    h = hyp.get(c.id)
    assert h.support_count == 0
    assert h.status == "pending"


def test_accepted_candidate_still_not_in_prompt(world):
    """证据在长，但它还是候选——不进 prompt。"""
    hyp, ctx = world["hyp"], world["ctx"]
    c = hyp.create_background_candidate(make_candidate())
    hyp.record_background_verdict(c.id, "correct")
    apply_natural_signal(ctx, hyp, "\u5bf9\uff0c\u6211\u786e\u5b9e\u6bcf\u5929\u90fd\u5728\u5b66\u4e60\u3002")
    assert all(h.id != c.id for h in hyp.list_for_prompt(limit=50))


# ============================================================ 毕业

def test_graduation_requires_enough_independent_evidence(world):
    """毕业门槛直接用既有的 HIGH_CONF_CAP / MIN_INDEPENDENT_FOR_HIGH。

    置信度要达到 0.70，add_evidence 本身就要求至少两个独立来源——
    所以"毕业"天然等价于"至少两天不同的真实证据"。
    """
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    hyp.record_background_verdict(c.id, "correct")
    assert hyp.graduate_candidates() == [], "证据不够就毕业了"
    assert hyp.get(c.id).status == "accepted"


def _grow(hyp, ctx, cand_id: str, times: int = 4) -> None:
    """用**真实**用户表达把候选养到毕业线。

    候选起点 0.40，毕业线是既有的 HIGH_CONF_CAP(0.70)，
    每条真实支持 +0.08 —— 所以需要 4 次跨天支持。
    这是设计意图：候选必须被反复印证，
    而不是被一次认可就放行。
    """
    import sqlite3
    from contextlib import closing

    from datetime import datetime
    phrases = [
        "\u5bf9\uff0c\u6211\u786e\u5b9e\u6bcf\u5929\u90fd\u5728\u5b66\u4e60\u3002",
        "\u5bf9\uff0c\u6211\u6b63\u5728\u5b66\u4e60\u3002",
        "\u5bf9\uff0c\u6211\u786e\u5b9e\u5728\u5b66\u4e60\u3002",
        "\u5bf9\uff0c\u6211\u6bcf\u5929\u90fd\u5728\u5b66\u4e60\u3002",
    ]
    for i in range(times):
        day = 3.0 * (i + 1)
        ts = time.time() - day * DAY
        with closing(sqlite3.connect(hyp.db_path)) as con:
            con.execute("UPDATE hypotheses SET updated_at_epoch=?,"
                        " updated_at_iso=? WHERE id=?",
                        (ts, datetime.fromtimestamp(ts).isoformat(), cand_id))
            con.execute("UPDATE hypothesis_evidence SET origin=?"
                        " WHERE id=(SELECT id FROM hypothesis_evidence"
                        " WHERE hypothesis_id=? ORDER BY created_at_epoch"
                        " DESC LIMIT 1)",
                        (datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
                         cand_id))
            con.commit()
        apply_natural_signal(ctx, hyp, phrases[i % len(phrases)])


def test_graduation_happens_when_evidence_is_sufficient(world):
    """反复印证 + 认可 → 毕业为正式理解。"""
    hyp, ctx = world["hyp"], world["ctx"]
    c = hyp.create_background_candidate(make_candidate(
        reason="最近 7 天有 6 次记录是「学习」，占 6/6。只是推测。",
        topic="学习", kind="focus_shift"))
    hyp.record_background_verdict(c.id, "correct")
    _grow(hyp, ctx, c.id)

    graduated = hyp.graduate_candidates()
    assert graduated == [c.id], \
        f"证据够了却没毕业：conf={hyp.get(c.id).confidence}"
    h = hyp.get(c.id)
    assert h.status == "confirmed"
    assert not h.scope.startswith(BACKGROUND_PREFIX), \
        "毕业了还留在 background 前缀里"


def test_graduated_candidate_enters_prompt(world):
    """毕业 = 正式理解。它现在应该能进 prompt 了。"""
    hyp, ctx = world["hyp"], world["ctx"]
    c = hyp.create_background_candidate(make_candidate(
        reason="最近 7 天有 6 次记录是「学习」，占 6/6。只是推测。",
        topic="学习", kind="focus_shift"))
    hyp.record_background_verdict(c.id, "correct")
    _grow(hyp, ctx, c.id)
    hyp.graduate_candidates()
    assert any(h.id == c.id for h in hyp.list_for_prompt(limit=50))


def test_graduation_needs_two_independent_days_not_just_high_confidence(world):
    """同一天反复说的话不算独立证据。

    `add_evidence` 在独立源 < 2 时把置信度**封顶到 0.70**（min 而非
    减一），所以"conf 正好 0.70 且只有一个来源"是完全可达的状态。
    毕业检查里的独立源判定存在的唯一理由就是拦住它——
    如果只测"证据不够"，这条检查删掉也没人发现。
    """
    import sqlite3
    from contextlib import closing

    from datetime import datetime

    hyp, ctx = world["hyp"], world["ctx"]
    c = hyp.create_background_candidate(make_candidate(
        reason="最近 7 天有 6 次记录是「学习」，占 6/6。只是推测。",
        topic="学习", kind="focus_shift"))
    hyp.record_background_verdict(c.id, "correct")

    phrases = ["对，我确实每天都在学习。", "对，我正在学习。",
               "对，我确实在学习。", "对，我每天都在学习。"]
    day = datetime(2026, 10, 1).strftime("%Y-%m-%d")
    for p in phrases:
        apply_natural_signal(ctx, hyp, p)
        # 全部按同一天算 → 独立源恒为 1
        with closing(sqlite3.connect(hyp.db_path)) as con:
            con.execute("UPDATE hypothesis_evidence SET origin=? WHERE id="
                        "(SELECT id FROM hypothesis_evidence WHERE"
                        " hypothesis_id=? ORDER BY created_at_epoch DESC"
                        " LIMIT 1)", (day, c.id))
            con.commit()
    # 认可那条证据的 origin 也归到同一天，否则它自成一个独立源
    with closing(sqlite3.connect(hyp.db_path)) as con:
        con.execute("UPDATE hypothesis_evidence SET origin=?"
                    " WHERE hypothesis_id=?", (day, c.id))
        con.commit()

    h = hyp.get(c.id)
    assert h.confidence >= 0.70, f"没到封顶线，测不到边界：{h.confidence}"
    assert hyp.independent_support_count(c.id) == 1
    assert hyp.graduate_candidates() == [], "同一天的重复发言就把候选放行了"
    assert hyp.get(c.id).status == "accepted"


def test_store_layer_blocks_natural_evidence_for_pending(world):
    """存储层自己也要挡住 pending。

    bridge 与 store 各有一道守卫；只测端到端的话，删掉其中任何一道
    另一道都会兜住，mutation 测不出"这道到底有没有被验证过"。
    """
    from memory.hypothesis_store import HypothesisStore as HS
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    assert HS._natural_eligible("pending", c.scope) is False
    assert HS._natural_eligible("accepted", c.scope) is True
    assert HS._natural_eligible("accepted",
                               "quota_overflow:学习") is False


def test_bridge_layer_blocks_pending_itself(world):
    """bridge 侧同样要挡 pending、放行 accepted。"""
    from core.natural_bridge import _eligible_hypotheses

    hyp = world["hyp"]
    p = hyp.create_background_candidate(make_candidate())
    assert [h.id for h in _eligible_hypotheses(hyp)] == []
    hyp.record_background_verdict(p.id, "correct")
    assert [h.id for h in _eligible_hypotheses(hyp)] == [p.id]


def test_rejected_candidate_never_graduates(world):
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    hyp.record_background_verdict(c.id, "wrong")
    assert hyp.graduate_candidates() == []
    assert hyp.get(c.id).status == "rejected"


def test_graduation_pauses_with_privacy_paused(world):
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    hyp.record_background_verdict(c.id, "correct")
    with hyp._connect() as con:      # noqa: SIM115 —— 测试直接写 meta
        con.execute("INSERT OR REPLACE INTO meta (key,value)"
                    " VALUES ('privacy_paused','1')")
    try:
        assert hyp.graduate_candidates() == []
    finally:
        with hyp._connect() as con:  # noqa: SIM115
            con.execute("UPDATE meta SET value='0' WHERE key='privacy_paused'")


# ============================================================ 3 去重 / 淘汰

def test_semantic_dedup_blocks_reworded_duplicate(world):
    """换了措辞的同一件事不该摆两遍。"""
    hyp = world["hyp"]
    a = hyp.create_background_candidate(make_candidate(
        reason="最近 7 天有 6 次记录是「学习」，占 6/6。只是推测。"))
    assert a is not None
    b = hyp.create_background_candidate(make_candidate(
        reason="最近七天有六次记录是「学习」，占比 6/6。只是推测。",
        scope=f"{BACKGROUND_PREFIX}behavior_change:学习"))
    assert b is None, "语义重复的候选又被提了一次"


def test_semantic_dedup_allows_different_topic(world):
    hyp = world["hyp"]
    a = hyp.create_background_candidate(make_candidate(
        reason="最近 7 天有 6 次记录是「学习」，占 6/6。只是推测。"))
    assert a is not None
    b = hyp.create_background_candidate(make_candidate(
        reason="最近 7 天有 5 次记录是「编程」，占 5/6。只是推测。",
        topic="编程"))
    assert b is not None, "不同话题被误判成重复"


def test_stale_candidate_expires(world):
    """长期没人理 → 淘汰，但**不删行**。"""
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    _age(hyp, c.id, days=45)
    assert hyp.expire_stale_candidates(stale_days=30) == [c.id]
    h = hyp.get(c.id)
    assert h.status == "expired"
    assert h is not None, "淘汰不该删历史——用户确实说过这些话"


def test_fresh_candidate_is_not_expired(world):
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    assert hyp.expire_stale_candidates(stale_days=30) == []
    assert hyp.get(c.id).status == "pending"


def test_accepted_but_stale_also_expires(world):
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    hyp.record_background_verdict(c.id, "correct")
    _age(hyp, c.id, days=45)
    assert hyp.expire_stale_candidates(stale_days=30) == [c.id]
    assert hyp.get(c.id).status == "expired"


def test_expired_candidate_is_not_reproposed(world):
    """淘汰过的不要再冒出来。"""
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    _age(hyp, c.id, days=45)
    hyp.expire_stale_candidates(stale_days=30)
    assert hyp.create_background_candidate(make_candidate()) is None


def test_normal_hypotheses_are_never_expired(world):
    """淘汰只针对 background: 候选——普通理解不能被时间冲掉。"""
    hyp = world["hyp"]
    h, _ = hyp.create("普通理解", "activity:学习")
    _age(hyp, h.id, days=999)
    hyp.expire_stale_candidates(stale_days=30)
    assert hyp.get(h.id).status == "tentative"


def _age(hyp_store, hypo_id: str, days: float) -> None:
    import sqlite3
    from contextlib import closing

    old = time.time() - days * DAY
    with closing(sqlite3.connect(hyp_store.db_path)) as con:
        con.execute("UPDATE hypotheses SET updated_at_epoch=?,"
                    " updated_at_iso=? WHERE id=?",
                    (old, datetime.fromtimestamp(old).isoformat(), hypo_id))
        con.commit()


# ============================================================ 4 idle worker

@pytest.fixture
def idle_world(world):
    db = world["db"]
    chat = ChatStore(db)
    w = IdleWorker(world["store"], world["hyp"], chat,
                   idle_seconds=60, cooldown_seconds=120, poll_seconds=0.01)
    return {**world, "chat": chat, "worker": w}


def _touch(chat, seconds_ago: float) -> None:
    import sqlite3
    from contextlib import closing

    t = time.time() - seconds_ago
    with closing(sqlite3.connect(chat.db_path)) as con:
        con.execute("INSERT OR REPLACE INTO conversations"
                    " (id, created_iso, updated_epoch) VALUES (?,?,?)",
                    ("s1", datetime.fromtimestamp(t).isoformat(), t))
        con.commit()


def test_idle_worker_does_not_run_while_user_is_active(idle_world):
    """用户还在用 → 一次都不跑。"""
    _touch(idle_world["chat"], 5)
    out = asyncio.run(idle_world["worker"].tick())
    assert out["ran"] is False
    assert out["reason"] == "用户还在用"


def test_idle_worker_runs_after_idle(idle_world):
    _touch(idle_world["chat"], 3600)
    out = asyncio.run(idle_world["worker"].tick())
    assert out["ran"] is True


def test_idle_worker_respects_cooldown(idle_world):
    _touch(idle_world["chat"], 3600)
    w = idle_world["worker"]
    assert asyncio.run(w.tick())["ran"] is True
    out = asyncio.run(w.tick())
    assert out["ran"] is False
    assert out["reason"] == "冷却中"


def test_idle_worker_does_nothing_without_any_conversation(idle_world):
    """从没聊过天 → 不制造任何东西。"""
    out = asyncio.run(idle_world["worker"].tick())
    assert out["ran"] is False
    assert out["reason"] == "还没有对话记录"


def test_idle_worker_passes_when_privacy_paused(idle_world):
    _touch(idle_world["chat"], 3600)
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(idle_world["db"])) as con:
        con.execute("INSERT OR REPLACE INTO meta (key,value)"
                    " VALUES ('privacy_paused','1')")
        con.commit()
    out = asyncio.run(idle_world["worker"].tick())
    assert out["ran"] is False
    assert out["reason"] == "隐私暂停中"


def test_idle_worker_does_not_block_the_event_loop(idle_world):
    """整理跑在线程池里——事件循环不能被占住。"""
    _touch(idle_world["chat"], 3600)

    async def scenario():
        ticks = []
        w = idle_world["worker"]

        async def heartbeat():
            for _ in range(5):
                await asyncio.sleep(0.005)
                ticks.append(1)

        hb = asyncio.create_task(heartbeat())
        out = await w.tick()
        await hb
        return out, len(ticks)

    out, ticks = asyncio.run(scenario())
    assert out["ran"] is True
    assert ticks >= 4, "整理把事件循环堵住了"


def test_idle_worker_start_is_idempotent_and_stop_cancels(idle_world):
    w = idle_world["worker"]

    async def scenario():
        assert w.start() is True
        assert w.start() is False, "重复启动了"
        assert w._task is not None
        await w.stop()
        return True

    assert asyncio.run(scenario())
    assert w._task is None


def test_idle_worker_start_is_a_noop_without_event_loop(idle_world):
    assert idle_world["worker"].start() is False


def test_idle_worker_never_calls_the_model(idle_world, monkeypatch):
    """不调云端模型是硬要求——用"任何模型调用都炸"来证明。"""
    _touch(idle_world["chat"], 3600)

    def boom(*a, **k):
        raise AssertionError("idle worker 碰了模型")

    import core.background_bridge as bb
    import core.background_reasoning as br
    monkeypatch.setattr(bb, "reason_all", boom)
    monkeypatch.setattr(br, "reason_all", boom)
    out = asyncio.run(idle_world["worker"].tick())
    assert out["ran"] is True          # 跑过了，没被炸


def test_idle_worker_survives_store_errors(idle_world, monkeypatch):
    """后台崩了不许掀翻服务。"""
    _touch(idle_world["chat"], 3600)

    class Boom:
        def list_hypotheses(self, **k):
            raise RuntimeError("炸了")

        def paused(self):
            return False

    w = IdleWorker(idle_world["store"], Boom(), idle_world["chat"],
                   idle_seconds=60, cooldown_seconds=0, poll_seconds=0.01)
    out = asyncio.run(w.tick())
    assert out["ran"] is True


def test_background_endpoint_shows_pending_and_accepted(world):
    """UI 要能看到 pending 与 accepted 两种状态。"""
    hyp = world["hyp"]
    a = hyp.create_background_candidate(make_candidate())
    b = hyp.create_background_candidate(make_candidate(
        reason="最近 7 天有 5 次记录是「编程」，占 5/6。只是推测。",
        topic="编程"))
    hyp.record_background_verdict(b.id, "correct")
    got = {h.id for h in list_background_candidates(hyp)}
    assert got == {a.id, b.id}


def test_expired_candidate_disappears_from_listing(world):
    hyp = world["hyp"]
    c = hyp.create_background_candidate(make_candidate())
    _age(hyp, c.id, days=45)
    hyp.expire_stale_candidates(stale_days=30)
    assert list_background_candidates(hyp) == []