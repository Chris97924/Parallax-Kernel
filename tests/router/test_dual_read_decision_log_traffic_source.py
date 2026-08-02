"""Decision-log records must carry ``traffic_source`` (M4 hybrid-loader contract).

``docs/m4-prep/traffic-gap-resolution.md`` §6 and
``parallax/server/middleware/traffic_source.py`` make this normative:

    Downstream metrics emission MUST read ``request.state.traffic_source``

Every other metric surface honours it —
``parallax/canary_shadow.py`` and ``parallax/router/discrepancy_live.py``
both carry a ``traffic_source`` label. The dual-read decision JSONL did
not, so the three gauges derived from it
(``parallax_dual_read_discrepancy_rate``,
``parallax_arbitration_conflict_rate``,
``parallax_dual_read_write_error_rate``) could not separate the 1 qps
synthetic burn-in loader from production traffic. That is what pinned
``ArbitrationConflictRateHigh`` at 1.0 from 2026-06-10 onwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import prometheus_client
import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest
from parallax.router.dual_read import DualReadRouter
from parallax.router.types import QueryType


def _evidence(*ids: str) -> RetrievalEvidence:
    hits = tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids)
    return RetrievalEvidence(hits=hits, stages=("test",))


class _StubPort:
    def __init__(self, result: RetrievalEvidence) -> None:
        self._result = result

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        return self._result


def _request() -> QueryRequest:
    return QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1", params=None)


@pytest.fixture()
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "dual_read"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(target))
    return target


def _records(log_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(log_dir.glob("dual-read-decisions-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_decision_log_records_synthetic_traffic_source(log_dir: Path) -> None:
    """A synthetic-labelled query writes ``traffic_source="synthetic"``."""
    router = DualReadRouter(primary=_StubPort(_evidence("a")), secondary=_StubPort(_evidence("a")))
    router.query(_request(), traffic_source="synthetic")

    records = _records(log_dir)
    assert len(records) == 1, records
    assert records[0]["traffic_source"] == "synthetic"


def test_decision_log_defaults_traffic_source_to_natural(log_dir: Path) -> None:
    """Unlabelled traffic falls back to ``natural`` per middleware spec §6."""
    router = DualReadRouter(primary=_StubPort(_evidence("a")), secondary=_StubPort(_evidence("a")))
    router.query(_request())

    records = _records(log_dir)
    assert len(records) == 1, records
    assert records[0]["traffic_source"] == "natural"


def test_skipped_path_also_records_traffic_source(log_dir: Path) -> None:
    """The flag-off short-circuit writes a record too — it must be labelled."""
    router = DualReadRouter(primary=_StubPort(_evidence("a")), secondary=_StubPort(_evidence("a")))
    router.query(_request(), dual_read_override=False, traffic_source="synthetic")

    records = _records(log_dir)
    assert len(records) == 1, records
    assert records[0]["outcome"] == "skipped"
    assert records[0]["traffic_source"] == "synthetic"


# ---------------------------------------------------------------------------
# parallax_dual_read_requests_total — liveness counter for
# DualReadDecisionLogSilent. Its placement is the contract, not just its
# existence: it counts ATTEMPTS, so it must keep advancing when the thing it
# is watching is broken.
# ---------------------------------------------------------------------------


@pytest.fixture()
def dual_read_on(log_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``log_dir`` plus ``DUAL_READ=true``.

    The counter only advances when a decision record is genuinely expected —
    dual-read enabled AND the log enabled — so these tests must turn dual-read
    on explicitly. ``log_dir`` alone enables only the log, which is the
    healthy-but-disabled state exercised by
    ``test_request_counter_does_not_advance_when_dual_read_disabled``.
    """
    monkeypatch.setenv("DUAL_READ", "true")
    return log_dir


def _requests_total(traffic_source: str) -> float:
    """Read parallax_dual_read_requests_total{traffic_source} from the registry."""
    value = prometheus_client.REGISTRY.get_sample_value(
        "parallax_dual_read_requests_total",
        {"traffic_source": traffic_source},
    )
    return 0.0 if value is None else value


def test_request_counter_carries_traffic_source_and_no_user_id(dual_read_on: Path) -> None:
    """The counter is partitioned by traffic_source ONLY.

    A ``user_id`` label would make the alert's ``sum(increase(...))`` guard
    unusable: prometheus_client keeps every label set for the process
    lifetime, so a one-shot user's series sits pinned at 1 forever and
    contributes a delta of zero. That is the defect this counter replaced.
    """
    before = _requests_total("synthetic")
    router = DualReadRouter(primary=_StubPort(_evidence("a")), secondary=_StubPort(_evidence("a")))
    router.query(_request(), traffic_source="synthetic")

    assert _requests_total("synthetic") - before == 1

    for metric in prometheus_client.REGISTRY.collect():
        if metric.name == "parallax_dual_read_requests":
            for sample in metric.samples:
                assert "user_id" not in sample.labels, sample.labels
            break
    else:  # pragma: no cover - only reached if the counter vanished
        pytest.fail("parallax_dual_read_requests not registered")


def test_request_counter_advances_when_decision_log_write_fails(
    dual_read_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken writer must NOT silence the liveness signal.

    This is the placement contract. ``_log_decision`` swallows writer
    failures, so if the counter lived inside or after the write path it would
    stop exactly when the decision log stopped — the traffic guard would read
    false and ``DualReadDecisionLogSilent`` could never fire in the one
    situation it exists for. Attempts and successful writes must diverge here.
    """
    import parallax.router.dual_read as dual_read_module

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated decision-log write failure")

    monkeypatch.setattr(dual_read_module, "append_decision", _boom)

    before = _requests_total("natural")
    router = DualReadRouter(primary=_StubPort(_evidence("a")), secondary=_StubPort(_evidence("a")))
    router.query(_request())

    assert _requests_total("natural") - before == 1, "liveness counter died with the writer"
    assert _records(dual_read_on) == [], "the write was supposed to fail"


def test_request_counter_does_not_advance_when_dual_read_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy DISABLED system must not look like a broken writer.

    ``DualReadRouter`` still serves ordinary queries with ``DUAL_READ=false``,
    and the decision log defaults to mirroring that flag — so no records are
    written, correctly. If the attempt counter advanced anyway, the traffic
    guard would be true while freshness stayed absent and
    ``DualReadDecisionLogSilent`` would fire on a perfectly healthy system,
    including immediately after a rollback.
    """
    target = tmp_path / "dual_read_disabled"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DUAL_READ_LOG_ENABLED", raising=False)
    monkeypatch.setenv("DUAL_READ", "false")
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(target))

    before = _requests_total("natural")
    router = DualReadRouter(primary=_StubPort(_evidence("a")), secondary=_StubPort(_evidence("a")))
    router.query(_request())

    assert _requests_total("natural") - before == 0, (
        "disabled dual-read advanced the liveness counter; the silence alert "
        "would fire on a healthy disabled system"
    )
    assert _records(target) == []


def test_request_counter_advances_when_primary_query_raises(dual_read_on: Path) -> None:
    """Counted at request entry, so even a failed request registers as traffic.

    Primary failures propagate by design and write no decision record. If they
    also went uncounted, a wholly broken primary would look like "no traffic"
    and suppress the silence alert — the same hole one layer up.
    """

    class _RaisingPort:
        def query(self, request: QueryRequest) -> RetrievalEvidence:
            raise RuntimeError("primary is down")

    before = _requests_total("natural")
    router = DualReadRouter(primary=_RaisingPort(), secondary=_StubPort(_evidence("a")))
    with pytest.raises(RuntimeError, match="primary is down"):
        router.query(_request())

    assert _requests_total("natural") - before == 1
    assert _records(dual_read_on) == []
