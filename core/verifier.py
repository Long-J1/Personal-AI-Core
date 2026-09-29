"""验证提问选择器（V0.4）：搭便车，不拦路。

只做两件事（全是确定性规则，无 LLM、无定时器、无后台）：
1. select_candidate：在用户主动发起的对话轮次里，挑一个值得顺带验证的假设；
2. resolve_pending_answer：把用户下一轮回答分类为确认/否决/回避，写入已有证据链。

沉默默认：任一条件不满足即返回 None，调用方（conversation.chat）照常回答，
绝不主动发起任何事。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from memory.hypothesis_store import (
    Hypothesis,
    HypothesisStore,
    _is_explicit_denial,
)

log = logging.getLogger("core.verifier")

# ---------- 选择常量（单测锁定）----------
CONFIRM_BAND = (0.50, 0.70)   # 待验证区间：再高不需问，再低不值得问
ASK_COOLDOWN_S = 86400        # 同一假设两次提问至少隔 24h
MAX_ASKS = 3                  # 问三次不答就闭嘴
MAX_QUESTION_CHARS = 120      # 问题文本长度上限
MIN_LABEL_LEN = 2             # scope 关键词太短不做话题匹配（防"在/的"误命中）

# 确认词（保守子集；"嗯/好/对"这种歧义的一律不算，宁可漏判）
CONFIRM_KEYWORDS = ("对的", "没错", "是的", "是这样", "没问题", "同意", "确实如此")
# 否决词扫描前的疑问剥离（与 V0.3 剥离表同理："是不是…"是提问不是确认）
_CONFIRM_STRIP = ("是不是", "有没有")
_CONFIRM_PUNCT = "吗？?。！，、 \t\n"
# V0.3 否决词之外的验证场景否决（"不同意"含"同意"，必须先判否决）
VERIFY_DENY_EXTRA = ("不同意",)


@dataclass
class VerificationQuestion:
    hypothesis_id: str
    text: str
    confidence: float
    reason: str
    kind: str = "hypothesis"  # hypothesis | pattern | goal

    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "text": self.text,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "kind": self.kind,
        }


# ---------- 话题相关 ----------
def scope_label(scope: str) -> str:
    """interaction:提醒 → 提醒；无冒号则整体作标签。"""
    scope = (scope or "").strip()
    if ":" in scope:
        _, _, label = scope.partition(":")
        return label.strip()
    return scope


def is_related(message: str, scope: str) -> bool:
    """本轮消息是否聊到该假设的主题（子串匹配，保守）。"""
    label = scope_label(scope)
    return len(label) >= MIN_LABEL_LEN and label in (message or "")


# ---------- 回答分类（否决优先）----------
def is_denial(text: str) -> bool:
    return _is_explicit_denial(text) or any(
        k in (text or "") for k in VERIFY_DENY_EXTRA)


def _normalize_confirm(text: str) -> str:
    t = text or ""
    for pat in _CONFIRM_STRIP:
        t = t.replace(pat, "")
    return t.strip(_CONFIRM_PUNCT)


def is_confirmation(text: str) -> bool:
    t = _normalize_confirm(text)
    return any(k in t for k in CONFIRM_KEYWORDS) and len(t) > 0


# ---------- 问题拼装 ----------
def _short_claim(scope: str, label: str) -> str:
    if scope.startswith("activity:"):
        return f"你最近在持续{label}"
    if scope.startswith("interaction:"):
        return f"你在{label}方面希望我保持一致"
    return f"「{label}」这条理解是对的"


def build_question(hyp: Hypothesis) -> tuple[str, str]:
    """返回 (问题文本, 选择原因)。文本自带来源三要素：哪个主题、为什么问、当前置信度。"""
    label = scope_label(hyp.scope)
    show = label if len(label) <= 12 else label[:12] + "…"
    text = (
        f"多问一句（验证我的理解：关于「{show}」我目前只有约"
        f"{hyp.confidence:.0%}把握，依据是「{show}」相关的"
        f"{hyp.support_count}条记录）：{_short_claim(hyp.scope, show)}，对吗？"
    )
    if len(text) > MAX_QUESTION_CHARS:
        # 极端长 scope 兜底：保问题形态（结尾"对吗？"不许被截掉）
        text = text[:MAX_QUESTION_CHARS - 4] + "…对吗？"
    reason = (f"独立支持源不足（{hyp.support_count}条证据仍只算"
              f"同一批确认，需要第二个独立确认）；与本轮话题「{show}」相关")
    return text, reason


# ---------- 选择 ----------
def select_candidate(
    hyp_store: HypothesisStore,
    message: str,
    *,
    paused: bool,
    now: float | None = None,
) -> tuple[Hypothesis, VerificationQuestion] | None:
    """按置信度倒序取首个满足全部条件的 tentative 假设，不满足即 None。"""
    if paused:
        return None
    if hyp_store.get_pending() is not None:
        return None  # 同时只挂一个
    now = time.time() if now is None else now
    for h in hyp_store.list_hypotheses(live_only=True, limit=200):
        if h.status != "tentative":
            continue
        if not (CONFIRM_BAND[0] <= h.confidence < CONFIRM_BAND[1]):
            continue
        try:
            indep = hyp_store.independent_support_count(h.id)
        except Exception as exc:
            log.warning("独立源计数失败，跳过该假设：%s", exc)
            continue
        if indep >= 2:
            continue
        if h.asked_count >= MAX_ASKS:
            continue
        if now - (h.last_asked_epoch or 0) <= ASK_COOLDOWN_S:
            continue
        if not is_related(message or "", h.scope):
            continue
        text, reason = build_question(h)
        return h, VerificationQuestion(
            hypothesis_id=h.id, text=text,
            confidence=h.confidence, reason=reason)
    return None


# ---------- 回答分类与落链 ----------
def resolve_pending_answer(
    hyp_store: HypothesisStore, message: str
) -> dict | None:
    """分类用户对挂起问题的回答并写入证据链。无挂起返回 None。

    返回 {"hypothesis_id", "verdict": "confirm"|"deny"|"evade", "confidence"}。
    失败只记日志不抛异常（调用方绝不能因此断掉对话）。
    """
    try:
        pending = hyp_store.get_pending()
    except Exception as exc:
        log.warning("读取挂起问题失败：%s", exc)
        return None
    if pending is None:
        return None
    try:
        if is_denial(message or ""):
            hyp_store.reject(pending.id, (message or "")[:200],
                             channel="verification")
            h = hyp_store.get(pending.id)
            return {"hypothesis_id": pending.id, "verdict": "deny",
                    "confidence": round(h.confidence, 2) if h else 0.25}
        if is_confirmation(message or ""):
            if not hyp_store.record_verification_confirm(
                    pending.id, message or ""):
                return None
            h = hyp_store.get(pending.id)
            return {"hypothesis_id": pending.id, "verdict": "confirm",
                    "confidence": round(h.confidence, 2) if h else 0.5}
        hyp_store.note_evasive(pending.id)
        return {"hypothesis_id": pending.id, "verdict": "evade",
                "confidence": round(pending.confidence, 2)}
    except Exception as exc:
        log.warning("验证回答落链失败（跳过）：%s", exc)
        return None


# ---------- Pattern 验证（V0.5：同一套规则，另一张表）----------
def select_pattern_candidate(
    pat_store,
    message: str,
    *,
    paused: bool,
    now: float | None = None,
):
    """tentative 规律同规则选人（band/独立源/冷却/次数/话题）。"""
    if paused:
        return None
    if pat_store.get_pending() is not None:
        return None
    now = time.time() if now is None else now
    for h in pat_store.list_patterns(live_only=True, limit=200):
        if h.status != "tentative":
            continue
        if not (CONFIRM_BAND[0] <= h.confidence < CONFIRM_BAND[1]):
            continue
        try:
            indep = pat_store.independent_support_count(h.id)
        except Exception as exc:
            log.warning("独立源计数失败，跳过该规律：%s", exc)
            continue
        if indep >= 2:
            continue
        if h.asked_count >= MAX_ASKS:
            continue
        if now - (h.last_asked_epoch or 0) <= ASK_COOLDOWN_S:
            continue
        if not is_related(message or "", h.scope):
            continue
        text, reason = build_question(h)  # 鸭子类型：Pattern 属性齐备
        return h, VerificationQuestion(
            hypothesis_id=h.id, text=text,
            confidence=h.confidence, reason=reason, kind="pattern")
    return None


def resolve_pattern_answer(pat_store, message: str) -> dict | None:
    """分类用户对挂起规律问题的回答并落链。"""
    try:
        pending = pat_store.get_pending()
    except Exception as exc:
        log.warning("读取挂起规律问题失败：%s", exc)
        return None
    if pending is None:
        return None
    try:
        if is_denial(message or ""):
            pat_store.reject(pending.id, (message or "")[:200],
                             channel="verification")
            h = pat_store.get(pending.id)
            return {"hypothesis_id": pending.id, "verdict": "deny",
                    "confidence": round(h.confidence, 2) if h else 0.25,
                    "kind": "pattern"}
        if is_confirmation(message or ""):
            if not pat_store.record_verification_confirm(
                    pending.id, message or ""):
                return None
            h = pat_store.get(pending.id)
            return {"hypothesis_id": pending.id, "verdict": "confirm",
                    "confidence": round(h.confidence, 2) if h else 0.6,
                    "kind": "pattern"}
        pat_store.note_evasive(pending.id)
        return {"hypothesis_id": pending.id, "verdict": "evade",
                "confidence": round(pending.confidence, 2),
                "kind": "pattern"}
    except Exception as exc:
        log.warning("规律验证回答落链失败（跳过）：%s", exc)
        return None


# ---------- Goal 确认（V0.5：只收用户原话，只做关联）----------
GOAL_MARKERS = ("我的目标是", "我想成为", "我想考上", "我要考上", "长期目标")


def is_goal_declaration(message: str) -> bool:
    return any(m in (message or "") for m in GOAL_MARKERS)


def extract_goal_title(message: str) -> str:
    """标题 = 首个标记之后的内容（<=60 字）；兜底整句截断。确定性。"""
    text = (message or "").strip()
    first = None
    for m in GOAL_MARKERS:
        i = text.find(m)
        if i >= 0 and (first is None or i < first[0]):
            first = (i, m)
    if first is not None:
        title = text[first[0] + len(first[1]):].strip(" ,.，。！？、:：")
        if title:
            return title[:60]
    return text[:60]


def build_goal_question(goal) -> tuple[str, str]:
    show = goal.title if len(goal.title) <= 20 else goal.title[:20] + "…"
    text = (f"要把「{show}」记为你的长期目标吗？"
            f"之后我会帮你关联相关的规律和进展，对吗？")
    if len(text) > MAX_QUESTION_CHARS:
        text = text[:MAX_QUESTION_CHARS - 4] + "…对吗？"
    reason = "你明确表达了目标，需你确认才记入（AI 不推测目标）"
    return text, reason


def select_goal_candidate(
    goal_store,
    message: str,
    *,
    paused: bool,
    now: float | None = None,
):
    """proposed 目标：话题相关（标题关键词或目标标记）才问。"""
    if paused:
        return None
    if goal_store.get_pending() is not None:
        return None
    now = time.time() if now is None else now
    from core.context import extract_keywords
    for g in goal_store.list_goals(live_only=True, limit=200):
        if g.status != "proposed":
            continue
        keys = {k for k in extract_keywords(g.title) if len(k) >= 2}
        related = (is_goal_declaration(message or "")
                   or any(k in (message or "") for k in keys))
        if not related:
            continue
        if g.asked_count >= MAX_ASKS:
            continue
        if now - (g.last_asked_epoch or 0) <= ASK_COOLDOWN_S:
            continue
        text, reason = build_goal_question(g)
        return g, VerificationQuestion(
            hypothesis_id=g.id, text=text, confidence=1.0,
            reason=reason, kind="goal")
    return None


def resolve_goal_answer(goal_store, message: str) -> dict | None:
    """分类用户对挂起目标问题的回答。确认→active；否决→rejected；回避→留挂起。"""
    try:
        pending = goal_store.get_pending()
    except Exception as exc:
        log.warning("读取挂起目标问题失败：%s", exc)
        return None
    if pending is None:
        return None
    try:
        if is_denial(message or ""):
            goal_store.reject(pending.id, (message or "")[:200])
            return {"hypothesis_id": pending.id, "verdict": "deny",
                    "confidence": 1.0, "kind": "goal"}
        if is_confirmation(message or ""):
            if not goal_store.confirm(pending.id):
                return None
            return {"hypothesis_id": pending.id, "verdict": "confirm",
                    "confidence": 1.0, "kind": "goal"}
        goal_store.note_evasive(pending.id)
        return {"hypothesis_id": pending.id, "verdict": "evade",
                "confidence": 1.0, "kind": "goal"}
    except Exception as exc:
        log.warning("目标回答落链失败（跳过）：%s", exc)
        return None


# ---------- Pattern 用户表达检测（V0.5 B 路径入口）----------
# 用户明确说出规律性陈述时的标记（子串匹配，保守；命中只建 tentative）。
PATTERN_MARKERS = ("我发现我", "我总是", "每次都", "固定模式", "我的习惯是")


def is_pattern_declaration(message: str) -> bool:
    return any(m in (message or "") for m in PATTERN_MARKERS)


def pattern_scope_for(message: str) -> str:
    """B 路径 scope：首个有效关键词的首个二连字，无则 自述（确定性）。

    不取整串长词：整串作标签会让话题相关过于严格（用户几乎不可能
    原样复述七八个字），二连字召回优先；误命中由冷却/次数/否决兜底。
    """
    from core.context import extract_keywords
    for k in extract_keywords(message or ""):
        if len(k) >= 2:
            return f"user:{k[:2]}"
    return "user:自述"
