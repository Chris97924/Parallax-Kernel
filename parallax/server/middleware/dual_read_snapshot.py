"""M3-T1.4 — Per-request DUAL_READ flag-snapshot middleware (US-011).

Snapshots ``is_dual_read_enabled()`` exactly once at request entry and
stores the result in ``request.state.dual_read``.  Downstream route handlers
MUST read the flag exclusively via ``request.state.dual_read`` — they must
NEVER call ``os.environ`` or ``is_dual_read_enabled()`` mid-request, because
an operator can flip the env flag for a rollback at any moment.  The snapshot
freezes the flag for the duration of this request so in-flight work is
consistent.

The circuit breaker is consulted at the same snapshot moment: if it is
tripped the effective flag is forced to ``False`` regardless of the env var
(fail-closed, per Q8 DECIDED, ralplan §10 2026-04-27).

Caller contract
---------------
Route handlers that invoke ``DualReadRouter.query(...)`` MUST forward
``request.state.dual_read`` as the ``dual_read_override`` keyword argument.
Failing to thread the snapshot through is a wiring trap: the router will
fall back to a fresh ``is_dual_read_enabled()`` env read AND skip the
breaker check, which silently ignores both the rollback flag and the
breaker for that request.  ``DualReadRouter.query`` emits a WARNING when
it observes this gap with a currently-tripped breaker so the wiring
omission surfaces in production logs.

Critical bug-guard
------------------
The inflight gauge increment/decrement is wrapped in ``try/finally`` so the
count is ALWAYS decremented — even on auth errors, 422 validation failures,
410 Gone, or bare RuntimeErrors.  Without ``try/finally`` the gauge would
leak on exception paths and the 15-minute drain promise would break silently.
This is the exact bug T1.4 is designed to prevent.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from parallax.router.circuit_breaker import get_breaker_state
from parallax.router.config import is_dual_read_enabled
from parallax.router.inflight import inflight_gauge

__all__ = ["DualReadSnapshotMiddleware", "INFLIGHT_EXCLUDED_PATHS", "install_middleware"]

# Paths that do NOT count toward ``parallax_inflight_requests``.
#
# The gauge means "application work that a graceful drain must wait for"
# (inflight.py: "requests currently being processed (dual-read-aware path)").
# Monitoring traffic is neither: nothing is served on behalf of a user, and a
# drain must not block on a scrape or a liveness probe.
#
# ``/metrics`` is the load-bearing case and the reason this set exists. The
# gauge is READ WHILE SERVING THE SCRAPE, so without this exclusion every
# scrape reported at least 1 and an idle instance could never report 0. That
# is not cosmetic: docs/m3-runbooks/q8-drain-runbook.md selects stuck
# instances with ``parallax_inflight_requests > 0`` and its CI/CD deploy gate
# waits for ``sum(parallax_inflight_requests) == 0``, so every healthy
# instance would be classified as stuck and the gate would burn its full
# 960-second timeout on every deploy. Found by the #102 review; the defect was
# invisible before #102 because the gauge had never reached the wire at all.
#
# ``/healthz`` is here for the same reason rather than by analogy: a liveness
# probe is in flight for a moment on a schedule, so leaving it in makes that
# same ``== 0`` gate flake intermittently instead of failing constantly —
# strictly harder to diagnose. Both are excluded, or the gate is still wrong.
#
# Exact match, not prefix: a future ``/metrics/foo`` route serving real work
# should count, and a caller cannot dodge the gauge by prefixing a path.
INFLIGHT_EXCLUDED_PATHS = frozenset({"/metrics", "/healthz"})


class DualReadSnapshotMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that:

    1. Snapshots ``DUAL_READ`` env flag + circuit-breaker state once at
       request entry into ``request.state.dual_read``.
    2. Wraps the rest of the request in the inflight gauge with a
       ``try/finally`` guard that always decrements on exit — except for
       :data:`INFLIGHT_EXCLUDED_PATHS`, which are monitoring endpoints
       rather than drainable work.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:  # type: ignore[override]
        # 1. Snapshot the effective flag at request boundary.
        env_says_true = is_dual_read_enabled()
        breaker_tripped = get_breaker_state().is_tripped()
        request.state.dual_read = env_says_true and not breaker_tripped

        # 2. Monitoring endpoints are not drainable work — see
        #    INFLIGHT_EXCLUDED_PATHS. The flag snapshot above still happens for
        #    them: it is per-request state with no drain semantics, and
        #    skipping it would be an unrelated behaviour change.
        if request.url.path in INFLIGHT_EXCLUDED_PATHS:
            return await call_next(request)

        # 3. CRITICAL try/finally — inflight gauge MUST decrement even when
        #    call_next raises (auth errors, validation errors, bare exceptions).
        inflight_gauge.inc()
        try:
            response = await call_next(request)
            return response
        finally:
            inflight_gauge.dec()


def install_middleware(app: FastAPI) -> None:
    """Register :class:`DualReadSnapshotMiddleware` on *app*.

    Convenience wrapper so ``create_app()`` only needs one import.
    Do NOT call this at module import time — call it inside ``create_app()``
    so the middleware is scoped to the specific app instance (test isolation).
    """
    app.add_middleware(DualReadSnapshotMiddleware)
