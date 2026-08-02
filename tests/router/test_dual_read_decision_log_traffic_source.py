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
