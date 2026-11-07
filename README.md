# Parallax-Kernel

Content-addressed canonical knowledge-base store. P0 core: 6-object schema
(`sources`, `memories`, `claims`, `decisions`, `events`, `index_state`) with
`content_hash` dedup on memories and claims, race-safe UPSERT ingest, an
append-only event log, an explicit state machine for lifecycle transitions,
and a deterministic search-index rebuild path.

> The GitHub repo is named `parallax-kernel`. The Python package is imported
> as `parallax` (`from parallax import ...`). It is **not published on PyPI**:
> the `parallax-kernel` name on PyPI belongs to an unrelated project, so
> install from a clone (Quick Start below), not with `pip install parallax-kernel`.

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
| `memory_by_content_hash` | function | Lookup one memory by `(content_hash, user_id)` (`Optional[dict]`). `user_id` is keyword-only and required. |
| `claim_by_content_hash` | function | Lookup one claim by `(content_hash, user_id)` (`Optional[dict]`). `user_id` is keyword-only and required. |
| `record_event` | function | Append a raw event row to the immutable event log. |
| `record_memory_reaffirmed` | function | Bump a memory's `reaffirm_count` and emit a `memory_reaffirmed` event. |
| `record_claim_state_changed` | function | Append a `claim.state_changed` event. **Does not mutate `claims.state`** — pair with the matching `UPDATE` or use `transition_claim_state`. |
| `transition_claim_state` | function | Atomically apply a claim transition: `SELECT current → is_allowed_transition → UPDATE claims → record_event` in a single transaction. |
| `is_allowed_transition` | function | Check `(from_state, to_state)` against the per-object transition table. |
| `MEMORY_TRANSITIONS` / `CLAIM_TRANSITIONS` / `SOURCE_TRANSITIONS` / `DECISION_TRANSITIONS` | dict[str, frozenset[str]] | Allowed lifecycle transitions per object kind: `state -> {allowed next states}`. |
| `rebuild_index` | function | Deterministic rebuild of `index_state` for a named search index. **Not DB-idempotent**: each call appends a new `index_state` row at `version = MAX(version) + 1`; derived content (`doc_count`, `state`, `source_watermark`) is stable across repeat calls on the same DB snapshot. |
| `target_ref_exists` | function | Existence check used to reject orphan events/decisions at the boundary. |
| `VALID_TARGET_KINDS` / `DECISION_TARGET_KINDS` / `TargetKind` | constants | Target-kind allow-lists for events and decisions. |
| `parallax_info` / `ParallaxInfo` | function / dataclass | Runtime introspection (version, modules, schema). |
| `health` | function | Operational snapshot (db path, table counts, journal mode, last error). |
| `Source` / `Memory` / `Claim` / `Event` | dataclass | Frozen record types used by the storage layer. |
| `__version__` | str | Package version (currently `0.6.0`). |

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
| `parallax.index` | `rebuild_index()` — deterministic rebuild of `index_state`. Each call appends a new history row; derived content (`doc_count`, `state`) is stable across repeats on the same DB snapshot. |
| `parallax.telemetry` | Stdlib-only structured events + in-memory metrics + `health()`. |
| `parallax.introspection` | `parallax_info()` / `ParallaxInfo` runtime metadata. |
| `parallax.obs.log` / `parallax.obs.metrics` | Lower-level logging + metrics primitives backing `telemetry`. |
| `bootstrap.py` | One-shot initializer + CLI. |

## State Machine

Lifecycle transitions are explicit and centralised in `parallax.transitions`.
Each object kind ships a `dict[str, frozenset[str]]` mapping a state to its
allowed next states. The audit log and the row mutation are split across two
APIs so callers can pick the level of guarantee they need:

- `record_claim_state_changed()` — appends an audit event only. Does not
  validate the transition and does not touch `claims.state`. Use this when
  the row mutation is already happening elsewhere in the same transaction
  (the pattern `parallax.extract.review` follows for its review-queue
  flow, which has stricter `from_state='pending'` semantics).
- `transition_claim_state()` — the canonical atomic API. Reads the current
  state, calls `is_allowed_transition()`, runs `UPDATE claims SET state=?,
  updated_at=? WHERE claim_id=? AND state=?` (with a TOCTOU rowcount
  guard), and records the event — all in one transaction. The event log
  and `claims.state` cannot drift.

```python
from parallax import (
    CLAIM_TRANSITIONS,
    is_allowed_transition,
    transition_claim_state,
)

# CLAIM_TRANSITIONS is a dict mapping state -> frozenset of allowed next states.
assert "confirmed" in CLAIM_TRANSITIONS["pending"]
assert is_allowed_transition("claim", "pending", "confirmed")

# transition_claim_state mutates the row AND emits the audit event.
# event_id = transition_claim_state(conn, claim_id=cid, to_state="confirmed")
```

See [`docs/contract.md`](./docs/contract.md) for which public APIs mutate
state vs. which only write to the event log.

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

`parallax.telemetry` is the in-process structured-event + metrics module
and remains stdlib-only (under 200 lines, zero third-party imports). It
exposes `snapshot()` so callers can adapt the in-memory state to any
scrape format without the kernel taking on an exporter dependency.

Since v0.6.0 the kernel also ships a thin Prometheus-format adapter
(`parallax.server.routes.metrics`) wired to `prometheus_client`. The
adapter is a separate layer — it imports `prometheus_client` at the
HTTP boundary, not inside `parallax.telemetry` — so projects that embed
the kernel without the FastAPI server still pay no dependency cost
beyond the four core libs. `prometheus_client` is in the core
`pyproject.toml` dependencies because the server routes are part of the
default install; it is not pulled into the telemetry module itself.

The Prometheus endpoint is auth-gated when a token is configured (see
the **Server / Production safety** section below). In open mode it
remains unauthenticated to match the `/healthz` posture.

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

## Phase 3: Shadow Write

`parallax.extract` is an optional subpackage that extracts claims from
free-form text via a pluggable `Provider` and dual-writes into the
canonical store so divergence vs. the existing vault writer can be
measured before any cut-over. Install with the extra:

```bash
pip install -e '.[extract]'   # from your clone
```

Core install never imports `httpx` / `anthropic` — only the
`[extract]` extra does. Wire the shadow behind an env flag so the
primary vault writer stays unchanged:

```python
import os
from parallax.extract.shadow import shadow_write

if os.environ.get("PARALLAX_DUAL_WRITE") == "1":
    claim_ids = shadow_write(
        conn,
        text,
        provider=provider,
        user_id=user_id,
        source_id=source_id,
    )
    log.info("parallax_shadow_write", extra={"count": len(claim_ids)})
write_to_vault(...)  # original primary path — unchanged
```

See [`docs/shadow-write.md`](./docs/shadow-write.md) for the log record
format and the nightly `pytest -m llm_integration` procedure that hits
OpenRouter for real.

## Server / Production safety

Five environment variables gate how the FastAPI server behaves when
exposed beyond localhost:

| Env var | Default | Effect |
|---|---|---|
| `PARALLAX_TOKEN` | unset | Single-token bearer auth on every non-`/healthz` route. Unset = open mode. |
| `PARALLAX_MULTI_USER` | `0` | When `1`, swap to per-user token lookup against the `api_tokens` table (see m0009). |
| `PARALLAX_BIND_HOST` | `""` (loopback) | Read by `assert_safe_to_start()` at app construction. The app **refuses to start** if this is a non-loopback address while no auth mode is configured. **Important**: this env is the *self-attested* bind address — uvicorn binds based on its own `--host` arg. The `parallax serve` CLI pins the env to its `--host` automatically; direct `uvicorn parallax.server.app:app --host X` invocation must set this env separately to match (see `pm2/ecosystem.config.js` for the pattern). |
| `PARALLAX_ALLOW_OPEN_PUBLIC` | unset | When `1`, bypass the bind-host safety check. Logs a loud audit warning at startup. NOT recommended for production. |
| `PARALLAX_METRICS_PUBLIC` | unset | When `1`, opt `/metrics` out of auth even when a token is configured. Logs a loud audit warning at startup. Use only when a private network or Cloudflare Access policy already protects the route. |

### Two ways to launch the server

| Launcher | Bind-host pin |
|---|---|
| `parallax serve --host 0.0.0.0 --port 8080` | Automatic — the CLI sets `PARALLAX_BIND_HOST` to match `--host` before importing the app. |
| `uvicorn parallax.server.app:app --host 0.0.0.0 --port 8080` (direct) | Manual — you must set `PARALLAX_BIND_HOST=0.0.0.0` in the launching environment, or `assert_safe_to_start()` reads the unset default and skips the check. |

### Production checklist

Before wiring the server to a public hostname / Cloudflare Tunnel:

1. Set `PARALLAX_TOKEN` (single-tenant) or `PARALLAX_MULTI_USER=1` (multi-tenant).
2. Decide your launcher (CLI vs direct uvicorn) and ensure `PARALLAX_BIND_HOST` reflects the actual bind address (CLI pins it for you).
3. Boot once with the production env and verify the safety check passes cleanly. Look for the `auth.startup.allow_open_public_override` warning in logs — if it appears, your override is active and the safety net is OFF.
4. Decide `/metrics` posture:
   - **Default (recommended)**: leave `PARALLAX_METRICS_PUBLIC` unset so
     Prometheus must scrape with the bearer token.
   - **Sidecar / private subnet**: set `PARALLAX_METRICS_PUBLIC=1` only if
     the network already bounds who can reach the route. Look for the
     `auth.metrics.public_override_active` warning to confirm the route is open.
5. Never expose `/docs` / `/redoc` publicly — they are off by default;
   `PARALLAX_DOCS_ENABLED=1` is for local dev only.
6. Verify backups: `parallax backup <archive.tar.gz>` round-trips
   through `parallax restore` against an empty DB before you cut over.

## Testing

```bash
pytest                              # full suite, with coverage gate
python -m pytest tests/acceptance/  # SQL acceptance harness only
```

GitHub Actions runs the suite on Python 3.11 for every PR and every push
to `main` or `main-next` (`.github/workflows/tests.yml`).

## License

MIT — see [`LICENSE`](./LICENSE).
