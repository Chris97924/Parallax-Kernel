"""#106.2 — the twelve dead apex/sqlite series, zero-exported and interpretable.

#106 found twelve series that shipped consumers select and no scrape has ever
contained. Their producers exist; the subsystems that would call them do not run
in the server, so the collectors were absent from the process, and the labelled
ones would have contributed no samples even after an import — registering a
Counter creates a family, not a series.

#106 chose no-data over zeros, reasoning that a zero is a measurement and
``increase(...) > 0`` over a permanent zero reads as "checked, and healthy". The
completion PR inverts that, because no-data has the identical failure mode AND
is indistinguishable from a broken exporter. What makes the zeros safe is the
third fact neither option published on its own: ``parallax_subsystem_wired``.

So these tests check three things, and the second is the one that makes the
change defensible rather than merely green:

1. every declared-dead series is on the wire, with the exact label set that was
   reviewed — not a plausible-looking one somebody added later;
2. the readiness gauge reports 0 for a subsystem that has not run, and 1 once it
   has, so a reader can tell the two kinds of zero apart;
3. the alert annotations describe the world that now exists — a stale
   "NO PRODUCER (#106.2) — THIS ALERT CANNOT FIRE" on a rule that can fire is
   how a working alert gets ignored.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from prometheus_client.parser import text_string_to_metric_families

from parallax.apex.router import ZERO_EXPORT_LABEL_SETS as APEX_ZERO_EXPORT_LABEL_SETS
from parallax.obs.subsystem_readiness import (
    KNOWN_SUBSYSTEMS,
    SUBSYSTEM_APEX_M7,
    SUBSYSTEM_SQLITE_GATE,
    is_wired,
    subsystem_wired,
)
from parallax.router.sqlite_gate import ZERO_EXPORT_LABEL_SETS as SQLITE_ZERO_EXPORT_LABEL_SETS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APEX_RULES = _REPO_ROOT / "prometheus" / "rules" / "parallax-apex-m7.rules.yml"

#: The exhaustive list #106 declared dead for these two subsystems. Hardcoded
#: rather than re-derived from the modules under test, so a table that quietly
#: loses an entry fails here instead of shrinking the contract with it.
_DECLARED_DEAD_106_2 = (
    "parallax_apex_read",
    "parallax_apex_read_latency_ms",
    "parallax_apex_read_errors",
    "parallax_apex_package_dir_errors",
    "parallax_apex_empty_result",
    "parallax_apex_empty_corpus",
    "parallax_apex_audit_write_failures",
    "parallax_apex_lib_version_info",
    "parallax_sqlite_lock_wait_seconds",
    "parallax_sqlite_lock_hold_seconds",
    "parallax_sqlite_lock_queue_depth",
    "parallax_sqlite_errors",
)


_SCRAPE_SCRIPT = """
import sys
from fastapi.testclient import TestClient
from parallax.server.app import create_app

sys.stdout.write(TestClient(create_app()).get("/metrics").text)
"""


@pytest.fixture(scope="module")
def scrape(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One /metrics body from a COLD process, for the same reason the parity gate is.

    These collectors are module-level singletons in a process-global registry,
    and tests/apex/ and tests/router/ both construct the real subsystems. Run
    in-process, this file would scrape a registry those suites had already
    incremented — the zeros would not be zero and ``parallax_subsystem_wired``
    would report 1 for a subsystem this app never built. Both of the properties
    under test would then be measured against the wrong process, and the
    "unwired reports 0" assertion in particular would pass or fail on test
    ordering rather than on the code.
    """
    tmp_path = tmp_path_factory.mktemp("zero-export")
    env = dict(os.environ)
    env.update(
        {
            "SHADOW_LOG_DIR": str(tmp_path / "shadow"),
            "DUAL_READ_LOG_DIR": str(tmp_path / "dual_read"),
            "PARALLAX_DB_PATH": str(tmp_path / "zero.db"),
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
        encoding="utf-8",
        timeout=180,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0, f"cold scrape failed:\n{result.stderr}"
    return result.stdout


@pytest.fixture(scope="module")
def families(scrape: str) -> dict[str, list[dict[str, str]]]:
    """``{family name -> [label sets]}``, with histogram ``le`` collapsed away.

    ``le`` is a bucket coordinate rather than a dimension of the series, so
    including it would make a twelve-bucket histogram look like twelve label
    sets and bury the thing being asserted.
    """
    out: dict[str, list[dict[str, str]]] = {}
    for family in text_string_to_metric_families(scrape):
        seen: list[dict[str, str]] = []
        for sample in family.samples:
            labels = {k: v for k, v in sample.labels.items() if k != "le"}
            if labels not in seen:
                seen.append(labels)
        out[family.name] = seen
    return out


# ---------------------------------------------------------------------------
# 1. On the wire, with the reviewed label sets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _DECLARED_DEAD_106_2)
def test_every_declared_dead_series_is_now_exported(
    name: str, families: dict[str, list[dict[str, str]]]
) -> None:
    """Presence is the floor, not the contract — but it is where it starts."""
    assert name in families, (
        f"{name} is still absent from a scrape; its consumers still evaluate no-data"
    )


def test_the_exported_label_sets_are_exactly_the_reviewed_ones() -> None:
    """Label choice is the reviewable decision here, so it is pinned literally.

    Priming ``reason="package_missing"`` instead of ``reason=""`` would assert
    that a specific failure mode was checked and found clean — inventing the
    reassurance the zero-export was accused of. Priming
    ``component="m3_dual_read"`` is the opposite case and equally deliberate:
    the dashboard selects that value by equality, so anything else leaves the
    panel matching nothing.
    """
    assert APEX_ZERO_EXPORT_LABEL_SETS == {
        "parallax_apex_read": ({"result": ""},),
        "parallax_apex_read_latency_ms": ({"result": ""},),
        "parallax_apex_read_errors": ({"reason": "", "exc_class": ""},),
        "parallax_apex_package_dir_errors": ({"reason": ""},),
        "parallax_apex_empty_result": ({"cause": "empty_corpus"},),
        "parallax_apex_empty_corpus": ({},),
        "parallax_apex_audit_write_failures": ({"cause": ""},),
        "parallax_apex_lib_version_info": ({"version": "", "min_version": ""},),
    }
    assert SQLITE_ZERO_EXPORT_LABEL_SETS == {
        "parallax_sqlite_lock_wait_seconds": ({"component": "m3_dual_read", "op": ""},),
        "parallax_sqlite_lock_hold_seconds": ({"component": "m3_dual_read", "op": ""},),
        "parallax_sqlite_lock_queue_depth": ({},),
        "parallax_sqlite_errors": ({"code": "", "component": "", "op": ""},),
    }


@pytest.mark.parametrize(
    ("name", "label_sets"),
    [
        *APEX_ZERO_EXPORT_LABEL_SETS.items(),
        *SQLITE_ZERO_EXPORT_LABEL_SETS.items(),
    ],
)
def test_the_scrape_carries_the_label_set_the_table_declares(
    name: str,
    label_sets: tuple[dict[str, str], ...],
    families: dict[str, list[dict[str, str]]],
) -> None:
    """The table and the wire have to agree, or the table is documentation."""
    exported = families[name]
    for declared in label_sets:
        assert declared in exported, (
            f"{name} is exported with {exported}, which does not include the declared "
            f"label set {declared} — every consumer selecting it still sees nothing"
        )


def test_the_dashboard_selector_for_the_lock_histograms_matches_a_real_series(
    families: dict[str, list[dict[str, str]]],
) -> None:
    """The one label value chosen for a consumer, checked against that consumer."""
    for name in ("parallax_sqlite_lock_wait_seconds", "parallax_sqlite_lock_hold_seconds"):
        components = {labels.get("component") for labels in families[name]}
        assert "m3_dual_read" in components, (
            f"{name} has no component=m3_dual_read series, so the dual-read dashboard "
            f"panel still matches nothing. Exported components: {components}"
        )


def test_the_apex_stuck_empty_corpus_selector_matches_a_real_series(
    families: dict[str, list[dict[str, str]]],
) -> None:
    """ApexStuckEmptyCorpus selects cause="empty_corpus" by equality."""
    causes = {labels.get("cause") for labels in families["parallax_apex_empty_result"]}

    assert "empty_corpus" in causes, (
        f"ApexStuckEmptyCorpus still matches nothing. Exported causes: {causes}"
    )


def test_the_exported_values_are_zero_not_noise(scrape: str) -> None:
    """A zero-export that exports something else is worse than no export."""
    for line in scrape.splitlines():
        if not line.startswith(("parallax_apex_", "parallax_sqlite_")):
            continue
        if line.startswith("parallax_sqlite_wal_size_bytes"):
            continue  # not part of the zero-export set; sampled lazily
        value = float(line.rsplit(" ", 1)[-1])
        assert value == 0.0, f"zero-exported series carries a non-zero value: {line}"


# ---------------------------------------------------------------------------
# 2. The readiness gauge — what makes a zero readable
# ---------------------------------------------------------------------------


def test_readiness_is_exported_for_every_subsystem(
    families: dict[str, list[dict[str, str]]],
) -> None:
    """An absent readiness signal reintroduces the ambiguity it exists to remove."""
    exported = {labels.get("subsystem") for labels in families["parallax_subsystem_wired"]}

    assert set(KNOWN_SUBSYSTEMS) <= exported, (
        f"missing readiness series for {set(KNOWN_SUBSYSTEMS) - exported}"
    )


def test_an_unwired_subsystem_reports_zero(scrape: str) -> None:
    """The whole justification for zero-exporting rests on this being honest.

    Nothing in ``create_app`` constructs an ApexPublicReadRouter, so apex M7 must
    read 0 in a freshly built app — if it read 1, every apex zero below it would
    be claiming to be a measurement.
    """
    line = next(
        ln
        for ln in scrape.splitlines()
        if ln.startswith(f'parallax_subsystem_wired{{subsystem="{SUBSYSTEM_APEX_M7}"}}')
    )

    assert float(line.rsplit(" ", 1)[-1]) == 0.0, (
        f"apex M7 reports itself wired in a server that never builds one: {line}"
    )


def test_constructing_a_subsystem_flips_its_readiness() -> None:
    """And the gauge has to actually move, or it is a constant dressed as a signal."""
    import sqlite3

    from parallax.router.sqlite_gate import SQLiteGate

    before = subsystem_wired.labels(subsystem=SUBSYSTEM_SQLITE_GATE)._value.get()  # noqa: SLF001
    try:
        subsystem_wired.labels(subsystem=SUBSYSTEM_SQLITE_GATE).set(0.0)
        assert not is_wired(SUBSYSTEM_SQLITE_GATE)

        conn = sqlite3.connect(":memory:")
        try:
            SQLiteGate(conn, component="m3_dual_read")
        finally:
            conn.close()

        assert is_wired(SUBSYSTEM_SQLITE_GATE), (
            "building a SQLiteGate left the readiness gauge at 0 — the zeros it "
            "qualifies would stay unreadable"
        )
    finally:
        subsystem_wired.labels(subsystem=SUBSYSTEM_SQLITE_GATE).set(before)


# ---------------------------------------------------------------------------
# 3. The annotations an oncall reads
# ---------------------------------------------------------------------------


def _apex_alerts() -> list[dict[str, object]]:
    doc = yaml.safe_load(_APEX_RULES.read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group.get("rules", []) if "alert" in rule]


def test_there_are_apex_alerts_to_check() -> None:
    """Guard the two tests below against silently checking an empty list."""
    assert len(_apex_alerts()) == 6


@pytest.mark.parametrize("rule", _apex_alerts(), ids=lambda r: str(r["alert"]))
def test_no_apex_alert_still_claims_it_cannot_fire(rule: dict[str, object]) -> None:
    """A stale annotation is a test failure, not a documentation nit.

    "THIS ALERT CANNOT FIRE" on a rule that can fire teaches the reader that the
    annotations are wrong, which costs more than the annotation was worth.
    """
    description = str(rule["annotations"]["description"])  # type: ignore[index]

    assert "NO PRODUCER" not in description, (
        f"{rule['alert']} still carries the pre-#106 NO PRODUCER warning:\n{description}"
    )


@pytest.mark.parametrize("rule", _apex_alerts(), ids=lambda r: str(r["alert"]))
def test_every_apex_alert_points_at_the_readiness_gauge(rule: dict[str, object]) -> None:
    """Replacing the warning is not enough — the replacement has to be usable.

    The zeros are only interpretable next to ``parallax_subsystem_wired``, so an
    annotation that says "zero until wired" without naming the series that
    reports wiredness has moved the problem rather than solved it.
    """
    description = str(rule["annotations"]["description"])  # type: ignore[index]

    assert "parallax_subsystem_wired" in description, (
        f"{rule['alert']} does not tell the reader how to tell a real zero from an "
        f"unwired one:\n{description}"
    )
