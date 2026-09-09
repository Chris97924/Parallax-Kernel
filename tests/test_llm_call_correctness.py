"""Correctness contract for ``parallax.llm.call`` (audit PA-PARALLAX, 2026-09-09).

Companion to ``tests/test_llm_call.py`` (cache/fallback behaviour) and
``tests/test_llm_call_mutation_harden.py`` (mutation coverage). The properties
here are the six audit findings the module was changed for:

* **F2** — a pinned ``cache_key`` must not replace the payload identity.
* **F3** — an empty response is never written to ``llm_cache``.
* **F4** — a truncated or blocked response is visible (``stop_reason``) and not
  cached.
* **F5** — errors are classified by exception TYPE, and the fallback model is
  reached by every exhausted transient, not only by a 429.
* **F6** — a deterministic config error costs one attempt and no sleep.
* **F7** — a ``claude-*`` model is an unsupported prefix, not a dispatch arm.

No network and no API key: every provider is a stand-in, and the cache is
redirected to ``tmp_path`` so ``~/.parallax/llm_cache.sqlite`` is never touched.
"""

from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import sys
import time
import types as pytypes
from typing import Any

import httpx
import pytest
from tenacity import wait_none

import parallax.llm.call as call_mod
from parallax.llm.call import (
    LLMCallError,
    LLMConfigError,
    LLMPermanentError,
    LLMTransientError,
    RateLimitError,
    _hash_prompt,
    call,
)

MESSAGES = [{"role": "user", "content": "hello"}]


@pytest.fixture()
def isolated_cache(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect the module cache into tmp_path and hand back the DB path."""
    path = tmp_path / "llm_cache.sqlite"
    monkeypatch.setenv("PARALLAX_LLM_CACHE", str(path))
    return path


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record tenacity's backoff instead of taking it, and expose the log.

    ``sleep`` is a documented ``@retry`` constructor argument, so replacing it
    is the supported way to assert "no wait happened" without the test paying
    the module's real 5s floor.
    """
    slept: list[float] = []
    monkeypatch.setattr(
        call_mod._dispatch_with_retry.retry, "sleep", lambda seconds: slept.append(seconds)
    )
    monkeypatch.setattr(call_mod._dispatch_with_retry.retry, "wait", wait_none())
    return slept


def _rows(path: pathlib.Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(path))
    try:
        return list(conn.execute("SELECT model, prompt_hash, response_json FROM llm_cache"))
    finally:
        conn.close()


def _ok(model: str, text: str = "answer", stop_reason: str = "stop") -> dict[str, Any]:
    return {
        "text": text,
        "raw": {},
        "model": model,
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "stop_reason": stop_reason,
    }


# ---------------------------------------------------------------------------
# F2 — a pin names the run; it does not replace the payload
# ---------------------------------------------------------------------------


def test_hash_prompt_with_cache_key_folds_messages_digest() -> None:
    """Same pin + different messages must be a different key.

    Before this change ``_hash_prompt`` returned ``sha256(f"{model}::{cache_key}")``
    and never looked at ``messages`` on the pinned branch, so editing a system
    prompt, re-rendering different evidence, or crossing midnight into a new
    ``Today is …`` all replayed the pre-edit answer under the same pin — with
    ``_cached: True`` and no signal that the prompt change had no effect.

    The three properties below are the whole contract: the payload changes the
    key, an identical payload keeps it stable (or a pin would stop being a
    cache at all), and the model is still part of it (or a Flash answer replays
    for a Pro request under one pin).
    """
    a = [{"role": "user", "content": "A"}]
    b = [{"role": "user", "content": "B"}]

    same_key_diff_messages = {
        _hash_prompt("gemini-2.5-pro", a, None, "run-7"),
        _hash_prompt("gemini-2.5-pro", b, None, "run-7"),
    }
    assert len(same_key_diff_messages) == 2, (
        "a pinned key must not ignore the rendered messages"
    )

    # Same pin + same messages stays one entry, and is reproducible.
    assert _hash_prompt("gemini-2.5-pro", a, None, "run-7") == _hash_prompt(
        "gemini-2.5-pro", a, None, "run-7"
    )

    # The model still selects the answer even under a pin.
    assert _hash_prompt("gemini-2.5-pro", a, None, "run-7") != _hash_prompt(
        "gemini-2.5-flash", a, None, "run-7"
    )

    # The digest is over key-sorted JSON, so the same message written with its
    # keys in the other order is the same message.
    assert _hash_prompt("gemini-2.5-pro", [{"role": "user", "content": "A"}], None, "p") == (
        _hash_prompt("gemini-2.5-pro", [{"content": "A", "role": "user"}], None, "p")
    )


# ---------------------------------------------------------------------------
# F3 — an empty response is never cached
# ---------------------------------------------------------------------------


def test_empty_response_not_cached_and_raises(
    isolated_cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty answer is a failure, not a result worth remembering forever.

    Gemini coerces a blocked / MAX_TOKENS / no-candidate response to ``""``, and
    a reasoning model whose budget went to a hidden ``thinking`` field returns
    the same. Cached, that row is served on every later identical call at no
    cost and with no signal — the 0%-accuracy sweep that survives its own fix.

    Raising (rather than returning) is what gives the provider the two remaining
    tenacity attempts, so a one-off block self-heals instead of being persisted.
    """
    attempts: list[str] = []

    def empty(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        attempts.append(model)
        return _ok(model, text="")

    monkeypatch.setattr(call_mod, "_dispatch", empty)

    with caplog.at_level(logging.WARNING, logger="parallax.llm.call"):
        with pytest.raises(LLMCallError) as exc_info:
            call("gemini-2.5-flash", MESSAGES)

    assert isinstance(exc_info.value, LLMCallError)
    assert len(attempts) == 3, "an empty response must be retried, not accepted"
    assert _rows(isolated_cache) == [], "an empty response must never be cached"

    prompt_hash = _hash_prompt("gemini-2.5-flash", MESSAGES, None, None, 0.0, 2048)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("gemini-2.5-flash" in m and prompt_hash in m for m in warnings), (
        f"expected a WARNING naming the model and the hash, got {warnings!r}"
    )


# ---------------------------------------------------------------------------
# F4 — the stop reason is visible, and a partial answer is not cached
# ---------------------------------------------------------------------------


class _FakeGeminiResp:
    """Minimal stand-in for a ``google.genai`` response."""

    def __init__(self, text: str, finish_reason: object, block_reason: object = None):
        self.text = text
        self.candidates = [pytypes.SimpleNamespace(finish_reason=finish_reason)]
        self.prompt_feedback = pytypes.SimpleNamespace(block_reason=block_reason)
        self.usage_metadata = pytypes.SimpleNamespace(
            prompt_token_count=5, candidates_token_count=3
        )


def _install_fake_genai(monkeypatch: pytest.MonkeyPatch, resp: object) -> None:
    """Inject a ``google.genai`` whose generate_content returns ``resp``."""
    google_mod = pytypes.ModuleType("google")
    genai_mod = pytypes.ModuleType("google.genai")
    types_mod = pytypes.ModuleType("google.genai.types")

    class _Config:
        def __init__(self, **_kw: Any) -> None: ...

    class _Models:
        def generate_content(self, **_kw: Any) -> Any:
            return resp

    class _Client:
        def __init__(self, **_kw: Any) -> None:
            self.models = _Models()

    types_mod.GenerateContentConfig = _Config  # type: ignore[attr-defined]
    genai_mod.Client = _Client  # type: ignore[attr-defined]
    genai_mod.types = types_mod  # type: ignore[attr-defined]
    google_mod.genai = genai_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)


class _FakeOllamaResp:
    def __init__(self, body: dict[str, Any]):
        self.status_code = 200
        self._body = body
        self.text = ""

    def raise_for_status(self) -> None: ...

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.mark.parametrize(
    ("backend", "provider_reason", "expected"),
    [
        ("gemini", "STOP", "stop"),
        ("gemini", "MAX_TOKENS", "length"),
        ("gemini", "SAFETY", "blocked"),
        ("gemini", "SOMETHING_NEW", "unknown"),
        ("gemini", None, "unknown"),
        ("ollama", "stop", "stop"),
        ("ollama", "length", "length"),
        ("ollama", "load", "unknown"),
        ("ollama", None, "unknown"),
    ],
)
def test_stop_reason_normalized_per_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    provider_reason: object,
    expected: str,
) -> None:
    """Every backend reports one of four values at ``result['stop_reason']``.

    Gemini's ``finish_reason`` and Ollama's ``done_reason`` were both captured
    into ``raw`` and read by nothing, so a response cut off at the token ceiling
    was indistinguishable from a complete one at every caller. Normalizing them
    to one vocabulary is what lets ``call()`` — and the extraction/judge callers
    downstream — treat a partial answer as partial.

    An unrecognized reason maps to ``unknown`` rather than optimistically to
    ``stop``: a provider adding a new terminal state must not silently look
    like a clean finish.
    """
    if backend == "gemini":
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        # Enum-valued finish reasons are read via .name, so wrap it like the SDK.
        reason = (
            pytypes.SimpleNamespace(name=provider_reason)
            if provider_reason is not None
            else None
        )
        _install_fake_genai(monkeypatch, _FakeGeminiResp("hi", reason))
        out = call_mod._call_gemini(
            "gemini-2.5-flash", MESSAGES, temperature=0.0, max_output_tokens=8
        )
    else:
        monkeypatch.delenv("PARALLAX_OLLAMA_THINK", raising=False)
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *_a, **_kw: _FakeOllamaResp(
                {
                    "message": {"content": "hi"},
                    "done_reason": provider_reason,
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                }
            ),
        )
        out = call_mod._call_ollama(
            "ollama:qwen3.6:latest", MESSAGES, temperature=0.0, max_output_tokens=8
        )

    assert out["stop_reason"] == expected
    assert out["stop_reason"] in {"stop", "length", "blocked", "unknown"}


def test_length_stop_not_cached_and_warns(
    isolated_cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A truncated answer is returned to the caller but never persisted.

    The extraction prompt asks for a JSON array; cut mid-object it parses as
    invalid, is logged as "found nothing", and the shadow writer reports a
    successful zero-claim write. Caching that makes the truncation permanent and
    free, so the bug survives every re-run — including the one after the token
    budget is raised.
    """
    dispatches: list[str] = []

    def truncated(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        dispatches.append(model)
        return _ok(model, text="[{\"claim\": \"half a", stop_reason="length")

    monkeypatch.setattr(call_mod, "_dispatch", truncated)

    with caplog.at_level(logging.WARNING, logger="parallax.llm.call"):
        first = call("gemini-2.5-flash", MESSAGES)

    assert first["stop_reason"] == "length"
    assert first["_cached"] is False
    assert _rows(isolated_cache) == [], "a length-stopped answer must not be cached"
    assert any(
        "length" in r.getMessage() and "gemini-2.5-flash" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )

    # Not cached means the next identical call re-dispatches rather than
    # replaying the truncation.
    call("gemini-2.5-flash", MESSAGES)
    assert len(dispatches) == 2


def test_blocked_stop_not_cached_and_warns(
    isolated_cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A safety-blocked answer is a provider-side refusal, not a result.

    Same shape as the length case and for the same reason: the response is
    partial, so persisting it replays a refusal forever — including after the
    prompt has been reworded to avoid the trigger.
    """
    monkeypatch.setattr(
        call_mod,
        "_dispatch",
        lambda model, _m, **_kw: _ok(model, text="I can't help with that", stop_reason="blocked"),
    )

    with caplog.at_level(logging.WARNING, logger="parallax.llm.call"):
        result = call("gemini-2.5-flash", MESSAGES)

    assert result["stop_reason"] == "blocked"
    assert _rows(isolated_cache) == [], "a blocked answer must not be cached"
    assert any(
        "blocked" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


# ---------------------------------------------------------------------------
# F5 — classification by exception TYPE
# ---------------------------------------------------------------------------


class _GenaiServerError(Exception):
    """Stands in for ``google.genai.errors.ServerError`` (module path matters)."""

    __module__ = "google.genai.errors"

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # google.genai: the SDK's own class + its numeric .code, no text probe.
        (_GenaiServerError("backend overwhelmed", 503), LLMTransientError),
        (_GenaiServerError("internal", 500), LLMTransientError),
        (_GenaiServerError("quota", 429), RateLimitError),
        # httpx transport classes.
        (httpx.TimeoutException("read timed out"), LLMTransientError),
        (httpx.ConnectError("connection refused"), LLMTransientError),
        # A message with no status and no known type is NOT transient.
        (RuntimeError("malformed provider payload"), LLMCallError),
    ],
)
def test_transient_classified_by_exception_type(
    exc: Exception, expected: type[Exception]
) -> None:
    """The TYPE decides, and the type is what reaches ``fallback_model``.

    Classifying on ``str(exc)`` alone cuts both ways: a request id or a token
    count containing ``429`` is misread as a rate limit, while a genuine
    ``ServerError`` whose prose says only "backend overwhelmed" is misread as a
    permanent failure and never reaches the fallback the caller configured.

    The last assertion is the point of the distinction — everything classified
    transient here (and ``RateLimitError``, which stays 429-only) is what
    ``call()`` catches to fall back on, while an unclassifiable error is not.
    """
    classified = call_mod._classify_provider_error(exc)

    assert type(classified) is expected
    assert isinstance(classified, (RateLimitError, LLMTransientError)) is (
        expected is not LLMCallError
    )


def test_transient_classified_by_exception_type_for_ollama_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ollama's own HTTP statuses: 429 is a rate limit, every 5xx is transient.

    ``_call_ollama`` reads the status off the response rather than off the
    message, so this is the same type-first rule expressed at the transport
    layer. Both classes reach the fallback branch in ``call()``; a 4xx that is
    not 429 is a ``LLMPermanentError`` (r3) — retrying a malformed request only
    wastes the budget, and falling back cannot make it well formed either.
    """

    class _Resp:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.text = "boom"

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError("err", request=None, response=None)

        def json(self) -> dict[str, Any]:  # pragma: no cover - never reached
            return {}

    monkeypatch.delenv("PARALLAX_OLLAMA_THINK", raising=False)

    for status, expected in (
        (429, RateLimitError),
        (500, LLMTransientError),
        (502, LLMTransientError),
        (503, LLMTransientError),
        (504, LLMTransientError),
        (400, LLMPermanentError),
    ):
        monkeypatch.setattr(httpx, "post", lambda *_a, _s=status, **_kw: _Resp(_s))
        with pytest.raises(expected) as exc_info:
            call_mod._call_ollama(
                "ollama:qwen3.6:latest", MESSAGES, temperature=0.0, max_output_tokens=8
            )
        assert type(exc_info.value) is expected, f"status {status}"

    # Last resort only: an opaque type carrying the status in its text.
    assert type(call_mod._classify_provider_error(RuntimeError("HTTP 429"))) is RateLimitError


@pytest.mark.parametrize(
    "primary_error",
    [
        RateLimitError("429 quota"),
        LLMTransientError("500 internal"),
        LLMTransientError("502 bad gateway"),
        LLMTransientError("503 UNAVAILABLE"),
        LLMTransientError("529 overloaded"),
        LLMTransientError("read timed out"),
    ],
    ids=["429", "500", "502", "503", "529", "timeout"],
)
def test_fallback_fires_after_retries_for_transient_and_rate_limit(
    isolated_cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: list[float],
    primary_error: Exception,
) -> None:
    """Every exhausted capacity failure reaches the fallback — not just 429.

    An evening Pro capacity event returns 503/overloaded rather than 429, so
    before this change a run with a perfectly good ``fallback_model="…flash"``
    degraded to mostly-ERROR instead of degrading to Flash.

    "After the retries are exhausted" is asserted, not assumed: the primary is
    attempted the full three times BEFORE the fallback is tried once, so a
    transient blip is still absorbed by a retry on the primary model rather than
    silently demoting the run to the cheaper model on the first hiccup.
    """
    attempts = {"primary": 0, "fallback": 0}

    def flaky(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        if model == "gemini-2.5-pro":
            attempts["primary"] += 1
            raise primary_error
        attempts["fallback"] += 1
        return _ok(model, text="flash-answer")

    monkeypatch.setattr(call_mod, "_dispatch", flaky)

    result = call("gemini-2.5-pro", MESSAGES, fallback_model="gemini-2.5-flash")

    assert result["text"] == "flash-answer"
    assert result["fallback_from"] == "gemini-2.5-pro"
    assert attempts["primary"] == 3, "the fallback must wait for the retry budget"
    assert attempts["fallback"] == 1


def test_fallback_not_for_config_error(
    isolated_cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: list[float],
) -> None:
    """A misconfiguration must surface, not be papered over by the fallback.

    Falling back on a missing key or a typo'd model id turns "your run is
    misconfigured" into "your run quietly used the cheaper model" — the results
    look fine and the model column in the report is the only trace.
    """
    attempts: list[str] = []

    def broken(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        attempts.append(model)
        raise LLMConfigError("no Gemini API key configured")

    monkeypatch.setattr(call_mod, "_dispatch", broken)

    with pytest.raises(LLMConfigError):
        call("gemini-2.5-pro", MESSAGES, fallback_model="gemini-2.5-flash")

    assert attempts == ["gemini-2.5-pro"], "the fallback model must not be tried"
    assert no_sleep == []


# ---------------------------------------------------------------------------
# F6 — config errors are not retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    ["missing_gemini_key", "sdk_not_importable", "unsupported_prefix", "claude_prefix"],
)
def test_config_errors_not_retried(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], scenario: str
) -> None:
    """One attempt, no sleep, and a message that names its own fix.

    ``LLMCallError`` is documented as "fails in a non-retryable way" yet was in
    the retry predicate, so every deterministic misconfiguration cost three
    attempts and two 5s waits. On a 500-question run that is hours of sleeping
    before a typo'd model name surfaces — and, because dispatch happens under
    ``_db_lock``, those sleeps stall every other worker's cache reads too.
    """
    for env in call_mod._GEMINI_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)

    if scenario == "missing_gemini_key":
        model = "gemini-2.5-flash"
    elif scenario == "sdk_not_importable":
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setitem(sys.modules, "google", None)
        model = "gemini-2.5-flash"
    elif scenario == "unsupported_prefix":
        model = "mistral-7b"
    else:
        model = "claude-sonnet-4-5"

    with pytest.raises(LLMConfigError) as exc_info:
        call_mod._dispatch_with_retry(
            model, MESSAGES, temperature=0.0, max_output_tokens=8
        )

    assert no_sleep == [], "a config error must not pay a backoff"

    message = str(exc_info.value)
    if scenario in {"unsupported_prefix", "claude_prefix"}:
        assert "unsupported model prefix" in message
        # The message has to name the closed set, or the operator's next step is
        # to guess; `claude-` in particular used to LOOK supported.
        for prefix in ("gemini-", "ollama:", "local:"):
            assert prefix in message, f"{prefix!r} missing from {message!r}"
        assert "claude" not in message.replace(model, ""), (
            "claude must appear only as the rejected input, never as a supported prefix"
        )


def test_config_errors_not_retried_counts_one_attempt(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    """The attempt count is the other half of "not retried".

    ``no_sleep == []`` alone would also hold for a policy that retried three
    times with a zero wait, so the dispatch count is asserted directly.
    """
    attempts: list[str] = []

    def broken(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        attempts.append(model)
        raise LLMConfigError("google-genai SDK not importable: no module named google")

    monkeypatch.setattr(call_mod, "_dispatch", broken)

    with pytest.raises(LLMConfigError):
        call_mod._dispatch_with_retry(
            "gemini-2.5-flash", MESSAGES, temperature=0.0, max_output_tokens=8
        )

    assert len(attempts) == 1
    assert no_sleep == []


def test_exception_hierarchy() -> None:
    """``except LLMCallError`` must keep catching everything it used to.

    The split exists to let the retry predicate and the fallback branch tell the
    three cases apart; it must not silently narrow what existing callers catch.
    ``RateLimitError`` deliberately stays outside the hierarchy — it is the
    module's public 429 signal and several callers match it by exact type.
    """
    assert issubclass(LLMConfigError, LLMCallError)
    assert issubclass(LLMTransientError, LLMCallError)
    assert LLMConfigError is not LLMTransientError
    assert not issubclass(LLMConfigError, LLMTransientError)
    assert not issubclass(LLMTransientError, LLMConfigError)

    # RateLimitError unchanged: a RuntimeError, not an LLMCallError.
    assert issubclass(RateLimitError, RuntimeError)
    assert not issubclass(RateLimitError, LLMCallError)

    # The retry predicate is the behavioural consequence of the hierarchy.
    assert call_mod._is_retryable(LLMTransientError("503")) is True
    assert call_mod._is_retryable(RateLimitError("429")) is True
    assert call_mod._is_retryable(LLMCallError("boom")) is True
    assert call_mod._is_retryable(LLMConfigError("no key")) is False
    assert call_mod._is_retryable(ValueError("bug")) is False

    # r3: the provider's own refusal joins the non-retryable side, without
    # becoming a LLMConfigError — callers matching that class mean "MY env is
    # wrong", which is not what a 404 for an unknown model says.
    assert issubclass(LLMPermanentError, LLMCallError)
    assert not issubclass(LLMPermanentError, LLMConfigError)
    assert not issubclass(LLMConfigError, LLMPermanentError)
    assert call_mod._is_retryable(LLMPermanentError("404 unknown model")) is False


# ---------------------------------------------------------------------------
# F3 + F4 + F9 — what actually lands in the cache
# ---------------------------------------------------------------------------


def test_stop_reason_is_persisted_and_replayed(
    isolated_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cacheable answer keeps its stop reason on the way back out.

    Callers read ``result['stop_reason']`` uniformly, so a cache hit that
    dropped it would make a replayed answer look different from the live one it
    replaces — the kind of asymmetry that turns a caching layer into a source
    of behaviour changes.
    """
    monkeypatch.setattr(call_mod, "_dispatch", lambda model, _m, **_kw: _ok(model))

    live = call("gemini-2.5-flash", MESSAGES)
    replay = call("gemini-2.5-flash", MESSAGES)

    assert live["_cached"] is False
    assert replay["_cached"] is True
    assert live["stop_reason"] == replay["stop_reason"] == "stop"
    stored = json.loads(_rows(isolated_cache)[0][2])
    assert stored["stop_reason"] == "stop"
    assert "_cached" not in stored


# ---------------------------------------------------------------------------
# r2 — the four minors the independent verifier raised against round 1
# ---------------------------------------------------------------------------


class _OllamaStatusResponse:
    """Minimal ``httpx.Response`` stand-in carrying only a status and a body."""

    def __init__(self, status: int) -> None:
        self.status_code = status
        self.text = "boom"

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self) -> dict[str, Any]:  # pragma: no cover - never reached
        return {}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (500, LLMTransientError),
        (501, LLMTransientError),
        (502, LLMTransientError),
        (503, LLMTransientError),
        (504, LLMTransientError),
        (505, LLMTransientError),
        (507, LLMTransientError),
        (529, LLMTransientError),
        (400, LLMPermanentError),
    ],
)
def test_all_5xx_statuses_are_transient(
    status: int, expected: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every 5xx reaches the fallback, not just the five anyone thought to list.

    Round 1 classified transience off a closed set {408, 425, 500, 502, 503,
    504, 529}. A 501, 505, 507, 508 or 511 fell through to a bare
    ``LLMCallError``: tenacity still retried it, so the failure looked handled,
    but ``call()`` catches only ``RateLimitError`` and ``LLMTransientError``, so
    the configured ``fallback_model`` was structurally unreachable for those
    codes. The parametrization walks the raw status through ``_call_ollama``'s
    real classification rather than asserting set membership, so a refactor that
    reintroduces an enumeration fails here.

    529 is included because round 1 never exercised it through the status path
    at all — its fallback test injected an already-constructed
    ``LLMTransientError``, so nothing walked raw status -> classification.
    400 is the control: a malformed request is not fixed by waiting, so it
    classifies as ``LLMPermanentError`` (r3) and reaches neither the retry nor
    the fallback.
    """
    monkeypatch.delenv("PARALLAX_OLLAMA_THINK", raising=False)
    monkeypatch.setattr(
        httpx, "post", lambda *_a, _s=status, **_kw: _OllamaStatusResponse(_s)
    )

    with pytest.raises(expected) as exc_info:
        call_mod._call_ollama(
            "ollama:qwen3.6:latest", MESSAGES, temperature=0.0, max_output_tokens=8
        )

    assert type(exc_info.value) is expected, f"status {status}"
    # The consequence, not just the class: only a transient reaches the fallback.
    assert isinstance(exc_info.value, (RateLimitError, LLMTransientError)) is (
        expected is LLMTransientError
    )
    assert call_mod._is_transient_status(status) is (expected is LLMTransientError)


@pytest.mark.parametrize(
    ("message", "is_rate_limit"),
    [
        # Digits that merely CONTAIN 429 -- a request id, a longer number, a
        # token count. None of these is a status.
        ("provider failed on req-4290", False),
        ("sequence 14291 rejected", False),
        ("budget exceeded: tokens=4290", False),
        # The shapes a provider actually uses to state the status.
        ("HTTP 429 Too Many Requests", True),
        ("upstream returned status 429", True),
        ("rate_limit_error: slow down", True),
    ],
)
def test_rate_limit_probe_is_word_bounded(message: str, is_rate_limit: bool) -> None:
    """The last-resort text probe must not read digits out of a request id.

    ``_classify_provider_error`` word-bounds the transient status codes for
    exactly this reason, but round 1 left the rate-limit fallback as a bare
    ``429`` substring test. An opaque provider exception whose message carried a
    request id like ``req-4290`` was therefore classified ``RateLimitError``,
    which is not merely a mislabel: it makes a permanent failure look like a
    capacity event, so it is retried three times and then answered by the
    fallback model instead of surfacing.

    Only the last-resort path is exercised here -- a bare ``RuntimeError``
    carries no status attribute and no known SDK type, so classification falls
    all the way through to the text.
    """
    classified = call_mod._classify_provider_error(RuntimeError(message))

    assert isinstance(classified, RateLimitError) is is_rate_limit, message
    if not is_rate_limit:
        # It must not be laundered into the other retry class either.
        assert type(classified) is LLMCallError


def test_legacy_cache_row_replays_with_stop_reason_unknown(
    isolated_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row written before ``stop_reason`` existed still replays with the key.

    ``call()`` documents ``stop_reason`` as always present, and every row
    written from now on carries it -- but ``~/.parallax/llm_cache.sqlite`` is a
    persistent file full of rows written before this change. Handing those back
    unchanged makes the contract true only for a cache that has been wiped, so
    the first consumer to subscript ``result['stop_reason']`` would KeyError on
    a replay and not on a live call. The row is seeded directly through SQLite
    so the test cannot be satisfied by anything the write path does.
    """
    conn = call_mod._connect_cache()
    try:
        prompt_hash = _hash_prompt("gemini-2.5-flash", MESSAGES, None, None, 0.0, 2048)
        legacy = {
            "text": "an answer from before stop_reason existed",
            "raw": {},
            "model": "gemini-2.5-flash",
            "prompt_tokens": 11,
            "completion_tokens": 22,
        }
        assert "stop_reason" not in legacy
        conn.execute(
            "INSERT INTO llm_cache (model, prompt_hash, response_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "gemini-2.5-flash",
                prompt_hash,
                json.dumps(legacy),
                "2026-01-01T00:00:00",
            ),
        )
    finally:
        conn.close()

    def never(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        raise AssertionError(f"cache miss: {model} was dispatched")

    monkeypatch.setattr(call_mod, "_dispatch", never)

    result = call("gemini-2.5-flash", MESSAGES)

    assert result["_cached"] is True
    assert result["stop_reason"] == "unknown"
    assert result["text"] == "an answer from before stop_reason existed"
    assert result["prompt_tokens"] == 11
    # Normalizing the replay must not rewrite the stored row.
    stored = json.loads(_rows(isolated_cache)[0][2])
    assert "stop_reason" not in stored


@pytest.mark.parametrize("stop_reason", ["blocked", "length"])
def test_blocked_or_length_stop_with_empty_text_names_stop_reason(
    stop_reason: str,
    isolated_cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The COMMON blocked case is empty text, and it must say so.

    A prompt-level Gemini block returns no candidate, so ``text`` is empty; a
    budget eaten by a hidden reasoning field returns ``length`` the same way.
    Both therefore hit the empty-text guard BEFORE ``call()`` ever evaluates its
    stop-reason branch, which meant round 1's operator-visible signal for the
    single most common real block was the generic "empty response text" --
    while the branch that names the reason was only reachable for the rarer
    blocked-response-that-still-carries-text shape.

    Safety is unchanged either way (nothing is cached); what this asserts is
    that the reason survives into the message and the WARNING, so an operator
    reading a log can tell a refusal from a broken decode.
    """
    monkeypatch.setattr(
        call_mod,
        "_dispatch",
        lambda model, _m, **_kw: _ok(model, text="", stop_reason=stop_reason),
    )

    with caplog.at_level(logging.WARNING, logger="parallax.llm.call"):
        with pytest.raises(LLMCallError) as exc_info:
            call("gemini-2.5-flash", MESSAGES)

    assert stop_reason in str(exc_info.value), (
        f"the exception must name the stop reason, got {exc_info.value!s}"
    )
    assert "gemini-2.5-flash" in str(exc_info.value)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(stop_reason in m and "gemini-2.5-flash" in m for m in warnings), (
        f"expected a WARNING naming the stop reason, got {warnings!r}"
    )
    assert _rows(isolated_cache) == [], "an empty blocked/length answer is not cached"


# ---------------------------------------------------------------------------
# r3 — a non-429 4xx is the provider refusing the REQUEST, not a blip
# ---------------------------------------------------------------------------


class _ProviderStatusError(Exception):
    """Opaque provider exception carrying only an HTTP status, as SDKs do."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status_code = status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_4xx_not_retried(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid key, an unknown model or a malformed body costs ONE attempt.

    Round 2 classified every non-429 4xx as a bare ``LLMCallError``, and the
    retry predicate excluded only ``LLMConfigError`` — so a 401 from a rotated
    key, or a 404 for a model tag that was never pulled on GB10, was dispatched
    three times with two 5s sleeps between them before surfacing. None of those
    requests can recover with backoff: the server read the request and rejected
    it, so the second and third attempts are the identical request collecting
    the identical refusal.

    Both provider paths are walked from the raw status rather than from an
    already-constructed exception (Gemini's classifier, Ollama's response), and
    the retry POLICY is exercised through ``_dispatch_with_retry`` with the real
    ``time.sleep`` monkeypatched — so a regression that reinstates the retry
    fails on the sleep log instead of quietly costing 10s per call.
    """
    # Gemini path: the SDK exception's status is authoritative.
    classified = call_mod._classify_provider_error(_ProviderStatusError("nope", status))
    assert type(classified) is LLMPermanentError

    # Ollama path: the status is read off the response.
    monkeypatch.delenv("PARALLAX_OLLAMA_THINK", raising=False)
    monkeypatch.setattr(
        httpx, "post", lambda *_a, _s=status, **_kw: _OllamaStatusResponse(_s)
    )
    with pytest.raises(LLMPermanentError):
        call_mod._call_ollama(
            "ollama:qwen3.6:latest", MESSAGES, temperature=0.0, max_output_tokens=8
        )

    # The consequence: one attempt, no sleep.
    slept: list[float] = []
    attempts: list[str] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))

    def refused(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        attempts.append(model)
        raise call_mod._classify_provider_error(_ProviderStatusError("nope", status))

    monkeypatch.setattr(call_mod, "_dispatch", refused)

    with pytest.raises(LLMPermanentError):
        call_mod._dispatch_with_retry(
            "gemini-2.5-flash", MESSAGES, temperature=0.0, max_output_tokens=8
        )

    assert len(attempts) == 1, f"HTTP {status} must not be retried"
    assert slept == [], f"HTTP {status} must not sleep before failing"
    assert call_mod._is_retryable(classified) is False


def test_permanent_4xx_not_fallback(
    isolated_cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: list[float],
) -> None:
    """A refused request must not be re-issued against ``fallback_model``.

    The fallback exists to survive a capacity event on the primary model. A
    401/403 is the same API key the fallback would present, and a 400 is the
    same malformed body — so falling back cannot succeed; it can only double
    the cost of the failure and, worse, hide a broken configuration behind a
    quietly-degraded run whose answers all came from the cheaper model.

    Asserted on the two things that would show it: the fallback model is never
    dispatched, and the permanent error is what escapes ``call()``.
    """
    attempts = {"primary": 0, "fallback": 0}

    def refused(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        if model == "gemini-2.5-pro":
            attempts["primary"] += 1
            raise LLMPermanentError("gemini HTTP 404: model not found")
        attempts["fallback"] += 1
        return _ok(model)

    monkeypatch.setattr(call_mod, "_dispatch", refused)

    with pytest.raises(LLMPermanentError):
        call("gemini-2.5-pro", MESSAGES, fallback_model="gemini-2.5-flash")

    assert attempts == {"primary": 1, "fallback": 0}
    assert no_sleep == []
    assert _rows(isolated_cache) == [], "a refused request caches nothing"
