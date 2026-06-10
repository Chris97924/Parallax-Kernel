"""ADR-006 §Acceptance gate #5 — baseline reproducibility harness.

Runs the same config twice with different seed / scheduler order, extracts
``fallback_e2e`` from each RunReportV2, and checks that
``max − min ≤ 0.01`` (±1pp). If this gate fails, the 81.7% fallback floor
itself is noisy and the broader ADR gate #3 is not meaningful — Day-1 is
expected to halt and stabilise the eval harness before accepting results.

**Skeleton only — ``run_one`` is stubbed**; Day-1 wires the real pipeline
(same ``run_one`` entry point used by ``ablate_fallback.py`` and
``sweep_thresholds.py``). The CLI and data flow here are frozen so a
real integration is a one-line swap on ``_stub_run``.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import hashlib
import json
import pathlib
import sys
from collections.abc import Callable
from typing import Any

from eval.longmemeval.schema_v2 import RunReportV2
from parallax.retrieval.contracts import Intent

# ADR-006 §Acceptance gate #5: max − min across A/B reproduce runs.
GATE_THRESHOLD: float = 0.01


@dataclasses.dataclass(frozen=True)
class ReproduceConfig:
    label: str
    seed_a: int = 1
    seed_b: int = 2


@dataclasses.dataclass(frozen=True)
class ReproduceReport:
    label: str
    fallback_e2e_a: float
    fallback_e2e_b: float
    max_minus_min: float
    passes_gate: bool
    seed_a: int
    seed_b: int
    created_at: str


def _deterministic_fallback_e2e(label: str, seed: int) -> float:
    """Stable per-(label, seed) synthetic score in [0.80, 0.90].

    Lets tests exercise both passes_gate=True and passes_gate=False paths
    without any real LLM work. Day-1 deletes this when ``_stub_run`` gets
    replaced by the real ``run_one`` wiring.
    """
    digest = hashlib.sha256(f"{label}:{seed}".encode()).hexdigest()
    # Map the first 4 hex chars (16 bits) onto [0.80, 0.90].
    bucket = int(digest[:4], 16) / 0xFFFF
    return 0.80 + 0.10 * bucket


def _stub_run(label: str, seed: int) -> dict[str, Any]:
    """Placeholder ``run_one`` — returns a schema-v2-compliant report.

    Day-1 replaces this with a real call into the filtered pipeline.
    """
    return {
        "results": [],
        "aggregate": {
            "router_acc": 0.0,
            "cond_acc_correct_route": 0.0,
            "e2e_acc": 0.0,
            "abstain_rate": 0.0,
            "oracle_router_e2e": 0.0,
            "fallback_e2e": _deterministic_fallback_e2e(label, seed),
            "by_intent_abstain": {i.value: 0.0 for i in Intent},
        },
        "run_id": f"reproduce_{label}_seed{seed}",
        "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "git_sha": None,
    }


def reproduce_once(
    cfg: ReproduceConfig,
    *,
    run_fn: Callable[[str, int], dict[str, Any]] = _stub_run,
) -> ReproduceReport:
    """Run ``run_fn`` twice with distinct seeds, compute the reproducibility gate.

    Both return values are validated through RunReportV2 before extraction
    so schema drift fails loudly instead of silently producing a bogus
    ``max_minus_min``.
    """
    report_a = run_fn(cfg.label, cfg.seed_a)
    report_b = run_fn(cfg.label, cfg.seed_b)
    validated_a = RunReportV2(**report_a)
    validated_b = RunReportV2(**report_b)

    a = validated_a.aggregate.fallback_e2e
    b = validated_b.aggregate.fallback_e2e
    max_minus_min = max(a, b) - min(a, b)

    return ReproduceReport(
        label=cfg.label,
        fallback_e2e_a=a,
        fallback_e2e_b=b,
        max_minus_min=max_minus_min,
        passes_gate=max_minus_min <= GATE_THRESHOLD,
        seed_a=cfg.seed_a,
        seed_b=cfg.seed_b,
        created_at=_dt.datetime.now(_dt.UTC).isoformat(),
    )


def write_reproduce_report(
    path: pathlib.Path | str, report: ReproduceReport
) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(dataclasses.asdict(report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="ADR-006 gate #5 baseline-reproducibility harness (skeleton).",
    )
    p.add_argument("--label", required=True, help="Config label (used in run_id + filename).")
    p.add_argument("--seed-a", type=int, default=1)
    p.add_argument("--seed-b", type=int, default=2)
    p.add_argument("--out-dir", default="eval/results/reproduce")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    cfg = ReproduceConfig(label=args.label, seed_a=args.seed_a, seed_b=args.seed_b)

    if args.dry_run:
        print(
            f"[reproduce] dry-run: label={cfg.label} "
            f"seed_a={cfg.seed_a} seed_b={cfg.seed_b} "
            f"gate_threshold={GATE_THRESHOLD}"
        )
        return 0

    report = reproduce_once(cfg)
    out_dir = pathlib.Path(args.out_dir)
    write_reproduce_report(out_dir / f"{cfg.label}.json", report)
    print(
        f"reproduce: fallback_e2e_a={report.fallback_e2e_a:.4f} "
        f"fallback_e2e_b={report.fallback_e2e_b:.4f} "
        f"max_minus_min={report.max_minus_min:.4f} "
        f"passes_gate={report.passes_gate}"
    )
    return 0 if report.passes_gate else 1


if __name__ == "__main__":
    sys.exit(main())
