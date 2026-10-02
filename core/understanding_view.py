"""第一次被理解：把内部理解翻译成用户看得懂的话（V0.5.7）。

这个模块存在的唯一理由：**内部模型不是产品语言**。

内部有 confidence、source_type、hypothesis_id、scope 前缀
（`activity:` / `inferred:` / `quota_overflow:`）——这些是给系统自己看的，
摆给用户既看不懂也不该看。所以这里做一次单向投影：

    内部对象  →  用户能读的一句话 + 他自己的原话 + 什么时候说的

硬性约束（测试会逐条锁死）：
1. 输出里**不允许**出现 confidence / source_type / hypothesis_id / scope 前缀；
2. 每一项都必须能追到**用户自己的话**（没来源的东西不许出现在时间线里）；
3. quota_overflow 是 V0.5.3 设的"不可验证"边界，不许露出给用户。

纯函数：不碰数据库、不联网、无副作用。
"""
from __future__ import annotations

import re
from datetime import datetime

# ---------- 对外词汇（产品语言，不含任何内部术语）----------
STATE_STABLE = "stable"       # 稳定理解
STATE_LEARNING = "learning"   # 还在摸索
STATE_CHANGED = "changed"     # 有过变化
STATE_DROPPED = "dropped"     # 不再使用

STATE_TEXT = {
    STATE_STABLE: "稳定理解",
    STATE_LEARNING: "还在摸索",
    STATE_CHANGED: "有过变化",
    STATE_DROPPED: "不再使用",
}

# natural_bridge 标记失活时写进 status_reason 的固定前缀。
# 只有带这个前缀的才当"用户当时说了什么"来读——别的路径写的
# 是 "user_correction" 这类内部标记，不是人话。
_CHANGE_REASON_PREFIX = "用户表示自己变了："

# 内部 status → 产品状态
_STATUS_MAP = {
    "tentative": STATE_LEARNING,
    "confirmed": STATE_STABLE,
    "rejected": STATE_DROPPED,
    "deleted": STATE_DROPPED,
}

# scope 前缀 → 用户看得懂的主题名
_SCOPE_PREFIX_TEXT = {
    "activity": "最近在做的事",
    "interaction": "相处方式",
    "inferred": "关于你",
    "user": "你提到过的",
    "fact": "你知道的事",
    "profile": "关于你",
    "dynamic": "最近的情况",
}

# 归纳器生成的标准句式 → 平白话（认不出来的走通用兜底）
_PLAIN_RULES = (
    (re.compile(r"^用户近期多次进行「(.+?)」相关活动.*$"), r"你最近经常在做「\1」"),
    (re.compile(r"^用户在「(.+?)」方面多次表达过相处偏好.*$"), r"你在「\1」上希望我保持一致"),
)

# 会暴露内部结构的词，一个都不许出现在输出里
FORBIDDEN_IN_OUTPUT = (
    "confidence", "source_type", "hypothesis_id", "quota_overflow",
    "activity:", "interaction:", "inferred:", "correction",
)


def _now(now: datetime | None = None) -> datetime:
    """接受 datetime / ISO 串 / None——展示层会被测试和回放反复喂不同形态。"""
    if isinstance(now, str):
        try:
            return datetime.fromisoformat(now)
        except ValueError:
            return datetime.now()
    return now or datetime.now()


def _epoch(iso) -> float:
    """ISO 串 → 时间戳，认不出来就返回 0（只影响排序，不影响展示）。"""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def human_date(dt, now: datetime | None = None) -> str:
    """今天/昨天/N天前/M月D日。产品语言，不出现 ISO 串。"""
    n = _now(now)
    try:
        d = datetime.fromisoformat(dt) if isinstance(dt, str) else dt
    except (TypeError, ValueError):
        return ""
    if d is None:
        return ""
    delta = (n.date() - d.date()).days
    if delta <= 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if delta < 7:
        return f"{delta} 天前"
    return f"{d.month} 月 {d.day} 日"


def plain_statement(statement: str) -> str:
    """把内部陈述翻成第二人称的话。

    不用模型——展示层必须确定性，且一次模型抖动就会改写用户看到的历史。
    """
    s = (statement or "").strip()
    if not s:
        return ""
    for pat, rep in _PLAIN_RULES:
        if pat.match(s):
            return pat.sub(rep, s)
    # 通用兜底：系统陈述一律改成"你"的口气
    if s.startswith("用户"):
        s = "你" + s[len("用户"):]
    return s.rstrip("。；;，,")


def topic_name(scope: str) -> str:
    """scope → 用户看得懂的主题名。去掉所有内部前缀。"""
    s = (scope or "").strip()
    if not s:
        return "关于你"
    if s.startswith("quota_overflow:"):
        return ""                       # 安全边界：不露出
    if ":" in s:
        prefix, _, label = s.partition(":")
        label = label.strip()
        # inferred:{kind} —— kind 自己就是类别（profile/fact/dynamic/...）
        if prefix == "inferred":
            return _kind_topic(label)
        head = _SCOPE_PREFIX_TEXT.get(prefix, "")
        if head and label:
            return f"{head} · {label}"
        return label or head or "关于你"
    # 无前缀：可能是 kind（profile/…），不是内部代号就当类别用
    return _kind_topic(s)


def _quotes_from_evidence(evidence, *, limit: int = 4) -> list[dict]:
    """证据 → 用户原话列表。只取真的有原话的那几条。"""
    out = []
    for e in evidence or []:
        text = (e.get("excerpt") or "").strip()
        if not text:
            continue
        out.append({
            "when": _when_of(e),
            "text": text[:120],
            "kind": "support" if e.get("polarity") == "support" else "changed",
        })
        if len(out) >= limit:
            break
    return out


def _change_note(hypo: dict) -> str:
    """最近一次变化的人话说明。

    优先读**反证证据行**——用户原话就在那里（log.detail 里只有内部
    source_id，没有可读内容）。挖不出人话时只说"调整了"，
    绝不回退到原始 detail（那会把 source_type 泄漏给用户，
    这条是被 assert_no_internal_fields 当场抓出来的）。
    """
    quote, when = _latest_contradiction(hypo)
    if quote is None:
        quote, when = _latest_change_log(hypo)
    if quote and when:
        return f"你在{when}说过「{quote}」，我调整了这条理解"
    if when:
        return f"你在{when}的话让我调整了这条理解"
    if quote:
        return f"你说过「{quote}」，我调整了这条理解"
    return ""


def _latest_contradiction(hypo: dict):
    """最新一条反证：用户当时说了什么。"""
    rows = [e for e in (hypo.get("evidence") or [])
            if e.get("polarity") == "contradict"]
    if not rows:
        return None, ""
    last = rows[-1]
    text = (last.get("excerpt") or "").strip()
    when = human_date(last.get("created_at_iso"))
    return (text[:80] or None), when


def _latest_change_log(hypo: dict):
    """没有反证证据时，退到 change / reject 这类带原话的日志。"""
    for last in reversed(hypo.get("history") or []):
        if last.get("action") not in ("change", "reject"):
            continue
        quote = _excerpt_from_detail(last.get("detail"))
        when = human_date(last.get("iso"))
        if quote or when:
            return (quote or None), when
    return None, ""


def _excerpt_from_detail(detail) -> str:
    """从 log.detail 里挖出用户原话；挖不出就返回空串。"""
    if not detail:
        return ""
    if isinstance(detail, dict):
        d = detail
    else:
        try:
            import json
            d = json.loads(detail)
        except (ValueError, TypeError):
            return ""
    if not isinstance(d, dict):
        return ""
    for key in ("excerpt", "reason", "answer"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:80]
    return ""


def build_understanding(hypo: dict, now: datetime | None = None) -> dict:
    """一条假设 → 时间线上的一项。输出里没有任何内部字段。"""
    topic = topic_name(hypo.get("scope", ""))
    if not topic:
        return {}                              # quota_overflow：不露出
    quotes = _quotes_from_evidence(hypo.get("evidence"))
    state = _STATUS_MAP.get(hypo.get("status", ""), STATE_LEARNING)
    note = _change_note(hypo)
    changed = bool(note) or state == STATE_CHANGED
    if changed and state == STATE_LEARNING:
        state = STATE_CHANGED
    return {
        "kind": "understanding",
        "topic": topic,
        "about": plain_statement(hypo.get("statement", "")),
        "state": state,
        "state_text": STATE_TEXT[state],
        "formed_on": _when_of(hypo),
        "changed": changed,
        "change_note": note,
        "quotes": quotes,
        "_ts": _epoch(hypo.get("created_at_iso") or hypo.get("created_at")),
    }


def _when_of(obj) -> str:
    """取"什么时候"。

    ContextEntry.to_public_dict() 给的是 created_at，hypothesis 给的是
    created_at_iso——两个来源字段名不一样，这里统一兜住。
    """
    if not isinstance(obj, dict):
        return ""
    for key in ("created_at_iso", "created_at", "iso"):
        v = obj.get(key)
        if v:
            return human_date(v)
    return ""


def build_note(entry: dict, now: datetime | None = None) -> dict:
    """一条 Context 理解 → 时间线上的一项（用户自己的话）。

    outdated 的条目**不丢弃**，而是进"有过变化"区：用户自己的话变了，
    这正是他最该看见的变化。丢了就等于系统悄悄改写了他的历史。
    """
    content = (entry.get("content") or "").strip()
    if not content:
        return {}
    outdated = entry.get("memory_status") == "outdated"
    when = _when_of(entry) or human_date(None)
    # set_memory_status 记的 status_reason 里带着用户当时说的原话
    # （"用户表示自己变了：…"）。只有这个前缀才认——其他路径写的是
    # "user_correction" 这类内部标记，不是人话。
    reason = (entry.get("status_reason") or "")
    quoted = ""
    if outdated and reason.startswith(_CHANGE_REASON_PREFIX):
        quoted = reason[len(_CHANGE_REASON_PREFIX):].strip()[:80]
    note = (f"你说过「{quoted}」，我调整了这条理解" if quoted
            else ("你后来提到的情况和这条不一样了" if outdated else ""))
    row = {
        "kind": "note",
        "topic": _kind_topic(entry.get("kind", "")),
        "about": content[:120],
        "state": STATE_CHANGED if outdated else STATE_LEARNING,
        "state_text": "有过变化" if outdated else "记住了",
        "formed_on": when,
        "changed": outdated,
        "change_note": note,
        "quotes": [{
            "when": when,
            "text": content[:120],
            "kind": "changed" if outdated else "support",
        }],
        "_ts": _epoch(entry.get("created_at_iso") or entry.get("created_at")),
    }
    return row


def _kind_topic(kind: str) -> str:
    return {
        "interaction": "相处方式",
        "profile": "关于你",
        "dynamic": "最近的情况",
        "fact": "你知道的事",
    }.get(kind, "关于你")


# ---------- 首次体验引导 ----------
# 三个引导方向。不做问卷——只是告诉用户"我在了解什么"，
# 覆盖判定全部从**已有数据**推导，不新增任何状态存储。
ONBOARDING_TOPICS = [
    ("routine", "你的作息", "平时什么时间段状态最好？随便说说就行。"),
    ("interest", "你在意的事", "最近有什么事是你上心的？"),
    ("style", "你希望我怎么配合", "有没有哪种回答方式你更受用？"),
]

_ROUTINE_HINT = ("学习", "作息", "早上", "晚上", "时间", "工作", "睡",
                 "状态", "效率", "习惯", "上班", "上课")
_INTEREST_HINT = ("喜欢", "爱好", "兴趣", "讨厌", "热爱", "着迷",
                  "好玩", "感兴趣", "沉迷", "最爱", "每天玩")
_STYLE_HINT = ("提示", "答案", "直接", "简短", "详细", "解释",
               "先给", "别废话", "风格")


def _mentions(text: str, hints) -> bool:
    return any(h in text for h in hints)


def onboarding_state(chat_count: int, context_entries,
                     understanding_count: int) -> dict:
    """首次体验进度。

    没有新增任何状态表：全部从"聊了多少句"和"已经记住了什么"推导。
    判定宁可宽松——引导是邀请，不是关卡。
    """
    texts = [e.get("content", "") for e in context_entries or []]
    blob = " ".join(t for t in texts if t)
    covered = {
        "routine": _mentions(blob, _ROUTINE_HINT),
        "interest": _mentions(blob, _INTEREST_HINT),
        "style": _mentions(blob, _STYLE_HINT),
    }
    topics = []
    for key, name, ask in ONBOARDING_TOPICS:
        topics.append({
            "key": key, "name": name, "ask": ask, "covered": covered[key],
        })
    got = sum(1 for t in topics if t["covered"])
    first_run = chat_count < 8 and len(texts) < 2 and understanding_count == 0
    return {
        "is_first_run": first_run,
        "done": got >= 2 or (chat_count >= 12 and got >= 1),
        "covered": got,
        "total": len(topics),
        "topics": topics,
    }


def _sort_key(row: dict) -> tuple:
    """按真实时间倒序。

    不能按 "今天"/"3 天前" 这种中文串排序——那是不确定的，
    回放测试会因此飘。
    """
    return row.get("_ts") or 0.0


def build_timeline(hypotheses, context_entries, now: datetime | None = None
                   ) -> dict:
    """完整时间线。hypotheses 需带 evidence/history（get_detail 的形状）。"""
    items = []
    for h in hypotheses or []:
        row = build_understanding(h, now)
        if row:
            items.append(row)
    for e in context_entries or []:
        # outdated/rejected 不当"现状"展示，但仍进"有过变化"区——
        # 用户自己的话变了，他必须看得见，这是信任的底线。
        row = build_note(e, now)
        if row:
            items.append(row)
    items.sort(key=_sort_key, reverse=True)
    for row in items:
        row.pop("_ts", None)               # 排序用的内部键不外泄
    changed = [r for r in items if r["changed"]]
    current = [r for r in items if not r["changed"]]
    return {
        "items": current,
        "changed": changed,
        "count": len(items),
    }


def assert_no_internal_fields(payload) -> list[str]:
    """自检：返回泄漏的内部字段名列表（测试与运行时都可用）。"""
    blob = repr(payload)
    return [w for w in FORBIDDEN_IN_OUTPUT if w in blob]