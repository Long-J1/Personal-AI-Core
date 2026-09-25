# D008：利用过去理解现在的通道 + 沉默记账（V0.2 B/C 组）

- 日期：2026-09-26
- 状态：已接受
- 背景：PLAN_v0.2 B 组要求"重提旧事主动带出进展/卡点，且问题不含存储时的关键词"；
  C 组要求开发期沉默可审计但不进正式记忆库。

## 决定

### 1. B1 的双通道（关键词检索不可靠时 Context 兜底）
- **通道 A（事件）**：对话组装时复用回忆的检索级联（`Recaller.retrieve`，D003），
  把"话题命中的更早事件"与"近期 8 条"合并去重（总上限 14，`chat_events_total`）。
  用户带"上次/之前"时 `plan_query` 自动放开时间窗，能捞回几天前的事件。
- **通道 B（Context 条目，主力）**：`dynamic`/`profile` 条目**无条件**进每轮提示词——
  即使旧事件被新事件挤出列表、即使提问一个关键词都不沾，AI 仍拿得到卡点。
  这正是"Personal Context 是一等对象"的价值：理解不依赖检索命中。
- **指令**：CHAT_SYSTEM 第 5 条要求"与话题相关的旧事主动接回：上次做到哪、卡在哪、
  当时为什么停——一句话自然带出"。
- 测试分层：mock 断言提示词组成（含被挤出场景）+ reale2e 断言真模型真的接回。

### 2. B3 可审计
- `_assemble` 完成即打结构化日志：`上下文组装：理解 N 条（画像x…）· 事件 M 条（话题命中 k）· 会话历史 K 条`。
- 日志在 `logs/pai.log`（开发可查），正式记忆库不存"组装行为"本身。

### 3. C1 沉默记账的落点
- 新模块 `core/audit.py`：`logs/silence_audit.jsonl` 追加一行
  `{kind: observation_silence, epoch, iso, event_id, importance, stored, intervene, reason}`。
- 管线在 `intervention.decide()` 之后调用；写失败仅告警（失败可恢复）；
  **暂停时没有观察也没有判断 → 不产生记账**。
- 断言不进记忆库：`tests/test_audit.py` 检查 events/facts/context 均不含沉默理由。
- 定位：开发期脚手架（PRODUCT_TRUTH §7：产品不保留普通沉默），未来可整体移除。

### 4. C2 的边界
- 沉淀结果只出现在 `ChatReply.deposit` 元数据与 UI ④ 信任界面，
  **永不拼进回答正文**（`test_c2_deposit_never_reported_in_answer` 锁定）。
- 界面上气泡下的小字（判断/沉淀计数）是系统状态标注，不是 AI 开口汇报——
  AI 自己从不说"我学到了 XX"。

### 5. 会话历史排序的平局裁决
- Windows 时钟粒度下同一毫秒插入多条消息，`ORDER BY epoch` 不稳定；
  改为 `epoch + rowid` 双键排序（`memory/chat_store.py`）。曾导致 C2 类断言偶发失败，已修。

- 测试：`tests/test_conversation.py`（B1/B2/B3/C2）、`tests/test_audit.py`（C1）、
  `tests/test_feedback_reale2e.py::test_real_b1_resurfaces_old_stuck_point`。
