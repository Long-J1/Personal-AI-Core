"""pytest 共享夹具：每个测试拿到独立的临时记忆库 + Mock 模型。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import Recaller
from core.pipeline import CorePipeline
from memory.store import MemoryStore
from models.mock_adapter import MockChatModel


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "test.db")


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
