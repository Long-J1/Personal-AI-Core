"""SQLite 记忆库：事件/事实的写入、时间/关键词检索、删除、导出。

设计原则（任务书 §7）：
- 记忆可查看、删除、导出、暂停 → 对应 list / delete / export / meta 隐私开关；
- 检索接口保持稳定（时间范围 + 关键词），未来可内部替换成向量检索，调用方无感。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .event import Event

log = logging.getLogger("memory.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  created_at_iso TEXT NOT NULL,
  created_at_epoch REAL NOT NULL,
  source TEXT,
  scene TEXT,
  activity TEXT,
  objects_json TEXT NOT NULL DEFAULT '[]',
  description TEXT NOT NULL DEFAULT '',
  importance REAL NOT NULL DEFAULT 0.5,
  facts_json TEXT NOT NULL DEFAULT '[]',
  raw_understanding TEXT,
  schema_version INTEGER NOT NULL DEFAULT 1,
  image_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_epoch ON events(created_at_epoch);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'fact',
  created_at_iso TEXT NOT NULL,
  created_at_epoch REAL NOT NULL,
  source_event_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_content ON facts(content);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_EVENT_COLUMNS = (
    "id, created_at_iso, created_at_epoch, source, scene, activity, "
    "objects_json, description, importance, facts_json, raw_understanding, "
    "schema_version, image_ref"
)


def _like_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


class MemoryStore:
    """线程安全的单文件 SQLite 记忆库。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            con.commit()
        log.info("记忆库就绪：%s", self.db_path)

    # ---------- 连接 ----------
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    # ---------- 事件写入 ----------
    def add_event(self, event: Event) -> None:
        """写入事件，并顺手把它带的长期事实并入 facts 表（去重）。"""
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    f"INSERT OR REPLACE INTO events ({_EVENT_COLUMNS}) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.id,
                        event.created_at.isoformat(),
                        event.created_at.timestamp(),
                        event.source,
                        event.scene,
                        event.activity,
                        json.dumps(event.objects, ensure_ascii=False),
                        event.description,
                        float(event.importance),
                        json.dumps(event.facts, ensure_ascii=False),
                        event.raw_understanding,
                        event.schema_version,
                        event.image_ref,
                    ),
                )
                for fact in event.facts:
                    content = fact.strip()
                    if not content:
                        continue
                    con.execute(
                        "INSERT OR IGNORE INTO facts "
                        "(id, content, kind, created_at_iso, created_at_epoch, source_event_id) "
                        "VALUES (?, ?, 'fact', ?, ?, ?)",
                        (
                            f"{event.id}_{abs(hash(content)) & 0xFFFFFFFF:08x}",
                            content,
                            event.created_at.isoformat(),
                            event.created_at.timestamp(),
                            event.id,
                        ),
                    )
        log.info(
            "记忆写入：%s [%s] %s", event.id[:8], event.time_label, event.description[:60]
        )

    # ---------- 查询 ----------
    def _rows_to_events(self, rows: Iterable[sqlite3.Row]) -> list[Event]:
        events: list[Event] = []
        for row in rows:
            try:
                events.append(
                    Event(
                        id=row["id"],
                        created_at=datetime.fromisoformat(row["created_at_iso"]),
                        source=row["source"] or "unknown",
                        scene=row["scene"] or "",
                        activity=row["activity"] or "",
                        objects=json.loads(row["objects_json"] or "[]"),
                        description=row["description"] or "",
                        importance=row["importance"],
                        facts=json.loads(row["facts_json"] or "[]"),
                        raw_understanding=row["raw_understanding"],
                        schema_version=row["schema_version"],
                        image_ref=row["image_ref"],
                    )
                )
            except Exception as exc:  # 单条损坏不能拖垮整个查询
                log.warning("事件行解析失败 id=%s：%s", row["id"], exc)
        return events

    def get_event(self, event_id: str) -> Event | None:
        with closing(self._connect()) as con:
            row = con.execute(
                f"SELECT {_EVENT_COLUMNS} FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._rows_to_events([row])[0] if row else None

    def list_events(
        self,
        since_epoch: float | None = None,
        until_epoch: float | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "desc",
    ) -> list[Event]:
        where, params = self._time_conditions(since_epoch, until_epoch)
        order_sql = "DESC" if order != "asc" else "ASC"
        sql = (
            f"SELECT {_EVENT_COLUMNS} FROM events {where} "
            f"ORDER BY created_at_epoch {order_sql} LIMIT ? OFFSET ?"
        )
        with closing(self._connect()) as con:
            rows = con.execute(sql, (*params, limit, offset)).fetchall()
        return self._rows_to_events(rows)

    def search_events(
        self,
        keywords: list[str] | None = None,
        since_epoch: float | None = None,
        until_epoch: float | None = None,
        limit: int = 20,
        mode: str = "and",
    ) -> list[Event]:
        """时间范围 + 关键词检索。

        mode="and"：所有关键词都要命中（精确）；
        mode="or"：命中任一即可（宽松兜底）。
        """
        where, params = self._time_conditions(since_epoch, until_epoch)
        kws = [k.strip() for k in (keywords or []) if k and k.strip()]
        if kws:
            conditions = []
            for kw in kws:
                esc = _like_escape(kw)
                conditions.append(
                    "(description LIKE ? ESCAPE '\\' OR scene LIKE ? ESCAPE '\\' "
                    "OR activity LIKE ? ESCAPE '\\' OR objects_json LIKE ? ESCAPE '\\' "
                    "OR facts_json LIKE ? ESCAPE '\\')"
                )
                params.extend([f"%{esc}%"] * 5)
            joiner = " AND " if mode == "and" else " OR "
            where += (" AND " if where else "WHERE ") + "(" + joiner.join(conditions) + ")"

        sql = (
            f"SELECT {_EVENT_COLUMNS} FROM events {where} "
            "ORDER BY created_at_epoch DESC LIMIT ?"
        )
        with closing(self._connect()) as con:
            rows = con.execute(sql, (*params, limit)).fetchall()
        events = self._rows_to_events(rows)
        log.debug(
            "检索：%s mode=%s → %d 条", kws, mode, len(events)
        )
        return events

    @staticmethod
    def _time_conditions(
        since_epoch: float | None, until_epoch: float | None
    ) -> tuple[str, list[float]]:
        where, params = "", []
        if since_epoch is not None:
            where += "WHERE created_at_epoch >= ?"
            params.append(since_epoch)
        if until_epoch is not None:
            where += (" AND " if where else "WHERE ") + "created_at_epoch <= ?"
            params.append(until_epoch)
        return where, params

    def count_events(self) -> int:
        with closing(self._connect()) as con:
            return int(con.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    # ---------- 事实（长期记忆雏形）----------
    def list_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT id, content, kind, created_at_iso, source_event_id FROM facts "
                "ORDER BY created_at_epoch DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_facts(self, keywords: list[str], limit: int = 20) -> list[dict[str, Any]]:
        kws = [k for k in keywords if k.strip()]
        if not kws:
            return self.list_facts(limit)
        conds, params = [], []
        for kw in kws:
            conds.append("content LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(kw)}%")
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT id, content, kind, created_at_iso, source_event_id FROM facts "
                f"WHERE {' AND '.join(conds)} ORDER BY created_at_epoch DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 删除（用户拥有自己的 AI）----------
    def delete_event(self, event_id: str) -> bool:
        """删除单条事件及其派生事实、缩略图记录。"""
        with self._lock, closing(self._connect()) as con:
            with con:
                cur = con.execute("DELETE FROM events WHERE id = ?", (event_id,))
                con.execute("DELETE FROM facts WHERE source_event_id = ?", (event_id,))
        deleted = cur.rowcount > 0
        if deleted:
            log.info("记忆删除：%s", event_id[:8])
        return deleted

    def delete_all_events(self) -> int:
        """清空全部记忆（事件 + 事实），保留隐私开关等元信息。"""
        with self._lock, closing(self._connect()) as con:
            with con:
                cur = con.execute("DELETE FROM events")
                con.execute("DELETE FROM facts")
        log.info("全部记忆已清空（删除 %d 条）", cur.rowcount)
        return cur.rowcount

    # ---------- 导出 ----------
    def export_all(self) -> dict[str, Any]:
        with closing(self._connect()) as con:
            event_rows = con.execute(
                f"SELECT {_EVENT_COLUMNS} FROM events ORDER BY created_at_epoch"
            ).fetchall()
            fact_rows = con.execute(
                "SELECT id, content, kind, created_at_iso, source_event_id FROM facts "
                "ORDER BY created_at_epoch"
            ).fetchall()
            meta_rows = con.execute("SELECT key, value FROM meta").fetchall()
        return {
            "exported_at": datetime.now().astimezone().isoformat(),
            "schema_version": 1,
            "counts": {"events": len(event_rows), "facts": len(fact_rows)},
            "meta": {r["key"]: r["value"] for r in meta_rows},
            "events": [dict(r) for r in event_rows],
            "facts": [dict(r) for r in fact_rows],
        }

    # ---------- 隐私开关 / 元信息 ----------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    @property
    def paused(self) -> bool:
        return self.get_meta("privacy_paused", "0") == "1"

    @paused.setter
    def paused(self, value: bool) -> None:
        self.set_meta("privacy_paused", "1" if value else "0")
        log.info("隐私状态：%s", "已暂停记录" if value else "恢复记录")
