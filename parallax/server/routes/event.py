"""POST /event — Orbit dual-write ingest endpoint.

Single endpoint accepting Orbit's M6 dual-write envelope. Persists the
envelope via :func:`parallax.events.record_event` into the existing
``events`` table as a system-level audit row (``target_kind=None``).
Mirrors the conventions of :mod:`parallax.server.routes.ingest`.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, status

from parallax.events import record_event
from parallax.server.auth import current_user_id, require_auth
from parallax.server.deps import get_conn
from parallax.server.schemas import EventIngestRequest, EventIngestResponse

router = APIRouter(
    tags=["event"],
    dependencies=[Depends(require_auth)],
)


@router.post(
    "/event",
    response_model=EventIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_event(
    body: EventIngestRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),  # noqa: B008
) -> EventIngestResponse:
    user_id = current_user_id(request, body.user_id)
    payload = {
        "source_instance": body.source_instance,
        "schema_version": body.schema_version,
        "run_id": body.run_id,
        "record_id": body.record_id,
        "created_at": body.created_at,
        "commit_sha": body.commit_sha,
        "payload_hash": body.payload_hash,
        "judge_metadata": body.judge_metadata,
        "payload": body.payload,
    }
    try:
        with conn:
            event_id = record_event(
                conn,
                user_id=user_id,
                actor=body.source,
                event_type=body.event_type,
                target_kind=None,
                target_id=None,
                payload=payload,
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return EventIngestResponse(
        event_id=event_id,
        user_id=user_id,
        event_type=body.event_type,
    )
