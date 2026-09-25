"""快速冒烟：验证所有模块可导入、关键函数基本可用。运行：python scripts/smoke.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings, ensure_dirs
from core.logging_setup import setup_logging
from core.understanding import parse_understanding, extract_json
from core.policy import MemoryPolicy
from core.intervention import InterventionPolicy
from core.pipeline import CorePipeline
from core.context import Recaller, plan_query, extract_keywords
from memory.event import Event
from memory.store import MemoryStore
from models.ollama_adapter import OllamaAdapter
from models.mock_adapter import MockChatModel
from agent.tools import build_default_registry
from perception.image_file import normalize_image

ensure_dirs()
setup_logging("WARNING")

r = parse_understanding(
    '{"scene":"书房","activity":"写作业","description":"在写数学作业",'
    '"importance":0.6,"objects":["作业本"]}',
    source="test",
)
assert r.ok and r.event.importance == 0.6, r

r2 = parse_understanding("看不懂这张图", source="test")
assert not r2.ok and r2.event is not None

p = plan_query("刚才我在干什么")
assert p.window_label.startswith("刚才"), p

kws = extract_keywords("最近我玩了什么游戏")
assert any("游戏" in k for k in kws), kws

store = MemoryStore(Path(__file__).resolve().parent.parent / "data" / "smoke.db")
store.delete_all_events()
event = r.event
store.add_event(event)
assert store.count_events() == 1
hits = store.search_events(["作业"])
assert hits and hits[0].id == event.id
store.delete_all_events()
assert store.count_events() == 0

reg = build_default_registry(store)
assert len(reg.list_all()) == 2

import base64
from PIL import Image
import io
buf = io.BytesIO()
Image.new("RGB", (400, 300), (10, 120, 200)).save(buf, format="PNG")
norm = normalize_image(buf.getvalue())
assert norm[:2] == b"\xff\xd8", "normalize 应输出 JPEG"

print("SMOKE OK")
