# M4 Canary Shadow Observer — Stage Runbook

> **文件版本**: v2.0 (rewrite — grounded in the real ZenBook mechanism)
> **最後更新**: 2026-06-07
> **擁有者**: Chris (single-operator homelab)
> **狀態**: Draft → Review by Chris
> **權威來源**: [canary-shadow-spec.md](./canary-shadow-spec.md) (frozen-2026-05-18) · `parallax/canary_shadow.py` · `parallax/canary/` CLI

---

## 0. ⚠️ 讀這份前必看 — 兩個未 reconcile 的問題（別照舊操作）

v1.0 的舊 runbook 是**通用企業 canary 範本，與本系統不符**（`canary-ctl.parallax.internal` 主機、`canary-deploy.sh`、image registry、load-balancer 流量切分、PagerDuty/Slack、Kernel/Aphelion 團隊 —— 這些**都不存在**）。本 v2.0 改寫對齊真實機制，但有兩點**刻意留給 Chris 決，未自行填補**：

1. **observer-only（§9）vs rollback 演練的張力。** spec §9 明寫此 observer **永不改變 served result**（client 永遠拿 `result.primary`，無真流量切分）。但 `parallax canary` CLI 卻有 `--rollback-drill` / `--drain-test` / `--orbit-reemit-test`（drain in-flight + Orbit re-emit + idempotency）。若 observer 不碰 served path，這些 drill「rollback」的到底是什麼？**推測**：它們屬於未來 **M5 Aphelion 真 cutover**，不是這個觀察層。**在 Chris 確認前，第 6 節 rollback 只當「關掉 observer」（`SHADOW_FRACTION=0.0`），對 served path 零風險。**
2. **告警通道。** CLI `--check-alerting` 寫「PagerDuty + Slack」，但本 homelab 真實告警是 **Discord relay (PR #60, LIVE) + Gmail (PR #62)**。PagerDuty/Slack 疑為 CLI 內的 placeholder。**本 runbook 一律寫 Discord/Gmail；CLI 的 PagerDuty/Slack 字樣待 Chris 確認是否為佔位。**

---

## 1. 這是什麼（reframe — 觀察，不是流量切分）

M4 canary = **shadow observer**：`parallax/canary_shadow.py` 對「已完成的 `DualReadRouter` 結果」抽樣一個比例，灌進**帶 `stage` label 的 Prometheus counter**，讓 alertmanager 能針對「當前 canary 階段的觀察子集」告警，而非全域 dual-read 率。

- **不切流量**：每個 request 仍由 `DualReadRouter` 同時跑 primary(`RealMemoryRouter`) + secondary(`AphelionReadAdapter`)，**client 永遠收到 `result.primary`**。推進 stage = **觀察更多**，不是把更多真流量導去新路徑。
- **stage 旋鈕** = `PARALLAX_CANARY_SHADOW_FRACTION` 環境變數（per-request `random.random() < fraction` 抽樣）。
- **主機**：ZenBook `192.168.1.111`，single-user。`parallax-server` = **system service**（`/etc/systemd/system/parallax-server.service`，需 `sudo`）。

## 2. Stage 對應表（spec §3）

| Stage | `PARALLAX_CANARY_SHADOW_FRACTION` | M4 GATE | 觀察期 | DoD CLI stage 名 |
|---|---|---|---|---|
| `disabled` | `0.0`（precise） | observer off | — | — |
| `s1` | `(0.0, 0.01]` | GATE 3 entry | 入場觀察 | `m4_1pct` |
| `s2` | `(0.01, 0.10]` | GATE 4 | 24h | `m4_10pct` |
| `s3` | `(0.10, 0.50]` | GATE 5 | 48h | `m4_50pct` |
| `s4` | `(0.50, 1.00]` | GATE 6/7 | 14-day final window（`1.0` = 全量抽樣） | `m4_100pct` |

上界 inclusive，讓用整數百分比的人剛好落在預期 stage。

## 3. Stage 0 — 首次部署（**目前尚未部署**）

> 2026-06-07 實查 ZenBook：`/etc/parallax/canary.env` 不存在、`parallax-server.service.d/` 只有 `hardening.conf` 無 `canary.conf` → observer **尚未上線**。推進任何 stage 前先做本節。前置：M5 burn-in DoD 已於 2026-05-28 accepted（解鎖推進）。

```bash
# 1. 建 canary.env（observer 先關）
sudo install -m 600 -o chris -g chris /dev/null /etc/parallax/canary.env
echo 'PARALLAX_CANARY_SHADOW_FRACTION=0.0' | sudo tee /etc/parallax/canary.env

# 2. system service drop-in（與既有 hardening.conf 並存）
sudo tee /etc/systemd/system/parallax-server.service.d/canary.conf >/dev/null <<'EOF'
[Service]
EnvironmentFile=/etc/parallax/canary.env
EOF
# ⚠️ hardening.conf 有 ProtectHome=read-only + ReadWritePaths 白名單（見 reference_zenbook_systemd_hardening）。
#    EnvironmentFile 讀的是 /etc/parallax（非 home），不需加白名單；但 restart 後確認 server active 不 crash-loop。

# 3. reload + restart（system service）
sudo systemctl daemon-reload
sudo systemctl restart parallax-server.service
systemctl status parallax-server.service --no-pager | head -3   # 確認 active

# 4. 把 recording rules + dashboard 進 observability stack
cp prometheus/rules/parallax-m4-canary.rules.yml \
   /home/chris/parallax-kernel/deploy/observability/prometheus/rules/
docker compose -f /home/chris/parallax-kernel/deploy/observability/docker-compose.yml \
   exec prometheus promtool check rules /etc/prometheus/rules/parallax-m4-canary.rules.yml
docker compose -f /home/chris/parallax-kernel/deploy/observability/docker-compose.yml kill -s HUP prometheus
# Grafana：import grafana/dashboards/parallax-m4-canary-stage-1.json
```

**Stage 0 驗收**：`parallax-server` active；`SHADOW_FRACTION=0.0`（observer off）；Grafana 出現 `parallax_m4_canary` series（idle stage 因 `>0` guard 不出 NaN）。

## 4. Stage 推進（s1 → s4，逐階）

每階只是改一個值 + 重啟（≤10s downtime，落在 `m4-m5-readiness-spec.md` 要求的 30s rollback window 內）：

```bash
# 範例：推進到 s1 (1%)。後續階改 0.10 / 0.50 / 1.00。
sudo sed -i 's/^PARALLAX_CANARY_SHADOW_FRACTION=.*/PARALLAX_CANARY_SHADOW_FRACTION=0.01/' /etc/parallax/canary.env
sudo systemctl restart parallax-server.service
```

1. 確認 Grafana 上對應 stage 的 series 開始有資料。
2. Discord `#指揮室` relay（PR #60）發推進通知。
3. **觀察期 dwell**：s1 入場 → s2 24h → s3 48h → s4 14-day。
4. dwell 滿後跑 **DoD**（spec §5 metrics + 7-day 窗）：
   ```bash
   PARALLAX_SPLIT_IMPLEMENTED=1 /home/chris/parallax/.venv/bin/parallax canary --dod --stage m4_10pct
   #   → PASS / INSUFFICIENT_DATA（樣本不足，續等，不算失敗）/ FAIL
   ```
5. **Gate 告警**（block 推進，非 auto-rollback）：`CanaryShadowDiscrepancyHigh` / `CanaryShadowAphelionUnreachableHigh`（rate > 0.5%，持續 10m，severity=warning，class=gate）。任一 firing → 不推進，查 divergence 來源。
6. **取得 Chris Go/No-Go ACK** 才推下一階。

## 5. 監控指標（spec §5）

| Metric | Type | Labels |
|---|---|---|
| `parallax_canary_shadow_attempts_total` | Counter | stage, user_id, traffic_source |
| `parallax_canary_shadow_outcomes_total` | Counter | stage, outcome, user_id, traffic_source |
| `parallax_canary_shadow_discrepancy_rate` | Recorded gauge | stage, traffic_source |
| `parallax_canary_shadow_aphelion_unreachable_rate` | Recorded gauge | stage, traffic_source |

Recording rules：`prometheus/rules/parallax-m4-canary.rules.yml`（group `parallax_m4_canary`），denominator `>0` guard。`outcome="skipped"` 不計入（dual-read 沒發生，不是 canary 觀察）。

## 6. Rollback（observer 層 — 簡單、零 served-path 風險）

因 observer **永不改變 served result（§9）**，「rollback」對這個觀察層就是**關掉它**：

```bash
sudo sed -i 's/^PARALLAX_CANARY_SHADOW_FRACTION=.*/PARALLAX_CANARY_SHADOW_FRACTION=0.0/' /etc/parallax/canary.env
sudo systemctl restart parallax-server.service
```

- ≤10s downtime；client 服務路徑全程不受影響（本來就只拿 `result.primary`）。
- 確認 Grafana 上 canary series 歸零。

> ⚠️ **不要**把 `parallax canary --rollback-drill` / `--drain-test` / `--orbit-reemit-test` 當成這個 observer 的 rollback 來跑 —— 見第 0 節問題①。那些 drill 疑屬未來 M5 Aphelion cutover，未經 Chris reconcile 前不在本 observer 的回滾流程內。

## 7. Escalation（single-operator）

本系統是 single-user homelab，無 oncall 團隊。異常路徑：

1. **Gate 告警 firing** → Discord relay 自動通知 Chris（PR #60）；查 `parallax_canary_shadow_discrepancy_rate` 來源（Aphelion read 與 primary 分歧）。
2. **server restart 後 crash-loop** → 多半是 systemd hardening 沙箱（`ReadWritePaths`/`ProtectHome`，見 reference_zenbook_systemd_hardening）；查 `journalctl -u parallax-server -n 50`。
3. **判斷不了** → 就是 Chris 自己。沒有 Kernel/Aphelion 團隊（那是 v1.0 範本虛構）。

## 8. M4 GA

s4 `1.0` 跑滿 14-day 觀察窗、DoD PASS、gate 告警全程未 firing、Chris 最終 ACK `M4 L3 Canary @100% PASS → GA`。M5 GA = M4 canary @100% 後自然 active（不需 deploy M5 binary、不需 flip flag，per `project_messier_v4_v5_progress`）。

---

*Parallax homelab 內部操作手冊。v2.0 把 v1.0 的通用企業範本改寫為真實 ZenBook / `SHADOW_FRACTION` / system-service 機制；第 0 節兩個 reconcile 問題待 Chris 決後再定稿。*
