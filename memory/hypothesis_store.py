"""Personal Hypotheses 存储：关于用户的、可证伪的假设（V0.3）。

设计原则（见 docs/PLAN_v0.3.md、成长模型 §15）：
- 与 `context_entries` 物理隔离：假设永远不是普通 Context，另起三张表；
- 每条假设绑定可追溯的证据（具体到行），铁律：假设永远不能成为证据；
- 置信度是显式数学（常量写死），不是模型说了算；
- 用户确认 ≠ 高置信度：confirm 只改状态，不动 confidence。

与 context_store.py 同风格：线程安全、WAL、变更审计日志。
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

log = logging.getLogger("memory.hypothesis")

# ---------- 置信度数学（显式常量，单测锁定）----------
INIT_CONFIDENCE = 0.50
SUPPORT_DELTA = 0.08
CONTRADICT_DELTA = -0.25
CORRECTION_SET = 0.25          # 用户亲口纠正/否决：直接置位（固定值）
CONF_MIN = 0.0
CONF_MAX = 1.0
MIN_INDEPENDENT_FOR_HIGH = 2   # 独立支持源 < 2 → 不许超过 HIGH_CONF_CAP
HIGH_CONF_CAP = 0.70
SCOPE_MAX = 120                # scope 写入与查找统一截断（防超长 activity 导致去重 miss）

# ---------- 状态机 ----------
STATUSES = ("tentative", "confirmed", "rejected", "deleted")
STATUS_LABELS = {
    "tentative": "待验证",
    "confirmed": "用户已确认",
    "rejected": "已否决",
    "deleted": "已删除",
}
LIVE_STATUSES = ("tentative", "confirmed")   # 进入 Prompt / UI 可见

# ---------- 证据 ----------
POLARITIES = ("support", "contradict")
# 允许的证据来源（白名单即铁律：hypothesis 不在其中，永远不能成为证据）
SOURCE_TYPES = ("context", "event", "correction")
SOURCE_TABLE = {
    "context": "context_entries",
    "event": "events",
    "correction": "context_entries",  # 纠正追溯到"被纠正的那条 Context"
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
  id TEXT PRIMARY KEY,
  statement TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'tentative',
  scope TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_hypo_status ON hypotheses(status, updated_at_epoch);

CREATE TABLE IF NOT EXISTS hypothesis_evidence (
  id TEXT PRIMARY KEY,
  hypothesis_id TEXT NOT NULL,
  polarity TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  origin TEXT NOT NULL,
  excerpt TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at_iso TEXT NOT NULL,
  created_at_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_he_hypo ON hypothesis_evidence(hypothesis_id, polarity);
CREATE UNIQUE INDEX IF NOT EXISTS idx_he_dedup
  ON hypothesis_evidence(hypothesis_id, polarity, source_type, source_id);

CREATE TABLE IF NOT EXISTS hypothesis_log (
  id TEXT PRIMARY KEY,
  epoch REAL NOT NULL,
  iso TEXT NOT NULL,
  action TEXT NOT NULL,
  hypothesis_id TEXT NOT NULL,
  conf_before REAL,
  conf_after REAL,
  detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_hl_hypo ON hypothesis_log(hypothesis_id, epoch);
"""


def _now() -> datetime:
    return datetime.now().astimezone()


def _clamp(v: float) -> float:
    return max(CONF_MIN, min(CONF_MAX, v))


# 显式否决词（用户亲口否定假设时触发置位 + rejected，确定性规则，可测）
DENY_KEYWORDS = ("不对", "不是", "没有", "别", "不要", "错了", "纠正", "胡说")
# 否决词扫描前的剥离项：疑问句式（是不是/有没有）与自述笔误（记错）不算否决，
# 否则"记错了""是不是这样"会被误判为显式否决；
# "别"是单字高危：先剥离常见非否决词（特别/别人/…），剩下的"别…"才算否决
_DENY_STRIP = ("是不是", "有没有", "记错",
               "特别", "别人", "告别", "别扭", "别致", "别处", "别名")


def _is_explicit_denial(text: str) -> bool:
    t = text or ""
    for pat in _DENY_STRIP:
        t = t.replace(pat, "")
    return any(k in t for k in DENY_KEYWORDS)


class Hypothesis:
    """轻量对象（不用 pydantic，保持存储层零额外依赖）。"""

    __slots__ = (
        "id", "statement", "confidence", "status", "scope",
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
            "support_count": self.support_count,
            "contradict_count": self.contradict_count,
            "used_count": self.used_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "date": self.created_at.strftime("%Y-%m-%d"),
            "time": self.created_at.strftime("%H:%M"),
        }


class HypothesisStore:
    """假设的增删改查 + 证据绑定 + 置信度更新（同一事务，不漂移）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as con:
            con.executescript(_SCHEMA)
            # V0.4 migration：老库的 hypotheses 表没有提问列，原地补上
            cols = {r[1] for r in con.execute(
                "PRAGMA table_info(hypotheses)").fetchall()}
            with con:
                if "asked_count" not in cols:
                    con.execute("ALTER TABLE hypotheses ADD COLUMN"
                                " asked_count INTEGER NOT NULL DEFAULT 0")
                if "last_asked_epoch" not in cols:
                    con.execute("ALTER TABLE hypotheses ADD COLUMN"
                                " last_asked_epoch REAL NOT NULL DEFAULT 0")
                if "pending_question" not in cols:
                    con.execute("ALTER TABLE hypotheses ADD COLUMN"
                                " pending_question TEXT NOT NULL DEFAULT ''")
            con.commit()
        log.info("Personal Hypotheses 就绪：%s", self.db_path)

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
        detail: dict | None = None,
        max_chars: int = 300,
    ) -> tuple[Hypothesis | None, str | None]:
        statement = (statement or "").strip()[:max_chars]
        scope = (scope or "").strip()[:SCOPE_MAX]
        if not statement:
            return None, "假设内容为空"
        if not scope:
            return None, "适用范围为空"
        now = _now()
        hypo = Hypothesis(
            id=uuid.uuid4().hex, statement=statement,
            confidence=INIT_CONFIDENCE, status="tentative", scope=scope,
            support_count=0, contradict_count=0, used_count=0,
            created_at=now, updated_at=now,
        )
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT INTO hypotheses (id, statement, confidence, status, scope,"
                    " support_count, contradict_count, used_count,"
                    " created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (hypo.id, statement, INIT_CONFIDENCE, "tentative", scope,
                     0, 0, 0, now.isoformat(), now.timestamp(),
                     now.isoformat(), now.timestamp()),
                )
                self._log(con, "create", hypo.id, None, INIT_CONFIDENCE,
                          detail or {"scope": scope})
        log.info("假设新增 [tentative·0.50] %s", statement[:60])
        return hypo, None

    # ---------- 读取 ----------
    @staticmethod
    def _row_to_hypo(row: sqlite3.Row) -> Hypothesis:
        keys = set(row.keys())
        return Hypothesis(
            id=row["id"], statement=row["statement"],
            confidence=row["confidence"], status=row["status"],
            scope=row["scope"], support_count=row["support_count"],
            contradict_count=row["contradict_count"],
            used_count=row["used_count"],
            asked_count=row["asked_count"] if "asked_count" in keys else 0,
            last_asked_epoch=(row["last_asked_epoch"]
                              if "last_asked_epoch" in keys else 0.0),
            pending_question=(row["pending_question"]
                              if "pending_question" in keys else ""),
            created_at=datetime.fromisoformat(row["created_at_iso"]),
            updated_at=datetime.fromisoformat(row["updated_at_iso"]),
        )

    def get(self, hypo_id: str) -> Hypothesis | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM hypotheses WHERE id=?", (hypo_id,)
            ).fetchone()
        return self._row_to_hypo(row) if row else None

    def list_hypotheses(
        self,
        *,
        status: str | None = None,
        live_only: bool = True,
        limit: int = 200,
    ) -> list[Hypothesis]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status=?")
            params.append(status)
        elif live_only:
            where.append(f"status IN ({','.join('?' * len(LIVE_STATUSES))})")
            params.extend(LIVE_STATUSES)
        sql = "SELECT * FROM hypotheses"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # V0.5.3：溢出假设垫底（quota_overflow 低优先级，不主动引用影响回答；
        # list_for_prompt 经此函数，自动生效）
        sql += (" ORDER BY CASE WHEN scope LIKE 'quota_overflow:%' THEN 1"
                " ELSE 0 END, confidence DESC, updated_at_epoch DESC LIMIT ?")
        with closing(self._connect()) as con:
            rows = con.execute(sql, (*params, limit)).fetchall()
        return [self._row_to_hypo(r) for r in rows]

    def list_for_prompt(self, limit: int = 3) -> list[Hypothesis]:
        """进入 Prompt 的假设：tentative + confirmed，按置信度倒序。"""
        return self.list_hypotheses(live_only=True, limit=limit)

    def get_detail(self, hypo_id: str) -> dict[str, Any] | None:
        """假设 + 全部证据 + 变更日志（UI"为什么这么认为"的依据）。"""
        hypo = self.get(hypo_id)
        if hypo is None:
            return None
        with closing(self._connect()) as con:
            ev_rows = con.execute(
                "SELECT * FROM hypothesis_evidence WHERE hypothesis_id=?"
                " ORDER BY created_at_epoch",
                (hypo_id,),
            ).fetchall()
            log_rows = con.execute(
                "SELECT * FROM hypothesis_log WHERE hypothesis_id=?"
                " ORDER BY epoch",
                (hypo_id,),
            ).fetchall()
        d = hypo.to_public_dict()
        d["independent_sources"] = self._independent_origins(hypo_id)
        d["evidence"] = [dict(r) for r in ev_rows]
        d["history"] = [dict(r) for r in log_rows]
        return d

    def find_by_scope(self, scope: str) -> list[Hypothesis]:
        """同 scope 的非删除假设（归纳器去重用）。

        含 rejected：调用方决定怎么处理（见 Inducer：rejected 只跳过，
        不喂新证据——用户否决过的主题不再打扰）。
        入参截断与写入侧一致（超长 activity 不 miss）。
        """
        scope = (scope or "").strip()[:SCOPE_MAX]
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM hypotheses WHERE scope=? AND status != 'deleted'"
                " ORDER BY updated_at_epoch DESC",
                (scope,),
            ).fetchall()
        return [self._row_to_hypo(r) for r in rows]


    def count_log_since(self, actions: tuple, since_epoch: float) -> int:
        """指定时刻之后某类变更次数（周报统计用）。"""
        marks = ','.join('?' * len(actions))
        with closing(self._connect()) as con:
            return int(con.execute(
                f"SELECT COUNT(*) FROM hypothesis_log WHERE action IN"
                f" ({marks}) AND epoch >= ?",
                (*actions, since_epoch),
            ).fetchone()[0])

    def support_coverage(self) -> dict:
        """所有非删除假设的支持证据覆盖：{hypo_id: (status, {(type, id)})}。

        归纳器用它防"改述复活"：同一批素材（即使被改写换了标签）
        不许建第二个假设。单查询，归纳每轮只调一次。"""
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT h.id, h.status, e.source_type, e.source_id"
                " FROM hypotheses h JOIN hypothesis_evidence e"
                " ON e.hypothesis_id = h.id"
                " WHERE h.status != 'deleted' AND e.polarity='support'",
            ).fetchall()
        out: dict = {}
        for r in rows:
            st, s = out.setdefault(r[0], (r[1], set()))
            s.add((r[2], r[3]))
        return out
    def count_live(self) -> int:
        with closing(self._connect()) as con:
            return int(con.execute(
                f"SELECT COUNT(*) FROM hypotheses WHERE status IN"
                f" ({','.join('?' * len(LIVE_STATUSES))})",
                LIVE_STATUSES,
            ).fetchone()[0])

    def mark_used(self, hypo_ids: list[str]) -> None:
        if not hypo_ids:
            return
        with self._lock, closing(self._connect()) as con:
            with con:
                con.executemany(
                    "UPDATE hypotheses SET used_count = used_count + 1 WHERE id=?",
                    [(i,) for i in hypo_ids],
                )

    def export_all(self) -> dict[str, Any]:
        """导出全部假设 + 证据 + 日志（信任导出/排错用，结构对标 context）。"""
        with closing(self._connect()) as con:
            hypos = con.execute(
                "SELECT * FROM hypotheses ORDER BY created_at_epoch"
            ).fetchall()
            evs = con.execute(
                "SELECT * FROM hypothesis_evidence ORDER BY created_at_epoch"
            ).fetchall()
            logs = con.execute(
                "SELECT * FROM hypothesis_log ORDER BY epoch"
            ).fetchall()
        return {
            "exported_at": _now().isoformat(),
            "schema_version": 1,
            "counts": {
                "hypotheses": len(hypos),
                "live": sum(1 for h in hypos if h["status"] in LIVE_STATUSES),
                "evidence": len(evs),
                "changes": len(logs),
            },
            "hypotheses": [self._row_to_hypo(h).to_public_dict()
                           for h in hypos],
            "evidence": [dict(e) for e in evs],
            "history": [dict(l) for l in logs],
        }

    def clear_all(self) -> int:
        """清空全部假设及证据（"清空全部"语义：失忆了就不该继续猜）。"""
        with self._lock, closing(self._connect()) as con:
            with con:
                cur = con.execute("DELETE FROM hypotheses")
                con.execute("DELETE FROM hypothesis_evidence")
                con.execute("DELETE FROM hypothesis_log")
        log.info("全部假设已清空（删除 %d 条）", cur.rowcount)
        return cur.rowcount

    # ---------- 证据 ----------
    def _source_exists(
        self, con: sqlite3.Connection, source_type: str, source_id: str
    ) -> bool:
        """证据必须追溯到真实行（同库查）。查不到 = 拒绝绑定。"""
        if source_type == "correction":
            # 纠正追溯到"被纠正的那条 Context"，或本次否决的用户行为记录
            hit = con.execute(
                "SELECT id FROM context_entries WHERE id=?", (source_id,)
            ).fetchone()
            if hit:
                return True
            hit = con.execute(
                "SELECT id FROM hypothesis_log WHERE id=? AND action IN"
                " ('reject', 'verify_confirm')",
                (source_id,),
            ).fetchone()
            return bool(hit)
        table = SOURCE_TABLE.get(source_type)
        if not table:
            return False
        try:
            hit = con.execute(
                f"SELECT id FROM {table} WHERE id=?", (source_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return False  # 表还不存在（如 fixture 库里没建 events 表）
        return bool(hit)

    @staticmethod
    def _row_day(con: sqlite3.Connection, source_type: str,
                 source_id: str) -> str:
        """证据来源行的"发生日"（独立性按来源日算，不按写入日）。

        同一天产生的多条 Context 只算 1 个源；不同天各算各的。
        correction 按纠正行为当天（行为本身就是独立用户动作）。
        """
        try:
            if source_type == "context":
                row = con.execute(
                    "SELECT created_at_iso FROM context_entries WHERE id=?",
                    (source_id,),
                ).fetchone()
                if row and row["created_at_iso"]:
                    return str(row["created_at_iso"])[:10]
            elif source_type == "event":
                row = con.execute(
                    "SELECT created_at_epoch FROM events WHERE id=?",
                    (source_id,),
                ).fetchone()
                if row and row["created_at_epoch"]:
                    return datetime.fromtimestamp(
                        row["created_at_epoch"]).astimezone().strftime(
                        "%Y-%m-%d")
        except sqlite3.OperationalError:
            pass
        return _now().strftime("%Y-%m-%d")

    @staticmethod
    def _default_origin(
        source_type: str, source_id: str, excerpt_day: str
    ) -> str:
        if source_type == "event":
            return f"event:{source_id}"
        if source_type == "correction":
            return f"correction:{source_id}:{excerpt_day}"
        return f"ctxday:{excerpt_day}"  # context：同日多条只算 1 个源（保守）

    def _independent_origins(self, hypo_id: str) -> int:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT COUNT(DISTINCT origin) FROM hypothesis_evidence"
                " WHERE hypothesis_id=? AND polarity='support'",
                (hypo_id,),
            ).fetchone()
        return int(row[0])

    def add_evidence(
        self,
        hypo_id: str,
        polarity: str,
        source_type: str,
        source_id: str,
        *,
        origin: str | None = None,
        excerpt: str = "",
        detail: dict | None = None,
    ) -> tuple[bool, str | None]:
        """绑定一条证据并更新置信度。失败返回 (False, 原因)。"""
        if polarity not in POLARITIES:
            return False, f"未知证据极性 {polarity}"
        if source_type not in SOURCE_TYPES:
            # 铁律：hypothesis 不在白名单里，永远不能成为证据
            return False, f"不允许的证据来源 {source_type}（假设不能证明自己）"
        if not source_id:
            return False, "证据来源 id 为空"
        with self._lock, closing(self._connect()) as con:
            hypo_row = con.execute(
                "SELECT confidence, status FROM hypotheses WHERE id=?",
                (hypo_id,),
            ).fetchone()
            if hypo_row is None:
                return False, "假设不存在"
            if hypo_row["status"] == "deleted":
                return False, "假设已删除，先恢复再绑定证据"
            if not self._source_exists(con, source_type, source_id):
                return False, f"证据来源不存在：{source_type}:{source_id[:8]}"
            # 默认 origin 取来源行的发生日（不是写入日），见 _row_day
            day = _now().strftime("%Y-%m-%d")
            origin = origin or self._default_origin(
                source_type, source_id,
                self._row_day(con, source_type, source_id)
                if source_type != "correction" else day)
            now = _now()
            dup = con.execute(
                "SELECT id FROM hypothesis_evidence WHERE hypothesis_id=?"
                " AND polarity=? AND source_type=? AND source_id=?",
                (hypo_id, polarity, source_type, source_id),
            ).fetchone()
            if dup:
                return False, "重复证据"
            conf_before = float(hypo_row["confidence"])
            if polarity == "support":
                conf = _clamp(conf_before + SUPPORT_DELTA)
                # 门控：独立源不够，写入时直接 cap（不是只显示时 cap）
                existing = {
                    r[0] for r in con.execute(
                        "SELECT DISTINCT origin FROM hypothesis_evidence"
                        " WHERE hypothesis_id=? AND polarity='support'",
                        (hypo_id,),
                    ).fetchall()
                }
                indep = len(existing) + (0 if origin in existing else 1)
                if indep < MIN_INDEPENDENT_FOR_HIGH:
                    conf = min(conf, HIGH_CONF_CAP)
            else:
                conf = _clamp(conf_before + CONTRADICT_DELTA)
            with con:
                con.execute(
                    "INSERT INTO hypothesis_evidence (id, hypothesis_id, polarity,"
                    " source_type, source_id, origin, excerpt, detail,"
                    " created_at_iso, created_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, hypo_id, polarity, source_type, source_id,
                     origin, (excerpt or "")[:200],
                     json.dumps(detail or {}, ensure_ascii=False),
                     now.isoformat(), now.timestamp()),
                )
                if polarity == "support":
                    con.execute(
                        "UPDATE hypotheses SET confidence=?, support_count ="
                        " support_count + 1, updated_at_iso=?, updated_at_epoch=?"
                        " WHERE id=?",
                        (conf, now.isoformat(), now.timestamp(), hypo_id),
                    )
                else:
                    con.execute(
                        "UPDATE hypotheses SET confidence=?, contradict_count ="
                        " contradict_count + 1, updated_at_iso=?,"
                        " updated_at_epoch=? WHERE id=?",
                        (conf, now.isoformat(), now.timestamp(), hypo_id),
                    )
                self._log(con, "support" if polarity == "support" else "contradict",
                          hypo_id, conf_before, conf,
                          {"source_type": source_type,
                           "source_id": source_id, "origin": origin,
                           **(detail or {})})
        log.info("假设 %s %s证据 %s:%s → %.2f",
                 hypo_id[:8], "支持" if polarity == "support" else "反证",
                 source_type, source_id[:8], conf)
        return True, None

    # ---------- 用户行为：确认 / 否决 / 纠正命中 ----------
    def confirm(self, hypo_id: str) -> bool:
        """用户确认：只改状态，不动 confidence（确认 ≠ 高置信度）。"""
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM hypotheses WHERE id=?",
                (hypo_id,),
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            if row["status"] == "confirmed":
                return True
            with con:
                # 任何渠道的解决（UI/对话）都清 pending：问题已答，不许留 stale
                con.execute(
                    "UPDATE hypotheses SET status='confirmed',"
                    " pending_question='', updated_at_iso=?,"
                    " updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), hypo_id),
                )
                self._log(con, "confirm", hypo_id, row["confidence"],
                          row["confidence"], {})
        log.info("假设 %s 被用户确认", hypo_id[:8])
        return True

    def reject(
        self, hypo_id: str, reason: str = "", channel: str = "trust_ui"
    ) -> bool:
        """用户否决：建反证（追溯到本次否决行为）+ 置 0.25 + 状态 rejected。

        幂等：已是 rejected 则直接返回 True（防双击刷证据数）。
        """
        reason = (reason or "").strip()[:200]
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM hypotheses WHERE id=?",
                (hypo_id,),
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            if row["status"] == "rejected":
                return True
            conf_before = float(row["confidence"])
            with con:
                log_id = uuid.uuid4().hex
                con.execute(
                    "INSERT INTO hypothesis_log (id, epoch, iso, action,"
                    " hypothesis_id, conf_before, conf_after, detail)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (log_id, now.timestamp(), now.isoformat(), "reject",
                     hypo_id, conf_before, CORRECTION_SET,
                     json.dumps({"reason": reason, "channel": channel},
                                ensure_ascii=False)),
                )
                con.execute(
                    "INSERT INTO hypothesis_evidence (id, hypothesis_id, polarity,"
                    " source_type, source_id, origin, excerpt, detail,"
                    " created_at_iso, created_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, hypo_id, "contradict", "correction",
                     log_id, f"denial:{log_id}",
                     reason,
                     json.dumps({"channel": channel}, ensure_ascii=False),
                     now.isoformat(), now.timestamp()),
                )
                con.execute(
                    "UPDATE hypotheses SET confidence=?, status='rejected',"
                    " contradict_count = contradict_count + 1,"
                    " pending_question='',"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (CORRECTION_SET, now.isoformat(), now.timestamp(), hypo_id),
                )
        log.info("假设 %s 被用户否决 → 0.25", hypo_id[:8])
        # V0.5.2 级联（D017 ②）：引用它的规律同步吃反证。
        # 本地导入防循环；失败只记日志，绝不掀翻否决主流程。
        try:
            from memory.pattern_store import PatternStore
            PatternStore(self.db_path).cascade_from_source(
                "hypothesis", hypo_id, reason or "假设被否决")
        except Exception as exc:
            log.warning("否决级联到规律失败（跳过）：%s", exc)
        return True

    def record_correction_hit(
        self,
        entry_id: str,
        user_text: str,
        reason: str = "",
    ) -> list[str]:
        """某条 Context 被纠正 → 以它为支持证据的假设全部吃反证。

        显式否决（用户原文含否决词）则升级为置 0.25 + rejected。
        返回受影响的假设 id 列表。
        """
        affected: list[str] = []
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT DISTINCT hypothesis_id FROM hypothesis_evidence"
                " WHERE source_type='context' AND source_id=?"
                " AND polarity='support'",
                (entry_id,),
            ).fetchall()
        hypo_ids = [r[0] for r in rows]
        for hid in hypo_ids:
            hypo = self.get(hid)
            if hypo is None or hypo.status == "deleted":
                continue
            if _is_explicit_denial(user_text):
                self.reject(hid, reason or user_text[:200], channel="chat")
            else:
                ok, _ = self.add_evidence(
                    hid, "contradict", "correction", entry_id,
                    excerpt=(reason or user_text)[:200],
                    detail={"channel": "chat", "reason": reason},
                )
                if not ok:
                    continue
            affected.append(hid)
        return affected

    # ---------- V0.4 验证提问：pending 状态 ----------
    def get_pending(self) -> Hypothesis | None:
        """全局唯一的挂起问题（没有则 None）。"""
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM hypotheses WHERE pending_question != ''"
                " ORDER BY updated_at_epoch DESC LIMIT 1",
            ).fetchone()
        return self._row_to_hypo(row) if row else None

    def set_pending(self, hypo_id: str, question: str) -> tuple[bool, str | None]:
        """挂起一个验证问题：计数+1、刷新冷却起点、记 ask 日志。"""
        question = (question or "").strip()
        if not question:
            return False, "问题文本为空"
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status FROM hypotheses WHERE id=?", (hypo_id,)
            ).fetchone()
            if not row:
                return False, "假设不存在"
            if row["status"] != "tentative":
                return False, f"只有待验证假设能挂问题（当前 {row['status']}）"
            other = con.execute(
                "SELECT id FROM hypotheses WHERE pending_question != ''"
                " AND id != ? LIMIT 1",
                (hypo_id,),
            ).fetchone()
            if other:
                return False, "已有挂起问题，同时只挂一个"
            with con:
                con.execute(
                    "UPDATE hypotheses SET pending_question=?,"
                    " asked_count = asked_count + 1,"
                    " last_asked_epoch=?, updated_at_iso=?, updated_at_epoch=?"
                    " WHERE id=?",
                    (question[:500], now.timestamp(), now.isoformat(),
                     now.timestamp(), hypo_id),
                )
                self._log(con, "ask", hypo_id, None, None,
                          {"question": question[:500]})
        log.info("假设 %s 挂起验证问题（第 %d 问）", hypo_id[:8],
                 self.get(hypo_id).asked_count)
        return True, None

    def clear_pending(self, hypo_id: str) -> bool:
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT pending_question FROM hypotheses WHERE id=?",
                (hypo_id,),
            ).fetchone()
            if not row or not row["pending_question"]:
                return False
            with con:
                con.execute(
                    "UPDATE hypotheses SET pending_question='' WHERE id=?",
                    (hypo_id,),
                )
        return True

    def note_evasive(self, hypo_id: str) -> bool:
        """用户回避作答：不清挂起，但刷新冷却（不立刻重问），记审计。"""
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT confidence FROM hypotheses WHERE id=?", (hypo_id,)
            ).fetchone()
            if not row:
                return False
            with con:
                con.execute(
                    "UPDATE hypotheses SET last_asked_epoch=?,"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (now.timestamp(), now.isoformat(), now.timestamp(),
                     hypo_id),
                )
                self._log(con, "evasive", hypo_id, row["confidence"],
                          row["confidence"], {})
        return True

    def record_verification_confirm(
        self, hypo_id: str, user_text: str
    ) -> bool:
        """用户亲口确认验证问题：confirm（置信度不动）+ 回答作为 support
        证据入链（追溯到本次确认行为），清 pending。

        注意：这是有意的例外——用户 verdict 不是统计证据，所以 confidence
        不走 +0.08；但 support_count 照常 +1（证据链完整）。
        """
        user_text = (user_text or "").strip()[:200]
        if not user_text:
            return False
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM hypotheses WHERE id=?",
                (hypo_id,),
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            conf = float(row["confidence"])
            with con:
                log_id = uuid.uuid4().hex
                con.execute(
                    "INSERT INTO hypothesis_log (id, epoch, iso, action,"
                    " hypothesis_id, conf_before, conf_after, detail)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (log_id, now.timestamp(), now.isoformat(),
                     "verify_confirm", hypo_id, conf, conf,
                     json.dumps({"answer": user_text, "channel": "verification"},
                                ensure_ascii=False)),
                )
                con.execute(
                    "INSERT INTO hypothesis_evidence (id, hypothesis_id,"
                    " polarity, source_type, source_id, origin, excerpt, detail,"
                    " created_at_iso, created_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, hypo_id, "support", "correction",
                     log_id, f"answer:{log_id}", user_text,
                     json.dumps({"channel": "verification"},
                                ensure_ascii=False),
                     now.isoformat(), now.timestamp()),
                )
                con.execute(
                    "UPDATE hypotheses SET status='confirmed',"
                    " support_count = support_count + 1, pending_question='',"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), hypo_id),
                )
        log.info("假设 %s 被验证确认（置信度保持 %.2f）", hypo_id[:8], conf)
        return True

    def independent_support_count(self, hypo_id: str) -> int:
        """公开版独立源计数（verifier 选人用）"""
        return self._independent_origins(hypo_id)

    # ---------- 删除 / 恢复 ----------
    def delete_entry(self, hypo_id: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM hypotheses WHERE id=?",
                (hypo_id,),
            ).fetchone()
            if not row or row["status"] == "deleted":
                return False
            with con:
                con.execute(
                    "UPDATE hypotheses SET status='deleted', pending_question='',"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), hypo_id),
                )
                self._log(con, "delete", hypo_id, row["confidence"],
                          row["confidence"], {})
        log.info("假设删除 %s", hypo_id[:8])
        return True

    def restore_entry(self, hypo_id: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, confidence FROM hypotheses WHERE id=?",
                (hypo_id,),
            ).fetchone()
            if not row or row["status"] != "deleted":
                return False
            with con:
                con.execute(
                    "UPDATE hypotheses SET status='tentative', updated_at_iso=?,"
                    " updated_at_epoch=? WHERE id=?",
                    (now.isoformat(), now.timestamp(), hypo_id),
                )
                self._log(con, "restore", hypo_id, row["confidence"],
                          row["confidence"], {})
        log.info("假设恢复 %s（回 tentative）", hypo_id[:8])
        return True

    @staticmethod
    def _log(
        con: sqlite3.Connection,
        action: str,
        hypo_id: str,
        conf_before: float | None,
        conf_after: float | None,
        detail: dict | None,
    ) -> None:
        now = _now()
        con.execute(
            "INSERT INTO hypothesis_log (id, epoch, iso, action, hypothesis_id,"
            " conf_before, conf_after, detail) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, now.timestamp(), now.isoformat(), action,
             hypo_id, conf_before, conf_after,
             json.dumps(detail or {}, ensure_ascii=False)),
        )
