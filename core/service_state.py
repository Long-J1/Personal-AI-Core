"""服务状态缓存（status 去联网）。

背景：`/api/status` 是桌面壳的**健康探针**，却在里面打跨网请求
（`model.ping()` → `GET {base}/models`）。后果有三个：

1. 页面每 15 秒一次跨网往返，单次 640~821ms，网络慢时冲破 0.8s 探测线，
   壳子判定 Core 没起来——这个坑踩过一次，当时只是用 0.8s 限死止血；
2. "在线"只代表 provider 端点可达，**不代表当前配置的模型可用**
   （实测智谱 `/models` 列表里就没有 glm-4-flash，徽章照样绿）；
3. 探针不该有网络副作用。

所以：status 改成**纯本地读**——只读这份缓存 + 本地 DB 计数，一个包都不发。
缓存由**真实调用**写入：

  · 真实聊天成功 / 失败        （core/conversation.py）
  · 用户主动点"测试连接"       （/api/model/test，webapp.py）
  · 模型设置一改就重置为 unknown（PUT /api/model/settings，webapp.py）

三态：
  unknown  还没做过任何真实调用。**首次打开页面就是这个，别为了好看去联网。**
  ok       最近一次真实调用成功，且 provider/base_url/model 至今没变过。
  error    最近一次真实调用失败，或模型测试判定当前模型不可用。

身份绑定：换了 provider / base_url / model 之后，旧状态一律作废
（snapshot 发现身份对不上就返回 unknown），不让上一个模型的"在线"污染新模型。

线程安全：写入可能发生在 FastAPI 线程池（/api/conversation 是同步端点）、
事件循环线程（SSE 流）和后台任务里；读取发生在 status 线程池。
用 `threading.Lock`（不是 asyncio.Lock —— 跨线程不成立），
且只在碰缓存的瞬间持锁，绝不持锁做 I/O。
"""
from __future__ import annotations

import threading
import time

STATE_UNKNOWN = "unknown"
STATE_OK = "ok"
STATE_ERROR = "error"


class ServiceState:
    """最近一次真实模型调用的结果（进程内，三态）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: str = STATE_UNKNOWN
        self._updated_at: float | None = None
        self._identity: tuple = ()
        self._elapsed_ms: int | None = None
        self._detail: str | None = None

    # ---------- 写 ----------
    def record(self, state: str, *, identity: tuple, elapsed_ms: int | None = None,
               detail: str | None = None) -> None:
        """记一次真实调用的结果。identity = (provider, base_url, model)。"""
        if state not in (STATE_UNKNOWN, STATE_OK, STATE_ERROR):
            raise ValueError(f"非法状态：{state}")
        with self._lock:
            self._state = state
            self._updated_at = time.time()
            self._identity = tuple(identity)
            self._elapsed_ms = elapsed_ms
            self._detail = detail

    def reset(self, *, detail: str | None = None) -> None:
        """模型设置变了（或明确要求）→ 回到 unknown。"""
        with self._lock:
            self._state = STATE_UNKNOWN
            self._updated_at = time.time()
            self._identity = ()
            self._elapsed_ms = None
            self._detail = detail

    # ---------- 读 ----------
    def snapshot(self, identity: tuple) -> dict:
        """读当前状态。identity 对不上 → 一律当 unknown（不泄漏旧模型的状态）。"""
        want = tuple(identity)
        with self._lock:
            if self._state == STATE_UNKNOWN:
                return {
                    "state": STATE_UNKNOWN,
                    "updated_at": self._updated_at,
                    "provider": None, "base_url": None, "model": None,
                    "elapsed_ms": None, "detail": self._detail,
                }
            if self._identity != want:
                # 换过配置了：旧结果不代表现在这台模型
                return {
                    "state": STATE_UNKNOWN,
                    "updated_at": self._updated_at,
                    "provider": None, "base_url": None, "model": None,
                    "elapsed_ms": None,
                    "detail": "模型设置已变更，等待下一次真实调用",
                }
            provider, base_url, model = self._identity
            return {
                "state": self._state,
                "updated_at": self._updated_at,
                "provider": provider, "base_url": base_url, "model": model,
                "elapsed_ms": self._elapsed_ms, "detail": self._detail,
            }


# 进程内单例：Core 只有一份模型配置，跨模块共享同一个状态。
_DEFAULT = ServiceState()


def default_service_state() -> ServiceState:
    return _DEFAULT


def identity_of(model) -> tuple:
    """从适配器（或 ModelRegistry 门面）取出身份三元组，不含 Key。"""
    cur = getattr(model, "current", model)      # ModelRegistry 转发到当前 Provider
    provider = getattr(cur, "name", None) or getattr(model, "name", "unknown")
    base_url = (getattr(cur, "base_url", None)
                or getattr(cur, "url", None) or "")
    name = getattr(cur, "model", None) or getattr(model, "model", "") or ""
    return (str(provider), str(base_url), str(name))