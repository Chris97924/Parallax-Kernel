"""GET /query routes — progressive disclosure over :mod:`parallax.retrieve`.

``GET /query`` dispatches to one of the six v0.3.0 retrieval functions
based on the ``kind`` query parameter and returns L1/L2/L3 projections
(default L1). ``GET /query/reminder`` renders the
``<system-reminder>`` block produced by
:func:`parallax.injector.build_session_reminder` — this is the payload the
SessionStart hook plugin consumes.
"""

from __future__ import annotations

import pathlib
import sqlite3
from contextlib import closing
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from parallax import retrieve as R
from parallax.apex.audit_db import get_thread_local_audit_conn
from parallax.injector import build_session_reminder
from parallax.obs.log import get_logger as _get_logger
from parallax.obs.metrics import get_counter as _get_counter
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router import (
    QueryRequest as RouterQueryRequest,
)
from parallax.router import RealMemoryRouter, UnroutableQueryError, is_router_enabled, resolve
from parallax.router.aphelion_adapter import AphelionReadAdapter
from parallax.router.config import is_dual_read_enabled
from parallax.router.dual_read import DualReadRouter
from parallax.server.auth import current_user_id, require_auth
from parallax.server.deps import DBFactory, default_db_factory, get_conn
from parallax.server.schemas import (
    RETRIEVE_KINDS,
    QueryResponse,
    ReminderResponse,
    RetrievalHitDTO,
    RetrieveKind,
)

router = APIRouter(
    prefix="/query",
    tags=["query"],
    dependencies=[Depends(require_auth)],
)

_log = _get_logger("parallax.server.routes.query")
_deprecated_kind_counter = _get_counter("deprecated_kind_bug_total")


class _FactoryRealMemoryRouter:
    """QueryPort wrapper that opens its SQLite connection inside worker threads."""

    def __init__(self, db_factory: DBFactory) -> None:
        self._db_factory = db_factory

    def query(self, request: RouterQueryRequest) -> RetrievalEvidence:
        with closing(self._db_factory()) as conn:
            return RealMemoryRouter(conn).query(request)


def _hit_to_dto(hit: R.RetrievalHit, *, level: int) -> RetrievalHitDTO:
    proj = hit.project(level)
    return RetrievalHitDTO(
        entity_kind=proj["entity_kind"],
        entity_id=proj["entity_id"],
        title=proj["title"],
        score=float(proj["score"]),
        level=level,  # type: ignore[arg-type]
        evidence=proj.get("evidence") if level >= 2 else None,
        full=_normalize_full(proj.get("full")) if level >= 3 else None,
        explain=hit.explain,
    )


def _normalize_full(full: Any) -> dict[str, Any] | str | None:
    """Ensure the L3 ``full`` field is JSON-serialisable.

    :meth:`parallax.retrieve.RetrievalHit.project` returns either a dict
    snapshot of the underlying row (from ``_event_to_hit`` / ``_claim_to_hit``)
    or the ``evidence`` string fallback when ``full is None``. Pydantic accepts
    both, but we coerce bytes/None defensively so the OpenAPI contract is
    predictable.
    """
    if full is None or isinstance(full, (dict, str)):
        return full
    return str(full)


def _router_hit_to_dto(hit: dict[str, Any], *, level: int, query_type: str) -> RetrievalHitDTO:
    """Convert router RetrievalEvidence hit dict into API DTO shape."""
    entity_kind = str(hit.get("kind", "unknown"))
    entity_id = str(hit.get("id", ""))
    title = str(hit.get("text", ""))
    evidence = hit.get("evidence") if level >= 2 else None
    full = _normalize_full(hit.get("full")) if level >= 3 else None
    upstream_explain = hit.get("explain")
    explain: dict[str, Any] = {
        "reason": "memory_router_dispatch",
        "score_components": {"router": 1.0},
        "query_type": query_type,
    }
    if level >= 3 and isinstance(upstream_explain, dict):
        explain["upstream"] = upstream_explain

    return RetrievalHitDTO(
        entity_kind=entity_kind,
        entity_id=entity_id,
        title=title,
        score=float(hit.get("score", 0.0) or 0.0),
        level=level,  # type: ignore[arg-type]
        evidence=evidence,
        full=full,
        explain=explain,
    )


def _dispatch_with_router(
    conn: sqlite3.Connection,
    *,
    db_factory: DBFactory,
    audit_db_path: pathlib.Path,
    kind: RetrieveKind,
    user_id: str,
    q: str,
    level: int,
    limit: int,
    since: str | None,
    until: str | None,
    dual_read_override: bool | None,
    traffic_source: str | None,
) -> list[RetrievalHitDTO]:
    try:
        query_type = resolve(f"RetrieveKind.{kind}")
    except UnroutableQueryError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    request = RouterQueryRequest(
        query_type=query_type,
        user_id=user_id,
        q=q,
        limit=limit,
        since=since,
        until=until,
        level=level,
    )
    dual_read_enabled = (
        dual_read_override if dual_read_override is not None else is_dual_read_enabled()
    )
    primary = _FactoryRealMemoryRouter(db_factory) if dual_read_enabled else RealMemoryRouter(conn)
    # The audit_conn_provider lambda is evaluated by AphelionReadAdapter.query()
    # inside the DualReadRouter ThreadPoolExecutor worker thread (not this
    # request thread), so get_thread_local_audit_conn opens the per-thread
    # connection on the correct thread. audit_db_path was validated + stashed
    # on app.state by parallax.server.lifespan at boot.
    secondary = AphelionReadAdapter(
        audit_conn_provider=lambda: get_thread_local_audit_conn(audit_db_path),
    )
    mem_router = DualReadRouter(primary=primary, secondary=secondary)
    try:
        result = mem_router.query(
            request,
            dual_read_override=dual_read_override,
            traffic_source=traffic_source or "natural",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    evidence = result.primary

    return [
        _router_hit_to_dto(hit, level=level, query_type=query_type.value) for hit in evidence.hits
    ]


def _dispatch(
    conn: sqlite3.Connection,
    *,
    kind: RetrieveKind,
    user_id: str,
    q: str,
    limit: int,
    since: str | None,
    until: str | None,
) -> list[R.RetrievalHit]:
    if kind == "recent":
        return R.recent_context(conn, user_id=user_id, limit=limit)
    if kind == "file":
        return R.by_file(conn, user_id=user_id, path=q, limit=limit)
    if kind == "decision":
        return R.by_decision(conn, user_id=user_id, limit=limit)
    if kind == "bug":
        return R.by_bug_fix(conn, user_id=user_id, limit=limit)
    if kind == "entity":
        return R.by_entity(conn, user_id=user_id, subject=q, limit=limit)
    # timeline
    if since is None or until is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="timeline kind requires 'since' and 'until' ISO-8601 params",
        )
    try:
        return R.by_timeline(conn, user_id=user_id, since=since, until=until, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("", response_model=QueryResponse)
def get_query(
    request: Request,
    kind: Annotated[RetrieveKind, Query(description=f"one of {list(RETRIEVE_KINDS)}")],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    user_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    q: Annotated[str, Query(description="path / subject for file / entity kinds")] = "",
    level: Annotated[int, Query(ge=1, le=3, description="progressive disclosure tier")] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 10,
    since: Annotated[str | None, Query(description="ISO-8601 lower bound (timeline)")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound (timeline)")] = None,
) -> QueryResponse:
    # Multi-user mode: the authenticated principal on request.state.user_id
    # overrides any query-string ``user_id`` (which is logged as a leak
    # attempt if it disagrees). Single-token mode: the query-string value
    # is required, same as before.
    resolved_user_id = current_user_id(request, user_id)

    # ADR-007: kind=bug is deprecated under router-on. Return 410 Gone with
    # RFC 8594 Deprecation + Sunset headers. Counter tracks caller adoption.
    if kind == "bug" and is_router_enabled():
        _log.warning(
            "deprecated kind called",
            extra={
                "event": "deprecated_kind_called",
                "kind": "bug",
                "user_id": resolved_user_id,
            },
        )
        _deprecated_kind_counter.inc()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "kind=bug is deprecated under MEMORY_ROUTER=true; "
                "use CHANGE_TRACE with params.legacy_kind='bug' instead"
            ),
            headers={
                "Deprecation": "true",
                "Sunset": "Sat, 01 Aug 2026 00:00:00 GMT",
            },
        )

    if is_router_enabled():
        db_factory = cast(
            DBFactory,
            getattr(request.app.state, "db_factory", default_db_factory),
        )
        # Validated + stashed on app.state by parallax.server.lifespan at boot;
        # the dual-read worker thread opens its per-thread audit connection from
        # this path. getattr-guarded (consistent with db_factory above) so an
        # app constructed without running the lifespan fails with a clear 500
        # rather than an opaque AttributeError.
        audit_db_path = getattr(request.app.state, "audit_db_path", None)
        if audit_db_path is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="audit_db_path not initialised — server lifespan did not run",
            )
        dtos = _dispatch_with_router(
            conn,
            db_factory=db_factory,
            audit_db_path=audit_db_path,
            kind=kind,
            user_id=resolved_user_id,
            q=q,
            level=level,
            limit=limit,
            since=since,
            until=until,
            dual_read_override=getattr(request.state, "dual_read", None),
            traffic_source=getattr(request.state, "traffic_source", None) or "natural",
        )
    else:
        hits = _dispatch(
            conn,
            kind=kind,
            user_id=resolved_user_id,
            q=q,
            limit=limit,
            since=since,
            until=until,
        )
        dtos = [_hit_to_dto(h, level=level) for h in hits]
    return QueryResponse(kind=kind, level=level, count=len(dtos), hits=dtos)


@router.get("/reminder", response_model=ReminderResponse)
def get_reminder(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    user_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    session_id: Annotated[str | None, Query()] = None,
    max_hits: Annotated[int, Query(ge=1, le=32)] = 8,
) -> ReminderResponse:
    resolved_user_id = current_user_id(request, user_id)
    text = build_session_reminder(
        conn, user_id=resolved_user_id, session_id=session_id, max_hits=max_hits
    )
    return ReminderResponse(reminder=text, length=len(text))
