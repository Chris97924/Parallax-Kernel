"""Mutation-hardening for ``parallax.llm.call`` (land/20260824 wave 5, S6).

Additive companion to ``tests/test_llm_call.py``. Eighty semantic mutants were
applied to a pristine tree one at a time against that suite.

Tally — applied 80 / killed by the pre-existing suite 39 / killed by the tests
below 41 / equivalent (excluded) 0 / unaddressed 0.

2026-09-09 (PA-PARALLAX-F7): the Claude backend and its ``claude-`` dispatch arm
were deleted from the module, so the test that asserted its missing-key message
and the fabricated-SDK injector it used went with it. The rate-limit-wording case
survives against the Gemini adapter, which exercises the same last-resort text
probe. Two mutants from the original wave (that key guard's message, that SDK's
import-error wording) no longer have code to mutate.

What the existing suite could not see
-------------------------------------
``test_llm_call.py`` is a strong suite about ONE property: that a config knob
which changes the response also changes the cache key. Every ``*_busts_cache``
test writes under config A, flips the knob, and asserts a re-dispatch — so the
mutants that drop a dimension from the ollama identity, invert the ollama/API
branch, or break prefix stripping all die instantly. What it never asks is what
the key, the request or the stored row actually CONTAIN:

* **Cache identity is only ever compared to itself.** Two runs under the same
  build are compared, so anything that changes the key consistently is
  invisible: the digest can drop to md5, the JSON can stop being key-sorted
  (making the key depend on dict insertion order, i.e. on Python and library
  version), the model can fall out of the pinned-``cache_key`` identity, and
  ``response_schema`` can stop contributing at all. Each of those silently
  strands every existing cache row on the next deploy, or — worse for the
  schema case — serves an answer shaped for a different schema.
* **The cache is exercised through ``call()``, never inspected as a table.**
  Nothing reads ``llm_cache`` back, so the ``PRIMARY KEY``, ``INSERT OR
  REPLACE``, the WAL/busy_timeout pragmas that make concurrent sweeps work, the
  ``ensure_ascii=False`` that keeps non-ASCII answers readable on disk, the
  ``_cached`` flag being stripped before persistence, and which model a
  fallback answer is filed under are all unobserved.
* **The retry policy is never asserted, only relied on.** ``stop_after_attempt(3)``,
  the 5s backoff floor, the exception types covered and ``reraise=True`` are the
  module's entire contract with a rate-limited provider — and the one test that
  exercises retry counts dispatches, which any budget >= 2 satisfies.
* **The provider adapters are only driven down their happy path.** The message
  role split, the join separator, and the substrings that classify a provider
  error as a rate limit (which decides whether the fallback model is ever
  reached) have no adversarial input; the Gemini SDK is not installed in this
  environment, so those paths need injected stand-ins to reach at all.
* **``_dispatch``'s prefix table is only fed models that match.** ``"gemini"``
  without its hyphen — and, since 2026-09-09, ``"claude-"`` in any form — must be
  UNSUPPORTED, and the fall-through must raise rather than return an empty result.

Expected values are literals throughout — 64 hex characters, 3 attempts, 5.0
seconds, 2048 tokens, 300.0, and the exact identity strings. Deriving an
expectation from the module's own constant is what left them untestable.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import types as pytypes
from typing import Any

import pytest
from tenacity import stop_after_attempt

from parallax.llm import call as call_mod
from parallax.llm.call import LLMCallError, RateLimitError

MESSAGES = [{"role": "user", "content": "hello"}]


@pytest.fixture()
def isolated_cache(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Point the module's cache at tmp_path and hand back the DB path."""
    path = tmp_path / "llm_cache.sqlite"
    monkeypatch.setenv("PARALLAX_LLM_CACHE", str(path))
    return path


def _rows(path: pathlib.Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(path))
    try:
        return list(conn.execute("SELECT model, prompt_hash, response_json FROM llm_cache"))
    finally:
        conn.close()


def _ok(model: str, text: str = "answer") -> dict[str, Any]:
    return {
        "text": text,
        "raw": {},
        "model": model,
        "prompt_tokens": 1,
        "completion_tokens": 2,
    }


# ---------------------------------------------------------------------------
# Cache location and connection setup
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_empty_cache_env_var_falls_back_to_the_default_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PARALLAX_LLM_CACHE=""`` means "unset", not "cache at the empty path".

    An env var exported without a value is the ordinary way a shell hands over
    an empty string. Treating it as a real override makes ``_connect_cache``
    build ``Path("")`` and create the DB in the process's cwd — a stray cache
    file per working directory, none of which the next run finds.
    """
    monkeypatch.setenv("PARALLAX_LLM_CACHE", "")

    assert call_mod._cache_path() == pathlib.Path.home() / ".parallax" / "llm_cache.sqlite"


@pytest.mark.unit
def test_the_default_cache_lives_under_the_dot_parallax_home_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One shared location, so a sweep and a CLI run hit the same cache."""
    monkeypatch.delenv("PARALLAX_LLM_CACHE", raising=False)

    path = call_mod._cache_path()

    assert path.name == "llm_cache.sqlite"
    assert path.parent.name == ".parallax"
    assert path.parent.parent == pathlib.Path.home()


@pytest.mark.unit
def test_the_cache_connection_is_wal_with_a_five_second_busy_timeout(
    isolated_cache: pathlib.Path,
) -> None:
    """Both pragmas exist for the same reason: concurrent sweeps.

    Ablation and threshold sweeps run several processes against one cache file.
    Without WAL a reader blocks writers; without a busy timeout a contended
    write fails immediately with "database is locked" rather than waiting. The
    suite runs single-process, so neither pragma is load-bearing for it and
    both can be dropped invisibly.
    """
    conn = call_mod._connect_cache()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


@pytest.mark.unit
def test_a_second_put_under_one_hash_replaces_rather_than_duplicating(
    isolated_cache: pathlib.Path,
) -> None:
    """The PRIMARY KEY and ``INSERT OR REPLACE`` are one mechanism.

    Two mutants live here and only a read-back sees either. Dropping the
    PRIMARY KEY makes every re-cache append a row, so the table grows without
    bound and ``_cache_get``'s ``LIMIT``-less SELECT starts returning whichever
    row SQLite reaches first. Switching to ``INSERT OR IGNORE`` keeps one row
    but pins it to the FIRST response forever, so a re-run after a provider fix
    silently keeps serving the old (possibly truncated or errored) answer.
    """
    conn = call_mod._connect_cache()
    try:
        call_mod._cache_put(conn, "gemini-pro", "HASH-1", {"text": "first"})
        call_mod._cache_put(conn, "gemini-pro", "HASH-1", {"text": "second"})
    finally:
        conn.close()

    rows = _rows(isolated_cache)
    assert len(rows) == 1
    assert json.loads(rows[0][2])["text"] == "second"


@pytest.mark.unit
def test_cached_payloads_keep_non_ascii_text_readable_on_disk(
    isolated_cache: pathlib.Path,
) -> None:
    """``ensure_ascii=False`` — the row is a debugging surface, not just bytes.

    Every non-English answer becomes a wall of ``\\uXXXX`` escapes otherwise:
    still correct on read-back, but unreadable to anyone opening the cache with
    sqlite3 to find out what a model actually said, and several times larger.
    """
    conn = call_mod._connect_cache()
    try:
        call_mod._cache_put(conn, "gemini-pro", "HASH-UTF8", {"text": "café ☕"})
    finally:
        conn.close()

    raw = _rows(isolated_cache)[0][2]
    assert "café ☕" in raw
    assert "\\u" not in raw


# ---------------------------------------------------------------------------
# Cache identity — what the key is made of
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_api_backend_generation_identity_is_a_stable_sorted_string() -> None:
    """Key-sorted JSON is what makes the identity reproducible across runs.

    Without ``sort_keys`` the component's byte form follows dict INSERTION
    order, so the cache key becomes an artefact of how the literal happens to
    be written. Any future reordering of those two lines silently invalidates
    every stored row — a whole-cache miss that looks exactly like a cold start.
    The expected string is written out in full because that is the only way to
    observe the ordering at all.
    """
    assert call_mod._generation_options_identity(0.0, 2048) == (
        'opts={"max_output_tokens": 2048, "temperature": 0.0}'
    )


@pytest.mark.unit
def test_the_ollama_provider_identity_is_a_stable_sorted_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guarantee on the ollama side, plus its endpoint and think state."""
    monkeypatch.delenv("PARALLAX_OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("PARALLAX_OLLAMA_THINK", raising=False)

    assert call_mod._ollama_provider_identity(0.0, 2048) == (
        "base=http://192.168.1.134:11434|think=unset"
        '|opts={"num_predict": 2048, "temperature": 0.0}'
    )


@pytest.mark.unit
def test_an_empty_cache_key_still_pins_the_entry() -> None:
    """``is not None``, not truthiness: ``""`` is a pin, not an absence.

    A caller passing ``cache_key=""`` — a run label that came back empty from a
    config lookup — means "one entry for this model and payload". Under
    truthiness the call silently reverts to the schema-bearing message-derived
    key, so a sweep that meant to reuse one cached answer starts dispatching per
    requested schema.

    The two calls differ only in ``response_schema``, which the pinned branch
    deliberately does not carry: the messages themselves are held identical
    because since 2026-09-09 they DO join a pinned key (PA-PARALLAX-F2), so
    varying them here would prove nothing about ``is not None``.
    """
    a = call_mod._hash_prompt("gemini-pro", MESSAGES, {"type": "object"}, "")
    b = call_mod._hash_prompt("gemini-pro", MESSAGES, {"type": "array"}, "")

    assert a == b


@pytest.mark.unit
def test_the_model_is_part_of_a_pinned_cache_key() -> None:
    """Two models under one pin must not share an entry.

    ``cache_key`` pins a RUN; the model still selects the answer. Dropping it
    from the identity makes a Flash answer replay for a Pro request under the
    same pin — the exact confusion the fallback-hash logic elsewhere in this
    module exists to prevent.
    """
    pro = call_mod._hash_prompt("gemini-pro", MESSAGES, None, "run-7")
    flash = call_mod._hash_prompt("gemini-flash", MESSAGES, None, "run-7")

    assert pro != flash


@pytest.mark.unit
def test_message_key_order_does_not_change_the_cache_key() -> None:
    """The same message written two ways is the same message.

    ``sort_keys=True`` on the message blob is what makes that true. Without it
    a caller that builds ``{"content": ..., "role": ...}`` misses every entry
    written by one that builds ``{"role": ..., "content": ...}`` — two halves
    of the same codebase quietly maintaining separate caches.
    """
    a = call_mod._hash_prompt("gemini-pro", [{"role": "user", "content": "x"}], None, None)
    b = call_mod._hash_prompt("gemini-pro", [{"content": "x", "role": "user"}], None, None)

    assert a == b


@pytest.mark.unit
def test_the_response_schema_is_part_of_the_cache_key() -> None:
    """A different requested shape is a different request.

    Structured-output calls send the schema to the provider, so it changes the
    answer. Dropping it from the key replays an answer shaped for the previous
    schema, which parses as valid JSON and fails only downstream.
    """
    obj = call_mod._hash_prompt("gemini-pro", MESSAGES, {"type": "object"}, None)
    arr = call_mod._hash_prompt("gemini-pro", MESSAGES, {"type": "array"}, None)
    none = call_mod._hash_prompt("gemini-pro", MESSAGES, None, None)

    assert obj != arr
    assert obj != none


@pytest.mark.unit
def test_the_cache_key_is_a_sha256_digest() -> None:
    """64 hex characters. The width is the assertion, since any digest "works".

    A weaker digest still produces stable keys, so nothing behavioural breaks —
    it just narrows the space in which two different prompts can collide onto
    one cached answer, which surfaces as one prompt mysteriously returning
    another's response.
    """
    digest = call_mod._hash_prompt("gemini-pro", MESSAGES, None, None)

    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


@pytest.mark.unit
def test_hash_prompt_defaults_match_the_values_call_dispatches_with() -> None:
    """The four-argument form must agree with ``call()``'s own defaults.

    ``_hash_prompt``'s defaults exist only so legacy four-arg callers keep
    working; the moment they drift from ``call()``'s (temperature 0.0, budget
    2048) those callers compute a key for a config nobody ever dispatched, and
    every one of their lookups misses forever.
    """
    implicit = call_mod._hash_prompt("gemini-pro", MESSAGES, None, None)
    explicit = call_mod._hash_prompt("gemini-pro", MESSAGES, None, None, 0.0, 2048)

    assert implicit == explicit


# ---------------------------------------------------------------------------
# The Gemini message split
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_messages_split_into_system_instruction_and_user_prompt() -> None:
    """System content goes to ``system_instruction``; everything else is the prompt.

    Inverting the split sends the user's question as the system instruction and
    the system prompt as the question — a call that still succeeds and returns
    fluent text, which is why no status-code or shape assertion can catch it.
    """
    system, user = call_mod._messages_to_gemini(
        [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "Explain X."},
        ]
    )

    assert system == "You are terse."
    assert user == "Explain X."


@pytest.mark.unit
def test_a_message_without_a_role_is_treated_as_user_content() -> None:
    """The default role is ``user``, and no system parts means ``None``.

    Defaulting to ``system`` would silently promote an unlabelled message into
    the system instruction. ``None`` (rather than ``""``) is what the SDK reads
    as "no system instruction at all" — an empty string is a real, if blank,
    instruction.
    """
    system, user = call_mod._messages_to_gemini([{"content": "bare"}])

    assert system is None
    assert user == "bare"


@pytest.mark.unit
def test_multiple_parts_are_joined_by_a_blank_line() -> None:
    """A blank line separates turns; a single newline runs them together.

    Two user messages joined by ``\\n`` read to the model as one continued
    paragraph, which changes the answer without changing anything a test that
    only checks "text came back" can see.
    """
    system, user = call_mod._messages_to_gemini(
        [
            {"role": "system", "content": "S1"},
            {"role": "system", "content": "S2"},
            {"role": "user", "content": "U1"},
            {"role": "user", "content": "U2"},
        ]
    )

    assert system == "S1\n\nS2"
    assert user == "U1\n\nU2"


# ---------------------------------------------------------------------------
# Rate-limit classification (decides whether the fallback model is reached)
# ---------------------------------------------------------------------------


def _install_fake_genai(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Inject a minimal ``google.genai`` whose generate_content raises ``exc``.

    The real SDK is not installed in this environment, so ``_call_gemini``'s
    error-classification branch is unreachable without a stand-in. Only the
    three names the function actually touches are provided.
    """
    google_mod = pytypes.ModuleType("google")
    genai_mod = pytypes.ModuleType("google.genai")
    types_mod = pytypes.ModuleType("google.genai.types")

    class _Config:
        def __init__(self, **_kw: Any) -> None: ...

    class _Models:
        def generate_content(self, **_kw: Any) -> Any:
            raise exc

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


@pytest.mark.unit
def test_gemini_resource_exhausted_is_a_rate_limit_not_a_hard_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini signals quota exhaustion by NAME as often as by status code.

    The classification is the whole fallback mechanism: only ``RateLimitError``
    is retried by tenacity and only ``RateLimitError`` reaches the
    ``fallback_model`` branch in ``call()``. Narrowing the match to ``"429"``
    turns a recoverable quota event into a hard failure that never tries the
    fallback — the exact scenario the fallback exists for.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _install_fake_genai(monkeypatch, RuntimeError("RESOURCE_EXHAUSTED: quota"))

    with pytest.raises(RateLimitError):
        call_mod._call_gemini("gemini-pro", MESSAGES, temperature=0.0, max_output_tokens=8)


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    ["rate limit exceeded", "rate_limit_error", "RATE_LIMIT exceeded", "HTTP 429"],
)
def test_rate_limit_wording_is_matched_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    """Three spellings and a status code, all case-insensitive.

    Providers change error prose without notice, which is why the last-resort
    text probe is a union of substrings rather than one. Two of the four cases
    below are invisible to any test using a single canonical message: the spaced
    ``"rate limit"`` variant, and an upper-cased body that only matches once
    ``msg.lower()`` has run.

    The probe is reached only when the exception carries no status code and no
    recognizable SDK class — a bare ``RuntimeError``, as here. Classification by
    TYPE is asserted separately in
    ``tests/test_llm_call_correctness.py::test_transient_classified_by_exception_type``.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _install_fake_genai(monkeypatch, RuntimeError(message))

    with pytest.raises(RateLimitError):
        call_mod._call_gemini("gemini-pro", MESSAGES, temperature=0.0, max_output_tokens=8)


# ---------------------------------------------------------------------------
# Ollama normalisation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_ollama_base_url_env_value_is_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A padded env value is the same endpoint, so it must be the same key.

    Whitespace arrives routinely from ``.env`` files and PM2 ecosystem configs.
    Untrimmed, ``" http://host:11434 "`` is both a broken request URL and a
    distinct cache identity — the same endpoint maintaining two caches.
    """
    monkeypatch.setenv("PARALLAX_OLLAMA_BASE_URL", "  http://gb10:11434  ")

    assert call_mod._ollama_base_url() == "http://gb10:11434"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", "true"), ("true", "true"), ("TRUE", "true"), ("yes", "true"),
        ("on", "true"), ("ON", "true"),
        ("0", "false"), ("false", "false"), ("FALSE", "false"), ("off", "false"),
        ("", "unset"), ("maybe", "unset"),
    ],
)
def test_the_think_tri_state_vocabulary_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    """One normaliser feeds both the wire payload and the cache key.

    That is the point of the tri-state: ``"unset"`` must stay distinct from
    ``"false"`` because omitting the key lets the model's own default apply,
    which is not the same as forcing reasoning off. ``"on"`` and the
    upper-cased spellings are the members the existing suite never supplies,
    so dropping either from the vocabulary silently reclassifies an operator's
    config as ``unset``.
    """
    monkeypatch.setenv("PARALLAX_OLLAMA_THINK", raw)

    assert call_mod._ollama_think_state() == expected


@pytest.mark.unit
def test_ollama_generation_options_are_canonically_typed() -> None:
    """``num_predict`` is an int on the wire and in the key.

    Ollama's ``options`` are typed; a float ``num_predict`` is both a
    questionable request body and — because the same dict is serialised into
    the cache identity — a second key for an identical config, so ``2048`` and
    ``2048.0`` would miss each other's entries.
    """
    opts = call_mod._ollama_options(0, 2048.0)

    assert opts == {"temperature": 0.0, "num_predict": 2048}
    assert isinstance(opts["num_predict"], int)
    assert isinstance(opts["temperature"], float)


@pytest.mark.unit
def test_the_ollama_request_deadline_defaults_to_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """300 seconds, because a local reasoning model is genuinely slow.

    The timeout is deliberately excluded from the cache identity (it cannot
    change a successful response), which also means no ``*_busts_cache`` test
    can see it. A 30-second deadline aborts a normal GB10 generation and
    surfaces as ``LLMCallError: ollama request failed`` — read as the model
    being down rather than as a client-side cut.
    """
    monkeypatch.delenv("PARALLAX_OLLAMA_TIMEOUT", raising=False)
    monkeypatch.delenv("PARALLAX_OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    seen: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {"message": {"content": "hi"}, "done_reason": "stop", "model": "m"}

    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: (seen.update(kw, url=url), _Resp())[1]
    )

    call_mod._call_ollama("ollama:qwen3", MESSAGES, temperature=0.0, max_output_tokens=8)

    assert seen["timeout"] == 300.0
    assert call_mod._DEFAULT_OLLAMA_TIMEOUT == 300.0


# ---------------------------------------------------------------------------
# _dispatch: the prefix table is a closed set
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "model",
    ["gemini", "claude", "claude-sonnet-4-5", "geminipro", "claudeish", "mistral-7b"],
)
def test_models_outside_the_prefix_table_are_rejected_by_name(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """The hyphen is part of the prefix, and the fall-through RAISES.

    ``"gemini"`` without its hyphen is not a model name; loosening the prefix
    routes it into a provider adapter that fails later with an unrelated
    message. And returning an empty dict instead of raising on the fall-through
    is worse: ``call()`` would cache ``{}`` as a successful response and every
    subsequent request for that model would replay an empty answer from the
    cache with ``_cached: True``.

    ``claude-sonnet-4-5`` is here because a fully-formed Claude model id is the
    live regression for the 2026-09-09 branch removal — it must land on the
    fall-through, not on a dispatch arm.

    The assertion is on the MESSAGE, since every one of these paths ends in
    ``LLMCallError`` one way or another.
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with pytest.raises(LLMCallError, match="unsupported model prefix"):
        call_mod._dispatch(model, MESSAGES, temperature=0.0, max_output_tokens=8)


# ---------------------------------------------------------------------------
# The retry policy
# ---------------------------------------------------------------------------


class _FakeClock:
    """Stands in for tenacity's sleeper: records the delay instead of taking it.

    ``sleep`` is a documented constructor argument of the ``@retry`` decorator,
    so swapping it is the supported way to watch the backoff schedule without
    paying for it in wall clock. Every test below drives the REAL policy object
    against this clock, which is what keeps the assertions on observable
    behaviour — attempts made, delays waited, exception finally raised — rather
    than on tenacity's internal attribute names.
    """

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _always_raises(exc: type[Exception]) -> tuple[Any, list[str]]:
    """A ``_dispatch`` replacement that always fails, plus its call log."""
    calls: list[str] = []

    def boom(
        model: str,
        messages: list[dict],
        *,
        temperature: float,
        max_output_tokens: int,
    ) -> dict:
        calls.append(model)
        raise exc("provider refused")

    return boom, calls


@pytest.mark.unit
@pytest.mark.parametrize("exc", [RateLimitError, LLMCallError])
def test_a_retryable_failure_is_attempted_three_times_and_then_reraised(
    monkeypatch: pytest.MonkeyPatch, exc: type[Exception]
) -> None:
    """Three attempts, a 5s floor between them, and the ORIGINAL exception out.

    Each of those is load-bearing, and each is asserted here by running the
    policy rather than by reading the decorator's fields: a tenacity upgrade
    that renames an internal must not be able to redden a file whose subject is
    ``parallax.llm.call`` while the policy has not changed at all.

    * three attempts — a 429 burst is usually over inside two retries; one
      attempt is no retry at all, and the fallback model then absorbs load the
      primary would have served. A test that merely counts dispatches without
      pinning the number passes under any budget >= 2.
    * a 5s floor — the raw exponential starts an order of magnitude below it,
      so the floor is the only thing standing between a provider-side rate
      limit and a tight retry loop against the service that just asked us to
      slow down. Both inter-attempt delays are asserted, not just the first.
    * both LLMCallError and RateLimitError — transient transport failures are
      LLMCallError, so narrowing the retried type to RateLimitError stops
      retrying the most common recoverable case. Hence the parametrisation:
      both must produce the same three-attempt shape.
    * reraise — without it an exhausted budget raises tenacity's RetryError,
      which ``call()``'s ``except RateLimitError`` does not catch, so the
      fallback branch is never entered. Asserted as the type that escapes.
    """
    boom, calls = _always_raises(exc)
    clock = _FakeClock()
    monkeypatch.setattr(call_mod, "_dispatch", boom)
    monkeypatch.setattr(call_mod._dispatch_with_retry.retry, "sleep", clock)

    with pytest.raises(exc):
        call_mod._dispatch_with_retry(
            "gemini-pro", MESSAGES, temperature=0.0, max_output_tokens=8
        )

    assert len(calls) == 3
    assert clock.sleeps == [5.0, 5.0]


@pytest.mark.unit
def test_a_failure_outside_the_retried_types_is_dispatched_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retried set is closed, and widening it to ``Exception`` has a real cost.

    A programming error inside an adapter — a ``TypeError`` on a malformed
    provider payload, say — is not transient. Retrying it burns the whole
    budget and two backoff windows before surfacing the same bug, and against a
    rate-limited provider it spends attempts a genuine 429 would have needed.
    One dispatch and no sleeps is the observable difference.
    """
    boom, calls = _always_raises(ValueError)
    clock = _FakeClock()
    monkeypatch.setattr(call_mod, "_dispatch", boom)
    monkeypatch.setattr(call_mod._dispatch_with_retry.retry, "sleep", clock)

    with pytest.raises(ValueError):
        call_mod._dispatch_with_retry(
            "gemini-pro", MESSAGES, temperature=0.0, max_output_tokens=8
        )

    assert len(calls) == 1
    assert clock.sleeps == []


@pytest.mark.unit
def test_the_backoff_curve_floors_at_five_seconds_and_is_capped_at_sixty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is unreachable within three attempts, so drive the same wait further.

    ``retry_with`` is tenacity's supported way to re-wrap a decorated function
    with one argument replaced. Only ``stop`` moves here, so the wait policy
    under observation is the very object the production decorator installed;
    twelve attempts is enough for the exponential to saturate.

    The assertions are properties of the curve rather than tenacity's doubling
    schedule — which delay lands on which attempt is tenacity's semantics and
    has moved between releases, whereas the floor, the cap and the fact that
    the curve rises at all are ours:

    * ``5.0`` as the smallest delay pins ``min=5``; the unclamped curve starts
      well below it, so nothing else in the policy could produce a 5.
    * ``60.0`` as the largest pins ``max=60`` AND that the cap actually binds.
      A loosened cap keeps climbing past it; a changed multiplier either never
      reaches it inside the probe budget or overshoots the floor first.
    * non-decreasing pins that this is a backoff at all — a constant or
      shrinking wait is the failure a floor-and-cap check on its own misses.
    """
    boom, calls = _always_raises(RateLimitError)
    clock = _FakeClock()
    monkeypatch.setattr(call_mod, "_dispatch", boom)
    probe = call_mod._dispatch_with_retry.retry_with(
        stop=stop_after_attempt(12), sleep=clock
    )

    with pytest.raises(RateLimitError):
        probe("gemini-pro", MESSAGES, temperature=0.0, max_output_tokens=8)

    assert len(calls) == 12
    assert len(clock.sleeps) == 11
    assert min(clock.sleeps) == 5.0
    assert max(clock.sleeps) == 60.0
    assert clock.sleeps == sorted(clock.sleeps)


# ---------------------------------------------------------------------------
# call(): cache semantics, fallback attribution, resource handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_empty_cached_response_is_still_a_cache_hit(
    isolated_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is not None``, not truthiness — an empty payload is a stored answer.

    A provider that returned nothing is a result worth remembering; under a
    truthiness check it is a permanent cache miss, so every call re-dispatches
    and re-pays for the same empty answer, and the ``_cached`` flag downstream
    consumers read is wrong on top of that.
    """
    prompt_hash = call_mod._hash_prompt("gemini-pro", MESSAGES, None, None, 0.0, 2048)
    conn = call_mod._connect_cache()
    try:
        call_mod._cache_put(conn, "gemini-pro", prompt_hash, {})
    finally:
        conn.close()

    dispatched: list[str] = []
    monkeypatch.setattr(
        call_mod,
        "_dispatch_with_retry",
        lambda model, _m, **_k: (dispatched.append(model), _ok(model))[1],
    )

    result = call_mod.call("gemini-pro", MESSAGES)

    assert dispatched == []
    assert result == {"_cached": True}


@pytest.mark.unit
def test_call_dispatches_with_the_documented_generation_defaults(
    isolated_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temperature 0.0 and a 2048-token budget unless the caller says otherwise.

    Both are response-affecting, so both join the cache key — which means
    changing a default silently invalidates the whole cache AND changes every
    answer. The budget is the sharper of the two: a smaller default truncates
    long structured outputs, and a truncated answer is cached like any other.
    """
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        call_mod,
        "_dispatch_with_retry",
        lambda model, _m, **kw: (seen.update(kw), _ok(model))[1],
    )

    call_mod.call("gemini-pro", MESSAGES)

    assert seen["temperature"] == 0.0
    assert seen["max_output_tokens"] == 2048


@pytest.mark.unit
def test_a_fallback_answer_is_filed_under_the_fallback_model(
    isolated_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored row's ``model`` column names who actually answered.

    The fallback hash already keeps the Flash answer out of the Pro key; the
    ``model`` column is the other half of the same guarantee, and it is what
    makes ``SELECT model, COUNT(*)`` — the query that answers "how much of this
    sweep was served by the fallback" — mean anything. The existing suite
    checks the hash and never reads the column.
    """
    def _fake(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        if model == "gemini-pro":
            raise RateLimitError("429")
        return _ok(model)

    monkeypatch.setattr(call_mod, "_dispatch_with_retry", _fake)

    result = call_mod.call("gemini-pro", MESSAGES, fallback_model="gemini-flash")

    assert result["fallback_from"] == "gemini-pro"
    rows = _rows(isolated_cache)
    assert len(rows) == 1
    assert rows[0][0] == "gemini-flash"


@pytest.mark.unit
def test_the_cached_flag_is_not_persisted_into_the_stored_row(
    isolated_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_cached`` describes THIS call, not the stored answer.

    Persisting it writes ``False`` into the row, and every later read then
    reports ``_cached: False`` for what is unambiguously a cache hit — the
    flag inverts its own meaning, and consumers counting cache efficiency read
    zero hits forever.
    """
    monkeypatch.setattr(
        call_mod, "_dispatch_with_retry", lambda model, _m, **_k: _ok(model)
    )

    result = call_mod.call("gemini-pro", MESSAGES)

    assert result["_cached"] is False
    stored = json.loads(_rows(isolated_cache)[0][2])
    assert "_cached" not in stored
    assert stored["text"] == "answer"


@pytest.mark.unit
def test_the_cache_connection_is_closed_after_every_call(
    isolated_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``finally: conn.close()`` is the only thing bounding handle growth.

    ``call()`` opens a fresh connection per invocation, so a leak is one
    handle per LLM call — thousands over a LongMemEval sweep — and each leaked
    WAL reader holds the snapshot open. Nothing about the returned result
    changes, which is why the connection has to be watched directly.
    """
    opened: list[sqlite3.Connection] = []
    real_connect = call_mod._connect_cache

    def _spy() -> sqlite3.Connection:
        conn = real_connect()
        opened.append(conn)
        return conn

    monkeypatch.setattr(call_mod, "_connect_cache", _spy)
    monkeypatch.setattr(
        call_mod, "_dispatch_with_retry", lambda model, _m, **_k: _ok(model)
    )

    call_mod.call("gemini-pro", MESSAGES)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
