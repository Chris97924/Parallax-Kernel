# M5 Entry Spec — Aphelion Real Retrieval

> **Version**: v0.1.0 (DRAFT)
> **Date**: 2026-05-06
> **Status**: Draft — pending Chris review + M4 DoD sign-off
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
| E.1 | M4 DoD signed off — Stage @1% / @10% / @50% / @100% all green per `canary-stage-runbook.md` §3–§6 | DoD verifier JSON `overall: pass` for all 4 stages × 7-day window; Chris ACK in `#m4-canary` for each promotion |
| E.2 | M4 P×A Dashboard row ticked | Notion `343f3661...` M4 row checkboxes all set |
| E.3 | M3 dual-read corpus stayed green throughout M4 rollout — `dual_read_discrepancy_rate < 0.1%` continuously for **≥ 72h covering each M4 stage transition** (per `stage-0-preflight-checklist.md` §2) | `dual_read_continuity_check --since=72h --metric=discrepancy` exit 0 at every Stage @1%/@10%/@50%/@100% promotion gate |
| E.4 | Aphelion v0.6 retrieval API spec drafted (separate Aphelion repo deliverable) | `Chris97924/Aphelion-Graph` has v0.6 PR open or merged with retrieval contract |
| E.5 | No M4 production rollback active or pending hysteresis | `RollbackController.state` returns `CanaryState.RUNNING`, not `TRIPPED` / `AWAITING_ACK`. CLI inspection mode (`parallax canary --check-state`) is **not yet wired**; tracked under §3.2 ops as a pre-M5 deliverable. Until then, query controller via REPL during preflight |
| E.6 | Fresh rollback-path drill against the M5 branch HEAD passes | `parallax canary --rollback-drill --dry-run --format json` exits 0 with all 3 drills `overall: pass` (per `stage-0-preflight-checklist.md` §7) |

## 3. Scope — What M5 Owns

### 3.1 In-scope code changes

| Module | Change | Notes |
|---|---|---|
| `parallax/router/aphelion_stub.py` | Rename to `aphelion_adapter.py` (or co-locate) and replace `query()` raise-only body with real HTTP call | Keep `AphelionUnreachableError` exception; raise on real network failure |
| `parallax/router/dual_read.py` | Allow non-stub `secondary` adapter; preserve dual-read semantics + arbitration | US-009 §7 O.2 explicitly held this back to M5 |
| `parallax/router/` (new) | Aphelion HTTP client config (base URL, auth, retry, timeout) | Read from `parallax.config` env vars |
| `tests/router/` | New tests covering real-adapter path + failure modes (timeout, 5xx, malformed payload) | Keep existing dual-read test expectations green per US-009 §7 O.2 |

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
| Edge rate-limit | Per-source-IP rate limit on `event_id` ingestion to prevent denial-of-cache via UUID flood (out of M5 scope but must be documented as upstream assumption) |
| Audit-write failure observability | Wire `parallax_audit_write_failures_total` Prom counter; alert on > 0 / 5 min (closes audit-evasion vector identified in security review) |

### 3.2 In-scope ops

- New retrieval-side observability: `aphelion_request_latency_ms` (p50/p99), `aphelion_unreachable_total` (error budget), `retrieval_evidence_size_bytes`.
- Update Grafana dashboard with retrieval panels (separate from M4 canary dashboard).

### 3.3 In-scope docs

- `docs/m5/aphelion-real-adapter-design.md` — adapter contract, retry policy, timeout budget.
- `docs/adr/00XX-m5-aphelion-retrieval.md` — ADR for the cutover.
- `docs/router/` updates if dual-read arbitration semantics shift.

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

## 6. Risks Surfaced from M4 Experience

- **Stub→real cutover regression**: M3 `dual_read_router` was hardened against stub raising every call. Real adapter sometimes succeeding, sometimes failing, sometimes slow → arbitration + breaker behaviour needs explicit re-test, not "shouldn't change".
- **Aphelion v0.6 dependency**: M5 cannot start before Aphelion v0.6 spec lands. If Aphelion delays, M5 entry blocks regardless of M4 status.
- **Latency budget pressure**: T3 = 100 ms is the M4 canary trigger; real Aphelion call adds latency. M5 may need either a budget revision or a request-side cache to stay below T3 during the cutover stages.

## 7. Open Questions for Chris

1. Does M5 reuse the M4 4-stage canary infra (1%→10%→50%→100%), or is a flag-based gradual rollout enough?
2. Is Aphelion v0.6 spec being drafted in parallel, or does M5 block until you start it?
3. Should the `AphelionReadAdapter` rename break the import path (forcing every site to update) or stay backward-compatible via re-export shim?
4. M5 DoD latency budget — keep at 100 ms inheriting T3, or relax (e.g., to 200 ms) given real network overhead?
5. **Real-adapter arbitration policy** — when both primary and real Aphelion succeed but with divergent payloads, which side wins? Options: `primary-wins` (safe / null hypothesis), `aphelion-wins` (trust upgrade), `consensus-required` (block on disagreement), `log-only-no-arbitration` (passive observation through M5 ramp). US-009 §7 O.2 deferred this exact question — M5 must answer it before §3.1 router changes land.

---

**Next step (when M4 GATE 7 passes)**: harden §5 into a numbered acceptance criteria doc, open M5 epic ticket, kick off implementation.
