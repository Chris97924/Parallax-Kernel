"""#106 — the arbitration latency histogram, and the panel that was reading a ghost.

``parallax_arbitration_latency_seconds`` was the one entry in the #106
declared-dead table that the issue did not find: no rule, no module, nothing in
the repo produced it, and a stat panel in the dual-read dashboard selected it
anyway. Its stand-in on /metrics was ``parallax_arbitration_p99_latency_ms``,
a Gauge hardcoded to ``0.0`` — so the two ways to ask "how slow is arbitration"
answered "no data" and "0ms, healthy" respectively, and neither had ever
measured anything.

These tests pin the measurement, not the presence of a series: an observation
has to land in the bucket the elapsed time belongs to, reach the scrape text
with the right count and sum, and the dashboard has to have stopped describing
itself as a placeholder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest
from parallax.router.dual_read import DualReadRouter
from parallax.router.live_arbitration import (
    ARBITRATION_LATENCY_BUCKETS,
    arbitration_latency_seconds,
)
from parallax.router.types import QueryType
from parallax.server.routes.metrics import _build_payload

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARD = (
    _REPO_ROOT / "grafana" / "dashboards" / "parallax-dual-read-observability.json"
)
_SERIES = "parallax_arbitration_latency_seconds"


def _scraped_histogram() -> dict[str, float]:
    """``{sample_name+labels -> value}`` for the arbitration histogram in a scrape."""
    out: dict[str, float] = {}
    for family in text_string_to_metric_families(_build_payload()):
        if family.name != _SERIES:
            continue
        assert family.type == "histogram", f"exported as {family.type}, not a histogram"
        for sample in family.samples:
            key = sample.name
            if "le" in sample.labels:
                key = f"{sample.name}{{le={sample.labels['le']}}}"
            out[key] = sample.value
    return out


@pytest.fixture(autouse=True)
def _isolate_histogram():
    """Rewind the process-global collector so counts are attributable to one test."""
    buckets_before = [b.get() for b in arbitration_latency_seconds._buckets]  # noqa: SLF001
    sum_before = arbitration_latency_seconds._sum.get()  # noqa: SLF001
    try:
        yield
    finally:
        for bucket, value in zip(
            arbitration_latency_seconds._buckets,  # noqa: SLF001
            buckets_before,
            strict=True,
        ):
            bucket.set(value)
        arbitration_latency_seconds._sum.set(sum_before)  # noqa: SLF001


def _zero_the_histogram() -> None:
    for bucket in arbitration_latency_seconds._buckets:  # noqa: SLF001
        bucket.set(0.0)
    arbitration_latency_seconds._sum.set(0.0)  # noqa: SLF001


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------


def test_an_observation_lands_in_the_bucket_its_value_belongs_to() -> None:
    """VALUE-LEVEL: not "a histogram exists" but "it recorded THIS number".

    0.0003s must be counted by every bucket at or above 0.0005 and by none
    below it, and contribute exactly 0.0003 to the sum. A histogram wired to
    the wrong clock, or observing a constant, fails here.
    """
    _zero_the_histogram()
    arbitration_latency_seconds.observe(0.0003)

    scraped = _scraped_histogram()

    assert scraped[f"{_SERIES}_count"] == 1.0
    assert scraped[f"{_SERIES}_sum"] == pytest.approx(0.0003)
    for bound in ARBITRATION_LATENCY_BUCKETS:
        expected = 1.0 if bound >= 0.0005 else 0.0
        assert scraped[f"{_SERIES}_bucket{{le={_le(bound)}}}"] == expected, (
            f"le={bound} bucket is wrong for a 0.0003s observation"
        )
    assert scraped[f"{_SERIES}_bucket{{le=+Inf}}"] == 1.0


def test_the_buckets_resolve_the_range_arbitration_actually_runs_in() -> None:
    """A ladder that starts above the signal reports a constant p99.

    ``arbitrate`` is a pure dict lookup — single-digit microseconds. With
    prometheus_client's DEFAULT_BUCKETS (first bound le=0.005) every real call
    lands in bucket one and histogram_quantile answers 0.005 forever, which
    looks measured and is not. At least three bounds must sit below 0.001.
    """
    sub_millisecond = [b for b in ARBITRATION_LATENCY_BUCKETS if b < 0.001]

    assert len(sub_millisecond) >= 3, (
        "the histogram cannot resolve a microsecond-scale call: "
        f"{ARBITRATION_LATENCY_BUCKETS}"
    )


def test_several_observations_accumulate() -> None:
    """Count and sum are the two numbers a rate()-based p99 is built from."""
    _zero_the_histogram()
    for value in (0.00002, 0.0002, 0.02):
        arbitration_latency_seconds.observe(value)

    scraped = _scraped_histogram()

    assert scraped[f"{_SERIES}_count"] == 3.0
    assert scraped[f"{_SERIES}_sum"] == pytest.approx(0.02022)
    # The 0.02 observation is above every finite bound except the last.
    assert scraped[f"{_SERIES}_bucket{{le={_le(0.005)}}}"] == 2.0
    assert scraped[f"{_SERIES}_bucket{{le={_le(0.1)}}}"] == 3.0


def test_the_real_arbitration_path_feeds_the_histogram() -> None:
    """The wiring, not the collector: a dual-read dispatch must move the count.

    Exercised through DualReadRouter rather than by calling ``observe``
    directly, because the defect this closes was a collector nobody called.
    """
    _zero_the_histogram()
    before = _scraped_histogram()[f"{_SERIES}_count"]

    _run_one_dual_read()

    assert _scraped_histogram()[f"{_SERIES}_count"] == before + 1.0, (
        "a dual-read dispatch did not record an arbitration latency — the "
        "observation is not on the live path"
    )


class _StubPort:
    """Minimal QueryPort returning a fixed hit — same shape tests/router/ uses."""

    def __init__(self, result: RetrievalEvidence) -> None:
        self._result = result

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        return self._result


def _run_one_dual_read() -> None:
    """Drive one DualReadRouter dispatch with stub ports.

    ``dual_read_override=True`` because ``DUAL_READ`` defaults to false and the
    flag-off fast path returns ``outcome="skipped"`` before any arbitration
    happens — which is correct behaviour, and would make this test pass
    vacuously if it were left to the environment.
    """
    evidence = RetrievalEvidence(
        hits=({"id": "a", "kind": "memory", "score": 1.0},), stages=("test",)
    )
    router = DualReadRouter(primary=_StubPort(evidence), secondary=_StubPort(evidence))
    router.query(
        QueryRequest(
            query_type=QueryType.RECENT_CONTEXT,
            user_id="u-arbitration-latency",
            params=None,
        ),
        dual_read_override=True,
    )


def _le(bound: float) -> str:
    """Render a bucket bound the way prometheus_client labels it."""
    from prometheus_client.utils import floatToGoString

    return floatToGoString(bound)


# ---------------------------------------------------------------------------
# The consumer
# ---------------------------------------------------------------------------


def _arbitration_latency_panel() -> dict[str, object]:
    dashboard = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    panels = [
        panel
        for panel in dashboard["panels"]
        if any(
            _SERIES in str(target.get("expr", ""))
            for target in panel.get("targets", []) or []
        )
    ]
    assert len(panels) == 1, f"expected exactly one panel selecting {_SERIES}, got {len(panels)}"
    return panels[0]


def test_the_dashboard_panel_no_longer_calls_itself_a_placeholder() -> None:
    """The panel outlived the placeholder; its label has to as well.

    Titled "M3b placeholder — arbitration_p99_latency_ms" it advertised both a
    metric it does not query and a status it no longer has, which is how a
    working panel gets ignored.
    """
    panel = _arbitration_latency_panel()
    title = str(panel.get("title", ""))
    description = str(panel.get("description", ""))

    assert "placeholder" not in title.lower(), f"panel title still says placeholder: {title!r}"
    assert "placeholder" not in description.lower(), (
        f"panel description still says placeholder: {description!r}"
    )


def test_the_panel_does_not_mask_no_data_as_zero() -> None:
    """``or vector(0)`` on a latency panel reports 0ms for an idle arbitrator.

    That is the same failure the whole #106 sweep is about — a consumer that
    cannot tell "not measured" from "measured, and healthy". With a real
    producer the fallback is no longer protecting the panel from a missing
    series; it is hiding the one state worth seeing.
    """
    expr = str(_arbitration_latency_panel()["targets"][0]["expr"])

    assert "vector(0)" not in expr, f"panel still masks no-data as zero: {expr}"
    assert f"{_SERIES}_bucket" in expr


def test_the_superseded_gauge_says_it_is_not_a_measurement() -> None:
    """``parallax_arbitration_p99_latency_ms`` still ships, still reads 0.0.

    Kept for compatibility, so its HELP text is the only thing standing between
    a reader and a hardcoded zero they would take for a latency.
    """
    payload = _build_payload()
    help_lines = [
        line
        for line in payload.splitlines()
        if line.startswith("# HELP parallax_arbitration_p99_latency_ms")
    ]

    assert help_lines, "the superseded placeholder gauge vanished from the scrape"
    assert "DEPRECATED" in help_lines[0]
    assert _SERIES in help_lines[0], (
        "the deprecated gauge must point at its replacement: " + help_lines[0]
    )
