"""US-009.1 §3.2 criteria 1.5–1.8 — audit log tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from parallax.canary.audit_log import (
    AUDIT_DB_ENV,
    DEFAULT_AUDIT_DB_NAME,
    AuditLog,
    AuditRecord,
    make_record,
)


def _record(event_id: str = "evt-1", *, idempotency_hit: bool = False) -> AuditRecord:
    return make_record(
        event_id=event_id,
        response_status=200,
        latency_ms=12.5,
        idempotency_hit=idempotency_hit,
    )


# ---------------------------------------------------------------------------
# Criterion 1.5 — independent SQLite file
# ---------------------------------------------------------------------------


def test_audit_log_uses_separate_db_file(tmp_path: Path) -> None:
    main_db = tmp_path / "main.db"
    main_db.touch()  # represents the main business DB
    audit_path = tmp_path / "audit.db"
    log = AuditLog(audit_path)
    try:
        assert log.db_path == audit_path
        assert log.db_path != main_db
        assert audit_path.exists()
    finally:
        log.close()


def test_audit_log_resolves_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "from-env.db"
    monkeypatch.setenv(AUDIT_DB_ENV, str(target))
    log = AuditLog()
    try:
        assert log.db_path == target
    finally:
        log.close()


def test_audit_log_default_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(AUDIT_DB_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    log = AuditLog()
    try:
        assert log.db_path.name == DEFAULT_AUDIT_DB_NAME
    finally:
        log.close()


# ---------------------------------------------------------------------------
# Criterion 1.6 — schema columns
# ---------------------------------------------------------------------------


def test_audit_log_schema_contains_required_columns(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    try:
        schema = log.schema()
    finally:
        log.close()
    required = {
        "event_id",
        "request_at_iso",
        "response_status",
        "latency_ms",
        "idempotency_hit",
        "created_at",
    }
    assert required.issubset(schema.keys()), f"missing columns: {required - schema.keys()}"
    # Spot-check declared types
    assert schema["event_id"] == "TEXT"
    assert schema["response_status"] == "INTEGER"
    assert schema["latency_ms"] == "REAL"


def test_audit_log_event_id_is_primary_key(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"
    log = AuditLog(db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("PRAGMA table_info(audit_log)").fetchall()
    finally:
        log.close()
    pk_columns = [r[1] for r in rows if r[5] != 0]
    assert pk_columns == ["event_id"]


# ---------------------------------------------------------------------------
# Criterion 1.7 — every request (incl. cache hit) writes audit
# ---------------------------------------------------------------------------


def test_record_inserts_row(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    try:
        ok = log.record(_record("evt-A"), response_body='{"ok":true}')
        assert ok is True
        row = log.lookup("evt-A")
        assert row is not None
        assert row.event_id == "evt-A"
        assert row.idempotency_hit is False
        assert row.response_status == 200
        assert log.lookup_response("evt-A") == '{"ok":true}'
    finally:
        log.close()


def test_record_marks_idempotency_hit(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    try:
        log.record(_record("evt-B"), response_body="b")
        log.record(_record("evt-B", idempotency_hit=True), response_body="b")
        row = log.lookup("evt-B")
        assert row is not None
        assert row.idempotency_hit is True
    finally:
        log.close()


# ---------------------------------------------------------------------------
# Criterion 1.8 — audit failure must NOT raise
# ---------------------------------------------------------------------------


def test_record_swallows_sqlite_error(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    try:
        with mock.patch.object(log, "_connect", side_effect=sqlite3.Error("boom")):
            ok = log.record(_record("evt-C"))
        assert ok is False  # surfaced via return, not exception
    finally:
        log.close()


def test_record_swallows_value_error(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    try:
        with mock.patch.object(log, "_connect", side_effect=ValueError("bad")):
            ok = log.record(_record("evt-D"))
        assert ok is False
    finally:
        log.close()


def test_lookup_swallows_sqlite_error(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    try:
        with mock.patch.object(log, "_connect", side_effect=sqlite3.Error):
            assert log.lookup("never") is None
    finally:
        log.close()


# ---------------------------------------------------------------------------
# Manual ACK trail — criterion 1.17
# ---------------------------------------------------------------------------


def test_record_ack_writes_operator(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    try:
        ok = log.record_ack("rollback-1", ack_by="oncall@parallax")
        assert ok is True
        row = log.lookup("rollback-1")
        assert row is not None
        assert row.ack_by == "oncall@parallax"
        assert row.ack_at is not None
    finally:
        log.close()


def test_record_ack_rejects_blank_operator(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    try:
        assert log.record_ack("rollback-2", ack_by="") is False
    finally:
        log.close()
