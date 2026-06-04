# M7 free-text retrieval + content-bearing hits — design note (#71)

> Status: **design note + bounded first slice**. Part A (content-bearing hits)
> is **shipped in this PR**. Part B (free-text → subject resolution) is an
> **architecture fork left for Chris** — no code committed for it here.
> Authored 2026-06-04 against `origin/main-next` `d71d47b`.

## 1. Problem (restated from #71)

The merged M7 public-read router (`parallax/apex/router.py` + the reused M5
`parallax/router/aphelion_adapter.py`) cannot serve the intended deliverable —
"Claude Code reads public knowledge from a prompt" — for two independent
reasons:

- **Gap 1 — exact-subject R4 only.** R4 step-1 is exact string equality:
  `aphelion/read_adapter.py:158-162` filters
  `c.get("subject") == subject and _r2_active(...)`. The adapter maps the
  request straight to one subject (`aphelion_adapter.py:_resolve_subject`,
  `request.params["subject"]` → `request.q` → `request.user_id`). A
  natural-language prompt fed as `subject` ~never exact-equals a stored
  canonical subject (e.g. `"retrieval-quality"`) → `ConflictClass.NOT_FOUND`
  → empty hits for ~100% of real traffic. There is no substring / token /
  embedding / index path on the read side.
- **Gap 2 — subject-only hits.** The hit builder set `text` to the claim
  *subject* (topic label), not the claim content. Even a perfect-subject hit
  returned a label + polarity — nothing to inject as knowledge. This was
  narrower than the kernel's own `parallax/retrieval/contracts.py:42`
  docstring (`hits` = `{id, text, created_at, source_id, kind}`, `text`
  implied content) and the M7 spec §4.1 "claim filter + evidence project per
  query → return ranked claim list".

The two gaps are **separable**: Gap 2 is a fork-free shape fix and a hard
prerequisite for the whole feature (without it, even a solved Gap 1 returns
nothing injectable). Gap 1 is where the real architecture decision lives.

## 2. Part A — content-bearing hits (SHIPPED in this PR, fork-free)

### 2.1 What changed

- **Capture the body that was already being read and discarded.**
  `router.py:_read_claim_frontmatter` did `yaml_part, _body = split_frontmatter(text)`
  and threw `_body` away. It now returns `(frontmatter, body)`, and
  `_project_claims` projects the markdown body onto each claim mapping under a
  single shared key (`aphelion_adapter.CLAIM_CONTENT_KEY = "body"`), alongside
  the existing `package_id` projection.
- **Content-bearing hit shape.** `aphelion_adapter._build_hit` replaces the
  inline subject-only dict. Each hit now carries:

  | field | value | maps to (`RetrievalHit` / DTO) |
  |---|---|---|
  | `text` | claim **content** — body → title → subject (fallback chain) | L1 `title` |
  | `content_source` | which field `text` came from: `body`/`title`/`subject` | — (observability) |
  | `evidence` | one-sentence provenance reason, **`str`** (never a Mapping) | L2 `evidence` |
  | `full` | shallow dict snapshot of the claim, frontmatter + body (JSON-safe while values stay scalars/lists) | L3 `full` |
  | `subject` | the subject label (kept as its own field — no info loss) | — |
  | `created_at` | claim `created_at` when present | answer-path timestamp |
  | `id` / `kind` / `polarity` | unchanged from pre-#71 | — |

  The shape deliberately matches `parallax/retrieve.py:RetrievalHit`
  (`evidence: str | None`, `full: dict | None`) and is consumed correctly by
  the existing `server/routes/query.py:_router_hit_to_dto` (reads
  `text`/`evidence`/`full`) and `answer/evidence.py:_render_evidence` (reads
  `text`/`created_at`).

### 2.2 Why this is safe (no R4 regression, no dual-read regression)

- **Validator tolerance.** The injected `body` key passes
  `aphelion.v03_validator.validate_v03_fields` — only `conflict_class` is a
  reserved field (`RESERVED_DERIVATION_FIELDS == frozenset({'conflict_class'})`).
  The R4 reader ignores unknown keys.
- **Dual-read is a no-op.** `server/routes/query.py:159` builds the M5 adapter
  with **no** `claim_loader` → `_empty_loader` → zero surfaced claims → zero
  hits. The richer hit builder never runs on that path, so dual-read behaviour
  is unchanged.
- **Fallback preserves old behaviour.** A claim with no body and no title still
  yields `text == subject` — the exact pre-#71 value.
- **The fall-through is observable, not silent.** Each hit carries
  `content_source` (`body`/`title`/`subject`), so a degraded label-only hit
  (e.g. an M7 claim whose body was lost at ingest) is distinguishable from a
  genuine content hit by downstream/ops — the graceful fallback does not hide
  a content-loss.
- **Tests.** `tests/apex/test_router.py::TestContentBearingHits` (3 integration
  tests on real signed packages) + `tests/router/test_aphelion_adapter.py`
  (2 unit tests on the fallback chain). The negative twin
  (`test_no_matching_claim_returns_empty`) and the full existing suite stay
  green (202 → 207 passing in the touched files).

### 2.3 What Part A does NOT do

It does **not** make a free-text prompt find a claim. The shipped tests still
query with an exact subject (`params["subject"]="retrieval-quality"`); they
prove the hit is now *content-bearing*, not that resolution is *free-text*.
That is Part B.

## 3. Part B — free-text → subject resolution (THE FORK, for Chris)

### 3.1 Why R4 can't just "fuzzy match"

R4 detection is **inherently per-subject**: supersession, contradiction, and
ambiguity (`aphelion/read_adapter.py` steps 1-3 + `_residual_default_policy`)
are all defined *within a single subject group*. You cannot loosen step-1
equality to a fuzzy match without changing what "a conflict" means. The
correct architecture (and what M7 spec §4.1 already sketches) is a
**candidate-resolution layer in front of R4**:

```
prompt ──▶ [resolve query → candidate subject(s)] ──▶ for each subject: R4 ──▶ merge/rank content-bearing hits
              ▲ THE FORK                                 ▲ unchanged (Part A makes hits content-bearing)
```

The resolver is new; R4 stays exactly as-is and runs over the resolved
candidate set instead of the raw prompt.

### 3.2 The options (pick per profiling/scope — #71 proposed-work item 1)

**Option A — subject/claim-key index maintained by M6 ingest + prompt→subject resolver.**
M6 ingest writes a `subject → {package_id, claim_key}` index when it drops a
package; the read path resolves prompt → candidate subjects against that index,
then unpacks only the matching package(s).
- *Pro:* also fixes the spec **§8.4 Q4 per-read full-corpus-unpack ceiling**
  (one index lookup vs unpacking every `.aphelion.tar`); scales past the
  ~"10s of packages" ceiling; resolution quality is whatever the index keys on.
- *Con:* cross-repo work (touches M6 ingest, currently shipped); index
  freshness contract needed (§8.4 forbids the silent-stale-window); the
  *resolver* still needs a matching strategy (exact key? token? embedding?),
  so A composes with B or C rather than replacing them.

**Option B — substring / token-overlap match over claim subjects (in-memory, per-read).**
Load the corpus (as today), collect distinct subjects, pick subjects whose
tokens overlap the prompt.
- *Pro:* zero new dependencies; pure-Python; trivially testable; smallest diff;
  works offline; a natural "v0 baseline".
- *Con:* keeps the per-read full-corpus unpack (does not touch §8.4 ceiling);
  lexical only — misses paraphrase ("CC reads public memory" vs
  `retrieval-quality`); needs a tie/threshold policy (see 3.3).

**Option C — embedding similarity (semantic).**
Embed prompt + subjects (or claim bodies), resolve by cosine top-k.
- *Pro:* handles paraphrase / semantic match — the actual "reads public
  knowledge from a prompt" UX; can rank claim *bodies* directly (Part A already
  surfaces bodies).
- *Con:* model + index choice (local vs API), dependency + latency budget
  (§4.2 p99 < 100ms), embedding store + freshness, determinism/versioning of
  the model. The heaviest fork.

### 3.3 Sub-decisions that ride on the fork (also Chris-gated)

1. **Match target** — resolve against *subjects* (cheap, label-level) or claim
   *bodies* (Part A makes these available; better recall, more compute)?
2. **Ranking / top-k** — how many candidate subjects to admit, and the ordering
   of the merged multi-subject hit list (`RetrievalEvidence.hits` is currently
   unordered-by-contract; the facade may expect a ranked list per §4.1).
3. **Multi-subject aggregation** — R4 runs per subject; merging hits across
   subjects needs a dedup + ordering rule.
4. **Index freshness** (only if A) — synchronous rebuild-on-miss vs a
   `parallax_apex_index_staleness_seconds` gauge (§8.4 stale-window rule).
5. **Negative-result contract** — the §4.5 `parallax_apex_empty_result{cause=...}`
   metric must keep firing; a resolver that broadens recall must not silently
   convert genuine misses into spurious hits.

### 3.4 Recommended sequencing (recommendation only — Chris decides)

A defensible low-regret path, **not** a decision:

1. Ship Part A (done here).
2. Land **Option A's index in M6 ingest** first — it is the long-pole, it
   unblocks the §8.4 scaling story, and every resolver strategy benefits from
   it. Resolve against subjects to start.
3. Start the resolver as **Option B (token-overlap)** behind the index as the
   v0 baseline + golden-set harness, *then* graduate to **Option C
   (embedding)** if the golden set shows lexical recall is the bottleneck.

This is sequenced so each step is independently shippable and testable, and so
the expensive embedding decision is deferred until a golden set proves it is
needed. **But the choice of B-vs-C, the match target, the ranking policy, and
whether to build the M6 index now are genuine forks** (dependencies, latency
budget, cross-repo scope) and are Chris's call.

## 4. Decision points for Chris (the forks)

- [ ] **D1** Build the M6-ingest subject/claim-key index now (Option A), or stay
      per-read for v0? (Couples to §8.4 Q4 + cross-repo M6 work.)
- [ ] **D2** Resolution strategy: token-overlap baseline (B) → embedding (C), or
      jump straight to embedding? If C: which model, local vs API, latency
      budget vs §4.2 p99 < 100ms.
- [ ] **D3** Match target: subjects vs claim bodies.
- [ ] **D4** Ranking / top-k + multi-subject merge policy for `hits`.

## 5. Out of scope (unchanged boundaries)

- §3 Perihelion boundary, §4.3 failure semantics, §4.5 metrics — untouched.
- The parked **facade slice** (`agent-config` → `claude/perihelion-bridge` +
  `claude/hooks/perihelion-retrieve.js`) stays parked; it can resume once Gap 1
  closes. Part A closes Gap 2; the facade still waits on a D1-D4 decision.
