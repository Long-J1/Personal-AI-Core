"""后台推理的接线（V0.5.8）。

`core/background_reasoning.py` 是纯逻辑（不碰库），这里是它和真实数据
之间的那一层薄壳：读事件 → 跑规则 → 落成 tentative 候选。

只负责"提出来"，不负责"用起来"——用的是另一套代码的事，
这里一件都不做。
"""
from __future__ import annotations

import logging
import time

from .background_reasoning import (
    BACKGROUND_PREFIX,
    reason_all,
)

log = logging.getLogger("core.background_bridge")

EVENT_SCAN_LIMIT = 400
MAX_PROPOSALS_PER_RUN = 2      # 一次最多摆两条到用户面前


def run_background_reasoning(memory_store, hyp_store, *,
                             now_epoch: float | None = None,
                             scan_limit: int = EVENT_SCAN_LIMIT,
                             max_proposals: int = MAX_PROPOSALS_PER_RUN,
                             ) -> dict:
    """跑一轮后台推理。返回可审计的结果字典。

    铁律：这里**只创建 tentative 候选**，不修改任何已有假设，
    不进 prompt，不参与 pattern。全程 try/except——
    后台推理出问题绝不能掀翻用户对话。
    """
    result = {"scanned": 0, "proposed": [], "skipped": [], "error": None}
    if hyp_store is None:
        return result
    try:
        events = memory_store.list_events(limit=scan_limit)
        result["scanned"] = len(events)
        if not events:
            return result
        # 只读已有理解做漂移判断——不写
        hypotheses = hyp_store.list_hypotheses(live_only=True, limit=200)
        candidates = reason_all(events, hypotheses,
                                now_epoch=time.time() if now_epoch is None
                                else now_epoch)
    except Exception as exc:
        result["error"] = str(exc)
        log.warning("后台推理读数据失败（跳过）：%s", exc)
        return result

    for c in candidates:
        if len(result["proposed"]) >= max_proposals:
            break
        try:
            if hyp_store.background_proposed(c.scope):
                result["skipped"].append({"scope": c.scope,
                                          "why": "已提过/已被否决"})
                continue
            hypo = hyp_store.create_background_candidate(c)
            if hypo is None:
                result["skipped"].append({"scope": c.scope, "why": "落库失败"})
                continue
            result["proposed"].append({"id": hypo.id, "scope": c.scope,
                                       "type": c.type})
        except Exception as exc:
            log.warning("后台推理落库失败（跳过）：%s", exc)
            result["skipped"].append({"scope": c.scope,
                                      "why": f"异常：{exc}"})
    if result["proposed"]:
        log.info("后台推理：扫描 %d 条事件，提出 %d 条推测",
                 result["scanned"], len(result["proposed"]))
    return result


def list_background_candidates(hyp_store) -> list:
    """待用户裁决 / 已认可待成长的后台候选（供 UI 展示）。

    V0.5.9：pending 与 accepted 都不在 LIVE_STATUSES 里，所以必须
    用 live_only=False 再自己按状态筛。
    """
    if hyp_store is None:
        return []
    out = []
    try:
        for h in hyp_store.list_hypotheses(live_only=False, limit=200):
            if str(h.scope or "").startswith(BACKGROUND_PREFIX) \
                    and h.status in ("pending", "accepted"):
                out.append(h)
    except Exception as exc:
        log.warning("后台候选读取失败（跳过）：%s", exc)
    return out