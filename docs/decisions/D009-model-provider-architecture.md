# D009 · Model Provider 架构与模型设置（桌面化阶段1）

- 日期：2026-09-26
- 状态：已采纳（V0.2 → Desktop 化阶段1）
- 关联：`docs/PRODUCT_TRUTH.md`（"模型不是 Personal AI"）、D002、D008

## 背景

V0.2 的 `webapp.default_model()` 硬编码 `OllamaAdapter`，模型供应商写死在 Core 里。
桌面化任务要求：Personal AI 可以使用不同模型（本地 Ollama / 任意 OpenAI-compatible 云端 API），
但 **Personal Context 与模型无关**——换模型只换推理引擎，不换"它"。

## 决策

### 1. Provider 分层（不改既有依赖注入）

```
ModelProvider（沿用 BaseModelAdapter 接口）
├── OllamaProvider        = 既有 OllamaAdapter（原名保留，别名 OllamaProvider）
└── OpenAICompatibleProvider   通用 /chat/completions，不绑定任何一家云厂商
```

- **Core 只认 `BaseModelAdapter`**，`create_app(model=...)` 注入签名不变——113 个旧测试的
  Mock 注入方式原样保留。
- `OpenAICompatibleProvider` 是纯通用实现：Base URL / API Key / 模型名全部可配；
  401/403 报"API Key 无效"；无 `/models` 路由时降级用 1-token 对话探测。
- 云端请求 `trust_env=True`（要走系统代理出网），与 Ollama 本地 `trust_env=False` 相反。

### 2. ModelRegistry（模型门面 = 热切换接缝）

- Core 各服务只持有 `ModelRegistry`；`reload()` 从设置存储重新装配当前 Provider。
- **切换 Provider 只换门面后的对象**：Conversation / Context / Memory / 事件一概不动
  （验收测试：`test_switch_provider_preserves_context_and_events`）。
- 配置无效时降级为 `_BrokenModel`（状态显示"配置无效"、连接测试给原因），**App 永不因配置坏而起不来**。

### 3. 设置与 API Key 存储

- 配置文件：`数据目录/model_settings.json`（`data/` 已在 .gitignore，桌面版将指向 `%APPDATA%`）。
- **API Key 三条铁律**：不进源码 / 不进 git / 任何 API 返回前经 `public_view()` 脱敏
  （只回 `api_key_set` + `api_key_masked`，如 `sk-a...xyz`）。
- Key 明文落盘在用户数据目录（Windows 用户目录 ACL 保护），换壳换模型时随数据一起带走。
  第一版接受此方案；若未来要更强保护再引入 Windows DPAPI（暂不增加复杂度）。
- patch 语义：请求里**不带** `api_key` 字段 = 不修改；空串 = 清除。

### 4. 新增 API（全部在 Core，UI 只是调用者）

| 端点 | 作用 |
|---|---|
| `GET /api/model/settings` | 读配置（脱敏）+ 当前 Provider/模型 |
| `PUT /api/model/settings` | 保存并热切换（校验失败 400，不落盘） |
| `POST /api/model/test` | 连接测试：**用临时配置测，不落盘** |
| `/api/status` 新增 `provider` 字段 | 状态栏显示当前 Provider |

### 5. 顺手修掉一个"时间炸弹"测试（与 Provider 无关）

`tests/test_context.py::test_recall_falls_back_to_all_when_window_empty` 原先种 10 小时前的
事件问"今天…"：11 点前跑窗口为空→通过，11 点后跑事件落进当天窗口→必挂（V0.2 验收在晚上跑
纯属运气）。改为 36 小时前，任何时刻语义一致（窗口空→兜底全部）。**产品行为零改动**，
`core/context.py` 一行未动。

## 验证

- `python -m pytest tests`：**138 passed, 3 skipped**（3 个 reale2e 需 Ollama；旧 113 全绿 + 新增 28）
- 新增测试：`tests/test_model_provider.py`（请求形态 / 图片转 data URL / 401 / 无 Key 不带头 /
  连接测试三态 / 脱敏 / Key patch 语义 / 校验 / 坏文件回退 / 工厂 / 注册表热切换 /
  坏配置降级 / API 不回显 Key / **切换不丢 Context** / Mock 注入不破坏）
- 真实环境：Ollama 在线 + `GET/PUT /api/model/settings` + `POST /api/model/test` + reale2e

## 影响面

- 新增：`models/settings_store.py`、`models/openai_provider.py`、`models/factory.py`、
  `tests/test_model_provider.py`
- 修改：`interfaces/webapp.py`（default_model → ModelRegistry + 3 个端点 + status.provider）、
  `models/ollama_adapter.py`（+test_connection、+别名）、`interfaces/static/index.html`（⑥ 模型设置）
- **未动**：Conversation / Context / Memory / Pipeline / Policy 语义
