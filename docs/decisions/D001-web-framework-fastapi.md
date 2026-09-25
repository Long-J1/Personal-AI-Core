# D001：Web 框架选 FastAPI（不用 Flask）

- 日期：2026-09-26
- 状态：已接受
- 背景：V0.1 需要一个最简 Web 界面 + JSON API。环境里 Flask 和 FastAPI 都已安装。
- 决定：用 **FastAPI + uvicorn**。
- 理由：
  1. Event/请求体用 pydantic 校验，和数据模型层天然一体（任务书要求结构化事件）；
  2. 自带 `/docs` 接口文档，后续接手机/眼镜端点时省事；
  3. 异步，未来接实时感知流不需换框架。
- 影响：`interfaces/webapp.py` 基于 FastAPI；启动入口 `run.py` 用 uvicorn。
