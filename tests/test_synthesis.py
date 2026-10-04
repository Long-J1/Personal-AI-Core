"""Deep Understanding Phase 1：Cross-Context Synthesis。

核心命题（这个文件整体在证明它）：

    系统不只是记住"用户发生过什么"，
    而是能从**多个不同经历**里发现一个更高层的模式，
    而且知道**什么时候证据还不够**。

分五层测，从纯逻辑一路走到真实链路：

1. **关系判定**——三个经历凭什么算"有关系"。共享词汇不算关系。
2. **证据独立性**——同一天说四遍是一个源，不是四个源。
3. **候选保护**——pending/accepted/rejected 一律不进 prompt，
   不影响 Judgment，不参与 Pattern，「我眼中的你」里不许露出。
4. **变化**——旧模式可能不成立，但历史必须留着。
5. **真实 replay**——走完整 conversation 链路，A/B 两个世界对比。

写这个文件时被真问题咬了三次，都写在对应测试的注释里：
- 变化检测的"之后"取错基准（用候选创建时刻而非证据时刻）
- `create` 的 detail 嵌套了一层，导致 `evidence_until` 根本读不到
- 反向测试（断言"不该出现"）对"删功能"型变异天然免疫
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import tempfile

import pytest

from core.synthesis import (
    DECAY_MIN_SAMPLES,
    MAX_CONFIDENCE,
    MIN_INDEPENDENT_DAYS,
    SYNTHESIS_PREFIX,
    Member,
    SynthesisCandidate,
    detect_pattern_decay,
    independent_days,
    is_synthesis,
    shared_behavior,
    synthesize,
    synthesize_exploration_depth,
    synthesize_feedback_continuity,
    synthesize_goal_impact,
)

DAY = 86400.0
NOW = 1_800_000_000.0        # 固定基准时刻，避免测试依赖真实时间


def M(mid, activity, description, day_offset, epoch=None, source="event"):
    """一条经历。day_offset 是"几天前"，epoch 默认从 NOW 推。"""
    return Member(id=mid, source_type=source, activity=activity,
                  description=description,
                  day="2026-10-%02d" % (10 - int(day_offset)),
                  epoch=NOW - day_offset * DAY if epoch is None else epoch)


# 故事 1 的三个场景：AI → 编程 → 项目
AI_EVENT     = M("e1", "AI",     "最近在研究新技术，一整天都在看论文", 20)
CODE_EVENT   = M("e2", "编程",   "开始学一门新语言，连着好几天一直在写", 18)
PROJ_EVENT   = M("e3", "项目",   "尝试新项目，一整个下午都在折腾", 16)
THREE_SCENES = [AI_EVENT, CODE_EVENT, PROJ_EVENT]


def _tmpdb():
    return tempfile.mkdtemp() + "/t.db"


# ============================================================ 1 关系判定

def test_cross_context_synthesis_finds_shared_structure():
    """① 跨 context 发现共同模式：三个不同场景 → 一条候选。"""
    out = synthesize(THREE_SCENES)
    assert len(out) == 1, out
    c = out[0]
    assert c.pattern == "A"
    assert set(c.contexts) == {"AI", "编程", "项目"}
    assert c.independent_days == 3
    # 候选必须能找到出处
    assert set(c.members) == {"e1", "e2", "e3"}
    assert c.detail["relation"] == "shared_behavior"


def test_single_context_makes_no_high_level_pattern():
    """② 单一 context 不得生成高层模式。一段经历不叫模式。"""
    assert synthesize([AI_EVENT]) == []
    assert synthesize([]) == []


def test_same_time_of_day_is_not_a_shared_structure():
    """第十五节的 False Positive：两个场景只共享"晚上"不得合并。

    "晚上跑步" + "晚上学习" → 不能推出"用户喜欢夜间活动"。
    关键词高度重合，但那只是同一个时间副词，不构成行为结构。

    刻意再加一条更难骗的对照：两件事**都投入很深**，唯一共同点仍然
    只有"晚上"。如果实现是靠"投入深"抓的，它会出候选——那就要看它
    抓的到底是"晚上"还是"投入深"。这里期望**不出**：
    两个主题 + 两天虽然过了最低门槛，但 Pattern A 额外要求
    "接触新东西"的场景（NOVELTY_MARKERS），跑步/学习都不满足。
    """
    plain = [M("n1", "跑步", "晚上跑步半小时", 3),
             M("n2", "学习", "晚上学习两小时", 2)]
    assert synthesize(plain) == []

    both_deep = [M("n1", "跑步", "晚上跑步一整天", 3),
                 M("n2", "学习", "晚上学习连着好几天", 2)]
    assert synthesize(both_deep) == [], \
        "只共享『晚上』不该形成模式——要么它抓错了特征，要么门槛太松"

    # 对照：三个不同主题、都有新东西场景、都深投入 → 应当成立
    real = [M("p1", "AI", "晚上研究新技术一整天", 3),
            M("p2", "编程", "晚上学新语言连着好几天", 2),
            M("p3", "项目", "晚上做新项目一整个下午", 1)]
    out = synthesize(real)
    assert [c.pattern for c in out] == ["A"]
    assert set(out[0].contexts) == {"AI", "编程", "项目"}


def test_one_statement_does_not_become_a_high_level_pattern():
    """三件事都只提到 AI——那不是关系，是词频。

    规格第六节 + 第十四节：关键词高度相关也不行。
    这三条在"投入深"上没有共同点，只是都出现了 AI。
    """
    vocab = [M("v1", "AI", "看了个 AI 视频", 3),
             M("v2", "AI", "AI 挺有意思", 2),
             M("v3", "AI", "AI 还挺有意思", 1)]
    assert synthesize(vocab) == []


def test_relationship_needs_two_distinct_activities():
    """同样是深投入，但都在同一个活动上 → 不构成跨 context。"""
    same = [M("s1", "AI", "研究新技术，一整天都在看论文", 5),
            M("s2", "AI", "又研究了一整晚新技术", 4),
            M("s3", "AI", "连续好几天在研究新技术", 3)]
    assert shared_behavior(same) is False
    assert synthesize(same) == []


def test_pattern_b_needs_both_feedback_conditions():
    """Pattern B 是条件—行为关系，只看到"投入深"那一半不算。"""
    only_deep = [M("d1", "AI", "研究新技术一整天，有了进展", 5),
                 M("d2", "编程", "学新语言连着好几天，有效果", 3)]
    assert synthesize_feedback_continuity(only_deep) == []

    full = only_deep + [
        M("d3", "画画", "又开了个新坑，一直没有进展，看不到反馈", 2),
        M("d4", "音乐", "又开了个新坑，随便看看就没深入", 1)]
    out = synthesize_feedback_continuity(full)
    assert len(out) == 1
    assert out[0].pattern == "B"
    assert out[0].detail["relation"] == "feedback_divergence"


def test_pattern_c_requires_time_causality():
    """Pattern C 必须有时间先后：目标在行为之前。"""
    goal = {"title": "准备 CSP 竞赛", "status": "active",
            "epoch": NOW - 30 * DAY, "scope": "goal"}
    after = [M("c1", "CSP", "刷题", 10), M("c2", "CSP", "练题", 8),
             M("c3", "CSP", "模拟赛", 6)]

    assert len(synthesize_goal_impact(after, goals=[goal])) == 1

    # 目标出现在行为**之后** → 同时存在不等于因果
    late = {"title": "准备 CSP 竞赛", "status": "active",
            "epoch": NOW - 1 * DAY, "scope": "goal"}
    assert synthesize_goal_impact(after, goals=[late]) == []


def test_pattern_c_requires_an_active_goal():
    """未认可的目标（proposed）不得驱动任何推断。"""
    events = [M("c1", "CSP", "刷题", 10), M("c2", "CSP", "练题", 8),
              M("c3", "CSP", "模拟赛", 6)]
    for status in ("proposed", "rejected", "deleted", "paused"):
        g = {"title": "准备 CSP 竞赛", "status": status,
             "epoch": NOW - 30 * DAY, "scope": "goal"}
        assert synthesize_goal_impact(events, goals=[g]) == [], status


def test_distinct_patterns_do_not_merge():
    """⑪ 多个不同模式不能错误合并。

    A（探索深投入）和 C（目标影响）描述的是两件事，
    即使证据重叠也必须各自成立、各自一条候选。
    """
    goal = {"title": "准备 CSP 竞赛", "status": "active",
            "epoch": NOW - 30 * DAY, "scope": "goal"}
    events = THREE_SCENES + [
        M("g1", "CSP", "刷题一整天", 5), M("g2", "CSP", "连着好几天练模拟赛", 3),
        M("g3", "CSP", "研究了很久", 2), M("g4", "CSP", "连续刷题", 1)]
    # A 和 C 需要的时间关系不同：A 看"投入深不深"，C 看"目标之后的占比"。
    # 共用一批事件时 C 的分母被 A 的老事件稀释到阈值下——这是**设计如此**
    #（宁可少提），所以这里给 C 自己的事件批次，而不是硬凑。
    c_events = [M("c1", "CSP", "刷题", 5), M("c2", "CSP", "练题", 4),
                M("c3", "CSP", "模拟赛", 3), M("c4", "CSP", "研究真题", 2),
                M("c5", "CSP", "复盘错题", 1)]
    out = synthesize(THREE_SCENES + c_events, goals=[goal])
    pats = {c.pattern for c in out}
    assert pats == {"A", "C"}, out
    # 两条候选的 scope 必须不同，否则后一条会顶掉前一条
    assert len({c.scope for c in out}) == len(out)


# ============================================================ 2 证据独立性

def test_same_source_repetition_fakes_no_evidence():
    """③④ 同一天说四遍只是一个源。

    第十四节的故事 3：一天内连说三句喜欢 AI，
    不能得出"进入长期探索模式"。
    """
    same_day = [M("s%d" % i, "AI", "我最近很喜欢研究 AI", 0) for i in range(4)]
    assert independent_days(same_day) == 1
    assert synthesize(same_day) == []


def test_same_activity_repeated_same_day_is_not_cross_context():
    """同一个活动、同一天、说很多遍 —— 也不是跨 context。"""
    same = [M("s%d" % i, "AI", "我最近很喜欢研究新技术", 0) for i in range(5)]
    assert synthesize(same) == []


def test_cross_day_independent_sources_can_form_support():
    """④ 跨天独立来源可以形成支持。"""
    assert independent_days(THREE_SCENES) >= MIN_INDEPENDENT_DAYS
    assert synthesize(THREE_SCENES)


def test_independent_days_counts_days_not_events():
    """4 段经历落在 2 天 → 独立源是 2，不是 4。"""
    ms = [M("a", "AI", "一整天", 3), M("b", "AI", "一整天", 3),
          M("c", "编程", "连着好几天", 1), M("d", "项目", "一整个下午", 1)]
    assert independent_days(ms) == 2


def test_decay_detected_after_pattern_graduated():
    """Story B 的核心：旧模式**已经毕业成正式理解**之后，变化仍要被发现。

    这是本次开发真踩到的第三个坑，而且是三个里最严重的：

    `graduate_candidates()` 把 scope 从 `synthesis:{rule}:{topic}` 换成
    `inferred:{rule}:{topic}`。而变化检测的参照系只认 `synthesis:` 前缀，
    于是模式一旦毕业，系统就**再也看不见它**——用户早已不是那样了，
    系统却彻底沉默。不报错、不产出、看起来像"没有变化"。

    而真正的风险恰恰在毕业之后：这条理解已经进了 prompt，
    正在影响每一次回答，"它已经不成立了"是最该被说出来的事。
    """
    _, store, hyp, ctx = _fresh()
    old = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(old.id, "correct", "确实是这样")
    for t in ("学新东西时我总是钻进去好几天", "研究新技术一投入就很深",
              "换新东西也总是钻进去"):
        hyp.record_natural_evidence(old.id, t, "support")
    assert hyp.graduate_candidates()
    grad = hyp.get(old.id)
    assert grad.status == "confirmed"
    assert grad.scope.startswith("inferred:")
    # 已毕业 → 已在影响回答
    assert old.id in [x.id for x in hyp.list_for_prompt()]

    _seed(store, [("AI", "随便看了几个视频，没深入", 6),
                  ("编程", "扫了一眼教程", 5),
                  ("项目", "试了试就放下了", 4),
                  ("AI", "划水刷了一下，没细看", 3),
                  ("项目", "又开了个坑，随便看看", 2)])
    from core.synthesis_bridge import run_synthesis
    r = run_synthesis(store, hyp)
    assert [p["pattern"] for p in r["proposed"]] == ["decay"], r
    # 旧理解仍然保留（不删历史），但变化已经摆到用户面前
    assert hyp.get(old.id).status == "confirmed"
    assert hyp.get(old.id).scope == grad.scope
    decay = [h for h in hyp.list_hypotheses(live_only=False)
             if "pattern_decay" in h.scope][0]
    assert decay.status == "pending"
    assert "可能" in decay.statement


def test_decay_wins_the_proposal_quota():
    """变化候选必须排在新建候选前面。

    一轮只摆 1 条（摆多了像算命），而 synthesize() 的产出顺序是
    "新建在前、变化在后"。于是只要有一条新建候选先抢到配额，
    decay 就永远被砍掉——而且 decay 更紧急：它针对的是**已经进了
    prompt、正在影响回答**的正式理解，新建候选只是没人看过的猜想。
    """
    _, store, hyp, _ = _fresh()
    old = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(old.id, "correct", "确实是这样")
    for t in ("学新东西时我总是钻进去好几天", "研究新技术一投入就很深",
              "换新东西也总是钻进去"):
        hyp.record_natural_evidence(old.id, t, "support")
    hyp.graduate_candidates()
    # 这些深投入记录保证"新建候选 A"这一轮也会被判定成立
    _seed(store, [("AI", "随便看了几个视频，没深入", 6),
                  ("编程", "扫了一眼教程", 5),
                  ("项目", "试了试就放下了", 4),
                  ("AI", "划水刷了一下，没细看", 3)])
    from core.synthesis_bridge import MAX_PROPOSALS_PER_RUN, run_synthesis
    assert MAX_PROPOSALS_PER_RUN == 1
    r = run_synthesis(store, hyp)
    assert len(r["proposed"]) == 1
    assert r["proposed"][0]["pattern"] == "decay", r


def test_graduated_synthesis_is_still_a_decay_reference():
    """已毕业的合成理解必须仍在参照系里（`_is_synthesis_scope` 双前缀）。"""
    from core.synthesis_bridge import (_is_synthesis_scope,
                                       _synthesis_rule_of)
    _, store, hyp, _ = _fresh()
    old = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(old.id, "correct", "确实是这样")
    for t in ("a", "b", "c"):
        hyp.record_natural_evidence(old.id, t, "support")
    hyp.graduate_candidates()
    s = hyp.get(old.id).scope
    assert s.startswith("inferred:")
    assert _is_synthesis_scope(s) is True
    assert _synthesis_rule_of(s) == "exploration_depth"
    # 非合成来源不许被误认
    assert _is_synthesis_scope("inferred:dynamic") is False
    assert _is_synthesis_scope("activity:学习") is False
    assert _is_synthesis_scope("background:focus_shift:学习") is False


# ============================================================ 3 候选，不是事实

def test_candidate_confidence_is_capped_below_formal():
    """⑧ 候选置信度必须低于 INIT_CONFIDENCE(0.50)。

    这样即便将来有代码误把它当普通假设处理，
    它也永远跨不过"看起来像正式理解"那条线。
    """
    for c in synthesize(THREE_SCENES):
        assert c.confidence <= MAX_CONFIDENCE < 0.50
    from memory.hypothesis_store import INIT_CONFIDENCE
    assert MAX_CONFIDENCE < INIT_CONFIDENCE


def test_statements_read_as_conjecture_never_as_personality():
    """第三节：不得读成"你就是……/本质上……/人格"。

    这是产品的第一原则，不是格式问题——
    "你就是深度投入型的人"不可反驳，而"可能存在一个模式"可以。
    """
    events = THREE_SCENES + [
        M("f1", "画画", "研究画风一整天，有了进展", 5),
        M("f2", "音乐", "学新乐器连着好几天，有效果", 4),
        M("f3", "做饭", "试新菜谱一整个下午", 3),
        M("f4", "健身", "又开了个新坑，一直没有进展", 2),
        M("f5", "木工", "随便看看就没深入", 1)]
    banned = ("你就是", "本质上", "人格", "性格", "属于", "天生")
    for c in synthesize(events):
        for w in banned:
            assert w not in c.statement, (w, c.statement)
            assert w not in c.reason, (w, c.reason)


def test_patterns_describe_behavior_not_motive():
    """模式只描述行为，不解释行为——不给原因、不给动机。"""
    for c in synthesize(THREE_SCENES):
        for w in ("因为", "所以他一定", "说明他就是", "反映出性格"):
            assert w not in c.statement, (w, c.statement)


# ============================================================ 4 真实 store 保护

def _fresh():
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore
    db = _tmpdb()
    return db, MemoryStore(db), HypothesisStore(db), ContextStore(db)


def _seed(store, specs):
    from memory.event import Event
    now = _dt.datetime.now().astimezone()
    for activity, desc, days in specs:
        store.add_event(Event(activity=activity, description=desc,
                              source="manual",
                              created_at=now - _dt.timedelta(days=days)))


def _propose_one(store, hyp):
    from core.synthesis_bridge import run_synthesis
    _seed(store, [("AI", "研究新技术，一整天都在看论文", 20),
                  ("编程", "学新语言，连着好几天一直在写", 18),
                  ("项目", "尝试新项目，一整个下午都在折腾", 16)])
    r = run_synthesis(store, hyp)
    assert len(r["proposed"]) == 1, r
    return hyp.list_hypotheses(live_only=False)[0]


def _iso():
    return _dt.datetime.now().astimezone().isoformat()


def _epoch():
    return _dt.datetime.now().astimezone().timestamp()


def test_pending_candidate_never_enters_prompt():
    """⑥ pending 不进 prompt。

    ⚠️ 这条**单独测不出前缀过滤**——pending 本来就不在
    LIVE_STATUSES 里，`live_only=True` 已经先挡了一道。
    前缀过滤是纵深防御，生效证据在
    `test_synthesis_prefix_guard_blocks_confirmed_candidate`。
    """
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    assert h.status == "pending"
    assert [x.id for x in hyp.list_for_prompt()] == []


def test_synthesis_prefix_guard_blocks_confirmed_candidate():
    """直接打纵深防御那层：即使状态被改成 confirmed，带 synthesis: 前缀也不进 prompt。

    正常链路里这个状态**不可达**——毕业会换掉前缀变成
    `inferred:...`。但正因不可达，前缀过滤很容易被当成死代码删掉，
    而"万一将来有人改了状态流"正是它存在的理由。
    写死这个状态来验它，是本项目第五次撞上双层守卫
    （V0.5.6 D / V0.5.8 L / V0.5.9 K / 本次 mutation M）。
    """
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    with sqlite3.connect(store.db_path) as con:
        con.execute("UPDATE hypotheses SET status='confirmed' WHERE id=?",
                    (h.id,))
        con.commit()
    # 前置条件：它现在确实是 live 的（否则这条测试是空转）
    assert h.id in [x.id for x in hyp.list_hypotheses(live_only=True,
                                                      limit=50)]
    # 但带 synthesis: 前缀 → 一律不进 prompt
    assert h.id not in [x.id for x in hyp.list_for_prompt()]


def test_accepted_candidate_still_never_enters_prompt():
    """⑦ accepted 也不进 prompt。

    用户认可过 ≠ 系统可以拿它当事实。
    accepted 依然不在 LIVE_STATUSES 里。
    """
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    assert hyp.get(h.id).status == "accepted"
    assert [x.id for x in hyp.list_for_prompt()] == []


def test_accepted_candidate_does_not_affect_judgment():
    """候选不得影响回答方式（第八节）。"""
    from core.judgment import Understanding, judge
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    u = Understanding(text=h.statement, kind="hypothesis",
                      status=hyp.get(h.id).status, grade="", scope=h.scope)
    assert judge("这道题怎么做？", [u]).response_mode == "direct"


def test_candidate_never_enters_pattern():
    """第十七节：候选不直接变 Pattern。

    ⚠️ 单条候选连 `PROMOTE_MIN_CONFIRMED(2)` 都达不到，所以这条**测不出
    前缀守卫**。必须造出**同 scope 两条 confirmed 候选**才轮得到
    守卫说话——见下一条。
    """
    from core.pattern_engine import PatternEngine
    from memory.pattern_store import PatternStore
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    pe = PatternEngine(store, hyp, PatternStore(store.db_path))
    assert pe.promote_all() == []


def test_synthesis_prefix_guard_blocks_pattern_promotion():
    """直接打 Pattern 侧那层守卫：两条同 scope 的 confirmed 候选也不许晋级。

    PROMOTE_MIN_CONFIRMED=2，所以要先造够样本、再把状态推到 confirmed，
    否则守卫根本轮不到执行——症状是"守卫看起来在但删了也没事"。
    """
    from core.pattern_engine import PROMOTE_MIN_CONFIRMED, PatternEngine
    from memory.pattern_store import PatternStore
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    # 补足样本：同一 scope 再造一条 confirmed 候选。
    # 走 INSERT 而非 create_synthesis_candidate——后者开头就用
    # synthesis_proposed(scope) 去重，同 scope 的第二条永远造不出来，
    # 症状是 `dup is None`，报错完全指不到"需要两条样本"这件事。
    #
    # PROMOTE 侧还有第三道门槛 `PROMOTE_MIN_INDEP(3)`：要 3 个不同
    # origin 的证据行。所以 dup 行必须**带证据**，否则 groups 凑够 2 条
    # 了却卡在 origin 数上——症状是 promote_all() 返回 []，
    # 和"前缀守卫生效"长得一模一样。这是个很容易骗过自己的陷阱。
    #
    # 原样本也一起推到 confirmed：**它本来是 pending**，
    # 只推 dup 行的话 confirmed 组里只有 1 条合成候选，
    # 照样够不到 PROMOTE_MIN_CONFIRMED。
    from core.pattern_engine import PROMOTE_MIN_INDEP
    cols = [r[1] for r in sqlite3.connect(store.db_path).execute(
        "PRAGMA table_info(hypotheses)")]
    iso, ep = _iso(), _epoch()

    def _row(rid):
        return {"id": rid, "statement": h.statement, "confidence": 0.45,
                "status": "confirmed", "scope": h.scope,
                "support_count": 0, "contradict_count": 0, "used_count": 0,
                "asked_count": 0, "last_asked_epoch": 0, "pending_question": "",
                "created_at_iso": iso, "created_at_epoch": ep,
                "updated_at_iso": iso, "updated_at_epoch": ep}

    with sqlite3.connect(store.db_path) as con:
        for i in range(PROMOTE_MIN_CONFIRMED):
            rid = "dup%d" % i
            row = _row(rid)
            con.execute(
                "INSERT INTO hypotheses (%s) VALUES (%s)"
                % (",".join(cols), ",".join("?" * len(cols))),
                tuple(row[c] for c in cols))
            for k in range(PROMOTE_MIN_INDEP):
                con.execute(
                    "INSERT INTO hypothesis_evidence (hypothesis_id, polarity,"
                    " source_type, source_id, origin, excerpt, detail,"
                    " created_at_iso, created_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (rid, "support", "event", "evt%d-%d" % (i, k),
                     "evt:%d-%d" % (i, k), "样本证据", "{}", iso, ep))
        # 原样本那条也推到 confirmed（它本来就是 pending）
        con.execute("UPDATE hypotheses SET status='confirmed' WHERE id=?",
                    (h.id,))
        con.commit()
    # 前置条件：confirmed 组里确实有合成候选（否则守卫是空转）
    confirmed = [x for x in hyp.list_hypotheses(status="confirmed", limit=50)
                 if x.scope.startswith(SYNTHESIS_PREFIX)]
    assert len(confirmed) >= PROMOTE_MIN_CONFIRMED, len(confirmed)
    # 全部仍在 synthesis: 前缀下
    assert all(x.scope.startswith(SYNTHESIS_PREFIX) for x in confirmed)

    assert PatternEngine(store, hyp, PatternStore(store.db_path)).promote_all() == []
    with sqlite3.connect(store.db_path) as con:
        assert con.execute("SELECT COUNT(*) FROM patterns").fetchone()[0] == 0


def test_candidate_never_enters_my_view_of_you():
    """第十八节：「我眼中的你」只展示正式理解。"""
    from core.understanding_view import topic_name
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    assert topic_name(h.scope) == ""
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    assert topic_name(hyp.get(h.id).scope) == ""


def test_source_type_whitelist_has_no_hypothesis_type():
    """第十一节的**外层**铁律：证据来源白名单里不许有 hypothesis。

    `SOURCE_TYPES = ("context","event","correction")`——`add_evidence`
    在最前面就按它拒绝 `'hypothesis'`，注释原话就是
    "假设不能证明自己"。这条断言钉的是这个常量本身，
    防止将来有人为了"支持自己"把它加进去。
    """
    from memory.hypothesis_store import SOURCE_TYPES
    assert "hypothesis" not in SOURCE_TYPES
    assert "synthesis" not in SOURCE_TYPES
    assert SOURCE_TYPES == ("context", "event", "correction")


def test_pending_candidate_cannot_take_natural_evidence():
    """pending 不吃自然证据——没人裁决过的猜测不许长大。

    两层都要验：`record_natural_evidence` 走**外层**状态检查，
    而 `add_evidence` 侧还有 `_natural_eligible` 那道白名单。
    只测外层的话，改内层白名单这条测试照样全绿（mutation Q 存活过一次）。
    """
    _, store, hyp, ctx = _fresh()
    h = _propose_one(store, hyp)

    # 外层
    assert hyp.record_natural_evidence(h.id, "我最近确实一直在学新东西",
                                       "support") is False
    # 内层白名单（直接问它，不经 record_natural_evidence）。
    # ⚠️ `_natural_eligible` 有两个重载：一个吃 (status, scope)，
    # 一个吃 (hypo_id)。只吃 id 那个叫 `_natural_eligible_check`。
    # 写错名字报的是 "missing 1 required positional argument"，
    # 完全指不到"两层守卫"。
    assert hyp._natural_eligible("pending", h.scope) is False
    assert hyp._natural_eligible("accepted", h.scope) is True
    assert hyp._natural_eligible_check(h.id) is False

    # ⚠️ 上面那行断言 `_natural_eligible("pending", ...)` 测的**不是**
    # synthesis 白名单——它被更前面那条 `status not in (...)` 拦住了。
    # 所以白名单本身要单独打：用 tentative（一个合法 live 状态）
    # 配 synthesis: 前缀，这样只有 synthesis 白名单能拦它。
    # 这是本项目第六次撞上双层守卫。
    assert hyp._natural_eligible("tentative", h.scope) is False, \
        "synthesis: 前缀在 tentative 下也必须被挡（自然证据白名单）"
    assert hyp._natural_eligible("tentative", "interaction:提示") is True, \
        "对照组：普通 interaction 不受这条白名单影响（否则上条是全拒）"
    assert hyp.get(h.id).status == "pending"


def test_accepted_candidate_takes_natural_evidence():
    """⑤ 用户认可后，自然证据可以支持它（故事 1 的关键一步）。"""
    _, store, hyp, ctx = _fresh()
    h = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    assert hyp.record_natural_evidence(h.id, "我最近确实一直在学新东西", "support") is True
    assert hyp.get(h.id).status == "accepted"      # 没变 confirmed


def test_verdict_never_self_confirms():
    """⑩ 错误模式不会自我确认。

    用户点一下"对"不构成系统可以拿它当事实的证据量——
    所以正确裁决只补证据 + 推进到 accepted，绝不跳到 confirmed。
    """
    _, store, hyp, ctx = _fresh()
    h = _propose_one(store, hyp)
    for _ in range(5):
        hyp.record_synthesis_verdict(h.id, "correct", f"第{_}次表态，其实是这样")
    assert hyp.get(h.id).status == "accepted"
    assert "confirmed" not in (hyp.get(h.id).status,)
    assert hyp.get(h.id).confidence <= 0.70       # HIGH_CONF_CAP


def test_rejected_candidate_does_not_revive():
    """⑧ rejected 不复活。"""
    _, store, hyp, ctx = _fresh()
    h = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(h.id, "wrong", "不对，我平时不这样")
    assert hyp.get(h.id).status == "rejected"
    assert hyp.synthesis_proposed(h.scope) is True       # 不再提
    from core.synthesis_bridge import run_synthesis
    assert run_synthesis(store, hyp)["proposed"] == []


def test_verdict_guard_rejects_non_synthesis_hypothesis():
    """只有 synthesis: 前缀能被裁决——否则等于开了一把能否决任意假设的钥匙。"""
    _, store, hyp, ctx = _fresh()
    # create() 返回 (hypothesis, why) 元组，不是 hypothesis 本身。
    # 取错的话下一行报 AttributeError('tuple' object has no attribute 'id')，
    # 报错完全指不到真正的守卫（scope 前缀检查）。
    created, why = hyp.create(statement="用户偏好先给提示",
                              scope="interaction:提示")
    assert created is not None, why
    assert hyp.record_synthesis_verdict(created.id, "wrong", "不对") is False
    assert hyp.get(created.id).status == "tentative"


def test_synthesis_is_pure_and_deterministic():
    """同样的输入永远给同样的输出。"""
    assert synthesize(THREE_SCENES) == synthesize(THREE_SCENES)
    assert synthesize(THREE_SCENES) == synthesize(list(THREE_SCENES))


def test_synthesis_has_no_side_effects_on_stores():
    """⑯ 纯逻辑：跑一百遍库里的行数不变。"""
    _, store, hyp, _ = _fresh()
    _seed(store, [("AI", "研究新技术，一整天", 20),
                  ("编程", "学新语言连着好几天", 18),
                  ("项目", "尝试新项目一整个下午", 16)])
    def count():
        with sqlite3.connect(store.db_path) as con:
            return (con.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0],
                    con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    before = count()
    for _ in range(5):
        synthesize(THREE_SCENES)
    assert count() == before


# ============================================================ 5 变化可修正

def test_decay_detected_when_old_pattern_shrinks():
    """⑨ 变化可以削弱旧模式（故事 2）。"""
    _, store, hyp, _ = _fresh()
    old = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(old.id, "correct", "确实是这样")
    # 后来同样三个主题全都浅尝
    _seed(store, [("AI", "随便看了几个视频，没深入", 4),
                  ("编程", "扫了一眼教程，粗略看了下", 3),
                  ("项目", "试了试就放下了", 2),
                  ("AI", "划水刷了一下，没细看", 1)])
    from core.synthesis_bridge import run_synthesis
    r = run_synthesis(store, hyp)
    assert [p["pattern"] for p in r["proposed"]] == ["decay"], r
    decay = [h for h in hyp.list_hypotheses(live_only=False)
             if "pattern_decay" in h.scope][0]
    assert decay.status == "pending"
    assert decay.confidence <= MAX_CONFIDENCE
    assert "可能" in decay.statement and "推测" in decay.statement


def test_decay_keeps_history_and_does_not_delete_old_candidate():
    """故事 2 的后半：不要删历史。旧模式仍在库里、仍可追溯。"""
    _, store, hyp, _ = _fresh()
    old = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(old.id, "correct", "确实是这样")
    _seed(store, [("AI", "随便看了几个视频，没深入", 4),
                  ("编程", "扫了一眼教程", 3),
                  ("项目", "试了试就放下了", 2),
                  ("AI", "划水刷了一下", 1)])
    from core.synthesis_bridge import run_synthesis
    run_synthesis(store, hyp)
    still = hyp.get(old.id)
    assert still is not None
    assert still.statement == hyp.get(old.id).statement
    assert "可能存在一个模式" in still.statement      # 原文没被改写
    # 变更日志也还在
    with sqlite3.connect(store.db_path) as con:
        rows = con.execute("SELECT COUNT(*) FROM hypothesis_log"
                           " WHERE hypothesis_id=?", (old.id,)).fetchone()[0]
    assert rows >= 2


def test_decay_ignored_when_pattern_still_holds():
    """模式还站得住时不得提"变浅了"——不能自己吓自己。"""
    _, store, hyp, _ = _fresh()
    old = _propose_one(store, hyp)
    _seed(store, [("AI", "又研究了一整天新技术", 4),
                  ("编程", "连着好几天在学", 3),
                  ("项目", "一整个下午在折腾", 2),
                  ("AI", "连续好几天研究", 1)])
    from core.synthesis_bridge import run_synthesis
    assert run_synthesis(store, hyp)["proposed"] == []


def test_decay_needs_enough_samples():
    """两次浅尝说明不了任何事。"""
    ref = (SYNTHESIS_PREFIX + "exploration_depth:x", "AI、编程、项目")
    ms = [M("a", "AI", "随便看看", 1), M("b", "编程", "试了试", 1)]
    assert detect_pattern_decay(ref[0], ms, ref[1]) == []
    assert DECAY_MIN_SAMPLES >= 3


def test_decay_only_watches_the_patterns_own_activities():
    """别的活动变浅不能说"这条模式变了"——那只是换了别的事做。"""
    ref = (SYNTHESIS_PREFIX + "exploration_depth:x", "AI、编程、项目")
    ms = [M("a", "游戏", "随便玩玩", 1), M("b", "运动", "试了试", 1),
          M("c", "做饭", "划水", 1)]
    assert detect_pattern_decay(ref[0], ms, ref[1]) == []


def test_decay_uses_evidence_time_not_candidate_creation_time():
    """分界必须是"支撑旧模式的证据最后一条"，不是候选的创建时刻。

    这是第一版的真 bug：候选是"刚才"建的，支撑它的经历都在过去，
    于是"分界之后"一条都没有，检测安静地永不触发。
    症状是不报错、不产出、看起来像没实现。
    """
    _, store, hyp, _ = _fresh()
    old = _propose_one(store, hyp)
    from core.synthesis_bridge import _reference_of
    acts, since = _reference_of(hyp, old.id)
    assert acts == "AI、编程、项目"
    assert since > 0, "分界时刻必须是 0 之外的真实值"
    # 分界应当等于最晚那条支撑经历的时刻，而不是候选 created_at
    latest = max(e.created_at.timestamp() for e in store.list_events(limit=10))
    assert abs(since - latest) < 5


def test_decay_reference_survives_verdict_and_evidence():
    """后续的裁决/证据记录不许覆盖 create 那条 detail。

    取用方必须按 action='create' 取；遍历"找第一个有 activities 的"
    会被后面的记录顶掉。
    """
    _, store, hyp, ctx = _fresh()
    old = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(old.id, "correct", "确实是这样")
    hyp.record_natural_evidence(old.id, "我确实总这样", "support")
    from core.synthesis_bridge import _reference_of
    acts, since = _reference_of(hyp, old.id)
    assert acts == "AI、编程、项目"
    assert since > 0


# ============================================================ 6 自我证明禁令

def test_synthesis_cannot_evidence_itself():
    """第十一节：**绝对禁止自我证明**。

    "我发现用户探索欲很强" → 因为是我的判断 → 又拿它当证据，这条链
    一旦成立，AI 就能把自己的猜测吹成正式理解。

    机制上已经成立：`_source_exists` 要求证据能追溯到
    context_entries / events / hypothesis_log 里的**真实行**，
    而候选自己只是 hypotheses 表里的一行，查不到任何表 ⇒ 绑定被拒。

    这条测试钉死的是"别去改 `_source_exists` 让候选自己能当证据"。
    """
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    # 候选刚建立时**零证据**——它凭什么支持自己？
    with sqlite3.connect(store.db_path) as con:
        n = con.execute("SELECT COUNT(*) FROM hypothesis_evidence"
                        " WHERE hypothesis_id=?", (h.id,)).fetchone()[0]
    assert n == 0, "候选建立时就带着证据 —— 自我证明"

    # 裁决之后唯一的证据必须指向一条真实的 log 行（用户的表态）
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    with sqlite3.connect(store.db_path) as con:
        rows = con.execute(
            "SELECT source_type, source_id FROM hypothesis_evidence"
            " WHERE hypothesis_id=? AND polarity='support'",
            (h.id,)).fetchall()
        assert rows, "裁决后应当有一条来自用户表态的证据"
        for stype, sid in rows:
            assert stype == "correction"
            hit = con.execute(
                "SELECT 1 FROM hypothesis_log WHERE id=? AND action IN"
                " ('reject','verify_confirm','change')", (sid,)).fetchone()
            assert hit, f"证据 {sid} 追溯不到真实的用户行为记录"

    # 候选自身（hypotheses 行）永远不能当 source_id。
    #
    # 这里有**两道**独立的守卫，必须分开验（否则改掉任何一道都测不出）：
    #
    # ① 白名单铁律：`source_type not in SOURCE_TYPES`（`'hypothesis'`
    #    根本不在里面），在 `_source_exists` **之前**就返回。
    #    这条注释写着"假设不能证明自己"——第十一节其实早就实现了。
    # ② 追溯校验 `_source_exists`：SOURCE_TYPES 里的三种都必须能
    #    追溯到真实行；候选自己不是任何一张表里的行 ⇒ 拒绝。
    #
    # 第一版我只写了"candidate id 当 source_id"一句，被 ① 遮住，
    # 于是改动 ② 时 mutation P 存活（本项目第六次双层守卫）。
    # 两条都必须直接打。① 靠一个**不在白名单里**的 source_type：
    # add_evidence 对它 `return False, "不允许的证据来源…"`，
    # 是 (False, 原因) 不是异常——写成 pytest.raises 会报
    # "DID NOT RAISE"，报错完全指不到"返回 False 才是契约"。
    ok, why = hyp.add_evidence(h.id, "support", "hypothesis", h.id)
    assert ok is False
    assert "不允许的证据来源" in why, why
    from memory.hypothesis_store import SOURCE_TYPES
    assert "hypothesis" not in SOURCE_TYPES
    # ② 在白名单里、但追溯不到行
    for stype in ("correction", "event", "context"):
        ok, _why = hyp.add_evidence(h.id, "support", stype, h.id)
        assert ok is False, f"{stype} 路径把候选自己当成合法证据源了"


def test_verdict_evidence_is_user_statement_not_ai_judgement():
    """裁决那条证据必须指向"用户表态"，不是"AI 的判断"。

    容易搞混：`record_synthesis_verdict` 是**系统**在记，
    但它记的是**用户说了什么**。所以 excerpt 必须是用户原话，
    不能是 candidate.statement（那句是 AI 自己写的）。
    """
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    said = "对，我确实一碰到新东西就钻进去"
    hyp.record_synthesis_verdict(h.id, "correct", said)
    # sqlite3.connect() 默认返回 tuple 行，不是按列名索引的 Row——
    # 用 row["origin"] 会报 "tuple indices must be integers"，
    # 报错完全不指向"行工厂没配"。
    with sqlite3.connect(store.db_path) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT origin, excerpt FROM hypothesis_evidence"
            " WHERE hypothesis_id=? LIMIT 1", (h.id,)).fetchone()
    assert row["origin"].startswith("synthesis_verdict:"), row["origin"]
    assert row["excerpt"] == said
    assert h.statement not in row["excerpt"]


# ============================================================ 7 毕业

def test_graduation_uses_existing_thresholds_and_scope_swap():
    """故事 1 完整链：候选 → 认可 → 真实证据 → 正式理解。

    毕业门槛就是既有的 HIGH_CONF_CAP(0.70) + 独立源 ≥ 2，
    毕业动作 = status→confirmed + scope 前缀换成 inferred:synthesis:...
    """
    _, store, hyp, ctx = _fresh()
    h = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    hyp.record_natural_evidence(h.id, "学新东西时我总是钻进去好几天", "support")
    hyp.record_natural_evidence(h.id, "研究新技术一投入就很深", "support")
    # 0.45 起 → 裁决 +0.08 → 两次自然证据 +0.16 = 0.69，**差 0.01 到 0.70**。
    # 所以必须凑到第 4 条独立证据；写 3 条会停在 0.69，
    # 而 graduate_candidates() 返回 [] —— 看起来像"毕业功能坏了"。
    hyp.record_natural_evidence(h.id, "换新东西时也总是钻进去", "support")
    assert hyp.get(h.id).confidence >= 0.70
    assert hyp.independent_support_count(h.id) >= 2
    assert hyp.graduate_candidates()

    f = hyp.get(h.id)
    assert f.status == "confirmed"
    assert f.scope.startswith("inferred:")
    assert not f.scope.startswith(SYNTHESIS_PREFIX)
    # 正式之后才进 prompt 和「我眼中的你」
    assert h.id in [x.id for x in hyp.list_for_prompt()]
    from core.understanding_view import topic_name
    topic = topic_name(f.scope)
    # 毕业 scope = `inferred:exploration_depth:接触新事物时的投入`
    # （只脱一层前缀）。topic_name 必须把它翻成**人话**主题，
    # 而不是把内部规则名 `exploration_depth` 原样端给用户——
    # 那是给检查系统的人看的字段。
    assert "exploration_depth" not in topic, topic
    assert topic and topic != "关于你", topic
    assert "接触新东西" in topic or "接触新事物" in topic, topic


def test_graduation_refuses_without_enough_independent_sources():
    """同一天说多遍凑不出独立源，也就不该毕业。"""
    _, store, hyp, ctx = _fresh()
    h = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    hyp.record_natural_evidence(h.id, "我确实总这样", "support")
    hyp.record_natural_evidence(h.id, "还是这样", "support")   # 同一天
    assert hyp.graduate_candidates() == []
    assert hyp.get(h.id).status == "accepted"


def test_lifecycle_survives_restart():
    """第21.16条：重启后生命周期保持。

    模拟进程重启 = **全新的 store 实例**，没有任何内存态。
    候选的 status / 置信度 / 独立源 / 是否可毕业必须全部照旧。

    这条容易被自己骗过去：测试里如果复用同一个 hyp 对象，
    它带着连接和缓存，看起来"重启了也正常"。
    """
    from core.synthesis_bridge import list_synthesis_candidates

    db, store, hyp, ctx = _fresh()
    h = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    hyp.record_natural_evidence(h.id, "学新东西时我总是钻进去好几天", "support")
    before = hyp.get(h.id)
    snap = (before.status, round(before.confidence, 2),
            hyp.independent_support_count(h.id))

    # ---- 重启：全新实例，只靠库里的数据 ----
    from memory.hypothesis_store import HypothesisStore
    hyp2 = HypothesisStore(db)
    after = hyp2.get(h.id)
    assert (after.status, round(after.confidence, 2),
            hyp2.independent_support_count(h.id)) == snap
    # 「我正在思考」仍列得出它
    assert h.id in [x.id for x in list_synthesis_candidates(hyp2)]
    # 但它依然不进 prompt
    assert h.id not in [x.id for x in hyp2.list_for_prompt()]

    # 重启后仍能继续长大并毕业
    for t in ("研究新技术一投入就很深", "换新东西也总是钻进去"):
        hyp2.record_natural_evidence(h.id, t, "support")
    assert hyp2.graduate_candidates()
    grad = hyp2.get(h.id)
    assert grad.status == "confirmed"
    assert h.id in [x.id for x in hyp2.list_for_prompt()]


def test_repeating_the_same_sentence_does_not_inflate_confidence():
    """确定性 log id：重复表态同一句话不能让 support 翻倍。"""
    _, store, hyp, ctx = _fresh()
    h = _propose_one(store, hyp)
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    c1 = hyp.get(h.id).confidence
    hyp.record_synthesis_verdict(h.id, "correct", "确实是这样")
    hyp.record_natural_evidence(h.id, "我确实总这样", "support")
    c2 = hyp.get(h.id).confidence
    hyp.record_natural_evidence(h.id, "我确实总这样", "support")
    assert hyp.get(h.id).confidence == c2
    assert c2 > c1


# ============================================================ 7 真实 replay + A/B

def test_real_conversation_replay_deposits_and_synthesizes():
    """⑬ 真实 conversation 链路：聊 → 沉淀 → 事件 → 合成。

    不直接调 synthesize()，走完整 `_write_turn`。

    ## 为什么事件要"回填"到三天前

    第一版让模型在三次 chat 里直接产出事件，结果一条候选都没有。
    查下去发现**这是对的**：三次 chat 发生在同一天，
    `_member_from_event` 按事件发生日分组 → 3 个事件同一天
    → 独立源 1 → 低于 MIN_INDEPENDENT_DAYS(2) → 不出候选。

    也就是说"同一天聊三次就总结出模式"被真实链路挡住了，
    这正是第九节要的。所以这里让 deposit 写完事件**再回填发生日**，
    模拟"这三天分别聊过一次"，而不是靠模型说什么来绕过独立性。
    """
    from core.conversation import ConversationService
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore
    import time

    db = _tmpdb()
    store, ctx = MemoryStore(db), ContextStore(db)
    hyp = HypothesisStore(db)

    class Depositing:
        """把用户那句话同时当回答，触发 deposit 写事件。"""
        def __init__(self):
            self.n = 0
        def chat(self, messages, images=None, timeout=None, **kw):
            system = str(messages[0].get("content", ""))
            if "记忆沉淀模块" in system:
                import json as _j
                # event 必须给成 dict 且带 **activity**：
                #   - 字符串 event 会被 `_apply_event` 的
                #     `if not isinstance(ev, dict): return` 直接丢掉；
                #   - 只有 description 的话 Pattern A 拿不到主题，
                #     跨 context 关系无从谈起（全部落在空活动上）。
                scenes = [("AI", "研究新技术，一整天都在看论文"),
                          ("编程", "学新语言，连着好几天一直在写"),
                          ("项目", "尝试新项目，一整个下午都在折腾")]
                act, desc = scenes[min(self.n, len(scenes)) - 1]
                self.n += 1
                return _j.dumps({
                    "new": [], "corrections": [],
                    "event": {"description": desc, "activity": act,
                              "importance": 0.7},
                }, ensure_ascii=False)
            return "好，记下了。"

    model = Depositing()
    # 位置参数顺序是 (store, context_store, model)，model 排第三。
    # 写成 (store, model, ctx) 会把 model 当成 ContextStore，
    # 于是报错是 'Depositing' object has no attribute 'list_entries'
    # ——指向沉淀层，而不是参数顺序。
    conv = ConversationService(store, ctx, model,
                               chat_store=ChatStore(db), hypothesis_store=hyp)
    # 沉淀用的是 conv.depositor.model，必须显式指过去——
    # 和 test_judgment.py 里的做法一致。不指的话 conv.chat 会用一个
    # 真实 ModelSettings 去调模型，测试要么联网要么炸。
    conv.depositor.model = model
    for text in ("最近在研究新技术", "开始学一门新语言",
                 "尝试新项目"):
        conv.chat(text)
        time.sleep(12)              # 等后台沉淀落库

    assert store.count_events() == 3, "沉淀没有按轮写出三条事件"

    # 回填发生日：模拟"这三天各聊过一次"。直接改 events 表的
    # created_at_iso / created_at_epoch，不经过 store 的公开接口——
    # 这里是构造测试前提，不是被测行为。
    with sqlite3.connect(db) as con:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM events ORDER BY created_at_epoch").fetchall()]
        for offset, eid in enumerate(reversed(ids), start=1):
            day = (_dt.datetime.now().astimezone() - _dt.timedelta(days=offset))
            con.execute(
                "UPDATE events SET created_at_iso=?, created_at_epoch=?"
                " WHERE id=?",
                (day.isoformat(), day.timestamp(), eid))

    from core.synthesis_bridge import run_synthesis
    r = run_synthesis(store, hyp)
    assert not r["error"], r
    assert [p["pattern"] for p in r["proposed"]] == ["A"], r
    # independent_days 只活在 SynthesisCandidate 上，落库后不作为列存在
    # （零 schema 变更），要从 create 的 log.detail 里读。
    c = hyp.list_hypotheses(live_only=False)[0]
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT detail FROM hypothesis_log WHERE hypothesis_id=?"
            " AND action='create'", (c.id,)).fetchone()
    d = json.loads(row[0])
    assert d["independent_days"] >= MIN_INDEPENDENT_DAYS
    assert len(set(d["activities"])) >= 2


def test_real_conversation_same_day_three_times_forms_no_pattern():
    """同一天聊三次，不得形成高层候选（第九节的独立性在真实链路上生效）。

    这条和上面那条成对：上一条把发生日摊到三天后 → 出候选；
    这条不动 → 不出。差别只有"独立来源天��"，证明起作用的是
    独立性判定，而不是别的巧合。
    """
    from core.conversation import ConversationService
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore
    import json as _j
    import time

    db = _tmpdb()
    store, ctx = MemoryStore(db), ContextStore(db)
    hyp = HypothesisStore(db)

    class Dep:
        def __init__(self):
            self.n = 0
        def chat(self, messages, images=None, timeout=None, **kw):
            if "记忆沉淀模块" in str(messages[0].get("content", "")):
                scenes = [("AI", "研究新技术，一整天都在看论文"),
                          ("编程", "学新语言，连着好几天一直在写"),
                          ("项目", "尝试新项目，一整个下午都在折腾")]
                a, d = scenes[min(self.n, 3) - 1]
                self.n += 1
                return _j.dumps({"new": [], "corrections": [],
                                  "event": {"description": d, "activity": a}},
                                 ensure_ascii=False)
            return "好。"

    m = Dep()
    conv = ConversationService(store, ctx, m, chat_store=ChatStore(db),
                               hypothesis_store=hyp)
    conv.depositor.model = m
    for txt in ("最近在研究新技术", "开始学一门新语言", "尝试新项目"):
        conv.chat(txt)
        time.sleep(12)
    assert store.count_events() == 3
    # 全部是今天 —— 不回填
    from core.synthesis_bridge import run_synthesis
    assert run_synthesis(store, hyp)["proposed"] == []


def test_ab_reality_single_fact_versus_many_experiences():
    """⑭ A/B Reality Test。

    A 只有一个孤立的深投入片段 → 不形成高层候选
    B 有多个跨 context 的独立经历 → 形成候选

    这条测试的意义：它证明系统不是"看到一句话就总结人格"，
    而是"积累多个独立经历之后才开始找模式"。
    """
    solo = synthesize([AI_EVENT])
    assert solo == [], "一条经历就总结模式 = 它在编故事"

    many = synthesize(THREE_SCENES)
    assert len(many) == 1
    assert many[0].independent_days >= MIN_INDEPENDENT_DAYS
    assert len(set(many[0].contexts)) >= 2


def test_ab_reality_conversation_unaffected():
    """⑫ 正常聊天不被影响：合成失败不许掀翻对话。"""
    from core.conversation import ConversationService
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore

    db = _tmpdb()
    class M:
        def chat(self, messages, images=None, timeout=None, **kw):
            return "你好。"
    conv = ConversationService(MemoryStore(db), ContextStore(db), M(),
                               chat_store=ChatStore(db),
                               hypothesis_store=HypothesisStore(db))
    # 把 synthesize 弄坏
    import core.synthesis_bridge as sb
    orig = sb.synthesize
    sb.synthesize = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        r = conv.chat("你好")
        assert r and r.answer
    finally:
        sb.synthesize = orig


def test_create_detail_is_flattened_not_nested():
    """create 的 detail 必须摊平。

    第一版写成 {"detail": {...}}，于是取用方按顶层键读
    （evidence_until / activities / relation）全都读不到，
    症状是变化检测静默不触发。
    """
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    with sqlite3.connect(store.db_path) as con:
        row = con.execute(
            "SELECT detail FROM hypothesis_log WHERE hypothesis_id=?"
            " AND action='create'", (h.id,)).fetchone()
    d = json.loads(row[0])
    for key in ("rule", "pattern", "relation", "activities",
                "evidence_until", "contexts"):
        assert key in d, f"create.detail 缺 {key}（被嵌套进去了？）"
    assert "detail" not in d, "detail 不该再嵌一层"


# ============================================================ 8 边界

def test_empty_and_single_inputs_are_safe():
    assert synthesize([]) == []
    assert synthesize(None) == []
    assert synthesize(THREE_SCENES, goals=None)


def test_broken_member_does_not_crash_synthesis():
    """一条脏数据不许掀翻整轮合成。"""
    dirty = list(THREE_SCENES) + [
        Member(id="bad", source_type="event", activity=None,
               description=None, day=None, epoch=None)]
    out = synthesize(dirty)
    assert any(c.pattern == "A" for c in out)


def test_is_synthesis_only_matches_the_prefix():
    assert is_synthesis(SYNTHESIS_PREFIX + "exploration_depth:x") is True
    assert is_synthesis("background:focus_shift:x") is False
    assert is_synthesis("inferred:synthesis:exploration_depth:x") is False
    assert is_synthesis("") is False


def test_candidate_scope_survives_roundtrip():
    """scope 里带冒号的主题名不能被切坏。"""
    _, store, hyp, _ = _fresh()
    h = _propose_one(store, hyp)
    assert h.scope.startswith(SYNTHESIS_PREFIX)
    assert hyp.list_hypotheses(live_only=False, limit=5)[0].scope == h.scope
