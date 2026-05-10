# ⚠️ DEPRECATED — `m5-entry-spec.md` is historical

**Effective:** 2026-05-09
**Reason:** Direction reframe — `m5-entry-spec.md` (PR #46 merged 2026-05-08) framed Aphelion as an HTTP service. That is wrong. Per the canonical Aphelion roadmap (Notion `347f3661...`), Aphelion is a **file format / Python package**, not a service; HTTP/REST API is permanently scope-cut.

## Forward SSoT

→ **`docs/m5-prep/apex-m5-entry-spec.md`** (Apex M5 entry spec, v0.3.0-reframe)

## What is preserved here

- `m5-entry-spec.md` — kept untouched as historical record of the wrong-direction draft. Do NOT reference for active work; do NOT delete (PR #46 merge is part of git history).

## Naming note (2026-05-09 flip-back)

The product is named **Apex** (not "Messier" — that was a brief 2026-05-08 alternate, flipped back 2026-05-09). Milestone path is **M0-M11** (not "V0-V11"). Filenames in this dir keep their `m5-` prefix unchanged. See `docs/_naming-changelog.md` for the full mapping + flip-back history.

## What changed in `apex-m5-entry-spec.md` vs `m5-entry-spec.md`

| Topic | Old (here, PR #46) | New (`apex-m5-entry-spec.md`) |
|---|---|---|
| Adapter abstraction | HTTP call to Aphelion service | Local `.aphelion` package read via `aphelion` Python lib |
| E.4 / P-A2 | Aphelion v0.6 envelope contract / staging health | Aphelion v0.3 R1-R4 (confidence/time/conflict) frozen + Python lib API stable |
| §3.1a Security | TLS / Bearer / rate-limit headers | Path validation / untar safety / signer mandatory + `audit.db` path discipline (E:\Parallax\data on Win, /home on Linux) |
| R-9 cache invalidate batching | Required (network round-trip) | Dropped (no network) |
| Latency miss-and-fetch p95 | ≤250ms (HTTP budget) | ≤150ms (local file ~10x faster than network) |
| ETA | Earliest 2026-06-04 | Same M4 dependency, **no Aphelion-side service work needed** — gated on Chris xcouncil session for v0.3 spec |

## ✅ RESOLVED 2026-05-09: v0.6 envelope vs v0.3 R1-R4 attribution (Option α)

xcouncil 2026-05-09 (see `apex-m5-envelope-mvp-decisions.md`) decided "envelope MVP" with `payload_type={query_result, event}` schema. Per Option α reconcile 2026-05-09, this is **Apex M5 envelope MVP** (Parallax-Kernel-owned wire format), NOT Aphelion-side. Aphelion-side ask is **v0.3 R1-R4** (claim semantics). Two separate artifacts, two owners, parallel paths. envelope spec lands at `docs/m5-prep/apex-m5-envelope-spec.md`.

## Pointers

- New spec: `docs/m5-prep/apex-m5-entry-spec.md`
- Naming context: `docs/_naming-changelog.md` (Apex/M flip-back from 2026-05-08 Messier/V)
- Forward roadmap: Obsidian vault `wiki/derived/strategy/messier-roadmap-v0-to-v11.md` (filename retained, content uses Apex/M)
- xcouncil decisions: vault `wiki/derived/strategy/apex-m5-envelope-mvp-decisions.md` (renamed 2026-05-09 from `aphelion-v06-mvp-decisions.md` per Option α)
- Aphelion canonical: Notion `347f3661...` "Aphelion v0.2 → v1.0 Roadmap (SSoT)"
