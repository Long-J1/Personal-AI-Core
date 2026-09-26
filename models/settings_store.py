"""模型设置存储：Provider 配置放在数据目录，不进源码、不进 git（D009）。

设计原则：
- Core 不硬编码任何一家模型供应商，只认 provider 名（ollama / openai）
- API Key 只落在数据目录的 model_settings.json，任何对外 API 返回前都经 public_view 脱敏
- 换 Provider 只是换"推理引擎"，本文件与 Personal Context / 事件数据完全无关
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core import config

log = logging.getLogger("models.settings")

PROVIDERS = ("ollama", "openai")


def default_config() -> dict:
    """默认配置：Ollama 本地（可用环境变量覆盖），云端 API 留空待用户填写。"""
    return {
        "provider": "ollama",
        "ollama": {
            "url": config.settings.ollama_url,
            "model": config.settings.model,
        },
        "openai": {"base_url": "", "model": "", "api_key": ""},
    }


def mask_key(key: str) -> str:
    """API Key 脱敏：只留头尾，中间打码。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def public_view(cfg: dict) -> dict:
    """对外（UI/API）可见的配置视图：永不含明文 API Key。"""
    return {
        "provider": cfg.get("provider", "ollama"),
        "ollama": {
            "url": cfg.get("ollama", {}).get("url", ""),
            "model": cfg.get("ollama", {}).get("model", ""),
        },
        "openai": {
            "base_url": cfg.get("openai", {}).get("base_url", ""),
            "model": cfg.get("openai", {}).get("model", ""),
            "api_key_set": bool(cfg.get("openai", {}).get("api_key")),
            "api_key_masked": mask_key(cfg.get("openai", {}).get("api_key", "")),
        },
    }


class ModelSettingsStore:
    """读写 数据目录/model_settings.json（原子写、损坏时回退默认值）。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else (config.DATA_DIR / "model_settings.json")

    # ---------- 读 ----------
    def load(self) -> dict:
        cfg = default_config()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cfg
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("模型设置读取失败，回退默认值：%s", exc)
            return cfg
        if not isinstance(raw, dict):
            return cfg
        if raw.get("provider") in PROVIDERS:
            cfg["provider"] = raw["provider"]
        for sect in ("ollama", "openai"):
            vals = raw.get(sect)
            if isinstance(vals, dict):
                for k, v in vals.items():
                    if k in cfg[sect] and isinstance(v, str):
                        cfg[sect][k] = v
        return cfg

    # ---------- 合并（不落盘） ----------
    def resolve(self, patch: dict | None) -> dict:
        """把 patch 合并进当前配置并校验；不保存。校验失败抛 ValueError。"""
        cfg = self.load()
        if not isinstance(patch, dict):
            return self._validate(cfg)

        provider = patch.get("provider")
        if provider is not None:
            if provider not in PROVIDERS:
                raise ValueError(f"未知 Provider：{provider}（可选：{'/'.join(PROVIDERS)}）")
            cfg["provider"] = provider
        for sect in ("ollama", "openai"):
            vals = patch.get(sect)
            if isinstance(vals, dict):
                for k, v in vals.items():
                    # api_key: None=不修改（patch 里不出现），""=清除
                    if k in cfg[sect] and isinstance(v, str):
                        cfg[sect][k] = v
        return self._validate(cfg)

    def _validate(self, cfg: dict) -> dict:
        if cfg["provider"] == "openai":
            if not cfg["openai"]["base_url"].strip():
                raise ValueError("云端 API 需要填写 Base URL")
            if not cfg["openai"]["model"].strip():
                raise ValueError("云端 API 需要填写模型名")
        else:
            if not cfg["ollama"]["url"].strip():
                raise ValueError("Ollama 地址不能为空")
            if not cfg["ollama"]["model"].strip():
                raise ValueError("Ollama 模型名不能为空")
        return cfg

    # ---------- 保存 ----------
    def update(self, patch: dict | None) -> dict:
        """合并 + 校验 + 落盘，返回保存后的配置。"""
        cfg = self.resolve(patch)
        self.save(cfg)
        return cfg

    def save(self, cfg: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def default_store() -> ModelSettingsStore:
    """默认存储（路径取自当前 config.DATA_DIR，随部署环境变化）。"""
    return ModelSettingsStore()
