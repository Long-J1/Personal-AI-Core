"""打包版 Personal AI Core 入口：只起服务，不开浏览器。

用法：仅供桌面壳拉起（D011）/ 调试打包产物。开发期请用 `python run.py`。
"""
from __future__ import annotations

import uvicorn

from core.config import ensure_dirs, settings
from core.logging_setup import setup_logging


def main() -> None:
    ensure_dirs()
    setup_logging(settings.log_level)
    uvicorn.run(
        "interfaces.webapp:app",
        host=settings.host,
        port=settings.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
