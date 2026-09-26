"""阶段2：数据导出 / 备份 / 恢复 / 导入 / 数据位置（D010）。

核心承诺：换电脑、换模型、换外壳——个人数据一个字节都不能丢；
恢复永远可回滚（先拍安全网备份）；坏包/坏路径一律拒绝。
"""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import interfaces.webapp as webapp_mod
from core.data_manager import DataManager, DataError
from interfaces.webapp import create_app
from memory.chat_store import ChatStore
from memory.context_store import ContextStore
from memory.event import Event
from memory.store import MemoryStore
from models.factory import ModelRegistry
from models.settings_store import ModelSettingsStore


@pytest.fixture
def api(tmp_path, monkeypatch):
    """数据目录/DB/缩略图全部落在 tmp，绝不碰真实 data/。"""
    import core.pipeline as pm

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    monkeypatch.setattr(pm, "THUMB_DIR", thumbs)
    monkeypatch.setattr(webapp_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(webapp_mod, "THUMB_DIR", thumbs)

    db = data_dir / "personal_ai.db"
    store = MemoryStore(db)
    ctx = ContextStore(db)
    chat = ChatStore(db)
    registry = ModelRegistry(ModelSettingsStore(data_dir / "model_settings.json"))
    app = create_app(store=store, model=registry, context_store=ctx, chat_store=chat)
    return TestClient(app), store, ctx, data_dir


def _seed(store, ctx) -> None:
    store.add_event(Event(
        created_at=datetime.now().astimezone() - timedelta(minutes=3),
        description="用户在书桌前写数学作业",
        scene="书房", activity="写作业", importance=0.7,
    ))
    ctx.add_entry("interaction", "回答要简短", source="chat")


# ---------- 位置 / 导出 ----------

def test_data_location(api):
    c, store, ctx, data_dir = api
    r = c.get("/api/data/location").json()
    assert r["ok"] and r["data_dir"] == str(data_dir)
    assert r["db_exists"] is True
    assert r["size_bytes"] > 0


def test_export_zip_contains_readable_db_and_manifest(api):
    c, store, ctx, data_dir = api
    _seed(store, ctx)

    r = c.get("/api/data/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    assert "personal_ai_data" in r.headers.get("content-disposition", "")

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "personal_ai.db" in names and "manifest.json" in names

    # DB 快照是活的：解出来能连、行数对得上（WAL 里的数据也进包）
    snap = data_dir / "_check.db"
    snap.write_bytes(zf.read("personal_ai.db"))
    con = sqlite3.connect(snap)
    try:
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM context_entries").fetchone()[0] == 1
    finally:
        con.close()
        snap.unlink()

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["kind"] == "personal_ai_data"
    assert manifest["counts"]["events"] == 1


# ---------- 备份 / 恢复 ----------

def test_backup_create_and_list(api):
    c, store, ctx, data_dir = api
    _seed(store, ctx)
    r = c.post("/api/data/backup").json()
    assert r["ok"] and r["name"].startswith("backup_")

    items = c.get("/api/data/backups").json()["items"]
    assert len(items) == 1
    assert items[0]["size_bytes"] > 0
    assert (data_dir / "backups" / items[0]["name"]).exists()


def test_restore_roundtrip_recovers_deleted_context(api):
    c, store, ctx, data_dir = api
    _seed(store, ctx)
    backup = c.post("/api/data/backup").json()["name"]

    # 把数据全删了
    entry_id = c.get("/api/context").json()["items"][0]["id"]
    c.delete(f"/api/context/{entry_id}")
    c.delete("/api/memories")
    assert c.get("/api/context").json()["total"] == 0
    assert store.count_events() == 0

    # 从备份恢复
    r = c.post("/api/data/restore", json={"name": backup}).json()
    assert r["ok"] and r["safety_backup"], "恢复前必须有安全网备份"
    assert r["counts"]["events"] == 1 and r["counts"]["context_entries"] == 1

    after = c.get("/api/context").json()
    assert after["total"] == 1
    assert after["items"][0]["content"] == "回答要简短"
    assert store.count_events() == 1
    # 安全网备份也真在盘上
    assert any(b["name"] == r["safety_backup"] for b in c.get("/api/data/backups").json()["items"])


def test_restore_rejects_bad_names(api):
    c, store, ctx, data_dir = api
    # 路径穿越
    r = c.post("/api/data/restore", json={"name": "../evil.zip"})
    assert r.status_code == 400 and "非法" in r.json()["error"]
    # 不存在
    r = c.post("/api/data/restore", json={"name": "nope.zip"})
    assert r.status_code == 400 and "不存在" in r.json()["error"]


def test_restore_swaps_model_settings_and_reloads_registry(api):
    """模型设置跟着数据走：换电脑/恢复备份后连 Provider 配置一起回来。"""
    c, store, ctx, data_dir = api
    # 1) 存一份 Ollama 配置并备份
    c.put("/api/model/settings", json={"provider": "ollama"})
    backup = c.post("/api/data/backup").json()["name"]

    # 2) 切成云端 API（模拟之后改过配置）
    r = c.put("/api/model/settings", json={
        "provider": "openai",
        "openai": {"base_url": "https://x/v1", "model": "m", "api_key": "sk-1"},
    })
    assert r.json()["current"]["provider"] == "openai"

    # 3) 恢复备份 → Provider 配置也回到 Ollama（含门面热切换）
    c.post("/api/data/restore", json={"name": backup})
    s = c.get("/api/model/settings").json()
    assert s["provider"] == "ollama"
    assert s["current"]["provider"] == "ollama"


# ---------- 导入 ----------

def test_import_roundtrip(api):
    c, store, ctx, data_dir = api
    _seed(store, ctx)
    pkg = c.get("/api/data/export").content

    # 清空（模拟换电脑后的空环境）
    entry_id = c.get("/api/context").json()["items"][0]["id"]
    c.delete(f"/api/context/{entry_id}")
    c.delete("/api/memories")
    assert c.get("/api/context").json()["total"] == 0

    r = c.post("/api/data/import", files={"file": ("personal_ai_data.zip", pkg, "application/zip")})
    body = r.json()
    assert r.status_code == 200 and body["ok"]
    assert body["counts"]["events"] == 1 and body["counts"]["context_entries"] == 1
    assert c.get("/api/context").json()["total"] == 1
    assert store.count_events() == 1


def test_import_rejects_garbage_and_wrong_zip(api):
    c, store, ctx, data_dir = api
    # 垃圾字节
    r = c.post("/api/data/import", files={"file": ("x.zip", b"definitely not a zip", "application/zip")})
    assert r.status_code == 400 and "zip" in r.json()["error"].lower()

    # 合法 zip 但不是 Personal AI 数据包
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello")
    r = c.post("/api/data/import", files={"file": ("x.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400 and "personal_ai.db" in r.json()["error"]


def test_zip_slip_rejected(api, tmp_path):
    """恶意包里的 ../ 路径必须被拒绝，不能写出数据目录。"""
    c, store, ctx, data_dir = api
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("personal_ai.db", b"SQLite format 3\x00" + b"\x00" * 100)
        zf.writestr("../evil.txt", "pwned")
    r = c.post("/api/data/import", files={"file": ("evil.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
    assert not (tmp_path / "evil.txt").exists()
    assert not (data_dir.parent / "evil.txt").exists()


def test_corrupt_db_in_package_rejected(api):
    """包里数据库不是合法 SQLite → 拒绝恢复（不许把好数据换成坏数据）。"""
    c, store, ctx, data_dir = api
    _seed(store, ctx)
    c.post("/api/data/backup")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("personal_ai.db", b"this is not a database at all")
    r = c.post("/api/data/import", files={"file": ("bad.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400 and "SQLite" in r.json()["error"]
    # 当前数据完好无损
    assert c.get("/api/context").json()["total"] == 1
    assert store.count_events() == 1


def test_export_excludes_backups_dir(api):
    """导出包不含 backups/（不然换电脑越搬越大、还会递归套娃）。"""
    c, store, ctx, data_dir = api
    _seed(store, ctx)
    c.post("/api/data/backup")
    zf = zipfile.ZipFile(io.BytesIO(c.get("/api/data/export").content))
    assert not any(n.startswith("backups/") for n in zf.namelist())
