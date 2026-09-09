"""Contract tests for ``eval.longmemeval.store.build_from_parallax_retrieval``.

Closes the v0.6 LongMemEval harness bug: the v1 pipeline built the answer
prompt from ``dump_all_sessions(q)`` which walked the in-memory Question
tuple, bypassing the Parallax store. This module asserts the new helper
actually reads back through the store we ingested into, plus pins down the
edge-case contract (empty store, tight budgets, tokenization, determinism,
NULL fields, chronological order).

No LLM calls — everything is deterministic SQLite round-trip. Pipeline-level
``use_retrieval`` behavior is tested here with a monkeypatched ``call`` so
the Gemini path never fires.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from eval.longmemeval import pipeline
from eval.longmemeval.dataset import Question, Session, Turn
from eval.longmemeval.store import (
    build_from_parallax_retrieval,
    ephemeral_store,
    ingest_question,
)


def _fixture_question() -> Question:
    """Minimal Question with two sessions covering a clear topical split."""
    sessions = (
        Session(
            session_id="s1",
            date="2026-01-01",
            turns=(
                Turn(
                    role="user",
                    content="My favourite colour is teal.",
                    has_answer=True,
                ),
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
        question_id="q_retrieval_smoke",
        question_type="single-session-user",
        question="What colour did I say I liked?",
        answer="teal",
        question_date="2026-02-01",
        sessions=sessions,
        answer_session_ids=("s1",),
    )


# ---------------------------------------------------------------------------
# Round-trip — the output MUST come from the store, not from q directly.
# ---------------------------------------------------------------------------


def test_build_from_parallax_retrieval_reads_through_store() -> None:
    """The ``[date] role`` title prefix only comes from ingest_question.

    ``dump_all_sessions(q)`` emits ``### Session N — date`` headers and
    uppercase ``USER:`` / ``ASSISTANT:`` lines. If someone swaps the function
    body for ``dump_all_sessions(q)`` (the original bypass bug), neither of
    those prefixes appears, so this test fails loud.
    """
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q)

    assert transcript, "expected non-empty transcript from Parallax store"
    # title format written by ingest_question: "[{date}] {role}"
    assert "[2026-01-01] user" in transcript, (
        "transcript must carry the store-side title prefix, proving it "
        "was read via memories_by_user (not dump_all_sessions)"
    )
    # dump_all_sessions sentinels MUST be absent.
    assert "### Session" not in transcript
    assert "USER:" not in transcript


def test_build_from_parallax_retrieval_empty_store_returns_empty_string() -> None:
    """A fresh store with nothing ingested must return '' (not raise)."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        transcript = build_from_parallax_retrieval(conn, q)
    assert transcript == ""


# ---------------------------------------------------------------------------
# top_k + char budget — tight assertions so silent regressions fail loud.
# ---------------------------------------------------------------------------


def test_build_from_parallax_retrieval_respects_top_k() -> None:
    """top_k=1 keeps EXACTLY one non-empty block (not zero)."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q, top_k=1)
    blocks = [b for b in transcript.split("\n\n") if b.strip()]
    assert len(blocks) == 1, f"top_k=1 expected 1 block, got {len(blocks)}"
    assert blocks[0].strip(), "surviving block must be non-empty"


def test_build_from_parallax_retrieval_respects_char_budget() -> None:
    """max_chars=10 keeps only the first block (soft cap; first always survives)."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q, max_chars=10)
    blocks = [b for b in transcript.split("\n\n") if b.strip()]
    assert len(blocks) == 1


# ---------------------------------------------------------------------------
# Chronological order — vault_path ordering preserved in output.
# ---------------------------------------------------------------------------


def test_build_from_parallax_retrieval_chronological_order() -> None:
    """Kept rows must emerge in vault_path order (not relevance order)."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q)

    s1_idx = transcript.find("Teal noted")
    s2_idx = transcript.find("Mochi")
    assert s1_idx != -1 and s2_idx != -1, "expected both sessions in output"
    assert s1_idx < s2_idx, "chronological order (s1 before s2) must be preserved"


# ---------------------------------------------------------------------------
# R4 — punctuation-robust tokenization.
# ---------------------------------------------------------------------------


def test_punctuation_does_not_break_lexical_match() -> None:
    """"teal" in the question must match "teal." in a turn, after tokenizing."""
    q = _fixture_question()
    # q.question ends with "?", row content ends with "." — punctuation must
    # not suppress the lexical overlap. top_k=1 forces the scorer to choose;
    # the teal row must win over the Mochi row.
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q, top_k=1)
    assert "teal" in transcript.lower(), (
        "punctuation-stripped tokenization should let the colour row win"
    )
    assert "mochi" not in transcript.lower()


# ---------------------------------------------------------------------------
# R5 — deterministic tie-breaking on zero-score ties.
# ---------------------------------------------------------------------------


def test_build_from_parallax_retrieval_is_deterministic() -> None:
    """Two identical runs over the same store must produce identical output."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        first = build_from_parallax_retrieval(conn, q, top_k=2)
        second = build_from_parallax_retrieval(conn, q, top_k=2)
    assert first == second


def test_zero_score_ties_break_on_vault_path() -> None:
    """A question with zero lexical overlap must still produce stable top_k."""
    q = _fixture_question()
    # question with no overlap against any ingested content:
    empty_overlap_q = q._replace(question="xyzzy plugh")
    with ephemeral_store() as conn:
        ingest_question(conn, empty_overlap_q)
        a = build_from_parallax_retrieval(conn, empty_overlap_q, top_k=2)
        b = build_from_parallax_retrieval(conn, empty_overlap_q, top_k=2)
    assert a == b, "tie-broken results must be deterministic"
    assert a, "zero-score ties must still produce a transcript (not empty)"


# ---------------------------------------------------------------------------
# R6 — NULL title / summary must not leak the literal string "None".
# ---------------------------------------------------------------------------


def test_build_from_parallax_retrieval_skips_null_fields() -> None:
    """Rows with NULL title and summary must not emit the string 'None'."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        # Overwrite one row with NULL title/summary. ingest_memory does not
        # expose a NULL path, so patch via raw SQL against the schema.
        conn.execute(
            "UPDATE memories SET title = NULL, summary = NULL "
            "WHERE user_id = ? AND vault_path = ?",
            (q.question_id, f"lme/{q.question_id}/s0/t0"),
        )
        conn.commit()
        transcript = build_from_parallax_retrieval(conn, q)
    assert "None" not in transcript, (
        "NULL title/summary must be coerced to '' — never rendered as 'None'"
    )


# ---------------------------------------------------------------------------
# R3 — pipeline must fail loud when retrieval returns empty transcript.
# ---------------------------------------------------------------------------


@dataclass
class _FakeGeminiResult:
    text: str = "MOCK"
    prompt_tokens: int = 0
    output_tokens: int = 0
    # Mirrors the real GeminiResult.cached that pipeline.run_one now records on
    # every AnswerRecord (PA-PARALLAX-F9).
    cached: bool = False


def test_run_one_returns_error_when_retrieval_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_one(use_retrieval=True)`` must ERROR when transcript is empty.

    Silent behavior would send an empty context to Gemini and get back a
    hallucinated prediction that grades as INCORRECT, poisoning benchmark
    numbers with no indication that the context was missing.
    """

    def _no_call(**_kwargs):
        raise AssertionError("gemini.call must not fire when retrieval empty")

    monkeypatch.setattr(pipeline, "call", _no_call)
    # Force build_from_parallax_retrieval to return "" regardless of store.
    monkeypatch.setattr(
        pipeline,
        "build_from_parallax_retrieval",
        lambda conn, q, **_kw: "",
    )

    q = _fixture_question()
    record = pipeline.run_one(
        q,
        answer_model="mock-answer",
        judge_model="mock-judge",
        use_retrieval=True,
    )
    assert record.verdict == "ERROR"
    assert "retrieval" in record.judge_reason.lower()


def test_run_one_does_not_trigger_retrieval_guard_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With use_retrieval=False, empty-transcript guard must not fire.

    Legacy behavior (dump_all_sessions) is preserved verbatim; only the
    retrieval-mode path carries the new fail-loud contract.
    """
    called: dict[str, bool] = {"call": False}

    def _fake_call(**_kwargs):
        called["call"] = True
        return _FakeGeminiResult(text="CORRECT\nreason")

    monkeypatch.setattr(pipeline, "call", _fake_call)

    q = _fixture_question()
    record = pipeline.run_one(
        q,
        answer_model="mock-answer",
        judge_model="mock-judge",
        use_retrieval=False,
    )
    assert called["call"] is True, "legacy path must still call gemini"
    assert record.verdict in {"CORRECT", "INCORRECT"}
