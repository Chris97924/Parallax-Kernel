"""Mutation-hardening for ``parallax.router.real_adapter`` (land-20260823 w4 S3).

Additive companion to ``test_real_adapter_query.py``,
``test_real_adapter_ingest.py``, ``test_real_adapter_backfill.py``,
``test_real_adapter_integration.py`` and
``test_adr007_change_trace_dispatch.py``. Every test below exists because a
semantic mutant of the module SURVIVED those suites. 34 mutants were applied
one at a time.

This module is the best-tested of the five in this lane — the dispatch table,
the TEMPORAL_CONTEXT precondition, the ADR-007 sub-dispatch, the ingest type
guards and the ``health()`` report are all genuinely pinned. Two areas are not.

  * **The H-1 query-limit cap has no test at all.** ``_MAX_QUERY_LIMIT = 500``
    exists, per its own comment, to stop ``request.limit=sys.maxsize`` becoming
    a SQL ``LIMIT`` and an OOM. Nothing anywhere reads it, and nothing observes
    ``capped_limit``: the constant can be raised a hundredfold, the clamp can be
    deleted outright, its ``max(..., 1)`` floor can be dropped to 0, its
    ``min``/``max`` can be swapped so every query is forced to 500, or a single
    branch can quietly forward the raw ``request.limit`` — and the whole suite
    stays green, because every existing test uses the default limit of 10 and
    asserts on hits rather than on what the retriever was asked for.

  * **Every alias ladder is pinned only at its first rung.** The declared order
    of these tuples IS the precedence contract — the module docstring says
    "Declared-order = canonical precedence" — but the existing tests only ever
    put two aliases in a payload at once: ``body`` vs ``object_``, ``object_``
    vs ``object``. Anything below rung two is unobserved, so ``summary`` can be
    promoted over ``object_``/``text`` and the three shorter tuples can be
    reversed wholesale without a red test. The tests below walk each ladder end
    to end: every alias present at once, then the winner removed and the next
    one demanded, rung by rung.

Also closed here: ``_derive_body``'s ``full``-before-``evidence`` source order
and its memory/claim key selection, neither of which any existing test can see
because no test gives a hit two competing sources or checks WHICH ladder was
used.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

import parallax.router.real_adapter as _adapter_mod
from parallax.router.contracts import IngestRequest, QueryRequest
from parallax.router.real_adapter import (
    _MAX_QUERY_LIMIT,
    CLAIM_OBJECT_KEYS,
    CLAIM_PREDICATE_KEYS,
    CLAIM_SUBJECT_KEYS,
    MEMORY_BODY_KEYS,
    MEMORY_TITLE_KEYS,
    RealMemoryRouter,
    _derive_body,
)
from parallax.router.types import QueryType

_USER = "harden_adapter_user"

# Every retriever ``query()`` can dispatch to, with the QueryType that selects
# it. CHANGE_TRACE's bug variant is reached through params, not query_type, so
# it is exercised separately below.
_DISPATCH_TARGETS = [
    (QueryType.RECENT_CONTEXT, "recent_context"),
    (QueryType.ARTIFACT_CONTEXT, "by_file"),
    (QueryType.ENTITY_PROFILE, "by_entity"),
    (QueryType.CHANGE_TRACE, "by_decision"),
    (QueryType.TEMPORAL_CONTEXT, "by_timeline"),
]


@pytest.fixture()
def recorded_limits(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace every dispatch target with a stub that records its ``limit``.

    ``query()`` imports ``parallax.retrieve`` inside the method body (to keep
    the router package's import-discipline test green), so patching attributes
    on the module object is what the production code will see.

    Recording the argument is the only way to observe the clamp: the retrievers
    themselves treat any limit >= the corpus size identically, which is exactly
    why every existing test is blind to it.
    """
    from parallax import retrieve as _retrieve

    seen: dict[str, int] = {}

    def _make(name: str) -> Any:
        def _stub(_conn: object, **kwargs: Any) -> tuple[()]:
            seen[name] = kwargs["limit"]
            return ()

        return _stub

    for _, retriever in _DISPATCH_TARGETS:
        monkeypatch.setattr(_retrieve, retriever, _make(retriever))
    monkeypatch.setattr(_retrieve, "by_bug_fix", _make("by_bug_fix"))
    return seen


def _query(router: RealMemoryRouter, query_type: QueryType, limit: int) -> None:
    """Issue one query of *query_type* with *limit*, satisfying its preconditions."""
    router.query(
        QueryRequest(
            query_type=query_type,
            user_id=_USER,
            q="anything",
            limit=limit,
            # TEMPORAL_CONTEXT refuses to run without both bounds; harmless
            # for the other four.
            since="2020-01-01T00:00:00+00:00",
            until="2030-01-01T00:00:00+00:00",
        )
    )


# ---------------------------------------------------------------------------
# The H-1 query-limit cap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_query_limit_cap_is_five_hundred() -> None:
    """The cap is a DoS contract and no test reads it.

    Its own comment says why it exists: "cap caller-supplied limit before
    forwarding into the SQL LIMIT parameter. Prevents OOM/DoS via
    request.limit=sys.maxsize". Pinned as a literal because the behavioural
    tests below would otherwise move with it.
    """
    assert _MAX_QUERY_LIMIT == 500


@pytest.mark.unit
@pytest.mark.parametrize(("query_type", "retriever"), _DISPATCH_TARGETS)
def test_every_dispatch_branch_clamps_a_huge_limit(
    conn: sqlite3.Connection,
    recorded_limits: dict[str, int],
    query_type: QueryType,
    retriever: str,
) -> None:
    """All five branches must forward 500, not the caller's number.

    Parametrized over every branch on purpose: the clamp is computed once but
    consumed five times, so a single branch that forwards ``request.limit``
    instead of ``capped_limit`` is a one-word change that four of five
    assertions would miss.
    """
    _query(RealMemoryRouter(conn), query_type, limit=10_000)

    assert recorded_limits[retriever] == 500


@pytest.mark.unit
def test_the_change_trace_bug_branch_clamps_too(
    conn: sqlite3.Connection, recorded_limits: dict[str, int]
) -> None:
    """``by_bug_fix`` is reached through params, so the parametrize above misses it."""
    RealMemoryRouter(conn).query(
        QueryRequest(
            query_type=QueryType.CHANGE_TRACE,
            user_id=_USER,
            limit=10_000,
            params={"legacy_kind": "bug"},
        )
    )

    assert recorded_limits["by_bug_fix"] == 500


@pytest.mark.unit
@pytest.mark.parametrize(
    ("requested", "forwarded"),
    [
        (-1, 1),  # nonsense input floors at 1, never goes negative
        (0, 1),  # LIMIT 0 would return nothing at all
        (1, 1),  # the floor itself
        (7, 7),  # inside the window: passed through untouched
        (499, 499),
        (500, 500),  # the cap itself is allowed
        (501, 500),  # one past it is not
    ],
)
def test_the_limit_is_clamped_into_the_one_to_five_hundred_window(
    conn: sqlite3.Connection,
    recorded_limits: dict[str, int],
    requested: int,
    forwarded: int,
) -> None:
    """Both ends of ``min(max(request.limit, 1), _MAX_QUERY_LIMIT)``.

    Every existing test uses the default limit of 10, which sits in the middle
    of the window and is therefore returned unchanged by the real expression,
    by a deleted clamp, by a floor of 0 and by a widened cap alike. 0 and 501
    are the two inputs that separate them; 7 is the counter-test that the clamp
    is not simply pinning everything to a constant (a swapped ``min``/``max``
    forces every query to 500 and would otherwise pass the 501 case).
    """
    _query(RealMemoryRouter(conn), QueryType.RECENT_CONTEXT, limit=requested)

    assert recorded_limits["recent_context"] == forwarded


# ---------------------------------------------------------------------------
# Alias ladders: pinned only at rung one today
# ---------------------------------------------------------------------------


def _walk_ladder(
    ladder: tuple[str, ...],
    base_payload: dict[str, Any],
    ingest: Any,
    read_back: Any,
) -> None:
    """Assert *ladder* is consumed strictly in declared order.

    Loads every alias at once with a value naming its own key, ingests, and
    demands the first rung win; then deletes that rung and repeats. A ladder of
    length n is therefore pinned by n assertions covering all n rungs, instead
    of the single "first beats second" assertion the existing suite makes.
    """
    payload = dict(base_payload)
    for key in ladder:
        payload[key] = f"value-{key}"

    for expected_winner in ladder:
        identifier = ingest(dict(payload))
        assert read_back(identifier) == f"value-{expected_winner}", (
            f"with {sorted(k for k in payload if k in ladder)} present, "
            f"{expected_winner!r} must win"
        )
        del payload[expected_winner]


@pytest.mark.unit
def test_memory_body_alias_precedence_is_the_whole_declared_ladder(
    conn: sqlite3.Connection,
) -> None:
    """Seven rungs, of which the existing suite pins two.

    ``test_ingest_memory_body_alias_first_key_wins`` proves ``body`` beats
    ``object_``; the fall-through tests each supply exactly one alias. So
    everything from rung three down is free to be reordered — promoting
    ``summary`` above ``object_`` and ``text``, for instance, silently changes
    which field of a rich payload becomes the memory's summary.
    """
    assert MEMORY_BODY_KEYS == (
        "body",
        "object_",
        "object",
        "payload_text",
        "text",
        "summary",
        "description",
    )

    router = RealMemoryRouter(conn)

    def _ingest(payload: dict[str, Any]) -> str:
        return router.ingest(
            IngestRequest(user_id=_USER, kind="memory", payload=payload)
        ).identifier

    def _read(identifier: str) -> str:
        return conn.execute(
            "SELECT summary FROM memories WHERE memory_id = ?", (identifier,)
        ).fetchone()["summary"]

    _walk_ladder(MEMORY_BODY_KEYS, {"vault_path": "v.md"}, _ingest, _read)


@pytest.mark.unit
def test_claim_object_alias_precedence_is_the_whole_declared_ladder(
    conn: sqlite3.Connection,
) -> None:
    """Six rungs, of which the existing suite pins two (``object_`` vs ``object``)."""
    assert CLAIM_OBJECT_KEYS == (
        "object_",
        "object",
        "body",
        "payload_text",
        "text",
        "summary",
    )

    router = RealMemoryRouter(conn)

    def _ingest(payload: dict[str, Any]) -> str:
        return router.ingest(
            IngestRequest(user_id=_USER, kind="claim", payload=payload)
        ).identifier

    def _read(identifier: str) -> str:
        return conn.execute(
            "SELECT object FROM claims WHERE claim_id = ?", (identifier,)
        ).fetchone()["object"]

    _walk_ladder(
        CLAIM_OBJECT_KEYS,
        {"subject": "alice", "predicate": "drinks"},
        _ingest,
        _read,
    )


@pytest.mark.unit
def test_claim_subject_alias_precedence_is_the_whole_declared_ladder(
    conn: sqlite3.Connection,
) -> None:
    """``subject`` then ``entity`` then ``name``.

    ``test_ingest_claim_subject_alias_entity_fallback`` supplies ``entity``
    alone, so it passes under any ordering that contains ``entity`` — including
    the exact reversal that makes ``name`` outrank ``subject``.
    """
    assert CLAIM_SUBJECT_KEYS == ("subject", "entity", "name")

    router = RealMemoryRouter(conn)

    def _ingest(payload: dict[str, Any]) -> str:
        return router.ingest(
            IngestRequest(user_id=_USER, kind="claim", payload=payload)
        ).identifier

    def _read(identifier: str) -> str:
        return conn.execute(
            "SELECT subject FROM claims WHERE claim_id = ?", (identifier,)
        ).fetchone()["subject"]

    _walk_ladder(
        CLAIM_SUBJECT_KEYS,
        {"predicate": "drinks", "object_": "coffee"},
        _ingest,
        _read,
    )


@pytest.mark.unit
def test_claim_predicate_alias_precedence_is_the_whole_declared_ladder(
    conn: sqlite3.Connection,
) -> None:
    """``predicate`` beats ``event_type``; only a payload with both can say so."""
    assert CLAIM_PREDICATE_KEYS == ("predicate", "event_type")

    router = RealMemoryRouter(conn)

    def _ingest(payload: dict[str, Any]) -> str:
        return router.ingest(
            IngestRequest(user_id=_USER, kind="claim", payload=payload)
        ).identifier

    def _read(identifier: str) -> str:
        return conn.execute(
            "SELECT predicate FROM claims WHERE claim_id = ?", (identifier,)
        ).fetchone()["predicate"]

    _walk_ladder(
        CLAIM_PREDICATE_KEYS,
        {"subject": "alice", "object_": "coffee"},
        _ingest,
        _read,
    )


@pytest.mark.unit
def test_memory_title_alias_precedence_is_the_whole_declared_ladder(
    conn: sqlite3.Connection,
) -> None:
    """``title`` beats ``name``; the existing test supplies ``name`` alone."""
    assert MEMORY_TITLE_KEYS == ("title", "name")

    router = RealMemoryRouter(conn)

    def _ingest(payload: dict[str, Any]) -> str:
        return router.ingest(
            IngestRequest(user_id=_USER, kind="memory", payload=payload)
        ).identifier

    def _read(identifier: str) -> str:
        return conn.execute(
            "SELECT title FROM memories WHERE memory_id = ?", (identifier,)
        ).fetchone()["title"]

    _walk_ladder(
        MEMORY_TITLE_KEYS,
        {"body": "a body", "vault_path": "v.md"},
        _ingest,
        _read,
    )


# ---------------------------------------------------------------------------
# _derive_body: which source, and which ladder
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_derive_body_prefers_the_full_row_over_the_evidence_blurb() -> None:
    """``full`` is the L3 row snapshot; ``evidence`` is an L2 one-liner.

    No existing test gives a hit two competing Mapping sources — they pair a
    resolvable ``full`` with an unrelated ``evidence``, or an unresolvable
    ``full`` with a str ``evidence`` — so the declared source order is
    unobserved and can be reversed. Reversed, a hit whose evidence happens to
    carry a body-shaped key would report the blurb as the canonical body.
    """
    hit = SimpleNamespace(
        entity_kind="memory",
        entity_id="m-both-sources",
        title="fallback-title",
        full={"body": "from-the-full-row"},
        evidence={"body": "from-the-evidence-blurb"},
    )

    assert _derive_body(hit) == "from-the-full-row"


@pytest.mark.unit
def test_derive_body_reads_memories_and_claims_through_their_own_ladders() -> None:
    """A memory resolves via MEMORY_BODY_KEYS, a claim via CLAIM_OBJECT_KEYS.

    The two ladders overlap heavily — both contain ``body``, ``object_``,
    ``object``, ``payload_text``, ``text`` and ``summary`` — so swapping which
    kind gets which is invisible to any payload containing a shared alias.
    ``description`` is in the memory ladder only, and ``object_`` outranks
    ``body`` for claims but not for memories, so these two hits are what make
    the selection decidable.
    """
    memory_hit = SimpleNamespace(
        entity_kind="memory",
        entity_id="m-desc-only",
        title="fallback-title",
        # ``description`` exists in MEMORY_BODY_KEYS and NOT in CLAIM_OBJECT_KEYS
        full={"description": "memory-ladder-only"},
        evidence=None,
    )
    assert _derive_body(memory_hit) == "memory-ladder-only"

    claim_hit = SimpleNamespace(
        entity_kind="claim",
        entity_id="c-precedence",
        title="fallback-title",
        # ``object_`` outranks ``body`` for claims; for memories it is the
        # other way round.
        full={"body": "memory-order-wins", "object_": "claim-order-wins"},
        evidence=None,
    )
    assert _derive_body(claim_hit) == "claim-order-wins"


@pytest.mark.unit
def test_derive_body_falls_back_to_title_for_an_unknown_entity_kind() -> None:
    """Neither ladder applies to an event or a future kind; the title stands in."""
    hit = SimpleNamespace(
        entity_kind="event",
        entity_id="e-1",
        title="an event title",
        full={"body": "ignored because the kind has no ladder"},
        evidence=None,
    )

    assert _derive_body(hit) == "an event title"


@pytest.mark.unit
def test_derive_body_returns_an_empty_string_when_even_the_title_is_missing() -> None:
    """``body`` is documented as always a ``str``, never None — including here."""
    hit = SimpleNamespace(
        entity_kind="event", entity_id="e-2", title=None, full=None, evidence=None
    )

    assert _derive_body(hit) == ""


@pytest.mark.unit
def test_derive_body_is_reachable_from_the_module_under_its_public_name() -> None:
    """Guards the indirection the tests above rely on.

    ``query()`` calls ``_derive_body`` through the module namespace, so a
    rename would leave these unit tests exercising a function nothing calls.
    """
    assert _adapter_mod._derive_body is _derive_body


# ---------------------------------------------------------------------------
# The hit DTO: asserted by key NAME today, never by what each key holds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_hit_dto_field_reads_the_field_it_names(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_assert_evidence_contract`` checks the key SET, never the values.

    It asserts ``set(hit.keys()) == {...}`` plus the types of ``score`` and
    ``explain``, so which source field feeds which DTO key is entirely
    unobserved: ``id`` and ``text`` can be swapped, ``created_at`` can read
    ``updated_at``, and ``full`` can stop falling back to ``evidence``. Every
    value below is distinct and self-naming so a swap cannot coincide.

    The retriever is stubbed rather than seeded because a real row cannot hold
    ``full=None`` next to a populated ``evidence`` — which is precisely the
    shape the ``full``-to-``evidence`` fallback exists for.
    """
    from parallax import retrieve as _retrieve

    crafted = _retrieve.RetrievalHit(
        entity_kind="memory",
        entity_id="the-entity-id",
        title="the-title",
        score=0.25,
        evidence="the-evidence-blurb",
        full={
            "created_at": "the-created-at",
            "updated_at": "the-updated-at",
            "source_id": "the-source-id",
            "body": "the-body",
        },
        explain={"reason": "because", "score_components": {"lexical": 1.0}},
    )
    monkeypatch.setattr(_retrieve, "recent_context", lambda *a, **k: (crafted,))

    evidence = RealMemoryRouter(conn).query(
        QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id=_USER)
    )

    assert len(evidence.hits) == 1
    hit = evidence.hits[0]
    assert hit["id"] == "the-entity-id"
    assert hit["text"] == "the-title"
    assert hit["body"] == "the-body"
    assert hit["created_at"] == "the-created-at", "created_at must not read updated_at"
    assert hit["source_id"] == "the-source-id"
    assert hit["kind"] == "memory"
    assert hit["score"] == 0.25
    assert hit["evidence"] == "the-evidence-blurb"
    assert hit["full"] == crafted.full
    assert hit["explain"] == {"reason": "because", "score_components": {"lexical": 1.0}}


@pytest.mark.unit
def test_a_hit_with_no_full_row_reports_its_evidence_as_full(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``full`` falls back to ``evidence`` so an L2-only hit still carries something.

    Nothing observes this fallback: no existing test constructs a hit with
    ``full=None``, so dropping the ``if h.full is not None else h.evidence``
    tail leaves the DTO reporting ``None`` for every L2-only hit — a key the
    contract test still sees as present, because it only checks key names.
    """
    from parallax import retrieve as _retrieve

    l2_only = _retrieve.RetrievalHit(
        entity_kind="claim",
        entity_id="c-l2-only",
        title="l2 title",
        score=1.0,
        evidence="confidence=0.7 state=auto",
        full=None,
        explain={"reason": "r", "score_components": {}},
    )
    monkeypatch.setattr(_retrieve, "recent_context", lambda *a, **k: (l2_only,))

    evidence = RealMemoryRouter(conn).query(
        QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id=_USER)
    )

    hit = evidence.hits[0]
    assert hit["full"] == "confidence=0.7 state=auto"
    assert hit["created_at"] == "", "a missing full row yields the documented empty string"
    assert hit["source_id"] == ""


@pytest.mark.unit
def test_the_adapter_declares_no_diversity_reranking(conn: sqlite3.Connection) -> None:
    """``diversity_mode="none"`` is a claim about what the adapter did NOT do.

    ``RetrievalEvidence`` carries it so a downstream consumer can tell a raw
    dispatch from an MMR-reranked result set. The existing contract helper
    checks ``stages`` and ``notes`` but never this field, so the adapter can
    claim to have diversified results it merely passed through.
    """
    evidence = RealMemoryRouter(conn).query(
        QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id=_USER)
    )

    assert evidence.diversity_mode == "none"
    assert evidence.sql_fragments == ()


# ---------------------------------------------------------------------------
# The ingest kind allowlist
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["event", "decision", "source", "MEMORY", ""])
def test_only_memory_and_claim_are_accepted_ingest_kinds(
    conn: sqlite3.Connection, kind: str
) -> None:
    """The allowlist is two entries wide, and only a bogus string tests it.

    ``test_ingest_unsupported_kind_raises`` passes ``"bogus"``, which any
    widened allowlist still rejects. The kinds that matter are the plausible
    ones — ``event`` names a real table in this schema, and ``MEMORY`` is the
    case-variant a hand-rolled caller sends. ``IngestRequest`` is a plain frozen
    dataclass, so its ``Literal`` hint stops nothing at runtime; this guard is
    the only thing between an unvalidated caller and the claim-alias parser
    running over a non-claim payload.
    """
    request = IngestRequest(user_id=_USER, kind="memory", payload={"body": "x"})
    object.__setattr__(request, "kind", kind)

    with pytest.raises(ValueError, match="unsupported ingest kind"):
        RealMemoryRouter(conn).ingest(request)


# ---------------------------------------------------------------------------
# health(): asserted on the MOCK router, not on this one
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_real_router_reports_all_four_ports_and_five_query_types(
    conn: sqlite3.Connection,
) -> None:
    """The existing ``ports_registered`` assertions are all about other objects.

    ``test_mock_and_seed``, ``test_contract_skeleton``, ``test_flag_wiring`` and
    ``test_contracts`` each pin the four-port tuple — on ``MockMemoryRouter`` or
    on a hand-built ``HealthReport``. ``RealMemoryRouter.health()`` has its own
    ``_PORTS`` constant and its own ``len(QueryType)``, and dropping a port from
    it leaves every one of those green. This is the readiness answer an operator
    reads to decide whether the router is fully wired.
    """
    report = RealMemoryRouter(conn).health()

    assert report.ok is True
    assert report.ports_registered == (
        "QueryPort",
        "IngestPort",
        "InspectPort",
        "BackfillPort",
    )
    assert report.query_type_count == 5
    assert isinstance(report.flag_enabled, bool)
    assert report.crosswalk_seed_hash
