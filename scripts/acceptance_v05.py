"""V0.5 验收：Pattern 晋升 + Goal + Reflection（HTTP 层为主，tmp 库零污染）。

用法：python scripts/acceptance_v05.py
说明：A 路径晋升的前置"2 个 confirmed 同簇假设"用直接建库做 fixture
  准备（注释标明），因为纯对话流里去重逻辑天然阻止同簇第二假设——
  这本身就是被测行为的一部分。其余全部走真实 HTTP 对话链。
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
from memory.context_store import ContextStore  # noqa: E402
from memory.hypothesis_store import HypothesisStore  # noqa: E402
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

    def chat(c, msg):
        return post(c, "/api/conversation", {"message": msg})

    with tempfile.TemporaryDirectory(prefix="pai_v05_acc_") as tmp:
        db = Path(tmp) / "acc.db"
        store = MemoryStore(db)
        texts = [
            "好的", _deposit(new=[
                {"kind": "interaction", "content": "考试前一天提醒我"},
                {"kind": "interaction", "content": "重要事情提前提醒我"}]),
            "收到", _deposit(),
            "明白", _deposit(new=[
                {"kind": "interaction", "content": "回答简短些"},
                {"kind": "interaction", "content": "简短优先"},
            ]),
            "好", _deposit(),
            "嗯", _deposit(),
            "收到", _deposit(),
            "明白", _deposit(),
            "好", _deposit(),
            "嗯", _deposit(),
            "收到", _deposit(),
            "明白", _deposit(),
            "好", _deposit(),
            "嗯", _deposit(),
            "收到", _deposit(),
            "明白", _deposit(),
            "好", _deposit(),
            "嗯", _deposit(),
            "收到", _deposit(),
            "明白", _deposit(),
        ]
        assert len(texts) == 38
        model = MockChatModel(text_queue=texts)
        c = TestClient(create_app(store=store, model=model))

        # 1. 空库状态
        s = c.get("/api/status").json()
        if c.get("/api/patterns").json()["total"] != 0:
            fail("初始规律不为空")
        if c.get("/api/goals").json()["total"] != 0:
            fail("初始目标不为空")
        ok("空库就绪")

        # 2. P0：三轮播种 6 条混杂理解 → 至少 2 个不同 scope（分裂生效）
        # 注：s2 会附带假设验证问题（预期内）；s3 用"对的"解决它
        chat(c, "考试前一天提醒我")
        r = chat(c, "考试提前三天提醒我")
        if not r.get("verification_question"):
            fail("播种轮次预期外：s2 应附带假设问题")
        r = chat(c, "对的，考试前是要提醒我的，另外说话简短点")
        if r.get("verification_question") is not None:
            fail("已解决还挂新问题")
        scopes = {i["scope"] for i in
                  c.get("/api/hypotheses").json()["items"]}
        if len(scopes) < 2:
            fail(f"P0 未分裂（scopes={scopes}）")
        ok(f"P0 分裂生效：scopes={sorted(scopes)}")

        # 3. B 路径：用户表达 → tentative（当轮不问）
        r = chat(c, "我发现我一到截止前就熬夜")
        if r.get("verification_question") is not None:
            fail("B 路径当轮就问（应该先建 tentative）")
        items = c.get("/api/patterns").json()["items"]
        bpat = [i for i in items if i["source"] == "user_declared"]
        if len(bpat) != 1 or bpat[0]["status"] != "tentative":
            fail(f"B 路径未建 tentative：{items}")
        bid = bpat[0]["id"]
        ok("B 路径建 tentative（当轮不问）")

        # 4. 相关轮次 → 规律验证问题（三要素）
        r = chat(c, "最近发现自己总是这样")
        vq = r.get("verification_question")
        if not vq or vq.get("kind") != "pattern":
            fail(f"B 路径未提问：{vq}")
        if vq["hypothesis_id"] != bid or "对吗？" not in vq["text"]:
            fail(f"问题内容不对：{vq}")
        ok("B 路径提问（含来源三要素）")

        # 5. 确认 → confirmed；下轮复用
        r = chat(c, "对的，就是这样")
        if r.get("verification_question") is not None:
            fail("已解决还挂新问题")
        h = c.get(f"/api/patterns/{bid}").json()["pattern"]
        if h["status"] != "confirmed":
            fail(f"确认后状态不对：{h['status']}")
        ev = [x for x in h["evidence"] if "verification" in str(x["detail"])]
        if len(ev) != 1:
            fail("确认回答未进证据链")
        r = chat(c, "截止前怎么安排")
        used = [x["id"] for x in r.get("patterns_used", [])]
        if bid not in used:
            fail("确认后的规律未参与后续判断")
        ok("B 路径确认链：confirmed + 入链 + 后续引用")

        # 6. A 路径：fixture 直建 2 个 confirmed 同簇假设（注释见文件头）
        hyp = HypothesisStore(db)
        ctx = ContextStore(db)
        h1, _ = hyp.create("用户接受开会提前准备甲", "interaction:开会")
        for text, org in (("开会前看材料甲", "ka:1"),
                          ("开会前列提纲甲", "ka:2")):
            e, _ = ctx.add_entry("interaction", text, source="chat")
            hyp.add_evidence(h1.id, "support", "context", e.id, origin=org)
        hyp.confirm(h1.id)
        h2, _ = hyp.create("用户接受开会提前准备乙", "interaction:开会")
        e, _ = ctx.add_entry("interaction", "开会前定目标乙", source="chat")
        hyp.add_evidence(h2.id, "support", "context", e.id, origin="ka:3")
        hyp.confirm(h2.id)
        chat(c, "随便聊聊开会的事")
        items = c.get("/api/patterns").json()["items"]
        apat = [i for i in items if i["scope"] == "interaction:开会"]
        if len(apat) != 1 or apat[0]["status"] != "tentative":
            fail(f"A 路径未晋升：{items}")
        aid = apat[0]["id"]
        if "次" in apat[0]["statement"] or "条" in apat[0]["statement"]:
            fail(f"陈述带计数：{apat[0]['statement']}")
        ok("A 路径晋升：tentative + 陈述无计数")

        # 7. A 路径提问 → 确认 → 复用
        r = chat(c, "下周开会怎么准备")
        vq = r.get("verification_question")
        if not vq or vq.get("kind") != "pattern" or vq["hypothesis_id"] != aid:
            fail(f"A 路径未提问：{vq}")
        chat(c, "没错，就是这样")
        if c.get(f"/api/patterns/{aid}").json()["pattern"]["status"] \
                != "confirmed":
            fail("A 路径确认失败")
        r = chat(c, "开会前还要做什么")
        if aid not in [x["id"] for x in r.get("patterns_used", [])]:
            fail("晋升规律未参与后续判断")
        ok("A 路径确认链全通")

        # 8. Goal：表达 → proposed → 提问 → active → 关联
        r = chat(c, "我的目标是CSP提高组通过")
        if r.get("verification_question") is not None:
            fail("Goal 表达当轮就问")
        goals = c.get("/api/goals").json()["items"]
        if len(goals) != 1 or goals[0]["status"] != "proposed":
            fail(f"Goal 未建 proposed：{goals}")
        gid = goals[0]["id"]
        r = chat(c, "CSP备考怎么安排")
        vq = r.get("verification_question")
        if not vq or vq.get("kind") != "goal":
            fail(f"Goal 未提问：{vq}")
        chat(c, "没错，记上")
        g = c.get(f"/api/goals/{gid}").json()["goal"]
        if g["status"] != "active":
            fail(f"Goal 确认失败：{g['status']}")
        chat(c, "下周怎么学")
        ok("Goal 链：proposed → 提问 → active")

        # 9. 否决链（新主题，免串扰）
        chat(c, "我总是提前半小时到")
        r = chat(c, "我总是提前到，开会也是")
        vq = r.get("verification_question")
        if not vq or vq.get("kind") != "pattern":
            fail(f"否决主题未提问：{vq}")
        did = vq["hypothesis_id"]
        chat(c, "不对")
        h = c.get(f"/api/patterns/{did}").json()["pattern"]
        if h["status"] != "rejected" or h["confidence"] != 0.25:
            fail(f"否决链不对：{h['status']}/{h['confidence']}")
        ok("否决链：rejected + 0.25")

        # 10. 沉默 + 周报 + 无主动设施
        r = chat(c, "今天天气不错")
        if r.get("verification_question") is not None:
            fail("不相关话题也提问")
        body = c.get("/api/reflection/weekly").json()
        if not body.get("ok") or len(body.get("lines", [])) != 3:
            fail(f"周报不对：{body}")
        if body["stats"].get("pat_confirmed", 0) < 2:
            fail(f"周报确认数不对：{body['stats']}")
        html = c.get("/").text
        if "过去7天" not in html or "weeklyList" not in html:
            fail("周报卡片缺失")
        import re
        srcs = {f: (ROOT / f).read_text(encoding="utf-8") for f in
                ("core/verifier.py", "core/reflection.py",
                 "core/pattern_engine.py", "core/conversation.py",
                 "core/inducer.py")}
        for f, t in srcs.items():
            for bad in ("threading.Timer", "setInterval", "toast", "push(",
                        "Notification", "schedule", "notify("):
                if bad not in t:
                    continue
                # "schedule" 作子串会误伤 V0.5.5 性能改造引入的
                # _schedule_deposit——那是"用户发消息后才启动"的后台任务
                # 调度器，与本步要禁止的主动设施（定时器/通知）正好相反。
                # 这里只豁免这一个方法名，其余任何 schedule 仍然判失败。
                if bad == "schedule":
                    residue = t.replace("_schedule_deposit", "")
                    if "schedule" not in residue:
                        continue
                fail(f"{f} 含主动设施痕迹：{bad}")
        ok("沉默保持 + 周报 + 无主动设施")

    print("\nV0.5 全部验收步骤通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
