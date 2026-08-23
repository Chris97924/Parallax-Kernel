"""Mutation-hardening for ``parallax.sqlite_store`` (land-20260824 w5 S3).

Additive companion to ``test_sqlite_store.py`` / ``test_schema.py`` /
``test_contracts.py`` / ``test_public_api.py`` / ``test_events*.py`` /
``test_wal.py`` and the ingest + retrieve suites that sit on top of them.

TALLY — applied 22 / killed-by-new 5 / already-covered 17 / equivalent 0 /
unaddressed 0. Twenty-two semantic mutants were applied one at a time to a
pristine tree: 17 died against the pre-existing suite and 5 walked through it.
All 5 are killed by the tests below.

What the existing suite is blind to, and why
--------------------------------------------

  * **The two dedup tests that DO exist stop one assertion short of the
    contract.** ``test_insert_memory_dedup_via_content_hash`` checks that the
    FIRST memory_id is the one retained (good, and it kills the OR-REPLACE
    mutant); its claim twin asserts only ``len(rows) == 1``, and the source
    test asserts only the ``uri`` round-trips. So ``INSERT OR IGNORE`` on
    ``sources`` can become ``INSERT OR REPLACE`` invisibly — which is not a
    cosmetic difference: REPLACE resolves a PK conflict by DELETING the
    existing row first, so re-registering a source both overwrites the
    original registrant's ``user_id``/``ingested_at`` (the exact thing
    ``_ensure_external_source``'s docstring promises will not happen) and, with
    ``PRAGMA foreign_keys = ON``, trips the FK from any memory or claim that
    already points at it.

  * **The append-only test never creates a conflict.**
    ``test_events_append_on_duplicate`` inserts two events with the same
    payload but DIFFERENT ``event_id``s, which no conflict resolution has an
    opinion about. So ``INSERT INTO events`` can become ``INSERT OR IGNORE
    INTO events`` and the append-only contract quietly degrades from "a
    duplicate event id is an error" to "a duplicate event id vanishes" — the
    failure mode an audit log must never have.

  * **``query`` is only ever asked for a handful of rows.** The whole suite's
    largest assertion is ``len(rows) == 2``, so a silent result cap in the one
    function every reader in the package funnels through is undetectable.

  * **``reaffirm``'s actor default and claim payload are unobserved.** Both
    reaffirm tests pass no ``actor`` and read back only ``event_type`` (plus
    ``target_id`` on the claim side), so the ``actor="system"`` default can
    become ``"user"`` — mislabelling machine-generated audit rows as
    human-approved — and the claim payload key can be renamed, breaking every
    consumer that reads ``payload_json["claim_id"]``.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import sqlite3

import pytest

from parallax.sqlite_store import (
    Claim,
    Event,
    Memory,
    Source,
    connect,
    insert_claim,
    insert_event,
    insert_memory,
    insert_source,
    now_iso,
    query,
    reaffirm,
)

_USER = "chris"


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    from parallax.migrations import migrate_to_latest

    c = connect(tmp_path / "store_harden.db")
    migrate_to_latest(c)
    yield c
    c.close()


def _source(source_id: str = "src-1", *, user_id: str = _USER, uri: str | None = None) -> Source:
    return Source(
        source_id=source_id,
        uri=uri if uri is not None else f"file://{source_id}",
        kind="file",
        content_hash=f"hash-{source_id}",
        user_id=user_id,
        ingested_at=now_iso(),
        state="ingested",
    )


def _memory(memory_id: str, *, source_id: str, content_hash: str) -> Memory:
    ts = now_iso()
    return Memory(
        memory_id=memory_id,
        user_id=_USER,
        source_id=source_id,
        vault_path="v.md",
        title="t",
        summary="s",
        content_hash=content_hash,
        state="active",
        created_at=ts,
        updated_at=ts,
    )


def _claim(claim_id: str, *, source_id: str, content_hash: str) -> Claim:
    ts = now_iso()
    return Claim(
        claim_id=claim_id,
        user_id=_USER,
        subject="x",
        predicate="y",
        object="z",
        source_id=source_id,
        content_hash=content_hash,
        confidence=0.9,
        state="auto",
        created_at=ts,
        updated_at=ts,
    )


def _event(event_id: str, *, event_type: str = "note") -> Event:
    return Event(
        event_id=event_id,
        user_id=_USER,
        actor="system",
        event_type=event_type,
        target_kind=None,
        target_id=None,
        payload_json='{"x": 1}',
        approval_tier=None,
        created_at=now_iso(),
    )


# ---------------------------------------------------------------------------
# INSERT conflict policy
# ---------------------------------------------------------------------------


class TestSourceConflictPolicy:
    @pytest.mark.unit
    def test_reregistering_a_source_keeps_the_original_registrant(
        self, conn: sqlite3.Connection
    ) -> None:
        """OR IGNORE: the second writer must no-op, not take ownership.

        ``test_insert_source_roundtrip`` reads back only ``uri``, so switching
        to OR REPLACE survives. It must not: ``_ensure_external_source``
        documents that a second write for the same id "silently no-ops without
        overwriting the original ``user_id`` / ``ingested_at`` of the
        registering caller", which is the guard stopping one user from
        re-labelling another user's source row.
        """
        first = _source("src-1", user_id="alice", uri="file://original")
        insert_source(conn, first)
        insert_source(
            conn, _source("src-1", user_id="mallory", uri="file://overwritten")
        )

        rows = query(conn, "SELECT * FROM sources WHERE source_id = ?", ("src-1",))

        assert len(rows) == 1
        assert rows[0]["user_id"] == "alice"
        assert rows[0]["uri"] == "file://original"
        assert rows[0]["ingested_at"] == first.ingested_at

    @pytest.mark.unit
    def test_reregistering_a_source_does_not_disturb_its_children(
        self, conn: sqlite3.Connection
    ) -> None:
        """The FK consequence of OR REPLACE, which is how this bites in prod.

        REPLACE resolves a PK conflict by DELETING the conflicting row first.
        With ``PRAGMA foreign_keys = ON`` (which ``connect`` sets) a memory or
        claim already referencing that source makes the delete illegal, so a
        perfectly ordinary re-registration starts raising IntegrityError --
        or, without FK enforcement, silently orphans the children.
        """
        insert_source(conn, _source("src-1"))
        insert_memory(conn, _memory("mem-1", source_id="src-1", content_hash="h1"))
        insert_claim(conn, _claim("cla-1", source_id="src-1", content_hash="h2"))

        insert_source(conn, _source("src-1", user_id="someone-else"))

        assert query(conn, "SELECT COUNT(*) AS n FROM memories", ())[0]["n"] == 1
        assert query(conn, "SELECT COUNT(*) AS n FROM claims", ())[0]["n"] == 1
        assert (
            query(conn, "SELECT source_id FROM memories WHERE memory_id = ?", ("mem-1",))[
                0
            ]["source_id"]
            == "src-1"
        )


class TestEventAppendOnlyConflictPolicy:
    @pytest.mark.unit
    def test_a_duplicate_event_id_is_an_error_not_a_silent_drop(
        self, conn: sqlite3.Connection
    ) -> None:
        """The append-only contract: a colliding event id must RAISE.

        ``test_events_append_on_duplicate`` inserts two events with different
        ids, so it never produces a conflict for any policy to resolve -- which
        leaves ``INSERT INTO events`` free to become ``INSERT OR IGNORE INTO
        events``. That turns the audit log's worst failure mode ("an event was
        silently discarded") from an exception into a no-op.
        """
        insert_event(conn, _event("ev-1", event_type="first"))

        with pytest.raises(sqlite3.IntegrityError):
            insert_event(conn, _event("ev-1", event_type="second"))

        rows = query(conn, "SELECT event_type FROM events WHERE event_id = ?", ("ev-1",))
        assert len(rows) == 1
        assert rows[0]["event_type"] == "first"

    @pytest.mark.unit
    def test_distinct_event_ids_still_both_append(
        self, conn: sqlite3.Connection
    ) -> None:
        """Control: tightening the policy must not block ordinary appends."""
        insert_event(conn, _event("ev-1"))
        insert_event(conn, _event("ev-2"))

        assert query(conn, "SELECT COUNT(*) AS n FROM events", ())[0]["n"] == 2


# ---------------------------------------------------------------------------
# query()
# ---------------------------------------------------------------------------


class TestQueryReturnsEverything:
    @pytest.mark.unit
    def test_query_does_not_cap_the_result_set(
        self, conn: sqlite3.Connection
    ) -> None:
        """Every reader in the package funnels through this one function.

        The suite's largest existing assertion on ``query`` is ``len(rows) ==
        2``, so a silent slice would be invisible here and would then quietly
        truncate retrieval, replay, export and the acceptance harness. 120 rows
        is deliberately well clear of any round number a cap would land on.
        """
        insert_source(conn, _source("src-1"))
        for i in range(120):
            insert_event(conn, _event(f"ev-{i:04d}"))

        rows = query(conn, "SELECT event_id FROM events ORDER BY event_id", ())

        assert len(rows) == 120
        assert rows[0]["event_id"] == "ev-0000"
        assert rows[-1]["event_id"] == "ev-0119"


# ---------------------------------------------------------------------------
# reaffirm()
# ---------------------------------------------------------------------------


class TestReaffirmDefaultsAndPayload:
    @staticmethod
    def _seed_claim(conn: sqlite3.Connection) -> str:
        insert_source(conn, _source("src-1"))
        insert_claim(conn, _claim("cla-1", source_id="src-1", content_hash="h2"))
        return "cla-1"

    @pytest.mark.unit
    def test_the_default_actor_is_system(self, conn: sqlite3.Connection) -> None:
        """A reaffirm nobody attributed is a MACHINE action, not a human one.

        Both existing reaffirm tests omit ``actor`` and never read the column
        back, so the default is unobserved. ``actor`` is what separates
        "the dedup path re-affirmed this row" from "a person confirmed this
        row" in every audit read of the events table.
        """
        assert inspect.signature(reaffirm).parameters["actor"].default == "system"

        claim_id = self._seed_claim(conn)
        eid = reaffirm(conn, user_id=_USER, kind="claim", entity_id=claim_id)

        row = query(conn, "SELECT actor FROM events WHERE event_id = ?", (eid,))[0]
        assert row["actor"] == "system"

    @pytest.mark.unit
    def test_an_explicit_actor_still_wins(self, conn: sqlite3.Connection) -> None:
        """Control: pinning the default must not freeze the parameter."""
        claim_id = self._seed_claim(conn)
        eid = reaffirm(
            conn, user_id=_USER, kind="claim", entity_id=claim_id, actor="user"
        )

        row = query(conn, "SELECT actor FROM events WHERE event_id = ?", (eid,))[0]
        assert row["actor"] == "user"

    @pytest.mark.unit
    def test_the_claim_reaffirm_payload_is_keyed_claim_id(
        self, conn: sqlite3.Connection
    ) -> None:
        """The payload key is the consumer-facing half of the event.

        ``test_claim_kind_emits_claim_reaffirmed`` reads ``event_type`` and
        ``target_id`` and stops there, so the payload can be re-keyed to
        ``memory_id`` -- and every replay/export consumer doing
        ``payload["claim_id"]`` starts KeyError-ing on a row whose type says
        ``claim.reaffirmed``.
        """
        claim_id = self._seed_claim(conn)
        eid = reaffirm(conn, user_id=_USER, kind="claim", entity_id=claim_id)

        row = query(conn, "SELECT payload_json FROM events WHERE event_id = ?", (eid,))[0]

        assert json.loads(row["payload_json"]) == {"claim_id": claim_id}
