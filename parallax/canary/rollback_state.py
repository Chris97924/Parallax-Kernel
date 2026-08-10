"""#106.1 — durable projection of the RollbackController state machine.

:class:`parallax.canary.rollback.RollbackController` lives entirely in memory,
inside a ``parallax canary`` process that Prometheus never scrapes. The stage-1
dashboard has a "Rollback controller state" panel selecting
``parallax_canary_rollback_state``, and nothing has ever produced it.

This is the durable side of that state: one row, in the same SQLite file as
``audit_log`` and ``canary_outcomes``, upserted whenever the controller changes
state. The server-side exporter (:mod:`parallax.canary.exporter`) reads it, so
the panel reflects what the CLI-side controller actually did rather than nothing
at all.

Why a table rather than deriving it from the audit log: the audit log records
ACKs but has no representation of "tripped", so a derivation could only ever
show half the state machine — and the half it would miss is the one an operator
is looking at the panel to find.

Numeric encoding is deliberate and small. ``-1`` for unknown matters as much as
the three real states: a panel that showed ``0`` (running) for a deployment that
has never run a canary would be asserting health it cannot know. Read it with
``parallax_canary_store_present``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Final

__all__ = [
    "ROLLBACK_STATE_SCHEMA",
    "STATE_UNKNOWN",
    "STATE_VALUES",
    "RollbackStateStore",
]

_log = logging.getLogger(__name__)

#: Gauge value when no durable state has ever been written. NOT 0 — see module
#: docstring; 0 means "running", which is a claim, and this is the absence of one.
STATE_UNKNOWN: Final[float] = -1.0

#: ``CanaryState`` value -> gauge value. Mirrors
#: :class:`parallax.canary.rollback.CanaryState`; the mapping lives here rather
#: than on the enum so the exporter can decode a row without importing the
#: controller (and its trigger machinery) into the server process.
STATE_VALUES: Final[dict[str, float]] = {
    "running": 0.0,
    "tripped": 1.0,
    "awaiting_ack": 2.0,
}

ROLLBACK_STATE_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS canary_rollback_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    state       TEXT NOT NULL,
    tripped_by  TEXT,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class RollbackStateStore:
    """Single-row durable store for the canary rollback state.

    Write path only — the exporter reads the table directly over its own
    read-only connection so a scrape can never create or migrate the database.

    Failures are swallowed and logged, matching :class:`AuditLog`'s
    fire-and-forget contract (criterion 1.8): losing the dashboard projection
    must never take down the rollback controller, which is the thing actually
    protecting the canary.
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)
            self._conn.executescript(ROLLBACK_STATE_SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            _log.warning(
                "canary_rollback_state.open_failed",
                extra={
                    "event": "canary_rollback_state.open_failed",
                    "exc_class": type(exc).__name__,
                },
            )
            self._conn = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    def record(self, state: str, *, tripped_by: str | None = None) -> bool:
        """Upsert the controller's current state. Returns True on success."""
        if self._conn is None:
            return False
        if state not in STATE_VALUES:
            raise ValueError(f"Unknown canary rollback state: {state!r}")
        try:
            self._conn.execute(
                """
                INSERT INTO canary_rollback_state (id, state, tripped_by, updated_at)
                VALUES (1, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(id) DO UPDATE SET
                    state      = excluded.state,
                    tripped_by = excluded.tripped_by,
                    updated_at = excluded.updated_at
                """,
                (state, tripped_by),
            )
            return True
        except sqlite3.Error as exc:
            _log.warning(
                "canary_rollback_state.record_failed",
                extra={
                    "event": "canary_rollback_state.record_failed",
                    "exc_class": type(exc).__name__,
                },
            )
            return False

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover — best-effort
                pass
            self._conn = None
