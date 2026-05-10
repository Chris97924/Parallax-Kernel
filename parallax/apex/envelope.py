"""Apex M5 envelope MVP — header + payload + checksum validation.

Implements the wire-format spec at ``docs/m5-prep/apex-m5-envelope-spec.md``
v0.1-frozen. The envelope is a Parallax-Kernel responsibility (Option α
reconcile, 2026-05-09) — no Aphelion-side coupling.

Public surface:
  * :class:`Envelope` — frozen dataclass DTO of the validated envelope
  * :class:`EnvelopeValidationError` — structural / required-field issues
  * :class:`EnvelopeChecksumError` — checksum present but recompute mismatch
  * :func:`parse_envelope` — strict JSON parse + structural validation
  * :func:`compute_checksum` — sha256 hex over canonical payload bytes
  * :func:`canonical_payload_bytes` — re-export of canonical serialization
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from parallax.apex.canonical_json import canonical_dumps, sha256_hex

ENVELOPE_VERSION_LITERAL: Final = "0.1"

REQUIRED_FIELDS: Final = frozenset(
    {
        "envelope_version",
        "schema_version",
        "message_id",
        "created_at",
        "source",
        "audit_db_ref",
        "payload_type",
        "payload",
        "checksum",
    }
)

# 20-char ISO 8601 UTC ``Z`` form (matches Aphelion v0.3 R2 + audit-row.ts).
_ISO_8601_UTC_Z_RE: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)
_SHA256_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class Source(StrEnum):
    """Spec §2: envelope ``source`` enum (closed)."""

    APHELION = "aphelion"
    PARALLAX = "parallax"


class PayloadType(StrEnum):
    """Spec §3.1: ``payload_type`` enum (closed)."""

    QUERY_RESULT = "query_result"
    EVENT = "event"


class EnvelopeValidationError(ValueError):
    """Structural validation failure (missing field, unknown enum, etc.)."""


class EnvelopeChecksumError(ValueError):
    """``checksum`` field present but does not match recomputed sha256.

    Spec §4.2: this is reserved for the recompute-mismatch case;
    absent ``checksum`` is :class:`EnvelopeValidationError`.
    """


@dataclass(frozen=True)
class Envelope:
    """Validated envelope DTO.

    All fields mirror the spec §2 + §3 schema. Construct via
    :func:`parse_envelope` rather than the dataclass directly so
    structural validation is enforced.
    """

    envelope_version: str
    schema_version: int
    message_id: str
    created_at: str
    source: Source
    audit_db_ref: str
    payload_type: PayloadType
    payload: Mapping[str, Any]
    checksum: str


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Re-export — useful at call sites that do not import canonical_json."""
    return canonical_dumps(payload)


def compute_checksum(payload: Mapping[str, Any]) -> str:
    """SHA-256 hex of the canonical payload bytes (spec §4.1)."""
    return sha256_hex(canonical_payload_bytes(payload))


def _validate_uuid_v4(value: str, *, field: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise EnvelopeValidationError(
            f"{field!r} is not a valid UUID: {value!r}"
        ) from exc
    if parsed.version != 4:
        raise EnvelopeValidationError(
            f"{field!r} must be UUIDv4, got version {parsed.version}"
        )


def parse_envelope(raw: Mapping[str, Any]) -> Envelope:
    """Validate ``raw`` against the v0.1 envelope schema.

    Steps (per spec §2 + §4):

      1. All 9 required fields present + no unknown top-level keys.
      2. ``envelope_version`` exactly ``"0.1"``.
      3. ``schema_version`` is an integer ≥ 1.
      4. ``message_id`` parses as a UUIDv4.
      5. ``created_at`` is the strict 20-char ISO 8601 UTC ``Z`` form.
      6. ``source`` and ``payload_type`` are members of their enums.
      7. ``audit_db_ref`` is exactly 64 lowercase hex chars (sha256).
      8. ``payload`` is a mapping (downstream consumers parse internals).
      9. ``checksum`` is exactly 64 lowercase hex chars AND matches
         the recomputed digest of the canonical payload bytes.

    Returns:
        :class:`Envelope`.

    Raises:
        EnvelopeValidationError: on any structural problem (rules 1-8 +
            checksum-shape).
        EnvelopeChecksumError: on rule 9 mismatch.
    """
    if not isinstance(raw, Mapping):
        raise EnvelopeValidationError(
            f"envelope must be a mapping, got {type(raw).__name__}"
        )

    keys = set(raw.keys())
    missing = REQUIRED_FIELDS - keys
    if missing:
        # Spec §2: surface a single missing-field error to keep the
        # ``AphelionUnreachableError(reason="envelope_checksum_mismatch")``
        # downstream classification simple. The first missing field in
        # canonical order is reported for caller diagnostics.
        first = sorted(missing)[0]
        raise EnvelopeValidationError(f"missing required field {first!r}")
    unknown = keys - REQUIRED_FIELDS
    if unknown:
        raise EnvelopeValidationError(
            f"unknown field {sorted(unknown)[0]!r}"
        )

    envelope_version = raw["envelope_version"]
    if envelope_version != ENVELOPE_VERSION_LITERAL:
        raise EnvelopeValidationError(
            f"unsupported envelope_version {envelope_version!r}"
        )

    schema_version = raw["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise EnvelopeValidationError(
            f"schema_version must be int, got {type(schema_version).__name__}"
        )
    if schema_version < 1:
        raise EnvelopeValidationError(
            f"schema_version must be >= 1, got {schema_version}"
        )

    message_id = raw["message_id"]
    if not isinstance(message_id, str):
        raise EnvelopeValidationError("message_id must be a string")
    _validate_uuid_v4(message_id, field="message_id")

    created_at = raw["created_at"]
    if not isinstance(created_at, str) or not _ISO_8601_UTC_Z_RE.fullmatch(created_at):
        raise EnvelopeValidationError(
            f"created_at must be 20-char ISO 8601 UTC Z, got {created_at!r}"
        )
    try:
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise EnvelopeValidationError(
            f"created_at is not a valid calendar timestamp, got {created_at!r}: {exc}"
        ) from exc

    source_raw = raw["source"]
    try:
        source = Source(source_raw)
    except ValueError as exc:
        raise EnvelopeValidationError(
            f"invalid source value {source_raw!r}"
        ) from exc

    audit_db_ref = raw["audit_db_ref"]
    if not isinstance(audit_db_ref, str) or not _SHA256_HEX_RE.fullmatch(audit_db_ref):
        raise EnvelopeValidationError(
            f"audit_db_ref must be 64-char lowercase sha256 hex, got {audit_db_ref!r}"
        )

    payload_type_raw = raw["payload_type"]
    try:
        payload_type = PayloadType(payload_type_raw)
    except ValueError as exc:
        raise EnvelopeValidationError(
            f"unknown payload_type {payload_type_raw!r}"
        ) from exc

    payload = raw["payload"]
    if not isinstance(payload, Mapping):
        raise EnvelopeValidationError(
            f"payload must be an object, got {type(payload).__name__}"
        )

    checksum = raw["checksum"]
    if not isinstance(checksum, str) or not _SHA256_HEX_RE.fullmatch(checksum):
        raise EnvelopeValidationError(
            f"checksum must be 64-char lowercase sha256 hex, got {checksum!r}"
        )

    try:
        expected = compute_checksum(payload)
    except (ValueError, TypeError) as exc:
        raise EnvelopeValidationError(
            f"payload is not canonically serializable: {exc}"
        ) from exc
    if expected != checksum:
        raise EnvelopeChecksumError(
            f"checksum mismatch: header={checksum} computed={expected}"
        )

    # Freeze the top-level payload mapping so callers cannot mutate
    # env.payload post-validation while env.checksum still reflects the
    # old bytes. dict(payload) breaks aliasing with the caller's input;
    # MappingProxyType blocks __setitem__/__delitem__ on env.payload.
    return Envelope(
        envelope_version=envelope_version,
        schema_version=schema_version,
        message_id=message_id,
        created_at=created_at,
        source=source,
        audit_db_ref=audit_db_ref,
        payload_type=payload_type,
        payload=MappingProxyType(dict(payload)),
        checksum=checksum,
    )
