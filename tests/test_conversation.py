"""对话通道 + Personal Context 读写闭环测试（V0.2 A2/A3/A4/A5）。

最关键的测试是 test_feedback_scenario——你要求的可重复验证反馈场景：
  第一次面对同类情境 → AI 默认判断 → 用户纠正 → Context 更新
  → 再次遇到同类情境 → 判断可观察地变化（approach、提示词、回答三处都断言）。
其中回答的变化由 BehaviorMock 从**提示词**推导，而不是脚本顺序编排——
即测试证明的是"是 Context 的变化导致了行为变化"，因果关系在测试里闭合。
"""
from __future__ import annotations

import json

import pytest

from core.conversation import ConversationService
from memory.context_store import ContextStore
from memory.chat_store import ChatStore
from memory.store import MemoryStore
from models.base import ModelUnavailableError
from models.mock_adapter import MockChatModel


def deposit_json(new=(), corrections=(), event=None) -> str:
    return json.dumps(
        {"new": list(new), "corrections": list(corrections), "event": event},
        ensure_ascii=False,
    )


def answer_prompts(mock: MockChatModel) -> list[str]:
    """所有对话回答调用的系统提示词（排除沉淀调用）。"""
    return [
        c["messages"][0]["content"]
        for c in mock.calls
        if c["messages"] and "记忆沉淀模块" not in str(c["messages"][0]["content"])
    ]


def directive_section(prompt: str) -> str:
    """系统提示词中【相处方式】那一段（不含后面其他段）。"""
    after = prompt.split("=== 相处方式", 1)[1]
    return after.split("=== 近期发生的事", 1)[0]


class BehaviorMock(MockChatModel):
    """"真的按提示词指令改变行为"的模型。

    系统提示里存在相处方式指令 → 只给提示不给答案；不存在 → 直接给答案。
    沉淀调用按队列出预置 JSON。这样：同一个用户问题在不同 Context 下
    产生不同回答，**因果上只能来自提示词的变化**。
    """

    DIRECTIVE = "给提示而不是直接答案"

    def chat(self, messages, *, images=None, timeout=None):
        if images:
            return super().chat(messages, images=images, timeout=timeout)
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append({"images": False, "messages": messages})
        system = str(messages[0].get("content", "")) if messages else ""
        if "记忆沉淀模块" in system:
            if self.text_queue:
                return self.text_queue.pop(0)
            return json.dumps({"new": [], "corrections": [], "event": None})
        if self.DIRECTIVE in system:
            return "给你一个提示：先把常数项移到等号右边，再自己算算看。"
        return "这道题的答案是 42。"


# ---------------------------------------------------------------- A2 读写闭环

def test_context_read_shapes_prompt_and_write_lands(
    conversation, context_store, mock_model
):
    # 轮1：透露信息 → 沉淀写入
    mock_model.text_queue = [
        "收到，我记下了。",
        deposit_json(new=[{"kind": "profile", "content": "用户是高二学生"}]),
    ]
    r1 = conversation.chat("我是高二学生")
    assert r1.deposit and len(r1.deposit["created"]) == 1
    assert context_store.count_active() == 1

    # 轮2：读侧生效——上一轮写入的理解进了提示词
    mock_model.text_queue = ["那最近学业忙吗？", deposit_json()]
    r2 = conversation.chat("最近有点累")
    prompts = answer_prompts(mock_model)
    assert "用户是高二学生" in prompts[1]
    # 参与过判断的条目被记了使用次数（Context 真被消费，不是摆设）
    assert r2.context_used and r2.context_used[0]["used_count"] >= 1


def test_conversation_can_feed_event_memory(conversation, store, mock_model):
    """对话也是感知：值得记的事走同一个事件记忆管线（范围条目1）。"""
    mock_model.text_queue = [
        "听起来强度不小。",
        deposit_json(event={
            "description": "用户刚跑完5公里",
            "activity": "跑步", "importance": 0.4,
        }),
    ]
    r = conversation.chat("我刚跑完5公里，累死了")
    assert r.deposit["event_id"]
    assert store.count_events() == 1
    assert [e.description for e in store.search_events(["跑步"])] == ["用户刚跑完5公里"]


def test_duplicate_statements_not_duplicated(
    conversation, context_store, mock_model
):
    mock_model.text_queue = [
        "好的。", deposit_json(new=[{"kind": "fact", "content": "用户不吃香菜"}]),
        "记住了。", deposit_json(new=[{"kind": "fact", "content": "用户不吃香菜"}]),
    ]
    conversation.chat("我不吃香菜")
    conversation.chat("再说一遍，我不吃香菜")
    assert context_store.count_active() == 1


def test_deposit_garbage_skipped_gracefully(
    conversation, context_store, mock_model
):
    mock_model.text_queue = ["正常回答。", "这不是JSON"]
    r = conversation.chat("你好")
    assert r.answer == "正常回答。"
    assert r.deposit and r.deposit["error"]
    assert context_store.count_active() == 0


def test_model_error_propagates(conversation, mock_model):
    mock_model.fail_with = ModelUnavailableError("连接失败")
    with pytest.raises(ModelUnavailableError):
        conversation.chat("你好")


# ------------------------------------------------ 核心：反馈场景（Context 改变行为）

def test_feedback_scenario_context_changes_judgment(conversation, context_store):
    """可重复验证的反馈闭环（用户验收要求，勿删）：

    ① 第一次面对该情境：AI 默认判断 → 直接给答案
    ② 用户纠正 → Personal Context 新增"相处方式"条目（带变更日志）
    ③ 再次遇到同类情境（同一句用户话）→ 判断变化：
       approach 变了、系统提示词变了、回答变了——三处都断言。
    """
    mock = BehaviorMock()
    conversation.model = mock
    conversation.depositor.model = mock

    # ---- ① 第一次：无相处方式 → 默认判断 ----
    mock.text_queue = [deposit_json()]                      # 沉淀：无新理解
    r1 = conversation.chat("帮我看看这道题怎么做？")

    assert r1.approach == ["default"]
    assert r1.answer == "这道题的答案是 42。"                # 默认：直接给答案
    section1 = directive_section(answer_prompts(mock)[0])
    assert "（暂无）" in section1                            # 指令段是空的
    assert BehaviorMock.DIRECTIVE not in section1

    # ---- ② 用户纠正 → Context 更新 ----
    mock.text_queue = [
        deposit_json(new=[{
            "kind": "interaction",
            "content": "给提示而不是直接答案，等用户要再揭晓",
        }]),
    ]
    r2 = conversation.chat("以后别直接给我答案，先给提示，我要自己想")

    interaction = context_store.list_entries(kind="interaction")
    assert len(interaction) == 1
    assert r2.deposit["created"][0]["kind"] == "interaction"
    assert context_store.history()[0]["action"] == "create"  # 变更可查

    # ---- ③ 再次遇到同类情境：同一句话，判断可观察地变化 ----
    mock.text_queue = [deposit_json()]
    r3 = conversation.chat("帮我看看这道题怎么做？")

    # 判断（approach）变了
    assert r3.approach != r1.approach
    assert r3.approach == ["给提示而不是直接答案，等用户要再揭晓"]
    # 提示词变了（指令真的进入了 System Prompt）
    section3 = directive_section(answer_prompts(mock)[-1])
    assert "给提示而不是直接答案" in section3
    assert "（暂无）" not in section3
    # 回答变了（且由提示词推导，不是脚本编排：同模型、同输入、不同 Context）
    assert r3.answer != r1.answer
    assert "42" not in r3.answer
    assert "提示" in r3.answer


def test_correction_takes_effect_immediately(
    conversation, context_store, mock_model
):
    """A3：纠正 → 同一会话后续回答用新值，旧值不再进提示词。"""
    mock_model.text_queue = [
        "收到。",
        deposit_json(new=[{"kind": "profile", "content": "用户是高二学生"}]),
    ]
    conversation.chat("我是高二学生")

    mock_model.text_queue = [
        "了解了，已更正。",
        deposit_json(corrections=[{
            "ref": "#1", "content": "用户是高一学生", "reason": "刚才说错了",
        }]),
    ]
    r2 = conversation.chat("不对，我刚才记错了，其实我是高一的")
    assert r2.deposit["corrected"][0]["content"] == "用户是高一学生"

    mock_model.text_queue = ["你读高一。", deposit_json()]
    conversation.chat("我读几年级？")

    sys_prompt = answer_prompts(mock_model)[-1]
    assert "用户是高一学生" in sys_prompt      # 新值生效
    assert "用户是高二学生" not in sys_prompt  # 旧值即刻失效
    # 历史可查：纠正记录里保留着旧内容
    rec = [h for h in context_store.history() if h["action"] == "correct"]
    assert rec and rec[0]["content_before"] == "用户是高二学生"


def test_delete_entry_no_longer_shapes_future_answers(
    conversation, context_store, mock_model
):
    """A4：删除即失效——后续对话的提示词不再包含它。"""
    entry, _ = context_store.add_entry("interaction", "回答末尾永远加一句鼓励")
    mock_model.text_queue = ["加油，你可以的！", deposit_json()]
    r1 = conversation.chat("给我点信心")
    assert "永远加一句鼓励" in answer_prompts(mock_model)[0]
    assert r1.approach == ["回答末尾永远加一句鼓励"]

    context_store.delete_entry(entry.id)

    mock_model.text_queue = ["好的，换个话题。", deposit_json()]
    r2 = conversation.chat("聊聊别的")
    assert "永远加一句鼓励" not in answer_prompts(mock_model)[1]
    assert r2.approach == ["default"]           # 指令消失 → 判断回到默认


def test_pause_blocks_writes_but_still_answers(
    conversation, store, context_store, chat_store, mock_model
):
    """A5：暂停后不观察、不沉淀；回答仍给出；恢复后一切复原。"""
    store.paused = True
    mock_model.text_queue = ["（这是回答）"]     # 不应有第二次调用
    r = conversation.chat("你好")

    assert r.answer == "（这是回答）" and r.paused is True
    assert r.deposit is None                     # 未沉淀
    assert context_store.count_active() == 0     # 理解没写入
    assert chat_store.count_messages() == 0      # 消息没持久化
    assert len(mock_model.calls) == 1            # 只有回答，没有沉淀调用

    store.paused = False
    mock_model.text_queue = [
        "记住了。",
        deposit_json(new=[{"kind": "profile", "content": "用户喜欢猫"}]),
    ]
    conversation.chat("我喜欢猫")
    assert context_store.count_active() == 1
    assert chat_store.count_messages() == 2


def test_session_history_persists_across_chats(conversation, chat_store, mock_model):
    """同一会话多轮历史持久化（A3 的"同一会话"前提）。"""
    mock_model.text_queue = ["1", deposit_json(), "2", deposit_json()]
    r1 = conversation.chat("第一句")
    r2 = conversation.chat("第二句", session_id=r1.session_id)
    assert r1.session_id == r2.session_id
    assert chat_store.count_messages(r1.session_id) == 4
    history = chat_store.history(r1.session_id, order="asc")
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
