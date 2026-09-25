"""对话通道（V0.2 A 组）：文本对话接入现有循环，与图片感知共用记忆管线。

一轮 chat() 走两段：
  读：Context（对你的理解）→ 派生"相处方式指令"进 System Prompt + 近期事件 + 会话历史
      → 模型回答。**指令是否存在直接改变本次判断（approach 字段可断言）。**
  写：沉淀模块提取本轮的新理解/纠正/事件 → 写回 Context。
      本轮的纠正 = 下一轮的指令——这就是"Context 改变 AI 后续行为"的闭环。

隐私：暂停时照常回答（显式提问不是观察），但不写消息、不沉淀、不记事件（D007）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from memory.context_store import ContextEntry, ContextStore
from memory.store import MemoryStore
from models.base import BaseModelAdapter

from .config import Settings, settings as default_settings
from .deposit import Depositor, DepositResult

log = logging.getLogger("core.conversation")

CHAT_SYSTEM = (
    "你是 Personal AI——只属于当前用户的个人智能层，正在和主人日常对话。\n"
    "规则：\n"
    "1) 使用下方“关于这个人的理解”组织回应，让它感觉到你懂他；\n"
    "2) 遇到与理解冲突的信息，以用户当下所说为准；\n"
    "3) 简洁自然，不要汇报你在做什么、不要解释你检索了什么；\n"
    "4) 默认克制：只在有帮助时多说，不主动长篇大论。\n"
    "\n=== 关于这个人的理解 ===\n{context_block}"
    "\n\n=== 相处方式（来自他的过往反馈，必须遵守）===\n{directives_block}"
    "\n\n=== 近期发生的事 ===\n{events_block}"
)


@dataclass
class ChatReply:
    session_id: str
    answer: str
    approach: list[str]                    # 本次判断：["default"] 或相处方式指令
    context_used: list[dict]               # 参与了本次判断的理解条目
    deposit: dict | None = None            # 沉淀结果（暂停时为 None）
    paused: bool = False
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "answer": self.answer,
            "approach": self.approach,
            "context_used": self.context_used,
            "deposit": self.deposit,
            "paused": self.paused,
            "duration_s": round(self.duration_s, 2),
        }


def derive_directives(entries: list[ContextEntry]) -> list[str]:
    """相处方式条目 → 行为指令。这是 Context 影响后续判断的正式通道。"""
    return [e.content for e in entries if e.kind == "interaction"]


def build_context_block(entries: list[ContextEntry]) -> str:
    knowledge = [e for e in entries if e.kind != "interaction"]
    if not knowledge:
        return "（暂无）"
    return "\n".join(
        f"- （{e.kind}）{e.content}" for e in knowledge
    )


class ConversationService:
    def __init__(
        self,
        store: MemoryStore,
        context_store: ContextStore,
        model: BaseModelAdapter,
        *,
        chat_store=None,
        settings: Settings | None = None,
    ):
        from memory.chat_store import ChatStore  # 局部导入避免循环

        self.store = store
        self.ctx = context_store
        self.model = model
        self.settings = settings or default_settings
        self.chat_store = chat_store or ChatStore(store.db_path)
        self.depositor = Depositor(context_store, store, model, settings=self.settings)

    # ---------- 组装 ----------
    def _assemble(
        self, session_id: str, entries: list[ContextEntry]
    ) -> tuple[list[dict], list[str]]:
        directives = derive_directives(entries)
        events = self.store.list_events(limit=self.settings.chat_events_max)
        # 取最近 N 条并按时间正序进入提示词（desc 取最近，再翻回正序）
        history = self.chat_store.history(
            session_id, limit=self.settings.chat_history_max, order="desc"
        )

        system = CHAT_SYSTEM.format(
            context_block=build_context_block(entries),
            directives_block="\n".join(f"- {d}" for d in directives) or "（暂无）",
            events_block="\n".join(e.context_line() for e in events) or "（暂无）",
        )
        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(
            {"role": m["role"], "content": m["content"]} for m in history
        )
        return messages, directives

    # ---------- 主流程 ----------
    def chat(
        self,
        message: str,
        session_id: str | None = None,
    ) -> ChatReply:
        started = time.perf_counter()
        sid = self.chat_store.get_or_create_session(session_id)
        paused = self.store.paused

        # ---- 读 ----
        entries = self.ctx.list_entries(limit=self.settings.context_max_entries)
        messages, directives = self._assemble(sid, entries)
        messages.append({"role": "user", "content": message})

        answer = self.model.chat(
            messages, timeout=self.settings.recall_timeout
        ).strip()

        # 判断可断言：指令存在 → approach = 指令；否则默认
        approach = directives or ["default"]

        # ---- 写 ----
        deposit: dict | None = None
        if paused:
            log.info("暂停中：回答已给出，但不写消息、不沉淀")
        else:
            self.ctx.mark_used(e.id for e in entries)
            for e in entries:  # 内存对象同步（回复里的 used_count 是"用过之后"）
                e.used_count += 1
            result: DepositResult = self.depositor.deposit(message, answer, entries)
            deposit = result.to_dict()
            self.chat_store.append(sid, "user", message)
            self.chat_store.append(sid, "assistant", answer)

        reply = ChatReply(
            session_id=sid,
            answer=answer,
            approach=approach,
            context_used=[e.to_public_dict() for e in entries],
            deposit=deposit,
            paused=paused,
            duration_s=time.perf_counter() - started,
        )
        log.info(
            "对话完成 session=%s 判断=%s 沉淀=%s 耗时=%.1fs",
            sid[:8],
            approach if len(approach) <= 2 else f"{len(approach)}条指令",
            "跳过（暂停）" if deposit is None else
            f"新增{len(deposit['created'])}/纠正{len(deposit['corrected'])}",
            reply.duration_s,
        )
        return reply
