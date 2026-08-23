"""Mutation-hardening for ``parallax.server.routes.metrics`` (land/20260823 wave 4, S2).

Additive companion to ``tests/server/test_metrics_endpoint.py``,
``tests/server/test_metrics_dual_read_endpoint.py``,
``tests/server/test_metrics_never_on_wire_102.py`` and ``tests/observability/``.
Thirty semantic mutants were applied to a pristine tree one at a time against
that whole set; twenty were killed by it. Of the ten that survived, the tests
below close nine; the tenth (S2-M13) is excluded as an equivalent mutant rather
than killed — see the manifest, which backs the equivalence with 207,380
differential inputs and zero differing outputs. The patches and exit codes are
in ``mutations-w4-obs.json``.

The shape of what the existing suites could not see
---------------------------------------------------
They are strong on *presence* — "is this series on the wire", "does this alert
have a producer", "is a pathological counter key skipped" — which is what they
were written for, and it is why two thirds of the mutants died on contact. What
none of them assert is the arithmetic and the text underneath:

* **The three numbers every rate is defined against are never named.**
  ``_WINDOW`` ("1h"), ``_DUAL_READ_WINDOW`` ("72h") and ``_CACHE_TTL_SECONDS``
  (30.0) can each be changed freely: the tests assert that a gauge exists and
  that a record written "now" is counted, which is true under any window wide
  enough to contain it.
* **The caches are never observed expiring.** Every test calls
  ``_reset_cache_for_tests()`` and then scrapes once, so a cache that never
  expires at all behaves identically to a correct one.
* **Label escaping has no adversarial input.** Every fixture label value is a
  plain identifier, so the quote/newline/backslash rules in
  ``_format_prometheus_labels`` are exercised only where they are a no-op.
* **Empty-window and clock-skew edges are untested.** An empty shadow window
  and a future-stamped record are both states an operator's alert reads
  directly, and neither is constructed anywhere.

Expected values are literals throughout. Deriving "72h" from
``_DUAL_READ_WINDOW`` or the expiry point from ``_CACHE_TTL_SECONDS`` is
exactly what makes a constant untestable.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from parallax.server.routes import metrics as metrics_mod
from parallax.shadow.discrepancy import LoadResult

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_metrics_cache() -> Iterator[None]:
    """The module caches are process-global; drop them around every test."""
    metrics_mod._reset_cache_for_tests()
    yield
    metrics_mod._reset_cache_for_tests()


# ---------------------------------------------------------------------------
# The three numbers every rate on this endpoint is defined against
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_documented_windows_and_cache_ttl_are_the_shipped_values() -> None:
    """These three are operational contract, not implementation detail.

    ``_WINDOW`` is the rolling window the two shadow gauges are defined on.
    ``_DUAL_READ_WINDOW`` is the 72h DoD window every dual-read rate is measured
    over — the number the promotion gate is written against, mirrored in
    ``scripts/dual_read_continuity_check.py`` and in the alert rules.
    ``_CACHE_TTL_SECONDS`` bounds alerting latency at ``TTL + scrape_interval``,
    which the module docstring calls out as the trade it is making.

    Pinned as literals because nothing else in the suite mentions any of them:
    a test that writes one record and asserts it is counted passes under any
    window wide enough to contain it, which is every window.
    """
    assert metrics_mod._WINDOW == "1h"
    assert metrics_mod._DUAL_READ_WINDOW == "72h"
    assert metrics_mod._CACHE_TTL_SECONDS == 30.0


# ---------------------------------------------------------------------------
# The caches expire (and expire at 30s, not "eventually")
# ---------------------------------------------------------------------------


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Install a hand-advanced ``time.monotonic`` and return its mutable cell.

    The stub replaces the module's ``time`` *binding*, not an attribute on the
    stdlib module. ``metrics.py`` does ``import time``, so patching
    ``metrics_mod.time.monotonic`` would mutate the shared module object and
    stop the clock for every ``time.monotonic()`` caller in the process —
    pytest's own timing, prometheus_client, any background thread — for the
    duration of the test. monkeypatch would undo it, so the blast radius was
    bounded, but the patch read as module-local while it was not. metrics.py
    uses only ``time.monotonic`` (lines 357, 362, 378, 383), so a one-attribute
    namespace is a complete stand-in.
    """
    now = [1000.0]
    monkeypatch.setattr(metrics_mod, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.mark.unit
def test_shadow_cache_serves_within_thirty_seconds_and_recomputes_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache hit inside the TTL, a real recompute outside it.

    Two mutants live here and the existing suite sees neither: dropping the
    ``(now - _cache_at) < _CACHE_TTL_SECONDS`` half of the guard (the cache then
    never expires, so a scrape can serve indefinitely stale discrepancy rates),
    and widening the TTL itself. The clock is advanced by hand to 29s and 31s —
    literals, not ``TTL ± 1`` — so a widened TTL fails on the second leg.
    """
    calls: list[int] = []

    def _collect() -> dict[str, float]:
        calls.append(1)
        return {"discrepancy_rate": 0.0, "checksum_consistency": 1.0, "log_records_total": 0.0}

    monkeypatch.setattr(metrics_mod, "_collect_shadow_metrics", _collect)
    clock = _fake_clock(monkeypatch)

    metrics_mod._cached_shadow_metrics()
    assert len(calls) == 1

    clock[0] = 1029.0
    metrics_mod._cached_shadow_metrics()
    assert len(calls) == 1, "a scrape 29s later must still be served from the cache"

    clock[0] = 1031.0
    metrics_mod._cached_shadow_metrics()
    assert len(calls) == 2, "a scrape 31s later must recompute — the TTL is 30s"


@pytest.mark.unit
def test_dual_read_cache_serves_within_thirty_seconds_and_recomputes_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guard, same TTL, on the dual-read snapshot.

    This is the cache MED-METRICS-CACHE added specifically so a cache-miss
    scrape walks the ~90MB/72h decision-log corpus once rather than three
    times, so "does it actually expire" is the question that makes it a cache
    rather than a one-shot memo.
    """
    calls: list[int] = []
    snapshot = metrics_mod._DualReadSnapshot(
        rates={}, counts={}, newest_age={}, dir_missing=0.0, compute_error=0.0
    )

    def _collect() -> metrics_mod._DualReadSnapshot:
        calls.append(1)
        return snapshot

    monkeypatch.setattr(metrics_mod, "_collect_dual_read_metrics", _collect)
    clock = _fake_clock(monkeypatch)

    metrics_mod._cached_dual_read_metrics()
    clock[0] = 1029.0
    metrics_mod._cached_dual_read_metrics()
    assert len(calls) == 1

    clock[0] = 1031.0
    metrics_mod._cached_dual_read_metrics()
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Prometheus text-format escaping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_label_values_escape_backslash_before_quote_and_newline() -> None:
    """Escaping order is load-bearing, and only a hostile value reveals it.

    The backslash rule must run FIRST. Running it last re-escapes the
    backslashes the quote and newline rules have just introduced, so a value
    containing one quote comes back with a doubled backslash and the exposition
    line stops parsing — a scrape-breaking bug that no fixture in the existing
    suites can produce, because every label value in them is a plain
    identifier on which all three rules are no-ops.
    """
    rendered = metrics_mod._format_prometheus_labels({"traffic_source": 'a\\b"c\nd'})
    assert rendered == '{traffic_source="a\\\\b\\"c\\nd"}'


# ---------------------------------------------------------------------------
# Freshness arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_future_stamped_record_is_clamped_to_zero_age() -> None:
    """Clock skew reads as "as fresh as possible", never as a negative age.

    The writer and the scraping host are different machines, so a record
    stamped slightly in the future is an ordinary occurrence. Without the clamp
    the gauge goes negative, which a ``> threshold`` freshness alert reads as
    healthy and a dashboard axis renders as nonsense — a silent failure in the
    signal that exists to notice silence.
    """
    now = _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.UTC)
    records = [{"timestamp": "2026-01-01T00:05:00+00:00"}]
    assert metrics_mod._newest_record_age_seconds(records, now=now) == 0.0


# ---------------------------------------------------------------------------
# The empty-window edge
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_shadow_window_reports_full_consistency_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No records at all means "nothing inconsistent", i.e. 1.0.

    ``parallax_shadow_checksum_consistency`` is read by an alert that fires on a
    LOW value, so the empty-window default is the difference between a quiet
    hour and a page. Zero records is the state every fresh deploy and every
    idle night passes through, and no existing test constructs it — they all
    write at least one record first.
    """
    monkeypatch.setattr(
        metrics_mod,
        "load_records",
        lambda **_kwargs: LoadResult(records=[], raw_lines=[], malformed=0),
    )

    collected = metrics_mod._collect_shadow_metrics()

    assert collected["checksum_consistency"] == 1.0
    assert collected["discrepancy_rate"] == 0.0
    assert collected["log_records_total"] == 0.0


# ---------------------------------------------------------------------------
# The arbitration policy info-metric
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_policy_version_info_metric_carries_a_constant_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An info-metric's *value* is the join key's carrier and must be 1.0.

    The prometheus_client info idiom is a constant-1.0 gauge whose label holds
    the fact; PromQL joins with ``* on(policy_version) group_left`` and a 0.0
    silently zeroes the joined result rather than failing. The existing
    assertions check that the series and its label are present, which a 0.0
    also satisfies.
    """
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("PARALLAX_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(_REPO_ROOT / "parallax" / "schema.sql"))
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)
    metrics_mod._reset_cache_for_tests()

    payload = metrics_mod._build_payload()

    lines = [
        line
        for line in payload.splitlines()
        if line.startswith("parallax_arbitration_policy_version{")
    ]
    assert len(lines) == 1, f"expected exactly one info series, got {lines!r}"
    assert lines[0].endswith(" 1.0"), lines[0]
