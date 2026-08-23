"""Mutation-hardening for ``parallax.retrieve`` (land-20260824 w5 S1).

Additive companion to ``test_retrieve.py`` / ``test_retrieve_api.py`` /
``test_retrieve_explain.py`` / ``test_retrieve_hit.py`` /
``test_by_bug_fix_order_by.py`` / ``test_by_entity_order_by.py`` /
``test_by_timeline_microsecond_boundary.py`` / ``test_fts_events_index.py`` /
``test_content_hash_user_id_scope.py``.

TALLY — applied 59 / killed-by-new 37 / already-covered 21 / equivalent 1 /
unaddressed 0. Fifty-nine semantic mutants were applied one at a time to a
pristine tree: 21 died against the pre-existing suite and 38 walked through
it. Of those 38 survivors, 37 are killed by the tests below and 1 is proven
EQUIVALENT rather than merely uncovered — see
``TestByBugFix.test_the_claim_dedup_guard_is_provably_dead_code``.

What the existing suite is blind to, and why
--------------------------------------------

  * **Scoring is asserted as an ordering, never as a number.** The whole suite
    checks "exact scores higher than prefix" and "hits are sorted", so every
    literal weight in the module — ``keyword=0.6``/``source=0.1`` in
    ``by_file``, ``0.5``/``0.5`` in ``by_decision``, ``0.7``/``0.1`` on
    bug-fix claims — can be re-balanced freely as long as the *relative*
    order survives. Worse, ``_recency_score``'s 24-hour half-life is never
    evaluated at a known age at all: it is only ever compared against another
    hit's score. The tests below pin the components as LITERAL numbers at a
    LITERAL age (24h ago -> exactly 0.5). That is deliberate and must stay
    that way: computing the expectation from the module's own constant is
    exactly what made the originals blind.

  * **``_recency_score``'s two fail-safes are never entered.** No test ever
    passes an unparseable ``created_at`` (so the ``except -> 0.0`` default can
    become ``1.0`` and rank garbage rows top) and none passes a *future*
    timestamp (so the ``max(..., 0.0)`` age clamp can be deleted; a row 48h
    in the future then scores 0.0 instead of 1.0 because the reciprocal goes
    negative).

  * **Every retrieval default is shadowed by an explicit argument.** The suite
    always passes ``limit=`` when it cares and never counts results when it
    does not, so ``recent_context(limit=20)``, ``by_timeline(limit=50)`` and
    ``explain_retrieve(limit=10)`` are all unobserved. The two merge-and-cut
    paths (``by_bug_fix`` / ``by_entity``) are worse: their ``hits[:limit]``
    truncation can be deleted outright, because no test drives more than
    ``limit`` rows through the *merged* event+claim list.

  * **The session anchor is asserted by count, not by identity.**
    ``test_latest_session_default`` only asserts ``len(hits) >= 3``, which
    stays true when ``recent_context`` anchors on ``session.end`` instead of
    ``session.start`` (falling back to "newest events for user" and returning
    MORE rows), and when the anchoring ``ORDER BY`` flips to ASC and picks the
    oldest session. The tests here assert which session the rows came from.

  * **The FTS/LIKE selector is only exercised away from its boundary.**
    ``test_short_query_uses_like_fallback`` uses a 2-char path and the plan
    tests use long ones, so ``len(path) >= _TRIGRAM_MIN_CHARS`` can become
    ``>`` and a 3-char query silently deserts the index (same rows, no index).
    The same hole hides ``by_bug_fix``'s ``all(len(tok) >= ...)`` guard, whose
    entire purpose is a *future* sub-trigram token. Both are pinned here
    through the trace / a monkeypatched token set.

  * **``by_decision``'s claim enrichment is only seen in its happy path.** One
    test records one ``claim.state_changed`` event with ``target_kind='claim'``
    and asserts ``entity_kind == 'event'`` — so the ``target_kind == 'claim'``
    guard can be dropped (any event whose ``target_id`` happens to collide
    with a claim id gets someone else's subject glued to its title) and the
    ``"{spo} — {title}"`` order can be reversed, invisibly. Nothing at all
    covers the ``event_type LIKE 'decision.%'`` half of the WHERE clause.

  * **The near-miss diagnostics are half-covered.** ``entity`` misses are
    tested; ``bug`` misses and the ``file`` corpus-empty note are not, so the
    ``kind in ('entity','file','bug')`` tuple can lose ``'bug'`` and the file
    branch can lose its "corpus empty" note. The 80-char payload sample
    budget is never read either.

  * **Two escaping helpers are only tested on the characters they were
    written for.** ``_like_escape`` is covered for ``%`` and ``_`` but not for
    the backslash it doubles FIRST (drop that and every later escape is
    itself mis-escaped), and ``_fts_phrase``'s double-quote doubling — the
    thing that stops a quote in a subject from breaking out of the FTS5
    string literal — is never exercised.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import pathlib
import sqlite3

import pytest
from ulid import ULID

import parallax.retrieve as _retr
from parallax.ingest import ingest_claim
from parallax.migrations import migrate_to_latest
from parallax.retrieve import (
    _FIX_TOKENS,
    _RETRIEVE_KINDS,
    _TRIGRAM_MIN_CHARS,
    RetrievalHit,
    _claim_to_hit,
    _event_title,
    _event_to_hit,
    _events_fts_available,
    _fts_phrase,
    _iso_normalize,
    _like_escape,
    _recency_score,
    by_bug_fix,
    by_decision,
    by_entity,
    by_file,
    by_timeline,
    explain_retrieve,
    recent_context,
)
from parallax.sqlite_store import connect

_U = "u"


# ---------------------------------------------------------------------------
# Helpers (deliberately local: this file must not inherit the originals' habits)
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    """Fully migrated store — ``events_fts`` (migration 0014) present."""
    c = connect(tmp_path / "retrieve_harden.db")
    migrate_to_latest(c)
    yield c
    c.close()


@pytest.fixture()
def bare_conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    """A store that has ``events`` but NOT ``events_fts`` (pre-0014 / schema.sql)."""
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, user_id TEXT)")
    yield c
    c.close()


def _iso_ago(hours: float) -> str:
    """An ISO-8601 UTC stamp ``hours`` in the past (negative = future)."""
    ts = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=hours)
    return ts.isoformat(timespec="microseconds")


def _raw_event(
    conn: sqlite3.Connection,
    *,
    user_id: str = _U,
    event_type: str = "note",
    payload_json: str = "{}",
    created_at: str | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """Insert one event straight through SQL.

    Deliberately bypasses ``record_event``: several mutants below are only
    observable on rows the validator would refuse to create (an event whose
    ``target_id`` is a claim id but whose ``target_kind`` is not ``claim``),
    or need ``created_at`` pinned to an exact string. The ``events_fts``
    triggers fire on this INSERT exactly as they do for the real writer.
    """
    eid = str(ULID())
    conn.execute(
        """INSERT INTO events(event_id, user_id, actor, event_type, target_kind,
                              target_id, payload_json, approval_tier,
                              created_at, session_id)
           VALUES (?, ?, 'system', ?, ?, ?, ?, NULL, ?, ?)""",
        (
            eid,
            user_id,
            event_type,
            target_kind,
            target_id,
            payload_json,
            created_at if created_at is not None else _iso_ago(0.0),
            session_id,
        ),
    )
    conn.commit()
    return eid


def _claim_hits(hits: list[RetrievalHit]) -> list[RetrievalHit]:
    return [h for h in hits if h.entity_kind == "claim"]


def _event_hits(hits: list[RetrievalHit]) -> list[RetrievalHit]:
    return [h for h in hits if h.entity_kind == "event"]


# ---------------------------------------------------------------------------
# RetrievalHit.project — L3 fallback
# ---------------------------------------------------------------------------


class TestRetrievalHitProjection:
    @staticmethod
    def _hit(**overrides: object) -> RetrievalHit:
        defaults: dict = {
            "entity_kind": "event",
            "entity_id": "e1",
            "title": "demo",
            "score": 0.42,
            "evidence": "because X",
            "full": {"event_id": "e1"},
            "explain": {"reason": "unit", "score_components": {"keyword": 0.42}},
        }
        defaults.update(overrides)
        return RetrievalHit(**defaults)  # type: ignore[arg-type]

    @pytest.mark.unit
    def test_l3_keeps_an_empty_full_row_instead_of_falling_back(self) -> None:
        """``full={}`` is a real (if empty) row and must NOT trigger the fallback.

        ``test_l3_fallback_when_full_is_none`` only ever passes ``full=None``,
        so ``self.full is not None`` can be relaxed to plain truthiness. Every
        falsy-but-present row then silently reports the L2 evidence string in
        the ``full`` slot — a caller doing ``p["full"]["event_id"]`` gets a
        ``TypeError`` on a str instead of a clean ``KeyError`` on a dict.
        """
        p = self._hit(full={}).project(3)
        assert p["full"] == {}
        assert p["full"] != "because X"


# ---------------------------------------------------------------------------
# _recency_score — the half-life, the clamp, and the parse fail-safe
# ---------------------------------------------------------------------------


class TestRecencyScore:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("age_hours", "expected"),
        [
            (0.0, 1.0),
            (24.0, 0.5),
            (72.0, 0.25),
            (216.0, 0.1),
        ],
    )
    def test_the_half_life_ladder_is_exactly_24_hours(
        self, age_hours: float, expected: float
    ) -> None:
        """1/(1 + age_h/24) evaluated at four literal ages.

        No existing test ever evaluates this function at a known age — scores
        are only ever compared against each other — so the 24.0 divisor can be
        changed to anything and every ordering assertion in the suite still
        holds. The expectations here are hand-computed literals on purpose:
        importing the constant to build them would restore the blindness.
        """
        now = _dt.datetime(2024, 6, 15, 12, 0, 0, tzinfo=_dt.UTC)
        created = (now - _dt.timedelta(hours=age_hours)).isoformat()
        assert _recency_score(created, now) == pytest.approx(expected, abs=1e-12)

    @pytest.mark.unit
    def test_a_future_timestamp_is_clamped_to_the_freshest_score(self) -> None:
        """A clock-skewed row 48h in the future must score 1.0, not 0.0.

        Nothing passes a future ``created_at``, so ``max(..., 0.0)`` on the age
        looks redundant — the trailing ``min(1.0, ...)`` seems to cover it. It
        does not: at age -48h the reciprocal is ``1/(1-2) == -1.0``, which the
        min passes through and the OUTER max floors to 0.0. Deleting the age
        clamp therefore sends future-dated rows to the BOTTOM of the ranking,
        and at exactly -24h it divides by zero.
        """
        now = _dt.datetime(2024, 6, 15, 12, 0, 0, tzinfo=_dt.UTC)
        future = (now + _dt.timedelta(hours=48)).isoformat()
        assert _recency_score(future, now) == 1.0

    @pytest.mark.unit
    def test_an_exactly_24h_future_timestamp_does_not_divide_by_zero(self) -> None:
        """age_h == -24 makes the unclamped denominator exactly zero."""
        now = _dt.datetime(2024, 6, 15, 12, 0, 0, tzinfo=_dt.UTC)
        future = (now + _dt.timedelta(hours=24)).isoformat()
        assert _recency_score(future, now) == 1.0

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", ["", "not-a-timestamp", "2024-13-45T99:99:99Z"])
    def test_an_unparseable_created_at_scores_zero_not_one(self, bad: str) -> None:
        """The parse fail-safe must rank garbage LAST, not first.

        The ``except Exception: return 0.0`` branch is never entered by the
        suite, so the default can be flipped to 1.0 — which would promote every
        row with a corrupt timestamp to the top of every recency-ranked result.
        """
        assert _recency_score(bad) == 0.0


# ---------------------------------------------------------------------------
# _iso_normalize — the keyword-only kind allowlist
# ---------------------------------------------------------------------------


class TestIsoNormalizeKindGuard:
    @pytest.mark.unit
    def test_only_since_and_until_are_accepted_kinds(self) -> None:
        """A third kind must be rejected, not silently treated as 'since'.

        The suite only ever calls with the two legal kinds, so widening the
        allowlist is invisible. Widening it matters: an unrecognised kind falls
        through to the ``kind == 'until'`` test, gets ``since`` semantics, and
        silently drops every event in the final second of the window.
        """
        with pytest.raises(ValueError, match="kind must be 'since' or 'until'"):
            _iso_normalize("2024-06-15T12:00:00Z", kind="between")

    @pytest.mark.unit
    def test_the_two_legal_kinds_still_normalize(self) -> None:
        """Guard-tightening control: neither legal kind may start raising."""
        assert (
            _iso_normalize("2024-06-15T12:00:00Z", kind="since")
            == "2024-06-15T12:00:00.000000+00:00"
        )
        assert (
            _iso_normalize("2024-06-15T12:00:00Z", kind="until")
            == "2024-06-15T12:00:00.999999+00:00"
        )


# ---------------------------------------------------------------------------
# Escaping helpers
# ---------------------------------------------------------------------------


class TestLikeEscape:
    @pytest.mark.unit
    def test_the_escape_character_itself_is_doubled_first(self) -> None:
        r"""A literal backslash must become ``\\`` BEFORE ``%``/``_`` are escaped.

        ``test_by_file_underscore_does_not_wildcard`` and
        ``test_by_entity_percent_does_not_wildcard`` cover the two wildcards but
        never a backslash, so the first ``.replace`` can be deleted. It is the
        load-bearing one: with it gone, a path containing ``\`` emits a lone
        escape character into the LIKE pattern, which either mis-escapes the
        following byte or makes SQLite reject the expression outright.
        """
        assert _like_escape(r"a\b") == r"a\\b"
        assert _like_escape("\\") == "\\\\"

    @pytest.mark.unit
    def test_ordering_control_a_backslash_before_a_wildcard(self) -> None:
        r"""``a\%b``: the backslash doubles, then the ``%`` gets its own escape."""
        assert _like_escape(r"a\%b") == r"a\\\%b"

    @pytest.mark.unit
    def test_a_backslash_path_matches_literally_through_by_file(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r"""End-to-end on the LIKE branch: ``dir\a.py`` must find only itself."""
        monkeypatch.setattr(_retr, "_events_fts_available", lambda _c: False)
        wanted = _raw_event(
            conn,
            event_type="tool.edit",
            payload_json='{"file_path": "dir\\a.py"}',
        )
        _raw_event(
            conn,
            event_type="tool.edit",
            payload_json='{"file_path": "dirXa.py"}',
        )
        hits = by_file(conn, user_id=_U, path="dir\\a.py")
        assert [h.entity_id for h in hits] == [wanted]


class TestFtsPhraseQuoting:
    @pytest.mark.unit
    def test_embedded_double_quotes_are_doubled(self) -> None:
        '''A ``"`` in user text must be escaped per FTS5 string-literal rules.

        Nothing in the suite retrieves on a subject/path containing a double
        quote, so the ``replace('"', '""')`` can be deleted. Deleted, the quote
        terminates the FTS5 phrase early and the remainder of the user's text
        is re-interpreted as FTS5 *operators* — at best a syntax error, at
        worst a match the caller never asked for.
        '''
        assert _fts_phrase('a"b') == '"a""b"'
        assert _fts_phrase("plain") == '"plain"'

    @pytest.mark.unit
    def test_a_quoted_subject_survives_the_fts_path(
        self, conn: sqlite3.Connection
    ) -> None:
        """A subject containing ``"`` must query cleanly against events_fts."""
        wanted = _raw_event(conn, payload_json='note say "hi" now here')
        _raw_event(conn, payload_json="note say hi now here")
        hits = by_entity(conn, user_id=_U, subject='say "hi" now')
        assert [h.entity_id for h in _event_hits(hits)] == [wanted]


class TestEventsFtsAvailability:
    @pytest.mark.unit
    def test_a_store_with_events_but_no_events_fts_reports_unavailable(
        self, bare_conn: sqlite3.Connection
    ) -> None:
        """The probe must name ``events_fts``, not just any events-ish table.

        Every test runs on a fully migrated DB, so the probe's ``name =`` filter
        is never falsified: a mutant that looks for ``events`` instead answers
        True everywhere the suite looks. On a real pre-0014 or
        ``schema.sql``-bootstrapped store (stress / canary harnesses) that
        wrong True routes the query to a JOIN against a table that is not
        there.
        """
        assert _events_fts_available(bare_conn) is False

    @pytest.mark.unit
    def test_a_migrated_store_reports_available(
        self, conn: sqlite3.Connection
    ) -> None:
        """Positive control so the test above cannot pass by always-False."""
        assert _events_fts_available(conn) is True


# ---------------------------------------------------------------------------
# recent_context — session anchoring, ordering, default limit
# ---------------------------------------------------------------------------


class TestRecentContext:
    @pytest.mark.unit
    def test_the_session_anchor_is_session_start(
        self, conn: sqlite3.Connection
    ) -> None:
        """Anchoring on any other event type silently widens the scan.

        ``test_latest_session_default`` asserts only ``len(hits) >= 3``, which
        a mutant anchoring on ``session.end`` satisfies *more* easily: finding
        no such row it takes the "newest events for user" fallback and returns
        every session's rows. Asserted here by session identity.
        """
        _raw_event(conn, event_type="session.start", session_id="sA",
                   created_at=_iso_ago(10.0))
        _raw_event(conn, session_id="sA", created_at=_iso_ago(9.0))
        _raw_event(conn, session_id="sB", created_at=_iso_ago(1.0))

        hits = recent_context(conn, user_id=_U)

        assert hits, "the session.start anchor must resolve to a session"
        assert {h.full["session_id"] for h in hits} == {"sA"}

    @pytest.mark.unit
    def test_the_newest_session_start_wins_not_the_oldest(
        self, conn: sqlite3.Connection
    ) -> None:
        """The anchoring SELECT's ``ORDER BY created_at DESC`` is load-bearing.

        With two sessions present, flipping that ORDER BY to ASC resurrects the
        *first* session the user ever opened. No existing test seeds two
        ``session.start`` rows, so the flip is invisible.
        """
        _raw_event(conn, event_type="session.start", session_id="sOld",
                   created_at=_iso_ago(100.0))
        _raw_event(conn, session_id="sOld", created_at=_iso_ago(99.0))
        _raw_event(conn, event_type="session.start", session_id="sNew",
                   created_at=_iso_ago(2.0))
        _raw_event(conn, session_id="sNew", created_at=_iso_ago(1.0))

        hits = recent_context(conn, user_id=_U)

        assert {h.full["session_id"] for h in hits} == {"sNew"}

    @pytest.mark.unit
    def test_hits_come_back_newest_first(self, conn: sqlite3.Connection) -> None:
        """``reverse=True`` on the score sort is the whole point of the call.

        Nothing asserts the direction: the suite checks membership and counts.
        Flipping it to ascending hands the injector the STALEST event as the
        top "recent context" hit.
        """
        _raw_event(conn, event_type="session.start", session_id="sA",
                   created_at=_iso_ago(200.0))
        old = _raw_event(conn, session_id="sA", created_at=_iso_ago(150.0))
        mid = _raw_event(conn, session_id="sA", created_at=_iso_ago(50.0))
        new = _raw_event(conn, session_id="sA", created_at=_iso_ago(1.0))

        hits = recent_context(conn, user_id=_U)
        ranked = [h.entity_id for h in hits]

        assert ranked[0] == new
        assert ranked.index(mid) < ranked.index(old)
        assert [h.score for h in hits] == sorted(
            (h.score for h in hits), reverse=True
        )

    @pytest.mark.unit
    def test_the_default_limit_is_twenty(self, conn: sqlite3.Connection) -> None:
        """Every existing call either passes ``limit=`` or ignores the count.

        Pinned twice: as the signature default (a literal, not the module's own
        name) and behaviourally, by driving more rows than a shrunken default
        would return.
        """
        assert inspect.signature(recent_context).parameters["limit"].default == 20

        _raw_event(conn, event_type="session.start", session_id="sA",
                   created_at=_iso_ago(50.0))
        for i in range(8):
            _raw_event(conn, session_id="sA", created_at=_iso_ago(40.0 - i))

        assert len(recent_context(conn, user_id=_U)) == 9


# ---------------------------------------------------------------------------
# by_file — score components and the trigram boundary
# ---------------------------------------------------------------------------


class TestByFile:
    @pytest.mark.unit
    def test_the_score_components_are_the_shipped_weights(
        self, conn: sqlite3.Connection
    ) -> None:
        """0.6 keyword / 0.3x recency / 0.1 source, as literals.

        The suite only asserts that a file hit exists. Any re-balance that
        preserves the total — 0.5/0.2 for instance — is invisible, yet the
        ``explain`` block is the audit record a reviewer reads to justify why
        this row outranked another.
        """
        _raw_event(
            conn,
            event_type="tool.edit",
            payload_json='{"file_path": "parallax/retrieve.py"}',
            created_at=_iso_ago(0.0),
        )
        hits = by_file(conn, user_id=_U, path="parallax/retrieve.py")

        assert len(hits) == 1
        comp = hits[0].explain["score_components"]
        assert set(comp) == {"keyword", "recency", "source"}
        assert comp["keyword"] == 0.6
        assert comp["source"] == 0.1
        # recency is 0.3 x a ~1.0 weight for a just-written row.
        assert comp["recency"] == pytest.approx(0.3, abs=0.01)

    @pytest.mark.unit
    def test_the_trigram_floor_is_three_and_inclusive(
        self, conn: sqlite3.Connection
    ) -> None:
        """A path of EXACTLY 3 characters must still use the index.

        ``test_short_query_uses_like_fallback`` probes 2 chars and the plan
        tests probe long ones, so nothing sits on the boundary: relaxing
        ``>=`` to ``>`` returns identical rows via an unindexed
        leading-wildcard scan, and no assertion in the suite can see it. Read
        off the trace, which records the filter actually issued.
        """
        assert _TRIGRAM_MIN_CHARS == 3

        _raw_event(
            conn,
            event_type="tool.edit",
            payload_json='{"file_path": "abc"}',
            created_at=_iso_ago(0.0),
        )
        trace = explain_retrieve(conn, kind="file", user_id=_U, query_text="abc")

        assert len(trace.hits) == 1
        assert trace.normalized_params["payload_pattern"].startswith("user_tag:")
        assert any("events_fts" in frag for frag in trace.sql_fragments)


# ---------------------------------------------------------------------------
# by_decision — the decision.* branch, the enrichment guard, the title order
# ---------------------------------------------------------------------------


class TestByDecision:
    @staticmethod
    def _seed_claim(conn: sqlite3.Connection) -> str:
        return ingest_claim(
            conn,
            user_id=_U,
            subject="Widget",
            predicate="status",
            object_="shipped",
        )

    @pytest.mark.unit
    def test_decision_family_events_are_retrieved(
        self, conn: sqlite3.Connection
    ) -> None:
        """The ``event_type LIKE 'decision.%'`` half of the WHERE has no test.

        Only ``claim.state_changed`` is ever seeded, so the OR branch can be
        deleted and every ``decision.*`` audit row disappears from the decision
        view while the suite stays green.
        """
        wanted = _raw_event(conn, event_type="decision.adopted")

        hits = by_decision(conn, user_id=_U)

        assert [h.entity_id for h in hits] == [wanted]

    @pytest.mark.unit
    def test_enrichment_requires_target_kind_claim(
        self, conn: sqlite3.Connection
    ) -> None:
        """An event pointing at a claim id under a DIFFERENT kind must NOT enrich.

        The suite's one enrichment test uses a well-formed
        ``target_kind='claim'`` row, so the ``== 'claim'`` half of the guard is
        free. Dropping it means any event whose ``target_id`` collides with a
        claim id — ids are ULIDs from one shared minter — inherits a stranger's
        subject/predicate/object into its title.
        """
        claim_id = self._seed_claim(conn)
        _raw_event(
            conn,
            event_type="claim.state_changed",
            target_kind="memory",
            target_id=claim_id,
        )

        hits = by_decision(conn, user_id=_U)
        titles = [h.title for h in hits if h.full["target_kind"] == "memory"]

        assert titles == [f"claim.state_changed [memory:{claim_id}]"]
        assert not any(t.startswith("Widget status shipped") for t in titles)

    @pytest.mark.unit
    def test_the_enriched_title_puts_the_claim_first(
        self, conn: sqlite3.Connection
    ) -> None:
        """``"{spo} — {title}"`` — the order is the readability contract.

        The injector renders this string verbatim; reversing it produces
        "claim.state_changed [claim:01J...] — Widget status shipped", which
        buries the human-readable half behind an opaque id. Nothing asserts the
        title's shape today.
        """
        claim_id = self._seed_claim(conn)
        _raw_event(
            conn,
            event_type="claim.state_changed",
            target_kind="claim",
            target_id=claim_id,
        )

        hits = by_decision(conn, user_id=_U)
        enriched = [h for h in hits if h.full["target_id"] == claim_id]

        assert len(enriched) == 1
        assert enriched[0].title == (
            f"Widget status shipped — claim.state_changed [claim:{claim_id}]"
        )

    @pytest.mark.unit
    def test_the_score_components_are_half_keyword_half_recency(
        self, conn: sqlite3.Connection
    ) -> None:
        """0.5 flat + 0.5x recency, as literals."""
        _raw_event(conn, event_type="decision.adopted", created_at=_iso_ago(0.0))

        hits = by_decision(conn, user_id=_U)
        comp = hits[0].explain["score_components"]

        assert set(comp) == {"keyword", "recency"}
        assert comp["keyword"] == 0.5
        assert comp["recency"] == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# by_bug_fix — token surface, truncation, weights, the trigram fallback guard
# ---------------------------------------------------------------------------


class TestByBugFix:
    @pytest.mark.unit
    def test_the_fix_token_surface_is_the_five_shipped_tokens(self) -> None:
        """Pinned as a literal tuple; the suite only ever exercises two.

        ``test_bug_keyword_in_payload`` uses text containing both ``FIX-`` and
        ``bug``, so ``regression`` and ``hotfix`` are never the reason a row
        matched. Narrowing the tuple to ``("fix", "bug")`` therefore survives
        while silently dropping a whole class of incident notes.
        """
        assert _FIX_TOKENS == ("fix", "bug", "FIX-", "regression", "hotfix")

    @pytest.mark.unit
    @pytest.mark.parametrize("token", ["regression", "hotfix", "FIX-", "fix", "bug"])
    def test_every_token_independently_matches(
        self, conn: sqlite3.Connection, token: str
    ) -> None:
        """Each token must be sufficient on its own to surface an event."""
        wanted = _raw_event(conn, payload_json=f'{{"text": "note {token} note"}}')

        hits = by_bug_fix(conn, user_id=_U)

        assert wanted in [h.entity_id for h in hits]

    @pytest.mark.unit
    def test_the_merged_result_is_truncated_to_limit(
        self, conn: sqlite3.Connection
    ) -> None:
        """``hits[:limit]`` after the merge is the only thing bounding the list.

        Each of the two SELECTs carries its own ``LIMIT ?``, so a caller asking
        for 2 can still be handed up to 4 rows if the post-merge slice is
        removed. No existing test drives BOTH sides past the limit at once, so
        deleting the slice survives.
        """
        for i in range(3):
            _raw_event(
                conn,
                payload_json=f'{{"text": "a regression number {i}"}}',
                created_at=_iso_ago(float(i + 1)),
            )
        for i in range(3):
            ingest_claim(
                conn,
                user_id=_U,
                subject=f"bug report {i}",
                predicate="is",
                object_="open",
            )

        hits = by_bug_fix(conn, user_id=_U, limit=2)

        assert len(hits) == 2

    @pytest.mark.unit
    def test_a_matching_claim_scores_exactly_point_eight(
        self, conn: sqlite3.Connection
    ) -> None:
        """0.7 keyword + 0.1 source, as literals.

        The claim weight decides whether claims outrank events in the merged
        list; the suite only asserts a claim is *present*. Dropping it to 0.4
        pushes every claim below a fresh event without failing anything.
        """
        ingest_claim(
            conn,
            user_id=_U,
            subject="bug report",
            predicate="is",
            object_="open",
        )

        claims = _claim_hits(by_bug_fix(conn, user_id=_U))

        assert len(claims) == 1
        assert claims[0].explain["score_components"] == {
            "keyword": 0.7,
            "source": 0.1,
        }
        assert claims[0].score == 0.8

    @pytest.mark.unit
    def test_a_sub_trigram_token_forces_the_like_fallback(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``all(len(tok) >= floor)`` guard exists for a FUTURE short token.

        Every shipped token is already >= 3 chars, so today the guard is a
        no-op and deleting it changes nothing the suite can see. Its whole
        purpose is the day someone adds a 2-char token: the trigram index has
        no window that short, the MATCH quietly returns nothing, and the
        incident notes vanish. Exercised by monkeypatching the token set —
        which is precisely the future the guard is written for.
        """
        monkeypatch.setattr(_retr, "_FIX_TOKENS", ("fix", "bug", "zq"))
        wanted = _raw_event(conn, payload_json='{"text": "zq marker only"}')

        hits = by_bug_fix(conn, user_id=_U)

        assert wanted in [h.entity_id for h in hits]

    @pytest.mark.unit
    def test_the_claim_dedup_guard_is_provably_dead_code(
        self, conn: sqlite3.Connection
    ) -> None:
        """EQUIVALENCE PROOF for the ``if cid in seen: continue`` guard.

        Mutant R33 (delete the guard) survived the suite, and it survives this
        file too — because it is EQUIVALENT, not merely uncovered:

        1. ``claim_rows`` comes from ONE ``SELECT * FROM claims WHERE user_id
           = ? AND (<ORed LIKEs>)``. There is no JOIN and no UNION, so SQLite
           yields each qualifying base-table row exactly once however many
           disjuncts it satisfies.
        2. ``claim_id`` is ``TEXT PRIMARY KEY`` on ``claims`` (schema.sql),
           so distinct rows carry distinct ``cid`` values.

        Therefore ``cid in seen`` is unreachable and removing the guard cannot
        change any observable output. This test pins premise (1) — the property
        a future refactor would break by adding a JOIN — rather than pretending
        to kill the mutant.
        """
        ingest_claim(
            conn,
            user_id=_U,
            subject="bug and fix and regression",  # satisfies several disjuncts
            predicate="is",
            object_="a hotfix",
            confidence=0.9,
        )

        claims = _claim_hits(by_bug_fix(conn, user_id=_U))
        ids = [h.entity_id for h in claims]

        assert len(ids) == len(set(ids)) == 1


# ---------------------------------------------------------------------------
# by_timeline — window ordering, the equal-bounds case, the default limit
# ---------------------------------------------------------------------------


class TestByTimeline:
    @pytest.mark.unit
    def test_results_are_ordered_oldest_first(
        self, conn: sqlite3.Connection
    ) -> None:
        """"Events in a window ordered ascending" is the documented contract.

        ``test_window_filter`` asserts membership only. Flipping ``ASC`` to
        ``DESC`` reverses the narrative order of a replayed timeline and, once
        ``LIMIT`` bites, returns the WRONG END of the window entirely.
        """
        first = _raw_event(conn, created_at="2024-06-15T12:00:01.000000+00:00")
        second = _raw_event(conn, created_at="2024-06-15T12:00:02.000000+00:00")
        third = _raw_event(conn, created_at="2024-06-15T12:00:03.000000+00:00")

        hits = by_timeline(
            conn,
            user_id=_U,
            since="2024-06-15T12:00:00Z",
            until="2024-06-15T12:00:10Z",
        )

        assert [h.entity_id for h in hits] == [first, second, third]

    @pytest.mark.unit
    def test_an_instantaneous_window_is_legal(
        self, conn: sqlite3.Connection
    ) -> None:
        """since == until is a point query, not an error.

        ``test_since_after_until_raises`` only covers strictly-inverted bounds,
        so tightening ``>`` to ``>=`` is invisible — yet it turns every "what
        happened at exactly this instant" probe into a ValueError. Needs an
        explicit non-zero microsecond on both bounds, since a zero-microsecond
        ``until`` is expanded to ``.999999`` and would not compare equal.
        """
        ts = "2024-06-15T12:00:00.500000+00:00"
        wanted = _raw_event(conn, created_at=ts)

        hits = by_timeline(conn, user_id=_U, since=ts, until=ts)

        assert [h.entity_id for h in hits] == [wanted]

    @pytest.mark.unit
    def test_the_default_limit_is_fifty(self, conn: sqlite3.Connection) -> None:
        """Timeline's default is deliberately larger than the other five."""
        assert inspect.signature(by_timeline).parameters["limit"].default == 50

        for i in range(25):
            _raw_event(conn, created_at=f"2024-06-15T12:00:{i:02d}.000000+00:00")

        hits = by_timeline(
            conn,
            user_id=_U,
            since="2024-06-15T11:00:00Z",
            until="2024-06-15T13:00:00Z",
        )

        assert len(hits) == 25


# ---------------------------------------------------------------------------
# by_entity — exactness, prefix-vs-substring, truncation
# ---------------------------------------------------------------------------


class TestByEntity:
    @pytest.mark.unit
    def test_exactness_is_case_sensitive(self, conn: sqlite3.Connection) -> None:
        """A case-differing subject is a PREFIX hit (0.6), not an exact hit (1.0).

        ``test_exact_subject_match_scores_higher`` compares an exact hit
        against a different-subject hit, so making ``exact`` case-insensitive
        keeps the ordering and survives. It should not: the claim SELECT
        already matches case-insensitively via ``LOWER(subject) LIKE``, and the
        exact/prefix split is what separates "you asked for this entity" from
        "this entity starts with what you typed".
        """
        ingest_claim(
            conn,
            user_id=_U,
            subject="Widget",
            predicate="status",
            object_="green",
        )

        claims = _claim_hits(by_entity(conn, user_id=_U, subject="widget"))

        assert len(claims) == 1
        assert claims[0].explain["score_components"]["keyword"] == 0.6
        assert "prefix" in claims[0].explain["reason"]

    @pytest.mark.unit
    def test_an_exact_subject_scores_a_full_keyword_point(
        self, conn: sqlite3.Connection
    ) -> None:
        """Control for the test above, pinned as a literal 1.0."""
        ingest_claim(
            conn,
            user_id=_U,
            subject="Widget",
            predicate="status",
            object_="green",
        )

        claims = _claim_hits(by_entity(conn, user_id=_U, subject="Widget"))

        assert claims[0].explain["score_components"]["keyword"] == 1.0
        assert "exact" in claims[0].explain["reason"]

    @pytest.mark.unit
    def test_the_claim_match_is_a_prefix_not_a_substring(
        self, conn: sqlite3.Connection
    ) -> None:
        """``LIKE 'x%'`` — a mid-word hit must NOT surface the claim.

        No test asks for a subject that is a strict infix of a stored one, so
        the leading ``%`` can be added and ``by_entity('Widget')`` starts
        dragging in every ``*Widget*`` entity in the store. Only the CLAIM side
        is asserted: the event scan is a substring scan by design and legitimately
        matches the ``claim.created`` payload.
        """
        ingest_claim(
            conn,
            user_id=_U,
            subject="MegaWidget",
            predicate="status",
            object_="green",
        )

        hits = by_entity(conn, user_id=_U, subject="Widget")

        assert _claim_hits(hits) == []

    @pytest.mark.unit
    def test_the_merged_result_is_truncated_to_limit(
        self, conn: sqlite3.Connection
    ) -> None:
        """Same merge-and-cut hole as ``by_bug_fix``: two LIMITed SELECTs, one slice."""
        for i in range(3):
            ingest_claim(
                conn,
                user_id=_U,
                subject=f"Widget{i}",
                predicate="status",
                object_="green",
            )
        for i in range(3):
            _raw_event(
                conn,
                payload_json=f'{{"text": "Widget mentioned {i}"}}',
                created_at=_iso_ago(float(i + 1)),
            )

        hits = by_entity(conn, user_id=_U, subject="Widget", limit=2)

        assert len(hits) == 2


# ---------------------------------------------------------------------------
# explain_retrieve — the kind allowlist, the timeline guard, near-miss notes
# ---------------------------------------------------------------------------


class TestExplainRetrieve:
    @pytest.mark.unit
    def test_the_kind_allowlist_is_the_six_shipped_kinds(
        self, conn: sqlite3.Connection
    ) -> None:
        """Pinned as a literal tuple, and the guard proven to reject extras.

        ``test_unknown_kind_raises`` passes an obviously-bogus string, so
        WIDENING the tuple survives: the new kind sails past the guard and
        lands in the ``else: # timeline`` arm, where it is treated as a
        timeline query and raises a confusing "since and until are required"
        instead of "unknown kind".
        """
        assert _RETRIEVE_KINDS == (
            "recent",
            "file",
            "decision",
            "bug",
            "entity",
            "timeline",
        )
        with pytest.raises(ValueError, match="unknown kind"):
            explain_retrieve(conn, kind="all", user_id=_U)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("since", "until"),
        [
            (None, "2024-06-15T13:00:00Z"),
            ("2024-06-15T12:00:00Z", None),
            (None, None),
        ],
    )
    def test_timeline_requires_both_bounds_independently(
        self, conn: sqlite3.Connection, since: str | None, until: str | None
    ) -> None:
        """``or``, not ``and``: ONE missing bound is already fatal.

        ``test_timeline_requires_since_until`` omits both at once, which an
        ``and`` mutant still rejects. With only one omitted the mutant falls
        through to ``by_timeline(since=None)``, where ``_parse_iso`` raises an
        opaque ``AttributeError`` on None instead of the documented ValueError.
        """
        with pytest.raises(ValueError, match="since and until are required"):
            explain_retrieve(
                conn, kind="timeline", user_id=_U, since=since, until=until
            )

    @pytest.mark.unit
    def test_a_bug_miss_still_gets_near_miss_notes(
        self, conn: sqlite3.Connection
    ) -> None:
        """``'bug'`` is in the near-miss tuple and nothing checks it.

        The suite covers ``entity`` and ``file`` misses only, so dropping
        ``'bug'`` from ``kind in ('entity','file','bug')`` survives — and an
        operator debugging an empty bug-fix view loses the one signal that
        tells them whether the corpus is empty or merely non-matching.
        """
        ingest_claim(
            conn,
            user_id=_U,
            subject="Widget",
            predicate="status",
            object_="green",
        )

        trace = explain_retrieve(conn, kind="bug", user_id=_U)

        assert trace.hits == ()
        assert any(n.startswith("near_miss(bug)") for n in trace.notes)

    @pytest.mark.unit
    def test_a_file_miss_on_an_empty_corpus_says_so(
        self, conn: sqlite3.Connection
    ) -> None:
        """The file branch's corpus-empty note has no test.

        ``test_file_miss_returns_sample_notes`` seeds file events, so the
        ``if not rows`` arm is never taken and can be deleted — leaving an
        operator with a completely silent trace and no way to tell "no file
        events at all" from "the note builder failed".
        """
        _raw_event(conn, payload_json='{"text": "not a file event"}')

        trace = explain_retrieve(
            conn, kind="file", user_id=_U, query_text="nothing.py"
        )

        assert trace.hits == ()
        assert any("corpus empty" in n for n in trace.notes)
        assert any(n.startswith("near_miss(file)") for n in trace.notes)

    @pytest.mark.unit
    def test_the_near_miss_payload_sample_is_eighty_characters(
        self, conn: sqlite3.Connection
    ) -> None:
        """The sample budget is what makes a note diagnostic rather than decorative.

        Nothing reads the sample's length, so 80 can shrink to anything and the
        note degrades to a useless stub while still "containing a sample".
        Built from a payload whose 80-char prefix is known exactly.
        """
        payload = '{"file_path": "' + "x" * 100 + '"}'
        _raw_event(conn, event_type="tool.edit", payload_json=payload)

        trace = explain_retrieve(
            conn, kind="file", user_id=_U, query_text="no-such-path"
        )

        recent = [n for n in trace.notes if "near_miss(file) recent" in n]
        assert len(recent) == 1
        sample = recent[0].split(": ", 2)[2]
        assert sample == payload[:80]
        assert len(sample) == 80

    @pytest.mark.unit
    def test_the_default_limit_is_ten(self, conn: sqlite3.Connection) -> None:
        """The dispatcher's default is deliberately tighter than the callee's."""
        assert inspect.signature(explain_retrieve).parameters["limit"].default == 10

        for i in range(15):
            _raw_event(conn, created_at=_iso_ago(float(i + 1)))

        trace = explain_retrieve(conn, kind="recent", user_id=_U)

        assert len(trace.hits) == 10


# ---------------------------------------------------------------------------
# Hit builders — the shared formatting layer under every retrieval function
# ---------------------------------------------------------------------------


class TestHitBuilders:
    @pytest.mark.unit
    def test_scores_keep_six_decimal_places(self) -> None:
        """``round(..., 6)`` is what makes two near-tied hits orderable.

        Every score assertion in the suite is either a comparison or a
        two-decimal value, so the precision can be cut to 2 without failing
        anything — and at 2 places the recency component stops separating rows
        written minutes apart, collapsing the ranking into ties broken by
        arbitrary list order. Driven through the builder with literal
        components so the expectation is exact rather than wall-clock
        dependent.
        """
        hit = _event_to_hit(
            {"event_id": "e1"},
            reason="unit",
            score_components={"keyword": 0.1234567, "recency": 0.0000004},
        )
        assert hit.score == 0.123457

    @pytest.mark.unit
    def test_a_real_retrieved_score_is_not_a_two_decimal_value(
        self, conn: sqlite3.Connection
    ) -> None:
        """End-to-end control: a live recency term carries sub-percent detail."""
        _raw_event(conn, event_type="decision.adopted", created_at=_iso_ago(5.0))

        score = by_decision(conn, user_id=_U)[0].score

        assert abs(score - round(score, 2)) > 1e-9

    @pytest.mark.unit
    def test_the_title_target_suffix_needs_BOTH_kind_and_id(self) -> None:
        """``tkind and tid`` — one half alone must not render a target suffix.

        ``record_event`` refuses to write a half-populated target, so the suite
        never sees one and the ``and`` can become ``or``. Rows written before
        that validator existed (and anything inserted by a migration or an
        external writer) still carry them, and the ``or`` mutant renders them
        as ``event [claim:None]`` — a target reference that points nowhere.
        """
        assert _event_title({"event_type": "x", "target_kind": "claim", "target_id": None}) == "x"
        assert _event_title({"event_type": "x", "target_kind": None, "target_id": "c1"}) == "x"
        assert (
            _event_title({"event_type": "x", "target_kind": "claim", "target_id": "c1"})
            == "x [claim:c1]"
        )

    @pytest.mark.unit
    def test_a_half_populated_target_renders_without_a_suffix_end_to_end(
        self, conn: sqlite3.Connection
    ) -> None:
        """Same guard, observed through ``by_decision`` on a raw legacy-shaped row."""
        _raw_event(
            conn,
            event_type="decision.adopted",
            target_kind="claim",
            target_id=None,
        )

        assert [h.title for h in by_decision(conn, user_id=_U)] == ["decision.adopted"]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "row",
        [
            {"claim_id": "c1", "subject": "", "predicate": "", "object": ""},
            {"claim_id": "c1", "subject": "  ", "predicate": " ", "object": "  "},
            {"claim_id": "c1"},
        ],
    )
    def test_a_blank_claim_gets_the_placeholder_title(self, row: dict) -> None:
        """``or "(empty claim)"`` is the only thing standing between a caller and "".

        Every claim the suite builds has a non-empty subject, so the fallback
        is never exercised. Without it a blank-triple claim renders as an empty
        title, which the injector emits as a zero-width bullet and the CLI
        prints as a bare score with nothing attached to it.
        """
        hit = _claim_to_hit(row, reason="unit", score_components={"keyword": 0.7})
        assert hit.title == "(empty claim)"
