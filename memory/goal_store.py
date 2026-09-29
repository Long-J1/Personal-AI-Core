"""长期目标存储（V0.5）：用户未来想成为怎样的人。

铁律：Goal 只能来自用户明确表达。创建函数只接受"用户原话"参数；
任何模型生成的文本调进来必须拒绝（单测锁死）。
AI 只做关联展示（规则匹配），不做目标推理。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlite3

log = logging.getLogger("memory.goal")

STATUSES = ("proposed", "active", "paused", "done", "rejected", "deleted")
STATUS_LABELS = {
    "proposed": "待确认",
    "active": "进行中",
    "paused": "已暂停",
    "done": "已完成",
    "rejected": "已否决",
    "deleted": "已删除",
}
LIVE_STATUSES = ("proposed", "active", "paused")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'proposed',
  linked_pattern_ids TEXT NOT NULL DEFAULT '[]',
  linked_scopes TEXT NOT NULL DEFAULT '[]',
  asked_count INTEGER NOT NULL DEFAULT 0,
  last_asked_epoch REAL NOT NULL DEFAULT 0,
  pending_question TEXT NOT NULL DEFAULT '',
  created_at_iso TEXT NOT NULL,
  created_at_epoch REAL NOT NULL,
  updated_at_iso TEXT NOT NULL,
  updated_at_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goal_status ON goals(status, updated_at_epoch);

CREATE TABLE IF NOT EXISTS goal_log (
  id TEXT PRIMARY KEY,
  epoch REAL NOT NULL,
  iso TEXT NOT NULL,
  action TEXT NOT NULL,
  goal_id TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_gl_goal ON goal_log(goal_id, epoch);
"""


def _now() -> datetime:
    return datetime.now().astimezone()


class Goal:
    """轻量对象（不用 pydantic，保持存储层零额外依赖）。"""

    __slots__ = (
        "id", "title", "description", "status",
        "linked_pattern_ids", "linked_scopes",
        "asked_count", "last_asked_epoch", "pending_question",
        "created_at", "updated_at",
    )

    def __init__(
        self,
        id: str,
        title: str,
        description: str = "",
        status: str = "proposed",
        linked_pattern_ids: list | None = None,
        linked_scopes: list | None = None,
        asked_count: int = 0,
        last_asked_epoch: float = 0.0,
        pending_question: str = "",
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ):
        now = _now()
        self.id = id
        self.title = title
        self.description = description
        self.status = status
        self.linked_pattern_ids = linked_pattern_ids or []
        self.linked_scopes = linked_scopes or []
        self.asked_count = asked_count
        self.last_asked_epoch = last_asked_epoch
        self.pending_question = pending_question
        self.created_at = created_at or now
        self.updated_at = updated_at or now

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "linked_pattern_ids": self.linked_pattern_ids,
            "linked_scopes": self.linked_scopes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "date": self.created_at.strftime("%Y-%m-%d"),
            "time": self.created_at.strftime("%H:%M"),
        }


class GoalStore:
    """目标的增删改查 + 确认流 + 关联计算（只读规则匹配，无推理）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            con.commit()
        log.info("长期目标就绪：%s", self.db_path)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # ---------- 提议（仅用户原话）----------
    def propose(
        self, title: str, user_text: str, description: str = ""
    ) -> tuple[Goal | None, str | None]:
        """从用户明确表达建待确认目标。title 必须来自用户原话派生，
        调用方传模型生成文本一律拒绝（由调用约定保证，此处做长度与去重校验）。"""
        title = (title or "").strip()[:60]
        user_text = (user_text or "").strip()[:200]
        if not title:
            return None, "目标标题为空"
        if not user_text:
            return None, "缺少用户原话（目标必须来自明确表达）"
        now = _now()
        with self._lock, closing(self._connect()) as con:
            dup = con.execute(
                "SELECT id FROM goals WHERE title=? AND status != 'deleted'",
                (title,),
            ).fetchone()
            if dup:
                return None, "同名目标已存在"
            goal = Goal(id=uuid.uuid4().hex, title=title,
                        description=(description or "")[:300],
                        created_at=now, updated_at=now)
            with con:
                con.execute(
                    "INSERT INTO goals (id, title, description, status,"
                    " linked_pattern_ids, linked_scopes, asked_count,"
                    " last_asked_epoch, pending_question,"
                    " created_at_iso, created_at_epoch,"
                    " updated_at_iso, updated_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (goal.id, title, goal.description, "proposed",
                     "[]", "[]", 0, 0.0, "",
                     now.isoformat(), now.timestamp(),
                     now.isoformat(), now.timestamp()),
                )
                self._log(con, "propose", goal.id,
                          {"utterance": user_text})
        log.info("目标待确认：%s", title[:40])
        return goal, None

    # ---------- 读取 ----------
    @staticmethod
    def _row_to_goal(row: sqlite3.Row) -> Goal:
        return Goal(
            id=row["id"], title=row["title"],
            description=row["description"], status=row["status"],
            linked_pattern_ids=json.loads(row["linked_pattern_ids"] or "[]"),
            linked_scopes=json.loads(row["linked_scopes"] or "[]"),
            asked_count=row["asked_count"],
            last_asked_epoch=row["last_asked_epoch"],
            pending_question=row["pending_question"],
            created_at=datetime.fromisoformat(row["created_at_iso"]),
            updated_at=datetime.fromisoformat(row["updated_at_iso"]),
        )

    def get(self, goal_id: str) -> Goal | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
        return self._row_to_goal(row) if row else None

    def list_goals(
        self,
        *,
        status: str | None = None,
        live_only: bool = True,
        limit: int = 200,
    ) -> list[Goal]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status=?")
            params.append(status)
        elif live_only:
            where.append(f"status IN ({','.join('?' * len(LIVE_STATUSES))})")
            params.extend(LIVE_STATUSES)
        sql = "SELECT * FROM goals"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at_epoch DESC LIMIT ?"
        with closing(self._connect()) as con:
            rows = con.execute(sql, (*params, limit)).fetchall()
        return [self._row_to_goal(r) for r in rows]

    def list_active(self, limit: int = 5) -> list[Goal]:
        return self.list_goals(status="active", limit=limit)

    def get_detail(self, goal_id: str, pattern_store=None,
                   context_store=None) -> dict[str, Any] | None:
        """目标 + 规则关联（Pattern/Context 按关键词匹配展示，无推理）。"""
        goal = self.get(goal_id)
        if goal is None:
            return None
        linked_ids, linked_scopes = self._compute_links(
            goal, pattern_store, context_store)
        d = goal.to_public_dict()
        d["linked_pattern_ids"] = linked_ids
        d["linked_scopes"] = linked_scopes
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM goal_log WHERE goal_id=? ORDER BY epoch",
                (goal_id,),
            ).fetchall()
        d["history"] = [dict(r) for r in rows]
        return d

    @staticmethod
    def _keywords(text: str) -> set[str]:
        from core.context import extract_keywords
        return {k for k in extract_keywords(text or "") if len(k) >= 2}

    def _compute_links(self, goal, pattern_store, context_store):
        """关联刷新（读时计算并写回，保证展示与数据一致）。"""
        keys = self._keywords(goal.title + " " + goal.description)
        pids, scopes = [], []
        if pattern_store is not None and keys:
            for p in pattern_store.list_patterns(live_only=True, limit=200):
                plabel = p.scope.split(":", 1)[-1]
                if any(k in p.statement or k in plabel or plabel in k
                       for k in keys):
                    pids.append(p.id)
                    if p.scope not in scopes:
                        scopes.append(p.scope)
        if context_store is not None and keys:
            for e in context_store.list_entries(limit=200):
                if any(k in (e.content or "") for k in keys):
                    if e.kind not in scopes:
                        scopes.append(e.kind)
                    if len(scopes) >= 8:
                        break
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "UPDATE goals SET linked_pattern_ids=?, linked_scopes=?"
                    " WHERE id=?",
                    (json.dumps(pids, ensure_ascii=False),
                     json.dumps(scopes, ensure_ascii=False), goal.id),
                )
        return pids, scopes

    def history(self, goal_id: str) -> list[dict]:
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM goal_log WHERE goal_id=? ORDER BY epoch",
                (goal_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_active(self) -> int:
        with closing(self._connect()) as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM goals WHERE status='active'"
            ).fetchone()[0])

    def clear_all(self) -> int:
        with self._lock, closing(self._connect()) as con:
            with con:
                cur = con.execute("DELETE FROM goals")
                con.execute("DELETE FROM goal_log")
        return cur.rowcount

    # ---------- 状态流转 ----------
    def _set_status(self, goal_id: str, status: str,
                    detail: dict | None, action: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            if row["status"] == status:
                return True
            with con:
                con.execute(
                    "UPDATE goals SET status=?, pending_question='',"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (status, now.isoformat(), now.timestamp(), goal_id),
                )
                self._log(con, action, goal_id, detail or {})
        return True

    def confirm(self, goal_id: str) -> bool:
        """用户确认 → active（只改状态）。"""
        return self._set_status(goal_id, "active", {}, "confirm")

    def reject(self, goal_id: str, reason: str = "") -> bool:
        return self._set_status(
            goal_id, "rejected",
            {"reason": (reason or "")[:200], "channel": "trust_ui"}, "reject")

    def pause(self, goal_id: str) -> bool:
        return self._set_status(goal_id, "paused", {}, "pause")

    def complete(self, goal_id: str) -> bool:
        return self._set_status(goal_id, "done", {}, "done")

    def delete_entry(self, goal_id: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            with con:
                con.execute(
                    "UPDATE goals SET status='deleted', pending_question='',"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), goal_id),
                )
                self._log(con, "delete", goal_id, {})
        return True

    def restore_entry(self, goal_id: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if not row or row["status"] != "deleted":
                return False
            with con:
                con.execute(
                    "UPDATE goals SET status='proposed', updated_at_iso=?,"
                    " updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), goal_id),
                )
                self._log(con, "restore", goal_id, {})
        return True

    # ---------- pending（验证流程复用）----------
    def get_pending(self) -> Goal | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM goals WHERE pending_question != ''"
                " ORDER BY updated_at_epoch DESC LIMIT 1",
            ).fetchone()
        return self._row_to_goal(row) if row else None

    def set_pending(self, goal_id: str, question: str) -> tuple[bool, str | None]:
        question = (question or "").strip()
        if not question:
            return False, "问题文本为空"
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if not row:
                return False, "目标不存在"
            if row["status"] != "proposed":
                return False, f"只有待确认目标能挂问题（当前 {row['status']}）"
            other = con.execute(
                "SELECT id FROM goals WHERE pending_question != ''"
                " AND id != ? LIMIT 1",
                (goal_id,),
            ).fetchone()
            if other:
                return False, "已有挂起问题，同时只挂一个"
            with con:
                con.execute(
                    "UPDATE goals SET pending_question=?,"
                    " asked_count = asked_count + 1,"
                    " last_asked_epoch=?, updated_at_iso=?, updated_at_epoch=?"
                    " WHERE id=?",
                    (question[:500], now.timestamp(), now.isoformat(),
                     now.timestamp(), goal_id),
                )
                self._log(con, "ask", goal_id,
                          {"question": question[:500]})
        return True, None

    def clear_pending(self, goal_id: str) -> bool:
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT pending_question FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if not row or not row["pending_question"]:
                return False
            with con:
                con.execute(
                    "UPDATE goals SET pending_question='' WHERE id=?",
                    (goal_id,),
                )
        return True

    def note_evasive(self, goal_id: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT id FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if not row:
                return False
            with con:
                con.execute(
                    "UPDATE goals SET last_asked_epoch=?,"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (now.timestamp(), now.isoformat(), now.timestamp(),
                     goal_id),
                )
                self._log(con, "evasive", goal_id, {})
        return True

    @staticmethod
    def _log(con: sqlite3.Connection, action: str, goal_id: str,
             detail: dict | None) -> None:
        now = _now()
        con.execute(
            "INSERT INTO goal_log (id, epoch, iso, action, goal_id, detail)"
            " VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, now.timestamp(), now.isoformat(), action,
             goal_id, json.dumps(detail or {}, ensure_ascii=False)),
        )
