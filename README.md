# Parallax-Kernel

> **⚠️ This branch (`main`) is archived.** Active development happens on
> [`main-next`](https://github.com/Chris97924/Parallax-Kernel/tree/main-next).
> `main` is frozen at the `v0.2.1` release (tag
> [`v0.2.x-archive-main`](https://github.com/Chris97924/Parallax-Kernel/releases/tag/v0.2.x-archive-main))
> and is kept for historical reference only — open PRs and issues against
> `main-next`, not here.

Content-addressed canonical knowledge-base store. P0 core: 6-object schema
(`sources`, `memories`, `claims`, `decisions`, `events`, `index_state`) with
`content_hash` dedup on memories and claims, race-safe UPSERT ingest, an
append-only event log, an explicit state machine for lifecycle transitions,
and a deterministic search-index rebuild path.

> The GitHub repo is named `parallax-kernel`. The Python package is imported
> as `parallax`, so `pip install parallax-kernel` gives you
> `from parallax import ...`.

## Quick Start

Three reproducible steps from a clean clone:

```bash
# 1. Clone + install (editable + dev extras for tests)
git clone https://github.com/<your-user>/parallax-kernel.git
cd parallax-kernel
pip install -e .[dev]

# 2. Bootstrap a fresh Parallax instance at any path
python bootstrap.py /tmp/my-parallax

# 3. Run the end-to-end quickstart (bootstrap → ingest → retrieve)
python examples/quickstart.py
```

## Public API

Everything re-exported from the `parallax` package root:

| Name | Kind | Role |
|---|---|---|
| `ingest_memory` | function | UPSERT a memory; returns the persisted `memory_id`. |
| `ingest_claim` | function | UPSERT a claim; returns the persisted `claim_id`. |
| `memories_by_user` | function | List memories for a user, optional `state` filter. |
| `claims_by_user` | function | List claims for a user, optional `state` filter. |
| `claims_by_subject` | function | List a user's claims filtered by exact subject. |
| `memory_by_content_hash` | function | Lookup one memory by content_hash (`Optional[dict]`). |
| `claim_by_content_hash` | function | Lookup one claim by content_hash (`Optional[dict]`). |
| `record_event` | function | Append a raw event row to the immutable event log. |
| `record_memory_reaffirmed` | function | Bump a memory's `reaffirm_count` and emit a `memory_reaffirmed` event. |
| `record_claim_state_changed` | function | Apply a claim state transition and append the matching event. |
| `is_allowed_transition` | function | Check `(from_state, to_state)` against the per-object transition table. |
| `MEMORY_TRANSITIONS` / `CLAIM_TRANSITIONS` / `SOURCE_TRANSITIONS` / `DECISION_TRANSITIONS` | frozenset | Allowed lifecycle transitions per object kind. |
| `rebuild_index` | function | Idempotent rebuild of `index_state` for a named search index. |
| `target_ref_exists` | function | Existence check used to reject orphan events/decisions at the boundary. |
| `VALID_TARGET_KINDS` / `DECISION_TARGET_KINDS` / `TargetKind` | constants | Target-kind allow-lists for events and decisions. |
| `parallax_info` / `ParallaxInfo` | function / dataclass | Runtime introspection (version, modules, schema). |
| `health` | function | Operational snapshot (db path, table counts, journal mode, last error). |
| `Source` / `Memory` / `Claim` / `Event` | dataclass | Frozen record types used by the storage layer. |
| `__version__` | str | Package version (currently `0.2.0`). |

## Modules

| Module | Role |
|---|---|
| `parallax.hashing` | `normalize(*parts)` + `content_hash(*parts)` — SSoT for dedup keys. |
| `parallax.config` | Frozen `ParallaxConfig` + `load_config()` (env-driven). |
| `parallax.sqlite_store` | Narrow SQLite surface. Events are append-only. |
| `parallax.migrations` | Forward-only numbered migrations (`m0001`–`m0005`). `migrate_to_latest()` is idempotent and atomic per migration. |
| `parallax.ingest` | UPSERT `ingest_memory` / `ingest_claim` with synthetic `direct:<user_id>` source. |
| `parallax.retrieve` | Read helpers returning `dict` / `Optional[dict]`. |
| `parallax.events` | Append-only event recorders + reaffirm / state-change helpers. |
| `parallax.transitions` | Per-object allowed-transition tables + `is_allowed_transition()`. |
| `parallax.validators` | Target-kind allow-lists + `target_ref_exists()` for orphan rejection. |
| `parallax.index` | `rebuild_index()` — deterministic, idempotent rebuild of `index_state`. |
| `parallax.telemetry` | Stdlib-only structured events + in-memory metrics + `health()`. |
| `parallax.introspection` | `parallax_info()` / `ParallaxInfo` runtime metadata. |
| `parallax.obs.log` / `parallax.obs.metrics` | Lower-level logging + metrics primitives backing `telemetry`. |
| `bootstrap.py` | One-shot initializer + CLI. |

## State Machine

Lifecycle transitions are explicit and centralised in `parallax.transitions`.
Each object kind ships a frozenset of allowed `(from_state, to_state)`
pairs; `record_claim_state_changed()` (and friends) call
`is_allowed_transition()` before mutating, so any disallowed transition
raises before it can pollute the event log.

```python
from parallax import CLAIM_TRANSITIONS, is_allowed_transition

assert ("pending", "confirmed") in CLAIM_TRANSITIONS
assert is_allowed_transition("claim", "pending", "confirmed")
```

## Acceptance Harness

`tests/acceptance/` is the SQL-level acceptance suite that proves the
canonical KB at the DB layer (not just at the Python boundary). Four `.sql`
files are the SSoT — the pytest runner is a thin parametrize wrapper, no
SQL is duplicated inline.

| File | Question it answers |
|---|---|
| `01_canonical.sql` | Does the canonical KB exist? (claims + memories non-empty) |
| `02_identity.sql` | Does every object have a PK, and does the claim→source FK join? |
| `03_state_traceable.sql` | Can any claim's state history be replayed from `events`? |
| `04_rebuild_identical.sql` | Is `rebuild_index()` byte-stable across repeat calls? |

```bash
python -m pytest tests/acceptance/ -q
```

## Coverage Gate

`pyproject.toml` enforces a global 80% coverage gate over the `parallax`
package via pytest's `--cov-fail-under=80`. CI fails the build if total
coverage drops below the threshold:

```bash
python -m pytest                       # uses the gate from pyproject.toml
python -m pytest --cov-report=html     # local exploration
```

## Observability

Parallax ships with a single-file, stdlib-only telemetry module
(`parallax/telemetry.py`, under 200 lines). No extra dependencies, no
Prometheus exporter -- Prometheus is intentionally out of scope (YAGNI);
call `snapshot()` and adapt to whatever scrape format the caller needs.

### Structured events

All events are emitted as single-line JSON records under the `parallax.*`
logger namespace (keys: `ts`, `level`, `logger`, `msg`, plus any flattened
extras including `event`).

| Event | Level | Emitted when |
|---|---|---|
| `dedup_hit` | INFO | An UPSERT collapses onto an existing `content_hash`. |
| `state_changed` | INFO | A row transitions between lifecycle states. |
| `orphan_rejected` | INFO | A target-less event/decision is rejected at the boundary. |
| `ingest_error` | ERROR | Any exception inside `ingest_memory` / `ingest_claim`. |

### In-memory metrics

Thread-safe counters plus a bounded (1024-sample) latency ring buffer with
nearest-rank percentiles:

- `ingested_total` -- successful `ingest_memory` + `ingest_claim` calls
- `dedup_hits_total` -- content_hash collisions absorbed by UPSERT
- `errors_total` -- exceptions captured by `emit_ingest_error`
- `latency_p50_ms` / `latency_p95_ms` / `latency_p99_ms`
- `last_error` -- timestamped string from the most recent `emit_ingest_error`

### Health snapshot

`parallax.health(db_path)` returns a dict with the DB path (resolved
absolute), per-table row counts, the SQLite `journal_mode` (expected
`"wal"` on a bootstrapped instance), and `last_error`.

```python
from parallax import health, telemetry

# Drive some ingest traffic first (see examples/quickstart.py).
print(telemetry.snapshot())
# {'ingested_total': 42, 'dedup_hits_total': 3, 'errors_total': 0,
#  'latency_p50_ms': 1.8, 'latency_p95_ms': 4.1, 'latency_p99_ms': 7.2,
#  'last_error': None}

print(health("/tmp/my-parallax/db/parallax.db"))
# {'db_path': '/tmp/my-parallax/db/parallax.db',
#  'table_counts': {'sources': 1, 'memories': 12, 'claims': 30,
#                   'decisions': 0, 'events': 0, 'index_state': 0},
#  'journal_mode': 'wal',
#  'last_error': None}
```

## Testing

```bash
pytest                              # full suite, with coverage gate
python -m pytest tests/acceptance/  # SQL acceptance harness only
```

GitHub Actions runs the suite on Python 3.11 for every PR and every push
to `main` (`.github/workflows/tests.yml`).

## License

MIT — see [`LICENSE`](./LICENSE).
