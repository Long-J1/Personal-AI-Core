"""核心管线：感知 → 理解 → 事件 → 判断 → 记忆（任务书 §4 最小闭环的前半段）。

一次 observe() 走完整条链，任何一步失败都转成明确的 skip_reason，不向上抛。
"""
from __future__ import annotations

import base64
import io
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from memory.event import Event
from memory.store import MemoryStore
from models.base import BaseModelAdapter, ModelError

from .config import THUMB_DIR, Settings, settings as default_settings
from .audit import audit_silence
from .intervention import InterventionDecision, InterventionPolicy
from .policy import MemoryPolicy
from .understanding import UNDERSTAND_PROMPT, parse_understanding

log = logging.getLogger("core.pipeline")


@dataclass
class ObserveResult:
    """一次观察的完整结果（给 API/UI 展示，也是测试断言的对象）。"""

    stored: bool
    parse_ok: bool
    skip_reason: str | None = None       # 未入库的原因（隐私暂停/模型错误/策略）
    policy_reason: str = ""
    duration_s: float = 0.0
    error: str | None = None
    event: Event | None = None
    intervention: InterventionDecision | None = None

    @property
    def ok(self) -> bool:
        return self.event is not None


def save_thumbnail(image_bytes: bytes, event_id: str, max_side: int = 320) -> str | None:
    """存一张小缩略图（最小化保存原始数据），返回 data/ 下相对路径。失败不致命。"""
    try:
        from PIL import Image

        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        rel = f"thumbs/{event_id}.jpg"
        img.save(THUMB_DIR / f"{event_id}.jpg", format="JPEG", quality=60)
        return rel
    except Exception as exc:
        log.warning("缩略图保存失败（不影响记忆）：%s", exc)
        return None


class CorePipeline:
    """Personal AI Core 的感知主循环。"""

    def __init__(
        self,
        store: MemoryStore,
        model: BaseModelAdapter,
        *,
        policy: MemoryPolicy | None = None,
        intervention: InterventionPolicy | None = None,
        settings: Settings | None = None,
    ):
        self.store = store
        self.model = model
        self.settings = settings or default_settings
        self.policy = policy or MemoryPolicy(self.settings.min_importance)
        self.intervention = intervention or InterventionPolicy()

    def observe(
        self,
        image_bytes: bytes,
        *,
        source: str = "image_upload",
        at: datetime | None = None,   # 测试可指定事件时间；正常不传=现在
    ) -> ObserveResult:
        started = time.perf_counter()

        # 0) 隐私闸门：暂停时连模型都不调（最小化处理）
        if self.store.paused:
            reason = "隐私暂停中：本次未调用模型、未记录"
            log.info("观察跳过：%s", reason)
            return ObserveResult(stored=False, parse_ok=False, skip_reason=reason)

        # 1) 感知已由调用方完成，这里做理解
        b64 = base64.b64encode(image_bytes).decode("ascii")
        try:
            raw = self.model.chat(
                [
                    {"role": "system", "content": UNDERSTAND_PROMPT},
                    {"role": "user", "content": "观察这张图片，按系统提示输出 JSON。"},
                ],
                images=[b64],
                timeout=self.settings.understand_timeout,
            )
        except ModelError as exc:
            log.error("模型调用失败：%s", exc)
            return ObserveResult(
                stored=False,
                parse_ok=False,
                skip_reason=f"模型错误：{exc}",
                error=str(exc),
                duration_s=time.perf_counter() - started,
            )
        except Exception as exc:  # 防御：任何意外都不能炸管线
            log.exception("观察流程内部错误")
            return ObserveResult(
                stored=False,
                parse_ok=False,
                skip_reason=f"内部错误：{exc}",
                error=str(exc),
                duration_s=time.perf_counter() - started,
            )

        # 2) 理解结果 → 标准事件
        parsed = parse_understanding(raw, source=source, now=at)
        event = parsed.event
        event.image_ref = save_thumbnail(image_bytes, event.id, self.settings.thumb_max)

        # 3) 判断是否值得记忆
        decision = self.policy.should_remember(
            event, paused=self.store.paused, parse_ok=parsed.ok
        )
        stored = False
        if decision.remember:
            self.store.add_event(event)
            stored = True
        else:
            log.info("按策略不记：%s", decision.reason)

        # 4) 介入决策（V0.1 永远沉默，仅记录决策结果）
        idec = self.intervention.decide(event)
        # 5) 沉默记账（开发期审计，只进日志文件，不进记忆库——PLAN C1）
        audit_silence({
            "kind": "observation_silence",
            "epoch": time.time(),
            "iso": datetime.now().astimezone().isoformat(),
            "event_id": event.id,
            "source": event.source,
            "importance": round(float(event.importance), 3),
            "stored": stored,
            "intervene": idec.intervene,
            "reason": idec.reason,
        })
        log.info(
            "观察完成 event=%s stored=%s parse_ok=%s 耗时=%.1fs 介入=%s",
            event.id[:8],
            stored,
            parsed.ok,
            time.perf_counter() - started,
            idec.reason,
        )
        return ObserveResult(
            stored=stored,
            parse_ok=parsed.ok,
            skip_reason=None if stored else decision.reason,
            policy_reason=decision.reason,
            duration_s=time.perf_counter() - started,
            event=event,
            intervention=idec,
        )
