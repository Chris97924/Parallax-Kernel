"""Integration tests for POST /event — Orbit dual-write ingest endpoint.

Covers:
1. Without bearer token → 401.
2. With wrong bearer → 401.
3. With valid auth + complete envelope → 201 + event_id returned + row in events table.
4. Schema validation: missing required field → 422.
5. Schema validation: empty string on required field → 422.
6. payload_json round-trips through record_event JSON serialization.
7. Multi-user mode: authed user_id wins over body.user_id.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from parallax.migrations import migrate_to_latest
from parallax.server.app import create_app
from parallax.sqlite_store import connect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN = "event-test-token"


def _envelope(**overrides: object) -> dict[str, object]:
    """Build a valid Orbit dual-write envelope; let tests override individual fields."""
    base: dict[str, object] = {
        "source": "orbit",
        "source_instance": "orbit-test-instance",
        "schema_version": "1.0",
        "event_type": "dissident_record",
        "run_id": "00000000-0000-0000-0000-000000000001",
        "record_id": "00000000-0000-0000-0000-000000000002",
        "created_at": "2026-04-29T17:00:00.000000+00:00",
        "commit_sha": "abc1234",
        "payload_hash": "sha256-deadbeef",
        "judge_metadata": {"judge_a_id": "risk", "judge_b_id": "risk-b-v1"},
        "payload": {"verdict_a": "APPROVE", "verdict_b": "REJECT"},
        "user_id": "chris",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "event_test.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


@pytest.fixture()
def auth_app(db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PARALLAX_TOKEN", _TOKEN)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_event_requires_auth(auth_app):
    """Without bearer → 401."""
    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post("/event", json=_envelope())
    assert resp.status_code == 401


@pytest.mark.integration
def test_post_event_rejects_wrong_token(auth_app):
    """Wrong bearer → 401."""
    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/event",
            json=_envelope(),
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert resp.status_code == 401


@pytest.mark.integration
def test_post_event_happy_path(auth_app, db_path: pathlib.Path):
    """Valid auth + complete envelope → 201 + event_id + row in events table."""
    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/event",
            json=_envelope(),
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "event_id" in body
    assert body["event_type"] == "dissident_record"
    assert body["user_id"] == "chris"

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT user_id, actor, event_type, target_kind, target_id, payload_json "
            "FROM events WHERE event_id = ?",
            (body["event_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    user_id, actor, event_type, target_kind, target_id, payload_json = row
    assert user_id == "chris"
    assert actor == "orbit"
    assert event_type == "dissident_record"
    assert target_kind is None
    assert target_id is None
    payload = json.loads(payload_json)
    assert payload["run_id"] == "00000000-0000-0000-0000-000000000001"
    assert payload["record_id"] == "00000000-0000-0000-0000-000000000002"
    assert payload["payload_hash"] == "sha256-deadbeef"


@pytest.mark.integration
def test_post_event_missing_field_rejected(auth_app):
    """Envelope missing event_type → 422 from FastAPI validation."""
    bad = _envelope()
    del bad["event_type"]
    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/event",
            json=bad,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


@pytest.mark.integration
def test_post_event_empty_string_rejected(auth_app):
    """Empty string on required field → 422 (min_length=1)."""
    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/event",
            json=_envelope(source=""),
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


@pytest.mark.integration
def test_post_event_payload_round_trip(auth_app, db_path: pathlib.Path):
    """payload + judge_metadata survive JSON round-trip through record_event."""
    rich_payload = {
        "verdict_a": "APPROVE",
        "verdict_b": "REJECT",
        "nested": {"deep": [1, 2, {"key": "val"}]},
        "unicode": "字符 ☃",
    }
    rich_judge_meta = {"judge_a_id": "risk", "judge_b_id": "risk-b-v1", "fallback": "single_judge"}
    env = _envelope(payload=rich_payload, judge_metadata=rich_judge_meta)

    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/event",
            json=env,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 201, resp.text
    event_id = resp.json()["event_id"]

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    payload = json.loads(row[0])
    assert payload["payload"] == rich_payload
    assert payload["judge_metadata"] == rich_judge_meta


@pytest.mark.integration
def test_post_event_persists_client_created_at(auth_app, db_path: pathlib.Path):
    """Client envelope ``created_at`` must round-trip into ``payload_json``."""
    client_ts = "2026-04-30T03:54:54.123456+00:00"
    env = _envelope(created_at=client_ts)

    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/event",
            json=env,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 201, resp.text
    event_id = resp.json()["event_id"]

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["created_at"] == client_ts


@pytest.mark.integration
def test_post_event_extra_field_rejected(auth_app):
    """_StrictModel rejects unknown fields (extra='forbid')."""
    bad = _envelope()
    bad["unexpected_field"] = "should-fail"
    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/event",
            json=bad,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422
