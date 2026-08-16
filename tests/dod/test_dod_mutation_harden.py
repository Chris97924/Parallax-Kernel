"""Mutation-hardening for ``parallax.canary.dod`` (overnight-20260816 S9).

Companion to ``tests/dod/test_dod.py``, whose 40-cell acceptance matrix
(AC-3.14) proves each metric can reach PASS and non-PASS but calibrates every
cell far away from its threshold and shapes its corpus so the numbers on both
sides of each comparison are equal. That leaves the arithmetic itself
unpinned, and each test below was written against a semantic mutant that
survived the whole suite because of it:

  * ``_populate`` gives every synthetic event the *same* latency, so
    ``_quantile``'s nearest-rank index is never observable —
    ``floor`` for ``ceil``, ``rank`` for ``rank - 1``, and dropping the
    ``max(1, ...)`` floor all return the same number from a constant list.
  * No test passes ``window_days`` to ``compute_dod``, so hardcoding the
    7-day default survives; and no test records an event *after* ``until``,
    so the window's upper-bound predicate could be deleted outright.
  * Every threshold cell sits well clear of its boundary (10 hits vs 200,
    50 ms vs 250 ms against a 100 ms ceiling), so ``<`` for ``<=`` and
    ``>=`` for ``>`` at the MIN_HITS floor, the sample-size gate and the
    metric ceilings are all invisible.
  * ``canonical_sample = max(sample_size, total_outcomes)`` is only
    distinguishable from ``min`` on an audit-thin corpus, which exactly one
    test builds — and that test asserts on the other two metrics.
  * ERROR_RATE and DISCREPANCY_RATE are split-gated, so they read
    PENDING_IMPLEMENTATION in nearly every fixture. The one test that opens
    the gate feeds it a *healthy* corpus, so both numerators are zero: the
    ``>= 500`` status predicate and the discrepancy denominator never had a
    test that could see them.
  * ``_metric_family_has_traffic_source_label``'s exception fence (which
    fails the gate closed and logs ``gate_disabled``), its target-name
    guard, and its older-API label backstop had no tests at all.
"""

from __future__ import annotations

import datetime as _dt
import logging
import pathlib
from collections.abc import Iterator

import pytest

from parallax.canary.audit_log import AuditLog, make_record
from parallax.canary.dod import (
    DOD_THRESHOLD,
    SPLIT_OVERRIDE_ENV,
    DodMetric,
    DodVerdict,
    _iso,
    _metric_family_has_traffic_source_label,
    _quantile,
    _verdict_for,
    compute_dod,
)
from parallax.canary.outcomes import OutcomeStore

_STAGE = "m4_1pct"
_FIXED_NOW = _dt.datetime(2026, 5, 5, 12, 0, 0, tzinfo=_dt.UTC)


@pytest.fixture
def stores(tmp_path: pathlib.Path) -> Iterator[tuple[AuditLog, OutcomeStore]]:
    shared_db = tmp_path / "canary_audit.db"
    audit = AuditLog(db_path=shared_db)
    outcomes = OutcomeStore(db_path=shared_db)
    try:
        yield audit, outcomes
    finally:
        audit.close()
        outcomes.close()


def _eid(seed: int) -> str:
    return f"01950000-0000-7000-8000-{seed:012x}"


def _seed(
    audit: AuditLog,
    outcomes: OutcomeStore,
    *,
    count: int,
    stage: str = _STAGE,
    status: int = 200,
    latency_ms: float | None = 10.0,
    outcome: str = "ok",
    when: _dt.datetime = _FIXED_NOW,
    with_audit_row: bool = True,
    seed_offset: int = 0,
) -> None:
    """Write ``count`` events, optionally without their ``audit_log`` half.

    ``with_audit_row=False`` produces outcome rows that the DoD JOIN cannot
    reach — the audit-thin corpus that separates the two sample-size counts.
    """
    when_iso = _iso(when)
    for i in range(count):
        eid = _eid(seed_offset + i)
        if with_audit_row:
            audit.record(
                make_record(
                    event_id=eid,
                    response_status=status,
                    latency_ms=latency_ms,
                    idempotency_hit=False,
                )
            )
        outcomes.record(event_id=eid, stage=stage, outcome=outcome, recorded_at=when_iso)


def _metric(report, metric: DodMetric):
    return next(m for m in report.metrics if m.metric == metric)


# ===========================================================================
# _quantile — nearest-rank arithmetic
# ===========================================================================


@pytest.mark.unit
class TestQuantileNearestRank:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (100, 99.0),  # ceil(0.99*100) = 99 -> values[98]
            (50, 50.0),  # ceil(0.99*50)  = 50 -> values[49]; floor would give 49.0
            (200, 198.0),  # ceil(0.99*200) = 198 -> values[197]
            (3, 3.0),  # ceil(0.99*3)   = 3  -> values[2] (the clamp's near edge)
        ],
    )
    def test_p99_picks_the_nearest_rank_value(self, n: int, expected: float) -> None:
        """The index must be ``ceil(q * n) - 1``, clamped to the last element.

        Every corpus in the acceptance suite gives all events an identical
        latency, so the quantile returns the same number no matter which index
        it reads. Against a strictly increasing list the three plausible
        off-by-one mutants each land on a different value: ``floor`` for
        ``ceil`` returns 49.0 at n=50, dropping the ``- 1`` returns 100.0 at
        n=100, and ``q * (n - 1)`` returns 98.0 at n=100.
        """
        values = [float(i) for i in range(1, n + 1)]
        assert _quantile(values, 0.99) == expected

    def test_median_uses_the_same_formula(self) -> None:
        """A second quantile so the rank rule is pinned, not just the p99 cell."""
        values = [float(i) for i in range(1, 11)]  # 1..10
        assert _quantile(values, 0.5) == 5.0

    def test_q_zero_returns_the_first_value_not_the_last(self) -> None:
        """``max(1, ...)`` is a real guard: ``ceil(0 * n) - 1`` is ``-1``.

        Without the floor, Python's negative indexing silently returns the
        *largest* observation for the 0th quantile.
        """
        assert _quantile([1.0, 2.0, 3.0], 0.0) == 1.0

    def test_q_one_returns_the_last_value(self) -> None:
        assert _quantile([1.0, 2.0, 3.0], 1.0) == 3.0

    def test_empty_input_is_zero(self) -> None:
        assert _quantile([], 0.99) == 0.0

    def test_single_value_is_returned_verbatim(self) -> None:
        assert _quantile([7.5], 0.99) == 7.5


# ===========================================================================
# _verdict_for — threshold strictness and the sample-size gate
# ===========================================================================


@pytest.mark.unit
class TestVerdictBoundaries:
    def test_min_hits_exactly_at_the_floor_passes(self) -> None:
        """MIN_HITS is ``observed >= 50``; the acceptance matrix uses 200 and 10.

        Tightening it to ``>`` holds a canary that has collected exactly the
        required sample at INSUFFICIENT_DATA forever.
        """
        floor = DOD_THRESHOLD[DodMetric.MIN_HITS]
        assert _verdict_for(DodMetric.MIN_HITS, floor, int(floor)) == DodVerdict.PASS

    def test_min_hits_one_below_the_floor_is_insufficient(self) -> None:
        floor = DOD_THRESHOLD[DodMetric.MIN_HITS]
        assert (
            _verdict_for(DodMetric.MIN_HITS, floor - 1, int(floor) - 1)
            == DodVerdict.INSUFFICIENT_DATA
        )

    def test_sample_gate_admits_a_corpus_of_exactly_the_floor(self) -> None:
        """The gate is ``sample_size < 50 -> INSUFFICIENT_DATA``.

        Relaxing it to ``<=`` refuses to evaluate a corpus that has met the
        documented minimum, which reads to an operator as "keep waiting" on a
        canary that is already eligible for promotion.
        """
        assert _verdict_for(DodMetric.P99_LATENCY_MS, 10.0, 50) == DodVerdict.PASS
        assert _verdict_for(DodMetric.P99_LATENCY_MS, 10.0, 49) == DodVerdict.INSUFFICIENT_DATA

    def test_p99_exactly_at_the_ceiling_fails(self) -> None:
        """PASS is documented as strictly ``< threshold``."""
        ceiling = DOD_THRESHOLD[DodMetric.P99_LATENCY_MS]
        assert _verdict_for(DodMetric.P99_LATENCY_MS, ceiling, 200) == DodVerdict.FAIL
        assert _verdict_for(DodMetric.P99_LATENCY_MS, ceiling - 0.001, 200) == DodVerdict.PASS

    def test_error_rate_exactly_at_the_ceiling_fails(self) -> None:
        ceiling = DOD_THRESHOLD[DodMetric.ERROR_RATE]
        assert _verdict_for(DodMetric.ERROR_RATE, ceiling, 200) == DodVerdict.FAIL
        assert _verdict_for(DodMetric.ERROR_RATE, ceiling / 2, 200) == DodVerdict.PASS

    def test_data_loss_is_the_one_metric_that_passes_at_its_threshold(self) -> None:
        """DATA_LOSS_COUNT is ``<= 0``, not ``< 0`` — zero losses must PASS."""
        assert _verdict_for(DodMetric.DATA_LOSS_COUNT, 0, 200) == DodVerdict.PASS
        assert _verdict_for(DodMetric.DATA_LOSS_COUNT, 1, 200) == DodVerdict.FAIL

    def test_min_hits_ignores_the_sample_gate(self) -> None:
        """MIN_HITS must never report itself INSUFFICIENT via the shared gate.

        It is the metric that *reports* sample size; routing it through the
        ``sample_size < 50`` early return would make it structurally incapable
        of returning PASS.
        """
        assert _verdict_for(DodMetric.MIN_HITS, 200, 200) == DodVerdict.PASS


# ===========================================================================
# Window handling
# ===========================================================================


@pytest.mark.unit
class TestObservationWindow:
    def test_window_days_argument_narrows_the_window(
        self, stores: tuple[AuditLog, OutcomeStore]
    ) -> None:
        """``window_days`` must reach the SQL predicate.

        No acceptance test passes the argument, so hardcoding the 7-day default
        — or widening rather than narrowing the window — survives. An operator
        asking ``--window-days 1`` after a rollback would silently be shown the
        full week, including the traffic that caused the rollback.
        """
        audit, outcomes = stores
        _seed(audit, outcomes, count=60, when=_FIXED_NOW - _dt.timedelta(days=2))

        wide = compute_dod(
            audit_log=audit, outcomes=outcomes, stage=_STAGE, window_days=7, until=_FIXED_NOW
        )
        narrow = compute_dod(
            audit_log=audit, outcomes=outcomes, stage=_STAGE, window_days=1, until=_FIXED_NOW
        )

        assert _metric(wide, DodMetric.MIN_HITS).observed == 60
        assert _metric(wide, DodMetric.MIN_HITS).verdict == DodVerdict.PASS
        assert _metric(narrow, DodMetric.MIN_HITS).observed == 0
        assert _metric(narrow, DodMetric.MIN_HITS).verdict == DodVerdict.INSUFFICIENT_DATA

    def test_window_days_three_still_reaches_two_day_old_events(
        self, stores: tuple[AuditLog, OutcomeStore]
    ) -> None:
        """Positive twin: the boundary moves with the argument, both ways."""
        audit, outcomes = stores
        _seed(audit, outcomes, count=60, when=_FIXED_NOW - _dt.timedelta(days=2))

        report = compute_dod(
            audit_log=audit, outcomes=outcomes, stage=_STAGE, window_days=3, until=_FIXED_NOW
        )
        assert _metric(report, DodMetric.MIN_HITS).observed == 60

    def test_events_after_the_window_end_are_excluded(
        self, stores: tuple[AuditLog, OutcomeStore]
    ) -> None:
        """The window is closed at both ends — ``recorded_at <= until``.

        Only the lower bound has a test, so the upper-bound predicate could be
        dropped entirely. It is what keeps a clock-skewed writer's
        future-stamped rows, and any replay written after the evaluated window,
        out of a historical DoD verdict.
        """
        audit, outcomes = stores
        # In-window healthy traffic.
        _seed(audit, outcomes, count=60, seed_offset=0)
        # Future traffic: all data_loss, which would flip the verdict if counted.
        _seed(
            audit,
            outcomes,
            count=60,
            outcome="data_loss",
            when=_FIXED_NOW + _dt.timedelta(days=1),
            seed_offset=10_000,
        )

        report = compute_dod(audit_log=audit, outcomes=outcomes, stage=_STAGE, until=_FIXED_NOW)

        data_loss = _metric(report, DodMetric.DATA_LOSS_COUNT)
        assert data_loss.observed == 0
        assert data_loss.verdict == DodVerdict.PASS
        assert _metric(report, DodMetric.MIN_HITS).observed == 60


# ===========================================================================
# Sample-size bookkeeping across the two corpora
# ===========================================================================


@pytest.mark.unit
class TestCanonicalSampleIsTheUnion:
    def test_min_hits_counts_the_outcome_corpus_not_the_joined_subset(
        self, stores: tuple[AuditLog, OutcomeStore]
    ) -> None:
        """``canonical_sample`` is ``max(...)`` — "either side has enough data".

        The audit-thin corpus is the only shape that separates ``max`` from
        ``min``, and the one test that builds it asserts on ERROR_RATE and
        P99_LATENCY_MS instead. Under ``min`` a stage with 205 recorded
        outcomes is reported as having 5 hits, so MIN_HITS reads
        INSUFFICIENT_DATA and the promotion gate never opens.
        """
        audit, outcomes = stores
        _seed(audit, outcomes, count=5, seed_offset=0)  # joined
        _seed(audit, outcomes, count=200, seed_offset=10_000, with_audit_row=False)

        report = compute_dod(audit_log=audit, outcomes=outcomes, stage=_STAGE, until=_FIXED_NOW)

        min_hits = _metric(report, DodMetric.MIN_HITS)
        assert min_hits.observed == 205
        assert min_hits.sample_size == 205
        assert min_hits.verdict == DodVerdict.PASS

    def test_audit_backed_metrics_report_the_joined_count(
        self, stores: tuple[AuditLog, OutcomeStore]
    ) -> None:
        """The per-metric ``sample_size`` must stay anchored to its own corpus.

        P99_LATENCY_MS counts rows with a latency (5), DATA_LOSS_COUNT counts
        the outcome corpus (205). Collapsing either onto the union is what the
        comment above ``canonical_sample`` explicitly warns against.
        """
        audit, outcomes = stores
        _seed(audit, outcomes, count=5, seed_offset=0)
        _seed(audit, outcomes, count=200, seed_offset=10_000, with_audit_row=False)

        report = compute_dod(audit_log=audit, outcomes=outcomes, stage=_STAGE, until=_FIXED_NOW)

        assert _metric(report, DodMetric.P99_LATENCY_MS).sample_size == 5
        assert _metric(report, DodMetric.P99_LATENCY_MS).verdict == DodVerdict.INSUFFICIENT_DATA
        assert _metric(report, DodMetric.DATA_LOSS_COUNT).sample_size == 205


# ===========================================================================
# B1 / B2 arithmetic — only reachable with the split gate open
# ===========================================================================


@pytest.mark.unit
class TestErrorRateNumerator:
    """The status predicate is ``>= 500``, and it is only ever evaluated when
    the split gate is open. The single existing split-open test feeds a corpus
    with zero errors, so the comparison itself has never been exercised.
    """

    @pytest.fixture(autouse=True)
    def _open_the_split_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SPLIT_OVERRIDE_ENV, "1")

    def test_status_500_counts_as_an_error(self, stores: tuple[AuditLog, OutcomeStore]) -> None:
        """500 is the first error status — ``> 500`` would exclude it.

        A plain ``500 Internal Server Error`` is the single most likely failure
        a canary sees; a strict ``>`` reports a wholly broken stage as clean.
        """
        audit, outcomes = stores
        _seed(audit, outcomes, count=198, status=200, seed_offset=0)
        _seed(audit, outcomes, count=2, status=500, seed_offset=10_000)

        report = compute_dod(audit_log=audit, outcomes=outcomes, stage=_STAGE, until=_FIXED_NOW)

        error_rate = _metric(report, DodMetric.ERROR_RATE)
        assert error_rate.observed == pytest.approx(0.01)
        assert error_rate.verdict == DodVerdict.FAIL
        assert error_rate.sample_size == 200
        assert report.overall == DodVerdict.FAIL

    def test_status_503_counts_as_an_error(self, stores: tuple[AuditLog, OutcomeStore]) -> None:
        audit, outcomes = stores
        _seed(audit, outcomes, count=198, status=200, seed_offset=0)
        _seed(audit, outcomes, count=2, status=503, seed_offset=10_000)

        report = compute_dod(audit_log=audit, outcomes=outcomes, stage=_STAGE, until=_FIXED_NOW)
        assert _metric(report, DodMetric.ERROR_RATE).verdict == DodVerdict.FAIL

    def test_status_499_does_not_count_as_an_error(
        self, stores: tuple[AuditLog, OutcomeStore]
    ) -> None:
        """Negative twin: the threshold is 500, not 400.

        Loosening the predicate to ``>= 400`` would charge client-side
        rejections against the canary and roll back a healthy stage.
        """
        audit, outcomes = stores
        _seed(audit, outcomes, count=190, status=200, seed_offset=0)
        _seed(audit, outcomes, count=10, status=499, seed_offset=10_000)

        report = compute_dod(audit_log=audit, outcomes=outcomes, stage=_STAGE, until=_FIXED_NOW)

        error_rate = _metric(report, DodMetric.ERROR_RATE)
        assert error_rate.observed == 0.0
        assert error_rate.verdict == DodVerdict.PASS


@pytest.mark.unit
class TestDiscrepancyRateDenominator:
    @pytest.fixture(autouse=True)
    def _open_the_split_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SPLIT_OVERRIDE_ENV, "1")

    def test_rate_is_anchored_to_the_outcome_corpus(
        self, stores: tuple[AuditLog, OutcomeStore]
    ) -> None:
        """The denominator is ``total_outcomes``, not the joined audit count.

        The module comment states DoD rates are anchored to the
        ``canary_outcomes`` corpus. The numbers here are chosen so the two
        candidate denominators straddle the 0.5% ceiling: 1/400 = 0.25% PASSes,
        while 1/100 = 1% would FAIL — a spurious rollback driven by how many
        events happened to have an audit row.
        """
        audit, outcomes = stores
        # 100 events with both halves; one of them is the discrepancy.
        _seed(audit, outcomes, count=1, outcome="discrepancy", seed_offset=0)
        _seed(audit, outcomes, count=99, seed_offset=1)
        # 300 more outcome rows with no audit row.
        _seed(audit, outcomes, count=300, seed_offset=10_000, with_audit_row=False)

        report = compute_dod(audit_log=audit, outcomes=outcomes, stage=_STAGE, until=_FIXED_NOW)

        discrepancy = _metric(report, DodMetric.DISCREPANCY_RATE)
        assert discrepancy.sample_size == 400
        assert discrepancy.observed == pytest.approx(0.0025)
        assert discrepancy.verdict == DodVerdict.PASS

    def test_discrepancy_above_the_ceiling_still_fails(
        self, stores: tuple[AuditLog, OutcomeStore]
    ) -> None:
        """Positive twin: the metric can still FAIL once the gate is open."""
        audit, outcomes = stores
        _seed(audit, outcomes, count=4, outcome="discrepancy", seed_offset=0)
        _seed(audit, outcomes, count=196, seed_offset=10)

        report = compute_dod(audit_log=audit, outcomes=outcomes, stage=_STAGE, until=_FIXED_NOW)

        discrepancy = _metric(report, DodMetric.DISCREPANCY_RATE)
        assert discrepancy.observed == pytest.approx(0.02)
        assert discrepancy.verdict == DodVerdict.FAIL


# ===========================================================================
# Prometheus introspection — the advisory split signal
# ===========================================================================


class _RaisingRegistry:
    """A registry whose ``collect()`` blows up, as a version-drifted one might."""

    def collect(self):  # noqa: ANN201 - test double
        raise RuntimeError("registry backend exploded")


class _StaticRegistry:
    """A registry returning hand-built metric families."""

    def __init__(self, families: list[object]) -> None:
        self._families = families

    def collect(self):  # noqa: ANN201 - test double
        return list(self._families)


class _Sample:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.labels = labels


class _Family:
    def __init__(self, name: str, samples=(), **attrs: object) -> None:  # noqa: ANN003
        self.name = name
        self.samples = samples
        for key, value in attrs.items():
            setattr(self, key, value)


@pytest.mark.unit
class TestSplitSignalIntrospection:
    @pytest.fixture(autouse=True)
    def _no_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SPLIT_OVERRIDE_ENV, raising=False)

    def test_a_broken_registry_fails_the_gate_closed_and_reports_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An exception must return False and log ``gate_disabled=True`` at ERROR.

        Nothing exercised the fence, so flipping it to fail *open* — a registry
        quirk silently declaring the traffic split shipped — survived. The
        structured marker is the contract the module docstring tells monitoring
        to alert on, so it is asserted alongside the verdict rather than left
        to the message text.
        """
        with caplog.at_level(logging.ERROR, logger="parallax.canary.dod"):
            assert _metric_family_has_traffic_source_label(registry=_RaisingRegistry()) is False

        flagged = [rec for rec in caplog.records if getattr(rec, "gate_disabled", None) is True]
        assert flagged, "the fence must emit a gate_disabled=True record"
        assert flagged[0].levelno == logging.ERROR
        assert getattr(flagged[0], "exc_class", None) == "RuntimeError"

    def test_the_label_only_counts_on_the_parallax_aphelion_family(self) -> None:
        """A ``traffic_source`` label on an unrelated metric must not open the gate.

        The target-name guard is the only thing separating "PR #54 shipped" from
        "some other subsystem happens to label its counters the same way".
        Dropping it turns any third-party collector into a deployment claim.
        """
        unrelated = _Family(
            "some_other_subsystem",
            samples=(_Sample("some_other_subsystem_total", {"traffic_source": "synthetic"}),),
        )
        assert _metric_family_has_traffic_source_label(registry=_StaticRegistry([unrelated])) is (
            False
        )

    def test_the_total_suffixed_sample_name_is_accepted(self) -> None:
        """Families are matched by sample name too, not only ``Metric.name``.

        prometheus_client renders the counter as ``parallax_aphelion_total``;
        wrapper helpers in the ecosystem surface that spelling as the family
        name. Both must be recognised or the gate stays shut after the split
        genuinely ships.
        """
        family = _Family(
            "not_the_family_name",
            samples=(_Sample("parallax_aphelion_total", {"traffic_source": "natural"}),),
        )
        assert _metric_family_has_traffic_source_label(registry=_StaticRegistry([family])) is True

    def test_the_older_api_label_name_backstop_is_used(self) -> None:
        """A family with no samples yet still declares its label names.

        A freshly registered Counter that has never been incremented exposes no
        samples, so the sample walk finds nothing; ``_labelnames`` is what keeps
        the gate correct in that window. Deleting the backstop loop was
        invisible because every existing introspection test increments its
        counter first.
        """
        family = _Family("parallax_aphelion", samples=(), _labelnames=("traffic_source",))
        assert _metric_family_has_traffic_source_label(registry=_StaticRegistry([family])) is True

    def test_the_modern_label_names_spelling_is_also_accepted(self) -> None:
        family = _Family("parallax_aphelion", samples=(), label_names=("traffic_source",))
        assert _metric_family_has_traffic_source_label(registry=_StaticRegistry([family])) is True

    def test_the_right_family_without_the_label_keeps_the_gate_shut(self) -> None:
        """Negative twin: the family name alone is not the signal."""
        family = _Family(
            "parallax_aphelion",
            samples=(_Sample("parallax_aphelion_total", {"user_id": "u1"}),),
            _labelnames=("user_id",),
        )
        assert _metric_family_has_traffic_source_label(registry=_StaticRegistry([family])) is (
            False
        )

    def test_an_empty_registry_keeps_the_gate_shut(self) -> None:
        assert _metric_family_has_traffic_source_label(registry=_StaticRegistry([])) is False
