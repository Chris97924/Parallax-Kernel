"""Mutation-hardening for ``parallax.canary.exporter`` (overnight-20260816 S3).

Additive companion to ``test_canary_exporter_106.py``. Each test pins an
invariant the module docstring states as load-bearing but which no assertion
held down — every one below was found by a semantic mutant that survived the
existing suite.

The gap the existing suite has is that it always drives the exporter through a
healthy, well-formed store: a real CLI run, latencies inside the bucket range,
a store path that either is a database or does not exist. The degraded shapes —
a latency above the top bucket, a path that is a directory, an audit row with a
NULL latency, a stale cache — are the ones where "never raises" and "read-only,
always" have to actually hold.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from prometheus_client.parser import text_string_to_metric_families

from parallax.canary.audit_log import AUDIT_DB_ENV, AuditLog, make_record
from parallax.canary.exporter import (
    CANARY_DURATION_BUCKETS_MS,
    _connect_readonly,
    cached_canary_snapshot,
    collect_canary_snapshot,
    reset_cache_for_tests,
    resolve_store_path,
)
from parallax.canary.instrument import CanaryRequestRecorder
from parallax.canary.outcomes import OutcomeStore
from parallax.server.routes.metrics import _build_payload, _reset_cache_for_tests

_STAGE = "m4_1pct"


@pytest.fixture
def store_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp canary DB that both the writer and the exporter resolve to."""
    path = tmp_path / "canary_audit.db"
    monkeypatch.setenv(AUDIT_DB_ENV, str(path))
    _reset_cache_for_tests()
    yield path
    _reset_cache_for_tests()


def _scrape() -> dict[str, float]:
    """``{sample_name{sorted labels} -> value}`` for every parallax_canary_* sample."""
    _reset_cache_for_tests()
    out: dict[str, float] = {}
    for family in text_string_to_metric_families(_build_payload()):
        if not family.name.startswith("parallax_canary_"):
            continue
        for sample in family.samples:
            labels = ",".join(f"{k}={v}" for k, v in sorted(sample.labels.items()))
            out[f"{sample.name}{{{labels}}}" if labels else sample.name] = sample.value
    return out


def _record(
    store: Path,
    *,
    outcome: str,
    status: int = 200,
    stage: str = _STAGE,
    latency_ms: float | None = None,
) -> str:
    """Write one canary event, optionally pinning its audit-log latency."""
    audit = AuditLog(db_path=store)
    outcomes = OutcomeStore(db_path=store)
    try:
        recorder = CanaryRequestRecorder(audit_log=audit, outcome_store=outcomes, stage=stage)
        with recorder.request() as span:
            span.outcome = outcome
            span.response_status = status
        if latency_ms is not None:
            audit.record(
                make_record(
                    event_id=span.event_id,
                    response_status=status,
                    latency_ms=latency_ms,
                    idempotency_hit=False,
                )
            )
        return span.event_id
    finally:
        audit.close()
        outcomes.close()


# ---------------------------------------------------------------------------
# Histogram overflow
# ---------------------------------------------------------------------------


def test_a_latency_above_the_top_bucket_still_reaches_the_count(
    store_path: Path,
) -> None:
    """An observation past the last finite bound belongs to +Inf, not to nothing.

    Every latency in the existing suite sits inside the bucket range, so the
    overflow path was unasserted. T3 reads a p99 off this histogram: if a
    5-second canary request were dropped from the count instead of landing in
    +Inf, the slowest requests in the fleet would be the ones invisible to the
    latency gate, and p99 would improve as things got worse.
    """
    slow_ms = 5000.0
    assert slow_ms > CANARY_DURATION_BUCKETS_MS[-1]
    _record(store_path, outcome="ok", latency_ms=slow_ms)

    scraped = _scrape()

    assert scraped[f"parallax_canary_request_duration_ms_count{{stage={_STAGE}}}"] == 1.0
    assert scraped[f"parallax_canary_request_duration_ms_sum{{stage={_STAGE}}}"] == pytest.approx(
        slow_ms
    )
    top = CANARY_DURATION_BUCKETS_MS[-1]
    assert scraped[f"parallax_canary_request_duration_ms_bucket{{le={top},stage={_STAGE}}}"] == 0.0


# ---------------------------------------------------------------------------
# Read-only, always
# ---------------------------------------------------------------------------


def test_the_scrape_connection_cannot_write_to_an_existing_store(
    store_path: Path,
) -> None:
    """``mode=ro`` must hold on a store that EXISTS, which is the only case it can.

    The existing "a scrape does not create the store" test passes with or
    without ``mode=ro``, because the missing-file branch returns before
    ``sqlite3.connect`` is ever reached. The read-only flag only does work once
    there is a file to open, so that is where it has to be asserted: a scrape
    must not be able to mutate the canary ledger it is reporting on.
    """
    _record(store_path, outcome="ok")

    conn = _connect_readonly(store_path)
    assert conn is not None
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM canary_outcomes")
    finally:
        conn.close()

    # The ledger is untouched.
    assert collect_canary_snapshot(store_path).total_events == 1


def test_a_directory_at_the_store_path_is_a_quiet_absence_not_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A path that is not a file is "no store", and must not log on every scrape.

    ``is_file()`` rather than ``exists()`` is what keeps a directory from
    reaching ``sqlite3.connect``. Relaxing it does still degrade to an empty
    snapshot — but only via the exception handler, which emits a WARNING. At a
    15s scrape interval that is 5760 warnings a day for a misconfiguration the
    module docstring calls a normal state, which is how operators learn to
    ignore the level.
    """
    not_a_db = tmp_path / "canary_dir"
    not_a_db.mkdir()

    with caplog.at_level(logging.DEBUG, logger="parallax.canary.exporter"):
        snapshot = collect_canary_snapshot(not_a_db)

    assert snapshot.store_present is False
    assert snapshot.total_events == 0
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], f"a non-file store path must not warn, got: {warnings}"


def test_an_audit_row_with_no_latency_does_not_break_the_scrape(
    store_path: Path,
) -> None:
    """A NULL ``latency_ms`` is skipped, not coerced.

    ``latency_ms`` is nullable in the audit schema, and the duration query
    excludes NULLs precisely so the exporter never calls ``float(None)``.
    ``collect_canary_snapshot`` has no ``except`` around its reads — only a
    ``finally`` that closes the connection — so a TypeError there does not
    degrade to an empty snapshot, it propagates and takes ``/metrics`` down for
    every other series on the endpoint.
    """
    event_id = _record(store_path, outcome="ok", latency_ms=42.0)
    audit = AuditLog(db_path=store_path)
    try:
        assert audit.record(
            make_record(
                event_id=event_id,
                response_status=200,
                latency_ms=None,
                idempotency_hit=False,
            )
        )
    finally:
        audit.close()

    snapshot = collect_canary_snapshot(store_path)

    assert snapshot.durations_ms.get(_STAGE, []) == []
    assert snapshot.events[(_STAGE, "ok")] == 1
    scraped = _scrape()
    assert scraped[f"parallax_canary_request_duration_ms_count{{stage={_STAGE}}}"] == 0.0
    assert scraped[f"parallax_canary_events_total{{outcome=ok,stage={_STAGE}}}"] == 1.0


# ---------------------------------------------------------------------------
# Store-path resolution
# ---------------------------------------------------------------------------


def test_an_explicit_store_path_beats_the_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Argument first, then ``PARALLAX_CANARY_AUDIT_DB``, then the cwd default.

    The docstring pins this order so one env var can point the CLI writer and
    this reader at the same file while a caller stays free to override. Reversed,
    an explicit path would be silently ignored on any host that sets the var.
    """
    monkeypatch.setenv(AUDIT_DB_ENV, str(tmp_path / "from_env.db"))
    explicit = tmp_path / "explicit.db"

    assert resolve_store_path(explicit) == explicit
    assert resolve_store_path(None) == tmp_path / "from_env.db"


# ---------------------------------------------------------------------------
# Snapshot cache
# ---------------------------------------------------------------------------


def test_the_snapshot_cache_expires_so_a_later_scrape_sees_new_events(
    store_path: Path,
) -> None:
    """The cache is a scrape-rate limiter, not a freeze.

    Nothing asserted that the TTL branch ever lets go: a cache that never
    expires would serve the first snapshot of the process forever, so a canary
    that started after the first scrape would read as "no store" indefinitely
    while ``store_present 0`` told the oncall to distrust the zeros.
    """
    reset_cache_for_tests()
    base = time.monotonic()

    with patch("time.monotonic", return_value=base):
        assert cached_canary_snapshot(store_path).store_present is False

    _record(store_path, outcome="ok")

    # Still inside the TTL: the cached (empty) read is served.
    with patch("time.monotonic", return_value=base + 1.0):
        assert cached_canary_snapshot(store_path).store_present is False

    # An hour later the cache must have let go and re-read the store.
    with patch("time.monotonic", return_value=base + 3600.0):
        refreshed = cached_canary_snapshot(store_path)

    assert refreshed.store_present is True
    assert refreshed.total_events == 1
    reset_cache_for_tests()
