"""Mutation-hardening for ``parallax.server.viewer`` (land/20260824 wave 5, S4).

Additive companion to ``tests/server/test_viewer.py``,
``tests/server/test_multi_user_auth.py`` and ``tests/server/test_server_safety.py``.
Thirty-three semantic mutants were applied to a pristine tree one at a time
against that set.

Tally — applied 33 / killed by the pre-existing suite 19 / killed by the tests
below 14 / equivalent (excluded) 0 / unaddressed 0.

What the existing suite could not see
-------------------------------------
``test_viewer.py`` is built around the IDOR that this module was rewritten to
close, and it is thorough about it: spoofed ``user_id``, cross-principal reads
and the disabled-router case are all covered on all three endpoints, so every
mutant that reopens the leak dies immediately. What it never asks is what a
CORRECTLY scoped response contains:

* **Row count and row order are never asserted.** Every test seeds one or two
  rows and checks that they come back, so ``LIMIT`` can be ignored entirely,
  the slice can be off by one, and ``ORDER BY created_at DESC`` can become
  ASC — a debug viewer whose first page is the OLDEST events is worse than
  useless, and it looks identical to a working one on a two-row fixture.
* **The query-parameter contract has no boundary cases.** ``limit``'s default
  and its 1000 ceiling, and the 128-character ``user_id`` cap on all three
  routes, are never given a value at or past the edge.
* **The ``by_entity`` alias is only ever exercised through the happy path.**
  ``explain_retrieve`` accepts ``entity`` and not ``by_entity``, so the
  normalisation is load-bearing — but the tests pass the alias and check that
  *something* came back, which a 500 would not satisfy and a wrong-kind trace
  would.
* **The trace envelope is checked one key at a time.** ``stages`` and ``kind``
  are asserted; that the response is the full ``dataclasses.asdict`` of the
  trace — params included — is not, so the endpoint can stop forwarding the
  query text without any test noticing.

Expected values are literals: 100, 1000, 128, ``"entity"``, and the seeded
event ids in the order they must come back.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from parallax.migrations import migrate_to_latest
from parallax.server import create_app
from parallax.sqlite_store import connect, now_iso


@pytest.fixture()
def vdb(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "viewer_harden.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


@pytest.fixture()
def vapp(vdb: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Viewer mounted, open mode (no token) — the same posture test_viewer uses."""
    monkeypatch.setenv("PARALLAX_VIEWER_ENABLED", "1")
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(vdb))

    def factory() -> sqlite3.Connection:
        return connect(vdb)

    return create_app(db_factory=factory)


@pytest.fixture()
def vclient(vapp: FastAPI) -> Iterator[TestClient]:
    with TestClient(vapp) as c:
        yield c


def _seed_events(db_path: pathlib.Path, *, user_id: str, stamps: list[str]) -> list[str]:
    """Insert one event per stamp; return the ids in the order inserted."""
    ids = [f"ev-{i:04d}" for i in range(len(stamps))]
    conn = connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO events "
            "(event_id, user_id, actor, event_type, target_kind, target_id, "
            " payload_json, approval_tier, created_at, session_id) "
            "VALUES (?, ?, 'test', 'test.event', 'memory', 'm1', "
            "        '{\"k\":\"v\"}', NULL, ?, NULL)",
            [(eid, user_id, stamp) for eid, stamp in zip(ids, stamps, strict=True)],
        )
        conn.commit()
    finally:
        conn.close()
    return ids


def _stamps(n: int) -> list[str]:
    """n ascending ISO stamps, one second apart."""
    return [f"2026-04-21T00:{i // 60:02d}:{i % 60:02d}.000000+00:00" for i in range(n)]


def _seed_claims(db_path: pathlib.Path, *, user_id: str, count: int) -> None:
    """Insert ``count`` claims (each with its FK source) in one transaction."""
    stamp = now_iso()
    conn = connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO sources(source_id, uri, kind, content_hash, "
            "user_id, ingested_at, state) VALUES (?, ?, 'test', ?, ?, ?, 'active')",
            [
                (f"src-c{i}", f"test://c{i}", f"hash-c{i}", user_id, stamp)
                for i in range(count)
            ],
        )
        conn.executemany(
            "INSERT INTO claims(claim_id, user_id, subject, predicate, object, "
            "source_id, content_hash, confidence, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'is', 'a thing', ?, ?, 1.0, 'active', ?, ?)",
            [
                (
                    f"c{i}", user_id, f"subject-c{i}", f"src-c{i}",
                    f"hash-c{i}", stamp, stamp,
                )
                for i in range(count)
            ],
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /viewer/ — the page itself
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_index_serves_the_viewer_page_not_an_empty_body(vclient: TestClient) -> None:
    """The one route whose entire output IS its body.

    An empty 200 is the failure a status-code assertion cannot see, and this
    page is the only UI the debug viewer has: the three tab controls and the
    fetch calls that drive them all live in this string.
    """
    resp = vclient.get("/viewer/")

    assert resp.status_code == 200
    body = resp.text
    assert "parallax viewer" in body
    assert "<title>Parallax Viewer</title>" in body
    assert "/viewer/events.json" in body
    assert "/viewer/claims.json" in body
    assert "/viewer/retrieve.json" in body


# ---------------------------------------------------------------------------
# /viewer/events.json — order, limit, shape
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_events_come_back_newest_first(vclient: TestClient, vdb: pathlib.Path) -> None:
    """``ORDER BY created_at DESC`` — both halves of it, the column and the direction.

    A debug viewer opens on the most recent activity, so DESC is the contract;
    ``created_at`` is the other half, and it is the half a fixture that seeds
    rows in timestamp order cannot see. There, insertion order, ``rowid`` and
    ``event_id`` all ascend together, which makes ``ORDER BY rowid DESC`` — a
    routine "integer compare beats a string compare on an unindexed TEXT
    column" rewrite — produce exactly the same rows in exactly the same order
    as the real thing. Backdated rows are not hypothetical for this table:
    ``conflict_writer`` accepts an explicit ``now_us_utc`` precisely so a
    replayed event can carry a ``created_at`` that disagrees with its
    insertion position, and a first page silently ordered by insertion
    sequence looks identical to a working one.

    So the stamps are handed over out of order — ``ev-0001`` newest,
    ``ev-0002`` in the middle, ``ev-0000`` oldest. The expected order is still
    written out as literal ids rather than derived by sorting the response,
    and it now matches none of ``created_at ASC``, ``event_id``/``rowid ASC``
    or ``event_id``/``rowid DESC``.
    """
    s0, s1, s2 = _stamps(3)
    ids = _seed_events(vdb, user_id="u1", stamps=[s0, s2, s1])

    rows = vclient.get("/viewer/events.json", params={"user_id": "u1"}).json()

    assert [r["event_id"] for r in rows] == [ids[1], ids[2], ids[0]]


@pytest.mark.integration
def test_events_default_limit_is_one_hundred(
    vclient: TestClient, vdb: pathlib.Path
) -> None:
    """101 rows in, 100 out — the default is a real cap, not a formality.

    The viewer is a debug surface over an unbounded table; without the cap a
    single click can pull the whole events table into a browser tab. Asserted
    at 101 seeded rows because any fixture smaller than the cap cannot see it.
    """
    _seed_events(vdb, user_id="u1", stamps=_stamps(101))

    rows = vclient.get("/viewer/events.json", params={"user_id": "u1"}).json()

    assert len(rows) == 100


@pytest.mark.integration
def test_events_limit_is_honoured_and_capped_at_one_thousand(
    vclient: TestClient, vdb: pathlib.Path
) -> None:
    """An explicit limit is applied; 1000 is accepted and 1001 is not."""
    _seed_events(vdb, user_id="u1", stamps=_stamps(5))

    assert len(vclient.get(
        "/viewer/events.json", params={"user_id": "u1", "limit": 2}
    ).json()) == 2
    assert vclient.get(
        "/viewer/events.json", params={"user_id": "u1", "limit": 1000}
    ).status_code == 200
    assert vclient.get(
        "/viewer/events.json", params={"user_id": "u1", "limit": 1001}
    ).status_code == 422
    assert vclient.get(
        "/viewer/events.json", params={"user_id": "u1", "limit": 0}
    ).status_code == 422


@pytest.mark.integration
def test_events_rows_are_objects_carrying_the_aliased_column_names(
    vclient: TestClient, vdb: pathlib.Path
) -> None:
    """The wire names are ``kind`` and ``payload``, not the raw column names.

    The page's ``populateTable`` call selects exactly these six keys, so the
    SQL aliases are the contract between the query and the renderer: drop
    either alias and the table renders blank cells with a 200 and no error
    anywhere.
    """
    _seed_events(vdb, user_id="u1", stamps=_stamps(1))

    rows = vclient.get("/viewer/events.json", params={"user_id": "u1"}).json()

    assert len(rows) == 1
    assert set(rows[0]) == {
        "event_id", "kind", "target_kind", "target_id", "payload", "created_at",
    }
    assert rows[0]["kind"] == "test.event"
    assert rows[0]["payload"] == '{"k":"v"}'


@pytest.mark.integration
def test_events_user_id_is_capped_at_one_hundred_and_twenty_eight(
    vclient: TestClient
) -> None:
    """128 passes, 129 is rejected before the query is built."""
    assert vclient.get(
        "/viewer/events.json", params={"user_id": "x" * 128}
    ).status_code == 200
    assert vclient.get(
        "/viewer/events.json", params={"user_id": "x" * 129}
    ).status_code == 422


# ---------------------------------------------------------------------------
# /viewer/claims.json — the post-query slice
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_claims_limit_slices_the_result(vclient: TestClient, vdb: pathlib.Path) -> None:
    """``claims_by_user`` has no LIMIT, so the slice is the only cap there is.

    That makes it exactly the wrong place for an off-by-one or a dropped
    slice: the query already walked the whole table, so the failure is silent
    and unbounded rather than an error. Three seeded rows and a limit of two
    is the smallest fixture that separates ``[:limit]`` from ``[:limit+1]``
    and from no slice at all.
    """
    _seed_claims(vdb, user_id="u1", count=3)

    rows = vclient.get(
        "/viewer/claims.json", params={"user_id": "u1", "limit": 2}
    ).json()

    assert len(rows) == 2


@pytest.mark.integration
def test_claims_default_limit_is_one_hundred(
    vclient: TestClient, vdb: pathlib.Path
) -> None:
    """101 claims in, 100 out — the same cap the events route carries.

    Worse here than on events, because ``claims_by_user`` has already
    materialised the caller's entire claim set before the slice runs: raising
    the default does not just widen the response, it makes a single default
    request the whole-table read the cap was there to bound.
    """
    _seed_claims(vdb, user_id="u1", count=101)

    rows = vclient.get("/viewer/claims.json", params={"user_id": "u1"}).json()

    assert len(rows) == 100


@pytest.mark.integration
def test_claims_require_a_non_empty_user_id(vclient: TestClient) -> None:
    """``user_id`` is required and must not be blank.

    In open / single-token mode ``current_user_id`` returns the caller's value
    verbatim, so a missing or empty ``user_id`` would query the scope ``""``
    rather than fail — an unscoped-looking read with no error. The requirement
    is enforced at the parameter, before that can happen.
    """
    assert vclient.get("/viewer/claims.json").status_code == 422
    assert vclient.get(
        "/viewer/claims.json", params={"user_id": ""}
    ).status_code == 422


@pytest.mark.integration
def test_claims_limit_ceiling_matches_the_events_route(vclient: TestClient) -> None:
    """Both JSON routes share one 1000-row ceiling."""
    assert vclient.get(
        "/viewer/claims.json", params={"user_id": "u1", "limit": 1000}
    ).status_code == 200
    assert vclient.get(
        "/viewer/claims.json", params={"user_id": "u1", "limit": 1001}
    ).status_code == 422


# ---------------------------------------------------------------------------
# /viewer/retrieve.json — the alias, the query text, the envelope
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_by_entity_alias_is_normalised_to_entity(vclient: TestClient) -> None:
    """``by_entity`` is a UI label; ``explain_retrieve`` only knows ``entity``.

    The ``<select>`` on the page offers ``by_entity`` as its first option, so
    this rename is on the default path of the most-used tab. Dropping it sends
    a kind the retrieval layer does not accept; inverting it breaks the plain
    ``entity`` value instead. Both directions are pinned by asserting the
    resolved kind in the trace rather than merely that a response arrived.
    """
    by_alias = vclient.get(
        "/viewer/retrieve.json", params={"user_id": "u1", "kind": "by_entity", "q": "x"}
    )
    plain = vclient.get(
        "/viewer/retrieve.json", params={"user_id": "u1", "kind": "entity", "q": "x"}
    )

    assert by_alias.status_code == 200
    assert plain.status_code == 200
    assert by_alias.json()["kind"] == "entity"
    assert plain.json()["kind"] == "entity"


@pytest.mark.integration
def test_retrieve_defaults_to_the_entity_kind(vclient: TestClient) -> None:
    """Omitting ``kind`` runs an entity lookup — the page's default tab."""
    resp = vclient.get("/viewer/retrieve.json", params={"user_id": "u1", "q": "x"})

    assert resp.status_code == 200
    assert resp.json()["kind"] == "entity"


@pytest.mark.integration
def test_retrieve_forwards_the_query_text_and_the_principal_separately(
    vclient: TestClient
) -> None:
    """``q`` is the subject and ``user_id`` is the scope; they are not the same slot.

    Blanking ``q`` turns every explain into an empty-subject lookup that still
    answers 200 with a well-formed trace, and swapping the two produces a trace
    that looks complete while explaining the wrong thing entirely. The trace's
    own ``params`` block is where both are observable.
    """
    trace = vclient.get(
        "/viewer/retrieve.json", params={"user_id": "u1", "q": "Chris", "kind": "entity"}
    ).json()

    assert trace["params"]["user_id"] == "u1"
    assert trace["params"]["query"] == "Chris"


@pytest.mark.integration
def test_retrieve_returns_the_whole_serialised_trace(vclient: TestClient) -> None:
    """The response is ``dataclasses.asdict(trace)`` — every field, not a summary.

    The trace exists to answer "why did this hit or miss": ``stages`` shows
    where rows were dropped, ``sql_fragments`` shows what actually ran,
    ``notes`` carries fallback reasons. A stringified or partial envelope still
    renders in the page's ``<pre>`` block, so the loss is invisible from the UI.
    """
    trace = vclient.get(
        "/viewer/retrieve.json", params={"user_id": "u1", "q": "x"}
    ).json()

    assert set(trace) >= {
        "kind", "params", "normalized_params", "sql_fragments", "stages", "notes", "hits",
    }
    assert isinstance(trace["stages"], list)
    assert isinstance(trace["params"], dict)


@pytest.mark.integration
def test_retrieve_q_defaults_to_the_empty_string(vclient: TestClient) -> None:
    """An omitted ``q`` is an empty query, not a wildcard.

    The page sends ``q`` on every request, so the default is only reachable by
    a direct caller — and giving it a non-empty default would silently change
    what an unqualified explain means.
    """
    trace = vclient.get(
        "/viewer/retrieve.json", params={"user_id": "u1", "kind": "entity"}
    ).json()

    assert trace["params"].get("query") == ""


@pytest.mark.unit
def test_the_retrieve_kind_vocabulary_is_the_full_declared_set() -> None:
    """The union as it is declared today — now matched by what the route serves.

    Asserted against the declared ``Literal`` rather than by request.
    ``timeline`` is in the union and in the page's ``<select>``
    (``viewer.py:102``); a request for it used to always 500 because the
    route called ``explain_retrieve`` without ``since``/``until``, which that
    function requires for this kind (``parallax/retrieve.py:1206-1210``), and
    the page's own form had no way to supply them. Both halves of that defect
    are fixed now: ``viewer_retrieve`` forwards ``since``/``until`` (missing
    either is a 422 naming them) and the page's retrieve tab has matching
    ``since``/``until`` inputs — see
    ``test_kind_timeline_is_offered_by_the_page_and_can_be_served`` below,
    which replaced the strict-xfail that used to track this as a deferred
    finding.

    This assertion pins the declared set unchanged: the defect was resolved
    by forwarding the window rather than by dropping ``timeline`` from the
    ``Literal`` and the ``<select>``, so the union is exactly what it was
    before. What must not happen is the union quietly losing a value the page
    still offers.
    """
    import typing

    from parallax.server import viewer as viewer_mod

    hints = typing.get_type_hints(viewer_mod.viewer_retrieve)

    assert set(typing.get_args(hints["kind"])) == {
        "by_entity", "recent", "file", "decision", "bug", "entity", "timeline",
    }


@pytest.mark.integration
def test_kind_timeline_is_offered_by_the_page_and_can_be_served(
    vclient: TestClient
) -> None:
    """The seventh kind: no longer a tracked defect, now a genuine round trip.

    This replaces the prior strict-xfail (which pinned a 500 as the expected,
    unhealthy state). Two things had to be true before the dropdown's
    ``timeline`` option stopped lying: the route must forward ``since``/
    ``until`` to ``explain_retrieve``, AND the page's own retrieve form must
    have controls to supply them — a route that accepts the params is not
    enough if nothing on the page can ever populate them. Both are checked
    here: the shipped HTML exposes ``since``/``until`` inputs, and a request
    built the way the page's own ``loadRetrieve()`` builds it (user_id, kind,
    q, since, until — see ``viewer.py``) answers 200 with a real timeline
    trace, not just a 200 for some other kind.
    """
    html = vclient.get("/viewer/").text
    assert 'id="rt-since"' in html, "retrieve tab must offer a since input"
    assert 'id="rt-until"' in html, "retrieve tab must offer an until input"

    resp = vclient.get(
        "/viewer/retrieve.json",
        params={
            "user_id": "u1",
            "kind": "timeline",
            "q": "x",
            "since": "2026-04-20T00:00:00Z",
            "until": "2026-04-22T00:00:00Z",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["kind"] == "timeline"


@pytest.mark.integration
def test_every_exercisable_kind_answers_and_unknown_kinds_are_rejected(
    vclient: TestClient
) -> None:
    """The six kinds the route can actually serve, plus the closed-set check.

    ``timeline`` is deliberately absent — it requires ``since``/``until``,
    which this loop's bare params don't supply, so it is covered separately by
    ``test_kind_timeline_is_offered_by_the_page_and_can_be_served`` above. An
    unrecognised kind must be a 422 at the boundary rather than reaching the
    retrieval layer.
    """
    for kind in ("by_entity", "recent", "file", "decision", "bug", "entity"):
        resp = vclient.get(
            "/viewer/retrieve.json", params={"user_id": "u1", "kind": kind, "q": "x"}
        )
        assert resp.status_code == 200, f"kind={kind!r} -> {resp.status_code}"

    assert vclient.get(
        "/viewer/retrieve.json", params={"user_id": "u1", "kind": "semantic", "q": "x"}
    ).status_code == 422


@pytest.mark.integration
def test_retrieve_user_id_is_capped_at_one_hundred_and_twenty_eight(
    vclient: TestClient
) -> None:
    """Same 128-character cap as the other two routes."""
    assert vclient.get(
        "/viewer/retrieve.json", params={"user_id": "x" * 128, "q": "y"}
    ).status_code == 200
    assert vclient.get(
        "/viewer/retrieve.json", params={"user_id": "x" * 129, "q": "y"}
    ).status_code == 422
