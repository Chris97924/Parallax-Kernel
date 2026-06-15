"""Day-1 threshold sweep — one Flash routing pass, nine filter configs.

Strategy:

1. Run Flash routing once per question and persist via the SQLite LLM
   cache so the nine subsequent filter-threshold configurations re-use
   cached decisions for free.
2. Evaluate rule-confidence thresholds ``{0.75, 0.80, 0.85}`` crossed with
   Flash-confidence thresholds ``{0.65, 0.70, 0.75}``.

Writes one schema-v2 report per (rule, flash) pair.

NOTE: Day-1 will replace ``_stub_report`` with real ``run_one(...)`` against
the filtered pipeline. The scaffold today exists to lock in the schema gate
and the config grid so a sweep kick-off is a one-line change.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import itertools
import pathlib
import sys
from typing import Any

from eval.longmemeval.schema_v2 import RunReportV2, write_run_report_v2
from parallax.eval.constants import FALLBACK_FLOOR
from parallax.retrieval.contracts import Intent

RULE_THRESHOLDS: tuple[float, ...] = (0.75, 0.80, 0.85)
FLASH_THRESHOLDS: tuple[float, ...] = (0.65, 0.70, 0.75)


@dataclasses.dataclass(frozen=True)
class ThresholdPair:
    rule_conf: float
    flash_conf: float

    def label(self) -> str:
        return f"rule{self.rule_conf}-flash{self.flash_conf}"


def _prime_flash_cache(dry_run: bool) -> None:
    """Day-1: enumerate the question set once and hit Flash to populate the cache."""
    if dry_run:
        print("[sweep_thresholds] dry-run — skipping Flash priming")
        return
    # Day-1 filled in: call parallax.llm.call.call(model='gemini-2.5-flash', ...)
    # for every question's routing prompt with a deterministic cache_key.
    print("[sweep_thresholds] prime-flash-cache: TODO Day-1 (hook into routing pass)")


def _stub_report(pair: ThresholdPair) -> dict[str, Any]:
    return {
        "results": [],
        "aggregate": {
            "router_acc": 0.0,
            "cond_acc_correct_route": 0.0,
            "e2e_acc": 0.0,
            "abstain_rate": 0.0,
            "oracle_router_e2e": 0.0,
            "fallback_e2e": 0.0,
            "by_intent_abstain": {i.value: 0.0 for i in Intent},
        },
        "run_id": f"sweep_{pair.label()}",
        "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "git_sha": None,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="eval/results/sweep_thresholds")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    pairs = [
        ThresholdPair(rule_conf=r, flash_conf=f)
        for r, f in itertools.product(RULE_THRESHOLDS, FLASH_THRESHOLDS)
    ]
    print(
        f"[sweep_thresholds] {len(pairs)} configs; FALLBACK_FLOOR={FALLBACK_FLOOR}"
    )

    _prime_flash_cache(args.dry_run)

    out_dir = pathlib.Path(args.out_dir)
    if args.dry_run:
        for pair in pairs:
            print(f"  would run: {pair.label()}")
        return 0

    for pair in pairs:
        report = _stub_report(pair)
        RunReportV2(**report)
        write_run_report_v2(out_dir / f"{pair.label()}.json", report)
    print(f"[sweep_thresholds] wrote {len(pairs)} reports → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
