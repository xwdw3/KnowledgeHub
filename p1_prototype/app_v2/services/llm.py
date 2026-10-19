"""LLM 封装：统一 chat / tool-call 接口，内置重试与超时。

为什么单独一层：
  业务代码不应该关心用的是 DeepSeek 还是内网 Qwen，
  换模型只改配置；调用失败的重试与超时策略也只在这一处维护。
"""
from __future__ import annotations

import logging
import time

from app_v2.core.response import UpstreamError
from app_v2.core.settings import settings

logger = logging.getLogger(__name__)

_client = None
MAX_RETRY = 2
RETRY_BACKOFF = 1.0


def _client_or_none():
    global _client
    if settings.resolved_model_mode() != "real":
        return None
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=settings.chat_api_key, base_url=settings.chat_base_url, timeout=45, max_retries=0)
    return _client


def is_available() -> bool:
    return _client_or_none() is not None


def chat(messages: list[dict], tools: list[dict] | None = None, temperature: float | None = None) -> dict:
    """返回归一化结果：{content, tool_calls, finish_reason, usage}。"""
    started = time.perf_counter()
    try:
        out = _chat_impl(messages, tools, temperature)
    except Exception:
        _observe(False, started)
        raise
    _observe(True, started)
    return out


def _observe(ok: bool, started: float) -> None:
    """上报 LLM 指标。指标模块异常不能影响主链路。"""
    try:
        from app_v2.core.metrics import metrics_registry
        metrics_registry.observe_llm(ok, time.perf_counter() - started)
    except Exception:
        pass


def _chat_impl(messages: list[dict], tools: list[dict] | None = None,
               temperature: float | None = None) -> dict:
    client = _client_or_none()
    if client is None:
        raise UpstreamError("未配置大模型（离线模式）")

    last_err: Exception | None = None
    for attempt in range(MAX_RETRY + 1):
        try:
            resp = client.chat.completions.create(
                model=settings.chat_model,
                messages=messages,
                tools=tools or None,
                tool_choice="auto" if tools else None,
                temperature=settings.llm_temperature if temperature is None else temperature,
            )
            msg = resp.choices[0].message
            tool_calls = None
            if getattr(msg, "tool_calls", None):
                tool_calls = [{
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                } for tc in msg.tool_calls]
            usage = getattr(resp, "usage", None)
            return {
                "content": msg.content or "",
                "tool_calls": tool_calls,
                "finish_reason": resp.choices[0].finish_reason,
                "usage": {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                } if usage else {},
            }
        except Exception as exc:
            last_err = exc
            if attempt < MAX_RETRY:
                wait = RETRY_BACKOFF * (attempt + 1)
                logger.warning("LLM 调用失败（第 %d 次），%.1fs 后重试：%s", attempt + 1, wait, exc)
                time.sleep(wait)

    raise UpstreamError(f"大模型调用失败：{last_err}")
