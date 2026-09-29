# V0.5 Goal 存储：用户来源铁律 + 状态流转 + 关联计算（fixture 库）。
from __future__ import annotations

import pytest

from memory.context_store import ContextStore
from memory.goal_store import GoalStore
from memory.pattern_store import PatternStore


@pytest.fixture
def stores(tmp_path):
    db = tmp_path / "g.db"
    return (GoalStore(db), PatternStore(db), ContextStore(db))


def test_propose_and_validation(stores):
    goal, _, _ = stores
    g, skip = goal.propose("CSP提高组通过", "我的目标是CSP提高组通过")
    assert skip is None and g.status == "proposed"
    assert goal.propose("", "我的目标是X")[0] is None  # 空标题
    assert goal.propose("标题", "")[0] is None  # 无用户原话不建
    g2, skip2 = goal.propose("CSP提高组通过", "我的目标是CSP提高组通过")
    assert g2 is None and skip2 is not None  # 去重


def test_status_flow(stores):
    goal, _, _ = stores
    g, _ = goal.propose("每天跑步", "我想成为每天跑步的人")
    assert goal.confirm(g.id) is True
    assert goal.get(g.id).status == "active"
    assert goal.pause(g.id) is True
    assert goal.get(g.id).status == "paused"
    assert goal.confirm(g.id) is True  # 恢复即确认回 active
    assert goal.complete(g.id) is True
    assert goal.get(g.id).status == "done"


def test_reject_delete_restore(stores):
    goal, _, _ = stores
    g, _ = goal.propose("学钢琴", "我的目标是学钢琴")
    assert goal.reject(g.id, reason="改主意了") is True
    assert goal.get(g.id).status == "rejected"
    g2, _ = goal.propose("学画画", "我的目标是学画画")
    assert goal.delete_entry(g2.id) is True
    assert goal.restore_entry(g2.id) is True
    assert goal.get(g2.id).status == "proposed"


def test_pending_singleton(stores):
    goal, _, _ = stores
    g1, _ = goal.propose("目标一", "我的目标是目标一")
    g2, _ = goal.propose("目标二", "我的目标是目标二")
    assert goal.set_pending(g1.id, "确认吗？") == (True, None)
    assert goal.set_pending(g2.id, "确认吗？")[0] is False
    assert goal.get_pending().id == g1.id
    # 确认即清挂起
    goal.confirm(g1.id)
    assert goal.get_pending() is None


def test_association_links(stores):
    """关联展示：按关键词匹配 Pattern/Context，无推理。"""
    goal, pat, ctx = stores
    p, _ = pat.create("用户长期存在跑步方面的稳定规律", "activity:跑步",
                      source="promotion")
    pat.confirm(p.id)
    ctx.add_entry("fact", "用户每天晨跑3公里", source="chat")
    g, _ = goal.propose("每天跑步", "我想成为每天跑步的人")
    d = goal.get_detail(g.id, pattern_store=pat, context_store=ctx)
    assert p.id in d["linked_pattern_ids"]
    assert d["history"] and d["history"][0]["action"] == "propose"
    # 无 store 也能读（关联为空）
    d2 = goal.get_detail(g.id)
    assert d2["linked_pattern_ids"] == []


def test_clear_all(stores):
    goal, _, _ = stores
    goal.propose("目标一", "我的目标是目标一")
    assert goal.clear_all() == 1
    assert goal.list_goals(live_only=False) == []
