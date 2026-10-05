"""本地 llama.cpp 适配器 + 一键启动。

llama.cpp 的 `llama-server` 暴露一个 OpenAI 兼容端点，所以协议层和
`OpenAICompatibleProvider` 基本一致。**但有两处必须单独处理**，否则
本地模型的表现会明显不如云端。

## 一、thinking 模型会把 max_tokens 吃光 → 回答为空

gpt-oss 走 harmony 格式：先输出 `reasoning_content`（思考），再输出
`content`（回答）。`max_tokens` 同时算两者，所以给小了会出现：

    content = ""    finish_reason = "length"

实测：`max_tokens=80` → 回答全空；`max_tokens=600` → 正常，且思考 399 字
+ 回答 36 字。

这和仓库里 Gemma 的老坑**完全同构**（thinking 吃光预算导致回答为空）。
所以这里：默认给足预算，并在"回答为空且像是被截断"时**自动加倍重试**一次。
不能只靠调大默认值——用户自己把滑块调小就会复发。

## 二、**绝不能**给 `-ngl 99`

实测教训：本机 RTX 3050 Laptop 只有 4GB 显存，gpt-oss-20b 是 11.3GB。
写 `-ngl 99` 的结果是：

    cudaMalloc failed: out of memory
    failed to create context with model ...
    llama_server: exiting due to model loading error

**不给 `-ngl`**，让 llama.cpp 自己按显存和内存分配，是能跑的唯一方式
（实测 23 tok/s，部分层上 GPU、其余走 CPU）。所以 launcher 默认
`ngl = None` → 不传这个参数；用户想手动指定时才传。

## 启动器的三条铁律

1. **绝不杀别人的进程。** 端口被占时先问"是谁"，是我们自己起的才复用/关闭。
2. **启动前先探活。** 已经有一个健康的 llama-server 就直接复用，不重复起。
3. **子进程不继承窗口。** `CREATE_NO_WINDOW`——否则每次点启动都弹一个黑框。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("models.llamacpp")

# llama-server 默认监听
DEFAULT_PORT = 8080
DEFAULT_CTX = 4096
# 单人应用开 4 个并发槽是白占内存，实测日志默认 n_slots=4
DEFAULT_PARALLEL = 1

# thinking 模型的回答预算。给小了 content 会空（见模块 docstring）。
DEFAULT_MAX_TOKENS = 1024
# 判定"像是被 thinking 吃光"的阈值：思考比回答长这么多倍且回答为空
_REASONING_DOMINANCE = 4.0

# 常见的 llama-server 位置。找不到就让用户手填，不猜。
_EXE_HINTS = (
    Path(r"D:\AI\gpt-oss-lab\llama-cuda-b11382\llama-server.exe"),
    Path(r"C:\llama.cpp\llama-server.exe"),
)
# 扫盘的根目录（限制深度，避免全盘扫描）
_SCAN_ROOTS = (Path("D:/AI"), Path("C:/llama.cpp"), Path("D:/llama.cpp"),
               Path(os.path.expanduser("~/Downloads")))


def _http_json(url: str, payload: dict | None = None,
               timeout: float = 30.0) -> dict:
    """发一个 JSON 请求。GET 走 payload=None。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def health(base_url: str, timeout: float = 3.0) -> dict | None:
    """服务是否健康。返回 props 或 None。

    注意 llama.cpp 的 `/health` 在模型还没加载完时也会返回 ok，
    所以真正可用要看 `/props`（它只在 context 建好之后才有内容）。
    """
    try:
        props = _http_json(base_url.rstrip("/") + "/props", timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return props if isinstance(props, dict) and props else None


def server_models(base_url: str = "", timeout: float = 3.0) -> list[str]:
    """服务端自报的模型名列表。"""
    url = (base_url or f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")
    try:
        r = _http_json(url + "/v1/models", timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []
    return [str(m.get("id", "")) for m in (r.get("data") or []) if m.get("id")]


# ---------------------------------------------------------------- 启动器

class LlamaCppServer:
    """管理一个 llama-server 子进程。

    **只管自己起的进程。** `self.proc is None` 时所有操作都是"只探不动"，
    这样用户在别处手动起的 llama-server 不会被我们误杀。
    """

    def __init__(self, exe: str = "", model: str = "", *,
                 port: int = DEFAULT_PORT, ctx: int = DEFAULT_CTX,
                 ngl: int | None = None, parallel: int = DEFAULT_PARALLEL,
                 extra_args: str = "", base_url: str = ""):
        self.exe = (exe or "").strip()
        self.model = (model or "").strip()
        self.port = int(port or DEFAULT_PORT)
        self.ctx = int(ctx or DEFAULT_CTX)
        # None = 不传 -ngl，让 llama.cpp 自动适配显存（模块 docstring 第二节）
        self.ngl = ngl
        self.parallel = int(parallel or DEFAULT_PARALLEL)
        self.extra_args = (extra_args or "").strip()
        self.base_url = (base_url or f"http://127.0.0.1:{self.port}").rstrip("/")
        self.proc: subprocess.Popen | None = None
        self.log_path: Path | None = None

    # ---------- 参数 ----------
    def argv(self) -> list[str]:
        a = ["-m", self.model, "--port", str(self.port),
             "-c", str(self.ctx), "--parallel", str(self.parallel)]
        if self.ngl is not None:
            a += ["-ngl", str(self.ngl)]
        if self.extra_args:
            a += self.extra_args.split()
        return a

    def describe(self) -> str:
        """给用户看的启动命令——出问题时他能自己复制到命令行跑。"""
        return f'"{self.exe}" ' + " ".join(self.argv())

    # ---------- 状态 ----------
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def ours(self) -> bool:
        """这个服务是不是**我们**启动的。

        做成只读属性而不是在 status() 里临时算：调用方（webapp 的
        stop 端点、start 的复用判断）直接问它，语义只有一个出处。
        判据是"我们还攥着这个进程的句柄"——`stop()` 会把句柄清掉，
        所以停掉之后自然变成 False，不会残留一个假的"我们启动的"。
        """
        return self.alive()

    def status(self) -> dict:
        """给 UI 的状态。区分"我们起的"和"外面已经有的"。"""
        props = health(self.base_url)
        return {
            "running": props is not None,
            "ours": self.ours,
            "pid": self.proc.pid if self.alive() else None,
            "base_url": self.base_url,
            "model": self.model,
            "models": server_models(self.base_url) if props else [],
            "command": self.describe() if self.exe else "",
            "log_path": str(self.log_path) if self.log_path else "",
        }

    # ---------- 生死 ----------
    def start(self, wait_s: float = 240.0) -> dict:
        """启动。等模型加载完（可能几十秒到几分钟）。

        先探活：已经有健康的服务就直接复用，**不重复起**。
        """
        if health(self.base_url):
            return {"ok": True, "reused": True, **self.status()}
        if not self.exe or not Path(self.exe).exists():
            return {"ok": False, "error": f"找不到 llama-server：{self.exe or '(未填)'}"}
        if not self.model or not Path(self.model).exists():
            return {"ok": False, "error": f"找不到模型文件：{self.model or '(未填)'}"}
        if self.alive():
            return {"ok": False, "error": "进程已在运行"}

        logdir = Path(os.environ.get("TEMP", ".")) / "personal-ai-llamacpp"
        logdir.mkdir(parents=True, exist_ok=True)
        self.log_path = logdir / "llama-server.log"
        env = dict(os.environ)
        # llama.cpp 的 CUDA 版依赖同目录的一堆 DLL，cwd 必须是 exe 所在目录
        cwd = str(Path(self.exe).parent)
        log.info("启动 llama-server: %s", self.describe())
        try:
            with open(self.log_path, "w", encoding="utf-8") as lf:
                self.proc = subprocess.Popen(
                    [self.exe] + self.argv(), cwd=cwd, env=env,
                    stdout=lf, stderr=subprocess.STDOUT,
                    # 不弹黑框
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self.proc = None
            return {"ok": False, "error": f"启动失败：{exc}"}

        deadline = time.time() + wait_s
        while time.time() < deadline:
            if not self.alive():
                tail = self._tail(12)
                self.proc = None
                return {"ok": False, "error": f"llama-server 启动后退出：\n{tail}",
                        "log_path": str(self.log_path)}
            if health(self.base_url, timeout=2.0):
                log.info("llama-server 就绪 %s", self.base_url)
                return {"ok": True, "reused": False, **self.status()}
            time.sleep(2.0)
        return {"ok": False, "error": f"{int(wait_s)} 秒内没加载完"
                                     f"（大模型首次加载很慢，再等等或看日志）",
                "log_path": str(self.log_path), **self.status()}

    def _tail(self, n: int = 10) -> str:
        if not self.log_path or not self.log_path.exists():
            return "(没有日志)"
        try:
            lines = self.log_path.read_text(encoding="utf-8",
                                            errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return "(日志读不出来)"

    def stop(self) -> dict:
        """停掉**我们自己起的**进程。

        外部起的（`self.proc is None`）一律不动——那是用户自己的东西。
        """
        if not self.alive():
            return {"ok": True, "note": "没有我们启动的进程"}
        proc, self.proc = self.proc, None
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "杀不掉"}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def wait_ready(self, wait_s: float = 180.0) -> bool:
        """等待已有服务就绪（用于"我已经手动起了，你等等"）。"""
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if health(self.base_url, timeout=2.0):
                return True
            time.sleep(2.0)
        return False


# ---------------------------------------------------------------- 自动发现

def find_exe(roots=None) -> str:
    """找 llama-server.exe。

    `roots` 非 None 时**只看这些目录**——否则传了参数却被硬编码路径抢先，
    调用方（测试、将来的自定义扫描）拿到的答案不是它问的那个。
    只有默认调用才额外看已知位置。
    """
    if roots is None:
        for p in _EXE_HINTS:
            if p.exists():
                return str(p)
        roots = _SCAN_ROOTS
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("llama-server.exe"):
                if p.is_file():
                    return str(p)
        except (OSError, PermissionError):
            continue
    return ""


def find_gguf(roots=None, limit: int = 40) -> list[dict]:
    """扫 *.gguf。返回 [{path, name, size_gb}]，大的在前。

    必须排除两类"不是模型"的 gguf，否则用户选了就启动失败：

    - `ggml-vocab-*.gguf`  词表（实测扫出 10+ 个，最大的 15MB）
    - `mmproj-*.gguf`      多模态投影器（实测 Qwen3.6 那个 0.84GB）
      名字只有 mmproj- 前缀，没有别的线索，所以按前缀排
    """
    out: list[dict] = []
    seen: set[str] = set()
    for root in (roots if roots is not None else _SCAN_ROOTS):
        root = Path(root)
        if not root.is_dir():
            continue
        try:
            found = list(root.rglob("*.gguf"))
        except (OSError, PermissionError):
            continue
        for p in found:
            key = str(p).lower()
            if key in seen:
                continue
            low = p.name.lower()
            if low.startswith("ggml-vocab-") or low.startswith("mmproj-"):
                continue
            seen.add(key)
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size < 64 * 1024 * 1024:      # <64MB 不可能是真模型
                continue
            out.append({"path": str(p), "name": p.name,
                        "size_gb": round(size / 1024 ** 3, 2)})
    out.sort(key=lambda d: -d["size_gb"])
    return out[:limit]


# ---------------------------------------------------------------- 对话

class LlamaCppAdapter:
    """走 llama-server 的 OpenAI 兼容端点。

    与 `OpenAICompatibleProvider` 的差别只有两处，都是必须的：
    更大的 max_tokens，以及**空回答自动加倍重试**。
    """

    name = "llamacpp"

    def __init__(self, base_url: str = "", model: str = "",
                 api_key: str = "", timeout: float = 180.0):
        self.base_url = (base_url or f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")
        self.model = model or "local"
        self.api_key = api_key
        self.timeout = timeout

    # ---------- 与 BaseModelAdapter 对齐 ----------
    def _post(self, payload: dict, timeout: float | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")

    def chat(self, messages, *, images=None, timeout=None) -> str:
        """发一轮对话，返回**正文**（不含思考过程）。

        思考内容不进产品：`处理摘要`/沉淀里出现"我先想了想…"是噪声。
        """
        text = self._chat_raw(messages, images, timeout,
                              max_tokens=DEFAULT_MAX_TOKENS)
        if text.strip():
            return text
        # 回答空 —— 多半是 thinking 吃光了预算。加倍再来一次。
        log.info("llama.cpp 回答为空（疑似 thinking 吃光预算），加倍重试")
        return self._chat_raw(messages, images, timeout,
                              max_tokens=DEFAULT_MAX_TOKENS * 3)

    def _chat_raw(self, messages, images, timeout, *, max_tokens: int) -> str:
        payload = {
            "messages": _strip_images(messages),
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "stream": False,
        }
        r = self._post(payload, timeout)
        try:
            msg = r["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"llama.cpp 返回格式异常：{exc}") from exc
        content = (msg.get("content") or "").strip()
        if not content:
            # 思考很长但正文空 → 确认就是被截断，如实说明而不是装作没这回事
            reasoning = (msg.get("reasoning_content") or "").strip()
            if len(reasoning) > len(content) * _REASONING_DOMINANCE and reasoning:
                log.warning("llama.cpp：思考 %d 字但正文为空（max_tokens=%d）",
                            len(reasoning), max_tokens)
        return content

    def chat_stream(self, messages, *, images=None, timeout=None):
        """流式。只取正文，思考过程直接丢掉。"""
        payload = {
            "messages": _strip_images(messages),
            "max_tokens": DEFAULT_MAX_TOKENS,
            "temperature": 0.7,
            "stream": True,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    delta = json.loads(body)["choices"][0].get("delta", {})
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                piece = (delta.get("content") or "")
                if piece:
                    yield piece

    def ping(self, timeout: float = 3.0) -> bool:
        return health(self.base_url, timeout=timeout) is not None

    def test_connection(self, timeout: float = 20.0) -> tuple[bool, str]:
        """真发一句，别只看 /health——那在模型没加载完时也是 ok。"""
        try:
            props = health(self.base_url, timeout=min(timeout, 5.0))
        except Exception as exc:                      # noqa: BLE001
            return False, f"连不上 llama-server：{exc}"
        if not props:
            return False, ("连不上 llama-server（模型可能还在加载，"
                           "首次加载大模型要几十秒到几分钟）")
        try:
            r = self._post({"messages": [{"role": "user", "content": "说「好」"}],
                            "max_tokens": 256, "temperature": 0.1}, timeout)
            msg = r["choices"][0]["message"]
        except (urllib.error.URLError, OSError, ValueError, KeyError,
                IndexError, TypeError, TimeoutError) as exc:
            return False, f"调用失败：{exc}"
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        if content:
            return True, f"连接成功 · 模型在回答（思考 {len(reasoning)} 字）"
        if reasoning:
            return False, ("模型只输出了思考过程、正文为空——"
                           "这是 thinking 模型预算不够的表现，重试一次通常就好")
        return False, "模型返回了空内容"

    @property
    def model_name(self) -> str:
        return self.model


def _strip_images(messages) -> list[dict]:
    """去掉图片字段。

    llama.cpp 的 OpenAI 兼容层对多模态格式挑剔，而 Personal AI 的
    图片理解本来就靠 Ollama 那条路。宁可少传也不要让整个请求 400。
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        item = {"role": m.get("role", "user"),
                "content": m.get("content", "")}
        if isinstance(item["content"], list):
            # 只取有文字的部分：image_url 项没有 text 字段，
            # 直接 join 会拼出尾部多余的空格（"看这个 " != "看这个"）。
            texts = [str(c.get("text", "")).strip() for c in item["content"]
                     if isinstance(c, dict)]
            item["content"] = " ".join(t for t in texts if t)
        out.append(item)
    return out
