"""Mutation-hardening for ``parallax.apex.resolver`` (overnight-20260816 S6).

Additive companion to ``test_resolver.py``. Every test below was written
against a semantic mutant that the existing suite let through.

The gap the existing suite has is that it asserts on the *identity and order*
of resolved subjects but never on the score fields those subjects carry, and
its ranking cases all happen to be ties on the primary key. Concretely:

  * ``ScoredSubject.coverage`` and ``.jaccard`` are public, documented fields
    with specific denominators, and no assertion pinned either value — swapping
    both denominators to ``len(prompt_tokens)`` left the whole suite green.
  * Every existing ranking case resolves exactly one candidate, or two with
    equal ``overlap``. Inverting the primary sort key so that the *least*
    overlapping subject ranks first therefore changed nothing observable.
  * Duplicate collapse is keyed on the exact subject string; case-folding that
    key silently drops a distinct canonical label (and with it every package
    the router would have read for that label), and no case-variant subject
    appeared anywhere in the corpus.
"""

from __future__ import annotations

import pytest

from parallax.apex.resolver import resolve_subjects

# One shared fixture case, chosen so |prompt tokens| != |subject tokens| != |union|
# — the three candidate denominators are pairwise distinct, so each score
# assertion below pins exactly one of them.
#
#   prompt tokens  = {query, router, latency}                      -> 3
#   subject tokens = {query, router, cache, eviction}              -> 4
#   overlap        = {query, router}                               -> 2
#   union          = {query, router, latency, cache, eviction}     -> 5
_PROMPT = "query router latency"
_SUBJECT = "query-router-cache-eviction"


@pytest.mark.unit
class TestScoreDenominators:
    """The score fields are part of the public contract, not just sort inputs."""

    def _only(self, prompt: str = _PROMPT, subject: str = _SUBJECT):
        result = resolve_subjects(prompt, (subject,))
        assert len(result) == 1
        return result[0]

    def test_overlap_counts_shared_tokens(self) -> None:
        assert self._only().overlap == 2

    def test_coverage_is_over_subject_tokens(self) -> None:
        """coverage = overlap / |subject tokens| — how completely the SUBJECT is covered.

        Dividing by the prompt's token count instead would report 2/3 here and
        would make coverage a property of the query rather than of the subject,
        collapsing to a constant across every candidate of a given prompt (i.e.
        silently removing the second sort key).
        """
        assert self._only().coverage == pytest.approx(2 / 4)

    def test_jaccard_is_over_the_union(self) -> None:
        """jaccard = overlap / |prompt ∪ subject| — the SYMMETRIC similarity.

        Dividing by the prompt's token count would report 2/3 here, which is
        the asymmetric measure: a subject with a long tail of unmatched tokens
        would score identically to an exact one.
        """
        assert self._only().jaccard == pytest.approx(2 / 5)

    def test_the_three_scores_are_distinct_here(self) -> None:
        """Guard the fixture itself: if these ever coincide the tests above go blind."""
        s = self._only()
        assert len({round(s.overlap, 6), round(s.coverage, 6), round(s.jaccard, 6)}) == 3


@pytest.mark.unit
class TestOverlapIsThePrimaryRankingKey:
    def test_higher_overlap_outranks_equal_coverage(self) -> None:
        """A 3-token match must beat a 1-token match, even at equal coverage.

        Both subjects below are fully covered by the prompt (coverage 1.0), so
        the ordering rests entirely on ``overlap``. The existing suite never had
        two candidates with unequal overlap, so an inverted primary key — least
        relevant subject first — was invisible to it.
        """
        result = resolve_subjects(
            "retrieval quality metrics dashboard",
            ("dashboard", "retrieval-quality-metrics"),
        )
        assert [s.subject for s in result] == ["retrieval-quality-metrics", "dashboard"]
        assert [s.overlap for s in result] == [3, 1]
        assert [s.coverage for s in result] == [1.0, 1.0]  # the tie that isolates overlap

    def test_ranking_is_monotonically_non_increasing_in_overlap(self) -> None:
        subjects = (
            "alpha",
            "alpha-beta",
            "alpha-beta-gamma",
            "alpha-beta-gamma-delta",
        )
        result = resolve_subjects("alpha beta gamma delta", subjects)
        overlaps = [s.overlap for s in result]
        assert overlaps == sorted(overlaps, reverse=True)
        assert overlaps == [4, 3, 2, 1]


@pytest.mark.unit
class TestDuplicateCollapseIsExactString:
    def test_case_variant_subjects_are_distinct_candidates(self) -> None:
        """Collapse is on the exact label — case variants are different subjects.

        The router feeds ``candidate.subject`` straight back into
        ``index.packages_for_subject(...)``, which is keyed on the exact index
        label. Folding case here would drop one of these labels from the
        candidate list and make every package carrying it unreadable by
        free text, while the suite stayed green.
        """
        result = resolve_subjects("alpha topic", ("Alpha-Topic", "alpha-topic"))
        assert {s.subject for s in result} == {"Alpha-Topic", "alpha-topic"}

    def test_exact_duplicates_still_collapse(self) -> None:
        """The positive twin: identical strings remain a single candidate."""
        result = resolve_subjects("alpha topic", ("alpha-topic", "alpha-topic"))
        assert [s.subject for s in result] == ["alpha-topic"]
