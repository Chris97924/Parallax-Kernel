"""Offline recall@k contract tests for ``eval.longmemeval.run_recall_eval``.

Pins down two things, both deterministic and network-free:

* ``oracle_positive_paths`` maps the right turns to oracle answers — a turn is
  positive when its session is in ``answer_session_ids`` OR the turn carries
  ``has_answer: true``.
* In the default offline gate, hybrid recall@k >= lexical recall@k. With no
  live embedding provider the hybrid selector degrades to lexical, so the two
  are equal by construction — but the assertion guards against a future change
  that makes hybrid silently *worse* than lexical offline.

No LLM calls, no embedding network calls. The provider check is forced OFF so
the gate stays offline even on a machine where ``PARALLAX_EMBEDDING_BASE_URL``
happens to point at a live GB10 box.
"""

from __future__ import annotations

import pytest

from eval.longmemeval import store
from eval.longmemeval.dataset import Question, Session, Turn
from eval.longmemeval.run_recall_eval import oracle_positive_paths, recall_at_k


def _labelled_question(
    qid: str, question: str, answer_session_ids: tuple[str, ...]
) -> Question:
    """A two-session Question with a clear topical split and oracle labels.

    Session ``s1`` holds the colour fact (the user turn flagged
    ``has_answer=True``); session ``s2`` is an unrelated pet anecdote.
    """
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
        question_id=qid,
        question_type="single-session-user",
        question=question,
        answer="teal",
        question_date="2026-02-01",
        sessions=sessions,
        answer_session_ids=answer_session_ids,
    )


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee the hybrid selector never touches the network in this gate."""
    monkeypatch.setattr(store, "has_live_embedding_provider", lambda: False)


def test_oracle_positive_paths_uses_answer_session_and_has_answer() -> None:
    """Both turns of an answer session are positive, regardless of has_answer."""
    q = _labelled_question("q_pos", "What colour did I like?", ("s1",))
    positive = oracle_positive_paths(q)
    # s1 is the answer session -> both of its turns are positive.
    assert f"lme/{q.question_id}/s0/t0" in positive
    assert f"lme/{q.question_id}/s0/t1" in positive
    # s2 has no answer-session membership and no has_answer turn.
    assert f"lme/{q.question_id}/s1/t0" not in positive
    assert f"lme/{q.question_id}/s1/t1" not in positive


def test_has_answer_turn_is_positive_without_answer_session() -> None:
    """A has_answer turn counts even if its session isn't an answer session."""
    q = _labelled_question("q_hasans", "What colour did I like?", ())
    positive = oracle_positive_paths(q)
    # s1/t0 carries has_answer=True even though answer_session_ids is empty.
    assert f"lme/{q.question_id}/s0/t0" in positive
    # The assistant turn (has_answer=False) is not positive.
    assert f"lme/{q.question_id}/s0/t1" not in positive


def test_hybrid_recall_at_k_ge_lexical_offline() -> None:
    """Default offline gate: hybrid recall@k must be >= lexical recall@k."""
    questions = [
        _labelled_question("q1", "What colour did I say I liked?", ("s1",)),
        _labelled_question("q2", "Which colour is my favourite?", ("s1",)),
    ]
    # top_k=1 makes ranking load-bearing: out of 4 ingested turns only the
    # single best-scored row survives, so a hit means the selector actually
    # ranked the colour turn first (not that we kept almost everything).
    lex = recall_at_k(questions, top_k=1, use_hybrid=False)
    hyb = recall_at_k(questions, top_k=1, use_hybrid=True)

    # recall is a fraction in [0, 1] over the scorable denominator.
    assert 0.0 <= lex.recall <= 1.0
    assert lex.scored == 2 and lex.skipped == 0
    # The lexical selector must rank the colour turn first for both questions.
    assert lex.recall == 1.0
    # The load-bearing contract: hybrid never worse than lexical offline.
    assert hyb.recall >= lex.recall
