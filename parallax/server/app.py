"""FastAPI application factory for Parallax.

``create_app()`` wires:

* ``GET /healthz``           — unauthenticated liveness probe
* ``POST /ingest/memory``    — ingest a memory row
* ``POST /ingest/claim``     — ingest a claim row
* ``GET  /query``            — progressive-disclosure retrieval
* ``GET  /query/reminder``   — SessionStart <system-reminder> block
* ``GET  /inspect/health``   — telemetry health
* ``GET  /inspect/info``     — parallax_info + health bundle

All non-``/healthz`` routes go through
:func:`parallax.server.auth.require_auth` (shared bearer token via
``PARALLAX_TOKEN``). When the env var is unset the server logs a loud
warning and runs in open mode — intended for localhost dev only.

Tests construct an app via ``create_app(db_factory=...)`` to swap the
SQLite factory with an in-memory or tmp-path fixture without touching
``PARALLAX_DB_PATH``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

from parallax import __version__
from parallax.server.auth import (
    PARALLAX_BIND_HOST_ENV,
    assert_safe_to_start,
    auth_configured,
    bind_host_is_safe,
    metrics_public_allowed,
)
from parallax.server.deps import DBFactory, default_db_factory
from parallax.server.lifespan import parallax_lifespan
from parallax.server.middleware.dual_read_snapshot import install_middleware as install_dual_read_snapshot_middleware
from parallax.server.middleware.traffic_source import install_middleware as install_traffic_source_middleware
from parallax.server.routes.admin.circuit_breaker import router as admin_circuit_breaker_router
from parallax.server.routes.backfill import router as backfill_router
from parallax.server.routes.event import router as event_router
from parallax.server.routes.export import router as export_router
from parallax.server.routes.ingest import router as ingest_router
from parallax.server.routes.inspect import router as inspect_router
from parallax.server.routes.metrics import router as metrics_router
from parallax.server.routes.query import router as query_router

__all__ = ["create_app"]

_log = logging.getLogger("parallax.server")


def _install_error_handlers(app: FastAPI) -> None:
    """Map unexpected errors to structured JSON instead of HTML stack traces.

    Validation errors keep FastAPI's shape but are wrapped so the body
    always matches :class:`parallax.server.schemas.ErrorResponse`'s keys
    for machine consumers. 500s never leak the traceback to the wire.
    """

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # `exc.errors()` can carry raw ValueError instances in `ctx`
        # (e.g. from a custom field_validator). Route through FastAPI's
        # jsonable_encoder so they're coerced to strings before serialisation.
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"error": "validation_error", "detail": exc.errors()}),
        )

    @app.exception_handler(sqlite3.Error)
    async def _on_sqlite_error(_request: Request, exc: sqlite3.Error) -> JSONResponse:
        # Log the full exception server-side for diagnosis, but never send
        # str(exc) to the client — SQLite errors leak table/column names
        # and sometimes row values that help an attacker map the schema.
        _log.exception("sqlite error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": "database_error", "detail": "internal database error"},
        )


def create_app(
    *,
    db_factory: DBFactory | None = None,
    settings: dict[str, Any] | None = None,
) -> FastAPI:
    """Build a Parallax FastAPI app.

    Parameters
    ----------
    db_factory:
        Optional connection factory. When ``None`` the app uses
        :func:`parallax.server.deps.default_db_factory`, which reads
        :func:`parallax.config.load_config`. Tests pass an in-memory /
        tmp-path factory here.
    settings:
        Reserved for future config injection (CORS origins, rate limits,
        feature flags). Accepted as a dict so callers don't need to import
        an internal settings type for a single knob.
    """
    # OpenAPI docs are opt-in: exposing `/docs` and `/redoc` on a public
    # endpoint gives attackers a free enumeration of every route and
    # schema. Set ``PARALLAX_DOCS_ENABLED=1`` for local dev.
    docs_enabled = os.environ.get("PARALLAX_DOCS_ENABLED", "").strip() in (
        "1",
        "true",
        "True",
        "yes",
    )
    app = FastAPI(
        title="Parallax Kernel",
        version=__version__,
        description="Parallax v0.6 hub — HTTP facade over the kernel.",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=parallax_lifespan,
    )
    app.state.db_factory = db_factory or default_db_factory
    app.state.settings = dict(settings or {})

    # Refuse to start when bound to a non-localhost interface without auth.
    # Override with ``PARALLAX_ALLOW_OPEN_PUBLIC=1`` if absolutely needed.
    assert_safe_to_start()

    if not auth_configured():
        bind_host = os.environ.get(PARALLAX_BIND_HOST_ENV, "")
        if bind_host_is_safe(bind_host):
            _log.warning(
                "PARALLAX_TOKEN is unset — server is running in OPEN MODE on "
                "loopback. Set PARALLAX_TOKEN before exposing this server "
                "outside localhost."
            )
        else:
            # Reachable only when PARALLAX_ALLOW_OPEN_PUBLIC=1 was explicitly
            # set (otherwise assert_safe_to_start would have raised).
            _log.error(
                "PARALLAX_TOKEN is unset AND %s=%r is non-localhost; opt-in "
                "via PARALLAX_ALLOW_OPEN_PUBLIC=1. This is unsafe — anyone on "
                "the network can read/write your kernel.",
                PARALLAX_BIND_HOST_ENV,
                bind_host,
            )

    # Audit log when the /metrics public override is active so post-incident
    # readers can see the route was deliberately exposed.
    if metrics_public_allowed():
        _log.warning(
            "auth.metrics.public_override_active — /metrics is reachable "
            "without a bearer token (PARALLAX_METRICS_PUBLIC=1). Ensure "
            "the network boundary or upstream proxy gates the route."
        )

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        """Unauthenticated liveness probe. No DB access.

        Intentionally does not hit SQLite so this endpoint stays usable as
        a Kubernetes / PM2 readiness probe even when the DB is locked.
        """
        return {
            "status": "ok",
            "service": "parallax-kernel",
        }

    app.include_router(ingest_router)
    app.include_router(event_router)
    app.include_router(query_router)
    app.include_router(inspect_router)
    app.include_router(export_router)
    app.include_router(backfill_router)
    app.include_router(metrics_router)
    app.include_router(admin_circuit_breaker_router)

    if os.environ.get("PARALLAX_VIEWER_ENABLED", "0") == "1":
        from parallax.server.viewer import router as viewer_router

        app.include_router(viewer_router)
        _log.info("parallax viewer enabled at /viewer")

    _install_error_handlers(app)
    install_traffic_source_middleware(app)
    install_dual_read_snapshot_middleware(app)

    return app


# Module-level ASGI handle for ``uvicorn parallax.server.app:app``.
# Built eagerly at import time so uvicorn (which does a plain attribute
# lookup via the import string ``parallax.server.app:app``) sees a ready
# ASGI callable. Tests never import this symbol — they construct their
# own app via ``create_app(db_factory=...)`` so fixture injection stays
# side-effect-free.
app: FastAPI = create_app()


def get_app_factory() -> Callable[[], FastAPI]:
    """Return the default factory (for CLI `parallax serve`)."""
    return create_app
