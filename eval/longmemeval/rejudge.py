"""Re-judge existing predictions with a different model.

Reads an answer jsonl produced by ``run.py``, calls the judge model on each
record's (question, gold, prediction), and writes a new jsonl with updated
``verdict`` / ``judge_reason`` / ``judge_*_tokens`` / ``judge_model``. All
other fields (prediction, answer_model, turns_ingested, etc.) are copied
verbatim, so downstream analysis can cross-reference the two files.

Usage::

    python -m eval.longmemeval.rejudge \\
        --in eval/results/s_baseline.jsonl \\
        --out eval/results/s_baseline_projudge.jsonl \\
        --judge-model gemini-3.1-pro-preview \\
        --concurrency 2

Resumable: if ``--out`` exists, records whose ``question_id`` is already in
it are skipped.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from eval.longmemeval.dataset import Question
from eval.longmemeval.gemini import GeminiResult, call
from eval.longmemeval.pipeline import (
    JUDGE_SYSTEM,
    AnswerRecord,
    build_judge_prompt,
    parse_verdict,
)

_write_lock = threading.Lock()
logger = logging.getLogger("rejudge")


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


def _append_jsonl(out: Path, rec: AnswerRecord) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dataclasses.asdict(rec), ensure_ascii=False) + "\n"
    with _write_lock, out.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


def _rejudge_one(src: dict, judge_model: str) -> AnswerRecord:
    # Build a fake Question with just the fields build_judge_prompt needs.
    q = Question(
        question_id=src["question_id"],
        question_type=src["question_type"],
        question=src["question"],
        answer=src["gold"],
        question_date="",
        sessions=(),
        answer_session_ids=(),
    )
    prediction = src.get("prediction", "") or ""
    try:
        jr: GeminiResult = call(
            model=judge_model,
            user=build_judge_prompt(q, prediction),
            system=JUDGE_SYSTEM,
            max_output_tokens=256,
        )
        verdict, reason = parse_verdict(jr.text)
        judge_pt, judge_ot = jr.prompt_tokens, jr.output_tokens
        # PA-PARALLAX-F9 (r3): the judge call this record describes is THIS
        # call, so its replay flag is the one the caching layer just reported.
        # Defaulting it to False marked every replayed re-judge as live spend.
        judge_cached = jr.cached
    except Exception as exc:  # noqa: BLE001
        logger.exception("rejudge failed for %s", src["question_id"])
        verdict = "ERROR"
        reason = f"rejudge exception: {str(exc)[:240]}"
        judge_pt = judge_ot = 0
        judge_cached = False

    return AnswerRecord(
        question_id=src["question_id"],
        question_type=src["question_type"],
        question=src["question"],
        gold=src["gold"],
        prediction=prediction,
        verdict=verdict,
        judge_reason=reason,
        turns_ingested=src.get("turns_ingested", 0),
        answer_prompt_tokens=src.get("answer_prompt_tokens", 0),
        answer_output_tokens=src.get("answer_output_tokens", 0),
        judge_prompt_tokens=judge_pt,
        judge_output_tokens=judge_ot,
        answer_model=src.get("answer_model", ""),
        judge_model=judge_model,
        # The answer is COPIED from the source row, not re-issued, so its cache
        # flag is a property of the source record and has to be carried across
        # verbatim — the rest of the answer_* fields already are. A row written
        # before the flag existed has no key and stays conservative (False).
        answer_cached=bool(src.get("answer_cached", False)),
        judge_cached=judge_cached,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="src", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--judge-model", default="gemini-3.1-pro-preview")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--skip-errors", action="store_true",
                   help="skip records where original verdict=ERROR (no prediction)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.src.exists():
        print(f"ERROR: src missing: {args.src}", file=sys.stderr)
        return 2

    records_src: list[dict] = []
    with args.src.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records_src.append(json.loads(line))

    done = _load_done(args.out)
    pending = [r for r in records_src if r["question_id"] not in done]
    if args.skip_errors:
        pending = [r for r in pending if r.get("prediction")]

    print(
        f"[rejudge] src={args.src.name} total={len(records_src)} "
        f"pending={len(pending)} judge={args.judge_model} "
        f"concurrency={args.concurrency}"
    )

    t0 = time.time()
    total = len(pending)

    def _task(src):
        t_q = time.time()
        rec = _rejudge_one(src, args.judge_model)
        _append_jsonl(args.out, rec)
        return rec, time.time() - t_q

    done_count = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(_task, s): s for s in pending}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                rec, dt = fut.result()
            except Exception:  # noqa: BLE001
                logger.exception("worker crashed for %s", src["question_id"])
                continue
            done_count += 1
            print(
                f"[{done_count}/{total}] {src['question_id'][:20]} "
                f"type={src['question_type'][:18]:<18} "
                f"verdict={rec.verdict:<9} dt={dt:5.1f}s",
                flush=True,
            )

    elapsed = time.time() - t0
    print(f"[rejudge] done in {elapsed:.1f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
