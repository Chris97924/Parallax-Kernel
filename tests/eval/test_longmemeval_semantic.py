"""M8 flag-gated hybrid retrieval wiring in the LongMemEval store.

Two contracts:

* **Flag OFF parity** — output is byte-identical to the pre-M8 lexical path.
* **Flag ON** — the hybrid path runs (stub provider, offline), preserving the
  store's existing invariants (non-empty, chronological, top_k/budget,
  determinism, NULL-safe) and degrading to lexical-only on provider failure.

No LLM and no network — DeterministicStubProvider backs the ON path.
"""

from __future__ import annotations

import pytest

from eval.longmemeval.dataset import Question, Session, Turn
from eval.longmemeval.store import (
    build_from_parallax_retrieval,
    ephemeral_store,
    ingest_question,
)
from parallax.retrieval.config import SEMANTIC_RETRIEVAL_ENV


def _fixture_question() -> Question:
    sessions = (
        Session(
            session_id="s1",
            date="2026-01-01",
            turns=(
                Turn(role="user", content="My favourite colour is teal.", has_answer=True),
                Turn(role="assistant", content="Teal noted.", has_answer=False),
            ),
        ),
        Session(
            session_id="s2",
            date="2026-01-02",
            turns=(
                Turn(
                    role="user",
                    content="I just adopted a tabby cat named Mochi.",
                    has_answer=False,
                ),
                Turn(role="assistant", content="Congrats on Mochi!", has_answer=False),
            ),
        ),
    )
    return Question(
        question_id="q_semantic_smoke",
        question_type="single-session-user",
        question="What colour did I say I liked?",
        answer="teal",
        question_date="2026-02-01",
        sessions=sessions,
        answer_session_ids=("s1",),
    )


# ---------------------------------------------------------------------------
# Flag OFF parity — the regression proof.
# ---------------------------------------------------------------------------


def test_flag_off_is_byte_identical_to_lexical(monkeypatch):
    """With the flag OFF (and unset), output matches the pre-M8 lexical path."""
    q = _fixture_question()
    monkeypatch.delenv(SEMANTIC_RETRIEVAL_ENV, raising=False)
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        off_default = build_from_parallax_retrieval(conn, q)

    monkeypatch.setenv(SEMANTIC_RETRIEVAL_ENV, "false")
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        off_explicit = build_from_parallax_retrieval(conn, q)

    assert off_default == off_explicit
    # The teal row must be present and ordered before Mochi (chronological).
    assert "teal" in off_default.lower()
    s1 = off_default.find("Teal noted")
    s2 = off_default.find("Mochi")
    assert s1 != -1 and s2 != -1 and s1 < s2


# ---------------------------------------------------------------------------
# Flag ON — hybrid path runs, invariants preserved.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _flag_on(monkeypatch):
    monkeypatch.setenv(SEMANTIC_RETRIEVAL_ENV, "1")
    # No PARALLAX_EMBEDDING_BASE_URL → no live provider → degrades to lexical
    # (stub vectors must not participate in production ranked fusion).
    monkeypatch.delenv("PARALLAX_EMBEDDING_BASE_URL", raising=False)


def test_flag_on_produces_nonempty_chronological_transcript(_flag_on):
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q)
    assert transcript, "hybrid path must return a non-empty transcript"
    # Store-side title prefix proves it read through the store.
    assert "[2026-01-01] user" in transcript
    # Chronological emission preserved regardless of relevance ranking.
    s1 = transcript.find("Teal noted")
    s2 = transcript.find("Mochi")
    assert s1 != -1 and s2 != -1 and s1 < s2


def test_flag_on_respects_top_k(_flag_on):
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q, top_k=1)
    blocks = [b for b in transcript.split("\n\n") if b.strip()]
    assert len(blocks) == 1


def test_flag_on_respects_char_budget(_flag_on):
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q, max_chars=10)
    blocks = [b for b in transcript.split("\n\n") if b.strip()]
    assert len(blocks) == 1


def test_flag_on_is_deterministic(_flag_on):
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        first = build_from_parallax_retrieval(conn, q, top_k=2)
        second = build_from_parallax_retrieval(conn, q, top_k=2)
    assert first == second


def test_flag_on_skips_null_fields(_flag_on):
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        conn.execute(
            "UPDATE memories SET title = NULL, summary = NULL "
            "WHERE user_id = ? AND vault_path = ?",
            (q.question_id, f"lme/{q.question_id}/s0/t0"),
        )
        conn.commit()
        transcript = build_from_parallax_retrieval(conn, q)
    assert "None" not in transcript


def test_flag_on_empty_store_returns_empty(_flag_on):
    q = _fixture_question()
    with ephemeral_store() as conn:
        transcript = build_from_parallax_retrieval(conn, q)
    assert transcript == ""


def test_flag_on_no_live_provider_degrades_to_lexical(_flag_on):
    """FLAG ON but no BASE_URL → output is byte-identical to lexical path.

    Stub dense vectors must not participate in ranked RRF fusion — hash-random
    vectors at equal weight can drop strong lexical matches from top_k.
    """
    from parallax.retrieval.config import SEMANTIC_RETRIEVAL_ENV

    q = _fixture_question()

    # Baseline: flag OFF (pure lexical).
    import os

    with ephemeral_store() as conn:
        ingest_question(conn, q)
        old_val = os.environ.pop(SEMANTIC_RETRIEVAL_ENV, None)
        try:
            lexical_out = build_from_parallax_retrieval(conn, q)
        finally:
            if old_val is not None:
                os.environ[SEMANTIC_RETRIEVAL_ENV] = old_val

    # Flag ON, no BASE_URL → must equal lexical.
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        hybrid_out = build_from_parallax_retrieval(conn, q)

    assert hybrid_out == lexical_out


def test_flag_on_degrades_to_lexical_on_provider_error(monkeypatch):
    """A failing live embedding provider must not crash retrieval (degrades to lexical)."""
    from eval.longmemeval import store as store_mod
    from parallax.retrieval.embeddings import EmbeddingError

    monkeypatch.setenv("PARALLAX_SEMANTIC_RETRIEVAL", "1")
    # Set a BASE_URL so has_live_embedding_provider() returns True and the hybrid
    # path is entered; then the boom provider simulates the Ollama call failing.
    monkeypatch.setenv("PARALLAX_EMBEDDING_BASE_URL", "http://127.0.0.1:11434")

    class _BoomProvider:
        dim = 3
        id = "boom"

        def embed(self, texts):
            raise EmbeddingError("server down")

    # store.py imports get_embedding_provider into its own namespace; patch there.
    monkeypatch.setattr(store_mod, "get_embedding_provider", lambda: _BoomProvider())

    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q)
    # Degraded to lexical; teal row still present.
    assert "teal" in transcript.lower()
