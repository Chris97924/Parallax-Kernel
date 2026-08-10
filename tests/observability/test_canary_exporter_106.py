"""#106.1 — the canary T1-T5 exporter, asserted on VALUES rather than presence.

"The series appear" is the assertion that would have passed on the broken tree
if anyone had written it: every one of these families had a consumer, a name,
and no producer, and a test that only checked names would have gone green the
moment an empty collector was registered. So each test below pins a number that
can only be right if the exporter read the right column of the right table.

The fixture is a real ``parallax canary`` invocation through the real argument
parser writing into a temp store, plus direct
:class:`~parallax.canary.instrument.CanaryRequestRecorder` spans where a test
needs a latency it can do arithmetic on. Nothing stubs the store.

What each test would catch:

* wrong denominator          — events counted from audit_log instead of the
                               outcome ledger, so ACK rows inflate T5
* wrong error definition     — 4xx counted as canary errors, so a bad client
                               triggers an auto-rollback
* fabricated latency         — a histogram that reports the buckets' shape but
                               not the observations' values
* static zeros               — an exporter registered but never fed
* label drift                — stage/outcome collapsed or misspelled, which
                               silently unbinds every alert selector
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families

from parallax.canary.audit_log import AUDIT_DB_ENV, AuditLog, make_record
from parallax.canary.cli import cmd_canary, register_canary_subparser
from parallax.canary.exporter import (
    CANARY_DURATION_BUCKETS_MS,
    ERROR_STATUS_FLOOR,
    collect_canary_snapshot,
)
from parallax.canary.instrument import CanaryRequestRecorder
from parallax.canary.outcomes import KNOWN_OUTCOMES, KNOWN_STAGES, OutcomeStore
from parallax.canary.rollback_state import RollbackStateStore
from parallax.server.routes.metrics import _build_payload, _reset_cache_for_tests

_STAGE = "m4_1pct"


@pytest.fixture
def store_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp canary DB that both the CLI writer and the exporter resolve to."""
    path = tmp_path / "canary_audit.db"
    monkeypatch.setenv(AUDIT_DB_ENV, str(path))
    _reset_cache_for_tests()
    yield path
    _reset_cache_for_tests()


def _scrape() -> dict[str, float]:
    """``{sample_name{sorted labels} -> value}`` for every parallax_canary_* sample."""
    _reset_cache_for_tests()
    out: dict[str, float] = {}
    for family in text_string_to_metric_families(_build_payload()):
        if not family.name.startswith("parallax_canary_"):
            continue
        for sample in family.samples:
            labels = ",".join(f"{k}={v}" for k, v in sorted(sample.labels.items()))
            out[f"{sample.name}{{{labels}}}" if labels else sample.name] = sample.value
    return out


def _run_reemit_cli(store: Path, *, stage: str | None, count: int) -> int:
    """Invoke ``parallax canary --orbit-reemit-test`` through the real parser."""
    parser = argparse.ArgumentParser(prog="parallax")
    register_canary_subparser(parser.add_subparsers(dest="command", required=True))
    argv = [
        "canary",
        "--orbit-reemit-test",
        "--audit-db",
        str(store),
        "--reemit-count",
        str(count),
    ]
    if stage is not None:
        argv += ["--stage", stage]
    return cmd_canary(parser.parse_args(argv))


def _record(
    store: Path,
    *,
    outcome: str,
    status: int = 200,
    stage: str = _STAGE,
    latency_ms: float | None = None,
) -> str:
    """Write one canary event. ``latency_ms`` pins the duration for bucket maths."""
    audit = AuditLog(db_path=store)
    outcomes = OutcomeStore(db_path=store)
    try:
        recorder = CanaryRequestRecorder(audit_log=audit, outcome_store=outcomes, stage=stage)
        with recorder.request() as span:
            span.outcome = outcome
            span.response_status = status
        if latency_ms is not None:
            # Overwrite the measured duration with a pinned one so the expected
            # histogram is arithmetic rather than a re-measurement. The recorder
            # still wrote the outcome row, so the join the exporter does is the
            # same one production takes.
            audit.record(
                make_record(
                    event_id=span.event_id,
                    response_status=status,
                    latency_ms=latency_ms,
                    idempotency_hit=False,
                )
            )
        return span.event_id
    finally:
        audit.close()
        outcomes.close()


# ---------------------------------------------------------------------------
# End-to-end: a real CLI invocation becomes real series
# ---------------------------------------------------------------------------


def test_a_canary_cli_run_produces_every_t1_t5_family_with_exact_values(
    store_path: Path,
) -> None:
    """THE ACCEPTANCE TEST. Five clean events in, five events measured out.

    Every number here is derivable from the fixture alone: five ``ok`` outcomes
    at stage m4_1pct, no errors, no discrepancies, no data loss, and a T3
    histogram whose count is five. An exporter that emitted the right names with
    static zeros fails on the first assertion.
    """
    assert _run_reemit_cli(store_path, stage=_STAGE, count=5) == 0

    scraped = _scrape()

    # T1/T2/T5 denominator — five ok events on one stage, and nothing else.
    assert scraped[f"parallax_canary_events_total{{outcome=ok,stage={_STAGE}}}"] == 5.0
    assert scraped[f"parallax_canary_events_total{{outcome=discrepancy,stage={_STAGE}}}"] == 0.0
    # T1 numerator — the drill emits 200s, so zero errors.
    assert scraped[f"parallax_canary_event_errors_total{{stage={_STAGE}}}"] == 0.0
    # T2 numerator.
    assert scraped[f"parallax_canary_discrepancy_total{{stage={_STAGE}}}"] == 0.0
    # T4 — the critical data-loss alert's series, now able to read zero rather
    # than nothing.
    assert scraped[f"parallax_canary_data_loss_events_total{{stage={_STAGE}}}"] == 0.0
    # T3 — five measured durations reached the histogram.
    assert scraped[f"parallax_canary_request_duration_ms_count{{stage={_STAGE}}}"] == 5.0
    # T5 gate reads the same rows the store holds.
    assert collect_canary_snapshot(store_path).total_events == 5

    assert scraped["parallax_canary_store_present"] == 1.0


def test_the_cli_persists_a_measured_duration_not_a_constant(store_path: Path) -> None:
    """T3 needed new instrumentation, and this is what "new" has to mean.

    The re-emit drill used to stamp ``latency_ms=10.0`` on every row. A
    histogram over that is a spike at a constant with a p99 that cannot move —
    it would satisfy "the series exists" and tell an operator nothing. The
    persisted values must be real measurements: positive, and not all identical
    to the old placeholder.
    """
    assert _run_reemit_cli(store_path, stage=_STAGE, count=5) == 0

    durations = collect_canary_snapshot(store_path).durations_ms[_STAGE]

    assert len(durations) == 5
    assert all(d > 0 for d in durations), f"non-positive measured duration: {durations}"
    assert not all(d == 10.0 for d in durations), (
        "every duration is the old hardcoded 10.0 — the drill is still stamping, "
        f"not measuring: {durations}"
    )


def test_a_cli_run_without_a_stage_writes_no_outcomes(store_path: Path) -> None:
    """Backwards compatibility: the drill without --stage is still just a drill.

    Existing invocations must not start producing canary observations as a side
    effect — a drill's synthetic events counted as canary traffic would corrupt
    the T5 sample size a promotion decision is made on.
    """
    assert _run_reemit_cli(store_path, stage=None, count=5) == 0

    assert collect_canary_snapshot(store_path).total_events == 0


# ---------------------------------------------------------------------------
# Value-level: each series reads the field it claims to
# ---------------------------------------------------------------------------


def test_t3_histogram_buckets_sum_and_count_match_the_stored_durations(
    store_path: Path,
) -> None:
    """The strongest assertion in the file: exact buckets from known values.

    Durations 5 / 40 / 120 ms straddle the T3 gate at 100. Every bucket count
    below is the number of observations at or under that bound, the sum is the
    arithmetic total, and +Inf equals the count. A histogram that fabricated its
    distribution, used seconds instead of milliseconds, or read a different
    column cannot satisfy all of them at once.
    """
    for latency in (5.0, 40.0, 120.0):
        _record(store_path, outcome="ok", latency_ms=latency)

    scraped = _scrape()
    prefix = "parallax_canary_request_duration_ms"

    assert scraped[f"{prefix}_count{{stage={_STAGE}}}"] == 3.0
    assert scraped[f"{prefix}_sum{{stage={_STAGE}}}"] == pytest.approx(165.0)

    expected_cumulative = {
        bound: float(sum(1 for v in (5.0, 40.0, 120.0) if v <= bound))
        for bound in CANARY_DURATION_BUCKETS_MS
    }
    for bound, expected in expected_cumulative.items():
        key = f"{prefix}_bucket{{le={bound},stage={_STAGE}}}"
        assert scraped[key] == expected, f"le={bound} should count {expected}"

    assert scraped[f"{prefix}_bucket{{le=+Inf,stage={_STAGE}}}"] == 3.0
    # The gate itself: two of three requests are under the 100ms T3 threshold.
    assert scraped[f"{prefix}_bucket{{le=100.0,stage={_STAGE}}}"] == 2.0


def test_errors_come_from_the_response_status_not_the_outcome(store_path: Path) -> None:
    """T1 measures the HTTP envelope; T2/T4 measure the business verdict.

    Reading the error count off ``outcome`` would double-count a discrepancy as
    an error and miss a 500 that returned a correct-looking payload. Fixture: one
    clean 200, one 500 whose outcome is still ``ok``, one ``discrepancy`` at 200.
    """
    _record(store_path, outcome="ok", status=200)
    _record(store_path, outcome="ok", status=500)
    _record(store_path, outcome="discrepancy", status=200)

    scraped = _scrape()

    assert scraped[f"parallax_canary_event_errors_total{{stage={_STAGE}}}"] == 1.0
    assert scraped[f"parallax_canary_discrepancy_total{{stage={_STAGE}}}"] == 1.0
    assert scraped[f"parallax_canary_events_total{{outcome=ok,stage={_STAGE}}}"] == 2.0
    assert scraped[f"parallax_canary_events_total{{outcome=discrepancy,stage={_STAGE}}}"] == 1.0


def test_a_client_error_is_not_a_canary_error(store_path: Path) -> None:
    """4xx must not trip T1 — it is the caller being wrong, not the canary.

    T1 is an auto-rollback gate; counting client errors would roll a deploy back
    because someone sent a malformed request.
    """
    _record(store_path, outcome="ok", status=404)
    _record(store_path, outcome="ok", status=ERROR_STATUS_FLOOR - 1)

    scraped = _scrape()

    assert scraped[f"parallax_canary_event_errors_total{{stage={_STAGE}}}"] == 0.0
    assert scraped[f"parallax_canary_events_total{{outcome=ok,stage={_STAGE}}}"] == 2.0


def test_data_loss_reaches_the_critical_t4_series(store_path: Path) -> None:
    """The sharpest end of #106: a hard-rollback alert that had never fired.

    ``increase(parallax_canary_data_loss_events_total[1h]) > 0`` was evaluating
    no-data since it was written, so its silence and "no data was lost" were the
    same observation.
    """
    _record(store_path, outcome="data_loss")
    _record(store_path, outcome="ok")

    scraped = _scrape()

    assert scraped[f"parallax_canary_data_loss_events_total{{stage={_STAGE}}}"] == 1.0
    assert scraped[f"parallax_canary_events_total{{outcome=data_loss,stage={_STAGE}}}"] == 1.0


def test_stages_are_counted_separately(store_path: Path) -> None:
    """Stage is the promotion unit; collapsing it would gate on the wrong sample."""
    _record(store_path, outcome="ok", stage="m4_1pct")
    _record(store_path, outcome="ok", stage="m4_10pct")
    _record(store_path, outcome="discrepancy", stage="m4_10pct")

    scraped = _scrape()

    assert scraped["parallax_canary_events_total{outcome=ok,stage=m4_1pct}"] == 1.0
    assert scraped["parallax_canary_events_total{outcome=ok,stage=m4_10pct}"] == 1.0
    assert scraped["parallax_canary_discrepancy_total{stage=m4_10pct}"] == 1.0
    # The clean stage reports an explicit zero rather than going absent: T2 is
    # numerator/denominator, and an absent numerator makes the division empty —
    # silent for the same reason the whole #106 class was silent.
    assert scraped["parallax_canary_discrepancy_total{stage=m4_1pct}"] == 0.0


def test_ack_rows_do_not_inflate_the_sample_size(store_path: Path) -> None:
    """The T5 denominator is the outcome ledger, and the join is what enforces it.

    ``AuditLog.record_ack`` writes a row with a synthetic event_id, status 0 and
    no outcome row. Counting audit rows instead of outcomes would let every
    manual ACK look like canary traffic — inflating the sample size that decides
    whether T1-T4 verdicts are trustworthy.
    """
    _record(store_path, outcome="ok")
    audit = AuditLog(db_path=store_path)
    try:
        audit.record_ack("ack-event-1", ack_by="operator")
    finally:
        audit.close()

    scraped = _scrape()

    assert collect_canary_snapshot(store_path).total_events == 1
    assert scraped[f"parallax_canary_events_total{{outcome=ok,stage={_STAGE}}}"] == 1.0
    assert scraped[f"parallax_canary_event_errors_total{{stage={_STAGE}}}"] == 0.0


# ---------------------------------------------------------------------------
# Rollback state — the dashboard-only series
# ---------------------------------------------------------------------------


def test_rollback_state_is_unknown_before_a_controller_has_written(
    store_path: Path,
) -> None:
    """-1, not 0. Zero means "running", which is a claim nothing has made yet."""
    _record(store_path, outcome="ok")

    assert _scrape()["parallax_canary_rollback_state"] == -1.0


@pytest.mark.parametrize(
    ("state", "expected"),
    [("running", 0.0), ("tripped", 1.0), ("awaiting_ack", 2.0)],
)
def test_rollback_state_reflects_the_durable_row(
    store_path: Path, state: str, expected: float
) -> None:
    """Each controller state has to reach the panel with its own value."""
    _record(store_path, outcome="ok")
    store = RollbackStateStore(store_path)
    try:
        store.record(state, tripped_by="T1")
    finally:
        store.close()

    assert _scrape()["parallax_canary_rollback_state"] == expected


def test_a_tripping_controller_persists_its_state(store_path: Path) -> None:
    """The controller is the producer; this is the wiring, not the store.

    Drives a real T1 breach through RollbackController and reads the value off
    the scrape — the whole cross-process path the dashboard panel depends on.
    """
    from parallax.canary.rollback import RollbackController

    audit = AuditLog(db_path=store_path)
    try:
        controller = RollbackController(audit_log=audit)
        assert _scrape()["parallax_canary_rollback_state"] == 0.0  # running

        for i in range(100):
            controller.observe_request(is_error=True, latency_ms=1.0, ts=float(i))
        controller.evaluate(now=100.0)
    finally:
        audit.close()

    assert _scrape()["parallax_canary_rollback_state"] == 1.0  # tripped


# ---------------------------------------------------------------------------
# The no-store case
# ---------------------------------------------------------------------------


def test_an_absent_store_still_exports_every_family_at_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-data is what made these alerts inert; zeros are what make them evaluate.

    A server that has never run a canary must still put the families on the
    wire, or ``increase(...) > 0`` goes back to being silent for the same reason
    a healthy canary is.
    """
    monkeypatch.setenv(AUDIT_DB_ENV, str(tmp_path / "nope" / "missing.db"))
    _reset_cache_for_tests()

    scraped = _scrape()

    assert scraped["parallax_canary_store_present"] == 0.0
    for stage in KNOWN_STAGES:
        for outcome in KNOWN_OUTCOMES:
            key = f"parallax_canary_events_total{{outcome={outcome},stage={stage}}}"
            assert scraped[key] == 0.0
        assert scraped[f"parallax_canary_event_errors_total{{stage={stage}}}"] == 0.0
        assert scraped[f"parallax_canary_discrepancy_total{{stage={stage}}}"] == 0.0
        assert scraped[f"parallax_canary_data_loss_events_total{{stage={stage}}}"] == 0.0
        assert scraped[f"parallax_canary_request_duration_ms_count{{stage={stage}}}"] == 0.0
    assert scraped["parallax_canary_rollback_state"] == -1.0


def test_an_audit_only_database_is_not_reported_as_a_readable_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``store_present`` means the OUTCOME LEDGER opened, not that a file did.

    A real-mode drill run without ``--stage`` constructs an AuditLog and no
    OutcomeStore, leaving a database with an ``audit_log`` table and no
    ``canary_outcomes`` at all. Deriving presence from the file opening would
    export zeros next to ``store_present 1``, which the alert annotations tell
    an oncall to read as measurements — so T1-T4 would be trusted over a ledger
    that was never there.
    """
    store = tmp_path / "audit_only.db"
    monkeypatch.setenv(AUDIT_DB_ENV, str(store))
    AuditLog(db_path=store).close()
    _reset_cache_for_tests()

    assert store.exists()
    assert _scrape()["parallax_canary_store_present"] == 0.0
    assert collect_canary_snapshot(store).store_present is False


def test_every_known_stage_has_a_zero_sample_before_its_first_event(
    store_path: Path,
) -> None:
    """A counter that first appears at 1 has no step for ``increase()`` to find.

    T4 is a per-series ``increase(...[1h]) > 0`` with no hysteresis. If
    ``parallax_canary_data_loss_events_total{stage="m4_10pct"}`` only came into
    existence when that stage recorded its first loss, the series would begin at
    1.0 and stay flat, ``increase()`` would return 0, and the FIRST data-loss
    event — the one the critical alert exists for — would read as healthy. The
    fix is a 0 sample for every stage the store can ever hold, from the first
    scrape.

    Fixture: activity on m4_1pct only. The other three stages must still be
    exported, at zero, so a later first event on any of them is a visible step.
    """
    _record(store_path, outcome="ok", stage="m4_1pct")

    scraped = _scrape()

    for stage in KNOWN_STAGES - {"m4_1pct"}:
        assert scraped[f"parallax_canary_data_loss_events_total{{stage={stage}}}"] == 0.0, (
            f"{stage} has no baseline data-loss sample; its first loss would be invisible"
        )
        assert scraped[f"parallax_canary_discrepancy_total{{stage={stage}}}"] == 0.0
        assert scraped[f"parallax_canary_event_errors_total{{stage={stage}}}"] == 0.0
        for outcome in KNOWN_OUTCOMES:
            key = f"parallax_canary_events_total{{outcome={outcome},stage={stage}}}"
            assert scraped[key] == 0.0


def test_a_first_data_loss_event_is_a_step_from_an_existing_zero(
    store_path: Path,
) -> None:
    """The same property stated as the transition an alert has to see.

    Scrape once before the event and once after: the series must exist both
    times, and its value must go 0 -> 1. Equal values, or a series absent from
    the first scrape, is the silent-T4 bug.
    """
    _record(store_path, outcome="ok", stage="m4_10pct")
    key = "parallax_canary_data_loss_events_total{stage=m4_10pct}"

    before = _scrape()[key]
    _record(store_path, outcome="data_loss", stage="m4_10pct")
    after = _scrape()[key]

    assert before == 0.0
    assert after == 1.0, f"data-loss counter did not step: {before} -> {after}"


def test_a_scrape_does_not_create_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only, and provably so.

    A scrape that opened the DB read-write would create an empty one at whatever
    path was configured, and ``store_present`` would then report 1 for a store
    the exporter had just invented.
    """
    missing = tmp_path / "must_not_appear.db"
    monkeypatch.setenv(AUDIT_DB_ENV, str(missing))
    _reset_cache_for_tests()

    _scrape()

    assert not missing.exists()
