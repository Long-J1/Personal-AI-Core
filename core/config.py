"""Personal AI Core 全局配置。

所有可调参数集中在这里；环境变量可覆盖（方便以后换机器/换模型）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 项目根目录（本文件在 core/ 下）
ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("PAI_DATA_DIR") or (ROOT / "data"))
THUMB_DIR = DATA_DIR / "thumbs"
DB_PATH = DATA_DIR / "personal_ai.db"
LOG_DIR = ROOT / "logs"

# 本机 Ollama 里实测可看图的模型（见 docs/decisions/D002）
DEFAULT_MODEL = "hf.co/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive:Q4_K_M"


@dataclass
class Settings:
    # 模型
    ollama_url: str = os.environ.get("PAI_OLLAMA_URL", "http://127.0.0.1:11434")
    model: str = os.environ.get("PAI_MODEL", DEFAULT_MODEL)
    understand_timeout: float = float(os.environ.get("PAI_UNDERSTAND_TIMEOUT", "180"))
    recall_timeout: float = float(os.environ.get("PAI_RECALL_TIMEOUT", "180"))

    # 服务
    host: str = os.environ.get("PAI_HOST", "127.0.0.1")
    port: int = int(os.environ.get("PAI_PORT", "8000"))

    # 记忆与回忆
    recall_top_k: int = int(os.environ.get("PAI_RECALL_TOP_K", "8"))
    min_importance: float = float(os.environ.get("PAI_MIN_IMPORTANCE", "0.0"))
    thumb_max: int = 320          # 缩略图最长边（最小化保存原始数据）

    # 对话与 Personal Context（V0.2）
    chat_history_max: int = 20            # 进入提示词的对话历史条数
    context_max_entries: int = 40         # 进入提示词的理解条目上限
    chat_events_max: int = 8              # 对话时携带的近期事件条数
    chat_events_total: int = 14           # 近期 + 话题命中合并后的事件总上限
    deposit_max_new: int = 3              # 单轮最多新增几条理解（防模型刷屏）
    deposit_max_chars: int = 200          # 单条理解内容长度上限

    # 日志
    log_level: str = os.environ.get("PAI_LOG_LEVEL", "INFO")

    version: str = "0.2.0"


settings = Settings()


def ensure_dirs() -> None:
    """创建数据/日志目录（幂等）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
