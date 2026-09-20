"""Thin wrapper around the Claude API -- every agent-loop phase calls
through this, so the client, model default, and usage extraction are
defined once.

No hand-rolled retry loop: the installed `anthropic` SDK (1.6.0) already
retries on transient failures (429/5xx/connection errors, honoring the
server's `x-should-retry` header) via its own `max_retries` -- verified by
reading anthropic._base_client's retry logic rather than assumed. Adding a
second retry layer on top would just double the backoff for no benefit, so
this only configures max_retries on the client.
"""
from __future__ import annotations

from anthropic import AsyncAnthropic
from anthropic.types import Message

from agent.config import settings

DEFAULT_MODEL = "claude-sonnet-5"

_client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=4)


async def call_claude(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2048,
) -> Message:
    """One Claude API call. `tools`/`tool_choice` are the Claude tool-calling
    shapes from agent.tools.registry.to_claude_tools() and, for a forced
    single-tool response, {"type": "tool", "name": ...}.
    """
    kwargs: dict = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    return await _client.messages.create(**kwargs)


def usage_dict(message: Message) -> dict:
    """Extract token counts from a response for observability logging."""
    u = message.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_input_tokens,
        "cache_write_tokens": u.cache_creation_input_tokens,
    }
