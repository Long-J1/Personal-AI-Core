"""待验证候选：产品语言投影。

和 `understanding_view` 同一套思路——**诊断面和产品面必须分开**。

`/api/hypotheses` 是诊断面：置信度、scope、支持/反证计数、evidence 的
source_type 都是给"检查这个系统的人"看的。那些字段一旦端到用户面前，
产品立刻变成一个数据库查询工具，而且用户会因为看不懂
`interaction:hint_first` 而放弃读它。

这里做的是单向投影：内部模型 → 一张人话卡片。

    内部                              卡片
    ────────────────────────────      ─────────────────────────────
    scope=interaction:提示       →    相处方式 · 提示
    statement"用户近期多次…"     →    你最近经常在做「写作业」
    confidence=0.72             →    （不出现在任何地方）
    evidence.source_type        →    （不出现在任何地方）
    support_count=5             →    （不出现在任何地方）
    created_at                  →    "今天"

三条硬规则：

1. **不许编。** 没有原话就说没有，不拿"可能""大概"凑数。
2. **不许重。** "对" 只是"值得继续观察"，不是"这就是事实"。
   所以文案里不出现"确认事实""设为记忆"。
3. **一个内部字段都不许漏。** `assert_no_internal_fields` 每次返回前自检。

状态分三档，让主列表只放"需要你注意的"：

    pending   待验证          需要用户判断，占主列表
    watching  已认可·继续观察  用户认可过但还没成形，折叠区
    settled   已处理          确认/否决/淡出，历史可查
"""
from __future__ import annotations

from datetime import datetime

from .understanding_view import (
    FORBIDDEN_IN_OUTPUT,
    assert_no_internal_fields,
    human_date,
    plain_statement,
    topic_name,
)

__all__ = [
    "FORBIDDEN_IN_OUTPUT", "STATE_TEXT", "build_card", "build_cards",
    "build_view", "assert_no_internal_fields",
]

# 内部状态 → 卡片档位。三个档位分别对应主列表 / 折叠 / 历史。
BUCKET = {
    "tentative": "pending",
    "pending": "pending",          # 后台候选还没毕业，不该出现在这里
    "accepted": "watching",
    "confirmed": "settled",
    "rejected": "settled",
    "expired": "settled",
    "deleted": "settled",
}

STATE_TEXT = {
    "pending": "待验证",
    "watching": "已认可 · 继续观察",
    "settled": "已处理",
}

# 依据区域的收尾句。产品化语言，不出现"假设""置信度""模型"这类词。
_WHY_TAIL = "因此我把它暂时看作一个「可能发生的变化」，等你确认或改口。"


def _quotes(detail: dict, *, now=None) -> list[dict]:
    """证据 → 用户原话。只取真的带原话的，没带就说没有。"""
    out = []
    for e in detail.get("evidence") or []:
        text = (e.get("excerpt") or "").strip()
        if not text:
            continue                      # 沉默的证据不进产品面
        out.append({
            "when": human_date(e.get("created_at_iso"), now) or "",
            "text": text[:120],
        })
    return out[:3]


def _created_of(hypo, detail: dict) -> str:
    """取"形成时间"。

    **两个来源字段名不一样**：Hypothesis 对象给的是 `created_at`，
    get_detail 给的也是 `created_at`（不是 created_at_iso）。
    只认一种就会让"时间"那一栏永远空着——这正是 D023 踩过的坑，
    我第一版又踩了一次。单测里喂的是自己造的 dict，所以全绿；
    真跑一次才发现时间全是空的。
    """
    for obj in (hypo, detail):
        for key in ("created_at_iso", "created_at", "created_at_epoch"):
            v = getattr(obj, key, None) if not isinstance(obj, dict) \
                else obj.get(key)
            if v:
                return v
    return ""


def build_card(hypo, detail: dict | None = None, *,
               now: datetime | None = None) -> dict:
    """一条内部假设 → 一张人话卡片。

    hypo 要带 .id/.statement/.scope/.status；
    detail 是 get_detail 的形状（带 evidence）。
    """
    detail = detail or {}
    status = getattr(hypo, "status", None) or detail.get("status") or "tentative"
    bucket = BUCKET.get(status, "settled")
    scope = getattr(hypo, "scope", "") or detail.get("scope") or ""

    topic = topic_name(scope)
    if not topic:
        # quota_overflow / background 这类不该进产品面。
        # 与其显示成一句看不懂的话，不如根本不出现。
        return {}

    about = plain_statement(getattr(hypo, "statement", "")
                            or detail.get("statement") or "")
    quotes = _quotes(detail, now=now)

    return {
        # id 只给前端调接口用，UI 不渲染它。
        "id": getattr(hypo, "id", "") or detail.get("id") or "",
        "bucket": bucket,
        "state_text": STATE_TEXT[bucket],
        "topic": topic,
        "about": about,
        "when": human_date(_created_of(hypo, detail), now) or "",
        "quotes": quotes,
        "has_evidence": bool(quotes),
        # 待验证才给操作；已处理的给"撤回"以外的东西没有意义。
        "can_act": bucket == "pending",
        # 收尾句只对"还没定论"的候选有意义。
        # 已确认的还写"暂时看作可能的变化"是把语义说反了。
        "why_tail": _WHY_TAIL if bucket in ("pending", "watching") else "",
    }


def build_cards(hypotheses, detail_by_id=None, *,
                now: datetime | None = None) -> list[dict]:
    detail_by_id = detail_by_id or {}
    cards = []
    for h in hypotheses or []:
        hid = getattr(h, "id", "")
        card = build_card(h, detail_by_id.get(hid), now=now)
        if card:
            cards.append(card)
    return cards


def build_view(hypotheses, detail_by_id=None, *,
               now: datetime | None = None) -> dict:
    """完整视图：主列表只放需要用户注意的。"""
    cards = build_cards(hypotheses, detail_by_id, now=now)
    return {
        "pending": [c for c in cards if c["bucket"] == "pending"],
        "watching": [c for c in cards if c["bucket"] == "watching"],
        "settled": [c for c in cards if c["bucket"] == "settled"],
        "counts": {
            "pending": sum(1 for c in cards if c["bucket"] == "pending"),
            "watching": sum(1 for c in cards if c["bucket"] == "watching"),
            "settled": sum(1 for c in cards if c["bucket"] == "settled"),
        },
    }