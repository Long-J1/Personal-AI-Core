# 🧠 Personal AI Core V0.2

> “做一个人的 AI，眼镜只是它长出来的第一只眼睛。”
> 依据《Personal AI：真正属于你的 AI｜Agent 执行总任务书 V1.0》与 `docs/PRODUCT_TRUTH.md` 交付。

## 它是什么

一个**跑在你自己 Windows 电脑上的个人 AI 核心**，V0.2 起它开始**拥有你的上下文**：

- 看图 → 理解成事件 → 存进私有记忆库 → 问“刚才我在干什么”带**依据**回答（V0.1）；
- **对话 → 读取“它对你的理解” → 回答 → 把新理解沉淀回去**（V0.2 读写闭环）；
- 你说“不对，我是高一不是高二” → 理解条目即刻纠正 → **同一会话后续回答马上用新值**；
- 你说“以后先给提示别直接给答案” → 沉淀为“相处方式” → **下一次判断真的改变**（可测试）；
- 所有内容只存本机 `data/personal_ai.db`，可查看、纠正、删除、暂停、导出。

```
感知(图/对话) → 理解 → 事件 + Personal Context → 检索/组装 → 回答 → 沉淀 → 修正理解
```

## 🚀 普通用户：安装即用（推荐）

不用 Python、不用命令行、不用 `python run.py`：

1. **双击安装包** `desktop\dist\PersonalAI-Setup-0.2.0.exe`
   → 下一步 → 完成（装到 `%LOCALAPPDATA%\Programs\Personal AI`，无需管理员权限）；
2. **双击桌面或开始菜单的 "Personal AI"**——窗口自动拉起内置 Core 并打开界面；
3. 开始使用。页面七块：

| 区域 | 能做什么 |
|---|---|
| ① 观察 | 上传图片 / 浏览器摄像头拍照 → AI 理解成事件并存入记忆 |
| ② 对话 | 和它聊天：带着“它对你的理解”回答，每轮把新理解沉淀回去 |
| ③ 回忆 | 输入“刚才我在干什么”→ 从记忆检索回答，附**依据** |
| ④ 它对你的理解 | 查看 / 纠正 / 删除 / 变更历史 / 导出——对它的理解你有解释权 |
| ⑤ 记忆库 | 查看全部记忆、删除、导出、**暂停记录**（暂停后不观察、不沉淀） |
| ⑥ 模型设置 | Ollama ↔ 任意 OpenAI 兼容云端热切换（Key 不明文回显）、连接测试 |
| ⑦ 数据与备份 | 数据目录 / 一键导出 / 本地备份 / 恢复（恢复前自动拍安全网） |

**你需要知道的几件事**：

- **前置**：本机装有 [Ollama](http://ollama.com) 且有可用视觉模型（壳会智能尝试帮你拉起）；
  也可以在 ⑥ 切换到任意 OpenAI 兼容的云端模型，完全不依赖 Ollama。
- **数据在哪**：`%APPDATA%\Personal AI\data`（记忆库）与 `...\logs`（日志）——
  和安装目录彻底分离：**卸载不删记忆，重装自动接回**。
- **出错不白屏**：Core 起不来 / 端口被占 / 中途崩溃 → 错误页用人话说明原因 +
  重试按钮 + 日志位置；关闭窗口必杀干净 Core，不留后台进程。
- **卸载**：设置 → 应用 → Personal AI（或安装目录里的 `Uninstall Personal AI.exe`），
  记忆数据保留在 `%APPDATA%\Personal AI`。

## 给开发者：源码运行（2 条命令）

```powershell
# 1. （首次）安装依赖 —— 本机已全部装好，换机器才需要
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 2. 启动（会自动打开浏览器）
python run.py
```

打开后访问 **http://127.0.0.1:8000**，页面七块（见上表 ①–⑦）。

### 构建桌面安装包（可选）

```powershell
# 1. 打包 Core（PyInstaller → desktop\build\core\personal-ai-core.exe）
powershell -ExecutionPolicy Bypass -File scripts\build_core.ps1

# 2. 打 NSIS 安装包（→ desktop\dist\PersonalAI-Setup-*.exe）
cd desktop; npm run dist
```

> 网络提示（国内）：构建需带 `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`、
> `ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/`、
> `ELECTRON_BUILDER_CACHE=C:\ebcache`；细节与踩坑见 `docs/decisions/D012`。

## 运行测试

```powershell
python -m pytest tests -m "not reale2e"   # 离线测试（秒级，Mock 模型）
python -m pytest tests                    # 全量（含真模型端到端，Ollama 在线时约 90 秒）
python scripts/acceptance.py              # V0.1 闭环验收（对运行中的服务）
python scripts/acceptance_v02.py          # V0.2 上下文闭环验收（对话/纠正/反馈场景/删除/暂停）
cd desktop; npm test                     # 桌面壳测试（Core 生命周期/端口冲突/收尸，18 项）
```

## 验收结果

### V0.1（2026-09-26 实测）

| 验收项（任务书 §11） | 结果 |
|---|---|
| 可启动 | ✅ `python run.py` 一条命令，自动开浏览器 |
| 能感知 | ✅ 真模型看图 → 结构化事件 |
| 能记忆 | ✅ 事件入 SQLite，按时间/关键词多级检索 |
| 能回忆 | ✅ “刚才我在干什么”→ 带依据的中文回答 |
| 有日志 | ✅ 控制台 + `logs/pai.log` 滚动记录 + `logs/silence_audit.jsonl` 沉默记账 |
| 可删除 | ✅ 单条删除 / 一键清空 / 导出 JSON |
| 可扩展 | ✅ 模型、感知、介入策略、工具均为接口隔离 |
| 可解释 | ✅ 回答附带引用事件的时间与描述 |

### V0.2（验收标准见 `docs/PLAN_v0.2.md`）

| 验收项 | 结果 |
|---|---|
| A1 上下文可看懂 | ✅ `GET /api/context` + UI ④，每条带来源与时间 |
| A2 读写闭环 | ✅ 自动化测试断言每轮先读后写 |
| A3 纠正生效 | ✅ 纠正→历史可查→同一会话即用新值（含真模型 reale2e） |
| A4 删除即失效 | ✅ 删除后后续提示词不再包含（自动化断言） |
| A5 暂停有效 | ✅ 暂停可回答但不沉淀，恢复可逆（自动化断言） |
| 附加：Context 改变判断 | ✅ 反馈场景三步闭环：默认判断→纠正→判断可观察地变化（mock 因果测试 + 真模型） |
| B1 接回旧事 | ✅ 换说法提问仍带出上次进展/卡点（上下文条目通道 + 话题检索） |
| B2 跨轮连贯 | ✅ 三轮前的约束在后续回答自然适用（自动化断言） |
| B3 无问先备 | ✅ 交互开始即组装完毕，日志可证 |
| C1 沉默可审计 | ✅ `logs/silence_audit.jsonl` 结构化记账，断言不进记忆库 |
| C2 后台不打扰 | ✅ 回答原文无“我学到了XX”式汇报，沉淀只在元数据/信任界面 |
| D1 V0.1 回归 | ✅ 原 72 用例全部保持通过 |

### 桌面版（2026-09-26 实测，清单见 `docs/ACCEPTANCE_desktop.md`）

| 验收项 | 结果 |
|---|---|
| 安装包生成 | ✅ `PersonalAI-Setup-0.2.0.exe`（110.4 MB，NSIS，无需管理员） |
| 安装后运行 | ✅ 静默安装实测；双击图标 → 自动拉起打包 Core → 健康检查 → 加载界面 |
| 失败不白屏 | ✅ 端口冲突 → `SELFTEST_FAIL:port-conflict` 明确报错；错误页带人话解释+重试+日志位置 |
| 退出无孤儿 | ✅ 关闭后无 `personal-ai-core` 残留进程、端口 8000 释放 |
| 数据分离 | ✅ 数据落 `%APPDATA%\Personal AI\data`，安装目录零污染 |
| 卸载/重装 | ✅ 卸载后数据完好、文件删净；重装自动接回旧数据 |
| 打包 Core 全量验收 | ✅ 对 PyInstaller 产物跑 V0.1 8 步 + V0.2 12 步全过 |
| 回归 | ✅ Python 152 测试 + 桌面壳 18 测试全绿 |

## 目录结构

```
├─ core/            # 配置、日志、理解解析、管线、判断、介入、回忆、对话、沉淀、沉默记账
├─ perception/      # 感知：图片输入、摄像头（未来 ASR 在这里扩展）
├─ memory/          # Event 标准 + SQLite 存储/检索；Personal Context；会话历史
├─ models/          # 模型 Provider 层：Ollama / OpenAI 兼容云端 / Mock（工厂热切换）
├─ agent/           # 工具调用接口（当前只有安全的只读记忆工具）
├─ interfaces/      # Web 界面与 JSON API（UI 复用 interfaces/static/index.html）
├─ desktop/         # Electron 桌面壳：生命周期/错误页/自检 + electron-builder 配置
├─ data/            # 本机数据库 + 缩略图（不进 git，隐私）
├─ logs/            # 运行日志 + 沉默记账（不进 git）
├─ tests/           # 自动化测试（Mock 确定性 + reale2e 真模型）
├─ scripts/         # 验收脚本 acceptance*.py + 打包脚本 build_core.ps1
├─ docs/            # PRODUCT_TRUTH、PLAN、decisions/ 决策记录（D001–D012）
├─ serve.py         # 打包版 Core 入口（只起服务不开浏览器）
└─ run.py           # 开发入口（起服务 + 自动开浏览器）
```

## 关键设计（详见 `docs/decisions/`）

- **上下文必须改变行为**（D007）：`interaction` 类理解派生为对话中的“相处方式指令”，
  `approach` 字段记录本次判断——纠正前后判断的变化被测试锁死，不是嘴上说说。
- **模型即插拔**（D009）：Core 只认 `BaseModelAdapter`，Ollama/OpenAI 兼容云端是平级
  Provider；UI ⑥ 热切换只换推理引擎，Context/对话/事件一个不动，Key 脱敏不回显。
- **数据可控**（D010）：导出/备份/恢复第一版就做，恢复前自动拍安全网，zip 防目录穿越。
- **安装目录与数据分离**（D011/D012）：卸载不删记忆；壳负责 Core 生死（复用/冲突/
  崩溃收尸），出错进错误页不白屏。
- **可替换性**：换模型用 UI ⑥，或改 `core/config.py` 的 `PAI_MODEL`，或实现 `models/base.py` 的接口。
- **失败可恢复**：沉淀失败只记日志不影响回答；模型挂了回忆降级为列出检索结果。
- **隐私**：暂停时不观察、不沉淀（对话仍可问）；原图不保存，只留 320px 缩略图；
  沉默记账只在开发日志，绝不进记忆库。

## 下一步（V0.3 候选）

语音输入（ASR）→ 主动性（Intervention Policy 真实现）→ 模式/偏好记忆归纳 → 多设备接入。
