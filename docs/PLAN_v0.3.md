# V0.3 计划 — Personal Hypotheses（可证伪的假设）

> 依据：`docs/PRODUCT_TRUTH.md`（北极星问题与功能过滤器）、成长模型 §13/§15（V0.3 行、事实与推测分离）、执行任务书 V0.3（§一~§二十三）
> 前置：V0.2 已验收（PLAN_v0.2 全 ✅）；Python 测试基线 **152**（任务书所写 113+ 为 V0.2 交付时数值，之后修启动/壳问题新增约 39 个，回归按 152 算）
> 状态：**进行中**

---

## 一、V0.3 唯一要证明的命题

> **Personal AI 能否从已经发生过的事实/事件中，谨慎地形成一个关于这个人的、可被推翻的假设，并让这个假设影响后续判断，同时允许用户否定它并让系统修正？**

本版最重要的不是假设数量，而是第一次让 AI 学会："我对这个人的理解可能是错的。"
关键字：**可追溯、可解释、可证伪、可修正、不自我强化。宁可少猜，不乱猜。**

---

## 二、Scope（做）

| # | 内容 | 对应任务书 |
|---|---|---|
| 1 | `hypotheses` 独立表（与 `context_entries` 物理隔离） | §五 |
| 2 | `hypothesis_evidence` 关系表（support/contradict，追溯到具体行） | §五 §九 |
| 3 | `hypothesis_log` 变更/置信度审计表（为什么这么认为，可查） | §五 |
| 4 | 显式置信度数学（常量写死，可测） | §十一 |
| 5 | 规则式归纳器 R1（活动重复）+ R2（相处偏好主题），后台静默跑 | §八 |
| 6 | 纠正→假设联动（软反证 -0.25；显式否决 → 0.25 + rejected） | §十五 |
| 7 | 假设进 Prompt：单独段、降权标记、tentative/confirmed 才进 | §十四 |
| 8 | Trust UI：假设独立区块（查看/confidence/状态/证据/确认/否决/删除） | §十六 |
| 9 | API：`GET /api/hypotheses`、`POST /{id}/confirm|reject`、`DELETE /{id}`、`POST /{id}/restore` | §十六 |
| 10 | 测试 H1~H6 + `scripts/acceptance_v03.py` | §十七 |

## 三、Non-goals（不做，V0.4+）

- 主动提问验证假设、InterventionPolicy 扩展（桩子保持原样）、自动执行
- Self/World Model、向量/RAG/Embedding、LLM 生成假设或画像
- 改 `context_entries` 结构与语义、改 `create_app(model=...)` 注入签名
- 官网 / installer / SHA / release（完整验收后统一做）
- 真实数据库灌数据（真库 18 Context / 4 Event，近期无假设是**正常**的）

---

## 四、数据模型

同库（`personal_ai.db`）三张新表，不动旧表：

```sql
hypotheses(id, statement, confidence REAL, status, scope,
           support_count, contradict_count,
           created_at_iso/epoch, updated_at_iso/epoch, used_count)
hypothesis_evidence(id, hypothesis_id, polarity[support|contradict],
           source_type[context|event|correction], source_id, origin,
           excerpt, detail JSON, created_at_iso/epoch)
hypothesis_log(id, epoch, iso, action, hypothesis_id,
           conf_before, conf_after, detail JSON)
```

- `status`：`tentative`（初始）→ `confirmed`（用户确认）/ `rejected`（用户否决）→ `deleted`（逻辑删，可 restore 回 tentative）。
- **用户确认 ≠ 高置信度**：confirm 只改状态，不动 confidence（两者是两个维度的信息）。
- 计数器与 confidence 在同一事务内更新，不漂移。

## 五、Evidence 模型与独立性定义

- 每条证据必须指向真实行：`context`→`context_entries` 存在；`event`→`events` 存在；`correction`→被纠正的 `context_entries` 行存在，或本次否决的 `hypothesis_log` 行存在（信任界面整条否决时，用户行为本身就是证据）。**`hypothesis` 永远不允许作为 source_type**（铁律进代码 + 测试）。
- `excerpt` 存来源内容快照：原行被删也能解释"当时为什么这么想"。
- **独立源定义（保守版）**：`origin` 分组键去重计数。
  - event 证据：`event:<event_id>`（每次事件都是一次独立发生）
  - context 证据：`ctxday:<YYYY-MM-DD>`（同日多条只算 1 个源——宁可保守）
  - correction 证据：`correction:<source_id>:<day>`（每次用户纠正都是一次独立用户行为）
- 同一次归纳批量链接的多行若 origin 相同，只计 1 个独立源（防"一次对话 3 条 Context 冒充 3 个证据"）。

## 六、Confidence 数学（常量，`memory/hypothesis_store.py` 顶部）

```
初始            0.50
每条支持证据     +0.08
每条反证         -0.25
用户亲口纠正/否决  直接置 0.25（固定值）
上下限           clamp 到 [0, 1]
门控             独立支持源 < 2 → 写入时 cap 到 0.70
```

- 增量式更新（非重算）：旧支持不会在被否决后"复活"冲高（§十二.6）。
- 反证权重 ≈ 3 倍支持权重：一次反证顶掉三条支持。

## 七、规则归纳器（`core/inducer.py`，无 LLM）

- **R1 活动重复**：相同非空 `activity` 的事件 ≥ 3 → 假设"用户近期多次进行「X」相关活动（n 次），可能正处在相关事项的持续期。"scope=`activity:X`，证据=这些事件。
- **R2 相处偏好主题**：≥ 2 条 active interaction 条目共享关键词（`extract_keywords`，≥2 字）→ 假设"用户在「kw」方面多次表达过相处偏好（n 条），可能希望 AI 在此类场景保持一致。"scope=`interaction:kw`，证据=这些条目。
- **去重**：同 scope 已有 active 假设 → 不新建，只链接新增证据（走正常置信度路径）。
- 触发点：`conversation.chat` 沉淀完成后，后台静默跑（try/except 包住，只记日志，绝不打断对话）；另提供 `induce_all()` 供测试/验收直调。
- 阈值全是模块常量。

## 八、纠正联动（§十五链）

`Depositor` 新增可选 `hypothesis_store` 参数（默认 None 即跳过，注入兼容）：
纠正成功应用到某 context 条目后 → 查以该条目为支持证据的 active 假设 →
加 contradict 证据（-0.25）；若用户原文含显式否决词（`不对|不是|没有|别|不要|错了|纠正`，常量可测）→ 置 0.25 + 状态 rejected。

## 九、Prompt 行为（最小扩展，不重写 assembly）

`_assemble` 末尾追加一段（tentative + confirmed，按 confidence 倒序，最多 `settings.hypotheses_max_prompt = 3` 条）：

```
=== 待验证假设（辅助判断用，不是事实；若与用户当下所说冲突，以用户为准）===
- [待验证·置信度0.58] ……
- [用户已确认·置信度0.66] ……
```

- `approach` 语义不变（仍只由 interaction 指令决定，D007 不动）。
- `ChatReply` 新增 `hypotheses_used`（加法字段）；`/api/status` 新增 `hypotheses` 计数（加法字段）。

## 十、UI 行为

`index.html` 在 ④ 之后加独立区块"待验证假设"（视觉分开，§十六）：
列表展示 statement + confidence + 状态 + 证据数；展开看支持/反证来源；
按钮：确认 / 否决 / 删除（confirm 风格与④一致）。不做评分、图谱、雷达图。

## 十一、测试计划（H1~H6）

- **H1**（store 数学）：初始 0.5；+0.08；-0.25；clamp；门控 cap 0.70；correction→0.25；hypothesis 作证据被拒绝；证据行不存在被拒绝；同日 collapse。
- **H2**（归纳）：fixture（tmp 库，与 conftest 同风格）→ R1/R2 产生假设且能说出为什么（scope/证据断言）；去重不断言重复建。
- **H3**（反证衰减）：support↑、contradict↓、非对称权重、否决后旧支持不复活。
- **H4**（纠正联动全链，Mock 模型）：seed interaction → chat → deposit → induce → 假设出现 → 否决性纠正 → 反证+confidence 降+prompt 段变化；approach 语义不回退。
- **H5**（API）：list/confirm/reject/delete/restore + evidence 可见；注入兼容（旧 `create_app(store, model)` 调用仍可用）。
- **H6**（回归）：152 旧测试全绿 + 新增全绿；`scripts/acceptance_v03.py`（沿用 v02 脚本风格，fixture 库，不碰真库）+ `scripts/acceptance.py` + `acceptance_v02.py` 重跑。

## 十二、验收标准

1. H1~H5 全部通过（含至少一次"撤掉修复→测试变红"的反向验证，沿用壳测试的做法）。
2. 152 回归零失败；三套 acceptance 全过。
3. 真实数据库内容零修改（acceptance 只用 tmp 库）。
4. 文档：本文件 + `docs/decisions/` 记录关键选择（含被拒绝的候选项：LLM 归纳、向量检索、R3 纠正主题归纳）。
5. 小步提交，最终一个 V0.3 commit + `v0.3.0` tag（tag 打，release 不发）。

## 十三、已知限制

- 真库数据太薄，短期内归纳器产出为 0 是预期行为，不是 bug。
- R2 关键词共享是字面匹配（沿用 `extract_keywords`），语义相近但用词不同归纳不出来——V0.4+ 实验方向，不在本版。
- 同日 collapse 偏保守：真·一日多独立场景会被低估，换来的是不自嗨（§十二要求）。

## 十四、Future work（记录，不实现）

LLM 归纳实验、R3 纠正主题归纳、向量语义检索、V0.4 主动提问、V0.5 介入、V0.6 Self/World。
