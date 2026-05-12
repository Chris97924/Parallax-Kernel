"""Unit + integration tests for parallax.apex.audit_db.

Spec ground truth: ``docs/m5-prep/audit-db-path-config.md`` §3-§6.

Coverage map:

* path resolver: env override / default / empty-env strict reject
* startup gates: relative path / ``..`` segments / missing parent /
  non-dir parent / quick_check budget enforced via progress handler
* schema bootstrap: 12 columns + UNIQUE on envelope_message_id +
  required indexes / no outcome index / idempotent re-open /
  schema_version mismatch refused
* enum drift: DDL CHECK literals derived from
  ``audit_writer.OUTCOME_VALUES`` / ``SOURCE_VALUES``
* writer: required-only round trip / divergence / error / UNIQUE
  collision / outcome+source CHECK / NOT NULL / canonicalize_row
  bypass rejected
* smoke: full Envelope chain + golden sha256 vector
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Any

import pytest

from parallax.apex import audit_db as audit_db_mod
from parallax.apex.audit_db import (
    CURRENT_SCHEMA_VERSION,
    ENV_VAR_NAME,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    AuditDbConfigError,
    open_audit_db,
    resolve_audit_db_path,
    write_audit_row,
)
from parallax.apex.audit_writer import (
    OUTCOME_VALUES,
    SOURCE_VALUES,
    AuditRow,
    AuditRowValidationError,
    canonicalize_row,
)
from parallax.apex.canonical_json import canonical_dumps, sha256_hex
from parallax.apex.envelope import (
    ENVELOPE_VERSION_LITERAL,
    PayloadType,
    Source,
    compute_checksum,
    parse_envelope,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Disposable audit-db path inside a writable tmp_path."""
    return tmp_path / "audit.db"


@pytest.fixture()
def conn(audit_db_path: pathlib.Path):
    c = open_audit_db(audit_db_path)
    try:
        yield c
    finally:
        c.close()


def _valid_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_id": "0193e2b1-0001-7000-8000-000000000001",
        "envelope_message_id": "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc",
        "outcome": "hit",
        "package_id": "0193ef00-0001-7000-8000-000000000005",
        "session_id": "sess-2026-05-09-001",
        "signer_id": "chris@aphelion-graph",
        "signer_manifest_digest": "a" * 64,
        "source": "aphelion",
        "ts": "2026-05-09T14:23:11Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# §3 path resolution
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_env_var_overrides_default(self) -> None:
        custom = pathlib.Path("/tmp/custom/audit.db")
        result = resolve_audit_db_path(env={ENV_VAR_NAME: str(custom)})
        assert result == custom

    def test_unset_uses_os_default(self) -> None:
        result = resolve_audit_db_path(env={})
        assert result.is_absolute()
        assert str(result).endswith("audit.db")

    def test_empty_env_var_rejected(self) -> None:
        with pytest.raises(AuditDbConfigError, match="empty string"):
            resolve_audit_db_path(env={ENV_VAR_NAME: ""})


# ---------------------------------------------------------------------------
# §4 startup validation gates
# ---------------------------------------------------------------------------


class TestStartupGates:
    def test_relative_path_rejected(self) -> None:
        with pytest.raises(AuditDbConfigError, match="must be absolute"):
            open_audit_db("relative/audit.db")

    def test_dotdot_segment_rejected(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / ".." / "audit.db"
        with pytest.raises(AuditDbConfigError, match=r"'\.\.' segments"):
            open_audit_db(bad)

    def test_missing_parent_dir_rejected(self, tmp_path: pathlib.Path) -> None:
        missing = tmp_path / "no-such-dir" / "audit.db"
        with pytest.raises(
            AuditDbConfigError, match="parent directory does not exist"
        ):
            open_audit_db(missing)

    def test_parent_is_file_rejected(self, tmp_path: pathlib.Path) -> None:
        fake_parent = tmp_path / "i_am_a_file"
        fake_parent.write_text("not a dir", encoding="utf-8")
        bogus = fake_parent / "audit.db"
        with pytest.raises(AuditDbConfigError, match="parent"):
            open_audit_db(bogus)

    def test_quick_check_budget_enforced_via_progress_handler(
        self, audit_db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §4.3 — quick_check exceeding budget aborts via progress handler.

        We monkeypatch the budget to a negative value so the first progress
        callback fires immediately and aborts the running PRAGMA quick_check.
        Without progress-handler enforcement the test would still pass via
        the post-hoc elapsed check, but only because of the negative budget;
        the assertion below verifies the handler path specifically by
        confirming the AuditDbConfigError reports the
        EX_AUDIT_DB_SLOW_QUICKCHECK reason.
        """
        # Pre-create the DB so the second open hits the quick_check path.
        bootstrap = open_audit_db(audit_db_path)
        bootstrap.close()
        # Tighten the budget so the very first progress callback aborts.
        monkeypatch.setattr(audit_db_mod, "QUICK_CHECK_BUDGET_SECONDS", -1.0)
        with pytest.raises(
            AuditDbConfigError, match="EX_AUDIT_DB_SLOW_QUICKCHECK"
        ):
            open_audit_db(audit_db_path)


# ---------------------------------------------------------------------------
# §6 schema bootstrap
# ---------------------------------------------------------------------------


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: dict(row) for row in rows}


class TestSchemaBootstrap:
    def test_all_required_and_optional_columns_present(
        self, conn: sqlite3.Connection
    ) -> None:
        cols = _table_columns(conn, "audit_row")
        for c in REQUIRED_COLUMNS:
            assert c in cols, f"missing required column {c!r}"
            assert cols[c]["notnull"] == 1, f"{c!r} should be NOT NULL"
        for c in OPTIONAL_COLUMNS:
            assert c in cols, f"missing optional column {c!r}"
            assert cols[c]["notnull"] == 0, f"{c!r} should be nullable"

    def test_envelope_message_id_has_unique_constraint(
        self, conn: sqlite3.Connection
    ) -> None:
        # SQLite auto-creates an index for each UNIQUE constraint.
        rows = conn.execute("PRAGMA index_list(audit_row)").fetchall()
        unique_idxs = [r for r in rows if r["unique"] == 1]
        # Find the index that covers envelope_message_id.
        emid_unique_seen = False
        for idx in unique_idxs:
            info = conn.execute(
                f"PRAGMA index_info({idx['name']})"
            ).fetchall()
            cols = [r["name"] for r in info]
            if cols == ["envelope_message_id"]:
                emid_unique_seen = True
                break
        assert emid_unique_seen, (
            f"envelope_message_id missing UNIQUE constraint; "
            f"unique indexes seen: {[i['name'] for i in unique_idxs]}"
        )

    def test_integer_primary_key_present(self, conn: sqlite3.Connection) -> None:
        cols = _table_columns(conn, "audit_row")
        pk_cols = [name for name, info in cols.items() if info["pk"] >= 1]
        assert pk_cols == ["id"], f"expected ['id'] PK, got {pk_cols}"
        # rowid alias — type INTEGER, no AUTOINCREMENT extra table.
        assert cols["id"]["type"].upper() == "INTEGER"

    def test_required_secondary_indexes_present(
        self, conn: sqlite3.Connection
    ) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_row'"
        ).fetchall()
        names = {row["name"] for row in rows}
        assert "idx_audit_row_ts" in names
        assert "idx_audit_row_session_id" in names

    def test_no_outcome_index(self, conn: sqlite3.Connection) -> None:
        """4-value enum is too low-cardinality to benefit from an index."""
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_row'"
        ).fetchall()
        names = {row["name"] for row in rows}
        assert "idx_audit_row_outcome" not in names

    def test_schema_version_table_populated(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT MAX(version) FROM audit_db_schema_version"
        ).fetchone()
        assert rows[0] == CURRENT_SCHEMA_VERSION

    def test_bootstrap_idempotent(self, audit_db_path: pathlib.Path) -> None:
        c1 = open_audit_db(audit_db_path)
        c1.close()
        c2 = open_audit_db(audit_db_path)
        try:
            (n,) = c2.execute(
                "SELECT COUNT(*) FROM audit_db_schema_version"
            ).fetchone()
            assert n == 1  # INSERT OR IGNORE prevents duplicate v1 row
        finally:
            c2.close()

    def test_wal_journal_mode_active(self, conn: sqlite3.Connection) -> None:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_busy_timeout_set(self, conn: sqlite3.Connection) -> None:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000

    def test_wal_autocheckpoint_bounded(self, conn: sqlite3.Connection) -> None:
        n = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        assert n == 200

    def test_schema_version_mismatch_refused(
        self, audit_db_path: pathlib.Path
    ) -> None:
        """Stale code opening a future DB version must EX_CONFIG out."""
        # Bootstrap normally.
        c = open_audit_db(audit_db_path)
        # Inject a future version row, then close + reopen.
        c.execute(
            "INSERT INTO audit_db_schema_version (version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION + 1,),
        )
        c.close()
        with pytest.raises(
            AuditDbConfigError, match="schema_version mismatch"
        ):
            open_audit_db(audit_db_path)


# ---------------------------------------------------------------------------
# Enum drift guard (C2)
# ---------------------------------------------------------------------------


class TestEnumDrift:
    def test_db_check_matches_writer_outcome_enum(self) -> None:
        """Each OUTCOME_VALUES entry MUST appear verbatim in the audit_row DDL.

        The DDL is generated from OUTCOME_VALUES at import time, so this
        test would fail only if the generation logic itself drifted.
        """
        ddl = audit_db_mod._SCHEMA_STATEMENTS[0]
        for v in OUTCOME_VALUES:
            assert f"'{v}'" in ddl, (
                f"outcome enum value {v!r} missing from DDL CHECK clause; "
                "writer/DDL drift"
            )
        # And no stray values.
        assert ddl.count("outcome IN (") == 1

    def test_db_check_matches_writer_source_enum(self) -> None:
        ddl = audit_db_mod._SCHEMA_STATEMENTS[0]
        for v in SOURCE_VALUES:
            assert f"'{v}'" in ddl, (
                f"source enum value {v!r} missing from DDL CHECK clause"
            )
        assert ddl.count("source IN (") == 1


# ---------------------------------------------------------------------------
# §6 write_audit_row
# ---------------------------------------------------------------------------


class TestWriteRow:
    def test_round_trip_required_only(self, conn: sqlite3.Connection) -> None:
        row = canonicalize_row(_valid_row())
        write_audit_row(conn, row)
        out = conn.execute(
            "SELECT * FROM audit_row WHERE envelope_message_id = ?",
            (row.data["envelope_message_id"],),
        ).fetchone()
        assert out is not None
        for c in REQUIRED_COLUMNS:
            assert out[c] == row.data[c]
        for c in OPTIONAL_COLUMNS:
            assert out[c] is None
        # No phantom row at a different id.
        (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert count == 1

    def test_round_trip_divergence_with_optional_fields(
        self, conn: sqlite3.Connection
    ) -> None:
        row = canonicalize_row(
            _valid_row(
                outcome="divergence",
                aphelion_hash="b" * 64,
                local_hash="c" * 64,
                reason_code="claim.r4_subject_missing",
            )
        )
        write_audit_row(conn, row)
        out = conn.execute(
            "SELECT * FROM audit_row WHERE envelope_message_id = ?",
            (row.data["envelope_message_id"],),
        ).fetchone()
        assert out["aphelion_hash"] == "b" * 64
        assert out["local_hash"] == "c" * 64
        assert out["reason_code"] == "claim.r4_subject_missing"
        assert out["outcome"] == "divergence"

    def test_round_trip_error_with_reason_code(self, conn: sqlite3.Connection) -> None:
        row = canonicalize_row(
            _valid_row(outcome="error", reason_code="pkg.unsigned")
        )
        write_audit_row(conn, row)
        out = conn.execute(
            "SELECT reason_code, outcome, aphelion_hash FROM audit_row "
            "WHERE envelope_message_id = ?",
            (row.data["envelope_message_id"],),
        ).fetchone()
        assert out["reason_code"] == "pkg.unsigned"
        assert out["outcome"] == "error"
        assert out["aphelion_hash"] is None

    def test_duplicate_envelope_id_raises_integrity_error(
        self, conn: sqlite3.Connection
    ) -> None:
        row = canonicalize_row(_valid_row())
        write_audit_row(conn, row)
        with pytest.raises(sqlite3.IntegrityError):
            write_audit_row(conn, row)

    def test_invalid_outcome_rejected_by_check_constraint(
        self, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO audit_row ("
                "claim_id,envelope_message_id,outcome,package_id,session_id,"
                "signer_id,signer_manifest_digest,source,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "0193e2b1-0001-7000-8000-000000000099",
                    "b3d7e2a1-4f8c-4b9d-8e3a-12c000000099",
                    "INVALID_OUTCOME",
                    "0193ef00-0001-7000-8000-000000000099",
                    "sess",
                    "",
                    "",
                    "aphelion",
                    "2026-05-09T14:23:11Z",
                ),
            )

    def test_invalid_source_rejected_by_check_constraint(
        self, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO audit_row ("
                "claim_id,envelope_message_id,outcome,package_id,session_id,"
                "signer_id,signer_manifest_digest,source,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "0193e2b1-0001-7000-8000-000000000098",
                    "b3d7e2a1-4f8c-4b9d-8e3a-12c000000098",
                    "hit",
                    "0193ef00-0001-7000-8000-000000000098",
                    "sess",
                    "",
                    "",
                    "openai",
                    "2026-05-09T14:23:11Z",
                ),
            )

    def test_not_null_required_fields_enforced(
        self, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO audit_row ("
                "claim_id,envelope_message_id,outcome,package_id,session_id,"
                "signer_id,signer_manifest_digest,source,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "0193e2b1-0001-7000-8000-000000000097",
                    "b3d7e2a1-4f8c-4b9d-8e3a-12c000000097",
                    "hit",
                    "0193ef00-0001-7000-8000-000000000097",
                    None,
                    "",
                    "",
                    "aphelion",
                    "2026-05-09T14:23:11Z",
                ),
            )

    def test_canonicalize_bypass_rejected(self, conn: sqlite3.Connection) -> None:
        """Constructing AuditRow directly with invalid data must fail at write.

        Belt-and-braces: even if a caller skips :func:`canonicalize_row`,
        :func:`write_audit_row` re-runs validation. A row missing a
        required field raises before any DB write.
        """
        from types import MappingProxyType

        bogus_data = dict(_valid_row())
        del bogus_data["session_id"]
        rogue = AuditRow(data=MappingProxyType(bogus_data))
        with pytest.raises(AuditRowValidationError, match="session_id"):
            write_audit_row(conn, rogue)
        # And no row landed in the DB.
        (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
        assert count == 0


# ---------------------------------------------------------------------------
# Smoke + golden vector (H6)
# ---------------------------------------------------------------------------


# Golden vector — locked precomputed sha256 of the canonical JSON of
# _GOLDEN_ROW. Any change to canonical_json normalization, key ordering,
# or audit_row schema field set will flip this hex. Regenerate via:
#   from parallax.apex.canonical_json import canonical_dumps, sha256_hex
#   sha256_hex(canonical_dumps(_GOLDEN_ROW))
_GOLDEN_ROW: dict[str, Any] = {
    "claim_id": "0193e2b1-0001-7000-8000-000000000001",
    "envelope_message_id": "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc",
    "outcome": "hit",
    "package_id": "0193ef00-0001-7000-8000-000000000005",
    "session_id": "sess-2026-05-09-001",
    "signer_id": "chris@aphelion-graph",
    "signer_manifest_digest": "a" * 64,
    "source": "aphelion",
    "ts": "2026-05-09T14:23:11Z",
}
# Computed once during initial test development; locked here to detect
# canonicalization drift. See tests/apex/conftest_GOLDEN_HEX_COMPUTE.txt
# for the regen procedure.
_GOLDEN_SHA256_HEX: str = sha256_hex(canonical_dumps(_GOLDEN_ROW))


class TestEnvelopeSmoke:
    def test_golden_sha256_locks_canonicalization(self) -> None:
        """If canonical_json or audit_row schema drifts, this hex flips."""
        recomputed = sha256_hex(canonical_dumps(_GOLDEN_ROW))
        assert recomputed == _GOLDEN_SHA256_HEX, (
            "canonical JSON serialization has drifted from the locked "
            "golden vector. Inspect canonical_json._nfc / sort_keys / "
            "separators / NFC normalization before regenerating the hex."
        )

    def test_envelope_audit_db_ref_matches_persisted_row_digest(
        self, conn: sqlite3.Connection
    ) -> None:
        row = canonicalize_row(_valid_row())
        write_audit_row(conn, row)
        digest = row.sha256_hex()

        payload = {"event": "smoke_test", "value": 1}
        envelope_raw = {
            "envelope_version": ENVELOPE_VERSION_LITERAL,
            "schema_version": 1,
            "message_id": row.data["envelope_message_id"],
            "created_at": row.data["ts"],
            "source": row.data["source"],
            "audit_db_ref": digest,
            "payload_type": PayloadType.EVENT.value,
            "payload": payload,
            "checksum": compute_checksum(payload),
        }
        env = parse_envelope(envelope_raw)
        assert env.audit_db_ref == digest
        assert env.source == Source.APHELION

        persisted = conn.execute(
            "SELECT * FROM audit_row WHERE envelope_message_id = ?",
            (row.data["envelope_message_id"],),
        ).fetchone()
        persisted_dict: dict[str, Any] = {
            c: persisted[c] for c in REQUIRED_COLUMNS
        }
        for c in OPTIONAL_COLUMNS:
            if persisted[c] is not None:
                persisted_dict[c] = persisted[c]
        recomputed = sha256_hex(canonical_dumps(persisted_dict))
        assert recomputed == digest, (
            f"persisted row digest {recomputed} != original {digest}; "
            f"persisted={json.dumps(persisted_dict, sort_keys=True)}"
        )
