"""Tests for GET /export/memory_md — Story D3.

Round-trip: ingest MEMORY.md → export → parse back → same entries.
Privacy belt-and-braces: rows bypassing ingest filter are dropped at export.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from parallax.memory_md import ingest_memory_md, parse_memory_md
from parallax.migrations import migrate_to_latest
from parallax.server import create_app
from parallax.sqlite_store import connect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYNTHETIC_MEMORY_MD = """\
# User
- [Alice Profile](alice.md) — Alice is a developer

# Projects (Active)
- [Project Alpha](alpha.md) — The alpha project
- [Project Beta](beta.md) — The beta project

# Feedback
- [Speed Feedback](speed.md) — Make it faster

# Reference
- [Docs Link](docs.md) — Official documentation
"""

# Minimal companion-file bodies (no secrets).
_COMPANIONS: dict[str, str] = {
    "alice.md": "Alice is a developer and lives in Taipei.",
    "alpha.md": "Alpha is a greenfield project.",
    "beta.md": "Beta is a refactor project.",
    "speed.md": "Response times should be under 200 ms.",
    "docs.md": "See https://example.com/docs.",
}


def _make_app(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Create an app with a fresh migrated tmp DB in open mode."""
    db_p = tmp_path / "export_test.db"
    boot = connect(db_p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()

    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(db_p))

    def factory() -> sqlite3.Connection:
        return connect(db_p)

    return create_app(db_factory=factory)


def _write_companions(directory: pathlib.Path, companions: dict[str, str]) -> None:
    """Write simple companion files with minimal frontmatter."""
    for filename, body in companions.items():
        name = filename.replace(".md", "").replace("_", " ").title()
        content = (
            f"---\nname: {name}\ndescription: companion for {filename}"
            f"\ntype: note\n---\n\n{body}"
        )
        (directory / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRoundTripSynthetic:
    def test_round_trip_synthetic(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ingest synthetic MEMORY.md → GET /export/memory_md → parse back → same entries."""
        # Write MEMORY.md + companions to tmp_path.
        mem_path = tmp_path / "MEMORY.md"
        mem_path.write_text(_SYNTHETIC_MEMORY_MD, encoding="utf-8")
        _write_companions(tmp_path, _COMPANIONS)

        app = _make_app(tmp_path, monkeypatch)

        # Ingest via library call (directly into the DB the app uses).
        db_p = tmp_path / "export_test.db"
        conn = connect(db_p)
        try:
            report = ingest_memory_md(conn, memory_md_path=mem_path, user_id="u1")
        finally:
            conn.close()

        assert report.cards_inserted == 5
        assert report.skipped_privacy == ()

        with TestClient(app) as client:
            resp = client.get("/export/memory_md", params={"user_id": "u1"})

        assert resp.status_code == 200
        body = resp.json()
        exported_text = body["memory_md"]
        companion_files = body["companion_files"]

        # Round-trip: parse exported text and compare to original parse.
        exported_entries = parse_memory_md(exported_text)
        original_entries = parse_memory_md(_SYNTHETIC_MEMORY_MD)

        assert len(exported_entries) == len(original_entries)
        assert {e.filename for e in exported_entries} == {e.filename for e in original_entries}
        assert {e.category for e in exported_entries} == {e.category for e in original_entries}

        # companion_files must contain all 5 filenames.
        assert set(companion_files.keys()) == set(_COMPANIONS.keys())
        # Bodies must match what we stored.
        for filename, body_text in companion_files.items():
            assert _COMPANIONS[filename] in body_text or body_text


class TestEmptyDb:
    def test_empty_db(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero rows → skeleton headers only, empty companion_files."""
        app = _make_app(tmp_path, monkeypatch)

        with TestClient(app) as client:
            resp = client.get("/export/memory_md", params={"user_id": "nobody"})

        assert resp.status_code == 200
        body = resp.json()
        md = body["memory_md"]

        # All four section headers must be present.
        assert "# User" in md
        assert "# Projects (Active)" in md
        assert "# Feedback" in md
        assert "# Reference" in md

        # No bullet lines.
        assert "- [" not in md

        # companion_files must be empty dict.
        assert body["companion_files"] == {}

        # Exact skeleton check (headers separated by blank lines, trailing newline).
        expected = "# User\n\n# Projects (Active)\n\n# Feedback\n\n# Reference\n\n"
        assert md == expected


class TestRenderFormatDeterministic:
    def test_render_format_deterministic(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling the endpoint twice returns identical bytes."""
        mem_path = tmp_path / "MEMORY.md"
        mem_path.write_text(_SYNTHETIC_MEMORY_MD, encoding="utf-8")
        _write_companions(tmp_path, _COMPANIONS)

        app = _make_app(tmp_path, monkeypatch)
        db_p = tmp_path / "export_test.db"
        conn = connect(db_p)
        try:
            ingest_memory_md(conn, memory_md_path=mem_path, user_id="u1")
        finally:
            conn.close()

        with TestClient(app) as client:
            r1 = client.get("/export/memory_md", params={"user_id": "u1"})
            r2 = client.get("/export/memory_md", params={"user_id": "u1"})

        assert r1.status_code == 200
        assert r1.text == r2.text


class TestAuthRequired:
    def test_auth_required(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With PARALLAX_TOKEN set, missing auth header → 401."""
        db_p = tmp_path / "auth_test.db"
        boot = connect(db_p)
        try:
            migrate_to_latest(boot)
        finally:
            boot.close()

        monkeypatch.setenv("PARALLAX_TOKEN", "s3cr3t")
        monkeypatch.setenv("PARALLAX_DB_PATH", str(db_p))

        def factory() -> sqlite3.Connection:
            return connect(db_p)

        app = create_app(db_factory=factory)

        with TestClient(app) as client:
            resp = client.get("/export/memory_md", params={"user_id": "u1"})

        assert resp.status_code == 401


class TestExportPrivacyFilterBeltAndBraces:
    def test_export_privacy_filter_belt_and_braces(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rows with secrets inserted directly (bypassing ingest) are dropped at export."""
        app = _make_app(tmp_path, monkeypatch)
        db_p = tmp_path / "export_test.db"

        # Directly INSERT a row with a real secret in body (bypasses ingest
        # filter). Use a key=value pattern with 8+ chars so body_looks_like_secret
        # triggers — see PRD S7.
        conn = connect(db_p)
        try:
            conn.execute(
                "INSERT INTO memory_cards "
                "(id, user_id, category, name, filename, description, "
                "body, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    "secret_row_001",
                    "u1",
                    "reference",
                    "Secret Creds",
                    "creds.md",
                    "Credential store",
                    "password=hunter2abc123",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        with TestClient(app) as client:
            resp = client.get("/export/memory_md", params={"user_id": "u1"})

        assert resp.status_code == 200
        body = resp.json()
        md = body["memory_md"]

        # Secret row must NOT appear in the rendered markdown.
        assert "creds.md" not in md
        assert "Secret Creds" not in md
        assert "password=xyz" not in md

        # Secret filename must NOT appear in companion_files.
        assert "creds.md" not in body["companion_files"]


# Synthetic MEMORY.md modelled on Chris's real layout: 4 categories, multiple
# entries each, plus a decorative code fence (ignored by parse_memory_md).
# Decoupling from the host file keeps the round-trip invariant test
# deterministic regardless of unrelated edits to the live MEMORY.md.
_REAL_SHAPED_MEMORY_MD = """\
# User
- [Chris Profile](chris_profile.md) — identity sketch

# Projects (Active)
- [Parallax Status](project_parallax.md) — current snapshot
- [Orbit Current](project_orbit.md) — current snapshot
- [Aphelion v0.5](project_aphelion.md) — shipped

```text
decorative fence: ignored by parse_memory_md
```

# Feedback
- [No Phantom Done](feedback_phantom_done.md) — verify before claim
- [Notion Sync](feedback_notion_sync.md) — dual-write

# Reference
- [ZenBook Host](reference_zenbook_host.md) — host on local LAN
- [Notion DB](reference_notion_db.md) — devlog DB pointer
"""

_REAL_SHAPED_COMPANIONS: dict[str, str] = {
    "chris_profile.md": "Chris is the project owner.",
    "project_parallax.md": "Parallax is the kernel project.",
    "project_orbit.md": "Orbit is the agent harness project.",
    "project_aphelion.md": "Aphelion is the public knowledge layer.",
    "feedback_phantom_done.md": "Verify tool result before claiming done.",
    "feedback_notion_sync.md": "Always update Notion devlog and roadmap.",
    "reference_zenbook_host.md": "ssh chris at the LAN host for ZenBook.",
    "reference_notion_db.md": "Devlog DB pointer lives in shared notes.",
}


class TestRoundTripRealMemoryMd:
    def test_round_trip_real_memory_md(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-trip invariant on a real-shaped synthetic MEMORY.md."""
        mem_path = tmp_path / "MEMORY.md"
        mem_path.write_text(_REAL_SHAPED_MEMORY_MD, encoding="utf-8")
        _write_companions(tmp_path, _REAL_SHAPED_COMPANIONS)

        app = _make_app(tmp_path, monkeypatch)
        db_p = tmp_path / "export_test.db"

        conn = connect(db_p)
        try:
            ingest_memory_md(conn, memory_md_path=mem_path, user_id="chris")
        finally:
            conn.close()

        with TestClient(app) as client:
            resp = client.get("/export/memory_md", params={"user_id": "chris"})

        assert resp.status_code == 200
        body = resp.json()
        exported_entries = parse_memory_md(body["memory_md"])

        # Round-trip invariant: every entry parsed from input survives ingest + export.
        expected_count = len(parse_memory_md(_REAL_SHAPED_MEMORY_MD))
        assert len(exported_entries) == expected_count

        # Category breakdown (all 4 categories populated).
        by_cat: dict[str, int] = {}
        for e in exported_entries:
            by_cat[e.category] = by_cat.get(e.category, 0) + 1

        assert by_cat.get("user", 0) >= 1
        assert by_cat.get("project", 0) >= 1
        assert by_cat.get("feedback", 0) >= 1
        assert by_cat.get("reference", 0) >= 1
