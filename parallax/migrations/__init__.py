"""Schema migration framework for Parallax.

Each migration is an ``up()`` / ``down()`` pair keyed by an integer version.
``schema_migrations`` records which versions have been applied so the same
migration is never run twice.

Atomicity (v0.1.5+):
    Each migration's ``up()`` runs inside an explicit
    ``BEGIN IMMEDIATE`` ... ``COMMIT`` block together with the matching
    ``schema_migrations`` insert. They succeed or fail together; on any
    exception the entire transaction is rolled back, so a partially-applied
    DDL pass is never recorded as applied. Migration ``up()`` functions
    therefore MUST issue individual ``conn.execute(stmt)`` calls and MUST
    NOT call ``conn.executescript`` (which would issue an implicit COMMIT
    and break this guarantee).

Public surface:

    Migration              -- frozen dataclass (version, name, up, down)
    MIGRATIONS             -- ordered list[Migration] (the registry)
    migrate_to_latest      -- apply all pending migrations atomically
    migrate_down_to        -- rollback to a target version (exclusive)
    applied_versions       -- read-only set[int] of applied versions
    pending                -- list[Migration] still to apply
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from parallax.migrations import (
    m0001_initial_schema,
    m0002_events_append_only,
    m0003_claim_metadata,
    m0004_events_user_time_index,
    m0005_claim_metadata_fk,
    m0006_events_session_id,
    m0007_claim_content_hash_user_id,
    m0008_normalize_naive_created_at,
    m0009_api_tokens,
    m0010_memory_cards,
    m0011_crosswalk,
    m0012_crosswalk_aphelion_doc_id,
    m0013_events_event_type_correlation_id_index,
    m0014_events_fts_trigram,
)
from parallax.sqlite_store import now_iso

__all__ = [
    "Migration",
    "MIGRATIONS",
    "MigrationStep",
    "MigrationPlan",
    "migrate_to_latest",
    "migrate_down_to",
    "migration_plan",
    "applied_versions",
    "pending",
    "ensure_schema_migrations_table",
]


@dataclasses.dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None]


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="initial_schema",
        up=m0001_initial_schema.up,
        down=m0001_initial_schema.down,
    ),
    Migration(
        version=2,
        name="events_append_only",
        up=m0002_events_append_only.up,
        down=m0002_events_append_only.down,
    ),
    Migration(
        version=3,
        name="claim_metadata",
        up=m0003_claim_metadata.up,
        down=m0003_claim_metadata.down,
    ),
    Migration(
        version=4,
        name="events_user_time_index",
        up=m0004_events_user_time_index.up,
        down=m0004_events_user_time_index.down,
    ),
    Migration(
        version=5,
        name="claim_metadata_fk",
        up=m0005_claim_metadata_fk.up,
        down=m0005_claim_metadata_fk.down,
    ),
    Migration(
        version=6,
        name="events_session_id",
        up=m0006_events_session_id.up,
        down=m0006_events_session_id.down,
    ),
    Migration(
        version=7,
        name="claim_content_hash_user_id",
        up=m0007_claim_content_hash_user_id.up,
        down=m0007_claim_content_hash_user_id.down,
    ),
    Migration(
        version=8,
        name="normalize_naive_created_at",
        up=m0008_normalize_naive_created_at.up,
        down=m0008_normalize_naive_created_at.down,
    ),
    Migration(
        version=9,
        name="api_tokens",
        up=m0009_api_tokens.up,
        down=m0009_api_tokens.down,
    ),
    Migration(
        version=10,
        name="memory_cards",
        up=m0010_memory_cards.up,
        down=m0010_memory_cards.down,
    ),
    Migration(
        version=11,
        name="crosswalk",
        up=m0011_crosswalk.up,
        down=m0011_crosswalk.down,
    ),
    Migration(
        version=12,
        name="crosswalk_aphelion_doc_id",
        up=m0012_crosswalk_aphelion_doc_id.up,
        down=m0012_crosswalk_aphelion_doc_id.down,
    ),
    Migration(
        version=13,
        name="events_event_type_correlation_id_index",
        up=m0013_events_event_type_correlation_id_index.up,
        down=m0013_events_event_type_correlation_id_index.down,
    ),
    Migration(
        version=14,
        name="events_fts_trigram",
        up=m0014_events_fts_trigram.up,
        down=m0014_events_fts_trigram.down,
    ),
]


def ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    """Create the ``schema_migrations`` ledger if missing."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TIMESTAMP NOT NULL
        )
        """)
    conn.commit()


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration versions already recorded as applied."""
    ensure_schema_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(r[0]) for r in rows}


def pending(conn: sqlite3.Connection) -> list[Migration]:
    """Return the unapplied migrations in ascending version order."""
    done = applied_versions(conn)
    return [m for m in sorted(MIGRATIONS, key=lambda m: m.version) if m.version not in done]


@contextmanager
def _manual_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block under an explicit BEGIN IMMEDIATE / COMMIT.

    Disables Python sqlite3's implicit transaction management for the
    duration so ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` are the
    only transaction boundaries in play. The previous isolation_level is
    restored on exit even if the block raises.
    """
    prev = conn.isolation_level
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # No transaction active (already rolled back by SQLite on error).
            pass
        raise
    finally:
        conn.isolation_level = prev


def migrate_to_latest(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration in version order, atomically.

    Returns the list of versions newly applied. Each migration's ``up()``
    runs inside an explicit ``BEGIN IMMEDIATE`` ... ``COMMIT`` together
    with the matching ``schema_migrations`` insert. A failure inside
    ``up()`` (or in the ledger insert) rolls the entire transaction back
    so the migration is neither half-applied nor recorded.
    """
    ensure_schema_migrations_table(conn)
    newly_applied: list[int] = []
    for mig in pending(conn):
        with _manual_tx(conn):
            mig.up(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (mig.version, mig.name, now_iso()),
            )
        newly_applied.append(mig.version)
    return newly_applied


@dataclasses.dataclass(frozen=True)
class MigrationStep:
    version: int
    name: str
    statements: tuple[str, ...]
    row_impact_estimates: dict[str, int]


@dataclasses.dataclass(frozen=True)
class MigrationPlan:
    applied: tuple[int, ...]
    pending: tuple[MigrationStep, ...]
    current_version: int | None
    target_version: int


_MIGRATION_MODULES: dict[int, object] = {
    1: m0001_initial_schema,
    2: m0002_events_append_only,
    3: m0003_claim_metadata,
    4: m0004_events_user_time_index,
    5: m0005_claim_metadata_fk,
    6: m0006_events_session_id,
    7: m0007_claim_content_hash_user_id,
    8: m0008_normalize_naive_created_at,
    9: m0009_api_tokens,
    10: m0010_memory_cards,
    11: m0011_crosswalk,
    12: m0012_crosswalk_aphelion_doc_id,
    13: m0013_events_event_type_correlation_id_index,
    14: m0014_events_fts_trigram,
}

# Matches table identifiers following the DDL/DML keywords we care about.
# Deliberately conservative: ignores quoted identifiers and schema prefixes —
# Parallax migrations use bare snake_case names throughout.
_TABLE_RE = re.compile(
    r"\b(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?"
    r"|DROP\s+TABLE(?:\s+IF\s+EXISTS)?"
    r"|ALTER\s+TABLE"
    r"|INSERT\s+INTO"
    r"|UPDATE"
    r"|FROM)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _extract_tables(statements: tuple[str, ...]) -> list[str]:
    seen: list[str] = []
    for stmt in statements:
        for m in _TABLE_RE.finditer(stmt):
            name = m.group(1)
            if name not in seen:
                seen.append(name)
    return seen


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    return int(row[0]) if row is not None else 0


def migration_plan(conn: sqlite3.Connection) -> MigrationPlan:
    """Return a non-destructive snapshot of the migration state.

    For every pending migration, enumerates the DDL/DML statements that
    would run and estimates row impact by running ``SELECT COUNT(*)``
    against every table the statements reference (tables that don't yet
    exist report 0). The function issues ONLY SELECTs — no BEGIN, no DDL,
    no writes — so it is safe to call against a live production DB and
    produces identical output on repeated calls.
    """
    ensure_schema_migrations_table(conn)
    applied = tuple(sorted(applied_versions(conn)))
    pending_migs = pending(conn)
    steps: list[MigrationStep] = []
    for mig in pending_migs:
        module = _MIGRATION_MODULES[mig.version]
        raw_stmts = tuple(getattr(module, "STATEMENTS", ()))
        tables = _extract_tables(raw_stmts)
        impact = {t: _row_count(conn, t) for t in tables}
        steps.append(
            MigrationStep(
                version=mig.version,
                name=mig.name,
                statements=raw_stmts,
                row_impact_estimates=impact,
            )
        )
    current = max(applied) if applied else None
    target = max(m.version for m in MIGRATIONS)
    return MigrationPlan(
        applied=applied,
        pending=tuple(steps),
        current_version=current,
        target_version=target,
    )


def migrate_down_to(conn: sqlite3.Connection, target_version: int) -> list[int]:
    """Roll back applied migrations whose version > ``target_version``.

    Iterates in descending version order. Each ``down()`` + ledger row
    delete runs in the same explicit transaction as ``migrate_to_latest``
    uses for ``up()``: both succeed or both roll back.
    """
    ensure_schema_migrations_table(conn)
    done = applied_versions(conn)
    by_version = {m.version: m for m in MIGRATIONS}
    reverted: list[int] = []
    for v in sorted((v for v in done if v > target_version), reverse=True):
        mig = by_version.get(v)
        if mig is None:
            raise RuntimeError(
                f"schema_migrations references unknown version {v}; "
                f"refusing to roll back without a registered Migration"
            )
        with _manual_tx(conn):
            mig.down(conn)
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (v,))
        reverted.append(v)
    return reverted
