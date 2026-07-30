"""DeepSeek LLM Client — 真实 AI 后端接入.

Replaces MockLLM with real API calls to DeepSeek (OpenAI-compatible).
Same interface: chat(system, user) → (response_text, tokens_consumed).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE = 0.7


class DeepSeekLLM:
    """Real DeepSeek API client with OpenAI-compatible chat completions.

    Usage:
        llm = DeepSeekLLM(api_key="sk-...")
        text, tokens = llm.chat(system="You are helpful.", user="Hello")
        text, tokens = llm.summarize(log_text="...")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_key = api_key or DEEPSEEK_API_KEY
        self.base_url = (base_url or DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or DEEPSEEK_MODEL

        if not self.api_key:
            raise ValueError(
                "DeepSeek API key is required. Set DEEPSEEK_API_KEY env var "
                "or pass api_key= to DeepSeekLLM()."
            )

    # ── Public API (same interface as MockLLM) ─────────────

    def chat(
        self,
        system: str,
        user: str,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> tuple[str, int]:
        """Send a chat request and return (response_text, tokens_consumed)."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        response_text, usage = self._call_api(messages, temperature, max_tokens)
        tokens = usage.get("total_tokens", 0) if usage else 0
        return response_text, tokens

    def summarize(
        self,
        log_text: str,
        temperature: float = 0.3,
        max_tokens: int = 256,
    ) -> tuple[str, int]:
        """Generate a concise summary from a log/text block."""
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个专业的助理，负责写工作总结。请用简洁的中文总结以下内容，"
                    "提取关键决策、待办事项和值得关注的低优先级事件。"
                    "输出格式：先写一段总结，然后列出关键决策和待办事项。"
                ),
            },
            {"role": "user", "content": f"请总结今天的工作日志：\n{log_text}"},
        ]

        response_text, usage = self._call_api(messages, temperature, max_tokens)
        tokens = usage.get("total_tokens", 0) if usage else 0
        return response_text, tokens

    # ── Internal ───────────────────────────────────────────

    def _call_api(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, Optional[dict]]:
        """Core API call. Returns (content_text, usage_dict)."""
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        logger.debug("DeepSeek API call: model=%s messages=%d", self.model, len(messages))

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            logger.error("DeepSeek API timeout")
            return "[API timeout]", None
        except requests.exceptions.RequestException as e:
            logger.error("DeepSeek API error: %s", e)
            return f"[API error: {e}]", None

        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        usage = data.get("usage")

        if not content:
            logger.warning("DeepSeek returned empty content. Raw: %s", str(data)[:200])

        return content, usage
