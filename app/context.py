"""应用上下文：把配置、内核、两个通道粘在一起，供 Web 控制台调用。

只在这里持有全局可变状态；适配器和内核都不互相 import，避免循环依赖。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from . import __version__, config, memory
from .adapters.bilibili import BiliAdapter
from .adapters.onebot import OneBotAdapter
from .core import Core

log = logging.getLogger("feiyu.context")


class AppContext:
    def __init__(self) -> None:
        self.cfg: Dict[str, Any] = config.load()
        self.core = Core(self.cfg)
        self.qq: Optional[OneBotAdapter] = None
        self.bili: Optional[BiliAdapter] = None
        self.events: Deque[Dict[str, Any]] = deque(maxlen=200)
        self._lock = threading.RLock()
        self.started_at = time.time()

    # ------------------------------------------------------------------
    def log_event(self, channel: str, text: str) -> None:
        item = {"ts": time.time(), "channel": channel, "text": text}
        with self._lock:
            self.events.appendleft(item)
        log.info("[%s] %s", channel, text)

    def recent_events(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.events)[:limit]

    # ------------------------------------------------------------------
    def reload(self) -> None:
        """配置改动后热更新：内核重建，适配器按需重建。"""
        with self._lock:
            self.cfg = config.load(force=True)
            self.core.reload(self.cfg)
        if self.qq:
            self.qq.cfg = self.cfg
        if self.bili:
            self.bili.cfg = self.cfg

    def ensure_qq(self) -> OneBotAdapter:
        with self._lock:
            if self.qq is None:
                self.qq = OneBotAdapter(self.cfg, self.core, self)
            return self.qq

    def ensure_bili(self) -> BiliAdapter:
        with self._lock:
            if self.bili is None:
                self.bili = BiliAdapter(self.cfg, self.core, self)
            return self.bili

    # ------------------------------------------------------------------
    def start_qq(self) -> Dict[str, Any]:
        a = self.ensure_qq()
        a.cfg = self.cfg
        a.start()
        self.log_event("system", "QQ 通道启动")
        return a.status()

    def stop_qq(self) -> None:
        if self.qq:
            self.qq.stop()
            self.log_event("system", "QQ 通道停止")

    def start_bili(self) -> Dict[str, Any]:
        a = self.ensure_bili()
        a.cfg = self.cfg
        a.start()
        self.log_event("system", "B 站巡逻启动")
        return a.status()

    def stop_bili(self) -> None:
        if self.bili:
            self.bili.stop()
            self.log_event("system", "B 站巡逻停止")

    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        bili_state = {"logged_in": False, "running": False}
        if self.bili:
            bili_state = self.bili.status()
        else:
            # 没实例化也能判断有没有存过 Cookie
            from .adapters.bilibili import BiliClient

            try:
                bili_state = {
                    "logged_in": BiliClient(self.cfg).has_login(),
                    "running": False,
                    "comments_today": memory.used_today("bili:comment"),
                }
            except Exception:
                pass

        return {
            "version": __version__,
            "uptime": int(time.time() - self.started_at),
            "llm_ready": config.ready_for_llm(self.cfg),
            "llm_model": (self.cfg.get("llm") or {}).get("model", ""),
            "persona": self.core.persona_title(),
            "qq": self.qq.status() if self.qq else {"connected": False, "error": "未启动"},
            "bili": bili_state,
            "memory": memory.stats(),
            "events": self.recent_events(40),
        }

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self.stop_qq()
        self.stop_bili()
