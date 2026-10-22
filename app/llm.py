"""LLM 客户端：只做一件事——把 messages 变成一句话。

兼容所有 OpenAI 风格的 /chat/completions 端点，所以 DeepSeek、通义、智谱、
本机 Ollama、lm-studio 改个 base_url 就能用。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("feiyu.llm")


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、额度、格式都算）。"""


class LLMClient:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        llm = cfg.get("llm", {})
        self.api_key: str = (llm.get("api_key") or "").strip()
        self.base_url: str = (llm.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.model: str = llm.get("model") or "deepseek-chat"
        self.fallback_model: str = llm.get("fallback_model") or ""
        self.temperature: float = float(llm.get("temperature", 1.2))
        self.max_tokens: int = int(llm.get("max_tokens", 256))
        self.timeout: float = float(llm.get("timeout", 60))
        self.retries: int = int(llm.get("retries", 3))

    # ------------------------------------------------------------------ 基础
    def reload(self, cfg: Dict[str, Any]) -> None:
        self.__init__(cfg)  # type: ignore[misc]

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/chat/completions"

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        if not self.api_key:
            raise LLMError("还没有填 DeepSeek API Key")

        models = [model or self.model]
        if self.fallback_model and self.fallback_model not in models:
            models.append(self.fallback_model)

        last_err: Exception | None = None
        for name in models:
            try:
                return self._call(name, messages, temperature, max_tokens)
            except LLMError as e:
                last_err = e
                log.warning("模型 %s 调用失败：%s", name, e)
                if "鉴权" in str(e) or "余额" in str(e):
                    raise  # 换模型也救不了
        raise LLMError(str(last_err or "调用失败"))

    def _call(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        delay = 1.0
        for attempt in range(1, self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(self._endpoint(), json=payload, headers=headers)
                if resp.status_code == 200:
                    return self._extract(resp.text)
                if resp.status_code in (401, 403):
                    raise LLMError("鉴权失败：API Key 不对，或者没有这个模型的权限")
                if resp.status_code == 402:
                    raise LLMError("余额不足，去 platform.deepseek.com 充值")
                if resp.status_code == 429:
                    raise LLMError("触发限流（429），稍等一会儿")
                if resp.status_code >= 500:
                    raise httpx.HTTPError(f"服务端 {resp.status_code}")
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except LLMError:
                raise
            except Exception as e:  # 网络抖动、超时
                if attempt >= self.retries:
                    raise LLMError(f"网络请求失败：{e}") from e
                log.warning("第 %d 次调用失败（%s），%.0fs 后重试", attempt, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise LLMError("调用失败")

    @staticmethod
    def _extract(raw: str) -> str:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"返回的不是 JSON：{raw[:200]}") from e
        if isinstance(data, dict) and data.get("error"):
            msg = data["error"].get("message") if isinstance(data["error"], dict) else data["error"]
            raise LLMError(f"接口报错：{msg}")
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"返回结构不对：{raw[:200]}") from e
        content = (msg.get("content") or "").strip()
        if not content and msg.get("reasoning_content"):
            # 思考型模型偶尔把内容塞进 reasoning 里，兜一层
            content = str(msg["reasoning_content"]).strip()
        if not content:
            raise LLMError("模型返回了空内容")
        return content

    # ------------------------------------------------------------------ 自检
    def test(self) -> Dict[str, Any]:
        """返回 {'ok': bool, 'model': str, 'reply': str, 'error': str}"""
        try:
            reply = self.chat(
                [
                    {"role": "system", "content": "只回复两个字：在的"},
                    {"role": "user", "content": "测试"},
                ],
                max_tokens=16,
                temperature=0.3,
            )
            return {"ok": True, "model": self.model, "reply": reply[:60], "error": ""}
        except Exception as e:
            return {"ok": False, "model": self.model, "reply": "", "error": str(e)}
