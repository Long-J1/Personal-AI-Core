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


def _existing_synthesis_topics(hyp_store) -> dict:
    """已有候选的 scope → (活动名, 形成时刻)。变化检测的参照系。

    两个键都要：只有活动名而没有形成时刻，检测会把**当初支撑旧模式的
    那批深投入记录**一起算进分母，比例永远降不下来——故事 2 明明发生了
    却检测不到。时刻让"后来变浅了"有明确的分母。

    rejected / deleted / expired 一律跳过：用户已经否决过的模式，
    不该再被拿来说"它变了"——那等于绕过他的一次明确否定。
    """
    out: dict[str, tuple] = {}
    try:
        for h in hyp_store.list_hypotheses(live_only=False, limit=200):
            if not is_synthesis(h.scope):
                continue
            if h.status in ("rejected", "deleted", "expired"):
                continue
            parts = h.scope.split(":", 2)
            if len(parts) < 3:
                continue
            rule = parts[1]
            if rule not in DECAY_WATCH_RULES:
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

    for c in candidates:
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
