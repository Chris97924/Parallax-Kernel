"""Mutation-hardening for ``parallax.router.backfill`` (land-20260823 w4 S2).

Additive companion to ``test_backfill_runner.py`` and ``test_backfill_security.py``.
Every test below exists because a semantic mutant of the module SURVIVED those
suites. 34 mutants were applied one at a time; only a handful died against the
existing tests.

What the existing suites are blind to, and why
----------------------------------------------

  * **Nothing ever exceeds a limit.** The largest corpus any existing test
    builds is five rows, so ``limit = 50 if scope == "sample" else
    _MAX_BACKFILL_ROWS`` is never exercised: 50, 500 and the inverted selector
    all return the same five rows. The same emptiness hides ``ORDER BY
    created_at DESC`` — with fewer rows than the limit, which end of the sort
    you take from does not matter, and ``ingest_claim`` stamps whole corpora
    inside the same clock tick anyway, so even a bigger corpus would tie. The
    tests below build 60 rows and then rewrite ``created_at`` to distinct
    values, which is what makes "newest first" observable at all.

  * **The chunked digest is only ever asserted to be 64 characters long.**
    ``test_table_snapshot_chunked`` checks ``len(digest) == 64`` and
    ``test_table_snapshot_chunked_with_data`` checks that two calls agree with
    each other — true of any hash of anything, including one that drops its
    field separators or collapses NULL into the empty string. And
    ``test_table_snapshot_chunk_boundary`` says in its own comment that it does
    not insert 1000+ rows, so the ``count < _CHUNK_SIZE`` loop exit is never
    reached with a full chunk. Here the digest is pinned to a literal sha256
    and the chunk boundary is driven with a patched-small chunk size.

  * **The retry ladder is asserted only by its effect, never its shape.**
    ``test_backfill_sqlite_busy_retry`` patches ``time.sleep`` away entirely and
    asserts ``busy_remaining == 0``, so the delays, the number of attempts, the
    "only retry SQLITE_BUSY" discriminator and the terminal ``_SqliteBusyError``
    are all unobserved. The tests below record what ``sleep`` was actually
    asked to wait.

  * **The crosswalk rows are counted, never read.**
    ``test_dry_run_false_writes_crosswalk`` ends at ``crosswalk_rows >= 2``, so
    every column the writer plumbs — the ``claim:``/``memory:`` canonical_ref
    prefix, target_kind, content_hash, source_id, vault_path and query_type —
    can be swapped or renamed freely.

  * **``plan_upserts`` has no test at all.** Its only caller is
    ``parallax/cli.py:872``. Its limit, its default scope and its sort are
    completely unobserved.

  * **The invariants are only proven in the direction that already holds.** The
    dry-run no-write check has no test that makes a dry run write; it is only
    ever run against an ``_enumerate`` that correctly writes nothing.
"""

from __future__ import annotations

import hashlib
import sqlite3
import unittest.mock

import pytest

import parallax.router.backfill as _backfill_mod
from parallax.ingest import ingest_claim, ingest_memory
from parallax.router.backfill import (
    _BUSY_DELAYS,
    _CHUNK_SIZE,
    _MAX_BACKFILL_ROWS,
    _SNAPSHOT_TABLES,
    BackfillRunner,
    _classify_claim_predicate,
    _core_fingerprint,
    _crosswalk_exists,
    _SqliteBusyError,
    _table_snapshot,
)
from parallax.router.contracts import BackfillRequest

_USER = "harden_backfill_user"


def _request(**kwargs: object) -> BackfillRequest:
    fields: dict[str, object] = {
        "user_id": _USER,
        "crosswalk_version": "harden_v1",
        "dry_run": True,
        "scope": "sample",
    }
    fields.update(kwargs)
    return BackfillRequest(**fields)  # type: ignore[arg-type]


def _seed_claims_with_distinct_times(conn: sqlite3.Connection, count: int) -> list[str]:
    """Ingest *count* claims and give each a distinct ``created_at``.

    ``ingest_claim`` stamps every row with ``now_iso()``, so a corpus written in
    one test shares a single timestamp and the ``created_at DESC`` sort degrades
    to the ``claim_id ASC`` tie-break. Rewriting the column afterwards is what
    makes "newest first" a decidable question.

    Returns the claim ids ordered oldest-first, so ``ids[-n:]`` is the newest n.
    """
    ids: list[str] = []
    for i in range(count):
        ids.append(
            ingest_claim(
                conn,
                user_id=_USER,
                subject=f"subject-{i:03d}",
                predicate="prefers",
                object_=f"object-{i:03d}",
            )
        )
    for i, claim_id in enumerate(ids):
        conn.execute(
            "UPDATE claims SET created_at = ? WHERE claim_id = ?",
            (f"2020-01-01T00:{i:02d}:00+00:00", claim_id),
        )
    conn.commit()
    return ids


def _seed_memories_with_distinct_times(conn: sqlite3.Connection, count: int) -> list[str]:
    """``_seed_claims_with_distinct_times`` for the memories table."""
    ids: list[str] = []
    for i in range(count):
        ids.append(
            ingest_memory(
                conn,
                user_id=_USER,
                title=f"title-{i:03d}",
                summary=f"summary-{i:03d}",
                vault_path=f"path-{i:03d}.md",
            )
        )
    for i, memory_id in enumerate(ids):
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE memory_id = ?",
            (f"2020-01-01T00:{i:02d}:00+00:00", memory_id),
        )
    conn.commit()
    return ids


# ---------------------------------------------------------------------------
# The row limits, which no existing corpus is big enough to reach
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_two_row_limits_are_the_shipped_values() -> None:
    """50 for a sample, a 10k hard cap for everything else.

    Both are memory/latency contract — the module docstring calls
    ``_MAX_BACKFILL_ROWS`` an "H-1 hard cap: scope='all' still bounded to
    protect memory / latency". The hard cap cannot be reached in a unit test
    without inserting ten thousand rows, so it is pinned as a literal here and
    the sample limit is proved behaviourally below.
    """
    assert _MAX_BACKFILL_ROWS == 10_000


@pytest.mark.unit
def test_scope_sample_stops_at_fifty_rows(conn: sqlite3.Connection) -> None:
    """A 60-row corpus must yield exactly 50 examined rows under scope=sample.

    Every existing test uses a corpus of five rows or fewer, so the sample
    limit is never reached and 50 is indistinguishable from 500 — or from the
    inverted selector that hands ``sample`` the 10k cap.
    """
    _seed_claims_with_distinct_times(conn, 60)

    report = BackfillRunner(conn).run(_request(scope="sample"))

    assert report.rows_examined == 50


@pytest.mark.unit
def test_scope_all_takes_the_whole_corpus(conn: sqlite3.Connection) -> None:
    """The counter-test: scope='all' is NOT capped at 50.

    Without this, "sample means 50" could be satisfied by a mutant that caps
    every scope at 50, and the selector itself would stay unobserved.
    """
    _seed_claims_with_distinct_times(conn, 60)

    report = BackfillRunner(conn).run(_request(scope="all"))

    assert report.rows_examined == 60


def _written_canonical_refs(conn: sqlite3.Connection) -> set[str]:
    """The canonical_refs a write-mode run actually put in the crosswalk."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT canonical_ref FROM crosswalk WHERE user_id = ?", (_USER,)
        )
    }


@pytest.mark.unit
def test_a_claim_sample_backfills_the_newest_rows_not_the_oldest(
    conn: sqlite3.Connection,
) -> None:
    """``ORDER BY created_at DESC`` decides WHICH 50 of 60 claims get written.

    This is the assertion the whole ``_seed_claims_with_distinct_times`` helper
    exists for: with a corpus smaller than the limit, or with every row sharing
    one timestamp, ASC and DESC select the same set and the sort is invisible.
    A sample that silently takes the oldest 50 rows would make the backfill
    permanently blind to recent data — and would keep re-backfilling the same
    ancient rows on every run.

    ``run(dry_run=False)`` is used rather than ``plan_upserts`` because the two
    carry SEPARATE copies of this ORDER BY; the plan-side copy has its own test
    below.
    """
    ids_oldest_first = _seed_claims_with_distinct_times(conn, 60)
    newest_fifty = {f"claim:{claim_id}" for claim_id in ids_oldest_first[10:]}

    BackfillRunner(conn).run(_request(dry_run=False, scope="sample"))

    assert _written_canonical_refs(conn) == newest_fifty


@pytest.mark.unit
def test_a_memory_sample_backfills_the_newest_rows_not_the_oldest(
    conn: sqlite3.Connection,
) -> None:
    """The memories enumeration carries its own ORDER BY and its own mutant."""
    ids_oldest_first = _seed_memories_with_distinct_times(conn, 60)
    newest_fifty = {f"memory:{memory_id}" for memory_id in ids_oldest_first[10:]}

    BackfillRunner(conn).run(_request(dry_run=False, scope="sample"))

    assert _written_canonical_refs(conn) == newest_fifty


@pytest.mark.unit
def test_plan_upserts_lists_the_newest_claims_not_the_oldest(
    conn: sqlite3.Connection,
) -> None:
    """``plan_upserts`` has its own ``ORDER BY created_at DESC`` — and no test.

    A plan that previews the oldest 50 rows while ``apply`` writes the newest 50
    is worse than no preview at all.
    """
    ids_oldest_first = _seed_claims_with_distinct_times(conn, 60)
    newest_fifty = set(ids_oldest_first[10:])

    planned = BackfillRunner(conn).plan_upserts(_USER, scope="sample")

    assert {entry["target_id"] for entry in planned} == newest_fifty


@pytest.mark.unit
def test_plan_upserts_lists_the_newest_memories_not_the_oldest(
    conn: sqlite3.Connection,
) -> None:
    """The plan's memories query carries a fourth copy of the same ORDER BY."""
    ids_oldest_first = _seed_memories_with_distinct_times(conn, 60)
    newest_fifty = set(ids_oldest_first[10:])

    planned = BackfillRunner(conn).plan_upserts(_USER, scope="sample")

    assert {entry["target_id"] for entry in planned} == newest_fifty


# ---------------------------------------------------------------------------
# plan_upserts — no existing test at all
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plan_upserts_defaults_to_a_sample(conn: sqlite3.Connection) -> None:
    """The default scope is ``sample``; the CLI's plan path relies on it.

    ``parallax/cli.py`` passes ``scope="all"`` explicitly, so the default is
    reachable only by other callers — and by nothing that tests it. A default
    flipped to ``all`` turns an interactive `backfill plan` into a 10k-row scan.
    """
    _seed_claims_with_distinct_times(conn, 60)

    assert len(BackfillRunner(conn).plan_upserts(_USER)) == 50


@pytest.mark.unit
def test_plan_upserts_returns_rows_sorted_by_canonical_ref(
    conn: sqlite3.Connection,
) -> None:
    """The plan is a human-readable diff, so its order is the contract.

    Memories are ingested first here and claims second, and each query returns
    newest-first, so the natural concatenation order is neither the sorted
    order nor the insertion order. Only the explicit ``planned.sort(...)``
    produces the assertion below.
    """
    _seed_memories_with_distinct_times(conn, 3)
    _seed_claims_with_distinct_times(conn, 3)

    planned = BackfillRunner(conn).plan_upserts(_USER, scope="all")
    refs = [entry["canonical_ref"] for entry in planned]

    assert len(refs) == 6
    assert refs == sorted(refs)
    assert refs[0].startswith("claim:"), "claim: sorts before memory:"
    assert refs[-1].startswith("memory:")


# ---------------------------------------------------------------------------
# The chunked content digest
# ---------------------------------------------------------------------------


@pytest.fixture()
def two_column_memories() -> sqlite3.Connection:
    """A standalone DB whose ``memories`` table holds arbitrary text values.

    ``_table_snapshot`` refuses any identifier outside ``_SNAPSHOT_TABLES``, so
    reusing the allowlisted name is what lets these tests feed it hand-chosen
    values (empty strings, NULLs, ambiguous concatenations) that the real
    memories schema cannot express. It is an isolated in-memory database; no
    production schema is involved.
    """
    raw = sqlite3.connect(":memory:")
    raw.execute("CREATE TABLE memories (a TEXT, b TEXT)")
    yield raw
    raw.close()


@pytest.mark.unit
def test_the_snapshot_digest_is_byte_exact(two_column_memories: sqlite3.Connection) -> None:
    """The digest of a fixed two-row table is pinned to a literal sha256.

    Every existing assertion about this digest is shape-only — 64 characters
    long, and equal to itself on a second call — which is true of any hash of
    any encoding. That leaves the encoding free to drift: the 0x1f field
    prefix, the 0x1e field terminator, the per-row newline and the ``<NULL>``
    sentinel can each be dropped or changed without a red test, even though the
    digest is the exact thing ``run()`` compares before and after to prove it
    did not write to a core table.

    The expectation is spelled out as the exact byte sequence the encoding is
    supposed to produce — 0x1f before every value, 0x1e after it, ``<NULL>`` for
    a NULL, and a newline between rows — written here as a literal rather than
    rebuilt from the module's own separators, which would reproduce the
    blindness this test exists to remove. Two rows of two columns, one of them
    a NULL beside an empty string, is the smallest fixture that exercises all
    four elements.
    """
    two_column_memories.execute("INSERT INTO memories VALUES ('ab', 'c')")
    two_column_memories.execute("INSERT INTO memories VALUES (NULL, '')")

    snapshot = _table_snapshot(two_column_memories, "memories")

    expected_encoding = (
        b"\x1fab\x1e\x1fc\x1e\n"  # row 1: "ab", "c"
        b"\x1f<NULL>\x1e\x1f\x1e\n"  # row 2: NULL, ""
    )
    assert snapshot["count"] == 2
    assert snapshot["digest"] == hashlib.sha256(expected_encoding).hexdigest()


@pytest.mark.unit
def test_a_null_and_an_empty_string_do_not_digest_alike(
    two_column_memories: sqlite3.Connection,
) -> None:
    """``<NULL>`` is a sentinel, not decoration.

    Collapsing NULL to the empty string makes "the column was never set" and
    "the column was set to ''" the same fingerprint, so a write that flips one
    into the other would pass the read-only core invariant undetected.
    """
    two_column_memories.execute("INSERT INTO memories VALUES (NULL, 'x')")
    with_null = _table_snapshot(two_column_memories, "memories")["digest"]

    two_column_memories.execute("DELETE FROM memories")
    two_column_memories.execute("INSERT INTO memories VALUES ('', 'x')")
    with_empty = _table_snapshot(two_column_memories, "memories")["digest"]

    assert with_null != with_empty


@pytest.mark.unit
def test_the_chunk_size_is_one_thousand_rows() -> None:
    """The streaming chunk size is a memory contract (MED-1) and nothing reads it."""
    assert _CHUNK_SIZE == 1000


@pytest.mark.unit
def test_a_table_that_is_an_exact_multiple_of_the_chunk_size_is_fully_hashed(
    two_column_memories: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``count < _CHUNK_SIZE`` loop exit, at the boundary that decides it.

    ``test_table_snapshot_chunk_boundary`` says in its own comment that it does
    not insert 1000+ rows, so the loop never returns a FULL chunk and the exit
    condition is untested. Relaxing it to ``count <= _CHUNK_SIZE`` truncates
    every table whose size is an exact multiple of the chunk — silently
    fingerprinting only the first chunk.

    The chunk size is patched small rather than inserting 1000 rows: the
    boundary being tested is the comparison, not the number. The literal 1000
    is pinned separately above so both halves are covered.
    """
    monkeypatch.setattr(_backfill_mod, "_CHUNK_SIZE", 4)
    for i in range(8):
        two_column_memories.execute("INSERT INTO memories VALUES (?, ?)", (str(i), "x"))

    snapshot = _table_snapshot(two_column_memories, "memories")

    assert snapshot["count"] == 8, "an exact multiple of the chunk size must not truncate"


@pytest.mark.unit
def test_the_digest_does_not_depend_on_the_chunk_size(
    two_column_memories: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunking is an implementation detail; the digest must not move with it."""
    for i in range(9):
        two_column_memories.execute("INSERT INTO memories VALUES (?, ?)", (str(i), "y"))

    digests = set()
    for chunk in (3, 4, 1000):
        monkeypatch.setattr(_backfill_mod, "_CHUNK_SIZE", chunk)
        digests.add(_table_snapshot(two_column_memories, "memories")["digest"])

    assert len(digests) == 1, f"chunk size changed the digest: {digests}"


# ---------------------------------------------------------------------------
# The identifier allowlist (there is no bind parameter for a table name)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_snapshot_table_allowlist_is_the_five_shipped_tables() -> None:
    """The allowlist is the only thing standing between the f-string and a caller.

    ``_table_snapshot`` interpolates the table name straight into
    ``SELECT * FROM "{table}"`` because sqlite has no bind parameter for an
    identifier; the source says so. Widening the set is therefore a widening of
    what a caller can make that f-string say, and nothing observes its contents.
    """
    assert _SNAPSHOT_TABLES == frozenset(
        {"events", "claims", "memories", "decisions", "crosswalk"}
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "table",
    [
        "sources",
        "sqlite_master",
        "index_state",
        'x" ; DROP TABLE claims; --',
    ],
)
def test_a_table_outside_the_allowlist_is_refused(
    conn: sqlite3.Connection, table: str
) -> None:
    """Every rejected identifier class, including ones that really exist.

    No existing test calls ``_table_snapshot`` with a bad identifier at all, so
    deleting the guard outright is invisible. ``sources`` and ``index_state``
    are real tables in the schema — they prove the guard is an allowlist rather
    than a does-this-table-exist check — and the quote-escape string is the
    injection shape the allowlist was written for.
    """
    with pytest.raises(ValueError, match="not in allowlist"):
        _table_snapshot(conn, table)


# ---------------------------------------------------------------------------
# What "the immutable core" actually means
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_core_fingerprint_covers_exactly_the_four_core_tables(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four core tables, and not the crosswalk table.

    The read-only core invariant is only as wide as this tuple. Dropping
    ``decisions`` from it leaves every existing test green — nothing in the
    suite writes to ``decisions`` — while silently exempting a whole table from
    the invariant that ``run()`` advertises. Adding ``crosswalk`` would be the
    opposite failure: the fingerprint would change on every legitimate
    ``dry_run=False`` write.
    """
    requested: list[str] = []

    def _recording_snapshot(_conn: sqlite3.Connection, table: str) -> dict[str, str | int]:
        requested.append(table)
        return {"count": 0, "digest": "x"}

    monkeypatch.setattr(_backfill_mod, "_table_snapshot", _recording_snapshot)
    _core_fingerprint(conn)

    assert requested == ["events", "claims", "memories", "decisions"]


# ---------------------------------------------------------------------------
# _crosswalk_exists, and the guard that depends on it
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crosswalk_exists_looks_for_the_crosswalk_table_specifically() -> None:
    """Not "does this database have any table at all".

    Dropping the ``name='crosswalk'`` predicate makes the probe answer True for
    any non-empty schema, which turns the clean "apply latest migrations first"
    ValueError into an OperationalError from deep inside the snapshot.
    """
    raw = sqlite3.connect(":memory:")
    try:
        raw.execute("CREATE TABLE decoy (x INTEGER)")
        assert _crosswalk_exists(raw) is False

        raw.execute("CREATE TABLE crosswalk (x INTEGER)")
        assert _crosswalk_exists(raw) is True
    finally:
        raw.close()


@pytest.mark.unit
def test_a_write_run_without_the_crosswalk_table_fails_cleanly(
    conn: sqlite3.Connection,
) -> None:
    """The operator-facing error, on a database that really lacks the table."""
    ingest_claim(conn, user_id=_USER, subject="s", predicate="p", object_="o")
    conn.execute("DROP TABLE crosswalk")
    conn.commit()

    with pytest.raises(ValueError, match="crosswalk table is required"):
        BackfillRunner(conn).run(_request(dry_run=False))


@pytest.mark.unit
def test_a_dry_run_without_the_crosswalk_table_still_works(
    conn: sqlite3.Connection,
) -> None:
    """A dry run reads core tables only, so a missing crosswalk is not its problem.

    This is the ``and`` in ``if not request.dry_run and not _crosswalk_exists``:
    turning it into ``or`` would refuse to plan a backfill on exactly the
    database an operator most wants to plan one for.
    """
    _seed_claims_with_distinct_times(conn, 2)
    conn.execute("DROP TABLE crosswalk")
    conn.commit()

    report = BackfillRunner(conn).run(_request(dry_run=True))

    assert report.rows_examined == 2
    assert report.writes_performed == 0


# ---------------------------------------------------------------------------
# The dry-run no-write invariant, proved in the direction that can fail
# ---------------------------------------------------------------------------


class _RunnerThatWritesDuringADryRun(BackfillRunner):
    """A runner whose enumeration writes to crosswalk even when asked not to."""

    def _enumerate(
        self, request: BackfillRequest, *, write: bool = False
    ) -> tuple[int, int, int, int]:
        self._conn.execute(
            "INSERT INTO crosswalk (user_id, canonical_ref, parallax_target_kind,"
            " parallax_target_id, state, content_hash, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (request.user_id, "claim:sneaky", "claim", "sneaky", "mapped", "h", "t", "t"),
        )
        return (0, 0, 0, 0)


@pytest.mark.unit
def test_a_dry_run_that_writes_to_crosswalk_is_caught(conn: sqlite3.Connection) -> None:
    """The invariant is only meaningful if something can trip it.

    Every existing dry-run test runs against an ``_enumerate`` that correctly
    writes nothing, so the before/after crosswalk comparison never has anything
    to detect and can be deleted outright without a red test. The core-table
    invariant has exactly this test (via a monkeypatched fingerprint); its
    crosswalk sibling did not.
    """
    with pytest.raises(RuntimeError, match="no-write invariant"):
        _RunnerThatWritesDuringADryRun(conn).run(_request(dry_run=True))


# ---------------------------------------------------------------------------
# SQLITE_BUSY: the ladder, the discriminator and the terminal error
# ---------------------------------------------------------------------------


class _ConnectionBusyForNAttempts:
    """Proxy that fails ``BEGIN IMMEDIATE`` a fixed number of times.

    ``sqlite3.Connection.execute`` is a read-only C attribute, so the existing
    suite's proxy shape is reused here.
    """

    def __init__(self, real: sqlite3.Connection, busy_times: int, then: str | None = None):
        self._conn = real
        self._busy_remaining = busy_times
        self._then = then

    def execute(self, sql: str, *args: object, **kwargs: object) -> object:
        if "BEGIN IMMEDIATE" in sql:
            if self._busy_remaining > 0:
                self._busy_remaining -= 1
                raise sqlite3.OperationalError("database is locked")
            if self._then is not None:
                raise sqlite3.OperationalError(self._then)
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self) -> object:
        return self._conn.commit()

    def rollback(self) -> object:
        return self._conn.rollback()

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


@pytest.mark.unit
def test_the_busy_backoff_ladder_is_the_shipped_one() -> None:
    """Three retries at 0.1 s, 0.5 s, 2 s. Nothing reads these numbers today."""
    assert _BUSY_DELAYS == (0.1, 0.5, 2.0)


@pytest.mark.unit
def test_a_persistently_locked_database_sleeps_exactly_the_ladder_then_gives_up(
    conn: sqlite3.Connection,
) -> None:
    """Pins the delays, the attempt count and the terminal error together.

    ``test_backfill_sqlite_busy_retry`` patches ``time.sleep`` into a no-op and
    asserts only that two busy errors were consumed, so it cannot see the delay
    values, cannot see a fourth retry being added, and never reaches the
    exhaustion path at all. Four consecutive busy errors is the smallest input
    that separates a three-rung ladder from a four-rung one: with three rungs
    the fourth failure lands on the final attempt and raises, with four rungs it
    is absorbed and the run quietly succeeds.
    """
    ingest_claim(conn, user_id=_USER, subject="s", predicate="p", object_="o")
    proxy = _ConnectionBusyForNAttempts(conn, busy_times=4)
    slept: list[float] = []

    with unittest.mock.patch.object(_backfill_mod.time, "sleep", slept.append):
        with pytest.raises(_SqliteBusyError, match="incident_id="):
            BackfillRunner(proxy).run(_request(dry_run=False))  # type: ignore[arg-type]

    assert slept == [0.1, 0.5, 2.0]


@pytest.mark.unit
def test_a_non_busy_operational_error_is_not_retried(conn: sqlite3.Connection) -> None:
    """Only "database is locked" earns a retry; everything else fails fast.

    Dropping that discriminator turns a genuine error — a missing table, a
    corrupt file — into three pointless sleeps totalling 2.6 s before it
    surfaces, and the existing test cannot see it because it patches ``sleep``
    into a no-op and never raises anything but a busy error.
    """
    ingest_claim(conn, user_id=_USER, subject="s", predicate="p", object_="o")
    proxy = _ConnectionBusyForNAttempts(conn, busy_times=0, then="no such table: nope")
    slept: list[float] = []

    with unittest.mock.patch.object(_backfill_mod.time, "sleep", slept.append):
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            BackfillRunner(proxy).run(_request(dry_run=False))

    assert slept == [], "a non-busy error must not sleep at all"


@pytest.mark.unit
def test_a_non_busy_error_on_the_final_attempt_is_not_relabelled_as_busy(
    conn: sqlite3.Connection,
) -> None:
    """The final attempt has its own copy of the discriminator, and its own mutant.

    Reaching it requires three busy errors followed by a different failure — a
    sequence no existing test produces. Without the check, every terminal
    failure would be reported as SQLITE_BUSY, sending an operator to look at
    lock contention for what is actually a schema problem.
    """
    ingest_claim(conn, user_id=_USER, subject="s", predicate="p", object_="o")
    proxy = _ConnectionBusyForNAttempts(conn, busy_times=3, then="no such table: nope")

    with unittest.mock.patch.object(_backfill_mod.time, "sleep", lambda _s: None):
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            BackfillRunner(proxy).run(_request(dry_run=False))


class _RunnerThatWritesThenFails(BackfillRunner):
    """A runner whose write-mode enumeration writes one row and then raises."""

    def _enumerate(
        self, request: BackfillRequest, *, write: bool = False
    ) -> tuple[int, int, int, int]:
        self._conn.execute(
            "INSERT INTO crosswalk (user_id, canonical_ref, parallax_target_kind,"
            " parallax_target_id, state, content_hash, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (request.user_id, "claim:partial", "claim", "partial", "mapped", "h", "t", "t"),
        )
        raise RuntimeError("enumeration blew up half way through")


@pytest.mark.unit
def test_a_failed_write_run_leaves_no_partial_crosswalk_rows(
    conn: sqlite3.Connection,
) -> None:
    """A write that dies mid-transaction must roll back what it already wrote.

    ``run()`` opens ``BEGIN IMMEDIATE`` and wraps the enumeration in a
    try/except that rolls back before re-raising. No existing test ever makes
    the enumeration fail, so that rollback can be deleted and every test stays
    green — while a real mid-backfill failure would leave a half-written
    crosswalk inside an open transaction on the caller's connection.
    """
    with pytest.raises(RuntimeError, match="blew up half way"):
        _RunnerThatWritesThenFails(conn).run(_request(dry_run=False))

    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM crosswalk WHERE canonical_ref = ?", ("claim:partial",)
        ).fetchone()[0]
        assert remaining == 0, "the partial write must have been rolled back"
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# What the writer actually puts in the crosswalk row
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_backfilled_claim_row_carries_every_column_from_its_claim(
    conn: sqlite3.Connection,
) -> None:
    """``crosswalk_rows >= 2`` is the only thing asserted about these rows today.

    That count is satisfied by any two rows with any contents, so the
    ``claim:`` prefix, the target kind, and the content_hash/source_id pair (two
    adjacent same-typed columns, exactly the shape that silently swaps) are all
    unobserved.
    """
    claim_id = ingest_claim(
        conn, user_id=_USER, subject="stack", predicate="prefers", object_="python"
    )
    source = conn.execute(
        "SELECT content_hash, source_id FROM claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()

    BackfillRunner(conn).run(_request(dry_run=False))

    row = conn.execute(
        "SELECT parallax_target_kind, parallax_target_id, content_hash, source_id,"
        " vault_path, state FROM crosswalk WHERE user_id = ? AND canonical_ref = ?",
        (_USER, f"claim:{claim_id}"),
    ).fetchone()

    assert row is not None, f"expected a crosswalk row keyed claim:{claim_id}"
    assert row["parallax_target_kind"] == "claim"
    assert row["parallax_target_id"] == claim_id
    assert row["content_hash"] == source["content_hash"]
    assert row["source_id"] == source["source_id"]
    assert row["vault_path"] is None, "claims have no vault path"
    assert row["state"] == "mapped"


@pytest.mark.unit
def test_a_backfilled_memory_row_carries_every_column_from_its_memory(
    conn: sqlite3.Connection,
) -> None:
    """The memory writer plumbs three adjacent columns and no test reads any of them.

    ``content_hash``, ``source_id`` and ``vault_path`` come out of ``row[1]``,
    ``row[2]`` and ``row[3]`` of a four-column SELECT; swapping the last two is
    a one-character change that leaks a vault path into the source column.
    """
    memory_id = ingest_memory(
        conn, user_id=_USER, title="Memo", summary="body", vault_path="notes/a.md"
    )
    source = conn.execute(
        "SELECT content_hash, source_id, vault_path FROM memories WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()

    BackfillRunner(conn).run(_request(dry_run=False))

    row = conn.execute(
        "SELECT parallax_target_kind, parallax_target_id, content_hash, source_id,"
        " vault_path, query_type FROM crosswalk WHERE user_id = ? AND canonical_ref = ?",
        (_USER, f"memory:{memory_id}"),
    ).fetchone()

    assert row is not None, f"expected a crosswalk row keyed memory:{memory_id}"
    assert row["parallax_target_kind"] == "memory"
    assert row["parallax_target_id"] == memory_id
    assert row["content_hash"] == source["content_hash"]
    assert row["source_id"] == source["source_id"]
    assert row["vault_path"] == "notes/a.md"


@pytest.mark.unit
def test_a_memory_is_routed_as_recent_context(conn: sqlite3.Connection) -> None:
    """Memories probe ``RetrieveKind.recent``, which resolves to ``recent_context``.

    Nothing asserts the query_type column, so the probe key can be changed to
    any other mapped RetrieveKind and the row is still written, still counted as
    MAPPED, and still passes every existing assertion — while every backfilled
    memory is filed under the wrong retriever. Pinned to the literal
    ``recent_context`` rather than to ``resolve("RetrieveKind.recent")``, which
    would move with the probe key.
    """
    memory_id = ingest_memory(
        conn, user_id=_USER, title="Memo", summary="body", vault_path="notes/a.md"
    )

    BackfillRunner(conn).run(_request(dry_run=False))

    row = conn.execute(
        "SELECT query_type FROM crosswalk WHERE user_id = ? AND canonical_ref = ?",
        (_USER, f"memory:{memory_id}"),
    ).fetchone()

    assert row["query_type"] == "recent_context"


# ---------------------------------------------------------------------------
# Predicate classification: the edges of both patterns
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        # The decision branch keeps its colon: a word that merely starts with
        # "decision" is not a decision predicate.
        ("decisions-are-hard", "RetrieveKind.entity"),
        ("decisionmaker", "RetrieveKind.entity"),
        # ...and it is case-insensitive, which only an uppercase input shows.
        ("DECISION:x", "RetrieveKind.decision"),
        ("Decision:x", "RetrieveKind.decision"),
        # The bug pattern is anchored at the start.
        ("hotfix:x", "RetrieveKind.entity"),
        ("prefix-bugfix:x", "RetrieveKind.entity"),
        # ...and terminated, so a longer word starting with "fix" is not a bug.
        ("fixture", "RetrieveKind.entity"),
        ("fixes-things", "RetrieveKind.entity"),
        # The bare-word alternative of (:|$).
        ("fix", "RetrieveKind.bug"),
        ("bugfix", "RetrieveKind.bug"),
        ("bug-fix", "RetrieveKind.bug"),
    ],
)
def test_predicate_classification_at_both_pattern_edges(
    predicate: str, expected: str
) -> None:
    """The six existing cases are all centre-of-the-pattern hits.

    ``decision:x``, ``fix:x``, ``bug_fix:x``, ``BUGFIX:x``, ``prefers`` and
    ``unknown`` never probe an edge: no input starts with "decision" without
    the colon, none is an uppercase decision, none has a prefix before "fix",
    and none is a longer word beginning with "fix". So the colon, the
    ``.lower()``, the ``^`` anchor and the ``(:|$)`` terminator are each free
    to disappear. Every case here is one of those edges.
    """
    assert _classify_claim_predicate(predicate) == expected
