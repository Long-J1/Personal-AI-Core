"""正式 Understanding 的稳定性生命周期（Understanding Stability）。

要解决的问题
------------
正式理解（`status='tentative'/'confirmed'`）一旦成立就基本只涨不跌：
证据一直加、矛盾只减分，但**没有任何一条路径会因为"现实反复相反"而
把它降级**。于是"我晚上学习"这种理解会永远留在 prompt 里，
哪怕用户已经连续一个月每天下午在学习。

同时反过来也要防另一头：**不能因为一次偶然反例就自我推翻**。
用户随口一句"今天下午学习"不构成对"我通常晚上学习"的否定。
—— 一次例外不是规律的变化。

三条铁律
--------
1. **独立日期才算数。** 同一天里重复十次也是同一个来源
   （复用 `_row_day` / `origin` 那一套，不另立算法）。
2. **只有真实用户表达能反向。** AI 自己说的话、自己推出来的理解、
   后台候选、综合候选 —— 全部不能作为反向证据。
   `SOURCE_TYPES` 白名单已把 `hypothesis` 挡在外面，这里再加一层：
   `background:` / `synthesis:` 开头的 scope 连"被引用"都不算。
3. **under_review 不等于失效。** 它只是"我不太确定了，先别当权威说"。
   后续继续得到支持就恢复 active；长期反向才真的失活。

阈值（都刻意保守，宁可晚一点翻脸也不要翻错脸）
----------------------------------------------
* `REVIEW_MIN_INDEPENDENT_DAYS = 2`  跨 2 个独立日期的反证才进 review。
  单日重复反证 → 仍然 active（规则明说）。
* `RESTORE_MIN_SUPPORT = 1`           review 期间再有 1 条支持就回 active。
  为什么这么松？因为**进 review 的依据是反证**，恢复的依据该是
  "又看到反过来的事实"，不是"攒够置信度"。攒够置信度那条路
  （`graduate_candidates`）本来就有，不该在这里复制。
* `INACTIVE_MIN_INDEPENDENT_DAYS = 3` review 之后**再跨 3 天**反向才失活。
  review 本身就是"暂缓"，立刻判死等于把一次误判变成永久损失。

explicit correction（用户明确纠正）走**另一条路**，不经过这里的计数：
它直接进 review（而不是 active），并且立刻失活。理由：用户亲口说
"我改成早上了"，这不是"一条反证"，这是**用户已经告诉你答案了**。
还留在 active 等系统慢慢发现，是在浪费用户已经给出的信息。

零 schema 变更
--------------
`status` 是 TEXT 列，新增 `under_review` / `inactive` 取值**不需要 migration**，
和 V0.5.9 加 `pending`/`accepted`/`expired` 时一样。
`under_review` 会在本模块的 `PROMPT_STATUSES` 里被排除，
所以 prompt / 「我眼中的你」**一处都不用改**就自动守住了信任边界。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("core.understanding_stability")

# ---------- 状态（与 hypothesis_store 的既有取值并列）----------

UNDER_REVIEW = "under_review"
INACTIVE = "inactive"

# 单一出处：状态值本身定义在 store 里（和 STATUS_LABELS 放在一起），
# 这里只做别名，避免两处各写一份字符串然后走偏。
from memory.hypothesis_store import (  # noqa: E402
    LIVE_STATUSES as _LIVE,
    MANAGED_STATUSES as _MANAGED,
    UNDER_REVIEW as _UR,
    INACTIVE as _IN,
)

assert (UNDER_REVIEW, INACTIVE) == (_UR, _IN), "状态值与 store 不一致"

# 只有这两个状态能无条件进入普通理解上下文。
# 刻意**不包含** UNDER_REVIEW —— 它进 prompt 的前提是
# 「用户明确说过要按它来」（见 should_enter_prompt）。
PROMPT_STATUSES = tuple(_LIVE)

# 能被本模块改写的既有状态（不动 rejected/deleted —— 那是用户终局决定）
MANAGED_STATUSES = tuple(_MANAGED)

# ---------- 阈值 ----------

# 跨这么多个**独立日期**的反证才把 active 降成 under_review。
# 1 天 = 不够：单日重复不算独立（_row_day 已经保证）。
REVIEW_MIN_INDEPENDENT_DAYS = 2

# under_review 期间，拿到这么多次支持就恢复 active。
RESTORE_MIN_SUPPORT = 1

# under_review 之后还要再跨这么多天反向，才真的失活。
# 绝不等于 0 —— 0 会让"刚进 review 就立刻死"，一次误判就永久损失。
INACTIVE_MIN_INDEPENDENT_DAYS = 3

# ---------- 不能反向的 scope 前缀 ----------
# 这些是 **AI 自己产出的候选**（后台推理 / 跨经历综合 / 配额溢出）。
# 它们**永远不能**让一条正式理解降级 —— 否则 AI 可以把自己
# 曾经说的话当成"现实"，从而自己推翻自己。
#
# 刻意**不含** `inferred:`：那是 background/synthesis **毕业之后**的
# 正式理解前缀（换前缀即毕业，见 D025），已经进了 prompt，
# 当然要受同样的稳定性规则管。把挡子写在这里会让它永远免疫复审，
# 那就是另一种形式的自我豁免。
SELF_GENERATED_PREFIXES = ("background:", "synthesis:", "quota_overflow:")


@dataclass(frozen=True)
class Decision:
    """一次稳定性判定的结果。

    `changed` 是 False 时 `from_status`/`to_status` 相同，
    调用方不需要做任何事。
    """

    changed: bool
    from_status: str
    to_status: str
    reason: str = ""
    independent_reverse_days: int = 0
    support_since_review: int = 0
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 纯逻辑

def is_self_generated(scope: str) -> bool:
    """这条理解是不是 AI 自己生成的（后台候选 / 综合候选 / 溢出转假设）。"""
    s = (scope or "").strip()
    return any(s.startswith(p) for p in SELF_GENERATED_PREFIXES)


def can_reverse_understanding(status: str, scope: str,
                              source_type: str) -> bool:
    """这条证据有没有资格让一条**正式理解**降级。

    三道闸门，任何一道不过就是 False：

    1. 状态可管理 —— rejected/deleted 是用户的终局决定，不碰。
    2. scope 不是 AI 自己生成的 —— 防 self-proof。
    3. 来源在白名单里 —— `hypothesis` 早已被 SOURCE_TYPES 挡掉，
       这里再挡一次是因为本模块可能不经 add_evidence 被调用。
    """
    from memory.hypothesis_store import SOURCE_TYPES

    if status not in MANAGED_STATUSES:
        return False
    if is_self_generated(scope):
        return False
    if source_type not in SOURCE_TYPES:
        return False
    return True


def should_enter_prompt(status: str, *, user_reaffirmed: bool = False) -> bool:
    """这条理解能不能进普通 prompt。

    - active（tentative/confirmed）→ 能
    - under_review → **默认不能**。它还"没死"，但已经不该被当成
      权威结论说出口。只有用户明确重申过（`user_reaffirmed`）才放行。
    - inactive / rejected / deleted → 永远不能
    """
    if status in PROMPT_STATUSES:
        return True
    if status == UNDER_REVIEW and user_reaffirmed:
        return True
    return False


def is_visible_as_understanding(status: str) -> bool:
    """「我眼中的你」这类正式理解视图要不要显示它。

    under_review **要显示**，只是标成"待复审" —— 藏起来等于
    让用户以为自己从没被理解过，而那条历史还在库里可查。
    inactive 不显示（但历史永远保留，见 status_detail）。
    """
    return status in PROMPT_STATUSES or status == UNDER_REVIEW


def decide_reverse(*, status: str, independent_reverse_days: int,
                   has_explicit_correction: bool) -> Decision:
    """已经出现反向证据 → 该怎么走。**纯函数，无 IO。**

    显式纠正优先于所有计数：用户已经亲口给了答案，
    不需要再等系统慢慢数够天数。
    """
    if has_explicit_correction:
        if status == INACTIVE:
            return Decision(False, INACTIVE, INACTIVE, "已经失活了")
        # 明确纠正 → 直接失活，不经过 review 过渡。
        # 中间态在这里没有价值：用户的话就是结论。
        return Decision(True, status, INACTIVE,
                        "用户明确说了已经不一样了",
                        independent_reverse_days)

    if status == INACTIVE:
        return Decision(False, INACTIVE, INACTIVE, "已失活，等新的支持重新建立")

    if independent_reverse_days < REVIEW_MIN_INDEPENDENT_DAYS:
        return Decision(False, status, status, "反证还不够独立（日数不足）",
                        independent_reverse_days)

    if status == UNDER_REVIEW:
        if independent_reverse_days >= (
                REVIEW_MIN_INDEPENDENT_DAYS + INACTIVE_MIN_INDEPENDENT_DAYS):
            return Decision(True, UNDER_REVIEW, INACTIVE,
                            "复审期间反向证据继续累积", independent_reverse_days)
        return Decision(False, UNDER_REVIEW, UNDER_REVIEW,
                        "已在复审中，反证还没到失活线", independent_reverse_days)

    # tentative / confirmed：跨独立日期的反证才降级
    return Decision(True, status, UNDER_REVIEW,
                    "多个独立日期都出现了相反的情况", independent_reverse_days)


def decide_support(*, status: str, support_since_review: int) -> Decision:
    """又出现了支持原理解的证据 → 该怎么走。**纯函数，无 IO。**

    under_review + 至少一次支持 → 回到 active。
    这条路径**不检查置信度**：进 review 的依据是反证，
    恢复的依据该是"又看到反过来的事实"，而不是"分数攒够了"。
    分数那条路（毕业）本来就有，不在这里复制。
    """
    if status != UNDER_REVIEW:
        return Decision(False, status, status, "不在复审中")
    if support_since_review < RESTORE_MIN_SUPPORT:
        return Decision(False, UNDER_REVIEW, UNDER_REVIEW, "支持还不够")
    return Decision(True, UNDER_REVIEW, "tentative",
                    "复审期间又出现了支持原理解的情况",
                    support_since_review=support_since_review)


def status_detail(decided_at_epoch: float | None) -> str:
    """给人看的状态说明（**不泄漏任何内部 id / 分数**）。"""
    if not decided_at_epoch:
        return ""
    from datetime import datetime
    when = datetime.fromtimestamp(decided_at_epoch).astimezone()
    return f"{when:%Y-%m-%d} 复审"


# ---------------------------------------------------------------- 编排

class StabilityBridge:
    """把纯逻辑接到 store 上。IO 只在这一层。

    `store` 是一个 `HypothesisStore`。所有判定依据都来自**已经落库的
    证据行**，而不是从传入的计数里拿 —— 计数只作为缓存，
    这样"同日重复不能累计成独立证据"这条规则只有一个出处：
    store 里 `origin` 的去重。
    """

    def __init__(self, store):
        self.store = store

    # ---------- 取事实 ----------
    def _reverse_days(self, hypo_id: str, since_epoch: float | None) -> int:
        """**独立日期数**，不是条数。

        按 `origin` 去重 —— `_default_origin` 对 context 产生
        `ctxday:<日期>`，同一天的多条共用一个 origin，
        所以 COUNT(DISTINCT origin) 天生就是"天数"。
        """
        from datetime import datetime
        with self.store._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT origin FROM hypothesis_evidence"
                " WHERE hypothesis_id=? AND polarity='contradict'"
                + (" AND created_at_epoch >= ?" if since_epoch else ""),
                ((hypo_id, since_epoch) if since_epoch else (hypo_id,)),
            ).fetchall()
        return len(rows)

    def _support_since(self, hypo_id: str, since_epoch: float | None) -> int:
        with self.store._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM hypothesis_evidence"
                " WHERE hypothesis_id=? AND polarity='support'"
                + (" AND created_at_epoch >= ?" if since_epoch else ""),
                ((hypo_id, since_epoch) if since_epoch else (hypo_id,)),
            ).fetchone()
        return int(row[0])

    # ---------- 动作 ----------
    def _apply(self, hypo_id: str, dec: Decision) -> None:
        """写 status + 一条 log。**不改 confidence**。

        为什么不动分数：置信度表达"我有多确定这条是真的"，
        稳定性表达"这条现在该不该被当成结论用"。两件事。
        让 review 也去压分数，会让"复审中"这条理解在重新得到支持后
        因为分数已经掉下去而永远升不回来。
        """
        import json
        import time
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self.store._lock:
            with self.store._connect() as con:
                with con:
                    con.execute(
                        "UPDATE hypotheses SET status=?, status_detail=?,"
                        " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                        (dec.to_status, status_detail(time.time()),
                         now_iso, time.time(), hypo_id),
                    )
                    con.execute(
                        "INSERT INTO hypothesis_log (id, epoch, iso, action,"
                        " hypothesis_id, detail)"
                        " VALUES (?,?,?,?,?,?)",
                        (__import__("uuid").uuid4().hex, time.time(), now_iso,
                         f"stability:{dec.to_status}", hypo_id,
                         json.dumps({**dec.detail, "reason": dec.reason,
                                     "reverse_days": dec.independent_reverse_days},
                                    ensure_ascii=False)),
                    )

    # ---------- 对外 ----------
    def on_reverse_evidence(self, hypo_id: str, *, source_type: str,
                            explicit: bool = False) -> Decision:
        """绑定了一条**反向**证据之后调用。

        explicit=True 表示这是用户明确纠正（"我改成早上了"），
        那种情况直接失活，不等天数。
        """
        hypo = self.store.get(hypo_id)
        if hypo is None:
            return Decision(False, "", "", "假设不存在")
        if not can_reverse_understanding(hypo.status, hypo.scope, source_type):
            # 被闸门挡下：AI 自己产的、或用户已否决/删除的 —— 一律不动。
            return Decision(False, hypo.status, hypo.status,
                            "这条证据没有资格改变正式理解")
        since = hypo.status_changed_epoch if hypo.status == UNDER_REVIEW else None
        days = self._reverse_days(hypo_id, since)
        dec = decide_reverse(status=hypo.status,
                             independent_reverse_days=days,
                             has_explicit_correction=explicit)
        if dec.changed:
            self._apply(hypo_id, dec)
        return dec

    def on_support_evidence(self, hypo_id: str) -> Decision:
        """绑定了一条**支持**证据之后调用。"""
        hypo = self.store.get(hypo_id)
        if hypo is None:
            return Decision(False, "", "", "假设不存在")
        since = hypo.status_changed_epoch if hypo.status == UNDER_REVIEW else None
        n = self._support_since(hypo_id, since)
        dec = decide_support(status=hypo.status, support_since_review=n)
        if dec.changed:
            self._apply(hypo_id, dec)
        return dec

    def evaluate(self, hypo_id: str, *, explicit: bool = False) -> Decision:
        """不绑定任何新证据，纯复核一次当前状态应该是什么。"""
        return self.on_reverse_evidence(hypo_id, source_type="context",
                                        explicit=explicit)

    # ---------- 批量复审 ----------
    # 刻意**不做定时器**（见 D025 的同一条理由：常驻后台循环是产品级
    # 决定）。只在用户说了一句话、沉淀完成之后跑一次 —— 既然用户刚
    # 开口了，那就是"现实可能有新证据"的最好时机。
    #
    # 上限不是性能妥协，是**语义上限**：只有"这一轮真的动过的"
    # 理解才可能因这一轮而改变。翻全库去复审几百条历史理解，
    # 只会让一次平常对话改变很久以前的判断。
    def review_all(self, *, limit: int = 40) -> int:
        """复审本轮可能需要重新判定的理解。返回状态变化条数。

        `limit` 默认 40，且按 updated_at 倒序 —— 只看最近动过的。
        """
        from memory.hypothesis_store import MANAGED_STATUSES

        with self.store._connect() as con:
            rows = con.execute(
                "SELECT id FROM hypotheses"
                " WHERE status IN ({}) AND status != 'deleted'"
                " ORDER BY updated_at_epoch DESC LIMIT ?".format(
                    ",".join("?" * len(MANAGED_STATUSES))),
                (*MANAGED_STATUSES, int(limit)),
            ).fetchall()
        changed = 0
        for r in rows:
            hypo_id = r[0]
            # 先看支持（under_review 可能恢复），再看反向（可能降级）。
            # 顺序有意义：一条 review 里的理解如果这一轮同时有支持和
            # 反向，先恢复 active 再判反向，得到的是保守结果。
            if self.on_support_evidence(hypo_id).changed:
                changed += 1
                continue
            if self.on_reverse_evidence(hypo_id, source_type="context").changed:
                changed += 1
        return changed