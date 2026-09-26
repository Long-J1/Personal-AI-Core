"""数据管理：导出 / 备份 / 恢复 / 导入 / 数据位置（桌面化阶段2，D010）。

铁律（PRODUCT_TRUTH"换电脑不能丢它"）：
- 安装目录与数据目录分离，这里的一切都发生在**数据目录**内
- 恢复前自动先给当前数据拍一张"安全网"备份，恢复永远可回滚
- 导出包 = 个人 AI 的全部身家（DB 快照 + 缩略图 + 模型设置），换电脑带上它即可复活
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("core.data")

_SQLITE_MAGIC = b"SQLite format 3\x00"
_DB_NAME = "personal_ai.db"
_SETTINGS_NAME = "model_settings.json"
_THUMBS_NAME = "thumbs"
_BACKUPS_NAME = "backups"
_MANIFEST_NAME = "manifest.json"


class DataError(Exception):
    """数据管理操作失败（面向用户的错误信息）。"""


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


class DataManager:
    """数据目录的导入导出中枢（测试可注入 tmp 数据目录）。"""

    def __init__(self, data_dir: Path, thumbs_dir: Path | None = None):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / _DB_NAME
        self.thumbs_dir = Path(thumbs_dir) if thumbs_dir else (self.data_dir / _THUMBS_NAME)
        self.backups_dir = self.data_dir / _BACKUPS_NAME

    # ---------- 位置 ----------
    def location(self) -> dict:
        total = 0
        if self.data_dir.exists():
            for p in self.data_dir.rglob("*"):
                if p.is_file() and _BACKUPS_NAME not in p.relative_to(self.data_dir).parts:
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
        return {
            "data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
            "db_exists": self.db_path.exists(),
            "thumbs_dir": str(self.thumbs_dir),
            "backups_dir": str(self.backups_dir),
            "size_bytes": total,
        }

    # ---------- 导出 ----------
    def _snapshot_db(self, dest: Path) -> None:
        """WAL 安全快照：用 SQLite backup API，导出时服务照常读写。"""
        if not self.db_path.exists():
            raise DataError("数据库不存在，没有可导出的数据")
        src = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=15)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def _write_zip(self, zip_path: Path) -> dict:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile_in(self.data_dir) as tmp_dir:
            snap = tmp_dir / _DB_NAME
            self._snapshot_db(snap)
            manifest = {
                "app": "personal-ai-core",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "kind": "personal_ai_data",
            }
            counts = self._counts()
            if counts:
                manifest["counts"] = counts
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(snap, _DB_NAME)
                zf.writestr(_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
                settings = self.data_dir / _SETTINGS_NAME
                if settings.exists():
                    zf.write(settings, _SETTINGS_NAME)
                if self.thumbs_dir.exists():
                    for p in sorted(self.thumbs_dir.rglob("*")):
                        if p.is_file():
                            zf.write(p, f"{_THUMBS_NAME}/{p.relative_to(self.thumbs_dir).as_posix()}")
        return manifest

    def _counts(self) -> dict:
        if not self.db_path.exists():
            return {}
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=15)
        try:
            out = {}
            for table in ("events", "context_entries"):
                try:
                    out[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    pass
            return out
        finally:
            con.close()

    def export(self, dest: Path | None = None) -> Path:
        """导出完整数据包（zip）。dest 缺省放系统临时目录，由调用方负责清理。"""
        import tempfile

        dest = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="pai_export_")) / f"personal_ai_data_{_now_stamp()}.zip"
        self._write_zip(dest)
        log.info("数据已导出：%s", dest)
        return dest

    # ---------- 备份 ----------
    def create_backup(self, prefix: str = "backup") -> str:
        """在 数据目录/backups/ 下创建一张备份，返回文件名。"""
        name = f"{prefix}_{_now_stamp()}.zip"
        target = self.backups_dir / name
        # 同一秒连拍两张时加序号，绝不覆盖已有备份
        i = 1
        while target.exists():
            name = f"{prefix}_{_now_stamp()}_{i}.zip"
            target = self.backups_dir / name
            i += 1
        self._write_zip(target)
        log.info("已创建备份：%s", name)
        return name

    def list_backups(self) -> list[dict]:
        if not self.backups_dir.exists():
            return []
        items = []
        for p in sorted(self.backups_dir.glob("*.zip"), reverse=True):
            try:
                st = p.stat()
            except OSError:
                continue
            items.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
            })
        return items

    # ---------- 恢复 / 导入 ----------
    def _safe_backup_name(self, name: str) -> Path:
        """备份名只允许纯文件名，且必须真实存在（防路径穿越）。"""
        if not name or Path(name).name != name:
            raise DataError(f"非法备份名：{name}")
        path = self.backups_dir / name
        if not path.exists():
            raise DataError(f"备份不存在：{name}")
        return path

    def _extract_validated(self, zip_path: Path, into: Path) -> None:
        try:
            zf_ctx = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise DataError(f"这不是有效的 zip 数据包：{exc}") from exc
        with zf_ctx as zf:
            names = zf.namelist()
            if _DB_NAME not in names:
                raise DataError("这不是 Personal AI 数据包（缺少 personal_ai.db）")
            into.mkdir(parents=True, exist_ok=True)
            for info in zf.infolist():
                target = (into / info.filename).resolve()
                if target != into.resolve() and into.resolve() not in target.parents:
                    raise DataError("数据包包含非法路径，已拒绝")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        if not _is_sqlite(into / _DB_NAME):
            raise DataError("数据包里的数据库损坏（不是合法 SQLite）")

    def restore(self, source: Path | None = None, *, from_backup: str | None = None) -> dict:
        """用 source zip 恢复数据（或 from_backup 指定本地备份名）。
        恢复前先给当前数据拍安全网备份。

        顺序：校验 → 安全网备份 → 换 DB（连 WAL）→ 换缩略图 → 换模型设置。
        """
        if from_backup is not None:
            source = self._safe_backup_name(from_backup)
        if source is None:
            raise DataError("未指定数据包或备份名")
        if not source.exists():
            raise DataError(f"数据包不存在：{source}")

        import tempfile

        tmp_root = Path(tempfile.mkdtemp(prefix="pai_restore_", dir=self.data_dir if self.data_dir.exists() else None))
        try:
            extracted = tmp_root / "pkg"
            self._extract_validated(source, extracted)

            safety = None
            if self.db_path.exists():
                try:
                    safety = self.create_backup(prefix="auto_safety")
                except Exception as exc:  # 安全网拍不出来就不许恢复
                    raise DataError(f"无法创建恢复前的安全备份，已中止：{exc}") from exc

            # 1) 换 DB（WAL 模式的 -wal/-shm 必须一起清掉，否则旧日志会污染新库）
            new_db = extracted / _DB_NAME
            for suffix in ("", "-wal", "-shm"):
                old = Path(str(self.db_path) + suffix)
                if old.exists():
                    old.unlink()
            _replace_with_retry(new_db, self.db_path)

            # 2) 换缩略图
            new_thumbs = extracted / _THUMBS_NAME
            if self.thumbs_dir.exists():
                shutil.rmtree(self.thumbs_dir, ignore_errors=True)
            if new_thumbs.exists():
                shutil.copytree(new_thumbs, self.thumbs_dir)
            else:
                self.thumbs_dir.mkdir(parents=True, exist_ok=True)

            # 3) 换模型设置（包里有才换；没有就保留当前）
            new_settings = extracted / _SETTINGS_NAME
            settings_restored = False
            if new_settings.exists():
                _replace_with_retry(new_settings, self.data_dir / _SETTINGS_NAME)
                settings_restored = True

            result = {"ok": True, "safety_backup": safety, "settings_restored": settings_restored,
                      "counts": self._counts()}
            log.info("数据已恢复（安全网：%s）", safety)
            return result
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)


def _replace_with_retry(src: Path, dest: Path, attempts: int = 5) -> None:
    """Windows 下偶有句柄占用，换文件失败就退避重试。"""
    import os

    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(src, dest)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.2 * (i + 1))
    raise DataError(f"替换文件失败：{dest}（{last}）")


class tempfile_in:
    """在指定目录下开临时子目录，退出时自动清理。"""

    def __init__(self, base: Path):
        self.base = base
        self.path: Path | None = None

    def __enter__(self) -> Path:
        import tempfile

        self.base.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix=".tmp_", dir=self.base))
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path:
            shutil.rmtree(self.path, ignore_errors=True)
