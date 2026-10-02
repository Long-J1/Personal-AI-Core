"""自然证据与变化信号（V0.5.6 夜间开发 Phase 1-2）。

只做两件事，全是纯函数：无数据库 IO、无网络、无副作用、可直接单测。

1. 判定用户这句话对"已有理解"是什么信号；
2. 判定这句话是否真的在谈某条假设/某条理解的话题。

判断优先级是唯一真源（本模块），别处不要另写一份：

    疑问 > 否定 > 变化 > 自然支持 > 无信号

刻意保守：宁可漏判，不可错判。错判会污染长期理解，
漏判只是让系统保持沉默——后者安全得多。
"""
from __future__ import annotations

from dataclasses import dataclass

from memory.hypothesis_store import _is_explicit_denial

from .verifier import _content_grams, scope_label

# ---------- 话题匹配门槛（安全门）----------
# 共用二连字 ≥2 个才算"强相关"。单个词（如"晚上"）同时出现在两条假设里
# 太常见，用它做不可逆动作 = 随机污染别人，必须挡住。
MIN_RELATED_GRAMS = 2

# 弱相关门槛（≥1 个二连字）：只允许做**可逆**的事（降权），
# 不允许做不可逆的事（否决置位 / 确认）。这样"不是，我现在早上效率更高"
# 这种换了说法但明显在谈同一件事的话，仍能被记录下来压低旧理解，
# 又不会因为一个共同词就误杀一条本来正确的假设。
MIN_WEAK_RELATED_GRAMS = 1

# ---------- 自然支持 ----------
# 第一类：自我陈述。带"我"的主张本身就是实质内容，不看尾巴有多长。
SELF_ASSERT_MARKERS = (
    "我确实", "我就是", "我一直", "我一直都", "一直都是",
    "我就是这样", "我向来", "我的确是",
)
# 第二类：纯附和前缀。后面必须跟实质内容，否则"对""是啊"这种空壳
# 会把一条假设白确认了——比漏判糟得多。
AFFIRM_PREFIXES = (
    "可不是", "对啊", "对呀", "嗯对", "是啊", "没错", "是的",
    "对", "嗯", "是",
)
# 附和前缀后必须剩下的实质内容长度（去标点后）
MIN_TAIL_CHARS = 6

# ---------- 变化信号 ----------
# "我改成早上了"不等于"旧规律永久错误"，只是"以前那样现在不成立了"。
# 语义上走降权/失活，不走否决置位。
CHANGE_MARKERS = (
    "改成", "改成了", "改成了", "换成", "换成了", "改为",
    "调整", "变成", "变成了",
    "不再像", "不再是", "不在像",
    "我现在开始", "我最近开始", "已经改成", "现在已经",
    "最近改成", "最近换", "开始改",
)

# ---------- 疑问 ----------
# 提问不是表态。"对吗？""是不是我晚上效率更高？"绝不能变成支持证据。
QUESTION_MARKS = ("?", "？")
QUESTION_WORDS = (
    "是不是", "对不对", "好不好", "有没有", "是不是", "是否", "能否",
    "什么", "怎么", "怎样", "如何", "哪", "几", "多少", "谁",
    "吗", "呢", "么",
)

# 判定结果
SIGNAL_NONE = "none"
SIGNAL_QUESTION = "question"
SIGNAL_DENY = "deny"
SIGNAL_CHANGE = "change"
SIGNAL_SUPPORT = "support"

_PUNCT = " \t\r\n，。！？、；：,.!?;:~…—\"'“”‘’()（）【】《》"


@dataclass(frozen=True)
class NaturalSignal:
    """一句话的判定结果。signal 为 none/question 时 targets 必为空。"""
    signal: str
    excerpt: str = ""      # 触发判定的那个片段（审计用）
    reason: str = ""       # 人类可读的判定理由

    @property
    def is_actionable(self) -> bool:
        return self.signal in (SIGNAL_DENY, SIGNAL_CHANGE, SIGNAL_SUPPORT)


def _strip_punct(text: str) -> str:
    return (text or "").strip(_PUNCT)


def _has_question(text: str) -> bool:
    t = text or ""
    if any(m in t for m in QUESTION_MARKS):
        return True
    return any(w in t for w in QUESTION_WORDS)


def _tail_after_prefix(text: str) -> str:
    """剥掉开头的附和前缀，返回剩余实质内容。"""
    t = _strip_punct(text)
    for p in sorted(AFFIRM_PREFIXES, key=len, reverse=True):
        if t.startswith(p):
            return _strip_punct(t[len(p):])
    return ""


def is_question(text: str) -> bool:
    """这一句是不是在提问。提问永远不是表态。"""
    return _has_question(text)


def has_change_signal(text: str) -> bool:
    """用户是否在说自己变了。"""
    t = text or ""
    return any(m in t for m in CHANGE_MARKERS)


def has_self_assertion(text: str) -> bool:
    """是否是带主语的主张（我确实…/我一直…）。"""
    t = text or ""
    return any(m in t for m in SELF_ASSERT_MARKERS)


def is_natural_support(text: str) -> bool:
    """是否是"用户主动陈述的认同"。

    与 verifier.is_confirmation 的区别（这两件事不是一回事）：
      - is_confirmation 判的是"对一次是非问句的应答"，所以"对"不算；
      - 本函数判的是"用户主动陈述时表达认同"，所以"对，我就是…"算，
        但光秃秃的"对"/"嗯对"仍然不算（没有实质内容）。
    """
    t = text or ""
    if not t:
        return False
    if has_self_assertion(t):
        return True
    tail = _tail_after_prefix(t)
    if not tail:
        return False
    if not any(t.strip().startswith(p) for p in AFFIRM_PREFIXES):
        return False
    return len(tail) >= MIN_TAIL_CHARS


def is_natural_denial(text: str) -> bool:
    """自然否定。直接复用 hypothesis_store 的显式否决词表，不另造一套。"""
    return _is_explicit_denial(text or "")


def classify_natural_signal(text: str) -> NaturalSignal:
    """把一句话判成一种信号。优先级：疑问 > 否定 > 变化 > 支持 > 无。"""
    t = (text or "").strip()
    if not t:
        return NaturalSignal(SIGNAL_NONE, reason="空文本")
    if is_question(t):
        return NaturalSignal(SIGNAL_QUESTION, reason="提问不是表态")
    if is_natural_denial(t):
        hit = next((k for k in ("不对", "不是", "没有", "不要", "错了",
                                "纠正", "胡说") if k in t), "")
        return NaturalSignal(SIGNAL_DENY, excerpt=hit, reason="显式否决词")
    if has_change_signal(t):
        hit = next((m for m in CHANGE_MARKERS if m in t), "")
        return NaturalSignal(SIGNAL_CHANGE, excerpt=hit,
                             reason="用户表示自己变了（降权，不否决）")
    if is_natural_support(t):
        return NaturalSignal(SIGNAL_SUPPORT, reason="用户主动陈述认同")
    return NaturalSignal(SIGNAL_NONE, reason="无表态信号")


# ---------- 话题匹配 ----------
def related_gram_count(text: str, scope: str) -> int:
    """这句话与该 scope 共享多少个非停用二连字。"""
    label = match_label(scope)
    if len(label) < 2:
        return 0
    return len(_content_grams(text) & _content_grams(label))


def label_gram_count(scope: str) -> int:
    """该 scope 的标签能产出多少个非停用二连字。"""
    label = match_label(scope)
    if len(label) < 2:
        return 0
    return len(_content_grams(label))


def match_label(scope: str) -> str:
    """拿来做话题匹配的标签。

    `scope_label()` 只按**第一个**冒号切分，于是
    `background:focus_shift:学习` 会得到 `focus_shift:学习` ——
    规则名混进了标签，gram 全被污染，匹配门槛跟着算错
    （实测 need 算成 2，实际只共享 1 个"学习"，于是永远匹配不上）。

    所以先把已知的 `background:` 剥掉，再交给 scope_label。
    """
    s = str(scope or "").strip()
    if s.startswith("background:"):
        s = s[len("background:"):]
    return scope_label(s)


def required_grams(scope: str, *, strict: bool = True) -> int:
    """这条假设要多少个共同二连字才算相关。

    门槛必须随标签长度缩放，否则功能等于半死：
      - R1 归纳出的 scope 常常只有两个字（如 activity:自习），
        标签总共只能产出 1 个二连字，固定门槛 2 永远达不到；
      - 标签越长（话题越具体），门槛保持 2，避免一个共同词就命中。
    这与 deposit 里 need = min(2, len(entry_kws)) 是同一口径。
    """
    n = label_gram_count(scope)
    if n <= 0:
        return 99                     # 标签太短/全是停用词，不可用于匹配
    if not strict:
        return MIN_WEAK_RELATED_GRAMS
    return max(1, min(MIN_RELATED_GRAMS, n))


def is_related(text: str, scope: str, *, strict: bool = True) -> bool:
    """保守话题相关判定。整个自然证据功能的安全门。"""
    return related_gram_count(text, scope) >= required_grams(scope,
                                                             strict=strict)


def rank_hypotheses(text: str, hypotheses, *, strict: bool = True):
    """按共享二连字数从多到少排序，返回相关假设（分数降序，同分按 id 稳定）。"""
    return [h for _, _, h in scored_hypotheses(text, hypotheses,
                                              strict=strict)]


def scored_hypotheses(text: str, hypotheses, *, strict: bool = True):
    """同 rank_hypotheses，但带上分数——调用方要按强弱区分动作时用。"""
    scored = []
    for h in hypotheses:
        scope = getattr(h, "scope", "") or ""
        n = related_gram_count(text, scope)
        if n >= required_grams(scope, strict=strict):
            scored.append((n, h.id, h))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored


def rank_entries(text: str, entries, *, strict: bool = True):
    """同理，按话题相关度给 context 条目排序。"""
    scored = []
    for e in entries:
        n = related_gram_count(text, getattr(e, "content", "") or "")
        if n >= MIN_WEAK_RELATED_GRAMS:
            scored.append((n, e.id, e))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [e for _, _, e in scored]