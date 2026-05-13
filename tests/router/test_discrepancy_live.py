"""M3-T1.1 tests — Live discrepancy counter (US-011).

Coverage target: ≥80% of parallax.router.discrepancy_live.

Test categories:
  Unit (13 cases):
    1-7   LiveDiscrepancyCounter direct API
    8     Thread safety
    9-10  Prometheus counter / gauge integration
    11    Threshold drift guard
    12    M2 DISCREPANCY_RATE_THRESHOLD drift guard
  Integration (1 case):
    13    Parity with M2 offline logic
"""

from __future__ import annotations

import threading
import time
from typing import cast

import prometheus_client
import pytest

from parallax.router.discrepancy_live import (
    APHELION_UNREACHABLE_RATE_THRESHOLD,
    DUAL_READ_DISCREPANCY_RATE_THRESHOLD,
    DualReadOutcome,
    LiveDiscrepancyCounter,
    dual_read_discrepancy_rate,
    record_dual_read_outcome,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_counter(window_seconds: float = 3600.0) -> LiveDiscrepancyCounter:
    """Return a new counter with no recorded outcomes."""
    c = LiveDiscrepancyCounter(window_seconds=window_seconds)
    return c


def _record_many(
    counter: LiveDiscrepancyCounter,
    *,
    user_id: str,
    outcomes: list[DualReadOutcome],
) -> None:
    for o in outcomes:
        counter.record(user_id=user_id, outcome=o)


# ---------------------------------------------------------------------------
# Unit tests — LiveDiscrepancyCounter
# ---------------------------------------------------------------------------


def test_record_match_no_discrepancy() -> None:
    """N match outcomes → discrepancy_rate == 0.0."""
    c = _fresh_counter()
    _record_many(c, user_id="alice", outcomes=cast(list[DualReadOutcome], ["match"] * 50))
    assert c.discrepancy_rate(user_id="alice") == 0.0


def test_record_diverge_increments_rate() -> None:
    """1 diverge out of 100 → discrepancy_rate ≈ 0.01."""
    c = _fresh_counter()
    outcomes: list[DualReadOutcome] = ["diverge"] + ["match"] * 99
    _record_many(c, user_id="alice", outcomes=outcomes)
    rate = c.discrepancy_rate(user_id="alice")
    assert abs(rate - 0.01) < 1e-9


def test_aphelion_unreachable_excluded_from_denominator() -> None:
    """50 match + 50 aphelion_unreachable → discrepancy_rate == 0.0.

    aphelion_unreachable is excluded from the denominator (mirrors M2's
    exclusion of shadow_only per ralplan §6 line 429).
    """
    c = _fresh_counter()
    outcomes: list[DualReadOutcome] = cast(list[DualReadOutcome], ["match"] * 50) + cast(
        list[DualReadOutcome], ["aphelion_unreachable"] * 50
    )
    _record_many(c, user_id="alice", outcomes=outcomes)
    assert c.discrepancy_rate(user_id="alice") == 0.0


def test_aphelion_unreachable_rate_isolated() -> None:
    """50 match + 50 aphelion_unreachable → aphelion_unreachable_rate == 0.5."""
    c = _fresh_counter()
    outcomes: list[DualReadOutcome] = cast(list[DualReadOutcome], ["match"] * 50) + cast(
        list[DualReadOutcome], ["aphelion_unreachable"] * 50
    )
    _record_many(c, user_id="alice", outcomes=outcomes)
    assert c.aphelion_unreachable_rate(user_id="alice") == 0.5


def test_window_eviction() -> None:
    """Records older than window_seconds are evicted; only newer ones count."""
    c = _fresh_counter(window_seconds=0.1)
    _record_many(c, user_id="alice", outcomes=cast(list[DualReadOutcome], ["diverge"] * 10))
    time.sleep(0.2)
    # Add one fresh match — the 10 diverges should be evicted
    c.record(user_id="alice", outcome="match")
    assert c.discrepancy_rate(user_id="alice") == 0.0
    # The fresh match is the only record; unreachable rate = 0
    assert c.aphelion_unreachable_rate(user_id="alice") == 0.0


def test_user_isolation() -> None:
    """Counters for different users are independent."""
    c = _fresh_counter()
    # Alice: 10 diverge → rate = 1.0
    _record_many(c, user_id="alice", outcomes=cast(list[DualReadOutcome], ["diverge"] * 10))
    # Bob: 10 match → rate = 0.0
    _record_many(c, user_id="bob", outcomes=cast(list[DualReadOutcome], ["match"] * 10))
    assert c.discrepancy_rate(user_id="alice") == 1.0
    assert c.discrepancy_rate(user_id="bob") == 0.0


def test_empty_window_returns_zero() -> None:
    """Fresh counter → both rates return 0.0."""
    c = _fresh_counter()
    assert c.discrepancy_rate(user_id="newuser") == 0.0
    assert c.aphelion_unreachable_rate(user_id="newuser") == 0.0


def test_reset_clears_all_users() -> None:
    """reset() removes all per-user deques."""
    c = _fresh_counter()
    _record_many(c, user_id="alice", outcomes=cast(list[DualReadOutcome], ["diverge"] * 5))
    _record_many(c, user_id="bob", outcomes=cast(list[DualReadOutcome], ["match"] * 5))
    c.reset()
    assert c.discrepancy_rate(user_id="alice") == 0.0
    assert c.discrepancy_rate(user_id="bob") == 0.0


def test_thread_safety() -> None:
    """100 threads × 100 records = 10 000 total entries, no race conditions."""
    c = _fresh_counter()
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            for _ in range(100):
                c.record(user_id="shared", outcome="match")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    with c._lock:
        total = len(c._data.get(("shared", "natural"), []))
    assert total == 10_000


# ---------------------------------------------------------------------------
# Unit tests — Prometheus integration
# ---------------------------------------------------------------------------


def _scrape_counter_value(metric_name: str, labels: dict[str, str]) -> float:
    """Read a labeled counter value directly from REGISTRY."""
    for metric in prometheus_client.REGISTRY.collect():
        if metric.name in (metric_name, metric_name + "_total"):
            for sample in metric.samples:
                if sample.name.endswith("_total") and sample.labels == labels:
                    return sample.value
    return 0.0


def _scrape_gauge_value(metric_name: str, labels: dict[str, str]) -> float:
    """Read a labeled gauge value directly from REGISTRY."""
    for metric in prometheus_client.REGISTRY.collect():
        if metric.name == metric_name:
            for sample in metric.samples:
                if sample.labels == labels:
                    return sample.value
    return 0.0


def test_prometheus_counter_increments() -> None:
    """record_dual_read_outcome N times → REGISTRY counter == N."""
    # Use a unique user_id per test invocation to avoid cross-test pollution
    uid = "prom_counter_test_user"
    n = 7
    # Snapshot before to handle cumulative prometheus state
    before = _scrape_counter_value(
        "parallax_dual_read_outcomes",
        {"outcome": "diverge", "user_id": uid, "traffic_source": "natural"},
    )
    for _ in range(n):
        record_dual_read_outcome(user_id=uid, outcome="diverge")
    after = _scrape_counter_value(
        "parallax_dual_read_outcomes",
        {"outcome": "diverge", "user_id": uid, "traffic_source": "natural"},
    )
    assert (after - before) == n


def test_prometheus_counter_carries_traffic_source_label() -> None:
    """M4 burn-in metrics must split synthetic vs natural traffic."""
    uid = "prom_traffic_source_test_user"
    before = _scrape_counter_value(
        "parallax_dual_read_outcomes",
        {"outcome": "match", "user_id": uid, "traffic_source": "synthetic"},
    )

    record_dual_read_outcome(user_id=uid, outcome="match", traffic_source="synthetic")

    after = _scrape_counter_value(
        "parallax_dual_read_outcomes",
        {"outcome": "match", "user_id": uid, "traffic_source": "synthetic"},
    )
    assert (after - before) == 1


def test_prometheus_gauge_reflects_rate() -> None:
    """After recording, gauge value matches dual_read_discrepancy_rate()."""
    uid = "prom_gauge_test_user"
    record_dual_read_outcome(user_id=uid, outcome="diverge")
    record_dual_read_outcome(user_id=uid, outcome="match")
    record_dual_read_outcome(user_id=uid, outcome="match")

    expected = dual_read_discrepancy_rate(user_id=uid)
    gauge_val = _scrape_gauge_value(
        "parallax_dual_read_discrepancy_rate",
        {"user_id": uid, "traffic_source": "natural"},
    )
    assert abs(gauge_val - expected) < 1e-9


# ---------------------------------------------------------------------------
# Unit tests — Threshold drift guards
# ---------------------------------------------------------------------------


def test_thresholds_match_ralplan() -> None:
    """M3 thresholds pinned to ralplan §6 values (drift guard)."""
    assert DUAL_READ_DISCREPANCY_RATE_THRESHOLD == 0.001
    assert APHELION_UNREACHABLE_RATE_THRESHOLD == 0.005


def test_does_not_modify_m2_threshold() -> None:
    """M2 DISCREPANCY_RATE_THRESHOLD == 0.003 must NOT be changed by M3."""
    from parallax.shadow.discrepancy import DISCREPANCY_RATE_THRESHOLD  # noqa: PLC0415

    assert DISCREPANCY_RATE_THRESHOLD == 0.003


# ---------------------------------------------------------------------------
# Integration test — parity with M2 offline logic
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_parity_with_m2_offline_logic() -> None:
    """Live counter must agree with M2 offline discrepancy_rate numerics.

    We feed the live counter a sequence of outcomes and independently compute
    what M2's offline formula would give for the same stream:
        diverge_rate = diverge / (match + diverge)   [excluding aphelion_unreachable]
        unreachable_rate = aphelion_unreachable / total

    Parity constraint (ralplan §6 + task spec):
    - For the same input stream, M3 live ``dual_read_discrepancy_rate`` must
      equal ``diverge / (match + diverge)`` within float tolerance.
    - M3 live ``aphelion_unreachable_rate`` must equal
      ``aphelion_unreachable / total``.

    Note: M2 offline groups aphelion-equivalent outcomes differently
    (shadow_only ≠ aphelion_unreachable), so we do NOT call the M2 function
    directly. Instead we verify against the offline formula applied to the
    same stream, which is the spec's "parity" intent.
    """
    uid = "parity_test_user"
    c = _fresh_counter()

    # Fixture: 70 match, 20 diverge, 10 aphelion_unreachable
    stream: list[DualReadOutcome] = (
        cast(list[DualReadOutcome], ["match"] * 70)
        + cast(list[DualReadOutcome], ["diverge"] * 20)
        + cast(list[DualReadOutcome], ["aphelion_unreachable"] * 10)
    )
    _record_many(c, user_id=uid, outcomes=stream)

    # M2 offline formula applied to this stream:
    #   denominator = match + diverge = 70 + 20 = 90 (excluding aphelion_unreachable)
    #   diverge_rate = 20 / 90 ≈ 0.2222...
    expected_diverge_rate = 20 / 90
    #   unreachable_rate = 10 / 100 = 0.1
    expected_unreachable_rate = 10 / 100

    assert abs(c.discrepancy_rate(user_id=uid) - expected_diverge_rate) < 1e-9
    assert abs(c.aphelion_unreachable_rate(user_id=uid) - expected_unreachable_rate) < 1e-9
