---
title: Apex M7 — Apex Router Public-Read Reframe Spec
status: spec-only
version: v0.1-reframe-2026-05-28
date: 2026-05-28
owner: Parallax-Kernel
consumers:
  - Apex M7 router implementation (future PR, NOT this spec)
  - M6.5 Perihelion bridge owners (boundary contract consumer)
  - Chris (M7 entry GATE decision)
upstream:
  - E:\Apex\vault\users\chris\active.md (2026-05-28 MIDDAY framing reconcile)
  - E:\Apex\vault\users\chris\wiki\derived\strategy\messier-roadmap-v0-to-v11.md M7 段 (2026-05-22 ripple note)
  - feedback_perihelion_as_memory_layer (2026-05-20 directive)
  - reference_memory_stack_architecture (4-layer stack)
  - Parallax/docs/m6-prep/m6-ingest-contract-spec.md v0.1-frozen-2026-05-17 (route A)
  - Parallax/docs/m5-prep/apex-m5-entry-spec.md v0.3.0-reframe (local-file adapter)
supersedes_wording:
  - "M7 — 雙系統 Primary：Apex router 接管讀寫 / MEMORY.md archive 到 .bak" (2026-05-08 era)
  - "M7 DoD: data_loss_events = 0 累積 10,000 write ops + 完整 backup/restore 週期" (2026-05-08 era — write-DoD now out-of-scope)
pivot:
  - 2026-05-20 Chris directive: 私人 memory 一律走 Perihelion，不再以 Claude Code auto-memory prefix 為 SoT (feedback_perihelion_as_memory_layer)
  - 2026-05-22 roadmap ripple note: M6.5 已重設為 Perihelion-based；M7 body 待 reconcile
  - 2026-05-28 MIDDAY framing reconcile: Apex (= Parallax × Aphelion) 只涵蓋公開知識記憶層；Perihelion 是 Singularity 5 sub-project sibling 不是 Apex 子集
---

# Apex M7 — Apex Router Public-Read Reframe Spec

> **Note**: This spec is a wording reframe. No code change is described or implied. The reframe target is the 2026-05-08 era M7 entry on the Apex M-roadmap (`messier-roadmap-v0-to-v11.md`), which said "Apex router 接管讀寫 + MEMORY.md archive 到 `.bak`". After the 2026-05-20 Perihelion directive and the 2026-05-28 MIDDAY framing reconcile, that wording is architecturally stale — Apex never owned the private write path. This document restates M7 scope, drivers, boundaries, paths, archive timeline, dependencies, and open questions in line with the current framing.

---

## 0. Why this spec exists (one paragraph)

The 2026-05-08 M7 entry on the Apex roadmap was drafted before two architectural reframes landed: (a) the 2026-05-20 Chris directive that private memory must flow through Perihelion (the private self-model sibling), not through Parallax's public dual-read path, captured in `feedback_perihelion_as_memory_layer`; and (b) the 2026-05-28 MIDDAY framing reconcile that scoped Apex (= Parallax × Aphelion) to **public knowledge memory only**, with Perihelion as a peer Singularity sub-project — not an Apex submodule. The 2026-05-22 roadmap ripple note already flagged that M6.5 had been reset for Perihelion via the xcouncil session, but explicitly left the M7 body alone pending Chris's call. This spec is that call: M7 is now scoped to the public-read router only, and the private read/write surface is permanently out-of-scope (it lives in Perihelion's M6.5 path). The original write-DoD (10k write ops + backup/restore cycle) and the `MEMORY.md → .bak` archive mechanism are also reframed — see §6.

---

## 1. Scope — M7 = Apex Router Public-Read Only

### 1.1 In-scope

| Surface | M7 owns |
|---|---|
| **Public read** | Apex router reads claim/evidence from Aphelion-packaged `.aphelion.tar` files (per M6 ingest route A), exposes a stable retrieve API to consumers (Claude Code via UserPromptSubmit hook, future external clients) |
| **Public read SLA** | p99 read < 100ms (preview of M8 SLA; locked in this spec so M7 wiring does not need rework at M8) |
| **Public read failure semantics** | Aphelion package missing / unsigned / corrupt / signer-untrusted → `AphelionUnreachableError` with explicit `reason_code`; **never** silently fall through to Perihelion (per §3 hard boundary) |
| **Public write metadata** | M6 `parallax ingest` CLI (already shipped, NOT deployed — see §7 dependencies) continues to be the only write path to the public layer; audit.db rows + sha256 manifest digest carry forward unchanged |
| **MEMORY.md archive timeline** | Per-purpose shrink schedule (§6), **not** wholesale `.bak` archive |

### 1.2 Out-of-scope (explicit — permanent move to Perihelion M6.5)

| Surface | Why out-of-scope | Where it lives instead |
|---|---|---|
| **Private read** (self-model / Chris-only memory) | Perihelion is the private self-model layer per 2026-05-20 directive; Apex router never reads it | Perihelion in-process Python API (M6.5 P0 SHIPPED, P1 SHIPPED, P2 substrate MERGED) |
| **Private write** | Perihelion ingest cascade (Stop-hook → fan-out → Perihelion ingest_episode); Apex router never writes a Perihelion claim | Perihelion `perihelion.ingest.*` API + agent-config perihelion-bridge fan-out worker |
| **Memory prefix mutation** | `~/.claude/projects/.../memory/MEMORY.md + *.md` remains a transitional safety-net through Perihelion soak window; M7 does not touch it | M6.5 P3 prefix shrink (Chris-gated, post-Perihelion v1.0) |
| **Wholesale MEMORY.md → .bak archive** | The 2026-05-08 framing assumed Apex would replace MEMORY.md atomically. After the Perihelion split, MEMORY.md belongs to the private layer that Apex never owned — there is nothing for Apex to archive | Per-purpose shrink schedule (§6) under M6.5 control |
| **Cross-write conflict resolution between Apex and Perihelion** | The two layers do not share keys, do not arbitrate, do not invalidate each other. They are independent stores | n/a |

### 1.3 Removed from prior wording

| Prior M7 line (2026-05-08) | Status |
|---|---|
| "Apex router 接管讀寫" | **Reframed** → "Apex router 接管公開讀 only" (private read/write deleted from scope) |
| "MEMORY.md archive 到 `.bak`" | **Deleted** — wholesale archive replaced by per-purpose shrink (§6) under M6.5 control |
| "DoD: data_loss_events = 0 累積 10,000 write ops" | **Reframed** — public-layer write DoD already lives in M5 dual-write + M6 ingest; M7 public-read does not introduce a new write surface, so the write-ops DoD does not apply here |
| "DoD: 跨過一次完整 backup/restore 週期" | **Reframed** — backup/restore belongs to the M4.5 S3 backup track (already carved out 5/9 council vote, parallel to canary stages); M7 public-read does not own backup. M7 just inherits M4.5's backup posture |

> **Backward-compatibility note**: the 2026-05-08 roadmap M7 entry is not deleted. It remains in `messier-roadmap-v0-to-v11.md` for historical lineage. This spec is the canonical M7 entry going forward; a follow-up doc-only PR can add a one-line pointer "see `docs/m7-prep/apex-router-public-read-spec.md` v0.1-reframe-2026-05-28" to the roadmap. That pointer-add is **not** in this PR's scope (per the brief's "不要動 — 其他 M-roadmap doc").

---

## 2. Drivers — what forced the reframe

### 2.1 2026-05-28 MIDDAY framing reconcile (primary)

From `E:\Apex\vault\users\chris\active.md` first headline 2026-05-28 MIDDAY:

> Apex (= Parallax × Aphelion) **只涵蓋公開知識記憶層**，**不包含** Perihelion (私人 self-model)；Perihelion 是 Singularity 5 sub-project 的 sibling 不是 Apex 子集；「整個記憶系統」實際 = Apex 公開讀 + Perihelion 私人寫讀 **雙軌獨立**。

This is the canonical product-scope sentence going forward. Apex does **not** subsume the private memory layer. The two layers are independent tracks with independent ETAs (the 2026-05-28 reconcile gave 6-10 weeks for Apex public-read router, 4-6 weeks for the Perihelion private track).

### 2.2 2026-05-20 Perihelion directive (load-bearing for §3 hard boundary)

From `feedback_perihelion_as_memory_layer`:

> 以後要用我的 Perihelion. memory prefix 是過渡期 placeholder，不是長期 SoT.

This is why Apex never gets the private read/write surface — it was always supposed to be Perihelion's. The 2026-05-08 M7 framing predated this directive and accidentally encroached on Perihelion territory by saying "router 接管讀寫".

The directive also pinned four hard "不要做的事" lines, three of which translate directly into M7 § 3 boundary rules below:

1. Don't hard-cut before Perihelion deploys → M7 entry gates on Perihelion v0.7.6+ stable (§7 E.3)
2. Don't use "Perihelion 未來會接" as an excuse to neglect prefix → M7 does not touch prefix, prefix remains under M6.5 P3 control (§6)
3. Don't auto-sync prefix into Perihelion without Chris ACK → orthogonal to M7 (sync is M6.5 P2); listed only to note M7 also does not auto-sync prefix into Apex public layer

### 2.3 Singularity 5 sub-project framing (LOCKED 2026-05-07)

From `active.md` Singularity section + `project_singularity_cosmological_umbrella`:

> 5 個 sub-project = 差異化方向：Parallax / Orbit / Aphelion / Perihelion / DUST V
> Apex = Parallax × Aphelion 融合層產品（不是第 6 個 sub-project，是疊加在 Parallax + Aphelion 上的記憶 KB 產品）

Apex is a fusion *layer* on top of Parallax + Aphelion. It does not extend across to Perihelion. Any M7 wording that has Apex router "owning" memory writ-large violates this framing.

### 2.4 2026-05-22 roadmap ripple note (explicit defer-to-Chris signal)

From `messier-roadmap-v0-to-v11.md` M7 段:

> M6.5 已重設為 Perihelion-based。M7 本段 要做/DoD 仍是 2026-05-08 era 的 Parallax-centric 框架，與 5/20 Perihelion directive 同樣需一次 reconcile — xcouncil 議會只 scope M6.5，M7 body 待 Chris 另判，暫不動。

This spec **is** that judgement. The reframe is intentionally narrow: only M7 body, not the M-roadmap doc itself (per brief's "不要動").

---

## 3. Apex Public-Read vs Perihelion Private — 4 Hard Boundaries

These boundaries are normative. Any future M7 implementation PR that violates them is rejecting the reconcile and must escalate to Chris.

### 3.1 Boundary 1 — Apex never writes a Perihelion claim

**Rule**: The Apex router (`parallax/router/`, `parallax/apex/`, and any M7-introduced router module) MUST NOT call any Perihelion ingest API directly or indirectly. Apex writes go through the M6 `parallax ingest` CLI into the public audit.db + `.aphelion.tar` corpus; Perihelion writes go through the perihelion-bridge fan-out worker into Perihelion's PG + pgvector store. No cross-layer write.

**Why**: Perihelion is private self-model; Apex is public knowledge fusion. Crossing the write boundary would (a) leak Chris's private claims into a corpus designed to be publishable / shareable / federation-bound, (b) bypass Perihelion's Tier-1/2/3 judge cascade that gates what becomes a stable claim, and (c) re-introduce the exact "private memory routed through Parallax" anti-pattern the 2026-05-20 directive shut down.

**Enforcement**: M7 router code review checklist MUST include "no Perihelion import / no Perihelion DSN read / no cross-call to perihelion-bridge". The retrieve facade design (M6.5 P0 shim) already enforces this on the read side — it imports Perihelion in-process but never lets the public Apex router see the Perihelion handle.

**Cross-ref**: `reference_memory_stack_architecture` §"Perihelion 私人 self-model 核心"; `feedback_perihelion_as_memory_layer` 不要做 #3 ("不要把 prefix 內容自動同步進 Perihelion 沒問 Chris" — same direction, write-side mirror).

### 3.2 Boundary 2 — Apex never reads `perihelion_*` prefix schema

**Rule**: The Apex router MUST NOT issue reads against any Perihelion schema. Concretely: no SELECT/SQL against `perihelion_*` tables, no in-process `perihelion.retrieval.Retriever` call from Apex code, no shared-memory channel where Apex pulls Perihelion-resident claims.

**Why**: The retrieve facade (M6.5 P0 thin shim, pm2-managed) is the only legal in-process touchpoint for Perihelion reads. Apex router calling Perihelion directly would (a) re-introduce the cross-machine in-process-import impossibility that the council rejected in M6.5 (Win/WSL2-resident Perihelion vs ZenBook-resident Apex), (b) defeat the timeout-and-fallback contract the shim guarantees (Perihelion unavailable → fall back to prefix, not raise into the public read path), and (c) entangle public-read SLA (p99 < 100ms) with private-read SLA (Perihelion can be slower because judge cascade).

**Enforcement**: M7 router code review checklist MUST include a grep of the router module for `perihelion_(claim|episode|retrieval|ingest)` and a grep for any `import perihelion` line — both expected empty. (Adjust path separator and shell quoting per the reviewer's OS; the pattern is what matters, not the shell.) The shim is the *only* place Perihelion is imported.

**Cross-ref**: `reference_memory_stack_architecture` §"為啥現在不能拆 — 3. Parallax retrieve quality 沒驗證夠"; M6.5 議會 推薦 (b) 獨立 thin shim 部署形態.

### 3.3 Boundary 3 — Public read = Aphelion `.aphelion.tar` package read (per M6 route A)

**Rule**: All public-layer reads exposed by the Apex router go through the Aphelion-packaged file format. Concretely: the router reads from `PARALLAX_APHELION_PACKAGE_DIR` (per M5 entry spec §3.1c P-A2'), invokes the Aphelion lib public surface — `aphelion.unpacker.unpack()` (untar with v0.2 §S2.5 safety rules) + `aphelion.verifier.verify_package()` (signer chain validation) + `aphelion.read_adapter.AphelionReadAdapter` (claim/evidence projection) + `aphelion.validator.validate_signatures(...)` (claim-level signature check per v0.3 R1-R4) — and returns claim list per Aphelion v0.5+ lib API. The exact composition of these calls is implementation-detail for the M7 PR; this spec pins only the public-API surface used (no private `_*` functions, no internal modules).

**Why**: This is the route-A pivot finalized 2026-05-16 noon. Route B (stub claim_loader reads JSON dir) is permanently rejected — it bypasses signer verification, evidence chains, and the canonical packaging contract. The Apex public layer's whole reason for existing is to expose *verified* knowledge; reading anything that isn't signature-verified is outside the product's value proposition.

**Library compatibility (load-bearing)**: M7 router MUST assert `aphelion.__version__ >= REQUIRED_APHELION_MIN_VERSION` at process startup; mismatch raises a hard startup error rather than risking silent behavioral divergence (e.g., a future v0.6 lib that changes `verify_package()` return shape would otherwise be silently misinterpreted as success). `REQUIRED_APHELION_MIN_VERSION` is pinned in the M7 implementation PR — likely `"0.5.0"` per Aphelion v0.5 ship status.

**Enforcement**: M7 router code review checklist MUST include "every read path eventually goes through `aphelion.verifier.verify_package + validator.validate_signatures`; no escape hatch for unsigned packages; `AphelionUnreachableError(reason='unsigned_package')` on signer fail; startup version assertion present and tested".

**Cross-ref**: `docs/m6-prep/m6-ingest-contract-spec.md` v0.1-frozen-2026-05-17 (route A canonical decision); `docs/m5-prep/apex-m5-entry-spec.md` §3.1a "Signer verification" invariant; `docs/m5-prep/apex-m5-entry-spec.md` §3.1a "Untar safety" invariant (Aphelion v0.2 §S2.5 untar-safety rules carry forward unchanged).

### 3.4 Boundary 4 — Private read/write = Perihelion in-process Python API (per M6.5)

**Rule**: All private-layer reads and writes go through `perihelion.retrieval.Retriever` and `perihelion.ingest.*` APIs in-process, called from the M6.5 P0 thin shim (`agent-config/claude/perihelion-bridge/`). Network round-trips are explicitly forbidden (Perihelion is `never-HTTP` per its own roadmap).

**Why**: This is the M6.5 P0 contract that landed in PR `Chris97924/agent-config#1` squash-merged `562af7d`. The shim is pm2-managed, runs on the same host as Perihelion (Win/WSL2 → eventual ZenBook), and exposes a loopback IPC surface that Claude Code's UserPromptSubmit/Stop hooks use. Apex router does not see this surface.

**Enforcement**: This boundary is enforced by Perihelion's deployment topology, not by Apex code. M7 only honors it by *not* attempting to call Perihelion. The cross-ref ensures M7 reviewers can verify the boundary is bidirectional.

**Cross-ref**: M6.5 P0 architecture in `messier-roadmap-v0-to-v11.md` M6.5 段 "dual-write 拓撲" + `project_apex_m65_redesign`; `reference_perihelion_naming` (Perihelion never-HTTP contract).

### 3.5 Boundary consistency check

The four boundaries above are derived from `reference_memory_stack_architecture` 4-layer table:

- Layer 1 (Parallax engine) — owns Apex router execution; bounded by §3.1 + §3.2 + §3.3
- Layer 2 (Aphelion) — owns the public package format; bounded by §3.3
- Layer 3 (Perihelion) — owns private self-model; bounded by §3.1 + §3.2 + §3.4
- Layer 4 (memory prefix) — transitional safety-net; bounded by §6 (not §3)

If a future PR introduces a fifth layer or merges two layers, this §3 boundary set must be re-derived. As of 2026-05-28, the 4-layer model is the canonical reference.

---

## 4. Read Path

### 4.1 Architecture (logical, not code)

```
Client (e.g. Claude Code UserPromptSubmit hook → retrieve facade shim)
   │
   ├─→ Apex Router public-read API
   │      │
   │      ├─→ resolve query → identify candidate .aphelion.tar package(s)
   │      │       via package-id index OR claim-key index (both maintained by M6 ingest)
   │      │
   │      ├─→ aphelion.unpacker.unpack(path)  [local FS read, no network, v0.2 §S2.5 safety]
   │      ├─→ aphelion.verifier.verify_package(...)  [package signer chain]
   │      ├─→ aphelion.validator.validate_signatures(...)  [claim-level sig per §3.3]
   │      ├─→ aphelion.read_adapter.AphelionReadAdapter.query(...)  [claim/evidence project]
   │      │       │
   │      │       └─→ on fail: AphelionUnreachableError(reason="unsigned_package" |
   │      │                                                     "signer_untrusted" |
   │      │                                                     "package_corrupt" |
   │      │                                                     "package_missing")
   │      │
   │      ├─→ claim filter + evidence project per query
   │      └─→ return ranked claim list
   │
   └─ (independently, NOT through Apex) retrieve facade shim also pulls
      Perihelion ranked claims via in-process Python API
        — but that is M6.5's concern, not M7's. "Independently" means
        logically separate paths; the actual concurrency model
        (sync sequential vs threaded vs async) is facade-side
        implementation detail, not specified here.
```

Apex returns its slice; the facade composes the final pre-prompt context. The composition / ranking / dedup between Apex and Perihelion claims is **facade business**, not Apex business. M7 spec does not specify it (and explicitly refuses to specify it, to avoid encroaching on Perihelion's territory).

### 4.2 SLA — p99 read < 100ms (M8 SLA preview)

This is the M8 router p99 read budget pulled forward into M7. Rationale: locking this in M7 wiring avoids a refactor at M8 where the router would otherwise discover its read path exceeds the budget. The 100ms budget is feasible because:

- Aphelion package read is local FS (no network), measured at single-digit-ms in M5 spec stress tests
- Signature verification is CPU-bound but bounded (~10-30ms typical per package per M5 spec)
- Claim filter + project is in-memory after package load
- No Perihelion call on the public read path (Perihelion's slower cascade is decoupled)

The 100ms p99 is **load-tested goal**, not a hard fence — if M7 implementation discovers a 130ms p99 under expected load, the M7 PR can land with a follow-up tightening track. Hard fence is M8's 5k QPS pressure test (per M8 DoD).

### 4.3 Failure modes (must surface, never silently fall through)

| Failure | Apex behavior | Caller (facade) behavior |
|---|---|---|
| Package missing on disk | `AphelionUnreachableError(reason="package_missing")` | Facade logs + returns empty claim list from Apex (does NOT fall back to Perihelion for the same query — they are independent corpora) |
| Package unsigned | `AphelionUnreachableError(reason="unsigned_package")` | Same as above (facade does NOT route to Perihelion as a fallback — §3.2 boundary) |
| Signer untrusted | `AphelionUnreachableError(reason="signer_untrusted")` | Same |
| Package corrupt | `AphelionUnreachableError(reason="package_corrupt")` | Same |
| `PARALLAX_APHELION_PACKAGE_DIR` inaccessible (relative path, non-existent dir, no read perms, broken symlink) | `AphelionUnreachableError(reason="package_dir_inaccessible")` + observable counter `parallax_apex_package_dir_errors_total{reason}` — MUST NOT silently degrade to empty result | Facade logs as misconfiguration; ops alert |
| Empty corpus — dir IS accessible but contains zero `.aphelion.tar` files | Return empty claim list (this is a legitimate fresh-deploy state); also emit `parallax_apex_empty_corpus_total` counter at first-read-per-process boundary so a stuck-empty deployment is visible in dashboards | Facade sees `[]` and proceeds without Apex context |
| Aphelion lib version mismatch (detected at startup per §3.3) | Hard startup error (process refuses to come up); not a per-request failure | n/a — process never enters serving state |
| Aphelion lib raises unexpectedly mid-request | Apex wraps as `AphelionUnreachableError(reason="lib_error", exc_class=<class name>)` + increment `parallax_apex_read_errors_total{reason="lib_error", exc_class=<class name>}` counter (per §4.5) | Same as the "Package missing" row |
| Audit ledger write failure during a divergence-telemetry read (R-10 path) | Hard raise `AphelionAuditWriteError(reason=<cause>)`; the claim list is NOT returned with a missing audit row. Also increment `parallax_apex_audit_write_failures_total{cause}` (M7-introduced, separate from M5's `parallax_audit_write_failures_total` — see §4.5 for why a new counter rather than label mutation) so the read-path audit failure is observable alongside the M5 write-path counter without label-mismatch silent-undercount | Facade logs + surfaces the error; **must not silently substitute Perihelion content for the failed read** |

**Critical rule**: the §3.2 boundary forbids Apex falling back to Perihelion. The facade also must not fall back across the boundary (it composes both sides but does not substitute one for the other). This is the explicit decision in §8 Q3 below. The audit-write-during-read row above is load-bearing because the natural implementer default ("read succeeded, audit failed, return claims anyway") silently drops the divergence audit row and breaks the M5 R-10 provenance guarantee — the spec forbids that path explicitly.

**Library version assertion** (carries forward from §3.3): the version check is startup-only, not per-request. If the assertion ever fails at runtime (e.g., hot-reload swapped lib mid-process — not expected but not forbidden by Python), the spec treats this as a programmer-error path that does not need a per-request failure mode.

### 4.4 Read invariants carried forward from M5

Several M5 invariants apply unchanged to M7 because they cover the same package-read + signature-validation path:

- R-7 session-scoped arbitration monotonicity (M5 §3.1c) — applies on read
- R-8 Aphelion-wins arbitration (M5 §3.1c) — applies when M7 router serves a query that already saw a dual-read result from M5
- R-10 divergence telemetry (M5 §3.1c) — applies on every Apex-overrides-cache event
- Untar safety + signer mandatory + audit `package_id`-only (M5 §3.1a) — apply on every read
- **M5 §3.1c P-A2' startup validation for `PARALLAX_APHELION_PACKAGE_DIR`** (existence, absolute path, read permission, no `..` segments) carries forward unchanged into M7. M7 does not re-derive this; the M5 entry condition is normative for M7 startup as well.

M7 does not re-derive these; the M5 spec entries are normative for M7 as well.

### 4.5 Observability requirements (M7-introduced metrics)

M7 router MUST emit (at minimum) the following Prometheus-shaped metrics. Exact metric names below are normative; histograms and counters per Prom conventions. Dashboards and alerts are out-of-scope for this spec (per §8 Q5, observability is the second PR of the recommended 2-PR split), but the metric *contract* below is normative so the dashboards PR has stable names to bind to:

| Metric | Type | Labels | Emit when |
|---|---|---|---|
| `parallax_apex_read_latency_ms` | Histogram (p50/p90/p99 buckets — bucket set TBD by impl PR) | `result={success, error}` | Every read regardless of outcome; measures wall-clock from request entry to result return |
| `parallax_apex_read_total` | Counter | `result={success, error}` | Every read |
| `parallax_apex_read_errors_total` | Counter | `reason={package_missing, unsigned_package, signer_untrusted, package_corrupt, package_dir_inaccessible, lib_error, audit_write_failure, other}`, `exc_class={<class name>}` for `reason=lib_error` only | Every `AphelionUnreachableError` or `AphelionAuditWriteError` raise. This is the canonical M7 error counter — granular per-reason but a single series family. (Earlier draft had a separate `parallax_apex_lib_errors_total`; folded into this counter to avoid double-count ambiguity.) |
| `parallax_apex_package_dir_errors_total` | Counter | `reason={relative_path, dir_missing, perm_denied, broken_symlink, traversal_segment}` | Every misconfiguration detection at startup or first-read |
| `parallax_apex_empty_result_total` | Counter | `cause={empty_corpus, no_matching_claim}` | Every read that returns `[]` for non-error reasons; distinguishes "no public knowledge on this topic" from "Apex unreachable" — load-bearing for §8 Q3 enforcement |
| `parallax_apex_empty_corpus_total` | Counter | (no high-cardinality labels) | First-read-per-process when the package directory is accessible but empty; lets ops dashboards detect stuck-empty deploys |
| `parallax_apex_lib_version_info` | Gauge (info-style, value=1) | `version={<aphelion __version__>}`, `min_version={<REQUIRED_APHELION_MIN_VERSION>}` | Emit once at startup after version assertion succeeds; lets dashboards detect version drift across the fleet |
| `parallax_apex_audit_write_failures_total` | Counter (M7-introduced; distinct from M5's `parallax_audit_write_failures_total`) | `cause={<sqlite error class>}` | The audit-write-during-read failure row in §4.3. Introduced as a separate metric instead of mutating M5's label set, because adding a `path` label to a counter that M5 may already emit without the label would create mixed-label series in Prometheus (Prom treats labeled vs unlabeled as distinct series → silent undercount on naive `sum()` queries). Dashboards alert on the union of M5's write-side and M7's read-side. |

**No-substitution enforcement**: the `parallax_apex_empty_result_total` counter is the load-bearing signal the §3.2 boundary needs. If a facade silently substitutes Perihelion results for an empty Apex result, the counter would still increment (Apex returned `[]`) and dashboards would show "Apex empty" alongside "Perihelion served" — making the substitution visible. This is why the counter is required even when the facade does the right thing: it makes the right behavior auditable and the wrong behavior visible.

---

## 5. Write Path (public layer only)

### 5.1 The only write surface

The Apex public layer's only legal write surface is the M6 `parallax ingest` CLI, already shipped in PR #58 squash `7434724` + PR #61 (spec doc) + PR #66 binary follow-ups → main-next `a978cff` (per M6 roadmap entry). M7 does **not** add a new write surface, does **not** expose a write API in the router, does **not** introduce any auto-ingest mechanism.

Concretely:

```
operator (Chris) places .aphelion.tar in PARALLAX_APHELION_PACKAGE_DIR
       │
       └─→ parallax ingest <path>  [manual CLI invocation per M6 Q-M6.1]
              │
              ├─→ manifest validation
              ├─→ signer verification (per M6 Q-M6.2 file-based trust store)
              ├─→ claim mapping extraction (per Aphelion v0.3 R1-R4 semantics)
              ├─→ per-package atomic audit.db write (per M6 PR #66 follow-up)
              └─→ sha256 manifest digest into audit row (M5 §3.1b audit row schema)
```

M7's responsibility on the write path is operational, not functional: ensure that once the M6 ingest CLI deploys to ZenBook (currently NOT deployed per M6 roadmap status), the Apex router serves freshly-ingested packages on next read. This is "no work" if the router reads `PARALLAX_APHELION_PACKAGE_DIR` on each request (current M5 design) — packages appear, router sees them, no router-side cache invalidation needed.

If M7 router introduces an in-process package index (LRU cache or precomputed key index for read-path latency), that cache MUST honor M6's "package appeared on disk" event. Concretely the M7 implementation PR (NOT this spec) must answer: (a) is the index rebuilt on every read? (b) on a clock tick? (c) on an inotify watcher? The brief defers this to M7 implementation — see §8 Q4.

### 5.2 audit.db unchanged

The audit.db schema, the `signer_manifest_digest` field, the 8 required + 3 optional columns, the closed-enum outcome, the namespaced `reason_code` — all carry forward unchanged from M5 §6 (`docs/m5-prep/audit-db-path-config.md`) and M6 (`docs/m6-prep/m6-ingest-contract-spec.md` Q-M6.2 + Q-M6.3 + per-package atomicity from PR #66).

### 5.3 sha256 manifest digest carry-forward

Per M5 envelope spec §8 Q1 (FROZEN 2026-05-09 PM, sha256-only REQUIRED no fallback), every audit row references the package by `signer_manifest_digest = sha256(manifest_bytes)`. M7 router code MUST surface this digest in any read response that exposes provenance (e.g., a future "show me where this claim came from" call). The exact serialization is implementation-detail for the M7 PR; the requirement is that the digest is reachable from a claim, not stripped during the read projection.

### 5.4 Write DoD does not apply to M7

The 2026-05-08 M7 DoD line "data_loss_events = 0 累積 10,000 write ops" was written assuming Apex owned a router-side write surface. After this reframe, the only write surface is `parallax ingest` (which is M6's responsibility) and the per-package audit.db write (which has its own atomicity guarantee shipped in PR #66). M7 does not introduce a new write operation, so this DoD line has nowhere to attach.

The data-loss guarantee for the public layer is still important — it just lives in M6 + M5 audit.db invariants, not in M7. The relocated home for the metric is:

- **Write-failure observability**: `parallax_audit_write_failures_total` (M5 §3.1a) — counts audit-row write failures on both write path (M6 ingest) and read path (M7 R-10 divergence telemetry, per §4.3 and §4.5).
- **Data-loss-events-zero assertion**: M5/M6 long-running observation track. The 10k-write-ops counting metric is a *derived* metric (cumulative writes since last `audit_write_failures_total` increment); if M5/M6 specs do not currently name this derived metric, that is a gap **upstream** of this M7 spec and SHOULD be raised before opening the M7 implementation PR. M7 inherits whatever name M5/M6 settle on; M7 does not introduce it.

The 2026-05-08 M7 DoD line "跨過一次完整 backup/restore 週期" reframes to: M4.5 S3 backup track owns backup; M7 inherits whatever posture M4.5 settles on (S3 bucket + `parallax-backup.timer/.service` per M4.5 entry on the roadmap). No new backup work in M7.

> **Caveat**: As of 2026-05-28, the M4.5 S3 backup track exists as a roadmap entry (carved out via 5/9 council vote) but has not yet been spec'd as a standalone document or shipped as code. The M7 entry condition that inherits M4.5 backup posture is therefore load-bearing on M4.5 actually shipping. If M4.5 is still vapor at M7 entry time, the M7 implementation PR may need a stop-gap backup runbook or escalate to Chris for a re-prioritization decision.

---

## 6. MEMORY.md Archive Timeline (per-purpose shrink, NOT wholesale `.bak`)

### 6.1 The 2026-05-08 framing problem

The 2026-05-08 M7 entry said "MEMORY.md archive 到 `.bak`" — implying a single atomic rename of the entire MEMORY.md + `*.md` corpus the day M7 lands. That framing fails the reconcile for three reasons:

1. **MEMORY.md belongs to the private layer**, which Apex does not own (per §3 boundary). Apex archiving it would be Apex reaching into Perihelion's territory.
2. **The shrink cadence is per-purpose**, not wholesale. Different prefix categories (`feedback_*`, `user_*`, `project_*`, `reference_*`) have different shrink readiness — `feedback_*` and `user_*` are already mapped to Perihelion ingestion in M6.5 P3 plan; `project_*` and `reference_*` decay naturally and don't all need active shrink.
3. **Pre-shrink hard precondition**: per `project_memory_housekeeping_system` (5/26 EVE SoT spec), each prefix file shrink requires `COUNT(claims) >= 1` distilled into Perihelion before the prefix becomes a pointer. A wholesale `.bak` doesn't honor this gating.

### 6.2 Reframed shrink timeline (under M6.5 P3 control, NOT M7)

M7 does **not** own the prefix shrink schedule. M7 only declares that the schedule exists and runs under M6.5 P3 / Memory Housekeeping System control. The phases below are reproduced from the M6.5 plan for cross-reference, not because M7 owns them:

| Phase | Scope | Trigger | Owner |
|---|---|---|---|
| **A** | shrink `feedback_*` + `user_*` (self-model body) | M6.5 P2 soak ≥ 2 weeks + Perihelion distill produces ≥1 claim per file + Chris ACK | M6.5 P3 (Chris-gated) |
| **B** | shrink `project_*` + `reference_*` | After M7 public-read router stable for ≥ 2 weeks + Apex pointer entries demonstrate retrieval works | M6.5 P3 (Chris-gated) |
| **C** | reduce MEMORY.md to pointer-only (1-line hook + Apex pointer + Perihelion pointer) | Phase A + Phase B complete + Perihelion v1.0+ shipped | M6.5 P3 (Chris-gated) + M11 entry signal |

**Important: each phase is Chris-gated.** None of them are M7 entry/exit conditions. M7 ships when the router ships; the shrink cadence continues afterward.

### 6.3 What M7 PR-time MEMORY.md looks like

When the M7 implementation PR lands, MEMORY.md is still LIVE prefix + `*.md` files. The Apex router does **not** read these files (boundary §3.2). The retrieve facade composes Apex + Perihelion + (transitionally) prefix; the prefix lives on as a fallback per the 2026-05-20 directive ("過渡期 prefix 仍是 active SoT").

Concretely: shipping M7 changes nothing in `~/.claude/projects/.../memory/`. The shrink work happens later, separately, under M6.5 P3.

### 6.4 Rollback story

If M7 lands and Apex router proves broken or unsatisfactory, the rollback is: stop reading Apex on the facade side (set a feature flag in the shim), retrieve degrades to prefix + Perihelion only, no data loss. Because M7 did not archive anything, there is nothing to restore from `.bak`.

This rollback simplicity is one of the reasons the reframe is *better* than the 2026-05-08 wording. The original "archive to `.bak`" framing introduced an irreversible state transition (atomic rename) at M7 ship time. The reframed version makes M7 ship a pure additive event.

---

## 7. Dependencies

### 7.1 Entry preconditions (all must hold before M7 implementation PR opens)

| # | Condition | Evidence |
|---|---|---|
| E.1 | **M5 GA active** — M4 canary @100% DoD signed off; M5 dual-write activates naturally per M5 roadmap entry | Roadmap M4/M5 row green; Chris ACK in `#m4-canary` (or successor channel) |
| E.2 | **M6 ingest pipeline deployed to ZenBook** | M6 code is already MERGED to main-next `a978cff` per M6 roadmap entry; deploy is the gating step — `parallax ingest --version` returns expected on ZenBook + at least one `.aphelion.tar` ingested into the live audit.db |
| E.3 | **Perihelion version where M6.5 D2 + D3 DoD met** — soak window ≥ 2 weeks with zero data-loss + injection-quality not regressing. As of 2026-05-28, Perihelion main is `5cbd9ca` (PR #13 P2 substrate merged); v0.7.0 is the latest tag; v0.7.5 is in PR; v0.7.6 (P2 application wiring + soak host) is queued behind v0.7.5. The exact tag that satisfies E.3 will be whichever version is in deploy when the soak clock reaches 14 days clean — likely v0.7.6 or later, but the spec gates on D2/D3 DoD evidence, not on a specific semver. | M6.5 D2 + D3 DoD met (per M6.5 roadmap row); Perihelion deployed + retrieve quality eval set passing; soak clock evidence in audit log |
| E.4 | **No M5 production rollback active or pending hysteresis** | Carry forward from M5 §2 E.5 — `RollbackController.state == RUNNING`, not `TRIPPED` / `AWAITING_ACK` |
| E.5 | **Public-read SLA preview met on M6 ingested corpus** — at least one synthetic load test demonstrates p99 < 100ms on Apex read path against a non-empty `PARALLAX_APHELION_PACKAGE_DIR` | Stress test artifact under `docs/m7-prep/` similar to M6 stress test (28650rps p99=0.057ms) |
| E.6 | **§3 boundary lint passes** — grep checks on M7 implementation branch return empty for Perihelion imports in router modules | Pre-merge CI hook OR manual reviewer checklist |

### 7.2 Soft dependencies (helpful but not blocking)

| # | Item | Why nice-to-have |
|---|---|---|
| S.1 | Aphelion v0.4 evidence schema (richer evidence binding) | M7 ships against v0.3 per M5/M6 ship-then-upgrade default; v0.4 adoption can wait for M8 or M9 |
| S.2 | M4.5 S3 backup track LIVE | Provides backup posture M7 inherits; but if M4.5 still in flight at M7 entry, M7 can ship with manual backup notes |
| S.3 | M6.5 P3 Phase A shrink complete | Lets M7 reviewers see fewer prefix entries during read-quality eval; not blocking |

### 7.3 Cross-dependency map

```
M5 GA ──────────────────┐
                        │
M6 ingest deployed ─────┼──→ M7 implementation PR opens
                        │
Perihelion v0.7.6+ ─────┘
                        │
M4.5 S3 backup LIVE ─── (soft)
                        │
                        └──→ M8 5k QPS pressure test (M7 inherits SLA preview)
                              │
                              └──→ M11 GA Primary (after M10 13 hard conditions)
```

### 7.4 ETA reference

Per the 2026-05-28 reconcile in `active.md`:

- **Public-read track (Apex router, this spec)**: 6-10 weeks conservative
- **Private track (Perihelion M6.5 P2 soak + P3 shrink)**: 4-6 weeks optimistic

The two tracks are independent and can run in parallel. M7 entry gates on E.1-E.6 above, not on the private track completing. The 6-10 week estimate is a Chris-set conservative figure based on M5/M6 ETAs and the unknowns in §8.

---

## 8. Open Questions (each with recommendation + Chris-gated note)

These questions are deliberately deferred from this spec to the M7 implementation PR (or to a Chris ACK before the PR opens, whichever Chris prefers).

### 8.1 Q1 — Apex router = HTTP service or in-process Python lib?

**Question**: Should the M7 Apex router be a long-running HTTP service (binding `parallax.chris-server.com/retrieve` or similar) OR an in-process Python module that callers `import` directly?

**Recommendation**: **In-process Python lib, with the retrieve facade shim providing an optional HTTP loopback wrapper if external clients need it.**

Rationale:
1. The retrieve facade shim is already pm2-managed and exposes loopback IPC for Claude Code hooks. Apex riding inside the shim (via in-process import) reuses that boundary and avoids a second network surface.
2. Aphelion is explicitly file-format + Python lib per the 2026-05-09 reframe (M5 §0); making Apex an HTTP service introduces an architectural mismatch — Apex would marshal Aphelion claims over HTTP for no value.
3. HTTP adds operational burden (TLS termination, auth, rate-limit) that M7 does not need for solo-dev / offline / single-host scenarios. The M9 OSS Surface Lock is when external SDK + reference router work happens; M7 is too early for that.
4. If a future external client genuinely needs HTTP access, the facade shim can expose a small loopback HTTP endpoint that wraps the in-process Apex calls. That wrapper is M9-shaped work, not M7.

**Chris-gated note**: this is the most consequential of the four questions. Chris should pin this before the M7 PR opens. If Chris prefers HTTP, the SLA §4.2 may need re-budgeting (HTTP overhead eats into the 100ms p99 budget by 10-20ms typical). The recommendation above is the council-style default; Chris can override.

### 8.2 Q2 — Aphelion package update frequency (daily / weekly cron)?

**Question**: How often does `PARALLAX_APHELION_PACKAGE_DIR` get new `.aphelion.tar` packages? Is M7 expected to keep up with daily ingest, weekly, ad-hoc, or one-shot bootstrap?

**Recommendation**: **Ad-hoc / manual, no cron in M7.**

Rationale:
1. M6 Q-M6.1 (route A) pinned ingest to manual CLI invocation. M7 should not introduce automated cadence behind that decision.
2. The M6 tradeoff acknowledgment explicitly said "if ingest volume exceeds ~10 packages/day, the operator burden becomes non-trivial. This is acceptable for M6 (solo dev, low ingest volume)" — same constraint applies in M7.
3. Adding a cron in M7 would be premature optimization. If Chris ingests 1-5 packages/week, manual is fine. If volume grows past 10/day, a watcher / cron is M8-or-later work (M8 owns operational maturity).
4. M7 router design must handle "package appeared on disk between two reads" correctly regardless of trigger — manual or cron. So the cron decision is decoupled from M7 correctness.

**Chris-gated note**: if Chris already has a planned weekly export → ingest pipeline (e.g., from Notion → Aphelion package), M7 should be told about it so the read-path cache (§5.1) can plan invalidation. The recommendation stands absent that signal.

### 8.3 Q3 — Public-read hit-miss fallback: Perihelion fallback or raise (per private boundary)?

**Question**: When Apex public-read returns an empty result for a query, should the caller (retrieve facade) substitute / fall back to a Perihelion query on the same key? OR should public-read empty be surfaced as empty with no cross-layer substitution?

**Recommendation**: **Surface the empty / error state without substituting Perihelion content. The facade composes both sides; it does not substitute.**

Concretely this means:

- **Empty Apex result** (non-error, public corpus simply has no match): facade still returns whatever Perihelion produced in its own slot of the composed result, but the Apex slot stays empty and is observable. The facade MUST emit `parallax_apex_empty_result_total{cause}` (per §4.5) so the empty-state is visible in dashboards; substituting Perihelion content into the Apex slot is forbidden.
- **Apex error** (`AphelionUnreachableError` / `AphelionAuditWriteError`): facade logs + surfaces the error to the upstream caller (Claude Code hook); does not silently swap Perihelion content into the Apex slot to hide the failure.

The word "raise" in earlier drafts of this question used a colloquial sense ("surface, do not hide"). The normative behavior is the §4.5 metric requirement plus the no-substitution rule above — not a Python `raise` per se.

Rationale:
1. §3.2 boundary 2 forbids Apex reading Perihelion. The facade is a separate component, so technically the facade could fall back across the boundary. But that would defeat the whole reconcile: it would re-merge the two layers from the consumer's perspective.
2. Public knowledge and private self-model claims have different semantic types. A query "what did I think about retrieval in May?" is private; a query "what does Aphelion v0.3 R1 say about polarity?" is public. Cross-layer substitution would produce semantically wrong results — the system would answer a public query with private content or vice versa.
3. The 2026-05-28 reconcile explicitly framed "雙軌獨立" (two independent tracks). Substitution violates "獨立".
4. Empty Apex result means the public corpus does not have a match. That is useful information for the caller; substituting hides it.
5. The `parallax_apex_empty_result_total` counter (§4.5) makes the no-substitution rule auditable: a facade that silently substitutes would still increment the counter (Apex genuinely returned `[]`), so dashboards would show "Apex empty" alongside "Perihelion served" — the substitution is visible to ops even if it sneaks past code review.

**Chris-gated note**: if Chris later wants a unified retrieve experience where the facade does opportunistic substitution, that is a *facade-side* decision (M6.5 P3 or a future M9 OSS surface), not Apex's. The recommendation above just refuses to bake substitution into M7 default behavior. Chris can override per-call via an explicit facade flag.

### 8.4 Q4 — Read-path package index: rebuilt-per-read, clock-tick, or inotify?

**Question**: To meet the §4.2 p99 < 100ms SLA, M7 router likely needs some in-process index of packages → claim keys to avoid scanning the entire `PARALLAX_APHELION_PACKAGE_DIR` on each query. How does that index stay fresh when M6 ingest drops a new package?

**Recommendation**: **Default to rebuilt-per-read for v0; add a clock-tick or watcher only if profiling shows it's necessary.**

Rationale:
1. Aphelion package read is local FS (single-digit-ms per M5 stress test). For solo-dev volumes (~10s of packages), a directory listing + manifest read on every query is well within the 100ms budget.
2. Per-read rebuild is the simplest correctness-preserving option — there is no cache invalidation logic to get wrong.
3. If profiling at M7 implementation time shows per-read rebuild eats > 30ms, M7 PR can add a clock-tick refresh (e.g., every 10s background thread re-scans the dir) as a small follow-up.
4. inotify (Linux) / ReadDirectoryChangesW (Windows) introduces daemon-lifecycle complexity that the M5/M6 burn-in stack already struggles with (per `project_messier_v4_v5_progress` 2026-05-16 sandbox bugs). Deferring this complexity to M8 is reasonable.

**Scaling assumption (load-bearing)**: the per-read recommendation assumes package count stays in the "10s of packages" range. If `PARALLAX_APHELION_PACKAGE_DIR` grows past ~100 packages (e.g., a wiki ingest produces one `.aphelion.tar` per top-level topic), the per-read directory scan + manifest read could exceed the 100ms p99 budget. M7 implementation PR MUST profile against the actual expected package count and document the chosen package-count ceiling for per-read mode. Past the ceiling, M7 (or M8 follow-up) moves to a refresh strategy.

**Stale-index window** (when M7 or M8 eventually moves off per-read): any clock-tick or inotify implementation MUST define behavior for the window between a new package landing on disk and the index seeing it. Queries during that window MUST NOT silently return stale-empty for keys present in the new package — the index either (a) rebuilds synchronously on cache miss before returning empty, or (b) emits an explicit `parallax_apex_index_staleness_seconds` gauge that ops can alert on. The spec does not prescribe which; the spec forbids the silent-stale-window path.

**Chris-gated note**: this is the lowest-stakes of the four questions for the v0 default but the highest-stakes for the scaling story. The M7 PR author should pick based on profiling data + the package-count ceiling. Chris can override but unlikely to need to.

### 8.5 Q5 — M7 PR split strategy

**Question**: Should M7 be a single PR (router + tests + observability + audit-row touchups) or split into 2-3 smaller PRs?

**Recommendation**: **2 PR split. PR-1 = router read API + integration tests; PR-2 = observability dashboards + alerts + stress test harness.**

Rationale:
1. M4 was split into 3 PRs per 5/9 council vote, lesson generalized: large PRs accumulate review fatigue and miss architectural CRITICAL findings (per `feedback_ralplan_audit_value`).
2. Router read API is the load-bearing slice; tests + observability are valuable but reviewable separately.
3. A 2 PR split also matches the M5/M6 cadence (M6 had spec PR + impl PR + binary follow-up PR).

**Chris-gated note**: this is purely process. If Chris prefers single PR (simpler ship coordination), that's also fine — the 2-split is a recommendation, not a requirement.

---

## 9. What this spec does NOT do (explicit, defensive)

These are listed so future readers (including LLM agents reading this in some session 6 weeks from now) do not infer commitments that aren't here:

- **Does not write a single line of code.** Spec-only, per brief.
- **Does not modify the M-roadmap doc.** That's a follow-up doc PR, brief explicitly says 不要動.
- **Does not touch parallax/ Python code.** Brief explicitly says 不要動.
- **Does not modify Aphelion repo or Perihelion repo files.** Brief explicitly says 不要動.
- **Does not commit to the M7 implementation start date.** That's Chris's call after E.1-E.6 hold.
- **Does not specify the router module path** (e.g., `parallax/apex/router.py` vs `parallax/router/apex.py`). That's an M7 implementation PR concern.
- **Does not modify MEMORY.md or any prefix file.** §6 explicitly defers shrink to M6.5 P3.
- **Does not bind M9 SDK / OSS Surface Lock decisions.** M7 ships well before M9.
- **Does not reconcile the M11 roadmap entry.** M11's current body ("Parallax 成為 Claude Code session continuity SSoT 永久取代 MEMORY.md" + "刪 MEMORY.md.bak 跑 7 天") inherits the same 2026-05-08 era Parallax-centric framing that this spec reframes for M7. After the 2026-05-28 reconcile, M11 also needs a wording pass — Parallax alone does not replace the private layer, and there is no `.bak` to delete because the per-purpose shrink schedule (§6) doesn't produce one. The M11 reconcile is **out-of-scope for this PR** but is flagged here so a future session can pick it up. M10 DoD that says "所有 M7 條件維持 30 天" will inherit whatever M7 conditions are defined by this spec, which is the desired ripple — M10 needs no separate reconcile.

---

## 10. Cross-reference index

For future readers tracing the lineage of this reframe:

- **Trigger** — `E:\Apex\vault\users\chris\active.md` 2026-05-28 MIDDAY framing reconcile (first headline)
- **Directive** — `feedback_perihelion_as_memory_layer` (2026-05-20 Chris directive)
- **Stack model** — `reference_memory_stack_architecture` (4-layer)
- **Roadmap ripple** — `messier-roadmap-v0-to-v11.md` M7 段 2026-05-22 ripple note
- **Singularity framing** — `project_singularity_cosmological_umbrella` (LOCKED 2026-05-07) + `active.md` Singularity section
- **Sibling spec — M6.5** — `messier-roadmap-v0-to-v11.md` M6.5 段 + `project_apex_m65_redesign` + xcouncil reset 2026-05-22 (Opus 4.7 + GPT 5.5 dual judge HIGH consensus)
- **M6 ingest route A** — `docs/m6-prep/m6-ingest-contract-spec.md` v0.1-frozen-2026-05-17 + roadmap M6 段
- **M5 spec** — `docs/m5-prep/apex-m5-entry-spec.md` v0.3.0-reframe (local-file adapter, signature verification, untar safety)
- **M5 audit row** — `docs/m5-prep/audit-db-path-config.md` §6
- **M5 envelope** — `docs/m5-prep/apex-m5-envelope-spec.md` v0.1-frozen-2026-05-09 (sha256-only)
- **Housekeeping spec** — `project_memory_housekeeping_system` (agent-config#4 squash `9c56941`)
- **Perihelion never-HTTP** — `reference_perihelion_naming`

---

## 11. Change log

| Date | Version | Change |
|---|---|---|
| 2026-05-28 | v0.1-reframe-2026-05-28 | Initial reframe. Reframes 2026-05-08 M7 entry per 2026-05-28 MIDDAY framing reconcile + 2026-05-20 Perihelion directive + 2026-05-22 roadmap ripple note. 8 sections + open questions + cross-refs. |
| 2026-05-28 | v0.1.1-reframe-2026-05-28 | Folded internal team review feedback (architect + critic + silent-failure-hunter, all 3 doc-review mode). Changes: §3.3 fix Aphelion API names to public surface (`unpacker.unpack` + `verifier.verify_package` + `validator.validate_signatures` + `read_adapter.AphelionReadAdapter`) + require startup version assertion. §4.1 diagram match. §4.3 split empty-corpus row into accessible-vs-inaccessible; add lib-version-mismatch row; add audit-write-during-read row mandating hard raise. §4.4 carry-forward P-A2' startup validation explicit. §4.5 NEW — 9 normative Prom-shaped metrics for M7 observability. §7.1 E.3 soften Perihelion version pin (gate on M6.5 D2+D3 DoD evidence, not on a specific semver). §8.3 Q3 clarify "raise" colloquial usage + require empty-result counter for no-substitution enforcement. §8.4 Q4 add package-count ceiling caveat + stale-index window prohibition. §9 add M11 staleness ripple note. §10 add date to xcouncil reset cross-ref. §5.4 expand data-loss metric relocation + M4.5 backup track caveat. Wording: §4.1 "in parallel" → "independently" (avoid implying concurrent execution model). §3.2 grep enforcement OS-neutral. |
