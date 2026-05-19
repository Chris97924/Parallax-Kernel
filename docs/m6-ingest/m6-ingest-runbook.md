---
title: Apex M6 Ingest Pipeline — Operator Runbook
status: living
version: v0.1
date: 2026-05-19
owner: Parallax-Kernel
audience: operator (Chris) — single-instance, single-operator deployment
companion_specs:
  - docs/m6-ingest/m6-ingest-impl-spec.md (binary-level facts)
  - docs/m6-prep/m6-ingest-contract-spec.md (wire-level contract)
---

# Apex M6 Ingest Pipeline — Operator Runbook

> **Audience**: the human running `parallax ingest <pkg>`. This runbook covers happy-path invocation, the eight named failure modes the binary can hit, single-instance safety, and post-deploy smoke verification.

---

## 1. Happy path

### 1.1 Pre-flight (one-time per host)

Before the first `parallax ingest`, the operator must:

1. **Create the package directory** and export `PARALLAX_APHELION_PACKAGE_DIR`:
   ```
   # Linux ZenBook
   mkdir -p /home/chris/parallax-data/aphelion-packages
   export PARALLAX_APHELION_PACKAGE_DIR=/home/chris/parallax-data/aphelion-packages

   # Windows
   $env:PARALLAX_APHELION_PACKAGE_DIR = "E:\Parallax\data\aphelion-packages"
   ```

2. **Create the trust store** and export `PARALLAX_APHELION_TRUST_STORE`:
   ```
   # Linux ZenBook
   mkdir -p /home/chris/parallax-data/trust-store
   export PARALLAX_APHELION_TRUST_STORE=/home/chris/parallax-data/trust-store

   # Windows
   $env:PARALLAX_APHELION_TRUST_STORE = "E:\Parallax\data\trust-store"
   ```

3. **Drop one `.pem` per trusted signer** into the trust store. The file must contain the **raw public-key bytes** (NOT a PEM-armored ASCII envelope — see impl-spec §9.5). The filename is irrelevant; the binary hashes the byte payload and matches it against each envelope's `SignerManifest.key_fingerprint`.

4. **Export `PARALLAX_AUDIT_DB_PATH`** (inherited from M5 — same value used by `parallax serve`):
   ```
   export PARALLAX_AUDIT_DB_PATH=/home/chris/parallax-kernel/db/audit.db
   ```

### 1.2 Per-invocation flow

```
parallax ingest /home/chris/parallax-data/aphelion-packages/2026-05-18-pkg.aphelion.tar
```

What runs, in order (the impl-spec §5.3 phase list):

| Phase | What happens | Code reference |
|---|---|---|
| 1 | env-var resolution + 6 path/dir gates (extension, regular file, resolves under `package_dir`, absolute, no `..`, readable) + 3 trust-store gates | `parallax/cli.py:_cmd_ingest` + `aphelion_ingest._validate_dir_arg` + `_validate_package_path` |
| 2 | `aphelion.verifier.verify_package(tar_path, require_signed=True, require_notary=False)` — unpack safety, semantic invariants, hash chain, signature crypto | `aphelion_ingest._run_aphelion_verify` |
| 3 | Multi-sig hard reject — `len(result.envelopes) > 1` → `signer.multi_sig_unsupported` exit 65 | `aphelion_ingest.ingest_package` (post-verify guard) |
| 4 | Per-envelope `key_fingerprint` lookup against the trust store; rejection → `signer.untrusted` exit 65 | `aphelion_ingest._verify_trust` |
| 5 | Re-unpack into a `parallax-m6-*` tempdir for manifest + claim file access | `aphelion_ingest.ingest_package` (re-unpack block) |
| 6 | `validate_v03_fields()` per claim + in-package `(subject, polarity, valid_from)` duplicate guard | `aphelion_ingest._build_audit_rows` + `_validate_and_check_duplicates` |
| 7 | `canonicalize_row` per claim — M5 contract preserved | `aphelion_ingest._build_audit_rows` (last loop) |
| 8 | `write_audit_row` + `assert_audit_row_committed` per row — M5 §8.1 write-order fence | `aphelion_ingest._write_batch` |
| 9 | Structured-log success: `event=parallax_ingest_succeeded`, `package_id`, `claim_count`, `audit_rows_written`, `signer_id`, `elapsed_ms` | `aphelion_ingest.ingest_package` (tail) |

On success: exit 0, one log line, one or more rows in `audit.db`.

On failure: exit ∈ {65, 70, 71, 78}, one structured-log error line. Failure at phases [1]-[7] is atomic — `audit.db` is byte-identical to its pre-invocation state. Failure during phase [8] is **not** atomic at the batch level: rows committed before the failing row stay persisted in `audit.db` (see §2.2 partial-batch recovery and impl-spec §5.3 + §9.10).

### 1.3 Verifying a successful ingest

```
sqlite3 $PARALLAX_AUDIT_DB_PATH \
  "SELECT package_id, COUNT(*) FROM audit_row GROUP BY package_id ORDER BY ts DESC LIMIT 5;"
```

Expected: one row per recently-ingested package_id, with the COUNT matching the `claims_ingested` field in the structured success log.

---

## 2. Failure modes — diagnosis and fix

The binary emits one structured log line per failure with `event=parallax_ingest_failed`, `reason_code`, `package_path`, `signer_id`, and `underlying`. The exit code is one of 65 / 70 / 71 / 78. The 23 named reason codes (full set in impl-spec §4.1) are grouped below into 5 operational categories.

### 2.1 Signer / trust verification failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `reason_code=signer.untrusted` exit 65 | `.pem` for this signer not in `PARALLAX_APHELION_TRUST_STORE`, OR `.pem` content is wrong bytes | (a) confirm the signer's public-key bytes are dropped into the trust store; (b) confirm the `.pem` is **raw bytes**, not ASCII PEM-armor (impl-spec §9.5) |
| `reason_code=signer.manifest_missing` exit 65 | package's signature references a `signer_id` for which no `signers/<id>.json` exists inside the tar | corrupted package; re-fetch from producer |
| `reason_code=signer.trust_pem_invalid` exit 78 | `.pem` file in trust store is empty (zero bytes) or unreadable | remove the bad `.pem`; re-drop a non-empty one |
| `reason_code=signer.signature_invalid` exit 65 | Aphelion cryptographic verification failed — bytes tampered or signer manifest malformed | reject the package; producer must re-sign |
| `reason_code=signer.multi_sig_unsupported` exit 65 | package has more than one `SignatureEnvelope` | M6 is single-signer only; producer must downgrade to single-sig until M7+ |
| `reason_code=pkg.unsigned` exit 65 | package has no `signatures.jsonl` at all | producer must sign before sending; no `--allow-unsigned` escape hatch (per contract I-2.1) |

### 2.2 sqlite WAL checkpoint stall / audit-db write failure

| Symptom | Likely cause | Fix |
|---|---|---|
| `reason_code=disk.audit_db_write_failed` exit 71 | `write_audit_row` raised `AuditDbWriteError` (constraint violation, schema drift, FK failure) | check audit.db schema matches `parallax.apex.audit_db._SCHEMA_STATEMENTS`; run `PRAGMA integrity_check` and `PRAGMA quick_check` |
| `reason_code=disk.permission` exit 71 | `PermissionError` on audit DB or tempdir | check filesystem permissions on `$PARALLAX_AUDIT_DB_PATH` parent dir + `$TMPDIR` |
| `reason_code=pkg.idempotency_duplicate` exit 70 | UUID v4 collision on `envelope_message_id` (1 in 2^122 — if you see it twice, something is wrong) | file a bug; do NOT retry blindly |
| Hung process (no log, no exit) | WAL checkpoint stall under contention | check for other writers holding `audit.db`: `lsof $PARALLAX_AUDIT_DB_PATH` (Linux) or `handle.exe $PARALLAX_AUDIT_DB_PATH` (Windows). M6 is single-instance only — see §3 |

**Partial-batch caveat (operator-critical)**: phase [8] of the pipeline (`_write_batch` in [aphelion_ingest.py:569-597](../../parallax/apex/aphelion_ingest.py)) is **not atomic at the batch level**. Each row goes through its own `BEGIN IMMEDIATE` → `INSERT` → `COMMIT` via `write_audit_row` ([audit_db.py:514-590](../../parallax/apex/audit_db.py)). If row N raises mid-batch, rows 1..N-1 are **already committed** to `audit.db` even though the ingest invocation exits with `ParallaxIngestError` for row N. The M5 §4.7 write-order fence is per-row, NOT per-package — see impl-spec §5.3 + §9.10 for the binary follow-up.

**Recovery on phase-[8] mid-batch failure**:

1. Inspect the failing log line: `reason_code`, `package_path`, `package_id` (the package_id appears in the structured-log `extra` dict).
2. Query the audit DB for how many rows already landed:
   ```
   sqlite3 $PARALLAX_AUDIT_DB_PATH \
     "SELECT COUNT(*) FROM audit_row WHERE package_id = '<package_id>';"
   ```
3. Compare against `manifest["claims"]` length inside the package (`tar -tf <pkg>.aphelion.tar | grep claims/`). If the DB count < manifest count → partial batch exists.
4. **Do NOT re-run `parallax ingest` blindly** — re-ingest produces fresh `envelope_message_id` UUIDs, so all claims get a second audit row (the existing N-1 rows are NOT updated; you would end up with N-1 + N rows total, not N).
5. Decide based on operational needs:
   - **If audit chain must reflect the full package**: manually `DELETE FROM audit_row WHERE package_id = '<package_id>'` (preserve a backup first), then re-ingest cleanly. Append-only invariant is broken at this step; document the manual repair.
   - **If partial chain is acceptable**: leave the N-1 rows in place; the missing claims simply have no audit record. Downstream `AphelionReadAdapter` will not surface them.

This partial-batch behaviour is tracked as a binary follow-up at impl-spec §9.10 — when the recommended fix lands (wrap `_write_batch` in a single transaction), this section can be reduced back to "the failure is atomic at the package level".

### 2.3 Tar corrupt / unpack safety violations

| Symptom | Likely cause | Fix |
|---|---|---|
| `reason_code=pkg.archive_unsafe` exit 65 | Aphelion `SecurityError` — PATH_TRAVERSAL, ARCHIVE_BOMB, symlink-out-of-tree, file-count > 10k, size > 100 MiB total or 25 MiB single-file | the tar is malformed or hostile; reject and re-fetch from producer |
| `reason_code=pkg.hash_mismatch` exit 65 | Aphelion `VerificationError` — manifest hash ↔ on-disk claim file mismatch | tar was modified in transit; re-fetch |
| `reason_code=pkg.semantic_invalid` exit 65 | Aphelion `SemanticError` — FILESET_DIVERGENCE / CHAIN_BROKEN / DANGLING_REFERENCE | producer-side build bug; report upstream |
| `reason_code=pkg.empty_package` exit 65 | `manifest["claims"]` is an empty list | producer error — empty ingest is rejected to avoid silent no-op |
| `reason_code=pkg.not_regular_file` / `pkg.not_found` / `pkg.extension_invalid` / `pkg.path_escape` exit 65 | operator typo or wrong path — symlink, dir, missing file, wrong extension, path resolves outside `package_dir` | fix the operator command line |

### 2.4 Claim mapping ambiguity

| Symptom | Likely cause | Fix |
|---|---|---|
| `reason_code=claim.format_invalid` exit 65 | v0.3 validator rejected a claim frontmatter | inspect the claim file inside the tar (extract manually: `tar -xf pkg.aphelion.tar`); look for missing required fields, wrong types, invalid enums |
| `reason_code=claim.subject_required_for_r4` exit 65 | claim has `polarity` / `valid_from` / `valid_until` / `supersedes` but no `subject` | producer-side build bug; the v0.3 validator requires `subject` whenever any R4-trigger field is set |
| `reason_code=claim.duplicate_in_package` exit 65 | two claims in the same package have identical `(subject, polarity, valid_from)` keys | producer-side build bug; the package author included two logical duplicates |

### 2.5 Operator config errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `reason_code=pkg.dir_unset` exit 78 | `PARALLAX_APHELION_PACKAGE_DIR` env var unset | `export PARALLAX_APHELION_PACKAGE_DIR=...` (see §1.1) |
| `reason_code=pkg.trust_store_missing` exit 78 | `PARALLAX_APHELION_TRUST_STORE` env var unset, OR the directory does not exist / is not readable | `export PARALLAX_APHELION_TRUST_STORE=...`; verify with `ls "$PARALLAX_APHELION_TRUST_STORE"` |
| `reason_code=disk.audit_db_unset` exit 78 | `PARALLAX_AUDIT_DB_PATH` env var unset AND `--audit-db` flag not provided | export the env var; do NOT use `--audit-db` for production ingest (it's a testing override) |

> **Operator warning — `--audit-db` guard scope**: the defensive guard that rejects audit DB paths containing the substring `parallax-kernel/db` only fires when `--audit-db` is combined with `--dry-run` ([parallax/cli.py:1046-1064](../../parallax/cli.py)). A plain `parallax ingest <pkg> --audit-db /home/chris/parallax-kernel/db/audit.db` (no `--dry-run`) will write to the production DB. Do NOT use `--audit-db` for production ingest under any circumstance — the env var is the production path. See impl-spec §9.8.
>
> **Operator warning — exit-code inconsistency**: if the `--audit-db + --dry-run + parallax-kernel/db` guard does fire, the process exits 70 even though the structured log tags `reason_code=disk.audit_db_unset` (which the impl-spec table maps to exit 78). Treat the log line as the source of truth for diagnosis; expect process exit 70. See impl-spec §9.9 — this is a tracked binary follow-up.

---

## 3. Single-instance safety

Per contract Q-M6.1 + Q-M6.4, M6 ingest is **strictly single-instance, single-operator, one-invocation-at-a-time**. There is no built-in lock. The operator MUST not run two `parallax ingest` invocations against the same `audit.db` concurrently.

### 3.1 Confirming no concurrent ingest is running

**Linux**:
```
pgrep -fa "parallax ingest" || echo "no ingest running"
lsof $PARALLAX_AUDIT_DB_PATH 2>/dev/null | grep -v '^COMMAND' || echo "no DB handles"
```

**Windows (PowerShell)**:
```
Get-Process | Where-Object { $_.CommandLine -match 'parallax ingest' }
handle.exe $env:PARALLAX_AUDIT_DB_PATH 2>$null
```

If either returns a non-empty result, **do not start a second ingest**. Wait for the running one to complete, then proceed.

### 3.2 What concurrent ingests would break

Two concurrent ingests against the same `audit.db` (WAL mode) are *partially* serialised by SQLite at the page-write level, but:

- The `envelope_message_id` UNIQUE constraint defence still holds (UUID v4 collision probability is negligible).
- There is no transactional grouping at the package level — if process A is mid-batch and process B starts, B's rows interleave with A's by SQLite write order, not by package order.
- WAL checkpoint contention degrades throughput sharply once two writers compete; the 28,650 rps single-thread baseline in impl-spec §6.1 does **not** hold under concurrent load.

For M6, the operator-discipline contract is: run one at a time. M7+ will add a lock file or a serialising daemon if/when concurrent ingest becomes a real need.

### 3.3 Cross-instance safety

Per contract Q-M6.4: M6 reads ONLY the local `PARALLAX_APHELION_PACKAGE_DIR`. There is no peer traversal, no remote fetch, no `peers/<other>/` walk. If both ZenBook and Win need to ingest the same package, the operator copies the `.aphelion.tar` file across hosts manually and runs `parallax ingest` on each host independently. Each host produces its own audit chain (per `audit-db-path-config.md §7`).

---

## 4. Smoke test commands

Use these after a deploy to confirm the binary is healthy without touching production data.

### 4.1 CLI surface smoke

```
parallax ingest --help
```

Expected output mentions `package_path`, `--audit-db`, `--dry-run`. Exit 0.

### 4.2 Missing-env-var smoke (negative test)

```
unset PARALLAX_APHELION_PACKAGE_DIR
parallax ingest /tmp/nonexistent.aphelion.tar
echo "exit=$?"
```

Expected: exit 78, one structured-log line with `reason_code=pkg.dir_unset`, `event=parallax_ingest_failed`.

```
export PARALLAX_APHELION_PACKAGE_DIR=/tmp
unset PARALLAX_APHELION_TRUST_STORE
parallax ingest /tmp/nonexistent.aphelion.tar
echo "exit=$?"
```

Expected: exit 78, `reason_code=pkg.trust_store_missing`.

### 4.3 Dry-run smoke (positive test, no production write)

```
parallax ingest "$PARALLAX_APHELION_PACKAGE_DIR/<any-real-pkg>.aphelion.tar" --dry-run
echo "exit=$?"
```

Expected on a real, signed, trusted, valid package: exit 0, structured log `event=parallax_ingest_succeeded`, `dry_run=true`, `audit_rows_written=0`, `claim_count=<N>`. The on-disk `audit.db` row count is unchanged.

Verify:
```
sqlite3 $PARALLAX_AUDIT_DB_PATH "SELECT COUNT(*) FROM audit_row;"
# expected: same count as before the dry-run
```

### 4.4 Stress smoke (optional, NOT part of normal deploy)

The stress harness lives at [scripts/m6_audit_db_stress.py](../../scripts/m6_audit_db_stress.py). 10k rows is sufficient for sanity; 1M-row run reproduces the impl-spec §6.1 baseline.

```
python scripts/m6_audit_db_stress.py --rows 10000
```

Expected: throughput ≥ 2,000 rps + p99 ≤ 10 ms + 0 errors. Output is JSON at `/tmp/m6_audit_db_stress_report.json`.

The script has a hard-coded production-DB guard at [scripts/m6_audit_db_stress.py:36-43](../../scripts/m6_audit_db_stress.py) — it will refuse to start if the resolved stress-DB path contains `parallax-kernel/db`.

---

## 5. Retry semantics

M6 ingest has **no automatic retry**. The binary returns a definitive exit code per invocation:

| Exit | Retry guidance |
|---|---|
| 0 | success; do NOT re-run on the same package (the same claims would be re-ingested with fresh `envelope_message_id` UUIDs — the audit chain would gain duplicate logical claims with distinct envelope IDs, which is the design but not what you want for a healthy retry) |
| 65 | data-error; producer or operator must fix the input. Re-running with no change will produce the same exit 65. |
| 70 | software bug (UUID collision). File an issue. Re-running might or might not reproduce. |
| 71 | OS-level transient (disk full, EIO, etc.). Diagnose the underlying OS condition first; once fixed, re-run is safe. |
| 78 | operator config error. Fix the env var or trust store; re-run. |

There is no built-in deduplication: if `parallax ingest pkg.aphelion.tar` succeeds, then the same command is run again, the audit chain will contain TWO sets of audit rows for the package's claims (different `envelope_message_id`, same `package_id` and `claim_id`). This is **intentional** per contract §5.4 — each invocation is independently recorded. Higher-layer dedup (if needed) is a downstream concern.

---

## 6. Cross-references

- Implementation-level spec: [docs/m6-ingest/m6-ingest-impl-spec.md](./m6-ingest-impl-spec.md)
- Contract spec: [docs/m6-prep/m6-ingest-contract-spec.md](../m6-prep/m6-ingest-contract-spec.md)
- Fixture catalog format: [docs/m6-ingest/m6-fixture-catalog-format.md](./m6-fixture-catalog-format.md)
- M5 audit-db path config: [docs/m5-prep/audit-db-path-config.md](../m5-prep/audit-db-path-config.md)
- Source: [parallax/apex/aphelion_ingest.py](../../parallax/apex/aphelion_ingest.py), [parallax/cli.py](../../parallax/cli.py)
- Stress harness: [scripts/m6_audit_db_stress.py](../../scripts/m6_audit_db_stress.py)

---

## Changelog

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-05-19 | Initial runbook retrofit alongside impl spec. Documents happy path + 8 failure modes + single-instance safety + 4 smoke commands. Closes P2 backlog #3 (operator-side). |
