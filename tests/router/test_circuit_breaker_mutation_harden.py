"""Mutation-hardening for ``parallax.router.circuit_breaker`` (overnight-20260816 S3).

Additive companion to ``test_circuit_breaker_rolling_window.py``. Every test
here exists because a semantic mutant of the module SURVIVED that suite, and
each pins the specific constant or boundary the mutant moved.

Why these two constants needed a separate file
----------------------------------------------
The existing eviction tests derive their own time arithmetic from the module's
``WINDOW_SECONDS``::

    future_time = base_time + WINDOW_SECONDS + 10.0

That is self-referential: change ``WINDOW_SECONDS`` to any value and the test
advances the clock by that same new value, so it stays green. A 300s window and
a 30000s window are indistinguishable to it. The same shape hides
``MIN_OBSERVATIONS``: the cold-start test records 5 observations, which is below
50 and below any smaller replacement, so the guard's actual height is untested.

The tests below therefore use LITERAL numbers, never the module constants, for
the values under test. That is deliberate and must stay that way — importing the
constant to build the expectation is what made the originals blind.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from parallax.router.circuit_breaker import (
    MIN_OBSERVATIONS,
    TRIP_THRESHOLD,
    WINDOW_SECONDS,
    BreakerState,
    get_breaker_state,
)


@pytest.fixture(autouse=True)
def _clean_singleton():
    """Reset the process-local singleton around each test."""
    get_breaker_state().reset()
    yield
    get_breaker_state().reset()


# ---------------------------------------------------------------------------
# Constants are contract, not implementation detail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_documented_window_and_cold_start_guard_are_the_shipped_values() -> None:
    """The module docstring promises a 5-minute window and a 50-sample guard.

    Both numbers are load-bearing operational contract: the window sets how long
    a burst of unreachability keeps influencing the breaker, and the guard is
    what stops 1-unreachable-out-of-1 from tripping production at cold start.
    Pinned as literals because every behavioural test that derives its
    arithmetic from these constants moves with them.
    """
    assert WINDOW_SECONDS == 300.0
    assert MIN_OBSERVATIONS == 50
    assert TRIP_THRESHOLD == 0.01


# ---------------------------------------------------------------------------
# MIN_OBSERVATIONS boundary (mutant: 50 -> 10 survived)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_forty_nine_unreachable_observations_do_not_trip() -> None:
    """One sample below the guard, at a 100% unreachable rate, must not trip.

    The existing cold-start test uses 5 observations, so any guard height from 6
    upward passes it. 49 is the only number that proves the guard is at 50.
    """
    state = BreakerState()
    for _ in range(49):
        state.record_unreachable_observation(observed_unreachable=True)

    assert state.is_tripped() is False
    assert state.current_unreachable_rate() is None, (
        "fewer than 50 samples is an insufficient sample and must report unknown, "
        "not a rate"
    )
    assert state.observation_count() == 49


@pytest.mark.unit
def test_the_fiftieth_observation_is_the_one_that_arms_the_breaker() -> None:
    """Crossing from 49 to 50 samples is what makes the rate actionable."""
    state = BreakerState()
    for _ in range(49):
        state.record_unreachable_observation(observed_unreachable=True)
    assert state.is_tripped() is False

    state.record_unreachable_observation(observed_unreachable=True)

    assert state.is_tripped() is True
    assert state.current_unreachable_rate() == pytest.approx(1.0)
    assert state.observation_count() == 50


# ---------------------------------------------------------------------------
# WINDOW_SECONDS boundary (mutant: 300 -> 30000 survived)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_observations_just_inside_five_minutes_are_retained() -> None:
    """At +299s the original samples are still inside the 5-minute window.

    Clock advance is a literal 299.0, not ``WINDOW_SECONDS - 1``, so widening
    the window cannot drag the expectation along with it.
    """
    state = BreakerState()
    base = 1000.0
    for _ in range(60):
        with patch("time.monotonic", return_value=base):
            state.record_unreachable_observation(observed_unreachable=False)

    with patch("time.monotonic", return_value=base + 299.0):
        assert state.observation_count() == 60


@pytest.mark.unit
def test_observations_just_outside_five_minutes_are_evicted() -> None:
    """At +301s every original sample has aged out of the 5-minute window.

    A widened window (the surviving 300 -> 30000 mutant) keeps all 60 here, so
    this is the assertion that pins the window to five minutes rather than to
    "whatever the constant says".
    """
    state = BreakerState()
    base = 1000.0
    for _ in range(60):
        with patch("time.monotonic", return_value=base):
            state.record_unreachable_observation(observed_unreachable=False)

    with patch("time.monotonic", return_value=base + 301.0):
        assert state.observation_count() == 0
        assert state.current_unreachable_rate() is None
