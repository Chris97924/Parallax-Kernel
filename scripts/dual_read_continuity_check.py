#!/usr/bin/env python3
"""M3b — 72h continuity check for the dual-read decision JSONL stream (US-006).

Verifies the six DoD numerics from ralplan §6 line 416-426 in one shot:

  ``discrepancy_rate``           ≤ 0.001  (0.1%)
  ``arbitration_conflict_rate``  ≤ 0.01   (1%)
  ``write_error_rate``           ≤ 0.0002 (0.02%)
  ``aphelion_unreachable_rate``  ≤ 0.005  (0.5%)
  ``crosswalk_miss_rate``        ≤ 0.05   (5%, measured at +48h gate per Q11)
  ``circuit_open_count``         ≤ 3      (absolute count over the 72h window)

Plus a record-count gate (``--min-records=N``) for "no dual-read activity =
DoD fail" semantics (mirror of the M2 shadow continuity check).

Traffic-source partitioning (2026-08-02)
----------------------------------------
The corpus is partitioned by ``traffic_source`` — ``natural`` / ``synthetic``
/ ``unknown`` — using the SAME record semantics as the ``/metrics``
exposition (``parallax.router.dual_read_metrics.record_traffic_source``: a
record with no ``traffic_source`` field is ``unknown``, never ``natural``).

**The gate evaluates the NATURAL partition only.** This matches the
retargeted Prometheus alerts, which select ``{traffic_source="natural"}`` per
``docs/m4-prep/traffic-gap-resolution.md`` §3.3. Before this, the alerts
evaluated natural while this CLI evaluated the combined population, so during
the M4 burn-in — synthetic records legitimately at conflict rate 1.0 next to
clean natural records — the two authoritative gates contradicted each other
and this one could block a promotion the alerts considered healthy.

Every partition's counts and rates are still printed, so synthetic remains
inspectable during burn-in; only the pass/fail verdict is natural-scoped.

Exit codes (tri-state)::

    0   PASS                  every natural-partition assertion holds
    1   FAIL                  a threshold was breached, the log dir is
                              missing, or --min-records was not met
    2   INSUFFICIENT_NATURAL  the natural partition has fewer than
                              --natural-min-records records, so the gate
                              cannot be evaluated yet

Exit 2 is NOT a failure. It is the ``WARN_NATURAL_INSUFFICIENT`` status from
traffic-gap-resolution.md §3.3 — "we cannot evaluate this yet" — and the M4
canary closeout re-homed that evaluation to the natural-traffic milestone
(Chris decision 2026-08-02). Callers that do not care about natural volume
(smoke runs, replaying a synthetic-only corpus) pass
``--natural-min-records=0``, which restores the pre-partitioning behaviour of
evaluating whatever is there.

Usage::

    python scripts/dual_read_continuity_check.py --since=72h
    python scripts/dual_read_continuity_check.py --since=72h --format=json --min-records=1000
    python scripts/dual_read_continuity_check.py --since=72h --natural-min-records=0

The summary report is written to stdout (never stderr) so the CLI is
composable in pipelines.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

# Add repo root to sys.path so this script is runnable without installation.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from parallax.router.dual_read_metrics import (  # noqa: E402
    APHELION_UNREACHABLE_THRESHOLD,
    ARBITRATION_CONFLICT_RATE_THRESHOLD,
    CIRCUIT_OPEN_72H_MAX,
    CROSSWALK_MISS_THRESHOLD,
    DISCREPANCY_RATE_THRESHOLD_M3,
    TRAFFIC_SOURCE_PARTITIONS,
    WRITE_ERROR_RATE_THRESHOLD,
    compute_all_rates,
    load_records,
    partition_by_traffic_source,
)
from parallax.shadow.discrepancy import parse_window  # noqa: E402

# The partition the DoD verdict is computed over. Mirrors the
# {traffic_source="natural"} selector the Prometheus alerts use — the two
# gates must agree on population or they can contradict each other.
GATE_TRAFFIC_SOURCE = "natural"

# Default natural-volume floor. traffic-gap-resolution.md §3.3 requires
# >= 100 natural calls/24h before the semantic gate means anything.
DEFAULT_NATURAL_MIN_RECORDS = 100

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INSUFFICIENT_NATURAL = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dual_read_continuity_check",
        description=(
            "M3b US-006 — verify 6-metric DoD over the dual-read decision "
            "JSONL stream (72h default window)."
        ),
    )
    parser.add_argument(
        "--since",
        default="72h",
        help="Window covered by the check (e.g. 1h, 24h, 72h, 3d). Default: 72h.",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Override DUAL_READ_LOG_DIR for the run. Default: env or parallax/logs/.",
    )
    parser.add_argument(
        "--threshold-discrepancy",
        type=float,
        default=DISCREPANCY_RATE_THRESHOLD_M3,
        help=f"Maximum tolerated discrepancy_rate. Default: {DISCREPANCY_RATE_THRESHOLD_M3}.",
    )
    parser.add_argument(
        "--threshold-conflict",
        type=float,
        default=ARBITRATION_CONFLICT_RATE_THRESHOLD,
        help=(
            f"Maximum tolerated arbitration_conflict_rate. Default: "
            f"{ARBITRATION_CONFLICT_RATE_THRESHOLD}."
        ),
    )
    parser.add_argument(
        "--threshold-write-error",
        type=float,
        default=WRITE_ERROR_RATE_THRESHOLD,
        help=f"Maximum tolerated write_error_rate. Default: {WRITE_ERROR_RATE_THRESHOLD}.",
    )
    parser.add_argument(
        "--threshold-aphelion-unreachable",
        type=float,
        default=APHELION_UNREACHABLE_THRESHOLD,
        help=(
            f"Maximum tolerated aphelion_unreachable_rate. Default: "
            f"{APHELION_UNREACHABLE_THRESHOLD}."
        ),
    )
    parser.add_argument(
        "--threshold-crosswalk-miss",
        type=float,
        default=CROSSWALK_MISS_THRESHOLD,
        help=(
            f"Maximum tolerated crosswalk_miss_rate. Default: "
            f"{CROSSWALK_MISS_THRESHOLD} (measured at +48h gate per Q11)."
        ),
    )
    parser.add_argument(
        "--threshold-circuit-open",
        type=int,
        default=CIRCUIT_OPEN_72H_MAX,
        help=(
            f"Maximum tolerated circuit_open_count over the window. Default: "
            f"{CIRCUIT_OPEN_72H_MAX}."
        ),
    )
    parser.add_argument(
        "--min-records",
        type=int,
        default=0,
        help=(
            "Minimum record count for the check to pass (zero-activity guard). "
            "Default: 0 (skip the gate when no dual-read traffic exists yet)."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="Output format. Default: human.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="ISO-8601 UTC anchor for the window cutoff (testing/replay only).",
    )
    parser.add_argument(
        "--natural-min-records",
        type=int,
        default=DEFAULT_NATURAL_MIN_RECORDS,
        help=(
            "Minimum records in the natural partition before the DoD gate can "
            "be evaluated. Below this the CLI exits 2 (INSUFFICIENT_NATURAL) — "
            "a distinct 'cannot evaluate yet' status, NOT a failure, per "
            "traffic-gap-resolution.md §3.3 WARN_NATURAL_INSUFFICIENT. Pass 0 "
            f"to evaluate whatever is present. Default: {DEFAULT_NATURAL_MIN_RECORDS}."
        ),
    )
    parser.add_argument(
        "--allow-missing-dir",
        action="store_true",
        default=False,
        help=(
            "Treat a missing log directory as PASS instead of FAIL. Default: "
            "missing dir is a misconfiguration breach (exit 1). Set this for "
            "smoke runs against fresh boxes where the dir has not been created yet."
        ),
    )
    return parser


def _parse_now(raw: str | None) -> _dt.datetime | None:
    if raw is None:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        # MED-LOWS-BUNDLED — the stdlib only stopped tripping on the ``Z``
        # suffix in 3.11; older runtimes still raise.  Surface a friendly
        # hint pointing operators at the right format.
        raise ValueError(
            f"--now value {raw!r} is not a valid ISO-8601 timestamp; "
            f"use e.g. '2026-04-30T12:00:00+00:00' (Z-suffix not accepted "
            f"on Python < 3.11): {exc}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed


def _build_report(args: argparse.Namespace) -> dict[str, Any]:
    now = _parse_now(args.now)
    log_dir = Path(args.log_dir) if args.log_dir else None

    # MED-LOWS-BUNDLED — single load_records call shared across all 6
    # rate computations. Pre-fix the CLI walked the file tree 7×; now
    # one walk feeds compute_all_rates which returns every metric in
    # one pass.
    delta = parse_window(args.since)
    load_result = load_records(log_dir=log_dir, since=delta, now=now)
    total_records = len(load_result.records)
    dir_missing = load_result.dir_missing
    malformed = load_result.malformed

    # Partition with the SAME semantics /metrics uses (absent field ->
    # "unknown", never "natural"), so the two authoritative gates on this
    # corpus cannot disagree about how a record is attributed.
    partitioned = partition_by_traffic_source(load_result.records)
    partitions: dict[str, dict[str, Any]] = {}
    for source in TRAFFIC_SOURCE_PARTITIONS:
        source_records = partitioned.get(source, [])
        source_metrics = compute_all_rates(source_records)
        partitions[source] = {
            "records": len(source_records),
            "discrepancy_rate": source_metrics["discrepancy_rate"],
            "arbitration_conflict_rate": source_metrics["arbitration_conflict_rate"],
            "write_error_rate": source_metrics["write_error_rate"],
            "aphelion_unreachable_rate": source_metrics["aphelion_unreachable_rate"],
            "crosswalk_miss_rate": source_metrics["crosswalk_miss_rate"],
            "circuit_open_count": int(source_metrics["circuit_open_count"]),
        }

    # The verdict is computed over the natural partition only — matching the
    # {traffic_source="natural"} selector the alert rules use.
    gate = partitions[GATE_TRAFFIC_SOURCE]
    natural_records = gate["records"]
    rates = {
        "discrepancy_rate": gate["discrepancy_rate"],
        "arbitration_conflict_rate": gate["arbitration_conflict_rate"],
        "write_error_rate": gate["write_error_rate"],
        "aphelion_unreachable_rate": gate["aphelion_unreachable_rate"],
        "crosswalk_miss_rate": gate["crosswalk_miss_rate"],
    }
    circuit_count = gate["circuit_open_count"]

    failures: list[str] = []
    # H5 — distinguish "log dir missing" from "log dir empty". Operators
    # explicitly opt in via --allow-missing-dir for fresh-box smoke runs.
    if dir_missing and not args.allow_missing_dir:
        failures.append("log_dir_missing")
    if total_records < args.min_records:
        failures.append("min_records")
    if rates["discrepancy_rate"] > args.threshold_discrepancy:
        failures.append("discrepancy_rate")
    if rates["arbitration_conflict_rate"] > args.threshold_conflict:
        failures.append("arbitration_conflict_rate")
    if rates["write_error_rate"] > args.threshold_write_error:
        failures.append("write_error_rate")
    if rates["aphelion_unreachable_rate"] > args.threshold_aphelion_unreachable:
        failures.append("aphelion_unreachable_rate")
    if rates["crosswalk_miss_rate"] > args.threshold_crosswalk_miss:
        failures.append("crosswalk_miss_rate")
    if circuit_count > args.threshold_circuit_open:
        failures.append("circuit_open_count")

    if malformed > 0:
        # MED-MALFORMED-COUNTER — log a warning to stderr (CLI does NOT
        # fail on malformed alone — operational nuisance, not a breach).
        sys.stderr.write(f"warning: {malformed} malformed JSONL line(s) skipped during load\n")

    # Precedence: a real breach always outranks "cannot evaluate yet".
    # ``log_dir_missing`` and ``min_records`` say something is wrong with the
    # corpus itself, and a natural-partition threshold breach can only happen
    # when there IS natural data to breach it — so any failure means exit 1,
    # and thin natural volume downgrades an otherwise-clean run to exit 2.
    if failures:
        status, exit_code = "fail", EXIT_FAIL
    elif natural_records < args.natural_min_records:
        status, exit_code = "insufficient_natural", EXIT_INSUFFICIENT_NATURAL
    else:
        status, exit_code = "pass", EXIT_PASS

    return {
        "since": args.since,
        "log_dir": str(log_dir) if log_dir else None,
        "log_dir_missing": dir_missing,
        "total_records": total_records,
        "malformed": malformed,
        "gate_traffic_source": GATE_TRAFFIC_SOURCE,
        "natural_records": natural_records,
        "partitions": partitions,
        "status": status,
        "exit_code": exit_code,
        "discrepancy_rate": rates["discrepancy_rate"],
        "arbitration_conflict_rate": rates["arbitration_conflict_rate"],
        "write_error_rate": rates["write_error_rate"],
        "aphelion_unreachable_rate": rates["aphelion_unreachable_rate"],
        "crosswalk_miss_rate": rates["crosswalk_miss_rate"],
        "circuit_open_count": circuit_count,
        "thresholds": {
            "discrepancy_rate": args.threshold_discrepancy,
            "arbitration_conflict_rate": args.threshold_conflict,
            "write_error_rate": args.threshold_write_error,
            "aphelion_unreachable_rate": args.threshold_aphelion_unreachable,
            "crosswalk_miss_rate": args.threshold_crosswalk_miss,
            "circuit_open_count": args.threshold_circuit_open,
            "min_records": args.min_records,
            "natural_min_records": args.natural_min_records,
        },
        "failures": failures,
        # Kept for backward compatibility with existing consumers. Note it is
        # False for BOTH "fail" and "insufficient_natural" — read ``status``
        # or ``exit_code`` to tell a breach from "not evaluable yet".
        "passed": status == "pass",
    }


def _format_human(report: dict[str, Any]) -> str:
    """Render the report as a one-screen oncall summary."""
    status = {
        "pass": "PASS",
        "fail": "FAIL",
        "insufficient_natural": "INSUFFICIENT_NATURAL",
    }[report.get("status", "pass" if report["passed"] else "fail")]
    th = report["thresholds"]
    log_dir_display = report["log_dir"] or "(env / default)"
    if report.get("log_dir_missing"):
        log_dir_display = f"{log_dir_display} [MISSING]"
    lines = [
        f"[{status}] M3b US-006 — dual-read continuity check",
        f"  window:                       {report['since']}",
        f"  log_dir:                      {log_dir_display}",
        f"  total_records:                {report['total_records']}",
        f"  malformed:                    {report.get('malformed', 0)}",
        f"  gate partition:               {report.get('gate_traffic_source', 'natural')}"
        f"  (verdict is computed over this partition only)",
    ]
    # Show every partition so synthetic stays inspectable during burn-in,
    # even though only natural is gated.
    for source in TRAFFIC_SOURCE_PARTITIONS:
        part = report.get("partitions", {}).get(source)
        if part is None:
            continue
        marker = " <- gated" if source == report.get("gate_traffic_source") else ""
        lines.append(
            f"  [{source}] records={part['records']}"
            f" discrepancy={part['discrepancy_rate']:.6f}"
            f" conflict={part['arbitration_conflict_rate']:.6f}"
            f" write_err={part['write_error_rate']:.6f}"
            f" unreachable={part['aphelion_unreachable_rate']:.6f}"
            f" crosswalk={part['crosswalk_miss_rate']:.6f}"
            f" circuit_open={part['circuit_open_count']}{marker}"
        )
    lines += [
        f"  discrepancy_rate:             {report['discrepancy_rate']:.6f}"
        f"  (threshold {th['discrepancy_rate']})",
        f"  arbitration_conflict_rate:    {report['arbitration_conflict_rate']:.6f}"
        f"  (threshold {th['arbitration_conflict_rate']})",
        f"  write_error_rate:             {report['write_error_rate']:.6f}"
        f"  (threshold {th['write_error_rate']})",
        f"  aphelion_unreachable_rate:    {report['aphelion_unreachable_rate']:.6f}"
        f"  (threshold {th['aphelion_unreachable_rate']})",
        f"  crosswalk_miss_rate:          {report['crosswalk_miss_rate']:.6f}"
        f"  (threshold {th['crosswalk_miss_rate']}; measured at +48h gate)",
        f"  circuit_open_count:           {report['circuit_open_count']}"
        f"  (threshold {th['circuit_open_count']})",
    ]
    if report["failures"]:
        lines.append(f"  failures:                     {', '.join(report['failures'])}")
    if report.get("status") == "insufficient_natural":
        lines.append(
            f"  natural_records:              {report.get('natural_records', 0)}"
            f"  (need >= {th.get('natural_min_records')}) — gate NOT evaluated;"
            f" this is WARN_NATURAL_INSUFFICIENT (§3.3), not a failure"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    report = _build_report(args)
    if args.format == "json":
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_format_human(report) + "\n")
    # Tri-state: 0 pass / 1 fail / 2 insufficient natural volume. See the
    # module docstring — exit 2 is "cannot evaluate yet", not a breach.
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
