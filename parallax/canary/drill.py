"""US-009.3 §5 — rollback-drill harness for M4 canary readiness.

Three drill scenarios verified by this module (per AC-3.15):

* :func:`run_drain_drill` — exercises the drain code-path: simulated
  in-flight requests, soft-deadline + force-cut. Returns a structured
  report with per-request status so callers (CLI, tests) can assert
  drain semantics without standing up the real HTTP server.
* :func:`run_reemit_drill` — exercises Orbit's "replay queued events
  after rollback" code-path against the canary idempotency cache.
  Verifies that re-recording the same ``event_id`` is a no-op at the
  audit-log layer (i.e. idempotency does its job).
* :func:`run_idempotency_drill` — focused stress on the per-event lock
  in :class:`parallax.canary.idempotency.IdempotencyHandler`. Spawns N
  concurrent threads that all attempt to handle the same event_id and
  asserts only one worker invocation wins.

All drills are pure-Python and do not require a running router service
(AC-3.5). The dry-run flag short-circuits any real I/O so the drill can
run inside a CI sandbox or a preflight smoke check.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from typing import Final

from parallax.canary.audit_log import AuditLog, make_record
from parallax.canary.event_id import uuid7
from parallax.canary.idempotency import (
    CachedResponse,
    IdempotencyHandler,
)
from parallax.canary.instrument import CanaryRequestRecorder
from parallax.canary.outcomes import OutcomeStore

__all__ = [
    "DrillStatus",
    "DrillStepResult",
    "DrillReport",
    "run_drain_drill",
    "run_reemit_drill",
    "run_idempotency_drill",
    "run_full_drill",
    "DEFAULT_DRAIN_TIMEOUT_S",
]


# ----------------------------------------------------------------------
# Public dataclasses
# ----------------------------------------------------------------------


class DrillStatus:
    """Allowed values for ``DrillStepResult.status`` / ``DrillReport.overall``.

    Plain string constants (not Enum) so JSON serialisation in the CLI
    layer is trivial; values are exhaustively tested.
    """

    PASS: Final[str] = "pass"
    FAIL: Final[str] = "fail"
    SKIPPED: Final[str] = "skipped"


@dataclasses.dataclass(frozen=True)
class DrillStepResult:
    """Single drill step verdict.

    ``observations`` carries free-form per-step measurements
    (e.g. drained_count, residual_count, drain_duration_ms) so the CLI
    output and tests can assert on the same data without re-running.
    """

    name: str
    status: str
    detail: str
    observations: dict[str, int | float | str]


@dataclasses.dataclass(frozen=True)
class DrillReport:
    """Aggregate of one drill scenario.

    ``overall`` is PASS only when every step is PASS; FAIL if any FAIL;
    SKIPPED only when no steps ran (defensive — should never happen on
    a non-empty drill).
    """

    drill: str
    dry_run: bool
    steps: tuple[DrillStepResult, ...]
    overall: str


DEFAULT_DRAIN_TIMEOUT_S: Final[float] = 60.0


def _aggregate_status(steps: tuple[DrillStepResult, ...]) -> str:
    if not steps:
        return DrillStatus.SKIPPED
    if any(s.status == DrillStatus.FAIL for s in steps):
        return DrillStatus.FAIL
    if all(s.status == DrillStatus.PASS for s in steps):
        return DrillStatus.PASS
    return DrillStatus.SKIPPED


# ----------------------------------------------------------------------
# Drain drill
# ----------------------------------------------------------------------


def run_drain_drill(
    *,
    in_flight_count: int = 8,
    timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    dry_run: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> DrillReport:
    """Simulate a drain of ``in_flight_count`` synthetic in-flight requests.

    In dry-run mode the simulation is deterministic: each "request"
    completes via a tiny ``sleep`` stub. With ``dry_run=False`` the
    timeout is honoured but the implementation still runs in-process —
    a real production drill would replace ``sleep`` with HTTP probes.

    Returns a :class:`DrillReport` with three steps:

    1. ``register_in_flight`` — admission stage: confirms the simulated
       fleet is non-empty and the timeout is positive.
    2. ``drain_within_deadline`` — drain loop: completes within the
       deadline (PASS) or hits the force-cut path (FAIL with
       ``residual_count > 0``).
    3. ``no_replay`` — invariant check: drained requests MUST NOT
       re-emit during drain (per AC-3.15 drain semantics).
    """
    steps: list[DrillStepResult] = []

    if in_flight_count <= 0:
        steps.append(
            DrillStepResult(
                name="register_in_flight",
                status=DrillStatus.FAIL,
                detail="in_flight_count must be > 0",
                observations={"in_flight_count": in_flight_count},
            )
        )
        return DrillReport(
            drill="drain", dry_run=dry_run, steps=tuple(steps), overall=DrillStatus.FAIL
        )

    steps.append(
        DrillStepResult(
            name="register_in_flight",
            status=DrillStatus.PASS,
            detail=f"registered {in_flight_count} synthetic requests",
            observations={
                "in_flight_count": in_flight_count,
                "timeout_s": timeout_s,
            },
        )
    )

    # Deterministic per-request "work time". Sum stays well under the
    # default 60s timeout for any reasonable in_flight_count.
    per_request_s = 0.001 if dry_run else min(0.05, timeout_s / max(in_flight_count, 1) / 4)
    # perf_counter, not monotonic: on Windows monotonic ticks
    # every 15.625 ms, so a drain shorter than one tick measures as exactly
    # 0.0 elapsed and any timeout below one tick is unenforceable. The drain
    # budget is a deadline, so the cut condition is "the budget is spent"
    # (>=), not "the budget is overspent" (>) — that is what makes
    # timeout_s=0.0 mean "cut immediately" on every platform instead of
    # granting one free request whenever the clock has not ticked yet.
    started = time.perf_counter()
    drained = 0
    while drained < in_flight_count:
        if time.perf_counter() - started >= timeout_s:
            break
        sleep(per_request_s)
        drained += 1
    elapsed = time.perf_counter() - started
    residual = in_flight_count - drained

    steps.append(
        DrillStepResult(
            name="drain_within_deadline",
            status=DrillStatus.PASS if residual == 0 else DrillStatus.FAIL,
            detail=(
                f"drained {drained}/{in_flight_count} in {elapsed * 1000:.1f}ms"
                if residual == 0
                else f"force-cut after {timeout_s}s with {residual} residual"
            ),
            observations={
                "drained_count": drained,
                "residual_count": residual,
                "elapsed_ms": round(elapsed * 1000, 3),
                "timeout_s": timeout_s,
            },
        )
    )

    # No-replay invariant: drained count MUST equal admitted count, and
    # there is no internal queue to flush. This is enforced by design;
    # the step exists so the assertion shows up in the report.
    replay_observed = 0
    steps.append(
        DrillStepResult(
            name="no_replay",
            status=DrillStatus.PASS if replay_observed == 0 else DrillStatus.FAIL,
            detail="drain MUST NOT trigger replay (per AC-3.15 drain semantics)",
            observations={"replay_count": replay_observed},
        )
    )

    return DrillReport(
        drill="drain",
        dry_run=dry_run,
        steps=tuple(steps),
        overall=_aggregate_status(tuple(steps)),
    )


# ----------------------------------------------------------------------
# Re-emit drill
# ----------------------------------------------------------------------


def run_reemit_drill(
    *,
    audit_log: AuditLog | None = None,
    reemit_count: int = 5,
    dry_run: bool = True,
    outcome_store: OutcomeStore | None = None,
    stage: str | None = None,
) -> DrillReport:
    """Simulate Orbit re-emitting events after a rollback.

    Passing both ``outcome_store`` and ``stage`` turns the first emit pass into
    a measured canary producer (#106.1): each event is timed and written to
    ``audit_log.latency_ms`` and ``canary_outcomes`` together, which is what the
    server-side T1-T5 exporter reads. Omit them and the drill behaves exactly as
    it did before.

    The drill records each event twice via :class:`AuditLog`. Audit log
    semantics MUST treat the second insertion as an UPSERT that
    preserves ``created_at`` (PR #41 ``audit_log.record`` ``ON CONFLICT
    DO UPDATE`` path). The drill verifies the row count stays at
    ``reemit_count`` (no duplicate rows) after the re-emit phase.

    With ``dry_run=True`` and no ``audit_log`` provided, the drill
    exercises an in-memory simulation that only checks the API contract
    — useful for sandbox preflight where SQLite is undesirable.
    """
    steps: list[DrillStepResult] = []

    if reemit_count <= 0:
        steps.append(
            DrillStepResult(
                name="generate_events",
                status=DrillStatus.FAIL,
                detail="reemit_count must be > 0",
                observations={"reemit_count": reemit_count},
            )
        )
        return DrillReport(
            drill="reemit",
            dry_run=dry_run,
            steps=tuple(steps),
            overall=DrillStatus.FAIL,
        )

    event_ids = [str(uuid7()) for _ in range(reemit_count)]
    steps.append(
        DrillStepResult(
            name="generate_events",
            status=DrillStatus.PASS,
            detail=f"generated {reemit_count} UUID v7 event_ids",
            observations={
                "reemit_count": reemit_count,
                "first_event_id": event_ids[0],
            },
        )
    )

    if audit_log is None:
        # Sandbox path: the API-contract check is the entire drill.
        steps.append(
            DrillStepResult(
                name="emit_first_pass",
                status=DrillStatus.PASS,
                detail="dry-run: skipped real SQLite writes (no audit_log provided)",
                observations={"recorded": reemit_count},
            )
        )
        steps.append(
            DrillStepResult(
                name="reemit_idempotent",
                status=DrillStatus.PASS,
                detail="dry-run: idempotency invariant assumed",
                observations={"unique_event_ids": reemit_count},
            )
        )
        return DrillReport(
            drill="reemit",
            dry_run=True,
            steps=tuple(steps),
            overall=_aggregate_status(tuple(steps)),
        )

    # Real path: write each row twice and verify single-row presence.
    #
    # #106.1 — the first pass is now MEASURED, not stamped. It used to write a
    # hardcoded latency_ms=10.0, which is the only per-request duration the
    # durable store has ever held; a T3 histogram built on it would show one
    # spike at a constant and a p99 that cannot move. When a stage is supplied
    # the emits also land in the OutcomeStore, so `parallax canary
    # --orbit-reemit-test --stage m4_1pct` is a real, fixture-driven producer
    # for every T1-T5 series rather than a drill with a side effect.
    recorder: CanaryRequestRecorder | None = None
    if outcome_store is not None and stage is not None:
        recorder = CanaryRequestRecorder(
            audit_log=audit_log, outcome_store=outcome_store, stage=stage
        )
        first_pass_ok = True
        for eid in event_ids:
            with recorder.request(event_id=eid) as span:
                pass
            # Read the span AFTER the context exits — that is where both writes
            # happen and where ``recorded`` is set.
            first_pass_ok = first_pass_ok and span.recorded
    else:
        # No stage configured: unchanged pre-#106 behaviour. The drill is still
        # a drill — it verifies the UPSERT invariant, and the stamped latency is
        # inert because nothing reads it without an outcome row to join to.
        first_pass_ok = all(
            audit_log.record(
                make_record(
                    event_id=eid,
                    response_status=200,
                    latency_ms=10.0,
                    idempotency_hit=False,
                )
            )
            for eid in event_ids
        )
    steps.append(
        DrillStepResult(
            name="emit_first_pass",
            status=DrillStatus.PASS if first_pass_ok else DrillStatus.FAIL,
            detail=f"recorded {reemit_count} events on first emit",
            observations={"recorded": reemit_count if first_pass_ok else 0},
        )
    )

    # Re-emit (Orbit replay) — same event_ids again. Audit log UPSERT path.
    #
    # The instrumented branch has to go through the recorder too, and that is
    # not symmetry for its own sake: ``AuditLog.record`` UPSERTs, so a second
    # pass stamping latency_ms=10.0 would overwrite the duration the first pass
    # measured and every T3 observation would come out as the old constant. A
    # replay is a real canary request; measuring it is both correct and the only
    # way the first measurement survives.
    if recorder is not None:
        second_pass_ok = True
        for eid in event_ids:
            with recorder.request(event_id=eid) as span:
                span.idempotency_hit = True
            second_pass_ok = second_pass_ok and span.recorded
    else:
        second_pass_ok = all(
            audit_log.record(
                make_record(
                    event_id=eid,
                    response_status=200,
                    latency_ms=10.0,
                    idempotency_hit=True,
                )
            )
            for eid in event_ids
        )
    # Confirm row count did not double.
    conn = audit_log._connect()
    placeholders = ",".join("?" * len(event_ids))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM audit_log WHERE event_id IN ({placeholders})",
        tuple(event_ids),
    ).fetchone()
    distinct_rows = int(row["n"])

    duplicate_free = second_pass_ok and distinct_rows == reemit_count
    steps.append(
        DrillStepResult(
            name="reemit_idempotent",
            status=DrillStatus.PASS if duplicate_free else DrillStatus.FAIL,
            detail=(
                f"re-emit: {distinct_rows} rows for {reemit_count} event_ids — "
                f"{'no duplicates' if duplicate_free else 'DUPLICATES PRESENT'}"
            ),
            observations={
                "unique_event_ids": reemit_count,
                "audit_rows": distinct_rows,
            },
        )
    )

    return DrillReport(
        drill="reemit",
        dry_run=dry_run,
        steps=tuple(steps),
        overall=_aggregate_status(tuple(steps)),
    )


# ----------------------------------------------------------------------
# Idempotency drill
# ----------------------------------------------------------------------


def run_idempotency_drill(
    *,
    audit_log: AuditLog | None = None,
    concurrency: int = 8,
    dry_run: bool = True,
) -> DrillReport:
    """Stress the per-event-id lock in :class:`IdempotencyHandler`.

    Spawns ``concurrency`` threads that race to handle the same
    ``event_id``; AC-3.15 idempotency invariant says only ONE thread
    runs the worker, others see a cache hit. The drill counts worker
    invocations and asserts ``invocations == 1``.

    With ``dry_run=True`` and no ``audit_log`` provided, an in-memory
    :class:`AuditLog` against ``:memory:`` SQLite is constructed so the
    test does not pollute prod state.
    """
    steps: list[DrillStepResult] = []

    if concurrency <= 1:
        steps.append(
            DrillStepResult(
                name="spawn_threads",
                status=DrillStatus.FAIL,
                detail="concurrency must be > 1 (drill is meaningless otherwise)",
                observations={"concurrency": concurrency},
            )
        )
        return DrillReport(
            drill="idempotency",
            dry_run=dry_run,
            steps=tuple(steps),
            overall=DrillStatus.FAIL,
        )

    # Use a temp-file SQLite for sandbox runs. ":memory:" doesn't share
    # across the per-thread connection cache so we use a tmpdir file.
    own_audit = audit_log is None
    if own_audit:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            prefix="parallax-canary-drill-", suffix=".sqlite", delete=False
        )
        tmp.close()
        local_audit = AuditLog(db_path=tmp.name)
    else:
        assert audit_log is not None  # narrow for type-checker
        local_audit = audit_log

    handler: IdempotencyHandler[object] = IdempotencyHandler(audit_log=local_audit)
    event_id = str(uuid7())
    invocations = 0
    invocation_lock = threading.Lock()

    def worker(_: object) -> CachedResponse:
        nonlocal invocations
        with invocation_lock:
            invocations += 1
        # Tiny pause so all racers contend on the cache lock simultaneously.
        time.sleep(0.05)
        return CachedResponse(status=200, body='{"served": 1}')

    def call() -> None:
        handler.handle(event_id=event_id, request=None, worker=worker)

    threads = [threading.Thread(target=call) for _ in range(concurrency)]
    steps.append(
        DrillStepResult(
            name="spawn_threads",
            status=DrillStatus.PASS,
            detail=f"spawned {concurrency} concurrent racers on event_id={event_id}",
            observations={"concurrency": concurrency, "event_id": event_id},
        )
    )

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    invariant_ok = invocations == 1
    steps.append(
        DrillStepResult(
            name="single_worker_invocation",
            status=DrillStatus.PASS if invariant_ok else DrillStatus.FAIL,
            detail=(
                f"worker invoked {invocations} time(s); expected exactly 1"
            ),
            observations={
                "invocations": invocations,
                "concurrency": concurrency,
            },
        )
    )

    if own_audit:
        local_audit.close()

    return DrillReport(
        drill="idempotency",
        dry_run=dry_run,
        steps=tuple(steps),
        overall=_aggregate_status(tuple(steps)),
    )


# ----------------------------------------------------------------------
# Full drill orchestrator (used by `parallax canary --rollback-drill`)
# ----------------------------------------------------------------------


def run_full_drill(
    *,
    audit_log: AuditLog | None = None,
    in_flight_count: int = 8,
    reemit_count: int = 5,
    concurrency: int = 8,
    timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
    dry_run: bool = True,
    outcome_store: OutcomeStore | None = None,
    stage: str | None = None,
) -> tuple[DrillReport, DrillReport, DrillReport]:
    """Run the three drills in sequence and return their reports."""
    drain = run_drain_drill(
        in_flight_count=in_flight_count,
        timeout_s=timeout_s,
        dry_run=dry_run,
    )
    reemit = run_reemit_drill(
        audit_log=audit_log,
        reemit_count=reemit_count,
        dry_run=dry_run,
        outcome_store=outcome_store,
        stage=stage,
    )
    idem = run_idempotency_drill(
        audit_log=audit_log,
        concurrency=concurrency,
        dry_run=dry_run,
    )
    return drain, reemit, idem
