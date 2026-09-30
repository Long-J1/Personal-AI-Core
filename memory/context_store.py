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

# ---------- V0.5.2 信任边界（常量，单测锁定）----------
# memory_status：active（可用）/ outdated（过时）/ rejected（被否认）
# prompt 只读 active；历史保留可审计，不物理删除。
MEMORY_STATUSES = ("active", "outdated", "rejected")
# source_grade：user_direct（用户原话）/ user_confirmed（用户确认）/
# db_grounded（既有理解的复述——有据可查才准进 memory，置信 0.8）/
# inferred（模型推测，不进 memory，只进 hypothesis）/
# system_generated（系统生成，拒绝入口，永不写入）
SOURCE_GRADES = ("user_direct", "user_confirmed", "inferred",
                 "system_generated", "db_grounded")
DECAY_HALF_LIFE_DAYS = 30  # 闲置半衰期（天）：权重每 30 天减半
CORRECTION_SET = 0.25      # 纠正/否认置信置位（与假设、规律的置位对齐）

# ---------- P1 沉淀卫生（常量，单测锁定）----------
SIM_MERGE_THRESHOLD = 0.6  # 二连字 Jaccard 相似度，达到即合并
KIND_WEEKLY_CAP = 10       # 同 kind 7 天新增上限，超了只合并
RECENT_DAYS = 7

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
  used_count INTEGER NOT NULL DEFAULT 0,
  memory_status TEXT NOT NULL DEFAULT 'active',
  status_reason TEXT NOT NULL DEFAULT '',
  source_grade TEXT NOT NULL DEFAULT 'inferred',
  confidence REAL NOT NULL DEFAULT 0.5,
  last_verified REAL NOT NULL DEFAULT 0.0,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  contradiction_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_context_active ON context_entries(active, updated_at_epoch);

CREATE TABLE IF NOT EXISTS context_log (
  id TEXT PRIMARY KEY,
  epoch REAL NOT NULL,
  iso TEXT NOT NULL,
  action TEXT NOT NULL,          -- create | correct | manual_edit | delete | restore
                               -- | merge | outdated | rejected | verify
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


def decay_weight(confidence: float, last_verified_epoch: float,
                 now_epoch: float | None = None) -> float:
    """读取权重 = 可信度 × 半衰（闲置每 30 天减半）。确定性，可测。

    上周偏好 vs 三个月前偏好必须有权重差，否则旧记忆永久平权。
    """
    import time
    now = time.time() if now_epoch is None else now_epoch
    idle_days = max(0.0, (now - (last_verified_epoch or 0.0)) / 86400)
    return float(confidence) * (0.5 ** (idle_days / DECAY_HALF_LIFE_DAYS))


def trust_level(confidence: float, source_grade: str, idle_days: float,
                contradictions: int) -> str:
    """高/中/低三档（只用于排序；展示后议，UI 不动）。"""
    if contradictions > 0 or float(confidence) < 0.5:
        return "low"
    if (float(confidence) >= 0.8 and idle_days <= DECAY_HALF_LIFE_DAYS
            and source_grade in ("user_direct", "user_confirmed")):
        return "high"
    return "medium"


_TRUST_RANK = {"high": 0, "medium": 1, "low": 2}


def sort_for_prompt(entries: list, now_epoch: float | None = None
                    ) -> list:
    """prompt 组装排序：（信任档，衰减权重 desc）。文本格式不动，
    行为变化只来自顺序。"""
    import time
    now = time.time() if now_epoch is None else now
    scored = []
    for e in entries:
        idle = max(0.0, (now - (e.last_verified or 0.0)) / 86400)
        w = decay_weight(e.confidence, e.last_verified, now)
        scored.append((_TRUST_RANK[trust_level(
            e.confidence, e.source_grade, idle,
            e.contradiction_count)], -w, e.updated_at, e))
    scored.sort(key=lambda t: (t[0], t[1], str(t[2])))
    return [t[3] for t in scored]


class ContextEntry:
    """轻量条目（不用 pydantic，保持存储层零额外依赖）。"""

    __slots__ = (
        "id", "kind", "content", "source", "created_at", "updated_at",
        "active", "used_count", "memory_status", "status_reason",
        "source_grade", "confidence", "last_verified",
        "evidence_count", "contradiction_count",
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
        memory_status: str = "active",
        status_reason: str = "",
        source_grade: str = "inferred",
        confidence: float = 0.5,
        last_verified: float = 0.0,
        evidence_count: int = 0,
        contradiction_count: int = 0,
    ):
        self.id = id
        self.kind = kind
        self.content = content
        self.source = source
        self.created_at = created_at
        self.updated_at = updated_at
        self.active = active
        self.used_count = used_count
        self.memory_status = memory_status
        self.status_reason = status_reason
        self.source_grade = source_grade
        self.confidence = confidence
        self.last_verified = last_verified
        self.evidence_count = evidence_count
        self.contradiction_count = contradiction_count

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
            "memory_status": self.memory_status,
            "status_reason": self.status_reason,
            "source_grade": self.source_grade,
            "confidence": round(self.confidence, 2),
            "last_verified": self.last_verified,
            "evidence_count": self.evidence_count,
            "contradiction_count": self.contradiction_count,
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
            # V0.5.2 migration：老库补信任列；last_verified 回填 updated_at
            #（旧条目按"最后更新"起算闲置，诚实 decay）；
            # source manual/correction 的行回填 user_confirmed/0.95（用户亲手写的）。
            cols = {r[1] for r in con.execute(
                "PRAGMA table_info(context_entries)").fetchall()}
            with con:
                for col, ddl in (
                    ("memory_status",
                     "ALTER TABLE context_entries ADD COLUMN memory_status"
                     " TEXT NOT NULL DEFAULT 'active'"),
                    ("status_reason",
                     "ALTER TABLE context_entries ADD COLUMN status_reason"
                     " TEXT NOT NULL DEFAULT ''"),
                    ("source_grade",
                     "ALTER TABLE context_entries ADD COLUMN source_grade"
                     " TEXT NOT NULL DEFAULT 'inferred'"),
                    ("confidence",
                     "ALTER TABLE context_entries ADD COLUMN confidence"
                     " REAL NOT NULL DEFAULT 0.5"),
                    ("last_verified",
                     "ALTER TABLE context_entries ADD COLUMN last_verified"
                     " REAL NOT NULL DEFAULT 0.0"),
                    ("evidence_count",
                     "ALTER TABLE context_entries ADD COLUMN evidence_count"
                     " INTEGER NOT NULL DEFAULT 0"),
                    ("contradiction_count",
                     "ALTER TABLE context_entries ADD COLUMN"
                     " contradiction_count INTEGER NOT NULL DEFAULT 0"),
                ):
                    if col not in cols:
                        con.execute(ddl)
                con.execute(
                    "UPDATE context_entries SET last_verified ="
                    " updated_at_epoch WHERE last_verified = 0.0")
                con.execute(
                    "UPDATE context_entries SET source_grade='user_confirmed',"
                    " confidence=0.95 WHERE source_grade='inferred'"
                    " AND source IN ('manual', 'correction')")
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
        source_grade: str = "inferred",
        confidence: float = 0.5,
    ) -> tuple[ContextEntry | None, str | None]:
        """新增一条理解。返回 (entry, None) 或 (None, 跳过原因)。

        V0.5.2：来源等级与可信度由调用方（防火墙分类）给出，默认 inferred/0.5
        （宁可低估，不许虚标）。
        """
        if kind not in KINDS:
            return None, f"未知类型 {kind}"
        content = (content or "").strip()[:max_chars]
        if not content:
            return None, "内容为空"
        if source_grade not in SOURCE_GRADES:
            return None, f"未知来源等级 {source_grade}"

        now = _now()
        entry = ContextEntry(
            id=uuid.uuid4().hex, kind=kind, content=content, source=source,
            created_at=now, updated_at=now,
            source_grade=source_grade, confidence=confidence,
            last_verified=now.timestamp(),
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
                    " updated_at_iso, updated_at_epoch, active, used_count,"
                    " memory_status, status_reason, source_grade, confidence,"
                    " last_verified, evidence_count, contradiction_count) "
                    "VALUES (?,?,?,?,?,?,?,?,1,0,'active','',?,?,?,0,0)",
                    (entry.id, kind, content, source, now.isoformat(),
                     now.timestamp(), now.isoformat(), now.timestamp(),
                     source_grade, confidence, now.timestamp()),
                )
                self._log(
                    con, "create", entry.id, kind,
                    content_before=None, content_after=content, detail=detail,
                )
        log.info("理解新增 [%s] %s", KIND_LABELS.get(kind, kind), content[:60])
        return entry, None

    # ---------- 相似合并（P1：长期运行不快速填满）----------
    @staticmethod
    def _bigrams(text: str) -> set[str]:
        text = text or ""
        return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) >= 2 else set()

    @staticmethod
    def _numeric_seq(text: str) -> list[str]:
        """数字序列（含中文数字）：1号 vs 2号、三天 vs 五天必须能区分。"""
        import re
        return re.findall(r"[0-9一二三四五六七八九十百千万]+", text or "")

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """二连字 Jaccard：确定、可测、无模型（换说法能并到一起）。

        数字守卫：数字序列不同直接判 0（"喜欢喝1号/2号"、"提前三天/五天"
        是不同事实，不是复述，绝不能合并）。
        """
        na, nb = (ContextStore._numeric_seq(a),
                  ContextStore._numeric_seq(b))
        if na != nb and (na or nb):
            return 0.0
        sa, sb = ContextStore._bigrams(a), ContextStore._bigrams(b)
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def count_recent_by_kind(self, kind: str, days: int = 7) -> int:
        """近 N 天新增的同 kind 活跃条目数（配额用）。"""
        since = _now().timestamp() - days * 86400
        with closing(self._connect()) as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM context_entries WHERE active=1"
                " AND kind=? AND created_at_epoch >= ?",
                (kind, since),
            ).fetchone()[0])

    def add_or_merge(
        self,
        kind: str,
        content: str,
        source: str = "chat",
        *,
        detail: dict | None = None,
        max_chars: int = 200,
        merge_only: bool = False,
        source_grade: str = "inferred",
        confidence: float = 0.5,
    ) -> tuple[ContextEntry | None, str | None]:
        """新增；已有相似条目则合并（used+1 + 变更日志），不新建。

        返回 (entry, None) / (None, 跳过原因）。合并返回 (None, "merged:<id>")：
        调用方按"跳过"处理（不占新增配额、不进 created），但理解被强化了一次。
        merge_only=True 时绝不新建（配额打满后的只合并模式）。
        grade/confidence 只用于新建分支（合并沿用旧条目等级）。
        """
        if kind not in KINDS:
            return None, f"未知类型 {kind}"
        content = (content or "").strip()[:max_chars]
        if not content:
            return None, "内容为空"
        if source_grade not in SOURCE_GRADES:
            return None, f"未知来源等级 {source_grade}"
        now = _now()
        with self._lock, closing(self._connect()) as con:
            dup = con.execute(
                "SELECT id FROM context_entries WHERE active=1 AND content = ?",
                (content,),
            ).fetchone()
            if dup:
                return None, "已存在相同理解"
            rows = con.execute(
                "SELECT id, content FROM context_entries"
                " WHERE active=1 AND kind=?",
                (kind,),
            ).fetchall()
            best_id, best_sim = None, 0.0
            for r in rows:
                sim = self._similarity(content, r["content"] or "")
                if sim > best_sim:
                    best_id, best_sim = r["id"], sim
            if best_id is not None and best_sim >= SIM_MERGE_THRESHOLD:
                with con:
                    con.execute(
                        "UPDATE context_entries SET used_count = used_count + 1,"
                        " last_verified=?, evidence_count = evidence_count + 1,"
                        " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                        (now.timestamp(), now.isoformat(), now.timestamp(),
                         best_id),
                    )
                    self._log(
                        con, "merge", best_id, kind,
                        content_before=None, content_after=None,
                        detail={"merged_from": content,
                                "similarity": round(best_sim, 2),
                                **(detail or {})},
                    )
                log.info("理解合并 [%s] 相似度 %.2f → %s",
                         KIND_LABELS.get(kind, kind), best_sim, best_id[:8])
                return None, f"merged:{best_id}"
            if merge_only:
                return None, "配额已满且无相似条目可合并"
            with con:
                entry = ContextEntry(
                    id=uuid.uuid4().hex, kind=kind, content=content,
                    source=source, created_at=now, updated_at=now,
                    source_grade=source_grade, confidence=confidence,
                    last_verified=now.timestamp(),
                )
                con.execute(
                    "INSERT INTO context_entries "
                    "(id, kind, content, source, created_at_iso, created_at_epoch, "
                    " updated_at_iso, updated_at_epoch, active, used_count,"
                    " memory_status, status_reason, source_grade, confidence,"
                    " last_verified, evidence_count, contradiction_count) "
                    "VALUES (?,?,?,?,?,?,?,?,1,0,'active','',?,?,?,0,0)",
                    (entry.id, kind, content, source, now.isoformat(),
                     now.timestamp(), now.isoformat(), now.timestamp(),
                     source_grade, confidence, now.timestamp()),
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
        # V0.5.2（D017 ②）：纠正命中 → 相似复本同步过时，打地鼠到此为止
        try:
            self.propagate_outdated(entry_id, row["kind"], row["content"])
        except Exception as exc:
            log.warning("纠正复本联动失败（跳过）：%s", exc)
        return True

    def propagate_outdated(self, source_id: str, kind: str,
                           ref_content: str) -> list[str]:
        """纠正/否认命中 → 同 kind 相似复本一起标 outdated（propagated）。

        历史保留可审计；prompt 只读 active（见 list_entries）。
        返回受影响的复本 id 列表。
        """
        if not kind or not ref_content:
            return []
        now = _now()
        affected: list[str] = []
        with self._lock, closing(self._connect()) as con:
            rows = con.execute(
                "SELECT id, content FROM context_entries"
                " WHERE active=1 AND memory_status='active'"
                " AND kind=? AND id!=?",
                (kind, source_id),
            ).fetchall()
            for r in rows:
                if self._similarity(ref_content,
                                    r["content"] or "") < SIM_MERGE_THRESHOLD:
                    continue
                with con:
                    con.execute(
                        "UPDATE context_entries SET memory_status='outdated',"
                        " status_reason=?, confidence=?,"
                        " contradiction_count = contradiction_count + 1,"
                        " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                        (f"propagated:{source_id}", CORRECTION_SET,
                         now.isoformat(), now.timestamp(), r["id"]),
                    )
                    self._log(con, "outdated", r["id"], kind,
                              content_before=None, content_after=None,
                              detail={"reason": "propagated",
                                      "from": source_id})
                affected.append(r["id"])
        if affected:
            log.info("纠正复本联动：%s → %d 条相似复本标 outdated",
                     source_id[:8], len(affected))
        return affected

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
        keys = set(row.keys())
        return ContextEntry(
            id=row["id"], kind=row["kind"], content=row["content"],
            source=row["source"],
            created_at=datetime.fromisoformat(row["created_at_iso"]),
            updated_at=datetime.fromisoformat(row["updated_at_iso"]),
            active=bool(row["active"]), used_count=row["used_count"],
            memory_status=(row["memory_status"]
                           if "memory_status" in keys else "active"),
            status_reason=(row["status_reason"]
                           if "status_reason" in keys else ""),
            source_grade=(row["source_grade"]
                          if "source_grade" in keys else "inferred"),
            confidence=(row["confidence"]
                        if "confidence" in keys else 0.5),
            last_verified=(row["last_verified"]
                           if "last_verified" in keys else 0.0),
            evidence_count=(row["evidence_count"]
                            if "evidence_count" in keys else 0),
            contradiction_count=(row["contradiction_count"]
                                 if "contradiction_count" in keys else 0),
        )

    def list_entries(
        self, kind: str | None = None, active_only: bool = True, limit: int = 200
    ) -> list[ContextEntry]:
        where, params = [], []
        if active_only:
            # V0.5.2：prompt 只读可用条目（outdated/rejected 不再参与组装）
            where.append("active=1 AND memory_status='active'")
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

    def set_memory_status(self, entry_id: str, status: str,
                            reason: str = "") -> bool:
        """标记 outdated/rejected（纠正不断根的核心动作）。

        不删除：历史保留可审计；prompt 只读 active（见 list_entries）。
        返回 False 表示条目不存在/已删除。
        """
        if status not in MEMORY_STATUSES or status == "active":
            return False
        reason = (reason or "")[:200]
        now = _now()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT active, kind, content FROM context_entries WHERE id=?",
                (entry_id,),
            ).fetchone()
            if not row or not row["active"]:
                return False
            with con:
                con.execute(
                    "UPDATE context_entries SET memory_status=?,"
                    " status_reason=?, confidence=?,"
                    " contradiction_count = contradiction_count + 1,"
                    " updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (status, reason, CORRECTION_SET, now.isoformat(),
                     now.timestamp(), entry_id),
                )
                self._log(con, status, entry_id, row["kind"],
                          content_before=None, content_after=None,
                          detail={"reason": reason})
        log.info("理解标记 %s %s：%s", status, entry_id[:8], reason[:40])
        # V0.5.2（D017 ②）：否认命中 → 相似复本同步 outdated（锁外，防死锁）
        try:
            self.propagate_outdated(entry_id, row["kind"], row["content"])
        except Exception as exc:
            log.warning("否认复本联动失败（跳过）：%s", exc)
        return True

    def touch_verified(self, entry_ids: Iterable[str]) -> None:
        """被引用/确认时刷新 last_verified + evidence_count（新鲜度依据）。"""
        ids = list(entry_ids)
        if not ids:
            return
        now = _now().timestamp()
        with self._lock, closing(self._connect()) as con:
            with con:
                con.executemany(
                    "UPDATE context_entries SET last_verified=?,"
                    " evidence_count = evidence_count + 1 WHERE id=?",
                    [(now, i) for i in ids],
                )

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


    def count_since(self, since_epoch: float) -> int:
        """指定时刻之后新增的活跃条目数（周报统计用）。"""
        with closing(self._connect()) as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM context_entries"
                " WHERE active=1 AND created_at_epoch >= ?",
                (since_epoch,),
            ).fetchone()[0])
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
