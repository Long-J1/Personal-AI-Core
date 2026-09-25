"""开发期沉默记账（PLAN C1 / PRODUCT_TRUTH §7 克制）。

记录"观察 → 判断不介入 → 理由"，供开发审计：
- 只写开发日志 `logs/silence_audit.jsonl`；
- **绝不写入正式记忆库**（events/facts/context 都不碰）；
- 产品形态不保留普通沉默——本文件是开发期脚手架，未来可整体移除。
"""
from __future__ import annotations

import json
import logging
import threading

from .config import LOG_DIR

log = logging.getLogger("core.audit")

SILENCE_LOG = LOG_DIR / "silence_audit.jsonl"
_lock = threading.Lock()


def audit_silence(record: dict, path=None) -> None:
    """追加一行 JSON。写失败只告警，绝不影响主管线。"""
    target = path or SILENCE_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _lock, open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 —— 记账绝不能拖垮主流程
        log.warning("沉默记账写入失败（不影响主流程）：%s", exc)
