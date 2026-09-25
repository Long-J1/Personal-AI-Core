"""SQLite 记忆库（memory.store）测试：写入、检索、删除、导出、暂停。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from memory.event import Event


def _event(desc: str, minutes: int = 0, **kw) -> Event:
    return Event(
        created_at=datetime.now().astimezone() - timedelta(minutes=minutes),
        description=desc,
        **kw,
    )


def test_add_get_roundtrip(store):
    e = _event("用户在写代码", scene="书房", activity="编程", objects=["电脑"], importance=0.7,
               facts=["用户是程序员"])
    store.add_event(e)
    got = store.get_event(e.id)
    assert got is not None
    assert got.description == "用户在写代码"
    assert got.scene == "书房" and got.objects == ["电脑"]
    assert got.facts == ["用户是程序员"]
    assert got.importance == pytest.approx(0.7)
    assert got.created_at == e.created_at


def test_count_and_list_order(store):
    store.add_event(_event("第一个", minutes=30))
    store.add_event(_event("第二个", minutes=10))
    store.add_event(_event("第三个", minutes=50))
    assert store.count_events() == 3
    listed = store.list_events()
    assert [e.description for e in listed] == ["第二个", "第一个", "第三个"]
    asc = store.list_events(order="asc")
    assert asc[0].description == "第三个"


def test_time_range_filter(store):
    old = _event("很久以前", minutes=600)
    recent = _event("刚刚发生", minutes=5)
    store.add_event(old)
    store.add_event(recent)
    now_epoch = datetime.now().astimezone().timestamp()
    hits = store.list_events(since_epoch=now_epoch - 3600)
    assert [e.description for e in hits] == ["刚刚发生"]


def test_keyword_search_and_or(store):
    store.add_event(_event("在写数学作业", scene="书房"))
    store.add_event(_event("在操场跑步", scene="操场"))
    store.add_event(_event("用 Excel 做表格", scene="书桌"))

    and_hits = store.search_events(["写", "作业"], mode="and")
    assert len(and_hits) == 1 and "作业" in and_hits[0].description

    or_hits = store.search_events(["作业", "跑步"], mode="or")
    assert len(or_hits) == 2

    assert store.search_events(["不存在的词"]) == []


def test_keyword_search_scenes_and_objects(store):
    store.add_event(_event("买东西", scene="超市", objects=["牛奶"]))
    store.add_event(_event("吃饭", scene="食堂", objects=["米饭"]))
    assert len(store.search_events(["超市"])) == 1
    assert len(store.search_events(["牛奶"])) == 1


def test_wildcard_escaped(store):
    store.add_event(_event("完成率 100% 的任务"))
    assert store.search_events(["100%"]) == store.search_events(["100"])
    # 不加转义时 % 是通配符，会错误匹配；两种都应只命中该条
    assert len(store.search_events(["100%"])) == 1


def test_delete_event_with_facts(store):
    e = _event("事件", facts=["一条事实"])
    store.add_event(e)
    assert store.list_facts()
    assert store.delete_event(e.id) is True
    assert store.get_event(e.id) is None
    assert store.list_facts() == []
    assert store.delete_event("不存在") is False


def test_delete_all_keeps_meta(store):
    store.add_event(_event("a"))
    store.add_event(_event("b", facts=["事实"]))
    store.paused = True
    deleted = store.delete_all_events()
    assert deleted == 2
    assert store.count_events() == 0
    assert store.list_facts() == []
    assert store.paused is True      # 隐私开关是元信息，不清记忆不影响


def test_facts_deduplicated(store):
    store.add_event(_event("a", facts=["用户喜欢猫"]))
    store.add_event(_event("b", minutes=5, facts=["用户喜欢猫", "用户喜欢狗"]))
    facts = store.list_facts()
    contents = [f["content"] for f in facts]
    assert contents.count("用户喜欢猫") == 1
    assert "用户喜欢狗" in contents


def test_export_structure(store):
    store.add_event(_event("导出我", facts=["事实1"]))
    data = store.export_all()
    assert data["counts"]["events"] == 1
    assert data["counts"]["facts"] == 1
    assert len(data["events"]) == 1
    assert data["events"][0]["description"] == "导出我"
    assert data["exported_at"] and data["schema_version"] == 1


def test_pause_flag_persist(store):
    assert store.paused is False
    store.paused = True
    assert store.paused is True
    # 同一个库文件新开连接仍应是暂停状态
    from memory.store import MemoryStore

    again = MemoryStore(store.db_path)
    assert again.paused is True
