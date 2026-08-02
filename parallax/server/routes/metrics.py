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

import dataclasses
import datetime as _dt
import re
import threading
import time
from contextlib import closing
from typing import Any, cast

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
    TRAFFIC_SOURCE_PARTITIONS as _DUAL_READ_TRAFFIC_SOURCE_PARTITIONS,
)
from parallax.router.dual_read_metrics import (
    _parse_timestamp as _dual_read_parse_timestamp,  # noqa: PLC2701
)
from parallax.router.dual_read_metrics import (
    compute_all_rates as _dual_read_compute_all_rates,
)
from parallax.router.dual_read_metrics import (
    load_records as _dual_read_load_records,
)
from parallax.router.dual_read_metrics import (
    partition_by_traffic_source as _dual_read_partition_by_traffic_source,
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
        "dual_read_log_records_total",
        "dual_read_log_dir_missing",
        "dual_read_log_newest_record_age_seconds",
        # Rendered from the DEFAULT registry below rather than built as a
        # Gauge here, but an in-house counter sanitizing to this name would
        # still emit a second metric of the same name into the same payload.
        "dual_read_requests_total",
        "arbitration_p99_latency_ms",
        "arbitration_policy_version",
    }
)

# Dual-read DoD measurement window (M3b — US-006). Mirrors the 72h DoD
# numerics from ralplan §6 line 416-426. Kept as a module constant rather
# than a magic literal so an operator can grep for it.
_DUAL_READ_WINDOW = "72h"

# Read-side traffic_source partitioning lives in ``dual_read_metrics`` — the
# module both authoritative gates on this corpus import from. /metrics and
# ``scripts/dual_read_continuity_check.py`` MUST agree on how a record is
# attributed; when they did not, they contradicted each other and could block
# a promotion between them. ``unknown`` is deliberately not one of the two
# values the write side can produce — see ``record_traffic_source`` for why.
#
# §6 rejected an ``"unknown"`` class on the grounds that no alert would be
# wired against it. That objection is answered here: the bucket is
# closed-ended (no new record can land in it), it drains to zero within
# ``_DUAL_READ_WINDOW`` of deploy, and it is counted on
# ``parallax_dual_read_log_records_total{traffic_source="unknown"}`` so an
# operator can watch it drain rather than infer it.
_TRAFFIC_SOURCE_PARTITIONS = _DUAL_READ_TRAFFIC_SOURCE_PARTITIONS


def _newest_record_age_seconds(
    records: list[dict[str, Any]],
    *,
    now: _dt.datetime,
) -> float | None:
    """Seconds since the newest record in ``records``; ``None`` if unmeasurable.

    ``load_records`` returns records sorted by timestamp ascending and the
    partition lists built from it preserve that order, so the last element is
    the newest. Timestamps are parsed with the loader's own helper so this can
    never disagree with the window filter about what a timestamp means.

    Clamped at zero: a record stamped in the future (clock skew between the
    writer and the scraping host) is "as fresh as possible", not negatively
    aged, which would read as a huge value under a ``> threshold`` alert.
    """
    for record in reversed(records):
        raw = record.get("timestamp")
        if not isinstance(raw, str):
            continue
        parsed = _dual_read_parse_timestamp(raw)
        if parsed is None:
            continue
        return max(0.0, (now - parsed).total_seconds())
    return None


@dataclasses.dataclass(frozen=True)
class _DualReadSnapshot:
    """One cache-miss worth of dual-read exposition state.

    ``rates`` holds only the partitions that actually have in-window
    records: a traffic source with no data yields **no series at all**
    rather than ``0.0``. That is Prometheus-idiomatic for "no data" and,
    unlike a zero, cannot be misread as "measured and healthy" — which is
    the failure mode that produced the Gate-5 misdiagnosis.

    ``newest_age`` follows the same absent-not-zero rule. It exists because
    ``counts`` is a lagging silence detector: a writer that stops leaves its
    backlog in the window, so the count does not reach zero until a whole
    ``_DUAL_READ_WINDOW`` has elapsed. Freshness notices immediately.

    ``counts`` is the opposite: it always carries every partition in
    ``_TRAFFIC_SOURCE_PARTITIONS``, zeros included, because a count of zero
    is itself a measurement ("we walked the corpus and found none"). Paired
    with ``dir_missing`` it separates a healthy quiet window from a
    misconfigured log directory.
    """

    rates: dict[str, dict[str, float]]
    counts: dict[str, float]
    newest_age: dict[str, float]
    dir_missing: float
    compute_error: float


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
# scrapes amortize the decision-log walk across the TTL window.
_dual_read_cache_lock = threading.Lock()
_dual_read_cache: _DualReadSnapshot | None = None
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


def _collect_dual_read_metrics() -> _DualReadSnapshot:
    """Compute every dual-read gauge from **one** ``load_records`` walk.

    MED-METRICS-CACHE — the previous implementation called the three public
    rate functions, each of which runs its own ``load_records``, so a
    cache-miss scrape walked the JSONL directory three times (~270 MB of
    reads every 30s against the live ~90 MB/72h corpus) while this docstring
    claimed one. Load once here, partition the records by ``traffic_source``,
    and hand each partition to ``compute_all_rates`` — which is exactly the
    single-pass helper ``scripts/dual_read_continuity_check.py`` already uses.

    MED-METRICS-EXC-CLASS — the whole load-and-compute is one failure domain
    now that it is one walk, so a single try/except wraps it and logs
    ``exc_class`` for grep-ability. ``compute_error`` flips to 1.0 iff it
    raised, so a stuck-at-empty exposition can be told apart from a genuinely
    empty window.
    """
    rates: dict[str, dict[str, float]] = {}
    counts: dict[str, float] = dict.fromkeys(_TRAFFIC_SOURCE_PARTITIONS, 0.0)
    newest_age: dict[str, float] = {}
    dir_missing = 0.0
    compute_error = 0.0

    try:
        loaded = _dual_read_load_records(since=parse_window(_DUAL_READ_WINDOW))
        dir_missing = 1.0 if loaded.dir_missing else 0.0

        partitions = _dual_read_partition_by_traffic_source(loaded.records)

        # One clock reading for the whole snapshot so the partitions' ages are
        # comparable to each other and to the window they were loaded with.
        now = _dt.datetime.now(_dt.UTC)
        for source, source_records in partitions.items():
            counts[source] = float(len(source_records))
            age = _newest_record_age_seconds(source_records, now=now)
            if age is not None:
                newest_age[source] = age
            computed = _dual_read_compute_all_rates(source_records)
            rates[source] = {
                "dual_read_discrepancy_rate": float(computed["discrepancy_rate"]),
                "arbitration_conflict_rate": float(computed["arbitration_conflict_rate"]),
                "dual_read_write_error_rate": float(computed["write_error_rate"]),
            }
    except Exception as exc:  # noqa: BLE001 — observability never crashes scrape
        _log.warning(
            "metric.dual_read_metrics_failed",
            extra={
                "event": "metric.dual_read_metrics_failed",
                "exc_class": type(exc).__name__,
                "exc_str": str(exc),
            },
        )
        # Emit nothing rather than a fabricated zero: a partially-built
        # partition map would understate whichever rate happened to fail.
        # ``dir_missing`` is kept at whatever the walk established before it
        # broke — a missing directory is reported by ``load_records`` without
        # raising, so discarding it here would hide the likeliest root cause.
        return _DualReadSnapshot(
            rates={},
            counts=dict.fromkeys(_TRAFFIC_SOURCE_PARTITIONS, 0.0),
            newest_age={},
            dir_missing=dir_missing,
            compute_error=1.0,
        )

    return _DualReadSnapshot(
        rates=rates,
        counts=counts,
        newest_age=newest_age,
        dir_missing=dir_missing,
        compute_error=compute_error,
    )


def _cached_dual_read_metrics() -> _DualReadSnapshot:
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
    # the file-based metric computation is swallowed and surfaced on
    # ``compute_error`` so an empty / missing dual-read log directory does
    # not 500 the scrape. The DoD CLI surfaces breaches with full detail;
    # /metrics is the live observability surface and must stay up.
    #
    # MED-METRICS-CACHE — one disk walk per cache-miss; concurrent scrapes
    # are served from the 30s cache.
    #
    # Each rate is partitioned by ``traffic_source`` so PromQL can select
    # the natural-traffic slice the Phase-2 semantic gate is defined on
    # (traffic-gap-resolution.md §3.3). A partition with no in-window
    # records emits NO series — never 0.0 — because "no natural traffic
    # has ever been observed" and "natural traffic is clean" must not look
    # identical on the wire.
    # ------------------------------------------------------------------
    dr_metrics = _cached_dual_read_metrics()
    dual_read_gauges = {
        "dual_read_discrepancy_rate": Gauge(
            "parallax_dual_read_discrepancy_rate",
            "Fraction of dual-read outcomes == 'diverge' over the 72h DoD window, "
            "by traffic_source. Denominator excludes aphelion_unreachable.",
            labelnames=["traffic_source"],
            registry=reg,
        ),
        "arbitration_conflict_rate": Gauge(
            "parallax_arbitration_conflict_rate",
            "Fraction of dual-read outcomes that produced an arbitration conflict "
            "(winning_source in {tie, fallback}) over the 72h DoD window, by "
            "traffic_source.",
            labelnames=["traffic_source"],
            registry=reg,
        ),
        "dual_read_write_error_rate": Gauge(
            "parallax_dual_read_write_error_rate",
            "Fraction of dual-read attempts that reported a write error over the 72h "
            "DoD window, by traffic_source. Denominator excludes aphelion_unreachable.",
            labelnames=["traffic_source"],
            registry=reg,
        ),
    }
    for source, source_rates in sorted(dr_metrics.rates.items()):
        for metric_key, gauge in dual_read_gauges.items():
            gauge.labels(traffic_source=source).set(source_rates[metric_key])

    # Denominator + directory-health gauges. The Gate-5 investigation read an
    # abandoned log directory, measured zero records, and concluded the
    # exposition was stale — a rate gauge alone cannot distinguish "the
    # corpus is empty" from "we are pointed at the wrong corpus". These two
    # do. Counts are emitted for every partition including zeros, mirroring
    # the shadow path's ``parallax_shadow_log_records_total``.
    log_records = Gauge(
        "parallax_dual_read_log_records_total",
        "Parsed dual-read decision-log record count in the 72h DoD window, by "
        "traffic_source. Counted before data-quality filtering and before the "
        "aphelion_unreachable denominator exclusion, so this is corpus volume "
        "rather than any single rate's denominator.",
        labelnames=["traffic_source"],
        registry=reg,
    )
    for source in _TRAFFIC_SOURCE_PARTITIONS:
        log_records.labels(traffic_source=source).set(dr_metrics.counts[source])

    # Freshness. The record count above is a LAGGING silence detector: a writer
    # that stops leaves its backlog sitting in the 72h window, so the count
    # does not reach zero until the whole window has rolled over. Age notices
    # at once, which is what lets DualReadDecisionLogSilent alert on a broken
    # writer in minutes instead of days.
    newest_age = Gauge(
        "parallax_dual_read_log_newest_record_age_seconds",
        "Seconds since the newest dual-read decision-log record in the 72h DoD "
        "window, by traffic_source. Absent for a partition with no in-window "
        "records — read alongside parallax_dual_read_log_records_total, which "
        "always reports, to tell 'nothing recent' from 'nothing at all'.",
        labelnames=["traffic_source"],
        registry=reg,
    )
    for source, age_seconds in sorted(dr_metrics.newest_age.items()):
        newest_age.labels(traffic_source=source).set(age_seconds)

    Gauge(
        "parallax_dual_read_log_dir_missing",
        "1.0 iff the resolved dual-read decision-log directory does not exist "
        "(DUAL_READ_LOG_DIR misconfigured or unmounted); else 0.0. Read with "
        "parallax_dual_read_log_records_total to tell a misconfigured reader "
        "apart from a genuinely quiet window.",
        registry=reg,
    ).set(dr_metrics.dir_missing)

    # MED-METRICS-EXC-CLASS — surface compute health on a dedicated gauge
    # so a stuck-at-0 in any of the rates above can be told apart from a
    # true zero rate.
    Gauge(
        "parallax_dual_read_metrics_compute_error",
        "1.0 iff the dual-read metric computation raised this scrape; "
        "else 0.0. Operators alert on this rather than guessing why a "
        "rate gauge sits at 0.0.",
        registry=reg,
    ).set(dr_metrics.compute_error)

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
            # Liveness signal for DualReadDecisionLogSilent. Labelled by
            # traffic_source only — the alert sums increase() over it, which
            # is unusable on a counter that carries user_id (a one-shot user's
            # series sits pinned at 1 forever and contributes a delta of 0).
            "parallax_dual_read_requests",
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
