"""Tests for the PENDING_IMPLEMENTATION verdict added in PR-C.

Spec ground truth: docs/m4-prep/traffic-gap-resolution.md §3.4.
"""

from __future__ import annotations

import pytest

from parallax.canary.dod import DodVerdict, _aggregate


@pytest.mark.unit
class TestDodVerdictPending:
    def test_pending_value_present(self) -> None:
        # Sentinel test: ensures the enum literal "pending_implementation"
        # exists for downstream tools that key on the string.
        assert DodVerdict.PENDING_IMPLEMENTATION.value == "pending_implementation"

    def test_pending_distinct_from_fail(self) -> None:
        # PENDING is not just a relabeled FAIL — they have different
        # operator-action semantics. FAIL = regression to fix; PENDING =
        # check not yet implementable.
        assert DodVerdict.PENDING_IMPLEMENTATION != DodVerdict.FAIL


@pytest.mark.unit
class TestAggregatePrecedence:
    def test_fail_dominates_pending(self) -> None:
        # FAIL is operator-actionable today; PENDING is not. Surface FAIL.
        assert (
            _aggregate([DodVerdict.PASS, DodVerdict.FAIL, DodVerdict.PENDING_IMPLEMENTATION])
            == DodVerdict.FAIL
        )

    def test_pending_dominates_insufficient(self) -> None:
        # Spec rationale: extending the window helps INSUFFICIENT, not PENDING.
        verdicts = [
            DodVerdict.PASS,
            DodVerdict.INSUFFICIENT_DATA,
            DodVerdict.PENDING_IMPLEMENTATION,
        ]
        assert _aggregate(verdicts) == DodVerdict.PENDING_IMPLEMENTATION

    def test_pending_dominates_pass(self) -> None:
        assert (
            _aggregate([DodVerdict.PASS, DodVerdict.PENDING_IMPLEMENTATION])
            == DodVerdict.PENDING_IMPLEMENTATION
        )

    def test_all_pass_unchanged(self) -> None:
        # Regression guard: precedent test for non-PENDING input
        # continues to reduce to PASS.
        assert _aggregate([DodVerdict.PASS, DodVerdict.PASS]) == DodVerdict.PASS

    def test_empty_unchanged(self) -> None:
        assert _aggregate([]) == DodVerdict.INSUFFICIENT_DATA
