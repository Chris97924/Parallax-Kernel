"""Tests for parallax.llm.call — cache behavior and fallback model."""

from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import parallax.llm.call as call_module
from parallax.llm.call import (
    LLMCallError,
    RateLimitError,
    _call_gemini,
    _call_ollama,
    _gemini_keys,
    _hash_prompt,
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


# ---------- Ollama (local) provider -----------------------------------------


class _FakeOllamaResp:
    """Minimal stand-in for an httpx.Response (no network)."""

    def __init__(self, status_code: int, json_body: dict | None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def raise_for_status(self) -> None:
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )

    def json(self) -> dict:
        if self._json is None:
            raise ValueError("no json body")
        return self._json


@pytest.fixture
def clean_ollama_env(monkeypatch):
    """Force the default GB10 base URL by clearing both override env vars."""
    monkeypatch.delenv("PARALLAX_OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("PARALLAX_OLLAMA_TIMEOUT", raising=False)
    monkeypatch.delenv("PARALLAX_OLLAMA_THINK", raising=False)
    yield monkeypatch


def test_call_ollama_strips_prefix_and_maps_tokens(clean_ollama_env):
    import httpx

    captured: dict = {}

    def fake_post(url, *, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeOllamaResp(
            200,
            {
                "model": "qwen3.6:latest",
                "message": {"role": "assistant", "content": "hi there"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 42,
                "eval_count": 7,
            },
        )

    clean_ollama_env.setattr(httpx, "post", fake_post)

    out = _call_ollama(
        "ollama:qwen3.6:latest",
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
        temperature=0.0,
        max_output_tokens=128,
    )

    # Prefix stripped on the wire; only the trailing model tag is sent — note
    # the model tag itself contains a colon, so only the routing prefix is cut.
    assert captured["json"]["model"] == "qwen3.6:latest"
    # Original prefixed name is preserved in the return for stable cache keys.
    assert out["model"] == "ollama:qwen3.6:latest"
    # Endpoint + default GB10 base URL.
    assert captured["url"] == "http://192.168.1.134:11434/api/chat"
    # Options + stream mapping.
    assert captured["json"]["options"]["num_predict"] == 128
    assert captured["json"]["options"]["temperature"] == 0.0
    assert captured["json"]["stream"] is False
    # System/user roles map natively to /api/chat messages.
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
    ]
    # Token mapping: prompt_eval_count -> prompt_tokens, eval_count -> completion.
    assert out["text"] == "hi there"
    assert out["prompt_tokens"] == 42
    assert out["completion_tokens"] == 7


def test_call_ollama_respects_base_url_override(monkeypatch):
    import httpx

    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("PARALLAX_OLLAMA_BASE_URL", "http://gb10.local:11434/")
    captured: dict = {}

    def fake_post(url, *, json, timeout):
        captured["url"] = url
        return _FakeOllamaResp(
            200, {"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1}
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    _call_ollama(
        "local:gemma4:31b",
        [{"role": "user", "content": "q"}],
        temperature=0.2,
        max_output_tokens=8,
    )
    # Trailing slash trimmed; override wins over the default.
    assert captured["url"] == "http://gb10.local:11434/api/chat"


def test_call_ollama_think_env_controls_payload(clean_ollama_env):
    import httpx

    captured: dict = {}

    def fake_post(url, *, json, timeout):
        captured["json"] = json
        return _FakeOllamaResp(
            200, {"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1}
        )

    clean_ollama_env.setattr(httpx, "post", fake_post)

    msgs = [{"role": "user", "content": "q"}]

    # Unset -> no think key (model default preserved).
    _call_ollama("ollama:qwen3.6:latest", msgs, temperature=0.0, max_output_tokens=8)
    assert "think" not in captured["json"]

    # Falsey -> think=false (reasoning suppressed).
    clean_ollama_env.setenv("PARALLAX_OLLAMA_THINK", "0")
    _call_ollama("ollama:qwen3.6:latest", msgs, temperature=0.0, max_output_tokens=8)
    assert captured["json"]["think"] is False

    # Truthy -> think=true.
    clean_ollama_env.setenv("PARALLAX_OLLAMA_THINK", "true")
    _call_ollama("ollama:qwen3.6:latest", msgs, temperature=0.0, max_output_tokens=8)
    assert captured["json"]["think"] is True


def test_call_ollama_429_raises_ratelimit(clean_ollama_env):
    import httpx

    def fake_post(url, *, json, timeout):
        return _FakeOllamaResp(429, None, text="too many requests")

    clean_ollama_env.setattr(httpx, "post", fake_post)

    with pytest.raises(RateLimitError, match="429"):
        _call_ollama(
            "ollama:qwen3.6:latest",
            [{"role": "user", "content": "x"}],
            temperature=0.0,
            max_output_tokens=8,
        )


def test_call_ollama_http_error_raises_llmcallerror(clean_ollama_env):
    import httpx

    def fake_post(url, *, json, timeout):
        return _FakeOllamaResp(500, None, text="boom")

    clean_ollama_env.setattr(httpx, "post", fake_post)

    with pytest.raises(LLMCallError, match="ollama HTTP 500"):
        _call_ollama(
            "ollama:qwen3.6:latest",
            [{"role": "user", "content": "x"}],
            temperature=0.0,
            max_output_tokens=8,
        )


def test_dispatch_routes_ollama_and_local_prefixes(monkeypatch):
    seen: list[str] = []

    def fake_ollama(model, messages, *, temperature, max_output_tokens):
        seen.append(model)
        return {
            "text": "ok",
            "raw": {},
            "model": model,
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_call_ollama", fake_ollama)

    msgs = [{"role": "user", "content": "x"}]
    call_module._dispatch("ollama:qwen3.6:latest", msgs, temperature=0.0, max_output_tokens=8)
    call_module._dispatch("local:gemma4:31b", msgs, temperature=0.0, max_output_tokens=8)

    assert seen == ["ollama:qwen3.6:latest", "local:gemma4:31b"]


def test_ollama_think_toggle_busts_cache(isolated_cache, clean_ollama_env):
    """Toggling PARALLAX_OLLAMA_THINK for the same model/messages must NOT
    replay a stale cache entry — the knob changes the response, so it must be
    part of the cache identity.

    Regression for codex PR #87 finding: a think-unset run can cache an
    empty-content answer (reasoning ate the whole num_predict budget); rerunning
    with PARALLAX_OLLAMA_THINK=0 previously returned that stale empty answer
    because the cache key ignored the think setting.
    """
    mp = clean_ollama_env
    dispatched: list[str] = []

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        think = os.environ.get("PARALLAX_OLLAMA_THINK", "").strip().lower()
        dispatched.append(think)
        # Mirror the real think-dependent behaviour: with think unset the hidden
        # reasoning phase consumes the budget and content comes back empty; with
        # think=0 the model answers directly.
        text = "42" if think in call_module._FALSEY else ""
        return {
            "text": text,
            "raw": {},
            "model": model,
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    mp.setattr(call_module, "_dispatch_with_retry", fake_dispatch)

    msgs = [{"role": "user", "content": "what is 6*7?"}]

    # First run: think unset -> empty content, cached under the think-unset key.
    first = call("ollama:qwen3.6:latest", msgs)
    assert first["_cached"] is False
    assert first["text"] == ""

    # Operator sets PARALLAX_OLLAMA_THINK=0 and reruns the SAME model/messages.
    mp.setenv("PARALLAX_OLLAMA_THINK", "0")
    second = call("ollama:qwen3.6:latest", msgs)
    assert second["_cached"] is False, (
        "think toggle did not bust the cache — stale empty answer replayed"
    )
    assert second["text"] == "42"
    assert dispatched == ["", "0"], "expected a fresh dispatch after the think toggle"


def test_hash_prompt_think_only_affects_ollama(clean_ollama_env):
    """The think setting joins the cache identity for ollama models only; the
    tri-state is normalized (``1`` == ``true``) and non-ollama models are
    unaffected so their pre-existing cache rows stay reachable (backward compat).
    """
    mp = clean_ollama_env
    msgs = [{"role": "user", "content": "q"}]

    # Ollama: each distinct think state yields a distinct cache identity.
    mp.delenv("PARALLAX_OLLAMA_THINK", raising=False)
    h_unset = _hash_prompt("ollama:qwen3.6:latest", msgs, None, None)
    mp.setenv("PARALLAX_OLLAMA_THINK", "true")
    h_true = _hash_prompt("ollama:qwen3.6:latest", msgs, None, None)
    mp.setenv("PARALLAX_OLLAMA_THINK", "0")
    h_false = _hash_prompt("ollama:qwen3.6:latest", msgs, None, None)
    assert len({h_unset, h_true, h_false}) == 3

    # Truthy variants normalize to the same identity (``1`` == ``true``).
    mp.setenv("PARALLAX_OLLAMA_THINK", "1")
    assert _hash_prompt("ollama:qwen3.6:latest", msgs, None, None) == h_true

    # Backward compat: for non-ollama models the think env is irrelevant, so the
    # hash is identical whether or not PARALLAX_OLLAMA_THINK is set — existing
    # gemini/claude cache rows keyed before this change stay reachable.
    mp.delenv("PARALLAX_OLLAMA_THINK", raising=False)
    g_unset = _hash_prompt("gemini-2.5-flash", msgs, None, None)
    mp.setenv("PARALLAX_OLLAMA_THINK", "true")
    g_think = _hash_prompt("gemini-2.5-flash", msgs, None, None)
    assert g_unset == g_think
