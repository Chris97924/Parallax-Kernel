"""US-009.3 §5 — ``parallax canary`` CLI subcommand wiring.

Exposes three CLI flag-groups invoked from :mod:`parallax.cli`:

* ``parallax canary --dod --stage <stage> [--days N] [--prometheus-url URL]``
  Run the per-stage DoD summary for one stage over the trailing window.
  Reads the canary shadow observer's **Prometheus** metrics
  (discrepancy_rate, aphelion_unreachable_rate, sample size) per Option A1
  (2026-06-07); error_rate / p99_latency / data_loss are gated by the
  T1-T5 Prometheus alerts (auto-rollback), not this summary. The SQLite
  ``OutcomeStore`` path is retired for ``--dod`` (nothing populates
  ``canary_outcomes`` in production).
* ``parallax canary --rollback-drill [--dry-run]``
  Run the three-scenario drill (drain + re-emit + idempotency).
* ``parallax canary --drain-test [--timeout SECONDS]``
  Run only the drain drill (preflight smoke).
* ``parallax canary --orbit-reemit-test``
  Run only the re-emit drill.
* ``parallax canary --check-alerting``
  Webhook reachability check (PagerDuty + Slack). Currently a stub
  that reports unconfigured-status; production wiring lands when the
  canary stage driver provides webhook URLs.

The module is import-friendly so :mod:`parallax.cli` can register the
subparser without dragging the canary module side-effects into every
CLI invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Final

from parallax.canary.audit_log import AuditLog
from parallax.canary.dod import (
    DEFAULT_WINDOW_DAYS,
    DodReport,
    DodVerdict,
)
from parallax.canary.dod_prometheus import (
    DEFAULT_PROM_URL,
    PROM_URL_ENV,
    compute_shadow_dod,
)
from parallax.canary.drill import (
    DEFAULT_DRAIN_TIMEOUT_S,
    DrillReport,
    DrillStatus,
    run_drain_drill,
    run_full_drill,
    run_reemit_drill,
)
from parallax.canary.outcomes import KNOWN_STAGES, OutcomeStore

__all__ = ["register_canary_subparser", "cmd_canary"]


def register_canary_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the ``canary`` subcommand to a parent ``parallax`` parser.

    Mutually-exclusive flag group — exactly one of ``--dod``,
    ``--rollback-drill``, ``--drain-test``, ``--orbit-reemit-test``,
    ``--check-alerting`` MUST be selected.
    """
    p_canary = sub.add_parser(
        "canary",
        help="Run M4 canary verification: DoD checks, rollback drills, alerting probe.",
    )
    mode = p_canary.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dod",
        action="store_true",
        help="Run DoD verification for --stage over a 7-day window.",
    )
    mode.add_argument(
        "--rollback-drill",
        action="store_true",
        help="Run drain + re-emit + idempotency drills.",
    )
    mode.add_argument(
        "--drain-test",
        action="store_true",
        help="Run only the drain drill (preflight smoke).",
    )
    mode.add_argument(
        "--orbit-reemit-test",
        action="store_true",
        help="Run only the Orbit re-emit + idempotency drill.",
    )
    mode.add_argument(
        "--check-alerting",
        action="store_true",
        help="Verify PagerDuty + Slack webhook reachability.",
    )

    p_canary.add_argument(
        "--stage",
        choices=sorted(KNOWN_STAGES),
        help=(
            "Canary stage (m4_1pct / m4_10pct / m4_50pct / m4_100pct). Required by "
            "--dod. On the real-mode drills it makes each emitted event a durable "
            "canary observation — measured duration into audit_log.latency_ms and "
            "business outcome into canary_outcomes — which is what the server's "
            "parallax_canary_* T1-T5 exporter reads."
        ),
    )
    p_canary.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"DoD window in days (default: {DEFAULT_WINDOW_DAYS}).",
    )
    p_canary.add_argument(
        "--audit-db",
        type=str,
        default=None,
        help=(
            "Override audit-log SQLite path (defaults to PARALLAX_CANARY_AUDIT_DB). "
            "Used by the rollback drills; --dod no longer reads SQLite."
        ),
    )
    p_canary.add_argument(
        "--prometheus-url",
        type=str,
        default=None,
        help=f"Prometheus base URL for --dod (defaults to ${PROM_URL_ENV} or {DEFAULT_PROM_URL}).",
    )
    p_canary.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_DRAIN_TIMEOUT_S,
        help=f"Drain timeout in seconds (default: {DEFAULT_DRAIN_TIMEOUT_S}).",
    )
    p_canary.add_argument(
        "--in-flight",
        type=int,
        default=8,
        help="Synthetic in-flight request count for drain drill (default: 8).",
    )
    p_canary.add_argument(
        "--reemit-count",
        type=int,
        default=5,
        help="Synthetic re-emit count for reemit drill (default: 5).",
    )
    p_canary.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Idempotency drill thread count (default: 8).",
    )
    p_canary.add_argument(
        "--dry-run",
        action="store_true",
        help="Drill mode: skip real I/O, exercise API contract only.",
    )
    p_canary.add_argument(
        "--format",
        choices=["pretty", "json"],
        default="pretty",
        help="Output format (default: pretty).",
    )


def cmd_canary(args: argparse.Namespace) -> int:
    """Dispatch ``parallax canary`` invocations."""
    if args.dod:
        return _cmd_dod(args)
    if args.rollback_drill:
        return _cmd_rollback_drill(args)
    if args.drain_test:
        return _cmd_drain_test(args)
    if args.orbit_reemit_test:
        return _cmd_reemit_test(args)
    if args.check_alerting:
        return _cmd_check_alerting(args)
    # argparse mutually-exclusive group should prevent this branch.
    print("error: no canary mode selected", file=sys.stderr)
    return 2


# ----------------------------------------------------------------------
# DoD command
# ----------------------------------------------------------------------


def _cmd_dod(args: argparse.Namespace) -> int:
    if not args.stage:
        print("error: --stage is required with --dod", file=sys.stderr)
        return 2

    # Option A1 (2026-06-07): the per-stage --dod summary reads the canary
    # shadow observer's Prometheus metrics, not the SQLite OutcomeStore
    # (which has no production producer). prom_url resolution order:
    # --prometheus-url flag > PARALLAX_PROMETHEUS_URL env > localhost default.
    prom_url = args.prometheus_url or os.environ.get(PROM_URL_ENV) or DEFAULT_PROM_URL

    report = compute_shadow_dod(
        stage=args.stage,
        window_days=args.days,
        prom_url=prom_url,
    )

    _print_dod(report, fmt=args.format)
    return 0 if report.overall == DodVerdict.PASS else 1


# Footer shown under every --dod report: the three metrics this per-stage
# summary does NOT compute are gated elsewhere (Option A1, 2026-06-07).
_DOD_GATE_NOTE: Final[str] = (
    "error_rate / p99_latency / data_loss are gated by the T1-T5 Prometheus "
    "alerts (auto-rollback), not this per-stage summary."
)


def _print_dod(report: DodReport, *, fmt: str) -> None:
    if fmt == "json":
        payload = {
            "stage": report.stage,
            "window_start": report.window_start,
            "window_end": report.window_end,
            "overall": report.overall.value,
            "metrics": [
                {
                    "metric": m.metric.value,
                    "observed": m.observed,
                    "threshold": m.threshold,
                    "verdict": m.verdict.value,
                    "sample_size": m.sample_size,
                }
                for m in report.metrics
            ],
            "note": _DOD_GATE_NOTE,
        }
        print(json.dumps(payload, indent=2))
        return

    print(f"DoD report — stage={report.stage}")
    print(f"  window: {report.window_start}  →  {report.window_end}")
    print(f"  overall: {report.overall.value.upper()}")
    print()
    print(f"  {'metric':<22} {'observed':>14} {'threshold':>14} {'verdict':<20} hits")
    print(f"  {'-' * 22} {'-' * 14} {'-' * 14} {'-' * 20} {'-' * 5}")
    for m in report.metrics:
        print(
            f"  {m.metric.value:<22} {m.observed:>14.6f} {m.threshold:>14.6f} "
            f"{m.verdict.value:<20} {m.sample_size}"
        )
    print()
    print(f"  note: {_DOD_GATE_NOTE}")


# ----------------------------------------------------------------------
# Drill commands
# ----------------------------------------------------------------------


def _open_outcome_store(args: argparse.Namespace) -> OutcomeStore | None:
    """Open the OutcomeStore when a stage was named (#106.1).

    ``--stage`` is what turns a drill into a canary producer: with it, each
    emitted event lands in ``canary_outcomes`` alongside its measured duration
    in ``audit_log``, which is the pair the server-side T1-T5 exporter joins.
    Without it the drills behave exactly as they did before, so no existing
    invocation changes meaning.
    """
    if not getattr(args, "stage", None) or args.dry_run:
        return None
    return OutcomeStore(db_path=args.audit_db)


def _cmd_rollback_drill(args: argparse.Namespace) -> int:
    audit: AuditLog | None = None
    outcomes: OutcomeStore | None = None
    try:
        # Real-mode drills MUST exercise the configured audit store —
        # falling back to None silently routes the drill back into dry-run
        # simulation (run_reemit_drill takes its no-audit branch and
        # run_idempotency_drill spins up an isolated temp DB), defeating
        # the production-path verification this command exists for.
        if not args.dry_run:
            audit = AuditLog(db_path=args.audit_db)
        outcomes = _open_outcome_store(args)
        drain, reemit, idem = run_full_drill(
            audit_log=audit,
            in_flight_count=args.in_flight,
            reemit_count=args.reemit_count,
            concurrency=args.concurrency,
            timeout_s=args.timeout,
            dry_run=args.dry_run,
            outcome_store=outcomes,
            stage=args.stage if outcomes is not None else None,
        )
    finally:
        if audit is not None:
            audit.close()
        if outcomes is not None:
            outcomes.close()

    _print_drill_reports([drain, reemit, idem], fmt=args.format)
    if any(r.overall == DrillStatus.FAIL for r in (drain, reemit, idem)):
        return 1
    return 0


def _cmd_drain_test(args: argparse.Namespace) -> int:
    report = run_drain_drill(
        in_flight_count=args.in_flight,
        timeout_s=args.timeout,
        dry_run=args.dry_run,
    )
    _print_drill_reports([report], fmt=args.format)
    return 0 if report.overall == DrillStatus.PASS else 1


def _cmd_reemit_test(args: argparse.Namespace) -> int:
    audit: AuditLog | None = None
    outcomes: OutcomeStore | None = None
    try:
        # Same gating as _cmd_rollback_drill — real mode must hit the
        # configured audit store, never None (which silently falls back
        # to the no-audit dry-run branch in run_reemit_drill).
        if not args.dry_run:
            audit = AuditLog(db_path=args.audit_db)
        outcomes = _open_outcome_store(args)
        report = run_reemit_drill(
            audit_log=audit,
            reemit_count=args.reemit_count,
            dry_run=args.dry_run,
            outcome_store=outcomes,
            stage=args.stage if outcomes is not None else None,
        )
    finally:
        if audit is not None:
            audit.close()
        if outcomes is not None:
            outcomes.close()
    _print_drill_reports([report], fmt=args.format)
    return 0 if report.overall == DrillStatus.PASS else 1


def _print_drill_reports(reports: Sequence[DrillReport], *, fmt: str) -> None:
    if fmt == "json":
        payload = [
            {
                "drill": r.drill,
                "dry_run": r.dry_run,
                "overall": r.overall,
                "steps": [
                    {
                        "name": s.name,
                        "status": s.status,
                        "detail": s.detail,
                        "observations": s.observations,
                    }
                    for s in r.steps
                ],
            }
            for r in reports
        ]
        print(json.dumps(payload, indent=2))
        return
    for r in reports:
        print(f"Drill: {r.drill}  (dry_run={r.dry_run})  →  {r.overall.upper()}")
        for s in r.steps:
            mark = (
                "✓"
                if s.status == DrillStatus.PASS
                else ("✗" if s.status == DrillStatus.FAIL else "·")
            )
            print(f"  {mark} {s.name}: {s.detail}")
            if s.observations:
                obs_str = ", ".join(f"{k}={v}" for k, v in s.observations.items())
                print(f"      ({obs_str})")
        print()


# ----------------------------------------------------------------------
# Alerting probe (stub — see §6 PR for artifact deliverables)
# ----------------------------------------------------------------------


def _cmd_check_alerting(args: argparse.Namespace) -> int:
    """Verify PagerDuty + Slack webhook URLs are configured.

    This is a *configuration* check only — it does NOT send a real test
    notification (which would page oncall) unless the env var
    ``PARALLAX_CANARY_ALERTING_LIVE_PROBE=1`` is set. In normal CI /
    preflight use, the check inspects environment variables seeded by
    the secret manager and reports their presence.
    """
    pd_url = os.environ.get("PAGERDUTY_M4_CANARY_KEY")
    slack_url = os.environ.get("SLACK_WEBHOOK_M4_CANARY")
    live_probe = os.environ.get("PARALLAX_CANARY_ALERTING_LIVE_PROBE") == "1"

    pd_status = "configured" if pd_url else "missing"
    slack_status = "configured" if slack_url else "missing"

    payload = {
        "pagerduty": {
            "env_var": "PAGERDUTY_M4_CANARY_KEY",
            "status": pd_status,
        },
        "slack": {
            "env_var": "SLACK_WEBHOOK_M4_CANARY",
            "status": slack_status,
        },
        "live_probe": live_probe,
        "live_probe_note": (
            "Set PARALLAX_CANARY_ALERTING_LIVE_PROBE=1 to send a real test "
            "notification (pages oncall — operators only)."
        ),
    }

    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print("Alerting probe (config-only):")
        print(f"  PagerDuty: {pd_status} (env: PAGERDUTY_M4_CANARY_KEY)")
        print(f"  Slack:     {slack_status} (env: SLACK_WEBHOOK_M4_CANARY)")
        print(f"  Live probe: {'enabled' if live_probe else 'disabled'}")
        if not live_probe:
            print(
                "  Note: set PARALLAX_CANARY_ALERTING_LIVE_PROBE=1 to send a real "
                "test notification (operators only)."
            )

    return 0 if pd_url and slack_url else 1
