"""Detached 6-cell gemma4:31b sweep driver.

Runs top_k ∈ {4, 8} × max_chars ∈ {8000, 16000, 24000} full 500Q oracle
with gemma4:31b answer+judge. Results written per-cell to eval/results/.
Results aggregate to a summary table at the end.

Usage (from repo root, with uv):
    PARALLAX_EMBEDDING_BASE_URL=... PARALLAX_SEMANTIC_RETRIEVAL=1 \\
    PARALLAX_OLLAMA_THINK=false \\
    uv run python -m eval.longmemeval.run_gemma_sweep 2>&1 | tee eval/results/m8_gemma_sweep.log
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Bootstrap env before any Parallax import
os.environ.setdefault("PARALLAX_EMBEDDING_BASE_URL", "http://192.168.1.134:11434")
# Mirror the embedding host for the answer/judge LLM calls unless the caller
# split them on purpose - parallax.llm.call reads PARALLAX_OLLAMA_BASE_URL,
# not the embedding URL, and the two must not silently diverge.
os.environ.setdefault("PARALLAX_OLLAMA_BASE_URL", os.environ["PARALLAX_EMBEDDING_BASE_URL"])
os.environ.setdefault("PARALLAX_SEMANTIC_RETRIEVAL", "1")
os.environ.setdefault("PARALLAX_OLLAMA_THINK", "false")

from eval.longmemeval.run_retrieval_vs_dump import main as run_cell  # noqa: E402

RESULTS_DIR = Path(__file__).parent.parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

MODEL = "ollama:gemma4:31b"
LIMIT = 500
CONCURRENCY = 2  # LLM calls serialized by _db_lock; 2 gives marginal ingest overlap

GRID = [
    (4, 8000),
    (4, 16000),
    (4, 24000),
    (8, 8000),
    (8, 16000),
    (8, 24000),
]


def cell_tag(top_k: int, max_chars: int) -> str:
    return f"m8_gemma_tk{top_k}_mc{max_chars}"


def run_all() -> None:
    summaries: list[dict] = []
    t_total = time.time()

    for top_k, max_chars in GRID:
        tag = cell_tag(top_k, max_chars)
        out = RESULTS_DIR / f"{tag}.jsonl"
        summary_path = RESULTS_DIR / f"{tag}.summary.json"

        if summary_path.exists():
            print(f"[SKIP] {tag} — summary already exists, loading from cache")
            summaries.append(json.loads(summary_path.read_text()))
            continue

        print(f"\n{'='*60}")
        print(f"[CELL] {tag}  ({LIMIT}Q, concurrency={CONCURRENCY})")
        print(f"{'='*60}", flush=True)
        t0 = time.time()

        argv = [
            "--limit", str(LIMIT),
            "--stratified",
            "--answer-model", MODEL,
            "--judge-model", MODEL,
            "--top-k", str(top_k),
            "--max-chars", str(max_chars),
            "--concurrency", str(CONCURRENCY),
            "--out", str(out),
        ]
        rc = run_cell(argv)
        elapsed = time.time() - t0
        print(f"[CELL] {tag} done in {elapsed:.0f}s  rc={rc}")

        if summary_path.exists():
            summaries.append(json.loads(summary_path.read_text()))

    # ---- aggregate table -----------------------------------------------
    print(f"\n{'='*60}")
    print("AGGREGATE TABLE")
    print(f"{'='*60}")
    header = (
        f"{'cell':<28} {'dump_acc':>8} {'retr_acc':>8}"
        f" {'Δpp':>6} {'ratio':>7} {'secs':>7} {'GO?'}"
    )
    print(header)
    print("-" * 80)
    for s in summaries:
        tag = cell_tag(s["top_k"], s["max_chars"])
        dump_acc = s["dump"]["accuracy"]
        retr_acc = s["retrieval"]["accuracy"]
        delta_pp = s["delta_pp"]
        ratio = s["token_ratio_dump_over_retr"]
        secs = s.get("elapsed_sec", -1)
        go = "YES" if s.get("pre_registered_interesting") else "no"
        row = (
            f"{tag:<28} {dump_acc:>8.4f} {retr_acc:>8.4f}"
            f" {delta_pp:>6.2f} {ratio:>7.2f} {secs:>7.0f} {go}"
        )
        print(row)

    total_elapsed = time.time() - t_total
    print(f"\nTotal sweep time: {total_elapsed:.0f}s ({total_elapsed/3600:.2f}h)")

    # Write aggregate JSON
    agg_path = RESULTS_DIR / "m8_gemma_aggregate.json"
    agg_path.write_text(json.dumps(summaries, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Aggregate JSON: {agg_path}")


if __name__ == "__main__":
    run_all()
