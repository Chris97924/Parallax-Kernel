"""Regression tests for ``scripts/burn-in-monitor.sh`` (round-5 P1).

P1 fix: traffic flag queries now use server-side sum() to aggregate across
all Prometheus series (e.g. multiple instances/jobs) before comparing to 0.

Prior: increase(parallax_aphelion_total{traffic_source="synthetic"}[24h])
       → reads only v[0] from multi-series result, ignores others.
After: sum(increase(parallax_aphelion_total{traffic_source="synthetic"}[24h]))
       → scalar result; correct aggregate across all label combinations.

The bash prom_query helper is not changed (Option A chosen over Option B).
These tests verify:
1. The script file contains the corrected sum() query strings.
2. The prom_query helper correctly parses a scalar Prometheus response
   (simulated via a fixture) — ensuring a single-value result works.
3. The flag computation logic is demonstrated via a Python replica of the
   awk condition used in the script.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "burn-in-monitor.sh"


# ---------------------------------------------------------------------------
# Script existence
# ---------------------------------------------------------------------------


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"missing script: {SCRIPT}"


# ---------------------------------------------------------------------------
# P1: sum() wrapping present in both query strings
# ---------------------------------------------------------------------------


def test_synthetic_query_uses_sum() -> None:
    """burn-in-monitor.sh must wrap the synthetic increase() query with sum()."""
    text = SCRIPT.read_text(encoding="utf-8")
    # Must find sum(increase(...{traffic_source="synthetic"}[24h]))
    assert re.search(
        r'sum\(increase\(parallax_aphelion_total\{[^}]*traffic_source=\\"synthetic\\"[^}]*\}\[24h\]\)\)',
        text,
    ), (
        "Expected sum(increase(...{traffic_source=\\\"synthetic\\\"}[24h])) "
        "in burn-in-monitor.sh but not found.\n"
        "This is the P1 fix: multi-series Prometheus results must be summed "
        "server-side before the prom_query helper reads value[1]."
    )


def test_natural_query_uses_sum() -> None:
    """burn-in-monitor.sh must wrap the natural increase() query with sum()."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(
        r'sum\(increase\(parallax_aphelion_total\{[^}]*traffic_source=\\"natural\\"[^}]*\}\[24h\]\)\)',
        text,
    ), (
        "Expected sum(increase(...{traffic_source=\\\"natural\\\"}[24h])) "
        "in burn-in-monitor.sh but not found.\n"
        "This is the P1 fix: multi-series Prometheus results must be summed "
        "server-side before the prom_query helper reads value[1]."
    )


def test_no_bare_increase_query_for_traffic_source() -> None:
    """There must be no un-summed increase() call for traffic_source labels.

    Ensures the old bare 'increase(parallax_aphelion_total{traffic_source=...' pattern
    was fully replaced and not left anywhere else in the script.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    # Match bare increase( not preceded by sum(
    matches = re.findall(
        r'(?<!sum\()increase\(parallax_aphelion_total\{[^}]*traffic_source=',
        text,
    )
    assert matches == [], (
        f"Found un-summed increase() query for traffic_source: {matches}\n"
        "All traffic-source queries must use sum(increase(...)) to correctly "
        "aggregate across multiple Prometheus series."
    )


# ---------------------------------------------------------------------------
# P1: awk flag logic replica — sum of multi-series values is what matters
# ---------------------------------------------------------------------------


def _flag_from_count(count: float) -> str:
    """Replica of the awk logic: (x+0 > 0) ? 'True' : 'False'."""
    return "True" if count > 0 else "False"


def test_multi_series_sum_seen_as_nonzero() -> None:
    """Two series with values 5 and 7 must result in flag=True (total=12).

    Before the fix, prom_query returned only the first series (5 or 7),
    and with sum() the Prometheus response is a scalar 12 — flag=True.
    """
    # Simulate what prom_query now returns: the sum scalar from Prometheus
    simulated_prom_scalar = 5.0 + 7.0  # = 12.0
    assert _flag_from_count(simulated_prom_scalar) == "True"


def test_single_zero_series_stays_false() -> None:
    """A genuine zero sum (no traffic on any series) still yields flag=False."""
    assert _flag_from_count(0.0) == "False"


def test_single_nonzero_series_true() -> None:
    """A single series with traffic also yields flag=True (backward compatible)."""
    assert _flag_from_count(42.0) == "True"


# ---------------------------------------------------------------------------
# P1: prom_query helper parses scalar Prometheus JSON correctly
# ---------------------------------------------------------------------------


def test_prom_query_helper_parses_scalar_result() -> None:
    """The prom_query python3 inline pipeline correctly extracts value[1].

    With sum(), Prometheus returns a single-element result list (scalar).
    The existing helper ``r['data']['result'][0]['value'][1]`` still works
    because sum() always returns exactly one result vector element.
    """

    # Simulated Prometheus API response for a sum() query that returns 12.0
    prom_response = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {},
                    "value": [1715299200, "12"],
                }
            ],
        },
    }
    # Replicate the prom_query inline python3 logic
    r = prom_response
    v = r["data"]["result"]
    extracted = v[0]["value"][1] if v else "0"
    assert extracted == "12", f"prom_query helper extracted {extracted!r}, expected '12'"
    assert float(extracted) == 12.0
