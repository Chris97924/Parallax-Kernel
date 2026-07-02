"""Tests for the events_fts trigram index (migration 0014) and the FTS-backed
retrieval hot paths (by_file / by_bug_fix / by_entity).

Three concerns:

* ``TestEventsFtsMigration`` — the m0014 table/trigger exist, existing rows are
  backfilled, new inserts stay in sync, and ``down`` cleans up completely.
* ``TestFtsLikeEquivalence`` — the FTS ``MATCH`` path returns exactly the same
  event set as the legacy leading-wildcard ``LIKE`` path, including the
  wildcard-literal (``%`` / ``_``) and CJK-path cases the LIKE escape guards.
* ``TestFtsQueryPlan`` — EXPLAIN QUERY PLAN of the *actual* executed event query
  proves the FTS path is served from the trigram virtual-table index and no
  longer reads the whole per-user events partition (the LIKE path did).
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax import retrieve
from parallax.events import record_event
from parallax.hooks import ingest_hook
from parallax.migrations import migrate_to_latest
from parallax.retrieve import (
    _TraceBuilder,  # type: ignore[attr-defined]
    by_bug_fix,
    by_entity,
    by_file,
)
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "fts.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


def _seed_corpus(conn: sqlite3.Connection, user: str = "u") -> None:
    """Seed a mixed corpus exercising wildcard-literal + CJK + bug-token cases."""
    ingest_hook(conn, hook_type="SessionStart", session_id="s1", payload={}, user_id=user)
    # File-edit events (event_type in FILE_EVENT_TYPES) with tricky paths.
    for path in (
        "parallax/retrieve.py",
        "utils_v2.py",          # '_' must stay literal
        "utilsXv2.py",          # decoy for the '_' case
        "src/測試檔案/mod.py",   # CJK path segment (>=3 chars)
        "reports/100%_done.md",  # both '%' and '_' literal
        "reports/100Xdone.md",   # decoy for the '%'/'_' case
    ):
        ingest_hook(
            conn,
            hook_type="PostToolUse",
            session_id="s1",
            payload={"tool_name": "Edit", "tool_input": {"file_path": path}},
            user_id=user,
        )
    # Free-form note events for entity + bug-token scans.
    for text in (
        "mentioned MegaWidget in the design doc",
        "MEGAWIDGET shouted in caps",         # case-insensitivity check
        "applied FIX-42 for the bug in retrieve",
        "a regression slipped into the hotfix",
        "debugging session, nothing shipped",  # 'bug' substring inside 'debugging'
        "totally unrelated content here",
        "字串比對 測試檔案 出現在 payload",       # CJK entity in a note payload
    ):
        record_event(
            conn,
            user_id=user,
            actor="system",
            event_type="note",
            target_kind=None,
            target_id=None,
            payload={"text": text},
        )
    # A second user's row to prove the user scope still holds under the JOIN.
    record_event(
        conn,
        user_id="other",
        actor="system",
        event_type="note",
        target_kind=None,
        target_id=None,
        payload={"text": "MegaWidget for another user"},
    )


class TestEventsFtsMigration:
    def test_table_and_trigger_exist(self, conn: sqlite3.Connection) -> None:
        objs = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE name IN ('events_fts', 'events_ai_fts')"
            ).fetchall()
        }
        assert ("events_fts", "table") in objs
        assert ("events_ai_fts", "trigger") in objs

    def test_backfill_and_insert_sync(self, conn: sqlite3.Connection) -> None:
        # Rows inserted after migration are mirrored by the AFTER INSERT trigger.
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="note",
            target_kind=None,
            target_id=None,
            payload={"text": "syncme UNIQUETOKEN123"},
        )
        n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
        assert n_events == n_fts >= 1
        matched = conn.execute(
            'SELECT event_id FROM events_fts WHERE events_fts MATCH ?',
            ('"UNIQUETOKEN123"',),
        ).fetchall()
        assert len(matched) == 1

    def test_backfill_covers_preexisting_rows(self, tmp_path: pathlib.Path) -> None:
        # Rows written BEFORE m0014 must be picked up by the one-time backfill.
        from parallax.migrations import migrate_down_to

        db = tmp_path / "backfill.db"
        c = connect(db)
        migrate_to_latest(c)
        migrate_down_to(c, 13)  # drop events_fts + trigger, keep events
        assert (
            c.execute(
                "SELECT name FROM sqlite_master WHERE name = 'events_fts'"
            ).fetchone()
            is None
        )
        record_event(
            c,
            user_id="u",
            actor="system",
            event_type="note",
            target_kind=None,
            target_id=None,
            payload={"text": "preexisting BACKFILLTOKEN"},
        )
        migrate_to_latest(c)  # re-apply m0014 -> backfill the pre-existing row
        got = c.execute(
            'SELECT event_id FROM events_fts WHERE events_fts MATCH ?',
            ('"BACKFILLTOKEN"',),
        ).fetchall()
        assert len(got) == 1
        c.close()

    def test_down_removes_all_fts_objects(self, conn: sqlite3.Connection) -> None:
        from parallax.migrations import migrate_down_to

        migrate_down_to(conn, 13)
        residual = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'events_fts%' "
            "OR name = 'events_ai_fts'"
        ).fetchall()
        assert residual == []

    def test_idempotent_reapply(self, conn: sqlite3.Connection) -> None:
        # up() was already run by the fixture; a bare re-run must not double-index.
        from parallax.migrations import m0014_events_fts_trigram

        before = conn.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
        m0014_events_fts_trigram.up(conn)
        after = conn.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
        assert before == after


class TestFtsLikeEquivalence:
    """The FTS path (default) and the forced-LIKE fallback must agree exactly."""

    @staticmethod
    def _ids(hits) -> set[str]:
        return {h.entity_id for h in hits}

    def _both(self, conn, monkeypatch, call):
        """Return (fts_ids, like_ids) for the same retrieval call."""
        fts_ids = self._ids(call())
        monkeypatch.setattr(retrieve, "_events_fts_available", lambda _c: False)
        like_ids = self._ids(call())
        monkeypatch.undo()
        return fts_ids, like_ids

    @pytest.mark.parametrize(
        "path",
        [
            "parallax/retrieve.py",
            "utils_v2.py",          # '_' literal
            "測試檔案",              # CJK (4 chars)
            "100%_done",            # '%' and '_' literal
            "retrieve",             # common substring across several rows
            "does_not_exist_xyz",   # zero-hit
        ],
    )
    def test_by_file_equivalence(self, conn, monkeypatch, path) -> None:
        _seed_corpus(conn)
        fts_ids, like_ids = self._both(
            conn, monkeypatch, lambda: by_file(conn, user_id="u", path=path)
        )
        assert fts_ids == like_ids

    @pytest.mark.parametrize(
        "subject",
        [
            "MegaWidget",     # mixed-case payloads
            "megawidget",     # lower query
            "測試檔案",        # CJK
            "regression",     # bug-token word as an entity
            "NoSuchEntity",   # zero-hit
        ],
    )
    def test_by_entity_event_equivalence(self, conn, monkeypatch, subject) -> None:
        _seed_corpus(conn)

        def _event_ids():
            return {
                h.entity_id
                for h in by_entity(conn, user_id="u", subject=subject)
                if h.entity_kind == "event"
            }

        fts = _event_ids()
        monkeypatch.setattr(retrieve, "_events_fts_available", lambda _c: False)
        like = _event_ids()
        monkeypatch.undo()
        assert fts == like

    def test_by_bug_fix_event_equivalence(self, conn, monkeypatch) -> None:
        _seed_corpus(conn)

        def _event_ids():
            return {
                h.entity_id
                for h in by_bug_fix(conn, user_id="u")
                if h.entity_kind == "event"
            }

        fts = _event_ids()
        monkeypatch.setattr(retrieve, "_events_fts_available", lambda _c: False)
        like = _event_ids()
        monkeypatch.undo()
        assert fts == like

    def test_short_query_uses_like_fallback(self, conn) -> None:
        # 2-char path is below the trigram floor -> LIKE path, still correct.
        ingest_hook(
            conn, hook_type="SessionStart", session_id="s1", payload={}, user_id="u"
        )
        ingest_hook(
            conn,
            hook_type="PostToolUse",
            session_id="s1",
            payload={"tool_name": "Edit", "tool_input": {"file_path": "go.py"}},
            user_id="u",
        )
        hits = by_file(conn, user_id="u", path="go")
        assert any("go.py" in (h.evidence or "") for h in hits)

    def test_user_scope_preserved_under_join(self, conn) -> None:
        _seed_corpus(conn)
        hits = by_entity(conn, user_id="u", subject="MegaWidget")
        # 'other' user's MegaWidget row must never leak through the FTS JOIN.
        for h in hits:
            if h.full is not None:
                assert h.full.get("user_id") == "u"


class TestFtsQueryPlan:
    """EXPLAIN QUERY PLAN of the real executed SQL: FTS index used, no full
    events-partition read."""

    @staticmethod
    def _event_fragment(trace) -> str:
        frags = [f for f in trace.sql_fragments if " events_fts " in f or "events_fts" in f]
        assert frags, f"no events_fts query executed; fragments={trace.sql_fragments}"
        return frags[0]

    @staticmethod
    def _like_event_fragment(trace) -> str:
        frags = [
            f
            for f in trace.sql_fragments
            if "payload_json LIKE" in f and "FROM events" in f
        ]
        assert frags, f"no LIKE events query executed; fragments={trace.sql_fragments}"
        return frags[0]

    def _plan_text(self, conn: sqlite3.Connection, frag: str) -> str:
        nparams = frag.count("?")
        # Param values don't influence the MATCH/index decision; supply dummies
        # of the right arity so EXPLAIN QUERY PLAN can compile the statement.
        params = tuple("x" for _ in range(nparams))
        rows = conn.execute("EXPLAIN QUERY PLAN " + frag, params).fetchall()
        return " ".join(str(r[3]) for r in rows)

    def _run(self, conn, func, **kw):
        b = _TraceBuilder(kind="probe", params={})
        func(conn, _trace=b, **kw)
        return b.freeze(hits=())

    def test_by_file_plan_uses_fts_index(self, conn) -> None:
        _seed_corpus(conn)
        trace = self._run(conn, by_file, user_id="u", path="parallax/retrieve.py")
        plan = self._plan_text(conn, self._event_fragment(trace))
        assert "VIRTUAL TABLE INDEX" in plan
        assert "SCAN events" not in plan
        assert "idx_events_user_time" not in plan

    def test_by_bug_fix_plan_uses_fts_index(self, conn) -> None:
        _seed_corpus(conn)
        trace = self._run(conn, by_bug_fix, user_id="u")
        plan = self._plan_text(conn, self._event_fragment(trace))
        assert "VIRTUAL TABLE INDEX" in plan
        assert "SCAN events" not in plan
        assert "idx_events_user_time" not in plan

    def test_by_entity_plan_uses_fts_index(self, conn) -> None:
        _seed_corpus(conn)
        trace = self._run(conn, by_entity, user_id="u", subject="MegaWidget")
        plan = self._plan_text(conn, self._event_fragment(trace))
        assert "VIRTUAL TABLE INDEX" in plan
        assert "SCAN events" not in plan
        assert "idx_events_user_time" not in plan

    def test_like_fallback_plan_reads_events_partition(self, conn, monkeypatch) -> None:
        # Contrast: with the FTS index unavailable, the event query falls back to
        # LIKE and the planner must read the per-user events partition (the very
        # cost m0014 removes). This pins the before/after difference.
        _seed_corpus(conn)
        monkeypatch.setattr(retrieve, "_events_fts_available", lambda _c: False)
        trace = self._run(conn, by_file, user_id="u", path="parallax/retrieve.py")
        plan = self._plan_text(conn, self._like_event_fragment(trace))
        assert "VIRTUAL TABLE INDEX" not in plan
        # Old path walks every event row for the user (via the user_time index)
        # or scans the table outright — either way it touches the events table.
        assert "events" in plan
