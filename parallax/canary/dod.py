"""US-009.3 §5 — DoD verification engine for M4 canary stages.

Reads from the audit-log SQLite (``audit_log`` + ``canary_outcomes`` tables)
and computes five DoD metrics over a configurable observation window. Each
metric returns a pass/fail verdict against the spec §3.3 thresholds:

==================  ============================================  ===============
Metric              Definition                                    Threshold (PASS)
==================  ============================================  ===============
error_rate          ``audit_log.response_status >= 500`` / total  < 0.5%
discrepancy_rate    ``canary_outcomes.outcome = 'discrepancy'`` /  < 0.5%
                    total
p99_latency_ms      99th percentile of ``audit_log.latency_ms``    < 100 ms
data_loss_count     count of                                       == 0
                    ``canary_outcomes.outcome = 'data_loss'``
min_hits            count of audit rows in window                  >= 50
==================  ============================================  ===============

Acceptance criterion 3.1 mandates the 7-day rolling window; AC 3.2 mandates
per-stage independence (no cross-stage spillover); AC 3.5 mandates this
module work without the M4 router service running — it only touches SQLite.

The module is intentionally pure-Python with no Prometheus / HTTP deps so
it can run inside ``uv run parallax canary --dod`` from a CI sandbox or a
production cron without any service prerequisites (AC 3.3).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import logging
import os
from collections.abc import Iterable
from typing import Final

from parallax.canary.audit_log import AuditLog
from parallax.canary.outcomes import KNOWN_STAGES, OutcomeStore

_LOG = logging.getLogger(__name__)

# Env-var kill-switch / override for the split-implemented gate. Set to
# "1" to force the gate open when the Prometheus introspection cannot
# observe the metric family (e.g. inside an offline CI sandbox or while
# investigating a prometheus_client version drift). See ``_split_implemented``
# docstring and ``xcouncil`` 2026-05-15 Q1 verdict (A + kill-switch).
SPLIT_OVERRIDE_ENV = "PARALLAX_SPLIT_IMPLEMENTED"

__all__ = [
    "DodMetric",
    "DodVerdict",
    "MetricResult",
    "DodReport",
    "DOD_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "compute_dod",
]


class DodMetric(enum.StrEnum):
    """The five DoD metric identifiers (AC-3.1)."""

    ERROR_RATE = "error_rate"
    DISCREPANCY_RATE = "discrepancy_rate"
    P99_LATENCY_MS = "p99_latency_ms"
    DATA_LOSS_COUNT = "data_loss_count"
    MIN_HITS = "min_hits"


class DodVerdict(enum.StrEnum):
    """Outcome of a single metric evaluation."""

    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_DATA = "insufficient_data"
    # Spec ``docs/m4-prep/traffic-gap-resolution.md`` §3.4: returned by
    # B1/B2 evaluators when synthetic/natural split logic has not yet
    # landed end-to-end. Distinct from FAIL — the metric cannot be
    # evaluated, period; not a known regression.
    PENDING_IMPLEMENTATION = "pending_implementation"


# Spec §3.3 — single source of truth for DoD thresholds. Mirrors the
# T1-T5 trigger constants but expressed as DoD-side pass criteria.
DOD_THRESHOLD: Final[dict[DodMetric, float]] = {
    DodMetric.ERROR_RATE: 0.005,  # < 0.5%
    DodMetric.DISCREPANCY_RATE: 0.005,  # < 0.5%
    DodMetric.P99_LATENCY_MS: 100.0,  # < 100 ms
    DodMetric.DATA_LOSS_COUNT: 0,  # == 0
    DodMetric.MIN_HITS: 50,  # >= 50
}

# AC 3.1 — 7-day rolling DoD window.
DEFAULT_WINDOW_DAYS: Final[int] = 7


@dataclasses.dataclass(frozen=True)
class MetricResult:
    """Per-metric DoD result.

    ``observed`` carries the raw value (rate as float in [0, 1]; latency
    in ms; counts as int). ``threshold`` is the spec ceiling/floor.
    ``verdict`` reflects whether the observed value satisfies the metric's
    pass criterion AND whether sample size was sufficient (AC-3.1 hits ≥ 50).
    """

    metric: DodMetric
    observed: float
    threshold: float
    verdict: DodVerdict
    sample_size: int


@dataclasses.dataclass(frozen=True)
class DodReport:
    """Aggregate DoD report for one stage over one window.

    ``overall`` is PASS only when every metric is PASS; if any metric is
    FAIL the overall is FAIL; otherwise INSUFFICIENT_DATA.
    """

    stage: str
    window_start: str
    window_end: str
    metrics: tuple[MetricResult, ...]
    overall: DodVerdict


def _resolve_window(
    window_days: int,
    until: _dt.datetime | None,
) -> tuple[_dt.datetime, _dt.datetime]:
    """Return ``(since, until)`` as timezone-aware UTC datetimes."""
    end = until.astimezone(_dt.UTC) if until else _dt.datetime.now(_dt.UTC)
    start = end - _dt.timedelta(days=window_days)
    return start, end


def _iso(ts: _dt.datetime) -> str:
    """Format datetime to match SQLite's ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')``.

    SQLite's ``%f`` produces ``SS.SSS`` (seconds + millisecond fraction);
    Python's ``strftime('%f')`` produces 6-digit microseconds and omits
    seconds entirely when the format string lacks ``%S``. The two
    representations don't sort identically in lexical comparison, so the
    DoD window predicates (``recorded_at >= since AND <= until``) can
    misclassify boundary rows. We format explicitly with ``%S`` plus a
    3-digit millisecond suffix to mirror SQLite exactly.
    """
    utc = ts.astimezone(_dt.UTC)
    millis = utc.microsecond // 1000
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"


def _quantile(sorted_values: list[float], q: float) -> float:
    """Return the q-th quantile (0 ≤ q ≤ 1) using nearest-rank.

    Empty input → 0.0. Single value → that value. Otherwise pick the
    value at index ``ceil(q * n) - 1`` (clamped to [0, n-1]). Nearest-rank
    is well-defined and matches what canary triggers use internally
    (parallax/canary/triggers.py::T3P99LatencyTrigger).
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    import math

    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[min(rank - 1, len(sorted_values) - 1)]


def _env_override_set() -> bool:
    """Return True when ``PARALLAX_SPLIT_IMPLEMENTED`` is set to a truthy value.

    Accepted truthy values (case-insensitive): ``"1"``, ``"true"``, ``"yes"``,
    ``"on"``. Anything else (including unset, empty string, whitespace) is
    False. This is intentionally narrow — the kill-switch is for explicit
    operator opt-in, not for accidental defaulting.
    """
    raw = os.environ.get(SPLIT_OVERRIDE_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _metric_family_has_traffic_source_label() -> bool:
    """Return True when the Prometheus registry exposes the M4 split label.

    Inspects ``prometheus_client.REGISTRY`` for the ``parallax_aphelion``
    Counter family (which renders as ``parallax_aphelion_total`` samples)
    and checks its label name set for ``traffic_source``. This is the
    actual ship artifact of item 4.2 (PR #54) — the SQLite ``audit_log``
    column referenced by the previous docstring was never created.

    A prometheus_client ``Counter("parallax_aphelion", ...)`` exposes
    ``Metric.name == "parallax_aphelion"`` via ``REGISTRY.collect()``,
    while each sample's name carries the ``_total`` suffix. We accept
    either spelling because both forms appear in the ecosystem (raw
    counters vs counters built via wrapper helpers).

    Robust to multi-version prometheus_client API drift: walks
    ``Metric.samples[*].labels`` (modern), the older
    ``Metric._labelnames`` backstop, and the sample-level name suffix.
    Any unexpected exception is caught and logged so a registry quirk
    cannot crash the DoD evaluator on a critical-path gate.

    Returns False when prometheus_client is not importable (e.g. CI
    sandbox without HTTP deps); callers should rely on the env-var
    override in that environment.
    """
    try:
        from prometheus_client import REGISTRY  # type: ignore[import-untyped]
    except ImportError:
        _LOG.debug("prometheus_client not importable; split signal=False")
        return False

    target_names = {"parallax_aphelion", "parallax_aphelion_total"}
    try:
        for metric in REGISTRY.collect():
            metric_name = getattr(metric, "name", "")
            sample_names = {
                getattr(s, "name", "") for s in getattr(metric, "samples", ())
            }
            if metric_name not in target_names and not (
                target_names & sample_names
            ):
                continue
            # Modern API: walk samples; each sample has a labels dict.
            for sample in getattr(metric, "samples", ()):
                labels = getattr(sample, "labels", None) or {}
                if "traffic_source" in labels:
                    return True
            # Older API backstop: introspect declared label names.
            for attr in ("_labelnames", "label_names"):
                names = getattr(metric, attr, None)
                if names and "traffic_source" in names:
                    return True
        return False
    except Exception as exc:  # registry shape drift / version skew
        _LOG.warning(
            "prometheus introspection failed (%s: %s); split signal=False — "
            "set %s=1 to force-open if appropriate",
            exc.__class__.__name__, exc, SPLIT_OVERRIDE_ENV,
        )
        return False


def _split_implemented(_conn: object | None = None) -> bool:
    """Return True when the synthetic/natural traffic-source split has landed.

    Decision tree (in order):

    1. ``PARALLAX_SPLIT_IMPLEMENTED`` env var truthy -> True (explicit
       operator opt-in / kill-switch for environments where prometheus
       introspection is unavailable).
    2. ``prometheus_client.REGISTRY`` has ``parallax_aphelion_total`` with
       a ``traffic_source`` label dimension -> True. This is the actual
       ship artifact of item 4.2 (PR #54).
    3. Otherwise -> False; B1/B2 metrics return PENDING_IMPLEMENTATION
       per spec §3.4.

    Previously (pre-2026-05-15) this checked the ``audit_log`` SQLite
    table for a ``traffic_source`` column. That signal never matched the
    actual implementation — PR #54 attached the label to Prometheus
    metrics, not to the canary audit DB. The ``_conn`` argument is kept
    for backwards compatibility with callers in ``compute_dod`` but is
    ignored. See xcouncil 2026-05-15 Q1 verdict A.
    """
    if _env_override_set():
        _LOG.info("split gate opened via %s env override", SPLIT_OVERRIDE_ENV)
        return True
    return _metric_family_has_traffic_source_label()


def _verdict_for(metric: DodMetric, observed: float, sample_size: int) -> DodVerdict:
    """Compute pass/fail/insufficient_data for one metric.

    MIN_HITS semantics mirror the T5 gate from PR #41 — when sample size
    is below the floor, we report INSUFFICIENT_DATA rather than FAIL.
    Extending the observation window is the correct response; T5 in
    triggers.py is also a *gate*, not a *trigger*.
    """
    if metric == DodMetric.MIN_HITS:
        return (
            DodVerdict.PASS
            if observed >= DOD_THRESHOLD[metric]
            else DodVerdict.INSUFFICIENT_DATA
        )

    # All other metrics depend on sample size — fewer than 50 hits means
    # we cannot trust the rate / quantile, so report INSUFFICIENT_DATA.
    min_hits_threshold = DOD_THRESHOLD[DodMetric.MIN_HITS]
    if sample_size < min_hits_threshold:
        return DodVerdict.INSUFFICIENT_DATA

    threshold = DOD_THRESHOLD[metric]
    if metric == DodMetric.DATA_LOSS_COUNT:
        # Spec: count == 0 to PASS.
        return DodVerdict.PASS if observed <= threshold else DodVerdict.FAIL
    # error_rate, discrepancy_rate, p99_latency: observed < threshold → PASS.
    return DodVerdict.PASS if observed < threshold else DodVerdict.FAIL


def _aggregate(verdicts: Iterable[DodVerdict]) -> DodVerdict:
    """Aggregate per-metric verdicts into an overall stage verdict.

    Order of precedence: FAIL > PENDING_IMPLEMENTATION > INSUFFICIENT_DATA > PASS.
    PENDING_IMPLEMENTATION trumps INSUFFICIENT_DATA because a metric we
    cannot evaluate is a sharper signal than a metric with too few
    observations — extending the window helps the latter, not the former.
    """
    seen_fail = False
    seen_pending = False
    seen_insufficient = False
    seen_any = False
    for v in verdicts:
        seen_any = True
        if v == DodVerdict.FAIL:
            seen_fail = True
        elif v == DodVerdict.PENDING_IMPLEMENTATION:
            seen_pending = True
        elif v == DodVerdict.INSUFFICIENT_DATA:
            seen_insufficient = True
    if not seen_any:
        return DodVerdict.INSUFFICIENT_DATA
    if seen_fail:
        return DodVerdict.FAIL
    if seen_pending:
        return DodVerdict.PENDING_IMPLEMENTATION
    if seen_insufficient:
        return DodVerdict.INSUFFICIENT_DATA
    return DodVerdict.PASS


def compute_dod(
    *,
    audit_log: AuditLog,
    outcomes: OutcomeStore,
    stage: str,
    window_days: int = DEFAULT_WINDOW_DAYS,
    until: _dt.datetime | None = None,
) -> DodReport:
    """Compute DoD verdict for ``stage`` over the trailing ``window_days``.

    ``audit_log`` and ``outcomes`` MUST share the same SQLite file (per
    OutcomeStore design); the function does not enforce this — the
    consequence of mismatched paths is silently empty results, which the
    INSUFFICIENT_DATA verdict surfaces.
    """
    if stage not in KNOWN_STAGES:
        raise ValueError(
            f"Unknown canary stage: {stage!r}. Expected one of {sorted(KNOWN_STAGES)}."
        )

    since, until_dt = _resolve_window(window_days, until)
    since_iso = _iso(since)
    until_iso = _iso(until_dt)

    conn_audit = audit_log._connect()
    conn_out = outcomes._connect()

    # Step 1 — collect audit_log rows joined to canary_outcomes for this
    # stage in the window. The JOIN restricts the row set to events that
    # have a recorded outcome for the requested stage; events without an
    # outcome row are excluded (e.g. pre-canary smoke traffic).
    audit_rows = conn_audit.execute(
        """
        SELECT a.event_id, a.response_status, a.latency_ms
        FROM audit_log a
        INNER JOIN canary_outcomes c ON c.event_id = a.event_id
        WHERE c.stage = ?
          AND c.recorded_at >= ?
          AND c.recorded_at <= ?
        """,
        (stage, since_iso, until_iso),
    ).fetchall()

    sample_size = len(audit_rows)

    # Step 1.5 — check whether the synthetic/natural split has landed.
    # Per spec §3.4: until items 4.2/4.7/4.8 are end-to-end live, the DoD
    # evaluator cannot distinguish synthetic from natural traffic, so the
    # two metrics that depend on this split (B1=error_rate, B2=discrepancy_rate)
    # must be stamped PENDING_IMPLEMENTATION rather than evaluated against
    # raw data. The signal is the Prometheus metric label (PR #54 ship
    # artifact); see _split_implemented docstring for the kill-switch.
    split_ready = _split_implemented(conn_audit)

    # Step 2 — per-metric computation.
    error_count = sum(
        1 for row in audit_rows if int(row["response_status"]) >= 500
    )
    error_rate = error_count / sample_size if sample_size else 0.0

    latencies = sorted(
        float(row["latency_ms"])
        for row in audit_rows
        if row["latency_ms"] is not None
    )
    p99_latency = _quantile(latencies, 0.99)

    # discrepancy_rate + data_loss_count come from canary_outcomes alone;
    # they need a separate query because some events may have an outcome
    # row but no audit_log row (e.g. parallel write paths). Per spec, DoD
    # rates are anchored to the canary_outcomes corpus.
    outcome_counts = dict(
        conn_out.execute(
            """
            SELECT outcome, COUNT(*) as n
            FROM canary_outcomes
            WHERE stage = ? AND recorded_at >= ? AND recorded_at <= ?
            GROUP BY outcome
            """,
            (stage, since_iso, until_iso),
        ).fetchall()
    )
    total_outcomes = int(sum(outcome_counts.values()) or 0)
    discrepancy_count = int(outcome_counts.get("discrepancy", 0))
    data_loss_count = int(outcome_counts.get("data_loss", 0))
    discrepancy_rate = (
        discrepancy_count / total_outcomes if total_outcomes else 0.0
    )

    # Each metric is gated on the sample size of the corpus IT reads from.
    # Mixing the two would let an outcome-heavy / audit-light corpus return
    # confident error_rate / p99_latency verdicts on too few audit rows
    # (and vice versa for discrepancy_rate / data_loss). MIN_HITS keeps
    # the union signal so "either side has enough data" promotes the gate.
    canonical_sample = max(sample_size, total_outcomes)

    metrics = (
        MetricResult(
            metric=DodMetric.ERROR_RATE,
            observed=error_rate,
            threshold=DOD_THRESHOLD[DodMetric.ERROR_RATE],
            # B1 metric — depends on synthetic/natural split per spec §3.4.
            verdict=(
                _verdict_for(DodMetric.ERROR_RATE, error_rate, sample_size)
                if split_ready
                else DodVerdict.PENDING_IMPLEMENTATION
            ),
            sample_size=sample_size,
        ),
        MetricResult(
            metric=DodMetric.DISCREPANCY_RATE,
            observed=discrepancy_rate,
            threshold=DOD_THRESHOLD[DodMetric.DISCREPANCY_RATE],
            # B2 metric — depends on synthetic/natural split per spec §3.4.
            verdict=(
                _verdict_for(
                    DodMetric.DISCREPANCY_RATE, discrepancy_rate, total_outcomes
                )
                if split_ready
                else DodVerdict.PENDING_IMPLEMENTATION
            ),
            sample_size=total_outcomes,
        ),
        MetricResult(
            metric=DodMetric.P99_LATENCY_MS,
            observed=p99_latency,
            threshold=DOD_THRESHOLD[DodMetric.P99_LATENCY_MS],
            # Gate on the count of rows with a non-null latency_ms,
            # NOT on sample_size (joined audit rows). A corpus where
            # half the rows have NULL latency would otherwise return
            # PASS/FAIL on too few latency observations instead of
            # INSUFFICIENT_DATA.
            verdict=_verdict_for(
                DodMetric.P99_LATENCY_MS, p99_latency, len(latencies)
            ),
            sample_size=len(latencies),
        ),
        MetricResult(
            metric=DodMetric.DATA_LOSS_COUNT,
            observed=float(data_loss_count),
            threshold=DOD_THRESHOLD[DodMetric.DATA_LOSS_COUNT],
            verdict=_verdict_for(
                DodMetric.DATA_LOSS_COUNT, data_loss_count, total_outcomes
            ),
            sample_size=total_outcomes,
        ),
        MetricResult(
            metric=DodMetric.MIN_HITS,
            observed=float(canonical_sample),
            threshold=DOD_THRESHOLD[DodMetric.MIN_HITS],
            verdict=_verdict_for(DodMetric.MIN_HITS, canonical_sample, canonical_sample),
            sample_size=canonical_sample,
        ),
    )

    return DodReport(
        stage=stage,
        window_start=since_iso,
        window_end=until_iso,
        metrics=metrics,
        overall=_aggregate(m.verdict for m in metrics),
    )
