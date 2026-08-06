"""#102 — the three never-on-the-wire metrics must reach an ACTUAL scrape.

Third instance of the defect class behind #100 and #101: a collector is
registered into ``prometheus_client``'s DEFAULT registry, while
``parallax.server.routes.metrics._build_payload`` serializes a *fresh*
``CollectorRegistry`` and renders default-registry series only from a
hardcoded name list. Registration is not exposition.

Two of the three back ``severity: critical`` alerts in
``prometheus/rules/parallax-dual-read.rules.yml`` — ``CircuitBreakerTripped``
and ``DrainTimeoutDetected`` — and until this change neither could ever fire:
they evaluated against no-data forever.

Every test here scrapes over real HTTP (``TestClient.get("/metrics")``) and
asserts on the exposition TEXT. A test that asserted "the collector is in the
registry" would have passed on the broken code — that assertion is exactly
the mistake this issue class is made of.

Counter name munging, because it is the trap in the fix: prometheus_client
strips a trailing ``_total`` from a Counter at construction, so
``Counter("parallax_drain_timeout_total")`` collects under the base name
``parallax_drain_timeout`` and emits a ``parallax_drain_timeout_total``
sample. The renderer takes base names; the wire names below are what a
scrape must show.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families

from parallax.router.circuit_breaker import circuit_breaker_tripped_total
from parallax.router.inflight import inflight_gauge
from parallax.server.app import create_app
from parallax.server.lifespan import drain_timeout_total

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Wire names — what Prometheus sees, suffix munging already applied.
_TRIPPED = "parallax_circuit_breaker_tripped_total"
_DRAIN = "parallax_drain_timeout_total"
_INFLIGHT = "parallax_inflight_requests"
_ALL_THREE = (_TRIPPED, _DRAIN, _INFLIGHT)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App pointed at an isolated tmp shadow + dual-read log dir + DB."""
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(tmp_path / "dual_read"))
    monkeypatch.setenv("PARALLAX_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(_REPO_ROOT / "parallax" / "schema.sql"))
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dual_read").mkdir(parents=True, exist_ok=True)

    from parallax.server.routes import metrics as metrics_route

    metrics_route._reset_cache_for_tests()

    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _restore_inflight_gauge() -> Iterator[None]:
    """Put the process-global inflight gauge back where it was.

    ``inflight_gauge`` is a module-level singleton in the default registry, so
    a test that leaves it at 7 corrupts every later test — including the drain
    loop tests, which poll it until it reads zero.
    """
    before = inflight_gauge._value.get()  # noqa: SLF001 — same private read get_inflight_count uses
    try:
        yield
    finally:
        inflight_gauge.set(before)


def _wire_samples(body: str, metric: str) -> list[tuple[str, float]]:
    """Every ``(label_block, value)`` for ``metric`` in a raw exposition body.

    Raw-text scan rather than a parser because the property under test is
    "this line is in the payload". ``label_block`` is the literal ``{...}``
    text, or ``""`` for an unlabelled series — the label-parity test asserts
    on it directly.
    """
    out: list[tuple[str, float]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        series, _, value = line.rpartition(" ")
        if series == metric:
            out.append(("", float(value)))
        elif series.startswith(f"{metric}{{") and series.endswith("}"):
            out.append((series[len(metric) :], float(value)))
    return out


def _wire_value(body: str, metric: str) -> float | None:
    """Sole sample value for an unlabelled ``metric``; ``None`` if absent."""
    samples = _wire_samples(body, metric)
    if not samples:
        return None
    assert len(samples) == 1, f"expected one series for {metric}, got {samples}"
    return samples[0][1]


# ---------------------------------------------------------------------------
# Exposition — one test per metric. RED on the unfixed tree.
# ---------------------------------------------------------------------------


def test_circuit_breaker_tripped_total_reaches_the_wire(client: TestClient) -> None:
    """A real trip must be visible in a real scrape.

    This is the CircuitBreakerTripped (CRITICAL) input. Asserted as a DELTA
    against the pre-increment scrape rather than an absolute value: the
    counter is a process-global singleton and the circuit-breaker suites run
    trips of their own, so any absolute expectation here would depend on test
    ordering.
    """
    before = _wire_value(client.get("/metrics").text, _TRIPPED)
    assert before is not None, "counter must be on the wire even before this test's increment"

    circuit_breaker_tripped_total.inc()

    body = client.get("/metrics").text
    after = _wire_value(body, _TRIPPED)
    assert after == pytest.approx(before + 1.0), body[-3000:]


def test_drain_timeout_total_reaches_the_wire(client: TestClient) -> None:
    """A drain timeout must be visible in a real scrape.

    This is the DrainTimeoutDetected (CRITICAL) input.
    """
    before = _wire_value(client.get("/metrics").text, _DRAIN)
    assert before is not None, "counter must be on the wire even before this test's increment"

    drain_timeout_total.inc()

    body = client.get("/metrics").text
    after = _wire_value(body, _DRAIN)
    assert after == pytest.approx(before + 1.0), body[-3000:]


def test_inflight_requests_gauge_reaches_the_wire(client: TestClient) -> None:
    """The inflight gauge must be visible, and must track back down.

    A gauge, not a counter: the drain runbook reads it to decide whether it is
    safe to stop the process, so the descent is as load-bearing as the climb
    and both directions are asserted.
    """
    inflight_gauge.set(7)
    body = client.get("/metrics").text
    assert _wire_value(body, _INFLIGHT) == pytest.approx(7.0), body[-3000:]

    inflight_gauge.set(0)
    body = client.get("/metrics").text
    assert _wire_value(body, _INFLIGHT) == pytest.approx(0.0), body[-3000:]


def test_metrics_scrape_is_not_counted_as_inflight_work(client: TestClient) -> None:
    """An idle instance must report exactly 0 — the drain gate depends on it.

    The middleware reads this gauge WHILE SERVING the scrape, so without
    ``INFLIGHT_EXCLUDED_PATHS`` the exported value is always at least 1 and an
    idle instance can never report 0. That is not cosmetic:
    docs/m3-runbooks/q8-drain-runbook.md selects stuck instances with
    ``parallax_inflight_requests > 0`` and gates deploys on
    ``sum(parallax_inflight_requests) == 0``, so a self-counting scrape marks
    every healthy instance as stuck and burns the gate's 960s timeout on every
    deploy.

    Asserted on the WIRE, not on the in-process gauge: in-process it reads 0
    either way, because the middleware has already decremented by the time the
    response is handed back. Only the scrape can see the difference, which is
    exactly why the bug survived until the gauge was first exported.
    """
    inflight_gauge.set(0)

    assert _wire_value(client.get("/metrics").text, _INFLIGHT) == pytest.approx(0.0), (
        "the scrape counted itself — /metrics is missing from INFLIGHT_EXCLUDED_PATHS"
    )


def test_healthz_probe_is_not_counted_as_inflight_work(client: TestClient) -> None:
    """A liveness probe in flight must not make an idle instance look busy.

    Same drain gate, subtler failure: probes are short and periodic, so
    counting them makes ``sum(...) == 0`` flake intermittently rather than fail
    outright — strictly harder to diagnose than the /metrics case.

    Observed from inside the handler, since a probe is only in flight while it
    is being served.
    """
    from parallax.server.middleware.dual_read_snapshot import INFLIGHT_EXCLUDED_PATHS

    assert "/healthz" in INFLIGHT_EXCLUDED_PATHS

    inflight_gauge.set(0)
    assert client.get("/healthz").status_code == 200
    # The scrape that follows sees no residue from the probe either.
    assert _wire_value(client.get("/metrics").text, _INFLIGHT) == pytest.approx(0.0)


def test_real_work_is_still_counted(client: TestClient) -> None:
    """The exclusion must not disarm the gauge for actual application work.

    The failure mode of an over-broad exclusion is silent and severe: a gauge
    stuck at 0 makes the drain gate pass instantly and every deploy cut live
    requests. Asserted from INSIDE a handler on a non-excluded path, which is
    the only moment the increment is observable.
    """
    from parallax.router.inflight import get_inflight_count
    from parallax.server.middleware.dual_read_snapshot import INFLIGHT_EXCLUDED_PATHS

    observed: list[int] = []

    def probe() -> dict[str, bool]:
        observed.append(get_inflight_count())
        return {"ok": True}

    app = client.app
    app.add_api_route("/__inflight_probe__", probe, methods=["GET"])

    inflight_gauge.set(0)
    assert TestClient(app).get("/__inflight_probe__").status_code == 200

    assert observed and observed[0] >= 1, (
        f"a non-excluded route must be counted while in flight; saw {observed}. "
        f"INFLIGHT_EXCLUDED_PATHS={sorted(INFLIGHT_EXCLUDED_PATHS)}"
    )
    assert get_inflight_count() == 0, "gauge must return to zero after the request"


# ---------------------------------------------------------------------------
# Exposition fidelity
# ---------------------------------------------------------------------------


def test_exposed_values_match_the_default_registry(client: TestClient) -> None:
    """What the wire says equals what the collector holds, for all three.

    The general invariant behind the three tests above: the renderer must
    reproduce the default registry rather than mirror it into something with a
    life of its own. Catches a copy that silently drifts (stale cache, a
    snapshot taken at import time) as well as an outright missing series.
    """
    circuit_breaker_tripped_total.inc()
    drain_timeout_total.inc()
    inflight_gauge.set(3)

    body = client.get("/metrics").text

    # Counters: nothing about serving a scrape touches them, so the wire must
    # equal the collector exactly.
    for name in (_TRIPPED, _DRAIN):
        registry_value = REGISTRY.get_sample_value(name)
        assert registry_value is not None, f"{name} vanished from the default registry"
        assert _wire_value(body, name) == pytest.approx(registry_value), (
            f"{name}: wire disagrees with the default registry"
        )

    # Gauge: exact parity too, now that the scrape is excluded from its own
    # measurement. Before that fix this line needed a +1 fudge, which was the
    # tell that the exported series and the in-process reading had diverged.
    gauge_value = REGISTRY.get_sample_value(_INFLIGHT)
    assert gauge_value == pytest.approx(3.0)
    assert _wire_value(body, _INFLIGHT) == pytest.approx(gauge_value)


def test_series_carry_no_labels_beyond_scrape_identity(client: TestClient) -> None:
    """All three are unlabelled — this is the promtool input contract.

    ``prometheus/tests/parallax-dual-read.test.yml`` feeds
    ``CircuitBreakerTripped`` and ``DrainTimeoutDetected`` synthetic series
    carrying ONLY ``job`` and ``instance`` (which Prometheus attaches at scrape
    time, and which no exporter can see). If a future change adds a label here
    — a ``reason`` on the breaker trip, say — those alert tests would keep
    passing against a series shape that no longer exists, which is the same
    "the test agrees with itself, not with reality" failure that let this
    issue class survive three times. This test is the link between them:
    break the shape and it fails here first.
    """
    inflight_gauge.set(1)
    circuit_breaker_tripped_total.inc()
    drain_timeout_total.inc()

    body = client.get("/metrics").text

    for name in _ALL_THREE:
        samples = _wire_samples(body, name)
        assert samples, f"{name} absent from the scrape"
        assert [label_block for label_block, _ in samples] == [""], (
            f"{name} grew a label; update prometheus/tests/parallax-dual-read.test.yml "
            f"input_series to match. Saw: {samples}"
        )


def test_type_headers_declare_the_right_collector_kind(client: TestClient) -> None:
    """Counters must declare ``counter``; the gauge must declare ``gauge``.

    The gauge needed its own renderer: the counter helper appends ``_total``
    to the name and hardcodes ``# TYPE ... counter``, so routing the gauge
    through it would have emitted a header for ``parallax_inflight_requests_total``
    with no sample lines under it — a family that parses fine and reports
    nothing.
    """
    body = client.get("/metrics").text

    assert f"# TYPE {_TRIPPED} counter" in body
    assert f"# TYPE {_DRAIN} counter" in body
    assert f"# TYPE {_INFLIGHT} gauge" in body
    assert f"# TYPE {_INFLIGHT}_total" not in body, (
        "gauge rendered through the counter helper — headers without samples"
    )


def test_payload_parses_and_declares_each_family_exactly_once(client: TestClient) -> None:
    """The whole payload is valid exposition, with no duplicated family.

    The new names were added to ``_RESERVED_GAUGE_SUFFIXES`` because an
    in-house counter sanitizing to one of them would put a second metric of
    the same name in the same payload; Prometheus rejects the whole scrape
    when that happens, so the blast radius is every metric on the endpoint,
    not just the colliding one. Parsing the real payload is what proves the
    reservation holds — a substring check would not notice a duplicate.
    """
    inflight_gauge.set(2)
    body = client.get("/metrics").text

    families = [family.name for family in text_string_to_metric_families(body)]

    assert len(families) == len(set(families)), (
        f"duplicated metric family in /metrics: "
        f"{sorted({f for f in families if families.count(f) > 1})}"
    )
    # Parser reports counters under their munged base name.
    for parsed_name in ("parallax_circuit_breaker_tripped", "parallax_drain_timeout", _INFLIGHT):
        assert parsed_name in families, f"{parsed_name} missing from parsed families"


# ---------------------------------------------------------------------------
# Cold start — the deliberate inversion of the absent-not-zero rule
# ---------------------------------------------------------------------------

_COLD_START_SCRIPT = """
import json, os, sys

from fastapi.testclient import TestClient

from parallax.server.app import create_app

body = TestClient(create_app()).get("/metrics").text
found = {}
for raw in body.splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    series, _, value = line.rpartition(" ")
    if series in ("parallax_circuit_breaker_tripped_total",
                  "parallax_drain_timeout_total",
                  "parallax_inflight_requests"):
        found[series] = float(value)
sys.stdout.write(json.dumps(found))
"""


def test_cold_start_exports_all_three_before_any_observation(tmp_path: Path) -> None:
    """A process that has never observed anything still exports all three.

    Deliberately the OPPOSITE of the absent-not-zero rule the dual-read DoD
    gauges follow (#100/#101), and the inversion is the point:

      * The two counters back ``increase(...) > 0`` alerts. A counter that
        only appeared on its first increment would spring into existence
        already at 1.0, and ``increase()`` over samples that are all 1.0 is
        **0** — so the first trip, the event the CRITICAL alert exists for,
        would read as "measured, and healthy". Exporting from zero gives
        Prometheus the baseline sample that makes the step visible. The
        ``absent-until-first-increment`` case in
        ``prometheus/tests/parallax-dual-read.test.yml`` demonstrates the dead
        alert directly, against the real rule file.
      * The gauge is a live concurrency reading, not a rate over a corpus, so
        zero is a genuine measurement — the drain runbook has to tell "0 in
        flight, safe to stop" from "no data, cannot tell".

    Runs in a subprocess because the collectors are module-level singletons in
    the process-global default registry: by the time this file's other tests
    (or the circuit-breaker and lifespan suites) have run, no in-process
    assertion can still see a cold start. Same idiom as
    ``tests/apex/test_audit_db.py``.
    """
    env = dict(os.environ)
    env.update(
        {
            "SHADOW_LOG_DIR": str(tmp_path / "shadow"),
            "DUAL_READ_LOG_DIR": str(tmp_path / "dual_read"),
            "PARALLAX_DB_PATH": str(tmp_path / "cold.db"),
            "PARALLAX_VAULT_PATH": str(tmp_path / "vault"),
            "PARALLAX_SCHEMA_PATH": str(_REPO_ROOT / "parallax" / "schema.sql"),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dual_read").mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [sys.executable, "-c", _COLD_START_SCRIPT],
        capture_output=True,
        text=True,
        # Explicit, because ``text=True`` alone decodes with the host locale
        # codec and the server's open-mode startup banner is not ASCII. On a
        # cp950 Windows host that raises UnicodeDecodeError inside subprocess's
        # reader THREAD, which surfaces as an empty stdout and a warning rather
        # than a test error — the assertion below would then report a
        # misleading "{}" instead of the real scrape.
        encoding="utf-8",
        timeout=120,
        cwd=str(_REPO_ROOT),
        env=env,
    )

    assert result.returncode == 0, f"cold-start scrape failed:\n{result.stderr}"

    found = json.loads(result.stdout)
    assert found == {
        _TRIPPED: 0.0,
        _DRAIN: 0.0,
        _INFLIGHT: 0.0,
    }, (
        "a never-observed process must still export all three at 0.0 — "
        "absent-until-first-increment silences CircuitBreakerTripped and "
        f"DrainTimeoutDetected. Got: {found}"
    )
