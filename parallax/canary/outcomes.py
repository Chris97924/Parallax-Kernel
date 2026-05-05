"""US-009.3 §5 — canary outcome tracking for DoD verification.

A second SQLite table sharing the audit DB file. Each row tags a canary
event with the stage it ran in (`m4_1pct`/`m4_10pct`/`m4_50pct`/`m4_100pct`)
and the business-level outcome (`ok`/`discrepancy`/`data_loss`) — separate
from the HTTP-envelope columns on ``audit_log``.

Why a sibling table rather than ALTER TABLE on ``audit_log``:

1. Zero blast-radius on PR #41's clean audit_log module — adds columns
   would change the public dataclass, the record() signature, and the
   schema string in ways that ripple into every existing canary test.
2. ``audit_log`` is the HTTP-lifecycle ledger (status code, latency,
   ack trail). Business outcome is a different concern and naturally
   belongs in its own row store.
3. DoD queries that need both tables can JOIN on ``event_id``; queries
   that only need outcomes (e.g. discrepancy rate by stage) can read a
   single table.

Hard invariants:

* Same SQLite file as ``audit_log`` (per-thread connection cache lives
  in this module separately from AuditLog's; both are tiny). Resolution
  follows AuditLog's path rules so a single ``PARALLAX_CANARY_AUDIT_DB``
  env var configures both stores.
* ``stage`` is enumerated — invalid values raise on insert. The four
  shipping stages are pinned in :data:`KNOWN_STAGES`; adding a stage
  requires a council decision per acceptance spec O.3.
* ``outcome`` is enumerated — invalid values raise on insert.
* Re-recording the same ``event_id`` is idempotent (UPSERT) so canary
  retries don't double-count.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from parallax.canary.audit_log import AUDIT_DB_ENV, DEFAULT_AUDIT_DB_NAME

__all__ = [
    "CanaryOutcome",
    "OutcomeRecord",
    "OutcomeStore",
    "Stage",
    "KNOWN_STAGES",
    "KNOWN_OUTCOMES",
]

_log = logging.getLogger(__name__)

# Spec §5 AC-3.2 — four canary stages (1% / 10% / 50% / 100%).
Stage = str  # PEP-695 TypeAlias would be cleaner; use str for compat.
KNOWN_STAGES: Final[frozenset[str]] = frozenset(
    {"m4_1pct", "m4_10pct", "m4_50pct", "m4_100pct"}
)

# Spec §5 — three business outcomes that DoD metrics derive from.
CanaryOutcome = str
KNOWN_OUTCOMES: Final[frozenset[str]] = frozenset({"ok", "discrepancy", "data_loss"})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS canary_outcomes (
    event_id    TEXT PRIMARY KEY,
    stage       TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_canary_outcomes_stage_recorded
    ON canary_outcomes (stage, recorded_at);
"""


@dataclasses.dataclass(frozen=True)
class OutcomeRecord:
    """Immutable projection of a ``canary_outcomes`` row."""

    event_id: str
    stage: str
    outcome: str
    recorded_at: str


def _resolve_path(path: str | os.PathLike[str] | None) -> Path:
    """Resolve audit DB path — same rules as :func:`audit_log._resolve_path`."""
    if path is not None:
        return Path(path)
    env = os.environ.get(AUDIT_DB_ENV)
    if env:
        return Path(env)
    return Path.cwd() / DEFAULT_AUDIT_DB_NAME


class OutcomeStore:
    """SQLite-backed canary outcome store.

    Per-thread connection cache mirrors :class:`AuditLog` so multi-thread
    canary handlers don't trigger SQLite's check_same_thread guard.
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self._db_path: Path = _resolve_path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connections: dict[int, sqlite3.Connection] = {}
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        ident = threading.get_ident()
        with self._lock:
            conn = self._connections.get(ident)
            if conn is None:
                conn = sqlite3.connect(
                    self._db_path,
                    isolation_level=None,
                    timeout=5.0,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._connections[ident] = conn
        return conn

    def close(self) -> None:
        with self._lock:
            for conn in self._connections.values():
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover — best-effort
                    pass
            self._connections.clear()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------
    def record(
        self,
        *,
        event_id: str,
        stage: str,
        outcome: str,
        recorded_at: str | None = None,
    ) -> bool:
        """Insert (or upsert) a canary outcome row.

        Validates ``stage`` and ``outcome`` against :data:`KNOWN_STAGES` /
        :data:`KNOWN_OUTCOMES`. Unlike :meth:`AuditLog.record` (which is
        spec'd as fire-and-forget), validation failures here raise
        ``ValueError`` — DoD correctness depends on enumerated values.
        """
        if stage not in KNOWN_STAGES:
            raise ValueError(
                f"Unknown canary stage: {stage!r}. Expected one of {sorted(KNOWN_STAGES)}."
            )
        if outcome not in KNOWN_OUTCOMES:
            raise ValueError(
                f"Unknown canary outcome: {outcome!r}. Expected one of {sorted(KNOWN_OUTCOMES)}."
            )
        try:
            conn = self._connect()
            if recorded_at is None:
                conn.execute(
                    """
                    INSERT INTO canary_outcomes (event_id, stage, outcome)
                    VALUES (?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        stage   = excluded.stage,
                        outcome = excluded.outcome
                    """,
                    (event_id, stage, outcome),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO canary_outcomes (event_id, stage, outcome, recorded_at)
                    VALUES (?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        stage       = excluded.stage,
                        outcome     = excluded.outcome,
                        recorded_at = excluded.recorded_at
                    """,
                    (event_id, stage, outcome, recorded_at),
                )
            return True
        except (sqlite3.Error, OSError) as exc:
            _log.warning(
                "canary_outcomes.record_failed",
                extra={"event": "canary_outcomes.record_failed", "error": str(exc)},
            )
            return False

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------
    def lookup(self, event_id: str) -> OutcomeRecord | None:
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT event_id, stage, outcome, recorded_at "
                "FROM canary_outcomes WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            _log.warning(
                "canary_outcomes.lookup_failed",
                extra={"event": "canary_outcomes.lookup_failed", "error": str(exc)},
            )
            return None
        if row is None:
            return None
        return OutcomeRecord(
            event_id=row["event_id"],
            stage=row["stage"],
            outcome=row["outcome"],
            recorded_at=row["recorded_at"],
        )

    def iter_stage(self, stage: str) -> Iterable[OutcomeRecord]:
        """Yield outcomes for a stage, ordered by recorded_at ascending."""
        if stage not in KNOWN_STAGES:
            raise ValueError(
                f"Unknown canary stage: {stage!r}. Expected one of {sorted(KNOWN_STAGES)}."
            )
        conn = self._connect()
        cur = conn.execute(
            "SELECT event_id, stage, outcome, recorded_at "
            "FROM canary_outcomes WHERE stage = ? "
            "ORDER BY recorded_at ASC",
            (stage,),
        )
        for row in cur:
            yield OutcomeRecord(
                event_id=row["event_id"],
                stage=row["stage"],
                outcome=row["outcome"],
                recorded_at=row["recorded_at"],
            )
