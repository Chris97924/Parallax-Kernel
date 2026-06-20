"""Integration tests for D2 — MEMORY.md parser + ingest orchestrator."""

from __future__ import annotations

import pathlib
import sqlite3
import textwrap

import pytest

from parallax.memory_md import (
    CompanionFile,
    ingest_memory_md,
    parse_companion,
    parse_memory_md,
)
from parallax.migrations import migrate_to_latest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REAL_MEMORY_MD = pathlib.Path(
    "C:/Users/user/.claude/projects/C--Users-user/memory/MEMORY.md"
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    migrate_to_latest(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Synthetic MEMORY.md text
# ---------------------------------------------------------------------------

SYNTHETIC_MEMORY_MD = textwrap.dedent(
    """\
    # User
    - [Identity Card](identity.md) — A user identity card

    # Projects (Active)
    - [Project Alpha](alpha.md) — Alpha project description
    - [Project Beta](beta.md) — Beta project description

    # Feedback
    - [Good Feedback](good_feedback.md) — Some feedback

    # Reference
    - [Ref Doc](ref_doc.md) — A reference document
    """
)


def _write_companion(
    tmp_path: pathlib.Path,
    filename: str,
    name: str,
    description: str,
    ftype: str,
    body: str,
) -> pathlib.Path:
    p = tmp_path / filename
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {ftype}\n---\n\n{body}",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# D2 parser tests
# ---------------------------------------------------------------------------


class TestParseMemoryMd:
    def test_parse_memory_md_4_sections(self) -> None:
        entries = parse_memory_md(SYNTHETIC_MEMORY_MD)
        categories = {e.category for e in entries}
        assert categories == {"user", "project", "feedback", "reference"}

    def test_correct_entry_count(self) -> None:
        entries = parse_memory_md(SYNTHETIC_MEMORY_MD)
        assert len(entries) == 5

    def test_entry_fields(self) -> None:
        entries = parse_memory_md(SYNTHETIC_MEMORY_MD)
        identity = next(e for e in entries if e.filename == "identity.md")
        assert identity.title == "Identity Card"
        assert identity.category == "user"
        assert identity.description == "A user identity card"

    def test_em_dash_and_ascii_dash_both_parse(self) -> None:
        text = textwrap.dedent(
            """\
            # User
            - [Em Dash Card](em.md) — em-dash description
            - [Ascii Dash Card](ascii.md) - ascii dash description
            """
        )
        entries = parse_memory_md(text)
        assert len(entries) == 2
        filenames = {e.filename for e in entries}
        assert filenames == {"em.md", "ascii.md"}
        em = next(e for e in entries if e.filename == "em.md")
        ascii_ = next(e for e in entries if e.filename == "ascii.md")
        assert em.description == "em-dash description"
        assert ascii_.description == "ascii dash description"


class TestParseCompanion:
    def test_parse_companion_frontmatter(self, tmp_path: pathlib.Path) -> None:
        p = _write_companion(
            tmp_path, "test.md", "Test Name", "Test desc", "user", "body text here"
        )
        companion = parse_companion(p)
        assert isinstance(companion, CompanionFile)
        assert companion.name == "Test Name"
        assert companion.description == "Test desc"
        assert companion.type == "user"
        assert companion.body == "body text here"

    def test_malformed_raises_value_error(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / "bad.md"
        p.write_text("no frontmatter here\njust plain text", encoding="utf-8")
        with pytest.raises(ValueError):
            parse_companion(p)


# ---------------------------------------------------------------------------
# D2 ingest tests
# ---------------------------------------------------------------------------


def _make_synthetic_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a synthetic MEMORY.md with 3 companions in tmp_path."""
    md = tmp_path / "MEMORY.md"
    md.write_text(
        textwrap.dedent(
            """\
            # User
            - [Card A](card_a.md) — description A

            # Projects (Active)
            - [Card B](card_b.md) — description B

            # Feedback
            - [Card C](card_c.md) — description C
            """
        ),
        encoding="utf-8",
    )
    for name, fname, ftype in [
        ("Card A", "card_a.md", "user"),
        ("Card B", "card_b.md", "project"),
        ("Card C", "card_c.md", "feedback"),
    ]:
        _write_companion(tmp_path, fname, name, f"desc {fname}", ftype, f"body {fname}")
    return md


class TestIngestMemoryMd:
    def test_idempotent_ingest(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        md_path = _make_synthetic_dir(tmp_path)

        report1 = ingest_memory_md(conn, memory_md_path=md_path, user_id="test_user")
        assert report1.cards_inserted == 3
        assert report1.cards_updated == 0

        report2 = ingest_memory_md(conn, memory_md_path=md_path, user_id="test_user")
        assert report2.cards_inserted == 0
        assert report2.cards_updated == 3

    def test_missing_companion_skipped(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        md = tmp_path / "MEMORY.md"
        md.write_text(
            textwrap.dedent(
                """\
                # User
                - [Missing](missing_file.md) — this file does not exist
                """
            ),
            encoding="utf-8",
        )
        report = ingest_memory_md(conn, memory_md_path=md, user_id="test_user")
        assert "missing_file.md" in report.skipped_missing_companion
        assert report.cards_inserted == 0

    def test_malformed_frontmatter_skipped(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        md = tmp_path / "MEMORY.md"
        md.write_text(
            "# User\n- [Bad](bad.md) — broken frontmatter\n",
            encoding="utf-8",
        )
        bad = tmp_path / "bad.md"
        bad.write_text("no frontmatter\njust text", encoding="utf-8")

        report = ingest_memory_md(conn, memory_md_path=md, user_id="test_user")
        assert "bad.md" in report.skipped_malformed
        assert report.cards_inserted == 0

    @pytest.mark.skipif(
        not REAL_MEMORY_MD.exists(),
        reason="Real MEMORY.md not present on this host",
    )
    def test_real_chris_memory_md(self, conn: sqlite3.Connection) -> None:
        report = ingest_memory_md(
            conn, memory_md_path=REAL_MEMORY_MD, user_id="chris"
        )
        # S7: body-only regex filter — prose mentions of 'token' no longer
        # trigger the filter. All 10 companions should now be persisted.
        total = report.cards_inserted + report.cards_updated
        skipped = len(report.skipped_privacy)
        assert skipped == 0, (
            f"Expected 0 privacy-skipped cards after S7 fix, got {skipped}: "
            f"{report.skipped_privacy}"
        )
        assert total >= 10, f"Expected >=10 persisted cards, got {total}"

        rows = conn.execute(
            "SELECT DISTINCT category FROM memory_cards WHERE user_id = 'chris'"
        ).fetchall()
        persisted_categories = {r[0] for r in rows}
        assert persisted_categories >= {
            "user",
            "project",
            "feedback",
            "reference",
        }, f"Expected all 4 categories, got {persisted_categories}"
