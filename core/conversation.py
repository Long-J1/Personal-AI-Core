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
    "如提及记忆，必须诚实说明这次内容不会进入长期记忆；"
    "禁止说“记住了/已清除记忆”等暗示已执行删除的话术。"
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
        # 话题命中复用回忆的检索级联（D003；模型只是构造需要，不参与检索）
        from .context import Recaller

        self._recaller = Recaller(store, model, settings=self.settings)

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

        # 取最近 N 条并按时间正序进入提示词（desc 取最近，再翻回正序）
        history = self.chat_store.history(
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
        # V0.5.2（D017 ⑤）：质量排序——信任档 + 时间衰减决定谁进 prompt，
        # 条数上限不变；prompt 文本格式不动（行为变化只来自顺序）。
        entries = sort_for_prompt(
            self.ctx.list_entries())[: self.settings.context_max_entries]
        messages, directives, hyps, pats = self._assemble(
            sid, entries, message, no_memorize=no_memorize)
        messages.append({"role": "user", "content": message})

        answer = self.model.chat(
            messages, timeout=self.settings.recall_timeout
        ).strip()
        if not answer:
            # 空回答不是回答：按模型故障处理（503），不拿空白糊弄用户
            raise ModelError("模型返回了空回答")

        # V0.4/V0.5：回答生成后，顺带选一个验证问题附在末尾（低存在感附加）。
        # 顺序固定：假设 → 规律 → 目标，一轮只问一个；刚解决完（确认/否决）
        # 本轮不再问新的（解决了一件事就闭嘴）。
        # 沉淀用模型原文（不含我们的问题），历史存用户实际看到的版本。
        model_answer = answer
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

        # 判断可断言：指令存在 → approach = 指令；否则默认
        approach = directives or ["default"]

        # ---- 写 ----
        deposit: dict | None = None
        if paused:
            log.info("暂停中：回答已给出，但不写消息、不沉淀")
        elif no_memorize:
            # V0.5.2 P0-3：零学习写入；history 照常追加保证对话连贯
            # （history 不属于五个长期表，不违反"DB 零新增"验收）
            self.chat_store.append(sid, "user", message)
            self.chat_store.append(sid, "assistant", answer)
        else:
            self.ctx.mark_used(e.id for e in entries)
            for e in entries:  # 内存对象同步（回复里的 used_count 是"用过之后"）
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
            # 沉淀自称"绝不抛异常"，这里再加一道保险：沉淀的任何失败
            # （如 sqlite 锁）都不许吞掉用户已经拿到的回答。
            # 注意：沉淀吃的是模型原文，不含我们附带的问题（问不算答）。
            try:
                result: DepositResult = self.depositor.deposit(
                    message, model_answer, entries)
            except Exception as exc:
                log.error("沉淀异常（本轮跳过沉淀）：%s", exc)
                result = DepositResult(error=f"沉淀异常，已跳过：{exc}")
            deposit = result.to_dict()
            # V0.3：沉淀后跑规则归纳（后台路径；失败只记日志，不打断对话）
            try:
                induced = self.inducer.induce_all()
                if induced.touched:
                    log.info("归纳：新建 %d / 补证据 %d",
                             len(induced.created), len(induced.linked))
            except Exception as exc:
                log.warning("归纳失败（跳过）：%s", exc)
            # V0.5：归纳后跑晋升（confirmed 假设组 → tentative 规律）
            try:
                promoted = self.promoter.promote_all()
                if promoted:
                    log.info("晋升：新建规律 %d 条", len(promoted))
            except Exception as exc:
                log.warning("晋升失败（跳过）：%s", exc)
            # V0.5 B 路径：用户明确表达规律 → 建 tentative（验证后才算数）
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
            # V0.5 Goal：用户明确表达目标 → 建 proposed（确认后才 active）
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
            # 本轮刚问出的问题，若目标在本轮写路径中已解决
            # （如沉淀纠正联动否决了它），收回挂起：下轮不再误判，
            # 元数据同步置空（回答正文已发出，历史保留原文）。
            if verification_out is not None:
                try:
                    verification_out = self._retract_if_resolved(
                        verification_out)
                except Exception as exc:
                    log.warning("挂起复核失败（保留原样）：%s", exc)
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
            hypotheses_used=[h.to_public_dict() for h in hyps],
            verification_question=verification_out,
            patterns_used=[p.to_public_dict() for p in pats],
        )
        log.info(
            "对话完成 session=%s 判断=%s 沉淀=%s 耗时=%.1fs%s",
            sid[:8],
            approach if len(approach) <= 2 else f"{len(approach)}条指令",
            "跳过（暂停）" if deposit is None else
            f"新增{len(deposit['created'])}/纠正{len(deposit['corrected'])}",
            reply.duration_s,
            (" 验证提问=" + verification_out["hypothesis_id"][:8])
            if verification_out else "",
        )
        return reply
