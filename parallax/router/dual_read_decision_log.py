"""M3b post-review — JSONL writer for the dual-read decision stream.

Pre-fix-cycle, ``parallax/router/dual_read_metrics.py`` was a perfect
*reader* but no producer wrote the JSONL files it consumed: every Gate C
smoke run was vacuous on zero records.  This module is the producer.

Schema
------
One line per decision, deterministic key-sorted JSON, one daily file
rotated by UTC date::

    dual-read-decisions-YYYY-MM-DD.jsonl

Required fields (schema_version="1.0"):

  - ``schema_version`` — locked at "1.0"; readers coerce missing fields
    to safe defaults
  - ``timestamp_us_utc`` — microsecond UTC clock at append time
  - ``timestamp`` — ISO-8601 UTC string with microseconds (mirror of
    ``timestamp_us_utc``); kept so the existing metrics-module reader
    parses these lines without a schema bump
  - ``correlation_id`` — opaque request id
  - ``query_type`` — :class:`parallax.router.types.QueryType` value
  - ``outcome`` — one of ``match`` | ``diverge`` | ``primary_only`` |
    ``aphelion_unreachable`` | ``skipped`` (5-value DualReadOutcome vocabulary
    from :class:`parallax.router.contracts.DualReadResult`).
  - ``winning_source`` — ``parallax`` | ``aphelion`` | ``tie`` |
    ``fallback`` | ``null`` (skipped path)
  - ``policy_version`` — version label of the rule table
  - ``write_error_observed`` — bool; True iff the conflict-event writer
    failed
  - ``conflict_event_id`` — str | None
  - ``data_quality_flag`` — ``cold_start`` | ``corpus_immature`` |
    ``normal``

Feature flag
------------
``DUAL_READ_LOG_ENABLED`` (env var) — kill switch.  Default mirror of
``DUAL_READ`` (when DUAL_READ=true the log is on).  Operators set
``DUAL_READ_LOG_ENABLED=false`` to silence the producer without
disabling dual-read traffic.

Best-effort
-----------
The writer is fail-closed: any I/O / encoding exception is logged
WARNING once and the call returns ``None`` — the dual-read query path
never raises.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path
from typing import Any

from parallax.obs.log import safe_log_warning

__all__ = [
    "SCHEMA_VERSION",
    "DUAL_READ_LOG_ENABLED_ENV",
    "DUAL_READ_LOG_DIR_ENV",
    "is_log_enabled",
    "resolve_log_dir",
    "append_decision",
]

_log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
DUAL_READ_LOG_ENABLED_ENV = "DUAL_READ_LOG_ENABLED"
DUAL_READ_LOG_DIR_ENV = "DUAL_READ_LOG_DIR"

# Mirror parallax.router.dual_read_metrics._DEFAULT_LOG_DIR — keep in sync.
_DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "parallax" / "logs"


_REQUIRED_FIELDS: tuple[str, ...] = (
    "correlation_id",
    "query_type",
    "outcome",
    "winning_source",
    "policy_version",
    "write_error_observed",
    "conflict_event_id",
    "data_quality_flag",
)


def is_log_enabled() -> bool:
    """Return True iff the JSONL producer should append.

    Resolution order:
      1. Explicit ``DUAL_READ_LOG_ENABLED`` — interpret common
         truthy / falsy strings.
      2. Mirror ``DUAL_READ`` flag (default-on when DUAL_READ is on).
      3. Default off when DUAL_READ is off.
    """
    raw = os.environ.get(DUAL_READ_LOG_ENABLED_ENV)
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    # Mirror DUAL_READ default.
    dr = os.environ.get("DUAL_READ", "").strip().lower()
    return dr in ("1", "true", "yes", "on")


def resolve_log_dir(log_dir: Path | str | None = None) -> Path:
    """Resolve the log directory.

    Priority: explicit argument > ``DUAL_READ_LOG_DIR`` env > built-in
    default mirroring the metrics reader's default.
    """
    if log_dir is not None:
        return Path(log_dir).resolve()
    env = os.environ.get(DUAL_READ_LOG_DIR_ENV)
    if env:
        return Path(env).resolve()
    return _DEFAULT_LOG_DIR.resolve()


def _now_iso_us(now_us: int) -> str:
    """ISO-8601 UTC string with microseconds for ``now_us`` (microseconds)."""
    seconds, micros = divmod(now_us, 1_000_000)
    return (
        _dt.datetime.fromtimestamp(seconds, _dt.UTC)
        .replace(microsecond=micros)
        .isoformat(timespec="microseconds")
    )


def _daily_path(log_dir: Path, now: _dt.datetime) -> Path:
    """Return the daily JSONL path for ``now`` (UTC date)."""
    return log_dir / f"dual-read-decisions-{now.date().isoformat()}.jsonl"


def _build_record(
    decision_record: dict[str, Any],
    *,
    timestamp_us_utc: int,
    timestamp_iso: str,
) -> dict[str, Any]:
    """Build a deterministic JSON record from caller-supplied fields.

    Missing required fields are coerced to safe defaults so the reader
    side never sees a partially-formed line:

      - ``winning_source``  → ``None``
      - ``conflict_event_id`` → ``None``
      - ``write_error_observed`` → ``False``
      - ``data_quality_flag`` → ``"normal"``
      - other strings → ``""``

    Unrecognised caller fields are passed through; readers will treat
    them as future-compat extras.
    """
    out: dict[str, Any] = {}
    out["schema_version"] = SCHEMA_VERSION
    out["timestamp_us_utc"] = timestamp_us_utc
    out["timestamp"] = timestamp_iso

    # Required fields with safe defaults.
    out["correlation_id"] = decision_record.get("correlation_id", "")
    out["query_type"] = decision_record.get("query_type", "")
    out["outcome"] = decision_record.get("outcome", "")
    out["winning_source"] = decision_record.get("winning_source")
    out["policy_version"] = decision_record.get("policy_version", "")
    out["write_error_observed"] = bool(decision_record.get("write_error_observed", False))
    out["conflict_event_id"] = decision_record.get("conflict_event_id")
    out["data_quality_flag"] = decision_record.get("data_quality_flag", "normal")

    # Forward-compat passthrough — keep extra keys the caller provided
    # except the schema/clock overrides that are owned by this writer.
    reserved = {"schema_version", "timestamp_us_utc", "timestamp"} | set(_REQUIRED_FIELDS)
    for key, value in decision_record.items():
        if key not in reserved and key not in out:
            out[key] = value
    return out


def append_decision(
    decision_record: dict[str, Any],
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
) -> Path | None:
    """Append a single dual-read decision to the daily JSONL file.

    Returns the path written on success, or ``None`` when:
      - the log is disabled via ``DUAL_READ_LOG_ENABLED=false``
      - any I/O / encoding exception was caught (logged WARNING once)

    Best-effort: never raises out of the function.

    Parameters
    ----------
    decision_record:
        Mapping of fields per the module docstring. Caller is
        responsible for resolving ``outcome`` / ``winning_source`` /
        ``write_error_observed`` etc.; the writer only fills in the
        schema_version + timestamps and applies safe defaults.
    log_dir:
        Optional override; resolves via :func:`resolve_log_dir` when
        unset.
    now:
        Optional UTC anchor for the timestamp + daily-file selection.
        Defaults to ``datetime.now(UTC)``. Test callers pin this for
        determinism.
    """
    try:
        if not is_log_enabled():
            return None
        anchor = now if now is not None else _dt.datetime.now(_dt.UTC)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=_dt.UTC)
        ts_us = int(anchor.timestamp() * 1_000_000)
        ts_iso = _now_iso_us(ts_us)
        record = _build_record(
            decision_record,
            timestamp_us_utc=ts_us,
            timestamp_iso=ts_iso,
        )
        line = json.dumps(record, sort_keys=True) + "\n"
        target_dir = resolve_log_dir(log_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = _daily_path(target_dir, anchor)
        # Append in binary mode so a partially-corrupt previous line
        # does not block the new write — text-mode line buffering is
        # already line-atomic on POSIX, and Windows line endings are
        # locked to \n via explicit utf-8 encoding.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        return path
    except Exception as exc:  # noqa: BLE001 — fail-closed: never crash the request
        # This used to be a hand-rolled copy of ``dual_read._safe_log_warning``:
        # it cannot import that one, because ``dual_read`` imports from *this*
        # module and sharing would close an import cycle. Hosting the helper in
        # ``parallax.obs.log`` removes the cycle and the duplicate together.
        safe_log_warning(_log, "dual_read_decision_log.append_failed", exc=exc)
        return None
