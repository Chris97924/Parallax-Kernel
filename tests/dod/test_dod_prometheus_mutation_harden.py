"""Mutation-hardening for ``parallax.canary.dod_prometheus`` (S9).

Companion to ``tests/dod/test_dod_prometheus.py``, which covers the query
wiring, the stage mapping and the documented degradation paths. Its canned
envelopes always carry exactly one result whose value is a whole number, and
its sample sizes are 10 or 1000 against a floor of 50 — so the parsing and
arithmetic around those happy shapes is unpinned. Each test below was written
against a semantic mutant that survived the whole suite:

  * ``attempts == 0`` — a stage whose series exists but did not move in the
    window — is never simulated, so the ``attempts not in (None, 0)`` guard
    that prevents a ZeroDivisionError could be relaxed to a plain ``is not
    None`` and the gate would crash instead of degrading.
  * ``sample_size = int(attempts)`` truncates, and ``increase()`` returns
    floats. No envelope carries a fractional value, so ``round`` for ``int``
    survived — it promotes a 49.6-attempt stage over the MIN_HITS floor.
  * Both floor comparisons (10 vs 1000 against 50) and the INSUFFICIENT
    branch's ``observed`` value had no boundary or value assertions.
  * ``_prom_instant_query`` was only ever handed a single-result envelope, a
    non-JSON body, or a non-success status: a JSON payload that is a *list*
    walks straight into ``payload.get`` unless the ``isinstance`` guard
    holds, a non-numeric value string relies on the ``ValueError`` arm, and
    ``result[0]`` versus ``result[-1]`` is unobservable with one result.
  * No test passes ``until``, asserts ``report.stage``, or reads the report's
    window stamps, and every ``prom_url`` is free of a trailing slash.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import urllib.request
from collections.abc import Callable

import pytest

from parallax.canary.dod import DodMetric, DodVerdict
from parallax.canary.dod_prometheus import (
    _MIN_HITS_FLOOR,
    _prom_instant_query,
    compute_shadow_dod,
)

_PROM = "http://prom:9090"


# ----------------------------------------------------------------------
# Canned-Prometheus harness (kept local so this file stands alone)
# ----------------------------------------------------------------------


def _envelope(*values: float | int | str) -> bytes:
    """A success envelope carrying one result per supplied value."""
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"series": str(i)}, "value": [1717_700_000.0, str(v)]}
                    for i, v in enumerate(values)
                ],
            },
        }
    ).encode()


def _empty_envelope() -> bytes:
    return json.dumps(
        {"status": "success", "data": {"resultType": "vector", "result": []}}
    ).encode()


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _install_router(
    monkeypatch: pytest.MonkeyPatch,
    router: Callable[[str], bytes],
    *,
    captured: list[str] | None = None,
    timeouts: list[float] | None = None,
) -> None:
    def _fake_urlopen(req: object, *, timeout: float = 0.0) -> _FakeResponse:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if captured is not None:
            captured.append(url)
        if timeouts is not None:
            timeouts.append(timeout)
        return _FakeResponse(router(url))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


def _make_router(
    *,
    attempts: float | int | None,
    diverge: float | int | None = 0,
    aphelion: float | int | None = 0,
) -> Callable[[str], bytes]:
    """Route each of the three legs by a substring unique to its PromQL."""

    def _router(url: str) -> bytes:
        if "aphelion_unreachable" in url:
            return _empty_envelope() if aphelion is None else _envelope(aphelion)
        if "diverge" in url:
            return _empty_envelope() if diverge is None else _envelope(diverge)
        return _empty_envelope() if attempts is None else _envelope(attempts)

    return _router


def _metric(report, metric: DodMetric):
    return next(m for m in report.metrics if m.metric == metric)


# ===========================================================================
# Zero attempts — the divide-by-zero guard
# ===========================================================================


@pytest.mark.unit
class TestZeroAttempts:
    def test_zero_attempts_degrades_instead_of_dividing_by_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stage whose counters exist but did not move must not crash the gate.

        Prometheus answers ``0`` (not an empty vector) for a series that exists
        and had no increase in the window — the normal shape for a stage that
        has been provisioned but is taking no traffic yet. The suite only ever
        simulates *absent* series, so the ``attempts not in (None, 0)`` guard
        could be relaxed to ``attempts is not None`` and every ``--dod`` run
        against a quiet stage would raise ZeroDivisionError out of a code path
        whose whole contract is "never crash the caller".
        """
        _install_router(monkeypatch, _make_router(attempts=0, diverge=0, aphelion=0))

        report = compute_shadow_dod(stage="m4_1pct", prom_url=_PROM)

        assert _metric(report, DodMetric.MIN_HITS).sample_size == 0
        for metric in (DodMetric.DISCREPANCY_RATE, DodMetric.APHELION_UNREACHABLE_RATE):
            result = _metric(report, metric)
            assert result.verdict == DodVerdict.INSUFFICIENT_DATA
            assert result.observed == 0.0
        assert report.overall == DodVerdict.INSUFFICIENT_DATA

    def test_zero_attempts_with_a_nonzero_numerator_still_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inconsistent-scrape shape: bad outcomes recorded, no attempts.

        This is the arrangement that actually reaches the division — the
        numerator is present and non-zero while the denominator is 0.
        """
        _install_router(monkeypatch, _make_router(attempts=0, diverge=3, aphelion=2))

        report = compute_shadow_dod(stage="m4_1pct", prom_url=_PROM)

        assert _metric(report, DodMetric.DISCREPANCY_RATE).verdict == DodVerdict.INSUFFICIENT_DATA
        assert _metric(report, DodMetric.DISCREPANCY_RATE).observed == 0.0


# ===========================================================================
# Sample-size derivation and the MIN_HITS floor
# ===========================================================================


@pytest.mark.unit
class TestSampleSizeFromIncrease:
    def test_fractional_attempts_are_truncated_not_rounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``increase()`` extrapolates, so its result is a float.

        Rounding instead of truncating promotes a stage that has not actually
        reached the floor: 49.6 observed attempts would be reported as 50 and
        MIN_HITS would flip from INSUFFICIENT_DATA to PASS. Every canned
        envelope in the suite carries a whole number, so the coercion was free
        to change.
        """
        _install_router(monkeypatch, _make_router(attempts=49.6, diverge=0, aphelion=0))

        report = compute_shadow_dod(stage="m4_1pct", prom_url=_PROM)

        min_hits = _metric(report, DodMetric.MIN_HITS)
        assert min_hits.sample_size == 49
        assert min_hits.observed == 49.0
        assert min_hits.verdict == DodVerdict.INSUFFICIENT_DATA

    def test_sample_size_exactly_at_the_floor_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The floor is ``>= 50``; the suite brackets it with 10 and 1000.

        Tightening it to ``>`` — or moving the literal — keeps a stage that has
        collected exactly the required sample at INSUFFICIENT_DATA forever.
        """
        _install_router(monkeypatch, _make_router(attempts=_MIN_HITS_FLOOR, diverge=0, aphelion=0))

        report = compute_shadow_dod(stage="m4_1pct", prom_url=_PROM)

        assert _metric(report, DodMetric.MIN_HITS).verdict == DodVerdict.PASS
        assert _metric(report, DodMetric.DISCREPANCY_RATE).verdict == DodVerdict.PASS
        assert report.overall == DodVerdict.PASS

    def test_sample_size_one_below_the_floor_is_insufficient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_router(
            monkeypatch, _make_router(attempts=_MIN_HITS_FLOOR - 1, diverge=0, aphelion=0)
        )

        report = compute_shadow_dod(stage="m4_1pct", prom_url=_PROM)

        for metric in (
            DodMetric.MIN_HITS,
            DodMetric.DISCREPANCY_RATE,
            DodMetric.APHELION_UNREACHABLE_RATE,
        ):
            assert _metric(report, metric).verdict == DodVerdict.INSUFFICIENT_DATA, metric


@pytest.mark.unit
class TestInsufficientDataStillReportsTheRate:
    def test_observed_rate_is_computed_even_when_the_sample_is_too_small(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The verdict is INSUFFICIENT_DATA; the number must still be the truth.

        ``_rate_metric`` deliberately computes the rate before returning the
        degraded verdict, so an operator reading the summary sees *why* the
        stage is being held. Collapsing that arm to a bare ``0.0`` — which no
        test could see, because none asserts ``observed`` on a degraded metric —
        renders a stage that is diverging on 20% of its (few) requests as a
        flat 0.0% next to the word "insufficient".
        """
        _install_router(monkeypatch, _make_router(attempts=10, diverge=2, aphelion=1))

        report = compute_shadow_dod(stage="m4_1pct", prom_url=_PROM)

        discrepancy = _metric(report, DodMetric.DISCREPANCY_RATE)
        aphelion = _metric(report, DodMetric.APHELION_UNREACHABLE_RATE)
        assert discrepancy.verdict == DodVerdict.INSUFFICIENT_DATA
        assert discrepancy.observed == pytest.approx(0.2)
        assert aphelion.observed == pytest.approx(0.1)

    def test_an_unanswered_numerator_reports_zero_not_a_guess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative twin: with no numerator there is no rate to report."""
        _install_router(monkeypatch, _make_router(attempts=1000, diverge=None, aphelion=0))

        report = compute_shadow_dod(stage="m4_1pct", prom_url=_PROM)

        discrepancy = _metric(report, DodMetric.DISCREPANCY_RATE)
        assert discrepancy.verdict == DodVerdict.INSUFFICIENT_DATA
        assert discrepancy.observed == 0.0


# ===========================================================================
# _prom_instant_query — envelope parsing
# ===========================================================================


@pytest.mark.unit
class TestInstantQueryParsing:
    def test_a_json_list_payload_returns_none_instead_of_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid JSON that is not an object must degrade, not raise.

        The existing malformed-payload test sends an HTML error page, which
        fails at ``json.loads`` and never reaches the envelope checks. A proxy
        that answers with a JSON array parses cleanly and then hits
        ``payload.get`` — an AttributeError that escapes the fail-closed gate,
        unless the ``isinstance(payload, dict)`` guard holds.
        """
        _install_router(monkeypatch, lambda _url: b'[{"status": "success"}]')
        assert _prom_instant_query(_PROM, "up") is None

    def test_a_non_numeric_value_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``float()`` on a garbage scalar must be caught, not propagated."""
        _install_router(monkeypatch, lambda _url: _envelope("not-a-number"))
        assert _prom_instant_query(_PROM, "up") is None

    def test_a_multi_series_result_reads_the_first_series(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``result[0]`` is the contract — every query is wrapped in ``sum()``.

        With one result in every canned envelope, ``result[-1]`` is
        indistinguishable. If a recording rule ever produces more than one
        series, reading from the wrong end silently changes the number the gate
        judges, so the index is worth pinning even though the queries aggregate.
        """
        _install_router(monkeypatch, lambda _url: _envelope(7, 99, 1234))
        assert _prom_instant_query(_PROM, "up") == 7.0

    def test_the_scalar_is_read_from_the_value_pair_not_the_timestamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_router(monkeypatch, lambda _url: _envelope(42.5))
        assert _prom_instant_query(_PROM, "up") == 42.5


@pytest.mark.unit
class TestRequestConstruction:
    def test_a_trailing_slash_on_the_base_url_does_not_double_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``http://prom:9090/`` is how an operator normally pastes a URL.

        Every existing test passes a slash-free base, so ``rstrip('/')`` could
        be dropped and the request would go to ``//api/v1/query`` — which some
        reverse proxies serve and others 404, making the gate's behaviour
        depend on the deployment's ingress rather than on the data.
        """
        captured: list[str] = []
        _install_router(monkeypatch, lambda _url: _envelope(1), captured=captured)

        _prom_instant_query("http://prom:9090/", "up")

        assert captured
        assert captured[0].startswith("http://prom:9090/api/v1/query?")
        assert "9090//api" not in captured[0]

    def test_a_read_timeout_is_always_supplied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gate must never block forever on a wedged Prometheus.

        Nothing asserted the timeout reached ``urlopen``, so dropping it — and
        inheriting the socket default of "wait indefinitely" — survived. A
        ``--dod`` invocation from cron that never returns is worse than one that
        degrades to INSUFFICIENT_DATA.
        """
        timeouts: list[float] = []
        _install_router(monkeypatch, lambda _url: _envelope(1), timeouts=timeouts)

        _prom_instant_query(_PROM, "up")
        assert timeouts == [10.0]

        timeouts.clear()
        _prom_instant_query(_PROM, "up", timeout=2.5)
        assert timeouts == [2.5]

    def test_every_shadow_query_carries_the_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All three legs of a report, not just a hand-rolled single query."""
        timeouts: list[float] = []
        _install_router(
            monkeypatch, _make_router(attempts=1000, diverge=0, aphelion=0), timeouts=timeouts
        )

        compute_shadow_dod(stage="m4_1pct", prom_url=_PROM)

        assert len(timeouts) == 3
        assert all(t > 0 for t in timeouts)


# ===========================================================================
# Report envelope — stage identity and window stamps
# ===========================================================================


@pytest.mark.unit
class TestReportEnvelope:
    def test_the_report_carries_the_dod_stage_name_not_the_shadow_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``report.stage`` is what the CLI and the runbook print.

        The shadow label ``s2`` is an internal detail of the observer's
        counters; stamping it on the report would make ``--stage m4_10pct``
        answer about a stage name that appears nowhere in the promotion
        runbook. Nothing asserted the field, so the substitution survived.
        """
        _install_router(monkeypatch, _make_router(attempts=1000, diverge=0, aphelion=0))

        report = compute_shadow_dod(stage="m4_10pct", prom_url=_PROM)

        assert report.stage == "m4_10pct"

    def test_the_window_stamps_follow_until_and_window_days(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``until`` is documented as stamping the report window.

        No test passes it, so ignoring the argument and stamping "now" was
        invisible — and the stamps are the only record of *which* window a
        stored DoD report describes.
        """
        _install_router(monkeypatch, _make_router(attempts=1000, diverge=0, aphelion=0))
        until = _dt.datetime(2026, 5, 5, 12, 0, 0, tzinfo=_dt.UTC)

        report = compute_shadow_dod(stage="m4_1pct", window_days=3, prom_url=_PROM, until=until)

        assert report.window_end == until.isoformat()
        assert report.window_start == (until - _dt.timedelta(days=3)).isoformat()

    def test_a_non_utc_until_is_converted_rather_than_relabelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``astimezone(UTC)`` shifts the instant; ``replace(tzinfo=UTC)`` lies.

        An operator in UTC+8 passing a local timestamp must get the same window
        as the equivalent UTC one, not a window eight hours in the future.
        """
        _install_router(monkeypatch, _make_router(attempts=1000, diverge=0, aphelion=0))
        tz8 = _dt.timezone(_dt.timedelta(hours=8))
        until = _dt.datetime(2026, 5, 5, 20, 0, 0, tzinfo=tz8)

        report = compute_shadow_dod(stage="m4_1pct", prom_url=_PROM, until=until)

        assert report.window_end == "2026-05-05T12:00:00+00:00"
