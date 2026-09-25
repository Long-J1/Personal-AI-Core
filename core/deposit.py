"""沉淀模块：对话结束后，把"这次交流学到了什么"写进 Personal Context（V0.2 A 组）。

职责（docs/decisions/D007）：
- 模型从对话里提取 新理解 / 纠正 / 值得记的事件，输出严格 JSON；
- 我们负责校验、去重、限量，然后应用到 ContextStore（纠正会写变更日志）；
- 任何一步失败都不影响用户已经拿到的回答——沉淀失败只记日志。

这里是"Context 改变后续行为"的写入侧：本轮的纠正 = 下一轮的指令。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from memory.context_store import KINDS, ContextEntry, ContextStore
from memory.event import Event
from memory.store import MemoryStore
from models.base import BaseModelAdapter, ModelError

from .config import Settings, settings as default_settings
from .understanding import extract_json, repair_truncated_json

log = logging.getLogger("core.deposit")

DEPOSIT_SYSTEM = (
    '你是 Personal AI 的记忆沉淀模块。读取"当前理解列表"和"最新对话"，'
    "只输出一个严格 JSON 对象（不要代码块、不要解释），格式：\n"
    '{"new": [{"kind": "profile|fact|dynamic|interaction", "content": "新理解"}],\n'
    ' "corrections": [{"ref": "#编号", "content": "纠正后的内容", "reason": "原因"}],\n'
    ' "event": null 或 {"description": "...", "activity": "...", "scene": "...", '
    '"importance": 0.5}}\n'
    "kind 含义：profile=关于他是谁/什么状态的稳定画像；fact=客观事实；"
    "dynamic=当前进行中的事（会过期）；interaction=他希望你如何与他相处（反馈/偏好/纠正）。\n"
    "规则：\n"
    "1. 只提取会改变你未来行为的稳定信息；寒暄、一次性请求不提取。\n"
    "2. 用户在单次请求里说“直接告诉我答案/这次简短点”之类的，是**这一次**的要求，"
    "不是长期偏好，不要提为 interaction。\n"
    "3. corrections 仅在用户明确纠正了当前理解列表中的某条时给出，ref 必须来自列表。\n"
    "4. 不要新增与列表已有内容重复的理解。\n"
    "5. 单轮 new 最多 3 条，每条 content 不超过 100 字。\n"
    "6. event 仅当用户报告了一件真实发生的事（可作为事件记忆）时给出，否则为 null。"
)

@dataclass
class DepositResult:
    created: list[dict] = field(default_factory=list)     # {id, kind, content}
    corrected: list[dict] = field(default_factory=list)   # {id, before, content}
    event_id: str | None = None
    error: str | None = None

    @property
    def touched(self) -> bool:
        return bool(self.created or self.corrected or self.event_id)

    def to_dict(self) -> dict:
        return {
            "created": self.created,
            "corrected": self.corrected,
            "event_id": self.event_id,
            "error": self.error,
        }


class Depositor:
    def __init__(
        self,
        context_store: ContextStore,
        memory_store: MemoryStore,
        model: BaseModelAdapter,
        *,
        settings: Settings | None = None,
    ):
        self.ctx = context_store
        self.store = memory_store
        self.model = model
        self.settings = settings or default_settings

    # ---------- 提示词 ----------
    @staticmethod
    def _entries_block(entries: list[ContextEntry]) -> str:
        if not entries:
            return "（暂无）"
        lines = [
            f"- [#{i}]（{e.kind}）{e.content}"
            for i, e in enumerate(entries, start=1)
        ]
        return "\n".join(lines)

    def build_user_prompt(
        self, user_text: str, answer: str, entries: list[ContextEntry]
    ) -> str:
        return (
            f"当前理解列表（ref 用 #编号 引用）：\n{self._entries_block(entries)}\n\n"
            f"最新对话：\n用户：{user_text}\nAI：{answer}"
        )

    # ---------- 主流程 ----------
    def deposit(
        self,
        user_text: str,
        answer: str,
        entries: list[ContextEntry],
    ) -> DepositResult:
        """提取 + 应用。失败返回带 error 的结果，绝不抛异常打断对话。"""
        result = DepositResult()
        try:
            raw = self.model.chat(
                [
                    {"role": "system", "content": DEPOSIT_SYSTEM},
                    {"role": "user", "content": self.build_user_prompt(
                        user_text, answer, entries
                    )},
                ],
                timeout=self.settings.recall_timeout,
            )
        except ModelError as exc:
            result.error = f"沉淀的模型调用失败：{exc}"
            log.warning(result.error)
            return result

        data = extract_json(raw or "")
        if data is None:
            # 兜底：输出被硬截断（num_predict 上限）时保守修复，救回整轮沉淀
            data = repair_truncated_json(raw or "")
            if data is not None:
                log.info("沉淀 JSON 被截断，已修复后应用：%s", list(data.keys()))
        if data is None:
            result.error = "沉淀输出不是有效 JSON（本轮跳过）"
            log.warning("%s：%s", result.error, (raw or "")[:80])
            return result

        alias_map = {f"#{i}": e for i, e in enumerate(entries, start=1)}
        self._apply_corrections(data, alias_map, result)
        self._apply_creates(data, result)
        self._apply_event(data, result)
        return result

    # ---------- 应用：先纠正，后新增 ----------
    def _apply_corrections(
        self,
        data: dict,
        alias_map: dict[str, ContextEntry],
        result: DepositResult,
    ) -> None:
        corrections = data.get("corrections")
        if not isinstance(corrections, list):
            return
        for item in corrections[:5]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            ref = str(item.get("ref") or item.get("entry_id") or "").strip()
            target = alias_map.get(ref)
            if target is None:  # 允许模型直接回 id
                for e in alias_map.values():
                    if e.id == ref:
                        target = e
                        break
            if target is None:
                log.warning("纠正目标不存在，跳过：ref=%s", ref)
                continue
            reason = str(item.get("reason") or "")
            if self.ctx.correct_entry(
                target.id,
                content,
                source="correction",
                reason=reason,
                max_chars=self.settings.deposit_max_chars,
            ):
                result.corrected.append(
                    {"id": target.id, "before": target.content, "content": content}
                )
                # 列表里的旧内容同步失效（本轮后续步骤用不上了，但保持一致）
                target.content = content

    def _apply_creates(self, data: dict, result: DepositResult) -> None:
        new_items = data.get("new")
        if not isinstance(new_items, list):
            return
        budget = self.settings.deposit_max_new
        for item in new_items:
            if budget <= 0:
                log.info("单轮新增已达上限 %d，剩余丢弃", self.settings.deposit_max_new)
                break
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            content = str(item.get("content") or "").strip()
            if kind not in KINDS:
                continue
            entry, skip = self.ctx.add_entry(
                kind,
                content,
                source="chat",
                max_chars=self.settings.deposit_max_chars,
            )
            if entry is None:
                log.debug("新增跳过：%s", skip)
                continue
            budget -= 1
            result.created.append(
                {"id": entry.id, "kind": kind, "content": content}
            )

    def _apply_event(self, data: dict, result: DepositResult) -> None:
        ev = data.get("event")
        if not isinstance(ev, dict):
            return
        description = str(ev.get("description") or "").strip()[:300]
        if not description:
            return
        try:
            importance = float(ev.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        event = Event(
            source="chat",
            scene=str(ev.get("scene") or "")[:80],
            activity=str(ev.get("activity") or "")[:120],
            description=description,
            importance=max(0.0, min(1.0, importance)),
            raw_understanding=None,
        )
        self.store.add_event(event)
        result.event_id = event.id
        log.info("对话沉淀事件：%s %s", event.id[:8], description[:60])
