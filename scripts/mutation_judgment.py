"""Adaptive Judgment 链的 mutation 基线。

目的不是搞一套工业级变异框架，而是回答一个具体问题：

    "理解真的改变行为"这条链上的测试，真的能打住吗？

做法：人为破坏一处 → 跑 Judgment 相关测试 → **必须变红**。
破坏之后测试还是绿的，说明那条测试是摆设，必须回去修测试。

任务书点名要打的六个（A–F）都在；另加三个 G–I，
是本次开发过程中**真的踩到过**的坑——测试没打中它们，
是写完第一版跑出来的真 bug，不是想象出来的。

用法：
    python scripts/mutation_judgment.py
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

JD = os.path.join(REPO, "core", "judgment.py")
CV = os.path.join(REPO, "core", "conversation.py")

TESTS = ["tests/test_judgment.py"]

# 变异体：(代号, 描述, 文件, 旧文本, 新文本)
MUTATIONS = [
    # ---------------- 任务书指定的 A–F ----------------
    ("A", "删除当前用户指令优先级（长期偏好压过用户当场所说）",
     JD,
     "    now_mode = current_instruction_mode(message)\n"
     "    if now_mode is not None:",
     "    now_mode = None\n"
     "    if now_mode is not None:"),

    ("B", "让 tentative 假设也能影响 Judgment（猜测变成正式）",
     JD,
     'FORMAL_HYPOTHESIS_STATUS = ("confirmed",)',
     'FORMAL_HYPOTHESIS_STATUS = ("confirmed", "tentative")'),

    ("C", "让 background 已认可候选影响 Judgment（未毕业就指挥行为）",
     JD,
     '    if scope.startswith(BACKGROUND_PREFIX):\n'
     '        return False',
     '    if False and scope.startswith(BACKGROUND_PREFIX):\n'
     '        return False'),

    ("D", "让 rejected 理解影响 Judgment（被否决的偏好复活）",
     JD,
     'FORMAL_HYPOTHESIS_STATUS = ("confirmed",)',
     'FORMAL_HYPOTHESIS_STATUS = ("confirmed", "rejected", "expired")'),

    ("E", "删除正式理解 → 策略的映射（永远直答，理解再正式也没用）",
     JD,
     "    mode = preferred or MODE_DIRECT",
     "    mode = MODE_DIRECT"),

    ("F", "删除 hint_first 策略的执行指令（判断对了但不落到提示词）",
     JD,
     '    MODE_HINT_FIRST: "先给提示或思路，**不要直接给完整答案**，"',
     '    MODE_HINT_FIRST: "正常回答。"'),

    # ---------------- G–I：本次真踩到的坑 ----------------
    ("G", "否定失效（'别直接给我答案'被读成要答案——方向读反）",
     JD,
     "    window = t[max(0, pos - _NEG_WINDOW):pos]",
     "    window = \"\""),

    ("H", "解释里带出内部 scope（interaction:提示 泄进提示词和产品文案）",
     JD,
     '        reasons.append("我记得你之前说过这类问题你更想先自己想一想，"\n'
     '                       "所以这次我先给你提示。")',
     '        reasons.append("我记得你之前说过这类问题你更想先自己想一想"\n'
     '                       f"（{prefer_src}），所以这次我先给你提示。")'),

    ("I", "Judgment 故障不再兜底（判断层能把整轮对话带崩）",
     CV,
     "        except Exception as exc:\n"
     '            log.warning("Judgment 失败（退回默认直答）：%s", exc)\n'
     "            from .judgment import Judgment\n"
     '            return Judgment(response_mode="direct")',
     "        except Exception as exc:\n"
     '            log.warning("Judgment 失败（退回默认直答）：%s", exc)\n'
     "            raise"),

    ("J", "策略指令不再进提示词（Judgment 算完就扔，模型看不到）",
     CV,
     "            strategy_line = strategy_directive(judgment)",
     "            strategy_line = \"\""),

    ("K", "多个理解不再组合（只认第一条偏好，简洁偏好被丢掉）",
     JD,
     "        if not concise and prefers_concise(u.text):",
     "        if False and not concise and prefers_concise(u.text):"),
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
    print("Adaptive Judgment mutation：破坏一处，测试必须变红")
    print("=" * 70)

    # 基线：未变异时必须全绿，否则说明测试本身有问题
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
            mark = "killed by tests [OK]" if killed else "SURVIVED [X] (tests are a placebo)"
            print(f"  {tag:<3} {desc}")
            print(f"       -> EXIT={code}  {mark}")
            results.append((tag, desc, "KILLED" if killed else "SURVIVED", code))
        finally:
            shutil.copy2(
                os.path.join(backup, os.path.basename(path)), path)

    # 收尾：确认源码已完全还原
    code, out = run_tests()
    print()
    print(f"  RESTORED                EXIT={code} "
          f"{'green again [OK]' if code == 0 else 'still failing [X]'}")

    print()
    print("=" * 70)
    print("Results")
    print("=" * 70)
    for tag, desc, verdict, _code in results:
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
