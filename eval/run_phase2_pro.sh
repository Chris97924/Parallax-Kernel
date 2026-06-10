#!/bin/bash
# Phase 2 Ladder — pro tier only (gemini-2.5-pro).
# Runs alongside run_phase2_nim.sh. Gemini provider is independent from NIM,
# so they can run in parallel without rate-limit interference.
# Pro oracle is skipped — reuse Run A oracle_500 = 87.2%.

set -u

export PATH="/c/Users/user/AppData/Roaming/npm:$PATH"
[[ -f "$HOME/.bashrc" ]] && source "$HOME/.bashrc"

cd /e/Parallax

OUT_DIR=eval/results/phase2_ladder
mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/master_pro.log"

JUDGE="gemini-2.5-flash"
SMOKE_N=50
FULL_N=500
GATE_PCT=15

TIER="pro"
MODEL="gemini-2.5-pro"

COND_KEYS=(no_memory parallax)
declare -A COND_FLAGS=(
  [no_memory]="--split s --no-memory"
  [parallax]="--split s"
)

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG"
}

raw_pct() {
  python3 - "$1" <<'PY'
import json, sys
try:
    total = correct = 0
    for line in open(sys.argv[1], encoding="utf-8"):
        r = json.loads(line)
        total += 1
        if r.get("verdict") == "CORRECT":
            correct += 1
    print(0 if total == 0 else correct * 100 // total)
except FileNotFoundError:
    print(0)
PY
}

run_stage() {
  local cond=$1 n=$2 tag=$3
  local flags=${COND_FLAGS[$cond]}
  local out="$OUT_DIR/${TIER}_${cond}_${tag}.jsonl"

  log "RUN tier=$TIER cond=$cond n=$n model=$MODEL flags=$flags → $out"
  # shellcheck disable=SC2086
  python -m eval.longmemeval.run \
    $flags \
    --limit "$n" \
    --answer-model "$MODEL" \
    --judge-model "$JUDGE" \
    --out "$out" \
    -v >>"$LOG" 2>&1
  local rc=$?
  local pct
  pct=$(raw_pct "$out")
  log "DONE tier=$TIER cond=$cond n=$n rc=$rc raw=${pct}%"
  return $rc
}

log "=== Phase 2 Ladder (PRO) START ==="
log "judge=$JUDGE smoke=$SMOKE_N full=$FULL_N gate=${GATE_PCT}% model=$MODEL"
log "SKIP pro/oracle — reuse Run A oracle_500 = 87.2%"

for cond in "${COND_KEYS[@]}"; do
  run_stage "$cond" "$SMOKE_N" "smoke${SMOKE_N}"
  pct=$(raw_pct "$OUT_DIR/${TIER}_${cond}_smoke${SMOKE_N}.jsonl")
  if [[ "$pct" -ge "$GATE_PCT" ]]; then
    log "GATE PASS $TIER/$cond smoke=${pct}% → running full $FULL_N"
    run_stage "$cond" "$FULL_N" "full${FULL_N}"
  else
    log "GATE FAIL $TIER/$cond smoke=${pct}% (floor ${GATE_PCT}%) → skipping full"
  fi
done

log "=== Phase 2 Ladder (PRO) DONE ==="
