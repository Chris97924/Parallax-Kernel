"""Offline retrieval recall@k harness — zero LLM / zero token burn.

Measures how often the ephemeral-store retrieval path surfaces an oracle
answer turn, WITHOUT calling any answer/judge model. For each question we:

1. Ingest every turn into a throwaway SQLite store (``ephemeral_store``).
2. Read the rows back via ``memories_by_user`` and rank them with the SAME
   selectors the production-eval path uses:
   * ``eval.longmemeval.store._select_rows_lexical`` (always),
   * ``eval.longmemeval.store._select_rows_hybrid`` (only when a live
     embedding provider is configured — offline it degrades to lexical, so
     hybrid recall is reported as ``null`` rather than a duplicate number).
3. Score recall@k = fraction of *scorable* questions whose top-k rows include
   at least one row that maps to an oracle answer: a turn in an
   ``answer_session_ids`` session, OR a turn flagged ``has_answer: true``.

This is the retrieval-quality counterpart to ``run_retrieval_vs_dump.py``
(which measures QA accuracy + token cost via Gemini). This module makes NO
network calls in the default offline gate, so it is safe for CI and for burn
lanes that must not spend tokens.

Read-only on source data: each question ingests into a per-question
``TemporaryDirectory`` SQLite DB (INSERT-only), torn down immediately. The
oracle corpus JSON is never mutated.

Usage::

    LONGMEMEVAL_DATA_DIR=E:/Workspace/longmemeval/data \\
        python -m eval.longmemeval.run_recall_eval --limit 100 --stratified
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from eval.longmemeval.dataset import Question, iter_questions, load_dataset
from eval.longmemeval.store import (
    _select_rows_hybrid,
    _select_rows_lexical,
    ephemeral_store,
    has_live_embedding_provider,
    ingest_question,
)
from parallax import memories_by_user

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("LONGMEMEVAL_DATA_DIR", "E:/Workspace/longmemeval/data"))

SPLIT_FILES = {
    "oracle": DATA_DIR / "longmemeval_oracle.json",
    "s": DATA_DIR / "longmemeval_s_cleaned.json",
    "m": DATA_DIR / "longmemeval_m_cleaned.json",
}


def oracle_positive_paths(q: Question) -> set[str]:
    """Vault paths that map to an oracle answer for ``q``.

    A turn is oracle-positive when EITHER its session id is listed in
    ``answer_session_ids`` OR the turn itself carries ``has_answer: true``.
    The path format mirrors ``ingest_question`` exactly
    (``lme/{question_id}/s{si}/t{ti}``) so the returned set can be intersected
    directly with the ``vault_path`` of retrieved rows.
    """
    ans_sids = set(q.answer_session_ids)
    positive: set[str] = set()
    for si, sess in enumerate(q.sessions):
        sess_is_answer = sess.session_id in ans_sids
        for ti, turn in enumerate(sess.turns):
            if sess_is_answer or turn.has_answer:
                positive.add(f"lme/{q.question_id}/s{si}/t{ti}")
    return positive


@dataclass(frozen=True)
class RecallResult:
    """Aggregate recall@k over a question slice.

    ``scored`` excludes questions with no oracle-positive turn (nothing to
    recall), so ``recall`` is hits over the *scorable* denominator.
    """

    recall: float
    hits: int
    scored: int
    skipped: int


def recall_at_k(
    questions: list[Question], top_k: int, *, use_hybrid: bool
) -> RecallResult:
    """Compute recall@k for ``questions`` using the chosen selector.

    ``use_hybrid=True`` routes through ``_select_rows_hybrid`` (which itself
    degrades to lexical when no live embedding provider is configured), so
    hybrid recall is always >= lexical recall in the offline gate by
    construction — never worse.
    """
    select_fn = _select_rows_hybrid if use_hybrid else _select_rows_lexical
    hits = 0
    scored = 0
    skipped = 0
    for q in questions:
        positives = oracle_positive_paths(q)
        if not positives:
            # No oracle-positive turn — nothing to recall, exclude from
            # denominator rather than scoring it as a guaranteed miss.
            skipped += 1
            continue
        scored += 1
        with ephemeral_store() as conn:
            ingest_question(conn, q)
            rows = memories_by_user(conn, q.question_id)
            kept = select_fn(q.question, rows, top_k)
        kept_paths = {r.get("vault_path") for r in kept}
        if kept_paths & positives:
            hits += 1
    recall = hits / scored if scored else 0.0
    return RecallResult(recall=recall, hits=hits, scored=scored, skipped=skipped)


def stratified_slice(questions: list[Question], limit: int) -> list[Question]:
    """Deterministic round-robin across ``question_type``.

    Mirrors ``run_retrieval_vs_dump._stratified_slice`` so the slice reflects
    the corpus mix instead of over-weighting whichever type leads the file.
    """
    by_type: dict[str, list[Question]] = {}
    for q in questions:
        by_type.setdefault(q.question_type, []).append(q)
    types = sorted(by_type)
    out: list[Question] = []
    idx = 0
    while len(out) < limit and any(idx < len(by_type[t]) for t in types):
        for t in types:
            if idx < len(by_type[t]) and len(out) < limit:
                out.append(by_type[t][idx])
        idx += 1
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--split", choices=sorted(SPLIT_FILES), default="oracle")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument(
        "--stratified",
        action="store_true",
        help="sample evenly across question_type instead of first-N",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="results JSON path (default eval/results/recall_<split>.json)",
    )
    args = p.parse_args(argv)

    split_path = SPLIT_FILES[args.split]
    if not split_path.exists():
        print(
            f"ERROR: split file not found: {split_path}\n"
            f"Set LONGMEMEVAL_DATA_DIR (currently {DATA_DIR}).",
            file=sys.stderr,
        )
        return 2

    if args.stratified:
        questions = stratified_slice(load_dataset(split_path), args.limit)
    else:
        questions = list(iter_questions(split_path, limit=args.limit))

    hybrid_enabled = has_live_embedding_provider()
    print(
        f"[recall] split={args.split} slice={len(questions)}Q top_k={args.top_k} "
        f"stratified={args.stratified} hybrid_enabled={hybrid_enabled} "
        f"embed={os.environ.get('PARALLAX_EMBEDDING_BASE_URL')}",
        flush=True,
    )

    t0 = time.time()
    lex = recall_at_k(questions, args.top_k, use_hybrid=False)
    hyb = (
        recall_at_k(questions, args.top_k, use_hybrid=True) if hybrid_enabled else None
    )

    summary: dict[str, object] = {
        "split": args.split,
        "slice_n": len(questions),
        "scored_n": lex.scored,
        "skipped_n": lex.skipped,
        "top_k": args.top_k,
        "stratified": args.stratified,
        "type_mix": dict(Counter(q.question_type for q in questions)),
        "lexical_recall_at_k": round(lex.recall, 4),
        "lexical_hits": lex.hits,
        "hybrid_enabled": hybrid_enabled,
        "hybrid_recall_at_k": round(hyb.recall, 4) if hyb else None,
        "hybrid_hits": hyb.hits if hyb else None,
        "elapsed_sec": round(time.time() - t0, 1),
    }

    out = args.out or (REPO_ROOT / "eval" / "results" / f"recall_{args.split}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("---")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[recall] wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
