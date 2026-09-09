"""CLI runner — sequential, resumable, jsonl-first.

Usage::

    python -m eval.longmemeval.run \\
        --split oracle \\
        --limit 5 \\
        --answer-model gemini-3.1-pro-preview \\
        --judge-model gemini-3.1-pro-preview \\
        --out eval/results/oracle_smoke.jsonl

The runner appends one JSON object per question to the output file. If
the output file already exists, questions whose ``question_id`` is already
present are skipped — so a crash mid-run can be resumed by re-invoking
the same command.

Concurrency is deliberately sequential here because Tier 1 TPM (250k
shared for free tier, higher for paid) is easier to respect one request
at a time during smoke. A later version can parallelize with an
``asyncio.Semaphore`` once the per-call token profile is known.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from eval.longmemeval.dataset import iter_questions
from eval.longmemeval.pipeline import AnswerRecord, run_one

_write_lock = threading.Lock()

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("LONGMEMEVAL_DATA_DIR", "E:/Workspace/longmemeval/data"))

SPLIT_FILES = {
    "oracle": DATA_DIR / "longmemeval_oracle.json",
    "s": DATA_DIR / "longmemeval_s_cleaned.json",
    "m": DATA_DIR / "longmemeval_m_cleaned.json",
}

logger = logging.getLogger("lme")


def _load_done(out: Path) -> set[str]:
    if not out.exists():
        return set()
    done: set[str] = set()
    with out.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["question_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def _append_jsonl(out: Path, record: AnswerRecord) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dataclasses.asdict(record), ensure_ascii=False) + "\n"
    with _write_lock, out.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


def _summarize(records: list[AnswerRecord]) -> dict:
    total = len(records)
    verdicts = Counter(r.verdict for r in records)
    by_type: dict[str, Counter] = {}
    for r in records:
        by_type.setdefault(r.question_type, Counter())[r.verdict] += 1
    accuracy = verdicts["CORRECT"] / total if total else 0.0
    tokens_in = sum(r.answer_prompt_tokens + r.judge_prompt_tokens for r in records)
    tokens_out = sum(r.answer_output_tokens + r.judge_output_tokens for r in records)
    # PA-PARALLAX-F9: a cache replay costs nothing, so counting it as spend
    # inflates every cost number in the eval line. ``tokens_prompted`` is what
    # the prompts weighed (all calls); ``tokens_billed`` is what a provider was
    # actually asked to read (live calls only); ``replay_count`` says how many
    # of the 2N calls behind these records were served from llm_cache.
    tokens_billed = sum(
        (0 if r.answer_cached else r.answer_prompt_tokens)
        + (0 if r.judge_cached else r.judge_prompt_tokens)
        for r in records
    )
    replay_count = sum(int(r.answer_cached) + int(r.judge_cached) for r in records)
    return {
        "total": total,
        "accuracy": accuracy,
        "verdicts": dict(verdicts),
        "by_type": {k: dict(v) for k, v in by_type.items()},
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_prompted": tokens_in,
        "tokens_billed": tokens_billed,
        "replay_count": replay_count,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--split", choices=sorted(SPLIT_FILES), default="oracle")
    p.add_argument("--limit", type=int, default=None, help="max questions")
    p.add_argument(
        "--types", default=None, help="comma-separated question_type filter"
    )
    p.add_argument("--answer-model", default="gemini-3.1-pro-preview")
    p.add_argument("--judge-model", default="gemini-3.1-pro-preview")
    p.add_argument("--max-output-tokens", type=int, default=512)
    p.add_argument(
        "--use-retrieval",
        action="store_true",
        help=(
            "Build the answer prompt via build_from_parallax_retrieval() "
            "(reads from Parallax store) instead of dump_all_sessions() "
            "(v1 bypass). Default False preserves Run B reproducibility."
        ),
    )
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--no-resume", action="store_true", help="ignore existing output file"
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="parallel workers (default 1 = sequential)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    path = SPLIT_FILES[args.split]
    if not path.exists():
        print(f"ERROR: split file missing: {path}", file=sys.stderr)
        return 2

    done = set() if args.no_resume else _load_done(args.out)
    if done:
        logger.warning("resuming — skipping %d already-logged questions", len(done))

    type_filter = (
        frozenset(x.strip() for x in args.types.split(",") if x.strip())
        if args.types
        else None
    )

    questions = list(iter_questions(path, limit=None, types=type_filter))
    pending = [q for q in questions if q.question_id not in done]
    if args.limit is not None:
        pending = pending[: args.limit]

    print(
        f"[run] split={args.split} pending={len(pending)}/{len(questions)} "
        f"answer={args.answer_model} judge={args.judge_model}"
    )

    records: list[AnswerRecord] = []
    total = len(pending)
    t0 = time.time()

    def _task(q):
        t_q = time.time()
        rec = run_one(
            q,
            answer_model=args.answer_model,
            judge_model=args.judge_model,
            max_output_tokens=args.max_output_tokens,
            use_retrieval=args.use_retrieval,
        )
        _append_jsonl(args.out, rec)
        return rec, time.time() - t_q

    if args.concurrency <= 1:
        for i, q in enumerate(pending, 1):
            rec, dt = _task(q)
            records.append(rec)
            print(
                f"[{i}/{total}] {q.question_id[:20]} "
                f"type={q.question_type[:18]:<18} verdict={rec.verdict:<9} "
                f"in={rec.answer_prompt_tokens:>6} dt={dt:5.1f}s"
            )
    else:
        print(f"[run] concurrency={args.concurrency}")
        done_count = 0
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            future_to_q = {ex.submit(_task, q): q for q in pending}
            for fut in as_completed(future_to_q):
                q = future_to_q[fut]
                try:
                    rec, dt = fut.result()
                except Exception:  # noqa: BLE001
                    logger.exception("worker crashed for %s", q.question_id)
                    continue
                records.append(rec)
                done_count += 1
                print(
                    f"[{done_count}/{total}] {q.question_id[:20]} "
                    f"type={q.question_type[:18]:<18} verdict={rec.verdict:<9} "
                    f"in={rec.answer_prompt_tokens:>6} dt={dt:5.1f}s",
                    flush=True,
                )

    elapsed = time.time() - t0
    summary = _summarize(records) if records else {"total": 0}
    summary["elapsed_sec"] = round(elapsed, 1)
    summary_path = args.out.with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("---")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[run] done in {elapsed:.1f}s → {args.out} (+ {summary_path.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
