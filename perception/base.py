"""感知层：图片文件与摄像头（未来扩展麦克风/ASR 也放这里）。"""
from __future__ import annotations


class PerceptionError(Exception):
    """感知失败（带用户可读提示）。"""

    hint: str = ""


class CameraUnavailableError(PerceptionError):
    hint = (
        "摄像头不可用。常见原因：①Win11 相机隐私开关未允许桌面应用访问；"
        "②摄像头被其它程序（会议软件等）占用。"
        "请到 系统设置 → 隐私和安全性 → 相机 打开“允许桌面应用访问相机”，或改用图片上传。"
    )
