"""Apex M5 audit.db persistence — write-path / fail-closed / write-order tests.

Covers the gap closed by the M5 audit-db-persistence follow-up PR: before
this work ``AphelionReadAdapter.query()`` built the audit row + computed
``audit_db_ref`` but never INSERTed the row to disk (``open_audit_db`` had
zero callers). These tests pin the new behaviour:

  1. real claim hit → exactly one row committed to audit.db, and the
     envelope's ``audit_db_ref`` equals that row's canonical sha256
  2. ``PARALLAX_AUDIT_DB_PATH`` unset → server boot fails (lifespan raises
     ``AuditDbConfigError``; ``parallax serve`` CLI preflight returns
     ``EX_CONFIG`` 78)
  3. ``write_audit_row`` failure is fail-closed → ``AphelionUnreachableError``,
     no envelope emitted, ``DualReadRouter`` falls back to primary
  4. miss path never touches the audit-conn provider
  5. ``get_thread_local_audit_conn`` reuses one connection per thread
  6. duplicate ``envelope_message_id`` → ``AphelionUnreachableError`` with
     reason ``audit_db_integrity_error``

Spec anchors:
  * ``docs/m5-prep/apex-m5-envelope-spec.md`` §8.1 — write-order invariant
  * ``docs/m5-prep/audit-db-path-config.md`` §4 — boot-time validation gates
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import types
import uuid
from typing import Any

import pytest
from fastapi import FastAPI

import parallax.apex.audit_db as audit_db_mod
from parallax.apex.audit_db import AuditDbConfigError, open_audit_db
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.aphelion_adapter import (
    AphelionReadAdapter,
    AphelionUnreachableError,
)
from parallax.router.contracts import QueryRequest
from parallax.router.dual_read import DualReadRouter
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PACKAGE_ID = "01963f7d-7000-7000-8000-0000000000aa"


def _claim(*, claim_id: str, supersedes: list[str] | None = None) -> dict[str, Any]:
    """Minimal v0.3-valid claim frontmatter for the supersession fixture."""
    out: dict[str, Any] = {
        "claim_id": claim_id,
        "subject": "subject:foo",
        "polarity": "affirm",
        "package_id": _PACKAGE_ID,
    }
    if supersedes is not None:
        out["supersedes"] = supersedes
    return out


def _supersession_claims() -> list[dict[str, Any]]:
    """Two-claim set where ``newer`` supersedes ``older`` → R4 emits an envelope."""
    older = _claim(claim_id="01963f7d-7000-7000-8000-000000000010")
    newer = _claim(
        claim_id="01963f7d-7000-7000-8000-000000000011",
        supersedes=["01963f7d-7000-7000-8000-000000000010"],
    )
    return [older, newer]


def _request() -> QueryRequest:
    return QueryRequest(
        query_type=QueryType.RECENT_CONTEXT, user_id="u1", q="subject:foo"
    )


class _StubPrimary:
    """Minimal synchronous QueryPort stub for the primary side."""

    def __init__(self) -> None:
        self.call_count = 0

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        self.call_count += 1
        return RetrievalEvidence(
            hits=({"id": "primary-hit", "kind": "memory", "score": 1.0},),
            stages=("primary",),
        )


class _FailingConn:
    """Fake audit-db connection whose first statement raises.

    Drives the ``AuditDbWriteError`` path inside ``write_audit_row`` — the
    ``BEGIN IMMEDIATE`` execute fails, which ``write_audit_row`` translates
    to ``AuditDbWriteError``.
    """

    in_transaction = False

    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("synthetic disk I/O error")


# ---------------------------------------------------------------------------
# 1. Real claim hit persists exactly one row; audit_db_ref matches
# ---------------------------------------------------------------------------


def test_real_hit_persists_exactly_one_row_and_ref_matches(tmp_path: Any) -> None:
    """A real R4 hit commits exactly one audit row; envelope.audit_db_ref
    hashes that committed row."""
    audit_db_file = tmp_path / "audit.db"
    conn = open_audit_db(audit_db_file, validate=False)
    try:
        adapter = AphelionReadAdapter(
            audit_conn_provider=lambda: conn,
            claim_loader=lambda _r: _supersession_claims(),
        )
        adapter.query(_request())

        env = adapter.last_envelope
        row = adapter.last_audit_row
        assert env is not None and row is not None
        # The envelope's audit_db_ref hashes the row that was written.
        assert env.audit_db_ref == row.sha256_hex()
    finally:
        conn.close()

    # Re-open with a SEPARATE connection: proves the row is committed to disk,
    # not merely sitting in an uncommitted transaction.
    verify = sqlite3.connect(audit_db_file)
    try:
        rows = verify.execute(
            "SELECT envelope_message_id, outcome, source FROM audit_row"
        ).fetchall()
    finally:
        verify.close()
    assert len(rows) == 1
    assert rows[0][0] == env.message_id
    assert rows[0][1] == "hit"
    assert rows[0][2] == "aphelion"


# ---------------------------------------------------------------------------
# 2. Boot fails when PARALLAX_AUDIT_DB_PATH is unset
# ---------------------------------------------------------------------------


def test_lifespan_boot_fails_when_audit_path_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """parallax_lifespan startup raises AuditDbConfigError when the audit
    path is unset — the server refuses to boot with a broken audit path (§4)."""
    from parallax.server.lifespan import parallax_lifespan

    monkeypatch.delenv("PARALLAX_AUDIT_DB_PATH", raising=False)
    app = FastAPI()

    async def _run_lifespan() -> None:
        # Startup raises AuditDbConfigError before the yield, so __aenter__
        # raises and the `with` body is never entered (no __aexit__ needed).
        async with parallax_lifespan(app):
            pass

    with pytest.raises(AuditDbConfigError):
        asyncio.run(_run_lifespan())


def test_cli_serve_preflight_returns_ex_config_when_audit_path_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``parallax serve`` runs an audit-db preflight before uvicorn binds and
    returns EX_CONFIG (78) on a broken audit path — the deterministic exit
    code uvicorn would otherwise swallow into a non-78 exit."""
    monkeypatch.delenv("PARALLAX_AUDIT_DB_PATH", raising=False)
    # uvicorn is not a test dependency; inject a stub so _cmd_serve reaches the
    # audit-db preflight instead of bailing out early on ImportError.
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda *a, **k: pytest.fail(  # type: ignore[attr-defined]
        "uvicorn.run must not be reached when the audit-db preflight fails"
    )
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    from parallax.cli import _cmd_serve

    rc = _cmd_serve(host="127.0.0.1", port=8765, log_level="info", reload=False)
    assert rc == 78


def test_cli_serve_preflight_returns_ex_config_on_raw_sqlite_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """``_cmd_serve`` returns EX_CONFIG (78) when ``open_audit_db(validate=True)``
    raises a raw ``sqlite3.Error`` (not just ``AuditDbConfigError``).

    Codex 2026-05-15 round-2 P2: the preflight previously only caught
    ``AuditDbConfigError``, but ``open_audit_db`` wraps the initial
    ``sqlite3.connect()`` in that type and lets the subsequent PRAGMA /
    schema-apply steps re-raise raw ``sqlite3.Error`` subclasses. A
    readonly / corrupt / partially-locked DB therefore terminated with an
    uncaught traceback instead of the deterministic EX_CONFIG promise.
    """
    audit_path = tmp_path / "audit.db"
    monkeypatch.setenv("PARALLAX_AUDIT_DB_PATH", str(audit_path))

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda *a, **k: pytest.fail(  # type: ignore[attr-defined]
        "uvicorn.run must not be reached when audit-db preflight fails"
    )
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    # Patch open_audit_db to raise a bare sqlite3.OperationalError so we
    # hit the new except branch (not the AuditDbConfigError branch).
    def _raise_op_error(path: Any, *, validate: bool = False) -> Any:
        raise sqlite3.OperationalError("synthetic: PRAGMA quick_check failed")

    monkeypatch.setattr(audit_db_mod, "open_audit_db", _raise_op_error)

    from parallax.cli import _cmd_serve

    rc = _cmd_serve(host="127.0.0.1", port=8765, log_level="info", reload=False)
    assert rc == 78, (
        f"raw sqlite3.OperationalError from open_audit_db must yield "
        f"EX_CONFIG (78), got {rc}"
    )


# ---------------------------------------------------------------------------
# 3. write_audit_row failure is fail-closed
# ---------------------------------------------------------------------------


def test_write_failure_is_fail_closed_at_adapter() -> None:
    """A write_audit_row failure surfaces as AphelionUnreachableError and the
    envelope is never emitted (write-order invariant — §8.1)."""
    adapter = AphelionReadAdapter(
        # _FailingConn is a duck-typed sqlite3.Connection stand-in (zero-arg
        # callable returning an object with .in_transaction + .execute).
        audit_conn_provider=_FailingConn,  # type: ignore[arg-type]
        claim_loader=lambda _r: _supersession_claims(),
    )
    with pytest.raises(AphelionUnreachableError) as excinfo:
        adapter.query(_request())
    assert excinfo.value.reason == "audit_db_write_failed"
    # No envelope when the row was not committed.
    assert adapter.last_envelope is None
    assert adapter.last_audit_row is None


def test_write_failure_via_dual_read_router_falls_back_to_primary() -> None:
    """Through DualReadRouter, an audit write failure classifies as
    aphelion_unreachable and the primary result is still served."""
    primary = _StubPrimary()
    secondary = AphelionReadAdapter(
        # _FailingConn: duck-typed sqlite3.Connection stand-in (see above).
        audit_conn_provider=_FailingConn,  # type: ignore[arg-type]
        claim_loader=lambda _r: _supersession_claims(),
    )
    result = DualReadRouter(primary=primary, secondary=secondary).query(
        _request(), dual_read_override=True
    )
    assert result.outcome == "aphelion_unreachable"
    assert result.aphelion_unreachable_reason == "audit_db_write_failed"
    # Primary still served — fail-closed in the canonical direction.
    assert primary.call_count == 1
    assert result.primary.hits[0]["id"] == "primary-hit"


# ---------------------------------------------------------------------------
# 4. Miss path never touches the audit-conn provider
# ---------------------------------------------------------------------------


def test_miss_path_never_calls_audit_conn_provider(tmp_path: Any) -> None:
    """An empty claim loader → R4 NOT_FOUND → query early-returns before the
    audit-write block, so the provider is never called and nothing is written."""
    conn = open_audit_db(tmp_path / "audit.db", validate=False)
    provider_calls = 0

    def _spy_provider() -> sqlite3.Connection:
        nonlocal provider_calls
        provider_calls += 1
        return conn

    try:
        adapter = AphelionReadAdapter(
            audit_conn_provider=_spy_provider,
            claim_loader=lambda _r: [],
        )
        evidence = adapter.query(_request())
        assert evidence.hits == ()
        assert adapter.last_envelope is None
        assert provider_calls == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. Thread-local connection is reused within a thread
# ---------------------------------------------------------------------------


def test_get_thread_local_audit_conn_reused_within_thread(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two calls on the same thread return the same connection; open_audit_db
    is invoked exactly once, with validate=False."""
    # Fresh thread-local so a connection cached by an earlier test on this
    # thread cannot mask the open-count assertion.
    monkeypatch.setattr(audit_db_mod, "_thread_local", threading.local())

    open_calls: list[tuple[Any, bool]] = []
    real_open = audit_db_mod.open_audit_db

    def _spy_open(path: Any, *, validate: bool = True) -> sqlite3.Connection:
        open_calls.append((path, validate))
        return real_open(path, validate=validate)

    monkeypatch.setattr(audit_db_mod, "open_audit_db", _spy_open)

    p = tmp_path / "audit.db"
    c1 = audit_db_mod.get_thread_local_audit_conn(p)
    c2 = audit_db_mod.get_thread_local_audit_conn(p)
    try:
        assert c1 is c2
        assert len(open_calls) == 1
        assert open_calls[0][1] is False  # opened with validate=False
    finally:
        c1.close()


# ---------------------------------------------------------------------------
# 6. Duplicate envelope_message_id → integrity error
# ---------------------------------------------------------------------------


def test_duplicate_envelope_message_id_surfaces_integrity_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writes with the same envelope_message_id hit the audit_row UNIQUE
    constraint → AphelionUnreachableError(reason='audit_db_integrity_error')."""
    import parallax.router.aphelion_adapter as adapter_mod

    # Pin uuid4 so both queries build the SAME envelope_message_id and the
    # second INSERT collides on the UNIQUE constraint. The literal is a valid
    # UUID v4 (audit_writer requires envelope_message_id to be v4).
    fixed = uuid.UUID("b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc")
    monkeypatch.setattr(adapter_mod.uuid, "uuid4", lambda: fixed)

    conn = open_audit_db(tmp_path / "audit.db", validate=False)
    try:
        adapter = AphelionReadAdapter(
            audit_conn_provider=lambda: conn,
            claim_loader=lambda _r: _supersession_claims(),
        )
        # First query commits the row.
        adapter.query(_request())
        assert adapter.last_envelope is not None

        # Second query rebuilds the same envelope_message_id → UNIQUE collision.
        with pytest.raises(AphelionUnreachableError) as excinfo:
            adapter.query(_request())
        assert excinfo.value.reason == "audit_db_integrity_error"
        # Fail-closed: the failed second query leaves no envelope behind.
        assert adapter.last_envelope is None
        assert adapter.last_audit_row is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. Envelope assembly failure AFTER the row is committed (spec §8.1)
# ---------------------------------------------------------------------------


def test_envelope_parse_failure_after_commit_leaves_row_on_disk_no_envelope(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §8.1: if envelope assembly fails AFTER the audit row is committed,
    the row stays on disk (recoverable by replay) but no envelope is emitted
    and the adapter fails closed."""
    import parallax.router.aphelion_adapter as adapter_mod

    def _boom(_envelope_dict: object) -> object:
        raise ValueError("synthetic parse_envelope failure")

    monkeypatch.setattr(adapter_mod, "parse_envelope", _boom)

    conn = open_audit_db(tmp_path / "audit.db", validate=False)
    try:
        adapter = AphelionReadAdapter(
            audit_conn_provider=lambda: conn,
            claim_loader=lambda _r: _supersession_claims(),
        )
        with pytest.raises(AphelionUnreachableError) as excinfo:
            adapter.query(_request())
        assert excinfo.value.reason == "envelope_checksum_mismatch"
        # Fail-closed: no envelope state leaked to the caller.
        assert adapter.last_envelope is None
        assert adapter.last_audit_row is None
        # ...but the audit row WAS committed before the envelope step — §8.1's
        # acceptable "row on disk, no envelope" mode, recoverable by replay.
        assert conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 10. Phase-4 critic finding: assert_audit_row_committed must be inside the
#     total fence so a write-order violation does not silently misclassify
#     as "primary_only" via the unwrapped-exception path.
# ---------------------------------------------------------------------------


def test_assert_audit_row_committed_violation_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """If ``assert_audit_row_committed`` ever raises ``AuditWriteOrderViolation``,
    it MUST be caught and re-raised as ``AphelionUnreachableError`` — not
    leak as an unexpected exception that DualReadRouter would classify as
    ``primary_only``.

    Practically: write_audit_row succeeds (so the real bool ``committed``
    is True and the guard would pass), but we monkeypatch the guard itself
    to force a violation. This proves the fence is in place independent of
    the bool wiring.
    """
    from parallax.apex.audit_writer import AuditWriteOrderViolation
    from parallax.router import aphelion_adapter as adapter_mod

    conn = open_audit_db(tmp_path / "audit.db", validate=False)

    def _exploding_guard(_committed: bool) -> None:
        raise AuditWriteOrderViolation("synthetic guard failure")

    monkeypatch.setattr(
        adapter_mod, "assert_audit_row_committed", _exploding_guard
    )

    adapter = AphelionReadAdapter(
        audit_conn_provider=lambda: conn,
        claim_loader=lambda _r: _supersession_claims(),
    )
    try:
        with pytest.raises(AphelionUnreachableError) as excinfo:
            adapter.query(_request())
        assert excinfo.value.reason == "audit_write_order_violation"
        # No envelope leaked.
        assert adapter.last_envelope is None
        assert adapter.last_audit_row is None
        # The audit row was actually committed (the guard fires AFTER the
        # write succeeds in the new ordering), so the row IS on disk —
        # this is the §8.1-compatible "row committed, envelope abandoned"
        # mode. Replay can recover from it; primary_only misclassification
        # cannot.
        assert conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()[0] == 1
    finally:
        conn.close()


# The DualReadRouter end-to-end version of the above is left implicit:
# - test_write_failure_via_dual_read_router_falls_back_to_primary already
#   proves that DualReadRouter classifies any AphelionUnreachableError from
#   the secondary as ``aphelion_unreachable`` (not ``primary_only``).
# - test_assert_audit_row_committed_violation_is_fail_closed above proves
#   the adapter wraps AuditWriteOrderViolation in AphelionUnreachableError.
# The composition is therefore covered; adding a real DualReadRouter test
# here would require a thread-safe conn fixture and adds no semantic
# coverage beyond what the existing pair already provides.
