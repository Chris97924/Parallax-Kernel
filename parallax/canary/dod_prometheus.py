"""Per-stage Prometheus shadow-metric DoD summary (Option A1, 2026-06-07).

``parallax canary --dod`` reads the live canary **shadow observer**'s
Prometheus counters instead of the SQLite ``canary_outcomes`` table. The
shadow observer (:mod:`parallax.canary_shadow`) emits
``parallax_canary_shadow_attempts_total`` and
``parallax_canary_shadow_outcomes_total`` (labelled by ``stage`` = ``s1``
..``s4``); it never calls ``OutcomeStore.record()``, so the SQLite DoD path
(:func:`parallax.canary.dod.compute_dod`) returns INSUFFICIENT_DATA forever
in a real deployment. This module replaces that path for the CLI.

**Scope — three metrics only.** The shadow observer only emits the data
needed for ``discrepancy_rate`` and ``aphelion_unreachable_rate`` (plus the
attempt count for ``min_hits``). The three metrics the observer does NOT
emit per-stage — ``error_rate``, ``p99_latency_ms``, ``data_loss_count`` —
are intentionally NOT computed here. They are gated by the T1-T5 Prometheus
**alerts** (auto-rollback), which are the authoritative promotion gate. This
per-stage summary is an advisory cross-check, not the gate.

Stdlib ``urllib`` only — no new dependencies (matches the alertmanager-relay
topology in ``scripts/gmail-smtp-relay.py``). A query that cannot be answered
(Prometheus unreachable, HTTP error, empty result, malformed JSON) degrades
to INSUFFICIENT_DATA — a DoD gate must never crash the caller.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Final

from parallax.canary.dod import (
    DEFAULT_WINDOW_DAYS,
    DOD_THRESHOLD,
    DodMetric,
    DodReport,
    DodVerdict,
    MetricResult,
    _aggregate,
)
from parallax.canary.outcomes import KNOWN_STAGES

_LOG = logging.getLogger(__name__)

__all__ = [
    "DOD_STAGE_TO_SHADOW",
    "PROM_URL_ENV",
    "DEFAULT_PROM_URL",
    "compute_shadow_dod",
]

# Map the DoD CLI stage names (m4_Npct) onto the shadow observer's stage
# labels (sN). The observer labels counters with s1..s4 (see
# parallax/canary_shadow.py::resolve_stage); the CLI / runbook speak in
# m4_1pct..m4_100pct (see parallax/canary/outcomes.py::KNOWN_STAGES).
DOD_STAGE_TO_SHADOW: Final[dict[str, str]] = {
    "m4_1pct": "s1",
    "m4_10pct": "s2",
    "m4_50pct": "s3",
    "m4_100pct": "s4",
}

# Operator-set Prometheus base URL. Default matches a local Prometheus.
PROM_URL_ENV: Final[str] = "PARALLAX_PROMETHEUS_URL"
DEFAULT_PROM_URL: Final[str] = "http://localhost:9090"

# Minimum attempts before a rate metric is trustworthy — mirrors the
# SQLite path's MIN_HITS floor (DOD_THRESHOLD[MIN_HITS] == 50) and the T5
# gate semantics.
_MIN_HITS_FLOOR: Final[int] = int(DOD_THRESHOLD[DodMetric.MIN_HITS])


def _prom_instant_query(
    prom_url: str,
    query: str,
    *,
    timeout: float = 10.0,
) -> float | None:
    """Run a Prometheus instant query; return the first scalar or ``None``.

    GETs ``{prom_url}/api/v1/query`` with a urlencoded ``query`` parameter,
    parses the JSON envelope, and returns
    ``data.result[0].value[1]`` coerced to float.

    Returns ``None`` (never raises out) on any of: HTTP error, connection
    error, non-``success`` status, empty result set, or malformed JSON /
    value shape. A warning is logged so a Prometheus outage is diagnosable
    from the log, but the DoD gate degrades to INSUFFICIENT_DATA rather than
    crashing the caller.
    """
    url = f"{prom_url.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    # Fail-closed gate: any failure issuing/reading the query — URLError,
    # OSError, or http.client.InvalidURL from a malformed --prometheus-url
    # (which is NOT a URLError/OSError) — must degrade to INSUFFICIENT_DATA.
    # --dod must never crash on a Prometheus outage or a bad config value.
    try:
        req = urllib.request.Request(url, method="GET")  # noqa: S310 — operator-supplied prom_url
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001 - fail-closed gate (see comment above)
        _LOG.warning(
            "prometheus query failed (%s: %s); query=%r → INSUFFICIENT_DATA",
            exc.__class__.__name__,
            exc,
            query,
        )
        return None

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        _LOG.warning(
            "prometheus returned non-JSON (%s); query=%r → INSUFFICIENT_DATA",
            exc,
            query,
        )
        return None

    if not isinstance(payload, dict) or payload.get("status") != "success":
        _LOG.warning(
            "prometheus query status not success (%r); query=%r → INSUFFICIENT_DATA",
            payload.get("status") if isinstance(payload, dict) else type(payload),
            query,
        )
        return None

    try:
        result = payload["data"]["result"]
        if not result:
            # Empty result is normal for a stage with zero traffic; this is
            # not an error, but it means "no value" → None (caller maps to
            # a 0-sample INSUFFICIENT_DATA verdict).
            return None
        # value is [<unix_ts>, "<scalar-as-string>"].
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        _LOG.warning(
            "prometheus result shape unexpected (%s); query=%r → INSUFFICIENT_DATA",
            exc,
            query,
        )
        return None


def _rate_metric(
    metric: DodMetric,
    *,
    numerator: float | None,
    attempts: float | None,
    sample_size: int,
) -> MetricResult:
    """Build a rate MetricResult (discrepancy / aphelion_unreachable).

    Verdict is INSUFFICIENT_DATA when the attempts query or the numerator
    query returned ``None`` (Prometheus could not answer), or when the
    sample size is below the MIN_HITS floor. Otherwise PASS if the rate is
    strictly below the threshold, else FAIL.
    """
    threshold = DOD_THRESHOLD[metric]
    if attempts is None or numerator is None or sample_size < _MIN_HITS_FLOOR:
        rate = (
            numerator / attempts if (attempts not in (None, 0) and numerator is not None) else 0.0
        )
        return MetricResult(
            metric=metric,
            observed=rate,
            threshold=threshold,
            verdict=DodVerdict.INSUFFICIENT_DATA,
            sample_size=sample_size,
        )
    rate = numerator / attempts if attempts else 0.0
    verdict = DodVerdict.PASS if rate < threshold else DodVerdict.FAIL
    return MetricResult(
        metric=metric,
        observed=rate,
        threshold=threshold,
        verdict=verdict,
        sample_size=sample_size,
    )


def compute_shadow_dod(
    *,
    stage: str,
    window_days: int = DEFAULT_WINDOW_DAYS,
    prom_url: str,
    until: _dt.datetime | None = None,
) -> DodReport:
    """Per-stage Prometheus shadow-metric DoD summary (Option A1).

    Reads the canary shadow observer's Prometheus counters for ``stage`` over
    the trailing ``window_days`` and reports exactly THREE metrics:
    ``discrepancy_rate``, ``aphelion_unreachable_rate``, and ``min_hits``.

    The three metrics the shadow observer does NOT emit per-stage —
    ``error_rate``, ``p99_latency_ms``, ``data_loss_count`` — are gated by
    the T1-T5 Prometheus **alerts** (auto-rollback), NOT by this summary.
    The alerts are the authoritative promotion gate; this is an advisory
    cross-check. This replaces the SQLite ``OutcomeStore`` path
    (:func:`parallax.canary.dod.compute_dod`) for the CLI per Chris's
    Option A1 (2026-06-07), because nothing populates ``canary_outcomes`` in
    production.

    Args:
        stage: DoD stage name (``m4_1pct``/``m4_10pct``/``m4_50pct``/
            ``m4_100pct``). Mapped to the observer's ``sN`` label.
        window_days: Trailing window for the ``increase()`` aggregate.
        prom_url: Prometheus base URL (e.g. ``http://localhost:9090``).
        until: Accepted for signature parity with ``compute_dod`` and to
            stamp the report window; the Prometheus ``increase()`` aggregate
            is relative-to-now (instant query), so ``until`` does not shift
            the queried window.

    Returns:
        A :class:`DodReport` with three :class:`MetricResult` entries and an
        aggregated ``overall`` verdict.

    Raises:
        ValueError: if ``stage`` is not a known canary stage.
    """
    if stage not in KNOWN_STAGES:
        raise ValueError(
            f"Unknown canary stage: {stage!r}. Expected one of {sorted(KNOWN_STAGES)}."
        )
    shadow_stage = DOD_STAGE_TO_SHADOW[stage]

    end = until.astimezone(_dt.UTC) if until else _dt.datetime.now(_dt.UTC)
    start = end - _dt.timedelta(days=window_days)

    attempts = _prom_instant_query(
        prom_url,
        "sum(increase("
        f'parallax_canary_shadow_attempts_total{{stage="{shadow_stage}"}}'
        f"[{window_days}d]))",
    )
    # `or on() vector(0)` on the NUMERATOR queries: a stage with real traffic
    # but zero bad outcomes has no `outcome="diverge"` counter child, so a bare
    # sum() returns an empty vector → None → INSUFFICIENT_DATA, which would
    # wrongly block a HEALTHY stage. The fallback reads "no bad outcomes" as 0.
    # A real Prometheus outage still fails the HTTP call → None, and the
    # attempts query (deliberately WITHOUT the fallback) then drives the
    # INSUFFICIENT_DATA verdict for genuine no-data.
    diverge = _prom_instant_query(
        prom_url,
        "sum(increase("
        f'parallax_canary_shadow_outcomes_total{{stage="{shadow_stage}",outcome="diverge"}}'
        f"[{window_days}d])) or on() vector(0)",
    )
    aphelion = _prom_instant_query(
        prom_url,
        "sum(increase("
        f'parallax_canary_shadow_outcomes_total{{stage="{shadow_stage}",'
        f'outcome="aphelion_unreachable"}}[{window_days}d])) or on() vector(0)',
    )

    sample_size = int(attempts) if attempts is not None else 0

    discrepancy = _rate_metric(
        DodMetric.DISCREPANCY_RATE,
        numerator=diverge,
        attempts=attempts,
        sample_size=sample_size,
    )
    aphelion_unreachable = _rate_metric(
        DodMetric.APHELION_UNREACHABLE_RATE,
        numerator=aphelion,
        attempts=attempts,
        sample_size=sample_size,
    )
    min_hits = MetricResult(
        metric=DodMetric.MIN_HITS,
        observed=float(sample_size),
        threshold=DOD_THRESHOLD[DodMetric.MIN_HITS],
        verdict=(
            DodVerdict.PASS if sample_size >= _MIN_HITS_FLOOR else DodVerdict.INSUFFICIENT_DATA
        ),
        sample_size=sample_size,
    )

    metrics = (discrepancy, aphelion_unreachable, min_hits)
    return DodReport(
        stage=stage,
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        metrics=metrics,
        overall=_aggregate(m.verdict for m in metrics),
    )
