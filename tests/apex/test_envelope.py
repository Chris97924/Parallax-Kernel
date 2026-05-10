"""Tests for parallax.apex.envelope (Apex M5 wire format).

Spec ground truth: docs/m5-prep/apex-m5-envelope-spec.md v0.1-frozen.
"""

from __future__ import annotations

from typing import Any

import pytest

from parallax.apex import (
    Envelope,
    EnvelopeChecksumError,
    EnvelopeValidationError,
    PayloadType,
    Source,
    canonical_payload_bytes,
    compute_checksum,
    parse_envelope,
)
from parallax.apex.canonical_json import canonical_dumps

VALID_AUDIT_REF = "8e4f2b7c1a9d3e6f5c8b2a4d7e1f3c9b8e4f2b7c1a9d3e6f5c8b2a4d7e1f3c9b"
VALID_MSG_ID = "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc"
SPEC_PAYLOAD = {"claim_id": "c-001", "confidence": 0.92}
SPEC_CHECKSUM = "584cba361917b908082e0b25f792ce932b0db25519caa05a2995e8a341c1cfbe"


def _valid_envelope(**overrides: Any) -> dict[str, Any]:
    base = {
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


# ---- Canonical serialization (spec §4.1) -------------------------------------


@pytest.mark.unit
class TestCanonicalSerialization:
    def test_spec_example_payload_byte_match(self) -> None:
        # Spec §6.1: SHA-256 of canonical {"claim_id":"c-001","confidence":0.92}
        # is exactly 584cba36...c1cfbe. The bytes must match for the digest.
        bytes_out = canonical_payload_bytes(SPEC_PAYLOAD)
        assert bytes_out == b'{"claim_id":"c-001","confidence":0.92}'
        assert compute_checksum(SPEC_PAYLOAD) == SPEC_CHECKSUM

    def test_keys_are_lex_sorted(self) -> None:
        # Insertion order zeta-alpha-mu must round-trip to alpha-mu-zeta.
        out = canonical_dumps({"zeta": 1, "alpha": 2, "mu": 3})
        assert out == b'{"alpha":2,"mu":3,"zeta":1}'

    def test_no_whitespace(self) -> None:
        out = canonical_dumps({"a": 1, "b": [1, 2, 3]})
        assert b" " not in out
        assert b"\n" not in out

    def test_nfc_applied_to_keys_and_values(self) -> None:
        # NFC: ñ (U+00F1) and n + combining tilde (U+006E U+0303) must
        # produce identical bytes.
        composed_key = "mañana"
        decomposed_key = "mañana"
        composed_val = "cañón"
        decomposed_val = "cañón"
        a = canonical_dumps({composed_key: composed_val})
        b = canonical_dumps({decomposed_key: decomposed_val})
        assert a == b

    def test_nfc_key_collision_raises(self) -> None:
        # Regression: round-6 P1 — two distinct keys that NFC-normalize to
        # the same string must raise instead of silently last-write-wins.
        composed = "é"  # é (U+00E9)
        decomposed = "é"  # e + combining acute
        with pytest.raises(ValueError, match="NFC key collision"):
            canonical_dumps({composed: 1, decomposed: 2})

    def test_nfc_key_collision_in_nested_dict_raises(self) -> None:
        # Regression: round-6 P1 — recursion must also catch collisions
        # in nested dicts, not just the top level.
        with pytest.raises(ValueError, match="NFC key collision"):
            canonical_dumps({"outer": {"é": 1, "é": 2}})


# ---- Envelope parsing (spec §2 + §4) -----------------------------------------


@pytest.mark.unit
class TestEnvelopeParsing:
    def test_spec_example_6_1_round_trip(self) -> None:
        env = parse_envelope(_valid_envelope())
        assert env.envelope_version == "0.1"
        assert env.schema_version == 1
        assert env.source == Source.APHELION
        assert env.payload_type == PayloadType.QUERY_RESULT
        assert env.checksum == SPEC_CHECKSUM
        assert isinstance(env, Envelope)

    @pytest.mark.parametrize(
        "missing_field",
        [
            "envelope_version",
            "schema_version",
            "message_id",
            "created_at",
            "source",
            "audit_db_ref",
            "payload_type",
            "payload",
            "checksum",
        ],
    )
    def test_each_required_field_missing_raises(self, missing_field: str) -> None:
        env = _valid_envelope()
        del env[missing_field]
        pattern = f"missing required field '{missing_field}'"
        with pytest.raises(EnvelopeValidationError, match=pattern):
            parse_envelope(env)

    def test_unknown_top_level_key_rejected(self) -> None:
        env = _valid_envelope(extra="not allowed")
        with pytest.raises(EnvelopeValidationError, match="unknown field 'extra'"):
            parse_envelope(env)

    def test_envelope_version_must_be_exactly_0_1(self) -> None:
        env = _valid_envelope(envelope_version="0.2")
        with pytest.raises(EnvelopeValidationError, match="unsupported envelope_version"):
            parse_envelope(env)

    def test_schema_version_rejects_bool(self) -> None:
        # bool is int subclass — must be rejected explicitly.
        env = _valid_envelope(schema_version=True)
        with pytest.raises(EnvelopeValidationError, match="schema_version must be int"):
            parse_envelope(env)

    def test_schema_version_rejects_zero(self) -> None:
        env = _valid_envelope(schema_version=0)
        with pytest.raises(EnvelopeValidationError, match="schema_version must be >= 1"):
            parse_envelope(env)

    def test_message_id_must_be_uuid_v4(self) -> None:
        # UUID v7 (used for claim_id elsewhere) is wrong shape for envelope.
        env = _valid_envelope(message_id="0193e2b1-0001-7000-8000-000000000001")
        with pytest.raises(EnvelopeValidationError, match="UUIDv4"):
            parse_envelope(env)

    def test_created_at_rejects_fractional_seconds(self) -> None:
        env = _valid_envelope(created_at="2026-05-09T14:23:11.500Z")
        with pytest.raises(EnvelopeValidationError, match="20-char ISO 8601"):
            parse_envelope(env)

    def test_invalid_source_value(self) -> None:
        env = _valid_envelope(source="other")
        with pytest.raises(EnvelopeValidationError, match="invalid source"):
            parse_envelope(env)

    def test_unknown_payload_type(self) -> None:
        env = _valid_envelope(payload_type="metadata")
        with pytest.raises(EnvelopeValidationError, match="unknown payload_type"):
            parse_envelope(env)

    def test_audit_db_ref_must_be_64_lowercase_hex(self) -> None:
        env = _valid_envelope(audit_db_ref="ABC")
        with pytest.raises(EnvelopeValidationError, match="audit_db_ref"):
            parse_envelope(env)
        env2 = _valid_envelope(audit_db_ref=VALID_AUDIT_REF.upper())
        with pytest.raises(EnvelopeValidationError, match="audit_db_ref"):
            parse_envelope(env2)

    def test_payload_must_be_object(self) -> None:
        env = _valid_envelope(payload="not-an-object")
        with pytest.raises(EnvelopeValidationError, match="payload must be an object"):
            parse_envelope(env)


# ---- Checksum validation (spec §4.1 + §4.2) ---------------------------------


@pytest.mark.unit
class TestChecksum:
    def test_correct_checksum_passes(self) -> None:
        # already covered by spec §6.1 test, but explicit assertion path
        env = parse_envelope(_valid_envelope())
        assert env.checksum == SPEC_CHECKSUM

    def test_wrong_checksum_raises_checksum_error(self) -> None:
        # Provide a syntactically valid checksum that doesn't match payload.
        wrong = "f" * 64
        env = _valid_envelope(checksum=wrong)
        with pytest.raises(EnvelopeChecksumError, match="checksum mismatch"):
            parse_envelope(env)

    def test_payload_mutation_breaks_checksum(self) -> None:
        # If payload is altered without recomputing checksum, parse fails.
        env = _valid_envelope(payload={"claim_id": "different", "confidence": 0.92})
        with pytest.raises(EnvelopeChecksumError):
            parse_envelope(env)

    def test_missing_checksum_is_validation_error_not_checksum_error(self) -> None:
        # Spec §4.2 taxonomy: required-field absence is structural,
        # not a checksum mismatch.
        env = _valid_envelope()
        del env["checksum"]
        with pytest.raises(EnvelopeValidationError):
            parse_envelope(env)


# ---- Checksum serialisation error taxonomy (round-4 P1) ---------------------


@pytest.mark.unit
class TestChecksumSerializationTaxonomy:
    def test_payload_non_serializable_raises_envelope_validation_error(self) -> None:
        # Regression: round-4 P1 — compute_checksum(payload) can raise raw
        # ValueError/TypeError on NaN/Infinity/non-serializable objects;
        # parse_envelope must map these to EnvelopeValidationError.
        for bad_payload in (
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": float("-inf")},
            {"value": {1, 2, 3}},  # set is not JSON-serialisable
        ):
            env = _valid_envelope(payload=bad_payload)
            with pytest.raises(EnvelopeValidationError, match="canonically serializable"):
                parse_envelope(env)


# ---- Envelope.payload immutability (round-7 P2) -----------------------------


@pytest.mark.unit
class TestEnvelopePayloadImmutability:
    def test_payload_is_immutable_after_parse(self) -> None:
        # Regression: round-7 P2 — parse_envelope used to return Envelope
        # backed by a plain dict; callers could mutate env.payload while
        # env.checksum still reflected the old bytes. Top-level payload
        # mapping must be read-only.
        env = parse_envelope(_valid_envelope())

        with pytest.raises(TypeError):
            env.payload["claim_id"] = "tampered"  # type: ignore[index]

        with pytest.raises(TypeError):
            env.payload["new_key"] = "x"  # type: ignore[index]

        with pytest.raises(TypeError):
            del env.payload["claim_id"]  # type: ignore[attr-defined]

    def test_payload_unaliased_from_caller_input(self) -> None:
        # dict() copy + MappingProxyType wrap means mutating the original
        # raw['payload'] post-parse must NOT change env.payload top-level.
        raw = _valid_envelope()
        env = parse_envelope(raw)
        original_keys = set(env.payload.keys())
        raw["payload"]["injected"] = "x"
        assert set(env.payload.keys()) == original_keys


# ---- created_at semantic validation (round-3 P2) ----------------------------


@pytest.mark.unit
class TestCreatedAtSemanticValidation:
    def test_impossible_calendar_date_rejected(self) -> None:
        # Regression: round-3 P2 — regex matched shape but accepted e.g.
        # month=99 / day=99.  strptime round-trip now catches these.
        for bad in ("2026-99-01T00:00:00Z", "2026-01-99T00:00:00Z", "2026-02-30T00:00:00Z"):
            with pytest.raises(EnvelopeValidationError, match="valid calendar timestamp"):
                parse_envelope(_valid_envelope(created_at=bad))
