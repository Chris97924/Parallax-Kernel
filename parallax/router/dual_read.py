"""M3-T1.2 — DualReadRouter flag-gated dual-read wrapper (US-011).

Mirrors ``parallax/router/shadow.py:111-198`` (ShadowInterceptor decorator
pattern, Q7 decision: keep parallel, do NOT modify shadow.py).

Hard invariants — never violated:
1. ``DualReadResult.primary`` always set to canonical result, even on failures.
2. If flag is off (or dual_read_override=False): return immediately with
   ``outcome="skipped"``, no Aphelion call, zero overhead.
3. Q5 CHANGE_TRACE.legacy_kind=bug short-circuit: outcome="skipped", no secondary.
4. Fail-closed: any secondary exception → outcome="aphelion_unreachable" or
   "primary_only"; primary still returned.
5. ``_hits_equal`` imported from shadow.py (not copied).
6. Outcome classification: match / diverge / aphelion_unreachable / primary_only / skipped.
7. Parallel dispatch via ThreadPoolExecutor(max_workers=2) with secondary timeout.
8. Live counter integration (optional, None-safe).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

from parallax.events.conflict_writer import write_conflict_event
from parallax.obs.log import get_logger
from parallax.router.aphelion_stub import AphelionUnreachableError
from parallax.router.circuit_breaker import get_breaker_state
from parallax.router.config import is_dual_read_enabled
from parallax.router.contracts import DualReadResult, QueryRequest
from parallax.router.discrepancy_live import (
    DualReadOutcome,
    record_dual_read_outcome,
)
from parallax.router.dual_read_decision_log import append_decision
from parallax.router.live_arbitration import arbitrate
from parallax.router.ports import QueryPort

# Import _hits_equal from shadow.py directly (Q7: reuse, do NOT copy-paste).
# Importing shadow.py is safe: it has no module-level side-effects that would
# break existing tests — its only side-effects are creating a log instance and
# reading SHADOW_LOG_DIR from env (done inside ShadowInterceptor.__init__, not
# at module scope).
from parallax.router.shadow import _hits_equal
from parallax.router.types import QueryType

if TYPE_CHECKING:
    from parallax.router.discrepancy_live import LiveDiscrepancyCounter

__all__ = ["DualReadRouter"]

_log = get_logger("parallax.router.dual_read")


def _safe_log_warning(event: str, **extras: object) -> None:
    """Emit a structured WARNING that never propagates a logger exception.

    The fail-closed invariant requires observability code to never crash
    the request path.  ``_log.warning`` itself can raise if the logger
    handler is closed or misconfigured (rare but possible during process
    shutdown), so every call site through this module routes through
    here so the request path is fully insulated.
    """
    try:
        _log.warning(event, extra={"event": event, **extras})
    except Exception:  # noqa: BLE001 — last-resort: even the logger may be broken
        pass


class DualReadRouter:
    """Flag-gated dual-read wrapper. Fail-closed to canonical on any failure.

    ``primary`` is the canonical QueryPort (e.g. RealMemoryRouter).
    ``secondary`` is the Aphelion adapter (e.g. AphelionReadAdapter stub).

    The ``dual_read_override`` parameter on ``query()`` allows per-request
    snapshots (T1.4 middleware) to override the env flag without re-reading env
    mid-request.  Pass ``True`` to force-enable, ``False`` to force-disable,
    or ``None`` to fall back to ``is_dual_read_enabled()``.

    Wiring contract for callers in a request context (M3b/M4):
        Route handlers calling ``query()`` MUST pass
        ``dual_read_override=request.state.dual_read``.  The middleware in
        ``parallax/server/middleware/dual_read_snapshot.py`` snapshots both
        the env flag AND the circuit-breaker state at request entry — passing
        the snapshot through is the only way to honor a tripped breaker for
        the duration of an in-flight request.

        If ``dual_read_override`` is ``None`` while the circuit breaker is
        currently tripped, ``query()`` logs a WARNING (event
        ``dual_read_override_missing_with_tripped_breaker``) so this wiring
        gap surfaces in production logs rather than silently ignoring the
        breaker.  The query still executes — the warning is observability,
        not a hard failure.
    """

    def __init__(
        self,
        *,
        primary: QueryPort,
        secondary: QueryPort,
        live_counter: LiveDiscrepancyCounter | None = None,
        executor: ThreadPoolExecutor | None = None,
        secondary_timeout_ms: float = 100.0,
        events_conn: sqlite3.Connection | None = None,
    ) -> None:
        """初始化雙讀取路由器，設定主要與次要查詢埠及相關參數。

        Args:
            primary: 主要查詢埠（正規來源，例如 RealMemoryRouter）。
            secondary: 次要查詢埠（Aphelion 適配器）。
            live_counter: 即時差異計數器，為 None 時停用計數功能。
            executor: 執行緒池，預設使用 max_workers=2 的 ThreadPoolExecutor。
            secondary_timeout_ms: 次要查詢的逾時時間（毫秒）。
            events_conn: 事件資料庫連線，用於記錄仲裁衝突事件；為 None 時停用。
        """
        self._primary = primary
        self._secondary = secondary
        self._live_counter = live_counter
        self._executor = executor or ThreadPoolExecutor(max_workers=2)
        self._secondary_timeout_ms = secondary_timeout_ms
        # M3b Phase 2 (US-005): optional events DB connection used to
        # emit ``arbitration_conflict`` rows when an arbitration decision
        # requires manual review.  None disables conflict-event logging
        # (e.g. unit tests for the dual-read mechanics that don't care
        # about the events table).
        self._events_conn = events_conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(
        self,
        request: QueryRequest,
        *,
        correlation_id: str | None = None,
        dual_read_override: bool | None = None,
    ) -> DualReadResult:
        """Dispatch primary + secondary in parallel; return DualReadResult.

        Invariant: always returns a result with ``primary`` set.  Secondary
        failures are caught and classified — they never propagate to the caller.
        Primary failures DO propagate (fail-closed in the canonical direction).
        """
        cid = correlation_id if correlation_id is not None else str(uuid.uuid4())

        # ------------------------------------------------------------------
        # Wiring-trap guard: if a route handler forgot to pass the middleware
        # snapshot AND the breaker is tripped, the env-fallback path silently
        # ignores the breaker.  Surface the gap as a WARNING so it shows up
        # in production logs and operators can fix the wire.
        # ------------------------------------------------------------------
        if dual_read_override is None:
            try:
                breaker_tripped = get_breaker_state().is_tripped()
            except Exception as exc:  # noqa: BLE001 — observability never crashes the request
                # Surface the swallowed exception so an operator can tell
                # "breaker fine" apart from "breaker check exploded".
                _safe_log_warning(
                    "breaker_is_tripped_check_failed",
                    exc_class=type(exc).__name__,
                    exc_str=str(exc),
                )
                breaker_tripped = False
            if breaker_tripped:
                _safe_log_warning(
                    "dual_read_override_missing_with_tripped_breaker",
                    user_id=request.user_id,
                )

        # ------------------------------------------------------------------
        # Fast-path: flag off → skipped (zero overhead beyond 2 bool checks)
        # ------------------------------------------------------------------
        enabled = dual_read_override if dual_read_override is not None else is_dual_read_enabled()
        if not enabled:
            primary_start = time.perf_counter()
            primary_result = self._primary.query(request)
            latency_primary_ms = (time.perf_counter() - primary_start) * 1000.0
            result = DualReadResult(
                outcome="skipped",
                primary=primary_result,
                secondary=None,
                correlation_id=cid,
                latency_primary_ms=latency_primary_ms,
                latency_secondary_ms=None,
                aphelion_unreachable_reason=None,
            )
            self._record(request.user_id, result.outcome)
            self._log_decision(
                outcome="skipped",
                correlation_id=cid,
                query_type=request.query_type.value,
                winning_source=None,
                policy_version="",
                conflict_event_id=None,
                write_error_observed=False,
            )
            return result

        # ------------------------------------------------------------------
        # Q5 short-circuit: CHANGE_TRACE.legacy_kind=bug → skipped
        # ------------------------------------------------------------------
        if (
            request.query_type == QueryType.CHANGE_TRACE
            and request.params is not None
            and request.params.get("legacy_kind") == "bug"
        ):
            primary_start = time.perf_counter()
            primary_result = self._primary.query(request)
            latency_primary_ms = (time.perf_counter() - primary_start) * 1000.0
            result = DualReadResult(
                outcome="skipped",
                primary=primary_result,
                secondary=None,
                correlation_id=cid,
                latency_primary_ms=latency_primary_ms,
                latency_secondary_ms=None,
                aphelion_unreachable_reason=None,
            )
            self._record(request.user_id, result.outcome)
            self._log_decision(
                outcome="skipped",
                correlation_id=cid,
                query_type=request.query_type.value,
                winning_source=None,
                policy_version="",
                conflict_event_id=None,
                write_error_observed=False,
            )
            return result

        # ------------------------------------------------------------------
        # Dual dispatch: primary + secondary in parallel
        # ------------------------------------------------------------------
        primary_start = time.perf_counter()
        primary_future: Future[object] = self._executor.submit(self._primary.query, request)
        secondary_future: Future[object] = self._executor.submit(self._secondary.query, request)

        # Always await primary to completion — fail-closed means we must have
        # the canonical result even if secondary times out.
        try:
            primary_result = primary_future.result()
        except Exception:
            # Primary failure propagates — don't swallow it.
            secondary_future.cancel()
            raise

        latency_primary_ms = (time.perf_counter() - primary_start) * 1000.0

        # Wait for secondary up to timeout.
        secondary_timeout_s = self._secondary_timeout_ms / 1000.0
        secondary_start = time.perf_counter()
        done, not_done = wait([secondary_future], timeout=secondary_timeout_s)
        latency_secondary_ms = (time.perf_counter() - secondary_start) * 1000.0

        outcome: DualReadOutcome
        secondary_result = None
        unreachable_reason: str | None = None

        if secondary_future in not_done:
            # Timed out — classify as aphelion_unreachable.
            secondary_future.cancel()
            outcome = "aphelion_unreachable"
            unreachable_reason = "timeout"
        else:
            # Future completed — check for exception.
            exc = secondary_future.exception()
            if exc is None:
                secondary_result = secondary_future.result()
                # Compare hits. Guarded — a malformed secondary result (e.g.
                # future M3b real adapter returning hits=None or missing attrs)
                # would otherwise raise AttributeError/TypeError out of the
                # protected block and propagate to the caller, violating
                # invariant #4. Reclassify as primary_only when comparison
                # blows up.
                try:
                    hits_equal = _hits_equal(primary_result, secondary_result)  # type: ignore[arg-type]
                except (AttributeError, TypeError) as cmp_exc:
                    _safe_log_warning(
                        "secondary_hits_equal_failed",
                        exc_class=type(cmp_exc).__name__,
                        exc_str=str(cmp_exc),
                    )
                    outcome = "primary_only"
                    secondary_result = None
                else:
                    outcome = "match" if hits_equal else "diverge"
            elif isinstance(exc, AphelionUnreachableError):
                outcome = "aphelion_unreachable"
                unreachable_reason = exc.reason
            else:
                # Unexpected exception — logic bug, not infra unavailability.
                # Log only class name + str, NO stack trace (avoids PII in logs).
                _safe_log_warning(
                    "secondary_unexpected_exception",
                    exc_class=type(exc).__name__,
                    exc_str=str(exc),
                )
                outcome = "primary_only"
                secondary_result = None

        # M3b Phase 2 (US-004-M3-T2.1): live cross-store arbitration. We
        # ran the dual dispatch — ``arbitrate`` is pure, no I/O, and
        # correctly resolves ``"fallback"`` when ``secondary_result`` is
        # ``None`` or empty (crosswalk-miss path).  Attach the verdict
        # only on dual-attempt paths; ``"skipped"`` short-circuits above
        # never reach here.
        arbitration = arbitrate(
            primary=primary_result,  # type: ignore[arg-type]
            secondary=secondary_result,  # type: ignore[arg-type]
            query_type=request.query_type,
            correlation_id=cid,
        )

        # M3b Phase 2 (US-005-M3-T2.2): when the arbitration verdict
        # requires manual review (winning_source in {"tie","fallback"}),
        # emit an ``arbitration_conflict`` envelope row to the events
        # table.  Best-effort: ``write_conflict_event`` is fail-closed
        # and returns "" on any exception — observability never crashes
        # the canonical query path.  The wiring is gated on an explicit
        # ``events_conn`` having been passed at construction time so
        # call sites that opt out (or unit tests) skip the write
        # entirely with no overhead.
        write_error_observed = False
        if arbitration.requires_manual_review and self._events_conn is not None:
            try:
                payload_for_writer = {
                    "primary": primary_result,
                    "secondary": secondary_result,
                    "user_id": request.user_id,
                }
                event_id = write_conflict_event(arbitration, payload_for_writer, self._events_conn)
                if event_id:
                    arbitration = dataclasses.replace(arbitration, conflict_event_id=event_id)
                else:
                    # H4 — empty event_id signals the writer caught an
                    # exception. Surface that on the DualReadResult so the
                    # JSONL decision log records the write-error path.
                    write_error_observed = True
            except Exception as exc:  # noqa: BLE001 — fail-closed
                _safe_log_warning(
                    "conflict_event_write_failed",
                    exc_class=type(exc).__name__,
                    exc_str=str(exc),
                )
                write_error_observed = True

        result = DualReadResult(
            outcome=outcome,
            primary=primary_result,  # type: ignore[arg-type]
            secondary=secondary_result,  # type: ignore[arg-type]
            correlation_id=cid,
            latency_primary_ms=latency_primary_ms,
            latency_secondary_ms=latency_secondary_ms,
            aphelion_unreachable_reason=unreachable_reason,
            arbitration=arbitration,
            write_error_observed=write_error_observed,
        )

        self._record(request.user_id, outcome)
        # JSONL-PRODUCER — record dual-read decision for downstream metrics.
        # Pass outcome through verbatim (5-value DualReadOutcome vocabulary:
        # match | diverge | primary_only | aphelion_unreachable | skipped) so
        # discrepancy_rate (counts 'diverge') and write_error_rate (filters
        # by key) see the records they're contracted to count.
        self._log_decision(
            outcome=outcome,
            correlation_id=cid,
            query_type=request.query_type.value,
            winning_source=arbitration.winning_source,
            policy_version=arbitration.policy_version,
            conflict_event_id=arbitration.conflict_event_id,
            write_error_observed=write_error_observed,
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_decision(
        self,
        *,
        outcome: str,
        correlation_id: str,
        query_type: str,
        winning_source: str | None,
        policy_version: str,
        conflict_event_id: str | None,
        write_error_observed: bool,
    ) -> None:
        """Best-effort append to the dual-read decision JSONL log.

        Best-effort: any failure inside the producer is swallowed; the
        canonical query path must never raise from observability code.
        """
        try:
            append_decision(
                {
                    "correlation_id": correlation_id,
                    "query_type": query_type,
                    "outcome": outcome,
                    "winning_source": winning_source,
                    "policy_version": policy_version,
                    "write_error_observed": write_error_observed,
                    "conflict_event_id": conflict_event_id,
                    "data_quality_flag": "normal",
                }
            )
        except Exception as exc:  # noqa: BLE001 — observability never crashes
            _safe_log_warning(
                "decision_log_append_failed",
                exc_class=type(exc).__name__,
                exc_str=str(exc),
            )

    def _record(self, user_id: str, outcome: DualReadOutcome) -> None:
        """Record outcome to live counter + Prometheus gauges (optional).

        Observability code MUST NEVER kill the request. A failing Prometheus
        collector (label-cardinality cap, mid-reset race, internal client
        bug) or a faulty live counter would otherwise propagate out of
        ``query()`` and the caller would lose the canonical primary result
        — that violates fail-closed invariant #1. Guard each side-effect.
        """
        try:
            record_dual_read_outcome(user_id=user_id, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 — observability must not crash query path
            _safe_log_warning(
                "record_dual_read_outcome_failed",
                exc_class=type(exc).__name__,
                exc_str=str(exc),
            )
        if self._live_counter is not None:
            try:
                self._live_counter.record(user_id=user_id, outcome=outcome)
            except Exception as exc:  # noqa: BLE001 — same rationale as above
                _safe_log_warning(
                    "live_counter_record_failed",
                    exc_class=type(exc).__name__,
                    exc_str=str(exc),
                )
        # T1.5: feed the rolling-window circuit breaker. Only count outcomes
        # where the secondary was actually attempted (not "skipped").
        if outcome != "skipped":
            try:
                get_breaker_state().record_unreachable_observation(
                    observed_unreachable=(outcome == "aphelion_unreachable")
                )
            except Exception as exc:  # noqa: BLE001 — same rationale as above
                _safe_log_warning(
                    "breaker_record_failed",
                    exc_class=type(exc).__name__,
                    exc_str=str(exc),
                )
