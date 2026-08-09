"""#106.1 / #106.2 — no consumer may reference a series nothing can produce.

This is the gate the never-on-the-wire family never had. #99, #100, #101/#103
and #102/#105 were four separate audits that each rediscovered the same shape by
hand: an alert or a dashboard panel selects ``parallax_<something>``, no scrape
ever contains that series, and the consumer therefore evaluates to **no-data
forever**. No-data is not zero. An ``increase(...) > 0`` alert over a series
Prometheus has never seen stays silent exactly as it would if the system were
healthy, so the failure mode of this bug is a green dashboard.

The audits kept finding it because nothing in CI could. Every existing
observability test asserts the wrong half:
``tests/observability/test_apex_m7_dashboards.py`` pins "the router *registers*
these and the rules *reference* these" and passes today while all eight apex
series are unreachable, because registration is not exposition;
``tests/server/test_metrics_never_on_wire_102.py`` scrapes for real but only for
the three names #102 already knew about.

So this test derives its subject instead of listing it: it harvests every
``parallax_*`` selector out of the shipped rules and dashboards, takes one real
``/metrics`` scrape, and requires each harvested name to be either **on the
wire** or **declared below with a reason**. A new consumer for an unproduced
series fails here on the PR that adds it, which is the property that ends the
sweep — the table is the exhaustive, reviewed list of what is knowingly dead,
and anything not on it must actually work.

Both directions of the table are enforced too, so it cannot rot: an entry that
starts being produced must be removed (it would otherwise keep a working metric
listed as dead), and an entry whose last consumer is deleted must be removed
(it would otherwise outlive the thing it explains).

Scope note: the reverse gap — a series on the wire that nothing consumes — is
deliberately not policed. That is spare capacity, not a broken alert.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RULES_DIR = _REPO_ROOT / "prometheus" / "rules"
_DASHBOARD_DIR = _REPO_ROOT / "grafana" / "dashboards"

_METRIC_NAME_RE = re.compile(r"parallax_[a-z0-9_]+(?::[a-z0-9_]+)?")
# Exposition suffixes prometheus_client appends to a collector's base name.
_SAMPLE_SUFFIXES = ("_bucket", "_count", "_sum", "_total", "_created")


# ---------------------------------------------------------------------------
# The declared-dead table
# ---------------------------------------------------------------------------
# Every entry is a series a shipped consumer selects and no scrape can contain.
# Listing one here is not a fix — it is an acknowledgement that the consumer is
# inert, paired with the reason, so the next person to read the alert knows its
# silence proves nothing. The rules themselves carry the same warning in their
# annotations (that is what an oncall actually reads); this table is what makes
# the set reviewable and stops it growing by accident.
#
# Hardcoded on purpose, matching the convention in test_apex_m7_dashboards.py:
# this IS the contract, and a change must be a conscious edit.

_NO_PRODUCER: dict[str, str] = {
    # -- #106.1: M4 canary T1-T5 -------------------------------------------
    # Zero Python producers anywhere: the six names below are selected by
    # prometheus/rules/parallax-m4-canary.rules.yml and the stage-1 dashboard,
    # and no module registers, let alone increments, any of them.
    #
    # The T1-T5 machinery in parallax/canary/ is real but lives on the other
    # side of a process boundary: triggers.py / rollback.py / outcomes.py are
    # imported only by parallax/canary/cli.py, i.e. by `parallax canary` runs,
    # not by the server that serves /metrics. Counters incremented there would
    # live and die inside a short CLI process that Prometheus never scrapes, so
    # "add producers" is not a wiring change — it needs a server-side exporter
    # reading the durable OutcomeStore/AuditLog, or the rules need deleting.
    # That call is left to Chris (#106 policy deferral); what is NOT deferred is
    # that the alerts must stop reading as healthy, which the annotations now
    # handle.
    #
    # M4CanaryDataLossDetected is the sharp end: a severity=critical,
    # no-hysteresis, hard-rollback alert on data loss, evaluating no-data since
    # the day it was written.
    "parallax_canary_events": "no producer; T1/T2/T5 denominator. See #106.1.",
    "parallax_canary_event_errors": "no producer; T1 numerator. See #106.1.",
    "parallax_canary_discrepancy": "no producer; T2 numerator. See #106.1.",
    "parallax_canary_data_loss_events": (
        "no producer; T4 DATA-LOSS critical alert has never been able to fire. See #106.1."
    ),
    "parallax_canary_request_duration_ms": "no producer; T3 p99 histogram. See #106.1.",
    "parallax_canary_rollback_state": "no producer; stage-1 dashboard only. See #106.1.",
    # -- #106.2: Apex M7 public read ---------------------------------------
    # Registered by parallax/apex/router.py, which nothing under parallax/
    # imports — ApexPublicReadRouter is never instantiated outside tests, so M7
    # public read does not run in the server at all. Both halves are broken:
    # the collectors are absent from the running process's default registry,
    # and _build_payload would not render them even if present.
    #
    # Deliberately NOT wired onto the wire. Exporting them would publish a
    # permanent zero for a subsystem that never executes, and a zero is a
    # measurement — the apex-m7 rules are `increase(...) > 0` alerts that would
    # then read as "checked, and healthy" rather than "not running". The
    # zero-export inversion in metrics.py is reserved for collectors that are
    # genuinely live in-process (see its comment on the counters it does pluck).
    "parallax_apex_read": "producer never instantiated (M7 not wired). See #106.2.",
    "parallax_apex_read_latency_ms": "producer never instantiated (M7 not wired). See #106.2.",
    "parallax_apex_read_errors": "producer never instantiated (M7 not wired). See #106.2.",
    "parallax_apex_package_dir_errors": (
        "producer never instantiated (M7 not wired). See #106.2."
    ),
    "parallax_apex_empty_result": "producer never instantiated (M7 not wired). See #106.2.",
    "parallax_apex_empty_corpus": "producer never instantiated (M7 not wired). See #106.2.",
    "parallax_apex_audit_write_failures": (
        "producer never instantiated (M7 not wired). See #106.2."
    ),
    "parallax_apex_lib_version_info": "producer never instantiated (M7 not wired). See #106.2.",
    # -- #106.2: SQLiteGate -------------------------------------------------
    # Same shape. parallax/router/sqlite_gate.py registers five collectors;
    # SQLiteGate is instantiated nowhere in parallax/ (the only `SQLiteGate(`
    # in the tree is inside its own docstring), and crosswalk_backfill imports
    # it solely for the is_connection_gated classmethod. Four of the five are
    # selected by panels in the dual-read dashboard.
    "parallax_sqlite_lock_wait_seconds": "SQLiteGate never instantiated. See #106.2.",
    "parallax_sqlite_lock_hold_seconds": "SQLiteGate never instantiated. See #106.2.",
    "parallax_sqlite_lock_queue_depth": "SQLiteGate never instantiated. See #106.2.",
    "parallax_sqlite_errors": "SQLiteGate never instantiated. See #106.2.",
    # -- pre-existing, already documented in the rule itself -----------------
    # Producer exists (crosswalk_backfill.py) but record_orphan_miss() has no
    # production call site and the module is imported by nothing. Diagnosed in
    # full in the CrosswalkMissRateHigh comment in parallax-dual-read.rules.yml
    # during #101; listed here so the set is complete rather than partly prose.
    "parallax_crosswalk_miss_orphan": "producer has no call site; see #101 note in the rule.",
    # -- found by this test, not by the issue --------------------------------
    # Selected by a latency panel in parallax-dual-read-observability.json and
    # registered by nothing at all — no Python file in the repo mentions it.
    # The nearest real series is parallax_arbitration_p99_latency_ms, itself a
    # hardcoded 0.0 placeholder awaiting the T1.4 follow-up.
    "parallax_arbitration_latency_seconds": (
        "no producer anywhere in the repo; dashboard panel only. Found by #106."
    ),
}


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------


def _normalize(selector: str) -> str:
    """Strip one exposition suffix to recover a collector's base name."""
    for suffix in _SAMPLE_SUFFIXES:
        if selector.endswith(suffix):
            return selector[: -len(suffix)]
    return selector


def _walk_exprs(node: object) -> list[str]:
    """Every ``expr`` string anywhere in a nested YAML/JSON structure."""
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


def _recording_rule_names() -> set[str]:
    """Series Prometheus computes for itself — produced by the rules, not by us."""
    names: set[str] = set()
    for path in sorted(_RULES_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for group in doc.get("groups", []):
            for rule in group.get("rules", []):
                if "record" in rule:
                    names.add(rule["record"])
    return names


def _consumer_references() -> dict[str, set[str]]:
    """Map each raw metric selector to the set of files that reference it."""
    found: dict[str, set[str]] = {}
    sources: list[Path] = [
        *sorted(_RULES_DIR.glob("*.yml")),
        *sorted(_DASHBOARD_DIR.glob("*.json")),
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        doc = yaml.safe_load(text) if path.suffix == ".yml" else json.loads(text)
        for expr in _walk_exprs(doc):
            for selector in _METRIC_NAME_RE.findall(expr):
                found.setdefault(selector, set()).add(path.name)
    return found


_SCRAPE_SCRIPT = """
import json, sys
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from parallax.server.app import create_app

body = TestClient(create_app()).get("/metrics").text
sys.stdout.write(json.dumps(sorted({f.name for f in text_string_to_metric_families(body)})))
"""


@pytest.fixture(scope="module")
def on_wire(tmp_path_factory: pytest.TempPathFactory) -> frozenset[str]:
    """Family names in a real scrape from a cold process.

    A subprocess, not an in-process TestClient, and that is load-bearing: the
    collectors are module-level singletons in a process-global registry, so any
    test that has already imported ``parallax.apex.router`` (tests/apex/ does)
    leaves its collectors registered for the rest of the session. In-process
    this test would then see apex series that a real server never has, and would
    pass against precisely the bug it exists to catch.
    """
    tmp_path = tmp_path_factory.mktemp("parity")
    env = dict(os.environ)
    env.update(
        {
            "SHADOW_LOG_DIR": str(tmp_path / "shadow"),
            "DUAL_READ_LOG_DIR": str(tmp_path / "dual_read"),
            "PARALLAX_DB_PATH": str(tmp_path / "parity.db"),
            "PARALLAX_VAULT_PATH": str(tmp_path / "vault"),
            "PARALLAX_SCHEMA_PATH": str(_REPO_ROOT / "parallax" / "schema.sql"),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dual_read").mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [sys.executable, "-c", _SCRAPE_SCRIPT],
        capture_output=True,
        text=True,
        # Explicit: the open-mode startup banner is non-ASCII and a cp950 host
        # would otherwise fail decoding inside subprocess's reader thread.
        encoding="utf-8",
        timeout=180,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0, f"cold scrape failed:\n{result.stderr}"
    return frozenset(json.loads(result.stdout))


def _is_on_wire(selector: str, on_wire: frozenset[str]) -> bool:
    """Match a selector against scraped family names.

    Both spellings count. A Counter declared ``parallax_x_total`` collects under
    ``parallax_x``, so a ``parallax_x_total`` selector is satisfied by the base
    name; but the in-house gauge mirrors keep a literal ``_total`` in their own
    family name, so the raw selector has to be tried too. Checking only the
    normalized form reports live metrics as dead.
    """
    return selector in on_wire or _normalize(selector) in on_wire


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_every_consumed_series_is_produced_or_declared_dead(on_wire: frozenset[str]) -> None:
    """The forward gate: no silently-inert alert or panel may be added.

    Failing here means a rule or dashboard selects something a scrape does not
    contain. Either wire the producer, or add the name to ``_NO_PRODUCER`` with
    a reason and annotate the consuming rule — a deliberate, reviewed choice
    rather than a discovery someone makes two months later.
    """
    recording = _recording_rule_names()
    undeclared: dict[str, set[str]] = {}

    for selector, sources in _consumer_references().items():
        base = _normalize(selector)
        if ":" in selector or selector in recording or base in recording:
            continue  # Prometheus produces these from the recording rules above.
        if _is_on_wire(selector, on_wire) or base in _NO_PRODUCER:
            continue
        undeclared[selector] = sources

    assert undeclared == {}, (
        "these series are selected by a shipped consumer but appear in no scrape, "
        "and are not declared in _NO_PRODUCER — each one is an alert or panel that "
        "reads as healthy forever:\n"
        + "\n".join(
            f"  {name}: referenced by {sorted(src)}"
            for name, src in sorted(undeclared.items())
        )
    )


def test_declared_dead_series_are_really_absent(on_wire: frozenset[str]) -> None:
    """The table may not over-claim.

    A name that starts being produced has to leave the table, or the table
    becomes a place where working metrics are documented as broken — which is
    how the next reader learns to distrust it.
    """
    alive = sorted(name for name in _NO_PRODUCER if _is_on_wire(name, on_wire))

    assert alive == [], (
        "these are declared as having no producer but DO appear in a scrape — "
        f"delete their _NO_PRODUCER entries: {alive}"
    )


def test_declared_dead_series_still_have_a_consumer() -> None:
    """The table may not rot.

    An entry exists to explain an inert consumer. Delete the consumer and the
    entry is just a fossil that makes the set look worse than it is.
    """
    referenced = {_normalize(selector) for selector in _consumer_references()}
    referenced |= set(_consumer_references())

    orphaned = sorted(name for name in _NO_PRODUCER if name not in referenced)

    assert orphaned == [], (
        "these _NO_PRODUCER entries are no longer selected by any rule or "
        f"dashboard — delete them: {orphaned}"
    )


def test_every_declaration_carries_a_reason() -> None:
    """An entry without a rationale is just a suppression."""
    thin = sorted(name for name, why in _NO_PRODUCER.items() if len(why.strip()) < 20)

    assert thin == [], f"_NO_PRODUCER entries need a real explanation: {thin}"


# ---------------------------------------------------------------------------
# The annotations an oncall actually reads
# ---------------------------------------------------------------------------


def _alerts_selecting(name: str) -> list[dict[str, object]]:
    """Every alert rule whose expression mentions ``name``."""
    out: list[dict[str, object]] = []
    for path in sorted(_RULES_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for group in doc.get("groups", []):
            for rule in group.get("rules", []):
                if "alert" in rule and name in str(rule.get("expr", "")):
                    out.append(rule)
    return out


@pytest.mark.parametrize("metric", sorted(_NO_PRODUCER))
def test_alerts_on_dead_series_say_so_in_their_description(metric: str) -> None:
    """Silence must be labelled as meaningless where it will be read.

    The table above is for whoever edits the rules; this is for whoever is
    woken by them. An alert that cannot fire and does not say so is worse than
    no alert, because its quiet is indistinguishable from health — which is the
    single property that let this class survive four sweeps.

    Dashboard-only entries have no alert to annotate and are skipped.
    """
    alerts = _alerts_selecting(metric)
    if not alerts:
        pytest.skip(f"{metric} has no alert consumer (dashboard-only)")

    missing = [
        str(rule["alert"])
        for rule in alerts
        if "NO PRODUCER" not in str(rule.get("annotations", {}).get("description", ""))
    ]

    assert missing == [], (
        f"{metric} has no producer, so these alerts can never fire; their description "
        f"must carry a 'NO PRODUCER' warning so the silence is not read as health: {missing}"
    )
