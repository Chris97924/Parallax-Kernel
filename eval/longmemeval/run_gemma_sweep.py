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

import hashlib
import json
import os
import time
from pathlib import Path

# Bootstrap env before any Parallax import
os.environ.setdefault("PARALLAX_EMBEDDING_BASE_URL", "http://192.168.1.134:11434")
# Mirror the embedding host for the answer/judge LLM calls unless the caller
# routed them explicitly - parallax.llm.call checks PARALLAX_OLLAMA_BASE_URL
# then OLLAMA_BASE_URL, so mirroring over either would hijack a deliberate
# split; only fill the gap when NEITHER is set.
if "PARALLAX_OLLAMA_BASE_URL" not in os.environ and "OLLAMA_BASE_URL" not in os.environ:
    os.environ["PARALLAX_OLLAMA_BASE_URL"] = os.environ["PARALLAX_EMBEDDING_BASE_URL"]
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


def _env_identity() -> dict:
    """Everything OUTSIDE the summary schema that still changes results:
    retrieval mode, embedding host, effective Ollama host (same precedence
    parallax.llm.call uses) and think state, plus this driver's limit/model."""
    return {
        "limit": LIMIT,
        "model": MODEL,
        "semantic_retrieval": os.environ.get("PARALLAX_SEMANTIC_RETRIEVAL", ""),
        "embedding_base_url": os.environ.get("PARALLAX_EMBEDDING_BASE_URL", ""),
        # mirror embeddings.get_embedding_provider defaults so unset and an
        # explicit "bge-m3" hash identically (no spurious cell re-runs)
        "embedding_model": os.environ.get("PARALLAX_EMBEDDING_MODEL", "bge-m3").strip()
        or "bge-m3",
        "ollama_base_url": os.environ.get("PARALLAX_OLLAMA_BASE_URL")
        or os.environ.get("OLLAMA_BASE_URL", ""),
        "ollama_think": os.environ.get("PARALLAX_OLLAMA_THINK", ""),
    }


def _env_tag() -> str:
    blob = json.dumps(_env_identity(), sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:8]


ENV_TAG = _env_tag()


def cell_tag(top_k: int, max_chars: int) -> str:
    # env hash in the tag = ANY env change switches result filenames, so a
    # cache file can never be reused across a different runtime environment.
    return f"m8_gemma_tk{top_k}_mc{max_chars}_env{ENV_TAG}"


def _summary_complete(summary: dict, expected: dict) -> bool:
    """A cached cell counts as done only if it belongs to THIS sweep's cell
    (limit/models/knobs all match - an old smoke run under the same tag must
    not stand in) AND every question got a verdict in BOTH arms -
    run_retrieval_vs_dump still writes a summary when workers crash mid-run
    (slice_n intact, per-arm records missing)."""
    if any(summary.get(k) != v for k, v in expected.items()):
        return False
    n = expected["slice_n"]
    for arm in ("dump", "retrieval"):
        verdicts = summary.get(arm, {}).get("verdicts")
        if not isinstance(verdicts, dict) or sum(verdicts.values()) != n:
            return False
    return True


def _load_summary(path: Path, expected: dict) -> dict | None:
    """Parsed summary, or None when the cell must (re)run: covers a missing
    file, crash-truncated JSON (process killed mid-write), a partial run,
    and a summary from a different limit/model/knob combination."""
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return summary if _summary_complete(summary, expected) else None


def run_all() -> None:
    summaries: list[dict] = []
    t_total = time.time()
    print(f"env identity {ENV_TAG}: {json.dumps(_env_identity(), sort_keys=True)}")

    for top_k, max_chars in GRID:
        tag = cell_tag(top_k, max_chars)
        out = RESULTS_DIR / f"{tag}.jsonl"
        summary_path = RESULTS_DIR / f"{tag}.summary.json"
        expected = {
            "slice_n": LIMIT,
            "answer_model": MODEL,
            "judge_model": MODEL,
            "top_k": top_k,
            "max_chars": max_chars,
        }

        if summary_path.exists():
            cached = _load_summary(summary_path, expected)
            if cached is not None:
                print(f"[SKIP] {tag} — complete summary cached")
                summaries.append(cached)
                continue
            print(f"[REDO] {tag} — cached summary incomplete/unreadable/mismatched, re-running")

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

        fresh = _load_summary(summary_path, expected)
        if fresh is None:
            print(
                f"[WARN] {tag} — no complete summary after run (rc={rc});"
                " EXCLUDED from aggregate, rerun the sweep to repair"
            )
        else:
            summaries.append(fresh)

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
