# D022 — Natural Evidence 与 Change：三个"看起来像 bug"的设计

日期：2026-10-02
基线：`f82afb1`
范围：让用户自然说出的话成为证据 + 用户变化时旧理解失活/降权。
**没有**新增 schema、依赖、表、端点、UI。

---

## 0. 为什么不用新 source_type

原计划是新增 `turn` 来源类型指向 `chat_messages`，需要改 `chat_store.append`
返回值 + 加白名单 + 迁移。实测后发现**完全不需要**：

`HypothesisStore._source_exists()` 对 `source_type='correction'` 的校验是
「`context_entries` 里有这行」**或**「`hypothesis_log` 里有这行且 action 属于
`('reject','verify_confirm','change')`」。只要我们自己写一条真实 log，
它的 id 就是合法来源，`add_evidence` 的置信度数学、去重索引、独立源计数
全部现成可用。**零表结构变更、零迁移。**

> 教训：动手前先读 `_source_exists` 的真实实现。上一次 V0.5.5 尝试就是因为
> 没读、按直觉设计，白做了 3 个文件的改动和 11 个红测试。

---

## 1. 话题门槛必须随标签长度缩放（不要写死 2）

`MIN_RELATED_GRAMS = 2`，但**只对标签能产出 ≥2 个二连字时成立**。
实际 `required_grams()`：

```python
return max(1, min(MIN_RELATED_GRAMS, label_gram_count(scope)))
```

**为什么**：R1 归纳出的 scope 是 `activity:<活动名>`，活动名常常只有两个字
（如 `activity:自习`），标签总共只能产出 **1** 个非停用二连字。写死 2 的话，
**所有短标签假设永远匹配不上**，整个功能等于半死——而且测试很难发现，
因为用长 scope 写的用例全都能过。

口径与 `deposit._apply_new` 里已有的 `need = min(2, len(entry_kws))` 一致。

> 反面证据：多假设隔离（H1 晚上学习 / H2 喜欢晚上运动 / H3 咖啡）仍然成立——
> 长标签时门槛是 2，「晚上」一个共同词不足以命中 H2。

---

## 2. 不可逆的动作只在强相关时做（deny 要分两档）

```
强相关（≥2 个共同二连字） → reject()   置 0.25 + status=rejected + 级联 Pattern
弱相关（≥1 个共同二连字） → 只吃反证    -0.25，status 保持不变
```

**为什么**：`reject()` 是不可逆的（要 `restore_entry` 才能回来），
而且会顺着 `reject` 的级联打到 Pattern。用户在换个说法时——假设写的是
「晚上学习效率」，用户说的是「不是，我现在**早上效率**更高」——两者只共享
「效率」一个词。这明显是在谈同一件事，不该被无视；但证据也只有这么多，
不足以把一条可能仍然正确的假设**永久判死**。

弱相关时降权是可逆的，且同样记进了可追溯的证据链。

> **不要**为了"简化"把弱相关也走 reject。那是拿不可逆性换覆盖率。

---

## 3. 自然证据的 log id 必须确定性生成

```python
uuid.uuid5(NAMESPACE_URL, f"personal-ai:natural:{hypo_id}:{day}:{归一化原文}")
```

**为什么**：来源 id 如果用 `uuid4()`，同一句话重复说每次都生成新行，
`add_evidence` 的去重索引（`hypothesis_id + polarity + source_type + source_id`）
就永远抓不住，`support_count` 会凭空翻倍——用户重复三遍同一条理解，
系统就以为有了三条独立证据。

口径与既有 `origin`（按来源日计独立源）一致：同一条陈述、同一天 = 同一个
来源；换了说法或跨天 = 新的独立来源。写入用 `INSERT OR IGNORE`。

---

## 4. 存储层的 quota 边界要挡 deny，不能只挡 support

`reject()` 自己只检查 `deleted` / `rejected`，**不认识 `quota_overflow`**。
最初只在 `record_natural_evidence` 的 support / change 分支加了
`_natural_eligible` 检查，deny 分支直接甩给 `reject()` —— 结果 quota_overflow
假设仍能被自然否定，还会顺着级联打到 Pattern，绕过 V0.5.3 设的边界。

现在 deny 分支也过 `_natural_eligible_check()`。

> 防线是**两层**：store 的 `_natural_eligible` + bridge 的 `_eligible_hypotheses`。
> 两层都要有直连 store 的测试，否则删掉一层另一层兜住，mutation 测不出来
> （D 见 `scripts/mutation_natural.py` 的 D 变异）。

---

## 5. 变化 ≠ 否决

「我改成早上了」不等于「旧规律永久错误」。change 信号走
`_natural_contradict`：只吃反证、`status` 不动。旧 Context 用现成的
`memory_status='outdated'`（V0.5.2 就有的三态之一，prompt 只读 active，
历史完整保留）——**没有新增任何状态值**。

> 用户可能只是暂时变化。判死是错，永久并存也是错；降权 + 失活 + 保留
> 历史才是对的。

---

## 6. 自然表达永远不新建假设

`apply_natural_signal` 只能对**已存在的** live 假设做确认/否定/降权。
它不碰 `HypothesisStore.create`。

否则用户随口一句「我确实特别擅长游泳」就能凭空造出长期理解，
而归纳门槛（R1 要同一活动 3 次）就形同虚设了。

---

## 附：mutation 基线

`python scripts/mutation_natural.py` — 删掉保护条件后测试必须变红。

| 变异 | 删掉什么 | 预期 |
|---|---|---|
| A | topic matching | 相关/漂移测试红 |
| B | 自然支持信号 | support 测试红 |
| C1 | 否定优先级 | 否定/场景 8 红 |
| C2 | 变化优先级 | 变化/场景 4 红 |
| D | quota_overflow 存储层守卫 | 生命周期测试红 |

变异存活 = 测试是摆设，必须先修测试。