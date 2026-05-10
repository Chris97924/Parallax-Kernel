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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from parallax.apex.canonical_json import canonical_dumps, sha256_hex

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


def _validate_optional_pairing(row: Mapping[str, Any]) -> None:
    """Optional fields appear only on relevant outcomes (spec §6.1).

    ``aphelion_hash`` + ``local_hash`` only on ``divergence``.
    ``reason_code`` only on ``error`` or ``divergence``.
    """
    outcome = row.get("outcome")
    has_diff_hashes = "aphelion_hash" in row or "local_hash" in row
    if has_diff_hashes and outcome != "divergence":
        raise AuditRowValidationError(
            "aphelion_hash/local_hash present without outcome='divergence'"
        )
    if "reason_code" in row and outcome not in ("error", "divergence"):
        raise AuditRowValidationError(
            "reason_code present without outcome in {error, divergence}"
        )


def canonicalize_row(row: Mapping[str, Any]) -> AuditRow:
    """Validate ``row`` against the audit-row schema and wrap as :class:`AuditRow`.

    The row is **not** mutated. Optional fields with value ``None`` are
    treated as absent (spec §6.1: empty string vs absent — empty string
    is valid for required fields, ``None`` is interpreted as "omit").
    """
    cleaned = {k: v for k, v in row.items() if v is not None}
    _validate_required(cleaned)
    _validate_outcome(cleaned)
    _validate_source(cleaned)
    _validate_reason_code(cleaned)
    _validate_optional_pairing(cleaned)

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

    # Reject unknown top-level keys to keep the sha256 stable across
    # implementations. Future fields require an envelope schema_version
    # bump per spec §6.2.
    allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    extra = set(cleaned.keys()) - allowed
    if extra:
        raise AuditRowValidationError(
            f"unknown audit-row field {sorted(extra)[0]!r}; "
            "new fields require envelope schema_version bump"
        )

    return AuditRow(data=cleaned)


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
