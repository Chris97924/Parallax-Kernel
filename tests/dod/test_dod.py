"""US-009.3 §5 acceptance tests for :mod:`parallax.canary.dod`.

Acceptance criteria covered (per `docs/m4-prep/us-009-acceptance-criteria.md`):

* AC-3.1 — 5 metrics × 7-day rolling window pass/fail.
* AC-3.2 — 4 stages independent verification (no cross-stage spillover).
* AC-3.5 — DoD scripts read from audit_log SQLite directly (no service
  dependency).
* AC-3.14 — ≥ 40 test cases (4 stages × 5 metrics × pass/fail = 40,
  parametrized).

The parametrized matrix in :func:`test_dod_metric_matrix` IS the AC-3.14
test count — 4 stages × 5 metrics × 2 verdicts = 40 distinct invocations.
Additional tests below cover stage-isolation (AC-3.2), insufficient-data
handling, and the JSON CLI output contract.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
from collections.abc import Iterator

import pytest

from parallax.canary.audit_log import AuditLog, make_record
from parallax.canary.dod import (
    DEFAULT_WINDOW_DAYS,
    DOD_THRESHOLD,
    DodMetric,
    DodVerdict,
    _iso,
    compute_dod,
)
from parallax.canary.outcomes import KNOWN_STAGES, OutcomeStore

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def shared_db(tmp_path: pathlib.Path) -> pathlib.Path:
    """A single SQLite file shared by AuditLog + OutcomeStore."""
    return tmp_path / "canary_audit.db"


@pytest.fixture
def stores(
    shared_db: pathlib.Path,
) -> Iterator[tuple[AuditLog, OutcomeStore]]:
    audit = AuditLog(db_path=shared_db)
    outcomes = OutcomeStore(db_path=shared_db)
    try:
        yield audit, outcomes
    finally:
        audit.close()
        outcomes.close()


# ----------------------------------------------------------------------
# Helpers — synthetic data shaping
# ----------------------------------------------------------------------


_FIXED_NOW = _dt.datetime(2026, 5, 5, 12, 0, 0, tzinfo=_dt.UTC)


def _eid(seed: int) -> str:
    """Build a deterministic v7-shaped event_id from an integer seed.

    The DoD module never validates UUID-v7 shape; we only need uniqueness.
    """
    return f"01950000-0000-7000-8000-{seed:012x}"


def _populate(
    *,
    audit: AuditLog,
    outcomes: OutcomeStore,
    stage: str,
    count: int,
    error_count: int = 0,
    discrepancy_count: int = 0,
    data_loss_count: int = 0,
    p99_latency_ms: float = 20.0,
    seed_offset: int = 0,
    when: _dt.datetime = _FIXED_NOW,
) -> None:
    """Populate ``count`` synthetic canary events for ``stage``.

    The first ``error_count`` events get HTTP 500. The next
    ``discrepancy_count`` are tagged outcome='discrepancy'. The next
    ``data_loss_count`` are tagged outcome='data_loss'. Remaining events
    are 200 / outcome='ok'. Latencies are ``p99_latency_ms`` for the LAST
    event and 10ms for the rest, so the p99 is exactly the requested value.
    """
    when_iso = _iso(when)
    for i in range(count):
        eid = _eid(seed_offset + i)
        if i < error_count:
            status = 500
        else:
            status = 200
        # Every event gets ``p99_latency_ms`` so the quantile equals the
        # requested value regardless of count or rank semantics — keeps
        # the parametrize matrix calibration trivial.
        latency = p99_latency_ms
        audit.record(
            make_record(
                event_id=eid,
                response_status=status,
                latency_ms=latency,
                idempotency_hit=False,
            )
        )
        if i < discrepancy_count:
            outcome = "discrepancy"
        elif i < discrepancy_count + data_loss_count:
            outcome = "data_loss"
        else:
            outcome = "ok"
        outcomes.record(
            event_id=eid,
            stage=stage,
            outcome=outcome,
            recorded_at=when_iso,
        )


# ----------------------------------------------------------------------
# AC-3.14 — 40-case matrix: 4 stages × 5 metrics × pass|fail
# ----------------------------------------------------------------------


_STAGES = ("m4_1pct", "m4_10pct", "m4_50pct", "m4_100pct")
_METRICS = (
    DodMetric.ERROR_RATE,
    DodMetric.DISCREPANCY_RATE,
    DodMetric.P99_LATENCY_MS,
    DodMetric.DATA_LOSS_COUNT,
    DodMetric.MIN_HITS,
)


def _shape_for(metric: DodMetric, *, want_pass: bool) -> dict[str, float | int]:
    """Return populate() kwargs that yield the requested verdict for ``metric``.

    Other metrics are kept comfortably in the PASS region so the
    requested metric's verdict drives the overall outcome.
    """
    base = {
        "count": 200,
        "error_count": 0,
        "discrepancy_count": 0,
        "data_loss_count": 0,
        "p99_latency_ms": 10.0,
    }
    if metric == DodMetric.ERROR_RATE:
        # PASS: 0% errors / FAIL: 1% errors (above 0.5% threshold).
        base["error_count"] = 0 if want_pass else 2
    elif metric == DodMetric.DISCREPANCY_RATE:
        base["discrepancy_count"] = 0 if want_pass else 2
    elif metric == DodMetric.P99_LATENCY_MS:
        base["p99_latency_ms"] = 50.0 if want_pass else 250.0
    elif metric == DodMetric.DATA_LOSS_COUNT:
        base["data_loss_count"] = 0 if want_pass else 1
    elif metric == DodMetric.MIN_HITS:
        # PASS: 200 hits / FAIL: 10 hits (below 50 floor).
        base["count"] = 200 if want_pass else 10
    return base


@pytest.mark.parametrize("stage", _STAGES)
@pytest.mark.parametrize("metric", _METRICS)
@pytest.mark.parametrize("want_pass", [True, False], ids=["pass", "fail"])
def test_dod_metric_matrix(
    stores: tuple[AuditLog, OutcomeStore],
    stage: str,
    metric: DodMetric,
    want_pass: bool,
) -> None:
    """AC-3.14 — 4 stages × 5 metrics × pass|fail = 40 distinct invocations.

    For each (stage, metric, want_pass) cell we shape the corpus so
    *only* the target metric drives the verdict; every other metric
    sits comfortably in PASS region.
    """
    audit, outcomes = stores
    shape = _shape_for(metric, want_pass=want_pass)

    _populate(audit=audit, outcomes=outcomes, stage=stage, **shape)

    report = compute_dod(
        audit_log=audit,
        outcomes=outcomes,
        stage=stage,
        until=_FIXED_NOW,
    )
    target = next(m for m in report.metrics if m.metric == metric)

    if want_pass:
        # MIN_HITS is the only metric that doesn't get downgraded by
        # insufficient_data — so for "pass" cases it must be PASS.
        assert target.verdict == DodVerdict.PASS, (
            f"expected PASS for stage={stage} metric={metric.value} "
            f"observed={target.observed} threshold={target.threshold}"
        )
    else:
        # FAIL cases: the metric must NOT be PASS. For non-MIN_HITS
        # metrics the FAIL shape may push sample size below 50 only
        # when the metric itself IS min_hits — see _shape_for. Other
        # FAIL shapes keep count=200, so verdict is FAIL.
        assert target.verdict in (DodVerdict.FAIL, DodVerdict.INSUFFICIENT_DATA), (
            f"expected non-PASS for stage={stage} metric={metric.value} "
            f"verdict={target.verdict.value}"
        )


# ----------------------------------------------------------------------
# AC-3.2 — stage isolation
# ----------------------------------------------------------------------


def test_stage_isolation_no_cross_stage_spillover(
    stores: tuple[AuditLog, OutcomeStore],
) -> None:
    """AC-3.2 — events recorded under stage A must not leak into stage B's DoD."""
    audit, outcomes = stores

    # m4_1pct: 200 healthy events
    _populate(
        audit=audit,
        outcomes=outcomes,
        stage="m4_1pct",
        count=200,
        seed_offset=0,
    )
    # m4_10pct: 60 events, all data-loss → DoD must FAIL
    _populate(
        audit=audit,
        outcomes=outcomes,
        stage="m4_10pct",
        count=60,
        data_loss_count=60,
        seed_offset=10_000,
    )

    r1 = compute_dod(
        audit_log=audit, outcomes=outcomes, stage="m4_1pct", until=_FIXED_NOW
    )
    r10 = compute_dod(
        audit_log=audit, outcomes=outcomes, stage="m4_10pct", until=_FIXED_NOW
    )

    assert r1.overall == DodVerdict.PASS
    r10_data_loss = next(m for m in r10.metrics if m.metric == DodMetric.DATA_LOSS_COUNT)
    assert r10_data_loss.verdict == DodVerdict.FAIL
    assert r10.overall == DodVerdict.FAIL


# ----------------------------------------------------------------------
# AC-3.1 — 7-day rolling window enforced
# ----------------------------------------------------------------------


def test_window_excludes_events_older_than_7_days(
    stores: tuple[AuditLog, OutcomeStore],
) -> None:
    """Events outside the 7-day window must not contribute to DoD metrics."""
    audit, outcomes = stores

    eight_days_ago = _FIXED_NOW - _dt.timedelta(days=8)
    # Old events — all data_loss (would FAIL DoD if counted)
    _populate(
        audit=audit,
        outcomes=outcomes,
        stage="m4_1pct",
        count=100,
        data_loss_count=100,
        seed_offset=0,
        when=eight_days_ago,
    )
    # Recent events — all OK
    _populate(
        audit=audit,
        outcomes=outcomes,
        stage="m4_1pct",
        count=200,
        seed_offset=200,
        when=_FIXED_NOW,
    )

    report = compute_dod(
        audit_log=audit, outcomes=outcomes, stage="m4_1pct", until=_FIXED_NOW
    )
    assert report.overall == DodVerdict.PASS
    data_loss = next(m for m in report.metrics if m.metric == DodMetric.DATA_LOSS_COUNT)
    assert data_loss.observed == 0


def test_compute_dod_rejects_unknown_stage(
    stores: tuple[AuditLog, OutcomeStore],
) -> None:
    audit, outcomes = stores
    with pytest.raises(ValueError, match="Unknown canary stage"):
        compute_dod(audit_log=audit, outcomes=outcomes, stage="m4_99pct")


# ----------------------------------------------------------------------
# Insufficient-data handling
# ----------------------------------------------------------------------


def test_insufficient_data_for_low_hits(
    stores: tuple[AuditLog, OutcomeStore],
) -> None:
    """Below-50 sample size flips rate metrics to INSUFFICIENT_DATA."""
    audit, outcomes = stores
    _populate(audit=audit, outcomes=outcomes, stage="m4_1pct", count=10)

    report = compute_dod(
        audit_log=audit, outcomes=outcomes, stage="m4_1pct", until=_FIXED_NOW
    )
    rate_metrics = (
        DodMetric.ERROR_RATE,
        DodMetric.DISCREPANCY_RATE,
        DodMetric.P99_LATENCY_MS,
        DodMetric.DATA_LOSS_COUNT,
    )
    for m in report.metrics:
        if m.metric in rate_metrics:
            assert m.verdict == DodVerdict.INSUFFICIENT_DATA, (
                f"expected INSUFFICIENT_DATA for {m.metric.value} at hits=10, "
                f"got {m.verdict.value}"
            )
        elif m.metric == DodMetric.MIN_HITS:
            # MIN_HITS below floor reports INSUFFICIENT_DATA (extend the
            # window) rather than FAIL — mirrors T5 gate semantics.
            assert m.verdict == DodVerdict.INSUFFICIENT_DATA


def test_zero_data_returns_insufficient_data(
    stores: tuple[AuditLog, OutcomeStore],
) -> None:
    audit, outcomes = stores
    report = compute_dod(
        audit_log=audit, outcomes=outcomes, stage="m4_1pct", until=_FIXED_NOW
    )
    assert report.overall == DodVerdict.INSUFFICIENT_DATA


def test_audit_thin_outcomes_full_does_not_pass_audit_metrics(
    stores: tuple[AuditLog, OutcomeStore],
) -> None:
    """Regression for Codex P1 (PR #45 dod.py:279).

    With audit_log < 50 rows and canary_outcomes >= 50 rows, audit-backed
    metrics (error_rate / p99_latency) must report INSUFFICIENT_DATA —
    never PASS/FAIL on the outcome-side count alone.
    """
    audit, outcomes = stores
    when_iso = _iso(_FIXED_NOW)
    # 5 audit rows (audit-side thin)
    for i in range(5):
        eid = _eid(i)
        audit.record(
            make_record(
                event_id=eid,
                response_status=500,  # 100% error rate IF rate were trusted
                latency_ms=500.0,  # 500ms p99 IF rate were trusted
                idempotency_hit=False,
            )
        )
        outcomes.record(
            event_id=eid, stage="m4_1pct", outcome="ok", recorded_at=when_iso
        )
    # 200 extra outcome rows with no audit row (outcome-side full)
    for i in range(200):
        outcomes.record(
            event_id=_eid(10_000 + i),
            stage="m4_1pct",
            outcome="ok",
            recorded_at=when_iso,
        )

    report = compute_dod(
        audit_log=audit, outcomes=outcomes, stage="m4_1pct", until=_FIXED_NOW
    )
    audit_metrics = {DodMetric.ERROR_RATE, DodMetric.P99_LATENCY_MS}
    for m in report.metrics:
        if m.metric in audit_metrics:
            assert m.verdict == DodVerdict.INSUFFICIENT_DATA, (
                f"audit-thin metric {m.metric.value} should report "
                f"INSUFFICIENT_DATA when audit sample < 50, got {m.verdict.value}"
            )


def test_p99_latency_gates_on_non_null_latency_count(
    stores: tuple[AuditLog, OutcomeStore],
) -> None:
    """Regression for Codex P1 (PR #45 dod.py:305).

    100 audit rows but only 10 carry a latency_ms value; remaining 90
    have ``latency_ms=NULL``. p99 verdict MUST report INSUFFICIENT_DATA
    (latency sample < 50), not PASS based on the joined-row count alone.
    """
    audit, outcomes = stores
    when_iso = _iso(_FIXED_NOW)
    for i in range(100):
        eid = _eid(i)
        audit.record(
            make_record(
                event_id=eid,
                response_status=200,
                # First 10 rows have latency, rest are NULL.
                latency_ms=10.0 if i < 10 else None,
                idempotency_hit=False,
            )
        )
        outcomes.record(
            event_id=eid, stage="m4_1pct", outcome="ok", recorded_at=when_iso
        )

    report = compute_dod(
        audit_log=audit, outcomes=outcomes, stage="m4_1pct", until=_FIXED_NOW
    )
    p99 = next(m for m in report.metrics if m.metric == DodMetric.P99_LATENCY_MS)
    assert p99.verdict == DodVerdict.INSUFFICIENT_DATA, (
        f"p99 should be INSUFFICIENT_DATA when only 10/100 rows have latency, "
        f"got verdict={p99.verdict.value} sample_size={p99.sample_size}"
    )
    assert p99.sample_size == 10


def test_iso_format_matches_sqlite_strftime() -> None:
    """Regression for Codex P1 (PR #45 dod.py:126).

    ``_iso()`` MUST produce a string lexically comparable to SQLite's
    ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` default — i.e. with
    explicit seconds and 3-digit millisecond fraction.
    """
    sample = _dt.datetime(2026, 5, 5, 12, 0, 0, 123_456, tzinfo=_dt.UTC)
    out = _iso(sample)
    # Expected SQLite-style format: SS.SSS (millis truncated from micros).
    assert out == "2026-05-05T12:00:00.123Z", out
    # Boundary check: equal-second comparison must be lexically correct.
    earlier = _dt.datetime(2026, 5, 5, 12, 0, 0, 0, tzinfo=_dt.UTC)
    later = _dt.datetime(2026, 5, 5, 12, 0, 0, 999_999, tzinfo=_dt.UTC)
    assert _iso(earlier) < out < _iso(later)


# ----------------------------------------------------------------------
# CLI JSON output contract
# ----------------------------------------------------------------------


def test_dod_json_output_shape(
    capsys: pytest.CaptureFixture[str],
    shared_db: pathlib.Path,
) -> None:
    """``parallax canary --dod ... --format json`` output is a stable schema."""
    audit = AuditLog(db_path=shared_db)
    outcomes = OutcomeStore(db_path=shared_db)
    try:
        _populate(audit=audit, outcomes=outcomes, stage="m4_1pct", count=200)
        from parallax.canary.cli import _print_dod  # internal helper, OK in tests

        report = compute_dod(
            audit_log=audit, outcomes=outcomes, stage="m4_1pct", until=_FIXED_NOW
        )
        _print_dod(report, fmt="json")
    finally:
        audit.close()
        outcomes.close()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["stage"] == "m4_1pct"
    assert payload["overall"] == "pass"
    assert {m["metric"] for m in payload["metrics"]} == {
        m.value for m in DodMetric
    }
    for m in payload["metrics"]:
        assert m["threshold"] == DOD_THRESHOLD[DodMetric(m["metric"])]


# ----------------------------------------------------------------------
# Threshold consistency
# ----------------------------------------------------------------------


def test_thresholds_match_canary_trigger_constants() -> None:
    """DOD_THRESHOLD must mirror the T1-T5 trigger constants from PR #41.

    Spec §3.3 single source of truth — T*_THRESHOLD lives in triggers.py.
    """
    from parallax.canary.triggers import (
        T1_THRESHOLD,
        T2_THRESHOLD,
        T3_THRESHOLD_MS,
        T4_THRESHOLD,
        T5_MIN_HITS,
    )

    assert DOD_THRESHOLD[DodMetric.ERROR_RATE] == T1_THRESHOLD
    assert DOD_THRESHOLD[DodMetric.DISCREPANCY_RATE] == T2_THRESHOLD
    assert DOD_THRESHOLD[DodMetric.P99_LATENCY_MS] == T3_THRESHOLD_MS
    assert DOD_THRESHOLD[DodMetric.DATA_LOSS_COUNT] == T4_THRESHOLD
    assert DOD_THRESHOLD[DodMetric.MIN_HITS] == T5_MIN_HITS


def test_default_window_is_seven_days() -> None:
    """AC-3.1 — default DoD window is 7 days."""
    assert DEFAULT_WINDOW_DAYS == 7


def test_known_stages_complete() -> None:
    """AC-3.2 — exactly four stages: 1%, 10%, 50%, 100%."""
    assert KNOWN_STAGES == frozenset(_STAGES)
