"""Mutation-hardening for ``parallax.server.lifespan`` (land/20260823 wave 4, S4).

Additive companion to ``tests/server/test_lifespan.py``,
``tests/server/test_drain_timeout_durable_counter_106.py``,
``tests/server/test_drain_timeout_durable_signal_106.py`` and
``tests/server/test_app_t14_wiring.py``. Every test below exists because a
semantic mutant survived that set. Patches and exit codes are in
``mutations-w4-obs.json``.

The shape of the blind spot
---------------------------
``test_lifespan.py`` drives ``_drain_inflight`` with tiny explicit arguments
(``timeout_seconds=0.2``) so the suite stays fast. That is the right call for
those tests and it is also why three whole classes of defect are invisible to
them:

* **The shipped constants are never used.** Every test passes its own timeout
  and poll interval, so ``DRAIN_TIMEOUT_SECONDS`` (900.0 — the 15 minutes the
  rollback runbook promises) and ``DRAIN_POLL_INTERVAL_SECONDS`` (0.5) are
  never read by an assertion. The runbook is the contract; the constants are
  where it is written down.
* **Replay idempotence is asserted only for the zero case.** The restore is
  ``inc(journal_total - current)`` precisely so a redeploy with no new timeout
  adds nothing. A restart *after* a real timeout — the case where the
  difference between ``delta`` and ``journal.total`` actually shows — is the
  one not constructed.
* **The deadline arithmetic is never bracketed.** A drain that waits twice as
  long still drains, so ``deadline = start + timeout_seconds`` can be widened
  without any existing test noticing; only measuring the elapsed time against
  a literal catches it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import pytest

from parallax.router.inflight import get_inflight_count, inflight_gauge
from parallax.server import lifespan as lifespan_mod


def _counter_value() -> float:
    return lifespan_mod.drain_timeout_total._value.get()  # noqa: SLF001


@pytest.fixture(autouse=True)
def _clean_gauge() -> None:
    """The inflight gauge is process-global; return it to zero either side."""
    _drain_gauge_to_zero()
    yield
    _drain_gauge_to_zero()


def _drain_gauge_to_zero() -> None:
    current = get_inflight_count()
    for _ in range(abs(current)):
        if current > 0:
            inflight_gauge.dec()
        else:
            inflight_gauge.inc()


# ---------------------------------------------------------------------------
# The shipped constants ARE the rollback contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_drain_constants_are_the_documented_values() -> None:
    """900s / 0.5s / "drain_timeout" are all operator-facing.

    The 15-minute window is what ralplan §3 M3-T1.4 promises the rollback
    procedure; the poll interval sets how quickly a clean drain is noticed; and
    the event key is what a log-based alert matches on, kept stable on purpose
    so a reworded sentence cannot stop it matching. All three are literals
    here because every existing drain test supplies its own values.
    """
    assert lifespan_mod.DRAIN_TIMEOUT_SECONDS == 900.0
    assert lifespan_mod.DRAIN_POLL_INTERVAL_SECONDS == 0.5
    assert lifespan_mod.DRAIN_TIMEOUT_EVENT == "drain_timeout"


# ---------------------------------------------------------------------------
# Replay idempotence — the property that makes a redeploy safe
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_restore_with_nothing_new_stays_completely_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A redeploy with no new timeout must not even log.

    The short-circuit is ``delta <= 0``, and the case that distinguishes it
    from ``delta < 0`` is exactly ``delta == 0`` — the ordinary redeploy, the
    single most common path through this function. Under the relaxed
    comparison the counter is still correct (``inc(0.0)`` is a no-op) and the
    return value is still ``0.0``, so nothing about the metric changes; the
    only observable is an INFO line claiming "restored 0 drain-timeout
    event(s)" on every restart, which is log noise that reads as activity.
    That makes the log line the assertion.

    The journal is written to hold exactly the counter's current value so the
    delta is zero regardless of what earlier tests left in the process-global
    counter.
    """
    journal = tmp_path / "level.json"
    journal.write_text(
        json.dumps({"drain_timeout_total": _counter_value()}), encoding="utf-8"
    )

    before = _counter_value()
    with caplog.at_level(logging.INFO, logger="parallax.server.lifespan"):
        assert lifespan_mod.restore_drain_timeout_counter(str(journal)) == 0.0

    assert _counter_value() == before
    assert not [r for r in caplog.records if "restored" in r.getMessage()], (
        "a restore with nothing to restore must not log that it restored anything"
    )


@pytest.mark.unit
def test_unreadable_journal_restores_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Silence here would read downstream as "this deployment never timed out".

    A corrupt journal means the restored total may under-count, and the whole
    point of ``DrainJournal.readable`` is that this is distinguishable from a
    clean zero. Dropping the warning throws away the distinction at the only
    place that consumes it.
    """
    journal = tmp_path / "corrupt.json"
    journal.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="parallax.server.lifespan"):
        lifespan_mod.restore_drain_timeout_counter(str(journal))

    assert any(
        "under-reported" in record.getMessage() for record in caplog.records
    ), "an unreadable journal must be reported at WARNING"


# ---------------------------------------------------------------------------
# Drain loop arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_drain_returns_promptly_when_nothing_is_in_flight() -> None:
    """``count <= 0`` is the clean-drain exit, and zero must take it.

    Tightening it to ``count < 0`` makes the exit unreachable — the tracker
    never goes negative — so every shutdown burns the full timeout and reports
    a drain timeout that did not happen. With the shipped 900s constant that is
    a 15-minute hang on every deploy.
    """
    before = _counter_value()
    start = time.monotonic()

    asyncio.run(
        lifespan_mod._drain_inflight(timeout_seconds=5.0, poll_interval_seconds=0.05)
    )

    assert time.monotonic() - start < 2.0, "a zero-inflight drain must not wait"
    assert _counter_value() == before, "a clean drain records no timeout"


@pytest.mark.unit
def test_drain_gives_up_at_the_deadline_it_was_given(tmp_path: Path) -> None:
    """The deadline is ``start + timeout_seconds``, not a multiple of it.

    A drain that waits twice as long still eventually times out and still
    increments the counter, so every existing assertion holds. Only the elapsed
    time distinguishes them — measured against literals, and generously, so the
    test cannot flake on a slow box while still failing a 2x deadline.

    The bracket is scaled rather than loosened. The measured window is not just
    the drain: ``time.monotonic()`` is sampled after ``asyncio.run`` returns, so
    loop setup/teardown and a full journal write (mkdir, NamedTemporaryFile,
    json.dump, os.fsync, os.replace) all land inside it. At the original 0.4s
    deadline that left ~0.35s of headroom, which one fsync stall on a loaded
    Windows box eats — and the upper bound could not simply be widened, because
    0.8 is exactly where the ``deadline * 2`` mutant lands. Multiplying both
    sides by five keeps the identical 2x discrimination while giving 2s of
    absolute headroom.
    """
    inflight_gauge.inc()
    start = time.monotonic()

    asyncio.run(
        lifespan_mod._drain_inflight(
            timeout_seconds=2.0,
            poll_interval_seconds=0.05,
            journal_path=str(tmp_path / "journal.json"),
        )
    )
    elapsed = time.monotonic() - start

    assert 2.0 <= elapsed < 4.0, f"drain must stop at its own deadline, took {elapsed:.3f}s"


@pytest.mark.unit
def test_drain_does_not_overshoot_the_deadline_by_a_poll_interval(
    tmp_path: Path,
) -> None:
    """The final sleep is clamped to the time actually remaining.

    ``asyncio.sleep(poll_interval_seconds)`` unclamped overshoots by up to a
    full interval. At the shipped 0.5s that is invisible; the clamp exists so a
    caller passing a coarse poll interval still gets the timeout it asked for,
    and this is what pins it.
    """
    inflight_gauge.inc()
    start = time.monotonic()

    asyncio.run(
        lifespan_mod._drain_inflight(
            timeout_seconds=0.2,
            poll_interval_seconds=2.0,
            journal_path=str(tmp_path / "journal.json"),
        )
    )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, (
        f"a 2.0s poll interval must be clamped to the 0.2s remaining, took {elapsed:.3f}s"
    )


@pytest.mark.unit
def test_reported_inflight_count_is_re_read_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The number in the log and the journal is the count AT the cut, not before it.

    ``final_count = get_inflight_count()`` deliberately re-reads rather than
    reusing ``count`` from the top of the loop. Requests keep completing while
    the drain waits, so the top-of-loop value is stale by exactly the interval
    the operator cares about — it overstates how much work was actually cut off,
    which is the one number the drain-timeout WARNING exists to carry.

    Both variants report a positive count and both journal an event, so only a
    changing count distinguishes them: the stub returns 5 on the first read and
    2 on every read after it.
    """
    from parallax.server.drain_journal import read_drain_journal

    reads: list[int] = []

    def _counts() -> int:
        reads.append(1)
        return 5 if len(reads) == 1 else 2

    monkeypatch.setattr(lifespan_mod, "get_inflight_count", _counts)
    journal = tmp_path / "journal.json"

    asyncio.run(
        lifespan_mod._drain_inflight(
            timeout_seconds=0.0, poll_interval_seconds=0.05, journal_path=str(journal)
        )
    )

    assert read_drain_journal(journal).last_inflight_count == 2, (
        "the count must be re-read at the deadline, not carried from the loop head"
    )


@pytest.mark.integration
def test_lifespan_startup_restores_the_journal_into_the_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore has to be WIRED, not merely correct.

    ``restore_drain_timeout_counter`` being right is worth nothing if nothing
    calls it, and that call is a single unguarded line at the top of the
    lifespan. Removing it leaves every unit test of the restore green while
    ``DrainTimeoutDetected`` goes back to never firing — the exact shape of the
    #106.3 defect, which is why the wiring is asserted end-to-end here rather
    than by inspecting the source.
    """
    from fastapi import FastAPI

    from parallax.server.drain_journal import record_drain_timeout

    journal = tmp_path / "journal.json"
    record_drain_timeout(inflight_count=1, timeout_seconds=900.0, path=journal)
    # Put the journal ahead of the live counter so the restore has a real delta.
    monkeypatch.setenv("PARALLAX_DRAIN_JOURNAL_PATH", str(journal))
    monkeypatch.setenv("PARALLAX_AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    target = _counter_value() + 1.0
    journal.write_text(json.dumps({"drain_timeout_total": target}), encoding="utf-8")

    app = FastAPI()

    async def _enter_and_exit() -> None:
        async with lifespan_mod.parallax_lifespan(app):
            pass

    asyncio.run(_enter_and_exit())

    assert _counter_value() == target, (
        "lifespan startup must carry the previous process's drain timeout into "
        "this process's counter"
    )
    assert app.state.audit_db_path is not None


@pytest.mark.integration
def test_lifespan_startup_runs_the_audit_db_boot_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``validate=True`` is the difference between a boot gate and a connect.

    ``open_audit_db(..., validate=True)`` is what runs the spec section 4
    startup checks — absolute path, parent exists and is writable, quick_check,
    write probe — and the flag is documented as "set False only for tests on
    disposable temp DBs". Flipping it in the lifespan leaves every existing
    assertion green: the connection still opens, the app still starts, and a
    misconfigured deployment serves traffic against an audit DB nobody checked.

    A relative path is the cheapest observable difference between the two: the
    gate rejects it outright, while a bare ``sqlite3.connect`` happily creates
    the file in whatever the process's working directory happens to be — which
    is exactly the misconfiguration the absolute-path rule exists to catch.
    """
    from fastapi import FastAPI

    from parallax.apex.audit_db import AuditDbConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PARALLAX_AUDIT_DB_PATH", "relative_audit.db")
    monkeypatch.setenv("PARALLAX_DRAIN_JOURNAL_PATH", str(tmp_path / "journal.json"))
    app = FastAPI()

    async def _enter_and_exit() -> None:
        async with lifespan_mod.parallax_lifespan(app):
            pass

    with pytest.raises(AuditDbConfigError, match="must be absolute"):
        asyncio.run(_enter_and_exit())
