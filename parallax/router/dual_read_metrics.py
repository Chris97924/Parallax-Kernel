"""M3b — file-based dual-read metrics computation (US-006-M3-T2.3).

Mirrors :mod:`parallax.shadow.discrepancy` for the dual-read decision JSONL
stream. Pure module — no I/O at import. Thresholds live here so the metrics
endpoint and the DoD CLI both import from one source of truth.

Sources of data
---------------
The dual-read decision log is a per-day JSONL file under ``log_dir``:

  ``dual-read-decisions-YYYY-MM-DD.jsonl``

Each line is a single JSON object with at least:

  - ``outcome`` (or ``arbitration_outcome``) — one of ``match``,
    ``diverge``, ``primary_only``, ``aphelion_unreachable``, ``skipped``.
  - ``timestamp`` — ISO-8601 UTC microseconds.
  - ``data_quality_flag`` — optional, defaults to ``"normal"`` when missing.
  - ``crosswalk_status`` — optional, ``"miss"`` triggers crosswalk-miss
    counting.
  - ``circuit_breaker_tripped`` — optional bool.
  - ``write_error_observed`` — optional bool; matches the JSONL producer's
    field name (``parallax.router.dual_read_decision_log.append_decision``)
    and ``DualReadResult.write_error_observed``.

Records with malformed JSON or unparseable timestamps are silently dropped
(they cannot be window-filtered safely).

Denominator semantics
---------------------
All rate denominators EXCLUDE ``aphelion_unreachable`` outcomes (mirrors
M2's ``shadow_only`` exclusion per ralplan §6 line 429). The exception is
:func:`aphelion_unreachable_rate`, where the unreachable count is itself
the numerator and the denominator is ALL outcomes.

Data-quality filtering
----------------------
The ``data_quality_filter`` argument defaults to
``["normal", "corpus_immature"]``. Records flagged ``cold_start`` are
excluded from production rate computation by default — pass an explicit
filter (e.g. including ``"cold_start"``) to opt back in for early-rollout
debug runs.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from parallax.shadow.discrepancy import parse_window

__all__ = [
    "DISCREPANCY_RATE_THRESHOLD_M3",
    "ARBITRATION_CONFLICT_RATE_THRESHOLD",
    "WRITE_ERROR_RATE_THRESHOLD",
    "APHELION_UNREACHABLE_THRESHOLD",
    "CROSSWALK_MISS_THRESHOLD",
    "CIRCUIT_OPEN_72H_MAX",
    "DEFAULT_DATA_QUALITY_FILTER",
    "TRAFFIC_SOURCE_PARTITIONS",
    "TRAFFIC_SOURCE_UNKNOWN",
    "record_traffic_source",
    "partition_by_traffic_source",
    "LoadResult",
    "discrepancy_rate",
    "arbitration_conflict_rate",
    "write_error_rate",
    "aphelion_unreachable_rate",
    "crosswalk_miss_rate",
    "circuit_open_count",
    "compute_all_rates",
    "load_records",
]

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Threshold constants (single source of truth — imported by metrics.py + CLI)
# ---------------------------------------------------------------------------

DISCREPANCY_RATE_THRESHOLD_M3 = 0.001  # 0.1% — Q2 Option B
ARBITRATION_CONFLICT_RATE_THRESHOLD = 0.01  # 1%
WRITE_ERROR_RATE_THRESHOLD = 0.0002  # 0.02%
APHELION_UNREACHABLE_THRESHOLD = 0.005  # 0.5%
CROSSWALK_MISS_THRESHOLD = 0.05  # 5% — measured at +48h gate (Q11)
CIRCUIT_OPEN_72H_MAX = 3  # absolute count over the 72h window

# Records flagged with these data_quality_flag values count toward production
# rates by default. ``cold_start`` is excluded so early-rollout noise does not
# pollute the DoD numerics.
#
# MED-LOWS-BUNDLED — promoted to module-level frozenset so
# ``_filter_by_quality`` doesn't rebuild the set per call.
DEFAULT_DATA_QUALITY_FILTER: tuple[str, ...] = ("normal", "corpus_immature")
_DEFAULT_DATA_QUALITY_SET: frozenset[str] = frozenset(DEFAULT_DATA_QUALITY_FILTER)

# ---------------------------------------------------------------------------
# Log directory + file pattern
# ---------------------------------------------------------------------------

_LOG_FILE_GLOB = "dual-read-decisions-*.jsonl"
_LOG_FILE_DATE_RE = re.compile(r"^dual-read-decisions-(\d{4}-\d{2}-\d{2})\.jsonl$")
_DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "parallax" / "logs"


def _resolve_log_dir(log_dir: Path | str | None) -> Path:
    """Resolve to an absolute path. ``.resolve()`` normalises any symlinks."""
    if log_dir is not None:
        return Path(log_dir).resolve()
    env = os.environ.get("DUAL_READ_LOG_DIR")
    if env:
        return Path(env).resolve()
    return _DEFAULT_LOG_DIR.resolve()


def _parse_timestamp(raw: str) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp string, defaulting to UTC if naive."""
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed


def _file_date(path: Path) -> _dt.date | None:
    """Extract the date from a ``dual-read-decisions-YYYY-MM-DD.jsonl`` filename."""
    match = _LOG_FILE_DATE_RE.match(path.name)
    if match is None:
        return None
    try:
        return _dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _normalize_outcome(record: dict[str, Any]) -> str | None:
    """Return the outcome string. Tolerates both field names (compat with shadow JSONL)."""
    val = record.get("outcome")
    if isinstance(val, str) and val:
        return val
    val = record.get("arbitration_outcome")
    if isinstance(val, str) and val:
        return val
    return None


TRAFFIC_SOURCE_UNKNOWN = "unknown"
TRAFFIC_SOURCE_PARTITIONS: tuple[str, ...] = ("natural", "synthetic", TRAFFIC_SOURCE_UNKNOWN)


def record_traffic_source(record: dict[str, Any]) -> str:
    """Partition one decision-log record by its recorded ``traffic_source``.

    Read-side semantics, and they are deliberately NOT the write-side
    semantics — the asymmetry is the whole point, so do not "fix" it:

    * **Write side** (``DualReadRouter._log_decision`` →
      ``discrepancy_live._normalize_traffic_source``) resolves an *absent or
      unrecognized* value to ``"natural"``. That is the fail-safe mandated by
      ``docs/m4-prep/traffic-gap-resolution.md`` §6: a live request arriving
      with no ``X-Parallax-Traffic-Source`` header is real production traffic
      and must never be discounted. Because that normalization runs
      unconditionally, every record written from that code onward carries an
      explicit ``"synthetic"`` or ``"natural"``.

    * **Read side** (here) maps a *missing* field to ``"unknown"``. A record
      with no ``traffic_source`` key was not written by a header-less request
      — it was written by a build that could not record the field at all, so
      its provenance is genuinely unknown. As of 2026-08-02 roughly 245k such
      records sit in the live 72h corpus and are known to be 100% synthetic
      burn-in. Read-defaulting them to ``"natural"`` would dump that backlog
      into the natural-labelled numbers and poison the exact Phase-2 gate
      (§3.3) the label exists to make measurable.

    This lives in the shared module rather than in either consumer because
    ``/metrics`` and ``scripts/dual_read_continuity_check.py`` are two
    authoritative gates on the same corpus. When they partitioned it
    differently — or when one partitioned and the other did not — they
    contradicted each other and could block a promotion between them.
    """
    raw = record.get("traffic_source")
    if not isinstance(raw, str):
        return TRAFFIC_SOURCE_UNKNOWN
    candidate = raw.strip().lower()
    if candidate in ("synthetic", "natural"):
        return candidate
    # Present but unrecognized: the writer only ever emits the two known
    # values, so this is foreign or hand-edited data. "Unknown" is the honest
    # answer — folding it into ``natural`` would attribute unaudited records
    # to the gate-bearing partition.
    return TRAFFIC_SOURCE_UNKNOWN


def partition_by_traffic_source(
    records: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group records by :func:`record_traffic_source`.

    Only partitions that actually have records appear as keys — callers that
    need a fixed set iterate :data:`TRAFFIC_SOURCE_PARTITIONS` themselves and
    decide what an empty partition means for their surface. ``/metrics``
    treats it as "emit no series"; the DoD CLI treats it as "cannot evaluate".
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        out.setdefault(record_traffic_source(record), []).append(record)
    return out


@dataclasses.dataclass(frozen=True)
class LoadResult:
    """Result of :func:`load_records` — records plus directory / parse health.

    Story H5 — surfaces ``dir_missing`` so the CLI can distinguish a
    misconfigured ``--log-dir`` (missing path) from a legitimately empty
    log dir during a quiet rollout window.

    Story MED-MALFORMED-COUNTER — surfaces ``malformed`` so callers can
    track JSONL parse failures without scanning the file twice. Lines
    that fail to parse (invalid JSON, non-dict payload, missing /
    unparseable ``timestamp``) increment this counter and a single
    aggregate WARNING is logged at the end of :func:`load_records` when
    any malformed lines were observed.
    """

    records: list[dict[str, Any]]
    dir_missing: bool
    malformed: int


def load_records(
    *,
    log_dir: Path | str | None = None,
    since: _dt.timedelta | None = None,
    now: _dt.datetime | None = None,
) -> LoadResult:
    """Load and parse all dual-read decision records under ``log_dir``.

    Records older than ``now - since`` (when ``since`` is given) are dropped.
    Records with unparseable JSON or unparseable timestamps are dropped
    silently from the record list — they cannot be window-filtered safely
    — but counted in ``LoadResult.malformed``. Returns records sorted by
    timestamp ascending.

    H5 — when the resolved log directory does not exist, returns a
    ``LoadResult(records=[], dir_missing=True, malformed=0)`` so the CLI
    can surface the misconfiguration as a distinct failure mode.
    """
    resolved_dir = _resolve_log_dir(log_dir)
    if not resolved_dir.is_dir():
        return LoadResult(records=[], dir_missing=True, malformed=0)

    cutoff: _dt.datetime | None = None
    if since is not None:
        anchor = now if now is not None else _dt.datetime.now(_dt.UTC)
        cutoff = anchor - since
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=_dt.UTC)

    records: list[tuple[_dt.datetime, dict[str, Any]]] = []
    malformed = 0
    for path in sorted(resolved_dir.glob(_LOG_FILE_GLOB)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        file_date = _file_date(path)
        file_in_window = cutoff is None or (file_date is not None and file_date >= cutoff.date())
        # Conservative file-level prefilter: skip files whose UTC date is
        # entirely before the cutoff date.
        if cutoff is not None and file_date is not None and file_date < cutoff.date():
            continue
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                if file_in_window:
                    malformed += 1
                continue
            if not isinstance(parsed, dict):
                if file_in_window:
                    malformed += 1
                continue
            ts_raw = parsed.get("timestamp")
            ts = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
            if ts is None:
                if file_in_window:
                    malformed += 1
                continue
            if cutoff is not None and ts < cutoff:
                continue
            records.append((ts, parsed))

    records.sort(key=lambda triple: triple[0])
    if malformed > 0:
        # MED-MALFORMED-COUNTER — single aggregate WARNING per call so
        # operators see the count without firehose log noise.
        _log.warning(
            "dual_read_metrics.malformed_lines_skipped",
            extra={
                "event": "dual_read_metrics.malformed_lines_skipped",
                "log_dir": str(resolved_dir),
                "count": malformed,
            },
        )
    return LoadResult(
        records=[r for _, r in records],
        dir_missing=False,
        malformed=malformed,
    )


def _filter_by_quality(
    records: Iterable[dict[str, Any]],
    data_quality_filter: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Keep only records whose ``data_quality_flag`` is in ``data_quality_filter``.

    Records missing the flag are treated as ``"normal"`` (default).
    """
    allowed: frozenset[str] | set[str] = (
        _DEFAULT_DATA_QUALITY_SET if data_quality_filter is None else set(data_quality_filter)
    )
    out: list[dict[str, Any]] = []
    for r in records:
        flag = r.get("data_quality_flag")
        flag = flag if isinstance(flag, str) and flag else "normal"
        if flag in allowed:
            out.append(r)
    return out


def _excluding_unreachable(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out ``aphelion_unreachable`` outcomes (denominator exclusion)."""
    return [r for r in records if _normalize_outcome(r) != "aphelion_unreachable"]


def _load_filtered(
    *,
    window: str,
    log_dir: Path | str | None,
    now: _dt.datetime | None,
    data_quality_filter: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Common path: parse window, load, filter by quality."""
    delta = parse_window(window)
    raw = load_records(log_dir=log_dir, since=delta, now=now)
    return _filter_by_quality(raw.records, data_quality_filter)


# ---------------------------------------------------------------------------
# Public rate functions
# ---------------------------------------------------------------------------


def discrepancy_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of in-window dual-read outcomes that are ``"diverge"``.

    Denominator excludes ``aphelion_unreachable``. Empty window → 0.0.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    denom_records = _excluding_unreachable(records)
    if not denom_records:
        return 0.0
    diverge = sum(1 for r in denom_records if _normalize_outcome(r) == "diverge")
    return diverge / len(denom_records)


def arbitration_conflict_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of dual-read outcomes that triggered an arbitration conflict.

    A record is a conflict when:
      - ``winning_source`` is ``"tie"`` or ``"fallback"`` (live arbitration
        verdict requires manual review), OR
      - ``conflict_event_id`` is a non-empty string (writer fired).

    Denominator excludes ``aphelion_unreachable``. Note that a plain
    ``"diverge"`` outcome alone is a discrepancy, NOT a conflict — the two
    metrics are intentionally distinct (Q1 Option A rule table treats
    most diverge cases as a clean parallax/aphelion win, not a conflict).
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    denom_records = _excluding_unreachable(records)
    if not denom_records:
        return 0.0
    conflicts = 0
    for r in denom_records:
        ws = r.get("winning_source")
        if isinstance(ws, str) and ws in ("tie", "fallback"):
            conflicts += 1
            continue
        eid = r.get("conflict_event_id")
        if isinstance(eid, str) and eid:
            conflicts += 1
    return conflicts / len(denom_records)


def write_error_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of dual-read outcomes that report a write error.

    Denominator excludes ``aphelion_unreachable``.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    denom_records = _excluding_unreachable(records)
    if not denom_records:
        return 0.0
    errors = sum(1 for r in denom_records if r.get("write_error_observed") is True)
    return errors / len(denom_records)


def aphelion_unreachable_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of in-window dual-read outcomes that are ``"aphelion_unreachable"``.

    Denominator: ALL outcomes (not excluded). Empty window → 0.0.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    if not records:
        return 0.0
    unreachable = sum(1 for r in records if _normalize_outcome(r) == "aphelion_unreachable")
    return unreachable / len(records)


def crosswalk_miss_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of in-window dual-read records with ``crosswalk_status == "miss"``.

    Denominator excludes ``aphelion_unreachable``. Per Q11, the operational
    measurement window is +48h from rollout.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    denom_records = _excluding_unreachable(records)
    if not denom_records:
        return 0.0
    misses = sum(1 for r in denom_records if r.get("crosswalk_status") == "miss")
    return misses / len(denom_records)


def circuit_open_count(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> int:
    """Count of in-window dual-read records flagged with circuit-breaker open.

    Returns an integer (not a rate). The DoD threshold is the absolute cap
    :data:`CIRCUIT_OPEN_72H_MAX` over a 72h window.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    return sum(1 for r in records if r.get("circuit_breaker_tripped") is True)


def compute_all_rates(
    records: list[dict[str, Any]],
    *,
    data_quality_filter: Sequence[str] | None = None,
) -> dict[str, float | int]:
    """Compute all 6 metrics from a single pre-loaded record list.

    Story MED-LOWS-BUNDLED — eliminates the N+1 file walks the CLI used
    to perform (one ``load_records`` per rate function = 6 walks per
    invocation).  Caller pre-loads the records once, hands them in, and
    receives a single dict of all metrics in one pass.
    """
    filtered = _filter_by_quality(records, data_quality_filter)
    denom = _excluding_unreachable(filtered)
    denom_n = len(denom)
    all_n = len(filtered)

    if denom_n == 0:
        d_rate = 0.0
        c_rate = 0.0
        w_rate = 0.0
        x_rate = 0.0
    else:
        d_rate = sum(1 for r in denom if _normalize_outcome(r) == "diverge") / denom_n
        conflicts = 0
        for r in denom:
            ws = r.get("winning_source")
            if isinstance(ws, str) and ws in ("tie", "fallback"):
                conflicts += 1
                continue
            eid = r.get("conflict_event_id")
            if isinstance(eid, str) and eid:
                conflicts += 1
        c_rate = conflicts / denom_n
        w_rate = sum(1 for r in denom if r.get("write_error_observed") is True) / denom_n
        x_rate = sum(1 for r in denom if r.get("crosswalk_status") == "miss") / denom_n

    if all_n == 0:
        u_rate = 0.0
    else:
        u_rate = sum(1 for r in filtered if _normalize_outcome(r) == "aphelion_unreachable") / all_n

    circuit = sum(1 for r in filtered if r.get("circuit_breaker_tripped") is True)

    return {
        "discrepancy_rate": d_rate,
        "arbitration_conflict_rate": c_rate,
        "write_error_rate": w_rate,
        "aphelion_unreachable_rate": u_rate,
        "crosswalk_miss_rate": x_rate,
        "circuit_open_count": circuit,
    }
