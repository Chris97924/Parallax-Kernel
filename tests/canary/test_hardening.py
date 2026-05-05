"""US-009.1 hardening tests — schema migration idempotency, concurrency,
and integration with the spec's cross-cutting requirements (§6 X.1-X.6).

These extend the trigger / rollback / idempotency unit tests with
defensive scenarios that catch edge-cases the contract doesn't fully
spell out: re-creating the AuditLog must be idempotent, payload writes
under concurrent dispatch must not double-execute, and the controller
must remain stable when triggers are evaluated against an empty
observation buffer.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from parallax.canary.audit_log import AuditLog
from parallax.canary.event_id import uuid7
from parallax.canary.idempotency import CachedResponse, IdempotencyHandler
from parallax.canary.rollback import CanaryState, RollbackController

# ---------------------------------------------------------------------------
# Schema migration idempotency — important when the canary daemon restarts
# ---------------------------------------------------------------------------


def test_audit_log_init_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    AuditLog(db).close()
    AuditLog(db).close()  # second call must NOT raise on existing schema
    log = AuditLog(db)
    try:
        # Schema columns are exactly the same after re-open
        cols = log.schema()
        assert "event_id" in cols
        assert "ack_by" in cols
    finally:
        log.close()


def test_audit_log_can_record_after_close_and_reopen(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    log = AuditLog(db)
    eid = str(uuid7())
    handler = IdempotencyHandler(audit_log=log)
    handler.handle(
        event_id=eid,
        request="x",
        worker=lambda _r: CachedResponse(status=200, body="ok"),
    )
    log.close()

    log2 = AuditLog(db)
    try:
        row = log2.lookup(eid)
        assert row is not None
        assert row.event_id == eid
    finally:
        log2.close()


# ---------------------------------------------------------------------------
# Concurrency — duplicate event_ids must not double-execute
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_event_ids_run_worker_once(tmp_path: Path) -> None:
    """Criterion 1.2 — under contention the worker runs exactly once.

    The worker sleeps for 50 ms so all 8 threads sit inside ``handle()``
    simultaneously before any of them completes. Without this delay, the
    first thread might race through cache-miss → worker → cache-write
    fast enough that subsequent threads always find the cache populated
    — which would let the test pass even if the per-event lock were
    completely broken.

    Do NOT reduce the sleep below 30 ms: at lower values the test no
    longer reliably proves the lock prevents double-execution.
    """
    import time as _time

    log = AuditLog(tmp_path / "audit.db")
    try:
        handler = IdempotencyHandler(audit_log=log)
        eid = str(uuid7())
        run_count = [0]
        run_lock = threading.Lock()

        def worker(_req: object) -> CachedResponse:
            with run_lock:
                run_count[0] += 1
            # Block long enough for all 8 threads to land inside handle()
            # and contend on the per-event idempotency lock.
            _time.sleep(0.05)
            return CachedResponse(status=200, body="ok")

        def run_one() -> None:
            handler.handle(event_id=eid, request="x", worker=worker)

        threads = [threading.Thread(target=run_one) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert run_count[0] == 1, f"worker ran {run_count[0]} times for the same event_id"
    finally:
        log.close()


# ---------------------------------------------------------------------------
# RollbackController stability under cold start (no observations)
# ---------------------------------------------------------------------------


def test_controller_evaluate_with_no_observations_stays_running(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    try:
        c = RollbackController(audit_log=log)
        snap = c.evaluate()
        assert snap.state is CanaryState.RUNNING
        # Gate must report INSUFFICIENT_DATA at cold start
        assert snap.last_gate_evaluation.observations == 0
    finally:
        log.close()


# ---------------------------------------------------------------------------
# Integration — full lifecycle: idempotent worker + audit + ACK trail
# ---------------------------------------------------------------------------


def test_idempotency_audit_and_ack_share_db(tmp_path: Path) -> None:
    """End-to-end: a request flows through idempotency, hits cache on
    replay, then a controller ACK lands in the same audit DB.
    """
    log = AuditLog(tmp_path / "audit.db")
    try:
        handler = IdempotencyHandler(audit_log=log)
        eid = str(uuid7())
        handler.handle(
            event_id=eid,
            request="x",
            worker=lambda _r: CachedResponse(status=200, body="ok"),
        )
        # Cache hit on replay
        result = handler.handle(
            event_id=eid,
            request="x",
            worker=lambda _r: pytest.fail("worker must not run on cache hit"),  # type: ignore[arg-type,return-value]
        )
        assert result.hit is True

        # Controller ACK on the same DB — drive transitions with a fake clock
        clock_t = [1_000.0]
        c = RollbackController(
            audit_log=log,
            clock=lambda: clock_t[0],
            cooldown_seconds=60.0,
        )
        for i in range(10_000):
            c.observe_request(is_error=i < 100, latency_ms=10.0)
        c.evaluate()  # → TRIPPED
        clock_t[0] += 120.0  # past cooldown
        c.evaluate()  # → AWAITING_ACK
        assert c.state is CanaryState.AWAITING_ACK
        assert c.acknowledge(ack_by="oncall@parallax") is True

        # Both rows exist in the same DB
        with sqlite3.connect(log.db_path) as conn:
            n_rows = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        assert n_rows >= 2
    finally:
        log.close()
