"""配置层：读写 config.json，提供默认值、路径管理与热更新。

设计原则
--------
* 只有一个配置文件 ``data/config.json``，用户不需要碰 .env。
* 缺字段时用默认值补齐（向后兼容，加字段不需要写迁移脚本）。
* 所有路径都相对项目根目录，方便整包拷走。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
COOKIE_PATH = DATA_DIR / "bili_cookie.json"
MEMORY_PATH = DATA_DIR / "memory.db"
LOG_DIR = DATA_DIR / "logs"

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "llm": {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "fallback_model": "",
        "temperature": 1.2,
        "max_tokens": 256,
        "timeout": 60,
        "retries": 3,
    },
    "persona": {
        "active": "default",
        "custom": {},
    },
    "qq": {
        "enabled": False,
        "ws_url": "ws://127.0.0.1:3001",
        "access_token": "",
        "self_id": 0,
        "allowed_groups": [],
        "allowed_friends": [],
        "at_only": True,
        "auto_chat_every": 0,
        "max_reply_chars": 60,
        "reply_emoji": True,
        "sticker_enabled": False,
        "sticker_dir": "data/stickers",
    },
    "bili": {
        "enabled": False,
        "dry_run": True,
        "max_reply_chars": 40,
        "daily_limit": 5,
        "delay_min": 180,
        "delay_max": 600,
        "videos_per_round": 5,
        "comment_enabled": True,
        "reply_enabled": True,
        "dm_enabled": False,
        "use_hot_comments": True,
        "use_subtitle": False,
        "owner_name": "",
        "phrases": [],
        "no_comment_keywords": ["抽奖", "广告"],
    },
    "safety": {
        "banned_words": [],
        "strip_markdown": True,
        "history_rounds": 8,
        "cooldown_seconds": 3,
        "max_input_chars": 500,
    },
    "web": {"host": "127.0.0.1", "port": 8787, "password": ""},
}

_lock = threading.RLock()
_cache: Dict[str, Any] | None = None


def ensure_dirs() -> None:
    for p in (DATA_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """把 over 合并进 base 的副本，缺失字段用默认值。"""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force: bool = False) -> Dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        ensure_dirs()
        raw: Dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                # 配置损坏时保留原文件，重新开始，避免用户直接崩在启动阶段
                try:
                    CONFIG_PATH.rename(CONFIG_PATH.with_suffix(".broken.json"))
                except Exception:
                    pass
        _cache = _deep_merge(DEFAULTS, raw)
        return _cache


def save(cfg: Dict[str, Any]) -> None:
    global _cache
    with _lock:
        ensure_dirs()
        merged = _deep_merge(DEFAULTS, cfg)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        _cache = merged


def patch(section: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """局部更新某个配置段并落盘。"""
    with _lock:
        cfg = load()
        sec = dict(cfg.get(section) or {})
        sec.update(values or {})
        cfg[section] = sec
        save(cfg)
        return cfg


def ready_for_llm(cfg: Dict[str, Any] | None = None) -> bool:
    cfg = cfg or load()
    return bool((cfg["llm"].get("api_key") or "").strip())


def load_cookie() -> Dict[str, Any]:
    if COOKIE_PATH.exists():
        try:
            return json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cookie(data: Dict[str, Any]) -> None:
    ensure_dirs()
    COOKIE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
