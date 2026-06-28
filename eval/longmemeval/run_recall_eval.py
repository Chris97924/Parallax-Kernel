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

.. warning::

   **The ``oracle`` split saturates recall@k at 1.0 by construction.** The
   oracle corpus lists ONLY each question's answer sessions, so every ingested
   turn is oracle-positive (positive fraction = 1.0). With nothing but
   positives to retrieve, *any* top-k slice is a hit and recall@k = 1.0 for
   every k — it cannot measure ranking quality. For a DISCRIMINATING number:

   * Prefer a non-oracle split that carries distractor sessions
     (``--split s`` / ``--split m``); positive fraction drops far below 1.0
     and recall@k becomes sensitive to k.
   * If only the oracle split is present, pass ``--distractors N`` to inject
     ``N`` non-answer sessions (borrowed from other questions) into every
     question, which likewise breaks saturation.

   The harness always emits a ``recall_curve`` (recall@1 .. recall@top_k), the
   ``positive_fraction``, and a ``discriminating`` flag (true when
   recall@1 < recall@top_k) so saturation is visible in the results JSON.

Usage::

    # Discriminating run on the real distractor-laden split (preferred):
    LONGMEMEVAL_DATA_DIR=E:/Workspace/longmemeval/data \\
        python -m eval.longmemeval.run_recall_eval --split s --top-k 50

    # Oracle-only fallback: inject distractors to break 1.0 saturation:
    python -m eval.longmemeval.run_recall_eval --distractors 8 --top-k 50
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from eval.longmemeval.dataset import Question, Session, iter_questions, load_dataset
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

# Session-id prefix for injected distractor sessions. Chosen so it can never
# collide with a real ``answer_session_ids`` entry, which keeps the injected
# sessions strictly oracle-NEGATIVE in ``oracle_positive_paths``.
DISTRACTOR_PREFIX = "distractor::"


def augment_with_distractors(
    questions: list[Question], n_distractors: int, *, seed: int = 0
) -> list[Question]:
    """Inject non-answer sessions into each question to break oracle saturation.

    The oracle split lists ONLY answer sessions, so every turn is
    oracle-positive (positive fraction = 1.0) and recall@k saturates at 1.0 for
    every k. This rebuilds each question with up to ``n_distractors`` extra
    sessions borrowed from OTHER questions in the slice and re-labelled as
    non-answer:

    * a unique session id prefixed with :data:`DISTRACTOR_PREFIX` (never in
      ``answer_session_ids``), and
    * every borrowed turn forced to ``has_answer=False``.

    So the injected sessions are guaranteed oracle-negative — they enlarge the
    haystack (and the recall denominator's positive *fraction*) without ever
    counting as a recallable answer. The original answer sessions keep their
    leading indices, so their oracle-positive ``vault_path`` set is unchanged.

    ``n_distractors <= 0`` is a no-op (returns the input questions unchanged).
    The borrowed-session pool is shuffled with a fixed ``seed`` so the result
    is deterministic and reproducible across runs and machines.
    """
    if n_distractors <= 0:
        return list(questions)
    n = len(questions)
    out: list[Question] = []
    for i, q in enumerate(questions):
        # Deterministic candidate pool: every session from every OTHER question,
        # walked in rotated order so each question draws a different mix.
        pool: list[Session] = []
        for offset in range(1, n):
            other = questions[(i + offset) % n]
            pool.extend(other.sessions)
        random.Random(seed + i).shuffle(pool)
        distractors = tuple(
            Session(
                session_id=f"{DISTRACTOR_PREFIX}{q.question_id}:{d_idx}",
                date=sess.date,
                turns=tuple(t._replace(has_answer=False) for t in sess.turns),
            )
            for d_idx, sess in enumerate(pool[:n_distractors])
        )
        out.append(q._replace(sessions=q.sessions + distractors))
    return out


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


@dataclass(frozen=True)
class RecallCurve:
    """Recall@k at several ``k`` from a SINGLE ingest pass per question.

    ``results`` maps each requested ``k`` to its :class:`RecallResult`.
    ``positive_fraction`` is the mean (over *scored* questions) of
    ``len(oracle_positive_paths) / total_turns`` — it reads 1.0 on the oracle
    split (saturated) and well below 1.0 once distractors are present, so it is
    the headline signal that the recall number is actually discriminating.
    """

    results: dict[int, RecallResult]
    positive_fraction: float
    scored: int
    skipped: int


def recall_curve(
    questions: list[Question], k_values: list[int], *, use_hybrid: bool
) -> RecallCurve:
    """Compute recall at every ``k`` in ``k_values`` with one ingest per question.

    The selector is asked for the top ``max(k_values)`` rows once; recall at
    each smaller ``k`` is read off the same ranked prefix (``kept_paths[:k]``),
    which is byte-identical to calling the selector with that ``k`` because both
    selectors rank-then-truncate. This keeps the heavy SQLite ingest off the
    hot loop so a full recall curve costs the same as a single ``recall_at_k``.

    ``use_hybrid=True`` routes through ``_select_rows_hybrid`` (which itself
    degrades to lexical when no live embedding provider is configured), so
    hybrid recall is always >= lexical recall in the offline gate.
    """
    select_fn = _select_rows_hybrid if use_hybrid else _select_rows_lexical
    ks = sorted({k for k in k_values if k > 0})
    if not ks:
        raise ValueError("k_values must contain at least one positive k")
    max_k = ks[-1]
    hits = dict.fromkeys(ks, 0)
    scored = 0
    skipped = 0
    pos_fraction_sum = 0.0
    for q in questions:
        positives = oracle_positive_paths(q)
        if not positives:
            # No oracle-positive turn — nothing to recall, exclude from
            # denominator rather than scoring it as a guaranteed miss.
            skipped += 1
            continue
        scored += 1
        total_turns = sum(len(s.turns) for s in q.sessions)
        if total_turns:
            pos_fraction_sum += len(positives) / total_turns
        with ephemeral_store() as conn:
            ingest_question(conn, q)
            rows = memories_by_user(conn, q.question_id)
            kept = select_fn(q.question, rows, max_k)
        kept_paths = [r.get("vault_path") for r in kept]
        for k in ks:
            if set(kept_paths[:k]) & positives:
                hits[k] += 1
    results = {
        k: RecallResult(
            recall=hits[k] / scored if scored else 0.0,
            hits=hits[k],
            scored=scored,
            skipped=skipped,
        )
        for k in ks
    }
    positive_fraction = pos_fraction_sum / scored if scored else 0.0
    return RecallCurve(
        results=results,
        positive_fraction=positive_fraction,
        scored=scored,
        skipped=skipped,
    )


def recall_at_k(
    questions: list[Question], top_k: int, *, use_hybrid: bool
) -> RecallResult:
    """Compute recall@k for ``questions`` using the chosen selector.

    Thin wrapper over :func:`recall_curve` for the single-``k`` case; kept as
    the stable entry point used by callers that only need one cut-off.
    """
    return recall_curve(questions, [top_k], use_hybrid=use_hybrid).results[top_k]


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
        "--distractors",
        type=int,
        default=0,
        metavar="N",
        help=(
            "inject N non-answer sessions per question to break oracle "
            "saturation (recommended only when no s/m split is available)"
        ),
    )
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

    if args.distractors > 0:
        questions = augment_with_distractors(questions, args.distractors)

    if args.split == "oracle" and args.distractors <= 0:
        print(
            "[recall] WARNING: the oracle split lists ONLY answer sessions, so "
            "recall@k saturates at 1.0 (positive_fraction=1.0) and does NOT "
            "measure ranking. Use --split s/m or --distractors N for a "
            "discriminating number.",
            file=sys.stderr,
            flush=True,
        )

    # Always evaluate at k=1 (ranking is load-bearing) up to the requested
    # top_k so the curve exposes whether recall actually responds to k.
    k_values = sorted({1, args.top_k})

    hybrid_enabled = has_live_embedding_provider()
    print(
        f"[recall] split={args.split} slice={len(questions)}Q top_k={args.top_k} "
        f"distractors={args.distractors} stratified={args.stratified} "
        f"hybrid_enabled={hybrid_enabled} "
        f"embed={os.environ.get('PARALLAX_EMBEDDING_BASE_URL')}",
        flush=True,
    )

    t0 = time.time()
    lex = recall_curve(questions, k_values, use_hybrid=False)
    hyb = recall_curve(questions, k_values, use_hybrid=True) if hybrid_enabled else None

    top_k = args.top_k
    recall_at_1 = lex.results[1].recall
    recall_at_top = lex.results[top_k].recall
    discriminating = recall_at_1 < recall_at_top

    summary: dict[str, object] = {
        "split": args.split,
        "distractors": args.distractors,
        "slice_n": len(questions),
        "scored_n": lex.scored,
        "skipped_n": lex.skipped,
        "top_k": top_k,
        "stratified": args.stratified,
        "type_mix": dict(Counter(q.question_type for q in questions)),
        "positive_fraction": round(lex.positive_fraction, 4),
        "lexical_recall_curve": {
            str(k): round(lex.results[k].recall, 4) for k in k_values
        },
        "lexical_recall_at_1": round(recall_at_1, 4),
        "lexical_recall_at_k": round(recall_at_top, 4),
        "lexical_hits": lex.results[top_k].hits,
        "discriminating": discriminating,
        "hybrid_enabled": hybrid_enabled,
        "hybrid_recall_curve": (
            {str(k): round(hyb.results[k].recall, 4) for k in k_values}
            if hyb
            else None
        ),
        "hybrid_recall_at_k": round(hyb.results[top_k].recall, 4) if hyb else None,
        "hybrid_hits": hyb.results[top_k].hits if hyb else None,
        "elapsed_sec": round(time.time() - t0, 1),
    }

    tag = args.split + (f"_d{args.distractors}" if args.distractors > 0 else "")
    out = args.out or (REPO_ROOT / "eval" / "results" / f"recall_{tag}.json")
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
