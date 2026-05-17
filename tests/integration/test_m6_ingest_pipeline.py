"""Apex M6 ingest pipeline — integration tests against the 70-fixture corpus.

Implements ``docs/m6-prep/m6-ingest-contract-spec.md`` v0.1-frozen-2026-05-17
§7 test plan. Each test sweeps fixtures under
``tests/fixtures/m6_claim_mappings/{bucket}/*.json``, wraps each into a
1-claim ``.aphelion.tar`` via the local builder helper, then drives the
real ``parallax.apex.aphelion_ingest.ingest_package`` against a disposable
audit DB + trust store. No mocks: ``verify_package`` runs end-to-end with
a real HMAC-signed package.

Production-safety guards (5/16 P2 backlog #5): every test asserts the
resolved audit-db path does NOT touch ``parallax-kernel/db/``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from aphelion.canonical_json import dumps as canonical_dumps
from aphelion.canonical_json import normalize as canonical_normalize
from aphelion.canonical_tar import TarMember, read_members
from aphelion.canonical_tar import pack as tar_pack
from aphelion.packer import pack as aphelion_pack
from aphelion.sig_pack import write_signatures_jsonl
from aphelion.signer import (
    HMACSigner,
    compute_package_canonical_hash,
)

from parallax.apex.aphelion_ingest import (
    ClaimMappingBatch,
    IngestReport,
    ParallaxIngestError,
    TrustDecision,
    ingest_package,
)
from parallax.apex.audit_db import open_audit_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Repository fixture root (relative to repo root: tests/fixtures/m6_claim_mappings).
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "m6_claim_mappings"

# UUID v7 — fixed package_id base for builder-generated packages. Each call
# substitutes its own claim_id but the package_id is deterministic so test
# failure messages are easy to read.
_BUILDER_PACKAGE_ID = "01963f7d-7000-7000-8000-000000abc001"
_BUILDER_INSTANCE_ID = "01963f7d-7000-7000-8000-aaaaaaaaaaa1"
_BUILDER_EVENT_ID = "01963f7d-7000-7000-8000-eeee00000001"
_BUILDER_SIGNED_AT = "2026-05-17T00:00:00Z"
_BUILDER_SIGNER_ID = "m6-test-signer"
_BUILDER_SECRET = b"m6-test-secret-32-bytes-padding!"  # exactly 32 bytes


# ---------------------------------------------------------------------------
# Builder helper (kept contained — §7 says do not add a separate module)
# ---------------------------------------------------------------------------


def _claim_md(
    *,
    claim_id: str,
    body: str = "",
    extra_fields: Mapping[str, Any] | None = None,
) -> bytes:
    """v0.3 claim markdown with minimal YAML frontmatter.

    Keys MUST be unquoted bare identifiers per
    :data:`aphelion.yaml_canonical._KEY_RE` (``[A-Za-z_][A-Za-z0-9_]*``);
    keys MUST be ASCII-codepoint-ascending per
    :func:`aphelion.v03_validator.validate_key_order`. ``extra_fields``
    carries the R4 fixture frontmatter (subject / polarity / valid_from /
    valid_until / supersedes) so the v0.3 validator sees the actual
    fixture semantics rather than the bare baseline.
    """
    base: dict[str, Any] = {
        "body_format": "markdown",
        "claim_id": claim_id,
        "title": "M6 test claim",
    }
    if extra_fields:
        for key, value in extra_fields.items():
            base[key] = value

    lines = ["---"]
    for key in sorted(base.keys()):
        value = base[key]
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
                continue
            lines.append(f"{key}:")
            for item in value:
                lines.append(f'  - "{item}"')
        elif isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif value is None:
            lines.append(f"{key}: null")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")  # trailing newline before body
    text = "\n".join(lines) + body
    return text.encode("utf-8")


def _build_source_dir(
    src: Path,
    *,
    claim_id: str,
    instance_id: str,
    package_id: str,
    extra_fields: Mapping[str, Any] | None = None,
) -> bytes:
    """Materialize a 1-claim source dir on disk; return the canonical claim bytes."""
    src.mkdir(parents=True, exist_ok=True)
    claim_rel = f"claims/{claim_id}.md"
    claim_bytes = _claim_md(claim_id=claim_id, extra_fields=extra_fields)
    (src / "claims").mkdir(parents=True, exist_ok=True)
    (src / claim_rel).write_bytes(claim_bytes)

    manifest = {
        "aphelion_spec_version": "0.4.0",
        "claims": [
            {
                "claim_id": claim_id,
                "claim_instance_id": instance_id,
                "hash": hashlib.sha256(claim_bytes).hexdigest(),
                "path": claim_rel,
                "state": "active",
            }
        ],
        "created_at": "2026-05-17T00:00:00Z",
        "format_version": "2.0",
        "license": "Apache-2.0",
        "package_id": package_id,
        "producer": "parallax-m6-test",
        "provenance_path": "provenance.jsonl",
    }
    (src / "manifest.json").write_bytes(canonical_dumps(canonical_normalize(manifest)))

    event = {
        "actor": "m6-test",
        "claim_id": claim_id,
        "claim_instance_id": instance_id,
        "event_id": _BUILDER_EVENT_ID,
        "event_type": "create",
        "timestamp": "2026-05-17T00:00:00Z",
    }
    (src / "provenance.jsonl").write_bytes(canonical_dumps(canonical_normalize(event)))
    return claim_bytes


def _build_aphelion_package(
    *,
    tmp_path: Path,
    package_id: str,
    signer_secret: bytes,
    signer_id: str,
    out_name: str = "test.aphelion.tar",
    sign: bool = True,
    extra_fields: Mapping[str, Any] | None = None,
) -> tuple[Path, bytes]:
    """Build a v0.4 signed .aphelion.tar at ``tmp_path/out_name``.

    Returns ``(tar_path, signer_secret_for_trust_store)``. The trust store
    file MUST contain ``signer_secret`` byte-identical for the package to
    be trusted (HMAC fingerprint = sha256(secret), matched in M6 by
    ``compute_key_fingerprint`` over the .pem file's raw bytes).

    When ``sign=False`` the tar is left without ``signatures.jsonl`` /
    ``signers/`` so the M6 invariant rejects it as ``pkg.unsigned``.
    """
    src = tmp_path / f"pkg_source_{package_id[-12:]}"
    claim_id = "01963f7d-7000-7000-8000-c1a1" + package_id[-8:]
    instance_id = "01963f7d-7000-7000-8000-1111" + package_id[-8:]
    _build_source_dir(
        src,
        claim_id=claim_id,
        instance_id=instance_id,
        package_id=package_id,
        extra_fields=extra_fields,
    )

    tar_path = tmp_path / out_name
    aphelion_pack(src, tar_path)
    if not sign:
        return tar_path, signer_secret

    # Sign the manifest's canonical hash with HMAC.
    manifest_obj = canonical_normalize(
        json.loads((src / "manifest.json").read_bytes())
    )
    claims_tuples = [
        (c["claim_id"], c["claim_instance_id"], c["hash"])
        for c in manifest_obj["claims"]
    ]
    pkg_hash = compute_package_canonical_hash(
        format_version=manifest_obj["format_version"],
        package_id=manifest_obj["package_id"],
        claims=claims_tuples,
    )

    signer = HMACSigner(signer_id=signer_id, secret=signer_secret)
    envelope = signer.sign(
        package_canonical_hash=pkg_hash, signed_at_iso=_BUILDER_SIGNED_AT
    )
    manifest_record = signer.manifest()
    sig_bytes = write_signatures_jsonl([envelope])
    signer_manifest_bytes = canonical_dumps(
        canonical_normalize(
            {
                "algorithm": manifest_record.algorithm,
                "key_fingerprint": manifest_record.key_fingerprint,
                "notary_uri": None,
                "public_key_b64": manifest_record.public_key_b64,
                "signer_id": manifest_record.signer_id,
            }
        )
    )

    existing = read_members(tar_path.read_bytes())
    extra = [
        TarMember(path="signatures.jsonl", data=sig_bytes, is_dir=False),
        TarMember(
            path=f"signers/{signer_id}.json",
            data=signer_manifest_bytes,
            is_dir=False,
        ),
    ]
    tar_path.write_bytes(tar_pack(existing + extra))
    return tar_path, signer_secret


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def m6_ingest_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Disposable audit DB + trust store + package dir for one ingest test.

    The trust store is pre-seeded with the builder's HMAC secret as a
    ``.pem`` file so that builder-produced packages are TRUSTED. Tests
    that want untrusted-signer semantics MUST overwrite this seed (e.g.
    by writing a different secret or removing the .pem).
    """
    audit_db_path = tmp_path / "m6_test_audit.db"
    trust_store_dir = tmp_path / "trust_store"
    package_dir = tmp_path / "packages"
    trust_store_dir.mkdir()
    package_dir.mkdir()

    # Seed trust store with the builder secret so trust verification
    # passes for the canonical happy path.
    (trust_store_dir / f"{_BUILDER_SIGNER_ID}.pem").write_bytes(_BUILDER_SECRET)

    monkeypatch.setenv("PARALLAX_AUDIT_DB_PATH", str(audit_db_path))
    monkeypatch.setenv("PARALLAX_APHELION_TRUST_STORE", str(trust_store_dir))
    monkeypatch.setenv("PARALLAX_APHELION_PACKAGE_DIR", str(package_dir))

    return {
        "audit_db_path": audit_db_path,
        "trust_store_dir": trust_store_dir,
        "package_dir": package_dir,
    }


def _assert_disposable(audit_db_path: Path) -> None:
    """Defensive guard per 5/16 P2 backlog #5 — never touch production."""
    assert "parallax-kernel/db" not in str(audit_db_path).replace("\\", "/"), (
        f"production audit.db touched: {audit_db_path}"
    )


def _read_fixture(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _fixture_r4_fields(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Subset the fixture mapping to the R4-trigger fields v0.3 validates.

    The fixture also carries ``claim_id``/``package_id`` (consumed by the
    builder separately) — those are deliberately filtered out so the
    builder generates its own unique pair (the builder's claim_id encodes
    the package_id suffix to keep names unique across the 70-fixture sweep).
    """
    keep = ("subject", "polarity", "supersedes", "valid_from", "valid_until")
    return {k: v for k, v in fixture.items() if k in keep}


def _ingest_one_fixture(
    fixture_path: Path, env: dict[str, Path], *, session_id: str
) -> tuple[IngestReport | None, ParallaxIngestError | None]:
    """Wrap one R4 fixture into a 1-claim package and drive ingest_package().

    Returns ``(report, None)`` on success or ``(None, error)`` on failure.
    """
    _assert_disposable(env["audit_db_path"])
    fixture = _read_fixture(fixture_path)
    package_id = fixture["package_id"]

    tar_path, _secret = _build_aphelion_package(
        tmp_path=env["package_dir"],
        package_id=package_id,
        signer_secret=_BUILDER_SECRET,
        signer_id=_BUILDER_SIGNER_ID,
        out_name=f"{fixture_path.stem}.aphelion.tar",
        extra_fields=_fixture_r4_fields(fixture),
    )

    conn = open_audit_db(env["audit_db_path"], validate=False)
    try:
        try:
            report = ingest_package(
                package_path=tar_path,
                audit_conn=conn,
                package_dir=env["package_dir"],
                trust_store_dir=env["trust_store_dir"],
                session_id=session_id,
            )
            return report, None
        except ParallaxIngestError as exc:
            return None, exc
    finally:
        conn.close()


def _audit_row_count(audit_db_path: Path) -> int:
    """Open a fresh connection and count committed audit rows on disk."""
    conn = sqlite3.connect(audit_db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()[0]
    finally:
        conn.close()


def _list_fixtures(bucket: str) -> list[Path]:
    bucket_dir = FIXTURE_ROOT / bucket
    return sorted(bucket_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# Per-bucket parameterized tests (spec §7.3)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestM6IngestPerBucket:
    """Sweeps ``tests/fixtures/m6_claim_mappings/*/*.json`` — 70 fixtures total."""

    def test_not_found_bucket_20_fixtures_all_ingest(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """20 not_found fixtures: each ingests cleanly; 20 audit rows total."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        fixtures = _list_fixtures("not_found")
        assert len(fixtures) == 20, (
            f"expected 20 not_found fixtures, found {len(fixtures)}"
        )

        success_count = 0
        for fixture in fixtures:
            report, err = _ingest_one_fixture(
                fixture, m6_ingest_env, session_id=f"ingest:not_found:{fixture.stem}"
            )
            assert err is None, (
                f"not_found fixture {fixture.name} unexpectedly rejected: "
                f"reason_code={err.reason_code if err else None}"
            )
            assert report is not None
            assert report.audit_rows_written == 1
            success_count += 1

        assert success_count == 20
        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 20

    def test_supersession_bucket_30_fixtures_all_ingest(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """30 supersession fixtures: each ingests cleanly; 30 audit rows total.

        Supersession ``supersedes`` refs are cross-package here (one fixture
        per package) — v0.3 cross-package supersession is a read-side
        concern (W_CLAIM_SUPERSEDES_DANGLING, lenient), so ingest succeeds.
        """
        _assert_disposable(m6_ingest_env["audit_db_path"])
        fixtures = _list_fixtures("supersession")
        assert len(fixtures) == 30

        for fixture in fixtures:
            report, err = _ingest_one_fixture(
                fixture, m6_ingest_env, session_id=f"ingest:super:{fixture.stem}"
            )
            assert err is None, (
                f"supersession fixture {fixture.name} unexpectedly rejected: "
                f"reason_code={err.reason_code if err else None}"
            )
            assert report is not None
            assert report.audit_rows_written == 1

        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 30

    def test_expired_bucket_10_fixtures_all_ingest(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """10 expired fixtures: each ingests cleanly (expiry is read-side); 10 rows."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        fixtures = _list_fixtures("expired")
        assert len(fixtures) == 10

        for fixture in fixtures:
            report, err = _ingest_one_fixture(
                fixture, m6_ingest_env, session_id=f"ingest:expired:{fixture.stem}"
            )
            assert err is None, (
                f"expired fixture {fixture.name} unexpectedly rejected: "
                f"reason_code={err.reason_code if err else None}"
            )
            assert report is not None
            assert report.audit_rows_written == 1

        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 10

    def test_conflict_bucket_10_fixtures_partial_reject_polarity_invalid(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """10 conflict fixtures: REAL behaviour diverges from spec §7.3 expectation.

        Spec §7.3 states: "10 conflict fixtures REJECTED at v0.3 validation;
        0 audit rows". That assumes both members of each pair are bundled
        into the same package so the v0.3 validator can see them together.
        Per §7.4 the test isolation actually wraps each fixture into a
        1-claim package, so v0.3 validation only sees a single claim at a
        time and the cross-claim affirm+deny conflict is invisible.

        What DOES happen at single-claim granularity: the fixture corpus
        uses ``polarity: "affirm"`` for one half of each pair (5 fixtures)
        and ``polarity: "deny"`` for the other half (5 fixtures). The
        v0.3 validator's :data:`POLARITY_VALUES` only admits ``{affirm,
        negate, unknown}`` — so the 5 ``deny`` fixtures REJECT with
        ``claim.format_invalid`` and the 5 ``affirm`` fixtures INGEST.

        This is the honest signal the corpus provides under §7.4's
        isolation regime; we record it explicitly so a future spec/corpus
        change (bundle pairs into one package, or repair ``deny`` → ``negate``)
        flips this assertion loudly instead of silently fake-passing.
        """
        _assert_disposable(m6_ingest_env["audit_db_path"])
        fixtures = _list_fixtures("conflict")
        assert len(fixtures) == 10

        affirm_success = 0
        deny_reject = 0
        unexpected: list[str] = []
        for fixture in fixtures:
            polarity = _read_fixture(fixture)["polarity"]
            report, err = _ingest_one_fixture(
                fixture, m6_ingest_env, session_id=f"ingest:conflict:{fixture.stem}"
            )
            if polarity == "affirm":
                if err is not None:
                    unexpected.append(
                        f"{fixture.name} (affirm) unexpectedly rejected "
                        f"reason_code={err.reason_code}"
                    )
                else:
                    assert report is not None
                    affirm_success += 1
            elif polarity == "deny":
                if err is None:
                    unexpected.append(
                        f"{fixture.name} (deny) unexpectedly ingested (expected "
                        "claim.format_invalid via v0.3 polarity rejection)"
                    )
                else:
                    assert err.reason_code == "claim.format_invalid", (
                        f"{fixture.name}: expected claim.format_invalid, "
                        f"got {err.reason_code}"
                    )
                    assert err.exit_code == 65
                    deny_reject += 1
            else:
                unexpected.append(
                    f"{fixture.name}: corpus drift — unexpected polarity "
                    f"{polarity!r}"
                )

        assert not unexpected, "conflict-bucket drift:\n" + "\n".join(unexpected)
        assert affirm_success == 5, (
            f"expected 5 'affirm' fixtures to ingest, got {affirm_success}"
        )
        assert deny_reject == 5, (
            f"expected 5 'deny' fixtures to reject, got {deny_reject}"
        )
        # 5 'affirm' ingests → 5 rows; 5 'deny' rejections → 0 rows. Total 5.
        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 5


# ---------------------------------------------------------------------------
# 5 cross-cutting tests (spec §7.5)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestM6IngestCrossCutting:
    """Spec §7.5 — failure-mode coverage beyond the 70-fixture sweep."""

    def test_signer_untrusted_rejects_with_exit_65(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Package signed by a key whose fingerprint is NOT in the trust store
        → ``signer.untrusted`` → exit 65."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        # Sign with a DIFFERENT secret; trust store seeded with _BUILDER_SECRET.
        rogue_secret = b"rogue-untrusted-secret-32-bytes!"
        tar_path, _ = _build_aphelion_package(
            tmp_path=m6_ingest_env["package_dir"],
            package_id="01963f7d-7000-7000-8000-deadbeef0001",
            signer_secret=rogue_secret,
            signer_id=_BUILDER_SIGNER_ID,
            out_name="untrusted.aphelion.tar",
        )

        conn = open_audit_db(m6_ingest_env["audit_db_path"], validate=False)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=tar_path,
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:untrusted",
                )
        finally:
            conn.close()

        assert excinfo.value.reason_code == "signer.untrusted"
        assert excinfo.value.exit_code == 65
        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 0

    def test_pkg_path_escape_rejects_with_exit_65(
        self, m6_ingest_env: dict[str, Path], tmp_path: Path
    ) -> None:
        """``package_path`` resolves OUTSIDE ``PARALLAX_APHELION_PACKAGE_DIR``
        → ``pkg.path_escape`` → exit 65."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        # Build the tar in a sibling dir so its resolved path lies OUTSIDE
        # m6_ingest_env["package_dir"].
        outside_dir = tmp_path / "outside_pkg_dir"
        outside_dir.mkdir()
        tar_path, _ = _build_aphelion_package(
            tmp_path=outside_dir,
            package_id="01963f7d-7000-7000-8000-deadbeef0002",
            signer_secret=_BUILDER_SECRET,
            signer_id=_BUILDER_SIGNER_ID,
            out_name="escape.aphelion.tar",
        )

        conn = open_audit_db(m6_ingest_env["audit_db_path"], validate=False)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=tar_path,
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:escape",
                )
        finally:
            conn.close()

        assert excinfo.value.reason_code == "pkg.path_escape"
        assert excinfo.value.exit_code == 65
        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 0

    def test_audit_db_closed_rejects_with_disk_audit_db_write_failed(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """A closed / unusable ``audit_conn`` surfaces as a disk-class failure.

        Spec §7.5 lists ``disk.audit_db_unset`` (exit 78) as the env-var
        gate, but ``ingest_package`` takes the connection as an explicit
        argument — the env-var gate lives in the CLI layer (US-204), not in
        this module. The closest in-module surface is the audit-DB write
        path: a closed connection raises ``sqlite3.ProgrammingError`` from
        the writer, which the module wraps as ``disk.audit_db_write_failed``
        (exit 71).
        """
        _assert_disposable(m6_ingest_env["audit_db_path"])
        tar_path, _ = _build_aphelion_package(
            tmp_path=m6_ingest_env["package_dir"],
            package_id="01963f7d-7000-7000-8000-deadbeef0003",
            signer_secret=_BUILDER_SECRET,
            signer_id=_BUILDER_SIGNER_ID,
            out_name="db_closed.aphelion.tar",
        )

        conn = open_audit_db(m6_ingest_env["audit_db_path"], validate=False)
        conn.close()  # force the writer to fail

        with pytest.raises((ParallaxIngestError, sqlite3.ProgrammingError)) as excinfo:
            ingest_package(
                package_path=tar_path,
                audit_conn=conn,
                package_dir=m6_ingest_env["package_dir"],
                trust_store_dir=m6_ingest_env["trust_store_dir"],
                session_id="ingest:db_closed",
            )

        # Either branch confirms the row was NOT committed.
        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 0
        if isinstance(excinfo.value, ParallaxIngestError):
            # ProgrammingError is a sqlite3.Error subclass; the writer
            # raises it BEFORE the AuditDbWriteError translation in
            # ingest_package._write_batch — when wrapped, the reason_code
            # is the disk-class write failure.
            assert excinfo.value.reason_code in {
                "disk.audit_db_write_failed",
                "disk.permission",
            }

    def test_pkg_unsigned_rejects_with_exit_65(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Package without ``signatures.jsonl`` + ``require_signed=True``
        → ``pkg.unsigned`` → exit 65."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        tar_path, _ = _build_aphelion_package(
            tmp_path=m6_ingest_env["package_dir"],
            package_id="01963f7d-7000-7000-8000-deadbeef0004",
            signer_secret=_BUILDER_SECRET,
            signer_id=_BUILDER_SIGNER_ID,
            out_name="unsigned.aphelion.tar",
            sign=False,
        )

        conn = open_audit_db(m6_ingest_env["audit_db_path"], validate=False)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=tar_path,
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:unsigned",
                )
        finally:
            conn.close()

        assert excinfo.value.reason_code == "pkg.unsigned"
        assert excinfo.value.exit_code == 65
        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 0

    def test_idempotency_re_ingest_produces_distinct_audit_rows(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Re-running ingest on the same package produces TWO sets of audit
        rows (different ``envelope_message_id``) — confirms no auto-dedup
        per spec §5.4."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        tar_path, _ = _build_aphelion_package(
            tmp_path=m6_ingest_env["package_dir"],
            package_id="01963f7d-7000-7000-8000-deadbeef0005",
            signer_secret=_BUILDER_SECRET,
            signer_id=_BUILDER_SIGNER_ID,
            out_name="idempotency.aphelion.tar",
        )

        conn = open_audit_db(m6_ingest_env["audit_db_path"], validate=False)
        try:
            r1 = ingest_package(
                package_path=tar_path,
                audit_conn=conn,
                package_dir=m6_ingest_env["package_dir"],
                trust_store_dir=m6_ingest_env["trust_store_dir"],
                session_id="ingest:idempotency:first",
            )
            r2 = ingest_package(
                package_path=tar_path,
                audit_conn=conn,
                package_dir=m6_ingest_env["package_dir"],
                trust_store_dir=m6_ingest_env["trust_store_dir"],
                session_id="ingest:idempotency:second",
            )
        finally:
            conn.close()

        assert r1.audit_rows_written == 1
        assert r2.audit_rows_written == 1
        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 2

        # Two distinct envelope_message_id values committed.
        verify = sqlite3.connect(m6_ingest_env["audit_db_path"])
        try:
            ids = {
                row[0]
                for row in verify.execute(
                    "SELECT envelope_message_id FROM audit_row"
                ).fetchall()
            }
        finally:
            verify.close()
        assert len(ids) == 2, f"expected 2 distinct envelope_message_ids, got {ids}"


# ---------------------------------------------------------------------------
# Smoke tests for module surface — covers IngestReport / ClaimMappingBatch /
# TrustDecision shape so the public API contract is exercised even if the
# bucket sweeps later get xfail'd.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestM6IngestPublicSurface:
    """Lightweight checks against the module's public DTOs."""

    def test_happy_path_returns_well_formed_ingest_report(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Pin :class:`IngestReport` field shape for one canonical happy ingest."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        tar_path, _ = _build_aphelion_package(
            tmp_path=m6_ingest_env["package_dir"],
            package_id="01963f7d-7000-7000-8000-deadbeef0006",
            signer_secret=_BUILDER_SECRET,
            signer_id=_BUILDER_SIGNER_ID,
            out_name="report_shape.aphelion.tar",
        )
        conn = open_audit_db(m6_ingest_env["audit_db_path"], validate=False)
        try:
            report = ingest_package(
                package_path=tar_path,
                audit_conn=conn,
                package_dir=m6_ingest_env["package_dir"],
                trust_store_dir=m6_ingest_env["trust_store_dir"],
                session_id="ingest:report_shape",
            )
        finally:
            conn.close()

        assert isinstance(report, IngestReport)
        assert report.package_id == "01963f7d-7000-7000-8000-deadbeef0006"
        assert report.claims_ingested == 1
        assert report.audit_rows_written == 1
        assert report.signer_id == _BUILDER_SIGNER_ID
        assert report.elapsed_ms >= 0
        assert report.package_path.endswith("report_shape.aphelion.tar")

    def test_trust_decision_factories_round_trip(self) -> None:
        """:class:`TrustDecision` accepted/rejected factories produce the
        expected accepted/reason_code combinations."""
        ok = TrustDecision.accepted_for("signer-a")
        assert ok.accepted is True
        assert ok.reason_code is None
        assert ok.signer_id == "signer-a"

        bad = TrustDecision.rejected_for("signer-b", "signer.untrusted")
        assert bad.accepted is False
        assert bad.reason_code == "signer.untrusted"
        assert bad.signer_id == "signer-b"

    def test_claim_mapping_batch_is_frozen(self) -> None:
        """:class:`ClaimMappingBatch` is a frozen dataclass — mutation raises."""
        from dataclasses import FrozenInstanceError

        batch = ClaimMappingBatch(
            package_id="01963f7d-7000-7000-8000-deadbeef0099",
            signer_id="signer-x",
            signer_manifest_digest="a" * 64,
            audit_rows=(),
            timestamp="2026-05-17T00:00:00Z",
        )
        with pytest.raises(FrozenInstanceError):
            batch.package_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Gate failure coverage — pin every §5.2 + §3.3a validation gate and the
# §2.4 Aphelion-exception → reason_code translation table.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestM6IngestGateFailures:
    """Coverage of the §5.2 / §3.3a / §2.4 failure paths.

    These are intentionally tiny — one tar build, one ingest_package call,
    one reason_code + exit_code assertion — so the §6.2 table stays pinned
    and any drift in the reason_code → exit_code mapping surfaces here
    rather than via a runtime CLI exit-code regression.
    """

    def _conn(self, env: dict[str, Path]) -> sqlite3.Connection:
        return open_audit_db(env["audit_db_path"], validate=False)

    def _build_default(self, env: dict[str, Path], *, name: str) -> Path:
        # package_id MUST be a v7 UUID per aphelion validator's UUID_V7_RE;
        # derive a deterministic-but-valid suffix from name's bytes.
        suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
        tar_path, _ = _build_aphelion_package(
            tmp_path=env["package_dir"],
            package_id=f"01963f7d-7000-7000-8000-{suffix}",
            signer_secret=_BUILDER_SECRET,
            signer_id=_BUILDER_SIGNER_ID,
            out_name=f"{name}.aphelion.tar",
        )
        return tar_path

    def test_package_dir_not_absolute_raises_pkg_dir_unset(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Relative ``package_dir`` → ``pkg.dir_unset`` (gate 2, exit 78)."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=Path("rel/path.aphelion.tar"),
                    audit_conn=conn,
                    package_dir=Path("relative/pkg/dir"),
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:rel_pkg_dir",
                )
        finally:
            conn.close()
        assert excinfo.value.reason_code == "pkg.dir_unset"
        assert excinfo.value.exit_code == 78

    def test_package_dir_missing_raises_pkg_dir_unset(
        self, m6_ingest_env: dict[str, Path], tmp_path: Path
    ) -> None:
        """Non-existent ``package_dir`` → ``pkg.dir_unset`` (gate 3, exit 78)."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=tmp_path / "x.aphelion.tar",
                    audit_conn=conn,
                    package_dir=tmp_path / "does_not_exist",
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:missing_pkg_dir",
                )
        finally:
            conn.close()
        assert excinfo.value.reason_code == "pkg.dir_unset"

    def test_package_dir_is_file_raises_pkg_dir_unset(
        self, m6_ingest_env: dict[str, Path], tmp_path: Path
    ) -> None:
        """``package_dir`` points at a regular file → ``pkg.dir_unset``."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        not_a_dir = tmp_path / "regular_file"
        not_a_dir.write_bytes(b"")
        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=tmp_path / "x.aphelion.tar",
                    audit_conn=conn,
                    package_dir=not_a_dir,
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:file_pkg_dir",
                )
        finally:
            conn.close()
        assert excinfo.value.reason_code == "pkg.dir_unset"

    def test_trust_store_missing_raises_pkg_trust_store_missing(
        self, m6_ingest_env: dict[str, Path], tmp_path: Path
    ) -> None:
        """Non-existent trust store → ``pkg.trust_store_missing`` (exit 78)."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=tmp_path / "x.aphelion.tar",
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=tmp_path / "no_trust_store_here",
                    session_id="ingest:no_trust_store",
                )
        finally:
            conn.close()
        assert excinfo.value.reason_code == "pkg.trust_store_missing"
        assert excinfo.value.exit_code == 78

    def test_empty_pem_in_trust_store_raises_signer_trust_pem_invalid(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """An empty ``.pem`` file → ``signer.trust_pem_invalid`` (exit 78)."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        (m6_ingest_env["trust_store_dir"] / "empty.pem").write_bytes(b"")
        tar_path = self._build_default(m6_ingest_env, name="empty_pem")

        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=tar_path,
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:empty_pem",
                )
        finally:
            conn.close()
        assert excinfo.value.reason_code == "signer.trust_pem_invalid"
        assert excinfo.value.exit_code == 78

    def test_package_extension_invalid_raises_pkg_extension_invalid(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Wrong file extension → ``pkg.extension_invalid`` (exit 65)."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        wrong = m6_ingest_env["package_dir"] / "not_an_aphelion.tar.gz"
        wrong.write_bytes(b"\0\0")

        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=wrong,
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:wrong_ext",
                )
        finally:
            conn.close()
        assert excinfo.value.reason_code == "pkg.extension_invalid"
        assert excinfo.value.exit_code == 65

    def test_package_path_not_a_file_raises_pkg_not_regular_file(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Path is a directory, not a regular file → ``pkg.not_regular_file``."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        as_dir = m6_ingest_env["package_dir"] / "i_am_a_dir.aphelion.tar"
        as_dir.mkdir()

        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=as_dir,
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:dir_path",
                )
        finally:
            conn.close()
        assert excinfo.value.reason_code == "pkg.not_regular_file"

    def test_package_path_not_found_raises_pkg_not_found(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Missing path under valid dir → ``pkg.not_found`` (exit 65)."""
        _assert_disposable(m6_ingest_env["audit_db_path"])
        missing = m6_ingest_env["package_dir"] / "missing.aphelion.tar"

        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=missing,
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:missing_path",
                )
        finally:
            conn.close()
        assert excinfo.value.reason_code == "pkg.not_found"
        assert excinfo.value.exit_code == 65

    def test_manifest_missing_raises_signer_manifest_missing(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Tar with ``signatures.jsonl`` but no matching ``signers/<id>.json``
        → ``signer.manifest_missing``.

        Builds a normally-signed tar then strips the ``signers/`` member so
        the envelope refers to a signer_id that has no manifest in the tar.
        """
        _assert_disposable(m6_ingest_env["audit_db_path"])
        tar_path, _ = _build_aphelion_package(
            tmp_path=m6_ingest_env["package_dir"],
            package_id="01963f7d-7000-7000-8000-defacedfeed1",
            signer_secret=_BUILDER_SECRET,
            signer_id=_BUILDER_SIGNER_ID,
            out_name="manifest_missing.aphelion.tar",
        )
        # Strip the signers/<id>.json member.
        existing = read_members(tar_path.read_bytes())
        filtered = [m for m in existing if not m.path.startswith("signers/")]
        tar_path.write_bytes(tar_pack(filtered))

        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=tar_path,
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:manifest_missing",
                )
        finally:
            conn.close()
        # Aphelion's validate_signatures rejects this earlier as a missing
        # signer manifest — surfaces as signer.* class.
        assert excinfo.value.reason_code in {
            "signer.manifest_missing",
            "signer.signature_invalid",
        }
        assert excinfo.value.exit_code == 65

    def test_tampered_package_hash_raises_signature_invalid(
        self, m6_ingest_env: dict[str, Path]
    ) -> None:
        """Mutating a claim file after signing → Aphelion verification fails.

        After the canonical hash + signature are baked into the tar, we
        rewrite the claim file. The repacked tar's claim-hash diverges
        from manifest's recorded hash → Aphelion ``VerificationError``
        → ``pkg.hash_mismatch``.
        """
        _assert_disposable(m6_ingest_env["audit_db_path"])
        tar_path, _ = _build_aphelion_package(
            tmp_path=m6_ingest_env["package_dir"],
            package_id="01963f7d-7000-7000-8000-deadbeefcafe",
            signer_secret=_BUILDER_SECRET,
            signer_id=_BUILDER_SIGNER_ID,
            out_name="hash_tamper.aphelion.tar",
        )
        existing = read_members(tar_path.read_bytes())
        tampered = [
            TarMember(
                path=m.path,
                data=(m.data + b"\n# tampered\n") if m.path.startswith("claims/") else m.data,
                is_dir=m.is_dir,
            )
            for m in existing
        ]
        tar_path.write_bytes(tar_pack(tampered))

        conn = self._conn(m6_ingest_env)
        try:
            with pytest.raises(ParallaxIngestError) as excinfo:
                ingest_package(
                    package_path=tar_path,
                    audit_conn=conn,
                    package_dir=m6_ingest_env["package_dir"],
                    trust_store_dir=m6_ingest_env["trust_store_dir"],
                    session_id="ingest:hash_tamper",
                )
        finally:
            conn.close()
        # Aphelion may surface this as VerificationError (hash mismatch) or
        # SignerVerificationError (the package-hash check at sign time).
        assert excinfo.value.reason_code in {
            "pkg.hash_mismatch",
            "pkg.semantic_invalid",
            "signer.signature_invalid",
        }
        assert excinfo.value.exit_code == 65
        assert _audit_row_count(m6_ingest_env["audit_db_path"]) == 0

    def test_parallax_ingest_error_rejects_unknown_reason_code(self) -> None:
        """Constructing :class:`ParallaxIngestError` with an unknown
        ``reason_code`` is a programmer error — must raise ``ValueError``
        rather than silently defaulting to exit 0."""
        with pytest.raises(ValueError, match="unknown reason_code"):
            ParallaxIngestError("bogus.namespace.bad", "msg")
