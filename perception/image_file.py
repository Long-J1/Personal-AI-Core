"""图片输入：校验、EXIF 矫正、过大自动缩小（保证后续管线拿到统一格式）。"""
from __future__ import annotations

import io
import logging

from .base import PerceptionError

log = logging.getLogger("perception.image")

MAX_SIDE = 1600  # 输入图最长边，控制内存与 base64 体积


def normalize_image(data: bytes, max_side: int = MAX_SIDE) -> bytes:
    """任意常见图片格式 → 规范 JPEG 字节。失败抛 PerceptionError。"""
    if not data:
        raise PerceptionError("图片数据为空")
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img) or img
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=88)
        return out.getvalue()
    except PerceptionError:
        raise
    except Exception as exc:
        log.warning("图片解析失败：%s", exc)
        raise PerceptionError(f"不是有效的图片文件：{exc}") from exc
