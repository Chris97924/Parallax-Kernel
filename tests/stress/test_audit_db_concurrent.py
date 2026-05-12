"""Concurrency stress for parallax.apex.audit_db.

Three scenarios:

* Unique inserts: 10 threads × 100 unique envelope_message_ids → 1000
  rows persisted. Exercises WAL + busy_timeout fairness; each thread
  opens its own connection (SQLite check_same_thread).
* Duplicate inserts: 10 threads attempt the SAME envelope_message_id →
  exactly 1 INSERT succeeds, the other 9 raise IntegrityError on the
  PK constraint. Proves the PK race is decided correctly under load.
* Read-while-write: a writer thread inserts 200 rows in WAL mode while
  a reader thread issues SELECT COUNT(*) repeatedly. The reader MUST
  observe monotonically non-decreasing counts and complete without
  raising, demonstrating that WAL reads are not blocked by writers.
"""

from __future__ import annotations

import pathlib
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from parallax.apex.audit_db import open_audit_db, write_audit_row
from parallax.apex.audit_writer import canonicalize_row

pytestmark = pytest.mark.integration

_THREADS = 10
_ITERS = 100


def _v7_hex(seed: int) -> str:
    """Synthesize a UUID v7-shaped hex (lex-sortable timestamp prefix).

    The audit-row schema enforces UUID v7 format on claim_id /
    package_id via parallax.apex.audit_writer validation; we only need
    distinct values, not real time-ordered UUIDs.
    """
    base = f"0193e2b1-0001-7000-8000-{seed:012x}"
    return base


def _v4_hex(_state: dict[str, int] | None = None) -> str:
    return str(uuid.uuid4())


def _valid_row_for_thread(seed: int) -> dict[str, object]:
    return {
        "claim_id": _v7_hex(seed),
        "envelope_message_id": _v4_hex(),
        "outcome": "hit",
        "package_id": _v7_hex(seed + 1_000_000),
        "session_id": f"sess-stress-{seed}",
        "signer_id": "stress@aphelion-graph",
        "signer_manifest_digest": "f" * 64,
        "source": "aphelion",
        "ts": "2026-05-09T14:23:11Z",
    }


# ---------------------------------------------------------------------------
# Scenario 1: unique inserts
# ---------------------------------------------------------------------------


def _worker_unique(db_path: pathlib.Path, thread_idx: int, iters: int) -> int:
    conn = open_audit_db(db_path, validate=False)
    try:
        n = 0
        for i in range(iters):
            row = canonicalize_row(_valid_row_for_thread(thread_idx * iters + i))
            write_audit_row(conn, row)
            n += 1
        return n
    finally:
        conn.close()


def test_unique_inserts_across_threads(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "audit.db"
    # Bootstrap on the main thread first to avoid CREATE TABLE races on
    # an empty file; spec §6 expects bootstrap on first envelope but
    # tests pre-create to focus the stress on INSERT contention.
    open_audit_db(db, validate=False).close()

    with ThreadPoolExecutor(max_workers=_THREADS) as pool:
        futures = [
            pool.submit(_worker_unique, db, t, _ITERS) for t in range(_THREADS)
        ]
        committed = [f.result() for f in as_completed(futures)]
    assert sum(committed) == _THREADS * _ITERS

    audit_conn = sqlite3.connect(str(db))
    try:
        (count,) = audit_conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
    finally:
        audit_conn.close()
    assert count == _THREADS * _ITERS, (
        f"expected {_THREADS * _ITERS} rows, got {count}"
    )


# ---------------------------------------------------------------------------
# Scenario 2: duplicate envelope_message_id — exactly one winner
# ---------------------------------------------------------------------------


def _worker_duplicate(
    db_path: pathlib.Path,
    duplicate_emid: str,
    barrier: threading.Barrier,
) -> str:
    """Return 'committed' or 'integrity_error'."""
    conn = open_audit_db(db_path, validate=False)
    try:
        row = canonicalize_row(
            {
                "claim_id": _v7_hex(42),
                "envelope_message_id": duplicate_emid,
                "outcome": "hit",
                "package_id": _v7_hex(43),
                "session_id": "sess-dup-race",
                "signer_id": "race@aphelion-graph",
                "signer_manifest_digest": "0" * 64,
                "source": "aphelion",
                "ts": "2026-05-09T14:23:11Z",
            }
        )
        barrier.wait(timeout=10)
        try:
            write_audit_row(conn, row)
            return "committed"
        except sqlite3.IntegrityError:
            return "integrity_error"
    finally:
        conn.close()


def test_duplicate_envelope_id_race_has_single_winner(
    tmp_path: pathlib.Path,
) -> None:
    db = tmp_path / "audit.db"
    open_audit_db(db, validate=False).close()

    duplicate_emid = str(uuid.uuid4())
    barrier = threading.Barrier(_THREADS)
    with ThreadPoolExecutor(max_workers=_THREADS) as pool:
        futures = [
            pool.submit(_worker_duplicate, db, duplicate_emid, barrier)
            for _ in range(_THREADS)
        ]
        results = [f.result() for f in as_completed(futures)]

    committed_count = results.count("committed")
    error_count = results.count("integrity_error")
    assert committed_count == 1, f"exactly one winner expected, got {committed_count}"
    assert error_count == _THREADS - 1, (
        f"expected {_THREADS - 1} IntegrityError losers, got {error_count}"
    )

    # The single row survived in the DB.
    audit_conn = sqlite3.connect(str(db))
    try:
        rows = audit_conn.execute(
            "SELECT COUNT(*) FROM audit_row WHERE envelope_message_id = ?",
            (duplicate_emid,),
        ).fetchone()
    finally:
        audit_conn.close()
    assert rows[0] == 1


# ---------------------------------------------------------------------------
# Scenario 3: WAL read-while-write non-blocking
# ---------------------------------------------------------------------------


def _wal_reader(db_path: pathlib.Path, stop_event: threading.Event) -> list[int]:
    """Poll SELECT COUNT(*) while writers are active. Records the count series."""
    conn = open_audit_db(db_path, validate=False)
    try:
        series: list[int] = []
        while not stop_event.is_set():
            (n,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
            series.append(n)
            time.sleep(0.001)
        # One final reading after the writer finishes.
        (n,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        series.append(n)
        return series
    finally:
        conn.close()


def test_wal_reads_not_blocked_by_writes(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "audit.db"
    open_audit_db(db, validate=False).close()

    stop = threading.Event()
    reader_count_series: list[int] = []

    def reader_target() -> None:
        nonlocal reader_count_series
        reader_count_series = _wal_reader(db, stop)

    reader = threading.Thread(target=reader_target, daemon=True)
    reader.start()
    try:
        # Writer thread: insert 200 unique rows.
        for i in range(200):
            conn = open_audit_db(db, validate=False)
            try:
                row = canonicalize_row(_valid_row_for_thread(5_000_000 + i))
                write_audit_row(conn, row)
            finally:
                conn.close()
    finally:
        stop.set()
        reader.join(timeout=10)

    assert reader_count_series, "reader recorded zero samples"
    # Monotonic non-decreasing: WAL readers see a stable snapshot per
    # statement, so each successive SELECT sees ≥ previous.
    for prev, curr in zip(reader_count_series, reader_count_series[1:]):
        assert curr >= prev, (
            f"reader saw count regression {prev} -> {curr}; series={reader_count_series}"
        )
    # Final count matches the 200 writes.
    assert reader_count_series[-1] == 200
