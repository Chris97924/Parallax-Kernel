"""S4 audit follow-up — end-to-end proof that router log lines carry no payload.

``tests/test_obs_log_sanitize.py`` pins the policy at the helper. This module
pins it where the audit found it broken: a real ``DualReadRouter.query()`` and
a real ``ShadowInterceptor.query()``, driven by a secondary that raises an
exception whose message contains a sensitive value. The assertion is made
against the *serialised* record — what actually lands on stderr — not against
the arguments the call site passed.

Headline site: ``parallax/router/dual_read.py`` ``secondary_unexpected_exception``
(``:327`` at audit revision ``dfc967a``), the one call site the S4 audit rated
YES: an unbounded, injectable, content-handling boundary whose exception was
``str()``'d verbatim into the log.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from parallax.obs.log import JSONFormatter, get_logger
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest
from parallax.router.dual_read import DualReadRouter
from parallax.router.shadow import ShadowInterceptor
from parallax.router.types import QueryType

NEEDLE = "PARALLAX-S2-NEEDLE-4b8ad2"


def _evidence(*ids: str) -> RetrievalEvidence:
    return RetrievalEvidence(
        hits=tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids),
        stages=("test",),
    )


class _StubPort:
    def __init__(self, result: RetrievalEvidence) -> None:
        self._result = result

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        return self._result


class _RaisingPort:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        raise self._exc


class _CaptureLog:
    """Attach a JSON capture handler to a Parallax logger for the with-block."""

    def __init__(self, name: str) -> None:
        self._logger = get_logger(name)
        self._buf = io.StringIO()
        self._handler = logging.StreamHandler(self._buf)
        self._handler.setFormatter(JSONFormatter())

    def __enter__(self) -> _CaptureLog:
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._handler.flush()
        self._logger.removeHandler(self._handler)

    @property
    def raw(self) -> str:
        return self._buf.getvalue()

    def records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.raw.splitlines() if line.strip()]

    def event(self, name: str) -> dict[str, object]:
        matches = [r for r in self.records() if r.get("event") == name]
        assert matches, f"expected a {name!r} record, got {self.records()!r}"
        return matches[0]


def _request() -> QueryRequest:
    return QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1", params=None)


# ---------------------------------------------------------------------------
# dual_read.py — secondary_unexpected_exception (the YES site)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "exc"),
    [
        # KeyError renders the key; the subject key resolves to request.q,
        # i.e. the raw user query string (audit §3.1).
        ("keyerror_subject", KeyError(NEEDLE)),
        # ValueError renders the offending substring — the shape produced by
        # int() / fromisoformat() / json.loads() on adapter-held content.
        ("valueerror_content", ValueError(f"Invalid isoformat string: '{NEEDLE}'")),
        # The M6/M7 shape: an HTTP error whose message carries URL + token.
        (
            "http_url_and_token",
            ConnectionError(
                "Server error '503' for url "
                f"'https://aphelion.internal/v1/read' Authorization: Bearer {NEEDLE}"
            ),
        ),
    ],
)
def test_secondary_unexpected_exception_does_not_log_payload(
    label: str, exc: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION (S4 audit, YES site): the secondary is an injected port, so
    ``str(exc)`` is an unbounded sink. Nothing it carries may reach stderr."""
    monkeypatch.setenv("DUAL_READ", "true")
    router = DualReadRouter(
        primary=_StubPort(_evidence("id0")),
        secondary=_RaisingPort(exc),
        secondary_timeout_ms=500.0,
    )

    with _CaptureLog("parallax.router.dual_read") as cap:
        result = router.query(_request())

    assert NEEDLE not in cap.raw, f"[{label}] secondary payload reached the log: {cap.raw!r}"

    record = cap.event("secondary_unexpected_exception")
    # The stated purpose of this handler — telling a logic bug apart from
    # infra unavailability — is served by the class name alone, so it stays.
    assert record["exc_class"] == type(exc).__name__
    # ...and the surrogate keeps repeated identical failures correlatable.
    assert record["exc_digest"]
    assert isinstance(record["exc_len"], int)

    # Fail-closed invariant #1 is untouched: the canonical result still returns.
    assert result.outcome == "primary_only"
    assert result.primary is not None
    assert result.secondary is None


def test_secondary_hits_equal_failure_does_not_log_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling site in the same triage block (audit site 3): a malformed secondary
    result makes ``_hits_equal`` raise while the hit contents are live in the
    frame. Rated NO by the audit because ``AttributeError``/``TypeError`` render
    type names — but the helper-level guard covers it regardless of who is right
    about the message shape, which is the point of fixing it at one place."""
    monkeypatch.setenv("DUAL_READ", "true")

    class _MalformedResult:
        """Returned, not raised — this drives the ``_hits_equal`` triage branch."""

        @property
        def hits(self) -> tuple[dict[str, object], ...]:
            raise AttributeError(f"'Evidence' object has no attribute {NEEDLE!r}")

    class _MalformedPort:
        def query(self, request: QueryRequest) -> RetrievalEvidence:
            return _MalformedResult()  # type: ignore[return-value]

    router = DualReadRouter(
        primary=_StubPort(_evidence("id0")),
        secondary=_MalformedPort(),
        secondary_timeout_ms=500.0,
    )
    with _CaptureLog("parallax.router.dual_read") as cap:
        result = router.query(_request())

    assert NEEDLE not in cap.raw
    assert cap.event("secondary_hits_equal_failed")["exc_class"] == "AttributeError"
    assert result.outcome == "primary_only"


def test_aphelion_unreachable_tag_still_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enum-tag path must not regress into silence: ``AphelionUnreachableError``
    is classified by tag and is the one message the allowlist keeps readable, so
    the sanitiser must not have made the breaker signal harder to read."""
    from parallax.router.aphelion_adapter import AphelionUnreachableError

    monkeypatch.setenv("DUAL_READ", "true")
    router = DualReadRouter(
        primary=_StubPort(_evidence("id0")),
        secondary=_RaisingPort(AphelionUnreachableError("claim_schema_error")),
        secondary_timeout_ms=500.0,
    )
    with _CaptureLog("parallax.router.dual_read") as cap:
        result = router.query(_request())

    assert result.outcome == "aphelion_unreachable"
    assert result.aphelion_unreachable_reason == "claim_schema_error"
    # Classified by tag without logging — no unexpected-exception record at all.
    assert not [
        r for r in cap.records() if r.get("event") == "secondary_unexpected_exception"
    ]


# ---------------------------------------------------------------------------
# shadow.py — shadow_query_error (audit §5.1, "the most important sibling")
# ---------------------------------------------------------------------------


def test_shadow_query_error_does_not_log_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REGRESSION (S4 audit §5.1): structurally identical to the YES site and
    strictly weaker — it catches bare ``Exception`` with no unreachable carve-out,
    so 100% of shadow failures took this path, not just the unexpected ones."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "u1")
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path))

    interceptor = ShadowInterceptor(
        canonical=_StubPort(_evidence("id0")),
        shadow_factory=lambda: _RaisingPort(KeyError(NEEDLE)),
    )
    with _CaptureLog("parallax.router.shadow") as cap:
        result = interceptor.query(_request())

    assert NEEDLE not in cap.raw, f"shadow payload reached the log: {cap.raw!r}"
    record = cap.event("shadow_query_error")
    assert record["exc_class"] == "KeyError"
    assert record["exc_digest"]

    # Canonical result is still returned untouched.
    assert result.hits[0]["id"] == "id0"


def test_shadow_log_write_failure_does_not_log_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The write-failure handler renders the resolved log path via ``OSError``
    (audit §5.1, LOW path disclosure). Same helper, same guarantee."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "u1")
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path))

    interceptor = ShadowInterceptor(
        canonical=_StubPort(_evidence("id0")),
        shadow_factory=lambda: _StubPort(_evidence("id0")),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise OSError(f"[Errno 13] Permission denied: '/srv/{NEEDLE}/shadow.jsonl'")

    monkeypatch.setattr("pathlib.Path.open", _explode)

    with _CaptureLog("parallax.router.shadow") as cap:
        interceptor.query(_request())

    assert NEEDLE not in cap.raw
    assert cap.event("shadow_log_write_failed")["exc_class"] == "OSError"


# ---------------------------------------------------------------------------
# dual_read_decision_log.py — append_failed (audit §5.1 sibling S-1)
# ---------------------------------------------------------------------------


def test_decision_log_append_failure_does_not_log_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hand-rolled duplicate of the helper (audit §5.1). It could not import the
    shared one without an import cycle, which is why the sanitiser lives in
    ``parallax.obs.log`` rather than in ``dual_read.py``."""
    from parallax.router import dual_read_decision_log as ddl

    monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(tmp_path))

    def _explode(*args: object, **kwargs: object) -> None:
        raise OSError(f"[Errno 28] No space left: '/srv/{NEEDLE}/decisions.jsonl'")

    monkeypatch.setattr("pathlib.Path.open", _explode)

    with _CaptureLog("parallax.router.dual_read_decision_log") as cap:
        assert ddl.append_decision({"correlation_id": "c1", "outcome": "match"}) is None

    assert NEEDLE not in cap.raw
    assert cap.event("dual_read_decision_log.append_failed")["exc_class"] == "OSError"
