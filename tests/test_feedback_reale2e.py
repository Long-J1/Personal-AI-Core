"""真实模型反馈闭环（reale2e）：Context 的变化 → AI 行为的可观察变化。

对应用户验收要求："这个变化必须可以测试，而不是靠演示时口头说明。"
用真 Ollama 模型跑完整三步：
  ① 第一次面对该情境（解方程）→ 默认判断：直接给答案
  ② 用户纠正"以后先给提示" → 沉淀出 interaction 条目
  ③ 再次面对同类情境 → 行为变化：不泄露答案、只给提示
（Mock 版的确定性因果测试见 tests/test_conversation.py::test_feedback_scenario_*）

运行：python -m pytest tests -m reale2e -s
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.reale2e

from core.conversation import ConversationService
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.store import MemoryStore


@pytest.fixture(autouse=True)
def _require(require_ollama):
    pass


@pytest.fixture
def real_conversation(tmp_path, real_model):
    db = tmp_path / "feedback.db"
    store = MemoryStore(db)
    ctx = ContextStore(db)
    chat = ChatStore(db)
    return ConversationService(store, ctx, real_model, chat_store=chat), store, ctx


def _norm(text: str) -> str:
    """归一化回答文本：去空格、全角等号、LaTeX 包裹（\text{x} → x）。"""
    t = text.replace(" ", "").replace("＝", "=")
    for junk in ("\\text", "\\mathrm", "\\ ", "{", "}", "\\"):
        t = t.replace(junk, "")
    return t


def test_real_feedback_loop_changes_behavior(real_conversation):
    conv, store, ctx = real_conversation

    # ---- ① 第一次：无相处方式 → 默认判断（直接给答案）----
    r1 = conv.chat("帮我解方程 3x+7=22")
    assert r1.answer.strip(), "第一轮没有回答"
    assert all("提示" not in d for d in r1.approach), (
        f"第一轮不应已有提示类指令：{r1.approach}"
    )
    assert "x=5" in _norm(r1.answer), (
        f"第一轮应按默认判断直接给出答案，实际：{r1.answer}"
    )

    # ---- ② 用户纠正 → 沉淀出"相处方式"条目 ----
    conv.chat("不对，以后别直接给我答案，先给提示，我要自己想，卡住了才要答案")
    interaction = ctx.list_entries(kind="interaction")
    assert interaction, (
        "沉淀未从明确纠正中提取 interaction 条目："
        f"{[(e.kind, e.content) for e in ctx.list_entries()]}"
    )
    assert any("提示" in e.content or "答案" in e.content for e in interaction), (
        f"interaction 条目内容未抓住纠正要点：{[e.content for e in interaction]}"
    )

    # ---- ③ 再次面对同类情境 → 行为可观察地变化 ----
    r3 = conv.chat("帮我解方程 5x-3=12")
    assert any("提示" in d or "答案" in d for d in r3.approach), (
        f"第二轮判断未携带相处方式指令：{r3.approach}"
    )
    assert "x=3" not in _norm(r3.answer), (
        f"已纠正为'先给提示'，但仍直接泄露了答案：{r3.answer}"
    )
    assert any(w in r3.answer for w in ("提示", "试试", "自己", "先")), (
        f"第二轮回答不含任何提示性表达：{r3.answer}"
    )
