"""Integration tests for the M3b dual-read gauges on ``GET /metrics``.

Story US-006-M3-T2.3: extend the existing ``/metrics`` endpoint with 4 new
Prometheus gauges + the ``parallax_arbitration_policy_version`` info-metric
without breaking the M2 shadow gauges.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from parallax.server.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build an app pointing at an isolated tmp shadow + dual-read log dir + DB."""
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(tmp_path / "dual_read"))
    monkeypatch.setenv("PARALLAX_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(_REPO_ROOT / "parallax" / "schema.sql"))
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dual_read").mkdir(parents=True, exist_ok=True)

    # Reset the metrics-route module cache so each test sees a fresh window.
    from parallax.server.routes import metrics as metrics_route

    metrics_route._reset_cache_for_tests()

    app = create_app()
    return TestClient(app)


def _write_dual_read(log_dir: Path, records: list[dict], date: str = "2026-04-26") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"dual-read-decisions-{date}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return path


def _decision(
    *,
    outcome: str = "match",
    winning_source: str = "parallax",
    traffic_source: str | None = None,
    write_error: bool = False,
    minutes_ago: int = 1,
) -> dict[str, Any]:
    """Build one in-window decision record.

    ``traffic_source=None`` omits the key entirely — that is the shape of
    every record written before the writer learned to record it, and the
    case the read-side ``unknown`` partition exists for. It is NOT the same
    as writing ``traffic_source: "natural"``.
    """
    ts = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=minutes_ago)
    record: dict[str, Any] = {
        "timestamp": ts.isoformat(),
        "outcome": outcome,
        "winning_source": winning_source,
        "write_error_observed": write_error,
        "conflict_event_id": None,
        "data_quality_flag": "normal",
    }
    if traffic_source is not None:
        record["traffic_source"] = traffic_source
    return record


def _write_in_window(log_dir: Path, records: list[dict[str, Any]]) -> Path:
    """Write records into today's log file so the 72h window includes them."""
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    return _write_dual_read(log_dir, records, date=today)


def _labelled_samples(body: str, metric: str) -> dict[str, float]:
    """Map ``traffic_source`` label value -> sample value for ``metric``."""
    out: dict[str, float] = {}
    prefix = f'{metric}{{traffic_source="'
    for line in body.splitlines():
        if line.startswith("#") or not line.startswith(prefix):
            continue
        source = line[len(prefix) :].split('"', 1)[0]
        out[source] = float(line.rsplit(" ", 1)[-1])
    return out


def _unlabelled_sample(body: str, metric: str) -> float | None:
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        if name == metric:
            return float(value)
    return None


def test_metrics_includes_dual_read_gauges(client: TestClient) -> None:
    """All 4 net-new dual-read gauges + policy_version info-metric present."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    for name in (
        "parallax_dual_read_discrepancy_rate",
        "parallax_arbitration_conflict_rate",
        "parallax_dual_read_write_error_rate",
        "parallax_arbitration_p99_latency_ms",
        "parallax_arbitration_policy_version",
    ):
        assert name in body, f"missing dual-read gauge {name} in /metrics output"


def test_metrics_preserves_m2_shadow_gauges(client: TestClient) -> None:
    """Story 6 must not regress the M2 shadow gauges already shipped."""
    resp = client.get("/metrics")
    body = resp.text
    for name in (
        "parallax_shadow_discrepancy_rate",
        "parallax_shadow_checksum_consistency",
        "parallax_shadow_log_records_total",
    ):
        assert name in body, f"M2 shadow gauge {name} regressed"


def test_metrics_policy_version_label_present(client: TestClient) -> None:
    """``parallax_arbitration_policy_version`` exposes a label with the RC string."""
    resp = client.get("/metrics")
    body = resp.text
    # Either as a label value or part of the help/value line — find the metric
    # line and assert the policy string is anywhere on it.
    found = False
    for line in body.splitlines():
        if line.startswith("parallax_arbitration_policy_version") and not line.startswith("#"):
            assert "v0.3.0-rc" in line, line
            found = True
    assert found, "parallax_arbitration_policy_version metric line not found"


# ---------------------------------------------------------------------------
# B1 — dual-read gauges partitioned by traffic_source
# ---------------------------------------------------------------------------


def test_dual_read_gauges_partitioned_by_traffic_source(client: TestClient, tmp_path: Path) -> None:
    """Each dual-read rate is emitted once per observed traffic_source.

    Without the label no PromQL selector can exclude the M4 burn-in
    synthetic loader, which is the entire reason ArbitrationConflictRateHigh
    has fired continuously since 2026-06-10.
    """
    _write_in_window(
        tmp_path / "dual_read",
        # Synthetic: 2/2 fallback → conflict rate 1.0 (the live burn-in shape).
        [_decision(winning_source="fallback", traffic_source="synthetic") for _ in range(2)]
        # Natural: 1 of 4 conflicts → 0.25.
        + [_decision(winning_source="fallback", traffic_source="natural")]
        + [_decision(winning_source="parallax", traffic_source="natural") for _ in range(3)],
    )

    body = client.get("/metrics").text
    conflict = _labelled_samples(body, "parallax_arbitration_conflict_rate")

    assert conflict["synthetic"] == pytest.approx(1.0)
    assert conflict["natural"] == pytest.approx(0.25)


def test_records_without_traffic_source_partition_as_unknown(
    client: TestClient, tmp_path: Path
) -> None:
    """A record with NO traffic_source field is 'unknown' on the read side.

    This is the read/write asymmetry that makes the whole change safe. The
    writer resolves an absent value to "natural" (fail-safe: unlabelled live
    traffic is real traffic). The reader must NOT, because a record with no
    field at all predates the writer being able to record one — its
    provenance is unaudited, and ~245k such records are known to be
    synthetic burn-in.
    """
    _write_in_window(
        tmp_path / "dual_read",
        [_decision(winning_source="fallback") for _ in range(3)],
    )

    body = client.get("/metrics").text
    conflict = _labelled_samples(body, "parallax_arbitration_conflict_rate")

    assert conflict["unknown"] == pytest.approx(1.0)
    assert "natural" not in conflict, (
        "field-absent records were read-defaulted to natural; that dumps the "
        "pre-2026-08-02 synthetic backlog into the Phase-2 gate partition"
    )


def test_unknown_backlog_does_not_pollute_natural_partition(
    client: TestClient, tmp_path: Path
) -> None:
    """The historical backlog must not move the natural-traffic numbers.

    Reproduces the live corpus in miniature: a large block of field-absent
    all-conflict records (the burn-in backlog) alongside a small block of
    explicitly-natural clean records. If the reader defaulted absent to
    natural, the natural conflict rate would read 50/54 ≈ 0.93 and
    ArbitrationConflictRateHigh{traffic_source="natural"} would fire on data
    that contains no natural traffic at all.
    """
    _write_in_window(
        tmp_path / "dual_read",
        [_decision(winning_source="fallback") for _ in range(50)]
        + [_decision(winning_source="parallax", traffic_source="natural") for _ in range(4)],
    )

    body = client.get("/metrics").text
    conflict = _labelled_samples(body, "parallax_arbitration_conflict_rate")

    assert conflict["natural"] == pytest.approx(0.0)
    assert conflict["unknown"] == pytest.approx(1.0)


def test_unrecognized_traffic_source_partitions_as_unknown(
    client: TestClient, tmp_path: Path
) -> None:
    """A present-but-foreign traffic_source is 'unknown', not 'natural'.

    The writer only ever emits "synthetic" or "natural", so anything else is
    hand-edited or foreign data and must not be attributed to the
    gate-bearing partition. Casing and surrounding whitespace ARE normalized
    (traffic-gap-resolution.md §6).
    """
    _write_in_window(
        tmp_path / "dual_read",
        [
            _decision(winning_source="fallback", traffic_source="bogus"),
            _decision(winning_source="parallax", traffic_source="  NATURAL  "),
        ],
    )

    body = client.get("/metrics").text
    conflict = _labelled_samples(body, "parallax_arbitration_conflict_rate")

    assert conflict["unknown"] == pytest.approx(1.0)
    assert conflict["natural"] == pytest.approx(0.0)


def test_dual_read_gauges_absent_rather_than_zero_when_no_records(
    client: TestClient,
) -> None:
    """An empty corpus emits NO rate series — never a healthy-looking 0.0.

    "No natural traffic has ever been observed" and "natural traffic is
    clean" must not be indistinguishable on the wire; that conflation is
    what let Gate-5 read a zero as a pass.
    """
    body = client.get("/metrics").text

    assert _labelled_samples(body, "parallax_arbitration_conflict_rate") == {}
    # Specifically NOT the old unlabelled `parallax_arbitration_conflict_rate 0.0`,
    # which reads as "measured, and fine".
    assert _unlabelled_sample(body, "parallax_arbitration_conflict_rate") is None
    # The family itself is still declared, so dashboards do not 404.
    assert "# TYPE parallax_arbitration_conflict_rate gauge" in body


# ---------------------------------------------------------------------------
# B5 — denominator + directory-health gauges
# ---------------------------------------------------------------------------


def test_dual_read_log_records_gauge_counts_every_partition(
    client: TestClient, tmp_path: Path
) -> None:
    """Record counts are emitted for all three partitions, zeros included.

    Unlike the rates, a count of zero is a real measurement ("walked the
    corpus, found none"), so the series must exist to be trusted.
    """
    _write_in_window(
        tmp_path / "dual_read",
        [_decision(traffic_source="synthetic") for _ in range(3)] + [_decision() for _ in range(2)],
    )

    body = client.get("/metrics").text
    counts = _labelled_samples(body, "parallax_dual_read_log_records_total")

    assert counts == {"natural": 0.0, "synthetic": 3.0, "unknown": 2.0}


def test_newest_record_age_gauge_partitioned_by_traffic_source(
    client: TestClient, tmp_path: Path
) -> None:
    """Freshness is reported per partition, from the newest record in each.

    The record COUNT is a lagging silence detector: a writer that stops leaves
    its backlog in the 72h window, so the count cannot reach zero for a full
    window. Age notices at once — this is what lets the silence alert fire in
    minutes rather than days.
    """
    _write_in_window(
        tmp_path / "dual_read",
        [
            _decision(traffic_source="synthetic", minutes_ago=90),
            _decision(traffic_source="synthetic", minutes_ago=2),
            _decision(traffic_source="natural", minutes_ago=45),
        ],
    )

    body = client.get("/metrics").text
    ages = _labelled_samples(body, "parallax_dual_read_log_newest_record_age_seconds")

    assert set(ages) == {"synthetic", "natural"}
    # Newest synthetic record is the 2-minute-old one, not the 90-minute-old one.
    assert ages["synthetic"] == pytest.approx(120, abs=60)
    assert ages["natural"] == pytest.approx(2700, abs=60)


def test_newest_record_age_absent_when_no_records(client: TestClient) -> None:
    """An empty corpus emits no freshness series at all.

    The silence alert leans on this: its ``absent(...)`` arm is what covers
    "there are no records to be fresh", and it only means something if the
    exposition genuinely omits the series rather than reporting a zero age,
    which would read as "just written".
    """
    body = client.get("/metrics").text

    assert _labelled_samples(body, "parallax_dual_read_log_newest_record_age_seconds") == {}
    assert _unlabelled_sample(body, "parallax_dual_read_log_newest_record_age_seconds") is None
    # The family is still declared so the gauge is discoverable when empty.
    assert "# TYPE parallax_dual_read_log_newest_record_age_seconds gauge" in body
    # ...and the count gauge still reports, which is how "nothing recent" is
    # told apart from "nothing at all".
    assert _labelled_samples(body, "parallax_dual_read_log_records_total") == {
        "natural": 0.0,
        "synthetic": 0.0,
        "unknown": 0.0,
    }


def test_dual_read_requests_counter_reaches_the_wire(client: TestClient) -> None:
    """The liveness counter must actually be exposed, not just registered.

    It lives in the DEFAULT registry, and ``_build_payload`` serializes a
    fresh ``CollectorRegistry`` — anything not explicitly plucked out never
    reaches a scrape. That is precisely how the never-exposed dual-read series
    tracked in #101 became invisible, so the alert's traffic guard would be
    querying a metric Prometheus has never seen.
    """
    from parallax.router.discrepancy_live import record_dual_read_request

    record_dual_read_request(traffic_source="synthetic")

    body = client.get("/metrics").text
    samples = _labelled_samples(body, "parallax_dual_read_requests_total")

    assert samples.get("synthetic", 0.0) >= 1.0, body[-2000:]


def test_dir_missing_gauge_distinguishes_misconfig_from_quiet_window(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong log dir reads differently from a real but empty one.

    This is the Gate-5 failure mode encoded as a test: that investigation
    pointed at an abandoned directory, measured zero records, and concluded
    the exposition was stale. With only rate gauges the two states are
    byte-identical on the wire.
    """
    from parallax.server.routes import metrics as metrics_route

    # Healthy but quiet: the directory exists and holds no records.
    body = client.get("/metrics").text
    assert _unlabelled_sample(body, "parallax_dual_read_log_dir_missing") == pytest.approx(0.0)
    assert _labelled_samples(body, "parallax_dual_read_log_records_total") == {
        "natural": 0.0,
        "synthetic": 0.0,
        "unknown": 0.0,
    }

    # Misconfigured: the resolved directory does not exist at all.
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(tmp_path / "nope" / "not-a-dir"))
    metrics_route._reset_cache_for_tests()

    body = client.get("/metrics").text
    assert _unlabelled_sample(body, "parallax_dual_read_log_dir_missing") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# B4 / MED-METRICS-CACHE — one disk walk per cache miss
# ---------------------------------------------------------------------------


def test_dual_read_metrics_single_disk_walk_per_cache_miss(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 scrapes within TTL → exactly ONE load_records walk.

    The previous implementation called the three public rate functions, each
    of which runs its own ``load_records``, so a cache miss walked the JSONL
    directory three times (~270 MB against the live corpus every 30s) while
    the docstring claimed one. Spying on the rate functions could not catch
    that — it counted calls, not walks — so this spies on the walk itself.
    """
    from parallax.server.routes import metrics as metrics_route

    walks = {"n": 0}
    real = metrics_route._dual_read_load_records

    def _spy(*args, **kwargs):
        walks["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(metrics_route, "_dual_read_load_records", _spy)
    metrics_route._reset_cache_for_tests()

    for _ in range(3):
        assert client.get("/metrics").status_code == 200

    assert walks["n"] == 1, f"expected 1 disk walk across 3 scrapes; saw {walks['n']}"


# ---------------------------------------------------------------------------
# MED-METRICS-EXC-CLASS — compute-error gauge surfaces failure
# ---------------------------------------------------------------------------


def test_metrics_compute_error_gauge_set_on_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch the corpus walk to raise → compute_error gauge == 1.0."""
    from parallax.server.routes import metrics as metrics_route

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated compute failure")

    monkeypatch.setattr(metrics_route, "_dual_read_load_records", _boom)

    # Reset cache so the broken function is actually called.
    metrics_route._reset_cache_for_tests()

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert _unlabelled_sample(
        resp.text, "parallax_dual_read_metrics_compute_error"
    ) == pytest.approx(1.0)
    # A failed walk must not fabricate rate series that look measured.
    assert _labelled_samples(resp.text, "parallax_arbitration_conflict_rate") == {}
