# D024 — Background Reasoning：后台能发现，但只能提，不能用

日期：2026-10-03
基线：`50779ba`（V0.5.7 第一次被理解）
范围：`core/background_reasoning.py`（纯逻辑）+ `core/background_bridge.py`（接线）
**零 schema 变更、零新依赖、零定时器。**

---

## 0. 这个版本加的是什么能力

之前用户不改口，系统永远不知道他变了——`natural_bridge` 的变化信号
只在他**说出**"我改成早上了"时才触发。如果他只是行为变了、没说，
系统完全看不见。

现在：系统会自己扫事件日志，**发现"好像有什么变了"，然后把这句话
摆到用户面前等他判断**。

关键在最后四个字——**等他判断**。

---

## 1. 不新建表：照抄 `quota_overflow:` 范式

`reasoning_type` 没有新字段。三处复用：

| 位置 | 用法 | 先例 |
|---|---|---|
| `scope` 前缀 | `background:{rule}:{topic}` | `activity:` `interaction:` `inferred:` `quota_overflow:` |
| `log.detail` | `{"rule":"background_reasoning","type":...,"reason":...,"evidence":[...]}` | `{"rule":"firewall"}` `{"rule":"promote"}` |
| `evidence.detail` | `{"channel":"background","signal":"user_correct"}` | `chat` `natural` `promotion` `trust_ui` |

`quota_overflow:` 是现成的"存但不生效"模板——它在 **6 个地方**被排除。
后台推测照抄同一套，加了第 7~10 处：

| 位置 | 作用 |
|---|---|
| `hypothesis_store.list_for_prompt()` | **不进 prompt** |
| `verifier.select_candidate()` | 不发起验证提问 |
| `pattern_engine.promote_all()` | 不参与 pattern 生成 |
| `natural_bridge._eligible_hypotheses()` | 不吃自然证据 |
| `hypothesis_store._natural_eligible()` | 存储层守卫 |
| `understanding_view.topic_name()` | 不出现在「我眼中的你」 |

> **第 10 条是测试逼出来的。** 加上后台推理之后，
> `test_first_run.py::test_three_chats_form_an_understanding` 直接红了——
> 后台候选（时间最新、无 evidence）排到了时间线第一位，混进了
> 「我眼中的你」。
>
> 那是**把猜测伪装成理解**。产品铁律是这两个面板必须分开：
> 「我眼中的你」= 用户确认过的；「我正在思考」= AI 猜的。

---

## 2. 猜测的置信度上限 0.45（刻意低于 INIT_CONFIDENCE）

`_shift_strength()` 封顶 0.45，而 `INIT_CONFIDENCE = 0.50`。

这不是随手取的数：**即便将来有代码误把候选当普通假设处理，
它的置信度也天生低于"新假设"的起跑线**，翻不出浪。

配套的一处修正：`HypothesisStore.create()` 原本硬编码 `INIT_CONFIDENCE`，
导致候选自称 0.33、库里却写 0.50——**显示得比实际更可信**。
现在 `create()` 接受可选 `confidence`，不改任何既有调用方的行为。

---

## 3. 三条规则的阈值都偏保守

| 常量 | 值 | 为什么 |
|---|---|---|
| `MIN_EVENTS_PER_WINDOW` | 3 | 两个窗口各少于 3 次就不推 |
| `MIN_RATIO_SHIFT` | 0.50 | 占比要移动 50 个百分点才算"变化" |
| `MIN_SLOT_EVENTS` | 2 | 某个时段本身至少 2 次，否则是噪声 |
| `MIN_DRIFT_CONFIDENCE` | 0.58 | 旧理解自己没站稳，不许拿它推漂移 |

时段识别（`slot_of`）**认不出就返回空串，绝不硬凑**——后台推理最容易
犯的错就是从三条记录里编一个故事。

窗口是 14 天对 7 天，中间那段不算：正在过渡的时期推不出趋势。

---

## 4. 用户裁决的两个分支，语义完全不同

| 用户说 | 发生什么 | 为什么 |
|---|---|---|
| **正确** | **只补一条 support 证据**，`status` 保持 `tentative` | 用户点一下"对"，不构成系统能拿它当事实的证据量 |
| **不对** | `reject()` → `rejected` → `background_proposed()` 命中 → **从此不再提** | 说错了就别再烦 |

「正确不升级 confirmed」是刻意的。第一版如果让"用户说对"就变成
confirmed，那么后台推理就有了自我确认的能力——它提出猜测、
用户随手点一下、猜测变成事实、然后进入 prompt。整条安全链就废了。

去重守卫有**两层**（bridge 记原因 + store 真拦），mutation 里分开测
（L / L2）。早先只有 bridge 层测试时，删掉 store 那道仍然全绿——
和 V0.5.6 的 D 变异一模一样的坑。

---

## 5. 接入点：promote 之后，不进定时器

```
_deposit_body:
  promote_all()          ← 先跑完
  run_background_reasoning()   ← 新增，在它之后
  declare_from_user() / goal.propose()
```

放 promote 之后：候选恒为 tentative，本来也进不了本轮 promote 的
`confirmed` 分组——就算顺序放前面也安全，但放后面语义更干净
（它只看已有的东西，不参与本轮任何判断）。

**没有定时器。** 沿用"用户消息后触发"模型——`acceptance_v05` 第 10 步
正在断言这五个文件里不许出现 `threading.Timer` / `schedule`，
加了就会红。

---

## 6. 踩到的两个真 bug

### ① `Event` 没有 `created_at_epoch`

`MemoryStore.list_events()` 返回的 `Event` 是 pydantic 模型，
时间字段是 `created_at`（datetime）。初版 `_epoch()` 只认
`created_at_epoch`（dict 形状）→ **真实事件一个都进不了窗口**，
规则"安静地什么都不产出"。

看起来像"后台推理没发现"，实际是根本没在工作。
现在 `_epoch()` 两种形状都认，并补了用真 `Event` 的测试。

### ② `record_background_verdict` 的 wrong 分支漏了守卫

scope 检查原本只写在 `correct` 分支里，`wrong` 分支直接甩给
`reject()`——**等于给这个端点开了一把能否决任意假设的钥匙**。
现在守卫提到分支之前。mutation M 专门钉这一条。

---

## 7. 断言过期不是回归

`acceptance_v03.py` 有三处 `total != N` 的断言（库里假设总数）。
后台推理会产生候选，总数自然变了。

改法**不是删断言也不是放宽阈值**，而是按这一步的**本意**重写：
「被否决的那条要移出列表」断言的是"这条不在列表里"，
而不是"库里总共只有 N 条"。

> 这类"被自己的旧决策反咬"在本项目反复出现（表单接入、邮箱移除、
> V0.5.5 的 quota 断言……）。判断口径：**断言在表达意图还是表达数字**。
> 表达数字的一律随产品演进更新，表达意图的不能动。

---

## 附：验收

| 标准 | 结果 |
|---|---|
| 真实调用链 | 通过（`_deposit_body` 内，与 promote 同链） |
| 数据库是否变化 | **否**（`create()` 只加了一个可选参数，默认行为不变） |
| 测试数量 | **538 通过 / 0 失败 / 0 错误 / 1 跳过**（基线 498，新增 40） |
| mutation | **15/15 KILLED**（新增 I/J/K/L/L2/M 六个打后台链） |
| 用户故事 | 通过（晚上学习 → 连续早起 → 提出推测 → **旧理解一字未动**） |
| 定时器 / 依赖 / schema | 零 / 零 / 零 |
| 发布 | **未发布、未部署、未打 tag、未改官网** |