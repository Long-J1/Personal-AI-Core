# D003：记忆库用 SQLite + LIKE 关键词检索（暂不上向量库）

- 日期：2026-09-26
- 状态：已接受
- 背景：任务书 §7 要求事件记忆为核心资产，V0.1 边界是"SQLite + 事件表 + 简单检索"，明确不做分布式向量库。
- 决定：
  1. 单文件 `data/personal_ai.db`，表 `events`（事件）+ `facts`（提炼的长期事实）+ `meta`（隐私状态等元信息）；
  2. 关键词检索用 `LIKE '%词%'` 对中文直接子串匹配；时间检索用 `created_at` 索引；
  3. 不做 FTS5：其默认分词器不切中文，jieba 又是新依赖，V0.1 数据量（几百条）LIKE 足够。
- 理由：零依赖（sqlite3 是 Python 标准库）、可解释、数据结构稳定，未来升级向量检索时只加一张 `embeddings` 表，不动事件表。
- 影响：`memory/store.py`；检索接口 `search(time_range, keywords, limit)` 保持稳定，未来可内部换实现。
