"""Mutation-hardening for ``parallax.canary_shadow`` (land-20260824 w5 S5).

Additive companion to ``test_canary_shadow.py``. Every test below exists
because a semantic mutant of the module SURVIVED the pre-existing suite
(``tests/router/test_canary_shadow.py``,
``tests/router/test_dual_read_decision_log_traffic_source.py``,
``tests/server/test_metrics_endpoint.py``). 43 mutants were applied one at a
time to an otherwise pristine tree; 33 died against the existing suite and 10
walked through it.

Tally for this module: applied 43 / killed-by-new 10 / already-covered 33 /
unaddressed 0.

This module has the strongest existing suite of the five in this wave -- the
sampling gate, the outcome routing, every counter label, the exception
swallow, the stage boundary comparisons and the warning dedup are all already
pinned. The ten survivors are the corners it stops at:

  * **Stage boundaries are tested AT the boundary, never between them.** The
    existing suite asserts 0.01 -> s1, 0.10 -> s2, 0.50 -> s3, which pins the
    ``<=`` comparisons but not the numbers themselves: widen a threshold from
    0.01 to 0.02 and the boundary case still lands on the same stage. A 1.5%
    rollout would then report itself as the 1% stage, and GATE 4's alert rules
    would be reading GATE 3's series. The tests here assert the values BETWEEN
    the documented thresholds.

  * **The disabled fast path is only asserted by its outcome.** With the
    observer off, "counters did not move" is also true if the guard is skipped
    and the RNG gate happens to reject -- which it always does, since
    ``random() >= 0.0`` can never be false. So the short-circuit could be
    removed and the only trace would be a draw taken from the process-wide RNG
    stream on every request. The test here counts the draws.

  * **The sampling gate is never sat on exactly.** ``random() >= fraction``
    excludes a draw exactly equal to the fraction. A real RNG hits that with
    probability zero, so only a stubbed draw can decide ``>=`` against ``>``.

  * **Silence is never asserted, only values.** An unset variable, an empty
    string and an explicit 0.0 all return 0.0 -- and all three still return 0.0
    if the code decides to treat them as malformed and warn about them first.
    An operator who has deliberately disabled the observer would get a
    misconfiguration warning per request for a configuration that is correct.

  * **The structured warning's fields are unasserted.** ``reason`` is the only
    thing distinguishing "not a float" from "out of range" in the log, and the
    module docstring says the ``event`` label deliberately does NOT vary, so
    dropping ``reason`` makes the two indistinguishable.

  * **The failure log's stack trace is unasserted.** The existing exception
    test asserts only that ``observe`` did not raise. The comment in the source
    is explicit that ``exc_info=True`` is the point -- "the swallow is what the
    spec asks for, not the silence" -- and a swallowed fault with no traceback
    is exactly the silence it warns against.

  * **The duplicate-registration path is only asserted not to explode.** The
    re-import test proves no ``DuplicatedTimeseries`` escapes; it does not prove
    the existing collector comes back, and it never creates the OTHER kind of
    name collision. prometheus indexes a Counter under three keys (the bare
    name, ``_total`` and ``_created``) but a Gauge under only the bare name, so
    probing the bare name instead of ``_total`` cannot distinguish "this module
    was re-imported" from "something else already owns this metric name" -- and
    the second case falls through to a ``KeyError``, which is precisely the
    masking the source comment says it does not want.

Expected values are LITERALS throughout: "s1"/"s2"/"s3"/"s4", 0.015, 0.15,
0.55, "not a float", "out of range [0,1]".
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any, cast

import prometheus_client
import pytest

from parallax import canary_shadow
from parallax.canary_shadow import _get_or_create_counter, get_shadow_fraction, resolve_stage
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import DualReadResult
from parallax.router.discrepancy_live import DualReadOutcome

_ENV = "PARALLAX_CANARY_SHADOW_FRACTION"
_LOGGER = "parallax.canary_shadow"


@pytest.fixture(autouse=True)
def _clean_env_and_sentinel() -> Iterator[None]:
    """Pop the fraction env var and reset the warning dedup sentinel per test."""
    original = os.environ.pop(_ENV, None)
    saved_sentinel = canary_shadow._last_warned_invalid_raw
    canary_shadow._last_warned_invalid_raw = None
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = original
        canary_shadow._last_warned_invalid_raw = saved_sentinel


def _result(outcome: str = "match") -> DualReadResult:
    evidence = RetrievalEvidence(
        hits=({"id": "a", "kind": "memory", "score": 1.0},), stages=("test",)
    )
    return DualReadResult(
        outcome=cast(DualReadOutcome, outcome),
        primary=evidence,
        secondary=evidence,
        correlation_id="corr-harden",
        latency_primary_ms=1.0,
        latency_secondary_ms=1.0,
        aphelion_unreachable_reason=None,
    )


def _counter_value(name: str, **labels: str) -> float:
    value = prometheus_client.REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else float(value)


def _invalid_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "canary_shadow_fraction_invalid"
    ]


# ----------------------------------------------------------------------
# Collector registration
# ----------------------------------------------------------------------


def test_duplicate_registration_returns_the_existing_collector() -> None:
    """A second registration hands back the FIRST collector, not an error.

    The existing re-import test proves no DuplicatedTimeseries escapes; it does
    not prove anything comes back. Paired with the name-clash test below this
    pins both halves of the recovery: a real duplicate is recovered, and a
    collision with a different collector is not.
    """
    name = "parallax_test_harden_duplicate_probe"
    first = _get_or_create_counter(name, "probe", ["stage"])
    try:
        second = _get_or_create_counter(name, "probe", ["stage"])
        assert second is first
    finally:
        prometheus_client.REGISTRY.unregister(first)


def test_a_name_clash_with_a_non_counter_re_raises_instead_of_masking() -> None:
    """The duplicate-recovery probes for a COUNTER, not just for the name.

    prometheus registers a Counter under three keys -- the bare name,
    ``_total`` and ``_created`` -- but a Gauge only under the bare name. So the
    bare name being taken does not mean a counter is there to hand back, and
    probing it instead of the ``_total`` suffix cannot tell "this module was
    re-imported" apart from "something else already owns this metric name". The
    source says exactly this: it does not want a genuine name conflict masked
    into a confusing ``KeyError``. The clash must surface as the ``ValueError``
    prometheus raised.
    """
    name = "parallax_test_harden_gauge_clash"
    gauge = prometheus_client.Gauge(name, "probe", ["stage"])
    try:
        with pytest.raises(ValueError):
            _get_or_create_counter(name, "probe", ["stage"])
    finally:
        prometheus_client.REGISTRY.unregister(gauge)


# ----------------------------------------------------------------------
# Fraction parsing: silence, and the reason field
# ----------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "0.0", "0"])
def test_a_deliberately_disabled_observer_warns_about_nothing(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Empty and zero are valid "off" settings, not misconfigurations.

    All three of these return 0.0 whether or not the code decides to treat them
    as malformed first, so the return value alone cannot tell the difference.
    An operator who has deliberately switched the observer off would otherwise
    get a misconfiguration warning on every single request.
    """
    os.environ[_ENV] = raw

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert get_shadow_fraction() == 0.0

    assert _invalid_warnings(caplog) == []


def test_invalid_fraction_warning_names_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``reason`` is the only field that separates the two failure modes.

    The ``event`` label is deliberately constant so log queries do not have to
    enumerate reasons -- which makes ``reason`` the sole carrier of "this was
    unparseable" versus "this parsed but is out of range".
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        os.environ[_ENV] = "not-a-number"
        assert get_shadow_fraction() == 0.0

    records = _invalid_warnings(caplog)
    assert len(records) == 1
    assert records[0].raw == "not-a-number"
    assert records[0].reason == "not a float"

    caplog.clear()
    canary_shadow._last_warned_invalid_raw = None

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        os.environ[_ENV] = "1.5"
        assert get_shadow_fraction() == 0.0

    records = _invalid_warnings(caplog)
    assert len(records) == 1
    assert records[0].raw == "1.5"
    assert records[0].reason == "out of range [0,1]"


# ----------------------------------------------------------------------
# Stage thresholds
# ----------------------------------------------------------------------


def test_stage_thresholds_hold_between_the_boundaries_too() -> None:
    """The threshold VALUES, not just the inclusive comparisons.

    Asserting only 0.01 / 0.10 / 0.50 pins ``<=`` against ``<`` but leaves the
    numbers free: widening s1 from 0.01 to 0.02 keeps every boundary case on
    its original stage. A 1.5% rollout would then label itself as the 1% stage
    and GATE 4's alert rules would be reading GATE 3's series.
    """
    # Upper bounds are inclusive.
    assert resolve_stage(0.01) == "s1"
    assert resolve_stage(0.10) == "s2"
    assert resolve_stage(0.50) == "s3"

    # And a hair above each bound belongs to the NEXT stage.
    assert resolve_stage(0.015) == "s2"
    assert resolve_stage(0.15) == "s3"
    assert resolve_stage(0.55) == "s4"


# ----------------------------------------------------------------------
# observe(): the disabled fast path and the sampling boundary
# ----------------------------------------------------------------------


def test_disabled_observer_never_draws_from_the_rng(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the observer off, ``observe`` short-circuits before sampling.

    "No counters moved" cannot show this: ``random() >= 0.0`` is true for every
    possible draw, so removing the guard produces the same counters and the
    only trace is a draw consumed from the process-wide RNG stream on every
    request -- perturbing it for every other consumer in the process.
    """
    draws: list[int] = []

    def _counting_random() -> float:
        draws.append(1)
        return 0.0

    monkeypatch.setattr(canary_shadow.random, "random", _counting_random)
    os.environ[_ENV] = "0.0"

    canary_shadow.observe(_result(), user_id="u-off", traffic_source="t-off")

    assert draws == []


def test_a_draw_equal_to_the_fraction_is_not_sampled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is ``draw >= fraction`` -> skip, so equality skips.

    A real RNG lands exactly on the fraction with probability zero, so this
    boundary is decidable only against a stubbed draw -- and getting it
    backwards makes the sampled subset one draw wider than the configured
    fraction at every stage.
    """
    attempts = "parallax_canary_shadow_attempts_total"
    labels = {"stage": "s3", "user_id": "u-edge", "traffic_source": "t-edge"}
    before = _counter_value(attempts, **labels)

    os.environ[_ENV] = "0.5"
    monkeypatch.setattr(canary_shadow.random, "random", lambda: 0.5)
    canary_shadow.observe(_result(), user_id="u-edge", traffic_source="t-edge")
    assert _counter_value(attempts, **labels) == before

    # Just under the fraction IS in the subset.
    monkeypatch.setattr(canary_shadow.random, "random", lambda: 0.4999)
    canary_shadow.observe(_result(), user_id="u-edge", traffic_source="t-edge")
    assert _counter_value(attempts, **labels) == before + 1.0


# ----------------------------------------------------------------------
# observe(): the swallowed failure must stay diagnosable
# ----------------------------------------------------------------------


class _ExplodingCounter:
    """Stands in for a metric backend that faults on every write."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def labels(self, **_labels: str) -> Any:
        raise self._exc


def test_observer_failure_is_logged_with_its_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The swallow is the contract; the silence is not.

    Existing coverage asserts only that ``observe`` does not raise. Without
    ``exc_info`` the warning names the exception but not where it came from,
    and a metric-backend fault inside a fire-and-forget observer is diagnosable
    from the log or from nothing at all.
    """
    boom = RuntimeError("metric backend down")
    monkeypatch.setattr(canary_shadow, "_canary_attempts_counter", _ExplodingCounter(boom))
    monkeypatch.setattr(canary_shadow.random, "random", lambda: 0.0)
    os.environ[_ENV] = "1.0"

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        canary_shadow.observe(_result(), user_id="u-boom", traffic_source="t-boom")

    records = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "canary_shadow_observe_failed"
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is boom
