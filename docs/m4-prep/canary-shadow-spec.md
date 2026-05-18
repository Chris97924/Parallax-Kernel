# Apex M4 Canary Shadow Observer — Spec v0.1

**Status**: frozen-2026-05-18
**Component**: `parallax/canary_shadow.py`
**Companion runbook**: [canary-stage-runbook.md](./canary-stage-runbook.md)
**Pairs with**: [traffic-gap-resolution.md](./traffic-gap-resolution.md), [stage-0-preflight-checklist.md](./stage-0-preflight-checklist.md)

## 1. Purpose

Turn the `M4 GATE 3-7 Stage @1%/@10%/@50%/@100% Chris ACK` from a ceremonial sign-off into a number-driven gate. The observer samples completed `DualReadRouter` results into stage-labelled Prometheus counters so alertmanager can target the **current canary observation subset** rather than the global dual-read population.

## 2. Reframe — why this is observation, not shadowing

`DualReadRouter` (M3-T1.2, `parallax/router/dual_read.py`) already runs primary (`RealMemoryRouter`) and secondary (`AphelionReadAdapter`) in parallel on every dual-read-enabled request and classifies the outcome into `match | diverge | primary_only | aphelion_unreachable | skipped`. The metrics `parallax_dual_read_outcomes_total` and `parallax_dual_read_discrepancy_rate` already exist.

What was missing for GATE 3-7: a way to alert on **the subset associated with a particular rollout stage**, not the global rate. Stage 1 (1%) cannot be deemed safe by looking at the 100% global rate, because the global rate is dominated by the steady-state population, not the recent canary sample.

Solution: a tiny post-`DualReadRouter` hook that, for a configurable fraction of completed dual-read results, increments a parallel metric pool labelled by `stage`. The fraction is the canary stage knob; the metric pool is the GATE evidence surface.

## 3. Stage mapping

| Stage      | `CANARY_SHADOW_FRACTION` range | M4 GATE          |
|------------|--------------------------------|------------------|
| `disabled` | exactly `0.0`                  | observer off     |
| `s1`       | `(0.0, 0.01]`                  | GATE 3 entry     |
| `s2`       | `(0.01, 0.10]`                 | GATE 4 (24h)     |
| `s3`       | `(0.10, 0.50]`                 | GATE 5 (48h)     |
| `s4`       | `(0.50, 1.00]`                 | GATE 6/7 (final) |

Boundaries are inclusive on the upper end so operators using round percentages land on the intended stage. `1.0` is the full-sample mode used for closing the GATE 7 14-day evidence window.

## 4. Public API

```python
from parallax import canary_shadow

canary_shadow.get_shadow_fraction() -> float        # [0.0, 1.0], 0.0 on error
canary_shadow.resolve_stage(fraction) -> CanaryStage
canary_shadow.observe(result, *, user_id, traffic_source) -> None
```

`observe()` is the only call site. It is invoked once per dispatched dual-read in `parallax/server/routes/query.py::_dispatch_with_router` after `mem_router.query(...)` returns, before `evidence = result.primary` propagation. It never raises out; any internal exception is logged with `event=canary_shadow_observe_failed` and swallowed.

## 5. Metrics

| Metric                                       | Type    | Labels                                             |
|----------------------------------------------|---------|----------------------------------------------------|
| `parallax_canary_shadow_attempts_total`      | Counter | `stage`, `user_id`, `traffic_source`               |
| `parallax_canary_shadow_outcomes_total`      | Counter | `stage`, `outcome`, `user_id`, `traffic_source`    |
| `parallax_canary_shadow_discrepancy_rate`    | Recorded gauge | `stage`, `traffic_source`                  |
| `parallax_canary_shadow_aphelion_unreachable_rate` | Recorded gauge | `stage`, `traffic_source`            |

Recorded gauges are produced by Prometheus recording rules in `prometheus/rules/parallax-m4-canary.rules.yml` (group `parallax_m4_canary`) so the rate calculation lives in one place and dashboards / alerts reference the same series.

Denominators are guarded with `> 0` so an idle stage returns no series rather than `NaN`, matching the existing M4 canary rules style.

## 6. Alerts

| Alert                                | Threshold | For | Severity | Class |
|--------------------------------------|-----------|-----|----------|-------|
| `CanaryShadowDiscrepancyHigh`        | rate > 0.5% | 10m | warning | gate  |
| `CanaryShadowAphelionUnreachableHigh`| rate > 0.5% | 10m | warning | gate  |

Both are gate-class (not auto-rollback) — they block stage promotion but do not fire the canary rollback controller. Auto-rollback remains the responsibility of T1-T4 in the same rule group.

## 7. Deploy

Not bundled in this change. Defer to the next ZenBook deploy window after the M5 burn-in completes 2026-05-28 01:11 (clock 5/14 → 5/28 per `project_messier_v4_v5_progress.md`). At deploy time:

```bash
# 1. /etc/parallax/canary.env (chmod 600 chris:chris)
CANARY_SHADOW_FRACTION=0.0

# 2. ~/.config/systemd/user/parallax-server.service.d/canary.conf
[Service]
EnvironmentFile=/etc/parallax/canary.env

# 3. reload + restart
systemctl --user daemon-reload
systemctl --user restart parallax-server.service

# 4. promote rule file into observability stack
cp prometheus/rules/parallax-m4-canary.rules.yml \
   /home/chris/parallax-kernel/deploy/observability/prometheus/rules/
docker compose exec prometheus promtool check rules /etc/prometheus/rules/parallax-m4-canary.rules.yml
docker compose kill -s HUP prometheus
```

Stage advance is a single `sed` + `systemctl restart` (≤ 10s downtime, well inside the 30s rollback window required by `m4-m5-readiness-spec.md`):

```bash
sudo sed -i 's/^CANARY_SHADOW_FRACTION=.*/CANARY_SHADOW_FRACTION=0.01/' /etc/parallax/canary.env
sudo systemctl restart parallax-server.service
```

Rollback to disabled:

```bash
sudo sed -i 's/^CANARY_SHADOW_FRACTION=.*/CANARY_SHADOW_FRACTION=0.0/' /etc/parallax/canary.env
sudo systemctl restart parallax-server.service
```

## 8. Invariants

1. `observe()` MUST NOT mutate the supplied `DualReadResult`. (The dataclass is frozen but defence-in-depth applies.)
2. `observe()` MUST NOT raise out. Internal exceptions are logged and swallowed.
3. `observe()` MUST NOT register new collectors at call time; collectors are module-scope and re-import safe via `_get_or_create_counter`.
4. Failure to parse `CANARY_SHADOW_FRACTION` MUST log `event=canary_shadow_fraction_invalid` and fall back to `0.0` (disabled).
5. The fraction value is the **configured rollout sampling rate**, not a per-user determinism gate. Stage advance is driven by the env var; per-request gating is `random.random() < fraction`. Sticky-per-user sampling is YAGNI in v0.1; revisit if Stage 5 introduces user-targeted canaries.

## 9. Non-goals

- Real traffic split. The client response is always `result.primary` from `DualReadRouter`; this module never alters the served result.
- Replacing `DualReadRouter` or its `parallax_dual_read_*` metrics. Those are the canonical dual-read substrate; this is a thin overlay.
- Per-user sticky bucketing or session-affinity. The current rollout has no notion of user cohorts.
