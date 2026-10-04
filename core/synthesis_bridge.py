"""Cross-Context Synthesis 的接线（Deep Understanding Phase 1）。

`core/synthesis.py` 是纯逻辑（不碰库），这里是它和真实数据之间的薄壳：
读事件 / 读目标 / 读已有候选 → 统一成 Member → 跑规则 → 落成 pending 候选。

**只负责"提出来"。** 不改已有假设、不进 prompt、不参与 pattern、
不吃自然证据（除非用户认可它并由既有的 accepted 白名单放行）。
全程 try/except——合成失败绝不能掀翻用户对话。
"""
from __future__ import annotations

import json
import logging
import time

from .synthesis import (
    MEMBER_SCAN_LIMIT,
    Member,
    is_synthesis,
    synthesize,
)

log = logging.getLogger("core.synthesis_bridge")

MAX_PROPOSALS_PER_RUN = 1      # 高层推测一次只摆一条——摆多了像算命

# 变化检测只针对这两类候选（Story 2 讲的是"旧模式可能不成立了"）
DECAY_WATCH_RULES = ("exploration_depth",)


def _member_from_event(store, e) -> Member | None:
    """事件 → Member。

    day 取**事件发生日**而不是写入日：`_row_day` 的独立源判定也是按
    发生日算的，两边必须同口径，否则会出现"这里算 3 天、那边算 1 天"。
    """
    import datetime as _dt
    from .background_reasoning import _epoch
    ts = _epoch(e)
    day = _dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d") \
        if ts else time.strftime("%Y-%m-%d")
    desc = str(getattr(e, "description", "") or "").strip()
    act = str(getattr(e, "activity", "") or "").strip()
    if not desc and not act:
        return None
    return Member(id=str(getattr(e, "id", "") or ts),
                  source_type="event", activity=act,
                  description=desc, day=day, epoch=ts)


def _create_detail(hyp_store, hypo_id: str) -> dict:
    """取候选**创建那一条** log 的 detail（聚合结构都记在这里）。

    必须按 action='create' 取，不能"遍历所有 log 找第一个有 activities 的"——
    后续的 verdict / evidence / promote 记录会覆盖结果，而症状是
    "变化检测静默不触发"，看不出是取错了行。
    """
    try:
        with hyp_store._connect() as con:      # noqa: SLF001 —— 同包内读
            row = con.execute(
                "SELECT detail FROM hypothesis_log WHERE hypothesis_id=?"
                " AND action='create' ORDER BY epoch LIMIT 1",
                (hypo_id,)).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    try:
        return json.loads(row["detail"] or "{}")
    except (ValueError, TypeError):
        return {}


def _reference_of(hyp_store, hypo_id: str) -> tuple:
    """候选 → (活动名, 分界时刻)。变化检测的参照系。

    分界时刻 = **支撑这条模式的那批经历里最晚的一条**，取自 create 时
    写进 detail 的 `evidence_until`。

    为什么不用候选的 created_at：候选是"刚才"建的，支撑它的经历都在
    过去，于是"分界之后"一条经历都没有，检测永远静默不触发——
    不报错、不产出、看起来像没实现（第一版就栽在这儿）。

    活动名两个键都认：Pattern A 的 detail 用 `contexts`，
    Pattern C 用 `activities`。只认一个会让另一个静默失效。
    """
    d = _create_detail(hyp_store, hypo_id)
    acts = d.get("activities") or d.get("contexts") or []
    if not acts:
        return ()
    since = 0.0
    try:
        since = float(d.get("evidence_until") or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    return ("、".join(str(a) for a in acts), since)


def _is_synthesis_scope(scope: str) -> bool:
    """这条 scope 是不是来自跨上下文合成。

    **两种前缀都要认**：
      - `synthesis:{rule}:{topic}`  —— 还在候选阶段
      - `inferred:{rule}:{topic}`   —— 已毕业成正式理解

    只认前者是个真 bug：模式一旦毕业，scope 就换成 `inferred:`，
    于是变化检测**再也看不见它**——用户后来完全不再那样了，
    系统却因为"那条记录不在我的参照系里"而彻底沉默。
    症状是"故事 B 什么都没发生"，而且不报错。
    """
    s = str(scope or "")
    if is_synthesis(s):
        return True
    if s.startswith("inferred:"):
        return s[len("inferred:"):].split(":", 1)[0] in DECAY_WATCH_RULES
    return False


def _synthesis_rule_of(scope: str) -> str:
    """取出规则名（exploration_depth / feedback_continuity / ...）。"""
    s = str(scope or "")
    if is_synthesis(s):
        parts = s.split(":", 2)
    elif s.startswith("inferred:"):
        parts = s[len("inferred:"):].split(":", 1)
        parts = ["inferred"] + parts
    else:
        return ""
    return parts[1] if len(parts) >= 2 else ""


def _existing_synthesis_topics(hyp_store) -> dict:
    """已有合成理解 → (活动名, 分界时刻)。变化检测的参照系。

    **包含已毕业的正式理解**（`_is_synthesis_scope` 两种前缀都认）。
    这是故事 B 能成立的前提：真正的风险不在"候选还悬着"的时候，
    而在"系统已经把这条当正式理解、而用户早已不是这样了"的时候。

    两个键都要：只有活动名而没有形成时刻，检测会把**当初支撑旧模式的
    那批深投入记录**一起算进分母，比例永远降不下来。

    rejected / deleted / expired 一律跳过：用户已经否决过的模式，
    不该再被拿来说"它变了"——那等于绕过他的一次明确否定。
    """
    out: dict[str, tuple] = {}
    try:
        for h in hyp_store.list_hypotheses(live_only=False, limit=200):
            if not _is_synthesis_scope(h.scope):
                continue
            if h.status in ("rejected", "deleted", "expired"):
                continue
            if _synthesis_rule_of(h.scope) not in DECAY_WATCH_RULES:
                continue
            ref = _reference_of(hyp_store, h.id)
            if ref:
                out[h.scope] = ref
    except Exception as exc:
        log.warning("读取已有合成候选失败（跳过变化检测）：%s", exc)
    return out


def run_synthesis(memory_store, hyp_store, goal_store=None, *,
                  now_epoch: float | None = None,
                  scan_limit: int = MEMBER_SCAN_LIMIT,
                  max_proposals: int = MAX_PROPOSALS_PER_RUN) -> dict:
    """跑一轮跨上下文合成。返回可审计的结果字典。

    铁律：这里**只创建 pending 候选**。候选的置信度天然低于
    INIT_CONFIDENCE，所以即便将来有代码误把它当普通假设处理，
    也永远不会跨过"看起来像正式理解"那条线。
    """
    result = {"scanned": 0, "proposed": [], "skipped": [], "error": None}
    if hyp_store is None:
        return result
    try:
        events = memory_store.list_events(limit=scan_limit)
        result["scanned"] = len(events)
        members = [m for m in (_member_from_event(memory_store, e)
                               for e in events) if m is not None]
        goals = []
        if goal_store is not None:
            for g in goal_store.list_active(limit=10):
                goals.append({"title": getattr(g, "title", ""),
                              "status": getattr(g, "status", ""),
                              "epoch": float(
                                  getattr(g, "created_at_epoch", 0.0) or 0.0),
                              "scope": "goal"})
        # 变化检测（第十一节 / 故事 2）需要"已有候选"作参照。
        # 在 synthesize 之前一次性读出来，不放进落库循环里——
        # 放循环里等于每提一条候选就重查一遍库，而候选之间互不影响。
        existing = _existing_synthesis_topics(hyp_store)
        candidates = synthesize(members, goals=goals,
                                existing_scopes=existing)
    except Exception as exc:
        result["error"] = str(exc)
        log.warning("跨上下文合成读数据失败（跳过）：%s", exc)
        return result

    # **变化候选优先**。
    #
    # 一轮只摆 max_proposals 条（摆多了像算命），而 synthesize() 产出的
    # 顺序是"新建在前、变化在后"。于是只要某条新建候选先抢到配额，
    # decay 就永远被砍掉——故事 B 的症状是：旧模式已毕业成正式理解、
    # 用户后来完全不是那样了，系统却一条变化都不提。
    #
    # 而且 decay **确实更紧急**：它针对的是一条**已经在影响回答**的
    # 正式理解（graduation 换前缀后已进入 prompt），而新建候选只是
    # 一个还没人看过的猜想。拿紧急的去给不紧急的让路是错的。
    ordered = ([c for c in candidates if c.pattern == "decay"]
               + [c for c in candidates if c.pattern != "decay"])
    for c in ordered:
        if len(result["proposed"]) >= max_proposals:
            break
        try:
            if hyp_store.synthesis_proposed(c.scope):
                result["skipped"].append({"scope": c.scope,
                                          "why": "已提过/已被否决"})
                continue
            hypo = hyp_store.create_synthesis_candidate(c)
            if hypo is None:
                result["skipped"].append({"scope": c.scope, "why": "落库失败"})
                continue
            result["proposed"].append({"id": hypo.id, "scope": c.scope,
                                       "pattern": c.pattern})
        except Exception as exc:
            log.warning("合成候选落库失败（跳过）：%s", exc)
            result["skipped"].append({"scope": c.scope,
                                      "why": f"异常：{exc}"})
    if result["proposed"]:
        log.info("跨上下文合成：扫描 %d 条经历，提出 %d 条高层推测",
                 result["scanned"], len(result["proposed"]))
    return result


def list_synthesis_candidates(hyp_store) -> list:
    """待用户裁决 / 已认可待成长的合成候选（供「我正在思考」展示）。

    pending 与 accepted 都不在 LIVE_STATUSES 里，必须 live_only=False
    再自己按状态筛——和 background 那套同构。
    """
    if hyp_store is None:
        return []
    out = []
    try:
        for h in hyp_store.list_hypotheses(live_only=False, limit=200):
            if is_synthesis(h.scope) and h.status in ("pending", "accepted"):
                out.append(h)
    except Exception as exc:
        log.warning("合成候选读取失败（跳过）：%s", exc)
    return out
