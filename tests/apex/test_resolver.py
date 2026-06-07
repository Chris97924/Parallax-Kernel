"""Apex M7 Part B — token-overlap resolver tests + golden-set recall harness.

Covers ``parallax.apex.resolver`` (#71 Gap 1, design doc D2/D3):
  * ``tokenize`` normalisation + stopword/short-token filtering.
  * ``resolve_subjects`` ranking, top-k, determinism, duplicate collapse.
  * The **negative-result contract**: zero meaningful overlap → no candidate
    (a real miss must never become a fabricated hit).
  * A **golden-set harness** (free-text prompt → expected subject) that measures
    lexical recall, so the later token-overlap-vs-embedding decision (design doc
    D2 / Option C) is made on data, not a guess.
"""

from __future__ import annotations

import pytest

from parallax.apex.resolver import (
    DEFAULT_TOP_K,
    ScoredSubject,
    resolve_subjects,
    tokenize,
)


@pytest.mark.unit
class TestTokenize:
    def test_lowercases_and_splits_on_non_alnum(self) -> None:
        assert tokenize("Retrieval-Quality") == frozenset({"retrieval", "quality"})

    def test_kebab_snake_and_spaces_all_split(self) -> None:
        assert tokenize("audit_row write-order") == frozenset(
            {"audit", "row", "write", "order"}
        )

    def test_drops_stopwords(self) -> None:
        # Only "retrieval" survives — the rest are function words.
        assert tokenize("what is the retrieval") == frozenset({"retrieval"})

    def test_drops_single_char_noise(self) -> None:
        # "v" (split residue) is dropped; "v0" survives (len 2).
        assert tokenize("v v0 r4") == frozenset({"v0", "r4"})

    def test_empty_text_is_empty_set(self) -> None:
        assert tokenize("") == frozenset()
        assert tokenize("   the a of  ") == frozenset()


@pytest.mark.unit
class TestResolveSubjects:
    _SUBJECTS = (
        "retrieval-quality",
        "vector-search-recall",
        "signer-trust-store",
    )

    def test_resolves_exact_token_overlap(self) -> None:
        result = resolve_subjects("how is the retrieval quality", self._SUBJECTS)
        assert result
        assert result[0].subject == "retrieval-quality"
        assert result[0].overlap == 2

    def test_zero_overlap_returns_empty_negative_contract(self) -> None:
        """The crux of the negative-result contract: no shared token → no candidate.

        A resolver that broadened recall into a spurious hit here would let the
        read path convert a genuine miss into fabricated knowledge.
        """
        result = resolve_subjects("completely unrelated banana sentence", self._SUBJECTS)
        assert result == ()

    def test_empty_prompt_returns_empty(self) -> None:
        assert resolve_subjects("", self._SUBJECTS) == ()
        assert resolve_subjects("the a of is", self._SUBJECTS) == ()

    def test_ranks_by_overlap_then_coverage(self) -> None:
        subjects = ("retrieval", "retrieval-quality-metrics")
        # The prompt fully covers "retrieval" (overlap 1, coverage 1.0) but only
        # partially covers the 3-token subject (overlap 1, coverage 0.33).
        result = resolve_subjects("retrieval", subjects)
        assert result[0].subject == "retrieval"  # higher coverage wins the tie

    def test_top_k_caps_results(self) -> None:
        subjects = tuple(f"topic-{i}-shared" for i in range(20))
        result = resolve_subjects("shared", subjects, top_k=3)
        assert len(result) == 3

    def test_top_k_zero_or_negative_returns_empty(self) -> None:
        assert resolve_subjects("retrieval quality", self._SUBJECTS, top_k=0) == ()
        assert resolve_subjects("retrieval quality", self._SUBJECTS, top_k=-1) == ()

    def test_duplicate_subjects_collapse(self) -> None:
        result = resolve_subjects(
            "retrieval quality", ("retrieval-quality", "retrieval-quality")
        )
        assert len(result) == 1

    def test_deterministic_ordering(self) -> None:
        # Subjects with identical scores must order by subject string (stable),
        # independent of input iteration order.
        a = resolve_subjects("alpha beta", ("alpha-x", "beta-x", "gamma"))
        b = resolve_subjects("alpha beta", ("beta-x", "gamma", "alpha-x"))
        assert [s.subject for s in a] == [s.subject for s in b]

    def test_scored_subject_is_frozen(self) -> None:
        s = ScoredSubject(subject="x", overlap=1, coverage=1.0, jaccard=0.5)
        with pytest.raises((AttributeError, TypeError)):
            s.subject = "y"  # type: ignore[misc]

    def test_default_top_k_constant(self) -> None:
        assert DEFAULT_TOP_K == 8


# ===========================================================================
# Golden-set recall harness (#71 acceptance) — lexical recall, data-driven.
# ===========================================================================
#
# A fixed subject corpus + labelled prompts. The harness reports recall@top_k
# for two case groups so the token-overlap-vs-embedding decision (design doc
# D2 / Option C) is grounded in numbers:
#   * LEXICAL_HITS — prompts that share surface tokens with the target subject.
#     Token-overlap is EXPECTED to resolve these; recall must be 1.0.
#   * PARAPHRASE_MISSES — semantically-equivalent prompts with no shared token.
#     Token-overlap is EXPECTED to miss these; the recall gap here is the
#     quantified motivation for graduating to embeddings (Option C).

GOLDEN_SUBJECTS: tuple[str, ...] = (
    "retrieval-quality",
    "vector-search-recall",
    "claim-supersession-policy",
    "signer-trust-store",
    "audit-row-write-order",
    "free-text-resolution",
    "package-signature-verification",
    "perihelion-private-boundary",
)

# (prompt, expected_subject) — surface-token overlap present.
LEXICAL_HITS: tuple[tuple[str, str], ...] = (
    ("how good is the retrieval quality lately", "retrieval-quality"),
    ("what does it say about vector search recall", "vector-search-recall"),
    ("explain the audit row write order invariant", "audit-row-write-order"),
    ("how does the signer trust store work", "signer-trust-store"),
    ("resolving free text into subjects", "free-text-resolution"),
    ("the perihelion private boundary rules", "perihelion-private-boundary"),
)

# (prompt, expected_subject) — paraphrase with NO shared surface token.
PARAPHRASE_MISSES: tuple[tuple[str, str], ...] = (
    ("finding memories by semantic meaning", "vector-search-recall"),
    ("how do I know which belief is current", "claim-supersession-policy"),
)


def _recall_at_k(cases: tuple[tuple[str, str], ...], *, top_k: int) -> float:
    """Fraction of cases whose expected subject appears in the top-k candidates."""
    if not cases:
        return 0.0
    hits = 0
    for prompt, expected in cases:
        candidates = resolve_subjects(prompt, GOLDEN_SUBJECTS, top_k=top_k)
        if expected in {c.subject for c in candidates}:
            hits += 1
    return hits / len(cases)


@pytest.mark.unit
class TestGoldenSetRecall:
    def test_lexical_recall_is_total(self) -> None:
        """Token-overlap must resolve every surface-overlap case (recall == 1.0)."""
        recall = _recall_at_k(LEXICAL_HITS, top_k=DEFAULT_TOP_K)
        assert recall == 1.0, f"lexical recall regressed to {recall:.2f}"

    def test_each_lexical_case_ranks_expected_first(self) -> None:
        """Stronger than recall: the intended subject is the TOP candidate."""
        for prompt, expected in LEXICAL_HITS:
            candidates = resolve_subjects(prompt, GOLDEN_SUBJECTS)
            assert candidates, f"no candidate for {prompt!r}"
            assert candidates[0].subject == expected, (
                f"{prompt!r} ranked {candidates[0].subject!r} over {expected!r}"
            )

    def test_paraphrase_gap_is_quantified(self) -> None:
        """Document the lexical ceiling: paraphrase recall is BELOW lexical recall.

        This is the data point that justifies (or defers) the embedding upgrade
        (design doc D2 / Option C). If a future change to ``tokenize`` ever makes
        these pass, the assertion below flips and prompts a re-read of D2 — i.e.
        the harness actively tracks the gap rather than hard-coding "0.0".
        """
        paraphrase_recall = _recall_at_k(PARAPHRASE_MISSES, top_k=DEFAULT_TOP_K)
        lexical_recall = _recall_at_k(LEXICAL_HITS, top_k=DEFAULT_TOP_K)
        # Token-overlap cannot bridge a zero-shared-token paraphrase.
        assert paraphrase_recall < lexical_recall
