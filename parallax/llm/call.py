"""Unified LLM call with SQLite cache, tenacity retry, and fallback model.

Contract rules (ADR-006):

* Every LLM call in Parallax goes through :func:`call`.
* Cache key is deterministic over ``(model, messages, response_schema)`` —
  or the caller-supplied ``cache_key`` **plus a digest of the rendered
  messages** when they want to pin a run. A pin names the RUN; it never
  replaces the payload identity, so a prompt edit or a change in the rendered
  evidence still invalidates the entry (PA-PARALLAX-F2). The
  response-affecting generation options (``temperature`` and the output-token
  budget) also join the key for EVERY backend, since they change the response for
  the same ``(model, messages, schema)``. ``ollama:`` / ``local:`` models fold
  them in via the normalized provider identity (which additionally covers the base
  URL and ``PARALLAX_OLLAMA_THINK`` state — see ``_ollama_provider_identity``);
  the API backends fold them in via ``_generation_options_identity``.
* Errors are classified by exception TYPE first (SDK error classes, HTTP status
  codes), with a substring check on ``str(exc)`` only as a last resort:
  :class:`RateLimitError` for 429 / quota exhaustion, :class:`LLMTransientError`
  for capacity and transport failures (5xx, overloaded, timeouts, connection
  errors), :class:`LLMConfigError` for deterministic misconfiguration.
* Tenacity retries the transient classes a few times with a 5s..60s exponential
  backoff; :class:`LLMConfigError` is excluded, so a missing key or an unknown
  prefix surfaces on the first attempt with no sleep. Only when retries are
  exhausted do we fall through to the ``fallback_model`` branch, which fires for
  :class:`RateLimitError` and :class:`LLMTransientError` and never for a config
  error.
* Fallback results are stored under a fallback-keyed hash so re-issuing the
  original call (e.g. once the primary model's quota is back) does not return
  the fallback model's answer masquerading as the primary model's.
* Every backend surfaces a normalized top-level ``stop_reason`` — one of
  ``stop`` / ``length`` / ``blocked`` / ``unknown``. A truncated or blocked
  response is logged at WARNING and NOT cached; an empty response is logged and
  raised so tenacity retries instead of caching ``''`` forever.
* All provider-specific HTTP stays in ``_call_gemini`` / ``_call_ollama``.
  Callers see a uniform ``dict`` return shape.
* Local models served by Ollama (GB10) route through ``_call_ollama`` when the
  model name carries an ``ollama:`` / ``local:`` prefix; no API key is required.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import sqlite3
import sys
import threading
import time

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ---------- Gemini key rotation pool ----------------------------------------

_GEMINI_KEY_ENVS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY_2",
    "GEMINI_API_KEY_3",
)

_key_idx: int = 0


class LLMCallError(RuntimeError):
    """Base class for every failure raised by this module.

    Kept as the base so existing ``except LLMCallError`` callers keep catching
    the two more specific classes below.
    """


class LLMConfigError(LLMCallError):
    """A deterministic misconfiguration — missing API key, SDK not importable,
    unsupported model prefix.

    Excluded from the retry predicate (PA-PARALLAX-F6): retrying cannot fix a
    typo'd model name or an unset env var, and three attempts with a 5s floor
    only delay the operator's feedback by ~10s per call.
    """


class LLMTransientError(LLMCallError):
    """A capacity or transport failure a retry can plausibly resolve — 5xx,
    ``UNAVAILABLE`` / overloaded, read timeouts, connection errors.

    Distinct from :class:`RateLimitError` (which stays a 429-only signal) but
    treated the same way by :func:`call`: retried by tenacity, and once the
    retries are exhausted it reaches the ``fallback_model`` branch
    (PA-PARALLAX-F5).
    """


def _gemini_keys() -> list[str]:
    """Return deduplicated Gemini API keys from configured env vars."""
    seen: set[str] = set()
    keys: list[str] = []
    for env in _GEMINI_KEY_ENVS:
        val = os.environ.get(env)
        if not val:
            continue
        if val in seen:
            continue
        seen.add(val)
        keys.append(val)
    return keys


def _next_gemini_key() -> str:
    """Return next key in round-robin rotation. Raises LLMCallError if pool empty."""
    global _key_idx
    keys = _gemini_keys()
    if not keys:
        raise LLMConfigError("no Gemini API key configured")
    key = keys[_key_idx % len(keys)]
    _key_idx += 1
    return key


class RateLimitError(RuntimeError):
    """Raised on 429 / RESOURCE_EXHAUSTED. Retried inside tenacity; if retries
    are exhausted, the caller falls back to ``fallback_model``."""


# ---------- Error classification (type first, text last) ---------------------

#: HTTP statuses that mean "the request was fine, try again later". 429 is
#: handled separately because it maps to :class:`RateLimitError`.
_TRANSIENT_STATUS: frozenset[int] = frozenset({408, 425, 500, 502, 503, 504, 529})

#: ``google.genai`` error class names, matched on the CLASS rather than on the
#: message so a prose change upstream cannot silently reclassify a failure.
_GENAI_TRANSIENT_ERROR_NAMES: frozenset[str] = frozenset(
    {"ServerError", "ServiceUnavailable", "InternalServerError", "DeadlineExceeded",
     "Unavailable"}
)
_GENAI_RATE_LIMIT_ERROR_NAMES: frozenset[str] = frozenset(
    {"ResourceExhausted", "TooManyRequests"}
)

#: Last-resort text probe for providers that raise an opaque exception type and
#: put the status in the message. Status codes are word-bounded so a token count
#: or a request id cannot masquerade as a 503.
_TRANSIENT_TEXT_RE = re.compile(
    r"\b(?:500|502|503|504|529)\b"
    r"|unavailable|overloaded|deadline exceeded|timed out|timeout"
    r"|connection (?:reset|refused|error|aborted)",
    re.IGNORECASE,
)


def _exception_status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status carried by a provider exception.

    ``google.genai.errors.APIError`` exposes ``.code``; ``httpx.HTTPStatusError``
    carries ``.response.status_code``; several SDKs use ``.status_code``. Reading
    the attribute is what makes classification type-driven rather than a guess at
    the message's wording.
    """
    for attr in ("status_code", "code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):  # a flag, not a status
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _is_httpx_transient(exc: BaseException) -> bool:
    """True for httpx transport failures (timeout, connect, protocol).

    ``sys.modules`` is read rather than imported: an httpx exception instance
    cannot exist unless httpx is already imported, so an absent module is a
    definitive "not an httpx error" and costs no import.
    """
    httpx = sys.modules.get("httpx")
    if httpx is None:
        return False
    transient = tuple(
        cls
        for cls in (
            getattr(httpx, "TimeoutException", None),
            getattr(httpx, "ConnectError", None),
            getattr(httpx, "ReadError", None),
            getattr(httpx, "RemoteProtocolError", None),
        )
        if isinstance(cls, type)
    )
    return bool(transient) and isinstance(exc, transient)


def _classify_provider_error(exc: BaseException, *, message: str | None = None) -> Exception:
    """Map a provider exception onto this module's error contract.

    TYPE first (PA-PARALLAX-F5). An SDK's own error classes and the HTTP status
    they carry are authoritative, so a provider changing its error prose, or a
    request id that happens to contain ``429``, cannot flip the classification —
    which is the decision that determines whether ``fallback_model`` is ever
    reached. The substring probe survives only as a LAST RESORT, for providers
    that raise a bare ``RuntimeError`` with the status in its text.

    ``message`` overrides the text of the returned exception (used to keep the
    ``ollama request failed: …`` context) without changing how the *incoming*
    exception is classified.
    """
    msg = str(exc) if message is None else message

    status = _exception_status_code(exc)
    if status == 429:
        return RateLimitError(msg)
    if status in _TRANSIENT_STATUS:
        return LLMTransientError(msg)

    name = type(exc).__name__
    if (type(exc).__module__ or "").startswith("google."):
        if name in _GENAI_RATE_LIMIT_ERROR_NAMES:
            return RateLimitError(msg)
        if name in _GENAI_TRANSIENT_ERROR_NAMES:
            return LLMTransientError(msg)

    if _is_httpx_transient(exc):
        return LLMTransientError(msg)

    low = msg.lower()
    if "429" in msg or "resource_exhausted" in low or "rate limit" in low or "rate_limit" in low:
        return RateLimitError(msg)
    if _TRANSIENT_TEXT_RE.search(msg):
        return LLMTransientError(msg)
    return LLMCallError(msg)


# ---------- Stop-reason normalization ----------------------------------------

#: The four values every backend's ``stop_reason`` is collapsed to. ``length``
#: and ``blocked`` are the two that mean "this answer is not the whole answer",
#: so :func:`call` refuses to cache them (PA-PARALLAX-F4).
_UNCACHEABLE_STOP_REASONS: frozenset[str] = frozenset({"length", "blocked"})

_STOP_REASON_ALIASES: dict[str, str] = {
    # normal completion
    "stop": "stop",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "complete": "stop",
    "finish_reason_stop": "stop",
    # hit the output-token ceiling
    "length": "length",
    "max_tokens": "length",
    "max_output_tokens": "length",
    # refused / filtered by the provider
    "safety": "blocked",
    "recitation": "blocked",
    "blocklist": "blocked",
    "prohibited_content": "blocked",
    "spii": "blocked",
    "image_safety": "blocked",
    "content_filter": "blocked",
    "refusal": "blocked",
}


def _normalize_stop_reason(raw: object) -> str:
    """Collapse a provider finish/done reason to the four-value contract.

    Enum members (``google.genai``'s ``FinishReason``) are read via ``.name`` so
    the value does not depend on the SDK's ``__str__``; anything unrecognized —
    including ``None`` — is ``"unknown"`` rather than being optimistically read
    as a clean stop.
    """
    if raw is None:
        return "unknown"
    key = str(getattr(raw, "name", raw)).strip().lower()
    return _STOP_REASON_ALIASES.get(key, "unknown")


_DEFAULT_CACHE_PATH = pathlib.Path.home() / ".parallax" / "llm_cache.sqlite"
_db_lock = threading.Lock()


def _cache_path() -> pathlib.Path:
    env = os.environ.get("PARALLAX_LLM_CACHE")
    if env:
        return pathlib.Path(env)
    return _DEFAULT_CACHE_PATH


def _connect_cache() -> sqlite3.Connection:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    # WAL + busy_timeout so concurrent readers/writers (ablation sweeps,
    # threshold sweeps) don't spuriously fail with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_cache (
            model         TEXT NOT NULL,
            prompt_hash   TEXT PRIMARY KEY,
            response_json TEXT NOT NULL,
            created_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_cache_model ON llm_cache(model)"
    )
    return conn


def _generation_options_identity(temperature: float, max_output_tokens: int) -> str:
    """Normalized, response-affecting generation options folded into the cache key
    for the API backends (gemini) whose request payload carries them.

    Same invariant #87 established for ollama (cache key == request payload):
    ``temperature`` and the output-token budget both change the model's response
    for identical ``(model, messages, schema)``, so a rerun under different values
    must re-dispatch rather than replay a stale (wrong-temperature or truncated)
    entry. Values are coerced to their canonical numeric type so equivalent inputs
    (``0`` vs ``0.0``) collapse to one identity and don't spuriously miss the
    cache. The ollama path folds these SAME two knobs in via
    :func:`_ollama_provider_identity`, alongside its endpoint + think dimensions.
    """
    opts = {
        "temperature": float(temperature),
        "max_output_tokens": int(max_output_tokens),
    }
    return f"opts={json.dumps(opts, sort_keys=True)}"


def _messages_digest(messages: list[dict]) -> str:
    """SHA-256 over the rendered messages.

    ``sort_keys=True`` keeps the digest independent of dict insertion order, so
    two halves of the codebase that build the same message with the keys in a
    different order still land on one cache entry.
    """
    return hashlib.sha256(
        json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _hash_prompt(
    model: str,
    messages: list[dict],
    response_schema: dict | None,
    cache_key: str | None,
    temperature: float = 0.0,
    max_output_tokens: int = 2048,
) -> str:
    # ``temperature`` / ``max_output_tokens`` default to call()'s defaults so
    # existing 4-arg callers keep working; call() always passes them explicitly
    # so the key matches the dispatch config.
    if cache_key is not None:
        # PA-PARALLAX-F2: a pin names the RUN's identity for reporting; it must
        # not REPLACE the payload identity. With the messages folded in, editing
        # a system prompt, re-rendering different evidence text, or crossing
        # midnight into a new ``today`` all invalidate the entry instead of
        # replaying a pre-edit answer forever under the same pin.
        raw = f"{model}::{cache_key}::msgs={_messages_digest(messages)}"
    else:
        msg_blob = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        schema_blob = json.dumps(response_schema or {}, sort_keys=True)
        raw = f"{model}::{msg_blob}::{schema_blob}"
    # Fold response-affecting config the (model, messages, schema) tuple does not
    # capture into the key, so a rerun under different knobs re-dispatches instead
    # of replaying a stale entry (invariant: cache key == request payload). Every
    # backend's request carries the generation options (temperature + output-token
    # budget); ollama additionally carries ambient endpoint config (base URL, think
    # knob). The ollama identity bundles the generation options together with those
    # extra dimensions — its exact byte form is kept intact so #87's existing cache
    # rows stay reachable — so it stays a single self-contained component; every
    # other backend folds in the generation options here.
    if model.startswith(_OLLAMA_PREFIXES):
        raw = f"{raw}::{_ollama_provider_identity(temperature, max_output_tokens)}"
    else:
        raw = f"{raw}::{_generation_options_identity(temperature, max_output_tokens)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(conn: sqlite3.Connection, prompt_hash: str) -> dict | None:
    row = conn.execute(
        "SELECT response_json FROM llm_cache WHERE prompt_hash = ?",
        (prompt_hash,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _cache_put(
    conn: sqlite3.Connection,
    model: str,
    prompt_hash: str,
    response: dict,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO llm_cache (model, prompt_hash, response_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            model,
            prompt_hash,
            json.dumps(response, ensure_ascii=False),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )


# ---------- Provider dispatch ------------------------------------------------


def _messages_to_gemini(messages: list[dict]) -> tuple[str | None, str]:
    """Split messages into (system_instruction, user_prompt)."""
    system_parts: list[str] = []
    user_parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            system_parts.append(content)
        else:
            user_parts.append(content)
    system = "\n\n".join(system_parts) if system_parts else None
    user = "\n\n".join(user_parts)
    return system, user


def _call_gemini(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_output_tokens: int,
) -> dict:
    try:
        from google import genai  # type: ignore[import-not-found]
        from google.genai import types as gtypes  # type: ignore[import-not-found]
    except Exception as exc:
        raise LLMConfigError(f"google-genai SDK not importable: {exc}") from exc

    api_key = _next_gemini_key()

    system, user = _messages_to_gemini(messages)
    client = genai.Client(api_key=api_key)
    config = gtypes.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        system_instruction=system,
    )
    try:
        resp = client.models.generate_content(model=model, contents=user, config=config)
    except Exception as exc:
        raise _classify_provider_error(exc) from exc

    usage = getattr(resp, "usage_metadata", None)
    pt = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
    ot = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
    return {
        "text": resp.text or "",
        "raw": {"candidates": str(getattr(resp, "candidates", None))[:400]},
        "model": model,
        "prompt_tokens": pt,
        "completion_tokens": ot,
        "stop_reason": _gemini_stop_reason(resp),
    }


def _gemini_stop_reason(resp: object) -> str:
    """Normalized stop reason for a Gemini response (PA-PARALLAX-F4).

    A prompt-level block is reported on ``prompt_feedback.block_reason`` and
    leaves the candidate list empty, so it is checked first; otherwise the first
    candidate's ``finish_reason`` decides. Both were previously visible only as a
    truncated ``repr`` inside ``raw``, which nothing read — so a ``MAX_TOKENS``
    cut was indistinguishable from a complete answer.
    """
    block = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
    if block:
        return "blocked"
    candidates = getattr(resp, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    return _normalize_stop_reason(finish)


# ---------- Local (Ollama) provider -----------------------------------------

#: Env vars selecting the Ollama base URL, checked in order. Operator config —
#: a non-empty value wins. Default points at GB10.
_OLLAMA_BASE_URL_ENVS: tuple[str, ...] = (
    "PARALLAX_OLLAMA_BASE_URL",
    "OLLAMA_BASE_URL",
)
_DEFAULT_OLLAMA_BASE_URL = "http://192.168.1.134:11434"
#: Routing prefixes that select the local Ollama provider. Stripped before the
#: model name is sent on the wire; the original prefixed name is echoed back so
#: cache keys and run reports stay stable.
_OLLAMA_PREFIXES: tuple[str, ...] = ("ollama:", "local:")
_DEFAULT_OLLAMA_TIMEOUT = 300.0
_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _ollama_base_url() -> str:
    """Return the normalized Ollama base URL used for BOTH the request path and
    the cache identity, so the two can never disagree about the endpoint.

    Env vars in :data:`_OLLAMA_BASE_URL_ENVS` are checked in order — the first
    non-empty value wins — else the GB10 default. Normalization is deliberate so
    equivalent configs don't spuriously miss the cache: surrounding whitespace is
    stripped, trailing slashes removed, and the value lower-cased. Ollama base
    URLs are ``scheme://host:port`` (scheme + host + port are case-insensitive)
    with no case-sensitive path in normal use, so lower-casing only collapses
    equivalent endpoints and never merges distinct ones.
    """
    for env in _OLLAMA_BASE_URL_ENVS:
        val = os.environ.get(env, "").strip()
        if val:
            return val.rstrip("/").lower()
    return _DEFAULT_OLLAMA_BASE_URL


def _strip_ollama_prefix(model: str) -> str:
    for prefix in _OLLAMA_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix) :]
    return model


def _ollama_think_state() -> str:
    """Normalize ``PARALLAX_OLLAMA_THINK`` to its canonical wire state.

    Single source of truth for the tri-state that both ``_call_ollama`` (which
    sends ``think=true`` / ``think=false`` or omits the key) and the cache
    identity (:func:`_hash_prompt`) depend on: a truthy value -> ``"true"``, a
    falsey value -> ``"false"``, anything else including unset -> ``"unset"``
    (model default). Keeping the two sites on one normalizer stops the wire
    payload and the cache key from disagreeing.
    """
    think_env = os.environ.get("PARALLAX_OLLAMA_THINK", "").strip().lower()
    if think_env in _TRUTHY:
        return "true"
    if think_env in _FALSEY:
        return "false"
    return "unset"


def _ollama_options(temperature: float, max_output_tokens: int) -> dict:
    """Generation ``options`` sent to Ollama's ``/api/chat``.

    Single source of truth shared by the request payload (``_call_ollama``) and
    the cache identity (:func:`_ollama_provider_identity`), so the wire call and
    the cache key can never disagree about the generation knobs. Values are
    coerced to their canonical numeric type (``float`` temperature, ``int``
    ``num_predict``) so equivalent inputs (e.g. ``0`` vs ``0.0``) don't
    spuriously miss the cache.
    """
    return {"temperature": float(temperature), "num_predict": int(max_output_tokens)}


def _ollama_provider_identity(temperature: float, max_output_tokens: int) -> str:
    """Normalized, response-affecting Ollama config that must join the cache
    identity so a rerun under different config re-dispatches instead of replaying
    a stale answer. Built from the SAME normalizers/builders ``_call_ollama`` uses
    for the request, so the wire call and the cache key cannot disagree.

    Components (each normalized to one canonical form so equivalent configs don't
    spuriously miss):

    * ``base`` — the endpoint (:func:`_ollama_base_url`). Endpoint-local: GB10 vs
      another host can serve different weights/revisions for the SAME tag, so the
      same ``ollama:``/``local:`` tag against a different base URL is a different
      answer and must be a different key.
    * ``think`` — the reasoning tri-state (:func:`_ollama_think_state`).
    * ``opts`` — the generation options (:func:`_ollama_options`): ``temperature``
      and ``num_predict``. These change the response for identical
      ``(model, messages, schema)`` — e.g. a small ``num_predict`` caches a
      truncated answer that must NOT be replayed for a rerun with a larger budget.

    Deliberately EXCLUDED — does not change the content of a successful response:

    * ``PARALLAX_OLLAMA_TIMEOUT`` — transport deadline only; a successful response
      is byte-identical regardless of the timeout.

    Scope note: this folds generation options into the OLLAMA identity alongside
    the endpoint + think dimensions. The API backends fold the SAME generation
    options into their key via :func:`_generation_options_identity`; this helper
    keeps its exact byte form so #87's existing ollama cache rows stay reachable.
    """
    opts_blob = json.dumps(_ollama_options(temperature, max_output_tokens), sort_keys=True)
    return f"base={_ollama_base_url()}|think={_ollama_think_state()}|opts={opts_blob}"


def _call_ollama(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_output_tokens: int,
) -> dict:
    """Call a local model served by Ollama via ``POST /api/chat``.

    ``base_url`` comes from ``PARALLAX_OLLAMA_BASE_URL`` / ``OLLAMA_BASE_URL``
    (operator config, default GB10) and is **not validated** — do not wire it to
    untrusted input (SSRF). The ``ollama:`` / ``local:`` routing prefix is
    stripped from the model name before it is sent; the original prefixed name is
    returned in the ``model`` field so cache keys and reports stay stable.

    ``PARALLAX_OLLAMA_THINK`` optionally controls reasoning models: an explicit
    falsey value sends ``think=false`` (so e.g. qwen3 emits its answer directly
    instead of spending the token budget on a hidden ``thinking`` field that
    leaves ``content`` empty); a truthy value sends ``think=true``. When unset
    the key is omitted and the model's own default applies.
    """
    try:
        import httpx  # type: ignore[import]
    except Exception as exc:  # pragma: no cover - httpx is a declared dep
        raise LLMConfigError(f"httpx not importable: {exc}") from exc

    base_url = _ollama_base_url()
    served_model = _strip_ollama_prefix(model)
    timeout = float(
        os.environ.get("PARALLAX_OLLAMA_TIMEOUT", str(_DEFAULT_OLLAMA_TIMEOUT))
    )
    payload = {
        "model": served_model,
        "messages": [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in messages
        ],
        "stream": False,
        "options": _ollama_options(temperature, max_output_tokens),
    }
    think = _ollama_think_state()
    if think == "true":
        payload["think"] = True
    elif think == "false":
        payload["think"] = False
    try:
        resp = httpx.post(f"{base_url}/api/chat", json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        # Type-first: a read timeout or a refused connection to GB10 is a
        # transient capacity event, not a permanent failure of the request.
        raise _classify_provider_error(
            exc, message=f"ollama request failed: {exc}"
        ) from exc

    if resp.status_code == 429:
        raise RateLimitError(f"ollama 429: {resp.text[:200]}")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"ollama HTTP {resp.status_code}: {resp.text[:200]}"
        if resp.status_code in _TRANSIENT_STATUS:
            raise LLMTransientError(detail) from exc
        raise LLMCallError(detail) from exc

    try:
        body = resp.json()
    except ValueError as exc:
        raise LLMCallError(f"ollama returned non-JSON body: {exc}") from exc

    message = body.get("message") if isinstance(body, dict) else None
    text = message.get("content", "") if isinstance(message, dict) else ""
    return {
        "text": text or "",
        "raw": {
            "done_reason": body.get("done_reason"),
            "served_model": body.get("model"),
        },
        "model": model,
        "prompt_tokens": int(body.get("prompt_eval_count", 0) or 0),
        "completion_tokens": int(body.get("eval_count", 0) or 0),
        "stop_reason": _normalize_stop_reason(body.get("done_reason")),
    }


#: Model-name prefixes ``_dispatch`` knows how to route. Named in the
#: unsupported-prefix error so a typo'd model reports its own fix.
_SUPPORTED_PREFIXES: tuple[str, ...] = ("gemini-", *_OLLAMA_PREFIXES)


def _dispatch(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_output_tokens: int,
) -> dict:
    if model.startswith("gemini-"):
        return _call_gemini(
            model,
            messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
    if model.startswith(_OLLAMA_PREFIXES):
        return _call_ollama(
            model,
            messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
    # The claude- arm and its adapter were removed 2026-09-09 (PA-PARALLAX-F7):
    # the SDK they lazy-imported was never a declared dependency, so the branch
    # could not run on any supported install. A claude-* model now fails here,
    # immediately and by name, instead of after three retries and 10s of sleep.
    raise LLMConfigError(
        f"unsupported model prefix: {model} "
        f"(supported prefixes: {', '.join(_SUPPORTED_PREFIXES)})"
    )


def _is_retryable(exc: BaseException) -> bool:
    """Retry predicate: transient failures yes, deterministic config no.

    ``LLMConfigError`` is excluded (PA-PARALLAX-F6) — a missing key, an absent
    SDK or an unknown prefix cannot be fixed by waiting, so retrying it only
    charges the operator two 5s backoff windows per call before showing the
    same message.
    """
    if isinstance(exc, LLMConfigError):
        return False
    return isinstance(exc, (LLMCallError, RateLimitError))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=5, max=60),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
def _dispatch_with_retry(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_output_tokens: int,
    prompt_hash: str | None = None,
) -> dict:
    """Dispatch once, validate the response, and let tenacity handle the rest.

    ``prompt_hash`` is carried purely for the operator-facing WARNING: an empty
    response is a silent failure whose only trace used to be a permanently
    cached ``''``, and the hash is what makes the offending row findable.
    """
    result = _dispatch(
        model,
        messages,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    result.setdefault("stop_reason", "unknown")
    if not (result.get("text") or "").strip():
        # PA-PARALLAX-F3: raising (rather than returning) keeps the empty answer
        # out of llm_cache AND gives the provider the two remaining tenacity
        # attempts — a blocked or budget-eaten generation is often transient.
        logger.warning(
            "empty response text from model=%s prompt_hash=%s stop_reason=%s "
            "— not cached",
            model,
            prompt_hash or "(unhashed)",
            result["stop_reason"],
        )
        raise LLMCallError(
            f"empty response text from {model} (prompt_hash={prompt_hash})"
        )
    return result


def call(
    model: str,
    messages: list[dict],
    *,
    response_schema: dict | None = None,
    cache_key: str | None = None,
    fallback_model: str | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 2048,
) -> dict:
    """Unified LLM call. Returns a dict with keys:

    ``text``, ``raw``, ``model``, ``prompt_tokens``, ``completion_tokens``,
    ``stop_reason``, ``_cached``.

    ``stop_reason`` is normalized to ``stop`` / ``length`` / ``blocked`` /
    ``unknown`` for every backend. A ``length`` or ``blocked`` response is
    returned to the caller but deliberately NOT cached: it is a partial answer,
    and caching it would replay the truncation forever with no signal
    (PA-PARALLAX-F4). An empty response is not cached either — it raises so
    tenacity retries (PA-PARALLAX-F3).

    Concurrency: the read-miss → dispatch → write sequence happens inside a
    single ``_db_lock`` span with a re-check after the (blocking) lock is
    acquired, so a thundering herd of identical calls only dispatches once.
    The ``_dispatch_with_retry`` call itself is invoked under the lock — this
    is intentional: for LongMemEval-style sweeps the right per-host concurrency
    is 2–4, and serializing dispatch across threads sharing the same cache file
    is cheaper than N parallel API calls that would all race to insert the
    same row.
    """
    prompt_hash = _hash_prompt(
        model, messages, response_schema, cache_key, temperature, max_output_tokens
    )

    with _db_lock:
        conn = _connect_cache()
        try:
            cached = _cache_get(conn, prompt_hash)
            if cached is not None:
                cached = dict(cached)
                cached["_cached"] = True
                return cached

            try:
                result = _dispatch_with_retry(
                    model,
                    messages,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    prompt_hash=prompt_hash,
                )
                store_hash = prompt_hash
                store_model = model
            except (RateLimitError, LLMTransientError) as exc:
                # PA-PARALLAX-F5: capacity events reach the fallback whether the
                # provider called them 429 or 503/overloaded/timeout. A
                # LLMConfigError is NOT caught here — falling back cannot fix a
                # missing key or an unknown prefix, it only hides it.
                if fallback_model is None:
                    raise
                logger.warning(
                    "%s on %s; falling back to %s",
                    type(exc).__name__,
                    model,
                    fallback_model,
                )
                # Store the fallback response under a fallback-specific hash so a
                # later call with the primary model does NOT return the Flash
                # answer labelled as Pro. This keeps fallback results cheap to
                # re-serve while preserving primary-model cache correctness.
                store_hash = _hash_prompt(
                    fallback_model,
                    messages,
                    response_schema,
                    cache_key,
                    temperature,
                    max_output_tokens,
                )
                store_model = fallback_model
                result = _dispatch_with_retry(
                    fallback_model,
                    messages,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    prompt_hash=store_hash,
                )
                result["fallback_from"] = model

            result["_cached"] = False
            stop_reason = result.get("stop_reason", "unknown")
            if stop_reason in _UNCACHEABLE_STOP_REASONS:
                logger.warning(
                    "stop_reason=%s from model=%s prompt_hash=%s — response "
                    "returned but NOT cached (partial answer)",
                    stop_reason,
                    store_model,
                    store_hash,
                )
                return result

            _cache_put(
                conn,
                store_model,
                store_hash,
                {k: v for k, v in result.items() if k != "_cached"},
            )
            return result
        finally:
            conn.close()
