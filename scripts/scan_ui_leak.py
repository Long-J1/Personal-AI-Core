"""全仓库安全扫描：用户可见界面不许出现内部字段。

任务书点名禁止的：
  source_type / source_id / hypothesis_id / scope= / confidence=
  raw prompt / system prompt / developer prompt / internal tool payload

扫三处：
  1. 前端源码里"模板渲染内部字段"的写法（真正的泄漏点）
  2. 后端投影函数 forbidden 清单的覆盖度
  3. 真跑一遍端点，扫**实际返回的 JSON**（源码干净但运行时有字段也拦不住）
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, "interfaces", "static", "index.html")

# 任务书点名禁止的
BANNED_UI = [
    "source_type", "source_id", "hypothesis_id", "scope=",
    "confidence=", "raw prompt", "system prompt", "developer prompt",
    "chain-of-thought",
]


def scan_ui() -> list[str]:
    """前端：模板里渲染内部字段。"""
    src = io.open(UI, encoding="utf-8").read()
    hits = []
    for w in BANNED_UI:
        for m in re.finditer(re.escape(w), src, re.I):
            line = src[:m.start()].count("\n") + 1
            text = src.split("\n")[line - 1].strip()
            # 注释里提到"不许泄漏 XX"不算泄漏
            if text.startswith(("/*", "*", "//", "<!--")) or "禁止" in text \
                    or "不许" in text or "FORBIDDEN" in text:
                continue
            hits.append(f"L{line} {w}: {text[:72]}")
    return hits


def scan_api() -> list[str]:
    """真跑端点，扫实际 JSON。"""
    from fastapi.testclient import TestClient
    from interfaces.webapp import create_app
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore

    db = os.path.join(tempfile.mkdtemp(), "s.db")
    store = MemoryStore(db)
    ctx = ContextStore(db)
    hyp = HypothesisStore(db)

    # 造最坏情况：各种 scope、各种状态、真实原话
    e1, _ = ctx.add_entry(kind="interaction", content="别直接给我答案，先给提示",
                          source="user_direct", source_grade="user_direct")
    h1, _ = hyp.create(statement="用户近期多次在深夜写作业", scope="activity:写作业")
    hyp.add_evidence(h1.id, "support", "context", e1.id, excerpt="别直接给我答案")
    h2, _ = hyp.create(statement="用户可能减少了查资料", scope="interaction:查资料")
    hyp.confirm(h2.id)
    hyp.create(statement="用户暂时不想被提醒", scope="quota_overflow:dynamic")

    app = create_app(store, None, ctx, None, hyp)
    client = TestClient(app)
    hits = []
    for path in ("/api/candidates", "/api/understanding", "/api/background"):
        r = client.get(path)
        if r.status_code != 200:
            hits.append(f"{path} HTTP {r.status_code}")
            continue
        blob = json.dumps(r.json(), ensure_ascii=False)
        for w in BANNED_UI:
            if w.lower() in blob.lower():
                hits.append(f"{path} 返回体含 {w}")
    return hits


def scan_forbidden_list() -> list[str]:
    """禁用清单本身要覆盖任务书点名的。"""
    from core.understanding_view import FORBIDDEN_IN_OUTPUT
    need = ["source_type", "source_id", "hypothesis_id", "scope=",
            "confidence"]
    return [f"禁用清单漏了 {w}" for w in need
            if not any(w in f for f in FORBIDDEN_IN_OUTPUT)]


def main() -> int:
    print("=" * 66)
    print("安全扫描：内部字段不许出现在用户界面")
    print("=" * 66)
    total = 0
    for name, fn in (("禁用清单覆盖度", scan_forbidden_list),
                     ("前端渲染", scan_ui),
                     ("端点实际返回", scan_api)):
        hits = fn()
        total += len(hits)
        print(f"\n  [{name}] {'通过 [OK]' if not hits else '发现问题 [X]'}")
        for h in hits[:14]:
            print("     -", h)
    print()
    if total:
        print(f"  共 {total} 处问题")
        return 1
    print("  未发现内部字段泄漏 [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())