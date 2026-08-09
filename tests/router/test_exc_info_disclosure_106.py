"""#106.5 — the audit-write failure logs must not disclose exception detail.

PR #104 replaced ``str(exc)`` with a value-free description at nine sites, but
its audit (``S4-safelog-audit.md`` §6) flagged a strictly larger surface it did
not cover: the five ``_log.error(..., exc_info=True)`` calls on
``aphelion_adapter``'s audit-DB write path. ``exc_info=True`` renders the whole
traceback — every frame, and with it the frame-local values #104's sanitiser
exists to withhold — and the same calls additionally interpolated ``str(exc)``
into the message via ``%s``, which is precisely where CPython puts the offending
*value* (``KeyError`` renders the key; a sqlite error renders the statement
fragment or the path).

The path is user-scoped: ``session_id = request.user_id`` is live on this write,
so a leaked frame local is attributable.

What must survive the fix, and is asserted here as hard as the redaction is: an
ERROR-level record, on every one of these branches, carrying enough for an
operator to correlate repeat failures (``exc_class`` + a stable digest). A fix
that silenced the log, or downgraded it to WARNING, would satisfy "no leak" and
be a worse outcome than the leak.

RED on the unfixed tree: the secret appears in the record and ``exc_info`` is set.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import pathlib
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

import pytest

from parallax.obs.log import JSONFormatter, get_logger
from parallax.router.aphelion_adapter import (
    AphelionReadAdapter,
    AphelionUnreachableError,
)
from parallax.router.contracts import QueryRequest
from parallax.router.types import QueryType

_ADAPTER_LOGGER = "parallax.router.aphelion_adapter"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Stands in for whatever a real exception drags along: a filesystem path, a
# statement fragment, a dict key. Distinctive so a substring scan cannot miss it.
_LEAK_CANARY = "canary-value-that-must-never-be-logged-2f4b8e"

_OLDER = "01963f7d-7000-7000-8000-000000000010"
_NEWER = "01963f7d-7000-7000-8000-000000000011"
_PACKAGE_ID = "01963f7d-7000-7000-8000-0000000000aa"


def _claim(claim_id: str, supersedes: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "claim_id": claim_id,
        "subject": "subject:foo",
        "polarity": "affirm",
        "package_id": _PACKAGE_ID,
    }
    if supersedes is not None:
        out["supersedes"] = supersedes
    return out


def _superseding_loader(_req: QueryRequest) -> Iterable[Mapping[str, Any]]:
    """Two claims in a supersession relation — the shape that reaches the write.

    A NOT_FOUND result skips envelope emission entirely (PR-D scope cut), so a
    loader returning nothing would never exercise the audit write and the test
    would pass against a completely broken fix.
    """
    return [_claim(_OLDER), _claim(_NEWER, supersedes=[_OLDER])]


@pytest.fixture()
def audit_conn_provider(
    tmp_path: pathlib.Path,
) -> Callable[[], sqlite3.Connection]:
    from parallax.apex.audit_db import open_audit_db

    conn = open_audit_db(tmp_path / "audit.db", validate=False)
    return lambda: conn


def _drive_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    provider: Callable[[], sqlite3.Connection],
    exc: BaseException,
) -> None:
    """Make the audit write raise ``exc`` and run one query through the adapter."""
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise exc

    monkeypatch.setattr("parallax.router.aphelion_adapter.write_audit_row", _boom)

    adapter = AphelionReadAdapter(
        audit_conn_provider=provider, claim_loader=_superseding_loader
    )
    with pytest.raises(AphelionUnreachableError):
        adapter.query(
            QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1", q="subject:foo")
        )


class _Capture(logging.Handler):
    """Keep the ``LogRecord`` objects AND their production rendering.

    Attached straight to the adapter's logger rather than using ``caplog``,
    because ``parallax.obs.log.get_logger`` sets ``propagate = False`` and
    ``caplog``'s handler lives on the root logger — the same idiom
    ``tests/router/test_log_payload_sanitize.py`` uses for the other JSON
    loggers. Both halves are needed: the record proves ``exc_info`` is unset,
    and the rendering proves nothing leaks through the text an operator reads.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._buf = io.StringIO()
        self.setFormatter(JSONFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self._buf.write(self.format(record) + "\n")

    @property
    def raw(self) -> str:
        return self._buf.getvalue()

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.raw.splitlines() if line.strip()]


@pytest.fixture()
def captured() -> Iterator[_Capture]:
    handler = _Capture()
    logger = get_logger(_ADAPTER_LOGGER)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _rendered(handler: _Capture, record: logging.LogRecord) -> str:
    """Everything this record could put in front of an operator, as one string.

    Renders through the production ``JSONFormatter`` *and* appends the standard
    traceback rendering, because the two disclose by different routes: the
    formatter drops ``exc_info`` (it is in ``_RESERVED``) while a stdlib handler
    renders it in full. Scanning only one of them would call the leak fixed
    while it still ships.
    """
    parts = [JSONFormatter().format(record), record.getMessage(), handler.raw]
    if record.exc_info is not None:
        parts.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_audit_write_failure_does_not_disclose_the_exception_value(
    monkeypatch: pytest.MonkeyPatch,
    captured: _Capture,
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``str(exc)`` must not reach the sink by any route."""
    _drive_write_failure(monkeypatch, audit_conn_provider, sqlite3.OperationalError(_LEAK_CANARY))

    assert captured.records, "the failure must still be logged — see the module docstring"
    for record in captured.records:
        rendered = _rendered(captured, record)
        assert _LEAK_CANARY not in rendered, (
            f"exception value disclosed by {record.levelname} record: {rendered}"
        )


def test_audit_write_failure_does_not_attach_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    captured: _Capture,
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """No ``exc_info`` — a traceback carries frame locals, which is the wider leak.

    Asserted on the record rather than on rendered text: whether a traceback is
    *rendered* depends on the handler, so a record that still carries
    ``exc_info`` is a leak waiting for a handler change, not a safe one.
    """
    _drive_write_failure(monkeypatch, audit_conn_provider, sqlite3.OperationalError(_LEAK_CANARY))

    assert captured.records
    for record in captured.records:
        assert record.exc_info is None, (
            f"{record.levelname} record still carries a traceback: "
            f"{logging.Formatter().formatException(record.exc_info)}"
        )


def test_no_exc_info_true_remains_on_the_audit_write_path() -> None:
    """Static backstop: the literal must be gone from the module.

    The behavioural tests above drive one branch; this covers all five without
    needing a bespoke trigger for each, and fails at review time if a new call
    site reintroduces the pattern.
    """
    source = (_REPO_ROOT / "parallax" / "router" / "aphelion_adapter.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    offenders = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "exc_info"
        and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
    ]

    assert offenders == [], f"exc_info= reintroduced in aphelion_adapter.py at {offenders}"


# ---------------------------------------------------------------------------
# Operator-grade logging must survive
# ---------------------------------------------------------------------------


def test_failure_is_still_reported_at_error_level_and_stays_correlatable(
    monkeypatch: pytest.MonkeyPatch,
    captured: _Capture,
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """Redaction must not cost the operator the signal.

    A fail-closed audit write dropping the secondary is an ERROR, and repeat
    failures have to be groupable without their content: ``exc_class`` says what
    broke and the digest says "same failure as last time". This is the half of
    the fix that a naive "delete the log line" would fail.

    Asserted on the rendered JSON, not just the record: these fields live in
    ``extra`` and the stdlib default formatter drops them, so "the record
    carries it" would still allow a deployment where the operator sees a bare
    event name.
    """
    _drive_write_failure(monkeypatch, audit_conn_provider, sqlite3.OperationalError(_LEAK_CANARY))

    errors = [r for r in captured.records if r.levelno >= logging.ERROR]
    assert errors, "the audit-write failure must still be an ERROR, not silenced or downgraded"

    payloads = [p for p in captured.payloads() if p.get("level") == "ERROR"]
    assert payloads, f"nothing rendered at ERROR: {captured.raw}"
    payload = payloads[0]
    assert payload.get("exc_class") == "OperationalError"
    assert payload.get("exc_digest"), "no digest — repeat failures stop being groupable"
    assert payload.get("event") == "audit_db_write_failed"


def test_the_unreachable_reason_still_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """The exception contract is untouched — redaction is a logging change only.

    ``DualReadRouter`` classifies on this reason; if the fix altered it, the
    circuit-breaker increment and the ``aphelion_unreachable`` signal would be
    lost, which is the exact silent-failure mode the total fence exists to stop.
    """
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError(_LEAK_CANARY)

    monkeypatch.setattr("parallax.router.aphelion_adapter.write_audit_row", _boom)
    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider, claim_loader=_superseding_loader
    )

    with pytest.raises(AphelionUnreachableError) as excinfo:
        adapter.query(
            QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1", q="subject:foo")
        )

    assert excinfo.value.reason == "audit_db_write_failed"
