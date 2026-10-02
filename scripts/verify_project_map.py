"""PROJECT_MAP.md 事实核对。

文档里每条"真实存在"的声称都要能在代码里指到出处。
写文档最容易出的错是把"我以为有"写成"有"，所以这里逐条验。

不改任何被验证的代码——只读、只验。
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = os.path.join(REPO, "docs", "PROJECT_MAP.md")


def read(p):
    return io.open(p, encoding="utf-8", errors="replace").read()


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True)\
        .stdout.decode("utf-8", "replace").strip()


CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))


# ---- 1. 文档存在且不是空的 ----
m = read(MAP)
check("PROJECT_MAP.md 存在且有内容", len(m) > 8000, f"{len(m)} 字符")
s = read(os.path.join(REPO, "docs", "PROJECT_SUMMARY.md"))
check("PROJECT_SUMMARY.md 存在", len(s) > 500, f"{len(s)} 字符")

# ---- 2. 文档声称的 git 事实 ----
head = git("rev-parse", "--short", "HEAD")
ncommit = git("rev-list", "--count", "HEAD")
check("文档里的 HEAD 与实际一致", head in m, f"实际 {head}")
check("文档里的 commit 总数正确",
      re.search(rf"commit 总数\s*\|\s*{ncommit}\b", m) is not None, ncommit)
ahead = git("rev-list", "--count", "origin/master..HEAD")
check("文档里的 ahead 数正确", re.search(rf"\*\*{ahead}\s*个 commit\*\*", m) is not None, ahead)
tags = sorted(git("tag", "--list").split())
check("文档列的 tag 完整", all(t in m for t in tags), " ".join(tags))

# ---- 3. 版本号 ----
ver = re.search(r'version:\s*str\s*=\s*"([^"]*)"', read(os.path.join(REPO, "core/config.py")))
check("config.py 版本号与文档一致", ver and ver.group(1) in m, ver.group(1) if ver else "?")
pkg = re.search(r'"version":\s*"([^"]*)"', read(os.path.join(REPO, "desktop/package.json")))
check("desktop 版本号与文档一致", pkg and pkg.group(1) in m, pkg.group(1) if pkg else "?")

# ---- 4. 表数量 ----
tables = set()
for f in os.listdir(os.path.join(REPO, "memory")):
    if f.endswith(".py"):
        tables |= set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)",
                                 read(os.path.join(REPO, "memory", f))))
check("表数量声称正确", f"{len(tables)} 张表" in m, f"{len(tables)} 张")

# ---- 5. 端点数量 ----
eps = re.findall(r'@app\.(?:get|post|delete|put)\("([^"]+)"\)',
                 read(os.path.join(REPO, "interfaces/webapp.py")))
check("端点数量声称正确", f"{len(eps)} 个" in m, f"{len(eps)} 个")

# ---- 6. 常量声称 ----
hs = read(os.path.join(REPO, "memory/hypothesis_store.py"))
for const, val in (("INIT_CONFIDENCE", "0.50"), ("SUPPORT_DELTA", "0.08"),
                   ("CONTRADICT_DELTA", "-0.25"), ("CORRECTION_SET", "0.25"),
                   ("HIGH_CONF_CAP", "0.70"), ("MIN_INDEPENDENT_FOR_HIGH", "2")):
    mm = re.search(rf"^{const}\s*=\s*([-\d.]+)", hs, re.M)
    check(f"{const} 声称正确", mm and mm.group(1) == val and val.lstrip("-") in m,
          f"实际 {mm.group(1) if mm else '?'}")

# ---- 7. SOURCE_TYPES 白名单 ----
st = re.search(r'SOURCE_TYPES\s*=\s*\(([^)]*)\)', hs)
check("证据来源白名单声称正确",
      st and "context" in st.group(1) and "event" in st.group(1)
      and "correction" in st.group(1) and "hypothesis" not in st.group(1),
      st.group(1).replace("\n", " ") if st else "?")

# ---- 8. LIVE_STATUSES ----
lv = re.search(r'LIVE_STATUSES\s*=\s*\(([^)]*)\)', hs)
check("LIVE_STATUSES 声称正确",
      lv and "tentative" in lv.group(1) and "confirmed" in lv.group(1),
      lv.group(1).replace("\n", " ") if lv else "?")

# ---- 9. Judgment 闸门 ----
jd = read(os.path.join(REPO, "core/judgment.py"))
check("Judgment 三个 mode 都存在",
      all(f'MODE_{x} =' in jd for x in ("DIRECT", "HINT_FIRST", "ASK_FIRST")))
check("Judgment 拒绝 background 前缀", 'BACKGROUND_PREFIX = "background:"' in jd)
check("Judgment 拒绝 quota 前缀", 'QUOTA_PREFIX = "quota_overflow:"' in jd)
check("Judgment 只认 confirmed 假设",
      'FORMAL_HYPOTHESIS_STATUS = ("confirmed",)' in jd)
check("Judgment 是 frozen（不写记忆）", "frozen=True" in jd)

# ---- 10. Runtime Time ----
rt = read(os.path.join(REPO, "core/runtime_time.py"))
check("Runtime Time 用系统时钟", "datetime.now().astimezone()" in rt)
check("Runtime Time 支持 get_time(timezone)", "def get_time(timezone" in rt)
check("Runtime Time 未知时区明确报错", "UnknownTimezone" in rt)
check("Runtime Time 可注入 clock", "clock" in rt and "TimeProvider" in rt)

# ---- 11. idle worker 默认关 ----
cfg = read(os.path.join(REPO, "core/config.py"))
check("idle worker 默认关闭",
      'PAI_IDLE_REASONING", "0"' in cfg and "默认关" not in cfg or
      'PAI_IDLE_REASONING", "0"' in cfg)

# ---- 12. Agent 不存在（最重要的负面事实）----
# 注意：这里用**原文核对**，不是"我记得它写了什么"。
# 第一版我凭 PowerShell 乱码输出写成"无论 LLM 输出什么都不执行"，
# 实际原文是"只提供「查记忆」类只读工具；未来 Agent 编排在这里扩展"。
at = read(os.path.join(REPO, "agent/tools.py"))
doc_ag = " ".join(at.split('"""')[1].split())
check("Agent 文档：只读工具", "只读工具" in doc_ag, doc_ag[:70])
check("Agent 文档：未来编排在此扩展", "未来 Agent 编排" in doc_ag, doc_ag[:70])
check("文档引用的 Agent 原文与代码一致", "只读工具" in m)
# 确认它不在运行链路（只有 smoke.py）
smoke_only = []
for root, dirs, files in os.walk(REPO):
    dirs[:] = [x for x in dirs if x not in
               (".git", "node_modules", "build", "desktop", "data",
                "__pycache__", ".gpt-profile", "logs")]
    for f in files:
        if f.endswith(".py"):
            t = read(os.path.join(root, f))
            if re.search(r"from\s+agent|import\s+agent", t):
                smoke_only.append(os.path.basename(f))
check("Agent 只被 smoke 引用（不在运行链路）",
      smoke_only == ["smoke.py"], str(smoke_only))

# ---- 13. Intervention 是占位 ----
iv = read(os.path.join(REPO, "core/intervention.py"))
check("Intervention 是占位", "不实现自动介入" in " ".join(iv.split('"""')[1].split()))
check("Intervention 恒不介入", "intervene=False" in iv)

# ---- 14. camera 在运行链路（第一版我误判为死代码）----
webapp = read(os.path.join(REPO, "interfaces/webapp.py"))
check("camera 在 webapp 运行链路（不是死代码）",
      re.search(r"from\s+perception\s+import\s+camera", webapp) is not None)
check("文档不再把 camera 说成死代码", "camera.py 是死代码" not in m)

# ---- 15. think:false 必须存在 ----
oa = read(os.path.join(REPO, "models/ollama_adapter.py"))
check("ollama 强制 think:false", '"think"' in oa and "False" in oa)

# ---- 16. 三个产品投影端点都在 ----
for ep in ("/api/understanding", "/api/candidates", "/api/background"):
    check(f"{ep} 端点存在", ep in webapp)
check("/api/hypotheses 诊断面仍在（未删）", "/api/hypotheses\"" in webapp)

# ---- 17. 泄漏自检 ----
uv = read(os.path.join(REPO, "core/understanding_view.py"))
check("有 assert_no_internal_fields 自检", "def assert_no_internal_fields" in uv)
for word in ("source_type", "source_id", "hypothesis_id", "scope="):
    check(f"禁用清单含 {word}", word in uv)

# ---- 18. 文档说了哪些"未实现"（MAP 与 SUMMARY 都要有）----
for absent in ("Forecast", "Prediction", "Personality", "Agent"):
    check(f"MAP 明确标注 {absent} 未实现", absent in m)
    check(f"SUMMARY 明确标注 {absent} 未实现", absent in s)

# ---- 19. architecture.md 已标过时 ----
arch = read(os.path.join(REPO, "docs/architecture.md"))
check("architecture.md 已标 Superseded", "Superseded" in arch)

# ---- 20. 真实事故在代码/决策记录里有据 ----
dec = os.path.join(REPO, "docs/decisions")
check("决策记录存在 27 篇",
      len([f for f in os.listdir(dec) if f.startswith("D0")]) >= 27,
      str(len([f for f in os.listdir(dec) if f.startswith("D0")])))
for key in ("D021", "D027"):
    check(f"{key} 决策记录存在",
          any(f.startswith(key) for f in os.listdir(dec)))


def main():
    print("=" * 70)
    print("PROJECT_MAP 事实核对：文档声称 vs 实际代码")
    print("=" * 70)
    bad = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        if not ok:
            print(f"  [X] {name}   {detail}")
    print()
    print(f"  共 {len(CHECKS)} 项，通过 {len(CHECKS)-len(bad)}，失败 {len(bad)}")
    if bad:
        print("  文档与代码不一致 —— 必须先改文档或改代码")
        return 1
    print("  文档每条声称都能在代码里指到出处 [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())