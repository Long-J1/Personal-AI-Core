"""升级守卫：程序换版本时，先给用户数据留一份底。

为什么需要
----------
用户数据在 `%APPDATA%\\Personal AI\\data`，程序在 `%LOCALAPPDATA%\\Programs`。
这两者本该完全解耦——安装/升级只碰后者。但人总会遇到：

* 误装了另一个版本，schema 或逻辑变了；
* 系统清理工具动了 AppData；
* 一次 import 之后数据库结构不对了；
* 更根本的：**用户不信任"更新一下我的数据还在吗"**。

所以每次检测到"程序版本变了"，就自动在
`%APPDATA%\\Personal AI\\data\\backups\\` 下留一份 WAL 安全快照。

三条硬约束
----------
1. **绝不阻塞启动。** 备份失败只记日志，App 照常起来。
2. **绝不产生垃圾堆积。** 滚动保留 `KEEP_UPGRADE_BACKUPS` 份，
   多出来的按时间删最旧的。绝不做"每次更新都留一份"。
3. **绝不碰主库。** 只读源、另写目标。用 SQLite backup API 而不是
   文件复制——服务可能在写，直接 copy 会拷到一个撕裂的库。

**只在版本真的变了时才备份**：同一版本反复启动（正常情况）一次都不做。
否则"每次打开都生成一份备份"就变成了垃圾制造机。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("core.upgrade_guard")

# 滚动保留几份升级备份。3 份足够回退最近三次更新，
# 又不会在磁盘上堆出十几份几百 MB 的库。
KEEP_UPGRADE_BACKUPS = 3

_STATE_FILE = "last_version.json"
_BACKUPS_DIR = "backups"
_PREFIX = "pre-upgrade"


def _read_stamp(data_dir: Path) -> str:
    p = data_dir / _STATE_FILE
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    return str(raw.get("version", "")) if isinstance(raw, dict) else ""


def _write_stamp(data_dir: Path, version: str) -> None:
    p = data_dir / _STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": version, "at": time.time()},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _prune(data_dir: Path) -> int:
    """滚动保留：只留最新的 KEEP_UPGRADE_BACKUPS 份，返回删掉几份。"""
    bak = data_dir / _BACKUPS_DIR
    if not bak.is_dir():
        return 0
    files = sorted(bak.glob(f"{_PREFIX}-*.db"), reverse=True)
    removed = 0
    for old in files[KEEP_UPGRADE_BACKUPS:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def on_startup(*, data_dir: Path, db_path: Path, version: str) -> dict:
    """Core 启动时调用一次。返回本次动作（给日志和诊断用）。

    **永不抛异常。** 数据是用户的东西，备份失败也不能拦住他打开软件。
    """
    out = {"version": version, "backed_up": False, "filename": "",
           "reason": "", "pruned": 0}
    try:
        data_dir = Path(data_dir)
        last = _read_stamp(data_dir)

        # 第一次运行：只记版本，不备份。
        # 理由：这时库里很可能还是空的（或者用户刚导入的数据），
        # 备份一份空气没有意义，还会占掉一份滚动配额。
        if not last:
            _write_stamp(data_dir, version)
            out["reason"] = "首次记录版本，本次不备份"
            return out

        if last == version:
            out["reason"] = "版本未变"
            return out

        # 版本真的变了 —— 备份
        bak_dir = data_dir / _BACKUPS_DIR
        bak_dir.mkdir(parents=True, exist_ok=True)
        name = f"{_PREFIX}-{time.strftime('%Y%m%d-%H%M%S')}-{last}.db"
        target = bak_dir / name
        if Path(db_path).exists():
            # SQLite backup API：源照常被读写也不会拷到撕裂状态
            src = sqlite3.connect(str(db_path), timeout=10)
            try:
                dst = sqlite3.connect(str(target), timeout=10)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            out["backed_up"] = True
            out["filename"] = name
            log.info("升级前已备份用户数据：%s（来自 %s → %s）", name, last, version)
        else:
            out["reason"] = "数据库还不存在，无需备份"

        out["pruned"] = _prune(data_dir)
        _write_stamp(data_dir, version)
        return out
    except Exception as exc:                     # noqa: BLE001
        # 备份失败绝不拦住启动
        log.warning("升级前备份失败（忽略，不影响启动）：%s", exc)
        out["reason"] = f"备份失败：{exc}"
        return out


def list_upgrade_backups(data_dir: Path) -> list[dict]:
    """列出升级备份（供 UI「数据与备份」显示）。"""
    bak = Path(data_dir) / _BACKUPS_DIR
    if not bak.is_dir():
        return []
    out = []
    for p in sorted(bak.glob(f"{_PREFIX}-*.db"), reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "size": st.st_size,
                    "mtime": st.st_mtime})
    return out