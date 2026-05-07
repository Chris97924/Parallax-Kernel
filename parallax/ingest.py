"""High-level ingest helpers for Parallax.

Wraps :mod:`parallax.sqlite_store` with UPSERT semantics:

* Every direct-input call (``source_id=None``) lazily creates a synthetic
  source row keyed ``direct:<user_id>``. This is what closes the UNIQUE
  NULL-hole on ``claims.source_id``.
* content_hash collisions are absorbed -- the existing row's id is returned
  and no duplicate is written. Re-ingestion is therefore safe and the
  reaffirm branch emits a ``*.reaffirmed`` event via the events log (see
  :func:`parallax.sqlite_store.reaffirm` / :func:`parallax.events.record_memory_reaffirmed`).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from typing import Literal

from ulid import ULID

from parallax import telemetry
from parallax.events import record_event, record_memory_reaffirmed
from parallax.hashing import content_hash
from parallax.obs.log import get_logger
from parallax.obs.metrics import get_counter
from parallax.sqlite_store import (
    Claim,
    Memory,
    Source,
    insert_claim,
    insert_memory,
    insert_source,
    now_iso,
    query,
)
from parallax.transitions import CLAIM_TRANSITIONS

_log = get_logger("parallax.ingest")
_tlog = telemetry.get_logger("parallax.ingest.telemetry")
_c_memory = get_counter("ingest_memory_total")
_c_claim = get_counter("ingest_claim_total")
_c_dedup = get_counter("dedup_hit_total")

__all__ = [
    "ingest_memory",
    "ingest_memory_with_status",
    "ingest_claim",
    "ingest_claim_with_status",
    "synthetic_direct_source_id",
]


def _ulid() -> str:
    return str(ULID())


def synthetic_direct_source_id(user_id: str) -> str:
    """Return the canonical synthetic source id for direct input."""
    return f"direct:{user_id}"


def _ensure_direct_source(conn: sqlite3.Connection, user_id: str) -> str:
    source_id = synthetic_direct_source_id(user_id)
    insert_source(
        conn,
        Source(
            source_id=source_id,
            uri=f"parallax://direct/{user_id}",
            kind="chat",
            content_hash=content_hash(source_id),
            user_id=user_id,
            ingested_at=now_iso(),
            state="ingested",
        ),
    )
    return source_id


def _ensure_external_source(
    conn: sqlite3.Connection,
    user_id: str,
    source_id: str,
) -> str:
    # Lazy-create a sources row for a client-supplied ``source_id`` so the
    # FK ``memories.source_id REFERENCES sources(source_id)`` (and the
    # equivalent on ``claims``) always points to a row that already exists.
    # Idempotent via INSERT OR IGNORE on the source_id PK -- second writes
    # for the same id silently no-op without overwriting the original
    # ``user_id`` / ``ingested_at`` of the registering caller.
    #
    # Reserved-namespace handling: ``direct:<user>`` is the canonical
    # synthetic source for direct input (see ``_ensure_direct_source``). If
    # the client supplies their own synthetic id (``direct:<self>``), route
    # to the canonical helper so the row is created with ``kind='chat'`` /
    # ``uri='parallax://direct/...'`` -- never as kind='external'. Cross-user
    # ``direct:<other>`` is rejected: without this guard a malicious caller
    # could pre-empt another user's synthetic row with external metadata,
    # and a later ingest for that user with ``source_id=None`` would no-op
    # via INSERT OR IGNORE and leave the direct: row permanently mislabeled.
    if source_id.startswith("direct:"):
        own = synthetic_direct_source_id(user_id)
        if source_id != own:
            raise ValueError(
                f"source_id {source_id!r} targets reserved direct: namespace "
                f"of another user; only {own!r} is permitted for user_id "
                f"{user_id!r}"
            )
        return _ensure_direct_source(conn, user_id)
    insert_source(
        conn,
        Source(
            source_id=source_id,
            uri=f"parallax://external/{source_id}",
            kind="external",
            content_hash=content_hash(source_id),
            user_id=user_id,
            ingested_at=now_iso(),
            state="ingested",
        ),
    )
    return source_id


def ingest_memory(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    title: str | None,
    summary: str | None,
    vault_path: str,
    source_id: str | None = None,
) -> str:
    """UPSERT a memory row. Returns the persisted memory_id.

    Backward-compat wrapper around :func:`ingest_memory_with_status` that
    drops the dedup flag. Prefer ``ingest_memory_with_status`` when the
    caller needs to know whether the row was deduped (Lane D-3 router).
    """
    persisted_id, _deduped = ingest_memory_with_status(
        conn,
        user_id=user_id,
        title=title,
        summary=summary,
        vault_path=vault_path,
        source_id=source_id,
    )
    return persisted_id


def ingest_memory_with_status(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    title: str | None,
    summary: str | None,
    vault_path: str,
    source_id: str | None = None,
) -> tuple[str, bool]:
    """UPSERT a memory row. Returns ``(persisted_memory_id, deduped)``.

    Race-safe: attempts INSERT OR IGNORE then re-selects by the UNIQUE
    (content_hash, user_id) index so the caller always receives the id of
    the row that is actually persisted, never a ULID that was silently
    dropped by the IGNORE branch.

    ``deduped`` is ``True`` when the persisted row is an existing match
    (the new ULID was minted but discarded by INSERT OR IGNORE), ``False``
    on first-write of a fresh content hash. The Lane D-3 router uses this
    flag to populate ``IngestResult.deduped`` without a TOCTOU-prone
    content_hash pre-check.
    """
    start = time.perf_counter()
    try:
        if source_id is None:
            source_id = _ensure_direct_source(conn, user_id)
        else:
            source_id = _ensure_external_source(conn, user_id, source_id)

        # v0.4.0: hashing.normalize encodes None with a distinct sentinel,
        # so title/summary flow straight through — no boundary conversion,
        # no silent collision between memory(title=None) and memory(title='').
        ch = content_hash(title, summary, vault_path)
        now = now_iso()
        new_id = _ulid()
        candidate = Memory(
            memory_id=new_id,
            user_id=user_id,
            source_id=source_id,
            vault_path=vault_path,
            title=title,
            summary=summary,
            content_hash=ch,
            state="active",
            created_at=now,
            updated_at=now,
        )
        insert_memory(conn, candidate)
        row = query(
            conn,
            "SELECT memory_id FROM memories WHERE content_hash = ? AND user_id = ?",
            (ch, user_id),
        )
        persisted_id = row[0]["memory_id"]
        _c_memory.inc()
        telemetry.inc("ingested_total")
        deduped = persisted_id != new_id
        if deduped:
            _c_dedup.inc()
            telemetry.inc("dedup_hits_total")
            telemetry.emit_dedup_hit(_tlog, kind="memory", user_id=user_id, memory_id=persisted_id)
            record_memory_reaffirmed(conn, user_id=user_id, memory_id=persisted_id)
        else:
            # Emit memory.created with the full row payload so
            # parallax.replay can rebuild this row bit-for-bit. Safe to
            # use ``asdict(candidate)`` because on the first-write branch
            # ``candidate.memory_id == new_id == persisted_id``.
            record_event(
                conn,
                user_id=user_id,
                actor="system",
                event_type="memory.created",
                target_kind="memory",
                target_id=persisted_id,
                payload=dataclasses.asdict(candidate),
            )
        _log.info(
            "ingest_memory",
            extra={
                "event": "ingest_memory",
                "user_id": user_id,
                "memory_id": persisted_id,
                "deduped": deduped,
            },
        )
        return persisted_id, deduped
    except Exception as exc:
        telemetry.emit_ingest_error(_tlog, kind="memory", user_id=user_id, error=str(exc))
        raise
    finally:
        telemetry.observe_latency_ms((time.perf_counter() - start) * 1000.0)


def ingest_claim(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    subject: str,
    predicate: str,
    object_: str,
    source_id: str | None = None,
    confidence: float | None = None,
    state: Literal["auto", "pending", "confirmed", "rejected"] = "auto",
) -> str:
    """UPSERT a claim row. Returns the persisted claim_id.

    Backward-compat wrapper around :func:`ingest_claim_with_status` that
    drops the dedup flag. Prefer ``ingest_claim_with_status`` when the
    caller needs to know whether the row was deduped (Lane D-3 router).
    """
    persisted_id, _deduped = ingest_claim_with_status(
        conn,
        user_id=user_id,
        subject=subject,
        predicate=predicate,
        object_=object_,
        source_id=source_id,
        confidence=confidence,
        state=state,
    )
    return persisted_id


def ingest_claim_with_status(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    subject: str,
    predicate: str,
    object_: str,
    source_id: str | None = None,
    confidence: float | None = None,
    state: Literal["auto", "pending", "confirmed", "rejected"] = "auto",
) -> tuple[str, bool]:
    """UPSERT a claim row. Returns ``(persisted_claim_id, deduped)``.

    Race-safe via INSERT OR IGNORE + re-select on the UNIQUE
    (content_hash, source_id, user_id) index (ADR-005, v0.5.0-pre1). See
    :func:`ingest_memory_with_status` for the dedup-flag rationale.

    ``state`` must be a registered initial state in
    :data:`parallax.transitions.CLAIM_TRANSITIONS` (defensive validation at
    the ingest boundary per the input-validation rule); defaults to
    ``'auto'`` to preserve prior behaviour. The extract layer uses
    ``state='pending'`` for low-confidence claims that need review.
    """
    if state not in CLAIM_TRANSITIONS:
        raise ValueError(
            f"invalid claim state {state!r}; " f"expected one of {sorted(CLAIM_TRANSITIONS)}"
        )
    start = time.perf_counter()
    try:
        if source_id is None:
            source_id = _ensure_direct_source(conn, user_id)
        else:
            source_id = _ensure_external_source(conn, user_id, source_id)

        # v0.5.0-pre1 / ADR-005: user_id is part of the hash so cross-user
        # same-source same-triple claims stay distinct.
        ch = content_hash(subject, predicate, object_, source_id, user_id)
        now = now_iso()
        new_id = _ulid()
        candidate = Claim(
            claim_id=new_id,
            user_id=user_id,
            subject=subject,
            predicate=predicate,
            object=object_,
            source_id=source_id,
            content_hash=ch,
            confidence=confidence,
            state=state,
            created_at=now,
            updated_at=now,
        )
        insert_claim(conn, candidate)
        row = query(
            conn,
            "SELECT claim_id FROM claims WHERE content_hash = ? AND source_id = ? "
            "AND user_id = ?",
            (ch, source_id, user_id),
        )
        persisted_id = row[0]["claim_id"]
        _c_claim.inc()
        telemetry.inc("ingested_total")
        deduped = persisted_id != new_id
        if deduped:
            _c_dedup.inc()
            telemetry.inc("dedup_hits_total")
            telemetry.emit_dedup_hit(_tlog, kind="claim", user_id=user_id, claim_id=persisted_id)
        else:
            # Emit claim.created with the full row payload. Same
            # ``persisted_id == candidate.claim_id`` invariant as the
            # memory branch above.
            record_event(
                conn,
                user_id=user_id,
                actor="system",
                event_type="claim.created",
                target_kind="claim",
                target_id=persisted_id,
                payload=dataclasses.asdict(candidate),
            )
        _log.info(
            "ingest_claim",
            extra={
                "event": "ingest_claim",
                "user_id": user_id,
                "claim_id": persisted_id,
                "deduped": deduped,
            },
        )
        return persisted_id, deduped
    except Exception as exc:
        telemetry.emit_ingest_error(_tlog, kind="claim", user_id=user_id, error=str(exc))
        raise
    finally:
        telemetry.observe_latency_ms((time.perf_counter() - start) * 1000.0)
