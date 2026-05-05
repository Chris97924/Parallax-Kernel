"""Tests for AphelionReadAdapter stub (M3-T1.2, US-011 + US-009.2 AC-2.x).

Verifies that the stub always raises AphelionUnreachableError("not_implemented")
and that it conforms to the QueryPort Protocol. US-009.2 adds AC-2.7 (exception
shape stability across 100 invocations) and AC-2.8 (DualReadRouter outcome
consistency across every QueryType).
"""

from __future__ import annotations

import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.aphelion_stub import AphelionReadAdapter, AphelionUnreachableError
from parallax.router.contracts import QueryRequest
from parallax.router.dual_read import DualReadRouter
from parallax.router.ports import QueryPort
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Stub always raises AphelionUnreachableError
# ---------------------------------------------------------------------------


def test_query_always_raises_unreachable() -> None:
    adapter = AphelionReadAdapter()
    request = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1")
    with pytest.raises(AphelionUnreachableError):
        adapter.query(request)


def test_unreachable_reason_is_not_implemented() -> None:
    adapter = AphelionReadAdapter()
    request = QueryRequest(query_type=QueryType.ENTITY_PROFILE, user_id="u1")
    try:
        adapter.query(request)
    except AphelionUnreachableError as exc:
        assert exc.reason == "not_implemented"
    else:
        pytest.fail("Expected AphelionUnreachableError to be raised")


# ---------------------------------------------------------------------------
# AphelionUnreachableError.reason attribute
# ---------------------------------------------------------------------------


def test_unreachable_error_reason_attribute() -> None:
    err = AphelionUnreachableError("timeout")
    assert err.reason == "timeout"
    assert "timeout" in str(err)


@pytest.mark.parametrize("reason", ["not_implemented", "timeout", "connection_error"])
def test_unreachable_error_reason_values(reason: str) -> None:
    err = AphelionUnreachableError(reason)
    assert err.reason == reason


# ---------------------------------------------------------------------------
# Conforms to QueryPort Protocol (runtime_checkable)
# ---------------------------------------------------------------------------


def test_conforms_to_query_port_protocol() -> None:
    adapter = AphelionReadAdapter()
    assert isinstance(adapter, QueryPort), "AphelionReadAdapter must conform to QueryPort Protocol"


# ---------------------------------------------------------------------------
# Constructor accepts base_url and timeout_ms
# ---------------------------------------------------------------------------


def test_constructor_defaults() -> None:
    adapter = AphelionReadAdapter()
    assert adapter._base_url is None
    assert adapter._timeout_ms == 100.0


def test_constructor_custom_args() -> None:
    adapter = AphelionReadAdapter(base_url="http://aphelion:8080", timeout_ms=50.0)
    assert adapter._base_url == "http://aphelion:8080"
    assert adapter._timeout_ms == 50.0


# ---------------------------------------------------------------------------
# All QueryType values raise unreachable (stub is query-type agnostic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("qt", list(QueryType))
def test_all_query_types_raise(qt: QueryType) -> None:
    adapter = AphelionReadAdapter()
    request = QueryRequest(query_type=qt, user_id="u1")
    with pytest.raises(AphelionUnreachableError):
        adapter.query(request)


# ---------------------------------------------------------------------------
# US-009.2 AC-2.7 — exception shape stability over 100 invocations
# ---------------------------------------------------------------------------


def test_query_exception_shape_stable_over_100_invocations() -> None:
    """AC-2.7: 100 consecutive query() calls raise an identically-shaped error."""
    adapter = AphelionReadAdapter()
    request = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1")

    first_str: str | None = None
    for _ in range(100):
        try:
            adapter.query(request)
        except AphelionUnreachableError as exc:
            assert type(exc) is AphelionUnreachableError
            assert exc.reason == "not_implemented"
            if first_str is None:
                first_str = str(exc)
            else:
                assert str(exc) == first_str
        else:
            pytest.fail("Expected AphelionUnreachableError to be raised")

    assert first_str is not None


# ---------------------------------------------------------------------------
# US-009.2 AC-2.8 — DualReadRouter outcome stays 'aphelion_unreachable'
# regardless of query type, when secondary is the real AphelionReadAdapter stub.
# ---------------------------------------------------------------------------


class _PrimaryEvidenceStub:
    """Minimal QueryPort returning a fixed RetrievalEvidence."""

    def __init__(self) -> None:
        self._result = RetrievalEvidence(
            hits=({"id": "h1", "kind": "memory", "score": 1.0},),
            stages=("test",),
        )

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        return self._result


@pytest.mark.parametrize("query_type", list(QueryType))
def test_dual_read_outcome_aphelion_unreachable_for_every_query_type(
    query_type: QueryType,
) -> None:
    """AC-2.8: every QueryType yields outcome='aphelion_unreachable' through DualReadRouter."""
    primary = _PrimaryEvidenceStub()
    router = DualReadRouter(
        primary=primary,
        secondary=AphelionReadAdapter(),
        secondary_timeout_ms=500.0,
    )
    request = QueryRequest(query_type=query_type, user_id="u1")

    result = router.query(request, dual_read_override=True)

    assert result.outcome == "aphelion_unreachable"
    assert result.aphelion_unreachable_reason == "not_implemented"
    assert result.primary is primary._result
    assert result.secondary is None
