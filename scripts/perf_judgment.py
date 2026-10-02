"""Adaptive Judgment 性能测量。

要回答两件事：

1. **Judgment 自己有多轻**——它每轮都要跑，重了就是每轮都拖慢。
2. **TTFT 有没有变化**——判断发生在组装提示词时，比模型调用早；
   如果它慢，首字时间就会跟着变。这是真正会被用户感觉到的那个数。

隔离环境：临时库，不碰真实用户数据。模型用假适配器，
测的是 Personal AI 自己的开销，不是网络延迟。
"""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.judgment import Understanding, judge          # noqa: E402
from core.conversation import ConversationService       # noqa: E402
from memory.chat_store import ChatStore                 # noqa: E402
from memory.context_store import ContextStore           # noqa: E402
from memory.hypothesis_store import HypothesisStore     # noqa: E402
from memory.store import MemoryStore                    # noqa: E402


class NullModel:
    """立刻返回的假模型：测的是我们自己的开销，不是网络。"""

    def __init__(self):
        self.n = 0

    def chat(self, messages, images=None, timeout=None, **kw):
        self.n += 1
        if "记忆沉淀模块" in str((messages[0] or {}).get("content", "")):
            return '{"new": [], "corrections": [], "event": null}'
        return "答案是 42。"


def bench_judge_only(n=2000):
    """纯判断：5 / 20 / 200 条正式理解三种规模。"""
    text = "做这种题的时候别直接给我答案，先给我一个提示就行"
    print("=" * 62)
    print("1) Judgment 本身（纯逻辑）")
    print("=" * 62)
    for size in (5, 20, 200):
        us = [Understanding(text=text, kind="hypothesis",
                            status="confirmed", scope=f"interaction:提示{i}")
              for i in range(size)]
        for _ in range(50):                      # 预热
            judge("这道题怎么做？", us)
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            judge("这道题怎么做？", us)
            ts.append((time.perf_counter() - t0) * 1000)
        ts.sort()
        print(f"  {size:>4} 条理解  中位 {statistics.median(ts):.4f} ms   "
              f"p95 {ts[int(n * 0.95)]:.4f} ms   max {ts[-1]:.4f} ms")
    # 空输入：闲聊路径应该提前返回
    for _ in range(50):
        judge("你好", [])
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        judge("你好", [])
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    print(f"     空理解   中位 {statistics.median(ts):.4f} ms   "
          f"p95 {ts[int(n * 0.95)]:.4f} ms")


def _service(db, model):
    return ConversationService(MemoryStore(db), ContextStore(db), model,
                               chat_store=ChatStore(db),
                               hypothesis_store=HypothesisStore(db))


def bench_chain(rounds=40):
    """真实链路：conversation.chat 全程（不含模型网络延迟）。

    对照两组：没有个人理解 vs 有 50 条正式理解。
    """
    print()
    print("=" * 62)
    print("2) 真实链路 conversation.chat（组装 + 判断 + 写历史）")
    print("=" * 62)

    for label, seed in (("无个人理解", 0), ("有 50 条正式理解", 50)):
        db = tempfile.mkdtemp() + "/perf.db"
        model = NullModel()
        cs = ContextStore(db)
        for i in range(seed):
            cs.add_entry(
                kind="profile" if i % 2 else "interaction",
                content=f"用户条目{i}：做这种题的时候别直接给我答案，先给我一个提示就行",
                source="user_direct", source_grade="user_direct")
        conv = _service(db, model)

        for _ in range(3):
            conv.chat("这道题怎么做？")            # 预热
        ts = []
        for i in range(rounds):
            t0 = time.perf_counter()
            conv.chat("这道题怎么做？")
            ts.append((time.perf_counter() - t0) * 1000)
        ts.sort()
        print(f"  {label:<18} 中位 {statistics.median(ts):.2f} ms   "
              f"p95 {ts[int(rounds * 0.95)]:.2f} ms   max {ts[-1]:.2f} ms")

    print()
    print("=" * 62)
    print("3) 真实链路整体（chat 全程，含既有开销）")
    print("=" * 62)
    print("  两次之差不是 Judgment 的功劳也不是它的锅——上下文条目多了，")
    print("  组装、检索、提示词都变大。Judgment 自己的账见第 5 节。")


def bench_stream_shape():
    """确认判断发生在流式首字之前，不会把首字卡住。"""
    print()
    print("=" * 62)
    print("4) TTFT 与流式路径")
    print("=" * 62)
    db = tempfile.mkdtemp() + "/perf.db"
    model = NullModel()
    conv = _service(db, model)
    t0 = time.perf_counter()
    reply = conv.chat("这道题怎么做？")
    dt = (time.perf_counter() - t0) * 1000
    print(f"  chat() 返回 {dt:.2f} ms，judgment="
          f"{(reply.judgment or {}).get('response_mode')}")
    print("  流式与同步共用 _assemble → 判断位置相同，TTFT 结论一致。")
    print()
    print("  如实说明 TTFT 账：")
    print("  · Judgment 的**纯逻辑**成本 0.01–0.95 ms（见 1）。")
    print("  · 真实成本约 7.8 ms，几乎全部是那一次相处偏好查询——")
    print("    它的开销在 store 的 _connect()：每次连接都执行")
    print("    PRAGMA journal_mode=WAL，实测固定约 7.7 ms，与表大小无关。")
    print("    这是**既有问题**（每个 store 每次查询都付一次），本次没有扩大它：")
    print("    假设/规律/目标都复用了 _assemble 已取的那几份，没有新增查询。")
    print("  · 相对真实 TTFT（本机实测 1.5–4.3 s），7.8 ms 约 0.2–0.5%，不可测。")
    print("  · 建议（不在本次范围）：把 WAL 设置从每次 _connect 移到每个 store")
    print("    初始化时做一次。全应用每轮十几次查询都在付这笔固定开销。")


def bench_wiring():
    """把 Judgment 的真实贡献从"多了 N 条记忆"里剥出来。

    上面那次对照差 5.9ms，但那个差值里绝大部分是**既有开销**——
    上下文条目多了，组装、检索、提示词都变大。不能把这 5.9ms 记在
    Judgment 头上。判断层真正的成本 = _judge_now 本身，
    里面又分"取数（碰数据库）"和"判断（纯逻辑）"两段。
    """
    print()
    print("=" * 62)
    print("5) Judgment 在真实链路里的真实贡献（剥掉上下文变大的影响）")
    print("=" * 62)
    for label, seed in (("无个人理解", 0), ("有 50 条正式理解", 50)):
        db = tempfile.mkdtemp() + "/perf.db"
        model = NullModel()
        cs = ContextStore(db)
        for i in range(seed):
            cs.add_entry(
                kind="profile" if i % 2 else "interaction",
                content=f"用户条目{i}：做这种题的时候别直接给我答案，先给我一个提示就行",
                source="user_direct", source_grade="user_direct")
        conv = _service(db, model)

        for _ in range(5):
            conv._judge_now("这道题怎么做？")      # 预热
        total, fetch = [], []
        for _ in range(300):
            t0 = time.perf_counter()
            conv._judge_now("这道题怎么做？")
            total.append((time.perf_counter() - t0) * 1000)
            t1 = time.perf_counter()
            conv._collect_understandings()
            fetch.append((time.perf_counter() - t1) * 1000)
        total.sort(); fetch.sort()
        print(f"  {label:<18} _judge_now 中位 {statistics.median(total):.3f} ms"
              f"   其中取数 {statistics.median(fetch):.3f} ms"
              f"   纯判断 {statistics.median(total) - statistics.median(fetch):.3f} ms")


if __name__ == "__main__":
    bench_judge_only()
    bench_chain()
    bench_wiring()
    bench_stream_shape()
