"""US-009.1 — M4 Canary Infrastructure.

Three subsystems live under this package, each isolated from the rest of
``parallax/router/`` (per ``us-009-acceptance-criteria.md`` §7 O.2):

* :mod:`parallax.canary.event_id` — RFC 4122 UUID v7 generator (criteria 1.1).
* :mod:`parallax.canary.audit_log` — Independent SQLite audit log
  (criteria 1.5–1.8).
* :mod:`parallax.canary.idempotency` — ``event_id``-keyed cache + handler
  decorator (criteria 1.2–1.4, 1.7).
* :mod:`parallax.canary.triggers` — T1-T5 rollback gate primitives
  (criteria 1.9–1.14).
* :mod:`parallax.canary.rollback` — RollbackController state machine with
  30-minute cooldown + manual ACK (criteria 1.15–1.17).

The whole package targets Python 3.11+; UUID v7 is implemented in pure
Python because the project's minimum is 3.11 and ``uuid.uuid7`` only
arrived in 3.13.
"""

from __future__ import annotations

__all__: list[str] = []
