"""Mutation-hardening for ``parallax.server.app`` (land/20260824 wave 5, S2).

Additive companion to ``tests/server/test_server_safety.py``,
``tests/server/test_app_t14_wiring.py``, ``tests/server/test_e2e.py``,
``tests/server/test_multi_user_auth.py``, ``tests/observability/`` and
``tests/test_public_api.py``. Thirty-nine semantic mutants were applied to a
pristine tree one at a time against that whole set.

Tally — applied 39 / killed by the pre-existing suite 26 / killed by the tests
below 12 / equivalent (excluded with proof, see below) 1 / unaddressed 0.

What the existing suites could not see
--------------------------------------
Two thirds of the mutants died instantly, because this module is mostly
*wiring* and the suite is full of requests: unhook a router and every test
against it 404s. What survives that shape is everything ``create_app`` does
which is not a route:

* **The docs gate is only ever observed in its OFF state.** ``/openapi.json``
  being absent by default is pinned, so the mutant that serves it
  unconditionally dies — but nothing asserts that ``/docs`` and ``/redoc``
  are gated by the same flag, and nothing ever turns the flag ON, so the
  accepted-value vocabulary (``"true"``) and the ``.strip()`` that tolerates a
  whitespace-padded env value are both unreachable from the suite.
* **The start-up log lines are treated as noise.** ``create_app`` emits three
  operator-facing records — the open-mode warning, its non-loopback
  counterpart, and the ``/metrics`` public-override audit — and every one of
  their guards can be inverted without a test noticing. These are the records
  a post-incident reader greps for to answer "was this listener deliberately
  open"; a silent one is indistinguishable from a safe boot.
* **``app.state`` is read but never characterised.** Tests inject a
  ``db_factory`` and then use it, so the ``or default_db_factory`` fallback and
  the defensive ``dict(...)`` copy of ``settings`` are never the code under
  test.
* **Middleware is asserted by effect, never by stack position.** Both
  middlewares are verified to have run; their relative order — which decides
  how much of the request the inflight gauge spans — is invisible.

One equivalent mutant, excluded rather than killed
---------------------------------------------------
``bind_host = os.environ.get(PARALLAX_BIND_HOST_ENV, "")`` -> default
``"127.0.0.1"``. The default is consumed only when ``PARALLAX_BIND_HOST`` is
unset, and ``parallax.server.auth._LOCALHOST_HOSTS`` contains BOTH ``""`` and
``"127.0.0.1"`` — the empty string is in the set precisely because uvicorn's
own default bind is loopback. So ``bind_host_is_safe`` returns ``True`` for
either value and control reaches the same warning branch, whose message is a
fixed string that does not interpolate ``bind_host``. The only site that does
interpolate it is the ``else`` branch, which is unreachable while the value is
safe. ``test_unset_and_explicit_loopback_bind_hosts_are_both_safe`` pins the
set membership the argument rests on, so the exclusion goes red if ``""`` ever
stops being loopback.

Expected values are literals throughout — the accepted docs vocabulary, the
sanitised error body, the middleware order.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import sqlite3
from typing import Any

import pytest
from fastapi import FastAPI

from parallax import __version__ as parallax_version
from parallax.migrations import migrate_to_latest
from parallax.server.app import create_app
from parallax.server.auth import bind_host_is_safe
from parallax.server.deps import default_db_factory
from parallax.server.middleware.dual_read_snapshot import DualReadSnapshotMiddleware
from parallax.server.middleware.traffic_source import TrafficSourceMiddleware
from parallax.sqlite_store import connect

_APP_LOGGER = "parallax.server"


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutral start-up environment: no token, no overrides, loopback bind."""
    for var in (
        "PARALLAX_TOKEN",
        "PARALLAX_DOCS_ENABLED",
        "PARALLAX_BIND_HOST",
        "PARALLAX_ALLOW_OPEN_PUBLIC",
        "PARALLAX_METRICS_PUBLIC",
        "PARALLAX_MULTI_USER",
        "PARALLAX_VIEWER_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def safety_db(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "app_harden.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


def _app(safety_db: pathlib.Path, **kwargs: Any) -> FastAPI:
    def factory() -> sqlite3.Connection:
        return connect(safety_db)

    return create_app(db_factory=factory, **kwargs)


def _startup_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _APP_LOGGER]


# ---------------------------------------------------------------------------
# The OpenAPI docs gate
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_all_three_docs_surfaces_are_off_by_default(
    clean_env: None, safety_db: pathlib.Path
) -> None:
    """``/docs``, ``/redoc`` and ``/openapi.json`` are gated by ONE flag.

    They have to move together: ``/openapi.json`` alone is the machine-readable
    enumeration of every route and schema, and ``/docs`` / ``/redoc`` are
    renderers that fetch it. Leaving either renderer mounted while the schema is
    gated is not a partial win — it is a second door onto the same enumeration
    the gate exists to close. The existing suite pins only ``/openapi.json``.
    """
    app = _app(safety_db)

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


@pytest.mark.integration
@pytest.mark.parametrize("raw", ["1", "true", "True", "yes", " 1 ", "\ttrue\n"])
def test_docs_gate_accepts_its_documented_vocabulary_padded_or_not(
    clean_env: None, safety_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Four spellings turn docs on, and surrounding whitespace does not count.

    The vocabulary is literal — ``1``, ``true``, ``True``, ``yes`` — because an
    operator setting ``PARALLAX_DOCS_ENABLED=true`` and silently getting nothing
    is the failure this list exists to prevent. The padded variants are here
    because env values routinely arrive with whitespace from ``.env`` files,
    shell heredocs and PM2 ecosystem configs, and ``.strip()`` is the only thing
    that makes those work. Nothing in the existing suite ever enables docs, so
    both the vocabulary and the trim are dead code to it.
    """
    monkeypatch.setenv("PARALLAX_DOCS_ENABLED", raw)

    app = _app(safety_db)

    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"


@pytest.mark.integration
@pytest.mark.parametrize("raw", ["0", "false", "no", "", "TRUE1", "y"])
def test_docs_gate_rejects_everything_outside_that_vocabulary(
    clean_env: None, safety_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Fail-closed: an unrecognised value leaves the docs surfaces off."""
    monkeypatch.setenv("PARALLAX_DOCS_ENABLED", raw)

    assert _app(safety_db).openapi_url is None


# ---------------------------------------------------------------------------
# app.state
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_omitting_db_factory_installs_the_default_one(clean_env: None) -> None:
    """``create_app()`` with no factory must still have one on ``app.state``.

    ``None`` there is not an inert default: ``routes/query.py`` reads
    ``app.state.db_factory`` and hands it to the dual-read primary, so the
    failure would surface as ``'NoneType' object is not callable`` inside a
    worker thread rather than at boot. Every test in the suite injects a
    factory, so this branch is only ever taken by real ``uvicorn`` boots.
    """
    app = create_app()

    assert app.state.db_factory is default_db_factory


@pytest.mark.integration
def test_settings_are_copied_so_later_caller_mutation_cannot_reach_the_app(
    clean_env: None, safety_db: pathlib.Path
) -> None:
    """``dict(settings or {})`` is a defensive copy, not a formality.

    ``settings`` is documented as the injection point for CORS origins, rate
    limits and feature flags. Aliasing the caller's dict makes those live: a
    later mutation by the caller silently reconfigures a running app, which is
    the kind of action-at-a-distance that is impossible to see in a traceback.
    """
    supplied: dict[str, Any] = {"feature_x": True}

    app = _app(safety_db, settings=supplied)
    supplied["feature_x"] = False
    supplied["injected_later"] = "should not appear"

    assert app.state.settings == {"feature_x": True}
    assert app.state.settings is not supplied


@pytest.mark.integration
def test_none_settings_becomes_an_empty_dict_not_none(
    clean_env: None, safety_db: pathlib.Path
) -> None:
    """The absent case is ``{}`` so callers can index without a guard."""
    assert _app(safety_db).state.settings == {}


@pytest.mark.integration
def test_app_advertises_the_package_version(
    clean_env: None, safety_db: pathlib.Path
) -> None:
    """The OpenAPI ``version`` field is the deployed package version.

    Equality with ``parallax.__version__`` is the contract itself — a literal
    would have to be edited on every release — so the literal assertion here is
    the *shape*: a real dotted release, never the ``0.0.0`` placeholder a
    hardcoded value degrades to. That pair is what makes the field usable for
    answering "which build is this listener running" from a scrape.
    """
    app = _app(safety_db)

    assert app.version == parallax_version
    assert app.version != "0.0.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", app.version), app.version


# ---------------------------------------------------------------------------
# Start-up operator log lines
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_open_mode_on_loopback_warns_and_does_not_escalate_to_error(
    clean_env: None, safety_db: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No token + loopback is a WARNING about open mode — not an ERROR.

    Two guards meet here and each can be inverted independently. ``if not
    auth_configured()`` decides whether anything is said at all; a silent boot
    with no token is exactly the state an operator must not be able to reach
    without a log line. ``if bind_host_is_safe(...)`` picks WHICH line: the
    loopback warning, or the "anyone on the network can read/write your kernel"
    error. Inverting it makes a safe local dev boot cry wolf at ERROR every
    time, which is how a real one gets tuned out.
    """
    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        _app(safety_db)

    records = _startup_records(caplog)
    warnings = [r for r in records if r.levelno == logging.WARNING]
    errors = [r for r in records if r.levelno >= logging.ERROR]

    assert errors == [], [r.getMessage() for r in errors]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "OPEN MODE" in warnings[0].getMessage()
    assert "loopback" in warnings[0].getMessage()


@pytest.mark.integration
def test_a_configured_token_boots_silently(
    clean_env: None, safety_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With ``PARALLAX_TOKEN`` set there is nothing to warn about.

    This is the other half of the ``if not auth_configured()`` guard: inverted,
    a properly configured server logs "PARALLAX_TOKEN is unset" on every boot.
    A warning that fires when the thing it warns about is absent trains the
    reader to ignore it, so the silent case has to be pinned too.
    """
    monkeypatch.setenv("PARALLAX_TOKEN", "t0ken")

    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        _app(safety_db)

    assert _startup_records(caplog) == []


@pytest.mark.integration
def test_metrics_public_override_is_audited_when_it_is_active(
    clean_env: None, safety_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deliberately exposing ``/metrics`` must leave a record.

    The whole point of this line is the post-incident question "was this route
    open on purpose?". It is emitted at boot precisely because nothing else in
    the logs distinguishes an intentional override from a misconfiguration.
    """
    monkeypatch.setenv("PARALLAX_TOKEN", "t0ken")
    monkeypatch.setenv("PARALLAX_METRICS_PUBLIC", "1")

    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        _app(safety_db)

    messages = [r.getMessage() for r in _startup_records(caplog)]
    assert any("public_override_active" in m for m in messages), messages


@pytest.mark.integration
def test_no_metrics_override_audit_when_the_override_is_absent(
    clean_env: None, safety_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The audit line must not fire on a default, auth-gated ``/metrics``.

    Inverting the guard emits it on every ordinary boot, which turns a
    high-signal security audit record into background noise — and would have a
    reader conclude the route was open when it was not.
    """
    monkeypatch.setenv("PARALLAX_TOKEN", "t0ken")

    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        _app(safety_db)

    messages = [r.getMessage() for r in _startup_records(caplog)]
    assert not any("public_override_active" in m for m in messages), messages


@pytest.mark.unit
def test_unset_and_explicit_loopback_bind_hosts_are_both_safe() -> None:
    """The premise behind excluding the bind-host default mutant as equivalent.

    ``""`` is in ``_LOCALHOST_HOSTS`` because an unset ``PARALLAX_BIND_HOST``
    means uvicorn's own loopback default. That is what makes the getattr
    default (``""`` vs ``"127.0.0.1"``) unobservable — and it is a real
    contract in its own right, since a fail-open reading of "unset" would let a
    tokenless server boot believing it was on loopback when it was not.
    """
    assert bind_host_is_safe("") is True
    assert bind_host_is_safe("127.0.0.1") is True
    assert bind_host_is_safe("0.0.0.0") is False


# ---------------------------------------------------------------------------
# Middleware stack order
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_dual_read_snapshot_middleware_sits_outside_traffic_source(
    clean_env: None, safety_db: pathlib.Path
) -> None:
    """Install order is stack order, and this order is the drain contract.

    ``add_middleware`` inserts at the head of ``user_middleware``, so the
    LAST-installed middleware ends up outermost. ``create_app`` installs
    traffic-source first and the dual-read snapshot second, which puts the
    inflight gauge on the outside: a request counts as in-flight for its whole
    server-side lifetime rather than for all-but-the-outermost layer. The
    ``sum(parallax_inflight_requests) == 0`` deploy gate in the drain runbook
    is read against that span. Swapping the two calls is invisible to every
    behavioural test — both middlewares still run, both still set their
    ``request.state`` attribute — so stack position is the only observable.

    Asserted as relative position rather than as the exact stack, because
    relative position is the whole of the contract: adding a CORS or
    request-id middleware later disturbs neither the gauge's span nor the
    drain gate, and an exact-list assertion would redden on it.
    """
    app = _app(safety_db)

    installed = [m.cls for m in app.user_middleware]

    assert installed.index(DualReadSnapshotMiddleware) < installed.index(
        TrafficSourceMiddleware
    )


# ---------------------------------------------------------------------------
# The sqlite error handler
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sqlite_errors_are_logged_server_side_while_the_wire_stays_generic(
    clean_env: None, safety_db: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The handler has two jobs and the suite only ever checks one.

    Sanitising the wire response is pinned elsewhere; ``_log.exception`` is
    not. Dropping it produces the worst possible pairing — the client is told
    "internal database error" and the server keeps no record of what actually
    failed — so a schema drift or a locked DB becomes a 500 with no diagnosis
    anywhere. Both halves are asserted together because the sanitisation is
    only defensible if the detail survives somewhere.
    """
    app = _app(safety_db)
    handler = app.exception_handlers[sqlite3.Error]

    with caplog.at_level(logging.DEBUG, logger=_APP_LOGGER):
        try:
            raise sqlite3.OperationalError("no such table: super_secret_table")
        except sqlite3.OperationalError as exc:
            response = asyncio.run(handler(None, exc))

    body = response.body.decode()
    assert response.status_code == 500
    assert "super_secret_table" not in body
    assert "internal database error" in body
    assert "database_error" in body

    logged = [r for r in _startup_records(caplog) if r.levelno >= logging.ERROR]
    assert len(logged) == 1, [r.getMessage() for r in _startup_records(caplog)]
    assert "super_secret_table" in logged[0].getMessage()
