# Apex M7 Public-Read — SLA Preview (spec §7.1 E.5)

- Generated: 2026-05-31T05:55:57Z
- Host: DESKTOP-0BKLFNS
- SLA preview budget (§4.2): **p99 < 100ms**, zero errors
- Iterations per package count: 100
- Harness: `scripts/m7_apex_read_stress.py` (real signed `.aphelion.tar`, no mocks)

## Package-count sweep

Each query runs the full §3.3 read path (unpack → verify_package →
validate_signatures → projection) across **every** package in the corpus
(per-read scan, §8.4 Q4 v0 default), so latency scales with package count.

| packages | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | errors | SLO |
|---|---|---|---|---|---|---|
| 1 | 14.862 | 16.492 | 18.168 | 22.341 | 0 | ✅ |
| 2 | 29.092 | 32.955 | 39.498 | 48.953 | 0 | ✅ |
| 4 | 57.978 | 65.743 | 78.368 | 96.252 | 0 | ✅ |
| 8 | 115.159 | 124.336 | 132.123 | 171.122 | 0 | ❌ |
| 16 | 228.649 | 244.179 | 248.778 | 310.378 | 0 | ❌ |

## Findings

- **Overall SLO (single-package non-empty corpus):** PASS — E.5 demonstrates p99 < 100ms on a non-empty corpus.
- **Per-read scan ceiling (§8.4 Q4):** p99 stays under the 100ms SLA up to **4 package(s)** at this iteration count on this host. Beyond the ceiling, the per-read full-scan design exceeds the budget — the §8.4 Q4 package-count ceiling is real and an index/refresh strategy (clock-tick or cache-miss rebuild) is required before the corpus grows past it. This is the load-bearing scaling note for the M7 implementation PR and an M8 follow-up.

> Numbers are machine- and load-dependent (measured locally, NOT on the ZenBook burn-in host — the burn-in clock is not interrupted). The M8 5k QPS pressure test is the hard fence; this is the §4.2 preview only.
