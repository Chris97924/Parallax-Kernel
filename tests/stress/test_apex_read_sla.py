"""Fast mechanics tests for the M7 Apex public-read SLA stress harness.

These verify the harness *works* (builds a real signed corpus, drives the real
router, computes percentiles, reports SLO correctly) using a tiny corpus and a
handful of iterations so the default test suite stays fast. The actual SLA
*finding* (p99 vs 100ms and the §8.4 Q4 package-count ceiling) is produced by a
full run that writes docs/m7-prep/m7-apex-read-sla-preview.md — it is NOT gated
here, because absolute latency is machine- and load-dependent.
"""

from __future__ import annotations

import pytest

from scripts import m7_apex_read_stress as harness


@pytest.mark.integration
class TestStressHarnessMechanics:
    def test_measure_returns_well_formed_report(self) -> None:
        result = harness.measure(package_count=2, iters=5)
        assert result["package_count"] == 2
        assert result["iters"] == 5
        # Latency percentiles are present and ordered p50 <= p95 <= p99 <= max.
        assert 0.0 <= result["p50_ms"] <= result["p95_ms"] <= result["p99_ms"] <= result["max_ms"]
        assert isinstance(result["slo_pass"], bool)

    def test_happy_path_has_zero_errors_and_no_empty_results(self) -> None:
        """A non-empty corpus of matching-subject packages must all hit."""
        result = harness.measure(package_count=2, iters=5)
        assert result["error_count"] == 0, result["error_samples"]
        assert result["empty_results"] == 0

    def test_measure_rejects_empty_corpus(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            harness.measure(package_count=0, iters=5)

    def test_measure_rejects_zero_iters(self) -> None:
        with pytest.raises(ValueError, match="iters"):
            harness.measure(package_count=1, iters=0)

    def test_sweep_reports_ceiling_and_slo(self) -> None:
        report = harness.run_sweep(package_counts=[1, 2], iters=4)
        assert report["sla_p99_ms"] == harness.SLA_P99_MS
        assert [r["package_count"] for r in report["results"]] == [1, 2]
        assert isinstance(report["slo_pass"], bool)
        # ceiling is either None or one of the swept counts
        assert report["p99_under_sla_ceiling_packages"] in (None, 1, 2)

    def test_markdown_render_includes_sweep_table(self) -> None:
        report = harness.run_sweep(package_counts=[1], iters=3)
        md = harness.render_markdown(report, generated_at="2026-05-31T00:00:00Z", host="test-host")
        assert "SLA Preview" in md
        assert "Package-count sweep" in md
        assert "| packages |" in md
        assert "§8.4 Q4" in md


@pytest.mark.unit
class TestPercentile:
    def test_p99_of_small_sample_returns_worst(self) -> None:
        """ceil-based nearest-rank: p99 of 5 values must be the WORST sample, so
        a low --iters run cannot hide a tail breach (the floor-based bug did)."""
        data = [10.0, 20.0, 30.0, 40.0, 500.0]
        assert harness._percentile(data, 99) == 500.0
        assert harness._percentile(data, 100) == 500.0

    def test_p50_is_a_central_sample(self) -> None:
        assert harness._percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0

    def test_empty_returns_zero(self) -> None:
        assert harness._percentile([], 99) == 0.0
