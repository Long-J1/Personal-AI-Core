"""Web 界面 + JSON API（FastAPI，见 docs/decisions/D001）。

页面在 interfaces/static/index.html；所有端点都在 127.0.0.1 本地使用。
通过 create_app() 注入依赖，方便测试用 Mock 模型替换。
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Literal
import re
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from core.config import DATA_DIR, DB_PATH, THUMB_DIR, ensure_dirs, settings
from core.context import Recaller
from core.conversation import ConversationService
from core.data_manager import DataManager, DataError
from core.pipeline import CorePipeline
from core.reflection import build_weekly_digest
from core.service_state import ServiceState, default_service_state, identity_of
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.hypothesis_store import HypothesisStore
from memory.goal_store import GoalStore
from memory.pattern_store import PatternStore
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


class BackgroundVerdictRequest(BaseModel):
    """用户对后台推测的裁决。"""

    verdict: Literal["correct", "wrong"]
    note: str = ""


class HypothesisRejectRequest(BaseModel):
    reason: str = Field(default="", max_length=200)


# ---------- 模型设置（D009） ----------
class OllamaCfg(BaseModel):
    url: str | None = None
    model: str | None = None


class OpenAICfg(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None   # None=不修改；""=清除


class LlamaCppCfg(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None   # None=不修改；""=清除
    # 一键启动用。用户不填也能连（自己在别处起 llama-server）。
    exe: str | None = None
    model_path: str | None = None
    port: int | None = None
    ctx: int | None = None
    parallel: int | None = None
    # ngl: None=不修改；""=清空=不传 -ngl（让 llama.cpp 自动适配显存）
    ngl: str | None = None
    extra_args: str | None = None


class ModelSettingsRequest(BaseModel):
    provider: str | None = None          # ollama | openai | llamacpp
    ollama: OllamaCfg | None = None
    openai: OpenAICfg | None = None
    llamacpp: LlamaCppCfg | None = None


class BetaUnlockRequest(BaseModel):
    code: str = ""                      # Beta 体验码（只在本机比对，不上报）


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
    hypothesis_store: HypothesisStore | None = None,
    pattern_store: PatternStore | None = None,
    goal_store: GoalStore | None = None,
    service_state=None,
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
    # V0.3：假设与记忆同库（零配置）；测试可注入独立实例
    hypothesis_store = hypothesis_store or HypothesisStore(store.db_path)
    # V0.5：规律与目标默认同库；测试可注入独立实例
    pattern_store = pattern_store or PatternStore(store.db_path)
    goal_store = goal_store or GoalStore(store.db_path)
    pipeline = CorePipeline(store, model)
    recaller = Recaller(store, model)
    conversation = ConversationService(
        store, context_store, model, chat_store=chat_store,
        hypothesis_store=hypothesis_store,
        pattern_store=pattern_store,
        goal_store=goal_store,
        service_state=service_state,
    )
    # 服务状态缓存：chat() 写、status 读、模型设置变更时重置。
    # 生产用进程内单例（Core 只有一份模型配置）；测试可注入独立实例，避免互相污染。
    service_state = service_state or default_service_state()

    def _safe_clear(pat_store) -> int:
        try:
            return pat_store.clear_all()
        except Exception as exc:
            log.warning("清空规律失败：%s", exc)
            return 0

    def _safe_hyp_count() -> int | None:
        """假设计数失败不许掀翻健康探针（status 是壳子的体温计）。"""
        try:
            return hypothesis_store.count_live()
        except Exception as exc:
            log.warning("假设计数失败：%s", exc)
            return None

    app = FastAPI(title="Personal AI Core", version=settings.version)
    # 暴露给 app.state：诊断 / 测试要看"这台 app 正在跑的那个 conversation"
    app.state.conversation = conversation
    # 我们启动过的 llama-server，按 base_url 索引。**只装我们自己起的**——
    # 用户在命令行里跑的不进这里，stop 也就永远碰不到它们。
    # 挂 app.state 而不是局部变量：不然测试和调试都够不着，也容易误加成全局。
    app.state.llamacpp_managed = {}

    # V0.5.9：用户停止操作后，本地做一次后台整理。
    # 只在真实运行时启动（默认关，测试与脚本不受影响）。
    idle_worker = None
    if getattr(settings, "idle_reasoning_enabled", False):
        from core.idle_worker import IdleWorker

        idle_worker = IdleWorker(store, hypothesis_store, chat_store)
    app.state.idle_worker = idle_worker

    @app.on_event("startup")
    async def _start_idle_worker():
        if idle_worker is None:
            return
        try:
            idle_worker.start()
        except Exception as exc:
            log.warning("idle worker 启动失败（不影响服务）：%s", exc)

    @app.on_event("shutdown")
    async def _shutdown():
        """应用退出：取消后台任务，不产生孤儿。"""
        if idle_worker is not None:
            try:
                await idle_worker.stop()
            except Exception as exc:
                log.warning("关闭时停止 idle worker 失败：%s", exc)
        try:
            await conversation.shutdown()
        except Exception as exc:
            log.warning("关闭时取消后台沉淀失败：%s", exc)

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
        """健康探针：**纯本地**，一个包都不发。

        这里曾经调 model.ping() → GET {base}/models，于是页面每 15 秒一次
        跨网往返（640~821ms），网络一慢就冲破探测线，壳子会误判 Core 没起来。
        现在只读「最近一次真实调用」的结果缓存（core/service_state.py），
        由聊天成功/失败、用户主动测试连接、模型设置变更这三处写入。
        """
        snap = service_state.snapshot(identity_of(model))
        state = snap["state"]
        return {
            "version": settings.version,
            "model": getattr(model, "model", getattr(model, "name", "unknown")),
            "provider": _describe(model)["provider"],
            "events": store.count_events(),
            "context_entries": context_store.count_active(),
            "hypotheses": _safe_hyp_count(),
            # 老字段保留（前端三态徽章用）：unknown → None，向后兼容
            "model_service_ok": True if state == "ok" else (
                False if state == "error" else None),
            # 新字段：三态原文 + 上次真实调用的耗时/时间（不再有"ping 耗时"）
            "model_service_state": state,
            "model_service_ms": snap["elapsed_ms"],
            "model_service_checked_at": snap["updated_at"],
            "model_service_detail": snap["detail"],
            "model_service_provider": snap["provider"],
            "model_service_model": snap["model"],
            "sessions": chat_store.count_sessions(),
            "paused": store.paused,
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
        # 换了模型 → 旧状态一律作废（不然上一个模型的"在线"会污染新模型）
        service_state.reset(detail="模型设置已变更，等待下一次真实调用")
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
        # 优先用结构化判定（reachable / model_ok 分开），它才是模型可用性的
        # 唯一来源；没有就退回老的 test_connection 二元结果。
        checker = getattr(adapter, "check_service", None)
        check = None
        if callable(checker):
            try:
                t0 = time.perf_counter()
                check = checker(timeout=8.0)
                elapsed = round((time.perf_counter() - t0) * 1000)
            except Exception as exc:  # 连接测试绝不许把服务打崩
                check = None
                ok, detail = False, f"测试失败：{exc}"
                elapsed = None
        if check is not None:
            # 用户可见的 ok 沿用各 Provider 的既有约定（check.test_ok 就是
            # 当年 test_connection 会返回的那个值）——那是产品行为，本次不动，
            # 而且零额外网络请求。
            ok, detail = check.test_ok, check.message
        else:
            tester = getattr(adapter, "test_connection", None)
            if not callable(tester):
                return {"ok": False, "detail": "该 Provider 不支持连接测试"}
            try:
                t0 = time.perf_counter()
                ok, detail = tester(timeout=8.0)
                elapsed = round((time.perf_counter() - t0) * 1000)
            except Exception as exc:  # 连接测试绝不许把服务打崩
                ok, detail, elapsed = False, f"测试失败：{exc}", None

        # 缓存用**严格**判定：端点通但当前模型不可用 → error（不写 ok）
        healthy = check.healthy if check is not None else bool(ok)
        # 测的是「当前生效配置」才写缓存：拿临时配置去测不该改徽章
        if identity_of(adapter) == identity_of(model):
            service_state.record(
                "ok" if healthy else "error",
                identity=identity_of(adapter),
                elapsed_ms=elapsed, detail=detail)
        return {"ok": bool(ok), "detail": detail,
                "provider": cfg["provider"],
                "model": getattr(adapter, "model", ""),
                "service_reachable": getattr(check, "reachable", None),
                "service_model_ok": getattr(check, "model_ok", None),
                "service_healthy": healthy}

    # ---------- 本地 llama.cpp：扫盘 + 一键启动 ----------
    # 只做三件事：帮忙找到 llama-server.exe / *.gguf，按填的参数把服务拉起来，
    # 以及查状态。不参与推理、不改任何用户数据。

    @app.get("/api/llamacpp/scan")
    def llamacpp_scan() -> dict:
        """扫本机的 llama-server.exe 和可用 gguf。

        扫不到就如实说扫不到，让用户手填——绝不猜一个路径上去。
        """
        from models import llamacpp_adapter as lc
        try:
            found_exe = lc.find_exe()
            found_models = lc.find_gguf()
        except Exception as exc:                      # 扫盘永远不该掀翻服务
            log.warning("llama.cpp 扫盘失败：%s", exc)
            return {"ok": False, "error": f"扫描失败：{exc}",
                    "exe": "", "models": []}
        return {"ok": True,
                "exe": found_exe,
                "exe_found": bool(found_exe),
                "models": found_models}

    def _llamacpp_manager(patch: dict | None = None):
        """按配置造一个启动器。

        `patch` 给了就用它（允许"填完直接点启动，不先保存"），
        但**一律不落盘**——没点保存就没改配置。
        """
        from models.llamacpp_adapter import LlamaCppServer
        cfg = (model_settings_store.resolve(patch) if patch
               else model_settings_store.load())
        c = cfg.get("llamacpp", {})
        ngl_raw = (c.get("ngl") or "").strip()
        try:
            ngl = int(ngl_raw) if ngl_raw else None
        except ValueError:
            ngl = None
        return LlamaCppServer(
            exe=c.get("exe", ""), model=c.get("model_path", ""),
            port=int(c.get("port") or 8080), ctx=int(c.get("ctx") or 4096),
            ngl=ngl, parallel=int(c.get("parallel") or 1),
            extra_args=c.get("extra_args", ""),
            base_url=c.get("base_url", ""))

    @app.get("/api/llamacpp/status")
    def llamacpp_status() -> dict:
        """服务是否在跑。注意**区分是不是我们起的**——用户自己起的不能动。"""
        try:
            cfg = model_settings_store.load()
            base = ((cfg.get("llamacpp") or {}).get("base_url") or ""
                    ).strip() or "http://127.0.0.1:8080"
            managed = app.state.llamacpp_managed.get(base)
            if managed is None:
                # 没经过我们启动 → 只探不动。
                from models.llamacpp_adapter import LlamaCppServer
                return {"ok": True, "managed": False,
                        **LlamaCppServer(base_url=base).status()}
            return {"ok": True, "managed": True, **managed.status()}
        except Exception as exc:                      # 状态查询绝不许把服务打崩
            return {"ok": False, "error": str(exc)}

    @app.post("/api/llamacpp/start")
    def llamacpp_start(req: LlamaCppCfg | None = None) -> dict:
        """一键启动。等模型加载完（大模型可能几十秒到几分钟），返回真实结果。"""
        patch = None
        if req is not None:
            dumped = req.model_dump(exclude_none=True)
            if dumped:
                patch = {"provider": "llamacpp", "llamacpp": dumped}
        try:
            srv = _llamacpp_manager(patch)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        if not srv.exe:
            return {"ok": False, "error": "还没填 llama-server.exe 的位置。"
                                          "可以先点「自动查找」让 App 扫一下。"}
        if not srv.model:
            return {"ok": False, "error": "还没选模型文件（.gguf）。"}

        # 已在同一个地址上跑着我们自己起的服务 → **沿用那个对象**。
        # 换成新的会把 proc 句柄丢掉，之后 ours 永远变 False，
        # 于是「App 起的」和「你自己起的」再也分不开，
        # 而这条铁律正是"绝不关掉用户的进程"的唯一依据。
        # 真要换模型/换参数时旧的进程还活着，那是用户自己点停止的事。
        existing = app.state.llamacpp_managed.get(srv.base_url)
        if existing is not None and existing.ours and existing.alive():
            srv = existing
        else:
            app.state.llamacpp_managed[srv.base_url] = srv
        try:
            res = srv.start()
        except Exception as exc:                      # 启动绝不许把服务打崩
            return {"ok": False, "error": f"启动失败：{exc}"}
        if res.get("ok"):
            # 起来了就把服务状态清掉——下次真实调用重新测，避免旧缓存说谎。
            service_state.reset(detail="llama.cpp 刚启动，等待下一次真实调用")
        return res

    @app.post("/api/llamacpp/stop")
    def llamacpp_stop() -> dict:
        """只停我们自己起的进程。外部起的（用户命令行里跑的）一律不动。"""
        try:
            for base in list(app.state.llamacpp_managed.keys()):
                srv = app.state.llamacpp_managed[base]
                if not srv.ours:
                    continue
                res = srv.stop()
                app.state.llamacpp_managed.pop(base, None)
                if not res.get("ok"):
                    return res
            return {"ok": True,
                    "detail": "没有由 App 启动的 llama-server"
                              "（你在别处起的我不关）"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.on_event("shutdown")
    async def _shutdown_llamacpp():
        """App 退出时收掉我们起的 llama-server。"""
        for srv in list(app.state.llamacpp_managed.values()):
            try:
                if srv.ours:
                    srv.stop()
            except Exception as exc:              # 永不掀翻退出
                log.warning("停止 llama-server 失败（忽略）：%s", exc)
        app.state.llamacpp_managed.clear()

    @app.on_event("startup")
    async def _startup():
        """启动时两件事，顺序有讲究：

        1. **升级守卫**：检测到程序版本变了就先备份用户数据。
           必须排在 Beta 套用**之前** —— 万一 Beta 预设套用把
           model_settings.json 写坏了，那是在用户数据上动手。
           先留底再动。
        2. Beta 体验活跃但配置被外部改过 → 重新套用预设。
           没有活跃的 Beta 状态就什么都不做，绝不碰用户自己的配置。
        """
        try:
            from core import upgrade_guard
            res = upgrade_guard.on_startup(
                data_dir=Path(DATA_DIR), db_path=store.db_path,
                version=getattr(settings, "version", "unknown"))
            if res.get("backed_up"):
                log.info("升级前备份：%s", res["filename"])
        except Exception as exc:      # 永不掀翻启动
            log.warning("升级守卫失败（忽略）：%s", exc)
        try:
            from core import beta
            beta.apply_on_startup(model_settings_store)
        except Exception as exc:      # 永不掀翻启动
            log.warning("Beta 预设套用失败（忽略）：%s", exc)

    # ---------- Beta 体验（V0.5.5 新手入门层） ----------
    # 工程捷径：让第一次用的人不用碰 Base URL / Model / API Key。
    # 不是授权系统：没有服务端校验、没有吊销，凭证在安装包里可被提取。

    @app.get("/api/beta/status")
    def beta_status() -> dict:
        """Beta 体验是否可用 / 是否已激活（前端据此显示入口）。"""
        try:
            from core import beta
            return {"ok": True, **beta.state()}
        except Exception as exc:      # pragma: no cover - 永不掀翻页面
            log.warning("Beta 状态读取失败：%s", exc)
            return {"ok": False, "available": False, "active": False}

    @app.post("/api/beta/unlock")
    def beta_unlock(req: BetaUnlockRequest) -> dict:
        """输入体验码 → 套用预设配置 → 自动连接测试。

        失败时只回中性文案，不暴露端点 / 模型 / Key 任何细节。
        """
        from core import beta
        ok, detail = beta.unlock(model_settings_store, (req.code or "").strip())
        if not ok:
            service_state.reset(detail="Beta 体验未解锁")
            return {"ok": False, "detail": detail}
        # 换了模型 → 旧状态作废（与手动改设置同一套语义）
        if isinstance(model, ModelRegistry):
            model.reload()
        service_state.reset(detail="Beta 体验已解锁，等待首次真实调用")
        # 自动连接测试（结果写缓存，用户不用自己点）
        try:
            tr = test_model_settings(ModelSettingsRequest())
            # 用 ok（产品口径，等价于当年 test_connection 的返回值），
            # 不用 service_healthy（严格内部标志：模型必须在 /models 清单里）。
            # 后者对「能直接指定模型名」的服务是假阴性——实测智谱的
            # glm-4-flash 不在清单里，但真实聊天完全正常（TTFT 1.47s）。
            # 用错标志会让用户解锁后看到「连接没通过」，以为 Beta 失败。
            healthy = bool(tr.get("ok"))
            detail = ("已准备好，可以开始聊天" if healthy
                      else "已解锁，但连接没通过，可以到设置里查看")
            return {"ok": True, "detail": detail,
                    "connection_ok": healthy,
                    "connection_detail": tr.get("detail", ""),
                    "provider": tr.get("provider"),
                    "model": tr.get("model")}
        except Exception as exc:      # pragma: no cover
            log.warning("Beta 解锁后自动测试失败：%s", exc)
            return {"ok": True, "detail": "已解锁，可以开始聊天",
                    "connection_ok": None}

    @app.post("/api/beta/disable")
    def beta_disable() -> dict:
        """关闭 Beta 体验：还原解锁前的模型配置。

        只碰模型设置——Memory、聊天历史、数据库一概不动。
        """
        from core import beta
        ok, detail = beta.disable(model_settings_store)
        if ok and isinstance(model, ModelRegistry):
            model.reload()
            service_state.reset(detail="已关闭 Beta 体验")
        return {"ok": ok, "detail": detail}

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

    @app.post("/api/conversation/stream")
    async def post_conversation_stream(req: ConversationRequest):
        """流式对话（SSE）：data: {"delta"}×N → data: {"done","reply"/"error"}。

        async 直驱（无线程池切换）：首字即达。沉淀在流结束后同步跑
        （与非流式同语义）。非流式端点原样保留。
        """
        async def gen():
            try:
                async for event in conversation.stream_chat(
                        req.message, req.session_id):
                    yield "data: " + json.dumps(
                        event, ensure_ascii=False) + "\n\n"
            except ModelError as exc:
                log.error("流式对话失败：%s", exc)
                yield "data: " + json.dumps(
                    {"error": f"模型暂不可用：{exc}"},
                    ensure_ascii=False) + "\n\n"
            except Exception as exc:
                log.error("流式沉淀异常：%s", exc)
                yield "data: " + json.dumps(
                    {"error": f"沉淀异常，已跳过：{exc}"},
                    ensure_ascii=False) + "\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

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
        if ok:
            # V0.3：手动纠正同样联动假设（只走对话沉淀会漏掉这条路）
            try:
                hypothesis_store.record_correction_hit(
                    entry_id, req.content, reason="用户手动编辑")
            except Exception as exc:
                log.warning("手动纠正联动假设失败（跳过）：%s", exc)
            # V0.5.2（D017 ②）：手动纠正同样联动规律（吃反证）
            try:
                pattern_store.cascade_from_source(
                    "context", entry_id, "用户手动编辑")
            except Exception as exc:
                log.warning("手动纠正联动规律失败（跳过）：%s", exc)
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
        # V0.3：理解导出顺带假设（同属"它对你的理解"，分开两节）
        try:
            data["hypotheses"] = hypothesis_store.export_all()
        except Exception as exc:
            log.warning("假设导出失败（理解部分不受影响）：%s", exc)
        return JSONResponse(
            content=data,
            headers={
                "Content-Disposition": "attachment; filename=personal_ai_context.json"
            },
        )

    # ---------- 假设管理（V0.3：事实与假设视觉分开，假设永远是"待验证"）----------
    @app.get("/api/hypotheses")
    def list_hypotheses() -> dict:
        items = hypothesis_store.list_hypotheses()
        return {
            "total": len(items),
            "items": [h.to_public_dict() for h in items],
        }

    @app.get("/api/hypotheses/{hypo_id}")
    def get_hypothesis(hypo_id: str) -> dict:
        detail = hypothesis_store.get_detail(hypo_id)
        if detail is None:
            return JSONResponse({"ok": False, "error": "假设不存在"},
                                status_code=404)
        return {"ok": True, "hypothesis": detail}

    @app.post("/api/hypotheses/{hypo_id}/confirm")
    def confirm_hypothesis(hypo_id: str) -> dict:
        """用户确认：只改状态，不动置信度（确认 ≠ 系统很确定）。"""
        return {"ok": hypothesis_store.confirm(hypo_id)}

    @app.post("/api/hypotheses/{hypo_id}/reject")
    def reject_hypothesis(hypo_id: str, req: HypothesisRejectRequest) -> dict:
        """用户否决：建反证 + 置信度压到 0.25 + 状态 rejected。"""
        return {"ok": hypothesis_store.reject(hypo_id, req.reason,
                                              channel="trust_ui")}

    @app.delete("/api/hypotheses/{hypo_id}")
    def delete_hypothesis(hypo_id: str) -> dict:
        """删除即失效：后续对话不再使用这条假设。"""
        found = hypothesis_store.delete_entry(hypo_id)
        return {"ok": found, "found": found}

    @app.post("/api/hypotheses/{hypo_id}/restore")
    def restore_hypothesis(hypo_id: str) -> dict:
        found = hypothesis_store.restore_entry(hypo_id)
        return {"ok": found, "found": found}

    # ---------- 待验证候选（产品语言投影）----------
    @app.get("/api/candidates")
    def get_candidates() -> dict:
        """把候选猜测翻译成用户看得懂的话。

        与 /api/hypotheses 的区别：那个是诊断面（置信度、scope、证据来源
        都是给检查系统的人看的），这个是**体验面**——只有人话、时间、
        和用户自己的原话。

        主列表只放需要用户注意的（待验证）；已认可/已处理的收进折叠区，
        不长期占位置。
        """
        from core.candidate_view import (
            assert_no_internal_fields,
            build_view,
        )

        details = {}
        try:
            rows = hypothesis_store.list_hypotheses(live_only=False)
        except Exception as exc:
            log.warning("读取候选失败（待验证面板可能为空）：%s", exc)
            rows = []
        for h in rows:
            try:
                d = hypothesis_store.get_detail(h.id)
            except Exception as exc:
                log.warning("读取候选详情失败（跳过 %s）：%s", h.id[:8], exc)
                continue
            if d:
                details[h.id] = d
        try:
            view = build_view(rows, details)
        except Exception as exc:
            log.warning("待验证投影失败（返回空面板，不掀翻页面）：%s", exc)
            return {"pending": [], "watching": [], "settled": [],
                    "counts": {"pending": 0, "watching": 0, "settled": 0}}
        # 泄漏自检：产品面出现内部字段即记 ERROR
        for k in ("pending", "watching", "settled"):
            for leak in assert_no_internal_fields(view[k]):
                log.error("待验证泄漏内部字段 [%s]：%s", k, leak)
        return view

    # ---------- 第一次被理解（V0.5.7：产品语言的"我眼中的你"）----------
    @app.get("/api/understanding")
    def get_understanding() -> dict:
        """把内部理解翻译成用户看得懂的话。

        与 /api/hypotheses 的区别：那个是诊断面（给要看置信度的人），
        这个是体验面——**不含任何内部字段**，只有用户自己的话、时间、
        和 AI 的说法。展示层不做任何猜测，缺证据的项根本不出现。
        """
        from core.understanding_view import (
            assert_no_internal_fields,
            build_timeline,
            onboarding_state,
        )

        details = []
        try:
            for h in hypothesis_store.list_hypotheses():
                d = hypothesis_store.get_detail(h.id)
                if d:
                    details.append(d)
        except Exception as exc:
            log.warning("读取假设详情失败（时间线可能不完整）：%s", exc)

        entries = []
        try:
            # active_only=False：要连 outdated 一起取——用户自己的话变了，
            # 这是他最该看见的一段历史。视图层自己按 active/memory_status 分流。
            entries = [e.to_public_dict()
                       for e in context_store.list_entries(active_only=False,
                                                            limit=200)
                       if e.active]
        except Exception as exc:
            log.warning("读取理解条目失败（时间线可能不完整）：%s", exc)

        try:
            chat_count = chat_store.count_messages()
        except Exception:
            chat_count = 0

        payload = build_timeline(details, entries)
        payload["onboarding"] = onboarding_state(
            chat_count, entries, payload["count"])
        # 自检：内部字段泄漏到展示层 = 产品事故，当场发现
        leaked = assert_no_internal_fields(payload)
        if leaked:
            log.error("理解时间线泄漏内部字段：%s", leaked)
        return payload

    @app.get("/api/understanding/warmup")
    def get_warmup() -> dict:
        """首次体验的引导语。不存状态——下次还是同一段话。

        刻意不落库、不建表：引导是邀请不是关卡，记下来反而变成负担。
        """
        from core.understanding_view import (
            ONBOARDING_TOPICS,
            onboarding_state,
        )

        try:
            entries = [e.to_public_dict()
                       for e in context_store.list_entries(active_only=False,
                                                            limit=200)
                       if e.active]
            chat_count = chat_store.count_messages()
        except Exception:
            entries, chat_count = [], 0
        st = onboarding_state(chat_count, entries, 0)
        return {
            "active": st["is_first_run"] and not st["done"],
            "opening": "聊几句，让我了解你的习惯。",
            "closing": "大致清楚了，之后我会按这个来。",
            "topics": [
                {"key": k, "name": n, "ask": a, "covered": c["covered"]}
                for k, n, a in ONBOARDING_TOPICS
                for c in st["topics"] if c["key"] == k
            ],
        }

    # ---------- 我正在思考（V0.5.8：AI 的猜测，不是事实）----------
    @app.get("/api/background")
    def list_background() -> dict:
        """待用户裁决的后台推测。

        刻意与 /api/understanding 分开：那边是用户确认过的理解，
        这边是 AI 的猜测。混在一起就分不清"我知道的"和"我猜的"——
        那恰恰是这套系统唯一不能含糊的地方。
        """
        from core.background_bridge import list_background_candidates
        from core.understanding_view import assert_no_internal_fields

        items = []
        for h in list_background_candidates(hypothesis_store):
            detail = {}
            try:
                detail = hypothesis_store.get_detail(h.id) or {}
            except Exception as exc:
                log.warning("后台推测详情读取失败：%s", exc)
            history = detail.get("history") or []
            origin = {}
            for row in history:
                if row.get("action") != "create":
                    continue
                try:
                    origin = json.loads(row.get("detail") or "{}")
                except (ValueError, TypeError):
                    origin = {}
                break
            items.append({
                "id": h.id,
                "topic": origin.get("topic") or "可能的发现",
                "type": origin.get("type") or "background",
                "reason": origin.get("reason") or h.statement,
                "evidence": origin.get("evidence") or [],
                "asked_on": (h.created_at.strftime("%Y-%m-%d")
                             if hasattr(h, "created_at") else ""),
                "caveat": "这只是根据你的行为推测出的可能变化，不一定正确。",
            })
        payload = {"items": items, "total": len(items)}
        leaked = assert_no_internal_fields(payload)
        if leaked:
            log.error("后台推测泄漏内部字段：%s", leaked)
        return payload

    @app.post("/api/background/{hypo_id}/verdict")
    def background_verdict(hypo_id: str,
                           req: BackgroundVerdictRequest) -> dict:
        """用户裁决：correct 只补证据，wrong 落否决从此不再提。"""
        ok = hypothesis_store.record_background_verdict(
            hypo_id, req.verdict, req.note or "")
        if not ok:
            return JSONResponse({"ok": False, "error": "这条推测不存在或已处理"},
                                status_code=404)
        return {"ok": True, "verdict": req.verdict}

    # ---------- 规律管理（V0.5：只有 confirmed 进 prompt）----------
    @app.get("/api/patterns")
    def list_patterns() -> dict:
        items = pattern_store.list_patterns()
        return {
            "total": len(items),
            "items": [h.to_public_dict() for h in items],
        }

    @app.get("/api/patterns/{pattern_id}")
    def get_pattern(pattern_id: str) -> dict:
        detail = pattern_store.get_detail(pattern_id)
        if detail is None:
            return JSONResponse({"ok": False, "error": "规律不存在"},
                                status_code=404)
        return {"ok": True, "pattern": detail}

    @app.post("/api/patterns/{pattern_id}/confirm")
    def confirm_pattern(pattern_id: str) -> dict:
        return {"ok": pattern_store.confirm(pattern_id)}

    @app.post("/api/patterns/{pattern_id}/reject")
    def reject_pattern(pattern_id: str, req: HypothesisRejectRequest) -> dict:
        return {"ok": pattern_store.reject(pattern_id, req.reason,
                                           channel="trust_ui")}

    @app.delete("/api/patterns/{pattern_id}")
    def delete_pattern(pattern_id: str) -> dict:
        found = pattern_store.delete_entry(pattern_id)
        return {"ok": found, "found": found}

    @app.post("/api/patterns/{pattern_id}/restore")
    def restore_pattern(pattern_id: str) -> dict:
        found = pattern_store.restore_entry(pattern_id)
        return {"ok": found, "found": found}

    # ---------- 目标管理（V0.5：只收用户原话，只做关联）----------
    @app.get("/api/goals")
    def list_goals() -> dict:
        items = goal_store.list_goals()
        return {
            "total": len(items),
            "items": [g.to_public_dict() for g in items],
        }

    @app.get("/api/goals/{goal_id}")
    def get_goal(goal_id: str) -> dict:
        detail = goal_store.get_detail(goal_id, pattern_store, context_store)
        if detail is None:
            return JSONResponse({"ok": False, "error": "目标不存在"},
                                status_code=404)
        return {"ok": True, "goal": detail}

    @app.post("/api/goals/{goal_id}/confirm")
    def confirm_goal(goal_id: str) -> dict:
        return {"ok": goal_store.confirm(goal_id)}

    @app.post("/api/goals/{goal_id}/reject")
    def reject_goal(goal_id: str, req: HypothesisRejectRequest) -> dict:
        return {"ok": goal_store.reject(goal_id, req.reason)}

    @app.delete("/api/goals/{goal_id}")
    def delete_goal(goal_id: str) -> dict:
        found = goal_store.delete_entry(goal_id)
        return {"ok": found, "found": found}

    @app.post("/api/goals/{goal_id}/restore")
    def restore_goal(goal_id: str) -> dict:
        found = goal_store.restore_entry(goal_id)
        return {"ok": found, "found": found}

    @app.post("/api/goals/{goal_id}/done")
    def done_goal(goal_id: str) -> dict:
        return {"ok": goal_store.complete(goal_id)}

    @app.post("/api/goals/{goal_id}/pause")
    def pause_goal(goal_id: str) -> dict:
        return {"ok": goal_store.pause(goal_id)}

    # ---------- 周报 Reflection（V0.5：只读 digest，失败静默）----------
    @app.get("/api/reflection/weekly")
    def weekly_reflection() -> dict:
        try:
            digest = build_weekly_digest(store, context_store,
                                         hypothesis_store, pattern_store,
                                         goal_store)
        except Exception as exc:
            log.warning("周报生成失败（返回空 digest）：%s", exc)
            digest = {"lines": [], "question": "", "stats": {}}
        return {"ok": True, **digest}

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
        # "清空全部" = 失忆：假设引用的事件都没了，留着假设就是"失忆了还在猜"
        try:
            hypo_deleted = hypothesis_store.clear_all()
        except Exception as exc:
            log.warning("清空假设失败：%s", exc)
            hypo_deleted = 0
        return {"ok": True, "deleted": deleted,
                "hypotheses_deleted": hypo_deleted,
                "patterns_deleted": _safe_clear(pattern_store)}

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
