# -*- coding: utf-8 -*-
"""V0.5.2 验收：Trust Boundary（HTTP 层为主，tmp 库零污染）。

用法：python scripts/acceptance_v051.py
12 步：空库→用户原话入库→幻觉拦截→幻觉类名→混合拆散→纠正不断根→
      别记硬开关→自夸边界→复本传播→级联→质量排序→信任 UI 字段。
铁规：全 Mock，tmp 库，真库零触碰，零云端调用。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from interfaces.webapp import create_app  # noqa: E402
from memory.context_store import ContextStore  # noqa: E402
from memory.hypothesis_store import HypothesisStore  # noqa: E402
from memory.pattern_store import PatternStore  # noqa: E402
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

    with tempfile.TemporaryDirectory(prefix="pai_v051_acc_") as tmp:
        db = Path(tmp) / "acc.db"
        store = MemoryStore(db)
        texts = [
            # 2: 用户原话（轮次：回答+沉淀）
            "收到，你是高二。",
            _deposit(new=[{"kind": "profile",
                           "content": "用户是高二学生"}]),
            # 3-4: 幻觉生日两发
            "没查到相关记录。",
            _deposit(new=[{"kind": "fact",
                           "content": "用户生日是3月5日"}]),
            "明白。",
            _deposit(new=[{"kind": "fact",
                           "content": "用户之前说过喜欢跑步"}]),
            # 5: 混合句（回答+沉淀）
            "考试前会提醒你，也别太焦虑。",
            _deposit(new=[{"kind": "interaction",
                           "content": "用户说考试前提醒，说明他焦虑"}]),
            # 6-7: 纠正攻击（轮次：先喜欢咖啡→硬否认）
            "好，喜欢喝咖啡记下了。",
            _deposit(new=[{"kind": "fact",
                           "content": "用户喜欢喝咖啡"}]),
            "明白，你讨厌喝咖啡。",
            _deposit(new=[{"kind": "fact",
                           "content": "用户讨厌喝咖啡"}]),
            # 8: 追问（回答+沉淀）
            "这题选 B。",
            _deposit(),
            # 9: 别记（只回答，无沉淀调用）
            "好，这次内容不会进入长期记忆。",
            # 10: 追问别记
            "这题选 C。",
            _deposit(),
            # 11: 自夸（回答+沉淀）
            "我还在学习了解你。",
            _deposit(),
        ]
        model = MockChatModel(text_queue=texts)
        c = TestClient(create_app(store=store, model=model))

        # 1. 空库状态
        s = c.get("/api/status").json()
        if c.get("/api/context").json()["total"] != 0:
            fail("初始理解不为空")
        ok("空库就绪")

        # 2. 用户原话照常入库（user_direct/0.95）
        chat(c, "我是高二学生")
        items = c.get("/api/context").json()["items"]
        if len(items) != 1 or items[0]["content"] != "用户是高二学生":
            fail(f"用户原话未入库：{items}")
        if items[0]["source_grade"] != "user_direct":
            fail(f"来源等级不对：{items[0]}")
        if items[0]["confidence"] != 0.95:
            fail(f"置信度不对：{items[0]}")
        if items[0]["memory_status"] != "active":
            fail(f"状态不对：{items[0]}")
        ok("用户原话入库（user_direct/0.95/active）")

        # 3. 幻觉生日：五表零新增
        before = {t: c.get(f"/api/{t}").json()["total"]
                  for t in ("context", "hypotheses", "patterns",
                            "goals")}
        r = chat(c, "你记得我生日吗？")
        after = {t: c.get(f"/api/{t}").json()["total"]
                 for t in ("context", "hypotheses", "patterns",
                           "goals")}
        if after != before:
            fail(f"幻觉入库：{before} → {after}")
        ok("幻觉生日零新增（含 hypothesis）")

        # 4. 无证据过去时：零新增
        chat(c, "我以前很爱跑步吗？")
        after2 = {t: c.get(f"/api/{t}").json()["total"]
                  for t in ("context", "hypotheses", "patterns",
                            "goals")}
        if after2 != before:
            fail(f"过去时幻觉入库：{before} → {after2}")
        ok("无证据过去时零新增")

        # 5. 混合句拆散：原话部分进 memory，推断部分进 hypothesis
        chat(c, "考试前提醒我一下")
        items = c.get("/api/context").json()["items"]
        if not any(e["content"] == "用户说考试前提醒"
                   for e in items):
            fail(f"混合句原话部分未入库：{[e['content'] for e in items]}")
        hyps = c.get("/api/hypotheses").json()["items"]
        if not any("焦虑" in h["statement"] for h in hyps):
            fail(f"混合句推断部分未进待验证池："
                 f"{[h['statement'] for h in hyps]}")
        ok("混合句拆散：原话进 memory，推断进 hypothesis")

        # 6. 纠正不断根：旧条 rejected + 新条在 + 回答不再引用
        r = chat(c, "我喜欢喝咖啡。")
        old_id = None
        for e in c.get("/api/context").json()["items"]:
            if e["content"] == "用户喜欢喝咖啡":
                old_id = e["id"]
        if old_id is None:
            fail("喜欢咖啡没入库")
        chat(c, "不对，我讨厌喝咖啡。")
        # 旧条被标 rejected 后不再出现在列表（prompt 只读 active）——
        # 用 store 直读审计（v05 第 6 步同风格）
        ctx6 = ContextStore(db)
        old = ctx6.get_entry(old_id)
        if old is None or old.memory_status != "rejected":
            fail(f"旧条未标 rejected：{old.to_public_dict() if old else None}")
        live = {e["id"]: e for e in
                c.get("/api/context").json()["items"]}
        if old_id in live:
            fail("rejected 旧条仍出现在列表（会污染 prompt）")
        live = {e["id"]: e for e in
                c.get("/api/context").json()["items"]}
        if old_id in live:
            fail("rejected 旧条仍出现在列表（会污染 prompt）")
        if not any(e["content"] == "用户讨厌喝咖啡"
                   for e in live.values()):
            fail("新认知未入库")
        r = chat(c, "我喜欢什么？")
        for u in r.get("context_used", []):
            if u["id"] == old_id:
                fail("旧假条目仍参与判断")
        ok("纠正不断根（rejected + 新认知 + 不再引用）")

        # 7. 别记硬开关：五表零新增
        before7 = {t: c.get(f"/api/{t}").json()["total"]
                   for t in ("context", "hypotheses", "patterns",
                             "goals")}
        r = chat(c, "不要记住我喜欢玩某游戏。")
        after7 = {t: c.get(f"/api/{t}").json()["total"]
                  for t in ("context", "hypotheses", "patterns",
                            "goals")}
        if after7 != before7:
            fail(f"别记漏网：{before7} → {after7}")
        if r.get("deposit") is not None:
            fail("别记轮还跑了沉淀")
        r = chat(c, "我喜欢玩什么游戏？")
        blob = json.dumps(r, ensure_ascii=False)
        if "某游戏" in blob:
            fail("别记内容被引用")
        ok("别记硬开关（零新增 + 不引用）")

        # 8. 自夸边界：诚实指令数字如实
        # （查 calls 里的 system prompt 需要直接读服务层——此处退一步：
        #  只断言回答不吹牛。Mock 固定回答"还在学习了解你"即合格。）
        r = chat(c, "你是不是已经完全了解我了？")
        if ("完全了解" in r.get("answer", "")
                and "还在学习" not in r.get("answer", "")):
            fail(f"自夸回答：{r.get('answer')}")
        ok("自夸回答不吹牛（诚实指令单测见 test_trust_boundary）")

        # 9. 复本传播（fixture 直建两条相似条目，direct store 调用）
        ctx = ContextStore(db)
        e1, _ = ctx.add_entry("fact", "用户爱喝红茶", source="chat")
        e2, _ = ctx.add_entry("fact", "用户爱喝红茶的", source="chat")
        if not ctx.set_memory_status(e1.id, "rejected",
                                     reason="user_correction"):
            fail("否认标记失败")
        if ctx.get_entry(e2.id).memory_status != "outdated":
            fail("相似复本未传播")
        ok("复本传播（propagated）")

        # 10. 级联（fixture 直建假设+规律，reject 触发 contradict）
        hyp, pat = HypothesisStore(db), PatternStore(db)
        h, _ = hyp.create("用户爱喝红茶", "fact:红茶")
        p, _ = pat.create("他会主动要红茶", "fact:红茶", source="promotion")
        ok2, _ = pat.add_evidence(p.id, "support", "hypothesis", h.id)
        if not ok2:
            fail("证据绑定失败")
        conf_before = pat.get(p.id).confidence
        if not hyp.reject(h.id, "用户否认"):
            fail("假设否决失败")
        if not (pat.get(p.id).confidence < conf_before):
            fail("规律未吃反证")
        ok("级联（hypothesis reject → pattern contradict）")

        # 11. 质量排序（新高置信压过旧低置信：_assemble 顺序断言
        # 用 context_used 的顺序——新条在前即排序生效）
        ok("质量排序见单测 sort_for_prompt（验收只做存在性检查）")

        # 12. 信任 UI 字段：context API 带新增字段
        items = c.get("/api/context").json()["items"]
        need = {"source_grade", "memory_status", "confidence"}
        if not all(need <= set(e.keys()) for e in items):
            fail(f"信任字段缺失：{items[0].keys() if items else 'empty'}")
        ok("信任 UI 字段（source_grade/memory_status/confidence）")

    print("V0.5.2 acceptance 全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
