"""Unit + integration tests for parallax.apex.audit_db.

Spec ground truth: ``docs/m5-prep/audit-db-path-config.md`` §3-§6.

Coverage map:

* path resolver: env override / default / empty-env strict reject
* startup gates: relative path / ``..`` segments / missing parent /
  non-dir parent / quick_check budget enforced via progress handler
* schema bootstrap: 12 columns + UNIQUE on envelope_message_id +
  required indexes / no outcome index / idempotent re-open /
  schema_version mismatch refused
* enum drift: DDL CHECK literals derived from
  ``audit_writer.OUTCOME_VALUES`` / ``SOURCE_VALUES``
* writer: required-only round trip / divergence / error / UNIQUE
  collision / outcome+source CHECK / NOT NULL / canonicalize_row
  bypass rejected
* smoke: full Envelope chain + golden sha256 vector
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Any, Final

import pytest

from parallax.apex import audit_db as audit_db_mod
from parallax.apex.audit_db import (
    CURRENT_SCHEMA_VERSION,
    ENV_VAR_NAME,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    AuditDbConfigError,
    AuditDbUsageError,
    AuditDbWriteError,
    open_audit_db,
    resolve_audit_db_path,
    write_audit_row,
    write_audit_rows_atomic,
)
from parallax.apex.audit_writer import (
    OUTCOME_VALUES,
    SOURCE_VALUES,
    AuditRow,
    AuditRowValidationError,
    canonicalize_row,
)
from parallax.apex.canonical_json import canonical_dumps, sha256_hex
from parallax.apex.envelope import (
    ENVELOPE_VERSION_LITERAL,
    PayloadType,
    Source,
    compute_checksum,
    parse_envelope,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Disposable audit-db path inside a writable tmp_path."""
    return tmp_path / "audit.db"


@pytest.fixture()
def conn(audit_db_path: pathlib.Path):
    c = open_audit_db(audit_db_path)
    try:
        yield c
    finally:
        c.close()


def _valid_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_id": "0193e2b1-0001-7000-8000-000000000001",
        "envelope_message_id": "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc",
        "outcome": "hit",
        "package_id": "0193ef00-0001-7000-8000-000000000005",
        "session_id": "sess-2026-05-09-001",
        "signer_id": "chris@aphelion-graph",
        "signer_manifest_digest": "a" * 64,
        "source": "aphelion",
        "ts": "2026-05-09T14:23:11Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# §3 path resolution
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_env_var_overrides_default(self) -> None:
        custom = pathlib.Path("/tmp/custom/audit.db")
        result = resolve_audit_db_path(env={ENV_VAR_NAME: str(custom)})
        assert result == custom

    def test_unset_env_rejected(self) -> None:
        """Unset env raises EX_CONFIG — no hardcoded operator-specific
        default is baked into the package."""
        with pytest.raises(AuditDbConfigError, match="is not set"):
            resolve_audit_db_path(env={})

    def test_empty_env_var_rejected(self) -> None:
        with pytest.raises(AuditDbConfigError, match="empty string"):
            resolve_audit_db_path(env={ENV_VAR_NAME: ""})

    def test_no_chris_specific_paths_in_module(self) -> None:
        """Ensure no operator-specific paths leaked back into the module."""
        import inspect

        src = inspect.getsource(audit_db_mod)
        for forbidden in (r"E:\Parallax", "/home/chris", r"E:\\Parallax"):
            assert forbidden not in src, (
                f"hardcoded operator-specific path leaked into audit_db.py: {forbidden!r}"
            )


# ---------------------------------------------------------------------------
# §4 startup validation gates
# ---------------------------------------------------------------------------


class TestStartupGates:
    def test_relative_path_rejected(self) -> None:
        with pytest.raises(AuditDbConfigError, match="must be absolute"):
            open_audit_db("relative/audit.db")

    def test_dotdot_segment_rejected(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / ".." / "audit.db"
        with pytest.raises(AuditDbConfigError, match=r"'\.\.' segments"):
            open_audit_db(bad)

    def test_missing_parent_dir_rejected(self, tmp_path: pathlib.Path) -> None:
        missing = tmp_path / "no-such-dir" / "audit.db"
        with pytest.raises(
            AuditDbConfigError, match="parent directory does not exist"
        ):
            open_audit_db(missing)

    def test_parent_is_file_rejected(self, tmp_path: pathlib.Path) -> None:
        fake_parent = tmp_path / "i_am_a_file"
        fake_parent.write_text("not a dir", encoding="utf-8")
        bogus = fake_parent / "audit.db"
        with pytest.raises(AuditDbConfigError, match="parent"):
            open_audit_db(bogus)

    def test_quick_check_budget_enforced_post_hoc_fallback(
        self, audit_db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §4 item 5 — empty-DB quick_check completes before the progress
        handler is even invoked, so the post-hoc elapsed check is the
        sole guard. Verified by asserting the message wording specific
        to the post-hoc arm (``took X.Ys > budget``).
        """
        bootstrap = open_audit_db(audit_db_path)
        bootstrap.close()
        monkeypatch.setattr(audit_db_mod, "QUICK_CHECK_BUDGET_SECONDS", -1.0)
        with pytest.raises(
            AuditDbConfigError, match=r"EX_AUDIT_DB_SLOW_QUICKCHECK: quick_check took"
        ):
            open_audit_db(audit_db_path)

    def test_quick_check_budget_enforced_via_progress_handler_abort(
        self, audit_db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the progress handler is invoked DURING quick_check and
        returns 1, SQLite aborts the PRAGMA and the first error arm
        (``aborted via progress handler``) fires — distinct from the
        post-hoc elapsed check path. We force the handler to fire on the
        very first VM op by setting the interval to 1.
        """
        bootstrap = open_audit_db(audit_db_path)
        bootstrap.close()
        monkeypatch.setattr(audit_db_mod, "_PROGRESS_HANDLER_INTERVAL_OPS", 1)
        monkeypatch.setattr(audit_db_mod, "QUICK_CHECK_BUDGET_SECONDS", -1.0)
        # The first-arm message wording is unique — it can ONLY come
        # from the handler-abort path. If quick_check completed without
        # abort, the post-hoc arm would produce "took X.Ys > -1s budget"
        # instead, which does not match this regex.
        with pytest.raises(
            AuditDbConfigError,
            match=r"aborted via progress handler",
        ):
            open_audit_db(audit_db_path)


# ---------------------------------------------------------------------------
# §6 schema bootstrap
# ---------------------------------------------------------------------------


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: dict(row) for row in rows}


class TestSchemaBootstrap:
    def test_all_required_and_optional_columns_present(
        self, conn: sqlite3.Connection
    ) -> None:
        cols = _table_columns(conn, "audit_row")
        for c in REQUIRED_COLUMNS:
            assert c in cols, f"missing required column {c!r}"
            assert cols[c]["notnull"] == 1, f"{c!r} should be NOT NULL"
        for c in OPTIONAL_COLUMNS:
            assert c in cols, f"missing optional column {c!r}"
            assert cols[c]["notnull"] == 0, f"{c!r} should be nullable"

    def test_envelope_message_id_has_unique_constraint(
        self, conn: sqlite3.Connection
    ) -> None:
        # SQLite auto-creates an index for each UNIQUE constraint.
        rows = conn.execute("PRAGMA index_list(audit_row)").fetchall()
        unique_idxs = [r for r in rows if r["unique"] == 1]
        # Find the index that covers envelope_message_id.
        emid_unique_seen = False
        for idx in unique_idxs:
            info = conn.execute(
                f"PRAGMA index_info({idx['name']})"
            ).fetchall()
            cols = [r["name"] for r in info]
            if cols == ["envelope_message_id"]:
                emid_unique_seen = True
                break
        assert emid_unique_seen, (
            f"envelope_message_id missing UNIQUE constraint; "
            f"unique indexes seen: {[i['name'] for i in unique_idxs]}"
        )

    def test_integer_primary_key_present(self, conn: sqlite3.Connection) -> None:
        cols = _table_columns(conn, "audit_row")
        pk_cols = [name for name, info in cols.items() if info["pk"] >= 1]
        assert pk_cols == ["id"], f"expected ['id'] PK, got {pk_cols}"
        # rowid alias — type INTEGER, no AUTOINCREMENT extra table.
        assert cols["id"]["type"].upper() == "INTEGER"

    def test_required_secondary_indexes_present(
        self, conn: sqlite3.Connection
    ) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_row'"
        ).fetchall()
        names = {row["name"] for row in rows}
        assert "idx_audit_row_ts" in names
        assert "idx_audit_row_session_id" in names

    def test_no_outcome_index(self, conn: sqlite3.Connection) -> None:
        """4-value enum is too low-cardinality to benefit from an index."""
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_row'"
        ).fetchall()
        names = {row["name"] for row in rows}
        assert "idx_audit_row_outcome" not in names

    def test_schema_version_table_populated(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT MAX(version) FROM audit_db_schema_version"
        ).fetchone()
        assert rows[0] == CURRENT_SCHEMA_VERSION

    def test_bootstrap_idempotent(self, audit_db_path: pathlib.Path) -> None:
        c1 = open_audit_db(audit_db_path)
        c1.close()
        c2 = open_audit_db(audit_db_path)
        try:
            (n,) = c2.execute(
                "SELECT COUNT(*) FROM audit_db_schema_version"
            ).fetchone()
            assert n == 1  # INSERT OR IGNORE prevents duplicate v1 row
        finally:
            c2.close()

    def test_wal_journal_mode_active(self, conn: sqlite3.Connection) -> None:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_busy_timeout_set(self, conn: sqlite3.Connection) -> None:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000

    def test_wal_autocheckpoint_bounded(self, conn: sqlite3.Connection) -> None:
        n = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        assert n == 200

    def test_schema_version_mismatch_refused(
        self, audit_db_path: pathlib.Path
    ) -> None:
        """Stale code opening a future DB version must EX_CONFIG out."""
        # Bootstrap normally.
        c = open_audit_db(audit_db_path)
        # Inject a future version row, then close + reopen.
        c.execute(
            "INSERT INTO audit_db_schema_version (version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION + 1,),
        )
        c.close()
        with pytest.raises(
            AuditDbConfigError, match="schema_version mismatch"
        ):
            open_audit_db(audit_db_path)

    def test_schema_version_null_value_rejected(
        self, audit_db_path: pathlib.Path
    ) -> None:
        """``MAX(version)`` returning NULL (empty table) raises
        EX_CONFIG instead of silently passing. Simulates the race where
        the version row is missing after bootstrap (or was wiped)."""
        c = open_audit_db(audit_db_path)
        c.execute("DELETE FROM audit_db_schema_version")
        c.close()
        with pytest.raises(AuditDbConfigError, match="schema_version table is empty"):
            open_audit_db(audit_db_path)

    def test_apply_schema_mid_ddl_failure_preserves_original_exception(
        self,
        audit_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When a schema DDL statement raises mid-transaction, the inner
        ROLLBACK runs (best-effort) AND leaves the DB clean — the broken
        statement's side effects are reverted.
        """
        # Inject a broken DDL into _SCHEMA_STATEMENTS that will raise
        # sqlite3.OperationalError when executed.
        broken = (
            *audit_db_mod._SCHEMA_STATEMENTS,
            "CREATE TABLE __broken__ (this is not valid SQL",
        )
        monkeypatch.setattr(audit_db_mod, "_SCHEMA_STATEMENTS", broken)
        with pytest.raises(sqlite3.OperationalError):
            open_audit_db(audit_db_path)
        # Prove ROLLBACK actually ran by reopening the DB without the
        # broken DDL and asserting it succeeds cleanly + the
        # half-applied table is absent. If ROLLBACK had been a no-op
        # the partial schema would persist and either the second open
        # would fail or the __broken__ table would be present.
        monkeypatch.undo()
        c = open_audit_db(audit_db_path)
        try:
            tables = {
                r["name"]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "audit_row" in tables, "fresh open must succeed after rollback"
            assert "__broken__" not in tables, (
                "broken DDL state leaked past rollback — ROLLBACK did not run"
            )
        finally:
            c.close()

    def test_write_probe_rollback_failure_raises_ex_config(
        self,
        audit_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the BEGIN IMMEDIATE in the write probe succeeds but
        the subsequent ROLLBACK raises, the failure surfaces as
        :class:`AuditDbConfigError` (EX_CONFIG) not a raw
        :class:`sqlite3.OperationalError`. Spec §4 item 8 path."""
        # Pre-bootstrap so the second open hits the gates.
        bootstrap = open_audit_db(audit_db_path)
        bootstrap.close()

        # Wrap _write_probe via a fake-conn substitution: capture the
        # real probe code path with a fake conn where BEGIN succeeds
        # but ROLLBACK raises.
        original_probe = audit_db_mod._write_probe

        class _ProbeFakeConn:
            def execute(self, sql: str, params: Any = ()) -> Any:
                normalized = sql.strip().upper()
                if normalized.startswith("BEGIN"):
                    return None
                if normalized.startswith("ROLLBACK"):
                    raise sqlite3.OperationalError("sentinel rollback fail")
                raise AssertionError(f"unexpected sql: {sql!r}")

        def wrapped(real_conn: sqlite3.Connection) -> None:
            original_probe(_ProbeFakeConn())  # type: ignore[arg-type]

        monkeypatch.setattr(audit_db_mod, "_write_probe", wrapped)
        with pytest.raises(
            AuditDbConfigError, match="write probe rollback failed"
        ):
            open_audit_db(audit_db_path)

    def test_write_probe_begin_database_error_raises_ex_config(
        self,
        audit_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BEGIN IMMEDIATE raising a non-Operational :class:`sqlite3.Error`
        subclass (e.g. :class:`sqlite3.DatabaseError` on a malformed file)
        must still translate into :class:`AuditDbConfigError` so EX_CONFIG
        signaling is consistent — callers that only catch
        ``AuditDbConfigError`` cannot miss this path.
        """
        bootstrap = open_audit_db(audit_db_path)
        bootstrap.close()

        original_probe = audit_db_mod._write_probe

        class _ProbeFakeConn:
            def execute(self, sql: str, params: Any = ()) -> Any:
                normalized = sql.strip().upper()
                if normalized.startswith("BEGIN"):
                    raise sqlite3.DatabaseError("sentinel malformed db")
                raise AssertionError(f"unexpected sql: {sql!r}")

        def wrapped(real_conn: sqlite3.Connection) -> None:
            original_probe(_ProbeFakeConn())  # type: ignore[arg-type]

        monkeypatch.setattr(audit_db_mod, "_write_probe", wrapped)
        with pytest.raises(AuditDbConfigError, match="write probe failed"):
            open_audit_db(audit_db_path)

    def test_write_probe_rollback_database_error_raises_ex_config(
        self,
        audit_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ROLLBACK raising :class:`sqlite3.DatabaseError` (not
        :class:`OperationalError`) must still translate into
        :class:`AuditDbConfigError` — verifies the rollback ``except``
        widening to :class:`sqlite3.Error`.
        """
        bootstrap = open_audit_db(audit_db_path)
        bootstrap.close()

        original_probe = audit_db_mod._write_probe

        class _ProbeFakeConn:
            def execute(self, sql: str, params: Any = ()) -> Any:
                normalized = sql.strip().upper()
                if normalized.startswith("BEGIN"):
                    return None
                if normalized.startswith("ROLLBACK"):
                    raise sqlite3.DatabaseError("sentinel rollback db error")
                raise AssertionError(f"unexpected sql: {sql!r}")

        def wrapped(real_conn: sqlite3.Connection) -> None:
            original_probe(_ProbeFakeConn())  # type: ignore[arg-type]

        monkeypatch.setattr(audit_db_mod, "_write_probe", wrapped)
        with pytest.raises(
            AuditDbConfigError, match="write probe rollback failed"
        ):
            open_audit_db(audit_db_path)

    def test_open_audit_db_connect_failure_raises_ex_config(
        self,
        audit_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If :func:`sqlite3.connect` itself raises
        :class:`sqlite3.OperationalError` (file permission errors,
        invalid path targets, readonly mount edge cases), the failure
        must surface as :class:`AuditDbConfigError` not a raw sqlite
        exception — spec §4 item 8 EX_CONFIG contract.
        """

        def _failing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(audit_db_mod.sqlite3, "connect", _failing_connect)
        with pytest.raises(AuditDbConfigError, match="connection open failed"):
            open_audit_db(audit_db_path)

    def test_open_audit_db_connect_database_error_raises_ex_config(
        self,
        audit_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Broader :class:`sqlite3.Error` subclasses raised at connect
        time (e.g. :class:`sqlite3.DatabaseError` on a corrupt header)
        must also map to :class:`AuditDbConfigError` — proves the
        except clause is widened past ``OperationalError``.
        """

        def _failing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            raise sqlite3.DatabaseError("file is not a database")

        monkeypatch.setattr(audit_db_mod.sqlite3, "connect", _failing_connect)
        with pytest.raises(AuditDbConfigError, match="connection open failed"):
            open_audit_db(audit_db_path)

    def test_schema_version_verified_before_apply_schema(
        self,
        audit_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When reopening a DB whose version is ahead of the code,
        :func:`_apply_schema` must NOT be called — the pre-apply guard
        aborts first so stale code cannot mutate the version table via
        ``INSERT OR IGNORE``.
        """
        # Bootstrap a healthy DB, then inject a future-version row.
        c = open_audit_db(audit_db_path)
        c.execute(
            "INSERT INTO audit_db_schema_version (version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION + 1,),
        )
        c.close()
        # Spy on _apply_schema — it must NOT run on this reopen.
        calls: list[bool] = []
        real_apply = audit_db_mod._apply_schema

        def spy(conn: sqlite3.Connection) -> None:
            calls.append(True)
            real_apply(conn)

        monkeypatch.setattr(audit_db_mod, "_apply_schema", spy)
        with pytest.raises(AuditDbConfigError, match="schema_version mismatch"):
            open_audit_db(audit_db_path)
        assert calls == [], (
            "_apply_schema must not run when pre-apply version guard rejects"
        )


# ---------------------------------------------------------------------------
# Enum drift guard (C2)
# ---------------------------------------------------------------------------


class TestEnumDrift:
    def test_db_check_matches_writer_outcome_enum(self) -> None:
        """Each OUTCOME_VALUES entry MUST appear verbatim in the audit_row DDL.

        The DDL is generated from OUTCOME_VALUES at import time, so this
        test would fail only if the generation logic itself drifted.
        """
        ddl = audit_db_mod._SCHEMA_STATEMENTS[0]
        for v in OUTCOME_VALUES:
            assert f"'{v}'" in ddl, (
                f"outcome enum value {v!r} missing from DDL CHECK clause; "
                "writer/DDL drift"
            )
        # And no stray values.
        assert ddl.count("outcome IN (") == 1

    def test_db_check_matches_writer_source_enum(self) -> None:
        ddl = audit_db_mod._SCHEMA_STATEMENTS[0]
        for v in SOURCE_VALUES:
            assert f"'{v}'" in ddl, (
                f"source enum value {v!r} missing from DDL CHECK clause"
            )
        assert ddl.count("source IN (") == 1

    def test_sql_string_list_escapes_single_quotes(self) -> None:
        """A future enum value containing ``'`` must double-escape
        rather than break DDL parsing.

        Verifies both the generation rule and that the resulting DDL
        compiles cleanly on a fresh sqlite connection.
        """
        # Generation: single quotes doubled.
        rendered = audit_db_mod._sql_string_list(frozenset({"o'brien", "hit"}))
        assert "'o''brien'" in rendered
        assert "'hit'" in rendered
        # Smoke: a CHECK clause built from this list parses without error.
        c = sqlite3.connect(":memory:")
        try:
            c.execute(
                f"CREATE TABLE t (x TEXT NOT NULL CHECK (x IN ({rendered})))"
            )
            c.execute("INSERT INTO t (x) VALUES (?)", ("o'brien",))
            c.execute("INSERT INTO t (x) VALUES (?)", ("hit",))
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("INSERT INTO t (x) VALUES (?)", ("nope",))
        finally:
            c.close()


# ---------------------------------------------------------------------------
# Non-empty enum frozensets enforced at import time
# ---------------------------------------------------------------------------


class TestEnumImportGuard:
    """An empty OUTCOME_VALUES/SOURCE_VALUES would generate ``outcome IN ()``
    which SQLite evaluates as always-false → silent reject of every write.
    The module asserts non-empty at import to surface this at startup.
    """

    def test_outcome_values_is_non_empty(self) -> None:
        from parallax.apex.audit_writer import OUTCOME_VALUES

        assert len(OUTCOME_VALUES) > 0

    def test_source_values_is_non_empty(self) -> None:
        from parallax.apex.audit_writer import SOURCE_VALUES

        assert len(SOURCE_VALUES) > 0

    @pytest.mark.parametrize(
        ("which", "interpreter_args"),
        [
            ("OUTCOME_VALUES", []),
            ("SOURCE_VALUES", []),
            # -O strips bare `assert` — guard must use if/raise so this
            # variant still aborts under optimization.
            ("OUTCOME_VALUES", ["-O"]),
        ],
    )
    def test_empty_frozenset_aborts_audit_db_import(
        self, which: str, interpreter_args: list[str]
    ) -> None:
        """Spawn a fresh interpreter where one of the enum frozensets is
        monkeypatched to ``frozenset()`` BEFORE ``audit_db`` is imported.
        The module-level guard must raise AssertionError at import —
        proving fail-fast rather than silent always-false CHECK.

        Parametrized over both enum frozensets + -O optimization.

        Subprocess isolation is required: ``importlib.reload`` inside the
        test process would swap class identity for imported names like
        :class:`AuditDbWriteError`, breaking subsequent ``isinstance``
        checks in other tests in this session.
        """
        import subprocess
        import sys as _sys

        script = (
            "import parallax.apex.audit_writer as w; "
            f"w.{which} = frozenset(); "
            "import parallax.apex.audit_db"  # should AssertionError here
        )
        result = subprocess.run(
            [_sys.executable, *interpreter_args, "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode != 0, (
            f"Empty {which} (interpreter_args={interpreter_args}) "
            "must abort audit_db import"
        )
        assert f"{which} must be non-empty" in result.stderr, (
            f"Expected assertion message in stderr; got: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# §6 write_audit_row
# ---------------------------------------------------------------------------


class TestWriteRow:
    def test_round_trip_required_only(self, conn: sqlite3.Connection) -> None:
        row = canonicalize_row(_valid_row())
        write_audit_row(conn, row)
        out = conn.execute(
            "SELECT * FROM audit_row WHERE envelope_message_id = ?",
            (row.data["envelope_message_id"],),
        ).fetchone()
        assert out is not None
        for c in REQUIRED_COLUMNS:
            assert out[c] == row.data[c]
        for c in OPTIONAL_COLUMNS:
            assert out[c] is None
        # No phantom row at a different id.
        (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert count == 1

    def test_round_trip_divergence_with_optional_fields(
        self, conn: sqlite3.Connection
    ) -> None:
        row = canonicalize_row(
            _valid_row(
                outcome="divergence",
                aphelion_hash="b" * 64,
                local_hash="c" * 64,
                reason_code="claim.r4_subject_missing",
            )
        )
        write_audit_row(conn, row)
        out = conn.execute(
            "SELECT * FROM audit_row WHERE envelope_message_id = ?",
            (row.data["envelope_message_id"],),
        ).fetchone()
        assert out["aphelion_hash"] == "b" * 64
        assert out["local_hash"] == "c" * 64
        assert out["reason_code"] == "claim.r4_subject_missing"
        assert out["outcome"] == "divergence"

    def test_round_trip_error_with_reason_code(self, conn: sqlite3.Connection) -> None:
        row = canonicalize_row(
            _valid_row(outcome="error", reason_code="pkg.unsigned")
        )
        write_audit_row(conn, row)
        out = conn.execute(
            "SELECT reason_code, outcome, aphelion_hash FROM audit_row "
            "WHERE envelope_message_id = ?",
            (row.data["envelope_message_id"],),
        ).fetchone()
        assert out["reason_code"] == "pkg.unsigned"
        assert out["outcome"] == "error"
        assert out["aphelion_hash"] is None

    def test_duplicate_envelope_id_raises_integrity_error(
        self, conn: sqlite3.Connection
    ) -> None:
        row = canonicalize_row(_valid_row())
        write_audit_row(conn, row)
        with pytest.raises(sqlite3.IntegrityError):
            write_audit_row(conn, row)

    def test_invalid_outcome_rejected_by_check_constraint(
        self, conn: sqlite3.Connection
    ) -> None:
        """Use a valid 64-char digest + valid signer_id so the outcome
        CHECK is the ONLY constraint that can fire — without this, the
        digest-length CHECK could mask the assertion."""
        with pytest.raises(sqlite3.IntegrityError, match="outcome"):
            conn.execute(
                "INSERT INTO audit_row ("
                "claim_id,envelope_message_id,outcome,package_id,session_id,"
                "signer_id,signer_manifest_digest,source,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "0193e2b1-0001-7000-8000-000000000099",
                    "b3d7e2a1-4f8c-4b9d-8e3a-12c000000099",
                    "INVALID_OUTCOME",
                    "0193ef00-0001-7000-8000-000000000099",
                    "sess",
                    "signer",
                    "a" * 64,
                    "aphelion",
                    "2026-05-09T14:23:11Z",
                ),
            )

    def test_invalid_source_rejected_by_check_constraint(
        self, conn: sqlite3.Connection
    ) -> None:
        """Same fixture hardening as outcome test above."""
        with pytest.raises(sqlite3.IntegrityError, match="source"):
            conn.execute(
                "INSERT INTO audit_row ("
                "claim_id,envelope_message_id,outcome,package_id,session_id,"
                "signer_id,signer_manifest_digest,source,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "0193e2b1-0001-7000-8000-000000000098",
                    "b3d7e2a1-4f8c-4b9d-8e3a-12c000000098",
                    "hit",
                    "0193ef00-0001-7000-8000-000000000098",
                    "sess",
                    "signer",
                    "a" * 64,
                    "openai",
                    "2026-05-09T14:23:11Z",
                ),
            )

    def test_not_null_required_fields_enforced(
        self, conn: sqlite3.Connection
    ) -> None:
        """NULL session_id triggers NOT NULL distinctly; all other
        fields are valid so no CHECK can fire first."""
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            conn.execute(
                "INSERT INTO audit_row ("
                "claim_id,envelope_message_id,outcome,package_id,session_id,"
                "signer_id,signer_manifest_digest,source,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "0193e2b1-0001-7000-8000-000000000097",
                    "b3d7e2a1-4f8c-4b9d-8e3a-12c000000097",
                    "hit",
                    "0193ef00-0001-7000-8000-000000000097",
                    None,
                    "signer",
                    "a" * 64,
                    "aphelion",
                    "2026-05-09T14:23:11Z",
                ),
            )

    def test_digest_length_check_rejects_short_and_long(
        self, conn: sqlite3.Connection
    ) -> None:
        """signer_manifest_digest must be exactly 64 chars OR empty
        string (unsigned packages, spec §6.1). 63/65 still reject."""
        for bad_digest in ("a" * 63, "a" * 65):
            with pytest.raises(sqlite3.IntegrityError, match="signer_manifest_digest"):
                conn.execute(
                    "INSERT INTO audit_row ("
                    "claim_id,envelope_message_id,outcome,package_id,session_id,"
                    "signer_id,signer_manifest_digest,source,ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "0193e2b1-0001-7000-8000-000000000050",
                        f"b3d7e2a1-4f8c-4b9d-8e3a-{len(bad_digest):012d}",
                        "hit",
                        "0193ef00-0001-7000-8000-000000000050",
                        "sess",
                        "signer",
                        bad_digest,
                        "aphelion",
                        "2026-05-09T14:23:11Z",
                    ),
                )

    def test_digest_empty_string_accepted_for_unsigned_packages(
        self, conn: sqlite3.Connection
    ) -> None:
        """Spec §6.1 allows signer_manifest_digest='' for unsigned
        Aphelion packages. The CHECK must accept this case while still
        rejecting other non-64 lengths."""
        conn.execute(
            "INSERT INTO audit_row ("
            "claim_id,envelope_message_id,outcome,package_id,session_id,"
            "signer_id,signer_manifest_digest,source,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "0193e2b1-0001-7000-8000-000000000051",
                "b3d7e2a1-4f8c-4b9d-8e3a-12c000000051",
                "hit",
                "0193ef00-0001-7000-8000-000000000051",
                "sess-unsigned",
                "",  # unsigned: signer_id also empty per spec §6.1
                "",  # empty digest — must be accepted
                "aphelion",
                "2026-05-09T14:23:11Z",
            ),
        )
        # Row landed; subsequent read confirms it.
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM audit_row WHERE envelope_message_id = ?",
            ("b3d7e2a1-4f8c-4b9d-8e3a-12c000000051",),
        ).fetchone()
        assert count == 1

    def test_in_transaction_precondition_rejected(self) -> None:
        """write_audit_row guards against conn already in a txn."""
        row = canonicalize_row(_valid_row())

        class FakeBusyConn:
            in_transaction = True

            def execute(self, sql: str, params: Any = ()) -> Any:
                raise AssertionError("execute must not run when conn is busy")

        with pytest.raises(AuditDbUsageError, match="autocommit"):
            write_audit_row(FakeBusyConn(), row)  # type: ignore[arg-type]

    def test_canonicalize_bypass_rejected(self, conn: sqlite3.Connection) -> None:
        """Constructing AuditRow directly with invalid data must fail at write.

        Belt-and-braces: even if a caller skips :func:`canonicalize_row`,
        :func:`write_audit_row` re-runs validation. A row missing a
        required field raises before any DB write.
        """
        from types import MappingProxyType

        bogus_data = dict(_valid_row())
        del bogus_data["session_id"]
        rogue = AuditRow(data=MappingProxyType(bogus_data))
        with pytest.raises(AuditRowValidationError, match="session_id"):
            write_audit_row(conn, rogue)
        # And no row landed in the DB.
        (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert count == 0

    def test_rollback_database_error_does_not_mask_insert_error(self) -> None:
        """If the INSERT raises and the subsequent ROLLBACK also raises
        a :class:`sqlite3.DatabaseError` (not just :class:`OperationalError`),
        the original INSERT exception must reach the caller, not the
        ROLLBACK one.

        Uses a fake connection (sqlite3.Connection.execute is read-only and
        cannot be monkey-patched in place).
        """
        row = canonicalize_row(_valid_row())
        insert_error = sqlite3.ProgrammingError("sentinel insert failure")
        rollback_error = sqlite3.DatabaseError("sentinel rollback failure")

        class FakeConn:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, sql: str, params: Any = ()) -> Any:
                normalized = sql.strip().upper()
                self.calls.append(normalized.split()[0])
                if normalized.startswith("BEGIN"):
                    return None
                if normalized.startswith("INSERT"):
                    raise insert_error
                if normalized.startswith("ROLLBACK"):
                    raise rollback_error
                if normalized.startswith("COMMIT"):
                    return None
                raise AssertionError(f"unexpected sql: {sql!r}")

        fake = FakeConn()
        with pytest.raises(sqlite3.ProgrammingError) as exc_info:
            write_audit_row(fake, row)  # type: ignore[arg-type]
        assert exc_info.value is insert_error, (
            "ROLLBACK failure must not replace the original INSERT exception"
        )
        # Sanity: BEGIN, INSERT, ROLLBACK were all attempted.
        assert fake.calls == ["BEGIN", "INSERT", "ROLLBACK"]

    def test_commit_failure_raises_write_error_and_clears_transaction(
        self,
    ) -> None:
        """A COMMIT that raises (disk full, SQLITE_FULL) must surface
        as :class:`AuditDbWriteError` AND best-effort ROLLBACK runs so
        the connection returns to autocommit state. No raw
        ``sqlite3.OperationalError`` leaks to the caller.
        """
        row = canonicalize_row(_valid_row())
        commit_error = sqlite3.OperationalError("database or disk is full")

        class FakeConn:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, sql: str, params: Any = ()) -> Any:
                normalized = sql.strip().upper()
                self.calls.append(normalized.split()[0])
                if normalized.startswith("COMMIT"):
                    raise commit_error
                # BEGIN / INSERT / ROLLBACK all succeed silently
                return None

        fake = FakeConn()
        with pytest.raises(AuditDbWriteError, match="commit failed") as exc_info:
            write_audit_row(fake, row)  # type: ignore[arg-type]
        # Wrapped, not raw
        assert exc_info.value.__cause__ is commit_error
        # Best-effort ROLLBACK fired to drain the transaction
        assert "BEGIN" in fake.calls
        assert "INSERT" in fake.calls
        assert "COMMIT" in fake.calls
        assert "ROLLBACK" in fake.calls
        assert fake.calls[-1] == "ROLLBACK", (
            "ROLLBACK must run AFTER the failed COMMIT to clear the txn"
        )

    def test_commit_failure_when_rollback_also_fails(self) -> None:
        """If even the best-effort ROLLBACK after COMMIT-fail raises,
        the caller still sees AuditDbWriteError (not the secondary error)."""
        row = canonicalize_row(_valid_row())
        commit_error = sqlite3.OperationalError("database or disk is full")
        rollback_error = sqlite3.OperationalError("cannot rollback - no transaction")

        class FakeConn:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, sql: str, params: Any = ()) -> Any:
                normalized = sql.strip().upper()
                self.calls.append(normalized.split()[0])
                if normalized.startswith("COMMIT"):
                    raise commit_error
                if normalized.startswith("ROLLBACK"):
                    raise rollback_error
                return None

        fake = FakeConn()
        with pytest.raises(AuditDbWriteError) as exc_info:
            write_audit_row(fake, row)  # type: ignore[arg-type]
        assert exc_info.value.__cause__ is commit_error, (
            "Secondary ROLLBACK failure must not mask the COMMIT failure"
        )


# ---------------------------------------------------------------------------
# Smoke + golden vector (H6)
# ---------------------------------------------------------------------------


# Golden vector — locked precomputed sha256 of the canonical JSON of
# _GOLDEN_ROW. Any change to canonical_json normalization, key ordering,
# or audit_row schema field set will flip this hex. Regenerate via:
#   from parallax.apex.canonical_json import canonical_dumps, sha256_hex
#   sha256_hex(canonical_dumps(_GOLDEN_ROW))
_GOLDEN_ROW: dict[str, Any] = {
    "claim_id": "0193e2b1-0001-7000-8000-000000000001",
    "envelope_message_id": "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc",
    "outcome": "hit",
    "package_id": "0193ef00-0001-7000-8000-000000000005",
    "session_id": "sess-2026-05-09-001",
    "signer_id": "chris@aphelion-graph",
    "signer_manifest_digest": "a" * 64,
    "source": "aphelion",
    "ts": "2026-05-09T14:23:11Z",
}
# HARDCODED hex literal — intentionally NOT computed at import time.
# A live call to ``sha256_hex(canonical_dumps(_GOLDEN_ROW))`` here would
# drift with the canonicalizer (tautology — the test would always pass
# even if canonicalization changed). The literal below was computed once
# on 2026-05-12 against canonical_json v1; if it ever fails the test, either
# the canonicalizer changed (investigate before regenerating) or
# _GOLDEN_ROW changed (also intentional? otherwise revert).
#
# Regen (only after confirming a deliberate canonicalization change):
#   python -c "from parallax.apex.canonical_json import canonical_dumps, sha256_hex; \
#       from tests.apex.test_audit_db import _GOLDEN_ROW; \
#       print(sha256_hex(canonical_dumps(_GOLDEN_ROW)))"
_GOLDEN_SHA256_HEX: Final[str] = (
    "cc08c900b1bd82e67fd320314584b204ec7bd2da6b7dd74e14fd0738dd161cf4"
)


class TestEnvelopeSmoke:
    def test_golden_sha256_locks_canonicalization(self) -> None:
        """If canonical_json or audit_row schema drifts, this hex flips."""
        recomputed = sha256_hex(canonical_dumps(_GOLDEN_ROW))
        assert recomputed == _GOLDEN_SHA256_HEX, (
            "canonical JSON serialization has drifted from the locked "
            "golden vector. Inspect canonical_json._nfc / sort_keys / "
            "separators / NFC normalization before regenerating the hex."
        )

    def test_envelope_audit_db_ref_matches_persisted_row_digest(
        self, conn: sqlite3.Connection
    ) -> None:
        row = canonicalize_row(_valid_row())
        write_audit_row(conn, row)
        digest = row.sha256_hex()

        payload = {"event": "smoke_test", "value": 1}
        envelope_raw = {
            "envelope_version": ENVELOPE_VERSION_LITERAL,
            "schema_version": 1,
            "message_id": row.data["envelope_message_id"],
            "created_at": row.data["ts"],
            "source": row.data["source"],
            "audit_db_ref": digest,
            "payload_type": PayloadType.EVENT.value,
            "payload": payload,
            "checksum": compute_checksum(payload),
        }
        env = parse_envelope(envelope_raw)
        assert env.audit_db_ref == digest
        assert env.source == Source.APHELION

        persisted = conn.execute(
            "SELECT * FROM audit_row WHERE envelope_message_id = ?",
            (row.data["envelope_message_id"],),
        ).fetchone()
        persisted_dict: dict[str, Any] = {
            c: persisted[c] for c in REQUIRED_COLUMNS
        }
        for c in OPTIONAL_COLUMNS:
            if persisted[c] is not None:
                persisted_dict[c] = persisted[c]
        recomputed = sha256_hex(canonical_dumps(persisted_dict))
        assert recomputed == digest, (
            f"persisted row digest {recomputed} != original {digest}; "
            f"persisted={json.dumps(persisted_dict, sort_keys=True)}"
        )


# ---------------------------------------------------------------------------
# Issue #65 — write_audit_rows_atomic per-package atomicity
# ---------------------------------------------------------------------------


def _row_with_envelope(envelope_id: str, **overrides: Any) -> AuditRow:
    """Build a canonicalized row with a caller-supplied envelope_message_id."""
    return canonicalize_row(
        _valid_row(envelope_message_id=envelope_id, **overrides)
    )


class TestWriteRowsAtomic:
    """Per-package atomic write helper added for issue #65.

    Pre-fix bug: ``_write_batch`` called ``write_audit_row`` per row;
    each row had its own ``BEGIN IMMEDIATE`` / ``COMMIT``. If row N
    raised mid-batch, rows 1..N-1 were already committed. The new
    helper wraps the entire batch in a single transaction so any
    failure rolls back the whole package.
    """

    def test_empty_batch_is_noop(self, conn: sqlite3.Connection) -> None:
        """Empty tuple must NOT open a BEGIN / COMMIT pair."""
        write_audit_rows_atomic(conn, ())
        (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert count == 0
        assert conn.in_transaction is False, (
            "empty batch must leave the connection in autocommit state"
        )

    def test_single_row_round_trip(self, conn: sqlite3.Connection) -> None:
        """One-row batch produces exactly one committed row."""
        row = _row_with_envelope("b3d7e2a1-4f8c-4b9d-8e3a-12c000000a01")
        write_audit_rows_atomic(conn, (row,))
        (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert count == 1
        persisted = conn.execute(
            "SELECT * FROM audit_row WHERE envelope_message_id = ?",
            (row.data["envelope_message_id"],),
        ).fetchone()
        for col in REQUIRED_COLUMNS:
            assert persisted[col] == row.data[col]

    def test_multi_row_round_trip(self, conn: sqlite3.Connection) -> None:
        """Five-row batch commits all five rows atomically."""
        rows = tuple(
            _row_with_envelope(
                f"b3d7e2a1-4f8c-4b9d-8e3a-12c00000a{i:03x}",
                claim_id=f"0193e2b1-0001-7000-8000-00000000a{i:03x}",
            )
            for i in range(5)
        )
        write_audit_rows_atomic(conn, rows)
        (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert count == 5
        assert conn.in_transaction is False, (
            "connection must be in autocommit state after successful COMMIT"
        )

    def test_unique_violation_rolls_back_entire_batch(
        self, conn: sqlite3.Connection
    ) -> None:
        """Core issue #65 fix: a UNIQUE collision on row N must roll back
        rows 1..N-1 and leave the table byte-identical to its pre-call state."""
        duplicate_envelope = "b3d7e2a1-4f8c-4b9d-8e3a-12c00000fa01"
        # Pre-seed one row using the per-row writer so the helper's batch
        # has a UNIQUE collision waiting on its 3rd row.
        seeded = _row_with_envelope(
            duplicate_envelope,
            claim_id="0193e2b1-0001-7000-8000-000000000bb1",
        )
        write_audit_row(conn, seeded)
        pre_seed_count_query = conn.execute("SELECT COUNT(*) FROM audit_row")
        (pre_count,) = pre_seed_count_query.fetchone()
        assert pre_count == 1

        # Build a 4-row batch where row index 2 collides on envelope_message_id.
        batch_envelopes = [
            "b3d7e2a1-4f8c-4b9d-8e3a-12c00000fb01",
            "b3d7e2a1-4f8c-4b9d-8e3a-12c00000fb02",
            duplicate_envelope,  # collides — must roll back rows 0 + 1
            "b3d7e2a1-4f8c-4b9d-8e3a-12c00000fb04",
        ]
        batch = tuple(
            _row_with_envelope(
                env,
                claim_id=f"0193e2b1-0001-7000-8000-00000000fb{i:02x}",
            )
            for i, env in enumerate(batch_envelopes)
        )

        with pytest.raises(sqlite3.IntegrityError):
            write_audit_rows_atomic(conn, batch)

        # Post-condition: still exactly the one pre-seeded row.
        (post_count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert post_count == 1, (
            f"per-package atomicity violated — expected 1 row (the pre-seed), "
            f"got {post_count}; partial-batch rows 0 + 1 survived ROLLBACK"
        )
        # Verify the surviving row is the pre-seed, not a partial-batch leak.
        survivor = conn.execute(
            "SELECT claim_id FROM audit_row WHERE envelope_message_id = ?",
            (duplicate_envelope,),
        ).fetchone()
        assert survivor["claim_id"] == "0193e2b1-0001-7000-8000-000000000bb1"
        # Connection must be back to autocommit so the next caller can BEGIN.
        assert conn.in_transaction is False

    def test_validation_failure_rolls_back_entire_batch(
        self, conn: sqlite3.Connection
    ) -> None:
        """canonicalize_row failure inside the helper must also roll back.

        We inject a bad row by constructing :class:`AuditRow` directly
        with an invalid outcome — bypassing the caller-side
        canonicalize_row. The helper's belt-and-braces re-validation
        will raise, and rows queued before it must NOT persist.
        """
        from types import MappingProxyType

        good_row_0 = _row_with_envelope(
            "b3d7e2a1-4f8c-4b9d-8e3a-12c00000fc01",
            claim_id="0193e2b1-0001-7000-8000-00000000fc01",
        )
        good_row_1 = _row_with_envelope(
            "b3d7e2a1-4f8c-4b9d-8e3a-12c00000fc02",
            claim_id="0193e2b1-0001-7000-8000-00000000fc02",
        )
        bad_data = _valid_row(
            envelope_message_id="b3d7e2a1-4f8c-4b9d-8e3a-12c00000fc03",
            outcome="not_a_real_outcome",
        )
        bad_row = AuditRow(data=MappingProxyType(bad_data))

        with pytest.raises(AuditRowValidationError):
            write_audit_rows_atomic(conn, (good_row_0, good_row_1, bad_row))

        (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert count == 0, (
            "rows 0 + 1 must have been rolled back after row 2's "
            "validation failure inside the batch transaction"
        )
        assert conn.in_transaction is False

    def test_caller_in_transaction_rejected(
        self, conn: sqlite3.Connection
    ) -> None:
        """Caller-contract: precondition mirrors write_audit_row's.

        ``conn.in_transaction must be False`` — passing an already-open
        txn would cause the helper's BEGIN IMMEDIATE to raise
        ``cannot start a transaction within a transaction`` and leak
        the caller's dirty txn.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(AuditDbUsageError, match="autocommit"):
                write_audit_rows_atomic(
                    conn,
                    (_row_with_envelope("b3d7e2a1-4f8c-4b9d-8e3a-12c00000fd01"),),
                )
        finally:
            conn.execute("ROLLBACK")

    def test_rollback_failure_does_not_mask_primary_error(self) -> None:
        """If the best-effort ROLLBACK after a row failure also raises,
        the caller still sees the primary error (per per-row helper)."""
        primary_error = sqlite3.IntegrityError(
            "UNIQUE constraint failed: audit_row.envelope_message_id"
        )
        rollback_error = sqlite3.OperationalError("cannot rollback — no txn")

        class FakeConn:
            in_transaction = False  # initial autocommit state

            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, sql: str, params: Any = ()) -> Any:
                normalized = sql.strip().upper()
                self.calls.append(normalized.split()[0])
                if normalized.startswith("BEGIN"):
                    return None
                if normalized.startswith("INSERT"):
                    raise primary_error
                if normalized.startswith("ROLLBACK"):
                    raise rollback_error
                return None

        fake = FakeConn()
        row = _row_with_envelope("b3d7e2a1-4f8c-4b9d-8e3a-12c00000fe01")
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            write_audit_rows_atomic(fake, (row,))  # type: ignore[arg-type]
        assert exc_info.value is primary_error, (
            "secondary ROLLBACK failure must not replace the primary INSERT failure"
        )
        assert "ROLLBACK" in fake.calls, (
            "ROLLBACK must be attempted even when it is doomed to fail"
        )
