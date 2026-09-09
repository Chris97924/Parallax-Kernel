"""Cache replays must be visible in the eval line's cost numbers (PA-PARALLAX-F9).

``parallax.llm.call.call()`` has always returned a faithful ``_cached`` marker,
but the LongMemEval shim dropped it one layer up, so every replayed call was
counted as spend. The M8 read in ``run_retrieval_vs_dump.py`` is explicitly a
COST thesis, and the DUMP arm's transcript does not depend on ``top_k`` /
``max_chars`` — so every grid cell after the first replays its whole dump arm
from ``llm_cache`` while still reporting the full token bill.

These tests fake the provider dispatch and redirect the cache into ``tmp_path``;
no network, no API key, and ``~/.parallax/llm_cache.sqlite`` is never opened.
"""

from __future__ import annotations

from typing import Any

import pytest

import eval.longmemeval.pipeline as pipeline
import eval.longmemeval.run as run_mod
import eval.longmemeval.run_retrieval_vs_dump as rvd
import parallax.llm.call as call_mod
from eval.longmemeval.dataset import Question, Session, Turn
from eval.longmemeval.gemini import GeminiResult
from eval.longmemeval.pipeline import AnswerRecord


def _question() -> Question:
    return Question(
        question_id="q1",
        question_type="single-session-user",
        question="What did I say?",
        answer="tea",
        question_date="2026/04/01",
        sessions=(
            Session(
                session_id="s1",
                date="2026-04-01",
                turns=(Turn(role="user", content="I like tea.", has_answer=True),),
            ),
        ),
        answer_session_ids=("s1",),
    )


def _record(**overrides: Any) -> AnswerRecord:
    base: dict[str, Any] = {
        "question_id": "q",
        "question_type": "t",
        "question": "?",
        "gold": "g",
        "prediction": "p",
        "verdict": "CORRECT",
        "judge_reason": "r",
        "turns_ingested": 1,
        "answer_prompt_tokens": 1000,
        "answer_output_tokens": 10,
        "judge_prompt_tokens": 100,
        "judge_output_tokens": 5,
        "answer_model": "gemini-2.5-pro",
        "judge_model": "gemini-2.5-pro",
    }
    base.update(overrides)
    return AnswerRecord(**base)


def test_cached_flag_reaches_answer_record(tmp_path, monkeypatch):
    """``_cached`` survives call() -> GeminiResult -> AnswerRecord.

    Driven through the REAL ``parallax.llm.call.call`` against a tmp cache so
    the flag is produced by the caching layer rather than asserted into
    existence: the first ``run_one`` dispatches live, the second replays both
    its answer and its judge call from the row the first one wrote.
    """
    monkeypatch.setenv("PARALLAX_LLM_CACHE", str(tmp_path / "cache.sqlite"))
    dispatched: list[str] = []

    def fake_dispatch(model: str, _messages: list[dict], **_kw: Any) -> dict[str, Any]:
        dispatched.append(model)
        return {
            "text": "CORRECT\nlooks right",
            "raw": {},
            "model": model,
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "stop_reason": "stop",
        }

    monkeypatch.setattr(call_mod, "_dispatch", fake_dispatch)
    monkeypatch.setattr(pipeline, "dump_all_sessions", lambda _q: "transcript")
    monkeypatch.setattr(pipeline, "ingest_question", lambda _c, _q: 3)

    q = _question()
    live = pipeline.run_one(q, answer_model="gemini-2.5-pro", judge_model="gemini-2.5-flash")
    replay = pipeline.run_one(q, answer_model="gemini-2.5-pro", judge_model="gemini-2.5-flash")

    assert len(dispatched) == 2, "the first run must dispatch answer + judge live"
    assert live.answer_cached is False
    assert live.judge_cached is False
    assert replay.answer_cached is True, "the replayed answer must be marked cached"
    assert replay.judge_cached is True
    assert isinstance(replay.answer_cached, bool)

    # The shim carries the flag on GeminiResult itself, which is what the
    # pipeline reads; the default keeps rejudge.py and old jsonl loadable.
    assert GeminiResult(text="x", prompt_tokens=0, output_tokens=0, model="m").cached is False


def test_summary_reports_billed_prompted_and_replays():
    """BOTH runners emit tokens_billed, tokens_prompted and a replay count.

    Two columns rather than one corrected column: ``tokens_prompted`` is still
    the right number for "how big are these prompts" (the context-window
    question), while ``tokens_billed`` is the only honest answer to "what did
    this run cost". Reporting the replay count alongside is what lets a reader
    tell a genuine saving from a caching artefact.
    """
    records = [
        _record(question_id="a", answer_cached=False, judge_cached=False),
        _record(question_id="b", answer_cached=True, judge_cached=False),
        _record(question_id="c", answer_cached=True, judge_cached=True),
    ]

    summary = run_mod._summarize(records)

    # 3 x (1000 + 100) prompted; billed drops the two replayed answers and the
    # one replayed judge call.
    assert summary["tokens_prompted"] == 3300
    assert summary["tokens_billed"] == 1000 + 100 + 100
    assert summary["replay_count"] == 3
    assert summary["tokens_prompted"] == summary["tokens_in"]

    # A run with nothing replayed must not silently differ from the old numbers.
    all_live = [_record(question_id="a"), _record(question_id="b")]
    live_summary = run_mod._summarize(all_live)
    assert live_summary["tokens_billed"] == live_summary["tokens_prompted"] == 2200
    assert live_summary["replay_count"] == 0

    # run_retrieval_vs_dump reports the same three, per arm.
    arm = [
        {"answer_prompt_tokens": 500, "answer_cached": False, "judge_cached": True},
        {"answer_prompt_tokens": 500, "answer_cached": True, "judge_cached": True},
    ]
    assert rvd._arm_tokens(arm) == 1000
    assert rvd._arm_billed_tokens(arm) == 500
    assert rvd._arm_replays(arm) == 3


def test_token_ratio_uses_tokens_billed(monkeypatch, tmp_path, capsys):
    """A replayed dump-arm call must not inflate the pre-registered ratio.

    The scenario is the real one: grid cell 2 of ``run_gemma_sweep`` replays
    every dump-arm answer (the dump transcript ignores top_k/max_chars) while
    the retrieval arm, whose prompt changed with the params, dispatches live.
    Computed from prompted tokens the cell reports a 10x cost saving that was
    already paid for in cell 1; computed from billed tokens it reports the
    truth — 0 dump-arm spend this cell.
    """
    dump = [
        {"verdict": "CORRECT", "answer_prompt_tokens": 10_000, "answer_cached": True,
         "judge_cached": True},
    ]
    retr = [
        {"verdict": "CORRECT", "answer_prompt_tokens": 500, "answer_cached": False,
         "judge_cached": False},
    ]

    monkeypatch.setattr(rvd, "load_dotenv", lambda *_a, **_kw: None)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(rvd, "iter_questions", lambda *_a, **_kw: [_question()])
    monkeypatch.setattr(rvd, "_run_one", lambda *_a, **_kw: (dump[0], retr[0]))

    out = tmp_path / "slice.jsonl"
    assert rvd.main(["--limit", "1", "--out", str(out)]) == 0

    import json

    summary = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))

    # Prompted tokens still describe the prompts (20x), but the COST ratio is 0
    # billed dump tokens over 500 billed retrieval tokens.
    assert summary["dump"]["tokens_prompted"] == 10_000
    assert summary["dump"]["tokens_billed"] == 0
    assert summary["dump"]["replay_count"] == 2
    assert summary["retrieval"]["tokens_billed"] == 500
    assert summary["retrieval"]["replay_count"] == 0
    assert summary["token_ratio_dump_over_retr"] == 0.0, (
        "the ratio must be computed from billed tokens, not from prompted tokens"
    )
    assert summary["token_ratio_basis"] == "tokens_billed (live calls only)"
    assert summary["pre_registered_interesting"] is False

    # And with nothing replayed the ratio is the genuine 20x.
    live_dump = dict(dump[0], answer_cached=False, judge_cached=False)
    monkeypatch.setattr(rvd, "_run_one", lambda *_a, **_kw: (live_dump, retr[0]))
    out2 = tmp_path / "slice2.jsonl"
    assert rvd.main(["--limit", "1", "--out", str(out2)]) == 0
    summary2 = json.loads(out2.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert summary2["token_ratio_dump_over_retr"] == 20.0
    assert summary2["pre_registered_interesting"] is True


@pytest.mark.parametrize("cached", [False, True])
def test_answer_record_cached_flags_default_to_live(cached: bool):
    """The two flags are plain bools with a live-call default.

    ``eval/longmemeval/rejudge.py`` builds ``AnswerRecord`` without them and
    every jsonl written before 2026-09-09 lacks the fields, so the default has
    to be the conservative one: count it as spend rather than silently
    discounting an unknown call.
    """
    rec = _record(answer_cached=cached, judge_cached=cached)
    assert rec.answer_cached is cached
    assert rec.judge_cached is cached
    assert _record().answer_cached is False
    assert _record().judge_cached is False


# ---------------------------------------------------------------------------
# r3 — the flags survive a re-judge, and output tokens are billed too
# ---------------------------------------------------------------------------


def _rejudge_src(**overrides: Any) -> dict[str, Any]:
    src: dict[str, Any] = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What did I say?",
        "gold": "tea",
        "prediction": "tea",
        "turns_ingested": 3,
        "answer_prompt_tokens": 1000,
        "answer_output_tokens": 10,
        "answer_model": "gemini-2.5-pro",
        "answer_cached": True,
    }
    src.update(overrides)
    return src


def test_rejudge_preserves_cache_flags(monkeypatch):
    """A re-judged row must report ITS judge call and keep the source's answer.

    ``_rejudge_one`` copies every other ``answer_*`` field verbatim — it does
    not re-issue the answer — but it built ``AnswerRecord`` without either flag,
    so both defaulted to False. The re-judged jsonl therefore said "live" for
    every replayed call in it: the source row's ``answer_cached`` was dropped on
    the floor, and a judge call served straight out of ``llm_cache`` (the normal
    case when a re-judge is re-run, or run twice with the same judge model) was
    counted as spend. Any cost number computed downstream from the re-judged
    file was then wrong in the one direction that flatters the run.
    """
    import eval.longmemeval.rejudge as rejudge

    def _judge(cached: bool):
        return lambda **_kw: GeminiResult(
            text="CORRECT\nlooks right",
            prompt_tokens=100,
            output_tokens=5,
            model="gemini-3.1-pro-preview",
            cached=cached,
        )

    # A replayed judge call on a row whose answer was itself a replay.
    monkeypatch.setattr(rejudge, "call", _judge(True))
    rec = rejudge._rejudge_one(_rejudge_src(), "gemini-3.1-pro-preview")
    assert rec.verdict == "CORRECT"
    assert rec.judge_cached is True, "the judge flag must come from the judge call"
    assert rec.answer_cached is True, "the source answer's flag must survive"

    # A live judge call on a live source answer: both False, no free lunch.
    monkeypatch.setattr(rejudge, "call", _judge(False))
    live = rejudge._rejudge_one(
        _rejudge_src(answer_cached=False), "gemini-3.1-pro-preview"
    )
    assert live.judge_cached is False
    assert live.answer_cached is False

    # The source flag is the ANSWER's, not the source judge's: a re-judge issues
    # a new judge call, so the old row's judge_cached must not be carried over.
    monkeypatch.setattr(rejudge, "call", _judge(False))
    stale = rejudge._rejudge_one(
        _rejudge_src(judge_cached=True), "gemini-3.1-pro-preview"
    )
    assert stale.judge_cached is False

    # A pre-flag jsonl row has neither key; and a failed judge call is not a
    # replay. Both stay on the conservative default.
    def _boom(**_kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(rejudge, "call", _boom)
    legacy_src = _rejudge_src()
    legacy_src.pop("answer_cached")
    legacy = rejudge._rejudge_one(legacy_src, "gemini-3.1-pro-preview")
    assert legacy.verdict == "ERROR"
    assert legacy.answer_cached is False
    assert legacy.judge_cached is False


def test_summary_output_tokens_exclude_replays(monkeypatch, tmp_path):
    """Completion tokens are billed too, so replays must be netted out of them.

    Round 2 discounted replays on the PROMPT side only. ``tokens_out`` kept
    summing the cached answer and judge completions, so a fully replayed run
    reported zero input spend beside a full output bill — a shape no provider
    invoice can have, and the more expensive half of the two per token. Both
    runners now carry a live-only ``tokens_out_billed`` beside the all-calls
    ``tokens_out``, and both summaries name which fields exclude replays.
    """
    records = [
        _record(question_id="a", answer_cached=False, judge_cached=False),
        _record(question_id="b", answer_cached=True, judge_cached=True),
    ]

    summary = run_mod._summarize(records)

    # _record() carries 10 answer + 5 judge completion tokens.
    assert summary["tokens_out"] == 2 * (10 + 5), "every call still counts here"
    assert summary["tokens_out_billed"] == 15, "the replayed record generated nothing"
    assert summary["tokens_billed"] == 1100
    assert "tokens_out_billed" in summary["tokens_basis"]

    # Half-replayed: only the live side of the record is billed.
    half = run_mod._summarize([_record(answer_cached=True, judge_cached=False)])
    assert half["tokens_out"] == 15
    assert half["tokens_out_billed"] == 5

    # run_retrieval_vs_dump's arm helpers, same rule.
    arm = [
        {"answer_prompt_tokens": 500, "answer_output_tokens": 40,
         "judge_output_tokens": 20, "answer_cached": True, "judge_cached": True},
        {"answer_prompt_tokens": 500, "answer_output_tokens": 40,
         "judge_output_tokens": 20, "answer_cached": False, "judge_cached": False},
    ]
    assert rvd._arm_output_tokens(arm) == 120
    assert rvd._arm_billed_output_tokens(arm) == 60

    # And the fields actually reach that runner's summary file.
    dump = {"verdict": "CORRECT", "answer_prompt_tokens": 10_000,
            "answer_output_tokens": 40, "judge_output_tokens": 20,
            "answer_cached": True, "judge_cached": True}
    retr = {"verdict": "CORRECT", "answer_prompt_tokens": 500,
            "answer_output_tokens": 40, "judge_output_tokens": 20,
            "answer_cached": False, "judge_cached": False}
    monkeypatch.setattr(rvd, "load_dotenv", lambda *_a, **_kw: None)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(rvd, "iter_questions", lambda *_a, **_kw: [_question()])
    monkeypatch.setattr(rvd, "_run_one", lambda *_a, **_kw: (dump, retr))

    out = tmp_path / "slice.jsonl"
    assert rvd.main(["--limit", "1", "--out", str(out)]) == 0

    import json

    written = json.loads(out.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert written["dump"]["tokens_out"] == 60
    assert written["dump"]["tokens_out_billed"] == 0
    assert written["retrieval"]["tokens_out"] == 60
    assert written["retrieval"]["tokens_out_billed"] == 60
    assert "tokens_out_billed" in written["tokens_basis"]
