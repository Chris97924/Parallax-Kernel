# Apex M5 Envelope MVP Spec

**Version:** 0.1-frozen-2026-05-09
**Status:** FROZEN (Chris-pinned via xcouncil consensus 2026-05-09; §8 Q1 resolved to sha256-only)
**Repo:** Parallax-Kernel
**Author:** Claude autopilot 2026-05-09
**Upstream refs:**
- `docs/m5-prep/apex-m5-entry-spec.md` §3.1b (envelope MVP row)
- `vault/users/chris/wiki/derived/strategy/apex-m5-envelope-mvp-decisions.md` (3 拍板 origin)
- `aphelion-graph/adr/0002-v0.3-claim-semantics-r1r4.md` (claim semantics consumer side)

## §0 — Why this spec exists

The Apex adapter passes reads and writes between the Parallax-Kernel store and `.aphelion` package files. Those messages need a stable wire format so the adapter, audit ledger, and future tooling stay in sync. This spec defines that format — the **Apex M5 envelope** — as a Parallax-Kernel responsibility (Option α reconcile, 2026-05-09). The envelope is NOT an Aphelion format; Aphelion's own v0.5 signer covers `.aphelion` package-level claims, not this envelope. Aphelion v0.3 R1-R4 (claim semantics) and this envelope spec are parallel, non-blocking deliverables.

---

## §1 — Scope

### In scope (MVP)

- Header schema — 6 required fields
- Payload schema — `payload_type` enum + opaque `payload` object
- Checksum validation — sha256 over `payload` only
- Two canonical JSON examples (§6)
- Error taxonomy for validation failures

### Out of scope (v0.2+ deferred — see §5)

- `signature` (Ed25519 envelope signing)
- `migration_flag`, `trace_id`, `retry_count`
- HTTP transport, retry/backoff, auth — envelope is file-format only
- Internal structure of `payload` — envelope layer does not inspect it
- Aphelion `.aphelion` file format internals (Aphelion-Graph repo)

---

## §2 — Header Schema

All 6 fields are **required**. Unknown top-level keys are **rejected** (strict mode — see §8 Q3 for v0.2+ relaxation path).

| Field | Type | Constraint |
|---|---|---|
| `envelope_version` | string | exactly `"0.1"` for MVP |
| `schema_version` | int | ≥ 1; per-payload-type schema revision |
| `message_id` | string | UUIDv4 format; unique per envelope |
| `created_at` | string | ISO 8601 UTC, `Z` suffix required (e.g. `"2026-05-09T14:23:11Z"`). No ordering invariant — consumers MUST NOT assume monotonicity across envelopes. |
| `source` | string | enum `"aphelion"` \| `"parallax"` — reads inbound = `"aphelion"`, writes outbound = `"parallax"` |
| `audit_db_ref` | string | sha256 hex of canonical audit-row JSON; 64 lowercase chars; REQUIRED. See §2.1 + §8.1. |

**Validation rules:**

- All 6 fields must be present; any missing field → `EnvelopeValidationError("missing required field '<name>'")`
- `envelope_version` must be exactly the string `"0.1"`; any other value → `EnvelopeValidationError("unsupported envelope_version")`
- `source` must be one of `{"aphelion", "parallax"}`; any other value → `EnvelopeValidationError("invalid source value")`
- Unknown top-level keys → `EnvelopeValidationError("unknown field '<name>'")`

### §2.1 — `audit_db_ref` normative detail

The `audit_db_ref` value is the SHA-256 hex digest (64 lowercase characters) of the canonical UTF-8 JSON serialization of the corresponding audit row. The audit row schema and canonicalization rules are pinned in `audit-db-path-config.md` §6. Same canonical-JSON rules apply as in §4.1: keys lex-sorted ascending, no whitespace, UTF-8 NFC, `ensure_ascii=False`.

The audit row itself lives in `E:\Parallax\data\audit.db` (Windows dev) or `/home/chris/parallax-kernel/db/audit.db` (Linux ZenBook); see `audit-db-path-config.md` for resolution rules. The envelope writer MUST persist the audit row first, then compute the sha256, then emit — see §8.1 for the write-order invariant and `AuditWriteOrderViolation` guard.

---

## §3 — Payload Schema

### 3.1 `payload_type`

Closed enum. Exactly two valid values:

| Value | Meaning |
|---|---|
| `"query_result"` | Result of reading an Aphelion package (inbound from adapter) |
| `"event"` | Dual-read arbitration event (e.g. divergence, cache invalidation) |

Unknown values → `EnvelopeValidationError("unknown payload_type '<value>'")`; no free-string fallback.

### 3.2 `payload`

Opaque JSON object. The envelope layer does **not** inspect or validate internal structure. Downstream consumers (dual-read router, audit writer) parse `payload` per `payload_type` convention. M5 implementation treats `payload` as a pass-through; internal schema for each `payload_type` is a downstream concern.

Both `payload_type` and `payload` are required. Missing either → `EnvelopeValidationError("missing required field '<name>'")`

---

## §4 — Validation

### 4.1 Checksum

`checksum` is a **required** top-level field.

| Property | Value |
|---|---|
| Algorithm | SHA-256 |
| Encoding | lowercase hex string, exactly 64 characters |
| Input | Canonical UTF-8 JSON serialization of the `payload` field ONLY (not full envelope) |

**Canonical serialization rules for `payload`:**

1. Keys sorted ascending (lexicographic, ASCII-codepoint order)
2. No whitespace (no spaces, no newlines)
3. UTF-8 encoding with NFC normalization applied to **string values AND to object keys** (non-ASCII characters retained verbatim, NOT escaped)
4. No floats — match the existing `aphelion-graph/spec/canonical-serialization.md` rule (`PX_E_4008 / FLOAT_FORBIDDEN`); confidence numbers serialize as strings or ints in the underlying audit row schema

Rule 3 explicitly covers BOTH keys and values to remove ambiguity — two implementations that disagree on key NFC will produce different sha256 digests for identical-looking JSON. Python reference: NFC-normalize keys and string values before `json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')`. `ensure_ascii=False` is a Python-specific flag that satisfies rule 3 for output bytes; non-Python implementers must achieve the same effect (do not `\uXXXX`-escape valid UTF-8 input).

### 4.2 Checksum mismatch behavior

| Condition | Error raised | Adapter surface |
|---|---|---|
| `checksum` field absent | `EnvelopeValidationError("missing required field 'checksum'")` | `AphelionUnreachableError(reason="envelope_checksum_mismatch")` |
| `checksum` value does not match recomputed sha256 | `EnvelopeChecksumError("checksum mismatch")` | `AphelionUnreachableError(reason="envelope_checksum_mismatch")` |
| `checksum` present and matches | pass — proceed to downstream consumer | — |

**Exception taxonomy**: `EnvelopeValidationError` is raised for any structural / required-field problem (including absent `checksum`). `EnvelopeChecksumError` is reserved for the case where `checksum` is present but does not match the recomputed digest. Both surface to the adapter as the same `AphelionUnreachableError(reason="envelope_checksum_mismatch")` for outcome-classification purposes.

`AphelionUnreachableError` is defined in `parallax/router/aphelion_stub.py` (to be renamed `aphelion_adapter.py` via PR #47). Its `reason` field is the short tag used for outcome classification in `DualReadRouter`.

---

## §5 — Optional Fields (v0.2+ — NOT in MVP)

The following fields are explicitly deferred. Implementations MUST NOT accept or emit them in v0.1 envelopes.

| Field | Planned version | Description |
|---|---|---|
| `signature` | v0.2+ | Ed25519 signature over canonical envelope. Note: Aphelion v0.5 signer (PR `feat/v0.5-signer-trust`) is scoped to `.aphelion` package-level claim attestation — it signs the **package canonical hash**, not Apex envelopes. Apex envelope signing is a separate v0.2+ feature. |
| `migration_flag` | v0.2+ | Signals schema migration in progress; relaxes unknown-key rejection |
| `trace_id` | v0.2+ | Distributed tracing correlation ID |
| `retry_count` | v0.2+ | Retry attempt counter for idempotency tracking |

---

## §6 — Canonical Examples

### 6.1 Valid Envelope — `payload_type: "query_result"`

Payload used: `{"claim_id": "c-001", "confidence": 0.92}`
Canonical JSON: `{"claim_id":"c-001","confidence":0.92}`
SHA-256: `584cba361917b908082e0b25f792ce932b0db25519caa05a2995e8a341c1cfbe`

```json
{
  "envelope_version": "0.1",
  "schema_version": 1,
  "message_id": "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc",
  "created_at": "2026-05-09T14:23:11Z",
  "source": "aphelion",
  "audit_db_ref": "8e4f2b7c1a9d3e6f5c8b2a4d7e1f3c9b8e4f2b7c1a9d3e6f5c8b2a4d7e1f3c9b",
  "payload_type": "query_result",
  "payload": {"claim_id": "c-001", "confidence": 0.92},
  "checksum": "584cba361917b908082e0b25f792ce932b0db25519caa05a2995e8a341c1cfbe"
}
```

Expected outcome: validation passes. Checksum verified against `{"claim_id":"c-001","confidence":0.92}` (canonical).

### 6.2 Valid Envelope — `payload_type: "event"`

Payload used: `{"event_type": "dual_read_divergence", "key": "parallax:claim:c-001", "session_id": "sess-2026-05-09-001"}`
Canonical JSON: `{"event_type":"dual_read_divergence","key":"parallax:claim:c-001","session_id":"sess-2026-05-09-001"}`
SHA-256: `33b2d209c2836b82e3201ca3dfb0f73e14ce8af146534c269a9e15ae5536fb2c`

```json
{
  "envelope_version": "0.1",
  "schema_version": 1,
  "message_id": "c4e8f3b2-5d9a-4c8e-9f4b-23d567890def",
  "created_at": "2026-05-09T14:23:42Z",
  "source": "parallax",
  "audit_db_ref": "1b3d5f7a9c2e4b6d8f0a1c3e5b7d9f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d",
  "payload_type": "event",
  "payload": {"event_type": "dual_read_divergence", "key": "parallax:claim:c-001", "session_id": "sess-2026-05-09-001"},
  "checksum": "33b2d209c2836b82e3201ca3dfb0f73e14ce8af146534c269a9e15ae5536fb2c"
}
```

Expected outcome: validation passes. Note `source: "parallax"` because event envelopes are emitted outbound by the dual-read router, not inbound from Aphelion. The `payload` shape shown is illustrative — the envelope layer treats it as opaque (per §3.2); the event payload's internal schema lands in `docs/m5-prep/apex-m5-aphelion-adapter-design.md`.

### 6.3 Invalid Envelope — Missing `checksum`

Same as 6.1 but `checksum` field omitted.

```json
{
  "envelope_version": "0.1",
  "schema_version": 1,
  "message_id": "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc",
  "created_at": "2026-05-09T14:23:11Z",
  "source": "aphelion",
  "audit_db_ref": "8e4f2b7c1a9d3e6f5c8b2a4d7e1f3c9b8e4f2b7c1a9d3e6f5c8b2a4d7e1f3c9b",
  "payload_type": "query_result",
  "payload": {"claim_id": "c-001", "confidence": 0.92}
}
```

Expected errors:

```
EnvelopeValidationError: missing required field 'checksum'
AphelionUnreachableError(reason="envelope_checksum_mismatch")
```

The adapter catches `EnvelopeValidationError` (per §4.2 taxonomy: required-field absence is structural validation, not checksum mismatch) and surfaces it as `AphelionUnreachableError(reason="envelope_checksum_mismatch")` so `DualReadRouter` treats it as a secondary-unavailable outcome and falls back to primary.

---

## §7 — Cross-references

| Document | Path | Relevance |
|---|---|---|
| M5 entry spec §3.1b | `E:\Parallax\docs\m5-prep\apex-m5-entry-spec.md` | This spec satisfies the "Apex M5 envelope MVP" deferred upstream assumption row |
| 3 拍板 origin doc | `E:\Parallax\vault\users\chris\wiki\derived\strategy\apex-m5-envelope-mvp-decisions.md` | Header/payload/validation skeleton + xcouncil consensus; field names and constraints derived from here |
| Aphelion v0.5 PR description | `E:\Parallax\.omc\plans\aphelion-v0.5-pr-description.md` | Clarifies v0.5 signer scope: signs `.aphelion` package canonical hash, NOT Apex envelopes |
| Adapter stub (pre-rename) | `E:\Parallax\parallax\router\aphelion_stub.py` | Defines `AphelionUnreachableError(reason: str)` reused in §4 error surfacing; file renamed to `aphelion_adapter.py` via PR #47 |

---

## §8 — Resolution Log (Chris-pinned 2026-05-09 via xcouncil consensus)

| # | Question | Resolution | Vote | Rationale |
|---|---|---|---|---|
| Q1 | `audit_db_ref` format — sha256 vs rowid vs hybrid | **(a) sha256-only — REQUIRED**. No rowid fallback. | 5/8 (Sonnet, MiniMax, Qwen, MiMo, plus 2 c=hybrid as second choice) | sha256 is content-addressed and immutable. The constraint that "dual-read router + audit writer is one process" guarantees the writer can sha256 the canonical row before envelope emission. rowid is mutable under SQLite VACUUM and silently corrupts audit chains. One parser path > two for solo-dev. |
| Q2 | `payload_type: "event"` internal schema | **defer to `apex-m5-aphelion-adapter-design.md`** (v0.1 unchanged) | implicit | Envelope layer is transport-agnostic. Event payload schema is downstream router concern. |
| Q3 | Strict vs lenient unknown-key handling | **strict in v0.1** (v0.1 unchanged); v0.2+ `migration_flag` field unlocks lenient mode without `envelope_version` bump | implicit | Strict default catches typos in MVP. Migration flag avoids version churn for additive evolution. |

### §8.1 Implementation note for Q1 sha256-only path

```
write_order_invariant:
    1. caller constructs canonical audit row dict
    2. caller writes row → audit.db (BEGIN; INSERT; COMMIT)
    3. caller computes sha256(canonical_json(row))
    4. caller assembles envelope with audit_db_ref = sha256_hex
    5. caller emits envelope
```

If the writer fails between steps 2 and 5 (e.g. process crash), the audit row exists but no envelope was emitted — an acceptable failure mode (recoverable by replay).

If the writer attempts to emit envelope before step 2, the audit writer module **MUST raise** an explicit `AuditWriteOrderViolation` exception, regardless of Python's `-O` optimization flag. **Plain `assert` is NOT acceptable** — Python assertions are stripped under `-O` and `PYTHONOPTIMIZE`, so an assert-only guard would silently corrupt the audit chain in production-optimized builds (envelope would carry an `audit_db_ref` sha256 that hashes a row no one ever wrote).

```python
# Minimum acceptable guard
class AuditWriteOrderViolation(RuntimeError):
    """Raised when envelope emission is attempted before audit row commit."""

def emit_envelope(payload, audit_row_committed: bool, ...):
    if not audit_row_committed:
        raise AuditWriteOrderViolation(
            "envelope emit attempted before audit row commit; "
            "see apex-m5-envelope-spec.md §8.1"
        )
    ...
```

§4.2 already maps this case to `AphelionUnreachableError(reason="envelope_checksum_mismatch")` for the dual-read router; the explicit raise inside the audit writer is the local invariant guard, not the surface-level error class.
