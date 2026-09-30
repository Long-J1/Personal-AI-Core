# V0.5.2 Trust Boundary（信任边界）

> 地位：V0.5 系列修复分支，只修信任基础，不新增能力。
> 定位：**还债版**——不是增强版。Day7 证明"记得"成立，但信任三断裂
> （幻觉/不断根/假删除）没修好之前，记得越多越危险。
> 本版让 V0.2-V0.5 的所有能力第一次敢被信任。
> 触发：V0.5.1 真实 32 轮审计 + V0.5 产品级验证任务 + 最终架构审查（6 处强制修改）。
> 状态：**已实施**（测试 24 项 + acceptance 12 步 + 全量回归通过，待 commit）

---

## 一、唯一目标（一句话）

让 AI 知道"什么可以记，什么不能记，什么时候应该承认不知道"。

## 二、Scope（只做五件事）

| # | 内容 | 对应审计发现 |
|---|---|---|
| P0-1 | Memory 写入防火墙：来源标记 + 证据检查 + 拦截规则 | 幻觉入库（指数函数、9月30日） |
| P0-2 | 纠正系统升级：memory_status（active/outdated/rejected）+ 原因 | 纠正不断根（函数条目永 active） |
| P0-3 | "别记这个"硬规则：本轮零写入 + 诚实回答 | 删除指令被当记忆素材 |
| P1-1 | R2 标签卫生：清洗 + bigram 相关性 | 引号 scope → 13 轮零提问 |
| P1-2 | Memory 质量评分：confidence/source/last_verified + 排序 | 高低可信混排 |

## 三、Non-goals（红线）

V0.6 Agent 行动 / 自动执行 / Web Search / 多 Agent / RAG / Embedding /
向量数据库 / 语音 / 外部设备 / 新 UI / 新 Pattern 机制 / 新 Goal 机制 /
云端模型 / 大规模重构。本地模型 + tmp 测试 + 真库零触碰 + acceptance 风格测试不变。

---

## 四、P0-1：Memory 写入防火墙

### 4.1 当前链路（审计确认）

用户输入 → LLM 总结 → deposit → 写入 memory。全经模型 JSON，
条目只有 kind/content，无来源标记。

### 4.2 目标链路

用户输入 → LLM 总结 → **Evidence Check（规则）** → 允许写入 → deposit。

### 4.3 来源枚举（`source_grade` 列，migration 默认见 §七）

- `user_direct`（用户明确表达）：允许写入，置信 0.95。
- `user_confirmed`（用户确认：纠正命中/手动确认/验证确认）：允许写入，置信 0.95。
- `db_grounded`（既有理解的复述——有据可查才准进）：允许写入，置信 0.8。
  有据复述不是推测（"CSP最近状态怎么样"→"CSP备考顺利"这类有依据提问的沉淀
  必须留在 memory，V0.5.1 保证）；但也不冒充用户原话，置信低一档。
- `inferred`（模型推测）：**只能进入 hypothesis（tentative），禁入 memory**。
- `system_generated`（系统生成）：**禁止进入任何长期存储**（枚举保留，
  用于分类器拒绝路径的单测）。

### 4.4 分类规则（确定性，不调模型）

对 deposit 每条 `new[]`，按顺序执行，首中即定。
混合句（用户原话 + 推断搭车，如"用户说考试前提醒，说明他焦虑"）先按
推断连接词（说明/意味着/表明/看来/反映出）切分子句，逐子句分类；
分句残留的尾巴标点在 `_split_clauses` 出口统一剥掉（切分副产品，不属于记忆内容）。

1. **硬拦截**（直接丢弃，记日志 `firewall_reject`，不进任何表）：
   - 含"你之前说过/上次你提到/你说过"且 DB 无对应证据（关键词交集为空）；
   - 含既往事件断言（`上次/之前` + `发生/做过/去过/见过`）且 DB 无对应事件；
   - 含具体日期（精确 token：`9月30日`/`30日`/`周三`，必须含数字防"日子"误报）
     且 DB/本轮用户原文均无该日期；
   - 含人名（`X(叫|名叫|名字是)` 提取前后文探针）且 DB/用户原文均无该人名；
   - 性格评价（`_is_personality_judgment`：性格词表 + "是个…的人"句式双判——
     模型主语多为"用户"而非"你"，只列"你很X"会漏）→ 判 `system_generated`，拒绝。
2. **用户原话判定**：归一化（去标点/空白/人称前缀）后条目 ⊆ 用户原文，
   或二连字 overlap 系数达标 → `user_direct`。
   阈值按 kind 分档（disaster test + v04 回归双重校准）：
   interaction（用户对 AI 的要求，先验更高）取宽松线——共享≥1 且 overlap≥0.2；
   其余 kind 取严格线——共享≥2 且 overlap≥0.4。
   overlap 系数 = 共享数 / min(条目克数, 原话克数)（防长度碾压）。
   注意："你喜欢X"按出处判，不按句式判——用户原话复述就是 user_direct。
3. **advisory 丢弃**：泛化建议（主语非 AI 的"你应该…"，如"你应该早睡"）
   既不进 memory 也不进 hypothesis，直接丢弃，连表都不建。
   例外：主语是 AI 的要求（"你应该提醒我"）→ 正常走 interaction 分类。
4. **兜底**：以上皆非 → `inferred` → 转 hypothesis 待验证池（同 statement 去重），
   不写 memory。

### 4.5 验收（Test1 幻觉污染）

- 用户从未说过生日；问"你记得我生日吗？" → 回答不知道（以"没有相关记录"类表述为准，
  不苛求逐字）；memory 零新增（含 hypothesis 也不许建——"生日"无任何证据，
  连猜的资格都没有；单测断言 `hypothesis_store` 同样零新增）。
- 附带断言：拦截动作写日志（`firewall_reject` + 原因），可审计。

## 五、P0-2：纠正系统升级

### 5.1 当前问题（审计确认）

`context_entries` 只有 active 0/1。用户否认后，deposit 模型路径经常产生
"新增一条对的"而不修正旧条（"覆盖了之前的认知"只活在回答里），旧假条目
永久 active 并继续进 prompt。V0.5.1 兜底只打得到被假设引用的条目，
plain context 够不着。

### 5.2 改成

- 新增列 `memory_status`（`active`/`outdated`/`rejected`，migration 默认 `active`）
  + `status_reason`（`user_correction` / `superseded` / `false_denied` 等枚举，
  存最近一次变更原因）。
- 否认流程（规则，不依赖模型 JSON）：用户原文含显式否决词 →
  关键词关联本轮 prompt 用过的条目 → 命中条目记 `outdated`（软否认，如"最近几天这样"）
  或 `rejected`（硬否认：用户原话含"不对/不是/没有/错了/胡说"且指向明确条目），
  `status_reason=user_correction`。
  软硬划分（`HARD_DENY_WORDS`，子集须 ⊆ 假设库否决词表）：事实性纠错 → rejected；
  行为纠偏（"别/不要/纠正"）→ outdated。
  无依据提问门拦下的 `new[]` 同样记 `firewall_reject` 日志（可审计口径统一）。
  **联动相似复本**：同 kind 相似度≥阈值的活跃条目一起标 `outdated`
  （reason=`propagated`，日志写清来源 id）——纠正打地鼠到此为止。
  **级联到 Pattern**：hypothesis 被 reject → 引用它的 pattern 加 contradict（-0.20）。
  不删除（历史保留可分析）。
- prompt 组装只读 `active` 条目；`outdated`/`rejected` 永不进入 prompt。
- V0.5.1 的假设联动保留（双通道并存，互不重复处罚：同一条目同一轮只记一次，
  以"条目状态变更"为准，假设侧去重索引已有）。

### 5.3 验收（Test2 纠正攻击）

- "我喜欢咖啡。" → "不对，我讨厌咖啡。" → "我喜欢什么？"
  第三轮不得回答喜欢咖啡（以"不含咖啡偏好肯定句"为准；允许回答"记录显示你agan改过"类诚实表述——以不出现肯定句为硬标准）。
- 附带断言：旧条目 `status=rejected` + reason 非空；prompt 文本不含该条目。

## 六、P0-3："别记这个"真正实现

### 6.1 当前问题（审计确认）

无任何抑制路径（暂停是唯一开关）。"昨天打游戏的事，你别记这个"产生
3 事件 + 4 理解，其中一条就是"用户要求删除"本身；回答却说"已清除"——
撒谎式安抚，信任核心事故。

### 6.2 改成（硬规则，不让模型决定）

- `conversation.chat` 入口：用户原文命中 no-memorize 模式
  （`别记|不要保存|不用记|别存|忘掉这个|删除.*记忆|别记住|不要记住|没必要记|只是随便聊聊|不要以后提`，
  常量可测）→ 本轮跳过 deposit/inducer/promoter/B-intake/goal-propose **全部写路径**
  （只读 + 回答照常；history 照常追加，保证对话连贯）。
- **边界（加粗）**：history（chat_store）**不算**长期记忆。"DB 零新增"验收指
  events/context/hypothesis/pattern/goal 五表，history 追加不在此列。
  漏网的新说法进 D017 附录"失败 admission"，下版补。
- 诚实回答：system prompt 本轮注入固定指令——
  "用户要求不记录。如提及记忆，必须说这次内容不会进入长期记忆；
  禁止说'记住了/已清除记忆标签'等暗示已执行删除的话术。"
  可测部分：单测断言本轮 system prompt 含该指令 + DB 零新增；
  回答措辞只做宽松断言（BehaviorMock 风格：mock 按指令回固定句）。
- 注意：是"不写"，不是"删除旧的"——用户说"别记这个"指当轮内容；
  历史删除走现有 delete API，不混。

### 6.3 验收（Test3 删除攻击 + Test4 闲聊污染）

- "不要记住我喜欢玩某游戏。" → DB（events/context/hypothesis/pattern 全表）
  零新增；后续问相关话题，回答不得引用该游戏。
- "哈哈/嗯/真的吗"连续 5 轮 → 长期理解零增长（已有行为，保持住，用单测锁死）。

## 七、Migration 策略（老库零破坏）

- `context_entries` 加列：`source_grade`（默认 `'inferred'`——诚实原则：
  历史条目无法区分，一律按低可信处理）、`memory_status`（默认 `'active'`）、
  `status_reason`（默认 `''`）、`confidence`（默认 `0.5`）、`last_verified`
  （默认 `0.0`）；`source IN ('manual','correction')` 的行回填
  `user_confirmed` / 0.95（用户亲手写的，信任）。
- `hypothesis_store` / `pattern_store`：不 Else，无 schema 变更。
- 单测：老 schema 库初始化 → 列存在 + 默认值正确 + 旧数据可读。

## 八、P1-1：R2 标签卫生修复

### 8.1 当前问题（审计确认，代码点对点）

- `_theme_label` 取原始内容 LCS，标点引号零清洗 → scope 如
  `interaction:“考试前一天”`；`is_related` 纯子串 → 用户永不输入弯引号 →
  13 轮真实对话零提问。P0 的停用词只管了连边，不管标签。
- 连带：deposit 长句（"用户希望AI在…"）产出 `用户希望AI` 类垃圾标签。

### 8.2 改成

- `clean_label()`：去中英文引号/标点/编号（`#1`/`1.`/`(1)`/【】）、
  去 AI 前缀（`^(AI|用户希望AI|系统)`）、去空洞词（复用 STOP_GRAMS +
  用户点名的用户/希望/AI/系统）、长度限制 2~8 中文字符；
  清洗后 <2 字 → 丢弃该组（宁可少猜）。
  另加**原话约束**：标签每个字必须在成员的 user_direct 片段里出现过；
  全由模型措辞拼成的候选串直接丢弃，换次优（防 deposit 长句污染）。
- `is_related` 改 bigram 交集：消息关键词二连字 ∩ 标签二连字非空即相关
  （停用 bigram 除外）。"编程怎么学习" ↔ "学习规划" 共享"学习" ✓；
  弯引号问题自然消失（清洗后无标点）。
- 验收拆两处（不许混在一起）：标签质量（无标点/2~8字/非停用/来自原话）；
  相关性（共享非停用 bigram 即相关）。单测断言
  `clean_label("“考试前一天”") == "考试前一天"`；acceptance 加
  "编程怎么学习 → 匹配 学习规划"用例（Mock 搭台，真规则跑）。

## 九、P1-2：Memory 质量评分

- 列：`confidence` / `source_grade` / `last_verified` /
  `evidence_count` / `contradiction_count`（见 §七）。
  `created_by` 不加（≈source_grade，无信息增量，拒绝列膨胀）。
- 赋值：`user_direct` → 0.95；`user_confirmed` → 0.95（确认即顶格，沿用"确认≠置信"？
  不——此处 confidence 是**条目可信度**，确认直接给 0.95，语义与假设置信度区分，
  注释写清）；`inferred` 不进 memory（§4.4）。
  `last_verified`：被引用/确认/纠正命中时刷新为 now。
  `evidence_count`/`contradiction_count`：被引用+1 / 被纠正命中+1（硬通货）。
- 时间衰减 `decay`：读取权重 = confidence × 0.5^(闲置天数/30)
  （half-life 30 天，常量可测）。上周偏好 vs 三个月前偏好必须有权重差，
  否则旧记忆永久平权。
- `trust_level` 派生函数：confidence + source + 新鲜度 + 矛盾史 → 高/中/低三档，
  只用于排序（展示后议，UI 不动）。
- Prompt 使用：组装排序改为（trust_level desc，decay 权重 desc），现有条数上限不变；
  prompt 文本格式不动（零文本 churn，行为变化只来自排序）。
  单测：_assemble 返回顺序断言。
- 纠正命中条目：confidence 置 0.25 同步（与假设 CORRECTION_SET 对齐），
  状态按 §五走。

## 十、测试要求（只测，不演示）

- Test1 幻觉污染（§4.5）、Test2 纠正攻击（§5.3）、Test3 删除攻击 + Test4 闲聊污染（§6.3）、
  Test5 自夸限制：用户问"你是不是已经完全了解我？" → 注入诚实指令
  （"只掌握 N 条理解、M 个待验证假设，不得声称完全了解"），单测断言指令存在 +
  Mock 固定回答不断言逐字。
- 每个能力配"行为变化"断言（prompt/response 双断言，D007 老办法）。
- 全量回归 + acceptance_v051（tmp+Mock，沿用 v03/v04/v05 脚本风格）。
- 真库零触碰，零云端调用（铁规延续）。

## 十一、已知限制（诚实声明）

- 中文人名/日期启发式会误伤（"周杰伦的歌"可能被当人名拦截）→ 白名单机制预留，
  本版先保证"宁可少记"，误伤案例进 D017 附录。
- 否决词疑问句（"不对吗"）仍只靠"无关联即跳过"兜住，无语义级区分（V0.5.1 遗留）。
- 中文数字覆盖常用字；兆/亿未列。
- `system_generated` 枚举保留但 deposit 永远不产生该值——它是分类器的拒绝出口，
  不是写入路径，注释写明防后人误用。
