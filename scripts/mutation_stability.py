"""Understanding Stability 的 mutation 基线。

每条对应任务书里明确点名的一条要求。写变异时的原则（本项目第七次踩坑后）：

  **锚点匹配 ≠ 行为改变。** 问的不是"这段代码变了吗"，而是
  "**行为**变了吗"。所以每条写完都要自己过一遍：
  把改动说成人话，是不是真的会让人做出不同的事？

八条（任务书点名要的）：
  A 降低 reverse threshold（1 天就 review）
  B 删除 independent-day 判断（条数当成天数）
  C 允许同日重复累计
  D 允许 AI 自己证明自己
  E 允许 under_review 进入普通 prompt
  F 删除 explicit correction guard
  G 删除 restore logic
  H 让 inactive 重新进入 prompt

用法：
    python scripts/mutation_stability.py
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

US = os.path.join(REPO, "core", "understanding_stability.py")
HS = os.path.join(REPO, "memory", "hypothesis_store.py")

TESTS = ["tests/test_understanding_stability.py", "tests/test_understanding_view.py"]

MUTATIONS = [
    # ---- A. 降低 reverse threshold ----
    # 锚点带上下文，避免误伤 docstring 里那行提到"= 2"的说明
    # （踩过：只锚 `REVIEW_MIN_INDEPENDENT_DAYS = 2` 会先命中注释，
    #  于是真正的那行代码没被改，变异变成 no-op —— 本项目第七次
    # 踩"锚点匹配 ≠ 行为改变"）。
    ("A", "reverse threshold 降到 1（单日反例就 review）",
     US,
     "# 1 天 = 不够：单日重复不算独立（_row_day 已经保证）。\n"
     "REVIEW_MIN_INDEPENDENT_DAYS = 2",
     "# 1 天 = 不够：单日重复不算独立（_row_day 已经保证）。\n"
     "REVIEW_MIN_INDEPENDENT_DAYS = 1"),

    # ---- B. 删除 independent-day 判断 ----
    # 真正的行为改变：把 COUNT(DISTINCT origin) 换成 COUNT(*) ——
    # 于是**条数**被当成**天数**。这才是"删除独立日判断"的实质。
    # （写成调用不存在的方法那种不算：那只是让测试因异常变红，
    #   不是验证了独立性判定被守住 —— 本项目已经因此吃过亏。）
    ("B", "拿条数当独立天数（同日多条被当成多天）",
     US,
     '            rows = con.execute(\n'
     '                "SELECT DISTINCT origin FROM hypothesis_evidence"\n'
     '                " WHERE hypothesis_id=? AND polarity=\'contradict\'"\n'
     '                + (" AND created_at_epoch >= ?" if since_epoch else ""),\n'
     '                ((hypo_id, since_epoch) if since_epoch else (hypo_id,)),\n'
     '            ).fetchall()',
     '            rows = con.execute(\n'
     '                "SELECT origin FROM hypothesis_evidence"\n'
     '                " WHERE hypothesis_id=? AND polarity=\'contradict\'"\n'
     '                + (" AND created_at_epoch >= ?" if since_epoch else ""),\n'
     '                ((hypo_id, since_epoch) if since_epoch else (hypo_id,)),\n'
     '            ).fetchall()'),

    # ---- C. 允许同日重复累计 ----
    # 换个入口打同一个洞：让同日 context 不再共享 origin
    # （`_default_origin` 本来对 context 产生 ctxday:<日期>，
    #   正是"同一天只算 1 个源"的唯一出处）。
    ("C", "同日重复被算成独立（context 不再按天共享 origin）",
     HS,
     '        return f"ctxday:{excerpt_day}"  # context：同日多条只算 1 个源（保守）',
     '        return f"ctx:{excerpt_day}:{source_id}"  # 变异：每条都独立'),

    # ---- D. 允许 AI 自己证明自己 ----
    ("D", "自我生成的 scope 也允许被推翻（self-proof）",
     US,
     "    if is_self_generated(scope):\n        return False",
     "    if False:\n        return False"),

    ("D2", "hypothesis 可以作为证据来源（自己证明自己）",
     US,
     "    if source_type not in SOURCE_TYPES:\n        return False",
     "    if False:\n        return False"),

    # ---- E. 允许 under_review 进入普通 prompt ----
    ("E", "under_review 也进 prompt（信任边界破）",
     US,
     "    if status == UNDER_REVIEW and user_reaffirmed:\n"
     "        return True\n    return False",
     "    return True"),

    # ---- F. 删除 explicit correction guard ----
    ("F", "忽略显式纠正（用户明确说了也只慢慢数天数）",
     US,
     "    if has_explicit_correction:\n",
     "    if False:\n"),

    # ---- G. 删除 restore logic ----
    ("G", "review 永不恢复（支持证据也不管）",
     US,
     "    if status != UNDER_REVIEW:\n"
     "        return Decision(False, status, status, \"不在复审中\")",
     "    if True:\n"
     "        return Decision(False, status, status, \"不在复审中\")"),

    # ---- H. 让 inactive 重新进入 prompt ----
    ("H", "inactive 也能进 prompt（失效的理解复活）",
     US,
     "    if status in PROMPT_STATUSES:\n        return True",
     "    if status in PROMPT_STATUSES or status == INACTIVE:\n"
     "        return True"),

    # ---- 附加两条（都是本轮真踩过 / 真想过的）----
    ("I", "inactive 阈值降到 0（刚进 review 立刻判死）",
     US,
     "# 绝不等于 0 —— 0 会让\"刚进 review 就立刻死\"，一次误判就永久损失。\n"
     "INACTIVE_MIN_INDEPENDENT_DAYS = 3",
     "# 绝不等于 0 —— 0 会让\"刚进 review 就立刻死\"，一次误判就永久损失。\n"
     "INACTIVE_MIN_INDEPENDENT_DAYS = 0"),

    ("J", "under_review 被并入 LIVE_STATUSES（直接进 prompt）",
     HS,
     'LIVE_STATUSES = ("tentative", "confirmed")   # 进入 Prompt / UI 可见',
     'LIVE_STATUSES = ("tentative", "confirmed", "under_review")'),

    ("K", "复审时额外压低置信度（恢复时升不回来）",
     US,
     '                        con.execute(\n'
     '                            "UPDATE hypotheses SET status=?, status_detail=?,"\n'
     '                            " status_changed_epoch=?, ever_reviewed=1,"\n'
     '                            " updated_at_iso=?, updated_at_epoch=? WHERE id=?",',
     '                        con.execute(\n'
     '                            "UPDATE hypotheses SET status=?, status_detail=?,'
     ' confidence=confidence-0.3,"\n'
     '                            " status_changed_epoch=?, ever_reviewed=1,"\n'
     '                            " updated_at_iso=?, updated_at_epoch=? WHERE id=?",'),

    ("L", "rejected / deleted 也纳入可改写状态（覆盖用户终局决定）",
     US,
     "    if status not in MANAGED_STATUSES:\n        return False",
     "    if False:\n        return False"),

    # ---- 抖动校准（2026-10-06）：三条都是 replay 抓到的真问题 ----
    ("M", "恢复 active 时不刷新 status_changed_epoch（旧反证一直被计数）",
     US,
     '                            " status_changed_epoch=?, ever_reviewed=1,"\n'
     '                            " updated_at_iso=?, updated_at_epoch=? WHERE id=?",\n'
     '                            (dec.to_status, status_detail(now),\n'
     '                             now, now_iso, now, hypo_id),\n'
     '                        )\n'
     '                    else:',
     '                            " ever_reviewed=1,"\n'
     '                            " updated_at_iso=?, updated_at_epoch=? WHERE id=?",\n'
     '                            (dec.to_status, status_detail(now),\n'
     '                             now_iso, now, hypo_id),\n'
     '                        )\n'
     '                    else:'),

    ("M2", "起点不看上次状态变更（天数又变回一生累计 → 抖动）",
     US,
     "        since = hypo.status_changed_epoch or None\n"
     "        days = self._reverse_days(hypo_id, since)\n"
     "        dec = decide_reverse(status=hypo.status,",
     "        days = self._reverse_days(hypo_id, None)\n"
     "        dec = decide_reverse(status=hypo.status,"),

    ("N", "恢复门槛退回 1 条支持（同一天重复原话就能推翻复审）",
     US,
     "    if support_independent_days < RESTORE_MIN_SUPPORT_DAYS:",
     "    if support_independent_days < 1:"),

    ("N2", "恢复不再要求独立日期（只看条数）",
     US,
     "    if support_independent_days < RESTORE_MIN_SUPPORT_DAYS:",
     "    if support_since_review < 1:"),

    ("O", "ever_reviewed 被清掉（恢复后第一次降级不再要求确认）",
     US,
     "                             ever_reviewed=bool(hypo.ever_reviewed))",
     "                             ever_reviewed=False)"),
]


def run_tests() -> tuple[int, str]:
    # **必须先清 __pycache__**。
    # 踩过的坑：Windows 上 pyc 的时间戳判定有时会让 Python 复用旧缓存，
    # 于是变异改了源码、pytest 却加载旧 .pyc → 变异"存活"。
    # 那个存活是假的：手动清掉缓存后同一个变异立刻被抓住。
    # 这不是本脚本独有的问题，**每个**变异脚本都该这么跑。
    for root, dirs, files in os.walk(REPO):
        if "__pycache__" in dirs:
            cache = os.path.join(root, "__pycache__")
            for f in os.listdir(cache):
                if f.endswith(".pyc"):
                    try:
                        os.remove(os.path.join(cache, f))
                    except OSError:
                        pass
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
    print("Understanding Stability mutation：破坏一处，测试必须变红")
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