# V0.5 计划 — Pattern Engine 第一阶段：稳定行为模式

> 依据：`docs/PRODUCT_TRUTH.md`（北极星：是否让 AI 更懂这个人或更可靠利用这种理解）、
> 成长模型（Observation → Hypothesis → Pattern → Strategy）、V0.4 审计结论、
> 用户 V0.5 范围决议（2026-09-29，含 6 条调整）。
> 前置：V0.4 已交付（tag `v0.4.0`）；P0/P1/P2 按本文件 §二执行。
> 状态：**已完成**（2026-09-30，P0/P1/P2 + Pattern 双源 + Goal + Reflection 全过）

---

## 一、V0.5 唯一要证明的命题

> **一个用户连续使用一段时间后，AI 能否发现一个他过去没有明确告诉它的稳定模式；
> 经他确认后，未来类似情况出现时，AI 的回答和建议发生变化。**

关键定语（用户调整）：**先证明"发现稳定模式并改变理解"，不追求数量，不设计"高级人格规律"**。
一条端到端走通即达标；第二条是 bonus，不是 KPI。

阶段目标不是"更多功能"，而是第一次回答：
"AI 记得我"之后，"AI 能否开始理解我的规律，并用它帮我成长"。

---

## 二、Scope（做）

| # | 内容 | 对应 |
|---|---|---|
| 1 | P0：R2 主题分裂（停用二连字/长词优先/高频降权/巨组拆分） | 审计 D1 |
| 2 | P1：沉淀卫生（相似合并/创建配额/变更日志） | 审计 D2 |
| 3 | P2：statement 去计数（新模板无数字，存量不动） | 审计 D3 |
| 4 | Pattern 最小实现：两表（`patterns` + `pattern_evidence`）+ 两来源晋升 | §四 |
| 5 | Pattern 进 prompt 独立段（仅 confirmed）+ 三次范式测试 | §六 |
| 6 | Goal 最小实现：`goals` 单表 + 用户表达检测 + 确认建 Goal + 关联展示 | §七 |
| 7 | Reflection readout：`build_weekly_digest` 纯函数 + 首页只读卡片 | §八 |
| 8 | 测试 + `scripts/acceptance_v05.py`（tmp+Mock，零污染，零云端） | §九 |

## 三、Non-goals（不做，写进验收 grep）

Agent 操作电脑 / 自动执行 / Web Search / 多 Agent / RAG / Embedding /
向量数据库 / 语音 / 外部设备 / 后台进程·定时器·推送 / LLM 生成 Pattern 或画像 /
一次聊天直出长期人格 / AI 推测人生目标 / UI 大改（除 Reflection 只读卡片）/
官网 / installer / SHA / release（封版统一做）。

---

## 四、Pattern 数据模型（两种来源，都有证据）

**晋升铁律**：Pattern 只能来自 A 或 B，**禁止一次聊天直接生成**。
B 路径同样必须经过一次显式确认（复用 V0.4 verifier 流程），
"用户亲口说"不等于"跳过验证"。

```sql
patterns(id, statement, confidence REAL, status[tentative|confirmed],
         scope, support_count, source[promotion|user_confirmed],
         created_at_iso/epoch, updated_at_iso/epoch, used_count)
pattern_evidence(id, pattern_id, polarity[support|contradict],
         source_type[hypothesis|context|event|confirmation],
         source_id, origin, excerpt, detail JSON, created_at_iso/epoch)
pattern_log(id, epoch, iso, action, pattern_id, conf_before/after, detail)
```

- **来源 A · Hypothesis 晋升（主要路径）**：同 scope 簇 ≥2 个 confirmed hypothesis，
  且独立源总数 ≥3 → 生成 tentative Pattern；`confidence` = 来源假设均值（显式公式）；
  `evidence_refs` = hypothesis id 列表（经假设可追溯到 Context/Event，全链不断）。
- **来源 B · 用户确认的重要模式**：用户明确表达规律性陈述
  （"我发现我一到截止前就…" / "把X记成我的固定模式"）→ 先建 **tentative**
  Pattern（证据=用户原话，不算结论）→ 后续相关轮次积累证据 →
  走 verifier 确认问题 → 用户确认 → confirmed / 反证 → rejected。
  **B 路径同样要过"待验证→积累→确认"全程，不许 deposit 直写 confirmed；
  用户自己的判断也可能有偏差，核心是形成可验证理解，不 رابطه是相信一句话。**
- 状态机沿用假设语义：tentative → confirmed（用户确认）/ rejected Sixth？——
  Pattern 的否决 = rejected（保留，与假设一致）；删除 = 逻辑删。
  **只有 confirmed 进 prompt**（比假设更严：规律的门槛高于猜测）。
- 置信度数学沿用 V0.3 常数风格另起一组（初始 0.60 / 支持 +0.06 / 反证 -0.20 /
  独立源<2 锁 0.75），常量单测锁定。用户确认只改状态不动数值（沿用"确认≠置信"）。

## 五、Pattern 进 Prompt 与行为证明

权重（从高到低）：相处方式指令 > **Pattern（confirmed）** > 假设段 > 事件。
Pattern 段格式沿用假设段风格，标记 `[规律·置信度0.78]`，后跟"（已验证规律，可作为建议依据）"。

三次范式测试（BehaviorMock 因果闭合，D007 老办法）：
- Turn1：同类情境问题 → prompt **无** Pattern 段 → 默认回答。
- Turn2：沉淀 + 归纳 + 确认（来源 A 或 B 均可参数化）。
- Turn3：同类情境重问 → prompt 出现 Pattern 段 → 回答引用该规律（断言关键词），
  approach/prompt 双断言。**回答的变化必须只能来自 Pattern 段的出现。**

## 六、Goal 系统（严格用户来源）

```sql
goals(id, title, description, status[active|paused|done],
      linked_pattern_ids JSON, linked_scopes JSON,
      created_at_iso/epoch, updated_at_iso/epoch)
```

- **来源铁律**：Goal 只能来自用户明确表达（"我想 CSP 提高组通过" /
  "明年想成为…"）。deposit 检测到目标表述 → 不建 Goal，
  而是走一次确认问题（"要把「CSP 提高组通过」记为你的长期目标吗？"）→
  确认才建 active Goal。**AI 永不推测人生目标**（grep 级禁令：goal 创建函数
  只接受"用户原话"参数，不接受模型生成文本）。
- **AI 只做关联**：Goal 详情页展示按 scope/关键词规则匹配到的 Pattern 与 Context
  （纯展示，无推理断言）。Goal 进 prompt 独立小段（"他的长期目标"），只引用。
- 状态：active / paused（暂停观察关联）/ done（用户标记完成）。删除 = 逻辑删。

## 七、Reflection readout（第一版：只读 digest）

- 纯函数 `build_weekly_digest(store)`：统计过去 7 天——事件数、新增理解数、
  假设状态变化（新建/确认/否决）、确认率、Pattern 数/状态 → 规则拼装 3~5 行中文
  + 至多 1 个 gentle 问句（"项目推进和休息时间看起来有点冲突，要聊聊吗？"类模板，
  常量可测）。**无模型调用，无幻觉空间**。
- 触发：仅用户主动打开时——落地为 `GET /api/reflection/weekly`，
  首页加载调一次展示只读卡片（V0.5 允许的最小 UI：无按钮、无交互、无跳转）。
  失败静默（digest 失败不影响首页其他区块）。
- 验收：digest 每一行数字都可从 DB count 推导（单测逐行断言）；
  无 Timer/线程/推送新增（沿用 V0.4 grep 锁死做法）。

## 八、P0/P1/P2 修复要点（执行摘要，详细见 §九测试）

- **P0**：停用二连字表（约 40 个，常量）→ 分组前剔除；label 改"最长且覆盖最多"；
  高频（>50% 条目）bigram 退出 label 候选；>6 条巨组二次切分。
  验收：审计同款混杂数据（提醒/三天/简短）必须长出 ≥2 个 interaction 假设；
  单主题数据仍只出 1 个。
- **P1**：`add_entry` 前相似度门（二连字 Jaccard ≥0.6 → 合并：旧条目 used+1 +
  变更日志，不新建）；同 kind 7 天新增 >10 只合并。验收：100 轮模拟灌入总量有上界。
- **P2**：新 statement 模板去计数；存量不动；UI 以 `support_count` 为准。

## 九、测试计划

- P0：混杂数据分裂测试 + 单主题不分裂测试 + 确定性测试（排序/字典序）。
- P1： paraphrase 合并测试 + 配额上界测试 + 合并走变更日志测试。
- P2：新模板无数字测试 + 存量兼容测试。
- Pattern：晋升测试（A 路径：2 confirmed 同簇 + 3 独立源 → tentative，均值公式；
  B 路径：用户表达 → 验证问题 → 确认 → 建 Pattern，跳过验证则不建）；
  证据链测试（A：refs 可逐级追溯到 Event；B：原话 + 引用行）；
  三次范式行为测试（来源 A/B 双参数化）；否决/删除测试。
- Goal：非用户表达不建测试（模型生成文本调创建函数必须拒绝）；
  确认建 Goal 测试；关联展示测试；暂停/完成测试。
- Reflection：逐行数字可推导测试；失败静默测试；无后台设施 grep 测试。
- 回归：V0.4 全部 + 全量 suite；`scripts/acceptance_v05.py`（tmp+Mock）。
- 铁律：每个能力配"行为变化"断言（prompt/response 双断言），不靠演示。

## 十、验收标准

1. P0/P1/P2 验收全过；Pattern 三次范式（A/B 双源）全过；Goal 确认链全过；
   Reflection 逐行可推导全过。
2. 全量回归零失败；真实数据库零触碰；零云端调用。
3. Non-goals grep 全过（Embedding/RAG/向量/Timer/推送/Agent exec/语音/设备零新增）。
4. 小步提交（P0/P1/P2 → Pattern → Goal → Reflection → 验收）；文档：本文件 + D015。

## 十一、已知限制与诚实声明

- Pattern 稀疏：按晋升规则，真实用户可能数周才长出第一条——**这是设计**（宁可少猜），
  上线后 Pattern 区长期为空是预期行为，不算 bug。
- Goal 目前只做到"引用"：把目标文本放进 prompt 不等于"理解目标"。
  真正的"行为对目标加分/减分"判断是 V0.6 的事；本版不声称 AI 懂你的目标，
  PRODUCT_TRUTH 修订时把这句写进去（防自嗨）。
- R2 修完后真实数据需重跑验证 D1 是否真正解决（审计数据复用为回归夹具）。
