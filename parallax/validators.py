"""Reference-integrity validators for cross-table targets.

Used by :func:`parallax.events.record_event` to verify the
``(target_kind, target_id)`` pair points at a real row owned by the writing
user before accepting an event.
"""

from __future__ import annotations

import sqlite3
from typing import Final, Literal

__all__ = [
    "target_ref_exists",
    "VALID_TARGET_KINDS",
    "DECISION_TARGET_KINDS",
    "TargetKind",
]


# Logical kind -> (physical table, primary-key column).
_KIND_TABLE: Final[dict[str, tuple[str, str]]] = {
    "memory": ("memories", "memory_id"),
    "claim": ("claims", "claim_id"),
    "source": ("sources", "source_id"),
    "decision": ("decisions", "decision_id"),
}

# Full allow-list accepted by ``target_ref_exists`` (mirrors ``events.target_kind``
# which is intentionally unconstrained so audit rows can reference any entity,
# including decision-level state changes).
VALID_TARGET_KINDS: Final[frozenset[str]] = frozenset(_KIND_TABLE)

# Narrower allow-list enforced by the ``decisions.target_kind`` CHECK
# constraint (``schema.sql:61``). Decisions never target other decisions in
# the Phase-0 model; the audit log (``events``) is where decision-on-decision
# traceability lives.
DECISION_TARGET_KINDS: Final[frozenset[str]] = frozenset({"memory", "claim", "source"})

TargetKind = Literal["memory", "claim", "source", "decision"]


def target_ref_exists(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
    *,
    user_id: str | None = None,
) -> bool:
    """Return True iff a row exists for ``(target_kind, target_id)``.

    Raises :class:`ValueError` when ``target_kind`` is not one of
    :data:`VALID_TARGET_KINDS`. ``target_id`` is compared by equality on the
    corresponding primary-key column; no wildcarding.

    When ``user_id`` is given, the row must also belong to that user; a row
    owned by another user counts as absent. Every target table carries a
    ``user_id`` column.

    **Transaction isolation.** Under WAL-mode SQLite, a row returned as
    present here can be deleted by a concurrent writer before the caller's
    dependent insert lands. Callers MUST hold a single transaction spanning
    the ``target_ref_exists`` check and the dependent write so both observe
    the same snapshot -- otherwise the check is a TOCTOU.
    """
    try:
        table, pk = _KIND_TABLE[target_kind]
    except KeyError as exc:
        raise ValueError(
            f"unknown target_kind {target_kind!r}; "
            f"expected one of {sorted(VALID_TARGET_KINDS)}"
        ) from exc

    if user_id is None:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {pk} = ? LIMIT 1",  # nosec B608: table+pk are fixed literals
            (target_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {pk} = ? AND user_id = ? LIMIT 1",  # nosec B608: table+pk are fixed literals
            (target_id, user_id),
        ).fetchone()
    return row is not None
