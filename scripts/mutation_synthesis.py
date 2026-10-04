"""Cross-Context Synthesis 的 mutation 基线。

要回答一个具体问题：

    "更会发现真实模式、也更懂什么时候证据不够"这条链上的测试，
    真的能打住吗？

做法：人为破坏一处 → 跑 synthesis 相关测试 → **必须变红**。
破坏之后测试还是绿的，说明那条测试是摆设，必须回去修测试。

任务书点名的 A–F 都在；G–J 是本次开发里**真的踩过**的坑，
以及"反向测试天然免疫删功能型变异"这个元教训。

用法：
    python scripts/mutation_synthesis.py
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

SY = os.path.join(REPO, "core", "synthesis.py")
SB = os.path.join(REPO, "core", "synthesis_bridge.py")
HS = os.path.join(REPO, "memory", "hypothesis_store.py")
PT = os.path.join(REPO, "core", "pattern_engine.py")
NJ = os.path.join(REPO, "core", "natural_bridge.py")
UV = os.path.join(REPO, "core", "understanding_view.py")

TESTS = ["tests/test_synthesis.py"]

# 变异体：(代号, 描述, 文件, 旧文本, 新文本)
MUTATIONS = [
    # ---------------- 任务书指定的 A–F ----------------
    ("A", "删除跨 context 条件（同一活动上的深投入也算跨 context）",
     SY,
     '    acts = {m.activity for m in deep if m.activity}\n'
     '    return len(acts) >= 2',
     '    acts = {m.activity for m in deep if m.activity}\n'
     '    return len(deep) >= 2'),

    ("B", "删除独立来源条件（同一天说多遍也算出模式）",
     SY,
     'MIN_INDEPENDENT_DAYS = 2',
     'MIN_INDEPENDENT_DAYS = 1'),

    ("C", "让单次表达就生成高层理解（同一天说多遍也算多份证据）",
     SY,
     '    return len({m.day for m in members if m.day})',
     '    return len([m for m in members if m.day])'),

    ("D", "让 synthesis candidate 进入 prompt（猜测当成事实）",
     HS,
     '        for h in self.list_hypotheses(live_only=True, limit=limit):\n'
     '            if (h.scope or "").startswith("background:"):\n'
     '                continue\n'
     '            if (h.scope or "").startswith("synthesis:"):\n'
     '                continue',
     '        for h in self.list_hypotheses(live_only=True, limit=limit):\n'
     '            if (h.scope or "").startswith("background:"):\n'
     '                continue'),

    ("E", "让 rejected candidate 复活（用户否决过还能再提）",
     HS,
     '    def synthesis_proposed(self, scope: str) -> bool:',
     '    def synthesis_proposed(self, scope: str) -> bool:\n'
     '        return False\n'
     '        scope = (scope or "").strip()[:SCOPE_MAX]'),

    ("F", "让旧模式在变化后继续被当作事实（删掉变化检测）",
     SY,
     '            acts, since = (ref if isinstance(ref, tuple)\n'
     '                           else (ref, 0.0))\n'
     '            out.extend(detect_pattern_decay(scope, members, acts,\n'
     '                                            since_epoch=since or 0.0))',
     '            pass'),

    # ---------------- G–J：本次真踩到的坑 ----------------
    # G 是变化检测的分界时刻。第一版用候选 created_at，于是"之后"一条
    # 都没有，故事 2 明明发生了却静默不产出——不报错、看起来像没实现。
    ("G", "变化检测用候选创建时刻当分界（支撑证据全被排除）",
     SB,
     '    since = 0.0\n'
     '    try:\n'
     '        since = float(d.get("evidence_until") or 0.0)',
     '    since = 0.0\n'
     '    try:\n'
     '        since = 0.0'),

    # H：create 的 detail 嵌套了一层。取用方按顶层键读全都读不到，
    # 症状同样是"变化检测静默不触发"。
    ("H", "create 的 detail 重新嵌一层（取用方读不到任何字段）",
     HS,
     '                           "independent_days": candidate.independent_days,\n'
     '                           **dict(candidate.detail)})',
     '                           "independent_days": candidate.independent_days,\n'
     '                           "detail": dict(candidate.detail)})'),

    # I：候选不再排除 pattern 晋级。
    ("I", "合成候选进入 Pattern（越过既有晋级门槛）",
     PT,
     '                if (h.scope or "").startswith("synthesis:"):\n'
     '                    continue',
     '                if False:\n'
     '                    continue'),

    # J：候选不再吃自然证据 → 认可之后也永远长不大（故事 1 断链）。
    ("J", "合成候选彻底不吃自然证据（认可之后也长不大）",
     HS,
     '        if (scope or "").startswith("synthesis:") \\\n'
     '                and status != ACCEPTED:\n'
     '            return False\n'
     '        return True',
     '        if (scope or "").startswith("synthesis:"):\n'
     '            return False\n'
     '        return True'),

    # K：topic_name 不再认合成规则名 → 毕业后的高层理解在
    # 「我眼中的你」里显示成一句没有主题的话（内部字段泄漏另一种形态）。
    ("K", "topic_name 不再把合成规则翻成人话（露出内部规则名）",
     UV,
     '        if sep and rule in _SYNTHESIS_TOPIC:\n'
     '            head = _SYNTHESIS_TOPIC[rule]\n'
     '            label = label.strip()\n'
     '            return f"{head} · {label}" if label and head else (label or head)',
     '        if False and sep and rule in _SYNTHESIS_TOPIC:\n'
     '            head = _SYNTHESIS_TOPIC[rule]\n'
     '            label = label.strip()\n'
     '            return f"{head} · {label}" if label and head else (label or head)'),

    # L：裁决直接 confirmed —— 后台推理/合成有了自我确认能力，
    # 整条信任链废掉（V0.5.9 mutation N 那个坑的合成版）。
    ("L", "裁决直接跳到 confirmed（候选自我确认）",
     HS,
     '                if row["status"] == PENDING:\n'
     '                    con.execute(',
     '                if False and row["status"] == PENDING:\n'
     '                    con.execute('),

    # M：变化检测不看活动交集 —— 拿别的活动变浅来说"这条模式变了"。
    ("M", "变化检测不看该模式自己的活动（拿别的事变浅来指证）",
     SY,
     '    rel = [m for m in members\n'
     '           if m.activity and m.activity in acts\n'
     '           and (not since_epoch or (m.epoch and m.epoch > since_epoch))]',
     '    rel = [m for m in members\n'
     '           if (not since_epoch or (m.epoch and m.epoch > since_epoch))]'),

    # ---------------- N–Q：第二版真踩到的坑 ----------------
    # N 是最严重的一个：graduate_candidates 把 scope 换成 inferred:，
    # 而参照系只认 synthesis: 前缀，于是模式一旦毕业，系统就再也看不见
    # 它的变化——旧理解继续留在 prompt 里影响每一次回答，而系统彻底沉默。
    ("N", "已毕业的合成理解退出变化检测参照系（旧理解不再被盯）",
     SB,
     '    if s.startswith("inferred:"):\n'
     '        return s[len("inferred:"):].split(":", 1)[0] in DECAY_WATCH_RULES',
     '    if s.startswith("inferred:"):\n'
     '        return False'),

    # O：变化候选排在新建候选后面 → 被 1 条/轮 的配额砍掉。
    ("O", "变化候选不再优先（新建候选抢走本轮唯一配额）",
     SB,
     '    ordered = ([c for c in candidates if c.pattern == "decay"]\n'
     '               + [c for c in candidates if c.pattern != "decay"])',
     '    ordered = list(candidates)'),

    # P：候选可以把自己当证据（自我证明）。
    # 打在 `_source_exists` 的 correction 分支**内部**——第一版把注入
    # 放在 `table = SOURCE_TABLE.get(...)` 那一行，可那个位置在
    # correction 分支的 `return` **之后**，永远走不到，于是成了空变异：
    # 脚本正确地把它标成 SURVIVED（脚本没坏，变异坏了）。
    # 教训：**锚点"能匹配上"不等于变异"真的改了行为"**，必须跑一次看结果。
    ("P", "候选自身成为合法证据来源（AI 的判断可以给自己作证）",
     HS,
     '            hit = con.execute(\n'
     '                "SELECT id FROM context_entries WHERE id=?", (source_id,)\n'
     '            ).fetchone()\n'
     '            if hit:\n'
     '                return True',
     '            hit = con.execute(\n'
     '                "SELECT id FROM hypotheses WHERE id=?", (source_id,)\n'
     '            ).fetchone()\n'
     '            if hit:\n'
     '                return True\n'
     '            hit = con.execute(\n'
     '                "SELECT id FROM context_entries WHERE id=?", (source_id,)\n'
     '            ).fetchone()\n'
     '            if hit:\n'
     '                return True'),

    # Q：自然证据对 accepted 合成候选全部放行 —— 连没被裁决过的
    # pending 也能吃到证据，候选不经用户确认就开始长大。
    ("Q", "pending 合成候选也能吃自然证据（不需用户确认）",
     HS,
     '        if (scope or "").startswith("synthesis:") \\\n'
     '                and status != ACCEPTED:\n'
     '            return False',
     '        if False:\n'
     '            return False'),
]


def run_tests() -> int:
    p = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "--tb=no",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    return p.returncode


def main() -> int:
    originals = {}
    for f in (SY, SB, HS, PT, UV):
        originals[f] = io.open(f, encoding="utf-8").read()

    print("=" * 70)
    print("Cross-Context Synthesis mutation")
    print("=" * 70)
    print(f"  测试文件: {', '.join(TESTS)}")
    print(f"  变异数: {len(MUTATIONS)}")
    print()

    killed, survived = [], []
    try:
        for code, desc, path, old, new in MUTATIONS:
            src = originals[path]
            if old not in src:
                print(f"  {code}   [X] 锚点没对上，跳过（文件已变？）")
                survived.append(code)
                continue
            io.open(path, "w", encoding="utf-8", newline="").write(
                src.replace(old, new, 1))
            rc = run_tests()
            io.open(path, "w", encoding="utf-8", newline="").write(src)
            tag = "killed by tests [OK]" if rc else "SURVIVED [X]"
            print(f"  {code}   {'KILLED   ' if rc else 'SURVIVED '} {desc}")
            print(f"       -> {tag}")
            (killed if rc else survived).append(code)
    finally:
        for f, src in originals.items():
            io.open(f, "w", encoding="utf-8", newline="").write(src)

    print()
    print("=" * 70)
    print("Results")
    print("=" * 70)
    for code, desc, *_ in MUTATIONS:
        mark = "KILLED   " if code in killed else "SURVIVED "
        print(f"  {code}   {mark} {desc}")
    print()
    if survived:
        print(f"  {len(survived)} mutation(s) SURVIVED "
              f"-- tests are not guarding what they claim to guard")
        return 1
    print(f"  all {len(MUTATIONS)} mutations killed [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
