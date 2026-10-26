"""Tests for the v0.3.0 explicit retrieval API."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.events import record_event
from parallax.hooks import ingest_hook
from parallax.ingest import ingest_claim
from parallax.migrations import migrate_to_latest
from parallax.retrieve import (
    by_bug_fix,
    by_decision,
    by_entity,
    by_file,
    by_timeline,
    recent_context,
)
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "retr.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


def _seed_session(conn: sqlite3.Connection, session_id: str = "s1") -> None:
    ingest_hook(
        conn,
        hook_type="SessionStart",
        session_id=session_id,
        payload={},
        user_id="u",
    )
    ingest_hook(
        conn,
        hook_type="PreToolUse",
        session_id=session_id,
        payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
        user_id="u",
    )
    ingest_hook(
        conn,
        hook_type="PostToolUse",
        session_id=session_id,
        payload={"tool_name": "Edit", "tool_input": {"file_path": "parallax/retrieve.py"}},
        user_id="u",
    )
    ingest_hook(
        conn,
        hook_type="PostToolUse",
        session_id=session_id,
        payload={"tool_name": "Edit", "tool_input": {"file_path": "tests/test_foo.py"}},
        user_id="u",
    )


class TestRecentContext:
    def test_latest_session_default(self, conn: sqlite3.Connection) -> None:
        _seed_session(conn, "s1")
        hits = recent_context(conn, user_id="u")
        assert len(hits) >= 3
        assert all(h.entity_kind == "event" for h in hits)

    def test_explicit_session(self, conn: sqlite3.Connection) -> None:
        _seed_session(conn, "sA")
        _seed_session(conn, "sB")
        hits = recent_context(conn, user_id="u", session_id="sA")
        assert len(hits) >= 3
        for h in hits:
            assert h.full["session_id"] == "sA"

    def test_empty_returns_empty_list(self, conn: sqlite3.Connection) -> None:
        assert recent_context(conn, user_id="nobody") == []


class TestByFile:
    def test_file_match(self, conn: sqlite3.Connection) -> None:
        _seed_session(conn, "s1")
        hits = by_file(conn, user_id="u", path="parallax/retrieve.py")
        assert len(hits) >= 1
        assert "retrieve.py" in hits[0].title or "retrieve.py" in (hits[0].evidence or "")

    def test_empty_path_no_hits(self, conn: sqlite3.Connection) -> None:
        _seed_session(conn, "s1")
        assert by_file(conn, user_id="u", path="") == []

    def test_limit_respected(self, conn: sqlite3.Connection) -> None:
        _seed_session(conn, "s1")
        _seed_session(conn, "s2")
        hits = by_file(conn, user_id="u", path="parallax/retrieve.py", limit=1)
        assert len(hits) == 1


class TestByDecision:
    def test_claim_state_changed_match(self, conn: sqlite3.Connection) -> None:
        # Ingest a claim so target_ref_exists is happy, then record the event.
        claim_id = ingest_claim(
            conn,
            user_id="u",
            subject="ProjectX",
            predicate="status",
            object_="shipped",
        )
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="claim.state_changed",
            target_kind="claim",
            target_id=claim_id,
            payload={"from": "pending", "to": "confirmed"},
        )
        hits = by_decision(conn, user_id="u")
        assert len(hits) == 1
        assert hits[0].entity_kind == "event"

    def test_enrichment_does_not_leak_other_users_claim(
        self, conn: sqlite3.Connection
    ) -> None:
        # record_event's orphan check is existence-only, so user "u" can
        # point an event at user "other"'s claim. The title enrichment must
        # stay inside u's tenant and never surface other's subject/predicate/object.
        foreign_id = ingest_claim(
            conn,
            user_id="other",
            subject="SecretProject",
            predicate="budget",
            object_="classified",
        )
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="claim.state_changed",
            target_kind="claim",
            target_id=foreign_id,
            payload={"from": "pending", "to": "confirmed"},
        )
        record_event(
            conn,
            user_id="other",
            actor="system",
            event_type="claim.state_changed",
            target_kind="claim",
            target_id=foreign_id,
            payload={"from": "pending", "to": "confirmed"},
        )

        hits = by_decision(conn, user_id="u")
        assert [h.title for h in hits] == [
            f"claim.state_changed [claim:{foreign_id}]"
        ]

        owner_hits = by_decision(conn, user_id="other")
        assert [h.title for h in owner_hits] == [
            f"SecretProject budget classified — claim.state_changed [claim:{foreign_id}]"
        ]

    def test_empty(self, conn: sqlite3.Connection) -> None:
        assert by_decision(conn, user_id="u") == []


class TestByBugFix:
    def test_bug_keyword_in_payload(self, conn: sqlite3.Connection) -> None:
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="note",
            target_kind=None,
            target_id=None,
            payload={"text": "applied FIX-42 for the bug in retrieve"},
        )
        hits = by_bug_fix(conn, user_id="u")
        assert len(hits) >= 1

    def test_claim_subject_match(self, conn: sqlite3.Connection) -> None:
        ingest_claim(
            conn,
            user_id="u",
            subject="bug report",
            predicate="is",
            object_="open",
        )
        hits = by_bug_fix(conn, user_id="u")
        assert any(h.entity_kind == "claim" for h in hits)

    def test_no_matches(self, conn: sqlite3.Connection) -> None:
        assert by_bug_fix(conn, user_id="u") == []


class TestByTimeline:
    def test_window_filter(self, conn: sqlite3.Connection) -> None:
        _seed_session(conn, "s1")
        # Ultra-wide window - everything fits.
        hits = by_timeline(
            conn,
            user_id="u",
            since="2020-01-01T00:00:00+00:00",
            until="2099-01-01T00:00:00+00:00",
        )
        assert len(hits) >= 3

    def test_bad_order_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="must be <="):
            by_timeline(
                conn,
                user_id="u",
                since="2030-01-01T00:00:00+00:00",
                until="2020-01-01T00:00:00+00:00",
            )


class TestByEntity:
    def test_exact_subject_match_scores_higher(self, conn: sqlite3.Connection) -> None:
        ingest_claim(
            conn,
            user_id="u",
            subject="Parallax",
            predicate="is",
            object_="kb",
        )
        ingest_claim(
            conn,
            user_id="u",
            subject="ParallaxKernel",
            predicate="is",
            object_="package",
        )
        hits = by_entity(conn, user_id="u", subject="Parallax")
        assert len(hits) >= 2
        # Exact match ranks first.
        assert hits[0].title.startswith("Parallax ")

    def test_empty_subject_no_hits(self, conn: sqlite3.Connection) -> None:
        assert by_entity(conn, user_id="u", subject="") == []

    def test_event_payload_match(self, conn: sqlite3.Connection) -> None:
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="note",
            target_kind=None,
            target_id=None,
            payload={"text": "mentioned MegaWidget here"},
        )
        hits = by_entity(conn, user_id="u", subject="MegaWidget")
        assert any(h.entity_kind == "event" for h in hits)


class TestLikeEscape:
    """Regression: LIKE wildcards in user input must match literally."""

    def test_by_file_underscore_does_not_wildcard(
        self, conn: sqlite3.Connection
    ) -> None:
        ingest_hook(
            conn,
            hook_type="SessionStart",
            session_id="s1",
            payload={},
            user_id="u",
        )
        # Two files whose paths differ only at the '_' position.
        ingest_hook(
            conn,
            hook_type="PostToolUse",
            session_id="s1",
            payload={"tool_name": "Edit", "tool_input": {"file_path": "utils_v2.py"}},
            user_id="u",
        )
        ingest_hook(
            conn,
            hook_type="PostToolUse",
            session_id="s1",
            payload={"tool_name": "Edit", "tool_input": {"file_path": "utilsXv2.py"}},
            user_id="u",
        )
        hits = by_file(conn, user_id="u", path="utils_v2.py")
        # The '_' must be literal, so only utils_v2.py matches.
        titles = " ".join(h.title for h in hits)
        payloads = " ".join(h.evidence or "" for h in hits)
        assert "utils_v2.py" in payloads
        assert "utilsXv2.py" not in payloads
        assert hits, titles

    def test_by_entity_percent_does_not_wildcard(
        self, conn: sqlite3.Connection
    ) -> None:
        ingest_claim(conn, user_id="u", subject="100% done", predicate="is", object_="ok")
        ingest_claim(conn, user_id="u", subject="100X done", predicate="is", object_="ok")
        hits = by_entity(conn, user_id="u", subject="100% done")
        subjects = {(h.full or {}).get("subject") for h in hits if h.full}
        assert "100% done" in subjects
        assert "100X done" not in subjects


class TestByTimelineErrors:
    def test_unparseable_since_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="ISO-8601"):
            by_timeline(conn, user_id="u", since="not-a-date", until="2026-04-19")

    def test_since_after_until_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="must be <= until"):
            by_timeline(
                conn,
                user_id="u",
                since="2026-04-19T10:00:00Z",
                until="2026-04-18T10:00:00Z",
            )

    def test_iso_format_variants_match_same_window(
        self, conn: sqlite3.Connection
    ) -> None:
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="marker",
            target_kind=None,
            target_id=None,
            payload={},
        )
        # 'Z' suffix and '+00:00' must both normalize to the same window.
        n1 = len(
            by_timeline(
                conn, user_id="u", since="2020-01-01T00:00:00Z", until="2099-01-01T00:00:00Z"
            )
        )
        n2 = len(
            by_timeline(
                conn,
                user_id="u",
                since="2020-01-01T00:00:00+00:00",
                until="2099-01-01T00:00:00+00:00",
            )
        )
        assert n1 == n2 >= 1
