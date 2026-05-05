# US-009 M4 Canary Acceptance Criteria

> **版本**：v1.0.0  
> **日期**：2026-05-03  
> **狀態**：Draft — pending codex agent 接線實作  
> **Plan ref**：見 §2 表格中的 `m4-entry-plan.md`（內部 plan，repo 外）  
> **Council vote**：2026-05-01，拆 3 PR（US-009.1 / US-009.2 / US-009.3）

---

## 1. 文件目的與受眾

本文件為 **codex agent 接 US-009.1、US-009.2、US-009.3 實作前必讀的 acceptance spec 基準**。所有 acceptance criteria 均為可測試（testable）、可驗證（verifiable）的硬性條件；codex agent 不得自行放寬或跳過任何條目。

**受眾**：codex agent（實作者）、reviewer（PR review 時對照）、council（stage gate 決策依據）。

**使用方式**：codex agent 在開始每個 PR 前，應逐條閱讀對應章節；PR description 中需引用本文件對應 criteria 編號，證明已滿足。

---

## 2. 前置依賴

| 依賴項 | 版本 / 狀態 | 說明 |
|---|---|---|
| M3 14-day corpus DoD | ✅ 已完成 | M3 所有 DoD 指標連續 14 天 PASS，corpus 資料已 freeze |
| Aphelion v0.5.x package format toolkit | v0.5.0 | **僅為 package format toolkit，zero retrieval semantics**。B1/B2/B3 全踩 fictional Protocol，不可當作 retrieval API 使用 |
| M4 entry plan | `m4-entry-plan.md` | xcouncil 2026-05-01 通過，為本 spec 的上游計畫文件 |

> ⚠️ **關鍵認知**：Aphelion v0.5.0 不提供任何 retrieval 語義。任何嘗試將 Aphelion 當作 retrieval API 的行為均為 out-of-scope，將在 M5 + Aphelion product spec ticket 中處理。

---

## 3. US-009.1 — Canary Infra Acceptance Criteria

### 3.1 Idempotency 機制

| # | Criteria | 驗證方式 |
|---|---|---|
| 1.1 | Idempotency key **僅使用 `event_id` UUID**（RFC 4122 v7），**不得**與 timestamp 做 hash 組合 | 單元測試：傳入相同 `event_id` + 不同 `timestamp`，確認判定為 duplicate |
| 1.2 | 相同 `event_id` 重複提交時，第二次及之後的請求必須回傳 **cached response**，不觸發任何 side-effect（不寫 DB、不發 event） | 單元測試：mock downstream，確認第二次呼叫次數為 0 |
| 1.3 | 不同 `event_id` 即使 payload 完全相同，也必須視為獨立事件並正常處理 | 單元測試：兩筆相同 payload、不同 UUID，確認各觸發一次處理 |
| 1.4 | Clock drift 場景：若系統時鐘回撥 > 5 min，idempotency 判定仍以 `event_id` 為唯一依據，不因 timestamp 變動而誤判 duplicate 或漏判 duplicate | 單元測試：注入 clock drift ±10 min，驗證行為不變 |

### 3.2 Audit Log

| # | Criteria | 驗證方式 |
|---|---|---|
| 1.5 | Audit log 使用**獨立 SQLite table**（不得與其他業務 table 共用 DB file） | 整合測試：確認 audit DB path 獨立，且與主 DB 無 foreign key 關聯 |
| 1.6 | Audit table schema 必須包含以下欄位（至少）：`event_id TEXT PK`, `request_at_iso TEXT NOT NULL`, `response_status INTEGER NOT NULL`, `latency_ms REAL`, `idempotency_hit BOOLEAN`, `created_at TEXT DEFAULT CURRENT_TIMESTAMP` | Schema migration 測試：`PRAGMA table_info(audit_log)` 欄位比對 |
| 1.7 | 每次請求（含 idempotency cache hit）都必須寫入 audit log，不得遺漏 | 單元測試：idempotency hit 場景下確認 audit log 有對應 row |
| 1.8 | Audit log 寫入失敗時，請求本身**不得**因此失敗（fire-and-forget with error logging） | 單元測試：mock SQLite write failure，確認請求仍正常回傳 |

### 3.3 Auto-Rollback Gates

> **結構：4 rollback triggers (T1-T4) + 1 min-hits gate (T5)**  
> T1-T4 為 trip-or-clear 語義的 rollback trigger；T5 為 sample-size **gate**（保護機制），啟動時將 T1-T4 的判定結果標記為 `insufficient_data`，自身**不 trip rollback、不 clear**。Gate vs trigger 的區分需在實作時保留，並與 sibling 文件 `stage-0-preflight-checklist.md` §4 L148「minimum 50 hits 保護」用語一致。

| # | Criteria | 驗證方式 |
|---|---|---|
| 1.9 | T1-T4 四個 rollback trigger **各自獨立計算 metric**，互不影響。T1-T4 任一 trigger 達到閾值即 trip rollback。**T5 gate 不在此獨立性條款範圍內**（gate 的跨 trigger 影響為設計語義，見 1.14） | 單元測試：逐一觸發 T1-T4 每個 trigger，確認其他 T1-T4 trigger 狀態不受影響 |

四個 rollback trigger + 一個 min-hits gate 定義如下：

| ID | 角色 | Metric | 閾值 | 視窗 | 動作 |
|---|---|---|---|---|---|
| `T1-error-rate` | trigger | 錯誤率（5xx / total） | ≥ 0.5% | 5 min sliding | trip rollback |
| `T2-discrepancy-rate` | trigger | 結果不一致率（mismatch / total） | ≥ 0.5% | 3 min sliding | trip rollback |
| `T3-p99-latency` | trigger | p99 latency | ≥ 100 ms | 5 min sliding | trip rollback |
| `T4-data-loss` | trigger | 資料遺失事件數 | > 0 | 累計（無視窗） | **immediate** trip rollback |
| `T5-min-hits-gate` | **gate** | 最低請求數 | < 50 hits | 5 min sliding | gate active 時，T1-T4 判定標記為 `insufficient_data`；gate 自身**不 trip、不 clear** |

| # | Criteria | 驗證方式 |
|---|---|---|
| 1.10 | `T1-error-rate`：在 5 min 視窗內 error rate 達到 0.5% 時 trip；低於 0.5% 時不 trip | 邊界測試：精準注入 0.49% 及 0.51% error，驗證 trip/no-trip |
| 1.11 | `T2-discrepancy-rate`：在 3 min 視窗內 discrepancy rate 達到 0.5% 時 trip；低於 0.5% 時不 trip | 邊界測試：同上，視窗為 3 min |
| 1.12 | `T3-p99-latency`：在 5 min 視窗內 p99 達到 100 ms 時 trip；99 ms 時不 trip | 邊界測試：注入 p99 = 99 ms 及 101 ms |
| 1.13 | `T4-data-loss`：任何單一資料遺失事件立即 trip，無視視窗大小 | 單元測試：注入 1 筆 data_loss event，確認 immediate trip |
| 1.14 | `T5-min-hits-gate`：當 5 min 視窗內 hits < 50 時 gate active，T1-T4 trigger 的判定結果均標記為 `insufficient_data`，T1-T4 不執行 trip 也不執行 clear。此跨 trigger 影響為 gate 的**設計語義**，屬 1.9 獨立性條款的明文例外 | 單元測試：注入 49 hits + error rate > 0.5%，確認 T1 標記為 `insufficient_data`（非 trip） |

### 3.4 Hysteresis 機制

| # | Criteria | 驗證方式 |
|---|---|---|
| 1.15 | Rollback trip 後，系統進入 `tripped` 狀態，**30 分鐘內不得 auto-recover**，即使所有 metric 已回到正常範圍 | 單元測試：trip 後模擬 metric 恢復正常，確認 30 min 內狀態仍為 `tripped` |
| 1.16 | 30 分鐘 cooldown 結束後，系統**不會自動 re-promote**；必須由人工執行 manual ACK 後才能 re-promote 至 `running` 狀態 | 單元測試：30 min 後確認狀態為 `awaiting_ack`，非 `running` |
| 1.17 | Manual ACK 必須記錄操作者身份（user ID 或 service account）及操作時間至 audit log | 整合測試：執行 ACK 後查 audit log 確認有 `ack_by` 及 `ack_at` 欄位 |

### 3.5 Test Coverage 要求

| # | Criteria | 驗證方式 |
|---|---|---|
| 1.18 | 測試數量最低門檻 **≥ 15 tests**：T1-T4 四個 trigger × pass/breach 邊界 × hysteresis（≥ 12 tests）+ T5 gate 三組覆蓋（hits<50 active / hits=50 邊界 / hits>>50 inactive，≥ 3 tests）= **≥ 15** | CI：`pytest tests/canary/ --collect-only -q \| tail -1` 確認 ≥ 15 |
| 1.19 | 每個 T1-T4 trigger 至少有 2 個邊界測試（剛好低於閾值 PASS + 剛好達到閾值 BREACH）；T5 gate 至少 1 組 hits=50 邊界測試 | 測試報告：逐條列出 trigger/gate ID + 對應 case |

---

## 4. US-009.2 — Aphelion Bridge Acceptance Criteria

### 4.1 Null-Stub 實作

> **與既有合約對齊（M3a baseline）**：`parallax/router/aphelion_stub.py::AphelionReadAdapter.query()` 已是 raise-only stub（`raise AphelionUnreachableError("not_implemented")`），由 `DualReadRouter` 捕捉並標記 `outcome="aphelion_unreachable"`。US-009.2 的工作是**保持**此契約穩定，**不得**改為 return-based 介面（會影響 §7 O.2 既有 M3 router code）。所有 anchor 一律以 symbol（`AphelionReadAdapter.query`）為準，避免 line-number drift。

| # | Criteria | 驗證方式 |
|---|---|---|
| 2.1 | `parallax/router/aphelion_stub.py::AphelionReadAdapter.query()` 必須以 `raise AphelionUnreachableError("not_implemented")` 結束，**不得**包含任何 real HTTP adapter 邏輯 | Code review：確認 `query()` body 為 single-statement raise，無 `httpx` / `requests` / `urllib` import |
| 2.2 | Stub 拋出的 `AphelionUnreachableError` 必須帶 `reason="not_implemented"`；既有 `DualReadRouter` 捕捉路徑（`outcome="aphelion_unreachable"`）必須維持綠燈，不得因 stub 改動而 break | 單元測試：`with pytest.raises(AphelionUnreachableError) as exc: adapter.query(req)` + assert `exc.value.reason == "not_implemented"`；整合測試 `pytest tests/router/ -k dual_read -v` 全 PASS |
| 2.3 | **不得**嘗試實作 real HTTP adapter。任何包含 `httpx.get`、`requests.post`、或等效 HTTP 呼叫的程式碼均為 violation | Lint rule / code review：grep `httpx\|requests\|urllib` 於 `aphelion_stub.py`，結果應為 0 matches |
| 2.4 | Real HTTP adapter 的實作留待 **M5 + Aphelion product spec ticket**，本 PR 不得預先建立 adapter skeleton 或 placeholder interface | Code review：確認無 `class AphelionAdapter` 或類似 forward-looking 抽象 |

### 4.2 Dual-Read 路由相容性

| # | Criteria | 驗證方式 |
|---|---|---|
| 2.5 | 既有 `dual_read` 路由在引入 stub 後**不得 break**。所有現有 `dual_read` 相關測試必須繼續 PASS | 整合測試：`pytest tests/router/ -k dual_read -v` 全數 PASS（涵蓋 `test_dual_read_router.py`、`test_dual_read_metrics.py`、`test_dual_read_decision_log.py`、`test_dual_read_result.py`、`test_dual_read_breaker_integration.py`、`test_is_dual_read_enabled.py`） |
| 2.6 | Stub 的引入不得改變 `dual_read` 路由的回傳型別（return type annotation 不變） | 型別檢查：`mypy tests/router/` 或 `pyright` 0 errors |

### 4.3 Test Coverage 要求

| # | Criteria | 驗證方式 |
|---|---|---|
| 2.7 | Stub return shape stability：連續呼叫 100 次，回傳物件的 schema（欄位名稱、型別）必須完全一致 | 單元測試：loop 100 次，assert schema 不變 |
| 2.8 | 跨 query type 行為一致：對 `QueryType` enum 全部 5 值（`recent_context` / `artifact_context` / `entity_profile` / `change_trace` / `temporal_context`），stub raise `AphelionUnreachableError` 後 `DualReadRouter` 標記的 `outcome` 均為 `'aphelion_unreachable'` | 參數化測試：`@pytest.mark.parametrize("query_type", list(QueryType))`，assert outcome 一致（已由 PR #39 / `tests/router/test_aphelion_stub.py::test_dual_read_outcome_aphelion_unreachable_for_every_query_type` 驗證） |

---

## 5. US-009.3 — DoD Scripts + Rollback Drill Harness Acceptance Criteria

### 5.1 DoD Scripts

| # | Criteria | 驗證方式 |
|---|---|---|
| 3.1 | DoD scripts 必須驗證 5 個 metric（error rate、discrepancy rate、p99 latency、data loss、min hits）**連續 7 天 PASS** | Script output：每個 metric 有 7-day rolling pass/fail 判定 |
| 3.2 | DoD scripts 必須支援四個 stage 各自獨立驗證：`M4@1%`、`M4@10%`、`M4@50%`、`M4@100%` | CLI 參數：`--stage m4_1pct` / `m4_10pct` / `m4_50pct` / `m4_100pct` |
| 3.3 | 每個 stage 的 DoD verification 結果必須輸出為結構化 JSON（含 `stage`, `metric`, `pass_count`, `fail_count`, `consecutive_pass_days`, `verdict`） | 整合測試：執行 script，parse JSON output，確認 schema 完整 |
| 3.4 | 若任一 metric 在 7 天內出現任何 FAIL，`verdict` 必須為 `FAIL`，且 `consecutive_pass_days` 歸零重新計算 | 單元測試：注入 Day 5 FAIL，確認 verdict 為 FAIL 且 counter 歸零 |
| 3.5 | DoD scripts 必須能獨立執行（不依賴 M4 router 服務運行），從 audit log SQLite 讀取資料 | 整合測試：僅提供 audit log DB file，確認 script 可正常執行 |

### 5.2 Rollback Drill Harness

| # | Criteria | 驗證方式 |
|---|---|---|
| 3.6 | Rollback drill 必須執行 **drain in-flight**：等待所有進行中請求完成後才執行 rollback，不得中斷進行中請求 | 整合測試：注入 10 筆 in-flight 請求，確認 drain 完成後才執行 rollback |
| 3.7 | Rollback drill **不得重放（replay）**已處理的事件。已處理的 `event_id` 不得被重新執行 | 單元測試：drill 前後比對 audit log，確認無重複 `event_id` |
| 3.8 | Rollback drill 依賴 **Orbit re-emit** 機制重新發送未確認事件，而非自行重新執行 | Code review：確認 drill harness 呼叫 Orbit re-emit API，不自行 loop 重送 |
| 3.9 | Orbit re-emit 的事件受 **idempotency 保護**：若 re-emit 的 `event_id` 已存在於 idempotency cache，則直接回傳 cached response，不重複處理 | 單元測試：re-emit 已知 `event_id`，確認 idempotency cache hit |
| 3.10 | Rollback drill 必須輸出 drill report（JSON），包含：`drill_id`, `started_at`, `drain_completed_at`, `rollback_executed_at`, `events_drained`, `events_re_emitted`, `events_idempotent_hit`, `verdict` | 整合測試：執行 drill，parse report JSON，確認所有欄位存在 |

### 5.3 Stage 推進

| # | Criteria | 驗證方式 |
|---|---|---|
| 3.11 | 四個 stage 推進順序為 `M4@1%` → `M4@10%` → `M4@50%` → `M4@100%`，不得跳級 | 整合測試：嘗試從 `1%` 直接推進至 `50%`，確認被拒絕 |
| 3.12 | 每個 stage 推進前，必須先完成該 stage 的 DoD verification 且 verdict 為 `PASS` | 整合測試：未完成 DoD 即嘗試推進，確認被拒絕 |
| 3.13 | Stage 推進狀態必須持久化（寫入 DB 或 config file），重啟後仍能正確恢復 | 整合測試：推進至 `M4@10%`，重啟服務，確認 stage 為 `M4@10%` |

### 5.4 Test Coverage 要求

| # | Criteria | 驗證方式 |
|---|---|---|
| 3.14 | DoD scripts 測試：4 stages × 5 metrics × pass/fail = **≥ 40 test cases**（可合併為參數化測試） | CI：`pytest tests/dod/ --collect-only -q \| tail -1` 確認 ≥ 40 |
| 3.15 | Rollback drill 測試：drain + re-emit + idempotency 三個面向各至少 2 個測試 = **≥ 6 tests** | CI：`pytest tests/drill/ --collect-only -q \| tail -1` 確認 ≥ 6 |

---

## 6. Cross-Cutting Acceptance Criteria

以下條件適用於 **US-009.1、US-009.2、US-009.3 全部三個 PR**：

| # | Criteria | 驗證方式 |
|---|---|---|
| X.1 | Lint clean：`ruff check .` 及 `ruff format --check .` 均 0 errors / 0 warnings | CI gate：lint step PASS |
| X.2 | `pytest tests/router/` 全數 PASS，0 failures | CI gate：router tests step PASS |
| X.3 | `pytest tests/cli/` 全數 PASS，0 failures | CI gate：cli tests step PASS |
| X.4 | 整體 test coverage 維持 **≥ 80%**（`coverage report --fail-under=80`） | CI gate：coverage step PASS |
| X.5 | 所有新增程式碼必須有 type hints（`mypy --strict` 或 `pyright` 0 errors on new files） | CI gate：type check step PASS |
| X.6 | PR description 必須引用本文件對應 criteria 編號（如 `AC-1.1 ✅`），證明已滿足 | Code review：人工檢查 PR description |

---

## 7. Out-of-Scope — Codex 不該動的東西

以下項目 **明確排除在 US-009 範圍之外**。若 codex agent 發現需要動到這些項目，應立即停止並回報主 session，由 council 決策。

| # | Item | 說明 |
|---|---|---|
| O.1 | **Aphelion repo 任何 file** | US-009 僅動 Parallax repo。Aphelion 的改動需獨立 ticket |
| O.2 | **既有 M3 router code** | Router 的改動需要 M5 ticket。US-009 不得修改 `tests/router/` 中既有測試的行為預期 |
| O.3 | **任何架構決策** | 如 idempotency store 選型、audit log retention policy、rollback strategy 變更等，均留主 session 決策。Codex agent 僅實作本 spec 已定義的方案 |
| O.4 | **Aphelion real HTTP adapter** | 留待 M5 + Aphelion product spec ticket。本 PR 僅有 null-stub |
| O.5 | **Production deployment scripts** | Stage 推進的 production 操作由 SRE 團隊執行，不在 codex agent 範圍內 |

---

## 8. 附錄：測試數量彙總

| PR | 最低測試數 | 說明 |
|---|---|---|
| US-009.1 | ≥ 15 | T1-T4 triggers × pass/breach × hysteresis (≥12) + T5 gate active/inactive (≥3) |
| US-009.2 | ≥ 5 | stub shape + cross query type + dual_read 相容 |
| US-009.3 | ≥ 46 | DoD (40) + drill (6) |
| **合計** | **≥ 66** | |

---

*本文件為 M4 Canary acceptance spec 基準。任何修改需經 council review 並更新版本號。*
