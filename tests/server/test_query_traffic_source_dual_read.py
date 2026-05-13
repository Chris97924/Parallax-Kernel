"""HTTP integration coverage for /query traffic_source dual-read metrics."""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import prometheus_client
import pytest
from fastapi.testclient import TestClient

from parallax.server import create_app
from parallax.sqlite_store import connect


def _scrape_counter_value(metric_name: str, labels: dict[str, str]) -> float:
    for metric in prometheus_client.REGISTRY.collect():
        if metric.name in (metric_name, metric_name + "_total"):
            for sample in metric.samples:
                if sample.name.endswith("_total") and sample.labels == labels:
                    return sample.value
    return 0.0


@pytest.fixture()
def dual_read_client(
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("MEMORY_ROUTER", "true")
    monkeypatch.setenv("DUAL_READ", "true")
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

    def factory() -> sqlite3.Connection:
        return connect(db_path)

    app = create_app(db_factory=factory)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    ("headers", "traffic_source", "user_id"),
    [
        ({"X-Parallax-Traffic-Source": "synthetic"}, "synthetic", "query-ts-synthetic"),
        ({}, "natural", "query-ts-natural"),
    ],
)
def test_query_threads_traffic_source_to_aphelion_metric(
    dual_read_client: TestClient,
    headers: dict[str, str],
    traffic_source: str,
    user_id: str,
) -> None:
    labels = {"user_id": user_id, "traffic_source": traffic_source}
    before = _scrape_counter_value("parallax_aphelion", labels)

    response = dual_read_client.get(
        "/query",
        params={"kind": "recent", "user_id": user_id, "limit": 1},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    after = _scrape_counter_value("parallax_aphelion", labels)
    assert (after - before) == 1

    metrics_response = dual_read_client.get("/metrics")
    assert metrics_response.status_code == 200
    assert f'parallax_aphelion_total{{traffic_source="{traffic_source}",user_id="{user_id}"}}' in (
        metrics_response.text
    )
