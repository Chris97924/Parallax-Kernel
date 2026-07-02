"""Tests for parallax.migrations.migration_plan — non-destructive dry-run."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import subprocess
import sys

from parallax.migrations import (
    MIGRATIONS,
    MigrationPlan,
    MigrationStep,
    applied_versions,
    ensure_schema_migrations_table,
    migrate_to_latest,
    migration_plan,
)
from parallax.sqlite_store import connect


def _fresh_conn(tmp_path: pathlib.Path, name: str = "mig.db") -> sqlite3.Connection:
    return connect(tmp_path / name)


class TestMigrationPlanShape:
    def test_fresh_db_reports_all_pending(self, tmp_path: pathlib.Path) -> None:
        c = _fresh_conn(tmp_path, "fresh.db")
        plan = migration_plan(c)
        assert isinstance(plan, MigrationPlan)
        assert plan.applied == ()
        assert plan.current_version is None
        assert plan.target_version == max(m.version for m in MIGRATIONS)
        pending_versions = [s.version for s in plan.pending]
        assert pending_versions == sorted(m.version for m in MIGRATIONS)
        for step in plan.pending:
            assert isinstance(step, MigrationStep)
            assert isinstance(step.statements, tuple)
            assert isinstance(step.row_impact_estimates, dict)
        c.close()

    def test_fully_migrated_db_reports_empty_pending(self, tmp_path: pathlib.Path) -> None:
        c = _fresh_conn(tmp_path, "full.db")
        migrate_to_latest(c)
        plan = migration_plan(c)
        assert plan.applied == tuple(sorted(m.version for m in MIGRATIONS))
        assert plan.pending == ()
        assert plan.current_version == plan.target_version
        c.close()

    def test_partially_migrated_db(self, tmp_path: pathlib.Path) -> None:
        c = _fresh_conn(tmp_path, "part.db")
        ensure_schema_migrations_table(c)
        # Manually apply only versions 1..3 via migrate_to_latest on a
        # registry subset: easier path is to run all then hand-delete ledger
        # rows for 4..6. But that leaves real DDL in place. Instead: apply
        # v1..3 by running their up() manually in a txn.
        from parallax.migrations import _manual_tx
        from parallax.sqlite_store import now_iso

        for mig in MIGRATIONS[:3]:
            with _manual_tx(c):
                mig.up(c)
                c.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) " "VALUES (?, ?, ?)",
                    (mig.version, mig.name, now_iso()),
                )
        plan = migration_plan(c)
        assert plan.applied == (1, 2, 3)
        assert [s.version for s in plan.pending] == [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        assert plan.current_version == 3
        c.close()


class TestRowImpactEstimates:
    def test_all_counts_non_negative_ints(self, tmp_path: pathlib.Path) -> None:
        c = _fresh_conn(tmp_path, "impact.db")
        migrate_to_latest(c)
        # Populate something so at least one table has rows.
        c.execute(
            "INSERT INTO sources(source_id, uri, kind, content_hash, user_id, "
            "ingested_at, state) VALUES "
            "('s1', 'file://x', 'file', 'h', 'u', datetime('now'), 'ingested')"
        )
        c.commit()
        # A fresh plan on a fully-migrated DB has zero pending — roll back
        # v7 so we have one pending migration to inspect.
        from parallax.migrations import migrate_down_to

        migrate_down_to(c, 7)
        plan = migration_plan(c)
        # After rolling back to v7, migrations 8..14 are pending.
        assert len(plan.pending) == 7
        for step in plan.pending:
            for table, count in step.row_impact_estimates.items():
                assert isinstance(count, int)
                assert count >= 0
                assert isinstance(table, str) and table
        c.close()


class TestNonDestructive:
    def test_migration_plan_has_no_side_effects(self, tmp_path: pathlib.Path) -> None:
        c = _fresh_conn(tmp_path, "side.db")
        migrate_to_latest(c)
        from parallax.migrations import migrate_down_to

        migrate_down_to(c, 4)

        def _snapshot() -> tuple:
            return (
                tuple(sorted(applied_versions(c))),
                c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
            )

        before = _snapshot()
        plan_a = migration_plan(c)
        plan_b = migration_plan(c)
        after = _snapshot()

        assert before == after
        assert plan_a == plan_b


class TestCliDryRun:
    def _env_with_db(self, db: pathlib.Path, vault: pathlib.Path) -> dict:
        import os

        env = os.environ.copy()
        env["PARALLAX_DB_PATH"] = str(db)
        env["PARALLAX_VAULT_PATH"] = str(vault)
        return env

    def test_cli_migrate_dry_run_success(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "cli.db"
        vault = tmp_path / "vault"
        vault.mkdir()
        env = self._env_with_db(db, vault)
        proc = subprocess.run(
            [sys.executable, "-m", "parallax.cli", "inspect", "migrate", "--dry-run"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert "pending" in proc.stdout

    def test_cli_migrate_json_emits_parseable_plan(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "cli-json.db"
        vault = tmp_path / "vault"
        vault.mkdir()
        env = self._env_with_db(db, vault)
        proc = subprocess.run(
            [sys.executable, "-m", "parallax.cli", "inspect", "migrate", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert set(payload) >= {
            "applied",
            "pending",
            "current_version",
            "target_version",
        }
        assert isinstance(payload["pending"], list)
