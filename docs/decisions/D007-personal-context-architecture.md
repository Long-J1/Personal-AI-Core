# D007：Personal Context 与对话通道的架构——"上下文必须改变行为"

- 日期：2026-09-26
- 状态：已接受（V0.2 A 组实现依据）
- 背景：V0.2 要证明"AI 开始拥有这个人的上下文"。用户明确加了一条实现原则：
  **不要做成静态用户档案，也不要只是把更多用户信息拼进 Prompt；
  Context 必须改变 AI 后续的理解和行为，且这个改变必须可重复测试。**

## 决定

### 1. Context 是"活条目"，不是档案
- 新表 `context_entries`（kind: profile/fact/dynamic/interaction，带 source、时间、used_count、active）
  与 `context_log`（create/correct/manual_edit/delete/restore 全量变更日志），与事件记忆同库
  `data/personal_ai.db`。纠正改内容但旧内容永久保留在日志里（历史可查）。
- 删除 = `active=0`（逻辑删，立即从所有提示词消失），可恢复。

### 2. "相处方式指令"是 Context 影响行为的正式通道
- `kind=interaction` 条目由 `derive_directives()` 派生为 System Prompt 中的
  **"相处方式（必须遵守）"** 段落；其余条目进"关于这个人的理解"段落。
- `ChatReply.approach` 记录本次判断（`["default"]` 或指令列表）——测试直接断言它前后变化。
- 每轮读取后 `mark_used()` 累加 used_count，证明条目真被消费。

### 3. 沉淀（deposit）的护栏
- 模型按 `DEPOSIT_SYSTEM` 输出 `{new, corrections, event}`，经 `extract_json` 容错解析；
  应用顺序 **先纠正后新增**，新增有去重（同内容跳过）与单轮上限（默认 3 条），
  单条长度上限 200 字。沉淀失败只记日志，绝不影响已发出的回答。
- 纠正目标用 `#编号` 别名映射（小模型容易抄错 32 位 hex id），同时容忍直接回 id。

### 4. 暂停语义（对话侧）
- 暂停 = 不观察、不沉淀：**对话仍回答**（显式提问不是观察），但不写消息、不写理解、不记事件，
  回复带 `paused=true`、`deposit=null`。恢复后一切照旧。理由：暂停的意图是"别再记录我"，
  不是"别理我"；且状态可逆、UI 醒目（PRODUCT_TRUTH §6）。

### 5. 端点划分
- `POST /api/chat` 保持 V0.1 回忆语义不动（D1 回归底线）；新对话走 `POST /api/conversation`
  （会话 id、approach、deposit 元数据）。两件事共用底层记忆库，但 API 不混。

### 6. 测试策略：因果必须在测试里闭合
- `BehaviorMock`（tests/test_conversation.py）：**同一个模型、同一句用户话**，
  回答由提示词推导（有指令→只给提示，无指令→直接给答案）。
  这样"回答变了"只能来自 Context 变化，不是脚本顺序编排出来的假因果。
- 真模型复验：`tests/test_feedback_reale2e.py`（三步真实闭环，Ollama 在线时自动跑）。

## 被拒绝的候选项（V0.2 过滤器留痕，PLAN D2）

- ✗ 向量/语义检索找相关历史——位置已留（D003），记忆变厚前不上；
- ✗ 每轮把全部事件历史塞进 Prompt——上限 8 条近期 + 话题命中，控制上下文体积；
- ✗ 多会话管理（切换/命名/归档）——单人单实例，先不做会话 UI；
- ✗ 对话转事件的自动重要度评分精修——沿用 0.5 默认，够用再说；
- ✗ 在回答里汇报"我学到了 XX"——违反 C2 克制（沉淀只进 UI 元信息与 ④ 信任界面）。

- 测试：`tests/test_conversation.py::test_feedback_scenario_context_changes_judgment`（mock 因果）、
  `tests/test_feedback_reale2e.py`（真模型）、A1-A5 各自的验收测试。
