"""Tests for parallax.migrations — framework + 0001/0002/0003."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.migrations import (
    MIGRATIONS,
    Migration,
    applied_versions,
    migrate_down_to,
    migrate_to_latest,
    pending,
)
from parallax.sqlite_store import connect


@pytest.fixture()
def empty_conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "fresh.db"
    c = connect(db)
    yield c
    c.close()


class TestMigrationRegistry:
    def test_migrations_in_order(self) -> None:
        versions = [m.version for m in MIGRATIONS]
        names = [m.name for m in MIGRATIONS]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        assert names == [
            "initial_schema",
            "events_append_only",
            "claim_metadata",
            "events_user_time_index",
            "claim_metadata_fk",
            "events_session_id",
            "claim_content_hash_user_id",
            "normalize_naive_created_at",
            "api_tokens",
            "memory_cards",
            "crosswalk",
            "crosswalk_aphelion_doc_id",
            "events_event_type_correlation_id_index",
            "events_fts_trigram",
        ]

    def test_migration_is_frozen_dataclass(self) -> None:
        import dataclasses

        m = MIGRATIONS[0]
        assert isinstance(m, Migration)
        with pytest.raises(dataclasses.FrozenInstanceError):
            m.version = 99  # type: ignore[misc]


class TestMigrateToLatest:
    def test_fresh_db_applies_all(self, empty_conn: sqlite3.Connection) -> None:
        applied = migrate_to_latest(empty_conn)
        assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        assert applied_versions(empty_conn) == {
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
        }
        assert pending(empty_conn) == []

    def test_idempotent_rerun(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        again = migrate_to_latest(empty_conn)
        assert again == []

    def test_creates_expected_tables(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        rows = empty_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        names = {r[0] for r in rows}
        assert {
            "sources",
            "memories",
            "claims",
            "decisions",
            "events",
            "index_state",
            "schema_migrations",
            "claim_metadata",
        }.issubset(names)

    def test_records_applied_at(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        rows = empty_conn.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        for _, _, applied_at in rows:
            assert applied_at  # non-empty ISO timestamp


class TestMigrateDownTo:
    def test_down_to_zero_drops_user_tables(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        migrate_down_to(empty_conn, 0)
        rows = empty_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        names = {r[0] for r in rows}
        # schema_migrations remains as the meta-ledger
        assert names == {"schema_migrations"}
        assert applied_versions(empty_conn) == set()

    def test_round_trip_up_down_up(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        migrate_down_to(empty_conn, 0)
        applied = migrate_to_latest(empty_conn)
        assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]

    def test_down_to_one_keeps_initial_schema(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        migrate_down_to(empty_conn, 1)
        names = {
            r[0]
            for r in empty_conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "events" in names
        assert "claim_metadata" not in names
        triggers = {
            r[0]
            for r in empty_conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert "events_no_update" not in triggers
        assert "events_no_delete" not in triggers


class TestEventsAppendOnlyTrigger:
    def _seed_event(self, conn: sqlite3.Connection) -> str:
        eid = "evt-1"
        conn.execute(
            """INSERT INTO events(event_id, user_id, actor, event_type,
                   target_kind, target_id, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, "u", "system", "test.event", None, None, "{}", "2026-04-18T00:00:00Z"),
        )
        conn.commit()
        return eid

    def test_insert_works(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        eid = self._seed_event(empty_conn)
        row = empty_conn.execute(
            "SELECT event_id FROM events WHERE event_id = ?", (eid,)
        ).fetchone()
        assert row[0] == eid

    def test_update_blocked(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        eid = self._seed_event(empty_conn)
        with pytest.raises(sqlite3.IntegrityError, match="events are append-only"):
            empty_conn.execute(
                "UPDATE events SET event_type = 'mutated' WHERE event_id = ?", (eid,)
            )

    def test_delete_blocked(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        eid = self._seed_event(empty_conn)
        with pytest.raises(sqlite3.IntegrityError, match="events are append-only"):
            empty_conn.execute("DELETE FROM events WHERE event_id = ?", (eid,))


class TestClaimMetadataTable:
    def test_columns_present(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        rows = empty_conn.execute("PRAGMA table_info(claim_metadata)").fetchall()
        cols = {r[1] for r in rows}
        assert {
            "claim_id",
            "reaffirm_count",
            "last_seen_at",
            "superseded_by",
            "superseded_at",
            "created_at",
            "updated_at",
        }.issubset(cols)

    def test_fk_to_claims_enforced(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        # PRAGMA foreign_keys = ON is set by parallax.sqlite_store.connect
        with pytest.raises(sqlite3.IntegrityError):
            empty_conn.execute(
                """INSERT INTO claim_metadata(claim_id, last_seen_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    "does-not-exist",
                    "2026-04-18T00:00:00Z",
                    "2026-04-18T00:00:00Z",
                    "2026-04-18T00:00:00Z",
                ),
            )
            empty_conn.commit()

    def test_down_drops_table(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        migrate_down_to(empty_conn, 2)
        rows = empty_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'claim_metadata'"
        ).fetchall()
        assert rows == []


class TestAtomicityFix01:
    """FIX-01 — up() failure rolls back BOTH the ledger insert AND the DDL."""

    def test_up_failure_rolls_back_ledger_and_ddl(self, empty_conn: sqlite3.Connection) -> None:
        # Apply 0001 first so the events table exists; we will register a
        # bogus migration whose up() partially executes a CREATE TABLE then
        # raises. The framework must roll BOTH the ledger row AND the
        # CREATE TABLE back.
        migrate_to_latest(empty_conn)
        baseline = applied_versions(empty_conn)

        def bad_up(c: sqlite3.Connection) -> None:
            c.execute("CREATE TABLE atomicity_probe (id TEXT PRIMARY KEY)")
            raise RuntimeError("intentional mid-up failure")

        bogus = Migration(version=999, name="bogus", up=bad_up, down=lambda _c: None)
        MIGRATIONS.append(bogus)
        try:
            with pytest.raises(RuntimeError, match="intentional mid-up failure"):
                migrate_to_latest(empty_conn)
            # Ledger unchanged: 999 not recorded.
            assert applied_versions(empty_conn) == baseline
            # DDL rolled back: probe table not present.
            tables = {
                r[0]
                for r in empty_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "atomicity_probe" not in tables
        finally:
            MIGRATIONS.remove(bogus)

    def test_no_executescript_in_migration_modules(self) -> None:
        """Statement-list pattern is mandatory; executescript breaks atomicity."""
        import pathlib

        migrations_dir = pathlib.Path(__file__).resolve().parent.parent / "parallax" / "migrations"
        for path in migrations_dir.glob("m*.py"):
            text = path.read_text(encoding="utf-8")
            assert "executescript" not in text, f"{path.name} must not call executescript"


class TestEventsUserTimeIndexFix03:
    """FIX-03 — events(user_id, created_at) covering index for watermark scans."""

    def test_index_exists(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        idxs = {
            r[0]
            for r in empty_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
            ).fetchall()
        }
        assert "idx_events_user_time" in idxs

    def test_query_planner_uses_index(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        plan = empty_conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT event_id FROM events WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            ("u1",),
        ).fetchall()
        plan_text = " ".join(str(row[3]) for row in plan)
        assert "idx_events_user_time" in plan_text

    def test_down_drops_index(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        migrate_down_to(empty_conn, 3)
        idxs = {
            r[0]
            for r in empty_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
            ).fetchall()
        }
        assert "idx_events_user_time" not in idxs


class TestClaimMetadataV5Fix02:
    """FIX-02 — claim_metadata recreated with ON DELETE SET NULL + self-cycle CHECK."""

    def _seed_claim(self, conn: sqlite3.Connection, claim_id: str) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO sources(source_id, uri, kind, content_hash,
                   user_id, ingested_at, state)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("src1", "x://1", "chat", "h", "u", "2026-04-18T00:00:00Z", "ingested"),
        )
        conn.execute(
            """INSERT INTO claims(claim_id, user_id, subject, predicate, object,
                   source_id, content_hash, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim_id,
                "u",
                "s",
                "p",
                "o",
                "src1",
                f"h-{claim_id}",
                "auto",
                "2026-04-18T00:00:00Z",
                "2026-04-18T00:00:00Z",
            ),
        )

    def _insert_metadata(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: str,
        superseded_by: str | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO claim_metadata(claim_id, last_seen_at, superseded_by,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                claim_id,
                "2026-04-18T00:00:00Z",
                superseded_by,
                "2026-04-18T00:00:00Z",
                "2026-04-18T00:00:00Z",
            ),
        )

    def test_self_supersession_check_blocks_insert(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        self._seed_claim(empty_conn, "c1")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            self._insert_metadata(empty_conn, claim_id="c1", superseded_by="c1")

    def test_on_delete_set_null_clears_superseded_by(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        self._seed_claim(empty_conn, "c1")
        self._seed_claim(empty_conn, "c2")
        self._insert_metadata(empty_conn, claim_id="c1", superseded_by="c2")
        empty_conn.commit()
        # Remove the predecessor's metadata row first so its FK to c1 doesn't
        # block deletion of c1, then drop c1 itself. The remaining metadata
        # row still references c2 via superseded_by — that's what we test.
        empty_conn.execute("DELETE FROM claim_metadata WHERE claim_id = ?", ("c1",))
        # Now make a fresh metadata row keyed on c2 that points at... no, easier:
        # we want to delete c2 (the successor) and assert c1's metadata pointer
        # becomes NULL. Re-insert the c1 metadata pointing at c2.
        self._insert_metadata(empty_conn, claim_id="c1", superseded_by="c2")
        empty_conn.commit()
        empty_conn.execute("DELETE FROM claims WHERE claim_id = ?", ("c2",))
        empty_conn.commit()
        row = empty_conn.execute(
            "SELECT superseded_by FROM claim_metadata WHERE claim_id = ?", ("c1",)
        ).fetchone()
        assert row[0] is None

    def test_data_preserved_across_v5_swap(self, empty_conn: sqlite3.Connection) -> None:
        # Apply through 4, seed a row, then apply 5 manually and verify the
        # row survives the table swap.
        migrate_to_latest(empty_conn)
        # Roll back to 4 to get the pre-FIX-02 schema.
        migrate_down_to(empty_conn, 4)
        self._seed_claim(empty_conn, "cA")
        self._insert_metadata(empty_conn, claim_id="cA", superseded_by=None)
        empty_conn.commit()

        # Apply 0005 directly via the framework (re-up).
        migrate_to_latest(empty_conn)

        rows = empty_conn.execute("SELECT claim_id, reaffirm_count FROM claim_metadata").fetchall()
        assert (rows[0][0], rows[0][1]) == ("cA", 0)

    def test_down_then_up_round_trip(self, empty_conn: sqlite3.Connection) -> None:
        migrate_to_latest(empty_conn)
        migrate_down_to(empty_conn, 4)  # undo 5
        migrate_to_latest(empty_conn)
        # CHECK is back in place
        self._seed_claim(empty_conn, "c1")
        with pytest.raises(sqlite3.IntegrityError):
            self._insert_metadata(empty_conn, claim_id="c1", superseded_by="c1")


# ---------------------------------------------------------------------------
# MED-MIGRATION-COMMIT — m0013 persists through the runner pattern
# ---------------------------------------------------------------------------


class TestM0013Persistence:
    """Story MED-MIGRATION-COMMIT — verify the m0013 index survives a
    process restart via the standard ``migrate_to_latest`` runner.

    The framework wraps each ``up()`` in an explicit ``BEGIN IMMEDIATE
    ... COMMIT`` (parallax.migrations._manual_tx); the m0013 ``up()``
    issues only ``CREATE INDEX IF NOT EXISTS`` so the index ought to be
    durable.  This test pins that contract."""

    def test_m0013_persistence_under_runner_pattern(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "m0013_persistence.db"
        # First connection: apply migrations.
        c1 = connect(db)
        migrate_to_latest(c1)
        idx_before = c1.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='events' AND name='idx_events_event_type_target_id'"
        ).fetchone()
        assert idx_before is not None
        c1.close()

        # Second connection: the index must still exist (migrations were
        # committed).
        c2 = connect(db)
        try:
            idx_after = c2.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='events' AND name='idx_events_event_type_target_id'"
            ).fetchone()
            assert idx_after is not None, (
                "m0013 index lost across connection boundary — runner did "
                "not commit the up() pass"
            )
            # Idempotent: re-running migrate_to_latest is a no-op.
            applied = migrate_to_latest(c2)
            assert applied == [], f"unexpected re-apply: {applied}"
        finally:
            c2.close()
