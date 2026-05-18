"""Apex M4 canary shadow observer — per-stage observation overlay.

This module is a thin post-hoc observer over :class:`DualReadRouter`'s
parallel dispatch. It is NOT a second shadow path: ``DualReadRouter`` already
runs primary (``RealMemoryRouter``) and secondary (``AphelionReadAdapter``)
in parallel on every request and classifies the outcome
(``match | diverge | primary_only | aphelion_unreachable | skipped``).

What this module adds: a per-request RNG gate driven by
``PARALLAX_CANARY_SHADOW_FRACTION`` that samples a subset of completed dual-read
results into stage-labelled Prometheus counters. This lets alertmanager
target ``parallax_canary_shadow_discrepancy_rate{stage="s1"}`` (the 1%
observation subset) instead of the global ``parallax_dual_read_discrepancy_rate``,
which is the actual M4 GATE 3-7 ACK semantic.

Stage mapping (resolved from the env value, not request-time RNG):

==============  ==========================  =====================
Stage           PARALLAX_CANARY_SHADOW_FRACTION   M4 GATE
==============  ==========================  =====================
disabled        0.0                         observer off
s1              (0.0, 0.01]                 GATE 3 entry (1%)
s2              (0.01, 0.10]                GATE 4 (10%)
s3              (0.10, 0.50]                GATE 5 (50%)
s4              (0.50, 1.00]                GATE 6/7 (100%)
==============  ==========================  =====================

Design constraints (see ``.omc/autopilot/m4-canary-shadow-observer-spec.md``):

- Pure post-hoc: ``observe()`` never mutates the supplied ``DualReadResult``
  and never raises out — internal exceptions are logged and swallowed so a
  metric backend fault cannot break the client response.
- Zero new dependencies; reuses ``prometheus_client`` and stdlib only.
- Re-import safe: counters use ``_get_or_create_counter`` so a test
  ``importlib.reload`` does not crash on ``DuplicatedTimeseries``.
"""

from __future__ import annotations

import logging
import math
import os
import random
from typing import Final, Literal

import prometheus_client

from parallax.router.contracts import DualReadResult

__all__ = [
    "CanaryStage",
    "CANARY_SHADOW_FRACTION_ENV",
    "get_shadow_fraction",
    "resolve_stage",
    "observe",
]

_log = logging.getLogger(__name__)

# Project-wide env var prefix is ``PARALLAX_`` (see PARALLAX_AUDIT_DB_PATH,
# PARALLAX_SPLIT_IMPLEMENTED, etc.). Keeping the prefix lets operators run
# ``env | grep PARALLAX_`` to see the full canary state in one shot.
CANARY_SHADOW_FRACTION_ENV: Final[str] = "PARALLAX_CANARY_SHADOW_FRACTION"

CanaryStage = Literal["disabled", "s1", "s2", "s3", "s4"]


# ---------------------------------------------------------------------------
# Prometheus collectors (re-import safe)
# ---------------------------------------------------------------------------


def _get_or_create_counter(
    name: str,
    documentation: str,
    labelnames: list[str],
) -> prometheus_client.Counter:
    """Return existing Counter or create new one.

    Mirrors the pattern in ``parallax.router.discrepancy_live`` so test
    re-imports do not crash on ``DuplicatedTimeseries``.
    """
    try:
        return prometheus_client.Counter(name, documentation, labelnames)
    except ValueError:
        # Re-raise unless this is the DuplicatedTimeseries case we expect.
        # (``Counter`` also raises ``ValueError`` for malformed names; we do
        # not want to mask that into a confusing ``KeyError`` below.)
        collectors = prometheus_client.REGISTRY._names_to_collectors  # type: ignore[attr-defined]
        if name + "_total" not in collectors:
            raise
        return collectors[name + "_total"]  # type: ignore[return-value]


_canary_attempts_counter = _get_or_create_counter(
    "parallax_canary_shadow_attempts",
    "Total dual-read results sampled into the canary observation subset.",
    ["stage", "user_id", "traffic_source"],
)

_canary_outcomes_counter = _get_or_create_counter(
    "parallax_canary_shadow_outcomes",
    "Total canary-sampled dual-read outcomes by type, stage, user, traffic source.",
    ["stage", "outcome", "user_id", "traffic_source"],
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_shadow_fraction() -> float:
    """Return ``PARALLAX_CANARY_SHADOW_FRACTION`` parsed as float in [0.0, 1.0].

    Returns 0.0 on missing, malformed, out-of-range, or non-finite input.
    Logs a warning on the first malformed value so the operator notices.
    """
    raw = os.environ.get(CANARY_SHADOW_FRACTION_ENV)
    if raw is None or raw == "":
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        _log.warning(
            "canary_shadow_fraction_invalid: not a float, falling back to 0.0",
            extra={"event": "canary_shadow_fraction_invalid", "raw": raw},
        )
        return 0.0
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        _log.warning(
            "canary_shadow_fraction_invalid: out of range [0,1], falling back to 0.0",
            extra={"event": "canary_shadow_fraction_invalid", "raw": raw},
        )
        return 0.0
    return value


def resolve_stage(fraction: float) -> CanaryStage:
    """Resolve a stage label from the *configured* fraction.

    Boundaries are inclusive on the upper end (e.g. exactly 0.01 → ``s1``)
    so operators using round percentages land on the intended stage.
    """
    if fraction <= 0.0:
        return "disabled"
    if fraction <= 0.01:
        return "s1"
    if fraction <= 0.10:
        return "s2"
    if fraction <= 0.50:
        return "s3"
    return "s4"


def observe(
    result: DualReadResult,
    *,
    user_id: str,
    traffic_source: str,
) -> None:
    """Sample a completed dual-read result into the canary metric pool.

    This is a fire-and-forget side-effect call. It never mutates ``result``
    and never raises out — any internal failure (env parse error, metric
    backend fault) is logged and swallowed.

    Sampling rule:
        - read ``PARALLAX_CANARY_SHADOW_FRACTION``
        - if 0.0 (disabled), skip
        - else if ``random.random() >= fraction``, skip
        - else increment attempts + outcomes counters under the stage label
    """
    try:
        fraction = get_shadow_fraction()
        if fraction <= 0.0:
            return
        if random.random() >= fraction:
            return
        stage = resolve_stage(fraction)
        _canary_attempts_counter.labels(
            stage=stage,
            user_id=user_id,
            traffic_source=traffic_source,
        ).inc()
        _canary_outcomes_counter.labels(
            stage=stage,
            outcome=result.outcome,
            user_id=user_id,
            traffic_source=traffic_source,
        ).inc()
    except Exception as exc:  # noqa: BLE001 - observer must never bubble up
        # exc_info=True preserves the stack trace so a metric-backend fault
        # is diagnosable from the log alone; the swallow is what the spec
        # asks for, not the silence.
        _log.warning(
            "canary_shadow_observe_failed: %s",
            exc,
            exc_info=True,
            extra={"event": "canary_shadow_observe_failed", "error": str(exc)},
        )
