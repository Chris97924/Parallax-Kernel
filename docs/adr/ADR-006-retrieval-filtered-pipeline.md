# ADR-006: Retrieval-filtered pipeline with intent router (xcouncil Phase 1)

## Status

Superseded — 2026-07-02. The Phase-1 `fallback_retrieve` prototype
(`parallax/retrieval/retrievers.py`) was removed as dead code; semantic
retrieval is now wired through the M8 semantic path
(`parallax/retrieval/semantic.py`). Re-wiring an intent-routed retrieval
pipeline on top of that path is tracked as separate work. The original
proposal is retained below unchanged for historical context.

Proposed — 2026-04-20, target v0.5.x (xcouncil Phase 1 ships inside the
v0.5.x LongMemEval retrieval-quality line, window 2026-04-21 to
2026-04-26 for the Cerebral Valley "Built with Opus 4.7" hackathon).

This ADR freezes the Day-2 implementation contract so the six retriever
strategies, the two-layer router, and the evaluation protocol can all
land in parallel without drifting on intent boundaries. It transitions
from `Proposed` to `Accepted` when `parallax/retrieval/classify.py` and
`parallax/retrieval/routes.py` land and the first xcouncil A/B run
reports the six-number tuple specified in §6 below.

Builds on [ADR-003](ADR-003-target-kind-split.md) (the `target_kind`
split informs per-intent table selection) and
[ADR-005](ADR-005-claim-content-hash-user-id-scope.md) (user-scoped
content hash; every retriever filters by `user_id` consistently).
Supersedes nothing.

## Context

The v0.5.0 LongMemEval Run A (2026-04-20, Pro-judge, 500Q) produced
oracle_full 87.2% and `_s` full-context 86.0%. Per-type decomposition
pinpoints multi-session (~160Q) and temporal (~105Q) as the segment
that drags the `_s` score: single-session-user is already 95.5%, so
moving `_s` from 86.0% toward 90%+ means buying accuracy specifically
on those ~265Q.

Full-context is the current default path: for every question, the
answerer sees the entire user vault. That has two costs:

1. **Token bill** scales linearly with vault size. At a 1592-memory
   corpus (current E:\Parallax DB) a single answer call already spends
   ~20k input tokens; at 10× that corpus size the full-context path is
   not affordable per-question at Pro-model latency and cost.
2. **Signal dilution**: long contexts wash out the precise temporal
   anchor or cross-session reference the question actually needs. The
   per-type gap (multi_session/temporal underperform while
   single-session-user excels) is consistent with this — when the
   relevant memory is easy to retrieve ("which city in the last
   session"), full-context works; when it requires filtering across
   sessions or along a time axis, full-context buries the signal.

A retrieval-filtered path — classify the question, fetch only the slice
that answers it, hand that slice (not the vault) to the answerer —
attacks both costs. The risk is double-scoring: if the router picks
the wrong intent AND the specialised retriever fetches a narrow wrong
slice, `_s` regresses below the 86.0% full-context floor. Run A's
86.0% is the hard floor this ADR must not let xcouncil Phase 1 drop
under.

This ADR freezes the pipeline shape now — before
`parallax/retrieval/classify.py` is written — so the retriever
implementer, the router implementer, and the evaluator do not each
invent different intent sets and then have to reconcile at A/B time.

## Decision

Parallax implements a three-stage pipeline — **Router → Retriever →
Answerer** — with the following frozen contracts.

### 1. Six intents (closed set) with deterministic priority

The intent set is closed at six. Adding a seventh requires a new ADR,
not a rule tweak in `classify.py`.

| Priority | Intent | Rule-layer keyword / regex signals                                  | Target retriever                        |
| -------- | ------------------- | --------------------------------------------------------------------- | ---------------------------------------- |
| 1        | `temporal`          | `before`, `after`, `when`, `on <date>`, `第幾天`, `之前`, `之後`, ISO-date regex | time-window + recency merge              |
| 2        | `multi_session`     | `上次`, `之前你說`, `剛剛`, `last time`, `across sessions`, `earlier session`  | cross-session canonical merge            |
| 3        | `preference`        | `prefer`, `like`, `hate`, `favorite`, `偏好`, `喜歡`, `不喜歡`               | canonical fact lookup (no recency)       |
| 4        | `user_fact`         | `my name`, `I am`, `I live`, `我的`, `我是`, `我住`                         | canonical fact lookup (no recency)       |
| 5        | `knowledge_update`  | `now`, `現在`, `最新`, `changed to`, `已改為`, `updated`                     | latest-N + old-value pair                |
| 6        | `fallback`          | (matched when no other rule fires OR both gates in §2 fall through)  | MMR top-k, no filter                     |

**Priority / tie-break rule (INITIAL).** When ≥2 rules fire, the
lower `Priority` wins. `INTENT_PRIORITY` in
`parallax/retrieval/classify.py` = `("temporal", "multi_session",
"preference", "user_fact", "knowledge_update", "fallback")`. This
order is author intuition, NOT validated against tie-break cases
(e.g. "when did I last say I prefer coffee" fires temporal +
preference + multi_session and currently routes to temporal when
preference is likely correct). Day-2 builds a ≥10-question fixture
and locks the permutation that maximises correctness at `Accepted`;
the constant name is stable. Confidence-weighted mixing is rejected
in Alternatives §2.

### 2. Router: two-layer gate with frozen thresholds

```python
def classify(q: str) -> Intent:
    r = rule_classify(q)             # regex + keyword; (intent, confidence)
    if r.confidence >= 0.80:
        return r
    l = gemini_flash_classify(q)     # Gemini 2.5 Flash, JSON schema
    if l.confidence >= 0.70:
        return l
    return Intent("fallback", 0.0)   # stable path — never wrong by contract
```

Initial thresholds: **rule layer `>= 0.80`**, **LLM layer `>= 0.70`**
— author-intuition defaults matched to the 60/30/10 target split, not
empirically calibrated; runtime-configurable via
`parallax/retrieval/classify.py:DEFAULTS`. **Calibration protocol**:
transition to `Accepted` requires sweeping the pair on the 200Q
labeled set and locking the values that maximise `router_acc ×
(cond_acc | correct_route)` subject to §3's `fallback_e2e ≥ 0.95 ×
full_context_baseline` CI gate. §3 is the PRIMARY invariant; specific
values are SECONDARY — better values that still satisfy §3 are
adopted without a new ADR.

The LLM layer is invoked only when the rule layer falls through, so
Flash quota is bounded to ≤40% of question volume in the worst case.

`gemini_flash_classify` uses a JSON schema that forces the model to
return exactly one intent from the six + a confidence in `[0, 1]`; a
malformed response is treated as confidence 0 and cascades to
`fallback`.

### 3. Fallback route contract (empirically-monitored floor)

The `fallback` retriever returns MMR top-k with `k = K_MAX` (default
`32`) over the user-scoped union of `claims` and `events`. This is a
**K_MAX-bounded SUBSET** of full-context, not a superset: MMR top-32
can omit the single memory full-context would have surfaced. The
86.0% `_s` floor is therefore an **empirical commitment measured at
A/B time**, not a construction-time guarantee.

To make it falsifiable, every xcouncil A/B report surfaces
`fallback_e2e` (end-to-end accuracy restricted to questions routed to
`fallback`) as the **sixth number** alongside §6's tuple. CI fails
any report where `fallback_e2e < 0.95 × full_context_baseline`
(initial tolerance; tunable on transition to `Accepted`). This gate
is the primary invariant; router, retriever, and threshold choices
are secondary to keeping `fallback_e2e` above the floor.

**Corollary.** When router confidence is uncertain, the system routes
to `fallback`, not to a guess — the empirical-floor argument holds
only if uncertain questions land in the measurable fallback bucket,
so §2 keeps two thresholds rather than a soft mixture.

### 4. Per-intent retriever strategies

All five specialised retrievers plus `fallback` emit the same
`RetrievalEvidence` shape: `{hits, stages, notes, sql_fragments}`.
This re-uses the `RetrievalTrace` skeleton that lands in
v0.5.0-pre5, so the existing `inspect retrieve --explain` CLI works
for every intent without per-kind branching.

- **temporal** — extract `since` / `until` anchors from the question
  via the same `_iso_normalize` used by `by_timeline` (microsecond-
  inclusive); fetch `events` and `claims` whose `created_at` falls
  in the window; rank by recency. If no date anchor is extractable
  (e.g. "when did Chris prefer coffee" without a temporal phrase),
  demote to `fallback` and note `"no_temporal_anchor"`.
- **multi_session** — resolve the session reference ("上次" → most
  recent prior `session.start` event before `now`; "第二次" → second
  `session.start`); canonical-merge rows sharing a `content_hash`
  across sessions; rank by `recency × salience` where `salience` is
  the per-user claim count on the subject (ADR-005 user scope).
- **preference** — `by_entity` exact subject match + top-3
  embedding neighbours on the subject; **no recency weighting**
  (preferences are treated as static unless `knowledge_update` fires
  instead). Claims only; events excluded.
- **user_fact** — same retriever as `preference`, different rule
  patterns. Claims only.
- **knowledge_update** — top-N (`N = 3` default) newest claims on
  the subject, plus the immediately-preceding claim with the same
  `(subject, predicate)` tuple so the answerer sees both old and
  new values and can phrase the supersession correctly.
- **fallback** — MMR with `k = K_MAX` over the user-scoped union of
  `claims` and `events`. No intent-specific filtering.

**Escape-hatch invariant.** Any specialised retriever returning
`< K_MIN` hits (default `K_MIN = 3`) must append a
`"demoted_to_fallback"` note to its `RetrievalEvidence` and re-route
the question through `fallback`. This keeps the no-regression
guarantee in §3 load-bearing even when a question technically
classifies but its specialised corpus is empty.

### 5. Evidence-only answerer prompt

The answerer receives only `RetrievalEvidence.hits` plus the
question. No vault dump, no session history, no intent label (the
intent is a routing input, not an answerer input — feeding it would
leak router confidence into the answerer and couple the A/B
decomposition in §6).

The answerer prompt contract adds one required output behaviour: when
`hits` do not support the question, the answerer MUST emit the literal
token `insufficient_evidence` as its sole answer. The scorer treats
`insufficient_evidence` as a **third bucket** — `abstain` — distinct
from both correct and incorrect. This matters because:

- Scoring abstains as wrong masks router/retriever failure as
  answerer hallucination in the per-type breakdown.
- Scoring abstains as correct would let a lazy answerer game the
  benchmark by abstaining on hard questions.
- The `abstain_rate` becomes a first-class debug signal alongside
  `router_acc`: a rising `abstain_rate` on questions the router
  classified confidently means the retriever is the bottleneck.

### 6. A/B evaluation protocol

Every xcouncil Phase-1 A/B report decomposes end-to-end accuracy into
a frozen six-number tuple. Reporting a single e2e accuracy number is
non-conformant; the eval harness and any CI job that publishes
xcouncil numbers MUST surface all six:

1. **`router_acc`** — `classify(q)` vs a human-labeled intent set of
   `N ≥ 200` questions sampled from LongMemEval (target: 200 labels
   covering all six intents, skewed toward the 265Q multi-session +
   temporal segment that motivates this ADR).
2. **`cond_acc | correct_route`** — end-to-end accuracy restricted to
   the subset of questions where the router chose the labeled intent.
   Isolates retriever + answerer quality from router quality.
3. **`oracle_router_e2e`** — end-to-end accuracy when the router is
   forced to the labeled intent (oracle). Upper bound on what
   retriever + answerer can deliver if the router were perfect.
   `oracle_router_e2e − cond_acc | correct_route ≈ 0` means the
   router is not the next thing to invest in.
4. **`full_context_baseline`** — Run A Pro-judge `_s` = 86.0% (as of
   2026-04-20). Every xcouncil report states this number verbatim
   next to its e2e so the reviewer can see the floor.
5. **`abstain_rate`** — fraction of questions where the answerer
   emitted `insufficient_evidence`. Reported as a total AND broken
   down by intent (so "abstain_rate on temporal" is a first-class
   signal).
6. **`fallback_e2e`** — end-to-end accuracy restricted to questions
   the router sent to `fallback`. CI gate: `fallback_e2e ≥ 0.95 ×
   full_context_baseline`; any report below this floor is rejected.
   Empirical enforcement of §3's no-regression commitment.

Reports that surface fewer than six numbers are rejected at review;
the CI job that publishes xcouncil eval artifacts enforces this by
checking for all six field names in the output JSON.

## Consequences

- **`parallax/llm/call.py` unification becomes a prerequisite.** The
  classifier (Gemini Flash) and the answerer (Pro-grade model) must
  share one transport with per-call model override so the router can
  swap models without duplicating HTTP / retry / cache code. This
  pulls the "gemini.py 抽 call(model, ...)" pre-work item in front
  of retriever implementation, not behind it.
- **Per-question cache becomes mandatory.** A single A/B run hits the
  classifier 500× and the answerer 500× per pipeline variant; without
  caching, iterating on retriever tuning is cost-prohibitive. Cache
  key is `(model, normalized_question)` for the classifier and
  `(model, normalized_question, evidence_hash)` for the answerer, so
  tuning the retriever invalidates only answerer entries.
- **Abstain introduces a third scoring bucket.** Downstream dashboards
  (Notion Parallax 3-Lane Roadmap, LongMemEval per-type table) must
  be updated to display abstain separately from correct/incorrect
  before Phase 1 ships, otherwise abstains silently count as wrong
  and mask the failure-mode attribution that §5 is designed to
  expose.
- **Day-2 retriever work is insulated from intent-set churn.** The
  six intents, the K_MIN escape hatch, and K_MAX are frozen; the
  priority order (§1) and router thresholds (§2) are INITIAL but
  their names and positions are stable. Retriever implementation
  reads the interface from §1 and §4 regardless of calibration.
- **The answerer no longer sees incidental vault content.** Any
  previously-silent assumption that the answerer could pick up
  cross-question context from the full vault (e.g. a persona prompt
  that relied on seeing policy claims mixed into every call) must
  become an explicit retrieval path (a new intent) or move into a
  system-prompt template.
- **Router mis-classification is bounded by CI, not by construction.**
  Worst case is a question routed to `fallback` that would have been
  better served by a specialised retriever. §3's `fallback_e2e ≥
  0.95 × full_context_baseline` gate — the sixth number in §6 —
  enforces the bound at A/B time; the §4 escape-hatch
  (`demoted_to_fallback`) makes the surface area grep-auditable in
  every `RetrievalEvidence`.
- **Unstated assumptions, now explicit.** *Events user_id scope*:
  ADR-005's user_id scope extends from `claims` to `events` for
  fallback's union and multi_session/temporal — minor widening, no
  new ADR. *Single-intent assumption*: `classify()` returns one
  intent; multi-intent falls through §1's tie-break (Day-2 fixture
  quantifies). *Label-distribution assumption*: 200Q labeled ≈ 500Q
  is assumed; first A/B run MUST report `classify()`'s 500Q
  distribution and flag any intent differing by >20%. *zh keywords
  LongMemEval-inert*: LongMemEval is English-only, so §2's ~60%
  rule-gate is English-driven.

## Implementation Plan

The execution contract for moving this ADR from `Proposed` → `Accepted`.
Adopts **`B + Day-2-pivot-to-C`** from the 2026-04-20 xcouncil stability
review (Gemini 3.1 Pro / Codex / Claude Sonnet, adjudicated by Opus
judge). Route B scored 48/60 as main track, Route C scored 50/60 as
Day-2 pivot branch, Route A (strict-serial per-file implementation)
scored 23/60 and is rejected — A defers retrieval-ceiling discovery
to Day-5 when sunk cost is maximal.

### Three-tier success gates (decision thresholds, not slogans)

- **Floor** — `fallback_e2e ≥ 0.95 × 86.0% = 81.7%`. Below this, halt
  all complexity additions; fix `fallback` retrieval first.
- **Target** — `e2e_acc ≥ 88%`. Above Floor, only add per-intent
  retrievers with proven bucket-level `route_gain > 0`.
- **Stretch** — `e2e_acc ≥ 91%`. Pursue only after Target is met and
  `router_acc` is stable.

### Six-day schedule (hackathon window 2026-04-21 → 2026-04-26)

**Day-1 — Measure the three unknowns in parallel**
1. `fallback-only ablation`: fix `K_MAX=32 / K_MIN=3`, sweep `K`,
   claims/events weighting, and dedup strategy. Output: `fallback_e2e`
   plus per-bucket failure table.
2. `200Q threshold sweep`: sweep `rule ≥ 0.80` / `Flash ≥ 0.70` and
   neighbouring pairs. Report `router_acc`, `cond_acc | correct_route`,
   and false-positive route rate; do NOT pick on accuracy alone.
3. `INTENT_PRIORITY ≥20-question fixture`: ambiguous / multi-intent /
   cross-session cases (upgraded from the §1 ≥10 minimum). Purpose is
   tie-break stability, not score.

**Day-2 — Hard decision branch (the C-pivot gate)**
The pivot uses **two numbers**, not one. Aggregate `fallback_e2e`
alone mis-fires when `preference` (~30% of LongMemEval traffic per the
label-distribution assumption) drags the mean down while `temporal`
and `multi_session` — the buckets this ADR was actually built to fix —
still have oracle-bounded headroom. Pivoting to C on a low aggregate
would sunk-cost the wrong thing: abandon the per-intent retrievers
right when the `temporal` / `multi_session` slice shows they are
working.

Compute both numbers on Day-1's ablation output:

1. `fallback_e2e` — aggregate accuracy on questions routed to
   `fallback`.
2. `bucket_headroom[b]` for `b ∈ {temporal, multi_session}` — gap
   between the **oracle-routed** specialised retriever's
   `e2e_on_route` and `fallback_on_same_bucket` on the same bucket.
   A bucket is **"live"** if `bucket_headroom[b] ≥ +2pp` and the same
   sign holds across Day-1's two ablation seeds (per gate #6).

Pick exactly one branch:
- **`fallback_e2e` ≤ 87.5% AND no live bucket → pivot to Route C**:
  abandon per-intent retrievers, invest Day-3~5 fully in `fallback` +
  evidence-prompt tuning, pin Target = 88%.
- **`fallback_e2e` ≤ 87.5% BUT ≥ 1 live bucket → B-lite (narrow)**:
  ship only the live bucket(s). Aggregate is low but the buckets
  this ADR targets still have headroom — do not pivot C just because
  `preference` drags the mean.
- **87.5% < `fallback_e2e` < 89.5% → B-lite (standard)**: ship only
  the 2 high-ROI buckets (`temporal` + `multi_session`); every other
  intent stays on `fallback`.
- **`fallback_e2e` ≥ 89.5% → full B**: ship 3–4 per-intent
  retrievers, pursue Stretch = 91%.

Day-2 EOD freezes: best `fallback` params, router threshold candidate
set, `INTENT_PRIORITY` permutation, and the **live-bucket list** —
the set of per-intent retrievers authorised to proceed to Day-3. A
bucket not on the live list routes to `fallback` regardless of
implementer preference.

**Day-3 — Per-intent retrievers, verified one at a time**
- Implement one retriever, immediately run bucket-level A/B:
  `route_gain = e2e_on_route − fallback_on_same_bucket`.
- `route_gain ≤ 0` → withdraw the route, force its bucket back to
  `fallback`. Do NOT keep a losing route to avoid sunk cost.
- Only start the second retriever after the first proves net gain.

**Day-4 — Integration, guardrail, code freeze (EOD)**
- Router enables only proven-gain buckets; every other intent routes
  to `fallback`.
- Route-level guardrail: low router confidence OR sparse evidence →
  `abstain` or demote to `fallback`.
- Full six-number tuple report (§6).
- **Day-4 EOD code freeze**: router thresholds, MMR params, dedup,
  evidence formatting — no further structural changes.

**Day-5 — Final 24h, single-knob convergence**
- Only `per-intent abstain threshold` moves.
- Objective order: (1) never fall below Floor, (2) maximise `e2e_acc`,
  (3) keep `abstain_rate` from spiking unreasonably.

**Day-6 — Final eval and exit strategy**
Run three versions and submit the most defensible:
- `fallback-only` (保底 / insurance)
- `selected per-intent` (主力 / main)
- `full router enabled` (Stretch)

Selection rule: satisfy Floor → highest stable `e2e_acc` → if `full`
is high-score but high-variance, fall back to `selected per-intent`.

### Risk hedges (cross-referenced from §3 / §6 invariants)

- **Retrieval-ceiling risk** — Day-1 ablation exposes it; Day-2 branch
  refuses to build on an unmeasured ceiling.
- **Router compound-error risk** — Day-3 per-bucket `route_gain` gate
  + Day-4 guardrail ensure losing routes never ship.
- **Sunk-cost risk** — Day-3 one-retriever-at-a-time + immediate
  withdrawal rule keeps abandonment cheap.
- **Day-6 overfit risk** — Day-4 EOD code freeze + Day-5 single-knob
  rule prevents simultaneous multi-parameter tuning.
- **Baseline regression risk** — `fallback-only` stays production-
  viable throughout as a one-flag rollback.

### Transition criteria (Proposed → Accepted)

All eight must hold. The four **artifact gates** confirm deliverables
landed; the four **risk gates** confirm the result is trustworthy
rather than a lucky single-run draw. The artifact-only view that
shipped in the previous revision could mark the ADR Accepted on a
noisy baseline, an unstable per-intent win, or an abstain signal that
cannot distinguish router failure from retriever failure — the risk
gates close those holes.

**Artifact gates** (deliverables land):
1. `parallax/retrieval/classify.py` and `parallax/retrieval/routes.py`
   have landed with the frozen six-intent set and the frozen thresholds
   (or their 200Q-sweep replacement).
2. A first xcouncil A/B run has reported the full six-number tuple
   from §6 on a held-out sample.
3. `fallback_e2e ≥ 0.95 × 86.0%` (= 81.7%) on that report; any lower
   rejects the ADR for rework before Accepted.
4. `INTENT_PRIORITY` permutation is locked from the ≥20-question
   fixture; the `0.80 / 0.70` thresholds are re-stated (kept or
   replaced by the 200Q-sweep winner, which satisfies §3).

**Risk gates** (the number is real, not a lucky run):
5. **Baseline reproducibility** — re-running the Run A full-context
   `_s` eval on the same 500Q set twice (different seed / scheduler
   order) produces scores whose **max − min ≤ ±1pp** around 86.0%.
   If variance exceeds ±1%, the 81.7% floor itself is noisy and gate
   #3 is not meaningful; halt and stabilise the eval harness
   (temperature pinning, judge determinism, tie-break seeding) before
   Accepted. Without this gate, #3 could pass on a favourable draw
   and fail on a second run.
6. **Per-intent gain stability** — every per-intent retriever that
   ships must show `route_gain > 0` on its bucket across **two
   independent A/B runs** (different seed / question order), and at
   least one of `temporal` or `multi_session` must clear this bar
   before any per-intent retriever is Accepted. A retriever that
   wins once and loses once does not count as proven — it ships on
   `fallback` until stabilised. This converts §6 per-intent numbers
   from point estimates into load-bearing ship decisions.
7. **Cache hit rate** — on the second A/B run of an unchanged config
   the classifier cache reports hit rate ≥ 90% and the answerer cache
   ≥ 50%. Lower rates indicate cache-key instability (question
   normalisation drift, evidence-hash churn) which makes subsequent
   tuning cost-unbounded and invalidates the §Consequences "cache
   is mandatory" claim. A cached A/B that is not actually cached
   silently triples the eval budget for every later iteration.
8. **Abstain source distinguishable** — `abstain_rate` is reported
   split into at minimum `abstain_on_confident_route` vs
   `abstain_on_fallback`, and per-intent for the confident-route
   slice. A single aggregate `abstain_rate` does not satisfy this
   gate. Without the split, rising abstains cannot be attributed to
   router (wrong intent, retriever finds nothing) vs retriever
   (right intent, narrow miss) vs answerer (right slice, refuses to
   answer), defeating §5's design purpose and the §6 attribution
   guarantee.

## Alternatives considered

1. **Single-stage retrieval with no router (one MMR retriever over
   embeddings for every question).** Rejected because the per-type
   Run A analysis shows temporal and multi_session questions need
   structural filters (explicit time windows, session anchors) that
   embedding MMR cannot express without a prohibitively high k. The
   cost to match specialised-retriever accuracy via MMR alone would
   push `k` past the point where the answerer's context again
   becomes the bottleneck — i.e. we end up re-creating the
   signal-dilution problem this ADR is designed to solve.

2. **Confidence-weighted mixture of intents (soft routing, union of
   per-intent evidence sets).** Rejected for two reasons:
   - Token cost balloons because the answerer receives the union of
     multiple specialised retrievers' hits; the main motivation
     (shrink context relative to full-vault) is defeated.
   - The A/B decomposition in §6 breaks: `router_acc` is undefined
     when no single intent is chosen, so there is no way to isolate
     router failure from retriever failure from answerer failure.
     The point of xcouncil Phase 1 is to produce that attribution,
     not to maximise raw e2e at the cost of unreadable diagnostics.

3. **End-to-end learned router (fine-tune a small model on labeled
   intents).** Rejected for Phase 1:
   - The 200Q human-labeled set is too small to train without
     leakage into the very evaluation the router is being scored on.
   - The rule + Flash two-layer gate is already cheap and auditable
     — a mis-classification has a grep-able root cause ("rule
     `temporal/before` fired with confidence 0.82") that a learned
     router obscures.
   - A learned router would need retraining every time the intent
     set changes; the rule + prompt approach is a two-line diff.
     Revisit in Phase 3+ once the intent set has actually stabilised.

4. **Put retrieval inside the answerer as a tool call (ReAct-style
   agent).** Rejected for Phase 1: introduces latency variance
   (variable tool-call depth) that makes the A/B comparison against
   a fixed-latency full-context baseline uninterpretable. Also
   re-introduces the "answerer sees everything" failure mode — the
   agent can tool-call itself back into vault-wide retrieval and
   silently undo the Phase-1 filtering. Reconsider in Phase 2 once
   the retrieval-filtered path has an oracle-bounded measurement.

5. **Skip the `insufficient_evidence` abstain token; score abstains
   as wrong.** Rejected because it collapses two distinct failure
   modes (router picked wrong intent; retriever fetched wrong slice
   within the right intent) into one scoring bucket, defeating the
   A/B decomposition. The abstain bucket is what makes `abstain_rate`
   by intent a first-class signal in §6.

## Evidence provenance — [M] / [E] / [A] classification

Every number in this ADR falls into exactly one of three provenance
classes. The class determines what action a reviewer should take when
the number looks wrong: `[M]` numbers are challenged by re-running the
eval; `[E]` numbers are challenged by a small calibration run; `[A]`
numbers are challenged by reading the code path they describe.

- **[M] Measured** — produced by a specific, reproducible eval run
  (commit SHA + config + seed). Replacement requires a new run, not
  an argument.
- **[E] Estimated** — author intuition or proxy calculation, not
  directly measured. Safe to adjust if a calibration run disagrees;
  not safe to cite as evidence.
- **[A] Assumed** — structural assumption about future state,
  distribution, or environment. May be wrong; first A/B run should
  flag any that fail to hold.

| Value in ADR                                 | Class | Source / basis                                                                                      |
| -------------------------------------------- | ----- | --------------------------------------------------------------------------------------------------- |
| `_s` full-context 86.0%                      | [M]   | Run A, 2026-04-20, Pro-judge, 500Q                                                                  |
| `oracle_full` 87.2%                          | [M]   | Run A, same run                                                                                     |
| single-session-user 95.5%                    | [M]   | Run A per-type decomposition                                                                        |
| ~1592-memory corpus                          | [M]   | E:\\Parallax DB row count at ADR draft time                                                          |
| ~20k input tokens / answer call              | [M]   | Observed token counter on Run A answerer calls                                                      |
| Floor = 0.95 × 86.0% = 81.7%                 | [M]   | Arithmetic on a [M] number                                                                          |
| ~160Q multi-session, ~105Q temporal          | [E]   | Approximate bucket counts from Run A per-type table; exact counts re-read before Day-1               |
| Rule threshold 0.80 / Flash threshold 0.70   | [E]   | Author intuition matched to 60/30/10 target split; replaced by 200Q sweep winner before Accepted     |
| K_MAX = 32, K_MIN = 3                        | [E]   | Author default; Day-1 ablation sweeps K and may replace                                               |
| Day-2 pivot thresholds 87.5% / 89.5%         | [E]   | Author intuition from the 86.0% floor and the 91% stretch; not derived from a calibration run        |
| Live-bucket threshold `≥ +2pp` headroom      | [E]   | Chosen to exceed plausible single-run noise at 500Q; confirmed if gate #5 (±1% reproducibility) holds |
| Baseline reproducibility tolerance ±1pp      | [E]   | Author estimate of acceptable judge/scheduler variance; tightens if first reproduce run is ≤0.5pp    |
| Cache hit rate targets 90% / 50%             | [E]   | Derived from expected question / evidence stability across an unchanged-config A/B pair              |
| `MAX_EVIDENCE_TOKENS = 6000`                 | [E]   | Context-budget headroom under Pro-grade answerer limits; not load-tested on largest corpora           |
| Intent split target 60/30/10                 | [A]   | Assumed LongMemEval rule-gate distribution; first A/B run MUST report observed split and flag >20% drift |
| 200Q labeled ≈ 500Q distribution             | [A]   | Assumed representative sampling; quantified by reporting `classify()`'s 500Q distribution vs label set   |
| Router returns one intent per question       | [A]   | Single-intent model; multi-intent cases fall through §1 tie-break                                      |
| Events inherit user_id scope from claims     | [A]   | Extending ADR-005 scope; validated by grep audit of every retriever in §4                              |
| LongMemEval is English-only (zh rules inert) | [A]   | Corpus property at the benchmark version used; re-check if LongMemEval updates                          |

**Convention going forward.** Any new numeric claim added to this ADR
(or to downstream ADRs that cite it) carries one of `[M]` / `[E]` /
`[A]` when first introduced. CI or review catches un-tagged numbers at
PR time; the cost of the tag is one character of metadata, the benefit
is that reviewers stop mistaking `[E]` / `[A]` values for `[M]` ground
truth.

## References

- `parallax/retrieval/classify.py` — intent classifier (rule layer +
  Gemini Flash fallback, `INTENT_PRIORITY` constant). To be added
  Day 1–2. (Existing `parallax/retrieve.py` stays as a thin re-export
  into `parallax/retrieval/`; no Phase-1 migration.)
- `parallax/retrieval/routes.py` — `ROUTE = {intent: Retriever}`
  mapping table.
- `parallax/retrieval/retrievers.py` — the five specialised
  retrievers and the `fallback` MMR retriever.
- `parallax/answer/evidence.py` — evidence-only prompt template
  and `insufficient_evidence` token contract.
- `parallax/llm/call.py` — unified `call(model, messages, **kw)`
  transport; prerequisite pre-work for both the router and the
  answerer.
- Parallax 3-Lane Roadmap Notion page — primary source of the 86.0%
  `_s` / 87.2% oracle_full Pro-judge baseline (Run A, 500Q,
  2026-04-20) this ADR must not regress below.
  `eval/longmemeval/run_a_20260420.md` is a future artifact path
  reserved for the eval-harness replay script.
- [ADR-003](ADR-003-target-kind-split.md) — `events.target_kind`
  vs `decisions.target_kind` split informs which table each
  per-intent retriever scans (events for temporal / multi_session,
  claims for preference / user_fact / knowledge_update).
- [ADR-005](ADR-005-claim-content-hash-user-id-scope.md) — every
  retriever in §4 filters by `user_id` consistent with the
  user-scoped content hash; fallback's union over `claims` +
  `events` also scopes by `user_id`.
- Cerebral Valley "Built with Opus 4.7" Claude Code hackathon
  window 2026-04-21 to 2026-04-26 — target ship date for the
  Phase 1 A/B baseline.

## Implementation Notes (Day-0, 2026-04-20)

**Note 1 — Intent routing pragmatism (pre-embedding infra):**
Until a production-grade semantic embedding layer ships (targeted
post-hackathon), the three intent classes below route to the generic
fallback retriever rather than specialised pipelines:

- `preference`
- `user_fact`
- `knowledge_update`

Rationale: each needs a bespoke retriever (contradiction detection,
latest-wins dedup, entity-scoped search) whose quality depends on semantic
embeddings we have not yet validated at scale. Shipping them on BM25 +
rules would regress `fallback_e2e` below the 0.817 CI floor. Day-3 work
explicitly skips these intents; Phase 2 re-opens them once infra lands.

**Note 2 — Fallback evidence budget:**
`MAX_EVIDENCE_TOKENS = 6000` is a hard ceiling enforced in
`parallax/retrieval/retrievers.py::fallback_retrieve`, applied in
conjunction with `K_MAX = 32` hits. Token estimation uses the cheap
`len(text) / 4` heuristic. Items exceeding the budget are dropped from the
tail (after recency top-3 is pinned to the front) so the highest-signal
context survives. This protects against model context overflow on long
corpora and was not specified in the original ADR body.
