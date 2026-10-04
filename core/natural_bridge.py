"""自然证据与变化的接线（V0.5.6 Phase 3）。

把 core/natural_evidence.py 的纯判定，落到真实的 Context / Hypothesis /
Evidence 上。只做编排，不含判定逻辑（判定唯一真源在 natural_evidence）。

三条铁律：
1. 永不新建假设——自然表达只能确认/否定/降权**已有**理解。
   否则一次普通聊天就能凭空造出长期假设。
2. 永不删除历史——"失活"用 context_entries.memory_status='outdated'，
   假设只吃反证不改 status。历史可追溯。
3. 永不掀翻对话——所有异常吞掉记日志，后台沉淀出问题不能影响用户。
"""
from __future__ import annotations

import logging

from .natural_evidence import (
    MIN_RELATED_GRAMS,
    related_gram_count,
    required_grams,
    MIN_WEAK_RELATED_GRAMS,
    SIGNAL_CHANGE,
    SIGNAL_DENY,
    SIGNAL_SUPPORT,
    classify_natural_signal,
    rank_entries,
    rank_hypotheses,
    scored_hypotheses,
)

log = logging.getLogger("core.natural_bridge")

# 一次最多碰几条假设。相关度排序后截断——宁可少认，不要一次误伤一片。
MAX_TARGETS = 2
ENTRY_SCAN_LIMIT = 200
HYP_SCAN_LIMIT = 200


def _paused(ctx_store, hyp_store) -> bool:
    for st in (hyp_store, ctx_store):
        fn = getattr(st, "paused", None)
        try:
            if callable(fn) and fn():
                return True
            if fn is True:
                return True
        except Exception:
            continue
    return False


def _eligible_hypotheses(hyp_store) -> list:
    """只取还能被自然证据碰的假设。

    V0.5.9：accepted 候选（用户已认可、等真实证据）**要**能被自然证据
    支持——这是它长大的唯一路径。pending（还没人裁决）依旧挡住。

    store 侧还有一道 _natural_eligible 兜底，这里是第二道——
    目的是不让不该碰的候选浪费话题匹配的计算。

    Deep Understanding：合成候选同一套语义。accepted（用户已认可
    的高层推测）**要**能被自然证据支持——那是第十二节故事 1 里
    它长大的唯一入口；pending（还没人裁决）依旧挡住。
    """
    out = []
    for h in hyp_store.list_hypotheses(live_only=False,
                                       limit=HYP_SCAN_LIMIT):
        if h.status not in ("tentative", "confirmed", "accepted"):
            continue
        if (h.scope or "").startswith("quota_overflow:"):
            continue
        if (h.scope or "").startswith("background:") \
                and h.status != "accepted":
            continue
        if (h.scope or "").startswith("synthesis:") \
                and h.status != "accepted":
            continue
        out.append(h)
    return out


# ---------- V0.5.10 soak 暴露的缺口：对话里没法认可候选 ----------
# V0.5.9 把候选生命周期做出来了，但只暴露在 /api/background/*/verdict。
# 用户在聊天里说"对，我最近确实已经改成早上了"——系统听见了这句话，
# 却不会把它当成对某条候选的裁决。soak 故事第 E 阶段因此走不通。
#
# 这里补一条最小通道：只在**话题强相关**且信号明确时才把裁决落下去，
# 不做任何模糊匹配（那是 pending route cross-talk 的来源）。
PENDING = "pending"
BACKGROUND_PREFIX = "background:"


def resolve_pending_candidate(hyp_store, user_text: str,
                              signal: str) -> str | None:
    """用户对某条待裁决候选说"对 / 不对"。返回被裁决的候选 id。

    只处理 status=pending 的后台候选——accepted 已经裁决过了，
    rejected / expired 是结束态，都不该被一句话翻回去。
    """
    if hyp_store is None:
        return None
    try:
        cands = hyp_store.list_pending_background()
    except Exception as exc:
        log.warning("读待裁决候选失败（跳过）：%s", exc)
        return None
    best, best_n = None, 0
    for h in cands:
        n = related_gram_count(user_text, h.scope)
        # 门槛随标签长度缩放——写死 2 的话，"学习"这种两字标签
        # （最多产出 1 个二连字）永远匹配不上，候选就再也裁决不了。
        if n >= required_grams(h.scope, strict=True) and n > best_n:
            best, best_n = h, n
    if best is None:
        return None

    # 候选自己声明的是哪一类猜测，用户的表态要按那一类读。
    #
    # 踩过的坑：分类器里"变化"优先于"支持"，所以
    # 「对，我最近已经改成早上学习了。」被判成 change。可这条消息对的
    # 候选恰恰是 behavior_change / habit_drift——用户说的正是"确实变了"，
    # 那是**印证**。一刀切当成否定，会把正确的猜测杀掉。
    #
    # focus_shift（最近主要精力在 X）不一样：用户说"我改成早上了"
    # 与它无关，不该动。
    try:
        ctype = str(best.scope or "").split(":", 2)[1] if ":" in best.scope else ""
    except Exception:
        ctype = ""
    if signal == "deny":
        verdict = "wrong"
    elif signal == "support":
        verdict = "correct"
    elif signal == "change" and ctype in ("behavior_change", "habit_drift"):
        verdict = "correct"
    else:
        return None                       # 猜不出来就不动

    try:
        if not hyp_store.record_background_verdict(best.id, verdict, user_text):
            return None
    except Exception as exc:
        log.warning("对话裁决候选失败（跳过）：%s", exc)
        return None
    log.info("对话裁决候选 %s → %s（相关度 %d）", best.id[:8], verdict, best_n)
    return best.id


def apply_natural_signal(
    ctx_store,
    hyp_store,
    user_text: str,
    *,
    exclude_entry_ids=(),
    max_targets: int = MAX_TARGETS,
) -> dict:
    """把这一轮用户自然表达接到已有理解上。返回可审计的结果字典。

    exclude_entry_ids：本轮刚写入的 Context（用户就是在这条之上说"我改成
    …了"，不能反过来把它自己标成 outdated）。
    """
    result = {
        "signal": "none", "reason": "", "excerpt": "",
        "hypo_ids": [], "entry_ids": [], "error": None,
    }
    if hyp_store is None:
        return result

    sig = classify_natural_signal(user_text or "")
    result["signal"] = sig.signal
    result["reason"] = sig.reason
    result["excerpt"] = sig.excerpt
    if not sig.is_actionable:
        return result

    try:
        if _paused(ctx_store, hyp_store):
            result["reason"] = "已暂停记忆，本轮不接线"
            return result

        # V0.5.10：对话里的"对 / 不对"可以裁决待定候选。
        # 只在信号明确 + 话题强相关时才落下去。
        #
        # 裁决过的这一条，**本回合不要再当证据对象**。
        # 实测踩过：「对，我最近已经改成早上学习了。」这一句
        # 同时产出了 support（裁决=你认可）和 contradict
        # （证据扫描=你说变了），同一条候选 conf 0.53→0.28。
        # 裁决已经把这句话消化过了，再算一遍是重复计数。
        just_verdicted: set[str] = set()
        try:
            vid = resolve_pending_candidate(hyp_store, user_text or "",
                                            sig.signal)
            if vid:
                just_verdicted.add(vid)
                result["verdict_candidate"] = vid
                result["hypo_ids"].append(vid)
        except Exception as exc:
            log.warning("对话裁决候选异常（跳过）：%s", exc)

        exclude = set(exclude_entry_ids or ())
        hyps = [h for h in _eligible_hypotheses(hyp_store)
                if h.id not in just_verdicted]

        if sig.signal == SIGNAL_SUPPORT:
            targets = rank_hypotheses(user_text or "", hyps)[:max_targets]
            for h in targets:
                try:
                    if hyp_store.record_natural_evidence(
                            h.id, user_text or "", SIGNAL_SUPPORT):
                        result["hypo_ids"].append(h.id)
                except Exception as exc:
                    log.warning("自然支持落链失败（跳过）：%s", exc)

        elif sig.signal == SIGNAL_DENY:
            # 先用弱匹配捞出候选，再按相关强度决定动作强弱：
            #   强相关 → reject（不可逆，用户明确说了"不是"）
            #   弱相关 → 只吃反证（可逆，证据不足以判死一条假设）
            scored = scored_hypotheses(user_text or "", hyps, strict=False)
            for n, _hid, h in scored[:max_targets]:
                try:
                    if hyp_store.record_natural_evidence(
                            h.id, user_text or "",
                            SIGNAL_DENY if n >= MIN_RELATED_GRAMS
                            else "deny_weak"):
                        result["hypo_ids"].append(h.id)
                except Exception as exc:
                    log.warning("自然否定落链失败（跳过）：%s", exc)

        elif sig.signal == SIGNAL_CHANGE:
            _apply_change(ctx_store, hyp_store, user_text or "",
                          hyps, exclude, max_targets, result)

    except Exception as exc:            # 兜底：后台链路绝不掀翻对话
        result["error"] = str(exc)
        log.warning("自然证据接线异常（跳过，不影响对话）：%s", exc)

    return result


def _apply_change(ctx_store, hyp_store, user_text, hyps, exclude,
                  max_targets, result) -> None:
    """变化：旧理解失活/降权，但一条历史都不删。

    这里用**弱匹配**（1 个二连字）是有意的：用户说"我改成早上学习了"
    时，旧理解往往写的是"晚上十点后"，只共享"学习"一个词——强匹配下
    这条功能等于不存在。但失活与降权都是**可逆**的（outdated 可恢复、
    status 不动），所以宁可放过一点，也不要因为措辞不同就完全看不见
    用户已经变了。不可逆的否决仍然只在强匹配时才允许。
    """
    # ① 话题相关的旧 Context → outdated（prompt 立刻不再读它，历史仍在）
    entries = []
    if ctx_store is not None:
        try:
            entries = [e for e in ctx_store.list_entries(
                active_only=True, limit=ENTRY_SCAN_LIMIT)
                if e.id not in exclude]
        except Exception as exc:
            log.warning("变化信号读取理解失败（跳过 Context 侧）：%s", exc)
    related = rank_entries(user_text, entries, strict=False)

    covered: set[str] = set()
    for e in related[:max_targets * 2]:
        reason = f"用户表示自己变了：{(user_text or '')[:80]}"
        try:
            if ctx_store is not None and ctx_store.set_memory_status(
                    e.id, "outdated", reason):
                result["entry_ids"].append(e.id)
        except Exception as exc:
            log.warning("变化信号标记理解失活失败（跳过）：%s", exc)
            continue
        # 以这条 Context 为来源的假设同步吃反证（既有机制，非新建）
        try:
            affected = hyp_store.record_correction_hit(
                e.id, user_text, reason) or []
            covered.update(affected)
        except Exception as exc:
            log.warning("变化信号联动假设失败（跳过）：%s", exc)

    # ② 没有对应 Context 可追溯的假设（如来自事件归纳的）：以本次
    #    用户行为本身作为来源吃反证，状态保留不判死。
    for n, _hid, h in scored_hypotheses(
            user_text, hyps, strict=False)[:max_targets]:
        if h.id in covered:
            continue
        try:
            if hyp_store.record_natural_evidence(
                    h.id, user_text, SIGNAL_CHANGE):
                result["hypo_ids"].append(h.id)
        except Exception as exc:
            log.warning("变化信号降权假设失败（跳过）：%s", exc)