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

# ---- Metric pass/fail (vacuous when both flags false) -----------------------
# Real per-metric evaluation is item 4.7 (Phase-1/Phase-2 split logic).
# This script just records whether traffic flowed at all today.
metrics_json='{"unreachable_rate":"no_data","discrepancy_rate":"no_data","p99_latency":"no_data","write_error_rate":"no_data","crosswalk_miss":"no_data","circuit_open_count":"no_data"}'
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
