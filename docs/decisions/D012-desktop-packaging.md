# D012：Windows 打包——PyInstaller Core + electron-builder NSIS 安装包

日期：2026-09-26
状态：已采纳（阶段4 实测完成）

## 背景

任务书硬约束：最终目标是**普通人可安装使用的 Windows 软件**——双击安装包装好就能用，
不需要 Python 环境、不需要手动 `python run.py`；Core 能自动启动；数据在安装目录之外。

## 决策

### 总体：两件产物，一个安装包

1. **Core → `personal-ai-core.exe`**（PyInstaller onedir）
   - 入口 `serve.py`：只起 FastAPI 服务，**不开浏览器**（打开窗口是壳的活）。
   - 选 onedir 不选 onefile：onefile 每次启动要解压到临时目录，慢且杀进程容易留垃圾；
     onedir 是一个 `personal-ai-core.exe + _internal\` 目录，整目录被壳拷贝进安装包，
     启动即跑、杀即净。
   - 依赖裁剪：**排除 `cv2`**（只有服务端抓帧路径懒加载用它，UI 摄像头走浏览器
     getUserMedia，根本用不到——cv2 全家桶能把包撑大几百 MB）、**排除 tkinter**
     （推测依赖，测试从不碰）。裁剪后 Core 产物 86.5 MB。
   - 构建脚本 `scripts/build_core.ps1`：**必须 UTF-8 带 BOM**（PS5.1 读无 BOM 的
     UTF-8 脚本会乱码炸解析，含中文注释必踩）；`--add-data` 用 `$staticDir`
     绝对路径，避免相对路径诡异失败。

2. **壳 → NSIS 安装包**（electron-builder）
   - `oneClick:false, perMachine:false`：装到 `%LOCALAPPDATA%\Programs\Personal AI`，
     普通用户无需管理员；带"下一步"界面 + 卸载器。
   - `deleteAppDataOnUninstall:false`：**卸载不删 `%APPDATA%\Personal AI\data`**
     （任务书：卸载不能丢 Personal Context）。
   - Core exe 经 `extraResources` 打进 `resources\core\`，壳有权限执行
     （asar 内的文件不可直接执行，必须走 unpacked/resources）。

3. **数据与安装目录分离**（壳启动时注入环境变量）
   - `PAI_DATA_DIR=%APPDATA%\Personal AI\data`、`PAI_LOG_DIR=%APPDATA%\Personal AI\logs`。
   - 首次启动由 Core 自动建目录。升级/卸载动安装目录，Context 纹丝不动。

### 打包期踩坑与决策（重要，将来重打包必读）

| 坑 | 决策 |
|---|---|
| **winCodeSign 解压失败**：`Cannot create symbolic link... 客户端没有所需特权`（归档里两个 macOS dylib 是符号链接，普通用户无 SeCreateSymbolicLinkPrivilege） | `build.win.signAndEditExecutable:false`——本机无代码签名证书，签名环节本来就"skipping"，直接整个关掉（winCodeSign 下载+解压、exe 资源编辑一并跳过）。**代价**：exe 属性里没有版本资源、图标用默认 Electron 图标（本来也没做图标）。将来有证书/要做图标时，需先解决该解压（预用 7za 手动解到缓存，或开发机开开发者模式），再把这个开关改回。 |
| **镜像下载瞬时 EOF**（npmmirror 拉 nsis-3.0.4.1.7z 连接被掐） | 重试即过（同 URL 2.19s 下载成功）。瞬时网络抖动，不是配置问题，失败先重跑。 |
| **ELECTRON_BUILDER_CACHE 中文路径风险** | 固定 `C:\ebcache`（项目路径带中文，缓存/工具链尽量避开）。 |
| Electron/nsis 二进制下载慢或被墙 | 三个环境变量：`ELECTRON_MIRROR`、`ELECTRON_BUILDER_BINARIES_MIRROR` 均指 npmmirror，`ELECTRON_BUILDER_CACHE=C:\ebcache`。 |

## 实测结果（2026-09-26）

- 构建：`scripts/build_core.ps1` → `desktop/build/core\personal-ai-core.exe`（86.5 MB）；
  `npm run dist` → `desktop/dist\PersonalAI-Setup-0.2.0.exe`（**110.4 MB**）。
- exe 单独冒烟：1s 健康检查过、`PAI_DATA_DIR/PAI_LOG_DIR` 注入生效、杀掉无孤儿。
- 静默安装 `/S` 成功：装出 `Personal AI.exe` + `resources\core\personal-ai-core.exe` +
  `Uninstall Personal AI.exe`，桌面与开始菜单快捷方式齐。
- **安装版自检（`PAI_SHELL_SELFTEST=1`）跑两轮**，每轮全绿：
  - 拉起自带打包 Core → `/api/status` 健康（provider=ollama、model_service_ok=true）→ 加载 UI → 退出码 0；
  - 无孤儿 Core/壳进程、端口 8000 释放；
  - 数据落 `%APPDATA%\Personal AI\data\personal_ai.db`（第二轮复用同一 db，**重启数据保留**）；
  - **安装目录零污染**（无 data/、logs/ 写入）。
- 快捷方式：桌面 ✓ 开始菜单 ✓；卸载器 ✓（`deleteAppDataOnUninstall:false` 保证卸载留数据）。

## 后续（不在本阶段）

- 代码签名与自定义图标（需证书 + 解决 winCodeSign 符号链接解压）。
- 自动更新（任务书 §15 明确排除）。
