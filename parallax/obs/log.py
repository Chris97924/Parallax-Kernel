"""Structured JSON logging for Parallax.

Each record is a single-line JSON object with fixed keys (``ts``, ``level``,
``logger``, ``msg``) plus any ``extra=...`` kwargs flattened onto the top
level. Emits to stderr by default so it never collides with stdout payloads
in CLI-driven callers.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

__all__ = [
    "get_logger",
    "JSONFormatter",
    "REDACTED_PREFIX",
    "UNSAFE_EVENT_NAME",
    "exc_fields",
    "safe_log_warning",
    "sanitize_log_extras",
]

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a JSON-formatted logger. Idempotent per-name."""
    logger = logging.getLogger(name)
    if not any(getattr(h, "_parallax_json", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JSONFormatter())
        handler._parallax_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


# ---------------------------------------------------------------------------
# Payload sanitisation
# ---------------------------------------------------------------------------
# ``JSONFormatter`` above flattens EVERY non-reserved record attribute onto the
# top-level object and serialises with ``default=str``, so whatever a caller
# hands to ``extra={...}`` reaches stderr verbatim — including values that are
# not JSON-serialisable, which ``default=str`` stringifies rather than drops.
# That makes ``extra`` an unbounded sink into a durable, broadly-readable
# destination (container logs / journald).
#
# The helpers below bound it. They live here rather than beside any one caller
# because ``dual_read.py`` imports from ``dual_read_decision_log.py``, so a
# sanitiser hosted in the former could not be shared with the latter without an
# import cycle — which is exactly why that module ended up with a hand-rolled
# copy of the same try/except-pass shape in the first place.
#
# Default-deny is deliberate: a key no one has classified yet is redacted until
# somebody consciously adds it to the allowlist. That review checkpoint is what
# ``docs/m5-prep/m5-entry-spec.md`` §3.1a asks for and what call-site-by-call-site
# discipline had failed to provide.

REDACTED_PREFIX = "<redacted:"

_DIGEST_CHARS = 12
_MAX_SAFE_STR_LEN = 200

# String-valued extras whose vocabulary is bounded: enum members, generated
# UUIDs, Python type names, schema versions. None of these can carry stored
# memory content, user queries, credentials, URLs or filesystem paths.
_SAFE_STR_KEYS = frozenset(
    {
        "arbitration_outcome",
        "conflict_event_id",
        "correlation_id",
        "crosswalk_status",
        "event",
        "exc_class",
        "outcome",
        "policy_version",
        "query_type",
        "schema_version",
        "selected_port",
        "traffic_source",
        "unreachable_reason",
        "winning_source",
    }
)

# Identifiers deliberately left un-redacted. ``user_id`` is already a Prometheus
# label on the dual-read outcome counters and a field in every shadow decision
# record, so scrubbing it *here alone* would hide it from nobody who can already
# read ``/metrics`` — the S4 audit calls that theatre. Whether ``user_id`` may
# ever hold an externally-meaningful value (an email, say) is a data
# classification ruling that has to land in both places at once; this set records
# the decision explicitly instead of leaving it implicit in the allowlist above.
_IDENTIFIER_STR_KEYS = frozenset({"user_id"})

# Exception types whose ``str()`` is provably value-free, referenced by dotted
# name so ``parallax.obs`` keeps no dependency on ``parallax.router``. Matching
# is on the EXACT type: a subclass may override ``__init__`` and put anything in
# the message, so it does not inherit the allowance.
#
# Adding an entry is a security decision — the type's message must be built from
# a fixed template plus bounded, enum-like data:
#
#   * ``AphelionUnreachableError`` builds ``f"Aphelion unreachable: {reason}"``
#     from its own tag, and ``m5-entry-spec.md`` §3.1a ("Error reason
#     sanitisation") binds ``reason`` to an enum, never a response body or URL.
_MESSAGE_SAFE_EXC_TYPES = frozenset(
    {
        "parallax.router.aphelion_adapter.AphelionUnreachableError",
    }
)

# The event name reaches the sink twice — as the record's ``msg`` and as its
# ``event`` field — and on neither route does it pass through the extras
# policy. So it gets its own: the dotted-snake vocabulary every call site in
# this repo already uses (``secondary_unexpected_exception``,
# ``dual_read_decision_log.append_failed``), and nothing else.
_EVENT_NAME_PATTERN = re.compile(r"^[a-z0-9_.]{1,64}$")

#: Substituted for any event name that fails :data:`_EVENT_NAME_PATTERN`.
UNSAFE_EVENT_NAME = "unsafe_event_name"


def _render(value: object) -> str:
    """``str(value)`` that cannot raise — a hostile ``__str__`` must not cost us
    the whole log line, since sanitisation runs inside a swallowing wrapper."""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — last-resort
        return ""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:_DIGEST_CHARS]


def _redact(value: object) -> str:
    """Bounded surrogate for a value that may not be emitted verbatim.

    Keeps the two things an operator actually needs from a failure log — "is
    this the same failure as last time" (digest) and "how big was it" (length)
    — while carrying none of the content.
    """
    rendered = _render(value)
    return f"{REDACTED_PREFIX}sha256={_digest(rendered)},len={len(rendered)}>"


def _type_name(obj_type: type) -> str:
    """Dotted ``module.QualName``; bare qualname for builtins."""
    module = getattr(obj_type, "__module__", "")
    qualname = getattr(obj_type, "__qualname__", None) or getattr(obj_type, "__name__", "?")
    return qualname if module in ("builtins", "") else f"{module}.{qualname}"


def exc_fields(exc: BaseException) -> dict[str, object]:
    """Value-free description of an exception, safe to emit to a log sink.

    Always yields ``exc_class``. The message is rendered as ``exc_str`` only for
    the exact types in :data:`_MESSAGE_SAFE_EXC_TYPES`; for everything else it is
    replaced by a ``sha256`` digest plus a length, so repeated identical failures
    stay correlatable and greppable without their content reaching stderr.

    Dropping the traceback is *not* sanitisation, which is the premise the old
    call sites got wrong: a traceback leaks frame locals, but ``str(exc)`` is
    precisely where CPython puts the offending **value** for the most common
    types — ``KeyError`` renders the key, ``ValueError`` from
    ``fromisoformat``/``int()``/``json.loads`` renders the offending substring,
    ``UnicodeDecodeError`` renders byte context, and (once the secondary goes
    over the network) ``httpx.HTTPStatusError`` renders the full request URL.
    """
    exc_type = type(exc)
    # Bare name, not the dotted one: this is what the call sites emitted before
    # and what existing log consumers grep for.
    fields: dict[str, object] = {"exc_class": exc_type.__name__}
    message = _render(exc)
    if _type_name(exc_type) in _MESSAGE_SAFE_EXC_TYPES:
        fields["exc_str"] = message[:_MAX_SAFE_STR_LEN]
    else:
        fields["exc_digest"] = _digest(message)
        fields["exc_len"] = len(message)
    return fields


def sanitize_log_extras(extras: Mapping[str, object]) -> dict[str, object]:
    """Apply the payload policy to a mapping bound for ``extra={...}``.

    * ``None`` / numbers / bools pass through — bounded, no free text.
    * ``str`` passes through under an allowlisted key and within
      ``_MAX_SAFE_STR_LEN``; an allowlisted key is trusted for its *vocabulary*,
      not for unbounded size, so an outsized value is redacted anyway.
    * Everything else is redacted, because ``default=str`` in the formatter
      would otherwise stringify it — that is the route a claim mapping carrying
      a markdown body would take.
    """
    clean: dict[str, object] = {}
    for key, value in extras.items():
        if value is None or isinstance(value, int | float):  # bool is an int subclass
            clean[key] = value
        elif isinstance(value, str):
            allowed = key in _SAFE_STR_KEYS or key in _IDENTIFIER_STR_KEYS
            clean[key] = value if allowed and len(value) <= _MAX_SAFE_STR_LEN else _redact(value)
        else:
            clean[key] = _redact(value)
    return clean


def _safe_event_name(event: object) -> tuple[str, dict[str, object]]:
    """Constrain the event name to this module's dotted-snake vocabulary.

    Returns ``(name_to_emit, extra_fields)``.

    ``event`` is the one value that used to reach stderr untouched, on both of
    its routes — the record's ``msg`` and its ``event`` field. A caller writing
    ``safe_log_warning(logger, f"failed: {user_query}")`` would therefore route
    straight around the whole guard, which is precisely the call-site
    dependence this module exists to remove.

    Rejection does not cost the record. The emit becomes
    :data:`UNSAFE_EVENT_NAME` and the offending value survives as a digest
    under ``unsafe_event``, so the line stays findable, the miswiring is
    obvious to whoever reads the log, and the value itself never lands.
    """
    if isinstance(event, str) and _EVENT_NAME_PATTERN.match(event):
        return event, {}
    return UNSAFE_EVENT_NAME, {"unsafe_event": _redact(event)}


def safe_log_warning(
    logger: logging.Logger,
    event: str,
    *,
    exc: BaseException | None = None,
    **extras: object,
) -> None:
    """Emit a sanitised structured WARNING that can never reach the caller.

    Two independent guarantees, both load-bearing:

    * **Crash-safe.** ``logger.warning`` can itself raise if a handler is closed
      or misconfigured (process shutdown, dead aggregator). Callers on a
      fail-closed request path must never lose their canonical result to an
      observability failure, so the whole emit is wrapped.
    * **Payload-safe.** Every value routes through :func:`sanitize_log_extras`,
      and exception detail is passed as ``exc=`` so :func:`exc_fields` decides
      what may be rendered. A caller that still hand-writes ``exc_str=str(exc)``
      gets it redacted here — the guard does not depend on call-site discipline,
      which is the property that makes it hold for call sites not yet written.

    ``event`` is held to that same standard rather than trusted: it must match
    the dotted-snake vocabulary (``^[a-z0-9_.]{1,64}$``), because it reaches the
    sink as both ``msg`` and the ``event`` field without passing through the
    extras policy. Anything else is emitted as :data:`UNSAFE_EVENT_NAME` with
    the original reduced to a digest under ``unsafe_event`` — see
    :func:`_safe_event_name`. Nothing is dropped; only the value is withheld.

    Known trade, inherited from the shape this replaces: because the emit is
    swallowed, "no warnings in the log" is not evidence that no warnings
    occurred. A broken logging pipeline stays invisible here by design.
    """
    try:
        # First, because ``event`` is the only argument that reaches the record
        # on two routes and the rest of the policy never sees it.
        event_name, event_fields = _safe_event_name(event)
        fields = sanitize_log_extras(extras)
        # Both updates land after sanitisation, not through it: their values are
        # bounded by construction and must win over any hand-passed key.
        fields.update(event_fields)
        if exc is not None:
            fields.update(exc_fields(exc))
        logger.warning(event_name, extra={"event": event_name, **fields})
    except Exception:  # noqa: BLE001 — last-resort: even the logger may be broken
        pass
