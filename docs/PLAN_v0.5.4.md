# V0.5.4 验证链调查 + Event 防火墙

> 地位：V0.5 系列信任修复分支第三弹。不新增能力。
> 组成：P1 验证链零触发**调查**（只出报告，不动机制）+
> P1 event 统一防火墙（小改）。假设生命周期暂缓。
> 状态：**已实施**（event 防火墙；验证链调查只报告不动手。
> 测试 5 项新 + 全量 310 通过 + 四套 acceptance 全过 + 8012 真机复测通过。
> 待 commit）

---

## 第一部分：验证链零触发调查报告

### 1. 当前完整触发链

```
用户消息
 → conversation._pick_verification（hyp → pattern → goal，一轮一个）
   → select_candidate：pending空 + tentative + CONFIRM_BAND[0.5,0.7)
     + 独立源<2 + asks<3 + 冷却>24h + is_related(话题相关)
   → set_pending（挂起）→ 回答末尾附问题
 → 下轮 resolve_pending_answer：确认/否决/回避落链
   → confirmed/rejected（confirmed 同簇≥2 → promote_all → pattern）
```

pattern 侧（select_pattern_candidate）与 goal 侧（select_goal_candidate）
同规则（band/独立源/冷却/次数/话题），pending 全局各一。

### 2. 真实数据（pai-val7，8 天，16 条假设）

- tentative 14（含 6 条 quota_overflow），rejected 2（context 纠正联动），
  confirmed 0，asked_count 全 0，pending 全空。
- verification_question 发出次数：**1**（Day 4 goal 提问），hypothesis 0，pattern 0。
- pattern 表：0 行。

### 3. 每条假设为什么没触发

| 假设 | band | asks/冷却 | related | 卡点 |
|---|---|---|---|---|
| interaction:固定 0.66 | ✓ | ✓/✓ | 实测 True | 时序：产出轮与话题轮错开（见案例1） |
| interaction:思考 0.70 | ✗（=上界） | ✓ | False（泛标签无话题命中） | band 上界 + 标签太泛 |
| interaction:倾向 0.70 | ✗ | ✓ | False | 同上 |
| interaction:时间/导其 | —（已 reject） | — | — | 纠正联动拒掉，与验证无关 |
| inferred×3（鸡腿/精力/茶饮） | ✓(0.5) | ✓ | False（英文 scope/泛化无人聊） | scope 不可聊 |
| quota_overflow×6 | ✓ | ✓ | — | V0.5.3 四不准主动跳过（设计内） |

### 4. 三个"本来可能触发"的真实案例

**案例 1（时序错开）**：Day 5 第二轮"固定时间纠结"↔interaction:固定，
is_related 实测 True，band/asks/冷却全应过，仍 0 提问。
R2 在沉淀后跑，假设产出轮与话题轮差一轮——"搭便车"要求假设
**在本轮之前**已存在且话题**在本轮**命中，两轮对齐缺一不可。
自然聊天话题漂移快，对齐是小概率事件。→ C（产品假设与真实节奏不合）为主，
A（多条件合谋）为辅。另：沉默无日志，事后无法复盘（可观测性缺口）。

**案例 2（路由串扰，Goal 被误杀）**：Day 4 goal 提问挂起后，
用户 4 轮答非所问（4×evasive），Day 5 一句"不对，我只是最近几天这样"
（本意否认"晚上效率高"假设）被 `resolve_goal_answer` 当成对 goal 的否决 →
goal rejected。hyp 侧 pending 为空（结构化问题从没挂过），否决按
hyp→pattern→goal 顺序落到 goal 头上。**一句话只答一个问题，
系统按固定顺序认领**。→ B（链路匹配错误）实锤。

**案例 3（标签泛化）**：R2 标签"倾向/思考/固定/时间/导其"——
单字泛词做 scope，"导其"（"导致其"切碎）永无话题命中，
"思考"无一轮相关。假设质量差 → 即使调低 band 也只会招来骚扰提问，
不敢调。→ A（门槛）不敢动是因为 B（标签质量）兜不住。

### 5. 归因判断：E（多因共存，主次分明）

- **主因 C**：机制假设用户按"问题→回答"回合制聊天，真实用户多话题穿插、
  答非所问（4×evasive 为证）。自然对话确认走 deposit（correct+reason，
  Day 3/5 两次落链正确），结构化验证 8 天 0 触发——**两条确认链并行，
   work 的那条不在验证体系内**。
- **次因 B**：pending 路由串扰（误杀实锤）+ R2 标签泛化。
- **次因 A**：band/indep/冷却/次数四重门 + 沉默无日志，调试靠猜。
- **D 不成立**：16 条假设、8 天、30 轮，量够了，触发 0（hyp）/1（goal 问完被杀），
  不是量的问题。

### 6. 建议：重设计（不停在调参）

- **应该重设计**：把"系统提问→用户回答→落链"改为
  "对话内自然确认捕捉→落链"为主链——deposit 的 correct 通道已经在做
  正确的事（reason 引用原话），把它扶正为确认主链，结构化提问降级为
  兜底（band 收窄到真正高价值假设）。不是调阈值，是换主链。
- **应该修（小）**：pending 路由串扰（否决只认领语义相关挂起，不按固定顺序），
  选择沉默记日志（可观测性）。
- **应该砍掉/冻结**：在重设计前，pattern 晋升门槛（2 confirmed 同簇）
  冻结不动——8 天 0 行证明它在真实频率下够不着，调低只会放进垃圾。
- 本版（V0.5.4）**只调查、不动手**：上面三条留 V0.5.5（或 V0.6 前置）议，
  本版唯一代码改动是 event 防火墙。

---

## 第二部分：Event 统一防火墙（设计）

### 1. event 写入点（全库审计）

| # | 位置 | 来源 | 本版动作 |
|---|---|---|---|
| 1 | `deposit._apply_event` | 对话沉淀（模型 JSON） | **加 firewall**（唯一改动点） |
| 2 | `pipeline.py:137` | 图片感知（摄像头观察） | 不动（来源是观察不是对话，无"用户原话"可对；写明） |
| 3 | 测试/smoke 直调 `add_event` | 测试夹具 | 不动 |

防绕过结论：对话沉淀进长期存储的 event **唯一入口**是 `_apply_event`，
门放这里即无绕行（别记轮 deposit 根本不跑，天然一致）。

### 2. firewall 位置与规则

`_apply_event` 入口，前三行即分类（`_apply_creates` 同款 `classify_source`，
新签名加 `user_text/entries/store` 实参——调用方 `deposit()` 都有，不缺）：

1. 四类硬拦截复用（过去时/日期/人名/性格无出处 → reject + `firewall_reject`）。
2. advisory 丢弃复用（泛化建议不当事件）。
3. 无依据门复用——**已由 `deposit()` 层的 grounded 门实现**
   （问句轮 `_apply_event` 根本不跑），此处只需注释写明，不加代码。
4. **闲聊启发式只做低价值标记，不做拒绝**（用户拍板）：
   用户明确说出的真实 event（user_direct/db_grounded）**必须写入**，
   不得因"看起来像闲聊"被拒。命中闲聊模式
   （`CHITCHAT_PATTERNS` + 去标点长度<20，单测锁定）→
   `importance = min(importance, 0.2)` + 日志 `event_low_value`，
   正常写入。误伤案例进 D019 附录。
   （设计立场：event 的问题是"分量"不是"真假"，分量用 importance 表达，
   不用"记/不记"二值表达——二值是 context 的语言，event 说分量。）
5. inferred 进 event 与否：沿用现状（模型 event 输出照收，分类只拦 1-2），
   不扩大——本版不管推测事件的去留，只管"用户原话事件不被误杀 + 低价值有标记"。
6. 通过 → 原逻辑不变（importance 钳制、add_event、event_id）。

### 3. 被拒绝后

硬拦截/advisory 命中 → 丢弃 + `firewall_reject [event:原因]` 日志，
不建 hypothesis（事件不是事实主张，无待验证价值；
与 context 的 infer 转假设区分，注释写清）。
闲聊命中 → **正常写入 + importance 压到 0.2 + `event_low_value` 日志**
（不是拒绝，是分量标记）。

### 4. 与 context correction 的关系

**本版无联动**：event 表无 status 列，不加（加列=扩大回归面，违"小改"）。
context 纠正/reject 不碰 event。鸡腿 event 仍在库——接受（历史数据不动，
只管住入口）。V0.5.5 议 event 状态列，本版写明暂缓。

### 5. 别记一致性

别记轮跳过整个 deposit（含 `_apply_event`），已一致，无需改。
单测锁死：别记轮 event 零新增（已有 V0.5.2 测试覆盖，本版加闲聊别记用例）。

### 6. 回归测试

- 新增测试（对照式，证明不误杀）：
  闲聊事件（"哈哈鸡腿好吃"→ 写入 + importance≤0.2 + 日志）；
  明确重要事件（"CSP备考压力时间不够"→ 照常入库 + importance 保持）；
  硬拦截事件（"上次去过网吧"无证据 → 拒 + 日志）；
  别记轮事件零新增。
- 全量回归 + 五套 acceptance（v03/v04/v05/v051）全过。
- 真库零触碰，零云端调用。

### 7. 回滚

单点改动（`_apply_event` 入口 + 常量表），`git revert` 单步回滚。
无 schema 变更，无残留。

### 8. 已知限制

- 闲聊启发式会误伤短而有 emailed 的事件（"今天拿奖了"长度<20但高价值？
  "拿奖"不在闲聊表，进得去——误伤面已用"模式表交集"收窄，但承认不完美）。
- 感知通道（pipeline）事件不管——摄像头看到的和对话说的不是一回事，
  分开治理，注释写清。
- 库里已有鸡腿 event 不动（只管入口不管存量）。
