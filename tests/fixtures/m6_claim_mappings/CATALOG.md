# Aphelion Claim-Mapping Fixtures — M6 Ingest Scaffold

Built: 2026-05-16 凌晨 ZenBook ralph session (Task B)
Total: 70 fixtures

## Scope clarification

These are post-ingest **claim mappings** (the shape Parallax
AphelionReadAdapter's claim_loader consumes), NOT the canonical
`.aphelion.tar` package format. M6 ingest pipeline (convert 
`.aphelion.tar` → claim mappings) is unbuilt — these JSON files
give the M6 implementer a reference target for that pipeline's
output, plus immediate diversity coverage for testing.

## R4 outcome distribution

- **NOT_FOUND baseline**: 20 claims at `not_found/`
- **SUPERSESSION pairs**: 30 claims (15 pairs) at `supersession/`
- **EXPIRED**: 10 claims (valid_from + valid_until past) at `expired/`
- **CONFLICT pairs**: 10 claims (5 pairs, affirm+deny same subject) at `conflict/`

## Schema

Each JSON file is a v0.3 claim mapping:
```json
{
  "claim_id": "01963f7d-7000-7000-8000-...",
  "subject": "subject:scope:value",
  "polarity": "affirm" | "deny",
  "package_id": "01963f7d-7000-7000-8000-...",
  "supersedes": ["<older_claim_id>", ...] (optional),
  "valid_from": "2025-01-01T00:00:00Z" (optional),
  "valid_until": "2025-06-01T00:00:00Z" (optional)
}
```

## Decision: Route A locked (Chris 拍板 2026-05-16 早)

**M6 路線 = A: build `.aphelion.tar` → claim mapping ingest pipeline**（canonical wire），**不**走 B (stub claim_loader 讀 JSON dir)。

These 70 JSON fixtures are now **M6 integration test fixtures**, NOT production ingest source. They serve as:
- Reference data for what claim mapping output shape should be after `.aphelion.tar` ingest
- Coverage matrix for R4 detection branches (verified empirically — see verify_claim_fixtures.py report)
- Smoke target for end-to-end ingest pipeline once it lands

### Rationale for A over B

1. **Aphelion canonical wire** — Aphelion roadmap §Scope Cut 寫死「.aphelion.tar 是 wire format，GraphQL/REST 永久不做」。B 繞過 canonical 跟 spec 不對齊。
2. **B 留永久 tech debt** — stub claim_loader 一旦進 production code，ship 壓力大時 A 永遠不會被做。
3. **M5 wire 已驗 200x M6 流量 headroom** — 5M stress run sustained 2269 rows/sec, p99 5.13ms。不需 B 跑 fake traffic 證明 wire 撐得住。
4. **M6 critical path = ingest pipeline 本身**。B 跑出 dual-read traffic 但 measure 的是 stub wire，不是 production wire — 走 B 並不前進 M6 entry。

### Constraints (unchanged)

- **DO NOT** point `PARALLAX_APHELION_PACKAGE_DIR` at this dir — env var currently only stores a path, no consumption logic. Restart parallax-server WILL change nothing visible until M6 ingest pipeline lands.
- Production audit.db should remain row count 0 until M6 ingest is wired (verify: `sqlite3 /home/chris/parallax-kernel/db/audit.db "SELECT COUNT(*) FROM audit_row;"`).
