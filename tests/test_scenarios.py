"""场景闭环测试：20 个“看图 → 事件 → 记忆 → 回忆”案例。

- 默认：Mock 模型（秒级、确定性）
- PAI_REAL=1 python -m pytest tests/test_scenarios.py：换真 Ollama 模型跑全流程
  （真模型措辞不可控，只断言结构性要求：入库、有回答、有依据）
"""
from __future__ import annotations

import json
import os

import pytest

from core.context import Recaller
from core.pipeline import CorePipeline
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel
from tests.scenarios import SCENARIOS
from tests.helpers import make_image, resolve_when

REAL = bool(os.environ.get("PAI_REAL"))


def _build(store, sc, tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "THUMB_DIR", tmp_path / "thumbs")
    if REAL:
        from core.config import settings
        from models.ollama_adapter import OllamaAdapter

        model = OllamaAdapter(settings.ollama_url, settings.model, settings.understand_timeout)
    else:
        model = MockChatModel(
            understanding_queue=[json.dumps(sc["understanding"], ensure_ascii=False)]
        )
    return CorePipeline(store, model), Recaller(store, model), model


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_scene_to_memory_to_recall(sc, tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "scenario.db")
    pipeline, recaller, _model = _build(store, sc, tmp_path, monkeypatch)

    # ① 感知 + 理解 + 事件 + 记忆
    at = resolve_when(sc["when"])
    result = pipeline.observe(make_image(sc["name"]), source="test", at=at)
    assert result.ok, f"观察失败：{result.skip_reason}"
    assert result.parse_ok, "理解结果应能解析为结构化事件"
    assert result.stored, f"事件应存入记忆库：{result.skip_reason}"
    assert result.event and sc["expect"] in (
        result.event.description + result.event.activity + result.event.scene
    ), "期望内容应出现在事件里"

    # ② 检索 + 回忆
    answer = recaller.recall(sc["query"])
    assert answer.retrieved_count >= 1, f"没检索到记忆：{answer.answer}"
    assert answer.sources, "回答必须附带依据（可解释性验收项）"
    if REAL:
        assert answer.answer.strip(), "真模型应回答"
    else:
        assert sc["expect"] in answer.answer, (
            f"Mock 回答应包含 {sc['expect']!r}，实际：{answer.answer!r}"
        )
