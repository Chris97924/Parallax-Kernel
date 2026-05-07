"""POST /ingest routes — thin wrappers around :mod:`parallax.ingest`.

Two endpoints:

* ``POST /ingest/memory`` — wraps :func:`parallax.ingest.ingest_memory`.
* ``POST /ingest/claim``  — wraps :func:`parallax.ingest.ingest_claim`.

Both are authenticated via :func:`parallax.server.auth.require_auth` and
return the persisted row id. Dedup is transparent: a repeat ingest returns
the same id without creating a duplicate (the kernel's INSERT-OR-IGNORE
semantics already handle this).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status

from parallax.ingest import ingest_claim, ingest_memory
from parallax.router import RealMemoryRouter, is_router_enabled
from parallax.router.contracts import IngestRequest
from parallax.server.auth import current_user_id, require_auth
from parallax.server.deps import get_conn
from parallax.server.schemas import (
    IngestClaimRequest,
    IngestMemoryRequest,
    IngestResponse,
    RouterIngestResponse,
)

router = APIRouter(
    prefix="/ingest",
    tags=["ingest"],
    dependencies=[Depends(require_auth)],
)


@router.post("/memory", response_model=None, status_code=status.HTTP_201_CREATED)
def post_ingest_memory(
    body: IngestMemoryRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RouterIngestResponse | IngestResponse:
    # In multi-user mode the authenticated user_id on request.state wins;
    # a mismatched body.user_id is logged (potential leak attempt) but
    # never trusted. Single-token mode keeps body.user_id as today.
    user_id = current_user_id(request, body.user_id)
    if is_router_enabled():
        req = IngestRequest(
            user_id=user_id,
            kind="memory",
            payload={
                "body": body.summary,
                "title": body.title,
                "vault_path": body.vault_path,
            },
            source_id=body.source_id,
        )
        try:
            result = RealMemoryRouter(conn).ingest(req)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        return RouterIngestResponse(
            kind=result.kind,
            id=result.identifier,
            user_id=user_id,
            deduped=result.deduped,
        )
    try:
        memory_id = ingest_memory(
            conn,
            user_id=user_id,
            title=body.title,
            summary=body.summary,
            vault_path=body.vault_path,
            source_id=body.source_id,
        )
    except ValueError as exc:
        # ingest_memory rejects reserved-namespace source_id (e.g. cross-user
        # ``direct:<other>``) at the boundary; surface as 400 to stay symmetric
        # with the claim path and the router-enabled branch above.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return IngestResponse(kind="memory", id=memory_id, user_id=user_id)


@router.post("/claim", response_model=None, status_code=status.HTTP_201_CREATED)
def post_ingest_claim(
    body: IngestClaimRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RouterIngestResponse | IngestResponse:
    user_id = current_user_id(request, body.user_id)
    if is_router_enabled():
        req = IngestRequest(
            user_id=user_id,
            kind="claim",
            payload={
                "subject": body.subject,
                "predicate": body.predicate,
                "object_": body.object_,
                "confidence": body.confidence,
                "state": body.state,
            },
            source_id=body.source_id,
        )
        try:
            result = RealMemoryRouter(conn).ingest(req)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        return RouterIngestResponse(
            kind=result.kind,
            id=result.identifier,
            user_id=user_id,
            deduped=result.deduped,
        )
    try:
        claim_id = ingest_claim(
            conn,
            user_id=user_id,
            subject=body.subject,
            predicate=body.predicate,
            object_=body.object_,
            source_id=body.source_id,
            confidence=body.confidence,
            state=body.state,
        )
    except ValueError as exc:
        # ingest_claim rejects unknown states at the boundary; surface as 400.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return IngestResponse(kind="claim", id=claim_id, user_id=user_id)
