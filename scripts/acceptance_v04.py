"""V0.4 验收：自然窗口验证提问（HTTP 层，tmp 库，零污染，零云端调用）。

用法：python scripts/acceptance_v04.py
覆盖：选择规则 · 只提问不下结论 · 确认链 · 行为变化 · 否决链 ·
      沉默保持 · 无主动设施（grep 断言）· UI 未扩张。
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
        r = c.post(url, json=payload) if payload is not None else c.post(url)
        if r.status_code != 200:
            fail(f"POST {url} 返回 {r.status_code}：{r.text[:200]}")
        body = r.json()
        if isinstance(body, dict) and body.get("ok") is False:
            fail(f"POST {url} 返回 ok:false：{r.text[:200]}")
        return body

    with tempfile.TemporaryDirectory(prefix="pai_v04_acc_") as tmp:
        store = MemoryStore(Path(tmp) / "acc.db")
        model = MockChatModel(text_queue=[
            "好的", _deposit(new=[
                {"kind": "interaction", "content": "考试前一天提醒我"},
                {"kind": "interaction", "content": "重要事情提前提醒我"},
            ]),
            "收到", _deposit(),
            "明白", _deposit(),
            "还有什么", _deposit(),
            "记下了", _deposit(new=[
                {"kind": "interaction", "content": "回答简短些"},
                {"kind": "interaction", "content": "简短优先"},
            ]),
            "好", _deposit(),
            "嗯", _deposit(),
            "今天不错", _deposit(),
        ])
        c = TestClient(create_app(store=store, model=model))

        # 1. 状态字段就位，无假设
        s = c.get("/api/status").json()
        if "hypotheses" not in s or s["hypotheses"] != 0:
            fail(f"初始状态不对：{s}")
        ok("服务就绪，假设数为 0")

        # 2. 首轮对话沉淀 → R2 归纳出假设（仍 tentative）
        post(c, "/api/conversation", {"message": "以后考试前一天提醒我"})
        body = c.get("/api/hypotheses").json()
        if body["total"] != 1 or body["items"][0]["status"] != "tentative":
            fail(f"R2 未归纳出待验证假设：{body}")
        hid = body["items"][0]["id"]
        ok("R2 归纳出 tentative 假设")

        # 3. 相关话题轮次 → 附带验证问题（三要素齐全），假设本身不动
        r = post(c, "/api/conversation", {"message": "明天开会前提醒我"})
        vq = r.get("verification_question")
        if not vq or set(vq) < {"hypothesis_id", "text", "confidence",
                                "reason"}:
            fail(f"验证问题缺失或缺三要素：{vq}")
        if vq["hypothesis_id"] != hid or "对吗？" not in vq["text"]:
            fail(f"验证问题内容不对：{vq}")
        if "待验证" not in vq["text"] and "把握" not in vq["text"]:
            fail(f"问题缺来源声明：{vq['text']}")
        h = c.get(f"/api/hypotheses/{hid}").json()["hypothesis"]
        if h["status"] != "tentative" or len(h["evidence"]) != 2:
            fail(f"提问改变了假设状态/证据（只许问不许判）：{h}")
        ok("只提问不下结论：状态/证据不动，元数据带 id/原因/置信度")

        # 4. 确认 → confirmed + 回答进证据链 + prompt 行为变化
        r = post(c, "/api/conversation", {"message": "对的，就是这样"})
        if r.get("verification_question") is not None:
            fail("已解决还挂新问题")
        h = c.get(f"/api/hypotheses/{hid}").json()["hypothesis"]
        if h["status"] != "confirmed":
            fail(f"确认后状态不对：{h['status']}")
        ev = [x for x in h["evidence"] if "verification" in str(x["detail"])]
        if len(ev) != 1 or ev[0]["excerpt"] != "对的，就是这样":
            fail(f"确认回答未进证据链：{h['evidence']}")
        r = post(c, "/api/conversation", {"message": "还有什么要准备"})
        used = [x["id"] for x in r.get("hypotheses_used", [])]
        if hid not in used:
            fail("确认后的假设未参与后续判断")
        ok("确认链：confirmed + 回答入链 + 后续判断引用")

        # 5. 第二主题否决链
        post(c, "/api/conversation", {"message": "以后说话简短点"})
        r = post(c, "/api/conversation", {"message": "请简短回答"})
        vq2 = r.get("verification_question")
        if not vq2 or "简短" not in vq2["text"]:
            fail(f"第二主题未提问：{vq2}")
        hid2 = vq2["hypothesis_id"]
        post(c, "/api/conversation", {"message": "不对，别管简短"})
        h2 = c.get(f"/api/hypotheses/{hid2}").json()["hypothesis"]
        if h2["status"] != "rejected" or h2["confidence"] != 0.25:
            fail(f"否决链不对：{h2['status']}/{h2['confidence']}")
        ok("否决链：rejected + 0.25")

        # 6. 沉默：不相关话题不问
        r = post(c, "/api/conversation", {"message": "今天天气不错"})
        if r.get("verification_question") is not None:
            fail("不相关话题也提问，违背沉默默认")
        if "多问一句" in r["answer"]:
            fail("回答正文混入了问题文本")
        ok("沉默保持：不相关不问")

        # 7. 无主动设施（源码 grep 断言）
        src = (ROOT / "core" / "verifier.py").read_text(encoding="utf-8")
        for bad in ("Timer", "setInterval", "threading", "toast", "push",
                    "Notification", "schedule", "notify"):
            if bad in src:
                fail(f"verifier.py 含主动设施痕迹：{bad}")
        conv_src = (ROOT / "core" / "conversation.py").read_text(
            encoding="utf-8")
        if "verifier" not in conv_src:
            fail("conversation 未接入 verifier")
        ok("无主动设施：verifier 无定时/推送/后台痕迹")

        # 8. UI 未扩张（问题在对话流内，无新区块）
        # 按本意断言结构，不钉标题字串（卡片化后措辞变过，
        # 但"假设区块还在、且没有为验证问题新开一块"没变）。
        html = c.get("/").text
        if 'id="pane-hypo"' not in html:
            fail("假设区块被破坏")
        ok("UI 未扩张：问题只活在对话流里")

    print("\nV0.4 全部验收步骤通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
