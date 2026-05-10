# Naming Changelog

**Effective:** 2026-05-09 (flip-back from 2026-05-08 Messier+V to original Apex+M)
**Status:** ✅ CONFIRMED — Chris 拍板 2026-05-09; mass rename executed; this file is the canonical mapping table going forward
**Forward SSoT (Obsidian):** `E:\Parallax\vault\users\chris\wiki\entities\messier.md` (filename retained, content uses Apex)

## Why this file exists

The "Parallax × Aphelion 融合" product had a brief 1-day naming experiment. On **2026-05-08** Chris trialed renaming it "Messier" + milestones "V0-V11" (astronomy theme: Charles Messier 1771 deep-sky catalog as memory-index metaphor). On **2026-05-09** Chris flipped back to the original proposal **Apex** + **M0-M11**, citing M-numbering's 1-to-1 milestone correspondence + Apex being more口語 as the product handle.

This file is the in-repo mapping table for grep-ability + onboarding. Full rationale lives in the Obsidian forward SSoT.

## Final mapping (effective 2026-05-09)

| Item | Final name | Notes |
|---|---|---|
| Parallax × Aphelion 融合 product | **Apex** | Product name (back to original proposal) |
| Lane C 融合 | **Apex** | Internal codename, same product |
| "Messier" (2026-05-08 brief alternate) | **Apex** | Flipped back |
| "Apex module" (early proposal) | **Apex** | Just "Apex" — drop "module" suffix |
| M0 — 今日基礎 | M0 | shipped 2026-04-25 |
| M1 — L0 Round-trip 通 | M1 | shipped 2026-04-25 |
| M2 — L1 Shadow 能觀察 | M2 | shipped 2026-04-29/30 |
| M3 — L2 Dual-read 穩定 | M3 | active |
| M4 — L3 Canary @100% | M4 | code-merged on main-next |
| M5 — Full Dual-Write | M5 | entry spec PR #46 merged 2026-05-08; reframe spec at `docs/m5-prep/apex-m5-entry-spec.md` 2026-05-09 |
| M6 — 主記憶 Dogfood (M1-arch model) | M6 | future |
| M7 — 雙系統 Primary (M2-arch model) | M7 | future |
| M8 — Performance SLA | M8 | future |
| M9 — OSS Surface Lock | M9 | future |
| M10 — v1.0 GA | M10 | future |
| M11 — 主記憶 GA Primary (M3-arch model) | M11 | future |
| `V0..V11` (2026-05-08 brief alternate) | **M0..M11** | Flipped back |

> **Disambiguation note**: M6 / M7 / M11 internally reference "(M1 model)" / "(M2 model)" / "(M3 model)" — these are the dog-fooding **memory architecture stages**, distinct from milestone numbering. Roadmap doc relabels them "(M1-arch model)" etc. to avoid collision.

## Forward-only rule

Historical artifacts are NOT renamed — they preserve their original M-prefix to keep git/PR/Slack history intact:

- ✅ Already-merged commit messages and PR titles (e.g. `feat(canary): US-009.1 — M4 canary infrastructure (#41)`) — never touched (`docs/m5-prep/m5-entry-spec.md` from PR #46 stays untouched)
- ✅ Slack channels (e.g. `#m4-canary`)
- ✅ Already-merged branch names (e.g. `docs/m5-prep-entry-spec`)
- ✅ Existing files under `docs/m4-prep/` and `docs/m5-prep/` (path retained)
- ✅ Vault filenames containing `messier` / `v0-to-v11` / `v5` (retained to avoid mass wikilink breakage; CONTENT updated per flip-back)

New names take effect from 2026-05-09 forward:

- ✅ New PR / issue / branch names → use Apex+M
- ✅ New design docs / specs / autopilot runs → use Apex+M
- ✅ New ralph PRDs and progress entries → use Apex+M
- ✅ Conversation references when context is post-2026-05-09

Transitional (1-2 months): dual-write where ambiguity matters — `Apex M5 (originally M5 Full Dual-Write per Notion canonical roadmap)`.

## Why "Apex"

Astronomy theme alignment with sibling products:

- **Parallax** (視差) — apparent shift in stars due to observer position
- **Aphelion** (遠日點) — orbit's farthest point from sun
- **Perihelion** (近日點) — orbit's closest point to sun
- **Orbit** — well, orbit
- **Apex** — orbital apex / vertex (highest point in projected motion); also generic "peak" connotation

(2026-05-08 trial of "Messier" was thematically valid — Charles Messier 1771 deep-sky catalog as memory-index metaphor — but Chris preferred Apex's brevity + milestone-prefix consistency.)

## Pointers

- **Obsidian SSoT (entity hub)**: `E:\Parallax\vault\users\chris\wiki\entities\messier.md` (filename retained, content uses Apex)
- **Obsidian SSoT (M0-M11 roadmap)**: `E:\Parallax\vault\users\chris\wiki\derived\strategy\messier-roadmap-v0-to-v11.md` (filename retained, content uses M0-M11)
- **Obsidian SSoT (decision record, frozen historical)**: `E:\Parallax\vault\users\chris\wiki\derived\strategy\2026-05-08-messier-naming-decision.md` (top note marks SUPERSEDED 2026-05-09)
- **Obsidian SSoT (M5 reframe)**: `E:\Parallax\vault\users\chris\wiki\derived\strategy\2026-05-09-v5-direction-reframe.md` (filename retained from V5 era, content uses M5)
- **Obsidian SSoT (xcouncil envelope MVP decisions)**: `E:\Parallax\vault\users\chris\wiki\derived\strategy\apex-m5-envelope-mvp-decisions.md`（renamed 2026-05-09 from `aphelion-v06-mvp-decisions.md` per Option α — envelope 歸 Apex/Parallax-Kernel，Aphelion v0.3 R1-R4 留 Aphelion repo，兩件事兩個 owner）
- **Notion legacy** (frozen + deprecated banner): `Parallax × Aphelion 融合使用說明` (`343f3661...`)
- **Repo M5 spec (active)**: `docs/m5-prep/apex-m5-entry-spec.md` (v0.3.0-reframe)
- **Repo M5 spec (deprecated, PR #46 merged)**: `docs/m5-prep/m5-entry-spec.md` + `docs/m5-prep/_DEPRECATED.md` sidecar
- **Aphelion-Graph issue**: [#7](https://github.com/Chris97924/Aphelion-Graph/issues/7) — title is now `[Apex M5-blocker] Aphelion v0.3 R1-R4 spec confirmation for Apex M5 reader integration`

## Naming history (for reference)

| Stage | Product name | Milestone path | Date |
|---|---|---|---|
| Original proposal (~2026-05-07) | Apex | M0-M11 | Chris's first-pass thinking |
| Brief alternate | **Messier** | **V0-V11** | 2026-05-08 (1-day trial) |
| **Final** | **Apex** | **M0-M11** | 2026-05-09 (flip-back, current) |

## NOT committed this session

This file is working-tree only. Open a separate `chore/apex-naming-changelog` PR if/when the team wants this in git. Acceptance for that future PR: file lints clean (markdown + link check).
