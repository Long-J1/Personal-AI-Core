"""V0.3 验收：Hypotheses 全链路（HTTP 层，tmp 库，零污染）。

用法：python scripts/acceptance_v03.py
不碰真实数据库（自建 tmp 库 + Mock 模型，走真实 FastAPI 层）。

覆盖：H1 数学（经 API 可见）· H2 归纳（R1+R2）· H3 反证衰减 ·
      H4 纠正联动 · H5 API 全链 · UI 区块存在。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Windows GBK 控制台下避免 emoji/中文报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from interfaces.webapp import create_app  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from models.mock_adapter import MockChatModel  # noqa: E402

_UNDERSTANDING = json.dumps(
    {"scene": "书房", "activity": "写作业", "objects": ["作业本"],
     "description": "用户在写数学作业", "importance": 0.6, "facts": []},
    ensure_ascii=False,
)


def _deposit(new=None, corrections=None):
    return json.dumps({"new": new or [], "corrections": corrections or [],
                       "event": None}, ensure_ascii=False)


def main() -> int:
    step = 0

    def ok(msg: str) -> None:
        nonlocal step
        step += 1
        print(f"[{step:02d}] OK {msg}")

    def fail(msg: str) -> None:
        print(f"[FAIL] {msg}")
        sys.exit(1)

    def post(c, url, payload=None):
        """POST 必须 200 且 ok 不为 False（坏行为不许悄悄溜过）."""
        r = c.post(url, json=payload) if payload is not None else c.post(url)
        if r.status_code != 200:
            fail(f"POST {url} 返回 {r.status_code}：{r.text[:200]}")
        body = r.json()
        if isinstance(body, dict) and body.get("ok") is False:
            fail(f"POST {url} 返回 ok:false：{r.text[:200]}")
        return body

    with tempfile.TemporaryDirectory(prefix="pai_v03_acc_") as tmp:
        store = MemoryStore(Path(tmp) / "acc.db")
        model = MockChatModel(
            understanding_queue=[_UNDERSTANDING] * 3,
            text_queue=[
                "好的",
                _deposit(new=[
                    {"kind": "interaction", "content": "考试前一天提醒我"},
                    {"kind": "interaction", "content": "重要事情提前提醒我"},
                ]),
                "收到", _deposit(),
                "学得不错", _deposit(),
            ],
        )
        c = TestClient(create_app(store=store, model=model))

        # 1. 状态字段就位
        s = c.get("/api/status").json()
        if "hypotheses" not in s:
            fail("状态缺少 hypotheses 字段（服务是旧版？）")
        ok(f"服务就绪：v{s['version']} 假设={s['hypotheses']}")

        # 2. 两轮对话沉淀出 2 条 interaction → R2 归纳出假设
        post(c, "/api/conversation", {"message": "以后考试前一天提醒我"})
        post(c, "/api/conversation", {"message": "重要事情也要提前提醒"})
        body = c.get("/api/hypotheses").json()
        if body["total"] != 1:
            fail(f"R2 未归纳出假设：{body}")
        h = body["items"][0]
        if h["scope"] != "interaction:提醒我" or h["status"] != "tentative":
            fail(f"假设内容不对：{h}")
        ok(f"H2-R2 归纳：scope={h['scope']} confidence={h['confidence']}")

        # 3. R1：3 次同类观察 → 活动假设（0.74，3 独立源不 cap）
        for _ in range(3):
            import base64
            from tests.helpers import make_image
            img = base64.b64encode(make_image("ACC")).decode("ascii")
            r = post(c, "/api/observe", {"image_b64": img, "source": "upload"})
            if not r.get("stored"):
                fail(f"观察未入库：{r}")
        post(c, "/api/conversation", {"message": "今天学得怎么样"})
        items = c.get("/api/hypotheses").json()["items"]
        r1 = [i for i in items if i["scope"] == "activity:写作业"]
        if not r1:
            fail(f"R1 未归纳出活动假设：{[i['scope'] for i in items]}")
        if r1[0]["confidence"] != 0.74:
            fail(f"R1 置信度不对（应 0.74）：{r1[0]}")
        ok(f"H2-R1 归纳：scope=activity:写作业 confidence=0.74")

        # 4. 证据可追溯
        hid = r1[0]["id"]
        d = c.get(f"/api/hypotheses/{hid}").json()["hypothesis"]
        if len(d["evidence"]) != 3 or d["independent_sources"] != 3:
            fail(f"证据追溯不对：{d['evidence']}")
        if not any(l["action"] == "create" for l in d["history"]):
            fail("缺少 create 审计记录")
        ok("H1/H3 证据可追溯：3 条事件证据 + create 审计")

        # 5. 否决：0.25 + rejected + 不再列出
        rb = post(c, f"/api/hypotheses/{hid}/reject", {"reason": "这段时间不需要"})
        if rb != {"ok": True}:
            fail(f"否决失败：{rb}")
        d2 = c.get(f"/api/hypotheses/{hid}").json()["hypothesis"]
        if d2["status"] != "rejected" or d2["confidence"] != 0.25:
            fail(f"否决后状态不对：{d2['status']}/{d2['confidence']}")
        # V0.5.8：库里可能同时存在后台推测（V0.5.8 新增），所以不能断言
        # 总数——只断言"被否决的那一条不在列表里"，那才是这一步的本意。
        listed = [i["id"] for i in c.get("/api/hypotheses").json()["items"]]
        if hid in listed:
            fail("被否决假设仍在列表里")
        ok("H4/H5 否决链：rejected + 0.25 + 移出列表")

        # 6. 确认：只改状态（另一条假设）
        other = [i for i in c.get("/api/hypotheses").json()["items"]
                 if i["id"] != hid][0]
        before = other["confidence"]
        post(c, f"/api/hypotheses/{other['id']}/confirm")
        after = c.get(f"/api/hypotheses/{other['id']}").json()["hypothesis"]
        if after["status"] != "confirmed" or after["confidence"] != before:
            fail(f"确认语义不对：{after}")
        ok("确认只改状态不动置信度")

        # 7. 删除/恢复
        #    V0.5.8：库里可能有后台推测，不再断言总数，只看这一条在不在。
        c.delete(f"/api/hypotheses/{other['id']}")
        ids = [i["id"] for i in c.get("/api/hypotheses").json()["items"]]
        if other["id"] in ids:
            fail("删除后仍可见")
        post(c, f"/api/hypotheses/{other['id']}/restore")
        ids = [i["id"] for i in c.get("/api/hypotheses").json()["items"]]
        if other["id"] not in ids:
            fail("恢复后不可见")
        ok("删除/恢复闭环")

        # 8. UI 区块存在且与④分开
        # 按本意断言结构，不钉标题字串：卡片化后标题措辞变过
        # （"待验证假设" → "这些是我的猜测"），但"独立一块、JS 接着"
        # 这两件事没变，钉字串只会在改文案时误报。
        html = c.get("/").text
        if 'id="pane-hypo"' not in html or "hypoList" not in html:
            fail("首页缺少假设区块")
        if "它对你的理解" not in html:
            fail("④信任界面被破坏")
        ok("H5-UI：假设区块独立存在，④不受影响")

    print("\nV0.3 全部验收步骤通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
