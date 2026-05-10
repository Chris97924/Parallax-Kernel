"""Apex M5 envelope + audit-writer module (Parallax-Kernel responsibility).

The Apex envelope is a wire format between the dual-read router /
audit ledger and the broader Parallax-Kernel. It is **not** an Aphelion
format — Aphelion v0.5 signer covers ``.aphelion`` package-level claim
attestation, not these envelopes (Option α reconcile, 2026-05-09).

Spec ground truth: ``docs/m5-prep/apex-m5-envelope-spec.md`` v0.1-frozen.
"""

from __future__ import annotations

from parallax.apex.envelope import (  # noqa: F401
    Envelope,
    EnvelopeChecksumError,
    EnvelopeValidationError,
    PayloadType,
    Source,
    canonical_payload_bytes,
    compute_checksum,
    parse_envelope,
)

__all__ = [
    "Envelope",
    "EnvelopeChecksumError",
    "EnvelopeValidationError",
    "PayloadType",
    "Source",
    "canonical_payload_bytes",
    "compute_checksum",
    "parse_envelope",
]

ENVELOPE_VERSION = "0.1"
