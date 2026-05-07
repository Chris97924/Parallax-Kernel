"""End-to-end tests for the Parallax HTTP server.

Coverage targets:

* ``/healthz`` is unauthenticated and always returns 200.
* ``POST /ingest/memory`` and ``POST /ingest/claim`` round-trip through
  the real kernel.
* ``GET /query`` dispatches to each of the six retrieval kinds and honours
  L1/L2/L3 progressive disclosure.
* ``GET /query/reminder`` matches what
  :func:`parallax.injector.build_session_reminder` would return.
* ``GET /inspect/health`` + ``GET /inspect/info`` reflect the DB.
* Bearer auth: missing / wrong / right tokens return 401 / 401 / 200.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# ----- /healthz -------------------------------------------------------------


class TestHealthz:
    def test_unauth_ok(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "parallax-kernel"
        # Auth posture and version are intentionally NOT leaked via healthz.
        assert "auth" not in body
        assert "version" not in body

    def test_healthz_always_minimal(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/healthz")
        assert resp.status_code == 200
        assert "auth" not in resp.json()


# ----- /ingest --------------------------------------------------------------


class TestIngestMemory:
    def test_201_with_id(self, client: TestClient) -> None:
        resp = client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "t1",
                "summary": "s1",
                "vault_path": "notes/t1.md",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["kind"] == "memory"
        assert body["user_id"] == "u"
        assert len(body["id"]) > 0

    def test_dedup_returns_same_id(self, client: TestClient) -> None:
        payload = {
            "user_id": "u",
            "title": "same",
            "summary": "same",
            "vault_path": "notes/same.md",
        }
        r1 = client.post("/ingest/memory", json=payload)
        r2 = client.post("/ingest/memory", json=payload)
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"]

    def test_validation_error_422(self, client: TestClient) -> None:
        # missing required vault_path
        resp = client.post(
            "/ingest/memory",
            json={"user_id": "u", "title": "x"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"] == "validation_error"

    def test_cross_user_direct_namespace_400(self, client: TestClient) -> None:
        # alice cannot write to bob's reserved direct: namespace; must surface
        # as 400 (input rejection) not 500 (uncaught ValueError).
        resp = client.post(
            "/ingest/memory",
            json={
                "user_id": "alice",
                "title": "t",
                "summary": "s",
                "vault_path": "notes/t.md",
                "source_id": "direct:bob",
            },
        )
        assert resp.status_code == 400
        assert "reserved direct: namespace" in resp.json()["detail"]


class TestIngestClaim:
    def test_201_returns_claim_id(self, client: TestClient) -> None:
        resp = client.post(
            "/ingest/claim",
            json={
                "user_id": "u",
                "subject": "P",
                "predicate": "is",
                "object": "ok",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["kind"] == "claim"

    def test_invalid_state_400(self, client: TestClient) -> None:
        resp = client.post(
            "/ingest/claim",
            json={
                "user_id": "u",
                "subject": "P",
                "predicate": "is",
                "object": "ok",
                "state": "not-a-state",
            },
        )
        assert resp.status_code == 400


# ----- /query ---------------------------------------------------------------


def _seed_claim_and_events(client: TestClient) -> str:
    """Ingest a claim so entity/bug/decision scans have something to return."""
    resp = client.post(
        "/ingest/claim",
        json={
            "user_id": "u",
            "subject": "bugfix-001",
            "predicate": "fixes",
            "object": "retrieve regression",
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


class TestQueryDispatch:
    def test_entity_returns_claim_hit_l1(self, client: TestClient) -> None:
        _seed_claim_and_events(client)
        resp = client.get(
            "/query",
            params={
                "kind": "entity",
                "user_id": "u",
                "q": "bugfix-001",
                "level": 1,
                "limit": 10,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "entity"
        assert body["level"] == 1
        assert body["count"] >= 1
        hit = body["hits"][0]
        assert hit["entity_kind"] == "claim"
        # L1: no evidence, no full
        assert hit["evidence"] is None
        assert hit["full"] is None

    def test_entity_l2_includes_evidence(self, client: TestClient) -> None:
        _seed_claim_and_events(client)
        resp = client.get(
            "/query",
            params={
                "kind": "entity",
                "user_id": "u",
                "q": "bugfix-001",
                "level": 2,
            },
        )
        hit = resp.json()["hits"][0]
        assert hit["evidence"] is not None
        assert hit["full"] is None

    def test_entity_l3_includes_full(self, client: TestClient) -> None:
        _seed_claim_and_events(client)
        resp = client.get(
            "/query",
            params={
                "kind": "entity",
                "user_id": "u",
                "q": "bugfix-001",
                "level": 3,
            },
        )
        hit = resp.json()["hits"][0]
        assert hit["full"] is not None

    def test_bug_kind_matches_fix_token(self, client: TestClient) -> None:
        _seed_claim_and_events(client)
        resp = client.get(
            "/query",
            params={"kind": "bug", "user_id": "u", "limit": 10},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

    def test_recent_empty_ok(self, client: TestClient) -> None:
        resp = client.get(
            "/query",
            params={"kind": "recent", "user_id": "nobody", "limit": 5},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_timeline_requires_since_until(self, client: TestClient) -> None:
        resp = client.get(
            "/query",
            params={"kind": "timeline", "user_id": "u"},
        )
        assert resp.status_code == 400

    def test_timeline_bad_iso(self, client: TestClient) -> None:
        resp = client.get(
            "/query",
            params={
                "kind": "timeline",
                "user_id": "u",
                "since": "not-a-date",
                "until": "2026-04-21T00:00:00Z",
            },
        )
        assert resp.status_code == 400

    def test_unknown_kind_422(self, client: TestClient) -> None:
        resp = client.get(
            "/query",
            params={"kind": "nonsense", "user_id": "u"},
        )
        assert resp.status_code == 422


class TestQueryRouterFlag:
    def test_entity_uses_memory_router_when_enabled(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMORY_ROUTER", "true")
        client.post(
            "/ingest/claim",
            json={
                "user_id": "u",
                "subject": "router-entity",
                "predicate": "is",
                "object": "active",
            },
        )
        resp = client.get(
            "/query",
            params={
                "kind": "entity",
                "user_id": "u",
                "q": "router-entity",
                "level": 2,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] >= 1
        assert body["hits"][0]["explain"]["reason"] == "memory_router_dispatch"

    def test_router_preserves_score_and_l3_full_contract(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subject = "router-contract-subject"
        client.post(
            "/ingest/claim",
            json={
                "user_id": "u",
                "subject": subject,
                "predicate": "is",
                "object": "contract-check",
            },
        )

        monkeypatch.setenv("MEMORY_ROUTER", "false")
        resp_off = client.get(
            "/query",
            params={
                "kind": "entity",
                "user_id": "u",
                "q": subject,
                "level": 3,
                "limit": 1,
            },
        )
        assert resp_off.status_code == 200, resp_off.text
        hit_off = resp_off.json()["hits"][0]
        assert hit_off["score"] > 0.0
        assert isinstance(hit_off["full"], dict)

        monkeypatch.setenv("MEMORY_ROUTER", "true")
        resp_on = client.get(
            "/query",
            params={
                "kind": "entity",
                "user_id": "u",
                "q": subject,
                "level": 3,
                "limit": 1,
            },
        )
        assert resp_on.status_code == 200, resp_on.text
        hit_on = resp_on.json()["hits"][0]
        assert hit_on["score"] > 0.0
        assert hit_on["score"] == pytest.approx(hit_off["score"])
        assert isinstance(hit_on["full"], dict)
        assert set(hit_on["full"].keys()) == set(hit_off["full"].keys())


# ----- /query/reminder ------------------------------------------------------


class TestReminder:
    def test_empty_db_renders_placeholders(self, client: TestClient) -> None:
        resp = client.get("/query/reminder", params={"user_id": "u"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["reminder"].startswith("<system-reminder>")
        assert body["reminder"].endswith("</system-reminder>")
        assert body["length"] == len(body["reminder"])
        assert "(none)" in body["reminder"]

    def test_matches_injector(
        self, client: TestClient, db_path: object, app: object
    ) -> None:
        """The HTTP reminder must equal what injector.build_session_reminder
        would produce — the whole point of the /query/reminder endpoint is
        to be a lossless HTTP shim, not a reformat."""
        from parallax.injector import build_session_reminder
        from parallax.sqlite_store import connect

        # Seed a claim so both paths render the same content.
        client.post(
            "/ingest/claim",
            json={"user_id": "u", "subject": "X", "predicate": "is", "object": "y"},
        )
        conn = connect(str(db_path))
        try:
            expected = build_session_reminder(conn, user_id="u")
        finally:
            conn.close()
        resp = client.get("/query/reminder", params={"user_id": "u"})
        assert resp.json()["reminder"] == expected


# ----- /inspect -------------------------------------------------------------


class TestInspect:
    def test_health_ok(self, client: TestClient) -> None:
        resp = client.get("/inspect/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ok", "degraded")
        assert "table_counts" in body
        assert "memories" in body["table_counts"]

    def test_info_has_version_and_counts(self, client: TestClient) -> None:
        client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "t",
                "summary": "s",
                "vault_path": "v.md",
            },
        )
        resp = client.get("/inspect/info")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"]
        assert body["memories_count"] >= 1
        assert "health" in body

    def test_db_path_is_redacted_to_basename(self, client: TestClient) -> None:
        """The wire response must never leak the absolute host filesystem path."""
        resp = client.get("/inspect/health")
        assert resp.status_code == 200
        db_path = resp.json()["db_path"]
        # basename only — no directory separator, no drive letter
        assert "/" not in db_path
        assert "\\" not in db_path
        assert ":" not in db_path
        # info endpoint redacts too
        resp2 = client.get("/inspect/info")
        assert "/" not in resp2.json()["db_path"]


class TestInspectUsesDbFactory:
    """Regression: inspect routes must flow through app.state.db_factory
    (via get_conn) rather than re-deriving the DB path from load_config().

    We pin this by swapping the app's db_factory to a completely separate
    DB after creation; the inspect endpoint must reflect the new factory,
    not the original."""

    def test_inspect_reflects_overridden_factory(
        self, app, tmp_path, monkeypatch
    ) -> None:
        import sqlite3 as _sqlite3

        from parallax.migrations import migrate_to_latest
        from parallax.sqlite_store import connect

        alt = tmp_path / "alt.db"
        boot = connect(alt)
        try:
            migrate_to_latest(boot)
        finally:
            boot.close()

        def alt_factory() -> _sqlite3.Connection:
            return connect(alt)

        app.state.db_factory = alt_factory
        with TestClient(app) as c:
            resp = c.get("/inspect/health")
            assert resp.status_code == 200
            # redacted basename should match the alt DB file
            assert resp.json()["db_path"] == "alt.db"


class TestSqliteErrorDoesNotLeak:
    """The sqlite3.Error handler must not echo str(exc) to the wire."""

    def test_detail_is_generic(self, app, monkeypatch) -> None:
        import sqlite3 as _sqlite3

        def broken_factory() -> _sqlite3.Connection:
            raise _sqlite3.OperationalError(
                "no such table: secret_internal_schema_hint"
            )

        app.state.db_factory = broken_factory
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post(
                "/ingest/memory",
                json={
                    "user_id": "u",
                    "title": "t",
                    "summary": "s",
                    "vault_path": "v.md",
                },
            )
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "database_error"
        assert body["detail"] == "internal database error"
        assert "secret_internal_schema_hint" not in resp.text


class TestVaultPathTraversalRejected:
    @pytest.mark.parametrize(
        "bad_path",
        [
            "../etc/passwd",
            "notes/../../etc/passwd",
            "/absolute/path.md",
            "C:/windows/path.md",
            "..\\..\\secret.md",
        ],
    )
    def test_reject_400ish(self, client: TestClient, bad_path: str) -> None:
        resp = client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "t",
                "summary": "s",
                "vault_path": bad_path,
            },
        )
        # Pydantic validation → 422 via our handler
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"] == "validation_error"


class TestDocsOffByDefault:
    def test_docs_disabled_by_default(self, client: TestClient) -> None:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


# ----- Auth -----------------------------------------------------------------


class TestAuth:
    def test_missing_token_rejected(self, auth_client: TestClient) -> None:
        resp = auth_client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "t",
                "summary": "s",
                "vault_path": "v.md",
            },
        )
        assert resp.status_code == 401

    def test_wrong_token_rejected(self, auth_client: TestClient) -> None:
        resp = auth_client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "t",
                "summary": "s",
                "vault_path": "v.md",
            },
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_right_token_accepted(self, auth_client: TestClient) -> None:
        resp = auth_client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "t",
                "summary": "s",
                "vault_path": "v.md",
            },
            headers={"Authorization": "Bearer t0ken"},
        )
        assert resp.status_code == 201

    def test_healthz_still_unauth(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/healthz")
        assert resp.status_code == 200


class TestInspectHealthAuth:
    """H-2: /inspect/health gates full payload behind valid bearer token."""

    def test_no_token_returns_ok_only(self, auth_client: TestClient) -> None:
        resp = auth_client.get("/inspect/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "table_counts" not in body
        assert body["status"] in ("ok", "degraded")

    def test_invalid_token_returns_ok_only(self, auth_client: TestClient) -> None:
        resp = auth_client.get(
            "/inspect/health", headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "table_counts" not in body

    def test_valid_token_returns_full_payload(self, auth_client: TestClient) -> None:
        resp = auth_client.get(
            "/inspect/health", headers={"Authorization": "Bearer t0ken"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "table_counts" in body
        assert "journal_mode" in body

    def test_open_mode_returns_full_payload(self, client: TestClient) -> None:
        resp = client.get("/inspect/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "table_counts" in body


# ----- Full-loop: hook-shaped consumer → server → kernel --------------------


class TestFullLoop:
    def test_hook_flow(self, client: TestClient) -> None:
        """Simulate the SessionStart hook's flow: ingest events, fetch
        reminder, assert it carries the expected markers. This is the
        demo path the hackathon walkthrough sells."""
        # 1. Seed a memory + a claim.
        client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "Parallax v0.6",
                "summary": "Hub-and-spoke architecture",
                "vault_path": "notes/v06.md",
            },
        )
        client.post(
            "/ingest/claim",
            json={
                "user_id": "u",
                "subject": "Parallax",
                "predicate": "is",
                "object": "content-addressed",
            },
        )

        # 2. Query entity — claim should surface.
        r = client.get(
            "/query",
            params={"kind": "entity", "user_id": "u", "q": "Parallax", "level": 2},
        )
        assert r.status_code == 200
        assert r.json()["count"] >= 1

        # 3. Reminder must be a non-empty system-reminder block.
        r = client.get("/query/reminder", params={"user_id": "u"})
        assert r.status_code == 200
        text = r.json()["reminder"]
        assert text.startswith("<system-reminder>")
        assert len(text) <= 2000  # injector MAX_REMINDER_CHARS
