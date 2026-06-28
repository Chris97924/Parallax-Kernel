"""Tests for parallax.llm.call — cache behavior and fallback model."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import parallax.llm.call as call_module
from parallax.llm.call import (
    LLMCallError,
    RateLimitError,
    _call_gemini,
    _gemini_keys,
    _next_gemini_key,
    call,
)


@pytest.fixture
def clean_gemini_env(monkeypatch):
    """Unset every Gemini/Google key env var so each test starts from an empty
    pool and only sees the keys it explicitly sets."""
    for env in call_module._GEMINI_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    yield monkeypatch


def test_gemini_keys_dedupes_and_preserves_first_seen_order(clean_gemini_env):
    mp = clean_gemini_env
    # GOOGLE_API_KEY duplicates GEMINI_API_KEY; GEMINI_API_KEY_2 duplicates it
    # again at a later position; GEMINI_API_KEY_3 is empty and must be skipped.
    mp.setenv("GEMINI_API_KEY", "key-a")
    mp.setenv("GOOGLE_API_KEY", "key-b")
    mp.setenv("GEMINI_API_KEY_2", "key-a")  # dup of first -> dropped
    mp.setenv("GEMINI_API_KEY_3", "")  # empty -> skipped

    keys = _gemini_keys()

    # First-seen order preserved, the later duplicate of "key-a" is dropped,
    # and the empty value never enters the pool.
    assert keys == ["key-a", "key-b"]


def test_gemini_keys_skips_unset(clean_gemini_env):
    mp = clean_gemini_env
    # Only the third slot is set; the other three are unset.
    mp.setenv("GEMINI_API_KEY_2", "only-key")

    assert _gemini_keys() == ["only-key"]


def test_gemini_keys_empty_when_nothing_configured(clean_gemini_env):
    assert _gemini_keys() == []


def test_next_gemini_key_round_robins(clean_gemini_env):
    mp = clean_gemini_env
    mp.setenv("GEMINI_API_KEY", "k0")
    mp.setenv("GOOGLE_API_KEY", "k1")
    # Reset the module-global rotation cursor for a deterministic sequence.
    mp.setattr(call_module, "_key_idx", 0)

    pool = _gemini_keys()
    assert pool == ["k0", "k1"]

    # Two keys -> successive calls cycle k0, k1, k0, k1, ...
    seq = [_next_gemini_key() for _ in range(5)]
    assert seq == ["k0", "k1", "k0", "k1", "k0"]


def test_next_gemini_key_raises_when_pool_empty(clean_gemini_env):
    mp = clean_gemini_env
    # Pool is empty (clean_gemini_env unset everything); cursor reset so the
    # failure is the guard, not an arithmetic accident.
    mp.setattr(call_module, "_key_idx", 0)

    with pytest.raises(LLMCallError, match="no Gemini API key configured"):
        _next_gemini_key()


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PARALLAX_LLM_CACHE", str(tmp_path / "cache.sqlite"))
    yield


def test_cache_miss_then_hit(isolated_cache, monkeypatch):
    calls: list[tuple] = []

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        calls.append((model, tuple(m["content"] for m in messages)))
        return {
            "text": "hello",
            "raw": {},
            "model": model,
            "prompt_tokens": 3,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_dispatch_with_retry", fake_dispatch)

    msgs = [{"role": "user", "content": "hi"}]
    first = call("gemini-2.5-flash", msgs)
    assert first["_cached"] is False
    assert first["text"] == "hello"

    second = call("gemini-2.5-flash", msgs)
    assert second["_cached"] is True
    assert second["text"] == "hello"
    assert len(calls) == 1, "dispatcher should have been called exactly once"


def test_cache_key_override(isolated_cache, monkeypatch):
    call_count = {"n": 0}

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        call_count["n"] += 1
        return {
            "text": "same-key",
            "raw": {},
            "model": model,
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_dispatch_with_retry", fake_dispatch)

    a = call("gemini-2.5-flash", [{"role": "user", "content": "A"}], cache_key="k1")
    b = call("gemini-2.5-flash", [{"role": "user", "content": "B"}], cache_key="k1")
    assert a["_cached"] is False
    assert b["_cached"] is True
    assert call_count["n"] == 1


def test_fallback_on_rate_limit(isolated_cache, monkeypatch):
    call_count = {"primary": 0, "fallback": 0}

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        if model == "gemini-2.5-pro":
            call_count["primary"] += 1
            raise RateLimitError("429 simulated")
        call_count["fallback"] += 1
        return {
            "text": "ok",
            "raw": {},
            "model": model,
            "prompt_tokens": 2,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_dispatch_with_retry", fake_dispatch)

    result = call(
        "gemini-2.5-pro",
        [{"role": "user", "content": "x"}],
        fallback_model="gemini-2.5-flash",
    )
    assert result["text"] == "ok"
    assert result["model"] == "gemini-2.5-flash"
    assert result["fallback_from"] == "gemini-2.5-pro"
    assert call_count["primary"] == 1
    assert call_count["fallback"] == 1


def test_concurrent_calls_dedupe(isolated_cache, monkeypatch):
    """Four threads hitting the same cache_key must share one dispatch."""
    dispatches = {"n": 0}
    gate = threading.Event()

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        dispatches["n"] += 1
        # Make the first dispatch slow enough that the other threads have a
        # chance to race. Without the _db_lock fix, they would each dispatch.
        gate.wait(timeout=0.5)
        return {
            "text": "shared",
            "raw": {},
            "model": model,
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_dispatch_with_retry", fake_dispatch)

    msgs = [{"role": "user", "content": "same"}]

    def _one():
        return call("gemini-2.5-flash", msgs, cache_key="shared-key")

    # Release the gate after threads are all queued at the DB lock.
    def _release_soon():
        threading.Event().wait(0.05)
        gate.set()

    releaser = threading.Thread(target=_release_soon)
    releaser.start()
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda _: _one(), range(4)))
    releaser.join()

    assert dispatches["n"] == 1, (
        f"expected exactly one dispatch, got {dispatches['n']}"
    )
    assert all(r["text"] == "shared" for r in results)
    # At least one must see a live dispatch, the others must be cache hits.
    assert sum(1 for r in results if r["_cached"]) >= 3


def test_fallback_not_cached_under_primary_key(isolated_cache, monkeypatch):
    """Primary 429 → fallback success must NOT leave a row under primary's hash."""
    call_seq = {"primary": 0, "fallback": 0}

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        if model == "gemini-2.5-pro":
            call_seq["primary"] += 1
            raise RateLimitError("429")
        call_seq["fallback"] += 1
        return {
            "text": "flash-answer",
            "raw": {},
            "model": model,
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_dispatch_with_retry", fake_dispatch)

    msgs = [{"role": "user", "content": "x"}]

    first = call(
        "gemini-2.5-pro",
        msgs,
        fallback_model="gemini-2.5-flash",
    )
    assert first["text"] == "flash-answer"
    assert first["fallback_from"] == "gemini-2.5-pro"

    # Now "primary quota returns": next call to the primary model must NOT
    # be served from the cached fallback answer.
    def fake_dispatch_ok(model, messages, *, temperature, max_output_tokens):
        call_seq["primary"] += 1
        return {
            "text": "pro-answer",
            "raw": {},
            "model": model,
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_dispatch_with_retry", fake_dispatch_ok)

    second = call("gemini-2.5-pro", msgs)
    assert second["text"] == "pro-answer", (
        "primary-model call was served from fallback-model cache — pollution bug"
    )
    assert second.get("_cached") is False


def test_call_gemini_missing_sdk_message(monkeypatch):
    """Absent google-genai SDK must surface the contract LLMCallError message.

    The ``google-genai`` SDK is an *optional* extra (``parallax-kernel[llm]``),
    so the default test gate must not require it. We force ``from google import
    genai`` to fail deterministically — regardless of whether the SDK happens to
    be installed — by poisoning ``sys.modules['google']`` with ``None``, then
    assert the error message that callers depend on stays intact.
    """
    monkeypatch.setitem(sys.modules, "google", None)

    with pytest.raises(LLMCallError) as exc_info:
        _call_gemini(
            "gemini-2.5-flash",
            [{"role": "user", "content": "x"}],
            temperature=0.0,
            max_output_tokens=8,
        )

    assert str(exc_info.value).startswith("google-genai SDK not importable:")


def test_ratelimit_retry_before_fallback(isolated_cache, monkeypatch):
    """RateLimitError is retried inside tenacity; fallback only if retries exhaust."""
    attempts = {"n": 0, "fallback": 0}

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        if model == "gemini-2.5-pro":
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RateLimitError("429 transient")
            return {
                "text": "pro-ok",
                "raw": {},
                "model": model,
                "prompt_tokens": 1,
                "completion_tokens": 1,
            }
        attempts["fallback"] += 1
        return {
            "text": "flash",
            "raw": {},
            "model": model,
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    # Bypass the tenacity-wrapped dispatcher by monkey-patching the *inner*
    # _dispatch, so retry behaviour from the real decorator is exercised.
    monkeypatch.setattr(call_module, "_dispatch", fake_dispatch)
    # Collapse the exponential wait so the test doesn't sleep 5s+.
    from tenacity import stop_after_attempt, wait_none

    monkeypatch.setattr(
        call_module._dispatch_with_retry.retry,
        "wait",
        wait_none(),
    )
    monkeypatch.setattr(
        call_module._dispatch_with_retry.retry,
        "stop",
        stop_after_attempt(3),
    )

    result = call(
        "gemini-2.5-pro",
        [{"role": "user", "content": "x"}],
        fallback_model="gemini-2.5-flash",
    )
    assert result["text"] == "pro-ok"
    assert attempts["n"] == 3
    assert attempts["fallback"] == 0, "fallback fired despite retry success"
