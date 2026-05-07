# M5 Entry Spec — Aphelion Real Retrieval

> **Version**: v0.2.0 (council-resolved)
> **Date**: 2026-05-07
> **Status**: Council-resolved (OQ1–OQ5) + Chris-pinned thresholds; pending PR #46 merge
> **Author**: autopilot session 2026-05-06
> **Supersedes**: nothing; first draft
> **Upstream refs**: `docs/m4-prep/us-009-acceptance-criteria.md` §2, §7; `docs/m4-prep/canary-stage-runbook.md`

---

## 1. Purpose

Define the entry conditions, scope, and out-of-scope boundary for **Parallax M5**, so that M5 work can begin the moment M4 DoD signs off without renegotiating scope.

M5 replaces the M3-era null-stub `AphelionReadAdapter` (raise-only) with a real HTTP adapter that performs actual retrieval against the Aphelion service.

**Audience**: Chris (拍板者), implementation owner (codex agent or human), reviewer (PR audit).

## 2. Entry Preconditions (all must hold before M5 PR opens)

| # | Condition | Evidence |
|---|---|---|
| E.1 | M4 DoD signed off — Stage @1% / @10% / @50% / @100% all green per `canary-stage-runbook.md` §3–§6 | DoD verifier JSON `overall: pass` at each stage promotion gate, scoped to that stage's runbook observation window: @1% = 24h (§3), @10% = 48h (§4), @50% = 72h (§5), @100% = 1 week (§6); Chris ACK in `#m4-canary` for each promotion |
| E.2 | M4 P×A Dashboard row ticked | Notion `343f3661...` M4 row checkboxes all set |
| E.3 | M3 dual-read corpus stayed green throughout M4 rollout — `dual_read_discrepancy_rate < 0.1%` continuously for **≥ 72h covering each M4 stage transition** (per `stage-0-preflight-checklist.md` §2) | `dual_read_continuity_check --since=72h --metric=discrepancy` exit 0 at every Stage @1%/@10%/@50%/@100% promotion gate |
| E.4 | Aphelion v0.6 retrieval API spec drafted (separate Aphelion repo deliverable) | `Chris97924/Aphelion-Graph` has v0.6 PR open or merged with retrieval contract |
| E.5 | No M4 production rollback active or pending hysteresis | `RollbackController.state` returns `CanaryState.RUNNING`, not `TRIPPED` / `AWAITING_ACK`. CLI inspection mode (`parallax canary --check-state`) is **not yet wired**; tracked under §3.2 ops as a pre-M5 deliverable. Until then, query controller via REPL during preflight |
| E.6 | Fresh rollback-path drill against the M5 branch HEAD passes | `parallax canary --rollback-drill --dry-run --format json` exits 0 with all 3 drills `overall: pass` (per `stage-0-preflight-checklist.md` §7) |
| P-A1 | Aphelion staging endpoint health gate — 連續 48h 可用度 ≥99.5% + p95 latency ≤150ms | Prometheus query: `avg_over_time(up{job="aphelion-staging"}[48h]) >= 0.995` AND `histogram_quantile(0.95, rate(aphelion_request_duration_seconds_bucket{job="aphelion-staging"}[48h])) * 1000 <= 150` |
| P-A2 | Aphelion v0.6 HTTP envelope spec frozen (auth headers, status codes, error shape, retry semantics) — payload schema 可保持 draft | Aphelion-Graph repo v0.6 PR §envelope 章節 marker frozen |
| P-A3 | rename-only PR #47 (`aphelion_stub.py` → `aphelion_adapter.py`) merged 進 main-next | git log shows PR #47 squash commit on main-next |

> **Note on E.4 vs P-A2 overlap**: E.4 is the broad "Aphelion v0.6 spec drafted" gate; P-A2 is the more precise envelope-vs-payload phased gate per OQ2 council resolution. The two coexist; Chris may decide to merge them at PR #46 review time.

## 3. Scope — What M5 Owns

### 3.1 In-scope code changes

| Module | Change | Notes |
|---|---|---|
| `parallax/router/aphelion_stub.py` | Rename to `aphelion_adapter.py` (or co-locate) and replace `query()` raise-only body with real HTTP call | Keep `AphelionUnreachableError` exception; raise on real network failure |
| `parallax/router/dual_read.py` | Allow non-stub `secondary` adapter; preserve dual-read semantics + arbitration | US-009 §7 O.2 explicitly held this back to M5 |
| `parallax/router/` (new) | Aphelion HTTP client config (base URL, auth, retry, timeout) | Read from `parallax.config` env vars |
| `tests/router/` | New tests covering real-adapter path + failure modes (timeout, 5xx, malformed payload) | Keep existing dual-read test expectations green per US-009 §7 O.2 |

**OQ3 rename strategy resolved**: Per OQ3 council resolution, the rename `aphelion_stub.py` → `aphelion_adapter.py` is split out to a standalone mechanical PR (#47) ahead of any implementation change. PR #46 (this spec) does not carry rename diff. The implementation PR for §3.1 lands after PR #47 merges, keeping reviewer diff signal-to-noise high.

### 3.1a Security Invariants (must hold for every M5 implementation)

Per security review 2026-05-06 — these are not negotiable and must be enforced via lint/test:

| Invariant | Rule |
|---|---|
| TLS verification | `verify=True` always; `verify=False` banned at lint level |
| Base URL allowlist | No user-influenced host segment; `PARALLAX_APHELION_BASE_URL` must match a fixed prefix |
| Credential injection | Bearer token from env var only (`PARALLAX_APHELION_TOKEN`); never inline in spec / docs / tests |
| Error reason sanitisation | `AphelionUnreachableError(reason)` carries only an enum-like tag (`http_5xx`, `timeout`, `tls_fail`); never raw response body or URLs/tokens |
| Retry policy | Exponential backoff with **hard cap ≤ 3 retries** + jitter; connect / read timeouts configured separately |
| Audit `response_body` PII | Cap size at 16 KB; PII-tagged retrievals MUST be redacted before audit-log write; SQLite audit DB file mode `0600` on creation |
| Audit-write failure observability | Wire `parallax_audit_write_failures_total` Prom counter; alert on > 0 / 5 min (closes audit-evasion vector identified in security review) |
| **Invalidate event session isolation** | Cache invalidate events triggered by R-8 must not leak across `session_id` boundaries. A given session's invalidate may not reveal another session's existence (timing-side-channel) or cached keys (event-payload). |
| **Aphelion auth header redaction** | The bearer token in Aphelion HTTP request headers MUST be redacted (replaced with `<REDACTED>`) before any audit-ledger write or log line. Lint rule: forbid passing raw `Authorization` value to `audit_log.write()` or `logger.*()`. |

### 3.1b Upstream Assumptions (deferred — non-blocking for M5)

These items came out of the same 2026-05-06 security review but are **not** M5 lint/test gates. They are documented here so reviewers do not mistake their absence for an oversight; ownership and target milestone are listed.

| Assumption | Rationale | Owner / Target |
|---|---|---|
| Edge rate-limit on `event_id` ingestion | Per-source-IP rate limit prevents denial-of-cache via UUID flood. Ingestion edge sits upstream of `parallax/router/`; controlling it inside M5 scope would require touching ingest-tier infra not in the M5 module map. | Edge / ingest-tier ticket; target M6 (or earlier if ingest infra work lands first). M5 PR review MUST NOT block on this row. |

### 3.1c Router Invariants (Real Adapter Path)

Per OQ4 + OQ5 council resolution + Chris-pinned numbers (2026-05-07), the following invariants are mandatory for any §3.1 router implementation PR:

| # | Invariant | Rationale |
|---|---|---|
| R-7 | **Session-scoped arbitration monotonicity** — within a single request lifecycle for a given `session_id`, the arbitration source (cache vs Aphelion) must be monotonic. Once the request triggers an Aphelion fetch, all subsequent same-session same-key lookups in that request route to the fresh path until the invalidate batch window flushes. | Mitigates "state tearing" risk surfaced by Gemini/GPT-OSS/Laguna council synthesis (cache hit returns stale, parallel miss-fetch overwrites mid-request). |
| R-8 | **Aphelion-wins arbitration with divergence budget** — when local cache and Aphelion HTTP responses disagree, Aphelion wins and local cache is invalidated. **Divergence rate ≤2% per 1h sliding window**; exceeding this auto-trips circuit-breaker into stub fallback (using OQ1 reused canary rollback infra). | Honours session continuity SSoT (Parallax = retrieval source of truth in Aphelion). Divergence budget addresses Codex/Qwen warning about "semantic drift hidden until M6". |
| R-9 | **Cache invalidate batching** — invalidate events triggered by R-8 are batched within a **≤500ms window**, not flushed per-event. | Mitigates Laguna/Nemotron warning about high-frequency invalidate causing Aphelion traffic spike. |
| R-10 | **Divergence telemetry** — every Aphelion-overrides-cache event MUST write an audit-ledger row containing `{event_id, session_id, key, local_hash, aphelion_hash, reason_code, ts}`. The `local_hash` and `aphelion_hash` are content hashes (not raw payloads, per §3.1a `response_body` PII rule). | Closes the M6 burn-in observability gap (Codex synthesis): semantic drift becomes a first-class signal not a corpus diff archaeology. |

**Latency tier (OQ4(c) Chris-pinned)**: cache hit p95 ≤100ms; miss-and-fetch p95 ≤250ms. Prometheus alerts label these paths via `path={hit|miss}` to avoid single-threshold alert fatigue.

### 3.2 In-scope ops

- New retrieval-side observability: `aphelion_request_latency_ms` (p50/p99), `aphelion_unreachable_total` (error budget), `retrieval_evidence_size_bytes`.
- Update Grafana dashboard with retrieval panels (separate from M4 canary dashboard).

### 3.3 In-scope docs

- `docs/m5/aphelion-real-adapter-design.md` — adapter contract, retry policy, timeout budget.
- `docs/adr/00XX-m5-aphelion-retrieval.md` — ADR for the cutover.
- `docs/router/` updates if dual-read arbitration semantics shift.

### 3.4 Canary Infrastructure — Reuse vs Redefinition (OQ1)

Per OQ1 council resolution, M5 reuses M4 canary infrastructure partially. CanaryRolloutDriver abstraction is **not** introduced in M5 (YAGNI for two callers).

**Reused as-is from M4 `parallax/canary/`**:
- `audit_log` SQLite store + UUIDv7 `event_id` keying
- Idempotency layer (duplicate `event_id` no-op)
- Stage promotion gates (1% / 10% / 50% / 100%)
- Sticky session-hash routing
- Kill-switch / feature-flag flip mechanism
- 30-min hysteresis cooldown after rollback

**Redefined for M5 (HTTP adapter failure modes)**:
- Rollback triggers — M4 triggers were internal SLO-breach; M5 adds: Aphelion divergence budget breach (R-8), Aphelion health-gate breach (P-A1), HTTP-class errors (timeout / 5xx / TLS) beyond M4's in-process exception model
- T1–T4 trigger thresholds — recalibrated against the latency tier in §3.1c (miss-fetch 250ms, not 100ms)
- T5 min-hits gate — sample-size baseline retuned for M5 traffic profile (Aphelion-routed % is initially low)

## 4. Out of Scope — Explicit Deferrals

| Item | Defer to | Reason |
|---|---|---|
| Multi-tenant retrieval | M6+ | M5 = single-tenant baseline |
| Vector reranking / hybrid search | M6+ | M5 ships exact-match retrieval first |
| Aphelion package format changes | Aphelion v0.7 ticket | M5 uses v0.6 contract as-is |
| New canary stages for retrieval cutover | M5 may reuse M4 canary infra; if not, separate ticket | Default: gradual flag-based rollout, not new 4-stage drill |
| Cross-circle handoff redesign | Orbit M-roadmap | Orbit owns this, not Parallax |

## 5. Deliverables (M5 DoD shape — to be hardened later)

This section is **placeholder**. Final M5 DoD will be set in a follow-up `docs/m5-prep/us-XXX-acceptance-criteria.md` once Aphelion v0.6 contract is locked. Tentative axes:

1. Real-adapter unit tests ≥ 30 cases covering happy + 4 failure modes.
2. Integration test: dual-read with real Aphelion adapter against a fixture server, < 0.1% discrepancy over 1k requests.
3. p99 retrieval latency budget (TBD with Aphelion team — placeholder ≤ 200 ms).
4. No regression on M3 metrics for 72h after M5 cutover.

### Hard gates (Council + Chris-pinned, 2026-05-07)

The following gates are NON-tentative and MUST be checked at M5 entry + maintained throughout burn-in:

- **G-A** — Aphelion staging health gate (P-A1) GREEN at M5 entry AND continuously GREEN throughout the 14-day M6 burn-in. Auto-page on >5min RED.
- **G-B** — Divergence telemetry dashboard (R-10 audit-ledger backed) instrumented before §3.1 router PR opens. Alert when divergence rate >2%/1h (R-8 budget).
- **G-C** — Tiered latency SLO (§3.1c) measured continuously: cache hit p95 ≤100ms, miss-fetch p95 ≤250ms. Two separate Prometheus alerts (path={hit|miss}).
- **G-D** — PR #47 (rename-only) AND M5 implementation PR both merged on main-next; PR #47 must precede the implementation PR per OQ3(b).

## 6. Risks Surfaced from M4 Experience

- **Stub→real cutover regression**: M3 `dual_read_router` was hardened against stub raising every call. Real adapter sometimes succeeding, sometimes failing, sometimes slow → arbitration + breaker behaviour needs explicit re-test, not "shouldn't change".
- **Aphelion v0.6 dependency**: M5 cannot start before Aphelion v0.6 spec lands. If Aphelion delays, M5 entry blocks regardless of M4 status.
- **Latency budget pressure**: T3 = 100 ms is the M4 canary trigger; real Aphelion call adds latency. M5 may need either a budget revision or a request-side cache to stay below T3 during the cutover stages.
- **Aphelion health gate is the implicit prerequisite of all other M5 decisions**: OQ2/OQ4/OQ5 council resolutions all assume Aphelion v0.6 endpoint stability. P-A1 (48h / 99.5% / 150ms) hardens this assumption into a measurable gate. If Aphelion staging fails P-A1, M5 entry is blocked regardless of all other readiness signals.

## 7. Open Questions for Chris

1. **[RESOLVED 2026-05-07 — OQ1(c)]** Does M5 reuse the M4 4-stage canary infra (1%→10%→50%→100%), or is a flag-based gradual rollout enough? → Council resolution: partial reuse, see §3.4.
2. **[RESOLVED 2026-05-07 — OQ2(c)]** Is Aphelion v0.6 spec being drafted in parallel, or does M5 block until you start it? → Phased per P-A2.
3. **[RESOLVED 2026-05-07 — OQ3(b)]** Should the `AphelionReadAdapter` rename break the import path (forcing every site to update) or stay backward-compatible via re-export shim? → Rename-only PR #47 (separate from this PR), see §3.1.
4. **[RESOLVED 2026-05-07 — OQ4(c)]** M5 DoD latency budget — keep at 100 ms inheriting T3, or relax (e.g., to 200 ms) given real network overhead? → Tiered, see §3.1c latency tier.
5. **[RESOLVED 2026-05-07 — OQ5(b) + guardrails]** **Real-adapter arbitration policy** — when both primary and real Aphelion succeed but with divergent payloads, which side wins? Options: `primary-wins` (safe / null hypothesis), `aphelion-wins` (trust upgrade), `consensus-required` (block on disagreement), `log-only-no-arbitration` (passive observation through M5 ramp). US-009 §7 O.2 deferred this exact question — M5 must answer it before §3.1 router changes land. → Aphelion-wins with R-7~R-10 in §3.1c.

### Resolution log

2026-05-07 — 12-model xcouncil (Gemini 3.1 Pro / Codex / Sonnet / Nemotron 3 / Laguna M.1 / DeepSeek V4 Pro / Qwen3-Next 80B / GPT-OSS 120B / MiMo V2.5 Pro; GLM-5.1 / MiniMax M2.7 / Kimi K2.5 endpoints failed) + Opus judge resolved OQ1–OQ5. Chris pinned the 5 numerical thresholds (P-A1, R-8 divergence, R-9 batch window) on the same date. Spec v0.2.0 fold landed in this PR.

---

**Next step (when M4 GATE 7 passes)**: harden §5 into a numbered acceptance criteria doc, open M5 epic ticket, kick off implementation.
