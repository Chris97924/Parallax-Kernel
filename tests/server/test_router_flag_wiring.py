"""Tests for Wave 2 (US-D3-03): server-side MEMORY_ROUTER flag wiring.

Coverage:
- /ingest/memory and /ingest/claim with router off and on
- dedup detection via router-on second ingest
- /backfill router off returns 400, router on returns 200
- /inspect/health unauthenticated returns ok-only payload
- /inspect/health authenticated returns full HealthResponse
- content_hash byte-equality across router-on and router-off paths
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from parallax.server import create_app
from parallax.sqlite_store import connect

# ---------------------------------------------------------------------------
# Extra fixtures for router-enabled app
# ---------------------------------------------------------------------------


@pytest.fixture()
def router_app(db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """App with MEMORY_ROUTER=true and no bearer auth."""
    monkeypatch.setenv("MEMORY_ROUTER", "true")
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


@pytest.fixture()
def router_client(router_app) -> TestClient:
    with TestClient(router_app) as c:
        yield c


# ---------------------------------------------------------------------------
# /ingest/memory
# ---------------------------------------------------------------------------


class TestIngestMemoryRouterOff:
    def test_post_ingest_memory_router_off(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MEMORY_ROUTER", raising=False)
        resp = client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "T",
                "summary": "S",
                "vault_path": "notes/t.md",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["kind"] == "memory"
        assert body["user_id"] == "u"
        assert len(body["id"]) > 0
        # router-off: no deduped field required


class TestIngestMemoryRouterOn:
    def test_post_ingest_memory_router_on(self, router_client: TestClient) -> None:
        resp = router_client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "TitleOn",
                "summary": "SummaryOn",
                "vault_path": "notes/on.md",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["kind"] == "memory"
        assert body["user_id"] == "u"
        assert len(body["id"]) > 0
        assert body["deduped"] is False

    def test_post_ingest_memory_dedup_router_on(self, router_client: TestClient) -> None:
        payload = {
            "user_id": "u",
            "title": "DedupTitle",
            "summary": "DedupSummary",
            "vault_path": "notes/dedup.md",
        }
        r1 = router_client.post("/ingest/memory", json=payload)
        r2 = router_client.post("/ingest/memory", json=payload)
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"]
        assert r1.json()["deduped"] is False
        assert r2.json()["deduped"] is True


# ---------------------------------------------------------------------------
# /ingest/claim
# ---------------------------------------------------------------------------


class TestIngestClaimRouterOn:
    def test_post_ingest_claim_router_on(self, router_client: TestClient) -> None:
        resp = router_client.post(
            "/ingest/claim",
            json={
                "user_id": "u",
                "subject": "Parallax",
                "predicate": "is",
                "object": "awesome",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["kind"] == "claim"
        assert body["user_id"] == "u"
        assert len(body["id"]) > 0
        assert "deduped" in body


# ---------------------------------------------------------------------------
# /backfill
# ---------------------------------------------------------------------------


class TestBackfillRouterOff:
    def test_post_backfill_router_off(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MEMORY_ROUTER", raising=False)
        resp = client.post(
            "/backfill",
            json={
                "user_id": "u",
                "crosswalk_version": "v1",
                "dry_run": True,
                "scope": "sample",
            },
        )
        assert resp.status_code == 400
        assert "MEMORY_ROUTER is not enabled" in resp.json()["detail"]


class TestBackfillRouterOn:
    def test_post_backfill_router_on(self, router_client: TestClient) -> None:
        resp = router_client.post(
            "/backfill",
            json={
                "user_id": "u",
                "crosswalk_version": "v1",
                "dry_run": True,
                "scope": "sample",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rows_examined"] >= 0
        assert "arbitrations" in body


# ---------------------------------------------------------------------------
# /inspect/health — H-2 auth gating
# ---------------------------------------------------------------------------


class TestHealthUnauthRouterOff:
    def test_health_unauth_router_off(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In open mode (no PARALLAX_TOKEN), full response is returned to all callers."""
        monkeypatch.delenv("MEMORY_ROUTER", raising=False)
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        resp = client.get("/inspect/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert body["status"] in ("ok", "degraded")
        # In open mode, full response is returned
        assert "table_counts" in body


class TestHealthUnauthRouterOn:
    def test_health_unauth_router_on(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When auth IS configured and no bearer sent, unauthenticated callers get ok-only."""
        monkeypatch.setenv("MEMORY_ROUTER", "true")
        monkeypatch.setenv("PARALLAX_TOKEN", "secret-tok")
        monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

        def factory() -> sqlite3.Connection:
            return connect(db_path)

        app = create_app(db_factory=factory)
        with TestClient(app) as c:
            resp = c.get("/inspect/health")  # no Authorization header
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert body["status"] in ("ok", "degraded")
        # No auth header when token IS required → ok-only
        assert "table_counts" not in body
        assert "journal_mode" not in body


class TestHealthAuthReturnsFullPayload:
    def test_health_auth_returns_full(
        self, auth_client: TestClient
    ) -> None:
        # auth_client sends Bearer t0ken, which counts as authenticated
        resp = auth_client.get(
            "/inspect/health",
            headers={"Authorization": "Bearer t0ken"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "table_counts" in body
        assert "journal_mode" in body


# ---------------------------------------------------------------------------
# H-2 regression: invalid bearer must not bypass health redaction
# ---------------------------------------------------------------------------


class TestHealthInvalidBearerRedacted:
    """P1 regression: any non-None credentials must be validated, not just present."""

    def test_garbage_bearer_returns_ok_only(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_TOKEN", "real-secret")
        monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

        def factory() -> sqlite3.Connection:
            return connect(db_path)

        app = create_app(db_factory=factory)
        with TestClient(app) as c:
            resp = c.get(
                "/inspect/health",
                headers={"Authorization": "Bearer garbage-not-the-real-token"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "table_counts" not in body
        assert "journal_mode" not in body

    def test_correct_bearer_returns_full_payload(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_TOKEN", "real-secret")
        monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

        def factory() -> sqlite3.Connection:
            return connect(db_path)

        app = create_app(db_factory=factory)
        with TestClient(app) as c:
            resp = c.get(
                "/inspect/health",
                headers={"Authorization": "Bearer real-secret"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "table_counts" in body
        assert "journal_mode" in body


# ---------------------------------------------------------------------------
# H-2 multi-user mode: health redaction must apply when PARALLAX_MULTI_USER=1
# ---------------------------------------------------------------------------


class TestHealthMultiUserRedaction:
    """Multi-user mode (no PARALLAX_TOKEN) must still gate the full payload."""

    @pytest.fixture()
    def mu_db_path(self, tmp_path: pathlib.Path) -> pathlib.Path:
        from parallax.migrations import migrate_to_latest
        from parallax.sqlite_store import connect as _connect

        p = tmp_path / "mu_health.db"
        c = _connect(p)
        try:
            migrate_to_latest(c)
        finally:
            c.close()
        return p

    def _make_app(
        self,
        mu_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.setenv("PARALLAX_DB_PATH", str(mu_db_path))

        def factory() -> sqlite3.Connection:
            return connect(mu_db_path)

        return create_app(db_factory=factory)

    def test_no_bearer_returns_ok_only(
        self,
        mu_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app = self._make_app(mu_db_path, monkeypatch)
        with TestClient(app) as cl:
            resp = cl.get("/inspect/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "table_counts" not in body

    def test_invalid_bearer_returns_ok_only(
        self,
        mu_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app = self._make_app(mu_db_path, monkeypatch)
        with TestClient(app) as cl:
            resp = cl.get(
                "/inspect/health",
                headers={"Authorization": "Bearer not-a-real-mu-token"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "table_counts" not in body

    def test_valid_bearer_returns_full_payload(
        self,
        mu_db_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import secrets

        from parallax.server.auth import hash_token
        from parallax.sqlite_store import connect as _connect
        from parallax.sqlite_store import now_iso

        plaintext = secrets.token_urlsafe(24)
        c = _connect(mu_db_path)
        c.execute(
            "INSERT INTO api_tokens(token_hash, user_id, created_at, revoked_at, label)"
            " VALUES (?, ?, ?, NULL, NULL)",
            (hash_token(plaintext), "u1", now_iso()),
        )
        c.commit()
        c.close()
        app = self._make_app(mu_db_path, monkeypatch)
        with TestClient(app) as cl:
            resp = cl.get(
                "/inspect/health",
                headers={"Authorization": f"Bearer {plaintext}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "table_counts" in body
        assert "journal_mode" in body


# ---------------------------------------------------------------------------
# Content hash byte-equality across router-on / router-off
# ---------------------------------------------------------------------------


class TestIngestContentHashByteEquality:
    def test_ingest_content_hash_byte_equality(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same content ingested via router-on and router-off paths produces
        same content_hash in the DB."""
        payload = {
            "user_id": "u_hash",
            "title": "Hash Title",
            "summary": "Hash Summary",
            "vault_path": "notes/hash.md",
        }

        # Ingest via router-off
        monkeypatch.delenv("MEMORY_ROUTER", raising=False)

        def factory() -> sqlite3.Connection:
            return connect(db_path)

        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))
        app_off = create_app(db_factory=factory)

        with TestClient(app_off) as c_off:
            r_off = c_off.post("/ingest/memory", json=payload)
        assert r_off.status_code == 201

        # Read content_hash for the first ingest
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT content_hash FROM memories WHERE user_id = 'u_hash' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        hash_off = row[0]

        # Ingest same payload again via router-on (should dedup, same content_hash)
        monkeypatch.setenv("MEMORY_ROUTER", "true")
        app_on = create_app(db_factory=factory)
        with TestClient(app_on) as c_on:
            r_on = c_on.post("/ingest/memory", json=payload)
        assert r_on.status_code == 201

        conn = connect(db_path)
        try:
            rows = conn.execute(
                "SELECT content_hash FROM memories WHERE user_id = 'u_hash'"
            ).fetchall()
        finally:
            conn.close()
        # Should still be exactly 1 row (deduped)
        assert len(rows) == 1
        assert rows[0][0] == hash_off
