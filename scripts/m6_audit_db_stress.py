"""M6 audit-db throughput stress — sanity check that audit_writer wire
preserves 200x M6 ingest-flow headroom (per spec §7.7).

Reuses pattern from 2026-05-16 Task C /tmp/audit_db_stress.py (Chris-noted
P2 backlog #6: move /tmp scripts into scripts/).

Usage:
    python scripts/m6_audit_db_stress.py [--rows N] [--out PATH]

Defaults: N=1_000_000, OUT=/tmp/m6_audit_db_stress_report.json
SLO (from 5/16 Task C baseline):
    throughput >= 2000 rows/sec
    p99_latency_ms <= 10
    error_count == 0
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Defensive guard: never write to production DB path (P2 backlog #5)
# ---------------------------------------------------------------------------

def _build_stress_db_path() -> Path:
    tmp_dir = tempfile.gettempdir()
    db_path = Path(tmp_dir) / "m6_audit_db_stress.db"
    if "parallax-kernel/db" in str(db_path):
        print(
            f"FATAL: stress DB path {db_path!r} matches production guard "
            "'parallax-kernel/db'. Aborting.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return db_path


# ---------------------------------------------------------------------------
# Minimal audit_row schema (mirrors audit_db.py — standalone copy for stress)
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS audit_row (
    id                      INTEGER PRIMARY KEY,
    claim_id                TEXT NOT NULL,
    envelope_message_id     TEXT NOT NULL UNIQUE,
    outcome                 TEXT NOT NULL,
    package_id              TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    signer_id               TEXT NOT NULL,
    signer_manifest_digest  TEXT NOT NULL,
    source                  TEXT NOT NULL,
    ts                      TEXT NOT NULL
)
"""

_INSERT_SQL = (
    "INSERT INTO audit_row "
    "(claim_id, envelope_message_id, outcome, package_id, "
    " session_id, signer_id, signer_manifest_digest, source, ts) "
    "VALUES (?,?,?,?,?,?,?,?,?)"
)


def _open_stress_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(_DDL)
    return conn


# ---------------------------------------------------------------------------
# Row generation — exercises canonicalize_row write path
# ---------------------------------------------------------------------------

def _make_row_values(
    session_id: str,
    package_id: str,
    signer_id: str,
    ts: str,
) -> tuple[str, str, str, str, str, str, str, str, str]:
    """Build one mock R4 audit row without importing aphelion or real envelopes.

    Uses a zero-filled sha256 hex as signer_manifest_digest — valid per the
    audit_row schema's 64-char hex CHECK (blank '' is also allowed per spec,
    but 64-char is more representative of the real ingest path).
    """
    claim_id = str(uuid.uuid4())
    envelope_message_id = str(uuid.uuid4())
    outcome = "hit"
    signer_manifest_digest = "0" * 64
    source = "aphelion"
    return (
        claim_id,
        envelope_message_id,
        outcome,
        package_id,
        session_id,
        signer_id,
        signer_manifest_digest,
        source,
        ts,
    )


# ---------------------------------------------------------------------------
# Stress loop
# ---------------------------------------------------------------------------

def run_stress(conn: sqlite3.Connection, n_rows: int) -> dict[str, object]:
    session_id = f"stress:{int(time.time())}:{uuid.uuid4()}"
    package_id = str(uuid.uuid4())
    signer_id = "stress-signer-01"
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    latencies_ms: list[float] = []
    error_count = 0
    # Capture the first 3 distinct error messages so a CI failure surfaces
    # *why* the stress run lost rows instead of just a count. Bounded so a
    # cascading failure (e.g. disk full at row 500k) does not balloon the
    # report file with N near-identical strings.
    error_samples: list[str] = []
    seen_errors: set[str] = set()

    t_start = time.perf_counter()

    for _ in range(n_rows):
        row_vals = _make_row_values(session_id, package_id, signer_id, ts)
        t0 = time.perf_counter()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(_INSERT_SQL, row_vals)
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001
            error_count += 1
            msg = f"{type(exc).__name__}: {exc}"[:256]
            if msg not in seen_errors and len(error_samples) < 3:
                error_samples.append(msg)
                seen_errors.add(msg)
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
        t1 = time.perf_counter()
        latencies_ms.append((t1 - t0) * 1000.0)

    t_end = time.perf_counter()
    elapsed_s = t_end - t_start

    # Compute percentiles via simple sort (stdlib-only).
    sorted_lat = sorted(latencies_ms)
    n = len(sorted_lat)

    def _percentile(p: float) -> float:
        if n == 0:
            return 0.0
        idx = max(0, int(p / 100.0 * n) - 1)
        return sorted_lat[min(idx, n - 1)]

    throughput_rps = n_rows / elapsed_s if elapsed_s > 0 else 0.0
    p50 = _percentile(50)
    p95 = _percentile(95)
    p99 = _percentile(99)
    max_ms = max(sorted_lat) if sorted_lat else 0.0

    slo_pass = (
        throughput_rps >= 2000
        and p99 <= 10.0
        and error_count == 0
    )

    return {
        "rows": n_rows,
        "elapsed_s": round(elapsed_s, 3),
        "throughput_rps": round(throughput_rps, 1),
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "p99_ms": round(p99, 3),
        "max_ms": round(max_ms, 3),
        "error_count": error_count,
        "error_samples": error_samples,
        "slo_pass": slo_pass,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="M6 audit-db throughput stress test (spec §7.7)."
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=1_000_000,
        metavar="N",
        help="number of audit rows to insert (default: 1_000_000)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=os.path.join(tempfile.gettempdir(), "m6_audit_db_stress_report.json"),
        metavar="PATH",
        help="JSON report output path (default: <tempdir>/m6_audit_db_stress_report.json)",
    )
    args = parser.parse_args()

    db_path = _build_stress_db_path()
    # Clean up any leftover DB from a previous run.
    if db_path.exists():
        db_path.unlink()

    print(f"stress: opening DB at {db_path}", file=sys.stderr)
    conn = _open_stress_db(db_path)

    try:
        print(f"stress: inserting {args.rows:,} rows …", file=sys.stderr)
        report = run_stress(conn, args.rows)
    finally:
        conn.close()
        # Clean up the stress DB after use.
        try:
            db_path.unlink()
        except OSError:
            pass

    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

    if report["slo_pass"]:
        print("SLO PASS", file=sys.stderr)
        return 0
    else:
        print(
            f"SLO FAIL: throughput={report['throughput_rps']} rps "
            f"(need >=2000), p99={report['p99_ms']} ms (need <=10), "
            f"errors={report['error_count']}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
