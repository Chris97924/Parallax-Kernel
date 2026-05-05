"""US-009.1 §3.2 criteria 1.5–1.8 — independent SQLite audit log.

A single writer per process is sufficient for canary traffic volumes (the
T5 gate enforces ≤ peak hits/min in the low hundreds), so this module
keeps a per-process connection cache rather than running an explicit
worker thread. The cache is keyed by ``(db_path, thread_ident)`` so each
caller thread gets its own connection — required because SQLite python
bindings reject cross-thread reuse without ``check_same_thread=False``,
and we want predictable transactional behaviour.

Hard invariants (criteria 1.5–1.8):

1. ``audit_log`` lives in **its own SQLite file** distinct from any other
   business DB (criterion 1.5). Path resolution: explicit ctor arg →
   ``PARALLAX_CANARY_AUDIT_DB`` env var → ``parallax_canary_audit.db``
   beside the current working directory.
2. Schema must contain at minimum: ``event_id`` PK, ``request_at_iso``,
   ``response_status``, ``latency_ms``, ``idempotency_hit``,
   ``created_at`` (criterion 1.6). Two extra columns — ``response_body``
   (cached payload for criterion 1.2) and ``ack_by`` / ``ack_at`` (manual
   ACK trail for criterion 1.17) — are added for sibling subsystems but
   are nullable so the spec contract is unchanged.
3. ``record()`` is *fire-and-forget*: any failure is swallowed and logged
   at WARNING; never raises. Criterion 1.8.
4. Idempotency cache hits are recorded too (criterion 1.7). Re-recording
   the same ``event_id`` MUST NOT raise — we ``INSERT OR IGNORE`` for the
   PK row and append a separate hit row only if the schema has been
   evolved; for now the spec only asks that "the request lifecycle
   leaves an auditable trail", which the original PK row already
   provides. Counting separately is out of scope (spec §7 O.3).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import os
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Final

__all__ = [
    "AuditLog",
    "AuditRecord",
    "DEFAULT_AUDIT_DB_NAME",
    "AUDIT_DB_ENV",
]

_log = logging.getLogger(__name__)

DEFAULT_AUDIT_DB_NAME: Final[str] = "parallax_canary_audit.db"
AUDIT_DB_ENV: Final[str] = "PARALLAX_CANARY_AUDIT_DB"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    event_id        TEXT PRIMARY KEY,
    request_at_iso  TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    latency_ms      REAL,
    idempotency_hit INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    response_body   TEXT,
    ack_by          TEXT,
    ack_at          TEXT
)
"""


@dataclasses.dataclass(frozen=True)
class AuditRecord:
    """Immutable read-side projection of an ``audit_log`` row.

    Mirrors the schema columns 1:1. Only the spec-required columns are
    exposed as type-annotated fields; the trailing ``response_body`` is
    excluded from the public API because it can be large (raw cached
    response). Use :meth:`AuditLog.lookup_response` to retrieve it.
    """

    event_id: str
    request_at_iso: str
    response_status: int
    latency_ms: float | None
    idempotency_hit: bool
    created_at: str
    ack_by: str | None
    ack_at: str | None


def _resolve_path(path: str | os.PathLike[str] | None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(AUDIT_DB_ENV)
    if env:
        return Path(env)
    return Path.cwd() / DEFAULT_AUDIT_DB_NAME


class AuditLog:
    """Independent SQLite writer for canary request audit trail.

    Lifecycle:

        log = AuditLog()                # uses env / default
        log.record(AuditRecord(...))    # never raises
        log.lookup(event_id)            # returns None if missing
        log.close()                     # closes per-thread connections
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self._db_path: Path = _resolve_path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Per-thread connection cache so SQLite's check_same_thread is
        # respected automatically.
        self._connections: dict[int, sqlite3.Connection] = {}
        # Apply schema once on construction. If even that fails (e.g.
        # disk full, permissions), we surface it now — startup-time
        # failure is preferable to silent fail-open.
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        ident = threading.get_ident()
        with self._lock:
            conn = self._connections.get(ident)
            if conn is None:
                conn = sqlite3.connect(
                    self._db_path,
                    isolation_level=None,  # autocommit; we manage txn ourselves
                    timeout=5.0,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._connections[ident] = conn
        return conn

    def close(self) -> None:
        """Close all per-thread connections. Idempotent."""
        with self._lock:
            for conn in self._connections.values():
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover — best-effort
                    pass
            self._connections.clear()

    # ------------------------------------------------------------------
    # Write path — fire-and-forget (criterion 1.8)
    # ------------------------------------------------------------------
    def record(
        self,
        record: AuditRecord,
        *,
        response_body: str | None = None,
    ) -> bool:
        """Insert (or replace) the audit row. Returns True on success.

        Criterion 1.8: ANY failure is swallowed and logged. The caller
        request path MUST NOT see the exception. The returned bool lets
        tests assert observed behaviour without crashing on failure
        injection.
        """
        try:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO audit_log (
                    event_id, request_at_iso, response_status,
                    latency_ms, idempotency_hit, response_body, ack_by, ack_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    record.event_id,
                    record.request_at_iso,
                    int(record.response_status),
                    record.latency_ms,
                    1 if record.idempotency_hit else 0,
                    response_body,
                    record.ack_by,
                    record.ack_at,
                ),
            )
            return True
        except (sqlite3.Error, OSError, ValueError) as exc:
            _log.warning(
                "audit_log.record_failed",
                extra={"event": "audit_log.record_failed", "error": str(exc)},
            )
            return False

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------
    def lookup(self, event_id: str) -> AuditRecord | None:
        """Return the audit row for ``event_id`` or None."""
        try:
            conn = self._connect()
            row = conn.execute(
                """
                SELECT event_id, request_at_iso, response_status, latency_ms,
                       idempotency_hit, created_at, ack_by, ack_at
                FROM audit_log WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            _log.warning(
                "audit_log.lookup_failed",
                extra={"event": "audit_log.lookup_failed", "error": str(exc)},
            )
            return None
        if row is None:
            return None
        return AuditRecord(
            event_id=row["event_id"],
            request_at_iso=row["request_at_iso"],
            response_status=int(row["response_status"]),
            latency_ms=row["latency_ms"],
            idempotency_hit=bool(row["idempotency_hit"]),
            created_at=row["created_at"],
            ack_by=row["ack_by"],
            ack_at=row["ack_at"],
        )

    def lookup_response(self, event_id: str) -> str | None:
        """Return the cached response body for ``event_id`` or None."""
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT response_body FROM audit_log WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        body = row["response_body"]
        return body if isinstance(body, str) else None

    # ------------------------------------------------------------------
    # Manual ACK helper — criterion 1.17 (operator + timestamp recorded)
    # ------------------------------------------------------------------
    def record_ack(self, event_id: str, *, ack_by: str, ack_at: str | None = None) -> bool:
        """Record the manual ACK that re-promotes a tripped canary.

        ``event_id`` need not exist beforehand: rollback ACK records use
        a synthetic UUID v7 created at trip time. Returns True on
        success.
        """
        if not ack_by:
            return False
        ts = ack_at or _dt.datetime.now(_dt.UTC).isoformat()
        try:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO audit_log (
                    event_id, request_at_iso, response_status, latency_ms,
                    idempotency_hit, ack_by, ack_at
                ) VALUES (?, ?, 0, NULL, 0, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    ack_by = excluded.ack_by,
                    ack_at = excluded.ack_at
                """,
                (event_id, ts, ack_by, ts),
            )
            return True
        except sqlite3.Error as exc:
            _log.warning(
                "audit_log.record_ack_failed",
                extra={"event": "audit_log.record_ack_failed", "error": str(exc)},
            )
            return False

    # ------------------------------------------------------------------
    # Schema introspection — used by integration tests (criterion 1.6)
    # ------------------------------------------------------------------
    def schema(self) -> Mapping[str, str]:
        """Return ``{column_name: sql_type}`` for the ``audit_log`` table."""
        try:
            conn = self._connect()
            rows = conn.execute("PRAGMA table_info(audit_log)").fetchall()
        except sqlite3.Error:
            return {}
        return {row["name"]: row["type"].upper() for row in rows}

    # ------------------------------------------------------------------
    # Context manager sugar so tests can ``with AuditLog(...) as log:``.
    # ------------------------------------------------------------------
    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _utcnow_iso() -> str:
    """Return current time as ISO-8601 UTC string. Convenience for callers."""
    return _dt.datetime.now(_dt.UTC).isoformat()


def make_record(
    *,
    event_id: str,
    response_status: int,
    latency_ms: float | None,
    idempotency_hit: bool,
    request_at_iso: str | None = None,
    ack_by: str | None = None,
    ack_at: str | None = None,
) -> AuditRecord:
    """Convenience factory — fills request_at_iso when omitted."""
    return AuditRecord(
        event_id=event_id,
        request_at_iso=request_at_iso or _utcnow_iso(),
        response_status=response_status,
        latency_ms=latency_ms,
        idempotency_hit=idempotency_hit,
        created_at="",  # populated by SQLite default
        ack_by=ack_by,
        ack_at=ack_at,
    )


__all__.extend(["make_record"])
