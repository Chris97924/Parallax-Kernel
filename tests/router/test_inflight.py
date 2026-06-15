"""M3-T1.4 — Unit tests for parallax.router.inflight (US-011).

Covers:
- Normal enter/exit increments then decrements gauge.
- Exception path: exit still decrements (proves try/finally semantics).
- Concurrent safety: 50 threads × 100 cycles reach zero.
- get_inflight_count() reflects manual inc/dec.
- Re-importing the module doesn't raise ValueError (collision-safe).
"""

from __future__ import annotations

import threading

import pytest

from parallax.router.inflight import (
    InflightTracker,
    get_inflight_count,
    inflight_gauge,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current() -> int:
    return get_inflight_count()


def _reset_to_zero() -> None:
    """Force gauge back to 0 between tests."""
    current = _current()
    if current > 0:
        for _ in range(int(current)):
            inflight_gauge.dec()
    elif current < 0:
        for _ in range(int(-current)):
            inflight_gauge.inc()


@pytest.fixture(autouse=True)
def _clean_gauge():
    _reset_to_zero()
    yield
    _reset_to_zero()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tracker_increments_and_decrements_on_normal_path():
    assert _current() == 0
    with InflightTracker():
        assert _current() == 1
    assert _current() == 0


@pytest.mark.unit
def test_tracker_decrements_on_exception_path():
    """CRITICAL: proves try/finally semantics — gauge decrements even on exception."""
    assert _current() == 0
    try:
        with InflightTracker():
            assert _current() == 1
            raise RuntimeError("simulated handler failure")
    except RuntimeError:
        pass
    # Must be 0 — not 1 (leaked gauge)
    assert _current() == 0


@pytest.mark.unit
def test_tracker_concurrent_safety():
    """50 threads × 100 enter/exit cycles → final count == 0."""
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(100):
                with InflightTracker():
                    pass
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker errors: {errors}"
    assert _current() == 0


@pytest.mark.unit
def test_get_inflight_count_returns_current_value():
    assert _current() == 0
    inflight_gauge.inc()
    assert _current() == 1
    inflight_gauge.inc()
    assert _current() == 2
    inflight_gauge.dec()
    assert _current() == 1
    inflight_gauge.dec()
    assert _current() == 0


@pytest.mark.unit
def test_gauge_collector_collision_safe():
    """Re-importing the module does not raise ValueError; same gauge returned."""
    import importlib

    import parallax.router.inflight as mod1

    # Force a re-import of the module.
    importlib.reload(mod1)

    import parallax.router.inflight as mod2  # noqa: PLC0415

    # Both should refer to Gauge objects; no ValueError during reload.
    assert mod1.inflight_gauge is not None
    assert mod2.inflight_gauge is not None
    # Both should report the same name.
    assert mod1.inflight_gauge._name == "parallax_inflight_requests"  # type: ignore[attr-defined]
    assert mod2.inflight_gauge._name == "parallax_inflight_requests"  # type: ignore[attr-defined]
