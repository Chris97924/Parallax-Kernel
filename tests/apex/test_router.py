"""Apex M7 — Apex public-read router tests.

Implements ``docs/m7-prep/apex-router-public-read-spec.md`` v0.1.3-reframe
test scope (§3.3 read-path call set, §4.3 failure modes, §4.5 observability
metrics, §3.4 boundaries). Two layers:

  * Unit (fast, deterministic): version assertion, package-dir validation,
    exception → reason classification, metric registration idempotency,
    source-level boundary greps.
  * Integration (real ``.aphelion.tar``): happy read, unsigned/corrupt
    failure modes, empty-corpus, audit-write atomic abort.

No mocks for the integration layer — every package is built + HMAC-signed
with the real ``aphelion`` lib (mirrors the M6 ingest test builder per
``tests/integration/test_m6_ingest_pipeline.py``), so the spec §3.3 read
path is exercised end-to-end.
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
from aphelion.errors import SchemaError, SecurityError, SemanticError, VerificationError
from aphelion.packer import pack as aphelion_pack
from aphelion.sig_pack import write_signatures_jsonl
from aphelion.signer import (
    HMACSigner,
    SignerVerificationError,
    compute_package_canonical_hash,
)

from parallax.apex import router as router_mod
from parallax.apex.audit_db import open_audit_db
from parallax.apex.router import (
    REQUIRED_APHELION_MIN_VERSION,
    ApexPublicReadRouter,
    ApexRouterStartupError,
    assert_aphelion_version,
    classify_package_exception,
    validate_package_dir,
)
from parallax.router.aphelion_adapter import AphelionUnreachableError
from parallax.router.contracts import QueryRequest
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Builder constants (mirror tests/integration/test_m6_ingest_pipeline.py)
# ---------------------------------------------------------------------------

_BUILDER_INSTANCE_ID = "01963f7d-7000-7000-8000-aaaaaaaaaaa1"
_BUILDER_EVENT_ID = "01963f7d-7000-7000-8000-eeee00000001"
_BUILDER_SIGNED_AT = "2026-05-17T00:00:00Z"
_BUILDER_SIGNER_ID = "m7-test-signer"
_BUILDER_HMAC = b"m7-test-hmac-32-bytes-padding!!!"  # exactly 32 bytes (test-only)
_SUBJECT = "retrieval-quality"


# ---------------------------------------------------------------------------
# Package builder helper (contained — no separate module, per §7 convention)
# ---------------------------------------------------------------------------


def _claim_md(*, claim_id: str, extra_fields: Mapping[str, Any] | None = None) -> bytes:
    """v0.3 claim markdown with ASCII-ascending bare-identifier YAML keys."""
    base: dict[str, Any] = {
        "body_format": "markdown",
        "claim_id": claim_id,
        "title": "M7 test claim",
    }
    if extra_fields:
        base.update(extra_fields)

    lines = ["---"]
    for key in sorted(base.keys()):
        value = base[key]
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
                continue
            lines.append(f"{key}:")
            lines.extend(f'  - "{item}"' for item in value)
        elif isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif value is None:
            lines.append(f"{key}: null")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    return ("\n".join(lines)).encode("utf-8")


def _build_source_dir(
    src: Path,
    *,
    claim_id: str,
    instance_id: str,
    package_id: str,
    extra_fields: Mapping[str, Any] | None = None,
) -> bytes:
    """Materialize a 1-claim source dir; return the canonical claim bytes."""
    (src / "claims").mkdir(parents=True, exist_ok=True)
    claim_rel = f"claims/{claim_id}.md"
    claim_bytes = _claim_md(claim_id=claim_id, extra_fields=extra_fields)
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
        "producer": "parallax-m7-test",
        "provenance_path": "provenance.jsonl",
    }
    (src / "manifest.json").write_bytes(canonical_dumps(canonical_normalize(manifest)))

    event = {
        "actor": "m7-test",
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
    out_name: str,
    sign: bool = True,
    extra_fields: Mapping[str, Any] | None = None,
) -> Path:
    """Build a v0.4 (optionally HMAC-signed) ``.aphelion.tar`` at tmp_path/out_name."""
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
        return tar_path

    manifest_obj = canonical_normalize(json.loads((src / "manifest.json").read_bytes()))
    claims_tuples = [
        (c["claim_id"], c["claim_instance_id"], c["hash"]) for c in manifest_obj["claims"]
    ]
    pkg_hash = compute_package_canonical_hash(
        format_version=manifest_obj["format_version"],
        package_id=manifest_obj["package_id"],
        claims=claims_tuples,
    )
    signer = HMACSigner(_BUILDER_SIGNER_ID, _BUILDER_HMAC)
    envelope = signer.sign(package_canonical_hash=pkg_hash, signed_at_iso=_BUILDER_SIGNED_AT)
    mr = signer.manifest()
    sig_bytes = write_signatures_jsonl([envelope])
    sm_bytes = canonical_dumps(
        canonical_normalize(
            {
                "algorithm": mr.algorithm,
                "key_fingerprint": mr.key_fingerprint,
                "notary_uri": None,
                "public_key_b64": mr.public_key_b64,
                "signer_id": mr.signer_id,
            }
        )
    )
    existing = read_members(tar_path.read_bytes())
    extra = [
        TarMember(path="signatures.jsonl", data=sig_bytes, is_dir=False),
        TarMember(path=f"signers/{_BUILDER_SIGNER_ID}.json", data=sm_bytes, is_dir=False),
    ]
    tar_path.write_bytes(tar_pack(existing + extra))
    return tar_path


_ACTIVE_CLAIM_FIELDS: dict[str, Any] = {
    "polarity": "affirm",
    "subject": _SUBJECT,
    "valid_from": "2026-01-01T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Metric helpers — read counter/gauge child values for delta assertions.
# ---------------------------------------------------------------------------


def _counter_value(metric: Any, **labels: str) -> float:
    """Current value of a (possibly labeled) prometheus counter child."""
    child = metric.labels(**labels) if labels else metric
    return child._value.get()  # type: ignore[attr-defined]


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    d = tmp_path / "packages"
    d.mkdir()
    return d


@pytest.fixture
def audit_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = open_audit_db(tmp_path / "m7_audit.db", validate=False)
    yield conn
    conn.close()


def _make_router(package_dir: Path, audit_conn: sqlite3.Connection) -> ApexPublicReadRouter:
    return ApexPublicReadRouter(
        package_dir=package_dir,
        audit_conn_provider=lambda: audit_conn,
    )


def _query(subject: str = _SUBJECT) -> QueryRequest:
    return QueryRequest(
        query_type=QueryType.RECENT_CONTEXT,
        user_id="m7-test-user",
        q=subject,
        params={"subject": subject},
    )


# ===========================================================================
# Unit — version assertion (§3.3 startup version pin)
# ===========================================================================


@pytest.mark.unit
class TestVersionAssertion:
    def test_passes_for_installed_min_version(self) -> None:
        """Installed aphelion satisfies the pinned floor."""
        version = assert_aphelion_version()
        assert isinstance(version, str)
        assert version >= REQUIRED_APHELION_MIN_VERSION

    def test_min_version_floor_is_0_4_0(self) -> None:
        """SD-1: floor pinned to 0.4.0 (installed), not spec's tentative 0.5.0."""
        assert REQUIRED_APHELION_MIN_VERSION == "0.4.0"

    def test_rejects_when_min_higher_than_installed(self) -> None:
        """A floor above the installed version is a hard startup error."""
        with pytest.raises(ApexRouterStartupError, match="aphelion"):
            assert_aphelion_version(min_version="99.0.0")

    def test_emits_lib_version_info_gauge_on_success(self) -> None:
        assert_aphelion_version()
        installed = router_mod._installed_aphelion_version()
        gauge_value = router_mod.LIB_VERSION_INFO.labels(
            version=installed, min_version=REQUIRED_APHELION_MIN_VERSION
        )._value.get()
        assert gauge_value == 1


# ===========================================================================
# Unit — package_dir validation (§4.4 M5 P-A2' carry-forward)
# ===========================================================================


@pytest.mark.unit
class TestValidatePackageDir:
    def test_happy_returns_resolved_absolute(self, tmp_path: Path) -> None:
        d = tmp_path / "pkgs"
        d.mkdir()
        resolved = validate_package_dir(d)
        assert resolved.is_absolute()
        assert resolved == d.resolve()

    def test_relative_path_raises_and_counts(self) -> None:
        before = _counter_value(router_mod.PACKAGE_DIR_ERRORS, reason="relative_path")
        with pytest.raises(AphelionUnreachableError) as exc:
            validate_package_dir(Path("relative/pkgs"))
        assert exc.value.reason == "package_dir_inaccessible"
        after = _counter_value(router_mod.PACKAGE_DIR_ERRORS, reason="relative_path")
        assert after == before + 1

    def test_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AphelionUnreachableError) as exc:
            validate_package_dir(tmp_path / "does_not_exist")
        assert exc.value.reason == "package_dir_inaccessible"

    def test_path_is_a_file_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "regular_file"
        f.write_bytes(b"")
        with pytest.raises(AphelionUnreachableError) as exc:
            validate_package_dir(f)
        assert exc.value.reason == "package_dir_inaccessible"

    def test_traversal_segment_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "pkgs"
        d.mkdir()
        sneaky = tmp_path / "pkgs" / ".." / "pkgs"
        with pytest.raises(AphelionUnreachableError) as exc:
            validate_package_dir(sneaky)
        assert exc.value.reason == "package_dir_inaccessible"

    def test_broken_symlink_counts_as_broken_symlink(self, tmp_path: Path) -> None:
        """A dangling symlink is classified under the spec-named broken_symlink."""
        link = tmp_path / "pkgs_link"
        try:
            link.symlink_to(tmp_path / "nonexistent_target", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unprivileged on this platform")
        before = _counter_value(router_mod.PACKAGE_DIR_ERRORS, reason="broken_symlink")
        with pytest.raises(AphelionUnreachableError) as exc:
            validate_package_dir(link)
        assert exc.value.reason == "package_dir_inaccessible"
        after = _counter_value(router_mod.PACKAGE_DIR_ERRORS, reason="broken_symlink")
        assert after == before + 1


# ===========================================================================
# Unit — claim-path traversal guard (W6 cross-review Tier-A hardening)
# ===========================================================================


@pytest.mark.unit
class TestProjectClaimsPathTraversal:
    """A manifest claim ``path`` that escapes the extraction dir is rejected.

    End-to-end this is normally caught by ``verify_package`` (fileset
    divergence), so the guard is exercised directly: the read path must not
    rely on upstream verification to keep its own filesystem access in-bounds.
    """

    def _write_manifest(self, extracted: Path, claim_path: str) -> None:
        extracted.mkdir(parents=True, exist_ok=True)
        manifest = {
            "claims": [{"claim_id": "c1", "path": claim_path}],
            "package_id": "01963f7d-7000-7000-8000-aaaa00000099",
        }
        (extracted / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_dotdot_escape_rejected_as_package_corrupt(self, tmp_path: Path) -> None:
        extracted = tmp_path / "extracted"
        self._write_manifest(extracted, "../escape.md")
        (tmp_path / "escape.md").write_text("---\nclaim_id: c1\n---\n", encoding="utf-8")
        with pytest.raises(AphelionUnreachableError) as exc:
            ApexPublicReadRouter._project_claims(tmp_path / "x.aphelion.tar", extracted)
        assert exc.value.reason == "package_corrupt"

    def test_absolute_path_rejected_as_package_corrupt(self, tmp_path: Path) -> None:
        extracted = tmp_path / "extracted"
        outside = tmp_path / "outside.md"
        outside.write_text("---\nclaim_id: c1\n---\n", encoding="utf-8")
        self._write_manifest(extracted, str(outside))
        with pytest.raises(AphelionUnreachableError) as exc:
            ApexPublicReadRouter._project_claims(tmp_path / "x.aphelion.tar", extracted)
        assert exc.value.reason == "package_corrupt"

    def test_unreadable_manifest_is_package_corrupt(self, tmp_path: Path) -> None:
        """Manifest that survived verify but is not valid JSON → package_corrupt."""
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "manifest.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(AphelionUnreachableError) as exc:
            ApexPublicReadRouter._project_claims(tmp_path / "x.aphelion.tar", extracted)
        assert exc.value.reason == "package_corrupt"

    def test_manifest_missing_required_key_is_package_corrupt(self, tmp_path: Path) -> None:
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "manifest.json").write_text('{"claims": []}', encoding="utf-8")
        with pytest.raises(AphelionUnreachableError) as exc:
            ApexPublicReadRouter._project_claims(tmp_path / "x.aphelion.tar", extracted)
        assert exc.value.reason == "package_corrupt"


# ===========================================================================
# Unit — exception → reason classification (§4.3 table, all branches)
# ===========================================================================


@pytest.mark.unit
class TestClassifyPackageException:
    def test_file_not_found_is_package_missing(self) -> None:
        assert classify_package_exception(FileNotFoundError("x")) == "package_missing"

    def test_security_error_is_package_corrupt(self) -> None:
        assert classify_package_exception(SecurityError(msg="unsafe")) == "package_corrupt"

    def test_verification_error_is_package_corrupt(self) -> None:
        assert classify_package_exception(VerificationError(msg="hash")) == "package_corrupt"

    def test_semantic_error_is_package_corrupt(self) -> None:
        assert classify_package_exception(SemanticError(msg="x")) == "package_corrupt"

    def test_schema_error_is_package_corrupt(self) -> None:
        assert classify_package_exception(SchemaError(msg="x")) == "package_corrupt"

    def test_signer_required_is_unsigned_package(self) -> None:
        exc = SignerVerificationError("E_SIGNER_REQUIRED", "no sigs")
        assert classify_package_exception(exc) == "unsigned_package"

    def test_signer_other_is_signer_untrusted(self) -> None:
        exc = SignerVerificationError("E_SIGNATURE_INVALID", "bad sig")
        assert classify_package_exception(exc) == "signer_untrusted"

    def test_unknown_exception_is_lib_error(self) -> None:
        assert classify_package_exception(RuntimeError("boom")) == "lib_error"


# ===========================================================================
# Unit — metric registration idempotency + boundary source greps (§3.3/§3.4)
# ===========================================================================


@pytest.mark.unit
class TestModuleHygiene:
    def test_reimport_does_not_raise_duplicate_metric(self) -> None:
        import importlib

        importlib.reload(router_mod)  # must not raise prometheus duplicate ValueError

    def test_no_perihelion_import(self) -> None:
        """§3.4 boundary: router never imports / references Perihelion."""
        source = Path(router_mod.__file__).read_text(encoding="utf-8")
        assert "import perihelion" not in source
        assert "perihelion_" not in source
        assert "from perihelion" not in source

    def test_no_ghost_audit_write_error_class(self) -> None:
        """Hard gate (#68 reconcile): AphelionAuditWriteError must stay clean.

        Covers both the M7 router and the reused M5 adapter (the #68 reconcile
        touched the adapter), since the ghost class must not exist as a code
        symbol in either.
        """
        from parallax.router import aphelion_adapter

        for mod in (router_mod, aphelion_adapter):
            source = Path(mod.__file__).read_text(encoding="utf-8")
            assert "AphelionAuditWriteError" not in source, mod.__name__

    def test_read_path_does_not_use_extract_signer_manifests(self) -> None:
        """§3.3: extract_signer_manifests is OUT-OF-SCOPE for the read path."""
        source = Path(router_mod.__file__).read_text(encoding="utf-8")
        assert "extract_signer_manifests" not in source


# ===========================================================================
# Integration — real .aphelion.tar read path (§3.3 + §4.3)
# ===========================================================================


@pytest.mark.integration
class TestPublicReadHappyPath:
    def test_returns_claim_for_matching_subject(
        self, package_dir: Path, audit_conn: sqlite3.Connection
    ) -> None:
        _build_aphelion_package(
            tmp_path=package_dir,
            package_id="01963f7d-7000-7000-8000-aaaa00000001",
            out_name="happy.aphelion.tar",
            extra_fields=_ACTIVE_CLAIM_FIELDS,
        )
        router = _make_router(package_dir, audit_conn)

        before = _counter_value(router_mod.READ_TOTAL, result="success")
        evidence = router.query(_query())
        after = _counter_value(router_mod.READ_TOTAL, result="success")

        assert len(evidence.hits) >= 1
        assert evidence.hits[0]["kind"] == "aphelion_claim"
        assert after == before + 1

    def test_audit_row_written_on_hit(
        self, package_dir: Path, audit_conn: sqlite3.Connection
    ) -> None:
        """package_id is projected into the claim mapping → audit row persists."""
        _build_aphelion_package(
            tmp_path=package_dir,
            package_id="01963f7d-7000-7000-8000-aaaa00000002",
            out_name="audited.aphelion.tar",
            extra_fields=_ACTIVE_CLAIM_FIELDS,
        )
        router = _make_router(package_dir, audit_conn)
        router.query(_query())

        count = audit_conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()[0]
        assert count == 1


@pytest.mark.integration
class TestPublicReadFailureModes:
    def test_unsigned_package_raises_unsigned_package(
        self, package_dir: Path, audit_conn: sqlite3.Connection
    ) -> None:
        _build_aphelion_package(
            tmp_path=package_dir,
            package_id="01963f7d-7000-7000-8000-bbbb00000001",
            out_name="unsigned.aphelion.tar",
            sign=False,
            extra_fields=_ACTIVE_CLAIM_FIELDS,
        )
        router = _make_router(package_dir, audit_conn)

        before = _counter_value(router_mod.READ_ERRORS, reason="unsigned_package", exc_class="")
        with pytest.raises(AphelionUnreachableError) as exc:
            router.query(_query())
        assert exc.value.reason == "unsigned_package"
        after = _counter_value(router_mod.READ_ERRORS, reason="unsigned_package", exc_class="")
        assert after == before + 1

    def test_corrupt_package_raises_package_corrupt(
        self, package_dir: Path, audit_conn: sqlite3.Connection
    ) -> None:
        """Claim file mutated after signing → hash mismatch → package_corrupt."""
        tar_path = _build_aphelion_package(
            tmp_path=package_dir,
            package_id="01963f7d-7000-7000-8000-cccc00000001",
            out_name="corrupt.aphelion.tar",
            extra_fields=_ACTIVE_CLAIM_FIELDS,
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
        router = _make_router(package_dir, audit_conn)

        with pytest.raises(AphelionUnreachableError) as exc:
            router.query(_query())
        assert exc.value.reason == "package_corrupt"

    def test_empty_corpus_returns_empty_and_counts(
        self, package_dir: Path, audit_conn: sqlite3.Connection
    ) -> None:
        """Accessible dir with zero .aphelion.tar → [] + empty_corpus counter."""
        router = _make_router(package_dir, audit_conn)
        before = _counter_value(router_mod.EMPTY_CORPUS)
        evidence = router.query(_query())
        after = _counter_value(router_mod.EMPTY_CORPUS)

        assert evidence.hits == ()
        assert after == before + 1

    def test_no_matching_claim_returns_empty(
        self, package_dir: Path, audit_conn: sqlite3.Connection
    ) -> None:
        """Corpus has a claim but for a different subject → empty result."""
        _build_aphelion_package(
            tmp_path=package_dir,
            package_id="01963f7d-7000-7000-8000-dddd00000001",
            out_name="other_subject.aphelion.tar",
            extra_fields={**_ACTIVE_CLAIM_FIELDS, "subject": "unrelated-topic"},
        )
        router = _make_router(package_dir, audit_conn)
        before = _counter_value(router_mod.EMPTY_RESULT, cause="no_matching_claim")
        evidence = router.query(_query())
        after = _counter_value(router_mod.EMPTY_RESULT, cause="no_matching_claim")

        assert evidence.hits == ()
        assert after == before + 1


@pytest.mark.integration
class TestAuditWriteAtomicAbort:
    def test_closed_conn_raises_audit_db_write_failed_no_partial_state(
        self, package_dir: Path, tmp_path: Path
    ) -> None:
        """§4.3 audit-write row: a failed audit write fully aborts the read.

        Claims are NOT returned; no envelope/audit state leaks; the read
        surfaces ``AphelionUnreachableError(reason='audit_db_write_failed')``
        and the M7 audit-failure counter increments.
        """
        _build_aphelion_package(
            tmp_path=package_dir,
            package_id="01963f7d-7000-7000-8000-eeee00000001",
            out_name="abort.aphelion.tar",
            extra_fields=_ACTIVE_CLAIM_FIELDS,
        )
        conn = open_audit_db(tmp_path / "abort_audit.db", validate=False)
        conn.close()  # force the writer to fail
        router = ApexPublicReadRouter(package_dir=package_dir, audit_conn_provider=lambda: conn)

        before = _counter_value(router_mod.AUDIT_WRITE_FAILURES, cause="ProgrammingError")
        with pytest.raises(AphelionUnreachableError) as exc:
            router.query(_query())
        assert exc.value.reason == "audit_db_write_failed"
        # Atomic abort: no envelope / audit row leaked onto the adapter.
        assert router.last_envelope is None
        after = _counter_value(router_mod.AUDIT_WRITE_FAILURES, cause="ProgrammingError")
        assert after == before + 1


@pytest.mark.unit
class TestQueryMetricVisibility:
    """No read may be metric-dark (§4.3/§4.5): an unexpected (non-typed) adapter
    failure must still increment the error counters before propagating, or
    ApexReadErrorRateHigh could silently never fire for adapter contract breaks."""

    def test_unexpected_adapter_error_is_recorded_then_reraised(
        self, package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router = _make_router(package_dir, audit_conn)

        before_err = _counter_value(router_mod.READ_TOTAL, result="error")
        before_lib = _counter_value(
            router_mod.READ_ERRORS, reason="lib_error", exc_class="RuntimeError"
        )

        def _boom(_request: QueryRequest) -> object:
            raise RuntimeError("adapter contract break")

        monkeypatch.setattr(router._adapter, "query", _boom)

        # The ORIGINAL exception type is preserved (not masked as AphelionUnreachableError).
        with pytest.raises(RuntimeError, match="adapter contract break"):
            router.query(_query())

        assert _counter_value(router_mod.READ_TOTAL, result="error") == before_err + 1
        assert (
            _counter_value(router_mod.READ_ERRORS, reason="lib_error", exc_class="RuntimeError")
            == before_lib + 1
        )
