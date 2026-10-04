# PROJECT_MAP — Personal AI 项目全局地图

> **这是本项目唯一的总入口。**
> 任何一次大型开发结束后，回到这个文件重新获得整个项目的地图。
>
> 建立：2026-10-03 ｜ 对应代码：`410ea0c` ｜ 数据来源：**当前仓库实际代码**
>
> **事实优先原则**：本文档描述的一切都必须能在当前代码里指到出处。
> 凡是"讨论过但没做"的，一律显式标注，不写成已实现。
> 与代码冲突时**以代码为准**。

---

## 目录

0. [一分钟版](#0-一句话)
1. [当前状态](#1-当前状态)
2. [核心架构与真实调用链](#2-核心架构与真实调用链)
3. [四层概念：Memory / Understanding / Reasoning / Judgment](#3-四层概念)
4. [核心数据结构](#4-核心数据结构)
5. [Trust Boundaries 信任边界](#5-trust-boundaries-信任边界)
6. [Runtime Context 与 Memory 的区别](#6-runtime-context-与-memory-的区别)
7. [产品层：用户实际能看到什么](#7-产品层)
8. [Provider / Model 架构](#8-provider--model-架构)
9. [测试体系](#9-测试体系)
10. [版本历史](#10-版本历史)
11. [真实事故档案](#11-真实事故档案)
12. [已知限制](#12-已知限制)
13. [真实存在 vs 未实现（对照表）](#13-真实存在-vs-未实现)
14. [30 分钟重新上手路线](#14-30-分钟重新上手路线)

---

## 0. 一句话

> **Personal AI 是一个跑在本地的个人智能层**：前面接一个大模型，
> 后面有自己的长期记忆和"对用户的理解"层。
> 它不只是保存用户说过什么，还会判断某些说法是不是长期规律；
> 拿不准的**不当作事实**，而是放进待验证，等用户认可后才允许继续积累证据；
> 证据够了，这个理解才正式影响之后的回答。

---

## 1. 当前状态

### 代码事实（`410ea0c`）

| 项 | 值 |
|---|---|
| 分支 | `master` |
| commit 总数 | 55 |
| **未推送** | **43 个 commit**（`ahead origin/master`） |
| tag | `v0.2.0` `v0.3.0` `v0.4.0` `v0.5.0` |
| `core/config.py` version | `0.5.4` |
| `desktop/package.json` version | `0.5.5` |
| 测试用例 | 723 通过 / 0 失败 / 1 跳过（639 个测试函数） |
| 数据库表 | **15 张** / 5 个 store |
| HTTP 端点 | **59 个** |
| 新增依赖 | 无（运行时仅 fastapi / pydantic / uvicorn / httpx / pillow + stdlib） |

### 发布状态

- **唯一对外发布版本：V0.5.4 Beta**（2026-09-30，官网下载 + GitHub Release `v0.2.0` 挂安装包）
- V0.5.5 ~ 至今（UI 升级 / Judgment / Runtime Time）**全部只在本地**，未推送、未打 tag、未发布

> ⚠️ **版本号不一致是已知状态**：`config.py` 写 0.5.4，`desktop/package.json` 写 0.5.5。
> 这是 V0.5.5 之后连续开发但未做发版的结果，不是 bug，但下次发版前必须对齐。

### 未提交的脏文件

工作区有 5 个未跟踪文件，与本项目开发无关（外部工具遗留）：
`.gpt-profile/`、`chatgpt_link_test.png`、`chatgpt_state.png`、
`scripts/gpt_browser_login.py`、`scripts/gpt_open_link.py`

---

## 2. 核心架构与真实调用链

### 2.1 一句话流程（**这是当前代码的真实链路**）

```
用户输入
  ↓
[UI] interfaces/static/index.html  →  POST /api/conversation/stream
  ↓
[Conversation] core/conversation.py :: stream_chat()
  ├─ ① 结算上一轮挂起的验证问题      verifier.resolve_*_answer
  ├─ ② 等待后台沉淀完成（防竞态）    _await_deposit()
  ├─ ③ 读 Context 条目               context_store.list_entries()
  ├─ ④ 组装提示词 _assemble()  ←──────────────┐
  │     ├ Runtime Time  runtime_time         │  纯本地读系统时钟
  │     ├ 相处方式      derive_directives    │  = interaction 条目原文
  │     ├ 假设          list_for_prompt      │  tentative + confirmed
  │     ├ 规律          list_confirmed      │
  │     ├ 目标          list_active         │
  │     ├ **Judgment**  judgment.judge()     │  ← 第四层，见 §3
  │     ├ 策略指令      strategy_directive   │  唯一改变模型行为的一步
  │     ├ 事件          recaller + list_events
  │     └ 会话历史      history_for_prompt
  ↓
[Model] models/factory.py → OllamaAdapter | OpenAICompatibleProvider
  ↓  流式 delta 逐段返回给 UI
[Response] 正文先显示
  ↓
[处理摘要] 回答完成后才追加（buildTrace），**不阻塞首字**
  ↓
[后台沉淀] _deposit_body（线程池，与对话解耦）
  ① mark_used
  ② deposit()               模型抽取 → new / corrections / event
  ③ natural_bridge          自然证据 + 变化信号
  ④ inducer.induce_all()    规则归纳 R1/R2 → 假设
  ⑤ pattern_engine.promote_all()  假设 → 规律
  ⑥ background_bridge       后台推理 → pending 候选
  ⑦ B 路径                  用户显式声明 → 待验证规律
  ⑧ Goal                    用户显式声明 → 待确认目标
  ↓
[UI] 刷新各面板（context/hypos/understanding/thinking/candidates）
```

### 2.2 每一层的归属

| 层 | 谁负责 | 输入 | 输出 | 落库 | 同步? | 影响 prompt? | 能改长期状态? |
|---|---|---|---|---|---|---|---|
| Runtime Time | `core/runtime_time.py` | 系统时钟 | 时间块文本 | ❌ 不落库 | ✅ 每轮实时 | ✅ system 首位 | ❌ |
| Context | `memory/context_store.py` | 用户原话 | 条目 | ✅ `context_entries` | ✅ | ✅ | ✅ |
| Event | `memory/store.py` | 观察/理解结果 | 事件 | ✅ `events` | ✅ | ✅ | ✅ |
| Hypothesis | `memory/hypothesis_store.py` | 归纳 + 证据 | 假设 | ✅ `hypotheses` | ✅ | ⚠️ tentative 降权进 | ✅ |
| Pattern | `memory/pattern_store.py` | 晋升后的假设 | 规律 | ✅ `patterns` | ✅ | ✅ | ✅ |
| Goal | `memory/goal_store.py` | 用户显式声明 | 目标 | ✅ `goals` | ✅ | ✅ 仅引用标题 | ✅ |
| Background | `core/background_reasoning.py` | 事件序列 | pending 候选 | ✅ 复用 hypotheses 表 | 后台 | ❌ **永不直接进** | ✅ 但状态受限 |
| Judgment | `core/judgment.py` | 正式理解 + 当前消息 | 回应方式 | ❌ 不落库 | ✅ | ✅ **策略指令** | ❌ |
| Deposit | `core/deposit.py` | 用户消息 + 回答 | new/corrections/event | ✅ | 后台 | ❌ | ✅ |
| Memory Policy | `core/policy.py` | Event | 是否记忆 | — | ✅ | — | — |
| Intervention | `core/intervention.py` | Event | **恒为"不打扰"** | — | ✅ | — | ❌ 占位 |
| Idle Worker | `core/idle_worker.py` | 事件 + 候选 | 整理结果 | ✅ | 后台定时 | ❌ | ✅ |

### 2.3 后台设施

| 设施 | 默认 | 开关 |
|---|---|---|
| 沉淀后台任务（deposit） | ✅ 开启 | 无开关（V0.5.5 引入） |
| Idle Worker | ❌ **关闭** | `PAI_IDLE_REASONING=1` |

> `PAI_IDLE_REASONING` 默认 `"0"`（`core/config.py`）。
> **不得为测试永久打开默认值** —— 常驻后台循环是产品级决定。

---

## 3. 四层概念

这一节是整个项目最重要的部分。用人话讲清楚四层各自管什么。

### 3.1 Memory —— 记事实

**是什么**：用户说过的话、做过的观察，存下来。

**存什么**：
- `context_entries`：Personal Context，用户的原话和归纳出的理解
  （kind: `profile` 档案 / `fact` 事实 / `dynamic` 最近情况 / `interaction` 相处方式）
- `events`：事件记忆（观察到的场景 + 活动 + 物品 + 重要度）
- `chat_messages`：对话历史（带 `suppressed` 标记"别记整轮"）
- `facts`：从理解中提取的长期事实

**什么时候写**：每轮对话后**后台**由 `deposit()` 用模型抽取写入。
不是每句话都写——有防火墙（见 §5）。

**什么时候读**：每轮 `_assemble()` 时读，按 `sort_for_prompt()` 排序后进 prompt。

**哪些信息不会进入 Memory**：
- Runtime Time（时间永远不落库）
- 模型的回答本身（只有用户原话 + 模型抽取的结构化结果）
- 用户明确说"别记这个"的整轮（`suppressed` 标记，模型看不到原文）
- 无依据提问产生的内容（`_is_ungrounded_question` 拦截）
- 置信度低于 `quota_overflow` 的溢出内容（转成假设，不进 Context）

### 3.2 Understanding —— 猜规律

**是什么**：从重复行为里归纳出的**可运行、可修正的假设**。

**Hypothesis 是什么**：一条"用户可能怎样"的猜测，带置信度和证据计数。

**Evidence 是什么**：支撑或反驳这条猜测的**真实用户证据**。
来源白名单只有三种（`memory/hypothesis_store.py`）：
```
SOURCE_TYPES = ("context", "event", "correction")
```
**hypothesis 不在白名单里 —— 假设永远不能证明自己。**

**状态机（这是理解链的核心）**：
```
                 ┌──────────┐
   归纳产生 ────►│ tentative│  待验证，置信度 0.50 起
                 └────┬─────┘
      证据足够 + 独立源≥2
                      ▼
                 ┌──────────┐
     用户确认 ──►│confirmed │  正式理解，进 prompt 与 Judgment
                 └──────────┘

   用户否决 ──► rejected（置信度压到 0.25，不可逆）
```

**Correction 是什么**：用户亲口纠正某条理解。
`correct_entry()` **原地改写**（同一条 id 换内容），历史记进 `context_log`。

**Change 是什么**：用户说"我变了 / 改成…"。
`memory_status` 置 `outdated`，旧条目**不物理删除**，进 UI"有过变化"区。

**置信度数学**（`memory/hypothesis_store.py` 实测值）：
```
INIT_CONFIDENCE        = 0.50
SUPPORT_DELTA          = +0.08
CONTRADICT_DELTA       = -0.25
CORRECTION_SET         = 0.25    （用户亲口纠正：直接置位）
HIGH_CONF_CAP          = 0.70    （封顶）
MIN_INDEPENDENT_FOR_HIGH = 2     （独立源 < 2 不许超过上限）
```
> 注意 `add_evidence` 用的是 `min(conf, HIGH_CONF_CAP)` **不是减一**——
> 所以"置信度正好 0.70 但只有 1 个独立源"是可达的，必须单独测。

### 3.3 Background Reasoning —— 后台推测

**是什么**：用户不说话时，Core 在本地扫事件，找"是不是变了"。

**什么时候运行**：两条路径
1. 每轮沉淀末尾（`_deposit_body` 第 ⑥ 步）
2. Idle Worker 空闲整理（**默认关闭**，`PAI_IDLE_REASONING=1` 才开）

**会看什么**：事件序列（时间、slot 分布、主题占比）

**三条规则**（`core/background_reasoning.py`）：
- `behavior_change`：时段变了（占比移动 ≥ 50pp）
- `habit_drift`：已站住的理解在漂移（只读）
- `focus_shift`：关注点转移（≥3 次且占比 ≥50%）

**能产生什么**：**只有 `pending` 候选**（复用 `hypotheses` 表，不新建表）

**不能产生什么**：
- ❌ 不能自己毕业成 confirmed（毕业门槛：置信度 ≥0.70 **且** 独立源 ≥2）
- ❌ 不能直接进 prompt
- ❌ 不能影响 Judgment（`is_formal()` 显式拒绝 `background:` 前缀）
- ❌ 不能自我确认（用户点"对"只到 `accepted`，不是 confirmed）

**为什么不能直接进 prompt**：见 §5。

**生命周期**：
```
pending ──用户认可──► accepted ──证据足够+独立源≥2──► confirmed（换 scope 前缀）
   │                      │
   └──用户否定──► rejected  └──长期无支持──► expired
```

### 3.4 Judgment —— 这次怎么答

**是什么**：Adaptive Judgment。判断"**这一次**该怎么回应"。

> 在此之前，所有理解被原样塞进 prompt，让模型自己决定怎么解释。
> 现在改成：**先判断、再执行**。

**输出**（`core/judgment.py`，纯逻辑零 IO）：
```
Judgment(response_mode="direct" | "hint_first" | "ask_first",
         verbosity, context, reasons, applied, ignored)
```
- `response_mode` —— 怎么答（三种，硬上限）
- `verbosity` —— `concise` / `normal`
- `context` —— **他眼下在忙什么**（正式 active 目标 / 当前 task）
- `reasons` —— **为什么这么答**（产品语言，可展示给用户）
- `applied` / `ignored` —— 内部可追溯，**不对外**

> `context` 与 `reasons` 分开是刻意的：混在一起会产生一个具体的谎——
> reasons 里写"因为你在准备 CSP"，读起来像"我判断你要 CSP 才给提示"，
> 而判断根本没看这个。情境不是理由。

**如何利用正式理解**：`is_formal()` 是唯一闸门。
允许的输入：
```
confirmed hypothesis
confirmed pattern
active goal
interaction 条目 且 source_grade ∈ ("user_direct", "user_confirmed")
```
`context` 只收 `active goal`，**规律不进**（规律说"他是什么样的人"，
那是 reasons 的地盘）。

**优先级**（高 → 低）：
```
① 系统 / 安全硬约束      不在 Judgment 层——它只在上游挡。
                        Judgment 三个取值都只能收紧，不能放开（结构上）
② 当前用户明确指令        "还是不会，直接告诉我吧" → direct
③ 正式个人理解            已确认的 hint_first 偏好
④ 默认 direct
```

**什么会覆盖长期偏好**：**当前这句用户明确说的话**。
长期记忆是对用户的尊重，不是命令。

**什么绝对不会影响回答**：
```
tentative 假设
background pending 候选
background accepted 但未毕业
rejected / expired / deleted
quota_overflow: 前缀
AI 自己推测（inferred）
```

**怎么真的改变模型行为**：算出的 `response_mode` 经 `strategy_directive()`
转成一句执行指令进 system prompt：
```
【本次回应方式】先给提示或思路，不要直接给完整答案，留出他自己想的空间。
【他眼下在忙】长期准备 CSP 竞赛（回答可以贴着这个来，但别念出来）。
```
**模型本身没变，变的是它这次被要求怎么做。**

> 注意 active 目标**早就在** system prompt 里了（`=== 他的长期目标 ===` 段）。
> `context` 不是重复注入——它把目标绑到了**这一次怎么答**上。
> 断言 context 有效性时必须切出策略指令那一段去查，
> 否则在加 context 之前就成立、测试会假通过。

**不写记忆**：Judgment 是 `frozen=True` dataclass，无任何写入口。
新的理解仍然只能走 `conversation → Natural Evidence → Understanding`。

---

## 4. 核心数据结构

**15 张表 / 5 个 store**，全部在同一个 SQLite 文件 `data/personal_ai.db`。

| 表 | 是什么 | 为什么存在 | 谁写 | 谁读 | 影响回答? |
|---|---|---|---|---|---|
| `context_entries` | Personal Context 条目 | 用户的原话 + 归纳理解 | deposit / correct | `_assemble` | ✅ |
| `context_log` | Context 变更日志 | 历史可追溯，改写不丢 | correct / delete / restore | UI 历史区 | ❌ |
| `events` | 事件记忆 | 观察到的场景 | pipeline.observe | recaller | ✅ |
| `facts` | 长期事实 | 从理解提取 | understanding | — | 间接 |
| `hypotheses` | 假设/候选 | "用户可能怎样" | inducer / background | `list_for_prompt` / judgment | ⚠️ tentative 降权 |
| `hypothesis_evidence` | 假设的证据 | 支撑/反驳 + 原话 | add_evidence | detail / UI 依据 | ❌ |
| `hypothesis_log` | 假设状态变更 | 置信度前后值可追溯 | confirm/reject/verdict | 审计 | ❌ |
| `patterns` | 规律 | 多条假设升起来的更稳规律 | pattern_engine | `list_confirmed` | ✅ |
| `pattern_evidence` / `pattern_log` | 规律的证据/日志 | 同上 | 同上 | — | ❌ |
| `goals` | 目标 | 用户显式声明的长期目标 | goal.propose | `list_active` | ✅ 仅标题 |
| `goal_log` | 目标变更 | 可追溯 | goal.* | — | ❌ |
| `chat_messages` | 对话历史 | 跨轮连贯 | `_write_turn` | `history_for_prompt` | ✅ |
| `conversations` | 会话 | 跨轮 session | chat_store | 同上 | ✅ |
| `meta` | 键值元数据 | 服务状态缓存等 | 各处 | `/api/status` | ❌ |

**没有为"时间"建表** —— Runtime Time 是每轮现读的，不落库。

### 关系图

```
用户表达
   ↓
context_entries  ←──── deposit() 抽取
   │                    ↑
   │              （模型看用户原话 + 已有列表）
   ↓
hypotheses       ←──── inducer.induce_all()（R1 活动重复 / R2 主题共享）
   │                    ↑
   │              从 context_entries 归纳
   ↓
hypothesis_evidence  ←── add_evidence()
   │                    来源白名单：context / event / correction
   │                    （**hypothesis 不能证明自己**）
   ↓
hypothesis_log    ←── confirm / reject / verdict（置信度前后值留痕）
   ↓
confirmed 理解
   ↓
Judgment.judge()  ←── 唯一闸门 is_formal()
   ↓
策略指令进 prompt
```

旁路（不参与主链）：
```
events ──background_reasoning──► pending 候选（同一个 hypotheses 表）
                                   ↓ 用户认可
                                accepted
                                   ↓ 证据够 + 独立源≥2
                     换 scope 前缀 background: → inferred:
                                confirmed
```

---

## 5. Trust Boundaries 信任边界

**一句话：AI 的猜测不能改变 AI 对用户的正式行为策略。**

### 5.1 什么能进正式理解

| 来源 | 能进 | 依据 |
|---|---|---|
| 用户明确说的话（`user_direct`） | ✅ | `source_grade` 白名单 |
| 用户亲口纠正（`correction`） | ✅ | `SOURCE_TYPES` |
| 用户显式声明的目标 | ✅ | 用户主动发起 |
| confirmed hypothesis / pattern | ✅ | `is_formal()` |
| 模型推测（`inferred`） | ❌ | 不是用户说的 |
| AI 自己产生的推理 | ❌ | 永远不行 |

### 5.2 什么只能是 candidate

```
background pending            → 用户还没裁决
background accepted 未毕业    → 用户认可了，但证据还不够
```

**为什么不能直接进 prompt**：如果"用户点了认可"就能指挥行为，
等于让后台推测**借用户的手**获得权力。用户认可的是"值得观察"，
不是"这就是事实"。

### 5.3 什么不能进 prompt

| 不能进 | 原因 |
|---|---|
| background 候选（任何状态） | 未毕业的理解不该指挥回答 |
| `quota_overflow:` 前缀 | V0.5.3 定义为"不可验证材料" |
| rejected / expired / deleted | 已被否决或失效 |
| 内部字段（confidence / scope / source_type / id） | 展示层泄漏，产品会变成数据库查询工具 |

### 5.4 什么不能成为证据

```
SOURCE_TYPES = ("context", "event", "correction")
```
**hypothesis 不在白名单** —— 假设永远不能证明自己。
（`add_evidence` 显式报错："不允许的证据来源 X（假设不能证明自己）"）

### 5.5 什么不能自我确认

| 机制 | 说明 |
|---|---|
| 毕业需双条件 | 置信度 ≥ `HIGH_CONF_CAP`(0.70) **且** 独立源 ≥ `MIN_INDEPENDENT_FOR_HIGH`(2) |
| 用户点"对" ≠ confirmed | 只到 `accepted`，需要继续积累跨天证据 |
| `add_evidence` 用 `min()` 不是减一 | 所以"conf 0.70 + 1 个源"可达，必须单独测 |

### 5.6 什么需要用户确认

- 假设 → confirmed（`/api/hypotheses/{id}/confirm`）
- 背景候选的 correct/wrong（`/api/background/{id}/verdict`）
- 规律 → confirmed
- 目标 → confirmed
- Beta 体验码

### 5.7 删除 vs 失活

| 操作 | 行为 | 历史 |
|---|---|---|
| `DELETE /api/hypotheses/{id}` | `delete_entry()` 失效 | 保留 |
| Context 条目 delete | 物理删除 | 进 `context_log` |
| Context memory_status → outdated | 失活不删 | 变更日志 + UI"有过变化" |
| 候选 expired | 只改 status 不删行 | 保留 |

---

## 6. Runtime Context 与 Memory 的区别

这一层最容易搞混，所以单独立一节。

| | **Runtime Context** | **Memory** | **Understanding** |
|---|---|---|---|
| 时间跨度 | **现在** | **过去** | **长期** |
| 内容 | 当前时间、时区、ISO 8601 | 用户说过的话、做过的事 | 关于这个人的稳定假设 |
| 来源 | 操作系统时钟 | 数据库 | 归纳 + 证据 |
| 会不会过期 | **每轮都变** | 不主动过期 | 有淘汰机制 |
| 会不会落库 | ❌ **永不** | ✅ | ✅ |
| 模型能改吗 | ❌ **不能** | ✅ 用户能改 | ⚠️ 需证据 |
| 举例 | "现在是 2026年10月3日 星期五 15:30" | "用户昨天说过想先要提示" | "这类题用户更希望先自己想一想" |

**为什么时间必须每轮实时生成**：缓存一次，之后每轮都在说错时间。
`TimeProvider` 接受注入 clock，生产用系统时钟，测试用假时钟。

**为什么模型改不了它**：时间块放在 system 提示词**最前面**并写明权威性。
用户说"现在是1999年"、记忆里写旧日期、模型上一轮说错时间 —— 都改不动它。
它不在模型的输出通路里。

**时间不进入 Memory 的结构性保证**：沉淀走的是自己的 `DEPOSIT_SYSTEM`
+ 用户原文，**根本看不到聊天提示词**。不是"被过滤掉"，是到不了。

**模型 / Provider 是同一套**：所有 provider 都经过 `_assemble()`，
所以换模型不会丢时间。

---

## 7. 产品层

用户实际能看到的界面（`interfaces/static/index.html`，单文件 92KB，零外部依赖）。

| 面板 | 用户看到 | 背后 | 连到 Core 哪里 |
|---|---|---|---|
| **首页** | 潮汐动画 + 时间太阳 + 问候 + 输入框 | 无 | — |
| **聊天区** | 消息气泡 + **处理摘要**（可折叠） | `stream_chat` → `_assemble` | conversation |
| **AI 记忆** | 用户原话卡片，可改/删 | `/api/context` | context_store |
| **回忆库** | 检索历史 + 依据 | `/api/memories` | MemoryStore |
| **我眼中的你** | 正式理解时间线 + "为什么这么认为" | `/api/understanding` | understanding_view |
| **我正在思考** | 后台推测（低强调虚线） | `/api/background` | background_bridge |
| **待验证** | **卡片化候选** + 依据折叠 + 对/不对 | `/api/candidates` | candidate_view |
| **过去 7 天** | 只读周报 | `/api/reflection/weekly` | reflection |
| **模型设置** | provider / base_url / model / key | `/api/model/*` | models/factory |
| **数据管理** | 导出 / 备份 / 恢复 / 导入 | `/api/data/*` | data_manager |
| **Beta 体验码** | 本地输码解锁预设 | `/api/beta/*` | core/beta |

### 三个"理解"面板的视觉区分

| 面板 | 定位 | 视觉 |
|---|---|---|
| 我眼中的你 | 正式理解 | 实心卡片，信息感 |
| 我正在思考 | 后台猜测 | 虚线边框，低强调 |
| 待验证 | **需要用户动手** | 实线 + 更强主标题 |

### 产品面泄漏防护

- `/api/understanding` `/api/candidates` `/api/background` → 产品语言投影
- `/api/hypotheses` → **诊断面**（含 confidence/scope/source_type，保留给检查系统的人）
- 每次返回前 `assert_no_internal_fields()` 自检，泄漏即记 ERROR
- `scripts/scan_ui_leak.py` 三处扫描（禁用清单 / 前端渲染 / 端点实际返回）

---

## 8. Provider / Model 架构

```
core 永远只持有 ModelRegistry（门面）
   ↓ settings_store.load()
   ├─ provider == "openai"  → OpenAICompatibleProvider(base_url, model, api_key)
   ├─ 有 ollama 配置        → OllamaAdapter(url, model)
   └─ 配置无效              → _BrokenModel（占位，让 App 起不来时显式报错）
```

| Provider | 类 | 流式 | 特点 |
|---|---|---|---|
| Ollama | `models/ollama_adapter.py` | ✅ `chat_stream_async` | 本地；**必须带 `"think": false` + `num_predict: 512`** |
| OpenAI-compatible | `models/openai_provider.py` | ✅ `chat_stream_async` | 云端（智谱等） |
| Mock | `models/mock_adapter.py` | — | 测试用 |
| Broken | `models/factory.py` | — | 配置无效占位 |

**`think: false` 不可删**：Gemma-4 带 thinking 阶段，思考会吃光 token 预算导致
**回答为空 / JSON 被截断**。见 §11 事故 #3。

---

## 9. 测试体系

**723 用例 / 639 个测试函数**，分五层，各解决不同问题。

| 层 | 数量 | 解决什么问题 |
|---|---|---|
| **unit** | 多数 | 单个函数对不对（存储层数学、解析、投影） |
| **integration** | `test_hypothesis_chain` / `test_first_run` / `test_pattern_flow` | 多个模块串起来通不通 |
| **acceptance** | 6 个脚本 | 产品级验收（起服务、走 API、验 UI 存在） |
| **mutation** | 4 个脚本 / **55 个变异** | **测试到底守没守住** |
| **replay / soak** | `soak_v0510.py` | 长时间连续跑会不会烂 |
| **real-model** | `test_feedback_reale2e.py` | 真模型链路（需 `--deselect`，本机 Ollama 未跑） |

### 为什么需要 mutation

**"全绿"不等于"测试够"。** 一个测试如果断言的是被测代码没覆盖的分支，
它永远绿，但什么都没守住。

做法：人为破坏一处保护条件 → 跑测试 → **必须变红**。绿了就说明那条测试是摆设。

本项目现存 55 个变异，全部 KILLED：

| 脚本 | 数量 | 守护什么 |
|---|---|---|
| `mutation_natural.py` | 21 | 自然证据、背景生命周期、双层守卫 |
| `mutation_judgment.py` | 17 | 三条安全闸门、否定语义、接线、context 链路 |
| `mutation_runtime_time.py` | 11 | 时间不泄漏/不缓存/时区/相对时间确定性 |
| `mutation_candidates.py` | 12 | 卡片不泄漏/不编造/不越权 |

### 为什么真实 replay 重要

V0.5.10 的 soak（95 分钟连续跑）抓到了 5 个单测看不到的真 bug，
其中两个是"看起来在工作、其实没工作"型。详见 §11。

### 为什么 soak 能发现单测看不到的问题

单测每次都是干净起步。soak 会遇到：跨轮状态残留、后台任务堆积、
长期不动作后的淘汰逻辑、时间漂移、缓存不刷新。**这些都是"跑得够久才出现"的。**

---

## 10. 版本历史

> 只写"解决了什么问题 / 为什么做 / 发生过什么失败 / 留下什么"。

### V0.1（`b4e3094`，2026-09-26）
- **解决**：感知→理解→事件→记忆→检索→回忆 的最小闭环 + Web UI
- **留下**：72 测试；`core/understanding.py` 的 JSON 容错解析；`core/policy.py` 阈值
- **留下的坑**：`agent/` 只有只读工具注册表，`intervention.py` 是占位

### V0.2（`3e74d44`→`d77c93f`→`a163583`，2026-09-26）
- **解决**：Personal Context 升为一等对象 + 对话读写闭环 + 反馈修正/删除/暂停
- **为什么**：D007 定下"Context 必须改变 AI 后续判断且**可测试**"
- **留下的坑**：关 `think:false` 根治空回答

### Desktop 五阶段（`1af668c`→`560521d`，2026-09-26）
- Provider 架构 → 数据导出/备份/恢复 → Electron 壳 → PyInstaller 打包 → 验收
- **留下的坑**：`build.win.signAndEditExecutable:false` 绕 winCodeSign 特权问题

### V0.3（`5faf80d`→`80773a4`，2026-09-27）
- **解决**：假设（hypothesis）体系 —— 置信度数学 + 规则归纳 R1/R2 + API + Trust UI
- **做了什么**：扫 bug 专场，三路并行审查 30 疑点 → 实锤 20 个修掉
- **留下的原则**：双层守卫必须各自直接测到（D/K/L 各撞一次）

### V0.4（`170ac5f`→`5042f90`，2026-09-29）
- **解决**：验证提问"搭便车不拦路" —— 选择规则 + pending 单例 + 回答分类

### V0.5（`3961265`→`796863f`，2026-09-29~30）
- **解决**：Pattern Engine（规律）+ Goal + Reflection 只读周报
- **为什么**：假设层之上还需要"更稳的规律"和"长期目标"

### V0.5.1 ~ V0.5.4（2026-09-30）
- **V0.5.1** 可靠性补丁：规则兜底纠正、无依据提问门、数字守卫
- **V0.5.2** Trust Boundary 还债版：写入防火墙逐子句分类 + 纠正升级三态
- **V0.5.3** 值得信任地记忆：别记整轮隐藏 + 配额溢出转假设
- **V0.5.4** event 统一防火墙 + 验证链调查 → **唯一对外发布版本**

### UI v3 / v5（`0163a6c` / `bb93bb7`，2026-09-30~10-01）
- 潮汐空间（冷青底 + 时间太阳 + 日晷签）→ 左栏 + 中央区 + 钉底 Composer
- **根治**了横向溢出：`.tide-hero` 负 margin 恒定撑宽 22px

### V0.5.5（`e2c662f`，2026-10-01）
- **性能两阶段**：流式三async 化 + 沉淀后台化
- **失败**：V0.5.5 尝试过继续推进后**主动停止**，记录在 `docs/decisions/D021`
- **留下的坑**：`stream_chat` 里 `asyncio.run` 会抛"cannot be called from a running event loop"
  并被 `except: pass` 吞掉，防竞态是假的

### V0.5.6 ~ V0.5.10（`467ebf1`→`0e5238c`，2026-10-02）
| 版本 | 解决 |
|---|---|
| V0.5.6 | Natural Evidence + Change（用户自己说的话成为证据） |
| V0.5.7 | 第一次被理解（`/api/understanding` 把理解让人看见） |
| V0.5.8 | Background Reasoning（能发现，但只能提不能用） |
| V0.5.9 | 候选生命周期（会长大、会死、毕业靠换 scope 前缀） |
| V0.5.10 | Reality / Soak —— 95 分钟连续跑，抓 5 个真 bug |

### 三个里程碑（2026-10-02~03，均未发版）
| commit | 内容 |
|---|---|
| `ed6563f` | Adaptive Judgment：Understanding → Judgment |
| `00a86fe` | Runtime Time Context |
| `410ea0c` | UI 体验升级：处理摘要 + 待验证卡片化 |

---

## 11. 真实事故档案

这一章不是为了纪念 bug，是为了**以后不要重犯**。

### #1 `_source_exists` 让证据静默丢失
- **问题**：证据写不进去，`get_detail()` 返回空 evidence，UI 依据区空白
- **为什么**：证据来源 id 必须在表里真实存在，`_source_exists` 校验
- **怎么发现**：测试喂了自己造的 id，全绿；真跑一次才发现
- **怎么修**：测试改用真 store + 真 context 条目
- **原则**：**单测构造的数据形状和真实形状不同时，单测是假的**

### #2 时间字段名不一致（踩了两次）
- **问题**：假设卡"形成时间"和 Context 引语时间全空
- **为什么**：`hypothesis` 给 `created_at`，`get_detail` 也给 `created_at`，
  不是 `created_at_iso`；单测喂了自己造的 dict
- **怎么发现**：V0.5.7 修过一次，V0.5.10 写 `candidate_view` 时**又踩了一次**
- **原则**：统一走 `_when_of()`；新模块必须用真 store 对象写测试

### #3 `think: true` 吃光 token → 空回答
- **问题**：Gemma-4 的 thinking 阶段把 token 预算吃光，回答空 / JSON 截断
- **怎么修**：`ollama_adapter` 强制 `"think": false` + `num_predict: 512`
- **原则**：**别删这两个参数**

### #4 status 健康检查 90 秒超时（真凶在别处）
- **问题**：桌面壳报"Core 90 秒未通过健康检查"
- **真因**：`/api/status` 内嵌同步 `ping()`（2s），壳子探测线恰是 2000ms
  —— Core 活得好好的，壳子每次都在 status 返回前 50ms 放弃
- **原则**：**status 永远不准做慢同步调用** —— 它是健康探针

### #5 `asyncio.run` 被 `except: pass` 吞掉
- **问题**：防竞态逻辑看着在跑，其实从来没执行
- **为什么**：`stream_chat` 在 event loop 里调 `asyncio.run()`，
  必抛 "cannot be called from a running event loop"，被吞掉
- **原则**：**看起来在工作 ≠ 真的在工作**

### #6 一句话同时既认可又否认同一条候选
- **问题**：conf 0.53 → 0.28（支持 +0.08 和反证 -0.25 同时生效）
- **怎么发现**：soak 实测，不是单测
- **怎么修**：本回合刚被裁决的候选从本回合证据对象里剔除

### #7 连点"正确"把置信度抬到毕业线
- **问题**：用户连点 4 次，conf 0.40 → 0.80，越过 0.70 直接毕业
- **为什么**：裁决按候选自己声明的类型读，而 accepted 候选每次点击都加证据
- **原则**：用户认可 ≠ 毕业（V0.5.9）

### #8 候选泄漏到 prompt
- **问题**：background 候选进了回答
- **为什么**：7 处安全过滤只认 scope 前缀
- **怎么修**：毕业靠**换前缀** `background:` → `inferred:`
- **原则**：**别到处加 background 例外**

### #9 quota 绕过
- **问题**：配额溢出内容进了正式理解
- **怎么修**：`quota_overflow:` 前缀 + 四不准

### #10 测试自己被重构误删
- **问题**：mutation O 存活，暴露 `test_graduation_needs_two_independent_days`
  这个测试在某次重构里被删了
- **原则**：**mutation 存活 = 有测试被误删或从来没在守**

### #11 定时炸弹测试（真实发生）
- **问题**：4 个测试硬编码 `2026-10-01` 断言"昨天"，却没注入 `now`
- **为什么**：`build_understanding(hypo, now)` **收了 `now` 却没往下传**，
  最终落到真时钟
- **怎么发现**：会话中途日期从 10/02 翻到 10/03，测试突然红，报错是
  "2 天前 != 昨天"，跟代码改动毫无关系
- **原则**：**凡是断言"今天/昨天/N 天前"的测试必须注入 now**（已有 AST 守卫）

### #12 否定读反（方向性 bug）
- **问题**：「别直接给我答案，先给我一个提示」被读成 `direct`
- **为什么**：正向关键词匹配，"给我答案"命中了
- **原则**：**方向读反比读不出来更糟** —— 读不出来只是没生效，
  读反是每次都做用户明确不要的事

### #13 陈旧逻辑残留（单测抓不到）
- **问题**：待验证卡片化后徽章恒为 0，而主列表还有内容
- **为什么**：文件末尾"生命感"包装器还在数旧类名 `.mem` 并覆盖徽章
- **怎么发现**：**浏览器实测**（单元测试和源码扫描都绿）
- **原则**：**改数据结构必须搜旧类名的残留引用**

### #14 实测才发现的空字段
- **问题**：候选卡"今天"一栏永远空
- **为什么**：`Hypothesis` 字段叫 `created_at` 不叫 `created_at_iso`
- **怎么发现**：浏览器实测
- **原则**：见 #1、#2

### #15 WAL 固定开销（性能陷阱）
- **问题**：每个 store 的 `_connect()` 每次连接都执行
  `PRAGMA journal_mode=WAL`，实测固定 **7.7ms/次**，与表大小无关
- **影响**：全应用每轮十几次查询都在付这笔
- **现状**：**未修**（超出各次里程碑范围），记在 §12 已知限制

### #16 双层守卫（通用教训）
- **问题**：同一保护在两层都有时，删掉任一层另一层会兜住 ⇒ mutation 存活
- **发生**：V0.5.6 D / V0.5.8 L / V0.5.9 K 各撞一次
- **原则**：**必须把两层各自直接测到**；若某层跑不到（死代码），
  要么删要么写清"这是纵深防御不是生效闸门"

---

## 12. 已知限制

### 技术限制

| 限制 | 说明 |
|---|---|
| **WAL 固定开销 7.7ms/次** | 每个 store 每次 `_connect()` 都执行 `PRAGMA journal_mode=WAL`。全应用每轮十几次。**建议**：把 WAL 设置移到 store 初始化时做一次。**未修** |
| **`tzdata` 是传递依赖** | `zoneinfo` 在 Windows 上通常靠它兜底，但它**没写进 requirements**。已做降级：固定偏移（`UTC+8`）永远可用，IANA 名解析不了就**明确报错**而非静默给错时间 |
| **`perception/camera.py` 只服务摄像头** | ✅ **在运行链路**（`webapp.py` 有 `/api/camera/snap`）。但 Win11 隐私开关默认挡摄像头，D005 记录了降级到上传图片 |
| **`agent/tools.py` 不在运行链路** | 只被 `smoke.py` 引用 |
| **`intervention.py` 是占位** | 恒返回"不打扰"。PRODUCT_TRUTH 里的"介入"循环**未实现** |
| 本机 Ollama 未运行 | `test_feedback_reale2e.py` 需 `--deselect` |
| 单用户单实例 | `data/personal_ai.db` 单文件，无多用户隔离 |

### 版本限制

- **V0.5.4 之后全部未推送**（43 个 commit）
- `config.py` 0.5.4 vs `desktop/package.json` 0.5.5 不一致

### 视觉限制

- **320px 有 67px 横向溢出**，根因是顶栏 `.modeltag`（模型徽章）—— 既有问题，
  本次 UI 升级**未碰顶栏**
- 截图需要可见桌面窗口，自动化环境只能做 DOM 实测

---

## 13. 真实存在 vs 未实现

### A. 真实存在（代码可指）

✅ 聊天（同步 + 流式）✅ 事件记忆 ✅ Personal Context ✅ 假设 + 置信度数学
✅ 自然证据 ✅ 变化检测 ✅ 验证提问 ✅ Pattern Engine ✅ Goal ✅ 只读周报
✅ Background Reasoning ✅ 候选生命周期 ✅ Adaptive Judgment ✅ Runtime Time
✅ 时间线投影 ✅ 待验证卡片 ✅ 处理摘要 ✅ Provider 架构 ✅ 数据导出/备份/恢复
✅ Electron 桌面壳 ✅ 安装包 ✅ Beta 体验码 ✅ 暂停/删除/隐私端点

### B. 曾经实现后被替代

| 东西 | 现状 |
|---|---|
| `POST /api/chat` | **仍存在但是遗留路径**（走 `recaller.recall`，不带 Understanding/Judgment）。UI 用的是 `/api/conversation` |
| `docs/architecture.md` | **Superseded** —— V0.1 版本，写着"模式记忆 ⬜ 留空"，已完全过时 |
| `.tide-hero` 负 margin 溢出 | 已修（UI v5） |

### C. 只有设计 / 占位

| 东西 | 真实状态 |
|---|---|
| **Agent** | ❌ **没有 Agent**。`agent/tools.py` 原文：*"V0.1 只提供「查记忆」类**只读工具**；未来 Agent 编排在这里扩展，不动核心数据结构。"* 可达性实测：该模块**只被 `scripts/smoke.py` 引用，不在运行链路**。无任务队列、无 ReAct 循环、无 LLM tool-calling |
| **Intervention / 主动介入** | ❌ **占位**。`core/intervention.py` 原文：*"V0.1：不实现自动介入（永远沉默），但管线每产生一个事件都会调用 decide()"*，`decide()` 恒返回 `intervene=False` |
| **Personality** | ❌ 未实现，**全仓库无相关代码**（grep 无命中） |
| **Forecast / Prediction（预测）** | ❌ 未实现，**全仓库无相关代码**。且被里程碑约束**明确排除**过 |
| **多用户 / 多设备** | ❌ 未实现。PRODUCT_TRUTH 提到"同一 AI 的不同身体"，但只做了 PC |
| **语音 / ASR** | ❌ 未实现。`architecture.md` 列为"未来扩展" |
| **向量检索 / embeddings** | ❌ 未实现。检索是时间词 + 关键词 + 级联 |
| **Web 搜索 / 浏览器** | ❌ 未实现 |

### D. 曾经尝试后停止

| 东西 | 结果 |
|---|---|
| V0.5.5 之后的推进 | 主动停止，记录在 `docs/decisions/D021-v055-attempt-and-stop.md` |

### E. 当前计划但未实现

- WAL 优化
- `tzdata` 显式声明
- 顶栏 320px 溢出
- 多轮 replay 自动化（目前靠手工）

### F. 明确决定不做

- Forecast / Prediction Engine
- 自动生活管理 / 自动干预 / 自动提醒（V0.5.10 明确排除）
- 云端后台推理
- 新 Memory Layer / 新数据库

---

## 14. 30 分钟重新上手路线

> 假设你三个月没碰这个项目，今天重新打开仓库。

### 0-5 分钟：先看地图
```
docs/PROJECT_SUMMARY.md      ← 3 分钟版，先建立整体印象
docs/PROJECT_MAP.md          ← 本文件，完整地图
docs/PRODUCT_TRUTH.md        ← 产品北极星，"为什么做这个"
```

### 5-15 分钟：跑起来 + 看真实链路
```bash
python run.py                     # 启动（127.0.0.1:8000）
python -m pytest tests -q         # 全量测试，约 7 分钟
python -m pytest tests/test_judgment.py -q   # 只看一条链，快
```
打开 UI 后**按这个顺序点**，能亲眼看到整条链：
1. 聊天区发一句「这道题怎么做？」→ 看回答下方的**处理摘要**（Judgment 生效）
2. 点「我眼中的你」→ 正式理解 + "为什么这么认为"
3. 点「我正在思考」→ 后台候选（低强调虚线）
4. 点「待验证」→ 卡片 + 展开依据 + 点「对/不对」

### 15-20 分钟：理解四层
```
core/judgment.py         第四层 Judgment，449 行，纯逻辑，最好读
core/runtime_time.py     Runtime Context，268 行，纯逻辑
core/understanding_view.py  产品语言投影（含泄漏自检）
memory/hypothesis_store.py  假设 + 置信度数学 + 生命周期（1383 行，最核心）
```
**先读 `core/judgment.py`** —— 它最短、纯逻辑、注释解释了整条信任链。

### 20-25 分钟：看真实调用链
```
core/conversation.py :: _assemble()      提示词怎么组的（同步/流式共用）
core/conversation.py :: _deposit_body()  沉淀链的 8 步顺序
```

### 25-30 分钟：知道边界在哪
```
core/intervention.py    ← 占位，看它为什么是占位
agent/tools.py          ← "Agent 不存在"的证据
docs/decisions/D027-adaptive-judgment.md   ← 最近一次完整设计推导
```

### 如果你忘了某件事，先看哪个文件

| 忘了什么 | 看 |
|---|---|
| 这项目到底能干什么 | `docs/PROJECT_SUMMARY.md` |
| 某个模块为什么这么设计 | `docs/decisions/D0xx-*.md`（27 篇，按时间序） |
| 数据存在哪、什么结构 | 本文件 §4 |
| 什么能进 prompt | 本文件 §5 |
| 时间从哪来 | `core/runtime_time.py` |
| 回答怎么被改变的 | `core/judgment.py` |
| 沉淀怎么发生 | `core/deposit.py` |
| UI 怎么渲染 | `interfaces/static/index.html`（单文件） |
| 出过什么 bug | 本文件 §11 |
| 怎么跑测试 | 本文件 §9 |
| V0.5.4 怎么发布 | `docs/ACCEPTANCE_desktop.md` |

---

## 维护规则

1. **改了架构就改这个文件**，别让地图过期
2. 新功能先问"它属于哪一层"，写进对应小节
3. 事故写进 §11，包含"留下了什么原则"
4. 讨论过没做的，写进 §13 C/D，**不要写进 §13 A**
5. 与代码冲突，**以代码为准**，并修正本文档