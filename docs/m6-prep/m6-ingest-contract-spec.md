---
title: Apex M6 Ingest Pipeline Contract Spec
status: frozen
version: v0.1-frozen-2026-05-17
date: 2026-05-17
owner: Parallax-Kernel
consumers: parallax/apex/aphelion_ingest.py (US-201), parallax/cli.py ingest subcommand (US-204)
upstream:
  - aphelion-graph/spec/v0.3-claim-semantics.md (claim semantics R1-R4)
  - aphelion-graph package (verifier, unpacker, read_adapter, trust, v03_validator)
  - Parallax/docs/m5-prep/audit-db-path-config.md §6 (audit_row schema + reason_code namespaces)
  - Parallax/docs/m5-prep/apex-m5-envelope-spec.md §8.1 (write-order invariant)
  - Parallax/docs/m6-prep/m6-readiness-checklist.md (original §3.2 Q-M6.1~Q-M6.4)
pivot:
  - 2026-05-16 noon Chris 拍 路線 A — build .aphelion.tar → claim mapping ingest pipeline (canonical wire), NOT 路線 B (stub claim_loader reads JSON dir)
---

# Apex M6 Ingest Pipeline Contract Spec

> **Note**: This spec is the M6 critical-path artifact per 2026-05-16 noon route-A pivot. The original `m6-readiness-checklist.md §1` "self-memory dual-read dog-fooding" narrative is deferred to **M6.5 / M7** (the ingest pipeline must land first; self-memory dual-read is a downstream consumer).

## 1. Decisions (Q-M6.1 ~ Q-M6.4)

The original `m6-readiness-checklist.md §3.2` Q-M6.1 ~ Q-M6.4 were framed under the pre-pivot narrative ("Parallax dog-foods its own memory via dual-read on self-memory queries"). Three of the four questions are reframed below for the route-A scope. Original Q-M6.1 (self-memory dual-read scope) is deferred and recorded at the end of this section.

### Q-M6.1 — Ingest trigger model

**Question**: How does the `.aphelion.tar` package get into the audit chain — manual CLI invocation, automatic file watcher (inotify), or scheduled poll?

**Decision**: Manual CLI invocation (`parallax ingest <path-to-aphelion-tar>`). The M6 ingest pipeline is a CLI command that takes one or more `.aphelion.tar` paths, runs manifest validation + signer verification + claim mapping extraction, and writes the resulting audit rows into the configured audit DB (per `audit-db-path-config.md`). No file watcher, no cron poll.

Rationale:
1. M6 critical path is the ingest pipeline itself — the wire contract from `.aphelion.tar` → claim mapping → audit row — not the trigger mechanism.
2. Chris is the sole operator; manual invocation matches the solo-dev / offline / YAGNI constraint carried from `aphelion-graph/spec/v0.3-claim-semantics.md §2.4`.
3. File watchers (inotify on Linux, ReadDirectoryChangesW on Windows) add cross-platform complexity and a daemon lifecycle that the M5 burn-in stack already barely manages (see `project_messier_v4_v5_progress.md` 2026-05-16 deploy session — systemd sandbox bugs).
4. Watcher/poll can be added in M7 as a convenience layer on top of the CLI contract.

**Tradeoff acknowledged**: Manual CLI means zero automation — every new `.aphelion.tar` package requires a human `parallax ingest` invocation. If ingest volume exceeds ~10 packages/day, the operator burden becomes non-trivial. This is acceptable for M6 (solo dev, low ingest volume) but must be revisited before any multi-user or automated-producer scenario.

**Cross-ref**: `m6-readiness-checklist.md §3.1` E.M6.2 (`PARALLAX_APHELION_PACKAGE_DIR` populated by SOMETHING); `apex-m5-entry-spec.md §4` bullet 5 (no package-fetch logic); `aphelion-graph/spec/v0.3-claim-semantics.md §2` constraint 4 (solo-dev / offline / YAGNI)

---

### Q-M6.2 — Signer trust key distribution

**Question**: How does the Parallax host learn which Aphelion signer fingerprints are trusted? (file-based trust store, env-var allow-list, or first-use-trust-on-deploy)

**Decision**: File-based trust store at a configurable path (`PARALLAX_APHELION_TRUST_STORE`), containing one PEM-encoded public key per `.pem` file. The ingest CLI reads all `.pem` files in the trust store directory at invocation time (not cached across invocations).

Rationale:
1. No server restart required on key rotation — drop a new `.pem` file, remove the old one, next `parallax ingest` picks it up.
2. Env-var allow-list (fingerprint strings) is fragile for multi-key scenarios and requires shell quoting discipline.
3. First-use-trust (TOFU) is inappropriate — the ingest pipeline is security-critical (signer verification is mandatory per `apex-m5-entry-spec.md §3.1a` "No `--require-signed=false` escape hatch"), and TOFU silently trusts the first package's signer without operator confirmation.
4. File-based approach mirrors the Aphelion v0.5 signer's own key storage pattern, keeping mental model consistent.

**Tradeoff acknowledged**: File-based trust store requires the operator to manually place `.pem` files before first ingest. There is no automated key discovery or certificate-authority chain. For a solo-dev deployment this is fine; for any multi-host scenario (e.g., ZenBook ingesting packages signed on Win), the operator must manually copy the public key cross-host. Federation key distribution is explicitly deferred (see Q-M6.4).

**Cross-ref**: `apex-m5-entry-spec.md §3.1a` signer verification invariant; `apex-m5-entry-spec.md §7` OQ6 (Signer key distribution — Chris owns this op decision); `audit-db-path-config.md §6.3` `signer.` namespace prefix for trust-related reason_codes

---

### Q-M6.3 — Aphelion v0.4 evidence schema gate

**Question**: When Aphelion v0.4 lands (richer evidence binding: role, capture_ts, source_uri, excerpt_range, original_hash), does M6 ingest pipeline ship as v0.3-compatible NOW and upgrade later (ship-then-upgrade), OR block M6 until v0.4 lands?

**Decision**: Ship-then-upgrade. M6 ingest pipeline ships against Aphelion v0.3 R1-R4 claim semantics. When v0.4 evidence schema lands, the pipeline adds parsing for the new fields without breaking existing ingested packages.

Rationale:
1. The additive-only invariant is verified — `aphelion-graph/spec/v0.3-claim-semantics.md §1` Migration states "additive to wire format 2.0 ... v0.4 readers ignoring these fields MUST still validate every previously-conforming v0.4 package unchanged", and `§2` constraint 1 states "no existing v0.4 field is removed or has its meaning changed".
2. PR #51 already landed a backward-compat fixture (`tests/fixtures/v04_to_v03_backward_compat/claim_v04_simple.yaml` + `test_v04_v03_backward_compat.py`) proving v0.4 frontmatter passes the v0.3 validator with zero errors.
3. The M5 entry spec explicitly defaulted to ship-then-upgrade (`apex-m5-entry-spec.md §3.1b` row 2: "M5 can use v0.3 minimal evidence for now"; `§7` OQ5: "Default: ship-then-upgrade").
4. Blocking M6 on v0.4 would create an unbounded dependency — v0.4 has no committed timeline.

**Tradeoff acknowledged**: Packages ingested under v0.3 will lack the richer evidence binding (role, capture_ts, source_uri, excerpt_range, original_hash). If v0.4 later makes any of these fields REQUIRED (not just optional), a backfill or re-ingest of existing packages would be needed. The additive-only invariant makes this unlikely but not impossible if a future spec introduces a new REQUIRED field (which would itself be a wire-format bump per `v0.3-claim-semantics.md §6.2` evolution rule).

**Cross-ref**: `aphelion-graph/spec/v0.3-claim-semantics.md §1` Migration + `§2` constraint 1 (additive-only invariant); `tests/fixtures/v04_to_v03_backward_compat/claim_v04_simple.yaml` (landed in PR #51); `apex-m5-entry-spec.md §3.1b` row 2 + `§7` OQ5 (ship-then-upgrade default)

---

### Q-M6.4 — Federation scope for M6 ingest

**Question**: Does M6 ingest pipeline support cross-instance package_dir (Win sees ZenBook's `.aphelion` packages via federation peers/), OR M6 is strictly single-instance only?

**Decision**: Strictly single-instance. M6 ingest reads `.aphelion.tar` packages from the local `PARALLAX_APHELION_PACKAGE_DIR` only. No cross-instance package discovery, no `peers/<other>/` traversal, no remote fetch.

Rationale:
1. Federation policy is mutual read-only (`feedback_federation_peers_readonly.md`): each instance treats peer content as a read-only mirror; only writes its own writable area. Ingest is a write operation (claim mapping → local audit DB), and ingesting from a peer's read-only mirror into local writable would require careful provenance tracking that M6 does not spec.
2. The original M9 federation milestone was pushed back by Orbit V2 insertion (`project_orbit_v2_strategy.md` Phase 3). Federation is now an M11+ concern.
3. Audit chains are already per-host by design (`audit-db-path-config.md §7`: "There is no requirement to merge the Windows and Linux audit trails. Each host's envelope `audit_db_ref` resolves only against that host's `audit.db`"). Cross-instance ingest would break this per-host audit isolation.
4. The 2026-05-12 tar-pipe incident (`feedback_federation_peers_readonly.md`) demonstrated the concrete risk of cross-instance content mixing.

**Tradeoff acknowledged**: Each host must independently receive its own `.aphelion.tar` packages. There is no mechanism for a package produced on Win to automatically appear in ZenBook's ingest pipeline (or vice versa). An operator who wants both hosts to ingest the same package must manually copy the `.aphelion.tar` file and run `parallax ingest` on each host separately. This is the correct tradeoff for M6 given the federation roadmap timeline and the mutual-read-only invariant.

**Cross-ref**: `feedback_federation_peers_readonly.md` (mutual read-only policy + 2026-05-12 incident); `audit-db-path-config.md §7` (per-host audit isolation); `project_orbit_v2_strategy.md` Phase 3 (federation pushed to M11+); `m6-readiness-checklist.md §3.2` Q-M6.4 (original question)

---

### Note on original Q-M6.1 (self-memory dual-read scope)

The original `m6-readiness-checklist.md §3.2` Q-M6.1 asked about self-memory dual-read behavior ("does the agent's context query bypass cache or always go through dual-read?"). Per the 2026-05-16 noon route-A pivot, that question is **deferred to M6.5 / M7**. The ingest pipeline (this spec) must land first; self-memory dual-read is a downstream consumer that depends on a populated audit DB.

When M6.5 / M7 scope opens, the deferred question regains relevance and must be answered before any code lands that wires self-memory queries through `DualReadRouter`.

---

## 2. Manifest validation contract

### 2.1 Aphelion API integration

Parallax M6 ingest invokes Aphelion's end-to-end verifier — it does NOT re-implement manifest hash / fileset / provenance-chain / signature checks. The single entry point:

```python
from aphelion.verifier import verify_package, VerifyResult
from aphelion.errors import SemanticError, VerificationError, SecurityError, SchemaError
from aphelion.signer import SignerVerificationError, SignatureEnvelope, SignerManifest
from aphelion.unpacker import extract_signer_manifests

result: VerifyResult = verify_package(
    tar_path,
    require_signed=True,   # M6 invariant: unsigned packages REJECTED (no escape hatch)
    require_notary=False,  # v0.5 notary is stub-only; defer to v0.6+
)
# result.envelopes  : tuple[SignatureEnvelope, ...] — each carries .signer_id,
#                     .algorithm, .signed_at_iso, .package_canonical_hash, .signature_b64
# result.attestations: tuple[NotaryAttestation, ...] — "verified-locally" until v0.6+
```

**Note**: `SignatureEnvelope` does NOT carry `key_fingerprint`. That field lives on `SignerManifest` (at `signers/<signer_id>.json` inside the tar). M6 ingest MUST resolve the matching `SignerManifest` for each envelope via `extract_signer_manifests(tar_path)[envelope.signer_id]` to access `key_fingerprint` — see §3 trust verification.

### 2.2 Inputs

| Field | Type | Source | Constraint |
|---|---|---|---|
| `tar_path` | `pathlib.Path` | CLI arg → resolved to absolute path | MUST be `.aphelion.tar` extension; MUST resolve under `PARALLAX_APHELION_PACKAGE_DIR` (no `..` escape); CLI accepts exactly ONE path per invocation (per §5.3) |

### 2.3 Outputs

On success: a `VerifyResult` (frozen dataclass) is returned. The ingest pipeline uses `result.envelopes` to feed §3 provenance verification.

### 2.4 Error cases

`verify_package` is internally a 4-step chain (unpack → v0.4 semantic → v0.5 signature → notary). M6 ingest catches each error class and re-raises as a `ParallaxIngestError` with a `reason_code` in the `pkg.*` or `signer.*` namespace per `audit-db-path-config.md §6.3`. NO bare `except:` — every error path is explicit.

**Exit code reference**: §6.3 is the normative full table. The mapping here is the §2-scoped subset; §6.2 is the canonical full reason_code → exit code mapping.

| Aphelion exception | reason_code | Parallax exit code |
|---|---|---|
| `SecurityError` (untar safety: PATH_TRAVERSAL, ARCHIVE_BOMB, etc.) | `pkg.archive_unsafe` | 65 |
| `SemanticError` (FILESET_DIVERGENCE, CHAIN_BROKEN, DANGLING_REFERENCE) | `pkg.semantic_invalid` | 65 |
| `VerificationError` (HASH_MISMATCH) | `pkg.hash_mismatch` | 65 |
| `SignerVerificationError("E_SIGNER_REQUIRED")` | `pkg.unsigned` | 65 |
| `SignerVerificationError` (other — signature invalid) | `signer.signature_invalid` | 65 |
| `SchemaError` (v0.3 claim frontmatter invariant) | `claim.format_invalid` | 65 |
| `FileNotFoundError` (tar_path missing) | `pkg.not_found` | 65 |
| `PermissionError` (read denied on tar / unpack dir) | `disk.permission` | 71 |

### 2.5 Invariants

- **I-2.1** `require_signed=True` is hard-coded in M6 ingest — no `--allow-unsigned` CLI flag, no env var override. (Mirrors `apex-m5-entry-spec.md §3.1a` invariant.)
- **I-2.2** Unpack always uses Aphelion's default `ExtractPolicy` (100 MiB total / 25 MiB single-file / 10k file count / path-length 512). M6 does NOT override these limits.
- **I-2.3** Verification is total — partial-success is rejected. Any error in steps 1-4 means the package is NOT ingested and NO audit row is written.

### 2.6 Out-of-scope (M6)

- Custom `ExtractPolicy` overrides (M7+ may add `--strict` / `--lenient` knobs)
- Notary verification (`require_notary=True`) — Aphelion v0.5 stub-only; M6 ships `require_notary=False`
- Partial-package ingest (claim subset extraction) — full-package atomic-or-reject only
- Multi-signer packages — M6 hard-rejects when `len(result.envelopes) > 1` with `signer.multi_sig_unsupported` (exit 65). M7+ may add multi-sig support.
- Empty packages — M6 rejects packages where `manifest["claims"]` is empty with `pkg.empty_package` (exit 65). Rationale: an empty ingest produces 0 audit rows, which is observably indistinguishable from a no-op operator mistake; reject explicitly so the operator notices.

## 3. Provenance verifier contract

### 3.1 Two-layer trust model

Aphelion v0.5 verifies the cryptographic validity of signatures embedded in the package (signer's self-attested public key + signature over canonical bytes). It does NOT decide which signers are trustworthy — that is the operator's policy.

M6 introduces a Parallax-side trust enforcement layer ON TOP OF Aphelion's signature verification:

```
.aphelion.tar
  ├─ signatures.jsonl       ← Aphelion verifies cryptographic correctness
  ├─ signers/<id>.json      ← SignerManifest: signer_id, algorithm, public_key_b64,
  │                            key_fingerprint, notary_uri
  └─ ...
                            ↓
Parallax trust enforcement (M6):
  For each envelope in result.envelopes:
    1. Resolve SignerManifest via extract_signer_manifests(tar_path)[envelope.signer_id]
    2. Is `envelope.signer_id` listed in PARALLAX_APHELION_TRUST_STORE/*.pem?
    3. Does `signer_manifest.key_fingerprint` match the .pem file's fingerprint?
                            ↓ yes (all envelopes) → ingest proceeds
                            ↓ no  → reject with signer.untrusted OR signer.fingerprint_mismatch
```

### 3.2 Trust store contract

Per Q-M6.2 decision (§1):

| Property | Value |
|---|---|
| Env var | `PARALLAX_APHELION_TRUST_STORE` |
| Type | Absolute path to a directory |
| File format | One `.pem` per trusted signer; filename irrelevant (signer_id derived from key fingerprint hashing) |
| Reload semantics | Re-read directory on each `parallax ingest` invocation; no in-memory cache between invocations |
| Empty trust store | All ingests REJECTED with `signer.untrusted`. NO default-trust-all bootstrap. |
| Missing trust store dir | Startup-fail with `EX_CONFIG (78)` — same exit-code convention as `PARALLAX_AUDIT_DB_PATH` validation per `audit-db-path-config.md §4` bullet 8 |

### 3.3 Verification algorithm

```python
def verify_trust(
    envelopes: tuple[SignatureEnvelope, ...],
    tar_path: Path,
    trust_store_dir: Path,
) -> tuple[TrustDecision, ...]:
    """For each envelope, decide trust based on operator's trust store.

    Returns one TrustDecision per envelope. Caller raises
    ParallaxIngestError if any decision is REJECTED.
    """
    trusted_fingerprints = _load_trust_store(trust_store_dir)  # set[str]
    raw_manifests = extract_signer_manifests(tar_path)  # Mapping[str, bytes]
    decisions: list[TrustDecision] = []
    for env in envelopes:
        manifest_bytes = raw_manifests.get(env.signer_id)
        if manifest_bytes is None:
            decisions.append(TrustDecision.rejected(
                signer_id=env.signer_id,
                reason_code="signer.manifest_missing",
            ))
            continue
        manifest = _parse_signer_manifest(manifest_bytes)  # SignerManifest
        if manifest.key_fingerprint not in trusted_fingerprints:
            decisions.append(TrustDecision.rejected(
                signer_id=env.signer_id,
                reason_code="signer.untrusted",
            ))
            continue
        decisions.append(TrustDecision.accepted(signer_id=env.signer_id))
    return tuple(decisions)
```

`TrustDecision` is a `@dataclass(frozen=True)` — returned as new objects per immutability invariant.

### 3.3a Trust store validation ordering (relative to §5.2)

Trust store validation runs as an **additional §5.2 gate after gate 6**:

7. **`PARALLAX_APHELION_TRUST_STORE` env present + non-empty**. Unset/empty → `pkg.trust_store_missing` → exit 78.
8. **Trust store dir exists, readable, contains ≥1 `.pem`**. Empty dir → all envelopes will reject with `signer.untrusted` (per §3.4); missing dir → exit 78.
9. **All `.pem` files parseable**. Any malformed file → `signer.trust_pem_invalid` → exit 78.

These gates run BEFORE `verify_package()` so operator config errors surface fast without spending tar-unpacking I/O.

### 3.4 Failure modes

| Condition | reason_code | Exit code |
|---|---|---|
| Trust store dir missing | `pkg.trust_store_missing` | 78 (EX_CONFIG) |
| Trust store env unset | `pkg.trust_store_missing` | 78 |
| Trust store dir present but empty | `signer.untrusted` (per-envelope) | 65 |
| Envelope signer_id has no matching `signers/<id>.json` in tar | `signer.manifest_missing` | 65 |
| Resolved SignerManifest.key_fingerprint not in trust store | `signer.untrusted` | 65 |
| Trust store `.pem` fingerprint differs from manifest.key_fingerprint | `signer.fingerprint_mismatch` | 65 |
| Trust store `.pem` unparseable | `signer.trust_pem_invalid` | 78 (operator config error) |
| Multiple envelopes in package | `signer.multi_sig_unsupported` | 65 (M6 single-signer only; M7+) |

### 3.5 Out-of-scope (M6)

- Key rotation grace period (accept old + new fingerprint for N hours) — M7+
- Multi-sig requirements (require N-of-M signers) — M7+
- Certificate authority chain validation — M7+ if Aphelion v0.6 introduces CA chains
- Online revocation check (CRL/OCSP) — never in this architecture (offline-first per `v0.3-claim-semantics.md §2`)

## 4. Claim mapping extractor contract

### 4.1 Purpose

For each verified, trusted package, extract the v0.3 R1-R4 claim frontmatters from the unpacked tree and map them to `audit_row` records per the schema in `audit-db-path-config.md §6`. Each ingested claim produces exactly one audit row.

### 4.2 Inputs

| Field | Type | Source |
|---|---|---|
| `unpacked_dir` | `pathlib.Path` | output of `aphelion.unpacker.unpack()` (called internally by `verify_package`) |
| `manifest` | `dict[str, Any]` | parsed `manifest.json` from unpacked tree |
| `envelopes` | `tuple[SignatureEnvelope, ...]` | from `VerifyResult.envelopes` |
| `package_id` | `str` (UUID v7) | from `manifest["package_id"]` |
| `session_id` | `str` | Parallax session correlator from CLI invocation context (e.g. `"ingest:<ts>:<uuid4>"`) |

### 4.3 Output

```python
@dataclass(frozen=True)
class ClaimMappingBatch:
    package_id: str
    signer_id: str           # from envelopes[0].signer_id (M6 single-signer; multi-sig hard-rejected per §3.4)
    signer_manifest_digest: str  # from envelopes[0].package_canonical_hash
                             # (this is the actual SignatureEnvelope field name — the audit_row
                             #  schema field is named `signer_manifest_digest` per audit-db-path-config.md §6.1,
                             #  but it stores the envelope's package_canonical_hash byte-identical;
                             #  M6 never unsigned per I-2.1, so this is always a non-empty 64-char hex string)
    audit_rows: tuple[AuditRow, ...]
    timestamp: str           # ISO 8601 UTC Z, second precision; reused across all rows in batch
```

`AuditRow` is the existing immutable dataclass from `parallax.apex.audit_writer.AuditRow` (NOT a new type — reuses M5 contract).

### 4.4 Per-claim mapping rules

For each claim frontmatter parsed via `aphelion.canonical_json.loads`:

| audit_row field | Source | Notes |
|---|---|---|
| `claim_id` | `frontmatter["claim_id"]` | UUID v7 string, validated by `aphelion.v03_validator.validate_v03_fields` |
| `envelope_message_id` | freshly generated UUID v4 per audit row | NOT reused across rows in the batch — each audit row is independent for idempotency (per `audit-db-path-config.md §6.1` UNIQUE constraint) |
| `outcome` | always `"hit"` for ingest path | Ingest is a write — there is no dual-read miss/divergence/error here. Future M6.5/M7 self-memory dual-read may produce other outcomes. |
| `package_id` | input `package_id` | same for all rows in batch |
| `session_id` | input `session_id` | same for all rows in batch |
| `signer_id` | from `envelopes[0]` | M6 single-signer only; multi-sig in M7+ |
| `signer_manifest_digest` | `envelopes[0].package_canonical_hash` | The `SignatureEnvelope` field is named `package_canonical_hash`; the audit_row schema field is named `signer_manifest_digest` (per `audit-db-path-config.md §6.1`). Same bytes, different field name on each side. Zero extra cost per `audit-db-path-config.md §6.5` Q4. |
| `source` | always `"aphelion"` | per audit-db-path-config.md §6.1 source enum |
| `ts` | batch `timestamp` | second-precision UTC Z, same for all rows |

### 4.5 R4-trigger field handling

The four R4-trigger fields (`polarity` / `valid_from` / `valid_until` / `supersedes`) per `v0.3-claim-semantics.md` ADR-0002 do NOT appear in the audit_row directly — the audit row records *that an ingest happened*, not the claim's semantic content. Conflict detection on read is the M6.5/M7 consumer's job via `AphelionReadAdapter.query()`.

However, R4-trigger field validation IS enforced at ingest time:

- `validate_v03_fields(frontmatter)` runs on every claim before mapping → invalid claim raises `SchemaError` → mapped to `claim.format_invalid` exit 65
- If R4-trigger field present without `subject`, `SchemaError(CLAIM_SUBJECT_REQUIRED_FOR_CONFLICT)` → exit 65
- If two claims in the same package have identical `(subject, polarity, valid_from)` keys (logical duplicate per spec), reject with `claim.duplicate_in_package` → exit 65

### 4.6 Supersession chain handling

A claim's `supersedes` list points to claim_ids of older claims. M6 ingest:

1. Writes audit rows for ALL claims in the package (including those that are superseded — they exist on disk and the ingest record proves their presence)
2. Does NOT modify any prior audit row when a supersession is ingested (audit chain is append-only)
3. Records the supersession relationship via the `claim_id` linkage already present in the canonical claim frontmatter on disk — the audit row's `claim_id` field is sufficient for downstream reconstruction

In the 70-fixture supersession bucket (30 claim mappings = 15 pairs), all 30 claims MUST produce audit rows. The dual-read consumer (M6.5+) is responsible for surfacing the active claim via `AphelionReadAdapter.query()`.

### 4.7 Write-order invariant (inherited from M5)

For each audit row, the write order is FIXED per `apex-m5-envelope-spec.md §8.1`:

```
canonicalize_row(row)
  → write_audit_row(audit_conn, canonicalized)
  → assert_audit_row_committed(committed)
  → ONLY THEN any subsequent sha256/envelope work
```

M6 ingest reuses `parallax.apex.audit_writer.write_audit_row` directly — does NOT re-implement the write path, does NOT bypass the assertion fence. Any change to write ordering MUST go through the M5 audit_writer module, not through M6 ingest.

### 4.8 Out-of-scope (M6)

- Multi-signer packages (M7+: write multiple `signer_id` values via additional audit_row columns or composite row)
- Partial-package ingest (claim subset) — all-or-reject per §2 I-2.3
- Rewriting prior audit rows on supersession — audit chain is append-only
- Cross-package supersession resolution at ingest time (left to read-side `AphelionReadAdapter.query()`)

## 5. `PARALLAX_APHELION_PACKAGE_DIR` resolver

### 5.1 Env var contract

| Property | Value |
|---|---|
| Env var | `PARALLAX_APHELION_PACKAGE_DIR` |
| Type | Absolute filesystem path (Windows or POSIX) |
| Requirement | REQUIRED for `parallax ingest`; absence raises `EX_CONFIG (78)` |
| Suggested Windows dev | `E:\Parallax\data\aphelion-packages\` (mirrors `feedback_e_drive_data.md` convention) |
| Suggested Linux ZenBook | `/home/chris/parallax-data/aphelion-packages/` (mirrors `audit-db-path-config.md §3` path convention) |

NO default. The operator MUST set the env var before each `parallax ingest` invocation. (Mirrors `audit-db-path-config.md §3` "no implicit default" pattern — same rationale: operator-explicit path discipline.)

### 5.2 Path validation gates (cold-load, no watcher)

On every `parallax ingest <package_path>` invocation, the CLI runs these gates in order BEFORE invoking Aphelion `verify_package()`:

1. **Env var present and non-empty.** Unset / empty → `EX_CONFIG (78)`.
2. **Resolves to absolute path with no `..` segments.** Both syntactic + `Path.resolve()` checks.
3. **Directory exists and is readable.** `os.access(dir, os.R_OK)` → `EX_CONFIG (78)` on failure.
4. **`<package_path>` resolves under `PARALLAX_APHELION_PACKAGE_DIR`.** Reject any path that resolves outside the dir (defence against operator typo / symlink trick). On violation: `pkg.path_escape` reason_code → `EX_DATAERR (65)`.
5. **`<package_path>` ends in `.aphelion.tar`.** Reject other extensions → `pkg.extension_invalid` → exit 65.
6. **`<package_path>` is a regular file (not symlink / dir / device).** `Path.is_file()` AND `not Path.is_symlink()` → `pkg.not_regular_file` → exit 65.

### 5.3 Explicitly NOT in M6

- File watcher (`inotify` / `ReadDirectoryChangesW`) — per Q-M6.1, M6 is manual-CLI-only. Watcher deferred to M7.
- Scheduled poll (`cron` / systemd timer) — same reason.
- Background daemon that auto-ingests new packages — same reason.
- Glob batch ingest (`parallax ingest pkg-*.tar`) — M6 CLI accepts ONE package_path per invocation. Operator runs the CLI multiple times for batch. Glob is a M7 ergonomic addition.

### 5.4 Cold-load semantics

Each invocation:

1. Reads env vars fresh (no daemon caching them at startup).
2. Reads trust store directory fresh (per §3.2 reload semantics).
3. Opens audit DB fresh via existing `parallax.apex.audit_db.open_audit_db(validate=True)` (M5 PR #55 lifespan path) — closes connection on exit.
4. No state survives between invocations except: (a) the audit DB rows written, (b) the unpacked package tree (handled by Aphelion's `verify_package` via tempfile.TemporaryDirectory which auto-cleans on context exit).

This makes the CLI safely idempotent — re-running `parallax ingest <same-pkg>` produces the same audit rows (modulo `envelope_message_id` UUID v4 generation) but the UNIQUE constraint on `envelope_message_id` makes duplicate detection a separate problem (see §6).

## 6. Error taxonomy + reason_code

### 6.1 Namespace alignment with M5

M6 ingest extends the namespaced reason_code taxonomy established in `audit-db-path-config.md §6.3`. M5 reserved 6 namespaces (`pkg.` / `signer.` / `cache.` / `network.` / `disk.` / `claim.`). M6 keeps the namespace set unchanged — all new reason_codes fit under the existing namespaces:

| Prefix | Usage | M6 additions (relative to M5) |
|---|---|---|
| `pkg.` | Package-level data / config / path issues | `pkg.not_regular_file`, `pkg.extension_invalid`, `pkg.path_escape`, `pkg.trust_store_missing`, `pkg.empty_package`, `pkg.dir_unset`, `pkg.idempotency_duplicate` |
| `signer.` | Signature / trust issues | `signer.signature_invalid`, `signer.untrusted`, `signer.fingerprint_mismatch`, `signer.trust_pem_invalid`, `signer.manifest_missing`, `signer.multi_sig_unsupported` |
| `claim.` | Claim-content issues | `claim.format_invalid`, `claim.duplicate_in_package`, `claim.subject_required_for_r4` |
| `disk.` | Filesystem / OS-level issues | `disk.permission`, `disk.audit_db_write_failed`, `disk.audit_db_unset` |
| `cache.` | Reserved (not used in M6 ingest) | — |
| `network.` | Reserved (not used in M6 — file-only) | — |

**Why no `ingest.*` namespace**: an earlier spec draft proposed `ingest.*` as a new namespace, but `parallax.apex.audit_writer.REASON_CODE_PREFIXES` (M5 PR #55) is a closed tuple of the 6 M5 namespaces and would reject any audit row whose `reason_code` starts with `ingest.`. Reclassifying under the existing namespaces avoids a coupled M5 schema bump.

### 6.2 Full reason_code list (M6)

| reason_code | When raised | Spec section | Exit code |
|---|---|---|---|
| `pkg.not_found` | `<package_path>` does not exist (FileNotFoundError from Aphelion) | §2.4 | 65 |
| `pkg.not_regular_file` | path is symlink / directory / device | §5.2 gate 6 | 65 |
| `pkg.extension_invalid` | path does not end in `.aphelion.tar` | §5.2 gate 5 | 65 |
| `pkg.path_escape` | path resolves outside `PARALLAX_APHELION_PACKAGE_DIR` | §5.2 gate 4 | 65 |
| `pkg.archive_unsafe` | Aphelion `SecurityError` during unpack (PATH_TRAVERSAL / ARCHIVE_BOMB / etc.) | §2.4 | 65 |
| `pkg.semantic_invalid` | Aphelion `SemanticError` (FILESET_DIVERGENCE / CHAIN_BROKEN / DANGLING_REFERENCE) | §2.4 | 65 |
| `pkg.hash_mismatch` | Aphelion `VerificationError` (manifest hash ↔ claim file mismatch) | §2.4 | 65 |
| `pkg.unsigned` | `verify_package(require_signed=True)` raised `SignerVerificationError("E_SIGNER_REQUIRED")` | §2.4 | 65 |
| `pkg.empty_package` | `manifest["claims"]` is empty list — operator mistake guard | §2.6 | 65 |
| `pkg.trust_store_missing` | `PARALLAX_APHELION_TRUST_STORE` env unset OR dir missing | §3.4 + §3.3a gate 7 | 78 (EX_CONFIG) |
| `pkg.dir_unset` | `PARALLAX_APHELION_PACKAGE_DIR` env unset | §5.1 | 78 |
| `pkg.idempotency_duplicate` | re-ingest attempt with same `envelope_message_id` triggered UNIQUE constraint (defence-in-depth; if seen, UUID collision bug) | §5.4 | 70 (EX_SOFTWARE) |
| `signer.signature_invalid` | Aphelion `SignerVerificationError` (signature cryptographically invalid) | §2.4 | 65 |
| `signer.untrusted` | resolved `SignerManifest.key_fingerprint` not in trust store | §3.4 | 65 |
| `signer.fingerprint_mismatch` | trust store `.pem` fingerprint differs from `SignerManifest.key_fingerprint` for same `signer_id` | §3.4 | 65 |
| `signer.manifest_missing` | envelope's `signer_id` has no matching `signers/<id>.json` in tar | §3.4 | 65 |
| `signer.multi_sig_unsupported` | `len(result.envelopes) > 1` — M6 single-signer only | §2.6 + §3.4 | 65 |
| `signer.trust_pem_invalid` | trust store `.pem` file is unparseable | §3.4 + §3.3a gate 9 | 78 |
| `claim.format_invalid` | Aphelion `SchemaError` from `validate_v03_fields()` (other than CLAIM_SUBJECT_REQUIRED_FOR_CONFLICT) | §2.4 + §4.5 | 65 |
| `claim.duplicate_in_package` | two claims in same package share `(subject, polarity, valid_from)` | §4.5 | 65 |
| `claim.subject_required_for_r4` | claim has R4-trigger field but no `subject` (Aphelion `SchemaError(CLAIM_SUBJECT_REQUIRED_FOR_CONFLICT)`) — separate code from generic `claim.format_invalid` to give operators a clearer fix signal | §4.5 | 65 |
| `disk.permission` | `PermissionError` on tar read / unpack dir write / audit DB write | §2.4 | 71 (EX_OSERR) |
| `disk.audit_db_write_failed` | `AuditDbWriteError` from `write_audit_row()` | §4.7 | 71 |
| `disk.audit_db_unset` | `PARALLAX_AUDIT_DB_PATH` env unset | inherited M5 §4 | 78 |

### 6.3 Exit code summary

| Code | Sysexits meaning | M6 usage |
|---|---|---|
| 0 | success | All claims in package ingested + all audit rows committed |
| 65 | EX_DATAERR | Data is malformed (package / signer trust / claim) — operator fixes the package |
| 70 | EX_SOFTWARE | Internal Parallax bug (e.g. UUID collision) — file an issue |
| 71 | EX_OSERR | OS-level error (disk permission / IO) |
| 78 | EX_CONFIG | Operator config error (env var unset / trust store missing) |

NO exit codes 1-63 used. NO exit code 64 (EX_USAGE) — handled by argparse before ingest logic runs.

### 6.4 Logging shape

Every reason_code emits a single structured log line:

```json
{
  "event": "parallax_ingest_failed",
  "reason_code": "pkg.archive_unsafe",
  "package_path": "<resolved abs path>",
  "signer_id": "<envelope.signer_id or empty>",
  "underlying": "<aphelion exception class + msg, str()-truncated to 256 chars>"
}
```

(On success, emit `parallax_ingest_succeeded` with `claim_count` + `audit_rows_written` + `signer_id` + `package_id`.)

## 7. Integration test plan

### 7.1 Fixture corpus

70 R4-diverse claim mappings shipped from ZenBook (`/home/chris/parallax-data/claim-fixtures/` per 2026-05-16 Task B output) into `tests/fixtures/m6_claim_mappings/` (committed in US-105). 4 buckets per the 5/16 smoke output:

| Bucket | Count | What it tests |
|---|---|---|
| `not_found/` | 20 | Subject not in package OR R2 valid-time window exclusion |
| `supersession/` | 30 (15 pairs) | R4 supersession detection — newer claim supersedes older |
| `expired/` | 10 | `valid_until` strictly < query_time |
| `conflict/` | 10 (5 pairs) | v0.3 schema violations: affirm+deny on same subject (rejected at validation) |

### 7.2 Test file location

`tests/integration/test_m6_ingest_pipeline.py` — pytest `@pytest.mark.integration` per `rules/python/testing.md`.

### 7.3 Per-bucket expected outcomes

| Bucket | Expected outcome | Audit rows written |
|---|---|---|
| `not_found/` (20) | Ingest succeeds for each fixture (claim shape is valid); 20 audit rows total. Read-side `AphelionReadAdapter.query()` later returns `ConflictClass.NOT_FOUND` for these subjects at query time. | 20 |
| `supersession/` (30 = 15 pairs) | Ingest succeeds for both claims in each pair; 30 audit rows total. Audit chain is append-only — superseding claim does NOT modify older claim's row. | 30 |
| `expired/` (10) | Ingest succeeds (the claim's expiry is read-side concern, NOT ingest-side); 10 audit rows. | 10 |
| `conflict/` (10 = 5 pairs) | Ingest REJECTS each conflict claim with `claim.format_invalid` (v0.3 validator rejects affirm+deny on same subject); 0 audit rows. Exit code 65 per fixture. | 0 |

**Total audit rows from clean 70-fixture run: 60.**

### 7.4 Test isolation

- Each test creates a fresh disposable audit DB via `pytest tmp_path` fixture → `tmp_path / "m6_test_audit.db"`. NEVER touches `/home/chris/parallax-kernel/db/audit.db` (production).
- Each test creates a fresh trust store via `tmp_path / "trust_store"` containing a test-only PEM generated per-session (via `aphelion.signer.generate_keypair()` helper if available, else fixture).
- Each test sets `PARALLAX_APHELION_PACKAGE_DIR` to `tmp_path` and resets after via `monkeypatch.setenv`.
- Each test uses tar fixtures generated on-the-fly by an `aphelion_pkg_builder` pytest helper that creates valid `.aphelion.tar` from claim frontmatter dicts — does NOT depend on pre-existing `.aphelion.tar` files committed to git (the 70 fixtures are claim mappings, not full `.aphelion.tar` archives; the builder wraps them).
- Defensive `--db` guard per 5/16 P2 backlog #5: `assert "parallax-kernel/db" not in str(audit_db_path)` at the top of every test.

### 7.5 Cross-cutting tests

Beyond the 70 fixtures, the test file MUST also exercise:

- `signer.untrusted`: package signed by a key whose fingerprint is NOT in test trust store → exit 65
- `pkg.path_escape`: `<package_path>` resolves outside `PARALLAX_APHELION_PACKAGE_DIR` (e.g. via symlink) → exit 65
- `disk.audit_db_unset`: `PARALLAX_AUDIT_DB_PATH` unset → exit 78
- `pkg.unsigned`: unsigned package (no `signatures.jsonl`) + `require_signed=True` → exit 65
- Idempotency: re-running ingest on same package produces TWO sets of audit rows (different `envelope_message_id`) — confirms M6 has no auto-dedup (intentional per §5.4)

### 7.6 Coverage requirement

`pytest --cov=parallax/apex/aphelion_ingest --cov-report=term-missing` on test_m6_ingest_pipeline.py MUST report ≥80% line coverage on `parallax/apex/aphelion_ingest.py` per `rules/python/testing.md`.

### 7.7 Audit-db throughput stress (sanity-check)

Reuse the `/tmp/audit_db_stress.py` pattern from 2026-05-16 Task C. 1M-row run is sufficient for sanity (5M is optional). SLO MUST hold:

- throughput ≥ 2000 rows/sec sustained (matches 5/16 baseline 2269 rows/sec)
- p99 latency ≤ 10ms (matches 5/16 p99=5.13ms with headroom)
- 0 error rows

Stress test is NOT a pytest integration test — it is a standalone script committed to `scripts/m6_audit_db_stress.py` (NOT `/tmp/` per 5/16 P2 backlog #6).

## 8. Out-of-scope (M6, deferred)

### 8.1 Deferred to M6.5 / M7

- **Self-memory dual-read consumer** — the original `m6-readiness-checklist.md §1` narrative; now M6.5 / M7 work that consumes audit rows produced by this pipeline
- **File watcher trigger** — per Q-M6.1; M7 adds `inotify` / `ReadDirectoryChangesW` as a convenience layer
- **Scheduled poll** — same as above
- **Glob batch CLI** (`parallax ingest pkg-*.tar`) — M7 ergonomic addition
- **`--require-notary=True`** — Aphelion v0.5 notary is stub-only; M7+ wires real notary if Aphelion v0.6 spec lands one

### 8.2 Deferred to M11+

- **Cross-instance package federation** — per Q-M6.4; pushed back by Orbit V2 insertion, target M11+
- **Cross-host audit replay** — `audit-db-path-config.md §7` explicitly says per-host audit chains; merge layer is M11+ federation work
- **Trust store sync across instances** — operator currently copies `.pem` files manually; federation will need an automated key distribution layer

### 8.3 Permanently out-of-scope (architecture invariant)

- **Perihelion 接點** — Perihelion is the private self-model layer with hard boundary "never writes Parallax claim; schema namespace perihelion_*; DPKG export 看到 prefix 必 raise" per `project_perihelion_phase0.md`. M6 ingest is for Aphelion canonical packages only. Perihelion never produces `.aphelion.tar`; M6 ingest never reads Perihelion data. (See `reference_perihelion_naming.md`.)
- **HTTP / network fetch of `.aphelion.tar`** — `apex-m5-entry-spec.md §0` explicitly states Aphelion is a file format, NOT a service. M6 ingest reads local files only; remote fetch is a separate (likely never-built) concern.
- **Partial-package ingest** — atomic all-or-reject per §2 I-2.3. Selective claim ingest would require provenance-chain rebuild, which is out of scope by design.
- **Mutating prior audit rows** — audit chain is append-only per `audit-db-path-config.md §6` + M5 PR #55 contract.

### 8.4 Aphelion v0.4 evidence schema upgrade

Per Q-M6.3 (§1): M6 ships against v0.3 R1-R4 claim semantics. When Aphelion v0.4 lands the richer evidence binding fields (`role`, `capture_ts`, `source_uri`, `excerpt_range`, `original_hash`), the M6 ingest pipeline will gain v0.4 parsing in a follow-up PR (NOT M6 scope). Existing v0.3-ingested packages remain valid per the additive-only invariant.
