"""Tests for RetrievalHit — 3-layer progressive disclosure + explain contract."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.events import record_event
from parallax.hooks import ingest_hook
from parallax.ingest import ingest_claim
from parallax.migrations import migrate_to_latest
from parallax.retrieve import (
    RetrievalHit,
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
    db = tmp_path / "hits.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


class TestProjectLevels:
    def _hit(self, **overrides) -> RetrievalHit:
        defaults = dict(
            entity_kind="event",
            entity_id="e1",
            title="demo",
            score=0.42,
            evidence="because X",
            full={"event_id": "e1", "extra": "data"},
            explain={"reason": "unit test", "score_components": {"keyword": 0.42}},
        )
        defaults.update(overrides)
        return RetrievalHit(**defaults)

    def test_l1_only(self) -> None:
        h = self._hit()
        p = h.project(1)
        assert set(p.keys()) == {"entity_kind", "entity_id", "title", "score"}

    def test_l2_adds_evidence(self) -> None:
        h = self._hit()
        p = h.project(2)
        assert p["evidence"] == "because X"
        assert "full" not in p

    def test_l3_adds_full(self) -> None:
        h = self._hit()
        p = h.project(3)
        assert p["full"] == {"event_id": "e1", "extra": "data"}

    def test_l3_fallback_when_full_is_none(self) -> None:
        h = self._hit(full=None)
        p = h.project(3)
        assert p["full"] == "because X"

    def test_invalid_level_raises(self) -> None:
        h = self._hit()
        with pytest.raises(ValueError, match="level must be"):
            h.project(4)
        with pytest.raises(ValueError):
            h.project(0)


class TestExplainContract:
    def _seed(self, conn: sqlite3.Connection) -> None:
        ingest_hook(
            conn,
            hook_type="SessionStart",
            session_id="s1",
            payload={},
            user_id="u",
        )
        ingest_hook(
            conn,
            hook_type="PostToolUse",
            session_id="s1",
            payload={"tool_name": "Edit", "tool_input": {"file_path": "a/b.py"}},
            user_id="u",
        )
        claim_id = ingest_claim(
            conn, user_id="u", subject="thing", predicate="is", object_="fixed"
        )
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="claim.state_changed",
            target_kind="claim",
            target_id=claim_id,
            payload={"from": "pending", "to": "confirmed", "note": "bug fix"},
        )

    def test_all_query_functions_populate_reason(self, conn: sqlite3.Connection) -> None:
        self._seed(conn)
        results = {
            "recent_context": recent_context(conn, user_id="u"),
            "by_file": by_file(conn, user_id="u", path="a/b.py"),
            "by_decision": by_decision(conn, user_id="u"),
            "by_bug_fix": by_bug_fix(conn, user_id="u"),
            "by_timeline": by_timeline(
                conn,
                user_id="u",
                since="2020-01-01T00:00:00+00:00",
                until="2099-01-01T00:00:00+00:00",
            ),
            "by_entity": by_entity(conn, user_id="u", subject="thing"),
        }
        for name, hits in results.items():
            assert hits, f"{name} returned no hits"
            for h in hits:
                assert h.explain["reason"], f"{name} hit has empty reason"
                assert isinstance(h.explain["score_components"], dict)
                assert all(
                    isinstance(v, (int, float))
                    for v in h.explain["score_components"].values()
                )
