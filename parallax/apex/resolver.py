"""Apex M7 Part B — free-text → candidate-subject resolver (#71 Gap 1).

This is the **resolution layer in front of R4** sketched in
``docs/m7-prep/m7-freetext-retrieval-design.md`` §3.1. R4 detection
(supersession / contradiction / ambiguity) is inherently per-subject, so the
read path cannot "fuzzy match" inside R4. Instead a free-text prompt is first
resolved to one or more *candidate subjects*; R4 then runs unchanged over each
candidate's claim set.

Chris-gated decisions implemented here (design doc §4):

  * **D2 — strategy:** token-overlap (Option B), the pure-Python v0 baseline.
    Zero new dependencies. The golden-set harness
    (``tests/apex/test_resolver.py``) measures lexical recall so the
    later B-vs-C (embedding) decision is data-driven, not a guess.
  * **D3 — match target:** *subjects* (the cheap label layer), NOT claim
    bodies. ``resolve_subjects`` is handed the distinct subject strings (from
    the M6-maintained subject index) and scores them against the prompt.

Negative-result contract (design doc §3.3.5, spec §4.5): a resolver that
broadens recall **must not** silently convert a genuine miss into a spurious
hit. Concretely: a subject with **zero** meaningful token overlap is never
returned as a candidate. When nothing overlaps, the caller gets an empty
candidate list → the read path returns ``[]`` and fires
``parallax_apex_empty_result{cause="no_matching_claim"}`` (the real miss
stays a real miss).

Latency (spec §4.2 p99 < 100ms): tokenisation + set intersection over the
distinct-subject set is microsecond-scale for solo-dev corpora; this layer
adds no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "DEFAULT_TOP_K",
    "STOPWORDS",
    "ScoredSubject",
    "resolve_subjects",
    "tokenize",
]

# Default number of candidate subjects to admit (design doc D4 top-k). Kept
# generous enough that a multi-subject prompt surfaces every relevant subject,
# small enough that the per-subject R4 fan-out stays cheap. Overridable by the
# router (``ApexPublicReadRouter(resolver_top_k=...)``).
DEFAULT_TOP_K = 8

# A deliberately small, well-known English function-word set. Dropping these
# keeps the negative-result contract honest: a prompt sharing only the word
# "the" with a subject must NOT count as a match. This is intentionally NOT a
# comprehensive NLP stoplist — it is the smallest filter that stops pure
# function-word noise from manufacturing candidates. The golden-set harness is
# the feedback loop for tuning it (or for graduating to embeddings, Option C).
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "did", "do",
        "does", "for", "from", "had", "has", "have", "how", "i", "if", "in",
        "into", "is", "it", "its", "me", "my", "of", "on", "or", "our", "so",
        "that", "the", "their", "them", "then", "there", "these", "they",
        "this", "to", "was", "we", "were", "what", "when", "which", "who",
        "why", "will", "with", "you", "your",
    }
)

# Minimum token length kept after splitting. Single characters (split residue
# like "v" from "v0.3") carry no discriminating signal and only add noise.
_MIN_TOKEN_LEN = 2

# Split on any run of non-alphanumeric characters. Subjects use kebab/snake
# style (``"retrieval-quality"``, ``"apex_router"``) so this cleanly yields the
# component words; prompts are natural language so punctuation is discarded.
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def tokenize(text: str) -> frozenset[str]:
    """Lower-case, split on non-alphanumeric runs, drop stopwords + 1-char noise.

    Returns a ``frozenset`` because subject matching is set-membership: token
    multiplicity in the prompt does not make a subject more relevant, and the
    immutable set is safe to share/cache.
    """
    if not text:
        return frozenset()
    tokens = {
        tok
        for tok in _SPLIT_RE.split(text.lower())
        if len(tok) >= _MIN_TOKEN_LEN and tok not in STOPWORDS
    }
    return frozenset(tokens)


@dataclass(frozen=True)
class ScoredSubject:
    """A candidate subject with its lexical match scores against a prompt.

    ``overlap`` — count of shared meaningful tokens (the primary ranking key).
    ``coverage`` — overlap / |subject tokens|: how completely the subject's own
    tokens appear in the prompt (favours precise subjects over incidental
    single-word hits). ``jaccard`` — overlap / |prompt ∪ subject tokens|: the
    symmetric similarity, used as a final tie-break and as the reported score.
    """

    subject: str
    overlap: int
    coverage: float
    jaccard: float

    @property
    def sort_key(self) -> tuple[int, float, float, str]:
        """Deterministic best-first ordering key.

        Higher overlap, then higher coverage, then higher jaccard wins; the
        subject string is the final ascending tie-break so the ordering is
        total and stable across runs (no reliance on dict/set iteration order).
        """
        return (-self.overlap, -self.coverage, -self.jaccard, self.subject)


def resolve_subjects(
    prompt: str,
    subjects: Iterable[str],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[ScoredSubject, ...]:
    """Resolve a free-text prompt to ranked candidate subjects (token-overlap).

    Args:
        prompt: the free-text query (e.g. a Claude Code prompt).
        subjects: the distinct canonical subject labels to score against
            (supplied by the M6-maintained subject index — design doc D1/D3).
        top_k: maximum candidates to return (design doc D4). Values ``<= 0``
            yield an empty result.

    Returns:
        Up to ``top_k`` :class:`ScoredSubject` ordered best-first. **Only
        subjects with ``overlap >= 1`` are included** — the negative-result
        contract. A prompt that shares no meaningful token with any subject
        yields ``()`` (a genuine miss the caller must surface as empty, never
        as a fabricated hit).

    Duplicate subject strings collapse to a single candidate (resolution is on
    the label, not the package); the per-subject read then unpacks every
    package carrying that subject so R4 sees the full claim set.
    """
    if top_k <= 0:
        return ()

    prompt_tokens = tokenize(prompt)
    if not prompt_tokens:
        return ()

    scored: list[ScoredSubject] = []
    seen_subjects: set[str] = set()
    for subject in subjects:
        if subject in seen_subjects:
            continue
        seen_subjects.add(subject)

        subject_tokens = tokenize(subject)
        if not subject_tokens:
            continue
        shared = prompt_tokens & subject_tokens
        overlap = len(shared)
        if overlap == 0:
            # Negative-result contract: no meaningful overlap → not a candidate.
            continue
        union = len(prompt_tokens | subject_tokens)
        scored.append(
            ScoredSubject(
                subject=subject,
                overlap=overlap,
                coverage=overlap / len(subject_tokens),
                jaccard=overlap / union if union else 0.0,
            )
        )

    scored.sort(key=lambda s: s.sort_key)
    return tuple(scored[:top_k])
