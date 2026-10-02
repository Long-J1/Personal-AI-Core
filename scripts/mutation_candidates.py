"""待验证候选卡片的 mutation 基线。

要守住的是三件事，每一件破了产品就变质：

1. **不泄漏** —— 内部字段混进产品面（最严重：产品变成数据库查询工具）
2. **不编造** —— 没证据却显示依据 / 硬塞"已分析你的问题"这种假步骤
3. **不越权** —— 未确认的候选显示成"已确认"（把没把握的事说成有把握）

用法：
    python scripts/mutation_candidates.py
退出码 0 = 全部变异都被打中。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CV = os.path.join(REPO, "core", "candidate_view.py")
UV = os.path.join(REPO, "core", "understanding_view.py")
UI = os.path.join(REPO, "interfaces", "static", "index.html")

TESTS = ["tests/test_candidate_view.py"]

MUTATIONS = [
    # ---------- 泄漏 ----------
    ("A", "泄漏 scope（把内部作用域端给用户）",
     CV,
     '        "topic": topic,',
     '        "topic": scope + "|" + topic,'),

    ("B", "泄漏置信度",
     CV,
     '        "about": about,',
     '        "about": about,\n        "confidence": 0.72,'),

    ("C", "泄漏证据来源类型",
     CV,
     '            "when": human_date(e.get("created_at_iso"), now) or "",\n'
     '            "text": text[:120],',
     '            "when": human_date(e.get("created_at_iso"), now) or "",\n'
     '            "text": text[:120],\n'
     '            "source_type": e.get("source_type"),'),

    # ---------- 编造 ----------
    ("D", "没有证据也显示依据",
     CV,
     "        if not text:\n"
     "            continue                      # 沉默的证据不进产品面",
     "        if not text:\n"
     "            text = '（用户提到过相关的事）'"),

    ("E", "内部候选也端出去（quota_overflow / background 露给用户）",
     CV,
     "    topic = topic_name(scope)\n"
     "    if not topic:",
     "    topic = topic_name(scope) or '其他'\n"
     "    if False:"),

    ("F", "处理摘要硬塞假步骤",
     UI,
     "  const steps = [];",
     "  const steps = ['已分析你的问题'];"),

    # ---------- 越权 ----------
    ("G", "未确认的候选说成已确认",
     CV,
     '    "tentative": "pending",',
     '    "tentative": "settled",'),

    ("H", "已认可（未毕业）被当成已确认",
     CV,
     '    "accepted": "watching",',
     '    "accepted": "settled",'),

    ("I", "已处理的候选仍留在主列表",
     CV,
     '        "pending": [c for c in cards if c["bucket"] == "pending"],',
     '        "pending": list(cards),'),

    ("J", "把「对」说成已经确认事实",
     CV,
     '_WHY_TAIL = "因此我把它暂时看作一个「可能发生的变化」，等你确认或改口。"',
     '_WHY_TAIL = "我已确认这是事实，并设为你的长期记忆。"'),

    # ---------- 时间（真踩过的坑）----------
    ("K", "时间字段只认 created_at_iso（真对象给的是 created_at → 时间栏全空）",
     CV,
     '        for key in ("created_at_iso", "created_at", "created_at_epoch"):',
     '        for key in ("created_at_iso",):'),

    ("L", "已处理的候选仍写「暂时看作可能的变化」（语义说反）",
     CV,
     '        "why_tail": _WHY_TAIL if bucket in ("pending", "watching") else "",',
     '        "why_tail": _WHY_TAIL,'),
]


def run_tests() -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "--tb=no",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    return p.returncode, (p.stdout or "")


def main() -> int:
    backup = tempfile.mkdtemp()
    results = []
    print("=" * 70)
    print("待验证卡片 mutation：破坏一处，测试必须变红")
    print("=" * 70)

    code, out = run_tests()
    print(f"  BASE (no mutation)      EXIT={code} "
          f"{'all green [OK]' if code == 0 else 'already red [X] -- fix tests first'}")
    if code != 0:
        print(out[-2000:])
        return 2

    for tag, desc, path, old, new in MUTATIONS:
        shutil.copy2(path, backup)
        try:
            src = io.open(path, encoding="utf-8").read()
            if old not in src:
                print(f"  {tag:<3} cannot locate site (source changed), skip")
                results.append((tag, desc, "SKIP"))
                continue
            io.open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
            code, _ = run_tests()
            killed = code != 0
            print(f"  {tag:<3} {desc}")
            print(f"       -> EXIT={code}  "
                  f"{'killed [OK]' if killed else 'SURVIVED [X] (tests are a placebo)'}")
            results.append((tag, desc, "KILLED" if killed else "SURVIVED"))
        finally:
            shutil.copy2(os.path.join(backup, os.path.basename(path)), path)

    code, _ = run_tests()
    print()
    print(f"  RESTORED                EXIT={code} "
          f"{'green again [OK]' if code == 0 else 'still failing [X]'}")
    print()
    print("=" * 70)
    print("Results")
    print("=" * 70)
    for tag, desc, v in results:
        print(f"  {tag:<3} {v:<9} {desc}")
    surv = [r for r in results if r[2] == "SURVIVED"]
    print()
    if code != 0:
        print("  RESTORE FAILED")
        return 2
    if surv:
        print(f"  {len(surv)} SURVIVED -- tests do not guard what they claim")
        return 1
    print(f"  all {len(results)} mutations killed [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())