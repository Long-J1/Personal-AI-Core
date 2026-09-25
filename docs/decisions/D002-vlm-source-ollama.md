# D002：视觉/语言模型用本机 Ollama 现有模型，零下载

- 日期：2026-09-26
- 状态：已接受
- 背景：任务书要求"调用一个可用的 VLM/LLM 对图像做结构化理解"，优先本地模型。
- 环境事实：本机 Ollama 只有一个模型 `hf.co/HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive:Q4_K_M`（6.3GB）。**实测 `/api/chat` 带 `images` 字段可正常看图回答**（红方块测试通过）。
- 决定：V0.1 的理解与回忆都走该模型，不再下载新模型。
- 理由：
  1. 实测可用，省 2GB+ 下载和磁盘；
  2. 通过 `models/base.py` 抽象接口隔离，模型随时可换（任务书"保留可替换性"）；
  3. RTX 3050 4GB + 6.3GB 模型 = 部分卸载到内存，冷启动约 18s、生成约 2s，可接受。
- 后续：如果对图像结构化输出质量不满意，备选 `qwen2.5vl:3b`（约 2GB）一行配置切换。
- 影响：`models/ollama_adapter.py`；模型名在 `core/config.py` 可配。
