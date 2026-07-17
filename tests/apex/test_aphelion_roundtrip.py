"""Apex — Parallax <-> Aphelion end-to-end round-trip acceptance harness.

Proves the full loop: *Parallax claims -> Aphelion package -> production ingest
-> identical claims read back*. It joins the outbound exporter
(:mod:`parallax.apex.aphelion_export`) to the inbound production ingest path
(:mod:`parallax.apex.aphelion_ingest`) and the M7 public-read projection
(:class:`parallax.apex.router.ApexPublicReadRouter`) with NO bespoke ingest or
read shim — every claim physically traverses a signed ``.aphelion.tar``.

Pipeline (per test, on a disposable tmp corpus):

  1. Build a seeded, deterministic synthetic corpus of N>=50 claims (store A)
     covering every v0.3 variant: plain claims, supersession chains
     (``supersedes``), valid-time windows (``valid_from`` / ``valid_until``),
     and polarity (affirm/negate/unknown) with a ``target_claim_id`` pointer.
  2. Export it via :func:`parallax.apex.aphelion_export.export_claims`.
  3. Sign the packed tar (HMAC-SHA256, spec-§5 test signer) and seed the
     operator trust store with the signer secret — this is the operator/signing
     concern the exporter deliberately leaves out, layered on exactly as the M6
     ingest suite layers it (``tests/integration/test_m6_ingest_pipeline.py``).
  4. ``aphelion.verifier.verify_package(require_signed=True)`` PASS on the tar.
  5. Ingest through the CURRENT production reader
     (:func:`parallax.apex.aphelion_ingest.ingest_package`).
  6. Read the claims back (store B) through the router's package-projection
     claim loader — the same unpack -> verify_package -> validate_signatures ->
     project call set the M7 read path runs.

Assertions:

  * **L1 (store level)** — per-claim canonical tuple
    ``(claim_id, body_canonical, polarity, target_claim_id, valid_from,
    valid_until, supersedes, subject)``: store A == store B as exact set
    equality.
  * **L2 (wire level)** — re-export store B to a second package; per claim_id
    the ``claims/<id>.md`` archive-member bytes are byte-identical to round 1,
    and the per-claim content-hash sets are equal. ``package_id`` / provenance
    legitimately differ on repack and are excluded from the byte comparison.

Why the router's claim loader (not ``router.query``) is store B: ``query``
applies R4 detection (supersession collapse, contradiction/ambiguity grouping),
so it deliberately does NOT return the raw claim set — it would drop superseded
chain members and reshape contradiction pairs. The loader projects exactly what
ingest persisted into the corpus, which is what "identical claims" must compare
against.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from aphelion.canonical_json import dumps as canonical_dumps
from aphelion.canonical_json import loads as canonical_loads
from aphelion.canonical_json import normalize as canonical_normalize
from aphelion.canonical_tar import TarMember, read_members
from aphelion.canonical_tar import pack as tar_pack
from aphelion.sig_pack import write_signatures_jsonl
from aphelion.signer import HMACSigner, compute_package_canonical_hash
from aphelion.verifier import VerifyResult, verify_package

from parallax.apex import subject_index
from parallax.apex.aphelion_export import (
    AphelionExportError,
    ExportClaim,
    build_claim_markdown,
    export_claims,
)
from parallax.apex.aphelion_ingest import IngestReport, ingest_package
from parallax.apex.audit_db import open_audit_db
from parallax.apex.router import ApexPublicReadRouter
from parallax.router.contracts import QueryRequest
from parallax.router.types import QueryType

# HMAC-SHA256 is the spec-§5 TEST-ONLY signer; its verifier emits a
# zero-non-repudiation UserWarning on every verify. It is expected and inert
# here (mirrors the M6 ingest suite) — filter it so the round trip stays quiet.
pytestmark = pytest.mark.filterwarnings("ignore:hmac-sha256")

_SIGNER_ID = "parallax-roundtrip-test-signer"
_SIGNER_KEY = b"parallax-roundtrip-hmac-key-32b!"  # HMAC key bytes; exactly 32
assert len(_SIGNER_KEY) == 32
_SIGNED_AT = "2026-01-01T00:00:00Z"

_N_PLAIN = 24
_N_VALID_TIME = 12
_N_CHAINS = 4
_CHAIN_LEN = 3
_N_PAIRS = 6
_CORPUS_SIZE = _N_PLAIN + _N_VALID_TIME + _N_CHAINS * _CHAIN_LEN + _N_PAIRS * 2  # 60


# ---------------------------------------------------------------------------
# Deterministic v7-UUID + corpus generation
# ---------------------------------------------------------------------------


def _uuid7(rng: random.Random) -> str:
    """A syntactically valid UUID v7 drawn from a seeded RNG (reproducible)."""
    hx = rng.randbytes(16).hex()
    variant = format((int(hx[16], 16) & 0x3) | 0x8, "x")
    return f"{hx[0:8]}-{hx[8:12]}-7{hx[13:16]}-{variant}{hx[17:20]}-{hx[20:32]}"


def _build_corpus(seed: int = 20_260_717) -> list[ExportClaim]:
    """Deterministic N>=50 synthetic corpus spanning every v0.3 variant.

    Duplicate-guard safety (M6 ``claim.duplicate_in_package`` fires on a shared
    ``(subject, polarity, valid_from)`` triple with all three present): only the
    valid-time claims carry ``valid_from``, and each of those has a UNIQUE
    subject — so no two claims collide. Chain members and contradiction pairs
    share a subject but omit ``valid_from`` entirely, so the triple is never
    complete for them.
    """
    rng = random.Random(seed)
    claims: list[ExportClaim] = []

    # 1. Plain claims — no R4 fields at all.
    for i in range(_N_PLAIN):
        claims.append(
            ExportClaim(
                claim_id=_uuid7(rng),
                claim_instance_id=_uuid7(rng),
                body=f"Plain claim number {i}.\n",
                subject=f"plain-subject-{i:02d}",
            )
        )

    # 2. Valid-time windows — unique subjects; alternate affirm / unknown to
    #    cover a third polarity value alongside the contradiction pairs.
    for i in range(_N_VALID_TIME):
        year = 2018 + i
        claims.append(
            ExportClaim(
                claim_id=_uuid7(rng),
                claim_instance_id=_uuid7(rng),
                body=f"Valid-time claim {i}.\n",
                subject=f"vt-subject-{i:02d}",
                polarity="affirm" if i % 2 == 0 else "unknown",
                valid_from=f"{year}-01-01T00:00:00Z",
                valid_until=f"{year}-12-31T23:59:59Z",
            )
        )

    # 3. Supersession chains — member k supersedes member k-1 (same subject).
    for c in range(_N_CHAINS):
        subject = f"chain-topic-{c}"
        prev: str | None = None
        for k in range(_CHAIN_LEN):
            cid = _uuid7(rng)
            claims.append(
                ExportClaim(
                    claim_id=cid,
                    claim_instance_id=_uuid7(rng),
                    body=f"Chain {c} revision {k}.\n",
                    subject=subject,
                    polarity="affirm",
                    supersedes=(prev,) if prev is not None else (),
                )
            )
            prev = cid

    # 4. Polarity / contradiction pairs — affirm + negate on a shared subject,
    #    each pointing at the other via target_claim_id.
    for p in range(_N_PAIRS):
        subject = f"contested-topic-{p}"
        a_id = _uuid7(rng)
        b_id = _uuid7(rng)
        claims.append(
            ExportClaim(
                claim_id=a_id,
                claim_instance_id=_uuid7(rng),
                body=f"Pair {p} supports the position.\n",
                subject=subject,
                polarity="affirm",
                target_claim_id=b_id,
            )
        )
        claims.append(
            ExportClaim(
                claim_id=b_id,
                claim_instance_id=_uuid7(rng),
                body=f"Pair {p} contradicts the position.\n",
                subject=subject,
                polarity="negate",
                target_claim_id=a_id,
            )
        )

    return claims


# ---------------------------------------------------------------------------
# Signing (operator concern; the exporter emits the unsigned canonical archive)
# ---------------------------------------------------------------------------


def _sign_package_in_place(tar_path: Path) -> None:
    """Attach a valid v0.5 HMAC signature envelope to an existing ``.aphelion.tar``.

    Reads the canonical manifest back out of the packed archive so the signed
    ``package_canonical_hash`` matches exactly what ``verify_package``
    recomputes. The claim member bytes are untouched by the repack.
    """
    members = read_members(tar_path.read_bytes())
    manifest_bytes = next(m.data for m in members if m.path == "manifest.json")
    manifest_obj = canonical_normalize(canonical_loads(manifest_bytes))
    claims_tuples = [
        (c["claim_id"], c["claim_instance_id"], c["hash"])
        for c in manifest_obj["claims"]
    ]
    pkg_hash = compute_package_canonical_hash(
        format_version=manifest_obj["format_version"],
        package_id=manifest_obj["package_id"],
        claims=claims_tuples,
    )
    signer = HMACSigner(_SIGNER_ID, _SIGNER_KEY)  # (signer_id, secret) positional
    envelope = signer.sign(package_canonical_hash=pkg_hash, signed_at_iso=_SIGNED_AT)
    mr = signer.manifest()
    sig_bytes = write_signatures_jsonl([envelope])
    signer_manifest_bytes = canonical_dumps(
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
    extra = [
        TarMember(path="signatures.jsonl", data=sig_bytes, is_dir=False),
        TarMember(path=f"signers/{_SIGNER_ID}.json", data=signer_manifest_bytes, is_dir=False),
    ]
    tar_path.write_bytes(tar_pack(members + extra))


# ---------------------------------------------------------------------------
# Canonical-tuple + archive helpers
# ---------------------------------------------------------------------------

_L1_FIELDS = (
    "claim_id",
    "body_canonical",
    "polarity",
    "target_claim_id",
    "valid_from",
    "valid_until",
    "supersedes",
    "subject",
)


def _tuple_from_export(claim: ExportClaim) -> tuple:
    return (
        claim.claim_id,
        claim.body.strip(),
        claim.polarity,
        claim.target_claim_id,
        claim.valid_from,
        claim.valid_until,
        tuple(sorted(claim.supersedes)),
        claim.subject,
    )


def _tuple_from_readback(claim: Mapping[str, object]) -> tuple:
    body = claim.get("body") or ""
    supersedes = claim.get("supersedes") or ()
    return (
        claim.get("claim_id"),
        str(body).strip(),
        claim.get("polarity"),
        claim.get("target_claim_id"),
        claim.get("valid_from"),
        claim.get("valid_until"),
        tuple(sorted(supersedes)),
        claim.get("subject"),
    )


def _readback_to_export_claim(claim: Mapping[str, object]) -> ExportClaim:
    """Reconstruct an :class:`ExportClaim` from a projected read-back claim.

    ``claim_instance_id`` is intentionally left ``None`` (the frontmatter never
    carried it) — the exporter derives a fresh one, which lands only in the
    manifest / provenance and is therefore excluded from the L2 byte comparison.
    """
    return ExportClaim(
        claim_id=str(claim["claim_id"]),
        claim_instance_id=None,
        body=str(claim.get("body") or ""),
        subject=_opt_str(claim.get("subject")),
        polarity=_opt_str(claim.get("polarity")),
        valid_from=_opt_str(claim.get("valid_from")),
        valid_until=_opt_str(claim.get("valid_until")),
        supersedes=tuple(str(s) for s in (claim.get("supersedes") or ())),
        target_claim_id=_opt_str(claim.get("target_claim_id")),
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _claim_members(tar_path: Path) -> dict[str, bytes]:
    """``{archive-path: bytes}`` for every ``claims/*.md`` member of a tar."""
    return {
        m.path: (m.data or b"")
        for m in read_members(tar_path.read_bytes())
        if m.path.startswith("claims/") and m.data is not None
    }


# ---------------------------------------------------------------------------
# Pipeline fixture: export -> sign -> verify -> ingest -> read back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundTrip:
    source_claims: tuple[ExportClaim, ...]
    read_back: tuple[Mapping[str, object], ...]
    verify_result: VerifyResult
    report: IngestReport
    audit_rows_on_disk: int
    round1_tar: Path
    tmp_path: Path
    audit_db_path: Path
    package_dir: Path


@pytest.fixture
def roundtrip(tmp_path: Path) -> RoundTrip:
    """Run the whole Parallax->Aphelion->ingest->read-back loop once."""
    package_dir = tmp_path / "packages"
    trust_dir = tmp_path / "trust_store"
    package_dir.mkdir()
    trust_dir.mkdir()
    # Seed the operator trust store with the signer secret (byte-identical) so
    # the HMAC key fingerprint is trusted by the M6 ingest gate.
    (trust_dir / f"{_SIGNER_ID}.pem").write_bytes(_SIGNER_KEY)
    audit_db_path = tmp_path / "roundtrip_audit.db"

    corpus = _build_corpus()
    package_id = _uuid7(random.Random(999))

    exported = export_claims(
        corpus,
        source_dir=tmp_path / "export_src",
        tar_path=package_dir / "roundtrip.aphelion.tar",
        package_id=package_id,
    )
    _sign_package_in_place(exported.tar_path)

    # Flow step: verify_package PASS on the signed tar (require_signed=True is
    # what the production ingest enforces internally).
    verify_result = verify_package(
        exported.tar_path, require_signed=True, require_notary=False
    )

    conn = open_audit_db(audit_db_path, validate=False)
    try:
        report = ingest_package(
            package_path=exported.tar_path,
            audit_conn=conn,
            package_dir=package_dir,
            trust_store_dir=trust_dir,
            session_id="roundtrip:ingest",
        )
    finally:
        conn.close()

    verify_conn = sqlite3.connect(audit_db_path)
    try:
        audit_rows = verify_conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()[0]
    finally:
        verify_conn.close()

    # Store B: the router's package-projection claim loader (the M7 read path's
    # unpack -> verify_package -> validate_signatures -> project call set). The
    # loader never touches audit_conn_provider, so a no-op provider is safe.
    router = ApexPublicReadRouter(
        package_dir=package_dir, audit_conn_provider=lambda: None
    )
    request = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="roundtrip")
    read_back = router._load_candidate_claims(request)

    return RoundTrip(
        source_claims=tuple(corpus),
        read_back=tuple(read_back),
        verify_result=verify_result,
        report=report,
        audit_rows_on_disk=audit_rows,
        round1_tar=exported.tar_path,
        tmp_path=tmp_path,
        audit_db_path=audit_db_path,
        package_dir=package_dir,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_corpus_covers_every_variant() -> None:
    """The synthetic corpus is >=50 claims and spans all four v0.3 variants."""
    corpus = _build_corpus()
    assert len(corpus) == _CORPUS_SIZE
    assert len(corpus) >= 50

    assert sum(1 for c in corpus if c.supersedes) >= _N_CHAINS  # supersession
    assert sum(1 for c in corpus if c.valid_from) == _N_VALID_TIME  # valid-time
    assert sum(1 for c in corpus if c.target_claim_id) == _N_PAIRS * 2  # polarity/target

    def _is_plain(c: ExportClaim) -> bool:  # no R4 fields whatsoever
        return (
            c.polarity is None
            and c.valid_from is None
            and not c.supersedes
            and c.target_claim_id is None
        )

    assert any(_is_plain(c) for c in corpus)
    assert {c.polarity for c in corpus} == {None, "affirm", "unknown", "negate"}

    # Every claim id is unique (set equality at L1 depends on this).
    assert len({c.claim_id for c in corpus}) == len(corpus)


def test_verify_package_passes_on_exported_tar(roundtrip: RoundTrip) -> None:
    """The exported+signed package verifies end-to-end with a single signer."""
    assert len(roundtrip.verify_result.envelopes) == 1
    assert roundtrip.verify_result.envelopes[0].signer_id == _SIGNER_ID


def test_production_ingest_accepts_every_claim(roundtrip: RoundTrip) -> None:
    """The current production ingest path maps every exported claim to an audit row."""
    assert isinstance(roundtrip.report, IngestReport)
    assert roundtrip.report.claims_ingested == _CORPUS_SIZE
    assert roundtrip.report.audit_rows_written == _CORPUS_SIZE
    assert roundtrip.audit_rows_on_disk == _CORPUS_SIZE
    assert roundtrip.report.signer_id == _SIGNER_ID


def test_L1_store_level_set_equality(roundtrip: RoundTrip) -> None:
    """Store A canonical tuples == store B canonical tuples (exact set equality)."""
    assert len(roundtrip.read_back) == _CORPUS_SIZE

    set_a = {_tuple_from_export(c) for c in roundtrip.source_claims}
    set_b = {_tuple_from_readback(c) for c in roundtrip.read_back}

    assert set_a == set_b, (
        f"L1 mismatch over fields {_L1_FIELDS}:\n"
        f"  only in A (source): {sorted(set_a - set_b)[:3]}\n"
        f"  only in B (readback): {sorted(set_b - set_a)[:3]}"
    )
    # Set equality already implies this, but pin the count so a silent
    # dedupe/collision (two claims collapsing to one tuple) fails loudly.
    assert len(set_a) == _CORPUS_SIZE
    assert len(set_b) == _CORPUS_SIZE


def test_L2_wire_level_byte_identity_on_reexport(roundtrip: RoundTrip) -> None:
    """Re-exporting store B reproduces byte-identical claim files + hash sets."""
    round2_claims = [_readback_to_export_claim(c) for c in roundtrip.read_back]

    # Repack => new package_id per spec (excluded from the byte comparison).
    export_claims(
        round2_claims,
        source_dir=roundtrip.tmp_path / "reexport_src",
        tar_path=roundtrip.tmp_path / "round2.aphelion.tar",
        package_id=_uuid7(random.Random(424242)),
    )

    round1 = _claim_members(roundtrip.round1_tar)
    round2 = _claim_members(roundtrip.tmp_path / "round2.aphelion.tar")

    # Same set of claim files (keyed by claims/<claim_id>.md).
    assert set(round1) == set(round2)
    assert len(round1) == _CORPUS_SIZE

    # Per-claim byte identity.
    diffs = [path for path in round1 if round1[path] != round2[path]]
    assert not diffs, f"L2 byte mismatch for {len(diffs)} claim file(s): {diffs[:3]}"

    # Content-hash sets equal (per-claim sha256 of the .md member bytes).
    hashes1 = {hashlib.sha256(data).hexdigest() for data in round1.values()}
    hashes2 = {hashlib.sha256(data).hexdigest() for data in round2.values()}
    assert hashes1 == hashes2
    assert len(hashes1) == _CORPUS_SIZE


def test_frontmatter_omits_absent_and_empty_fields() -> None:
    """Absent/empty claim fields are omitted (never emitted as null / [])."""
    plain = build_claim_markdown(
        ExportClaim(
            claim_id="0190ab63-5f8a-7a61-9b14-ffaa20c1d00d",
            claim_instance_id="0190ab63-5f8a-7a61-9b14-ffaa20c1d00e",
            body="Body.\n",
            subject="s",
            supersedes=(),  # empty -> must not appear as `supersedes: []`
        )
    ).decode("utf-8")
    assert "supersedes" not in plain
    assert "polarity" not in plain
    assert "target_claim_id" not in plain
    assert "null" not in plain
    assert "[]" not in plain
    # claim_id sorts before subject (ASCII-ascending canonical key order).
    assert plain.index("claim_id") < plain.index("subject")

    full = build_claim_markdown(
        ExportClaim(
            claim_id="0190ab63-5f8a-7a61-9b14-ffaa20c1d00d",
            claim_instance_id="0190ab63-5f8a-7a61-9b14-ffaa20c1d00e",
            body="Body.\n",
            subject="s",
            polarity="negate",
            supersedes=("0190ab63-5f8a-7a61-9b14-ffaa20c1d0aa",),
            target_claim_id="0190ab63-5f8a-7a61-9b14-ffaa20c1d0bb",
        )
    ).decode("utf-8")
    assert "polarity: \"negate\"" in full
    assert "supersedes:" in full
    assert "target_claim_id:" in full


# ---------------------------------------------------------------------------
# Path-traversal guard (W6 cross-review Tier-A): an unsafe claim_id must never
# become a filesystem write primitive (aphelion_export.py claims/<id>.md write).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../evil",
        "../../evil",
        "a/b",
        "..",
        "",
        "x" * 100,
        "claims/../../evil",
        "0190ab63-5f8a-7a61-9b14-ffaa20c1d00d\n",  # valid uuid + trailing newline
        "0190ab63-5f8a-7a61-9b14-ffaa20c1d00d\n../evil",  # newline injection
        "0190AB63-5F8A-7A61-9B14-FFAA20C1D00D",  # uppercase -> not the lowercase v7 form
    ],
)
def test_export_rejects_unsafe_claim_id(bad_id: str, tmp_path: Path) -> None:
    """An unsafe/invalid claim_id raises AphelionExportError and writes nothing.

    The guard runs before any dir creation / file write, so a traversal id can
    never materialize a file inside OR outside source_dir.
    """
    src = tmp_path / "pkg_src"
    tar = tmp_path / "out.aphelion.tar"
    claim = ExportClaim(
        claim_id=bad_id,
        claim_instance_id=_uuid7(random.Random(1)),
        body="body\n",
        subject="s",
    )
    with pytest.raises(AphelionExportError):
        export_claims(
            [claim], source_dir=src, tar_path=tar, package_id=_uuid7(random.Random(2))
        )
    assert not list(tmp_path.rglob("*.md")), "no claim file may be written"
    assert not list(tmp_path.rglob("*evil*")), "no traversal escapee may be written"
    assert not tar.exists()


def test_export_accepts_valid_claim_id(tmp_path: Path) -> None:
    """A well-formed lowercase UUID v7 exports normally (guard is not over-broad)."""
    pkg = export_claims(
        [
            ExportClaim(
                claim_id=_uuid7(random.Random(7)),
                claim_instance_id=_uuid7(random.Random(8)),
                body="ok\n",
                subject="s",
            )
        ],
        source_dir=tmp_path / "ok_src",
        tar_path=tmp_path / "ok.aphelion.tar",
        package_id=_uuid7(random.Random(9)),
    )
    assert pkg.tar_path.exists()
    assert len(pkg.claim_ids) == 1


# ---------------------------------------------------------------------------
# Ingest is inside the proof chain (codex P2-B): store B is read by re-scanning
# the package, so L1/L2 alone could pass even if ingest_package persisted audit
# rows with the wrong claim_ids. Assert on ingest's OWN artifacts.
# ---------------------------------------------------------------------------


def test_ingest_artifacts_match_corpus(roundtrip: RoundTrip) -> None:
    """The audit rows + subject index ingest wrote reflect the exact corpus."""
    corpus_ids = {c.claim_id for c in roundtrip.source_claims}
    corpus_subjects = {c.subject for c in roundtrip.source_claims if c.subject}
    assert len(corpus_ids) == _CORPUS_SIZE

    conn = sqlite3.connect(roundtrip.audit_db_path)
    try:
        rows = conn.execute(
            "SELECT claim_id, outcome, source FROM audit_row"
        ).fetchall()
    finally:
        conn.close()

    # Exact set + count over ingest's committed audit rows (not the re-scan).
    assert len(rows) == _CORPUS_SIZE
    assert {r[0] for r in rows} == corpus_ids
    assert {r[1] for r in rows} == {"hit"}
    assert {r[2] for r in rows} == {"aphelion"}

    # The M6 ingest path also maintains the free-text subject index. Every corpus
    # claim carries a subject, so it indexes exactly the corpus claim_ids +
    # subjects.
    index = subject_index.load_index(roundtrip.package_dir)
    assert index is not None, "ingest_package must have written the subject index"
    assert {entry.claim_id for entry in index.entries} == corpus_ids
    assert {entry.subject for entry in index.entries} == corpus_subjects


# ---------------------------------------------------------------------------
# Source-dir reuse hygiene (codex P2-A): a re-export into a reused source_dir
# must leave the exporter-owned claims/ subtree equal to exactly the new set —
# no stale claim files carried over.
# ---------------------------------------------------------------------------


def test_export_reuse_dir_drops_stale_claims(tmp_path: Path) -> None:
    """Re-exporting a smaller set into the same dir carries no stale claim file."""
    shared_src = tmp_path / "shared_src"
    rng = random.Random(4242)
    big = [
        ExportClaim(
            claim_id=_uuid7(rng),
            claim_instance_id=_uuid7(rng),
            body=f"claim {i}\n",
            subject=f"subject-{i}",
        )
        for i in range(3)
    ]
    export_claims(
        big,
        source_dir=shared_src,
        tar_path=tmp_path / "big.aphelion.tar",
        package_id=_uuid7(rng),
    )
    assert len(list((shared_src / "claims").glob("*.md"))) == 3

    # Re-export ONLY the first claim into the SAME source dir.
    small = [big[0]]
    pkg2 = export_claims(
        small,
        source_dir=shared_src,
        tar_path=tmp_path / "small.aphelion.tar",
        package_id=_uuid7(rng),
    )

    expected = {f"claims/{big[0].claim_id}.md"}
    # On-disk owned subtree == exactly the new set (this is what the fix repairs).
    on_disk = {f"claims/{p.name}" for p in (shared_src / "claims").glob("*.md")}
    assert on_disk == expected, f"stale claim files left behind: {on_disk - expected}"

    # And the packed archive fileset matches its manifest (verify_package passes),
    # containing exactly the smaller set.
    verify_package(pkg2.tar_path, require_signed=False, require_notary=False)
    packed = {
        m.path
        for m in read_members(pkg2.tar_path.read_bytes())
        if m.path.startswith("claims/")
    }
    assert packed == expected
