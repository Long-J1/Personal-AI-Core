# D025 — Background Reasoning Lifecycle：候选会长大、会死

日期：2026-10-03
基线：`5e85e1e`（V0.5.8）
范围：candidate lifecycle / 后续证据积累 / 去重淘汰 / idle reasoning
**零 schema migration、零新依赖、零定时器框架。**

---

## 0. 上一版缺什么

V0.5.8 的候选是**死的**：用户点"正确"，它拿到一条证据，然后永远停在
tentative。没有成长、没有淘汰、没有第二次机会。

一个不会长大的猜测和一个不会死的关系，在产品上是同一个东西——
都是在积累垃圾。

---

## 1. 生命周期（status 是 TEXT 列，不需要 migration）

```
pending ──用户认可──► accepted ──证据足够──► confirmed（毕业）
   │                      │
   │                      └──长期无动静──► expired
   ├──用户否定──────────────► rejected
   └──长期无动静──────────────► expired
```

**`pending` / `accepted` / `expired` 都是既有 `status` 列的新取值**，
不是新列。`LIVE_STATUSES = ('tentative','confirmed')` 不变——三个新状态
天然不在 live 里，所以 prompt / pattern /「我眼中的你」**一处都不用改**。

### 毕业靠换前缀，不靠加例外

毕业动作 = `status→confirmed` + `scope` 从 `background:` 换成 `inferred:`。

那 7 处安全过滤全都只认 `background:` 前缀，换掉前缀候选就自然进入
prompt / pattern / 理解视图——**不需要在每个地方再加一条 background 例外**。
这是整套设计里最省事也最不容易漏的一步。

---

## 2. 毕业门槛直接复用既有两把尺子

```python
confidence >= HIGH_CONF_CAP (0.70)   且   独立来源数 >= MIN_INDEPENDENT_FOR_HIGH (2)
```

不为后台推理另立标准。`0.70` 这个数有个容易被忽略的性质：
`add_evidence` 在独立源 < 2 时是 `conf = min(conf, 0.70)`——
**用 min 而不是减一**，所以"conf 正好 0.70 且只有一个来源"是可达状态。

这意味着**只测"证据不够"测不到独立源检查**：必须专门构造
"同一天反复说 4 遍 → conf 0.70 / indep 1 → 不许毕业"。
`test_graduation_needs_two_independent_days_not_just_high_confidence` 专测这个，
mutation O 也打的是这一条。

候选起点 0.40、每条真实支持 +0.08 → **毕业需要 4 次跨天支持**。
这是设计意图：候选必须被反复印证，不是被一次认可放行。

---

## 3. 认可 ≠ confirmed

用户点"正确" → `accepted` + 一条证据，**不进 confirmed**。

如果允许升级，后台推理就有了**自我确认**的能力：它提猜测 → 用户随手点
一下 → confirmed → 进 prompt → 影响回答。整条安全链就废了。

mutation N 专门盯这一条。

---

## 4. accepted 是唯一能吃自然证据的后台候选

| 状态 | 自然证据 | 为什么 |
|---|---|---|
| pending | ✗ | 还没人裁决的猜测 |
| **accepted** | **✓** | 用户已认可，这正是它长大的**唯一**入口 |
| rejected / expired | ✗ | 结束态 |

两道守卫：`hypothesis_store._natural_eligible` 与
`natural_bridge._eligible_hypotheses`（mutation K / K2 分开测）。

### 一处死代码，说明一下

两道守卫里都还留着 `scope.startswith("background:")` 的显式检查，
但 `pending` 已经被状态白名单挡在前面了——**那两行现在跑不到**。

保留它们是刻意的纵深防御（将来若有人往白名单里加状态，这两行还在），
但它们**不是当前生效的闸门**。真正的闸门是状态白名单，mutation 也打那里。
**留着跑不到又测不到的行，比没有更容易误导后来的人**——所以这里写清楚。

---

## 5. 淘汰用已有的 `updated_at_epoch`

`expire_stale_candidates()` 只改 status 为 `expired`，**不删行**：
用户确实说过这些话，历史必须可追溯（D022 定的规矩）。

判据是 `now - updated_at_epoch > 30 天`。`add_evidence` 和裁决都会刷新它，
所以不需要新增任何列。普通理解（不带 `background:` 前缀）**永不淘汰**。

---

## 6. 语义去重比 topic，不比整句

早先版本比 `reason[整句]` 的二连字交集，结果模板套话
（"最近 7 天有 N 次记录是…"）自己就共享好几个二连字，
把"学习"和"编程"误判成同一件事。

现在比 **topic**（从 `scope` 尾部取）。这正好对应实测到的真问题：
V0.5.8 会同时产出 `behavior_change:学习` 和 `focus_shift:学习`——
同一份数据、三条规则、两句话。mutation Q 盯这一条。

---

## 7. 踩到的四个真 bug

### ① `scope_label()` 只按第一个冒号切

`background:focus_shift:学习` → 标签变成 `focus_shift:学习`，
规则名混进标签污染 gram，门槛算成 need=2 而实际只共享 1 个"学习"
→ **accepted 候选永远匹配不上任何自然证据**。

修法：`natural_evidence.match_label()` 先剥掉已知的 `background:` 前缀。

### ② `if not self._should_run():` 判的是元组

`_should_run()` 返回 `(bool, reason)`，元组永远为真 →
**idle 的冷却与活跃判定整个失效**。测试连着四个红才暴露出来。

### ③ 语义去重比整句（见上）

### ④ `hypothesis_store` 漏 `import time`

`expire_stale_candidates` 用了 `time.time()`，模块没导入 →
一调用就 `NameError`。idle worker 的日志里先看到它。

---

## 8. idle worker：默认关闭

`PAI_IDLE_REASONING=1` 才启动。

**默认关**是刻意的：本地跑一个常驻后台循环是产品级决定，不该由一次
代码升级悄悄带进用户的机器。

四条边界都有测试钉着：

| 边界 | 测试 |
|---|---|
| 用户还在用 → 一次都不跑 | `test_idle_worker_does_not_run_while_user_is_active` |
| 冷却 | `test_idle_worker_respects_cooldown` |
| 不占事件循环 | `test_idle_worker_does_not_block_the_event_loop`（心跳协程被验证没被饿死） |
| 不调云端模型 | `test_idle_worker_never_calls_the_model`（把 `reason_all` 换成会炸的桩） |
| 能取消、不留孤儿 | `test_idle_worker_start_is_idempotent_and_stop_cancels` |
| 隐私暂停时不跑 | `test_idle_worker_passes_when_privacy_paused` |

`chat_store.last_activity_epoch()` 是新增的**读方法**（复用 sessions 表
已有的 `updated_epoch`），不是 schema 变更。

---

## 9. 断言过期 vs 回归（又一次）

`acceptance_v03.py` 的 `total != N` 在 V0.5.8 已经改过一次。
V0.5.9 候选状态变化后仍全过——因为改的是**本意**而不是数字。

判断口径不变：**断言在表达意图还是表达数字**。表达数字的随产品演进更新，
表达意图的不能动。

---

## 附：验收

| 标准 | 结果 |
|---|---|
| 数据库 migration | **无**（status 是 TEXT，新增取值；淘汰复用 `updated_at_epoch`） |
| 测试 | **574 通过 / 0 失败 / 0 错误 / 1 跳过**（基线 538，新增 36） |
| mutation | **21/21 KILLED**（新增 K/K2/L2/N/O/P/Q/R 打 V0.5.9 链） |
| acceptance | 四套安全套件全过 |
| 定时器 / 依赖 | 零 / 零 |
| 发布 | **未发布、未部署、未打 tag、未改官网** |