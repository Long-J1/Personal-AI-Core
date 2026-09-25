"""理解 → 事件：把模型的自然语言/JSON 输出解析成标准 Event（任务书 §10 第3条）。

健壮性要求：模型输出必须容忍——带 markdown 围栏、带多余解释、缺字段、类型不对、
完全失败，都要有明确路径，绝不能让管线崩掉（"失败可恢复"）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from memory.event import Event

log = logging.getLogger("core.understanding")

UNDERSTAND_PROMPT = (
    "你是 Personal AI 的视觉理解引擎。仔细观察图片，只输出一个严格 JSON 对象"
    "（不要 markdown 代码块、不要任何解释文字），格式：\n"
    '{"scene": "场景地点", "activity": "用户可能正在做什么", '
    '"objects": ["出现的物品或人物"], '
    '"description": "用中文一句话客观概括这张图里正在发生什么", '
    '"importance": 0.0到1.0, '
    '"facts": ["最多2条长期稳定的事实，没有就空数组"]}\n'
    "importance 标准：日常琐事 0.2-0.4；重要、异常或信息量大 0.6-1.0。"
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)

# 容错字段别名（模型偶尔发挥）
_ALIASES = {
    "scene": ("scene", "location", "place", "场景", "地点"),
    "activity": ("activity", "action", "doing", "活动", "行为"),
    "description": ("description", "desc", "summary", "概述", "描述"),
    "importance": ("importance", "score", "priority", "重要性"),
    "objects": ("objects", "items", "things", "物品"),
    "facts": ("facts", "long_term_facts", "事实"),
}


@dataclass
class ParseResult:
    event: Event
    ok: bool
    error: str | None = None


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象；抠不出返回 None。"""
    if not text or not text.strip():
        return None
    t = text.strip()

    # 1) 整体就是 JSON
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 2) ```json 围栏
    m = _FENCE_RE.search(t)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # 3) 夹在解释文字里：取第一 { 到最后 }
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(t[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _pick(data: dict, key: str) -> Any:
    for alias in _ALIASES.get(key, (key,)):
        if alias in data and data[alias] is not None:
            return data[alias]
    return None


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _as_importance(value: Any) -> float:
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, score))


def parse_understanding(
    raw: str,
    *,
    source: str,
    now: datetime | None = None,
) -> ParseResult:
    """模型原始输出 → Event。解析失败也会生成兜底事件（宁可记下原文，不丢记忆）。"""
    created_at = now or datetime.now().astimezone()
    data = extract_json(raw or "")

    if data is None:
        fallback_text = (raw or "").strip()[:500] or "（模型无输出）"
        event = Event(
            created_at=created_at,
            source=source,
            scene="（未结构化）",
            description=fallback_text,
            importance=0.2,
            raw_understanding=raw,
        )
        log.warning("理解输出无法解析为 JSON，已按原文记忆")
        return ParseResult(event=event, ok=False, error="模型输出不是有效 JSON")

    description = _as_str(_pick(data, "description"))
    activity = _as_str(_pick(data, "activity"))
    scene = _as_str(_pick(data, "scene"))
    if not description:
        description = activity or scene or "（模型未给出描述）"

    event = Event(
        created_at=created_at,
        source=source,
        scene=scene,
        activity=activity,
        objects=_as_str_list(_pick(data, "objects")),
        description=description,
        importance=_as_importance(_pick(data, "importance")),
        facts=_as_str_list(_pick(data, "facts")),
        raw_understanding=raw,
    )
    missing = [
        name
        for name, val in (("scene", scene), ("activity", activity))
        if not val
    ]
    if missing:
        log.debug("理解结果缺字段（已用默认值）：%s", "、".join(missing))
    return ParseResult(event=event, ok=True, error=None)
