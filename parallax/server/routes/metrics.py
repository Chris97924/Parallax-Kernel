"""GET /metrics — Prometheus text-format endpoint.

Wraps :mod:`parallax.obs.metrics` (in-house thread-safe Counter registry) and
exposes WS-3 shadow observability gauges:

* ``parallax_shadow_discrepancy_rate`` — current rolling-1h discrepancy rate
* ``parallax_shadow_checksum_consistency`` — current rolling-1h consistency
* ``parallax_shadow_log_records_total`` — record count in the rolling window

Auth posture
------------
* **Open mode** (no ``PARALLAX_TOKEN``, no ``PARALLAX_MULTI_USER``):
  unauthenticated. Same posture as ``/healthz``.
* **Auth configured**: requires the same bearer token as the rest of the
  API. Operators who deliberately want an open scrape endpoint (e.g.
  behind a private network or Cloudflare Access policy) can opt in by
  setting ``PARALLAX_METRICS_PUBLIC=1``.

The values themselves carry no PII or query contents — only aggregate
floats — but exposing them publicly still leaks ingest cadence, retrieve
volume, shadow discrepancy rate, and service-existence signals that an
attacker can use for reconnaissance. Defaulting to fail-closed when auth
is available keeps that signal off the public internet without
operators having to remember to gate it.

Disk reads are cached for ``_CACHE_TTL_SECONDS`` so concurrent scrapes don't
re-walk the JSONL files. Tests can reset the cache via
``_reset_cache_for_tests()``.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import closing
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Gauge,
    generate_latest,
)

from parallax.obs.log import get_logger as _get_logger
from parallax.obs.metrics import registry as _inhouse_registry
from parallax.router.dual_read_metrics import (
    arbitration_conflict_rate as _dual_read_arbitration_conflict_rate,
)
from parallax.router.dual_read_metrics import (
    discrepancy_rate as _dual_read_discrepancy_rate,
)
from parallax.router.dual_read_metrics import (
    write_error_rate as _dual_read_write_error_rate,
)
from parallax.router.live_arbitration import POLICY_VERSION_DEFAULT
from parallax.server.auth import (
    bearer_security,
    metrics_auth_required,
    multi_user_mode,
    require_auth,
)
from parallax.server.deps import DBFactory, default_db_factory
from parallax.shadow.discrepancy import (
    is_record_consistent,
    load_records,
    parse_window,
)

__all__ = ["router"]

_log = _get_logger("parallax.server.routes.metrics")

# Names reserved for explicit shadow gauges emitted by ``_build_payload``.
# An in-house counter that sanitizes to one of these would crash the scrape
# with prometheus_client's ``Duplicated timeseries`` check. The in-house
# loop skips + warns instead.
_RESERVED_GAUGE_SUFFIXES = frozenset(
    {
        "shadow_discrepancy_rate",
        "shadow_checksum_consistency",
        "shadow_log_records_total",
        "dual_read_discrepancy_rate",
        "arbitration_conflict_rate",
        "dual_read_write_error_rate",
        "dual_read_metrics_compute_error",
        "arbitration_p99_latency_ms",
        "arbitration_policy_version",
    }
)

# Dual-read DoD measurement window (M3b — US-006). Mirrors the 72h DoD
# numerics from ralplan §6 line 416-426. Kept as a module constant rather
# than a magic literal so an operator can grep for it.
_DUAL_READ_WINDOW = "72h"

router = APIRouter(tags=["meta"])

_BEARER_DEP = Depends(bearer_security)

# Cache scrape results so a Prometheus 15s scrape interval doesn't flog disk.
# 30s TTL is a deliberate over-shoot so two consecutive scrapes hit the cache.
#
# Trade-off: alerting latency is bounded by `30s + scrape_interval`. With a
# 15s Prometheus scrape interval, post-incident discrepancy spikes can show
# stale healthy values for up to 30s. Acceptable for the 72h DoD window
# (30s is noise on a 72h timeline). Tighten this if sub-minute alerts ever
# matter.
_CACHE_TTL_SECONDS = 30.0
_WINDOW = "1h"

_cache_lock = threading.Lock()
_cache: dict[str, float] | None = None
_cache_at: float = 0.0

# MED-METRICS-CACHE: separate cache for the dual-read gauges so /metrics
# scrapes amortize the 3 file walks (discrepancy_rate +
# arbitration_conflict_rate + write_error_rate) into one.
_dual_read_cache_lock = threading.Lock()
_dual_read_cache: dict[str, float] | None = None
_dual_read_cache_at: float = 0.0


def _reset_cache_for_tests() -> None:
    """Drop the in-process cache. Test-only — never call from production code."""
    global _cache, _cache_at, _dual_read_cache, _dual_read_cache_at
    with _cache_lock:
        _cache = None
        _cache_at = 0.0
    with _dual_read_cache_lock:
        _dual_read_cache = None
        _dual_read_cache_at = 0.0


def _collect_shadow_metrics() -> dict[str, float]:
    """Compute all three shadow gauge values with a single ``load_records`` walk.

    Calling ``discrepancy_rate`` + ``checksum_consistency`` separately would
    re-walk the JSONL directory twice; collapsing here trims a cache-miss
    scrape from 3 reads to 1. Semantics must mirror the public functions
    exactly — drift is pinned by ``test_metrics_collapsed_walk_matches_*``
    in tests/server/test_metrics_endpoint.py.
    """
    delta = parse_window(_WINDOW)
    loaded = load_records(since=delta)
    parsed = len(loaded.records)
    total = parsed + loaded.malformed

    diverge = sum(1 for r in loaded.records if r.get("arbitration_outcome") == "diverge")
    discrepancy = diverge / parsed if parsed else 0.0

    if total:
        consistent = sum(
            1
            for record, raw in zip(loaded.records, loaded.raw_lines, strict=True)
            if is_record_consistent(record, raw)
        )
        consistency = consistent / total
    else:
        consistency = 1.0

    return {
        "discrepancy_rate": discrepancy,
        "checksum_consistency": consistency,
        "log_records_total": float(parsed),
    }


def _collect_dual_read_metrics() -> dict[str, float]:
    """Compute the 3 dual-read gauges from a single load_records walk.

    MED-METRICS-CACHE — three separate calls to the public rate
    functions would re-walk the JSONL directory three times per scrape.
    Collapse to one walk + per-rate compute, then cache the result for
    ``_CACHE_TTL_SECONDS`` so a 15s Prometheus scrape interval hits disk
    once every other scrape rather than three times every scrape.

    MED-METRICS-EXC-CLASS — wraps each compute in a try/except that
    logs ``exc_class`` so operators can grep for the failing rate
    without parsing free-text strings. ``compute_error`` flips to 1.0
    iff any compute raised so a single gauge surfaces the health of
    this scrape.
    """
    out: dict[str, float] = {
        "dual_read_discrepancy_rate": 0.0,
        "arbitration_conflict_rate": 0.0,
        "dual_read_write_error_rate": 0.0,
        "compute_error": 0.0,
    }
    try:
        out["dual_read_discrepancy_rate"] = _dual_read_discrepancy_rate(_DUAL_READ_WINDOW)
    except Exception as exc:  # noqa: BLE001 — observability never crashes scrape
        _log.warning(
            "metric.dual_read_discrepancy_rate_failed",
            extra={
                "event": "metric.dual_read_discrepancy_rate_failed",
                "exc_class": type(exc).__name__,
                "exc_str": str(exc),
            },
        )
        out["compute_error"] = 1.0
    try:
        out["arbitration_conflict_rate"] = _dual_read_arbitration_conflict_rate(_DUAL_READ_WINDOW)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "metric.arbitration_conflict_rate_failed",
            extra={
                "event": "metric.arbitration_conflict_rate_failed",
                "exc_class": type(exc).__name__,
                "exc_str": str(exc),
            },
        )
        out["compute_error"] = 1.0
    try:
        out["dual_read_write_error_rate"] = _dual_read_write_error_rate(_DUAL_READ_WINDOW)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "metric.dual_read_write_error_rate_failed",
            extra={
                "event": "metric.dual_read_write_error_rate_failed",
                "exc_class": type(exc).__name__,
                "exc_str": str(exc),
            },
        )
        out["compute_error"] = 1.0
    return out


def _cached_dual_read_metrics() -> dict[str, float]:
    """Read-then-fill cache for the dual-read gauges (mirror of shadow path)."""
    global _dual_read_cache, _dual_read_cache_at
    with _dual_read_cache_lock:
        now = time.monotonic()
        if _dual_read_cache is not None and (now - _dual_read_cache_at) < _CACHE_TTL_SECONDS:
            return _dual_read_cache
        fresh = _collect_dual_read_metrics()
        _dual_read_cache = fresh
        _dual_read_cache_at = time.monotonic()
        return fresh


def _cached_shadow_metrics() -> dict[str, float]:
    """Read-then-fill cache under a single lock to prevent N concurrent scrapes
    from each running ``_collect_shadow_metrics()`` (which walks the JSONL dir).

    Holding the lock across the disk read trades scrape latency for
    correctness: a burst of N scrapes computes the metric exactly once, then
    each waiter copies the cached dict. Disk I/O time dominates lock-hold time
    only under abnormal scrape concurrency (>>1/s) — Prometheus default is
    well under that.
    """
    global _cache, _cache_at
    with _cache_lock:
        now = time.monotonic()
        if _cache is not None and (now - _cache_at) < _CACHE_TTL_SECONDS:
            return _cache
        fresh = _collect_shadow_metrics()
        _cache = fresh
        _cache_at = time.monotonic()
        return fresh


_METRIC_NAME_INVALID_RE = re.compile(r"[^a-zA-Z0-9_]")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")


def _sanitize_metric_name(name: str) -> str:
    """Return a valid Prometheus metric name derived from an in-house counter key.

    Strips any embedded label selector (``{...}``), removes a leading
    ``parallax_`` prefix so the caller's ``f"parallax_{...}"`` doesn't
    double-prefix, then replaces any remaining invalid characters with ``_``.

    Note: ``:`` is valid per the Prometheus exposition spec but is reserved
    for recording rules. Parallax in-house counter keys never use it, so the
    sanitizer collapses it to ``_`` along with other non-identifier chars.

    Pathological inputs (``"parallax_"``, ``"{kind='bug'}"``, ``"___"``)
    collapse to ``""``. ``_build_payload`` MUST skip empty results before
    constructing a Gauge — the empty case would otherwise emit a metric
    named ``parallax_`` (spec-valid trailing underscore) and a second such
    key would crash the scrape with prometheus_client's duplicate-name
    check.
    """
    brace = name.find("{")
    if brace != -1:
        name = name[:brace]
    if name.startswith("parallax_"):
        name = name[len("parallax_") :]
    name = _METRIC_NAME_INVALID_RE.sub("_", name)
    name = _MULTI_UNDERSCORE_RE.sub("_", name)
    return name.strip("_")


def _format_prometheus_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    rendered = []
    for key, value in sorted(labels.items()):
        escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        rendered.append(f'{key}="{escaped}"')
    return "{" + ",".join(rendered) + "}"


def _render_default_registry_counter(name: str) -> str:
    """Render a live prometheus_client Counter from the DEFAULT registry as text.

    ``name`` is the Counter's *base* name (no ``_total`` suffix), e.g.
    ``"parallax_aphelion"`` or ``"parallax_canary_shadow_attempts"``. The
    matching collector exposes a ``<name>_total`` sample per label-set; this
    renders the HELP/TYPE header plus every sample line in Prometheus text
    format, preserving all labels via ``_format_prometheus_labels``.

    Counters incremented into ``prometheus_client.REGISTRY`` (the default
    registry) are otherwise invisible to ``/metrics``: ``_build_payload``
    serializes a *fresh* ``CollectorRegistry``, so default-registry series
    must be plucked out explicitly. Returns ``""`` when the metric is not
    registered (e.g. no observation yet), matching the prior aphelion
    special-case behavior byte-for-byte.
    """
    total_name = f"{name}_total"
    for metric in REGISTRY.collect():
        if metric.name != name:
            continue
        lines = [
            f"# HELP {total_name} {metric.documentation}",
            f"# TYPE {total_name} counter",
        ]
        for sample in metric.samples:
            if sample.name != total_name:
                continue
            labels = _format_prometheus_labels(sample.labels)
            lines.append(f"{total_name}{labels} {sample.value}")
        return "\n".join(lines) + "\n"
    return ""


def _build_payload() -> str:
    """Render Prometheus text format combining in-house counters + shadow gauges."""
    reg = CollectorRegistry()

    # In-house counters: emit each as a Prometheus Gauge mirroring its current value.
    # Counter (monotonic) would be more idiomatic, but the existing in-house Counter
    # supports reset() (used by tests), so a Gauge mirror is the safer adapter.
    #
    # ``list(...)`` snapshots the registry so a concurrent ``get_counter()`` call
    # from an ingest thread can't trigger ``RuntimeError: dictionary changed size
    # during iteration`` mid-scrape.
    for name, counter in list(_inhouse_registry.items()):
        sanitized = _sanitize_metric_name(name)
        if not sanitized:
            # Pathological key collapses to empty after sanitization (e.g.,
            # ``"parallax_"`` or ``"{kind='bug'}"``). Skip + warn so an operator
            # can trace the orphan via logs — silent drop would hide the
            # registration bug from /metrics dashboards.
            _log.warning(
                "metric.skip_empty_after_sanitize",
                extra={"original_key": name},
            )
            continue
        if sanitized in _RESERVED_GAUGE_SUFFIXES:
            # An in-house counter whose sanitized form collides with a reserved
            # shadow-gauge name would 500 the scrape on the second registration
            # of ``f"parallax_{sanitized}"``. Skip + warn — fixing the root cause
            # is the caller's job (rename the counter).
            _log.warning(
                "metric.skip_reserved_collision",
                extra={"original_key": name, "sanitized": sanitized},
            )
            continue
        gauge = Gauge(
            f"parallax_{sanitized}",
            f"Mirror of parallax.obs.metrics.{name}",
            registry=reg,
        )
        gauge.set(counter.value)

    metrics = _cached_shadow_metrics()
    discrepancy = Gauge(
        "parallax_shadow_discrepancy_rate",
        "Fraction of arbitration_outcome=diverge records in the rolling 1h window.",
        registry=reg,
    )
    discrepancy.set(metrics["discrepancy_rate"])

    consistency = Gauge(
        "parallax_shadow_checksum_consistency",
        "Fraction of consistent (parseable, 9-field, schema-locked) records in the "
        "rolling 1h window.",
        registry=reg,
    )
    consistency.set(metrics["checksum_consistency"])

    log_count = Gauge(
        "parallax_shadow_log_records_total",
        "Parsed shadow decision-log record count in the rolling 1h window.",
        registry=reg,
    )
    log_count.set(metrics["log_records_total"])

    # ------------------------------------------------------------------
    # M3b dual-read gauges (US-006-M3-T2.3). Best-effort: any failure in
    # the file-based metric computation is swallowed and surfaced as 0.0
    # so an empty / missing dual-read log directory does not 500 the
    # scrape. The DoD CLI surfaces breaches with full detail; /metrics is
    # the live observability surface and must stay up.
    #
    # MED-METRICS-CACHE — collapse 3 file walks into one disk read per
    # cache-miss; serve cached values from concurrent scrapes for 30s.
    # ------------------------------------------------------------------
    dr_metrics = _cached_dual_read_metrics()
    Gauge(
        "parallax_dual_read_discrepancy_rate",
        "Fraction of dual-read outcomes == 'diverge' over the 72h DoD window. "
        "Denominator excludes aphelion_unreachable.",
        registry=reg,
    ).set(dr_metrics["dual_read_discrepancy_rate"])
    Gauge(
        "parallax_arbitration_conflict_rate",
        "Fraction of dual-read outcomes that produced an arbitration conflict "
        "(winning_source in {tie, fallback}) over the 72h DoD window.",
        registry=reg,
    ).set(dr_metrics["arbitration_conflict_rate"])
    Gauge(
        "parallax_dual_read_write_error_rate",
        "Fraction of dual-read attempts that reported a write error over the 72h "
        "DoD window. Denominator excludes aphelion_unreachable.",
        registry=reg,
    ).set(dr_metrics["dual_read_write_error_rate"])

    # MED-METRICS-EXC-CLASS — surface compute health on a dedicated gauge
    # so a stuck-at-0 in any of the three rates above can be told apart
    # from a true zero rate.
    Gauge(
        "parallax_dual_read_metrics_compute_error",
        "1.0 iff any dual-read metric compute call raised this scrape; "
        "else 0.0. Operators alert on this rather than guessing why a "
        "rate gauge sits at 0.0.",
        registry=reg,
    ).set(dr_metrics["compute_error"])

    # Architect-flagged observability gap: real arbitration p99 latency wiring
    # is deferred to a future T1.4 follow-up. Expose 0.0 as a placeholder so
    # downstream Grafana panels do not 404 on the metric.
    Gauge(
        "parallax_arbitration_p99_latency_ms",
        "p99 latency of arbitrate() over the rolling 72h window — placeholder "
        "(real latency wired by T1.4 follow-up).",
        registry=reg,
    ).set(0.0)

    # Info-metric (Q1' wiring): expose the live arbitration policy version as
    # a label on a constant 1.0-valued gauge so Prometheus joins on this
    # series cleanly. Mirror the prometheus_client info-metric idiom without
    # pulling in the ``Info`` collector (it would name the series differently
    # and break the test contract).
    policy_gauge = Gauge(
        "parallax_arbitration_policy_version",
        "Live cross-store arbitration policy version (info-metric).",
        labelnames=["policy_version"],
        registry=reg,
    )
    policy_gauge.labels(policy_version=POLICY_VERSION_DEFAULT).set(1.0)

    # Default-registry Counters that bypass the fresh CollectorRegistry above.
    # ``parallax_aphelion`` is the M4 split counter; the two
    # ``parallax_canary_shadow_*`` counters are incremented by
    # ``parallax.canary_shadow.observe`` into the DEFAULT registry and would
    # otherwise never appear in a scrape (the M4 canary observability blind
    # spot). Render each explicitly via the same helper.
    default_registry_counters = "".join(
        _render_default_registry_counter(name)
        for name in (
            "parallax_aphelion",
            "parallax_canary_shadow_attempts",
            "parallax_canary_shadow_outcomes",
        )
    )
    return generate_latest(reg).decode("utf-8") + default_registry_counters


@router.get("/metrics", response_class=PlainTextResponse)
def get_metrics(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = _BEARER_DEP,
) -> PlainTextResponse:
    """Prometheus scrape endpoint.

    Auth is enforced when :func:`parallax.server.auth.metrics_auth_required`
    returns True — i.e. some auth mode is configured AND the operator has
    not opted into ``PARALLAX_METRICS_PUBLIC=1``. In open mode the route
    behaves like ``/healthz`` and skips the bearer check entirely.

    The SQLite connection is opened lazily and only in multi-user mode,
    where token lookup actually needs the DB. Single-token mode and open
    mode never touch the database, so a DB-open failure cannot 500 the
    scrape and remove observability.
    """
    if metrics_auth_required():
        if multi_user_mode():
            # Honor the test-override contract from ``parallax.server.deps.get_conn``
            # so ``create_app(db_factory=...)`` fixtures still scope multi-user
            # token lookups correctly.
            factory = cast(
                DBFactory,
                getattr(request.app.state, "db_factory", default_db_factory),
            )
            with closing(factory()) as conn:
                require_auth(request, creds, conn)
        else:
            # Single-token path: require_auth never reads conn.
            require_auth(request, creds, None)
    return PlainTextResponse(_build_payload(), media_type=CONTENT_TYPE_LATEST)
