"""S4 audit follow-up — payload sanitisation for the structured log helpers.

Companion to ``tests/test_obs_log.py`` (which covers the formatter and the
logger factory). This module covers the *policy*: what is allowed to reach
stderr through ``extra={...}``.

Each ``test_lc*`` below pins one leak class enumerated in the S4 audit
(``E:/Workspace/.land-batch-20260805/reports/S4-safelog-audit.md``). The class
→ test mapping is reproduced in the PR body; keep them in sync.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from parallax.obs.log import (
    JSONFormatter,
    exc_fields,
    get_logger,
    safe_log_warning,
    sanitize_log_extras,
)
from parallax.router.aphelion_adapter import AphelionUnreachableError

# A distinctive needle. Every assertion below looks for this substring in the
# fully serialised record, which is what actually reaches the log sink — a
# check against the returned dict alone would miss anything the formatter
# stringifies on its own via ``default=str``.
NEEDLE = "PARALLAX-S2-NEEDLE-9f31c7"


def _capture(name: str) -> tuple[logging.Logger, io.StringIO]:
    """Attach a JSON-formatted capture handler; return (logger, buffer)."""
    logger = get_logger(name)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
    return logger, buf


def _emit(logger_name: str, event: str, **kwargs: object) -> tuple[str, dict[str, object]]:
    """Emit one warning and return (raw serialised line, parsed payload)."""
    logger, buf = _capture(logger_name)
    try:
        safe_log_warning(logger, event, **kwargs)  # type: ignore[arg-type]
        logger.handlers[-1].flush()
    finally:
        logger.removeHandler(logger.handlers[-1])
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one record, got {lines!r}"
    return lines[0], json.loads(lines[0])


def _assert_scrubbed(raw: str, payload: dict[str, object], needle: str = NEEDLE) -> None:
    """The needle is absent, and a correlatable surrogate took its place."""
    assert needle not in raw, f"sensitive value reached the log record: {raw!r}"
    assert payload["exc_digest"], "expected a sha256 surrogate so failures stay correlatable"
    assert isinstance(payload["exc_len"], int)


# ---------------------------------------------------------------------------
# Leak classes — one test per class named in the S4 audit
# ---------------------------------------------------------------------------


def test_lc1_keyerror_renders_the_key() -> None:
    """LC-1 (audit §exec-summary ¶4): ``str(KeyError)`` renders the key itself."""
    exc = KeyError(NEEDLE)
    assert NEEDLE in str(exc), "premise check: KeyError must actually echo its key"
    raw, payload = _emit("parallax.test.lc1", "lc1", exc=exc)
    _assert_scrubbed(raw, payload)
    assert payload["exc_class"] == "KeyError"


def test_lc2_valueerror_renders_the_offending_substring() -> None:
    """LC-2 (audit §exec-summary ¶4): ``int()``/``fromisoformat``/``json.loads``
    put the offending input straight into the message."""
    try:
        int(NEEDLE)
    except ValueError as caught:
        exc: ValueError = caught
    assert NEEDLE in str(exc), "premise check: ValueError must echo the offending text"
    raw, payload = _emit("parallax.test.lc2", "lc2", exc=exc)
    _assert_scrubbed(raw, payload)


def test_lc3_unicodedecodeerror_renders_byte_context() -> None:
    """LC-3 (audit §exec-summary ¶4): decode failures leak byte-level context
    about the payload being decoded."""
    try:
        (NEEDLE.encode() + b"\xff").decode("utf-8")
    except UnicodeDecodeError as caught:
        exc: UnicodeDecodeError = caught
    message = str(exc)
    assert "can't decode byte" in message, "premise check: message describes the payload bytes"
    raw, payload = _emit("parallax.test.lc3", "lc3", exc=exc)
    assert message not in raw, "byte-context message must not reach the record"
    assert payload["exc_digest"]
    assert payload["exc_class"] == "UnicodeDecodeError"


def test_lc4_raw_user_query_subject_is_not_emitted() -> None:
    """LC-4 (audit §3.1): ``_resolve_subject`` falls back to ``request.q`` — the
    raw user query string — so it is live in every unwrapped adapter frame."""
    exc = RuntimeError(f"failed resolving subject: {NEEDLE}")
    raw, payload = _emit("parallax.test.lc4", "lc4", exc=exc)
    _assert_scrubbed(raw, payload)


def test_lc5_stored_claim_body_is_not_emitted() -> None:
    """LC-5 (audit §3.1): candidate claim mappings carry the markdown body under
    ``CLAIM_CONTENT_KEY``. Passed as a mapping it would still be serialised,
    because ``JSONFormatter`` uses ``default=str``."""
    candidate = {"subject": "s", "body": f"# note\n\n{NEEDLE}\n"}
    raw, payload = _emit("parallax.test.lc5", "lc5", candidate=candidate)
    assert NEEDLE not in raw, f"claim body reached the log record: {raw!r}"
    assert str(payload["candidate"]).startswith("<redacted:")


def test_lc6_nfc_key_collision_renders_the_key_repr() -> None:
    """LC-6 (audit §3.2): ``canonical_dumps`` raises ``ValueError`` echoing a
    key ``repr`` — safe only while the keys at that call site stay literals."""
    exc = ValueError(f"NFC key collision: {NEEDLE!r}")
    raw, payload = _emit("parallax.test.lc6", "lc6", exc=exc)
    _assert_scrubbed(raw, payload)


def test_lc7_request_url_is_not_emitted() -> None:
    """LC-7 (audit §3.2 ¶3 / §5.3): once the secondary goes over the network in
    M6/M7, ``HTTPStatusError.__str__`` renders the full request URL."""

    class HTTPStatusError(Exception):
        """Message shape mirrors httpx, without depending on it at test time."""

    exc = HTTPStatusError(
        "Server error '503 Service Unavailable' for url "
        f"'https://aphelion.internal/v1/read?trace={NEEDLE}'"
    )
    raw, payload = _emit("parallax.test.lc7", "lc7", exc=exc)
    _assert_scrubbed(raw, payload)
    assert "aphelion.internal" not in raw, "endpoint host must not reach the record either"


def test_lc8_bearer_credential_is_not_emitted() -> None:
    """LC-8 (audit §3.2 ¶2, quoting ``m5-entry-spec.md:63``): the Aphelion bearer
    token must never reach a log line, by whatever route."""
    exc = ConnectionError(f"auth failed for header Authorization: Bearer {NEEDLE}")
    raw, payload = _emit("parallax.test.lc8", "lc8", exc=exc)
    _assert_scrubbed(raw, payload)
    assert "Bearer" not in raw


def test_lc9_user_id_is_a_documented_passthrough() -> None:
    """LC-9 (audit §4.2): ``user_id`` is deliberately NOT redacted.

    It is already a Prometheus label and a field in every shadow decision
    record, so redacting it here alone would change nothing an operator or an
    authorised scraper can already read — the audit calls that theatre. This
    test pins the decision so a future data-classification ruling has one
    obvious place to land, together with the metrics side.
    """
    raw, payload = _emit("parallax.test.lc9", "lc9", user_id="u-123")
    assert payload["user_id"] == "u-123"
    assert "<redacted:" not in raw


def test_lc10_filesystem_path_is_not_emitted() -> None:
    """LC-10 (audit §5.1): ``OSError`` renders the resolved path — the log-dir /
    DB-file disclosure the sibling sites share."""
    exc = OSError(f"[Errno 28] No space left on device: '/srv/{NEEDLE}/decisions.jsonl'")
    raw, payload = _emit("parallax.test.lc10", "lc10", exc=exc)
    _assert_scrubbed(raw, payload)


# ---------------------------------------------------------------------------
# Policy mechanics
# ---------------------------------------------------------------------------


def test_allowlisted_exception_type_emits_its_validated_tag() -> None:
    """``AphelionUnreachableError`` carries an enum-like tag, so the tag stays
    readable — the allowlist is not a blanket ban that would cost operators the
    one signal they need. Only the tag is emitted, never ``str(exc)``."""
    raw, payload = _emit(
        "parallax.test.allow", "allow", exc=AphelionUnreachableError("claim_schema_error")
    )
    assert payload["exc_tag"] == "claim_schema_error"
    assert "exc_str" not in payload, "the message itself must never be rendered"
    assert "exc_digest" not in payload
    assert "<redacted:" not in raw


def test_allowlisted_type_with_a_dynamic_tag_falls_back_to_digest() -> None:
    """REGRESSION (gate r1, driver-carried): the allowlist rested on every
    ``AphelionUnreachableError`` raise site being in this repo with a literal
    tag. It is not — the exception is raisable by an injected ``QueryPort``
    implemented anywhere, and ``shadow.py``'s bare ``except Exception`` hands
    whatever it constructed straight to the logger. The AST pin cannot see
    those raise sites, so the tag is validated at runtime instead.
    """
    exc = AphelionUnreachableError(f"lookup failed for {NEEDLE}")
    assert NEEDLE in str(exc), "premise check: the dynamic reason is in the message"
    raw, payload = _emit("parallax.test.allow_bad", "allow_bad", exc=exc)
    assert NEEDLE not in raw, f"dynamic tag reached the log record: {raw!r}"
    assert "exc_tag" not in payload
    _assert_scrubbed(raw, payload)


@pytest.mark.parametrize(
    ("label", "reason"),
    [
        ("uppercase", "Timeout"),
        ("whitespace", "connection refused"),
        ("punctuation", "http_5xx: https://aphelion.internal/v1"),
        ("too_long", "e" * 65),
        ("empty", ""),
        ("non_str", 42),
    ],
)
def test_only_enum_shaped_tags_are_emitted(label: str, reason: object) -> None:
    """The tag vocabulary is documented as enum-like; anything outside that
    shape is not a tag and is treated as unbounded content."""
    exc = AphelionUnreachableError("placeholder")
    exc.reason = reason  # type: ignore[assignment]
    _, payload = _emit("parallax.test.tagshape", "tagshape", exc=exc)
    assert "exc_tag" not in payload, label
    assert payload["exc_digest"], label


def test_documented_tag_vocabulary_still_passes() -> None:
    """The validator must not have broken the real tags — every reason the
    adapter documents has to keep rendering, or operators lose the breaker
    signal the allowlist exists to preserve."""
    for reason in (
        "timeout",
        "connection_error",
        "claim_loader_error",
        "claim_schema_error",
        "envelope_checksum_mismatch",
        "audit_db_write_failed",
        "unsafe_archive",
        "http_5xx",
        "package_dir_inaccessible",
    ):
        _, payload = _emit("parallax.test.tagok", "tagok", exc=AphelionUnreachableError(reason))
        assert payload["exc_tag"] == reason


def test_allowlist_does_not_extend_to_subclasses() -> None:
    """Matching is on the exact type: a subclass can override ``__init__`` and
    put anything in the message, so it must not inherit the allowance."""

    class SneakyUnreachable(AphelionUnreachableError):
        def __init__(self) -> None:
            Exception.__init__(self, NEEDLE)
            self.reason = "timeout"

    raw, payload = _emit("parallax.test.subclass", "subclass", exc=SneakyUnreachable())
    _assert_scrubbed(raw, payload)
    assert payload["exc_class"] == "SneakyUnreachable"


def test_hand_written_exc_str_is_redacted_at_the_helper() -> None:
    """The guard must not depend on call-site discipline: a caller that still
    passes ``exc_str=str(exc)`` by hand gets it scrubbed anyway. This is the
    property that makes the fix durable for call sites not yet written."""
    raw, payload = _emit("parallax.test.legacy", "legacy", exc_str=f"boom {NEEDLE}")
    assert NEEDLE not in raw
    assert str(payload["exc_str"]).startswith("<redacted:")


def test_unknown_string_key_defaults_to_redacted() -> None:
    """Default-deny: a key nobody has classified yet is redacted, not trusted."""
    out = sanitize_log_extras({"some_new_field": NEEDLE})
    assert str(out["some_new_field"]).startswith("<redacted:")
    assert NEEDLE not in str(out)


def test_bounded_scalars_pass_through() -> None:
    """Numbers, bools and None carry no free text and stay readable."""
    out = sanitize_log_extras({"count": 3, "ok": True, "ratio": 0.5, "missing": None})
    assert out == {"count": 3, "ok": True, "ratio": 0.5, "missing": None}


def test_allowlisted_key_is_still_length_capped() -> None:
    """An allowlisted key is trusted for its *vocabulary*, not for unbounded
    size — a value far outside that vocabulary is redacted regardless."""
    out = sanitize_log_extras({"outcome": "x" * 5000})
    assert str(out["outcome"]).startswith("<redacted:")


def test_digest_is_stable_so_repeated_failures_correlate() -> None:
    """The surrogate has to be useful: identical messages must digest alike and
    different messages must not, or operators lose failure grouping."""
    a = exc_fields(RuntimeError("same"))
    b = exc_fields(RuntimeError("same"))
    c = exc_fields(RuntimeError("different"))
    assert a["exc_digest"] == b["exc_digest"]
    assert a["exc_digest"] != c["exc_digest"]


def test_unrenderable_value_does_not_lose_the_log_line() -> None:
    """A broken ``__str__`` must not cost us the record — sanitisation runs
    inside the crash-safe wrapper, so a raise there would silently drop it."""

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("nope")

    raw, payload = _emit("parallax.test.hostile", "hostile", thing=Hostile())
    assert payload["event"] == "hostile"
    assert str(payload["thing"]).startswith("<redacted:")


# ---------------------------------------------------------------------------
# Event-name validation (W6 Tier-A: the event string bypassed the sanitiser)
# ---------------------------------------------------------------------------


def test_legal_event_name_passes_through_unchanged() -> None:
    """The vocabulary every existing call site already uses must stay untouched,
    including the dotted form — a validator that broke those would just get
    reverted."""
    for event in ("secondary_unexpected_exception", "dual_read_decision_log.append_failed"):
        raw, payload = _emit("parallax.test.evt_ok", event)
        assert payload["event"] == event
        assert payload["msg"] == event
        assert "unsafe_event" not in raw


def test_dynamic_event_name_is_digested_not_emitted() -> None:
    """REGRESSION (W6 Tier-A): ``event`` reaches the record on two routes —
    ``msg`` and the ``event`` field — and neither passed through the extras
    policy. A caller interpolating user content into it bypassed the whole
    guard, contradicting this module's stated no-call-site-discipline property.
    """
    raw, payload = _emit("parallax.test.evt_bad", f"query failed: {NEEDLE}")
    assert NEEDLE not in raw, f"event name reached the log record: {raw!r}"
    # Both routes are covered, not just the field.
    assert payload["event"] == "unsafe_event_name"
    assert payload["msg"] == "unsafe_event_name"
    # The line stays findable and the miswiring stays visible.
    assert str(payload["unsafe_event"]).startswith("<redacted:")


@pytest.mark.parametrize(
    ("label", "event"),
    [
        ("uppercase", "Secondary_Unexpected"),
        ("whitespace", "secondary unexpected"),
        ("too_long", "e" * 65),
        ("empty", ""),
        ("punctuation", "failed: 'key'"),
        ("newline_injection", "ok_event\nlevel=CRITICAL forged=1"),
        ("non_str", 12345),
    ],
)
def test_illegal_event_names_are_rejected_without_losing_the_record(
    label: str, event: object
) -> None:
    """Rejection must never cost the log line — dropping it would trade a leak
    for a blind spot — and a non-``str`` must not raise inside the swallowing
    wrapper either, since a raise there loses the record silently.

    ``_emit`` asserts exactly one record came out, so the "not lost" half is
    covered there. Newline injection is included because ``msg`` lands in a
    line-oriented sink; it is doubly contained (rejected here, and JSON-escaped
    by the formatter even if it were not).
    """
    raw, payload = _emit("parallax.test.evt_reject", event)  # type: ignore[arg-type]
    assert payload["event"] == "unsafe_event_name", label
    assert payload["msg"] == "unsafe_event_name", label
    assert str(payload["unsafe_event"]).startswith("<redacted:"), label
    rendered = str(event)
    if rendered:  # the empty-string case has nothing to look for
        assert rendered not in raw, f"[{label}] illegal event name reached the record"


def test_event_name_digest_is_stable() -> None:
    """Two occurrences of the same miswiring group together, so an operator can
    tell one bad call site from many."""
    _, first = _emit("parallax.test.evt_d1", f"boom {NEEDLE}")
    _, second = _emit("parallax.test.evt_d2", f"boom {NEEDLE}")
    _, other = _emit("parallax.test.evt_d3", "boom something-else")
    assert first["unsafe_event"] == second["unsafe_event"]
    assert first["unsafe_event"] != other["unsafe_event"]


def test_every_call_site_passes_a_literal_event() -> None:
    """Pin 'all call sites use constants' as a property rather than a claim.

    The runtime validator above is the real guard; this makes the *obvious*
    leak shape — an f-string or a concatenation as the event name — fail in CI
    at the point someone writes it, with the offending file and line named.

    A bare parameter name is allowed: ``dual_read._safe_log_warning`` forwards
    its own ``event`` argument, and that wrapper is the reason the module-local
    binding exists at all.
    """
    import ast
    from pathlib import Path

    # Index of the event argument in each helper's positional signature.
    event_arg_index = {"safe_log_warning": 1, "_safe_log_warning": 0}
    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    def visit(node: ast.AST, params: frozenset[str], path: Path) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            spec = node.args
            names = {a.arg for a in (*spec.posonlyargs, *spec.args, *spec.kwonlyargs)}
            if spec.vararg:
                names.add(spec.vararg.arg)
            if spec.kwarg:
                names.add(spec.kwarg.arg)
            params = frozenset(names)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            index = event_arg_index.get(name)
            if index is not None and len(node.args) > index:
                argument = node.args[index]
                literal = isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                forwarded = isinstance(argument, ast.Name) and argument.id in params
                if not (literal or forwarded):
                    offenders.append(
                        f"{path.relative_to(repo_root)}:{node.lineno}: "
                        f"{name}(... {ast.unparse(argument)} ...)"
                    )
        for child in ast.iter_child_nodes(node):
            visit(child, params, path)

    for path in sorted((repo_root / "parallax").rglob("*.py")):
        visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), frozenset(), path)

    assert offenders == [], (
        "event names must be string literals (or a forwarded parameter); these are built:\n"
        + "\n".join(offenders)
    )


def test_allowlisted_exception_reasons_stay_literals() -> None:
    """Keep in-repo raise sites honest — defence in depth, not the guard.

    This test used to be the allowlist's only protection, and that was wrong:
    it can only see ``parallax/``, while the exception is raisable by an
    injected port implemented anywhere, and ``shadow.py`` logs whatever such a
    port throws. ``exc_fields`` now validates the tag at runtime, which is the
    real guarantee (see ``test_allowlisted_type_with_a_dynamic_tag_falls_back_to_digest``).

    What this still buys: an in-repo ``AphelionUnreachableError(f"...{user_q}")``
    fails here, at the line someone writes it, instead of silently degrading to
    a digest and losing the operator-facing tag.
    """
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted((repo_root / "parallax").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "AphelionUnreachableError":
                continue
            supplied = [*node.args, *(kw.value for kw in node.keywords)]
            for argument in supplied:
                literal = isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                # A plain Name is the classifier's return value bound to a local;
                # a Call is the classifier invoked inline. Both are closed sets.
                indirect = isinstance(argument, ast.Name | ast.Call)
                if not (literal or indirect):
                    offenders.append(
                        f"{path.relative_to(repo_root)}:{node.lineno}: {ast.unparse(argument)}"
                    )
                if isinstance(argument, ast.JoinedStr):  # f-string — always a leak risk
                    offenders.append(
                        f"{path.relative_to(repo_root)}:{node.lineno}: f-string reason"
                    )
    assert offenders == [], (
        "AphelionUnreachableError reasons must stay enum-like; these interpolate:\n"
        + "\n".join(offenders)
    )


def test_crash_safety_is_preserved(caplog: pytest.LogCaptureFixture) -> None:
    """The original guarantee still holds: a broken handler cannot propagate
    into the request path (fail-closed invariant #1)."""

    class _BrokenLogger:
        def warning(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("handler closed")

    safe_log_warning(_BrokenLogger(), "boom", exc=ValueError(NEEDLE))  # type: ignore[arg-type]
