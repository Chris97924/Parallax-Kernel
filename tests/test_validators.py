"""Tests for parallax.validators.target_ref_exists."""

from __future__ import annotations

import sqlite3

import pytest

from parallax.ingest import ingest_claim, ingest_memory
from parallax.sqlite_store import now_iso
from parallax.validators import (
    DECISION_TARGET_KINDS,
    VALID_TARGET_KINDS,
    target_ref_exists,
)


class TestTargetRefExists:
    def test_valid_kinds(self) -> None:
        assert VALID_TARGET_KINDS == frozenset(
            {"memory", "claim", "source", "decision"}
        )

    def test_decision_target_kinds_is_narrower(self) -> None:
        # DECISION_TARGET_KINDS matches the hard CHECK on decisions.target_kind
        # (schema.sql:61); it's a strict subset of VALID_TARGET_KINDS because
        # events.target_kind is intentionally unconstrained.
        assert DECISION_TARGET_KINDS == frozenset({"memory", "claim", "source"})
        assert DECISION_TARGET_KINDS < VALID_TARGET_KINDS
        assert "decision" not in DECISION_TARGET_KINDS

    def test_memory_present(self, conn: sqlite3.Connection) -> None:
        mid = ingest_memory(
            conn, user_id="u", title="t", summary="s", vault_path="v.md"
        )
        assert target_ref_exists(conn, "memory", mid) is True

    def test_memory_missing(self, conn: sqlite3.Connection) -> None:
        assert target_ref_exists(conn, "memory", "01NOPE") is False

    def test_claim_present(self, conn: sqlite3.Connection) -> None:
        cid = ingest_claim(
            conn, user_id="u", subject="x", predicate="y", object_="z"
        )
        assert target_ref_exists(conn, "claim", cid) is True

    def test_claim_missing(self, conn: sqlite3.Connection) -> None:
        assert target_ref_exists(conn, "claim", "01NOPE") is False

    def test_source_present(self, conn: sqlite3.Connection) -> None:
        # ingest_memory auto-creates the synthetic direct source.
        ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        assert target_ref_exists(conn, "source", "direct:u") is True

    def test_source_missing(self, conn: sqlite3.Connection) -> None:
        assert target_ref_exists(conn, "source", "src-nope") is False

    def test_decision_present(self, conn: sqlite3.Connection) -> None:
        # Decisions are schema-ready but un-ingested at v0.1.2; insert one
        # directly to cover the lookup path.
        conn.execute(
            """INSERT INTO decisions(decision_id, user_id, target_kind,
                   target_id, action, actor, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "dec-1",
                "u",
                "claim",
                "claim-x",
                "confirm",
                "user",
                "proposed",
                now_iso(),
            ),
        )
        conn.commit()
        assert target_ref_exists(conn, "decision", "dec-1") is True

    def test_decision_missing(self, conn: sqlite3.Connection) -> None:
        assert target_ref_exists(conn, "decision", "dec-nope") is False

    def test_user_id_scopes_lookup_to_owner(self, conn: sqlite3.Connection) -> None:
        mid = ingest_memory(
            conn, user_id="u", title="t", summary="s", vault_path="v.md"
        )
        cid = ingest_claim(
            conn, user_id="u", subject="x", predicate="y", object_="z"
        )
        conn.execute(
            """INSERT INTO decisions(decision_id, user_id, target_kind,
                   target_id, action, actor, state, created_at)
               VALUES ('dec-1', 'u', 'claim', ?, 'confirm', 'user', 'proposed', ?)""",
            (cid, now_iso()),
        )
        conn.commit()
        for kind, tid in (
            ("memory", mid),
            ("claim", cid),
            ("source", "direct:u"),
            ("decision", "dec-1"),
        ):
            assert target_ref_exists(conn, kind, tid, user_id="u") is True, kind
            assert target_ref_exists(conn, kind, tid, user_id="other") is False, kind
            # Omitting user_id keeps the unscoped existence check.
            assert target_ref_exists(conn, kind, tid) is True, kind

    def test_unknown_kind_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="unknown target_kind"):
            target_ref_exists(conn, "event", "e-1")
        with pytest.raises(ValueError, match="unknown target_kind"):
            target_ref_exists(conn, "", "x")
