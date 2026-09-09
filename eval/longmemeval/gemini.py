"""Shim that routes all LongMemEval Gemini traffic through parallax.llm.call.

Preserves the historical :class:`GeminiResult` / :func:`call` surface used by
``pipeline.py`` and friends, but the underlying transport, retry, caching, and
rate-limit handling live in :func:`parallax.llm.call.call`. No module outside
``parallax/llm/call.py`` should touch ``google.genai`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from parallax.llm.call import call as _unified_call


@dataclass(frozen=True)
class GeminiResult:
    text: str
    prompt_tokens: int
    output_tokens: int
    model: str
    #: True when :func:`parallax.llm.call.call` served this from ``llm_cache``
    #: instead of a provider. Without it every replay is counted as spend and
    #: the eval line's cost numbers are "tokens the prompt WOULD cost", not
    #: tokens actually billed (PA-PARALLAX-F9).
    cached: bool = False


def call(
    *,
    model: str,
    user: str,
    system: str | None = None,
    temperature: float = 0.1,
    max_output_tokens: int = 2048,
) -> GeminiResult:
    """Call a Gemini model via the unified parallax.llm.call pipeline."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    result = _unified_call(
        model,
        messages,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    return GeminiResult(
        text=result.get("text", "") or "",
        prompt_tokens=int(result.get("prompt_tokens", 0) or 0),
        output_tokens=int(result.get("completion_tokens", 0) or 0),
        model=result.get("model", model),
        cached=bool(result.get("_cached", False)),
    )
