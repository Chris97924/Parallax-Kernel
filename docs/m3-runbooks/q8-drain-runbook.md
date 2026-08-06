# M3 Lane C v0.3 Dual-read — Graceful Drain Handler Runbook

> **文件編號**：`docs/m3-runbooks/q8-drain-runbook.md`
> **適用版本**：M3 dual-read（Lane C v0.3）
> **最後更新**：2026-04-27
> **關聯 Ralplan**：2026-04-27 §10 Q8
> **Owner**：Parallax SRE

---

## 用途 / 觸發情境

本 runbook 覆蓋 M3 dual-read 模式下，**deploy 或 restart** 時的 graceful drain 流程。

觸發情境：

| 情境 | 說明 |
|------|------|
| **Rolling deploy** | 新版本 Pod 就緒後，舊版本進入 drain |
| **手動 restart** | `systemctl restart parallax-dual-read` 或 pm2 restart |
| **OOM / CrashLoop** | 進程異常退出，需確認 inflight 是否已歸零 |
| **Scale-in** | HPA 或手動縮容，淘汰舊實例 |

核心原則：**新請求路由到新版本，舊版本只完成 in-flight 請求後收尾**。Drain 由 `parallax/server/lifespan.py` 內建的 asyncio 自管 drain loop 處理。

> **判準來源（2026-08-06 #102 review 更正）**：oncall 判斷 drain 是否完成，主訊號是
> **systemd/編排層的 lifecycle 狀態 + drain log 行**，**不是** Prometheus。
> SIGTERM 之後 `parallax_inflight_requests` 與 `parallax_drain_timeout_total`
> **都掃不到**（實測，見下節「觀測性硬限制」）。PromQL 只在 SIGTERM 前有效。

---

## Drain 行為定義（FastAPI Lifespan 自管 Drain Loop）

實作位置：`parallax/server/lifespan.py::parallax_lifespan` + `_drain_inflight`。

**核心常數**：

| 常數 | 值 | 說明 |
|------|---|------|
| `DRAIN_TIMEOUT_SECONDS` | `900.0`（15 min） | SIGTERM 後最大 drain 等待時間 |
| `DRAIN_POLL_INTERVAL_SECONDS` | `0.5` | 每 0.5s polling 一次 `get_inflight_count()` |

**行為邏輯**：

1. **進程啟動**：FastAPI 透過 `lifespan` context manager 啟動；`parallax_inflight_requests` gauge 初始化為 0。
2. **請求進入 / 完成**：`InflightTracker` context manager 在 handler enter 時 `inc()`，exit 時 `dec()`（即使 raise 也會 dec，見 `parallax/router/inflight.py`）。
3. **收到 SIGTERM**：FastAPI lifespan 進入 shutdown 分支，呼叫 `_drain_inflight()`，**進程自動進入 graceful drain**（**不需要 oncall 手動介入**）。
4. **drain loop**：每 0.5s 讀取 `get_inflight_count()`，若 ≤ 0 立即 return（log INFO `drain complete in {elapsed}s`）；若 deadline 到（900s），增 `parallax_drain_timeout_total` counter + log WARNING（`drain timeout after {n}s — {c} request(s) still in flight`）+ return（讓 process 收尾）。**這兩行 log 是 oncall 的判準來源；同一時點增的 counter 則掃不到（限制 2）。**
5. **觀察方式**：oncall **僅作觀察**，**勿在 drain 自然完成前強制 SIGKILL**（會吃掉本來會 drain 完的 in-flight）。觀察用什麼訊號**請先讀下一節「觀測性硬限制」**——SIGTERM 之後 Prometheus 兩個 metric 都看不到，主訊號是 systemd lifecycle + log。

---

## ⚠️ 觀測性硬限制（SIGTERM 之後 Prometheus 看不到 drain）

**這一節是本 runbook 所有程序步驟的前提。** 兩個限制都是 #102 review 對真實 uvicorn 0.52.1 server 實測出來的，不是推論。

### 限制 1：SIGTERM 之後，`parallax_inflight_requests` 的下降過程掃不到

uvicorn 在 shutdown 一開始就**先關掉 listening socket**，然後才等 in-flight 請求跑完。所以從 SIGTERM 那一刻起，Prometheus **建立不了新連線**，掃不到任何東西：

```
scrape_before_sigterm          : parallax_inflight_requests 0.0     <- idle，正確
scrape_while_busy_pre_sigterm  : parallax_inflight_requests 1.0     <- 有真流量，正確
scrape_during_drain_after_sigterm: [ConnectTimeout, ConnectTimeout,
                                    ConnectTimeout, ConnectTimeout] <- SIGTERM 後全滅
slow_request_outcome           : 200                                <- 該請求其實有跑完
```

也就是說 gauge 確實從 1 掉到 0 了（請求正常回 200），但**沒有任何一次 scrape 看得到那個下降**。

後果：**Prometheus 手上最後一個樣本永遠是 SIGTERM 前那個「正值」**，撐到 staleness（~5 min）後才變 empty。所以

> **`sum(parallax_inflight_requests) == 0` 這種 poll gate 永遠等不到 0。**
> 它只會一直讀到 stale 正值 → 然後 empty，兩種都不是「完成」。

`/metrics` 與 `/healthz` 已排除出 gauge（見下節），那修的是**「掃得到的時候讀數對不對」**；它**不會**、也不可能讓上面這個 gate 變可靠——socket 都關了，讀數對不對已經沒差。

**因此本 runbook 的 drain 完成判準用 systemd lifecycle（步驟 2），不用 PromQL。** PromQL 只在 **SIGTERM 之前**有效，當作「還有多少在飛」的平衡檢查。

### 限制 2：`parallax_drain_timeout_total` 在生產環境觀測不到

該 counter 在 lifespan shutdown 期間、`_drain_inflight()` 尾端才 `inc()`，
而**那個時間點 listening socket 已經關閉**：從 shutdown 視窗內發出的 3 次
scrape 全部 ConnectTimeout，連裸 TCP connect 都 timeout。所以 Prometheus
拿到的最後一個樣本永遠是 increment 前的 0，`increase()` 看不到跳變，
`DrainTimeoutDetected` alert **在真的 drain timeout 時也不會響**。

把 increment 提早到 timeout 偵測當下**也不能解**——整個 drain 都在同一個
「已不服務」的視窗內；由「即將終止的 exporter 自己」持有的 counter 無法
回報自己的終止。要修需要 durable/external 路徑（audit-db 落一筆，或對
log 行做 log-based alert），屬 producer 工作。

另有實測發現：**uvicorn 會先自己等 in-flight 請求結束才進 lifespan
shutdown**（SIGTERM 當下仍在跑的請求正常回 200，隨後 `_drain_inflight`
看到的 inflight 已是 0），所以 timeout 分支在 uvicorn 下幾乎不可達。

> **oncall 實務**：判斷 drain 有沒有 timeout，一律抓 log 行
> `parallax.lifespan: drain timeout after`（步驟 2b）。
> **alert 沒響不代表 drain 乾淨。**

### 這兩個限制對照表

| 想知道什麼 | ❌ 不要用 | ✅ 用什麼 |
|---|---|---|
| SIGTERM 前還有多少請求在飛 | — | `parallax_inflight_requests`（PromQL，步驟 1） |
| 舊進程 drain 完了沒 | `sum(parallax_inflight_requests) == 0` | systemd/編排層 lifecycle：舊 MainPID 是否退出（步驟 2） |
| drain 有沒有被 900s 硬斷 | `increase(parallax_drain_timeout_total[...])`、`DrainTimeoutDetected` alert | journalctl 抓 `parallax.lifespan: drain ` log 行（步驟 2b） |
| 查不到上述訊號時 | 當作沒問題放行 | **fail closed**：明確回報「無法確認」並擋住 |

> **設計重點**：`_drain_inflight` 用 `asyncio.sleep`（不是 `time.sleep`），所以 drain loop 跟其他 coroutine（包含正在 drain 的 in-flight 請求）可以並行進度。

---

## 觀察指標：parallax_inflight_requests（**僅 SIGTERM 前有效**）

> **Gauge 語義（#102 起 gauge 才真的上線，2026-08-06）**：
> `parallax_inflight_requests` 只計「drain 必須等的應用請求」。
> `/metrics` 與 `/healthz` **不計入**（`INFLIGHT_EXCLUDED_PATHS`，
> `parallax/server/middleware/dual_read_snapshot.py`）。
> 這是必要的：gauge 是在「服務該次 scrape 的當下」被讀取的，若把 `/metrics`
> 算進去，每次 scrape 都至少回報 1，**idle 實例永遠不可能回報 0**。
> `/healthz` 一併排除，否則同一個判準會變成間歇性 flaky（更難查）。
>
> **這個修正只讓「掃得到的時候讀數是對的」**——它處理的是讀數正確性。
> 它**不**讓任何 `== 0` 的 poll gate 變可靠：見上面「限制 1」，SIGTERM 之後
> 根本掃不到。**下面的表格與查詢一律只在 SIGTERM 之前成立。**
> （r1 版本這裡曾寫「本文的 `== 0` 判準在修正後才成立」，那句是錯的，
> 已於 r2 更正——排除 `/metrics` 與 gate 可靠性是兩件事。）

下表是**進程還在服務時**「還有多少請求在飛」的預期收斂速度，供 SIGTERM 前的平衡檢查與異常判斷用；**不是** drain 完成判準（那個看步驟 2 的 systemd 訊號）。

| 條件 | 預期 inflight 收斂時間 |
|------|----------------|
| 正常流量（p99 latency < 500ms） | **< 10 秒** |
| 高延遲查詢（p99 ~ 2s） | **< 15 秒** |
| 含外部 IO 回調（DB / RPC） | **< 30 秒** |
| 異常：inflight 卡住 | **> 60 秒 → 進入異常處理** |

**觀察方式（SIGTERM 前）**：

```bash
# 即時觀察 —— 僅在進程仍在服務時有意義
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=parallax_inflight_requests{job="m3-dual-read"}' | jq

# Grafana 面板
# Dashboard: M3 Dual-Read → Panel: Inflight Requests
#   ⚠️ 這個 panel 在 SIGTERM 後會停在最後一個正值（stale），不是「還沒 drain 完」。
```

---

## Oncall 動作

### 步驟 1：**SIGTERM 前**的平衡檢查（PromQL 唯一有效的用途）

**先做這一步，再送 SIGTERM。** 一旦 SIGTERM 送出，這個查詢就失效了（限制 1）。

```bash
# 確認目標實例當下還有多少在飛 —— 進程必須仍在服務中
INSTANCES=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=parallax_inflight_requests{job="m3-dual-read"} > 0' | jq -r '.data.result[].metric.instance')

echo "SIGTERM 前尚有 inflight 的實例：${INSTANCES:-（無）}"
```

這一步告訴你「等一下大概要 drain 多久」，**不是** drain 完成判準。

> ⚠️ **不要**把這個查詢搬到 SIGTERM 之後重跑並期待它變 0。它不會。
> SIGTERM 後 socket 已關，Prometheus 讀到的是 stale 正值，接著變 empty；
> 兩者都不代表 drain 完成或未完成。完成與否**只看步驟 2 的 systemd 訊號**。

### 步驟 2：等 drain 完成（**主訊號 = systemd lifecycle，不是 PromQL**）

> **為什麼不是 PromQL**：見限制 1。SIGTERM 後 socket 先關，gauge 的下降過程掃不到，`sum(...) == 0` 永遠等不到。判準必須是**在「正在終止的 HTTP server 之外」也觀察得到的 lifecycle 狀態**——舊進程是否真的退出了。
>
> **重點不變**：drain 由 `parallax_lifespan` 在進程內自動跑（最長 `DRAIN_TIMEOUT_SECONDS=900`）。oncall 的角色是**外部觀察 + 異常時介入**，**不是**外部超時關閉。`OBSERVE_TIMEOUT` 必須 ≥ 900s，給 buffer 取 960s。

```bash
UNIT=parallax-dual-read
OBSERVE_TIMEOUT=960   # 900s server drain + 60s buffer
ELAPSED=0

# 送 SIGTERM 前先記下舊進程 PID —— 這是「舊進程真的走了」的判準錨點。
OLD_PID=$(systemctl show -p MainPID --value "$UNIT")
if [ -z "$OLD_PID" ] || [ "$OLD_PID" = "0" ]; then
  echo "❌ 取不到 $UNIT 的 MainPID —— 無法確認 drain 狀態（fail closed）"; exit 1
fi
echo "舊進程 PID=$OLD_PID，開始等待其退出（上限 ${OBSERVE_TIMEOUT}s）"

while [ $ELAPSED -lt $OBSERVE_TIMEOUT ]; do
  ACTIVE=$(systemctl show -p ActiveState --value "$UNIT")
  SUB=$(systemctl show -p SubState --value "$UNIT")
  CUR_PID=$(systemctl show -p MainPID --value "$UNIT")

  # 完成判準：舊 PID 不在了（unit 停了，或已被新進程取代）。
  if ! kill -0 "$OLD_PID" 2>/dev/null; then
    echo "✅ Drain 完成：舊進程 $OLD_PID 已退出，耗時 ${ELAPSED}s（ActiveState=$ACTIVE SubState=$SUB CurrentMainPID=$CUR_PID）"
    echo "   ⚠️ 「退出」只代表 lifespan 跑完了，不代表沒 timeout —— 是否被 900s 切斷請看步驟 2b。"
    exit 0
  fi

  # failed 是終態，不用等滿 960s
  if [ "$ACTIVE" = "failed" ]; then
    echo "❌ $UNIT 進入 failed（SubState=$SUB）— 進入步驟 3"; exit 1
  fi

  echo "⏳ 舊進程 $OLD_PID 仍在（ActiveState=$ACTIVE SubState=$SUB），已等 ${ELAPSED}s（server-side drain 上限 900s）..."
  sleep 10
  ELAPSED=$((ELAPSED + 10))
done

echo "❌ 外部觀察超時（${OBSERVE_TIMEOUT}s），舊進程 $OLD_PID 仍未退出 — 進入步驟 3"
exit 1
```

**非 systemd 環境（k8s / pm2）用等價的 lifecycle 訊號**，原則一樣——問**編排層**、不要問正在死掉的 HTTP server：

```bash
# k8s：等舊 Pod 真的不見（Terminating -> 消失）
kubectl wait --for=delete pod/<old-pod> -n parallax --timeout=960s

# pm2：等該 process 的 pm_id 重啟計數變動 / 狀態離開 stopping
pm2 jlist | jq -r '.[] | select(.name=="parallax-dual-read") | .pm2_env.status'
```

### 步驟 2b：drain 有沒有被 900s 切斷（**看 log，不看 counter**）

舊進程退出**不等於** drain 乾淨——它可能是 hit 了 900s timeout 才收尾。判斷唯一可靠訊號是 WARNING log 行（限制 2）：

```bash
UNIT=parallax-dual-read

# 只看這次 drain 的時間窗；--since 用你送 SIGTERM 的時間
DRAIN_LOG=$(journalctl -u "$UNIT" --since "-20 min" --no-pager 2>/dev/null \
  | grep -F "parallax.lifespan: drain " || true)

if [ -z "$DRAIN_LOG" ]; then
  # fail closed：查無證據 = 無法確認，絕不報 clean
  echo "❓ 無法確認 drain 結果：時間窗內找不到 'parallax.lifespan: drain ' log 行。"
  echo "   可能是 log 收集沒到位、--since 窗口不對，或進程被 SIGKILL 沒來得及寫。"
  echo "   請擴大時間窗 / 換 log 來源後重查；**不要**當成 clean drain。"
  exit 1
fi

if printf '%s\n' "$DRAIN_LOG" | grep -qF "drain timeout after"; then
  echo "❌ drain 被 900s timeout 切斷（in-flight 被硬斷）— 進入步驟 3 的事件記錄"
  printf '%s\n' "$DRAIN_LOG"
  exit 1
fi

if printf '%s\n' "$DRAIN_LOG" | grep -qF "drain complete"; then
  echo "✅ drain 自然完成"
  printf '%s\n' "$DRAIN_LOG"
  exit 0
fi

echo "❓ 找到 drain log 但格式不符預期，無法判定（fail closed）"; printf '%s\n' "$DRAIN_LOG"; exit 1
```

> 為什麼不用 `increase(parallax_drain_timeout_total[5m])`：那個 counter 在
> 生產環境**永遠掃不到**（限制 2），所以用它判斷等於「一定回報沒 timeout」。
> r1 之前這裡就是那樣寫的，會在真的 timeout 時回報 clean。

### 步驟 3：超時 → 強制終止 + 事件記錄

```bash
UNIT=parallax-dual-read

# 強制終止（OLD_PID 沿用步驟 2；沒有的話用 systemctl kill）
kill -9 "$OLD_PID"
# 或
systemctl kill -s KILL "$UNIT"

# 寫入事件記錄（供事後 postmortem）
#
# ⚠️ 這裡刻意「不」記 inflight 讀數。步驟 2 舊版寫的 inflight_at_timeout 來自
# SIGTERM 後的 PromQL 查詢，而那個值必定是 stale 的 SIGTERM 前樣本（限制 1），
# 記進 postmortem 只會誤導。要知道被硬斷時還剩多少，看 drain timeout 那行
# WARNING log —— 它印的是進程自己數的 final_count，是唯一真值。
DRAIN_LOG_LINE=$(journalctl -u "$UNIT" --since "-20 min" --no-pager 2>/dev/null \
  | grep -F "parallax.lifespan: drain timeout after" | tail -1)

cat >> /var/log/parallax/drain-events.jsonl <<EOF
{"ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","event":"drain_timeout","unit":"$UNIT","old_pid":"$OLD_PID","drain_log":"${DRAIN_LOG_LINE:-NOT_FOUND}","action":"SIGKILL","operator":"$USER"}
EOF
```

---

## 異常處理：Drain Timeout / Inflight 卡住不降

### 狀態：inflight 長時間不降（> 60s）

**可能原因與處置**：

| 原因 | 診斷 | 處置 |
|------|------|------|
| 請求 hang（外部服務無回應） | **SIGTERM 前**觀察 `parallax_inflight_requests` 是否長時間不降（SIGTERM 後這個 gauge 掃不到，見「限制 1」）。**不要**指望 `parallax_drain_timeout_total`——見「限制 2」，該 counter 在生產環境觀測不到；改抓 log 行 `parallax.lifespan: drain timeout after`（步驟 2b） | 設定上游 timeout（建議 30s），或強制 kill |
| Gauge 計數 bug（inc/dec 不配對） | 查 code review，grep `inflight.inc` vs `inflight.dec` | Hotfix：確保所有 exit path 都有 dec |
| 連線池洩漏 | `ss -tnp` 檢查 ESTABLISHED 連線數 | 重啟進程 + 排查 connection leak |
| 死鎖 | `kill -SIGQUIT <pid>` 取 thread dump | 分析 thread dump，修復後 redeploy |

### 緊急繞過（強制終止）

若 drain 持續卡住且影響 deploy pipeline，且**已確認 drop in-flight dual-read 請求是可接受代價**：

```bash
# 強制 SIGKILL（殺掉所有 in-flight；僅限緊急）
systemctl kill -s KILL parallax-dual-read
# 或
kill -9 <pid>
```

> ⚠️ `lifespan.py` 沒有讀任何「強制縮短 drain」的環境變數（`DRAIN_TIMEOUT_SECONDS` 是 `Final[float]`）；唯一的逃生口就是 SIGKILL。執行前請於 `#parallax-sre` 公告影響範圍 + 寫入事件記錄。

---

## 驗證 Drain 成功：Post-Deploy Probes

Drain 完成後，執行以下驗證（每一步都有 explicit failure path，不要依賴 `grep -q ... && echo` 的 silent-fail 模式）：

```bash
# 1. 新實例 inflight 健康確認
#
#    ⚠️ 這一步驗的是「新實例正常」，不是「舊實例 drain 完了」——後者看步驟 2。
#    舊實例的 series 在它退出後還會 stale 殘留 ~5 min（限制 1），所以必須
#    用 instance 標籤鎖定新實例，否則 .data.result[0] 可能撈到舊的那條。
NEW_INSTANCE="${NEW_INSTANCE:?請先設成新實例的 instance 標籤值，例如 10.0.0.7:8765}"
INFLIGHT=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode "query=parallax_inflight_requests{job=\"m3-dual-read\",instance=\"$NEW_INSTANCE\"}" \
  | jq -r '.data.result[0].value[1] // "missing"')
if [ "$INFLIGHT" = "missing" ]; then
  echo "❌ 新實例 $NEW_INSTANCE 沒有 inflight series —— 可能還沒被 scrape 或 job/instance 標籤不對"; exit 1
fi
if awk -v v="$INFLIGHT" 'BEGIN { exit !(v+0 >= 0 && v+0 < 1000) }'; then
  echo "✅ 新實例 inflight = $INFLIGHT（idle 應為 0；/metrics 與 /healthz 不計入）"
else
  echo "❌ 新實例 inflight = $INFLIGHT 異常"; exit 1
fi

# 2. 舊實例 drain 有沒有被 900s 切斷 —— 看 log，不看 counter
#
#    ⚠️ 舊版這裡用 increase(parallax_drain_timeout_total[10m]) 且把查無結果
#    用 // "0" 湊成 0。那個 counter 在生產環境永遠掃不到（限制 2），所以舊版
#    在「真的 timeout」時會印 ✅ 過去 10 min 無 drain timeout —— 正好報反。
#    改用 durable log 訊號，查無證據時 fail closed。
UNIT=parallax-dual-read
DRAIN_LOG=$(journalctl -u "$UNIT" --since "-20 min" --no-pager 2>/dev/null \
  | grep -F "parallax.lifespan: drain " || true)

if [ -z "$DRAIN_LOG" ]; then
  echo "❓ 無法確認 drain 結果：時間窗內查無 'parallax.lifespan: drain ' log 行。"
  echo "   這是「不知道」，不是「沒問題」——請擴大時間窗或換 log 來源後重查。"; exit 1
elif printf '%s\n' "$DRAIN_LOG" | grep -qF "drain timeout after"; then
  echo "❌ 舊實例 drain 被 900s timeout 切斷（in-flight 被硬斷）"
  printf '%s\n' "$DRAIN_LOG"; exit 1
elif printf '%s\n' "$DRAIN_LOG" | grep -qF "drain complete"; then
  echo "✅ 舊實例 drain 自然完成"
else
  echo "❓ drain log 格式不符預期，無法判定（fail closed）"; printf '%s\n' "$DRAIN_LOG"; exit 1
fi

# 3. 新版本 readiness
if curl -sf http://localhost:8080/healthz >/dev/null; then
  echo "✅ /healthz OK"
else
  echo "❌ /healthz 非 200"; exit 1
fi

# ⚠️ 2026-08-02 起，下面兩個 gauge 依 traffic_source 分 label
# （natural / synthetic / unknown）。bare metric name 會回**多條 series**，
# `.data.result[0]` 取到哪一條不保證，可能把 synthetic 的讀數當成整體 SLO。
# 每條查詢都要顯式指定 partition，並用 max() 收成單一 series。
#   natural   = DoD 與 alert 的語意（門檻對它判）
#   synthetic = M4 burn-in loader；自然流量出現前，只有它有讀數
# 某個 partition 沒資料時回 "missing"（誠實的「沒量到」，不是 0）。

# 4. Dual-read 寫入錯誤率（用真實 gauge，不是不存在的 errors_total counter）
WRITE_ERR_NATURAL=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=max(parallax_dual_read_write_error_rate{traffic_source="natural"})' \
  | jq -r '.data.result[0].value[1] // "missing"')
WRITE_ERR_SYNTHETIC=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=max(parallax_dual_read_write_error_rate{traffic_source="synthetic"})' \
  | jq -r '.data.result[0].value[1] // "missing"')
echo "dual_read_write_error_rate natural=$WRITE_ERR_NATURAL synthetic=$WRITE_ERR_SYNTHETIC（DoD ≤ 0.0005 對 natural 判；> 0.0005 → 升 P1）"

# 5. Discrepancy 抽樣（72h 滾動平均，確認 deploy 未引入 drift）
DISCREPANCY_NATURAL=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=max(parallax_dual_read_discrepancy_rate{traffic_source="natural"})' \
  | jq -r '.data.result[0].value[1] // "missing"')
DISCREPANCY_SYNTHETIC=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=max(parallax_dual_read_discrepancy_rate{traffic_source="synthetic"})' \
  | jq -r '.data.result[0].value[1] // "missing"')
echo "dual_read_discrepancy_rate natural=$DISCREPANCY_NATURAL synthetic=$DISCREPANCY_SYNTHETIC（DoD ≤ 0.001 對 natural 判 / 30 min → DualReadDiscrepancyRateHigh warning）"
```

---

## 回滾：Drain 未完成即部署的應變

若新版本在舊版本 drain 完成前就已部署（pipeline 誤判或手動操作失誤）：

1. **立即暫停 pipeline**：
   ```bash
   kubectl rollout pause deployment/m3-dual-read -n parallax
   ```

2. **回退到舊版本**：
   ```bash
   kubectl rollout undo deployment/m3-dual-read -n parallax
   # 或指定 revision
   kubectl rollout undo deployment/m3-dual-read -n parallax --to-revision=<N>
   ```

3. **確認回退後 inflight 正常**（看的是**回退後仍在服務**的實例，這些是活的，PromQL 有效）：
   ```bash
   watch -n 2 'curl -s http://localhost:9090/api/v1/query \
     --data-urlencode "query=parallax_inflight_requests" | jq ".data.result"'
   ```
   > ⚠️ 結果裡可能混著**已終止實例的 stale series**（限制 1，殘留 ~5 min）。
   > 判讀時以 `metric.instance` 對照當前活著的實例，別把 stale 正值當成「還有請求卡著」。

4. **事件記錄**：
   ```json
   {"ts":"...","event":"rollback_drain_incomplete","reason":"drain_not_finished_before_deploy","action":"rollout_undo"}
   ```

> **預防措施**：在 CI/CD pipeline 中加入 drain gate——但 gate 的判準**必須是 lifecycle 訊號（舊進程是否退出），不是 PromQL**。
>
> 舊版這裡寫的是「deploy job 等待 `parallax_inflight_requests == 0`」，那個 gate **不可能通過**：SIGTERM 後 socket 先關，Prometheus 讀到的是 stale 正值然後 empty，兩者都不是 0（限制 1）。實作請直接用步驟 2 的迴圈（`systemctl show -p MainPID` + `kill -0` 等舊 PID 消失；k8s 用 `kubectl wait --for=delete pod/<old-pod>`），observe timeout 960s（對齊 server-side `DRAIN_TIMEOUT_SECONDS=900` + buffer），並在 gate 後接步驟 2b 的 log 檢查確認不是被硬斷的。
>
> gate 取不到 lifecycle 訊號時**一律 fail closed**（當作未完成），不要因為查不到就放行。

---

## 與 systemd / pm2 整合

### systemd 建議

```ini
# /etc/systemd/system/parallax-dual-read.service
[Service]
Type=notify
ExecStart=/usr/local/bin/parallax-dual-read
KillSignal=SIGTERM

# 給 server-side drain 的寬限時間（DRAIN_TIMEOUT_SECONDS=900s + 60s buffer）
# 太小會在 server-side drain 完成前 SIGKILL，等同殺掉本來會 drain 完的 in-flight
TimeoutStopSec=960
# Reload 時也給足夠時間
TimeoutStartSec=30

# 確保 SIGTERM 後有足夠時間 drain
SendSIGKILL=yes
KillMode=mixed
```

| 參數 | 建議值 | 說明 |
|------|--------|------|
| `TimeoutStopSec` | **960s** | 對齊 `lifespan.py::DRAIN_TIMEOUT_SECONDS=900` + 60s buffer；< 900 會吃掉 server-side drain |
| `TimeoutStartSec` | **30s** | 啟動超時 |
| `KillSignal` | **SIGTERM** | 先 graceful，server 內部 drain 最長 900s，再 SIGKILL |
| `KillMode` | **mixed** | 先 SIGTERM 主進程，超時後 SIGKILL 全部 |

### pm2 建議

```javascript
// ecosystem.config.js
module.exports = {
  apps: [{
    name: 'parallax-dual-read',
    script: './dist/index.js',
    kill_timeout: 10000,   // 10s（pm2 預設 SIGKILL 前等待）
    listen_timeout: 10000,
    shutdown_with_message: true,  // 支持 graceful shutdown via IPC
    // 注意：pm2 的 kill_timeout 上限較低，
    // server-side drain 上限 900s（lifespan.py），pm2 撐不到，建議改用 systemd
  }]
};
```

> **⚠️ 注意**：pm2 的 `kill_timeout` 最大實務值約 15-30s，撐不住 `lifespan.py` 的 900s server-side drain。**強烈建議使用 systemd**，將 `TimeoutStopSec` 設為 960s（900s + 60s buffer）。

---

## 附錄：快速決策樹

```
Deploy / Restart 觸發
  │
  ├─ [步驟 1] SIGTERM 前：PromQL 看 parallax_inflight_requests（平衡檢查）
  │            ⚠️ 這是 PromQL 唯一有效的時機
  │
  ├─ SIGTERM 發送 → 舊版本停止接受新請求
  │            ⚠️ 從這一刻起 Prometheus 掃不到該進程（socket 已關）
  │            ⚠️ inflight 的下降過程與 drain_timeout counter 都看不到
  │
  ├─ [步驟 2] 等 lifecycle 訊號：舊 MainPID 是否退出（systemd / k8s / pm2）
  │     │      取不到訊號 → ❓ fail closed，當作未完成
  │     │
  │     ├─ ≤ 960s 舊進程退出 → 進入步驟 2b（退出 ≠ 乾淨）
  │     │
  │     └─ 逾時仍未退出 / unit failed → ❌ 進入步驟 3
  │
  ├─ [步驟 2b] journalctl 抓 "parallax.lifespan: drain " log 行
  │     │
  │     ├─ "drain complete"      → ✅ drain 自然完成
  │     ├─ "drain timeout after" → ❌ 被 900s 切斷 → 記錄事件 + postmortem
  │     └─ 查無 log              → ❓ 無法確認（fail closed，不可當 clean）
  │
  ├─ [步驟 3] SIGKILL 強制終止 + 寫事件記錄（不記 stale inflight 讀數）
  │
  └─ Post-deploy probes → 確認**新**實例健康（instance 標籤鎖定，避開 stale series）
```

---

*本文件由 Parallax SRE 維護。如有疑問，於 #parallax-sre 頻道聯繫 oncall。*
