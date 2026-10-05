"""llama.cpp 本地接入测试。

全部用假 HTTP（monkeypatch urlopen），**不打真网络、不加载真模型**。
真机连通性由用户自己点「连接测试」验证，这里只管行为。

三条要命的行为，各自单独钉死：

1. `-ngl` 在未指定时**不能**出现在命令行里
   （本机 4GB 显存 + 11GB 模型，带 -ngl 99 直接 OOM 启动失败）
2. 回答被 thinking 吃光时必须**加倍重试**
   （gpt-oss 的 reasoning_content 会吃满 max_tokens，content 变空）
3. **绝不**动不是我们启动的 llama-server
   （用户在命令行里跑的那个，被误杀就是事故）
"""
from __future__ import annotations

import json
import subprocess
import urllib.request

import pytest

from models import llamacpp_adapter as lc
from models.llamacpp_adapter import (
    DEFAULT_MAX_TOKENS,
    LlamaCppAdapter,
    LlamaCppServer,
    find_exe,
    find_gguf,
    health,
)
from models.settings_store import ModelSettingsStore, public_view


# ------------------------------------------------------------------ 假 HTTP

class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resp(payload):
    return _Resp(payload)


def _completion(content="", reasoning=""):
    return _resp({"choices": [{"message": {"role": "assistant",
                                           "content": content,
                                           "reasoning_content": reasoning},
                               "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 2}})


@pytest.fixture
def fake_http(monkeypatch):
    """把 urlopen 换成假响应。记录每次调用，返回队列里的下一个。"""
    calls: list[dict] = []
    queue: list = []

    def _urlopen(req, timeout=None):
        payload = getattr(req, "data", None)
        body = json.loads(payload) if payload else None
        calls.append({"body": body,
                      "url": getattr(req, "full_url", str(req)),
                      "headers": dict(getattr(req, "headers", {}) or {})})
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return _resp(item)
        raise urllib.error.URLError("fake_http: 队列空了")

    monkeypatch.setattr(lc.urllib.request, "urlopen", _urlopen)
    return {"calls": calls, "queue": queue}


# ------------------------------------------------- 一、命令行：-ngl 的真教训

def test_argv_omits_ngl_when_not_set():
    """没指定 ngl 就不许出现 -ngl。

    这条是硬约束：本机 RTX 3050 只有 4GB 显存，gpt-oss-20b 是 11.3GB，
    `-ngl 99` 的真实结果是 cudaMalloc failed → 进程直接退出。
    """
    srv = LlamaCppServer(exe="llama-server.exe", model="m.gguf")
    assert srv.ngl is None
    argv = srv.argv()
    assert "-ngl" not in argv
    assert "99" not in argv


def test_argv_includes_ngl_when_set():
    """用户主动指定了才传（他可能知道自己显存够）。"""
    srv = LlamaCppServer(exe="e.exe", model="m.gguf", ngl=20)
    argv = srv.argv()
    assert "-ngl" in argv
    assert argv[argv.index("-ngl") + 1] == "20"


def test_argv_zero_is_kept_not_treated_as_missing():
    """ngl=0 是有意义的（强制全 CPU），不能被当成"没填"吞掉。"""
    srv = LlamaCppServer(exe="e.exe", model="m.gguf", ngl=0)
    argv = srv.argv()
    assert "-ngl" in argv
    assert argv[argv.index("-ngl") + 1] == "0"


def test_argv_always_has_model_port_ctx():
    srv = LlamaCppServer(exe="e.exe", model="m.gguf", port=9999, ctx=8192)
    argv = srv.argv()
    assert argv[argv.index("-m") + 1] == "m.gguf"
    assert argv[argv.index("--port") + 1] == "9999"
    assert argv[argv.index("-c") + 1] == "8192"


def test_parallel_defaults_to_one():
    """单人应用开 4 个并发槽是白占内存（实测 llama.cpp 默认 n_slots=4）。"""
    assert LlamaCppServer(exe="e.exe", model="m.gguf").parallel == 1


def test_extra_args_appended():
    srv = LlamaCppServer(exe="e.exe", model="m.gguf",
                         extra_args="--no-warmup --jinja")
    argv = srv.argv()
    assert "--no-warmup" in argv and "--jinja" in argv


def test_describe_is_runnable_by_hand():
    """出问题时用户要能复制到命令行自己跑——所以是完整可执行的一行。"""
    line = LlamaCppServer(exe="e.exe", model="m.gguf").describe()
    assert line.startswith('"e.exe"')
    assert "-m m.gguf" in line


# ------------------------------------------------- 二、空回答必须重试

def test_chat_retries_when_content_empty(fake_http):
    """thinking 把 max_tokens 吃光 → content 空 → 必须加倍重试。"""
    fake_http["queue"].append(_CHAT_RAW_EMPTY)
    fake_http["queue"].append(_CHAT_RAW_OK)

    a = LlamaCppAdapter("http://127.0.0.1:8080", "m")
    out = a.chat([{"role": "user", "content": "hi"}])
    assert out == "答出来了"
    assert len(fake_http["calls"]) == 2
    first = fake_http["calls"][0]["body"]["max_tokens"]
    second = fake_http["calls"][1]["body"]["max_tokens"]
    assert second > first, "重试必须放大 token 预算"
    assert first == DEFAULT_MAX_TOKENS


def test_chat_no_retry_when_content_present(fake_http):
    fake_http["queue"].append(_CHAT_RAW_OK)
    a = LlamaCppAdapter("http://127.0.0.1:8080", "m")
    assert a.chat([{"role": "user", "content": "hi"}]) == "答出来了"
    assert len(fake_http["calls"]) == 1, "有正文就不该重试（白白翻倍延迟）"


def test_reasoning_never_leaks_into_product_output(fake_http):
    """思考过程不进产品——`处理摘要`/沉淀里出现"我先想了想…"是噪声。"""
    fake_http["queue"].append(_CHAT_RAW_OK)
    a = LlamaCppAdapter("http://127.0.0.1:8080", "m")
    out = a.chat([{"role": "user", "content": "hi"}])
    assert "Thinking Process" not in out
    assert "internal monologue" not in out


def test_stream_yields_content_only(fake_http):
    """流式也要滤掉思考，只推正文。"""
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "Thinking..."}}]},
        {"choices": [{"delta": {"content": "正"}}]},
        {"choices": [{"delta": {"content": "文"}}]},
    ]
    monkey = urllib.request.urlopen

    class _SSE:
        def __init__(self):
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for c in chunks:
                yield ("data: " + json.dumps(c) + "\n").encode()
            yield b"data: [DONE]\n"

    fake_http["queue"].append(None)
    import models.llamacpp_adapter as mod
    orig = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = lambda *a, **k: _SSE()
    try:
        a = LlamaCppAdapter("http://127.0.0.1:8080", "m")
        got = list(a.chat_stream([{"role": "user", "content": "hi"}]))
    finally:
        mod.urllib.request.urlopen = orig
    assert "".join(got) == "正文"
    assert not any("Thinking" in g for g in got)


def test_chat_raises_on_malformed_response(fake_http):
    """返回结构不对要说实话，不能当成"模型没说话"糊过去。"""
    fake_http["queue"].append({"choices": []})
    a = LlamaCppAdapter("http://127.0.0.1:8080", "m")
    with pytest.raises(RuntimeError):
        a.chat([{"role": "user", "content": "hi"}])


def test_strip_images_flattens_list_content():
    """llama.cpp 对多模态格式挑剔；Personal AI 看图走 Ollama 那条路。"""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "看这个"},
                                          {"type": "image_url",
                                           "image_url": {"url": "data:..."}}]}]
    out = lc._strip_images(msgs)
    assert out[0]["content"] == "看这个"
    assert "image_url" not in json.dumps(out)


# ------------------------------------------- 三、绝不碰别人起的 llama-server

def test_stop_does_nothing_when_we_did_not_start_it():
    """proc 是 None = 不是我们起的。用户在命令行里跑的那个不能被误杀。"""
    srv = LlamaCppServer(exe="e.exe", model="m.gguf")
    assert srv.alive() is False
    res = srv.stop()
    assert res["ok"] is True
    assert "没有我们启动" in res["note"]


def test_stop_really_kills_the_process_we_started():
    """**真起一个进程，真杀掉。**

    这条不是凑数：之前只有"不误杀外部进程"的测试（还是用假类测的），
    于是把 `if not self.alive(): return` 换成裸 `return` 之后，
    后面 12 行全变成死代码、stop() 变成永远不杀——变异测试活了下来。
    那不是变异的错，是这里本来就没测。
    """
    import sys
    import time

    srv = LlamaCppServer(exe="e.exe", model="m.gguf")
    srv.proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert srv.alive() is True
    pid = srv.proc.pid
    try:
        res = srv.stop()
        assert res["ok"] is True
        assert res.get("note") is None, "真杀掉了就不该返回『没进程』"
        assert srv.proc is None, "杀完要清掉句柄，不能重复杀"
        assert not _pid_alive(pid), f"进程 {pid} 还活着 —— 根本没杀掉"
    finally:
        if srv.alive():
            srv.proc.kill()


def test_stop_escalates_to_kill_when_terminate_ignored():
    """terminate 没用（子进程赖着不走）时要升级成 kill，不能干等。"""
    import sys

    srv = LlamaCppServer(exe="e.exe", model="m.gguf")
    srv.proc = subprocess.Popen(
        [sys.executable, "-c", "import signal,time\n"
         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = srv.proc.pid
    try:
        # 把 wait 压短，测试才跑得完；生产用 15 秒
        orig_wait = subprocess.Popen.wait
        try:
            subprocess.Popen.wait = lambda self, timeout=None: (
                self.poll() if self.poll() is None
                else (_ for _ in ()).throw(subprocess.TimeoutExpired("", 0)))
            res = srv.stop()
        finally:
            subprocess.Popen.wait = orig_wait
        assert res["ok"] is True
        assert not _pid_alive(pid)
    finally:
        if srv.alive():
            srv.proc.kill()


def _pid_alive(pid: int) -> bool:
    """Windows 上没有 os.kill(pid, 0) 这套；用 tasklist 判。"""
    import subprocess as sp
    out = sp.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                 capture_output=True, text=True, errors="replace")
    return str(pid) in (out.stdout or "")


def test_alive_is_false_after_process_exits():
    """自己退出的进程不该被当成"还活着"。"""
    import sys
    srv = LlamaCppServer(exe="e.exe", model="m.gguf")
    srv.proc = subprocess.Popen([sys.executable, "-c", "pass"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    srv.proc.wait(timeout=30)
    assert srv.alive() is False
    assert srv.stop()["ok"] is True


def test_status_distinguishes_ours_from_external(fake_http):
    """UI 要能说清"在跑，但不是我起的"。"""
    fake_http["queue"].append({"n_ctx": 4096, "model_path": "m.gguf"})
    fake_http["queue"].append({"data": [{"id": "m.gguf"}]})
    srv = LlamaCppServer(exe="e.exe", model="m.gguf")
    st = srv.status()
    assert st["running"] is True
    assert st["ours"] is False
    assert st["managed" if "managed" in st else "pid"] in (None,)


def test_start_reuses_healthy_server_instead_of_spawning(fake_http, monkeypatch):
    """已经有健康的服务就别再起一个——重复加载 11GB 模型是灾难。"""
    fake_http["queue"].append({"n_ctx": 4096})       # /health
    fake_http["queue"].append({"data": [{"id": "m"}]})  # /v1/models
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: pytest.fail("不该起新进程"))
    srv = LlamaCppServer(exe="e.exe", model="m.gguf")
    res = srv.start(wait_s=1)
    assert res["ok"] is True
    assert res["reused"] is True


def test_start_refuses_when_exe_missing(fake_http):
    fake_http["queue"].clear()
    srv = LlamaCppServer(exe="", model="")
    res = srv.start(wait_s=1)
    assert res["ok"] is False
    assert "llama-server" in res["error"]


def test_start_refuses_when_model_file_missing(fake_http, tmp_path):
    fake_http["queue"].clear()
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"MZ")
    srv = LlamaCppServer(exe=str(exe), model=str(tmp_path / "nope.gguf"))
    res = srv.start(wait_s=1)
    assert res["ok"] is False
    assert "模型文件" in res["error"]


def test_health_returns_none_when_server_down(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(lc.urllib.request, "urlopen", boom)
    assert health("http://127.0.0.1:59999") is None


def test_health_uses_props_not_health(fake_http):
    """/health 在模型没加载完时也返回 ok；/props 才有意义。"""
    fake_http["queue"].append({"n_ctx": 4096})
    health("http://127.0.0.1:8080")
    assert fake_http["calls"][0]["url"].endswith("/props")


# ------------------------------------------------------------- 四、扫盘

def test_find_gguf_excludes_vocab_and_projector(tmp_path):
    """词表和多模态投影器都不是模型，选了必然启动失败。

    真机实测踩到：扫出 10+ 个 ggml-vocab-*.gguf 和一个 0.84GB 的
    mmproj-Qwen3.6-*.gguf。
    """
    (tmp_path / "good.gguf").write_bytes(b"x" * (70 * 1024 * 1024))
    (tmp_path / "ggml-vocab-gemma.gguf").write_bytes(b"x" * (70 * 1024 * 1024))
    (tmp_path / "mmproj-Qwen3.6.gguf").write_bytes(b"x" * (70 * 1024 * 1024))
    (tmp_path / "tiny.gguf").write_bytes(b"x" * 1024)
    names = [m["name"] for m in find_gguf(roots=[tmp_path])]
    assert names == ["good.gguf"]


def test_find_gguf_sorted_big_first(tmp_path):
    (tmp_path / "small.gguf").write_bytes(b"x" * (70 * 1024 * 1024))
    (tmp_path / "big.gguf").write_bytes(b"x" * (300 * 1024 * 1024))
    got = find_gguf(roots=[tmp_path])
    assert [g["name"] for g in got] == ["big.gguf", "small.gguf"]


def test_find_gguf_returns_empty_not_guess(tmp_path):
    assert find_gguf(roots=[tmp_path]) == []


def test_find_exe_returns_empty_when_absent(tmp_path):
    """扫不到就返回空让用户手填——绝不猜一个路径上去。"""
    assert find_exe(roots=[tmp_path]) == ""


def test_find_exe_finds_known_layout(tmp_path):
    d = tmp_path / "llama-cuda-1234"
    d.mkdir()
    (d / "llama-server.exe").write_bytes(b"MZ")
    assert find_exe(roots=[tmp_path]).endswith("llama-server.exe")


# ----------------------------------------------------------- 五、配置存储

def _llamacpp_store(tmp_path, **over):
    patch = {"provider": "llamacpp", "llamacpp": {"base_url": "http://127.0.0.1:8080"}}
    patch["llamacpp"].update(over)
    return ModelSettingsStore(tmp_path / "ms.json").update(patch)


def test_settings_roundtrip_keeps_int_fields(tmp_path):
    """port/ctx/parallel 是 int，通用循环只收 str 会静默丢掉它们。"""
    st = _llamacpp_store(tmp_path, port=9090, ctx=8192, parallel=1)
    back = ModelSettingsStore(tmp_path / "ms.json").load()
    assert back["provider"] == "llamacpp"
    assert back["llamacpp"]["port"] == 9090
    assert back["llamacpp"]["ctx"] == 8192
    assert back["llamacpp"]["parallel"] == 1


def test_settings_bool_is_not_a_port(tmp_path):
    """bool 是 int 的子类——true 会被当成端口 1。"""
    st = ModelSettingsStore(tmp_path / "ms.json")
    st.save({"provider": "llamacpp",
             "llamacpp": {"port": True, "base_url": "http://127.0.0.1:8080"}})
    assert st.load()["llamacpp"]["port"] == 8080


def test_settings_accepts_numeric_string(tmp_path):
    """UI 表单交上来的一律是字符串。"""
    st = ModelSettingsStore(tmp_path / "ms.json")
    st.save({"provider": "llamacpp",
             "llamacpp": {"ctx": "8192", "base_url": "http://127.0.0.1:8080"}})
    assert st.load()["llamacpp"]["ctx"] == 8192


def test_settings_ngl_empty_stays_empty(tmp_path):
    """ngl 空 = 不传 -ngl。默认就必须是这样，否则新用户一开就 OOM。"""
    st = _llamacpp_store(tmp_path, ngl="")
    assert st["llamacpp"]["ngl"] == ""


def test_settings_requires_base_url(tmp_path):
    st = ModelSettingsStore(tmp_path / "ms.json")
    with pytest.raises(ValueError):
        st.resolve({"provider": "llamacpp", "llamacpp": {"base_url": "  "}})


@pytest.mark.parametrize("bad", [10, 999999])
def test_settings_rejects_absurd_context(tmp_path, bad):
    # 必须同时切 provider：_validate 只校验**当前生效**的 provider
    # （和 openai / ollama 的既有行为一致，不动）。
    st = ModelSettingsStore(tmp_path / "ms.json")
    with pytest.raises(ValueError):
        st.resolve({"provider": "llamacpp",
                    "llamacpp": {"ctx": bad,
                                 "base_url": "http://127.0.0.1:8080"}})


def test_settings_llamacpp_does_not_require_ollama_fields(tmp_path):
    """加第三家之后最容易踩的坑：`if openai / else` 会让 llamacpp 掉进
    Ollama 校验分支，然后报"请先配置 Ollama"。"""
    cfg = ModelSettingsStore(tmp_path / "ms.json").resolve(
        {"provider": "llamacpp", "llamacpp": {"base_url": "http://127.0.0.1:8080"}})
    assert cfg["provider"] == "llamacpp"


def test_settings_llamacpp_does_not_require_exe_to_save(tmp_path):
    """用户可以自己在别处起 llama-server，我们只负责连。"""
    cfg = _llamacpp_store(tmp_path)
    assert cfg["llamacpp"]["exe"] == ""


def test_public_view_hides_api_key(tmp_path):
    cfg = _llamacpp_store(tmp_path, api_key="sk-llamacpp-secret-123456")
    view = public_view(cfg)
    assert view["llamacpp"]["api_key_set"] is True
    assert "sk-llamacpp-secret-123456" not in json.dumps(view, ensure_ascii=False)


def test_llamacpp_is_a_known_provider(tmp_path):
    from models.settings_store import PROVIDERS
    assert "llamacpp" in PROVIDERS


# ----------------------------------------------------------------- 六、工厂

def test_factory_builds_llamacpp_adapter(tmp_path):
    from models.factory import build_model
    cfg = _llamacpp_store(tmp_path)
    a = build_model(cfg)
    assert isinstance(a, LlamaCppAdapter)
    assert a.base_url == "http://127.0.0.1:8080"


def test_factory_llamacpp_timeout_is_not_the_understand_timeout(tmp_path):
    """本地模型冷启动慢，超时不能跟着"理解"那一档的小值走。

    注意：`understand_timeout` 默认就是 180，所以断言必须**显式传一个小的
    timeout 进去**才能测出差别——只断 >=120 的话，变异成 `t` 一样能过。
    """
    from models.factory import build_model
    a = build_model(_llamacpp_store(tmp_path), timeout=5.0)
    assert a.timeout >= 120, f"5 秒的 timeout 不该原样透给本地模型，实际 {a.timeout}"


def test_factory_llamacpp_keeps_a_longer_explicit_timeout(tmp_path):
    """调用方给了更长的超时就别压回去——那是它自己算过的。"""
    from models.factory import build_model
    a = build_model(_llamacpp_store(tmp_path), timeout=600.0)
    assert a.timeout == 600.0


def test_factory_llamacpp_needs_base_url(tmp_path):
    from core.config import settings as app_settings
    from models.base import ModelUnavailableError
    from models.factory import build_model
    with pytest.raises(ModelUnavailableError):
        build_model({"provider": "llamacpp", "llamacpp": {"base_url": ""}})


def test_factory_unknown_provider_falls_back_to_ollama(tmp_path):
    """老配置里的 provider 值不该让 App 起不来。"""
    from models.llamacpp_adapter import LlamaCppAdapter  # noqa: F401
    from models.ollama_adapter import OllamaAdapter
    from models.factory import build_model
    a = build_model({"provider": "something-else", "ollama": {
        "url": app_settings_url(), "model": "m"}})
    assert isinstance(a, OllamaAdapter)


def app_settings_url():
    from core.config import settings
    return settings.ollama_url


# --------------------------------------------------------- 七、连接测试口径

def test_test_connection_reports_reasoning_only_truthfully(fake_http):
    """只输出思考、正文为空 —— 这是 thinking 预算不够的表现，要如实说。"""
    fake_http["queue"].append({"n_ctx": 4096})        # /props
    fake_http["queue"].append(_CHAT_RAW_EMPTY)       # 只思考，正文空
    a = LlamaCppAdapter("http://127.0.0.1:8080", "m")
    ok, detail = a.test_connection(5.0)
    assert ok is False
    assert "思考" in detail


def test_test_connection_ok_when_content_present(fake_http):
    fake_http["queue"].append({"n_ctx": 4096})
    fake_http["queue"].append(_CHAT_RAW_OK)
    a = LlamaCppAdapter("http://127.0.0.1:8080", "m")
    ok, detail = a.test_connection(5.0)
    assert ok is True
    assert "连接成功" in detail


def test_test_connection_explains_model_still_loading(monkeypatch):
    """llama-server 的 /health 在模型没加载完时也是 ok，别报"连不上"。"""
    monkeypatch.setattr(lc.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            urllib.error.URLError("refused")))
    a = LlamaCppAdapter("http://127.0.0.1:8080", "m")
    ok, detail = a.test_connection(5.0)
    assert ok is False
    assert "加载" in detail


# ------------------------------------------------------------------- 夹具

_CHAT_RAW_OK = {"choices": [{"message": {
    "role": "assistant", "content": "答出来了",
    "reasoning_content": "Thinking Process: blah blah internal monologue"},
    "finish_reason": "stop"}], "usage": {"completion_tokens": 5}}

_CHAT_RAW_EMPTY = {"choices": [{"message": {
    "role": "assistant", "content": "",
    "reasoning_content": "Thinking Process: " + ("long thought " * 200)},
    "finish_reason": "length"}], "usage": {"completion_tokens": 1024}}