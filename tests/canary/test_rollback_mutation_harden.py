"""Mutation-hardening for ``parallax.canary.rollback`` (land-20260824 w5 S3).

Additive companion to ``test_rollback.py``. Every test below exists because a
semantic mutant of the module SURVIVED the pre-existing suite
(``tests/canary/test_rollback.py``, ``tests/canary/test_hardening.py``,
``tests/observability/test_canary_exporter_106.py``). 56 mutants were applied
one at a time to an otherwise pristine tree; 35 died against the existing suite
and 21 walked through it -- 20 needing a new killer, 1 provably equivalent.

Tally for this module: applied 56 / killed-by-new 20 / already-covered 35 /
equivalent-with-proof 1 / unaddressed 0.

The existing suite covers the state machine itself thoroughly: the trip paths
for all four triggers, the gate downgrade, the cooldown, the no-auto-recover
invariant and the ACK guards are all pinned. What survived clusters elsewhere:

  * **The durable projection is never read back.** ``_persist_state`` exists
    entirely so the server-side exporter can render a panel the in-process
    controller would otherwise never publish, and no test in the module's own
    suite queries the row. So the trip could be persisted without its
    ``tripped_by`` attribution, the ``awaiting_ack`` transition could lose it,
    and the return to ``running`` after an ACK could not be written at all --
    leaving the dashboard permanently showing a canary awaiting an
    acknowledgement that already happened. These tests read
    ``canary_rollback_state`` directly, the way the exporter does.

  * **Every controller is constructed with an audit log.** So the no-argument
    constructor -- the one a caller gets when they just want the state machine
    -- was never exercised, and a mutated derivation guard that crashes on it
    was invisible. So was one that silently DISCARDS an explicitly supplied
    ``state_store`` and rebuilds it from the audit log's path.

  * **The clock is always injected.** Every existing test passes a fake, so the
    default was free to move from ``monotonic`` to the wall clock -- which is
    the one substitution that breaks the cooldown, because an NTP step or a DST
    change would make a 30-minute hysteresis window expire early or never.

  * **``ts`` is never passed to ``observe_request``.** The parameter exists so a
    caller can backfill an observation at its real time; nothing ever supplies
    one, so the whole argument could be ignored and every backfilled sample
    would silently land at "now" -- in the window it should have aged out of.

  * **``observe_data_loss`` is only ever called one way.** Existing tests call
    it bare, so the default and the forwarding of an explicit count are
    interchangeable and a multi-record loss reports as a single event.

  * **The cooldown deadline is never sat on exactly.** ``>=`` could relax to
    ``>``, and the snapshot could report the module constant rather than the
    controller's configured cooldown, with every existing assertion holding.

  * **Only one breach is ever live at a time.** So "first breach in spec order
    wins" and "last breach wins" are indistinguishable -- the operator-facing
    attribution of WHICH trigger rolled the canary back was unpinned.

  * **The ACK payload is not read back, and neither is what it resets.** The
    existing ACK tests assert the audit row and the state transition. They do
    not assert that ``tripped_at`` is cleared, that the operator and timestamp
    land in the snapshot, that the timestamp is timezone-aware, that T1-T4 are
    reset (without which the canary re-trips on its next evaluation, on stale
    observations), or that T5 is deliberately NOT reset (without which the
    sample-size gate re-engages and blinds the controller right after a
    rollback).

One mutant is provably EQUIVALENT and is recorded as such rather than given a
contrived test: seeding ``_last_gate_evaluation`` with ``self._t5.evaluate(0.0)``
instead of ``self._t5.evaluate(self._clock())``. ``self._t5`` is constructed a
few lines earlier and nothing records into it before that call, so its deque is
empty; ``T5MinHitsGate.evaluate`` on an empty window evicts nothing and returns
``n = 0`` for every possible argument, producing a byte-identical
``TriggerEvaluation`` whatever timestamp it is handed.

Expected values are LITERALS throughout -- 1800.0, "T1-error-rate",
"T4-data-loss", "tripped", "awaiting_ack", "running".
"""

from __future__ import annotations

import datetime as _dt
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from parallax.canary.audit_log import AuditLog
from parallax.canary.rollback import (
    COOLDOWN_SECONDS,
    CanaryState,
    RollbackController,
)
from parallax.canary.rollback_state import RollbackStateStore

#: Comfortably above the T5 minimum so the sample-size gate is inactive.
ENOUGH_HITS = 100


class _Clock:
    """Mutable clock so the state machine can be driven deterministically."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture
def audit(tmp_path: pathlib.Path) -> Iterator[AuditLog]:
    log = AuditLog(tmp_path / "harden_rollback_audit.db")
    try:
        yield log
    finally:
        log.close()


@pytest.fixture
def state_db(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "harden_rollback_state.db"


def _saturate(controller: RollbackController, *, is_error: bool = False) -> None:
    """Push enough hits through T5 that the sample-size gate goes inactive."""
    for _ in range(ENOUGH_HITS):
        controller.observe_request(is_error=is_error, latency_ms=1.0)


def _persisted(db_path: pathlib.Path) -> tuple[str, str | None]:
    """Read the durable row the way the server-side exporter reads it."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT state, tripped_by FROM canary_rollback_state WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "no durable rollback-state row was ever written"
    return row[0], row[1]


def _drive_to_awaiting_ack(
    controller: RollbackController, clock: _Clock, *, cooldown: float
) -> None:
    _saturate(controller)
    controller.observe_data_loss()
    assert controller.evaluate(now=clock()).state is CanaryState.TRIPPED
    assert (
        controller.evaluate(now=clock() + cooldown).state is CanaryState.AWAITING_ACK
    )


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------


def test_cooldown_constant_is_thirty_minutes() -> None:
    """Criterion 1.15's hysteresis window, as a literal.

    Existing assertions compare against the constant itself, so it was free to
    move.
    """
    assert COOLDOWN_SECONDS == 1800.0


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------


def test_controller_constructs_with_no_arguments_and_uses_a_monotonic_clock() -> None:
    """The bare constructor must work, and must not time on the wall clock.

    Every existing test injects both an audit log and a clock, so neither the
    no-argument path nor the default clock was ever exercised. The wall clock
    is the one substitution that breaks hysteresis: an NTP step or a DST change
    moves it, and a 30-minute cooldown measured on it expires early or never.
    A monotonic reading is seconds-since-boot, so it is orders of magnitude
    below a Unix timestamp.
    """
    controller = RollbackController()
    _saturate(controller)
    controller.observe_data_loss()

    snapshot = controller.evaluate()
    assert snapshot.state is CanaryState.TRIPPED
    assert snapshot.tripped_at is not None
    assert 0.0 < snapshot.tripped_at < 1_000_000_000.0


def test_an_explicitly_supplied_state_store_is_not_replaced(
    audit: AuditLog, state_db: pathlib.Path
) -> None:
    """A caller-supplied store wins over the one derived from the audit log.

    The derivation is a convenience for callers who configured only an audit
    log. Applying it on top of an explicit store would send the durable state
    to a different SQLite file than the operator pointed at, and nothing else
    would notice.
    """
    store = RollbackStateStore(state_db)
    try:
        RollbackController(audit_log=audit, state_store=store)
        assert _persisted(state_db) == ("running", None)
    finally:
        store.close()


# ----------------------------------------------------------------------
# Observation API
# ----------------------------------------------------------------------


def test_observe_request_honours_an_explicit_timestamp() -> None:
    """``ts`` backfills an observation at its real time, not at "now".

    Nothing in the existing suite passes one, so the argument could be dropped
    entirely -- and a backfilled sample that should have aged out of the
    5-minute window would instead be counted as current.
    """
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock)

    # 1000 s in the past: outside T1's 300 s window.
    controller.observe_request(is_error=True, latency_ms=1.0, ts=0.0)
    stale = controller.evaluate(now=1_000.0)
    assert stale.last_evaluations["T1-error-rate"].observations == 0

    # 100 s in the past: inside it.
    controller.observe_request(is_error=True, latency_ms=1.0, ts=900.0)
    fresh = controller.evaluate(now=1_000.0)
    assert fresh.last_evaluations["T1-error-rate"].observations == 1


def test_observe_data_loss_defaults_to_one_event() -> None:
    """A bare call records exactly one loss, which is enough to trip T4."""
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock)
    _saturate(controller)

    controller.observe_data_loss()

    snapshot = controller.evaluate(now=1_000.0)
    assert snapshot.state is CanaryState.TRIPPED
    assert snapshot.tripped_by == "T4-data-loss"
    assert snapshot.last_evaluations["T4-data-loss"].metric_value == 1.0


def test_observe_data_loss_forwards_an_explicit_count() -> None:
    """Four lost events are four, not one.

    The count reaches the operator as the T4 metric value; collapsing it to 1
    understates the blast radius of the incident that caused the rollback.
    """
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock)
    _saturate(controller)

    controller.observe_data_loss(4)

    snapshot = controller.evaluate(now=1_000.0)
    assert snapshot.last_evaluations["T4-data-loss"].metric_value == 4.0
    assert snapshot.last_evaluations["T4-data-loss"].observations == 4


# ----------------------------------------------------------------------
# evaluate(): cooldown edge, snapshot completeness, breach attribution
# ----------------------------------------------------------------------


def test_cooldown_releases_exactly_on_the_deadline() -> None:
    """The cooldown has ELAPSED at t + cooldown, not one tick later.

    Existing hysteresis tests advance well past the deadline, so the boundary
    itself is unpinned. The snapshot must also echo the configured cooldown
    rather than the module default -- an operator running a shortened cooldown
    would otherwise be shown 30 minutes.
    """
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock, cooldown_seconds=100.0)
    _saturate(controller)
    controller.observe_data_loss()

    tripped = controller.evaluate(now=1_000.0)
    assert tripped.state is CanaryState.TRIPPED
    assert tripped.cooldown_seconds == 100.0

    assert controller.evaluate(now=1_099.0).state is CanaryState.TRIPPED
    assert controller.evaluate(now=1_100.0).state is CanaryState.AWAITING_ACK


def test_snapshot_carries_every_trigger_evaluation() -> None:
    """All four T1-T4 verdicts reach the snapshot, not just the first.

    The snapshot is what the CLI and dashboard render; a partial map hides
    whichever triggers are missing rather than showing them as passing.
    """
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock)
    _saturate(controller)

    snapshot = controller.evaluate(now=1_000.0)

    assert sorted(snapshot.last_evaluations) == [
        "T1-error-rate",
        "T2-discrepancy-rate",
        "T3-p99-latency",
        "T4-data-loss",
    ]


def test_the_first_breach_in_spec_order_is_the_one_recorded() -> None:
    """With T1 and T4 both breaching, the canary is attributed to T1.

    Existing tests only ever have a single trigger breaching, so "first in spec
    order" and "last in spec order" are indistinguishable -- and this field is
    the operator's answer to "what rolled us back".
    """
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock)
    _saturate(controller, is_error=True)  # T1: a 100% error rate
    controller.observe_data_loss()  # T4: a data-loss event

    snapshot = controller.evaluate(now=1_000.0)

    assert snapshot.state is CanaryState.TRIPPED
    assert snapshot.tripped_by == "T1-error-rate"


# ----------------------------------------------------------------------
# Durable projection
# ----------------------------------------------------------------------


def test_every_transition_is_mirrored_to_the_durable_store(
    state_db: pathlib.Path,
) -> None:
    """running -> tripped -> awaiting_ack -> running, with attribution.

    This row is the ONLY producer of the dashboard's rollback-state panel, and
    the module's own suite never reads it back. A missing write after the ACK
    leaves the panel showing a canary awaiting an acknowledgement that already
    happened; a missing ``tripped_by`` leaves it showing a rollback with no
    cause.
    """
    clock = _Clock(1_000.0)
    store = RollbackStateStore(state_db)
    try:
        controller = RollbackController(
            clock=clock, cooldown_seconds=100.0, state_store=store
        )
        assert _persisted(state_db) == ("running", None)

        _saturate(controller)
        controller.observe_data_loss()

        assert controller.evaluate(now=1_000.0).state is CanaryState.TRIPPED
        assert _persisted(state_db) == ("tripped", "T4-data-loss")

        assert controller.evaluate(now=1_100.0).state is CanaryState.AWAITING_ACK
        assert _persisted(state_db) == ("awaiting_ack", "T4-data-loss")

        assert controller.acknowledge(ack_by="alice") is True
        assert _persisted(state_db) == ("running", None)
    finally:
        store.close()


# ----------------------------------------------------------------------
# acknowledge()
# ----------------------------------------------------------------------


def test_acknowledge_records_the_operator_and_clears_the_trip() -> None:
    """The ACK payload lands in the snapshot and the trip fields are cleared.

    Existing ACK tests assert the audit row and the state transition; the
    snapshot fields an operator actually reads afterwards were unpinned, and a
    ``tripped_at`` left populated describes a rollback that is over.
    """
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock, cooldown_seconds=100.0)
    _drive_to_awaiting_ack(controller, clock, cooldown=100.0)

    assert (
        controller.acknowledge(ack_by="alice", ack_at="2026-01-01T00:00:00+00:00")
        is True
    )

    snapshot = controller.snapshot()
    assert snapshot.state is CanaryState.RUNNING
    assert snapshot.tripped_at is None
    assert snapshot.tripped_by is None
    assert snapshot.last_ack_by == "alice"
    assert snapshot.last_ack_at == "2026-01-01T00:00:00+00:00"


def test_acknowledge_timestamp_is_timezone_aware_utc() -> None:
    """A generated ACK timestamp carries UTC, not a naive local reading.

    The same string goes into the audit trail, and a naive local timestamp
    there is unresolvable after the fact -- two operators in different zones
    produce indistinguishable rows.
    """
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock, cooldown_seconds=100.0)
    _drive_to_awaiting_ack(controller, clock, cooldown=100.0)

    assert controller.acknowledge(ack_by="alice") is True

    recorded = controller.snapshot().last_ack_at
    assert recorded is not None
    parsed = _dt.datetime.fromisoformat(recorded)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == _dt.timedelta(0)


def test_acknowledge_resets_the_triggers_but_keeps_the_sample_gate() -> None:
    """T1-T4 are cleared by the ACK; T5 deliberately is not.

    Both halves matter and neither was pinned. Leaving T1-T4 loaded means the
    very next evaluation re-trips on the same stale observations, so the ACK
    cannot actually return the canary to service. Clearing T5 as well means the
    sample-size gate re-engages immediately after a rollback and downgrades
    every verdict to insufficient_data -- the controller goes blind exactly
    when it has just been told to resume.
    """
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock, cooldown_seconds=100.0)
    _saturate(controller, is_error=True)  # T1 breaches on a 100% error rate

    assert controller.evaluate(now=1_000.0).state is CanaryState.TRIPPED
    assert controller.evaluate(now=1_100.0).state is CanaryState.AWAITING_ACK
    assert controller.acknowledge(ack_by="alice") is True

    resumed = controller.evaluate(now=1_100.0)
    assert resumed.state is CanaryState.RUNNING
    assert resumed.last_evaluations["T1-error-rate"].observations == 0
    assert resumed.last_gate_evaluation.observations == ENOUGH_HITS


# ----------------------------------------------------------------------
# force_running()
# ----------------------------------------------------------------------


def test_force_running_returns_the_controller_to_running() -> None:
    """The test-only escape hatch really does land on RUNNING."""
    clock = _Clock(1_000.0)
    controller = RollbackController(clock=clock)
    _saturate(controller)
    controller.observe_data_loss()
    assert controller.evaluate(now=1_000.0).state is CanaryState.TRIPPED

    controller.force_running()

    assert controller.state is CanaryState.RUNNING
    snapshot = controller.snapshot()
    assert snapshot.tripped_at is None
    assert snapshot.tripped_by is None
