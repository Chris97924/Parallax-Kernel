"""Apex M5 audit DB — connection bootstrap + schema + row writer.

Pins ``docs/m5-prep/audit-db-path-config.md`` §3-§6 normative contract:

  * §3 — ``PARALLAX_AUDIT_DB_PATH`` env var with OS-family default
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
``BEGIN IMMEDIATE`` around the writer's INSERT, mirroring
:mod:`parallax.canary.audit_log`. Each producer thread/process gets its
own connection via :func:`open_audit_db`.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import time
from collections.abc import Mapping
from typing import Any, Final

from parallax.apex.audit_writer import (
    OUTCOME_VALUES,
    SOURCE_VALUES,
    AuditRow,
    canonicalize_row,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "ENV_VAR_NAME",
    "EX_CONFIG",
    "OPTIONAL_COLUMNS",
    "QUICK_CHECK_BUDGET_SECONDS",
    "REQUIRED_COLUMNS",
    "AuditDbConfigError",
    "open_audit_db",
    "resolve_audit_db_path",
    "write_audit_row",
]

ENV_VAR_NAME: Final = "PARALLAX_AUDIT_DB_PATH"
EX_CONFIG: Final = 78  # sysexits.h EX_CONFIG — spec §4.5
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
    so ``CREATE TABLE IF NOT EXISTS`` does not flap.
    """
    return ",".join(f"'{v}'" for v in sorted(values))


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
        signer_manifest_digest  TEXT NOT NULL,
        source                  TEXT NOT NULL
            CHECK (source IN ({_SOURCE_SQL_LITERALS})),
        ts                      TEXT NOT NULL,
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
    ``EX_AUDIT_DB_SLOW_QUICKCHECK``) matching the spec §4.5 structured
    log format. Callers at the server-startup boundary translate this
    to process exit code :data:`EX_CONFIG`.
    """


def _default_path() -> pathlib.Path:
    """OS-family default audit-db path per spec §3."""
    if sys.platform.startswith("win"):
        return pathlib.Path(r"E:\Parallax\data\audit.db")
    return pathlib.Path("/home/chris/parallax-kernel/db/audit.db")


def resolve_audit_db_path(env: Mapping[str, str] | None = None) -> pathlib.Path:
    """Resolve audit-db path from env var with OS-family fallback.

    Empty-string env var (``PARALLAX_AUDIT_DB_PATH=``) is rejected as
    EX_CONFIG — silent fallback to default would mask operator intent.
    Unset env var (key absent) uses the OS-family default per spec §3.

    ``env`` defaults to :data:`os.environ`; an explicit mapping lets
    callers (and tests) override without mutating process state.
    """
    if env is None:
        env = os.environ
    raw = env.get(ENV_VAR_NAME)
    if raw is None:
        return _default_path()
    if raw == "":
        raise AuditDbConfigError(
            f"EX_CONFIG: {ENV_VAR_NAME} is set to empty string; "
            "unset the variable to use the default, or set an absolute path"
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
            "Create it before starting parallax-server (spec §6 Chris-action 1+2)."
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
    """Spec §4.3 — ``PRAGMA quick_check`` within wall-clock budget.

    Uses ``conn.set_progress_handler`` so a stalled quick_check (corrupted
    page, hung filesystem) is proactively aborted instead of hanging the
    server. The post-hoc elapsed check defends against the case where
    quick_check finishes between progress callbacks but still over budget.
    """
    start = time.monotonic()
    timed_out = False

    def _progress() -> int:
        nonlocal timed_out
        if time.monotonic() - start > QUICK_CHECK_BUDGET_SECONDS:
            timed_out = True
            return 1  # non-zero return → SQLite aborts the running operation
        return 0

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
    """Spec §4.3 — ``BEGIN IMMEDIATE`` + ``ROLLBACK`` to detect read-only files.

    Catches OS-level read-only filesystems, snapshotted backup volumes,
    and stale lockfiles AT STARTUP rather than on first envelope emission.
    Failures (including the rollback path) surface as
    :class:`AuditDbConfigError` so spec §4.5 EX_CONFIG logging fires.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise AuditDbConfigError(
            f"EX_CONFIG: audit_db write probe failed (read-only or locked): {exc}"
        ) from exc
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
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
        AuditDbConfigError: when spec §4 gates or schema_version
            verification fail.
    """
    resolved = pathlib.Path(path) if path is not None else resolve_audit_db_path()

    if validate:
        _check_absolute_path(resolved)
        _check_parent_writable(resolved)

    # Note on PRAGMA busy_timeout ordering (spec §4.3 "FIRST"):
    # ``sqlite3.connect(timeout=N)`` internally calls
    # ``sqlite3_busy_timeout()`` to N×1000 ms before any user code runs.
    # We pass ``_CONNECT_TIMEOUT_SECONDS = _BUSY_TIMEOUT_MS / 1000`` so
    # the connect-time default already matches the spec value, then
    # re-apply via PRAGMA as the first user statement for traceability.
    # If the two constants ever diverge the file-level Final binding
    # makes the mismatch a static-analysis lint, not a runtime surprise.
    conn = sqlite3.connect(
        str(resolved),
        isolation_level=None,
        timeout=_CONNECT_TIMEOUT_SECONDS,
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA wal_autocheckpoint = {_WAL_AUTOCHECKPOINT_PAGES}")
        if validate:
            _quick_check(conn)
            _write_probe(conn)
        _apply_schema(conn)
        _verify_schema_version(conn)
    except BaseException:
        # Suppress secondary close() failures so the original
        # AuditDbConfigError reaches the caller.
        try:
            conn.close()
        except Exception:
            pass
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

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(sql, values)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    conn.execute("COMMIT")
