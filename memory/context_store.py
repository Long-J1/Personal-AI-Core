"""Personal Context 存储：AI 对这个人的"当前理解"（V0.2 A 组一等对象）。

设计原则（见 docs/PRODUCT_TRUTH.md §6 §7、docs/decisions/D007）：
- 条目是**活的**：可新增、可纠正（原内容进变更日志）、可删除（立即失效）、可恢复；
- 每条带来源与时间，支持查看"它对你的理解"和变更历史；
- "相处方式（interaction）"类条目会被派生为对话中的行为指令——
  **Context 通过改变后续判断来起作用，而不是静态摆设**。

表与 events/facts 同库共存（data/personal_ai.db）。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import sqlite3

log = logging.getLogger("memory.context")

KINDS = ("profile", "fact", "dynamic", "interaction")
KIND_LABELS = {
    "profile": "画像",
    "fact": "事实",
    "dynamic": "动态",
    "interaction": "相处方式",
}
SOURCES = ("chat", "observe", "correction", "manual", "system")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_entries (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at_iso TEXT NOT NULL,
  created_at_epoch REAL NOT NULL,
  updated_at_iso TEXT NOT NULL,
  updated_at_epoch REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  used_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_context_active ON context_entries(active, updated_at_epoch);

CREATE TABLE IF NOT EXISTS context_log (
  id TEXT PRIMARY KEY,
  epoch REAL NOT NULL,
  iso TEXT NOT NULL,
  action TEXT NOT NULL,          -- create | correct | manual_edit | delete | restore
  entry_id TEXT NOT NULL,
  kind TEXT,
  content_before TEXT,
  content_after TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_context_log_epoch ON context_log(epoch);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now().astimezone()


class ContextEntry:
    """轻量条目（不用 pydantic，保持存储层零额外依赖）。"""

    __slots__ = (
        "id", "kind", "content", "source", "created_at", "updated_at",
        "active", "used_count",
    )

    def __init__(
        self,
        id: str,
        kind: str,
        content: str,
        source: str,
        created_at: datetime,
        updated_at: datetime,
        active: bool = True,
        used_count: int = 0,
    ):
        self.id = id
        self.kind = kind
        self.content = content
        self.source = source
        self.created_at = created_at
        self.updated_at = updated_at
        self.active = active
        self.used_count = used_count

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "kind_label": KIND_LABELS.get(self.kind, self.kind),
            "content": self.content,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "active": self.active,
            "used_count": self.used_count,
            "date": self.created_at.strftime("%Y-%m-%d"),
            "time": self.created_at.strftime("%H:%M"),
        }


class ContextStore:
    """线程安全；纠正/删除都写变更日志（历史可查）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            con.commit()
        log.info("Personal Context 就绪：%s", self.db_path)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # ---------- 写入 ----------
    def add_entry(
        self,
        kind: str,
        content: str,
        source: str = "chat",
        *,
        detail: dict | None = None,
        max_chars: int = 200,
    ) -> tuple[ContextEntry | None, str | None]:
        """新增一条理解。返回 (entry, None) 或 (None, 跳过原因)。"""
        if kind not in KINDS:
            return None, f"未知类型 {kind}"
        content = (content or "").strip()[:max_chars]
        if not content:
            return None, "内容为空"

        now = _now()
        entry = ContextEntry(
            id=uuid.uuid4().hex, kind=kind, content=content, source=source,
            created_at=now, updated_at=now,
        )
        with self._lock, closing(self._connect()) as con:
            # 去重：同内容的活跃条目已存在则跳过
            dup = con.execute(
                "SELECT id FROM context_entries WHERE active=1 AND content = ?",
                (content,),
            ).fetchone()
            if dup:
                log.debug("理解去重跳过：%s", content[:40])
                return None, "已存在相同理解"
            with con:
                con.execute(
                    "INSERT INTO context_entries "
                    "(id, kind, content, source, created_at_iso, created_at_epoch, "
                    " updated_at_iso, updated_at_epoch, active, used_count) "
                    "VALUES (?,?,?,?,?,?,?,?,1,0)",
                    (entry.id, kind, content, source, now.isoformat(),
                     now.timestamp(), now.isoformat(), now.timestamp()),
                )
                self._log(
                    con, "create", entry.id, kind,
                    content_before=None, content_after=content, detail=detail,
                )
        log.info("理解新增 [%s] %s", KIND_LABELS.get(kind, kind), content[:60])
        return entry, None

    def correct_entry(
        self,
        entry_id: str,
        new_content: str,
        *,
        source: str = "correction",
        reason: str = "",
        detail: dict | None = None,
        max_chars: int = 200,
    ) -> bool:
        """纠正/编辑一条理解（原内容进日志，可追溯）。"""
        new_content = (new_content or "").strip()[:max_chars]
        if not new_content:
            return False
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT kind, content, active FROM context_entries WHERE id=?",
                (entry_id,),
            ).fetchone()
            if not row or not row["active"]:
                log.warning("纠正失败，条目不存在或已删除：%s", entry_id[:8])
                return False
            with con:
                con.execute(
                    "UPDATE context_entries SET content=?, source=?, updated_at_iso=?, "
                    "updated_at_epoch=? WHERE id=?",
                    (new_content, source, now.isoformat(), now.timestamp(), entry_id),
                )
                self._log(
                    con, "correct" if source == "correction" else "manual_edit",
                    entry_id, row["kind"],
                    content_before=row["content"], content_after=new_content,
                    detail={"reason": reason, **(detail or {})},
                )
        log.info("理解纠正 %s：%s → %s", entry_id[:8],
                 (row["content"] if row else "?")[:40], new_content[:40])
        return True

    def delete_entry(self, entry_id: str) -> bool:
        """删除（逻辑删）：立即失效，后续组装不再读取。"""
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT kind, content, active FROM context_entries WHERE id=?",
                (entry_id,),
            ).fetchone()
            if not row or not row["active"]:
                return False
            with con:
                con.execute(
                    "UPDATE context_entries SET active=0, updated_at_iso=?, "
                    "updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), entry_id),
                )
                self._log(con, "delete", entry_id, row["kind"],
                          content_before=row["content"], content_after=None)
        log.info("理解删除 %s：%s", entry_id[:8], row["content"][:50])
        return True

    def restore_entry(self, entry_id: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT kind, content, active FROM context_entries WHERE id=?",
                (entry_id,),
            ).fetchone()
            if not row or row["active"]:
                return False
            with con:
                con.execute(
                    "UPDATE context_entries SET active=1, updated_at_iso=?, "
                    "updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), entry_id),
                )
                self._log(con, "restore", entry_id, row["kind"],
                          content_before=None, content_after=row["content"])
        log.info("理解恢复 %s", entry_id[:8])
        return True

    @staticmethod
    def _log(
        con: sqlite3.Connection,
        action: str,
        entry_id: str,
        kind: str,
        *,
        content_before: str | None,
        content_after: str | None,
        detail: dict | None = None,
    ) -> None:
        now = _now()
        con.execute(
            "INSERT INTO context_log (id, epoch, iso, action, entry_id, kind, "
            "content_before, content_after, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, now.timestamp(), now.isoformat(), action,
             entry_id, kind, content_before, content_after,
             json.dumps(detail or {}, ensure_ascii=False)),
        )

    # ---------- 读取 ----------
    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> ContextEntry:
        return ContextEntry(
            id=row["id"], kind=row["kind"], content=row["content"],
            source=row["source"],
            created_at=datetime.fromisoformat(row["created_at_iso"]),
            updated_at=datetime.fromisoformat(row["updated_at_iso"]),
            active=bool(row["active"]), used_count=row["used_count"],
        )

    def list_entries(
        self, kind: str | None = None, active_only: bool = True, limit: int = 200
    ) -> list[ContextEntry]:
        where, params = [], []
        if active_only:
            where.append("active=1")
        if kind:
            where.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM context_entries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # 画像/相处方式优先，其余按更新时间
        sql += (" ORDER BY CASE kind WHEN 'interaction' THEN 0 WHEN 'profile' THEN 1 "
                "WHEN 'dynamic' THEN 2 ELSE 3 END, updated_at_epoch DESC LIMIT ?")
        with closing(self._connect()) as con:
            rows = con.execute(sql, (*params, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_entry(self, entry_id: str) -> ContextEntry | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM context_entries WHERE id=?", (entry_id,)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def mark_used(self, entry_ids: Iterable[str]) -> None:
        """记录条目被上下文使用过（证明 Context 真的参与了后续判断）。"""
        ids = list(entry_ids)
        if not ids:
            return
        with self._lock, closing(self._connect()) as con:
            with con:
                con.executemany(
                    "UPDATE context_entries SET used_count = used_count + 1 WHERE id=?",
                    [(i,) for i in ids],
                )

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM context_log ORDER BY epoch DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def count_active(self) -> int:
        with closing(self._connect()) as con:
            return int(
                con.execute(
                    "SELECT COUNT(*) FROM context_entries WHERE active=1"
                ).fetchone()[0]
            )

    def export_all(self) -> dict[str, Any]:
        with closing(self._connect()) as con:
            entries = con.execute(
                "SELECT * FROM context_entries ORDER BY created_at_epoch"
            ).fetchall()
            logs = con.execute(
                "SELECT * FROM context_log ORDER BY epoch"
            ).fetchall()
        return {
            "exported_at": _now().isoformat(),
            "schema_version": 1,
            "counts": {
                "entries": len(entries),
                "active": sum(1 for e in entries if e["active"]),
                "changes": len(logs),
            },
            "entries": [self._row_to_entry(e).to_public_dict() for e in entries],
            "history": [dict(l) for l in logs],
        }

    # ---------- 隐私（与事件记忆共用同一个开关）----------
    @property
    def paused(self) -> bool:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT value FROM meta WHERE key='privacy_paused'"
            ).fetchone()
        return bool(row and row["value"] == "1")
