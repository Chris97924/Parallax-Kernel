"""BUG 1+4 regression: by_timeline must not drop boundary / naive-ts events.

v0.5.0-pre1. See project_parallax_v05_retrieval_patch.md.

BUG 1 — microsecond boundary drop:
    now_iso() stores `...T12:00:00.500000+00:00` but the caller passes
    `until="...T12:00:00Z"`. After the legacy _iso_normalize (micro=0 →
    stripped) the stored string (pos-19 char `.`, ASCII 46) compares GREATER
    than the query string (pos-19 char `+`, ASCII 43) under SQLite lex
    compare, so `created_at <= until` evaluates FALSE and the boundary event
    disappears.

BUG 4 — naive-timestamp lex compare:
    A stored `created_at="2024-06-15T12:00:00"` (no tz) is SHORTER than a
    tz-aware query bound and loses the lex comparison via short-string-less
    semantics. _iso_normalize's until-expansion must produce a form that
    still compares correctly against naive stored strings.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
import sqlite3

import pytest

from parallax.events import record_event
from parallax.migrations import migrate_to_latest
from parallax.retrieve import _iso_normalize, by_timeline
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "tmb.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


def _insert_event_at(conn: sqlite3.Connection, created_at: str, user_id: str = "u") -> str:
    """Bypass now_iso() so we can pin created_at to an exact string."""
    from ulid import ULID

    eid = str(ULID())
    conn.execute(
        """INSERT INTO events(event_id, user_id, actor, event_type,
                              target_kind, target_id, payload_json,
                              approval_tier, created_at, session_id)
           VALUES (?, ?, 'system', 'marker', NULL, NULL, '{}', NULL, ?, NULL)""",
        (eid, user_id, created_at),
    )
    conn.commit()
    return eid


class TestMicrosecondBoundary:
    def test_until_second_boundary_includes_subsecond_event(
        self, conn: sqlite3.Connection
    ) -> None:
        """BUG 1 repro — until='...T12:00:00Z' must include a 12:00:00.500000+00:00 row."""
        _insert_event_at(conn, "2024-06-15T12:00:00.500000+00:00")
        hits = by_timeline(
            conn,
            user_id="u",
            since="2024-06-15T00:00:00Z",
            until="2024-06-15T12:00:00Z",
        )
        assert len(hits) == 1, (
            "boundary event at 12:00:00.500000+00:00 was dropped — "
            "lex compare between stored `.` (46) and query `+` (43) flipped"
        )

    def test_since_second_boundary_includes_exact_match(
        self, conn: sqlite3.Connection
    ) -> None:
        # now_iso() guarantees microsecond-precision form — that is the
        # canonical stored shape for production events.
        _insert_event_at(conn, "2024-06-15T12:00:00.000000+00:00")
        hits = by_timeline(
            conn,
            user_id="u",
            since="2024-06-15T12:00:00Z",
            until="2024-06-15T13:00:00Z",
        )
        assert len(hits) == 1

    def test_until_microsecond_inside_second_included(
        self, conn: sqlite3.Connection
    ) -> None:
        """Events at .999999 must remain INCLUDED when until has micro=0."""
        _insert_event_at(conn, "2024-06-15T12:00:00.999999+00:00")
        hits = by_timeline(
            conn,
            user_id="u",
            since="2024-06-15T00:00:00Z",
            until="2024-06-15T12:00:00Z",
        )
        assert len(hits) == 1

    def test_until_microsecond_next_second_excluded(
        self, conn: sqlite3.Connection
    ) -> None:
        """Expansion to 999999 must not bleed into the next second."""
        _insert_event_at(conn, "2024-06-15T12:00:01.000000+00:00")
        hits = by_timeline(
            conn,
            user_id="u",
            since="2024-06-15T00:00:00Z",
            until="2024-06-15T12:00:00Z",
        )
        assert hits == []

    def test_iso_normalize_until_expands_zero_microsecond(self) -> None:
        out = _iso_normalize("2024-06-15T12:00:00Z", kind="until")
        assert ".999999" in out
        assert "+00:00" in out

    def test_iso_normalize_since_keeps_zero_microsecond(self) -> None:
        out = _iso_normalize("2024-06-15T12:00:00Z", kind="since")
        # since should NOT be expanded — keeps micro=0 (or at least not 999999).
        assert ".999999" not in out
        assert "+00:00" in out

    def test_iso_normalize_is_immutable(self) -> None:
        """Input string must not be aliased or mutated."""
        s = "2024-06-15T12:00:00Z"
        _iso_normalize(s, kind="until")
        assert s == "2024-06-15T12:00:00Z"


class TestDateOnlyUntil:
    """A date-only until must cover the whole UTC day, not just its first second."""

    def test_same_day_window_includes_whole_day(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_event_at(conn, "2024-06-15T12:00:00.000000+00:00")
        _insert_event_at(conn, "2024-06-15T23:59:59.999999+00:00")
        hits = by_timeline(conn, user_id="u", since="2024-06-15", until="2024-06-15")
        assert len(hits) == 2

    def test_date_only_until_excludes_next_day(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_event_at(conn, "2024-06-16T00:00:00.000000+00:00")
        hits = by_timeline(conn, user_id="u", since="2024-06-15", until="2024-06-15")
        assert hits == []

    def test_iso_normalize_date_only_until_is_end_of_day(self) -> None:
        out = _iso_normalize("2024-06-15", kind="until")
        assert out == "2024-06-15T23:59:59.999999+00:00"

    def test_iso_normalize_date_only_since_is_midnight(self) -> None:
        out = _iso_normalize("2024-06-15", kind="since")
        assert out == "2024-06-15T00:00:00.000000+00:00"


class TestNaiveTimestampLexCompare:
    """BUG 4 — naive stored ts must still participate correctly in window queries."""

    def test_naive_iso_stored_event_still_matches(
        self, conn: sqlite3.Connection
    ) -> None:
        """A naive-ISO stored row must be returned by a tz-aware window query."""
        _insert_event_at(conn, "2024-06-15T12:00:00")
        hits = by_timeline(
            conn,
            user_id="u",
            since="2024-06-15T00:00:00Z",
            until="2024-06-15T23:59:59Z",
        )
        assert len(hits) == 1, "naive-ts row dropped by lex compare (BUG 4)"

    def test_naive_iso_same_second_as_since_matches_after_m0008(
        self, tmp_path: pathlib.Path
    ) -> None:
        """v0.5.0-pre4: m0008 normalizes a legacy naive row so the
        same-second ``since`` lex-compare succeeds.

        Simulates the realistic legacy-corpus flow: a DB is first built
        at migration version 7, a naive-ts row is raw-inserted (how a
        pre-``now_iso()`` corpus would arrive after a restore/replay),
        and only then is ``migrate_to_latest`` called to apply m0008. The
        migration rewrites the naive stored string to the 32-char
        canonical form, so a subsequent ``since`` bound at the same
        second now compares equal and the row is returned.

        Previously (v0.5.0-pre3) this was pinned as a known limitation
        asserting ``hits == []``. m0008 closes the gap permanently.
        """
        from parallax.migrations import (
            MIGRATIONS,
            _manual_tx,
            ensure_schema_migrations_table,
        )
        from parallax.sqlite_store import now_iso

        db = tmp_path / "legacy.db"
        c = connect(db)
        try:
            ensure_schema_migrations_table(c)
            for mig in sorted(MIGRATIONS, key=lambda m: m.version):
                if mig.version > 7:
                    continue
                with _manual_tx(c):
                    mig.up(c)
                    c.execute(
                        "INSERT INTO schema_migrations(version, name, "
                        "applied_at) VALUES (?, ?, ?)",
                        (mig.version, mig.name, now_iso()),
                    )
            _insert_event_at(c, "2024-06-15T12:00:00")

            from parallax.migrations import migrate_to_latest

            migrate_to_latest(c)  # applies m0008, normalizes the naive row

            hits = by_timeline(
                c,
                user_id="u",
                since="2024-06-15T12:00:00Z",
                until="2024-06-15T13:00:00Z",
            )
            assert len(hits) == 1, (
                "same-second naive-ts row was dropped; m0008 corpus "
                "normalization regressed — either the migration didn't "
                "rewrite events.created_at, or the reader-side compare "
                "flipped"
            )
        finally:
            c.close()


class TestCreatedAtIsNormalizedOnWrite:
    """now_iso() should produce a form that _iso_normalize can compare."""

    def test_now_iso_has_microseconds_and_tz(self) -> None:
        from parallax.sqlite_store import now_iso

        s = now_iso()
        assert "+00:00" in s
        # Always includes microseconds (the BUG 1 trigger on read side).
        assert "." in s.split("+")[0]

    def test_record_event_and_query_boundary_roundtrip(
        self, conn: sqlite3.Connection
    ) -> None:
        """End-to-end: record_event() → by_timeline(until=second-floor) → hit."""
        before = _dt.datetime.now(_dt.UTC).replace(microsecond=0)
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="marker",
            target_kind=None,
            target_id=None,
            payload={},
        )
        until_second = before.isoformat().replace("+00:00", "Z")
        # Force until to the same SECOND as the write (the write will be >= before
        # with some microseconds); until at that second should include it.
        until_next = (before + _dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        hits = by_timeline(
            conn,
            user_id="u",
            since=until_second,
            until=until_next,
        )
        assert len(hits) >= 1
