"""Runtime Time Context 的 mutation 基线。

目的：回答"时间这块的测试真的在守什么"。

最要紧的两类是：

  * **时间被污染** —— 时间块不注入 / 悄悄被缓存 / 混进沉淀 / 沉淀进记忆。
    这几条一旦破了，用户会开始听到错的时间，而且**不会知道自己听错了**。
  * **相对时间漂移** —— 又是定时炸弹那一类。V0.5.7 的 `now` 参数
    早就存在但没往下传（`build_understanding` 收了不用），
    2026-10-03 才炸出来。这条专门盯住那条链路。

用法：
    python scripts/mutation_runtime_time.py
退出码 0 = 全部变异都被测试打中。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RT = os.path.join(REPO, "core", "runtime_time.py")
CV = os.path.join(REPO, "core", "conversation.py")
UV = os.path.join(REPO, "core", "understanding_view.py")

TESTS = ["tests/test_runtime_time.py", "tests/test_understanding_view.py",
         "tests/test_judgment.py"]

MUTATIONS = [
    ("A", "时间块不进提示词（模型继续瞎猜时间）",
     CV,
     "            time_block=self._time_block(),",
     "            time_block=\"\","),

    ("B", "时间被缓存（第二轮开始一直说过期时间）",
     RT,
     "        if raw.tzinfo is None:\n"
     "            return raw.astimezone()\n"
     "        return raw",
     "        if getattr(self, '_memo', None) is None:\n"
     "            self._memo = (raw.astimezone()\n"
     "                          if raw.tzinfo is None else raw)\n"
     "        return self._memo"),

    ("C", "时区参数被忽略（get_time(tz) 静默返回本地时间）",
     RT,
     "        tz = resolve_timezone(timezone)",
     "        tz = resolve_timezone(None)"),

    ("D", "未知时区静默降级成本地时间（悄悄给错时间）",
     RT,
     '    except Exception as exc:\n'
     '        raise UnknownTimezone(',
     '    except Exception as exc:\n'
     '        return None\n'
     '        raise UnknownTimezone('),

    ("E", "naive 时间当成 UTC（差 8 小时）",
     RT,
     "        if raw.tzinfo is None:\n"
     "            return raw.astimezone()",
     "        if raw.tzinfo is None:\n"
     "            return raw.replace(tzinfo=_dt_timezone.utc)"),

    ("F", "时间块不再声明权威性（用户说啥就信啥）",
     RT,
     'f"（本段由系统读取操作系统时钟生成，是唯一权威时间。"',
     'f"（时间仅供参考。"'),

    ("G", "时间块没有 ISO 8601（模型无法精确引用时刻）",
     RT,
     '                f"ISO 8601：{self.iso}\\n"',
     '                f""'),

    ("H", "读取时间失败时抛异常（辅助信息掀翻对话）",
     RT,
     '        return (f"{TIME_BLOCK_HEADER}\\n"\n'
     '                f"（读取系统时间失败：{exc}。"',
     '        raise\n'
     '        return (f"{TIME_BLOCK_HEADER}\\n"\n'
     '                f"（读取系统时间失败：{exc}。"'),

    ("I", "相对时间退回真时钟（重建定时炸弹）",
     UV,
     "            return human_date(v, now)",
     "            return human_date(v)"),

    ("J", "quotes 的时间不再注入 now（只有 formed_on 钉住）",
     UV,
     "    quotes = _quotes_from_evidence(hypo.get(\"evidence\"), now=now)",
     "    quotes = _quotes_from_evidence(hypo.get(\"evidence\"))"),

    ("K", "变化说明不再注入 now（'你在昨天说过'会飘）",
     UV,
     "    note = _change_note(hypo, now)",
     "    note = _change_note(hypo)"),
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
    print("Runtime Time Context mutation：破坏一处，测试必须变红")
    print("=" * 70)

    code, out = run_tests()
    base_ok = code == 0
    print(f"  BASE (no mutation)      EXIT={code} "
          f"{'all green [OK]' if base_ok else 'already red [X] -- fix tests first'}")
    if not base_ok:
        print(out[-2500:])
        return 2

    for tag, desc, path, old, new in MUTATIONS:
        shutil.copy2(path, backup)
        try:
            src = io.open(path, encoding="utf-8").read()
            if old not in src:
                print(f"  {tag:<3} cannot locate mutation site (source changed), skip")
                results.append((tag, desc, "SKIP", 0))
                continue
            io.open(path, "w", encoding="utf-8").write(
                src.replace(old, new, 1))
            code, out = run_tests()
            killed = code != 0
            mark = ("killed by tests [OK]" if killed
                    else "SURVIVED [X] (tests are a placebo)")
            print(f"  {tag:<3} {desc}")
            print(f"       -> EXIT={code}  {mark}")
            results.append((tag, desc, "KILLED" if killed else "SURVIVED", code))
        finally:
            shutil.copy2(
                os.path.join(backup, os.path.basename(path)), path)

    code, out = run_tests()
    print()
    print(f"  RESTORED                EXIT={code} "
          f"{'green again [OK]' if code == 0 else 'still failing [X]'}")

    print()
    print("=" * 70)
    print("Results")
    print("=" * 70)
    for tag, desc, verdict, _c in results:
        print(f"  {tag:<3} {verdict:<9} {desc}")

    survived = [r for r in results if r[2] == "SURVIVED"]
    print()
    if code != 0:
        print("  RESTORE FAILED -- source was not put back cleanly")
        return 2
    if survived:
        print(f"  {len(survived)} mutation(s) SURVIVED -- tests are not guarding "
              f"what they claim to guard")
        return 1
    print(f"  all {len(results)} mutations killed [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
