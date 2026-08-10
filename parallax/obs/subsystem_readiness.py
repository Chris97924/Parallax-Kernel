"""#106.2 — tell "measured, and healthy" apart from "never ran".

The apex M7 read path and ``SQLiteGate`` both register Prometheus collectors and
are both instantiated nowhere in the running server. #106 declared their twelve
series dead and argued against exporting zeros for them, on the grounds that a
zero is a measurement: ``increase(parallax_apex_read_errors_total[10m]) > 0``
over a permanent 0 reads as "checked, and healthy" for a subsystem that has
never executed a single read.

That argument is right about zeros and wrong about the alternative. No-data has
exactly the same failure mode — a silent alert — and it additionally cannot be
distinguished from a broken scrape, a renamed metric, or a Prometheus outage. So
the resolution is not to pick the less bad silence, but to publish the missing
fact: **whether the subsystem is running at all**.

``parallax_subsystem_wired`` is that fact. It is 0.0 from process start for
every subsystem this module knows about, and flips to 1.0 the first time one is
actually constructed. Paired with a zero-exported counter it answers the
question the counter alone cannot:

    parallax_apex_read_errors_total == 0  and  wired{subsystem="apex_m7"} == 0
        -> nothing has run; the zero carries no information
    parallax_apex_read_errors_total == 0  and  wired{subsystem="apex_m7"} == 1
        -> the read path ran and produced no errors; the zero is a measurement
    (either series absent)
        -> the exporter is broken; neither reading is trustworthy

The alert annotations for those subsystems name this series, so an oncall paged
by silence has somewhere to look that is not the source tree.
"""

from __future__ import annotations

from typing import Final

import prometheus_client

__all__ = [
    "KNOWN_SUBSYSTEMS",
    "SUBSYSTEM_APEX_M7",
    "SUBSYSTEM_SQLITE_GATE",
    "is_wired",
    "mark_wired",
    "subsystem_wired",
]

SUBSYSTEM_APEX_M7: Final[str] = "apex_m7"
SUBSYSTEM_SQLITE_GATE: Final[str] = "sqlite_gate"

#: Every subsystem that zero-exports. The gauge is primed for all of them at
#: import so a scrape from a cold process reports 0.0 rather than omitting the
#: series — an absent readiness signal would reintroduce the ambiguity this
#: module exists to remove.
KNOWN_SUBSYSTEMS: Final[tuple[str, ...]] = (SUBSYSTEM_APEX_M7, SUBSYSTEM_SQLITE_GATE)


def _get_or_create_gauge() -> prometheus_client.Gauge:
    """Register the gauge, or return the one a previous import left behind."""
    try:
        return prometheus_client.Gauge(
            "parallax_subsystem_wired",
            "1.0 iff the named subsystem has been constructed in this process; else 0.0. "
            "Read alongside that subsystem's zero-exported counters to tell a real zero "
            "from a subsystem that never ran.",
            ["subsystem"],
        )
    except ValueError:
        registry = prometheus_client.REGISTRY._names_to_collectors  # type: ignore[attr-defined]
        return registry["parallax_subsystem_wired"]  # type: ignore[return-value]


subsystem_wired = _get_or_create_gauge()

for _subsystem in KNOWN_SUBSYSTEMS:
    # ``.labels(...)`` materialises the child series; without this the family
    # exists with no samples and Prometheus records nothing at all.
    subsystem_wired.labels(subsystem=_subsystem)


def mark_wired(subsystem: str) -> None:
    """Record that ``subsystem`` is now live in this process.

    Called from the constructor of the subsystem itself rather than from a
    wiring site, so it cannot drift out of step with reality: if the object is
    built, the gauge is 1.0, and there is no second place to remember to update.
    """
    subsystem_wired.labels(subsystem=subsystem).set(1.0)


def is_wired(subsystem: str) -> bool:
    """Current readiness of ``subsystem``. Provided for tests and diagnostics."""
    return subsystem_wired.labels(subsystem=subsystem)._value.get() == 1.0  # noqa: SLF001
