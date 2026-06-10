"""``run_one(no_memory=True)`` skips ingest + transcript dump.

Regression harness for the Phase 2 ladder ablation: the closed-book baseline
must never touch ``ephemeral_store`` or ``dump_all_sessions`` — otherwise
a lift-curve datapoint that claims "no memory" silently leaks the full
history and the oracle/parallax/no_memory gap collapses to noise.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from eval.longmemeval.dataset import Question
from eval.longmemeval.gemini import GeminiResult
from eval.longmemeval.pipeline import run_one


@pytest.fixture
def question() -> Question:
    return Question(
        question_id="q-no-mem-1",
        question_type="single-session-user",
        question="What did the user say about coffee?",
        answer="they like it black",
        question_date="2026-04-21",
        sessions=(),
        answer_session_ids=(),
    )


def _stub_result(text: str) -> GeminiResult:
    return GeminiResult(text=text, prompt_tokens=1, output_tokens=1, model="stub")


class TestNoMemorySkipsIngest:
    def test_ephemeral_store_not_opened(self, question: Question) -> None:
        with (
            patch("eval.longmemeval.pipeline.ephemeral_store") as m_store,
            patch(
                "eval.longmemeval.pipeline.call",
                side_effect=[_stub_result("black"), _stub_result("CORRECT\nmatch")],
            ),
        ):
            run_one(
                question,
                answer_model="stub-answer",
                judge_model="stub-judge",
                no_memory=True,
            )
        assert m_store.call_count == 0

    def test_answer_prompt_has_empty_marker(self, question: Question) -> None:
        captured: list[str] = []

        def fake_call(**kw):  # type: ignore[no-untyped-def]
            captured.append(kw.get("user", ""))
            return _stub_result("CORRECT\nok" if len(captured) > 1 else "black")

        with (
            patch("eval.longmemeval.pipeline.ephemeral_store"),
            patch("eval.longmemeval.pipeline.call", side_effect=fake_call),
        ):
            rec = run_one(
                question,
                answer_model="stub-answer",
                judge_model="stub-judge",
                no_memory=True,
            )

        assert "(no chat history provided)" in captured[0]
        assert rec.turns_ingested == 0
        assert rec.verdict == "CORRECT"


class TestMemoryEnabledStillIngests:
    """Sanity: flag off → ingest path still runs (no regression)."""

    def test_store_called(self, question: Question) -> None:
        with (
            patch("eval.longmemeval.pipeline.ephemeral_store") as m_store,
            patch("eval.longmemeval.pipeline.ingest_question", return_value=3),
            patch("eval.longmemeval.pipeline.dump_all_sessions", return_value="T"),
            patch(
                "eval.longmemeval.pipeline.call",
                side_effect=[_stub_result("x"), _stub_result("CORRECT\n")],
            ),
        ):
            run_one(
                question,
                answer_model="stub-answer",
                judge_model="stub-judge",
            )
        assert m_store.call_count == 1
