"""M8 retrieval-quality harness: hybrid (BM25 + bge-m3 + RRF) vs lexical-only.

The headline open thread for M8 is that the hybrid retrieval path has *never*
been evaluated end-to-end for ranking quality — every LongMemEval result so far
ran with ``use_retrieval=False``, so the flag-gated hybrid selector
(:func:`eval.longmemeval.store._select_rows_hybrid`) shipped untested against a
real quality bar. This file is that bar.

The corpus is a small labelled fixture, not an external dataset. One row is the
**answer-bearing** row: it is *lexically weak* (it shares no salient content
token with the question — only an unavoidable stopword) yet *semantically
strong* (it is the unique topical match). A second row is a deliberate
**lexical distractor**: it shares several rare, high-IDF question tokens used in
an unrelated sense (so lexical scoring ranks it first) while being semantically
off-topic.

Two layers, mirroring the rest of the M8 suite:

* **Offline (runs in the default gate).** A controllable embedding provider
  reproduces the bge-m3 ranking shape deterministically — no network. It proves
  the contract: lexical-only selects the *wrong* row at ``top_k=1`` while the
  hybrid path selects the answer-bearing row. This is the regression guard.

* **Live (skipped by default; GB10-gated).** The same fixture is run through the
  real ``OllamaEmbeddingProvider`` against live bge-m3 on GB10, exercising the
  actual M8 production path. This is the genuine end-to-end hybrid-vs-lexical
  evaluation. Run it with::

      PARALLAX_EMBEDDING_LIVE=1 \\
      PARALLAX_EMBEDDING_BASE_URL=http://192.168.1.134:11434 \\
      pytest tests/eval/test_hybrid_vs_lexical_quality.py -m integration

The live fixture strings were validated against live bge-m3 (2026-06-20): the
answer row embeds to cosine ~0.554 vs the question, the distractor ~0.486, and
the rankings below reproduce deterministically across repeated calls.
"""

from __future__ import annotations

import os

import pytest

from eval.longmemeval.dataset import Question, Session, Turn
from eval.longmemeval.store import (
    _select_rows_hybrid,
    _select_rows_lexical,
    build_from_parallax_retrieval,
    ephemeral_store,
    ingest_question,
)
from parallax.retrieval.config import SEMANTIC_RETRIEVAL_ENV
from parallax.retrieval.embeddings import EMBEDDING_BASE_URL_ENV

# ---------------------------------------------------------------------------
# The labelled fixture corpus.
#
# The question semantically asks "which programming language am I learning?".
# The answer row is about Rust/the borrow checker — the unique topical match —
# but shares NO content token with the question (only the stopword "I"). The
# distractor row reuses the rare question tokens {kernel, memory, strict} in a
# childhood-popcorn sense, so lexical IDF scoring ranks it FIRST even though it
# is semantically irrelevant.
# ---------------------------------------------------------------------------

QUESTION_TEXT = "Which systems language with a strict kernel memory model am I picking up lately?"

# vault_path of the answer-bearing row and the lexical distractor.
ANSWER_VAULT = "lme/q_hybrid_quality/s0/t0"
DISTRACTOR_VAULT = "lme/q_hybrid_quality/s1/t0"

ANSWER_SUMMARY = (
    "Rust has become my new obsession; every night I wrestle its borrow "
    "checker to write faster code."
)
DISTRACTOR_SUMMARY = (
    "A fond childhood memory the corn kernel popping was our strict bedtime "
    "movie ritual."
)
NOISE_SUMMARIES = (
    "Our roast duck finally hit crispy skin after resting uncovered overnight in the fridge.",
    "The hiking group rescheduled the ridge trail because of the thunderstorm warning.",
)


def _fixture_question() -> Question:
    """A Question whose answer row is lexically weak but semantically strong."""
    turns_meta = [
        ("2026-01-01", ANSWER_SUMMARY, True),
        ("2026-01-02", DISTRACTOR_SUMMARY, False),
        ("2026-01-03", NOISE_SUMMARIES[0], False),
        ("2026-01-04", NOISE_SUMMARIES[1], False),
    ]
    sessions = tuple(
        Session(
            session_id=f"s{i}",
            date=date,
            turns=(Turn(role="user", content=content, has_answer=has_answer),),
        )
        for i, (date, content, has_answer) in enumerate(turns_meta)
    )
    return Question(
        question_id="q_hybrid_quality",
        question_type="single-session-user",
        question=QUESTION_TEXT,
        answer="Rust",
        question_date="2026-02-01",
        sessions=sessions,
        answer_session_ids=("s0",),
    )


def _rows_from_question(q: Question) -> list[dict]:
    """Build the store row dicts the selector consumes, in ingest order."""
    rows: list[dict] = []
    for si, sess in enumerate(q.sessions):
        for ti, turn in enumerate(sess.turns):
            rows.append(
                {
                    "vault_path": f"lme/{q.question_id}/s{si}/t{ti}",
                    "title": f"[{sess.date}] {turn.role}",
                    "summary": turn.content,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Controllable offline embedding provider.
#
# Reproduces the bge-m3 ranking shape deterministically: the answer row is the
# closest vector to the query, the distractor second, noise rows far. No
# network, so this runs in the default gate as the regression guard.
# ---------------------------------------------------------------------------


class _LabelledProvider:
    """Maps each fixture text to a hand-tuned vector matching live bge-m3 order.

    Query is closest to the answer row (cosine ~0.55 against live bge-m3), then
    the distractor (~0.49), then the noise rows (~0.31). The geometry below
    preserves that strict ordering with a clear margin so the offline contract
    does not hinge on floating-point ties.

    The store embeds ``"{title} {summary}"`` (date-prefixed title + summary),
    not the bare summary, so matching is by *substring*: each candidate text is
    classified by which known summary it contains. The query is matched by
    exact identity. An unrecognized text raises — a silently-misvectored row
    would make the test lie.
    """

    dim = 3
    id = "labelled-test"

    _QUERY_VEC = [1.0, 0.0, 0.0]
    # (substring marker, vector). Ordered so the answer is nearest the query.
    _ROW_VECS = (
        (ANSWER_SUMMARY, [0.96, 0.28, 0.0]),  # nearest to query
        (DISTRACTOR_SUMMARY, [0.80, 0.60, 0.0]),  # second
        (NOISE_SUMMARIES[0], [0.30, 0.0, 0.95]),  # far
        (NOISE_SUMMARIES[1], [0.20, 0.0, 0.98]),  # far
    )

    def embed(self, texts):
        return [self._vec_for(t) for t in texts]

    def _vec_for(self, text: str) -> list[float]:
        if text == QUESTION_TEXT:
            return list(self._QUERY_VEC)
        for marker, vec in self._ROW_VECS:
            if marker in text:
                return list(vec)
        raise KeyError(f"_LabelledProvider has no vector for: {text!r}")


# ---------------------------------------------------------------------------
# Offline contract — runs in the default gate (no GB10).
# ---------------------------------------------------------------------------


def test_offline_lexical_ranks_distractor_above_answer(monkeypatch):
    """Sanity: lexical-only buries the answer below the lexical distractor.

    If this ever flips, the fixture has lost its teeth — the hybrid win below
    would become trivial. The distractor shares three high-IDF question tokens
    (kernel/memory/strict); the answer shares only the stopword "i".
    """
    q = _fixture_question()
    rows = _rows_from_question(q)
    lexical_order = [r["vault_path"] for r in _select_rows_lexical(q.question, rows, top_k=4)]
    assert lexical_order.index(DISTRACTOR_VAULT) < lexical_order.index(ANSWER_VAULT)
    # And at top_k=1, lexical-only returns the WRONG row.
    top1 = _select_rows_lexical(q.question, rows, top_k=1)
    assert top1[0]["vault_path"] == DISTRACTOR_VAULT


def test_offline_hybrid_ranks_answer_above_lexical(monkeypatch):
    """Headline: the hybrid path surfaces the answer row above the lexical path.

    With a live (here, controllable) embedding provider, the dense leg pulls the
    semantically-strong answer row to rank 1 in RRF fusion, beating the lexical
    distractor that pure lexical scoring put first.
    """
    from eval.longmemeval import store as store_mod

    monkeypatch.setenv(EMBEDDING_BASE_URL_ENV, "http://gb10.test:11434")
    monkeypatch.setattr(store_mod, "get_embedding_provider", lambda: _LabelledProvider())

    q = _fixture_question()
    rows = _rows_from_question(q)

    hybrid_order = [r["vault_path"] for r in _select_rows_hybrid(q.question, rows, top_k=4)]
    lexical_order = [r["vault_path"] for r in _select_rows_lexical(q.question, rows, top_k=4)]

    # The answer row strictly outranks where lexical-only placed it...
    assert hybrid_order.index(ANSWER_VAULT) < lexical_order.index(ANSWER_VAULT)
    # ...and lands at rank 1 under fusion.
    assert hybrid_order[0] == ANSWER_VAULT
    # ...above the lexical distractor it previously lost to.
    assert hybrid_order.index(ANSWER_VAULT) < hybrid_order.index(DISTRACTOR_VAULT)


def test_offline_hybrid_top1_selects_answer_lexical_does_not(monkeypatch):
    """The decisive single-row contrast at ``top_k=1``.

    Lexical-only selects the distractor (wrong); hybrid selects the answer
    (right). This is the difference M8 was built to make.
    """
    from eval.longmemeval import store as store_mod

    monkeypatch.setenv(EMBEDDING_BASE_URL_ENV, "http://gb10.test:11434")
    monkeypatch.setattr(store_mod, "get_embedding_provider", lambda: _LabelledProvider())

    q = _fixture_question()
    rows = _rows_from_question(q)

    lexical_top1 = _select_rows_lexical(q.question, rows, top_k=1)[0]["vault_path"]
    hybrid_top1 = _select_rows_hybrid(q.question, rows, top_k=1)[0]["vault_path"]

    assert lexical_top1 == DISTRACTOR_VAULT
    assert hybrid_top1 == ANSWER_VAULT


def test_offline_hybrid_transcript_includes_answer_at_tight_top_k(monkeypatch):
    """End-to-end through the store: a tight top_k keeps the answer only on hybrid.

    With ``top_k=1`` the lexical transcript carries the distractor's text and
    omits the answer; the hybrid transcript carries the answer's text. This is
    the property a downstream answer model actually depends on.
    """
    from eval.longmemeval import store as store_mod

    q = _fixture_question()

    # Lexical path (flag OFF): transcript should NOT contain the answer text.
    monkeypatch.delenv(SEMANTIC_RETRIEVAL_ENV, raising=False)
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        lexical_tx = build_from_parallax_retrieval(conn, q, top_k=1)
    assert "borrow" not in lexical_tx.lower()  # answer text absent
    assert "popping" in lexical_tx.lower()  # distractor text present

    # Hybrid path (flag ON + live provider): transcript SHOULD contain the answer.
    monkeypatch.setenv(SEMANTIC_RETRIEVAL_ENV, "1")
    monkeypatch.setenv(EMBEDDING_BASE_URL_ENV, "http://gb10.test:11434")
    monkeypatch.setattr(store_mod, "get_embedding_provider", lambda: _LabelledProvider())
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        hybrid_tx = build_from_parallax_retrieval(conn, q, top_k=1)
    assert "borrow" in hybrid_tx.lower()  # answer text present


def test_offline_hybrid_degrades_to_lexical_when_provider_down(monkeypatch):
    """If GB10 is unreachable mid-eval the harness must NOT crash.

    A live provider that raises on every call must let the hybrid selector fall
    back to the lexical ordering — never propagate the error. (This is the
    failure mode the real GB10 server can hit: certain inputs 500 the embed
    endpoint, which surfaces as EmbeddingError.)
    """
    from eval.longmemeval import store as store_mod
    from parallax.retrieval.embeddings import EmbeddingError

    monkeypatch.setenv(EMBEDDING_BASE_URL_ENV, "http://gb10.test:11434")

    class _BoomProvider:
        dim = 3
        id = "boom"

        def embed(self, texts):
            raise EmbeddingError("embed endpoint 500")

    monkeypatch.setattr(store_mod, "get_embedding_provider", lambda: _BoomProvider())

    q = _fixture_question()
    rows = _rows_from_question(q)
    hybrid_order = [r["vault_path"] for r in _select_rows_hybrid(q.question, rows, top_k=4)]
    lexical_order = [r["vault_path"] for r in _select_rows_lexical(q.question, rows, top_k=4)]
    # Degraded cleanly to lexical ordering (distractor still first, no crash).
    assert hybrid_order == lexical_order


# ---------------------------------------------------------------------------
# Live bge-m3 on GB10 — the genuine end-to-end M8 hybrid quality evaluation.
# Skipped unless PARALLAX_EMBEDDING_LIVE is set so the default gate has no GB10
# dependency. Marked ``integration`` like the existing live smoke test.
# ---------------------------------------------------------------------------

_LIVE = os.environ.get("PARALLAX_EMBEDDING_LIVE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@pytest.mark.integration
@pytest.mark.skipif(not _LIVE, reason="set PARALLAX_EMBEDDING_LIVE=1 to hit GB10 bge-m3")
def test_live_bge_m3_hybrid_ranks_answer_above_lexical(monkeypatch):
    """REAL M8 path: live bge-m3 hybrid must rank the answer above lexical-only.

    This is the evaluation that had never been run: the flag-gated hybrid
    selector against live embeddings, scored for ranking quality. With bge-m3
    on GB10 the dense leg lifts the lexically-weak Rust row to rank 1, beating
    the lexical distractor that pure-lexical scoring placed first.
    """
    base_url = os.environ.get("PARALLAX_EMBEDDING_BASE_URL", "http://192.168.1.134:11434")
    monkeypatch.setenv(SEMANTIC_RETRIEVAL_ENV, "1")
    monkeypatch.setenv(EMBEDDING_BASE_URL_ENV, base_url)

    q = _fixture_question()
    rows = _rows_from_question(q)

    lexical_order = [r["vault_path"] for r in _select_rows_lexical(q.question, rows, top_k=4)]
    hybrid_order = [r["vault_path"] for r in _select_rows_hybrid(q.question, rows, top_k=4)]

    # Fixture must keep its teeth against the live model: lexical buries the answer.
    assert lexical_order.index(DISTRACTOR_VAULT) < lexical_order.index(ANSWER_VAULT), (
        "lexical fixture lost its teeth against live bge-m3; "
        f"lexical_order={lexical_order}"
    )
    # The genuine quality win: hybrid surfaces the answer above where lexical put it.
    assert hybrid_order.index(ANSWER_VAULT) < lexical_order.index(ANSWER_VAULT), (
        f"hybrid did not improve answer rank; "
        f"lexical={lexical_order} hybrid={hybrid_order}"
    )
    # And to rank 1.
    assert hybrid_order[0] == ANSWER_VAULT, (
        f"hybrid did not rank the answer first; hybrid_order={hybrid_order}"
    )


@pytest.mark.integration
@pytest.mark.skipif(not _LIVE, reason="set PARALLAX_EMBEDDING_LIVE=1 to hit GB10 bge-m3")
def test_live_bge_m3_hybrid_top1_selects_answer(monkeypatch):
    """Live ``top_k=1``: hybrid selects the answer row; lexical selects the distractor."""
    base_url = os.environ.get("PARALLAX_EMBEDDING_BASE_URL", "http://192.168.1.134:11434")
    monkeypatch.setenv(SEMANTIC_RETRIEVAL_ENV, "1")
    monkeypatch.setenv(EMBEDDING_BASE_URL_ENV, base_url)

    q = _fixture_question()
    rows = _rows_from_question(q)

    lexical_top1 = _select_rows_lexical(q.question, rows, top_k=1)[0]["vault_path"]
    hybrid_top1 = _select_rows_hybrid(q.question, rows, top_k=1)[0]["vault_path"]

    assert lexical_top1 == DISTRACTOR_VAULT
    assert hybrid_top1 == ANSWER_VAULT
