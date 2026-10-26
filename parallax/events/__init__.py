"""Event-write helpers for Parallax.

Sits on top of :func:`parallax.sqlite_store.insert_event` and adds:

* ULID generation for ``event_id``
* JSON serialization for ``payload``
* Reference-integrity validation via :func:`parallax.validators.target_ref_exists`
  so events with a non-empty ``(target_kind, target_id)`` cannot reference a
  row that does not exist (orphan rejection)
* Two convenience helpers — :func:`record_memory_reaffirmed` and
  :func:`record_claim_state_changed` — for the two event types that the
  ingest / state-machine layers emit today.

Events without a target (system-level audit rows) are accepted by passing
``target_kind=None`` and ``target_id=None``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from ulid import ULID

from parallax import telemetry
from parallax.obs.log import get_logger
from parallax.sqlite_store import Event, insert_event, now_iso
from parallax.transitions import is_allowed_transition
from parallax.validators import VALID_TARGET_KINDS, target_ref_exists

__all__ = [
    "record_event",
    "record_memory_reaffirmed",
    "record_claim_state_changed",
    "transition_claim_state",
]

_log = get_logger("parallax.events")
_tlog = telemetry.get_logger("parallax.events.telemetry")


def _ulid() -> str:
    return str(ULID())


def record_event(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    actor: str,
    event_type: str,
    target_kind: str | None,
    target_id: str | None,
    payload: Mapping[str, Any] | None = None,
    approval_tier: str | None = None,
    session_id: str | None = None,
) -> str:
    """Append a single event row. Returns the generated event_id.

    When ``target_kind`` is one of :data:`VALID_TARGET_KINDS` and
    ``target_id`` is a non-empty string, the row referenced by
    ``(target_kind, target_id)`` MUST exist and belong to ``user_id`` or
    :class:`ValueError` is raised (orphan rejection; another user's row is
    reported exactly like a missing one). System-level events with no target
    pass ``target_kind=None`` and ``target_id=None``.

    The validator and the insert run on the same connection; callers MUST
    hold the surrounding transaction so the existence check and the insert
    observe a consistent snapshot under WAL-mode SQLite (see
    :func:`parallax.validators.target_ref_exists` docstring for the TOCTOU
    rationale).
    """
    if (target_kind is None) != (target_id is None):
        raise ValueError(
            "target_kind and target_id must be provided together or both omitted; "
            f"got target_kind={target_kind!r}, target_id={target_id!r}"
        )

    if target_kind is not None and target_id is not None and target_id != "":
        if target_kind in VALID_TARGET_KINDS:
            if not target_ref_exists(conn, target_kind, target_id, user_id=user_id):
                telemetry.emit_orphan_rejected(
                    _tlog,
                    user_id=user_id,
                    target_kind=target_kind,
                    target_id=target_id,
                )
                raise ValueError(
                    f"orphan event rejected: ({target_kind!r}, {target_id!r}) " f"does not exist"
                )

    event_id = _ulid()
    payload_json = json.dumps(dict(payload) if payload is not None else {}, sort_keys=True)
    insert_event(
        conn,
        Event(
            event_id=event_id,
            user_id=user_id,
            actor=actor,
            event_type=event_type,
            target_kind=target_kind,
            target_id=target_id,
            payload_json=payload_json,
            approval_tier=approval_tier,
            created_at=now_iso(),
            session_id=session_id,
        ),
    )
    _log.info(
        "record_event",
        extra={
            "event": "record_event",
            "event_type": event_type,
            "user_id": user_id,
            "event_id": event_id,
            "target_kind": target_kind,
            "target_id": target_id,
        },
    )
    return event_id


def record_memory_reaffirmed(
    conn: sqlite3.Connection, *, user_id: str, memory_id: str, actor: str = "system"
) -> str:
    """Emit a ``memory.reaffirmed`` event for a deduped ingest hit."""
    return record_event(
        conn,
        user_id=user_id,
        actor=actor,
        event_type="memory.reaffirmed",
        target_kind="memory",
        target_id=memory_id,
        payload={"memory_id": memory_id},
    )


def record_claim_state_changed(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    claim_id: str,
    from_state: str,
    to_state: str,
    updated_at: str | None = None,
    actor: str = "system",
) -> str:
    """Emit a ``claim.state_changed`` event capturing the from/to transition.

    **This helper writes the audit row only.** It does NOT validate the
    transition against :data:`parallax.transitions.CLAIM_TRANSITIONS` and
    it does NOT mutate the ``claims`` table. Callers that need to apply
    the transition AND emit the event in one step should use
    :func:`transition_claim_state`, which wraps the ``SELECT current /
    is_allowed_transition / UPDATE / record_event`` sequence in a single
    transaction.

    ``updated_at`` is the ISO-8601 timestamp the corresponding
    ``UPDATE claims SET state=?, updated_at=?`` applied. Carrying it in the
    event payload lets :func:`parallax.replay.replay_events` reconstruct the
    row bit-for-bit; omitting it keeps backward compatibility with events
    written before v0.4.1 (replay falls back to state-only UPDATE).
    """
    payload: dict[str, Any] = {"from": from_state, "to": to_state}
    if updated_at is not None:
        payload["updated_at"] = updated_at
    return record_event(
        conn,
        user_id=user_id,
        actor=actor,
        event_type="claim.state_changed",
        target_kind="claim",
        target_id=claim_id,
        payload=payload,
    )


def transition_claim_state(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    to_state: str,
    actor: str = "system",
    expected_user_id: str | None = None,
) -> str:
    """Apply a claim state transition and emit the matching audit event.

    Atomic ``SELECT current state → is_allowed_transition → UPDATE
    claims SET state=?, updated_at=? → record_event`` wrapped in a single
    transaction. Returns the generated ``event_id``.

    Compared with :func:`record_claim_state_changed`, this function is
    the canonical API for callers that intend the row to actually change:
    it guarantees that ``claims.state`` and the event log can never go out
    of sync (no scenario where the event says ``confirmed`` but the row
    is still ``pending``).

    Parameters
    ----------
    claim_id:
        Target claim primary key. Raises ``ValueError`` when the row does
        not exist.
    to_state:
        Desired next state. Raises ``ValueError`` when the transition
        from the current state is not in
        :data:`parallax.transitions.CLAIM_TRANSITIONS`.
    actor:
        Audit-trail attribution string (default ``"system"``).
    expected_user_id:
        Optional defensive guard. When set, raises ``ValueError`` if the
        stored ``user_id`` differs from the expected value. Useful for
        multi-tenant routes that want to refuse a claim id supplied by
        another tenant before committing the transition.

    Self-loops on non-terminal states (e.g. ``pending → pending``) are
    accepted because :data:`CLAIM_TRANSITIONS` allows them: this matches
    the documented retry-safe semantics in ``docs/state-transitions.md``.
    """
    with conn:
        row = conn.execute(
            "SELECT user_id, state FROM claims WHERE claim_id = ? LIMIT 1",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"claim {claim_id!r} not found")

        # sqlite3.Row supports both name and index access; tuple factories
        # only support index. Stay compatible with both.
        if hasattr(row, "keys"):
            stored_user_id = row["user_id"]
            from_state = row["state"]
        else:
            stored_user_id = row[0]
            from_state = row[1]

        if expected_user_id is not None and stored_user_id != expected_user_id:
            raise ValueError(
                f"claim {claim_id!r} belongs to a different user "
                f"(expected {expected_user_id!r}); refusing to transition"
            )

        if not is_allowed_transition("claim", from_state, to_state):
            raise ValueError(f"transition {from_state!r} -> {to_state!r} not allowed for claims")

        updated_at = now_iso()
        cursor = conn.execute(
            "UPDATE claims SET state = ?, updated_at = ? " "WHERE claim_id = ? AND state = ?",
            (to_state, updated_at, claim_id, from_state),
        )
        if cursor.rowcount == 0:
            # A concurrent writer changed the state between our SELECT and
            # the UPDATE. Surface as ValueError so the caller can retry
            # with a fresh read instead of silently accepting a no-op.
            raise ValueError(
                f"claim {claim_id!r} state changed concurrently "
                f"(expected from_state={from_state!r}); transition aborted"
            )
        return record_claim_state_changed(
            conn,
            user_id=str(stored_user_id),
            claim_id=claim_id,
            from_state=from_state,
            to_state=to_state,
            updated_at=updated_at,
            actor=actor,
        )
