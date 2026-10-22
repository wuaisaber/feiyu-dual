"""统一对话内核：人格 + 记忆 + 模型 + 后处理。

QQ 和 B 站都走这一个函数，区别只在"喂进来的上下文"和"发出去的通道"。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from . import memory, personas, safety
from .llm import LLMClient, LLMError

log = logging.getLogger("feiyu.core")


class Core:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.llm = LLMClient(cfg)
        self._last_reply: Dict[str, float] = {}
        self._lock = threading.RLock()

    def reload(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.llm.reload(cfg)

    # ------------------------------------------------------------------
    def reply(
        self,
        platform: str,
        channel: str,
        user_text: str,
        *,
        name: str = "",
        persona: Optional[str] = None,
        max_chars: int = 60,
        system_extra: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        use_history: bool = True,
        record: bool = True,
    ) -> str:
        cfg = self.cfg
        safety_cfg = cfg.get("safety", {})
        rounds = int(safety_cfg.get("history_rounds", 8))
        banned = safety_cfg.get("banned_words") or []

        system = personas.build_system_prompt(
            cfg,
            platform,
            persona_name=persona,
            max_chars=max_chars,
            extra_rules=[system_extra] if system_extra else None,
        )

        known = memory.facts(platform, channel)
        if known:
            system += "\n\n你记得这些事：\n" + "\n".join(f"- {k}：{v}" for k, v in known.items())

        messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
        if use_history:
            messages.extend(memory.history(platform, channel, rounds))
        messages.append({"role": "user", "content": user_text})

        raw = self.llm.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens or max(64, max_chars * 3),
        )
        out = safety.clean(
            raw,
            max_chars=max_chars,
            banned=banned,
            strip_markdown=bool(safety_cfg.get("strip_markdown", True)),
        )

        if record and out:
            memory.append(platform, channel, "user", user_text, user_id="", name=name)
            memory.append(platform, channel, "assistant", out)
        with self._lock:
            self._last_reply[f"{platform}:{channel}"] = time.time()
        return out

    # ------------------------------------------------------------------
    def cooldown_ok(self, platform: str, channel: str) -> bool:
        """同一个会话别回太快，防自己和自己的 JSON 打架。"""
        gap = float(self.cfg.get("safety", {}).get("cooldown_seconds", 3))
        with self._lock:
            last = self._last_reply.get(f"{platform}:{channel}", 0.0)
        return (time.time() - last) >= gap

    def persona_title(self, persona: Optional[str] = None) -> str:
        return personas.get_persona(self.cfg, persona)["title"]

    # ------------------------------------------------------------------
    def selftest(self) -> List[str]:
        """离线自检：不联网，只验证拼装与后处理链路。"""
        problems: List[str] = []
        prompt = personas.build_system_prompt(self.cfg, "qq", "default", 60)
        if "大肥鱼" not in prompt:
            problems.append("人格 prompt 缺少角色名")
        for name in personas.persona_names():
            p = personas.get_persona(self.cfg, name)
            if not p.get("style"):
                problems.append(f"人格 {name} 没有说话方式")
        sample = safety.clean("**你好**：这是「测试」\n\n- 第一点\n- 第二点", 20)
        if "*" in sample or "\n" in sample:
            problems.append("markdown 清理没生效")
        if len(safety.clip("一二三四五六七八九十", 5)) != 5:
            problems.append("截断逻辑异常")
        return problems
