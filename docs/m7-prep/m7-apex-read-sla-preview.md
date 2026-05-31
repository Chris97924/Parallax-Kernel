# Apex M7 Public-Read — SLA Preview (spec §7.1 E.5)

- Generated: 2026-05-31T06:52:32Z
- Host: DESKTOP-0BKLFNS
- SLA preview budget (§4.2): **p99 < 100ms**, zero errors
- Iterations per package count: 200
- Harness: `scripts/m7_apex_read_stress.py` (real signed `.aphelion.tar`, no mocks)

## Package-count sweep

Each query runs the full §3.3 read path (unpack → verify_package →
validate_signatures → projection) across **every** package in the corpus
(per-read scan, §8.4 Q4 v0 default), so latency scales with package count.

| packages | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | errors | SLO |
|---|---|---|---|---|---|---|
| 1 | 15.037 | 19.405 | 23.38 | 39.602 | 0 | ✅ |
| 2 | 29.161 | 34.752 | 44.46 | 62.633 | 0 | ✅ |
| 4 | 57.656 | 63.131 | 75.513 | 107.28 | 0 | ✅ |
| 8 | 116.923 | 134.253 | 182.508 | 267.466 | 0 | ❌ |
| 16 | 230.853 | 257.581 | 289.171 | 394.057 | 0 | ❌ |

## Findings

- **Overall SLO (single-package non-empty corpus):** PASS — E.5 demonstrates p99 < 100ms on a non-empty corpus.
- **Per-read scan ceiling (§8.4 Q4):** p99 stays under the 100ms SLA up to **4 package(s)** at this iteration count on this host. Beyond the ceiling, the per-read full-scan design exceeds the budget — the §8.4 Q4 package-count ceiling is real and an index/refresh strategy (clock-tick or cache-miss rebuild) is required before the corpus grows past it. This is the load-bearing scaling note for the M7 implementation PR and an M8 follow-up.

> Numbers are machine- and load-dependent (measured locally, NOT on the ZenBook burn-in host — the burn-in clock is not interrupted). The M8 5k QPS pressure test is the hard fence; this is the §4.2 preview only.
