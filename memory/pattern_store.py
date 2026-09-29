"""Personal Patterns 存储：验证过的稳定行为规律（V0.5 第一阶段）。

与 Hypothesis 的本质区别：Pattern 的证据是"被确认过的假设"或"用户原话+引用行"，
永远不是单轮聊天的直接产物。晋升铁律：禁止一次聊天直接生成长期人格。

置信度是另一组显式数学（常量写死）；"用户确认只改状态不动数值"沿用 V0.3。
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

log = logging.getLogger("memory.pattern")

# ---------- 置信度数学（显式常量，单测锁定）----------
PATTERN_INIT = 0.60
PATTERN_SUPPORT = 0.06
PATTERN_CONTRADICT = -0.20
PATTERN_CORRECTION_SET = 0.25  # 用户否决置位（与假设一致）
PATTERN_MIN = 0.0
PATTERN_MAX = 1.0
PATTERN_MIN_INDEP = 2
PATTERN_HIGH_CAP = 0.75

# ---------- 状态机 ----------
STATUSES = ("tentative", "confirmed", "rejected", "deleted")
STATUS_LABELS = {
    "tentative": "待验证",
    "confirmed": "用户已确认",
    "rejected": "已否决",
    "deleted": "已删除",
}
LIVE_STATUSES = ("tentative", "confirmed")

# 来源：晋升（主要）/ 用户表达（同样先 tentative，不过验证不开 confirmed）
SOURCES = ("promotion", "user_declared")

# 证据白名单（铁律：pattern 自身永不在其中；单轮聊天原文也不直接进，
# 用户原话以 confirmation 日志行的方式追溯；correction 沿用 V0.3 词汇，
# 指"被纠正的那条 Context"）
SOURCE_TYPES = ("hypothesis", "context", "event", "correction",
                "confirmation")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
  id TEXT PRIMARY KEY,
  statement TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'tentative',
  scope TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'promotion',
  support_count INTEGER NOT NULL DEFAULT 0,
  contradict_count INTEGER NOT NULL DEFAULT 0,
  used_count INTEGER NOT NULL DEFAULT 0,
  asked_count INTEGER NOT NULL DEFAULT 0,
  last_asked_epoch REAL NOT NULL DEFAULT 0,
  pending_question TEXT NOT NULL DEFAULT '',
  created_at_iso TEXT NOT NULL,
  created_at_epoch REAL NOT NULL,
  updated_at_iso TEXT NOT NULL,
  updated_at_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pat_status ON patterns(status, updated_at_epoch);

CREATE TABLE IF NOT EXISTS pattern_evidence (
  id TEXT PRIMARY KEY,
  pattern_id TEXT NOT NULL,
  polarity TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  origin TEXT NOT NULL,
  excerpt TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at_iso TEXT NOT NULL,
  created_at_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pe_pat ON pattern_evidence(pattern_id, polarity);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pe_dedup
  ON pattern_evidence(pattern_id, polarity, source_type, source_id);

CREATE TABLE IF NOT EXISTS pattern_log (
  id TEXT PRIMARY KEY,
  epoch REAL NOT NULL,
  iso TEXT NOT NULL,
  action TEXT NOT NULL,
  pattern_id TEXT NOT NULL,
  conf_before REAL,
  conf_after REAL,
  detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_pl_pat ON pattern_log(pattern_id, epoch);
"""


def _now() -> datetime:
    return datetime.now().astimezone()


def _clamp(v: float) -> float:
    return max(PATTERN_MIN, min(PATTERN_MAX, v))


class Pattern:
    """轻量对象（不用 pydantic，保持存储层零额外依赖）。"""

    __slots__ = (
        "id", "statement", "confidence", "status", "scope", "source",
        "support_count", "contradict_count", "used_count",
        "asked_count", "last_asked_epoch", "pending_question",
        "created_at", "updated_at",
    )

    def __init__(
        self,
        id: str,
        statement: str,
        confidence: float,
        status: str,
        scope: str,
        source: str,
        support_count: int,
        contradict_count: int,
        used_count: int,
        created_at: datetime,
        updated_at: datetime,
        asked_count: int = 0,
        last_asked_epoch: float = 0.0,
        pending_question: str = "",
    ):
        self.id = id
        self.statement = statement
        self.confidence = confidence
        self.status = status
        self.scope = scope
        self.source = source
        self.support_count = support_count
        self.contradict_count = contradict_count
        self.used_count = used_count
        self.asked_count = asked_count
        self.last_asked_epoch = last_asked_epoch
        self.pending_question = pending_question
        self.created_at = created_at
        self.updated_at = updated_at

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "confidence": round(self.confidence, 2),
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "scope": self.scope,
            "source": self.source,
            "support_count": self.support_count,
            "contradict_count": self.contradict_count,
            "used_count": self.used_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "date": self.created_at.strftime("%Y-%m-%d"),
            "time": self.created_at.strftime("%H:%M"),
        }


class PatternStore:
    """规律的增删改查 + 证据绑定 + 置信度更新（同一事务，不漂移）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            con.commit()
        log.info("Personal Patterns 就绪：%s", self.db_path)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # ---------- 创建 ----------
    def create(
        self,
        statement: str,
        scope: str,
        *,
        source: str = "promotion",
        detail: dict | None = None,
        max_chars: int = 300,
    ) -> tuple[Pattern | None, str | None]:
        statement = (statement or "").strip()[:max_chars]
        scope = (scope or "").strip()[:120]
        if not statement:
            return None, "规律内容为空"
        if not scope:
            return None, "适用范围为空"
        if source not in SOURCES:
            return None, f"未知来源 {source}"
        now = _now()
        pat = Pattern(
            id=uuid.uuid4().hex, statement=statement,
            confidence=PATTERN_INIT, status="tentative", scope=scope,
            source=source, support_count=0, contradict_count=0, used_count=0,
            created_at=now, updated_at=now,
        )
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT INTO patterns (id, statement, confidence, status,"
                    " scope, source, support_count, contradict_count,"
                    " used_count, asked_count, last_asked_epoch,"
                    " pending_question, created_at_iso, created_at_epoch,"
                    " updated_at_iso, updated_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pat.id, statement, PATTERN_INIT, "tentative", scope,
                     source, 0, 0, 0, 0, 0.0, "",
                     now.isoformat(), now.timestamp(),
                     now.isoformat(), now.timestamp()),
                )
                self._log(con, "create", pat.id, None, PATTERN_INIT,
                          detail or {"scope": scope, "source": source})
        log.info("规律新增 [tentative·0.60] %s", statement[:60])
        return pat, None

    # ---------- 读取 ----------
    @staticmethod
    def _row_to_pattern(row: sqlite3.Row) -> Pattern:
        return Pattern(
            id=row["id"], statement=row["statement"],
            confidence=row["confidence"], status=row["status"],
            scope=row["scope"], source=row["source"],
            support_count=row["support_count"],
            contradict_count=row["contradict_count"],
            used_count=row["used_count"],
            asked_count=row["asked_count"],
            last_asked_epoch=row["last_asked_epoch"],
            pending_question=row["pending_question"],
            created_at=datetime.fromisoformat(row["created_at_iso"]),
            updated_at=datetime.fromisoformat(row["updated_at_iso"]),
        )

    def get(self, pattern_id: str) -> Pattern | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM patterns WHERE id=?", (pattern_id,)
            ).fetchone()
        return self._row_to_pattern(row) if row else None

    def list_patterns(
        self,
        *,
        status: str | None = None,
        live_only: bool = True,
        limit: int = 200,
    ) -> list[Pattern]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status=?")
            params.append(status)
        elif live_only:
            where.append(f"status IN ({','.join('?' * len(LIVE_STATUSES))})")
            params.extend(LIVE_STATUSES)
        sql = "SELECT * FROM patterns"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY confidence DESC, updated_at_epoch DESC LIMIT ?"
        with closing(self._connect()) as con:
            rows = con.execute(sql, (*params, limit)).fetchall()
        return [self._row_to_pattern(r) for r in rows]

    def list_confirmed(self, limit: int = 5) -> list[Pattern]:
        """进 Prompt 的规律：只有 confirmed（门槛高于猜测）。"""
        return self.list_patterns(status="confirmed", limit=limit)

    def get_detail(self, pattern_id: str) -> dict[str, Any] | None:
        hypo = self.get(pattern_id)
        if hypo is None:
            return None
        with closing(self._connect()) as con:
            ev_rows = con.execute(
                "SELECT * FROM pattern_evidence WHERE pattern_id=?"
                " ORDER BY created_at_epoch",
                (pattern_id,),
            ).fetchall()
            log_rows = con.execute(
                "SELECT * FROM pattern_log WHERE pattern_id=?"
                " ORDER BY epoch",
                (pattern_id,),
            ).fetchall()
        d = hypo.to_public_dict()
        d["independent_sources"] = self._independent_origins(pattern_id)
        d["evidence"] = [dict(r) for r in ev_rows]
        d["history"] = [dict(r) for r in log_rows]
        return d

    def find_by_scope(self, scope: str) -> list[Pattern]:
        scope = (scope or "").strip()[:120]
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM patterns WHERE scope=? AND status != 'deleted'"
                " ORDER BY updated_at_epoch DESC",
                (scope,),
            ).fetchall()
        return [self._row_to_pattern(r) for r in rows]

    def count_live(self) -> int:
        with closing(self._connect()) as con:
            return int(con.execute(
                f"SELECT COUNT(*) FROM patterns WHERE status IN"
                f" ({','.join('?' * len(LIVE_STATUSES))})",
                LIVE_STATUSES,
            ).fetchone()[0])

    def mark_used(self, pattern_ids: list[str]) -> None:
        if not pattern_ids:
            return
        with self._lock, closing(self._connect()) as con:
            with con:
                con.executemany(
                    "UPDATE patterns SET used_count = used_count + 1 WHERE id=?",
                    [(i,) for i in pattern_ids],
                )

    def clear_all(self) -> int:
        """清空全部规律及证据（"清空全部"语义连带）。"""
        with self._lock, closing(self._connect()) as con:
            with con:
                cur = con.execute("DELETE FROM patterns")
                con.execute("DELETE FROM pattern_evidence")
                con.execute("DELETE FROM pattern_log")
        log.info("全部规律已清空（删除 %d 条）", cur.rowcount)
        return cur.rowcount

    # ---------- 证据 ----------
    def _source_exists(
        self, con: sqlite3.Connection, source_type: str, source_id: str
    ) -> bool:
        if source_type == "confirmation":
            hit = con.execute(
                "SELECT id FROM pattern_log WHERE id=? AND action IN"
                " ('declared', 'verify_confirm', 'reject')",
                (source_id,),
            ).fetchone()
            return bool(hit)
        if source_type == "correction":
            # 沿用 V0.3：纠正追溯到"被纠正的那条 Context"
            hit = con.execute(
                "SELECT id FROM context_entries WHERE id=?", (source_id,)
            ).fetchone()
            return bool(hit)
        table = {"hypothesis": "hypotheses", "context": "context_entries",
                 "event": "events"}.get(source_type)
        if not table:
            return False
        try:
            hit = con.execute(
                f"SELECT id FROM {table} WHERE id=?", (source_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return bool(hit)

    def _independent_origins(self, pattern_id: str) -> int:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT COUNT(DISTINCT origin) FROM pattern_evidence"
                " WHERE pattern_id=? AND polarity='support'",
                (pattern_id,),
            ).fetchone()
        return int(row[0])

    def independent_support_count(self, pattern_id: str) -> int:
        return self._independent_origins(pattern_id)

    def add_evidence(
        self,
        pattern_id: str,
        polarity: str,
        source_type: str,
        source_id: str,
        *,
        origin: str | None = None,
        excerpt: str = "",
        detail: dict | None = None,
    ) -> tuple[bool, str | None]:
        """绑定一条证据并更新置信度。失败返回 (False, 原因)。"""
        if polarity not in ("support", "contradict"):
            return False, f"未知证据极性 {polarity}"
        if source_type not in SOURCE_TYPES:
            # 铁律：pattern 自身、单轮聊天原文，永远不能成为证据
            return False, f"不允许的证据来源 {source_type}"
        if not source_id:
            return False, "证据来源 id 为空"
        with self._lock, closing(self._connect()) as con:
            pat_row = con.execute(
                "SELECT confidence, status FROM patterns WHERE id=?",
                (pattern_id,),
            ).fetchone()
            if pat_row is None:
                return False, "规律不存在"
            if pat_row["status"] == "deleted":
                return False, "规律已删除，先恢复再绑定证据"
            if not self._source_exists(con, source_type, source_id):
                return False, f"证据来源不存在：{source_type}:{source_id[:8]}"
            day = _now().strftime("%Y-%m-%d")
            if origin is None:
                if source_type == "hypothesis":
                    origin = f"hypo:{source_id}"
                elif source_type == "confirmation":
                    origin = f"answer:{source_id}"
                else:
                    origin = f"ctxday:{day}"
            now = _now()
            dup = con.execute(
                "SELECT id FROM pattern_evidence WHERE pattern_id=?"
                " AND polarity=? AND source_type=? AND source_id=?",
                (pattern_id, polarity, source_type, source_id),
            ).fetchone()
            if dup:
                return False, "重复证据"
            conf_before = float(pat_row["confidence"])
            if polarity == "support":
                conf = _clamp(conf_before + PATTERN_SUPPORT)
                existing = {
                    r[0] for r in con.execute(
                        "SELECT DISTINCT origin FROM pattern_evidence"
                        " WHERE pattern_id=? AND polarity='support'",
                        (pattern_id,),
                    ).fetchall()
                }
                indep = len(existing) + (0 if origin in existing else 1)
                if indep < PATTERN_MIN_INDEP:
                    conf = min(conf, PATTERN_HIGH_CAP)
            else:
                conf = _clamp(conf_before + PATTERN_CONTRADICT)
            with con:
                con.execute(
                    "INSERT INTO pattern_evidence (id, pattern_id, polarity,"
                    " source_type, source_id, origin, excerpt, detail,"
                    " created_at_iso, created_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, pattern_id, polarity, source_type,
                     source_id, origin, (excerpt or "")[:200],
                     json.dumps(detail or {}, ensure_ascii=False),
                     now.isoformat(), now.timestamp()),
                )
                if polarity == "support":
                    con.execute(
                        "UPDATE patterns SET confidence=?, support_count ="
                        " support_count + 1, updated_at_iso=?,"
                        " updated_at_epoch=? WHERE id=?",
                        (conf, now.isoformat(), now.timestamp(), pattern_id),
                    )
                else:
                    con.execute(
                        "UPDATE patterns SET confidence=?, contradict_count ="
                        " contradict_count + 1, updated_at_iso=?,"
                        " updated_at_epoch=? WHERE id=?",
                        (conf, now.isoformat(), now.timestamp(), pattern_id),
                    )
                self._log(con, "support" if polarity == "support" else "contradict",
                          pattern_id, conf_before, conf,
                          {"source_type": source_type,
                           "source_id": source_id, "origin": origin,
                           **(detail or {})})
        log.info("规律 %s %s证据 %s:%s → %.2f",
                 pattern_id[:8], "支持" if polarity == "support" else "反证",
                 source_type, source_id[:8], conf)
        return True, None

    # ---------- 用户行为：确认 / 否决 ----------
    def confirm(self, pattern_id: str) -> bool:
        """用户确认：只改状态，不动 confidence（确认 ≠ 高置信度）。"""
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM patterns WHERE id=?",
                (pattern_id,),
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            if row["status"] == "confirmed":
                return True
            with con:
                con.execute(
                    "UPDATE patterns SET status='confirmed',"
                    " pending_question='', updated_at_iso=?,"
                    " updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), pattern_id),
                )
                self._log(con, "confirm", pattern_id, row["confidence"],
                          row["confidence"], {})
        log.info("规律 %s 被用户确认", pattern_id[:8])
        return True

    def reject(
        self, pattern_id: str, reason: str = "", channel: str = "trust_ui"
    ) -> bool:
        """用户否决：建反证 + 置 0.25 + 状态 rejected（幂等）。"""
        reason = (reason or "").strip()[:200]
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM patterns WHERE id=?",
                (pattern_id,),
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            if row["status"] == "rejected":
                return True
            conf_before = float(row["confidence"])
            with con:
                log_id = uuid.uuid4().hex
                con.execute(
                    "INSERT INTO pattern_log (id, epoch, iso, action,"
                    " pattern_id, conf_before, conf_after, detail)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (log_id, now.timestamp(), now.isoformat(), "reject",
                     pattern_id, conf_before, PATTERN_CORRECTION_SET,
                     json.dumps({"reason": reason, "channel": channel},
                                ensure_ascii=False)),
                )
                con.execute(
                    "INSERT INTO pattern_evidence (id, pattern_id, polarity,"
                    " source_type, source_id, origin, excerpt, detail,"
                    " created_at_iso, created_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, pattern_id, "contradict",
                     "confirmation", log_id, f"denial:{log_id}", reason,
                     json.dumps({"channel": channel}, ensure_ascii=False),
                     now.isoformat(), now.timestamp()),
                )
                con.execute(
                    "UPDATE patterns SET confidence=?, status='rejected',"
                    " contradict_count = contradict_count + 1,"
                    " pending_question='',"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (PATTERN_CORRECTION_SET, now.isoformat(), now.timestamp(),
                     pattern_id),
                )
        log.info("规律 %s 被用户否决 → 0.25", pattern_id[:8])
        return True

    def record_verification_confirm(
        self, pattern_id: str, user_text: str
    ) -> bool:
        """验证问题亲口确认：confirm（置信度不动）+ 回答入 support 链，清 pending。"""
        user_text = (user_text or "").strip()[:200]
        if not user_text:
            return False
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM patterns WHERE id=?",
                (pattern_id,),
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            conf = float(row["confidence"])
            with con:
                log_id = uuid.uuid4().hex
                con.execute(
                    "INSERT INTO pattern_log (id, epoch, iso, action,"
                    " pattern_id, conf_before, conf_after, detail)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (log_id, now.timestamp(), now.isoformat(),
                     "verify_confirm", pattern_id, conf, conf,
                     json.dumps({"answer": user_text,
                                 "channel": "verification"},
                                ensure_ascii=False)),
                )
                con.execute(
                    "INSERT INTO pattern_evidence (id, pattern_id,"
                    " polarity, source_type, source_id, origin, excerpt, detail,"
                    " created_at_iso, created_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, pattern_id, "support", "confirmation",
                     log_id, f"answer:{log_id}", user_text,
                     json.dumps({"channel": "verification"},
                                ensure_ascii=False),
                     now.isoformat(), now.timestamp()),
                )
                con.execute(
                    "UPDATE patterns SET status='confirmed',"
                    " support_count = support_count + 1, pending_question='',"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), pattern_id),
                )
        log.info("规律 %s 被验证确认（置信度保持 %.2f）",
                 pattern_id[:8], conf)
        return True

    def declare_from_user(
        self, statement: str, scope: str, user_text: str
    ) -> tuple[Pattern | None, str | None]:
        """B 路径入口：用户明确表达规律 → 建 tentative（不算结论）。

        用户原话以 confirmation 日志行追溯（铁律：单轮原文不直接进证据）。
        同 scope 非删除已存在 → 不重复建（返回 exists）。
        """
        user_text = (user_text or "").strip()[:200]
        if not user_text:
            return None, "用户原话为空"
        scope = (scope or "").strip()[:120]
        if not scope:
            return None, "适用范围为空"
        if self.find_by_scope(scope):
            return None, "同主题规律已存在"
        pat, skip = self.create(statement, scope, source="user_declared",
                                detail={"channel": "user_expression"})
        if pat is None:
            return None, skip
        now = _now()
        with self._lock, closing(self._connect()) as con:
            with con:
                log_id = uuid.uuid4().hex
                con.execute(
                    "INSERT INTO pattern_log (id, epoch, iso, action,"
                    " pattern_id, conf_before, conf_after, detail)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (log_id, now.timestamp(), now.isoformat(), "declared",
                     pat.id, PATTERN_INIT, PATTERN_INIT,
                     json.dumps({"utterance": user_text}, ensure_ascii=False)),
                )
                con.execute(
                    "INSERT INTO pattern_evidence (id, pattern_id,"
                    " polarity, source_type, source_id, origin, excerpt, detail,"
                    " created_at_iso, created_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, pat.id, "support", "confirmation",
                     log_id, f"declare:{log_id}", user_text,
                     json.dumps({"channel": "user_expression"},
                                ensure_ascii=False),
                     now.isoformat(), now.timestamp()),
                )
                con.execute(
                    "UPDATE patterns SET support_count = support_count + 1,"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), pat.id),
                )
        log.info("规律待验证 [user_declared] %s", statement[:60])
        return self.get(pat.id), None

    # ---------- pending（V0.4 验证流程复用）----------
    def get_pending(self) -> Pattern | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM patterns WHERE pending_question != ''"
                " ORDER BY updated_at_epoch DESC LIMIT 1",
            ).fetchone()
        return self._row_to_pattern(row) if row else None

    def set_pending(
        self, pattern_id: str, question: str
    ) -> tuple[bool, str | None]:
        question = (question or "").strip()
        if not question:
            return False, "问题文本为空"
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status FROM patterns WHERE id=?", (pattern_id,)
            ).fetchone()
            if not row:
                return False, "规律不存在"
            if row["status"] != "tentative":
                return False, f"只有待验证规律能挂问题（当前 {row['status']}）"
            other = con.execute(
                "SELECT id FROM patterns WHERE pending_question != ''"
                " AND id != ? LIMIT 1",
                (pattern_id,),
            ).fetchone()
            if other:
                return False, "已有挂起问题，同时只挂一个"
            with con:
                con.execute(
                    "UPDATE patterns SET pending_question=?,"
                    " asked_count = asked_count + 1,"
                    " last_asked_epoch=?, updated_at_iso=?, updated_at_epoch=?"
                    " WHERE id=?",
                    (question[:500], now.timestamp(), now.isoformat(),
                     now.timestamp(), pattern_id),
                )
                self._log(con, "ask", pattern_id, None, None,
                          {"question": question[:500]})
        return True, None

    def clear_pending(self, pattern_id: str) -> bool:
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT pending_question FROM patterns WHERE id=?",
                (pattern_id,),
            ).fetchone()
            if not row or not row["pending_question"]:
                return False
            with con:
                con.execute(
                    "UPDATE patterns SET pending_question='' WHERE id=?",
                    (pattern_id,),
                )
        return True

    def note_evasive(self, pattern_id: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT confidence FROM patterns WHERE id=?", (pattern_id,)
            ).fetchone()
            if not row:
                return False
            with con:
                con.execute(
                    "UPDATE patterns SET last_asked_epoch=?,"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (now.timestamp(), now.isoformat(), now.timestamp(),
                     pattern_id),
                )
                self._log(con, "evasive", pattern_id, row["confidence"],
                          row["confidence"], {})
        return True

    # ---------- 删除 / 恢复 ----------
    def delete_entry(self, pattern_id: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM patterns WHERE id=?",
                (pattern_id,),
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            with con:
                con.execute(
                    "UPDATE patterns SET status='deleted', pending_question='',"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), pattern_id),
                )
                self._log(con, "delete", pattern_id, row["confidence"],
                          row["confidence"], {})
        log.info("规律删除 %s", pattern_id[:8])
        return True

    def restore_entry(self, pattern_id: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM patterns WHERE id=?",
                (pattern_id,),
            ).fetchone()
            if not row or row["status"] != "deleted":
                return False
            with con:
                con.execute(
                    "UPDATE patterns SET status='tentative', updated_at_iso=?,"
                    " updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), pattern_id),
                )
                self._log(con, "restore", pattern_id, row["confidence"],
                          row["confidence"], {})
        log.info("规律恢复 %s（回 tentative）", pattern_id[:8])
        return True

    @staticmethod
    def _log(
        con: sqlite3.Connection,
        action: str,
        pattern_id: str,
        conf_before: float | None,
        conf_after: float | None,
        detail: dict | None,
    ) -> None:
        now = _now()
        con.execute(
            "INSERT INTO pattern_log (id, epoch, iso, action, pattern_id,"
            " conf_before, conf_after, detail) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, now.timestamp(), now.isoformat(), action,
             pattern_id, conf_before, conf_after,
             json.dumps(detail or {}, ensure_ascii=False)),
        )
