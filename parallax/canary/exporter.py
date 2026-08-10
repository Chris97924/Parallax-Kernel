"""#106.1 — the server-side canary exporter: T1-T5 over the durable store.

The M4 canary trigger machinery (``triggers.py`` / ``rollback.py`` /
``outcomes.py``) is real, and it is imported only by ``parallax/canary/cli.py``.
It therefore runs inside short-lived ``parallax canary`` processes that
Prometheus never scrapes, while five alerts and seven dashboard panels select
``parallax_canary_*`` series that nothing has ever emitted. #106 documented that
gap and deferred the fix, noting that "add producers" is not a wiring change: it
needs a server-side exporter reading the durable OutcomeStore/AuditLog. This is
that exporter.

WHAT EACH SERIES IS DERIVED FROM
--------------------------------
The two SQLite tables in the canary audit DB carry different halves of an event
and share ``event_id``, which is what makes the join below well-defined:

* ``canary_outcomes(event_id, stage, outcome)`` — the business verdict, one of
  ``ok`` / ``discrepancy`` / ``data_loss``. It is the canary event ledger, so it
  is the denominator.
* ``audit_log(event_id, response_status, latency_ms, ...)`` — the HTTP envelope.
  It is where an *error* (a status, not an outcome) and a *duration* live.

  ===================================== ==================================================
  series                                derivation
  ===================================== ==================================================
  parallax_canary_events_total          COUNT(canary_outcomes) BY (stage, outcome)
  parallax_canary_event_errors_total    COUNT(join) BY stage WHERE response_status >= 500
  parallax_canary_discrepancy_total     COUNT(canary_outcomes) BY stage WHERE outcome=
                                        'discrepancy'
  parallax_canary_data_loss_events_total same, outcome='data_loss'
  parallax_canary_request_duration_ms   histogram over audit_log.latency_ms via the join
  parallax_canary_rollback_state        canary_rollback_state.state, numerically encoded
  ===================================== ==================================================

``response_status >= 500`` and not ``>= 400``: T1 is an auto-rollback gate, and
a 4xx is the client asking for something wrong, not the canary being wrong.
Rolling back a deploy because a caller sent bad requests would be a false
positive on the most expensive alert in the group. The floor is a named constant
so the choice is greppable and testable rather than an inline literal.

The ACK rows ``AuditLog.record_ack`` writes carry ``response_status = 0`` and a
synthetic event_id with no ``canary_outcomes`` row; the join excludes them
without needing a special case, which is why it is a join and not two scans.

READ-ONLY, ALWAYS
-----------------
A scrape must not create, migrate or lock the canary database. The connection is
opened with ``mode=ro`` and a missing file is a normal state, not an error, so
``/metrics`` on a server that has never run a canary works and reports
``parallax_canary_store_present 0``.

DEPLOYMENT NOTE: the store is a WAL database, and SQLite materialises the
``-shm`` / ``-wal`` sidecars next to it even for a read-only connection. The
directory holding the canary DB must therefore be writable by the server
process, or the scrape degrades to ``store_present 0`` — it will not raise, but
it will also not report. This is a property of reading WAL, not of this module.

ZEROS AND WHAT THEY MEAN
------------------------
Every series is exported whether or not the store has a row for it, because an
``increase(...) > 0`` alert over a series Prometheus has never seen is silent in
exactly the way a healthy canary is — the defect this closes. The baseline is
the full ``KNOWN_STAGES × KNOWN_OUTCOMES`` grid rather than one placeholder
row, and the difference is load-bearing: a series that first appears on its own
first increment is already at 1.0 and has no 0 sample to step from, so T4 would
miss the first data-loss event on a stage. See ``_BASELINE_STAGES``.

``parallax_canary_store_present`` is what keeps those zeros honest, and it means
**the outcome ledger was readable**, not "a file opened". An audit-only database
— what a real-mode drill without ``--stage`` leaves behind — has no
``canary_outcomes`` table at all, and reporting it as present would hand an
oncall zeros the annotations tell them to trust.

NOTE FOR OPERATORS: with no store the T5 gate
(``sum(increase(events_total[5m])) < 50``) evaluates true and fires. That is
deliberate — "the sample is too thin to promote on" is correct when nobody is
measuring — but it does mean T5 is a standing warning on a server with no
canary. Its annotation says so.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Final

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)

from parallax.canary.audit_log import AUDIT_DB_ENV, DEFAULT_AUDIT_DB_NAME
from parallax.canary.outcomes import KNOWN_OUTCOMES, KNOWN_STAGES
from parallax.canary.rollback_state import STATE_UNKNOWN, STATE_VALUES

__all__ = [
    "CANARY_DURATION_BUCKETS_MS",
    "ERROR_STATUS_FLOOR",
    "CanaryExporter",
    "CanarySnapshot",
    "collect_canary_snapshot",
    "resolve_store_path",
]

_log = logging.getLogger(__name__)

#: A canary "error" for T1 purposes. Server-side failures only — see module
#: docstring on why 4xx is excluded from an auto-rollback gate.
ERROR_STATUS_FLOOR: Final[int] = 500

#: Milliseconds, straddling the T3 threshold (triggers.T3_THRESHOLD_MS == 100).
#: prometheus_client's DEFAULT_BUCKETS are in seconds and top out at le=10, so a
#: 50ms canary request would land only in +Inf and histogram_quantile would
#: saturate — the same structural un-measurability the apex latency histogram
#: was given explicit buckets to avoid. Bounds sit either side of 100ms so p99
#: resolves below the gate and breaches above it stay visible.
CANARY_DURATION_BUCKETS_MS: Final[tuple[float, ...]] = (
    1.0,
    2.5,
    5.0,
    10.0,
    25.0,
    50.0,
    75.0,
    100.0,
    150.0,
    250.0,
    500.0,
    1000.0,
)

#: Every stage/outcome pair is exported at zero, whether or not the store has a
#: row for it, and THIS IS NOT THE #106.2 PRIMING RULE. The apex/sqlite zero
#: export uses a single all-empty baseline because its label spaces are open
#: (exception class names) and its alerts wrap `sum()`. Here neither holds.
#:
#: The canary alerts are PER-SERIES ``increase(...)``: T4 is
#: ``increase(parallax_canary_data_loss_events_total[1h]) > 0``. A series that
#: first appears on its first increment springs into existence already at 1.0
#: and stays there, and increase() over samples that are all 1.0 is 0 — so the
#: FIRST data-loss event on a stage, the event the no-hysteresis critical alert
#: exists for, would evaluate as "measured, and healthy". That is the same
#: absent-until-first-increment trap the #102 exposition comment in
#: routes/metrics.py documents for the breaker counters, and the reason those
#: are exported from zero too. Prometheus needs the 0 sample to see a step.
#:
#: Priming the real names costs nothing in honesty because the sets are closed
#: and spec-pinned: KNOWN_STAGES is the four shipping canary stages (adding one
#: "requires a council decision per acceptance spec O.3") and KNOWN_OUTCOMES is
#: the three business outcomes OutcomeStore validates inserts against. These are
#: not guesses about what might be observed; they are the complete enumeration
#: of what CAN be.
_BASELINE_STAGES: Final[tuple[str, ...]] = tuple(sorted(KNOWN_STAGES))
_BASELINE_OUTCOMES: Final[tuple[str, ...]] = tuple(sorted(KNOWN_OUTCOMES))

_CACHE_TTL_SECONDS: Final[float] = 30.0

_cache_lock = threading.Lock()
_cache: CanarySnapshot | None = None
_cache_at: float = 0.0


@dataclasses.dataclass(frozen=True)
class CanarySnapshot:
    """One read of the durable canary store.

    Every mapping is keyed by stage (``events`` by ``(stage, outcome)``) and
    holds only stages the store actually contains. The zero baseline is added at
    render time, not here, so a caller inspecting a snapshot sees the data and
    not the padding.
    """

    events: dict[tuple[str, str], int]
    errors: dict[str, int]
    durations_ms: dict[str, list[float]]
    rollback_state: float
    store_present: bool

    @property
    def discrepancies(self) -> dict[str, int]:
        """T2 numerator — derived, so it can never disagree with ``events``."""
        return {
            stage: count
            for (stage, outcome), count in self.events.items()
            if outcome == "discrepancy"
        }

    @property
    def data_loss(self) -> dict[str, int]:
        """T4 numerator — derived from the same rows as the denominator."""
        return {
            stage: count
            for (stage, outcome), count in self.events.items()
            if outcome == "data_loss"
        }

    @property
    def total_events(self) -> int:
        """T5 sample size: every row in the outcome store."""
        return sum(self.events.values())

    @property
    def stages(self) -> tuple[str, ...]:
        """Every stage to export, primed set included.

        The union of the four spec-pinned stages and anything the store actually
        holds. Both halves matter and for different reasons:

        * the primed stages give every series a 0 sample before its first event,
          without which ``increase()`` cannot see the step (see
          ``_BASELINE_STAGES``);
        * a stage present in the store but not in ``KNOWN_STAGES`` — only
          reachable if the enum changed under a store written by an older build
          — is still exported, because dropping rows on the floor is a worse
          failure than exporting an unexpected label.

        Zero-filling the numerator families across this set is not cosmetic
        either. T1 is ``sum(rate(errors)) / (sum(rate(events)) > 0)``: with a
        denominator series present and the numerator ABSENT — which is what a
        healthy stage produces if you only export stages that had errors — the
        division yields an empty vector and the alert goes silent for exactly
        the reason the whole #106 class was silent. A healthy stage has to say
        zero out loud.
        """
        return tuple(sorted(set(_BASELINE_STAGES) | {stage for stage, _ in self.events}))

    @property
    def event_cells(self) -> tuple[tuple[str, str], ...]:
        """Every ``(stage, outcome)`` pair to export, primed grid included."""
        return tuple(
            sorted(
                {(stage, outcome) for stage in _BASELINE_STAGES for outcome in _BASELINE_OUTCOMES}
                | set(self.events)
            )
        )


def resolve_store_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Explicit argument -> ``PARALLAX_CANARY_AUDIT_DB`` -> cwd default.

    Identical resolution to :class:`parallax.canary.audit_log.AuditLog` and
    :class:`parallax.canary.outcomes.OutcomeStore`, so one env var points the
    CLI writer and this reader at the same file.
    """
    if path is not None:
        return Path(path)
    env = os.environ.get(AUDIT_DB_ENV)
    if env:
        return Path(env)
    return Path.cwd() / DEFAULT_AUDIT_DB_NAME


def _connect_readonly(path: Path) -> sqlite3.Connection | None:
    """Open ``path`` read-only, or return None if it cannot be read.

    ``mode=ro`` is load-bearing: the default connect() would CREATE an empty
    database, so a scrape against a misconfigured path would silently
    manufacture the very store it is supposed to be reporting on.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        _log.warning(
            "canary_exporter.open_failed",
            extra={"event": "canary_exporter.open_failed", "exc_class": type(exc).__name__},
        )
        return None


def _empty_snapshot(*, store_present: bool) -> CanarySnapshot:
    return CanarySnapshot(
        events={},
        errors={},
        durations_ms={},
        rollback_state=STATE_UNKNOWN,
        store_present=store_present,
    )


def collect_canary_snapshot(path: str | os.PathLike[str] | None = None) -> CanarySnapshot:
    """Read the durable canary store. Never raises.

    A missing store, a store predating one of the tables, or a locked database
    all degrade to an empty snapshot: observability may not take down the scrape
    endpoint, and ``store_present`` plus the zero baseline let a reader tell the
    degraded case from a genuinely idle canary.
    """
    conn = _connect_readonly(resolve_store_path(path))
    if conn is None:
        return _empty_snapshot(store_present=False)

    # Each query is its own failure domain, deliberately. The three tables are
    # created by three different writers at three different times — AuditLog on
    # construction, OutcomeStore on construction, RollbackStateStore on the
    # first controller — so a store with only some of them is an ordinary state,
    # not corruption. One try around the whole read would turn "the controller
    # has not run yet" into an empty snapshot and lose the outcome counts that
    # were sitting right there.
    try:
        # THE LEDGER QUERY DECIDES ``store_present``, not the file opening.
        # An audit-only database — which a real-mode drill run WITHOUT --stage
        # creates, because AuditLog builds its table and OutcomeStore is never
        # constructed — opens cleanly and has no canary_outcomes at all. Reporting
        # that as present would export zeros next to ``store_present 1``, which the
        # alert annotations tell an oncall to read as measurements, and T1-T4 would
        # be silently trusted over a ledger that was never there.
        outcome_rows, ledger_readable = _query(
            conn,
            "SELECT stage, outcome, COUNT(*) AS n FROM canary_outcomes GROUP BY stage, outcome",
        )
        events: dict[tuple[str, str], int] = {}
        for row in outcome_rows:
            events[(str(row["stage"]), str(row["outcome"]))] = int(row["n"])

        errors: dict[str, int] = {}
        error_rows, _ = _query(
            conn,
            "SELECT o.stage AS stage, COUNT(*) AS n "
            "FROM canary_outcomes o JOIN audit_log a ON a.event_id = o.event_id "
            "WHERE a.response_status >= ? GROUP BY o.stage",
            (ERROR_STATUS_FLOOR,),
        )
        for row in error_rows:
            errors[str(row["stage"])] = int(row["n"])

        durations: dict[str, list[float]] = {}
        duration_rows, _ = _query(
            conn,
            "SELECT o.stage AS stage, a.latency_ms AS latency_ms "
            "FROM canary_outcomes o JOIN audit_log a ON a.event_id = o.event_id "
            "WHERE a.latency_ms IS NOT NULL",
        )
        for row in duration_rows:
            durations.setdefault(str(row["stage"]), []).append(float(row["latency_ms"]))

        rollback_state = STATE_UNKNOWN
        state_rows, _ = _query(conn, "SELECT state FROM canary_rollback_state WHERE id = 1")
        if state_rows:
            rollback_state = STATE_VALUES.get(str(state_rows[0]["state"]), STATE_UNKNOWN)

        return CanarySnapshot(
            events=events,
            errors=errors,
            durations_ms=durations,
            rollback_state=rollback_state,
            store_present=ledger_readable,
        )
    finally:
        conn.close()


def _query(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> tuple[list[sqlite3.Row], bool]:
    """Run one read. Returns ``(rows, ok)``; an unavailable table yields ``([], False)``.

    The bool is not decoration: callers must be able to tell "this query returned
    nothing" from "this query could not run", because zero rows is a
    measurement and a missing table is not. ``store_present`` is derived from
    it.

    Logged rather than swallowed, so a genuinely broken store is diagnosable
    from the log instead of only from a suspiciously flat dashboard.
    """
    try:
        return conn.execute(sql, params).fetchall(), True
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            # Normal, not broken: the three tables are created by three writers
            # at different times, and canary_rollback_state in particular does
            # not exist until a controller first trips. Logging this at WARNING
            # would put a line on every scrape of a healthy pre-trip store and
            # train operators to ignore the level.
            _log.debug(
                "canary_exporter.table_absent",
                extra={"event": "canary_exporter.table_absent", "exc_str": str(exc)},
            )
            return [], False
        _log.warning(
            "canary_exporter.read_failed",
            extra={
                "event": "canary_exporter.read_failed",
                "exc_class": type(exc).__name__,
                "exc_str": str(exc),
            },
        )
        return [], False
    except sqlite3.Error as exc:
        _log.warning(
            "canary_exporter.read_failed",
            extra={
                "event": "canary_exporter.read_failed",
                "exc_class": type(exc).__name__,
                "exc_str": str(exc),
            },
        )
        return [], False


def cached_canary_snapshot(path: str | os.PathLike[str] | None = None) -> CanarySnapshot:
    """Read-then-fill cache, mirroring the shadow/dual-read gauge paths.

    A 15s Prometheus scrape interval must not re-walk SQLite every time. The
    cache is keyed on nothing but time because the store path is process-wide
    configuration; tests that need a fresh read call
    :func:`reset_cache_for_tests` or :func:`collect_canary_snapshot` directly.
    """
    global _cache, _cache_at
    with _cache_lock:
        now = time.monotonic()
        if _cache is not None and (now - _cache_at) < _CACHE_TTL_SECONDS:
            return _cache
        fresh = collect_canary_snapshot(path)
        _cache = fresh
        _cache_at = time.monotonic()
        return fresh


def reset_cache_for_tests() -> None:
    """Drop the in-process cache. Test-only — never call from production code."""
    global _cache, _cache_at
    with _cache_lock:
        _cache = None
        _cache_at = 0.0


def _cumulative_buckets(values: list[float]) -> list[tuple[str, float]]:
    """Cumulative ``(le, count)`` pairs, the shape a Prometheus histogram exposes.

    Each bucket counts every observation at or below its bound, so the counts
    are non-decreasing and the final ``+Inf`` bucket equals the total. Building
    this by hand rather than calling ``Histogram.observe`` is deliberate: the
    values live in SQLite and are re-derived every scrape, so a stateful
    collector would double-count on the second read.
    """
    pairs: list[tuple[str, float]] = []
    for bound in CANARY_DURATION_BUCKETS_MS:
        pairs.append((str(bound), float(sum(1 for v in values if v <= bound))))
    pairs.append(("+Inf", float(len(values))))
    return pairs


class CanaryExporter:
    """Prometheus collector rendering T1-T5 from a :class:`CanarySnapshot`.

    Registered into the per-scrape ``CollectorRegistry`` that
    ``parallax.server.routes.metrics._build_payload`` builds, so the values are
    read from the durable store at scrape time rather than accumulated in
    process memory — which is the only way a server can report on events that
    happened in a different process.
    """

    def __init__(self, snapshot: CanarySnapshot) -> None:
        self._snapshot = snapshot

    def collect(self):  # noqa: ANN201 — prometheus_client's untyped collector protocol
        snapshot = self._snapshot

        events = CounterMetricFamily(
            "parallax_canary_events",
            "Canary events recorded in the durable OutcomeStore, by stage and business "
            "outcome. Denominator for T1, T2 and the T5 sample-size gate.",
            labels=["stage", "outcome"],
        )
        for stage, outcome in snapshot.event_cells:
            events.add_metric([stage, outcome], float(snapshot.events.get((stage, outcome), 0)))
        yield events

        errors = CounterMetricFamily(
            "parallax_canary_event_errors",
            f"Canary events whose audit-log response_status was >= {ERROR_STATUS_FLOOR}, "
            "by stage. T1 numerator; 4xx is excluded because a client error is not a "
            "reason to auto-roll-back.",
            labels=["stage"],
        )
        for stage in snapshot.stages:
            errors.add_metric([stage], float(snapshot.errors.get(stage, 0)))
        yield errors

        discrepancy = CounterMetricFamily(
            "parallax_canary_discrepancy",
            "Canary events recorded with outcome='discrepancy', by stage. T2 numerator.",
            labels=["stage"],
        )
        discrepancies = snapshot.discrepancies
        for stage in snapshot.stages:
            discrepancy.add_metric([stage], float(discrepancies.get(stage, 0)))
        yield discrepancy

        data_loss = CounterMetricFamily(
            "parallax_canary_data_loss_events",
            "Canary events recorded with outcome='data_loss', by stage. T4 is a "
            "no-hysteresis hard-rollback alert on this series.",
            labels=["stage"],
        )
        losses = snapshot.data_loss
        for stage in snapshot.stages:
            data_loss.add_metric([stage], float(losses.get(stage, 0)))
        yield data_loss

        duration = HistogramMetricFamily(
            "parallax_canary_request_duration_ms",
            "Canary request wall-clock duration in milliseconds, measured by the canary "
            "CLI and persisted to audit_log.latency_ms. T3 reads its p99.",
            labels=["stage"],
        )
        for stage in snapshot.stages:
            values = snapshot.durations_ms.get(stage, [])
            duration.add_metric([stage], _cumulative_buckets(values), sum_value=float(sum(values)))
        yield duration

        yield GaugeMetricFamily(
            "parallax_canary_rollback_state",
            "RollbackController state from the durable store: -1 unknown (no state ever "
            "recorded), 0 running, 1 tripped, 2 awaiting_ack.",
            value=snapshot.rollback_state,
        )

        yield GaugeMetricFamily(
            "parallax_canary_store_present",
            "1.0 iff the durable canary OUTCOME LEDGER (canary_outcomes) was readable "
            "this scrape; else 0.0 — including when the database file opened fine but "
            "holds no outcome table, as an audit-only drill run leaves it. Every "
            "parallax_canary_* series above is a placeholder zero when this is 0.",
            value=1.0 if snapshot.store_present else 0.0,
        )
