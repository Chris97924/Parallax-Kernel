"""Mutation-hardening for ``parallax.apex.audit_db`` (land-20260823 wave 4, S2).

Additive companion to ``tests/apex/test_audit_db.py`` and
``tests/router/test_audit_db_persistence.py``. Every test below was written
against a semantic mutant that the pre-existing suites let through.

The existing coverage is broad — startup gates, schema shape, enum drift,
per-row and per-batch write paths all have cases. It is thin in five specific
places:

  * **Guards are proven in one direction only.** ``schema_version`` mismatch is
    exercised with a DB *ahead* of the code, never behind — so narrowing
    ``!=`` to ``>`` keeps the suite green while a stale DB is silently upgraded
    by the very ``INSERT OR IGNORE`` the pre-apply check exists to prevent.
    Likewise ``quick_check`` is driven to its two *timeout* arms but never to a
    non-``ok`` result, so the corruption branch itself is unobserved.

  * **Cleanup is invisible when it is skipped.** ``_quick_check`` installs a
    progress handler and removes it in a ``finally``. Nothing observes the
    removal, yet a leaked handler stays armed on the connection the caller is
    handed and aborts unrelated queries once the budget elapses.

  * **Failure paths are checked for the exception, not for the state they
    leave behind.** ``test_duplicate_envelope_id_raises_integrity_error``
    asserts the ``IntegrityError`` and stops. Whether the ROLLBACK actually
    ran — i.e. whether the connection is still usable — is unasserted, so
    dropping it strands every later write on that connection.

  * **Structured operator logs are contract, not decoration.** The batch
    rollback-failure log carries ``event`` / ``stage`` / ``batch_size`` for
    oncall. The existing test asserts the primary error survives and that
    ROLLBACK was attempted; the record itself is never inspected.

  * **Defaults and derived values that only one caller supplies.**
    ``resolve_audit_db_path``'s ``os.environ`` fallback (every test passes an
    explicit mapping), ``PRAGMA synchronous`` (asserted for the router's
    sqlite gate, never for the audit DB), the sorted DDL rendering (the enum
    tests check membership, not order), and the per-thread connection cache's
    wrong-path guard and cross-thread isolation.

Expected values are literals throughout.
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3
import threading
from typing import Any

import pytest

from parallax.apex import audit_db as audit_db_mod
from parallax.apex.audit_db import (
    AuditDbConfigError,
    AuditDbUsageError,
    AuditDbWriteError,
    open_audit_db,
    resolve_audit_db_path,
    write_audit_row,
    write_audit_rows_atomic,
)
from parallax.apex.audit_writer import canonicalize_row

pytestmark = pytest.mark.integration

_AUDIT_DB_LOGGER = "parallax.apex.audit_db"


@pytest.fixture()
def audit_db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "audit.db"


@pytest.fixture()
def conn(audit_db_path: pathlib.Path) -> Any:
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


class _FakeQuickCheckConn:
    """Minimal connection stand-in for :func:`_quick_check`.

    Only the two methods that function touches are implemented, which is what
    lets the corruption branch be reached at all — a real SQLite file that
    returns a non-``ok`` quick_check would have to be genuinely corrupted on
    disk, which is neither portable nor fast.
    """

    def __init__(self, row: Any) -> None:
        self._row = row
        self.handler_calls: list[tuple[Any, int]] = []
        self.statements: list[str] = []

    def set_progress_handler(self, handler: Any, n: int) -> None:
        self.handler_calls.append((handler, n))

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.statements.append(sql)
        row = self._row

        class _Cursor:
            def fetchone(self) -> Any:
                return row

        return _Cursor()


def _fresh_thread_local() -> Any:
    """A brand-new instance of whatever type the module's cache holder is.

    The pre-existing persistence test swaps in a literal ``threading.local()``
    to isolate itself from connections other tests cached. Doing that here
    would *supply* the thread-locality these tests are trying to observe — the
    cross-thread case would pass even if the module had been changed to a plain
    shared object. Constructing from ``type(...)`` keeps the isolation and
    leaves the property under test where it belongs: in the module.
    """
    return type(audit_db_mod._thread_local)()


class _RecordingConn:
    """Records every statement; never fails."""

    in_transaction = False

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.statements.append(sql.strip().split()[0].upper())
        return None


# ---------------------------------------------------------------------------
# §3 path resolution — the os.environ default
# ---------------------------------------------------------------------------


class TestResolvePathDefaultEnv:
    def test_defaults_to_process_environment(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``env=None`` must mean ``os.environ``, not "no configuration".

        Every pre-existing case passes an explicit mapping, so the default
        argument — the one production actually uses, since
        ``parallax_lifespan`` calls ``resolve_audit_db_path()`` with no
        arguments — is never exercised. A mutant that defaults to an empty
        mapping turns a correctly configured deployment into an EX_CONFIG
        refusal to boot, with the whole suite still green.
        """
        expected = tmp_path / "from-env.db"
        monkeypatch.setenv("PARALLAX_AUDIT_DB_PATH", str(expected))
        assert resolve_audit_db_path() == expected


# ---------------------------------------------------------------------------
# §4 absolute-path gate — '..' is a path segment, not a substring
# ---------------------------------------------------------------------------


class TestAbsolutePathGuard:
    def test_dotdot_inside_a_segment_is_not_a_traversal(
        self, tmp_path: pathlib.Path
    ) -> None:
        """``release..v2`` is a legal directory name, not parent traversal.

        The guard compares whole ``path.parts`` entries. Relaxing it to a
        substring test (``".." in part``) is invisible to the pre-existing
        case — which uses a real ``..`` segment — but starts rejecting valid
        operator paths at boot, and the only symptom is a server that will
        not start.
        """
        audit_db_mod._check_absolute_path(tmp_path / "release..v2" / "audit.db")

    def test_dotdot_segment_in_the_middle_is_rejected(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Traversal anywhere in the path counts, not just at the end."""
        with pytest.raises(AuditDbConfigError, match="segments"):
            audit_db_mod._check_absolute_path(
                tmp_path / ".." / "sibling" / "audit.db"
            )


# ---------------------------------------------------------------------------
# §4 quick_check — the corruption arm and the progress-handler teardown
# ---------------------------------------------------------------------------


class TestQuickCheckResultHandling:
    def test_non_ok_result_is_rejected(self) -> None:
        """The branch the gate exists for: a corrupt page report.

        Both pre-existing quick_check cases drive the *budget* arms. The
        result comparison itself is unobserved, so relaxing ``result != "ok"``
        to a None-check lets a DB that SQLite has just reported as corrupt
        open normally and start collecting audit rows.
        """
        fake = _FakeQuickCheckConn(("*** in database main ***\nPage 42 is never used",))
        with pytest.raises(AuditDbConfigError, match="quick_check returned non-ok"):
            audit_db_mod._quick_check(fake)

    def test_missing_result_row_is_rejected(self) -> None:
        """``fetchone()`` returning None must not become an AttributeError.

        The module deliberately routes it into the same EX_CONFIG refusal;
        dropping the ``row is not None`` guard turns a silent-DB edge case
        into an unhandled crash at boot.
        """
        fake = _FakeQuickCheckConn(None)
        with pytest.raises(AuditDbConfigError, match="quick_check returned non-ok"):
            audit_db_mod._quick_check(fake)

    def test_progress_handler_is_uninstalled_before_returning(self) -> None:
        """The ``finally`` teardown, observed directly."""
        fake = _FakeQuickCheckConn(("ok",))
        audit_db_mod._quick_check(fake)
        assert fake.handler_calls[-1] == (None, 0)

    def test_leaked_progress_handler_does_not_abort_later_queries(
        self, audit_db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end consequence of skipping that teardown.

        The handler closes over the budget and is installed on the very
        connection ``open_audit_db`` hands back. If it is never removed, then
        the moment the budget elapses — and it is a *wall-clock* budget, so on
        a long-lived server connection it always eventually does — the next
        statement of any size is aborted by SQLite. Here the budget is forced
        negative after boot to make "eventually" immediate.
        """
        conn = open_audit_db(audit_db_path)
        try:
            monkeypatch.setattr(audit_db_mod, "QUICK_CHECK_BUDGET_SECONDS", -1.0)
            (count,) = conn.execute(
                "WITH RECURSIVE c(x) AS ("
                "  SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 50000"
                ") SELECT count(*) FROM c"
            ).fetchone()
        finally:
            conn.close()
        assert count == 50000


# ---------------------------------------------------------------------------
# schema_version — the "DB behind the code" direction
# ---------------------------------------------------------------------------


class TestSchemaVersionDirection:
    def test_db_behind_code_is_refused_and_left_untouched(
        self, audit_db_path: pathlib.Path
    ) -> None:
        """Mismatch means mismatch — in both directions.

        The pre-existing mismatch case only injects a version *ahead* of the
        code, so narrowing the comparison to ``value > CURRENT_SCHEMA_VERSION``
        survives. Under that mutant a DB at v=0 opens, and the pre-apply guard
        that exists precisely to stop ``_apply_schema``'s
        ``INSERT OR IGNORE`` from touching a foreign DB no longer fires — so
        the second assertion here (the version table is exactly what we left)
        is the load-bearing half.
        """
        c = open_audit_db(audit_db_path)
        c.execute("DELETE FROM audit_db_schema_version")
        c.execute("INSERT INTO audit_db_schema_version (version) VALUES (0)")
        c.close()

        with pytest.raises(AuditDbConfigError, match="schema_version mismatch"):
            open_audit_db(audit_db_path)

        raw = sqlite3.connect(str(audit_db_path))
        try:
            versions = [
                r[0]
                for r in raw.execute(
                    "SELECT version FROM audit_db_schema_version ORDER BY version"
                )
            ]
        finally:
            raw.close()
        assert versions == [0]


# ---------------------------------------------------------------------------
# Connection pragmas
# ---------------------------------------------------------------------------


class TestConnectionPragmas:
    def test_synchronous_is_normal(self, conn: sqlite3.Connection) -> None:
        """``synchronous=NORMAL`` (1) — durability, not just speed.

        WAL mode, busy_timeout and wal_autocheckpoint each have a pre-existing
        assertion; this pragma does not, so a mutant setting ``OFF`` (0) leaves
        the audit chain — the artifact the whole module exists to make
        trustworthy — losing committed rows on power loss, silently.
        """
        (sync,) = conn.execute("PRAGMA synchronous").fetchone()
        assert sync == 1


# ---------------------------------------------------------------------------
# DDL generation determinism
# ---------------------------------------------------------------------------


class TestDdlGeneration:
    def test_sql_string_list_is_sorted(self) -> None:
        """Sorted rendering — the reason ``CREATE TABLE IF NOT EXISTS`` is safe.

        The enum-drift tests assert each value *appears* in the DDL, which
        holds for any ordering. Dropping the ``sorted()`` makes the generated
        DDL depend on frozenset iteration order, i.e. on ``PYTHONHASHSEED``:
        the schema bytes then differ run to run.
        """
        rendered = audit_db_mod._sql_string_list(
            frozenset(
                {
                    "delta", "alpha", "foxtrot", "charlie",
                    "bravo", "echo", "golf", "hotel",
                }
            )
        )
        assert rendered == (
            "'alpha','bravo','charlie','delta','echo','foxtrot','golf','hotel'"
        )


# ---------------------------------------------------------------------------
# write_audit_row — transaction hygiene after a failed INSERT
# ---------------------------------------------------------------------------


class TestWriteRowTransactionHygiene:
    def test_integrity_error_leaves_connection_usable(
        self, conn: sqlite3.Connection
    ) -> None:
        """A rejected row must not poison the connection.

        The pre-existing duplicate test asserts the ``IntegrityError`` and
        stops. Without the best-effort ROLLBACK the connection stays inside
        the ``BEGIN IMMEDIATE``, so the *next* call trips the autocommit
        precondition with ``AuditDbUsageError`` — every subsequent audit row
        on that connection is lost, and the traceback blames the innocent
        caller. In production that connection is the thread-local one, cached
        for the life of the worker thread.
        """
        first = canonicalize_row(_valid_row())
        write_audit_row(conn, first)
        with pytest.raises(sqlite3.IntegrityError):
            write_audit_row(conn, first)

        assert conn.in_transaction is False

        second = canonicalize_row(
            _valid_row(
                envelope_message_id="b3d7e2a1-4f8c-4b9d-8e3a-12c4567890ff",
                claim_id="0193e2b1-0001-7000-8000-0000000000ff",
            )
        )
        write_audit_row(conn, second)
        (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert count == 2


# ---------------------------------------------------------------------------
# write_audit_rows_atomic — empty batch and the operator log
# ---------------------------------------------------------------------------


class TestAtomicBatchEmptyContract:
    def test_empty_batch_opens_no_transaction(self) -> None:
        """"No BEGIN / COMMIT pair is opened" — as stated, not as inferred.

        With a real connection an empty ``BEGIN`` / ``COMMIT`` pair is
        indistinguishable from doing nothing: no rows either way, autocommit
        either way. A recording connection is what makes the difference
        visible, and the difference matters — ``BEGIN IMMEDIATE`` takes the
        database's write lock, so a no-op batch would serialise against every
        other writer for no reason.
        """
        fake = _RecordingConn()
        write_audit_rows_atomic(fake, ())  # type: ignore[arg-type]
        assert fake.statements == []


class TestAtomicBatchRollbackLogging:
    """The structured fields oncall reads when a rollback fails.

    ``test_rollback_failure_does_not_mask_primary_error`` proves the exception
    contract; the log record it produces is never inspected, so the severity,
    the stage label that says *where* the batch died, and the batch size are
    all free to drift.
    """

    def test_row_failure_logs_error_with_stage_and_batch_size(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        primary = sqlite3.IntegrityError("UNIQUE constraint failed")

        class FakeConn:
            in_transaction = False

            def execute(self, sql: str, params: Any = ()) -> Any:
                normalized = sql.strip().upper()
                if normalized.startswith("INSERT"):
                    raise primary
                if normalized.startswith("ROLLBACK"):
                    raise sqlite3.OperationalError("cannot rollback")
                return None

        rows = (
            canonicalize_row(_valid_row()),
            canonicalize_row(
                _valid_row(
                    envelope_message_id="b3d7e2a1-4f8c-4b9d-8e3a-12c4567890ab",
                    claim_id="0193e2b1-0001-7000-8000-0000000000ab",
                )
            ),
        )
        with caplog.at_level(logging.ERROR, logger=_AUDIT_DB_LOGGER):
            with pytest.raises(sqlite3.IntegrityError):
                write_audit_rows_atomic(FakeConn(), rows)  # type: ignore[arg-type]

        records = [r for r in caplog.records if r.name == _AUDIT_DB_LOGGER]
        assert len(records) == 1, records
        record = records[0]
        assert record.levelno == logging.ERROR
        assert record.event == "audit_db_rollback_failed"
        assert record.stage == "row_failure"
        assert record.batch_size == 2

    def test_commit_failure_logs_the_commit_stage(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The two rollback sites must not report the same stage.

        They are what tells an operator whether the rows were rejected or the
        storage layer gave out — different pages, different fixes.
        """

        class FakeConn:
            in_transaction = False

            def execute(self, sql: str, params: Any = ()) -> Any:
                normalized = sql.strip().upper()
                if normalized.startswith("COMMIT"):
                    raise sqlite3.OperationalError("disk I/O error")
                if normalized.startswith("ROLLBACK"):
                    raise sqlite3.OperationalError("cannot rollback")
                return None

        with caplog.at_level(logging.ERROR, logger=_AUDIT_DB_LOGGER):
            with pytest.raises(AuditDbWriteError, match="batch commit failed"):
                write_audit_rows_atomic(  # type: ignore[arg-type]
                    FakeConn(), (canonicalize_row(_valid_row()),)
                )

        records = [r for r in caplog.records if r.name == _AUDIT_DB_LOGGER]
        assert len(records) == 1, records
        assert records[0].stage == "commit_failure"
        assert records[0].batch_size == 1


# ---------------------------------------------------------------------------
# get_thread_local_audit_conn — wrong-path guard and per-thread isolation
# ---------------------------------------------------------------------------


class TestThreadLocalConnCache:
    def test_second_path_on_same_thread_is_refused(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cached connection is never silently reused for another DB.

        The pre-existing test proves reuse for the *same* path. Dropping the
        path comparison would hand back a connection to the first database
        while the caller believes it is writing to the second — audit rows
        landing in the wrong file, with nothing raised.
        """
        monkeypatch.setattr(audit_db_mod, "_thread_local", _fresh_thread_local())
        first = audit_db_mod.get_thread_local_audit_conn(tmp_path / "a.db")
        try:
            with pytest.raises(AuditDbUsageError, match="single audit-db path"):
                audit_db_mod.get_thread_local_audit_conn(tmp_path / "b.db")
        finally:
            first.close()

    def test_each_thread_gets_its_own_connection(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of the cache is that it is *thread*-local.

        sqlite3 connections are not thread-safe and this cache feeds
        DualReadRouter's worker threads. A module-level dict would pass the
        pre-existing same-thread reuse test perfectly while handing one
        connection to every worker at once.
        """
        monkeypatch.setattr(audit_db_mod, "_thread_local", _fresh_thread_local())
        path = tmp_path / "shared.db"
        main_conn = audit_db_mod.get_thread_local_audit_conn(path)
        worker_result: dict[str, Any] = {}

        def _worker() -> None:
            # sqlite3 forbids using a connection from any thread but the one
            # that created it — closing it included. So the worker's connection
            # is both inspected and closed here; only the verdict travels back.
            try:
                conn = audit_db_mod.get_thread_local_audit_conn(path)
                worker_result["is_same_object"] = conn is main_conn
                conn.close()
            except BaseException as exc:  # pragma: no cover - surfaced below
                worker_result["error"] = exc

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(timeout=30)

        try:
            assert "error" not in worker_result, worker_result["error"]
            assert worker_result["is_same_object"] is False
        finally:
            main_conn.close()
