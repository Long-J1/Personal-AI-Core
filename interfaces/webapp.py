"""Web 界面 + JSON API（FastAPI，见 docs/decisions/D001）。

页面在 interfaces/static/index.html；所有端点都在 127.0.0.1 本地使用。
通过 create_app() 注入依赖，方便测试用 Mock 模型替换。
"""
from __future__ import annotations

import base64
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from core.config import DATA_DIR, DB_PATH, THUMB_DIR, ensure_dirs, settings
from core.context import Recaller
from core.conversation import ConversationService
from core.data_manager import DataManager, DataError
from core.pipeline import CorePipeline
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.store import MemoryStore
from models.base import BaseModelAdapter, ModelError
from models.factory import ModelRegistry, build_model
from models.settings_store import default_store, public_view
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


class ConversationRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None


class ContextUpdateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500)


# ---------- 模型设置（D009） ----------
class OllamaCfg(BaseModel):
    url: str | None = None
    model: str | None = None


class OpenAICfg(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None   # None=不修改；""=清除


class ModelSettingsRequest(BaseModel):
    provider: str | None = None          # ollama | openai
    ollama: OllamaCfg | None = None
    openai: OpenAICfg | None = None


class RestoreRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class PrivacyRequest(BaseModel):
    paused: bool


def default_model() -> ModelRegistry:
    """默认模型门面：从数据目录的设置装配，可热切换 Provider（D009）。"""
    return ModelRegistry()


def _describe(model: BaseModelAdapter) -> dict:
    """当前模型描述（不泄露 API Key）。"""
    if isinstance(model, ModelRegistry):
        return model.describe()
    return {"provider": getattr(model, "name", "unknown"), "model": getattr(model, "model", "")}


def create_app(
    store: MemoryStore | None = None,
    model: BaseModelAdapter | None = None,
    context_store: ContextStore | None = None,
    chat_store: ChatStore | None = None,
) -> FastAPI:
    ensure_dirs()
    store = store or MemoryStore(DB_PATH)
    model = model or default_model()
    # 模型设置的读写目标：注册表自带存储（测试可注入 tmp 路径），否则用默认存储
    model_settings_store = getattr(model, "settings_store", None) or default_store()
    # 数据管理（阶段2：导出/备份/恢复/导入）——全部发生在数据目录内
    data_mgr = DataManager(Path(DATA_DIR), Path(THUMB_DIR))
    context_store = context_store or ContextStore(store.db_path)
    chat_store = chat_store or ChatStore(store.db_path)
    pipeline = CorePipeline(store, model)
    recaller = Recaller(store, model)
    conversation = ConversationService(
        store, context_store, model, chat_store=chat_store
    )

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
            "provider": _describe(model)["provider"],
            "events": store.count_events(),
            "context_entries": context_store.count_active(),
            "sessions": chat_store.count_sessions(),
            "paused": store.paused,
            "model_service_ok": service_ok,
        }

    # ---------- 模型设置（D009：换模型不换"它"） ----------
    @app.get("/api/model/settings")
    def get_model_settings() -> dict:
        cfg = model_settings_store.load()
        return {"ok": True, **public_view(cfg), "current": _describe(model)}

    @app.put("/api/model/settings")
    def put_model_settings(req: ModelSettingsRequest):
        try:
            cfg = model_settings_store.update(req.model_dump(exclude_none=True))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if isinstance(model, ModelRegistry):
            model.reload()   # 只换推理引擎；Context/事件/会话一概不动
        return {"ok": True, **public_view(cfg), "current": _describe(model)}

    @app.post("/api/model/test")
    def test_model_settings(req: ModelSettingsRequest | None = None) -> dict:
        """连接测试：可用保存前的临时配置测试，不落盘。"""
        try:
            cfg = model_settings_store.resolve(req.model_dump(exclude_none=True)) if req else model_settings_store.load()
            adapter = build_model(cfg, timeout=10.0)
        except ValueError as exc:
            return {"ok": False, "detail": str(exc)}
        except ModelError as exc:
            return {"ok": False, "detail": str(exc)}
        tester = getattr(adapter, "test_connection", None)
        if not callable(tester):
            return {"ok": False, "detail": "该 Provider 不支持连接测试"}
        try:
            ok, detail = tester(timeout=8.0)
        except Exception as exc:  # 连接测试绝不许把服务打崩
            ok, detail = False, f"测试失败：{exc}"
        return {"ok": bool(ok), "detail": detail,
                "provider": cfg["provider"],
                "model": getattr(adapter, "model", "")}

    # ---------- 数据管理（阶段2：导出/备份/恢复/导入，D010） ----------
    @app.get("/api/data/location")
    def data_location() -> dict:
        return {"ok": True, **data_mgr.location()}

    @app.get("/api/data/export")
    def data_export():
        try:
            zip_path = data_mgr.export()
        except DataError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=zip_path.name,
            background=BackgroundTask(shutil.rmtree, zip_path.parent, ignore_errors=True),
        )

    @app.get("/api/data/backups")
    def data_backups() -> dict:
        return {"ok": True, "items": data_mgr.list_backups()}

    @app.post("/api/data/backup")
    def data_backup():
        try:
            name = data_mgr.create_backup()
        except (DataError, OSError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "name": name, "items": data_mgr.list_backups()}

    @app.post("/api/data/restore")
    def data_restore(req: RestoreRequest):
        try:
            result = data_mgr.restore(from_backup=req.name)
        except DataError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if isinstance(model, ModelRegistry):
            model.reload()   # 模型设置也随包恢复，门面跟上
        return {"ok": True, **result}

    @app.post("/api/data/import")
    async def data_import(file: UploadFile = File(...)):
        tmp_dir = Path(tempfile.mkdtemp(prefix="pai_upload_"))
        tmp = tmp_dir / (Path(file.filename or "import.zip").name)
        try:
            tmp.write_bytes(await file.read())
            result = data_mgr.restore(source=tmp)
        except DataError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"ok": False, "error": f"读取上传文件失败：{exc}"}, status_code=400)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if isinstance(model, ModelRegistry):
            model.reload()
        return {"ok": True, **result}

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

    # ---------- 对话（V0.2：读 Context → 回答 → 沉淀写回）----------
    @app.post("/api/conversation")
    def post_conversation(req: ConversationRequest) -> dict:
        try:
            reply = conversation.chat(req.message, req.session_id)
        except ModelError as exc:
            log.error("对话的模型调用失败：%s", exc)
            return JSONResponse(
                {"ok": False, "error": f"模型暂不可用：{exc}"}, status_code=503
            )
        return {"ok": True, **reply.to_dict()}

    @app.get("/api/conversation/history")
    def conversation_history(
        session_id: str | None = Query(None), limit: int = Query(200, ge=1, le=500)
    ) -> dict:
        sid = chat_store.get_or_create_session(session_id)
        return {
            "session_id": sid,
            "messages": chat_store.history(sid, limit=limit, order="asc"),
        }

    # ---------- Personal Context（"它对你的理解"，信任界面）----------
    @app.get("/api/context")
    def list_context() -> dict:
        items = context_store.list_entries()
        counts: dict[str, int] = {}
        for e in items:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        return {
            "total": len(items),
            "paused": store.paused,
            "counts": counts,
            "items": [e.to_public_dict() for e in items],
        }

    @app.put("/api/context/{entry_id}")
    def edit_context(entry_id: str, req: ContextUpdateRequest) -> dict:
        """手动纠正（信任界面：你随时可以改它对你的理解）。"""
        ok = context_store.correct_entry(
            entry_id, req.content, source="manual", reason="用户手动编辑"
        )
        entry = context_store.get_entry(entry_id)
        return {
            "ok": ok,
            "entry": entry.to_public_dict() if (ok and entry) else None,
        }

    @app.delete("/api/context/{entry_id}")
    def delete_context(entry_id: str) -> dict:
        """删除即失效：后续对话不再使用这条理解。"""
        found = context_store.delete_entry(entry_id)
        return {"ok": found, "found": found}

    @app.post("/api/context/{entry_id}/restore")
    def restore_context(entry_id: str) -> dict:
        found = context_store.restore_entry(entry_id)
        return {"ok": found, "found": found}

    @app.get("/api/context/history")
    def context_history(limit: int = Query(100, ge=1, le=500)) -> dict:
        """变更历史：它什么时候学到了/被纠正了什么。"""
        return {"items": context_store.history(limit)}

    @app.get("/api/context/export")
    def export_context() -> JSONResponse:
        data = context_store.export_all()
        return JSONResponse(
            content=data,
            headers={
                "Content-Disposition": "attachment; filename=personal_ai_context.json"
            },
        )

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
