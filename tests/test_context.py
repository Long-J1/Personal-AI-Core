"""查询解析与回忆（core.context）测试：时间窗、关键词、检索级联、可解释性。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.context import extract_keywords, plan_query
from core.pipeline import CorePipeline
from memory.event import Event
from memory.store import MemoryStore
from models.base import ModelUnavailableError
from models.mock_adapter import MockChatModel
from tests.helpers import make_image, now_, today_sometime, yesterday_pm


# ---------- 时间窗解析 ----------

def test_plan_just_now():
    p = plan_query("刚才我在干什么")
    assert p.window_label.startswith("刚才")
    assert p.since_epoch is not None and p.until_epoch is not None
    span = p.until_epoch - p.since_epoch
    assert 25 * 60 <= span <= 31 * 60


def test_plan_today():
    p = plan_query("今天我都做了什么")
    assert p.window_label == "今天"
    today0 = now_().replace(hour=0, minute=0, second=0, microsecond=0)
    assert abs(p.since_epoch - today0.timestamp()) < 2


def test_plan_yesterday():
    p = plan_query("昨天发生了什么")
    assert p.window_label == "昨天"
    today0 = now_().replace(hour=0, minute=0, second=0, microsecond=0)
    assert p.until_epoch == pytest.approx(today0.timestamp())
    assert p.since_epoch == pytest.approx((today0 - timedelta(days=1)).timestamp())


def test_plan_recent_7d():
    p = plan_query("最近有什么重要的事")
    assert p.window_label == "最近7天"
    assert (p.until_epoch - p.since_epoch) == pytest.approx(7 * 86400, abs=60)


def test_plan_last_time_no_window():
    p = plan_query("上次我在哪里吃饭")
    assert p.window_label == "全部记忆"
    assert p.since_epoch is None and p.until_epoch is None


def test_plan_default_24h():
    p = plan_query("我在干什么")
    assert p.window_label == "最近24小时"
    assert (p.until_epoch - p.since_epoch) == pytest.approx(86400, abs=5)


# ---------- 关键词 ----------

def test_extract_keywords_basic():
    assert "学习" in extract_keywords("今天我学习了什么")
    assert extract_keywords("刚才我在干什么") == []   # 全是废话，正确切空
    assert "excel" in [k.lower() for k in extract_keywords("提到Excel的事件")]
    assert len(extract_keywords("今天做了很多事：写代码、跑步、做饭")) >= 2


def test_extract_keywords_drops_stopwords():
    kws = extract_keywords("你今天到底干了什么呀")
    assert kws == []


# ---------- 检索级联与回答 ----------

def _seed(store, desc, minutes=5, at=None, **kw):
    e = Event(
        created_at=at or (datetime.now().astimezone() - timedelta(minutes=minutes)),
        description=desc,
        **kw,
    )
    store.add_event(e)
    return e


def test_recall_happy_path_with_sources(store, mock_model, recaller):
    _seed(store, "用户在书桌前写数学作业", scene="书房")
    ans = recaller.recall("刚才我在干什么")
    assert ans.retrieved_count == 1
    assert "写数学作业" in ans.answer
    assert ans.sources and ans.sources[0].description == "用户在书桌前写数学作业"
    assert ans.sources[0].time  # 依据时间


def test_recall_empty_store_deterministic(store, recaller, mock_model):
    ans = recaller.recall("刚才我在干什么")
    assert ans.retrieved_count == 0
    assert "没有找到" in ans.answer
    assert ans.sources == []
    assert mock_model.calls == []     # 没有记忆就不用烧模型


def test_recall_keyword_cascade_keeps_time_window(store, recaller, mock_model):
    """关键词切得太烂也不该放弃时间窗（级联最终落到窗口检索）。"""
    _seed(store, "用户在打羽毛球", minutes=10)
    plan = plan_query("刚才我干的那件超难的事是什么")
    assert plan.keywords  # 有切出关键词
    ans = recaller.recall("刚才我干的那件超难的事是什么")
    assert ans.retrieved_count == 1, "应通过级联回到时间窗检索"
    assert "羽毛球" in ans.answer
    assert ans.fallback_used


def test_recall_falls_back_to_all_when_window_empty(store, recaller, mock_model):
    _seed(store, "很久以前修好了电脑", minutes=600)
    ans = recaller.recall("今天我修电脑了吗")
    assert ans.retrieved_count == 1
    assert ans.fallback_used and "全部" in ans.fallback_used


def test_recall_model_error_degrades_gracefully(store, recaller):
    _seed(store, "用户在做晚饭")
    broken = MockChatModel(fail_with=ModelUnavailableError("模拟模型挂了"))
    from core.context import Recaller

    ans = Recaller(store, broken).recall("刚才我在做什么")
    assert ans.retrieved_count == 1
    assert "模型暂不可用" in ans.answer
    assert "做晚饭" in ans.answer      # 降级也要把记忆端出来


def test_recall_answer_is_built_from_context(store, recaller, mock_model):
    """确认记忆真的进了提示词（而不是模型瞎答）。"""
    _seed(store, "用户在拼乐高", scene="卧室")
    recaller.recall("刚才我在干什么")
    calls = mock_model.calls
    assert calls, "应调用模型"
    user_prompt = calls[-1]["messages"][-1]["content"]
    assert "拼乐高" in user_prompt
    assert "检索范围" in user_prompt
