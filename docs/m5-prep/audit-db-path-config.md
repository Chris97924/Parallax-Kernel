# Apex M5 Audit DB Path Configuration

**Status:** Normative (Chris-pinned 2026-05-09 via xcouncil consensus)
**Date:** 2026-05-09
**Owner:** Parallax-Kernel
**Consumers:** Apex M5 envelope writer, dual-read router, audit-replay tooling

---

## 1. Rationale

The Apex M5 dual-read flow writes one audit row per envelope (per `apex-m5-envelope-spec.md` §4.1, §8.1). The row content is sha256-hashed and that digest becomes the envelope's `audit_db_ref`. Two host environments (Windows dev, Linux ZenBook) need clear, fixed paths so the audit chain stays reproducible across reboots and OS-level moves.

This document fixes those paths and the env var that resolves them.

## 2. Env var

```
PARALLAX_AUDIT_DB_PATH
```

If set, this absolute path overrides the default. If unset, the default is selected per OS family.

## 3. Default paths

| Environment | Default path | Reason |
|---|---|---|
| Windows dev | `E:\Parallax\data\audit.db` | Chris's E-drive data convention (`feedback_e_drive_data.md` — Parallax/Apex data lives on E:) |
| Linux ZenBook | `/home/chris/parallax-kernel/db/audit.db` | User-local, sibling to other Parallax artifacts; no `/var/lib/` to sidestep sudo for dev work |

## 4. Validation rules at startup

The Parallax server (`parallax serve`) MUST validate the resolved path on startup, before binding to any port:

1. Path MUST be absolute (reject relative paths and `..` segments).
2. Parent directory MUST exist with write permissions for the running user.
3. If the file exists:
   - MUST be a regular file readable+writable by the running user.
   - MUST open as a valid SQLite v3 database. Open with `PRAGMA busy_timeout = 5000` set FIRST, then run `PRAGMA quick_check` with a wall-clock budget of **30 seconds**. If `quick_check` does not return within 30s, abort startup with `EX_AUDIT_DB_SLOW_QUICKCHECK` (do NOT block startup indefinitely on a slow DB).
   - MUST pass an explicit write-permission probe: open a `BEGIN IMMEDIATE` transaction and `ROLLBACK` it. Read-only or locked files fail the probe even if `quick_check` passes (catches OS-level read-only filesystems, snapshotted backup volumes, and stale lockfiles AT STARTUP, not on first envelope emission).
4. If the file does not exist, the server MUST create it on first envelope emission, applying the audit schema migration (`schema/audit_v1.sql` — Chris-action separately).
5. On any validation failure: log error to stderr (structured JSON: `{"event": "audit_db_validation_failed", "reason": "<code>", "path": "<resolved_path>"}`), exit code 78 (EX_CONFIG); do NOT serve traffic with a broken audit path.

**Note on EX_CONFIG 78**: Parallax server adopts the `sysexits.h` exit-code convention here for the first time. Other Parallax-side startup failures use ad-hoc codes; this one fixes 78 because the audit chain is critical-path. Future startup-failure exits SHOULD adopt sysexits codes for consistency.

## 5. Worked examples

### 5.1 Windows dev

```powershell
$env:PARALLAX_AUDIT_DB_PATH = "E:\Parallax\data\audit.db"
parallax serve
# → audit chain at E:\Parallax\data\audit.db
```

If `PARALLAX_AUDIT_DB_PATH` is unset, the same path is used by default on Windows.

### 5.2 Linux ZenBook (systemd-managed)

```bash
# /etc/parallax/parallax.env (Chris-action — see §6)
PARALLAX_AUDIT_DB_PATH=/home/chris/parallax-kernel/db/audit.db

# Service drops privs to chris user before reading this file.
sudo systemctl restart parallax-server
```

If `PARALLAX_AUDIT_DB_PATH` is unset on Linux, the user-local default is used. ZenBook deployment SHOULD set the env var explicitly (audit-trail clarity).

## 6. Chris-action items

| # | Action | Where | When |
|---|---|---|---|
| 1 | Create directory `E:\Parallax\data\` if missing | Windows dev | First M5 work session |
| 2 | Create directory `/home/chris/parallax-kernel/db/` if missing | ZenBook | Pre-M5 entry |
| 3 | Add `PARALLAX_AUDIT_DB_PATH=/home/chris/parallax-kernel/db/audit.db` to `/etc/parallax/parallax.env` (sudo) | ZenBook | Pre-M5 entry |
| 4 | `sudo systemctl restart parallax-server` | ZenBook | After step 3 |
| 5 | Apply audit schema migration (`schema/audit_v1.sql`) — separate ticket | Both | Pre-M5 entry |

## 7. Two environments, two paths — no conflict

Audit chains are per-host. There is no requirement to merge the Windows and Linux audit trails. Each host's envelope `audit_db_ref` resolves only against that host's `audit.db`. Cross-host replay is out-of-scope for M5 (and likely M6 — it requires a sync layer not yet specced).

## 8. Why not `~/.local/share/parallax/`

The XDG-style path was considered but rejected for these reasons:

- ZenBook is a single-user host; `~/.local/share/` adds path complexity without isolation benefit.
- Chris's E-drive convention is project-explicit; Parallax data co-locates with code/vault under `E:\Parallax\`.
- The `parallax-kernel/db/` subpath on Linux mirrors the repo layout (`parallax-kernel/` is the repo root on ZenBook), making backups/snapshots straightforward.

## 6. Canonical audit-row JSON shape

**Status:** Normative (Chris-confirmed 2026-05-09 PM after Phase-4 review)
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
  "signer_manifest_digest": "<sha256 hex — Aphelion v0.5 package_canonical_hash; empty string for unsigned packages>",
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

Python reference:

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

## 9. References

- `apex-m5-envelope-spec.md` §4.1, §8.1 — envelope `audit_db_ref` semantics and write-order invariant
- `apex-m5-entry-spec.md` §3.1a — audit `package_id` only (no raw payload), `audit-write failure observability`
- `feedback_e_drive_data.md` — Chris's E-drive data convention
