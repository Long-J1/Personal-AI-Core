"""Context Builder + 回忆：把"刚才/今天我在干什么"变成有依据的回答（任务书 §10 第5步）。

流程：解析时间词与关键词 → 检索记忆（时间窗 + 关键词，多级兜底）
      → 组装上下文提示词 → 模型回答 → 附上"依据"（可解释性验收项）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from memory.event import Event
from memory.store import MemoryStore
from models.base import BaseModelAdapter, ModelError

from .config import Settings, settings as default_settings

log = logging.getLogger("core.context")

# ---------- 查询解析 ----------

# 口语停用词：按"子串剔除"处理（中文没空格，整段匹配不现实），长的先删
_STOPWORDS_SUBSTR = [
    "有没有", "是不是", "为什么", "请问", "告诉我", "什么时候",
    "刚才", "刚刚", "今天", "昨天", "前天", "最近", "上次", "之前", "以前",
    "这几天", "这两天", "近几天",
    "现在", "时候", "什么", "哪些", "哪个", "怎么", "如何", "为啥",
    "到底", "究竟", "真的", "好像", "是不是",
    "做了", "一下", "请问",
    "我", "你", "他", "她", "的", "了", "是", "在", "有", "和", "与",
    "吗", "呢", "啊", "呀", "吧", "干", "做", "想", "问",
]

_TIME_WORDS: list[tuple[tuple[str, ...], str]] = [
    (("刚才", "刚刚", "刚"), "刚才（30分钟内）"),
    (("今天", "今日"), "今天"),
    (("昨天", "昨日"), "昨天"),
    (("前天",), "前天"),
    (("最近", "近几天", "这几天", "这两天"), "最近7天"),
    (("上次", "之前", "以前"), "全部记忆"),
]


@dataclass
class QueryPlan:
    question: str
    since_epoch: float | None = None
    until_epoch: float | None = None
    window_label: str = "最近24小时"
    keywords: list[str] = field(default_factory=list)

    @property
    def has_window(self) -> bool:
        return self.since_epoch is not None or self.until_epoch is not None


def extract_keywords(question: str) -> list[str]:
    """中文口语关键词提取：剔除时间词与废话子串 → 剩余 ≥2 字的片段。

    粗糙但有效（决策见 docs/decisions/D003）：检索级联会兜住切分不准的情况。
    """
    text = question
    for sw in sorted(_STOPWORDS_SUBSTR, key=len, reverse=True):
        text = text.replace(sw, " ")
    segments = re.findall(r"[一-鿿]{2,}|[a-zA-Z0-9]{2,}", text)
    seen: set[str] = set()
    kws: list[str] = []
    for seg in segments:
        low = seg.lower()
        if seg in _STOPWORDS_SUBSTR or low in _STOPWORDS_SUBSTR:
            continue
        if low not in seen:
            seen.add(low)
            kws.append(seg)
    return kws[:6]


def _loose_keywords(keywords: list[str]) -> list[str]:
    """把长关键词拆成二连字，用于更宽松的一轮 OR 匹配。"""
    out: list[str] = []
    for kw in keywords:
        if len(kw) > 2 and re.fullmatch(r"[一-鿿]+", kw):
            out.extend(kw[i : i + 2] for i in range(len(kw) - 1))
        else:
            out.append(kw)
    # 去重保序
    seen: set[str] = set()
    result: list[str] = []
    for k in out:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result[:12]


def plan_query(question: str, now: datetime | None = None) -> QueryPlan:
    """识别时间词 → 时间窗；剩余文本 → 关键词。"""
    now = now or datetime.now().astimezone()
    plan = QueryPlan(question=question)
    q = question

    matched_label: str | None = None
    for words, label in _TIME_WORDS:
        if any(w in q for w in words):
            matched_label = label
            break

    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if matched_label == "刚才（30分钟内）":
        plan.since_epoch = (now - timedelta(minutes=30)).timestamp()
        plan.until_epoch = now.timestamp()
    elif matched_label == "今天":
        plan.since_epoch = today0.timestamp()
        plan.until_epoch = now.timestamp()
    elif matched_label == "昨天":
        plan.since_epoch = (today0 - timedelta(days=1)).timestamp()
        plan.until_epoch = today0.timestamp()
    elif matched_label == "前天":
        plan.since_epoch = (today0 - timedelta(days=2)).timestamp()
        plan.until_epoch = (today0 - timedelta(days=1)).timestamp()
    elif matched_label == "最近7天":
        plan.since_epoch = (now - timedelta(days=7)).timestamp()
        plan.until_epoch = now.timestamp()
    elif matched_label == "全部记忆":
        plan.since_epoch = None
        plan.until_epoch = None
    else:
        plan.since_epoch = (now - timedelta(hours=24)).timestamp()
        plan.until_epoch = now.timestamp()

    plan.window_label = matched_label or plan.window_label
    plan.keywords = extract_keywords(q)
    return plan


# ---------- 回答 ----------

RECALL_SYSTEM = (
    "你是 Personal AI 的回忆模块，只能依据给出的“记忆（事件）”回答，禁止编造。"
    "规则：1) 用中文，简洁，最多3句话；2) 如果记忆里没有，直接说“我的记忆里没有找到”，"
    "并说明检索范围；3) 最后必须另起一行写“依据：”，列出引用事件的时间（如 09-26 14:03）。"
)


@dataclass
class SourceRef:
    id: str
    time: str
    description: str
    importance: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "time": self.time,
            "description": self.description,
            "importance": self.importance,
        }


@dataclass
class RecallAnswer:
    answer: str
    sources: list[SourceRef]
    window_label: str
    retrieved_count: int
    keywords: list[str]
    fallback_used: str | None = None   # 兜底说明（如 "时间窗无结果→查全部"）


class Recaller:
    """按时间/关键词检索记忆，并让模型基于记忆回答。"""

    def __init__(
        self,
        store: MemoryStore,
        model: BaseModelAdapter,
        *,
        settings: Settings | None = None,
    ):
        self.store = store
        self.model = model
        self.settings = settings or default_settings

    # -- 检索级联：时间窗优先，关键词逐级放宽，最后才放时间限制 --
    #   1) 窗口 + 关键词 AND → 2) 窗口 + OR → 3) 窗口 + 二连字 OR
    #   → 4) 仅窗口 → 5) 最近全部
    def retrieve(self, plan: QueryPlan) -> tuple[list[Event], str | None]:
        top_k = self.settings.recall_top_k
        win = {"since_epoch": plan.since_epoch, "until_epoch": plan.until_epoch}
        kws = plan.keywords

        if kws:
            hits = self.store.search_events(kws, limit=top_k, mode="and", **win)
            if hits:
                return hits, None
            hits = self.store.search_events(kws, limit=top_k, mode="or", **win)
            if hits:
                return hits, "关键词宽松匹配（OR）"
            hits = self.store.search_events(
                _loose_keywords(kws), limit=top_k, mode="or", **win
            )
            if hits:
                return hits, "关键词拆分匹配"

        hits = self.store.search_events(limit=top_k, **win)
        if hits:
            return hits, "关键词未命中，按时间范围检索" if kws else None

        hits = self.store.search_events(limit=top_k)
        return hits, "时间范围内无结果，改查最近全部记忆" if hits else None

    def recall(self, question: str, now: datetime | None = None) -> RecallAnswer:
        plan = plan_query(question, now=now)
        events, fallback = self.retrieve(plan)

        if not events:
            answer = (
                f"我的记忆里没有找到相关事件（检索范围：{plan.window_label}，"
                f"关键词：{'、'.join(plan.keywords) or '无'}）。"
            )
            log.info("回忆：无结果 question=%s", question[:50])
            return RecallAnswer(
                answer=answer,
                sources=[],
                window_label=plan.window_label,
                retrieved_count=0,
                keywords=plan.keywords,
                fallback_used=fallback,
            )

        # 组装上下文
        memory_block = "\n".join(e.context_line() for e in events)
        user_prompt = (
            f"检索范围：{plan.window_label}\n"
            f"记忆（事件）：\n{memory_block}\n"
            f"问题：{question}"
        )
        try:
            answer = self.model.chat(
                [
                    {"role": "system", "content": RECALL_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                timeout=self.settings.recall_timeout,
            ).strip()
        except ModelError as exc:
            log.error("回忆的模型调用失败：%s", exc)
            # 模型不可用时降级：直接列出检索到的记忆（失败可恢复）
            answer = (
                "模型暂不可用，以下是直接检索到的记忆：\n"
                + "\n".join(f"· {e.time_label} {e.description}" for e in events[:5])
            )

        sources = [
            SourceRef(
                id=e.id,
                time=f"{e.date_label[5:]} {e.time_label}",
                description=e.description,
                importance=e.importance,
            )
            for e in events[:5]
        ]
        log.info(
            "回忆完成：question=%s 命中=%d 窗口=%s",
            question[:40],
            len(events),
            plan.window_label,
        )
        return RecallAnswer(
            answer=answer,
            sources=sources,
            window_label=plan.window_label,
            retrieved_count=len(events),
            keywords=plan.keywords,
            fallback_used=fallback,
        )
