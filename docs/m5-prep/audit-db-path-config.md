# Apex M5 Audit DB Path Configuration

**Status:** Normative (Chris-pinned 2026-05-09 via xcouncil consensus; §3 / §4 / §6 / §9 aligned with `parallax/apex/audit_db.py` impl 2026-05-13)
**Date:** 2026-05-09 (spec freeze) / 2026-05-13 (impl-sync revision)
**Owner:** Parallax-Kernel
**Consumers:** Apex M5 envelope writer, dual-read router, audit-replay tooling

---

## 1. Rationale

The Apex M5 dual-read flow writes one audit row per envelope (per `apex-m5-envelope-spec.md` §4.1, §8.1). The row content is sha256-hashed and that digest becomes the envelope's `audit_db_ref`. Two host environments (Windows dev, Linux ZenBook) need clear, fixed paths so the audit chain stays reproducible across reboots and OS-level moves.

This document fixes the env-var contract, the startup validation gates, and the canonical audit-row schema that production code (`parallax/apex/audit_db.py`) enforces.

## 2. Env var

```
PARALLAX_AUDIT_DB_PATH
```

REQUIRED. The variable resolves to an absolute filesystem path. Unset and empty-string are both rejected at startup as `EX_CONFIG` (see §4). Production code MUST NOT embed an operator-specific default — the operator sets it explicitly or the server refuses to serve traffic.

## 3. Path requirement (no implicit default)

There is no OS-family default. The operator MUST set `PARALLAX_AUDIT_DB_PATH` for every Parallax host before starting `parallax serve`.

Suggested values (documented in `.env.example`, not embedded in code):

| Environment | Suggested path | Reason |
|---|---|---|
| Windows dev | `E:\Parallax\data\audit.db` | Chris's E-drive data convention (`feedback_e_drive_data.md`) |
| Linux ZenBook | `/home/chris/parallax-kernel/db/audit.db` | User-local, sibling to other Parallax artifacts; no `/var/lib/` to sidestep sudo for dev work |

These values are suggestions. The contract is: the env var is set, points to an absolute path, and that path's parent directory exists and is writable.

## 4. Validation rules at startup

The Parallax server (`parallax serve`) MUST validate the resolved path on startup, before binding to any port. The order below matches `audit_db.open_audit_db()` so failures surface deterministically.

1. **Env var present.** Unset and empty-string raise `AuditDbConfigError("EX_CONFIG: PARALLAX_AUDIT_DB_PATH is not set …")`.
2. **Path absolute, no `..` segments.** Both syntactic check and `Path.resolve(strict=False)` are run; malformed UNC / junction / extended-length paths surface here.
3. **Parent directory exists and is writable.** `os.access(parent, os.W_OK)` is the gate; non-existent or non-directory parent is `EX_CONFIG`.
4. **SQLite open + pragmas.** `sqlite3.connect(..., isolation_level=None, timeout=5.0)`; on success the following pragmas are applied unconditionally:
   - `busy_timeout = 5000` (ms)
   - `journal_mode = WAL`
   - `synchronous = NORMAL`
   - `wal_autocheckpoint = 200` (pages)
   - Connect-time `sqlite3.Error` (including `OperationalError` for permission/lock failures) is translated to `AuditDbConfigError` so all startup-gate failures share one exit class.
5. **`PRAGMA quick_check` within a 30 s wall-clock budget.** A `set_progress_handler` callback aborts the operation if the budget is exceeded; both a budget-driven abort and a post-hoc elapsed check raise `AuditDbConfigError("EX_AUDIT_DB_SLOW_QUICKCHECK: …")`. A non-`ok` quick_check result raises `AuditDbConfigError("EX_CONFIG: audit_db quick_check returned non-ok: …")`.
6. **Write-permission probe.** `BEGIN IMMEDIATE` followed by `ROLLBACK`; failure (read-only mount, snapshotted backup volume, stale lockfile) raises `AuditDbConfigError("EX_CONFIG: audit_db write probe failed …")`.
7. **Schema applied + version verified.** Execution order: (a) `_verify_schema_version_if_present` — pre-apply guard that reads `MAX(version)` from `audit_db_schema_version` IF the table already exists; refuses to open a DB whose recorded version disagrees with `CURRENT_SCHEMA_VERSION`. This catches stale code (v=N) opening a DB previously written by newer code (v=N+1) BEFORE any DDL writes mutate the version table. (b) `_apply_schema()` runs the DDL (see §6) idempotently via `CREATE TABLE IF NOT EXISTS` plus an `INSERT OR IGNORE` into `audit_db_schema_version`. The schema is **not** loaded from an external `.sql` file — the DDL is generated at module import time from `audit_writer.OUTCOME_VALUES` and `SOURCE_VALUES` so the `CHECK` literals cannot drift from the writer's validation set. (c) `_verify_schema_version` re-runs the version check after apply as belt-and-braces (covers the fresh-DB case where the pre-apply guard was a no-op).
8. **On any §4 gate failure:** the structured log shape is `{"event": "audit_db_validation_failed", "reason": "<code>", "path": "<resolved_path>"}` and the server exits with code `78` (`EX_CONFIG`, per `sysexits.h`). Do NOT serve traffic with a broken audit path.

**Note on EX_CONFIG 78**: Parallax server adopts the `sysexits.h` exit-code convention here for the first time. Other Parallax-side startup failures use ad-hoc codes; this one fixes 78 because the audit chain is critical-path. Future startup-failure exits SHOULD adopt sysexits codes for consistency.

## 5. Worked examples

### 5.1 Windows dev

```powershell
$env:PARALLAX_AUDIT_DB_PATH = "E:\Parallax\data\audit.db"
parallax serve
# → audit chain at E:\Parallax\data\audit.db (schema auto-applied on first open)
```

`E:\Parallax\data\` must already exist (`mkdir` it manually before first run).

### 5.2 Linux ZenBook (systemd-managed)

```bash
# /etc/parallax/parallax.env
PARALLAX_AUDIT_DB_PATH=/home/chris/parallax-kernel/db/audit.db

# Service drops privs to chris user before reading this file.
sudo systemctl restart parallax-server
```

`/home/chris/parallax-kernel/db/` must already exist (Chris-action — see §9).

## 6. Canonical audit-row JSON shape

**Status:** Normative (Chris-confirmed 2026-05-09 PM after Phase-4 review; §6.1 schema notes updated 2026-05-13 to match `audit_db._SCHEMA_STATEMENTS`)
**Consumed by:** `apex-m5-envelope-spec.md` §2.1 — `audit_db_ref` is sha256 of the canonical JSON of a row matching this schema.

The `audit_db_ref` envelope field is the SHA-256 of the canonical UTF-8 JSON serialization of an audit-row record. For two implementations (Apex envelope writer + audit-replay tool) to compute the same digest, the row schema and canonicalization rules are normative.

### 6.1 Audit row schema

The audit row is a flat JSON object. All fields REQUIRED unless marked optional. Canonical key order (lex-ascending) shown:

```json
{
  "claim_id": "<UUID v7>",
  "envelope_message_id": "<UUID v4 — same value as envelope.message_id>",
  "outcome": "<enum, see §6.2>",
  "package_id": "<UUID v7 — the .aphelion package the read targeted>",
  "session_id": "<string — Parallax session correlator; matches the canonical field name used across parallax/events, parallax/router, parallax/server (verified against parallax/events/__init__.py 2026-05-09)>",
  "signer_id": "<string — Aphelion v0.5 signer fingerprint, OR empty string for unsigned packages>",
  "signer_manifest_digest": "<sha256 hex (64 chars), OR empty string for unsigned packages>",
  "source": "<enum: aphelion | parallax — same set as envelope.source>",
  "ts": "<ISO 8601 UTC, Z suffix, 20 chars; second precision matching envelope.created_at>"
}
```

Optional fields (present only when applicable; MUST be omitted when not — do NOT include with null):

```json
{
  "aphelion_hash": "<sha256 hex — content hash of the aphelion claim, only on outcome=divergence>",
  "local_hash":    "<sha256 hex — content hash of the local Parallax claim, only on outcome=divergence>",
  "reason_code":   "<namespaced tag, see §6.3 — present on outcome=error and on outcome=divergence>"
}
```

Empty string vs absent: empty string is a valid REQUIRED-field value where allowed (`signer_id` and `signer_manifest_digest` for unsigned packages). Empty string MUST NOT be conflated with absent — the optional fields above are absent (omitted from JSON), not empty-string.

**Defense-in-depth DDL constraints** (`parallax/apex/audit_db.py::_SCHEMA_STATEMENTS`) — 4 `CHECK` + 1 `UNIQUE`:

| Column | Constraint kind | Constraint | Purpose |
|---|---|---|---|
| `outcome` | CHECK | `outcome IN (<literals>)` | Literals derived from `audit_writer.OUTCOME_VALUES` at module import time so the DDL cannot drift independently of the writer's enum |
| `source` | CHECK | `source IN (<literals>)` | Same generation pattern, sourced from `audit_writer.SOURCE_VALUES` |
| `signer_manifest_digest` | CHECK | `length(...) = 64 OR ... = ''` | Allows the unsigned-package exemption (empty string per spec) while rejecting any non-64-char non-empty value |
| `ts` | CHECK | `ts LIKE '____-__-__T__:__:__Z'` | Last-resort sanity for direct INSERTs that bypass `canonicalize_row`; the writer remains source-of-truth for ISO-8601 strict validation |
| `envelope_message_id` | UNIQUE | `UNIQUE` | Idempotency guard — re-inserting the same envelope returns `sqlite3.IntegrityError` instead of duplicating |

**Indexes:** `idx_audit_row_ts` on `ts`, `idx_audit_row_session_id` on `session_id`. No index on `outcome` — 4-value enum has too low cardinality for an index to beat a table scan and the write-amp cost would be pure loss.

**Schema-version bookkeeping table** — `audit_db_schema_version`:

```sql
CREATE TABLE IF NOT EXISTS audit_db_schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
```

Append-only. `open_audit_db()` reads `MAX(version)` and refuses to open a DB whose recorded version disagrees with `audit_db.CURRENT_SCHEMA_VERSION` (currently `1`). Bumping the schema is a code change to `_SCHEMA_STATEMENTS` plus an incremented `CURRENT_SCHEMA_VERSION`; the bump emits a new row via `INSERT OR IGNORE`.

### 6.2 `outcome` enum (closed, with versioned evolution)

Closed enum, exactly four values:

| Value | Meaning |
|---|---|
| `"hit"` | Both primary (Parallax) and secondary (Aphelion) returned data and they agreed |
| `"miss"` | Primary returned no data for the queried key |
| `"divergence"` | Both sides returned data but disagreed (triggers R-8 arbitration; populates `aphelion_hash` + `local_hash` + `reason_code`) |
| `"error"` | Secondary unreachable / unsigned package / archive-safety violation / etc. (populates `reason_code`) |

**Evolution rule:** any new `outcome` value MUST be introduced via a bump of the envelope `schema_version` field (per-payload-type schema revision in the envelope header). Readers seeing `outcome` from an envelope with unfamiliar `schema_version` MUST gracefully degrade — read the row but treat unknown outcomes as advisory, not as data corruption. This keeps existing row sha256 digests stable across schema evolution.

### 6.3 `reason_code` taxonomy

`reason_code` is a namespaced string. The spec does NOT close the value set — adding a new reason MUST NOT be a schema bump (sha256 stays stable because the row is opaque-string-typed regardless of the value).

**Naming convention:**

- snake_case
- format `<namespace>.<reason>` — namespace is one of the prefixes below; reason is a short verb-noun token
- length ≤ 64 characters

**Reserved namespaces:**

| Prefix | Usage |
|---|---|
| `pkg.` | Package-level issues (e.g. `pkg.unsigned`, `pkg.archive_unsafe`, `pkg.not_found`, `pkg.corrupt`) |
| `signer.` | Signature / trust issues (e.g. `signer.mismatch`, `signer.expired`, `signer.unknown_id`) |
| `cache.` | Cache-layer issues (e.g. `cache.evict`, `cache.stale`, `cache.lock_timeout`) |
| `network.` | Reserved for future remote-fetch reasons; not used in M5 (file-format only) |
| `disk.` | Filesystem issues (e.g. `disk.permission`, `disk.io_error`, `disk.full`) |
| `claim.` | Claim-content issues that surfaced during dual-read (e.g. `claim.r4_subject_missing`, `claim.format_invalid`) |

**Reader behavior on unknown `reason_code`:** log + count under a `reason_unknown` bucket in metrics; do NOT raise a validation error. Forward-compatibility is intentional.

**Adding new reasons** is a documentation update against this §6.3 list — no schema_version bump, no envelope_version bump.

### 6.4 Canonicalization rules

Identical to `apex-m5-envelope-spec.md` §4.1 to keep both digests under one rule set:

1. Keys lex-sorted ascending (ASCII codepoint order)
2. No whitespace (no spaces, no newlines)
3. UTF-8 with NFC normalization on **keys AND string values**
4. No floats, no `null`. Optional fields are omitted when absent (NOT serialized as null).
5. Empty string is valid where the schema permits (REQUIRED fields can be empty string when documented; MUST NOT be conflated with absent).

Python reference (illustrative — production form is `parallax/apex/audit_writer.py::canonicalize_row()` returning `AuditRow` with `.sha256_hex()` method; the function below is for cross-implementation reference only):

```python
import json, hashlib, unicodedata

def _nfc(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {unicodedata.normalize("NFC", k): _nfc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    return value

def audit_row_sha256(row: dict) -> str:
    # Drop only None values (absent optional fields). Empty strings are kept.
    cleaned = {k: v for k, v in row.items() if v is not None}
    cleaned = _nfc(cleaned)
    blob = json.dumps(cleaned, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
```

### 6.5 Resolution log (Chris-confirmed 2026-05-09 PM)

| OQ | Resolution |
|---|---|
| Q1 `session_id` field name | Confirmed canonical — verified against `parallax/events/__init__.py:58` (`session_id: str | None = None` parameter) and 11 other Parallax modules. |
| Q2 `outcome` enum closure | Closed at 4 values with `schema_version` evolution rule per §6.2. |
| Q3 `reason_code` taxonomy | Open value set inside fixed namespace prefixes per §6.3. Not a closed enum — adding reasons is a doc update, not a schema bump. |
| Q4 `signer_manifest_digest` | Added as REQUIRED field. Empty string for unsigned packages. Aphelion v0.5 signer already computes `package_canonical_hash`; envelope writer reuses it at zero extra cost. |

## 7. Two environments, two paths — no conflict

Audit chains are per-host. There is no requirement to merge the Windows and Linux audit trails. Each host's envelope `audit_db_ref` resolves only against that host's `audit.db`. Cross-host replay is out-of-scope for M5 (and likely M6 — it requires a sync layer not yet specced).

## 8. Why not `~/.local/share/parallax/`

The XDG-style path was considered but rejected for these reasons:

- ZenBook is a single-user host; `~/.local/share/` adds path complexity without isolation benefit.
- Chris's E-drive convention is project-explicit; Parallax data co-locates with code/vault under `E:\Parallax\`.
- The `parallax-kernel/db/` subpath on Linux mirrors the repo layout (`parallax-kernel/` is the repo root on ZenBook), making backups/snapshots straightforward.

## 9. Chris-action items

| # | Action | Where | When |
|---|---|---|---|
| 1 | Create directory `E:\Parallax\data\` if missing | Windows dev | First M5 work session |
| 2 | Create directory `/home/chris/parallax-kernel/db/` if missing | ZenBook | Pre-M5 entry |
| 3 | Add `PARALLAX_AUDIT_DB_PATH=/home/chris/parallax-kernel/db/audit.db` to `/etc/parallax/parallax.env` (sudo) | ZenBook | Pre-M5 entry |
| 4 | `sudo systemctl restart parallax-server` | ZenBook | After step 3 |
| 5 | Set `$env:PARALLAX_AUDIT_DB_PATH` in the Windows session before running `parallax serve` | Windows dev | Per shell session (or persist via System Properties → Environment Variables) |

Schema migration is **not** a Chris-action — `open_audit_db()` applies the DDL idempotently on every open via `CREATE TABLE IF NOT EXISTS` and an `INSERT OR IGNORE` into `audit_db_schema_version`. No external `.sql` file is loaded.

## 10. References

- `apex-m5-envelope-spec.md` §4.1, §8.1 — envelope `audit_db_ref` semantics and write-order invariant
- `apex-m5-entry-spec.md` §3.1a — audit `package_id` only (no raw payload), `audit-write failure observability`
- `feedback_e_drive_data.md` — Chris's E-drive data convention
- `parallax/apex/audit_db.py` — implementation pins for §3-§4 + §6 (schema constants `REQUIRED_COLUMNS` / `OPTIONAL_COLUMNS` / `CURRENT_SCHEMA_VERSION`, error classes `AuditDbConfigError` / `AuditDbWriteError` / `AuditDbUsageError`)
- `.env.example` — operator-facing form of §2 + §3
