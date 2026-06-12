# M8 — Semantic Retrieval Plan

Status: design (Phase 1). Author: lane B (/land). Branch: `feat/m8-semantic-retrieval`.

Chris approved M8 GO on 2026-06-11: replace the lexical BM25 stub used in the
LongMemEval harness — which the v0.6 burn proved too weak on multi-session
questions (10–25%, a recall problem) — with a true hybrid retriever:
**lexical (BM25-lite) + dense vector cosine, fused with Reciprocal Rank Fusion
(RRF, k=60)**. Embedding model = `bge-m3` (1024-dim) served by the GB10 Ollama
instance at `http://192.168.1.134:11434`.

This document surveys the existing retrieval paths, fixes the storage/provider
design to Parallax's SQLite/in-proc ecosystem, and specifies the flag, the
module layout, and the test plan. Phase 2 implements exactly what is written
here.

---

## 1. Existing retrieval paths (survey)

### 1.1 `parallax/retrieval/` (ADR-006 Phase 1 primitives)

* `parallax/retrieval/contracts.py:38` — `RetrievalEvidence`, a frozen dataclass
  with `hits: tuple[dict, ...]`, `stages`, `notes`, `sql_fragments`,
  `diversity_mode`. This is the canonical evidence bundle every retriever
  returns. The new hybrid retriever MUST return this same type so it is a
  drop-in alongside `fallback_retrieve`.
* `parallax/retrieval/retrievers.py:201` — `fallback_retrieve(conn, user_id,
  query, ...)`. Today it:
  * pulls a recency-ordered candidate pool from `claims` + `events`
    (`_fetch_candidates`, `retrievers.py:85`),
  * tries a `sentence-transformers` MMR path (`_load_model`,
    `retrievers.py:37`; `_mmr_rank`, `retrievers.py:163`),
  * **falls back to a pure-lexical overlap stub** when the model is
    unavailable (`_bm25_stub_rank`, `retrievers.py:151`).
* **Reality check on the env**: `sentence-transformers` and `numpy` are NOT
  installed in `.venv` (verified). So in CI / the test env the MMR path is dead
  code today — `_load_model()` returns `None` and `fallback_retrieve` always
  uses `_bm25_stub_rank`. Any dense path we add MUST be **pure-Python** (no
  numpy), or it is dead on arrival in the same way. `_mmr_rank` imports numpy
  locally (`retrievers.py:169`), confirming numpy was always an optional,
  embedding-only dependency, never core.

### 1.2 `eval/longmemeval/` (the harness M8 actually targets)

* `eval/longmemeval/store.py:95` — `build_from_parallax_retrieval(conn, q, *,
  top_k=64, max_chars=40000)`. **This is the BM25 stub M8 replaces.** It:
  * reads ingested rows via `memories_by_user(conn, user_id)`
    (`store.py:135`),
  * scores each row by lexical token overlap with `q.question`
    (`_score`, `store.py:141`; `_tokenize`, `store.py:31`),
  * keeps top-`top_k`, applies a char budget, re-sorts kept rows into
    `vault_path` (chronological) order for emission.
* `eval/longmemeval/pipeline.py:101` — `run_one(..., use_retrieval=False)`.
  When `use_retrieval=True` the answer prompt is built from
  `build_from_parallax_retrieval`; otherwise from `dump_all_sessions(q)` (the
  v1 long-context bypass). `run_one` already fails loud on empty transcript
  (`pipeline.py:145`).
* `eval/longmemeval/run.py:106` — `--use-retrieval` CLI flag threads into
  `run_one`. Default off preserves Run B (88.92%) reproducibility.
* `eval/longmemeval/store.py:49` — `ingest_question` writes each turn as a
  `Memory` row (`title = "[date] role"`, `summary = turn.content`,
  `vault_path = lme/<qid>/s<si>/t<ti>`). The dense retriever embeds
  `f"{title} {summary}"` — the same blob `_score` tokenizes today
  (`store.py:142`).
* Per-question memory is **ephemeral**: `ephemeral_store` (`store.py:36`)
  spins a fresh temp SQLite DB per question and tears it down. Haystacks are
  small (tens–hundreds of turns). This is the decisive fact for storage
  design (§2).

### 1.3 Legacy `parallax/retrieve.py`

Typed lexical/index retrieval (`RetrievalHit`, `recent_context`, `by_*`).
Out of M8 scope — untouched. The injector (`parallax/injector.py`) and the
M4/M5 canary/audit/dual-read chains are out of scope and untouched.

---

## 2. Vector storage design

### Decision: in-process, per-call ephemeral index. No persisted vectors.

Rationale, ranked by weight:

1. **The corpus M8 targets is ephemeral and small.** The LongMemEval store is
   re-created per question (`ephemeral_store`, `store.py:36`) and discarded.
   Persisting vectors into that DB buys nothing — they would be written and
   thrown away within one `run_one` call. Embedding the candidate blobs once
   per retrieval call and ranking in memory is the natural fit.
2. **No new heavy dependency** (hard boundary #3). pgvector/faiss are banned;
   Parallax is a SQLite/in-proc package. A pure-Python cosine over a few
   hundred 1024-dim vectors is microseconds — no ANN index needed at this
   scale.
3. **No numpy** — numpy is not installed and is not a core dep (§1.1). Cosine
   is implemented with the stdlib `math` module over `list[float]`.

### What we explicitly reject and why

* **SQLite BLOB vector column / sidecar `.npy`**: adds a migration + a schema
  surface to a hot path that resets every question. Pure overhead at this
  scale. Revisit only if/when a *durable* user-level corpus needs semantic
  recall (a future milestone, not M8).
* **pgvector / Postgres**: forbidden (hard boundary #3); wrong ecosystem.
* **faiss / hnswlib**: forbidden heavy dep; unnecessary below ~10⁴ vectors.

### Caching

Within a single retrieval call we embed each candidate blob once. We add a
small process-local `(provider_id, text) -> vector` LRU-ish dict guarded by a
lock, mirroring the existing `_EMB_CACHE` pattern (`retrievers.py:33`). Cache
is best-effort and process-local; correctness never depends on it. This keeps
repeated sweeps over the same corpus from re-hitting Ollama.

---

## 3. Embedding provider interface

A minimal protocol so tests never need GB10 and prod points at bge-m3.

```python
class EmbeddingProvider(Protocol):
    dim: int
    id: str  # stable identity for the per-call cache key
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
```

### 3.1 `DeterministicStubProvider` (tests + offline default)

* Maps each text to a fixed-dim (`dim=64` for tests) unit vector derived
  deterministically from a hash of the text (e.g. seed a `random.Random` with
  `blake2b(text)` and draw `dim` Gaussians, then L2-normalize). Pure stdlib.
* Same text → identical vector (determinism); different texts → different
  vectors; lexically/semantically related test strings are *constructed* to be
  near each other in the fixtures so the dense path is exercised meaningfully.
* Zero network. This is what the unit suite uses, and what the semantic path
  falls back to if no provider is wired and the flag is on.

### 3.2 `OllamaEmbeddingProvider` (prod, bge-m3)

* `httpx` at module level (already a dep — `httpx==0.28.1` verified; mirrors
  `parallax/extract/providers/openrouter.py:13`).
* `dim = 1024`, model `bge-m3`.
* Base URL from env `PARALLAX_EMBEDDING_BASE_URL`, default
  `http://192.168.1.134:11434` (GB10). Calls `POST {base}/api/embeddings` (or
  `/api/embed` for batch) with `{"model": "bge-m3", "prompt": text}`.
* SSRF note (mirror `openrouter.py:26` docstring): `base_url` is operator
  config, not validated; pin in deployment.
* Network/HTTP errors are caught and surfaced as a typed failure so the
  caller can degrade to lexical-only (never crash a retrieval).
* `timeout` configurable, default 30s.

Provider construction is centralized in a small factory keyed on env so the
hot path stays clean. Default factory returns the stub unless an Ollama base
URL/flag explicitly selects the live provider — so importing the module never
makes a network call.

---

## 4. RRF merge design

For a query we produce two ranked lists over the same candidate pool:

* **Lexical list** — BM25-lite (token-overlap / IDF-lite ranking; we reuse the
  existing tokenizer semantics so behavior is comparable to the stub it
  replaces). Ranked descending by lexical score.
* **Dense list** — cosine similarity between the query embedding and each
  candidate embedding (pure-Python `math` cosine). Ranked descending.

Fuse with Reciprocal Rank Fusion:

```
rrf_score(d) = Σ_over_lists 1 / (k + rank_list(d))      # k = 60, ranks 1-based
```

* `k = 60` (Perihelion v0.4 / Cormack et al. default).
* A document present in only one list still scores from that list (the other
  contributes 0).
* Ties in the fused score break deterministically on a stable key
  (`vault_path` for the eval store, `id` for the `retrieval/` pool) so reruns
  are bit-identical — matching the existing determinism contract
  (`test_longmemeval_retrieval.py:175`).
* Output: candidates sorted by `rrf_score` desc, then the existing top_k +
  char/token budget + chronological re-sort is applied unchanged.

A small pure function `rrf_merge(ranked_lists, k=60, tie_key) -> list[item]`
lives in the new core module and is unit-tested in isolation (rank math,
single-list, ties, empty).

---

## 5. Flag design (default OFF, off ⇒ bit-for-bit identical)

Single env flag, parsed with the existing `_TRUTHY` convention
(`parallax/config.py:37`):

* `PARALLAX_SEMANTIC_RETRIEVAL` — `1/true/yes/on` enables the hybrid path.
  **Default OFF.**

Off-path guarantee:

* `eval/longmemeval/store.py::build_from_parallax_retrieval` keeps its current
  body verbatim as the OFF branch. When the flag is OFF the function takes the
  *exact same code path* it does today — same tokenizer, same scoring, same
  ordering, same return value. The hybrid branch is only entered when the flag
  is ON. No signature change for existing callers (new keyword-only params have
  defaults; a hidden `provider`/`use_semantic` seam defaults to env).
* The new core module (`parallax/retrieval/semantic.py`) is pure addition; no
  existing import path changes when the flag is off.
* Supporting env (only consulted when the flag is ON, so OFF is unaffected):
  * `PARALLAX_EMBEDDING_BASE_URL` (default GB10),
  * `PARALLAX_EMBEDDING_MODEL` (default `bge-m3`).

Regression evidence required before PR (gate, §7):
* Full existing suite green at ≥ baseline (2094 collected).
* A dedicated flag-OFF parity test asserting hybrid-OFF output ==
  pre-existing lexical output for a fixed fixture (byte-identical transcript).

---

## 6. Module layout (Phase 2)

New files (additive):

* `parallax/retrieval/embeddings.py`
  * `EmbeddingProvider` protocol,
  * `DeterministicStubProvider`,
  * `OllamaEmbeddingProvider` (httpx, bge-m3),
  * `get_embedding_provider()` env-driven factory (stub by default).
* `parallax/retrieval/semantic.py`
  * pure-Python `cosine(a, b)` (stdlib `math`),
  * `rrf_merge(...)`,
  * `lexical_rank(query, items)` (BM25-lite, reusing tokenizer semantics),
  * `dense_rank(query, items, provider)`,
  * `hybrid_rank(query, items, provider, *, k=60) -> list[int]` — the core
    fusion entrypoint returning candidate indices in fused order.
* `parallax/retrieval/config.py` (or extend `parallax/config.py`)
  * `semantic_retrieval_enabled()` and embedding env readers. (Decision:
    keep semantic knobs in a small `retrieval/config.py` to avoid widening the
    frozen `ParallaxConfig`; revisit if it needs to join the main config.)

Touched files (surgical, flag-gated):

* `eval/longmemeval/store.py` — add hybrid branch to
  `build_from_parallax_retrieval`, gated on `semantic_retrieval_enabled()`;
  OFF branch byte-identical to today.

Out of scope (explicitly NOT touched):

* LongMemEval **judge** — neutral judge work is parked until 2026-06-15
  (Claude API key). We leave at most an interface seam (the existing
  `judge_model` parameter already is one); no judge code is written.
* `parallax/retrieve.py`, `injector.py`, M4/M5 canary/audit/dual-read,
  `parallax/router/*`.

---

## 7. Test plan

All deterministic — **no test depends on GB10 being online**. The stub
provider backs every unit test.

`tests/test_semantic_retrieval.py` (new, core module):
* `cosine`: identical vectors → 1.0; orthogonal → 0.0; zero-vector safe;
  symmetry.
* `rrf_merge`: known 2-list ranking → hand-computed fused order; single-list
  passthrough; ties break on tie_key; empty lists → empty.
* `DeterministicStubProvider`: same text → identical vector; different text →
  different; output L2-normalized; `dim` honored.
* `hybrid_rank`: a fixture where the dense path surfaces a semantically-near
  row that lexical overlap misses (proves hybrid > lexical-only), and the
  reverse (lexical exact-match survives fusion).
* `OllamaEmbeddingProvider`: HTTP mocked via monkeypatched `httpx.post`
  (mirror `tests/test_extract_*` mock style) — request shape (model, prompt,
  URL from env), response parse, error → typed failure (no crash). No live
  call.

`tests/eval/test_longmemeval_semantic.py` (new, harness wiring):
* **Flag OFF parity**: `build_from_parallax_retrieval` output is byte-identical
  to the current lexical output for `_fixture_question` (regression proof).
* **Flag ON** (env monkeypatched, stub provider injected): hybrid path runs,
  returns a non-empty chronological transcript, honors top_k + char budget,
  is deterministic across two runs, skips NULL fields (re-assert the existing
  `test_longmemeval_retrieval.py` invariants under the ON path).
* Flag ON with a forced provider error → degrades to lexical-only, never
  raises.

`tests/eval/test_longmemeval_live_smoke.py` (new, **skip by default**):
* Marked `@pytest.mark.integration` (existing marker, `pyproject.toml`) and
  guarded by an env switch (e.g. `PARALLAX_EMBEDDING_LIVE=1`), so default
  `pytest` collection skips it. Hits GB10 bge-m3, asserts a 1024-dim vector
  comes back. Documents the live path without making CI depend on GB10.

Gate (all green before PR):
* `pytest` full suite: 0 fail, pass count ≥ baseline (2094 collected); any
  pre-existing parallax e2e 401 failure is recorded as known-unrelated.
* `ruff check parallax eval tests` clean (0 new errors; selectors `E,F,I,UP,B`
  per `pyproject.toml`).
* `mypy` clean per repo convention (0 new errors) on touched modules.
* Manual `git diff --staged` secret grep before commit (Win hook fail-open).

---

## 8. Phase 2 build order

1. `parallax/retrieval/embeddings.py` + `tests/test_semantic_retrieval.py`
   (provider + stub + cosine + rrf, all offline).
2. `parallax/retrieval/semantic.py` `hybrid_rank` + tests.
3. `parallax/retrieval/config.py` flag + env readers + tests.
4. Wire flag-gated hybrid branch into
   `eval/longmemeval/store.build_from_parallax_retrieval` +
   `tests/eval/test_longmemeval_semantic.py` (parity + ON-path).
5. Live smoke test (skip-by-default).
6. Full gate, then PR (base `main-next`).
