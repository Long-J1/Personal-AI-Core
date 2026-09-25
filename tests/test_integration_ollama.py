"""真实端到端测试：真 Ollama 模型跑 感知→理解→事件→记忆→检索→回忆。

Ollama 不在运行时自动跳过。运行：
    python -m pytest tests -m reale2e -s
"""
from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.reale2e

from core.config import settings
from core.context import Recaller
from core.pipeline import CorePipeline
from memory.store import MemoryStore
from models.ollama_adapter import OllamaAdapter
from tests.helpers import make_image


@pytest.fixture(autouse=True)
def require_ollama():
    try:
        ok = (
            httpx.get(
                f"{settings.ollama_url}/api/version", timeout=3, trust_env=False
            ).status_code
            == 200
        )
    except Exception:
        ok = False
    if not ok:
        pytest.skip("Ollama 服务未运行，跳过真实端到端测试")
    # 确认模型存在
    try:
        tags = httpx.get(
            f"{settings.ollama_url}/api/tags", timeout=5, trust_env=False
        ).json()
        names = [t.get("name", "") for t in tags.get("models", [])]
        if settings.model not in names:
            pytest.skip(f"模型不存在：{settings.model}")
    except Exception:
        pass


@pytest.fixture
def real_model():
    return OllamaAdapter(settings.ollama_url, settings.model, settings.understand_timeout)


def test_real_observe_then_recall(tmp_path, monkeypatch, real_model):
    import core.pipeline as pm

    monkeypatch.setattr(pm, "THUMB_DIR", tmp_path / "thumbs")
    store = MemoryStore(tmp_path / "real.db")
    pipeline = CorePipeline(store, real_model)

    result = pipeline.observe(make_image("REAL-DEMO"), source="test")
    assert result.ok, f"观察失败：{result.skip_reason}"
    assert result.parse_ok, (
        f"模型输出未结构化：{result.event and result.event.raw_understanding!r}"
    )
    assert result.stored
    assert result.event.scene and result.event.description

    recaller = Recaller(store, real_model)
    answer = recaller.recall("刚才我看到了什么")
    assert answer.retrieved_count >= 1, answer.answer
    assert answer.answer.strip()
    assert answer.sources, "回答必须带依据"
