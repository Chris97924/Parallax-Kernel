"""Tests for the /viewer read-only web interface.

Covers:
* GET /viewer/ returns 200 HTML containing "parallax"
* GET /viewer/events.json returns seeded event
* GET /viewer/claims.json returns seeded claim
* GET /viewer/retrieve.json returns trace with 'stages' key
* PARALLAX_VIEWER_ENABLED unset → /viewer/ returns 404
* Auth enforced: no bearer token → 401 when PARALLAX_TOKEN is set
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from parallax.migrations import migrate_to_latest
from parallax.server import create_app
from parallax.server.auth import hash_token
from parallax.sqlite_store import connect, now_iso

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def viewer_db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Fresh migrated DB."""
    p = tmp_path / "viewer.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


@pytest.fixture()
def viewer_app(
    viewer_db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """App with PARALLAX_VIEWER_ENABLED=1, no auth (open mode)."""
    monkeypatch.setenv("PARALLAX_VIEWER_ENABLED", "1")
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(viewer_db_path))

    def factory() -> sqlite3.Connection:
        return connect(viewer_db_path)

    return create_app(db_factory=factory)


def _create_token(db_path: pathlib.Path, *, user_id: str) -> str:
    """Mint a per-user token, persist only its hash, return the plaintext."""
    import secrets

    plaintext = secrets.token_urlsafe(24)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO api_tokens(token_hash, user_id, created_at, "
            "revoked_at, label) VALUES (?, ?, ?, NULL, NULL)",
            (hash_token(plaintext), user_id, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return plaintext


def _seed_event(db_path: pathlib.Path, *, event_id: str, user_id: str) -> None:
    """Insert a single event row for ``user_id`` (no FK on target_id)."""
    conn = connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO events
                (event_id, user_id, actor, event_type, target_kind, target_id,
                 payload_json, approval_tier, created_at, session_id)
            VALUES (?, ?, 'test', 'test.event', NULL, NULL,
                    '{"k":"v"}', NULL, '2026-04-21T00:00:00.000000+00:00', NULL)
            """,
            (event_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_claim(
    db_path: pathlib.Path,
    *,
    claim_id: str,
    user_id: str,
    subject: str,
) -> None:
    """Insert a source + claim row for ``user_id`` (claims FK source_id)."""
    conn = connect(db_path)
    try:
        source_id = f"src-{claim_id}"
        conn.execute(
            "INSERT INTO sources(source_id, uri, kind, content_hash, "
            "user_id, ingested_at, state) "
            "VALUES (?, ?, 'test', ?, ?, ?, 'active')",
            (source_id, f"test://{claim_id}", f"hash-{claim_id}", user_id, now_iso()),
        )
        conn.execute(
            "INSERT INTO claims(claim_id, user_id, subject, predicate, object, "
            "source_id, content_hash, confidence, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'is', 'a thing', ?, ?, 1.0, 'active', ?, ?)",
            (
                claim_id,
                user_id,
                subject,
                source_id,
                f"hash-{claim_id}",
                now_iso(),
                now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def mu_viewer_db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Fresh migrated DB for the multi-user viewer tests."""
    p = tmp_path / "mu_viewer.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


@pytest.fixture()
def mu_viewer_app(
    mu_viewer_db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """Viewer app with PARALLAX_MULTI_USER=1 (per-user bearer tokens)."""
    monkeypatch.setenv("PARALLAX_VIEWER_ENABLED", "1")
    monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(mu_viewer_db_path))

    def factory() -> sqlite3.Connection:
        return connect(mu_viewer_db_path)

    return create_app(db_factory=factory)


@pytest.fixture()
def mu_viewer_client(mu_viewer_app: FastAPI) -> TestClient:
    with TestClient(mu_viewer_app) as c:
        yield c


@pytest.fixture()
def viewer_client(viewer_app: FastAPI, viewer_db_path: pathlib.Path) -> TestClient:
    with TestClient(viewer_app) as c:
        # Seed a claim via the ingest API (handles FK + source automatically).
        resp = c.post(
            "/ingest/claim",
            json={
                "user_id": "u1",
                "subject": "Paris",
                "predicate": "is",
                "object": "a city",
            },
        )
        assert resp.status_code == 201
        # Seed an event directly — events table has no FK on target_id.
        conn = connect(viewer_db_path)
        try:
            conn.execute(
                """
                INSERT INTO events
                    (event_id, user_id, actor, event_type, target_kind, target_id,
                     payload_json, approval_tier, created_at, session_id)
                VALUES ('evt-001', 'u1', 'test', 'test.event', NULL, NULL,
                        '{"k":"v"}', NULL, '2026-04-21T00:00:00.000000+00:00', NULL)
                """
            )
            conn.commit()
        finally:
            conn.close()
        yield c


@pytest.fixture()
def viewer_auth_app(
    viewer_db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """App with PARALLAX_VIEWER_ENABLED=1 AND PARALLAX_TOKEN set."""
    monkeypatch.setenv("PARALLAX_VIEWER_ENABLED", "1")
    monkeypatch.setenv("PARALLAX_TOKEN", "s3cret")
    monkeypatch.setenv("PARALLAX_DB_PATH", str(viewer_db_path))

    def factory() -> sqlite3.Connection:
        return connect(viewer_db_path)

    return create_app(db_factory=factory)


@pytest.fixture()
def viewer_auth_client(viewer_auth_app: FastAPI) -> TestClient:
    with TestClient(viewer_auth_app) as c:
        yield c


@pytest.fixture()
def no_viewer_app(
    viewer_db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """App with PARALLAX_VIEWER_ENABLED unset — viewer router not mounted."""
    monkeypatch.delenv("PARALLAX_VIEWER_ENABLED", raising=False)
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(viewer_db_path))

    def factory() -> sqlite3.Connection:
        return connect(viewer_db_path)

    return create_app(db_factory=factory)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestViewerIndex:
    def test_returns_200_html(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/viewer/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_contains_parallax(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/viewer/")
        assert resp.status_code == 200
        assert "parallax" in resp.text.lower()


class TestViewerEventsJson:
    def test_returns_list(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/viewer/events.json", params={"user_id": "u1"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_includes_seeded_event(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/viewer/events.json", params={"user_id": "u1"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        # evt-001 was directly inserted; at least one event must be present
        ids = [row["event_id"] for row in data]
        assert "evt-001" in ids

    def test_event_has_expected_fields(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/viewer/events.json", params={"user_id": "u1"})
        row = resp.json()[0]
        for field in ("event_id", "kind", "target_kind", "target_id", "payload", "created_at"):
            assert field in row, f"missing field: {field}"

    def test_no_user_id_scopes_to_principal(
        self, mu_viewer_client: TestClient, mu_viewer_db_path: pathlib.Path
    ) -> None:
        """Omitting user_id in multi-user mode must NOT dump other users.

        Previously this route SELECTed events with no WHERE user_id, leaking
        every user's events to any valid-token holder. After the fix the
        authenticated principal is bound, so a caller only ever sees its own
        events even when user_id is omitted.
        """
        alice_bearer = _create_token(mu_viewer_db_path, user_id="alice")
        _seed_event(mu_viewer_db_path, event_id="evt-alice", user_id="alice")
        _seed_event(mu_viewer_db_path, event_id="evt-bob", user_id="bob")

        resp = mu_viewer_client.get(
            "/viewer/events.json",
            headers={"Authorization": f"Bearer {alice_bearer}"},
        )
        assert resp.status_code == 200
        ids = [row["event_id"] for row in resp.json()]
        assert "evt-alice" in ids
        assert "evt-bob" not in ids, "leaked another user's events"


class TestViewerEventsCrossUserIsolation:
    """Safety-critical: viewer_events must never leak across principals.

    These exercise the confirmed IDOR on GET /viewer/events.json in
    multi-user mode. Before the fix the route trusts the attacker-supplied
    ?user_id (and dumps the whole table when it is omitted); after the fix
    the authenticated principal is bound via current_user_id() so a caller
    can only ever read its OWN events.
    """

    def test_spoofed_user_id_does_not_leak(
        self, mu_viewer_client: TestClient, mu_viewer_db_path: pathlib.Path
    ) -> None:
        alice_bearer = _create_token(mu_viewer_db_path, user_id="alice")
        _create_token(mu_viewer_db_path, user_id="bob")
        _seed_event(mu_viewer_db_path, event_id="evt-alice", user_id="alice")
        _seed_event(mu_viewer_db_path, event_id="evt-bob", user_id="bob")

        # Alice's token explicitly requests bob's events — must be ignored.
        resp = mu_viewer_client.get(
            "/viewer/events.json",
            params={"user_id": "bob"},
            headers={"Authorization": f"Bearer {alice_bearer}"},
        )
        assert resp.status_code == 200
        ids = [row["event_id"] for row in resp.json()]
        assert "evt-bob" not in ids, "leaked bob's events to alice"
        assert "evt-alice" in ids

    def test_principal_sees_only_own_events(
        self, mu_viewer_client: TestClient, mu_viewer_db_path: pathlib.Path
    ) -> None:
        bob_bearer = _create_token(mu_viewer_db_path, user_id="bob")
        _create_token(mu_viewer_db_path, user_id="alice")
        _seed_event(mu_viewer_db_path, event_id="evt-alice", user_id="alice")
        _seed_event(mu_viewer_db_path, event_id="evt-bob", user_id="bob")

        resp = mu_viewer_client.get(
            "/viewer/events.json",
            headers={"Authorization": f"Bearer {bob_bearer}"},
        )
        assert resp.status_code == 200
        ids = [row["event_id"] for row in resp.json()]
        assert ids == ["evt-bob"]


class TestViewerClaimsCrossUserIsolation:
    """Safety-critical: viewer_claims must never leak across principals.

    The /viewer/claims.json route takes a required ?user_id and queries
    WHERE user_id = ? but (before the fix) does NOT bind the authenticated
    principal — so any valid-token holder could read another user's claims
    by passing their user_id. After the fix current_user_id() binds the
    authed principal so a caller can only ever read its OWN claims.
    """

    def test_spoofed_user_id_does_not_leak(
        self, mu_viewer_client: TestClient, mu_viewer_db_path: pathlib.Path
    ) -> None:
        alice_bearer = _create_token(mu_viewer_db_path, user_id="alice")
        _create_token(mu_viewer_db_path, user_id="bob")
        _seed_claim(
            mu_viewer_db_path,
            claim_id="clm-alice",
            user_id="alice",
            subject="AliceSecret",
        )
        _seed_claim(
            mu_viewer_db_path,
            claim_id="clm-bob",
            user_id="bob",
            subject="BobSecret",
        )

        # Alice's token explicitly requests bob's claims — must be ignored.
        resp = mu_viewer_client.get(
            "/viewer/claims.json",
            params={"user_id": "bob"},
            headers={"Authorization": f"Bearer {alice_bearer}"},
        )
        assert resp.status_code == 200
        subjects = [c["subject"] for c in resp.json()]
        assert "BobSecret" not in subjects, "leaked bob's claims to alice"
        assert "AliceSecret" in subjects


class TestViewerClaimsJson:
    def test_returns_seeded_claim(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/viewer/claims.json", params={"user_id": "u1"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        subjects = [c["subject"] for c in data]
        assert "Paris" in subjects

    def test_claim_has_spo_fields(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/viewer/claims.json", params={"user_id": "u1"})
        claim = resp.json()[0]
        for field in ("subject", "predicate", "object", "confidence", "state"):
            assert field in claim, f"missing field: {field}"

    def test_empty_user_returns_empty(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/viewer/claims.json", params={"user_id": "nobody"})
        assert resp.status_code == 200
        assert resp.json() == []


class TestViewerRetrieveJson:
    def test_returns_trace_with_stages(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(
            "/viewer/retrieve.json",
            params={"q": "Paris", "kind": "by_entity", "user_id": "u1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "stages" in data, f"'stages' key missing from: {list(data.keys())}"

    def test_trace_has_kind_field(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(
            "/viewer/retrieve.json",
            params={"q": "Paris", "kind": "by_entity", "user_id": "u1"},
        )
        data = resp.json()
        assert data["kind"] == "entity"

    def test_trace_hits_contains_seeded_claim(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(
            "/viewer/retrieve.json",
            params={"q": "Paris", "kind": "by_entity", "user_id": "u1"},
        )
        hits = resp.json().get("hits", [])
        assert len(hits) >= 1
        # The seeded claim (Paris is a city) must appear as a hit.
        assert any(h.get("entity_kind") == "claim" for h in hits)


class TestViewerRetrieveTimeline:
    """kind=timeline previously always crashed with a 500.

    ``explain_retrieve(kind='timeline', ...)`` raises ``ValueError`` when
    ``since``/``until`` are missing, but the route never accepted or forwarded
    those query params and never caught the ``ValueError`` — so any
    ``kind=timeline`` request hit an unhandled exception -> 500. Fixed by
    accepting optional ``since``/``until`` query params and translating
    missing-or-invalid values into a 422 instead of letting the ValueError
    propagate.
    """

    def test_missing_since_until_returns_422_naming_them(
        self, viewer_client: TestClient
    ) -> None:
        resp = viewer_client.get(
            "/viewer/retrieve.json",
            params={"kind": "timeline", "user_id": "u1"},
        )
        assert resp.status_code == 422, (
            f"expected 422, got {resp.status_code}: {resp.text}"
        )
        detail = str(resp.json().get("detail", ""))
        assert "since" in detail and "until" in detail, (
            f"422 detail must name since/until, got: {detail!r}"
        )

    def test_since_and_until_returns_200_timeline_trace(
        self, viewer_client: TestClient
    ) -> None:
        resp = viewer_client.get(
            "/viewer/retrieve.json",
            params={
                "kind": "timeline",
                "user_id": "u1",
                "since": "2026-04-20T00:00:00Z",
                "until": "2026-04-22T00:00:00Z",
            },
        )
        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["kind"] == "timeline"
        assert "stages" in data

    def test_non_timeline_kind_unaffected_by_new_params(
        self, viewer_client: TestClient
    ) -> None:
        resp = viewer_client.get(
            "/viewer/retrieve.json",
            params={"q": "Paris", "kind": "by_entity", "user_id": "u1"},
        )
        assert resp.status_code == 200
        assert resp.json()["kind"] == "entity"


class TestViewerRetrieveCrossUserIsolation:
    """Safety-critical: viewer_retrieve must never leak across principals.

    The /viewer/retrieve.json route takes a required ?user_id and passes it
    straight to explain_retrieve (which scopes WHERE user_id = ?) but
    (before the fix) does NOT bind the authenticated principal — so any
    valid-token holder could explain another user's retrieval by passing
    their user_id. After the fix current_user_id() binds the authed
    principal so a caller can only ever retrieve over its OWN data.
    """

    def test_spoofed_user_id_does_not_leak(
        self, mu_viewer_client: TestClient, mu_viewer_db_path: pathlib.Path
    ) -> None:
        alice_bearer = _create_token(mu_viewer_db_path, user_id="alice")
        _create_token(mu_viewer_db_path, user_id="bob")
        _seed_claim(
            mu_viewer_db_path,
            claim_id="clm-alice",
            user_id="alice",
            subject="AliceTopic",
        )
        _seed_claim(
            mu_viewer_db_path,
            claim_id="clm-bob",
            user_id="bob",
            subject="BobTopic",
        )

        # Alice's token explains a retrieval over bob's data — must be ignored.
        resp = mu_viewer_client.get(
            "/viewer/retrieve.json",
            params={"q": "BobTopic", "kind": "by_entity", "user_id": "bob"},
            headers={"Authorization": f"Bearer {alice_bearer}"},
        )
        assert resp.status_code == 200
        hits = resp.json().get("hits", [])
        # bob's claim must never appear in alice's retrieval trace.
        leaked = [h for h in hits if h.get("entity_id") == "clm-bob"]
        assert not leaked, "leaked bob's claim into alice's retrieval"
        # The trace must be scoped to alice (params echo the bound principal).
        assert resp.json()["params"]["user_id"] == "alice"


class TestViewerDisabledReturns404:
    def test_viewer_index_404_when_disabled(
        self, no_viewer_app: FastAPI
    ) -> None:
        with TestClient(no_viewer_app) as c:
            resp = c.get("/viewer/")
        assert resp.status_code == 404

    def test_viewer_events_404_when_disabled(
        self, no_viewer_app: FastAPI
    ) -> None:
        with TestClient(no_viewer_app) as c:
            resp = c.get("/viewer/events.json")
        assert resp.status_code == 404


class TestViewerAuthEnforced:
    def test_no_token_returns_401(self, viewer_auth_client: TestClient) -> None:
        resp = viewer_auth_client.get("/viewer/")
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, viewer_auth_client: TestClient) -> None:
        resp = viewer_auth_client.get(
            "/viewer/", headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    def test_correct_token_returns_200(self, viewer_auth_client: TestClient) -> None:
        resp = viewer_auth_client.get(
            "/viewer/", headers={"Authorization": "Bearer s3cret"}
        )
        assert resp.status_code == 200
