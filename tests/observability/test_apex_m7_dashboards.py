"""Apex M7 observability contract tests (spec §4.5).

These tests pin *parity* between three things that drift independently:

  1. the §4.5 metric set the router actually registers (``parallax.apex.router``),
  2. the metrics the M7 Grafana dashboard panels reference, and
  3. the metrics the M7 Prometheus alert rules reference.

A metric the router emits but nothing visualizes/alerts on — or a panel/alert
that references a metric the router never emits — fails the build. This is the
guard against the dashboard/alert drift PR #69 explicitly flagged for "the
dashboards PR".

The latency-histogram bucket test additionally asserts the p99 < 100ms SLA
(spec §4.2) is *measurable*: with prometheus_client's default buckets (max
finite ``le=10.0``) and millisecond-scale observations, ``histogram_quantile``
would saturate at 10 and the SLA alert could never evaluate the real p99.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest
import yaml

from parallax.apex import router as router_mod

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARD = _REPO_ROOT / "grafana" / "dashboards" / "parallax-apex-m7-public-read.json"
_RULES = _REPO_ROOT / "prometheus" / "rules" / "parallax-apex-m7.rules.yml"

# The module-level collector objects that own the §4.5 contract. Reading their
# ``_name`` ties this test to the real router, so a metric rename there fails
# here too (and forces the dashboard/rules to follow).
_ROUTER_METRIC_ATTRS = (
    "READ_LATENCY",
    "READ_TOTAL",
    "READ_ERRORS",
    "PACKAGE_DIR_ERRORS",
    "EMPTY_RESULT",
    "EMPTY_CORPUS",
    "AUDIT_WRITE_FAILURES",
    "LIB_VERSION_INFO",
)

# The 8 normative §4.5 metric base names (counter base, before the Prometheus
# ``_total`` / histogram ``_bucket`` exposition suffix). Hardcoded on purpose:
# this *is* the contract, and a change here must be a conscious edit.
_SPEC_METRIC_NAMES = frozenset(
    {
        "parallax_apex_read_latency_ms",
        "parallax_apex_read",
        "parallax_apex_read_errors",
        "parallax_apex_package_dir_errors",
        "parallax_apex_empty_result",
        "parallax_apex_empty_corpus",
        "parallax_apex_audit_write_failures",
        "parallax_apex_lib_version_info",
    }
)

_METRIC_PATTERN = re.compile(r"parallax_apex_[a-z0-9_]+")
_SAMPLE_SUFFIXES = ("_bucket", "_count", "_sum", "_total")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(token: str) -> str:
    """Strip a single Prometheus exposition suffix to recover the base name."""
    for suffix in _SAMPLE_SUFFIXES:
        if token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _router_metric_names() -> set[str]:
    return {getattr(router_mod, attr)._name for attr in _ROUTER_METRIC_ATTRS}


def _walk_exprs(node: object) -> list[str]:
    """Collect every ``expr`` string anywhere in a nested JSON/dict structure."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "expr" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_walk_exprs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_exprs(item))
    return found


def _dashboard() -> dict:
    return json.loads(_DASHBOARD.read_text(encoding="utf-8"))


def _rules() -> dict:
    return yaml.safe_load(_RULES.read_text(encoding="utf-8"))


def _rule_entries() -> list[dict]:
    out: list[dict] = []
    for group in _rules().get("groups", []):
        out.extend(group.get("rules", []))
    return out


def _referenced_bases(exprs: list[str]) -> set[str]:
    bases: set[str] = set()
    for expr in exprs:
        for token in _METRIC_PATTERN.findall(expr):
            bases.add(_normalize(token))
    return bases


def _dashboard_bases() -> set[str]:
    return _referenced_bases(_walk_exprs(_dashboard()))


def _rules_bases() -> set[str]:
    return _referenced_bases([r["expr"] for r in _rule_entries() if "expr" in r])


# ---------------------------------------------------------------------------
# Artifact existence + well-formedness
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestArtifactsWellFormed:
    def test_dashboard_file_exists(self) -> None:
        assert _DASHBOARD.is_file(), f"missing dashboard {_DASHBOARD}"

    def test_rules_file_exists(self) -> None:
        assert _RULES.is_file(), f"missing rules {_RULES}"

    def test_dashboard_is_valid_json_with_panels(self) -> None:
        data = _dashboard()
        assert data.get("uid"), "dashboard needs a stable uid"
        assert data.get("title")
        assert isinstance(data.get("panels"), list) and data["panels"], "no panels"

    def test_dashboard_targets_use_prometheus_datasource(self) -> None:
        for panel in _dashboard()["panels"]:
            for target in panel.get("targets", []):
                ds = target.get("datasource", {})
                assert ds.get("type") == "prometheus", f"panel {panel.get('id')} non-prom target"

    def test_rules_is_valid_yaml_with_groups(self) -> None:
        doc = _rules()
        assert isinstance(doc.get("groups"), list) and doc["groups"], "no rule groups"

    def test_every_alert_rule_is_complete(self) -> None:
        """Each alert needs expr + severity label + summary annotation."""
        for rule in _rule_entries():
            if "alert" not in rule:
                continue
            assert rule.get("expr", "").strip(), f"{rule.get('alert')} has empty expr"
            assert rule.get("labels", {}).get("severity"), f"{rule['alert']} missing severity"
            assert rule.get("annotations", {}).get("summary"), f"{rule['alert']} missing summary"


# ---------------------------------------------------------------------------
# The §4.5 metric contract + dashboard/rules parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMetricContract:
    def test_router_registers_exactly_the_spec_metric_set(self) -> None:
        """The router's registered metric base names == the §4.5 contract."""
        assert _router_metric_names() == set(_SPEC_METRIC_NAMES)

    def test_every_router_metric_is_observed_somewhere(self) -> None:
        """No §4.5 metric is left without a panel or an alert (union)."""
        observed = _dashboard_bases() | _rules_bases()
        missing = set(_SPEC_METRIC_NAMES) - observed
        assert not missing, f"metrics emitted but never visualized/alerted: {sorted(missing)}"

    def test_no_orphan_metric_references_in_dashboard(self) -> None:
        """Every parallax_apex_* metric a panel references is one the router emits."""
        bases = _dashboard_bases()
        # Guard against a vacuous pass: a dashboard that lost ALL its panel
        # expressions would otherwise produce an empty set and pass silently.
        assert bases, "dashboard references no parallax_apex_* metrics at all"
        orphans = bases - set(_SPEC_METRIC_NAMES)
        assert not orphans, f"dashboard references non-emitted metrics: {sorted(orphans)}"

    def test_no_orphan_metric_references_in_rules(self) -> None:
        bases = _rules_bases()
        assert bases, "rules reference no parallax_apex_* metrics at all"
        orphans = bases - set(_SPEC_METRIC_NAMES)
        assert not orphans, f"rules reference non-emitted metrics: {sorted(orphans)}"


# ---------------------------------------------------------------------------
# Required alerts (the load-bearing failure modes from §4.3 / §4.5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequiredAlerts:
    def _alert_exprs(self) -> dict[str, str]:
        return {r["alert"]: r["expr"] for r in _rule_entries() if "alert" in r}

    def test_sla_p99_latency_alert_present_and_measurable(self) -> None:
        """An alert must evaluate p99 read latency against the 100ms SLA (§4.2).

        The threshold is matched with a word-boundary regex (``> 100``), not a
        substring — a substring ``"100" in expr`` would also accept a 10x-looser
        ``> 1000`` typo and silently never fire on a real breach.
        """
        threshold = re.compile(r">\s*100(?:\.0+)?\b")
        hits = [
            expr
            for expr in self._alert_exprs().values()
            if "histogram_quantile" in expr
            and "parallax_apex_read_latency_ms_bucket" in expr
            and threshold.search(expr)
        ]
        assert hits, "no p99 alert binding parallax_apex_read_latency_ms_bucket at the 100ms SLA"

    def test_audit_write_failure_alert_present(self) -> None:
        """R-10 provenance break must alert (§4.3 audit-write-during-read)."""
        assert any(
            "parallax_apex_audit_write_failures_total" in expr
            for expr in self._alert_exprs().values()
        ), "no alert on parallax_apex_audit_write_failures_total"

    def test_package_dir_misconfig_alert_present(self) -> None:
        assert any(
            "parallax_apex_package_dir_errors_total" in expr
            for expr in self._alert_exprs().values()
        ), "no alert on parallax_apex_package_dir_errors_total"

    def test_read_error_alert_present(self) -> None:
        assert any(
            "parallax_apex_read_errors_total" in expr for expr in self._alert_exprs().values()
        ), "no alert on parallax_apex_read_errors_total"


# ---------------------------------------------------------------------------
# Latency histogram bucket fitness (router fix driven by this slice)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLatencyHistogramBuckets:
    def test_buckets_cover_the_100ms_sla(self) -> None:
        """Default prom buckets cap at le=10.0; ms-scale + p99<100ms SLA needs
        a finite bucket boundary at/above 100 or histogram_quantile saturates."""
        bounds = list(router_mod.READ_LATENCY._upper_bounds)
        finite = [b for b in bounds if not math.isinf(b)]
        assert 100.0 in bounds, f"no le=100 bucket for the SLA boundary; got {finite}"
        assert max(finite) >= 100.0, f"max finite bucket {max(finite)} < 100ms SLA"

    def test_buckets_have_sub_sla_resolution(self) -> None:
        """At least 3 finite buckets at/below 100ms so p50/p90/p99 resolve."""
        finite = [b for b in router_mod.READ_LATENCY._upper_bounds if not math.isinf(b)]
        below_or_at_sla = [b for b in finite if b <= 100.0]
        assert len(below_or_at_sla) >= 3, f"too few sub-SLA buckets: {below_or_at_sla}"
