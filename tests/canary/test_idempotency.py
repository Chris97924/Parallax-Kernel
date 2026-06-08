"""US-009.1 §3.1 + §3.2 — IdempotencyHandler tests (criteria 1.1-1.8)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from parallax.canary.audit_log import AuditLog
from parallax.canary.event_id import uuid7
from parallax.canary.idempotency import (
    CachedResponse,
    IdempotencyHandler,
    InvalidEventIdError,
)


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    log = AuditLog(tmp_path / "audit.db")
    yield log
    log.close()


def _ok_worker(payload: object) -> CachedResponse:
    return CachedResponse(status=200, body=json.dumps({"echo": payload}))


def _counting_worker(box: list[int]) -> Callable[[object], CachedResponse]:
    def w(payload: object) -> CachedResponse:
        box.append(1)
        return CachedResponse(status=201, body="created")

    return w


# ---------------------------------------------------------------------------
# Criterion 1.1 — event_id MUST be UUID v7 (not hash-with-timestamp)
# ---------------------------------------------------------------------------


def test_handler_rejects_non_uuid_event_id(audit: AuditLog) -> None:
    h = IdempotencyHandler(audit_log=audit)
    with pytest.raises(InvalidEventIdError):
        h.handle(event_id="not-a-uuid", request="x", worker=_ok_worker)


def test_handler_rejects_uuid_v4_event_id(audit: AuditLog) -> None:
    import uuid

    h = IdempotencyHandler(audit_log=audit)
    with pytest.raises(InvalidEventIdError):
        h.handle(event_id=str(uuid.uuid4()), request="x", worker=_ok_worker)


def test_handler_accepts_valid_uuid_v7(audit: AuditLog) -> None:
    h = IdempotencyHandler(audit_log=audit)
    eid = str(uuid7())
    out = h.handle(event_id=eid, request="x", worker=_ok_worker)
    assert out.status == 200
    assert out.hit is False


# ---------------------------------------------------------------------------
# Criterion 1.2 — duplicate event_id returns cached response, no side-effects
# ---------------------------------------------------------------------------


def test_duplicate_event_id_returns_cached_response(audit: AuditLog) -> None:
    h = IdempotencyHandler(audit_log=audit)
    box: list[int] = []
    worker = _counting_worker(box)
    eid = str(uuid7())

    first = h.handle(event_id=eid, request="payload", worker=worker)
    second = h.handle(event_id=eid, request="payload", worker=worker)

    assert first.hit is False and second.hit is True
    assert first.status == second.status == 201
    assert first.body == second.body == "created"
    assert len(box) == 1, "worker must run exactly once for duplicate event_id"


def test_duplicate_event_id_does_not_invoke_downstream(audit: AuditLog) -> None:
    h = IdempotencyHandler(audit_log=audit)
    eid = str(uuid7())
    h.handle(event_id=eid, request="x", worker=_ok_worker)

    def explode(_: object) -> CachedResponse:  # pragma: no cover — must not run
        raise AssertionError("downstream worker must not run on cache hit")

    out = h.handle(event_id=eid, request="x", worker=explode)
    assert out.hit is True


# ---------------------------------------------------------------------------
# Criterion 1.3 — different event_ids are independent even with same payload
# ---------------------------------------------------------------------------


def test_different_event_ids_run_independently(audit: AuditLog) -> None:
    h = IdempotencyHandler(audit_log=audit)
    box: list[int] = []
    worker = _counting_worker(box)
    eid_a = str(uuid7())
    eid_b = str(uuid7())
    h.handle(event_id=eid_a, request="same", worker=worker)
    h.handle(event_id=eid_b, request="same", worker=worker)
    assert len(box) == 2


# ---------------------------------------------------------------------------
# Criterion 1.4 — clock drift cannot affect duplicate detection
# ---------------------------------------------------------------------------


def test_clock_drift_does_not_break_idempotency(audit: AuditLog) -> None:
    """Even when the wall clock jumps ±10 min, ``event_id`` is the only key.

    The handler does not use timestamps for its idempotency key — this
    test injects a frozen clock to make the invariant explicit.
    """
    eid = str(uuid7())
    drifting = iter([1_000.0, 1_000.0 + 600.0, 1_000.0 - 600.0, 1_000.0 - 600.0])
    h = IdempotencyHandler(audit_log=audit, clock=lambda: next(drifting))

    first = h.handle(event_id=eid, request="x", worker=_ok_worker)
    second = h.handle(event_id=eid, request="x", worker=_ok_worker)

    assert first.hit is False
    assert second.hit is True
    assert second.body == first.body


# ---------------------------------------------------------------------------
# Criterion 1.7 — every request (incl. cache hit) writes audit
# ---------------------------------------------------------------------------


def test_audit_row_written_on_first_call(audit: AuditLog) -> None:
    h = IdempotencyHandler(audit_log=audit)
    eid = str(uuid7())
    h.handle(event_id=eid, request="x", worker=_ok_worker)
    row = audit.lookup(eid)
    assert row is not None
    assert row.idempotency_hit is False
    assert row.response_status == 200


def test_audit_row_marked_hit_on_duplicate(audit: AuditLog) -> None:
    h = IdempotencyHandler(audit_log=audit)
    eid = str(uuid7())
    h.handle(event_id=eid, request="x", worker=_ok_worker)
    h.handle(event_id=eid, request="x", worker=_ok_worker)
    row = audit.lookup(eid)
    assert row is not None
    assert row.idempotency_hit is True


# ---------------------------------------------------------------------------
# Criterion 1.8 — audit failure must NOT fail the request
# ---------------------------------------------------------------------------


def test_audit_write_failure_does_not_fail_request(
    audit: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = IdempotencyHandler(audit_log=audit)
    eid = str(uuid7())
    monkeypatch.setattr(audit, "record", lambda *a, **kw: False)
    out = h.handle(event_id=eid, request="x", worker=_ok_worker)
    assert out.status == 200, "request must succeed even when audit write fails"
    assert out.audit_persisted is False


# ---------------------------------------------------------------------------
# Worker exception must release the per-event lock entry
# ---------------------------------------------------------------------------


class _BoomError(RuntimeError):
    """Sentinel exception used to verify propagation + cleanup."""


def test_worker_exception_releases_per_event_lock(audit: AuditLog) -> None:
    """Worker exception propagates AND the eid is removed from _event_locks,
    so a subsequent handle() with the same eid is not blocked by a stale lock.
    """
    h = IdempotencyHandler(audit_log=audit)
    eid = str(uuid7())

    def boom(_: object) -> CachedResponse:
        raise _BoomError("downstream blew up")

    with pytest.raises(_BoomError):
        h.handle(event_id=eid, request="x", worker=boom)

    assert eid not in h._event_locks, "per-event lock must be released even when worker raises"

    out = h.handle(event_id=eid, request="x", worker=_ok_worker)
    assert out.status == 200
    assert out.hit is False


# ---------------------------------------------------------------------------
# Issue #42 — worker-exception lock lifecycle must not allow concurrent
# double-execution of the worker for the same event_id.
# ---------------------------------------------------------------------------


def test_worker_exception_does_not_allow_concurrent_double_execution(
    audit: AuditLog,
) -> None:
    """Reproduce the issue #42 race deterministically and assert it is closed.

    Sequence under contention (issue #42):

    1. Thread A acquires the per-event lock, enters the worker, and the worker
       raises while still holding the lock.
    2. Thread B has already taken a reference to the *same* lock object and is
       blocked on ``with lock:``.
    3. When A unwinds, the eid must NOT be dropped from ``_event_locks`` while B
       still references it — otherwise a later thread C creates a *fresh* lock
       for the same eid and runs the worker concurrently with B.

    The assertion is behavioural: the worker is never inside its body for the
    same eid on two threads at once (``max_concurrency == 1``). On the pre-fix
    code C runs against a brand-new lock and ``max_concurrency`` reaches 2.
    """
    h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)
    eid = str(uuid7())

    # Worker-side concurrency guard — the actual bug detector.
    guard = threading.Lock()
    in_worker = 0
    max_concurrency = 0

    def _enter_worker() -> None:
        nonlocal in_worker, max_concurrency
        with guard:
            in_worker += 1
            max_concurrency = max(max_concurrency, in_worker)

    def _exit_worker() -> None:
        nonlocal in_worker
        with guard:
            in_worker -= 1

    a_in_worker = threading.Event()
    a_may_raise = threading.Event()
    b_acquired = threading.Event()
    b_in_worker = threading.Event()

    def worker_a(_payload: object) -> CachedResponse:
        _enter_worker()
        a_in_worker.set()
        a_may_raise.wait(timeout=5.0)
        try:
            raise _BoomError("A worker blew up while holding the lock")
        finally:
            _exit_worker()

    def worker_bc(_payload: object) -> CachedResponse:
        name = threading.current_thread().name
        _enter_worker()
        try:
            if name == "B":
                b_in_worker.set()
            # Widen the window so any genuine overlap with C is observed.
            time.sleep(0.05)
            return CachedResponse(status=200, body="ok")
        finally:
            _exit_worker()

    # Instrument _acquire_lock so we know exactly when B has obtained its lock
    # reference (and is about to block on ``with lock:``).
    orig_acquire = type(h)._acquire_lock

    def traced_acquire(event_id: str) -> threading.Lock:
        lock = orig_acquire(h, event_id)
        if threading.current_thread().name == "B":
            b_acquired.set()
        return lock

    h._acquire_lock = traced_acquire  # type: ignore[method-assign]

    errors: dict[str, BaseException] = {}

    def run(name: str, worker: Callable[[object], CachedResponse]) -> None:
        try:
            h.handle(event_id=eid, request="x", worker=worker)
        except BaseException as exc:  # noqa: BLE001 - captured for assertion
            errors[name] = exc

    ta = threading.Thread(target=run, args=("A", worker_a), name="A")
    ta.start()
    assert a_in_worker.wait(timeout=5.0), "A never entered the worker"

    tb = threading.Thread(target=run, args=("B", worker_bc), name="B")
    tb.start()
    assert b_acquired.wait(timeout=5.0), "B never acquired its lock reference"
    # Give B a beat to park on ``with lock:`` (A still holds it).
    time.sleep(0.05)

    # A raises now. On the buggy code A drops eid from _event_locks here while
    # B still holds the old lock.
    a_may_raise.set()
    assert b_in_worker.wait(timeout=5.0), "B never reached the worker after A raised"

    # C arrives only after B is inside the worker — i.e. after A's unwind. On the
    # buggy code C gets a brand-new lock and runs concurrently with B.
    tc = threading.Thread(target=run, args=("C", worker_bc), name="C")
    tc.start()

    for t in (ta, tb, tc):
        t.join(timeout=10.0)
        assert not t.is_alive(), f"thread {t.name} did not finish"

    assert max_concurrency == 1, (
        "worker ran concurrently for the same event_id "
        f"(max_concurrency={max_concurrency}) — issue #42 race is open"
    )
    assert isinstance(errors.get("A"), _BoomError), "A's exception must propagate"
    assert "B" not in errors, f"B should have succeeded, got {errors.get('B')!r}"
    assert "C" not in errors, f"C should have succeeded, got {errors.get('C')!r}"
    # All references released → entry removed (memory-bounded).
    assert eid not in h._event_locks


def test_repeated_worker_exceptions_keep_event_locks_bounded(audit: AuditLog) -> None:
    """PR #41's memory-bounded property holds under the refcount lifecycle:
    repeated worker exceptions must not grow ``_event_locks``.
    """
    h: IdempotencyHandler[str] = IdempotencyHandler(audit_log=audit)

    def boom(_: object) -> CachedResponse:
        raise _BoomError("nope")

    for _ in range(50):
        eid = str(uuid7())
        with pytest.raises(_BoomError):
            h.handle(event_id=eid, request="x", worker=boom)

    assert h._event_locks == {}, "lock dict must be empty after exceptions drain"
