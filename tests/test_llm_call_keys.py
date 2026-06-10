"""Tests for the Gemini API key pool in parallax.llm.call.

Covers the env-var enumeration, frozen-key exclusion, dedup of duplicate
values, round-robin advancement on every call, and the no-key error
path. Integration with ``_call_gemini`` is exercised via
``monkeypatch.setattr`` on the inner SDK client.
"""

from __future__ import annotations

import pytest

import parallax.llm.call as call_module


@pytest.fixture(autouse=True)
def _reset_rotation(monkeypatch):
    """Each test starts from a clean key pool and rotation index."""
    for env in call_module._GEMINI_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(call_module, "_key_idx", 0)
    yield


def test_pool_excludes_unset_envs(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_2", "kB")
    assert call_module._gemini_keys() == ["kB"]


def test_pool_excludes_frozen_envs(monkeypatch):
    """A renamed-to-FROZEN_* key must not be picked up by the pool."""
    monkeypatch.setenv("GEMINI_API_KEY_FROZEN_EXPIRED_20260420", "kExpired")
    monkeypatch.setenv("GEMINI_API_KEY_2", "kB")
    monkeypatch.setenv("GEMINI_API_KEY_3", "kC")
    keys = call_module._gemini_keys()
    assert "kExpired" not in keys
    assert keys == ["kB", "kC"]


def test_pool_dedupes_repeated_values(monkeypatch):
    """Same key in two env vars must not be served twice in the rotation."""
    monkeypatch.setenv("GEMINI_API_KEY", "kSame")
    monkeypatch.setenv("GOOGLE_API_KEY", "kSame")
    assert call_module._gemini_keys() == ["kSame"]


def test_round_robin_advances_each_call(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_2", "kB")
    monkeypatch.setenv("GEMINI_API_KEY_3", "kC")
    seen = [call_module._next_gemini_key() for _ in range(5)]
    # 5 calls over a pool of 2 → B,C,B,C,B
    assert seen == ["kB", "kC", "kB", "kC", "kB"]


def test_no_key_raises_llm_call_error(monkeypatch):
    with pytest.raises(call_module.LLMCallError, match="no Gemini API key"):
        call_module._next_gemini_key()


def test_call_gemini_uses_next_key(monkeypatch):
    """_call_gemini must pull the api_key from the rotation, not a hard env."""
    monkeypatch.setenv("GEMINI_API_KEY_2", "kB")
    monkeypatch.setenv("GEMINI_API_KEY_3", "kC")

    seen_keys: list[str] = []

    class _FakeResp:
        text = "ok"
        usage_metadata = None
        candidates = None

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            return _FakeResp()

    class _FakeClient:
        def __init__(self, *, api_key):
            seen_keys.append(api_key)
            self.models = _FakeModels()

    # Inject a stub google.genai before _call_gemini imports it.
    import sys
    import types

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = _FakeClient
    fake_types = types.ModuleType("google.genai.types")

    class _FakeConfig:
        def __init__(self, **_kw):
            pass

    fake_types.GenerateContentConfig = _FakeConfig
    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

    msgs = [{"role": "user", "content": "hi"}]
    call_module._call_gemini(
        "gemini-2.5-flash", msgs, temperature=0.0, max_output_tokens=64
    )
    call_module._call_gemini(
        "gemini-2.5-flash", msgs, temperature=0.0, max_output_tokens=64
    )
    call_module._call_gemini(
        "gemini-2.5-flash", msgs, temperature=0.0, max_output_tokens=64
    )

    assert seen_keys == ["kB", "kC", "kB"]
