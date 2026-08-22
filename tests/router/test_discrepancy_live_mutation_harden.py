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
# The window is re-evaluated on read, not only on write (#116)
# ---------------------------------------------------------------------------


class TestReadSideWindowDecay:
    """The trim used to run on the write path only.

    Unlike the rest of this file these were not written against a mutant of
    the shipped code — the shipped code *was* the defect (#116, found in the
    same S7 sweep). A user that stops sending traffic never reaches the
    write-path trim again, so whatever the window last held was reported as
    the current rate forever: a burst of divergences followed by silence read
    as a permanently elevated rate instead of decaying out. The contract is
    that a read re-evaluates the window against the current clock; these pin
    that, and the read path's own trim arithmetic with it.
    """

    def test_quiet_users_discrepancy_rate_decays_out_of_the_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#116 repro: rate 1.0 -> traffic stops -> clock past the window -> 0.0.

        MUTANT, and the shipped defect: no trim on the read path at all, or
        a cutoff computed from the newest recorded entry instead of from the
        current clock. Both leave the stale burst in the denominator, and
        both are invisible to every existing test, because all of them read
        back either immediately after a write or after a write that did the
        trimming for them.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        counter = LiveDiscrepancyCounter(window_seconds=100.0)

        counter.record(user_id="quiet", outcome="diverge")
        assert counter.discrepancy_rate(user_id="quiet") == 1.0, "precondition"

        clock.now = 1000.0  # silence, well past the 100s window

        assert counter.discrepancy_rate(user_id="quiet") == 0.0, (
            "a user that stopped sending traffic kept its last rate; the read "
            "side is not re-evaluating the window against the current clock"
        )

    def test_quiet_users_unreachable_rate_decays_out_of_the_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same freeze on the other reader.

        MUTANT: the read-side trim wired into ``discrepancy_rate`` only. The
        two rates share a window and a future consumer; a frozen unreachable
        rate holds ``AphelionUnreachableRateHigh`` on just as effectively as
        a frozen discrepancy rate holds its own alert on.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        counter = LiveDiscrepancyCounter(window_seconds=100.0)

        counter.record(user_id="quiet", outcome="aphelion_unreachable")
        assert counter.aphelion_unreachable_rate(user_id="quiet") == 1.0, "precondition"

        clock.now = 1000.0

        assert counter.aphelion_unreachable_rate(user_id="quiet") == 0.0, (
            "the unreachable rate froze at its last value after traffic stopped"
        )

    def test_the_public_reader_decays_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same repro through the module-level API on the singleton.

        ``dual_read_discrepancy_rate`` is the function the first consumer
        will call, and the singleton's default window is the one it will get.
        A fix applied to ``LiveDiscrepancyCounter`` but bypassed by the
        module-level wrapper would still ship the frozen rate.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        uid = "s7-read-decay-singleton"

        record_dual_read_outcome(user_id=uid, outcome="diverge")
        assert dl.dual_read_discrepancy_rate(user_id=uid) == 1.0, "precondition"

        clock.now = 7200.0  # two default windows of silence

        assert dl.dual_read_discrepancy_rate(user_id=uid) == 0.0, (
            "the public rolling-window reader froze at its last value"
        )

    def test_a_third_partys_write_is_not_what_clears_the_quiet_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The issue's control, kept as a test: the decay belongs to the reader.

        A write trims only the deque it appends to. Pinning that keeps the
        repro above honest — a "fix" that swept every user's window on any
        write would pass it while leaving a genuinely idle process, one
        taking no writes at all, frozen exactly as before.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        counter = LiveDiscrepancyCounter(window_seconds=100.0)

        counter.record(user_id="quiet", outcome="diverge")
        clock.now = 1000.0
        counter.record(user_id="busy", outcome="match")

        assert counter._data[("quiet", "natural")], (
            "another user's write emptied the quiet user's window; a write "
            "must only ever touch its own deque"
        )
        assert counter.discrepancy_rate(user_id="quiet") == 0.0, (
            "the read did not decay the quiet user's window"
        )

    def test_read_keeps_entries_that_are_still_inside_the_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: the read-side comparison reversed, ``<`` -> ``>``.

        Reversed, every read empties the window from the front and every
        rate reads 0.0, so no alert can ever fire. The repro above cannot
        catch it — there the expected answer is 0.0 as well.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        counter = LiveDiscrepancyCounter(window_seconds=100.0)

        counter.record(user_id="u", outcome="diverge")
        clock.now = 50.0  # half a window later; the entry is still in

        assert counter.discrepancy_rate(user_id="u") == 1.0, (
            "a read evicted an entry that was still inside the window"
        )

    def test_read_retains_the_entry_sitting_exactly_on_the_cutoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: the read-side comparison ``< cutoff`` -> ``<= cutoff``.

        ``TestWindowTrim`` pins this boundary for the write path. The read
        path has to agree with it, or one entry is inside or outside the
        window depending on which side looked at it last.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        counter = LiveDiscrepancyCounter(window_seconds=100.0)

        counter.record(user_id="u", outcome="diverge")
        clock.now = 100.0  # cutoff is now exactly the entry's own timestamp

        assert counter.discrepancy_rate(user_id="u") == 1.0, (
            "the entry sitting exactly on the cutoff was evicted on read"
        )

    def test_read_evicts_the_oldest_entry_not_the_newest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: the read-side eviction popping the wrong end.

        One expired entry, one live one, and no write in between to hide it:
        popping from the right drops the live entry first and then spins on
        the expired one until the deque is empty, so the rate reads 0.0
        where the surviving outcome says 1.0.
        """
        clock = _FakeClock()
        monkeypatch.setattr(dl, "time", clock)
        counter = LiveDiscrepancyCounter(window_seconds=100.0)

        counter.record(user_id="u", outcome="match")  # t=0 — expires
        clock.now = 60.0
        counter.record(user_id="u", outcome="diverge")  # t=60 — must survive
        clock.now = 120.0  # cutoff 20.0: t=0 is out, t=60 is in

        assert counter.discrepancy_rate(user_id="u") == 1.0, (
            "the read-side trim evicted the live entry instead of the expired one"
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
