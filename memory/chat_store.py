"""对话历史存储：会话与消息（V0.2 对话通道的载体）。

对话本身也是一种感知——但消息只有在未暂停时才持久化（暂停 = 不沉淀）。
"""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

import sqlite3

log = logging.getLogger("memory.chat")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  created_iso TEXT NOT NULL,
  updated_epoch REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id TEXT PRIMARY KEY,
  conv_id TEXT NOT NULL,
  role TEXT NOT NULL,             -- user | assistant
  content TEXT NOT NULL,
  epoch REAL NOT NULL,
  iso TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_conv ON chat_messages(conv_id, epoch);
"""


def _now() -> datetime:
    return datetime.now().astimezone()


class ChatStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # ---------- 会话 ----------
    def create_session(self) -> str:
        sid = uuid.uuid4().hex
        now = _now()
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT INTO conversations (id, created_iso, updated_epoch) "
                    "VALUES (?,?,?)",
                    (sid, now.isoformat(), now.timestamp()),
                )
        log.debug("新建会话 %s", sid[:8])
        return sid

    def latest_session(self) -> str | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT id FROM conversations ORDER BY updated_epoch DESC LIMIT 1"
            ).fetchone()
        return row["id"] if row else None

    def get_or_create_session(self, session_id: str | None = None) -> str:
        if session_id:
            with closing(self._connect()) as con:
                row = con.execute(
                    "SELECT id FROM conversations WHERE id=?", (session_id,)
                ).fetchone()
            if row:
                return session_id
        return self.latest_session() or self.create_session()

    def session_exists(self, session_id: str) -> bool:
        with closing(self._connect()) as con:
            return bool(
                con.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (session_id,)
                ).fetchone()
            )

    # ---------- 消息 ----------
    def append(self, conv_id: str, role: str, content: str) -> None:
        if not content:
            return
        now = _now()
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT INTO chat_messages (id, conv_id, role, content, epoch, iso) "
                    "VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, conv_id, role, content,
                     now.timestamp(), now.isoformat()),
                )
                con.execute(
                    "UPDATE conversations SET updated_epoch=? WHERE id=?",
                    (now.timestamp(), conv_id),
                )

    def history(
        self, conv_id: str, limit: int = 50, order: str = "asc"
    ) -> list[dict]:
        """返回消息列表；order='asc' 按时间正序，'desc' 倒序（取最近 N 条）。"""
        sql_order = "DESC" if order == "desc" else "ASC"
        with closing(self._connect()) as con:
            if order == "desc":
                rows = con.execute(
                    "SELECT role, content, iso FROM chat_messages WHERE conv_id=? "
                    f"ORDER BY epoch {sql_order} LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = con.execute(
                    "SELECT role, content, iso FROM chat_messages WHERE conv_id=? "
                    f"ORDER BY epoch {sql_order} LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def count_messages(self, conv_id: str | None = None) -> int:
        with closing(self._connect()) as con:
            if conv_id:
                return int(con.execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE conv_id=?",
                    (conv_id,),
                ).fetchone()[0])
            return int(
                con.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
            )

    def count_sessions(self) -> int:
        with closing(self._connect()) as con:
            return int(
                con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            )
