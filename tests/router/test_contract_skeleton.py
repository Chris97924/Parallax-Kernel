"""US-005: Contract-test skeleton — the frozen port methods raise, by contract.

The three frozen methods (query/ingest/backfill) assert ``NotImplementedError``
directly. ``health()`` is the one method that works.

Why these are ``pytest.raises`` and no longer ``xfail(strict=True)``
--------------------------------------------------------------------
They were written as strict xfails while the real adapter was still pending,
which reads as "this test is expected to fail" when what is actually being
asserted is "this method is expected to *raise*" — a positive contract about
the frozen mock, not a deferred assertion about anything. Two costs came with
the disguise: a strict xfail reports as ``xfailed`` rather than ``passed``, so
these three never counted as coverage of the freeze; and had the mock started
raising a *different* exception, the xfail would have stayed green because any
failure satisfies it.

The real adapter arrived and is covered by
``tests/router/test_real_adapter_backfill.py`` and
``tests/router/test_backfill_runner.py``, so nothing here is deferred any more.
The exception type is named explicitly below, which is the assertion the xfail
could not make.
"""

from __future__ import annotations

import pytest

from parallax.router.contracts import BackfillRequest, IngestRequest, QueryRequest
from parallax.router.mock_adapter import MockMemoryRouter
from parallax.router.types import QueryType


def test_query_raises_not_implemented() -> None:
    """``MockMemoryRouter.query`` is frozen and raises ``NotImplementedError``."""
    router = MockMemoryRouter()
    req = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1")
    with pytest.raises(NotImplementedError, match=r"MockMemoryRouter\.query"):
        router.query(req)


def test_ingest_raises_not_implemented() -> None:
    """``MockMemoryRouter.ingest`` is frozen and raises ``NotImplementedError``."""
    router = MockMemoryRouter()
    req = IngestRequest(user_id="u1", kind="memory", payload={"body": "hi"})
    with pytest.raises(NotImplementedError, match=r"MockMemoryRouter\.ingest"):
        router.ingest(req)


def test_backfill_raises_not_implemented() -> None:
    """``MockMemoryRouter.backfill`` is frozen and raises ``NotImplementedError``."""
    router = MockMemoryRouter()
    req = BackfillRequest(user_id="u1", crosswalk_version="v1")
    with pytest.raises(NotImplementedError, match=r"MockMemoryRouter\.backfill"):
        router.backfill(req)


def test_health_works() -> None:
    """health() is the one port method that works in frozen mode."""
    report = MockMemoryRouter().health()
    assert report.ok is True
    assert report.query_type_count == 5
    assert report.ports_registered == ("QueryPort", "IngestPort", "InspectPort", "BackfillPort")
