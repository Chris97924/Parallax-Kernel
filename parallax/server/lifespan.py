"""M3-T1.4 — Graceful drain lifespan handler (US-011).

Implements the rollback drain contract from ralplan §3 M3-T1.4:
on SIGTERM / SIGINT the server waits up to ``DRAIN_TIMEOUT_SECONDS`` (15 min)
for all in-flight requests to complete before the process exits.

Without this handler the rollback procedure "drain in-flight 15 min" is
paper — in-flight requests would crash on a ``DUAL_READ=false`` flip during
a live rollback.

Uses FastAPI's ``lifespan`` context-manager pattern (0.93+).  The deprecated
``@app.on_event("startup"/"shutdown")`` decorators are NOT used.

Important: the drain loop uses ``asyncio.sleep`` (not ``time.sleep``) so
other coroutines — including the last in-flight requests — can progress
while we poll.  Using ``time.sleep`` would block the event loop and make
it impossible for those requests to actually finish.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Final

from fastapi import FastAPI
from prometheus_client import Counter

from parallax.apex.audit_db import open_audit_db, resolve_audit_db_path
from parallax.router.inflight import get_inflight_count

__all__ = [
    "DRAIN_TIMEOUT_EVENT",
    "DRAIN_TIMEOUT_SECONDS",
    "DRAIN_POLL_INTERVAL_SECONDS",
    "drain_timeout_total",
    "parallax_lifespan",
]

_log = logging.getLogger("parallax.server.lifespan")

DRAIN_TIMEOUT_SECONDS: Final[float] = 900.0  # 15 minutes
DRAIN_POLL_INTERVAL_SECONDS: Final[float] = 0.5

#: Structured key on the drain-timeout log record — the one producer-side signal
#: for this event that survives the process (#106.3).
#:
#: ``drain_timeout_total`` cannot carry it: uvicorn closes the listening socket
#: before running lifespan shutdown, so the increment below happens when no
#: scrape can still reach this process, and ``DrainTimeoutDetected`` evaluates
#: against a series whose last sample predates the event it is meant to catch.
#: The log stream is what outlives the process, so the event gets a key an alert
#: can match exactly, instead of a prose fragment that stops matching the day
#: the sentence is reworded. Named here rather than inlined so the rule
#: annotation, the alert, and this producer can be pinned to one another by
#: ``tests/server/test_drain_timeout_durable_signal_106.py``.
DRAIN_TIMEOUT_EVENT: Final[str] = "drain_timeout"


# prometheus_client raises ValueError on duplicate registration.
try:
    drain_timeout_total: Counter = Counter(
        "parallax_drain_timeout_total",
        "Number of times the graceful-drain timeout fired before all "
        "in-flight requests completed.",
    )
except ValueError:
    import prometheus_client as _pc

    drain_timeout_total = _pc.REGISTRY._names_to_collectors["parallax_drain_timeout_total"]  # type: ignore[assignment]


async def _drain_inflight(
    *,
    timeout_seconds: float = DRAIN_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DRAIN_POLL_INTERVAL_SECONDS,
) -> None:
    """Poll until inflight count reaches 0 or *timeout_seconds* elapses.

    On timeout:
    - Increments ``drain_timeout_total`` Prometheus counter.
    - Logs a WARNING with the final inflight count.
    - Returns (lifespan exits regardless; we do not block forever).

    On clean drain:
    - Logs an INFO message with elapsed time.

    Parameters
    ----------
    timeout_seconds:
        Maximum seconds to wait.  Pass a small value (e.g. 0.2) in tests.
    poll_interval_seconds:
        How often to check the gauge.
    """
    start = time.monotonic()
    deadline = start + timeout_seconds

    while True:
        count = get_inflight_count()
        if count <= 0:
            elapsed = time.monotonic() - start
            _log.info("parallax.lifespan: drain complete in %.3fs (0 inflight)", elapsed)
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            final_count = get_inflight_count()
            drain_timeout_total.inc()
            # Sentence for a human reading the log, structured fields for
            # whatever alerts on it — and the key=value tail below repeats the
            # same fields IN THE MESSAGE ITSELF, not only in extra. Both
            # audiences need to find them in the same rendering: ``parallax
            # serve`` (parallax.cli._cmd_serve) hands uvicorn no custom
            # log_config, so under the canonical launcher this is a plain
            # logging.getLogger with the stdlib default formatter, which
            # renders only record.getMessage() and never touches extra — a
            # JSON sink is not guaranteed to be attached (codex #107 review).
            # The tail is what stays alert-matchable there; extra stays for
            # sinks that do parse it.
            _log.warning(
                "parallax.lifespan: drain timeout after %.1fs — %d request(s) still "
                "in flight; proceeding with shutdown "
                "(event=%s inflight_count=%d timeout_seconds=%.1f)",
                timeout_seconds,
                final_count,
                DRAIN_TIMEOUT_EVENT,
                final_count,
                timeout_seconds,
                extra={
                    "event": DRAIN_TIMEOUT_EVENT,
                    "inflight_count": final_count,
                    "timeout_seconds": timeout_seconds,
                },
            )
            return

        await asyncio.sleep(min(poll_interval_seconds, max(remaining, 0)))


@contextlib.asynccontextmanager
async def parallax_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan context manager.

    Startup: Apex M5 audit-db boot validation — resolve
    ``PARALLAX_AUDIT_DB_PATH``, run the spec §4 startup gates via
    :func:`open_audit_db` with ``validate=True``, then close the
    validation connection (the lifespan holds no runtime connection — the
    write path opens its own per-thread connections via
    :func:`parallax.apex.audit_db.get_thread_local_audit_conn`). The
    validated path is stashed on ``app.state.audit_db_path`` for the query
    route to hand to the thread-local provider.

    Shutdown: drain in-flight requests up to ``DRAIN_TIMEOUT_SECONDS``.
    """
    # Startup — Apex M5 audit-db boot validation (spec §4). A misconfigured
    # or unwritable audit path raises AuditDbConfigError here and the server
    # refuses to serve traffic. NOTE: uvicorn swallows a lifespan-startup
    # exception into a non-78 process exit; ``parallax serve`` (parallax.cli
    # ._cmd_serve) runs the same check as a preflight BEFORE uvicorn.run() so
    # the canonical launcher exits with the deterministic EX_CONFIG (78).
    audit_db_path = resolve_audit_db_path()
    # contextlib.closing guarantees the validation connection is released even
    # if a later line raises — the lifespan holds no runtime connection.
    with contextlib.closing(open_audit_db(audit_db_path, validate=True)):
        pass  # boot-time §4 gates run inside open_audit_db; nothing else to do
    app.state.audit_db_path = audit_db_path

    yield
    # Shutdown — drain
    await _drain_inflight(timeout_seconds=DRAIN_TIMEOUT_SECONDS)
