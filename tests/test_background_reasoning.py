"""Background Reasoning（V0.5.8）。

三层：
1. 规则纯函数——数据足够才生成，够了就生成；
2. 安全——background: 候选处处与 quota_overflow 同构（不 prompt /
   不 pattern / 不 confirmed / 不吃自然证据 / 用户裁决后才动）；
3. 反馈闭环——说"对"只补证据，说"不对"从此不再提。

最重要的一条贯穿全文件：**后台推理只能提，不能用。**
"""
from __future__ import annotations

import time
import uuid

import pytest

from core.background_bridge import run_background_reasoning
from core.background_reasoning import (
    BACKGROUND_PREFIX,
    MIN_EVENTS_PER_WINDOW,
    Candidate,
    detect_behavior_change,
    detect_focus,
    detect_habit_drift,
    reason_all,
    slot_of,
    topic_of,
)
from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel

NOW = 1_800_000_000.0
DAY = 86400.0


def ev(days_ago: float, description: str, activity: str = "自习"):
    return {
        "created_at_epoch": NOW - days_ago * DAY,
        "description": description,
        "activity": activity,
        "scene": "家里",
    }


def night_study(days_ago):
    return ev(days_ago, "晚上写数学作业", "自习")


def morning_study(days_ago):
    return ev(days_ago, "早上六点起来写数学作业", "自习")


# ============================================================ 时段识别

def test_slot_of_is_recognised():
    assert slot_of("晚上写数学作业") == "晚上"
    assert slot_of("早上六点起来") == "早晨"
    assert slot_of("上午的课") == "上午"
    assert slot_of("下午开会") == "下午"


def test_slot_of_refuses_to_guess():
    """认不出就是认不出——硬凑时段是后台推理最容易犯的错。"""
    assert slot_of("今天食堂红烧肉真难吃") == ""
    assert slot_of("") == ""
    assert slot_of(None) == ""


def test_topic_prefers_activity():
    assert topic_of(ev(1, "晚上写数学作业", "自习")) == "自习"
    assert topic_of({"description": "随便一句", "activity": ""}) == "随便一句"


# ============================================================ Rule 1

def test_rule1_detects_slot_shift():
    """老窗口全是晚上，新窗口全是早晨 → 生成一条推测。"""
    events = [night_study(d) for d in (16, 15, 14, 13, 12)] + \
             [morning_study(d) for d in (6, 5, 4, 3, 2, 1)]
    out = detect_behavior_change(events, NOW)
    assert len(out) == 1
    c = out[0]
    assert c.type == "behavior_change"
    assert c.scope.startswith(BACKGROUND_PREFIX)
    assert "晚上" in c.reason and "早晨" in c.reason
    assert c.evidence, "推测必须带得上依据"


def test_rule1_stays_quiet_when_sample_too_small():
    """老窗口 2 次、新窗口 2 次 → 不生成。样本不够讲不了趋势。"""
    events = [night_study(16), night_study(15),
              morning_study(6), morning_study(5)]
    assert detect_behavior_change(events, NOW) == []


def test_rule1_stays_quiet_without_a_shift():
    """两窗口时段没变 → 不是变化，不该被讲成变化。"""
    events = [night_study(d) for d in (16, 15, 14, 6, 5, 4)]
    assert detect_behavior_change(events, NOW) == []


def test_rule1_stays_quiet_when_one_side_is_noise():
    """新窗口只有 1 次早晨 → 单点不算变化。"""
    events = [night_study(d) for d in (16, 15, 14, 13, 12)] + \
             [morning_study(1)]
    assert detect_behavior_change(events, NOW) == []


def test_rule1_candidate_confidence_is_always_low():
    """猜测就该看起来像猜测——永远低于 INIT_CONFIDENCE(0.5)。"""
    events = [night_study(d) for d in (16, 15, 14, 13, 12)] + \
             [morning_study(d) for d in (6, 5, 4, 3, 2, 1)]
    for c in detect_behavior_change(events, NOW):
        assert c.confidence < 0.5, c.confidence


# ============================================================ Rule 2

def test_rule2_detects_drift_of_an_existing_understanding():
    events = [night_study(d) for d in (16, 15, 14, 13)] + \
             [morning_study(d) for d in (6, 5, 4, 3)]
    old = {"scope": "dynamic:自习", "status": "tentative", "confidence": 0.66}
    out = detect_habit_drift(events, [old], NOW)
    assert len(out) == 1
    assert out[0].type == "habit_drift"
    assert out[0].scope.startswith(BACKGROUND_PREFIX)


def test_rule2_needs_a_standing_understanding():
    """旧理解自己还没站稳（置信度低），不许拿它推漂移。"""
    events = [night_study(d) for d in (16, 15, 14, 13)] + \
             [morning_study(d) for d in (6, 5, 4, 3)]
    weak = {"scope": "dynamic:自习", "status": "tentative", "confidence": 0.5}
    assert detect_habit_drift(events, [weak], NOW) == []


def test_rule2_ignores_background_and_quota_scopes():
    """推测不能推推测，配额溢出材料也不能拿来推。"""
    events = [night_study(d) for d in (16, 15, 14, 13)] + \
             [morning_study(d) for d in (6, 5, 4, 3)]
    for scope in ("background:behavior_change:自习", "quota_overflow:自习"):
        h = {"scope": scope, "status": "tentative", "confidence": 0.9}
        assert detect_habit_drift(events, [h], NOW) == [], scope


# ============================================================ Rule 3

def test_rule3_detects_focus():
    events = [ev(d, f"写 AI 项目代码 {d}", "AI项目") for d in (1, 2, 3, 4, 5)]
    out = detect_focus(events, NOW)
    assert len(out) == 1
    assert out[0].type == "focus_shift"
    assert "AI项目" in out[0].topic


def test_rule3_needs_three_records():
    events = [ev(1, "写代码", "编程"), ev(2, "写代码", "编程")]
    assert detect_focus(events, NOW) == []


def test_rule3_picks_the_dominant_topic():
    """占比高的才是"主要投入"——少数派不该被当成重心。"""
    events = [ev(d, f"写代码 {d}", "编程") for d in (1, 2, 3)] + \
             [ev(d, f"玩游戏 {d}", "游戏") for d in (1, 2, 3, 4, 5, 6)]
    out = detect_focus(events, NOW)
    assert len(out) == 1
    assert out[0].topic == "游戏", "把少数派当成重心了"


def test_rule3_stays_quiet_without_dominance():
    """谁都不占一半 → 没有"主要方向"，不该硬挑一个。"""
    events = [ev(d, f"写代码 {d}", "编程") for d in (1, 2, 3, 4)] + \
             [ev(d, f"玩游戏 {d}", "游戏") for d in (1, 2, 3, 4)] + \
             [ev(d, f"看书 {d}", "阅读") for d in (1, 2)]
    assert detect_focus(events, NOW) == []


# ============================================================ 总入口

def test_reason_all_is_deterministic():
    """纯函数：同样的输入永远同样的输出（回放的前提）。"""
    events = [night_study(d) for d in (16, 15, 14, 13, 12)] + \
             [morning_study(d) for d in (6, 5, 4, 3, 2, 1)]
    a = [c.to_dict() for c in reason_all(events, [], NOW)]
    b = [c.to_dict() for c in reason_all(events, [], NOW)]
    assert a == b


def test_reason_all_on_empty_data_returns_nothing():
    assert reason_all([], [], NOW) == []
    assert reason_all(None, None, NOW) == []


def test_reason_all_survives_broken_input():
    """一条规则坏了不许掀翻其余。"""
    events = [ev(d, f"写代码 {d}", "编程") for d in (1, 2, 3)]
    broken = [object()] * 5
    assert isinstance(reason_all(broken, [], NOW), list)
    assert isinstance(reason_all(events, [object()], NOW), list)


def test_candidate_is_frozen():
    """候选一旦生成就不许被别处悄悄改写。"""
    c = Candidate(type="focus_shift", topic="x", scope="background:x",
                  confidence=0.3, reason="r")
    with pytest.raises(Exception):
        c.confidence = 0.9          # type: ignore[misc]


# ============================================================ 落库与安全

@pytest.fixture
def world(tmp_path):
    db = tmp_path / "t.db"
    return {"db": db, "store": MemoryStore(db), "ctx": ContextStore(db),
            "hyp": HypothesisStore(db)}


def seed_events(store, count=8, days=(1, 2, 3, 4, 5, 6)):
    """用真实的 Event（pydantic，created_at 是 datetime）落库。

    刻意用真对象而不是 dict——早期版本只认 created_at_epoch，
    结果真实事件一个都进不了窗口，规则"安静地什么都不产出"。
    """
    from datetime import datetime, timedelta

    from memory.event import Event
    for i in range(count):
        d = days[i % len(days)]
        store.add_event(Event(
            id=uuid.uuid4().hex,
            created_at=datetime.fromtimestamp(NOW - d * DAY),
            source="chat", scene="家里", activity="AI项目",
            objects=[], description=f"早上六点起来写代码 {i}",
            importance=0.5, facts=[]))


def test_run_produces_tentative_candidate(world):
    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    assert out["proposed"], out
    h = hyp.get(out["proposed"][0]["id"])
    # V0.5.9：新推测落库就是 pending（等用户裁决），不是 tentative
    assert h.status == "pending"
    assert h.scope.startswith(BACKGROUND_PREFIX)


def test_background_never_enters_prompt(world):
    """安全要求 1：后台推理不能进 prompt。"""
    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    run_background_reasoning(store, hyp, now_epoch=NOW)
    assert hyp.list_for_prompt(limit=50) == [], "后台推测进了 prompt"


def test_background_never_becomes_confirmed(world):
    """安全要求 2：不能自己变 confirmed。"""
    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    h = hyp.get(out["proposed"][0]["id"])
    assert h.status in ("pending", "accepted")
    assert h.status != "confirmed"
    assert h.confidence < 0.5


def test_background_never_becomes_pattern(world):
    """安全要求 3：没有证据不能形成 pattern。"""
    from core.inducer import Inducer
    from core.pattern_engine import PatternEngine
    from memory.pattern_store import PatternStore

    db, store, hyp = world["db"], world["store"], world["hyp"]
    seed_events(store)
    ps = PatternStore(db)
    run_background_reasoning(store, hyp, now_epoch=NOW)
    for _ in range(3):
        Inducer(store, ContextStore(db), hyp).induce_all()
        PatternEngine(store, hyp, ps).promote_all()
    assert ps.list_patterns() == [], "后台推测变成了 pattern"


def test_background_is_not_naturally_confirmable(world):
    """安全要求 4：用户随口一句"对"不能把它确认掉。"""
    from core.natural_bridge import apply_natural_signal

    store, hyp, ctx = world["store"], world["hyp"], world["ctx"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    hid = out["proposed"][0]["id"]
    apply_natural_signal(ctx, hyp, "对，我就是最近主要在写代码。")
    # pending 候选还没人裁决，不允许被口头确认
    assert hyp.get(hid).support_count == 0
    assert hyp.get(hid).status == "pending"


def test_background_is_not_asked_about(world):
    """后台推测不发起验证提问——它本来就是没确认的猜测。"""
    from core.verifier import select_candidate

    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    got = select_candidate(hyp, "最近主要在写代码", paused=False, now=NOW)
    if got:
        assert got[0].id != out["proposed"][0]["id"]


def test_quota_overflow_still_blocked(world):
    """V0.5.3 的边界不能因为加了 background 而松动。"""
    from core.natural_bridge import apply_natural_signal

    hyp, ctx = world["hyp"], world["ctx"]
    h, _ = hyp.create("配额材料", "quota_overflow:自习")
    apply_natural_signal(ctx, hyp, "对，我就是最近主要在写代码。")
    assert hyp.get(h.id).support_count == 0


# ============================================================ 反馈闭环

def test_user_correct_adds_evidence_but_not_confirmed(world):
    """说"对"只补证据——用户点一下不构成系统能当事实的证据量。"""
    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    hid = out["proposed"][0]["id"]
    assert hyp.record_background_verdict(hid, "correct", "对的") is True
    h = hyp.get(hid)
    assert h.support_count == 1
    # V0.5.9：认可后进 accepted（等真实证据），而不是 confirmed
    assert h.status == "accepted", "认可一下就变 confirmed？"
    assert h.confidence < 0.5


def test_user_correct_still_stays_out_of_prompt(world):
    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    hyp.record_background_verdict(out["proposed"][0]["id"], "correct")
    assert hyp.list_for_prompt(limit=50) == []


def test_user_wrong_stops_future_proposals(world):
    """说"不对" → 落 rejected → 以后不再提。"""
    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    hid = out["proposed"][0]["id"]
    assert hyp.record_background_verdict(hid, "wrong", "不对") is True
    assert hyp.get(hid).status == "rejected"

    again = run_background_reasoning(store, hyp, now_epoch=NOW)
    assert again["proposed"] == [], "已否决的推测又提了一次"
    assert again["skipped"], "应该记录为什么跳过"


def test_same_scope_is_not_proposed_twice(world):
    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    a = run_background_reasoning(store, hyp, now_epoch=NOW)
    b = run_background_reasoning(store, hyp, now_epoch=NOW)
    assert a["proposed"]
    assert b["proposed"] == [], "同一推测重复提出"


def test_verdict_refuses_non_background_hypothesis(world):
    """裁决接口只对后台推测开放，不许拿来操作普通假设。"""
    hyp = world["hyp"]
    h, _ = hyp.create("普通理解", "dynamic:自习")
    assert hyp.record_background_verdict(h.id, "wrong") is False
    assert hyp.get(h.id).status == "tentative"


def test_unknown_verdict_is_refused(world):
    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    assert hyp.record_background_verdict(out["proposed"][0]["id"],
                                         "maybe") is False


def test_background_never_shows_up_as_understanding(world):
    """后台推测不许出现在「我眼中的你」。

    那里的每一条都是用户确认过的理解；把 AI 的猜测混进去，
    就是把猜测伪装成事实——这是这套系统唯一不能含糊的地方。
    """
    from core.understanding_view import (
        FORBIDDEN_IN_OUTPUT,
        assert_no_internal_fields,
        build_timeline,
    )

    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    assert out["proposed"]

    detail = hyp.get_detail(out["proposed"][0]["id"])
    tl = build_timeline([detail], [])
    assert tl["items"] == [], "后台推测混进了'我眼中的你'"
    assert "background:" not in repr(tl)
    assert "background:" in FORBIDDEN_IN_OUTPUT
    assert assert_no_internal_fields(tl) == []


def test_background_endpoint_does_not_leak_internal_fields(world, tmp_path,
                                                         monkeypatch):
    from core.understanding_view import FORBIDDEN_IN_OUTPUT
    from fastapi.testclient import TestClient

    from core.service_state import ServiceState
    from interfaces.webapp import create_app

    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    run_background_reasoning(store, hyp, now_epoch=NOW)
    app = create_app(store=store, model=MockChatModel(),
                     hypothesis_store=hyp,
                     service_state=ServiceState())
    body = TestClient(app).get("/api/background").text
    for w in FORBIDDEN_IN_OUTPUT:
        assert w not in body, f"后台面板泄漏内部字段：{w}"
    assert '"caveat"' in body


def test_store_layer_also_blocks_reshout(world):
    """store 层自己也要挡住重复提出。

    bridge 和 store 各有一道去重守卫；只测 bridge 的话，删掉
    store 那道仍然全绿——两层保护里有一层从未被直接验证过。
    """
    store, hyp = world["store"], world["hyp"]
    seed_events(store)
    out = run_background_reasoning(store, hyp, now_epoch=NOW)
    scope = out["proposed"][0]["scope"]

    # 直连 store：绕过 bridge 再问一次
    assert hyp.background_proposed(scope) is True
    assert hyp.create_background_candidate(
        Candidate(type="focus_shift", topic="x", scope=scope,
                  confidence=0.3, reason="重复的")) is None


def test_background_proposed_is_false_for_fresh_scope(world):
    hyp = world["hyp"]
    assert hyp.background_proposed("background:focus_shift:从没提过的东西") is False


def test_background_proposed_treats_empty_scope_as_already_proposed(world):
    """空 scope 一律当作"提过"——宁可不说，也不做无主题的推测。"""
    hyp = world["hyp"]
    assert hyp.background_proposed("") is True


def test_run_is_safe_when_store_explodes(world):
    """后台推理出问题绝不能掀翻用户对话。"""
    store = world["store"]
    out = run_background_reasoning(store, None)
    assert out["proposed"] == [] and out["error"] is None


# ============================================================ 真实链路

def test_full_story_notices_change_without_touching_old_understanding(world):
    """用户故事（V0.5.8 要证明的就是这一条）：

        过去：晚上学习     → 系统形成理解
        最近：连续早晨学习 → 后台发现变化
        用户看到：可能变化（而不是事实）
        而且：旧理解还在

    最后一条断言是重点——后台推理"发现"了东西，不代表它有权
    "修改"任何已有的东西。
    """
    from datetime import datetime

    from core.background_bridge import run_background_reasoning as run
    from core.inducer import Inducer
    from memory.event import Event

    store, hyp, ctx = world["store"], world["hyp"], world["ctx"]

    def add(days_ago, text):
        store.add_event(Event(
            id=uuid.uuid4().hex,
            created_at=datetime.fromtimestamp(NOW - days_ago * DAY),
            source="chat", scene="家里", activity="学习",
            objects=[], description=text, importance=0.5, facts=[]))

    # ① 过去晚上学习，走真实归纳形成一条正式理解
    for d in (16, 15, 14, 13):
        add(d, f"晚上写数学作业 第{d}天")
    Inducer(store, ctx, hyp).induce_all()
    old_hyps = [h for h in hyp.list_hypotheses(live_only=True, limit=50)
                if not h.scope.startswith(BACKGROUND_PREFIX)]
    assert old_hyps, "旧理解没形成，故事不成立"
    old_before = hyp.get(old_hyps[0].id)

    # ② 最近连续早晨学习
    for d in (6, 5, 4, 3, 2):
        add(d, f"早上六点起来写数学作业 第{d}天")

    # ③ 后台推理发现变化
    out = run(store, hyp, now_epoch=NOW)
    assert out["proposed"], "后台推理没发现时段变化"
    cand = hyp.get(out["proposed"][0]["id"])
    assert cand.scope.startswith(BACKGROUND_PREFIX)
    assert cand.status == "pending"
    assert cand.confidence < 0.5

    # ④ 旧理解一点没被动过
    old_after = hyp.get(old_before.id)
    assert old_after.status == old_before.status
    assert old_after.confidence == old_before.confidence
    assert old_after.contradict_count == old_before.contradict_count

    # ⑤ 推测进不了 prompt
    assert all(h.id != cand.id for h in hyp.list_for_prompt(limit=50))


def test_story_second_run_does_not_reshout(world):
    """同一段故事再跑一次，不该把同一条推测再摆一遍给用户。"""
    from datetime import datetime

    from core.background_bridge import run_background_reasoning as run
    from memory.event import Event

    store, hyp = world["store"], world["hyp"]
    for d in (6, 5, 4, 3, 2):
        store.add_event(Event(
            id=uuid.uuid4().hex,
            created_at=datetime.fromtimestamp(NOW - d * DAY),
            source="chat", scene="家里", activity="学习",
            objects=[], description=f"早上六点起来写数学作业 {d}",
            importance=0.5, facts=[]))
    first = run(store, hyp, now_epoch=NOW)
    second = run(store, hyp, now_epoch=NOW)
    assert first["proposed"]
    assert second["proposed"] == []
    # bridge 那道检查不是为了拦（store 也会拦），是为了**说清楚为什么跳过**。
    # 用户看得见的可追溯性就靠这条 reason。
    assert any("已提过" in s.get("why", "") for s in second["skipped"]), \
        f"跳过原因没记录清楚：{second['skipped']}"