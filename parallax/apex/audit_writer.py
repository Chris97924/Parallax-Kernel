"""Apex M5 audit-row writer + write-order invariant guard.

Implements the audit row schema and SHA-256 calculation pinned in
``docs/m5-prep/audit-db-path-config.md`` §6, plus the write-order
invariant from ``docs/m5-prep/apex-m5-envelope-spec.md`` §8.1.

Key contract (spec §8.1):

  1. caller constructs canonical audit row dict
  2. caller writes row → audit.db (BEGIN; INSERT; COMMIT)
  3. caller computes sha256(canonical_json(row))
  4. caller assembles envelope with audit_db_ref = sha256_hex
  5. caller emits envelope

Step 2 → 5 ordering MUST be enforced by an explicit ``raise``, NOT a
plain ``assert`` (asserts are stripped under ``python -O``). The
:class:`AuditWriteOrderViolation` raise survives optimization.
"""

from __future__ import annotations

import re
import uuid as _uuid_mod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from parallax.apex.canonical_json import canonical_dumps, sha256_hex

# Compiled once — SHA-256 hex: exactly 64 lowercase hex chars.
_SHA256_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")

# Exact format: YYYY-MM-DDTHH:MM:SSZ (20 chars, Z suffix, second precision).
_TS_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"
_TS_LENGTH: Final = 20


def _validate_sha256_hex(value: object, field_name: str) -> None:
    """Raise AuditRowValidationError if *value* is not a 64-char lowercase SHA-256 hex."""
    if not isinstance(value, str):
        raise AuditRowValidationError(
            f"{field_name!r} must be a string, got {type(value).__name__}"
        )
    if not _SHA256_HEX_RE.match(value):
        raise AuditRowValidationError(
            f"{field_name!r} must be a 64-char lowercase SHA-256 hex string, got {value!r}"
        )

REQUIRED_FIELDS: Final = (
    "claim_id",
    "envelope_message_id",
    "outcome",
    "package_id",
    "session_id",
    "signer_id",
    "signer_manifest_digest",
    "source",
    "ts",
)

OPTIONAL_FIELDS: Final = (
    "aphelion_hash",
    "local_hash",
    "reason_code",
)

OUTCOME_VALUES: Final = frozenset({"hit", "miss", "divergence", "error"})
SOURCE_VALUES: Final = frozenset({"aphelion", "parallax"})

# ``reason_code`` namespace prefixes — open value set per spec §6.3.
REASON_CODE_PREFIXES: Final = (
    "pkg.",
    "signer.",
    "cache.",
    "network.",
    "disk.",
    "claim.",
)


class AuditWriteOrderViolation(RuntimeError):
    """Raised when envelope emission is attempted before audit row commit.

    Spec §8.1: plain ``assert`` is NOT acceptable as the guard because
    Python strips asserts under ``-O`` / ``PYTHONOPTIMIZE``. This raise
    survives optimization and prevents silent audit-chain corruption
    in production-optimized builds.
    """


class AuditRowValidationError(ValueError):
    """Audit row missing a required field, or has an out-of-domain value."""


@dataclass(frozen=True)
class AuditRow:
    """Validated audit row DTO.

    Construct via :func:`canonicalize_row` rather than directly so the
    schema constraint is enforced before the row is hashed for
    inclusion in :data:`Envelope.audit_db_ref`.

    The row is intentionally immutable — any mutation after sha256
    computation would silently invalidate the envelope's
    ``audit_db_ref``.
    """

    data: Mapping[str, Any]

    def to_canonical_bytes(self) -> bytes:
        """Canonical UTF-8 JSON bytes (the input to sha256)."""
        return canonical_dumps(dict(self.data))

    def sha256_hex(self) -> str:
        """Hex digest used as ``envelope.audit_db_ref``."""
        return sha256_hex(self.to_canonical_bytes())


def _validate_required(row: Mapping[str, Any]) -> None:
    for name in REQUIRED_FIELDS:
        if name not in row:
            raise AuditRowValidationError(f"audit row missing required field {name!r}")


def _validate_outcome(row: Mapping[str, Any]) -> None:
    outcome = row.get("outcome")
    if outcome not in OUTCOME_VALUES:
        raise AuditRowValidationError(
            f"outcome must be one of {sorted(OUTCOME_VALUES)}, got {outcome!r}"
        )


def _validate_source(row: Mapping[str, Any]) -> None:
    source = row.get("source")
    if source not in SOURCE_VALUES:
        raise AuditRowValidationError(
            f"source must be one of {sorted(SOURCE_VALUES)}, got {source!r}"
        )


def _validate_reason_code(row: Mapping[str, Any]) -> None:
    rc = row.get("reason_code")
    if rc is None:
        return
    if not isinstance(rc, str):
        raise AuditRowValidationError(
            f"reason_code must be string, got {type(rc).__name__}"
        )
    if not any(rc.startswith(p) for p in REASON_CODE_PREFIXES):
        raise AuditRowValidationError(
            f"reason_code {rc!r} does not start with a reserved namespace; "
            f"expected one of {REASON_CODE_PREFIXES}"
        )


def _validate_field_formats(row: Mapping[str, Any]) -> None:
    """Validate field formats for fields with structural constraints (spec §6.1)."""
    # session_id: required string, may be empty (no UUID/hex constraint per spec §6.1).
    session_id = row.get("session_id", "")
    if not isinstance(session_id, str):
        raise AuditRowValidationError(
            f"'session_id' must be a string, got {type(session_id).__name__}"
        )

    # signer_id: required string, may be empty (no UUID/hex constraint per spec §6.1).
    signer_id = row.get("signer_id", "")
    if not isinstance(signer_id, str):
        raise AuditRowValidationError(
            f"'signer_id' must be a string, got {type(signer_id).__name__}"
        )

    # claim_id: UUID v7
    claim_id = row.get("claim_id", "")
    if not isinstance(claim_id, str):
        raise AuditRowValidationError(
            f"'claim_id' must be a string, got {type(claim_id).__name__}"
        )
    if claim_id:
        try:
            parsed = _uuid_mod.UUID(claim_id)
        except ValueError:
            raise AuditRowValidationError(
                f"'claim_id' must be a valid UUID, got {claim_id!r}"
            )
        if parsed.version != 7:
            raise AuditRowValidationError(
                f"'claim_id' must be UUID v7, got version {parsed.version}"
            )

    # package_id: UUID v7
    package_id = row.get("package_id", "")
    if not isinstance(package_id, str):
        raise AuditRowValidationError(
            f"'package_id' must be a string, got {type(package_id).__name__}"
        )
    if package_id:
        try:
            parsed = _uuid_mod.UUID(package_id)
        except ValueError:
            raise AuditRowValidationError(
                f"'package_id' must be a valid UUID, got {package_id!r}"
            )
        if parsed.version != 7:
            raise AuditRowValidationError(
                f"'package_id' must be UUID v7, got version {parsed.version}"
            )

    # envelope_message_id: UUID v4
    emid = row.get("envelope_message_id", "")
    if not isinstance(emid, str):
        raise AuditRowValidationError(
            f"'envelope_message_id' must be a string, got {type(emid).__name__}"
        )
    if emid:
        try:
            parsed = _uuid_mod.UUID(emid)
        except ValueError:
            raise AuditRowValidationError(
                f"'envelope_message_id' must be a valid UUID, got {emid!r}"
            )
        if parsed.version != 4:
            raise AuditRowValidationError(
                f"'envelope_message_id' must be UUID v4, got version {parsed.version}"
            )

    # ts: exactly 20 chars, Z suffix, second precision ISO 8601 UTC
    ts = row.get("ts", "")
    if not isinstance(ts, str):
        raise AuditRowValidationError(
            f"'ts' must be a string, got {type(ts).__name__}"
        )
    if ts:
        if len(ts) != _TS_LENGTH or not ts.endswith("Z"):
            raise AuditRowValidationError(
                f"'ts' must be ISO 8601 UTC with Z suffix and second precision "
                f"(20 chars, e.g. '2026-05-09T14:23:11Z'), got {ts!r}"
            )
        try:
            datetime.strptime(ts, _TS_FORMAT)
        except ValueError:
            raise AuditRowValidationError(
                f"'ts' is not a valid ISO 8601 UTC timestamp, got {ts!r}"
            )

    # signer_manifest_digest: sha256 hex OR empty string (unsigned packages)
    smd = row.get("signer_manifest_digest", "")
    if not isinstance(smd, str):
        raise AuditRowValidationError(
            f"'signer_manifest_digest' must be a string, got {type(smd).__name__}"
        )
    if smd:  # non-empty: must be valid sha256 hex
        _validate_sha256_hex(smd, "signer_manifest_digest")


def _validate_divergence_hashes(row: Mapping[str, Any]) -> None:
    """Validate aphelion_hash and local_hash are SHA-256 hex when present (P2)."""
    for field in ("aphelion_hash", "local_hash"):
        value = row.get(field)
        if value is not None:
            _validate_sha256_hex(value, field)


def _validate_optional_pairing(row: Mapping[str, Any]) -> None:
    """Optional fields appear only on relevant outcomes (spec §6.1).

    ``aphelion_hash`` + ``local_hash`` only on ``divergence``.
    ``reason_code`` only on ``error`` or ``divergence``.
    Both checks are bidirectional: present-without-correct-outcome is rejected
    AND correct-outcome-without-required-field is rejected.
    """
    outcome = row.get("outcome")
    has_diff_hashes = "aphelion_hash" in row or "local_hash" in row
    if has_diff_hashes and outcome != "divergence":
        raise AuditRowValidationError(
            "aphelion_hash/local_hash present without outcome='divergence'"
        )
    if outcome == "divergence":
        if "aphelion_hash" not in row or "local_hash" not in row:
            raise AuditRowValidationError(
                "outcome='divergence' requires both aphelion_hash and local_hash"
            )
    if "reason_code" in row and outcome not in ("error", "divergence"):
        raise AuditRowValidationError(
            "reason_code present without outcome in {error, divergence}"
        )
    if outcome in ("error", "divergence") and "reason_code" not in row:
        raise AuditRowValidationError(
            f"outcome={outcome!r} requires reason_code to be present"
        )


def canonicalize_row(row: Mapping[str, Any]) -> AuditRow:
    """Validate ``row`` against the audit-row schema and wrap as :class:`AuditRow`.

    The row is **not** mutated. Optional fields with value ``None`` are
    treated as absent (spec §6.1: empty string vs absent — empty string
    is valid for required fields, ``None`` is interpreted as "omit").
    """
    # Reject unknown top-level keys against the RAW input so that a
    # typo'd field set to None cannot bypass strict-schema enforcement
    # by being filtered out below. Future fields require an envelope
    # schema_version bump per spec §6.2.
    allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    extra = set(row.keys()) - allowed
    if extra:
        raise AuditRowValidationError(
            f"unknown audit-row field {sorted(extra)[0]!r}; "
            "new fields require envelope schema_version bump"
        )

    cleaned = {k: v for k, v in row.items() if v is not None}
    _validate_required(cleaned)
    _validate_outcome(cleaned)
    _validate_source(cleaned)
    _validate_reason_code(cleaned)
    _validate_field_formats(cleaned)
    _validate_optional_pairing(cleaned)
    _validate_divergence_hashes(cleaned)

    # Empty-string-valid required fields: signer_id + signer_manifest_digest
    # for unsigned packages. All other required fields must be non-empty
    # strings (or ints for fields that are explicitly numeric — none yet).
    for name in REQUIRED_FIELDS:
        value = cleaned[name]
        if isinstance(value, str) and value == "" and name not in (
            "signer_id",
            "signer_manifest_digest",
        ):
            raise AuditRowValidationError(
                f"required field {name!r} must not be empty string"
            )

    # Freeze the backing mapping so callers cannot mutate row.data
    # post-validation and silently invalidate sha256_hex().
    return AuditRow(data=MappingProxyType(cleaned))


def assert_audit_row_committed(committed: bool) -> None:
    """Spec §8.1 explicit raise — survives ``python -O``.

    Callers MUST invoke this guard immediately before envelope
    assembly. If ``committed`` is ``False``, raises
    :class:`AuditWriteOrderViolation`.
    """
    if not committed:
        raise AuditWriteOrderViolation(
            "envelope emit attempted before audit row commit; "
            "see apex-m5-envelope-spec.md §8.1"
        )
