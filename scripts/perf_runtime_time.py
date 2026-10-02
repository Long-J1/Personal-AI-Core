"""Runtime Time Context 的开销测量。

需求 10：不影响 streaming / TTFT。

要回答两件事：

1. **时间块自己有多贵** —— 一次系统时钟读取 + 一段字符串格式化。
2. **它对 TTFT 的贡献** —— 关键在于它发生在 `_assemble` 里、
   **模型调用之前**，所以它的耗时是全额压在首字时间上的，
   不能拿"占比很小"糊弄过去。

隔离：临时库 + 假时钟 + 假模型，测的是我们自己的开销，不是网络延迟。
"""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone as dtz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from core.runtime_time import (                       # noqa: E402
    TimeProvider, build_time_block, get_time_context)

SH = dtz(timedelta(hours=8))
FAKE = datetime(2026, 3, 12, 15, 30, tzinfo=SH)


def p99(xs, q=0.99):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def bench_build(n=5000):
    print("=" * 66)
    print("1) 时间块生成（读时钟 + 格式化）")
    print("=" * 66)
    # 真时钟路径（生产用）
    for _ in range(200):
        build_time_block()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        build_time_block()
        ts.append((time.perf_counter() - t0) * 1e6)      # 微秒
    print(f"  build_time_block()   中位 {statistics.median(ts):8.2f} µs"
          f"   p99 {p99(ts):8.2f} µs   max {max(ts):8.2f} µs")
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        get_time_context()
        ts.append((time.perf_counter() - t0) * 1e6)
    print(f"  get_time_context()  中位 {statistics.median(ts):8.2f} µs"
          f"   p99 {p99(ts):8.2f} µs   max {max(ts):8.2f} µs")
    n1 = len(build_time_block().encode("utf-8"))
    print(f"  时间块长度: {n1} 字节（每轮固定注入这么多 token）")


def bench_conversation(rounds=60):
    print()
    print("=" * 66)
    print("2) 真实链路 conversation.chat（含时间块 vs 不含）")
    print("=" * 66)

    class Null:
        def __init__(self):
            self.sys = []

        def chat(self, messages, images=None, timeout=None, **kw):
            s = str((messages[0] or {}).get("content", ""))
            self.sys.append(s)
            if "记忆沉淀模块" in s:
                return '{"new": [], "corrections": [], "event": null}'
            return "答案是 42。"

    def run(with_time: bool):
        db = tempfile.mkdtemp() + "/perf.db"
        from memory.chat_store import ChatStore
        from memory.context_store import ContextStore
        from memory.hypothesis_store import HypothesisStore
        from memory.store import MemoryStore
        from core.conversation import ConversationService
        from core.runtime_time import TimeProvider

        model = Null()
        kw = {}
        if with_time:
            kw["time_provider"] = TimeProvider(clock=lambda: FAKE)
        conv = ConversationService(
            MemoryStore(db), ContextStore(db), model,
            chat_store=ChatStore(db),
            hypothesis_store=HypothesisStore(db), **kw)
        for _ in range(5):
            conv.chat("现在几点")
        ts = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            conv.chat("现在几点")
            ts.append((time.perf_counter() - t0) * 1000)
        ts.sort()
        return ts

    off = run(False)
    on = run(True)
    print(f"  不注入时间块   中位 {statistics.median(off):7.2f} ms"
          f"   p95 {p99(off, .95):7.2f} ms")
    print(f"  注入时间块     中位 {statistics.median(on):7.2f} ms"
          f"   p95 {p99(on, .95):7.2f} ms")
    d = statistics.median(on) - statistics.median(off)
    print(f"  差值（时间块的全部成本）: {d:+.3f} ms")
    print()
    print("  参照：本机真实 TTFT 实测 1.5–4.3 s（记忆/沉淀类操作本身就要 175–280 ms）")
    print(f"  → 时间块占 chat() 组装时间的 {d / statistics.median(on) * 100:.3f}%")


def bench_stream(rounds=25):
    print()
    print("=" * 66)
    print("3) 流式首字（TTFT）")
    print("=" * 66)
    import asyncio

    class StreamModel:
        def __init__(self):
            self.first_yield = None

        def chat(self, messages, images=None, timeout=None, **kw):
            s = str((messages[0] or {}).get("content", ""))
            if "记忆沉淀模块" in s:
                return '{"new": [], "corrections": [], "event": null}'
            return "答案"

        async def chat_stream_async(self, messages, timeout=None):
            for ch in "答案是 42。":
                yield ch
                await asyncio.sleep(0)

    def run(with_time: bool):
        db = tempfile.mkdtemp() + "/perf.db"
        from memory.chat_store import ChatStore
        from memory.context_store import ContextStore
        from memory.hypothesis_store import HypothesisStore
        from memory.store import MemoryStore
        from core.conversation import ConversationService
        from core.runtime_time import TimeProvider

        kw = ({"time_provider": TimeProvider(clock=lambda: FAKE)}
              if with_time else {})
        conv = ConversationService(
            MemoryStore(db), ContextStore(db), StreamModel(),
            chat_store=ChatStore(db),
            hypothesis_store=HypothesisStore(db), **kw)

        async def first_token():
            t0 = time.perf_counter()
            async for chunk in conv.stream_chat("现在几点"):
                return (time.perf_counter() - t0) * 1000

        for _ in range(3):
            asyncio.run(first_token())
        return [asyncio.run(first_token()) for _ in range(rounds)]

    off = run(False)
    on = run(True)
    print(f"  不注入   首字 中位 {statistics.median(off):7.3f} ms")
    print(f"  注入     首字 中位 {statistics.median(on):7.3f} ms")
    print(f"  差值: {(statistics.median(on) - statistics.median(off)):+.3f} ms")
    print("  （与第 2 节一致：判断在 _assemble 完成，流式与同步共用同一位置）")


if __name__ == "__main__":
    bench_build()
    bench_conversation()
    bench_stream()