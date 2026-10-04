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
import time
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

# ---------- V0.5.9 后台候选生命周期 ----------
# status 是 TEXT 列，新增状态值**不需要 migration**。
# 生命周期：pending → accepted → confirmed（毕业成正式理解）
#           pending/accepted → rejected（用户否定）
#           pending/accepted → expired（长期无支持，自动淘汰）
BACKGROUND_PREFIX = "background:"
PENDING = "pending"
ACCEPTED = "accepted"
EXPIRED = "expired"
# 候选在"多久没有任何动静"之后判为失效。
# 复用已有的 updated_at_epoch —— 不新增任何列。
CANDIDATE_STALE_DAYS = 30
# 语义去重的比较单位（不是整句）：两个 topic 能共享一个非停用二连字就算同一件事
CANDIDATE_DUP_GRAMS = 1
# 毕业门槛：直接用既有的 HIGH_CONF_CAP / MIN_INDEPENDENT_FOR_HIGH，
# 不为后台推理另立一套标准。
GRADUATE_MIN_INDEP = MIN_INDEPENDENT_FOR_HIGH

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
        confidence: float | None = None,
    ) -> tuple[Hypothesis | None, str | None]:
        statement = (statement or "").strip()[:max_chars]
        scope = (scope or "").strip()[:SCOPE_MAX]
        if not statement:
            return None, "假设内容为空"
        if not scope:
            return None, "适用范围为空"
        # V0.5.8：后台推测要写自己的置信度（比 INIT 低）。否则库里
        # 显示 0.50 而它自称 0.33——看起来比实际更可信。
        conf = _clamp(INIT_CONFIDENCE if confidence is None else confidence)
        now = _now()
        hypo = Hypothesis(
            id=uuid.uuid4().hex, statement=statement,
            confidence=conf, status="tentative", scope=scope,
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
                    (hypo.id, statement, conf, "tentative", scope,
                     0, 0, 0, now.isoformat(), now.timestamp(),
                     now.isoformat(), now.timestamp()),
                )
                self._log(con, "create", hypo.id, None, conf,
                          detail or {"scope": scope})
        log.info("假设新增 [tentative·%.2f] %s", conf, statement[:60])
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
        """进入 Prompt 的假设：tentative + confirmed，按置信度倒序。

        V0.5.8：**后台推测一律不进 prompt**。它恒为 tentative，
        而 tentative 默认是 live 的——不挡就等于让 AI 拿自己的猜测
        当事实去回答用户。与 quota_overflow 同样的处理。

        Deep Understanding：**合成候选同样一律不进 prompt**（第十八节）。
        它恒为 pending，而 pending 本来就不在 LIVE_STATUSES 里，所以
        `live_only=True` 已经挡住一道；这里再显式挡一次前缀，
        是为了让"为什么它进不了 prompt"这件事写在代码里而不是靠约定。
        """
        out = []
        for h in self.list_hypotheses(live_only=True, limit=limit):
            if (h.scope or "").startswith("background:"):
                continue
            if (h.scope or "").startswith("synthesis:"):
                continue
            out.append(h)
        return out

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
                " ('reject', 'verify_confirm', 'change')",
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

    # ---------- V0.5.6 自然证据（用户主动说出来的话）----------
    # 复用既有 correction 来源机制：不新增 source_type，只写一条真实的
    # hypothesis_log，它的 id 就是合法证据来源，可追溯到用户原话。
    @staticmethod
    def _natural_eligible(status: str, scope: str) -> bool:
        """自然证据只能碰 live 且可验证的假设。

        rejected / deleted 不可复活；quota_overflow 是 V0.5.3 设的安全
        边界（不可验证、不可晋级、不可自然确认），这里一并挡住。
        """
        if status not in ("tentative", "confirmed", ACCEPTED):
            return False
        if (scope or "").startswith("quota_overflow:"):
            return False
        # V0.5.9：pending 是"还没人裁决的猜测"，不许吃自然证据；
        # accepted 是"用户认可、等真实证据"的候选——这一段正是
        # 它靠真实用户证据长大的唯一入口，必须放行。
        if (scope or "").startswith("background:") \
                and status != ACCEPTED:
            return False
        # 合成候选同构：pending 是没人裁决的猜测（不许吃自然证据）；
        # accepted 是用户已认可的高层推测，必须放行——那是它长大的
        # 唯一入口（第十二节故事 1 的关键一步）。
        if (scope or "").startswith("synthesis:") \
                and status != ACCEPTED:
            return False
        return True

    def _natural_eligible_check(self, hypo_id: str) -> bool:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, scope FROM hypotheses WHERE id=?", (hypo_id,),
            ).fetchone()
        return bool(row) and self._natural_eligible(row["status"],
                                                    row["scope"])

    def record_natural_evidence(
        self,
        hypo_id: str,
        user_text: str,
        signal: str,
        *,
        superseded_entry_id: str = "",
    ) -> bool:
        """用户自然表达 → 假设证据。

        support：走 add_evidence 的统计路径（+0.08），但**不改 status**——
                 用户说一句"对"不等于这条理解已被验证，更不等于可以
                直接晋级成 Pattern（晋级门槛不变）。
        deny：   话题强相关时走既有 reject()（置 0.25 + rejected + 规律级联）。
        change：只吃反证（-0.25），**不改 status**——用户可能只是暂时
                变了，不能把旧理解永久判死；历史完整保留可追溯。
        deny_weak：话题只有弱相关时用，只吃反证。否决是不可逆的，
                证据不足时绝不能用它；降权是安全的。
        """
        user_text = (user_text or "").strip()[:200]
        if not user_text:
            return False
        if signal == "support":
            return self._natural_support(hypo_id, user_text)
        if signal == "deny":
            # reject() 自己只挡 deleted/rejected；这里必须补一道，
            # 否则 quota_overflow 这类"不可验证"的假设能被自然否定，
            # 还会顺着 reject 的级联打到 pattern——绕过了 V0.5.3 的边界。
            if not self._natural_eligible_check(hypo_id):
                return False
            return self.reject(hypo_id, user_text, channel="natural")
        if signal == "change":
            return self._natural_contradict(
                hypo_id, user_text, superseded_entry_id or None,
                log_action="change", channel="natural_change")
        if signal == "deny_weak":
            return self._natural_contradict(
                hypo_id, user_text, superseded_entry_id or None,
                log_action="change", channel="natural_deny")
        return False

    @staticmethod
    def _natural_log_id(hypo_id: str, user_text: str, now) -> str:
        """自然证据的确定性 log id。

        来源 id 必须可复现，否则同一句话重复说会每次生成新行，
        add_evidence 的去重索引就抓不住，support_count 会凭空翻倍。
        口径与既有 origin 一致：同一条陈述、同一天 = 同一个来源；
        换了说法或跨天 = 新的独立来源。
        """
        norm = "".join(ch for ch in (user_text or "")
                       if not ch.isspace() and ch not in "，。！？、；：,.!?;:~…—")
        day = now.strftime("%Y-%m-%d")
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"personal-ai:natural:{hypo_id}:{day}:{norm}",
        ).hex

    def _natural_support(self, hypo_id: str, user_text: str) -> bool:
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, scope FROM hypotheses WHERE id=?", (hypo_id,),
            ).fetchone()
            if not row or not self._natural_eligible(row["status"],
                                                      row["scope"]):
                return False
            log_id = self._natural_log_id(hypo_id, user_text, now)
            with con:
                con.execute(
                    "INSERT OR IGNORE INTO hypothesis_log (id, epoch, iso,"
                    " action, hypothesis_id, conf_before, conf_after, detail)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (log_id, now.timestamp(), now.isoformat(),
                     "verify_confirm", hypo_id, None, None,
                     json.dumps({"answer": user_text, "channel": "natural",
                                 "signal": "support"}, ensure_ascii=False)),
                )
        ok, err = self.add_evidence(
            hypo_id, "support", "correction", log_id,
            origin=f"natural:{log_id}", excerpt=user_text,
            detail={"channel": "natural", "signal": "support"},
        )
        if not ok:
            log.warning("自然支持证据落链失败（跳过）：%s", err)
            return False
        log.info("假设 %s 收到用户自然支持（+%.2f）",
                 hypo_id[:8], SUPPORT_DELTA)
        return True

    def _natural_contradict(self, hypo_id: str, user_text: str,
                            superseded_entry_id: str | None, *,
                            log_action: str, channel: str) -> bool:
        """只吃反证、不动 status。可逆动作的统一落点（变化 / 弱否定）。"""
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, scope, confidence FROM hypotheses WHERE id=?",
                (hypo_id,),
            ).fetchone()
            if not row or not self._natural_eligible(row["status"],
                                                      row["scope"]):
                return False
            source_id = superseded_entry_id
            if not source_id:
                # 无对应 Context 可追溯时，用本次用户行为本身作为来源。
                # id 同样确定性生成：同一条陈述同一天重复出现算一次反证。
                source_id = self._natural_log_id(hypo_id, user_text, now)
                with con:
                    con.execute(
                        "INSERT OR IGNORE INTO hypothesis_log (id, epoch, iso, action,"
                        " hypothesis_id, conf_before, conf_after, detail)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (source_id, now.timestamp(), now.isoformat(),
                         log_action, hypo_id, row["confidence"], None,
                         json.dumps({"excerpt": user_text, "channel": channel},
                                    ensure_ascii=False)),
                    )
        ok, err = self.add_evidence(
            hypo_id, "contradict", "correction", source_id,
            excerpt=user_text, detail={"channel": channel},
        )
        if not ok:
            log.warning("自然反证落链失败（跳过）：%s", err)
            return False
        log.info("假设 %s 收到%s信号（降权 %.2f，状态保留）",
                 hypo_id[:8], channel, CONTRADICT_DELTA)
        return True

    # ---------- V0.5.8 Background Reasoning ----------
    def background_proposed(self, scope: str) -> bool:
        """这个 scope 的后台推测是否已经提过。

        用户点过"不对"之后就不该再提——`reject()` 把 status 置为
        rejected，本查询用"存在任一非 deleted 记录"来覆盖三种情况：
        待确认（pending）、已认可、已否决。都在这儿挡住。
        """
        scope = (scope or "").strip()[:SCOPE_MAX]
        if not scope:
            return True
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT 1 FROM hypotheses WHERE scope=? AND status!='deleted'"
                " LIMIT 1", (scope,)).fetchone()
        return bool(row)

    def synthesis_proposed(self, scope: str) -> bool:
        """这个 scope 的高层推测是否已经提过。

        与 `background_proposed` 同一套语义：用户点过"不对"之后
        不该再提。分开写而不是共用——前缀不同就该分开判断，
        合成候选的"提过"绝不能因为后台候选存在而被挡住，反之亦然。
        """
        scope = (scope or "").strip()[:SCOPE_MAX]
        if not scope:
            return True
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT 1 FROM hypotheses WHERE scope=? AND status!='deleted'"
                " LIMIT 1", (scope,)).fetchone()
        return bool(row)

    def list_pending_background(self, limit: int = 50) -> list:
        """待裁决的后台候选（定向查询，不扫全表）。

        V0.5.10：对话裁决每回合都要问一次"有没有待裁决的候选"。
        用 list_hypotheses(live_only=False) 拉 200 条再在 Python 里
        过滤，实测给每个可表态的回合平白加了约 30ms。
        """
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM hypotheses WHERE status=? AND scope LIKE"
                " 'background:%' ORDER BY updated_at_epoch DESC LIMIT ?",
                (PENDING, limit)).fetchall()
        return [self._row_to_hypo(r) for r in rows]

    def is_duplicate_candidate(self, scope: str) -> bool:
        """语义去重：新推测跟已有的活候选说的是不是同一件事。

        精确 scope 去重挡不住换了措辞的重复——三条规则完全可能从
        同一份行为数据里各说一句话（实测就有：behavior_change 与
        focus_shift 同时指向"学习"）。

        比的是 **topic**（从 scope 尾部取），不是整句 reason。
        早先版本比整句，结果模板套话（"最近 7 天有 N 次记录是…"）
        自己就共享好几个二连字，把"学习"和"编程"误判成同一件事。

        纯内容比较，不调模型——提案链上不引入抖动。
        """
        from core.natural_evidence import _content_grams

        topic = str(scope or "").split(":", 2)[-1].strip()
        grams = _content_grams(topic)
        if not grams:
            return False
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT scope FROM hypotheses WHERE scope LIKE"
                " 'background:%' AND status IN (?,?)",
                (PENDING, ACCEPTED)).fetchall()
        for r in rows:
            other = str(r["scope"] or "").split(":", 2)[-1].strip()
            if not other:
                continue
            if grams & _content_grams(other):
                return True
        return False

    def create_background_candidate(self, candidate) -> "Hypothesis | None":
        """把一条后台推测落成 **pending** 候选。

        复用既有表结构，不新建表、不加字段——来源信息全部记在
        hypothesis_log.detail 里（既有 {"rule": ...} 惯例）。
        """
        if self.background_proposed(candidate.scope):
            return None
        if self.is_duplicate_candidate(candidate.scope):
            return None
        conf = _clamp(candidate.confidence)
        hypo_id = uuid.uuid4().hex
        now = _now()
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT INTO hypotheses (id, statement, confidence,"
                    " status, scope, support_count, contradict_count,"
                    " used_count, created_at_iso, created_at_epoch,"
                    " updated_at_iso, updated_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (hypo_id, candidate.reason[:300], conf, PENDING,
                     candidate.scope[:SCOPE_MAX], 0, 0, 0,
                     now.isoformat(), now.timestamp(),
                     now.isoformat(), now.timestamp()),
                )
                self._log(con, "create", hypo_id, None, conf,
                          {"rule": "background_reasoning",
                           "type": candidate.type,
                           "topic": candidate.topic,
                           "confidence": round(conf, 2),
                           "reason": candidate.reason,
                           "evidence": list(candidate.evidence),
                           "detail": dict(candidate.detail)})
        log.info("后台候选新增 [pending·%.2f] %s", conf, candidate.reason[:50])
        return self.get(hypo_id)

    # ---------- Deep Understanding：Cross-Context Synthesis ----------
    def create_synthesis_candidate(self, candidate) -> "Hypothesis | None":
        """把一条高层推测落成 **pending** 候选。

        复用既有表结构，**不新建表、不加字段**——与
        `create_background_candidate` 完全同构：

        - scope 用 `synthesis:` 前缀，与 `background:` 一样是"存但不生效"，
          于是既有的那 7 处安全过滤（prompt / pattern / 「我眼中的你」/
          Judgment / 自然证据白名单…）一处都不用改。
        - 聚合结构（哪些经历、跨几个主题、什么关系）记在
          `hypothesis_log.detail`，沿用既有 `{"rule": ...}` 惯例。
        - 候选**不吃自然证据**（status=pending 不在白名单里），
          也不进 prompt、不影响 Judgment、不参与 pattern。
        """
        if self.synthesis_proposed(candidate.scope):
            return None
        if self.is_duplicate_candidate(candidate.scope):
            return None
        conf = _clamp(candidate.confidence)
        hypo_id = uuid.uuid4().hex
        now = _now()
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT INTO hypotheses (id, statement, confidence,"
                    " status, scope, support_count, contradict_count,"
                    " used_count, created_at_iso, created_at_epoch,"
                    " updated_at_iso, updated_at_epoch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (hypo_id, candidate.statement[:300], conf, PENDING,
                     candidate.scope[:SCOPE_MAX], 0, 0, 0,
                     now.isoformat(), now.timestamp(),
                     now.isoformat(), now.timestamp()),
                )
                # 注意：`detail` 是**摊平**写进这一行的，不是嵌套在
                # 下一层。第一次写成 {"detail": dict(candidate.detail)}，
                # 于是取用方按 candidate.detail 的键去读（evidence_until /
                # activities / relation …）全都读不到——而症状是
                # "变化检测静默不触发"，看起来像规则没实现。
                # 摊平之后和 background 侧的 detail 惯例一致：
                # 顶层就有 rule / type / topic。
                self._log(con, "create", hypo_id, None, conf,
                          {"rule": "cross_context_synthesis",
                           "pattern": candidate.pattern,
                           "topic": candidate.topic,
                           "confidence": round(conf, 2),
                           "statement": candidate.statement,
                           "reason": candidate.reason,
                           "members": list(candidate.members),
                           "contexts": list(candidate.contexts),
                           "independent_days": candidate.independent_days,
                           **dict(candidate.detail)})
        log.info("合成候选新增 [pending·%.2f] %s", conf,
                 candidate.statement[:50])
        return self.get(hypo_id)

    def record_synthesis_verdict(
        self, hypo_id: str, verdict: str, user_text: str = ""
    ) -> bool:
        """用户对高层推测的裁决。

        语义与 `record_background_verdict` 完全一致，且**刻意保持一致**：

        - 正确 → **只补一条 support 证据**，绝不自动 confirmed。
          用户点一下"对"不构成系统可以拿它当事实的证据量。
        - 不对 → `reject()`，落 rejected，从此不再提出。

        守卫同样放在分支之前：只有 `synthesis:` 前缀能被裁决。
        漏在这里等于给这个端点开了一把能否决任意假设的钥匙。
        """
        user_text = (user_text or "").strip()[:200]
        if verdict not in ("correct", "wrong"):
            return False
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, scope, confidence FROM hypotheses WHERE id=?",
                (hypo_id,)).fetchone()
        if not row or row["status"] == "deleted":
            return False
        if not str(row["scope"]).startswith("synthesis:"):
            return False
        if verdict == "wrong":
            self.reject(hypo_id, reason=f"用户不认可这个模式：{user_text[:80]}"
                           if user_text else "用户不认可这个模式")
            return True
        # 认可：只补证据。
        #
        # **status 也要推进**（pending → accepted）。这是与
        # `record_background_verdict` 的一处刻意差异，必须写清楚：
        # 那边点"对"之后候选停在 pending，自然证据永远进不来，
        # 于是它只能永远停在"用户认可过的猜测"，永远长不大——
        # V0.5.9 的 `_natural_eligible` 只放行 accepted。
        #
        # 为什么合成候选不学这个"安全"：
        # `accepted` 仍然**不是 confirmed**——它依然不在 LIVE_STATUSES 里，
        # 于是依旧不进 prompt、不进 pattern、不进「我眼中的你」、
        # 不影响 Judgment。accepted 的唯一含义是"用户认可过了，
        # 真实证据可以开始支持它"，而毕业还需要另外两把尺子
        # （置信度 ≥ 0.70 且独立源 ≥ 2）。
        # 所以推进 status 换来的只是"能长大"，不是"能生效"。
        #
        # 而且**不自动 confirmed**：用户点一下"对"不构成系统可以
        # 拿它当事实的证据量——这条和 background 侧一模一样。
        #
        # 用确定性 id：重复表态同一句话不会让 support 凭空翻倍。
        now = _now()
        log_id = uuid.uuid5(uuid.NAMESPACE_URL,
                            f"{hypo_id}|synthesis_verdict|{user_text[:60]}").hex
        with self._lock, closing(self._connect()) as con:
            with con:
                exists = con.execute(
                    "SELECT 1 FROM hypothesis_log WHERE id=?",
                    (log_id,)).fetchone()
                if not exists:
                    # action 用 'verify_confirm'：_source_exists 对
                    # correction 的特判接受的三个 action 之一。
                    _log_write_id(con, log_id, "verify_confirm", hypo_id,
                                  None, None,
                                  {"channel": "synthesis_verdict",
                                   "text": user_text[:200]})
                if row["status"] == PENDING:
                    con.execute(
                        "UPDATE hypotheses SET status=?, updated_at_iso=?,"
                        " updated_at_epoch=? WHERE id=? AND status=?",
                        (ACCEPTED, now.isoformat(), now.timestamp(),
                         hypo_id, PENDING))
                    _log_write_id(con, uuid.uuid4().hex, "accept",
                                  hypo_id, float(row["confidence"]),
                                  float(row["confidence"]),
                                  {"rule": "synthesis_verdict",
                                   "from_status": PENDING,
                                   "to_status": ACCEPTED})
        ok, err = self.add_evidence(
            hypo_id, "support", "correction", log_id,
            origin=f"synthesis_verdict:{now.strftime('%Y-%m-%d')}",
            excerpt=user_text[:120],
            detail={"channel": "synthesis_verdict"})
        if not ok:
            log.warning("合成候选认可证据落链失败（跳过）：%s", err)
            return False
        return True

    def record_background_verdict(
        self, hypo_id: str, verdict: str, user_text: str = ""
    ) -> bool:
        """用户对后台推测的裁决。

        正确：**只补一条 support 证据**，绝不自动 confirmed——
              用户点一下"对"，不构成系统可以拿它当事实的证据量。
        不对：走 reject()，落 rejected，从此不再提出。
        """
        user_text = (user_text or "").strip()[:200]
        if verdict not in ("correct", "wrong"):
            return False
        # 守卫放在分支之前：只有后台推测能被裁决。
        # 漏在这里等于给这个端点开了一把能否决任意假设的钥匙。
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT status, scope, confidence FROM hypotheses WHERE id=?",
                (hypo_id,)).fetchone()
        if not row or row["status"] == "deleted":
            return False
        if not str(row["scope"]).startswith("background:"):
            return False
        if verdict == "wrong":
            return self.reject(hypo_id, user_text or "这条推测不对",
                               channel="background")
        now = _now()
        # log id 必须确定性生成：同一句话连点 5 次会生成 5 条不同的
        # log 行，add_evidence 的去重索引（candidate+polarity+source）
        # 就抓不住，support_count 会一路涨到毕业线以上——用户点一次
        # 的心意被当成 5 次真实证据。实测：连点 4 次 conf 0.40→0.72。
        # 口径与自然证据一致：同一条裁决、同一天 = 同一个来源。
        log_id = self._natural_log_id(hypo_id, verdict + "|" + user_text, now)
        with self._lock, closing(self._connect()) as con:
            with con:
                con.execute(
                    "INSERT OR IGNORE INTO hypothesis_log (id, epoch, iso,"
                    " action, hypothesis_id, conf_before, conf_after, detail)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (log_id, now.timestamp(), now.isoformat(),
                     "verify_confirm", hypo_id, row["confidence"],
                     row["confidence"],
                     json.dumps({"answer": user_text or "对",
                                 "channel": "background",
                                 "signal": "user_correct"},
                                ensure_ascii=False)),
                )
                if row["status"] == PENDING:
                    # 认可 ≠ 成熟：进 accepted，等真实用户证据。
                    # 直接给 confirmed 就等于让后台推理自我确认——
                    # 它提猜测、用户点一下、猜测变事实、进 prompt。
                    con.execute(
                        "UPDATE hypotheses SET status=?, updated_at_iso=?,"
                        " updated_at_epoch=? WHERE id=?",
                        (ACCEPTED, now.isoformat(), now.timestamp(), hypo_id),
                    )
        ok, err = self.add_evidence(
            hypo_id, "support", "correction", log_id,
            excerpt=user_text or "用户认为这条推测是对的",
            detail={"channel": "background", "signal": "user_correct"},
        )
        if not ok:
            # "重复证据" 不是失败：同一句话连点多次，裁决早已生效。
            # 状态也已经推进到 accepted 了，这里保持幂等成功。
            if err == "重复证据":
                log.info("后台候选 %s 重复裁决（幂等，不重复计证据）",
                         hypo_id[:8])
                return True
            log.warning("后台候选认可落证据失败：%s", err)
            return False
        log.info("后台候选 %s 被认可 → accepted（等真实证据）", hypo_id[:8])
        return True

    # ---------- V0.5.9 成长 / 淘汰 ----------
    def graduate_candidates(self, *, now: float | None = None) -> list[str]:
        """把证据足够的 accepted 候选升级为**正式理解**。

        门槛直接用既有的两把尺子，不为后台推理另立标准：
          - 置信度 ≥ HIGH_CONF_CAP(0.70)。这个数只有在**独立来源 ≥2**
            时才可能达到（add_evidence 里写死的 cap），所以它本身
            就蕴含了"至少两个不同日子的真实证据"。
          - 独立来源数 ≥ MIN_INDEPENDENT_FOR_HIGH(2)。

        毕业动作 = status→confirmed + scope 从 `background:` 换成
        `inferred:`。换前缀是关键：`background:` 的那 7 处过滤只认
        前缀，换掉之后它自然进入 prompt / pattern /「我眼中的你」，
        而不需要在每个地方再加一条例外。

        Deep Understanding 的合成候选走**同一条路径、同一把尺子**
        （第十七节：不改 Pattern 晋级门槛、不为高层模式另立标准）。
        它的 `synthesis:` 前缀同样只是"存但不生效"，毕业时换掉即可。

        注意 `split(":", 1)[-1]` 只脱第一层前缀，所以
        `synthesis:exploration_depth:X` 毕业成
        `inferred:exploration_depth:X`——两层都在，正好让
        `topic_name()` 还能认出规则名（见 understanding_view）。
        """
        out: list[str] = []
        if self.paused():
            return out
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT id, scope, confidence FROM hypotheses"
                " WHERE status=? AND scope LIKE 'background:%'",
                (ACCEPTED,)).fetchall()
            # 合成候选走同一条毕业路径、同一把尺子（第十七节：
            # 不为高层模式另立标准）。前缀换成 inferred:synthesis:
            # —— 那几处安全过滤只认 background:/quota_overflow:，
            # 换掉前缀即自然放行，不用到处加例外。
            rows += con.execute(
                "SELECT id, scope, confidence FROM hypotheses"
                " WHERE status=? AND scope LIKE 'synthesis:%'",
                (ACCEPTED,)).fetchall()
        for r in rows:
            if float(r["confidence"]) < HIGH_CONF_CAP:
                continue
            if self._independent_origins(r["id"]) < GRADUATE_MIN_INDEP:
                continue
            now_s = _now()
            with self._lock, closing(self._connect()) as con:
                with con:
                    con.execute(
                        "UPDATE hypotheses SET status='confirmed',"
                        " scope=?, updated_at_iso=?, updated_at_epoch=?"
                        " WHERE id=? AND status=?",
                        ("inferred:" + r["scope"].split(":", 1)[-1],
                         now_s.isoformat(), now_s.timestamp(),
                         r["id"], ACCEPTED),
                    )
                    self._log(con, "promote", r["id"], r["confidence"],
                              r["confidence"],
                              {"rule": "graduate",
                               "from_scope": r["scope"],
                               "to_scope": "inferred:"})
            out.append(r["id"])
            log.info("后台候选 %s 毕业 → 正式理解（独立源达标）", r["id"][:8])
        return out

    def expire_stale_candidates(
        self, *, stale_days: int = CANDIDATE_STALE_DAYS,
        now: float | None = None,
    ) -> list[str]:
        """淘汰长期没有任何动静的候选。

        "动静"看已有的 `updated_at_epoch`——add_evidence / 裁决都会
        刷新它，所以不需要新增任何列。

        淘汰只改状态为 expired，**不删行**：用户确实说过这些话，
        历史必须可追溯（这是 D022 定下的规矩）。
        """
        now_f = time.time() if now is None else now
        cutoff = now_f - stale_days * 86400
        out: list[str] = []
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT id FROM hypotheses WHERE status IN (?,?)"
                " AND scope LIKE 'background:%' AND updated_at_epoch < ?",
                (PENDING, ACCEPTED, cutoff)).fetchall()
        for r in rows:
            t = _now()
            with self._lock, closing(self._connect()) as con:
                with con:
                    con.execute(
                        "UPDATE hypotheses SET status=?, pending_question='',"
                        " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                        (EXPIRED, t.isoformat(), t.timestamp(), r["id"]),
                    )
                    self._log(con, "expire", r["id"], None, None,
                              {"rule": "stale",
                               "stale_days": stale_days})
            out.append(r["id"])
            log.info("后台候选 %s 长期无支持 → expired（%d 天）",
                     r["id"][:8], stale_days)
        return out

    def paused(self) -> bool:
        """隐私暂停标记（与 ContextStore 同口径，读 meta 表）。"""
        try:
            with closing(self._connect()) as con:
                row = con.execute(
                    "SELECT value FROM meta WHERE key='privacy_paused'"
                ).fetchone()
            return bool(row and row["value"] == "1")
        except Exception:
            return False

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
        _log_write_id(con, uuid.uuid4().hex, action, hypo_id,
                      conf_before, conf_after, detail)


def _log_write_id(
        con: sqlite3.Connection,
        log_id: str,
        action: str,
        hypo_id: str,
        conf_before: float | None,
        conf_after: float | None,
        detail: dict | None,
    ) -> None:
    """和 `_log` 一样，但允许指定 log id。

    自然证据那条链需要**确定性 id**：同一天说同一句话两次，
    不该让 support_count 凭空翻倍（V0.5.6 的坑）。
    """
    now = _now()
    con.execute(
        "INSERT INTO hypothesis_log (id, epoch, iso, action, hypothesis_id,"
        " conf_before, conf_after, detail) VALUES (?,?,?,?,?,?,?,?)",
        (log_id, now.timestamp(), now.isoformat(), action,
         hypo_id, conf_before, conf_after,
         json.dumps(detail or {}, ensure_ascii=False)),
    )
