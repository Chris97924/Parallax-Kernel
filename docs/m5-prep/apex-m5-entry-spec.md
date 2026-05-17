# Apex M5 Entry Spec — Aphelion-format Local Read Adapter

> **Version**: v0.3.0-reframe
> **Date**: 2026-05-09
> **Status**: SUPERSEDES `docs/m5-prep/m5-entry-spec.md` (PR #46 merged 2026-05-08, kept as historical artifact, see `_DEPRECATED.md` sidecar)
> **Author**: ralph follow-up after 2026-05-09 Aphelion roadmap re-read + Chris naming flip-back to Apex+M
> **Upstream refs**:
> - `docs/m4-prep/us-009-acceptance-criteria.md` §2, §7 (M4 entry)
> - `docs/m4-prep/canary-stage-runbook.md` (M4 stage gates)
> - Aphelion canonical roadmap (Notion `347f3661...` "Aphelion v0.2 → v1.0 Roadmap (SSoT)") §TL;DR + §Scope Cut
> - `E:\Parallax\vault\users\chris\wiki\entities\messier.md` (Apex product hub; filename retained, content alias=Apex)
> - `E:\Parallax\vault\users\chris\wiki\derived\strategy\apex-m5-envelope-mvp-decisions.md` (xcouncil 2026-05-09 + Option α reconcile — adapter=B, audit_db_path, payload_type, envelope 歸 Apex)

---

## 0. Why this spec rewrites the prior one

The prior spec `docs/m5-prep/m5-entry-spec.md` (PR #46) framed M5 as wiring `AphelionReadAdapter` to a real **HTTP service** — assumed Aphelion v0.6 would ship a retrieval API. That framing is wrong. Per the canonical Aphelion roadmap (Notion `347f3661...`):

> **Aphelion = Declarative Package** — 知識包可攜檔案格式（claim + evidence + event log，canonical pack）。
> **Scope Cut（永久不做）**: GraphQL / REST API layer（Aphelion 是檔案格式）.

Aphelion is a **file format + Python library**, not a service. There is no HTTP endpoint, no staging gate, no envelope contract on Aphelion side. The "graphiti-like behavior" the fusion produces emerges from **Parallax + Aphelion files together** — Parallax reads `.aphelion` packages locally, parses claim + evidence + event chain via `aphelion` Python lib (v0.5+), then arbitrates against its own SQLite store.

This spec restates the M5 entry conditions in line with that architecture. PR #46 spec is preserved as a historical record of the wrong direction we briefly went down (`_DEPRECATED.md` sidecar in same dir points here).

## 1. Purpose

Define entry conditions, scope, and out-of-scope boundary for **Apex M5 (Full Dual-Write)**, so M5 work can begin once M4 DoD signs off without renegotiating scope.

M5 replaces the M3-era null-stub `AphelionReadAdapter` (`raise AphelionUnreachableError("not_implemented")`) with a **local-file reader** that calls into the `aphelion` Python lib to load `.aphelion` packages from a configured store path, validates signatures (per Aphelion v0.5 signer/verifier), and returns claim/evidence data for dual-read arbitration.

**Audience**: Chris (拍板者), implementation owner, reviewer.

## 2. Entry Preconditions (all must hold before M5 PR opens)

| # | Condition | Evidence |
|---|---|---|
| E.1 | M4 DoD signed off — Stage @1% / @10% / @50% / @100% all green per `canary-stage-runbook.md` §3–§6 | DoD verifier JSON `overall: pass` at each stage promotion gate, scoped to that stage's runbook observation window: @1% = 24h, @10% = 48h, @50% = 72h, @100% = 1 week; Chris ACK in `#m4-canary` for each promotion |
| E.2 | M4 P×A Dashboard row ticked | Notion `343f3661...` row checkboxes all set |
| E.3 | M3 dual-read corpus stayed green throughout M4 rollout — `dual_read_discrepancy_rate < 0.1%` continuously for **≥ 72h covering each M4 stage transition** | `dual_read_continuity_check --since=72h --metric=discrepancy` exit 0 at every Stage @1%/@10%/@50%/@100% promotion gate |
| E.4 | **Aphelion v0.3 R1–R4 spec frozen** (separate Aphelion repo deliverable) — confidence semantics (R1) + valid-time (R2) + polarity (R3) + supersedes + conflict taxonomy (R4) | ✅ **DONE 2026-05-09** — `aphelion-graph/spec/v0.3-claim-semantics.md` v0.3.0-r1r4 + `aphelion-graph/adr/0002-v0.3-claim-semantics-r1r4.md` (Chris-pinned via xcouncil 8-model consensus). Validator+reader code is a separate Aphelion-Graph PR (M5 implementation phase, NOT entry gate). |
| E.5 | No M4 production rollback active or pending hysteresis | `RollbackController.state` returns `CanaryState.RUNNING`, not `TRIPPED` / `AWAITING_ACK`. CLI inspection: `parallax canary --check-state` |
| E.6 | Fresh rollback-path drill against the M5 branch HEAD passes | `parallax canary --rollback-drill --dry-run --format json` exits 0 with all 3 drills `overall: pass` |
| P-A1' | Aphelion v0.5+ `aphelion` Python lib API stable for reader (`read_package` / `validate_signatures` / replay) | `pip install aphelion==<version>` works; `import aphelion; aphelion.read_package(path)` returns claim list per spec §S2 + §S3 |
| P-A2' | Aphelion package store path configured on Parallax host | `PARALLAX_APHELION_PACKAGE_DIR` env var set; dir exists with read perms; at least 1 `.aphelion` test package present |
| P-A3 | rename-only PR #47 (`aphelion_stub.py` → `aphelion_adapter.py`) merged into main-next | git log shows PR #47 squash commit on main-next |

**Removed from prior spec (no longer applicable):**

- ~~Aphelion staging endpoint 48h ≥99.5% / p95 ≤150ms~~ — Aphelion has no service to deploy
- ~~Aphelion v0.6 HTTP envelope spec frozen~~ — name was a misattribution; the wire-format envelope is Apex's responsibility (see §3.1b "Apex M5 envelope MVP"), not Aphelion's
- ~~Aphelion retrieval API spec~~ — no such API exists by design

> **✅ Reconcile RESOLVED 2026-05-09 (Option α)**: The "v0.6 envelope" from xcouncil is **Apex M5 envelope MVP** (Parallax-side wire format, owned by Parallax-Kernel repo). Aphelion-side ask is **v0.3 R1-R4** (claim semantics per Aphelion canonical roadmap). Two separate artifacts, two owners, parallel paths. See `apex-m5-envelope-mvp-decisions.md`.

## 3. Scope — What M5 Owns

### 3.1 In-scope code changes

| Module | Change | Notes |
|---|---|---|
| `parallax/router/aphelion_adapter.py` (renamed from `aphelion_stub.py` via PR #47) | Replace `query()` raise-only body with `aphelion`-lib package read + claim filter + evidence return | Keep `AphelionUnreachableError` for genuine I/O failures (file missing, package corrupt, signer fail) |
| `parallax/router/dual_read.py` | Allow non-stub `secondary` adapter; preserve dual-read semantics + arbitration | US-009 §7 O.2 explicitly held this back to M5 |
| `parallax/config.py` | Add `aphelion_package_dir: Path` config field; load from `PARALLAX_APHELION_PACKAGE_DIR` env | Optional with validate-on-startup; fail fast if dir missing on M5 deployments |
| `tests/router/` | New tests covering local-file adapter path + failure modes (missing dir, corrupt package, unsigned package, expired claim) | Keep existing dual-read test expectations green per US-009 §7 O.2 |

**OQ3 rename strategy** (per prior spec, still applies): PR #47 is a standalone mechanical rename PR ahead of any implementation change. PR #46 / this spec do not carry rename diff.

### 3.1a Security Invariants (rewritten for file-system path)

The HTTP-era invariants (TLS / Bearer token / rate-limit headers / Aphelion auth header redaction) are **not applicable** — there is no network surface. New invariants for file-read path:

| Invariant | Rule |
|---|---|
| Package dir path validation | `aphelion_package_dir` MUST be absolute path; reject relative paths and `..` segments at config load time |
| Untar safety | Aphelion v0.2 §S2.5 untar-safety rules are mandatory (reject abs path / `..` / symlink / hardlink / device; entry size ≤ 64 MiB; total ≤ 256 MiB; entry count ≤ 1024). Parallax MUST surface these as `AphelionUnreachableError(reason="unsafe_archive")` |
| Signer verification | All packages read by adapter MUST be signature-verified per Aphelion v0.5 §1–§4. Unsigned packages → `AphelionUnreachableError(reason="unsigned_package")`. No `--require-signed=false` escape hatch in M5 |
| Audit `package_id` only | Audit ledger captures `package_id` + `claim_id` + signer manifest digest; **never** raw package body or filesystem path inside container |
| Audit-write failure observability | `parallax_audit_write_failures_total` Prom counter; alert > 0 / 5 min (carries forward unchanged from prior spec) |
| Invalidate event session isolation | Cache invalidate events for M5 dual-read MUST not leak across `session_id` boundaries (carries forward unchanged) |
| `audit.db` path discipline | Windows dev: `E:\Parallax\data\audit.db` (per Chris E-drive rule, `feedback_e_drive_data.md`). Linux ZenBook: `/home/chris/parallax-kernel/db/audit.db`. Two environments, two paths, no conflict. |

### 3.1b Upstream Assumptions (deferred — non-blocking for M5)

| Assumption | Rationale | Owner / Target |
|---|---|---|
| Aphelion v0.3 R5 manifest vs claim.md arbitration | Already implicit via existing `aphelion-graph/spec/claim-frontmatter.md` Rule 5 ("on conflict manifest wins, ERR-SEM-020"). v0.3 may add explicit error-code surfacing; non-blocking for M5. Parallax already pins to manifest-as-truth, no fallback needed. |
| Aphelion v0.4 evidence schema (role, capture_ts, source_uri, excerpt_range, original_hash) | Richer evidence binding; M5 can use v0.3 minimal evidence for now | Aphelion v0.4; M5 ships before this without functional regression |
| Apex M5 envelope MVP (Parallax-side wire format, per xcouncil 2026-05-09 + Option α reconcile) | Wire format wraps reads/writes between Apex adapter and `.aphelion` files in dir; payload_type={query_result, event}; checksum sha256; envelope_version="0.1" + schema_version + message_id + audit_db_ref (sha256-only). Spec at `docs/m5-prep/apex-m5-envelope-spec.md` v0.1-frozen-2026-05-09 | **FROZEN 2026-05-09** via xcouncil consensus (§8 Q1 → sha256-only) |
| Aphelion v0.3 R1-R4 claim semantics (claim-side, NOT envelope) | Spec at `aphelion-graph/spec/v0.3-claim-semantics.md` v0.3.0-r1r4; ADR `aphelion-graph/adr/0002-v0.3-claim-semantics-r1r4.md`; conditional `subject`-required-when-R4; lenient cross-package `supersedes`; 14 new `PX_E_4101..4144` + 1 `PX_W_4151` error codes | **SPEC FROZEN 2026-05-09** via xcouncil consensus (5/8 majority on each §9 unresolved item); **VALIDATOR + READER CODE PENDING** as separate Aphelion-Graph PR, Chris-gated |
| Audit DB path config | `PARALLAX_AUDIT_DB_PATH` env var REQUIRED (no implicit default — server refuses to start if unset/empty); suggested paths in `.env.example` (Windows `E:\Parallax\data\audit.db`, Linux ZenBook `/home/chris/parallax-kernel/db/audit.db`). Validation at startup via 7 gates; schema auto-applied on `open_audit_db()` via `CREATE TABLE IF NOT EXISTS` (no external migration). | **CONFIGURED 2026-05-09** via `docs/m5-prep/audit-db-path-config.md`; **ZenBook DEPLOYED 2026-05-12 night** (env file + mkdir + systemctl restart per spec §5.2 + §9); Windows dev `mkdir E:\Parallax\data\` + `$env:PARALLAX_AUDIT_DB_PATH=...` per spec §9 still pending |
| M4 GATE 2A traffic gap resolution | Hybrid synthetic loader (1 qps, `traffic_source="synthetic"` label) starts B1/B2 metric series; cutover to natural traffic via Phase-1 (clock) + Phase-2 (semantic-natural) DoD split. Spec at `docs/m4-prep/traffic-gap-resolution.md`. | **DESIGN FROZEN 2026-05-09** via xcouncil consensus (6/8 hybrid majority); **CODE LANDING PENDING** (items 4.2-4.9) as small follow-up PR |

### 3.1c Router Invariants (Real Adapter Path)

Per OQ4 + OQ5 council resolution (carried forward from prior spec). R-9 cache invalidate batching dropped because no network round-trip to batch.

| # | Invariant | Rationale |
|---|---|---|
| R-7 | **Session-scoped arbitration monotonicity** — within a single request lifecycle for a given `session_id`, the arbitration source (cache vs Aphelion) must be monotonic. Once the request triggers an Aphelion fetch, all subsequent same-session same-key lookups in that request route to the fresh path until the invalidate batch window flushes | Mitigates state-tearing risk |
| R-8 | **Aphelion-wins arbitration with divergence budget** — when local cache and Aphelion package responses disagree, Aphelion wins and local cache is invalidated. **Divergence rate ≤2% per 1h sliding window**; exceeding this auto-trips circuit-breaker into stub fallback | Aphelion = retrieval SoT per fusion contract |
| R-10 | **Divergence telemetry** — every Aphelion-overrides-cache event MUST write an audit-ledger row containing `{event_id, session_id, key, local_hash, aphelion_hash, package_id, signer_id, reason_code, ts}`. The hashes are content hashes per Aphelion §S1.2, not raw payloads | Closes M6 burn-in observability gap |

**Latency tier (Chris-pinned 2026-05-07, carried forward + relaxed for local I/O)**:
- cache hit p95 ≤100ms (unchanged)
- miss-and-fetch p95 ≤150ms (was ≤250ms for HTTP; tightened because local file read is ~10x faster than network)

### 3.2 In-scope ops

- New retrieval-side observability: `aphelion_package_read_latency_ms` (p50/p99), `aphelion_package_read_total` (success counter), `aphelion_package_read_errors_total{reason}` (replaces `aphelion_unreachable_total`), `retrieval_evidence_size_bytes`
- Update Grafana dashboard with retrieval panels (separate from M4 canary dashboard)
- `parallax canary --check-state` CLI inspection mode (already specced in prior spec §3.2; keep)

### 3.3 In-scope docs

- `docs/m5-prep/apex-m5-aphelion-adapter-design.md` — local-file adapter contract, retry policy (none — fail fast), error taxonomy
- `docs/adr/00XX-apex-m5-aphelion-local-reader.md` — ADR for the file-format-vs-service decision
- `docs/router/` updates if dual-read arbitration semantics shift

### 3.4 Canary Infrastructure — Reuse vs Redefinition

Per OQ1 council resolution, M5 reuses M4 canary infrastructure partially. CanaryRolloutDriver abstraction is **not** introduced in M5 (YAGNI for two callers).

## 4. Out of Scope (explicit)

- **No HTTP client for Aphelion** — file format only, period
- **No retry/backoff network policy** — local file reads are deterministic; either succeed or `AphelionUnreachableError`
- **No staging endpoint health gate** — there is no service to monitor
- **No Aphelion-side authentication / authz** — file-system perms govern access
- **No package-fetch logic** — packages must already be on local FS via separate ingest pipeline (out-of-scope for Parallax M5)
- **No package validation beyond Aphelion v0.5 signer + v0.2 untar safety** — those are sufficient

## 5. Done Definition (DoD)

M5 is complete when:

1. `AphelionReadAdapter.query()` reads `.aphelion` packages from `PARALLAX_APHELION_PACKAGE_DIR`, validates signatures, returns claim list — replacing raise-only stub
2. Dual-read path works end-to-end: query → primary (Parallax store) + secondary (Aphelion package read) → arbitrate per R-8 → return
3. R-7, R-8, R-10 invariants enforced + tested
4. `parallax_dual_read_discrepancy_rate` reports real values (not stub-fixed 0.0)
5. Audit ledger captures divergence rows per R-10
6. p95 latency tier targets met (cache hit ≤100ms, miss-and-fetch ≤150ms)
7. Test coverage ≥80% on new adapter path (unit + integration)

## 6. Estimated Timeline (relative to M4 DoD)

M5 entry possible the moment M4 DoD signs off + Aphelion v0.3 R1–R4 frozen. Without HTTP staging burn-in, M5 entry is **gated only on Aphelion v0.3 spec completion** (Chris-driven via xcouncil session) + M4 4-stage canary completion.

```
Earliest case (everything aligns):
  M4 GATE 2 ACK ──► 13d 4-stage canary ──► M4 DoD ✅ ──► M5 entry
                                                          AND
  Chris xcouncil session ──► Aphelion v0.3 R1-R4 spec ──┘

M5 entry ETA = max(M4 DoD date, Aphelion v0.3 spec ready)

Likely 2026-06-04 ~ 2026-06-22 depending on:
  - M4 burn-in clock + canary cadence
  - Chris's xcouncil session timing for Aphelion v0.3
  - Whether any M4 stage triggers rollback / hysteresis
```

## 7. Open Questions

1. ~~v0.6 envelope ⟷ v0.3 R1-R4 reconcile~~ — ✅ RESOLVED 2026-05-09 (Option α): Apex M5 envelope MVP belongs to Parallax-Kernel repo; Aphelion v0.3 R1-R4 stays in Aphelion-Graph#7. Two separate artifacts.
2. **Aphelion v0.3 R5 (manifest vs claim.md arbitration)** — if not ready by M5 start, Parallax pins to manifest. Confirm with Aphelion-side timing.
3. **`PARALLAX_APHELION_PACKAGE_DIR` ingest pipeline** — ✅ **RESOLVED 2026-05-17** in `docs/m6-prep/m6-ingest-contract-spec.md` (v0.1-frozen-2026-05-17). Decision: manual CLI invocation (`parallax ingest <package_path>`) per Q-M6.1 — operator places `.aphelion.tar` into `PARALLAX_APHELION_PACKAGE_DIR` and invokes the CLI; no watcher / poll / daemon in M6. Out-of-scope for M5 itself; M6 ingest impl lands the CLI subcommand.
4. **Dual-write conflict back to package** — M5 entry writes back to Parallax event log on conflict. Does M5 also write back to a NEW `.aphelion` package, or only to Parallax store? (Notion canonical roadmap M5 says "Lane C US-010 衝突回寫 Parallax event" — so Parallax-side only; Aphelion stays read-only in M5.)
5. **Aphelion v0.4 evidence schema** — M5 uses v0.3 minimal evidence. If v0.4 lands during M5 implementation, do we delay or ship-then-upgrade? Default: ship-then-upgrade.
6. **Signer key distribution** — how does ZenBook get the verifier public key(s) to validate signatures? Chris owns this op decision.

## 8. Migration from prior PR #46 spec

| Prior section | Status |
|---|---|
| §1 Purpose | Same intent; rewritten with file-format framing |
| §2 E.1–E.6 | E.1, E.2, E.3, E.5, E.6 carried forward; E.4 corrected to v0.3 R1-R4 (separate from xcouncil's Apex M5 envelope MVP per Option α reconcile) |
| §2 P-A1, P-A2, P-A3 | P-A1 / P-A2 dropped (no Aphelion service); P-A3 unchanged; new P-A1' (lib API stable) + P-A2' (package dir configured) |
| §3.1 in-scope code | Reframed: local-file reader instead of HTTP client |
| §3.1a security invariants | Replaced (HTTP → file-system) — most prior items dropped; added audit.db path discipline |
| §3.1b deferred upstream | Reframed (different deferred items); added Apex M5 envelope MVP item (Parallax-Kernel-owned, separate from Aphelion v0.3 R1-R4) |
| §3.1c R-7, R-8, R-10 | Carried forward; R-9 dropped (no network batching) |
| §3.2, §3.3, §3.4 | Mostly carried forward; doc paths use `apex-m5-` prefix |
| §4 Out-of-Scope | Expanded with no-HTTP / no-retry-policy explicit calls |
| §5 DoD | Reformulated for file-read semantics |
| §7 Open Questions | New set; OQ1–OQ5 resolved or moved to §3.1c R-7~R-10; new OQ1 = v0.6 vs v0.3 reconcile |

PR #46 spec preserved at `docs/m5-prep/m5-entry-spec.md` (sidecar `_DEPRECATED.md` points here, NOT deleted).
