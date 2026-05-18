"""Tests for canary_shadow observation overlay (Apex M4 GATE 3-7 pre-req).

Covers:
- get_shadow_fraction env parsing (valid / unset / malformed / out-of-range)
- resolve_stage boundary classification
- observe() per-request RNG gate at fraction edges (0.0, 1.0, middle)
- observe() metric increments by DualReadResult.outcome (match / diverge /
  primary_only / aphelion_unreachable / skipped)
- observe() exception isolation — never raises out
- observe() is pure post-hoc — does not mutate DualReadResult
- Module re-import idempotent (no prometheus DuplicatedTimeseries)
- Concurrent observe() correctness under N threads
"""

from __future__ import annotations

import importlib
import os
import threading
from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

import prometheus_client
import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import DualReadResult
from parallax.router.discrepancy_live import DualReadOutcome

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(*ids: str) -> RetrievalEvidence:
    hits = tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids)
    return RetrievalEvidence(hits=hits, stages=("test",))


def _result(
    outcome: str = "match",
    *,
    primary_ids: tuple[str, ...] = ("a",),
    secondary_ids: tuple[str, ...] | None = ("a",),
    correlation_id: str = "corr-test",
) -> DualReadResult:
    secondary = _evidence(*secondary_ids) if secondary_ids is not None else None
    return DualReadResult(
        outcome=cast(DualReadOutcome, outcome),
        primary=_evidence(*primary_ids),
        secondary=secondary,
        correlation_id=correlation_id,
        latency_primary_ms=1.0,
        latency_secondary_ms=None if secondary is None else 1.0,
        aphelion_unreachable_reason=None,
    )


def _counter_value(counter_name: str, **labels: str) -> float:
    """Return the current value of a labelled Counter via the public API.

    Uses ``prometheus_client.REGISTRY.get_sample_value`` which is the stable
    cross-version read path for the test suite. ``counter_name`` is the
    metric name including the ``_total`` suffix that ``Counter`` appends.
    Returns 0.0 if the sample does not yet exist.
    """
    value = prometheus_client.REGISTRY.get_sample_value(counter_name, labels)
    return 0.0 if value is None else float(value)


_ENV = "PARALLAX_CANARY_SHADOW_FRACTION"


@pytest.fixture(autouse=True)
def _clean_env_and_module() -> Iterator[None]:
    """Pop PARALLAX_CANARY_SHADOW_FRACTION before/after each test to avoid leakage."""
    original = os.environ.pop(_ENV, None)
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = original


# ---------------------------------------------------------------------------
# 1. Env parsing & stage resolution
# ---------------------------------------------------------------------------


def test_get_shadow_fraction_unset_returns_zero() -> None:
    from parallax import canary_shadow

    assert canary_shadow.get_shadow_fraction() == 0.0


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0.0", 0.0),
        ("0.01", 0.01),
        ("0.1", 0.1),
        ("0.5", 0.5),
        ("1.0", 1.0),
    ],
)
def test_get_shadow_fraction_valid_values(raw: str, expected: float) -> None:
    os.environ[_ENV] = raw
    from parallax import canary_shadow

    assert canary_shadow.get_shadow_fraction() == expected


@pytest.mark.parametrize("raw", ["abc", "", "-0.1", "1.5", "NaN", "inf"])
def test_get_shadow_fraction_malformed_falls_back_to_zero(raw: str) -> None:
    os.environ[_ENV] = raw
    from parallax import canary_shadow

    assert canary_shadow.get_shadow_fraction() == 0.0


@pytest.mark.parametrize(
    "fraction, expected_stage",
    [
        (0.0, "disabled"),
        (0.005, "s1"),
        (0.01, "s1"),
        (0.05, "s2"),
        (0.10, "s2"),
        (0.30, "s3"),
        (0.50, "s3"),
        (0.99, "s4"),
        (1.00, "s4"),
    ],
)
def test_resolve_stage_boundaries(fraction: float, expected_stage: str) -> None:
    from parallax import canary_shadow

    assert canary_shadow.resolve_stage(fraction) == expected_stage


# ---------------------------------------------------------------------------
# 2. observe() — disabled path
# ---------------------------------------------------------------------------


def test_observe_disabled_does_not_increment() -> None:
    from parallax import canary_shadow

    before_attempts = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="disabled",
        user_id="u1",
        traffic_source="natural",
    )

    canary_shadow.observe(_result("match"), user_id="u1", traffic_source="natural")

    after_attempts = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="disabled",
        user_id="u1",
        traffic_source="natural",
    )
    assert after_attempts == before_attempts


# ---------------------------------------------------------------------------
# 3. observe() — sampled increments by outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    ["match", "diverge", "primary_only", "aphelion_unreachable", "skipped"],
)
def test_observe_at_full_fraction_increments_outcome(outcome: str) -> None:
    os.environ[_ENV] = "1.0"
    from parallax import canary_shadow

    user = f"u-full-{outcome}"
    before_outcome = _counter_value(
        "parallax_canary_shadow_outcomes_total",
        stage="s4",
        outcome=outcome,
        user_id=user,
        traffic_source="natural",
    )
    before_attempts = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="s4",
        user_id=user,
        traffic_source="natural",
    )

    canary_shadow.observe(
        _result(outcome, secondary_ids=None if outcome == "skipped" else ("a",)),
        user_id=user,
        traffic_source="natural",
    )

    after_outcome = _counter_value(
        "parallax_canary_shadow_outcomes_total",
        stage="s4",
        outcome=outcome,
        user_id=user,
        traffic_source="natural",
    )
    after_attempts = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="s4",
        user_id=user,
        traffic_source="natural",
    )
    assert after_outcome == before_outcome + 1.0
    assert after_attempts == before_attempts + 1.0


# ---------------------------------------------------------------------------
# 4. observe() — RNG gate at middle fraction
# ---------------------------------------------------------------------------


def test_observe_rng_below_fraction_samples() -> None:
    os.environ[_ENV] = "0.5"
    from parallax import canary_shadow

    before = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="s3",
        user_id="u-rng-below",
        traffic_source="natural",
    )

    with patch("parallax.canary_shadow.random.random", return_value=0.3):
        canary_shadow.observe(_result("match"), user_id="u-rng-below", traffic_source="natural")

    after = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="s3",
        user_id="u-rng-below",
        traffic_source="natural",
    )
    assert after == before + 1.0


def test_observe_rng_above_fraction_skips() -> None:
    os.environ[_ENV] = "0.5"
    from parallax import canary_shadow

    before = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="s3",
        user_id="u-rng-above",
        traffic_source="natural",
    )

    with patch("parallax.canary_shadow.random.random", return_value=0.7):
        canary_shadow.observe(_result("match"), user_id="u-rng-above", traffic_source="natural")

    after = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="s3",
        user_id="u-rng-above",
        traffic_source="natural",
    )
    assert after == before


# ---------------------------------------------------------------------------
# 5. Failure isolation
# ---------------------------------------------------------------------------


def test_observe_swallows_internal_exception(caplog: pytest.LogCaptureFixture) -> None:
    """observe() must NOT raise even if metric backend faults."""
    os.environ[_ENV] = "1.0"
    from parallax import canary_shadow

    def _boom(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated prom failure")

    with patch.object(canary_shadow._canary_outcomes_counter, "labels", side_effect=_boom):
        # Should not raise.
        canary_shadow.observe(_result("match"), user_id="u-boom", traffic_source="natural")

    assert any("canary_shadow_observe_failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 6. Immutability — does not mutate DualReadResult
# ---------------------------------------------------------------------------


def test_observe_does_not_mutate_result() -> None:
    os.environ[_ENV] = "1.0"
    from parallax import canary_shadow

    result = _result("match")
    snapshot = (
        result.outcome,
        result.correlation_id,
        result.latency_primary_ms,
        tuple(h["id"] for h in result.primary.hits),
    )

    canary_shadow.observe(result, user_id="u-immutable", traffic_source="natural")

    assert (
        result.outcome,
        result.correlation_id,
        result.latency_primary_ms,
        tuple(h["id"] for h in result.primary.hits),
    ) == snapshot


# ---------------------------------------------------------------------------
# 6b. _get_or_create_counter re-raises on non-duplicate ValueError
# ---------------------------------------------------------------------------


def test_get_or_create_counter_reraises_unrelated_value_error() -> None:
    """A non-duplicate ValueError must surface, not be swallowed into KeyError."""
    from parallax import canary_shadow

    sentinel = ValueError("not a duplicate timeseries error")

    def _raise_unrelated(*_args: object, **_kwargs: object) -> None:
        raise sentinel

    with patch("parallax.canary_shadow.prometheus_client.Counter", side_effect=_raise_unrelated):
        with pytest.raises(ValueError) as excinfo:
            canary_shadow._get_or_create_counter(
                "parallax_canary_shadow_not_registered_metric",
                "doc",
                ["label_a"],
            )
        assert excinfo.value is sentinel


# ---------------------------------------------------------------------------
# 7. Module re-import idempotent
# ---------------------------------------------------------------------------


def test_module_reimport_does_not_crash() -> None:
    import parallax.canary_shadow as mod

    importlib.reload(mod)
    # Counters still callable post-reload.
    mod.observe(_result("match"), user_id="u-reload", traffic_source="natural")


# ---------------------------------------------------------------------------
# 8. Concurrent observe() correctness
# ---------------------------------------------------------------------------


def test_observe_concurrent_threads_consistent() -> None:
    os.environ[_ENV] = "1.0"
    from parallax import canary_shadow

    user = "u-concurrent"
    before = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="s4",
        user_id=user,
        traffic_source="natural",
    )

    def _worker() -> None:
        for _ in range(10):
            canary_shadow.observe(_result("match"), user_id=user, traffic_source="natural")

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    after = _counter_value(
        "parallax_canary_shadow_attempts_total",
        stage="s4",
        user_id=user,
        traffic_source="natural",
    )
    assert after == before + 100.0
