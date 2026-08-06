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

核心原則：**新請求路由到新版本，舊版本只完成 in-flight 請求後收尾**。Drain 由 `parallax/server/lifespan.py` 內建的 asyncio 自管 drain loop 處理，oncall 透過 `parallax_inflight_requests` gauge 觀察進度。

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
4. **drain loop**：每 0.5s 讀取 `get_inflight_count()`，若 ≤ 0 立即 return（log INFO `drain complete in {elapsed}s`）；若 deadline 到（900s），增 `parallax_drain_timeout_total` counter + log WARNING + return（讓 process 收尾）。
5. **觀察方式**：oncall **僅作觀察**（透過 `parallax_inflight_requests` gauge + 下方 ⚠️ 指定的 log 行），**勿在 drain 自然完成前強制 SIGKILL**（會吃掉本來會 drain 完的 in-flight）。

> ⚠️ **`parallax_drain_timeout_total` 在生產環境觀測不到（#102 review 實測，uvicorn 0.52.1）**
>
> 該 counter 在 lifespan shutdown 期間、`_drain_inflight()` 尾端才 `inc()`，
> 而**那個時間點 listening socket 已經關閉**：從 shutdown 視窗內發出的 3 次
> scrape 全部 ConnectTimeout，連裸 TCP connect 都 timeout。所以 Prometheus
> 拿到的最後一個樣本永遠是 increment 前的 0，`increase()` 看不到跳變，
> `DrainTimeoutDetected` alert **在真的 drain timeout 時也不會響**。
>
> 把 increment 提早到 timeout 偵測當下**也不能解**——整個 drain 都在同一個
> 「已不服務」的視窗內；由「即將終止的 exporter 自己」持有的 counter 無法
> 回報自己的終止。要修需要 durable/external 路徑（audit-db 落一筆，或對
> log 行做 log-based alert），屬 producer 工作。
>
> 另有實測發現：**uvicorn 會先自己等 in-flight 請求結束才進 lifespan
> shutdown**（SIGTERM 當下仍在跑的請求正常回 200，隨後 `_drain_inflight`
> 看到的 inflight 已是 0），所以 timeout 分支在 uvicorn 下幾乎不可達。
>
> **oncall 實務**：判斷 drain 有沒有 timeout，一律抓 log 行
> `parallax.lifespan: drain timeout after`。**alert 沒響不代表 drain 乾淨。**

> **設計重點**：`_drain_inflight` 用 `asyncio.sleep`（不是 `time.sleep`），所以 drain loop 跟其他 coroutine（包含正在 drain 的 in-flight 請求）可以並行進度。

---

## 觀察指標：parallax_inflight_requests 何時降到 0

> **Gauge 語義（#102 起 gauge 才真的上線，2026-08-06）**：
> `parallax_inflight_requests` 只計「drain 必須等的應用請求」。
> `/metrics` 與 `/healthz` **不計入**（`INFLIGHT_EXCLUDED_PATHS`，
> `parallax/server/middleware/dual_read_snapshot.py`）。
> 這是必要的：gauge 是在「服務該次 scrape 的當下」被讀取的，若把 `/metrics`
> 算進去，每次 scrape 都至少回報 1，**idle 實例永遠不可能回報 0**——本文
> 下面所有 `> 0`（挑出卡住的實例）與 `sum(...) == 0`（deploy gate）的查詢
> 就會把每台健康實例判成卡住，deploy gate 每次都空跑滿 960s。
> 因此本文的 `== 0` 判準在修正後才成立；`/healthz` 一併排除，否則同一個
> gate 會變成間歇性 flaky（更難查）。

| 條件 | 預期 drain 時間 |
|------|----------------|
| 正常流量（p99 latency < 500ms） | **< 10 秒** |
| 高延遲查詢（p99 ~ 2s） | **< 15 秒** |
| 含外部 IO 回調（DB / RPC） | **< 30 秒** |
| 異常：inflight 卡住 | **> 60 秒 → 進入異常處理** |

**觀察方式**：

```bash
# 即時觀察
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=parallax_inflight_requests{job="m3-dual-read"}' | jq

# Grafana 面板
# Dashboard: M3 Dual-Read → Panel: Inflight Requests
```

---

## Oncall 動作

### 步驟 1：觀察 Inflight Gauge

```bash
# 確認目標實例
INSTANCES=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=parallax_inflight_requests{job="m3-dual-read"} > 0' | jq -r '.data.result[].metric.instance')

echo "尚有 inflight 的實例：$INSTANCES"
```

若所有實例 inflight = 0，drain 已完成，無需後續動作。

### 步驟 2：觀察 Drain 進度（外部觀察，最長 ~15 min = 900s）

> **重點**：drain 由 `parallax_lifespan` 在進程內自動跑（最長 `DRAIN_TIMEOUT_SECONDS=900`）。oncall 的角色是**外部觀察 + 異常時介入**，**不是**外部超時關閉。下方 loop 的 `OBSERVE_TIMEOUT` 必須 ≥ 900s（容納 server-side drain），給 buffer 取 960s。

```bash
OBSERVE_TIMEOUT=960   # 900s server drain + 60s buffer
ELAPSED=0

while [ $ELAPSED -lt $OBSERVE_TIMEOUT ]; do
  COUNT=$(curl -s http://localhost:9090/api/v1/query \
    --data-urlencode 'query=sum(parallax_inflight_requests{job="m3-dual-read"})' \
    | jq '.data.result[0].value[1]' | tr -d '"')

  if [ "$COUNT" = "0" ] || [ "$COUNT" = "NaN" ]; then
    echo "✅ Drain 完成，耗時 ${ELAPSED}s（server-side lifespan 自然收尾）"
    exit 0
  fi

  # 同時看 drain timeout counter；若 server 自己已 timeout，oncall 要介入
  TIMEOUTS=$(curl -s http://localhost:9090/api/v1/query \
    --data-urlencode 'query=increase(parallax_drain_timeout_total[5m])' \
    | jq -r '.data.result[0].value[1] // "0"')
  if awk -v v="$TIMEOUTS" 'BEGIN { exit !(v+0 > 0) }'; then
    echo "❌ parallax_drain_timeout_total 增 $TIMEOUTS — server-side drain 已 hit 900s timeout，進入步驟 3"
    exit 1
  fi

  echo "⏳ Inflight: $COUNT，已觀察 ${ELAPSED}s（server-side drain 上限 900s）..."
  sleep 10
  ELAPSED=$((ELAPSED + 10))
done

echo "❌ 外部觀察超時（${OBSERVE_TIMEOUT}s）— 進入步驟 3"
```

### 步驟 3：超時 → 強制終止 + 事件記錄

```bash
# 強制終止
kill -9 <pid>
# 或
systemctl kill -s KILL parallax-dual-read

# 寫入事件記錄（供事後 postmortem）
cat >> /var/log/parallax/drain-events.jsonl <<EOF
{"ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","event":"drain_timeout","instance":"$INSTANCE","inflight_at_timeout":$COUNT,"action":"SIGKILL","operator":"$USER"}
EOF
```

---

## 異常處理：Drain Timeout / Inflight 卡住不降

### 狀態：inflight 長時間不降（> 60s）

**可能原因與處置**：

| 原因 | 診斷 | 處置 |
|------|------|------|
| 請求 hang（外部服務無回應） | 觀察 `parallax_inflight_requests` 是否長時間不降。**不要**指望 `parallax_drain_timeout_total`——見下方 ⚠️，該 counter 在生產環境觀測不到；改抓 log 行 `parallax.lifespan: drain timeout after` | 設定上游 timeout（建議 30s），或強制 kill |
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
# 1. Inflight 歸零確認
INFLIGHT=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=parallax_inflight_requests{job="m3-dual-read"}' \
  | jq -r '.data.result[0].value[1] // "missing"')
if [ "$INFLIGHT" = "0" ]; then
  echo "✅ Inflight = 0"
else
  echo "❌ Inflight = $INFLIGHT（非 0 或 metric 缺失）"; exit 1
fi

# 2. Drain timeout 沒被觸發
DRAIN_TIMEOUTS=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=increase(parallax_drain_timeout_total[10m])' \
  | jq -r '.data.result[0].value[1] // "0"')
if awk -v v="$DRAIN_TIMEOUTS" 'BEGIN { exit !(v+0 == 0) }'; then
  echo "✅ 過去 10 min 無 drain timeout"
else
  echo "❌ parallax_drain_timeout_total 增 $DRAIN_TIMEOUTS（drain 提前被切斷）"; exit 1
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

3. **確認回退後 inflight 正常**：
   ```bash
   watch -n 2 'curl -s http://localhost:9090/api/v1/query \
     --data-urlencode "query=parallax_inflight_requests" | jq ".data.result"'
   ```

4. **事件記錄**：
   ```json
   {"ts":"...","event":"rollback_drain_incomplete","reason":"drain_not_finished_before_deploy","action":"rollout_undo"}
   ```

> **預防措施**：在 CI/CD pipeline 中加入 drain gate——deploy job 等待 `parallax_inflight_requests == 0` 或外部 observe timeout 960s（對齊 server-side `DRAIN_TIMEOUT_SECONDS=900` + buffer）後才 proceed。

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
  ├─ SIGTERM 發送 → 舊版本停止接受新請求
  │
  ├─ 觀察 parallax_inflight_requests + parallax_drain_timeout_total
  │     │
  │     ├─ ≤ 900s 降到 0 → ✅ Server-side drain 成功
  │     │
  │     └─ parallax_drain_timeout_total 增加 → ❌ Server-side drain timeout
  │           │
  │           ├─ 記錄事件
  │           ├─ SIGKILL 強制終止
  │           └─ 觸發 postmortem（若為首次）
  │
  └─ Post-deploy probes → 確認新版本健康
```

---

*本文件由 Parallax SRE 維護。如有疑問，於 #parallax-sre 頻道聯繫 oncall。*
