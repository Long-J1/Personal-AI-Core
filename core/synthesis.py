"""Cross-Context Synthesis：从多个不同经历里找共同结构（Deep Understanding Phase 1）。

## 它要解决的问题

系统已经很会"记住用户表达的内容"，也能发现变化。但它缺一档：

> A：用户经常主动研究新技术。
> B：遇到感兴趣的新东西会投入很多时间。
> C：连续几天没有明显结果后，会开始怀疑方向。

A/B/C 是三个不同场景、各自独立的事实。**系统能不能看出它们共享一个结构**，
而不是把它们当成三条并列的记录？

这是"从记住内容"到"理解关系"的分界。

## 第一原则：模式不是人格

这个模块产出的每一句话都是**推测**，而且必须能这么读：

    "可能存在一个模式……"   ✅
    "你就是……"             ❌ 永久生效、无法反驳、听起来像诊断

所以：
- 候选恒为 `pending`，置信度**封顶 0.45**（低于 INIT_CONFIDENCE 0.50）——
  即便将来有代码误把它当普通假设处理，也翻不出浪。
- 三个模式都**只描述行为，不解释行为**。不给原因、不给性格、不给动机。
- 用户说"不对" → `reject()` → 从此不再提；历史全部保留。

## 三条硬约束（每一条都有测试和变异盯着）

1. **不得仅因共享词汇就合并。** 三个事件如果只是都含"AI"，
   那不是关系，是词频。必须至少有一种**真实关系**：
   共同行为模式 / 反馈条件差异 / 目标 → 行为的时间因果。
2. **证据必须跨来源。** 同一天说四遍"我喜欢研究 AI"是**一个**来源。
   沿用既有的 `_row_day` 独立源判定，不降低门槛。
3. **只提，不用。** 候选不进 prompt、不影响 Judgment、不改人格、
   不参与 Pattern。第八节和第十八节的全部要求。

## 关系类型（比 Background Reasoning 更强的那个"关系"）

Background Reasoning 看的是**同一主题**在不同时段怎么变。
本模块看的是**不同主题之间**共享什么——这是它俩的分工，不是替代：

    behavior_change / habit_drift / focus_shift  → 同一件事变了
    cross_context_synthesis                      → 不同的事共享一个结构

## 与既有模块的关系（第十六、十七节）

    Background Reasoning  发现可能的关系（后台）
              ↓
    Synthesis Candidate   更高一层的候选（待用户裁决）
              ↓
    Natural Evidence      用户认可后才会吃自然证据
              ↓
    Understanding         达到毕业门槛 → 换 scope 前缀 → 正式理解

**不改 Pattern 的晋级门槛**，synthesis candidate 也不直接变 Pattern。
Pattern 仍然走它自己那套成熟机制。

## 纯逻辑

不碰数据库、不联网、不调模型。所有 IO 在 `core/synthesis_bridge.py`。
文本相似只用已有的 `extract_keywords` + 二连字，不引入 embedding。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .context import extract_keywords

# ============================================================ 存储约定
#
# 与 background: / quota_overflow: 完全同构：**存但不生效**。
# 换前缀即毕业（既有 graduate_candidates 就是这么干的），
# 安全过滤那几处只认自己那个前缀，所以不用到处加 synthesis 例外。

SYNTHESIS_PREFIX = "synthesis:"

# 置信度封顶。比 Background Reasoning 的 0.45 再低一点：
# 高层模式的自由度更大，更容易讲得像真的，也就更需要保守。
MAX_CONFIDENCE = 0.45
INIT_CONFIDENCE = 0.40

# ============================================================ 关系与阈值
# 全保守。第一版的唯一目标不是"多发现"，是"别乱发现"。

MIN_MEMBERS = 2              # 至少两个不同场景
MIN_INDEPENDENT_DAYS = 2     # 且来自至少 2 个不同日子（同一天说不算）
MIN_GROUP_KEYWORD_LEN = 2    # 主题词最短长度（与 inducer 的 MIN_KEYWORD_LEN 对齐）
MEMBER_SCAN_LIMIT = 400      # 每次扫描的经历上限（与 background_bridge 对齐）

# 时间因果：目标出现 → 行为变化，至少要这么多条后来的事件
MIN_AFTER_GOAL_EVENTS = 2
# 后继行为必须真的落在目标相关活动上多少比例才算"目标影响行为"
MIN_AFTER_GOAL_RATIO = 0.60

# ============================================================ 分类词典
#
# 这些词**只**用来把事件归类，不进入最终陈述。
# 陈述里出现的是活动名（用户自己说过的话），不是这些词——
# 否则会变成"系统知道你属于 XX 型"，正是第三节禁止的。

# 新东西：探索的触发场景
NOVELTY_MARKERS = (
    "新", "第一次", "开始学", "学起", "研究", "了解", "尝试", "探索",
    "入门", "接触", "试了", "上手", "折腾",
)

# 行为片段标记：用户把一次经历说清楚了（有时间/动作/结果）
BEHAVIOR_MARKERS = (
    "学", "练", "写", "做", "看", "读", "刷", "调", "测", "改", "背",
    "复习", "调试", "部署", "跑", "查", "整理", "规划", "研究",
)

# 反馈：有可见进展 = 有反馈
FEEDBACK_POSITIVE = (
    "有进展", "有效果", "学会了", "搞定了", "解决了", "做出来了",
    "通过了", "跑通了", "有结果", "有提升", "进步", "完成", "成功",
    "拿到", "做好了", "过��", "有突破",
)
# 无反馈：反复但没进展
FEEDBACK_NEGATIVE = (
    "没进展", "没结果", "没头绪", "没进展", "卡住", "卡了", "停滞",
    "看不到", "没反馈", "没动静", "白费", "白学", "没进展", "没进展",
    "还是不懂", "还是不会", "没什么进展",
)

# 高投入：投入深（"一口气""连续""好久"这类是强度的直接证据）
DEEP_MARKERS = (
    "一整天", "一晚上", "一整个", "整个下午", "整个晚上", "熬夜",
    "连续", "好几天", "很久", "一直在", "反复", "投入", "钻进去",
    "沉迷", "专注", "细致", "啃", "死磕", "大量",
)

# 浅尝：投入低（Pattern B 的对照组，也是故事 2 的反转证据）
SHALLOW_MARKERS = (
    "随便", "随便看看", "试了试", "看了一眼", "划水", "浅尝", "摸了一下",
    "放了一下", "没深入", "粗略", "大概看了", "扫了一眼",
)

# 切换：项目/主题之间来回换
SWITCH_MARKERS = (
    "换", "切", "又开始", "转去", "改成", "另一个", "新的坑", "又开了",
)

STOP = frozenset({
    "用户", "今天", "昨天", "最近", "一个", "什么", "怎么", "这个",
    "那个", "现在", "时候", "东西", "事情", "感觉", "觉得", "开始",
})


# ============================================================ 数据形状
@dataclass(frozen=True)
class Member:
    """参与聚合的一条经历。

    用统一的形状，这样三种关系都能吃同一份输入，
    而 IO 那侧（bridge）负责把 events / contexts 统一成它。
    """
    id: str                      # 事件 id 或 context 条目 id
    source_type: str             # "event" | "context"
    activity: str = ""           # 主题名（"AI" / "编程" / "项目"）
    description: str = ""        # 原始描述
    day: str = ""                # 发生日 YYYY-MM-DD（独立源判定用）
    epoch: float = 0.0           # 发生时刻（时间因果用）
    scope: str = ""              # 来源 context 的 scope 线索

    def blob(self) -> str:
        return f"{self.activity} {self.description}"


@dataclass(frozen=True)
class SynthesisCandidate:
    """一条高层推测。frozen：生成后不许在别处被悄悄改写。"""
    pattern: str                 # A / B / C
    topic: str                   # 人类可读主题
    scope: str                   # synthesis:xxx
    statement: str               # 进库的陈述 —— 必须是推测口吻
    reason: str                  # 给用户看的依据
    confidence: float
    members: tuple = ()          # 参与聚合的经历 id
    contexts: tuple = ()         # 参与聚合的 context 标签（可追溯）
    independent_days: int = 0
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "topic": self.topic,
            "scope": self.scope,
            "statement": self.statement,
            "reason": self.reason,
            "confidence": round(self.confidence, 2),
            "members": list(self.members),
            "contexts": list(self.contexts),
            "independent_days": self.independent_days,
            "detail": dict(self.detail),
        }


# ============================================================ 工具
def _norm(text: str) -> str:
    return re.sub(r"[\s，。、？！,.?!；;：:]", "", text or "")


def grams(text: str) -> set[str]:
    """二连字集合。与 inducer 同一套做法，不引入 embedding。"""
    out: set[str] = set()
    for kw in extract_keywords(text or ""):
        if len(kw) == MIN_GROUP_KEYWORD_LEN:
            out.add(kw)
        elif len(kw) > MIN_GROUP_KEYWORD_LEN:
            out.update(kw[i:i + MIN_GROUP_KEYWORD_LEN]
                       for i in range(len(kw) - 1))
    return {g for g in out if g not in STOP}


def has(text: str, marks) -> bool:
    t = _norm(text)
    return any(_norm(m) in t for m in marks)


def independent_days(members) -> int:
    """独立来源天数。同一天说几遍都算 1 天。

    不自己发明规则——直接沿用 `_row_day` 的口径（按**发生日**，
    不按写入日）。同一天的多条只是话多，不是证据多。
    """
    return len({m.day for m in members if m.day})


def _strength(members, *, base: float = INIT_CONFIDENCE) -> float:
    """置信度：随独立天数增长，但封顶 0.45。

    封顶的意义在于：即便将来有代码误把它当普通假设处理
    （INIT_CONFIDENCE 是 0.50），它也永远不会跨过"看起来像正式理解"
    那条线。宁可慢，不可错。
    """
    return max(0.20, min(MAX_CONFIDENCE, base + independent_days(members) * 0.03))


def _clean_label(text: str) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:24].strip(" -—·、,")


# ============================================================ 关系判定
#
# 第六节：不得仅因共享词汇就合并。必须有**真实关系**之一。
# 这里把"关系"做成可判定的量，而不是留给模型"体会"。


def shared_behavior(members) -> bool:
    """关系①：共同行为模式。

    不是"共享一个词"，是**共享一个行为特征**——
    同样的投入深度标志出现在不同活动上。
    这正是 A/B/C 的结构：不同主题，同一个"投入很深"。
    """
    if len(members) < MIN_MEMBERS:
        return False
    deep = [m for m in members if has(m.blob(), DEEP_MARKERS)]
    # 每个成员都得有投入信号，且覆盖至少两个不同主题
    if len(deep) < MIN_MEMBERS:
        return False
    acts = {m.activity for m in deep if m.activity}
    return len(acts) >= 2


def feedback_divergence(members) -> bool:
    """关系②：反馈条件差异。

    有反馈的投入深、没反馈的浅——**条件与行为对不上地成对出现**。
    单看"投入深"不够（那是关系①）；必须同时看到"另一个条件下浅"。
    """
    if len(members) < MIN_MEMBERS:
        return False
    deep = [m for m in members
            if has(m.blob(), DEEP_MARKERS) and has(m.blob(), FEEDBACK_POSITIVE)]
    shallow = [m for m in members
               if has(m.blob(), SHALLOW_MARKERS)
               or (has(m.blob(), FEEDBACK_NEGATIVE) and not has(m.blob(), DEEP_MARKERS))]
    if not deep or not shallow:
        return False
    return len({m.activity for m in deep} | {m.activity for m in shallow}) >= 2


def goal_then_change(members) -> bool:
    """关系③：目标 → 行为变化。

    必须有**时间先后**：目标出现在前，后继行为落在目标相关活动上，
    且占比够高。只看"目标 + 行为同时存在"不算——那只是同一天。

    实现在 `synthesize_goal_impact`（它需要 goals 输入，不在这里）。
    这个函数保留成可单测的关系判据——第十九节第 11 条
    "多个不同模式不能错误合并"就是靠它挡 Pattern C 去吃 Pattern A 的证据。
    """
    return False


# ============================================================ Pattern A
def synthesize_exploration_depth(members) -> list[SynthesisCandidate]:
    """Pattern A：探索 → 高投入。

    不同主题上都出现"面对新东西时投入很深"。

    陈述口吻必须是"可能存在一个模式"，不是"你就是……
    （见模块 docstring 的第一原则）。
    """
    if not shared_behavior(members):
        return []
    deep = [m for m in members if has(m.blob(), DEEP_MARKERS)]
    acts = sorted({m.activity for m in deep if m.activity})
    days = independent_days(deep)
    if days < MIN_INDEPENDENT_DAYS or len(acts) < 2:
        return []
    # 必须是"新东西"场景，不是因为无聊才死磕
    if not any(has(m.blob(), NOVELTY_MARKERS) for m in deep):
        return []
    topic = "接触新事物时的投入"
    return [SynthesisCandidate(
        pattern="A",
        topic=topic,
        scope=f"{SYNTHESIS_PREFIX}exploration_depth:{topic}",
        statement=(f"可能存在一个模式：他接触新东西时（{'、'.join(acts[:3])}）"
                   f"投入都比较深。只是推测，可能不对。"),
        reason=(f"有 {len(deep)} 段经历来自 {days} 个不同日子，"
                f"都出现了一整段/连续/很久这类投入信号，"
                f"而且分布在 {len(acts)} 个不同主题上。"
                f"同一天说几遍不会算成多份证据。"),
        confidence=_strength(deep),
        members=tuple(m.id for m in deep),
        contexts=tuple(sorted({m.activity for m in deep if m.activity})),
        independent_days=days,
        detail={"rule": "cross_context_synthesis",
                "pattern": "A", "topic": topic,
                "relation": "shared_behavior",
                "activities": acts, "member_count": len(deep),
                # 支撑这条模式的经历里**最晚的一条**发生在什么时候。
                # 变化检测（第十一节）拿它当分界：只有它之后的经历才谈得上
                # "变浅了"。用候选的 created_at 是错的——候选是"刚才"建的，
                # 而支撑它的经历都在过去,于是"之后"一条都没有。
                "evidence_until": max((m.epoch for m in deep), default=0.0),
                "independent_days": days},
    )]


# ============================================================ Pattern B
def synthesize_feedback_continuity(members) -> list[SynthesisCandidate]:
    """Pattern B：反馈 → 持续性。

    有反馈时投入深、长时间没反馈时转浅。这是**条件—行为**关系，
    比"投入深"多了一层信息，所以是独立的一条模式。
    """
    if not feedback_divergence(members):
        return []
    deep = [m for m in members
            if has(m.blob(), DEEP_MARKERS) and has(m.blob(), FEEDBACK_POSITIVE)]
    shallow = [m for m in members
               if has(m.blob(), SHALLOW_MARKERS)
               or (has(m.blob(), FEEDBACK_NEGATIVE)
                   and not has(m.blob(), DEEP_MARKERS))]
    days = independent_days(deep + shallow)
    if days < MIN_INDEPENDENT_DAYS:
        return []
    topic = "有没有反馈时的坚持程度"
    return [SynthesisCandidate(
        pattern="B",
        topic=topic,
        scope=f"{SYNTHESIS_PREFIX}feedback_continuity:{topic}",
        statement=(f"可能存在一个模式：他在能看见进展时投入更深，"
                   f"而一段时间看不到反馈时投入会下来。只是推测，可能不对。"),
        reason=(f"{len(deep)} 段经历里同时有投入信号和进展信号；"
                f"另外 {len(shallow)} 段出现「没进展/随便看看/卡住」"
                f"这类信号但没有深投入。共 {days} 个不同日子。"),
        confidence=_strength(deep + shallow),
        members=tuple(m.id for m in deep + shallow),
        contexts=tuple(sorted({m.activity for m in deep + shallow if m.activity})),
        independent_days=days,
        detail={"rule": "cross_context_synthesis",
                "pattern": "B", "topic": topic,
                "relation": "feedback_divergence",
                "deep_count": len(deep), "shallow_count": len(shallow),
                "evidence_until": max((m.epoch for m in deep + shallow),
                                      default=0.0),
                "independent_days": days},
    )]


# ============================================================ Pattern C
def synthesize_goal_impact(members, goals=None) -> list[SynthesisCandidate]:
    """Pattern C：目标变化 → 行为变化。

    唯一一个需要**跨表**的模式：goals 表里的活跃目标，
    加上它出现之后、落在相关活动上的行为。

    严格时间因果：目标时间 ≤ 行为时间，且行为在目标之后。
    同时存在不等于有因果。
    """
    goals = goals or []
    if not goals:
        return []
    out: list[SynthesisCandidate] = []
    for g in goals:
        status = _field(g, "status")
        if status not in ("active", "confirmed"):
            continue
        title = _field(g, "title") or ""
        gkey = grams(title)
        if not gkey:
            continue
        g_epoch = _epoch_of(g)
        after = [m for m in members
                 if m.epoch and g_epoch and m.epoch > g_epoch
                 and (grams(m.activity) & gkey or grams(m.description) & gkey)]
        if len(after) < MIN_AFTER_GOAL_EVENTS:
            continue
        if len(after) / max(1, len(members)) < MIN_AFTER_GOAL_RATIO:
            continue
        topic = _clean_label(title)
        if not topic:
            continue
        days = independent_days(after)
        out.append(SynthesisCandidate(
            pattern="C",
            topic=f"{topic} 目标的影响",
            scope=f"{SYNTHESIS_PREFIX}goal_impact:{topic}",
            statement=(f"可能存在一个模式：他定了「{topic}」之后，"
                       f"后来的记录里明显往这个方向靠。只是推测，可能不对。"),
            reason=(f"这个目标之后有 {len(after)} 段相关经历，"
                    f"占同期记录的 {len(after)}/{len(members)}，"
                    f"分布在 {days} 个不同日子。"),
            confidence=_strength(after, base=INIT_CONFIDENCE + 0.02),
            members=tuple(m.id for m in after),
            contexts=(topic,),
            independent_days=days,
            detail={"rule": "cross_context_synthesis",
                    "pattern": "C", "topic": topic,
                    "relation": "goal_then_change",
                    "goal_scope": _field(g, "scope") or "goal",
                    "after_count": len(after), "total_count": len(members),
                    "evidence_until": max((m.epoch for m in after),
                                          default=0.0),
                    "independent_days": days},
        ))
    return out


# ============================================================ 输入兼容
def _field(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _epoch_of(obj) -> float:
    v = _field(obj, "epoch") or _field(obj, "created_at_epoch")
    if isinstance(v, (int, float)) and v:
        return float(v)
    v = _field(obj, "created_at")
    if isinstance(v, (int, float)) and v:
        return float(v)
    return 0.0


# ============================================================ 第十一节 / 故事 2
# 变化：原模式可能已经不成立了
#
# 故事 2：系统曾形成"用户开始新项目时会深度投入"，
# 后来他连续几个项目都只是浅尝辄止。
#
# 需求是**不假装没变化**，而且**不删历史**。所以这里
# 不去改旧候选，而是产出**一条新的变化候选**，摆到用户面前。
#
# 判定必须和建候选一样严：
# - 只看**已有候选曾经记录过的那批活动**（不能随便找个活动）
# - 同一批活动里，深投入的比例掉到某个程度以下
# - 而且要有足够的样本，否则 2 次浅尝说明不了任何事

DECAY_MIN_SAMPLES = 3      # 相关主题至少要有这么多段经历才谈得上变化
DECAY_RATIO = 0.34         # 深投入占比掉到这个数以下才提出变化


def detect_pattern_decay(candidate_scope: str, members,
                         topic_activities=None, *,
                         since_epoch: float = 0.0,
                         now_epoch: float | None = None
                         ) -> list[SynthesisCandidate]:
    """已有的探索模式，可能正在变浅。

    `candidate_scope` 是**已有候选**的 scope（synthesis:exploration_depth:...）。
    只在 topic_activities 与该候选的主题有交集时才算——不相关的活动
    浅尝不能说明"这个模式变了"，那只是他换了别的事做。

    ## 关键：只看**旧候选形成之后**的经历

    第一版我只统计"相关主题上的全部经历"，于是刚形成模式时那些
    **深投入的老记录**一直算在分子里，ratio 永远降不下来——
    故事 2 明明发生了，检测却安静地什么都不产出。症状是"功能没报错
    但从不触发"，看起来像没实现。

    正确口径是"**后来**变浅了"：分母只算 `since_epoch` 之后的经历。
    老记录是旧模式的**依据**，不是它现在还成不成立的证据。
    """
    topic = str(topic_activities or "")
    acts = {a.strip() for a in re.split(r"[、,，/]", topic) if a.strip()}
    if not acts:
        return []
    rel = [m for m in members
           if m.activity and m.activity in acts
           and (not since_epoch or (m.epoch and m.epoch > since_epoch))]
    if len(rel) < DECAY_MIN_SAMPLES:
        return []
    deep = [m for m in rel if has(m.blob(), DEEP_MARKERS)]
    ratio = len(deep) / len(rel)
    if ratio >= DECAY_RATIO:
        return []
    topic_label = "、".join(sorted(acts)[:3])
    return [SynthesisCandidate(
        pattern="decay",
        topic=f"{topic_label} 的投入变化",
        scope=f"{SYNTHESIS_PREFIX}pattern_decay:{topic_label}",
        statement=(f"可能存在一个变化：之前在「{topic_label}」上看到的那种"
                   f"投入，最近变浅了。这只是推测，可能不对。"),
        reason=(f"相关的 {len(rel)} 段经历里只有 {len(deep)} 段还看得出"
                f"深度投入（{len(deep)}/{len(rel)}）。"
                f"旧模式不会被删掉，只是可能已经不成立了。"),
        confidence=max(0.20, min(MAX_CONFIDENCE,
                                 0.20 + (DECAY_RATIO - ratio) * 0.2)),
        members=tuple(m.id for m in rel),
        contexts=(topic_label,),
        independent_days=independent_days(rel),
        detail={"rule": "cross_context_synthesis",
                "pattern": "decay",
                "relation": "pattern_decay",
                "against_scope": candidate_scope,
                "sample_count": len(rel), "deep_count": len(deep),
                "ratio": round(ratio, 2),
                "independent_days": independent_days(rel)},
    )]


# ============================================================ 总入口
def synthesize(members, goals=None, existing_scopes=None) -> list[SynthesisCandidate]:
    """跑全部三条规则。纯函数：同样的输入永远给同样的输出。

    和 `reason_all` 一样，一条规则坏了不许掀翻其余——
    后台推理出问题绝不能掀翻用户对话。
    """
    members = list(members or [])
    out: list[SynthesisCandidate] = []
    for fn in (synthesize_exploration_depth,
               synthesize_feedback_continuity):
        try:
            out.extend(fn(members))
        except Exception:
            continue
    try:
        out.extend(synthesize_goal_impact(members, goals))
    except Exception:
        pass
    # 变化检测（第十一节 / 故事 2）：对**已有**候选才做。
    # 不看 existing_scopes 就等于每轮都从零重建旧模式——
    # 那会让"变化"永远检测不到，因为旧模式根本没被记录过。
    #
    # existing_scopes 的值是 (活动名, 旧候选形成时刻)：
    # 时刻必须一起传过去，否则统计会把**支撑旧模式的那批深投入记录**
    # 也算进分母，ratio 永远降不下来（第一版就栽在这儿）。
    for scope, ref in dict(existing_scopes or {}).items():
        try:
            acts, since = (ref if isinstance(ref, tuple)
                           else (ref, 0.0))
            out.extend(detect_pattern_decay(scope, members, acts,
                                            since_epoch=since or 0.0))
        except Exception:
            continue
    # 同一 topic 只留一条，避免用户面前摆一堆重复猜测
    best: dict[str, SynthesisCandidate] = {}
    for c in out:
        if c.scope not in best or c.confidence > best[c.scope].confidence:
            best[c.scope] = c
    return sorted(best.values(), key=lambda c: (-c.confidence, c.scope))


def is_synthesis(scope: str) -> bool:
    return str(scope or "").startswith(SYNTHESIS_PREFIX)
