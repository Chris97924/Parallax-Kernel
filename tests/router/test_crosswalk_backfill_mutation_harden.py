"""Mutation-hardening for ``parallax.router.crosswalk_backfill`` (land-20260823 w4 S5).

Additive companion to ``test_crosswalk_backfill.py``. Every test below exists
because a semantic mutant of the module SURVIVED that suite (plus
``test_crosswalk_v05_full.py``, ``test_m0011_crosswalk.py``,
``test_m0012_crosswalk_aphelion_doc_id.py`` and
``test_metric_producer_consumer_parity_106.py``). 24 mutants were applied one
at a time; 14 died against those suites and 10 walked through.

Three shapes account for all ten:

  * **Everything is proved on the memories half only.** ``backfill_crosswalk``
    contains two near-identical scan loops, and every batch-limit test seeds
    memories: ``test_backfill_respects_batch_limit_arg`` and
    ``_env`` both call ``_add_memories``. So the claims loop's own copy of the
    ``rows_examined >= limit`` guard is unobserved and can be relaxed to ``>``.
    The same asymmetry hides the claims row-plumbing.

  * **The crosswalk rows are counted and grouped, never read.** The existing
    assertions are ``rows_inserted == 5``, ``_crosswalk_count(...) == 3`` and
    ``SELECT DISTINCT parallax_target_kind``. Nothing reads ``canonical_ref``
    (``test_lazy_materialize_hit`` reads it only to compare it with itself),
    ``content_hash``, ``source_id`` or the timestamps — so the ``memory:``
    prefix can be renamed, the two same-typed columns can be swapped in either
    loop, and the timestamps can lose their timezone.

  * **The two guards that scope a query are only tested in the direction that
    passes.** ``test_backfill_user_isolation`` proves the backfill respects
    ``user_id``; ``lazy_materialize_by_content_hash`` has the same predicate and
    no such test, so dropping it — a cross-user read — is invisible. Likewise
    ``batch_limit`` is only ever passed a truthy number, so the explicit
    ``is not None`` check can be relaxed to a truthiness test and ``0`` silently
    stops meaning zero.
"""

from __future__ import annotations

import datetime
import sqlite3

import pytest

from parallax.ingest import ingest_claim, ingest_memory
from parallax.router.crosswalk_backfill import (
    backfill_crosswalk,
    lazy_materialize_by_content_hash,
)

_USER = "harden_cw_alice"
_OTHER_USER = "harden_cw_bob"


def _add_memories(conn: sqlite3.Connection, count: int, user_id: str = _USER) -> list[str]:
    """Insert *count* memories and return their ids in insertion order."""
    return [
        ingest_memory(
            conn,
            user_id=user_id,
            title=f"title-{user_id}-{i}",
            summary=f"summary-{user_id}-{i}",
            vault_path=f"path/{user_id}/{i}.md",
        )
        for i in range(count)
    ]


def _add_claims(conn: sqlite3.Connection, count: int, user_id: str = _USER) -> list[str]:
    """Insert *count* claims and return their ids in insertion order."""
    return [
        ingest_claim(
            conn,
            user_id=user_id,
            subject=f"subject-{i}",
            predicate=f"decision:choose-{i}",
            object_=f"object-{i}",
        )
        for i in range(count)
    ]


def _stamp_created_at(
    conn: sqlite3.Connection, table: str, id_column: str, ids: list[str]
) -> None:
    """Give each row a distinct, increasing ``created_at``.

    ``ingest_*`` stamps a whole corpus inside one clock tick, so ``ORDER BY
    created_at`` degrades to the id tie-break and ASC vs DESC become
    indistinguishable. Rewriting the column is what makes the scan direction a
    decidable question. ``table`` and ``id_column`` are module-local literals,
    never caller input.
    """
    for i, row_id in enumerate(ids):
        conn.execute(
            f"UPDATE {table} SET created_at = ? WHERE {id_column} = ?",  # noqa: S608
            (f"2020-01-01T00:{i:02d}:00+00:00", row_id),
        )
    conn.commit()


def _refs(conn: sqlite3.Connection, user_id: str = _USER) -> set[str]:
    """The canonical_refs currently in the crosswalk for *user_id*."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT canonical_ref FROM crosswalk WHERE user_id = ?", (user_id,)
        )
    }


# ---------------------------------------------------------------------------
# The batch limit: only ever given a truthy number, only ever on memories
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_explicit_batch_limit_of_zero_means_zero(conn: sqlite3.Connection) -> None:
    """``batch_limit=0`` is an explicit instruction, not an absent one.

    ``_get_batch_limit`` distinguishes them with ``if batch_limit is not None``.
    Every existing test passes 3 or 2, so relaxing that to a truthiness check is
    invisible — and it turns "examine nothing" into "examine up to the env or
    default 10000", which is the exact opposite instruction. A caller computing
    a remaining budget that has reached zero is the realistic way to hit this.
    """
    _add_memories(conn, 5)

    stats = backfill_crosswalk(conn, user_id=_USER, batch_limit=0)

    assert stats.rows_examined == 0
    assert stats.rows_inserted == 0
    assert stats.batch_limit_reached is True
    assert _refs(conn) == set()


@pytest.mark.unit
def test_the_env_batch_limit_tolerates_surrounding_whitespace(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.strip()`` is why an env var out of a shell or compose file works.

    ``test_backfill_respects_batch_limit_env`` sets a clean ``"2"``, which
    ``isdigit()`` accepts with or without the strip. A padded value is the
    realistic one — and without the strip it fails ``isdigit()`` silently and
    falls back to the 10000 default, so an operator's cap is ignored with no
    error anywhere.
    """
    monkeypatch.setenv("CROSSWALK_BACKFILL_BATCH_LIMIT", "  2  ")
    _add_memories(conn, 5)

    stats = backfill_crosswalk(conn, user_id=_USER)

    assert stats.rows_examined == 2
    assert stats.batch_limit_reached is True


@pytest.mark.unit
def test_the_claims_loop_honours_the_batch_limit_too(conn: sqlite3.Connection) -> None:
    """The second scan loop carries its own copy of the guard, and its own mutant.

    Both existing batch-limit tests seed memories, so the claims loop's
    ``rows_examined >= limit`` is never the one that fires. Relaxed to ``>`` it
    lets exactly one extra row past — invisible on a memories-only corpus.
    """
    _add_claims(conn, 5)

    stats = backfill_crosswalk(conn, user_id=_USER, batch_limit=3)

    assert stats.rows_examined == 3
    assert stats.rows_inserted == 3
    assert stats.batch_limit_reached is True
    assert stats.source_breakdown == {"memory": 0, "claim": 3}


@pytest.mark.unit
def test_the_limit_is_shared_across_both_scans_not_applied_twice(
    conn: sqlite3.Connection,
) -> None:
    """``rows_examined`` is a single running total, not a per-source budget.

    The docstring promises "at most ``batch_limit`` rows total across both
    sources". With three memories and three claims and a limit of 4, the scan
    must stop one row into the claims loop.
    """
    _add_memories(conn, 3)
    _add_claims(conn, 3)

    stats = backfill_crosswalk(conn, user_id=_USER, batch_limit=4)

    assert stats.rows_examined == 4
    assert stats.source_breakdown == {"memory": 3, "claim": 1}
    assert stats.batch_limit_reached is True


# ---------------------------------------------------------------------------
# Scan order: oldest first, so a bounded re-run makes forward progress
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_bounded_memory_scan_takes_the_oldest_rows_first(
    conn: sqlite3.Connection,
) -> None:
    """``ORDER BY created_at ASC`` is deliberate here, and nothing observes it.

    Note the direction is the opposite of ``parallax/router/backfill.py``, which
    samples newest-first: this routine is a bounded *catch-up* over an existing
    corpus, so it starts at the oldest unprocessed rows and a repeated run walks
    forward. Flipped to DESC it would re-examine the newest rows on every run
    and never reach the tail of a corpus larger than the batch limit.

    With fewer rows than the limit, or with a corpus that shares one timestamp,
    both directions select the same set — which is why the fixture rewrites
    ``created_at`` and the limit is set below the row count.
    """
    ids = _add_memories(conn, 6)
    _stamp_created_at(conn, "memories", "memory_id", ids)

    backfill_crosswalk(conn, user_id=_USER, batch_limit=3)

    assert _refs(conn) == {f"memory:{memory_id}" for memory_id in ids[:3]}


@pytest.mark.unit
def test_a_bounded_claim_scan_takes_the_oldest_rows_first(
    conn: sqlite3.Connection,
) -> None:
    """The claims scan carries its own ORDER BY and its own mutant."""
    ids = _add_claims(conn, 6)
    _stamp_created_at(conn, "claims", "claim_id", ids)

    backfill_crosswalk(conn, user_id=_USER, batch_limit=3)

    assert _refs(conn) == {f"claim:{claim_id}" for claim_id in ids[:3]}


# ---------------------------------------------------------------------------
# What the backfilled row actually contains
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_backfilled_memory_row_is_keyed_and_plumbed_correctly(
    conn: sqlite3.Connection,
) -> None:
    """The existing assertions never read a single column of these rows.

    They count them and group them by ``parallax_target_kind``. So the
    ``memory:`` canonical_ref prefix — the key ``lazy_materialize_by_content_hash``
    hands back to callers and the crosswalk's own primary key — can be renamed,
    and ``content_hash``/``source_id``, two adjacent same-typed bind parameters,
    can be swapped.
    """
    (memory_id,) = _add_memories(conn, 1)
    source = conn.execute(
        "SELECT content_hash, source_id, vault_path FROM memories WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()

    backfill_crosswalk(conn, user_id=_USER)

    row = conn.execute(
        "SELECT parallax_target_kind, parallax_target_id, content_hash, source_id,"
        " query_type, vault_path, aphelion_doc_id FROM crosswalk"
        " WHERE user_id = ? AND canonical_ref = ?",
        (_USER, f"memory:{memory_id}"),
    ).fetchone()

    assert row is not None, f"expected a row keyed memory:{memory_id}"
    assert row["parallax_target_kind"] == "memory"
    assert row["parallax_target_id"] == memory_id
    assert row["content_hash"] == source["content_hash"]
    assert row["source_id"] == source["source_id"]
    # M3a leaves the Aphelion-side columns for M4 to fill.
    assert row["query_type"] is None
    assert row["vault_path"] is None
    assert row["aphelion_doc_id"] is None


@pytest.mark.unit
def test_a_backfilled_claim_row_is_keyed_and_plumbed_correctly(
    conn: sqlite3.Connection,
) -> None:
    """The claims loop plumbs the same two columns, and has the same mutant."""
    (claim_id,) = _add_claims(conn, 1)
    source = conn.execute(
        "SELECT content_hash, source_id FROM claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()

    backfill_crosswalk(conn, user_id=_USER)

    row = conn.execute(
        "SELECT parallax_target_kind, parallax_target_id, content_hash, source_id, state"
        " FROM crosswalk WHERE user_id = ? AND canonical_ref = ?",
        (_USER, f"claim:{claim_id}"),
    ).fetchone()

    assert row is not None, f"expected a row keyed claim:{claim_id}"
    assert row["parallax_target_kind"] == "claim"
    assert row["parallax_target_id"] == claim_id
    assert row["content_hash"] == source["content_hash"]
    assert row["source_id"] == source["source_id"]
    assert row["state"] == "mapped"
    assert row["content_hash"] != row["source_id"], (
        "precondition: the two columns must differ, otherwise a swap is undetectable"
    )


@pytest.mark.unit
def test_backfilled_timestamps_are_timezone_aware_utc(conn: sqlite3.Connection) -> None:
    """``datetime.now(datetime.UTC)``, not ``datetime.now()``.

    Nothing reads ``created_at``/``updated_at`` off these rows today, so the
    ``datetime.UTC`` argument can be dropped and every row silently starts
    carrying naive local time. The repo has a whole migration about this
    (m0008, naive timestamp normalization), and the neighbouring writer in
    ``parallax.router.backfill`` goes through ``now_iso()`` for the same reason.
    """
    _add_memories(conn, 1)

    backfill_crosswalk(conn, user_id=_USER)

    row = conn.execute(
        "SELECT created_at, updated_at FROM crosswalk WHERE user_id = ?", (_USER,)
    ).fetchone()

    for column in ("created_at", "updated_at"):
        parsed = datetime.datetime.fromisoformat(row[column])
        assert parsed.tzinfo is not None, f"{column}={row[column]!r} is naive"
        assert parsed.utcoffset() == datetime.timedelta(0), f"{column} must be UTC"


# ---------------------------------------------------------------------------
# lazy_materialize_by_content_hash is scoped to a user
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_content_hash_does_not_resolve_across_users(conn: sqlite3.Connection) -> None:
    """The ``user_id`` predicate is a tenancy boundary, and it has no test.

    ``test_lazy_materialize_hit`` looks a hash up as its own owner and
    ``test_lazy_materialize_miss`` uses a hash nobody has, so the query passes
    both with the ``user_id`` term deleted — at which point one user's
    content_hash resolves to another user's canonical_ref. The backfill's own
    user scoping IS tested (``test_backfill_user_isolation``); its read-side
    twin was not.
    """
    _add_memories(conn, 1, user_id=_USER)
    backfill_crosswalk(conn, user_id=_USER)

    alice_row = conn.execute(
        "SELECT canonical_ref, content_hash FROM crosswalk WHERE user_id = ?", (_USER,)
    ).fetchone()
    alice_hash = alice_row["content_hash"]

    assert (
        lazy_materialize_by_content_hash(conn, user_id=_USER, content_hash=alice_hash)
        == alice_row["canonical_ref"]
    ), "precondition: the hash must resolve for its own owner"

    assert (
        lazy_materialize_by_content_hash(
            conn, user_id=_OTHER_USER, content_hash=alice_hash
        )
        is None
    ), "another user's content_hash must not resolve"
