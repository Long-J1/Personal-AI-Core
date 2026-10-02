"""Understanding 链的最小 mutation 基线（V0.5.6 Phase 5）。

目的不是搞一套工业级变异框架，而是回答一个具体问题：

    这些测试真的能打住核心逻辑吗？

做法：人为删掉一处保护条件 → 跑自然证据相关测试 → **必须变红**。
如果删掉保护条件测试还是绿的，说明这些测试是摆设，必须修测试。

用法：
    python scripts/mutation_natural.py
退出码 0 = 四个变异全部被测试打中。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NE = os.path.join(REPO, "core", "natural_evidence.py")
HS = os.path.join(REPO, "memory", "hypothesis_store.py")

UV = os.path.join(REPO, "core", "understanding_view.py")

TESTS = ["tests/test_natural_evidence.py", "tests/test_natural_scenarios.py",
         "tests/test_understanding_view.py", "tests/test_first_run.py"]

# 变异体：(代号, 描述, 文件, 旧文本, 新文本)
MUTATIONS = [
    ("A", "禁用 topic matching（什么都当相关）",
     NE,
     "    if n <= 0:\n        return 99",
     "    if n <= 0:\n        return 0\n    if True:\n        return 0"),

    ("B", "禁用自然支持信号（认同不再被识别）",
     NE,
     "    if is_natural_support(t):\n"
     "        return NaturalSignal(SIGNAL_SUPPORT, reason=\"用户主动陈述认同\")",
     "    if False and is_natural_support(t):\n"
     "        return NaturalSignal(SIGNAL_SUPPORT, reason=\"用户主动陈述认同\")"),

    ("C1", "禁用否定优先级（'不是'不再是否定）",
     NE,
     "    if is_natural_denial(t):",
     "    if False and is_natural_denial(t):"),

    ("C2", "禁用变化优先级（'改成'不再是变化）",
     NE,
     "    if has_change_signal(t):",
     "    if False and has_change_signal(t):"),

    ("D", "让 quota_overflow 可以进入自然证据（存储层守卫）",
     HS,
     "        if (scope or \"\").startswith(\"quota_overflow:\"):\n"
     "            return False",
     "        if False:\n            return False"),

    # --- V0.5.7 理解展示链 ---
    ("E", "泄漏自检失效（内部字段可原样露出）",
     UV,
     "    return [w for w in FORBIDDEN_IN_OUTPUT if w in blob]",
     "    return []"),

    ("F", "取消变化识别（用户改主意也不标记）",
     UV,
     "    quote, when = _latest_contradiction(hypo)\n"
     "    if quote is None:\n"
     "        quote, when = _latest_change_log(hypo)",
     "    quote, when = None, \"\""),

    ("G", "无原话也照样展示（凭空造来源）",
     UV,
     "        text = (e.get(\"excerpt\") or \"\").strip()\n"
     "        if not text:\n            continue",
     "        text = (e.get(\"excerpt\") or \"\").strip()\n"
     "        if not text:\n            text = \"\u6765\u6e90\u672a\u8bb0\""),

    ("H", "让 quota_overflow 露给用户（越权展示）",
     UV,
     "    if s.startswith(\"quota_overflow:\"):\n        return \"\"",
     "    if False:\n        return \"\""),
]


def run_tests() -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "--tb=no",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    return p.returncode, (p.stdout or "")


def count_passed(out: str) -> int | None:
    """Windows 下 subprocess 抓到的 pytest 输出可能是 UTF-16LE，
    解析不出摘要就返回 None——退回只看退出码。"""
    for line in reversed(out.split("\n")):
        line = line.replace("\x00", "").strip()
        if " passed" in line or " failed" in line:
            import re
            m = re.search(r"(\d+) failed", line)
            if m:
                return -int(m.group(1))
            m = re.search(r"(\d+) passed", line)
            if m:
                return int(m.group(1))
    return None


def main() -> int:
    backup = tempfile.mkdtemp()
    results = []
    print("=" * 70)
    print("Mutation 基线：删掉保护条件后，测试必须变红")
    print("=" * 70)

    # 基线：未变异时必须全绿，否则说明测试本身有问题
    code, out = run_tests()
    base_ok = code == 0
    print(f"  基线（未变异）         EXIT={code} "
          f"{'全绿 [OK]' if base_ok else '已红 [X] —— 先修测试再谈变异'}")
    if not base_ok:
        print(out[-2500:])
        return 2

    for tag, desc, path, old, new in MUTATIONS:
        shutil.copy2(path, backup)
        try:
            src = io.open(path, encoding="utf-8").read()
            if old not in src:
                print(f"  {tag:<3} 无法定位变异点（源码已变），跳过")
                results.append((tag, desc, "SKIP", 0))
                continue
            io.open(path, "w", encoding="utf-8").write(
                src.replace(old, new, 1))
            code, out = run_tests()
            failed = count_passed(out)
            killed = code != 0
            mark = "被测试打中 [OK]" if killed else "未被打中 [X]（测试是摆设）"
            print(f"  {tag:<3} {desc}")
            print(f"       → EXIT={code}  {mark}")
            results.append((tag, desc, "KILLED" if killed else "SURVIVED", code))
        finally:
            shutil.copy2(
                os.path.join(backup, os.path.basename(path)), path)

    # 收尾：确认源码已完全还原
    code, out = run_tests()
    print()
    print(f"  还原后复跑             EXIT={code} "
          f"{'恢复全绿 [OK]' if code == 0 else '仍有失败 [X]'}")

    print()
    print("=" * 70)
    print("结果")
    print("=" * 70)
    for tag, desc, verdict, _code in results:
        print(f"  {tag:<3} {verdict:<9} {desc}")

    survived = [r for r in results if r[2] == "SURVIVED"]
    print()
    if survived or code != 0:
        print(f"  失败：{len(survived)} 个变异存活，还原未通过")
        return 1
    print(f"  全部 {len(results)} 个变异都被测试打中，且源码已还原。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())