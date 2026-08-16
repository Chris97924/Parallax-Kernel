"""Mutation-hardening for ``parallax.canary.audit_log`` (overnight-20260816 S9).

Companion to ``tests/canary/test_audit_log.py``, which covers the US-009.1
§3.2 acceptance criteria (1.5-1.8, 1.17) at the level they were written:
"a row is written", "a failure returns False", "created_at survives a
re-record". Each test below was written against a semantic mutant that
survived the whole suite anyway, because the existing tests read every row
back through the *same* connection that wrote it and never re-record a row
with different content:

  * ``sqlite3.connect(..., isolation_level=None)`` is the only reason a
    recorded row is ever durable — ``record()`` issues no ``commit()``.
    Restoring the driver default (implicit transactions) loses every audit
    row when the process exits, and the whole suite stays green because a
    reader on the writing connection sees its own open transaction.
  * The three ``COALESCE(excluded.col, col)`` clauses in ``record``'s
    ON CONFLICT arm exist so a later write that omits a column preserves
    what is already there. No test re-records with a *different* payload,
    so dropping any of them — a cache-hit rewrite wiping the cached
    response body, an ordinary request wiping the operator ACK trail —
    changed nothing.
  * ``record_ack`` updates only ``ack_by``/``ack_at`` so ACKing an event
    keeps its real response status; no test ACKs an id that already exists.
  * ``_resolve_path``'s explicit > env > cwd precedence: every existing test
    exercises exactly one of the three arms, so reversing them was invisible.
  * ``lookup_response``'s ``isinstance(body, str)`` narrowing, the
    ``OSError`` arm of criterion 1.8, the per-thread connection cache, and
    ``close()``'s cache clear were all unpinned.

Read alongside ``tests/canary/test_outcomes_mutation_harden.py`` (S6), which
does the same job for the sibling ``OutcomeStore`` on the same SQLite file.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
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


@pytest.fixture
def log(tmp_path: Path):
    audit = AuditLog(tmp_path / "audit.db")
    yield audit
    audit.close()


def _record(
    event_id: str = "evt-1",
    *,
    status: int = 200,
    latency_ms: float | None = 12.5,
    idempotency_hit: bool = False,
) -> AuditRecord:
    return make_record(
        event_id=event_id,
        response_status=status,
        latency_ms=latency_ms,
        idempotency_hit=idempotency_hit,
    )


# ===========================================================================
# Durability — the write must leave the writing connection
# ===========================================================================


@pytest.mark.unit
class TestWritesAreDurable:
    def test_recorded_row_is_visible_to_a_separate_connection(self, tmp_path: Path) -> None:
        """``record()`` never calls ``commit()`` — autocommit is what saves the row.

        The connection is opened with ``isolation_level=None``; restore the
        driver default and every INSERT sits in an implicit transaction that
        nothing ever commits, so the audit trail is silently discarded when the
        process exits. The existing suite cannot see this because it reads back
        through the same connection, which observes its own uncommitted writes.
        In production the DoD verifier is a *separate process*.
        """
        db = tmp_path / "audit.db"
        writer = AuditLog(db)
        try:
            assert writer.record(_record("evt-durable", status=201), response_body="cached") is True

            raw = sqlite3.connect(db, timeout=5.0)
            try:
                row = raw.execute(
                    "SELECT response_status, response_body FROM audit_log WHERE event_id = ?",
                    ("evt-durable",),
                ).fetchone()
            finally:
                raw.close()
        finally:
            writer.close()

        assert row == (201, "cached")

    def test_ack_row_is_visible_to_a_separate_connection(self, tmp_path: Path) -> None:
        """Same contract for the ACK write path (criterion 1.17)."""
        db = tmp_path / "audit.db"
        writer = AuditLog(db)
        try:
            assert writer.record_ack("rollback-durable", ack_by="oncall@parallax") is True
            raw = sqlite3.connect(db, timeout=5.0)
            try:
                row = raw.execute(
                    "SELECT ack_by FROM audit_log WHERE event_id = ?",
                    ("rollback-durable",),
                ).fetchone()
            finally:
                raw.close()
        finally:
            writer.close()

        assert row == ("oncall@parallax",)


# ===========================================================================
# UPSERT column semantics — what a re-record keeps and what it refreshes
# ===========================================================================


@pytest.mark.unit
class TestReRecordPreservesOmittedColumns:
    def test_cache_hit_rewrite_keeps_the_cached_response_body(self, log: AuditLog) -> None:
        """A re-record that omits the body must NOT erase the cached payload.

        Criterion 1.7 writes an audit row on every idempotency cache hit, and
        that write carries no ``response_body`` — the body is already stored.
        Dropping ``COALESCE(excluded.response_body, response_body)`` makes the
        first cache hit null out the very payload the cache exists to serve, so
        ``idempotency.py`` (which reads None as "not found") silently
        re-executes the request.
        """
        log.record(_record("evt-body"), response_body='{"ok":true}')
        log.record(_record("evt-body", idempotency_hit=True))  # no body this time

        assert log.lookup_response("evt-body") == '{"ok":true}'

    def test_ordinary_record_keeps_an_existing_ack_trail(self, log: AuditLog) -> None:
        """An ordinary request write must not wipe ``ack_by``/``ack_at``.

        The ACK columns are written by a different method on the same PK row;
        without the COALESCE arms the next ordinary ``record()`` for that
        ``event_id`` erases the operator trail that criterion 1.17 requires be
        retained.
        """
        log.record_ack("evt-acked", ack_by="oncall@parallax", ack_at="2026-08-16T00:00:00.000Z")
        log.record(_record("evt-acked", status=200))

        row = log.lookup("evt-acked")
        assert row is not None
        assert (row.ack_by, row.ack_at) == ("oncall@parallax", "2026-08-16T00:00:00.000Z")

    def test_re_record_refreshes_status_and_latency(self, log: AuditLog) -> None:
        """Positive twin: the columns the retry DOES carry must overwrite.

        ``DO NOTHING`` (or a narrower update list) would keep the stale 200/10ms
        after the request was retried and came back 503 — the audit trail would
        disagree with what the caller actually saw.
        """
        log.record(_record("evt-refresh", status=200, latency_ms=10.0))
        log.record(_record("evt-refresh", status=503, latency_ms=99.5))

        row = log.lookup("evt-refresh")
        assert row is not None
        assert (row.response_status, row.latency_ms) == (503, 99.5)

    def test_re_record_with_a_new_body_overwrites_the_old_one(self, log: AuditLog) -> None:
        """COALESCE preserves on NULL only — a present body still wins."""
        log.record(_record("evt-newbody"), response_body="first")
        log.record(_record("evt-newbody"), response_body="second")

        assert log.lookup_response("evt-newbody") == "second"

    def test_re_record_does_not_add_a_second_row(self, log: AuditLog) -> None:
        log.record(_record("evt-single"))
        log.record(_record("evt-single", idempotency_hit=True))

        count = log._connect().execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        assert count == 1


@pytest.mark.unit
class TestRecordAckIsNarrow:
    def test_acking_an_existing_event_keeps_its_response_status(self, log: AuditLog) -> None:
        """``record_ack``'s INSERT arm supplies ``response_status = 0``.

        That literal is only correct for the synthetic rollback UUIDs the
        docstring describes. Widening the ON CONFLICT arm to also copy the
        excluded status/latency would stamp a real request's audit row as
        ``0 / NULL`` the moment an operator ACKs it — losing the response the
        row exists to record.
        """
        log.record(_record("evt-real", status=201, latency_ms=42.0))
        assert log.record_ack("evt-real", ack_by="oncall@parallax") is True

        row = log.lookup("evt-real")
        assert row is not None
        assert (row.response_status, row.latency_ms) == (201, 42.0)
        assert row.ack_by == "oncall@parallax"

    def test_explicit_ack_at_is_honoured(self, log: AuditLog) -> None:
        """The caller's timestamp must be stored, not silently replaced by now().

        The existing test only asserts ``ack_at is not None``, so ignoring the
        argument entirely — which would backdate every replayed ACK to the time
        the row was re-imported — survived.
        """
        log.record_ack("evt-ts", ack_by="oncall@parallax", ack_at="2026-08-16T07:30:00.000Z")

        row = log.lookup("evt-ts")
        assert row is not None
        assert row.ack_at == "2026-08-16T07:30:00.000Z"

    def test_omitted_ack_at_defaults_to_now(self, log: AuditLog) -> None:
        """Positive twin: without the argument a timestamp is still generated."""
        log.record_ack("evt-nots", ack_by="oncall@parallax")

        row = log.lookup("evt-nots")
        assert row is not None
        assert row.ack_at
        assert row.ack_at.startswith("20")


# ===========================================================================
# Path resolution — explicit > env > cwd default
# ===========================================================================


@pytest.mark.unit
class TestPathResolutionPrecedence:
    def test_explicit_path_wins_over_the_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``AuditLog(path)`` is an override, not a fallback.

        Each existing path test sets exactly one of the two, so reversing the
        precedence was invisible — and a log that writes to the ambient env
        path instead of the one it was handed puts audit rows in a different
        file from the ``canary_outcomes`` rows the DoD query JOINs them to,
        which reads as "no canary traffic" rather than as an error.
        """
        env_db = tmp_path / "from_env.db"
        explicit_db = tmp_path / "explicit.db"
        monkeypatch.setenv(AUDIT_DB_ENV, str(env_db))

        audit = AuditLog(explicit_db)
        try:
            assert audit.db_path == explicit_db
            audit.record(_record("evt-path"))
        finally:
            audit.close()

        assert explicit_db.exists()
        assert not env_db.exists()

    def test_empty_env_var_falls_through_to_the_cwd_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PARALLAX_CANARY_AUDIT_DB=""`` must behave as unset.

        An exported-but-empty env var is the normal shape of a half-filled
        deployment template. Testing ``env is not None`` instead of the
        truthiness of ``env`` turns it into ``Path("")`` — the current
        directory — and the log then tries to open a *directory* as a database.
        """
        monkeypatch.setenv(AUDIT_DB_ENV, "")
        monkeypatch.chdir(tmp_path)

        audit = AuditLog()
        try:
            assert audit.db_path.name == DEFAULT_AUDIT_DB_NAME
            assert audit.db_path.parent == tmp_path
        finally:
            audit.close()

    def test_missing_parent_directory_is_created(self, tmp_path: Path) -> None:
        """The log builds its own parent chain rather than failing to open."""
        nested = tmp_path / "does" / "not" / "exist" / "audit.db"
        audit = AuditLog(nested)
        try:
            assert audit.record(_record("evt-nested")) is True
        finally:
            audit.close()

        assert nested.exists()


# ===========================================================================
# Criterion 1.8 — fire-and-forget covers every failure class
# ===========================================================================


@pytest.mark.unit
class TestRecordSwallowsEveryFailureClass:
    def test_record_swallows_oserror(self, log: AuditLog, caplog: pytest.LogCaptureFixture) -> None:
        """Criterion 1.8 says ANY failure — a full disk is an OSError, not a
        ``sqlite3.Error``.

        The existing tests inject ``sqlite3.Error`` and ``ValueError`` only, so
        narrowing the except tuple to those two lets a disk-full write escape
        into the canary request path and take the request down with it — the
        exact outcome criterion 1.8 forbids.
        """
        with mock.patch.object(log, "_connect", side_effect=OSError("no space left on device")):
            with caplog.at_level(logging.WARNING, logger="parallax.canary.audit_log"):
                assert log.record(_record("evt-oserror")) is False

        assert any("record_failed" in rec.getMessage() for rec in caplog.records)

    def test_record_ack_swallows_sqlite_error(self, log: AuditLog) -> None:
        """The ACK path is fire-and-forget too — it reports, never raises."""
        with mock.patch.object(log, "_connect", side_effect=sqlite3.Error("boom")):
            assert log.record_ack("evt-ack-fail", ack_by="oncall@parallax") is False

    def test_schema_returns_empty_mapping_on_db_error(self, log: AuditLog) -> None:
        """Introspection degrades to ``{}`` rather than raising."""
        with mock.patch.object(log, "_connect", side_effect=sqlite3.Error("boom")):
            assert log.schema() == {}

    def test_successful_record_returns_true(self, log: AuditLog) -> None:
        """Positive twin: the return value must separate the two cases."""
        assert log.record(_record("evt-fine")) is True


# ===========================================================================
# Read-path narrowing
# ===========================================================================


@pytest.mark.unit
class TestLookupResponseNarrowsToStr:
    def test_non_text_body_reads_as_a_cache_miss(self, tmp_path: Path) -> None:
        """A BLOB in ``response_body`` must read as None, not as bytes.

        SQLite's TEXT affinity converts numbers to text but leaves BLOBs
        untouched, so a row written by anything other than this module can hand
        ``lookup_response`` a ``bytes``. Returning it unnarrowed pushes a
        non-str into the idempotency replay path, where it is treated as a
        cached response body and serialised into an HTTP response.
        """
        db = tmp_path / "audit.db"
        audit = AuditLog(db)
        try:
            raw = sqlite3.connect(db, timeout=5.0)
            try:
                raw.execute(
                    """
                    INSERT INTO audit_log (
                        event_id, request_at_iso, response_status, response_body
                    ) VALUES (?, ?, ?, ?)
                    """,
                    ("evt-blob", "2026-08-16T00:00:00.000Z", 200, b"\x00\x01\x02"),
                )
                raw.commit()
            finally:
                raw.close()

            assert audit.lookup_response("evt-blob") is None
        finally:
            audit.close()

    def test_text_body_still_round_trips(self, log: AuditLog) -> None:
        """Positive twin: the narrowing must not reject legitimate bodies."""
        log.record(_record("evt-text"), response_body='{"ok":true}')
        assert log.lookup_response("evt-text") == '{"ok":true}'

    def test_missing_row_reads_as_none(self, log: AuditLog) -> None:
        assert log.lookup_response("evt-never-written") is None


# ===========================================================================
# Connection lifecycle
# ===========================================================================


@pytest.mark.unit
class TestConnectionLifecycle:
    def test_each_thread_gets_its_own_connection(self, log: AuditLog) -> None:
        """The per-thread cache is what keeps a threaded request handler working.

        Handing a second thread the first thread's connection trips SQLite's
        ``check_same_thread`` guard; ``record`` catches that and reports False,
        so the write is lost *silently* rather than crashing. No existing test
        writes from another thread, so collapsing the cache to one shared
        connection survived.
        """
        log.record(_record("evt-main"))
        results: list[object] = []

        def _write_from_thread() -> None:
            results.append(log.record(_record("evt-worker")))

        worker = threading.Thread(target=_write_from_thread)
        worker.start()
        worker.join(timeout=30)

        assert results == [True]
        assert log.lookup("evt-worker") is not None

    def test_close_clears_the_cache_so_the_log_is_reusable(self, tmp_path: Path) -> None:
        """``close()`` is documented idempotent; it must not poison the log.

        Closing the connections without clearing the cache leaves a closed
        handle behind, and the next ``record()`` fails on it — reported as
        False, per criterion 1.8, so the caller never learns the audit trail
        stopped. Reconnecting is the observable half of "idempotent".
        """
        audit = AuditLog(tmp_path / "audit.db")
        try:
            audit.record(_record("evt-before-close"))
            audit.close()
            audit.close()  # idempotent — must not raise

            assert audit.record(_record("evt-after-close")) is True
            assert audit.lookup("evt-after-close") is not None
            assert audit.lookup("evt-before-close") is not None
        finally:
            audit.close()
