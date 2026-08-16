"""Mutation-hardening tests for ``parallax.router.dual_read`` (S7).

Every test below was written against a specific *surviving* mutant: a
semantic edit to ``dual_read.py`` that the pre-existing suite accepted.
Each docstring names the mutant it kills, so a later reader can tell what
the assertion is standing guard over rather than guessing from the shape
of the test.

The mutants covered here:

  * the two-condition gate on the request-attempt counter (both halves)
  * the ``QueryType.CHANGE_TRACE`` term of the Q5 short-circuit
  * the H4 "writer returned ''" branch that sets ``write_error_observed``
  * the millisecond unit of ``latency_primary_ms``
  * parallel dispatch (invariant #7) in the default executor
  * arbitration latency being measured on the ``arbitrate`` exception path
  * the breaker-check handler failing open rather than closed
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import prometheus_client
import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.aphelion_adapter import AphelionUnreachableError
from parallax.router.contracts import QueryRequest
from parallax.router.dual_read import DualReadRouter
from parallax.router.live_arbitration import arbitration_latency_seconds
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Helpers — same shapes tests/router/test_dual_read_router.py uses
# ---------------------------------------------------------------------------


def _evidence(*ids: str) -> RetrievalEvidence:
    hits = tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids)
    return RetrievalEvidence(hits=hits, stages=("test",))


class _StubPort:
    def __init__(self, result: RetrievalEvidence) -> None:
        self._result = result
        self.call_count = 0

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        self.call_count += 1
        return self._result


class _RaisingPort:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        raise self._exc


class _SleepingPort:
    def __init__(self, result: RetrievalEvidence, delay_ms: float) -> None:
        self._result = result
        self._delay_ms = delay_ms

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        time.sleep(self._delay_ms / 1000.0)
        return self._result


def _request(
    qt: QueryType = QueryType.RECENT_CONTEXT, *, params: dict | None = None
) -> QueryRequest:
    return QueryRequest(query_type=qt, user_id="u1", params=params)


def _requests_total(traffic_source: str) -> float:
    """Read ``parallax_dual_read_requests_total{traffic_source}``."""
    value = prometheus_client.REGISTRY.get_sample_value(
        "parallax_dual_read_requests_total",
        {"traffic_source": traffic_source},
    )
    return 0.0 if value is None else value


def _records(log_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(log_dir.glob("dual-read-decisions-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# The request-attempt counter gate — `enabled AND is_log_enabled()`
#
# Both halves are load-bearing and the module comment says so, but the
# pre-existing tests only ever move the two flags together, so either half
# can be deleted (or the `and` widened to `or`) without a red test.
# ---------------------------------------------------------------------------


class TestRequestAttemptCounterGate:
    def test_log_forced_on_with_dual_read_off_does_not_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: ``if enabled and is_log_enabled()`` widened to ``or``.

        Dual-read off with the log explicitly forced on is a real operator
        state — it is the deliberate under-count the module comment
        describes. Widening the gate counts those attempts, so
        ``DualReadDecisionLogSilent``'s traffic guard reads true on a system
        that is correctly serving ordinary queries and writing no dual-read
        decisions. The existing disabled-path test clears both flags at
        once, so it never separates them.
        """
        monkeypatch.setenv("DUAL_READ", "false")
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
        monkeypatch.setenv("DUAL_READ_LOG_DIR", str(tmp_path))

        before = _requests_total("natural")
        router = DualReadRouter(
            primary=_StubPort(_evidence("a")), secondary=_StubPort(_evidence("a"))
        )
        result = router.query(_request())

        assert result.outcome == "skipped"
        assert _requests_total("natural") - before == 0, (
            "dual-read is off; the attempt counter must not advance just "
            "because the decision log was forced on"
        )
        # The skipped path still writes its record — that is what makes the
        # under-count deliberate rather than a hole.
        assert len(_records(tmp_path)) == 1

    def test_dual_read_on_with_log_disabled_does_not_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: the gate reduced to ``if enabled:``.

        With the log killed by its own switch no record is expected of this
        request, so counting the attempt would make the writer look broken
        (attempts climbing, freshness absent) on a system an operator
        deliberately silenced.
        """
        monkeypatch.setenv("DUAL_READ", "true")
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "false")
        monkeypatch.setenv("DUAL_READ_LOG_DIR", str(tmp_path))

        before = _requests_total("natural")
        router = DualReadRouter(
            primary=_StubPort(_evidence("a")), secondary=_StubPort(_evidence("a"))
        )
        result = router.query(_request())

        assert result.outcome == "match"
        assert _requests_total("natural") - before == 0, (
            "the decision log is switched off, so no record is expected of "
            "this request and no attempt should be counted"
        )
        assert _records(tmp_path) == []


# ---------------------------------------------------------------------------
# Q5 short-circuit
# ---------------------------------------------------------------------------


class TestQ5ShortCircuitScope:
    def test_legacy_kind_bug_outside_change_trace_still_dual_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: the ``query_type == QueryType.CHANGE_TRACE`` term dropped.

        Q5 is a CHANGE_TRACE-specific carve-out. Without the query-type term
        any request that happens to carry ``legacy_kind=bug`` skips the
        secondary, silently shrinking dual-read coverage for every other
        query type — invisible in the outcome mix because "skipped" is a
        legitimate outcome. The existing tests only ever pass that param on
        a CHANGE_TRACE request.
        """
        monkeypatch.setenv("DUAL_READ", "true")
        secondary = _StubPort(_evidence("a"))
        router = DualReadRouter(
            primary=_StubPort(_evidence("a")),
            secondary=secondary,
            secondary_timeout_ms=2000.0,
        )

        result = router.query(_request(QueryType.RECENT_CONTEXT, params={"legacy_kind": "bug"}))

        assert result.outcome == "match"
        assert result.secondary is not None
        assert secondary.call_count == 1, (
            "legacy_kind=bug is a CHANGE_TRACE carve-out; it must not "
            "suppress the Aphelion read on other query types"
        )

    def test_change_trace_with_legacy_kind_bug_still_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the test above — the carve-out itself still holds."""
        monkeypatch.setenv("DUAL_READ", "true")
        secondary = _StubPort(_evidence("a"))
        router = DualReadRouter(
            primary=_StubPort(_evidence("a")),
            secondary=secondary,
            secondary_timeout_ms=2000.0,
        )

        result = router.query(_request(QueryType.CHANGE_TRACE, params={"legacy_kind": "bug"}))

        assert result.outcome == "skipped"
        assert secondary.call_count == 0


# ---------------------------------------------------------------------------
# H4 — the conflict writer's "I caught an exception" signal
# ---------------------------------------------------------------------------


class TestConflictWriteErrorFlag:
    def test_empty_event_id_sets_write_error_observed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: the H4 else-branch sets ``write_error_observed = False``.

        ``write_conflict_event`` is fail-closed: it signals "I caught an
        exception" by returning ``''`` rather than by raising. If that does
        not raise the flag, ``write_error_rate`` is computed from a record
        stream in which the write failures are invisible — the one number
        the field exists to make computable. The pre-existing suite only
        drives the writer's success path.
        """
        monkeypatch.setenv("DUAL_READ", "true")
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
        monkeypatch.setenv("DUAL_READ_LOG_DIR", str(tmp_path))

        import parallax.router.dual_read as dr_mod

        monkeypatch.setattr(dr_mod, "write_conflict_event", lambda *a, **kw: "")

        events_conn = sqlite3.connect(":memory:")
        try:
            router = DualReadRouter(
                primary=_StubPort(_evidence("a")),
                secondary=_RaisingPort(AphelionUnreachableError("not_implemented")),
                secondary_timeout_ms=2000.0,
                events_conn=events_conn,
            )
            result = router.query(_request(QueryType.RECENT_CONTEXT))
        finally:
            events_conn.close()

        assert result.arbitration is not None
        assert result.arbitration.requires_manual_review is True
        assert result.write_error_observed is True, (
            "an empty conflict_event_id is the writer reporting a caught "
            "exception; it has to reach the result"
        )

        records = _records(tmp_path)
        assert len(records) == 1
        assert records[0]["write_error_observed"] is True


# ---------------------------------------------------------------------------
# latency_primary_ms unit
# ---------------------------------------------------------------------------


class TestLatencyUnits:
    def test_latency_primary_ms_is_milliseconds_not_seconds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: ``(t1 - t0) * 1000.0`` reduced to ``(t1 - t0)``.

        Every pre-existing assertion on this field is ``> 0``, which a
        seconds-valued number satisfies just as well — so the unit is
        unpinned and a dashboard reading it would under-report by 1000x.
        A 60ms primary separates the two readings by three orders of
        magnitude, well outside any clock jitter.
        """
        monkeypatch.setenv("DUAL_READ", "true")
        router = DualReadRouter(
            primary=_SleepingPort(_evidence("a"), delay_ms=60.0),
            secondary=_StubPort(_evidence("a")),
            secondary_timeout_ms=2000.0,
        )

        result = router.query(_request())

        assert result.outcome == "match"
        assert result.latency_primary_ms >= 30.0, (
            "a 60ms primary reported as "
            f"{result.latency_primary_ms} — the field is in seconds, not ms"
        )


# ---------------------------------------------------------------------------
# Invariant #7 — parallel dispatch
# ---------------------------------------------------------------------------


class TestParallelDispatch:
    def test_primary_and_secondary_overlap_in_the_default_executor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: the default executor built with ``max_workers=1``.

        Serialized, both ports still run and every outcome assertion in the
        suite still holds — the only observable difference is that the two
        reads can no longer overlap, which is the entire point of invariant
        #7 and the whole latency argument for dual-read. Make overlap
        load-bearing: the primary cannot finish until the secondary has
        started, which is unsatisfiable on a single worker.
        """
        monkeypatch.setenv("DUAL_READ", "true")
        secondary_started = threading.Event()

        class _WaitsForSecondary:
            def query(self, request: QueryRequest) -> RetrievalEvidence:
                if not secondary_started.wait(timeout=3.0):
                    raise AssertionError(
                        "primary finished without the secondary ever starting — "
                        "the two reads are dispatched serially"
                    )
                return _evidence("a")

        class _SignallingSecondary:
            def query(self, request: QueryRequest) -> RetrievalEvidence:
                secondary_started.set()
                return _evidence("a")

        # No `executor=` — the constructor's own ThreadPoolExecutor is the
        # thing under test.
        router = DualReadRouter(
            primary=_WaitsForSecondary(),
            secondary=_SignallingSecondary(),
            secondary_timeout_ms=2000.0,
        )

        result = router.query(_request())

        assert result.outcome == "match"
        assert secondary_started.is_set()


# ---------------------------------------------------------------------------
# #106 — arbitration latency on the exception path
# ---------------------------------------------------------------------------


def _arbitration_observations() -> float:
    """Total observations on the arbitration histogram.

    ``Histogram.observe`` increments only the single bucket a value falls
    into — prometheus_client makes the buckets cumulative at collect time,
    not at write time — so the total is their sum, not the last one.
    """
    return sum(bucket.get() for bucket in arbitration_latency_seconds._buckets)  # noqa: SLF001


class TestArbitrationLatencyOnFailure:
    def test_latency_recorded_when_arbitrate_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MUTANT: the ``try/finally`` around ``arbitrate`` reduced to a
        post-call ``observe``.

        The #106 comment states the reason in so many words: an exception
        path that stopped reporting would make a broken rule table look like
        a quiet one. The pre-existing latency tests only drive the happy
        path, so moving the observation out of the ``finally`` is invisible
        to them.
        """
        monkeypatch.setenv("DUAL_READ", "true")

        import parallax.router.dual_read as dr_mod

        def _boom(**kwargs: object) -> None:
            raise RuntimeError("rule table is broken")

        monkeypatch.setattr(dr_mod, "arbitrate", _boom)

        router = DualReadRouter(
            primary=_StubPort(_evidence("a")),
            secondary=_StubPort(_evidence("a")),
            secondary_timeout_ms=2000.0,
        )

        before = _arbitration_observations()
        with pytest.raises(RuntimeError, match="rule table is broken"):
            router.query(_request())

        assert _arbitration_observations() == before + 1.0, (
            "a raising arbitration went unmeasured — a broken rule table would read as an idle one"
        )


# ---------------------------------------------------------------------------
# The breaker-check handler fails open
# ---------------------------------------------------------------------------


class TestBreakerCheckFailsOpen:
    def test_exploding_breaker_check_does_not_claim_a_wiring_gap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: ``breaker_tripped = False`` flipped to ``True`` in the
        ``except`` handler.

        The handler exists so a breaker whose state check explodes cannot
        take the request path with it. Defaulting to "tripped" turns every
        such failure into a second warning that names a wiring gap nobody
        has evidence for, while the accurate signal —
        ``breaker_is_tripped_check_failed`` — is already being logged
        alongside it. The existing tests supply breakers that answer
        cleanly, so the handler's default is unpinned.
        """
        monkeypatch.setenv("DUAL_READ", "true")

        import parallax.router.dual_read as dr_mod

        class _ExplodingBreaker:
            def is_tripped(self) -> bool:
                raise RuntimeError("breaker state store is down")

            def record_unreachable_observation(self, *, observed_unreachable: bool) -> None:
                pass

        monkeypatch.setattr(dr_mod, "get_breaker_state", lambda: _ExplodingBreaker())

        warnings: list[str] = []
        monkeypatch.setattr(dr_mod._log, "warning", lambda msg, *a, **kw: warnings.append(msg))

        router = DualReadRouter(
            primary=_StubPort(_evidence("a")),
            secondary=_StubPort(_evidence("a")),
            secondary_timeout_ms=2000.0,
        )
        result = router.query(_request())  # dual_read_override left as None

        assert result.outcome == "match"
        assert "breaker_is_tripped_check_failed" in warnings, (
            "the swallowed exception must still be surfaced"
        )
        assert "dual_read_override_missing_with_tripped_breaker" not in warnings, (
            "a breaker check that failed is not a breaker that is tripped"
        )
