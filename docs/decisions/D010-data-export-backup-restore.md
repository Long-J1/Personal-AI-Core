# D010 · 数据导出 / 备份 / 恢复 / 导入（桌面化阶段2）

- 日期：2026-09-26
- 状态：已采纳
- 关联：`docs/PRODUCT_TRUTH.md`（"换电脑不能丢它"）、D009（数据目录与安装目录分离）

## 背景

桌面化要求：安装目录（Program Files）与用户数据目录（%APPDATA%）分离；卸载不误删
Personal Context；换电脑 / 换模型 / 换外壳时"我的 AI 还是那个 AI"。
第一版就要有可用的导出、备份、恢复、导入与数据位置可见性。

## 决策

### 1. `core/data_manager.py`：数据目录的导入导出中枢

| 能力 | 实现 |
|---|---|
| 导出 | zip = `personal_ai.db`（SQLite **backup API 快照**，WAL 安全、导出期间服务照常读写）+ `thumbs/` + `model_settings.json` + `manifest.json`（版本/时间/计数） |
| 备份 | 落在 `数据目录/backups/backup_时间戳.zip`；同秒连拍自动加序号，**绝不覆盖已有备份** |
| 恢复 | 校验 → **先给当前数据拍安全网备份** → 换 DB（连 `-wal/-shm` 一起清，防旧日志污染新库）→ 换缩略图 → 换模型设置（包里有才换） |
| 导入 | = 对上传 zip 走同一条恢复路径，校验一视同仁 |

### 2. 安全防线（全部有测试）

- **恢复永远可回滚**：恢复/导入前自动创建 `auto_safety_*` 备份；安全网拍不出来则中止恢复
- **zip slip 防护**：包内任何 `../` 或绝对路径 → 拒绝，文件绝不出数据目录
- **合法性校验**：必须含 `personal_ai.db` 且文件头是合法 SQLite——宁可拒绝也不把好数据换成坏数据
- **备份名白名单**：恢复只接受 `backups/` 下的纯文件名（`Path(name).name == name`），路径穿越直接 400
- **导出不含 backups/**：避免递归套娃、包越来越大
- **Windows 句柄竞争**：`os.replace` 失败退避重试 5 次（MemoryStore 系列全是"每次操作新建连接"，无常驻句柄，见 `closing(self._connect())`）

### 3. API（Core 提供，UI 只是调用者）

| 端点 | 作用 |
|---|---|
| `GET /api/data/location` | 数据位置 / 大小 / DB 是否存在 |
| `GET /api/data/export` | 下载完整数据包（响应后自动清理临时文件） |
| `GET /api/data/backups` | 备份列表（名/大小/时间） |
| `POST /api/data/backup` | 创建备份 |
| `POST /api/data/restore` `{name}` | 从本地备份恢复（含门面 reload，模型设置随包回归） |
| `POST /api/data/import` multipart | 导入上传的数据包（同一套校验+安全网） |

### 4. UI：⑦ 数据与备份

导出 / 创建备份 / 导入（文件选择，双重确认）/ 备份列表逐条恢复 / 数据位置展示。
恢复与导入都有确认弹窗，并明示"当前数据会先自动存安全网备份"。

### 5. 安全说明（第一版取舍）

- 导出包**包含** `model_settings.json`（即 API Key）：换电脑要能直接复活，这是"我的 AI 还是
  那个 AI"的必要代价。UI 明示用户自行保管导出包。
- 上传/下载都在本机回环进行，无网络暴露面。

## 验证

- `python -m pytest tests`：**152 passed**（141 + 阶段2新增 11，全含真模型 reale2e）
- 新增 `tests/test_data_manager.py`：位置 / 导出包 DB 可读行数对账 / 备份列表 /
  恢复找回被删的 Context+事件+安全网存在 / 坏名拒绝 / 模型设置随包恢复+门面 reload /
  导入往返 / 垃圾字节拒绝 / 非本包拒绝 / zip slip 拒绝且文件未落地 / 坏 DB 拒绝且原数据无损 /
  导出不含 backups/
- 真机：`GET/POST` 各端点实测 + V0.1/V0.2 acceptance 复跑
