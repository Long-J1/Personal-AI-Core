"""Personal AI Core V0.1 启动入口。

用法：python run.py   （浏览器会自动打开 http://127.0.0.1:8000）
"""
from __future__ import annotations

import logging
import threading
import webbrowser

import uvicorn

from core.config import ensure_dirs, settings
from core.logging_setup import setup_logging


def main() -> None:
    ensure_dirs()
    setup_logging(settings.log_level)
    log = logging.getLogger("run")

    url = f"http://{settings.host}:{settings.port}"
    log.info("Personal AI Core V%s 启动中… %s", settings.version, url)
    log.info("模型：%s @ %s", settings.model, settings.ollama_url)

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "interfaces.webapp:app",
        host=settings.host,
        port=settings.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
