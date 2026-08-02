#!/usr/bin/env bash
# Apex M4 burn-in daily monitor — emits one JSONL row per day.
#
# Spec: docs/m4-prep/traffic-gap-resolution.md §4.8.
#
# Run via systemd user-timer at 02:00 UTC (10:00 CST). Each day's row is
# appended to /home/chris/parallax-kernel/burn-in-logs/burn-in-YYYY-MM-DD.jsonl.
#
# Output schema (compact JSON, single line):
#   day_n                       integer, day number since burn-in start
#   stack_healthy               bool, 1 = Prometheus + Alertmanager + Grafana up
#   metrics                     object with per-check pass/fail/no_data
#   breaches                    array of metric names that failed today
#   synthetic_started_24h       bool, true if synthetic series saw motion
#                               in the last 24h (replaces single
#                               traffic_started_24h field per spec §4.8)
#   natural_started_24h         bool, true if natural series saw motion
#                               in the last 24h
#   traffic_indicators          object, raw counters used for the splits

set -euo pipefail

PROM_URL="${PARALLAX_PROM_URL:-http://127.0.0.1:9090}"
ALERTMGR_URL="${PARALLAX_ALERTMGR_URL:-http://127.0.0.1:9093}"
GRAFANA_URL="${PARALLAX_GRAFANA_URL:-http://127.0.0.1:3000}"
LOG_DIR="${PARALLAX_BURN_IN_LOG_DIR:-${HOME}/parallax-kernel/burn-in-logs}"
BURN_IN_START_DATE="${PARALLAX_BURN_IN_START_DATE:-2026-05-09}"

mkdir -p "${LOG_DIR}"
TODAY="$(date -u +%Y-%m-%d)"
LOG_FILE="${LOG_DIR}/burn-in-${TODAY}.jsonl"

# ---- Day number since burn-in start -----------------------------------------
day_n=$(( ($(date -u -d "${TODAY}" +%s) - $(date -u -d "${BURN_IN_START_DATE}" +%s)) / 86400 + 1 ))

# ---- Stack health probes -----------------------------------------------------
stack_healthy=1
if ! curl -sSf -o /dev/null -m 5 "${PROM_URL}/-/healthy"; then stack_healthy=0; fi
if ! curl -sSf -o /dev/null -m 5 "${ALERTMGR_URL}/-/healthy"; then stack_healthy=0; fi
if ! curl -sSf -o /dev/null -m 5 "${GRAFANA_URL}/api/health"; then stack_healthy=0; fi

# ---- Traffic source split (spec §4.8) ---------------------------------------
# Use Prometheus instant queries to count how many series are emitting motion
# under each traffic_source label in the last 24h.
prom_query() {
  local q="$1"
  curl -sSfG -m 5 "${PROM_URL}/api/v1/query" --data-urlencode "query=${q}" |
    python3 -c "import json, sys; r=json.load(sys.stdin); v=r['data']['result']; print(v[0]['value'][1] if v else '0')"
}

# Record an SLO gauge's RAW CURRENT VALUE (read-only observability — NOT a
# pass/fail verdict; authoritative DoD evaluation lives in `parallax canary
# --dod`, item 4.7). Unlike prom_query, this distinguishes a genuine 0 from an
# absent series: prints the value if the series is scraped, "no_series" if the
# metric is not yet instrumented, "query_failed" if Prometheus is unreachable.
# Always exits 0 so `set -e` never aborts on a transient query error.
prom_slo() {
  # $1 is a full PromQL expression, not just a metric name — the dual-read
  # gauges are label-partitioned and a bare name would return several series.
  local query="$1" out
  out=$(curl -sSfG -m 5 "${PROM_URL}/api/v1/query" --data-urlencode "query=${query}" 2>/dev/null) \
    || { printf 'query_failed'; return 0; }
  printf '%s' "${out}" | python3 -c "import json, sys
try:
    r = json.load(sys.stdin); v = r['data']['result']
    sys.stdout.write(v[0]['value'][1] if v else 'no_series')
except Exception:
    sys.stdout.write('query_failed')"
}

# These queries assume server-side instrumentation has the traffic_source
# label wired (item 4.2). Until then both will return "0" → both flags
# false → DoD evaluator returns PENDING_IMPLEMENTATION per spec §3.4.
if synthetic_24h_count=$(prom_query "sum(increase(parallax_aphelion_total{traffic_source=\"synthetic\"}[24h]))" 2>&1); then
  synthetic_started_24h=$(awk -v x="${synthetic_24h_count}" 'BEGIN { print (x+0 > 0) ? "True" : "False" }')
else
  synthetic_24h_count="QUERY_FAILED"
  synthetic_started_24h="None"
fi
if natural_24h_count=$(prom_query "sum(increase(parallax_aphelion_total{traffic_source=\"natural\"}[24h]))" 2>&1); then
  natural_started_24h=$(awk -v x="${natural_24h_count}" 'BEGIN { print (x+0 > 0) ? "True" : "False" }')
else
  natural_24h_count="QUERY_FAILED"
  natural_started_24h="None"
fi

# ---- Per-metric RAW SLO values (observability, not verdicts) ----------------
# Record each SLO gauge's current observed value so the daily row is non-vacuous
# even before natural traffic exists. These are RAW NUMBERS, not pass/fail — the
# authoritative DoD verdict (thresholds + Phase-1/Phase-2 split) is item 4.7 in
# `parallax canary --dod`. Metrics whose series is not yet scraped record
# "no_series" (honest) rather than the old blanket "no_data" placeholder.
#   - discrepancy_rate / write_error_rate: live dual-read gauges, selected per
#     traffic_source (see below).
#   - unreachable_rate / p99_latency / crosswalk_miss / circuit_open_count:
#     not yet instrumented as standalone scraped series → "no_series".
#
# 2026-08-02: these two gauges are now partitioned by `traffic_source`, so a
# bare metric name returns a MULTI-SERIES vector and prom_slo's `result[0]`
# would silently record whichever partition Prometheus happened to return
# first — the daily row could flip between populations from one day to the
# next and nothing would say so. Every query below selects its partition
# explicitly. `max()` keeps the result a single series.
#   *_natural   — the DoD-relevant population, matching what the retargeted
#                 alert rules evaluate. Reads "no_series" while natural
#                 traffic is 0, which is the honest answer, not a healthy 0.
#   *_synthetic — keeps the row non-vacuous during burn-in, which is what
#                 this block was added for.
discrepancy_rate=$(prom_slo 'max(parallax_dual_read_discrepancy_rate{traffic_source="natural"})')
write_error_rate=$(prom_slo 'max(parallax_dual_read_write_error_rate{traffic_source="natural"})')
discrepancy_rate_synthetic=$(prom_slo 'max(parallax_dual_read_discrepancy_rate{traffic_source="synthetic"})')
write_error_rate_synthetic=$(prom_slo 'max(parallax_dual_read_write_error_rate{traffic_source="synthetic"})')
metrics_json=$(python3 <<PY
import json
print(json.dumps({
    "discrepancy_rate": "${discrepancy_rate}",
    "write_error_rate": "${write_error_rate}",
    "discrepancy_rate_synthetic": "${discrepancy_rate_synthetic}",
    "write_error_rate_synthetic": "${write_error_rate_synthetic}",
    "unreachable_rate": "no_series",
    "p99_latency": "no_series",
    "crosswalk_miss": "no_series",
    "circuit_open_count": "no_series",
}, sort_keys=True))
PY
)
breaches='[]'

# ---- Compose JSONL row -------------------------------------------------------
row=$(python3 <<PY
import json, sys
print(json.dumps({
    "day_n": ${day_n},
    "date": "${TODAY}",
    "stack_healthy": bool(${stack_healthy}),
    "metrics": ${metrics_json},
    "breaches": ${breaches},
    "synthetic_started_24h": ${synthetic_started_24h},
    "natural_started_24h": ${natural_started_24h},
    "traffic_indicators": {
        "synthetic_24h_count": "${synthetic_24h_count}",
        "natural_24h_count": "${natural_24h_count}"
    }
}, sort_keys=True))
PY
)

echo "${row}" >> "${LOG_FILE}"
echo "${row}"
