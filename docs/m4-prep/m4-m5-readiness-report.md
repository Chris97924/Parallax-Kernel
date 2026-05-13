# M4/M5 Readiness Report

## §1 Scope

Stub section reserved for M4/M5 readiness scope.

## §2 Entry criteria

Stub section reserved for entry criteria.

## §3 M4 canary readiness

Stub section reserved for M4 canary readiness.

## §4 Observability

Stub section reserved for observability readiness.

## §5 Rollback readiness

Stub section reserved for rollback readiness.

## §6 Burn-in status

Stub section reserved for burn-in status.

## §7 M5 handoff

Stub section reserved for M5 handoff notes.

## §8 Readiness amendments

Stub section reserved for dated readiness amendments.

## §8.8 Traffic-gap hybrid resolution (2026-05-13)

M4 burn-in metrics now carry the `traffic_source` label per
`traffic-gap-resolution.md` §6 server-side normative behavior, with absent or
unrecognized request headers defaulting to `natural`. The M4 traffic-gap
resolution follows the hybrid design in `traffic-gap-resolution.md` §3: Phase 1
uses synthetic-aware series existence for the burn-in clock, while Phase 2 keeps
semantic DoD evaluation tied to natural traffic per §3.3.
