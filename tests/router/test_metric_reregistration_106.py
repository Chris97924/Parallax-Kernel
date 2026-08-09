"""#106.4 — the duplicate-registration fallbacks must return the live collector.

``prometheus_client`` raises ``ValueError: Duplicated timeseries`` when a module
that owns a module-level collector is imported twice (a re-import under
``importlib.reload``, a test that re-executes the module, a plugin loaded under
two names). Both helpers below exist to absorb that: on ValueError they look the
already-registered collector up in ``REGISTRY._names_to_collectors`` and hand it
back, so the second import binds the same object instead of exploding.

The lookup key was wrong. ``prometheus_client`` strips a trailing ``_total``
from a Counter at construction, so ``Counter("parallax_x_total")`` registers
under the *base* name ``parallax_x`` and the registry indexes it under every
name it can be referenced by — ``parallax_x``, ``parallax_x_total`` and
``parallax_x_created``. Appending ``_total`` to a name that already ends in
``_total`` therefore asks for ``parallax_x_total_total``, which is in no
collector's name set, and the fallback raises ``KeyError`` — replacing a
recoverable duplicate-registration with a hard import failure.

``lifespan.py`` got this right (it looks up ``parallax_drain_timeout_total``
verbatim); these two did not. The fix drops the suffix arithmetic entirely:
the base name is always in the registry index, so the name as passed resolves
whether or not it carries ``_total``.

RED on the unfixed tree: both tests raise ``KeyError`` at the fallback.
"""

from __future__ import annotations

import prometheus_client
import pytest

from parallax.router import sqlite_gate
from parallax.router.circuit_breaker import (
    _get_or_create_counter as breaker_get_or_create_counter,
)
from parallax.router.circuit_breaker import (
    circuit_breaker_tripped_total,
)


def test_breaker_counter_reregistration_returns_the_live_collector() -> None:
    """A second registration must yield the object the first one bound.

    Identity, not equality: the whole point of the fallback is that a re-imported
    module increments the counter Prometheus is already exporting. A fresh
    equal-looking collector would silently split the series in two, and the
    ``CircuitBreakerTripped`` CRITICAL alert would read whichever half the
    renderer happened to find.
    """
    again = breaker_get_or_create_counter(
        "parallax_circuit_breaker_tripped_total",
        "Number of times the dual-read circuit breaker has tripped",
    )

    assert again is circuit_breaker_tripped_total


def test_sqlite_gate_counter_reregistration_returns_the_live_collector() -> None:
    """Same defect, same helper shape, second module.

    ``sqlite_gate._get_or_create_counter`` carries an identical ``name +
    "_total"`` fallback and is called with ``"parallax_sqlite_errors_total"`` —
    a name that already ends in ``_total``, so it is reachable in exactly the
    same way and fixed in exactly the same way.
    """
    again = sqlite_gate._get_or_create_counter(
        "parallax_sqlite_errors_total",
        "Count of sqlite errors by error class, component, and op.",
        ["code", "component", "op"],
    )

    assert again is sqlite_gate._errors_counter


@pytest.mark.parametrize("suffix", ["", "_total"])
def test_fallback_resolves_a_name_with_or_without_the_total_suffix(suffix: str) -> None:
    """The corrected lookup must work for both spellings a caller may pass.

    ``circuit_breaker`` passes the wire name (``..._total``) and other call
    sites pass the base name; the registry indexes a Counter under both, so one
    unsuffixed lookup covers them. Each case registers its own probe name — the
    default registry is process-global, so a shared name would make the second
    parametrisation collide with the first at construction rather than in the
    fallback under test.
    """
    base = f"parallax_test_reregistration_probe_106{suffix or '_bare'}"
    first = prometheus_client.Counter(f"{base}_total", "probe counter for #106")

    resolved = breaker_get_or_create_counter(f"{base}{suffix}", "probe counter for #106")

    assert resolved is first
