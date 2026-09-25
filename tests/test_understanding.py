"""理解输出解析（core.understanding）的健壮性测试。"""
from __future__ import annotations

import pytest

from core.understanding import extract_json, parse_understanding


def test_clean_json():
    r = parse_understanding(
        '{"scene":"书房","activity":"写作业","description":"写数学作业",'
        '"importance":0.6,"objects":["笔"],"facts":["爱学习"]}',
        source="test",
    )
    assert r.ok and r.error is None
    e = r.event
    assert e.scene == "书房" and e.activity == "写作业"
    assert e.importance == pytest.approx(0.6)
    assert e.objects == ["笔"] and e.facts == ["爱学习"]
    assert e.raw_understanding is not None


def test_markdown_fenced_json():
    raw = '好的，这是结果：\n```json\n{"scene":"厨房","description":"在做饭","importance":0.4}\n```\n希望有帮助！'
    r = parse_understanding(raw, source="test")
    assert r.ok and r.event.scene == "厨房"


def test_json_wrapped_in_prose():
    raw = '我看到：{"scene":"客厅","description":"看电视","importance":0.3} 以上。'
    r = parse_understanding(raw, source="test")
    assert r.ok and r.event.description == "看电视"


def test_missing_fields_use_defaults():
    r = parse_understanding('{"activity":"跑步"}', source="test")
    assert r.ok
    assert r.event.description == "跑步"       # 没有 description 时用 activity 兜底
    assert r.event.objects == [] and r.event.scene == ""


def test_importance_coercion_and_clamp():
    assert parse_understanding('{"description":"a","importance":"0.9"}', source="t").event.importance == pytest.approx(0.9)
    assert parse_understanding('{"description":"a","importance":1.7}', source="t").event.importance == 1.0
    assert parse_understanding('{"description":"a","importance":-5}', source="t").event.importance == 0.0
    assert parse_understanding('{"description":"a","importance":"高"}', source="t").event.importance == 0.5


def test_objects_as_string():
    r = parse_understanding('{"description":"a","objects":"书包"}', source="t")
    assert r.event.objects == ["书包"]


def test_garbage_output_falls_back():
    r = parse_understanding("I'm sorry, I cannot see the image.", source="t")
    assert not r.ok and r.error
    assert r.event is not None
    assert "cannot see" in r.event.description
    assert r.event.scene == "（未结构化）"
    assert r.event.importance < 0.5


def test_empty_output():
    r = parse_understanding("", source="t")
    assert not r.ok
    assert r.event.description == "（模型无输出）"


def test_extract_json_none_cases():
    assert extract_json("") is None
    assert extract_json("没有花括号") is None
    assert extract_json('{"broken": ') is None
    assert extract_json('{"ok": 1}') == {"ok": 1}


def test_alias_fields():
    raw = '{"location":"教室","action":"上课","summary":"正在上课","score":0.7}'
    r = parse_understanding(raw, source="t")
    assert r.ok
    assert r.event.scene == "教室" and r.event.activity == "上课"
    assert r.event.description == "正在上课"
    assert r.event.importance == pytest.approx(0.7)


# ---------- 截断修复（D008 补记：num_predict 撞上限时的兜底） ----------

from core.understanding import repair_truncated_json  # noqa: E402


def test_repair_truncated_in_array():
    """输出在 corrections 数组中间被硬截断 → 补括号救回 new。"""
    raw = ('{"new": [{"kind": "profile", "content": "用户是高一学生"}], '
           '"corrections": [{"ref"')
    data = repair_truncated_json(raw)
    assert data is not None
    assert data["new"][0]["content"] == "用户是高一学生"
    assert isinstance(data["corrections"], list)


def test_repair_truncated_mid_string():
    """截断点落在字符串值中间 → 裁掉悬空字符串与无值的键。"""
    raw = '{"new": [{"kind": "fact", "content": "用户不吃香'
    data = repair_truncated_json(raw)
    assert data is not None
    assert data["new"][0]["kind"] == "fact"     # 完整的字段保留


def test_repair_truncated_dangling_key_no_value():
    """结尾是 "event": 没来得及给值 → 去掉悬挂键后补齐。"""
    raw = '{"new": [], "corrections": [], "event"'
    data = repair_truncated_json(raw)
    assert data is not None
    assert data["new"] == [] and data["corrections"] == []


def test_repair_cuts_back_to_last_complete_structure():
    """补括号也救不活时，砍到最后一个完整闭合的结构再补。"""
    raw = '{"new": [{"kind": "fact", "content": "不吃香菜"}], "corrections": [{"ref": "#1", "content"'
    data = repair_truncated_json(raw)
    assert data is not None
    assert data["new"][0]["content"] == "不吃香菜"


def test_repair_garbage_returns_none():
    assert repair_truncated_json("这不是JSON") is None
    assert repair_truncated_json("") is None
    assert repair_truncated_json("[] 不是对象") is None


def test_repair_complete_json_returns_it():
    assert repair_truncated_json('{"a": 1, "b": [2]}') == {"a": 1, "b": [2]}
