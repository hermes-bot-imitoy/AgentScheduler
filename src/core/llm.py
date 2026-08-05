"""DeepSeek LLM Client — 真实 AI 后端接入.

DeepSeek V4 Flash with optional thinking (reasoning) mode.
OpenAI-compatible API: chat(system, user) → (response_text, tokens_consumed).

Thinking mode: set DEEPSEEK_THINKING=true or pass thinking=True.
When enabled, DeepSeek returns reasoning_content before the final answer.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "sk-9198d90e243440aabbac814ad8647f1c")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_THINKING = os.environ.get("DEEPSEEK_THINKING", "true").lower() in ("1", "true", "yes", "on")

DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE = 0.7


# # DeepSeek API客户端, 支持思维链(thinking)模式. 参数: api_key, base_url, model, thinking
class DeepSeekLLM:
    """Real DeepSeek API client with optional thinking (chain-of-thought) mode.

    Usage:
        llm = DeepSeekLLM(api_key="sk-...")
        # Thinking mode via env: DEEPSEEK_THINKING=true
        # or constructor: DeepSeekLLM(thinking=True)
        text, tokens = llm.chat(system="You are helpful.", user="Hello")
        text, tokens = llm.summarize(log_text="...")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        thinking: Optional[bool] = None,
        label: Optional[str] = None,
    ):
        self.api_key = api_key or DEEPSEEK_API_KEY
        self.base_url = (base_url or DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or DEEPSEEK_MODEL
        self.thinking = thinking if thinking is not None else DEEPSEEK_THINKING
        # 角色标识: DEBUG 日志前缀, 便于多角色并发时区分是谁在调 API
        self.label = label or ""

        if not self.api_key:
            raise ValueError(
                "DeepSeek API key is required. Set DEEPSEEK_API_KEY env var "
                "or pass api_key= to DeepSeekLLM()."
            )

    # ── 调试日志 (带角色前缀) ─────────────────────────────

    def _debug(self, msg: str, *args) -> None:
        """打 DEBUG 日志, 消息前加角色标识 (如 [ceo]), 无角色则不加."""
        if self.label:
            # 前缀拼进格式串: 占位符总数 = 1(label) + msg 自身占位符
            logger.debug("[%s] " + msg, self.label, *args)
        else:
            logger.debug(msg, *args)

    # ── Public API (same interface as MockLLM) ─────────────

# # 发送聊天请求. 返回(回复文本, Token数). 参数: system=系统提示, user=用户输入
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
            self._debug("chat: 追加 system 消息 (%d 字符): %s",
                        len(system), system)
        messages.append({"role": "user", "content": user})
        self._debug("chat: 追加 user 消息 (%d 字符): %s",
                    len(user), user)

        response_text, usage = self._call_api(messages, temperature, max_tokens)
        tokens = usage.get("total_tokens", 0) if usage else 0
        return response_text, tokens

# # 从日志/文本生成简洁总结. 返回(总结文本, Token数)
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
        self._debug("summarize: 追加 user 消息 (%d 字符): %s",
                    len(log_text), log_text)

        response_text, usage = self._call_api(messages, temperature, max_tokens)
        tokens = usage.get("total_tokens", 0) if usage else 0
        return response_text, tokens

    # ── Internal ───────────────────────────────────────────

# # 核心API调用. 返回(回复文本, usage字典). thinking模式自动启用推理并提取reasoning_content
    def _call_api(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, Optional[dict]]:
        """Core API call. Returns (content_text, usage_dict).

        When thinking mode is enabled, DeepSeek returns reasoning_content
        before the final content. We include reasoning in debug logs but
        return only the final content to the caller.
        """
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Enable thinking mode for deepseek-v4-flash and compatible models
        if self.thinking:
            payload["thinking"] = {"type": "enabled"}
            # When thinking is enabled, max_tokens must be >= thinking budget
            # Set a higher default to leave room for reasoning
            if max_tokens < 1024:
                payload["max_tokens"] = 1024

        self._debug(
            "DeepSeek API call: model=%s messages=%d thinking=%s",
            self.model, len(messages), self.thinking,
        )

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            logger.error("DeepSeek API timeout")
            return "[API timeout]", None
        except requests.exceptions.RequestException as e:
            logger.error("DeepSeek API error: %s", e)
            return f"[API error: {e}]", None

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})

        # Extract content — if thinking is enabled, reasoning_content is separate
        content = message.get("content", "")
        reasoning = message.get("reasoning_content", "")

        if reasoning:
            self._debug("DeepSeek reasoning (%d chars): %s",
                        len(reasoning), reasoning)

        # If content is empty but thinking was enabled, the model might have
        # put everything in reasoning_content (edge case)
        if not content and reasoning:
            logger.warning("DeepSeek: empty content, falling back to reasoning_content")
            content = reasoning

        usage = data.get("usage")

        if not content:
            logger.warning("DeepSeek returned empty content. Raw: %s", str(data)[:200])

        # Log token breakdown when thinking is on
        if usage and self.thinking:
            self._debug(
                "DeepSeek tokens: prompt=%s completion=%s reasoning=%s total=%s",
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
                usage.get("completion_tokens_details", {}).get("reasoning_tokens", "?"),
                usage.get("total_tokens", "?"),
            )

        return content, usage

    # ── 原生 function calling ─────────────────────────────

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = 1024,
    ) -> tuple[str, list[dict], Optional[dict]]:
        """原生 function calling 请求 (OpenAI 兼容).

        请求携带 tools 声明, 响应里 message.tool_calls 结构化给出工具调用
        (name + arguments JSON), 而非文本协议 ```tool_call 块.

        参数:
            messages: 对话消息 (含 role/tool_call_id 等字段, 原样透传).
            tools:    OpenAI 格式工具声明列表.

        返回:
            (content, raw_tool_calls, usage).
            raw_tool_calls: 响应中 message["tool_calls"] 原样列表 (每个含
            id/type/function{name,arguments}); 无工具调用时为 [].
        """
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": tools,
            "tool_choice": "auto",
        }

        # 与 chat() 相同的 thinking 模式处理
        if self.thinking:
            payload["thinking"] = {"type": "enabled"}
            if max_tokens < 1024:
                payload["max_tokens"] = 1024

        self._debug(
            "DeepSeek API call (tools): model=%s messages=%d tools=%d thinking=%s",
            self.model, len(messages), len(tools), self.thinking,
        )

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            logger.error("DeepSeek API timeout")
            return "[API timeout]", [], None
        except requests.exceptions.RequestException as e:
            logger.error("DeepSeek API error: %s", e)
            return f"[API error: {e}]", [], None

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})

        content = message.get("content") or ""
        reasoning = message.get("reasoning_content", "")
        raw_calls = message.get("tool_calls") or []

        if reasoning:
            self._debug("DeepSeek reasoning (%d chars): %s",
                        len(reasoning), reasoning)

        # 原生工具调用: content 可能为空 (推理全在 reasoning_content),
        # 有 tool_calls 时以 tool_calls 为准, 不回退到 reasoning 当正文
        if not content and reasoning and not raw_calls:
            logger.warning("DeepSeek: empty content, falling back to reasoning_content")
            content = reasoning

        usage = data.get("usage")

        if usage and self.thinking:
            self._debug(
                "DeepSeek tokens: prompt=%s completion=%s reasoning=%s total=%s",
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
                usage.get("completion_tokens_details", {}).get("reasoning_tokens", "?"),
                usage.get("total_tokens", "?"),
            )

        return content, raw_calls, usage
