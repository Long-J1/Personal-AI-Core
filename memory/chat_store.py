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
  iso TEXT NOT NULL,
  suppressed INTEGER NOT NULL DEFAULT 0   -- V0.5.3：1=别记轮，进 prompt 时隐藏
);
CREATE INDEX IF NOT EXISTS idx_chat_conv ON chat_messages(conv_id, epoch);
"""

# 进 prompt 时隐藏轮的占位符（V0.5.3：不断连贯——模型知道这里有一轮但看不见，
# 而不是对话断了；UI 回看不受影响）。
SUPPRESSED_PLACEHOLDER = (
    "[本轮用户要求不记录，内容已隐藏，不可引用、不可复述]"
)


def _now() -> datetime:
    return datetime.now().astimezone()


class ChatStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            # V0.5.3 migration：老库补 suppressed 列（默认 0 = 老行为全可见）
            cols = {r[1] for r in con.execute(
                "PRAGMA table_info(chat_messages)").fetchall()}
            with con:
                if "suppressed" not in cols:
                    con.execute("ALTER TABLE chat_messages ADD COLUMN suppressed"
                                " INTEGER NOT NULL DEFAULT 0")
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

    def last_activity_epoch(self) -> float | None:
        """最近一次活动的时间戳（会话表里 updated_epoch 最大值）。

        V0.5.9 idle worker 用它判断"用户停止操作了没有"。
        表里已有这个字段，所以只是多一个读方法，不改结构。
        """
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT updated_epoch FROM conversations"
                " ORDER BY updated_epoch DESC LIMIT 1"
            ).fetchone()
        if not row or row["updated_epoch"] is None:
            return None
        return float(row["updated_epoch"])

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
    def append(self, conv_id: str, role: str, content: str,
               suppressed: bool = False) -> None:
        """追加一条消息。suppressed=True 标别记轮（V0.5.3）。

        老调用零改动（默认 False）。UI 回看不受影响，只有进 prompt 过滤。
        """
        if not content:
            return
        now = _now()
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT INTO chat_messages (id, conv_id, role, content, epoch,"
                    " iso, suppressed) VALUES (?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, conv_id, role, content,
                     now.timestamp(), now.isoformat(), 1 if suppressed else 0),
                )
                con.execute(
                    "UPDATE conversations SET updated_epoch=? WHERE id=?",
                    (now.timestamp(), conv_id),
                )

    def history(
        self, conv_id: str, limit: int = 50, order: str = "asc"
    ) -> list[dict]:
        """返回消息列表；order='asc' 按时间正序，'desc' 取最近 N 条再翻回正序
        （供提示词使用：最近的、但按时间先后排列）。
        rowid 作平局裁决——Windows 时钟粒度下同一毫秒插入也必须稳定排序。"""
        with closing(self._connect()) as con:
            if order == "desc":
                rows = con.execute(
                    "SELECT role, content, iso FROM chat_messages WHERE conv_id=? "
                    "ORDER BY epoch DESC, rowid DESC LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = con.execute(
                    "SELECT role, content, iso FROM chat_messages WHERE conv_id=? "
                    "ORDER BY epoch ASC, rowid ASC LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def history_for_prompt(
        self, conv_id: str, limit: int = 50, order: str = "asc"
    ) -> list[dict]:
        """进 prompt 的历史：suppressed 轮替换为占位符（V0.5.3）。

        与 history() 的区别只在这一处——UI 回看调 history()（原文可见），
        prompt 组装调本方法（隐藏轮不可引用、不可复述）。
        返回项多一个 `suppressed` 键（0/1），调用方可审计。
        """
        with closing(self._connect()) as con:
            cols = {r[1] for r in con.execute(
                "PRAGMA table_info(chat_messages)").fetchall()}
            sup_sel = ("suppressed" if "suppressed" in cols else "0 AS suppressed")
            if order == "desc":
                rows = con.execute(
                    "SELECT role, content, iso, " + sup_sel +
                    " FROM chat_messages WHERE conv_id=? "
                    "ORDER BY epoch DESC, rowid DESC LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = con.execute(
                    "SELECT role, content, iso, " + sup_sel +
                    " FROM chat_messages WHERE conv_id=? "
                    "ORDER BY epoch ASC, rowid ASC LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("suppressed"):
                d["content"] = SUPPRESSED_PLACEHOLDER
            out.append(d)
        return out

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
