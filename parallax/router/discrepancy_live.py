"""M3-T1.1 — Live in-process discrepancy counter for US-011 Dual-read.

In-process Prometheus counters/gauges fed live by the ``DualReadRouter``
(T1.2). M2's ``parallax/shadow/discrepancy.py`` parses JSONL files offline;
this module is the live in-process counterpart.

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


def _get_or_create_gauge(
    name: str,
    documentation: str,
    labelnames: list[str],
) -> prometheus_client.Gauge:
    """Return an existing Gauge or create a new one."""
    try:
        return prometheus_client.Gauge(name, documentation, labelnames)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


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

# NOTE: this module deliberately does NOT register a
# ``parallax_dual_read_discrepancy_rate`` gauge. It used to, labelled
# ``["user_id", "traffic_source"]``, which made two producers of that one
# metric name: this one, and the 72h DoD gauge built by
# ``parallax.server.routes.metrics._build_payload``. Only the latter ever
# reached the wire — ``_build_payload`` serializes a fresh
# ``CollectorRegistry`` and plucks just three named counters out of the
# default registry, so this gauge was set on every outcome and read by
# nobody. Worse, the two carried different label sets, so anything that ever
# did render the default registry would have produced two contradictory
# definitions of one metric name.
#
# ``metrics.py`` is now the single producer, partitioned by
# ``traffic_source``. The rolling-window rate this gauge mirrored is still
# computed and still public — call ``dual_read_discrepancy_rate()``.
# Re-adding a collector here would also put the high-cardinality ``user_id``
# label back on the wire; ``parallax_aphelion_total`` already carries it and
# has ~82k distinct values in the live corpus.

_unreachable_rate_gauge = _get_or_create_gauge(
    "parallax_aphelion_unreachable_rate",
    "Rolling-window fraction of dual-read outcomes where Aphelion was unreachable.",
    ["user_id", "traffic_source"],
)

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
    3. Recompute and SET the unreachable-rate gauge for ``user_id``.

    The discrepancy rate is deliberately not mirrored onto a collector here
    — see the note above ``_unreachable_rate_gauge``.
    """
    source = _normalize_traffic_source(traffic_source)
    _outcomes_counter.labels(outcome=outcome, user_id=user_id, traffic_source=source).inc()
    if outcome != "skipped":
        _aphelion_counter.labels(user_id=user_id, traffic_source=source).inc()
    _singleton.record(user_id=user_id, outcome=outcome, traffic_source=source)
    _unreachable_rate_gauge.labels(user_id=user_id, traffic_source=source).set(
        _singleton.aphelion_unreachable_rate(user_id=user_id, traffic_source=source)
    )


def dual_read_discrepancy_rate(*, user_id: str, traffic_source: str | None = None) -> float:
    """Return the current rolling-window discrepancy rate for ``user_id``."""
    return _singleton.discrepancy_rate(user_id=user_id, traffic_source=traffic_source)


def aphelion_unreachable_rate(*, user_id: str, traffic_source: str | None = None) -> float:
    """Return the current rolling-window Aphelion-unreachable rate for ``user_id``."""
    return _singleton.aphelion_unreachable_rate(user_id=user_id, traffic_source=traffic_source)
