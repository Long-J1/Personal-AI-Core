# D028：本地 llama.cpp 接入（GGUF + 一键启动）

日期：2026-10-05
基线：`b3fc042`（Deep Understanding: Cross-Context Synthesis）
状态：已实现，测试与变异测试全绿

## 背景

用户要求"接本地 Llama"，并且**要一个一键启动按钮**，做完直接更新桌面上的软件。

开工前先查了机器，发现三件事决定了整件事的性质：

| 事实 | 值 |
|---|---|
| `llama-server.exe` | **已编好**（CUDA 版），在 `D:\AI\gpt-oss-lab\llama-cuda-b11382\` |
| `GPT-OSS-20B-...MXFP4-Balanced.gguf` | **11.3 GB**，已下载 |
| 显卡 | **RTX 3050 Laptop，4 GB 显存** |
| 内存 | 15.8 GB |

所以这不是"从零支持一个新推理引擎"，而是"把机器上已经有的东西接上"。

## 三个实测踩出来的坑

### 一、`-ngl 99` 会让 llama-server 启动就死

第一次按常规写法给了 `-ngl 99`（"全部层上显卡"）：

```
W common_fit_params: failed to fit params to free device memory: n_gpu_layers already set by user to 99, abort
cudaMalloc failed: out of memory
failed to create context with model ...
llama_server: exiting due to model loading error
```

4 GB 显存装 11.3 GB 模型，**塞不下**。

**修法：不给 `-ngl`**，让 llama.cpp 自己按显存和内存分配（能上多少层上多少层，剩下的走 CPU）。实测 23 tok/s。

代码里因此把 `ngl` 默认成 `None` → 不传这个参数。这条钉在 `test_argv_omits_ngl_when_not_set`。

**顺带一个假值陷阱**：`ngl=0` 是有意义的（强制全 CPU），所以判定必须用 `is not None` 而不是真值判断 —— `if self.ngl:` 会把 0 吞掉。钉在 `test_argv_zero_is_kept_not_treated_as_missing`。

### 二、thinking 模型会把 `max_tokens` 吃光 → 回答为空

gpt-oss 走 harmony 格式，先输出 `reasoning_content`（思考），再输出 `content`（回答）。`max_tokens` **两者一起算**：

```
max_tokens=80  → content = ""          finish_reason = "length"
max_tokens=600 → content = "傅里叶变换把时域…"  思考 399 字 + 回答 36 字
```

这和仓库里 Gemma 的老坑**完全同构**（thinking 吃光预算导致回答为空）。

**修法两条**：
1. 默认给足预算（1024）
2. 回答为空时**自动加倍重试一次**

第 2 条不能省 —— 用户自己把参数调小就会复发。

**思考过程绝不进产品**：`处理摘要` / 沉淀里出现"我先想了想…"是噪声。流式和非流式都只取 `content`。

### 三、`/health` 在模型没加载完时也返回 ok

llama.cpp 的 `/health` 只要 HTTP 服务活着就返回 ok，即使权重还在加载。所以：

- 探活用 `/props`（只在 context 建好之后才有内容）
- 连接测试**真的发一句**，别只看 `/health`

否则用户看到的会是"连接成功"然后聊天一直转圈。

## 启动器的三条铁律

1. **绝不杀别人的进程。** `self.proc is None` 时所有操作都是"只探不动"。用户在别处手动起的 llama-server 不会被我们误杀。
2. **启动前先探活。** 已经有健康服务就直接复用，不重复加载 11 GB 模型。
3. **子进程不继承窗口。** `CREATE_NO_WINDOW` —— 否则每次点启动都弹一个黑框。

第 1 条在两层都有守卫（adapter 的 `stop()` / webapp 的 `if not srv.ours: continue`），按本项目的老教训**每层都单独测**，不然删掉一层另一层会兜住、变异存活。

## 参数选择（有实测依据，不是抄默认）

| 参数 | 值 | 依据 |
|---|---|---|
| `-c` | 4096 | Personal AI 的 system 提示词实测只有 270–413 token（空记忆 / 塞了记忆两种情况） |
| `--parallel` | 1 | llama.cpp 默认 `n_slots = 4`，单人应用开 4 槽白占内存 |
| `-ngl` | **不传** | 见坑一 |
| `timeout` | ≥ 180 | 本地模型冷启动慢。**不能**用 `understand_timeout` |

`--no-warmup` 加上，少跑一次预热。

## 扫盘的两个过滤

扫 `*.gguf` 时实测会扫出两类**不是模型**的文件，混进下拉框只会让人选错然后启动失败：

- `ggml-vocab-*.gguf` —— 词表（实测 10+ 个）
- `mmproj-*.gguf` —— 多模态投影器（Qwen3.6 那个 0.84 GB）

另外 `<64 MB` 的也排掉（不可能是真模型）。扫不到时返回空让用户手填，**绝不猜一个路径上去**。

## 加第三家 provider 踩到的分支坑

`_validate` 原本是 `if openai / else`（else 意味着 Ollama）。加了 llamacpp 之后它会掉进 Ollama 分支，报"请先配置 Ollama"。必须拆成 `if / elif / else`。

同理 `load()` / `resolve()` 里的 `for sect in ("ollama", "openai")` 只收字符串，而 llamacpp 有整数字段（port/ctx/parallel），需要单独按类型取。

`bool` 是 `int` 的子类 —— `port: true` 会被当成端口 1。

## 测试

- `tests/test_llamacpp.py` —— 适配器与启动器行为（假 HTTP，零真实进程）+ 两条真进程测试（验证 `stop()` 真能杀）
- `tests/test_llamacpp_api.py` —— HTTP 端点（TestClient + 假服务）
- `scripts/mutation_llamacpp.py` —— 19 条变异，全部锚在**真踩过的坑**上

### 变异测试抓到的真实测试漏洞

第一轮 19 条变异里 **3 条存活**，其中一条是真漏洞：

**G：把 `if not self.alive(): return` 换成裸 `return`** —— 后 12 行全变成死代码，`stop()` 变成**永远不杀进程**。而当时我只有"不误杀外部进程"的测试（还是用假类测的），压根没测过 `stop()` 能不能真杀。

补了 `test_stop_really_kills_the_process_we_started`（真起一个 `sleep(60)` 的进程、真杀、用 tasklist 验进程真没了）。

**这是本项目第四次踩"锚点匹配 ≠ 行为改变"**（V0.5.6 的 P、这一次的 G/R）。写变异时要问的不是"这段代码变了吗"，而是"**行为**变了吗"。

另一条存活（Q）是我的断言太松：`understand_timeout` 默认就是 180，所以只断 `>= 120` 的话，变异成原样透传 `t` 一样能过。改成**显式传 `timeout=5.0`** 再断底线。

## 顺带修掉的一个既有 bug：`[hidden]` 完全失效

做这个功能时发现模型面板里 **三家的输入框一直同时显示**——切 Provider 形同虚设。

根因不是 JS 写错了，是 CSS：作者样式 `.row { display: flex }` 的优先级**高于 UA 样式表**里的 `[hidden] { display: none }`。只要元素带 `display` 类，`hidden` 属性就完全不生效。

实测中招的 5 处（**全是既有代码，不是本次引入**）：

| 元素 | 后果 |
|---|---|
| `mdOllamaBox` | Ollama 的地址/模型名跟别的家一起显示 |
| `mdOpenaiBox` | 云端的 Base URL / Key 一起显示 |
| `observeResult` | 观察结果容器空占位 |
| `betaState` / `betaState2` | Beta 状态徽章空占位 |

修法是一条全局规则：`[hidden] { display: none !important; }`。

**教训**：JS 里 `el.hidden = true` 是**看起来**在做事。用 `getComputedStyle(el).display` 验一遍才算数。这个坑在没有布局的单元测试里永远抓不到——**DOM 层的事必须去 DOM 层验**。

## 真机抓到的第二个 bug：连点两次启动会丢掉进程身份

第一次真机点「启动」成功了（17 秒，PID 20668，`ours=true`）。接着又点一次「停止」，再点「启动」——服务确实还活着，但状态变成：

```
pid: 20668 → null
ours: true → false
```

原因：`/api/llamacpp/start` 每次都用**新的** `LlamaCppServer` 覆盖 `app.state` 里的旧对象。新对象没有 `proc` 句柄，于是 `ours` 永远是 False。

**这比看起来严重**：`ours=False` 意味着"我们分不清这个服务是谁起的"——而这条正是「绝不关掉用户的进程」那条铁律的唯一依据。一旦失效，App 就会去关用户自己开的 llama-server。

修法：同一个地址上已有我们启动的、还活着的服务，就**沿用同一个对象**，不换新的。

顺带把 `ours` 从 `status()` 里临时算改成 **只读属性**，语义只有一个出处，而且 `stop()` 清掉句柄后它自然变 False，不会残留假的"我们启动的"。

## 真机上抓到的三个 bug（都是"看起来在工作其实没工作"）

### 一、连接测试一律 8 秒 → 本地模型永远超时

`/api/model/test` 里写死 `tester(timeout=8.0)`。而本地 20B 模型光
"喂 prompt + 出第一个字"就要 8~16 秒（实测），于是用户看到
`❌ 调用失败：timed out`——**而模型其实好好的**。

修法：`test_timeout = 90.0 if provider == "llamacpp" else 8.0`。
云端保持短超时，因为那里慢就是真有问题。

**注意别把服务状态探测（`/api/status` 里的 ping）也改长**——那个是
健康探针，慢调用会拖垮首页（这正是 0.6.0-beta.1 修过的另一个坑）。

### 二、保存设置后模型路径丢失 → 点启动报"还没选模型"

下拉框里的选项是"找模型"时**动态加的**，刷新页面就没了；而
`model_path` 输入框是"从配置恢复"的唯一来源。保存时只写了配置、
**没把下拉的选择同步回输入框**，于是：用户选好模型 → 保存 → 点启动 →
「还没选模型文件」。

修法：`mdCollectLlama()` 里 `if (sel) $('mdLcModelPath').value = sel;`

### 三、公开版里 Beta 入口点了也没用

`beta_state.json` 是 10-02 用 Beta 码激活时留下的。装公开版后
`available=false` 但 `active=true`，而 UI 条件是
`!(available || active)`，所以入口**一直显示**，点进去输任何码都只
得到「体验码无效」——看起来像用户自己操作坏了。

修法（三件事缺一不可）：
1. `is_active()` 在 `enabled()` 为 False 时恒返回 False；
2. `apply_on_startup()` **还原解锁前的配置**——否则用户永远停在
   Beta 那套 provider/base_url/model/api_key 上，公开版里 preset
   没了，他既看不到也改不了（这条是补测试时才发现的第二个 bug）；
3. 清掉激活标记与备份文件——否则将来切回 Beta 会突然"还原"出他
   早就改掉的配置。

---

## 已知边界

- **图片理解仍走 Ollama 那条路**。llama.cpp 的 OpenAI 兼容层对多模态格式挑剔，所以 `_strip_images` 直接把图片字段剥掉——宁可少传也不要让整个请求 400。
- **只接一个 gguf**。多模型切换要等 llama.cpp 自己支持（它一次只服务一个模型）。
- **不做进程守护**。App 退出就收掉 llama-server；崩溃了不会自动重启。
- **`--jinja` 这类模板参数交给用户在「高级参数」里自己填**，不硬编码——不同模型需要的模板不一样。