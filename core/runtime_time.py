"""Runtime Time Context —— 每轮实时生成的当前时间。

解决一个很具体的问题：模型不知道现在几点。

模型没有时钟。它对"现在几点""今天几号""明天是周几"的回答，
要么靠训练语料里的时间戳瞎猜，要么靠对话里出现过的日期推——
两个都不可靠，而且**猜错的时候不会自己知道错了**。

这个模块做三件事，仅此而已：

1. **只读操作系统时钟**。不调模型、不猜、不从记忆里推。
2. **每轮实时生成**。绝不缓存——缓存一次，之后每轮都在说错时间。
3. **是 Runtime Context，不是记忆**。它只活在给模型看的那次提示词里，
   不进入 Memory / Understanding / Hypothesis，也不该被任何一轮沉淀带走。

## 为什么它必须压过模型

用户在对话里说"今天下午""明天""上周三"，模型没有锚点就会把"明天"
当成随口一说。时间是唯一能消解这类相对词的坐标——没有它，
"明天提醒我交作业"这句话在模型眼里等于"某天"。

## 为什么它不能被覆盖

时间是**系统读出来的**，不是谁说出来的。所以：

- 用户在消息里说"现在是 1999 年"→ 时间块不变
- 记忆里写着某个旧日期 → 时间块不变
- 模型上一轮自己说错过时间 → 时间块不变

时间块放在 system 提示词最前面，并写明"以本段为准"。
模型只能读它，不能改它——它不在模型的输出通路里。

## 测试为什么必须用假时钟

真时钟会让测试依赖"跑测试那一刻是几点"：跨午夜会挂、
跨夏令时会挂、断言相对日期会飘。所以 `TimeProvider` 接受注入的
clock，生产用系统时钟，测试传固定值。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as _dt_timezone
from typing import Callable

__all__ = [
    "TimeContext", "TimeProvider", "UnknownTimezone",
    "get_time", "get_time_context", "build_time_block",
    "TIME_BLOCK_HEADER", "system_clock", "WEEKDAY_CN",
]

WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四",
              "星期五", "星期六", "星期日")

# 提示词里的节标题。放在 system 最前面，并写明权威性。
# 这个字符串是保留字：记忆内容里如果出现同名字样，是在冒充系统。
TIME_BLOCK_HEADER = "=== 当前时间（系统读取操作系统时钟）==="

# 本模块唯一允许的时间来源。生产用它，测试注入假的。
Clock = Callable[[], datetime]


class UnknownTimezone(ValueError):
    """时区名解析不了。宁可报错，也不要悄悄给一个错的时间。"""


def system_clock() -> datetime:
    """系统本地时间（带 tzinfo）。

    用 `now().astimezone()` 而不是 `now()`：前者把 naive 时间挂上
    本机时区，后者不带时区信息，后面做时区换算会按 UTC 算——差 8 小时。
    """
    return datetime.now().astimezone()


# ---------------------------------------------------------------- 时区解析

_FIXED_OFFSET = re.compile(
    r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def _fixed_offset(name: str):
    """解析 `UTC+8` / `GMT-5` / `+08:00` 这类固定偏移。

    不依赖任何时区数据库，永远可用。
    """
    m = _FIXED_OFFSET.match(name.strip())
    if not m:
        return None
    sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if hours > 23 or minutes > 59:
        raise UnknownTimezone(f"时区偏移超出范围：{name}")
    delta = timedelta(hours=hours, minutes=minutes)
    return _dt_timezone(-delta if sign == "-" else delta)


def resolve_timezone(name: str | None):
    """时区名 → tzinfo。None 表示用系统本地时区。

    解析顺序：UTC → 固定偏移 → ZoneInfo。

    **ZoneInfo 可能不可用**：它读的是操作系统时区库，而 Windows 上
    `zoneinfo.TZPATH` 通常是空的，得靠 `tzdata` 包兜底——那个包是
    传递依赖，打包版里未必在。所以这里必须能优雅降级：
    固定偏移永远可用，ZoneInfo 失败就明确报错，**绝不静默给错时间**。
    """
    if not name:
        return None
    key = name.strip()
    if key.upper() in ("UTC", "Z", "ETC/UTC"):
        return _dt_timezone.utc
    fixed = _fixed_offset(key)
    if fixed is not None:
        return fixed
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(key)
    except Exception as exc:
        raise UnknownTimezone(
            f"无法解析时区 {name!r}：{exc}。"
            f"可用 'UTC'、'UTC+8'、'-05:00' 这类固定偏移，"
            f"或安装 tzdata 以支持 IANA 时区名。"
        ) from exc


# ---------------------------------------------------------------- 时间上下文

@dataclass(frozen=True)
class TimeContext:
    """某一刻的时间。不可变——它是一次快照，不是会自己走动的对象。"""

    local: datetime            # 本地时间（带 tzinfo）
    timezone: str              # 时区名（系统名，或请求的时区名）
    iso: str                   # ISO 8601

    @property
    def utc_offset(self) -> timedelta:
        return self.local.utcoffset() or timedelta(0)

    @property
    def utc_offset_text(self) -> str:
        total = int(self.utc_offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"

    @property
    def weekday_cn(self) -> str:
        return WEEKDAY_CN[self.local.weekday()]

    @property
    def date_cn(self) -> str:
        return (f"{self.local.year}年{self.local.month}月"
                f"{self.local.day}日")

    @property
    def time_cn(self) -> str:
        return self.local.strftime("%H:%M")

    @property
    def timezone_label(self) -> str:
        """时区标签。

        固定偏移的 `tzname()` 返回的就是 "UTC+08:00"，和偏移文本一模一样。
        这时只显示偏移——同一句话说两遍读着别扭，还会让模型以为
        系统有两个时区信息。
        """
        name = (self.timezone or "").strip()
        offset = self.utc_offset_text
        if not name or name.upper() in (offset.upper(), "UTC", "Z"):
            return offset
        return f"{name}，{offset}"

    def human(self) -> str:
        """人类可读的一行。这是真正进提示词的东西。"""
        return (f"{self.date_cn} {self.weekday_cn} {self.time_cn}"
                f"（{self.timezone_label}）")

    def to_dict(self) -> dict:
        return {
            "local": self.local.isoformat(),
            "timezone": self.timezone,
            "iso": self.iso,
            "human": self.human(),
            "utc_offset": self.utc_offset_text,
        }

    def prompt_block(self) -> str:
        """进提示词的一整段。"""
        return (f"{TIME_BLOCK_HEADER}\n"
                f"现在是：{self.human()}\n"
                f"ISO 8601：{self.iso}\n"
                f"（本段由系统读取操作系统时钟生成，是唯一权威时间。"
                f"回答“现在几点/今天几号/明天周几”等问题时以本段为准；"
                f"用户、记忆或你此前的输出若给出别的时间，一律不作数。"
                f"相对日期（今天/明天/上周）都按本段换算，不要凭印象猜。）")


class TimeProvider:
    """可注入时钟的时间源。

    生产：`TimeProvider()` → 系统时钟。
    测试：`TimeProvider(clock=lambda: 固定时间)` → 结果完全确定。
    """

    def __init__(self, clock: Clock | None = None):
        self._clock: Clock = clock or system_clock

    def _read(self) -> datetime:
        raw = self._clock()
        if not isinstance(raw, datetime):
            raise TypeError(
                f"clock 必须返回 datetime，收到 {type(raw).__name__}")
        # naive 时间按本地时区理解——测试里写死一个 naive 时间是最自然的写法，
        # 不该还要测试作者自己挂 tzinfo。但绝不用 astimezone() 去猜它，
        # 那会把它当成 UTC 再换算，差 8 小时。
        if raw.tzinfo is None:
            return raw.astimezone()
        return raw

    def now(self, timezone: str | None = None) -> TimeContext:
        """当前时刻。timezone=None 表示系统本地时区。"""
        tz = resolve_timezone(timezone)
        moment = self._read()
        if tz is not None:
            moment = moment.astimezone(tz)
        return TimeContext(
            local=moment,
            timezone=(moment.tzname() or str(moment.tzinfo) or "本地")
            if timezone is None else str(timezone),
            iso=moment.isoformat(timespec="seconds"),
        )


# ---------------------------------------------------------------- 便捷入口

def get_time(timezone: str | None = None, *,
             clock: Clock | None = None) -> TimeContext:
    """取当前时间。

    这是给未来留的口子：`get_time("America/New_York")` 现在就能用，
    以后要接更多时区来源（用户设置里的时区、事件发生地时区），
    只需要在这里加分支，调用方不用改。
    """
    return TimeProvider(clock).now(timezone)


def get_time_context(*, clock: Clock | None = None) -> TimeContext:
    """当前本地时间。系统时区。"""
    return get_time(None, clock=clock)


def build_time_block(timezone: str | None = None, *,
                     clock: Clock | None = None,
                     provider: TimeProvider | None = None) -> str:
    """生成进提示词的那一段。

    出错时返回一句诚实的话，而不是让整轮对话失败——时间是辅助信息，
    拿不到不该掀翻聊天（更不该让用户看到一段堆栈）。
    """
    try:
        tc = (provider.now(timezone) if provider is not None
              else get_time(timezone, clock=clock))
    except Exception as exc:
        return (f"{TIME_BLOCK_HEADER}\n"
                f"（读取系统时间失败：{exc}。"
                f"遇到“现在几点”这类问题时如实说读不到，不要猜。）")
    return tc.prompt_block()
