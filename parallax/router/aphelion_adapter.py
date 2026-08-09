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

import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aphelion.errors import SchemaError
from aphelion.read_adapter import AphelionReadAdapter as _V03Reader
from aphelion.read_adapter import ConflictClass
from aphelion.v03_validator import validate_v03_fields

from parallax.apex.audit_db import AuditDbUsageError, AuditDbWriteError, write_audit_row
from parallax.apex.audit_writer import (
    AuditRow,
    AuditWriteOrderViolation,
    assert_audit_row_committed,
    canonicalize_row,
)
from parallax.apex.envelope import (
    Envelope,
    PayloadType,
    Source,
    compute_checksum,
    parse_envelope,
)
from parallax.obs.log import get_logger, safe_log_error
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest

__all__ = ["CLAIM_CONTENT_KEY", "AphelionReadAdapter", "AphelionUnreachableError"]

# JSON logger, matching ``dual_read.py`` / ``shadow.py`` — the other modules that
# emit through ``parallax.obs.log``'s helpers. The choice is load-bearing rather
# than cosmetic: those helpers put everything an operator needs (``exc_class``,
# ``exc_digest``, ``envelope_message_id``) in the record's *extras*, and the
# stdlib default formatter renders only ``msg``. Under a plain logger the
# redacted call sites below would therefore print a bare event name and the
# #106.5 fix would read as "the detail was deleted" rather than "the detail was
# made value-free". ``JSONFormatter`` emits every field.
_log = get_logger(__name__)

ENVELOPE_VERSION_LITERAL = "0.1"
ENVELOPE_SCHEMA_VERSION = 1

# Key under which the M7 package loader
# (:meth:`parallax.apex.router.ApexPublicReadRouter._project_claims`) projects a
# claim's markdown *body* onto the otherwise-frontmatter-only claim mapping, so
# the hit builder below can surface claim content (not just the subject label).
# It is intentionally a single shared constant: the loader writes it, the hit
# builder reads it, and nothing else depends on the name. The v0.3 validator
# tolerates the extra key (only ``conflict_class`` is reserved) and the R4
# reader ignores unknown keys, so injecting it does not perturb detection. The
# M5 dual-read path uses ``_empty_loader`` (no body), so content-bearing hits
# are a pure additive no-op there.
CLAIM_CONTENT_KEY = "body"

ClaimLoader = Callable[[QueryRequest], Iterable[Mapping[str, Any]]]

# Zero-arg callable returning the calling thread's audit-db connection.
# query.py wires this to ``lambda: get_thread_local_audit_conn(path)`` so the
# connection is opened lazily inside the DualReadRouter worker thread (not the
# request thread). See ``parallax.apex.audit_db.get_thread_local_audit_conn``.
AuditConnProvider = Callable[[], sqlite3.Connection]


class AphelionUnreachableError(Exception):
    """Raised when the Aphelion secondary cannot be reached or an emit fails.

    ``reason`` is a short tag used for outcome classification in DualReadRouter:
      - ``"timeout"`` — secondary exceeded secondary_timeout_ms
      - ``"connection_error"`` — network/transport failure (reserved for future)
      - ``"claim_loader_error"`` — claim_loader raised before R4 detection ran
      - ``"claim_schema_error"`` — v0.3 validator rejected a candidate frontmatter
      - ``"envelope_checksum_mismatch"`` — envelope round-trip failed
      - ``"audit_row_invalid"`` — canonicalize_row rejected the row
      - ``"audit_db_write_failed"`` — write_audit_row raised AuditDbWriteError
        (disk full / locked / commit failed); fail-closed, no envelope emitted
      - ``"audit_db_usage_error"`` — write_audit_row raised AuditDbUsageError
        (caller-contract violation, e.g. conn already in a transaction)
      - ``"audit_db_integrity_error"`` — audit row INSERT hit a UNIQUE/CHECK
        constraint (duplicate envelope_message_id); fail-closed, no envelope
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


def _claim_content(claim: Mapping[str, Any]) -> tuple[str, str]:
    """Best human-readable content for a claim hit, plus which field it came from.

    Returns ``(content, source)`` where ``source`` is one of ``"body"`` /
    ``"title"`` / ``"subject"``. Preference order: the markdown ``body`` (the
    claim statement, projected by the M7 loader under :data:`CLAIM_CONTENT_KEY`)
    → the ``title`` frontmatter field → the ``subject`` label. The subject
    fallback preserves the pre-#71 behaviour for callers (e.g. M5 dual-read)
    whose claims carry no body/title.

    The ``source`` is surfaced on the hit so a degraded label-only fall-through
    (e.g. an M7 claim whose body was lost at ingest) is distinguishable from a
    genuine content hit — the fall-through itself is intentional (graceful) but
    must not be silent.
    """
    body = claim.get(CLAIM_CONTENT_KEY)
    if isinstance(body, str) and body.strip():
        return body.strip(), CLAIM_CONTENT_KEY
    title = _string_field(claim, "title")
    if title is not None:
        return title, "title"
    return (_string_field(claim, "subject") or ""), "subject"


def _build_hit(claim: Mapping[str, Any]) -> dict[str, Any]:
    """Build a content-bearing RetrievalEvidence hit from a surfaced claim (#71).

    The hit aligns with the 3-layer ``RetrievalHit`` contract
    (``parallax.retrieve.RetrievalHit``):
      * ``text`` — claim content (L1 title surface), via :func:`_claim_content`.
      * ``evidence`` — a one-sentence provenance reason (L2), ``str | None``
        per the contract (never a Mapping).
      * ``full`` — a shallow dict snapshot of the claim frontmatter + body (L3),
        ``dict | None`` (JSON-safe while frontmatter values stay scalars/lists).
    ``id`` / ``kind`` / ``polarity`` / ``subject`` are preserved as discrete
    fields so no information is lost relative to the pre-#71 subject-only hit;
    ``content_source`` records which field ``text`` was drawn from.
    """
    claim_id = _string_field(claim, "claim_id") or ""
    subject = _string_field(claim, "subject") or ""
    polarity = _string_field(claim, "polarity") or "affirm"
    package_id = _string_field(claim, "package_id")
    content, content_source = _claim_content(claim)

    package_part = f", package={package_id}" if package_id else ""
    provenance = (
        f"aphelion claim {claim_id} (subject={subject!r}, polarity={polarity}{package_part})"
    )

    hit: dict[str, Any] = {
        "id": claim_id,
        "text": content,
        "kind": "aphelion_claim",
        "polarity": polarity,
        "subject": subject,
        "content_source": content_source,
        "evidence": provenance,
        "full": dict(claim),
    }
    created_at = _string_field(claim, "created_at")
    if created_at is not None:
        hit["created_at"] = created_at
    return hit


class AphelionReadAdapter:
    """Wired QueryPort adapter for Aphelion secondary reads (PR-D / M5 entry).

    Instantiation is cheap; the adapter is stateless modulo ``last_envelope``
    / ``last_audit_row`` which are overwritten on every ``query()`` call.

    Args:
        audit_conn_provider: REQUIRED zero-arg callable returning the calling
            thread's audit-db :class:`sqlite3.Connection`. ``query()`` invokes
            it on a real claim hit to persist the audit row BEFORE the
            envelope's ``audit_db_ref`` sha256 is computed (apex-m5-envelope
            -spec.md §8.1 write-order invariant). Production wires this to
            ``lambda: get_thread_local_audit_conn(app.state.audit_db_path)``;
            the callable is evaluated inside the DualReadRouter worker thread
            so the per-thread connection is created on the correct thread.
            There is intentionally no default — the adapter must never emit an
            envelope without a committed audit row.
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
        audit_conn_provider: AuditConnProvider,
        package_dir: Path | None = None,
        timeout_ms: float = 100.0,
        claim_loader: ClaimLoader | None = None,
    ) -> None:
        self._audit_conn_provider = audit_conn_provider
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
        # Clear cached envelope/audit state up-front so any subsequent failure
        # (loader, validator, reader, canonicalize, parse_envelope) cannot leak
        # values from a prior successful query into ``last_envelope`` /
        # ``last_audit_row``. Real values are reassigned only after the full
        # success path below.
        self.last_envelope = None
        self.last_audit_row = None

        subject = _resolve_subject(request)
        try:
            candidates = tuple(self._claim_loader(request))
        except AphelionUnreachableError:
            # Loader pre-raised the contract error (e.g. unsafe_archive,
            # unsigned_package) — let it propagate verbatim without remapping.
            raise
        except Exception as exc:
            # Wrap arbitrary loader failures (filesystem / unpack / network
            # once M6/M7 lands) so DualReadRouter classifies them as
            # ``aphelion_unreachable`` rather than ``primary_only``.
            raise AphelionUnreachableError("claim_loader_error") from exc

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

        hits = tuple(_build_hit(claim) for claim in result.surfaced)

        evidence = RetrievalEvidence(
            hits=hits,
            stages=("aphelion_v03_r4",),
            notes=(f"conflict_class={result.conflict_class.value}",),
        )

        if primary_claim_id is None or primary_package_id is None:
            return evidence

        # Spec ``audit-db-path-config.md`` §6.1 (audit row schema, ``ts`` field)
        # requires the audit row's ``ts`` to match ``envelope.created_at``
        # (second precision matching envelope.created_at, per §6.1 schema
        # comment). Compute a single emission timestamp
        # here and reuse it for both fields so the contract holds even when the
        # reader runs against a non-default ``query_time`` or crosses a second
        # boundary between the audit row and the envelope construction.
        emit_ts = _utc_now_iso_z()
        envelope_message_id = str(uuid.uuid4())
        audit_row_data: dict[str, Any] = {
            "claim_id": primary_claim_id,
            "package_id": primary_package_id,
            "envelope_message_id": envelope_message_id,
            "ts": emit_ts,
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

        # Spec apex-m5-envelope-spec.md §8.1 write-order invariant: the audit
        # row MUST be committed to audit.db BEFORE the envelope's audit_db_ref
        # sha256 is computed and the envelope is emitted — otherwise the
        # envelope carries a hash of a row no one ever wrote. write_audit_row
        # failures are fail-closed: the secondary is treated as unreachable
        # (DualReadRouter falls back to primary) and NO envelope is emitted,
        # so last_envelope / last_audit_row stay None (cleared at the top of
        # this method). No queue / retry — a retried row cannot re-emit an
        # already-abandoned envelope, so retry has no semantic value here.
        #
        # The except fence below is total on purpose: ANY exception escaping
        # this block reaches DualReadRouter as an *unexpected* exception, which
        # it classifies as "primary_only" — silently losing the
        # "aphelion_unreachable" signal AND the circuit-breaker increment. So
        # every failure mode (the provider raising, write_audit_row's
        # un-wrapped INSERT-step sqlite errors, its belt-and-braces
        # canonicalize re-validation ValueError, anything else) is funnelled
        # into AphelionUnreachableError.
        committed = False
        try:
            write_audit_row(self._audit_conn_provider(), audit_row)
            committed = True
        except sqlite3.IntegrityError as exc:
            safe_log_error(
                _log,
                "audit_db_integrity_error",
                exc=exc,
                envelope_message_id=envelope_message_id,
            )
            raise AphelionUnreachableError("audit_db_integrity_error") from exc
        except AuditDbUsageError as exc:
            safe_log_error(_log, "audit_db_usage_error", exc=exc)
            raise AphelionUnreachableError("audit_db_usage_error") from exc
        except (AuditDbWriteError, sqlite3.Error) as exc:
            # AuditDbWriteError = BEGIN/COMMIT failures write_audit_row wraps;
            # a bare sqlite3.Error = an INSERT-step failure it re-raises
            # un-wrapped (e.g. OperationalError on disk-full mid-statement).
            safe_log_error(
                _log,
                "audit_db_write_failed",
                exc=exc,
                envelope_message_id=envelope_message_id,
            )
            raise AphelionUnreachableError("audit_db_write_failed") from exc
        except Exception as exc:  # noqa: BLE001 — see the fence rationale above
            # The audit-conn provider raising (e.g. open_audit_db hitting
            # AuditDbConfigError on a worker thread), or any other unforeseen
            # failure. Must NOT escape as "primary_only".
            safe_log_error(
                _log,
                "audit_db_write_failed",
                exc=exc,
                envelope_message_id=envelope_message_id,
            )
            raise AphelionUnreachableError("audit_db_write_failed") from exc

        # Explicit write-order guard (survives ``python -O`` — see
        # audit_writer.assert_audit_row_committed). ``committed`` is set True
        # ONLY after write_audit_row returns normally above, so a future
        # refactor that drops the write, or moves this guard / the envelope
        # assembly ahead of it, trips AuditWriteOrderViolation instead of
        # emitting an envelope for an uncommitted row. Passing a literal here
        # would make the guard a no-op.
        #
        # The guard MUST run inside the same total-fence semantic as the
        # write-audit-row block: a guard failure means we are about to emit an
        # envelope whose audit_db_ref is decoupled from any actually-committed
        # row. Letting that escape unwrapped would reach DualReadRouter as an
        # *unexpected* exception, get classified as "primary_only", and lose
        # the aphelion_unreachable signal + the circuit-breaker increment —
        # the exact silent-failure mode the fence above was written to
        # prevent. Wrap the guard so a write-order violation is fail-closed.
        try:
            assert_audit_row_committed(committed)
        except AuditWriteOrderViolation as exc:
            safe_log_error(
                _log,
                "audit_write_order_violation",
                exc=exc,
                committed=committed,
                envelope_message_id=envelope_message_id,
            )
            raise AphelionUnreachableError("audit_write_order_violation") from exc

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
            "created_at": emit_ts,
            "source": Source.APHELION.value,
            # sha256 of the row already committed above (write-order invariant).
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
