"""Mutation-hardening for ``parallax.ingest`` (land-20260824 w5 S2).

Additive companion to ``test_ingest.py`` / ``test_ingest_with_status.py`` /
``test_ingest_claim_state.py`` / ``test_claim_dedup_semantics.py`` /
``test_claim_no_source.py`` / ``test_content_hash_user_id_scope.py`` /
``test_telemetry.py``.

TALLY — applied 23 / killed-by-new 7 / already-covered 16 / equivalent 0 /
unaddressed 0. Twenty-three semantic mutants were applied one at a time to a
pristine tree: 16 died against the pre-existing suite and 7 walked through it.
All 7 are killed by the tests below.

What the existing suite is blind to, and why
--------------------------------------------

  * **The synthetic source row is checked field-by-field except its hash.**
    ``test_auto_creates_source_on_first_direct_memory`` asserts ``kind``,
    ``uri`` and ``user_id`` but never ``content_hash``, so the direct branch
    can fingerprint the bare ``user_id`` instead of the ``source_id`` it is
    supposed to describe — silently disagreeing with the external branch
    right next to it, which hashes its own ``source_id``.

  * **The lazily-created EXTERNAL source row is never inspected.**
    ``test_memory_with_novel_source_id_creates_sources_row`` asserts the row
    exists; nothing reads back its ``state``. The whole point of the lazy
    create is to register the row in the same lifecycle state as every other
    freshly ingested source.

  * **Both dedup re-selects can be relaxed off the calling tenant without any
    observable result change.** ``... WHERE content_hash = ? AND user_id = ?``
    can become ``user_id >= ?`` and the suite stays green — and so does a
    result-level test, because the unique index
    (``memories(content_hash, user_id)`` /
    ``claims(content_hash, source_id, user_id)``) makes SQLite return the
    matching rows in ascending ``user_id``, and the caller's own row was just
    INSERT-OR-IGNOREd into existence, so it is always the first one back. The
    tenant leak is real but currently masked by the query plan, which is
    exactly the kind of latent bug an index change would detonate. So these
    tests observe the STATEMENT the code really issued, via
    ``sqlite3.Connection.set_trace_callback``, rather than a result that
    today happens to come out right.

  * **The error path is only proven on the claim half.**
    ``test_ingest_error_path_emits_and_reraises`` calls ``ingest_claim``, so
    ``ingest_memory``'s ``telemetry.emit_ingest_error`` can be deleted and
    every failed memory ingest becomes invisible to ``/health`` — no
    ``errors_total`` bump, no ``last_error``.

  * **``ingest_claim_with_status``'s ``state`` default is shadowed by its own
    wrapper.** ``test_default_state_is_auto`` goes through ``ingest_claim``,
    which passes ``state=state`` explicitly from ITS default — so the
    ``_with_status`` signature default (the one the Lane D-3 router calls
    directly) is unobserved and can be changed to ``'pending'``, quietly
    routing every router-ingested claim into the review queue.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import pathlib
import re
import sqlite3
from collections.abc import Iterator

import pytest

from parallax import telemetry
from parallax.ingest import (
    ingest_claim,
    ingest_claim_with_status,
    ingest_memory,
    ingest_memory_with_status,
)
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect, query

# Expectations are re-derived from stdlib sha256 over LITERAL byte strings, not
# from parallax.hashing.content_hash(...): calling the production helper with
# whatever arguments the module happens to pass would let a mutated argument
# list move the expectation along with the code. (The digests are not written
# out as hex literals because the repo's secret-scan hook flags any 64-char hex
# run.)
_SHA256_DIRECT_U = hashlib.sha256(b"direct:u").hexdigest()
_SHA256_BARE_U = hashlib.sha256(b"u").hexdigest()


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "ingest_harden.db")
    migrate_to_latest(c)
    yield c
    c.set_trace_callback(None)
    c.close()


@contextlib.contextmanager
def _traced(conn: sqlite3.Connection) -> Iterator[list[str]]:
    """Capture every SQL statement the connection actually executes."""
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        yield seen
    finally:
        conn.set_trace_callback(None)


# ``set_trace_callback`` reports EXPANDED SQL — bound values are inlined as
# literals — so every structural check below masks the literals first. The
# pattern honours SQL's doubled-quote escaping so a value containing a quote
# cannot end the match early.
_SQL_LITERAL = re.compile(r"'(?:[^']|'')*'")
_PREDICATE = re.compile(r"(\w+) *(>=|<=|<>|!=|=|<|>) *\?")


def _skeleton(sql: str) -> str:
    """``sql`` with literals masked as ``?`` and all whitespace collapsed.

    Reduces a statement to its STRUCTURE, so assertions made on it survive a
    reflow or a change of bound values but still see the operators and the
    columns they are applied to.
    """
    return " ".join(_SQL_LITERAL.sub("?", sql).split())


def _only(statements: list[str], needle: str) -> str:
    """The single executed statement whose skeleton contains ``needle``.

    Matched against the skeleton rather than the raw text so that reflowing
    the SQL — a line break after the column list, say — does not turn a real
    assertion into an "expected exactly one ... got []" failure.
    """
    want = " ".join(needle.split())
    matches = [s for s in statements if want in _skeleton(s)]
    assert len(matches) == 1, f"expected exactly one {needle!r} statement, got {matches}"
    return matches[0]


def _where_predicates(sql: str) -> list[tuple[str, str]]:
    """Every ``<column> <op> <value>`` comparison in ``sql``'s WHERE clause.

    Returned sorted, so the result is invariant under a semantics-preserving
    reorder of the conjuncts and under whitespace, while still changing the
    moment any column's operator is relaxed away from ``=``. Duplicates are
    preserved (a list, not a dict) so a repeated column stays visible.
    """
    where = _skeleton(sql).partition(" WHERE ")[2]
    assert where, f"no WHERE clause in {sql!r}"
    return sorted(_PREDICATE.findall(where))


# ---------------------------------------------------------------------------
# The lazily-created source rows
# ---------------------------------------------------------------------------


class TestSyntheticSourceRow:
    @pytest.mark.unit
    def test_the_direct_source_hash_fingerprints_the_source_id(
        self, conn: sqlite3.Connection
    ) -> None:
        """``content_hash`` must cover ``direct:<user>``, not the bare user_id.

        The existing row assertion stops at kind/uri/user_id. The hash is the
        column the schema declares as the row's content fingerprint, and the
        external branch three lines below hashes ITS ``source_id`` — the two
        branches have to agree on what is being fingerprinted or the column
        means nothing.
        """
        ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")

        rows = query(
            conn, "SELECT * FROM sources WHERE source_id = ?", ("direct:u",)
        )

        assert len(rows) == 1
        assert rows[0]["content_hash"] == _SHA256_DIRECT_U
        assert rows[0]["content_hash"] != _SHA256_BARE_U

    @pytest.mark.unit
    def test_the_external_source_hash_fingerprints_its_own_source_id(
        self, conn: sqlite3.Connection
    ) -> None:
        """Control for the test above: same rule on the external branch."""
        ingest_memory(
            conn,
            user_id="u",
            title="t",
            summary="s",
            vault_path="v.md",
            source_id="ext-1",
        )

        rows = query(conn, "SELECT * FROM sources WHERE source_id = ?", ("ext-1",))

        assert rows[0]["content_hash"] == hashlib.sha256(b"ext-1").hexdigest()

    @pytest.mark.unit
    def test_a_lazily_created_external_source_starts_ingested(
        self, conn: sqlite3.Connection
    ) -> None:
        """Every field of the external row, including the one nothing reads.

        ``test_memory_with_novel_source_id_creates_sources_row`` asserts the
        row exists and nothing more, so ``state`` is free. A source registered
        in the wrong lifecycle state is invisible to every state-filtered
        query over ``sources`` afterwards.
        """
        ingest_memory(
            conn,
            user_id="u",
            title="t",
            summary="s",
            vault_path="v.md",
            source_id="ext-1",
        )

        row = query(conn, "SELECT * FROM sources WHERE source_id = ?", ("ext-1",))[0]

        assert row["state"] == "ingested"
        assert row["kind"] == "external"
        assert row["uri"] == "parallax://external/ext-1"
        assert row["user_id"] == "u"

    @pytest.mark.unit
    def test_the_direct_and_external_rows_agree_on_state(
        self, conn: sqlite3.Connection
    ) -> None:
        """Both lazy-create branches must land in the same lifecycle state."""
        ingest_memory(conn, user_id="u", title="a", summary="s", vault_path="a.md")
        ingest_memory(
            conn,
            user_id="u",
            title="b",
            summary="s",
            vault_path="b.md",
            source_id="ext-1",
        )

        states = {
            r["source_id"]: r["state"]
            for r in query(conn, "SELECT source_id, state FROM sources", ())
        }

        assert states == {"direct:u": "ingested", "ext-1": "ingested"}


# ---------------------------------------------------------------------------
# Tenant scoping of the dedup re-selects
# ---------------------------------------------------------------------------


class TestReselectTenantScoping:
    @pytest.mark.unit
    def test_the_memory_reselect_is_equality_scoped_on_user_id(
        self, conn: sqlite3.Connection
    ) -> None:
        """``user_id = ?`` — a range comparison here is a cross-tenant read.

        Asserted on the STATEMENT, not the result, and deliberately so: with
        the unique index on ``memories(content_hash, user_id)`` SQLite walks
        the range in ascending user_id, and the caller's own row always exists
        by re-select time (INSERT OR IGNORE either wrote it or it was already
        there). The caller is therefore always the SMALLEST user_id satisfying
        ``user_id >= <caller>``, so ``row[0]`` is its own row and a relaxed
        ``>=`` returns the right memory_id for every input that can be built.
        The leak is latent in the query plan; the only honest way to pin it is
        to read back the comparison the code issued.

        Read back STRUCTURALLY (masked literals, collapsed whitespace, sorted
        predicates) rather than as a substring of the raw text, so reordering
        the conjuncts or reflowing the string — both semantics-preserving —
        leaves this test alone, while relaxing any operator fails it.
        """
        with _traced(conn) as seen:
            ingest_memory(
                conn, user_id="aaa", title="t", summary="s", vault_path="v.md"
            )

        stmt = _only(seen, "SELECT memory_id FROM memories")

        assert _where_predicates(stmt) == [("content_hash", "="), ("user_id", "=")]

    @pytest.mark.unit
    def test_the_claim_reselect_is_equality_scoped_on_user_id_and_source_id(
        self, conn: sqlite3.Connection
    ) -> None:
        """Same rule on the claim re-select, for BOTH scoping columns.

        ADR-005 made the claim hash user-scoped; the re-select's ``source_id``
        and ``user_id`` equality filters are the second half of that boundary,
        and are masked by the same index-order accident as the memory one.

        Structural for the same reason, and asserting the whole predicate SET
        rather than membership: dropping a scoping column entirely is as much
        of a leak as relaxing its operator, and only an equality against the
        full list catches both.
        """
        with _traced(conn) as seen:
            ingest_claim(
                conn, user_id="aaa", subject="s", predicate="p", object_="o"
            )

        stmt = _only(seen, "SELECT claim_id FROM claims")

        assert _where_predicates(stmt) == [
            ("content_hash", "="),
            ("source_id", "="),
            ("user_id", "="),
        ]

    @pytest.mark.unit
    def test_two_users_ingesting_identical_memory_content_stay_separate(
        self, conn: sqlite3.Connection
    ) -> None:
        """Behavioural companion: the boundary the statements above protect."""
        mid_z = ingest_memory(
            conn, user_id="zzz", title="t", summary="s", vault_path="v.md"
        )
        mid_a, deduped = ingest_memory_with_status(
            conn, user_id="aaa", title="t", summary="s", vault_path="v.md"
        )

        assert deduped is False
        assert mid_a != mid_z
        owner = query(
            conn, "SELECT user_id FROM memories WHERE memory_id = ?", (mid_a,)
        )[0]
        assert owner["user_id"] == "aaa"


# ---------------------------------------------------------------------------
# Error telemetry on the memory path
# ---------------------------------------------------------------------------


class TestMemoryErrorTelemetry:
    @pytest.mark.unit
    def test_a_failed_memory_ingest_is_reported_to_telemetry(
        self, conn: sqlite3.Connection
    ) -> None:
        """The memory half of the error path has no test of its own.

        ``test_ingest_error_path_emits_and_reraises`` exercises
        ``ingest_claim``, so deleting ``emit_ingest_error`` from
        ``ingest_memory_with_status`` survives — and every failed memory
        ingest then leaves ``errors_total`` and ``last_error`` untouched, so
        ``/health`` reports a healthy store while writes are failing.
        """
        before = telemetry.snapshot()

        with pytest.raises(ValueError, match="reserved direct: namespace"):
            ingest_memory(
                conn,
                user_id="a",
                title="t",
                summary="s",
                vault_path="v.md",
                source_id="direct:b",
            )

        after = telemetry.snapshot()

        assert after["errors_total"] == before["errors_total"] + 1
        assert after["last_error"] is not None
        assert "reserved direct: namespace" in after["last_error"]

    @pytest.mark.unit
    def test_the_exception_still_propagates_unchanged(
        self, conn: sqlite3.Connection
    ) -> None:
        """Guard against 'fixing' the above by swallowing the error."""
        with pytest.raises(ValueError):
            ingest_memory(
                conn,
                user_id="a",
                title="t",
                summary="s",
                vault_path="v.md",
                source_id="direct:b",
            )
        assert query(conn, "SELECT COUNT(*) AS n FROM memories", ())[0]["n"] == 0


# ---------------------------------------------------------------------------
# Defaults that are shadowed by their own wrapper
# ---------------------------------------------------------------------------


class TestWithStatusDefaults:
    @pytest.mark.unit
    def test_the_with_status_claim_state_default_is_auto(
        self, conn: sqlite3.Connection
    ) -> None:
        """The Lane D-3 router calls ``_with_status`` directly, not the wrapper.

        Every existing state test goes through ``ingest_claim``, which forwards
        ``state=state`` from its OWN default — so the ``_with_status`` default
        is never the value that reaches the row and can be changed to
        ``'pending'``, silently diverting every router-ingested claim into the
        review queue. Pinned as a literal signature default AND read back off
        the persisted row.
        """
        assert (
            inspect.signature(ingest_claim_with_status).parameters["state"].default
            == "auto"
        )

        claim_id, _ = ingest_claim_with_status(
            conn, user_id="u", subject="s", predicate="p", object_="o"
        )

        row = query(conn, "SELECT state FROM claims WHERE claim_id = ?", (claim_id,))[0]
        assert row["state"] == "auto"

    @pytest.mark.unit
    def test_the_wrapper_claim_state_default_is_also_auto(
        self, conn: sqlite3.Connection
    ) -> None:
        """Control: the two defaults must not drift apart.

        Read back off the persisted row, not just off the signature: resolving
        the default inside the body (``state: ... | None = None``) is a
        semantics-preserving refactor that a signature-only assertion would
        fail while every stored row still said ``'auto'``.
        """
        assert inspect.signature(ingest_claim).parameters["state"].default == "auto"

        claim_id = ingest_claim(
            conn, user_id="u", subject="s", predicate="p", object_="o"
        )

        row = query(conn, "SELECT state FROM claims WHERE claim_id = ?", (claim_id,))[0]
        assert row["state"] == "auto"
