"""M3-T1.5 — Integration tests: DualReadRouter feeds circuit-breaker (US-011).

Covers:
1. match outcome feeds breaker as reachable (observation_count++, unreachable=0)
2. aphelion_unreachable outcome feeds breaker as unreachable
3. skipped outcome does NOT feed breaker
4. Q5 CHANGE_TRACE.legacy_kind=bug short-circuit → skipped → breaker unchanged
5. 200 aphelion_unreachable queries → breaker trips
6. In-flight cohort semantics: pre-trip snapshot survives mid-flight breaker state change
"""

from __future__ import annotations

import threading

import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.aphelion_adapter import AphelionUnreachableError
from parallax.router.circuit_breaker import get_breaker_state
from parallax.router.contracts import DualReadResult, QueryRequest
from parallax.router.dual_read import DualReadRouter
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(*ids: str) -> RetrievalEvidence:
    hits = tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids)
    return RetrievalEvidence(hits=hits, stages=("test",))


def _req(qt: QueryType = QueryType.RECENT_CONTEXT) -> QueryRequest:
    return QueryRequest(query_type=qt, user_id="u1", q="hello")


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


class _SlowPort:
    """QueryPort that blocks until release() is called."""

    def __init__(self, result: RetrievalEvidence) -> None:
        self._result = result
        self._gate = threading.Event()
        self.started = threading.Event()

    def release(self) -> None:
        self._gate.set()

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        self.started.set()
        self._gate.wait()
        return self._result


_SAME_RESULT = _evidence("a", "b")
_DIFF_RESULT = _evidence("x", "y")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_breaker():
    get_breaker_state().reset()
    yield
    get_breaker_state().reset()


def _make_router(
    primary_result: RetrievalEvidence = _SAME_RESULT,
    secondary_result: RetrievalEvidence | None = None,
    secondary_exc: Exception | None = None,
    secondary_timeout_ms: float = 500.0,
) -> DualReadRouter:
    primary = _StubPort(primary_result)
    if secondary_exc is not None:
        secondary = _RaisingPort(secondary_exc)
    else:
        secondary = _StubPort(secondary_result or primary_result)
    return DualReadRouter(
        primary=primary,
        secondary=secondary,
        secondary_timeout_ms=secondary_timeout_ms,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_dual_read_feeds_breaker_on_match():
    """Match outcome feeds breaker; observation_count increments, unreachable stays 0."""
    router = _make_router()
    before_count = get_breaker_state().observation_count()

    result = router.query(_req(), dual_read_override=True)
    assert result.outcome == "match"

    after_count = get_breaker_state().observation_count()
    assert after_count == before_count + 1
    # rate is None because we're below MIN_OBSERVATIONS, but internal state has 0 unreachable
    state = get_breaker_state()
    with state._lock:
        unreachable_obs = [u for _, u in state._observations if u]
    assert len(unreachable_obs) == 0


@pytest.mark.integration
def test_dual_read_feeds_breaker_on_aphelion_unreachable():
    """aphelion_unreachable outcome → breaker observation_count++, unreachable obs recorded."""
    router = _make_router(secondary_exc=AphelionUnreachableError("test"))
    before_count = get_breaker_state().observation_count()

    result = router.query(_req(), dual_read_override=True)
    assert result.outcome == "aphelion_unreachable"

    after_count = get_breaker_state().observation_count()
    assert after_count == before_count + 1

    state = get_breaker_state()
    with state._lock:
        unreachable_obs = [u for _, u in state._observations if u]
    assert len(unreachable_obs) == 1


@pytest.mark.integration
def test_dual_read_does_not_feed_breaker_on_skipped():
    """Flag off → outcome=skipped → breaker observation_count unchanged."""
    router = _make_router()
    before_count = get_breaker_state().observation_count()

    result = router.query(_req(), dual_read_override=False)
    assert result.outcome == "skipped"

    assert get_breaker_state().observation_count() == before_count


@pytest.mark.integration
def test_dual_read_does_not_feed_breaker_on_change_trace_bug_skip():
    """Q5 CHANGE_TRACE.legacy_kind=bug short-circuit → skipped → breaker unchanged."""
    router = _make_router()
    before_count = get_breaker_state().observation_count()

    req = QueryRequest(
        query_type=QueryType.CHANGE_TRACE,
        user_id="u1",
        q="test",
        params={"legacy_kind": "bug"},
    )
    result = router.query(req, dual_read_override=True)
    assert result.outcome == "skipped"

    assert get_breaker_state().observation_count() == before_count


@pytest.mark.integration
def test_breaker_trips_under_aphelion_outage():
    """200 queries to an always-unreachable Aphelion → breaker trips."""
    router = _make_router(secondary_exc=AphelionUnreachableError("down"))

    for _ in range(200):
        router.query(_req(), dual_read_override=True)

    assert get_breaker_state().is_tripped() is True


@pytest.mark.integration
def test_breaker_trip_does_not_break_in_flight_request():
    """In-flight request has its dual_read snapshot frozen at entry.

    Sequence:
    1. Middleware snapshots dual_read=True (breaker not tripped yet).
    2. Primary (slow) is dispatched with dual_read_override=True.
    3. While primary is in-flight, breaker trips via external observation feed.
    4. Primary completes — its override was already passed as True, so it still
       returns a DualReadResult (not skipped).

    This proves the in-flight cohort semantics from Q10 (ralplan §3 line 281):
    'in-flight cohort keeps pre-trip snapshot until natural completion.'
    """
    slow_primary = _SlowPort(_SAME_RESULT)
    # Secondary matches primary to get outcome=match (not skipped)
    secondary = _StubPort(_SAME_RESULT)
    router = DualReadRouter(
        primary=slow_primary,
        secondary=secondary,
        secondary_timeout_ms=2000.0,
    )

    results: list[DualReadResult] = []
    errors: list[Exception] = []

    def run_query():
        try:
            # Snapshot is True here (pre-trip)
            r = router.query(_req(), dual_read_override=True)
            results.append(r)
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=run_query)
    t.start()

    # Wait for primary to start, then trip the breaker externally
    slow_primary.started.wait(timeout=2.0)

    # Trip the breaker by directly manipulating state
    state = get_breaker_state()
    with state._lock:
        state.tripped = True

    # Release the slow primary to let the query finish
    slow_primary.release()
    t.join(timeout=5.0)

    assert not errors, f"Query raised: {errors}"
    assert len(results) == 1
    # The in-flight query used dual_read_override=True (pre-trip snapshot)
    # so it should complete with an actual dual-read outcome (not "skipped")
    assert results[0].outcome in {"match", "diverge", "primary_only", "aphelion_unreachable"}
    assert results[0].primary is not None
