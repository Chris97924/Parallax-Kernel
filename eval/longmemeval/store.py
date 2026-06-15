"""Per-question Parallax store helpers.

LongMemEval protocol resets memory between questions: each question carries
its own haystack, so we spin up an isolated SQLite file per question, ingest
every turn as a :class:`parallax.Memory`, and tear down at the end.

This v1 uses Parallax as a session-indexed memory store; no LLM claim
extraction is performed. Retrieval falls back to the full memory dump,
which a 1M-ctx answer model can consume directly. A v2 adapter can layer
:func:`parallax.extract.extract_and_ingest` on top for claim-level
retrieval without changing this surface.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from eval.longmemeval.dataset import Question
from parallax import ingest_memory, memories_by_user, migrate_to_latest
from parallax.retrieval.config import semantic_retrieval_enabled
from parallax.retrieval.embeddings import get_embedding_provider, has_live_embedding_provider
from parallax.retrieval.semantic import hybrid_rank
from parallax.sqlite_store import connect

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> set[str]:
    """Lowercase word-boundary tokenizer so "teal." matches "teal"."""
    return set(_WORD_RE.findall(text.lower()))


@contextmanager
def ephemeral_store() -> Iterator[sqlite3.Connection]:
    """Yield a fresh migrated Parallax DB that is deleted on exit."""
    with TemporaryDirectory(prefix="parallax_lme_") as tmpdir:
        db_path = Path(tmpdir) / "parallax.db"
        conn = connect(str(db_path))
        try:
            migrate_to_latest(conn)
            yield conn
        finally:
            conn.close()


def ingest_question(conn: sqlite3.Connection, q: Question) -> int:
    """Ingest every turn of a Question as a memory row. Returns turn count."""
    user_id = q.question_id
    n = 0
    for si, sess in enumerate(q.sessions):
        for ti, turn in enumerate(sess.turns):
            vault_path = f"lme/{q.question_id}/s{si}/t{ti}"
            title = f"[{sess.date}] {turn.role}"
            ingest_memory(
                conn,
                user_id=user_id,
                title=title,
                summary=turn.content,
                vault_path=vault_path,
            )
            n += 1
    return n


def dump_all_sessions(q: Question) -> str:
    """Render the full haystack as a single chronological transcript.

    Used by the v1 long-context answer strategy. Sessions are emitted in
    their listed order (not sorted by date — the dataset preserves the
    user's original arrival order, which matters for temporal questions).
    """
    blocks: list[str] = []
    for si, sess in enumerate(q.sessions):
        header = f"### Session {si + 1} — {sess.date} (id={sess.session_id})"
        lines = [header]
        for turn in sess.turns:
            lines.append(f"{turn.role.upper()}: {turn.content}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def dump_via_memories(conn: sqlite3.Connection, user_id: str) -> str:
    """Alternative: reconstruct transcript from Parallax memory rows.

    Proves the ingest round-trip is lossless — useful for smoke assertions.
    """
    rows = memories_by_user(conn, user_id)
    rows_sorted = sorted(rows, key=lambda r: r["vault_path"])
    return "\n".join(f"{r['title']}\n{r['summary']}" for r in rows_sorted)


def _select_rows_lexical(question: str, rows: list[dict], top_k: int) -> list[dict]:
    """BM25-stub selection — the pre-M8 default path (flag OFF).

    Score each row by lexical token overlap with the question, keep top_k.
    Ties break on vault_path ascending so reruns are deterministic. Behavior
    is byte-identical to the original inline implementation.
    """
    q_tokens = _tokenize(question)

    def _score(row: dict[str, object]) -> float:
        blob = f"{row.get('title') or ''} {row.get('summary') or ''}"
        tokens = _tokenize(blob)
        return len(q_tokens & tokens) / max(1, len(q_tokens))

    scored: list[tuple[float, dict]] = [(_score(r), r) for r in rows]
    # Primary: relevance descending. Secondary: vault_path ascending so
    # zero-score ties (and any other ties) resolve deterministically.
    scored.sort(key=lambda item: (-item[0], item[1].get("vault_path") or ""))
    return [r for _, r in scored[:top_k]]


def _select_rows_hybrid(question: str, rows: list[dict], top_k: int) -> list[dict]:
    """Hybrid lexical + dense (RRF) selection — M8 path (flag ON).

    When a live embedding provider is configured (``PARALLAX_EMBEDDING_BASE_URL``
    is set), embeds ``title + summary`` per row via bge-m3 on GB10 and fuses
    with lexical ranking via RRF. Ties break on vault_path ascending — same
    determinism contract as the lexical path.

    When no live provider is configured, falls back to lexical-only rather than
    fusing hash-random stub vectors into ranked results (stub fused at equal RRF
    weight can push exact lexical hits out of ``top_k``).

    A dense-side failure during a live call degrades to lexical-only inside
    ``hybrid_rank``.
    """
    if not has_live_embedding_provider():
        # No live Ollama URL — stub vectors carry no semantic signal.  Running
        # RRF fusion with a hash-random provider can corrupt retrieval results
        # by pushing strong lexical matches out of top_k.  Degrade to the
        # lexical path instead.
        logger.debug(
            "PARALLAX_EMBEDDING_BASE_URL not set; semantic retrieval "
            "degrades to lexical-only (no stub fusion in production)"
        )
        return _select_rows_lexical(question, rows, top_k)

    texts = [f"{r.get('title') or ''} {r.get('summary') or ''}".strip() for r in rows]
    # Lower tie_break value wins; rank rows by vault_path so ties resolve the
    # same way the lexical path does (ascending vault_path).
    order = sorted(range(len(rows)), key=lambda i: rows[i].get("vault_path") or "")
    tie_break = [0.0] * len(rows)
    for position, i in enumerate(order):
        tie_break[i] = float(position)

    provider = get_embedding_provider()
    ranked_idx = hybrid_rank(question, texts, provider, tie_break=tie_break)
    return [rows[i] for i in ranked_idx[:top_k]]


def build_from_parallax_retrieval(
    conn: sqlite3.Connection,
    q: Question,
    *,
    top_k: int = 64,
    max_chars: int = 40000,
) -> str:
    """Retrieval-filtered transcript — reads from Parallax store, not from ``q``.

    The v1 pipeline built the answer prompt from ``dump_all_sessions(q)``,
    which walks the in-memory Question tuple and therefore never exercises the
    Parallax store we just ingested into. That bypass hid any breakage in the
    ingest/retrieve round-trip behind a 1M-ctx answer model.

    This helper closes the loop:

    1. Fetch every ingested memory row via ``memories_by_user(conn, ...)``.
    2. Score each row by lexical overlap with ``q.question`` (BM25-stub — no
       model dependency, deterministic, fast).
    3. Keep the top ``top_k`` rows, stop early once ``max_chars`` is reached.
    4. Emit a chronological transcript ordered by ``vault_path`` so temporal
       questions keep the user's original arrival order.

    The defaults are generous (64 rows / 40K chars) so short haystacks pass
    through unfiltered; narrow them to actually exercise retrieval pressure.

    Contract notes:

    * Returns ``""`` when the store is empty. Callers that treat an empty
      transcript as valid context will silently pass junk to an answer model;
      ``run_one`` guards against this when ``use_retrieval=True``.
    * ``max_chars`` is the soft cap on cumulative ``len(block)`` across all
       blocks beyond the first. The first block is always emitted even if
       it alone exceeds the cap — otherwise a single oversized memory would
       produce an empty transcript, which is a worse failure mode.
    * Ties in the relevance score (common when the question has zero lexical
      overlap with a row) break deterministically on ``vault_path`` so
      reruns produce bit-identical transcripts.
    """
    user_id = q.question_id
    rows = memories_by_user(conn, user_id)
    if not rows:
        return ""

    if semantic_retrieval_enabled():
        kept = _select_rows_hybrid(q.question, rows, top_k)
    else:
        kept = _select_rows_lexical(q.question, rows, top_k)

    # Restore chronological order for the emitted transcript — relevance is used
    # only to choose which rows survive the top_k + char budget.
    kept.sort(key=lambda r: r.get("vault_path") or "")

    lines: list[str] = []
    total = 0
    for r in kept:
        title = r.get("title") or ""
        summary = r.get("summary") or ""
        block = f"{title}\n{summary}".strip()
        if not block:
            continue
        if total + len(block) > max_chars and lines:
            break
        lines.append(block)
        total += len(block)
    return "\n\n".join(lines)
