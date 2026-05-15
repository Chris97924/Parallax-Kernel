"""Apex M5 audit DB — connection bootstrap + schema + row writer.

Pins ``docs/m5-prep/audit-db-path-config.md`` §3-§6 normative contract:

  * §3 — ``PARALLAX_AUDIT_DB_PATH`` env var; unset/empty rejected as EX_CONFIG
  * §4 — startup validation gates (absolute path, parent exists+writable,
         ``PRAGMA quick_check`` within 30 s with a progress-handler abort,
         write-permission probe via ``BEGIN IMMEDIATE`` + ``ROLLBACK``)
  * §6 — ``audit_row`` schema (9 required + 3 optional columns, ``outcome``
         and ``source`` CHECK constraints derived AT IMPORT TIME from
         :data:`parallax.apex.audit_writer.OUTCOME_VALUES` /
         :data:`SOURCE_VALUES` so the DDL cannot drift from the writer's
         validation set)

The schema is applied on every :func:`open_audit_db` via ``CREATE TABLE
IF NOT EXISTS`` for idempotency. ``audit_db_schema_version`` records
applied schema versions append-only; the open path reads back
``MAX(version)`` and refuses to open a DB whose version disagrees with
:data:`CURRENT_SCHEMA_VERSION` so a stale client cannot silently
operate against a newer DB.

Concurrency: WAL journal mode, ``busy_timeout=5000`` ms,
``wal_autocheckpoint=200`` pages, autocommit isolation with explicit
``BEGIN IMMEDIATE`` around the writer's INSERT. Each producer
thread/process gets its own connection via :func:`open_audit_db`.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
import threading
import time
from collections.abc import Mapping
from typing import Any, Final

from parallax.apex.audit_writer import (
    OUTCOME_VALUES,
    SOURCE_VALUES,
    AuditRow,
    canonicalize_row,
)

_log = logging.getLogger(__name__)
# Standard library practice (PEP 282 / logging HOWTO): a library module
# attaches a NullHandler so an application that has not configured the
# root logger does not emit "No handlers" warnings on library log calls.
_log.addHandler(logging.NullHandler())

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "ENV_VAR_NAME",
    "EX_CONFIG",
    "OPTIONAL_COLUMNS",
    "QUICK_CHECK_BUDGET_SECONDS",
    "REQUIRED_COLUMNS",
    "AuditDbConfigError",
    "AuditDbUsageError",
    "AuditDbWriteError",
    "get_thread_local_audit_conn",
    "open_audit_db",
    "resolve_audit_db_path",
    "write_audit_row",
]

ENV_VAR_NAME: Final = "PARALLAX_AUDIT_DB_PATH"
EX_CONFIG: Final = 78  # sysexits.h EX_CONFIG — spec §4 item 8
CURRENT_SCHEMA_VERSION: Final = 1
QUICK_CHECK_BUDGET_SECONDS: float = 30.0  # NOT Final — tests monkeypatch this
_BUSY_TIMEOUT_MS: Final = 5000
_CONNECT_TIMEOUT_SECONDS: Final = _BUSY_TIMEOUT_MS / 1000.0  # keep in sync
_WAL_AUTOCHECKPOINT_PAGES: Final = 200
_PROGRESS_HANDLER_INTERVAL_OPS: Final = 1000

# Column tuples match audit-db-path-config.md §6.1 lex-ascending key order.
REQUIRED_COLUMNS: Final = (
    "claim_id",
    "envelope_message_id",
    "outcome",
    "package_id",
    "session_id",
    "signer_id",
    "signer_manifest_digest",
    "source",
    "ts",
)

OPTIONAL_COLUMNS: Final = (
    "aphelion_hash",
    "local_hash",
    "reason_code",
)


def _sql_string_list(values: frozenset[str]) -> str:
    """Render a frozenset of strings as a SQL ``IN (...)`` literal list.

    Sorted for deterministic DDL — schema bytes are stable across runs
    so ``CREATE TABLE IF NOT EXISTS`` does not flap. Single quotes inside
    a value are SQL-doubled (``'`` → ``''``) so a future enum value
    containing an apostrophe (e.g. ``o'brien``) cannot break DDL parsing.
    """
    return ",".join(f"'{v.replace(chr(39), chr(39) * 2)}'" for v in sorted(values))


# Empty enum frozensets generate `outcome IN ()` which SQLite evaluates
# as always-false → every write would silently IntegrityError. Fail fast
# at import time instead so an operator-visible startup error surfaces.
# Bare `assert` would be stripped under `python -O` / PYTHONOPTIMIZE=1
# so use explicit if/raise to survive optimized builds.
if not OUTCOME_VALUES:
    raise AssertionError(
        "parallax.apex.audit_writer.OUTCOME_VALUES must be non-empty; "
        "empty enum would silently reject every audit row at write time"
    )
if not SOURCE_VALUES:
    raise AssertionError(
        "parallax.apex.audit_writer.SOURCE_VALUES must be non-empty; "
        "empty enum would silently reject every audit row at write time"
    )
_OUTCOME_SQL_LITERALS: Final = _sql_string_list(OUTCOME_VALUES)
_SOURCE_SQL_LITERALS: Final = _sql_string_list(SOURCE_VALUES)


# Schema generated FROM the writer's enum constants so the CHECK literals
# cannot drift independently of audit_writer.OUTCOME_VALUES / SOURCE_VALUES.
# A test (test_db_check_matches_writer_enum) asserts this generation.
_SCHEMA_STATEMENTS: Final = (
    f"""
    CREATE TABLE IF NOT EXISTS audit_row (
        id                      INTEGER PRIMARY KEY,
        claim_id                TEXT NOT NULL,
        envelope_message_id     TEXT NOT NULL UNIQUE,
        outcome                 TEXT NOT NULL
            CHECK (outcome IN ({_OUTCOME_SQL_LITERALS})),
        package_id              TEXT NOT NULL,
        session_id              TEXT NOT NULL,
        signer_id               TEXT NOT NULL,
        -- signer_manifest_digest: sha256 hex (64 chars) for signed
        -- packages, OR empty string for unsigned Aphelion packages
        -- (spec §6.1 + audit_writer.canonicalize_row's empty-string
        -- exemption for this column). The empty-string allowance is
        -- a deliberate gap — NULL is still NOT NULL-rejected.
        signer_manifest_digest  TEXT NOT NULL
            CHECK (length(signer_manifest_digest) = 64
                   OR signer_manifest_digest = ''),
        source                  TEXT NOT NULL
            CHECK (source IN ({_SOURCE_SQL_LITERALS})),
        -- ts: defense-in-depth gate matching the writer's strict
        -- 20-char ISO-8601 second-precision UTC format. The writer
        -- (audit_writer.canonicalize_row) is the source-of-truth
        -- validator; this CHECK exists as last-resort sanity for
        -- direct INSERTs that bypass canonicalize_row.
        ts                      TEXT NOT NULL
            CHECK (ts LIKE '____-__-__T__:__:__Z'),
        aphelion_hash           TEXT,
        local_hash              TEXT,
        reason_code             TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_row_ts ON audit_row (ts)",
    "CREATE INDEX IF NOT EXISTS idx_audit_row_session_id ON audit_row (session_id)",
    # Note: no index on `outcome` — 4-value enum has too low cardinality
    # for an index to beat a table scan; the write-amp cost is pure loss.
    """
    CREATE TABLE IF NOT EXISTS audit_db_schema_version (
        version    INTEGER NOT NULL PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
)


class AuditDbConfigError(RuntimeError):
    """Audit-db path / permissions / integrity failed spec §4 startup gates.

    The string form starts with a reason code (e.g. ``EX_CONFIG``,
    ``EX_AUDIT_DB_SLOW_QUICKCHECK``) matching the spec §4 item 8 structured
    log format. Callers at the server-startup boundary translate this
    to process exit code :data:`EX_CONFIG`.
    """


class AuditDbWriteError(RuntimeError):
    """Audit-db write-time failure (disk full, locked, corrupt WAL).

    Distinct from :class:`AuditDbConfigError` so the server-startup
    boundary does not wrongly classify runtime disk-full as a config
    issue worthy of ``EX_CONFIG`` exit. Callers at the envelope-emit
    boundary should log + degrade gracefully (e.g. queue + retry) rather
    than exit the process.
    """


class AuditDbUsageError(RuntimeError):
    """Audit-db caller-contract violation (caller-side bug).

    Raised when :func:`write_audit_row` is given a connection that
    already has an open transaction. Distinct from
    :class:`AuditDbWriteError` so callers can distinguish "I passed the
    wrong conn" (fix the caller) from "the DB had a write-time storage
    failure" (degrade gracefully and retry).
    """


def resolve_audit_db_path(env: Mapping[str, str] | None = None) -> pathlib.Path:
    """Resolve audit-db path from the ``PARALLAX_AUDIT_DB_PATH`` env var.

    Both unset and empty-string are rejected as EX_CONFIG. Production
    code MUST NOT embed operator-specific defaults; the spec §3 contract
    is that the operator sets the env var explicitly, or the server
    refuses to start. See ``.env.example`` for the documented form.

    ``env`` defaults to :data:`os.environ`; an explicit mapping lets
    callers (and tests) override without mutating process state.
    """
    if env is None:
        env = os.environ
    raw = env.get(ENV_VAR_NAME)
    if raw is None:
        raise AuditDbConfigError(
            f"EX_CONFIG: {ENV_VAR_NAME} is not set; spec §3 requires the "
            "audit-db path to be configured explicitly. See .env.example."
        )
    if raw == "":
        raise AuditDbConfigError(
            f"EX_CONFIG: {ENV_VAR_NAME} is set to empty string; "
            "set an absolute path or unset the variable to surface the same error"
        )
    return pathlib.Path(raw)


def _check_absolute_path(path: pathlib.Path) -> None:
    if not path.is_absolute():
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db path must be absolute, got {str(path)!r}"
        )
    if any(part == ".." for part in path.parts):
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db path must not contain '..' segments, got {str(path)!r}"
        )
    # Defense: resolve() collapses well-formed paths and catches malformed
    # UNC / junction / extended-length syntax that pathlib.Path accepts
    # syntactically but the OS rejects at open time. resolve(strict=False)
    # doesn't require the path to exist.
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db path resolution failed for {str(path)!r}: {exc}"
        ) from exc
    if not resolved.is_absolute():
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db path is not absolute after resolution: {resolved}"
        )


def _check_parent_writable(path: pathlib.Path) -> None:
    parent = path.parent
    if not parent.exists():
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db parent directory does not exist: {parent}. "
            "Create it before starting parallax-server (spec §9)."
        )
    if not parent.is_dir():
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db parent path is not a directory: {parent}"
        )
    if not os.access(parent, os.W_OK):
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db parent directory not writable: {parent}"
        )


def _quick_check(conn: sqlite3.Connection) -> None:
    """Spec §4 item 5 — ``PRAGMA quick_check`` within wall-clock budget.

    Uses ``conn.set_progress_handler`` so a stalled quick_check (corrupted
    page, hung filesystem) is proactively aborted instead of hanging the
    server. The post-hoc elapsed check defends against the case where
    quick_check finishes between progress callbacks but still over budget.
    """
    start = time.monotonic()
    timed_out = False

    def _progress() -> int:
        nonlocal timed_out
        # Defense: any exception inside the callback would be silently
        # discarded by CPython sqlite3 (treated as 0/continue), nulling
        # the budget guard. Force abort on internal failure so a broken
        # clock or unexpected error still trips the timeout. Log the
        # exception first so operators can distinguish a genuine budget
        # exhaustion from a programmer error masquerading as a timeout.
        try:
            if time.monotonic() - start > QUICK_CHECK_BUDGET_SECONDS:
                timed_out = True
                return 1  # non-zero return → SQLite aborts the running operation
            return 0
        except BaseException as exc:  # noqa: BLE001 — see above
            _log.warning(
                "audit_db _progress callback raised unexpectedly: %s",
                exc,
                exc_info=True,
            )
            timed_out = True
            return 1

    conn.set_progress_handler(_progress, _PROGRESS_HANDLER_INTERVAL_OPS)
    try:
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            if timed_out:
                raise AuditDbConfigError(
                    f"EX_AUDIT_DB_SLOW_QUICKCHECK: quick_check exceeded "
                    f"{QUICK_CHECK_BUDGET_SECONDS:.0f}s budget (aborted via progress handler)"
                ) from exc
            raise AuditDbConfigError(
                f"EX_CONFIG: audit_db quick_check raised: {exc}"
            ) from exc
    finally:
        conn.set_progress_handler(None, 0)

    elapsed = time.monotonic() - start
    if timed_out or elapsed > QUICK_CHECK_BUDGET_SECONDS:
        raise AuditDbConfigError(
            f"EX_AUDIT_DB_SLOW_QUICKCHECK: quick_check took {elapsed:.3f}s "
            f"> {QUICK_CHECK_BUDGET_SECONDS:.0f}s budget"
        )
    result = row[0] if row is not None else None
    if result != "ok":
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db quick_check returned non-ok: {result!r}"
        )


def _write_probe(conn: sqlite3.Connection) -> None:
    """Spec §4 item 6 — ``BEGIN IMMEDIATE`` + ``ROLLBACK`` to detect read-only files.

    Catches OS-level read-only filesystems, snapshotted backup volumes,
    and stale lockfiles AT STARTUP rather than on first envelope emission.
    All :class:`sqlite3.Error` subclasses (``OperationalError`` on
    read-only/locked DBs, ``DatabaseError`` on a malformed file, etc.)
    are translated to :class:`AuditDbConfigError` so spec §4 item 8 EX_CONFIG
    classification stays consistent — callers that only catch
    :class:`AuditDbConfigError` cannot miss this path.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db write probe failed (read-only or locked): {exc}"
        ) from exc
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error as exc:
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db write probe rollback failed: {exc}"
        ) from exc


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Apply schema idempotently. Atomic across statements via explicit txn."""
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _SCHEMA_STATEMENTS:
                cur.execute(stmt)
            cur.execute(
                "INSERT OR IGNORE INTO audit_db_schema_version (version) VALUES (?)",
                (CURRENT_SCHEMA_VERSION,),
            )
        except Exception:
            # Best-effort rollback; preserve the original exception even if
            # the rollback itself fails (e.g. SQLite already auto-rolled back).
            try:
                cur.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        cur.execute("COMMIT")
    finally:
        cur.close()


def _verify_schema_version_if_present(conn: sqlite3.Connection) -> None:
    """Pre-apply guard: verify version IFF the version table already exists.

    A stale client (code at v=N) opening a DB previously written by
    newer code (DB at v=N+1) must abort BEFORE :func:`_apply_schema`
    runs, or the v=N ``INSERT OR IGNORE INTO audit_db_schema_version``
    would mutate the newer DB. On a truly fresh DB the table does not
    yet exist and this is a no-op; :func:`_verify_schema_version`
    re-checks post-apply as belt-and-braces.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='audit_db_schema_version'"
    ).fetchone()
    if row is None:
        return
    _verify_schema_version(conn)


def _verify_schema_version(conn: sqlite3.Connection) -> None:
    """Refuse to open a DB whose schema version disagrees with the code.

    Reads ``MAX(version)`` so future appended versions (v=2, v=3 …) are
    honored. Mismatch in either direction (DB ahead of code, DB behind
    code with no row, NULL after race) raises EX_CONFIG so the server
    operator sees a clear error instead of writing rows under a
    different schema than expected.
    """
    row = conn.execute(
        "SELECT MAX(version) FROM audit_db_schema_version"
    ).fetchone()
    value = row[0] if row is not None else None
    if value is None:
        raise AuditDbConfigError(
            "EX_CONFIG: audit_db_schema_version table is empty after bootstrap"
        )
    if value != CURRENT_SCHEMA_VERSION:
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db schema_version mismatch: "
            f"expected {CURRENT_SCHEMA_VERSION}, got {value}"
        )


def open_audit_db(
    path: pathlib.Path | str | None = None,
    *,
    validate: bool = True,
) -> sqlite3.Connection:
    """Open the audit DB with pragmas + schema applied.

    Args:
        path: Override path. ``None`` resolves via :func:`resolve_audit_db_path`.
        validate: When ``True`` (default), run spec §4 startup gates
            (absolute path, parent exists+writable, ``PRAGMA quick_check``,
            ``BEGIN IMMEDIATE`` write probe). Set ``False`` only for tests
            that exercise schema/write logic on disposable temp DBs.

    Returns:
        A :class:`sqlite3.Connection` with ``row_factory = sqlite3.Row``,
        autocommit isolation, WAL + ``synchronous=NORMAL`` +
        ``busy_timeout=5000`` ms + ``wal_autocheckpoint=200`` pragmas,
        and the audit-row schema present.

    Raises:
        AuditDbConfigError: when spec §4 gates, schema_version verification,
            or the underlying :func:`sqlite3.connect` call fail (e.g. file
            permission errors, invalid path targets, readonly mount edge
            cases). All :class:`sqlite3.Error` subclasses raised at
            connection-open time are translated to ``AuditDbConfigError``
            so callers that only catch ``AuditDbConfigError`` cover this
            path.
    """
    resolved = pathlib.Path(path) if path is not None else resolve_audit_db_path()

    if validate:
        _check_absolute_path(resolved)
        _check_parent_writable(resolved)

    # ``timeout=N`` makes sqlite3 call ``sqlite3_busy_timeout(N×1000)``
    # before any user statement runs (spec §4 item 4 "busy_timeout FIRST");
    # the explicit PRAGMA below re-applies the same value for traceability.
    # Translate connect-time sqlite3.Error subclasses to AuditDbConfigError
    # so spec §4 item 8 EX_CONFIG signaling matches the other startup gates;
    # see the docstring for the enumerated failure modes covered.
    try:
        conn = sqlite3.connect(
            str(resolved),
            isolation_level=None,
            timeout=_CONNECT_TIMEOUT_SECONDS,
        )
    except sqlite3.Error as exc:
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db connection open failed for {str(resolved)!r}: {exc}"
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA wal_autocheckpoint = {_WAL_AUTOCHECKPOINT_PAGES}")
        if validate:
            _quick_check(conn)
            _write_probe(conn)
        # Verify BEFORE applying schema so stale code (v=N) opening a
        # newer DB (v=N+1) does not mutate the version table via the
        # INSERT OR IGNORE in _apply_schema.
        _verify_schema_version_if_present(conn)
        _apply_schema(conn)
        _verify_schema_version(conn)
    except BaseException:
        # Suppress secondary close() failures so the original
        # AuditDbConfigError reaches the caller. Log the close failure
        # so an operator can investigate (silently swallowing it would
        # mask file-handle leaks or FS-level errors).
        try:
            conn.close()
        except Exception as close_exc:
            _log.warning(
                "audit_db connection close failed during error handling: %s",
                close_exc,
            )
        raise
    return conn


def write_audit_row(conn: sqlite3.Connection, row: AuditRow) -> None:
    """Insert one canonicalized audit row.

    The caller MUST construct ``row`` via
    :func:`parallax.apex.audit_writer.canonicalize_row` — that function
    enforces §6 schema invariants (required fields, enum values,
    optional pairing rules) before the row reaches the DB. This writer
    additionally re-runs canonicalize_row as a belt-and-braces guard so
    callers cannot bypass validation by constructing :class:`AuditRow`
    directly. The DB schema adds belt-and-braces CHECK on outcome/source
    plus the UNIQUE constraint on ``envelope_message_id``.

    Wrapped in an explicit ``BEGIN IMMEDIATE`` → ``COMMIT`` so the
    insert is atomic in autocommit mode. Raises :class:`sqlite3.IntegrityError`
    on UNIQUE collision (envelope_message_id already inserted) or CHECK
    violation; raises :class:`AuditRowValidationError` if the row data
    does not pass canonicalize_row.
    """
    # Precondition: writer expects an autocommit connection. If the
    # caller passes a conn that already has an open transaction the
    # BEGIN IMMEDIATE below would raise "cannot start a transaction
    # within a transaction" and leave the caller's txn dirty. Trip
    # early with a clearer error so the contract violation is visible.
    # The getattr fallback to False is intentional for FakeConn used in
    # tests; production sqlite3.Connection always exposes in_transaction.
    if getattr(conn, "in_transaction", False):
        raise AuditDbUsageError(
            "write_audit_row requires a connection in autocommit state "
            "(conn.in_transaction must be False)"
        )

    # Belt-and-braces: re-validate. canonicalize_row is idempotent on
    # already-validated rows; cost is microseconds vs the cost of a
    # divergent audit chain caused by a row that skipped validation.
    canonicalize_row(dict(row.data))

    data = row.data
    columns: list[str] = []
    values: list[Any] = []
    for col in REQUIRED_COLUMNS:
        columns.append(col)
        values.append(data[col])
    for col in OPTIONAL_COLUMNS:
        if col in data:
            columns.append(col)
            values.append(data[col])
    placeholders = ",".join("?" * len(columns))
    sql = f"INSERT INTO audit_row ({','.join(columns)}) VALUES ({placeholders})"

    # BEGIN / INSERT / COMMIT all wrapped so callers see a single
    # AuditDbWriteError contract instead of raw sqlite3.Error variants.
    # ROLLBACK is best-effort on the failure paths; a secondary
    # rollback failure is logged but does not mask the primary error.
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise AuditDbWriteError(
            f"audit_db begin-immediate failed (locked or busy): {exc}"
        ) from exc
    try:
        conn.execute(sql, values)
    except Exception:
        # sqlite3.Error covers OperationalError + DatabaseError so a
        # corrupt-WAL ROLLBACK failure does not replace the original
        # INSERT exception in the caller's traceback.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as rb_exc:
            _log.warning(
                "audit_db rollback after insert-failure also raised: %s",
                rb_exc,
            )
        raise
    try:
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as rb_exc:
            _log.warning(
                "audit_db rollback after commit-failure also raised: %s",
                rb_exc,
            )
        raise AuditDbWriteError(
            f"audit_db commit failed (disk full or locked): {exc}"
        ) from exc


# Per-thread audit-db connection cache. sqlite3 connections are not
# thread-safe, and the Apex M5 write path (AphelionReadAdapter.query) runs
# inside DualReadRouter's ThreadPoolExecutor worker threads — so each worker
# thread gets its own connection, matching this module's "each producer
# thread/process gets its own connection" contract and SQLite's WAL design
# (concurrent writers, one connection each, no shared-connection lock).
#
# NOTE(apex-m5): there is no explicit per-thread close hook. A connection is
# released when its owning worker thread terminates, or at process exit.
# Under the current wiring DualReadRouter is constructed per-request in
# query._dispatch_with_router, so its ThreadPoolExecutor (and worker threads)
# are short-lived — connections are reclaimed as those threads die, but the
# per-thread *reuse* this cache is built for only fully pays off once a
# shared, long-lived executor is wired. That executor-lifecycle change is the
# deferred per-request-construction refactor (see progress.txt); an explicit
# shutdown hook is out of scope for this M5 persistence follow-up.
_thread_local = threading.local()


def get_thread_local_audit_conn(path: pathlib.Path | str) -> sqlite3.Connection:
    """Return the calling thread's audit-db connection, opening it lazily.

    The first call on a given thread opens the connection via
    :func:`open_audit_db` with ``validate=False`` — the expensive spec §4
    startup gates (``PRAGMA quick_check`` within a 30 s budget, the
    ``BEGIN IMMEDIATE`` write probe) already ran once at server boot in
    :func:`parallax.server.lifespan.parallax_lifespan`, so re-running them
    per worker thread would be pure latency with no added safety. The WAL
    pragmas and the idempotent ``CREATE TABLE IF NOT EXISTS`` schema are
    still applied on every open. Subsequent calls on the same thread return
    the cached connection.

    The process is expected to use a single audit-db path for its lifetime
    (``app.state.audit_db_path``, set once at boot). ``path`` is therefore
    only consumed on the first call per thread; a later call on the same
    thread with a *different* path raises :class:`AuditDbUsageError` rather
    than silently returning a connection to the wrong database.
    """
    resolved = pathlib.Path(path)
    conn = getattr(_thread_local, "audit_conn", None)
    if conn is None:
        conn = open_audit_db(resolved, validate=False)
        _thread_local.audit_conn = conn
        _thread_local.audit_path = resolved
        return conn
    cached_path = getattr(_thread_local, "audit_path", None)
    if cached_path != resolved:
        raise AuditDbUsageError(
            f"get_thread_local_audit_conn called with {str(resolved)!r} but this "
            f"thread already cached a connection for {str(cached_path)!r}; the "
            "process must use a single audit-db path for its lifetime"
        )
    return conn
