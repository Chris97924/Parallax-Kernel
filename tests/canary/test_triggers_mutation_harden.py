"""Mutation-hardening for ``parallax.canary.triggers`` (land-20260824 w5 S2).

Additive companion to ``test_triggers.py``. Every test below exists because a
semantic mutant of the module SURVIVED the pre-existing suite
(``tests/canary/test_triggers.py``, ``tests/canary/test_rollback.py``,
``tests/dod/test_dod.py``). 66 mutants were applied one at a time to an
otherwise pristine tree; 38 died against the existing suite and 28 walked
through it -- 23 needing a new killer, 5 provably equivalent.

Tally for this module: applied 66 / killed-by-new 23 / already-covered 38 /
equivalent-with-proof 5 / unaddressed 0.

The existing suite is genuinely good on the parts it covers: every threshold
and window constant, every trip boundary, the error/mismatch polarity, and the
whole eviction mechanism are already pinned. What it does not look at:

  * **The window edge is never sat on exactly.** Eviction drops a sample when
    its timestamp is strictly OLDER than the cutoff, so a sample landing
    exactly ``window`` seconds ago is still in scope. No existing test places a
    sample on that boundary, so ``<`` could relax to ``<=`` and silently
    shorten every window by one sample-width. The test here records a single
    error at t=0 and evaluates at exactly t=300.

  * **Each trigger's window length is asserted from its own constant, never
    from behaviour.** ``T1`` could be built on the 3-minute T2 window (and T3
    likewise) and still report ``window_seconds=300.0`` in its evaluation --
    the reported field and the deque that actually holds the samples are
    independent, and nothing crosses them. These tests drive a sample to an age
    that only the correct window keeps, and separately assert the reported
    field as a LITERAL.

  * **p99 is only ever asserted through a breach.** Existing latency tests feed
    values far above or far below 100 ms, so the verdict is the same for any
    rank near the top -- p95, ``ceil`` vs ``floor``, a missing ``-1``, even a
    descending sort all still trip or still pass. The rank itself is only
    visible in ``metric_value`` on a distinct, tightly spaced sample set, which
    is what the two tests here build (n=100 and n=50; the second exists because
    0.99*100 is an integer and hides the ceil/floor choice).

  * **reset() is never checked from the outside.** Nothing records, resets and
    then re-reads, so both the window's ``clear()`` and each trigger's
    delegation to it can be dropped with the suite still green.

  * **The evaluation payload is trusted.** ``threshold``, ``window_seconds``,
    ``metric_value`` and the gate's ``trigger_id`` are what the controller and
    the CLI dashboard render; several can be zeroed or renamed without any
    verdict changing. These are asserted as literals.

  * **``all_triggers()`` has no test at all.** It carries a
    ``pragma: no cover``, and dropping T4 from it -- which would silently
    disable data-loss rollback for every caller that enumerates rather than
    naming the triggers -- was invisible.

Five mutants are provably EQUIVALENT and are recorded as such rather than
being given a contrived test:

  * ``n > 0 and rate >= T1_THRESHOLD`` -> ``n >= 0 and ...`` (and the T2 twin).
    The two differ only at n == 0, and there ``rate`` is unconditionally 0.0
    from the ``if n else 0.0`` arm, so the comparison ``0.0 >= 0.005`` is False
    either way and the conjunction is False either way. No input distinguishes
    them.
  * ``min(n - 1, ceil(0.99*n) - 1)`` -> ``min(n, ...)``. ``n`` is an integer
    and ``0.99*n <= n``, so ``ceil(0.99*n) <= n`` and the right operand is
    always ``<= n - 1``; the ``min`` therefore always selects it and the bound
    is never the deciding one.
  * ``max(0, min(n - 1, ceil(0.99*n) - 1))`` -> ``min(n - 1, ...)``. The n == 0
    case returns early, and for n >= 1 ``0.99*n >= 0.99 > 0`` so
    ``ceil(0.99*n) >= 1`` and the index is already >= 0. The clamp is dead.
  * ``T5MinHitsGate.record`` storing ``1.0`` -> ``0.0``. T5 reads its window
    only through ``count()``, which returns ``len(deque)``; the stored value is
    never read on any T5 path, and the window is private with no accessor.

Expected values are LITERALS throughout -- 300.0, 180.0, 0.005, 100.0, 50.0,
"T5-min-hits-gate". Importing the module's own constant to build the
expectation is precisely what lets the constant move with nothing failing.
"""

from __future__ import annotations

from parallax.canary.triggers import (
    T1ErrorRateTrigger,
    T2DiscrepancyRateTrigger,
    T3P99LatencyTrigger,
    T4DataLossTrigger,
    T5MinHitsGate,
    TriggerVerdict,
    all_triggers,
)

# ----------------------------------------------------------------------
# Verdict wire values
# ----------------------------------------------------------------------


def test_trigger_verdict_wire_values_are_literal_strings() -> None:
    """The three verdicts are a wire format rendered by the CLI and dashboard.

    Every existing assertion compares against the enum member, which moves with
    the value.
    """
    assert TriggerVerdict.PASS.value == "pass"
    assert TriggerVerdict.BREACH.value == "breach"
    assert TriggerVerdict.INSUFFICIENT_DATA.value == "insufficient_data"


# ----------------------------------------------------------------------
# Sliding-window edge and reset
# ----------------------------------------------------------------------


def test_sample_exactly_one_window_old_is_still_in_scope() -> None:
    """A sample at exactly ``now - window`` has NOT aged out yet.

    Eviction drops strictly older samples. Sitting exactly on the edge is the
    only place the ``<``/``<=`` choice is observable, and it decides a real
    verdict here: the single in-window error is a 100% error rate.
    """
    trigger = T1ErrorRateTrigger()
    trigger.record(0.0, is_error=True)

    on_the_edge = trigger.evaluate(300.0)
    assert on_the_edge.observations == 1
    assert on_the_edge.metric_value == 1.0
    assert on_the_edge.verdict is TriggerVerdict.BREACH

    # One second past the edge it is gone, and the trigger is clean again.
    past_the_edge = trigger.evaluate(301.0)
    assert past_the_edge.observations == 0
    assert past_the_edge.metric_value == 0.0
    assert past_the_edge.verdict is TriggerVerdict.PASS


def test_reset_actually_empties_the_window() -> None:
    """``reset()`` has to reach the deque, not just be called.

    Nothing else records, resets and re-reads, so a reset that delegates
    nowhere -- or a window whose ``reset`` rebuilds the deque instead of
    clearing it -- leaves a tripped trigger tripped forever.
    """
    trigger = T1ErrorRateTrigger()
    for i in range(5):
        trigger.record(float(i), is_error=True)
    assert trigger.evaluate(10.0).observations == 5

    trigger.reset()

    after = trigger.evaluate(10.0)
    assert after.observations == 0
    assert after.metric_value == 0.0
    assert after.verdict is TriggerVerdict.PASS


# ----------------------------------------------------------------------
# T1 — window length and reported payload
# ----------------------------------------------------------------------


def test_t1_empty_window_reports_a_zero_rate() -> None:
    """With nothing recorded the reported rate is 0.0, not a sentinel."""
    evaluation = T1ErrorRateTrigger().evaluate(0.0)

    assert evaluation.observations == 0
    assert evaluation.metric_value == 0.0
    assert evaluation.verdict is TriggerVerdict.PASS


def test_t1_keeps_samples_for_a_full_five_minutes() -> None:
    """T1's storage really is the 5-minute window, not T2's 3-minute one.

    At t=250 the sample is 250 s old: inside 300 s, outside 180 s. The reported
    ``window_seconds`` field cannot prove this -- it is a separate literal in
    the evaluation and would still read 300.0 on the wrong deque.
    """
    trigger = T1ErrorRateTrigger()
    trigger.record(0.0, is_error=True)

    assert trigger.evaluate(250.0).observations == 1
    assert trigger.evaluate(301.0).observations == 0


def test_t1_evaluation_reports_its_threshold_and_window(  # noqa: D103
) -> None:
    trigger = T1ErrorRateTrigger()
    trigger.record(0.0, is_error=False)

    evaluation = trigger.evaluate(1.0)
    assert evaluation.trigger_id == "T1-error-rate"
    assert evaluation.threshold == 0.005
    assert evaluation.window_seconds == 300.0
    assert evaluation.observations == 1


# ----------------------------------------------------------------------
# T2 — reported payload
# ----------------------------------------------------------------------


def test_t2_evaluation_reports_the_three_minute_window() -> None:
    """T2's window is 180 s. Reporting T1's 300 s would misread every dashboard."""
    trigger = T2DiscrepancyRateTrigger()
    trigger.record(0.0, is_mismatch=False)

    evaluation = trigger.evaluate(1.0)
    assert evaluation.trigger_id == "T2-discrepancy-rate"
    assert evaluation.threshold == 0.005
    assert evaluation.window_seconds == 180.0


# ----------------------------------------------------------------------
# T3 — the p99 rank itself
# ----------------------------------------------------------------------


def test_t3_p99_of_a_hundred_distinct_samples_is_the_ninety_ninth() -> None:
    """Nearest-rank p99 over 1..100 ms is 99.0 ms.

    ceil(0.99 * 100) - 1 = 98, and the sorted sample at index 98 is 99.0. Every
    neighbouring rank is a different number here, which is what makes the
    percentile visible at all: p95 reads 95.0, a missing ``-1`` reads 100.0,
    and a descending sort reads 2.0. All four verdicts are PASS, so the rank is
    only observable through ``metric_value``.
    """
    trigger = T3P99LatencyTrigger()
    for i in range(1, 101):
        trigger.record(0.0, latency_ms=float(i))

    evaluation = trigger.evaluate(1.0)
    assert evaluation.observations == 100
    assert evaluation.metric_value == 99.0
    assert evaluation.verdict is TriggerVerdict.PASS


def test_t3_p99_rounds_the_rank_up_on_a_fractional_index() -> None:
    """With n=50 the rank is fractional, so ceil vs floor finally diverges.

    0.99 * 50 = 49.5. Rounding up gives index 49 -- the largest sample, 50.0.
    Rounding down would give index 48 and report 49.0. n=100 cannot see this
    because 0.99 * 100 is exactly 99.
    """
    trigger = T3P99LatencyTrigger()
    for i in range(1, 51):
        trigger.record(0.0, latency_ms=float(i))

    evaluation = trigger.evaluate(1.0)
    assert evaluation.observations == 50
    assert evaluation.metric_value == 50.0


def test_t3_keeps_samples_for_a_full_five_minutes() -> None:
    """T3's storage is the 5-minute window, not T2's 3-minute one."""
    trigger = T3P99LatencyTrigger()
    trigger.record(0.0, latency_ms=42.0)

    assert trigger.evaluate(250.0).observations == 1
    assert trigger.evaluate(301.0).observations == 0


# ----------------------------------------------------------------------
# T4 — cumulative counting and the cumulative contract
# ----------------------------------------------------------------------


def test_t4_accumulates_across_calls() -> None:
    """T4 is a running total, not a last-value.

    Existing tests only ever record once, so an assignment reads identically to
    an accumulation -- and a canary that loses data twice would report one loss.
    """
    trigger = T4DataLossTrigger()
    trigger.record()
    trigger.record(3)

    evaluation = trigger.evaluate()
    assert evaluation.metric_value == 4.0
    assert evaluation.observations == 4
    assert evaluation.verdict is TriggerVerdict.BREACH


def test_t4_declares_itself_windowless() -> None:
    """T4 is cumulative: both the class attribute and the evaluation say None.

    A numeric window here would make the controller and the dashboard render
    the cumulative loss count as a windowed rate.
    """
    assert T4DataLossTrigger.window_seconds is None

    trigger = T4DataLossTrigger()
    evaluation = trigger.evaluate()
    assert evaluation.window_seconds is None
    assert evaluation.trigger_id == "T4-data-loss"
    assert evaluation.threshold == 0.0
    assert evaluation.verdict is TriggerVerdict.PASS


# ----------------------------------------------------------------------
# T5 — the gate's reported payload
# ----------------------------------------------------------------------


def test_t5_evaluation_reports_the_measured_hits_and_its_threshold() -> None:
    """The gate's own identity and numbers, as literals.

    The gate never breaches, so its verdict alone cannot distinguish a correct
    payload from a zeroed one -- and the operator reads exactly these fields to
    decide whether a canary verdict is trustworthy yet.
    """
    gate = T5MinHitsGate()
    for i in range(3):
        gate.record(float(i))

    evaluation = gate.evaluate(10.0)
    assert evaluation.trigger_id == "T5-min-hits-gate"
    assert evaluation.metric_value == 3.0
    assert evaluation.threshold == 50.0
    assert evaluation.observations == 3
    assert evaluation.verdict is TriggerVerdict.INSUFFICIENT_DATA
    assert gate.is_active(10.0) is True


# ----------------------------------------------------------------------
# all_triggers()
# ----------------------------------------------------------------------


def test_all_triggers_returns_t1_to_t4_in_spec_order() -> None:
    """The convenience enumerator had no test at all.

    A caller that enumerates rather than naming the triggers gets whatever this
    returns -- so dropping T4 from it silently disables data-loss rollback for
    that caller, and reordering misaligns anything that zips the result against
    the spec table.
    """
    assert [t.trigger_id for t in all_triggers()] == [
        "T1-error-rate",
        "T2-discrepancy-rate",
        "T3-p99-latency",
        "T4-data-loss",
    ]
