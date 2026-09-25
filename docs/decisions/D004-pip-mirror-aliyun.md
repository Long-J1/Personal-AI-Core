# D004：pip 镜像从清华切到阿里云

- 日期：2026-09-26
- 状态：已接受
- 背景：`pip install pytest` 走 `pypi.tuna.tsinghua.edu.cn` 报 `Could not find a version ... (from versions: none)`；直接 GET 镜像返回 **HTTP 403**（镜像方对该 IP 的反爬拦截），与代理无关（NO_PROXY 已含 tuna，走的是直连）。
- 决定：临时用 `-i https://mirrors.aliyun.com/pypi/simple/` 安装成功（pytest 9.1.1）。
- 后续：若再装包失败，依次尝试 tencent 镜像 → 代理直连官方 PyPI（本机代理出口正常）。
- 影响：不改全局 pip 配置，避免影响用户其它项目；需要时在命令后加 `-i` 参数。
