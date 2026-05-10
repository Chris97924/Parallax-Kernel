"""Per-request ``traffic_source`` snapshot middleware (Apex M4 hybrid loader).

Implements ``docs/m4-prep/traffic-gap-resolution.md`` §6 normative
server behavior:

  * ``X-Parallax-Traffic-Source: synthetic`` → ``traffic_source="synthetic"``
  * ``X-Parallax-Traffic-Source: natural`` → ``traffic_source="natural"``
  * absent OR unrecognized value → ``traffic_source="natural"`` (fail-safe)
  * lowercase compare + trim whitespace before matching

The default is "natural" so any unlabeled production traffic counts as
real traffic — the alternative ("unknown" or omitted label) silently
breaks the Phase-2 GREEN PromQL filter
``{traffic_source="natural"}`` which the DoD evaluator uses to
distinguish synthetic burn-in load from production reality.

Downstream metrics emission MUST read ``request.state.traffic_source``
rather than re-parsing the header — middleware snapshot is the single
ground-truth for the request lifetime.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

HEADER_NAME: Final = "X-Parallax-Traffic-Source"
KNOWN_VALUES: Final = frozenset({"synthetic", "natural"})
DEFAULT_LABEL: Final = "natural"


def resolve_traffic_source(raw_header: str | None) -> str:
    """Apply spec §6 resolution rules to a header value.

    Args:
        raw_header: the value of ``X-Parallax-Traffic-Source`` or ``None``.

    Returns:
        Either ``"synthetic"`` or ``"natural"`` (the only two values
        downstream metrics emit).
    """
    if raw_header is None:
        return DEFAULT_LABEL
    candidate = raw_header.strip().lower()
    if candidate in KNOWN_VALUES:
        return candidate
    return DEFAULT_LABEL


class TrafficSourceMiddleware(BaseHTTPMiddleware):
    """Snapshot ``X-Parallax-Traffic-Source`` once at request entry.

    Stores result in ``request.state.traffic_source``. Route handlers /
    Prometheus instrumentation MUST consult that attribute.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: ASGIApp,  # type: ignore[override]
    ) -> Response:
        raw = request.headers.get(HEADER_NAME)
        request.state.traffic_source = resolve_traffic_source(raw)
        return await call_next(request)


def install_middleware(app: FastAPI) -> None:
    """Register :class:`TrafficSourceMiddleware` on ``app``.

    Idempotent — repeat calls do not re-register (the attr-flag mirrors
    the ``dual_read_snapshot`` pattern in this directory).
    """
    if getattr(app.state, "_traffic_source_installed", False):
        return
    app.add_middleware(TrafficSourceMiddleware)
    app.state._traffic_source_installed = True
