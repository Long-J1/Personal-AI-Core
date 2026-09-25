"""测试辅助：时间构造、测试图片生成。"""
from __future__ import annotations

from datetime import datetime, timedelta


def now_() -> datetime:
    return datetime.now().astimezone()


def minutes_ago(n: int) -> datetime:
    return now_() - timedelta(minutes=n)


def days_ago(n: int) -> datetime:
    return now_() - timedelta(days=n)


def today_sometime() -> datetime:
    """"今天"的某个过去时刻（跨零点也安全）。"""
    n = now_()
    today0 = n.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = n - timedelta(minutes=30)
    if candidate.date() != n.date() or candidate <= today0:
        candidate = today0 + timedelta(minutes=1)
    if candidate >= n:
        candidate = n - timedelta(seconds=5)
    return candidate


def yesterday_pm() -> datetime:
    """昨天下午 3 点（必然是过去、必然在"昨天"窗口内）。"""
    n = now_()
    today0 = n.replace(hour=0, minute=0, second=0, microsecond=0)
    return (today0 - timedelta(days=1)).replace(hour=15)


def resolve_when(kind: str) -> datetime:
    table = {
        "recent": lambda: minutes_ago(5),
        "today": today_sometime,
        "yesterday": yesterday_pm,
        "3days": lambda: days_ago(3),
    }
    if kind not in table:
        raise ValueError(f"未知时间类型：{kind}")
    return table[kind]()


def make_image(label: str = "TEST", color: tuple[int, int, int] = (46, 96, 160)) -> bytes:
    """生成一张测试图（纯色 + 文字），字节可复现。"""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (480, 320), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, 471, 311], outline=(255, 255, 255), width=3)
    draw.text((24, 140), label[:24], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
