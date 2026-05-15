"""Tests for the wired AphelionReadAdapter (PR-D / Apex M5 entry).

Supersedes the M3-T1.2 stub tests. The adapter now runs Aphelion v0.3 R4
detection and emits an Apex M5 envelope + audit row; this file exercises
the happy path (NOT_FOUND / SUPERSESSION) plus the failure paths that
still surface as ``AphelionUnreachableError``.

Spec anchors:
  * ``docs/m5-prep/apex-m5-envelope-spec.md`` §2 + §3.1 + §4.1
  * ``docs/m5-prep/audit-db-path-config.md`` §6
  * ``Aphelion-Graph/spec/v0.3-claim-semantics.md`` §6
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

import pytest

from parallax.apex.audit_db import open_audit_db
from parallax.apex.envelope import Envelope, PayloadType, Source, parse_envelope
from parallax.router.aphelion_adapter import (
    AphelionReadAdapter,
    AphelionUnreachableError,
)
from parallax.router.contracts import QueryRequest
from parallax.router.ports import QueryPort
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


_PACKAGE_ID = "01963f7d-7000-7000-8000-0000000000aa"


def _claim(
    *,
    claim_id: str = "01963f7d-7000-7000-8000-000000000001",
    subject: str = "subject:foo",
    polarity: str = "affirm",
    valid_from: str | None = None,
    valid_until: str | None = None,
    supersedes: list[str] | None = None,
    package_id: str = _PACKAGE_ID,
) -> dict[str, Any]:
    """Build a minimal v0.3-valid claim frontmatter with package metadata.

    ``package_id`` is opaque to the v0.3 validator but carried through so the
    adapter can populate the audit row's required ``package_id`` field
    (M6/M7 ingest will set this for real; PR-D tests inject it directly).
    """
    out: dict[str, Any] = {
        "claim_id": claim_id,
        "subject": subject,
        "polarity": polarity,
        "package_id": package_id,
    }
    if valid_from is not None:
        out["valid_from"] = valid_from
    if valid_until is not None:
        out["valid_until"] = valid_until
    if supersedes is not None:
        out["supersedes"] = supersedes
    return out


def _request(
    *,
    user_id: str = "u1",
    q: str = "subject:foo",
    query_type: QueryType = QueryType.RECENT_CONTEXT,
) -> QueryRequest:
    return QueryRequest(query_type=query_type, user_id=user_id, q=q)


@pytest.fixture()
def audit_conn_provider(
    tmp_path: pathlib.Path,
) -> Iterator[Callable[[], sqlite3.Connection]]:
    """Real audit-db connection provider backed by a disposable tmp audit.db.

    Mirrors what :func:`parallax.apex.audit_db.get_thread_local_audit_conn`
    hands the adapter in production: a zero-arg callable returning a working
    :class:`sqlite3.Connection` with the ``audit_row`` schema applied.
    ``validate=False`` skips the boot-time quick_check / write probe (the
    server lifespan owns that gate); the WAL pragmas + schema still apply.
    """
    conn = open_audit_db(tmp_path / "audit.db", validate=False)
    try:
        yield lambda: conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Construction + Protocol conformance
# ---------------------------------------------------------------------------


def test_constructor_defaults(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    adapter = AphelionReadAdapter(audit_conn_provider=audit_conn_provider)
    assert adapter._timeout_ms == 100.0
    assert adapter._package_dir is None
    assert adapter.last_envelope is None
    assert adapter.last_audit_row is None


def test_constructor_custom_args(
    tmp_path: Any, audit_conn_provider: Callable[[], sqlite3.Connection]
) -> None:
    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider,
        package_dir=tmp_path,
        timeout_ms=50.0,
    )
    assert adapter._package_dir == tmp_path
    assert adapter._timeout_ms == 50.0


def test_conforms_to_query_port_protocol(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    adapter = AphelionReadAdapter(audit_conn_provider=audit_conn_provider)
    assert isinstance(adapter, QueryPort)


# ---------------------------------------------------------------------------
# AphelionUnreachableError
# ---------------------------------------------------------------------------


def test_unreachable_error_reason_attribute() -> None:
    err = AphelionUnreachableError("timeout")
    assert err.reason == "timeout"
    assert "timeout" in str(err)


@pytest.mark.parametrize(
    "reason",
    [
        "timeout",
        "connection_error",
        "claim_schema_error",
        "envelope_checksum_mismatch",
        "audit_row_invalid",
        "audit_db_write_failed",
        "audit_db_usage_error",
        "audit_db_integrity_error",
    ],
)
def test_unreachable_error_reason_values(reason: str) -> None:
    err = AphelionUnreachableError(reason)
    assert err.reason == reason


# ---------------------------------------------------------------------------
# NOT_FOUND happy path (default empty loader)
# ---------------------------------------------------------------------------


def test_query_returns_empty_evidence_on_not_found(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """Default empty loader → NOT_FOUND, empty hits, no envelope emission.

    PR-D scope-cut: no real ``package_id`` to anchor an audit row, so the
    adapter intentionally skips envelope emission for NOT_FOUND. M6/M7
    ingest pipeline will revisit this once package metadata is available.
    """
    adapter = AphelionReadAdapter(audit_conn_provider=audit_conn_provider)
    evidence = adapter.query(_request())

    assert evidence.hits == ()
    assert evidence.stages == ("aphelion_v03_r4",)
    assert "conflict_class=not_found" in evidence.notes[0]
    assert adapter.last_envelope is None
    assert adapter.last_audit_row is None


# ---------------------------------------------------------------------------
# R4 supersession path emits envelope + audit row
# ---------------------------------------------------------------------------


def test_r4_supersession_surfaces_active_claim_and_emits_envelope(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """Newer claim with `supersedes: [old]` → primary == newer; envelope emitted."""
    older = _claim(claim_id="01963f7d-7000-7000-8000-000000000010")
    newer = _claim(
        claim_id="01963f7d-7000-7000-8000-000000000011",
        supersedes=["01963f7d-7000-7000-8000-000000000010"],
    )

    def loader(_req: QueryRequest) -> Iterable[Mapping[str, Any]]:
        return [older, newer]

    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider, claim_loader=loader
    )
    evidence = adapter.query(_request())

    env = adapter.last_envelope
    assert isinstance(env, Envelope)
    assert env.payload_type is PayloadType.QUERY_RESULT
    assert env.source is Source.APHELION
    assert env.envelope_version == "0.1"
    assert env.schema_version == 1
    assert env.payload["conflict_class"] == "supersession"
    assert env.payload["primary_claim_id"] == newer["claim_id"]
    assert env.payload["superseded_count"] == 1
    assert evidence.hits[0]["id"] == newer["claim_id"]


def test_audit_db_ref_matches_canonical_sha256(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """envelope.audit_db_ref MUST equal the canonical sha256 of the audit row."""
    older = _claim(claim_id="01963f7d-7000-7000-8000-000000000010")
    newer = _claim(
        claim_id="01963f7d-7000-7000-8000-000000000011",
        supersedes=["01963f7d-7000-7000-8000-000000000010"],
    )
    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider,
        claim_loader=lambda _r: [older, newer],
    )
    adapter.query(_request())

    env = adapter.last_envelope
    row = adapter.last_audit_row
    assert env is not None and row is not None
    assert _SHA256_HEX_RE.match(env.audit_db_ref) is not None
    assert env.audit_db_ref == row.sha256_hex()


def test_audit_row_ts_matches_envelope_created_at(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """Audit row ``ts`` MUST equal envelope ``created_at`` (audit-db-path-config.md §6.1).

    Regression for Codex P2 review on PR #51: the previous implementation
    populated ``audit_row.ts`` from ``result.used_query_time`` (reader time)
    and ``envelope.created_at`` from a freshly computed timestamp, which can
    diverge when a non-default ``query_time`` is supplied or a second boundary
    is crossed between the two computations.
    """
    older = _claim(claim_id="01963f7d-7000-7000-8000-000000000010")
    newer = _claim(
        claim_id="01963f7d-7000-7000-8000-000000000011",
        supersedes=["01963f7d-7000-7000-8000-000000000010"],
    )
    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider,
        claim_loader=lambda _r: [older, newer],
    )
    adapter.query(_request())

    env = adapter.last_envelope
    row = adapter.last_audit_row
    assert env is not None and row is not None
    assert row.data["ts"] == env.created_at


def test_envelope_round_trips_through_parse_envelope(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """Emitted envelope passes parse_envelope without raising."""
    older = _claim(claim_id="01963f7d-7000-7000-8000-000000000010")
    newer = _claim(
        claim_id="01963f7d-7000-7000-8000-000000000011",
        supersedes=["01963f7d-7000-7000-8000-000000000010"],
    )
    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider,
        claim_loader=lambda _r: [older, newer],
    )
    adapter.query(_request())

    env = adapter.last_envelope
    assert env is not None
    raw = {
        "envelope_version": env.envelope_version,
        "schema_version": env.schema_version,
        "message_id": env.message_id,
        "created_at": env.created_at,
        "source": env.source.value,
        "audit_db_ref": env.audit_db_ref,
        "payload_type": env.payload_type.value,
        "payload": dict(env.payload),
        "checksum": env.checksum,
    }
    reparsed = parse_envelope(raw)
    assert reparsed.checksum == env.checksum


# ---------------------------------------------------------------------------
# Failure path — bad claim surfaces as AphelionUnreachableError
# ---------------------------------------------------------------------------


def test_schema_error_surfaces_as_unreachable(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """Validator rejects a reserved field → AphelionUnreachableError(claim_schema_error).

    ``conflict_class`` is a reserved derivation field (spec §7) that MUST NOT
    appear in frontmatter; the v0.3 validator raises ``SchemaError`` and the
    adapter surfaces it as the M3 contract error.
    """

    def bad_loader(_req: QueryRequest) -> Iterable[Mapping[str, Any]]:
        return [
            {
                "claim_id": "01963f7d-7000-7000-8000-000000000050",
                "subject": "subject:foo",
                "polarity": "affirm",
                "conflict_class": "ambiguity",
            }
        ]

    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider, claim_loader=bad_loader
    )
    with pytest.raises(AphelionUnreachableError) as excinfo:
        adapter.query(_request())
    assert excinfo.value.reason == "claim_schema_error"


def test_loader_runtime_error_surfaces_as_unreachable(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """A claim_loader that raises a non-contract exception must be wrapped.

    Without wrapping, DualReadRouter classifies the failure as ``primary_only``
    and loses the ``aphelion_unreachable`` signal expected for secondary
    outages (filesystem/network errors once M6/M7 ingest lands).
    """

    def crashing_loader(_req: QueryRequest) -> Iterable[Mapping[str, Any]]:
        raise RuntimeError("synthetic loader I/O failure")

    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider, claim_loader=crashing_loader
    )
    with pytest.raises(AphelionUnreachableError) as excinfo:
        adapter.query(_request())
    assert excinfo.value.reason == "claim_loader_error"


def test_loader_unreachable_passthrough_preserves_reason(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """Loader-side AphelionUnreachableError keeps its reason verbatim."""

    def precise_loader(_req: QueryRequest) -> Iterable[Mapping[str, Any]]:
        raise AphelionUnreachableError("unsafe_archive")

    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider, claim_loader=precise_loader
    )
    with pytest.raises(AphelionUnreachableError) as excinfo:
        adapter.query(_request())
    assert excinfo.value.reason == "unsafe_archive"


def test_failed_query_clears_last_envelope_and_last_audit_row(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """A failing query MUST reset cached envelope/audit state.

    Regression for Codex P2 review on PR #51: the previous implementation
    only reset ``last_envelope`` / ``last_audit_row`` on the explicit
    primary-None branch, so any exception raised before that branch (e.g.
    schema validation failure) left stale values from a prior successful
    query visible to callers.
    """
    older = _claim(claim_id="01963f7d-7000-7000-8000-000000000010")
    newer = _claim(
        claim_id="01963f7d-7000-7000-8000-000000000011",
        supersedes=["01963f7d-7000-7000-8000-000000000010"],
    )

    state: dict[str, list[Mapping[str, Any]]] = {
        "claims": [older, newer],
    }

    def loader(_req: QueryRequest) -> Iterable[Mapping[str, Any]]:
        return state["claims"]

    adapter = AphelionReadAdapter(
        audit_conn_provider=audit_conn_provider, claim_loader=loader
    )

    # First call: successful supersession query populates the cache.
    adapter.query(_request())
    assert adapter.last_envelope is not None
    assert adapter.last_audit_row is not None

    # Second call on the SAME adapter with a v0.3-invalid claim must clear
    # the cached values before the SchemaError surfaces as the M3 contract
    # error — never leave stale envelope/audit data behind.
    state["claims"] = [
        {
            "claim_id": "01963f7d-7000-7000-8000-000000000050",
            "subject": "subject:foo",
            "polarity": "affirm",
            "conflict_class": "ambiguity",  # reserved derivation field
        }
    ]
    with pytest.raises(AphelionUnreachableError) as excinfo:
        adapter.query(_request())
    assert excinfo.value.reason == "claim_schema_error"
    assert adapter.last_envelope is None
    assert adapter.last_audit_row is None
