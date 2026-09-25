"""统一日志：控制台 + logs/pai.log 滚动文件（任务书验收项"有日志"）。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """初始化根日志（幂等，重复调用只生效一次）。"""
    # Windows 控制台（GBK）下避免中文/emoji 打印报错
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    root = logging.getLogger()
    if getattr(root, "_pai_configured", False):
        root.setLevel(level)
        return

    root.setLevel(level)
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_DIR / "pai.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # 文件不可写不能把程序搞挂
        logging.getLogger(__name__).warning("日志文件创建失败，仅输出到控制台：%s", exc)

    # 降噪
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    root._pai_configured = True  # type: ignore[attr-defined]
