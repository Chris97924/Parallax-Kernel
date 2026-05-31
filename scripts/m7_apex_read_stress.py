"""M7 Apex public-read SLA-preview stress harness (spec §4.2 + §7.1 E.5).

Drives the real :class:`parallax.apex.router.ApexPublicReadRouter` against a
synthetic non-empty corpus of HMAC-signed ``.aphelion.tar`` packages and
measures per-query wall-clock latency. Produces the E.5 SLA-preview artifact:
a JSON report (and optional markdown) demonstrating p99 < 100ms on a non-empty
corpus, plus a package-count sweep that surfaces the §8.4 Q4 per-read scan
ceiling (the router verifies EVERY package on every query, so query latency
scales with package count).

No mocks: every package is built + signed with the real ``aphelion`` lib,
mirroring tests/apex/test_router.py's builder. Latency is measured
independently of the Prometheus histogram (its own stdlib percentiles), so this
harness validates the SLA regardless of bucket configuration.

Usage:
    python scripts/m7_apex_read_stress.py [--package-counts 1,2,4,8,16]
                                          [--iters 100] [--out PATH] [--md PATH]

SLO (spec §4.2): p99 < 100ms on a non-empty corpus, error_count == 0.
"""

# ruff: noqa: E402 — third-party/local imports follow a deliberate sys.path
# bootstrap (below) so the harness always binds this worktree's parallax.

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Bootstrap: ensure THIS repo root wins over any editable `parallax` install
# pointing elsewhere, so the harness always exercises the worktree's router.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aphelion.canonical_json import dumps as canonical_dumps
from aphelion.canonical_json import normalize as canonical_normalize
from aphelion.canonical_tar import TarMember, read_members
from aphelion.canonical_tar import pack as tar_pack
from aphelion.packer import pack as aphelion_pack
from aphelion.sig_pack import write_signatures_jsonl
from aphelion.signer import HMACSigner, compute_package_canonical_hash

from parallax.apex.audit_db import open_audit_db
from parallax.apex.router import ApexPublicReadRouter
from parallax.router.contracts import QueryRequest
from parallax.router.types import QueryType

# SLA preview budget (ms), spec §4.2.
SLA_P99_MS = 100.0

_SUBJECT = "retrieval-quality"
_SIGNER_ID = "m7-stress-signer"
_BUILDER_HMAC = b"m7-test-hmac-32-bytes-padding!!!"  # exactly 32 bytes (test-only)
_SIGNED_AT = "2026-05-17T00:00:00Z"
_EVENT_ID = (
    "01963f7d-7000-7000-8000-eeee00000001"  # shared across stress pkgs — test-only, never ingested
)


# ---------------------------------------------------------------------------
# Synthetic package builder (mirrors tests/apex/test_router.py)
# ---------------------------------------------------------------------------


def _claim_md(claim_id: str) -> bytes:
    fields = {
        "body_format": "markdown",
        "claim_id": claim_id,
        "polarity": "affirm",
        "subject": _SUBJECT,
        "title": "M7 stress claim",
        "valid_from": "2026-01-01T00:00:00Z",
    }
    lines = ["---"]
    for key in sorted(fields):
        lines.append(f'{key}: "{fields[key]}"')
    lines.append("---")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _build_signed_package(*, work: Path, package_dir: Path, index: int) -> Path:
    """Build one HMAC-signed ``.aphelion.tar`` into ``package_dir``."""
    suffix = f"{index:08d}"
    package_id = f"01963f7d-7000-7000-8000-9999{suffix}"
    claim_id = f"01963f7d-7000-7000-8000-c1a1{suffix}"
    instance_id = f"01963f7d-7000-7000-8000-1111{suffix}"

    src = work / f"src_{suffix}"
    (src / "claims").mkdir(parents=True, exist_ok=True)
    claim_rel = f"claims/{claim_id}.md"
    claim_bytes = _claim_md(claim_id)
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
        "producer": "parallax-m7-stress",
        "provenance_path": "provenance.jsonl",
    }
    (src / "manifest.json").write_bytes(canonical_dumps(canonical_normalize(manifest)))

    event = {
        "actor": "m7-stress",
        "claim_id": claim_id,
        "claim_instance_id": instance_id,
        "event_id": _EVENT_ID,
        "event_type": "create",
        "timestamp": "2026-05-17T00:00:00Z",
    }
    (src / "provenance.jsonl").write_bytes(canonical_dumps(canonical_normalize(event)))

    tar_path = package_dir / f"pkg_{suffix}.aphelion.tar"
    aphelion_pack(src, tar_path)

    manifest_obj = canonical_normalize(json.loads((src / "manifest.json").read_bytes()))
    claims_tuples = [
        (c["claim_id"], c["claim_instance_id"], c["hash"]) for c in manifest_obj["claims"]
    ]
    pkg_hash = compute_package_canonical_hash(
        format_version=manifest_obj["format_version"],
        package_id=manifest_obj["package_id"],
        claims=claims_tuples,
    )
    signer = HMACSigner(_SIGNER_ID, _BUILDER_HMAC)
    envelope = signer.sign(package_canonical_hash=pkg_hash, signed_at_iso=_SIGNED_AT)
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
        TarMember(path=f"signers/{_SIGNER_ID}.json", data=sm_bytes, is_dir=False),
    ]
    tar_path.write_bytes(tar_pack(existing + extra))
    return tar_path


def _query() -> QueryRequest:
    return QueryRequest(
        query_type=QueryType.RECENT_CONTEXT,
        user_id="m7-stress-user",
        q=_SUBJECT,
        params={"subject": _SUBJECT},
    )


# ---------------------------------------------------------------------------
# Percentiles (stdlib only — mirrors scripts/m6_audit_db_stress.py)
# ---------------------------------------------------------------------------


def _percentile(sorted_lat: list[float], p: float) -> float:
    """Nearest-rank percentile (stdlib). Uses ``ceil`` so the worst sample is
    included at small ``n`` — a floor-based index would drop the tail (e.g.
    p99 of n=5 must return the 5th value, not the 4th), which would let a real
    SLA breach hide in a low-``--iters`` run."""
    n = len(sorted_lat)
    if n == 0:
        return 0.0
    idx = math.ceil(p / 100.0 * n) - 1
    return sorted_lat[min(max(idx, 0), n - 1)]


# ---------------------------------------------------------------------------
# Single measurement at a given package count
# ---------------------------------------------------------------------------


def measure(package_count: int, iters: int) -> dict[str, Any]:
    """Build ``package_count`` signed packages and time ``iters`` queries.

    Returns a result dict with stdlib-computed latency percentiles, the error
    count, and ``slo_pass`` (p99 < 100ms and zero errors). Builds everything in
    a fresh temp dir that is removed before returning.
    """
    if package_count < 1:
        raise ValueError("package_count must be >= 1 (corpus must be non-empty)")
    if iters < 1:
        raise ValueError("iters must be >= 1")

    with tempfile.TemporaryDirectory(prefix="m7_stress_") as tmp:
        root = Path(tmp)
        package_dir = root / "packages"
        package_dir.mkdir()
        work = root / "work"
        work.mkdir()

        for i in range(package_count):
            _build_signed_package(work=work, package_dir=package_dir, index=i)

        conn = None
        try:
            conn = open_audit_db(root / "stress_audit.db", validate=False)
            router = ApexPublicReadRouter(
                package_dir=package_dir,
                audit_conn_provider=lambda: conn,
            )
            request = _query()

            latencies_ms: list[float] = []
            error_count = 0
            error_samples: list[str] = []
            seen: set[str] = set()
            empty_hits = 0

            for _ in range(iters):
                t0 = time.perf_counter()
                try:
                    evidence = router.query(request)
                    if not evidence.hits:
                        empty_hits += 1
                except Exception as exc:  # noqa: BLE001 — stress harness records, never crashes
                    error_count += 1
                    msg = f"{type(exc).__name__}: {exc}"[:256]
                    if msg not in seen and len(error_samples) < 3:
                        error_samples.append(msg)
                        seen.add(msg)
                t1 = time.perf_counter()
                latencies_ms.append((t1 - t0) * 1000.0)
        finally:
            if conn is not None:
                conn.close()

    sorted_lat = sorted(latencies_ms)
    p50 = _percentile(sorted_lat, 50)
    p95 = _percentile(sorted_lat, 95)
    p99 = _percentile(sorted_lat, 99)
    max_ms = sorted_lat[-1] if sorted_lat else 0.0
    slo_pass = p99 < SLA_P99_MS and error_count == 0

    return {
        "package_count": package_count,
        "iters": iters,
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "p99_ms": round(p99, 3),
        "max_ms": round(max_ms, 3),
        "error_count": error_count,
        "error_samples": error_samples,
        "empty_results": empty_hits,
        "slo_pass": slo_pass,
    }


def run_sweep(package_counts: list[int], iters: int) -> dict[str, Any]:
    """Run :func:`measure` across ``package_counts`` and derive the SLA ceiling."""
    results = [measure(count, iters) for count in sorted(set(package_counts))]

    def _passes(r: dict[str, Any]) -> bool:
        return r["p99_ms"] < SLA_P99_MS and r["error_count"] == 0

    # Ceiling = largest corpus size that passes AND for which every smaller size
    # also passed. Walking from the smallest and stopping at the first failure
    # avoids reporting a misleadingly-high ceiling when a noisy run is
    # non-monotone (e.g. N=2 fails but N=4 passes) — the markdown artifact's
    # "safe up to N packages" claim must not lie.
    ceiling: int | None = None
    for r in results:  # results are sorted ascending by package_count
        if not _passes(r):
            break
        ceiling = r["package_count"]
    # Overall SLO: the smallest non-empty corpus (single package) must meet the
    # p99<100ms budget with zero errors. The ceiling documents §8.4 Q4 scaling.
    smallest = min(results, key=lambda r: r["package_count"])
    return {
        "sla_p99_ms": SLA_P99_MS,
        "iters": iters,
        "results": results,
        "p99_under_sla_ceiling_packages": ceiling,
        "slo_pass": smallest["p99_ms"] < SLA_P99_MS and smallest["error_count"] == 0,
    }


# ---------------------------------------------------------------------------
# Markdown report rendering (E.5 artifact)
# ---------------------------------------------------------------------------


def render_markdown(report: dict[str, Any], *, generated_at: str, host: str) -> str:
    lines = [
        "# Apex M7 Public-Read — SLA Preview (spec §7.1 E.5)",
        "",
        f"- Generated: {generated_at}",
        f"- Host: {host}",
        f"- SLA preview budget (§4.2): **p99 < {report['sla_p99_ms']:.0f}ms**, zero errors",
        f"- Iterations per package count: {report['iters']}",
        "- Harness: `scripts/m7_apex_read_stress.py` (real signed `.aphelion.tar`, no mocks)",
        "",
        "## Package-count sweep",
        "",
        "Each query runs the full §3.3 read path (unpack → verify_package →",
        "validate_signatures → projection) across **every** package in the corpus",
        "(per-read scan, §8.4 Q4 v0 default), so latency scales with package count.",
        "",
        "| packages | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | errors | SLO |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        verdict = "✅" if (r["p99_ms"] < report["sla_p99_ms"] and r["error_count"] == 0) else "❌"
        lines.append(
            f"| {r['package_count']} | {r['p50_ms']} | {r['p95_ms']} | "
            f"{r['p99_ms']} | {r['max_ms']} | {r['error_count']} | {verdict} |"
        )
    ceiling = report["p99_under_sla_ceiling_packages"]
    lines += [
        "",
        "## Findings",
        "",
        f"- **Overall SLO (single-package non-empty corpus):** "
        f"{'PASS' if report['slo_pass'] else 'FAIL'} — E.5 demonstrates p99 < "
        f"{report['sla_p99_ms']:.0f}ms on a non-empty corpus.",
        f"- **Per-read scan ceiling (§8.4 Q4):** p99 stays under the "
        f"{report['sla_p99_ms']:.0f}ms SLA up to **{ceiling} package(s)** at this "
        "iteration count on this host. Beyond the ceiling, the per-read full-scan "
        "design exceeds the budget — the §8.4 Q4 package-count ceiling is real and "
        "an index/refresh strategy (clock-tick or cache-miss rebuild) is required "
        "before the corpus grows past it. This is the load-bearing scaling note for "
        "the M7 implementation PR and an M8 follow-up.",
        "",
        "> Numbers are machine- and load-dependent (measured locally, NOT on the "
        "ZenBook burn-in host — the burn-in clock is not interrupted). The M8 5k "
        "QPS pressure test is the hard fence; this is the §4.2 preview only.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_counts(value: str) -> list[int]:
    return [int(p.strip()) for p in value.split(",") if p.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apex M7 public-read SLA-preview stress (§4.2 / E.5)."
    )
    parser.add_argument(
        "--package-counts",
        type=_parse_counts,
        default=[1, 2, 4, 8, 16],
        metavar="N,N,...",
        help="comma-separated corpus sizes to sweep (default: 1,2,4,8,16)",
    )
    parser.add_argument(
        "--iters", type=int, default=100, metavar="N", help="queries per count (default: 100)"
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(Path(tempfile.gettempdir()) / "m7_apex_read_stress_report.json"),
        metavar="PATH",
        help="JSON report output path",
    )
    parser.add_argument(
        "--md", type=str, default=None, metavar="PATH", help="optional markdown report path"
    )
    args = parser.parse_args(argv)

    print(
        f"stress: sweeping package counts {args.package_counts} x {args.iters} iters …",
        file=sys.stderr,
    )
    report = run_sweep(args.package_counts, args.iters)

    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

    if args.md:
        generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        import socket

        md = render_markdown(report, generated_at=generated_at, host=socket.gethostname())
        Path(args.md).write_text(md, encoding="utf-8")
        print(f"stress: wrote markdown report to {args.md}", file=sys.stderr)

    if report["slo_pass"]:
        print("SLO PASS (single-package corpus p99 < 100ms)", file=sys.stderr)
        return 0
    print(
        f"SLO FAIL: smallest-corpus p99 >= {SLA_P99_MS}ms or errors present",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
