"""M3-T1.2 — Thread-safe SQLite gate for dual-read cross-thread access (US-011).

All dual-read sqlite access routes through ``SQLiteGate``.  Existing M1/M2
callers stay on raw ``sqlite3.Connection``; only the new dual-read code path
uses this abstraction.

Q4 (ralplan §10 lines 587-616) must-do implementation notes:
  - Single ``threading.Lock`` serialises all cross-thread access on a shared conn.
  - Cursor lifecycle MUST end inside the lock: ``fetch_all`` calls ``fetchall()``
    before releasing, so no cursor ever crosses the lock boundary (corruption risk).
  - WAL pragmas applied on first construction per connection; idempotent across
    subsequent ``SQLiteGate`` instances wrapping the same ``sqlite3.Connection``.

Connection registry (``_active_gate_by_conn_id``):
    Class-level dict mapping ``id(connection)`` to a ``weakref.ref`` of the
    wrapping ``SQLiteGate``.  This is GC-race-safe: when the wrapping gate is
    garbage-collected, the weakref returns None, so a new gate constructed on
    a new connection that happens to land at the same memory address will
    correctly re-apply WAL pragmas instead of falsely skipping them.
    Other modules can call :meth:`SQLiteGate.is_connection_gated` to detect
    whether a connection is currently wrapped by a live gate (used by
    ``parallax/router/crosswalk_backfill.py`` to refuse running its long-row
    scan on a connection that is also serving dual-read traffic).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import weakref
from typing import Any, ClassVar, Literal

import prometheus_client

from parallax.obs.log import get_logger
from parallax.obs.subsystem_readiness import SUBSYSTEM_SQLITE_GATE, mark_wired

__all__ = [
    "ZERO_EXPORT_LABEL_SETS",
    "SQLiteGate",
    "SQLiteGateMetrics",
    "prime_zero_series",
]

_log = get_logger("parallax.router.sqlite_gate")

# ---------------------------------------------------------------------------
# Prometheus helpers (re-import safe)
# ---------------------------------------------------------------------------

_LOCK_WAIT_BUCKETS = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]

_COMPONENT_LABEL_VALUES = frozenset({"ingest", "m2_shadow", "regular_query", "m3_dual_read"})
_OP_LABEL_VALUES = frozenset({"read", "write"})


def _get_or_create_histogram(
    name: str, doc: str, labelnames: list[str], buckets: list[float]
) -> prometheus_client.Histogram:
    """Return an existing Histogram or create a new one (re-import safe)."""
    try:
        return prometheus_client.Histogram(name, doc, labelnames, buckets=buckets)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[name + "_bucket"]  # type: ignore[return-value]


def _get_or_create_gauge(name: str, doc: str, labelnames: list[str]) -> prometheus_client.Gauge:
    """Return an existing Gauge or create a new one (re-import safe)."""
    try:
        return prometheus_client.Gauge(name, doc, labelnames)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


def _get_or_create_counter(name: str, doc: str, labelnames: list[str]) -> prometheus_client.Counter:
    """Return an existing Counter or create a new one (re-import safe).

    Looked up under ``name`` as passed — see the #106.4 note on
    ``parallax.router.circuit_breaker._get_or_create_counter``. The only caller
    passes ``parallax_sqlite_errors_total``, so the old ``name + "_total"`` key
    resolved to ``..._total_total`` and raised KeyError instead of returning the
    live collector.
    """
    try:
        return prometheus_client.Counter(name, doc, labelnames)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


_lock_wait_hist = _get_or_create_histogram(
    "parallax_sqlite_lock_wait_seconds",
    "Time spent waiting to acquire the SQLiteGate lock, by component and op.",
    ["component", "op"],
    _LOCK_WAIT_BUCKETS,
)

_lock_hold_hist = _get_or_create_histogram(
    "parallax_sqlite_lock_hold_seconds",
    "Time spent holding the SQLiteGate lock, by component and op.",
    ["component", "op"],
    _LOCK_WAIT_BUCKETS,
)

_queue_depth_gauge = _get_or_create_gauge(
    "parallax_sqlite_lock_queue_depth",
    "Number of threads currently waiting for the SQLiteGate lock (process-wide).",
    [],
)

_wal_size_gauge = _get_or_create_gauge(
    "parallax_sqlite_wal_size_bytes",
    "Approximate size of the SQLite WAL file in bytes (lazily sampled).",
    [],
)

_errors_counter = _get_or_create_counter(
    "parallax_sqlite_errors_total",
    "Count of sqlite errors by error class, component, and op.",
    ["code", "component", "op"],
)


# ---------------------------------------------------------------------------
# Zero-export priming (#106.2)
# ---------------------------------------------------------------------------
# Four of the five collectors above are selected by panels in
# grafana/dashboards/parallax-dual-read-observability.json, and SQLiteGate is
# instantiated nowhere under parallax/ — so those panels have been reading
# no-data since they shipped. Registration is not exposition, and for a labelled
# collector it is not even a series: the child only exists once ``.labels()``
# runs. See ``parallax.apex.router.prime_zero_series`` for the full argument and
# the one rule that picks these label values:
#
#     Prime exactly the label sets the shipped consumers select, leaving every
#     label they do not constrain EMPTY. A collector no consumer label-selects
#     gets a single all-empty series.
#
# Both lock histogram panels select ``component="m3_dual_read"``; ``op`` and
# ``code`` are grouped by, never equality-matched, so they are primed empty
# rather than with a guessed value.
#
# ``parallax_sqlite_wal_size_bytes`` is deliberately absent: no rule or panel
# selects it, so it has no consumer to un-break, and the #106 gate does not
# police series that nothing reads.
#
# parallax_subsystem_wired{subsystem="sqlite_gate"} reports 0.0 until a
# SQLiteGate is constructed, so these zeros can be told apart from measurements.
ZERO_EXPORT_LABEL_SETS: dict[str, tuple[dict[str, str], ...]] = {
    "parallax_sqlite_lock_wait_seconds": ({"component": "m3_dual_read", "op": ""},),
    "parallax_sqlite_lock_hold_seconds": ({"component": "m3_dual_read", "op": ""},),
    "parallax_sqlite_lock_queue_depth": ({},),
    "parallax_sqlite_errors": ({"code": "", "component": "", "op": ""},),
}

_ZERO_EXPORT_COLLECTORS: dict[str, Any] = {
    "parallax_sqlite_lock_wait_seconds": _lock_wait_hist,
    "parallax_sqlite_lock_hold_seconds": _lock_hold_hist,
    "parallax_sqlite_lock_queue_depth": _queue_depth_gauge,
    "parallax_sqlite_errors": _errors_counter,
}


def prime_zero_series() -> None:
    """Materialise every consumed SQLiteGate series at zero. Idempotent.

    ``.labels(...)`` creates the child at its zero value and is not an
    increment, so re-running this after real traffic has started changes
    nothing. ``_queue_depth_gauge`` takes no labels and already has its single
    series from construction; it is listed so the table stays the exhaustive
    statement of what this module zero-exports.
    """
    for name, label_sets in ZERO_EXPORT_LABEL_SETS.items():
        collector = _ZERO_EXPORT_COLLECTORS[name]
        for labels in label_sets:
            if labels:
                collector.labels(**labels)


class SQLiteGateMetrics:
    """Namespace exposing the module-level Prometheus collectors for inspection."""

    lock_wait = _lock_wait_hist
    lock_hold = _lock_hold_hist
    queue_depth = _queue_depth_gauge
    wal_size = _wal_size_gauge
    errors = _errors_counter


# ---------------------------------------------------------------------------
# Cancellable (used by start_background_checkpoint)
# ---------------------------------------------------------------------------


class _Cancellable:
    """Object returned by ``start_background_checkpoint``; caller calls ``.stop()``.

    ``stop()`` is fail-safe: it sets ``_stop_event`` and (if a thread reference
    was provided at construction) joins the daemon thread before returning so
    the caller can safely close the underlying ``sqlite3.Connection`` without
    racing the background checkpoint thread (which would SIGSEGV in C if the
    cursor tries to ``execute()`` against a closed connection).
    """

    # Default cap on how long stop() will wait for the daemon thread to exit
    # before returning anyway. 5s is generous vs. the typical 0.05–300s loop.
    _DEFAULT_JOIN_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        event: threading.Event,
        thread: threading.Thread | None = None,
    ) -> None:
        """Initialise with a stop event and optional daemon thread reference."""
        self._stop_event = event
        self._thread = thread

    def stop(self, *, join_timeout: float | None = None) -> None:
        """Signal the background checkpoint thread to exit and join it.

        Pass ``join_timeout=0`` to skip the join (legacy non-blocking
        behaviour); ``None`` uses the default 5 s cap.
        """
        self._stop_event.set()
        if self._thread is None:
            return
        timeout = self._DEFAULT_JOIN_TIMEOUT_SECONDS if join_timeout is None else join_timeout
        if timeout > 0:
            self._thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# SQLiteGate
# ---------------------------------------------------------------------------


class SQLiteGate:
    """Thread-safe wrapper around a ``sqlite3.Connection`` for dual-read access.

    Usage::

        gate = SQLiteGate(conn, component="m3_dual_read")
        rows = gate.fetch_all("SELECT ...", (param,))
        row  = gate.fetch_one("SELECT ... LIMIT 1", (param,))
        gate.execute("UPDATE ...", (param,))
        gate.executemany("INSERT ...", batch)

    component label values: ``ingest`` | ``m2_shadow`` | ``regular_query`` | ``m3_dual_read``
    """

    # id(connection) -> weakref to the SQLiteGate currently wrapping it. When
    # the gate is GC'd, the weakref returns None, so a fresh gate on a
    # connection at the same memory address (post-GC reuse) re-applies WAL
    # pragmas instead of falsely skipping them. See module docstring.
    _active_gate_by_conn_id: ClassVar[dict[int, weakref.ref[SQLiteGate]]] = {}
    _registry_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        component: str = "m3_dual_read",
    ) -> None:
        """Wrap *connection* with thread-safe locking and apply WAL pragmas.

        Args:
            connection: The ``sqlite3.Connection`` to wrap.
            component: Label for Prometheus metrics; must be one of
                ``ingest``, ``m2_shadow``, ``regular_query``, ``m3_dual_read``.
        """
        if component not in _COMPONENT_LABEL_VALUES:
            raise ValueError(f"component={component!r} not in {sorted(_COMPONENT_LABEL_VALUES)}")
        self._conn = connection
        self._component = component
        self._lock = threading.Lock()
        # #106.2 — from here on this module's zero-exported series are real
        # measurements. Marked in the constructor so the readiness gauge cannot
        # claim the gate is live when nothing built one.
        mark_wired(SUBSYSTEM_SQLITE_GATE)
        self._register_and_apply_pragmas()

    # ------------------------------------------------------------------
    # Class-level registry helpers
    # ------------------------------------------------------------------

    @classmethod
    def _prune_dead_refs_locked(cls) -> None:
        """Drop entries whose weakref has expired. Caller MUST hold ``_registry_lock``."""
        dead = [cid for cid, ref in cls._active_gate_by_conn_id.items() if ref() is None]
        for cid in dead:
            del cls._active_gate_by_conn_id[cid]

    @classmethod
    def is_connection_gated(cls, conn: sqlite3.Connection) -> bool:
        """Return True if any live ``SQLiteGate`` is currently wrapping ``conn``.

        Used by ``parallax/router/crosswalk_backfill.py`` to refuse the long-row
        scan on a connection that is also serving dual-read traffic — running
        them concurrently violates the cross-thread sqlite invariant and risks
        SIGSEGV in the C extension (which is exactly the bug ``SQLiteGate``
        exists to prevent).
        """
        with cls._registry_lock:
            cls._prune_dead_refs_locked()
            ref = cls._active_gate_by_conn_id.get(id(conn))
            return ref is not None and ref() is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register_and_apply_pragmas(self) -> None:
        """Register self in the gate registry and apply WAL pragmas if needed.

        Pragmas are applied at most once per live wrapping gate per connection.
        If a previous gate on this connection has been GC'd, its weakref will
        be dead and we re-apply pragmas (defensive: pragmas are idempotent).
        """
        cid = id(self._conn)
        with SQLiteGate._registry_lock:
            SQLiteGate._prune_dead_refs_locked()
            existing = SQLiteGate._active_gate_by_conn_id.get(cid)
            need_apply = existing is None or existing() is None
            if need_apply:
                cur = self._conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA wal_autocheckpoint=0")
                cur.close()
            SQLiteGate._active_gate_by_conn_id[cid] = weakref.ref(self)

    def _db_file(self) -> str | None:
        """Return the database file path from ``PRAGMA database_list``, or None.

        Cursor lifecycle is held inside ``self._lock`` per the Q4 invariant —
        sqlite3 connections are not thread-safe, so a pragma cursor that
        crosses the lock boundary can race a parallel SELECT in another
        thread and raise ProgrammingError. Mirrors the pattern in
        :meth:`_execute_op` and :meth:`start_background_checkpoint`.

        Narrowed except: only ``sqlite3.Error`` and ``OSError`` are swallowed.
        Anything else (e.g. KeyboardInterrupt, SystemExit, or a TypeError
        from a corrupt PRAGMA row shape) propagates so a real bug does not
        silently disable WAL-size telemetry.
        """
        try:
            with self._lock:
                cur = self._conn.cursor()
                cur.execute("PRAGMA database_list")
                rows = cur.fetchall()
                cur.close()
            for row in rows:
                # (seq, name, file) — we want the 'main' database file
                if row[1] == "main" and row[2]:
                    return row[2]
        except (sqlite3.Error, OSError):
            pass
        return None

    def _sample_wal_size(self) -> None:
        """Update the WAL size gauge lazily (cheap os.path.getsize).

        Narrowed except: only a missing WAL file (which is the expected
        case for in-memory DBs and pre-first-write file DBs) is swallowed.
        Permission denied, ENOSPC, and other ``OSError`` subclasses
        propagate so a real I/O problem is not hidden.
        """
        db_file = self._db_file()
        if db_file:
            wal_path = db_file + "-wal"
            try:
                size = os.path.getsize(wal_path)
                _wal_size_gauge.set(size)
            except FileNotFoundError:
                pass  # WAL file may not exist for in-memory or pre-first-write DBs

    def _execute_op(
        self,
        op: Literal["read", "write"],
        sql: str,
        params: tuple[Any, ...] | list[tuple[Any, ...]] | None,
        *,
        many: bool = False,
        one: bool = False,
    ) -> list[Any]:
        """Core lock-guarded execution helper.

        Returns:
          - fetch_all path: list of rows
          - fetch_one path: list with at most one row
          - write path: empty list
        """
        # Signal that we are waiting for the lock (process-wide queue depth).
        _queue_depth_gauge.inc()
        wait_start = time.perf_counter()
        queue_decremented = False
        try:
            with self._lock:
                wait_elapsed = time.perf_counter() - wait_start
                # Order matters: set the flag BEFORE dec() so a failure inside
                # dec() (e.g. a Prometheus client internal error) does not let
                # the outer ``finally`` double-decrement.
                queue_decremented = True
                _queue_depth_gauge.dec()
                _lock_wait_hist.labels(component=self._component, op=op).observe(wait_elapsed)

                hold_start = time.perf_counter()
                try:
                    cur = self._conn.cursor()
                    if many:
                        cur.executemany(sql, params)  # type: ignore[arg-type]
                        result: list[Any] = []
                    elif one:
                        cur.execute(sql, params or ())
                        row = cur.fetchone()
                        result = [row] if row is not None else []
                    elif op == "read":
                        cur.execute(sql, params or ())
                        # Materialise BEFORE releasing lock — Q4 must-do patch.
                        result = cur.fetchall()
                    else:
                        cur.execute(sql, params or ())
                        result = []
                    cur.close()
                except sqlite3.Error as exc:
                    _errors_counter.labels(
                        code=type(exc).__name__,
                        component=self._component,
                        op=op,
                    ).inc()
                    raise
                finally:
                    hold_elapsed = time.perf_counter() - hold_start
                    _lock_hold_hist.labels(component=self._component, op=op).observe(hold_elapsed)
        finally:
            # Queue depth gauge MUST decrement on every path including
            # pre-lock failures (e.g. a faulty Lock subclass raising on
            # __enter__).  Without this, the gauge over-counts permanently.
            if not queue_decremented:
                _queue_depth_gauge.dec()

        if op == "read":
            self._sample_wal_size()
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_all(self, sql: str, params: tuple[Any, ...] | None = None) -> list[Any]:
        """Execute SELECT; return all rows as a list (materialised inside the lock)."""
        return self._execute_op("read", sql, params)

    def fetch_one(self, sql: str, params: tuple[Any, ...] | None = None) -> Any | None:
        """Execute SELECT LIMIT 1; return the first row or None (materialised inside the lock)."""
        rows = self._execute_op("read", sql, params, one=True)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        """Execute a single write statement inside the lock."""
        self._execute_op("write", sql, params)

    def executemany(self, sql: str, batch: list[tuple[Any, ...]]) -> None:
        """Execute a batched write statement inside the lock."""
        self._execute_op("write", sql, batch, many=True)

    def start_background_checkpoint(self, *, interval_seconds: float = 300.0) -> _Cancellable:
        """Spawn a daemon thread running ``PRAGMA wal_checkpoint(PASSIVE)`` every N seconds.

        The returned ``Cancellable`` has a ``.stop()`` method.  The checkpoint
        thread is a daemon so it does not prevent process exit if ``stop()`` is
        not called.

        Auto-checkpoint is disabled on construction (``wal_autocheckpoint=0``).
        Callers that need periodic checkpointing should call this method explicitly.
        """
        stop_event = threading.Event()

        def _run() -> None:
            """Background loop that runs ``PRAGMA wal_checkpoint(PASSIVE)``."""
            while not stop_event.wait(timeout=interval_seconds):
                try:
                    with self._lock:
                        cur = self._conn.cursor()
                        cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
                        cur.close()
                except Exception as exc:  # noqa: BLE001 — daemon must survive
                    # Bump Prometheus counter so persistent checkpoint failures
                    # (disk full, closed conn) are alertable.  The WARNING log
                    # alone produces noise without a metric signal.
                    try:
                        _errors_counter.labels(
                            code="checkpoint_failed",
                            component=self._component,
                            op="write",
                        ).inc()
                    except Exception:  # noqa: BLE001 — observability must never crash daemon
                        pass
                    _log.warning(
                        "wal_checkpoint_failed",
                        extra={"event": "wal_checkpoint_failed", "error": str(exc)},
                    )

        t = threading.Thread(target=_run, daemon=True, name="sqlite-gate-checkpoint")
        t.start()
        return _Cancellable(stop_event, thread=t)
