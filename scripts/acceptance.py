"""验收演示：对运行中的服务走一遍完整闭环（真模型）。

用法：python run.py 后另开终端
    python scripts/acceptance.py
"""
from __future__ import annotations

import base64
import io
import json
import sys
import time
from pathlib import Path

# Windows GBK 控制台下避免 emoji/中文报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

BASE = "http://127.0.0.1:8000"


def make_demo_image(label: str, color: tuple[int, int, int]) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (640, 420), color)
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 619, 399], outline=(255, 255, 255), width=4)
    d.text((60, 190), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    step = 0

    def ok(msg: str) -> None:
        nonlocal step
        step += 1
        print(f"[{step:02d}] ✅ {msg}")

    def fail(msg: str) -> None:
        print(f"[FAIL] ❌ {msg}")
        sys.exit(1)

    # 1. 状态
    r = httpx.get(f"{BASE}/api/status", timeout=10)
    if r.status_code != 200:
        fail(f"服务未启动（{r.status_code}）")
    s = r.json()
    ok(f"服务在线：v{s['version']} 模型={s['model'][:40]}… 服务可用={s['model_service_ok']}")

    # 2. 观察 A（真实调用 Ollama，可能要等 20-60 秒）
    print("      观察图A：模型理解中…")
    img_a = base64.b64encode(make_demo_image("SCENE-A: desk with laptop", (40, 70, 140))).decode()
    t0 = time.time()
    r = httpx.post(f"{BASE}/api/observe", json={"image_b64": img_a, "source": "upload"}, timeout=300)
    d = r.json()
    if not (d.get("ok") and d.get("stored")):
        fail(f"观察A失败：{d}")
    ok(f"观察A → 事件：{d['event']['description'][:50]}（场景={d['event']['scene']}，"
       f"重要度={d['event']['importance']}，耗时={d['duration_s']}s）")

    # 3. 观察 B
    print("      观察图B：模型理解中…")
    img_b = base64.b64encode(make_demo_image("SCENE-B: kitchen cooking", (150, 90, 40))).decode()
    r = httpx.post(f"{BASE}/api/observe", json={"image_b64": img_b, "source": "camera"}, timeout=300)
    d = r.json()
    if not (d.get("ok") and d.get("stored")):
        fail(f"观察B失败：{d}")
    ok(f"观察B → 事件：{d['event']['description'][:50]}")

    # 4. 记忆列表
    r = httpx.get(f"{BASE}/api/memories", timeout=10)
    items = r.json()["items"]
    if len(items) < 2:
        fail(f"记忆数量不足：{len(items)}")
    ok(f"记忆库可查询，共 {r.json()['total']} 条")

    # 5. 回忆
    r = httpx.post(f"{BASE}/api/chat", json={"question": "刚才我在干什么"}, timeout=300)
    c = r.json()
    if not c["answer"] or not c["sources"]:
        fail(f"回忆失败：{c}")
    ok(f"回忆：“刚才我在干什么” → {c['answer'][:80]}…")
    print(f"      依据：{[s['time'] for s in c['sources']]}  窗口={c['window_label']}")

    # 6. 导出
    r = httpx.get(f"{BASE}/api/memories/export", timeout=10)
    data = r.json()
    if data["counts"]["events"] < 2:
        fail("导出数据不完整")
    out = Path(__file__).resolve().parent.parent / "data" / "export_demo.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    ok(f"导出 JSON → {out.name}（{data['counts']}）")

    # 7. 删除单条
    victim = items[-1]["id"]
    r = httpx.delete(f"{BASE}/api/memories/{victim}", timeout=10)
    if not r.json().get("found"):
        fail("删除单条失败")
    ok("删除单条记忆成功")

    # 8. 隐私暂停验证
    httpx.post(f"{BASE}/api/privacy", json={"paused": True}, timeout=10)
    r = httpx.post(f"{BASE}/api/observe", json={"image_b64": img_a}, timeout=300)
    if r.json().get("stored"):
        fail("暂停状态竟然存了记忆！")
    httpx.post(f"{BASE}/api/privacy", json={"paused": False}, timeout=10)
    ok("隐私暂停生效：暂停期间不调用模型、不记录")

    print("\n全部验收步骤通过 🎉")
    return 0


if __name__ == "__main__":
    sys.exit(main())
