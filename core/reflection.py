"""周报 Reflection（V0.5）：只读 digest 纯函数，无模型调用、无后台。

build_weekly_digest() 统计过去 7 天，规则拼装 3~5 行中文 + 至多 1 个
gentle 问句。每一行数字都可从 DB count 推导（单测逐行断言），无幻觉空间。
触发只在用户主动打开时（API + 首页只读卡片）；失败必须静默（分段 try）。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("core.reflection")

WEEK_S = 7 * 86400


def _safe(fn, default):
    try:
        return fn()
    except Exception as exc:
        log.warning("周报统计项失败（用默认值）：%s", exc)
        return default


def build_weekly_digest(store, ctx, hyp, pat, goal,
                        *, now: float | None = None) -> dict:
    """返回 {"lines": [...], "question": str, "stats": {...}}。

    stats 键：events, entries, hyp_created, hyp_confirmed, hyp_rejected,
    hyp_asked, pat_created, pat_confirmed, patterns_live, goals_active。
    """
    now = time.time() if now is None else now
    since = now - WEEK_S
    s = {
        "events": _safe(lambda: store.count_events_since(since), 0),
        "entries": _safe(lambda: ctx.count_since(since), 0),
        "hyp_created": _safe(
            lambda: hyp.count_log_since(("create",), since), 0),
        "hyp_confirmed": _safe(
            lambda: hyp.count_log_since(("confirm", "verify_confirm"), since),
            0),
        "hyp_rejected": _safe(
            lambda: hyp.count_log_since(("reject",), since), 0),
        "hyp_asked": _safe(lambda: hyp.count_log_since(("ask",), since), 0),
        "hyp_live": _safe(lambda: hyp.count_live(), 0),
        "pat_created": _safe(
            lambda: pat.count_log_since(("create",), since), 0),
        "pat_confirmed": _safe(
            lambda: pat.count_log_since(("confirm", "verify_confirm"), since),
            0),
        "patterns_live": _safe(lambda: pat.count_live(), 0),
        "goals_active": _safe(lambda: goal.count_active(), 0),
    }
    pending = _safe(
        lambda: (hyp.get_pending() is not None
                 or pat.get_pending() is not None
                 or goal.get_pending() is not None),
        False)
    lines = [
        f"过去7天：新增事件 {s['events']} 件，新增理解 {s['entries']} 条。",
        f"假设：新建 {s['hyp_created']} 个，确认 {s['hyp_confirmed']} 个，"
        f"否决 {s['hyp_rejected']} 个；规律：新建 {s['pat_created']} 个，"
        f"确认 {s['pat_confirmed']} 个；共发起验证提问 {s['hyp_asked']} 次。",
        f"当前：在列假设 {s['hyp_live']} 个，规律 {s['patterns_live']} 条在列，"
        f"长期目标 {s['goals_active']} 个进行中。",
    ]
    # 至多 1 个 gentle 问句，按固定优先级取首个命中（确定性）
    if s["hyp_rejected"] > 0:
        question = (f"有 {s['hyp_rejected']} 个理解被你否决了，"
                    f"要聊聊哪里不对吗？")
    elif pending:
        question = "上次有个问题你还没回，要现在说吗？"
    elif s["pat_created"] > 0:
        question = "发现了新的待验证规律，要逐个过一遍吗？"
    else:
        question = ""
    return {"lines": lines, "question": question, "stats": s}
