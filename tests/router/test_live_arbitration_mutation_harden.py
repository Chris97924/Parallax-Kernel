"""Mutation-hardening for ``parallax.router.live_arbitration`` (land-20260823 w4 S4).

Additive companion to ``test_live_arbitration.py``. Every test below exists
because a semantic mutant of the module SURVIVED that suite (plus
``test_dual_read_router.py``, ``test_dual_read_mutation_harden.py``,
``test_arbitration_latency_106.py`` and ``test_conflict_writer.py``).
24 mutants were applied one at a time; 15 died against those suites and 9
walked through.

The existing decision matrix is genuinely strong — the ownership table, the
fail-closed ``.get`` default, ``_is_empty``, the ``requires_manual_review``
property and the sorted-keys serialisation are all pinned. What survived
clusters in four places:

  * **One cell of the emptiness matrix is missing.** The suite tests
    (populated, populated), (populated, None), (populated, empty) and
    (empty, empty) — but never (empty primary, populated secondary). That is
    the only input that exercises the ``or _is_empty(primary)`` half of the
    fallback condition, so dropping it entirely leaves every existing test
    green while a query Parallax could not answer starts reporting a winner.

  * **``reason_code`` is only ever compared to another ``reason_code``.**
    ``test_reason_code_stable_for_same_inputs`` compares two identical calls
    and ``test_reason_code_differs_across_outcomes`` compares two different
    ones; both hold for any format that is deterministic and outcome-sensitive,
    including one with the segments transposed. The docstring advertises
    ``"source-level/{query_type}/{outcome}"`` as a stable, greppable key, so it
    is pinned here as a literal.

  * **The timestamp's UNIT is unobserved.** ``decided_at_us_utc`` names its
    unit in the field name and nothing checks it, so ``// 1_000`` can become
    ``// 1_000_000`` or vanish. Every consumer that subtracts two of these
    would be off by three orders of magnitude. Pinned with a scripted clock,
    following the precedent set for the canary suite in f242400.

  * **The decoder's robustness is proved for one key only.**
    ``from_json_line`` has ``.get`` fallbacks for ``policy_version`` AND
    ``conflict_event_id`` and an ``int()`` coercion on the timestamp; only the
    ``policy_version`` one has a test, and even that asserts against the
    sentinel constant rather than its value.
"""

from __future__ import annotations

import json

import pytest

import parallax.router.live_arbitration as _la_mod
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.live_arbitration import (
    POLICY_VERSION_DEFAULT,
    POLICY_VERSION_PRE_RC,
    LiveArbitrationDecision,
    _get_or_create_arbitration_histogram,
    arbitrate,
    arbitration_latency_seconds,
)
from parallax.router.types import QueryType

# A fixed nanosecond reading with non-zero digits below every unit boundary, so
# a microsecond result, a millisecond result and a raw nanosecond result are
# three visibly different numbers.
_FIXED_NS = 1_700_000_000_123_456_789
_EXPECTED_US = 1_700_000_000_123_456


def _evidence(*ids: str) -> RetrievalEvidence:
    """RetrievalEvidence carrying one hit per id."""
    hits = tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids)
    return RetrievalEvidence(hits=hits, stages=("test",))


def _empty() -> RetrievalEvidence:
    """RetrievalEvidence with no hits — a crosswalk miss."""
    return RetrievalEvidence(hits=(), stages=("test",))


# ---------------------------------------------------------------------------
# The missing cell of the emptiness matrix
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "query_type",
    [
        QueryType.RECENT_CONTEXT,
        QueryType.ARTIFACT_CONTEXT,
        QueryType.CHANGE_TRACE,
        QueryType.TEMPORAL_CONTEXT,
        QueryType.ENTITY_PROFILE,
    ],
)
def test_an_empty_primary_forces_fallback_even_when_the_secondary_answered(
    query_type: QueryType,
) -> None:
    """The one combination the existing matrix never builds.

    Four cells are covered — (populated, populated), (populated, None),
    (populated, empty), (empty, empty) — and this fifth one, an empty PRIMARY
    beside a populated secondary, is not. It is the only input that exercises
    the ``or _is_empty(primary)`` half of the fallback condition; without it
    that term can be deleted and every test stays green.

    It matters most for ENTITY_PROFILE, where the table awards the win to
    aphelion: with the term gone, a query Parallax returned nothing for would
    be reported as a clean aphelion win rather than as the crosswalk miss it is,
    and ``requires_manual_review`` would go quiet on it.
    """
    decision = arbitrate(
        _empty(), _evidence("a", "b"), query_type, correlation_id="cid-empty-primary"
    )

    assert decision.winning_source == "fallback"
    assert decision.requires_manual_review is True


# ---------------------------------------------------------------------------
# reason_code is a greppable key, not just a deterministic string
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("primary", "secondary", "query_type", "expected"),
    [
        (
            _evidence("a"),
            _evidence("a"),
            QueryType.ENTITY_PROFILE,
            "source-level/entity_profile/aphelion",
        ),
        (
            _evidence("a"),
            _evidence("a"),
            QueryType.RECENT_CONTEXT,
            "source-level/recent_context/parallax",
        ),
        (
            _evidence("a"),
            None,
            QueryType.CHANGE_TRACE,
            "source-level/change_trace/fallback",
        ),
    ],
)
def test_the_reason_code_format_is_rule_slash_query_type_slash_outcome(
    primary: RetrievalEvidence,
    secondary: RetrievalEvidence | None,
    query_type: QueryType,
    expected: str,
) -> None:
    """Pinned as a literal, because both existing tests are self-referential.

    One compares a reason_code to another reason_code from identical inputs;
    the other compares two reason_codes from different outcomes. Every format
    that is deterministic and outcome-sensitive satisfies both — including one
    with the query-type and outcome segments transposed. The module docstring
    sells this string as "stable across calls with identical inputs" and
    machine-searchable, which is a claim about the shape, not just the
    determinism.
    """
    decision = arbitrate(primary, secondary, query_type, correlation_id="cid-reason")

    assert decision.reason_code == expected


# ---------------------------------------------------------------------------
# The timestamp's unit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_decided_at_is_stamped_in_microseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """``decided_at_us_utc`` means microseconds, and nothing checks that today.

    The field is wall-clock, so the existing suite works around it rather than
    pinning it — ``test_to_json_line_is_deterministic_byte_equal`` says as much
    and hand-rolls the second decision with a copied timestamp. That leaves the
    ``// 1_000`` divisor free: milliseconds or raw nanoseconds serialise just as
    happily, and every consumer that subtracts two of these values would be off
    by three orders of magnitude in either direction.

    Driven from a scripted clock rather than the real one, following f242400
    ("test(canary): pin both remaining wall-clock assertions to scripted
    clocks").
    """
    monkeypatch.setattr(_la_mod.time, "time_ns", lambda: _FIXED_NS)

    decision = arbitrate(
        _evidence("a"), _evidence("a"), QueryType.RECENT_CONTEXT, correlation_id="cid-clock"
    )

    assert decision.decided_at_us_utc == _EXPECTED_US
    assert len(str(decision.decided_at_us_utc)) == 16, (
        "epoch microseconds for a 2023 instant is a 16-digit number; "
        "13 digits would be milliseconds and 19 would be nanoseconds"
    )


# ---------------------------------------------------------------------------
# The decision fields arbitrate() is responsible for
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_arbitrate_leaves_conflict_event_id_unset() -> None:
    """``arbitrate`` never invents a conflict event id — Story 5 fills it in.

    Nothing asserts this: the round-trip test compares an arbitrate() result to
    itself, and the ``requires_manual_review`` tests hand-build their decisions
    with ``conflict_event_id=None`` already. Stamping the correlation id here
    instead would make every decision look like it had a recorded conflict
    event, and the conflict writer keys on exactly this field.
    """
    decision = arbitrate(
        _evidence("a"), _evidence("a"), QueryType.RECENT_CONTEXT, correlation_id="cid-abc"
    )

    assert decision.conflict_event_id is None
    assert decision.correlation_id == "cid-abc", "the correlation id has its own field"


# ---------------------------------------------------------------------------
# Decoder robustness, for the two keys that have no test
# ---------------------------------------------------------------------------


def _line_without(*omitted: str) -> str:
    """A serialized decision line with *omitted* keys left out entirely."""
    payload = {
        "winning_source": "parallax",
        "tie_breaker_rule": "source-level",
        "conflict_event_id": None,
        "policy_version": POLICY_VERSION_DEFAULT,
        "correlation_id": "cid-old",
        "query_type": QueryType.RECENT_CONTEXT.value,
        "reason_code": "source-level/recent_context/parallax",
        "decided_at_us_utc": 1714000000000000,
    }
    for key in omitted:
        del payload[key]
    return json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_a_line_written_without_conflict_event_id_still_decodes() -> None:
    """``conflict_event_id`` has a ``.get`` fallback too, and no test for it.

    ``test_from_json_line_missing_policy_version_coerces_to_sentinel`` omits
    only ``policy_version``; its payload still carries ``conflict_event_id``.
    So the second ``.get`` can be tightened to a subscript and the suite stays
    green, while any writer that omits a null field — ``json.dumps`` with a
    custom encoder, a hand-edited line, a different language's serialiser —
    starts raising KeyError on read. The decoder's stated contract is that it
    is "robust to historical data without ever raising".
    """
    restored = LiveArbitrationDecision.from_json_line(_line_without("conflict_event_id"))

    assert restored.conflict_event_id is None
    assert restored.winning_source == "parallax"


@pytest.mark.unit
def test_the_decoded_timestamp_is_always_an_int() -> None:
    """``int(...)`` on read is a coercion, not a formality.

    Every existing decode path feeds the decoder a payload it just encoded, so
    the timestamp arrives as a JSON number and the ``int()`` call is a no-op —
    which means removing it is invisible. It stops being a no-op the moment a
    line comes from anywhere else: large integers are commonly stringified on
    the wire, and a str would flow into ``decided_at_us_utc`` and break the
    first consumer that does arithmetic on it.
    """
    payload = json.loads(_line_without())
    payload["decided_at_us_utc"] = "1714000000000000"

    restored = LiveArbitrationDecision.from_json_line(json.dumps(payload))

    assert restored.decided_at_us_utc == 1714000000000000
    assert isinstance(restored.decided_at_us_utc, int)
    assert not isinstance(restored.decided_at_us_utc, str)


@pytest.mark.unit
def test_the_pre_rc_sentinel_is_the_shipped_string() -> None:
    """The sentinel's VALUE, not just its identity.

    ``test_from_json_line_missing_policy_version_coerces_to_sentinel`` asserts
    ``restored.policy_version == POLICY_VERSION_PRE_RC``, which is true for any
    value that constant could hold. It is written into decoded records and
    compared by operators against real policy versions, so the string itself is
    the contract — and it must stay distinguishable from a real one.
    """
    assert POLICY_VERSION_PRE_RC == "v0.0-pre-rc"
    assert POLICY_VERSION_DEFAULT == "v0.3.0-rc"
    assert POLICY_VERSION_PRE_RC != POLICY_VERSION_DEFAULT


# ---------------------------------------------------------------------------
# Prometheus re-registration (the #106.4 shape, on a Histogram)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_re_registering_the_arbitration_histogram_returns_the_live_collector() -> None:
    """The duplicate-registration fallback must hand back the SAME collector.

    This is the bug class of #106.4 on ``sqlite_gate._get_or_create_counter``,
    where the fallback looked the collector up under a key that did not exist
    and raised KeyError instead of returning it. The helper's
    ``except ValueError`` branch only runs on a duplicate registration — a
    module reload, or pytest importing the tree twice — so nothing in the suite
    reaches it, and the lookup key can be wrong without anyone noticing until
    an import order changes and the whole module fails to load. Calling the
    helper a second time IS that duplicate.

    A note on the key, since the source comment is easy to over-read: this
    prometheus_client registers a Histogram under five names — the bare metric
    name plus ``_bucket`` / ``_sum`` / ``_count`` / ``_created`` — and all five
    resolve to the same collector, so ``_bucket`` and the bare name are
    interchangeable HERE. A ``_total`` suffix (the shape the #106.4 bug had)
    exists for none of them, and that is what this test actually catches.
    """
    again = _get_or_create_arbitration_histogram()

    assert again is arbitration_latency_seconds
