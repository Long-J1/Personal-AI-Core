"""摄像头抓帧（尽力而为：失败给出可操作的提示，不阻塞主流程）。"""
from __future__ import annotations

import io
import logging

from .base import CameraUnavailableError

log = logging.getLogger("perception.camera")


def capture(index: int = 0) -> bytes:
    """抓一帧 → JPEG 字节。打不开设备抛 CameraUnavailableError（含修复提示）。"""
    try:
        import cv2
    except ImportError as exc:
        raise CameraUnavailableError(
            "未安装 opencv-python，无法使用服务器端摄像头（可用图片上传替代）"
        ) from exc

    tried: list[int] = []
    for idx in (index, 0, 1, 2):
        if idx in tried:
            continue
        tried.append(idx)
        cap = cv2.VideoCapture(idx, cv2.CAP_ANY)
        try:
            if not cap.isOpened():
                continue
            ok, frame = cap.read()
            if ok and frame is not None and frame.size > 0:
                ok2, buf = cv2.imencode(".jpg", frame)
                if ok2:
                    log.info("摄像头抓帧成功 index=%s", idx)
                    return buf.tobytes()
        finally:
            cap.release()

    log.warning("摄像头不可用（尝试过 index=%s）", tried)
    raise CameraUnavailableError(
        f"打不开摄像头（尝试了 index={tried}，0 个可用设备）"
    )
