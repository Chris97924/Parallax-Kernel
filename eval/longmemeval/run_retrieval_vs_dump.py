"""Decisive retrieval-vs-dump slice for M8 — COST thesis, not accuracy.

Runs the SAME oracle questions through two answer-context strategies with the
SAME pinned judge, capturing BOTH QA accuracy AND token cost per arm:

* DUMP arm:      answer prompt = dump_all_sessions(q)               (the 86.96%
                 oracle-dump baseline path).
* RETRIEVAL arm: answer prompt = build_from_parallax_retrieval(q) with TIGHTENED
                 params (top_k / max_chars) so the M8 hybrid (lexical + live
                 bge-m3 RRF on GB10) actually filters instead of passing the
                 whole short oracle haystack through unchanged.

Pre-registered read: M8 is interesting iff RETRIEVAL lands within ~3pp of DUMP
accuracy while cutting answer-prompt tokens >= 10x.

CAVEAT (reported, load-bearing): this is the EVAL path — ephemeral SQLite store +
ephemeral re-embed via parallax.retrieval.semantic. It is NOT the production
retrieval stack (fallback_retrieve / pgvector). A result here may not transfer.

The eval itself is read-only on source data: each question ingests into a
throwaway TemporaryDirectory SQLite DB (INSERT-only), torn down per question.
Source corpus JSON is never mutated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from eval.longmemeval.dataset import iter_questions, load_dataset
from eval.longmemeval.gemini import call
from eval.longmemeval.pipeline import (
    ANSWER_SYSTEM,
    JUDGE_SYSTEM,
    build_answer_prompt,
    build_judge_prompt,
    parse_verdict,
)
from eval.longmemeval.store import (
    build_from_parallax_retrieval,
    dump_all_sessions,
    ephemeral_store,
    ingest_question,
)

DATA_DIR = Path(os.environ.get("LONGMEMEVAL_DATA_DIR", "E:/Workspace/longmemeval/data"))
ORACLE = DATA_DIR / "longmemeval_oracle.json"


def _answer_and_judge(q, transcript, answer_model, judge_model):
    ans = call(
        model=answer_model,
        user=build_answer_prompt(q, transcript),
        system=ANSWER_SYSTEM,
        max_output_tokens=512,
    )
    jr = call(
        model=judge_model,
        user=build_judge_prompt(q, ans.text),
        system=JUDGE_SYSTEM,
        max_output_tokens=256,
    )
    try:
        verdict, reason = parse_verdict(jr.text)
    except ValueError as exc:
        verdict, reason = "ERROR", f"parse fail: {exc}"
    return {
        "question_id": q.question_id,
        "question_type": q.question_type,
        "prediction": ans.text,
        "gold": q.answer,
        "verdict": verdict,
        "reason": reason,
        "answer_prompt_tokens": ans.prompt_tokens,
        "answer_output_tokens": ans.output_tokens,
        "judge_prompt_tokens": jr.prompt_tokens,
        "judge_output_tokens": jr.output_tokens,
        "transcript_chars": len(transcript),
    }


def _run_one(q, answer_model, judge_model, top_k, max_chars):
    # Ingest once into a throwaway store; build BOTH transcripts from the same
    # ingested rows so the two arms see identical source data.
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        dump_tx = dump_all_sessions(q)
        retr_tx = build_from_parallax_retrieval(
            conn, q, top_k=top_k, max_chars=max_chars
        )
    if not retr_tx:
        retr_rec = {
            "question_id": q.question_id,
            "question_type": q.question_type,
            "verdict": "ERROR",
            "reason": "empty retrieval transcript",
            "answer_prompt_tokens": 0,
            "answer_output_tokens": 0,
            "judge_prompt_tokens": 0,
            "judge_output_tokens": 0,
            "transcript_chars": 0,
        }
    else:
        retr_rec = _answer_and_judge(q, retr_tx, answer_model, judge_model)
    dump_rec = _answer_and_judge(q, dump_tx, answer_model, judge_model)
    return dump_rec, retr_rec


def _stratified_slice(limit):
    """Deterministic round-robin across question_type so the slice mirrors the
    corpus mix instead of over-weighting temporal-reasoning (the first 60
    oracle questions are all temporal)."""
    by_type: dict[str, list] = {}
    for q in load_dataset(ORACLE):
        by_type.setdefault(q.question_type, []).append(q)
    types = sorted(by_type)
    out, idx = [], 0
    while len(out) < limit and any(idx < len(by_type[t]) for t in types):
        for t in types:
            if idx < len(by_type[t]) and len(out) < limit:
                out.append(by_type[t][idx])
        idx += 1
    return out


def _acc(records):
    graded = [r for r in records if r["verdict"] in ("CORRECT", "INCORRECT")]
    if not graded:
        return 0.0, 0, 0
    c = sum(1 for r in graded if r["verdict"] == "CORRECT")
    return c / len(graded), c, len(graded)


def _arm_tokens(records):
    return sum(r["answer_prompt_tokens"] for r in records)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--answer-model", default="gemini-3.1-pro-preview")
    p.add_argument("--judge-model", default="gemini-3.1-pro-preview")
    p.add_argument("--top-k", type=int, default=8)
    p.add_argument("--max-chars", type=int, default=4000)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--stratified",
        action="store_true",
        help="sample evenly across question_type instead of first-N (the "
        "first 60 oracle questions are all temporal-reasoning)",
    )
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    load_dotenv("E:/Workspace/Parallax/.env")
    # Only Gemini answer/judge models need a Gemini key. Local (ollama:/local:)
    # or Claude runs must not be blocked by an absent GEMINI_API_KEY.
    needs_gemini = args.answer_model.startswith("gemini-") or args.judge_model.startswith(
        "gemini-"
    )
    if needs_gemini and not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not loaded", file=sys.stderr)
        return 2

    if args.stratified:
        questions = _stratified_slice(args.limit)
    else:
        questions = list(iter_questions(ORACLE, limit=args.limit))
    print(
        f"[run] slice={len(questions)}Q answer={args.answer_model} "
        f"judge={args.judge_model} top_k={args.top_k} max_chars={args.max_chars} "
        f"semantic={os.environ.get('PARALLAX_SEMANTIC_RETRIEVAL')} "
        f"embed={os.environ.get('PARALLAX_EMBEDDING_BASE_URL')}"
    )

    dump_recs, retr_recs = [], []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {
            ex.submit(
                _run_one, q, args.answer_model, args.judge_model,
                args.top_k, args.max_chars,
            ): q
            for q in questions
        }
        done = 0
        for fut in as_completed(futs):
            q = futs[fut]
            try:
                d, r = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  CRASH {q.question_id}: {exc}", file=sys.stderr)
                continue
            dump_recs.append(d)
            retr_recs.append(r)
            done += 1
            print(
                f"[{done}/{len(questions)}] {q.question_id[:18]} "
                f"dump={d['verdict']:<9}({d['answer_prompt_tokens']:>5}t) "
                f"retr={r['verdict']:<9}({r['answer_prompt_tokens']:>5}t)",
                flush=True,
            )

    dump_acc, dc, dn = _acc(dump_recs)
    retr_acc, rc, rn = _acc(retr_recs)
    dump_tok = _arm_tokens(dump_recs)
    retr_tok = _arm_tokens(retr_recs)
    ratio = dump_tok / max(1, retr_tok)
    delta_pp = (retr_acc - dump_acc) * 100

    interesting = abs(delta_pp) <= 3.0 and ratio >= 10.0

    summary = {
        "slice_n": len(questions),
        "answer_model": args.answer_model,
        "judge_model": args.judge_model,
        "top_k": args.top_k,
        "max_chars": args.max_chars,
        "dump": {
            "accuracy": round(dump_acc, 4),
            "correct": dc,
            "graded": dn,
            "answer_prompt_tokens": dump_tok,
            "verdicts": dict(Counter(r["verdict"] for r in dump_recs)),
        },
        "retrieval": {
            "accuracy": round(retr_acc, 4),
            "correct": rc,
            "graded": rn,
            "answer_prompt_tokens": retr_tok,
            "verdicts": dict(Counter(r["verdict"] for r in retr_recs)),
        },
        "delta_pp": round(delta_pp, 2),
        "token_ratio_dump_over_retr": round(ratio, 2),
        "pre_registered_interesting": interesting,
        "pre_registered_read": "interesting iff |delta| <= 3pp AND token_ratio >= 10x",
        "elapsed_sec": round(time.time() - t0, 1),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for d, r in zip(dump_recs, retr_recs, strict=False):
            f.write(json.dumps({"arm": "dump", **d}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"arm": "retrieval", **r}, ensure_ascii=False) + "\n")
    args.out.with_suffix(".summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("---")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
