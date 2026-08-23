"""US-009.3 §5 acceptance tests for :mod:`parallax.canary.drill`.

AC-3.15 mandates ≥ 6 drill tests covering 3 scenarios at ≥ 2 each:

* drain (≥ 2): success path + force-cut path
* re-emit (≥ 2): dry-run sandbox + real-AuditLog idempotency check
* idempotency (≥ 2): single-call invariant + concurrent-racer invariant

The test count below covers each scenario at 2-3 tests, satisfying
AC-3.15 with margin.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest

from parallax.canary.audit_log import AuditLog
from parallax.canary.drill import (
    DEFAULT_DRAIN_TIMEOUT_S,
    DrillStatus,
    run_drain_drill,
    run_full_drill,
    run_idempotency_drill,
    run_reemit_drill,
)


@pytest.fixture
def shared_db(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "drill_audit.db"


@pytest.fixture
def audit_log(shared_db: pathlib.Path) -> Iterator[AuditLog]:
    log = AuditLog(db_path=shared_db)
    try:
        yield log
    finally:
        log.close()


# ----------------------------------------------------------------------
# Drain drill (≥ 2)
# ----------------------------------------------------------------------


def test_drain_drill_passes_under_deadline() -> None:
    """8 in-flight requests drain comfortably under the 60s default timeout."""
    report = run_drain_drill(in_flight_count=8, dry_run=True)
    assert report.drill == "drain"
    assert report.overall == DrillStatus.PASS
    assert {s.name for s in report.steps} == {
        "register_in_flight",
        "drain_within_deadline",
        "no_replay",
    }
    drain_step = next(s for s in report.steps if s.name == "drain_within_deadline")
    assert drain_step.observations["residual_count"] == 0
    assert drain_step.observations["drained_count"] == 8


def test_drain_drill_force_cut_when_deadline_misses() -> None:
    """A timeout of 0s forces the drain loop to exit before completing.

    With ``in_flight_count > 0`` and ``timeout_s=0.0``, the deadline is
    breached on the first loop iteration; residual_count > 0 ⇒ FAIL.
    """
    report = run_drain_drill(in_flight_count=4, timeout_s=0.0, dry_run=True)
    assert report.overall == DrillStatus.FAIL
    drain_step = next(s for s in report.steps if s.name == "drain_within_deadline")
    assert drain_step.status == DrillStatus.FAIL
    assert int(drain_step.observations["residual_count"]) > 0


def test_drain_drill_small_positive_timeout_still_drains() -> None:
    """A small but non-zero budget must still drain normally.

    Companion to the force-cut test above: the deadline comparison is ">="
    so that timeout_s=0.0 cuts immediately, and this pins that the change
    did not turn every short timeout into a cut. 4 dry-run requests cost
    ~4 ms of simulated work against a 200 ms budget, so the loop must
    complete. This also only means anything because the drain clock is
    perf_counter: under the 15.625 ms Windows monotonic tick a 200 ms
    budget is barely 12 observable ticks and elapsed_ms quantises to 0.
    """
    report = run_drain_drill(in_flight_count=4, timeout_s=0.2, dry_run=True)
    assert report.overall == DrillStatus.PASS
    drain_step = next(s for s in report.steps if s.name == "drain_within_deadline")
    assert drain_step.status == DrillStatus.PASS
    assert int(drain_step.observations["drained_count"]) == 4
    assert int(drain_step.observations["residual_count"]) == 0


def test_drain_drill_zero_in_flight_is_invalid() -> None:
    """``in_flight_count=0`` is meaningless — drill must FAIL fast."""
    report = run_drain_drill(in_flight_count=0, dry_run=True)
    assert report.overall == DrillStatus.FAIL


def test_drain_default_timeout_is_60s() -> None:
    """AC-3.15 references 60s timeout for the readiness smoke."""
    assert DEFAULT_DRAIN_TIMEOUT_S == 60.0


# ----------------------------------------------------------------------
# Re-emit drill (≥ 2)
# ----------------------------------------------------------------------


def test_reemit_dry_run_sandbox_passes_without_audit_log() -> None:
    """Dry-run mode without an audit_log exercises only the API contract."""
    report = run_reemit_drill(reemit_count=5, dry_run=True)
    assert report.drill == "reemit"
    assert report.overall == DrillStatus.PASS
    assert report.dry_run is True


def test_reemit_against_real_audit_log_is_idempotent(audit_log: AuditLog) -> None:
    """Real path: 5 events written twice → still 5 distinct rows."""
    report = run_reemit_drill(audit_log=audit_log, reemit_count=5, dry_run=False)
    assert report.overall == DrillStatus.PASS
    idempotent_step = next(s for s in report.steps if s.name == "reemit_idempotent")
    assert int(idempotent_step.observations["audit_rows"]) == 5
    assert int(idempotent_step.observations["unique_event_ids"]) == 5


def test_reemit_zero_count_invalid() -> None:
    report = run_reemit_drill(reemit_count=0, dry_run=True)
    assert report.overall == DrillStatus.FAIL


# ----------------------------------------------------------------------
# Idempotency drill (≥ 2)
# ----------------------------------------------------------------------


def test_idempotency_drill_single_worker_invocation() -> None:
    """8 concurrent racers on one event_id → exactly one worker invocation."""
    report = run_idempotency_drill(concurrency=8, dry_run=True)
    assert report.drill == "idempotency"
    assert report.overall == DrillStatus.PASS
    inv_step = next(
        s for s in report.steps if s.name == "single_worker_invocation"
    )
    assert int(inv_step.observations["invocations"]) == 1


def test_idempotency_drill_higher_concurrency_still_serialises() -> None:
    """Pump concurrency to 16 — the per-event lock still admits one worker."""
    report = run_idempotency_drill(concurrency=16, dry_run=True)
    assert report.overall == DrillStatus.PASS
    inv_step = next(
        s for s in report.steps if s.name == "single_worker_invocation"
    )
    assert int(inv_step.observations["invocations"]) == 1


def test_idempotency_drill_concurrency_one_invalid() -> None:
    """concurrency=1 is meaningless (no race) — drill MUST FAIL."""
    report = run_idempotency_drill(concurrency=1, dry_run=True)
    assert report.overall == DrillStatus.FAIL


# ----------------------------------------------------------------------
# Full-drill orchestrator
# ----------------------------------------------------------------------


def test_run_full_drill_returns_three_reports() -> None:
    drain, reemit, idem = run_full_drill(
        in_flight_count=4,
        reemit_count=3,
        concurrency=4,
        dry_run=True,
    )
    assert drain.drill == "drain"
    assert reemit.drill == "reemit"
    assert idem.drill == "idempotency"
    assert all(r.overall == DrillStatus.PASS for r in (drain, reemit, idem))


def test_run_full_drill_with_real_audit_log_passes(audit_log: AuditLog) -> None:
    drain, reemit, idem = run_full_drill(
        audit_log=audit_log,
        in_flight_count=4,
        reemit_count=3,
        concurrency=4,
        dry_run=False,
    )
    assert all(r.overall == DrillStatus.PASS for r in (drain, reemit, idem))


# ----------------------------------------------------------------------
# CLI gating regression (Codex P1 on PR #45 cli.py:230)
# ----------------------------------------------------------------------


def test_cli_rollback_drill_real_mode_uses_audit_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Regression: ``--rollback-drill`` without ``--dry-run`` MUST hit a real
    AuditLog, even when ``--audit-db`` is omitted.

    Old code only constructed AuditLog when ``--audit-db`` was explicit,
    so default-path real-mode drills silently fell back to the no-audit
    dry-run branch and reported PASS without exercising the production
    store.
    """
    from parallax.canary import cli as canary_cli

    db_path = tmp_path / "drill_audit.db"
    monkeypatch.setenv("PARALLAX_CANARY_AUDIT_DB", str(db_path))

    constructed: list[pathlib.Path] = []
    real_init = canary_cli.AuditLog.__init__

    def spy_init(self: object, db_path: object = None, **kw: object) -> None:
        constructed.append(pathlib.Path(str(db_path)) if db_path else db_path)  # type: ignore[arg-type]
        real_init(self, db_path=db_path, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(canary_cli.AuditLog, "__init__", spy_init)

    parser = __import__(
        "parallax.cli", fromlist=["build_parser"]
    ).build_parser()
    args = parser.parse_args(
        [
            "canary",
            "--rollback-drill",
            "--in-flight", "2",
            "--reemit-count", "2",
            "--concurrency", "2",
        ]
    )
    rc = canary_cli.cmd_canary(args)
    assert rc == 0
    # AuditLog was constructed at least once in real mode (no --dry-run).
    assert len(constructed) >= 1, (
        "expected real-mode rollback-drill to construct AuditLog; got 0 calls"
    )
