"""事件记忆的标准数据结构（任务书 §7：事件记忆）。

任何感知结果（图片、摄像头、未来的声音）都必须先转成 Event 才能进入记忆库。
这个结构就是整个系统的"通用词汇"，未来加 ASR/TTS/设备时不能推翻它。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Event(BaseModel):
    """一次"发生了什么"的结构化记录。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    source: str = "unknown"        # image_upload / camera / manual / 未来 asr...
    scene: str = ""                # 场景：书房、厨房、地铁...
    activity: str = ""             # 用户可能正在做什么
    objects: list[str] = Field(default_factory=list)   # 出现的物品/人物
    description: str = ""          # 一句话概括
    importance: float = 0.5        # 0~1：是否值得记忆/介入的基础信号
    facts: list[str] = Field(default_factory=list)     # 提炼出的长期事实
    raw_understanding: str | None = None   # 模型原始输出（可解释、可排错）
    schema_version: int = 1
    image_ref: str | None = None   # data/ 下的缩略图相对路径（不存原图）

    # ---- 查询与展示辅助 ----
    @property
    def created_at_epoch(self) -> float:
        return self.created_at.timestamp()

    @property
    def time_label(self) -> str:
        return self.created_at.strftime("%H:%M")

    @property
    def date_label(self) -> str:
        return self.created_at.strftime("%Y-%m-%d")

    def context_line(self) -> str:
        """给"回忆"提示词用的一行摘要（- [日期 时间] 描述（细节））。"""
        objs = "、".join(self.objects[:8])
        parts = [
            f"- [{self.date_label} {self.time_label}] {self.description or '（无描述）'}",
            f"场景：{self.scene or '未知'}",
            f"活动：{self.activity or '未知'}",
        ]
        if objs:
            parts.append(f"物品：{objs}")
        parts.append(f"重要度 {self.importance:.1f}")
        return "（".join([parts[0], "；".join(parts[1:])]) + "）"

    def to_public_dict(self) -> dict:
        """API 输出用（不吐 raw_understanding 之外的内部字段按需保留）。"""
        return self.model_dump()
