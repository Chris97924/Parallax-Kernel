"""M3-T1.1 — Live in-process discrepancy counter for US-011 Dual-read.

In-process Prometheus counters fed live by the ``DualReadRouter`` (T1.2).
M2's ``parallax/shadow/discrepancy.py`` parses JSONL files offline; this
module is the live in-process counterpart.

Counters only — this module registers no gauges. The rate gauges it used to
mirror are exposed by ``parallax.server.routes.metrics`` from the 72h
decision-log corpus; see the note in the collectors section below.

Public API:
    DUAL_READ_DISCREPANCY_RATE_THRESHOLD  -- 0.1% (Option B, ralplan §6 line 416)
    APHELION_UNREACHABLE_RATE_THRESHOLD   -- 0.5% (ralplan §6 line 420)
    DualReadOutcome                       -- Literal of five outcome labels
    LiveDiscrepancyCounter                -- rolling-window per-user state
    record_dual_read_outcome              -- module-level convenience wrapper
    dual_read_discrepancy_rate            -- pure read on singleton
    aphelion_unreachable_rate             -- pure read on singleton
    parallax_aphelion_total               -- Aphelion-bound request counter

Design notes
------------
- M2's ``DISCREPANCY_RATE_THRESHOLD = 0.003`` (0.3%) is intentionally left
  unchanged. This module uses 0.001 (0.1%) per Q2 Option B decision.
- Q3 decision: new stream — ``DualReadOutcome`` is NOT an extension of M2's
  ``ArbitrationOutcome`` Literal. Both Literals stay independent.
- Thread safety: a single ``threading.Lock`` guards the whole deque dict.
  Per-user locks were considered but the single global lock is simpler and
  correct; contention is bounded by the roll-up write frequency, not by
  user count.
- Prometheus collectors registered at module scope. Re-import in tests causes
  a ``ValueError``; we catch it and retrieve the existing collector from
  ``REGISTRY._names_to_collectors`` (a stable internal that prometheus_client
  has never broken across minor versions).
"""

from __future__ import annotations

import collections
import dataclasses
import threading
import time
from typing import Final, Literal

import prometheus_client

__all__ = [
    "DUAL_READ_DISCREPANCY_RATE_THRESHOLD",
    "APHELION_UNREACHABLE_RATE_THRESHOLD",
    "DualReadOutcome",
    "LiveDiscrepancyCounter",
    "record_dual_read_outcome",
    "record_dual_read_request",
    "dual_read_discrepancy_rate",
    "aphelion_unreachable_rate",
]

# ---------------------------------------------------------------------------
# Constants (pinned to ralplan §6 thresholds — DIFFERENT from M2's 0.003)
# ---------------------------------------------------------------------------

DUAL_READ_DISCREPANCY_RATE_THRESHOLD: Final[float] = 0.001  # 0.1%, Q2 Option B
APHELION_UNREACHABLE_RATE_THRESHOLD: Final[float] = 0.005  # 0.5%

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

DualReadOutcome = Literal["match", "diverge", "primary_only", "aphelion_unreachable", "skipped"]
TrafficSource = Literal["synthetic", "natural"]
KNOWN_TRAFFIC_SOURCES: Final[frozenset[str]] = frozenset({"synthetic", "natural"})
DEFAULT_TRAFFIC_SOURCE: Final[TrafficSource] = "natural"


def _normalize_traffic_source(value: str | None) -> TrafficSource:
    if value is None:
        return DEFAULT_TRAFFIC_SOURCE
    candidate = value.strip().lower()
    if candidate == "synthetic":
        return "synthetic"
    return DEFAULT_TRAFFIC_SOURCE

# ---------------------------------------------------------------------------
# Prometheus collectors
# ---------------------------------------------------------------------------


def _get_or_create_counter(
    name: str,
    documentation: str,
    labelnames: list[str],
) -> prometheus_client.Counter:
    """Return an existing Counter or create a new one.

    prometheus_client raises ``ValueError: Duplicated timeseries`` when the
    same name is registered twice (happens on test module re-import).
    """
    try:
        return prometheus_client.Counter(name, documentation, labelnames)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[name + "_total"]  # type: ignore[return-value]


_outcomes_counter = _get_or_create_counter(
    "parallax_dual_read_outcomes",
    "Total dual-read outcome events by type, user, and burn-in traffic source.",
    ["outcome", "user_id", "traffic_source"],
)

_aphelion_counter = _get_or_create_counter(
    "parallax_aphelion",
    "Total dual-read requests that attempted the Aphelion secondary.",
    ["user_id", "traffic_source"],
)

# Liveness counter for DualReadDecisionLogSilent. Deliberately NOT labelled by
# ``user_id`` — two or three series total, ever.
#
# The alert needs to know "were dual-read requests being served?" so it can
# tell a broken decision-log writer from a legitimately quiet system. It used
# to ask ``parallax_aphelion_total`` that question, which is wrong twice over:
#
#   1. ``prometheus_client`` retains every label combination for the life of
#      the process, so a user that queries once leaves a series pinned at 1
#      forever. ``increase()`` diffs last-minus-first and returns 0 for it,
#      and there is no "series appeared from nothing" delta because Prometheus
#      has no earlier sample to diff against. Traffic made of mostly-unique
#      user ids therefore summed to ~0 and silently SUPPRESSED the alert — in
#      exactly the sparse natural-traffic regime it exists for.
#   2. Summing ``increase()`` across ~82k retained user-id series is expensive
#      for what is a yes/no liveness question.
_requests_counter = _get_or_create_counter(
    "parallax_dual_read_requests",
    "Total dual-read query attempts by traffic source. Counted at request "
    "entry, before any decision-log write is attempted, so it stays live when "
    "the writer fails.",
    ["traffic_source"],
)

# NOTE: this module deliberately registers NO gauges. It used to register
# ``parallax_dual_read_discrepancy_rate`` and
# ``parallax_aphelion_unreachable_rate``, both labelled
# ``["user_id", "traffic_source"]``, which made two producers of each of
# those metric names: these, and the 72h DoD gauges built by
# ``parallax.server.routes.metrics._build_payload``. Only the latter ever
# reached the wire — ``_build_payload`` serializes a fresh
# ``CollectorRegistry`` and plucks a fixed list of counters out of the
# default registry, so these gauges were set on every outcome and read by
# nobody. Worse, the two carried different label sets, so anything that ever
# did render the default registry would have produced two contradictory
# definitions of one metric name.
#
# The discrepancy gauge went in #100; the unreachable gauge went in #101,
# where the never-rendered half was what left ``AphelionUnreachableRateHigh``
# and two Grafana targets matching nothing at all.
#
# ``metrics.py`` is now the single producer of both, partitioned by
# ``traffic_source``. The rolling-window rates these gauges mirrored are
# still computed and still public — call ``dual_read_discrepancy_rate()``
# and ``aphelion_unreachable_rate()``. Re-adding a collector here would also
# put the high-cardinality ``user_id`` label back on the wire;
# ``parallax_aphelion_total`` already carries it and has ~82k distinct
# values in the live corpus.

# ---------------------------------------------------------------------------
# LiveDiscrepancyCounter
# ---------------------------------------------------------------------------

# Internal record: (monotonic_timestamp, outcome)
_Entry = tuple[float, str]


@dataclasses.dataclass
class LiveDiscrepancyCounter:
    """Process-local rolling-window counter. Thread-safe.

    Uses a single module-level lock to protect the per-user deques.
    This is simpler than per-user locks and correct: the lock is held only
    for deque.append + deque trimming, which is O(evicted_entries) but
    bounded by ``window_seconds``.
    """

    window_seconds: float = 3600.0  # mirrors M2's discrepancy_rate(window='1h')

    def __post_init__(self) -> None:
        # Per (user_id, traffic_source) deques keep M4 synthetic and natural
        # burn-in windows independent.
        self._data: dict[tuple[str, TrafficSource], collections.deque[_Entry]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        user_id: str,
        outcome: DualReadOutcome,
        traffic_source: str | None = None,
    ) -> None:
        """Append (now, outcome) for user; trim entries older than window."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        source = _normalize_traffic_source(traffic_source)
        with self._lock:
            dq = self._data.setdefault((user_id, source), collections.deque())
            dq.append((now, outcome))
            # Trim front (oldest entries)
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    def discrepancy_rate(self, *, user_id: str, traffic_source: str | None = None) -> float:
        """Fraction of in-window outcomes that are 'diverge'.

        Excludes 'aphelion_unreachable' from the denominator (mirrors M2's
        exclusion of 'shadow_only' from the discrepancy denominator per
        ralplan §6 line 429). Empty window → 0.0.
        """
        source = _normalize_traffic_source(traffic_source)
        with self._lock:
            dq = self._data.get((user_id, source))
            if not dq:
                return 0.0
            entries = list(dq)

        # Denominator: all outcomes EXCEPT aphelion_unreachable
        denominator = sum(1 for _, o in entries if o != "aphelion_unreachable")
        if denominator == 0:
            return 0.0
        diverge = sum(1 for _, o in entries if o == "diverge")
        return diverge / denominator

    def aphelion_unreachable_rate(
        self,
        *,
        user_id: str,
        traffic_source: str | None = None,
    ) -> float:
        """Fraction of in-window outcomes that are 'aphelion_unreachable'.

        Denominator is ALL outcomes (total events). Empty window → 0.0.
        """
        source = _normalize_traffic_source(traffic_source)
        with self._lock:
            dq = self._data.get((user_id, source))
            if not dq:
                return 0.0
            entries = list(dq)

        total = len(entries)
        if total == 0:
            return 0.0
        unreachable = sum(1 for _, o in entries if o == "aphelion_unreachable")
        return unreachable / total

    def reset(self) -> None:
        """Clear all per-user deques. Test helper."""
        with self._lock:
            self._data.clear()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_singleton = LiveDiscrepancyCounter()

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def record_dual_read_outcome(
    *,
    user_id: str,
    outcome: DualReadOutcome,
    traffic_source: str | None = None,
) -> None:
    """Record one dual-read outcome:

    1. Increment ``parallax_dual_read_outcomes_total{outcome, user_id, traffic_source}``.
    2. Append to singleton rolling window.

    Neither the discrepancy rate nor the unreachable rate is mirrored onto a
    collector here — see the "registers NO gauges" note in the collectors
    section. Both are exposed by ``parallax.server.routes.metrics``, computed
    from the 72h decision-log corpus and partitioned by ``traffic_source``.
    """
    source = _normalize_traffic_source(traffic_source)
    _outcomes_counter.labels(outcome=outcome, user_id=user_id, traffic_source=source).inc()
    if outcome != "skipped":
        _aphelion_counter.labels(user_id=user_id, traffic_source=source).inc()
    _singleton.record(user_id=user_id, outcome=outcome, traffic_source=source)


def record_dual_read_request(*, traffic_source: str | None = None) -> None:
    """Count one dual-read query ATTEMPT.

    Call this at request entry, before the decision-log write is attempted and
    outside its exception handling. The distinction is the whole point: this
    counter measures attempts and the decision log measures successful writes,
    so their DIVERGENCE is what tells an operator the writer is broken. If
    this only advanced when a record was written, a failing writer would stop
    both signals together, ``DualReadDecisionLogSilent``'s traffic guard would
    read false, and the alert could never fire in the one situation it was
    built for.
    """
    _requests_counter.labels(traffic_source=_normalize_traffic_source(traffic_source)).inc()


def dual_read_discrepancy_rate(*, user_id: str, traffic_source: str | None = None) -> float:
    """Return the current rolling-window discrepancy rate for ``user_id``."""
    return _singleton.discrepancy_rate(user_id=user_id, traffic_source=traffic_source)


def aphelion_unreachable_rate(*, user_id: str, traffic_source: str | None = None) -> float:
    """Return the current rolling-window Aphelion-unreachable rate for ``user_id``."""
    return _singleton.aphelion_unreachable_rate(user_id=user_id, traffic_source=traffic_source)
