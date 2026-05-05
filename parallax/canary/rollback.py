"""US-009.1 §3.3 + §3.4 — RollbackController state machine.

Wires the five trigger primitives from :mod:`parallax.canary.triggers`
into a single controller that observes the ``running`` /
``tripped`` / ``awaiting_ack`` state machine and enforces the 30 min
hysteresis cooldown plus manual ACK requirement (criteria 1.15–1.17).

State transitions
-----------------

::

    running ──(any T1-T4 BREACH)──▶ tripped
       ▲                              │
       │                              │ now > tripped_at + 30 min
       │                              ▼
       │                         awaiting_ack
       │                              │
       └────────(manual ACK)──────────┘

Per criteria 1.15–1.16: the controller MUST NOT auto-recover; the only
path back to ``running`` is via :meth:`acknowledge` after the cooldown
window has elapsed. Operator identity is recorded in the audit log
(criterion 1.17).

Per criterion 1.14: when ``T5MinHitsGate.is_active`` is true, the
T1-T4 trigger verdicts are downgraded to ``INSUFFICIENT_DATA`` and the
controller does NOT trip.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import threading
import time
from collections.abc import Mapping
from typing import Final

from parallax.canary.audit_log import AuditLog
from parallax.canary.event_id import uuid7
from parallax.canary.triggers import (
    T1ErrorRateTrigger,
    T2DiscrepancyRateTrigger,
    T3P99LatencyTrigger,
    T4DataLossTrigger,
    T5MinHitsGate,
    TriggerEvaluation,
    TriggerVerdict,
)

__all__ = [
    "CanaryState",
    "RollbackController",
    "RollbackSnapshot",
    "COOLDOWN_SECONDS",
]

_log = logging.getLogger(__name__)

COOLDOWN_SECONDS: Final[float] = 30 * 60.0  # 30 minutes (criterion 1.15)


class CanaryState(enum.Enum):
    """Lifecycle states of the canary controller."""

    RUNNING = "running"
    TRIPPED = "tripped"
    AWAITING_ACK = "awaiting_ack"


@dataclasses.dataclass(frozen=True)
class RollbackSnapshot:
    """Read-only snapshot of the controller state.

    Surfaced via :meth:`RollbackController.snapshot` so observers (CLI,
    dashboard, tests) can inspect the controller without touching its
    internal mutex.
    """

    state: CanaryState
    tripped_at: float | None
    tripped_by: str | None
    cooldown_seconds: float
    last_evaluations: Mapping[str, TriggerEvaluation]
    last_gate_evaluation: TriggerEvaluation
    last_ack_by: str | None
    last_ack_at: str | None


class RollbackController:
    """Drives the canary lifecycle in response to trigger evaluations.

    The controller is *thread-safe*: a single mutex guards both the
    state machine and trigger observation buffers so concurrent
    observers never see torn state. Trigger record() calls are cheap
    (deque append + lock acquire); a single mutex keeps the API simple.
    """

    def __init__(
        self,
        *,
        audit_log: AuditLog | None = None,
        clock: callable = time.monotonic,  # type: ignore[type-arg]
        cooldown_seconds: float = COOLDOWN_SECONDS,
    ) -> None:
        self._audit = audit_log
        self._clock = clock
        self._cooldown = cooldown_seconds

        self._t1 = T1ErrorRateTrigger()
        self._t2 = T2DiscrepancyRateTrigger()
        self._t3 = T3P99LatencyTrigger()
        self._t4 = T4DataLossTrigger()
        self._t5 = T5MinHitsGate()

        self._lock = threading.Lock()
        self._state: CanaryState = CanaryState.RUNNING
        self._tripped_at: float | None = None
        self._tripped_by: str | None = None
        self._last_evaluations: dict[str, TriggerEvaluation] = {}
        self._last_gate_evaluation: TriggerEvaluation = self._t5.evaluate(self._clock())
        self._last_ack_by: str | None = None
        self._last_ack_at: str | None = None

    # ------------------------------------------------------------------
    # Read-only inspection
    # ------------------------------------------------------------------
    @property
    def state(self) -> CanaryState:
        with self._lock:
            return self._state

    def snapshot(self) -> RollbackSnapshot:
        with self._lock:
            return RollbackSnapshot(
                state=self._state,
                tripped_at=self._tripped_at,
                tripped_by=self._tripped_by,
                cooldown_seconds=self._cooldown,
                last_evaluations=dict(self._last_evaluations),
                last_gate_evaluation=self._last_gate_evaluation,
                last_ack_by=self._last_ack_by,
                last_ack_at=self._last_ack_at,
            )

    # ------------------------------------------------------------------
    # Observation API — host calls these as canary requests flow through
    # ------------------------------------------------------------------
    def observe_request(
        self,
        *,
        is_error: bool,
        latency_ms: float,
        is_mismatch: bool = False,
        ts: float | None = None,
    ) -> None:
        """Record a single canary request. Updates T1, T2, T3, T5."""
        now = self._clock() if ts is None else ts
        with self._lock:
            self._t1.record(now, is_error=is_error)
            self._t2.record(now, is_mismatch=is_mismatch)
            self._t3.record(now, latency_ms=latency_ms)
            self._t5.record(now)

    def observe_data_loss(self, count: int = 1) -> None:
        """Record a data-loss event. Counted in T4 only (cumulative)."""
        with self._lock:
            self._t4.record(count)

    # ------------------------------------------------------------------
    # State machine — drive it via :meth:`evaluate`
    # ------------------------------------------------------------------
    def evaluate(self, *, now: float | None = None) -> RollbackSnapshot:
        """Evaluate all triggers and update the state machine.

        Call from a periodic scheduler (e.g. every second). Idempotent
        — calling repeatedly without new observations is a no-op for
        ``running`` and ``awaiting_ack``; ``tripped`` may transition
        to ``awaiting_ack`` once the cooldown elapses.
        """
        ts = self._clock() if now is None else now
        with self._lock:
            gate = self._t5.evaluate(ts)
            gate_active = gate.verdict is TriggerVerdict.INSUFFICIENT_DATA
            evaluations = self._evaluate_triggers(ts, gate_active)
            self._last_evaluations = {ev.trigger_id: ev for ev in evaluations}
            self._last_gate_evaluation = gate

            if self._state is CanaryState.RUNNING:
                breach = self._first_breach(evaluations)
                if breach is not None:
                    self._trip(ts, breach)
            elif self._state is CanaryState.TRIPPED:
                if self._tripped_at is not None and ts - self._tripped_at >= self._cooldown:
                    self._state = CanaryState.AWAITING_ACK
            # AWAITING_ACK only exits via :meth:`acknowledge`.

            return RollbackSnapshot(
                state=self._state,
                tripped_at=self._tripped_at,
                tripped_by=self._tripped_by,
                cooldown_seconds=self._cooldown,
                last_evaluations=dict(self._last_evaluations),
                last_gate_evaluation=gate,
                last_ack_by=self._last_ack_by,
                last_ack_at=self._last_ack_at,
            )

    def _evaluate_triggers(self, ts: float, gate_active: bool) -> list[TriggerEvaluation]:
        # Per spec §3.3 table + criterion 1.14: when T5 gate is active
        # ALL T1-T4 verdicts get downgraded to INSUFFICIENT_DATA. We
        # apply the rule uniformly — including T4 — to match the spec
        # table verbatim.
        results: list[TriggerEvaluation] = []
        for trig in (self._t1, self._t2, self._t3, self._t4):
            ev = trig.evaluate(ts)
            if gate_active:
                ev = dataclasses.replace(ev, verdict=TriggerVerdict.INSUFFICIENT_DATA)
            results.append(ev)
        return results

    def _first_breach(self, evaluations: list[TriggerEvaluation]) -> TriggerEvaluation | None:
        for ev in evaluations:
            if ev.verdict is TriggerVerdict.BREACH:
                return ev
        return None

    def _trip(self, ts: float, breach: TriggerEvaluation) -> None:
        self._state = CanaryState.TRIPPED
        self._tripped_at = ts
        self._tripped_by = breach.trigger_id
        _log.warning(
            "canary.rollback.tripped",
            extra={
                "event": "canary.rollback.tripped",
                "trigger_id": breach.trigger_id,
                "metric_value": breach.metric_value,
                "threshold": breach.threshold,
            },
        )

    # ------------------------------------------------------------------
    # Manual ACK — criterion 1.16 + 1.17
    # ------------------------------------------------------------------
    def acknowledge(self, *, ack_by: str, ack_at: str | None = None) -> bool:
        """Manually clear an ``awaiting_ack`` state and return to running.

        Criterion 1.16: cooldown elapsed → ``awaiting_ack`` (NOT auto
        re-promote). The caller MUST exec this method explicitly.

        Criterion 1.17: identity + timestamp recorded in audit log.

        Returns True on transition; False when the controller is not in
        ``awaiting_ack`` or ``ack_by`` is empty.
        """
        if not ack_by:
            return False
        with self._lock:
            if self._state is not CanaryState.AWAITING_ACK:
                return False
            self._state = CanaryState.RUNNING
            self._tripped_at = None
            self._tripped_by = None
            self._last_ack_by = ack_by
            self._last_ack_at = ack_at
            for trig in (self._t1, self._t2, self._t3, self._t4):
                trig.reset()
            # T5 is intentionally NOT reset — sample window survives
            # the rollback so the gate continues to gate.
        if self._audit is not None:
            event_id = str(uuid7())
            self._audit.record_ack(event_id, ack_by=ack_by, ack_at=ack_at)
        return True

    # ------------------------------------------------------------------
    # Test helpers — explicitly named so production callers don't reach
    # ------------------------------------------------------------------
    def force_running(self) -> None:
        """Reset state to ``RUNNING`` (TEST ONLY — bypasses ACK)."""
        with self._lock:
            self._state = CanaryState.RUNNING
            self._tripped_at = None
            self._tripped_by = None
