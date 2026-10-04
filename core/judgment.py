"""Adaptive Judgment：Formal Understanding → Judgment（Phase 1）。

这个模块回答一个具体问题：

    AI 已经理解了这个人之后，它下一次回答时，
    是不是真的因为这个理解而改变了做法？

以前做不到——所有理解被原样塞进 prompt，让模型自己决定怎么解释。
现在是**先判断、再执行**：

    Formal Understanding → Judgment → Response Strategy → 模型

设计约束（三条，都由代码保证）：

1. **纯逻辑。** 不碰数据库、不调模型、不联网。调用方负责取数据，
   这里只负责判断。判断错了要能单测定位到具体规则。
2. **只有可信理解能影响判断。** AI 的猜测（tentative / background
   pending / accepted 未毕业 / rejected / expired / quota_overflow）
   一律不参与——猜测不能改变 AI 对用户的正式行为策略。
3. **当前用户指令优先。** 长期记忆是对用户的**尊重**，不是命令。
   用户说"直接告诉我"，就当场直说。

Judgment 只决定"这一次怎么回应"。它**不写记忆**——新的理解仍然只能
通过 conversation → Natural Evidence → Understanding 这条既有链路形成。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------- Response Strategy（第一版只这三个）----------
MODE_DIRECT = "direct"          # 正常回答
MODE_HINT_FIRST = "hint_first"  # 先给提示 / 方向，不给完整答案
MODE_ASK_FIRST = "ask_first"    # 信息不足，先问一个必要的问题

MODES = (MODE_DIRECT, MODE_HINT_FIRST, MODE_ASK_FIRST)

# 场景（纯逻辑判定，不调模型）
SCENE_QUESTION = "question"    # 用户在问具体问题
SCENE_CHITCHAT = "chitchat"    # 闲聊/无明确任务
SCENE_UNKNOWN = "unknown"

# ---------- 判断依据的"可信度"闸门 ----------
# 只有这些组合能影响 Judgment。其余一律当作猜测。
FORMAL_GRADES = ("user_direct", "user_confirmed")
BACKGROUND_PREFIX = "background:"
QUOTA_PREFIX = "quota_overflow:"

# 正式理解才有的状态
FORMAL_HYPOTHESIS_STATUS = ("confirmed",)
FORMAL_PATTERN_STATUS = ("confirmed",)
FORMAL_GOAL_STATUS = ("active",)
FORMAL_CONTEXT_STATUS = ("active",)


@dataclass(frozen=True)
class Understanding:
    """一条"可能影响判断"的理解。调用方从各 store 取出来后包装成它。

    judgment 不关心它从哪来，只看这些字段——这样每条安全规则都能
    用构造出来的数据直接单测。
    """
    text: str
    kind: str = "context"        # context | hypothesis | pattern | goal
    status: str = "active"
    grade: str = "inferred"      # user_direct / user_confirmed / inferred / ...
    scope: str = ""

    @property
    def scope_head(self) -> str:
        return (self.scope or "").split(":", 1)[0]


@dataclass(frozen=True)
class Judgment:
    """这一次该怎么回应。
    `context` 是**情境**，不是**理由**：它回答"他眼下在忙什么"，
    而 `reasons` 回答"我为什么这么答"。两者必须分开——
    把目标混进 reasons 会产生一个很具体的谎：用户问"这道题怎么做"，
    reasons 写着"因为你在准备 CSP"，读起来像"我判断你要 CSP 才给提示"，
    而真相只是我顺带知道他在忙 CSP。所以目标**不进 reasons**。
    """
    response_mode: str = MODE_DIRECT
    verbosity: str = "normal"          # concise | normal
    context: list[str] = field(default_factory=list)    # 当前情境（正式目标/任务）
    reasons: list[str] = field(default_factory=list)   # 产品语言，可展示给用户
    applied: list[str] = field(default_factory=list)   # 内部：哪些来源起了作用
    ignored: list[str] = field(default_factory=list)    # 内部：被挡掉的猜测来源

    def to_dict(self) -> dict:
        return {
            "response_mode": self.response_mode,
            "verbosity": self.verbosity,
            "context": list(self.context),
            "reasons": list(self.reasons),
            "applied": list(self.applied),
            "ignored": list(self.ignored),
        }


# ============================================================ 闸门

def is_formal(u: Understanding) -> bool:
    """这条理解有没有资格影响 Judgment。

    **AI 的猜测不能改变 AI 对用户的正式行为策略。**
    """
    scope = (u.scope or "")
    if scope.startswith(BACKGROUND_PREFIX):
        return False          # 还没毕业的候选（含 accepted）
    if scope.startswith(QUOTA_PREFIX):
        return False          # V0.5.3 的不可验证材料
    if u.kind == "hypothesis":
        return u.status in FORMAL_HYPOTHESIS_STATUS
    if u.kind == "pattern":
        return u.status in FORMAL_PATTERN_STATUS
    if u.kind == "goal":
        return u.status in FORMAL_GOAL_STATUS
    if u.kind == "context":
        return (u.status in FORMAL_CONTEXT_STATUS
                and u.grade in FORMAL_GRADES)
    return False


def split_by_formal(understandings) -> tuple[list[Understanding],
                                              list[Understanding]]:
    formal, guesses = [], []
    for u in understandings or []:
        (formal if is_formal(u) else guesses).append(u)
    return formal, guesses


# ============================================================ 场景

_Q = ("?", "？", "吗", "呢", "怎么", "如何", "为什么", "什么", "哪",
      "多少", "是不是", "能不能", "可不可以", "help", "how")
_PROBLEM_HINT = ("题", "作业", "例题", "习题", "函数", "算法", "代码",
                 "程序", "阅读理解", "这道", "这题", "怎么写", "求解")
_CHITCHAT_HINT = ("你好", "在吗", "早", "晚安", "谢谢", "哈哈", "嗯",
                  "吃了", "睡了", "无聊", "干嘛")


def detect_scene(message: str) -> str:
    """这一轮是什么场景。纯词法判定，不调模型。"""
    t = (message or "").strip()
    if not t:
        return SCENE_UNKNOWN
    if any(h in t for h in _CHITCHAT_HINT) and len(t) <= 12:
        return SCENE_CHITCHAT
    if any(h in t for h in _PROBLEM_HINT):
        return SCENE_QUESTION
    if any(h in t for h in _Q):
        return SCENE_QUESTION
    return SCENE_UNKNOWN


def looks_like_task(message: str) -> bool:
    """这一轮是不是在要一个"做法"（值得用 hint_first）。"""
    return detect_scene(message) == SCENE_QUESTION


# ============================================================ 偏好 → 策略
#
# 这里必须处理否定，否则会把用户的话读反。
#
# "做这种题的时候别直接给我答案，先给我一个提示就行"——这句话里
# 同时有"直接"和"答案"，正向关键词匹配会判定成 direct，
# 而用户要的是提示。**方向读反比不读更糟**：不读只是没生效，
# 读反是每次都做用户明确不要的事。
#
# 做法：把每个信号词记成 (位置, 类别, 极性)，取**最早**的那个决定结果。
#   提示类 + 肯定 → hint_first    提示类 + 否定 → direct（"别提示"）
#   答案类 + 肯定 → direct        答案类 + 否定 → hint_first（"别直接给答案"）
# 取最早 = 用户先说的那个意图说了算，后半句的补充不翻盘。

# 否定词。刻意**不含**单独的"不"：中文里"不"太常见（"不错""不是不"…），
# 靠它判极性会把大量正常句子判反。只收明确的祈使否定。
_NEG = ("别", "不要", "不用", "甭", "不想", "不需要", "不必", "无需",
        "别再", "不要在")

_HINT_WORDS = ("提示", "思路", "方向", "自己想", "自己想一想", "自己先想")
_ANSWER_WORDS = ("答案", "直接", "告诉", "直说", "最终", "别绕", "过程")
_ASK_FIRST = ("先问我", "先问我再", "先确认", "先问清", "先问清楚",
              "先跟我说一下需求", "不确定就问我", "缺少信息就问")
_CONCISE = ("简短", "简洁", "别啰嗦", "不要长", "短一点", "少说点",
            "一句话", "别展开", "长话短说")

_NEG_WINDOW = 3   # 否定词最多隔几个字还能管住后面的信号词


def _norm(text: str) -> str:
    return re.sub(r"[\s，。、？！,.?!]", "", text or "")


def _negated(t: str, pos: int) -> bool:
    """命中位置前面一小段里有没有否定词。"""
    window = t[max(0, pos - _NEG_WINDOW):pos]
    return any(n in window for n in _NEG)


def _earliest_signal(t: str):
    """返回 (位置, 类别hint/answer, 是否被否定) 中位置最靠前的那个。

    没有信号返回 None。
    """
    best = None
    for kind, words in (("hint", _HINT_WORDS), ("answer", _ANSWER_WORDS)):
        for w in words:
            nw = _norm(w)
            if not nw:
                continue
            pos = t.find(nw)
            if pos < 0:
                continue
            cand = (pos, kind, _negated(t, pos))
            if best is None or cand[0] < best[0]:
                best = cand
    return best


def _read_preference(text: str) -> str | None:
    """一段文字表达的回应偏好。纯逻辑，不调模型。

    "先问"优先于其他——它是对信息不足的处理方式，不是对答案形式的偏好。
    """
    t = _norm(text)
    if not t:
        return None
    for w in _ASK_FIRST:
        nw = _norm(w)
        pos = t.find(nw)
        if pos >= 0 and not _negated(t, pos):
            return MODE_ASK_FIRST
    sig = _earliest_signal(t)
    if sig is None:
        return None
    _, kind, neg = sig
    if kind == "hint":
        return MODE_DIRECT if neg else MODE_HINT_FIRST
    return MODE_HINT_FIRST if neg else MODE_DIRECT


def current_instruction_mode(message: str) -> str | None:
    """**当前**这一句用户明确要求的回应方式。

    这是优先级最高的一层：长期记忆是对用户的尊重，不是命令。
    """
    return _read_preference(message)


def preferred_mode_from(text: str) -> str | None:
    """一条**正式**理解表达的回应偏好。"""
    return _read_preference(text)


def wants_concise(message: str) -> bool:
    t = _norm(message)
    return any(_norm(k) in t for k in _CONCISE)


def prefers_concise(text: str) -> bool:
    t = _norm(text)
    return any(_norm(k) in t for k in _CONCISE)

# ============================================================ 情境（context）
#
# 第九节要求的第三个输出：多个理解 → 一个 Judgment = mode + verbosity + context。
#
# 为什么单独一档而不是塞进 reasons：context 是"他眼下在忙什么"，
# reasons 是"我为什么这么答"。塞进 reasons 会让用户误以为
# "我给提示是因为判断你在备考"——而判断根本没看这个。

MAX_CONTEXT = 3          # 超过就是噪声：模型会开始讨好地挑一个
MAX_CONTEXT_LEN = 40     # 单条上限；目标是标题，不是正文

# 目标标题里的伪节头。用户自己写 "===" 或 "##" 时如果原样进 system prompt，
# 就能伪造出一个看起来像系统指令的段落。假设段早就栽过这个（见 conversation
# 里 "假设陈述里的换行/伪节头不许污染 system prompt 结构"），情境段同理。
_FAKE_HEADER = ("=", "#", "-", "*", "`", ">", "【", "[", "|")


def clean_context_label(text: str) -> str:
    """把一条情境压成能安全进提示词的单行短标签。"""
    flat = " ".join(str(text or "").split())   # 换行/连续空白全部压掉
    if not flat:
        return ""
    # 去掉会伪装成节头的符号；标题去掉符号后仍然是可读的
    while flat and flat[0] in _FAKE_HEADER:
        flat = flat[1:].lstrip()
    if len(flat) > MAX_CONTEXT_LEN:
        flat = flat[:MAX_CONTEXT_LEN].rstrip() + "…"
    return flat


def collect_context(understandings, *, task: str | None = None) -> list[str]:
    """正式理解里与"当前情境"有关的部分。

    只收 **active 的正式目标**——它们是用户自己说自己在做的事，
    而且已经过 `is_formal()` 闸门（tentative / background / rejected /
    expired / quota_overflow 一律进不来）。

    **两道闸门，作用不同，别把第二道当生效闸门：**

    1. `judge()` 里的 `split_by_formal()` —— **这道才是生效的**。
       传进来的列表已经滤过了。
    2. 这里面的 `is_formal(u)` —— 纵深防御。`collect_context` 是 public，
       直接调用会绕过第 1 道（mutation M 把这道删掉、只靠第 1 道兜住，
       测试仍全绿——那说明第 2 道从未被验证过，所以现在有测试直接打它）。

    规律不进 context：规律说的是"他是什么样的人"，那是 reasons 的地盘，
    而 context 说的是"他眼下在忙什么"。混了会变成"因为了解你所以给提示"。
    """
    out: list[str] = []
    for u in understandings or []:
        if u.kind != "goal" or not is_formal(u):
            continue
        label = clean_context_label(u.text)
        if label and label not in out:
            out.append(label)
        if len(out) >= MAX_CONTEXT:
            break
    cur = clean_context_label(task or "")
    if cur and cur not in out and len(out) < MAX_CONTEXT:
        out.insert(0, cur)      # 当前任务比长期目标更贴近这一轮
    return out

# ============================================================ 判断

def judge(message: str, understandings=None, *,
          force_ask: bool = False,
          task: str | None = None) -> Judgment:
    """Formal Understanding + 当前这句 → 这一次怎么回应。

    优先级（高到低）：
      1. 系统 / 安全硬约束
         ——**不在本层**。安全、暂停、"别记"这些是硬闸，由上游在调用本层
         之前就挡掉；Judgment 只在 `direct/hint_first/ask_first` 里选，
         没有任何一个输出能"打开"被关掉的东西。它只能收紧，不能放开。
         写成注释不算数，`test_judgment_cannot_open_what_safety_closed` 盯着。
      2. 当前用户明确指令
      3. 正式个人理解
      4. 无 → 默认 direct
    """
    formal, guesses = split_by_formal(understandings)
    reasons: list[str] = []
    applied: list[str] = []
    ignored = [g.scope or g.kind for g in guesses]

    # 情境在**任何提前返回之前**算好。
    # 覆盖只该改"怎么答"，不该把"他眼下在忙什么"一起抹掉——
    # 用户说"还是不会，直接告诉我吧"，CSP 这个目标照样是他的处境。
    ctx = collect_context(formal, task=task)
    if ctx:
        applied.append("context")

    # ① 当前这一句的明确要求——最高优先级
    now_mode = current_instruction_mode(message)
    if now_mode is not None:
        why = ("你这次说了要直接给答案"
               if now_mode == MODE_DIRECT else
               "你这次说了先给提示" if now_mode == MODE_HINT_FIRST else
               "你这次说了先问你")
        return Judgment(response_mode=now_mode,
                        context=ctx,
                        reasons=[why + "，所以这次照你说的来。"],
                        applied=["current_user_instruction"] + applied,
                        ignored=ignored)

    # ② 正式理解里的偏好
    preferred: str | None = None
    prefer_src: str | None = None
    concise = False
    for u in formal:
        if preferred is None:
            m = preferred_mode_from(u.text)
            if m:
                preferred, prefer_src = m, (u.scope or u.kind)
                applied.append(u.scope or u.kind)
                continue
        if not concise and prefers_concise(u.text):
            concise = True
            reasons.append("你说过希望我答得简短。")
            applied.append(u.scope or u.kind)

    if preferred is None and force_ask:
        return Judgment(response_mode=MODE_ASK_FIRST,
                        context=ctx,
                        reasons=["信息不够，我先问你一句。"],
                        applied=["insufficient_context"] + applied,
                        verbosity="concise" if concise else "normal",
                        ignored=ignored)

    mode = preferred or MODE_DIRECT
    # reasons 会同时进提示词和产品文案，**绝不能带 scope / id / 置信度**。
    # 来源只留在 applied（内部可追溯），对外只说人话。
    # 也不能编——没找到正式理解就不写这句。
    if preferred == MODE_HINT_FIRST and prefer_src:
        reasons.append("我记得你之前说过这类问题你更想先自己想一想，"
                       "所以这次我先给你提示。")
    elif preferred == MODE_DIRECT and prefer_src:
        reasons.append("你说过这类问题你更希望直接拿到答案，"
                       "所以这次我直接说。")
    elif preferred == MODE_ASK_FIRST and prefer_src:
        reasons.append("你说过信息不够的时候先问你，所以我先确认一下。")

    return Judgment(response_mode=mode,
                    verbosity="concise" if concise else "normal",
                    context=ctx,
                    reasons=reasons, applied=applied, ignored=ignored)


# ============================================================ 落到提示词

STRATEGY_TEXT = {
    MODE_DIRECT: "直接给出结论。",
    MODE_HINT_FIRST: "先给提示或思路，**不要直接给完整答案**，"
                     "留出他自己想的空间。",
    MODE_ASK_FIRST: "信息不足时先问一个必要的问题，"
                    "不要凭猜测替他决定。",
}


def strategy_directive(j: Judgment) -> str:
    """把判断转成一句给模型的执行指令。

    这是"理解 → Judgment → 模型"真正生效的那一步：
    模型本身没变，变的是它这次被要求怎么做。

    **context 必须在里面。** 只加一个字段而它不到提示词，
    等于"目标参与了判断"这句话是假的——模型压根不知道他在备考 CSP，
    那这条理解对回答的实际影响就是零。
    """
    parts = ["【本次回应方式】" + STRATEGY_TEXT.get(j.response_mode,
                                                  STRATEGY_TEXT[MODE_DIRECT])]
    if j.context:
        parts.append("【他眼下在忙】" + "；".join(j.context)
                     + "（回答可以贴着这个来，但别念出来）。")
    if j.verbosity == "concise":
        parts.append("尽量简短，不要展开。")
    if j.reasons:
        parts.append("依据：" + " ".join(j.reasons))
    return " ".join(parts)


def product_explanation(j: Judgment) -> str:
    """给用户看的说法。没有内部字段（无 response_mode / scope / id）。"""
    if not j.reasons:
        return "这次按平常方式回答。"
    return " ".join(j.reasons)