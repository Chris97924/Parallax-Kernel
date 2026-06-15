"""Tests for D4 — ingest-side privacy filter."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.memory_md import (
    SECRET_KEYWORDS,
    contains_secret,
    ingest_memory_md,
)
from parallax.migrations import migrate_to_latest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    migrate_to_latest(c)
    yield c
    c.close()


def _write_companion(
    tmp_path: pathlib.Path,
    filename: str,
    name: str,
    description: str,
    ftype: str,
    body: str,
) -> None:
    p = tmp_path / filename
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {ftype}\n---\n\n{body}",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# contains_secret tests
# ---------------------------------------------------------------------------


class TestContainsSecret:
    def test_contains_secret_case_insensitive(self) -> None:
        assert contains_secret("PASSWORD") is True
        assert contains_secret("api_key=abc") is True
        assert contains_secret("use API-KEY here") is True
        assert contains_secret("My Secret value") is True
        assert contains_secret("load .env file") is True

    def test_clean_text_not_filtered(self) -> None:
        assert contains_secret("This is a normal sentence") is False
        assert contains_secret("username: chris") is False

    def test_benign_keywords_also_filtered(self) -> None:
        # 'tokenization' contains substring 'token' — spec says substring match
        assert contains_secret("tokenization algorithm") is True

    def test_secret_keywords_tuple(self) -> None:
        assert isinstance(SECRET_KEYWORDS, tuple)
        assert "password" in SECRET_KEYWORDS
        assert "token" in SECRET_KEYWORDS
        assert "api_key" in SECRET_KEYWORDS
        assert "api-key" in SECRET_KEYWORDS
        assert "secret" in SECRET_KEYWORDS
        assert "credential" in SECRET_KEYWORDS
        assert ".env" in SECRET_KEYWORDS
        assert "private_key" in SECRET_KEYWORDS


# ---------------------------------------------------------------------------
# Privacy filter integration tests
# ---------------------------------------------------------------------------


class TestIngestPrivacyFilter:
    def test_ingest_skips_secret_body(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        md = tmp_path / "MEMORY.md"
        md.write_text(
            "# User\n- [Safe Card](safe.md) — safe description\n",
            encoding="utf-8",
        )
        _write_companion(
            tmp_path,
            "safe.md",
            "Safe Card",
            "safe description",
            "user",
            "password=abc123xyz stored here",  # secret in body (8+ char value)
        )

        report = ingest_memory_md(conn, memory_md_path=md, user_id="test_user")
        assert "safe.md" in report.skipped_privacy
        assert report.cards_inserted == 0

        row = conn.execute(
            "SELECT id FROM memory_cards WHERE user_id = 'test_user' AND filename = 'safe.md'"
        ).fetchone()
        assert row is None, "Secret card should not be in DB"

    def test_ingest_skips_secret_description(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        # body-only regex scope; substring filter was dropped for being too
        # aggressive — see PRD S7. Description containing 'api_key' in prose
        # no longer triggers the filter; only body key=value patterns do.
        md = tmp_path / "MEMORY.md"
        md.write_text(
            "# Reference\n- [API Ref](api_ref.md) — contains api_key for service\n",
            encoding="utf-8",
        )
        _write_companion(
            tmp_path,
            "api_ref.md",
            "API Reference",
            "safe description",
            "reference",
            "normal body text",
        )

        report = ingest_memory_md(conn, memory_md_path=md, user_id="test_user")
        assert "api_ref.md" not in report.skipped_privacy
        assert report.cards_inserted == 1

    def test_ingest_skips_secret_name(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        # body-only regex scope; substring filter was dropped for being too
        # aggressive — see PRD S7. Name containing 'secret' in prose no longer
        # triggers the filter; only body key=value patterns do.
        md = tmp_path / "MEMORY.md"
        md.write_text(
            "# Feedback\n- [Secret Config](secret_cfg.md) — normal description\n",
            encoding="utf-8",
        )
        _write_companion(
            tmp_path,
            "secret_cfg.md",
            "secret_xyz configuration",  # 'secret' in name — no longer filtered
            "normal description",
            "feedback",
            "normal body text",
        )

        report = ingest_memory_md(conn, memory_md_path=md, user_id="test_user")
        assert "secret_cfg.md" not in report.skipped_privacy
        assert report.cards_inserted == 1

    def test_benign_card_passes_through(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        md = tmp_path / "MEMORY.md"
        md.write_text(
            "# User\n- [Normal Card](normal.md) — completely benign description\n",
            encoding="utf-8",
        )
        _write_companion(
            tmp_path,
            "normal.md",
            "Normal Card",
            "completely benign description",
            "user",
            "just some regular content",
        )

        report = ingest_memory_md(conn, memory_md_path=md, user_id="test_user")
        assert report.cards_inserted == 1
        assert report.skipped_privacy == ()
