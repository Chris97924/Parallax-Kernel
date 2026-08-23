"""Mutation-hardening for ``parallax.server.routes.query`` (land/20260824 wave 5, S1).

Additive companion to ``tests/server/test_e2e.py``,
``tests/server/test_deprecated_kind.py``,
``tests/server/test_query_traffic_source_dual_read.py``,
``tests/server/test_multi_user_auth.py``, ``tests/server/test_router_flag_wiring.py``
and ``tests/test_retrieve_api.py``. Forty-six semantic mutants were applied to a
pristine tree one at a time and run against that whole set.

Tally — applied 46 / killed by the pre-existing suite 18 / killed by the tests
below 26 / equivalent (excluded with proof, see below) 2 / unaddressed 0.

What the existing suites could not see
--------------------------------------
They are strong on *routing* — does ``kind=bug`` 410 under router-on, does the
traffic-source middleware reach the dual-read call, does an authenticated
principal beat a query-string ``user_id`` — and that is why the deprecation
gate, the 410 headers, the adoption counter and the principal binding all died
on contact. What none of them assert is the **shape of a hit** and the
**numbers in the query-parameter contract**:

* **Progressive disclosure is never checked per level on the router path.**
  Every router-path assertion is made at the default L1, so the L2 ``evidence``
  gate and the L3 ``full`` / ``upstream`` gates can each be moved a tier in
  either direction without a single test noticing — including the direction
  that puts an L3 payload on an L2 response.
* **The DTO's defaults and fallbacks are never given a degenerate hit.** Every
  fixture hit carries a complete ``{id, text, score, kind}``, so the
  ``"unknown"`` kind default, the ``""`` id default, the ``or 0.0`` null-score
  fallback and the ``text`` key choice are all exercised only where they are
  no-ops.
* **The four query-parameter numbers are never named.** ``level`` default 1 and
  ceiling 3, ``limit`` default 10 and ceiling 200, ``max_hits`` default 8 and
  ceiling 32: the suite always passes these explicitly or ignores them, so each
  can be changed freely.
* **The legacy ``_dispatch`` table is only ever walked, never cross-checked.**
  A test that asserts ``kind=decision`` returns rows passes just as well when
  ``decision`` is wired to ``by_bug_fix``.

Two equivalent mutants, excluded rather than killed
---------------------------------------------------
``_hit_to_dto`` guards its own projection twice. It calls
``RetrievalHit.project(level)``, which already omits ``evidence`` at L1 and
``full`` at L1/L2, and *then* re-gates the same two fields with
``if level >= 2`` / ``if level >= 3``. Because ``dict.get`` returns ``None`` for
the omitted key and ``_normalize_full(None)`` returns ``None``, relaxing either
guard is unobservable at every legal level:

* ``evidence ... if level >= 2`` -> ``>= 1``: at L1 the mutant evaluates
  ``proj.get("evidence")`` on a projection that has no ``"evidence"`` key, i.e.
  ``None`` — the same value the ``else`` branch produced. L2/L3 satisfy both
  predicates.
* ``full ... if level >= 3`` -> ``>= 2``: at L2 the mutant evaluates
  ``_normalize_full(proj.get("full"))`` on a projection that has no ``"full"``
  key, i.e. ``_normalize_full(None)`` -> ``None``. L1 fails both predicates and
  L3 satisfies both.

``level`` cannot leave ``{1, 2, 3}``: the route pins it with ``Query(ge=1,
le=3)``, ``project`` raises ``ValueError`` outside that set, and line 299 is the
function's only call site. So the two mutants are behaviourally identical to
the original on the whole reachable input domain.
``test_hit_to_dto_level_gating_is_redundant_with_project`` pins the projection
gating that *makes* them equivalent, so the equivalence argument itself goes
red if ``project`` ever stops omitting those keys.

Expected values are literals throughout. Deriving the ceiling from the route's
own ``Query(...)`` metadata, or the default from the signature, is exactly what
leaves a constant untested.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from parallax.retrieve import RetrievalHit
from parallax.router.crosswalk_seed import UnroutableQueryError
from parallax.server.routes import query as query_mod


def _hit(**over: Any) -> RetrievalHit:
    """A complete RetrievalHit; callers override just the field under test."""
    base: dict[str, Any] = {
        "entity_kind": "memory",
        "entity_id": "m1",
        "title": "a title",
        "score": 0.5,
        "evidence": "because reasons",
        "full": {"body": "the whole row"},
        "explain": {"reason": "test", "score_components": {"x": 1.0}},
    }
    base.update(over)
    return RetrievalHit(**base)


# ---------------------------------------------------------------------------
# _normalize_full — the L3 serialisation guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normalize_full_passes_none_through_instead_of_stringifying_it() -> None:
    """``None`` must stay ``None``, never become the four-character string.

    Dropping the ``full is None`` half of the guard sends ``None`` to
    ``str(full)``, so the L3 ``full`` field ships the literal ``"None"``. That
    is a value a JSON consumer reads as a present-but-empty row rather than an
    absent one, and Pydantic accepts it happily because the field is
    ``dict | str | None``. No existing test calls this helper with ``None``.
    """
    assert query_mod._normalize_full(None) is None


@pytest.mark.unit
def test_normalize_full_keeps_dicts_and_strings_but_coerces_anything_else() -> None:
    """The passthrough set is exactly ``(dict, str)``; everything else is str()-ed."""
    assert query_mod._normalize_full({"a": 1}) == {"a": 1}
    assert query_mod._normalize_full("raw") == "raw"
    assert query_mod._normalize_full(b"bytes") == "b'bytes'"
    assert query_mod._normalize_full(7) == "7"


@pytest.mark.unit
def test_hit_to_dto_level_gating_is_redundant_with_project() -> None:
    """``project`` already omits the fields ``_hit_to_dto`` re-gates.

    This is the load-bearing premise of the two equivalence exclusions in the
    module docstring: if ``RetrievalHit.project`` ever starts returning
    ``evidence`` at L1 or ``full`` at L2, the re-gate in ``_hit_to_dto`` stops
    being redundant and those two mutants become killable — so this assertion
    is what keeps the exclusion honest rather than permanent.
    """
    hit = _hit()
    assert "evidence" not in hit.project(1)
    assert "full" not in hit.project(1)
    assert "full" not in hit.project(2)
    assert hit.project(2)["evidence"] == "because reasons"
    assert hit.project(3)["full"] == {"body": "the whole row"}

    assert query_mod._hit_to_dto(hit, level=1).evidence is None
    assert query_mod._hit_to_dto(hit, level=1).full is None
    assert query_mod._hit_to_dto(hit, level=2).evidence == "because reasons"
    assert query_mod._hit_to_dto(hit, level=2).full is None
    assert query_mod._hit_to_dto(hit, level=3).full == {"body": "the whole row"}


# ---------------------------------------------------------------------------
# _router_hit_to_dto — the shape of a router-path hit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_router_hit_defaults_name_the_missing_field_rather_than_blanking_it() -> None:
    """A hit with nothing in it must still say *what* it could not identify.

    ``kind`` falls back to the literal ``"unknown"`` and ``id`` to the empty
    string — deliberately different sentinels, because a blank ``entity_kind``
    reads on the wire as a valid-but-unnamed kind while ``"unknown"`` is
    greppable, and a non-empty ``entity_id`` sentinel would collide with a real
    id. Both are pinned as literals: every fixture hit in the existing suites
    carries a complete dict, so neither default is ever reached there.
    """
    dto = query_mod._router_hit_to_dto({}, level=1, query_type="RECENT_CONTEXT")

    assert dto.entity_kind == "unknown"
    assert dto.entity_id == ""
    assert dto.title == ""
    assert dto.score == 0.0


@pytest.mark.unit
def test_router_hit_title_comes_from_the_text_key() -> None:
    """The router's evidence dicts carry ``text``, not ``title``.

    ``RetrievalEvidence.hits`` is documented as ``{id, text, created_at,
    source_id, kind}``. Reading ``title`` instead yields an empty title for
    every real hit, which the existing L1 assertions do not notice because they
    check that hits are *present*, not what they say.
    """
    dto = query_mod._router_hit_to_dto(
        {"text": "the real text", "title": "not this one"},
        level=1,
        query_type="RECENT_CONTEXT",
    )
    assert dto.title == "the real text"


@pytest.mark.unit
def test_router_hit_null_score_falls_back_to_zero_rather_than_raising() -> None:
    """An explicit ``None`` score is data, not a crash.

    ``hit.get("score", 0.0)`` only defends against a *missing* key; a present
    ``None`` (which an upstream adapter emits for an unscored hit) goes
    straight to ``float(None)`` and raises ``TypeError`` out of the request
    unless the ``or 0.0`` fallback catches it.
    """
    dto = query_mod._router_hit_to_dto(
        {"id": "x", "score": None}, level=1, query_type="RECENT_CONTEXT"
    )
    assert dto.score == 0.0


@pytest.mark.unit
def test_router_hit_evidence_appears_at_l2_and_is_withheld_at_l1() -> None:
    """L2 is the tier that buys ``evidence`` — not L1, and not L3.

    Unlike the legacy path, nothing upstream pre-filters the router's hit dict,
    so this predicate is the only thing standing between an L1 caller and the
    evidence string. Raising it to ``>= 3`` silently empties L2 responses;
    lowering it to ``>= 1`` leaks. Both tiers are asserted so neither direction
    survives.
    """
    hit = {"id": "x", "evidence": "why this hit"}

    assert query_mod._router_hit_to_dto(hit, level=1, query_type="Q").evidence is None
    assert (
        query_mod._router_hit_to_dto(hit, level=2, query_type="Q").evidence == "why this hit"
    )
    assert (
        query_mod._router_hit_to_dto(hit, level=3, query_type="Q").evidence == "why this hit"
    )


@pytest.mark.unit
def test_router_hit_full_row_is_withheld_until_l3() -> None:
    """``full`` is the whole underlying row — L2 must not carry it.

    Relaxing the gate to ``>= 2`` puts the complete row on every L2 response,
    which is the disclosure tier the API exists to keep separate.
    """
    hit = {"id": "x", "full": {"body": "whole row"}}

    assert query_mod._router_hit_to_dto(hit, level=1, query_type="Q").full is None
    assert query_mod._router_hit_to_dto(hit, level=2, query_type="Q").full is None
    assert query_mod._router_hit_to_dto(hit, level=3, query_type="Q").full == {
        "body": "whole row"
    }


@pytest.mark.unit
def test_upstream_explain_is_only_merged_when_it_is_actually_a_dict() -> None:
    """A non-dict ``explain`` must not be spliced into the explain envelope.

    ``explain`` is a typed ``dict[str, Any] | None`` on the DTO; assigning a
    bare string under ``"upstream"`` still validates, so the corruption ships.
    The existing suites never construct a hit whose ``explain`` is anything but
    a dict, so the ``isinstance`` half of the guard is dead weight to them.
    """
    dto = query_mod._router_hit_to_dto(
        {"id": "x", "explain": "not-a-dict"}, level=3, query_type="Q"
    )
    assert dto.explain is not None
    assert "upstream" not in dto.explain


@pytest.mark.unit
def test_upstream_explain_is_an_l3_only_disclosure() -> None:
    """Upstream provenance rides with ``full``, at L3 — never at L2."""
    hit = {"id": "x", "explain": {"stage": "aphelion"}}

    l2 = query_mod._router_hit_to_dto(hit, level=2, query_type="Q")
    l3 = query_mod._router_hit_to_dto(hit, level=3, query_type="Q")

    assert l2.explain is not None and "upstream" not in l2.explain
    assert l3.explain is not None and l3.explain["upstream"] == {"stage": "aphelion"}


@pytest.mark.unit
def test_router_explain_envelope_is_the_documented_shape() -> None:
    """``reason`` and the router's score weight are contract, not decoration.

    ``score_components`` is the auditable attribution map
    (``RetrievalHit.explain`` promises it), and a router dispatch attributes
    the whole score to the router: the literal is ``1.0``. A ``0.0`` weight
    reads downstream as "the router contributed nothing", which is the opposite
    of what happened, and the existing suites only assert that ``explain`` is
    present.
    """
    dto = query_mod._router_hit_to_dto({"id": "x"}, level=1, query_type="RECENT_CONTEXT")

    assert dto.explain == {
        "reason": "memory_router_dispatch",
        "score_components": {"router": 1.0},
        "query_type": "RECENT_CONTEXT",
    }


# ---------------------------------------------------------------------------
# _dispatch — the legacy (router-off) kind table
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_each_legacy_kind_calls_its_own_retrieve_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kind -> function table is asserted entry by entry.

    ``decision`` and ``bug`` are the pair that makes this necessary: they
    return the same row *shape*, so a test that only checks "rows come back"
    stays green when ``decision`` is wired to ``by_bug_fix``. The table is
    pinned by name rather than by result.
    """
    called: list[str] = []

    for name in ("recent_context", "by_file", "by_decision", "by_bug_fix", "by_entity"):
        monkeypatch.setattr(
            query_mod.R,
            name,
            lambda *_a, _n=name, **_k: (called.append(_n), [])[1],
        )

    for kind, expected in (
        ("recent", "recent_context"),
        ("file", "by_file"),
        ("decision", "by_decision"),
        ("bug", "by_bug_fix"),
        ("entity", "by_entity"),
    ):
        called.clear()
        query_mod._dispatch(
            None, kind=kind, user_id="u1", q="q", limit=5, since=None, until=None
        )
        assert called == [expected], f"kind={kind!r} dispatched to {called!r}"


@pytest.mark.unit
def test_file_and_entity_kinds_forward_the_query_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``q`` is the path (file) / subject (entity) — dropping it queries nothing.

    Both kinds still return a well-formed empty result when ``q`` is blanked,
    so the failure is silent: the endpoint answers 200 with zero hits and looks
    like an honest miss.
    """
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        query_mod.R, "by_file", lambda *_a, **kw: (seen.update(kw), [])[1]
    )
    monkeypatch.setattr(
        query_mod.R, "by_entity", lambda *_a, **kw: (seen.update(kw), [])[1]
    )

    query_mod._dispatch(
        None, kind="file", user_id="u1", q="notes/a.md", limit=5, since=None, until=None
    )
    assert seen["path"] == "notes/a.md"

    seen.clear()
    query_mod._dispatch(
        None, kind="entity", user_id="u1", q="Chris", limit=5, since=None, until=None
    )
    assert seen["subject"] == "Chris"


@pytest.mark.unit
def test_timeline_requires_both_bounds_not_merely_one() -> None:
    """Either bound missing is a 400 with the documented message.

    Relaxing ``or`` to ``and`` lets a half-specified range through to
    ``by_timeline`` with a ``None`` bound. The detail string is asserted
    verbatim because the mutant also produces a 4xx on some inputs — only the
    message distinguishes "you forgot a bound" from whatever the retrieval
    layer says about a ``None`` it should never have received.
    """
    expected = "timeline kind requires 'since' and 'until' ISO-8601 params"

    for since, until in (
        (None, None),
        ("2026-01-01T00:00:00Z", None),
        (None, "2026-01-02T00:00:00Z"),
    ):
        with pytest.raises(HTTPException) as exc:
            query_mod._dispatch(
                None,
                kind="timeline",
                user_id="u1",
                q="",
                limit=5,
                since=since,
                until=until,
            )
        assert exc.value.status_code == 400
        assert exc.value.detail == expected


@pytest.mark.unit
def test_timeline_bounds_are_not_swapped_on_the_way_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``since`` is the lower bound and ``until`` the upper — in that order.

    Swapping them yields an empty window rather than an error, so the endpoint
    answers 200 with zero hits: the failure mode is a silently empty timeline,
    not a visible one.
    """
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        query_mod.R, "by_timeline", lambda *_a, **kw: (seen.update(kw), [])[1]
    )

    query_mod._dispatch(
        None,
        kind="timeline",
        user_id="u1",
        q="",
        limit=5,
        since="2026-01-01T00:00:00Z",
        until="2026-01-02T00:00:00Z",
    )

    assert seen["since"] == "2026-01-01T00:00:00Z"
    assert seen["until"] == "2026-01-02T00:00:00Z"


# ---------------------------------------------------------------------------
# _dispatch_with_router — dual-read wiring and the traffic-source default
# ---------------------------------------------------------------------------


class _FakeEvidence:
    hits: tuple[dict[str, Any], ...] = ()


class _FakeResult:
    primary = _FakeEvidence()
    secondary = _FakeEvidence()


@pytest.fixture()
def router_probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the dual-read stack and record what ``_dispatch_with_router`` builds."""
    probe: dict[str, Any] = {"primary": None, "query_kwargs": {}, "observe_kwargs": {}}

    class _FakePrimaryFactory:
        def __init__(self, db_factory: Any) -> None:
            probe["primary"] = ("factory", db_factory)

    class _FakeRealRouter:
        def __init__(self, conn: Any) -> None:
            probe["primary"] = ("real", conn)

    class _FakeDualRead:
        def __init__(self, *, primary: Any, secondary: Any) -> None:
            self._primary = primary

        def query(self, _request: Any, **kwargs: Any) -> _FakeResult:
            probe["query_kwargs"] = kwargs
            return _FakeResult()

    monkeypatch.setattr(query_mod, "_FactoryRealMemoryRouter", _FakePrimaryFactory)
    monkeypatch.setattr(query_mod, "RealMemoryRouter", _FakeRealRouter)
    monkeypatch.setattr(query_mod, "DualReadRouter", _FakeDualRead)
    monkeypatch.setattr(query_mod, "AphelionReadAdapter", lambda **_k: object())
    monkeypatch.setattr(
        query_mod,
        "canary_shadow",
        SimpleNamespace(observe=lambda _r, **kw: probe["observe_kwargs"].update(kw)),
    )
    return probe


def _run_router_dispatch(**over: Any) -> list[Any]:
    kwargs: dict[str, Any] = {
        "db_factory": lambda: None,
        "audit_db_path": "audit.db",
        "kind": "recent",
        "user_id": "u1",
        "q": "",
        "level": 1,
        "limit": 10,
        "since": None,
        "until": None,
        "dual_read_override": None,
        "traffic_source": None,
    }
    kwargs.update(over)
    return query_mod._dispatch_with_router(None, **kwargs)


@pytest.mark.unit
def test_an_explicit_false_override_wins_over_the_ambient_dual_read_flag(
    router_probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dual_read_override=False`` means OFF, even when the env flag says on.

    The distinction is ``is not None`` versus truthiness: under ``or``, an
    explicit ``False`` — the per-request kill switch a caller uses to opt one
    query out of dual read — is indistinguishable from "unset" and falls
    through to the ambient flag. The observable difference is which primary
    gets built: the thread-safe factory (dual-read on, because the secondary
    runs in a worker thread) or the request-bound connection.
    """
    monkeypatch.setattr(query_mod, "is_dual_read_enabled", lambda: True)

    _run_router_dispatch(dual_read_override=False)
    assert router_probe["primary"][0] == "real"

    _run_router_dispatch(dual_read_override=None)
    assert router_probe["primary"][0] == "factory"

    _run_router_dispatch(dual_read_override=True)
    assert router_probe["primary"][0] == "factory"


@pytest.mark.unit
def test_router_query_receives_natural_when_no_traffic_source_was_tagged(
    router_probe: dict[str, Any],
) -> None:
    """An untagged request is ``"natural"`` traffic, never ``None``.

    ``traffic_source`` is a decision-log dimension and a Prometheus label.
    ``None`` reaching the label renders as the string ``"None"`` in one place
    and an empty label in another, which silently splits every dual-read rate
    into two series — the failure the ``or "natural"`` default exists to stop.
    """
    _run_router_dispatch(traffic_source=None)
    assert router_probe["query_kwargs"]["traffic_source"] == "natural"

    _run_router_dispatch(traffic_source="synthetic")
    assert router_probe["query_kwargs"]["traffic_source"] == "synthetic"


@pytest.mark.unit
def test_canary_observer_receives_natural_when_no_traffic_source_was_tagged(
    router_probe: dict[str, Any],
) -> None:
    """The canary shadow gets the same default the router query got.

    Two independent call sites carry the same ``or "natural"``; the observer's
    copy is the one no existing test reaches, and a disagreement between them
    means the canary's cohort split stops matching the decision log's.
    """
    _run_router_dispatch(traffic_source=None)
    assert router_probe["observe_kwargs"]["traffic_source"] == "natural"
    assert router_probe["observe_kwargs"]["user_id"] == "u1"


@pytest.mark.unit
def test_an_unroutable_kind_is_a_client_error_not_a_server_error(
    router_probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmapped legacy kind is the caller's fault: 400, not 500.

    ``resolve`` fails closed on a key absent from the crosswalk seed. Mapping
    that to 500 makes a client mistake page the on-call and, because 5xx is
    what retry policies retry, turns a permanent failure into a retry storm.
    """
    def _boom(_legacy: str) -> Any:
        raise UnroutableQueryError("legacy key 'RetrieveKind.nope' is unmapped")

    monkeypatch.setattr(query_mod, "resolve", _boom)

    with pytest.raises(HTTPException) as exc:
        _run_router_dispatch()

    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# get_query — app.state / request.state fallbacks
# ---------------------------------------------------------------------------


@pytest.fixture()
def route_probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Force the router-on branch and capture what ``get_query`` forwards."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(query_mod, "is_router_enabled", lambda: True)
    monkeypatch.setattr(
        query_mod, "_dispatch_with_router", lambda _conn, **kw: (seen.update(kw), [])[1]
    )
    return seen


def _fake_request(*, app_state: dict[str, Any], req_state: dict[str, Any]) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(**app_state)),
        state=SimpleNamespace(**req_state),
        url=SimpleNamespace(path="/query"),
    )


@pytest.mark.unit
def test_missing_app_state_db_factory_falls_back_to_the_default_factory(
    route_probe: dict[str, Any],
) -> None:
    """The getattr default is ``default_db_factory``, not ``None``.

    ``create_app`` always sets ``app.state.db_factory``, so this fallback is
    reached only by an app assembled another way — which is exactly when a
    ``None`` would be worst: it is passed straight into
    ``_FactoryRealMemoryRouter`` and only fails later, inside a dual-read
    worker thread, as ``TypeError: 'NoneType' object is not callable``.
    """
    request = _fake_request(app_state={"audit_db_path": "audit.db"}, req_state={})

    query_mod.get_query(request=request, kind="recent", conn=None, user_id="u1")

    assert route_probe["db_factory"] is query_mod.default_db_factory


@pytest.mark.unit
def test_missing_request_state_traffic_source_becomes_natural(
    route_probe: dict[str, Any],
) -> None:
    """A request that never passed the middleware is still ``"natural"``.

    ``request.state.traffic_source`` is set by
    ``parallax.server.middleware.traffic_source``; anything constructed without
    it (an internal call, a test client with the middleware stripped) hits this
    default. ``None`` here reaches the same label the router-query default
    protects.
    """
    request = _fake_request(app_state={"audit_db_path": "audit.db"}, req_state={})

    query_mod.get_query(request=request, kind="recent", conn=None, user_id="u1")

    assert route_probe["traffic_source"] == "natural"


# ---------------------------------------------------------------------------
# The query-parameter contract (defaults and ceilings), over HTTP
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_default_disclosure_level_is_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1 is the default tier — the whole point of progressive disclosure.

    Defaulting to L3 hands every caller the full row and the upstream explain
    without asking, which is a disclosure change no test would notice: the
    response is still well-formed and still 200.
    """
    monkeypatch.setattr(query_mod, "is_router_enabled", lambda: False)

    resp = client.get("/query", params={"kind": "recent", "user_id": "u1"})

    assert resp.status_code == 200
    assert resp.json()["level"] == 1


@pytest.mark.integration
def test_level_above_three_is_rejected(client: TestClient) -> None:
    """Three tiers exist; ``level=4`` is a validation error, not a fourth tier."""
    resp = client.get("/query", params={"kind": "recent", "user_id": "u1", "level": 4})
    assert resp.status_code == 422


@pytest.mark.integration
def test_default_limit_is_ten(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ten rows unless asked otherwise.

    Nothing in the response echoes ``limit``, so the default is invisible from
    the wire — it is asserted where it lands, on the retrieval call. A larger
    default is a quiet cost and latency change on every untuned caller.
    """
    monkeypatch.setattr(query_mod, "is_router_enabled", lambda: False)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        query_mod, "_dispatch", lambda _conn, **kw: (seen.update(kw), [])[1]
    )

    resp = client.get("/query", params={"kind": "recent", "user_id": "u1"})

    assert resp.status_code == 200
    assert seen["limit"] == 10


@pytest.mark.integration
def test_limit_above_two_hundred_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 is the ceiling: 200 passes, 201 is a validation error."""
    monkeypatch.setattr(query_mod, "is_router_enabled", lambda: False)

    ok = client.get("/query", params={"kind": "recent", "user_id": "u1", "limit": 200})
    too_big = client.get(
        "/query", params={"kind": "recent", "user_id": "u1", "limit": 201}
    )

    assert ok.status_code == 200
    assert too_big.status_code == 422


@pytest.mark.integration
def test_reminder_default_max_hits_is_eight(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight hits is the SessionStart budget.

    ``/query/reminder`` renders straight into a ``<system-reminder>`` block
    that is prepended to a model's context, so ``max_hits`` is a context-budget
    knob, not a paging knob. Doubling it doubles the injected prompt on every
    session start and nothing in the response says so.
    """
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        query_mod,
        "build_session_reminder",
        lambda _conn, **kw: (seen.update(kw), "reminder text")[1],
    )

    resp = client.get("/query/reminder", params={"user_id": "u1"})

    assert resp.status_code == 200
    assert seen["max_hits"] == 8


@pytest.mark.integration
def test_reminder_max_hits_above_thirty_two_is_rejected(client: TestClient) -> None:
    """32 is the ceiling: 32 passes, 33 is a validation error."""
    ok = client.get("/query/reminder", params={"user_id": "u1", "max_hits": 32})
    too_big = client.get("/query/reminder", params={"user_id": "u1", "max_hits": 33})

    assert ok.status_code == 200
    assert too_big.status_code == 422


@pytest.mark.integration
def test_reminder_forwards_the_session_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``session_id`` scopes the reminder to one session.

    Dropping it makes every session render the same cross-session reminder,
    which still returns a plausible non-empty block — so the endpoint keeps
    looking healthy while the scoping it exists to provide is gone.
    """
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        query_mod,
        "build_session_reminder",
        lambda _conn, **kw: (seen.update(kw), "reminder text")[1],
    )

    resp = client.get(
        "/query/reminder", params={"user_id": "u1", "session_id": "sess-42"}
    )

    assert resp.status_code == 200
    assert seen["session_id"] == "sess-42"
