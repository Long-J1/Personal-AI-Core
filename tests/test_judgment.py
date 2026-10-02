"""Adaptive Judgment：Formal Understanding → Judgment。

核心命题（这一整个文件都在证明它）：

    AI 已经理解了这个人之后，它下一次回答时，
    是不是真的因为这个理解而改变了做法？

分三层测：
1. 纯判断层——三条安全规则（只有正式理解能影响 / 当前指令优先 /
   猜测一律不算），全部用构造数据直接钉。
2. A/B replay——**同一个模型、同一个输入、同样的参数**，
   唯一变量是有没有正式理解。模型没变，变的是 Judgment。
3. 真实 conversation 链路——证明聊天真的用了 Judgment，
   不是只有 judgment.py 自己能用。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone as _dtz

import pytest

from core.judgment import (
    MODE_ASK_FIRST,
    MODE_DIRECT,
    MODE_HINT_FIRST,
    Understanding,
    current_instruction_mode,
    is_formal,
    judge,
    preferred_mode_from,
    product_explanation,
    strategy_directive,
)

HINT_PREF = "做这种题的时候别直接给我答案，先给我一个提示就行"
DIRECT_PREF = "以后这类题直接告诉我答案吧"

# A/B 回放用的固定时刻：比真实时钟早，且在下午——深夜容易撞跨日边界，
# 那样"同一天的三次请求"就不再是同一天了。
SHANGHAI = _dtz(timedelta(hours=8))
AB_FAKE_NOW = datetime(2026, 3, 12, 15, 30, tzinfo=SHANGHAI)


def formal_pref(text):
    return Understanding(text=text, kind="context", status="active",
                        grade="user_direct", scope="")


def confirmed_hyp(text):
    return Understanding(text=text, kind="hypothesis", status="confirmed",
                        grade="", scope="interaction:提示")


# ============================================================ 1 纯判断

def test_confirmed_understanding_gives_hint_first():
    """核心故事 ①：正式理解 → hint_first。"""
    j = judge("这道题怎么做？", [formal_pref(HINT_PREF)])
    assert j.response_mode == MODE_HINT_FIRST
    assert j.applied, "要说得清是哪个来源起了作用"


def test_user_direct_interaction_counts_as_formal():
    u = formal_pref(HINT_PREF)
    assert is_formal(u) is True


def test_inferred_context_does_not_count_as_formal():
    """AI 自己推测出来的相处偏好，不算数。"""
    u = Understanding(text=HINT_PREF, kind="context", status="active",
                      grade="inferred", scope="")
    assert is_formal(u) is False
    assert judge("这道题怎么做？", [u]).response_mode == MODE_DIRECT


@pytest.mark.parametrize("status", ["tentative", "rejected", "deleted",
                                    "pending", "accepted", "expired"])
def test_non_confirmed_hypothesis_never_affects_judgment(status):
    """AI 的猜测不能改变 AI 对用户的正式行为策略。"""
    u = Understanding(text=HINT_PREF, kind="hypothesis", status=status,
                      grade="", scope="interaction:提示")
    assert is_formal(u) is False
    assert judge("这道题怎么做？", [u]).response_mode == MODE_DIRECT


@pytest.mark.parametrize("scope", [
    "background:behavior_change:学习",
    "background:focus_shift:学习",
    "quota_overflow:dynamic",
])
def test_background_and_quota_never_affect_judgment(scope):
    """核心故事 ③：background 候选不得控制 Judgment。

    即使 status=accepted（用户已认可），只要还没毕业成正式理解，
    就不能改变回应方式。
    """
    for status in ("pending", "accepted", "confirmed"):
        u = Understanding(text=HINT_PREF, kind="hypothesis", status=status,
                          grade="", scope=scope)
        assert is_formal(u) is False, f"{scope} / {status} 不该有资格"
        assert judge("这道题怎么做？", [u]).response_mode == MODE_DIRECT


def test_background_candidate_cannot_change_verbosity_either():
    u = Understanding(text="回答要简短", kind="hypothesis", status="accepted",
                      grade="", scope="background:focus_shift:学习")
    assert judge("这道题怎么做？", [u]).verbosity == "normal"


def test_current_user_instruction_overrides_long_term():
    """核心故事 ①后半：长期 hint_first，用户这次要直说 → direct。

    长期记忆是对用户的尊重，不是命令。
    """
    j = judge("还是不会，直接告诉我吧", [formal_pref(HINT_PREF)])
    assert j.response_mode == MODE_DIRECT
    assert j.applied == ["current_user_instruction"]
    assert "你这次" in j.reasons[0]


def test_current_hint_instruction_overrides_long_term_direct():
    j = judge("先给我个提示，别直接给答案", [formal_pref(DIRECT_PREF)])
    assert j.response_mode == MODE_HINT_FIRST


def test_current_instruction_beats_confirmed_hypothesis():
    j = judge("直接告诉我答案", [confirmed_hyp(HINT_PREF)])
    assert j.response_mode == MODE_DIRECT


def test_ask_first_from_formal_understanding():
    j = judge("这道题怎么做？", [formal_pref("信息不够的时候先问我再答")])
    assert j.response_mode == MODE_ASK_FIRST


def test_ask_first_when_forced_and_nothing_known():
    j = judge("那道题怎么做？", [], force_ask=True)
    assert j.response_mode == MODE_ASK_FIRST
    assert j.applied == ["insufficient_context"]


def test_plain_question_is_direct_by_default():
    assert judge("今天天气怎么样？", []).response_mode == MODE_DIRECT


def test_no_understanding_is_direct():
    assert judge("这道题怎么做？", []).response_mode == MODE_DIRECT


def test_verdict_never_raises_memory():
    """Judgment 只决定这一次怎么回应，不写记忆。

    纯函数：调用前后 store 一行不多。真实链路版本见
    test_judgment_has_no_memory_side_effect_in_real_chain。
    """
    j = judge("这道题怎么做？", [formal_pref(HINT_PREF)])
    # Judgment 是 frozen dataclass，没有任何写入口
    with pytest.raises(Exception):
        j.response_mode = MODE_DIRECT     # type: ignore[misc]


# ============================================================ 2 多理解组合

def test_multiple_understandings_combine():
    """核心故事 ④：多个正式理解 → 一个 Judgment。

    不创建 Personality System——只是多几条输入合成一个判断。
    """
    j = judge("这道程序阅读题怎么做？", [
        formal_pref(HINT_PREF),
        formal_pref("回答要简短，别啰嗦"),
        Understanding(text="长期目标是 CSP", kind="goal", status="active",
                      grade="", scope="goal"),
    ])
    assert j.response_mode == MODE_HINT_FIRST
    assert j.verbosity == "concise"
    # 目标本身不决定回应方式，只作为上下文
    assert any("CSP" not in r for r in j.reasons)


def test_first_preference_wins_when_conflicting():
    """多条偏好冲突时，取先出现的那条——确定性优先于聪明。"""
    j = judge("这道题怎么做？",
              [formal_pref(HINT_PREF), formal_pref(DIRECT_PREF)])
    assert j.response_mode == MODE_HINT_FIRST


# ============================================================ 3 偏好纠正

def test_preference_correction_flips_the_mode():
    """核心故事 ②：用户改主意 → 之后同类问题走 direct。"""
    before = judge("这道题怎么做？", [formal_pref(HINT_PREF)])
    assert before.response_mode == MODE_HINT_FIRST
    after = judge("这道题怎么做？", [formal_pref(DIRECT_PREF)])
    assert after.response_mode == MODE_DIRECT


def test_revoked_understanding_restores_default():
    """撤回理解后恢复默认——不是"退回上一次偏好"。"""
    assert judge("这道题怎么做？", []).response_mode == MODE_DIRECT
    u = Understanding(text=HINT_PREF, kind="hypothesis", status="rejected",
                      grade="", scope="interaction:提示")
    assert judge("这道题怎么做？", [u]).response_mode == MODE_DIRECT


# ============================================================ 4 落到模型

def test_strategy_directive_reaches_the_prompt():
    """Judgment → 策略指令 → 提示词。这一步才真正改变模型行为。"""
    d = strategy_directive(judge("这道题怎么做？", [formal_pref(HINT_PREF)]))
    assert "不要直接给完整答案" in d
    assert "【本次回应方式】" in d


def test_direct_strategy_says_so():
    d = strategy_directive(judge("这道题怎么做？", []))
    assert "直接给出结论" in d


def test_strategy_directive_carries_no_internal_fields():
    """执行指令里不能出现内部字段名。"""
    d = strategy_directive(judge("这道题怎么做？",
                                 [confirmed_hyp(HINT_PREF)]))
    for w in ("hypothesis_id", "source_type", "confidence",
              "response_mode", "interaction:"):
        assert w not in d, f"提示词里不该出现内部字段：{w}"


def test_product_explanation_is_human_and_sourced():
    """产品化解释：说人话，带真实来源，不编。"""
    j = judge("这道题怎么做？", [confirmed_hyp(HINT_PREF)])
    text = product_explanation(j)
    assert "说过" in text
    for w in ("hypothesis_id", "confidence", "source_type", "response_mode"):
        assert w not in text


def test_product_explanation_without_understanding_is_honest():
    assert product_explanation(judge("今天天气？", [])) == "这次按平常方式回答。"


def test_preferred_mode_from_reads_plain_chinese():
    assert preferred_mode_from(HINT_PREF) == MODE_HINT_FIRST
    assert preferred_mode_from(DIRECT_PREF) == MODE_DIRECT
    assert preferred_mode_from("我喜欢安静") is None


# ============================================================ 5b 否定语义
#
# 这一组是本次里程碑里最容易静默复发的一类 bug：方向读反。
# 读不出来只是没生效；读反是每次都做用户明确说过不要的事。

@pytest.mark.parametrize("text", [
    "做这种题的时候别直接给我答案，先给我一个提示就行",
    "别直接给答案",
    "不要直接告诉我答案",
    "先给我个提示，别直接给答案",
    "我想自己想一想，别直接说",
    "给我思路就行",
])
def test_negated_answer_request_reads_as_hint_first(text):
    assert preferred_mode_from(text) == MODE_HINT_FIRST, \
        f"否定句被读反了：{text}"


@pytest.mark.parametrize("text", [
    "还是不会，直接告诉我吧",
    "别绕了，直接说",
    "给我最终答案",
    "我不要提示，直接给答案",
])
def test_plain_answer_request_reads_as_direct(text):
    assert preferred_mode_from(text) == MODE_DIRECT, text


def test_negation_also_applies_to_current_instruction():
    """当前指令同样要认否定——用户说"这次别直接给答案"就该给提示。"""
    assert current_instruction_mode("这次别直接给我答案") == MODE_HINT_FIRST
    assert current_instruction_mode("这次直接告诉我") == MODE_DIRECT


def test_earliest_signal_wins_over_later_contrast():
    """先说的意图说了算，后半句的转折不翻盘。

    "先给提示，别直接给答案"——"提示"在前 → 提示。
    """
    assert current_instruction_mode("先给提示，别直接给答案") == MODE_HINT_FIRST


def test_negation_does_not_fire_on_innocent_words():
    """"不错"里有个"不"字，不能因此把整句判成否定。"""
    from core.judgment import _negated, _norm

    assert _negated(_norm("这个方法很不错"), 0) is False
    # 真实效果：句子里有"不"也不影响判定方向
    assert current_instruction_mode("这个方法很不错，直接告诉我答案") == MODE_DIRECT


# ============================================================ 6 性能

def test_judgment_is_light():
    """Judgment 必须非常轻：不调模型、不检索、不阻塞。"""
    import statistics
    us = [confirmed_hyp(HINT_PREF) for _ in range(20)]
    times = []
    for i in range(200):
        t0 = time.perf_counter()
        judge("这道题怎么做？", us, force_ask=(i % 2 == 0))
        times.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(times)
    assert med < 2.0, f"Judgment 太重：{med:.3f}ms"


# ============================================================ 6 A/B replay
# 同一个模型、同一个输入、同样的参数。唯一变量：有没有正式理解。


class StrategyEcho:
    """按提示词里的【本次回应方式】改变回答。

    这正是要证明的事：模型本身没变，变的是它被要求怎么做。
    """

    DIRECTIVE = "【本次回应方式】"

    def __init__(self):
        self.prompts: list[str] = []

    def chat(self, messages, images=None, timeout=None, **kw):
        system = str((messages[0] or {}).get("content", "")) if messages else ""
        self.prompts.append(system)
        if self.DIRECTIVE not in system:
            return "答案是 42。"
        seg = system.split(self.DIRECTIVE, 1)[1]
        if "不要直接给完整答案" in seg:
            return "先提示：把常数项移到等号右边，再自己算算看。"
        if "先问" in seg:
            return "我先确认一下：你是想问思路还是答案？"
        return "答案是 42。"


def test_ab_same_model_same_input_only_understanding_differs():
    """A/B replay：模型、输入、参数全同，只有正式理解在变。

    Case A 无理解 → Case B 有 confirmed hint_first → Case C 撤回 → 回到 A

    撤回用**真实的 delete_entry**，不是换个空库。换个空库测的是
    "数据库空了会怎样"，测不到"已存在的理解被删掉后能不能自己走掉"——
    后者才是产品里真会发生的事，也是更容易漏的那种。
    """
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore
    from core.conversation import ConversationService
    import tempfile

    model = StrategyEcho()
    db = tempfile.mkdtemp() + "/t.db"
    cs = ContextStore(db)

    def ask() -> tuple[str, str]:
        """问一句，返回 (回答, 本次系统提示词)。

        不 clear 提示词——要比的就是三次提示词之间**除策略段以外**
        是不是完全一样。只留最后一次就比不了。

        **必须注入固定时钟**：Runtime Time Context 每轮实时生成时间，
        三次请求的时间戳会差一两秒，那本身不是缺陷，是设计。
        但这里要证明的是"唯一变量是正式理解"，所以时间必须钉死，
        否则变量从 1 个变成 2 个，比对就失去意义。
        """
        from core.runtime_time import TimeProvider

        conv = ConversationService(
            MemoryStore(db), ContextStore(db), model,
            chat_store=ChatStore(db),
            hypothesis_store=HypothesisStore(db),
            time_provider=TimeProvider(clock=lambda: AB_FAKE_NOW))
        mark = len(model.prompts)
        answer = conv.chat("这道题怎么做？").answer
        return answer, model.prompts[mark]

    # ---- Case A：没有个人理解 ----
    a, pa = ask()
    # ---- Case B：有正式理解（用户原话，user_direct）----
    entry, _ = cs.add_entry(kind="interaction", content=HINT_PREF,
                            source="user_direct", source_grade="user_direct")
    b, pb = ask()
    # ---- Case C：撤回这条理解 ----
    assert cs.delete_entry(entry.id) is True
    c, pc = ask()

    assert a == "答案是 42。", a
    assert "提示" in b and "42" not in b, f"有理解时必须先给提示：{b}"
    assert c == a, f"撤回后必须恢复默认：{c!r} != {a!r}"

    # 提示词：只有 Case B 带 hint_first 策略，其余结构必须完全一致。
    # 这一条才是"唯一变量是正式理解"的直接证据——模型收到的输入
    # 除了策略段一模一样。
    assert StrategyEcho.DIRECTIVE in pa and StrategyEcho.DIRECTIVE in pc
    assert "不要直接给完整答案" in pb
    assert "不要直接给完整答案" not in pa
    assert "不要直接给完整答案" not in pc
    strip = lambda s: s.split(StrategyEcho.DIRECTIVE)[0]
    assert strip(pb) != "" and strip(pa) == strip(pc), \
        "策略段之外，输入应该完全相同"


# ============================================================ 7 真实链路
# 上面测的是 judgment.py 自己。这一节证明**真实聊天真的用了它**——
# 只测纯模块，等于没测接线。

def _fresh(db, model, **stores):
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore
    from core.conversation import ConversationService

    return ConversationService(
        MemoryStore(db), stores.get("ctx") or ContextStore(db), model,
        chat_store=ChatStore(db),
        hypothesis_store=stores.get("hyp") or HypothesisStore(db))


def _tmpdb():
    import tempfile
    return tempfile.mkdtemp() + "/t.db"


def test_real_chat_reply_carries_judgment():
    """真实 chat 的返回值里必须带本次判断，且和提示词里的策略一致。"""
    from memory.context_store import ContextStore
    db = _tmpdb()
    model = StrategyEcho()
    cs = ContextStore(db)
    cs.add_entry(kind="interaction", content=HINT_PREF,
                 source="user_direct", source_grade="user_direct")

    r = _fresh(db, model, ctx=cs).chat("这道题怎么做？")
    assert r.judgment is not None, "真实聊天必须带上 Judgment"
    assert r.judgment["response_mode"] == MODE_HINT_FIRST
    assert r.judgment["applied"], "要能说清是哪个来源起了作用"
    assert "提示" in r.answer


def test_real_chat_judgment_is_visible_in_prompt_not_only_in_return():
    """接线点钉死：策略必须真的进了模型看到的提示词。"""
    from memory.context_store import ContextStore
    db = _tmpdb()
    model = StrategyEcho()
    cs = ContextStore(db)
    cs.add_entry(kind="interaction", content=HINT_PREF,
                 source="user_direct", source_grade="user_direct")

    _fresh(db, model, ctx=cs).chat("这道题怎么做？")
    answer_prompt = [p for p in model.prompts if StrategyEcho.DIRECTIVE in p]
    assert answer_prompt, "回答用的提示词里必须有策略段"
    assert "不要直接给完整答案" in answer_prompt[-1]


def test_strategy_survives_restart():
    """重启后策略保持——理解在库里，判断就该跟着在。"""
    from memory.context_store import ContextStore
    db = _tmpdb()
    model = StrategyEcho()
    cs = ContextStore(db)
    cs.add_entry(kind="interaction", content=HINT_PREF,
                 source="user_direct", source_grade="user_direct")

    r1 = _fresh(db, model, ctx=ContextStore(db)).chat("这道题怎么做？")
    assert r1.judgment["response_mode"] == MODE_HINT_FIRST

    # 重启：全新一套 store / service 对象，同一个库
    r2 = _fresh(db, model, ctx=ContextStore(db)).chat("这道题怎么做？")
    assert r2.judgment["response_mode"] == MODE_HINT_FIRST
    assert r2.answer == r1.answer


# ---- 真库里的"猜测不得指挥行为" ----
# 纯函数层已经测过，但纯函数不代表接线。假设真的躺在库里时，
# 行为必须一样。这里每一条都真的写进数据库再走完整链路。

def test_real_tentative_hypothesis_does_not_steer():
    db = _tmpdb()
    model = StrategyEcho()
    from memory.hypothesis_store import HypothesisStore
    h, _ = HypothesisStore(db).create(statement=HINT_PREF,
                                      scope="interaction:提示")
    assert h.status == "tentative"
    r = _fresh(db, model).chat("这道题怎么做？")
    assert r.judgment["response_mode"] == MODE_DIRECT, r.judgment


def test_real_background_candidate_does_not_steer():
    """核心故事 ③的真库版：用户已认可（accepted）也照样不指挥行为。

    直到它真正毕业成正式理解，才算数。
    """
    from memory.hypothesis_store import HypothesisStore
    from memory.context_store import ContextStore
    from core.background_reasoning import Candidate, TYPE_BEHAVIOR_CHANGE
    db = _tmpdb()
    model = StrategyEcho()
    ContextStore(db)          # 真库里两个 store 总是并存，schema 一并建好
    h = HypothesisStore(db)
    scope = "background:behavior_change:学习"
    cand = h.create_background_candidate(Candidate(
        type=TYPE_BEHAVIOR_CHANGE, topic="学习", scope=scope,
        confidence=0.42,
        reason="用户最近的学习时间好像挪到了晚上，先给提示他可能更喜欢",
        detail={"rule": "background_reasoning"}))
    assert cand is not None, "候选没落库"
    # 用户点了"正确"——候选变 accepted，但**还没有毕业**
    assert h.record_background_verdict(cand.id, "correct",
                                       "对，我最近确实晚上学") is True
    assert h.get(cand.id).status == "accepted", "前提：候选已被认可"

    r = _fresh(db, model).chat("这道题怎么做？")
    assert r.judgment["response_mode"] == MODE_DIRECT, \
        f"未毕业的候选不该改变回应方式：{r.judgment}"
    assert "不要直接给完整答案" not in model.prompts[-1]


def test_real_confirmed_hypothesis_does_steer():
    """对照组：同样的内容，毕业后就该生效。"""
    from memory.hypothesis_store import HypothesisStore
    db = _tmpdb()
    model = StrategyEcho()
    h = HypothesisStore(db)
    hyp, _ = h.create(statement=HINT_PREF, scope="interaction:提示")
    assert h.confirm(hyp.id) is True
    assert h.get(hyp.id).status == "confirmed"

    r = _fresh(db, model).chat("这道题怎么做？")
    assert r.judgment["response_mode"] == MODE_HINT_FIRST, r.judgment


def test_real_rejected_hypothesis_does_not_steer():
    db = _tmpdb()
    model = StrategyEcho()
    from memory.hypothesis_store import HypothesisStore
    h = HypothesisStore(db)
    hyp, _ = h.create(statement=HINT_PREF, scope="interaction:提示")
    h.confirm(hyp.id)
    assert h.reject(hyp.id, reason="用户说不是这样") is True
    assert h.get(hyp.id).status == "rejected"

    r = _fresh(db, model).chat("这道题怎么做？")
    assert r.judgment["response_mode"] == MODE_DIRECT, \
        f"被否决的偏好不该复活：{r.judgment}"


def test_real_inferred_context_does_not_steer():
    """AI 自己推测的相处偏好（inferred）不是用户说的，不算数。"""
    from memory.context_store import ContextStore
    db = _tmpdb()
    model = StrategyEcho()
    ContextStore(db).add_entry(kind="interaction", content=HINT_PREF,
                               source="chat", source_grade="inferred")
    r = _fresh(db, model).chat("这道题怎么做？")
    assert r.judgment["response_mode"] == MODE_DIRECT, r.judgment


def test_real_outdated_context_does_not_steer():
    """用户表示自己变了之后，旧偏好不该还在指挥行为。"""
    from memory.context_store import ContextStore
    db = _tmpdb()
    model = StrategyEcho()
    cs = ContextStore(db)
    entry, _ = cs.add_entry(kind="interaction", content=HINT_PREF,
                            source="user_direct", source_grade="user_direct")
    cs.set_memory_status(entry.id, "outdated",
                         reason="用户表示自己变了：以后直接告诉我")
    r = _fresh(db, model, ctx=ContextStore(db)).chat("这道题怎么做？")
    assert r.judgment["response_mode"] == MODE_DIRECT, r.judgment


def test_judgment_failure_does_not_break_chat(monkeypatch):
    """判断层故障不许掀翻对话——它是辅助，没有这个权力。"""
    import core.judgment as J
    db = _tmpdb()
    model = StrategyEcho()

    def boom(*a, **kw):
        raise RuntimeError("judgment exploded")

    monkeypatch.setattr(J, "judge", boom)
    r = _fresh(db, model).chat("这道题怎么做？")
    assert r.answer, "判断挂了也必须给出回答"
    assert r.judgment["response_mode"] == MODE_DIRECT, "退回默认直答"
    assert "42" in r.answer


def test_judgment_has_no_memory_side_effect():
    """Judgment 只决定这一次怎么回应，一行记忆都不许写。

    新的理解仍然只能走 conversation → Natural Evidence → Understanding。
    """
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    db = _tmpdb()
    model = StrategyEcho()
    cs = ContextStore(db)
    cs.add_entry(kind="interaction", content=HINT_PREF,
                 source="user_direct", source_grade="user_direct")

    def counts():
        hyp = HypothesisStore(db)
        return (cs.count_active(), len(hyp.list_hypotheses(live_only=False)))

    before = counts()
    for _ in range(3):
        _fresh(db, model, ctx=ContextStore(db)).chat("这道题怎么做？")
    assert counts() == before, "Judgment 不该写任何记忆"


def test_judgment_never_invents_understanding():
    """聊了 hint_first 一轮，也不会因此长出一条新的偏好。"""
    from memory.context_store import ContextStore
    db = _tmpdb()
    model = StrategyEcho()
    cs = ContextStore(db)
    cs.add_entry(kind="interaction", content=HINT_PREF,
                 source="user_direct", source_grade="user_direct")

    _fresh(db, model, ctx=ContextStore(db)).chat("这道题怎么做？")
    texts = [e.content for e in ContextStore(db).list_entries(active_only=False)]
    assert texts == [HINT_PREF], f"不该多出理解：{texts}"


# ============================================================ 8 完整故事 replay
# 任务书的核心闭环，一次跑完：
#   用户立规矩 → 系统形成正式理解 → 之后同类问题按规矩答
#   → 用户当场改主意 → 当前回合听用户的 → 长期理解被纠正

class StoryModel:
    """能沉淀的模型：按提示词策略改变回答，沉淀走队列。"""

    def __init__(self):
        self.text_queue: list[str] = []

    def chat(self, messages, images=None, timeout=None, **kw):
        system = str(messages[0].get("content", "")) if messages else ""
        if "记忆沉淀模块" in system:
            if self.text_queue:
                return self.text_queue.pop(0)
            return json.dumps({"new": [], "corrections": [], "event": None})
        if StrategyEcho.DIRECTIVE in system:
            seg = system.split(StrategyEcho.DIRECTIVE, 1)[1]
            if "不要直接给完整答案" in seg:
                return "先提示：先看条件里给了什么。"
            return "答案是 42。"
        return "答案是 42。"


def _deposit_new(content, kind="interaction"):
    return json.dumps({"new": [{"kind": kind, "content": content}],
                       "corrections": [], "event": None})


def _deposit_correct(ref, content):
    """走**真实**纠正链路：原地改写那条理解，而不是新建一条。

    差别很关键。新建一条时旧偏好还活着，只是排在后面——"最新赢"
    看着像纠正，其实只是排序侥幸；旧条目一旦失效就会自己回来。
    原地改写才是真的把旧理解换掉。
    """
    return json.dumps({"new": [], "corrections": [
        {"ref": ref, "content": content, "reason": "用户改主意了"}],
        "event": None})


def test_full_story_replay():
    """完整闭环：立规矩 → 按规矩答 → 当场反悔 → 长期被纠正。"""
    from memory.context_store import ContextStore
    db = _tmpdb()
    model = StoryModel()
    conv = _fresh(db, model, ctx=ContextStore(db))
    conv.depositor.model = model

    # ---- ① 用户立规矩 ----
    model.text_queue = [_deposit_new(HINT_PREF)]
    conv.chat(HINT_PREF)
    time.sleep(12)                       # 等后台沉淀落库

    # ---- ② 同类问题：按正式理解先给提示 ----
    r2 = conv.chat("这道题怎么做？")
    assert r2.judgment["response_mode"] == MODE_HINT_FIRST, r2.judgment
    assert "先提示" in r2.answer, r2.answer

    # ---- ③ 用户当场反悔：当前回合必须听用户的 ----
    r3 = conv.chat("还是不会，直接告诉我吧")
    assert r3.judgment["response_mode"] == MODE_DIRECT, r3.judgment
    assert r3.judgment["applied"] == ["current_user_instruction"]
    assert "42" in r3.answer

    # ---- ④ 用户改主意：走真实纠正链路，原地换掉旧理解 ----
    model.text_queue = [_deposit_correct("#1", DIRECT_PREF)]
    conv.chat(DIRECT_PREF)
    time.sleep(12)

    active = ContextStore(db).list_entries(kind="interaction")
    assert len(active) == 1, f"不该多出一条并存偏好：{[e.content for e in active]}"
    assert HINT_PREF not in active[0].content, "旧偏好必须被换掉，不是被压过"

    # ---- ⑤ 之后同类问题走 direct，且旧规矩没有自己回来 ----
    r5 = conv.chat("这道题怎么做？")
    assert r5.judgment["response_mode"] == MODE_DIRECT, r5.judgment
    assert "42" in r5.answer

    # 历史仍可追溯：变更日志里能查到它原来是什么
    hist = json.dumps(ContextStore(db).history(), ensure_ascii=False)
    assert HINT_PREF in hist, "历史不该消失"


def test_story_replay_is_deterministic():
    """同一个故事跑两遍，判断序列必须完全一致。"""
    def run():
        from memory.context_store import ContextStore
        db = _tmpdb()
        model = StoryModel()
        conv = _fresh(db, model, ctx=ContextStore(db))
        conv.depositor.model = model
        model.text_queue = [_deposit_new(HINT_PREF)]
        conv.chat(HINT_PREF)
        time.sleep(12)
        return [conv.chat("这道题怎么做？").judgment["response_mode"],
                conv.chat("还是不会，直接告诉我吧").judgment["response_mode"]]

    assert run() == run()