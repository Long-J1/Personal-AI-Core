# V0.4 计划 — 自然窗口中的低打扰验证提问

> 依据：`docs/PRODUCT_TRUTH.md`（沉默默认、介入必须赚取）、成长模型 §10（主动提问是主动学习机制，不是闲聊）、
> 执行任务书 V0.4 范围决议（2026-09-29）：**只验证"AI 能否基于已有 Hypothesis，在用户自然交互窗口中提出低打扰验证问题"，不做主动聊天。**
> 前置：V0.3 已冻结（tag `v0.3.0`，假设/证据/置信度/纠正联动全链打通）
> 状态：**已完成**（2026-09-29，Q1~Q7 全过 + acceptance_v04 8 步 3 连过）

---

## 一、V0.4 唯一要证明的命题

> **AI 能否在用户本来就在对话时，顺带问一个有来源、可追溯的验证问题，且用户回答后进入已有证据链？**

关键定语：**搭便车，不拦路**。问题只出现在用户主动发起的对话轮次里，作为自然回答的附带（最多一句），
绝不主动发起会话、不弹窗、不推送、不定时。沉默仍是默认产出。

## 二、Scope（做）

| # | 内容 |
|---|---|
| 1 | 验证对象选择规则（全确定性常量，见 §五） |
| 2 | 问题文本模板（规则拼装，不调 LLM，见 §六） |
| 3 | 待回答（pending）状态：存假设行上，重启不丢，同时只挂一个 |
| 4 | 下轮回答分类：确认 / 否决 / 回避（规则关键词，否决优先，见 §七） |
| 5 | 分类结果进已有证据链（确认→support+confirmed；否决→走 V0.3 reject；回避→不处理） |
| 6 | 回复元数据携带 `verification_question`（假设 id/文本/置信度/原因），可断言 |
| 7 | hypotheses 表 migration（`asked_count`、`last_asked_epoch`），老库平滑升级 |
| 8 | 测试 Q1~Q6 + `scripts/acceptance_v04.py`（tmp 库 + Mock，零污染） |

## 三、Non-goals（不做）

- 主动发起对话 / 推送 / toast / 定时器 / 后台轮询（任何形态的主动打扰）
- LLM 生成问题文本（措辞不可测，违反 D007 精神）
- 一轮多问；向 confirmed 假设提问；问完追问（一次无回答即进入冷却）
- V0.5 InterventionPolicy 扩展；自动执行；语音；多设备
- UI 大改（问题文本自带来源说明，无需新区块；理由见 §九）
- 真实数据库灌数据；官网 / installer / SHA / release（封版统一做）

## 四、数据模型（migration，不是新表）

`hypotheses` 加两列（`HypothesisStore.__init__` 里 `PRAGMA table_info` 检查 +
`ALTER TABLE ADD COLUMN`，老库无感升级；新库建表语句同步更新）：

- `asked_count INTEGER DEFAULT 0`（累计问过几次，≥3 不再问——防唠叨）
- `last_asked_epoch REAL DEFAULT 0`（上次提问时间，冷却用）
- pending 本体：`pending_question TEXT DEFAULT ''`（空=无挂起；非空=等回答）

## 五、选择规则（`core/verifier.py`，常量可测）

候选 = status 为 tentative 的假设，按置信度倒序取首个满足全部条件者：

1. 置信度在待验证区间：`0.50 ≤ conf < 0.70`（再高不需要问，再低不值得问）
2. 独立支持源 `< 2`（问的目的就是要第二个独立确认；≥2 的走自然证据积累）
3. 无全局挂起（同时只挂一个问题）
4. 该假设冷却中：`now - last_asked_epoch > ASK_COOLDOWN_S`（默认 24h，可测常量）
5. `asked_count < MAX_ASKS`（默认 3，问三次不答就闭嘴）
6. **话题相关**：用户本轮消息含 scope 关键词（`interaction:X`→X；`activity:X`→X，子串匹配）
7. 非暂停（暂停=停止学习，沿用 inducer 语义）

常量：`CONFIRM_BAND=(0.50, 0.70)`、`ASK_COOLDOWN_S=86400`、`MAX_ASKS=3`。

## 六、问题模板（规则拼装）

```
多问一句（验证我的理解：关于「{label}」我目前只有约{conf:.0%}把握，
依据是{scope}相关的{n}条记录）：{short_claim}，对吗？
```

- `short_claim` 按 scope 前缀规则派生：`activity:X`→"你最近在持续X"；
  `interaction:X`→"你在X方面希望我保持一致"。未知前缀→通用句式。
- 文本即来源声明（哪个主题、为什么问、当前置信度），满足"提问必须有来源"。
- 长度上限 120 字；附在自然回答末尾，独立成段。

## 七、回答分类（下轮用户消息，否决优先）

顺序执行（顺序即语义，先判否决防"不对"误命中"对"）：

1. 含 `_is_explicit_denial`（复用 V0.3 否决词+剥离表）→ **否决**：走 `reject()`（0.25 + rejected + 反证入链），清 pending。
2. 含确认词（`CONFIRM_KEYWORDS=("对的","没错","是的","是这样","没问题","同意","确实如此")`，
   且已先排除否决）→ **确认**：`confirm()`（状态 confirmed，置信度不动）+ 加 support 证据
   （来源=本次用户回答，反证链要求"回答进证据链"；origin=`answer:<log_id>` 独立源）。
3. 其他 → **回避**：不清 pending（给用户留着，下次相关话题可再答），不记证据，
   但计一次冷却（不立刻重问）。

确认/否决的证据 `excerpt` 存用户原话（截断 200），`detail.channel="verification"`。

## 八、证据链（复用 V0.3，不新建设施）

- 确认回答 = `correction` 型 support 证据（用户行为记录，可追溯；铁律：假设内容本身永不入链）。
- 否决回答 = V0.3 reject 全套（反证 + 0.25 + rejected）。
- pending 问题文本本身永不成为证据（问不算答）。
- `approach` 语义不动（仍只由 interaction 指令决定）；行为变化体现在：
  确认后 prompt 段标记变为"用户已确认"，后续回答可引用已确认理解。

## 九、Prompt 与 UI 行为

- Prompt：沿用 V0.3 假设段；confirmed 显示"用户已确认"，是最强的"后续行为变化"证据。
  问题文本**不**进 prompt（问过即焚，下轮只看分类结果）。
- UI：**不加新区块**。理由：问题是对话流的一部分，文本自带来源说明；
  回复元数据 `hypotheses_used` 照常记录参与的假设。过度 UI 会把"低打扰"做成"高存在感"，违背北极星。
- `ChatReply` 加法字段：`verification_question: dict | None`
 （`{hypothesis_id, text, confidence, reason}`，无则 None；`to_dict` 同步）。

## 十、测试计划（Q1~Q6，Mock/本地，零云端调用）

- **Q1 选择规则**：band 上下界、独立源≥2 不问、有挂起不问、冷却中不问、问满 3 次不问、
  话题不相关不问、暂停不问（单测，全用 fixture）。
- **Q2 只提问不下结论**：挂起后假设 status/confidence/evidence 全不变；
  回复元数据含问题 + 来源三要素（id/原因/置信度）。
- **Q3 确认链**："对的"→ confirmed + support 证据 +1 + prompt 段变为"用户已确认"。
- **Q4 行为变化**：确认前后 `_assemble` 的 prompt 文本可断言变化；
  否决后 prompt 不再含该假设（复用 V0.3 语义 + V0.4 不重建：`skipped`）。
- **Q5 否决链**："不对…"→ rejected + 0.25（走 V0.3 reject，不断言重复）。
- **Q6 沉默保持**：无符合假设 / 全冷却 / 不相关话题 → 回复无附加问题，
  `verification_question is None`。
- **Q7 回归**：V0.3 全部测试 + 全量 suite 全绿；migration 测试（老 schema 库初始化不丢数据）。
- `scripts/acceptance_v04.py`：tmp+Mock+TestClient，8 步左右，风格沿用 v03。

## 十一、验收标准

1. Q1~Q6 全过；migration 有单测；acceptance_v04 连过 3 次。
2. 全量回归零失败；真实数据库零触碰（只读验证，不写）。
3. `InterventionPolicy`、推送/定时设施：零新增（grep 断言：无新 `Timer`/`sleep` 轮询/通知调用）。
4. 小步提交；文档：本文件 + D014（记录被拒绝项：LLM 出题、多问、追问、UI 新区块）。

## 十二、已知限制

- 子串相关性是字面匹配（"提醒"能命中，"别打扰我"命中不了"提醒"主题）——保守即低打扰，可接受。
- 确认词表是中文口语子集，方言/短句（"嗯""好"）判回避——宁可漏判，不误判。
- 冷却 24h 对"高频验证"偏保守； constants 可调，不在本版调。
- pending 跨会话共享（按假设行存，不按 session）：用户另起话题先答旧问题，照常分类——符合"自然窗口"定义。

## 十三、Future work（记录，不实现）

V0.5 介入策略（验证通过的假设何时转行动）、语义相关性、语音窗口、confirmed 假设的行动化。
