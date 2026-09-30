# V0.5.3 值得信任地记忆

> 地位：V0.5 系列信任修复分支第二弹，只补 P0 信任洞，不新增能力。
> 定位：**还债版 Part 2**。V0.5.2 让"记得准"成立，7 天真机验证证明两处 P0 信任失败：
> 别记守住了库没守住嘴、配额满了静默丢记忆。本版只修这两件。
> 状态：**已实施**（测试 10 项 + 全量 305 通过 + 五套 acceptance 全过，待 commit）

---

## 一、问题复盘（7 天真机验证实锤）

### P0-1：别记回答层失败（Day 4，Trust Failure）
- 存储层满分：别记轮五表零新增，"同桌/笔记"全表零命中。
- 回答层零分（三连败）：
  1. 追问轮复述别记细节（"同桌昨天借笔记抄"）——来源是 history 原文进 prompt；
  2. 别记对象张冠李戴（把"同桌别记"听成"球鞋别记"）——单轮多主题时模型自选锚点；
  3. 反向验证编造虚假承诺（"球鞋不会进长期记忆"，库里 3 条 active）。
- 根因：`_assemble` 里 history 原文无过滤进 prompt（`conversation.py:274`），
  别记轮原文下轮原样可见，模型必然"记得"。防火墙管住了五张表，
  管不住 history 这条暗道。

### P0-2：配额静默丢记忆（Day 6，信任级）
- dynamic kind 7 天新增打满 10 条（`KIND_WEEKLY_CAP`）后，
  "喜欢跑步""喜欢咖啡"正当记忆零写入、无任何用户可见提示。
- 用户默认"说过=记住"，系统实际丢弃——"假装记住"，违"宁可少记"原则。
- 根因：`add_or_merge` 的 `merge_only` 模式无相似可合并即丢弃，
  且丢弃只记 debug 日志，无分级去处。

### 非目标（本次不碰）
- 衍生计划不随前提失效、自然确认不进假设链、验证 7 天零触发、
  pattern 7 天零行——真实缺口，但属 P1/架构议题，留 V0.5.4 调查，
  不在本版扩散。
- **event 防火墙（V0.5.4 候选，不要遗忘）**：所有进入长期存储的入口
  必须经过统一 memory firewall。Day 1-2 真机验证实锤 event 通道绕行
  （鸡腿入库且被回答引用）。本版不修（保回归面），V0.5.4 首项议题。

## 二、修改目标（只两件）

1. 别记成为用户可感知的承诺：当前 session 不主动引用被标记轮次，
   追问时诚实说不知道，不编细节，不错认对象，不虚假承诺。
2. 配额满不再静默丢：溢出内容有去处（待验证池）+ 可审计日志，
   系统从不"假装记住"。

## 三、数据结构变化

### 3.1 `chat_messages` 加 `suppressed` 列（唯一 schema 变更）
- `suppressed INTEGER NOT NULL DEFAULT 0`（0=正常，1=别记轮）。
- migration：`PRAGMA table_info` 探列，无则 `ADD COLUMN`，老库默认 0（=老行为）。
- 别记轮 `append` 时 user/assistant 两条都标 1（assistant 回答可能复述别记细节，
  Day 4 追问轮就是例子——只标 user 不够）。
- 被隐藏轮**仍进 history**（保证当前对话连贯，用户回看原样可见），
  但禁止进入 context / event / hypothesis / goal / pattern 五表
  （写路径本来就跳过，本条是重申 + 单测锁死）。
- 后续 retrieve（事件话题命中）和 prompt 构建（history/事件段）阶段，
  同样必须过滤隐藏轮相关内容——不止 `_assemble` 的 history 段，
  事件 retrieve 命中隐藏轮衍生的 event 也不行（别记轮本身不产 event，
  这条是防未来回归，单测用 grep 断言 `retrieve` 调用过滤）。
- UI 的 history 查询**不过滤**（用户回看聊天记录原样可见，不删记录）；
  只有进 prompt 的那份过滤。

### 3.2 配额方案零 schema 变更
- 复用 hypothesis 表（待验证池）+ 日志，无新表新列。

## 四、Prompt 变化（只改文案，不改结构）

### 4.1 history 进 prompt 前过滤（`_assemble`）
- `suppressed=1` 的轮次，user/assistant 原文替换为占位符：
  `[本轮用户要求不记录，内容已隐藏，不可引用、不可复述]`
- 条数不变（占位符占位，不断连贯——模型知道"这里有一轮但我看不见"，
  而不是"对话断了"）。
- 单测双断言：prompt 含占位符 + 不含原文关键词。

### 4.2 `NO_MEMORIZE_PROMPT_RULE` 升级
- 旧版只管"不说记住了"，新版加三条：
  "你的上下文里看不到本轮具体内容（已被隐藏）。如被问及该轮，\
  必须说'你要求那部分不记录，我这里没有保留'；禁止复述任何细节\
  （你也看不到，编出来就是撒谎）；禁止对其他轮次内容声称'不会记'\
  （只对被隐藏轮次做此声明）。"
- 别记对象误解**自消**：不需要识别对象——整轮隐藏后，
  "哪个内容别记"不再是模型要判断的题，自然锚定正确。
- 语义锁定（单测）：用户说"别记" = **整轮**别记（含轮内正常部分）。
  可预测 > 精细，这是信任产品的取舍，注释写清。

## 五、流程变化

### 5.1 别记轮（`conversation.chat`）
- 判定不变（`is_no_memorize` + "记错"剥离）。
- 写路径不变（跳过 deposit/inducer/promoter/B-intake/goal-propose 五路）。
- 新增：本轮两条 history 标 `suppressed=1`。
- 验证回答解决（pending）本来就跳过，保持。

### 5.2 配额溢出（`add_or_merge`）
- 语义变更：`merge_only=True` 且无相似可合并时，不再丢弃，
  而是返回 `(None, "overflow:<kind>")`（新跳过原因，可测）。
- 调用方（`_apply_creates`）：收到 `overflow` → 内容转 hypothesis 待验证池
  （scope=`quota_overflow:{kind}`，detail 注明原 grade 与原文），
  复用 firewall 的 infer 转假设路径（去重照常）。
- **溢出假设四不准**（防"假设垃圾桶"，单测逐条锁定）：
  1. 低优先级：`list_for_prompt` 按 scope 降权（`quota_overflow` 排最后，
     正常假设优先；实现方式见实施注释，行为断言：混合列表里溢出项垫底）；
  2. 不主动引用影响回答：同 1（排不上 prompt 就影响不了）；
  3. 不触发 verification：`select_candidate` 跳过 `quota_overflow` scope
     （选择器加 scope 前缀过滤，一行）；
  4. 不参与 pattern 生成：`induce_all` 与 `promote_all` 跳过
     `quota_overflow` scope（归纳/晋升入口各加一行过滤）。
  只作为未来分析材料（V0.5.4 再处理假设生命周期）。
- 日志：`quota_overflow [kind]：...`（INFO，可审计；与 `firewall_reject` 同级）。
- 不承诺：deposit 本来就不影响本轮回答措辞（回答在沉淀前已生成），
  所以不存在"说了记下了但没记"——转假设后东西没丢，将来验证可转正，
  假设池去重防膨胀。**这是"宁可少记"的字面实现：memory 没记，但没丢。**

### 5.3 不碰的
- `list_entries`（只读 active）不动；`set_memory_status`/传播/级联不动；
  防火墙分类不动；R2/验证/Goal/Reflection 全不动；UI 不动。

## 六、测试方案（只测，不演示）

- 灾难测试 Day 4 重放（`test_trust_boundary.py` 新增）：
  "同桌借笔记别记" → 追问"记得吗" → 断言回答不含"同桌/笔记/球鞋细节"，
  且含"没有保留"类诚实表述（宽松断言，Mock 固定句）；
  反向验证轮断言无虚假"不会记"承诺（prompt 双断言）。
- **5 轮追问测试**：别记轮后连续追问 5 轮（换说法问：记得吗/那个事呢/
  同桌是谁/笔记内容/再提示一下），断言每一轮回答都不含隐藏关键词，
  且 prompt（system + history 段）全程不含原文。
  覆盖"任何路径"：history 段、事件段、假设段、规律段四段逐段断言。
- migration 单测：老 schema（无 suppressed 列）库初始化 → 列存在且默认 0，
  旧 history 全可见（行为不变）。
- 占位符单测：suppressed 轮进 prompt 是占位符；UI history 查询仍返回原文。
- 配额溢出单测：fixture 打满 dynamic → 新 user_direct 内容 →
  memory 零新增 + hypothesis +1（scope 前缀 `quota_overflow`）+
  日志含 `quota_overflow`。
- 全量回归 + 四套 acceptance（v03/v04/v05/v051）全过。
- 真库零触碰，零云端调用（铁规延续）。

## 七、回滚方案

- migration 不可逆但无害：`suppressed` 默认 0 = 老行为；
  整版 revert 后，老代码无视该列（`SELECT *` 多列不影响 `_row` 映射——
  需确认 chat_store 的 row 解析是按名取列，实施前检查，风险低）。
- 配额溢出：revert 即回静默丢；已转假设是正常假设行，
  可单独删除或保留待验证，无残留 schema。
- 回滚命令：`git revert <commit>` 单步回滚（单 commit 交付）。

## 八、已知限制（诚实声明）

- 整轮隐藏是粗粒度：别记轮内的正常部分也一并不可见。
  这是"可预测性优先于精细度"的取舍，下版可议 span 级隐藏，本版不做。
- 配额溢出进假设池：池子会涨，但有去重 + 验证链消耗；
  若用户从不验证，池子胀——这是 V0.5.4 验证触发率调查的输入，不是本版问题。
- 别记判定仍是关键词表（漏网进 D017 附录，下版补），本版不动判定只修引用。
