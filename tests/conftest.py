"""pytest 共享夹具：每个测试拿到独立的临时记忆库 + Mock 模型。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import Recaller
from core.conversation import ConversationService
from core.pipeline import CorePipeline
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "test.db")


@pytest.fixture
def context_store(tmp_path: Path) -> ContextStore:
    return ContextStore(tmp_path / "test.db")


@pytest.fixture
def chat_store(tmp_path: Path) -> ChatStore:
    return ChatStore(tmp_path / "test.db")


@pytest.fixture
def mock_model() -> MockChatModel:
    return MockChatModel()


@pytest.fixture
def pipeline(store, mock_model, monkeypatch, tmp_path) -> CorePipeline:
    import core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "THUMB_DIR", tmp_path / "thumbs")
    return CorePipeline(store, mock_model)


@pytest.fixture
def recaller(store, mock_model) -> Recaller:
    return Recaller(store, mock_model)


@pytest.fixture
def conversation(store, context_store, chat_store, mock_model) -> ConversationService:
    return ConversationService(
        store, context_store, mock_model, chat_store=chat_store
    )


@pytest.fixture
def require_ollama():
    """Ollama 未运行/模型不存在时跳过（真模型测试共用）。"""
    import httpx

    from core.config import settings

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
def real_model(require_ollama):
    from models.ollama_adapter import OllamaAdapter

    from core.config import settings

    return OllamaAdapter(settings.ollama_url, settings.model, settings.understand_timeout)
