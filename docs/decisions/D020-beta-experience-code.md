# D020 — Beta 体验码（V0.5.5 真实用户入门层）

日期：2026-10-01
状态：已实现（**未发布**，等第一批用户实机验证）

---

## 一句话

让第一次用的人不用碰 `Provider / Base URL / Model / API Key`，
点一下「Beta 体验」、输入体验码，就能直接开始聊天。

---

## 这是什么（准确定位，不要误读）

这是 **V0.5.5 早期 Beta 测试体验凭证**。

它**是**：

- 一个让新用户跳过开发者配置流程的**工程捷径**
- 一个降低上手门槛的**体验设计**

它**不是**：

- ❌ 安全授权系统
- ❌ 账户系统
- ❌ 正式用户系统
- ❌ 计费 / 配额系统

**不要在任何文档、注释、对外说明里把它描述成"权限控制"或"授权"。**

---

## 已知安全限制（必须如实知道）

| # | 限制 | 后果 |
|---|---|---|
| 1 | 凭证构建时打进安装包 | 任何人都能从 exe 里提取 Key |
| 2 | 体验码只在本机做一次 SHA-256 比对 | 没有服务端校验、没有吊销、没有过期 |
| 3 | 没有设备绑定、没有次数限制 | 码可无限转发 |
| 4 | 谁拿到码谁就能用 | 用量直接算在作者那个云端账号上 |

所以它的定位是**短期 Beta 阶段的省事工具**，不是安全边界。
真要做授权，必须走 Gateway（见 `docs/PLAN_gateway.md` 的前置条件）。

---

## 凭证从哪来、放到哪

### 构建时注入（不进仓库）

`scripts/build_core.ps1` 在 PyInstaller 之前生成 `models/_beta_preset.py`：

```powershell
$env:PAI_BETA_API_KEY   # 预设云端 Key
$env:PAI_BETA_CODE      # 体验码明文（用于算 SHA-256）
$env:PAI_BETA_BASE_URL  # 可选，默认智谱
$env:PAI_BETA_MODEL     # 可选，默认 glm-4-flash
```

四条铁律：

1. **凭证只从环境变量读**，脚本里不出现任何明文
2. 生成的 `models/_beta_preset.py` **已 gitignore**，不进仓库、不进 history
3. 源码仓库**绝不**包含真实 Key
4. 测试**只**用 fake key（`sk-FAKE-BETA-TEST-KEY-NOT-REAL-0000`，内存注入 `sys.modules`，不落盘）

### 运行时存储

沿用**现有**的 `data/model_settings.json`（它本来就明文存 `api_key`），
**没新增存储层、没改 `ModelSettingsStore`**。

| 文件 | 作用 |
|---|---|
| `data/model_settings.json` | 当前生效配置（Beta 预设就写这里） |
| `data/beta_state.json` | 是否已解锁 + 解锁时间（**不存凭证**） |
| `data/saved_config.json` | 解锁**前**的原配置备份，供"关闭 Beta"还原 |

「关闭 Beta 体验」只动模型设置：
**不删 Memory、不删聊天历史、不碰数据库。**

---

## 体验码怎么验证

纯本地，不联网、不上报：

```python
sha256(用户输入) == 预设里的 code_sha256[]
```

用 `hmac.compare_digest` 常量时间比较。
预设里存的是**摘要不是明文码**，所以安装包里也搜不到码本身。

失败时只回一句中性的话：

> 体验码无效或当前不可用

已用测试断言失败文案里**不出现** `base_url` / `model` / `sk-` 任何片段。

---

## API 端点

| 端点 | 作用 |
|---|---|
| `GET /api/beta/status` | 是否可用 / 是否已激活（前端据此显示入口） |
| `POST /api/beta/unlock` | 输码 → 套用预设 → 自动连接测试 |
| `POST /api/beta/disable` | 关闭 Beta → 原样还原解锁前的配置 |

启动时 `apply_on_startup()`：Beta 活跃但配置被外部改过 → 重新套用预设
（否则"解锁后改了设置、重启就失效"）。

---

## UI 位置

两处，都是普通用户会看到的地方：

1. **首页空状态** —— 绿色卡片「第一次用？先开 Beta 体验」，第一屏就能看见
2. **「模型」面板顶部** —— 同一张卡片，压在 Provider/Base URL 字段**上面**

未激活 → 弹窗输码；已激活 → 状态条 +「关闭 Beta 体验」。

---

## 开发环境没有 Beta 功能？

**这是正确行为。** 没生成过 `models/_beta_preset.py` 时：

- `available = False`
- UI 入口隐藏
- 输码返回「体验码无效或当前不可用」

不是报错，是"当前版本未提供"。

---

## 已知坑（别踩回去）

### 坑 1：preset 结构必须扁平

预设是扁平的（`provider / base_url / model / api_key`），
存盘结构才是分段的（`openai.xxx`）。

曾因此在 `apply_on_startup` 里只换了 provider 没换 model ——
**测试抓到了**。现在两处统一走 `_preset_patch()`，别再各写一遍。

### 坑 2：测试绝不能写真库

`create_app()` 里 `model_settings_store = default_store()`，
那指向**真实用户数据目录**。测试必须 monkeypatch：

```python
monkeypatch.setattr(W, "default_store", lambda: mss)   # mss 指向 tmp
```

否则一次 `unlock()` 就把用户的 `model_settings.json` 覆盖了。

### 坑 3：测试 preset 走内存，不落盘

```python
monkeypatch.setitem(sys.modules, "models._beta_preset", mod)
```

这样 `load_preset()` 走的是**真实代码路径**（`__import__` 命中缓存），
但既不写磁盘也不依赖外部文件，测试自包含。

---

## 本阶段明确不做

Gateway · 用户注册登录 · 云端 token · 用户数据库 · 计费 · 配额 · 手机端 · 新 Memory 架构

---

## 将来真要做正式授权，需要补什么

只是**前置条件清单**，不是本阶段方案，**本阶段一行代码都没做**。

**最小可用（能防"码被转发"）**
- 一台服务器 + `POST /api/beta/redeem`（收码 → 签发短期 token）
- 客户端改成拿 token 调 `https://<gateway>/chat`，不再直连智谱
- 凭证只在 Gateway 侧，客户端根本拿不到
- 需要：一个域名（或 Cloudflare Tunnel）、TLS 证书

**加限额**
- Gateway 侧记 usage（按 token 或次数），超了拒绝
- 需要：数据库（SQLite / Postgres 都够）

**加管理**
- 管理面板：发码、看谁用了、封禁
- 需要：管理员鉴权（一个密码或 TOTP 就够）

**用户系统**
- 注册 / 登录 / 找回 —— 成本最高，等确实需要时再做

**成本提醒**：Gateway 一上线，你的云端账号就从"我自己在用"变成"替所有人扛"。
用量不可控，必须先想清楚兜底（限额 / 白名单 / 随时能关）。