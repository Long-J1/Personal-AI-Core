"""Personal Context 存储测试（V0.2 A1/A3/A4：增、纠、删、恢复、日志、去重）。"""
from __future__ import annotations

from memory.context_store import ContextStore


def test_add_and_list_with_source_and_time(context_store: ContextStore):
    entry, err = context_store.add_entry("profile", "用户是高二学生", source="chat")
    assert err is None and entry is not None

    items = context_store.list_entries()
    assert len(items) == 1
    d = items[0].to_public_dict()
    assert d["content"] == "用户是高二学生"
    assert d["source"] == "chat"           # A1：带来源
    assert d["created_at"] and d["date"]   # A1：带时间
    assert d["kind_label"] == "画像"


def test_invalid_kind_rejected(context_store: ContextStore):
    entry, err = context_store.add_entry("banana", "x")
    assert entry is None and err is not None


def test_duplicate_content_skipped(context_store: ContextStore):
    context_store.add_entry("fact", "用户不吃香菜")
    entry, err = context_store.add_entry("fact", "用户不吃香菜")
    assert entry is None and err == "已存在相同理解"
    assert context_store.count_active() == 1


def test_correct_updates_content_and_keeps_history(context_store: ContextStore):
    entry, _ = context_store.add_entry("profile", "用户是高二学生")
    ok = context_store.correct_entry(
        entry.id, "用户是高一学生", reason="用户说刚才记错了"
    )
    assert ok

    now = context_store.get_entry(entry.id)
    assert now.content == "用户是高一学生"

    history = context_store.history()
    assert len(history) == 2          # create + correct
    h = [x for x in history if x["action"] == "correct"][0]
    assert h["content_before"] == "用户是高二学生"  # 旧内容可查
    assert h["content_after"] == "用户是高一学生"
    assert "刚才记错了" in (h["detail"] or "")


def test_correct_nonexistent_fails(context_store: ContextStore):
    assert not context_store.correct_entry("nope", "内容")


def test_delete_makes_entry_disappear_from_active_list(context_store: ContextStore):
    entry, _ = context_store.add_entry("interaction", "先给提示再给答案")
    assert context_store.delete_entry(entry.id)
    assert context_store.list_entries() == []          # 立即失效
    assert context_store.get_entry(entry.id).active is False  # 记录还在（可恢复）
    assert context_store.history()[0]["action"] == "delete"
    assert not context_store.delete_entry(entry.id)    # 不能重复删


def test_restore_bring_back(context_store: ContextStore):
    entry, _ = context_store.add_entry("dynamic", "正在准备期中考试")
    context_store.delete_entry(entry.id)
    assert context_store.restore_entry(entry.id)
    assert len(context_store.list_entries()) == 1
    assert context_store.history()[0]["action"] == "restore"


def test_mark_used_counts_participation(context_store: ContextStore):
    a, _ = context_store.add_entry("profile", "喜欢简洁回答")
    b, _ = context_store.add_entry("fact", "学校在城东")
    context_store.mark_used([a.id, b.id])
    context_store.mark_used([a.id])
    assert context_store.get_entry(a.id).used_count == 2
    assert context_store.get_entry(b.id).used_count == 1


def test_kinds_ordering_interaction_first(context_store: ContextStore):
    context_store.add_entry("fact", "事实条目")
    context_store.add_entry("interaction", "相处方式条目")
    context_store.add_entry("profile", "画像条目")
    kinds = [e.kind for e in context_store.list_entries()]
    assert kinds[0] == "interaction" and kinds[1] == "profile"


def test_export_includes_entries_and_history(context_store: ContextStore):
    entry, _ = context_store.add_entry("profile", "喜欢猫")
    context_store.delete_entry(entry.id)
    data = context_store.export_all()
    assert data["counts"] == {"entries": 1, "active": 0, "changes": 2}
    assert data["entries"][0]["content"] == "喜欢猫"
    assert {h["action"] for h in data["history"]} == {"create", "delete"}
