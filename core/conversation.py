"""对话通道（V0.2 A 组）：文本对话接入现有循环，与图片感知共用记忆管线。

一轮 chat() 走两段：
  读：Context（对你的理解）→ 派生"相处方式指令"进 System Prompt + 近期事件 + 会话历史
      → 模型回答。**指令是否存在直接改变本次判断（approach 字段可断言）。**
  写：沉淀模块提取本轮的新理解/纠正/事件 → 写回 Context。
      本轮的纠正 = 下一轮的指令——这就是"Context 改变 AI 后续行为"的闭环。

隐私：暂停时照常回答（显式提问不是观察），但不写消息、不沉淀、不记事件（D007）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from memory.context_store import (
    KIND_LABELS,
    ContextEntry,
    ContextStore,
    sort_for_prompt,
)
from memory.goal_store import GoalStore
from memory.hypothesis_store import STATUS_LABELS, HypothesisStore
from memory.pattern_store import PatternStore
from memory.store import MemoryStore
from models.base import BaseModelAdapter, ModelError

from .config import Settings, settings as default_settings
from .deposit import Depositor, DepositResult
from .inducer import Inducer
from .pattern_engine import PatternEngine
from .verifier import (
    extract_goal_title,
    is_goal_declaration,
    is_pattern_declaration,
    pattern_scope_for,
    resolve_goal_answer,
    resolve_pattern_answer,
    resolve_pending_answer,
    select_candidate,
    select_goal_candidate,
    select_pattern_candidate,
)
from .verifier import resolve_pending_answer, select_candidate

log = logging.getLogger("core.conversation")

# ---------- V0.5.2 P0-3："别记这个"硬开关（D017 ③，常量可测）----------
# 用户明确要求不记录 → 本轮跳过 deposit/inducer/promoter/B-intake/goal-propose
# 全部学习写路径（只读 + 回答照常；history 照常追加，保证对话连贯）。
# 边界：history（chat_store）不算长期记忆——"DB 零新增"指
# events/context/hypothesis/pattern/goal 五个长期表，history 追加不在此列。
NO_MEMORIZE_PATTERNS = (
    "别记", "不要保存", "不用记", "别存", "忘掉这个", "删除.{0,6}记忆",
    "别记住", "不要记住", "没必要记", "只是随便聊聊", "不要以后提",
)
NO_MEMORIZE_RE = re.compile("|".join(NO_MEMORIZE_PATTERNS))
# 本轮 system prompt 注入的固定指令（可测：断言含此文案）
NO_MEMORIZE_PROMPT_RULE = (
    "【本轮不记录】用户要求这次内容不进入长期记忆。"
    "你的上下文里看不到本轮具体内容（已被隐藏）。"
    "如被问及该轮，必须说'你要求那部分不记录，我这里没有保留'；"
    "禁止复述任何细节（你也看不到，编出来就是撒谎）；"
    "禁止对其他轮次内容声称'不会记'（只对被隐藏轮次做此声明）。"
)


def is_no_memorize(text: str) -> bool:
    """确定性判定：用户要求本轮不记录吗？

    "记错"先剥离——"别记错，我是高二"是纠正（必须写），不是删除请求。
    """
    t = (text or "").replace("记错", "")
    return bool(NO_MEMORIZE_RE.search(t))


# ---------- V0.5.2 Test5：自夸限制（D017，常量可测）----------
SELF_PRAISE_RE = re.compile("完全了解|彻底了解|非常了解|很懂我|全部了解")
SELF_PRAISE_RULE = (
    "【诚实边界】你目前只掌握 {n} 条理解、{m} 个待验证假设——"
    "不得声称已完全了解或已经吃透用户；被问及了解程度时，"
    "如实说明掌握范围与不确定性。"
)

CHAT_SYSTEM = (
    "你是 Personal AI——只属于当前用户的个人智能层，正在和主人日常对话。\n"
    "规则：\n"
    "1) 使用下方“关于这个人的理解”组织回应，让它感觉到你懂他；\n"
    "2) 遇到与理解冲突的信息，以用户当下所说为准；\n"
    "3) 简洁自然，不要汇报你在做什么、不要解释你检索了什么；\n"
    "4) 默认克制：只在有帮助时多说，不主动长篇大论；\n"
    "5) 若“发生的事”里有与他当前话题相关的旧事（哪怕他换了说法），"
    "主动接回：上次做到哪、卡在哪、当时为什么停——一句话自然带出，别像汇报。\n"
    "\n=== 关于这个人的理解 ===\n{context_block}"
    "\n\n=== 相处方式（来自他的过往反馈，必须遵守）===\n{directives_block}"
    "\n\n=== 发生的事（近期 + 与话题相关的更早记录）===\n{events_block}"
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
    hypotheses_used: list[dict] | None = None  # V0.3：本次进入 Prompt 的假设
    verification_question: dict | None = None  # V0.4：本轮附带的问题，无则 None
    patterns_used: list[dict] | None = None  # V0.5：本次进入 Prompt 的规律

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "answer": self.answer,
            "approach": self.approach,
            "context_used": self.context_used,
            "deposit": self.deposit,
            "paused": self.paused,
            "duration_s": round(self.duration_s, 2),
            "hypotheses_used": self.hypotheses_used or [],
            "verification_question": self.verification_question,
            "patterns_used": self.patterns_used or [],
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
        hypothesis_store: HypothesisStore | None = None,
        pattern_store: PatternStore | None = None,
        goal_store: GoalStore | None = None,
        service_state=None,
    ):
        from memory.chat_store import ChatStore  # 局部导入避免循环

        self.store = store
        self.ctx = context_store
        self.model = model
        self.settings = settings or default_settings
        self.chat_store = chat_store or ChatStore(store.db_path)
        # V0.3/V0.5：默认与记忆同库（零配置）；测试可注入独立实例
        self.hyp = hypothesis_store or HypothesisStore(store.db_path)
        self.pat = pattern_store or PatternStore(store.db_path)
        self.goal = goal_store or GoalStore(store.db_path)
        self.depositor = Depositor(context_store, store, model,
                                   settings=self.settings,
                                   hypothesis_store=self.hyp,
                                   pattern_store=self.pat)
        self.inducer = Inducer(store, context_store, self.hyp)
        self.promoter = PatternEngine(store, self.hyp, self.pat)
        # ---- 异步沉淀基础设施（第二阶段：Memory 后台化）----
        # _deposit_tasks: 正在后台跑的沉淀任务引用（防 GC）
        # _deposit_dedup: 已调度/正在跑的消息哈希（sha256 前 16 字符）
        # _deposit_lock: 保护去重集合的并发访问
        self._deposit_tasks: set = set()
        self._deposit_dedup: set = set()
        self._deposit_lock = None  # asyncio.Lock 在首次 async 调用时创建
        self._loop = None          # 创建后台任务的 event loop（线程池侧投递等待用）
        self._deposit_wait_timeout = 15.0  # 等待后台沉淀的上限（秒），超时继续对话
        # ---- 服务状态缓存（status 去联网）----
        # 真实调用成功/失败写这里，/api/status 只读不联网。
        from .service_state import default_service_state
        self._svc = service_state or default_service_state()
        # 话题命中复用回忆的检索级联（D003；模型只是构造需要，不参与检索）
        from .context import Recaller

        self._recaller = Recaller(store, model, settings=self.settings)

    # ---------- 服务状态：只记真实调用，不记探测 ----------
    def _mark_service(self, ok: bool, *, elapsed_ms: int | None = None,
                      detail: str | None = None) -> None:
        """把一次**真实回答调用**的结果写进缓存（status 据此显示三态）。

        只在回答路径记；后台沉淀用的是另一轮调用，失败也不该把徽章打成红
        （答案已经交付了，那是沉淀的锅，不是模型服务的锅）。
        """
        try:
            from .service_state import identity_of
            self._svc.record(
                "ok" if ok else "error",
                identity=identity_of(self.model),
                elapsed_ms=elapsed_ms, detail=detail)
        except Exception as exc:  # 记状态绝不许掀翻对话
            log.warning("写服务状态缓存失败（忽略）：%s", exc)

    # ---------- 组装 ----------
    def _assemble(
        self, session_id: str, entries: list[ContextEntry], message: str,
        no_memorize: bool = False,
    ) -> tuple[list[dict], list[str], list]:
        from .context import plan_query

        directives = derive_directives(entries)

        # V0.3：待验证假设进 Prompt——单独段、降权标记，绝不当事实。
        # 韧性：假设库故障不许掀翻对话（辅助判断没有这个权力）。
        try:
            hyps = self.hyp.list_for_prompt(
                limit=self.settings.hypotheses_max_prompt)
        except Exception as exc:
            log.warning("假设读取失败（本轮跳过假设段）：%s", exc)
            hyps = []
        if hyps:
            lines = []
            for h in hyps:
                tag = ("用户已确认" if h.status == "confirmed"
                       else "待验证")
                # 假设陈述里的换行/伪节头不许污染 system prompt 结构
                stmt = " ".join(str(h.statement).split())
                lines.append(
                    f"- [{tag}·置信度{h.confidence:.2f}] {stmt}")
            hypo_block = "\n".join(lines)
        else:
            hypo_block = "（暂无）"

        # V0.5：已确认规律进 Prompt（独立段，权重高于假设段）；
        # 活跃目标进 Prompt（只引用标题，不推测）。
        # 韧性：附属读取失败不许掀翻对话。
        try:
            pats = self.pat.list_confirmed(
                limit=self.settings.patterns_max_prompt)
        except Exception as exc:
            log.warning("规律读取失败（本轮跳过规律段）：%s", exc)
            pats = []
        try:
            goals = self.goal.list_active(
                limit=self.settings.goals_max_prompt)
        except Exception as exc:
            log.warning("目标读取失败（本轮跳过目标段）：%s", exc)
            goals = []

        # 近期事件 + 话题命中的更早事件（B1：重提旧事），合并去重
        matched, _ = self._recaller.retrieve(plan_query(message))
        recent = self.store.list_events(limit=self.settings.chat_events_max)
        seen: set[str] = set()
        events = []
        for e in [*matched, *recent]:
            if e.id not in seen:
                seen.add(e.id)
                events.append(e)
        events = events[: self.settings.chat_events_total]

        # 取最近 N 条并按时间正序进入提示词（desc 取最近，再翻回正序）。
        # V0.5.3：走 history_for_prompt——别记轮替换为占位符，
        # 模型看不到原文（不可引用、不可复述）；UI 回看不受影响。
        history = self.chat_store.history_for_prompt(
            session_id, limit=self.settings.chat_history_max, order="desc"
        )

        system = CHAT_SYSTEM.format(
            context_block=build_context_block(entries),
            directives_block="\n".join(f"- {d}" for d in directives) or "（暂无）",
            events_block="\n".join(e.context_line() for e in events) or "（暂无）",
        )
        if hyps:
            system += (
                "\n\n=== 待验证假设（辅助判断用，不是事实；"
                "若与用户当下所说冲突，以用户为准）===\n" + hypo_block
            )
        if pats:
            pat_lines = []
            for p in pats:
                stmt = " ".join(str(p.statement).split())
                pat_lines.append(
                    f"- [规律·置信度{p.confidence:.2f}] {stmt}")
            system += (
                "\n\n=== 已验证规律（可作为建议依据；"
                "若与用户当下所说冲突，以用户为准）===\n"
                + "\n".join(pat_lines)
            )
        if goals:
            goal_lines = [f"- {g.title}" for g in goals]
            system += (
                "\n\n=== 他的长期目标（仅引用，不推测；"
                "建议应尽量与目标同向）===\n" + "\n".join(goal_lines)
            )
        if no_memorize:
            # V0.5.2 P0-3：本轮注入诚实回答指令（单测断言 system 含此文案）
            system += "\n\n" + NO_MEMORIZE_PROMPT_RULE
        if SELF_PRAISE_RE.search(message or ""):
            # V0.5.2 Test5：自夸限制（数字如实，单测断言指令存在）
            system += "\n\n" + SELF_PRAISE_RULE.format(
                n=len(entries), m=len(hyps))
        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(
            {"role": m["role"], "content": m["content"]} for m in history
        )

        # B3 无问先备：组装完成即入日志（可审计）
        kinds: dict[str, int] = {}
        for e in entries:
            label = KIND_LABELS.get(e.kind, e.kind)
            kinds[label] = kinds.get(label, 0) + 1
        log.info(
            "上下文组装：理解 %d 条（%s）· 事件 %d 条（话题命中 %d）·"
            " 会话历史 %d 条 · 待验证假设 %d 条 · 已确认规律 %d 条 ·"
            " 活跃目标 %d 个",
            len(entries),
            "、".join(f"{k}{v}" for k, v in kinds.items() if v) or "无",
            len(events),
            len(matched),
            len(history),
            len(hyps),
            len(pats),
            len(goals),
        )
        return messages, directives, hyps, pats

    # ---------- 验证提问选择（假设 → 规律 → 目标，一轮一个）----------
    def _pick_verification(self, message: str):
        """返回 (store, candidate, question) 或 None。各 store 内部判空。"""
        try:
            if self.hyp.get_pending() is not None:
                return None
            if self.pat.get_pending() is not None:
                return None
            if self.goal.get_pending() is not None:
                return None
        except Exception as exc:
            log.warning("挂起检查失败（本轮不问）：%s", exc)
            return None
        picked = select_candidate(self.hyp, message, paused=False)
        if picked is not None:
            cand, question = picked
            return self.hyp, cand, question
        picked = select_pattern_candidate(self.pat, message, paused=False)
        if picked is not None:
            cand, question = picked
            return self.pat, cand, question
        picked = select_goal_candidate(self.goal, message, paused=False)
        if picked is not None:
            cand, question = picked
            return self.goal, cand, question
        return None

    @staticmethod
    def _still_askable(kind: str, obj) -> bool:
        """目标是否仍值得挂着问题：hyp/pattern 限 tentative，goal 限 proposed。"""
        if obj is None:
            return False
        if kind == "goal":
            return obj.status == "proposed"
        return obj.status == "tentative"

    def _retract_if_resolved(self, verification_out: dict | None):
        """收回已解决目标的挂起（本轮写路径可能否决/确认了它）。

        返回传入的元数据（仍可问）或 None（已解决：清挂起 + 元数据置空）。
        回答正文已发出无法收回，历史保留原文——这是诚实代价，注释在此。
        """
        if verification_out is None:
            return None
        stores = {"hypothesis": self.hyp, "pattern": self.pat,
                  "goal": self.goal}
        kind = verification_out.get("kind", "hypothesis")
        store_obj = stores.get(kind)
        if store_obj is None:
            return verification_out
        hid = verification_out.get("hypothesis_id", "")
        try:
            target = store_obj.get(hid)
        except Exception:
            return verification_out
        if self._still_askable(kind, target):
            return verification_out
        try:
            store_obj.clear_pending(hid)
        except Exception:
            pass
        log.info("验证问题目标已解决，收回挂起：%s %s",
                 kind, hid[:8])
        return None

    # ---------- 主流程 ----------
    def chat(
        self,
        message: str,
        session_id: str | None = None,
    ) -> ChatReply:
        started = time.perf_counter()
        sid = self.chat_store.get_or_create_session(session_id)
        paused = self.store.paused
        # V0.5.2 P0-3："别记这个"是硬开关，不让模型决定（D017 ③）
        no_memorize = is_no_memorize(message)
        if no_memorize:
            log.info("用户要求不记录：本轮跳过全部学习写路径（history 照常）")

        # V0.4/V0.5：先解决上一轮挂起的验证问题（确认/否决立即生效，
        # 本轮 prompt 就能用上），再组装。暂停时不学不问；
        # 不记录轮同样不写（挂起保持原样，下轮再谈）。
        # 解决顺序固定：假设 → 规律 → 目标（一次只解决一条，不双重分类）。
        verification_in = None
        if not paused and not no_memorize:
            try:
                if self.hyp.get_pending() is not None:
                    verification_in = resolve_pending_answer(
                        self.hyp, message)
                elif self.pat.get_pending() is not None:
                    verification_in = resolve_pattern_answer(
                        self.pat, message)
                elif self.goal.get_pending() is not None:
                    verification_in = resolve_goal_answer(
                        self.goal, message)
            except Exception as exc:  # 双保险：绝不断对话
                log.warning("验证回答处理失败（跳过）：%s", exc)
                verification_in = None
            if verification_in is not None:
                log.info("验证回答落链：%s %s → %s",
                         verification_in.get("kind", "hypothesis"),
                         verification_in["hypothesis_id"][:8],
                         verification_in["verdict"])

        # ---- 读 ----
        # 第二阶段：读取 memory 前，等待后台沉淀完成（防竞态）。
        # chat() 是同步函数，在 FastAPI 里跑在线程池（没有 event loop），
        # 必须把等待投递回"创建任务的那个 loop"，不能 asyncio.run 新建 loop。
        self._wait_deposit_sync()
        # V0.5.2（D017 ⑤）：质量排序——信任档 + 时间衰减决定谁进 prompt，
        # 条数上限不变；prompt 文本格式不动（行为变化只来自顺序）。
        entries = sort_for_prompt(
            self.ctx.list_entries())[: self.settings.context_max_entries]
        messages, directives, hyps, pats = self._assemble(
            sid, entries, message, no_memorize=no_memorize)
        messages.append({"role": "user", "content": message})

        _t0 = time.perf_counter()
        try:
            answer = self.model.chat(
                messages, timeout=self.settings.recall_timeout
            ).strip()
        except ModelError as exc:
            self._mark_service(False, detail=str(exc))
            raise
        _ms = round((time.perf_counter() - _t0) * 1000)
        self._mark_service(True, elapsed_ms=_ms)
        if not answer:
            # 空回答不是回答：按模型故障处理（503），不拿空白糊弄用户
            self._mark_service(False, elapsed_ms=_ms, detail="模型返回了空回答")
            raise ModelError("模型返回了空回答")

        # V0.4/V0.5：回答生成后，顺带选一个验证问题附在末尾（低存在感附加）。
        # 顺序固定：假设 → 规律 → 目标，一轮只问一个；刚解决完（确认/否决）
        # 本轮不再问新的（解决了一件事就闭嘴）。
        # 沉淀用模型原文（不含我们的问题），历史存用户实际看到的版本。
        return self._write_turn(
            sid=sid, message=message, model_answer=answer,
            entries=entries, directives=directives, hyps=hyps, pats=pats,
            verification_in=verification_in, paused=paused,
            no_memorize=no_memorize, started=started)

    async def _amodel_chunks(self, messages, timeout):
        """取模型流：优先 async（真链路），Mock/旧适配器回落同步版。"""
        fn = getattr(self.model, "chat_stream_async", None)
        if fn is not None:
            async for chunk in fn(messages, timeout=timeout):
                yield chunk
            return
        for chunk in self.model.chat_stream(messages, timeout=timeout):
            yield chunk

    async def stream_chat(self, message: str, session_id: str | None = None):
        """流式对话异步生成器（第一阶段 Streaming 真链路）。

        yield 顺序：{"delta": 片段}×N → {"done": True, "reply": ChatReply.to_dict()}。
        沉淀/归纳/验证附加在流结束后同步跑（与 chat 同语义，完整文本已得，
        下一轮可见，无竞态）。空回答 → yield {"error": ...} 并结束。
        """
        started = time.perf_counter()
        sid = self.chat_store.get_or_create_session(session_id)
        paused = self.store.paused
        no_memorize = is_no_memorize(message)
        if no_memorize:
            log.info("用户要求不记录：本轮跳过全部学习写路径（history 照常）")
        verification_in = None
        if not paused and not no_memorize:
            try:
                if self.hyp.get_pending() is not None:
                    verification_in = resolve_pending_answer(
                        self.hyp, message)
                elif self.pat.get_pending() is not None:
                    verification_in = resolve_pattern_answer(
                        self.pat, message)
                elif self.goal.get_pending() is not None:
                    verification_in = resolve_goal_answer(
                        self.goal, message)
            except Exception as exc:
                log.warning("验证回答处理失败（跳过）：%s", exc)
                verification_in = None
        # 第二阶段：读取 memory 前，等待后台沉淀完成（防竞态）。
        # 注意：这里已经在 event loop 里，必须 await，不能 asyncio.run（会抛
        # "cannot be called from a running event loop" 并静默跳过等待）。
        try:
            await asyncio.wait_for(self._await_deposit(),
                                   timeout=self._deposit_wait_timeout)
        except asyncio.TimeoutError:
            log.warning("等待后台沉淀超时 %.0fs（用现有记忆继续）",
                        self._deposit_wait_timeout)
        except Exception as exc:  # 等待失败不掀翻对话
            log.warning("等待后台沉淀失败（继续对话）：%s", exc)
        entries = sort_for_prompt(
            self.ctx.list_entries())[: self.settings.context_max_entries]
        messages, directives, hyps, pats = self._assemble(
            sid, entries, message, no_memorize=no_memorize)
        messages.append({"role": "user", "content": message})
        _t0 = time.perf_counter()
        try:
            parts = []
            async for chunk in self._amodel_chunks(
                    messages, self.settings.recall_timeout):
                if chunk:
                    parts.append(chunk)
                    yield {"delta": chunk}
        except ModelError as exc:
            self._mark_service(
                False,
                elapsed_ms=round((time.perf_counter() - _t0) * 1000),
                detail=str(exc))
            yield {"error": str(exc)}
            return
        _ms = round((time.perf_counter() - _t0) * 1000)
        self._mark_service(True, elapsed_ms=_ms)
        model_answer = "".join(parts).strip()
        if not model_answer:
            self._mark_service(False, elapsed_ms=_ms, detail="模型返回了空回答")
            yield {"error": "模型返回了空回答"}
            return
        try:
            reply = self._write_turn(
                sid=sid, message=message, model_answer=model_answer,
                entries=entries, directives=directives, hyps=hyps, pats=pats,
                verification_in=verification_in, paused=paused,
                no_memorize=no_memorize, started=started)
        except ModelError as exc:
            yield {"error": str(exc)}
            return
        yield {"done": True, "reply": reply.to_dict()}

    async def _deposit_bg(self, *, sid: str, message: str, model_answer: str,
                          entries, hyps, pats, verification_out):
        """后台沉淀：mark_used + deposit + induce + promote + B-path + goal。

        这一段全是**同步阻塞**调用（含模型请求 5~9s）。必须整体丢进线程池，
        否则它会卡死 event loop——那不叫后台，只叫"换个地方卡"。

        任何异常只记日志，绝不影响已完成的聊天。
        """
        await asyncio.to_thread(
            self._deposit_body, sid=sid, message=message,
            model_answer=model_answer, entries=entries, hyps=hyps, pats=pats,
            verification_out=verification_out)

    def _deposit_body(self, *, sid: str, message: str, model_answer: str,
                      entries, hyps, pats, verification_out):
        """沉淀的实际工作（跑在线程池里，见 _deposit_bg）。"""
        try:
            self.ctx.mark_used(e.id for e in entries)
            for e in entries:
                e.used_count += 1
            if hyps:
                try:
                    self.hyp.mark_used([h.id for h in hyps])
                except Exception as exc:
                    log.warning("假设使用计数失败（跳过）：%s", exc)
            if pats:
                try:
                    self.pat.mark_used([p.id for p in pats])
                except Exception as exc:
                    log.warning("规律使用计数失败（跳过）：%s", exc)
            # 沉淀（模型调用，5~9s）
            result: DepositResult | None = None
            try:
                result = self.depositor.deposit(
                    message, model_answer, entries)
                deposit = result.to_dict()
            except Exception as exc:
                log.error("后台沉淀异常（跳过）：%s", exc)
                deposit = {"error": f"沉淀异常，已跳过：{exc}"}
            # 自然证据 / 变化信号（V0.5.6）
            # 位置：沉淀之后、归纳之前——此时本轮 Context 已落库，
            # 归纳尚未跑（不会给本轮刚冒出来的假设倒灌证据，更保守）。
            try:
                from .natural_bridge import apply_natural_signal

                natural = apply_natural_signal(
                    self.ctx, self.hyp, message,
                    exclude_entry_ids=[e.get("id") for e
                                       in (result.created or [])],
                ) if result is not None else {}
                if natural.get("hypo_ids") or natural.get("entry_ids"):
                    log.info("自然信号 %s：假设 %d / 理解 %d",
                             natural.get("signal"),
                             len(natural.get("hypo_ids") or []),
                             len(natural.get("entry_ids") or []))
            except Exception as exc:
                log.warning("自然证据接线失败（跳过，不影响对话）：%s", exc)
            # 归纳
            try:
                induced = self.inducer.induce_all()
                if induced.touched:
                    log.info("归纳：新建 %d / 补证据 %d",
                             len(induced.created), len(induced.linked))
            except Exception as exc:
                log.warning("归纳失败（跳过）：%s", exc)
            # 晋升
            try:
                promoted = self.promoter.promote_all()
                if promoted:
                    log.info("晋升：新建规律 %d 条", len(promoted))
            except Exception as exc:
                log.warning("晋升失败（跳过）：%s", exc)
            # 后台推理（V0.5.8）
            # 位置在晋升之后：后台推测恒为 tentative，本来也进不了
            # 本轮 promote 的 confirmed 分组；放后面语义更干净——
            # 它只看已有的东西，不参与本轮任何判断。
            try:
                from .background_bridge import run_background_reasoning

                bg = run_background_reasoning(self.store, self.hyp)
                if bg.get("proposed"):
                    log.info("后台推理提出 %d 条待验证推测",
                             len(bg["proposed"]))
            except Exception as exc:
                log.warning("后台推理失败（跳过，不影响对话）：%s", exc)
            # B 路径
            try:
                if is_pattern_declaration(message):
                    scope = pattern_scope_for(message)
                    pat, why = self.pat.declare_from_user(
                        message.strip()[:300], scope, message)
                    if pat is not None:
                        log.info("B 路径建待验证规律 [%s]", scope)
                    else:
                        log.debug("B 路径跳过：%s", why)
            except Exception as exc:
                log.warning("B 路径建规律失败（跳过）：%s", exc)
            # Goal
            try:
                if is_goal_declaration(message):
                    title = extract_goal_title(message)
                    goal, why = self.goal.propose(title, message)
                    if goal is not None:
                        log.info("Goal 待确认：%s", title[:40])
                    else:
                        log.debug("Goal 跳过：%s", why)
            except Exception as exc:
                log.warning("Goal 提议失败（跳过）：%s", exc)
            # 挂起复核
            if verification_out is not None:
                try:
                    verification_out = self._retract_if_resolved(
                        verification_out)
                except Exception as exc:
                    log.warning("挂起复核失败（保留原样）：%s", exc)
            log.info("后台沉淀完成 session=%s deposit=%s",
                     sid[:8],
                     "ok" if not deposit.get("error") else deposit["error"])
        except Exception as exc:
            log.error("后台沉淀顶层异常（不影响聊天）：%s", exc)

    def _schedule_deposit(self, *, sid: str, message: str, model_answer: str,
                          entries, hyps, pats, verification_out):
        """调度后台沉淀：去重 + create_task。同一条消息不重复沉淀。"""
        import hashlib
        key = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
        if key in self._deposit_dedup:
            log.info("沉淀去重跳过：%s", key)
            return
        self._deposit_dedup.add(key)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无 event loop（同步测试）：直接跑完
            asyncio.run(self._deposit_bg(
                sid=sid, message=message, model_answer=model_answer,
                entries=entries, hyps=hyps, pats=pats,
                verification_out=verification_out))
            return
        task = loop.create_task(self._deposit_bg(
            sid=sid, message=message, model_answer=model_answer,
            entries=entries, hyps=hyps, pats=pats,
            verification_out=verification_out))
        self._deposit_tasks.add(task)
        task.add_done_callback(self._deposit_tasks.discard)
        self._loop = loop   # 供线程池侧投递等待（_wait_deposit_sync）

    def _wait_deposit_sync(self):
        """同步侧等待后台沉淀（chat() 在线程池里跑时用）。

        把协程投递回"创建后台任务的那个 loop"执行并阻塞等待结果；
        没有已知的 loop（纯同步场景，如测试、脚本）时什么都不做——
        那种情况下 _schedule_deposit 已经直接 asyncio.run 跑完了。
        """
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._await_deposit(), loop)
            fut.result(timeout=self._deposit_wait_timeout)
        except Exception as exc:
            log.warning("等待后台沉淀失败（继续对话）：%s", exc)

    async def _await_deposit(self):
        """下一轮读取 memory 前，等待后台沉淀完成（防竞态）。"""
        if not self._deposit_tasks:
            return
        pending = list(self._deposit_tasks)
        await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self):
        """应用退出：取消所有后台沉淀任务，不产生孤儿。"""
        if not self._deposit_tasks:
            return
        tasks = list(self._deposit_tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._deposit_tasks.clear()
        self._loop = None
        log.info("后台沉淀任务已全部取消")

    def _write_turn(self, *, sid: str, message: str, model_answer: str,
                    entries, directives, hyps, pats,
                    verification_in, paused: bool, no_memorize: bool,
                    started: float) -> ChatReply:
        """快速路径：验证提问 + history 追加 → 立即返回。

        慢速路径（mark_used + deposit + induce + promote + B-path + goal）
        通过 _schedule_deposit 后台执行，不阻塞用户请求。
        """
        answer = model_answer
        verification_out = None
        resolved = (verification_in is not None
                    and verification_in.get("verdict") in ("confirm", "deny"))
        if not paused and not resolved:
            try:
                picked = self._pick_verification(message)
            except Exception as exc:
                log.warning("验证对象选择失败（本轮不问）：%s", exc)
                picked = None
            if picked is not None:
                store_obj, cand, question = picked
                try:
                    ok, why = store_obj.set_pending(cand.id, question.text)
                except Exception as exc:
                    log.warning("挂起验证问题失败（本轮不问）：%s", exc)
                    ok, why = False, str(exc)
                if ok:
                    verification_out = question.to_dict()
                    answer = model_answer + "\n\n" + question.text
                else:
                    log.info("验证问题未挂起（%s），本轮不问", why)

        approach = directives or ["default"]

        # ---- 快速路径：history 追加（保证对话连贯）----
        if paused:
            log.info("暂停中：回答已给出，但不写消息、不沉淀")
        elif no_memorize:
            self.chat_store.append(sid, "user", message, suppressed=True)
            self.chat_store.append(sid, "assistant", answer, suppressed=True)
        else:
            self.chat_store.append(sid, "user", message)
            self.chat_store.append(sid, "assistant", answer)

        # ---- 慢速路径：后台沉淀（不阻塞请求）----
        if not paused and not no_memorize:
            self._schedule_deposit(
                sid=sid, message=message, model_answer=model_answer,
                entries=entries, hyps=hyps, pats=pats,
                verification_out=verification_out)

        reply = ChatReply(
            session_id=sid,
            answer=answer,
            approach=approach,
            context_used=[e.to_public_dict() for e in entries],
            deposit=None,  # 后台执行，请求内不可见
            paused=paused,
            duration_s=time.perf_counter() - started,
            hypotheses_used=[h.to_public_dict() for h in hyps],
            verification_question=verification_out,
            patterns_used=[p.to_public_dict() for p in pats],
        )
        log.info(
            "对话完成 session=%s 判断=%s 沉淀=后台 耗时=%.1fs%s",
            sid[:8],
            approach if len(approach) <= 2 else f"{len(approach)}条指令",
            reply.duration_s,
            (" 验证提问=" + verification_out["hypothesis_id"][:8])
            if verification_out else "",
        )
        return reply
