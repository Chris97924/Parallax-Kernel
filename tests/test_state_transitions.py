"""Tests for transition_claim_state + cross-user content_hash isolation.

Pinned scenarios from the v0.6.1 stabilization pass:

* :func:`transition_claim_state` actually mutates ``claims.state`` and
  ``claims.updated_at`` (closes the gap where
  :func:`record_claim_state_changed` only wrote the audit row).
* :func:`is_allowed_transition` rejects unknown / terminal-from
  transitions cleanly so disallowed state moves cannot pollute the
  event log.
* :func:`memory_by_content_hash` and :func:`claim_by_content_hash` are
  user-scoped — same content, different user IDs, no cross-tenant leak.
* The README "State Machine" code block actually executes (drift guard).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from parallax import (
    CLAIM_TRANSITIONS,
    claim_by_content_hash,
    claims_by_user,
    ingest_claim,
    ingest_memory,
    is_allowed_transition,
    memory_by_content_hash,
    record_claim_state_changed,
    transition_claim_state,
)
from parallax.hashing import content_hash

# ---------------------------------------------------------------------------
# transition_claim_state — atomic mutation + audit
# ---------------------------------------------------------------------------


class TestTransitionClaimState:
    def test_updates_state_column(self, conn: sqlite3.Connection) -> None:
        cid = ingest_claim(
            conn, user_id="chris", subject="x", predicate="y", object_="z"
        )
        # Default state for ingest_claim is 'auto'.
        before = conn.execute(
            "SELECT state FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()[0]
        assert before == "auto"

        eid = transition_claim_state(conn, claim_id=cid, to_state="confirmed")
        assert isinstance(eid, str) and len(eid) >= 16

        after = conn.execute(
            "SELECT state, updated_at FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()
        assert after[0] == "confirmed"
        # updated_at should have been bumped. Lex-compare is safe because
        # now_iso() emits microsecond-precision UTC timestamps.
        original_updated_at = conn.execute(
            "SELECT created_at FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()[0]
        assert after[1] >= original_updated_at

    def test_emits_state_changed_event_with_payload(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = ingest_claim(
            conn, user_id="chris", subject="x", predicate="y", object_="z"
        )
        transition_claim_state(conn, claim_id=cid, to_state="pending")

        rows = conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'claim.state_changed'"
        ).fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0][0])
        assert payload["from"] == "auto"
        assert payload["to"] == "pending"
        # updated_at must be carried so replay can rebuild the row exactly.
        assert "updated_at" in payload

    def test_disallowed_transition_raises_and_does_not_mutate(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = ingest_claim(
            conn, user_id="chris", subject="x", predicate="y", object_="z",
            state="rejected",  # terminal — no outgoing edges
        )
        with pytest.raises(ValueError, match="not allowed"):
            transition_claim_state(conn, claim_id=cid, to_state="confirmed")
        # State unchanged, no event emitted.
        state_after = conn.execute(
            "SELECT state FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()[0]
        assert state_after == "rejected"
        events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'claim.state_changed'"
        ).fetchone()[0]
        assert events == 0

    def test_unknown_claim_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="not found"):
            transition_claim_state(conn, claim_id="nope", to_state="confirmed")

    def test_expected_user_id_guard(self, conn: sqlite3.Connection) -> None:
        cid = ingest_claim(
            conn, user_id="chris", subject="x", predicate="y", object_="z"
        )
        with pytest.raises(ValueError, match="different user"):
            transition_claim_state(
                conn,
                claim_id=cid,
                to_state="confirmed",
                expected_user_id="alice",
            )
        # Original transition still possible with the correct user_id guard.
        transition_claim_state(
            conn,
            claim_id=cid,
            to_state="confirmed",
            expected_user_id="chris",
        )
        state_after = conn.execute(
            "SELECT state FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()[0]
        assert state_after == "confirmed"

    def test_self_loop_pending_is_allowed(self, conn: sqlite3.Connection) -> None:
        """CLAIM_TRANSITIONS allows pending->pending as a retry-safe self-loop."""
        cid = ingest_claim(
            conn,
            user_id="chris",
            subject="x",
            predicate="y",
            object_="z",
            state="pending",
        )
        transition_claim_state(conn, claim_id=cid, to_state="pending")
        # state unchanged, but an audit event was still written.
        events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'claim.state_changed'"
        ).fetchone()[0]
        assert events == 1


# ---------------------------------------------------------------------------
# record_claim_state_changed — explicitly does NOT mutate
# ---------------------------------------------------------------------------


class TestRecordClaimStateChangedContract:
    """The legacy helper writes audit only; pin the contract so the README
    description and the implementation cannot drift apart again."""

    def test_does_not_mutate_claims_table(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = ingest_claim(
            conn, user_id="chris", subject="x", predicate="y", object_="z"
        )
        original_state = conn.execute(
            "SELECT state FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()[0]

        record_claim_state_changed(
            conn,
            user_id="chris",
            claim_id=cid,
            from_state="auto",
            to_state="confirmed",
        )

        # Event was written...
        ev = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'claim.state_changed'"
        ).fetchone()[0]
        assert ev == 1
        # ...but the row state must be unchanged. This is the explicit
        # contract: callers that want mutation must use
        # transition_claim_state.
        state_after = conn.execute(
            "SELECT state FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()[0]
        assert state_after == original_state


# ---------------------------------------------------------------------------
# Cross-user content_hash isolation
# ---------------------------------------------------------------------------


class TestContentHashIsolation:
    def test_memory_by_hash_is_user_scoped(
        self, conn: sqlite3.Connection
    ) -> None:
        """Same memory content under two users must not leak across the boundary."""
        ingest_memory(
            conn, user_id="chris", title="t", summary="s", vault_path="v.md"
        )
        ingest_memory(
            conn, user_id="alice", title="t", summary="s", vault_path="v.md"
        )
        h = content_hash("t", "s", "v.md")

        chris_row = memory_by_content_hash(conn, h, user_id="chris")
        alice_row = memory_by_content_hash(conn, h, user_id="alice")
        assert chris_row is not None
        assert alice_row is not None
        assert chris_row["user_id"] == "chris"
        assert alice_row["user_id"] == "alice"
        assert chris_row["memory_id"] != alice_row["memory_id"]

        # Lookup with a third user_id finds nothing even though the hash
        # exists in the table for two other users.
        assert memory_by_content_hash(conn, h, user_id="bob") is None

    def test_claim_by_hash_is_user_scoped(
        self, conn: sqlite3.Connection
    ) -> None:
        """Claim hash is already user-scoped at the hashing layer (ADR-005);
        the explicit user_id filter is the defence-in-depth guarantee."""
        ingest_claim(
            conn, user_id="chris", subject="x", predicate="y", object_="z"
        )
        # Same SPO under a different user — the hash differs (because user_id
        # is in the hash), but the lookup API still requires user_id so we
        # exercise both paths.
        ingest_claim(
            conn, user_id="alice", subject="x", predicate="y", object_="z"
        )

        chris_hash = content_hash("x", "y", "z", "direct:chris", "chris")
        alice_hash = content_hash("x", "y", "z", "direct:alice", "alice")

        chris_row = claim_by_content_hash(conn, chris_hash, user_id="chris")
        alice_row = claim_by_content_hash(conn, alice_hash, user_id="alice")
        assert chris_row is not None and chris_row["user_id"] == "chris"
        assert alice_row is not None and alice_row["user_id"] == "alice"

        # Right hash, wrong user: still no leak.
        assert claim_by_content_hash(conn, chris_hash, user_id="alice") is None

    def test_user_id_is_required_keyword(
        self, conn: sqlite3.Connection
    ) -> None:
        """Calling without user_id is a programming error, not a silent leak."""
        with pytest.raises(TypeError):
            memory_by_content_hash(conn, "deadbeef")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            claim_by_content_hash(conn, "deadbeef")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# README "State Machine" example — drift guard
# ---------------------------------------------------------------------------


class TestReadmeStateMachineExample:
    """The README block under 'State Machine' must be executable verbatim.
    Pin the assertions here so a future README rewrite cannot reintroduce
    the dict-vs-tuple bug that v0.6.1 fixed."""

    def test_dict_lookup_form_works(self) -> None:
        # README form #1: nested dict lookup.
        assert "confirmed" in CLAIM_TRANSITIONS["pending"]

    def test_is_allowed_transition_form_works(self) -> None:
        # README form #2: helper function.
        assert is_allowed_transition("claim", "pending", "confirmed")

    def test_old_buggy_form_remains_buggy(self) -> None:
        # Sanity-check that the old README example was indeed broken.
        # If this assertion ever flips to True, CLAIM_TRANSITIONS has been
        # restructured and the README needs another revisit.
        assert ("pending", "confirmed") not in CLAIM_TRANSITIONS


# ---------------------------------------------------------------------------
# Sanity-check that transition_claim_state interacts correctly with
# claims_by_user (no rows lost / duplicated by the UPDATE).
# ---------------------------------------------------------------------------


class TestTransitionInteractionWithRetrieval:
    def test_state_filter_reflects_transition(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = ingest_claim(
            conn, user_id="chris", subject="x", predicate="y", object_="z"
        )
        # Auto → pending → confirmed.
        transition_claim_state(conn, claim_id=cid, to_state="pending")
        assert {r["state"] for r in claims_by_user(conn, "chris")} == {"pending"}
        transition_claim_state(conn, claim_id=cid, to_state="confirmed")
        assert {r["state"] for r in claims_by_user(conn, "chris")} == {"confirmed"}
        # Exactly one row, never duplicated.
        rows = claims_by_user(conn, "chris")
        assert len(rows) == 1
        assert rows[0]["claim_id"] == cid
