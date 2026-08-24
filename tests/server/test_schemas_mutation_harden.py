"""Mutation-hardening for ``parallax.server.schemas`` (land/20260824 wave 5, S3).

Additive companion to ``tests/test_event_ingest_schema.py``,
``tests/test_contracts.py``, ``tests/test_validators.py``, ``tests/test_ingest.py``,
``tests/server/`` and ``tests/router/test_crosswalk_v05_full.py``. Forty semantic
mutants were applied to a pristine tree one at a time against that whole set.

Tally — applied 40 / killed by the pre-existing suites 16 / killed by the tests
below 23 / equivalent (excluded with proof, see below) 1 / unaddressed 0.

What the existing suites could not see
--------------------------------------
This module is the system's input boundary, and the suite exercises it the way
a well-behaved client would: valid payloads, correct types, the documented
happy path. That shape kills the mutants that break a *valid* request — the
kind vocabulary, ``extra="forbid"``, the ``object`` wire alias, the two
traversal rules that reject a real ``..`` — and is blind to everything else:

* **The bounds are asserted from one side only.** ``min_length``/``max_length``
  on ``user_id``, ``vault_path``, ``subject`` and the whole
  ``EventIngestRequest`` envelope are only ever given values comfortably
  inside the range, so widening a ceiling or dropping a floor to zero changes
  nothing observable. Only a value AT the boundary distinguishes 128 from 1024.
* **``confidence`` is never given an out-of-range number.** ``ge=0.0, le=1.0``
  is what makes the field a probability rather than an arbitrary float; a
  ``le=100.0`` accepts 47 and every downstream consumer that treats it as a
  probability is silently wrong.
* **Defaults are supplied rather than defaulted.** Every backfill request in
  the suite passes ``dry_run`` and ``scope`` explicitly, so the two knobs that
  decide whether a backfill WRITES and over how much of the corpus can flip
  without a test noticing.
* **Response models are only ever built from correct values.** The
  ``Literal[1,2,3]`` on ``level`` and the ``Literal`` unions on ``kind`` and
  ``status`` are the server's own last check that it is not about to emit a
  shape its contract forbids; they are only load-bearing on a value that is
  wrong, and no test constructs one.
* **The traversal validator's edges are untested.** A single ``..`` segment is
  rejected somewhere, but the substring-vs-segment distinction (is ``a..b`` a
  legal filename?), the bare drive letter ``C:``, a leading NUL, and whether
  the caller's own spelling survives validation are all unexercised.

One equivalent mutant, excluded rather than killed
---------------------------------------------------
``payload: dict[str, Any] = Field(default_factory=dict)`` -> a bare ``= {}``.
In Pydantic v1 that would be the classic shared-mutable-default bug; in
Pydantic v2 (2.13.4 here) a mutable default is copied per instance, so the two
spellings are behaviourally identical — two models built from the same class
get independent dicts either way, and mutating one never reaches the other.
The observable property is that independence, not which spelling produces it,
so ``test_event_payload_and_judge_metadata_default_to_independent_dicts``
asserts the behaviour directly: it stays green today and would go red on any
runtime that reverted to sharing one dict, which is the condition that would
make this mutant killable again.

Expected values are literals throughout — 128, 64, 1.0, ``"sample"``, ``True``.
Reading a bound off the model field to build the expectation is what makes a
bound untestable.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from parallax.server.schemas import (
    BackfillBodyRequest,
    ErrorResponse,
    EventIngestRequest,
    HealthOkResponse,
    IngestClaimRequest,
    IngestMemoryRequest,
    IngestResponse,
    QueryResponse,
    RetrievalHitDTO,
    RouterIngestResponse,
)


def _memory(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"user_id": "u1", "vault_path": "notes/a.md"}
    base.update(over)
    return base


def _claim(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "user_id": "u1",
        "subject": "Chris",
        "predicate": "prefers",
        "object": "PowerShell",
    }
    base.update(over)
    return base


def _event(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "orbit",
        "source_instance": "orbit-prod-1",
        "schema_version": "1.0",
        "event_type": "judge.verdict",
        "run_id": "run-1",
        "record_id": "rec-1",
        "created_at": "2026-08-24T00:00:00+00:00",
        "commit_sha": "0123456789abcdef",
        "payload_hash": "sha256:" + "a" * 64,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# The strict base: whitespace normalisation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_strict_models_strip_surrounding_whitespace() -> None:
    """``str_strip_whitespace`` is a correctness setting, not cosmetics.

    ``user_id`` is a scoping key: every retrieval, every event row and every
    claim is filtered on it. Without the strip, ``"u1"`` and ``"u1 "`` are two
    different tenants that render identically in every log line and dashboard,
    so the failure mode is data silently partitioned across a value nobody can
    see. ``vault_path`` has the same problem as a dedup key.
    """
    parsed = IngestMemoryRequest(**_memory(user_id="  u1  ", vault_path="  notes/a.md  "))

    assert parsed.user_id == "u1"
    assert parsed.vault_path == "notes/a.md"


# ---------------------------------------------------------------------------
# IngestMemoryRequest bounds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_user_id_is_rejected() -> None:
    """An empty principal is not a principal.

    ``min_length=1`` is what stops ``user_id=""`` becoming a real scope that
    rows are written under and later retrieved from — a tenant nobody can name
    and no operator would think to look in.
    """
    with pytest.raises(ValidationError):
        IngestMemoryRequest(**_memory(user_id=""))


@pytest.mark.unit
def test_user_id_ceiling_is_exactly_one_hundred_and_twenty_eight() -> None:
    """128 passes, 129 does not — the boundary, not a comfortable interior value.

    The ceiling bounds an unbounded-length key that reaches SQLite indexes and
    Prometheus label values. It is asserted at the edge because every existing
    fixture uses a short id, under which 128 and 1024 are indistinguishable.
    """
    IngestMemoryRequest(**_memory(user_id="x" * 128))

    with pytest.raises(ValidationError):
        IngestMemoryRequest(**_memory(user_id="x" * 129))


@pytest.mark.unit
def test_empty_vault_path_is_rejected() -> None:
    """``vault_path`` is the memory's identity; blank is not a value."""
    with pytest.raises(ValidationError):
        IngestMemoryRequest(**_memory(vault_path=""))


# ---------------------------------------------------------------------------
# The vault_path traversal validator
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_real_parent_segment_is_rejected_in_either_separator() -> None:
    """``..`` as a path SEGMENT is the attack; both separators are checked."""
    for bad in ("notes/../etc/passwd", "notes\\..\\etc", "..", "../a.md"):
        with pytest.raises(ValidationError):
            IngestMemoryRequest(**_memory(vault_path=bad))


@pytest.mark.unit
def test_dots_inside_a_filename_are_not_a_traversal() -> None:
    """``a..b.md`` is a legal filename, and the rule is per-segment for a reason.

    Widening the check to a substring test (``".." in normalized``) looks
    stricter and therefore safer, but it rejects ordinary vault filenames —
    ``2026..draft.md``, ``a..b.md`` — with a message about traversal. A
    validator that refuses valid input is a correctness bug the "stricter is
    safer" reading hides, and nothing in the suite constructs such a name.
    """
    assert IngestMemoryRequest(**_memory(vault_path="notes/a..b.md")).vault_path == (
        "notes/a..b.md"
    )
    assert IngestMemoryRequest(**_memory(vault_path="2026..draft.md")).vault_path == (
        "2026..draft.md"
    )


@pytest.mark.unit
def test_a_bare_drive_letter_is_rejected() -> None:
    """``C:`` is the shortest absolute path there is — exactly two characters.

    The guard reads ``len(v) >= 2 and v[1] == ":"``, so the two-character case
    is the one the length check exists for and the only one that distinguishes
    ``>= 2`` from ``>= 3``. Longer drive paths (``C:/x``) pass either version,
    which is why the off-by-one survives a suite that only tries realistic
    paths.
    """
    for bad in ("C:", "c:", "D:", "C:/Windows/System32/config/SAM"):
        with pytest.raises(ValidationError):
            IngestMemoryRequest(**_memory(vault_path=bad))


@pytest.mark.unit
def test_a_leading_nul_byte_is_rejected() -> None:
    """The NUL check covers the whole string, first character included.

    A leading NUL is the interesting position, not an incidental one: it is
    what truncates a path to empty in a C API while Python still sees the rest
    of the string. Scanning from index 1 leaves precisely that case open.
    """
    for bad in ("\x00notes/a.md", "notes/\x00a.md", "notes/a.md\x00"):
        with pytest.raises(ValidationError):
            IngestMemoryRequest(**_memory(vault_path=bad))


@pytest.mark.unit
def test_the_validator_returns_the_callers_spelling_not_its_normalisation() -> None:
    """Normalisation is for the CHECK; the stored value is what was sent.

    ``normalized`` exists so a Windows-separator path is checked by the same
    rule as a POSIX one. Returning it instead of ``v`` silently rewrites the
    caller's path, so a client that sent ``notes\\a.md`` and later reads back
    ``notes/a.md`` sees the server having changed its data — and any dedup key
    computed client-side stops matching the server's.
    """
    parsed = IngestMemoryRequest(**_memory(vault_path="notes\\a.md"))

    assert parsed.vault_path == "notes\\a.md"


# ---------------------------------------------------------------------------
# IngestClaimRequest
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_claims_can_be_built_by_field_name_as_well_as_by_wire_alias() -> None:
    """``populate_by_name`` is what lets server-side code construct the model.

    ``object_`` is aliased to ``object`` on the wire to avoid shadowing the
    builtin. Without ``populate_by_name=True`` the Python-side name stops
    working, so every internal construction site — tests, CLI paths, any code
    building a claim without going through JSON — breaks while the HTTP path
    keeps passing.
    """
    by_alias = IngestClaimRequest(**_claim())
    by_name = IngestClaimRequest(
        user_id="u1", subject="Chris", predicate="prefers", object_="PowerShell"
    )

    assert by_alias.object_ == "PowerShell"
    assert by_name.object_ == "PowerShell"


@pytest.mark.unit
def test_confidence_is_a_probability_and_the_bounds_are_zero_and_one() -> None:
    """0.0 and 1.0 pass; anything outside does not.

    Every consumer reads this as a probability — arbitration thresholds, the
    review queue, the export. A widened ceiling lets ``47`` through and each of
    those silently treats it as "very confident" rather than rejecting it, so
    the bound is the only thing enforcing the field's meaning.
    """
    assert IngestClaimRequest(**_claim(confidence=0.0)).confidence == 0.0
    assert IngestClaimRequest(**_claim(confidence=1.0)).confidence == 1.0

    for bad in (1.5, -0.5, 47.0, 100.0):
        with pytest.raises(ValidationError):
            IngestClaimRequest(**_claim(confidence=bad))


@pytest.mark.unit
def test_unstated_confidence_stays_unstated() -> None:
    """The default is ``None`` — "not asserted" — never 1.0.

    Defaulting to certainty is the worst available default: every claim that
    declined to make a confidence statement would arrive downstream
    indistinguishable from one that asserted maximum confidence, and the
    arbitration path reads exactly this field.
    """
    assert IngestClaimRequest(**_claim()).confidence is None


@pytest.mark.unit
def test_claim_subject_cannot_be_empty() -> None:
    """The subject is half the claim's identity; blank makes it unaddressable."""
    with pytest.raises(ValidationError):
        IngestClaimRequest(**_claim(subject=""))


# ---------------------------------------------------------------------------
# BackfillBodyRequest — the two blast-radius knobs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_backfill_defaults_to_a_dry_run_over_a_sample() -> None:
    """Both defaults are safety defaults, and both are literals here.

    An omitted ``dry_run`` must mean "do not write" and an omitted ``scope``
    must mean "sample", so the most under-specified request a client can send
    is also the least destructive one. Every existing test passes both
    explicitly, which is exactly why flipping either default is invisible: the
    dangerous request is the one that says nothing.
    """
    req = BackfillBodyRequest(user_id="u1", crosswalk_version="v1")

    assert req.dry_run is True
    assert req.scope == "sample"


# ---------------------------------------------------------------------------
# Response contracts — the Literals that stop a bad shape reaching the wire
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hit_dto_level_admits_only_the_three_disclosure_tiers() -> None:
    """``Literal[1,2,3]`` is the server's own guard against emitting a bad tier.

    It only ever fires on a value the route should not have produced — which is
    why nothing in the suite reaches it, and why widening it to ``int`` is
    invisible until the day an off-by-one in the route ships an ``L4`` hit to a
    client whose parser has three branches.
    """
    RetrievalHitDTO(entity_kind="memory", entity_id="m1", title="t", score=1.0, level=3)

    for bad in (0, 4, -1):
        with pytest.raises(ValidationError):
            RetrievalHitDTO(
                entity_kind="memory", entity_id="m1", title="t", score=1.0, level=bad
            )


@pytest.mark.unit
def test_query_response_pins_both_its_level_and_its_kind() -> None:
    """The envelope echoes back what was asked; both fields are closed sets."""
    QueryResponse(kind="recent", level=1, count=0, hits=[])

    with pytest.raises(ValidationError):
        QueryResponse(kind="recent", level=4, count=0, hits=[])

    with pytest.raises(ValidationError):
        QueryResponse(kind="semantic", level=1, count=0, hits=[])


@pytest.mark.unit
def test_ingest_response_kind_is_a_closed_set() -> None:
    """``memory`` and ``claim`` are the only two things this endpoint ingests."""
    IngestResponse(kind="memory", id="m1", user_id="u1")

    with pytest.raises(ValidationError):
        IngestResponse(kind="source", id="s1", user_id="u1")


@pytest.mark.unit
def test_router_ingest_response_must_state_whether_it_deduped() -> None:
    """``deduped`` has no default, and that is deliberate.

    It is the flag a caller reads to learn whether its write created a row.
    Giving it a default means a construction site that forgot to set it reports
    "not deduped" — a confident false statement — instead of failing loudly at
    the boundary.
    """
    RouterIngestResponse(kind="memory", id="m1", user_id="u1", deduped=True)

    with pytest.raises(ValidationError):
        RouterIngestResponse(kind="memory", id="m1", user_id="u1")


@pytest.mark.unit
def test_health_status_is_ok_or_degraded_and_nothing_else() -> None:
    """A health flag with an open vocabulary cannot be alerted on."""
    HealthOkResponse(status="ok")
    HealthOkResponse(status="degraded")

    with pytest.raises(ValidationError):
        HealthOkResponse(status="unknown")


@pytest.mark.unit
def test_error_response_detail_is_optional() -> None:
    """``detail`` is optional so an error can be emitted with only a code.

    Making it required turns the error path into a second failure whenever the
    handler has nothing safe to say — which is precisely the sqlite case, where
    withholding the detail is the point.
    """
    assert ErrorResponse(error="database_error").detail is None


# ---------------------------------------------------------------------------
# EventIngestRequest — the dual-write envelope's bounds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_event_source_ceiling_is_exactly_sixty_four() -> None:
    """64 passes, 65 does not.

    ``source`` becomes a Prometheus label and a SQLite index key; the ceiling
    is what stops an unbounded upstream string from reaching both.
    """
    EventIngestRequest(**_event(source="x" * 64))

    with pytest.raises(ValidationError):
        EventIngestRequest(**_event(source="x" * 65))


@pytest.mark.unit
def test_event_payload_hash_ceiling_admits_a_prefixed_sha256() -> None:
    """128 is sized for a real digest — ``sha256:`` plus 64 hex is 71 characters.

    Narrowing the ceiling is the interesting mutation because it rejects
    genuine traffic rather than accepting junk: the endpoint starts 422-ing
    every well-formed envelope, and the fixture digest in the existing suite is
    short enough not to notice.
    """
    EventIngestRequest(**_event(payload_hash="x" * 128))
    EventIngestRequest(**_event(payload_hash="sha256:" + "a" * 64))

    with pytest.raises(ValidationError):
        EventIngestRequest(**_event(payload_hash="x" * 129))


@pytest.mark.unit
def test_event_created_at_cannot_be_empty() -> None:
    """An event with no timestamp is unorderable and unwindowable."""
    with pytest.raises(ValidationError):
        EventIngestRequest(**_event(created_at=""))


@pytest.mark.unit
def test_event_commit_sha_is_required() -> None:
    """Provenance is mandatory on the dual-write envelope.

    ``commit_sha`` is what ties a judged record back to the code that produced
    it. Given a default, an envelope that omits it is accepted and stored with
    an empty provenance field, which reads downstream as a real value.
    """
    payload = _event()
    del payload["commit_sha"]

    with pytest.raises(ValidationError):
        EventIngestRequest(**payload)


@pytest.mark.unit
def test_event_user_id_stays_optional() -> None:
    """System-level events have no user; the field must tolerate that.

    ``record_event`` persists these as system rows (``target_kind=None``), so
    requiring ``user_id`` would reject the envelope shape the endpoint exists
    to accept.
    """
    assert EventIngestRequest(**_event()).user_id is None


@pytest.mark.unit
def test_event_payload_and_judge_metadata_default_to_independent_dicts() -> None:
    """Two envelopes must not share one dict.

    Pydantic v2 copies a mutable default per instance, so this holds under both
    the ``default_factory=dict`` spelling and a bare ``{}`` — the assertion is
    on the behaviour that matters (independence), not on which spelling
    produces it.
    """
    a = EventIngestRequest(**_event())
    b = EventIngestRequest(**_event())

    a.payload["k"] = "v"
    a.judge_metadata["k"] = "v"

    assert b.payload == {}
    assert b.judge_metadata == {}
