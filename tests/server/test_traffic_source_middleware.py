"""Tests for parallax.server.middleware.traffic_source.

Spec ground truth: docs/m4-prep/traffic-gap-resolution.md §6.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from parallax.server.middleware.traffic_source import (
    DEFAULT_LABEL,
    HEADER_NAME,
    KNOWN_VALUES,
    install_middleware,
    resolve_traffic_source,
)

# ---- resolve_traffic_source pure-fn semantics -------------------------------


@pytest.mark.unit
class TestResolveSemantics:
    def test_synthetic_passes_through(self) -> None:
        assert resolve_traffic_source("synthetic") == "synthetic"

    def test_natural_passes_through(self) -> None:
        assert resolve_traffic_source("natural") == "natural"

    def test_absent_defaults_to_natural(self) -> None:
        # Spec §6: absent → "natural" — production traffic must not be lost.
        assert resolve_traffic_source(None) == DEFAULT_LABEL
        assert DEFAULT_LABEL == "natural"

    def test_unrecognized_value_defaults_to_natural(self) -> None:
        assert resolve_traffic_source("unknown") == DEFAULT_LABEL
        assert resolve_traffic_source("test") == DEFAULT_LABEL

    @pytest.mark.parametrize("variant", ["Synthetic", "SYNTHETIC", "  synthetic  "])
    def test_case_and_whitespace_normalized(self, variant: str) -> None:
        assert resolve_traffic_source(variant) == "synthetic"

    def test_empty_string_defaults_to_natural(self) -> None:
        # Spec §6 implicit — empty string is not in the known set after
        # trim+lowercase, so it falls to the default.
        assert resolve_traffic_source("") == DEFAULT_LABEL

    def test_known_values_set_is_closed(self) -> None:
        # Defensive — if someone adds a third value, multiple call sites
        # need updating in lockstep.
        assert KNOWN_VALUES == frozenset({"synthetic", "natural"})


# ---- ASGI middleware integration --------------------------------------------


@pytest.fixture()
def client_with_middleware() -> TestClient:
    """A FastAPI app with the traffic_source middleware + a probe route."""
    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request) -> JSONResponse:
        return JSONResponse({"traffic_source": request.state.traffic_source})

    install_middleware(app)
    return TestClient(app)


@pytest.mark.unit
class TestMiddleware:
    def test_default_natural_when_header_absent(self, client_with_middleware: TestClient) -> None:
        response = client_with_middleware.get("/probe")
        assert response.json() == {"traffic_source": "natural"}

    def test_synthetic_header_recognized(self, client_with_middleware: TestClient) -> None:
        response = client_with_middleware.get(
            "/probe", headers={HEADER_NAME: "synthetic"}
        )
        assert response.json() == {"traffic_source": "synthetic"}

    def test_unrecognized_header_falls_to_natural(
        self, client_with_middleware: TestClient
    ) -> None:
        response = client_with_middleware.get(
            "/probe", headers={HEADER_NAME: "anomaly"}
        )
        assert response.json() == {"traffic_source": "natural"}

    def test_install_middleware_idempotent(self) -> None:
        app = FastAPI()
        install_middleware(app)
        # Second install should not raise or stack a second middleware
        # (would double-snapshot otherwise).
        install_middleware(app)
        assert app.state._traffic_source_installed is True
