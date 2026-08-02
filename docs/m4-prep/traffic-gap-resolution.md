# M4 GATE 2A Traffic Gap — Resolution Plan

**Status:** Normative (Chris-pinned 2026-05-09 via xcouncil consensus, 6/8 majority)
**Date:** 2026-05-09
**Owner:** Parallax-Kernel
**Blocks:** B1 (14-day M3 corpus DoD), B2 (`aphelion_unreachable_rate < 0.5%`)
**Resolves:** Day-1 burn-in finding `traffic_started_24h=false` + B1/B2 metrics `NO_DATA`

---

## 1. Problem (re-stated for clarity)

The M4 GATE 2A burn-in stack on ZenBook is healthy (Prometheus / Alertmanager / Grafana all GREEN per `2026-05-09 day-1` snapshot at `.omc/burn-in/day-1-snapshot.md`). But the burn-in monitor reports:

```
traffic_started_24h: false
B1 (M3 corpus dual-read continuity): metric series ABSENT
B2 (aphelion_unreachable_rate < 0.5%): metric series ABSENT
```

Root cause: the dual-read code path on ZenBook is wired but no production traffic exercises it. Counters never `inc()` so Prometheus has no series to scrape. The 14-day burn-in clock runs in calendar time but the data window is always empty — B1/B2 cannot turn GREEN no matter how many days pass.

This blocks M4 DoD, which blocks M5 entry, which blocks the entire Apex M5 dual-write rollout.

## 2. Decision (xcouncil consensus 2026-05-09)

**Verdict: (d) Hybrid — synthetic loader during burn-in, swap to natural traffic when production query exists** (6/8 substantive votes: Codex, Laguna, Nemotron, MiniMax, MiMo, GPT-OSS; 1 Sonnet for pure-synthetic, 1 Qwen for Stage @0%).

- (a) wait-for-natural rejected: ZenBook is a single-user dev box, weekend traffic can be zero, no ETA bound.
- (b) pure synthetic rejected: synthetic-only metrics could mask real production bugs (B2 `aphelion_unreachable_rate` measured against synthetic loader is meaningless for prod-fitness).
- (c) Stage @0% pre-roll rejected: requires GATE 3 (binary build, canary registry, feature flag, LB rule), which is Chris-gated and not bypassable while Chris is out.
- (d) hybrid wins: synthetic starts the 14-day clock immediately (no Chris action), explicit cutover criteria swap to natural traffic when production exists, label-discrimination prevents synthetic from masking prod bugs.

## 3. Hybrid Design

### 3.1 Synthetic loader

A small background service runs alongside `parallax-server` on ZenBook:

```
Service:        parallax-burn-in-synth-loader
Schedule:       continuous (systemd user-service or pm2-managed background process)
Rate:           1 query / second (low constant rate; sufficient to keep counters live)
Query shape:    realistic dual-read query against M3 corpus claims
Authentication: localhost-only, bypasses external auth
Label:          every emitted metric carries `traffic_source="synthetic"`
```

**Why 1 qps**: high enough that Prometheus's 30-second scrape sees motion every interval; low enough that resource cost is negligible (< 0.1% CPU on ZenBook). Higher rates risk masking edge cases that only show under low load.

### 3.2 Metric labeling discipline

All M4 burn-in counters/histograms MUST carry a `traffic_source` label with values:

- `"synthetic"` — emitted by the loader
- `"natural"` — emitted by any non-loopback request the production server processes

The dashboards already deployed (Grafana `parallax-m4-canary-stage-1`) need a one-line PromQL update on each panel's query:

```promql
# Before
sum by (instance) (rate(parallax_aphelion_unreachable_total[5m]))

# After (hybrid-aware)
sum by (instance, traffic_source) (rate(parallax_aphelion_unreachable_total[5m]))
```

This makes the synthetic vs natural split visible at-a-glance.

### 3.3 Cutover criteria

The synthetic loader does NOT shut off when natural traffic appears. Both run side-by-side. The DoD evaluator (`parallax canary --dod`) is what changes behavior:

```
B2 GREEN condition (post-2026-05-09 update):

Phase 1 — 14-day clock proof (synthetic-aware):
  series_exists(parallax_aphelion_unreachable_total{traffic_source=~"synthetic|natural"})
  for ≥ 14 calendar days
  → satisfies B1's "metric series exists for window" requirement

Phase 2 — semantic GREEN (natural-required):
  rate(parallax_aphelion_unreachable_total{traffic_source="natural"}[1h])
       /
  rate(parallax_aphelion_total{traffic_source="natural"}[1h])
       < 0.005   (i.e. < 0.5%)
  for ≥ 7 of the last 14 days where natural sample size ≥ 100 calls/24h
  → satisfies B2's semantic intent

If natural sample never reaches 100/24h within the 14d window:
  raise WARN_NATURAL_INSUFFICIENT — Chris-gated decision whether to extend, run with reduced confidence, or proceed with synthetic-validated B2 with caveat documented in DoD report
```

This double gate (synthetic for clock, natural for semantics) addresses both Sonnet's "synthetic-only could mask bugs" and Codex's "natural-required for production fitness" concerns.

#### 2026-08-02 — WARN_NATURAL_INSUFFICIENT resolved (Chris decision)

The `WARN_NATURAL_INSUFFICIENT` branch above is now settled for M4.

Measured state: natural sample size has been **0 for the entire burn-in**. Every `/query` since the loader started on 2026-06-10 carries `user_id=parallax-burn-in-synth`, and `parallax_aphelion_total{traffic_source="natural"}` has never been non-zero. Phase 2 was therefore not satisfiable at any point in the 14-day window — not "failed", but unmeasurable for want of a denominator.

Decision:

- **Gate-5 (2026-07-26) closes out as a valid Phase-1 clock proof.** The series existed continuously for the required window and the clock ran. That is exactly what Phase 1 asserts, and it stands.
- Gate-5's items 2–4 (`discrepancy_rate` 0.000000, `aphelion_unreachable_rate` 0.000000, min_hits 82,387) were measured on 100% synthetic traffic that returns zero hits from **both** stores. Two empty result sets match trivially, so those zeros say the burn-in claim ids are absent from both stores — not that Parallax and Aphelion agree. They are not semantic validation and must not be cited as such.
- **Phase 2 (semantic GREEN, ≥100 natural calls/24h) is re-homed to the natural-traffic milestone.** It is not an M4 exit condition. M4 closes on the Phase-1 clock proof.

This does not weaken the gate — it moves it to the first milestone where it can actually be evaluated. The metrics layer needed to evaluate it landed the same day: the three dual-read DoD gauges are now partitioned by `traffic_source`, so `{traffic_source="natural"}` is a selector that resolves rather than one that silently matches every series. Until natural traffic exists that selector returns no data, which is the honest reading.

See `.omc/reports/tailsweep-runbook-20260802.md` for the measurement that drove this.

### 3.4 PENDING_IMPLEMENTATION gate (added 2026-05-09 Phase-4 review)

Items 4.7 (`parallax canary --dod` Phase-1/Phase-2 split) and 4.8 (burn-in monitor split) were PENDING at freeze time and shipped via PR #50 on 2026-05-10. The DoD evaluator now has awareness of `traffic_source` and can distinguish synthetic from natural traffic.

**Mandatory gate**: until items 4.7 AND 4.8 are merged, `parallax canary --dod` MUST return `PENDING_IMPLEMENTATION` (NOT pass / NOT fail) for the B1 and B2 checks. The CLI exits with status code 65 (`EX_DATAERR` — sysexits) and prints:

```
B1: PENDING_IMPLEMENTATION — synthetic/natural split logic not landed (see traffic-gap-resolution.md item 4.7)
B2: PENDING_IMPLEMENTATION — synthetic/natural split logic not landed (see traffic-gap-resolution.md item 4.7)
```

This prevents the failure mode where the burn-in clock runs, synthetic-only metric series exists, and the evaluator naively reads `series_exists=true` to declare Phase 1 GREEN against synthetic-only data — silently validating nothing.

The gate auto-removes when items 4.7 and 4.8 ship. No manual flag flip.

## 4. Implementation work (autopilot scope)

| # | Task | Owner | Status |
|---|---|---|---|
| 4.1 | Spec the loader (this doc) | Claude autopilot | DONE 2026-05-09 |
| 4.2 | Add `traffic_source` label to all M4 burn-in metrics in `parallax/router/dual_read.py` + `parallax/router/discrepancy_live.py` | Claude / Codex follow-up | DONE 2026-05-13 (this PR) |
| 4.3 | Update Prometheus rules in `prometheus/rules/parallax-dual-read.rules.yml` to group by `traffic_source` | Claude / Codex follow-up | DONE 2026-05-13 (this PR) |
| 4.4 | Update Grafana dashboard JSON to split panels by `traffic_source` | Claude / Codex follow-up | DONE 2026-05-13 (this PR) |
| 4.5 | Write loader script (`scripts/burn-in-synth-loader.py` — 1 qps, M3-corpus-shape queries, localhost) | Claude / Codex follow-up | DONE 2026-05-10 (PR #50) |
| 4.6 | systemd user-service unit (`~/.config/systemd/user/parallax-burn-in-synth-loader.service`) | Claude / Codex follow-up | DONE 2026-05-10 (PR #50) |
| 4.7 | Update `parallax canary --dod` to honor the Phase 1 / Phase 2 split | Claude / Codex follow-up | DONE 2026-05-10 (PR #50) |
| 4.8 | Update `scripts/burn-in-monitor.sh` to surface `synthetic_started_24h` + `natural_started_24h` separately | Claude / Codex follow-up | DONE 2026-05-10 (PR #50) |
| 4.9 | Append §8.8 to `m4-m5-readiness-report.md` documenting the hybrid resolution | Claude / Codex follow-up | DONE 2026-05-13 (this PR) |

Items 4.2–4.9 are mechanical and chunk-able. They are NOT Chris-gated for design (this doc settles design). Chris-gated only for: (a) deciding when to run them in the M4 timeline, (b) approving the small PR that lands them.

## 5. Loader skeleton (reference)

This is reference-level only — not the production script. The follow-up implementation will live in `scripts/burn-in-synth-loader.py` with proper config + signal handling.

```python
"""
parallax burn-in synthetic loader — 1 qps localhost dual-read driver.

Purpose: keep B1/B2 metric series alive during M4 burn-in when natural
production traffic is zero. Pairs with hybrid DoD logic in
parallax canary --dod (see traffic-gap-resolution.md §3.3).

CRITICAL: every metric this loader's queries trigger MUST carry
traffic_source="synthetic". The server-side metric emission code
detects the X-Parallax-Traffic-Source header and labels accordingly
(see §6 normative server behavior for header-absent default).
"""
from __future__ import annotations

import logging
import time

import httpx

LOG = logging.getLogger("parallax.burn_in.synth_loader")

ENDPOINT = "http://127.0.0.1:8000/v1/dual-read"  # localhost ZenBook
SAMPLE_KEYS = [
    # M3 corpus claim ids — populate from a fixture file rather than hardcoding
]
HEADERS = {
    "X-Parallax-Traffic-Source": "synthetic",
    "X-Parallax-Synth-Marker": "burn-in-loader-v1",
}

# After this many consecutive errors, exit so systemd Restart=always fires
# rather than silently looping at 1 qps with the metric series going dark.
CONSECUTIVE_ERROR_BUDGET = 30


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
    )
    if not SAMPLE_KEYS:
        LOG.error("synth loader requires M3 corpus sample fixture; aborting")
        raise SystemExit(2)
    client = httpx.Client(timeout=5.0, headers=HEADERS)
    idx = 0
    consecutive_errors = 0
    while True:
        key = SAMPLE_KEYS[idx % len(SAMPLE_KEYS)]
        idx += 1
        try:
            r = client.get(f"{ENDPOINT}?key={key}")
            LOG.info("synth_qry key=%s status=%d", key, r.status_code)
            consecutive_errors = 0
        except httpx.HTTPError as exc:
            consecutive_errors += 1
            LOG.error(
                "synth_qry key=%s exc=%s msg=%s consecutive=%d",
                key, exc.__class__.__name__, exc, consecutive_errors,
            )
            if consecutive_errors >= CONSECUTIVE_ERROR_BUDGET:
                LOG.critical(
                    "synth loader exhausted error budget=%d; exiting so systemd restarts",
                    CONSECUTIVE_ERROR_BUDGET,
                )
                raise SystemExit(75)  # EX_TEMPFAIL — invite restart
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
```

Logging discipline notes:

- `LOG.info` for every successful query, `LOG.error` for connection/HTTP failures, `LOG.critical` for budget exhaustion. journald renders levels distinctly — error skim is reliable.
- `CONSECUTIVE_ERROR_BUDGET` (30 ≈ 30s at 1qps) ensures `Restart=always` actually fires rather than the loader silently looping forever on a broken endpoint.
- Independent of this, `synth_loader_heartbeat_total` Prom counter (incremented inside the loop) plus alert on `absent(synth_loader_heartbeat_total[5m])` covers the "process alive but stuck" case.

The server-side change is the load-bearing piece: emitted metrics must inspect the request and label `traffic_source` accordingly. That landed in items 4.2 above. Server-side header-absent default is normative per §6 below.

## 6. Server-side normative behavior

When the Parallax server receives a request, it labels the resulting `traffic_source` metric label per the following rules (normative):

| `X-Parallax-Traffic-Source` request header value | `traffic_source` metric label |
|---|---|
| `"synthetic"` | `"synthetic"` |
| `"natural"` | `"natural"` |
| absent OR unrecognized value | `"natural"` (default — see rationale) |

The "absent → natural" default is intentional: any unlabeled production traffic is real production traffic and MUST be counted as such. Defaulting to `"unknown"` or omitting the label was rejected because:

- Omitting the label silently breaks `{traffic_source="natural"}` PromQL filters — unlabeled natural queries would be invisible to the Phase-2 GREEN evaluator while still incrementing the underlying counter, leading to "WARN_NATURAL_INSUFFICIENT never fires because natural traffic is happening but unlabeled".
- An `"unknown"` value introduces a third class that no PromQL alert is wired against — same silent-failure surface.

Synthetic traffic is therefore the **only** traffic that requires opt-in labeling (via the loader-set header). Any client that forgets the header gets counted as natural, which is the correct fail-safe for Phase-2 GREEN semantics.

Server implementation MUST:

- Reject neither absent nor unrecognized header — both fall to the default
- Lowercase-compare header values (`"Synthetic"` and `"synthetic"` both → `"synthetic"`)
- Trim whitespace from header value before matching
- Add a unit test asserting every M4 burn-in metric has `traffic_source ∈ {"synthetic", "natural"}` and never the empty string or absent label

## 7. Risks

| Risk | Mitigation |
|---|---|
| Synthetic load shapes don't match production | Use real M3-corpus claim ids and the same endpoint shape; document deviations in §3.1 if any are needed for performance reasons |
| Server forgets to label by `traffic_source` | Unit test per §6 last bullet asserts every M4 burn-in metric carries the label and a value from `{synthetic, natural}` |
| Cutover never triggers (no natural traffic ever) | Phase-2 GREEN condition explicitly raises `WARN_NATURAL_INSUFFICIENT` with Chris-gated escalation; no silent green |
| Loader process dies, clock pauses without notice | systemd `Restart=always` + Alertmanager alert on `absent(synth_loader_heartbeat_total[5m])`; loader exits 75 (EX_TEMPFAIL) on consecutive-error budget exhaustion to invite restart |
| Resource cost on ZenBook | 1 qps is < 0.1% CPU; no concern |

## 8. Cross-references

- `m4-prep/canary-stage-runbook.md` §6 (M4 observability stack)
- `m4-prep/us-009-acceptance-criteria.md` (B1/B2 originating definitions)
- `.omc/burn-in/day-1-snapshot.md` (the empirical observation that drove this resolution)
- `apex-m5-entry-spec.md` §2 E.3 (downstream consumer — `dual_read_continuity_check` must understand the synthetic/natural split)
- xcouncil verdict trail — `/c/Users/user/AppData/Local/Temp/xcouncil_opus_prompt.txt` (session-local 2026-05-09)
