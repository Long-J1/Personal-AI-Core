# D021 — V0.5.5 尝试记录：为什么停在 V0.5.4 基线

日期：2026-10-02
状态：**未完成，已回滚**

---

## 一句话

0.5.5「自然理解闭环」的**诊断已完成、代码骨架已写、但测试没跑通**，
按「不能为了交付版本而掩盖问题」的要求回滚到 `8edd2e9`。

---

## 已确认的事实（这部分是可靠的，可以复用）

诊断出的真实缺口，**不是猜的**，是从代码读出来的：

| 缺口 | 证据 |
|---|---|
| 没有自然证据路径 | `SOURCE_TYPES` 只有 `("context", "event", "correction")`；全仓库搜 `natural` = 0 处 |
| 没有自然确认 | `record_verification_confirm` 只在 AI 提问后触发；搜 `spontaneous` = 0 处 |
| correction 不级联到 hypothesis | `_cascade_pattern` 只级联到 **pattern**；`hypothesis_store` 无 `demote` |
| conversation 不接证据 | 沉淀里只调了 `inducer.induce_all()` |

**所以 V0.5.5 的方向是对的**：要补「从用户自然发言里直接读出表态」的主链，
而不是继续加固提问链。

### 写代码时踩到的三个真实约束

1. **`add_evidence` 有硬白名单 + `_source_exists` 同库校验**
   → 用户聊天轮次不是合法来源，自然证据**无处可落**。
   解法：新增 `turn` 来源类型指向真实 `chat_messages` 行。
   注意 `SOURCE_TABLE` 若直接映射，`_source_exists` 只查 id 存在，
   **assistant 行也会通过** —— 必须专门判 `role='user'`，否则 AI 可以自己证明自己。

2. **`chat_store.append()` 返回 None，`history()` 也不返回 id**
   → 拿不到本轮消息 id。解法：让 `append` 返回 id（纯增量，原调用方都忽略返回值）。

3. **中断点**：`is_confirmation("对，我就是晚上学习效率最高")` 返回 **False**。
   我按直觉写了「自然确认」的测试，但 `verifier.CONFIRM_KEYWORDS`
   里并没有「对」这个说法。11 个测试因此变红。
   **这是我的错**：应该先读 `CONFIRM_KEYWORDS` 再写测试，而不是反过来。

---

## 为什么停

不是能力问题，是**质量门槛问题**：

- 用户要求「必须有 mutation tests」
- 用户要求「禁止为了让测试绿而放宽规则」
- 用户要求「如果某个版本无法可靠完成：不要硬凑」

在确认词表没读对的情况下，硬凑一版「能跑绿」的测试，
等于把「自然确认」定义成我猜的语义 —— 那正是 D007 里反复出现过的教训
（"看起来在工作其实没工作"）。

已回到最近一个可靠版本，不留半成品代码。

---

## 下次接着做时的第一步

**先读 `core/verifier.py` 的 `CONFIRM_KEYWORDS` / `VERIFY_DENY_EXTRA`
/ `_normalize_confirm` 三个定义**，再决定「自然确认」复用哪一套。

如果现有词表不适合自然确认（比如它是为「回答验证问题」设计的），
那应该**新建独立的确认词表并写进测试**，而不是硬套现有的。

第二件事：确认「否定词优先于确认词」在现有实现里是否已经成立
（`resolve_pending_answer` 是这样，但自然路径还没走它）。

---

## 当前仓库状态（回滚后）

```
HEAD      8edd2e9  fix(beta): 打包缺失预设 + 明文码不入产物 + 解锁提示用产品口径
测试      358 passed, 1 skipped
工作区    干净（只有 5 个历史遗留未跟踪文件）
```

V0.5.4 Release / 官网 / 安装包 **全程未动**。