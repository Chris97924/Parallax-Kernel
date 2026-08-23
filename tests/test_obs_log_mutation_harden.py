"""Mutation-hardening for ``parallax.obs.log`` (land/20260823 wave 4, S1).

Additive companion to ``tests/test_obs_log.py`` and
``tests/test_obs_log_sanitize.py``. Every test here exists because a *semantic*
mutant of the module survived those two suites plus
``tests/router/test_log_payload_sanitize.py`` and
``tests/router/test_exc_info_disclosure_106.py``. Each pins the specific
constant, boundary, guard or ordering the mutant moved.

Tally — applied 24 / killed by the pre-existing suites 12 / killed by the tests
below 12 / equivalent (excluded) 0 / unaddressed 0.

What the existing suites could not see
--------------------------------------
Three blind spots, all of the same family:

* **Constants are never asserted.** ``_MAX_SAFE_STR_LEN`` (200) is only
  exercised with ``"x" * 5000``, which is over any plausible cap, so widening
  the cap to 1000 — or narrowing the comparison from ``<=`` to ``<`` — changes
  nothing observable. ``_DIGEST_CHARS`` (12) is only ever checked for
  *stability* (``a == b``, ``a != c``), never for *width*, so an 8-character
  digest passes.
* **The formatter is only fed friendly records.** No test hands it a
  private (``_``-prefixed) attribute, a non-JSON-serialisable value, or
  non-ASCII text, so dropping ``key.startswith("_")``, dropping ``default=str``
  and flipping ``ensure_ascii`` are all invisible. Nor is ``ts`` inspected
  beyond ``"ts" in payload``, so the UTC timezone can be dropped entirely.
* **Only the sink is observed, never the route to it.** ``_safe_log``
  deliberately dispatches through ``logger.warning`` / ``logger.error`` and
  deliberately applies the bounded event/exception fields *after*
  sanitisation. Both are documented in the module as load-bearing, and both
  can be reversed without changing a single serialised line, so a test that
  reads only the emitted JSON cannot tell.

Expected values below are LITERALS — ``200``, ``12``, ``"+00:00"`` — and the
digests are recomputed from ``hashlib`` in the test rather than imported from
the module. Deriving an expectation from the constant under test is exactly
what made the original suites blind to these mutants; do not "tidy" these back
into references to ``_MAX_SAFE_STR_LEN`` or ``_DIGEST_CHARS``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import logging
from unittest.mock import patch

import pytest

from parallax.obs.log import (
    JSONFormatter,
    exc_fields,
    get_logger,
    safe_log_error,
    safe_log_warning,
    sanitize_log_extras,
)

#: A real member of the closed event vocabulary, so tests that are not about
#: the event name exercise the accepting path.
CANONICAL_EVENT = "secondary_unexpected_exception"

#: Distinctive needle: absence from the serialised line is the assertion.
NEEDLE = "PARALLAX-W4-NEEDLE-3c81ff"


def _sha12(text: str) -> str:
    """First 12 hex characters of the SHA-256 of ``text``.

    Computed here from ``hashlib`` on purpose. Importing ``_DIGEST_CHARS`` to
    build the expectation would make this test move with the constant, which is
    the defect it exists to close.
    """
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _make_record(msg: str = "hello", **attrs: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="parallax.test", level=logging.INFO, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )
    for key, value in attrs.items():
        setattr(record, key, value)
    return record


def _emit(
    logger_name: str, level: str = "warning", event: object = CANONICAL_EVENT, **kwargs: object
) -> tuple[str, dict[str, object]]:
    """Emit one record through the safe helpers; return (raw line, payload)."""
    logger = get_logger(logger_name)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
    emit = safe_log_warning if level == "warning" else safe_log_error
    try:
        emit(logger, event, **kwargs)  # type: ignore[arg-type]
        handler.flush()
    finally:
        logger.removeHandler(handler)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one record, got {lines!r}"
    return lines[0], json.loads(lines[0])


# ---------------------------------------------------------------------------
# The safe-string cap is a number, not "something large"
# (mutants: _MAX_SAFE_STR_LEN 200 -> 1000; ``<=`` -> ``<``)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_allowlisted_string_of_exactly_two_hundred_chars_passes_through() -> None:
    """200 characters is *inside* the cap — the comparison is ``<=``, not ``<``.

    The existing cap test uses ``"x" * 5000``, which is redacted under any cap
    from 1 to 4999, so it cannot see the boundary move by one. This is the
    assertion that pins the inclusive end.
    """
    value = "o" * 200
    out = sanitize_log_extras({"outcome": value})
    assert out["outcome"] == value, "a value exactly at the cap must survive verbatim"


@pytest.mark.unit
def test_allowlisted_string_of_two_hundred_and_one_chars_is_redacted() -> None:
    """201 characters is outside the cap, so the cap is exactly 200.

    Paired with the test above, this is what forbids widening
    ``_MAX_SAFE_STR_LEN`` to 1000: an allowlisted key is trusted for its
    *vocabulary*, and a 201-character value is by construction outside any
    bounded vocabulary.
    """
    out = sanitize_log_extras({"outcome": "o" * 201})
    assert str(out["outcome"]).startswith("<redacted:")
    assert ",len=201>" in str(out["outcome"])


# ---------------------------------------------------------------------------
# The redaction surrogate has a fixed shape
# (mutants: _DIGEST_CHARS 12 -> 8; the ``len=`` half dropped)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_redaction_surrogate_is_a_twelve_char_digest_plus_the_length() -> None:
    """Both halves of the surrogate are contract, and so is the digest width.

    The surrogate is what an operator greps for, so its rendering is an
    interface: "is this the same failure as last time" needs the digest and
    "how big was it" needs the length. The existing tests only assert the
    digest is *stable*, which an 8-character digest also is.
    """
    out = sanitize_log_extras({"unclassified_key": "abcdefghij"})
    expected = "<redacted:sha256=" + _sha12("abcdefghij") + ",len=10>"
    assert out["unclassified_key"] == expected


@pytest.mark.unit
def test_exception_digest_is_twelve_hex_chars_of_sha256() -> None:
    """``exc_digest`` is the same 12-character SHA-256 prefix.

    Recomputed from ``hashlib`` rather than compared against another call to
    ``exc_fields``, so shortening the digest fails here instead of staying
    self-consistently green.
    """
    fields = exc_fields(RuntimeError("boom"))
    assert fields["exc_digest"] == _sha12("boom")
    assert len(str(fields["exc_digest"])) == 12
    assert fields["exc_len"] == 4


# ---------------------------------------------------------------------------
# Bounded fields win over hand-passed extras
# (mutant: sanitised extras applied last instead of first)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exception_fields_override_a_caller_supplied_exc_class() -> None:
    """``exc_fields`` lands *after* sanitisation and must win.

    A caller passing ``exc_class="SPOOFED"`` alongside a real exception must
    not be able to relabel the record: the module states the bounded values
    "must win over any hand-passed key", and reversing the two updates leaves
    the caller's redacted string in the field instead. Nothing in the emitted
    line changes shape when that happens, which is why no existing test sees it.
    """
    _, payload = _emit(
        "parallax.test.w4.precedence",
        exc=RuntimeError("boom"),
        exc_class="SPOOFED",
    )
    assert payload["exc_class"] == "RuntimeError"
    assert payload["exc_digest"] == _sha12("boom")


@pytest.mark.unit
def test_rejected_event_digest_overrides_a_caller_supplied_unsafe_event() -> None:
    """The ``unsafe_event`` surrogate is likewise not overridable by a caller.

    Otherwise a miswired call site could pass its own ``unsafe_event=`` and
    hide which value was actually rejected — the one thing that field exists
    to record.
    """
    _, payload = _emit(
        "parallax.test.w4.precedence2",
        event="definitely_not_a_known_event",
        unsafe_event="SPOOFED",
    )
    assert payload["event"] == "unsafe_event_name"
    assert payload["unsafe_event"] == (
        "<redacted:sha256=" + _sha12("definitely_not_a_known_event") + ",len=28>"
    )


# ---------------------------------------------------------------------------
# The emit goes through the level-specific method, not logger.log
# (mutant: getattr(logger, method) -> logger.log(level, ...))
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_warning_is_dispatched_through_logger_warning() -> None:
    """``logger.warning`` is the seam tests patch to observe an emit.

    The module says so explicitly: ``logger.log`` still lands the record at the
    right level, so switching to it is invisible at the sink and shows up only
    as an emit that patched-attribute tests silently stop seeing. That failure
    mode is why this is asserted on the route rather than on the output.
    """
    logger = get_logger("parallax.test.w4.seam.warning")
    with patch.object(logger, "warning") as warned:
        safe_log_warning(logger, CANONICAL_EVENT, outcome="ok")
    warned.assert_called_once()
    assert warned.call_args.args[0] == CANONICAL_EVENT
    assert warned.call_args.kwargs["extra"]["event"] == CANONICAL_EVENT


@pytest.mark.unit
def test_error_is_dispatched_through_logger_error() -> None:
    """Same seam at ERROR — and the severity itself is not negotiable.

    ``safe_log_error`` exists so the #106.5 audit-write failures could drop
    ``exc_info=True`` without dropping a level; redacting the payload must not
    quietly become a downgrade to WARNING.
    """
    logger = get_logger("parallax.test.w4.seam.error")
    with patch.object(logger, "error") as errored:
        safe_log_error(logger, CANONICAL_EVENT, outcome="ok")
    errored.assert_called_once()
    assert errored.call_args.args[0] == CANONICAL_EVENT


# ---------------------------------------------------------------------------
# get_logger does not clobber caller configuration
# (mutants: unconditional setLevel; propagate True)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_logger_leaves_a_caller_configured_level_alone() -> None:
    """A level set by the caller survives ``get_logger``.

    The guard is ``if logger.level == logging.NOTSET``: INFO is a *default*,
    not a policy. Dropping the guard silently raises a debugging logger back to
    INFO, and every existing test uses a fresh logger, where both versions
    behave identically.
    """
    name = "parallax.test.w4.level.preconfigured"
    logging.getLogger(name).setLevel(logging.DEBUG)
    try:
        logger = get_logger(name)
        assert logger.level == 10, "DEBUG (10) must survive; an unconditional set makes it 20"
    finally:
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.mark.unit
def test_get_logger_defaults_an_unconfigured_logger_to_info() -> None:
    """The other half of the same guard: NOTSET does get the INFO default."""
    logger = get_logger("parallax.test.w4.level.fresh")
    assert logger.level == 20


@pytest.mark.unit
def test_get_logger_does_not_propagate_to_the_root_logger() -> None:
    """Propagation stays off, or every record is emitted twice.

    Once as JSON by this module's handler and once by whatever the root logger
    is configured with — which in a container is the plain-text default, so the
    duplicate carries the same payload outside the JSON contract.
    """
    assert get_logger("parallax.test.w4.propagate").propagate is False


# ---------------------------------------------------------------------------
# The formatter's own guards
# (mutants: "_" prefix skip dropped; default=str dropped; ensure_ascii flipped;
#  UTC dropped)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_private_record_attributes_are_not_serialised() -> None:
    """A ``_``-prefixed attribute is internal state, not payload.

    The handler this module attaches carries ``_parallax_json``; anything else
    that stashes state on a record — a filter, a third-party handler — lands in
    the same namespace. Emitting them turns an implementation detail into log
    content, and none of the existing formatter tests set one.
    """
    record = _make_record(user_id="u1")
    record._internal_state = NEEDLE  # type: ignore[attr-defined]
    out = JSONFormatter().format(record)
    payload = json.loads(out)
    assert "_internal_state" not in payload
    assert NEEDLE not in out
    assert payload["user_id"] == "u1", "non-private extras still pass through"


@pytest.mark.unit
def test_non_serialisable_attribute_is_stringified_not_fatal() -> None:
    """``default=str`` is a guard, not a convenience.

    ``format`` runs inside ``logging``'s own machinery, so a ``TypeError``
    here becomes a logging error on a path whose whole point is that it cannot
    cost the caller anything. Every existing formatter test passes plain
    strings, so removing ``default=str`` breaks nothing they can observe.
    """

    class NotSerialisable:
        def __str__(self) -> str:
            return "opaque-object"

    payload = json.loads(JSONFormatter().format(_make_record(thing=NotSerialisable())))
    assert payload["thing"] == "opaque-object"


@pytest.mark.unit
def test_non_ascii_is_emitted_verbatim_not_escaped() -> None:
    """``ensure_ascii=False`` keeps the line readable and byte-honest.

    Records carry non-ASCII by way of ``msg`` (this repo already handles a
    cp950 CLI path). Escaping to ``\\uXXXX`` still round-trips through
    ``json.loads``, so a parse-then-compare test cannot tell the two apart —
    the assertion has to be on the raw serialised bytes.
    """
    out = JSONFormatter().format(_make_record(msg="統計 café"))
    assert "統計 café" in out
    assert "\\u" not in out


@pytest.mark.unit
def test_timestamp_is_utc_with_an_explicit_offset() -> None:
    """``ts`` is UTC, and says so.

    A naive local timestamp still satisfies ``"ts" in payload`` — the only
    thing the existing test asserts — while making every log line
    uninterpretable outside the machine that wrote it, and unorderable against
    lines from a differently-zoned host. The expected value is a literal, so
    the assertion cannot drift with the box's timezone.
    """
    record = _make_record()
    record.created = 1700000000.0
    payload = json.loads(JSONFormatter().format(record))
    assert payload["ts"] == "2023-11-14T22:13:20+00:00"
    assert _dt.datetime.fromisoformat(str(payload["ts"])).utcoffset() == _dt.timedelta(0)
