"""LLM API 请求重试测试 (限速/超时/5xx → 重试; 4xx → 立即失败)."""

from __future__ import annotations

import requests

from src.core import llm as llm_mod
from src.core.llm import OllamaLLM


class _FakeResp:
    """伪造 requests.Response (够 raise_for_status/json 用)."""

    def __init__(self, status_code: int = 200, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 3},
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} error", response=None)

    def json(self) -> dict:
        return self._data


def _setup(monkeypatch, responses):
    """安装 fake post (按序返回 responses, 可含异常对象), 关闭重试延时."""
    monkeypatch.setattr(llm_mod, "API_RETRY_DELAY", 0)
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, _FakeResp) else _FakeResp(r)

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def test_success_no_retry(monkeypatch):
    """正常请求: 只调用 1 次, 返回内容正确."""
    calls = _setup(monkeypatch, [200])
    text, tokens = OllamaLLM().chat(system="s", user="u", max_tokens=8)
    assert text == "ok"
    assert tokens == 3
    assert len(calls) == 1


def test_429_then_success(monkeypatch):
    """限速 429 两次后成功: 自动重试, 最终拿到结果."""
    calls = _setup(monkeypatch, [429, 429, 200])
    text, _ = OllamaLLM().chat(system="s", user="u", max_tokens=8)
    assert text == "ok"
    assert len(calls) == 3


def test_5xx_retries_until_success(monkeypatch):
    """5xx 服务端错误同样重试."""
    calls = _setup(monkeypatch, [500, 503, 200])
    text, _ = OllamaLLM().chat(system="s", user="u", max_tokens=8)
    assert text == "ok"
    assert len(calls) == 3


def test_retry_exhausted_returns_error(monkeypatch):
    """重试耗尽 (API_RETRY_MAX 次) → 返回错误文本, 任务将标记失败."""
    monkeypatch.setattr(llm_mod, "API_RETRY_MAX", 3)
    calls = _setup(monkeypatch, [500, 500, 500, 500])
    text, _ = OllamaLLM().chat(system="s", user="u", max_tokens=8)
    assert text.startswith("[API error: 重试 3 次仍失败")
    assert len(calls) == 3  # 恰好重试 3 次后放弃, 不额外请求


def test_client_error_does_not_retry(monkeypatch):
    """400 客户端错误: 重试无意义, 立即失败 (只请求 1 次)."""
    calls = _setup(monkeypatch, [400])
    text, _ = OllamaLLM().chat(system="s", user="u", max_tokens=8)
    assert text.startswith("[API error:")
    assert len(calls) == 1


def test_timeout_retries(monkeypatch):
    """请求超时: 视为可恢复, 重试后成功."""
    calls = _setup(monkeypatch, [requests.exceptions.Timeout(), 200])
    text, _ = OllamaLLM().chat(system="s", user="u", max_tokens=8)
    assert text == "ok"
    assert len(calls) == 2
