"""US-009.1 §3.3 criteria 1.9-1.14 — rollback trigger primitives.

Five primitives:

* ``T1ErrorRateTrigger``       — error_rate ≥ 0.5% over 5 min sliding
* ``T2DiscrepancyRateTrigger`` — discrepancy_rate ≥ 0.5% over 3 min sliding
* ``T3P99LatencyTrigger``      — p99 latency ≥ 100 ms over 5 min sliding
* ``T4DataLossTrigger``        — any data_loss event (no window)
* ``T5MinHitsGate``            — hits < 50 over 5 min sliding (gate, NOT trigger)

The four T1-T4 triggers each maintain their own sliding-window storage
so they evaluate independently (criterion 1.9). T5 exposes ``is_active``
so the :class:`RollbackController` can mark T1-T4 verdicts as
``insufficient_data`` when sample size is too small (criterion 1.14).

Each trigger keeps observations as ``(timestamp, payload)`` tuples in a
``deque``. Eviction happens on every read so the window stays accurate
without a background sweep thread.
"""

from __future__ import annotations

import dataclasses
import enum
import math
from collections import deque
from collections.abc import Iterable
from typing import Final, Protocol

__all__ = [
    "TriggerVerdict",
    "TriggerEvaluation",
    "T1ErrorRateTrigger",
    "T2DiscrepancyRateTrigger",
    "T3P99LatencyTrigger",
    "T4DataLossTrigger",
    "T5MinHitsGate",
    "T1_THRESHOLD",
    "T2_THRESHOLD",
    "T3_THRESHOLD_MS",
    "T4_THRESHOLD",
    "T5_MIN_HITS",
    "WINDOW_T1_SECONDS",
    "WINDOW_T2_SECONDS",
    "WINDOW_T3_SECONDS",
    "WINDOW_T5_SECONDS",
]


# Threshold + window constants — single source of truth, imported by
# tests + RollbackController. Values pin the spec's §3.3 table.
T1_THRESHOLD: Final[float] = 0.005  # 0.5% error rate
T2_THRESHOLD: Final[float] = 0.005  # 0.5% discrepancy rate
T3_THRESHOLD_MS: Final[float] = 100.0  # p99 latency 100 ms
T4_THRESHOLD: Final[int] = 0  # > 0 events (cumulative)
T5_MIN_HITS: Final[int] = 50  # hits < 50 → gate active

WINDOW_T1_SECONDS: Final[float] = 300.0
WINDOW_T2_SECONDS: Final[float] = 180.0
WINDOW_T3_SECONDS: Final[float] = 300.0
WINDOW_T5_SECONDS: Final[float] = 300.0


class TriggerVerdict(enum.Enum):
    """Outcome of a single trigger evaluation."""

    PASS = "pass"
    BREACH = "breach"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclasses.dataclass(frozen=True)
class TriggerEvaluation:
    """Structured trigger verdict — used by the controller and surfaced
    to operators via the canary CLI / dashboard.
    """

    trigger_id: str
    verdict: TriggerVerdict
    metric_value: float
    threshold: float
    window_seconds: float | None
    observations: int


class _SlidingWindow:
    """Append-only ``(timestamp, value)`` deque with O(1) eviction.

    Eviction runs on every public read so callers don't see stale data;
    that costs O(n) once per call but n is bounded by traffic over the
    window length, which is small even at peak canary volumes.
    """

    __slots__ = ("_data", "_window_seconds")

    def __init__(self, window_seconds: float) -> None:
        self._data: deque[tuple[float, float]] = deque()
        self._window_seconds = window_seconds

    def add(self, ts: float, value: float) -> None:
        self._data.append((ts, value))

    def _evict(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def values(self, now: float) -> list[float]:
        self._evict(now)
        return [v for _, v in self._data]

    def count(self, now: float) -> int:
        self._evict(now)
        return len(self._data)

    def reset(self) -> None:
        self._data.clear()


class _TriggerProtocol(Protocol):
    """Internal — what the RollbackController expects from T1-T4."""

    trigger_id: str
    threshold: float
    window_seconds: float | None

    def evaluate(self, now: float) -> TriggerEvaluation: ...

    def reset(self) -> None: ...


# ---------------------------------------------------------------------------
# T1 — error rate (5 min sliding) ≥ 0.5%
# ---------------------------------------------------------------------------


class T1ErrorRateTrigger:
    """Trip when ``errors / total >= 0.5%`` over the 5 min sliding window.

    Records each request as a 1.0 (error) or 0.0 (success). Evaluation
    reports the ratio — not raw count — because the spec threshold is a
    rate.
    """

    trigger_id = "T1-error-rate"
    threshold = T1_THRESHOLD
    window_seconds: float | None = WINDOW_T1_SECONDS

    def __init__(self) -> None:
        self._window = _SlidingWindow(WINDOW_T1_SECONDS)

    def record(self, ts: float, *, is_error: bool) -> None:
        self._window.add(ts, 1.0 if is_error else 0.0)

    def evaluate(self, now: float) -> TriggerEvaluation:
        values = self._window.values(now)
        n = len(values)
        rate = (sum(values) / n) if n else 0.0
        # Spec criterion 1.10: "達到 0.5% 時 trip；低於 0.5% 時不 trip"
        # → exact 0.5% trips.
        verdict = TriggerVerdict.BREACH if (n > 0 and rate >= T1_THRESHOLD) else TriggerVerdict.PASS
        return TriggerEvaluation(
            trigger_id=self.trigger_id,
            verdict=verdict,
            metric_value=rate,
            threshold=T1_THRESHOLD,
            window_seconds=WINDOW_T1_SECONDS,
            observations=n,
        )

    def reset(self) -> None:
        self._window.reset()


# ---------------------------------------------------------------------------
# T2 — discrepancy rate (3 min sliding) ≥ 0.5%
# ---------------------------------------------------------------------------


class T2DiscrepancyRateTrigger:
    """Trip when ``mismatch / total >= 0.5%`` over the 3 min sliding window."""

    trigger_id = "T2-discrepancy-rate"
    threshold = T2_THRESHOLD
    window_seconds: float | None = WINDOW_T2_SECONDS

    def __init__(self) -> None:
        self._window = _SlidingWindow(WINDOW_T2_SECONDS)

    def record(self, ts: float, *, is_mismatch: bool) -> None:
        self._window.add(ts, 1.0 if is_mismatch else 0.0)

    def evaluate(self, now: float) -> TriggerEvaluation:
        values = self._window.values(now)
        n = len(values)
        rate = (sum(values) / n) if n else 0.0
        verdict = TriggerVerdict.BREACH if (n > 0 and rate >= T2_THRESHOLD) else TriggerVerdict.PASS
        return TriggerEvaluation(
            trigger_id=self.trigger_id,
            verdict=verdict,
            metric_value=rate,
            threshold=T2_THRESHOLD,
            window_seconds=WINDOW_T2_SECONDS,
            observations=n,
        )

    def reset(self) -> None:
        self._window.reset()


# ---------------------------------------------------------------------------
# T3 — p99 latency (5 min sliding) ≥ 100 ms
# ---------------------------------------------------------------------------


class T3P99LatencyTrigger:
    """Trip when p99 latency ≥ 100 ms over the 5 min sliding window.

    p99 is computed via the nearest-rank method on a sorted copy of the
    in-window samples. Cheap at canary volumes (< 1k samples per
    window).
    """

    trigger_id = "T3-p99-latency"
    threshold = T3_THRESHOLD_MS
    window_seconds: float | None = WINDOW_T3_SECONDS

    def __init__(self) -> None:
        self._window = _SlidingWindow(WINDOW_T3_SECONDS)

    def record(self, ts: float, *, latency_ms: float) -> None:
        self._window.add(ts, float(latency_ms))

    def evaluate(self, now: float) -> TriggerEvaluation:
        values = self._window.values(now)
        n = len(values)
        if n == 0:
            return TriggerEvaluation(
                trigger_id=self.trigger_id,
                verdict=TriggerVerdict.PASS,
                metric_value=0.0,
                threshold=T3_THRESHOLD_MS,
                window_seconds=WINDOW_T3_SECONDS,
                observations=0,
            )
        sorted_values = sorted(values)
        # nearest-rank: index = ceil(0.99 * n) - 1, clamped to [0, n-1]
        idx = max(0, min(n - 1, math.ceil(0.99 * n) - 1))
        p99 = sorted_values[idx]
        verdict = TriggerVerdict.BREACH if p99 >= T3_THRESHOLD_MS else TriggerVerdict.PASS
        return TriggerEvaluation(
            trigger_id=self.trigger_id,
            verdict=verdict,
            metric_value=p99,
            threshold=T3_THRESHOLD_MS,
            window_seconds=WINDOW_T3_SECONDS,
            observations=n,
        )

    def reset(self) -> None:
        self._window.reset()


# ---------------------------------------------------------------------------
# T4 — data loss (cumulative, no window)
# ---------------------------------------------------------------------------


class T4DataLossTrigger:
    """Immediate trip on any data-loss observation (criterion 1.13).

    Per spec §3.3: "any single data-loss event triggers immediate
    rollback regardless of window size". Cumulative counter — never
    decreases except via :meth:`reset`.
    """

    trigger_id = "T4-data-loss"
    threshold = float(T4_THRESHOLD)
    window_seconds: float | None = None  # cumulative

    def __init__(self) -> None:
        self._count = 0

    def record(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("data_loss count must be non-negative")
        self._count += count

    def evaluate(self, now: float | None = None) -> TriggerEvaluation:  # noqa: ARG002
        verdict = TriggerVerdict.BREACH if self._count > T4_THRESHOLD else TriggerVerdict.PASS
        return TriggerEvaluation(
            trigger_id=self.trigger_id,
            verdict=verdict,
            metric_value=float(self._count),
            threshold=float(T4_THRESHOLD),
            window_seconds=None,
            observations=self._count,
        )

    def reset(self) -> None:
        self._count = 0


# ---------------------------------------------------------------------------
# T5 — min hits gate (NOT a trigger)
# ---------------------------------------------------------------------------


class T5MinHitsGate:
    """Sample-size gate. ``hits < 50`` in the 5 min window → gate active.

    The gate itself NEVER trips a rollback (criterion 1.14). It only
    informs the controller that T1-T4 verdicts should be downgraded to
    ``INSUFFICIENT_DATA`` because the sample is too small to be
    statistically meaningful.
    """

    gate_id = "T5-min-hits-gate"
    threshold = float(T5_MIN_HITS)
    window_seconds: float = WINDOW_T5_SECONDS

    def __init__(self) -> None:
        self._window = _SlidingWindow(WINDOW_T5_SECONDS)

    def record(self, ts: float) -> None:
        """Record a single canary hit. ``ts`` is a monotonic timestamp."""
        self._window.add(ts, 1.0)

    def hits(self, now: float) -> int:
        return self._window.count(now)

    def is_active(self, now: float) -> bool:
        """Gate active when in-window hits < threshold."""
        return self.hits(now) < T5_MIN_HITS

    def evaluate(self, now: float) -> TriggerEvaluation:
        n = self.hits(now)
        # Verdict semantics for the gate are different from triggers:
        # PASS when there's enough data (hits >= threshold), and
        # INSUFFICIENT_DATA when active. Gates never BREACH.
        verdict = TriggerVerdict.INSUFFICIENT_DATA if n < T5_MIN_HITS else TriggerVerdict.PASS
        return TriggerEvaluation(
            trigger_id=self.gate_id,
            verdict=verdict,
            metric_value=float(n),
            threshold=float(T5_MIN_HITS),
            window_seconds=WINDOW_T5_SECONDS,
            observations=n,
        )

    def reset(self) -> None:
        self._window.reset()


def all_triggers() -> Iterable[_TriggerProtocol]:  # pragma: no cover — convenience
    """Return the four T1-T4 triggers in spec order."""
    return (
        T1ErrorRateTrigger(),
        T2DiscrepancyRateTrigger(),
        T3P99LatencyTrigger(),
        T4DataLossTrigger(),
    )
