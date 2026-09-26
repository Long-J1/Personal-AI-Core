# 桌面版手工验收清单（阶段 5）

日期：2026-09-26
对应：Desktop 任务书 §13 验收项（11 项）；每项给出**操作步骤**与**实测证据**。
自动化复跑入口见文末"回归命令"。

> 环境：Windows 11 + Ollama 0.32.15（本地 Gemma-4 视觉模型在线）；
> 安装包 `desktop/dist/PersonalAI-Setup-0.2.0.exe`（110.4 MB）。

## 验收项

### 01 桌面 App 可以启动 ✅

- 步骤：双击桌面快捷方式 "Personal AI"。
- 预期：显示加载页 → 自动拉起 Core → 健康检查通过 → 打开界面，**任何情况下不白屏**。
- 证据：自检模式实测两轮 `PAI_SHELL_SELFTEST=1` 启动 → `/api/status` 健康
  （provider=ollama、model_service_ok=true）→ 加载 index → 退出码 0。

### 02 安装包可以生成 ✅

- 步骤：`powershell -File scripts\build_core.ps1` → `cd desktop; npm run dist`。
- 预期：产出 `desktop/dist/PersonalAI-Setup-0.2.0.exe`。
- 证据：110.4 MB 安装包实产成功（PyInstaller onedir Core 86.5 MB 打入 `resources/core/`）；
  镜像与坑位记录见 `docs/decisions/D012`。

### 03 安装后可运行，无需 python run.py，Core 自动启动 ✅

- 步骤：双击安装包（或 `/S` 静默）→ 完成 → 启动程序。
- 预期：全程不需要 Python 环境与命令行；Core 由壳自动 spawn 并健康检查。
- 证据：安装到 `%LOCALAPPDATA%\Programs\Personal AI`（无管理员）；
  安装版自检两轮均拉起自带 `resources\core\personal-ai-core.exe` 并通过健康检查。

### 04 开始菜单 / 桌面入口 ✅

- 证据：安装后桌面 `Personal AI.lnk` ✓、开始菜单 `Personal AI.lnk` ✓；
  卸载后两处快捷方式移除 ✓。

### 05 Core 启动失败 / 端口冲突 / 崩溃 → 明确报错不白屏 ✅

- 步骤：外来程序占用 8000 端口后启动壳。
- 预期：错误页显示人话解释 + 重试按钮 + 日志位置。
- 证据：真机场景 C `SELFTEST_FAIL:port-conflict` 明确报错 ✓；
  场景 B 正常链路 ✓、场景 A 复用已运行 Core ✓；错误码人话文案见 `error.html`。

### 06 关闭不留孤儿 Core 进程 ✅

- 步骤：启动（含安装版）→ 关闭窗口 / 自检退出。
- 预期：无 `personal-ai-core` 残留进程、8000 端口释放。
- 证据：三场景 + 安装版两轮自检，每次 `orphan=False / port free=True`；
  `before-quit` + `taskkill /T /F` 收尸逻辑经单测（18 项）。

### 07 数据目录与安装目录分离；卸载不删 Context；重装接回 ✅

- 步骤：启动 → 查 `%APPDATA%\Personal AI\data` → 卸载 → 查数据 → 重装 → 启动。
- 预期：数据不落安装目录；卸载保留；重装自动用回旧数据。
- 证据：安装目录零污染（无 data/、logs/）✓；卸载文件删净（854→0，仅余空目录壳）、
  `personal_ai.db` 完好 ✓；重装 854 文件回来、db 字节数不变 ✓。

### 08 导出 / 备份 / 恢复第一版就做，安装版可用 ✅

- 步骤：UI ⑦ 或 `GET/POST /api/data/*`：定位 → 导出 zip → 本地备份 → 恢复。
- 预期：导出含 db 快照+manifest+settings+缩略图；恢复前自动拍安全网。
- 证据：阶段 2 对开发版全流程实测（D010）；打包 Core 上 `acceptance.py` 再全过，
  数据管理 API 同一套代码（Core 不分开发/打包版）。

### 09 V0.1 / V0.2 语义与测试标准不降 ✅

- 步骤：`python -m pytest tests` + 对**打包 Core** 跑两套 acceptance。
- 证据：**152 测试全绿**（旧 113 + 新 39，含真模型 reale2e）；
  对安装目录里的 PyInstaller 产物跑 `acceptance.py` 8 步、`acceptance_v02.py` 12 步，
  均 exit=0（真模型闭环：感知→记忆→回忆、上下文读写→纠正→删除→暂停）。

### 10 模型 Provider 可切换，Key 脱敏，切换不丢 Context ✅

- 步骤：UI ⑥ 切换 Ollama ↔ OpenAI 兼容云端 → 查看 ④ Context。
- 预期：只换推理引擎，Context/对话/事件不丢；Key 不明文回显。
- 证据：阶段 1 实测（D009）——连接测试通过、切换后 Context 9 条不变、
  `api_key_masked` 形如 `sk-l...3456`；坏配置降级不崩 App。

### 11 说明清晰 + 工作区干净 + 分阶段独立提交 ✅

- 步骤：读 `README.md`；`git status`；`git log`。
- 证据：README 含"普通用户安装即用"完整说明（安装/启动/数据位置/出错处理/卸载）+
  开发者构建命令；`docs/decisions/D009–D012` 决策记录齐；
  提交序列 `3fad2ba`(阶段1) → `ee6d478`(阶段2) → `da7d191`(阶段3) →
  `5ce22ed`(阶段4) → 阶段5 收尾提交，产物 build/dist 已 gitignore。

## 已知小瑕疵（不阻塞）

- 卸载后安装目录剩一个**空目录壳**（0 文件）——NSIS 未连根删除，无害。
- 安装包未签名（无证书）：Windows SmartScreen 首次运行可能提示"未知发布者"，
  选"仍要运行"即可；exe 属性无版本资源（`signAndEditExecutable:false`，见 D012）。

## 回归命令（全绿即验收维持通过）

```powershell
python -m pytest tests                 # 152 预期（Ollama 在线）
cd desktop; npm test                   # 18 预期
$env:PAI_SHELL_SELFTEST='1'; npm start # 安装版：把入口换成已装 exe 同理
python -m uvicorn interfaces.webapp:app --port 8000   # 另开终端
python scripts/acceptance.py           # 8 步
python scripts/acceptance_v02.py       # 12 步
```
