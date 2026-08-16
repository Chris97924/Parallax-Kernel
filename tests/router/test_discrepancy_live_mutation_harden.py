"""Mutation-hardening tests for ``parallax.router.discrepancy_live`` (S7).

Every test below was written against a specific *surviving* mutant of the
live discrepancy counter. Each docstring names the mutant it kills.

Two themes run through the set. The rolling window's trim loop is only
ever exercised by the existing suite in the one shape where every entry
falls out at once, which hides both the eviction end and the boundary
comparison. And ``traffic_source`` — the label that separates the M4
synthetic burn-in loader from production traffic — is threaded through
this module in four places, none of which the existing tests read back
under a non-default value.
"""

from __future__ import annotations

import prometheus_client
import pytest

from parallax.router import discrepancy_live as dl
from parallax.router.discrepancy_live import (
    LiveDiscrepancyCounter,
    _normalize_traffic_source,
    record_dual_read_outcome,
)


class _FakeClock:
    """Stand-in for the ``time`` module: only ``monotonic`` is used here.

    Patched in as ``discrepancy_live.time`` so the window arithmetic is
    exact rather than sleep-timed — the boundary cases below are one
    floating-point comparison wide and cannot be reached with real sleeps.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


def _outcomes_total(user_id: str, outcome: str, traffic_source: str) -> float:
    value = prometheus_client.REGISTRY.get_sample_value(
        "parallax_dual_read_outcomes_total",
        {"outcome": outcome, "user_id": user_id, "traffic_source": traffic_source},
    )
    return 0.0 if value is None else value


def _aphelion_total(user_id: str, traffic_source: str) -> float:
    value = prometheus_client.REGISTRY.get_sample_value(
        "parallax_aphelion_total",
        {"user_id": user_id, "traffic_source": traffic_source},
    )
    return 0.0 if value is None else value


# ---------------------------------------------------------------------------
# The rolling window's trim loop
# ---------------------------------------------------------------------------


class TestWindowTrim:
    def test_trim_evicts_the_oldest_entry_not_the_newest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: ``dq.popleft()`` -> ``dq.pop()``.

        The deque is ordered oldest-first, so popping from the right evicts
        the entry that was just appended while the expired one at the front
        stays — which means the loop condition never goes false and the
        whole window is wiped on any eviction. The existing eviction test
        cannot see it: there, *every* entry is expired, so "keep the recent
        ones" and "throw everything away" produce the same rate of 0.0.
        Here exactly one entry is expired and the survivor is the one whose
        outcome the rate depends on.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        counter = LiveDiscrepancyCounter(window_seconds=100.0)

        counter.record(user_id="u", outcome="match")  # t=0 — expires
        clock.now = 1000.0
        counter.record(user_id="u", outcome="diverge")  # t=1000 — must survive

        assert counter.discrepancy_rate(user_id="u") == 1.0, (
            "the surviving in-window entry was evicted along with the "
            "expired one; the trim is popping the wrong end"
        )

    def test_entry_exactly_at_the_cutoff_is_retained(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MUTANT: the trim comparison ``dq[0][0] < cutoff`` -> ``<= cutoff``.

        An entry exactly ``window_seconds`` old sits on the boundary, and
        the window is the closed interval the parameter names: strictly
        older is what expires. Flipping to ``<=`` shortens every window by
        one instant, which no rate assertion built on sleeps can detect —
        this is only reachable with an exact clock.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        counter = LiveDiscrepancyCounter(window_seconds=100.0)

        counter.record(user_id="u", outcome="diverge")  # t=0
        clock.now = 100.0  # cutoff is now exactly 0.0
        counter.record(user_id="u", outcome="match")

        assert counter.discrepancy_rate(user_id="u") == 0.5, (
            "the entry sitting exactly on the cutoff was evicted"
        )

    def test_default_window_is_an_hour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MUTANT: the ``window_seconds`` default shrunk from 3600.0 to 60.0.

        The default is what the module-level singleton runs with — nothing
        constructs it with an explicit window — so it is the window every
        production reader of ``dual_read_discrepancy_rate`` actually gets.
        Every existing test either passes its own window or records and
        reads back within microseconds, so a shorter default stays green
        while quietly making the live rate a one-minute rate labelled as an
        hour's.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        counter = LiveDiscrepancyCounter()  # default window is the subject

        counter.record(user_id="u", outcome="diverge")  # t=0
        clock.now = 600.0  # ten minutes later — still well inside an hour
        counter.record(user_id="u", outcome="match")

        assert counter.discrepancy_rate(user_id="u") == 0.5, (
            "a ten-minute-old entry fell out of the default window"
        )


# ---------------------------------------------------------------------------
# traffic_source normalisation
# ---------------------------------------------------------------------------


class TestTrafficSourceNormalisation:
    @pytest.mark.parametrize(
        "raw", ["synthetic", "SYNTHETIC", "Synthetic", " synthetic ", "\tsynthetic\n"]
    )
    def test_synthetic_survives_case_and_whitespace(self, raw: str) -> None:
        """MUTANTS: ``value.strip().lower()`` losing either half.

        Both halves fail in the same direction and it is the dangerous one:
        an unrecognised spelling falls through to ``natural``, so burn-in
        traffic gets counted as production rather than being rejected
        loudly. That is precisely the mislabelling that pinned
        ``ArbitrationConflictRateHigh`` at 1.0. Every existing caller
        passes the exact lowercase literal.
        """
        assert _normalize_traffic_source(raw) == "synthetic"

    @pytest.mark.parametrize("raw", [None, "natural", "NATURAL", " natural ", "", "   ", "bogus"])
    def test_everything_else_resolves_to_natural(self, raw: str | None) -> None:
        """The fail-safe half: unknown or absent labels must read as real traffic."""
        assert _normalize_traffic_source(raw) == "natural"

    def test_uppercase_label_lands_on_the_synthetic_series(self) -> None:
        """The same normalisation, read back off the Prometheus label.

        Pins that the counter is partitioned by the *normalised* value, not
        by whatever string the caller happened to pass.
        """
        uid = "s7-normalise-label"
        before = _outcomes_total(uid, "diverge", "synthetic")

        record_dual_read_outcome(user_id=uid, outcome="diverge", traffic_source="  SYNTHETIC  ")

        assert _outcomes_total(uid, "diverge", "synthetic") - before == 1


class TestPerSourceWindows:
    def test_record_keeps_synthetic_and_natural_windows_separate(self) -> None:
        """MUTANT: ``record`` pins ``source`` to the default instead of
        normalising the argument.

        The per-``(user_id, traffic_source)`` key is what keeps the M4
        synthetic burn-in window independent of the natural one — the
        comment on ``_data`` says so. Dropping the argument routes every
        synthetic outcome into the natural window: the synthetic rate then
        reads 0.0 no matter how badly the burn-in loader is diverging, and
        the natural rate carries the loader's divergences as if they were
        production's. The existing counter tests never pass a
        ``traffic_source``, so both halves of that swap are invisible.
        """
        counter = LiveDiscrepancyCounter()

        counter.record(user_id="u", outcome="diverge", traffic_source="synthetic")

        assert counter.discrepancy_rate(user_id="u", traffic_source="synthetic") == 1.0, (
            "a synthetic outcome did not reach the synthetic window"
        )
        assert counter.discrepancy_rate(user_id="u", traffic_source="natural") == 0.0, (
            "a synthetic outcome leaked into the natural window"
        )
        assert counter.discrepancy_rate(user_id="u") == 0.0


# ---------------------------------------------------------------------------
# parallax_aphelion_total — "the secondary was actually attempted"
# ---------------------------------------------------------------------------


class TestAphelionAttemptCounter:
    def test_counts_attempted_outcomes_and_not_skips(self) -> None:
        """MUTANT: ``if outcome != "skipped"`` inverted to ``==``.

        This counter's documented meaning is "dual-read requests that
        attempted the Aphelion secondary", and ``skipped`` is by definition
        the outcome where no attempt was made. Inverted it counts exactly
        the wrong set while still moving on traffic, so it looks alive. No
        existing test reads this series back.
        """
        uid = "s7-aphelion-gate"

        before = _aphelion_total(uid, "natural")
        record_dual_read_outcome(user_id=uid, outcome="match")
        after_match = _aphelion_total(uid, "natural")

        assert after_match - before == 1, "an attempted dual read went uncounted"

        record_dual_read_outcome(user_id=uid, outcome="skipped")

        assert _aphelion_total(uid, "natural") - after_match == 0, (
            "a skipped request counted as an Aphelion attempt"
        )
