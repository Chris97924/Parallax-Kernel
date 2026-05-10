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


# ---------------------------------------------------------------------------
# P2-A (round-6): query params must be passed via params= not f-string
# ---------------------------------------------------------------------------


def test_query_params_passed_via_params_kwarg():
    """Keys with special chars must be sent via params= not f-string interpolation.

    Verifies that client.get is called with a bare endpoint URL and a
    params dict — never with query-string interpolated into the URL.
    """
    loader = _load_module()

    special_key = "key with spaces & reserved=chars?#hash"
    captured_calls = []

    def _capturing_get(url, **kwargs):
        captured_calls.append({"url": url, "kwargs": kwargs})
        return _make_response(200)

    mock_client = _mock_client_factory([_make_response(200)])
    mock_client.get.side_effect = _capturing_get

    base_endpoint = "http://127.0.0.1:8000/query"

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        loader.run_loader(
            endpoint=base_endpoint,
            sample_keys=[special_key],
            user_id="test-user",
            interval_seconds=0,
            iterations=1,
        )

    assert len(captured_calls) == 1
    call = captured_calls[0]
    # URL must be the bare endpoint — no query string embedded
    assert "?" not in call["url"], (
        f"endpoint URL must not contain '?', got: {call['url']!r}"
    )
    assert call["url"] == base_endpoint
    # params must be a dict with the special key passed verbatim
    assert "params" in call["kwargs"], "client.get must be called with params= kwarg"
    assert call["kwargs"]["params"]["q"] == special_key
    assert call["kwargs"]["params"]["kind"] == "recent"


def test_query_params_user_id_in_params_dict():
    """user_id with reserved chars must appear in params dict, not the URL."""
    loader = _load_module()

    special_user = "user&id=with+reserved#chars"
    captured_calls = []

    def _capturing_get(url, **kwargs):
        captured_calls.append({"url": url, "kwargs": kwargs})
        return _make_response(200)

    mock_client = _mock_client_factory([_make_response(200)])
    mock_client.get.side_effect = _capturing_get

    base_endpoint = "http://127.0.0.1:8000/query"

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        loader.run_loader(
            endpoint=base_endpoint,
            sample_keys=["somekey"],
            user_id=special_user,
            interval_seconds=0,
            iterations=1,
        )

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert "?" not in call["url"]
    assert call["kwargs"]["params"]["user_id"] == special_user


# ---------------------------------------------------------------------------
# P2-B (round-6): 5xx streak resets on any non-5xx HTTP response
# ---------------------------------------------------------------------------


def test_interleaved_503_401_does_not_trip_5xx_budget():
    """Sequence [503, 401, 503, 401, 503, 401] must NOT trip the 5xx budget.

    The 401s break the 5xx streak so consecutive_errors never reaches 3.
    With client_error_budget=30 and 6 iterations, loop completes (exit 0)
    or exits 75 via client_error_budget only — never via error_budget=3.
    """
    loader = _load_module()

    responses = [
        _make_response(503),
        _make_response(401),
        _make_response(503),
        _make_response(401),
        _make_response(503),
        _make_response(401),
    ]

    mock_client = _mock_client_factory(responses)

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=3,
            client_error_budget=30,
            iterations=6,
        )

    # Must not trip 5xx budget (error_budget=3). 3 client errors < budget=30.
    assert result == 0, (
        f"expected 0 (5xx streak broken by 4xx, no budget trip), got {result}"
    )


def test_5xx_streak_resets_after_401():
    """Sequence [503, 503, 401, 503] — streak resets after 401; last 503 is count=1.

    With error_budget=3, the trailing 503 after the 401 reset is count=1,
    never reaching 3. Loop completes all 4 iterations with exit 0.
    """
    loader = _load_module()

    responses = [
        _make_response(503),
        _make_response(503),
        _make_response(401),
        _make_response(503),
    ]

    mock_client = _mock_client_factory(responses)

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=3,
            client_error_budget=30,
            iterations=4,
        )

    assert result == 0, (
        f"expected 0 (5xx streak reset by 401; only 1 trailing 503), got {result}"
    )


def test_three_consecutive_503_trips_5xx_budget():
    """Sequence [503, 503, 503] with error_budget=3 MUST trip the 5xx exit.

    Confirms that the reset only fires on non-5xx; a genuine unbroken
    5xx streak still exhausts the budget and returns 75.
    """
    loader = _load_module()

    responses = [_make_response(503)] * 10

    mock_client = _mock_client_factory(responses)

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=3,
            client_error_budget=30,
            iterations=10,
        )

    assert result == 75, (
        f"expected 75 (3 consecutive 503s exhaust error_budget=3), got {result}"
    )


# ---------------------------------------------------------------------------
# P2 (round-7): 5xx and transport exceptions reset consecutive_client_errors
# ---------------------------------------------------------------------------


def test_interleaved_401_503_does_not_trip_4xx_budget():
    """Sequence [401, 503, 401, 503, 401, 503] must NOT trip the 4xx budget.

    Each 503 resets consecutive_client_errors to 0, so the 4xx streak
    never reaches client_error_budget=3. With iterations=6, exits 0.
    Also must NOT trip 5xx budget (each 401 already resets that, per round-6).
    """
    loader = _load_module()

    responses = [
        _make_response(401),
        _make_response(503),
        _make_response(401),
        _make_response(503),
        _make_response(401),
        _make_response(503),
    ]

    mock_client = _mock_client_factory(responses)

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=3,
            client_error_budget=3,
            iterations=6,
        )

    assert result == 0, (
        f"expected 0 (5xx resets 4xx streak; no budget tripped), got {result}"
    )


def test_503_resets_4xx_streak_allowing_continued_401s():
    """Sequence [401, 401, 503, 401] must NOT trip the 4xx budget.

    After 2 consecutive 401s (streak=2), the 503 resets consecutive_client_errors
    to 0. The trailing 401 then counts as 1, never reaching client_error_budget=3.
    With iterations=4, exits 0.
    """
    loader = _load_module()

    responses = [
        _make_response(401),
        _make_response(401),
        _make_response(503),
        _make_response(401),
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
            iterations=4,
        )

    assert result == 0, (
        f"expected 0 (503 resets 4xx counter; trailing 401 is streak=1), got {result}"
    )


def test_three_consecutive_401_still_trips_4xx_budget():
    """Sequence [401, 401, 401] with client_error_budget=3 MUST trip exit 75.

    Confirms the reset only fires on 5xx/transport — a genuine unbroken
    4xx streak still exhausts the budget.
    """
    loader = _load_module()

    responses = [_make_response(401)] * 10

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
        f"expected 75 (3 consecutive 401s exhaust client_error_budget=3), got {result}"
    )


def test_transport_error_resets_4xx_streak():
    """Sequence [401, 401, ConnectError, 401] must NOT trip the 4xx budget.

    The ConnectError (httpx.HTTPError) resets consecutive_client_errors to 0.
    The trailing 401 then counts as streak=1, never reaching client_error_budget=3.
    With iterations=4, exits 0 (error_budget=30 is not tripped by 1 transport error).
    """
    loader = _load_module()

    connect_error = httpx.ConnectError("connection refused")
    responses_or_exc = [
        _make_response(401),
        _make_response(401),
        connect_error,  # transport exception — resets 4xx streak
        _make_response(401),
    ]

    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.close = MagicMock()
    call_count = [0]

    def _get(url, **kwargs):
        idx = call_count[0] % len(responses_or_exc)
        call_count[0] += 1
        item = responses_or_exc[idx]
        if isinstance(item, Exception):
            raise item
        return item

    mock_client.get.side_effect = _get

    with patch.object(loader.httpx, "Client", return_value=mock_client):
        result = loader.run_loader(
            endpoint="http://127.0.0.1:8000/query",
            sample_keys=["key1"],
            user_id="test-user",
            interval_seconds=0,
            error_budget=30,
            client_error_budget=3,
            iterations=4,
        )

    assert result == 0, (
        f"expected 0 (ConnectError resets 4xx streak; trailing 401 is streak=1), "
        f"got {result}"
    )
