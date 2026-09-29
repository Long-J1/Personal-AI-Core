"""Pattern 晋升引擎（V0.5 来源 A）：confirmed 假设 → tentative 规律。

规则（常量可测，无 LLM）：
- 同 scope 簇 ≥ PROMOTE_MIN_CONFIRMED 个 confirmed 假设
- 它们合计独立支持源 ≥ PROMOTE_MIN_INDEP
- 同 scope 无 live 规律（去重）、无 rejected 规律（尊重否决）
→ 建 tentative Pattern，confidence = 来源均值，证据逐条链接。

触发：conversation.chat 沉淀+归纳之后（失败只记日志，不打断对话）；
测试/验收可直调。
"""
from __future__ import annotations

import logging

from memory.hypothesis_store import HypothesisStore
from memory.pattern_store import PatternStore
from memory.store import MemoryStore

log = logging.getLogger("core.pattern_engine")

# ---------- 晋升阈值（常量，单测锁定）----------
PROMOTE_MIN_CONFIRMED = 2   # 同簇几个 confirmed 假设才晋升
PROMOTE_MIN_INDEP = 3       # 合计几个独立源才晋升


def _pattern_statement(scope: str) -> str:
    """陈述模板（P2 教训：不写计数，计数只活在字段里）。"""
    if ":" in scope:
        kind, _, label = scope.partition(":")
        label = label.strip() or scope
    else:
        kind, label = "", scope
    if kind == "activity":
        return f"用户长期存在「{label}」方面的稳定规律。"
    if kind == "interaction":
        return f"用户在「{label}」方面有长期稳定的相处模式。"
    return f"用户长期存在「{label}」方面的稳定规律。"


class PatternEngine:
    def __init__(
        self,
        memory_store: MemoryStore,
        hypothesis_store: HypothesisStore,
        pattern_store: PatternStore,
    ):
        self.store = memory_store
        self.hyp = hypothesis_store
        self.pat = pattern_store

    def promote_all(self) -> list[str]:
        """扫 confirmed 假设做晋升。返回新建的 pattern id 列表。"""
        created: list[str] = []
        if self.store.paused:
            log.info("晋升跳过：隐私暂停中")
            return created
        try:
            groups: dict[str, list] = {}
            for h in self.hyp.list_hypotheses(status="confirmed", limit=200):
                groups.setdefault(h.scope, []).append(h)
            for scope in sorted(groups):
                members = groups[scope]
                if len(members) < PROMOTE_MIN_CONFIRMED:
                    continue
                if self.pat.find_by_scope(scope):
                    continue  # 去重：已有非删除规律（尊重否决也在内）
                origins: set[str] = set()
                for h in members:
                    d = self.hyp.get_detail(h.id)
                    if d is None:
                        continue
                    for e in d["evidence"]:
                        if e["polarity"] == "support":
                            origins.add(e["origin"])
                if len(origins) < PROMOTE_MIN_INDEP:
                    log.info("晋升跳过 [%s]：独立源 %d 不足", scope,
                             len(origins))
                    continue
                pat, skip = self.pat.create(
                    _pattern_statement(scope), scope, source="promotion",
                    detail={"rule": "promote",
                            "from": [h.id for h in members]})
                if pat is None:
                    log.debug("晋升创建跳过 [%s]：%s", scope, skip)
                    continue
                for h in members:
                    self.pat.add_evidence(
                        pat.id, "support", "hypothesis", h.id,
                        excerpt=h.statement,
                        detail={"channel": "promotion"})
                mean_conf = sum(h.confidence for h in members) / len(members)
                self.pat.set_confidence(
                    pat.id, mean_conf, reason="promotion-mean")
                created.append(pat.id)
                log.info("晋升新建 [%s] 均值 %.2f", scope, mean_conf)
        except Exception as exc:
            log.warning("晋升失败（跳过）：%s", exc)
        return created
