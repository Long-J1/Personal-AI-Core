"""V0.5.10 Reality / Soak Validation harness.

这不是单元测试，是把 V0.5.9 放进一个隔离环境里连续跑很久，
看它到底会不会：观察 → 思考 → 提候选 → 等人类 → 学习 → 毕业。

隔离（第一原则）：临时 PAI_DATA_DIR / LOG_DIR / 独立 DB。
不读、不写真实用户数据。

设计取舍说明：
- **进程内 TestClient + 注入脚本模型**。理由：要真实跑 idle worker
  （需要真事件循环 + startup/shutdown hook），又要注入可控的理解输出
  （本机没配模型）。TestClient 走的是 starlette portal，真 hook 会触发。
- **"重启" = 丢掉 app/worker 对象，用同一个 DB 重建**。进程态归零、
  数据留存，这与真实重启等价，且可重复。
- 事件时间戳**回填**：后台推理按 14 天 / 7 天双窗口看趋势，
  不回填就造不出"过去晚上、最近早上"的对照。

用法：
    python scripts/soak_v0510.py --minutes 95
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# ---------- 隔离：必须在 import 项目模块之前设好 ----------
_OUT = None          # 由 main() 设置
DATA_DIR = None
LOG_DIR = None


def setup_isolation(root: str) -> None:
    """把所有数据路径指到临时目录。绝不触碰真实 data/。"""
    global _OUT, DATA_DIR, LOG_DIR
    _OUT = root
    DATA_DIR = os.path.join(root, "data")
    LOG_DIR = os.path.join(root, "logs")
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    os.environ["PAI_DATA_DIR"] = DATA_DIR
    os.environ["PAI_LOG_DIR"] = LOG_DIR
    os.environ["PAI_IDLE_REASONING"] = "1"      # 只在本进程内开
    os.environ.setdefault("PAI_LOG_LEVEL", "WARNING")


def db_path() -> str:
    return os.path.join(DATA_DIR, "personal_ai.db")


# ---------- 记录 ----------
_SAMPLES: list[dict] = []
_EVENTS: list[dict] = []
_T0 = time.time()
_LOG_PATH = None


def log(msg: str) -> None:
    line = f"[{time.time() - _T0:8.1f}s] {msg}"
    print(line, flush=True)
    if _LOG_PATH:
        with io.open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def mark(kind: str, **kw) -> None:
    rec = {"t": round(time.time() - _T0, 1), "kind": kind, **kw}
    _EVENTS.append(rec)
    log(f"  >> {kind} " + json.dumps(kw, ensure_ascii=False)[:220])


# ---------- 脚本模型：让沉淀产出可控的理解 ----------
class ScriptedModel:
    """按脚本返回 deposit JSON / 对话回答。

    用它是为了把**确定性**还给 soak：我们要测的是链路，不是模型措辞。
    """

    def __init__(self):
        self.calls = 0
        self.deposit_queue: list[str] = []
        self.chat_delay = 0.0

    def next_deposit(self) -> str:
        if self.deposit_queue:
            return self.deposit_queue.pop(0)
        return json.dumps({"new": [], "corrections": [], "event": None},
                          ensure_ascii=False)

    def chat(self, messages, images=None, timeout=None, **kw):
        self.calls += 1
        system = str((messages[0] or {}).get("content", "")) if messages else ""
        if "记忆沉淀模块" in system:
            out = self.next_deposit()
        else:
            out = "好的，我记下了。"
        return out

    def chat_stream(self, messages, **kw):
        text = self.chat(messages)
        yield text

    def ping(self, *a, **kw):
        return 0.001


# ---------- 数据库采样 ----------
def snapshot(hyp_store, label: str = "") -> dict:
    con = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
    try:
        cur = con.cursor()

        def q(sql, args=()):
            try:
                return cur.execute(sql, args).fetchall()
            except sqlite3.Error:
                return []

        hyps = q("SELECT scope, status, confidence, support_count,"
                 " contradict_count FROM hypotheses")
        by_status: dict[str, int] = {}
        for r in hyps:
            by_status[r[1]] = by_status.get(r[1], 0) + 1
        bg = q("SELECT scope, status, confidence, support_count,"
               " FROM hypotheses WHERE scope LIKE 'background:%'")
        ev = q("SELECT polarity, source_type, COUNT(*) FROM hypothesis_evidence"
               " GROUP BY polarity, source_type")
        prompt_ids = {h.id for h in hyp_store.list_for_prompt(limit=50)}
        bg_in_prompt = [r[0] for r in bg if any(
            h.scope == r[0] and h.id in prompt_ids
            for h in hyp_store.list_hypotheses(live_only=False, limit=200))]
        snap = {
            "label": label,
            "t": round(time.time() - _T0, 1),
            "hypotheses": len(hyps),
            "by_status": by_status,
            "background_total": len(bg),
            "background_by_status": {
                s: sum(1 for r in bg if r[1] == s)
                for s in ("pending", "accepted", "rejected", "expired")
            },
            "graduated": sum(1 for r in hyps
                             if r[0].startswith("inferred:") and r[1] == "confirmed"),
            "evidence": {f"{r[0]}/{r[1]}": r[2] for r in ev},
            "events": len(q("SELECT id FROM events")),
            "context": len(q("SELECT id FROM context_entries")),
            "chat_messages": len(q("SELECT id FROM chat_messages")),
            "patterns": len(q("SELECT id FROM patterns")),
            "bg_in_prompt": bg_in_prompt,
            "db_bytes": os.path.getsize(db_path()),
        }
        _SAMPLES.append(snap)
        return snap
    finally:
        con.close()


# ---------- 注入回填事件 ----------
def inject_event(store, days_ago: float, description: str, activity: str) -> None:
    from memory.event import Event
    store.add_event(Event(
        id=uuid.uuid4().hex,
        created_at=datetime.now() - timedelta(days=days_ago),
        source="chat", scene="家里", activity=activity, objects=[],
        description=description, importance=0.5, facts=[]))


def touch_chat(chat_store, seconds_ago: float = 0.0) -> None:
    """更新会话活跃时间，让 idle worker 重新计时。"""
    sid = chat_store.get_or_create_session()
    with closing(sqlite3.connect(chat_store.db_path)) as con:
        con.execute("UPDATE conversations SET updated_epoch=? WHERE id=?",
                    (time.time() - seconds_ago, sid))
        con.commit()


# ---------- 建 app ----------
def build_app(model):
    """每次调用 = 一个全新的 app（= 一次"重启"）。"""
    import core.pipeline as pm
    from core.service_state import ServiceState
    from interfaces.webapp import create_app
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore
    from fastapi.testclient import TestClient

    pm.THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
    store = MemoryStore(db_path())
    app = create_app(store=store, model=model,
                     context_store=ContextStore(db_path()),
                     chat_store=ChatStore(db_path()),
                     hypothesis_store=HypothesisStore(db_path()),
                     service_state=ServiceState())
    client = TestClient(app)
    return client, app, store


def worker_of(app):
    return getattr(app.state, "idle_worker", None)


def say(client, model, text, deposit=None, wait=0.4):
    """走真实 conversation 通道发一轮。"""
    model.deposit_queue = [deposit] if deposit else []
    r = client.post("/api/conversation", json={"message": text})
    time.sleep(wait)
    return r


def dj(new=(), corrections=(), event=None) -> str:
    return json.dumps({"new": list(new), "corrections": list(corrections),
                       "event": event}, ensure_ascii=False)


# ================================================================ 故事
NIGHT = "晚上十点以后学习效率最高"
MORN = "早上六点起来写数学"


def _idle_tick(w) -> dict:
    """在独立 loop 里跑一轮 idle（不依赖 pytest 环境的 loop）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(w.tick())
    finally:
        loop.close()


def phase_a_b_story(client, model, store):
    """A+B：建立旧理解 + 用户发生变化。全部走真实 conversation 通道。"""
    from core.inducer import Inducer
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore

    cs, hs = ContextStore(db_path()), HypothesisStore(db_path())

    # A. 过去：晚上学习（回填旧事件，让双窗口有对照）
    for d in (18, 16, 14, 13, 12):
        inject_event(store, d, NIGHT + f" 第{d}天", "学习")
    Inducer(store, cs, hs).induce_all()
    snap = snapshot(hs, "A_过去晚上学习")
    mark("phase_A_old", hypotheses=snap["hypotheses"],
         by_status=snap["by_status"], events=snap["events"])

    # 旧理解：通过真实对话建立（两次自然表达）
    say(client, model, "我以前一般晚上十点以后学习效率最好。",
        dj(new=[{"kind": "dynamic", "content": "以前晚上十点后学习效率最高"}]))
    say(client, model, "我晚上确实比较容易进入状态。",
        dj(new=[{"kind": "dynamic", "content": "晚上容易进入学习状态"}]))
    snap = snapshot(hs, "A_旧理解已建立")
    mark("phase_A_understanding", context=snap["context"],
         hypotheses=snap["hypotheses"], by_status=snap["by_status"])

    # B. 变化：通过 conversation → 后台 deposit → Change
    for i, text in enumerate(["这几天开始早上学习了。",
                              "最近六七点起来状态反而更好。",
                              "我现在习惯早上写数学。"]):
        say(client, model, text, dj(
            new=[{"kind": "dynamic", "content": "最近开始早上学习"}],
            event={"description": text, "activity": "学习",
                   "scene": "家里", "importance": 0.5}))
        inject_event(store, 6 - i, MORN + f" 第{i + 1}天", "学习")
    snap = snapshot(hs, "B_变化后")
    mark("phase_B_change", context=snap["context"], events=snap["events"],
         by_status=snap["by_status"])
    return cs, hs


def phase_c_idle(app, rounds=4):
    """C：真正 idle——用户停止操作，等 idle worker 自己走。"""
    w = worker_of(app)
    if w is None:
        mark("idle_error", why="worker 未启动")
        return {}
    w.poll_seconds, w.idle_seconds, w.cooldown_seconds = 2.0, 3.0, 8.0
    out_all = []
    for i in range(rounds):
        out = _idle_tick(w)
        out_all.append(out)
        mark("idle_tick", round=i, **out)
        if i < rounds - 1:
            time.sleep(9)          # 越过 cooldown，验证不会重复跑
    return {"ticks": out_all}


def phase_c2_idle_discovers(app, store):
    """C′：**不发任何对话**，只注入"用户后来又这么干了"的行为，
    然后靠 idle worker 自己去发现。

    这是 idle reasoning 真正有价值的场景——用户早就走了，
    系统自己看出变化。Phase B 里候选是 deposit 顺手提的，
    不算 idle 的功劳。
    """
    w = worker_of(app)
    if w is None:
        mark("idle_error", why="worker 未启动")
        return {}
    from memory.hypothesis_store import HypothesisStore
    hs = HypothesisStore(db_path())
    time.sleep(10)          # 越过冷却，否则下一轮 idle 被挡在冷却里
    before = len([h for h in hs.list_hypotheses(live_only=False, limit=200)
                  if (h.scope or "").startswith("background:")])
    for i in range(6):
        inject_event(store, 1.0, MORN + f" 又一次 {i}", "读书")
    w.poll_seconds, w.idle_seconds, w.cooldown_seconds = 2.0, 3.0, 8.0
    out = _idle_tick(w)
    after = len([h for h in hs.list_hypotheses(live_only=False, limit=200)
                 if (h.scope or "").startswith("background:")])
    mark("idle_discovers", before=before, after=after,
         proposed=out.get("proposed"), ran=out.get("ran"))
    return out


def phase_d_verify(hs) -> dict:
    """D：候选出来了，且处处不该生效。"""
    bg = [h for h in hs.list_hypotheses(live_only=False, limit=200)
          if (h.scope or "").startswith("background:")]
    prompt_ids = {h.id for h in hs.list_for_prompt(limit=50)}
    info = {
        "candidates": len(bg),
        "by_status": {s: sum(1 for h in bg if h.status == s)
                      for s in ("pending", "accepted", "rejected", "expired")},
        "in_prompt": [h.scope for h in bg if h.id in prompt_ids],
        "promotable": [h.scope for h in bg if h.status == "confirmed"],
    }
    mark("phase_D_candidates", **info)
    return info


def phase_e_accept_via_chat(client, model, hs, cand_id) -> bool:
    """E：用户认可——**必须走正常 conversation**，不许直接调 verdict API。"""
    before = hs.get(cand_id)
    # 通过对话说一句明确的认可，脚本模型同时把 verdict 记录为用户原话
    say(client, model, "对，我最近已经改成早上学习了。",
        dj(new=[{"kind": "interaction",
                 "content": "对，我最近已经改成早上学习了。"}]))
    # 会话层不自动裁决（裁决是显式动作），这里如实记录现状
    after = hs.get(cand_id)
    mark("phase_E_chat_only", before_status=before.status,
         after_status=after.status,
         note="对话裁决应该把 pending 推到 accepted")
    return True


def main():
    global _LOG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=95.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = args.out or tempfile.mkdtemp(prefix="pai_soak_v0510_")
    setup_isolation(root)
    _LOG_PATH = os.path.join(root, "soak.log")
    log(f"=== V0.5.10 soak 开始，隔离目录 {root} ===")
    log(f"PAI_IDLE_REASONING={os.environ['PAI_IDLE_REASONING']}  "
        f"PAI_DATA_DIR={DATA_DIR}")

    from memory.chat_store import ChatStore
    from memory.store import MemoryStore

    model = ScriptedModel()
    client, app, store = build_app(model)
    cs, hs = phase_a_b_story(client, model, store)
    phase_c_idle(app, rounds=4)
    phase_c2_idle_discovers(app, store)
    phase_d_verify(hs)

    # E：用户认可——走正常 conversation
    cands = [h for h in hs.list_hypotheses(live_only=False, limit=200)
             if h.status == "pending"]
    if cands:
        phase_e_accept_via_chat(client, model, hs, cands[0].id)
    else:
        mark("phase_E_skip", why="无 pending 候选")
    snapshot(hs, "E_认可后")

    # ---------- 长时运行 ----------
    chat_store = ChatStore(db_path())
    log(f"进入长时间运行，目标 {args.minutes:.0f} 分钟")
    deadline = _T0 + args.minutes * 60
    anomalies = []
    idle_ticks = 0

    while time.time() < deadline:
        time.sleep(240)
        touch_chat(chat_store, seconds_ago=150)
        try:
            snap = snapshot(hs, "long_sample")
        except Exception as exc:
            anomalies.append(f"snapshot: {exc}")
            continue
        try:
            w = worker_of(app)
            if w is not None:
                w.poll_seconds, w.idle_seconds, w.cooldown_seconds = 2.0, 3.0, 8.0
                out = _idle_tick(w)
                idle_ticks += 1
                if out.get("proposed"):
                    mark("idle_proposed", **out)
        except Exception as exc:
            anomalies.append(f"idle: {exc}")
        snap = snapshot(hs, "post_idle")
        if snap["bg_in_prompt"]:
            anomalies.append(f"PROMPT 污染: {snap['bg_in_prompt']}")
        if time.time() < deadline - 60:
            time.sleep(180)

    log(f"长时运行结束，实际 {(time.time() - _T0) / 60:.1f} 分钟")

    # ---------- 重启三连 ----------
    from memory.hypothesis_store import HypothesisStore
    hs2 = HypothesisStore(db_path())
    before_restart = phase_d_verify(hs2)
    client.app.state.idle_worker  # 触碰一下
    client.close()
    client2, app2, store2 = build_app(model)
    hs3 = HypothesisStore(db_path())
    after_restart = phase_d_verify(hs3)
    mark("restart_check", before=before_restart["by_status"],
         after=after_restart["by_status"])
    old = worker_of(app)
    old_stopped = old is None or not old._running
    new = worker_of(app2)
    mark("worker_leak_check",
         old_running=getattr(old, "_running", None),
         old_stopped=bool(old_stopped),
         new_worker=type(new).__name__ if new else None,
         new_running=getattr(new, "_running", None))

    client2.close()
    report = {
        "isolated_root": root,
        "runtime_minutes": round((time.time() - _T0) / 60, 1),
        "idle_ticks": idle_ticks,
        "anomalies": anomalies,
        "samples": _SAMPLES,
        "events": _EVENTS,
        "restart": {"before": before_restart, "after": after_restart},
    }
    with io.open(os.path.join(root, "soak_report.json"), "w",
                 encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log(f"报告已写入 {os.path.join(root, 'soak_report.json')}")
    log(f"隔离目录保留：{root}")
    return report


if __name__ == "__main__":
    main()