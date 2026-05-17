"""Apex M6 ingest pipeline — `.aphelion.tar` → audit-row mapping.

Implements ``docs/m6-prep/m6-ingest-contract-spec.md`` v0.1-frozen-2026-05-17
(US-201 + US-202 combined). Single-package, manual-CLI ingest:

  1. §5.2 + §3.3a path / env / trust-store gates.
  2. Aphelion ``verify_package(require_signed=True, require_notary=False)``
     end-to-end verification (unpack safety, semantic, hash, signature).
  3. M6 trust-store enforcement on top of Aphelion's cryptographic
     verification (§3.3): resolve each envelope's ``SignerManifest`` and
     reject signers whose ``key_fingerprint`` is not in the operator's
     ``PARALLAX_APHELION_TRUST_STORE`` directory.
  4. Per-claim mapping to ``audit_row`` records using the M5
     ``audit_writer`` contract (§4.4 + §4.7 write-order invariant).

NO env vars are read at module import time — all configuration is taken
as explicit arguments so the CLI layer (US-204) controls env resolution.

Exit-code / ``reason_code`` mapping per spec §6.2 is encoded on each
``ParallaxIngestError`` raise site; the CLI layer translates these to
process exit codes (65 / 70 / 71 / 78 per spec §6.3).
"""

from __future__ import annotations

import os
import sqlite3
import tarfile
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from aphelion.canonical_json import loads as canonical_loads
from aphelion.errors import SchemaError, SecurityError, SemanticError, VerificationError
from aphelion.signer import (
    SignerManifest,
    SignerVerificationError,
    compute_key_fingerprint,
)
from aphelion.unpacker import ExtractPolicy, extract_signer_manifests, unpack
from aphelion.v03_validator import validate_v03_fields
from aphelion.verifier import VerifyResult, verify_package
from aphelion.yaml_canonical import parse_frontmatter, split_frontmatter

from parallax.apex.audit_db import AuditDbWriteError, write_audit_row
from parallax.apex.audit_writer import (
    AuditRow,
    assert_audit_row_committed,
    canonicalize_row,
)
from parallax.obs.log import get_logger

__all__ = [
    "ClaimMappingBatch",
    "IngestReport",
    "ParallaxIngestError",
    "TrustDecision",
    "ingest_package",
]

_log = get_logger(__name__)

# Sysexits codes used by M6 reason_codes (spec §6.3).
_EX_DATAERR: Final = 65
_EX_SOFTWARE: Final = 70
_EX_OSERR: Final = 71
_EX_CONFIG: Final = 78

# Reason-code → exit-code lookup (spec §6.2 canonical table).
_REASON_TO_EXIT: Final[Mapping[str, int]] = {
    # pkg.*
    "pkg.not_found": _EX_DATAERR,
    "pkg.not_regular_file": _EX_DATAERR,
    "pkg.extension_invalid": _EX_DATAERR,
    "pkg.path_escape": _EX_DATAERR,
    "pkg.archive_unsafe": _EX_DATAERR,
    "pkg.semantic_invalid": _EX_DATAERR,
    "pkg.hash_mismatch": _EX_DATAERR,
    "pkg.unsigned": _EX_DATAERR,
    "pkg.empty_package": _EX_DATAERR,
    "pkg.trust_store_missing": _EX_CONFIG,
    "pkg.dir_unset": _EX_CONFIG,
    "pkg.idempotency_duplicate": _EX_SOFTWARE,
    # signer.*
    "signer.signature_invalid": _EX_DATAERR,
    "signer.untrusted": _EX_DATAERR,
    # NOTE: spec §3.4 also defines ``signer.fingerprint_mismatch`` to
    # distinguish "matching <signer_id>.pem present but fingerprint
    # differs" from the broader ``signer.untrusted`` (no matching .pem at
    # all). The M6 trust store implementation here is filename-irrelevant
    # — fingerprints are aggregated over every ``*.pem`` byte payload —
    # so the precondition for detecting the mismatch case (a .pem named
    # ``<signer_id>.pem``) is absent. ``signer.fingerprint_mismatch`` is
    # therefore intentionally NOT in this map; emitting it would be
    # dead-code reachable only via misuse. Documented as a follow-up spec
    # gap for round-2 (filename convention + mismatch detection).
    "signer.manifest_missing": _EX_DATAERR,
    "signer.multi_sig_unsupported": _EX_DATAERR,
    "signer.trust_pem_invalid": _EX_CONFIG,
    # claim.*
    "claim.format_invalid": _EX_DATAERR,
    "claim.duplicate_in_package": _EX_DATAERR,
    "claim.subject_required_for_r4": _EX_DATAERR,
    # disk.*
    "disk.permission": _EX_OSERR,
    "disk.audit_db_write_failed": _EX_OSERR,
    "disk.audit_db_unset": _EX_CONFIG,
}

_TS_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


class ParallaxIngestError(Exception):
    """Raised on any M6 ingest failure.

    The ``reason_code`` attribute is one of the namespaced strings from
    spec §6.2 (``pkg.*`` / ``signer.*`` / ``claim.*`` / ``disk.*``).
    ``exit_code`` is the sysexits value the CLI layer should exit with.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in _REASON_TO_EXIT:
            # Defence: unknown reason_code is a programmer error — raising
            # plain ValueError makes the misuse loud rather than silently
            # shipping an exit_code=0 ingest failure.
            raise ValueError(
                f"unknown reason_code {reason_code!r}; "
                f"must be one of {sorted(_REASON_TO_EXIT)}"
            )
        super().__init__(f"{reason_code}: {message}")
        self.reason_code: str = reason_code
        self.exit_code: int = _REASON_TO_EXIT[reason_code]


@dataclass(frozen=True)
class TrustDecision:
    """Per-envelope trust verdict from the operator-side trust store."""

    signer_id: str
    accepted: bool
    reason_code: str | None  # None when accepted

    @classmethod
    def accepted_for(cls, signer_id: str) -> TrustDecision:
        return cls(signer_id=signer_id, accepted=True, reason_code=None)

    @classmethod
    def rejected_for(cls, signer_id: str, reason_code: str) -> TrustDecision:
        return cls(signer_id=signer_id, accepted=False, reason_code=reason_code)


@dataclass(frozen=True)
class ClaimMappingBatch:
    """One audit-row batch per ingested package (spec §4.3)."""

    package_id: str
    signer_id: str
    signer_manifest_digest: str  # envelopes[0].package_canonical_hash
    audit_rows: tuple[AuditRow, ...]
    timestamp: str  # shared across batch; second-precision UTC Z


@dataclass(frozen=True)
class IngestReport:
    """Operator-facing success summary returned by :func:`ingest_package`."""

    package_id: str
    package_path: str
    claims_ingested: int
    audit_rows_written: int
    signer_id: str
    elapsed_ms: int


# ---------------------------------------------------------------------------
# §5.2 + §3.3a validation gates
# ---------------------------------------------------------------------------


def _validate_dir_arg(dir_path: Path, *, label: str, dir_unset_code: str) -> Path:
    """Gates 1-3 for a required-directory argument (spec §5.2 + §3.3a).

    ``label`` and ``dir_unset_code`` let one helper serve both
    ``PARALLAX_APHELION_PACKAGE_DIR`` and ``PARALLAX_APHELION_TRUST_STORE``.
    """
    if not isinstance(dir_path, Path) or str(dir_path) == "":
        raise ParallaxIngestError(
            dir_unset_code, f"{label} is unset or empty"
        )
    if not dir_path.is_absolute():
        raise ParallaxIngestError(
            dir_unset_code, f"{label} must be an absolute path, got {str(dir_path)!r}"
        )
    if any(part == ".." for part in dir_path.parts):
        raise ParallaxIngestError(
            dir_unset_code,
            f"{label} must not contain '..' segments, got {str(dir_path)!r}",
        )
    try:
        resolved = dir_path.resolve(strict=False)
    except OSError as exc:
        raise ParallaxIngestError(
            dir_unset_code, f"{label} path resolution failed: {exc}"
        ) from exc
    if not resolved.exists():
        raise ParallaxIngestError(
            dir_unset_code, f"{label} directory does not exist: {resolved}"
        )
    if not resolved.is_dir():
        raise ParallaxIngestError(
            dir_unset_code, f"{label} is not a directory: {resolved}"
        )
    if not os.access(resolved, os.R_OK):
        raise ParallaxIngestError(
            dir_unset_code, f"{label} directory not readable: {resolved}"
        )
    return resolved


def _validate_package_path(package_path: Path, package_dir: Path) -> Path:
    """Gates 4-6 — package path under package_dir, .aphelion.tar, regular file."""
    try:
        resolved_pkg = package_path.resolve(strict=False)
    except OSError as exc:
        raise ParallaxIngestError(
            "pkg.path_escape", f"package_path resolution failed: {exc}"
        ) from exc

    # Gate 4 — under package_dir (post-symlink resolution).
    try:
        resolved_pkg.relative_to(package_dir)
    except ValueError as exc:
        raise ParallaxIngestError(
            "pkg.path_escape",
            f"package_path {str(resolved_pkg)!r} resolves outside "
            f"PARALLAX_APHELION_PACKAGE_DIR {str(package_dir)!r}",
        ) from exc

    # Gate 5 — extension. Compare on the original argument name so a
    # symlink trick cannot bypass the suffix check.
    if not str(package_path).endswith(".aphelion.tar"):
        raise ParallaxIngestError(
            "pkg.extension_invalid",
            f"package_path must end in '.aphelion.tar', got {str(package_path)!r}",
        )

    # Gate 6 — regular file, not symlink / dir / device. Check both the
    # original (catches the symlink itself) and resolved form.
    if package_path.is_symlink():
        raise ParallaxIngestError(
            "pkg.not_regular_file",
            f"package_path is a symlink: {str(package_path)!r}",
        )
    if not resolved_pkg.exists():
        raise ParallaxIngestError(
            "pkg.not_found", f"package_path does not exist: {resolved_pkg}"
        )
    if not resolved_pkg.is_file():
        raise ParallaxIngestError(
            "pkg.not_regular_file",
            f"package_path is not a regular file: {resolved_pkg}",
        )
    return resolved_pkg


def _load_trust_store(trust_store_dir: Path) -> set[str]:
    """Gates 8-9 — read every ``.pem`` in trust_store_dir, return fingerprint set.

    Empty trust store returns an empty set — downstream
    :func:`_verify_trust` rejects every envelope with ``signer.untrusted``
    per spec §3.4.
    """
    fingerprints: set[str] = set()
    # iterdir() can raise OSError (PermissionError, BlockingIOError, FS
    # going read-only, etc.) before we even see the first .pem. Guard
    # explicitly so the caller sees ``disk.permission`` (exit 71) rather
    # than an unwrapped OSError leaking through the public API.
    try:
        pem_files = sorted(
            p for p in trust_store_dir.iterdir() if p.suffix == ".pem"
        )
    except OSError as exc:
        raise ParallaxIngestError(
            "disk.permission",
            f"trust store directory unreadable: {trust_store_dir}: {exc}",
        ) from exc
    for pem in pem_files:
        try:
            raw = pem.read_bytes()
        except PermissionError as exc:
            raise ParallaxIngestError(
                "disk.permission",
                f"trust store PEM unreadable: {pem}: {exc}",
            ) from exc
        except OSError as exc:
            raise ParallaxIngestError(
                "signer.trust_pem_invalid",
                f"trust store PEM read failed: {pem}: {exc}",
            ) from exc
        # M6 trust-store treats each .pem as the raw public-key bytes that
        # produced the signer's `key_fingerprint`. The hash is computed
        # over the file's exact byte content so the operator can drop in
        # whatever encoding their Aphelion signer used (HMAC b64 secret,
        # Ed25519 raw 32-byte, etc.) — `compute_key_fingerprint` matches
        # the same SHA256(public_key_bytes) the signer manifest stores.
        if not raw:
            raise ParallaxIngestError(
                "signer.trust_pem_invalid",
                f"trust store PEM is empty: {pem}",
            )
        fingerprints.add(compute_key_fingerprint(raw))
    return fingerprints


def _parse_signer_manifest(manifest_bytes: bytes) -> SignerManifest:
    """Parse a ``signers/<id>.json`` byte payload into ``SignerManifest``."""
    try:
        raw = canonical_loads(manifest_bytes)
    except SchemaError as exc:
        raise ParallaxIngestError(
            "signer.signature_invalid",
            f"signer manifest not canonical JSON: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise ParallaxIngestError(
            "signer.signature_invalid",
            f"signer manifest must be a JSON object, got {type(raw).__name__}",
        )
    required = {"signer_id", "algorithm", "public_key_b64", "key_fingerprint"}
    missing = required - raw.keys()
    if missing:
        raise ParallaxIngestError(
            "signer.signature_invalid",
            f"signer manifest missing required fields: {sorted(missing)}",
        )
    return SignerManifest(
        signer_id=raw["signer_id"],
        algorithm=raw["algorithm"],
        public_key_b64=raw["public_key_b64"],
        key_fingerprint=raw["key_fingerprint"],
        notary_uri=raw.get("notary_uri"),
    )


def _verify_trust(
    envelopes: tuple[Any, ...],
    tar_path: Path,
    trusted_fingerprints: set[str],
) -> tuple[TrustDecision, ...]:
    """Spec §3.3 algorithm — one TrustDecision per envelope.

    Multi-sig is rejected by the caller BEFORE this is invoked
    (spec §2.6 + §3.4 ``signer.multi_sig_unsupported``).
    """
    raw_manifests = extract_signer_manifests(tar_path)
    decisions: list[TrustDecision] = []
    for env in envelopes:
        manifest_bytes = raw_manifests.get(env.signer_id)
        if manifest_bytes is None:
            decisions.append(
                TrustDecision.rejected_for(env.signer_id, "signer.manifest_missing")
            )
            continue
        manifest = _parse_signer_manifest(manifest_bytes)
        if manifest.key_fingerprint not in trusted_fingerprints:
            decisions.append(
                TrustDecision.rejected_for(env.signer_id, "signer.untrusted")
            )
            continue
        decisions.append(TrustDecision.accepted_for(env.signer_id))
    return tuple(decisions)


# ---------------------------------------------------------------------------
# Aphelion verify_package + exception translation (spec §2.4)
# ---------------------------------------------------------------------------


def _run_aphelion_verify(tar_path: Path) -> VerifyResult:
    """Invoke ``verify_package(require_signed=True)`` and re-raise as
    ``ParallaxIngestError`` per spec §2.4 mapping table.
    """
    try:
        return verify_package(
            tar_path, require_signed=True, require_notary=False
        )
    except FileNotFoundError as exc:
        raise ParallaxIngestError(
            "pkg.not_found", f"package file missing: {exc}"
        ) from exc
    except PermissionError as exc:
        raise ParallaxIngestError(
            "disk.permission", f"OS permission denied: {exc}"
        ) from exc
    except SecurityError as exc:
        raise ParallaxIngestError(
            "pkg.archive_unsafe", f"unpack safety violated: {exc}"
        ) from exc
    except VerificationError as exc:
        raise ParallaxIngestError(
            "pkg.hash_mismatch", f"package hash mismatch: {exc}"
        ) from exc
    except SemanticError as exc:
        raise ParallaxIngestError(
            "pkg.semantic_invalid", f"package semantic invariant violated: {exc}"
        ) from exc
    except SignerVerificationError as exc:
        if getattr(exc, "code", None) == "E_SIGNER_REQUIRED":
            raise ParallaxIngestError(
                "pkg.unsigned", f"package is unsigned: {exc}"
            ) from exc
        raise ParallaxIngestError(
            "signer.signature_invalid", f"signature verification failed: {exc}"
        ) from exc
    except SchemaError as exc:
        # SchemaError can be raised by validate_signatures during the v0.4
        # frontmatter sweep. Tag the R4 subject case distinctly per §4.5.
        code_val = getattr(exc.code, "name", str(exc.code))
        if code_val == "CLAIM_SUBJECT_REQUIRED_FOR_CONFLICT":
            raise ParallaxIngestError(
                "claim.subject_required_for_r4",
                f"R4-trigger field without 'subject': {exc}",
            ) from exc
        raise ParallaxIngestError(
            "claim.format_invalid", f"claim schema violation: {exc}"
        ) from exc
    except OSError as exc:
        # PermissionError is already handled above. Any other OSError
        # (FileNotFoundError shadowed earlier, BlockingIOError, transient
        # read failures inside verify_package's tempdir unpack, etc.)
        # surfaces as ``disk.permission`` (exit 71) per spec §6.2 disk.*
        # bucket rather than escaping the public API uncaught.
        raise ParallaxIngestError(
            "disk.permission", f"OS-level read failure during verify: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Claim mapping (spec §4)
# ---------------------------------------------------------------------------


def _utc_now_iso_z() -> str:
    """Second-precision UTC ISO 8601 with Z suffix (matches audit_writer)."""
    return datetime.now(tz=UTC).strftime(_TS_FORMAT)


def _read_claim_frontmatter(claim_path: Path) -> Mapping[str, Any]:
    """Parse a v0.3 markdown claim file's YAML frontmatter."""
    try:
        text = claim_path.read_text(encoding="utf-8")
    except PermissionError as exc:
        raise ParallaxIngestError(
            "disk.permission", f"claim file unreadable: {claim_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise ParallaxIngestError(
            "claim.format_invalid", f"claim file read failed: {claim_path}: {exc}"
        ) from exc
    try:
        yaml_part, _body = split_frontmatter(text)
        data, _key_order = parse_frontmatter(yaml_part)
    except SchemaError as exc:
        raise ParallaxIngestError(
            "claim.format_invalid", f"claim frontmatter parse failed: {exc}"
        ) from exc
    return data


def _validate_and_check_duplicates(
    frontmatters: list[tuple[str, Mapping[str, Any]]],
) -> None:
    """Run v0.3 validator on each claim + reject in-package R4 duplicates."""
    seen_keys: set[tuple[str, str, str]] = set()
    for claim_id, fm in frontmatters:
        try:
            validate_v03_fields(fm, claim_id=claim_id)
        except SchemaError as exc:
            code_val = getattr(exc.code, "name", str(exc.code))
            if code_val == "CLAIM_SUBJECT_REQUIRED_FOR_CONFLICT":
                raise ParallaxIngestError(
                    "claim.subject_required_for_r4",
                    f"claim {claim_id}: R4 trigger without subject: {exc}",
                ) from exc
            raise ParallaxIngestError(
                "claim.format_invalid", f"claim {claim_id} invalid: {exc}"
            ) from exc

        # Duplicate guard per spec §4.5 — (subject, polarity, valid_from)
        # only meaningful when all three are present.
        subject = fm.get("subject")
        polarity = fm.get("polarity")
        valid_from = fm.get("valid_from")
        if isinstance(subject, str) and isinstance(polarity, str) and isinstance(
            valid_from, str
        ):
            key = (subject, polarity, valid_from)
            if key in seen_keys:
                raise ParallaxIngestError(
                    "claim.duplicate_in_package",
                    f"two claims share (subject, polarity, valid_from)={key!r}",
                )
            seen_keys.add(key)


def _build_audit_rows(
    *,
    manifest: Mapping[str, Any],
    unpacked_dir: Path,
    package_id: str,
    session_id: str,
    signer_id: str,
    signer_manifest_digest: str,
    timestamp: str,
) -> tuple[AuditRow, ...]:
    """Spec §4.4 — one canonical audit_row per claim in ``manifest["claims"]``."""
    raw_claims = manifest.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        raise ParallaxIngestError(
            "pkg.empty_package",
            "manifest['claims'] is empty — operator mistake guard (spec §2.6)",
        )

    # Parse all frontmatters up front so the in-package duplicate guard
    # runs before any audit row is built.
    parsed: list[tuple[str, Mapping[str, Any]]] = []
    for entry in raw_claims:
        if not isinstance(entry, dict):
            raise ParallaxIngestError(
                "pkg.semantic_invalid",
                f"manifest['claims'] entry must be object, got {type(entry).__name__}",
            )
        claim_id = entry.get("claim_id")
        rel_path = entry.get("path")
        if not isinstance(claim_id, str) or not isinstance(rel_path, str):
            raise ParallaxIngestError(
                "pkg.semantic_invalid",
                f"manifest claim entry missing claim_id/path: {entry!r}",
            )
        claim_path = unpacked_dir / rel_path
        fm = _read_claim_frontmatter(claim_path)
        parsed.append((claim_id, fm))

    _validate_and_check_duplicates(parsed)

    rows: list[AuditRow] = []
    for claim_id, _fm in parsed:
        envelope_message_id = str(uuid.uuid4())
        row_data: dict[str, Any] = {
            "claim_id": claim_id,
            "envelope_message_id": envelope_message_id,
            "outcome": "hit",
            "package_id": package_id,
            "session_id": session_id,
            "signer_id": signer_id,
            "signer_manifest_digest": signer_manifest_digest,
            "source": "aphelion",
            "ts": timestamp,
        }
        rows.append(canonicalize_row(row_data))
    return tuple(rows)


def _write_batch(
    audit_conn: sqlite3.Connection, batch: ClaimMappingBatch
) -> int:
    """Write every row in the batch, respecting the §4.7 write-order fence."""
    written = 0
    for row in batch.audit_rows:
        try:
            write_audit_row(audit_conn, row)
        except sqlite3.IntegrityError as exc:
            # UNIQUE on envelope_message_id — defence-in-depth per spec
            # §6.2 ``pkg.idempotency_duplicate`` (UUID collision = bug).
            raise ParallaxIngestError(
                "pkg.idempotency_duplicate",
                f"envelope_message_id UNIQUE collision: {exc}",
            ) from exc
        except AuditDbWriteError as exc:
            raise ParallaxIngestError(
                "disk.audit_db_write_failed",
                f"audit_db write failed: {exc}",
            ) from exc
        except PermissionError as exc:
            raise ParallaxIngestError(
                "disk.permission", f"audit_db permission denied: {exc}"
            ) from exc
        # M5 §8.1 fence — the row is committed iff write_audit_row returned
        # without raising. Explicit raise guard survives `python -O`.
        assert_audit_row_committed(True)
        written += 1
    return written


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def ingest_package(
    *,
    package_path: Path,
    audit_conn: sqlite3.Connection,
    package_dir: Path,
    trust_store_dir: Path,
    session_id: str,
) -> IngestReport:
    """End-to-end M6 ingest pipeline (spec §2-§5).

    Args:
        package_path: absolute path to the ``.aphelion.tar`` file.
        audit_conn: open audit-DB connection (via
            ``parallax.apex.audit_db.open_audit_db``) in autocommit state.
        package_dir: absolute ``PARALLAX_APHELION_PACKAGE_DIR``; package
            must resolve under this directory (spec §5.2 gate 4).
        trust_store_dir: absolute ``PARALLAX_APHELION_TRUST_STORE``;
            contains one ``.pem`` per trusted signer (spec §3.2).
        session_id: Parallax ingest correlator (e.g.
            ``"ingest:<ts>:<uuid4>"``); copied to every audit row.

    Returns:
        :class:`IngestReport` summarizing the run on success.

    Raises:
        ParallaxIngestError: on any §6.2 failure. ``.reason_code`` selects
            the spec-defined CLI exit code via ``.exit_code``.
    """
    started = time.monotonic()

    # Gates 1-3 + 7-9.
    resolved_pkg_dir = _validate_dir_arg(
        package_dir, label="PARALLAX_APHELION_PACKAGE_DIR", dir_unset_code="pkg.dir_unset"
    )
    resolved_trust_dir = _validate_dir_arg(
        trust_store_dir,
        label="PARALLAX_APHELION_TRUST_STORE",
        dir_unset_code="pkg.trust_store_missing",
    )
    trusted_fingerprints = _load_trust_store(resolved_trust_dir)

    # Gates 4-6.
    resolved_pkg = _validate_package_path(package_path, resolved_pkg_dir)

    # Aphelion end-to-end verification (spec §2.1).
    result = _run_aphelion_verify(resolved_pkg)

    # Multi-sig hard reject (spec §2.6 + §3.4).
    if len(result.envelopes) > 1:
        raise ParallaxIngestError(
            "signer.multi_sig_unsupported",
            f"package has {len(result.envelopes)} envelopes; "
            "M6 supports single-signer only",
        )
    if len(result.envelopes) == 0:
        # Belt-and-braces: verify_package with require_signed=True should
        # have already raised E_SIGNER_REQUIRED → mapped to pkg.unsigned.
        raise ParallaxIngestError(
            "pkg.unsigned", "no envelopes after verify_package(require_signed=True)"
        )

    # Trust-store enforcement (spec §3.3).
    decisions = _verify_trust(result.envelopes, resolved_pkg, trusted_fingerprints)
    for decision in decisions:
        if not decision.accepted:
            # decision.reason_code is non-None on the rejected branch.
            assert decision.reason_code is not None  # noqa: S101 — narrow type
            raise ParallaxIngestError(
                decision.reason_code,
                f"signer {decision.signer_id!r} rejected by trust store",
            )

    # Re-unpack to access manifest + claim frontmatters. verify_package
    # cleans its own tempdir internally; we own this second unpack so the
    # claim mapping has a stable file tree to walk.
    envelope = result.envelopes[0]
    signer_id: str = envelope.signer_id
    signer_manifest_digest: str = envelope.package_canonical_hash

    with tempfile.TemporaryDirectory(prefix="parallax-m6-") as tmp_dir:
        try:
            unpacked = unpack(resolved_pkg, tmp_dir, ExtractPolicy.default())
        except SecurityError as exc:
            raise ParallaxIngestError(
                "pkg.archive_unsafe", f"re-unpack safety violated: {exc}"
            ) from exc
        except PermissionError as exc:
            raise ParallaxIngestError(
                "disk.permission", f"re-unpack permission denied: {exc}"
            ) from exc
        except tarfile.TarError as exc:
            raise ParallaxIngestError(
                "pkg.archive_unsafe", f"re-unpack tar error: {exc}"
            ) from exc
        except OSError as exc:
            # Codex round-2 P2: catch generic OSError subclasses
            # (ENOSPC, EIO, ESTALE, transient FS faults) that escape
            # PermissionError/TarError narrowing. Map to disk.permission
            # so the failure becomes deterministic per spec §6.2.
            raise ParallaxIngestError(
                "disk.permission", f"re-unpack OS error: {exc}"
            ) from exc

        try:
            manifest = canonical_loads((unpacked / "manifest.json").read_bytes())
        except FileNotFoundError as exc:
            raise ParallaxIngestError(
                "pkg.semantic_invalid", f"manifest.json missing post-unpack: {exc}"
            ) from exc
        except SchemaError as exc:
            raise ParallaxIngestError(
                "pkg.semantic_invalid", f"manifest.json not canonical JSON: {exc}"
            ) from exc
        if not isinstance(manifest, dict):
            raise ParallaxIngestError(
                "pkg.semantic_invalid",
                f"manifest.json must be a JSON object, got {type(manifest).__name__}",
            )

        package_id = manifest.get("package_id")
        if not isinstance(package_id, str) or not package_id:
            raise ParallaxIngestError(
                "pkg.semantic_invalid",
                "manifest.json missing string 'package_id'",
            )

        timestamp = _utc_now_iso_z()
        audit_rows = _build_audit_rows(
            manifest=manifest,
            unpacked_dir=unpacked,
            package_id=package_id,
            session_id=session_id,
            signer_id=signer_id,
            signer_manifest_digest=signer_manifest_digest,
            timestamp=timestamp,
        )

        batch = ClaimMappingBatch(
            package_id=package_id,
            signer_id=signer_id,
            signer_manifest_digest=signer_manifest_digest,
            audit_rows=audit_rows,
            timestamp=timestamp,
        )

        rows_written = _write_batch(audit_conn, batch)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    report = IngestReport(
        package_id=package_id,
        package_path=str(resolved_pkg),
        claims_ingested=len(audit_rows),
        audit_rows_written=rows_written,
        signer_id=signer_id,
        elapsed_ms=elapsed_ms,
    )
    _log.info(
        "parallax_ingest_succeeded",
        extra={
            "event": "parallax_ingest_succeeded",
            "package_id": package_id,
            "package_path": str(resolved_pkg),
            "claim_count": report.claims_ingested,
            "audit_rows_written": report.audit_rows_written,
            "signer_id": signer_id,
            "elapsed_ms": elapsed_ms,
        },
    )
    return report
