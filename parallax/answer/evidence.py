"""Evidence-only answerer.

Uses a **semantic** instruction so the model can perform reasonable logical
deduction (e.g. resolving a relative time reference), while still gating on
``insufficient_evidence`` when the corpus genuinely does not support the
question. The prompt deliberately avoids the phrase "exact quotes" — past
runs showed that wording drove over-abstention on paraphrased evidence.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json

from parallax.llm.call import call
from parallax.retrieval.contracts import (
    INSUFFICIENT_EVIDENCE,
    AnswerOutput,
    RetrievalEvidence,
)

SYSTEM_PROMPT_BASE = """\
Base your answer on the semantic meaning of the provided evidence.
Reasonable logical deduction is allowed (e.g. resolving relative time
references against today's date, combining two facts).
Only respond with `insufficient_evidence` when the evidence genuinely
does not support any reasonable inference toward the answer.
"""


def _render_evidence(evidence: RetrievalEvidence) -> str:
    lines: list[str] = []
    for i, hit in enumerate(evidence.hits, 1):
        text = hit.get("text", "")
        ts = hit.get("created_at", "unknown")
        lines.append(f"[{i}] {text} (at {ts})")
    if not lines:
        return "(no evidence)"
    return "\n".join(lines)


def _build_system(today: str) -> str:
    return SYSTEM_PROMPT_BASE + f"\nToday is {today}."


def answer(
    evidence: RetrievalEvidence,
    question: str,
    *,
    model: str = "gemini-2.5-pro",
    question_id: str | None = None,
    today: str | None = None,
    fallback_model: str | None = "gemini-2.5-flash",
) -> AnswerOutput:
    today = today or _dt.date.today().isoformat()
    system = _build_system(today)
    user = (
        "Evidence:\n"
        f"{_render_evidence(evidence)}\n\n"
        f"Question: {question}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    # PA-PARALLAX-F2: the pin must cover the evidence CONTENT and the date, not
    # just the hit ids. A retrieval change that grows hit ``c1``'s text from one
    # sentence to a paragraph keeps the id, and ``Today is …`` changes at
    # midnight — under an id-only key both replay the answer computed from the
    # older, thinner evidence (or from yesterday). ``parallax.llm.call`` folds
    # the rendered messages in as well, so the system prompt is covered too.
    ev_hash = hashlib.sha256(
        json.dumps(
            [
                [h.get("id", ""), h.get("text", ""), h.get("created_at", "")]
                for h in evidence.hits
            ],
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:16]
    cache_key = f"answer::{question_id or question}::{ev_hash}::{today}"
    result = call(
        model,
        messages,
        cache_key=cache_key,
        fallback_model=fallback_model,
        temperature=0.0,
        max_output_tokens=512,
    )
    text = (result.get("text") or "").strip()
    low = text.lower()
    if low == INSUFFICIENT_EVIDENCE or low.startswith(INSUFFICIENT_EVIDENCE):
        return AnswerOutput(
            answer=INSUFFICIENT_EVIDENCE,
            abstained=True,
            model=result.get("model", model),
            prompt_tokens=result.get("prompt_tokens"),
            completion_tokens=result.get("completion_tokens"),
            notes=("abstained_insufficient_evidence",),
        )
    return AnswerOutput(
        answer=text,
        abstained=False,
        model=result.get("model", model),
        prompt_tokens=result.get("prompt_tokens"),
        completion_tokens=result.get("completion_tokens"),
    )
