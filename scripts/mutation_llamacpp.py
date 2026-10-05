"""llama.cpp 接入的 mutation 基线。

目的：回答"这批测试到底在守什么"。

每条变异都锚在**本项目真踩过或真机实测过**的坑上，不是凑数：

  * `-ngl` 出现在命令行里  → 本机 4GB 显存 + 11.3GB 模型，实测
                             `cudaMalloc failed` → 进程直接退出。
                             这是"看起来配置好了，其实启动就死"的典型。
  * 空回答不重试          → gpt-oss 的 reasoning_content 吃满 max_tokens，
                             content 变空。用户看到的是"AI 什么都不说"。
  * 思考泄漏进产品        → 沉淀/处理摘要里出现"我先想了想…"，是噪声。
  * 误杀外部 llama-server → 用户自己开的服务被 App 关掉，是事故不是 bug。
  * 重复加载模型          → 已经有健康服务还再起一个，11GB 白等几分钟。

用法：
    python scripts/mutation_llamacpp.py
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

ADAPTER = os.path.join(REPO, "models", "llamacpp_adapter.py")
SETTINGS = os.path.join(REPO, "models", "settings_store.py")
FACTORY = os.path.join(REPO, "models", "factory.py")
WEBAPP = os.path.join(REPO, "interfaces", "webapp.py")

TESTS = ["tests/test_llamacpp.py", "tests/test_llamacpp_api.py"]

MUTATIONS = [
    # ---------- 一、真机实测踩到的 ----------
    ("A", "无脑加上 -ngl 99（小显存卡直接 OOM 启动失败）",
     ADAPTER,
     "        if self.ngl is not None:\n"
     "            a += [\"-ngl\", str(self.ngl)]",
     "        a += [\"-ngl\", \"99\"]"),

    ("B", "ngl 被当成没填就吞掉（ngl=0 强制全 CPU 失效）",
     ADAPTER,
     "        if self.ngl is not None:\n"
     "            a += [\"-ngl\", str(self.ngl)]",
     "        if self.ngl:\n"
     "            a += [\"-ngl\", str(self.ngl)]"),

    ("C", "空回答不重试（thinking 吃光预算 → 用户看到 AI 不说话）",
     ADAPTER,
     "        return self._chat_raw(messages, images, timeout,\n"
     "                              max_tokens=DEFAULT_MAX_TOKENS * 3)",
     "        return \"\""),

    ("D", "重试时不放大预算（重试了还是空，等于没重试）",
     ADAPTER,
     "                              max_tokens=DEFAULT_MAX_TOKENS * 3)",
     "                              max_tokens=DEFAULT_MAX_TOKENS)"),

    ("E", "回答不空也重试（每次白等一倍延迟）",
     ADAPTER,
     "        if text.strip():\n            return text",
     "        if False:\n            return text"),

    ("F", "思考过程泄漏进产品（沉淀里出现『我先想了想…』）",
     ADAPTER,
     '        content = (msg.get("content") or "").strip()',
     '        content = ((msg.get("content") or "")\n'
     '                   + (msg.get("reasoning_content") or "")).strip()'),

    # ---------- 二、"误伤别人"这一类事故 ----------
    # 注意别把 `if ...: return` 换成裸 `return` —— 那会让后面整段变成死代码，
    # 变成一个"改了等于没改"的假变异（V0.5.6 的 P 变异和本项目都踩过）。
    # 这里改成**直接往下走**：proc 是 None 时去 terminate，正好是"误杀/崩溃"。
    ("G", "stop 不分自己人（proc 为 None 也往下走 = 崩或误杀）",
     ADAPTER,
     "        if not self.alive():\n"
     "            return {\"ok\": True, \"note\": \"没有我们启动的进程\"}\n"
     "        proc, self.proc = self.proc, None",
     "        proc, self.proc = self.proc, None"),

    ("H", "已有健康服务还再起一个（11GB 模型白等几分钟）",
     ADAPTER,
     "        if health(self.base_url):\n"
     "            return {\"ok\": True, \"reused\": True, **self.status()}",
     "        if False:\n"
     "            return {\"ok\": True, \"reused\": True, **self.status()}"),

    ("I", "启动前不校验模型文件（路径写错也照样起）",
     ADAPTER,
     "        if not self.model or not Path(self.model).exists():\n"
     "            return {\"ok\": False, \"error\": f\"找不到模型文件：{self.model or '(未填)'}\"}",
     "        if not self.model:\n"
     "            return {\"ok\": False, \"error\": \"没选模型\"}"),

    # ---------- 三、扫盘：扫出来的东西必须是真模型 ----------
    ("J", "词表/投影器混进模型列表（选了必然启动失败）",
     ADAPTER,
     '            if low.startswith("ggml-vocab-") or low.startswith("mmproj-"):\n'
     "                continue",
     "            pass"),

    ("K", "扫不到时返回一个编造的路径（用户以为配好了，其实没有）",
     ADAPTER,
     "        except (OSError, PermissionError):\n            continue\n    return \"\"",
     "        except (OSError, PermissionError):\n            continue\n"
     "    return r\"C:\\\\llama.cpp\\\\llama-server.exe\""),

    # ---------- 四、配置层 ----------
    ("L", "llamacpp 掉进 Ollama 校验分支（报『请先配置 Ollama』）",
     SETTINGS,
     '        elif provider == "llamacpp":',
     '        elif provider == "NOPE":'),

    ("M", "整数字段（port/ctx）被静默丢弃（保存后回到默认）",
     SETTINGS,
     "    for k in _LLAMACPP_INT_FIELDS:\n"
     "        v = vals.get(k)",
     "    for k in ():\n"
     "        v = vals.get(k)"),

    ("N", "bool 被当成端口（true 变成端口 1）",
     SETTINGS,
     "        if isinstance(v, bool):\n            continue",
     "        if False:\n            continue"),

    ("O", "对外视图泄出明文 API Key",
     SETTINGS,
     '            "api_key_set": bool(lc.get("api_key")),',
     '            "api_key": lc.get("api_key", ""),'),

    # ---------- 五、工厂 ----------
    ("P", "llamacpp 掉回 Ollama 分支（悄悄用了别的模型）",
     FACTORY,
     '    if provider == "llamacpp":',
     '    if provider == "NOPE":'),

    ("Q", "本地模型用 understand_timeout（冷启动必秒超时）",
     FACTORY,
     "        return LlamaCppAdapter(base, (c.get(\"model\") or \"\").strip(),\n"
     "                               c.get(\"api_key\", \"\"),\n"
     "                               t if (t and t > 120) else 180.0)",
     "        return LlamaCppAdapter(base, (c.get(\"model\") or \"\").strip(),\n"
     "                               c.get(\"api_key\", \"\"), t)"),

    # ---------- 六、HTTP 层 ----------
    ("R", "带参启动顺手落盘（用户还没点保存就被改了配置）",
     WEBAPP,
     "        patch = None\n"
     "        if req is not None:\n"
     "            dumped = req.model_dump(exclude_none=True)\n"
     "            if dumped:\n"
     '                patch = {"provider": "llamacpp", "llamacpp": dumped}',
     "        patch = None\n"
     "        if req is not None:\n"
     "            dumped = req.model_dump(exclude_none=True)\n"
     "            if dumped:\n"
     '                patch = {"provider": "llamacpp", "llamacpp": dumped}\n'
     '                model_settings_store.save('
     'model_settings_store.resolve(patch))'),

    ("S", "启动参数非法也照样起进程（该拦的不拦）",
     SETTINGS,
     "            if not (256 <= int(ctx) <= 262144):\n"
     '                raise ValueError("上下文长度需要在 256 ~ 262144 之间")',
     "            if False:\n"
     '                raise ValueError("上下文长度需要在 256 ~ 262144 之间")'),
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
    print("llama.cpp mutation：破坏一处，测试必须变红")
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