# D011 · Windows 桌面壳（Electron）与 Core 生命周期（桌面化阶段3）

- 日期：2026-09-26
- 状态：已采纳
- 关联：D009（Provider）、D010（数据管理）、`docs/PRODUCT_TRUTH.md`（"UI 不是 Personal AI"）

## 背景

Core 目前靠 `python run.py` + 浏览器访问 localhost。目标：普通 Windows 用户双击图标即可使用，
不知道 Python / FastAPI / 端口 / 命令行的存在。**Core 是灵魂，Desktop App 是身体和入口。**

## 决策

### 1. 技术选型：Electron（不选 Tauri）

- Core 已是 FastAPI + 现成单页 Web UI，"包壳"是最低风险路径，UI **零重写**
- Node 本机已有；Tauri 要引入整套 Rust 工具链 → 违背"不增加不必要复杂度"
- 管理 Python 子进程生命周期（启动/健康检查/杀进程树）在 Electron 生态最成熟
- 代价（接受）：安装包体积较大——第一版以"能用、稳"优先

### 2. 分层（严格遵守）

```
Desktop App（desktop/）          启动 · 停止 · 连接 · 状态 · 设置入口 · 打包
    ↓ 只通过 HTTP
Personal AI Core                Conversation / Context / Memory / Policy / Model
    ↓
Memory / Context / Policy  ·  Model Provider（D009）
```

- **外壳绝不碰 SQLite、绝不直接改 Context**；与页面之间只有一条 preload 桥
  （`window.pai`：状态/重试/打开日志 三个能力，contextIsolation 开启）
- Core 与 UI 之间是同源 HTTP，无 CORS、无第二套数据通道

### 3. `desktop/src/core-manager.js`：Core 生命周期（依赖注入、可单测）

| 场景 | 行为 |
|---|---|
| Core 已在运行 | 健康检查通过 → **复用连接，绝不启动第二个** |
| 端口被外来程序占 | TCP 通但 `/api/status` 不通 → `port-conflict` 明确报错，不静默 |
| 冷启动 | spawn → 每 400ms 轮询 `/api/status`，60s 超时 |
| 启动即退 | `core-failed` + 带上 stdout/stderr 尾巴（错误页可看） |
| 运行中崩溃 | `crashed` 事件 → 错误页（不是白屏） |
| 超时/退出 | `taskkill /T /F` 杀进程树——**绝不留孤儿 Core** |

启动命令：打包后用自带 `personal-ai-core.exe`（PyInstaller，阶段4）；开发期用
`python -m uvicorn interfaces.webapp:app`（不用 run.py，避免壳里再弹浏览器）。

### 4. 数据/日志目录与安装目录分离

- `core/config.py` 新增 `PAI_LOG_DIR` 覆盖（此前 LOG_DIR 写死项目根，装进 Program Files 会炸）
- 打包模式下外壳注入：`PAI_DATA_DIR=%APPDATA%\Personal AI\data`、`PAI_LOG_DIR=…\logs`
- → 卸载安装目录不带数据；重装自动接回旧数据（`deleteAppDataOnUninstall: false`）

### 5. Ollama 尽力而为拉起（本机实测的坑）

- 现象：直接 `ollama serve` 会读**空的默认模型目录**（模型实际在 `D:\.ollama\models`），
  Core 会报"本机没有模型"
- 规则：`OLLAMA_MODELS` 环境变量 → 默认目录有货则不干预 → `D:\.ollama\models` 有货则指过去
- **永不阻塞、永不抛错**：Ollama 不通只是 UI 显示"模型离线"，Core 本身照常工作

### 6. 页面与交互

- 启动：`loading.html`（状态实时更新，有状态补拉兜底）→ 健康检查通过才加载 Core UI
- 失败：`error.html` 分错误码给**人话解释**（端口冲突/启动失败/超时/崩溃）+ 日志位置 +
  重试按钮 + 技术细节折叠；页面固定注明"你的个人数据是安全的"
- 导出下载：`window.open` → `setWindowOpenHandler` 转 `downloadURL`（走系统下载）；
  外部链接交系统浏览器；导航只允许 Core 同源（防外链带跑）
- 单实例锁：二次双击只聚焦已开窗口
- 优雅退出：`before-quit` 里收 Core（只收自己拉起的）+ 自检拉起的 Ollama

### 7. 自检钩子 `PAI_SHELL_SELFTEST=1`

启动链路走通（或失败分类）后自动优雅退出并打印 `SELFTEST_OK` / `SELFTEST_FAIL:<code>`，
让桌面流程可以被脚本反复回归——"能自动化的尽量自动化"。

## 验证

- `npm test`：**18/18**（复用不重复启动 / 端口冲突 / 启动→健康→spawned / 超时收尸 /
  启动即退带输出 / 崩溃事件 / 主动关闭≠崩溃 / 命令与环境装配 / 模型目录四态 / Ollama 三态）
- 真机自检：
  - 场景A（Core 已运行）→ 复用连接 ✅
  - 场景B（Core 未运行）→ 自己拉起 → 健康检查 → 加载 UI → 优雅退出 → **无孤儿进程、端口释放** ✅
  - 场景C（外来程序占 8000）→ `SELFTEST_FAIL:port-conflict` 明确报错 ✅
- `python -m pytest tests`：**152 passed**（config 改动无回归）

## 已知边界（阶段4处理）

- 还没有打包安装（PyInstaller + electron-builder 在阶段4）
- 开发期壳拉起的是 `python`；打包后必须是自带 exe（`buildCoreCommand` 已就位）
