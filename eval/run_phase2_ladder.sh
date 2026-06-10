#!/bin/bash
# Phase 2 Ablation Ladder — 3 model tiers × 3 memory conditions.
#
# Produces lift-curve data: for each tier, measure
#   oracle_score - parallax_score - no_memory_score
# The gap between conditions at a given tier = Parallax's value at that
# capability level.
#
# Run unattended via:
#   pm2 start bash --name parallax-phase2 -- /e/Parallax/eval/run_phase2_ladder.sh
#
# Sequential by design — only one model's rate-limit pool in use at a time.
# --no-resume is NOT passed, so a restart resumes from the last question.

set -u  # undefined vars fatal; but no -e so one failure does not kill the ladder

export PATH="/c/Users/user/AppData/Roaming/npm:$PATH"
[[ -f "$HOME/.bashrc" ]] && source "$HOME/.bashrc"

cd /e/Parallax

OUT_DIR=eval/results/phase2_ladder
mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/master.log"

JUDGE="gemini-2.5-flash"
SMOKE_N=50
FULL_N=500
GATE_PCT=15  # smoke raw accuracy floor below which full 500Q is skipped

# tier label -> answerer model id (nim: prefix routes via parallax.llm NIM provider)
TIERS_KEYS=(low mid pro)
declare -A TIER_MODEL=(
  [low]="nim:meta/llama-3.1-8b-instruct"
  [mid]="nim:openai/gpt-oss-120b"
  [pro]="gemini-2.5-pro"
)

# condition label -> extra run.py flags (split + optional --no-memory)
COND_KEYS=(no_memory parallax oracle)
declare -A COND_FLAGS=(
  [no_memory]="--split s --no-memory"
  [parallax]="--split s"
  [oracle]="--split oracle"
)

log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG"
}

raw_pct() {
  # prints integer percent CORRECT / total for a jsonl, or 0 if empty.
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
  local tier=$1 cond=$2 n=$3 tag=$4
  local model=${TIER_MODEL[$tier]}
  local flags=${COND_FLAGS[$cond]}
  local out="$OUT_DIR/${tier}_${cond}_${tag}.jsonl"

  log "RUN tier=$tier cond=$cond n=$n model=$model flags=$flags → $out"
  # shellcheck disable=SC2086
  python -m eval.longmemeval.run \
    $flags \
    --limit "$n" \
    --answer-model "$model" \
    --judge-model "$JUDGE" \
    --out "$out" \
    -v >>"$LOG" 2>&1
  local rc=$?
  local pct
  pct=$(raw_pct "$out")
  log "DONE tier=$tier cond=$cond n=$n rc=$rc raw=${pct}%"
  return $rc
}

ladder_tier() {
  local tier=$1
  for cond in "${COND_KEYS[@]}"; do
    # Pro oracle: reuse Run A 500Q result (87.2%), skip to save credits.
    if [[ "$tier" == "pro" && "$cond" == "oracle" ]]; then
      log "SKIP pro/oracle — reuse Run A oracle_500 = 87.2%"
      continue
    fi

    run_stage "$tier" "$cond" "$SMOKE_N" "smoke${SMOKE_N}"
    local pct
    pct=$(raw_pct "$OUT_DIR/${tier}_${cond}_smoke${SMOKE_N}.jsonl")
    if [[ "$pct" -ge "$GATE_PCT" ]]; then
      log "GATE PASS $tier/$cond smoke=${pct}% → running full $FULL_N"
      run_stage "$tier" "$cond" "$FULL_N" "full${FULL_N}"
    else
      log "GATE FAIL $tier/$cond smoke=${pct}% (floor ${GATE_PCT}%) → skipping full"
    fi
  done
}

log "=== Phase 2 Ladder START ==="
log "judge=$JUDGE smoke=$SMOKE_N full=$FULL_N gate=${GATE_PCT}%"
log "tiers=${TIERS_KEYS[*]} conditions=${COND_KEYS[*]}"

for tier in "${TIERS_KEYS[@]}"; do
  log "--- tier=$tier model=${TIER_MODEL[$tier]} ---"
  ladder_tier "$tier"
done

log "=== Phase 2 Ladder DONE ==="
log "Results under: $OUT_DIR"
log "Next: eyeball lift curve via compute_lift_curve.py (TODO)"
