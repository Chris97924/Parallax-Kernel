"""Tests for parallax.answer.evidence.answer — semantic prompt + abstain path."""

from __future__ import annotations

import parallax.answer.evidence as evidence_module
from parallax.answer.evidence import answer
from parallax.retrieval.contracts import INSUFFICIENT_EVIDENCE, RetrievalEvidence


def _evidence(hits: list[dict] | None = None) -> RetrievalEvidence:
    return RetrievalEvidence(
        hits=tuple(
            hits
            or [
                {
                    "id": "c1",
                    "text": "Chris prefers dark mode.",
                    "created_at": "2026-04-01",
                    "source_id": "s1",
                    "kind": "claim",
                }
            ]
        ),
        stages=("mmr_embedding",),
        diversity_mode="mmr_embedding",
    )


def test_abstain_path(monkeypatch):
    captured: dict = {}

    def fake_call(model, messages, **kw):
        captured["model"] = model
        captured["messages"] = messages
        return {
            "text": "insufficient_evidence",
            "model": model,
            "prompt_tokens": 10,
            "completion_tokens": 2,
        }

    monkeypatch.setattr(evidence_module, "call", fake_call)

    out = answer(_evidence([]), "What is Chris' favourite theme?")
    assert out.abstained is True
    assert out.answer == INSUFFICIENT_EVIDENCE


def test_semantic_answer_path(monkeypatch):
    def fake_call(model, messages, **kw):
        return {
            "text": "Chris prefers dark mode.",
            "model": model,
            "prompt_tokens": 20,
            "completion_tokens": 5,
        }

    monkeypatch.setattr(evidence_module, "call", fake_call)

    out = answer(_evidence(), "What theme does Chris prefer?")
    assert out.abstained is False
    assert "dark mode" in out.answer.lower()


def test_prompt_is_semantic_not_exact_quotes(monkeypatch):
    seen: dict = {}

    def fake_call(model, messages, **kw):
        seen["messages"] = messages
        return {"text": "ok", "model": model, "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(evidence_module, "call", fake_call)

    answer(_evidence(), "Q?")
    system = seen["messages"][0]["content"]
    assert seen["messages"][0]["role"] == "system"
    assert "semantic meaning" in system
    assert "exact quotes" not in system.lower()
    assert "insufficient_evidence" in system


def test_cache_key_includes_evidence_content(monkeypatch):
    """Same question_id but different evidence hits must produce different cache keys.

    Without this, a caller that re-runs a question after swapping retrieval backends
    would hit the cached answer computed from the *previous* retriever's
    evidence — a silent correctness bug.
    """
    seen: list[str] = []

    def fake_call(model, messages, *, cache_key, **kw):
        seen.append(cache_key)
        return {
            "text": "ok",
            "model": model,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    monkeypatch.setattr(evidence_module, "call", fake_call)

    ev_a = _evidence([{"id": "c1", "text": "a", "created_at": "2026-01-01"}])
    ev_b = _evidence(
        [
            {"id": "c1", "text": "a", "created_at": "2026-01-01"},
            {"id": "c2", "text": "b", "created_at": "2026-01-02"},
        ]
    )
    ev_c = _evidence([{"id": "c9", "text": "a", "created_at": "2026-01-01"}])

    answer(ev_a, "Q?", question_id="qid-1")
    answer(ev_b, "Q?", question_id="qid-1")
    answer(ev_c, "Q?", question_id="qid-1")

    assert len(set(seen)) == 3, f"expected 3 distinct cache keys, got {seen!r}"
    # Same question_id prefix, distinct suffixes.
    assert all(k.startswith("answer::qid-1::") for k in seen)
