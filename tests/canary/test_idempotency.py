"""US-009.1 §3.1 + §3.2 — IdempotencyHandler tests (criteria 1.1-1.8)."""

from __future__ import annotations

import json
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
# Codex P2 (PR #41) — worker exception must release the per-event lock
# ---------------------------------------------------------------------------


class _BoomError(RuntimeError):
    """Custom exception used to verify exception propagation + cleanup."""


def test_worker_exception_releases_per_event_lock(audit: AuditLog) -> None:
    """If worker raises, _event_locks must not retain the event_id.

    Pre-fix: cleanup ran after worker(), so any worker exception leaked
    the lock entry forever and unbounded growth of _event_locks could
    degrade long-running processes (Codex P2 PR #41).
    """
    h = IdempotencyHandler(audit_log=audit)
    eid = str(uuid7())

    def boom(_: object) -> CachedResponse:
        raise _BoomError("downstream blew up")

    with pytest.raises(_BoomError):
        h.handle(event_id=eid, request="x", worker=boom)

    assert eid not in h._event_locks, (
        "per-event lock must be released even when worker raises"
    )

    # Sanity: a retry with the same event_id and a non-raising worker
    # must succeed. If the lock were stuck, this would deadlock or hit
    # a stale lock object.
    out = h.handle(event_id=eid, request="x", worker=_ok_worker)
    assert out.status == 200
    assert out.hit is False
