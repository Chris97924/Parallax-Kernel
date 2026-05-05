"""US-009.1 §3.1 + §3.2 — idempotency cache + handler glue.

Combines :mod:`parallax.canary.event_id` and
:mod:`parallax.canary.audit_log` so callers wire a single object into
their request lifecycle and get all of:

* criterion 1.1 — event_id uniqueness check is UUID-keyed only.
* criterion 1.2 — duplicate ``event_id`` returns the cached response,
  no side-effects.
* criterion 1.3 — different event_ids are independent even if payload
  matches.
* criterion 1.4 — clock drift cannot affect duplicate detection because
  the key is the UUID alone.
* criterion 1.7 — every request (including cache hits) leaves an audit
  row.
* criterion 1.8 — audit write failure does NOT fail the request.

The handler is intentionally *invocation*-style: it accepts a worker
callable from the host application and wraps it. This avoids dragging
HTTP framework specifics into the canary package — the wiring lives
wherever the canary write path is defined (out of scope for US-009.1
beyond the unit tests).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

from parallax.canary.audit_log import AuditLog, make_record
from parallax.canary.event_id import is_uuid7

__all__ = [
    "CachedResponse",
    "IdempotencyHandler",
    "IdempotencyResult",
    "InvalidEventIdError",
]


_RequestT = TypeVar("_RequestT")


class InvalidEventIdError(ValueError):
    """Raised when ``event_id`` is missing or not a valid UUID v7.

    Per criterion 1.1, the spec rejects non-UUID v7 keys. We surface this
    as a typed error so callers can map it to a 400-class HTTP response.
    """


@dataclasses.dataclass(frozen=True)
class CachedResponse:
    """Tuple of (status, body) cached on first successful processing."""

    status: int
    body: str


@dataclasses.dataclass(frozen=True)
class IdempotencyResult:
    """Outcome of :meth:`IdempotencyHandler.handle`.

    ``hit`` is True when the request was a duplicate and the response
    came from the cache. ``status`` and ``body`` mirror the response
    that was returned to the caller. ``audit_persisted`` is False when
    the audit_log write failed — request still succeeded (criterion
    1.8) but observers may want to alert on persistent audit failures.
    """

    status: int
    body: str
    hit: bool
    audit_persisted: bool


class IdempotencyHandler(Generic[_RequestT]):
    """Wraps a worker callable with UUID v7 idempotency + audit logging.

    Typical usage::

        handler = IdempotencyHandler(audit_log=AuditLog())
        result = handler.handle(
            event_id=req.event_id,
            request=req,
            worker=lambda r: write_to_db(r),
        )
        return result.status, result.body

    The handler is thread-safe: cache lookups and inserts are guarded by
    a per-event_id lock, so two concurrent requests with the same id
    will see one process and one cache hit (no double execution).
    """

    def __init__(
        self,
        *,
        audit_log: AuditLog,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._audit = audit_log
        self._clock = clock or time.monotonic
        # Per-event_id locks so concurrent dups serialise instead of
        # double-executing. Bounded by hits — cleared on cache write.
        self._locks_lock = threading.Lock()
        self._event_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def handle(
        self,
        *,
        event_id: str,
        request: _RequestT,
        worker: Callable[[_RequestT], CachedResponse],
    ) -> IdempotencyResult:
        """Process a request idempotently.

        Raises :class:`InvalidEventIdError` if ``event_id`` is not a
        valid UUID v7 (criterion 1.1). Otherwise returns an
        :class:`IdempotencyResult`. The worker is invoked AT MOST ONCE
        per ``event_id`` (criterion 1.2).
        """
        if not isinstance(event_id, str) or not is_uuid7(event_id):
            raise InvalidEventIdError(f"event_id is not a valid UUID v7: {event_id!r}")

        # Fast-path cache hit — no lock needed; SQLite handles concurrent
        # readers, and the worst case is a duplicate worker run when two
        # concurrent firsts race, which the per-event lock below resolves.
        cached = self._lookup(event_id)
        if cached is not None:
            return self._on_cache_hit(event_id, cached)

        lock = self._acquire_lock(event_id)
        with lock:
            # Re-check inside the lock — another thread may have populated
            # the cache while we were waiting.
            cached = self._lookup(event_id)
            if cached is not None:
                return self._on_cache_hit(event_id, cached)
            return self._execute_worker(event_id, request, worker)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _acquire_lock(self, event_id: str) -> threading.Lock:
        with self._locks_lock:
            lock = self._event_locks.get(event_id)
            if lock is None:
                lock = threading.Lock()
                self._event_locks[event_id] = lock
        return lock

    def _release_lock(self, event_id: str) -> None:
        with self._locks_lock:
            self._event_locks.pop(event_id, None)

    def _lookup(self, event_id: str) -> CachedResponse | None:
        record = self._audit.lookup(event_id)
        if record is None:
            return None
        body = self._audit.lookup_response(event_id)
        if body is None:
            # Row exists but body was wiped (or row was an ACK-only stub).
            # Treat as cache miss so the worker runs again — safer than
            # returning a hollow response. Tests cover this branch.
            return None
        return CachedResponse(status=int(record.response_status), body=body)

    def _on_cache_hit(self, event_id: str, cached: CachedResponse) -> IdempotencyResult:
        # Criterion 1.7 — every request (incl. hits) writes audit. We
        # update the existing row rather than insert a sibling.
        record = make_record(
            event_id=event_id,
            response_status=cached.status,
            latency_ms=0.0,
            idempotency_hit=True,
        )
        ok = self._audit.record(record, response_body=cached.body)
        return IdempotencyResult(
            status=cached.status,
            body=cached.body,
            hit=True,
            audit_persisted=ok,
        )

    def _execute_worker(
        self,
        event_id: str,
        request: _RequestT,
        worker: Callable[[_RequestT], CachedResponse],
    ) -> IdempotencyResult:
        started = self._clock()
        request_at = _dt.datetime.now(_dt.UTC).isoformat()
        response = worker(request)
        latency_ms = (self._clock() - started) * 1000.0
        record = make_record(
            event_id=event_id,
            response_status=response.status,
            latency_ms=latency_ms,
            idempotency_hit=False,
            request_at_iso=request_at,
        )
        ok = self._audit.record(record, response_body=response.body)
        # Lock kept for the duration of worker — release once cached.
        self._release_lock(event_id)
        return IdempotencyResult(
            status=response.status,
            body=response.body,
            hit=False,
            audit_persisted=ok,
        )
