"""US-009.1 §3.3 + §3.4 — RollbackController tests.

Covers criteria 1.9, 1.14-1.17:
- Trigger independence (1.9)
- Gate downgrade behaviour (1.14)
- 30-min cooldown after trip (1.15)
- No auto re-promote — manual ACK required (1.16)
- ACK records operator + timestamp (1.17)
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from parallax.canary.audit_log import AuditLog
from parallax.canary.rollback import (
    COOLDOWN_SECONDS,
    CanaryState,
    RollbackController,
)
from parallax.canary.triggers import (
    T5_MIN_HITS,
    TriggerVerdict,
)

ENOUGH_HITS: Final[int] = T5_MIN_HITS + 50  # ample sample to deactivate gate


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    log = AuditLog(tmp_path / "audit.db")
    yield log
    log.close()


class _FakeClock:
    """Mutable clock helper so tests can drive the state machine."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _saturate_t5(controller: RollbackController, clock: _FakeClock) -> None:
    """Push enough requests through the controller so T5 gate is inactive."""
    for _ in range(ENOUGH_HITS):
        controller.observe_request(is_error=False, latency_ms=10.0)
    # Re-evaluate so cached gate state reflects the new sample size
    controller.evaluate(now=clock())


# ---------------------------------------------------------------------------
# Initial state + happy path
# ---------------------------------------------------------------------------


def test_initial_state_is_running(audit: AuditLog) -> None:
    c = RollbackController(audit_log=audit, clock=_FakeClock())
    assert c.state is CanaryState.RUNNING


def test_running_with_no_breaches_stays_running(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    _saturate_t5(c, clock)
    snap = c.evaluate()
    assert snap.state is CanaryState.RUNNING


# ---------------------------------------------------------------------------
# T1 trip path (1.10) — pass / breach / hysteresis
# ---------------------------------------------------------------------------


def test_t1_pass_does_not_trip(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    # 49 errors out of 10_000 = 0.49% → PASS
    for i in range(10_000):
        c.observe_request(is_error=i < 49, latency_ms=10.0)
    snap = c.evaluate()
    assert snap.state is CanaryState.RUNNING


def test_t1_breach_trips_controller(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for i in range(10_000):
        c.observe_request(is_error=i < 50, latency_ms=10.0)
    snap = c.evaluate()
    assert snap.state is CanaryState.TRIPPED
    assert snap.tripped_by == "T1-error-rate"


def test_t1_breach_then_30min_cooldown_then_awaiting_ack(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for i in range(10_000):
        c.observe_request(is_error=i < 50, latency_ms=10.0)
    c.evaluate()
    assert c.state is CanaryState.TRIPPED
    # Just before cooldown elapses — still tripped
    clock.advance(COOLDOWN_SECONDS - 1)
    c.evaluate()
    assert c.state is CanaryState.TRIPPED
    # After cooldown elapses → awaiting_ack
    clock.advance(2)
    c.evaluate()
    assert c.state is CanaryState.AWAITING_ACK


# ---------------------------------------------------------------------------
# T2 trip path (1.11)
# ---------------------------------------------------------------------------


def test_t2_pass(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for i in range(10_000):
        c.observe_request(is_error=False, latency_ms=10.0, is_mismatch=i < 49)
    c.evaluate()
    assert c.state is CanaryState.RUNNING


def test_t2_breach(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for i in range(10_000):
        c.observe_request(is_error=False, latency_ms=10.0, is_mismatch=i < 50)
    c.evaluate()
    assert c.state is CanaryState.TRIPPED
    assert c.snapshot().tripped_by == "T2-discrepancy-rate"


def test_t2_hysteresis(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for i in range(10_000):
        c.observe_request(is_error=False, latency_ms=10.0, is_mismatch=i < 50)
    c.evaluate()
    clock.advance(COOLDOWN_SECONDS + 1)
    c.evaluate()
    # Cooldown finished → awaiting_ack, NOT auto-recover
    assert c.state is CanaryState.AWAITING_ACK


# ---------------------------------------------------------------------------
# T3 trip path (1.12)
# ---------------------------------------------------------------------------


def test_t3_pass_below_100ms(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for _ in range(200):
        c.observe_request(is_error=False, latency_ms=99.0)
    c.evaluate()
    assert c.state is CanaryState.RUNNING


def test_t3_breach_at_100ms(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for _ in range(200):
        c.observe_request(is_error=False, latency_ms=100.0)
    c.evaluate()
    assert c.state is CanaryState.TRIPPED
    assert c.snapshot().tripped_by == "T3-p99-latency"


def test_t3_hysteresis(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for _ in range(200):
        c.observe_request(is_error=False, latency_ms=150.0)
    c.evaluate()
    clock.advance(COOLDOWN_SECONDS + 60)
    c.evaluate()
    assert c.state is CanaryState.AWAITING_ACK


# ---------------------------------------------------------------------------
# T4 trip path (1.13) — immediate, no window
# ---------------------------------------------------------------------------


def test_t4_pass_when_no_data_loss(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    _saturate_t5(c, clock)
    assert c.state is CanaryState.RUNNING


def test_t4_immediate_trip(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    _saturate_t5(c, clock)
    c.observe_data_loss(count=1)
    c.evaluate()
    assert c.state is CanaryState.TRIPPED
    assert c.snapshot().tripped_by == "T4-data-loss"


def test_t4_hysteresis(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    _saturate_t5(c, clock)
    c.observe_data_loss(count=1)
    c.evaluate()
    clock.advance(COOLDOWN_SECONDS + 60)
    c.evaluate()
    assert c.state is CanaryState.AWAITING_ACK


# ---------------------------------------------------------------------------
# T5 gate (1.14) — active / boundary / inactive
# ---------------------------------------------------------------------------


def test_t5_gate_active_marks_t1_insufficient(audit: AuditLog) -> None:
    """49 hits + error rate > 0.5% → T1 marked insufficient_data, no trip."""
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    # 49 hits where 1 is an error → error rate ≈ 2% but only 49 hits → gate active
    for i in range(49):
        c.observe_request(is_error=(i == 0), latency_ms=10.0)
    snap = c.evaluate()
    assert snap.state is CanaryState.RUNNING
    t1 = snap.last_evaluations["T1-error-rate"]
    assert t1.verdict is TriggerVerdict.INSUFFICIENT_DATA


def test_t5_gate_inactive_at_50_hits_allows_trip(audit: AuditLog) -> None:
    """At exactly 50 hits the gate is inactive (criterion 1.19 boundary)."""
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    # 50 hits where 50 are errors → 100% error → BREACH (gate inactive)
    for _ in range(50):
        c.observe_request(is_error=True, latency_ms=10.0)
    snap = c.evaluate()
    assert snap.last_gate_evaluation.verdict is TriggerVerdict.PASS
    assert snap.state is CanaryState.TRIPPED


def test_t5_gate_inactive_with_high_volume(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    _saturate_t5(c, clock)
    snap = c.evaluate()
    assert snap.last_gate_evaluation.verdict is TriggerVerdict.PASS


# ---------------------------------------------------------------------------
# Hysteresis / ACK (criteria 1.15, 1.16, 1.17)
# ---------------------------------------------------------------------------


def test_no_auto_recover_during_cooldown(audit: AuditLog) -> None:
    """Even when the metric returns to PASS, state stays TRIPPED for 30 min."""
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for i in range(10_000):
        c.observe_request(is_error=i < 100, latency_ms=10.0)
    c.evaluate()
    assert c.state is CanaryState.TRIPPED
    clock.advance(60)
    c.evaluate()
    assert c.state is CanaryState.TRIPPED, "must NOT auto-recover before cooldown"


def test_acknowledge_only_works_in_awaiting_ack(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    # RUNNING → ACK rejected
    assert c.acknowledge(ack_by="oncall") is False
    # Trip + cooldown → AWAITING_ACK
    for i in range(10_000):
        c.observe_request(is_error=i < 100, latency_ms=10.0)
    c.evaluate()
    clock.advance(COOLDOWN_SECONDS + 60)
    c.evaluate()
    assert c.state is CanaryState.AWAITING_ACK
    assert c.acknowledge(ack_by="oncall") is True
    assert c.state is CanaryState.RUNNING


def test_acknowledge_records_operator_in_audit_log(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for i in range(10_000):
        c.observe_request(is_error=i < 100, latency_ms=10.0)
    c.evaluate()
    clock.advance(COOLDOWN_SECONDS + 60)
    c.evaluate()
    c.acknowledge(ack_by="chris@parallax", ack_at="2026-05-05T00:00:00Z")
    # Find the ACK row — there's exactly one row with ack_by set.
    import sqlite3

    with sqlite3.connect(audit.db_path) as conn:
        rows = conn.execute(
            "SELECT ack_by, ack_at FROM audit_log WHERE ack_by IS NOT NULL"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "chris@parallax"
    assert rows[0][1] == "2026-05-05T00:00:00Z"


def test_acknowledge_rejects_blank_operator(audit: AuditLog) -> None:
    clock = _FakeClock()
    c = RollbackController(audit_log=audit, clock=clock)
    for i in range(10_000):
        c.observe_request(is_error=i < 100, latency_ms=10.0)
    c.evaluate()
    clock.advance(COOLDOWN_SECONDS + 60)
    c.evaluate()
    assert c.acknowledge(ack_by="") is False
    assert c.state is CanaryState.AWAITING_ACK
