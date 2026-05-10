"""Apex M5 PR-D — wired AphelionReadAdapter (R4 detection + envelope emission).

Supersedes the M3-T1.2 stub. The adapter now:
  1. Loads candidate claim frontmatters via an injectable ``claim_loader``
     (M6/M7 ingest pipeline will provide the real loader; PR-D defaults to
     an empty loader so the adapter returns a NOT_FOUND envelope until the
     ingest path lands).
  2. Validates each claim against ``aphelion.v03_validator.validate_v03_fields``.
  3. Runs Aphelion v0.3 R4 detection via ``aphelion.read_adapter.AphelionReadAdapter``.
  4. Emits an Apex M5 envelope (``payload_type="query_result"``) with a
     sha256 ``audit_db_ref`` derived from a canonical audit row.

Envelopes and audit rows are exposed on the adapter instance as
``last_envelope`` / ``last_audit_row`` for downstream wiring and tests;
the ``RetrievalEvidence`` return shape is unchanged so existing
``QueryPort`` consumers (DualReadRouter, etc.) keep working.

Spec anchors:
  * ``docs/m5-prep/apex-m5-entry-spec.md`` v0.3.0-reframe — entry conditions / scope
  * ``docs/m5-prep/apex-m5-envelope-spec.md`` §2 + §3.1 + §4.1 — envelope contract
  * ``docs/m5-prep/audit-db-path-config.md`` §6 — audit row schema + sha256
  * ``Aphelion-Graph/spec/v0.3-claim-semantics.md`` §6 — R4 detection contract
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aphelion.errors import SchemaError
from aphelion.read_adapter import AphelionReadAdapter as _V03Reader
from aphelion.read_adapter import ConflictClass
from aphelion.v03_validator import validate_v03_fields

from parallax.apex.audit_writer import AuditRow, canonicalize_row
from parallax.apex.envelope import (
    Envelope,
    PayloadType,
    Source,
    compute_checksum,
    parse_envelope,
)
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest

__all__ = ["AphelionReadAdapter", "AphelionUnreachableError"]


ENVELOPE_VERSION_LITERAL = "0.1"
ENVELOPE_SCHEMA_VERSION = 1

ClaimLoader = Callable[[QueryRequest], Iterable[Mapping[str, Any]]]


class AphelionUnreachableError(Exception):
    """Raised when the Aphelion secondary cannot be reached or an emit fails.

    ``reason`` is a short tag used for outcome classification in DualReadRouter:
      - ``"timeout"`` — secondary exceeded secondary_timeout_ms
      - ``"connection_error"`` — network/transport failure (reserved for future)
      - ``"claim_schema_error"`` — v0.3 validator rejected a candidate frontmatter
      - ``"envelope_checksum_mismatch"`` — envelope round-trip failed
      - ``"audit_row_invalid"`` — canonicalize_row rejected the row
      - ``"unsafe_archive"`` — Aphelion v0.2 untar safety violation (M6+ scope)
      - ``"unsigned_package"`` — Aphelion v0.5 signature missing (M6+ scope)

    M3a "not_implemented" is no longer emitted now that the adapter is wired
    (PR-D); call sites switching from M3a should accept the wider reason set.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"Aphelion unreachable: {reason}")
        self.reason = reason


def _empty_loader(_request: QueryRequest) -> tuple[Mapping[str, Any], ...]:
    """Default claim loader — returns empty until M6/M7 ingest pipeline ships."""
    return ()


def _resolve_subject(request: QueryRequest) -> str:
    """Map a Parallax ``QueryRequest`` to the Aphelion R4 ``subject``.

    Per spec §6 R4 step 0, ``subject`` is required. PR-D treats:
      * ``request.params["subject"]`` if present (test/explicit path)
      * else ``request.q`` (user query string) as the subject
      * else ``request.user_id`` as a last-resort scope tag
    """
    if request.params:
        explicit = request.params.get("subject")
        if isinstance(explicit, str) and explicit:
            return explicit
    if request.q:
        return request.q
    return request.user_id


def _utc_now_iso_z() -> str:
    """20-char ISO 8601 UTC ``Z`` timestamp matching envelope-spec §2."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conflict_class_to_outcome(cc: ConflictClass) -> str:
    """Map R4 ``ConflictClass`` to audit-row ``outcome`` enum.

    Per ``audit-db-path-config.md`` §6.1 OUTCOME_VALUES = hit / miss /
    divergence / error. PR-D is single-source so only hit / miss are emitted.
    """
    if cc is ConflictClass.NOT_FOUND:
        return "miss"
    return "hit"


def _string_field(claim: Mapping[str, Any], name: str) -> str | None:
    """Return ``claim[name]`` only if it's a non-empty string, else None."""
    value = claim.get(name)
    if isinstance(value, str) and value:
        return value
    return None


class AphelionReadAdapter:
    """Wired QueryPort adapter for Aphelion secondary reads (PR-D / M5 entry).

    Instantiation is cheap; the adapter is stateless modulo ``last_envelope``
    / ``last_audit_row`` which are overwritten on every ``query()`` call.

    Args:
        package_dir: Aphelion package store path (``PARALLAX_APHELION_PACKAGE_DIR``).
            Stored for M6/M7 ingest hookup; PR-D does not walk it.
        timeout_ms: Reserved for the M3 dual-read shadow timeout contract.
        claim_loader: Callable that maps a ``QueryRequest`` to an iterable of
            v0.3 claim frontmatter mappings. Defaults to an empty loader so
            production traffic continues to return NOT_FOUND envelopes until
            the ingest pipeline lands.
    """

    def __init__(
        self,
        *,
        package_dir: Path | None = None,
        timeout_ms: float = 100.0,
        claim_loader: ClaimLoader | None = None,
    ) -> None:
        self._package_dir = package_dir
        self._timeout_ms = timeout_ms
        self._claim_loader: ClaimLoader = claim_loader or _empty_loader
        self._reader = _V03Reader()
        self.last_envelope: Envelope | None = None
        self.last_audit_row: AuditRow | None = None

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        """Run R4 detection + emit Apex M5 envelope; return ``RetrievalEvidence``.

        Raises:
            AphelionUnreachableError: any failure surfaces as the M3 contract
                error so DualReadRouter can classify outcome consistently.
        """
        subject = _resolve_subject(request)
        candidates = tuple(self._claim_loader(request))

        for claim in candidates:
            try:
                validate_v03_fields(claim)
            except SchemaError as exc:
                raise AphelionUnreachableError("claim_schema_error") from exc

        try:
            result = self._reader.query(
                subject=subject,
                candidate_claims=candidates,
                query_time=None,
            )
        except SchemaError as exc:
            raise AphelionUnreachableError("claim_schema_error") from exc

        outcome = _conflict_class_to_outcome(result.conflict_class)
        primary_claim_id = (
            _string_field(result.primary, "claim_id")
            if result.primary is not None
            else None
        )
        primary_package_id = (
            _string_field(result.primary, "package_id")
            if result.primary is not None
            else None
        )

        hits = tuple(
            {
                "id": str(claim.get("claim_id", "")),
                "text": str(claim.get("subject", "")),
                "kind": "aphelion_claim",
                "polarity": str(claim.get("polarity", "affirm")),
            }
            for claim in result.surfaced
        )

        evidence = RetrievalEvidence(
            hits=hits,
            stages=("aphelion_v03_r4",),
            notes=(f"conflict_class={result.conflict_class.value}",),
        )

        if primary_claim_id is None or primary_package_id is None:
            self.last_envelope = None
            self.last_audit_row = None
            return evidence

        envelope_message_id = str(uuid.uuid4())
        audit_row_data: dict[str, Any] = {
            "claim_id": primary_claim_id,
            "package_id": primary_package_id,
            "envelope_message_id": envelope_message_id,
            "ts": result.used_query_time,
            "outcome": outcome,
            "source": "aphelion",
            # session_id is required-non-empty per audit_writer canonicalization
            # (see ``parallax/apex/audit_writer.py`` empty-string check). PR-D
            # has no per-session correlation surface yet, so we anchor on
            # ``request.user_id`` until the upstream router supplies a real
            # session id.
            "session_id": request.user_id,
            "signer_id": "",
            "signer_manifest_digest": "",
        }

        try:
            audit_row = canonicalize_row(audit_row_data)
        except Exception as exc:
            raise AphelionUnreachableError("audit_row_invalid") from exc

        payload: dict[str, Any] = {
            "subject": subject,
            "conflict_class": result.conflict_class.value,
            "primary_claim_id": primary_claim_id,
            "surfaced_count": len(result.surfaced),
            "superseded_count": len(result.superseded),
            "used_query_time": result.used_query_time,
        }

        envelope_dict: dict[str, Any] = {
            "envelope_version": ENVELOPE_VERSION_LITERAL,
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "message_id": envelope_message_id,
            "created_at": _utc_now_iso_z(),
            "source": Source.APHELION.value,
            "audit_db_ref": audit_row.sha256_hex(),
            "payload_type": PayloadType.QUERY_RESULT.value,
            "payload": payload,
            "checksum": compute_checksum(payload),
        }

        try:
            envelope = parse_envelope(envelope_dict)
        except Exception as exc:
            raise AphelionUnreachableError("envelope_checksum_mismatch") from exc

        self.last_envelope = envelope
        self.last_audit_row = audit_row

        return evidence
