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

import json
from pathlib import Path

import pytest

from eval.longmemeval import run_recall_eval, store
from eval.longmemeval.dataset import Question, Session, Turn
from eval.longmemeval.run_recall_eval import (
    DISTRACTOR_PREFIX,
    augment_with_distractors,
    oracle_positive_paths,
    recall_at_k,
    recall_curve,
)


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


# --------------------------------------------------------------------------- #
# Discriminating recall: the oracle split saturates at 1.0 (every turn belongs
# to an answer session), so it cannot measure ranking. These cover the two
# escape hatches the harness exposes: a hand-built distractor-augmented fixture
# (recall_curve must respond to k) and the programmatic distractor injector.
# --------------------------------------------------------------------------- #


def _distractor_question(
    qid: str,
    question: str,
    answer_content: str,
    distractor_contents: tuple[str, ...],
) -> Question:
    """An oracle-style Question with one answer session + inline distractors.

    ``s0`` is the gold answer session (its user turn carries
    ``has_answer=True``); ``s1..`` are non-answer distractor sessions
    (``has_answer=False``, session ids outside ``answer_session_ids``). Lexical
    scoring is driven by ``content`` overlap with ``question``, so the caller
    controls ranking precisely by choosing how much each string echoes the
    question.
    """
    sessions = [
        Session(
            session_id="ans",
            date="2026-01-01",
            turns=(
                Turn(role="user", content=answer_content, has_answer=True),
                Turn(role="assistant", content="ok", has_answer=False),
            ),
        )
    ]
    for di, content in enumerate(distractor_contents):
        sessions.append(
            Session(
                session_id=f"dis{di}",
                date="2026-01-02",
                turns=(
                    Turn(role="user", content=content, has_answer=False),
                    Turn(role="assistant", content="ok", has_answer=False),
                ),
            )
        )
    return Question(
        question_id=qid,
        question_type="single-session-user",
        question=question,
        answer=answer_content,
        question_date="2026-02-01",
        sessions=tuple(sessions),
        answer_session_ids=("ans",),
    )


def test_recall_curve_discriminates_recall_at_1_below_recall_at_large_k() -> None:
    """A distractor-augmented set must yield recall@1 < recall@large_k.

    ``q_easy`` answers itself (the answer turn echoes the question) so its gold
    row ranks first -> a k=1 hit. ``q_hard`` answers with a non-matching string
    while a distractor echoes the whole question -> the distractor outranks the
    gold row at k=1 (a miss) but the gold row is recovered once k is large. So
    recall@1 lands strictly inside (0, 1) and below recall@large_k: ranking is
    load-bearing, which is exactly what the saturated oracle split hides.
    """
    questions = [
        _distractor_question(
            "q_easy",
            question="apple banana cherry fruit",
            answer_content="apple banana cherry fruit basket",
            distractor_contents=("random noise foo", "qux quux corge"),
        ),
        _distractor_question(
            "q_hard",
            question="elephant giraffe zebra safari",
            answer_content="zzz placeholder",  # no overlap with the question
            distractor_contents=("elephant giraffe zebra safari herd migration",),
        ),
    ]
    k_large = 100  # >> rows per question, so every positive survives top-k
    curve = recall_curve(questions, [1, k_large], use_hybrid=False)

    assert curve.scored == 2 and curve.skipped == 0
    # Distractors enlarge the haystack -> positive fraction must drop below the
    # oracle's saturated 1.0.
    assert curve.positive_fraction < 1.0

    r1 = curve.results[1].recall
    r_large = curve.results[k_large].recall
    # recall@large_k recovers every gold row.
    assert r_large == 1.0
    assert 0.0 < r_large <= 1.0
    # recall@1 is discriminating: strictly between 0 and 1...
    assert 0.0 < r1 < 1.0
    # ...and strictly below recall@large_k (the load-bearing assertion).
    assert r1 < r_large


def test_augment_with_distractors_breaks_oracle_saturation() -> None:
    """Injecting distractor sessions drops positive fraction below 1.0.

    Each source question is oracle-style (every session is an answer session ->
    positive fraction 1.0). After augmentation every question must gain
    distractor sessions that are (a) prefixed/identifiable, (b) never in
    ``answer_session_ids``, and (c) absent from ``oracle_positive_paths`` — so
    the original gold turns stay positive while the denominator grows.
    """
    base = [
        _labelled_question("qa", "What colour did I like?", ("s1",)),
        _labelled_question("qb", "Which colour is my favourite?", ("s1",)),
        _labelled_question("qc", "Name my favourite colour.", ("s1",)),
    ]
    base_turns = sum(len(s.turns) for s in base[0].sessions)

    augmented = augment_with_distractors(base, n_distractors=2, seed=0)
    assert len(augmented) == len(base)

    for orig, aug in zip(base, augmented, strict=True):
        # Original answer sessions are preserved at their leading indices, so
        # their oracle-positive vault paths are unchanged.
        assert oracle_positive_paths(orig) == oracle_positive_paths(aug)
        # Two distractor sessions were appended.
        assert len(aug.sessions) == len(orig.sessions) + 2
        injected = aug.sessions[len(orig.sessions):]
        for sess in injected:
            assert sess.session_id.startswith(DISTRACTOR_PREFIX)
            assert sess.session_id not in aug.answer_session_ids
            # Borrowed turns are forced non-answer.
            assert all(not t.has_answer for t in sess.turns)
        # Positive fraction strictly drops: same positives, bigger haystack.
        total_turns = sum(len(s.turns) for s in aug.sessions)
        positive = len(oracle_positive_paths(aug))
        assert total_turns > base_turns
        assert positive / total_turns < 1.0

    # No-op guard: zero distractors returns the questions unchanged.
    assert augment_with_distractors(base, 0) == base


def test_augment_with_distractors_is_deterministic() -> None:
    """A fixed seed yields byte-identical augmentation across calls."""
    base = [
        _labelled_question("qa", "What colour did I like?", ("s1",)),
        _labelled_question("qb", "Which colour is my favourite?", ("s1",)),
    ]
    a = augment_with_distractors(base, n_distractors=1, seed=7)
    b = augment_with_distractors(base, n_distractors=1, seed=7)
    assert a == b


def test_main_emits_discriminating_results_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: --distractors on an oracle file writes a discriminating JSON.

    Builds a tiny oracle-format split where ``q_easy``'s gold turn self-matches
    and ``q_hard``'s gold turn is out-ranked by ``q_easy``'s session once it is
    injected as a distractor. The written results JSON must report
    ``discriminating=true`` with recall@1 strictly inside (0, 1).
    """
    oracle = [
        {
            "question_id": "q_hard",
            "question_type": "single-session-user",
            "question": "elephant giraffe zebra safari",
            "answer": "zzz",
            "question_date": "2026-02-01",
            "haystack_dates": ["2026-01-01"],
            "haystack_session_ids": ["ans_hard"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "zzz noise", "has_answer": True},
                    {"role": "assistant", "content": "ok", "has_answer": False},
                ]
            ],
            "answer_session_ids": ["ans_hard"],
        },
        {
            "question_id": "q_easy",
            "question_type": "single-session-user",
            "question": "apple banana cherry",
            "answer": "elephant giraffe zebra safari herd",
            "question_date": "2026-02-01",
            "haystack_dates": ["2026-01-01"],
            "haystack_session_ids": ["ans_easy"],
            "haystack_sessions": [
                [
                    {
                        "role": "user",
                        "content": "elephant giraffe zebra safari herd",
                        "has_answer": True,
                    },
                    {"role": "assistant", "content": "ok", "has_answer": False},
                ]
            ],
            "answer_session_ids": ["ans_easy"],
        },
    ]
    oracle_file = tmp_path / "oracle.json"
    oracle_file.write_text(json.dumps(oracle), encoding="utf-8")
    out_file = tmp_path / "recall.json"

    monkeypatch.setitem(run_recall_eval.SPLIT_FILES, "oracle", oracle_file)
    # Keep the run fully offline regardless of the host's embedding config.
    monkeypatch.setattr(run_recall_eval, "has_live_embedding_provider", lambda: False)

    rc = run_recall_eval.main(
        [
            "--split",
            "oracle",
            "--distractors",
            "4",
            "--top-k",
            "50",
            "--out",
            str(out_file),
        ]
    )
    assert rc == 0

    summary = json.loads(out_file.read_text(encoding="utf-8"))
    assert summary["distractors"] == 4
    assert summary["discriminating"] is True
    assert summary["positive_fraction"] < 1.0
    assert 0.0 < summary["lexical_recall_at_1"] < 1.0
    assert summary["lexical_recall_at_1"] < summary["lexical_recall_at_k"]
    assert summary["lexical_recall_at_k"] == 1.0
