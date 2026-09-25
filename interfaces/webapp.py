"""Web 界面 + JSON API（FastAPI，见 docs/decisions/D001）。

页面在 interfaces/static/index.html；所有端点都在 127.0.0.1 本地使用。
通过 create_app() 注入依赖，方便测试用 Mock 模型替换。
"""
from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from core.config import DB_PATH, ensure_dirs, settings
from core.context import Recaller
from core.pipeline import CorePipeline
from memory.store import MemoryStore
from models.base import BaseModelAdapter
from models.ollama_adapter import OllamaAdapter
from perception.base import PerceptionError
from perception.image_file import normalize_image

log = logging.getLogger("interfaces.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
_THUMB_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")

_SOURCE_MAP = {"upload": "image_upload", "camera": "camera", "manual": "manual"}


# ---------- 请求体 ----------
class ObserveRequest(BaseModel):
    image_b64: str = Field(min_length=8)
    source: str = "upload"   # upload | camera


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class PrivacyRequest(BaseModel):
    paused: bool


def default_model() -> OllamaAdapter:
    return OllamaAdapter(settings.ollama_url, settings.model, settings.understand_timeout)


def create_app(
    store: MemoryStore | None = None,
    model: BaseModelAdapter | None = None,
) -> FastAPI:
    ensure_dirs()
    store = store or MemoryStore(DB_PATH)
    model = model or default_model()
    pipeline = CorePipeline(store, model)
    recaller = Recaller(store, model)

    app = FastAPI(title="Personal AI Core", version=settings.version)

    # ---------- 页面 ----------
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/thumbs/{event_id}", response_model=None)
    def thumbnail(event_id: str):
        if not _THUMB_ID_RE.match(event_id):
            return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
        path = settings_thumb_path(event_id)
        if not path.exists():
            return JSONResponse({"ok": False, "error": "no thumb"}, status_code=404)
        return FileResponse(path, media_type="image/jpeg")

    # ---------- 状态 ----------
    @app.get("/api/status")
    def status() -> dict:
        ping = getattr(model, "ping", None)
        service_ok = None
        if callable(ping):
            try:
                service_ok = bool(ping())
            except Exception:
                service_ok = False
        return {
            "version": settings.version,
            "model": getattr(model, "model", getattr(model, "name", "unknown")),
            "events": store.count_events(),
            "paused": store.paused,
            "model_service_ok": service_ok,
        }

    # ---------- 感知 → 理解 → 记忆 ----------
    @app.post("/api/observe")
    def observe(req: ObserveRequest) -> dict:
        raw = req.image_b64.split(",", 1)[-1] if "," in req.image_b64[:64] and "base64" in req.image_b64[:64] else req.image_b64
        try:
            data = base64.b64decode(raw, validate=False)
            data = normalize_image(data)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"图片解码失败：{exc}"}, status_code=400
            )

        result = pipeline.observe(data, source=_SOURCE_MAP.get(req.source, "image_upload"))
        return {
            "ok": result.ok,
            "stored": result.stored,
            "parse_ok": result.parse_ok,
            "skip_reason": result.skip_reason,
            "policy_reason": result.policy_reason,
            "duration_s": round(result.duration_s, 2),
            "error": result.error,
            "intervention": result.intervention.reason if result.intervention else None,
            "event": result.event.to_public_dict() if result.event else None,
        }

    @app.post("/api/camera/snap")
    def camera_snap() -> dict:
        """服务器端摄像头抓帧（浏览器摄像头失败时的备用路径）。"""
        from perception.base import CameraUnavailableError
        from perception import camera as camera_mod

        try:
            frame = camera_mod.capture()
            return {"ok": True, "image_b64": base64.b64encode(frame).decode("ascii")}
        except CameraUnavailableError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "hint": exc.hint}, status_code=400
            )
        except PerceptionError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    # ---------- 回忆 ----------
    @app.post("/api/chat")
    def chat(req: ChatRequest) -> dict:
        answer = recaller.recall(req.question)
        return {
            "answer": answer.answer,
            "sources": [s.to_dict() for s in answer.sources],
            "window_label": answer.window_label,
            "retrieved_count": answer.retrieved_count,
            "keywords": answer.keywords,
            "fallback_used": answer.fallback_used,
        }

    # ---------- 记忆管理（查看/删除/导出/暂停）----------
    @app.get("/api/memories")
    def list_memories(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        kw: str | None = Query(None, max_length=100),
    ) -> dict:
        keywords = [kw] if kw else None
        items = (
            store.search_events(keywords, limit=limit)
            if keywords
            else store.list_events(limit=limit, offset=offset)
        )
        return {
            "total": store.count_events(),
            "paused": store.paused,
            "items": [e.to_public_dict() for e in items],
        }

    @app.delete("/api/memories/{event_id}")
    def delete_memory(event_id: str) -> dict:
        found = store.delete_event(event_id)
        return {"ok": found, "found": found}

    @app.delete("/api/memories")
    def clear_memories() -> dict:
        deleted = store.delete_all_events()
        return {"ok": True, "deleted": deleted}

    @app.get("/api/memories/export")
    def export_memories() -> JSONResponse:
        data = store.export_all()
        return JSONResponse(
            content=data,
            headers={
                "Content-Disposition": "attachment; filename=personal_ai_memories.json"
            },
        )

    @app.get("/api/privacy")
    def get_privacy() -> dict:
        return {"paused": store.paused}

    @app.post("/api/privacy")
    def set_privacy(req: PrivacyRequest) -> dict:
        store.paused = req.paused
        return {"paused": store.paused}

    @app.get("/api/facts")
    def list_facts(limit: int = Query(50, ge=1, le=200)) -> dict:
        return {"items": store.list_facts(limit)}

    return app


def settings_thumb_path(event_id: str) -> Path:
    from core.config import THUMB_DIR

    return THUMB_DIR / f"{event_id}.jpg"


# uvicorn 入口：uvicorn interfaces.webapp:app
app = create_app()
