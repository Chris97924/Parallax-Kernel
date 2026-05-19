---
title: Apex M6 Ingest Pipeline — Implementation Spec
status: living
version: v0.1
date: 2026-05-19
owner: Parallax-Kernel
implements:
  - docs/m6-prep/m6-ingest-contract-spec.md (v0.1-frozen-2026-05-17)
code:
  - parallax/apex/aphelion_ingest.py
  - parallax/cli.py (ingest subcommand)
  - scripts/m6_audit_db_stress.py
landed_in:
  - PR #58 (commit 7434724, main-next, 2026-05-17)
consistency_test:
  - tests/docs/test_m6_spec_consistency.py
---

# Apex M6 Ingest Pipeline — Implementation Spec

> **Scope**: This doc is the *implementation-level* spec for the code that landed in PR #58. The *contract* spec at [docs/m6-prep/m6-ingest-contract-spec.md](../m6-prep/m6-ingest-contract-spec.md) (v0.1-frozen-2026-05-17) is the normative wire / reason-code / decision contract; this doc records the concrete CLI surface, env-var contract, error inventory, outcome set, performance baseline, and post-ship gaps as actually shipped — reverse-engineered from `parallax/apex/aphelion_ingest.py` + `parallax/cli.py`. The contract spec freezes the *wire*; this spec freezes the *binary*.

---

## §1 Overview — M6 in the Apex M-progress

### 1.1 Position in the Apex roadmap

| Milestone | Status | What it gave us |
|---|---|---|
| M3 | ✅ shipped | DualReadRouter + 5-outcome decision log (`hit` / `miss` / `divergence` / `error` / `skipped`) |
| M4 | ✅ shipped | Canary shadow observation overlay (GATE 3-7) |
| M5 | ✅ shipped | `audit_writer` contract + `audit.db` persistence wired into `AphelionReadAdapter` (PR #55) |
| **M6** | **✅ shipped (PR #58)** | **Route-A canonical ingest: `.aphelion.tar` → claim mapping → `audit_row` chain** |
| M6.5 / M7 | future | self-memory dual-read consumer (downstream of this pipeline); watcher/glob ergonomics |
| M11+ | future | federation / cross-instance package distribution |

### 1.2 Relationship to the contract spec

The contract spec (v0.1-frozen-2026-05-17) decided the *wire*:

| Contract §  | Decided | Implemented at |
|---|---|---|
| §1 Q-M6.1 | manual CLI invocation only (no watcher / no poll) | `parallax/cli.py` ingest subparser |
| §1 Q-M6.2 | file-based PEM trust store, re-read per invocation | `aphelion_ingest._load_trust_store` |
| §1 Q-M6.3 | ship-then-upgrade vs Aphelion v0.4 | `aphelion_ingest._read_claim_frontmatter` uses v0.3 validator |
| §1 Q-M6.4 | strictly single-instance (no federation) | `aphelion_ingest._validate_package_path` resolves under one `PARALLAX_APHELION_PACKAGE_DIR` |
| §2 verifier integration | `verify_package(require_signed=True, require_notary=False)` | `aphelion_ingest._run_aphelion_verify` |
| §3 trust store | per-envelope `key_fingerprint` lookup against `*.pem` byte-payload hashes | `aphelion_ingest._verify_trust` |
| §4 claim mapping | one audit row per claim in `manifest["claims"]` | `aphelion_ingest._build_audit_rows` |
| §5 path resolver | 6 gates pre-`verify_package`, exit 65/78 per failure mode | `aphelion_ingest._validate_dir_arg` + `_validate_package_path` |
| §6 reason-code namespace | closed set under `pkg.*` / `signer.*` / `claim.*` / `disk.*` | `aphelion_ingest._REASON_TO_EXIT` |

### 1.3 What this spec adds on top of the contract

The contract spec stops at the wire level. The following are implementation-level facts that operators and reviewers need but that didn't land in the contract:

- The exact CLI surface (positional + flags) — §2
- The exact env-var contract (names, defaults, error mapping) — §3
- The complete `ParallaxIngestError` reason_code closed set as shipped — §4
- Outcomes / states the audit_row can carry from ingest — §5
- Measured performance baseline + single-threaded caveat — §6
- Cross-link to the operator runbook — §7
- Whether the four contract-level decisions made it into the binary as-decided — §8
- Known follow-ups (post-merge spec gaps + headroom caveats) — §9

---

## §2 CLI surface

The `parallax ingest` subcommand is registered in `parallax/cli.py` and accepts exactly one positional + two optional flags. Subcommand semantics are single-invocation, atomic, idempotent-per-UUID (see §5).

### 2.1 Argument table

| Name | Kind | Type | Default | Code reference | Behaviour |
|---|---|---|---|---|---|
| `package_path` | positional, required | `pathlib.Path` | — | [parallax/cli.py:319-323](../../parallax/cli.py) | Absolute or CWD-relative path to a `.aphelion.tar`. Must resolve under `PARALLAX_APHELION_PACKAGE_DIR` per §5.2 gate 4. Exactly ONE path per invocation (no glob, no batch). |
| `--audit-db` | optional flag | `pathlib.Path` | `None` | [parallax/cli.py:324-332](../../parallax/cli.py) | Per-invocation override of `PARALLAX_AUDIT_DB_PATH`. Defensive guard: when combined with `--dry-run`, the resolved path is rejected if it contains the substring `parallax-kernel/db` ([cli.py:1049-1064](../../parallax/cli.py)) — protects against accidental production writes during test/dry-run. |
| `--dry-run` | optional flag | `bool` (`action="store_true"`) | `False` | [parallax/cli.py:333-340](../../parallax/cli.py) | Runs every validation gate (§5.2), `verify_package`, trust enforcement, claim extraction, and the canonical `write_audit_row` path — but routes writes to an in-memory `:memory:` SQLite clone of the schema instead of the on-disk audit DB ([cli.py:1098-1104](../../parallax/cli.py)). Returns 0 on success, non-zero on any validation failure. On-disk audit DB is closed BEFORE the in-memory clone is opened so no two handles ever co-exist against the real DB. |

### 2.2 Invocation shape

```
parallax ingest <path-to-aphelion-tar>
                [--audit-db <override-db-path>]
                [--dry-run]
```

### 2.3 What `argparse` rejects before any ingest logic runs

`argparse` itself returns exit 2 (Python's `argparse.ArgumentError`) on:
- missing `package_path` positional
- unknown flags
- type-coercion failures (e.g. `--audit-db` not a path)

These are pre-ingest; the spec §6.3 exit-code table starts at 0/65/70/71/78 (no use of 1-64 from ingest code).

### 2.4 Out-of-scope CLI knobs (per contract §5.3 + §2.6)

- No `--allow-unsigned` (`require_signed=True` is hard-coded; contract I-2.1)
- No `--strict` / `--lenient` `ExtractPolicy` overrides (contract I-2.2)
- No glob batch (`parallax ingest pkg-*.tar` — M7+ ergonomic)
- No `--require-notary` (Aphelion v0.5 notary is stub-only)

---

## §3 Environment variables

The implementation reads exactly three env vars at CLI invocation time, all consumed in [`parallax/cli.py:_cmd_ingest`](../../parallax/cli.py) (NOT at module import — `parallax/apex/aphelion_ingest.py` deliberately reads zero env vars so the CLI layer owns env resolution per the module docstring at [aphelion_ingest.py:16-17](../../parallax/apex/aphelion_ingest.py)).

### 3.1 Variable table

| Variable | Required | Default | Read site | Failure (KeyError) | Notes |
|---|---|---|---|---|---|
| `PARALLAX_APHELION_PACKAGE_DIR` | yes | none | [parallax/cli.py:998](../../parallax/cli.py) | structured-log `parallax_ingest_failed` + `reason_code=pkg.dir_unset` + exit 78 (`EX_CONFIG`) ([cli.py:1000-1010](../../parallax/cli.py)) | Absolute directory containing inbound `.aphelion.tar` packages. `<package_path>` MUST resolve under this dir (gate 4, §5.2). |
| `PARALLAX_APHELION_TRUST_STORE` | yes | none | [parallax/cli.py:1013](../../parallax/cli.py) | structured-log + `reason_code=pkg.trust_store_missing` + exit 78 ([cli.py:1015-1025](../../parallax/cli.py)) | Absolute directory containing one `.pem` per trusted signer. Re-read every invocation; no in-memory cache. |
| `PARALLAX_AUDIT_DB_PATH` | yes (unless `--audit-db` set) | none | [parallax/cli.py:1032](../../parallax/cli.py) | structured-log + `reason_code=disk.audit_db_unset` + exit 78 ([cli.py:1034-1044](../../parallax/cli.py)) | Inherited from M5 audit-db contract (`docs/m5-prep/audit-db-path-config.md §3`). Override precedence: `--audit-db > PARALLAX_AUDIT_DB_PATH`. |

The CLI uses `os.environ[name]` (subscript) rather than `os.environ.get(name)` for these three, which means a KeyError on missing is the canonical fail-loud path — matching `rules/python/security.md` ("Use `os.environ["KEY"]` (not `.get()`; raises `KeyError` if missing)"). The KeyError is caught directly at the read site and translated into a deterministic structured log + sysexits exit code.

### 3.2 Env vars NOT read by the ingest path

For absence of doubt:
- `PARALLAX_BIND_HOST` — used only by `parallax serve` (see [cli.py:1161](../../parallax/cli.py)); ingest ignores it.
- `PARALLAX_USER_ID` — used only by `parallax backfill ...` (see [cli.py:287-300](../../parallax/cli.py)); ingest ignores it.

No env-var overrides exist for: `require_signed`, `require_notary`, `ExtractPolicy` limits, idempotency dedup, or retry behaviour. These are all hard-coded per contract §2 / §3.

---

## §4 Error namespace — `ParallaxIngestError`

The module defines exactly one error class: [`ParallaxIngestError(Exception)`](../../parallax/apex/aphelion_ingest.py) at [aphelion_ingest.py:118-137](../../parallax/apex/aphelion_ingest.py). Every M6-defined failure path raises this class with a namespaced `reason_code` string + a derived `exit_code` int.

### 4.1 Closed reason_code set

The full closed set lives in `_REASON_TO_EXIT` at [aphelion_ingest.py:75-113](../../parallax/apex/aphelion_ingest.py). Any reason_code not in this map causes the constructor itself to raise `ValueError` ([aphelion_ingest.py:127-134](../../parallax/apex/aphelion_ingest.py)) — programmer-error guard against silent exit-0 ingest failures.

> **Reading the raise-site column**: every line number below points at the `raise ParallaxIngestError(...)` *keyword* line in `parallax/apex/aphelion_ingest.py`. Two codes (`signer.untrusted` + `signer.manifest_missing`) propagate via `TrustDecision` indirection — the rejection is constructed at one line and raised at another; both are listed. Two codes (`pkg.dir_unset` + `pkg.trust_store_missing`) share the seven raise lines inside `_validate_dir_arg` because the helper takes the reason_code as an argument; each raise line emits whichever code the caller passed in.

| Reason code | Raise site (file:line) | Exit code | Namespace meaning |
|---|---|---|---|
| `pkg.not_found` | aphelion_ingest.py:260, 393 | 65 | package path does not exist on disk |
| `pkg.not_regular_file` | aphelion_ingest.py:255, 264 | 65 | path is symlink / directory / device |
| `pkg.extension_invalid` | aphelion_ingest.py:247 | 65 | path does not end in `.aphelion.tar` |
| `pkg.path_escape` | aphelion_ingest.py:230, 238 | 65 | path resolves outside `PARALLAX_APHELION_PACKAGE_DIR` |
| `pkg.archive_unsafe` | aphelion_ingest.py:401, 688, 696 | 65 | Aphelion `SecurityError` (PATH_TRAVERSAL / ARCHIVE_BOMB / etc.) |
| `pkg.semantic_invalid` | aphelion_ingest.py:409, 534, 541, 711, 715, 719, 726 | 65 | Aphelion `SemanticError` or post-verify manifest invariant violation |
| `pkg.hash_mismatch` | aphelion_ingest.py:405 | 65 | Aphelion `VerificationError` (manifest hash ↔ claim-file divergence) |
| `pkg.unsigned` | aphelion_ingest.py:414, 662 | 65 | `verify_package(require_signed=True)` raised `SignerVerificationError("E_SIGNER_REQUIRED")` |
| `pkg.empty_package` | aphelion_ingest.py:524 | 65 | `manifest["claims"]` was an empty list — operator-mistake guard |
| `pkg.trust_store_missing` | aphelion_ingest.py:192, 196, 200, 207, 211, 215, 219 (via `_validate_dir_arg(dir_unset_code="pkg.trust_store_missing")` at line 639) | 78 | `PARALLAX_APHELION_TRUST_STORE` unset, missing, unreadable, or rejecting absolute/no-`..` policy |
| `pkg.dir_unset` | aphelion_ingest.py:192, 196, 200, 207, 211, 215, 219 (via `_validate_dir_arg(dir_unset_code="pkg.dir_unset")` at line 636) | 78 | `PARALLAX_APHELION_PACKAGE_DIR` unset, missing, unreadable, or rejecting absolute/no-`..` policy |
| `pkg.idempotency_duplicate` | aphelion_ingest.py:580 | 70 | UNIQUE-constraint collision on `envelope_message_id` (UUID v4 collision = bug) |
| `signer.signature_invalid` | aphelion_ingest.py:325, 330, 337, 417 | 65 | Aphelion `SignerVerificationError` (signature cryptographically invalid) OR malformed `SignerManifest` JSON |
| `signer.untrusted` | aphelion_ingest.py:672 (raise) via `TrustDecision.rejected_for(..., "signer.untrusted")` constructed at line 372 inside `_verify_trust` | 65 | resolved `SignerManifest.key_fingerprint` not in trust store |
| `signer.manifest_missing` | aphelion_ingest.py:672 (raise) via `TrustDecision.rejected_for(..., "signer.manifest_missing")` constructed at line 366 inside `_verify_trust` | 65 | envelope's `signer_id` has no matching `signers/<id>.json` in tar |
| `signer.multi_sig_unsupported` | aphelion_ingest.py:654 | 65 | `len(result.envelopes) > 1` — M6 is single-signer only |
| `signer.trust_pem_invalid` | aphelion_ingest.py:301, 312 | 78 | trust store `.pem` file unreadable as PEM or zero-byte |
| `claim.format_invalid` | aphelion_ingest.py:429, 462, 469, 490 | 65 | Aphelion `SchemaError` from `validate_v03_fields()` (other than the R4-subject special case) |
| `claim.duplicate_in_package` | aphelion_ingest.py:504 | 65 | two claims in the same package share `(subject, polarity, valid_from)` |
| `claim.subject_required_for_r4` | aphelion_ingest.py:425, 486 | 65 | claim has an R4-trigger field but no `subject` (Aphelion `SchemaError(CLAIM_SUBJECT_REQUIRED_FOR_CONFLICT)`) |
| `disk.permission` | aphelion_ingest.py:288, 296, 397, 438, 458, 590, 692, 704 | 71 | `PermissionError` or other `OSError` on tar / unpack / claim file / audit DB / re-unpack |
| `disk.audit_db_write_failed` | aphelion_ingest.py:585 | 71 | `AuditDbWriteError` from `write_audit_row()` |
| `disk.audit_db_unset` | parallax/cli.py:1034-1044 (env-var KeyError → `return 78`; no exception raised) | 78 | `PARALLAX_AUDIT_DB_PATH` env var unset and no `--audit-db` override. **NB**: an additional structured-log emit site at parallax/cli.py:1055 also tags this `reason_code`, but the exit there is hard-coded `SystemExit(70)` — this is a binary-level inconsistency tracked as a follow-up at §9.9. |

23 reason codes total. The contract spec §6.2 listed 24; the missing one (`signer.fingerprint_mismatch`) is intentionally NOT implemented and is documented as a follow-up at [aphelion_ingest.py:92-101](../../parallax/apex/aphelion_ingest.py) (see §9.1 below).

### 4.2 Exit-code lookup

```
0   success  — all claims ingested + all audit rows committed
65  EX_DATAERR — operator-fixable data problem (package / signer / claim)
70  EX_SOFTWARE — internal Parallax bug (UUID v4 collision)
71  EX_OSERR — OS-level read/write failure
78  EX_CONFIG — operator config / env-var error
```

Codes 1-63 and 64 (`EX_USAGE`) are reserved for `argparse` itself and are never used by ingest logic.

### 4.3 Public surface

`parallax.apex.aphelion_ingest.__all__` ([aphelion_ingest.py:58-64](../../parallax/apex/aphelion_ingest.py)) exports five symbols:

| Symbol | Kind | Use |
|---|---|---|
| `ParallaxIngestError` | exception class | the only failure type callers need to catch |
| `TrustDecision` | `@dataclass(frozen=True)` | one per envelope; `.accepted_for(signer_id)` / `.rejected_for(signer_id, reason_code)` classmethods construct |
| `ClaimMappingBatch` | `@dataclass(frozen=True)` | the contract §4.3 batch object |
| `IngestReport` | `@dataclass(frozen=True)` | success summary returned by `ingest_package` |
| `ingest_package` | function | the public entry point — keyword-only args; see [aphelion_ingest.py:605-632](../../parallax/apex/aphelion_ingest.py) |

---

## §5 Outcomes / states

### 5.1 Per-audit-row `outcome` field on the ingest path

Every audit row written by `_build_audit_rows` carries the literal string `"hit"` in its `outcome` field ([aphelion_ingest.py:557](../../parallax/apex/aphelion_ingest.py)). This is a deliberate constant — ingest is a *write*, so the read-side notions of `miss` / `divergence` / `error` / `skipped` are not applicable here.

Compare to the read-side closed set established by `DualReadRouter` (M3 + M4):

| `outcome` value | Producer | Meaning |
|---|---|---|
| `hit` | M6 ingest (always) + M3 dual-read (cache+aphelion agree) | row materialised; canonical path completed |
| `miss` | M3 dual-read read path | aphelion returned no claim for the queried subject |
| `divergence` | M3 dual-read | cache and aphelion disagreed |
| `error` | M3 dual-read | one side raised; outcome inconclusive |
| `skipped` | M3 dual-read | dual-read disabled or circuit-breaker open |

M6 ingest only ever produces `hit`. The non-`hit` outcomes are exercised exclusively by the read-side adapter and are out of scope for this spec.

### 5.2 Per-envelope `TrustDecision` states

`TrustDecision` (frozen dataclass at [aphelion_ingest.py:140-154](../../parallax/apex/aphelion_ingest.py)) is constructed via two classmethods:

| Constructor | `accepted` | `reason_code` |
|---|---|---|
| `TrustDecision.accepted_for(signer_id)` | `True` | `None` |
| `TrustDecision.rejected_for(signer_id, reason_code)` | `False` | one of `signer.manifest_missing` / `signer.untrusted` (per `_verify_trust` at aphelion_ingest.py:350-376) |

A rejection produced inside `_verify_trust` propagates to a `ParallaxIngestError` raise at [aphelion_ingest.py:668-675](../../parallax/apex/aphelion_ingest.py) — the rejected `reason_code` is the exception's `reason_code`.

### 5.3 Pipeline phase states (internal, not persisted)

Conceptually the ingest pipeline traverses these phases per invocation:

```
[1] env+path gates           (§3 + §5.2 gates 1-6 + gates 7-9 trust-dir)
[2] aphelion verify_package  (unpack → semantic → signature → notary)
[3] multi-sig hard reject    (envelopes count == 1 invariant)
[4] trust-store enforcement  (per-envelope fingerprint match)
[5] re-unpack                (manifest + claim files for mapping)
[6] claim validation         (v0.3 validator + in-package dup guard)
[7] audit_row canonicalize   (M5 audit_writer canonicalize_row)
[8] audit_row write          (write_audit_row + assert_audit_row_committed)
[9] structured log success   (parallax_ingest_succeeded)
```

**Atomicity is per-phase, not per-package.** Phases [1]-[7] are read-only or in-memory only, so a failure at any of them leaves `audit.db` byte-identical to its pre-invocation state — partial-success is structurally impossible up to and including the canonicalize step. Per contract §2 I-2.3, this matches the "verify-or-reject" contract for the verification pipeline.

Phase [8] is **not atomic at the batch level.** `_write_batch` ([aphelion_ingest.py:569-597](../../parallax/apex/aphelion_ingest.py)) iterates rows one at a time, calling `write_audit_row` per row. `write_audit_row` ([audit_db.py:514-590](../../parallax/apex/audit_db.py)) wraps each INSERT in its own `BEGIN IMMEDIATE` → `INSERT` → `COMMIT` against an autocommit-state connection — the M5 §4.7 write-order fence is **per row**, not per batch. If row N raises `AuditDbWriteError` / `PermissionError` / `sqlite3.IntegrityError`, rows 1..N-1 are **already committed** and survive in `audit.db`; only row N's own transaction is rolled back. The ingest invocation then exits with the `ParallaxIngestError` for row N, and the caller observes `IngestReport`-less failure even though some audit rows from that package_id are now persisted.

Operators MUST treat phase-[8] mid-batch failures as **partial-write**, not as atomic rollback — see the runbook §2.2 recovery section. The structural-impossibility-of-partial-batch language in earlier drafts was wrong and has been removed. This is tracked as a binary follow-up at §9.10 (proposed fix: wrap `_write_batch` in a single `BEGIN IMMEDIATE` so the M5 per-row fence escalates to a per-batch fence).

---

## §6 Performance baseline

### 6.1 Measured SLO (PR #58 stress smoke, 2026-05-17)

The PR-#58 pre-merge stress smoke (`scripts/m6_audit_db_stress.py`, 10k-row run, single-process, no concurrent ingest) recorded:

| Metric | Measured | Spec SLO (contract §7.7) | Headroom |
|---|---|---|---|
| Throughput | **28,650 rows/sec** | ≥ 2,000 rows/sec | ~14× over SLO |
| p99 latency | **0.057 ms** | ≤ 10 ms | ~175× under SLO |
| Errors | 0 | 0 | — |

These figures are recorded in the PR #58 description table under "Stress smoke (10k rows)".

### 6.2 Single-threaded caveat (P2 backlog #4)

The stress smoke runs single-process, single-thread against an `audit.db` opened in WAL mode. SQLite WAL serialises writers, so the measured 28,650 rps is the *write-path single-thread headroom* — concurrent producer scaling above 1 writer is bounded by WAL checkpoint contention, not by the M6 mapping cost.

Because the contract (§Q-M6.1) freezes M6 as manual-CLI-only / one invocation at a time, the single-thread number is the correct SLO. When M7+ adds a watcher or glob batch ingest that fans out multiple `ingest_package` calls in parallel, the SLO claim MUST be re-measured under concurrent load and (P2 backlog #4) the spec MUST add a concurrent-headroom row to this table.

### 6.3 Stress harness location

The harness lives at [scripts/m6_audit_db_stress.py](../../scripts/m6_audit_db_stress.py) — explicitly NOT `/tmp/` per 5/16 P2 backlog #6. It uses the same `_DDL` schema as `parallax/apex/audit_db.py` and writes to a tempdir-based DB (the script has a hard-coded `parallax-kernel/db` substring guard at [m6_audit_db_stress.py:36-43](../../scripts/m6_audit_db_stress.py)).

---

## §7 Operator runbook

Operating procedures, smoke commands, and failure-case troubleshooting (PEM verify fail / sqlite WAL stall / tar corrupt / claim mapping ambiguity / single-instance safety) live in the companion runbook:

→ [docs/m6-ingest/m6-ingest-runbook.md](./m6-ingest-runbook.md)

---

## §8 Decisions cross-reference — Q-M6.1 ~ Q-M6.4

For each contract-level decision (contract spec §1), this table records whether the implementation matches the decision and where in the binary that match is enforced.

| Decision | Contract decided | Implementation matches? | Enforcement site |
|---|---|---|---|
| Q-M6.1 (trigger model) | Manual CLI only — no watcher, no poll, no daemon | ✅ implements decision; binary is *stricter* than the contract wording (see note below) | `parallax/cli.py` ingest subparser is the sole entry point; no service, no `--watch` flag |
| Q-M6.2 (trust keys) | File-based PEM store at `PARALLAX_APHELION_TRUST_STORE`, re-read per invocation | ✅ exact | `_load_trust_store` ([aphelion_ingest.py:271-317](../../parallax/apex/aphelion_ingest.py)) re-reads directory on every call; no module-level cache |
| Q-M6.3 (v0.4 evidence schema) | Ship-then-upgrade against v0.3 R1-R4 claim semantics | ✅ exact | `_read_claim_frontmatter` uses `aphelion.v03_validator.validate_v03_fields` (not v04_*); additive-only invariant preserved |
| Q-M6.4 (federation scope) | Strictly single-instance — local `PARALLAX_APHELION_PACKAGE_DIR` only, no peer traversal | ✅ exact | `_validate_package_path` ([aphelion_ingest.py:225-268](../../parallax/apex/aphelion_ingest.py)) hard-rejects any path that does not resolve under the single configured `package_dir` |

The route-A vs route-B pivot (2026-05-16 noon — build `.aphelion.tar` → claim mapping pipeline as canonical wire, NOT stub `claim_loader` reading JSON dir) was honoured: the pipeline reads `.aphelion.tar` via `aphelion.verifier.verify_package` and `aphelion.unpacker.unpack`; the 70 JSON fixtures at `tests/fixtures/m6_claim_mappings/` are *test fixtures only*, not a production ingest source (see [fixture catalog doc](./m6-fixture-catalog-format.md)).

**Q-M6.1 binary-stricter-than-contract note**: the contract spec [§1 Q-M6.1 decision](../m6-prep/m6-ingest-contract-spec.md) text reads "takes one or more `.aphelion.tar` paths", but the contract spec §5.3 also explicitly defers "Glob batch ingest (`parallax ingest pkg-*.tar`)" to M7+ and says "M6 CLI accepts ONE package_path per invocation". The implementation honours §5.3 (single path per invocation; the `package_path` positional is non-`nargs`); the §1 wording is a contract-internal inconsistency between the trigger-model decision and the path-resolver section. This impl spec aligns with the §5.3 reading.

---

## §9 Future work / known follow-ups

These are the items shipped *with* the binary as known gaps, ordered by surface area.

### 9.1 `signer.fingerprint_mismatch` not implemented

The contract spec §3.4 defines `signer.fingerprint_mismatch` to distinguish "matching `<signer_id>.pem` present but fingerprint differs" from the broader `signer.untrusted` (no matching `.pem` at all). The M6 trust-store implementation is filename-irrelevant — every `.pem` file's bytes hash into the trusted-fingerprint set regardless of filename — so the precondition for detecting the mismatch case (a `.pem` named exactly `<signer_id>.pem`) is absent. The code at [aphelion_ingest.py:92-101](../../parallax/apex/aphelion_ingest.py) intentionally omits this reason code from `_REASON_TO_EXIT` rather than ship dead-reachable code. **Follow-up**: round-2 spec must either (a) introduce a `<signer_id>.pem` filename convention to make the mismatch case detectable, or (b) drop the reason code from the contract spec.

### 9.2 Ship-then-upgrade strategy for Aphelion v0.4

Per contract Q-M6.3, M6 ships against Aphelion v0.3. When v0.4 lands the richer evidence binding (`role`, `capture_ts`, `source_uri`, `excerpt_range`, `original_hash`), the upgrade lane is:

1. Verify the additive-only invariant on the v0.4 release (`v0.3-claim-semantics.md §1 Migration` + §2 constraint 1 must still hold).
2. Bump `aphelion.v03_validator` → `aphelion.v04_validator` import in `_read_claim_frontmatter` and `_validate_and_check_duplicates`.
3. Re-run the M6 integration suite + 70-fixture corpus against v0.4 backward-compat fixtures from PR #51.
4. Add v0.4 fields to the audit_row mapping if any new field is operator-relevant for read-side use cases.

NO action is required on already-ingested v0.3 audit rows — the audit chain is append-only and v0.4 readers must accept v0.3-shape rows unchanged.

### 9.3 Concurrent-headroom caveat (P2 backlog #4)

Per §6.2: the 28,650 rps SLO is single-thread only. M7+ watcher / batch ergonomics will need to either:
- serialise ingests via a CLI lock file (preserves single-thread SLO claim), OR
- re-measure under concurrent load + revise §6 with a concurrent-headroom row + WAL checkpoint contention analysis.

### 9.4 Route-B stub `claim_loader` JSON-dir tech debt — permanently rejected

The pre-pivot route-B (stub `claim_loader` reads JSON dir under `PARALLAX_APHELION_PACKAGE_DIR`) is permanently NOT implemented and the 70 JSON fixtures at `tests/fixtures/m6_claim_mappings/` are explicitly test-only (per [tests/fixtures/m6_claim_mappings/CATALOG.md](../../tests/fixtures/m6_claim_mappings/CATALOG.md) "Route A locked"). **Operator-action item**: do NOT point `PARALLAX_APHELION_PACKAGE_DIR` at `tests/fixtures/m6_claim_mappings/` — the directory contains bare claim-mapping JSON, NOT `.aphelion.tar` archives; gate 5 of §5.2 ("ends in `.aphelion.tar`") will reject every file in the dir, which is the correct behaviour.

### 9.5 Trust-store `.pem` format ambiguity

Per PR #58 reviewer-focus #4: the implementation hashes raw `.pem` file bytes via `aphelion.signer.compute_key_fingerprint` (matches the HMAC/Ed25519 raw-bytes fingerprint scheme). Operator-supplied `.pem` files must contain the raw public-key bytes — NOT a PEM-armored ASCII envelope — for the fingerprint to match `SignerManifest.key_fingerprint`. The contract spec §3.2 is silent on this. **Follow-up**: contract spec round-2 should explicitly document the byte-payload contract.

### 9.6 Double-unpack cost

`verify_package` unpacks into its own internal `tempfile.TemporaryDirectory` for verification; the M6 ingest pipeline then re-unpacks via `unpack(resolved_pkg, tmp_dir, ExtractPolicy.default())` ([aphelion_ingest.py:684-706](../../parallax/apex/aphelion_ingest.py)) to access the manifest + claim frontmatters for mapping. This double-unpack is acceptable for M6 (small packages, manual CLI volume) but if Aphelion v0.6+ exposes the verified-unpack directory directly, the M6 path SHOULD switch to single-unpack. Flag for v0.6+ API review.

### 9.7 Conflict-bucket fixture `polarity:"deny"` divergence

PR #58 reviewer-focus #5: the `tests/fixtures/m6_claim_mappings/conflict/*.json` fixtures use `polarity:"deny"`, but v0.3 only accepts `affirm` / `negate` / `unknown`. The current M6 test corpus documents the reverse-engineered behaviour (these fixtures are rejected with `claim.format_invalid`); a future corpus fix (rename `deny` → `negate`, or bundle conflict pairs into a single package whose validation produces `claim.duplicate_in_package`) will flip the assertion. The audit-side spec (this doc) is unaffected because the fixtures are test-only; the [fixture catalog doc](./m6-fixture-catalog-format.md) records the divergence.

### 9.8 `--audit-db` defensive guard scope limitation

The defensive guard at [parallax/cli.py:1046-1064](../../parallax/cli.py) only fires when BOTH `--audit-db` is set AND `--dry-run` is set. A plain `parallax ingest <pkg> --audit-db /home/chris/parallax-kernel/db/audit.db` (no `--dry-run`) is NOT blocked — the guard does not protect against operator typo on a real production-write invocation, only against test-route invocations. **Follow-up**: either widen the guard to fire on `--audit-db` regardless of `--dry-run`, OR document the scope limitation prominently in the `--help` text. The runbook §2.5 captures the operator-side warning.

### 9.9 `cli.py:1064` `SystemExit(70)` + `reason_code=disk.audit_db_unset` binary inconsistency

At [parallax/cli.py:1051-1064](../../parallax/cli.py) the `--audit-db + --dry-run + parallax-kernel/db` guard emits a structured log line tagged `reason_code=disk.audit_db_unset` but raises `SystemExit(70)`. Per §4.2 and the `_REASON_TO_EXIT` mapping at [aphelion_ingest.py:112](../../parallax/apex/aphelion_ingest.py), `disk.audit_db_unset` should map to exit 78 (`EX_CONFIG`), not 70 (`EX_SOFTWARE`). The combination is a binary-level inconsistency: an operator who reads the log expects exit 78, but the actual process exit is 70. **Follow-up**: open a separate GitHub issue against the implementation to either (a) change the exit code to 78 (matches the map), or (b) introduce a new reason code such as `disk.audit_db_path_invalid` mapped to 70. This doc PR does NOT modify the implementation per the doc-only retrofit scope.

### 9.10 Phase-[8] partial-batch persistence (codex round-2 finding)

`_write_batch` ([aphelion_ingest.py:569-597](../../parallax/apex/aphelion_ingest.py)) iterates audit rows one at a time, calling `write_audit_row` ([audit_db.py:514-590](../../parallax/apex/audit_db.py)) per row. `write_audit_row` wraps each INSERT in its own `BEGIN IMMEDIATE` / `COMMIT` against an autocommit-state connection. Consequence: if row N raises mid-batch, rows 1..N-1 are already committed and survive in `audit.db`; the M5 §4.7 write-order fence is **per row**, not per package. The ingest invocation then exits with the row-N `ParallaxIngestError`, and the operator sees a "failed ingest" log line even though some audit rows for that `package_id` are now persisted.

The contract spec §2 I-2.3 said "partial-success is rejected" but the scope of that invariant covers verify_package phases [1]-[6] only — the contract did not specify atomic-batch semantics for the audit-write phase. The implementation honours the contract; the over-claim was in the impl spec's earlier draft of §5.3 (now fixed). The runbook §2.2 carries the operator-side recovery procedure for partial-batch scenarios.

**Recommended binary fix** (separate implementation PR, NOT this doc PR): wrap the row loop in `_write_batch` inside a single `BEGIN IMMEDIATE` / `COMMIT` so the per-row `write_audit_row` calls run inside a parent transaction; any row failure would then rollback the entire batch. This is a small surgical change but requires careful handling of `write_audit_row`'s precondition check (`conn.in_transaction must be False`) at [audit_db.py:539](../../parallax/apex/audit_db.py) — likely the cleanest path is a new sibling helper `write_audit_rows_atomic(conn, rows)` rather than mutating the per-row contract.

Credit: codex round-2 review on PR #61 flagged this as P2 (2× same-finding inline comments on impl-spec §5.3 line 240).

---

## §10 Spec-vs-code consistency

This spec is paired with [tests/docs/test_m6_spec_consistency.py](../../tests/docs/test_m6_spec_consistency.py). The test:

1. Grep-extracts every `p_ingest.add_argument(...)` argument flag in `parallax/cli.py`
2. Grep-extracts every `os.environ[...]` or `os.environ.get(...)` access in `parallax/cli.py` + `parallax/apex/aphelion_ingest.py` whose key starts with `PARALLAX_`, then narrows to env vars referenced inside the `_cmd_ingest` function body
3. Grep-extracts every `class .*Error` definition in `parallax/apex/aphelion_ingest.py`
4. Grep-extracts every key of `_REASON_TO_EXIT` in `parallax/apex/aphelion_ingest.py`
5. Grep-extracts every symbol listed in `aphelion_ingest.__all__`
6. Asserts every extracted symbol is mentioned by name somewhere in this spec doc

The test runs under pytest as part of the doc-PR consistency gate. If you change the CLI surface, env vars, error classes, reason codes, or `__all__` exports, you MUST also update this spec doc (and possibly the runbook + fixture catalog doc) before the test will pass.

---

## Changelog

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-05-19 | Initial implementation-level spec retrofit from PR #58 binary (commit `7434724`). Reverse-engineered from `parallax/apex/aphelion_ingest.py` 735 LoC + `parallax/cli.py` ingest subparser + stress harness. Closes P2 backlog #3 ("M6 ingest spec doc 缺"). |
