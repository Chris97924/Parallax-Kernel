"""Immutable data contracts for the ADR-006 retrieval-filtered pipeline.

Every component in the retrieval/answer chain speaks these types. They are
intentionally frozen dataclasses with tuple-valued collections so an
evidence object can be hashed, cached, and safely shared across threads.

INITIAL priority ordering in ``INTENT_PRIORITY`` is provisional and will be
locked on Day-2 once the ambiguous fixture set is labeled.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

INSUFFICIENT_EVIDENCE: str = "insufficient_evidence"


class Intent(str, Enum):  # noqa: UP042 — keep (str, Enum) str() semantics, not StrEnum
    TEMPORAL = "temporal"
    MULTI_SESSION = "multi_session"
    PREFERENCE = "preference"
    USER_FACT = "user_fact"
    KNOWLEDGE_UPDATE = "knowledge_update"
    FALLBACK = "fallback"


INTENT_PRIORITY: tuple[Intent, ...] = (
    Intent.TEMPORAL,
    Intent.MULTI_SESSION,
    Intent.PREFERENCE,
    Intent.USER_FACT,
    Intent.KNOWLEDGE_UPDATE,
    Intent.FALLBACK,
)


@dataclass(frozen=True)
class RetrievalEvidence:
    """Frozen evidence bundle returned by a retrieval path.

    ``hits`` is a tuple of dicts (``{id, text, created_at, source_id, kind}``).
    We keep it as ``tuple[dict, ...]`` because dicts give callers structural
    access without a proliferation of per-kind dataclasses; the outer tuple is
    what makes the bundle hashable as a container. The dicts themselves are
    treated as read-only by downstream code — mutation is a bug, not a feature.
    """

    hits: tuple[dict, ...]
    stages: tuple[str, ...]
    notes: tuple[str, ...] = ()
    sql_fragments: tuple[str, ...] = ()
    diversity_mode: str = "none"


@dataclass(frozen=True)
class SixTuple:
    router_acc: float
    cond_acc_correct_route: float
    e2e_acc: float
    abstain_rate: float
    oracle_router_e2e: float
    fallback_e2e: float


@dataclass(frozen=True)
class AnswerInput:
    question: str
    evidence: RetrievalEvidence
    intent: Intent
    question_id: str | None = None


@dataclass(frozen=True)
class AnswerOutput:
    answer: str
    abstained: bool
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    notes: tuple[str, ...] = ()
