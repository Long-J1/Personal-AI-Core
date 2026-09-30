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

from memory.hypothesis_store import _is_explicit_denial
from memory.context_store import (
    KINDS,
    KIND_WEEKLY_CAP,
    RECENT_DAYS,
    ContextEntry,
    ContextStore,
)
from memory.event import Event
from memory.store import MemoryStore
from models.base import BaseModelAdapter, ModelError

from .config import Settings, settings as default_settings
from .context import extract_keywords
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

# ---------- V0.5.1 可靠性：规则兜底（不依赖模型输出）----------
# 疑问标记：出现即可能是提问；必须同时"无依据"才拦截新建
# （有依据的提问如"你觉得我最近状态怎么样"照常沉淀）。
QUESTION_MARKERS = ("?", "？", "吗", "呢", "么", "谁", "什么", "怎么",
                    "怎样", "如何", "哪", "几", "多少", "是不是", "对不对",
                    "好不好", "能否", "是否")


def _keywords(text: str) -> set[str]:
    """整词关键词（无依据门用：严格，防单字巧合蒙混过关）。"""
    return {k for k in extract_keywords(text or "") if len(k) >= 2}


def _match_grams(text: str) -> set[str]:
    """关键词 + 其二连字展开（兜底关联用）。

    整串匹配太严（"效率很高"对不上"效率高"），二连字保证召回；
    调用方用数量阈值防单字巧合误伤。
    """
    out: set[str] = set()
    for k in extract_keywords(text or ""):
        if len(k) < 2:
            continue
        out.add(k)
        out.update(k[i:i + 2] for i in range(len(k) - 1))
    return out


def _is_ungrounded_question(user_text: str, entries,
                            store) -> bool:
    """无依据提问：是问句，且与现有理解/近期事件零关键词交集。

    此时用户没教给系统任何新东西（如"弟弟叫什么"），只会产生
    "正在询问X"这类噪音理解——拦截新建，但纠正通道照常。
    """
    if not any(m in (user_text or "") for m in QUESTION_MARKERS):
        return False
    msg_kws = _keywords(user_text)
    corpus = [e.content for e in (entries or [])]
    if store is not None:
        try:
            corpus.extend(
                e.description for e in store.list_events(limit=20))
        except Exception:
            pass
    for text in corpus:
        if msg_kws & _keywords(text):
            return False
    return True


# ---------- V0.5.2 防火墙：来源分类（确定性，不调模型）----------
# 优先级：硬拦截 > 用户原话 > 推断降级/advisory丢弃 > 兜底 inferred。
INFER_CONNECTORS = ("说明了", "这说明", "意味着", "表明", "看来", "反映出",
                    "可见", "看出来", "说明")
# AI 定语动词：含这些词 + 指向 AI 行为 = 用户对 AI 的要求（正常走）；
# 否则泛化建议直接丢弃（advisory，连表都不建）。
AI_DIRECTED_VERBS = ("提醒", "回答", "记住", "理解", "告诉")
# 硬拦截：过去时断言（无证据时）/ 性格评价。日期人名走专用探测。
PAST_CLAIM_PATTERNS = ("你之前说过", "上次你提到", "你说过", "之前说过",
                       "上次提到", "以前说过")
# 性格评价句式 → 系统生成嫌疑，拒绝（"你/用户是个…的人" 或 程度词+性格字）。
# 模型主语多为"用户"，只列"你很X"会漏，故用词表 + 句式双判。
PERSONALITY_WORDS = ("很懒", "很笨", "很聪明", "很乖",
                     "真懒", "真笨", "性格是")
# 硬否决词：指向事实性错误 → rejected；其余显式否决（别/不要/纠正）
# 属行为纠偏 → outdated（软否认）。子集须 ⊆ hypothesis_store.DENY_KEYWORDS。
HARD_DENY_WORDS = ("不对", "不是", "没有", "错了", "胡说")
_DATE_CHARS = "年月日号"
_DIGIT_CHARS = "0123456789一二三四五六七八九十百千万"
_WEEK_TAIL = "一二三四五六日天末"
_NAME_MARKERS = ("名叫", "名字是", "叫")
_PUNCT = ("，", "。", "、", "；", "：", "！", "？", "…", "—", "·",
          "（", "）", "(", ")", "[", "]", "【", "】")
_PRONOUN_PREFIX = ("用户", "咱们", "我们", "大家", "你", "我", "他", "她",
                   "它")


def _norm_text(text: str) -> str:
    t = text or ""
    for p in _PUNCT:
        t = t.replace(p, "")
    t = "".join(t.split())
    changed = True
    while changed:
        changed = False
        for pre in _PRONOUN_PREFIX:
            if t.startswith(pre) and len(t) > len(pre) + 1:
                t = t[len(pre):]
                changed = True
                break
    return t


def _split_clauses(content: str) -> list[str]:
    # 混合句拆散分类，防搭车；最长优先防部分重叠。
    parts, buf, i = [], "", 0
    text = content or ""
    cons = sorted(INFER_CONNECTORS, key=len, reverse=True)
    while i < len(text):
        hit = None
        for c in cons:
            if text.startswith(c, i):
                hit = c
                break
        if hit:
            if buf.strip():
                parts.append(buf.strip())
            buf = hit
            i += len(hit)
        else:
            buf += text[i]
            i += 1
    if buf.strip():
        parts.append(buf.strip())
    # 分句残留的尾巴标点剥掉："用户说考试前提醒，" → "用户说考试前提醒"
    # （切分副产品，不属于记忆内容；下游分类/入库直接用干净子句）
    cleaned = [p.rstrip("，,。、；;：:！!？? \t") for p in parts]
    cleaned = [p for p in cleaned if p]
    return cleaned or [content]


def _find_date_spans(text: str) -> list[str]:
    """提取精确日期 token（"9月30日"/"30日"/"下周三"?→"周[尾]"）。

    窗口式提取会把上下文（如"用户"）框进探针，导致用户原话里的日期
    也对不上——这里只收日期本体，且必须含数字（防"日子/号码"误报）。
    """
    out = []
    t = text or ""

    def digits_before(pos: int) -> int:
        lo = pos
        while lo > 0 and t[lo - 1] in _DIGIT_CHARS:
            lo -= 1
        return lo

    for i, ch in enumerate(t):
        token = None
        if ch in ("月", "号"):
            lo = digits_before(i)
            hi = i + 1
            if ch == "月":
                while hi < len(t) and t[hi] in _DIGIT_CHARS:
                    hi += 1
                if hi < len(t) and t[hi] in "日号":
                    hi += 1
            token = t[lo:hi]
        elif ch == "日":
            lo = digits_before(i)
            if lo < i:
                token = t[lo:i + 1]
        elif ch == "周" and i + 1 < len(t) and t[i + 1] in _WEEK_TAIL:
            token = t[digits_before(i):i + 2]
        if (token and len(token) >= 2 and token not in out
                and any(d in token for d in _DIGIT_CHARS)):
            out.append(token)
    return out


def _find_name_probes(text: str) -> list[str]:
    out = []
    t = text or ""
    for m in sorted(_NAME_MARKERS, key=len, reverse=True):
        idx = t.find(m)
        while idx >= 0:
            out.append(t[max(0, idx - 6):idx + 6])
            idx = t.find(m, idx + 1)
    return out


def _has_db_evidence(text: str, entries, store) -> bool:
    kws = _keywords(text)
    if not kws:
        return False
    corpus = [e.content for e in (entries or [])]
    if store is not None:
        try:
            corpus.extend(
                e.description for e in store.list_events(limit=20))
        except Exception:
            pass
    for line in corpus:
        if kws & _keywords(line):
            return True
    return False


def _is_db_grounded(text: str, entries, store) -> bool:
    """条目内容在既有理解/事件里有据可查（共享≥2 个二连字）。

    有据复述不是推测："CSP最近状态怎么样"→"CSP备考顺利"这类有依据
    提问的沉淀必须留在 memory（V0.5.1 保证），否则被防火墙误伤。
    """
    kws = _match_grams(text)
    if len(kws) < 2:
        return False
    corpus = [e.content for e in (entries or [])]
    if store is not None:
        try:
            corpus.extend(
                e.description for e in store.list_events(limit=20))
        except Exception:
            pass
    for line in corpus:
        if len(kws & _match_grams(line)) >= 2:
            return True
    return False


def _probes_have_source(probes, user_text, entries, store) -> bool:
    if not probes:
        return False
    hay = [user_text or ""]
    hay.extend(e.content or "" for e in (entries or []))
    if store is not None:
        try:
            hay.extend(e.description or ""
                       for e in store.list_events(limit=20))
        except Exception:
            pass
    big = chr(10).join(hay)
    return any(p in big for p in probes)


def _is_personality_judgment(text: str) -> bool:
    if any(w in text for w in PERSONALITY_WORDS):
        return True
    return "是个" in text and "的人" in text


def classify_source(content, user_text, entries, store, kind=""):
    # 返回 (grade, reason)：user_direct / inferred / advisory / reject。
    text = (content or "").strip()
    if not text:
        return "reject", "内容为空"
    # 分句残留的尾巴标点（"用户说考试前提醒，"）剥掉再判——
    # 这是 _split_clauses 切分的副产品，不属于记忆内容
    text = text.rstrip("，,。、；;：:！!？? \t")
    if not text:
        return "reject", "内容为空"
    if (any(p in text for p in PAST_CLAIM_PATTERNS)
            and not _has_db_evidence(text, entries, store)):
        return "reject", "无证据的过去时断言"
    # 硬拦截：既往事件断言（上次/之前 + 发生/做过/去过/见过）无证据
    if (("上次" in text or "之前" in text)
            and any(w in text for w in ("发生", "做过", "去过", "见过"))
            and not _has_db_evidence(text, entries, store)):
        return "reject", "无证据的既往事件断言"
    probes = _find_date_spans(text) + _find_name_probes(text)
    if probes and not _probes_have_source(probes, user_text, entries,
                                          store):
        return "reject", "无出处的具体日期/人名"
    if _is_personality_judgment(text):
        return "reject", "性格评价（system_generated 嫌疑）"
    if ("你应该" in text or "你们应该" in text) and not any(
            v in text for v in AI_DIRECTED_VERBS):
        return "advisory", "泛化建议，非记忆内容"
    norm_item, norm_user = _norm_text(text), _norm_text(user_text)
    if norm_item and norm_item in norm_user:
        return "user_direct", None
    # 复述改写（"用户不吃香菜" vs "我不吃香菜"）靠二连字召回；
    # 含推断连接词的不在此列（走 inferred），防搭车。
    # 阈值按 kind 分：interaction 本就是"用户对 AI 的要求"，先验更高取宽松线；
    # 其余取严格线。硬拦截照常先行，不受阈值影响。
    # （数量门同样分档：interaction 允许单主题词命中如"简短"；
    #  其余 kind 要求≥2，防单字巧合。）
    if not any(c in text for c in INFER_CONNECTORS):
        try:
            gi = ContextStore._bigrams(norm_item)
            gu = ContextStore._bigrams(norm_user)
            shared = gi & gu
            denom = min(len(gi), len(gu))
            # interaction 放宽到 0.2：D007 场景实测共享 4/16=0.25 刚好压线，
            # 其余 kind 保持严格线防误判
            bar = 0.2 if kind == "interaction" else 0.4
            need = 1 if kind == "interaction" else 2
            if len(shared) >= need and denom and len(shared) / denom >= bar:
                return "user_direct", None
        except Exception:
            pass
    # 既有理解的复述：有据可查 → 可进 memory（不冒充用户原话）
    if (not any(c in text for c in INFER_CONNECTORS)
            and _is_db_grounded(text, entries, store)):
        return "db_grounded", None
    return "inferred", None


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
        hypothesis_store=None,  # V0.3：纠正→假设联动；None 即关闭（注入兼容）
        pattern_store=None,  # V0.5.2：纠正→规律级联；None 即关闭（注入兼容）
    ):
        self.ctx = context_store
        self.store = memory_store
        self.model = model
        self.settings = settings or default_settings
        self.hyp = hypothesis_store
        self.pat = pattern_store

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
        """提取 + 应用。失败返回带 error 的结果，绝不抛异常打断对话。

        可靠性顺序：纠正永远先行（模型/规则任一通道）；新建理解与事件
        只在"有依据"时进行（无依据提问只读不写）；显式否决走规则兜底，
        不依赖模型 JSON 是否合法。
        """
        result = DepositResult()
        grounded = not _is_ungrounded_question(
            user_text, entries, self.store)
        if not grounded:
            log.info("无依据提问，本轮只处理纠正、不新建理解/事件：%s",
                     (user_text or "")[:40])
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
            self._fallback_denial(user_text, answer, entries, result)
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
            self._fallback_denial(user_text, answer, entries, result)
            return result

        alias_map = {f"#{i}": e for i, e in enumerate(entries, start=1)}
        self._apply_corrections(data, alias_map, result, user_text)
        if grounded:
            self._apply_creates(data, result, user_text, entries)
            self._apply_event(data, result)
        else:
            # 无依据提问门：new[] 全数拦下，动作同样可审计（firewall_reject）
            for item in (data.get("new") or []):
                if isinstance(item, dict) and item.get("content"):
                    log.info("firewall_reject [无依据提问门]：%s",
                             str(item["content"])[:60])
        self._fallback_denial(user_text, answer, entries, result)
        return result

    def _fallback_denial(self, user_text: str, answer: str,
                         entries, result: DepositResult) -> None:
        """规则兜底纠正：显式否决 + 本轮无纠正生效 → 按关键词直连反证。

        不依赖模型 JSON（解析失败/模型没提取时仍能工作）；
        已有纠正生效时不重复处罚。候选条目限定为本轮进入 prompt 的理解，
        关联文本 = 用户原话 + 本轮回答（回答复述了依据时能对上"这样"类指代）。
        V0.5.2（D017 ①）：命中条目直接标 outdated/rejected（不断根），
        硬否决（不对/不是/没有/错了/胡说）→ rejected，其余 → outdated。
        """
        if result.corrected:
            return
        if not _is_explicit_denial(user_text or ""):
            return
        pool_kws = _match_grams(
            str(user_text or "") + "\n" + str(answer or ""))
        if not pool_kws:
            log.warning("显式否决但无关键词可关联（跳过兜底）：%s",
                        (user_text or "")[:40])
            return
        hard = any(w in str(user_text or "") for w in HARD_DENY_WORDS)
        hit_any = False
        for e in entries or []:
            entry_kws = _match_grams(e.content)
            shared = pool_kws & entry_kws
            # 阈值 2 个二连字（单字巧合如"是我"不够）；超短条目按自身放宽
            need = min(2, len(entry_kws)) if entry_kws else 2
            if len(shared) < need:
                continue
            marked = False
            try:
                # V0.5.2：先标记理解本身——这是"纠正不断根"的主语
                marked = self.ctx.set_memory_status(
                    e.id, "rejected" if hard else "outdated",
                    reason="user_correction")
            except Exception as exc:
                log.warning("兜底标记失败（%s）：%s", e.id[:8], exc)
            if marked:
                hit_any = True
                log.info("规则兜底标记理解 %s：%s",
                         e.id[:8], "rejected" if hard else "outdated")
            if self.hyp is not None:
                try:
                    hits = self.hyp.record_correction_hit(
                        e.id, user_text, "规则兜底：显式否决")
                except Exception as exc:
                    log.warning("兜底联动失败（%s）：%s", e.id[:8], exc)
                    hits = []
                if hits:
                    hit_any = True
                    log.info("规则兜底纠正命中假设：%s",
                             [h[:8] for h in hits])
            # V0.5.2（D017 ②）：规律级联（引用该条目的规律吃反证）
            self._cascade_pattern("context", e.id, "规则兜底：显式否决")
        if not hit_any:
            log.warning("显式否决但本轮理解均无关联（跳过兜底）：%s",
                        (user_text or "")[:40])

    # ---------- 应用：先纠正，后新增 ----------
    def _apply_corrections(
        self,
        data: dict,
        alias_map: dict[str, ContextEntry],
        result: DepositResult,
        user_text: str = "",
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
                # V0.3：纠正→假设联动（以该条目为证据的假设吃反证/被否决）
                if self.hyp is not None:
                    try:
                        hit = self.hyp.record_correction_hit(
                            target.id, user_text, reason)
                        if hit:
                            log.info("纠正联动假设：%s", [h[:8] for h in hit])
                    except Exception as exc:  # 联动失败不影响沉淀本身
                        log.warning("纠正联动假设失败（跳过）：%s", exc)
                # V0.5.2（D017 ②）：纠正→规律级联（引用该条目的规律吃反证）
                self._cascade_pattern(
                    "context", target.id, reason or user_text[:200])

    def _cascade_pattern(self, source_type: str, source_id: str,
                         reason: str) -> None:
        """纠正级联到规律；pat 未注入或异常一律跳过，绝不影响主流程。"""
        if self.pat is None:
            return
        try:
            self.pat.cascade_from_source(source_type, source_id, reason)
        except Exception as exc:
            log.warning("纠正级联到规律失败（跳过）：%s", exc)

    def _apply_creates(self, data: dict, result: DepositResult,
                         user_text: str = "", entries=None) -> None:
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
            # V0.5.2 防火墙：逐子句分类，混合句拆散处理
            direct_parts, grounded_parts, infer_parts = [], [], []
            for clause in _split_clauses(content):
                grade, why = classify_source(clause, user_text, entries,
                                             self.store, kind)
                if grade == "reject":
                    log.info("firewall_reject [%s]：%s", why, clause[:40])
                elif grade == "advisory":
                    log.info("泛化建议丢弃：%s", clause[:40])
                elif grade == "user_direct":
                    direct_parts.append(clause)
                elif grade == "db_grounded":
                    grounded_parts.append(clause)
                else:
                    infer_parts.append(clause)
            # 写 memory 的两档：用户原话 0.95 / 既有理解复述 0.8
            for grade, parts in (("user_direct", direct_parts),
                                 ("db_grounded", grounded_parts)):
                if not parts or budget <= 0:
                    continue
                # P1：同 kind 近期新增打满配额 → 只合并模式（绝不新建）
                throttled = self.ctx.count_recent_by_kind(
                    kind, days=RECENT_DAYS) >= KIND_WEEKLY_CAP
                entry, skip = self.ctx.add_or_merge(
                    kind,
                    "；".join(parts),
                    source="chat",
                    max_chars=self.settings.deposit_max_chars,
                    merge_only=throttled,
                    source_grade=grade,
                    confidence=0.95 if grade == "user_direct" else 0.8,
                )
                if entry is None:
                    log.debug("新增跳过：%s", skip)
                else:
                    budget -= 1
                    # 记录落库后的实际内容，前后端一致
                    result.created.append(
                        {"id": entry.id, "kind": kind,
                         "content": entry.content}
                    )
            if infer_parts and self.hyp is not None and budget > 0:
                hypo_text = "；".join(infer_parts)
                try:
                    hypo, _ = self.hyp.create(
                        hypo_text, f"inferred:{kind}",
                        detail={"rule": "firewall"})
                except Exception as exc:
                    log.warning("推测转假设失败（跳过）：%s", exc)
                    continue
                if hypo is None:
                    continue
                budget -= 1
                log.info("推测转假设待验证：%s", hypo_text[:40])

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
