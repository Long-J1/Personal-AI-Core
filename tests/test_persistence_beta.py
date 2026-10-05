"""数据持久化 / 升级守卫 / Beta 回退 的测试。

这一组的测试对象不是"某个函数返回什么"，而是**一串承诺**：

  · 升级不会新建空数据库
  · 升级前一定先备份
  · 备份不产生垃圾堆积
  · 备份失败也不拦住启动
  · Beta → 普通版 / 普通版 → Beta，数据都在

所以绝大多数测试都是**真建库、真写数据、真换版本、真重开**。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.upgrade_guard import (
    KEEP_UPGRADE_BACKUPS,
    _prune,
    _read_stamp,
    _write_stamp,
    list_upgrade_backups,
    on_startup,
)


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def livedata(tmp_path):
    """一份"用了一段时间"的真实数据：聊天 + Context + 理解。"""
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    from memory.store import MemoryStore

    db = tmp_path / "personal_ai.db"
    store = MemoryStore(db)
    chat = ChatStore(db)
    ctx = ContextStore(db)
    hyp = HypothesisStore(db)
    # 造一批"历史"
    for i in range(12):
        chat.append(f"s{i}", "user", f"消息 {i}")
        chat.append(f"s{i}", "assistant", f"回答 {i}")
    for i in range(5):
        ctx.add_entry(kind="dynamic", content=f"记忆 {i}", source="chat")
    h, _ = hyp.create(statement="用户通常在晚上学习。", scope="activity:学习")
    return tmp_path, db, store, chat, ctx, hyp, h


def _counts(db: Path) -> dict:
    con = sqlite3.connect(db)
    try:
        out = {}
        for t in ("chat_messages", "context_entries", "hypotheses"):
            try:
                out[t] = con.execute(f"select count(*) from {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = None
        return out
    finally:
        con.close()


# ---------------------------------------------------------------- 升级前备份

def test_first_run_does_not_backup(livedata):
    """首次运行不备份：这时多半是空库，备份一份空气还占掉滚动配额。"""
    tmp, db, *_ = livedata
    r = on_startup(data_dir=tmp, db_path=db, version="1.0.0")
    assert r["backed_up"] is False
    assert "首次" in r["reason"]
    assert list_upgrade_backups(tmp) == []


def test_same_version_does_not_backup(livedata):
    """同版本反复启动一次都不备份——否则成了垃圾制造机。"""
    tmp, db, *_ = livedata
    on_startup(data_dir=tmp, db_path=db, version="1.0.0")
    for _ in range(5):
        r = on_startup(data_dir=tmp, db_path=db, version="1.0.0")
        assert r["backed_up"] is False
        assert r["reason"] == "版本未变"
    assert list_upgrade_backups(tmp) == []


def test_version_change_backs_up(livedata):
    """版本变了才备份。"""
    tmp, db, *_ = livedata
    on_startup(data_dir=tmp, db_path=db, version="1.0.0")
    r = on_startup(data_dir=tmp, db_path=db, version="1.1.0")
    assert r["backed_up"] is True
    files = list_upgrade_backups(tmp)
    assert len(files) == 1
    assert "1.0.0" in files[0]["name"]      # 名字里记着"来自哪个版本"


def test_backup_contains_real_data(livedata):
    """备份必须真的有数据，而且条数与主库一致。"""
    tmp, db, *_ = livedata
    on_startup(data_dir=tmp, db_path=db, version="1.0.0")
    r = on_startup(data_dir=tmp, db_path=db, version="2.0.0")
    bak = tmp / "backups" / r["filename"]
    assert bak.exists()
    assert _counts(bak) == _counts(db)
    c = _counts(bak)
    assert c["chat_messages"] == 24      # 真的拷到了历史，不是空壳
    assert c["context_entries"] == 5


def test_prune_keeps_only_recent(livedata):
    """滚动保留：不能每次更新都留一份，否则磁盘堆满。"""
    tmp, db, *_ = livedata
    on_startup(data_dir=tmp, db_path=db, version="1.0.0")
    for i in range(1, KEEP_UPGRADE_BACKUPS + 4):
        on_startup(data_dir=tmp, db_path=db, version=f"1.{i}")
        time.sleep(0.02)          # 文件名靠时间戳，别撞名
    files = list_upgrade_backups(tmp)
    assert len(files) == KEEP_UPGRADE_BACKUPS


def test_backup_failure_does_not_raise(livedata):
    """备份失败绝不拦住启动。"""
    tmp, db, *_ = livedata
    on_startup(data_dir=tmp, db_path=db, version="1.0.0")
    # 把 db 换成一个不存在的路径 → 必然失败
    r = on_startup(data_dir=tmp, db_path=tmp / "nope.db", version="2.0.0")
    assert isinstance(r, dict)
    assert r["backed_up"] is False
    # 崩溃不了就是关键；reason 可能有也可能没有
    assert "reason" in r


def test_missing_db_is_not_an_error(livedata):
    """库还不存在（比如刚装上还没用）→ 跳过备份而不是报错。"""
    tmp, db, *_ = livedata
    on_startup(data_dir=tmp, db_path=db, version="1.0.0")
    r = on_startup(data_dir=tmp, db_path=tmp / "gone.db", version="2.0.0")
    assert r["backed_up"] is False
    assert "还不存在" in r["reason"]


def test_stamp_survives_corrupt_file(livedata):
    """版本戳文件坏掉时当"首次运行"处理，绝不崩。"""
    tmp, db, *_ = livedata
    (tmp / "last_version.json").write_text("{ 这不是 json", encoding="utf-8")
    r = on_startup(data_dir=tmp, db_path=db, version="1.0.0")
    assert r["backed_up"] is False


# ---------------------------------------------------------------- 数据持久化

def test_reopening_after_upgrade_keeps_all_data(livedata):
    """**核心承诺**：升级不新建空库。"""
    tmp, db, store, chat, ctx, hyp, h = livedata
    before = _counts(db)

    on_startup(data_dir=tmp, db_path=db, version="1.0.0")

    # 模拟"新版起来"：同一个 db_path，全新 store 实例
    from memory.chat_store import ChatStore
    from memory.context_store import ContextStore
    from memory.hypothesis_store import HypothesisStore
    chat2 = ChatStore(db)
    ctx2 = ContextStore(db)
    hyp2 = HypothesisStore(db)
    on_startup(data_dir=tmp, db_path=db, version="2.0.0")

    assert _counts(db) == before
    assert chat2.count_messages() == 24
    assert len(ctx2.list_entries()) == 5
    assert hyp2.get(h.id) is not None
    assert hyp2.get(h.id).statement == "用户通常在晚上学习。"


def test_data_dir_follows_pai_data_dir(monkeypatch, tmp_path):
    """**数据目录由环境变量切换，这正是持久化的机制本身。**

    源码直接跑时落在项目 `data/`（开发方便）；打包后 Electron 壳注入
    `PAI_DATA_DIR=%APPDATA%\\Personal AI\\data`，于是：
      · 程序在 `%LOCALAPPDATA%\\Programs`（可随意替换）
      · 数据在 `%APPDATA%`（升级不碰）

    ⚠️ 这里**不**用 `importlib.reload(config)`：那会全局改掉
    `core.config.DATA_DIR`，同一次 pytest 运行里后面的测试全部读到
    被污染的值（表现为"单跑全绿、全量挂 4 个"）。改成在子进程里验，
    污染不了当前进程。
    """
    import subprocess
    monkeypatch.setenv("PAI_DATA_DIR", str(tmp_path / "userdata"))
    code = (
        "import os,sys;sys.path.insert(0,%r)\n"
        "from core import config\n"
        "print(config.DATA_DIR)\n"
        "print(config.DB_PATH)\n"
    ) % os.getcwd()
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
    assert len(lines) == 2, f"子进程没输出对：{p.stdout} {p.stderr}"
    assert lines[0] == str((tmp_path / "userdata").resolve())
    assert "Programs" not in lines[0]
    assert Path(lines[1]).parent == Path(lines[0])


def test_default_data_dir_is_not_under_install_dir():
    """源码模式的默认 DATA_DIR 绝不能落在 Programs 下。"""
    from core import config
    data = Path(config.DATA_DIR).resolve()
    assert "Programs" not in str(data)


def test_public_build_clears_stale_beta_state(tmp_path, monkeypatch, beta_dir):
    """**回退到公开版时必须清掉残留的 Beta 标记。**

    真机上撞到的：装过 Beta 版 → 换成公开版，`beta_state.json` 里的
    active 标记还在，于是 `is_active()` 为 True，UI 显示一个
    "点了也没用"的 Beta 入口——用户会以为是自己操作坏了。

    这条测试钉住：公开版里 active 恒为 False，且启动时会自行清理。
    """
    import sys
    import types
    from core import beta
    from memory.hypothesis_store import HypothesisStore
    from models.settings_store import ModelSettingsStore

    # 先造出"Beta 版已激活"的现场
    mod = types.ModuleType("models._beta_preset")
    mod.PRESET = {"provider": "openai", "base_url": "http://b/v1",
                  "model": "b", "api_key": "sk-b", "code_sha256": []}
    mod.BUILD_CHANNEL = "beta"
    monkeypatch.setitem(sys.modules, "models._beta_preset", mod)
    # beta 模块用模块级 DATA_DIR 定位状态文件，测试必须指向 tmp，
    # 否则会写进**真实用户数据目录**。
    monkeypatch.setattr(beta, "DATA_DIR", tmp_path)
    db = tmp_path / "t.db"
    hyp = HypothesisStore(db)
    h, _ = hyp.create(statement="用户理解", scope="activity:x")
    st = ModelSettingsStore(tmp_path / "ms.json")
    st.update({"provider": "openai",
               "openai": {"base_url": "http://mine/v1", "model": "mine",
                          "api_key": "sk-mine"}})
    # 走**真实**的 unlock 造出"已激活"现场（而不是手工写状态文件）——
    # 这样测的是真路径，也顺带验了 backup 文件确实被写出来了。
    import hashlib
    code = "REAL-CODE"
    mod.PRESET["code_sha256"] = [hashlib.sha256(code.encode()).hexdigest()]
    ok, _ = beta.unlock(st, code)
    assert ok is True
    assert beta.is_active() is True
    assert (tmp_path / "saved_config.json").exists(), "解锁前配置应被备份"

    # 换成公开版（空 preset）
    pub = types.ModuleType("models._beta_preset")
    pub.PRESET = {}
    pub.BUILD_CHANNEL = "public"
    monkeypatch.setitem(sys.modules, "models._beta_preset", pub)
    assert beta.enabled() is False
    assert beta.is_active() is False, "公开版里不该还认 Beta 激活"

    beta.apply_on_startup(st)
    # 残留标记与备份都被清掉
    assert not (tmp_path / "beta_state.json").exists()
    assert not (tmp_path / "saved_config.json").exists()
    # **配置与数据都没被动**（仍是用户自己那套）
    assert st.load()["openai"]["model"] == "mine"
    assert hyp.get(h.id) is not None

    # 换成公开版（空 preset）
    pub = types.ModuleType("models._beta_preset")
    pub.PRESET = {}
    pub.BUILD_CHANNEL = "public"
    monkeypatch.setitem(sys.modules, "models._beta_preset", pub)
    assert beta.enabled() is False
    assert beta.is_active() is False, "公开版里不该还认 Beta 激活"

    beta.apply_on_startup(st)
    # 残留标记与备份都被清掉
    assert not (tmp_path / "beta_state.json").exists()
    assert not (tmp_path / "saved_config.json").exists()
    # **配置与数据都没被动**
    assert st.load()["openai"]["model"] == "mine"
    assert hyp.get(h.id) is not None


def test_uninstall_does_not_delete_appdata():
    """installer 配置：卸载**不得**删用户数据。

    这是本轮最重要的一条持久化承诺，也是最容易被一次打包改动破坏的。
    """
    import json as _json
    pb = _json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))
    nsis = pb["build"]["nsis"]
    assert nsis.get("deleteAppDataOnUninstall") is False, \
        "卸载删数据 = 违反本轮最硬的要求"
    assert nsis.get("oneClick") is False, "需要能选安装目录"
    assert nsis.get("createDesktopShortcut") is True


# ---------------------------------------------------------------- Beta 回退

@pytest.fixture
def beta_dir(tmp_path, monkeypatch):
    """把 beta 模块的 DATA_DIR 指向 tmp。

    不加这个的话，unlock/disable 会把 beta_state.json 和
    saved_config.json 写进**真实用户数据目录** —— 测试污染用户数据。
    """
    from core import beta
    monkeypatch.setattr(beta, "DATA_DIR", tmp_path)
    return tmp_path


def test_beta_state_is_only_a_flag_not_a_data_gate(tmp_path):
    """**Beta 码绝不能成为数据生命周期的前置条件。**

    具体验证：没有 Beta 状态文件，数据库照常能开、数据照常在。
    """
    from memory.hypothesis_store import HypothesisStore
    db = tmp_path / "t.db"
    hyp = HypothesisStore(db)
    h, _ = hyp.create(statement="x", scope="y")
    # 完全没有 beta_state.json
    assert not (tmp_path / "beta_state.json").exists()
    assert hyp.get(h.id) is not None
    assert hyp.count_live() >= 1


def test_public_build_preset_reports_unavailable(monkeypatch):
    """公开版 preset 是空 dict → available=False，UI 入口该隐藏。"""
    import sys
    import types
    from core import beta

    mod = types.ModuleType("models._beta_preset")
    mod.PRESET = {}
    mod.BUILD_CHANNEL = "public"
    monkeypatch.setitem(sys.modules, "models._beta_preset", mod)

    assert beta.enabled() is False
    st = beta.preset_status()
    assert st["available"] is False
    assert st["channel"] == "public"
    assert "公开版" in st["reason"]
    # UI 依据
    assert beta.state()["available"] is False


def test_beta_build_preset_reports_available(monkeypatch):
    import sys
    import types
    from core import beta

    mod = types.ModuleType("models._beta_preset")
    mod.PRESET = {"provider": "openai", "base_url": "http://x/v1",
                  "model": "m", "api_key": "sk-x", "code_sha256": []}
    mod.BUILD_CHANNEL = "beta"
    monkeypatch.setitem(sys.modules, "models._beta_preset", mod)

    assert beta.enabled() is True
    st = beta.preset_status()
    assert st["available"] is True
    assert st["channel"] == "beta"


def test_incomplete_beta_preset_is_reported_honestly(monkeypatch):
    """预设缺 Key → 明确说"预设不完整"，而不是让用户输完码才发现。"""
    import sys
    import types
    from core import beta

    mod = types.ModuleType("models._beta_preset")
    mod.PRESET = {"provider": "openai", "base_url": "", "model": "", "api_key": ""}
    mod.BUILD_CHANNEL = "beta"
    monkeypatch.setitem(sys.modules, "models._beta_preset", mod)

    st = beta.preset_status()
    assert st["available"] is False
    assert "不完整" in st["reason"]


def test_beta_preset_never_exposes_key(monkeypatch):
    """preset_status 绝不能把 Key 端出去。"""
    import sys
    import types
    from core import beta

    mod = types.ModuleType("models._beta_preset")
    mod.PRESET = {"provider": "openai", "base_url": "http://x/v1",
                  "model": "m", "api_key": "sk-SECRET-123456",
                  "code_sha256": []}
    mod.BUILD_CHANNEL = "beta"
    monkeypatch.setitem(sys.modules, "models._beta_preset", mod)

    assert "sk-SECRET" not in json.dumps(beta.preset_status())
    assert "sk-SECRET" not in json.dumps(beta.state())


def test_disable_restores_config_and_keeps_data(tmp_path, beta_dir):
    """关闭 Beta → 还原模型配置，**数据一概不动**。"""
    from core.beta import disable, is_active, unlock
    from models.settings_store import ModelSettingsStore
    from memory.hypothesis_store import HypothesisStore
    import sys
    import types
    import hashlib

    code = "TEST-CODE-123"
    mod = types.ModuleType("models._beta_preset")
    mod.PRESET = {
        "provider": "openai", "base_url": "http://beta.example/v1",
        "model": "beta-model", "api_key": "sk-beta",
        "code_sha256": [hashlib.sha256(code.encode()).hexdigest()]}
    mod.BUILD_CHANNEL = "beta"
    sys.modules["models._beta_preset"] = mod

    db = tmp_path / "t.db"
    hyp = HypothesisStore(db)
    h, _ = hyp.create(statement="用户理解", scope="activity:x")

    st = ModelSettingsStore(tmp_path / "ms.json")
    st.update({"provider": "openai",
               "openai": {"base_url": "http://mine/v1", "model": "my-model",
                          "api_key": "sk-mine"}})

    ok, _ = unlock(st, code)
    assert ok is True
    assert is_active() is True
    assert st.load()["openai"]["model"] == "beta-model"

    ok, _ = disable(st)
    assert ok is True
    assert is_active() is False
    # 配置还原
    assert st.load()["openai"]["model"] == "my-model"
    # **数据一条不少**
    assert hyp.get(h.id) is not None
    assert hyp.count_live() >= 1

    del sys.modules["models._beta_preset"]


def test_wrong_code_does_not_touch_config(tmp_path, beta_dir):
    """码输错 → 配置与数据一概不动。"""
    import sys
    import types
    import hashlib
    from core.beta import is_active, unlock
    from models.settings_store import ModelSettingsStore

    mod = types.ModuleType("models._beta_preset")
    mod.PRESET = {"provider": "openai", "base_url": "http://b/v1",
                  "model": "b-model", "api_key": "sk-b",
                  "code_sha256": [hashlib.sha256(b"RIGHT").hexdigest()]}
    mod.BUILD_CHANNEL = "beta"
    sys.modules["models._beta_preset"] = mod

    st = ModelSettingsStore(tmp_path / "ms.json")
    st.update({"provider": "openai",
               "openai": {"base_url": "http://mine/v1", "model": "mine",
                          "api_key": "sk-mine"}})
    before = json.dumps(st.load())

    ok, msg = unlock(st, "WRONG")
    assert ok is False
    assert is_active() is False
    assert json.dumps(st.load()) == before, "输错码竟然改了配置"

    del sys.modules["models._beta_preset"]


# ---------------------------------------------------------------- 构建模式

def test_build_script_has_explicit_beta_mode():
    """构建脚本必须**显式区分**两种模式，不能再是"没变量就删 preset"。"""
    src = Path("scripts/build_core.ps1").read_text(encoding="utf-8")
    assert "param([switch]$Beta)" in src, "缺少 -Beta 构建模式开关"
    # Beta 模式缺凭证必须 throw，不能只打一行日志
    assert "throw" in src and "PAI_BETA_CODE" in src
    # 公开模式也要生成显式空 preset
    assert "PRESET = {}" in src
    # 两种模式都收 hidden-import（不再是"文件存在才加"）
    assert "$betaHidden = @('--hidden-import', 'models._beta_preset')" in src


def test_build_script_has_post_pack_self_check():
    """打包后必须自检 preset 真在包里——那正是 Beta 曾经失效的根因。"""
    src = Path("scripts/build_core.ps1").read_text(encoding="utf-8")
    assert "xref-" in src
    assert "models._beta_preset 在包内" in src or "models._beta_preset" in src
    assert "中止" in src


def test_build_preset_files_are_gitignored():
    """生成物绝不能进仓库 / history / 官网。"""
    gi = Path(".gitignore").read_text(encoding="utf-8")
    assert "_beta_preset" in gi
    assert "_build_version" in gi