"""Tests for parallax.answer.evidence.answer — semantic prompt + abstain path."""

from __future__ import annotations

import pytest

import parallax.answer.evidence as evidence_module
import parallax.llm.call as call_module
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


# ---------------------------------------------------------------------------
# PA-PARALLAX-F2 — the pinned key must track the payload, not just the hit ids
#
# These three drive the REAL parallax.llm.call.call() against a tmp_path cache
# (only the provider dispatch is faked), because the property under test is the
# end-to-end cache identity: evidence.answer's cache_key plus the messages
# digest call() folds in. Asserting on the key string alone would pass for a
# key that changes but is never consulted.
# ---------------------------------------------------------------------------


@pytest.fixture()
def cached_answer_probe(tmp_path, monkeypatch):
    """Isolate the LLM cache in tmp_path and count live dispatches.

    Never points at ``~/.parallax/llm_cache.sqlite``: these tests write rows,
    and the developer cache is shared with real runs.
    """
    monkeypatch.setenv("PARALLAX_LLM_CACHE", str(tmp_path / "cache.sqlite"))
    dispatched: list[str] = []

    def fake_dispatch(model, _messages, **_kw):
        dispatched.append(model)
        return {
            "text": f"answer-{len(dispatched)}",
            "raw": {},
            "model": model,
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "stop_reason": "stop",
        }

    monkeypatch.setattr(call_module, "_dispatch", fake_dispatch)
    return dispatched


def test_pinned_key_changes_when_evidence_text_changes(cached_answer_probe):
    """Same hit ids, different hit TEXT must re-dispatch.

    A retrieval change that grows hit ``c1``'s snippet from one sentence to a
    paragraph keeps the id, so an id-only pin replayed the answer computed from
    the old, thinner evidence — with no signal that the new evidence was never
    read. The pre-fix ``ev_hash`` covered ``[h["id"] for h in hits]`` only, so
    this is the exact case ``test_cache_key_includes_evidence_content`` above
    advertised in its docstring but did not cover (it varied ids).
    """
    thin = _evidence([{"id": "c1", "text": "Chris likes tea.", "created_at": "2026-04-01"}])
    thick = _evidence(
        [{"id": "c1", "text": "Chris likes tea, especially oolong.", "created_at": "2026-04-01"}]
    )

    first = answer(thin, "What does Chris like?", question_id="qid-1", today="2026-09-09")
    second = answer(thick, "What does Chris like?", question_id="qid-1", today="2026-09-09")

    assert len(cached_answer_probe) == 2, "different evidence text must be a cache MISS"
    assert first.answer != second.answer

    # Re-issuing the identical evidence is still a HIT, or the pin would have
    # stopped being a cache at all.
    answer(thin, "What does Chris like?", question_id="qid-1", today="2026-09-09")
    assert len(cached_answer_probe) == 2


def test_pinned_key_changes_when_today_changes(cached_answer_probe):
    """A new ``Today is …`` must re-dispatch.

    The system prompt exists to support relative-time deduction ("last Tuesday"),
    so a run that crosses midnight — or is re-run next week — replaying
    yesterday's date silently answers the wrong question with full confidence.
    """
    ev = _evidence()

    answer(ev, "When is the appointment?", question_id="qid-1", today="2026-09-09")
    answer(ev, "When is the appointment?", question_id="qid-1", today="2026-09-10")

    assert len(cached_answer_probe) == 2, "a new today must be a cache MISS"

    answer(ev, "When is the appointment?", question_id="qid-1", today="2026-09-09")
    assert len(cached_answer_probe) == 2, "the same today must still hit"


def test_pinned_key_changes_when_system_prompt_changes(cached_answer_probe, monkeypatch):
    """Editing the prompt must invalidate the cache.

    This is the failure that hides its own experiment: you edit the abstain
    wording to fix over-abstention, re-run the eval, get byte-identical results,
    and conclude the wording does not matter. The pin covers the ids and the
    date; the system prompt reaches the key through the messages digest that
    ``parallax.llm.call._hash_prompt`` folds into every pinned key.
    """
    ev = _evidence()
    kwargs = {"question_id": "qid-1", "today": "2026-09-09"}

    answer(ev, "Q?", **kwargs)
    assert len(cached_answer_probe) == 1

    answer(ev, "Q?", **kwargs)
    assert len(cached_answer_probe) == 1, "an unchanged prompt must still hit"

    monkeypatch.setattr(
        evidence_module,
        "SYSTEM_PROMPT_BASE",
        evidence_module.SYSTEM_PROMPT_BASE + "\nPrefer the most recent evidence.\n",
    )
    answer(ev, "Q?", **kwargs)
    assert len(cached_answer_probe) == 2, "a prompt edit must be a cache MISS"
