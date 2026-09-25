# 🧠 Personal AI Core V0.1

> “做一个人的 AI，眼镜只是它长出来的第一只眼睛。”
> 依据《Personal AI：真正属于你的 AI｜Agent 执行总任务书 V1.0》交付的可运行最小闭环。

## 它是什么

一个**跑在你自己 Windows 电脑上的个人 AI 核心**：看一张图 → 理解成结构化事件 → 存进你的私有记忆库 → 你问“刚才/今天我在干什么”，它从记忆里检索并**带着依据**回答。所有记忆只存在本机 `data/personal_ai.db`，可随时查看、删除、导出、暂停。

```
感知 → 理解 → 事件 → 判断 → SQLite 记忆库 → 时间/关键词检索 → AI 回忆
```

## 快速启动（2 条命令）

```powershell
# 1. （首次）安装依赖 —— 本机已全部装好，换机器才需要
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 2. 启动（会自动打开浏览器）
python run.py
```

打开后访问 **http://127.0.0.1:8000**，页面三块：

| 区域 | 能做什么 |
|---|---|
| ① 观察 | 上传图片 / 浏览器摄像头拍照 → AI 理解成事件并存入记忆 |
| ② 回忆 | 输入“刚才我在干什么”→ 从记忆检索回答，附**依据**（时间+来源） |
| ③ 记忆库 | 查看全部记忆、删除单条、导出 JSON、清空、**暂停记录** |

> 前置条件：本机 [Ollama](http://ollama.com) 正在运行且有可用视觉模型（当前配置见 `core/config.py`，实测模型可直接看图）。

## 运行测试

```powershell
python -m pytest tests              # 全量：72 个用例（含真模型端到端，约 50 秒）
python -m pytest tests -m "not reale2e"   # 只跑离线测试（秒级）
PAI_REAL=1 python -m pytest tests/test_scenarios.py   # 20 个场景换真模型跑
python scripts/acceptance.py        # 对着运行中的服务做完整验收演示
```

## 验收结果（2026-09-26 实测）

| 验收项（任务书 §11） | 结果 |
|---|---|
| 可启动 | ✅ `python run.py` 一条命令，自动开浏览器 |
| 能感知 | ✅ 真模型看图 → 结构化事件（单次理解约 7 秒） |
| 能记忆 | ✅ 事件入 SQLite，按时间/关键词多级检索 |
| 能回忆 | ✅ “刚才我在干什么”→ 带依据的中文回答 |
| 有日志 | ✅ 控制台 + `logs/pai.log` 滚动记录 |
| 可删除 | ✅ 单条删除 / 一键清空 / 导出 JSON |
| 可扩展 | ✅ 模型、感知、介入策略、工具均为接口隔离 |
| 可解释 | ✅ 回答附带引用事件的时间与描述 |

自动化测试：**72 通过 / 0 失败**（含 20 个“看图→事件→记忆→回忆”场景 + 1 个真模型端到端）。

## 目录结构

```
├─ core/            # 核心：配置、日志、理解解析、管线、判断、介入接口、回忆
├─ perception/      # 感知：图片输入、摄像头（未来 ASR 在这里扩展）
├─ memory/          # 记忆：Event 标准 + SQLite 存储/检索/删除/导出
├─ models/          # 模型适配层：Ollama 实现 + Mock 测试实现（可替换）
├─ agent/           # 工具调用接口（当前只有安全的只读记忆工具）
├─ interfaces/      # Web 界面与 JSON API
├─ data/            # 本机数据库 + 缩略图（不进 git，隐私）
├─ tests/           # 72 个自动化测试 + 20 个场景案例
├─ docs/            # 架构说明、实施计划、decisions/ 决策记录
└─ run.py           # 启动入口
```

## 关键设计（详见 `docs/decisions/`）

- **可替换性**：换模型只改 `core/config.py` 的 `PAI_MODEL`，或实现 `models/base.py` 的接口。
- **失败可恢复**：模型挂了 → 回忆降级为直接列出检索结果；理解输出解析失败 → 按原文保守记忆。
- **隐私**：暂停时不调用模型、不落库；原始图片不保存（只留 320px 缩略图）。
- **留架构位**：主动介入 `core/intervention.py`、工具 `agent/`、ASR `perception/` 均为接口占位，不推翻数据结构即可扩展。

## 下一步（V0.2 候选）

语音输入（ASR）→ 主动性（Intervention Policy 真实现）→ 事实/模式记忆归纳 → 多设备接入。
