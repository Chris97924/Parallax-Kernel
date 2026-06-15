"""M3-T1.4 — Tests for DualReadSnapshotMiddleware (US-011).

Covers:
1. dual_read=False when env unset.
2. dual_read=True when DUAL_READ=true and breaker not tripped.
3. dual_read=False when breaker tripped (even if env=true).
4. Snapshot is immutable during request (env change mid-request ignored).
5. inflight gauge >= 1 inside handler.
6. inflight gauge returns to 0 after normal response.
7. inflight gauge returns to 0 after HTTPException (try/finally bug-guard).
8. inflight gauge returns to 0 after 422 validation error.
9. inflight gauge returns to 0 after bare RuntimeError.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from parallax.migrations import migrate_to_latest
from parallax.router.circuit_breaker import get_breaker_state
from parallax.router.inflight import get_inflight_count, inflight_gauge
from parallax.server.app import create_app
from parallax.sqlite_store import connect

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "mw_test.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


def _make_app(db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


@pytest.fixture(autouse=True)
def _clean_gauge():
    """Ensure gauge is 0 before/after each test."""
    _reset_gauge()
    yield
    _reset_gauge()


@pytest.fixture(autouse=True)
def _clean_breaker():
    get_breaker_state().reset()
    yield
    get_breaker_state().reset()


def _reset_gauge() -> None:
    current = get_inflight_count()
    if current > 0:
        for _ in range(current):
            inflight_gauge.dec()
    elif current < 0:
        for _ in range(-current):
            inflight_gauge.inc()


# ---------------------------------------------------------------------------
# Helper: build minimal test app with custom routes
# ---------------------------------------------------------------------------


def _build_test_app(
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_routes: list[tuple[str, callable]] | None = None,
) -> FastAPI:
    """Build create_app() and attach extra test routes before returning."""
    app = _make_app(db_path, monkeypatch)

    if extra_routes:
        from fastapi.routing import APIRouter

        r = APIRouter()
        for path, handler in extra_routes:
            r.add_api_route(path, handler, methods=["GET"])
        app.include_router(r)

    return app


# ---------------------------------------------------------------------------
# Test 1: dual_read=False when DUAL_READ unset
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_middleware_sets_request_state_dual_read_to_false_when_env_unset(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DUAL_READ", raising=False)
    captured: list[bool] = []

    def _handler(request: Request):
        captured.append(request.state.dual_read)
        return {"ok": True}

    app = _build_test_app(db_path, monkeypatch, extra_routes=[("/test_dr", _handler)])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/test_dr")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"

    assert captured == [False]


# ---------------------------------------------------------------------------
# Test 2: dual_read=True when env=true and breaker not tripped
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_middleware_sets_to_true_when_env_true_and_breaker_not_tripped(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    captured: list[bool] = []

    def _handler(request: Request):
        captured.append(request.state.dual_read)
        return {"ok": True}

    app = _build_test_app(db_path, monkeypatch, extra_routes=[("/test_dr2", _handler)])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/test_dr2")
        assert resp.status_code == 200

    assert captured == [True]


# ---------------------------------------------------------------------------
# Test 3: dual_read=False when breaker tripped (even env=true)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_middleware_sets_to_false_when_breaker_tripped(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    # Manually trip the breaker
    get_breaker_state().tripped = True
    captured: list[bool] = []

    def _handler(request: Request):
        captured.append(request.state.dual_read)
        return {"ok": True}

    app = _build_test_app(db_path, monkeypatch, extra_routes=[("/test_dr3", _handler)])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/test_dr3")
        assert resp.status_code == 200

    assert captured == [False]


# ---------------------------------------------------------------------------
# Test 4: Snapshot immutable during request (Q8 contract)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_middleware_snapshot_immutable_during_request(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    captured_before: list[bool] = []
    captured_after: list[bool] = []

    def _handler(request: Request):
        captured_before.append(request.state.dual_read)
        # Flip env mid-request — snapshot must NOT change
        os.environ["DUAL_READ"] = "false"
        captured_after.append(request.state.dual_read)
        return {"ok": True}

    app = _build_test_app(db_path, monkeypatch, extra_routes=[("/test_dr4", _handler)])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/test_dr4")
        assert resp.status_code == 200

    # Both snapshots should be True (value at request entry, before the flip)
    assert captured_before == [True]
    assert captured_after == [True], (
        "request.state.dual_read should be the entry-time snapshot, "
        "not re-read from env mid-request"
    )


# ---------------------------------------------------------------------------
# Test 5: inflight gauge >=1 inside handler
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_middleware_inflight_gauge_increments_during_handler(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DUAL_READ", raising=False)
    captured: list[int] = []

    def _handler(request: Request):
        captured.append(get_inflight_count())
        return {"ok": True}

    app = _build_test_app(db_path, monkeypatch, extra_routes=[("/test_dr5", _handler)])
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/test_dr5")

    assert captured, "Handler was never called"
    assert captured[0] >= 1, f"Expected inflight >= 1 inside handler, got {captured[0]}"


# ---------------------------------------------------------------------------
# Test 6: inflight gauge = 0 after normal response
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_middleware_inflight_gauge_decrements_after_normal_response(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DUAL_READ", raising=False)

    def _handler(request: Request):
        return {"ok": True}

    app = _build_test_app(db_path, monkeypatch, extra_routes=[("/test_dr6", _handler)])
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/test_dr6")

    assert get_inflight_count() == 0


# ---------------------------------------------------------------------------
# Test 7: inflight gauge = 0 after HTTPException (try/finally bug-guard)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_middleware_inflight_gauge_decrements_after_handler_exception(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DUAL_READ", raising=False)

    def _handler(request: Request):
        raise HTTPException(status_code=500, detail="test error")

    app = _build_test_app(db_path, monkeypatch, extra_routes=[("/test_dr7", _handler)])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/test_dr7")
        assert resp.status_code == 500

    assert get_inflight_count() == 0, (
        "CRITICAL: try/finally bug-guard failed — gauge leaked on HTTPException"
    )


# ---------------------------------------------------------------------------
# Test 8: inflight gauge = 0 after 422 validation error
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_middleware_inflight_gauge_decrements_after_validation_error(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DUAL_READ", raising=False)

    app = _make_app(db_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as client:
        # POST /ingest/memory without required fields triggers 422
        resp = client.post("/ingest/memory", json={"user_id": "u"})
        assert resp.status_code == 422

    assert get_inflight_count() == 0, (
        "CRITICAL: try/finally bug-guard failed — gauge leaked on 422 validation error"
    )


# ---------------------------------------------------------------------------
# Test 9: inflight gauge = 0 after bare RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_middleware_inflight_gauge_decrements_after_uncaught_runtime_error(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DUAL_READ", raising=False)

    def _handler(request: Request):
        raise RuntimeError("unexpected crash")

    app = _build_test_app(db_path, monkeypatch, extra_routes=[("/test_dr9", _handler)])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/test_dr9")
        # Should get 500 from the default handler
        assert resp.status_code in (500, 503)

    assert get_inflight_count() == 0, (
        "CRITICAL: try/finally bug-guard failed — gauge leaked on RuntimeError"
    )
