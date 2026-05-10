# Apex M6 Readiness Checklist

**Status:** Drafted (autopilot 2026-05-09 PM, post-xcouncil consensus)
**Date:** 2026-05-09
**Owner:** Parallax-Kernel
**Purpose:** Track M5→M6 transition prep so Chris can resume from a known state without re-deriving context.

> **Per Chris's autopilot directive (2026-05-09 pre-out)**: "除了 DoD 可以滾動到可以進行 M6 的準備狀態" — DoD itself stays Chris-gated, but M6 prep work that has no DoD dependency rolls forward now.

---

## 1. M5→M6 narrative

M5 = Full Dual-Write. AphelionReadAdapter actually reads `.aphelion` packages from local file store, dual-read router arbitrates against Parallax SQLite, divergence is logged + R-10 audit row written. M5 is read-side fidelity, write-side stays Parallax-only.

M6 = 主記憶 Dogfood (M1-arch model per `_naming-changelog.md`). The Parallax instance starts dog-fooding its own dual-read for self-memory: agent context queries route through dual-read, divergence informs which side wins.

The handoff M5→M6 is fundamentally about **trust**: M6 only makes sense if M5's R-8 invariant ("Aphelion-wins arbitration with divergence budget ≤2%") has held under real load. M6 cannot start cleanly unless M5 has shipped + observed.

## 2. Spec-level freezes done 2026-05-09 (M6 unblockers)

> **Path note**: paths below are repo-relative pointers under the `E:\Workspace\` root, NOT clickable cross-repo links. `aphelion-graph/...` lives in a sibling repo; `Parallax/docs/...` is this repo's `docs/`. Treat them as text references — for click-through, open the file directly in your editor.

| Item | Status | Where |
|---|---|---|
| Aphelion v0.3 R1-R4 claim semantics | ✅ FROZEN | `aphelion-graph/spec/v0.3-claim-semantics.md` |
| Apex M5 envelope MVP (sha256-only) | ✅ FROZEN | `Parallax/docs/m5-prep/apex-m5-envelope-spec.md` v0.1-frozen-2026-05-09 |
| Audit DB path config | ✅ NORMATIVE | `Parallax/docs/m5-prep/audit-db-path-config.md` |
| Traffic gap resolution (hybrid loader) | ✅ DESIGN FROZEN | `Parallax/docs/m4-prep/traffic-gap-resolution.md` |

## 3. M6-specific prep that can roll forward NOW (no M4 DoD dependency)

These are docs/specs that benefit from being in place when M5 ships, so M6 doesn't bottleneck on design at handoff time. None of them touch production code.

### 3.1 M6 entry preconditions (draft outline — Chris to refine when M5 lands)

- E.M5.1 — M5 DoD signed (R-7, R-8, R-10 all enforced + tested in production)
- E.M5.2 — `parallax_dual_read_discrepancy_rate` natural-traffic stable < 0.5% for ≥ 7 days
- E.M5.3 — Aphelion v0.3 R1-R4 validator + reader code merged + Apex consumed it
- E.M5.4 — `parallax-burn-in-synth-loader` cleanly disabled (`systemctl --user disable`, post-cutover Phase 2)
- E.M6.1 — Self-memory dual-read scope decision (which agent context queries route through dual-read?)
- E.M6.2 — Memory layer ingest pipeline ready (`PARALLAX_APHELION_PACKAGE_DIR` populated by SOMETHING — currently a Chris-gated open question per `apex-m5-entry-spec.md` §7.3)

### 3.2 M6 design questions to flag now (not answer)

- **Q-M6.1**: When the Parallax instance dog-foods its own memory, does the agent's context query bypass cache or always go through dual-read? (latency vs fidelity tradeoff)
- **Q-M6.2**: How does R-8 divergence-budget circuit-breaker behave when triggered on agent's own self-memory? (rollback to Parallax-only is safe but loses M6's dog-fooding intent)
- **Q-M6.3**: Aphelion v0.4 evidence schema (richer evidence binding) — ship-then-upgrade or block M6 until v0.4 lands?
- **Q-M6.4**: Cross-instance memory federation (per `feedback_workspace_cwd_convention.md` direction) — is M6 still single-instance, or does it open the federation door?

These are NOT for autopilot to answer. They are Chris-direction calls for when M5 metric data informs the answers.

### 3.3 Cross-references that will be needed at M6 entry

- `apex-m5-entry-spec.md` §3.1c R-7, R-8, R-10 — invariants M6 inherits and tightens
- `aphelion-graph/spec/v0.3-claim-semantics.md` — claim semantics M6 dog-foods
- `feedback_workspace_cwd_convention.md` — vault layout and memory dir conventions
- Apex canonical roadmap (Notion `347f3661...`) §M6 — long-form M6 description

## 4. M6 readiness state — TODO list seed

When Chris resumes, this is the punch list to break into PRs:

```
M5 prep (Chris-action, today/tomorrow):
  [ ] mkdir + env file + systemd restart per audit-db-path-config.md §5
  [x] Confirm canonical audit-row schema — DONE 2026-05-09 PM (audit-db-path-config.md §6 NORMATIVE; OQs resolved in §6.5)
  [x] Confirm `confidence` carve-out from R4-trigger list — DONE 2026-05-09 PM (v0.3-claim-semantics.md §6.5 carve-out section, ADR-0002 final list of 4 trigger fields)
  [ ] v0.4 → v0.3 backward-compat fixture: round-trip an existing v0.4 producer's `.aphelion` package through the v0.3 validator and assert zero new errors. Without this fixture, the "additive only" claim in v0.3-claim-semantics.md §1 is theoretical.
  [ ] Aphelion-Graph#7 issue body re-paste from spec §1-§6
  [ ] Aphelion validator + reader code PR (UNBLOCKED: spec + carve-out both confirmed)
  [ ] Apex M5 envelope encoder/decoder + audit writer code PR (UNBLOCKED: audit-row schema confirmed)
  [ ] Traffic gap items 4.2-4.9 (label + loader + systemd + DoD split)
  [ ] M4 GATE 3-7 (Chris-driven canary push)

M5 ship (post-burn-in):
  [ ] M5 PR opens (entry conditions all green)
  [ ] M5 implementation + tests
  [ ] M5 DoD per apex-m5-entry-spec.md §5

M6 prep (after M5 ships, before M6 PR):
  [ ] M6 entry preconditions doc (use §3.1 as outline)
  [ ] Q-M6.1..Q-M6.4 design discussion
  [ ] Memory layer ingest pipeline (P-A2' resolution)
  [ ] M6 entry spec doc
```

## 5. What autopilot did NOT do (and won't, until Chris is back)

- Did NOT touch any production code (`parallax/router/*`, `parallax/canary/*`, etc.)
- Did NOT modify any merged PR or open any new PR
- Did NOT push any commits (working-tree edits only; Chris reviews before commit)
- Did NOT sign off M4 DoD (per directive)
- Did NOT escalate any GATE 3-7 step (Chris-gated)
- Did NOT call any external API beyond xcouncil (one autopilot-internal multi-model design call)

## 6. Audit trail

- xcouncil verdict trail: `C:\Users\user\AppData\Local\Temp\xcouncil_opus_prompt.txt` (session-local)
- Spec docs landed (working-tree, not committed):
  - `aphelion-graph/spec/v0.3-claim-semantics.md` (new)
  - `aphelion-graph/spec/error-codes.md` (PX_E_4101..4144 + PX_W_4151 added)
  - `aphelion-graph/adr/0002-v0.3-claim-semantics-r1r4.md` (new)
  - `aphelion-graph/spec/claim-frontmatter.md` (Rules §6 + v0.3-r1r4 fields table)
  - `Parallax/docs/m5-prep/apex-m5-envelope-spec.md` (frontmatter + §2 audit_db_ref + §8 resolution log)
  - `Parallax/docs/m5-prep/apex-m5-entry-spec.md` (§2 E.4 + §3.1b 4 rows)
  - `Parallax/docs/m5-prep/audit-db-path-config.md` (new)
  - `Parallax/docs/m4-prep/traffic-gap-resolution.md` (new)
  - `Parallax/docs/m6-prep/m6-readiness-checklist.md` (this file, new)
- Memory snapshots updated:
  - `project_aphelion_v03_spec.md` (rename + body rewrite for FROZEN state)
  - `project_messier_v4_v5_progress.md` (PM section + Chris-action list)
