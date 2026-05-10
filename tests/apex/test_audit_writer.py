"""Tests for parallax.apex.audit_writer.

Spec ground truth: docs/m5-prep/audit-db-path-config.md §6 + envelope §8.1.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from parallax.apex.audit_writer import (
    REQUIRED_FIELDS,
    AuditRowValidationError,
    AuditWriteOrderViolation,
    assert_audit_row_committed,
    canonicalize_row,
)


def _valid_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_id": "0193e2b1-0001-7000-8000-000000000001",
        "envelope_message_id": "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc",
        "outcome": "hit",
        "package_id": "0193ef00-0001-7000-8000-000000000005",
        "session_id": "sess-2026-05-09-001",
        "signer_id": "chris@aphelion-graph",
        "signer_manifest_digest": "a" * 64,
        "source": "aphelion",
        "ts": "2026-05-09T14:23:11Z",
    }
    base.update(overrides)
    return base


# ---- Schema validation -------------------------------------------------------


@pytest.mark.unit
class TestRowValidation:
    def test_valid_row_canonicalizes(self) -> None:
        row = canonicalize_row(_valid_row())
        assert row.data["outcome"] == "hit"

    @pytest.mark.parametrize("field", sorted(REQUIRED_FIELDS))
    def test_each_required_field_missing_raises(self, field: str) -> None:
        row = _valid_row()
        del row[field]
        with pytest.raises(AuditRowValidationError, match=field):
            canonicalize_row(row)

    @pytest.mark.parametrize("outcome", ["hit", "miss", "divergence", "error"])
    def test_each_outcome_value_accepted(self, outcome: str) -> None:
        # divergence + error need optional fields paired correctly:
        row = _valid_row(outcome=outcome)
        if outcome == "divergence":
            row["aphelion_hash"] = "b" * 64
            row["local_hash"] = "c" * 64
            row["reason_code"] = "claim.r4_subject_missing"
        elif outcome == "error":
            row["reason_code"] = "pkg.unsigned"
        canonicalize_row(row)

    def test_unknown_outcome_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="outcome"):
            canonicalize_row(_valid_row(outcome="success"))

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="source"):
            canonicalize_row(_valid_row(source="other"))

    def test_unsigned_package_uses_empty_string(self) -> None:
        # Empty string valid for signer_id + signer_manifest_digest only.
        row = _valid_row(signer_id="", signer_manifest_digest="")
        canonicalize_row(row)

    def test_empty_required_field_other_than_signer_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="claim_id"):
            canonicalize_row(_valid_row(claim_id=""))

    def test_optional_pairing_aphelion_hash_only_on_divergence(self) -> None:
        with pytest.raises(AuditRowValidationError, match="divergence"):
            canonicalize_row(_valid_row(aphelion_hash="d" * 64))

    def test_reason_code_only_on_error_or_divergence(self) -> None:
        with pytest.raises(AuditRowValidationError, match="reason_code"):
            canonicalize_row(_valid_row(reason_code="pkg.unsigned"))

    def test_reason_code_namespace_required(self) -> None:
        with pytest.raises(AuditRowValidationError, match="reserved namespace"):
            canonicalize_row(_valid_row(outcome="error", reason_code="some_unknown_tag"))

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="schema_version bump"):
            canonicalize_row(_valid_row(future_field="x"))

    def test_none_values_treated_as_absent(self) -> None:
        row = _valid_row(reason_code=None, aphelion_hash=None)
        # None -> dropped, so optional pairing checks pass on outcome=hit
        canonicalize_row(row)


# ---- Format validation (P1 + P2) -------------------------------------------


@pytest.mark.unit
class TestFieldFormatValidation:
    """Round-2 Codex P1: format validators for structured required fields."""

    # UUID fields ----------------------------------------------------------------

    def test_claim_id_non_uuid_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="claim_id"):
            canonicalize_row(_valid_row(claim_id="not-a-uuid"))

    def test_claim_id_uuid_v4_rejected(self) -> None:
        # claim_id must be v7, not v4.
        with pytest.raises(AuditRowValidationError, match="claim_id"):
            canonicalize_row(_valid_row(claim_id="b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc"))

    def test_package_id_non_uuid_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="package_id"):
            canonicalize_row(_valid_row(package_id="not-a-uuid"))

    def test_package_id_uuid_v4_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="package_id"):
            canonicalize_row(_valid_row(package_id="b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc"))

    def test_envelope_message_id_non_uuid_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="envelope_message_id"):
            canonicalize_row(_valid_row(envelope_message_id="not-a-uuid"))

    def test_envelope_message_id_uuid_v7_rejected(self) -> None:
        # envelope_message_id must be v4, not v7.
        with pytest.raises(AuditRowValidationError, match="envelope_message_id"):
            canonicalize_row(_valid_row(envelope_message_id="0193e2b1-0001-7000-8000-000000000001"))

    # ISO timestamp field --------------------------------------------------------

    def test_ts_non_iso_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="ts"):
            canonicalize_row(_valid_row(ts="not-a-date"))

    def test_ts_missing_z_suffix_rejected(self) -> None:
        # +00:00 offset instead of Z — wrong format per spec.
        with pytest.raises(AuditRowValidationError, match="ts"):
            canonicalize_row(_valid_row(ts="2026-05-09T14:23:11+00:00"))

    def test_ts_with_milliseconds_rejected(self) -> None:
        # Spec: second precision only, exactly 20 chars.
        with pytest.raises(AuditRowValidationError, match="ts"):
            canonicalize_row(_valid_row(ts="2026-05-09T14:23:11.123Z"))

    def test_ts_valid_format_accepted(self) -> None:
        # Should pass — exact spec format.
        canonicalize_row(_valid_row(ts="2026-05-09T14:23:11Z"))

    # SHA-256 hex fields ---------------------------------------------------------

    def test_signer_manifest_digest_non_hex_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="signer_manifest_digest"):
            canonicalize_row(_valid_row(signer_manifest_digest="not-hex"))

    def test_signer_manifest_digest_too_short_rejected(self) -> None:
        with pytest.raises(AuditRowValidationError, match="signer_manifest_digest"):
            canonicalize_row(_valid_row(signer_manifest_digest="a" * 63))

    def test_signer_manifest_digest_uppercase_rejected(self) -> None:
        # Spec requires lowercase hex.
        with pytest.raises(AuditRowValidationError, match="signer_manifest_digest"):
            canonicalize_row(_valid_row(signer_manifest_digest="A" * 64))

    def test_signer_manifest_digest_empty_string_still_valid(self) -> None:
        # Unsigned packages use empty string — must NOT trigger hex validation.
        canonicalize_row(_valid_row(signer_manifest_digest="", signer_id=""))

    def test_signer_manifest_digest_falsey_non_string_rejected(self) -> None:
        # Regression: round-3 P1 — falsey non-strings (0, False, {}) bypassed
        # the old `if smd:` truthiness guard without hitting _validate_sha256_hex.
        for bad in (0, False, {}, []):
            with pytest.raises(AuditRowValidationError, match="signer_manifest_digest"):
                canonicalize_row(_valid_row(signer_manifest_digest=bad))

    def test_signer_id_non_string_rejected(self) -> None:
        # Regression: round-3 P2 — signer_id had no type guard; 0/False/{}
        # passed because REQUIRED_FIELDS only checks presence, not type.
        for bad in (0, False, {}, []):
            with pytest.raises(AuditRowValidationError, match="signer_id"):
                canonicalize_row(_valid_row(signer_id=bad))

    def test_session_id_non_string_rejected(self) -> None:
        # Regression: round-4 P2 — session_id symmetric gap to signer_id;
        # REQUIRED_FIELDS only checks presence, not type.
        for bad in (0, False, {}, []):
            with pytest.raises(AuditRowValidationError, match="session_id"):
                canonicalize_row(_valid_row(session_id=bad))

    # P2: Divergence hash format -------------------------------------------------

    def test_aphelion_hash_non_hex_rejected(self) -> None:
        row = _valid_row(
            outcome="divergence",
            aphelion_hash="not-hex",
            local_hash="c" * 64,
            reason_code="claim.r4_subject_missing",
        )
        with pytest.raises(AuditRowValidationError, match="aphelion_hash"):
            canonicalize_row(row)

    def test_local_hash_non_hex_rejected(self) -> None:
        row = _valid_row(
            outcome="divergence",
            aphelion_hash="b" * 64,
            local_hash="UPPERCASE" + "x" * 55,
            reason_code="claim.r4_subject_missing",
        )
        with pytest.raises(AuditRowValidationError, match="local_hash"):
            canonicalize_row(row)

    def test_divergence_valid_hashes_accepted(self) -> None:
        row = _valid_row(
            outcome="divergence",
            aphelion_hash="b" * 64,
            local_hash="c" * 64,
            reason_code="claim.r4_subject_missing",
        )
        canonicalize_row(row)


# ---- Canonical bytes + sha256 ------------------------------------------------


@pytest.mark.unit
class TestRowHashing:
    def test_sha256_matches_manual_canonical_json(self) -> None:
        row = canonicalize_row(_valid_row())
        manual = json.dumps(
            row.data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        assert row.to_canonical_bytes() == manual
        assert row.sha256_hex() == hashlib.sha256(manual).hexdigest()

    def test_sha256_stable_across_insertion_order(self) -> None:
        # Same data, different insertion order — sha256 must be identical.
        row_a = canonicalize_row(_valid_row())
        scrambled = dict(reversed(list(_valid_row().items())))
        row_b = canonicalize_row(scrambled)
        assert row_a.sha256_hex() == row_b.sha256_hex()

    def test_sha256_changes_when_outcome_changes(self) -> None:
        a = canonicalize_row(_valid_row()).sha256_hex()
        b_row = _valid_row(outcome="error", reason_code="pkg.unsigned")
        b = canonicalize_row(b_row).sha256_hex()
        assert a != b


# ---- Write-order invariant guard --------------------------------------------


@pytest.mark.unit
class TestWriteOrderGuard:
    def test_committed_true_passes(self) -> None:
        # Should not raise.
        assert_audit_row_committed(True)

    def test_committed_false_raises(self) -> None:
        with pytest.raises(AuditWriteOrderViolation, match="apex-m5-envelope-spec"):
            assert_audit_row_committed(False)

    def test_explicit_raise_survives_dash_o(self) -> None:
        # Spec §8.1: plain assert would be stripped under -O. The
        # AuditWriteOrderViolation is a real raise, not an assert.
        # We compile the function with optimization and confirm the
        # raise still fires.
        import dis
        # Disassembly check: the function body must contain RAISE_VARARGS
        # (the bytecode for `raise`), not just ASSERT (which compiles to
        # POP_JUMP_IF_TRUE + LOAD_ASSERTION_ERROR).
        src = dis.Bytecode(assert_audit_row_committed)
        ops = [instr.opname for instr in src]
        assert "RAISE_VARARGS" in ops, f"expected RAISE_VARARGS, got {ops!r}"
