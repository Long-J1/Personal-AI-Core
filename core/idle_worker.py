"""Idle Background Reasoning（V0.5.9）。

用户停止操作之后，Core 在**本地**做一次后台整理：扫事件、提候选、
淘汰长期没人理的候选、把证据够的候选升级成正式理解。

四条硬边界，全部由代码保证，不靠自觉：

1. **不打断用户。** 这里只读事件、写候选——不发消息、不提问、
   不弹通知、不改聊天风格。沉默是产品原则（V0.4「搭便车不拦路」），
   本模块对那条原则零例外。
2. **不调云端模型。** 全部是本地确定性规则；跑在 `asyncio.to_thread`
   里，事件循环不会被占住，chat / streaming 不受影响。
3. **有冷却。** 空闲时间不到不跑，跑完一次也要隔一段时间才可能再跑。
   不是"用户每停一次就整理一次"。
4. **能取消。** `stop()` 取消任务，`conversation.shutdown()` 与
   FastAPI shutdown 都会调它——不留孤儿任务。

刻意不引入定时器框架（`threading.Timer` / APScheduler 之类）：
一个 asyncio task + `sleep` 就够，而且能被 `cancel()` 干净地收掉。
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("core.idle_worker")

# 用户"停止操作"多久之后才考虑整理。设得保守：
# 人在思考、在写东西、在切窗口——都不该被判定成"闲着"。
IDLE_MINUTES = 30
# 两次整理之间的最小间隔（防止长期闲置时反复空转）
COOLDOWN_MINUTES = 60
# 轮询间隔。取 60s 是"反应慢一点没关系，但不能烧 CPU"的折中。
POLL_SECONDS = 60.0


class IdleWorker:
    """一个可取消的后台整理循环。

    只用 store / bridge，不碰 conversation——这样它与聊天主链完全解耦，
    聊天侧出问题也不会牵连它，反之亦然。
    """

    def __init__(self, memory_store, hyp_store, chat_store=None, *,
                 idle_seconds: float | None = None,
                 cooldown_seconds: float | None = None,
                 poll_seconds: float = POLL_SECONDS) -> None:
        self.store = memory_store
        self.hyp = hyp_store
        self.chat = chat_store
        self.idle_seconds = (IDLE_MINUTES * 60 if idle_seconds is None
                             else idle_seconds)
        self.cooldown_seconds = (COOLDOWN_MINUTES * 60
                                 if cooldown_seconds is None
                                 else cooldown_seconds)
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._last_run = 0.0
        self._last_activity: float | None = None
        self._running = False

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """启动循环。已经在跑就什么都不做（幂等）。"""
        if self._task is not None and not self._task.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.info("idle worker 未启动：当前无事件循环")
            return False
        self._running = True
        self._task = loop.create_task(self._loop())
        log.info("idle worker 启动：空闲 %ds / 冷却 %ds / 轮询 %.0fs",
                 self.idle_seconds, self.cooldown_seconds,
                 self.poll_seconds)
        return True

    async def stop(self) -> None:
        """关闭：取消任务，不留孤儿。"""
        self._running = False
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):   # noqa: B014
            pass
        log.info("idle worker 已停止")

    # ---------- 循环 ----------
    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.poll_seconds)
                await self.tick()
            except asyncio.CancelledError:
                raise                       # 取消是正常退出路径，往上传
            except Exception as exc:
                # 整理失败只记日志：绝不能因为后台循环崩了掀翻服务
                log.warning("idle worker 本轮失败（跳过）：%s", exc)

    async def tick(self) -> dict:
        """跑一轮。测试直接调它，不经过 sleep。"""
        out = {"ran": False, "reason": "", "proposed": 0, "graduated": 0,
               "expired": 0}
        # 注意：_should_run 返回 (bool, reason) 元组。
        # 直接 `if not self._should_run():` 判的永远是元组（恒真），
        # 冷却与活跃判定会整个失效——这个坑踩过一次。
        ok, reason = self._should_run()
        if not ok:
            out["reason"] = reason
            return out
        # 线程池里跑：本地规则是同步 IO，绝不能占住事件循环
        out = await asyncio.to_thread(self._run_once)
        self._last_run = time.time()
        out["ran"] = True
        if out.get("proposed") or out.get("graduated") or out.get("expired"):
            log.info("idle 整理：候选+%d 毕业%d 淘汰%d",
                     out["proposed"], out["graduated"], out["expired"])
        return out

    # ---------- 判定 ----------
    def _should_run(self) -> tuple[bool, str]:
        if not self._running and self._task is None:
            # tick() 被测试直接调用时不要求先 start()
            pass
        try:
            if self.hyp.paused():
                return False, "隐私暂停中"
        except Exception:
            pass
        now = time.time()
        if now - self._last_run < self.cooldown_seconds:
            return False, "冷却中"
        last = self.last_activity_epoch()
        if last is None:
            # 还没有任何对话：没什么可整理的，也别制造噪声
            return False, "还没有对话记录"
        if now - last < self.idle_seconds:
            return False, "用户还在用"
        return True, "空闲够久"

    def last_activity_epoch(self) -> float | None:
        """最近一次用户活动的时间戳。取不到就返回 None。"""
        if self.chat is None:
            return self._last_activity
        try:
            ts = self.chat.last_activity_epoch()
            if ts:
                return float(ts)
        except Exception as exc:
            log.warning("读最近活动时间失败：%s", exc)
        return self._last_activity

    def note_activity(self) -> None:
        """外部（conversation）告知"用户刚说了话"。"""
        self._last_activity = time.time()

    # ---------- 真正干活的地方（同步，线程池里跑）----------
    def _run_once(self) -> dict:
        from .background_bridge import run_background_reasoning

        out = {"ran": True, "reason": "空闲够久", "proposed": 0,
               "graduated": 0, "expired": 0, "error": None}
        try:
            bg = run_background_reasoning(self.store, self.hyp)
            out["proposed"] = len(bg.get("proposed") or [])
        except Exception as exc:
            out["error"] = f"推理失败：{exc}"
        # 成长与淘汰每次都做——它们不需要新数据，只需要时间
        try:
            out["graduated"] = len(self.hyp.graduate_candidates())
        except Exception as exc:
            log.warning("idle 毕业判定失败（跳过）：%s", exc)
        try:
            out["expired"] = len(self.hyp.expire_stale_candidates())
        except Exception as exc:
            log.warning("idle 淘汰判定失败（跳过）：%s", exc)
        return out