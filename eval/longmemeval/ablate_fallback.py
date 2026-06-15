"""Day-1 ablation — sweep K, claims/events ratio, dedup strategy.

Emits a schema-v2 RunReportV2 JSON per configuration plus a CSV summary.
Dry-run (``--dry-run``) validates the config matrix and exits without
hitting the LLM.

NOTE: Day-1 will replace ``_stub_run`` with real ``run_one(...)`` against the
filtered pipeline. The scaffold today exists to lock in the schema gate and
the config matrix so a sweep kick-off is a one-line change.
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
from parallax.llm.call import call as llm_call  # noqa: F401  (ensures cache wiring)
from parallax.retrieval.contracts import Intent

K_GRID: tuple[int, ...] = (16, 24, 32, 48)
CLAIMS_EVENTS_RATIO_GRID: tuple[tuple[float, float], ...] = (
    (1.0, 0.0),
    (0.5, 0.5),
    (0.0, 1.0),
)
DEDUP_STRATEGIES: tuple[str, ...] = ("none", "source_id", "content_hash")


@dataclasses.dataclass(frozen=True)
class AblateConfig:
    k: int
    claims_weight: float
    events_weight: float
    dedup: str

    def label(self) -> str:
        return f"k{self.k}-c{self.claims_weight}-e{self.events_weight}-d{self.dedup}"


def _matrix() -> list[AblateConfig]:
    cfgs: list[AblateConfig] = []
    for k, (cw, ew), dedup in itertools.product(
        K_GRID, CLAIMS_EVENTS_RATIO_GRID, DEDUP_STRATEGIES
    ):
        cfgs.append(AblateConfig(k=k, claims_weight=cw, events_weight=ew, dedup=dedup))
    return cfgs


def _stub_run(cfg: AblateConfig) -> dict[str, Any]:
    """Placeholder run: returns a schema-v2 report with zero questions.

    Day-1 implementation fills this in against the real pipeline.
    """
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
        "run_id": f"ablate_fallback_{cfg.label()}",
        "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "git_sha": None,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="eval/results/ablate_fallback")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    out_dir = pathlib.Path(args.out_dir)
    cfgs = _matrix()
    print(f"[ablate_fallback] {len(cfgs)} configs; FALLBACK_FLOOR={FALLBACK_FLOOR}")

    if args.dry_run:
        for c in cfgs[:5]:
            print(f"  would run: {c.label()}")
        print(f"  ... ({len(cfgs) - 5} more)")
        return 0

    for cfg in cfgs:
        report = _stub_run(cfg)
        RunReportV2(**report)  # schema gate — crash loudly if drift
        write_run_report_v2(out_dir / f"{cfg.label()}.json", report)
    print(f"[ablate_fallback] wrote {len(cfgs)} reports → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
