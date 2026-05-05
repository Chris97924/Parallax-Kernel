"""US-009.1 §3.3 — trigger primitive tests (criteria 1.10-1.14, 1.19).

Each trigger gets pass / breach / boundary coverage so the controller
can rely on the primitives without coupling to T5's gate semantics.
"""

from __future__ import annotations

import math

import pytest

from parallax.canary.triggers import (
    T1_THRESHOLD,
    T2_THRESHOLD,
    T3_THRESHOLD_MS,
    T5_MIN_HITS,
    WINDOW_T1_SECONDS,
    WINDOW_T2_SECONDS,
    WINDOW_T3_SECONDS,
    WINDOW_T5_SECONDS,
    T1ErrorRateTrigger,
    T2DiscrepancyRateTrigger,
    T3P99LatencyTrigger,
    T4DataLossTrigger,
    T5MinHitsGate,
    TriggerVerdict,
)

# ---------------------------------------------------------------------------
# T1 — error rate (criterion 1.10)
# ---------------------------------------------------------------------------


def _bulk_record_t1(t: T1ErrorRateTrigger, *, errors: int, total: int, base_ts: float) -> None:
    # ms-spaced so all observations fit inside the 5-min sliding window
    for i in range(total):
        t.record(base_ts + i * 0.001, is_error=i < errors)


def test_t1_pass_below_threshold() -> None:
    t = T1ErrorRateTrigger()
    # 49 errors out of 10_000 → 0.49% → PASS
    _bulk_record_t1(t, errors=49, total=10_000, base_ts=0.0)
    ev = t.evaluate(now=15.0)
    assert ev.verdict is TriggerVerdict.PASS
    assert ev.metric_value == pytest.approx(0.0049)


def test_t1_breach_at_threshold() -> None:
    t = T1ErrorRateTrigger()
    # 50 errors / 10_000 = 0.5% (exact threshold) → BREACH per 1.10
    _bulk_record_t1(t, errors=50, total=10_000, base_ts=0.0)
    ev = t.evaluate(now=15.0)
    assert ev.verdict is TriggerVerdict.BREACH


def test_t1_breach_above_threshold() -> None:
    t = T1ErrorRateTrigger()
    _bulk_record_t1(t, errors=51, total=10_000, base_ts=0.0)
    ev = t.evaluate(now=15.0)
    assert ev.verdict is TriggerVerdict.BREACH
    assert ev.metric_value > T1_THRESHOLD


def test_t1_pass_with_no_observations() -> None:
    t = T1ErrorRateTrigger()
    ev = t.evaluate(now=0.0)
    assert ev.verdict is TriggerVerdict.PASS
    assert ev.observations == 0


def test_t1_window_evicts_old_observations() -> None:
    t = T1ErrorRateTrigger()
    # 100 errors at ts=0 (outside window when now > WINDOW_T1_SECONDS)
    for _ in range(100):
        t.record(0.0, is_error=True)
    # 100 successes inside the window (ms-spaced near "now")
    inside_base = WINDOW_T1_SECONDS + 100.0
    for i in range(100):
        t.record(inside_base + i * 0.001, is_error=False)
    ev = t.evaluate(now=inside_base + 1.0)
    assert ev.verdict is TriggerVerdict.PASS
    assert ev.observations == 100


# ---------------------------------------------------------------------------
# T2 — discrepancy rate (criterion 1.11)
# ---------------------------------------------------------------------------


def test_t2_pass_below_threshold() -> None:
    t = T2DiscrepancyRateTrigger()
    for i in range(10_000):
        t.record(i * 0.001, is_mismatch=i < 49)
    ev = t.evaluate(now=15.0)
    assert ev.verdict is TriggerVerdict.PASS
    assert ev.metric_value < T2_THRESHOLD


def test_t2_breach_at_threshold() -> None:
    t = T2DiscrepancyRateTrigger()
    for i in range(10_000):
        t.record(i * 0.001, is_mismatch=i < 50)
    ev = t.evaluate(now=15.0)
    assert ev.verdict is TriggerVerdict.BREACH


def test_t2_uses_3min_window() -> None:
    t = T2DiscrepancyRateTrigger()
    # Old breaches outside the 3-min window
    for _ in range(200):
        t.record(0.0, is_mismatch=True)
    inside_base = WINDOW_T2_SECONDS + 100.0
    for i in range(200):
        t.record(inside_base + i * 0.001, is_mismatch=False)
    ev = t.evaluate(now=inside_base + 1.0)
    assert ev.verdict is TriggerVerdict.PASS


# ---------------------------------------------------------------------------
# T3 — p99 latency (criterion 1.12)
# ---------------------------------------------------------------------------


def test_t3_pass_when_p99_below_threshold() -> None:
    t = T3P99LatencyTrigger()
    # 99 samples at 99 ms, 1 at 99 — all below 100 ms p99 → PASS
    for i in range(100):
        t.record(float(i), latency_ms=99.0)
    ev = t.evaluate(now=200.0)
    assert ev.verdict is TriggerVerdict.PASS
    assert ev.metric_value == pytest.approx(99.0)


def test_t3_breach_when_p99_at_threshold() -> None:
    t = T3P99LatencyTrigger()
    # 99 fast + 1 slow → p99 (nearest-rank, idx=ceil(0.99*100)-1=98) = fast.
    # Push more so p99 lands on the slow sample.
    for i in range(99):
        t.record(float(i), latency_ms=50.0)
    t.record(99.0, latency_ms=101.0)
    # nearest-rank for n=100, p=0.99 → idx = ceil(99) - 1 = 98 → 50.0
    # We need at least 100 slow to hit p99. Use 1 fast + 99 slow:
    t = T3P99LatencyTrigger()
    t.record(0.0, latency_ms=50.0)
    for i in range(99):
        t.record(float(i + 1), latency_ms=101.0)
    ev = t.evaluate(now=200.0)
    assert ev.verdict is TriggerVerdict.BREACH
    assert ev.metric_value >= T3_THRESHOLD_MS


def test_t3_p99_exact_at_100ms_breaches() -> None:
    """Criterion 1.12: p99 ≥ 100 ms trips."""
    t = T3P99LatencyTrigger()
    # All samples at exactly 100 ms → p99 = 100 → BREACH
    for i in range(100):
        t.record(float(i), latency_ms=100.0)
    ev = t.evaluate(now=200.0)
    assert ev.verdict is TriggerVerdict.BREACH
    assert math.isclose(ev.metric_value, 100.0)


def test_t3_pass_with_no_observations() -> None:
    t = T3P99LatencyTrigger()
    ev = t.evaluate(now=0.0)
    assert ev.verdict is TriggerVerdict.PASS
    assert ev.observations == 0


# ---------------------------------------------------------------------------
# T4 — data loss (criterion 1.13)
# ---------------------------------------------------------------------------


def test_t4_pass_when_zero() -> None:
    t = T4DataLossTrigger()
    ev = t.evaluate()
    assert ev.verdict is TriggerVerdict.PASS
    assert ev.metric_value == 0.0


def test_t4_immediate_breach_on_first_event() -> None:
    """Criterion 1.13: any single event trips immediately."""
    t = T4DataLossTrigger()
    t.record()
    ev = t.evaluate()
    assert ev.verdict is TriggerVerdict.BREACH
    assert ev.metric_value == 1.0


def test_t4_rejects_negative_count() -> None:
    t = T4DataLossTrigger()
    with pytest.raises(ValueError):
        t.record(count=-1)


def test_t4_reset_clears_state() -> None:
    t = T4DataLossTrigger()
    t.record(count=5)
    t.reset()
    assert t.evaluate().verdict is TriggerVerdict.PASS


# ---------------------------------------------------------------------------
# T5 — min hits gate (criterion 1.14, 1.19)
# ---------------------------------------------------------------------------


def test_t5_active_when_below_min() -> None:
    g = T5MinHitsGate()
    for i in range(T5_MIN_HITS - 1):
        g.record(float(i))
    assert g.is_active(now=200.0) is True
    ev = g.evaluate(now=200.0)
    assert ev.verdict is TriggerVerdict.INSUFFICIENT_DATA


def test_t5_boundary_at_50_hits() -> None:
    """Criterion 1.19: hits=50 boundary — gate inactive (statistical sufficiency)."""
    g = T5MinHitsGate()
    for i in range(T5_MIN_HITS):
        g.record(float(i))
    assert g.is_active(now=200.0) is False
    ev = g.evaluate(now=200.0)
    assert ev.verdict is TriggerVerdict.PASS


def test_t5_inactive_above_min() -> None:
    g = T5MinHitsGate()
    for i in range(500):
        g.record(i * 0.001)
    assert g.is_active(now=10.0) is False


def test_t5_window_evicts_old_hits() -> None:
    g = T5MinHitsGate()
    # 100 hits at ts=0 (will fall outside the 5-min window when we evaluate)
    for _ in range(100):
        g.record(0.0)
    # 10 recent hits placed INSIDE the window relative to `now` so they
    # survive eviction. Earlier version of this test placed them at
    # WINDOW + 1 + i and evaluated at WINDOW + 1000, which evicted BOTH
    # batches and still returned active=True for the wrong reason.
    inside_base = WINDOW_T5_SECONDS + 100.0
    for i in range(10):
        g.record(inside_base + i * 0.001)
    now = inside_base + 1.0
    # Old batch is evicted (now - WINDOW > 0); new batch survives.
    assert g.hits(now=now) == 10, "only the recent 10 hits must survive eviction"
    assert g.is_active(now=now) is True, "10 < 50 → gate active on recent batch"


# ---------------------------------------------------------------------------
# Independence (criterion 1.9) — T1-T4 don't share state
# ---------------------------------------------------------------------------


def test_triggers_are_independent() -> None:
    """Criterion 1.9 — recording on one trigger MUST NOT change another's metric.

    Earlier version of this test only checked that each ``evaluate()``
    returned a ``TriggerEvaluation`` instance, which would have passed
    even if the four triggers shared a counter. This version asserts
    that observation counts and metric values stay isolated.
    """
    t1 = T1ErrorRateTrigger()
    t2 = T2DiscrepancyRateTrigger()
    t3 = T3P99LatencyTrigger()
    t4 = T4DataLossTrigger()

    # Step 1: record a BREACH-level error rate on T1 only.
    for i in range(200):
        t1.record(i * 0.001, is_error=i < 5)  # 5/200 = 2.5% > 0.5% → BREACH
    ev_t1 = t1.evaluate(now=1.0)
    ev_t2 = t2.evaluate(now=1.0)
    ev_t3 = t3.evaluate(now=1.0)
    ev_t4 = t4.evaluate()
    assert ev_t1.verdict is TriggerVerdict.BREACH
    assert ev_t1.observations == 200
    assert ev_t2.observations == 0, "T2 must not see T1 observations"
    assert ev_t3.observations == 0, "T3 must not see T1 observations"
    assert ev_t4.metric_value == 0.0, "T4 must not see T1 observations"

    # Step 2: record a data-loss event on T4 only — must not bleed back.
    t4.record()
    ev_t1 = t1.evaluate(now=1.0)
    ev_t2 = t2.evaluate(now=1.0)
    ev_t3 = t3.evaluate(now=1.0)
    ev_t4 = t4.evaluate()
    assert ev_t4.verdict is TriggerVerdict.BREACH
    assert ev_t1.observations == 200, "T1 must not lose observations from T4 record"
    assert ev_t2.observations == 0
    assert ev_t3.observations == 0


# ---------------------------------------------------------------------------
# Window constants are in spec table (defensive)
# ---------------------------------------------------------------------------


def test_window_constants_match_spec() -> None:
    assert WINDOW_T1_SECONDS == 300.0  # 5 min
    assert WINDOW_T2_SECONDS == 180.0  # 3 min
    assert WINDOW_T3_SECONDS == 300.0  # 5 min
    assert WINDOW_T5_SECONDS == 300.0  # 5 min
    assert T1_THRESHOLD == 0.005
    assert T2_THRESHOLD == 0.005
    assert T3_THRESHOLD_MS == 100.0
    assert T5_MIN_HITS == 50
