"""Migration 0014 — ``events_fts`` FTS5 trigram index for payload substring search.

The v0.3.0 retrieval hot paths (:func:`parallax.retrieve.by_file`,
:func:`~parallax.retrieve.by_bug_fix`, :func:`~parallax.retrieve.by_entity`)
match against ``events.payload_json`` with a leading-wildcard
``LIKE '%needle%'`` scan. A leading wildcard cannot use a B-tree index, so the
best the planner can do is walk every one of that user's event rows via
``idx_events_user_time`` and run ``LIKE`` on each — cost ``O(user's events)``
per query.

This migration adds a standalone FTS5 virtual table over ``payload_json`` using
the **trigram** tokenizer. Trigram phrase matching (``MATCH '"needle"'``) has the
same substring semantics as ``LIKE '%needle%'`` (case-insensitive, literal ``%``
/ ``_``), but is served from the trigram index instead of a full-partition scan.
``event_id`` is carried as an ``UNINDEXED`` column so a matched FTS row joins
back to ``events`` by primary key.

User scope (multi-tenant):
    A searchable ``user_tag`` column carries ``char(2) || user_id || char(2)``
    so the retrieval hot paths can restrict the FTS ``MATCH`` to one user's rows
    (``user_tag:"<tag>" AND payload_json:"<needle>"``) instead of matching a
    common term (``bug`` / ``fix``) across every tenant and discarding the other
    users after a primary-key join. The bounding STX (``0x02``) sentinels — a
    byte that does not occur in Parallax user_ids — serve two jobs: they keep
    the tag ``>= 3`` chars so short ids like ``'u'`` still yield a trigram, and
    they stop one id from matching as a substring of another (a phrase query for
    user ``'u'`` must not leak ``'u2'`` rows, nor ``'abc'`` leak ``'abcd'``).

Sync strategy:
    ``events`` is append-only. m0002 installs ``BEFORE UPDATE`` / ``BEFORE
    DELETE`` triggers that ``RAISE(ABORT)``, and :mod:`parallax.sqlite_store`
    exposes no ``update_event`` / ``delete_event``. There is therefore no
    UPDATE or DELETE path to mirror — a single ``AFTER INSERT`` trigger keeps
    ``events_fts`` in lock-step with every row appended to ``events``.

Idempotency:
    ``CREATE VIRTUAL TABLE IF NOT EXISTS`` / ``CREATE TRIGGER IF NOT EXISTS``
    are no-ops when already present, and the one-time backfill is guarded on
    ``events_fts`` being empty, so a bare re-run of ``up()`` cannot
    double-index. The ``schema_migrations`` ledger prevents re-application
    regardless.

Reversibility:
    ``down`` drops the trigger then the virtual table (which also drops its
    FTS5 shadow tables) — no ``events`` row data is touched.
"""

from __future__ import annotations

import sqlite3

_CREATE_FTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5("
    "event_id UNINDEXED, user_tag, payload_json, tokenize='trigram')"
)

# ``char(2)`` emits the STX sentinel byte; keep this encoding identical to
# ``parallax.retrieve._fts_user_tag`` so query-side tags match stored tags.
_BACKFILL = (
    "INSERT INTO events_fts(event_id, user_tag, payload_json) "
    "SELECT event_id, char(2) || user_id || char(2), payload_json FROM events"
)

_CREATE_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS events_ai_fts AFTER INSERT ON events "
    "BEGIN INSERT INTO events_fts(event_id, user_tag, payload_json) "
    "VALUES (new.event_id, char(2) || new.user_id || char(2), new.payload_json); "
    "END"
)

# Representative DDL/DML for the static ``migration_plan`` estimator. The real
# apply path is ``up()`` because the backfill is guarded on emptiness; these
# strings let the planner attribute row impact to the ``events`` table.
STATEMENTS: list[str] = [_CREATE_FTS, _BACKFILL, _CREATE_TRIGGER]

DOWN_STATEMENTS: list[str] = [
    "DROP TRIGGER IF EXISTS events_ai_fts",
    "DROP TABLE IF EXISTS events_fts",
]


def up(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_FTS)
    # Backfill exactly once. Guarded so re-invoking up() outside the version
    # ledger does not append duplicate FTS rows.
    already = conn.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
    if already == 0:
        conn.execute(_BACKFILL)
    conn.execute(_CREATE_TRIGGER)


def down(conn: sqlite3.Connection) -> None:
    for stmt in DOWN_STATEMENTS:
        conn.execute(stmt)
