"""#106.3 completion — a drain-timeout signal that survives the process.

``DrainTimeoutDetected`` reads ``increase(parallax_drain_timeout_total[1h])``.
#102 put that counter on the wire and #105/#106 measured that the alert still
could not fire: uvicorn closes the listening socket *before* running lifespan
shutdown, so ``_drain_inflight``'s increment lands in a process Prometheus can
no longer reach. The counter was never wrong — it was unscrapeable, because a
counter owned by the terminating exporter cannot report its own termination.

The fix is to stop asking the dying process to publish the event. The timeout is
written to a tiny on-disk journal as the last act of shutdown, and the *next*
process restores the accumulated total into the same counter at startup. The
step from N to N+1 then happens across a restart, inside a live process that
Prometheus is scraping normally, so ``increase()`` sees it exactly as it sees
any other counter increment.

Three properties this file exists to guarantee:

* **Durability.** The event outlives the process that observed it.
* **Replay idempotence.** Restoring is ``inc(total - current)``, not
  ``inc(total)``. A restart with no new timeout adds nothing, so a scrape sees a
  flat series rather than a fabricated step on every deploy.
* **Monotonic accumulation.** The journal holds a running total, not a flag, so
  a second timeout is distinguishable from the first one being restored again.

Format is a small JSON object rather than an append-only log: the only consumer
is a counter restore, and a single read-modify-write keeps the restore O(1) and
the file bounded. Writes go through a temp file + ``os.replace`` so a crash
mid-write leaves the previous total intact rather than a truncated file.

Failure posture is fail-open with a WARNING. A missing, unreadable or corrupt
journal reports zero and never raises: losing the drain signal is bad, but
refusing to start the server — or crashing its shutdown — because a state file
is unwritable is worse.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DEFAULT_DRAIN_JOURNAL_NAME",
    "DRAIN_JOURNAL_ENV",
    "DrainJournal",
    "read_drain_journal",
    "record_drain_timeout",
    "resolve_drain_journal_path",
]

_log = logging.getLogger("parallax.server.drain_journal")

#: Operator override for the journal location. Defaults beside the process's
#: working directory, matching :data:`parallax.canary.audit_log.AUDIT_DB_ENV`'s
#: resolution convention (explicit arg -> env var -> cwd-relative default).
DRAIN_JOURNAL_ENV: Final[str] = "PARALLAX_DRAIN_JOURNAL_PATH"
DEFAULT_DRAIN_JOURNAL_NAME: Final[str] = "parallax_drain_journal.json"

_TOTAL_KEY: Final[str] = "drain_timeout_total"
_LAST_INFLIGHT_KEY: Final[str] = "last_inflight_count"
_LAST_TIMEOUT_KEY: Final[str] = "last_timeout_seconds"


@dataclasses.dataclass(frozen=True)
class DrainJournal:
    """Immutable projection of the journal file.

    ``total`` is the number of drain timeouts this deployment has recorded
    across every process that has ever written the file. ``readable`` is False
    when the file exists but could not be parsed — distinct from a genuinely
    absent journal (total 0, readable True), so a restore can tell "no timeout
    has happened" from "the record of one was lost".
    """

    total: float
    last_inflight_count: int | None
    last_timeout_seconds: float | None
    readable: bool


def resolve_drain_journal_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Explicit argument -> ``PARALLAX_DRAIN_JOURNAL_PATH`` -> cwd default."""
    if path is not None:
        return Path(path)
    env = os.environ.get(DRAIN_JOURNAL_ENV)
    if env:
        return Path(env)
    return Path.cwd() / DEFAULT_DRAIN_JOURNAL_NAME


def _coerce_total(raw: Any) -> float:
    """Non-negative float, or 0.0 for anything a writer could not have produced."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return 0.0
    value = float(raw)
    if value != value or value < 0:  # NaN or negative
        return 0.0
    return value


def read_drain_journal(path: str | os.PathLike[str] | None = None) -> DrainJournal:
    """Read the journal. Never raises.

    An absent file is the normal first-boot state and is reported as a readable
    zero. A present-but-broken file is reported as ``readable=False`` with a
    WARNING, so the restore can log that a signal may have been lost instead of
    silently treating the deployment as timeout-free.
    """
    resolved = resolve_drain_journal_path(path)
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DrainJournal(total=0.0, last_inflight_count=None, last_timeout_seconds=None,
                            readable=True)
    except OSError as exc:
        _log.warning(
            "parallax.drain_journal: unreadable journal at %s (%s) — treating as empty",
            resolved,
            type(exc).__name__,
        )
        return DrainJournal(total=0.0, last_inflight_count=None, last_timeout_seconds=None,
                            readable=False)

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _log.warning(
            "parallax.drain_journal: corrupt journal at %s — treating as empty",
            resolved,
        )
        return DrainJournal(total=0.0, last_inflight_count=None, last_timeout_seconds=None,
                            readable=False)

    if not isinstance(payload, dict):
        _log.warning(
            "parallax.drain_journal: journal at %s is not an object — treating as empty",
            resolved,
        )
        return DrainJournal(total=0.0, last_inflight_count=None, last_timeout_seconds=None,
                            readable=False)

    inflight = payload.get(_LAST_INFLIGHT_KEY)
    timeout_seconds = payload.get(_LAST_TIMEOUT_KEY)
    return DrainJournal(
        total=_coerce_total(payload.get(_TOTAL_KEY)),
        last_inflight_count=int(inflight) if isinstance(inflight, int) else None,
        last_timeout_seconds=(
            float(timeout_seconds) if isinstance(timeout_seconds, int | float) else None
        ),
        readable=True,
    )


def record_drain_timeout(
    *,
    inflight_count: int,
    timeout_seconds: float,
    path: str | os.PathLike[str] | None = None,
) -> float:
    """Persist one drain-timeout event; return the new running total.

    Read-modify-write of the running total, then an atomic replace. Returns 0.0
    without raising if the journal cannot be written — this runs during
    shutdown, where an exception would be reported as a lifespan failure and
    obscure the drain timeout it was trying to record.
    """
    resolved = resolve_drain_journal_path(path)
    total = read_drain_journal(resolved).total + 1.0
    payload = {
        _TOTAL_KEY: total,
        _LAST_INFLIGHT_KEY: int(inflight_count),
        _LAST_TIMEOUT_KEY: float(timeout_seconds),
    }
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Same-directory temp file so os.replace stays on one filesystem and is
        # therefore atomic; a crash between write and replace leaves the old
        # total readable rather than a half-written file.
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(resolved.parent),
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, resolved)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
    except OSError as exc:
        _log.warning(
            "parallax.drain_journal: could not persist drain timeout to %s (%s) — "
            "the event survives only in the log line",
            resolved,
            type(exc).__name__,
        )
        return 0.0
    return total
