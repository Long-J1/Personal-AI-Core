# V0.5 集成：Pattern 三次范式（A/B 双源）+ Goal 流 + 提问单例（Mock，零云端）。
#
# 因果闭合沿用 D007 老办法：PatternMock 只看 prompt 里有没有规律段，
# 同一个问题在不同理解下产生不同回答——变化只能来自 Pattern 段的出现。
from __future__ import annotations

import json

import pytest

from core.conversation import ConversationService
from core.verifier import extract_goal_title
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.goal_store import GoalStore
from memory.hypothesis_store import HypothesisStore
from memory.pattern_store import PatternStore
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel


def _deposit(new=None, corrections=None):
    return json.dumps({"new": new or [], "corrections": corrections or [],
                       "event": None}, ensure_ascii=False)


class PatternMock(MockChatModel):
    """prompt 里有已验证规律段 → 引用规律作答；否则默认回答。"""

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
        if "=== 已验证规律" in system:
            return "按你的长期规律，建议把大任务拆开，并提前启动。"
        if "他的长期目标" in system:
            return "收到。后续建议会尽量和你的长期目标同向。"
        return "好的，知道了。"


@pytest.fixture
def flow(tmp_path):
    db = tmp_path / "flow.db"
    store = MemoryStore(db)
    ctx = ContextStore(db)
    chat = ChatStore(db)
    model = PatternMock(text_queue=[_deposit() for _ in range(12)])
    conv = ConversationService(store, ctx, model, chat_store=chat)
    hyp = HypothesisStore(db)
    pat = PatternStore(db)
    goal = GoalStore(db)
    return conv, model, hyp, pat, goal, ctx


def _seed_confirmed_pair(hyp, ctx):
    """两个 confirmed 同簇假设 + 3 独立源（晋升就绪态，用夹具直写，非聊天直出）。"""
    h1, _ = hyp.create("用户接受提前提醒甲", "interaction:提醒")
    for text, org in (("考试前提醒我甲", "sess:1"),
                      ("比赛前提醒我甲", "sess:2")):
        e, _ = ctx.add_entry("interaction", text, source="chat")
        hyp.add_evidence(h1.id, "support", "context", e.id, origin=org)
    hyp.confirm(h1.id)
    h2, _ = hyp.create("用户接受提前提醒乙", "interaction:提醒")
    e, _ = ctx.add_entry("interaction", "开会前提醒我乙", source="chat")
    hyp.add_evidence(h2.id, "support", "context", e.id, origin="sess:3")
    hyp.confirm(h2.id)
    return h1, h2


def _sys(model, idx):
    return model.calls[idx]["messages"][0]["content"]


# ---------- A 路径三次范式 ----------

def test_pattern_threetimes_paradigm(flow):
    """T1 无知 → T2 提问 → T3 确认 → T4 引用规律作答（行为变化只能来自 Pattern 段）。"""
    conv, model, hyp, pat, goal, ctx = flow
    _seed_confirmed_pair(hyp, ctx)
    q = "下周事情很多，怎么安排提醒"

    # T1：晋升还没跑（跑在沉淀后），prompt 无规律段 → 默认回答
    r1 = conv.chat(q)
    assert "=== 已验证规律" not in _sys(model, 0)
    assert r1.answer == "好的，知道了。"
    assert pat.count_live() == 1  # T1 写路径已晋升（tentative）

    # T2：相关话题 → 附带规律验证问题
    r2 = conv.chat("评审前提醒我")
    vq = r2.to_dict()["verification_question"]
    assert vq is not None and vq["kind"] == "pattern"
    assert "对吗？" in vq["text"]

    # T3：确认 → confirmed，prompt 当轮即变
    r3 = conv.chat("对的，就是这样")
    p = pat.list_patterns()[0]
    assert p.status == "confirmed"
    assert "[规律·" in _sys(model, 4)
    assert r3.to_dict()["verification_question"] is None

    # T4：同问 T1 → prompt 有规律段 → 回答引用规律
    r4 = conv.chat(q)
    assert "=== 已验证规律" in _sys(model, 6)
    assert "长期规律" in r4.answer
    assert len(r4.to_dict()["patterns_used"]) == 1


# ---------- B 路径：用户表达 → tentative → 验证 → confirmed ----------

def test_pattern_source_b_flow(flow):
    conv, model, hyp, pat, goal, ctx = flow
    r1 = conv.chat("我发现我一到截止前就熬夜")
    p = pat.list_patterns()[0]
    assert p.status == "tentative" and p.source == "user_declared"
    assert r1.to_dict()["verification_question"] is None  # 当轮不问自己刚说的话

    r2 = conv.chat("最近发现自己总是这样")
    vq = r2.to_dict()["verification_question"]
    assert vq is not None and vq["kind"] == "pattern"

    conv.chat("没错，就是这样")
    assert pat.get(p.id).status == "confirmed"
    r4 = conv.chat("截止前怎么安排")
    assert "=== 已验证规律" in _sys(model, 6)


def test_pattern_declare_markers():
    from core.verifier import is_pattern_declaration, pattern_scope_for
    assert is_pattern_declaration("我发现我一到截止前就熬夜") is True
    assert is_pattern_declaration("我总是提前做完") is True
    assert is_pattern_declaration("今天天气不错") is False
    assert pattern_scope_for("我发现我一到截止前就熬夜").startswith("user:")
    assert pattern_scope_for("嗯") == "user:自述"


# ---------- Goal 流 ----------

def test_goal_flow(flow):
    conv, model, hyp, pat, goal, ctx = flow
    assert extract_goal_title("我的目标是CSP提高组通过") == "CSP提高组通过"
    assert extract_goal_title("没标记的话") == "没标记的话"

    r1 = conv.chat("我的目标是CSP提高组通过")
    g = goal.list_goals()[0]
    assert g.status == "proposed"
    assert r1.to_dict()["verification_question"] is None  # 当轮不问

    r2 = conv.chat("CSP备考怎么安排")
    vq = r2.to_dict()["verification_question"]
    assert vq is not None and vq["kind"] == "goal"
    assert "CSP提高组通过" in vq["text"]

    conv.chat("没错，记上")
    assert goal.get(g.id).status == "active"
    r4 = conv.chat("下周怎么学")
    assert "他的长期目标" in _sys(model, 6)
    assert "CSP提高组通过" in _sys(model, 6)


def test_goal_no_inference(flow):
    """模型生成文本不许建 Goal（只有用户原话通道经过 conversation 检测）。"""
    conv, model, hyp, pat, goal, ctx = flow
    g, skip = goal.propose("", "某段模型生成的总结文本")
    assert g is None  # 空标题拒绝
    g, skip = goal.propose("模型总结的规律", "")
    assert g is None  # 无用户原话拒绝


# ---------- 提问单例与解决顺序 ----------

def test_single_pending_across_stores(flow):
    """hyp/pat/goal 各挂一个？不许：全局同时只问一个。"""
    conv, model, hyp, pat, goal, ctx = flow
    h, _ = hyp.create("用户接受提前提醒", "interaction:提醒")
    e, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    hyp.add_evidence(h.id, "support", "context", e.id)
    hyp.set_pending(h.id, "假设问题？")
    p, _ = pat.create("长期规律", "interaction:提醒", source="promotion")
    assert conv._pick_verification("提醒我开会") is None  # hyp 挂起挡住一切
    hyp.clear_pending(h.id)
    pat.set_pending(p.id, "规律问题？")
    assert conv._pick_verification("提醒我开会") is None  # pat 挂起同样挡
    pat.clear_pending(p.id)
    # set 动作消耗了冷却（asked+1/last_asked=now）：SQL 回拨后重验选择顺序
    import sqlite3
    con = sqlite3.connect(str(hyp.db_path))
    con.execute("UPDATE hypotheses SET last_asked_epoch=0 WHERE id=?",
                (h.id,))
    con.commit()
    con.close()
    got = conv._pick_verification("提醒我开会")
    assert got is not None and got[2].kind == "hypothesis"  # 假设优先


def test_resolve_order_hyp_first(flow):
    """双挂起（理论上不应发生）→ 先解决假设，规律不动。"""
    conv, model, hyp, pat, goal, ctx = flow
    h, _ = hyp.create("用户接受提前提醒", "interaction:提醒")
    e, _ = ctx.add_entry("interaction", "考试前提醒我", source="chat")
    hyp.add_evidence(h.id, "support", "context", e.id)
    hyp.set_pending(h.id, "假设问题？")
    p, _ = pat.create("长期规律", "interaction:提醒", source="promotion")
    pat.set_pending(p.id, "规律问题？")
    conv.chat("对的")
    assert hyp.get(h.id).status == "confirmed"
    assert pat.get(p.id).pending_question == "规律问题？"  # 本轮没动它


def test_pattern_scope_bigram_label():
    """B scope 取首关键词首个二连字（召回优先，不过严）。"""
    from core.verifier import pattern_scope_for
    assert pattern_scope_for("我发现我一到截止前就熬夜") == "user:发现"
    assert pattern_scope_for("我总是提前半小时到") == "user:提前"
    assert pattern_scope_for("嗯") == "user:自述"
