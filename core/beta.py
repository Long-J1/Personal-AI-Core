"""Beta 体验码解锁（V0.5.5 真实用户入门层）。

定位（请不要误读）
------------------
这是 **V0.5.5 早期 Beta 测试体验凭证**。

它是：让第一次用的人跳过 Provider / Base URL / Model / API Key 的工程捷径。
它**不是**：安全授权系统、账户系统、正式用户系统、计费或配额系统。

所以文案里不承诺"安全"，只承诺"省事"。开发文档见
`docs/decisions/D020-beta-experience-code.md`。

已知安全限制（如实列出，别自我美化）
------------------------------------
1. 预设凭证构建时打进安装包 → 任何人都能从安装目录里挖出来；
2. 体验码只在本机做一次 SHA-256 比对 → 没有服务端校验、没有吊销、没有过期；
3. 没有设备绑定、没有次数限制 → 码可以随便转发；
4. 谁拿到码谁都能用 → 用量直接算在作者那个云端账号上。

预设凭证从哪来
--------------
构建时由 `scripts/build_core.ps1` 生成 `models/_beta_preset.py`
（已 gitignore，不进仓库 / history / 官网 / README）。
仓库里只有这个模块，没有 Key、没有码。
开发环境没生成过 → `status.available = False`，UI 显示"当前版本未提供"。

存储
----
沿用现有的 `data/model_settings.json`（本来就明文存 key），不新增存储层。
「关闭 Beta」时把解锁前的配置原样写回去——只碰模型设置，
**不碰 Memory、不碰聊天历史、不碰数据库**。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

from core.config import DATA_DIR

log = logging.getLogger("core.beta")

STATE_FILE = "beta_state.json"
_PRESET_MODULE = "models._beta_preset"

# 本机保存「解锁前的配置」，关闭 Beta 时原样还原
_SAVED_BACKUP = "saved_config.json"
_SAVED_MARK = "beta_saved"


# ---------------------------------------------------------------- 预设加载
def load_preset() -> dict | None:
    """读构建时注入的预设；没有就返回 None（= 功能不可用，不是出错）。"""
    try:
        mod = __import__(_PRESET_MODULE, fromlist=["PRESET"])
    except Exception:
        return None
    p = getattr(mod, "PRESET", None)
    if not isinstance(p, dict) or not p.get("provider"):
        return None
    return p


def enabled() -> bool:
    return load_preset() is not None


def _expected_digests(preset: dict) -> list[str]:
    return [str(x).strip().lower()
            for x in (preset.get("code_sha256") or []) if str(x).strip()]


def _configured_codes(preset: dict) -> list[str]:
    """可选：构建时也可以直接写明文码（便于作者自查），运行时优先用摘要。"""
    return [str(x) for x in (preset.get("codes") or []) if str(x).strip()]


def verify_code(code: str) -> bool:
    """本地比对体验码。不联网、不上报、不记录明文码到日志。"""
    if not code or not code.strip():
        return False
    got = hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
    preset = load_preset()
    if preset is None:
        return False
    for d in _expected_digests(preset):
        if hmac.compare_digest(got, d):
            return True
    for c in _configured_codes(preset):
        if hmac.compare_digest(code.strip(), c):
            return True
    return False


# ---------------------------------------------------------------- Beta 状态
def _state_path() -> Path:
    return Path(DATA_DIR) / STATE_FILE


def _read_state() -> dict:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def is_active() -> bool:
    return bool(_read_state().get("active"))


def state() -> dict:
    return {
        "available": enabled(),
        "active": is_active(),
        "provider": _read_state().get("provider"),
        "model": _read_state().get("model"),
        "unlocked_at": _read_state().get("unlocked_at"),
    }


# ---------------------------------------------------------------- 备份/还原
def _backup_path() -> Path:
    return Path(DATA_DIR) / _SAVED_BACKUP


def _save_backup(cfg: dict) -> None:
    p = _backup_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)


def _load_backup() -> dict | None:
    try:
        raw = json.loads(_backup_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def _clear_backup() -> None:
    try:
        _backup_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 解锁/关闭
def unlock(store, code: str) -> tuple[bool, str]:
    """输入体验码 → 套用预设配置 → 自动连接测试。

    成功时把**解锁前的配置**存一份，之后「关闭 Beta」能原样还原。
    失败时只回一句中性的话，不暴露端点/模型/Key 任何细节。
    """
    preset = load_preset()
    if preset is None:
        return False, "体验码无效或当前不可用"
    if not verify_code(code):
        return False, "体验码无效或当前不可用"

    provider = str(preset.get("provider") or "")
    if provider not in ("openai", "ollama"):
        log.warning("Beta 预设 provider 非法：%s", provider)
        return False, "体验码无效或当前不可用"

    # 先备份**当前生效**配置（用户可能已经手填过一套）
    current = store.load()
    if not is_active():
        _save_backup(current)

    patch = _preset_patch(preset)
    sect = "openai" if provider == "openai" else "ollama"

    try:
        cfg = store.update(patch)
    except ValueError as exc:
        log.warning("Beta 预设配置无效：%s", exc)
        return False, "体验码无效或当前不可用"

    # 只写状态，不落配置内容（配置已经在 model_settings.json 里了）
    st = {"active": True, "provider": provider,
          "model": cfg.get(sect, {}).get("model", ""),
          "unlocked_at": time.time()}
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)

    log.info("Beta 体验已解锁 provider=%s model=%s", provider, st["model"])
    return True, "已准备好，可以开始聊天"


def disable(store) -> tuple[bool, str]:
    """关闭 Beta：还原解锁前的 Provider 配置。

    只改模型设置。Memory / 聊天历史 / 数据库一概不碰。
    """
    if not is_active():
        return True, "当前不在 Beta 体验状态"

    backup = _load_backup()
    if backup:
        try:
            store.save(backup)          # 原样写回，不做校验（用户原来的组合可能不满足现行校验）
        except Exception as exc:        # pragma: no cover - 磁盘异常
            log.warning("还原 Beta 前的配置失败：%s", exc)
            return False, "关闭失败，请稍后再试"
    _clear_backup()
    p = _state_path()
    try:
        p.unlink()
    except OSError:
        pass
    log.info("Beta 体验已关闭，模型配置已还原")
    return True, "已关闭 Beta 体验，模型设置已还原"


def _preset_patch(preset: dict) -> dict:
    """预设 → 可直接喂给 ModelSettingsStore.update 的 patch。

    预设是**扁平**的（provider / base_url / model / api_key / url），
    存盘结构才是分段的（openai.xxx / ollama.xxx）。这里做唯一一次转换，
    unlock 和 apply_on_startup 共用，免得两处各写一遍然后走偏
    （曾因此漏掉 model，只换了 provider）。
    """
    provider = str(preset.get("provider") or "")
    sect = "openai" if provider == "openai" else "ollama"
    patch: dict = {"provider": provider}
    for k in ("base_url", "model", "url", "api_key"):
        v = preset.get(k)
        if v:
            patch.setdefault(sect, {})[k] = str(v)
    return patch


def apply_on_startup(store) -> None:
    """Core 启动时：Beta 活跃但配置被外部改过 → 重新套用预设。

    没有活跃的 Beta 状态就什么都不做（不碰用户自己的配置）。
    """
    if not is_active():
        return
    preset = load_preset()
    if preset is None:
        return
    patch = _preset_patch(preset)
    provider = patch["provider"]
    sect = "openai" if provider == "openai" else "ollama"
    want = patch.get(sect, {}).get("model", "")
    try:
        cur = store.load()
    except Exception:                   # pragma: no cover
        return
    if cur.get("provider") == provider and cur.get(sect, {}).get("model") == want:
        return                          # 已经是预设状态，什么都不做
    try:
        store.update(patch)
    except Exception as exc:            # pragma: no cover
        log.warning("启动时重新套用 Beta 预设失败：%s", exc)