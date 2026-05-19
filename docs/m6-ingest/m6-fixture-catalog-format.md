---
title: M6 Claim-Mapping Fixture Catalog Format
status: living
version: v0.1
date: 2026-05-19
owner: Parallax-Kernel
documents:
  - tests/fixtures/m6_claim_mappings/ (70 fixtures, 4 buckets)
companion_specs:
  - docs/m6-ingest/m6-ingest-impl-spec.md
  - docs/m6-prep/m6-ingest-contract-spec.md
---

# M6 Claim-Mapping Fixture Catalog Format

> **What this doc is**: the JSON schema and bucket layout for the 70 R4-diverse claim-mapping fixtures at [tests/fixtures/m6_claim_mappings/](../../tests/fixtures/m6_claim_mappings/). These are *test fixtures only* — they are NOT a production ingest source (M6 is route-A canonical, `.aphelion.tar` only; see impl spec §9.4).

---

## 1. Scope

The 70 fixtures were built on ZenBook 2026-05-16 (ralph session Task B) as the *output shape* that a successful `.aphelion.tar` ingest would produce per claim — one JSON file per claim mapping, four buckets covering the R4 outcome surface (NOT_FOUND / SUPERSESSION / EXPIRED / CONFLICT).

After the 2026-05-16 noon route-A pivot ([m6-ingest-contract-spec.md §1](../m6-prep/m6-ingest-contract-spec.md)), these fixtures became:

- Reference data for the post-ingest claim-mapping output shape
- Integration test corpus for the R4 detection surface
- A coverage matrix verified by `verify_claim_fixtures.py` (M6 PR #58 era)

They are explicitly NOT:

- A production ingest source (M6 ingests `.aphelion.tar` archives, not bare JSON)
- An audit_row schema reference (audit_row schema lives in `parallax.apex.audit_db._SCHEMA_STATEMENTS`)
- A v0.4 Aphelion evidence sample (these are v0.3 minimal-evidence shape)

---

## 2. Directory layout

```
tests/fixtures/m6_claim_mappings/
├── CATALOG.md                       # human-readable index (existing, unchanged)
├── not_found/                       # 20 fixtures — baseline NOT_FOUND coverage
│   ├── 01963f7d-7000-7000-8000-000001000000.json
│   ├── 01963f7d-7000-7000-8000-000001000001.json
│   └── ... (20 total: 000000..00000f, 000010..000013)
├── supersession/                    # 30 fixtures — 15 newer/older pairs
│   ├── 01963f7d-7000-7000-8000-000002000000.json   # base claim
│   ├── 01963f7d-7000-7000-8000-000002000001.json   # supersedes the previous
│   └── ... (30 total: 000000..00001d)
├── expired/                         # 10 fixtures — valid_until in past
│   └── ... (10 total: 000000..000009)
└── conflict/                        # 10 fixtures — 5 affirm+deny pairs (same subject)
    └── ... (10 total: 000000..000009)
```

70 fixtures total. Filename pattern: `<claim_id>.json`, where `<claim_id>` is a UUID v7 with a per-bucket prefix encoded in bytes 13-15 (visible as `000001` / `000002` / `000003` / `000004`).

---

## 3. JSON schema

Every fixture is a single JSON object with the following fields. The schema is a strict subset of the v0.3 claim frontmatter — only the fields the post-ingest mapping needs for R4 testing are included; richer v0.4 evidence binding is deliberately absent.

### 3.1 Field table

| Field | Type | Required | Validation | Notes |
|---|---|---|---|---|
| `claim_id` | string | yes | UUID v7 lowercase, 36 chars with hyphens | MUST equal the filename stem (`<claim_id>.json`) |
| `package_id` | string | yes | UUID v7 lowercase | the synthetic package this claim would belong to; not necessarily unique across fixtures |
| `polarity` | string | yes | one of `"affirm"`, `"deny"` (see §3.3 divergence) | the claim's directional polarity |
| `subject` | string | yes | non-empty, pattern `subject:<scope>:<value>` | the entity the claim is about; identical across pair members in `supersession/` and `conflict/` |
| `supersedes` | array[string] | no | non-empty list of claim_id UUID v7 strings | present ONLY in `supersession/` fixtures where the file is the *newer* member of a pair |
| `valid_from` | string | no | ISO 8601 UTC with `Z` suffix (e.g. `"2025-01-01T00:00:00Z"`) | present in `expired/` fixtures |
| `valid_until` | string | no | ISO 8601 UTC with `Z` suffix | present in `expired/` fixtures; strictly less than the test query time |

### 3.2 Bucket-level schema invariants

| Bucket | Required fields | Optional fields | Pair structure |
|---|---|---|---|
| `not_found/` | `claim_id`, `package_id`, `polarity`, `subject` | none | independent — each fixture is a singleton |
| `supersession/` | `claim_id`, `package_id`, `polarity`, `subject` | `supersedes` (on the *newer* member of each pair) | 15 pairs; older=`...000` newer=`...001`, older=`...002` newer=`...003`, etc. The newer member's `supersedes` array contains exactly one entry: the older member's `claim_id`. |
| `expired/` | `claim_id`, `package_id`, `polarity`, `subject`, `valid_from`, `valid_until` | none | independent — each fixture is a singleton |
| `conflict/` | `claim_id`, `package_id`, `polarity`, `subject` | none | 5 pairs; `...000` and `...001` share the same `subject` but with opposing `polarity` values |

### 3.3 Known divergence — `polarity:"deny"` in `conflict/`

Per impl spec §9.7 and PR #58 reviewer-focus #5: the `conflict/` bucket fixtures use `polarity:"deny"`, but the Aphelion v0.3 validator only accepts the enum `affirm` / `negate` / `unknown`. As a consequence:

- If a fixture from `conflict/` were ever wrapped into a `.aphelion.tar` and fed to `parallax ingest`, it would be rejected with `claim.format_invalid` exit 65.
- The current test corpus documents this reverse-engineered behaviour and asserts the rejection.
- A future corpus fix (rename `deny` → `negate`, or bundle the pair into a single package whose validation produces `claim.duplicate_in_package`) will flip the assertion.

This divergence is contained entirely within the test corpus. It does NOT propagate to the binary contract or to the audit_row schema.

---

## 4. Example fixtures

### 4.1 `not_found/` (baseline singleton)

[tests/fixtures/m6_claim_mappings/not_found/01963f7d-7000-7000-8000-000001000000.json](../../tests/fixtures/m6_claim_mappings/not_found/01963f7d-7000-7000-8000-000001000000.json):

```json
{
  "claim_id": "01963f7d-7000-7000-8000-000001000000",
  "package_id": "01963f7d-7000-7000-8000-843c00000000",
  "polarity": "affirm",
  "subject": "subject:project:orbit"
}
```

### 4.2 `supersession/` pair (older + newer)

Older — [01963f7d-7000-7000-8000-000002000000.json](../../tests/fixtures/m6_claim_mappings/supersession/01963f7d-7000-7000-8000-000002000000.json):

```json
{
  "claim_id": "01963f7d-7000-7000-8000-000002000000",
  "package_id": "01963f7d-7000-7000-8000-204100000000",
  "polarity": "affirm",
  "subject": "subject:supersede:topic_00"
}
```

Newer — [01963f7d-7000-7000-8000-000002000001.json](../../tests/fixtures/m6_claim_mappings/supersession/01963f7d-7000-7000-8000-000002000001.json):

```json
{
  "claim_id": "01963f7d-7000-7000-8000-000002000001",
  "package_id": "01963f7d-7000-7000-8000-204100000000",
  "polarity": "affirm",
  "subject": "subject:supersede:topic_00",
  "supersedes": [
    "01963f7d-7000-7000-8000-000002000000"
  ]
}
```

Note the shared `subject` + `package_id` and the `supersedes` linkage on the newer member only.

### 4.3 `expired/` (valid window past)

[01963f7d-7000-7000-8000-000003000000.json](../../tests/fixtures/m6_claim_mappings/expired/01963f7d-7000-7000-8000-000003000000.json):

```json
{
  "claim_id": "01963f7d-7000-7000-8000-000003000000",
  "package_id": "01963f7d-7000-7000-8000-801900000000",
  "polarity": "affirm",
  "subject": "subject:expired:topic_00",
  "valid_from": "2025-01-01T00:00:00Z",
  "valid_until": "2025-06-01T00:00:00Z"
}
```

### 4.4 `conflict/` pair (same subject, opposing polarity)

Affirm — [01963f7d-7000-7000-8000-000004000000.json](../../tests/fixtures/m6_claim_mappings/conflict/01963f7d-7000-7000-8000-000004000000.json):

```json
{
  "claim_id": "01963f7d-7000-7000-8000-000004000000",
  "package_id": "01963f7d-7000-7000-8000-15d900000000",
  "polarity": "affirm",
  "subject": "subject:conflict:topic_00"
}
```

Deny (see §3.3 divergence) — [01963f7d-7000-7000-8000-000004000001.json](../../tests/fixtures/m6_claim_mappings/conflict/01963f7d-7000-7000-8000-000004000001.json):

```json
{
  "claim_id": "01963f7d-7000-7000-8000-000004000001",
  "package_id": "01963f7d-7000-7000-8000-851500000000",
  "polarity": "deny",
  "subject": "subject:conflict:topic_00"
}
```

---

## 5. R4 outcome coverage matrix

The four buckets cover the R4 detection surface in `AphelionReadAdapter.query()` per `aphelion-graph/spec/v0.3-claim-semantics.md`:

| Bucket | Count | What it tests | Expected ingest outcome (route-A binary) | Expected read-side outcome |
|---|---|---|---|---|
| `not_found/` | 20 | subject not in package OR R2 valid-time window exclusion | ingest accepts (claim shape is valid); 20 audit rows | `ConflictClass.NOT_FOUND` at query time |
| `supersession/` | 30 (15 pairs) | R4 supersession detection — newer claim supersedes older | ingest accepts both pair members; 30 audit rows (audit chain is append-only — superseding row does NOT modify older row) | newer member surfaced as active; older surfaced as superseded |
| `expired/` | 10 | `valid_until` < query_time | ingest accepts (the expiry is read-side concern, NOT ingest-side); 10 audit rows | `ConflictClass.EXPIRED` at query time |
| `conflict/` | 10 (5 pairs) | v0.3 schema violations: affirm+deny on same subject | per §3.3 — current corpus uses `polarity:"deny"` which is invalid → ingest REJECTS with `claim.format_invalid` exit 65; 0 audit rows | N/A (never reaches read side) |

**Total audit rows from a clean 70-fixture ingest (assuming the §3.3 divergence is resolved by re-mapping `deny`→`negate`): 70.**
**Total audit rows from the current corpus: 60** = `not_found/` (20) + `supersession/` (30) + `expired/` (10) + `conflict/` (0 — all 10 rejected with `claim.format_invalid`).

---

## 6. Path note

The contract spec at `docs/m6-prep/m6-ingest-contract-spec.md §7.1` references the source path `/home/chris/parallax-data/claim-fixtures/`; that is the ZenBook build location. The shipped path inside the repo is `tests/fixtures/m6_claim_mappings/` — the same 70 files, committed under the test tree. The contract path is historical; the in-repo path is canonical.

---

## 7. Adding a new fixture

When extending the corpus:

1. **Pick a bucket** based on which R4 surface you're covering (NOT_FOUND / SUPERSESSION / EXPIRED / CONFLICT).
2. **Generate a UUID v7** for `claim_id`. Use the per-bucket byte-prefix scheme (`000001` for not_found, `000002` for supersession, `000003` for expired, `000004` for conflict) so the bucket is visually identifiable from the filename.
3. **Set `package_id`** to a fresh UUID v7 (independent — fixtures within a bucket do NOT need to share `package_id`, but `supersession/` and `conflict/` pair members typically share one).
4. **Add the fixture to the appropriate bucket directory** with filename `<claim_id>.json`.
5. **Update [tests/fixtures/m6_claim_mappings/CATALOG.md](../../tests/fixtures/m6_claim_mappings/CATALOG.md)** count if the bucket count changes.
6. **Re-run the integration suite**: `pytest tests/integration/test_m6_ingest_pipeline.py -v` to confirm the new fixture is picked up and asserted correctly.
7. **Do NOT add v0.4 evidence-binding fields** (`role`, `capture_ts`, `source_uri`, `excerpt_range`, `original_hash`) — these are reserved for the v0.4 upgrade lane (impl spec §9.2).

---

## 8. Cross-references

- Existing human-readable catalog: [tests/fixtures/m6_claim_mappings/CATALOG.md](../../tests/fixtures/m6_claim_mappings/CATALOG.md)
- Impl spec: [docs/m6-ingest/m6-ingest-impl-spec.md](./m6-ingest-impl-spec.md)
- Contract spec §7.1: [docs/m6-prep/m6-ingest-contract-spec.md](../m6-prep/m6-ingest-contract-spec.md)
- v0.3 claim semantics: `aphelion-graph/spec/v0.3-claim-semantics.md`
- Integration suite: `tests/integration/test_m6_ingest_pipeline.py`

---

## Changelog

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-05-19 | Initial fixture-catalog-format retrofit. Documents per-bucket schema, R4 coverage matrix, the `polarity:"deny"` divergence in `conflict/`, and the path mapping between contract §7.1 (ZenBook build path) and the in-repo `tests/fixtures/m6_claim_mappings/` (canonical shipped path). Closes P2 backlog #3 (fixture-side). |
