"""规则式 Hypothesis 归纳器（V0.3 H2）：后台静默跑，无 LLM。

思想："重复出现的同类行为/事件/反馈"达到阈值 → 形成假设。
规则只读已有结构化字段（event.activity、interaction 条目关键词），
不做语义聚类、不调模型——确定、可测、可解释。

触发：conversation.chat 沉淀完成后调用 induce_all()（失败只记日志，
绝不打断对话）；测试/验收可直调。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.store import MemoryStore

from .context import extract_keywords

log = logging.getLogger("core.inducer")

# ---------- 规则阈值（常量，单测锁定）----------
ACTIVITY_REPEAT_MIN = 3      # R1：同一活动出现几次才归纳
INTERACTION_THEME_MIN = 2    # R2：共享关键词的相处偏好条目几条才归纳
EVENT_SCAN_LIMIT = 200       # 每次扫描的事件上限
MIN_KEYWORD_LEN = 2          # 归纳关键词最小长度


@dataclass
class InduceResult:
    created: list[str] = field(default_factory=list)  # 新建的假设 id
    linked: list[str] = field(default_factory=list)   # 去重命中、只补证据的假设 id
    checked_events: int = 0
    checked_interactions: int = 0

    @property
    def touched(self) -> bool:
        return bool(self.created or self.linked)


class Inducer:
    def __init__(
        self,
        memory_store: MemoryStore,
        context_store: ContextStore,
        hypothesis_store: HypothesisStore,
    ):
        self.store = memory_store
        self.ctx = context_store
        self.hyp = hypothesis_store

    # ---------- 入口 ----------
    def induce_all(self) -> InduceResult:
        """跑全部规则。隐私暂停时跳过（暂停 = 停止学习）。"""
        result = InduceResult()
        if self.store.paused:
            log.info("归纳跳过：隐私暂停中")
            return result
        try:
            self._rule_activity_repeat(result)
        except Exception as exc:  # 单规则失败不影响其他规则
            log.warning("R1 归纳失败（跳过）：%s", exc)
        try:
            self._rule_interaction_theme(result)
        except Exception as exc:
            log.warning("R2 归纳失败（跳过）：%s", exc)
        return result

    # ---------- R1：活动重复 ----------
    def _rule_activity_repeat(self, result: InduceResult) -> None:
        events = self.store.list_events(limit=EVENT_SCAN_LIMIT)
        result.checked_events = len(events)
        groups: dict[str, list] = {}
        for e in events:
            act = (e.activity or "").strip()
            if not act:
                continue
            groups.setdefault(act, []).append(e)
        for act in sorted(groups):
            members = groups[act]
            if len(members) < ACTIVITY_REPEAT_MIN:
                continue
            scope = f"activity:{act}"
            statement = (
                f"用户近期多次进行「{act}」相关活动（{len(members)}次），"
                f"可能正处在相关事项的持续期。"
            )
            self._create_or_link(
                result, scope, statement,
                [(m.id, m.description) for m in members],
                source_type="event",
            )

    # ---------- R2：相处偏好主题 ----------
    def _rule_interaction_theme(self, result: InduceResult) -> None:
        entries = self.ctx.list_entries(kind="interaction")
        result.checked_interactions = len(entries)
        # 中文无空格：整串关键词精确匹配组不上，用二连字做主题键；
        # 共享任一二连字的条目连通成组（小规模 O(n²)，确定性）。
        keys: dict[str, set[str]] = {}
        for e in entries:
            grams: set[str] = set()
            for kw in extract_keywords(e.content):
                if len(kw) == MIN_KEYWORD_LEN:
                    grams.add(kw)
                elif len(kw) > MIN_KEYWORD_LEN:
                    grams.update(
                        kw[i:i + 2] for i in range(len(kw) - 1))
            keys[e.id] = grams
        groups = self._connected_groups(entries, keys)
        for members in groups:
            if len(members) < INTERACTION_THEME_MIN:
                continue
            label = self._theme_label(members, keys)
            scope = f"interaction:{label}"
            statement = (
                f"用户在「{label}」方面多次表达过相处偏好（{len(members)}条），"
                f"可能希望 AI 在此类场景保持一致。"
            )
            self._create_or_link(
                result, scope, statement,
                [(m.id, m.content) for m in members],
                source_type="context",
            )

    @staticmethod
    def _connected_groups(entries: list, keys: dict[str, set[str]]) -> list[list]:
        parent = {e.id: e.id for e in entries}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        ids = [e.id for e in entries]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if keys[ids[i]] & keys[ids[j]]:
                    union(ids[i], ids[j])
        buckets: dict[str, list] = {}
        for e in entries:
            buckets.setdefault(find(e.id), []).append(e)
        # 确定性：组内按 id 排，组间按首 id 排
        grouped = [sorted(g, key=lambda m: m.id) for g in buckets.values()]
        return sorted(grouped, key=lambda g: g[0].id)

    @staticmethod
    def _theme_label(members: list, keys: dict[str, set[str]]) -> str:
        """组内最多条目共享的二连字；并列取字典序首个（确定性）。"""
        from collections import Counter
        counter: Counter[str] = Counter()
        for m in members:
            for g in keys[m.id]:
                counter[g] += 1
        best = max(counter.values())
        return sorted(g for g, c in counter.items() if c == best)[0]

    # ---------- 去重：同 scope 已有 → 只补证据 ----------
    def _create_or_link(
        self,
        result: InduceResult,
        scope: str,
        statement: str,
        members: list[tuple[str, str]],
        *,
        source_type: str,
    ) -> None:
        existing = self.hyp.find_by_scope(scope)
        if existing:
            hypo = existing[0]
            for source_id, excerpt in members:
                self.hyp.add_evidence(
                    hypo.id, "support", source_type, source_id,
                    excerpt=excerpt)
            result.linked.append(hypo.id)
            log.info("归纳去重 [%s]：补 %d 条证据", scope, len(members))
            return
        hypo, skip = self.hyp.create(statement, scope,
                                     detail={"rule": scope.split(":")[0]})
        if hypo is None:
            log.debug("归纳创建跳过 [%s]：%s", scope, skip)
            return
        for source_id, excerpt in members:
            self.hyp.add_evidence(
                hypo.id, "support", source_type, source_id, excerpt=excerpt)
        result.created.append(hypo.id)
        log.info("归纳新建 [%s] %s", scope, statement[:50])
