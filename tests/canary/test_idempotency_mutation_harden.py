"""Mutation-hardening for ``parallax.canary.idempotency`` (overnight-20260816 S10).

Companion to ``test_idempotency.py``, which walks acceptance criteria 1.1-1.8
and the issue #42 lock-lifecycle race. What it does *not* do is read the audit
row back for anything except ``idempotency_hit`` and ``response_status`` on a
200, or exercise the cache-hit path's own failure handling — and each test below
was written against a semantic mutant that survived because of it:

  * ``if not isinstance(event_id, str) or not is_uuid7(event_id)`` with the type
    half removed. ``is_uuid7`` accepts a :class:`uuid.UUID` object, so a caller
    passing the object instead of its string form would sail through the gate
    and then fail *silently*: SQLite rejects the unsupported parameter type,
    ``AuditLog.record`` swallows it per criterion 1.8, and every request becomes
    a cache miss. Nothing in the suite passes a non-``str``.
  * the validity check moved after the worker call, so an invalid ``event_id``
    still fires the side effect before raising. ``pytest.raises`` alone cannot
    see the difference.
  * ``_on_cache_hit``'s ``audit_persisted=ok`` hardcoded to ``True``. The only
    audit-failure test injects the failure before the *first* call, which routes
    through ``_execute_worker``; the hit path's own return value is untested.
  * ``_on_cache_hit``'s ``latency_ms=0.0`` changed to ``None`` or to the
    cached-response latency — a cache hit did no work, and a non-zero duration
    on a hit inflates the T3 histogram with requests that never ran.
  * ``latency_ms = (self._clock() - started) * 1000.0`` with the ``* 1000.0``
    dropped or the operands swapped, and the injected ``clock`` ignored in
    favour of ``time.monotonic``. No test reads the persisted duration.
  * ``response_status=response.status`` hardcoded to ``200``. Every audit-row
    assertion in the suite happens to use a 200 worker.
  * ``worker(request)`` degraded to ``worker(None)``. Both suite workers ignore
    their argument.
  * ``_lookup``'s "row exists but body is NULL → treat as a miss" branch
    returning a hollow ``CachedResponse`` instead. The docstring claims "Tests
    cover this branch"; they do not — no test creates a body-less row, which is
    exactly what ``AuditLog.record_ack`` writes.
  * ``_release_lock`` reached only on the exception path. The existing
    bounded-growth test drives 50 *failures*; 50 successes were never checked.
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from parallax.canary import idempotency as idempotency_mod
from parallax.canary.audit_log import AuditLog
from parallax.canary.event_id import uuid7
from parallax.canary.idempotency import (
    CachedResponse,
    IdempotencyHandler,
    InvalidEventIdError,
)


@pytest.fixture
def audit(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.db")
    yield log
    log.close()


def _ok_worker(_payload: object) -> CachedResponse:
    return CachedResponse(status=200, body="ok")


class _NeverRuns:
    """Worker that fails the test if it is ever invoked."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _payload: object) -> CachedResponse:
        self.calls += 1
        raise AssertionError("worker must not run")


# ===========================================================================
# The event_id gate: type, validity, and ordering relative to side effects
# ===========================================================================


@pytest.mark.unit
class TestEventIdGate:
    def test_uuid_object_is_rejected(self, audit: AuditLog) -> None:
        """``is_uuid7`` accepts a UUID *object*; ``handle`` must not.

        Dropping the ``isinstance(event_id, str)`` half of the gate does not
        fail loudly — it fails silently, because SQLite refuses the parameter
        type and ``AuditLog.record`` swallows the error by design (criterion
        1.8). Every request would then be a cache miss forever.
        """
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        with pytest.raises(InvalidEventIdError):
            h.handle(event_id=uuid7(), request="x", worker=_ok_worker)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [None, 42, b"018bcfe5-6800-7bcd-af01-020304050607"])
    def test_non_string_event_ids_are_rejected(self, audit: AuditLog, bad: object) -> None:
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        with pytest.raises(InvalidEventIdError):
            h.handle(event_id=bad, request="x", worker=_ok_worker)  # type: ignore[arg-type]

    def test_invalid_event_id_does_not_invoke_the_worker(self, audit: AuditLog) -> None:
        """The gate runs *before* the side effect, not after it.

        A mutant that validates after calling the worker still raises, so
        ``pytest.raises`` on its own keeps passing while every rejected request
        has already hit the database.
        """
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        worker = _NeverRuns()
        with pytest.raises(InvalidEventIdError):
            h.handle(event_id="not-a-uuid", request="x", worker=worker)
        assert worker.calls == 0

    def test_invalid_event_id_writes_no_audit_row(self, audit: AuditLog) -> None:
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        with pytest.raises(InvalidEventIdError):
            h.handle(event_id="not-a-uuid", request="x", worker=_ok_worker)
        assert audit.lookup("not-a-uuid") is None

    def test_error_is_a_valueerror_for_400_mapping(self) -> None:
        """Documented contract: callers map this to a 400-class response."""
        assert issubclass(InvalidEventIdError, ValueError)


# ===========================================================================
# The worker contract
# ===========================================================================


@pytest.mark.unit
class TestWorkerInvocation:
    def test_worker_receives_the_request_object(self, audit: AuditLog) -> None:
        """Kills ``worker(None)`` — both existing suite workers ignore their
        argument, so the payload never had to arrive."""
        h: IdempotencyHandler[object] = IdempotencyHandler(audit_log=audit)
        sentinel = object()
        seen: list[object] = []

        def worker(payload: object) -> CachedResponse:
            seen.append(payload)
            return CachedResponse(status=200, body="ok")

        h.handle(event_id=str(uuid7()), request=sentinel, worker=worker)
        assert seen == [sentinel]
        assert seen[0] is sentinel


# ===========================================================================
# What the first call persists
# ===========================================================================


@pytest.mark.unit
class TestFirstCallPersistsTheWorkersResult:
    @pytest.mark.parametrize("status", [201, 400, 500, 503])
    def test_non_200_status_is_persisted(self, audit: AuditLog, status: int) -> None:
        """Kills a hardcoded ``response_status=200`` in ``_execute_worker`` —
        invisible today because every audit-row assertion uses a 200 worker."""
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        eid = str(uuid7())
        out = h.handle(
            event_id=eid,
            request="x",
            worker=lambda _r: CachedResponse(status=status, body="body"),
        )
        assert out.status == status
        row = audit.lookup(eid)
        assert row is not None
        assert row.response_status == status

    def test_latency_is_elapsed_milliseconds_from_the_injected_clock(
        self, audit: AuditLog
    ) -> None:
        """0.5 clock-units becomes 500.0 ms.

        Kills three mutants: ``* 1000.0`` dropped (0.5), the subtraction
        reversed (-500.0), and the injected ``clock`` ignored in favour of
        ``time.monotonic`` (a real, near-zero duration).
        """
        ticks = iter([100.0, 100.5])
        h: IdempotencyHandler[str] = IdempotencyHandler(
            audit_log=audit, clock=lambda: next(ticks)
        )
        eid = str(uuid7())
        h.handle(event_id=eid, request="x", worker=_ok_worker)
        row = audit.lookup(eid)
        assert row is not None
        assert row.latency_ms == 500.0

    def test_request_at_is_the_timestamp_this_module_captured(
        self, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``request_at_iso=request_at`` is passed through, not left to
        ``make_record``'s own "now" fallback.

        The scripted datetime returns a stamp no wall clock will produce, so a
        mutant that drops the keyword records the real time instead and the
        equality fails.
        """
        pinned = _dt.datetime(2001, 2, 3, 4, 5, 6, tzinfo=_dt.UTC)
        monkeypatch.setattr(idempotency_mod, "_dt", _scripted_datetime_module(pinned))
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        eid = str(uuid7())
        h.handle(event_id=eid, request="x", worker=_ok_worker)
        row = audit.lookup(eid)
        assert row is not None
        assert row.request_at_iso == pinned.isoformat()

    def test_request_at_is_captured_before_the_worker_runs(self, audit: AuditLog) -> None:
        """It is the time the request *started*, not the time it finished.

        Kills the mutant that moves the capture below ``worker(request)``: with
        a 500 ms worker, an end-of-request stamp lands half a second late. The
        tolerance sits well clear of both sides — a correct capture is separated
        from ``started`` only by a SQLite lookup, and the mutant misses by 0.5 s
        — so a loaded machine cannot flip the verdict either way.
        """
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        eid = str(uuid7())
        started = _dt.datetime.now(_dt.UTC)

        def slow_worker(_r: object) -> CachedResponse:
            time.sleep(0.5)
            return CachedResponse(status=200, body="ok")

        h.handle(event_id=eid, request="x", worker=slow_worker)
        row = audit.lookup(eid)
        assert row is not None
        recorded = _dt.datetime.fromisoformat(row.request_at_iso)
        drift = (recorded - started).total_seconds()
        assert 0.0 <= drift < 0.2, f"request_at drifted {drift:.3f}s — captured after the worker?"


def _scripted_datetime_module(stamp: _dt.datetime):
    """Return a stand-in for the ``datetime`` module whose ``now`` is pinned."""

    class _Datetime:
        @staticmethod
        def now(_tz: object = None) -> _dt.datetime:
            return stamp

    class _Module:
        UTC = _dt.UTC
        datetime = _Datetime

    return _Module()


# ===========================================================================
# What the cache-hit path persists and reports
# ===========================================================================


@pytest.mark.unit
class TestCacheHitPath:
    def test_audit_failure_on_the_hit_is_reported(self, audit: AuditLog) -> None:
        """``audit_persisted`` must reflect the *hit's* own write.

        The suite's only audit-failure test injects before the first call, which
        never reaches ``_on_cache_hit``. Hardcoding ``audit_persisted=True``
        there hides exactly the "persistent audit failure" the field exists to
        let observers alert on.
        """
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        eid = str(uuid7())
        first = h.handle(event_id=eid, request="x", worker=_ok_worker)
        assert first.hit is False
        assert first.audit_persisted is True

        original = audit.record
        calls = {"n": 0}

        def failing_record(*args: object, **kwargs: object) -> bool:
            calls["n"] += 1
            return False

        audit.record = failing_record  # type: ignore[method-assign]
        try:
            second = h.handle(event_id=eid, request="x", worker=_ok_worker)
        finally:
            audit.record = original  # type: ignore[method-assign]

        assert calls["n"] == 1, "the hit path must still attempt an audit write"
        assert second.hit is True, "the request itself must still succeed"
        assert second.status == 200
        assert second.body == "ok"
        assert second.audit_persisted is False

    def test_hit_records_zero_latency(self, audit: AuditLog) -> None:
        """A cache hit did no work — its audit row must say so.

        Kills ``latency_ms=0.0`` changed to ``None`` (which drops the row out of
        every duration aggregate) or to the original request's duration (which
        double-counts work that never happened).
        """
        h: IdempotencyHandler[str] = IdempotencyHandler(
            audit_log=audit, clock=iter([100.0, 100.5]).__next__
        )
        eid = str(uuid7())
        h.handle(event_id=eid, request="x", worker=_ok_worker)
        h.handle(event_id=eid, request="x", worker=_ok_worker)
        row = audit.lookup(eid)
        assert row is not None
        assert row.latency_ms == 0.0

    def test_hit_preserves_the_cached_status_and_body(self, audit: AuditLog) -> None:
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        eid = str(uuid7())
        h.handle(
            event_id=eid,
            request="x",
            worker=lambda _r: CachedResponse(status=202, body="queued"),
        )
        second = h.handle(event_id=eid, request="x", worker=_NeverRuns())
        assert (second.status, second.body, second.hit) == (202, "queued", True)
        row = audit.lookup(eid)
        assert row is not None
        assert row.response_status == 202


# ===========================================================================
# A row with no cached body is a miss, not a hollow hit
# ===========================================================================


@pytest.mark.unit
class TestBodylessRowIsTreatedAsAMiss:
    def test_ack_only_row_does_not_short_circuit_the_worker(self, audit: AuditLog) -> None:
        """``record_ack`` writes a real ``audit_log`` row with a NULL body.

        If ``_lookup`` returned a ``CachedResponse`` for it, the caller would
        get the ACK stub's ``response_status=0`` and an empty body — a hollow
        response for a request that never ran. The branch is documented as
        covered; it was not.
        """
        eid = str(uuid7())
        assert audit.record_ack(eid, ack_by="operator") is True
        assert audit.lookup(eid) is not None, "the ACK row exists"
        assert audit.lookup_response(eid) is None, "but carries no cached body"

        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        calls: list[int] = []

        def worker(_r: object) -> CachedResponse:
            calls.append(1)
            return CachedResponse(status=201, body="created")

        out = h.handle(event_id=eid, request="x", worker=worker)
        assert calls == [1], "a body-less row must not suppress the worker"
        assert out.hit is False
        assert (out.status, out.body) == (201, "created")

    def test_the_rerun_populates_the_cache_for_the_next_call(self, audit: AuditLog) -> None:
        eid = str(uuid7())
        audit.record_ack(eid, ack_by="operator")
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        h.handle(
            event_id=eid,
            request="x",
            worker=lambda _r: CachedResponse(status=201, body="created"),
        )
        second = h.handle(event_id=eid, request="x", worker=_NeverRuns())
        assert second.hit is True
        assert (second.status, second.body) == (201, "created")


# ===========================================================================
# Lock lifecycle on the paths the suite never drove
# ===========================================================================


@pytest.mark.unit
class TestLockLifecycleOnSuccessAndHit:
    def test_successful_calls_leave_no_lock_entries(self, audit: AuditLog) -> None:
        """The bounded-growth guarantee holds for successes, not just failures.

        The existing bounded test drives 50 worker *exceptions*; a mutant that
        releases the lock only in an ``except`` clause keeps it green while
        ``_event_locks`` grows without bound in production, where requests
        mostly succeed.
        """
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        for _ in range(50):
            h.handle(event_id=str(uuid7()), request="x", worker=_ok_worker)
        assert h._event_locks == {}

    def test_cache_hits_leave_no_lock_entries(self, audit: AuditLog) -> None:
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        eid = str(uuid7())
        for _ in range(10):
            h.handle(event_id=eid, request="x", worker=_ok_worker)
        assert h._event_locks == {}

    def test_concurrent_first_requests_serialise_on_one_lock_object(
        self, audit: AuditLog
    ) -> None:
        """Two threads arriving on a cold ``event_id`` must share one lock.

        Complements the issue #42 test, which proves the *exception* path keeps
        the entry alive. This pins the plain concurrent-arrival case: the second
        caller must be handed the same object, not a parallel one.
        """
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        eid = str(uuid7())
        first = h._acquire_lock(eid)
        second = h._acquire_lock(eid)
        try:
            assert first is second
            assert h._event_locks[eid].refs == 2
            h._release_lock(eid)
            assert eid in h._event_locks, "one referent remains — entry must survive"
            assert h._acquire_lock(eid) is first
            h._release_lock(eid)
        finally:
            h._release_lock(eid)
        assert eid not in h._event_locks


# ===========================================================================
# The re-check inside the lock
# ===========================================================================


@pytest.mark.unit
class TestSecondCallerUnderContentionGetsACacheHit:
    def test_blocked_duplicate_returns_a_hit_without_rerunning(self, audit: AuditLog) -> None:
        """A thread that waited on the lock must come out as a cache *hit*.

        ``test_hardening.py`` already proves the worker runs once under
        contention; what it does not check is what the losing thread reports.
        A mutant that drops the re-check inside the lock would re-run the worker
        — but a mutant that keeps the re-check while mislabelling the result
        (``hit=False``) is invisible to a run-count assertion alone.
        """
        h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
        eid = str(uuid7())
        in_worker = threading.Event()
        may_finish = threading.Event()
        runs: list[int] = []

        def blocking_worker(_r: object) -> CachedResponse:
            runs.append(1)
            in_worker.set()
            may_finish.wait(timeout=5.0)
            return CachedResponse(status=200, body="ok")

        results: dict[str, object] = {}

        def run(name: str, worker: Callable[[object], CachedResponse]) -> None:
            results[name] = h.handle(event_id=eid, request="x", worker=worker)

        ta = threading.Thread(target=run, args=("A", blocking_worker), name="A")
        ta.start()
        assert in_worker.wait(timeout=5.0), "A never entered the worker"

        tb = threading.Thread(target=run, args=("B", blocking_worker), name="B")
        tb.start()
        # Let B park on the per-event lock before A completes.
        time.sleep(0.05)
        may_finish.set()
        for t in (ta, tb):
            t.join(timeout=10.0)
            assert not t.is_alive(), f"thread {t.name} did not finish"

        assert len(runs) == 1, f"worker ran {len(runs)} times for one event_id"
        assert results["A"].hit is False  # type: ignore[union-attr]
        assert results["B"].hit is True, "the blocked duplicate must report a cache hit"  # type: ignore[union-attr]


# ===========================================================================
# Guard: the module's own uuid dependency
# ===========================================================================


@pytest.mark.unit
def test_v4_string_is_rejected_even_though_it_parses(audit: AuditLog) -> None:
    """Restates criterion 1.1 at the boundary the gate actually checks."""
    h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
    with pytest.raises(InvalidEventIdError):
        h.handle(event_id=str(uuid.uuid4()), request="x", worker=_NeverRuns())
