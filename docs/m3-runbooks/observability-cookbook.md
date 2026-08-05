# M3 Observability Cookbook

> **文件版本**：v0.3.1　｜　**最後更新**：2026-04-30  
> **適用範圍**：Lane C v0.3 Dual-read（M3）  
> **維護者**：Parallax SRE — M3 oncall rotation

---

## 1　用途與受眾

本文件為 **M3 oncall 工程師** 的日常操作手冊，目標是：

1. 將 6 個 DoD gauges、15 個 Grafana panels、5 條 Prometheus alert rules 串成一條可執行的觀測鏈。
2. 提供三大常見異常場景的標準排查 SOP，降低 MTTR。
3. 在 14-day corpus 觀察期（day 0 = 2026-04-30）內，定義每日 health check 動作。

**閱讀前提**：你應已讀過 `q8-drain-runbook.md` 與 `circuit-breaker-runbook.md`，並擁有 Grafana M3 dashboard 與 Prometheus alertmanager 的唯讀權限。

---

## 2　DoD Gauges 速查表

> **命名規則**：所有 metric 都帶 `parallax_` prefix，下表已對齊 `parallax/server/routes/metrics.py` + `prometheus/rules/parallax-dual-read.rules.yml` 的真實 series。
> **欄位拆兩列**：「Gauge 部署」= metrics endpoint 是否暴露此 series；「Alert 部署」= `parallax-dual-read.rules.yml` 是否有對應 alert rule。

| # | Gauge 名稱 | Gauge 部署 | Alert 部署 | Alert 閾值（已部署）/ Rationale |
|---|-----------|-----------|-----------|-------------------------------|
| 1 | `parallax_dual_read_discrepancy_rate` | ✅ | ✅ `DualReadDiscrepancyRateHigh` | `> 0.001`（0.1 %）for 30m / severity warning（對齊 M3 DoD 的 0.1 % 門檻） |
| 2 | `parallax_arbitration_conflict_rate` | ✅ | ✅ `ArbitrationConflictRateHigh` | `> 0.015`（1.5 %）for 1m / severity warning（alert 較鬆，留 0.5 % buffer 對 14-day corpus DoD 的 1 % 退場條件） |
| 3 | `parallax_dual_read_write_error_rate` | ✅ | ✅ `DualReadWriteErrorRateHigh` | `> 0.0005`（0.05 %）for 2m / severity warning（alert 較鬆，留 0.03 % buffer 對 corpus DoD 的 0.02 %） |
| 4 | `parallax_aphelion_unreachable_rate`（gauge，72h DoD 用）／ `parallax_dual_read_outcomes:sum_without_user_id`（alert 用） | ✅（#101 起才真正上線） | ✅ `AphelionUnreachableRateHigh` | **300s** natural ratio `> 0.01`（1 %）+ 分母 `>= 50` for 0m / severity warning（對齊 breaker 的 300s/1%/50-obs）。⚠️ gauge 與 alert 讀**不同窗**是刻意的：gauge = 72h corpus DoD，alert = 300s 營運事件，見 §4.A 註解 |
| 5 | `parallax_crosswalk_miss_orphan_total` / `parallax_dual_read_outcomes_total` | 分母 ✅（#101 起）；**分子 ❌ 無 producer** | ⚠️ `CrosswalkMissRateHigh` 目前恆為 no-data | rate 比 `> 0.05`（5 %）for 30m / severity warning |
| 6 | `parallax_circuit_breaker_tripped_total` | ✅ | ✅ `CircuitBreakerTripped` | `increase[10m] > 0` for 0m / severity critical（單調 counter，**沒有 72h windowed gauge**） |

> **重要修正**：
> - 不存在 `circuit_open_count_72h` gauge，請改用 `parallax_circuit_breaker_tripped_total` counter + `increase[Xh]` 表達式。
> - **「Gauge 部署 ✅」≠ 真的能被 scrape 到**（#101）。`/metrics` 的 `_build_payload` 會序列化一份**全新的** `CollectorRegistry`，只把固定名單上的 counter 從 default registry 撈出來；註冊在 default registry 但不在名單上的 collector，永遠不會出現在任何一次 scrape。第 4 列與第 5 列的分母都踩過這個坑（分別註冊於 `discrepancy_live.py`，卻從未被 render），alert 與 Grafana panel 長期比對到空集合。加新 gauge/counter 時，驗收條件是**實際 scrape 一次看到那一行**，不是「有註冊」。
> - `parallax_arbitration_conflict_rate` 與 `parallax_dual_read_write_error_rate` 對應 alert rule 已部署（2026-05-04 PR），閾值 `1.5 %` / `0.05 %` 設計留 buffer 對 14-day corpus DoD 1 % / 0.02 %。
> - 14-day corpus 退場條件 `< 0.1 %` 對齊 deployed `DualReadDiscrepancyRateHigh > 0.001`。

---

## 3　Grafana 15 Panels 分組

Dashboard URL：`https://grafana.internal/d/m3-lane-c-v03/`

### Block A — 即時指標（Row 1，3 panels）

| Panel | 指標來源 | 顯示方式 | 說明 |
|-------|---------|---------|------|
| A-1 | `parallax_dual_read_discrepancy_rate` | Stat + sparkline | 即時雙讀不一致率，**紅色閾值線 0.1 %**（對齊 deployed `DualReadDiscrepancyRateHigh`） |
| A-2 | `parallax_arbitration_conflict_rate` | Stat + sparkline | 即時仲裁衝突率（黃色閾值線 1.5 %，alert deployed `ArbitrationConflictRateHigh`） |
| A-3 | `parallax_dual_read_write_error_rate` | Stat + sparkline | 即時雙寫錯誤率（黃色閾值線 0.05 %，alert deployed `DualReadWriteErrorRateHigh`） |

> **操作提示**：Block A 是 oncall 每次 alert 觸發時第一眼看到的區域。若三個 panel 均為綠色，可快速排除 M3 本身問題。

### Block B — 趨勢與比較（Row 2–3，6 panels）

| Panel | 指標來源 | 顯示方式 | 說明 |
|-------|---------|---------|------|
| B-1 | `parallax_dual_read_discrepancy_rate` | Time series（72h rolling） | 72 小時滾動趨勢，疊加 deploy marker |
| B-2 | `parallax_arbitration_conflict_rate` | Time series（72h rolling） | 同上，仲裁衝突趨勢 |
| B-3 | `parallax_dual_read_write_error_rate` | Time series（72h rolling） | 同上，寫入錯誤趨勢 |
| B-4 | `parallax_aphelion_unreachable_rate` | Time series（72h rolling） | Aphelion 不可達率（已部署 alert `AphelionUnreachableRateHigh`） |
| B-5 | `parallax_crosswalk_miss_orphan_total / parallax_dual_read_outcomes_total` rate | Time series（5m rate / 30m window） | crosswalk miss 比率（已部署 alert `CrosswalkMissRateHigh`） |
| B-6 | `parallax_circuit_breaker_tripped_total` | Bar chart（`increase[24h]` × 14d corpus 進度） | 斷路器跳脫次數 vs. corpus 觀察期日曆（counter 是 monotonic，圖表用 increase 計算每日跳脫量） |

> **操作提示**：B-6 panel 同時顯示 14-day corpus 觀察期的 day counter，方便快速定位當前處於觀察期第幾天。

### Block C — 衍生分析（Row 4–6，6 panels）

| Panel | 指標來源 | 顯示方式 | 說明 |
|-------|---------|---------|------|
| C-1 | `parallax_dual_read_discrepancy_rate` by query_type | Heatmap | 依 query type 拆分的不一致率熱力圖 |
| C-2 | `parallax_arbitration_conflict_rate` by result | Pie chart | 仲裁結果分佈（auto-resolved / manual / timeout） |
| C-3 | `parallax_dual_read_write_error_rate` by endpoint | Table | 依寫入端點拆分的錯誤率明細 |
| C-4 | `parallax_dual_read_discrepancy_rate` vs `parallax_arbitration_conflict_rate` | Correlation scatter | 雙讀不一致 vs 仲裁衝突相關性 |
| C-5 | `parallax_aphelion_unreachable_rate` by node | Table | 依 Aphelion 節點拆分的不可達率（gauge 已部署） |
| C-6 | `parallax_circuit_breaker_tripped_total` timeline | Event timeline | 斷路器跳脫事件時間線（每次 counter increase = 一次跳脫），疊加 deploy / drain 事件 |

> **操作提示**：排查場景 A/B 時，先看 C-1/C-2 定位問題 query type 或仲裁規則，再回溯 Block B 趨勢確認是否為突發或漸進。

---

## 4　Prometheus Alert Rules 與 Severity 分級

> 本節分兩段：(A) **首批 deployed** 規則；(B) **第二批 deployed**（2026-05-04 補上 `ArbitrationConflictRateHigh` + `DualReadWriteErrorRateHigh`）。所有 alert 都活在同一個 `parallax_dual_read` group。閾值以 rules.yml 為準。

### 4.A　已部署規則（`prometheus/rules/parallax-dual-read.rules.yml`，single source of truth）

```yaml
groups:
  - name: parallax_dual_read
    interval: 30s
    rules:
      # discrepancy（M3 DoD 主訊號）
      # 2026-08-02 起 selector 帶 traffic_source：三個 dual-read DoD gauge 都
      # 依 partition 分 label，門檻只對 natural 判（synthetic = burn-in loader，
      # unknown = 舊 corpus）。沒有自然流量時該 series **不存在**（no-data），
      # 不是 0.0——這是刻意的，0 會被誤讀成「量過而且健康」。
      - alert: DualReadDiscrepancyRateHigh
        expr: |
          max by (traffic_source) (parallax_dual_read_discrepancy_rate{traffic_source="natural"}) > 0.001
        for: 30m
        labels: { severity: warning, component: parallax_dual_read }

      # Aphelion 不可達（circuit breaker 正在跳 / 即將跳）
      # 2026-08-05（PR #103 review）起改讀 **300s counter ratio**，不再讀 72h DoD gauge：
      # breaker（parallax/router/circuit_breaker.py）是 300s / 1% / >=50 observations，
      # 72h gauge 在「大量健康 backlog + 突發斷線」時會被平均掉而永遠不觸發——
      # 正是這條 alert 存在的那個場景。**五個項**全部對齊 breaker：
      #   窗 [5m]=WINDOW_SECONDS 300s／閾值 >0.01=TRIP_THRESHOLD／分母 >=50=MIN_OBSERVATIONS
      #   ／母體 outcome!="skipped"=dual_read.py:501 那道 gate／範圍 by (job,instance)=breaker 是 process-local
      # 分母 `>= 50` 同時是 cold-start flap guard 與除零保護；不足 50 筆是 no-data（**不是** 0.0）。
      # `skipped`（DUAL_READ 關閉、CHANGE_TRACE legacy_kind=bug 短路）根本沒呼叫 Aphelion，
      # 算進分母會稀釋成「看起來健康」而 breaker 已經在跳。`primary_only` **要留著**，它有進 breaker。
      - alert: AphelionUnreachableRateHigh
        expr: |
          (
            sum by (job, instance, traffic_source) (
              increase(parallax_dual_read_outcomes:sum_without_user_id{traffic_source="natural",outcome="aphelion_unreachable"}[5m])
            )
            /
            (
              sum by (job, instance, traffic_source) (
                increase(parallax_dual_read_outcomes:sum_without_user_id{traffic_source="natural",outcome!="skipped"}[5m])
              ) >= 50
            )
          ) > 0.01
        for: 0m        # [5m] range 本身就是 breaker 的窗，再加 for: 會晚於它要預警的那次 trip
        labels: { severity: warning, component: parallax_dual_read }

      # circuit breaker 跳脫（counter increase）
      - alert: CircuitBreakerTripped
        expr: increase(parallax_circuit_breaker_tripped_total[10m]) > 0
        for: 0m
        labels: { severity: critical, component: parallax_dual_read }

      # drain timeout（lifespan 內部 900s drain 提前被切）
      - alert: DrainTimeoutDetected
        expr: increase(parallax_drain_timeout_total[1h]) > 0
        for: 0m
        labels: { severity: critical, component: parallax_dual_read }

      # crosswalk miss rate
      - alert: CrosswalkMissRateHigh
        expr: |
          (rate(parallax_crosswalk_miss_orphan_total[5m])
           / (rate(parallax_dual_read_outcomes_total[5m]) > 0)) > 0.05
        for: 30m
        labels: { severity: warning, component: parallax_dual_read }
```

### 4.B　Deployed 規則 — 第二批（2026-05-04 PR）

> 兩條 alert 直接寫進 §4.A 同一個 `parallax_dual_read` group（單一 SSoT），閾值刻意比 14-day corpus DoD 退場條件鬆，留 buffer 避 false positive — 詳見 §2 註解。
> 真檔位置：`prometheus/rules/parallax-dual-read.rules.yml`，無獨立 `parallax_dual_read_proposed` group。

```yaml
- alert: ArbitrationConflictRateHigh
  # 1.5 % alert / 1 % corpus DoD；2026-08-02 起只對 natural partition 判
  expr: |
    max by (traffic_source) (parallax_arbitration_conflict_rate{traffic_source="natural"}) > 0.015
  for: 1m
  labels: { severity: warning, component: parallax_dual_read }
  annotations:
    runbook: docs/m3-runbooks/observability-cookbook.md#場景-b-arbitration-conflict

- alert: DualReadWriteErrorRateHigh
  # 0.05 % alert / 0.02 % corpus DoD；2026-08-02 起只對 natural partition 判
  expr: |
    max by (traffic_source) (parallax_dual_read_write_error_rate{traffic_source="natural"}) > 0.0005
  for: 2m
  labels: { severity: warning, component: parallax_dual_read }
  annotations:
    runbook: docs/m3-runbooks/observability-cookbook.md#場景-a-discrepancy-spike
```

### Severity 對應

> Deployed rules 用 Prometheus 標準的 `severity: warning` / `severity: critical`；下表是 oncall 內部分級對應。

| Prometheus severity | 內部分級 | 通知管道 | 回應 SLA | 對應 deployed alerts |
|---|---|---|---|---|
| `critical` | P0 | PagerDuty + Slack #m3-incidents | 5 分鐘內 ack | `CircuitBreakerTripped`、`DrainTimeoutDetected` |
| `warning` | P1 | Slack #m3-alerts + email | 15 分鐘內 ack | `DualReadDiscrepancyRateHigh`、`AphelionUnreachableRateHigh`、`CrosswalkMissRateHigh`、`ArbitrationConflictRateHigh`、`DualReadWriteErrorRateHigh` |

---

## 5　三大 Oncall 場景應對

### 場景 A：Discrepancy Spike

**觸發條件**：`DualReadDiscrepancyRateHigh`（severity: warning，deployed §4.A）

**排查步驟**：

1. **確認範圍** → 打開 Block A-1，確認 spike 時間點與持續時間。
2. **定位 query type** → 切到 Block C-1 熱力圖，找出 discrepancy 集中的 query type。
3. **檢查路由** → 查詢 `route_decision_log`，確認是否有非預期的路由切換（例如 Aphelion fallback 觸發）。
4. **比對雙寫一致性** → 檢查 Block A-3 write error rate，若同步升高，優先排查雙寫路徑。
5. **查看 deploy marker** → Block B-1 是否有近期 deploy 對應 spike。
6. **決策**：
   - 若為單一 query type 且 discrepancy < 0.5 % → 持續觀察 15 分鐘。
   - 若 discrepancy > 0.5 % 或擴散至多 query type → 啟動 `q8-drain-runbook.md` drain 流程。

### 場景 B：Arbitration Conflict

**觸發條件**：`ArbitrationConflictRateHigh`（severity: warning，deployed §4.B）

**排查步驟**：

1. **確認衝突分佈** → Block C-2 pie chart，看 auto-resolved vs manual vs timeout 比例。
2. **定位欄位** → 查詢 `arbitration_detail_log`，找出衝突集中的欄位名稱。
3. **比對 source-level mismatch** → 檢查上游資料源是否有 schema 變更或資料延遲。
4. **檢查仲裁規則** → 確認 `arbitration_rules.yaml` 中對應欄位的優先序是否仍合理。
5. **決策**：
   - 若 auto-resolved > 90 % → 記錄 event，觀察。
   - 若 manual/timeout > 10 % → 升級至 M3 platform team，必要時暫停該欄位的雙讀。

### 場景 C：Circuit Breaker Trip

**觸發條件**：`CircuitBreakerTripped`（severity: critical，deployed §4.A）或前置警示 `AphelionUnreachableRateHigh`（severity: warning，deployed §4.A）

**排查步驟**：

1. **確認跳脫時間線** → Block C-6 event timeline，定位每次跳脫的精確時間。
2. **交叉比對** → Block B-4 aphelion unreachable rate（若已部署），確認是否為 Aphelion 節點問題。
3. **執行 circuit-breaker-runbook 手動 reset 流程** → BreakerState 只有 binary `tripped: bool`（見 `parallax/router/circuit_breaker.py:84-86`），**無 half-open / 無 cooldown / 無 probe**；Q10 ralplan 明確不提供 auto-recovery（避免 thrashing 放大事件）。依照 `circuit-breaker-runbook.md` 的順序執行：
   1. 確認 Aphelion `/health` 回 200。
   2. 確認根因（Aphelion 服務異常）已解決。
   3. `curl -X POST -H "Authorization: Bearer $PARALLAX_TOKEN" http://<parallax-admin>/admin/circuit_breaker/reset`，預期 HTTP 200 + `was_tripped: true`。
   4. 觀察 `parallax_inflight_requests` 從近 0 緩升（重新對 Aphelion 發請求的訊號）。
4. **記錄根因** → 填寫 incident log，標記是否需要調整 circuit breaker threshold（`TRIP_THRESHOLD=0.01` / `MIN_OBSERVATIONS=50` / `WINDOW_SECONDS=300`）。

---

## 6　14-Day Corpus 觀察期（Day 0 = 2026-04-30）

在觀察期內，oncall 工程師需於 **每日 UTC 09:00** 執行以下 health check：

| 步驟 | 動作 | 預期結果 | 異常處理 |
|------|------|---------|---------|
| 1 | 檢查 Block A 三個即時指標 | 全部綠色 | 若任一紅色 → 依場景 A/B/C 處理 |
| 2 | 檢查 Block B-6 day counter | Day N 與日曆吻合 | 若不符 → 確認 deploy pipeline |
| 3 | 檢查 Block B-1~B-3 趨勢 | 無明顯上升趨勢 | 若有上升 → 記錄並標記為 watch item |
| 4 | 檢查 Block C-1 熱力圖 | 無新 query type 出現異常 | 若有 → 新增至 watch list |
| 5 | 檢查 `increase(parallax_circuit_breaker_tripped_total[72h])` | ≤ 3 | 若 > 3 → 累計跳脫過頻，安排根因分析 |
| 6 | 更新觀察期 tracker spreadsheet | 記錄當日 status | — |

> **觀察期結束條件**（Day 14 = 2026-05-13）：
> - 所有已部署 alerts 連續 14 天無 critical 級觸發。
> - `parallax_dual_read_discrepancy_rate` 72h rolling 均值 < 0.1 %（對齊 deployed `DualReadDiscrepancyRateHigh > 0.001`）。
> - `increase(parallax_circuit_breaker_tripped_total[14d])` 累計 ≤ 5。
>
> 若未達標，觀察期自動延長 7 天，並通知 M3 platform team。

---

## 7　與其他 Runbook 連動

| Runbook | 觸發時機 | 連動方式 |
|---------|---------|---------|
| **`q8-drain-runbook.md`** | 場景 A 中 discrepancy > 0.5 % 且持續擴散 | 啟動 Q8 drain 流程，將流量從 Lane C 切回 Lane A/B；drain 期間 Block A 指標應逐步回落 |
| **`circuit-breaker-runbook.md`** | 場景 C 斷路器跳脫 | 依照 circuit breaker state machine 操作；reset 後需觀察 Block B-4/B-6 至少 30 分鐘確認穩定 |

> **連動原則**：執行 drain 或 circuit breaker reset 後，oncall 工程師需在 Slack #m3-incidents 發布狀態更新，並在 incident log 中記錄操作時間與觀察結果。

---

## 附錄：快速連結

- Grafana Dashboard：`https://grafana.internal/d/m3-lane-c-v03/`
- Prometheus Alerts：`https://prometheus.internal/alerts?group=m3_lane_c_alerts`
- Incident Log Template：`https://wiki.internal/m3/incident-log-template`
- Observability Cookbook（本文件）：`docs/m3-runbooks/observability-cookbook.md`

---

*本文件由 Parallax SRE 維護。如有問題，請於 Slack #m3-oncall 提出。*
