"""M3-T1.5 — BreakerState singleton with rolling-window logic (US-011).

Design notes (§10 Q10 ralplan 2026-04-27, lines 547-555):
- Process-local state only.  DOES NOT mutate ``os.environ``.
- Singleton: a single ``BreakerState`` instance lives for the lifetime of the
  Python process.  Tests that need a clean slate should call ``reset()``
  or monkey-patch ``get_breaker_state`` to return a fresh instance.
- Rolling window: 5-minute window, 1% unreachable-rate threshold, minimum
  50 observations before the breaker can trip (cold-start flap guard).
- Manual reset ONLY — no auto-recovery. Q10 DECIDED: "auto-flapping under
  thrashing Aphelion would amplify the incident" (line 552). The operator
  must verify Aphelion health before calling reset().
- The trip transition (False → True) increments ``parallax_circuit_breaker_tripped_total``.
  The counter is monotonic; reset() does NOT decrement it.
- ``record_unreachable_observation`` is thread-safe via ``_lock``.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Final

import prometheus_client

__all__ = ["BreakerState", "get_breaker_state"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_SECONDS: Final[float] = 300.0  # 5-minute rolling window
TRIP_THRESHOLD: Final[float] = 0.01  # 1% unreachable rate (strict >)
MIN_OBSERVATIONS: Final[int] = 50  # cold-start flap guard
# ``MIN_OBSERVATIONS = 50`` ensures the breaker never trips on a tiny sample
# (e.g. 1 unreachable / 1 total = 100% at cold start). At typical low traffic
# (5 req/s) 50 observations accumulate in 10 s — well within the 5-min window.

# ---------------------------------------------------------------------------
# Prometheus Counter (co-located with its semantics — NOT in metrics.py)
# ---------------------------------------------------------------------------


def _get_or_create_counter(name: str, doc: str) -> prometheus_client.Counter:
    """Return an existing Counter or register a new one.

    prometheus_client raises ``ValueError: Duplicated timeseries`` on
    re-import (common in test runs). We catch it and return the existing
    collector via the stable ``REGISTRY._names_to_collectors`` dict.
    """
    try:
        return prometheus_client.Counter(name, doc)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[  # type: ignore[return-value]
            name + "_total"
        ]


circuit_breaker_tripped_total = _get_or_create_counter(
    "parallax_circuit_breaker_tripped_total",
    "Number of times the dual-read circuit breaker has tripped",
)

# ---------------------------------------------------------------------------
# BreakerState
# ---------------------------------------------------------------------------


class BreakerState:
    """Process-local circuit-breaker state for the dual-read path.

    Rolling-window logic (T1.5):
    - Each call to ``record_unreachable_observation`` appends a timestamped
      sample to a deque.  On each append, samples older than ``WINDOW_SECONDS``
      are evicted.
    - When ``not tripped`` and the window has ≥ ``MIN_OBSERVATIONS`` samples
      with ``unreachable_count / total > TRIP_THRESHOLD``, the breaker trips:
      ``tripped=True``, ``tripped_at=now(UTC)``, counter incremented by 1.
    - Once tripped, only ``reset()`` can un-trip it (no auto-recovery).
    """

    def __init__(self) -> None:
        """初始化斷路器狀態，建立觀察窗口佇列與執行緒鎖。"""
        self.tripped: bool = False
        self.tripped_at: datetime | None = None
        # (monotonic_ts, observed_unreachable) — evicted on each append
        self._observations: deque[tuple[float, bool]] = deque()
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_tripped(self) -> bool:
        """Return whether the breaker is currently tripped (under lock)."""
        with self._lock:
            return self.tripped

    def record_unreachable_observation(self, *, observed_unreachable: bool) -> None:
        """Record one unreachable observation from the dual-read path.

        Thread-safe.  The trip transition is atomic under ``_lock`` so
        concurrent threads cannot double-count a single trip event.

        When already tripped, observations are still recorded for future
        rate inspection (e.g. operator dashboards) but the trip transition
        is suppressed — no double-counting.
        """
        now_ts = time.monotonic()
        with self._lock:
            self._observations.append((now_ts, observed_unreachable))
            self._evict_old(now_ts)

            if self.tripped:
                # Already tripped — no auto-recovery.
                return

            total = len(self._observations)
            if total < MIN_OBSERVATIONS:
                return

            unreachable_count = sum(1 for _, u in self._observations if u)
            rate = unreachable_count / total
            if rate > TRIP_THRESHOLD:
                self.tripped = True
                self.tripped_at = datetime.now(UTC)
                circuit_breaker_tripped_total.inc()

    def reset(self) -> None:
        """Manual re-arm: clear tripped state and observation window.

        Called by ``POST /admin/circuit_breaker/reset``.

        IMPORTANT: NO automatic re-arm — the operator must verify Aphelion
        health before calling this.  The Prometheus counter is NOT decremented
        (it is monotonic by design; reset events do not undo trip events).
        """
        with self._lock:
            self.tripped = False
            self.tripped_at = None
            self._observations.clear()

    def current_unreachable_rate(self) -> float | None:
        """Return the current unreachable rate over the rolling window.

        Returns ``None`` when the window has fewer than ``MIN_OBSERVATIONS``
        samples (insufficient sample — caller should treat as "unknown").
        """
        with self._lock:
            now_ts = time.monotonic()
            self._evict_old(now_ts)
            total = len(self._observations)
            if total < MIN_OBSERVATIONS:
                return None
            unreachable_count = sum(1 for _, u in self._observations if u)
            return unreachable_count / total

    def observation_count(self) -> int:
        """Return the number of observations currently in the rolling window."""
        with self._lock:
            now_ts = time.monotonic()
            self._evict_old(now_ts)
            return len(self._observations)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_old(self, now_ts: float) -> None:
        """Remove observations older than WINDOW_SECONDS from the left of the deque.

        Called under ``_lock``.  The deque is ordered by insertion time
        (monotonically increasing), so we can pop from the left until the
        oldest entry is within the window.
        """
        cutoff = now_ts - WINDOW_SECONDS
        while self._observations and self._observations[0][0] < cutoff:
            self._observations.popleft()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_BREAKER_STATE_SINGLETON: BreakerState | None = None
_SINGLETON_LOCK: threading.Lock = threading.Lock()


def get_breaker_state() -> BreakerState:
    """Return the process-local BreakerState singleton.

    Lazy initialisation with double-checked locking so the singleton is
    safe to call from any thread.  Singleton-by-design: process-local
    circuit-breaker state must be shared across all request handlers
    (a new instance per request would defeat the purpose of a breaker).
    """
    global _BREAKER_STATE_SINGLETON  # noqa: PLW0603
    if _BREAKER_STATE_SINGLETON is None:
        with _SINGLETON_LOCK:
            if _BREAKER_STATE_SINGLETON is None:
                _BREAKER_STATE_SINGLETON = BreakerState()
    return _BREAKER_STATE_SINGLETON
