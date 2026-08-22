"""Mutation-hardening for ``parallax.apex.envelope`` (land-20260823 wave 4, S4).

Additive companion to ``tests/apex/test_envelope.py``. Every test below was
written against a semantic mutant that the pre-existing suite let through.

The existing file walks the nine validation rules one malformed field at a
time, which is thorough about *which* inputs are rejected and silent about
four other things:

  * **One defect at a time hides every ordering decision.** Each case deletes
    one field, or adds one unknown key. ``sorted(missing)[0]`` and
    ``sorted(unknown)[0]`` therefore have nothing to sort, and the
    missing-before-unknown sequencing has nothing to sequence. Replace either
    ``sorted(...)[0]`` with an arbitrary set element and the suite cannot
    tell — but the message an operator reads then changes between runs of the
    same failing deploy, because set iteration order follows PYTHONHASHSEED.

  * **``StrEnum`` makes the coercion invisible to ``==``.** The round-trip
    case asserts ``env.source == Source.APHELION``, which is equally true for
    the raw string ``"aphelion"`` — that is what ``StrEnum`` is for. So
    dropping the ``Source(...)`` / ``PayloadType(...)`` construction leaves a
    DTO whose fields are plain strings, typed as enums, with the suite green.
    Identity is what separates them.

  * **Spec §4.2's error taxonomy is proven for a missing checksum, not a
    malformed one.** ``EnvelopeChecksumError`` is reserved for a
    recompute mismatch; a checksum of the wrong *shape* is structural. The
    only shape-vs-mismatch case in the suite is an absent field, so hoisting
    the recompute above the shape guard — or dropping the shape guard, whose
    work the comparison appears to redo — reclassifies malformed input as
    tampering, and the downstream router maps the two to different reasons.

  * **The DTO's own immutability is unasserted.** The payload's freezing has
    two dedicated cases; the dataclass holding it has none, even though a
    mutable ``checksum`` beside a frozen ``payload`` is the more dangerous
    half — it is the field the audit chain is compared against.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from parallax.apex import (
    EnvelopeChecksumError,
    EnvelopeValidationError,
    PayloadType,
    Source,
    parse_envelope,
)
from parallax.apex import envelope as envelope_mod

pytestmark = pytest.mark.unit

# Split across adjacent literals purely to keep any single source line under
# the repo secret-scan hook's 40-hex-run threshold; Python concatenates them at
# compile time, so both remain literal expected values.
VALID_AUDIT_REF = (
    "8e4f2b7c1a9d3e6f5c8b2a4d7e1f3c9b"
    "8e4f2b7c1a9d3e6f5c8b2a4d7e1f3c9b"
)
VALID_MSG_ID = "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc"
SPEC_PAYLOAD = {"claim_id": "c-001", "confidence": 0.92}
SPEC_CHECKSUM = (
    "584cba361917b908082e0b25f792ce93"
    "2b0db25519caa05a2995e8a341c1cfbe"
)


def _valid_envelope(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "envelope_version": "0.1",
        "schema_version": 1,
        "message_id": VALID_MSG_ID,
        "created_at": "2026-05-09T14:23:11Z",
        "source": "aphelion",
        "audit_db_ref": VALID_AUDIT_REF,
        "payload_type": "query_result",
        "payload": dict(SPEC_PAYLOAD),
        "checksum": SPEC_CHECKSUM,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Deterministic diagnostics when more than one thing is wrong
# ---------------------------------------------------------------------------


class TestErrorReportDeterminism:
    def test_two_missing_fields_report_the_first_in_sorted_order(self) -> None:
        env = _valid_envelope()
        del env["source"]
        del env["created_at"]
        with pytest.raises(
            EnvelopeValidationError, match="missing required field 'created_at'"
        ):
            parse_envelope(env)

    def test_two_unknown_fields_report_the_first_in_sorted_order(self) -> None:
        env = _valid_envelope(zulu_extra=1, alpha_extra=2)
        with pytest.raises(EnvelopeValidationError, match="unknown field 'alpha_extra'"):
            parse_envelope(env)

    def test_missing_field_is_reported_before_unknown_field(self) -> None:
        """Absence outranks surplus.

        A caller that dropped a field *and* added one is almost always running
        a different schema version; the missing field is what names the
        version gap, and the unknown key is usually its replacement. Swapping
        the two blocks reports the new field as garbage and never mentions the
        one that is actually required.
        """
        env = _valid_envelope(some_new_field=1)
        del env["audit_db_ref"]
        with pytest.raises(
            EnvelopeValidationError, match="missing required field 'audit_db_ref'"
        ):
            parse_envelope(env)


# ---------------------------------------------------------------------------
# Enum coercion — identity, because StrEnum equality proves nothing
# ---------------------------------------------------------------------------


class TestEnumCoercion:
    def test_source_is_the_enum_member_not_the_raw_string(self) -> None:
        env = parse_envelope(_valid_envelope())
        assert env.source is Source.APHELION

    def test_payload_type_is_the_enum_member_not_the_raw_string(self) -> None:
        env = parse_envelope(_valid_envelope())
        assert env.payload_type is PayloadType.QUERY_RESULT

    def test_parallax_source_is_also_coerced(self) -> None:
        """The second member of each enum, which no case reaches.

        ``source="parallax"`` never appears in the suite — only ``aphelion``
        and an invalid value — so half of a two-member closed enum is
        unexercised on the accepting side.
        """
        env = parse_envelope(_valid_envelope(source="parallax"))
        assert env.source is Source.PARALLAX

    def test_event_payload_type_is_also_coerced(self) -> None:
        env = parse_envelope(
            _valid_envelope(payload_type="event")
        )
        assert env.payload_type is PayloadType.EVENT


# ---------------------------------------------------------------------------
# Spec §4.2 taxonomy — shape first, then recompute
# ---------------------------------------------------------------------------


class TestChecksumErrorTaxonomy:
    def test_uppercase_checksum_is_structural_not_a_mismatch(self) -> None:
        """The right digest in the wrong case is malformed, not tampered.

        Both errors are ``ValueError`` subclasses but they are siblings, and
        the adapter maps a mismatch to ``envelope_checksum_mismatch`` — an
        integrity alarm — while a structural error is a schema complaint.
        Checking the shape after the recompute (or not at all: the comparison
        looks like it subsumes the pattern) turns every case-mangled digest
        into a false integrity alarm.
        """
        env = _valid_envelope(checksum=SPEC_CHECKSUM.upper())
        with pytest.raises(EnvelopeValidationError, match="64-char lowercase"):
            parse_envelope(env)

    def test_short_checksum_is_structural_not_a_mismatch(self) -> None:
        env = _valid_envelope(checksum="abc123")
        with pytest.raises(EnvelopeValidationError, match="64-char lowercase"):
            parse_envelope(env)

    def test_non_string_checksum_is_structural_not_a_mismatch(self) -> None:
        env = _valid_envelope(checksum=12345)
        with pytest.raises(EnvelopeValidationError, match="64-char lowercase"):
            parse_envelope(env)

    def test_wrong_but_well_formed_checksum_is_still_a_mismatch(self) -> None:
        """The other side of the same line — the shape guard must not swallow
        the case ``EnvelopeChecksumError`` exists for."""
        env = _valid_envelope(checksum="f" * 64)
        with pytest.raises(EnvelopeChecksumError, match="checksum mismatch"):
            parse_envelope(env)


# ---------------------------------------------------------------------------
# schema_version — int, and only int
# ---------------------------------------------------------------------------


class TestSchemaVersionType:
    @pytest.mark.parametrize("value", [1.0, 2.5, "1"])
    def test_non_int_schema_version_rejected(self, value: Any) -> None:
        """``1.0`` is not ``1``.

        The suite pins the two special cases the code names explicitly
        (``bool``, and ``0``), so the plain ``isinstance(..., int)`` guard is
        only ever exercised by a value that also fails another rule. A JSON
        document with ``"schema_version": 1.0`` is entirely ordinary — that is
        what a float-typed field looks like coming out of a serializer that
        does not distinguish them.
        """
        with pytest.raises(EnvelopeValidationError, match="schema_version"):
            parse_envelope(_valid_envelope(schema_version=value))

    def test_negative_schema_version_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError, match="must be >= 1"):
            parse_envelope(_valid_envelope(schema_version=-1))


# ---------------------------------------------------------------------------
# The DTO itself
# ---------------------------------------------------------------------------


class TestEnvelopeDtoImmutability:
    def test_envelope_fields_cannot_be_reassigned(self) -> None:
        """``frozen=True`` is load bearing for ``checksum`` above all.

        The payload's freezing has two dedicated cases; the dataclass around
        it has none. An envelope whose ``checksum`` can be reassigned after
        validation is one where the recompute proves nothing about the value
        a later reader sees.
        """
        env = parse_envelope(_valid_envelope())
        with pytest.raises(dataclasses.FrozenInstanceError):
            env.checksum = "f" * 64  # type: ignore[misc]

    def test_envelope_payload_cannot_be_reassigned(self) -> None:
        env = parse_envelope(_valid_envelope())
        with pytest.raises(dataclasses.FrozenInstanceError):
            env.payload = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Input shape
# ---------------------------------------------------------------------------


class TestNonMappingInput:
    @pytest.mark.parametrize(
        "raw", [["envelope_version", "0.1"], "envelope", 42, None]
    )
    def test_non_mapping_input_is_a_validation_error(self, raw: Any) -> None:
        """The guard that keeps a wrong-typed body from becoming a traceback.

        Nothing in the suite passes a non-mapping, so removing the check
        leaves the first ``raw.keys()`` to raise ``AttributeError`` — which
        the ingest boundary does not catch, so a malformed request body
        becomes a 500 instead of a rejected envelope.
        """
        with pytest.raises(EnvelopeValidationError, match="must be a mapping"):
            parse_envelope(raw)


class TestUuidHelperErrorTranslation:
    @pytest.mark.parametrize("value", [None, 42, b"not-a-uuid"])
    def test_non_string_uuid_input_is_translated(self, value: Any) -> None:
        """``_validate_uuid_v4`` catches TypeError/AttributeError on purpose.

        ``parse_envelope`` type-checks ``message_id`` before calling it, so
        those two arms are unreachable from there and the helper's own
        contract — every bad input leaves as an ``EnvelopeValidationError`` —
        is untested. Narrowing the except clause to ``ValueError`` alone lets
        a raw ``TypeError`` escape any future caller that trusts the
        docstring.
        """
        with pytest.raises(EnvelopeValidationError, match="not a valid UUID"):
            envelope_mod._validate_uuid_v4(value, field="message_id")
