"""主管线（core.pipeline）+ 判断策略 + 介入接口测试。"""
from __future__ import annotations

import pytest

from core.intervention import InterventionPolicy
from core.pipeline import CorePipeline
from core.policy import MemoryPolicy
from memory.event import Event
from models.base import ModelUnavailableError
from models.mock_adapter import MockChatModel
from tests.helpers import make_image


def test_observe_happy_path(pipeline, store):
    result = pipeline.observe(make_image("HAPPY"))
    assert result.ok and result.parse_ok and result.stored
    assert result.event and result.event.source == "image_upload"
    assert result.duration_s > 0
    assert result.policy_reason
    assert store.count_events() == 1
    # 缩略图应落盘
    from core.config import THUMB_DIR  # patched by fixture? use event.image_ref path check below
    if result.event.image_ref:
        import core.pipeline as pm
        thumb_path = pm.THUMB_DIR / f"{result.event.id}.jpg"
        assert thumb_path.exists()


def test_observe_normalizes_source(store, mock_model, monkeypatch, tmp_path):
    import core.pipeline as pm

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    pipe = CorePipeline(store, mock_model)
    pipe.observe(make_image(), source="camera")
    assert store.list_events()[0].source == "camera"


def test_privacy_pause_skips_everything(pipeline, store, mock_model):
    store.paused = True
    result = pipeline.observe(make_image())
    assert not result.stored and not result.ok
    assert "暂停" in result.skip_reason
    assert mock_model.calls == [], "暂停时连模型都不该调用"
    assert store.count_events() == 0


def test_model_error_is_contained(store, monkeypatch, tmp_path):
    import core.pipeline as pm

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    model = MockChatModel(fail_with=ModelUnavailableError("连接失败"))
    pipe = CorePipeline(store, model)
    result = pipe.observe(make_image())
    assert not result.stored
    assert "模型错误" in result.skip_reason
    assert "连接失败" in result.error
    assert store.count_events() == 0


def test_unstructured_output_still_memorized(pipeline, store):
    model = MockChatModel(understanding_queue=["看不懂这张图"])
    import core.pipeline as pm

    pipeline.model = model
    result = pipeline.observe(make_image())
    assert result.ok and not result.parse_ok
    assert result.stored, "理解失败也应保守记忆原文"
    assert result.event.scene == "（未结构化）"
    assert result.event.raw_understanding == "看不懂这张图"


def test_intervention_default_is_silent(pipeline):
    result = pipeline.observe(make_image())
    assert result.intervention is not None
    assert result.intervention.intervene is False
    assert "沉默" in result.intervention.reason


def test_memory_policy_threshold():
    policy = MemoryPolicy(min_importance=0.8)
    low = Event(description="琐事", importance=0.3)
    high = Event(description="大事", importance=0.9)
    assert policy.should_remember(low, paused=False, parse_ok=True).remember is False
    assert policy.should_remember(high, paused=False, parse_ok=True).remember is True
    assert policy.should_remember(high, paused=True, parse_ok=True).remember is False


def test_memory_policy_remembers_parse_failure():
    policy = MemoryPolicy(min_importance=0.9)
    bad = Event(description="原文", importance=0.2)
    d = policy.should_remember(bad, paused=False, parse_ok=False)
    assert d.remember and "保守" in d.reason


def test_intervention_interface_exists():
    dec = InterventionPolicy().decide(Event(description="x"))
    assert dec.intervene is False and dec.reason
