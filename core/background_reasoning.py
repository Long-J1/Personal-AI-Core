"""Background Reasoning：后台整理与发现趋势（V0.5.8）。

第一版只做一件事：**在用户不说话的时候，替他看一眼自己的记录，
发现"好像有什么变了"，然后把这句话摆到用户面前等他判断。**

这不是新能力，是把已有的三样东西接起来看：
    events（他做了什么）+ hypotheses（系统已经理解什么）+ 时间窗

三条铁律（贯穿整个模块）：

1. **猜测不是事实。** 产出永远是 candidate，状态恒为 tentative。
   想变成理解，只有一条路：用户自己点"正确"。
2. **不许自作主张。** 本模块不改任何已有假设，不进 prompt，
   不参与 pattern 生成，不吃自然证据。
3. **纯逻辑。** 不碰数据库、不联网、无副作用——所有 IO 都在
   `HypothesisStore` 与 `conversation` 那侧。这样每条规则都能
   单测，也能被回放逐字验证。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

# ---------- 时间窗 ----------
# 用"事件发生日"切窗。默认 14 天对 7 天：样本太少的窗口推不出趋势，
# 而趋势本来就该看一个量级而不是一天。
DEFAULT_OLD_DAYS = 14
DEFAULT_NEW_DAYS = 7
MIN_EVENTS_PER_WINDOW = 3      # 任一窗口少于这么多次就不推
MIN_RATIO_SHIFT = 0.50         # 占比变化至少要这么多才叫"变化"
MIN_SLOT_EVENTS = 2            # 某个时段本身至少要有这么多次，否则是噪声
MIN_FOCUS_EVENTS = 3           # 近期主要投入方向的最低记录数
MIN_FOCUS_RATIO = 0.50
MIN_DRIFT_CONFIDENCE = 0.58    # 旧理解得先站得住脚，才谈漂移

# candidate scope 前缀（与 quota_overflow 同构：存但不生效）
BACKGROUND_PREFIX = "background:"

# ---------- 时段词典 ----------
# 事件表里没有"一天中的什么时候"这个字段，只能从描述里认。
# 刻意保守：认不出就是空，宁可不分组也不乱分。
SLOT_WORDS = {
    "早晨": ("早上", "早晨", "清晨", "一早", "起床", "晨读"),
    "上午": ("上午",),
    "中午": ("中午", "午休"),
    "下午": ("下午",),
    "晚上": ("晚上", "夜里", "深夜", "今晚", "晚自习", "夜晚"),
}
SLOT_ORDER = ("早晨", "上午", "中午", "下午", "晚上")

# candidate 类型
TYPE_BEHAVIOR_CHANGE = "behavior_change"   # 同一件事的时段变了
TYPE_HABIT_DRIFT = "habit_drift"            # 旧理解对应的习惯在漂移
TYPE_FOCUS_SHIFT = "focus_shift"            # 最近主要投入方向


@dataclass(frozen=True)
class Candidate:
    """一条后台推测。

    刻意做成 frozen：后台推理的产物一旦生成就不许在别处被悄悄改写，
    要改就重新跑一遍规则。
    """
    type: str
    topic: str                      # 人类可读的主题（"学习时间"）
    scope: str                      # background:xxx（存储用）
    confidence: float               # 永远很低——这是猜测
    reason: str                     # 依据，写给人看
    evidence: tuple = ()            # 用户的原始行为描述
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "topic": self.topic,
            "scope": self.scope,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "detail": dict(self.detail),
        }


# ---------- 输入兼容：对象与 dict 都吃 ----------
def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def slot_of(*texts) -> str:
    """这段话发生在哪个时段。认不出返回空串（不硬凑）。"""
    blob = " ".join(str(t) for t in texts if t)
    for slot in SLOT_ORDER:
        for w in SLOT_WORDS[slot]:
            if w in blob:
                return slot
    return ""


def topic_of(event) -> str:
    """事件主题：优先 activity，其次描述里的活动词。"""
    act = str(_get(event, "activity", "") or "").strip()
    if act:
        return act
    desc = str(_get(event, "description", "") or "").strip()
    return desc[:8] if desc else ""


def _epoch(event) -> float:
    """取事件发生时刻。

    两种真实形状都要认：`MemoryStore.list_events()` 返回的 `Event`
    （pydantic，只有 datetime 的 created_at），和 dict 形状的
    created_at_epoch。只认后者的话，真实事件会全部落不进窗口——
    规则会"安静地什么都不产出"，看起来像没坏，其实是没在工作。
    """
    v = _get(event, "created_at_epoch")
    if isinstance(v, (int, float)) and v:
        return float(v)
    dt = _get(event, "created_at")
    if isinstance(dt, datetime):
        return dt.timestamp()
    if isinstance(dt, str):
        try:
            return datetime.fromisoformat(dt).timestamp()
        except ValueError:
            return 0.0
    return 0.0


# ---------- 窗口切分 ----------
def split_windows(events, now_epoch: float, *,
                  old_days: int = DEFAULT_OLD_DAYS,
                  new_days: int = DEFAULT_NEW_DAYS):
    """切成"老窗口"和"新窗口"。中间那段不算——正在过渡，不能算趋势。"""
    new_start = now_epoch - new_days * 86400
    old_start = now_epoch - (new_days + old_days) * 86400
    old, new = [], []
    for e in events or []:
        t = _epoch(e)
        if t >= new_start:
            new.append(e)
        elif t >= old_start:
            old.append(e)
    return old, new


def slot_distribution(events) -> dict[str, int]:
    """窗口内各时段的事件数。没有时段的计入 key ''（不参与判断）。"""
    d: dict[str, int] = {}
    for e in events or []:
        s = slot_of(_get(e, "description"), _get(e, "scene"),
                    _get(e, "activity"))
        d[s] = d.get(s, 0) + 1
    return d


def _ratio(d: dict, slot: str) -> float:
    total = sum(d.get(k, 0) for k in SLOT_ORDER)
    return (d.get(slot, 0) / total) if total else 0.0


# ============================================================ Rule 1
def detect_behavior_change(events, now_epoch: float, *,
                           old_days: int = DEFAULT_OLD_DAYS,
                           new_days: int = DEFAULT_NEW_DAYS
                           ) -> list[Candidate]:
    """同一件事的"时段"变了。

    只有当**同一个主题**在两个窗口都够样本、且占比朝相反方向移动
    超过阈值时才算变化。样本不足一律不生成——后台推理最容易犯的错
    就是拿三个事件讲一个趋势。
    """
    old, new = split_windows(events, now_epoch, old_days=old_days,
                             new_days=new_days)
    topics = {t for e in list(old) + list(new)
              for t in [topic_of(e)] if t}

    out: list[Candidate] = []
    for topic in sorted(topics):
        old_t = [e for e in old if topic_of(e) == topic]
        new_t = [e for e in new if topic_of(e) == topic]
        if len(old_t) < MIN_EVENTS_PER_WINDOW or len(new_t) < MIN_EVENTS_PER_WINDOW:
            continue
        od, nd = slot_distribution(old_t), slot_distribution(new_t)
        dropped = [s for s in SLOT_ORDER
                   if od.get(s, 0) >= MIN_SLOT_EVENTS
                   and _ratio(od, s) - _ratio(nd, s) >= MIN_RATIO_SHIFT]
        gained = [s for s in SLOT_ORDER
                  if nd.get(s, 0) >= MIN_SLOT_EVENTS
                  and _ratio(nd, s) - _ratio(od, s) >= MIN_RATIO_SHIFT]
        if not dropped or not gained:
            continue
        frm, to = dropped[0], gained[0]
        conf = _shift_strength(_ratio(od, frm) - _ratio(nd, frm))
        out.append(Candidate(
            type=TYPE_BEHAVIOR_CHANGE,
            topic=f"{topic}的时段",
            scope=f"{BACKGROUND_PREFIX}{TYPE_BEHAVIOR_CHANGE}:{topic}",
            confidence=conf,
            reason=(f"过去{old_days}天里「{topic}」有{od.get(frm, 0)}次发生在{frm}，"
                    f"最近{new_days}天降到{nd.get(frm, 0)}次；"
                    f"同时{frm}→{to}出现了{nd.get(to, 0)}次。"
                    f"这只是推测，不一定对。"),
            evidence=tuple(str(_get(e, "description", ""))[:40]
                            for e in new_t[:3] if _get(e, "description")),
            detail={"rule": "behavior_change", "topic": topic,
                    "from_slot": frm, "to_slot": to,
                    "old_count": od.get(frm, 0), "new_count": nd.get(frm, 0),
                    "to_count": nd.get(to, 0)},
        ))
    return out


def _shift_strength(delta: float) -> float:
    """占比移动越大越自信——但封顶很低，猜测就该像猜测。

    上限 0.45：candidate 永远低于 INIT_CONFIDENCE(0.5)，
    这样即便将来有代码误把它当普通假设处理，也翻不出浪。
    """
    return max(0.20, min(0.45, 0.20 + delta * 0.25))


# ============================================================ Rule 2
def detect_habit_drift(events, hypotheses, now_epoch: float, *,
                       old_days: int = DEFAULT_OLD_DAYS,
                       new_days: int = DEFAULT_NEW_DAYS) -> list[Candidate]:
    """已有的稳定理解，对应的习惯最近在漂移。

    只**读**假设，绝不改它：旧理解继续有效，漂移是另一件事。
    """
    old, new = split_windows(events, now_epoch, old_days=old_days,
                             new_days=new_days)
    out: list[Candidate] = []
    for h in hypotheses or []:
        if _get(h, "status") not in ("tentative", "confirmed"):
            continue
        scope = str(_get(h, "scope", "") or "")
        # 别人的推测不参与漂移推理——推测不能推推测
        if scope.startswith(BACKGROUND_PREFIX) or scope.startswith("quota_overflow:"):
            continue
        if float(_get(h, "confidence", 0.0) or 0.0) < MIN_DRIFT_CONFIDENCE:
            continue
        topic = scope.split(":", 1)[1] if ":" in scope else scope
        if not topic:
            continue
        old_t = [e for e in old if topic_of(e) == topic]
        new_t = [e for e in new if topic_of(e) == topic]
        if len(new_t) < MIN_EVENTS_PER_WINDOW:
            continue
        od, nd = slot_distribution(old_t), slot_distribution(new_t)
        if sum(od.values()) < MIN_SLOT_EVENTS:
            continue
        top_new = max(SLOT_ORDER, key=lambda s: nd.get(s, 0))
        top_old = max(SLOT_ORDER, key=lambda s: od.get(s, 0))
        if not top_new or not top_old or top_new == top_old:
            continue
        if nd.get(top_new, 0) < MIN_SLOT_EVENTS:
            continue
        if _ratio(nd, top_new) - _ratio(od, top_new) < MIN_RATIO_SHIFT:
            continue
        out.append(Candidate(
            type=TYPE_HABIT_DRIFT,
            topic=f"{topic}的偏好",
            scope=f"{BACKGROUND_PREFIX}{TYPE_HABIT_DRIFT}:{topic}",
            confidence=_shift_strength(_ratio(nd, top_new) - _ratio(od, top_new)),
            reason=(f"你之前在这点上已经形成了理解，但最近{new_days}天的记录里，"
                    f"「{topic}」有{nd.get(top_new, 0)}次在{top_new}，"
                    f"而过去主要是{top_old}。可能正在调整——只是推测。"),
            evidence=tuple(str(_get(e, "description", ""))[:40]
                            for e in new_t[:3] if _get(e, "description")),
            detail={"rule": "habit_drift", "topic": topic,
                    "from_slot": top_old, "to_slot": top_new,
                    "hypothesis_scope": scope},
        ))
    return out


# ============================================================ Rule 3
def detect_focus(events, now_epoch: float, *,
                 old_days: int = DEFAULT_OLD_DAYS,
                 new_days: int = DEFAULT_NEW_DAYS) -> list[Candidate]:
    """最近主要精力可能集中在哪。"""
    old, new = split_windows(events, now_epoch, old_days=old_days,
                             new_days=new_days)
    if len(new) < MIN_FOCUS_EVENTS:
        return []
    counts: dict[str, int] = {}
    for e in new:
        t = topic_of(e)
        if t:
            counts[t] = counts.get(t, 0) + 1
    if not counts:
        return []
    top, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if n < MIN_FOCUS_EVENTS or n / len(new) < MIN_FOCUS_RATIO:
        return []
    old_counts: dict[str, int] = {}
    for e in old:
        t = topic_of(e)
        if t:
            old_counts[t] = old_counts.get(t, 0) + 1
    old_top = max(old_counts.items(), key=lambda kv: (kv[1], kv[0]))[0] \
        if old_counts else ""
    tail = f"，而之前主要在{old_top}" if old_top and old_top != top else ""
    return [Candidate(
        type=TYPE_FOCUS_SHIFT,
        topic=top,
        scope=f"{BACKGROUND_PREFIX}{TYPE_FOCUS_SHIFT}:{top}",
        confidence=max(0.20, min(0.45, 0.20 + n / len(new) * 0.2)),
        reason=(f"最近{new_days}天有{n}次记录是「{top}」"
                f"（占全部记录的{n}/{len(new)}）{tail}。只是推测。"),
        evidence=tuple(str(_get(e, "description", ""))[:40]
                        for e in new[:3] if _get(e, "description")),
        detail={"rule": "focus_shift", "topic": top, "count": n,
                "total": len(new)},
    )]


# ============================================================ 总入口
def reason_all(events, hypotheses=None, now_epoch: float | None = None,
               **kw) -> list[Candidate]:
    """跑全部三条规则。纯函数：同样的输入永远给同样的输出。"""
    now = float(now_epoch if now_epoch is not None else time.time())
    out: list[Candidate] = []
    for fn in (detect_behavior_change, detect_focus):
        try:
            out.extend(fn(events, now, **kw))
        except Exception:
            continue                       # 一条规则坏了不许掀翻其余
    try:
        out.extend(detect_habit_drift(events, hypotheses, now, **kw))
    except Exception:
        pass
    # 同一 topic 只留最自信的那条，避免用户面前摆一堆重复猜测
    best: dict[str, Candidate] = {}
    for c in out:
        if c.scope not in best or c.confidence > best[c.scope].confidence:
            best[c.scope] = c
    return sorted(best.values(), key=lambda c: (-c.confidence, c.scope))


def is_background(scope: str) -> bool:
    return str(scope or "").startswith(BACKGROUND_PREFIX)