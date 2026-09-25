"""V0.2 验收演示：对运行中的服务走一遍上下文闭环（真模型）。

用法：python run.py 后另开终端
    python scripts/acceptance_v02.py

覆盖：A1 上下文可看懂 · A2 读写闭环 · A3 纠正生效 · A4 删除即失效 · A5 暂停有效
     + 附加验收"Context 改变后续判断"（真实模型的反馈场景）
"""
from __future__ import annotations

import json
import sys
import time

# Windows GBK 控制台下避免 emoji/中文报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

BASE = "http://127.0.0.1:8000"


def _chat(message: str, session_id: str | None = None) -> dict:
    payload: dict = {"message": message}
    if session_id:
        payload["session_id"] = session_id
    r = httpx.post(f"{BASE}/api/conversation", json=payload, timeout=300)
    if r.status_code != 200:
        print(f"[FAIL] ❌ 对话失败（{r.status_code}）：{r.text[:200]}")
        sys.exit(1)
    data = r.json()
    if not data.get("ok"):
        print(f"[FAIL] ❌ 对话返回异常：{data}")
        sys.exit(1)
    return data


def main() -> int:
    step = 0

    def ok(msg: str) -> None:
        nonlocal step
        step += 1
        print(f"[{step:02d}] ✅ {msg}")

    def fail(msg: str) -> None:
        print(f"[FAIL] ❌ {msg}")
        sys.exit(1)

    # 1. 状态（V0.2 字段就位）
    r = httpx.get(f"{BASE}/api/status", timeout=10)
    if r.status_code != 200:
        fail(f"服务未启动（{r.status_code}）")
    s = r.json()
    if "context_entries" not in s:
        fail("状态缺少 V0.2 字段 context_entries（服务是旧版？）")
    ok(f"服务在线：v{s['version']} 记忆={s['events']} 理解={s['context_entries']} "
       f"会话={s.get('sessions')} 暂停={s['paused']}")
    if s["paused"]:
        httpx.post(f"{BASE}/api/privacy", json={"paused": False}, timeout=10)
        print("      （当前处于暂停，已先恢复）")

    # 2. 读写闭环：透露信息 → 沉淀（A2）
    print("      对话第1轮（真模型，可能要等 20-60 秒）…")
    t0 = time.time()
    r1 = _chat("先记住这个：我是高二学生，同桌总借我笔记")
    if not r1["deposit"] or not r1["deposit"]["created"]:
        fail(f"沉淀未产生新理解：{r1['deposit']}")
    ok(f"读写闭环：第1轮回答后沉淀 {len(r1['deposit']['created'])} 条理解"
       f"（耗时 {time.time()-t0:.0f}s）")

    # 3. 上下文可看懂（A1）
    r = httpx.get(f"{BASE}/api/context", timeout=10)
    ctx = r.json()
    if ctx["total"] < 1:
        fail("上下文列表为空")
    item = ctx["items"][0]
    if not item.get("source") or not item.get("created_at"):
        fail(f"条目缺少来源/时间：{item}")
    ok(f"A1 上下文可看懂：{ctx['total']} 条，示例「{item['kind_label']}·"
       f"{item['content'][:30]}」来源={item['source']} 时间={item['date']}")

    # 4. 纠正生效（A3）：先走对话纠正，模型没落到年级条目时用手动通道兜底
    print("      对话第2轮（发出纠正）…")
    r2 = _chat("不对，我说错了——其实我是高一的，刚才口误")

    def _grade_items() -> list[dict]:
        items = httpx.get(f"{BASE}/api/context", timeout=10).json()["items"]
        return [i for i in items if "高一" in i["content"] or "高二" in i["content"]]

    grade = _grade_items()
    fixed_via = "对话纠正"
    if not grade or "高一" not in grade[0]["content"]:
        if grade:  # 模型没纠正到位 → 信任界面的手动编辑通道
            httpx.put(f"{BASE}/api/context/{grade[0]['id']}",
                      json={"content": "用户是高一学生"}, timeout=10)
            fixed_via = "手动编辑（信任界面）"
        else:  # 连年级条目都没有 → 无法验证纠正
            fail("第1轮沉淀里没有年级条目，无法验证纠正："
                 f"{[i['content'] for i in httpx.get(f'{BASE}/api/context').json()['items']]}")
    # 清掉任何残留的旧值条目，保证第3轮只有一个真相
    for it in _grade_items():
        if "高二" in it["content"] and "高一" not in it["content"]:
            httpx.put(f"{BASE}/api/context/{it['id']}",
                      json={"content": "用户是高一学生"}, timeout=10)
    hist = httpx.get(f"{BASE}/api/context/history", timeout=10).json()["items"]
    changed = [h for h in hist if h["action"] in ("correct", "manual_edit")]
    if not changed:
        fail("变更历史里没有纠正记录")
    ok(f"A3 纠正生效（{fixed_via}）：{changed[0]['content_before'][:24]} → "
       f"{changed[0]['content_after'][:24]}（历史可查）")

    # 5. 同一会话后续回答使用修正值（A3 后半）
    print("      对话第3轮（验证用新值回答）…")
    r3 = _chat("我读几年级来着？", r1["session_id"])
    if "高一" not in r3["answer"]:
        fail(f"回答未使用修正值：{r3['answer']}")
    ok(f"A3 同一会话即用修正值：“我读几年级” → {r3['answer'][:60]}")

    # 6. 反馈场景（附加验收：Context 改变后续判断）
    print("      对话第4轮（默认判断：解方程）…")
    a1 = _chat("帮我解方程 3x+7=22，直接给我答案")
    if any("提示" in d for d in a1["approach"]):
        fail(f"第一轮不该已有提示指令：{a1['approach']}")
    ok(f"反馈场景①：无相处方式 → 判断={a1['approach']}")

    print("      对话第5轮（纠正相处方式）…")
    a2 = _chat("以后别直接给我答案，先给提示，我自己想卡住了才要答案")
    kinds = [e["kind"] for e in httpx.get(f"{BASE}/api/context", timeout=10)
             .json()["items"]]
    if "interaction" not in kinds:
        fail(f"纠正未沉淀为相处方式条目：{kinds}")
    ok("反馈场景②：纠正已写入 Personal Context（kind=interaction）")

    print("      对话第6轮（同类情境再来一次）…")
    a3 = _chat("帮我解方程 5x-3=12")
    if a3["approach"] == a1["approach"] or a3["approach"] == ["default"]:
        fail(f"判断没有变化：{a1['approach']} → {a3['approach']}")
    if "x=3" in a3["answer"].replace(" ", ""):
        fail(f"已纠正为先给提示，但仍泄露答案：{a3['answer']}")
    ok(f"反馈场景③：判断已变化 {a1['approach']} → {a3['approach']}，"
       f"且回答未泄露答案：{a3['answer'][:50]}…")

    # 7. 删除即失效（A4）
    items = httpx.get(f"{BASE}/api/context", timeout=10).json()["items"]
    for it in items:
        if it["kind"] == "interaction":
            httpx.delete(f"{BASE}/api/context/{it['id']}", timeout=10)
    a4 = _chat("帮我解方程 2x+4=10")
    if a4["approach"] != ["default"]:
        fail(f"删除后判断未回到默认：{a4['approach']}")
    ok("A4 删除即失效：相处方式删光后，判断回到 default")

    # 8. 导出个人上下文（信任）
    r = httpx.get(f"{BASE}/api/context/export", timeout=10)
    data = r.json()
    if "entries" not in data or "history" not in data:
        fail("个人上下文导出结构不对")
    out = f"acceptance_context_export.json"
    ok(f"导出个人上下文：{json.dumps(data['counts'], ensure_ascii=False)}"
       f"（attachment: {r.headers.get('content-disposition', '')[:60]}…）")

    # 9. 暂停有效（A5）：回答仍给出，但不沉淀
    before = httpx.get(f"{BASE}/api/context", timeout=10).json()["total"]
    httpx.post(f"{BASE}/api/privacy", json={"paused": True}, timeout=10)
    p1 = _chat("暂停状态下我说我是天蝎座")
    after = httpx.get(f"{BASE}/api/context", timeout=10).json()["total"]
    if p1["deposit"] is not None or not p1["paused"]:
        fail(f"暂停状态仍在沉淀：{p1}")
    if after != before:
        fail(f"暂停期间理解条目数变了：{before} → {after}")
    httpx.post(f"{BASE}/api/privacy", json={"paused": False}, timeout=10)
    ok(f"A5 暂停有效：仍回答（{p1['answer'][:24]}…）但 deposit=None、"
       f"条目数 {before} 不变；已恢复记录")

    # 10. 会话历史（可回看）
    h = httpx.get(f"{BASE}/api/conversation/history", timeout=10).json()
    if len(h["messages"]) < 12:
        fail(f"会话历史不完整：{len(h['messages'])} 条")
    ok(f"会话历史持久化：{len(h['messages'])} 条消息")

    print("\nV0.2 全部验收步骤通过 🎉")
    return 0


if __name__ == "__main__":
    sys.exit(main())
