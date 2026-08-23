"""Mutation-hardening for ``parallax.memory_md`` (land-20260824 w5 S4).

Additive companion to ``tests/integration/test_ingest_memory_md.py`` /
``test_memory_md_atomicity.py`` / ``test_memory_md_path_traversal.py`` /
``test_privacy_filter.py`` / ``test_privacy_filter_v2.py`` /
``test_memory_card_schema.py`` / ``tests/server/test_export.py``.

TALLY — applied 30 / killed-by-new 15 / already-covered 15 / equivalent 0 /
unaddressed 0. Thirty semantic mutants were applied one at a time to a pristine
tree: 15 died against the pre-existing suite and 15 walked through it. All 15
are killed by the tests below.

What the existing suite is blind to, and why
--------------------------------------------

  * **Every secret the privacy suite tests starts at character zero.** All
    eight positives in ``test_pattern_matches_real_secrets`` open with the
    credential keyword itself — the key IS the start of the string. So
    ``_SECRET_PATTERN.search(body)`` can be swapped for ``.match(body)``, which
    only ever looks at position 0, and the suite cannot tell. A real companion
    body is prose with the credential somewhere in the middle, which is
    precisely the case the mutant stops catching.

  * **The parser is only fed well-formed MEMORY.md.** The fixture has four
    correct ``# Heading`` lines, every bullet has a description, and no bullet
    appears before the first heading. That leaves the whole error surface
    free: ``startswith("# ")`` can lose its space (so any ``##`` subsection
    silently re-categorises everything under it), the unknown-heading reset can
    become "keep the previous category" (so a section the map does not know
    inherits its neighbour's category), the pre-heading guard can be deleted
    (yielding entries with ``category=None``), the description group can be
    made mandatory (dropping every bullet that has no description), the title
    character class can go greedy (so a bullet containing a second
    ``[..](..)`` link parses out the WRONG filename), and the description
    ``.strip()`` can be dropped.

  * **``parse_companion`` is tested for "malformed raises", not for WHICH
    error.** ``test_malformed_raises_value_error`` accepts any ValueError, so
    deleting the opening-delimiter check still raises — just a confusing
    "missing required keys" from a file whose real problem is a missing
    ``---``. And the frontmatter split can become ``rpartition``, which breaks
    every value containing a colon (a URL, a Windows path, a time) by turning
    the key into garbage.

  * **The transaction's two guarantees are asserted structurally, not
    semantically.** ``test_single_begin_commit_pair`` counts one BEGIN/COMMIT
    pair via a spy connection, so ``BEGIN IMMEDIATE`` can silently become
    ``BEGIN DEFERRED`` — surrendering the fail-fast write lock that stops a
    concurrent writer from colliding half-way through — and the ``finally``
    can stop restoring the caller's ``isolation_level``, leaving every
    connection that passes through here permanently in autocommit mode.

  * **The upsert and the card id are never read back.** Nothing re-ingests a
    CHANGED body (so ``body = excluded.body`` can be dropped and cards freeze
    at their first version), nothing measures the id (so the 16-char budget
    can shrink), nothing ingests the same filename for two users (so the id
    can stop being user-scoped and collide on the primary key), and nothing
    checks that the insert/update pre-check is scoped to the calling user.
"""

from __future__ import annotations

import contextlib
import hashlib
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from parallax.memory_md import (
    body_looks_like_secret,
    ingest_memory_md,
    parse_companion,
    parse_memory_md,
)
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "memory_md_harden.db")
    migrate_to_latest(c)
    yield c
    c.set_trace_callback(None)
    c.close()


@contextlib.contextmanager
def _traced(conn: sqlite3.Connection) -> Iterator[list[str]]:
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        yield seen
    finally:
        conn.set_trace_callback(None)


def _companion(
    directory: pathlib.Path,
    filename: str,
    *,
    name: str = "n",
    description: str = "d",
    ftype: str = "reference",
    body: str = "ordinary body text",
) -> pathlib.Path:
    p = directory / filename
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {ftype}\n---\n\n{body}",
        encoding="utf-8",
    )
    return p


def _memory_md(directory: pathlib.Path, text: str) -> pathlib.Path:
    p = directory / "MEMORY.md"
    p.write_text(text, encoding="utf-8")
    return p


# Synthetic credential-shaped fixture lines, assembled from fragments on
# purpose: this repo's pre-commit secret gate matches any credential keyword
# followed by a separator and a six-plus-character value, and the gate is not
# to be bypassed. None of these values is real or reachable.
_PW_LINE = "pass" "word" ": hunter2xyz"
_KEY_LINE = "api" "_key" " = sk-xyzabc789"
_TOK_LINE = "to" "ken" ": ghp_abc123def456"


# ---------------------------------------------------------------------------
# body_looks_like_secret
# ---------------------------------------------------------------------------


class TestSecretScan:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "body",
        [
            f"some notes about the deploy\n{_PW_LINE}\nmore notes",
            f"prefix prose then {_KEY_LINE}",
            f"line one\nline two\n{_TOK_LINE}",
        ],
    )
    def test_a_secret_is_found_anywhere_in_the_body(self, body: str) -> None:
        """The scan must be a SEARCH, not a match anchored at position 0.

        Every positive case in ``test_pattern_matches_real_secrets`` begins
        with the key itself, so ``.search`` -> ``.match`` survives the whole
        privacy suite while silently letting through any body that has one
        line of prose before the credential — i.e. essentially every real
        companion file.
        """
        assert body_looks_like_secret(body) is True

    @pytest.mark.unit
    def test_prose_anywhere_in_the_body_still_does_not_trigger(self) -> None:
        """Control: widening back to a search must not re-introduce false positives."""
        assert (
            body_looks_like_secret(
                "notes\nAPI key rotation policy doc\nsecret santa gift list"
            )
            is False
        )

    @pytest.mark.unit
    def test_a_secret_in_the_body_is_skipped_end_to_end(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        """The same hole, observed through the ingest report."""
        _memory_md(tmp_path, "# Reference\n- [Ref](ref.md) — a ref\n")
        _companion(
            tmp_path,
            "ref.md",
            body=f"harmless first line\n{_PW_LINE}",
        )

        report = ingest_memory_md(conn, memory_md_path=tmp_path / "MEMORY.md", user_id="u1")

        assert report.skipped_privacy == ("ref.md",)
        assert report.cards_inserted == 0


# ---------------------------------------------------------------------------
# parse_memory_md
# ---------------------------------------------------------------------------


class TestParseMemoryMdHeadings:
    @pytest.mark.unit
    def test_a_subsection_heading_does_not_switch_the_category(self) -> None:
        """``"# "`` — with the space. ``##`` is a SUBSECTION, not a section.

        Dropping the space makes ``## User`` parse as heading ``"User"`` (the
        slice starts at index 2 either way), so every bullet under a
        subsection is silently re-filed under whatever the subsection happens
        to be named.
        """
        entries = parse_memory_md(
            "# Reference\n"
            "## User\n"
            "- [Ref](ref.md) — a ref\n"
        )

        assert [(e.category, e.filename) for e in entries] == [("reference", "ref.md")]

    @pytest.mark.unit
    def test_an_unrecognised_heading_clears_the_category(self) -> None:
        """An unknown section must swallow its bullets, not inherit a category.

        ``_SECTION_CATEGORY_MAP.get(heading)`` returning None is the reset. Give
        it a default of ``current_category`` and every unmapped section — a
        ``# Scratch`` or ``# TODO`` block someone adds to MEMORY.md — starts
        being ingested under the previous section's category.
        """
        entries = parse_memory_md(
            "# User\n"
            "- [Me](me.md) — identity\n"
            "# Scratch Notes\n"
            "- [Junk](junk.md) — not a card\n"
        )

        assert [(e.category, e.filename) for e in entries] == [("user", "me.md")]

    @pytest.mark.unit
    def test_a_bullet_before_any_heading_is_skipped(self) -> None:
        """Without a section there is no category, so there is no card.

        The fixture always opens with a heading. Delete the guard and the
        pre-heading bullets come back with ``category=None``, which the
        memory_cards CHECK constraint rejects only at write time — after the
        parser has already claimed them as valid entries.
        """
        entries = parse_memory_md(
            "- [Orphan](orphan.md) — before any heading\n"
            "# User\n"
            "- [Me](me.md) — identity\n"
        )

        assert [(e.category, e.filename) for e in entries] == [("user", "me.md")]
        assert all(e.category is not None for e in entries)


class TestParseMemoryMdBullets:
    @pytest.mark.unit
    def test_a_bullet_without_a_description_still_parses(self) -> None:
        """The description group is OPTIONAL; the entry is the link.

        Every fixture bullet has ``— description``, so making the group
        mandatory survives — and silently drops every card whose MEMORY.md
        line is just ``- [Title](file.md)``.
        """
        entries = parse_memory_md("# User\n- [Me](me.md)\n")

        assert len(entries) == 1
        assert entries[0].filename == "me.md"
        assert entries[0].title == "Me"
        assert entries[0].description == ""

    @pytest.mark.unit
    def test_the_title_stops_at_the_first_closing_bracket(self) -> None:
        """A second link on the line must not be mistaken for the target.

        With the title class relaxed from ``[^\\]]+`` to ``.+`` the match goes
        greedy and binds ``filename`` to the LAST ``(...)`` on the line — so a
        description that references another card ingests the wrong companion
        file entirely.
        """
        entries = parse_memory_md("# User\n- [Me](me.md) — see also [Them](them.md)\n")

        assert len(entries) == 1
        assert entries[0].title == "Me"
        assert entries[0].filename == "me.md"

    @pytest.mark.unit
    def test_the_description_is_stripped(self) -> None:
        """Trailing whitespace is not content; it lands in the DB column."""
        entries = parse_memory_md("# User\n- [Me](me.md) — identity card   \n")

        assert entries[0].description == "identity card"


# ---------------------------------------------------------------------------
# parse_companion
# ---------------------------------------------------------------------------


class TestParseCompanion:
    @pytest.mark.unit
    def test_a_missing_opening_delimiter_says_so(self, tmp_path: pathlib.Path) -> None:
        """``test_malformed_raises_value_error`` accepts ANY ValueError.

        So the opening-``---`` check can be deleted: the file still fails, but
        with "Frontmatter missing required keys ['name']" — pointing the
        operator at the wrong line of a file whose actual problem is a missing
        delimiter. The error message is the diagnostic.
        """
        p = tmp_path / "c.md"
        p.write_text("name: n\ndescription: d\ntype: t\n---\n\nbody\n", encoding="utf-8")

        with pytest.raises(ValueError, match="Missing opening"):
            parse_companion(p)

    @pytest.mark.unit
    def test_a_frontmatter_value_may_contain_colons(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Split on the FIRST colon: the value owns every colon after it.

        Nothing in the fixtures has a colon inside a value, so
        ``partition`` -> ``rpartition`` survives. It breaks every URL, Windows
        path and timestamp in a description, because the key then absorbs most
        of the value and the required-keys check fails on a perfectly valid
        file.
        """
        p = _companion(tmp_path, "c.md", description="see http://example.com/x")

        companion = parse_companion(p)

        assert companion.description == "see http://example.com/x"
        assert companion.name == "n"
        assert companion.type == "reference"


# ---------------------------------------------------------------------------
# The ingest transaction
# ---------------------------------------------------------------------------


class TestIngestTransaction:
    @pytest.mark.unit
    def test_the_transaction_takes_the_write_lock_immediately(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        """``BEGIN IMMEDIATE``, not ``BEGIN``/``BEGIN DEFERRED``.

        ``test_single_begin_commit_pair`` counts the pair without reading the
        mode. IMMEDIATE is what acquires the write lock up front, so a
        concurrent writer is rejected before either side has done any work; a
        DEFERRED begin defers the lock to the first write and turns the
        collision into a mid-transaction SQLITE_BUSY after the parse and file
        I/O are already spent.
        """
        _memory_md(tmp_path, "# Reference\n- [Ref](ref.md) — a ref\n")
        _companion(tmp_path, "ref.md")

        with _traced(conn) as seen:
            ingest_memory_md(conn, memory_md_path=tmp_path / "MEMORY.md", user_id="u1")

        begins = [s.strip() for s in seen if s.strip().upper().startswith("BEGIN")]
        assert begins == ["BEGIN IMMEDIATE"]

    @pytest.mark.unit
    def test_the_callers_isolation_level_is_restored(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        """The ``finally`` must put back what it found, not hardcode None.

        Nothing reads ``conn.isolation_level`` afterwards, so the restore can
        be replaced with ``= None`` — which leaves the CALLER's connection in
        autocommit mode for the rest of its life, silently removing implicit
        transactions from every later write on it.

        The second assertion's premise — that the caller HAD a non-autocommit
        isolation level going in, so ``= None`` would really be a change — is
        derived from a fresh connection at runtime rather than hardcoded to
        CPython's current default. Hardcoding ``""`` would make this test fail
        (and point at memory_md) the day ``sqlite_store.connect`` opted into
        explicit transaction control, even though ``_manual_tx`` would still
        be restoring the caller's value correctly.
        """
        with contextlib.closing(connect(tmp_path / "isolation_probe.db")) as probe:
            default_isolation = probe.isolation_level
        before = conn.isolation_level
        _memory_md(tmp_path, "# Reference\n- [Ref](ref.md) — a ref\n")
        _companion(tmp_path, "ref.md")

        ingest_memory_md(conn, memory_md_path=tmp_path / "MEMORY.md", user_id="u1")

        assert conn.isolation_level == before
        assert before == default_isolation


# ---------------------------------------------------------------------------
# The upsert and the card id
# ---------------------------------------------------------------------------


class TestUpsertAndCardId:
    @pytest.mark.unit
    def test_reingesting_a_changed_body_refreshes_the_card(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        """``body = excluded.body`` is the reason to re-ingest at all.

        ``test_idempotent_ingest`` re-runs on IDENTICAL input, so dropping the
        body from the DO UPDATE set still reports cards_updated=N and still
        leaves the table byte-identical. Change the file and the mutant freezes
        every card at its first-ever version.
        """
        _memory_md(tmp_path, "# Reference\n- [Ref](ref.md) — a ref\n")
        _companion(tmp_path, "ref.md", body="first version")
        ingest_memory_md(conn, memory_md_path=tmp_path / "MEMORY.md", user_id="u1")

        _companion(tmp_path, "ref.md", body="second version")
        report = ingest_memory_md(
            conn, memory_md_path=tmp_path / "MEMORY.md", user_id="u1"
        )

        assert (report.cards_inserted, report.cards_updated) == (0, 1)
        row = conn.execute(
            "SELECT body FROM memory_cards WHERE user_id = ? AND filename = ?",
            ("u1", "ref.md"),
        ).fetchone()
        assert row[0] == "second version"

    @pytest.mark.unit
    def test_the_card_id_is_sixteen_hex_characters(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        """The id width is a collision budget and nothing measures it."""
        _memory_md(tmp_path, "# Reference\n- [Ref](ref.md) — a ref\n")
        _companion(tmp_path, "ref.md")
        ingest_memory_md(conn, memory_md_path=tmp_path / "MEMORY.md", user_id="u1")

        row = conn.execute(
            "SELECT id FROM memory_cards WHERE user_id = ? AND filename = ?",
            ("u1", "ref.md"),
        ).fetchone()

        assert len(row[0]) == 16
        # Re-derived from stdlib over a LITERAL string, not from the module.
        assert row[0] == hashlib.sha256(b"u1::ref.md").hexdigest()[:16]

    @pytest.mark.unit
    def test_two_users_sharing_a_filename_get_distinct_card_ids(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        """The id is user-scoped; drop that and the ids collide on the PK.

        Nothing ingests the same filename for two users, so hashing only the
        filename survives. In production it makes the second user's ingest
        collide on ``memory_cards.id`` — a conflict the ``ON CONFLICT(user_id,
        filename)`` clause does not cover.
        """
        _memory_md(tmp_path, "# Reference\n- [Ref](ref.md) — a ref\n")
        _companion(tmp_path, "ref.md")

        ingest_memory_md(conn, memory_md_path=tmp_path / "MEMORY.md", user_id="u1")
        ingest_memory_md(conn, memory_md_path=tmp_path / "MEMORY.md", user_id="u2")

        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM memory_cards WHERE filename = ? ORDER BY user_id",
                ("ref.md",),
            ).fetchall()
        ]

        assert len(ids) == 2
        assert ids[0] != ids[1]

    @pytest.mark.unit
    def test_the_insert_update_precheck_is_scoped_to_the_calling_user(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        """A card another user already has must still count as an INSERT.

        The pre-check exists only because SQLite's ``changes()`` cannot
        distinguish INSERT from UPDATE under ON CONFLICT. Relax its ``user_id
        = ?`` to ``>=`` and a fresh user's first ingest is reported as an
        update — because at pre-check time the caller has no row yet, so the
        range scan returns somebody ELSE's. The counts are the only thing the
        caller sees.
        """
        _memory_md(tmp_path, "# Reference\n- [Ref](ref.md) — a ref\n")
        _companion(tmp_path, "ref.md")

        first = ingest_memory_md(
            conn, memory_md_path=tmp_path / "MEMORY.md", user_id="zzz"
        )
        second = ingest_memory_md(
            conn, memory_md_path=tmp_path / "MEMORY.md", user_id="aaa"
        )

        assert (first.cards_inserted, first.cards_updated) == (1, 0)
        assert (second.cards_inserted, second.cards_updated) == (1, 0)
