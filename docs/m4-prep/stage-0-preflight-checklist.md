# M4 Stage 0 — Pre-flight Checklist

> **文件版本**: v1.0.0  
> **最後更新**: 2025-01-15  
> **Owner**: Parallax SRE  
> **審核者**: Chris (拍板者)  
> **狀態**: ACTIVE  

---

## 1. 文件用途

### 受眾

| 角色 | 職責 |
|------|------|
| M4 canary deployment oncall 工程師 | 逐項勾選、執行驗證命令、回報結果 |
| Chris (拍板者) | Final GO/NO-GO 簽核 |

### 用途

Stage @1% 啟動前 **T-1 hour** 內，oncall 工程師依本文件逐項勾選。  
**全綠 → GO**，啟動 stage @1%。  
**任一紅 → STOP**，記錄 blocker 於 `m4-launch-blockers.md`，通知 Chris。

### 與上游文件之關係

```
us-009-acceptance-criteria.md   ← 定義 US-009.1 / US-009.2 / US-009.3 acceptance criteria
         │
         ▼
canary-stage-runbook.md         ← 4-stage 推進 SOP (1% → 10% → 50% → 100%)
         │
         ▼
stage-0-preflight-checklist.md  ← 本文件：啟動前 gate check (全綠才 GO)
```

---

## 2. M3 14-day Corpus DoD Verification（必要前置）

> M3 corpus 穩定運行 14 天後，以下指標必須連續 72 小時達標。  
> 任一項未達標 → **STOP**，不得啟動 M4 canary。

### Checklist

- [ ] **dual_read_discrepancy_rate < 0.1%** 連續 72h
- [ ] **arbitration_conflict_rate < 1%** 連續 72h
- [ ] **dual_read_write_error_rate < 0.02%** 連續 72h
- [ ] **aphelion_unreachable_rate < 0.5%**（待 PR #27 / US-006 deploy 後可驗）
- [ ] **crosswalk_miss_rate < 5%**（測量窗口 +48h）
- [ ] **circuit_open_count_72h < 3**

### ⚠️ 前置：log dir 一律從 service env 解析，不得用 repo default

> **新增 2026-08-02**（Gate-5 誤判的直接成因）

下面三條 `dual_read_continuity_check` 全部讀 dual-read decision log 目錄。
該目錄由 **`DUAL_READ_LOG_DIR`** 決定（writer `parallax/router/dual_read_decision_log.py`、
reader `parallax/router/dual_read_metrics.py` 兩邊都是這個規則）；**只有** env 未設時才落回
repo 內建 default `parallax/logs/`。

驗證前先在**跑 service 的那台機器上、用 service 自己的 env** 取出實際路徑：

```bash
# ZenBook: 取 systemd 實際注入的值，不要相信 shell 裡的 env
DUAL_READ_LOG_DIR_RESOLVED=$(
  systemctl show parallax-server.service -p Environment \
    | tr ' ' '\n' | sed -n 's/^DUAL_READ_LOG_DIR=//p'
)
echo "resolved: ${DUAL_READ_LOG_DIR_RESOLVED:?DUAL_READ_LOG_DIR not set on the service — STOP}"
```

**下一節每一條驗證命令都必須顯式帶 `--log-dir="${DUAL_READ_LOG_DIR_RESOLVED}"`**，
不能只靠 shell 的環境變數。oncall 的 shell 不會繼承 systemd 注入的 env——這正是 Gate-5
當時的情境——`--log-dir` 沒帶，工具就落回 shell env 或 repo default
（`scripts/dual_read_continuity_check.py:66-68`），照著清單做也還是量錯目錄。

Gate-5（2026-07-26）跳過了這一步，改讀 repo default，量到 0 筆記錄，據此判定
`arbitration_conflict_rate` 的 exposition 是 stale。實際上 service 目錄
（`/home/chris/parallax-data/dual-read-logs`）自 2026-05-15 起未曾中斷，當時窗內有 245,491 筆。
repo default 那個路徑在 systemd hardening（`ProtectHome=read-only`）下 service 根本寫不進去，
所以它**不可能**是 production sink。

同一個盲點現在也有機器可讀的訊號：`/metrics` 會輸出
`parallax_dual_read_log_dir_missing`（目錄不存在時為 1.0）與
`parallax_dual_read_log_records_total{traffic_source=...}`（窗內筆數，含 0）。
量到 0 之前先看這兩個 gauge，別再用 rate gauge 反推目錄健康。

### 驗證命令

```bash
# 前置：${DUAL_READ_LOG_DIR_RESOLVED} 來自上一節，未設就不要往下跑

# dual_read_discrepancy_rate — 連續 72h < 0.1%
dual_read_continuity_check \
  --log-dir="${DUAL_READ_LOG_DIR_RESOLVED:?run the resolve step above first}" \
  --since=72h \
  --metric=discrepancy \
  --format=json
# 預期 exit 0，JSON 內 "pass": true

# arbitration_conflict_rate — 連續 72h < 1%
dual_read_continuity_check \
  --log-dir="${DUAL_READ_LOG_DIR_RESOLVED:?run the resolve step above first}" \
  --since=72h \
  --metric=arbitration_conflict \
  --format=json
# 預期 exit 0

# dual_read_write_error_rate — 連續 72h < 0.02%
dual_read_continuity_check \
  --log-dir="${DUAL_READ_LOG_DIR_RESOLVED:?run the resolve step above first}" \
  --since=72h \
  --metric=write_error \
  --format=json
# 預期 exit 0

# aphelion_unreachable_rate — < 0.5% (PR #27 deploy 後)
curl -s "http://prometheus:9090/api/v1/query" \
  --data-urlencode 'query=rate(aphelion_unreachable_total[72h]) / rate(aphelion_requests_total[72h])' \
  | jq '.data.result[0].value[1]'
# 預期 < 0.005

# crosswalk_miss_rate — < 5% (測量窗口 +48h)
curl -s "http://prometheus:9090/api/v1/query" \
  --data-urlencode 'query=rate(crosswalk_miss_total[48h]) / rate(crosswalk_requests_total[48h])' \
  | jq '.data.result[0].value[1]'
# 預期 < 0.05

# circuit_open_count_72h — < 3
curl -s "http://prometheus:9090/api/v1/query" \
  --data-urlencode 'query=increase(circuit_open_total[72h])' \
  | jq '.data.result[0].value[1]'
# 預期 < 3
```

---

## 3. Aphelion v0.5.x Package Toolkit Verification

> ⚠️ **重要**：依據 xcouncil verdict，M4 僅使用 Aphelion **package format**，  
> **NOT retrieval API**。真實 HTTP adapter 延至 M5+ ticket。

### Checklist

- [ ] **Aphelion v0.5.0+ 已部署**（package format only，非 retrieval API）
- [ ] **`AphelionReadAdapter.query()`** 為 raise-only stub：`raise AphelionUnreachableError("not_implemented")`（US-009.2 null-stub；`DualReadRouter` 標記 `outcome="aphelion_unreachable"`）
- [ ] **無真實 HTTP adapter wired**（deferred to M5+ ticket）

### 驗證命令

```bash
# 確認 Aphelion package 版本 >= 0.5.0
pip show aphelion 2>/dev/null | grep -i version
# 或
python -c "import aphelion; print(aphelion.__version__)"
# 預期 >= 0.5.0

# 確認 stub 行為：query() raise AphelionUnreachableError("not_implemented")
grep -A2 'def query' parallax/router/aphelion_stub.py | head -5
# 預期看到 raise AphelionUnreachableError("not_implemented")

# 確認 dual_read 路徑沒有被 stub 改動 break
pytest tests/router/ -k dual_read -v 2>&1 | tail -5
# 預期：全數 PASS（涵蓋 dual_read_router / dual_read_metrics / dual_read_decision_log
#       / dual_read_result / dual_read_breaker_integration / is_dual_read_enabled）

# 確認無真實 HTTP adapter 被 import
grep -rn 'import.*aphelion.*http\|from.*aphelion.*http' parallax/ --include='*.py'
# 預期：無輸出（exit 0, empty result）

# 確認 stub 是唯一 wired 的 aphelion backend
grep -rn 'aphelion_stub\|AphelionReadAdapter' parallax/router/ --include='*.py'
# 預期：僅出現在 router config + dual_read 入口
```

---

## 4. Canary Infra (US-009.1) Ready Check

> US-009.1 定義 canary 基礎設施：idempotency、audit log、auto-rollback triggers。

### Checklist

- [ ] **event_id 使用 UUID v7**（NOT hash-with-timestamp — clock drift 有 dup 風險）
- [ ] **audit_log SQLite table 已建立**（schema: `event_id` PK, `request_at_iso`, `response_status`, ...）
- [ ] **4 rollback triggers + 1 min-hits gate 已接線**（cf. `us-009-acceptance-criteria.md` §3.3）：
  - **T1-error-rate**: error rate ≥ 0.5% / 5min sliding → trip rollback
  - **T2-discrepancy-rate**: discrepancy rate ≥ 0.5% / 3min sliding → trip rollback
  - **T3-p99-latency**: p99 latency ≥ 100ms / 5min sliding → trip rollback
  - **T4-data-loss**: data_loss > 0 → **immediate** trip rollback
  - **T5-min-hits-gate**: hits < 50 / 5min sliding → gate active（將 T1-T4 判定標記為 `insufficient_data`，gate 自身不 trip 也不 clear）
- [ ] **hysteresis 30min cooldown** + manual ACK re-promote logic 已實作

### 驗證命令

```bash
# 一鍵檢查 canary infra readiness
parallax canary --check-infra --pretend
# 預期 exit 0，所有 sub-check PASS

# 確認 event_id 為 UUID v7
grep -n 'uuid7\|uuid_v7\|UUIDv7' parallax/canary/event_id.py
# 預期：有對應 import / function call

# 確認 audit_log schema
sqlite3 parallax_canary.db ".schema audit_log"
# 預期欄位：event_id TEXT PRIMARY KEY, request_at_iso TEXT, response_status INTEGER, ...

# 確認 5 個 rollback triggers
parallax canary --list-triggers
# 預期輸出 5 個 trigger，含對應 threshold + window

# 確認 hysteresis cooldown 設定
grep -A5 'cooldown\|hysteresis' parallax/canary/rollback.py
# 預期：cooldown_seconds=1800 (30min)
```

---

## 5. DoD Scripts (US-009.3) Ready Check

> US-009.3 定義 rollback drill 與 drain 行為驗證。

### Checklist

- [ ] **rollback drill harness 就緒**：`parallax canary --rollback-drill --dry-run` exit 0
- [ ] **drain in-flight requests**（NO replay）per design
- [ ] **Orbit re-emit + idempotency 保護**驗過至少 1 次（drain test）

### 驗證命令

```bash
# rollback drill — dry run
parallax canary --rollback-drill --dry-run
# 預期 exit 0，輸出 drill steps + simulated metrics

# drain in-flight requests — readiness smoke（CLI 可呼叫檢查）
# 60s timeout 僅 smoke 用，不驗 60-300s long-tail；
# prod-equivalent drain drill 見 §7（300s timeout）
parallax canary --drain-test --timeout=60s
# 預期：所有 in-flight requests drain 完畢，無 replay

# Orbit re-emit + idempotency 保護驗證
parallax canary --orbit-reemit-test
# 預期：re-emit 後 event_id dedup 正常，無重複寫入
# 檢查 audit_log 中同一 event_id 僅出現一次
sqlite3 parallax_canary.db \
  "SELECT event_id, COUNT(*) as cnt FROM audit_log GROUP BY event_id HAVING cnt > 1"
# 預期：無輸出（無重複）
```

---

## 6. Observability Ready Check

> 確保監控、告警、通知管道在 canary 啟動前全部到位。

### Checklist

- [ ] **Grafana dashboard `parallax-m4-canary-stage-1`** 已從 `grafana/dashboards/parallax-m4-canary-stage-1.json` import 並 publish (UID `parallax-m4-canary-stage-1`，7 個 panels：T1/T2/T3 gauges + T4/T5 stats + outcome timeseries + rollback state)
- [ ] **Prometheus alert rules `parallax-m4-canary.rules.yml`** 已部署到 `/etc/prometheus/rules/`（來源：`prometheus/rules/parallax-m4-canary.rules.yml`，5 個 triggers：T1/T2/T3/T4 critical + T5 warning gate）
- [ ] **Alertmanager routing** 已合併 `ops/alerting/m4-canary-alertmanager.example.yaml` 對應 fragment（PagerDuty + Slack #m4-canary，secrets 從 secret manager 注入）
- [ ] **runbook (`docs/m4-prep/canary-stage-runbook.md`)** 已分享給 oncall 團隊

### 驗證命令

```bash
# 確認 Grafana dashboard 存在（UID 比對最穩，title 偶爾會 i18n 化）
curl -s -H "Authorization: Bearer ${GRAFANA_TOKEN}" \
  "http://grafana:3000/api/dashboards/uid/parallax-m4-canary-stage-1" \
  | jq -r '.dashboard.title'
# 預期：包含 "M4 Canary Stage 1"（中英皆可，視 publish 時 title）

# 確認 Prometheus alert rules 已載入（5 條 rules）
curl -s "http://prometheus:9090/api/v1/rules" \
  | jq '.data.groups[] | select(.name == "parallax_m4_canary") | .rules | length'
# 預期：== 5

# 確認 alert rule 檔案存在
ls -la /etc/prometheus/rules/parallax-m4-canary.rules.yml
# 預期：檔案存在且非空

# 本地 promtool lint（CI 自動跑 .github/workflows/prometheus-rules-check.yml）
promtool check rules prometheus/rules/parallax-m4-canary.rules.yml
# 預期：SUCCESS — 5 rules

# 確認 Alertmanager 路由匹配 m4-canary 標籤
amtool config routes test --config.file=/etc/alertmanager/alertmanager.yml \
    severity=critical component=parallax_m4_canary trigger=T1
# 預期：m4-canary-pagerduty + m4-canary-slack 兩個 receiver 都 match

# 確認 PagerDuty / Slack webhook（傳一次 dry-run 通知；US-009.3 deliverable）
parallax canary --check-alerting
# 預期：PagerDuty + Slack 均回 200 OK

# 確認 runbook 已分享
ls -la docs/m4-prep/canary-stage-runbook.md
# 預期：檔案存在
```

---

## 7. Rollback Path Drill（T-1h 內驗 1 次）

> **T-1 hour** 內必須實際走過一次完整 rollback 路徑，確認 < 30 min 完成。

### Rollback 步驟

| 步驟 | 動作 | 預期耗時 | Timeout |
|------|------|----------|---------|
| 1 | drain in-flight requests | ~60s（典型）/ 上限 300s | `DRAIN_TIMEOUT_SEC=300`（@50% 改用 600；對齊 `canary-stage-runbook.md` §7） |
| 2 | flag flip `ENABLE_M4_CANARY=false` | ~5s | n/a |
| 3 | Orbit re-emit（idempotency 保護） | ~120s | n/a |
| 4 | verify metric 回 baseline（5 min check） | ~300s | n/a |

### Checklist

- [ ] **T-1h 內走過上述 4 步**，confirm 總時間 < 30 min

### 驗證命令

```bash
# 步驟 1: drain in-flight requests
# 對齊 canary-stage-runbook.md §7 Step 1 的 prod default（@50% 改用 600s）
export DRAIN_TIMEOUT_SEC=${DRAIN_TIMEOUT_SEC:-300}
parallax canary --drain --timeout=${DRAIN_TIMEOUT_SEC}s
echo "Step 1 done — drain complete (timeout=${DRAIN_TIMEOUT_SEC}s)"

# 步驟 2: flag flip（Python CLI；對齊 runbook §7 Step 2）
parallax canary --set ENABLE_M4_CANARY=false
echo "Step 2 done — canary disabled"

# 步驟 3: Orbit re-emit
parallax orbit --re-emit --idempotent
echo "Step 3 done — Orbit re-emit complete"

# 步驟 4: verify metric 回 baseline
sleep 300
parallax canary --verify-baseline --window=5m
echo "Step 4 done — baseline verified"

# 總時間驗證
echo "Rollback drill complete — verify total time < 30 min"
```

---

## 8. Stakeholder Communication

> 確保所有相關人員已通知、排班已確認。

### Checklist

- [ ] **Chris 拍板**（signed off in Notion P×A dashboard）
- [ ] **oncall 排班確認**（24h 後 stage @10% 推進，oncall 在線）
- [ ] **Slack `#parallax-deploy` 公告**（T-30min）
- [ ] **Status page 更新**（canary in progress）

### 驗證命令

```bash
# 確認 Notion sign-off (手動檢查)
echo "→ 請至 Notion P×A dashboard 確認 Chris 已 sign off M4 stage @1%"
echo "   URL: https://notion.so/parallax/pxa-dashboard"

# 確認 oncall 排班
parallax oncall --check-schedule --next=24h
# 預期：oncall 工程師已排班且在線

# Slack 公告 (T-30min)
curl -X POST "${SLACK_WEBHOOK_URL}" \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "🚀 M4 Canary Stage @1% 啟動中 — T-30min\nOncall: @engineian\nRunbook: <https://wiki.parallax/canary-stage-runbook|canary-stage-runbook.md>"
  }'
# 預期：200 OK

# Status page 更新
parallax status-page --update \
  --message="M4 canary deployment in progress (stage @1%)" \
  --status=investigating
# 預期：更新成功
```

---

## 9. Final GO / NO-GO

### 判定規則

| 狀態 | 條件 | 動作 |
|------|------|------|
| ✅ **GO** | 第 2 ~ 8 章節所有 checkbox 全綠 | 執行 stage @1% 啟動命令 |
| 🛑 **NO-GO** | 任一 checkbox 為紅 | **STOP**，記錄 blocker 於 `m4-launch-blockers.md`，通知 Chris |

### NO-GO 流程

```bash
# 建立 blocker 記錄
cat >> docs/m4-prep/m4-launch-blockers.md << 'EOF'

## Blocker — $(date -u +%Y-%m-%dT%H:%M:%SZ)

- **Section**: [填入章節編號]
- **Checkbox**: [填入未通過項目]
- **Root cause**: [填入原因]
- **Owner**: [填入負責人]
- **ETA to fix**: [填入預計修復時間]
EOF

# 通知 Chris
echo "🛑 M4 Stage @1% NO-GO — blocker 已記錄，請見 m4-launch-blockers.md"
```

### GO 流程

全 8 章節 checkbox 全綠後，執行 stage @1% 啟動命令（見 §10）。

---

## 10. 範例命令（Bash Code Blocks 完整版）

```bash
# ============================================
# 啟動 stage @1%
# ============================================
parallax canary --start --stage=1
# 預期：canary 啟動，1% 流量導入

# ============================================
# 即時查看 canary status + dashboard URL
# ============================================
parallax canary --status
# 預期：輸出 current stage, metrics summary, Grafana dashboard URL

# ============================================
# 緧急 rollback
# ============================================
parallax canary --abort
# 預期：立即停止 canary，drain in-flight，flag flip，Orbit re-emit

# ============================================
# 查看當前 stage 推進歷史
# ============================================
parallax canary --history
# 預期：列出所有 stage transition + timestamp + metrics snapshot

# ============================================
# 手動推進至下一 stage (需 Chris ACK)
# ============================================
parallax canary --promote --stage=10 --ack-by=chris
# 預期：推進至 stage @10%，需 Chris 簽核
```

---

## Appendix: Checklist Summary Table

| # | 章節 | Checkbox 數 | 狀態 |
|---|------|-------------|------|
| 2 | M3 14-day Corpus DoD | 6 | ☐ |
| 3 | Aphelion v0.5.x Toolkit | 3 | ☐ |
| 4 | Canary Infra (US-009.1) | 4 | ☐ |
| 5 | DoD Scripts (US-009.3) | 3 | ☐ |
| 6 | Observability | 4 | ☐ |
| 7 | Rollback Path Drill | 1 | ☐ |
| 8 | Stakeholder Communication | 4 | ☐ |
| **Total** | | **25** | |

> **全 25 項 checkbox 全綠 → GO**  
> **任一紅 → STOP → 記錄 blocker → 通知 Chris**
