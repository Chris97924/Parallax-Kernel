"""Tests for :mod:`parallax.canary.dod_prometheus` (Option A1, 2026-06-07).

The per-stage ``parallax canary --dod`` summary reads the canary shadow
observer's Prometheus counters instead of the SQLite ``canary_outcomes``
table. These tests patch ``urllib.request.urlopen`` to return canned
``/api/v1/query`` envelopes so the query wiring, stage→sN mapping, rate
computation, sample-size gating, and graceful-degradation paths are all
exercised without a live Prometheus.

Companion to ``tests/dod/test_dod.py`` — those tests cover the (retained but
CLI-unwired) SQLite ``compute_dod`` path and must NOT be weakened here.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from collections.abc import Callable

import pytest

from parallax.canary.dod import DOD_THRESHOLD, DodMetric, DodVerdict
from parallax.canary.dod_prometheus import (
    DOD_STAGE_TO_SHADOW,
    _prom_instant_query,
    compute_shadow_dod,
)

# ----------------------------------------------------------------------
# Canned-Prometheus harness
# ----------------------------------------------------------------------


def _scalar_envelope(value: float | int) -> bytes:
    """A Prometheus instant-query success envelope with one scalar result."""
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {},
                        "value": [1717_700_000.0, str(value)],
                    }
                ],
            },
        }
    ).encode()


def _empty_envelope() -> bytes:
    """A success envelope with an empty result set (zero-traffic stage)."""
    return json.dumps(
        {"status": "success", "data": {"resultType": "vector", "result": []}}
    ).encode()


class _FakeResponse(io.BytesIO):
    """Minimal context-manager stand-in for ``urlopen``'s return value."""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _install_router(
    monkeypatch: pytest.MonkeyPatch,
    router: Callable[[str], bytes],
    *,
    captured: list[str] | None = None,
) -> None:
    """Patch ``urllib.request.urlopen`` to dispatch on the request URL.

    ``router`` maps the full request URL (which carries the urlencoded
    PromQL ``query=`` param) to a canned response body. If ``captured`` is
    provided, each requested URL is appended to it for assertion.
    """

    def _fake_urlopen(req: object, *, timeout: float = 0.0) -> _FakeResponse:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if captured is not None:
            captured.append(url)
        return _FakeResponse(router(url))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


def _make_router(
    *,
    attempts: float | int | None,
    diverge: float | int | None = 0,
    aphelion: float | int | None = 0,
) -> Callable[[str], bytes]:
    """Build a URL→body router keyed on the PromQL each metric issues.

    ``None`` for any leg yields an empty result set (Prometheus answered but
    had no series). The legs are distinguished by substrings unique to each
    query (``attempts_total`` vs ``outcome="diverge"`` vs
    ``outcome="aphelion_unreachable"``).
    """

    def _router(url: str) -> bytes:
        # urlencoded, so quotes/braces are percent-escaped — match on the
        # stable encoded tokens.
        if "aphelion_unreachable" in url:
            return _empty_envelope() if aphelion is None else _scalar_envelope(aphelion)
        if "diverge" in url:
            return _empty_envelope() if diverge is None else _scalar_envelope(diverge)
        # attempts query: outcomes_total carries an outcome= filter; the
        # attempts_total query does not, so anything left is attempts.
        return _empty_envelope() if attempts is None else _scalar_envelope(attempts)

    return _router


def _metric(report, metric: DodMetric):
    return next(m for m in report.metrics if m.metric == metric)


# ----------------------------------------------------------------------
# _prom_instant_query — unit-level degradation
# ----------------------------------------------------------------------


def test_prom_instant_query_returns_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_router(monkeypatch, lambda _url: _scalar_envelope(42.5))
    assert _prom_instant_query("http://prom:9090", "up") == 42.5


def test_prom_instant_query_empty_result_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_router(monkeypatch, lambda _url: _empty_envelope())
    assert _prom_instant_query("http://prom:9090", "up") is None


def test_prom_instant_query_connection_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(req: object, *, timeout: float = 0.0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert _prom_instant_query("http://prom:9090", "up") is None


def test_prom_instant_query_bad_json_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_router(monkeypatch, lambda _url: b"<html>502 Bad Gateway</html>")
    assert _prom_instant_query("http://prom:9090", "up") is None


def test_prom_instant_query_non_success_status_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"status": "error", "errorType": "bad_data"}).encode()
    _install_router(monkeypatch, lambda _url: body)
    assert _prom_instant_query("http://prom:9090", "up") is None


# ----------------------------------------------------------------------
# Stage mapping
# ----------------------------------------------------------------------


def test_stage_mapping_issues_queries_with_shadow_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--stage m4_10pct`` must query ``stage="s2"`` (URL-encoded)."""
    captured: list[str] = []
    _install_router(
        monkeypatch,
        _make_router(attempts=100, diverge=0, aphelion=0),
        captured=captured,
    )

    compute_shadow_dod(stage="m4_10pct", prom_url="http://prom:9090")

    assert captured, "expected at least one Prometheus query"
    # urlencoded stage="s2" → stage%3D%22s2%22
    assert all("s2" in url for url in captured), captured
    # And never the other stages' labels.
    for other in ("s1", "s3", "s4"):
        assert all(other not in url for url in captured), (other, captured)


def test_all_stages_map_to_expected_shadow_label() -> None:
    assert DOD_STAGE_TO_SHADOW == {
        "m4_1pct": "s1",
        "m4_10pct": "s2",
        "m4_50pct": "s3",
        "m4_100pct": "s4",
    }


def test_unknown_stage_raises() -> None:
    with pytest.raises(ValueError, match="Unknown canary stage"):
        compute_shadow_dod(stage="m4_99pct", prom_url="http://prom:9090")


# ----------------------------------------------------------------------
# discrepancy_rate PASS / FAIL
# ----------------------------------------------------------------------


def test_discrepancy_rate_pass_when_below_threshold_and_sample_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1000 attempts, 1 diverge → 0.1% < 0.5% threshold, sample >= 50 → PASS.
    _install_router(monkeypatch, _make_router(attempts=1000, diverge=1, aphelion=0))

    report = compute_shadow_dod(stage="m4_1pct", prom_url="http://prom:9090")
    disc = _metric(report, DodMetric.DISCREPANCY_RATE)

    assert disc.verdict == DodVerdict.PASS
    assert disc.observed == pytest.approx(0.001)
    assert disc.threshold == DOD_THRESHOLD[DodMetric.DISCREPANCY_RATE]
    assert disc.sample_size == 1000


def test_discrepancy_rate_fail_when_at_or_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1000 attempts, 10 diverge → 1.0% >= 0.5% threshold, sample ok → FAIL.
    _install_router(monkeypatch, _make_router(attempts=1000, diverge=10, aphelion=0))

    report = compute_shadow_dod(stage="m4_50pct", prom_url="http://prom:9090")
    disc = _metric(report, DodMetric.DISCREPANCY_RATE)

    assert disc.verdict == DodVerdict.FAIL
    assert disc.observed == pytest.approx(0.01)
    assert report.overall == DodVerdict.FAIL


def test_discrepancy_rate_boundary_exactly_threshold_is_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rate exactly == 0.005 must FAIL (PASS is strictly ``< threshold``)."""
    # 1000 attempts, 5 diverge → exactly 0.5%.
    _install_router(monkeypatch, _make_router(attempts=1000, diverge=5, aphelion=0))

    report = compute_shadow_dod(stage="m4_1pct", prom_url="http://prom:9090")
    disc = _metric(report, DodMetric.DISCREPANCY_RATE)
    assert disc.observed == pytest.approx(0.005)
    assert disc.verdict == DodVerdict.FAIL


# ----------------------------------------------------------------------
# aphelion_unreachable_rate verdict path
# ----------------------------------------------------------------------


def test_aphelion_unreachable_rate_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1000 attempts, 20 aphelion_unreachable → 2% >= 0.5% → FAIL.
    _install_router(monkeypatch, _make_router(attempts=1000, diverge=0, aphelion=20))

    report = compute_shadow_dod(stage="m4_100pct", prom_url="http://prom:9090")
    aph = _metric(report, DodMetric.APHELION_UNREACHABLE_RATE)

    assert aph.metric == DodMetric.APHELION_UNREACHABLE_RATE
    assert aph.observed == pytest.approx(0.02)
    assert aph.verdict == DodVerdict.FAIL
    assert report.overall == DodVerdict.FAIL


def test_aphelion_unreachable_rate_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1000 attempts, 0 unreachable, 0 diverge → all PASS.
    _install_router(monkeypatch, _make_router(attempts=1000, diverge=0, aphelion=0))

    report = compute_shadow_dod(stage="m4_10pct", prom_url="http://prom:9090")
    aph = _metric(report, DodMetric.APHELION_UNREACHABLE_RATE)
    disc = _metric(report, DodMetric.DISCREPANCY_RATE)
    min_hits = _metric(report, DodMetric.MIN_HITS)

    assert aph.verdict == DodVerdict.PASS
    assert disc.verdict == DodVerdict.PASS
    assert min_hits.verdict == DodVerdict.PASS
    assert report.overall == DodVerdict.PASS


# ----------------------------------------------------------------------
# Sample-size gating
# ----------------------------------------------------------------------


def test_sample_below_floor_all_metrics_insufficient_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """< 50 attempts → both rate metrics AND min_hits INSUFFICIENT_DATA."""
    _install_router(monkeypatch, _make_router(attempts=10, diverge=0, aphelion=0))

    report = compute_shadow_dod(stage="m4_1pct", prom_url="http://prom:9090")

    for metric in (
        DodMetric.DISCREPANCY_RATE,
        DodMetric.APHELION_UNREACHABLE_RATE,
        DodMetric.MIN_HITS,
    ):
        assert _metric(report, metric).verdict == DodVerdict.INSUFFICIENT_DATA, metric
    assert _metric(report, DodMetric.MIN_HITS).sample_size == 10
    assert report.overall == DodVerdict.INSUFFICIENT_DATA


def test_report_has_exactly_three_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shadow summary reports exactly DISCREPANCY / APHELION / MIN_HITS —
    error_rate, p99_latency, data_loss are deferred to the T1-T5 alerts.
    """
    _install_router(monkeypatch, _make_router(attempts=1000, diverge=0, aphelion=0))

    report = compute_shadow_dod(stage="m4_1pct", prom_url="http://prom:9090")

    assert {m.metric for m in report.metrics} == {
        DodMetric.DISCREPANCY_RATE,
        DodMetric.APHELION_UNREACHABLE_RATE,
        DodMetric.MIN_HITS,
    }
    # The deferred metrics must NOT appear.
    assert DodMetric.ERROR_RATE not in {m.metric for m in report.metrics}
    assert DodMetric.P99_LATENCY_MS not in {m.metric for m in report.metrics}
    assert DodMetric.DATA_LOSS_COUNT not in {m.metric for m in report.metrics}


# ----------------------------------------------------------------------
# Graceful degradation — Prometheus unreachable / empty
# ----------------------------------------------------------------------


def test_prometheus_unreachable_degrades_to_insufficient_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A total Prometheus outage must NOT raise — all metrics INSUFFICIENT_DATA."""

    def _boom(req: object, *, timeout: float = 0.0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    report = compute_shadow_dod(stage="m4_1pct", prom_url="http://prom:9090")

    assert report.overall == DodVerdict.INSUFFICIENT_DATA
    for m in report.metrics:
        assert m.verdict == DodVerdict.INSUFFICIENT_DATA
    assert _metric(report, DodMetric.MIN_HITS).sample_size == 0


def test_empty_result_set_degrades_to_insufficient_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prometheus answers but has no series (zero-traffic stage) → sample 0."""
    _install_router(monkeypatch, lambda _url: _empty_envelope())

    report = compute_shadow_dod(stage="m4_1pct", prom_url="http://prom:9090")

    assert _metric(report, DodMetric.MIN_HITS).sample_size == 0
    assert report.overall == DodVerdict.INSUFFICIENT_DATA


def test_attempts_present_but_diverge_query_fails_is_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If attempts is known but the diverge leg returns None, the
    discrepancy verdict degrades to INSUFFICIENT_DATA (a needed query could
    not be answered) while aphelion can still PASS.
    """
    _install_router(
        monkeypatch,
        _make_router(attempts=1000, diverge=None, aphelion=0),
    )

    report = compute_shadow_dod(stage="m4_1pct", prom_url="http://prom:9090")

    assert _metric(report, DodMetric.DISCREPANCY_RATE).verdict == (DodVerdict.INSUFFICIENT_DATA)
    # aphelion leg answered (0 / 1000) and sample is fine → PASS.
    assert _metric(report, DodMetric.APHELION_UNREACHABLE_RATE).verdict == (DodVerdict.PASS)
    # min_hits still PASS on 1000 attempts.
    assert _metric(report, DodMetric.MIN_HITS).verdict == DodVerdict.PASS
    # Aggregate: one INSUFFICIENT_DATA, no FAIL → INSUFFICIENT_DATA.
    assert report.overall == DodVerdict.INSUFFICIENT_DATA


# ----------------------------------------------------------------------
# Window param threads into the increase() range selector
# ----------------------------------------------------------------------


def test_window_days_threads_into_query(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    _install_router(
        monkeypatch,
        _make_router(attempts=1000, diverge=0, aphelion=0),
        captured=captured,
    )

    compute_shadow_dod(stage="m4_1pct", window_days=3, prom_url="http://prom:9090")

    # urlencoded "[3d]" → %5B3d%5D
    assert all("3d" in url for url in captured), captured
