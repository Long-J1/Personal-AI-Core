"""规则式 Hypothesis 归纳器（V0.3 H2）：后台静默跑，无 LLM。

思想："重复出现的同类行为/事件/反馈"达到阈值 → 形成假设。
规则只读已有结构化字段（event.activity、interaction 条目关键词），
不做语义聚类、不调模型——确定、可测、可解释。

触发：conversation.chat 沉淀完成后调用 induce_all()（失败只记日志，
绝不打断对话）；测试/验收可直调。
"""
from __future__ import annotations

import logging
import re
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

# ---------- R2 主题分裂（P0：防高频词污染/巨组合并）----------
# 停用二连字：只做分组和标签用，不碰原文。主题词（提醒/考试/作业/三天/简短…）
# 绝不在此表——一旦主题词被停用，整个主题就隐形了（单测锁死几个代表）。
STOP_GRAMS = frozenset({
    "用户", "希望", "觉得", "认为", "知道", "看到", "听说",
    "想让", "让我", "帮我", "给我", "告诉", "对我", "为我",
    "喜欢", "讨厌", "AI",
    "我", "你", "他", "她", "们",
    "的", "了", "在", "和", "与", "对", "就", "是", "有", "要",
    "会", "能", "可", "都", "也", "还", "再", "更", "很", "太",
    "不", "没", "这", "那", "个", "种", "样", "么", "吗", "呢",
    "啊", "吧", "把", "被", "让", "去", "来", "上", "下", "里",
    "前", "后", "时", "事情", "东西", "问题", "方面", "保持", "一致",
    "进行", "相关", "时候", "一下", "一点", "已经", "经常", "总是",
    "不要", "系统",
})
LABEL_MAX_LEN = 8            # 主题标签最长字数（scope 可读性）
LABEL_MIN_LEN = 2            # 清洗后不足此长度直接丢弃该组（宁可少猜）
# 标签原话约束：候选串每个字必须在 user_direct/user_confirmed 成员内容里
# 出现过；全由模型措辞拼成的串没有资格当标签
LABEL_ORIGIN_GRADES = ("user_direct", "user_confirmed")
GIANT_SPLIT_MIN = 6          # 超过此规模的组尝试二次切分
HIGH_FREQ_RATIO = 0.5        # 出现在超半数条目里的 bigram 退出 label 候选


@dataclass
class InduceResult:
    created: list[str] = field(default_factory=list)  # 新建的假设 id
    linked: list[str] = field(default_factory=list)   # 去重命中、只补证据的假设 id
    skipped: list[str] = field(default_factory=list)  # 主题曾被否决、跳过不再打扰
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
            # P2：陈述不写具体计数（"3次"会随证据增长过期撒谎），计数只活在字段里
            statement = (
                f"用户近期多次进行「{act}」相关活动，"
                f"可能正处在相关事项的持续期。"
            )
            self._create_or_link(
                result, scope, statement,
                [(m.id, m.description) for m in members],
                source_type="event",
            )

    # ---------- R2：相处偏好主题 ----------
    # V0.5.3 注：归纳源是 context 条目，配额溢出内容没进 context，
    # 天然不参与归纳，无需 scope 过滤（晋升侧见 pattern_engine 防御性过滤）。
    def _rule_interaction_theme(self, result: InduceResult) -> None:
        entries = self.ctx.list_entries(kind="interaction")
        result.checked_interactions = len(entries)
        # 中文无空格：整串关键词精确匹配组不上，用二连字做主题键；
        # P0：停用词先剔除（"用户/希望/AI"这种高频词不再有资格连边），
        # 否则真实数据下所有条目连成一个巨组，第二个主题永不诞生。
        keys: dict[str, set[str]] = {}
        for e in entries:
            grams: set[str] = set()
            for kw in extract_keywords(e.content):
                if len(kw) == MIN_KEYWORD_LEN:
                    grams.add(kw)
                elif len(kw) > MIN_KEYWORD_LEN:
                    grams.update(
                        kw[i:i + 2] for i in range(len(kw) - 1))
            keys[e.id] = {g for g in grams if g not in STOP_GRAMS}
        groups = self._connected_groups(entries, keys)
        # P0：巨组二次切分（去掉组内最高频 bigram 后还连通的部分各立门户）
        split: list[list] = []
        for g in groups:
            split.extend(self._split_giant(g, keys))
        for members in split:
            if len(members) < INTERACTION_THEME_MIN:
                continue
            label = self._theme_label(members, keys)
            if not label:
                log.debug("主题标签清洗后为空，跳过该组")
                continue
            scope = f"interaction:{label}"
            statement = (
                f"用户在「{label}」方面多次表达过相处偏好，"
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
    def _split_giant(members: list, keys: dict[str, set[str]]) -> list[list]:
        """巨组二次切分：去掉组内最高频 bigram 后还连通的部分各立门户。

        切不开（仍是一组）就原样返回；全确定性（频次→字典序）。
        """
        if len(members) <= GIANT_SPLIT_MIN:
            return [members]
        freq: dict[str, int] = {}
        for m in members:
            for g in keys[m.id]:
                freq[g] = freq.get(g, 0) + 1
        if not freq:
            return [members]
        top = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        keys2 = {m.id: (keys[m.id] - {top}) for m in members}
        subs = Inducer._connected_groups(members, keys2)
        return subs if len(subs) > 1 else [members]

    @staticmethod
    def _has_content_gram(cand: str) -> bool:
        """候选串里至少含一个非停用二连字（纯胶水串不能当标签）。"""
        return any(
            cand[i:i + 2] not in STOP_GRAMS
            for i in range(len(cand) - 1)
        )

    _STRIP_CHARS = (
        "“”‘’" + "「」『』【】" + "\x22\x27"
    )
    _STRIP_RE = re.compile(
        "[\\[\\]()\uFF08\uFF09<>\u300A\u300B\u3008\u3009"
        "#@\uFF0A*\u00B7\u2026\u2014\\-_+/\\\\=|~`^$%&?!"
        "\uFF1F\uFF01\uFF0C\u3001\uFF1B\uFF1A]")
    _NUM_PREFIX_RE = re.compile(
        "^(?:#\\d+|\\d+[\\.\\)、]|\\(\\d+\\)|[①②③④⑤⑥⑦⑧⑨⑩])")
    _AI_PREFIX_RE = re.compile("^(?:AI|用户希望AI|系统)")

    @staticmethod
    def clean_label(text: str) -> str:
        """标签清洗：去引号/标点/编号/AI 前缀/空洞词，限长。
        返回 "" 表示丢弃（调用方跳过该组）。确定性。"""
        t = text or ""
        # 顺序重要：编号先行（标点剥掉后 "#1 (2)" 就认不出来了）
        t = Inducer._NUM_PREFIX_RE.sub("", t)
        for _q in Inducer._STRIP_CHARS:
            t = t.replace(_q, "")
        t = Inducer._STRIP_RE.sub("", t)
        t = Inducer._AI_PREFIX_RE.sub("", t)
        t = t.strip()
        if len(t) < LABEL_MIN_LEN:
            return ""
        return t[:LABEL_MAX_LEN]

    @staticmethod
    def _theme_label(members: list, keys: dict[str, set[str]]) -> str:
        """组内最长公共子串（含非停用词）；并列字典序首个；截断保可读。

        比"出现最多的二连字"更精确："提前三天"不会输给"提醒"。
        找不到（理论上极少）回退旧规则，保证总有标签。
        """
        contents = {m.id: m.content for m in members}
        texts = [contents[m.id] for m in members]
        # 原话约束：候选串每个字必须出自 user 直接表达过的成员；
        # 无此类成员时回退全员（老数据默认 inferred，不能直接判死刑）。
        origin_chars: set[str] = set()
        for m in members:
            if getattr(m, "source_grade", "inferred") in LABEL_ORIGIN_GRADES:
                origin_chars.update(contents[m.id])
        if not origin_chars:
            origin_chars = set("".join(texts))
        base = texts[0]
        best = ""
        for i in range(len(base)):
            for j in range(i + 2, min(len(base), i + 14) + 1):
                cand = base[i:j]
                if len(cand) < len(best):
                    continue
                if not all(cand in t for t in texts[1:]):
                    continue
                if not Inducer._has_content_gram(cand):
                    continue
                if any(c not in origin_chars for c in cand):
                    continue
                if len(cand) > len(best) or (
                        len(cand) == len(best) and cand < best):
                    best = cand
        if best:
            return Inducer.clean_label(best)
        # 回退：组内最高频 bigram（高频>半数的不配当标签）
        from collections import Counter
        counter: Counter[str] = Counter()
        for m in members:
            for g in keys[m.id]:
                counter[g] += 1
        total = len(members)
        cands = {g: c for g, c in counter.items()
                 if c / total <= HIGH_FREQ_RATIO}
        pool = cands or dict(counter)
        best_n = max(pool.values())
        return Inducer.clean_label(
            sorted(g for g, c in pool.items() if c == best_n)[0])

    def _covering_live_hypo(
        self, members: list[tuple[str, str]], source_type: str
    ) -> str | None:
        """成员被单个 live 假设全部覆盖 → 返回其 id（只补证据不新建）。

        覆盖按 (source_type, source_id) 认，不按文字认。
        """
        want = {(source_type, sid) for sid, _ in members}
        if not want:
            return None
        for hid, (status, have) in self.hyp.support_coverage().items():
            if status in ("tentative", "confirmed") and want <= have:
                return hid
        return None

    def _covering_rejected_hypo(
        self, members: list[tuple[str, str]], source_type: str
    ) -> str | None:
        """成员被单个 rejected 假设全部覆盖 → 返回其 id（尊重否决，不复活）。"""
        want = {(source_type, sid) for sid, _ in members}
        if not want:
            return None
        for hid, (status, have) in self.hyp.support_coverage().items():
            if status == "rejected" and want <= have:
                return hid
        return None

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
        live = [h for h in existing
                if h.status in ("tentative", "confirmed")]
        if live:
            hypo = live[0]
            for source_id, excerpt in members:
                self.hyp.add_evidence(
                    hypo.id, "support", source_type, source_id,
                    excerpt=excerpt)
            result.linked.append(hypo.id)
            log.info("归纳去重 [%s]：补 %d 条证据", scope, len(members))
            return
        if existing:
            # 同 scope 已被用户否决：尊重否决，不新建、不喂料、不打扰
            log.info("归纳跳过 [%s]：该主题曾被用户否决", scope)
            result.skipped.append(existing[0].id)
            return
        # 成员重叠去重：纠正改写条目内容会导致 R2 重打标签（scope 漂移），
        # scope 精确去重此时 miss；同一批素材（按 id 认，不按文字认）
        # 若已被某个假设覆盖，只补证据不新建——防"改述复活"。
        covering = self._covering_live_hypo(members, source_type)
        if covering is not None:
            for source_id, excerpt in members:
                self.hyp.add_evidence(
                    covering, "support", source_type, source_id,
                    excerpt=excerpt)
            result.linked.append(covering)
            log.info("归纳去重 [成员重叠→%s]：补 %d 条证据",
                     covering[:8], len(members))
            return
        rejected = self._covering_rejected_hypo(members, source_type)
        if rejected is not None:
            log.info("归纳跳过 [%s]：素材归属被否决假设，不复活", scope)
            result.skipped.append(rejected)
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
