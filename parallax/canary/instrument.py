"""#106.1 — the write side of T3: measure a canary request, persist what it took.

The other five canary series are a read over state the canary already keeps.
T3 is not: ``#106`` called it "the hardest of the six to produce even with a
server-side exporter — the durable OutcomeStore records outcomes, not
per-request durations, so T3 would need new instrumentation rather than a read
over existing state."

The instrumentation is small because the column was already there. ``audit_log``
has a nullable ``latency_ms`` that nothing measured — the re-emit drill wrote a
hardcoded ``10.0``, which would have produced a histogram with one spike at a
constant and a p99 that never moves. :class:`CanaryRequestRecorder` closes the
loop: it times the request with ``perf_counter``, writes the real elapsed
milliseconds to ``audit_log`` and the business outcome to ``canary_outcomes``,
both keyed by the same ``event_id`` so the exporter's join resolves.

One writer, two tables, one span. Recording them separately is what let the two
halves drift apart in the first place — an audit row with no outcome row is
invisible to every T-series, because the outcome table is the event ledger.

Usage::

    recorder = CanaryRequestRecorder(audit_log=log, outcome_store=store, stage="m4_1pct")
    with recorder.request() as span:
        span.response_status = do_the_work()
        span.outcome = "ok"
"""

from __future__ import annotations

import contextlib
import dataclasses
import time
from collections.abc import Iterator

from parallax.canary.audit_log import AuditLog, make_record
from parallax.canary.event_id import uuid7
from parallax.canary.outcomes import OutcomeStore

__all__ = ["CanaryRequestRecorder", "CanaryRequestSpan"]


@dataclasses.dataclass
class CanaryRequestSpan:
    """Mutable handle a caller fills in while the request runs.

    The defaults describe a successful request, so a caller that only cares
    about timing does not have to say anything. ``outcome`` must be one of
    :data:`parallax.canary.outcomes.KNOWN_OUTCOMES`; an invalid value raises
    from ``OutcomeStore.record`` on exit rather than being silently coerced,
    because a mislabelled outcome corrupts the T2/T4 numerators.
    """

    event_id: str
    response_status: int = 200
    outcome: str = "ok"
    idempotency_hit: bool = False
    #: Populated on exit. Present so a caller (and a test) can assert on the
    #: number that was persisted rather than re-measuring it.
    latency_ms: float | None = None
    #: Populated on exit: True iff BOTH writes landed. A caller reporting a
    #: PASS/FAIL verdict has to read this — ``AuditLog.record`` is spec'd
    #: fire-and-forget and returns False rather than raising, so a drill that
    #: ignored it would report success for events that never reached the store
    #: and the T-series would come out short with nothing having failed.
    recorded: bool = False


class CanaryRequestRecorder:
    """Times canary requests and persists duration + outcome durably."""

    def __init__(
        self,
        *,
        audit_log: AuditLog,
        outcome_store: OutcomeStore,
        stage: str,
    ) -> None:
        self._audit_log = audit_log
        self._outcome_store = outcome_store
        self._stage = stage

    @property
    def stage(self) -> str:
        return self._stage

    @contextlib.contextmanager
    def request(self, *, event_id: str | None = None) -> Iterator[CanaryRequestSpan]:
        """Time one canary request and record it on exit.

        ``perf_counter`` rather than wall-clock: this is a duration, and a wall
        clock that steps (NTP, DST) would produce negative or inflated latencies
        that land in the wrong histogram bucket.

        Recorded in a ``finally`` so a request that raises is still measured and
        still counted. Dropping the failures would bias T3 towards the fast path
        — the requests most likely to be slow are exactly the ones that break.
        The span's own defaults are not overridden here: a caller who raises
        before setting ``outcome`` records the ``ok`` default, so callers that
        can fail should set ``response_status``/``outcome`` before doing work
        that might raise.
        """
        span = CanaryRequestSpan(event_id=event_id or str(uuid7()))
        started = time.perf_counter()
        try:
            yield span
        finally:
            span.latency_ms = (time.perf_counter() - started) * 1000.0
            audit_ok = self._audit_log.record(
                make_record(
                    event_id=span.event_id,
                    response_status=span.response_status,
                    latency_ms=span.latency_ms,
                    idempotency_hit=span.idempotency_hit,
                )
            )
            outcome_ok = self._outcome_store.record(
                event_id=span.event_id,
                stage=self._stage,
                outcome=span.outcome,
            )
            # Both, not either: the exporter joins the two tables, so an event
            # with only one half written contributes to no series at all.
            span.recorded = bool(audit_ok and outcome_ok)
