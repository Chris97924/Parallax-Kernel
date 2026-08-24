"""US-003: Tests for request/response contract dataclasses."""

from __future__ import annotations

import dataclasses
import json

import pytest

from parallax.router.contracts import (
    ArbitrationDecision,
    BackfillReport,
    BackfillRequest,
    FieldCandidate,
    HealthReport,
    IngestRequest,
    IngestResult,
    MappingState,
    QueryRequest,
    QueryType,
    RetrievalEvidence,
)
from parallax.router.mock_adapter import MockMemoryRouter

# ---------------------------------------------------------------------------
# Re-export check
# ---------------------------------------------------------------------------


def test_retrieval_evidence_reexported() -> None:
    """RetrievalEvidence must be importable from contracts, not redefined."""
    from parallax.retrieval.contracts import RetrievalEvidence as Orig

    assert RetrievalEvidence is Orig


# ---------------------------------------------------------------------------
# Frozen / FrozenInstanceError checks
# ---------------------------------------------------------------------------


def test_query_request_frozen() -> None:
    r = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.user_id = "other"  # type: ignore[misc]


def test_ingest_request_frozen() -> None:
    r = IngestRequest(user_id="u1", kind="memory", payload={"body": "hi"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.user_id = "other"  # type: ignore[misc]


def test_ingest_result_frozen() -> None:
    r = IngestResult(kind="memory", identifier="abc", deduped=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.identifier = "xyz"  # type: ignore[misc]


def test_backfill_request_frozen() -> None:
    r = BackfillRequest(user_id="u1", crosswalk_version="v1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.user_id = "other"  # type: ignore[misc]


def test_backfill_report_frozen() -> None:
    r = BackfillReport(
        rows_examined=10,
        rows_mapped=5,
        rows_unmapped=3,
        rows_conflict=2,
        writes_performed=0,
        arbitrations=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.rows_examined = 99  # type: ignore[misc]


def test_health_report_frozen() -> None:
    r = HealthReport(
        ok=True,
        flag_enabled=False,
        query_type_count=5,
        ports_registered=("QueryPort",),
        crosswalk_seed_hash="abc",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ok = False  # type: ignore[misc]


def test_arbitration_decision_frozen() -> None:
    fc = FieldCandidate(source="s", field_name="f", value=1, confidence=0.9)
    d = ArbitrationDecision(
        canonical_field="f",
        state=MappingState.MAPPED,
        selected=fc,
        candidates=(fc,),
        reason_code="ok",
        reason="fine",
        confidence=0.9,
        requires_manual_review=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.canonical_field = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Field name + type checks via dataclasses.fields
# ---------------------------------------------------------------------------


def test_query_request_fields() -> None:
    names = [f.name for f in dataclasses.fields(QueryRequest)]
    # ADR-007: params added as additive optional field (default None)
    assert names == ["query_type", "user_id", "q", "limit", "since", "until", "level", "params"]


def test_ingest_request_fields() -> None:
    names = [f.name for f in dataclasses.fields(IngestRequest)]
    assert names == ["user_id", "kind", "payload", "source_id"]


def test_ingest_result_fields() -> None:
    names = [f.name for f in dataclasses.fields(IngestResult)]
    assert names == ["kind", "identifier", "deduped"]


def test_backfill_request_fields() -> None:
    names = [f.name for f in dataclasses.fields(BackfillRequest)]
    assert names == ["user_id", "crosswalk_version", "dry_run", "scope"]


def test_backfill_report_fields() -> None:
    names = [f.name for f in dataclasses.fields(BackfillReport)]
    assert names == [
        "rows_examined",
        "rows_mapped",
        "rows_unmapped",
        "rows_conflict",
        "writes_performed",
        "arbitrations",
    ]


def test_health_report_fields() -> None:
    names = [f.name for f in dataclasses.fields(HealthReport)]
    assert names == [
        "ok",
        "flag_enabled",
        "query_type_count",
        "ports_registered",
        "crosswalk_seed_hash",
    ]


def test_arbitration_decision_fields() -> None:
    names = [f.name for f in dataclasses.fields(ArbitrationDecision)]
    assert names == [
        "canonical_field",
        "state",
        "selected",
        "candidates",
        "reason_code",
        "reason",
        "confidence",
        "requires_manual_review",
    ]


# ---------------------------------------------------------------------------
# BackfillReport dry_run invariant
# ---------------------------------------------------------------------------


def test_frozen_mock_cannot_violate_the_dry_run_invariant() -> None:
    """The dry-run invariant is an *adapter* obligation, not a dataclass one.

    Named for what it asserts. The old name
    (``test_backfill_report_dry_run_writes_zero``) advertised coverage of the
    dry-run invariant that this test does not have — the same "signal that says
    one thing and does another" class the rest of this branch set out to fix.
    Kept rather than deleted as a duplicate of
    ``test_contract_skeleton.py::test_backfill_raises_not_implemented``, because
    this is the file a reader looking for the BackfillReport invariant opens,
    and the docstring below is the signpost to where it is really enforced.

    ``BackfillReport`` has no ``__post_init__``, so constructing one with
    ``dry_run=True`` and ``writes_performed=5`` here would prove nothing about
    the system — which is why this was a non-strict xfail placeholder that
    raised ``AssertionError`` rather than a test.

    What is assertable at this layer is the boundary itself: the frozen mock
    adapter does not implement ``backfill`` at all, so it cannot violate the
    invariant, and any implementation that starts returning a report has to
    turn this red first. The invariant proper is enforced against the real
    adapter in ``tests/router/test_real_adapter_backfill.py`` and
    ``tests/router/test_backfill_runner.py``.
    """
    router = MockMemoryRouter()
    request = BackfillRequest(user_id="u1", crosswalk_version="v1")
    with pytest.raises(NotImplementedError, match=r"MockMemoryRouter\.backfill"):
        router.backfill(request)


# ---------------------------------------------------------------------------
# ArbitrationDecision.to_json_line()
# ---------------------------------------------------------------------------


def test_arbitration_decision_to_json_line_stable() -> None:
    fc = FieldCandidate(source="src", field_name="body", value="hello", confidence=0.8)
    d = ArbitrationDecision(
        canonical_field="body",
        state=MappingState.MAPPED,
        selected=fc,
        candidates=(fc,),
        reason_code="exact",
        reason="single source",
        confidence=0.8,
        requires_manual_review=False,
    )
    line1 = d.to_json_line()
    line2 = d.to_json_line()
    assert line1 == line2  # deterministic across two calls


def test_arbitration_decision_to_json_line_roundtrip() -> None:
    fc = FieldCandidate(source="src", field_name="body", value="hello", confidence=0.8)
    d = ArbitrationDecision(
        canonical_field="body",
        state=MappingState.CONFLICT,
        selected=None,
        candidates=(fc,),
        reason_code="conflict",
        reason="two sources disagree",
        confidence=0.4,
        requires_manual_review=True,
    )
    parsed = json.loads(d.to_json_line())
    assert parsed["state"] == MappingState.CONFLICT.value  # string value, not enum
    assert parsed["canonical_field"] == "body"
    assert parsed["selected"] is None
