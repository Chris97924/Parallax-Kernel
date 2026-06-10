# M4 Canary Stage-1 Readiness Card — 2026-06-10

> **Status**: PREP-ONLY · ⛔ **BLOCKED by GATE 2A** (do **NOT** flip `SHADOW_FRACTION`)
> **Author**: autonomous prep pass (ZenBook, read-only verification)
> **Authority**: [canary-stage-runbook.md](./canary-stage-runbook.md) v2.0 ·
> [canary-shadow-spec.md](./canary-shadow-spec.md) frozen-2026-05-18 ·
> [stage-0-preflight-checklist.md](./stage-0-preflight-checklist.md)
>
> This card is a **current-state snapshot + a ready-to-execute runbook card**. It
> executes nothing on the served path. The canary shadow observer is **not yet
> deployed** and **must not** be flipped while GATE 2A is open (§C).

---

## ⛔ A. Why this is blocked (GATE 2A)

GATE 2A (M4 burn-in evidence) is **not satisfied**, so Stage-1 entry (GATE 3) is
blocked. Verified 2026-06-10 on ZenBook:

| Evidence | Observed | Needed | Status |
|---|---|---|---|
| Burn-in snapshot recorder | running again (exec-bit fix, PR #79) — emits daily row | persistent daily rows | ✅ recovered today |
| Burn-in clock | `day_n=33` | ≥14d series (Phase-1) | ✅ clock satisfied (synthetic) |
| Synthetic traffic 24h | `True` (~81k increase) | series exists | ✅ |
| **Natural traffic 24h** | **`False` (0 calls)** | ≥100 calls/24h for ≥7/14d (Phase-2 semantic) | ❌ **gap** |
| `parallax canary --dod --stage m4_1pct` | `INSUFFICIENT_DATA` | PASS | ❌ (shadow not deployed → 0 hits) |

**Bottom line:** Phase-1 (synthetic clock) is satisfied; **Phase-2 (natural-traffic
semantic gate) is not** — there is zero natural traffic. Whether to accept a
synthetic-validated B2 with caveat, extend the window, or wait for natural traffic
is a **Chris-gated policy decision** (`traffic-gap-resolution.md` §3.3
`WARN_NATURAL_INSUFFICIENT`). Until resolved, **GATE 2A stays open and Stage-1 must
not start.**

---

## B. Stage-0 pre-flight — current-state assessment

Run-through of `stage-0-preflight-checklist.md` (25 items). `PASS`/`FAIL`/`N-A`
reflect **verified current state on ZenBook 2026-06-10**, read-only.

### §2 M3 14-day Corpus DoD (6 items)
| Item | Status | Evidence |
|---|---|---|
| dual_read_discrepancy_rate < 0.1% | ⚠️ UNVERIFIED-LOCALLY | `parallax_dual_read_discrepancy_rate=0` now, but the `dual_read_continuity_check --since=72h` tool is not present on ZenBook; 72h continuity not machine-checked here |
| arbitration_conflict_rate < 1% | ⚠️ UNVERIFIED | same tool absent; `parallax_arbitration_conflict_rate` series exists |
| dual_read_write_error_rate < 0.02% | ⚠️ UNVERIFIED | `parallax_dual_read_write_error_rate=0` now; 72h continuity not checked |
| aphelion_unreachable_rate < 0.5% | N-A | no `aphelion_unreachable_*` series scraped (stub is raise-only; metric appears only under canary) |
| crosswalk_miss_rate < 5% | N-A | no `crosswalk_*` series scraped |
| circuit_open_count_72h < 3 | N-A | no `circuit_open_*` series scraped |

> The §2 verification commands (`dual_read_continuity_check`) reference a CLI not
> installed on this host. The underlying gauges read 0 instantaneously but a 72h
> continuity proof needs that tool (or a Prometheus range query) — **left for the
> oncall T-1h run**, not satisfiable in this prep pass.

### §3 Aphelion v0.5.x toolkit (3 items)
| Item | Status | Evidence |
|---|---|---|
| package-format only, no retrieval API | ✅ PASS | no `import ...aphelion...http` in `parallax/` |
| `AphelionReadAdapter.query()` raise-only stub | ✅ PASS (path drift) | logic lives in `parallax/router/aphelion_adapter.py` — **checklist says `aphelion_stub.py`, which no longer exists** (renamed). Update the checklist path. |
| dual_read path not broken by stub | ✅ PASS | `pytest tests/router -k dual_read` → **127 passed** |

### §4 Canary infra US-009.1 (4 items)
| Item | Status | Evidence |
|---|---|---|
| event_id UUID v7 | ⚠️ UNVERIFIED | not probed this pass |
| audit_log SQLite table | N-A (A1 change) | per runbook §4 note: shadow `observe()` only increments Prometheus counters; the SQLite outcome table is intentionally unused by the live path now |
| 5 rollback triggers wired | ✅ PASS (as Prometheus alerts) | rule group `parallax_m4_canary` loaded with 5 alerts: `M4CanaryErrorRateBreach`, `…DiscrepancyRateBreach`, `…P99LatencyBreach`, `…DataLossDetected`, `…InsufficientSampleSize` |
| hysteresis 30min cooldown | ⚠️ UNVERIFIED | not probed |

### §5 DoD scripts US-009.3 (3 items) — drain/rollback-drill
| Item | Status | Evidence |
|---|---|---|
| rollback-drill / drain-test / orbit-reemit | **N-A for M4** | runbook §0 decision①: these belong to **M5 Aphelion cutover**, not the M4 observer. The M4 "rollback" is `SHADOW_FRACTION=0.0` only. |

### §6 Observability (4 items)
| Item | Status | Evidence |
|---|---|---|
| Prometheus alert rules loaded | ✅ PASS | `/api/v1/rules` shows group `parallax_m4_canary` (5 rules) live |
| promtool lint of repo rule file | N-A locally | `promtool` not installed on ZenBook; CI `prometheus-rules-check.yml` covers it |
| Grafana dashboard `parallax-m4-canary-stage-1` | ⚠️ UNVERIFIED | Grafana up (`:3000` healthy); dashboard UID not probed (no `GRAFANA_TOKEN` in this pass) |
| Alertmanager routing | ✅ adapted | **Discord relay (PR #60) + Gmail (PR #62)** are LIVE (`parallax-discord-relay` + `parallax-gmail-smtp-relay` active). PagerDuty/Slack in the checklist are placeholders (runbook §0 decision②). |

### §7 Rollback path drill (1 item)
| Item | Status | Evidence |
|---|---|---|
| T-1h full rollback drill <30min | **N-A for M4 observer** | observer rollback = single `sed`+restart (`SHADOW_FRACTION=0.0`), ≤10s, zero served-path risk. The 4-step drain drill is M5 (runbook §0①). |

### §8 Stakeholder comms (4 items)
| Item | Status |
|---|---|
| Chris sign-off / oncall / Slack / status-page | **N-A (single-operator homelab)** — runbook §7: no oncall team; alerting is Discord→Chris. Final GO/No-Go is Chris's ACK. |

**Pre-flight summary:** the items that are *machine-verifiable on this host and in
M4 scope* are green (aphelion stub + dual_read 127-pass; 5 alert rules loaded;
Discord/Gmail alerting live). The rest are either **N-A for the M4 observer**
(drain/drill/oncall — those are M5 / enterprise-template artifacts), **deferred to
the oncall T-1h window** (72h continuity proofs needing a CLI not on this host), or
**Chris-gated** (final ACK). **No machine-checkable blocker in the M4-observer
scope — the only true blocker is GATE 2A natural traffic (§A).**

---

## C. Stage-1 ready-to-execute runbook card

> ⛔ **DO NOT RUN while GATE 2A is open.** These commands need `sudo`, touch the
> live **system** service `parallax-server` (`:8765`), and are listed here only so
> the flip is a copy-paste once Chris clears GATE 2A. The observer never changes
> the served result (`canary-shadow-spec.md` §9), but a restart is still a live-
> service action.

### C0. One-time Stage-0 deploy (NOT done yet — verified 2026-06-10)
Current state: `/etc/parallax/canary.env` **absent**; `parallax-server.service.d/`
has only `hardening.conf` (no `canary.conf`); `parallax_canary_shadow_*` series = 0.
The Prometheus rule group `parallax_m4_canary` **is already loaded**.

```bash
# 1. canary.env (observer OFF)
sudo install -m 600 -o chris -g chris /dev/null /etc/parallax/canary.env
echo 'PARALLAX_CANARY_SHADOW_FRACTION=0.0' | sudo tee /etc/parallax/canary.env
# 2. system-service drop-in (coexists with hardening.conf; EnvironmentFile reads
#    /etc/parallax, not $HOME, so no ReadWritePaths whitelist change needed)
sudo tee /etc/systemd/system/parallax-server.service.d/canary.conf >/dev/null <<'EOF'
[Service]
EnvironmentFile=/etc/parallax/canary.env
EOF
# 3. reload + restart, confirm active (watch for hardening-sandbox crash-loop)
sudo systemctl daemon-reload
sudo systemctl restart parallax-server.service
systemctl status parallax-server.service --no-pager | head -3
curl -sf http://127.0.0.1:8765/healthz >/dev/null && echo "served path OK"
# 4. reload the FULL rule file — the live group currently has only T1–T5; the
#    shadow recording rules + 2 CanaryShadow gate alerts are NOT loaded yet (§D3).
#    docker-compose mounts repo prometheus/rules into /etc/prometheus/rules:ro.
docker compose -f deploy/observability/docker-compose.yml \
   exec prometheus promtool check rules /etc/prometheus/rules/parallax-m4-canary.rules.yml
docker compose -f deploy/observability/docker-compose.yml kill -s HUP prometheus
# Grafana: import grafana/dashboards/parallax-m4-canary-stage-1.json
```
**Stage-0 acceptance:** server `active`; `SHADOW_FRACTION=0.0`; rule group reloaded
with **all** rules (7 alerts incl. the 2 CanaryShadow gate alerts + 4 shadow
recording rules — see §D3); **no** `parallax_canary_shadow_*` series yet
(fraction=0 → `observe()` returns early).

### C1. Flip to Stage-1 (1% sampling) — the one-line flip
```bash
sudo sed -i 's/^PARALLAX_CANARY_SHADOW_FRACTION=.*/PARALLAX_CANARY_SHADOW_FRACTION=0.01/' /etc/parallax/canary.env
sudo systemctl restart parallax-server.service          # ≤10s downtime
```
- **Which unit sets the knob:** the **system** service
  `parallax-server.service` via `EnvironmentFile=/etc/parallax/canary.env`.
- **The "observer":** in-process — `parallax/canary_shadow.py::observe()` is called
  once per dual-read in `query.py::_dispatch_with_router`. It activates automatically
  when `fraction>0`; there is **no separate observer process to start**.
- **Confirm it took:** within a few minutes,
  `parallax_canary_shadow_attempts_total{stage="s1"}` starts incrementing
  (`curl -sG :9090/api/v1/query --data-urlencode 'query=parallax_canary_shadow_attempts_total'`).
- **Notify:** Discord `#指揮室` relay (PR #60).

### C2. Dwell + DoD (advisory)
- Dwell: s1 entry → s2 after 24h → s3 48h → s4 14-day.
- DoD (reads Prometheus shadow metrics, A1):
  ```bash
  /home/chris/parallax/.venv/bin/parallax canary --dod --stage m4_1pct
  #   → PASS / INSUFFICIENT_DATA (samples<50 or Prom unreachable; keep waiting) / FAIL
  ```
- **Authoritative promotion gates** = the Prometheus T1–T5 alerts (auto-rollback) +
  the two CanaryShadow gate alerts (discrepancy / aphelion-unreachable >0.5% for
  10m). `--dod` is an advisory cross-check, not the gate.
- Promote only on Chris Go/No-Go ACK.

### C3. Rollback (observer layer — zero served-path risk)
```bash
sudo sed -i 's/^PARALLAX_CANARY_SHADOW_FRACTION=.*/PARALLAX_CANARY_SHADOW_FRACTION=0.0/' /etc/parallax/canary.env
sudo systemctl restart parallax-server.service
# confirm Grafana canary series return to zero
```
≤10s downtime; the served path is unaffected throughout (client always gets
`result.primary`). **Do NOT** run `--rollback-drill`/`--drain-test` for this —
those are M5 (runbook §0①).

---

## D. Doc drifts found (for a later cleanup PR — not fixed here)
1. `stage-0-preflight-checklist.md` §3 references `parallax/router/aphelion_stub.py`
   → file renamed to `aphelion_adapter.py`.
2. `canary-shadow-spec.md` §7 says `~/.config/systemd/user/parallax-server.service`
   (user service); the real unit is the **system** service
   `/etc/systemd/system/parallax-server.service` (runbook v2.0 §1 is correct).
3. The two CanaryShadow gate alerts in `canary-shadow-spec.md` §6 were not visible
   in the live `parallax_m4_canary` rule group (only T1–T5 alerts loaded) — confirm
   the deployed rule file matches the repo before Stage-1.
