# D027 · Adaptive Judgment：理解 → 判断（里程碑）

日期：2026-10-03
基线：`0e5238c`（V0.5.10 Reality / Soak）
范围：**只做** Understanding → Judgment → Response Strategy
**零 schema、零新依赖、零新端点外的表、零新 capability**

---

## 0. 这次解决什么

到 V0.5.10 为止，Personal AI 已经能做到：

```
用户表达 → Memory → Understanding → Background Reasoning
        → Candidate → 用户反馈 → Evidence → 正式理解
```

但最后一步是断的：**正式理解进入 prompt 之后，模型自己决定怎么执行。**
"我先提示"和"我先自说自话地解释"之间，取决于模型这轮心情。

这次把断的那一环补上：

```
Formal Understanding → Judgment → Response Strategy → 模型
```

**判断层不是把理解再抄一遍塞给模型，而是先算出一个明确的回应方式，
再用一句执行指令约束模型这次怎么做。**

---

## 1. 三条设计决定

### ① 只有可信理解能改变行为

`core/judgment.py:is_formal()` 是唯一闸门。**AI 的猜测不能改变 AI 对用户的
正式行为策略**：

| 允许 | 拒绝 |
|---|---|
| confirmed hypothesis | tentative |
| confirmed pattern | background pending / accepted（未毕业） |
| active goal | rejected / deleted / expired |
| interaction + `user_direct` / `user_confirmed` | `quota_overflow:` scope |
| | interaction + `inferred`（AI 自己推测的相处偏好） |
| | `background:` scope 的任何状态 |

**`accepted` 尤其重要**：用户在"我正在思考"面板点了"正确"，说明他认可这条
推测——但认可 ≠ 毕业。没毕业就指挥行为，等于让猜测借用户的手获得了权力。

### ② 当前用户指令永远压过长期理解

优先级：`当前明确指令 > 正式理解 > 默认 direct`。

**长期记忆是对用户的尊重，不是命令。** 用户说"还是不会，直接告诉我吧"，
就当场直说。他下一句可能又改主意——那是他的权利。

### ③ 策略指令单独走，不混进 `directives`

`directives`（interaction 条目原文）是**用户的话**，进 `approach` 字段、
进"相处方式"提示段。`strategy_directive()` 是**系统对本次的执行要求**。

两者混在一起会让 `approach` 这个既有契约从"用户原话"变成"系统自述"，
破坏 `test_feedback_scenario_context_changes_judgment`（D007）的断言语义。
所以策略单独成行，且**不进 `approach`**。

---

## 2. 否定必须被读懂（本次最要紧的一个 bug）

第一版的偏好匹配是正向关键词表，结果把任务书里的核心故事原话

> 「做这种题的时候**别直接给我答案**，先给我一个提示就行」

判成了 `direct`。

**方向读反比读不出来更糟**：读不出来只是没生效；读反是每次都做用户
明确说过不要的事。

改成信号词级联：每个信号词记成 `(位置, 类别, 极性)`，取**最早**的那个决定结果。

```
提示类 + 肯定 → hint_first     提示类 + 否定 → direct（"别提示"）
答案类 + 肯定 → direct         答案类 + 否定 → hint_first（"别直接给答案"）
```

取最早 = 用户先说的意图说了算，后半句的转折不翻盘。

否定词表**刻意不含单独的"不"**（`_NEG`）：中文里"不"太常见
（"不错""不是不"…），靠它判极性会把大量正常句子判反。只收明确的祈使否定：
别 / 不要 / 不用 / 甭 / 不想 / 不需要 / 不必 / 无需。

这条有专门的 mutation（`G`）钉死。

---

## 3. 判断层不写记忆

`Judgment` 是 `frozen=True` 的 dataclass，没有任何写入口。
新的理解仍然只能走既有链路：
`conversation → Natural Evidence → Understanding`。

**不许因为"用户这次看起来喜欢这个回答"就自己写一条记忆。**
那是 D007 / D017 那条链的职责，判断层伸手就越权了。

---

## 4. 性能：不为功能付 30ms

第一版 `_collect_understandings()` 查四个 store，实测 **31ms/轮**——
正是 V0.5.10 修过的"全表扫 +30ms"，这次又亲手引入了一遍。

定位到根因不在扫描而在**固定开销**：每个 store 的 `_connect()` 每次连接都执行

```python
con.execute("PRAGMA journal_mode=WAL")
```

实测约 **7.7ms/次，与表大小无关**（空库 0 行同样 7.7ms）。这是既有问题。

本次**不去动 5 个 store**（超出范围，且 WAL 协商的行为变化需要另做验证），
改为**不再多查**：假设/规律/目标直接复用 `_assemble` 已经取到的那几份。

| | 成本 |
|---|---|
| Judgment 纯逻辑 | 0.011ms（空） / 0.11ms（20条） / 0.95ms（200条） |
| 真实链路新增 | 约 7.8ms（只剩相处偏好一次查询） |

只保留相处偏好的单独查询是刻意的：它**不能靠话题检索碰运气**——
用户上个月说的规矩，不该因为这轮没检索到就失效。

**遗留建议（不在本次范围）**：把 WAL 设置从每次 `_connect` 移到每个 store
初始化时做一次。全应用每轮十几次查询都在付这笔固定开销。

---

## 5. 判断层没有掀翻对话的权力

`_judge_now()` 任何异常都退回默认 `direct`（mutation `I` 钉死）。
判断是辅助，不是关键路径。

---

## 6. 产品化解释

用户看不到 `response_mode` / `hypothesis_id` / `confidence` / `source_type` /
`scope`。

`reasons` 会同时进提示词和产品文案，**绝不能带内部字段**。第一版把
`（{prefer_src}）` 拼进了理由，结果 `interaction:提示` 原样泄进提示词
（mutation `H` 钉死）。来源只留在 `applied`（内部可追溯），对外只说人话。

> 「我记得你之前说过这类问题你更想先自己想一想，所以这次我先给你提示。」

这句话有真实来源（`applied` 里有对应 scope），没有正式理解时它就不出现。

---

## 7. 三层验证

1. **纯判断层**——三条安全规则用构造数据直接钉。
2. **A/B replay**——同一个模型、同一个输入、同样的参数，唯一变量是有没有
   正式理解。Case A 无理解 / B 有 hint_first / C 用**真实 `delete_entry`**撤回。
   提示词比对到"策略段之外完全相同"。
3. **真库链路**——假设真的躺在数据库里走完整 `conversation.chat`：
   tentative / rejected / background accepted / inferred / outdated 都不许指挥行为，
   confirmed 才生效。纯函数通过不代表接线通过。

---

## 8. 完整故事 replay

```
用户：「做这种题的时候别直接给我答案，先给我一个提示就行」
  → 沉淀成正式 interaction 理解
用户：「这道题怎么做？」
  → Judgment = hint_first，模型先给提示
用户：「还是不会，直接告诉我吧」
  → Judgment = direct（当前指令压过长期理解）
用户：「以后这类题直接告诉我答案吧」
  → 走真实纠正链路**原地改写**那条理解
用户：「这道题怎么做？」
  → Judgment = direct，旧规矩不会自己回来
  → 历史仍可追溯（变更日志查得到原话）
```

第 ④ 步**必须走 `corrections` 而不是 `new`**：新建一条时旧偏好还活着，
只是排在后面（`list_entries` 按 `created_at_epoch` 降序），"最新赢"看着像
纠正，其实只是排序侥幸——旧条目一旦失效就会自己回来。原地改写才是真换掉。

---

## 9. 本次没有做什么

- 没有新建人格系统（多个正式理解 → 一个 Judgment，仅此而已）
- 没有 prediction / forecast / 自动干预 / 自动提醒
- 没有新 Memory Layer / 新数据库 / 新依赖 / 新模型
- 没有动 `PAI_IDLE_REASONING` 默认值
- 没有改 schema、没有 migration
- 没有碰官网 / Release / deploy / tag / push
