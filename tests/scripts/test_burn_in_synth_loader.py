"""Regression tests for ``scripts/burn-in-synth-loader.py`` run_loader().

Covers the two-counter (5xx / 4xx) budget split introduced in round-4
of the Codex P1 review cycle.

Round-5 additions:
- P2: 3xx redirects must NOT reset counters and must increment
  consecutive_client_errors (trip the 4xx budget).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import importlib.util
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "burn-in-synth-loader.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_burn_in_synth_loader", str(SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_response(status_code: int) -> httpx.Response:
    """Build a minimal httpx.Response with the given status code."""
    response = httpx.Response(status_code=status_code)
    return response


def _mock_client_factory(responses: list[httpx.Response]):
    """Return a mock httpx.Client that yields ``responses`` in order, then loops."""
    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)
    call_count = [0]

    def _get(url, **kwargs):
        idx = call_count[0] % len(responses)
        call_count[0] += 1
        return responses[idx]

    mock_client.get.side_effect = _get
    mock_client.close = MagicMock()
    return mock_client


# ---------------------------------------------------------------------------
# test_persistent_4xx_eventually_exits_75
# ---------------------------------------------------------------------------


def test_persistent_4xx_eventually_exits_75():
    """Persistent 401 stream exhausts the 4xx budget and returns 75.

    With client_error_budget=5 and iterations=10, run_loader should
    return 75 after exactly 5 consecutive 4xx responses.
    """
    loader = _load_module()

    responses = [_make_response(401)] * 10  # all 401s

    mock_client = _mock_client_factory(responses)

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1", "key2"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=30,
            client_error_budget=5,
            iterations=10,
        )

    assert result == 75, f"expected 75 (4xx budget exhausted), got {result}"


# ---------------------------------------------------------------------------
# test_2xx_resets_both_counters
# ---------------------------------------------------------------------------


def test_2xx_resets_both_counters():
    """A 2xx after a mixed 4xx/5xx sequence resets both counters.

    Sequence: [4xx, 4xx, 4xx, 2xx, 5xx, 5xx, ...] — the 2xx at index 3
    resets both consecutive_errors and consecutive_client_errors to 0.
    With error_budget=30 and client_error_budget=300 neither small cluster
    trips the budget; the loop completes all ``iterations`` and returns 0.
    """
    loader = _load_module()

    # Build a repeating pattern: 3x 401, 1x 200, 2x 503, 1x 200
    pattern = [
        _make_response(401),
        _make_response(401),
        _make_response(401),
        _make_response(200),
        _make_response(503),
        _make_response(503),
        _make_response(200),
    ]
    # Enough iterations to run 2+ full cycles but not exhaust either budget
    iterations = 14  # 2 full cycles of the 7-item pattern

    mock_client = _mock_client_factory(pattern)

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=30,
            client_error_budget=300,
            iterations=iterations,
        )

    assert result == 0, (
        f"expected 0 (iterations exhausted without budget trip), got {result}"
    )


# ---------------------------------------------------------------------------
# P2 (round-5): 3xx redirects treated as failures, not successes
# ---------------------------------------------------------------------------


def test_3xx_redirect_increments_client_error_counter():
    """307 redirect must NOT reset counters — it increments consecutive_client_errors.

    With client_error_budget=3 and 4 consecutive 307s, run_loader should
    return 75 (budget exhausted), verifying that 3xx is NOT treated as success.
    """
    loader = _load_module()

    responses = [_make_response(307)] * 10  # all 307 redirects

    mock_client = _mock_client_factory(responses)

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=30,
            client_error_budget=3,
            iterations=10,
        )

    assert result == 75, (
        f"expected 75 (3xx redirect should exhaust 4xx budget), got {result}"
    )


def test_3xx_redirect_does_not_reset_prior_4xx_counter():
    """A 307 after 4xx errors must not zero consecutive_client_errors.

    Sequence: [401, 401, 307] with client_error_budget=3 must exhaust
    the budget (3 consecutive client errors including the redirect).
    """
    loader = _load_module()

    responses = [
        _make_response(401),
        _make_response(401),
        _make_response(307),
    ]

    mock_client = _mock_client_factory(responses)

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=30,
            client_error_budget=3,
            iterations=10,
        )

    assert result == 75, (
        f"expected 75 (401+401+307 should exhaust 4xx budget=3), got {result}"
    )


def test_2xx_after_3xx_resets_client_error_counter():
    """A genuine 2xx after a 3xx must reset consecutive_client_errors to 0.

    Sequence: [307, 200, 307, 200, ...] with client_error_budget=2 and
    iterations=10 must complete with exit 0, because each 2xx resets the
    counter before the next 307 can exhaust it.
    """
    loader = _load_module()

    responses = [
        _make_response(307),
        _make_response(200),
    ]

    mock_client = _mock_client_factory(responses)

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=30,
            client_error_budget=2,
            iterations=10,
        )

    assert result == 0, (
        f"expected 0 (200 resets counter each cycle), got {result}"
    )
