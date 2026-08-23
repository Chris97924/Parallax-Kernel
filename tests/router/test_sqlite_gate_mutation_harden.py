"""Mutation-hardening for ``parallax.router.sqlite_gate`` (land-20260823 w4 S1).

Additive companion to ``test_sqlite_gate.py``. Every test below exists because a
semantic mutant of the module SURVIVED the pre-existing suite
(``test_sqlite_gate.py``, ``test_metric_reregistration_106.py``,
``test_crosswalk_backfill.py``, ``test_dual_read_router.py``,
``test_zero_export_apex_sqlite_106.py``). 27 mutants were applied one at a time;
8 died against the existing suite and 19 walked through it.

What the existing suite is blind to, and why
--------------------------------------------

  * **The idempotence test re-implements the thing it is testing.**
    ``test_pragmas_not_reapplied_on_second_gate`` builds a ``TrackingGate``
    subclass that *overrides* ``_register_and_apply_pragmas`` with its own copy
    of the branch, then asserts that copy took the "skipped" path. The real
    method never runs, so ``need_apply = existing is None or existing() is None``
    can be replaced by a flat ``need_apply = True`` — pragmas re-applied on every
    gate — and the suite stays green. The tests here observe the *connection*
    instead, via ``sqlite3.Connection.set_trace_callback``, so what is asserted
    is the SQL the production code really issued.

  * **Every metric label is only ever observed at one value.** The component
    label is exercised as ``m3_dual_read`` (and once as ``ingest``), and the op
    label only as ``read``. So ``op=op`` -> ``op="read"`` and
    ``component=self._component`` -> ``component="m3_dual_read"`` are all
    invisible: the series the assertion reads is the same one the hardcoded
    mutant writes to. The tests below drive each histogram and the error counter
    from a component *no other test uses* and assert the neighbouring series did
    NOT move.

  * **The WAL gauge is asserted to be a float, nothing more.**
    ``test_sample_wal_size_with_file_db`` ends at ``assert isinstance(val,
    float)``, which is true of the gauge's initial 0.0. That single weak
    assertion hides the whole ``_db_file`` -> ``_sample_wal_size`` chain: the
    ``main``/``temp`` selector, the ``row[2]`` path index, and the ``-wal``
    suffix. Here the gauge is seeded with a sentinel and then required to equal
    ``os.path.getsize(<db>-wal)`` exactly.

  * **Two constants are pinned to themselves.**
    ``test_cancellable_stop_default_join_timeout_calls_join`` asserts
    ``join_calls[0] == _Cancellable._DEFAULT_JOIN_TIMEOUT_SECONDS`` — move the
    constant and the expectation moves with it. Same shape for the histogram
    buckets and the checkpoint interval, which nothing reads at all.
    The tests below use LITERAL numbers for those values. That is deliberate and
    must stay that way: importing the constant to build the expectation is
    exactly what made the originals blind.

  * **The fail-safe paths are never taken.** ``_Cancellable.stop()`` is only
    called with a thread attached, so moving ``self._stop_event.set()`` below the
    ``thread is None`` early return — which silently strands the daemon loop of
    every caller that built a ``_Cancellable`` without a thread — survives.
    Likewise the ``queue_decremented`` flag ordering, whose entire purpose is a
    failure *inside* ``dec()``, and the checkpoint thread's error counter.
"""

from __future__ import annotations

import gc
import inspect
import os
import sqlite3
import threading
import time
import weakref
from typing import Any

import pytest

import parallax.router.sqlite_gate as _gate_mod
from parallax.router.sqlite_gate import (
    _COMPONENT_LABEL_VALUES,
    _LOCK_WAIT_BUCKETS,
    SQLiteGate,
    SQLiteGateMetrics,
    _Cancellable,
)

# A value the real WAL-size sampler can never produce, so "the gauge did not
# move" and "the gauge was set to a real size" are distinguishable.
_WAL_SENTINEL = -12345.0


# ---------------------------------------------------------------------------
# Helpers (deliberately local: this file must not inherit the originals' habits)
# ---------------------------------------------------------------------------


def _hist_count(labeled_hist: Any) -> float:
    """Return the ``_count`` sample of a labelled Histogram child."""
    for metric in labeled_hist.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                return sample.value
    return 0.0


def _hist_bucket_bounds(labeled_hist: Any) -> list[str]:
    """Return the ``le`` bounds actually exported by a labelled Histogram child."""
    bounds: list[str] = []
    for metric in labeled_hist.collect():
        for sample in metric.samples:
            if sample.name.endswith("_bucket"):
                bounds.append(sample.labels["le"])
    return bounds


def _counter_value(labeled_counter: Any) -> float:
    """Return the ``_total`` sample of a labelled Counter child."""
    for metric in labeled_counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                return sample.value
    return 0.0


def _gauge_value(gauge: Any) -> float:
    """Return the current value of an unlabelled Gauge."""
    for metric in gauge.collect():
        for sample in metric.samples:
            if not sample.name.endswith("_created"):
                return sample.value
    return 0.0


@pytest.fixture()
def mem_conn():
    """Fresh in-memory connection, de-registered from the gate registry."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    SQLiteGate._active_gate_by_conn_id.pop(id(conn), None)
    yield conn
    SQLiteGate._active_gate_by_conn_id.pop(id(conn), None)
    conn.close()


@pytest.fixture()
def file_db(tmp_path):
    """``(db_path, connection)`` for a file-backed DB — WAL needs a real file."""
    db_path = tmp_path / "harden.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    SQLiteGate._active_gate_by_conn_id.pop(id(conn), None)
    yield db_path, conn
    SQLiteGate._active_gate_by_conn_id.pop(id(conn), None)
    conn.set_trace_callback(None)
    conn.close()


# ---------------------------------------------------------------------------
# Constants are contract, not implementation detail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_component_allowlist_is_the_four_shipped_call_sites() -> None:
    """All four component labels are contract, and only two are ever exercised.

    ``m2_shadow`` and ``regular_query`` appear in the class docstring as legal
    values but no test constructs a gate with either, so narrowing the frozenset
    to the two the suite happens to use is invisible. Pinned as a literal set
    because the constructor's guard is built from this same constant — asserting
    "the guard accepts everything in the constant" would be circular.
    """
    assert _COMPONENT_LABEL_VALUES == frozenset(
        {"ingest", "m2_shadow", "regular_query", "m3_dual_read"}
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "component", ["ingest", "m2_shadow", "regular_query", "m3_dual_read"]
)
def test_every_documented_component_really_constructs(component: str) -> None:
    """Each documented label must survive the constructor's allowlist check."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        SQLiteGate._active_gate_by_conn_id.pop(id(conn), None)
        gate = SQLiteGate(conn, component=component)
        assert gate._component == component
    finally:
        SQLiteGate._active_gate_by_conn_id.pop(id(conn), None)
        conn.close()


@pytest.mark.unit
def test_the_default_component_is_m3_dual_read(mem_conn) -> None:
    """Omitting ``component`` must label the metrics ``m3_dual_read``.

    Every existing test passes ``component=`` explicitly, so the default in the
    signature is unobserved and can be changed to any other allowlisted value.
    The default is the load-bearing one: it is what the dual-read call path gets
    and what both Grafana lock panels equality-match on.
    """
    mem_conn.execute("CREATE TABLE t (v INTEGER)")
    mem_conn.execute("INSERT INTO t VALUES (1)")
    mem_conn.commit()

    gate = SQLiteGate(mem_conn)  # no component= on purpose

    dual = SQLiteGateMetrics.lock_wait.labels(component="m3_dual_read", op="read")
    ingest = SQLiteGateMetrics.lock_wait.labels(component="ingest", op="read")
    before_dual, before_ingest = _hist_count(dual), _hist_count(ingest)

    gate.fetch_all("SELECT v FROM t")

    assert _hist_count(dual) == before_dual + 1, "default gate must report as m3_dual_read"
    assert _hist_count(ingest) == before_ingest, "no other component series may move"


@pytest.mark.unit
def test_the_lock_latency_buckets_are_the_shipped_millisecond_to_five_second_ladder(
    mem_conn,
) -> None:
    """The bucket ladder is a dashboard contract; nothing else reads it.

    ``histogram_quantile`` over these series is only as good as the boundaries,
    and no existing assertion touches them, so the top bound can be moved from
    5 s to 50 s without a red test. Both halves are pinned with literals: the
    constant itself, and the ``le`` labels the collector actually exports (which
    proves the constant is really what was wired into the Histogram).
    """
    assert _LOCK_WAIT_BUCKETS == [
        0.001,
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
    ]

    gate = SQLiteGate(mem_conn, component="m3_dual_read")
    assert gate is not None
    child = SQLiteGateMetrics.lock_wait.labels(component="m3_dual_read", op="read")
    assert _hist_bucket_bounds(child) == [
        "0.001",
        "0.005",
        "0.01",
        "0.025",
        "0.05",
        "0.1",
        "0.25",
        "0.5",
        "1.0",
        "2.5",
        "5.0",
        "+Inf",
    ]


@pytest.mark.unit
def test_the_default_checkpoint_interval_is_five_minutes() -> None:
    """``interval_seconds`` defaults to 300 s and no test ever takes the default.

    Both existing checkpoint tests pass ``interval_seconds=0.05`` explicitly, so
    the shipped cadence — the one a caller who just says
    ``gate.start_background_checkpoint()`` gets, with ``wal_autocheckpoint=0``
    meaning nothing else will truncate the WAL — is unobserved.
    """
    default = inspect.signature(SQLiteGate.start_background_checkpoint).parameters[
        "interval_seconds"
    ].default
    assert default == 300.0


# ---------------------------------------------------------------------------
# _Cancellable — the fail-safe paths
# ---------------------------------------------------------------------------


class _RecordingThread:
    """Stand-in for the daemon thread that records its ``join`` calls."""

    def __init__(self) -> None:
        self.join_calls: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)


@pytest.mark.unit
def test_the_default_join_timeout_is_five_seconds() -> None:
    """The 5 s cap is pinned as a literal, not as the constant it comes from.

    ``test_cancellable_stop_default_join_timeout_calls_join`` asserts
    ``join_calls[0] == _Cancellable._DEFAULT_JOIN_TIMEOUT_SECONDS``, which is
    true for every possible value of that constant. The number matters: it is
    how long ``stop()`` will block a caller that is about to ``close()`` the
    underlying connection, and the docstring sells it as "generous vs. the
    typical 0.05-300 s loop".
    """
    assert _Cancellable._DEFAULT_JOIN_TIMEOUT_SECONDS == 5.0

    fake_thread = _RecordingThread()
    _Cancellable(threading.Event(), thread=fake_thread).stop()  # type: ignore[arg-type]

    assert fake_thread.join_calls == [5.0]


@pytest.mark.unit
def test_stop_signals_the_event_even_with_no_thread_to_join() -> None:
    """``stop()`` must set the event before it can early-return on ``thread=None``.

    Both existing ``_Cancellable`` tests attach a thread, so an ordering change
    that moves ``self._stop_event.set()`` below the ``if self._thread is None:
    return`` guard survives. That mutant is a live-lock: a ``_Cancellable`` built
    without a thread reference would signal nothing and its daemon loop would
    run until process exit. The class docstring calls ``stop()`` fail-safe;
    this is the assertion that makes that word mean something.
    """
    stop_event = threading.Event()

    _Cancellable(stop_event).stop()

    assert stop_event.is_set(), "stop() must signal the loop even with thread=None"


# ---------------------------------------------------------------------------
# WAL-size sampling: _db_file -> _sample_wal_size
# ---------------------------------------------------------------------------


def _seed_wal(db_path, conn) -> int:
    """Write enough rows to force a non-empty WAL, and return its size."""
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(300)])
    conn.commit()
    wal_path = str(db_path) + "-wal"
    assert os.path.exists(wal_path), "WAL mode must have produced a -wal sidecar"
    size = os.path.getsize(wal_path)
    assert size > 0, "the -wal file must hold the uncheckpointed frames"
    return size


@pytest.mark.unit
def test_the_wal_gauge_reports_the_main_database_wal_file_size(file_db) -> None:
    """The gauge must equal ``getsize(<main db>-wal)`` exactly, not merely be a float.

    ``test_sample_wal_size_with_file_db`` stops at ``isinstance(val, float)``,
    which the gauge's untouched 0.0 satisfies, so the whole lookup chain is
    unobserved. Three separate mutants walk through it: selecting the ``temp``
    row instead of ``main`` from ``PRAGMA database_list``, returning ``row[1]``
    (the schema name) instead of ``row[2]`` (the file path), and measuring the
    ``-shm`` sidecar instead of ``-wal``. Each leaves the gauge at whatever it
    already held, which is why this test seeds a sentinel first.
    """
    db_path, conn = file_db
    gate = SQLiteGate(conn, component="m3_dual_read")
    wal_size = _seed_wal(db_path, conn)

    shm_size = os.path.getsize(str(db_path) + "-shm")
    assert wal_size != shm_size, (
        "precondition: the two sidecars must differ in size, otherwise this test "
        "cannot tell -wal from -shm"
    )

    SQLiteGateMetrics.wal_size.set(_WAL_SENTINEL)
    gate.fetch_all("SELECT n FROM t")

    assert _gauge_value(SQLiteGateMetrics.wal_size) == float(wal_size)


@pytest.mark.unit
def test_writes_do_not_sample_the_wal_gauge(file_db) -> None:
    """Only the read path samples the WAL size; the ``op == "read"`` guard is real.

    Nothing observes that guard today, so dropping it — sampling on every write
    as well — is free. It is not free in production: ``_sample_wal_size`` runs
    ``PRAGMA database_list`` under the gate lock plus an ``os.path.getsize``,
    which is exactly the per-write overhead the guard exists to avoid.
    """
    db_path, conn = file_db
    gate = SQLiteGate(conn, component="m3_dual_read")
    _seed_wal(db_path, conn)

    SQLiteGateMetrics.wal_size.set(_WAL_SENTINEL)
    gate.execute("INSERT INTO t VALUES (9999)")

    assert _gauge_value(SQLiteGateMetrics.wal_size) == _WAL_SENTINEL, (
        "a write must leave the WAL gauge untouched"
    )


# ---------------------------------------------------------------------------
# Metric labels: every one of them is only ever seen at a single value
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_lock_wait_histogram_carries_the_write_op_label(mem_conn) -> None:
    """A write must land on ``op="write"``, not on the read series.

    Every existing lock_wait assertion reads the ``op="read"`` child, so
    hardcoding ``op="read"`` at the observation site is undetectable — the
    mutant writes to the very series the assertions sample. The negative half
    (``read`` did not move) is what makes this test specific.
    """
    mem_conn.execute("CREATE TABLE t (v INTEGER)")
    mem_conn.commit()
    gate = SQLiteGate(mem_conn, component="m2_shadow")

    write_child = SQLiteGateMetrics.lock_wait.labels(component="m2_shadow", op="write")
    read_child = SQLiteGateMetrics.lock_wait.labels(component="m2_shadow", op="read")
    before_write, before_read = _hist_count(write_child), _hist_count(read_child)

    gate.execute("INSERT INTO t VALUES (1)")

    assert _hist_count(write_child) == before_write + 1
    assert _hist_count(read_child) == before_read, "a write must not observe the read series"


@pytest.mark.unit
def test_the_lock_hold_histogram_carries_the_callers_component_label(mem_conn) -> None:
    """lock_hold must be labelled with the gate's own component.

    ``test_component_label_isolation`` proves per-component isolation for
    lock_wait only; lock_hold is asserted exclusively at
    ``component="m3_dual_read"``, so pinning that value at the observation site
    survives. A gate built as ``regular_query`` would then silently report its
    hold times as dual-read traffic.
    """
    mem_conn.execute("CREATE TABLE t (v INTEGER)")
    mem_conn.execute("INSERT INTO t VALUES (1)")
    mem_conn.commit()
    gate = SQLiteGate(mem_conn, component="regular_query")

    own = SQLiteGateMetrics.lock_hold.labels(component="regular_query", op="read")
    dual = SQLiteGateMetrics.lock_hold.labels(component="m3_dual_read", op="read")
    before_own, before_dual = _hist_count(own), _hist_count(dual)

    gate.fetch_all("SELECT v FROM t")

    assert _hist_count(own) == before_own + 1
    assert _hist_count(dual) == before_dual, "another component's series must not move"


@pytest.mark.unit
def test_the_error_counter_carries_the_callers_component_label(mem_conn) -> None:
    """A sqlite error must be attributed to the gate that raised it.

    Both existing error-counter tests use ``component="m3_dual_read"``, so
    hardcoding that label keeps them green while making every non-dual-read
    gate's failures land on the dual-read series — the one the #106 alert rules
    read.
    """
    gate = SQLiteGate(mem_conn, component="m2_shadow")

    own = SQLiteGateMetrics.errors.labels(
        code="OperationalError", component="m2_shadow", op="read"
    )
    dual = SQLiteGateMetrics.errors.labels(
        code="OperationalError", component="m3_dual_read", op="read"
    )
    before_own, before_dual = _counter_value(own), _counter_value(dual)

    with pytest.raises(sqlite3.OperationalError):
        gate.fetch_all("SELECT * FROM table_that_does_not_exist_harden")

    assert _counter_value(own) == before_own + 1
    assert _counter_value(dual) == before_dual, "another component's series must not move"


# ---------------------------------------------------------------------------
# The queue-depth flag ordering (only observable when dec() itself fails)
# ---------------------------------------------------------------------------


class _GaugeThatFailsItsFirstDec:
    """Gauge stand-in whose first ``dec()`` raises, recording every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._failures_left = 1

    def inc(self) -> None:
        self.calls.append("inc")

    def dec(self) -> None:
        self.calls.append("dec")
        if self._failures_left:
            self._failures_left -= 1
            raise RuntimeError("prometheus client blew up inside dec()")


@pytest.mark.unit
def test_a_failing_dec_does_not_double_decrement_the_queue_gauge(
    mem_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``queue_decremented`` must be set BEFORE ``dec()``, not after.

    The source comment says so explicitly — "Order matters: set the flag BEFORE
    dec() so a failure inside dec() does not let the outer finally
    double-decrement" — and nothing tests it, because the only way to observe
    the ordering is to make ``dec()`` itself fail.
    ``test_metric_queue_depth_zero_after`` covers the happy path and stays green
    when the two lines are swapped.

    With the flag set first, a raising ``dec()`` is attempted exactly once. With
    the lines swapped, the outer ``finally`` sees a false flag and decrements a
    second time — a permanent -1 drift on a process-wide gauge for every failure.
    """
    mem_conn.execute("CREATE TABLE t (v INTEGER)")
    mem_conn.execute("INSERT INTO t VALUES (1)")
    mem_conn.commit()
    gate = SQLiteGate(mem_conn, component="m3_dual_read")

    fake_gauge = _GaugeThatFailsItsFirstDec()
    monkeypatch.setattr(_gate_mod, "_queue_depth_gauge", fake_gauge)

    with pytest.raises(RuntimeError, match="blew up inside dec"):
        gate.fetch_all("SELECT v FROM t")

    assert fake_gauge.calls == ["inc", "dec"], (
        "exactly one dec() attempt is allowed once the flag is set; "
        f"got {fake_gauge.calls}"
    )


# ---------------------------------------------------------------------------
# Pragma application, observed on the connection rather than on a copy of the code
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_first_gate_issues_exactly_the_three_wal_pragmas(file_db) -> None:
    """Counter-test: the statements the production path really sends.

    ``test_pragmas_applied_on_first_construction`` reads the resulting pragma
    *values* back, which is the right check for the values but says nothing
    about which statements were issued or in what order. Recorded here with a
    trace callback so the companion test below — "and the second gate sends
    none of them" — has a proven baseline.
    """
    _db_path, conn = file_db
    traced: list[str] = []
    conn.set_trace_callback(traced.append)

    gate = SQLiteGate(conn, component="m3_dual_read")
    conn.set_trace_callback(None)
    assert gate is not None

    assert traced == [
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA wal_autocheckpoint=0",
    ]


@pytest.mark.unit
def test_a_second_gate_on_a_live_connection_issues_no_pragmas_at_all(file_db) -> None:
    """The idempotence guard, observed on the connection instead of on a copy.

    ``test_pragmas_not_reapplied_on_second_gate`` asserts against a
    ``TrackingGate`` subclass that re-implements ``_register_and_apply_pragmas``
    in the test body; the production method is never called, so replacing its
    ``need_apply = existing is None or existing() is None`` with a flat
    ``need_apply = True`` leaves that test green. Re-applying
    ``PRAGMA journal_mode=WAL`` on a connection with an open read transaction
    raises, so this is not a cosmetic difference.

    The first gate is held alive for the whole test on purpose: the registry
    stores a weakref, and letting it die would legitimately re-arm the pragmas.
    """
    _db_path, conn = file_db
    first_gate = SQLiteGate(conn, component="m3_dual_read")

    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    second_gate = SQLiteGate(conn, component="ingest")
    conn.set_trace_callback(None)

    assert first_gate is not None, "the registry weakref must stay live"
    assert second_gate is not None
    assert traced == [], f"second gate must issue no SQL, got {traced}"


# ---------------------------------------------------------------------------
# The connection registry: pruning and liveness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_collected_gate_is_pruned_out_of_the_connection_registry(mem_conn) -> None:
    """``_prune_dead_refs_locked`` must really delete, not just be called.

    The registry is a process-lifetime class-level dict keyed by ``id(conn)``.
    Nothing today observes that entries ever leave it, so turning the prune into
    a no-op is invisible: both readers (``is_connection_gated`` and
    ``_register_and_apply_pragmas``) independently re-check the weakref, so
    behaviour is unchanged and only the leak remains — one dict entry per
    connection ever gated, for the life of the process.
    """
    other_conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        SQLiteGate._active_gate_by_conn_id.pop(id(other_conn), None)
        gate = SQLiteGate(mem_conn, component="m3_dual_read")
        conn_id = id(mem_conn)
        assert conn_id in SQLiteGate._active_gate_by_conn_id

        gate_ref = weakref.ref(gate)
        del gate
        gc.collect()
        assert gate_ref() is None, "precondition: the gate must actually be collected"

        # Any registry operation takes the lock and prunes first.
        SQLiteGate.is_connection_gated(other_conn)

        assert conn_id not in SQLiteGate._active_gate_by_conn_id, (
            "the dead entry must be pruned, not merely ignored"
        )
    finally:
        SQLiteGate._active_gate_by_conn_id.pop(id(other_conn), None)
        other_conn.close()


@pytest.mark.unit
def test_is_connection_gated_is_false_once_the_wrapping_gate_is_gone(mem_conn) -> None:
    """The contract ``crosswalk_backfill`` depends on, at both ends.

    ``test_crosswalk_backfill`` proves only the True direction (a live gate makes
    the long-row scan refuse). The False direction after collection is what lets
    a backfill run at all once dual-read traffic has stopped; if it stayed True
    forever the scan would be permanently refused on that connection.
    """
    assert SQLiteGate.is_connection_gated(mem_conn) is False

    gate = SQLiteGate(mem_conn, component="m3_dual_read")
    assert SQLiteGate.is_connection_gated(mem_conn) is True

    del gate
    gc.collect()

    assert SQLiteGate.is_connection_gated(mem_conn) is False


# ---------------------------------------------------------------------------
# The background checkpoint thread
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_background_thread_checkpoints_in_passive_mode(file_db) -> None:
    """The checkpoint mode is PASSIVE, and no existing test reads the statement.

    ``test_background_checkpoint_runs`` verifies a checkpoint fired — but through
    a ``TrackingGate`` subclass that re-implements the loop body, so the SQL it
    observes is the SQL the *test* wrote. PASSIVE is the whole point: TRUNCATE
    or FULL block on readers, and this thread holds the gate lock while it runs,
    so a stronger mode converts a background housekeeping tick into a stall of
    every dual-read query on the connection.
    """
    db_path, conn = file_db
    gate = SQLiteGate(conn, component="m3_dual_read")
    _seed_wal(db_path, conn)

    seen: list[str] = []
    fired = threading.Event()

    def _trace(statement: str) -> None:
        if "wal_checkpoint" in statement:
            seen.append(statement)
            fired.set()

    conn.set_trace_callback(_trace)
    cancellable = gate.start_background_checkpoint(interval_seconds=0.02)
    try:
        assert fired.wait(timeout=10.0), "no checkpoint statement was issued"
    finally:
        cancellable.stop()
        conn.set_trace_callback(None)

    assert seen[0] == "PRAGMA wal_checkpoint(PASSIVE)"


class _ConnectionWhoseCursorAlwaysFails:
    """Stand-in connection that fails the way a closed/full-disk one would."""

    def cursor(self) -> Any:
        raise sqlite3.OperationalError("disk I/O error")


@pytest.mark.unit
def test_a_failing_checkpoint_increments_the_checkpoint_failed_counter(mem_conn) -> None:
    """The alertable code label is ``checkpoint_failed`` and nothing asserts it.

    The daemon deliberately swallows its exception so the loop survives, so the
    Prometheus counter is the ONLY machine-readable signal that a connection has
    stopped checkpointing — the source comment says as much ("The WARNING log
    alone produces noise without a metric signal"). No test reaches this except
    branch at all, so both the label value and the increment itself are free to
    drift.
    """
    gate = SQLiteGate(mem_conn, component="m2_shadow")
    gate._conn = _ConnectionWhoseCursorAlwaysFails()  # type: ignore[assignment]

    counter = SQLiteGateMetrics.errors.labels(
        code="checkpoint_failed", component="m2_shadow", op="write"
    )
    before = _counter_value(counter)

    cancellable = gate.start_background_checkpoint(interval_seconds=0.02)
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and _counter_value(counter) <= before:
            time.sleep(0.02)
    finally:
        cancellable.stop()

    assert _counter_value(counter) > before, (
        'a failing checkpoint must increment errors_total{code="checkpoint_failed"}'
    )
