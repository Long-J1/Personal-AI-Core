# D026 — Reality / Soak：长时间运行暴露的四个真实缺陷

日期：2026-10-03
基线：`5fba2dd`（V0.5.9）
范围：soak / replay / e2e 验证 + **只修被它们暴露的问题**
**零 schema、零依赖、零新能力。**

---

## 0. 这次不做什么

V0.5.10 不是功能版本。跑 soak、修 soak 撞出来的问题、记录数据。
没找到的问题一律不动——"觉得未来可能有问题"不是本次的修 bug 依据。

---

## 1. soak 撞出来的四个真 bug

### ① 候选只能在 API 上裁决，对话里说了不算（能力缺口）

**现象**：soak 故事第 E 阶段走不通。V0.5.9 把生命周期做完了，
但入口只有 `POST /api/background/{id}/verdict`。用户在聊天里说
「对，我最近已经改成早上学习了。」——系统**听见了这句话**，
却不把它当成对某条候选的裁决。

这不是 bug 是缺口，但任务书第五节要求 E 阶段走正常 conversation，
所以补了一条最小通道：`natural_bridge.resolve_pending_candidate()`。

**难点一：语义方向容易搞反。**
分类器里"变化"优先于"支持"，所以「对，我改成早上了」被判成 `change`。
可这条消息对的候选恰恰是 `behavior_change` / `habit_drift`——
用户说的正是"确实变了"，那是**印证**。一刀切当否定会把正确的猜测杀掉。

修法：裁决**按候选自己声明的类型读**。

```
deny                              → wrong
support                           → correct
change + behavior_change/habit_drift → correct   ← 印证变化类猜测
change + focus_shift              → 不动         ← 与"主要精力在哪"无关
其他                              → 不动
```

**难点二：门槛不能写死 2。**
`related_gram_count` 对 `background:behavior_change:学习` 只有 1 个共同
gram（标签就俩字），写死 `max_grams=2` 的话候选**永远裁决不了**。
改用自适应的 `required_grams()`。

---

### ② `match_label` 剥前缀剥得不够（V0.5.9 漏了一半）

V0.5.9 修了 `scope_label` 只按第一个冒号切的问题，剥掉了 `background:`。
但剩下的是 `behavior_change:学习`，**又被按第一个冒号切了一次**，
标签变成 `behavior_change:学习`，规则名照样污染 gram。

结果：候选裁决永远匹配不上。这次才真正修对。

---

### ③ 连点「正确」能把自己抬到毕业线（严重）

**实测**：

```
候选起点 conf=0.40
第1次点「正确」-> support=1 conf=0.48
第2次点「正确」-> support=2 conf=0.56
第3次点「正确」-> support=3 conf=0.64
第4次点「正确」-> support=4 conf=0.72   ← 越过毕业线 0.70
第5次点「正确」-> support=5 conf=0.80
```

**用户点一次的心意，被算成 5 次真实证据；连点 4 次就能毕业。**
这直接击穿了 V0.5.9"毕业必须靠真实用户证据"的核心设计。

根因：裁决的 log id 用 `uuid4()` 随机生成 → 每次点击一条新行 →
`add_evidence` 的去重索引（candidate+polarity+source）抓不住。

修法：改成确定性生成，口径与自然证据一致
（`uuid5(candidate_id + verdict + 原文 + 日期)`，`INSERT OR IGNORE`）。
改后连点 5 次 `support_count` 稳定在 1、`conf` 稳定在 0.48。

**顺带**：去重时 `add_evidence` 返回 `False` 会让 API 报 404
"已处理"，但裁决其实成功（状态已推进到 accepted）。改成幂等返回 True。

---

### ④ 裁决查询每个回合全量扫 200 条假设（性能）

`resolve_pending_candidate` 用 `list_hypotheses(live_only=False, limit=200)`
拉全表再在 Python 里过滤。实测给每个"可表态"的回合平白加约 30ms。

修法：加定向查询 `list_pending_background()`，
只取 `status='pending' AND scope LIKE 'background:%'`。

---

## 2. 一条被 mutation 抓出来的测试缺失

修 bug 过程中重构掉了一块代码区间，**误删了**
`test_graduation_needs_two_independent_days_not_just_high_confidence`。

mutation O（删掉毕业时的独立源检查）当场**存活**暴露了它。

这条测试守的是一条很隐蔽的性质：`add_evidence` 在独立源 < 2 时用
`min(conf, 0.70)` 而不是减一，所以**"conf 正好 0.70 且只有一个来源"
是完全可达的状态**——只测"证据不够"根本测不到那道检查。

> 这正是建 mutation 基线的价值：它不只验证代码，也验证**测试本身还在**。

---

## 3. soak 用的是隔离环境

`scripts/soak_v0510.py` 在 import 项目模块**之前**设好
`PAI_DATA_DIR` / `PAI_LOG_DIR` 指向临时目录，
`PAI_IDLE_REASONING=1` 也只在这个进程内生效。

**默认配置一个字没改**——`PAI_IDLE_REASONING` 默认仍是 `"0"`。
常驻后台循环是产品级决定，不该由一次代码升级悄悄带进用户机器。

`data/personal_ai.db`（真实用户库）全程未被打开。

### 两处测试脚手架的限制，如实记

1. **"重启" = 丢掉 app/worker 对象，用同一个 DB 重建。**
   进程态归零、数据留存，与真实重启等价且可重复；但没起独立 OS 进程。
2. **TestClient 的 startup hook 只在 `with` 上下文里触发。**
   harness 里 `build_app()` 之后没有进 `with`，所以第二个 app 的
   worker 处于未启动态。旧 worker 停止与否是单独验证的
   （`worker_leak_check: old_running=false`）。

---

## 4. 长时运行实测数据

见 V0.5.10 报告。关键几条：

| 指标 | 结果 |
|---|---|
| idle 开启前后 reasoning 耗时 | 18.15ms vs 18.50ms（无可测退化） |
| background 自跑 5 次 | support_count 完全不变，evidence 行数 0 |
| prompt 污染 | pending / accepted / rejected / expired 四种状态全部不进 |
| DB 竞争后完整性 | `PRAGMA integrity_check` = ok |

---

## 附：修 bug 的纪律

本次修的四个问题，全部来自 **soak / 异常测试 / 真实链路**，
没有一个来自"我觉得未来可能有问题"：

| Bug | 发现方式 |
|---|---|
| ① 对话无法裁决 | soak 故事第 E 阶段走不通 |
| ② match_label 剥一半 | soak 故事第 E 阶段仍走不通，追查时发现 |
| ③ 连点抬到毕业线 | 异常测试"用户连续点击正确" |
| ④ 裁决查询全表扫描 | 性能对比（44ms → 定向查询） |
| 缺失的测试 | mutation O 存活 |