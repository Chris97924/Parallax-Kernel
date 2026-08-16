"""Mutation-hardening for ``parallax.canary.instrument`` (overnight-20260816 S10).

New test file — ``CanaryRequestRecorder`` had no direct tests. Every existing
exercise of it is in ``tests/observability/``, where it is a *fixture*: those
tests use ``recorder.request()`` to manufacture rows and then assert on what the
exporter computed from them. That aggregate oracle turns out to be a good deal
sharper than it looks — it already catches a hardcoded stage, a hardcoded
outcome, a hardcoded status, an ignored ``event_id`` and a second ``event_id``
on the outcome write, all of which distort the T-series it reads back. What it
does *not* see is everything that leaves the aggregate well-formed. Each test
below was written against a semantic mutant of ``instrument.py`` that the
pre-existing suite ran green:

  * ``span.recorded = bool(audit_ok and outcome_ok)`` degraded to ``or``, or to
    a bare ``True``. The module comment states the reason for ``and`` — "the
    exporter joins the two tables, so an event with only one half written
    contributes to no series at all" — and the recorder's whole point is that a
    drill can trust the flag. No test ever made either write fail, so a
    half-written event reporting success changed nothing.
  * The ``finally:`` degraded to an ``else:`` (record only when the body
    returns). The docstring is explicit that a raising request must still be
    measured "because the requests most likely to be slow are exactly the ones
    that break"; nothing exercised a raising body.
  * ``* 1000.0`` dropped from the duration, so every latency is persisted in
    seconds. ``test_the_cli_persists_a_measured_duration_not_a_constant`` pins
    the two failure modes that motivated it — ``d > 0`` and "not all 10.0" — and
    a uniformly rescaled duration satisfies both, so the *unit* was unpinned.
  * ``perf_counter`` swapped for wall-clock ``time.time()``. The ``d > 0``
    assertion would catch a clock that actually stepped backwards, but no test
    makes one step, so on any ordinary run the mutant is indistinguishable.
    Deciding it needs a clock that misbehaves on demand, which is what the fake
    below provides.
  * ``idempotency_hit=span.idempotency_hit`` hardcoded to ``False``. The
    observability fixtures set only ``outcome`` and ``response_status``, and no
    T-series reads the column, so the pass-through had no witness.
  * The ``CanaryRequestSpan`` defaults, the ``stage`` property, and the
    publication of ``span.latency_ms`` back onto the span — all of them read by
    callers (and by drills reporting a verdict), none of them read by the
    exporter, hence none of them pinned.

Where a test below also covers a mutant the exporter tests already catch, it is
stated as pinning the invariant at the unit boundary rather than as new
coverage; ``reports/S10-mutation-report.md`` records which is which.

The fake clock is installed by swapping the ``time`` module object out of
``instrument``'s own namespace rather than patching the stdlib in place, so the
substitution cannot leak into unrelated tests. It exposes both ``perf_counter``
and ``time`` so that the wall-clock mutant is killed by an *assertion* on the
recorded duration rather than by an ``AttributeError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from parallax.canary import instrument as instrument_mod
from parallax.canary.audit_log import AuditLog
from parallax.canary.instrument import CanaryRequestRecorder, CanaryRequestSpan
from parallax.canary.outcomes import OutcomeStore

_STAGE = "m4_1pct"


@pytest.fixture
def store(tmp_path: Path):
    """Audit log + outcome store over one shared DB file, as production wires them."""
    db = tmp_path / "canary_audit.db"
    audit = AuditLog(db_path=db)
    outcomes = OutcomeStore(db_path=db)
    yield audit, outcomes
    audit.close()
    outcomes.close()


@pytest.fixture
def recorder(store) -> CanaryRequestRecorder:
    audit, outcomes = store
    return CanaryRequestRecorder(audit_log=audit, outcome_store=outcomes, stage=_STAGE)


class _FakeTimeModule:
    """Stand-in for the ``time`` module with a scripted monotonic clock.

    ``perf_counter`` walks the supplied schedule; ``time`` (wall clock) walks it
    *backwards*, the way an NTP step or a DST jump would. A recorder that timed
    the request with the wall clock therefore produces a negative duration,
    which the assertions catch.
    """

    def __init__(self, schedule: list[float], wall: list[float] | None = None) -> None:
        self._schedule = list(schedule)
        self._wall = list(wall if wall is not None else [1_000_000.0, 999_999.0])

    def perf_counter(self) -> float:
        return self._schedule.pop(0) if self._schedule else 0.0

    def time(self) -> float:
        return self._wall.pop(0) if self._wall else 0.0


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeTimeModule:
    """Pin the measured duration at exactly 250.0 ms."""
    clock = _FakeTimeModule(schedule=[100.0, 100.25])
    monkeypatch.setattr(instrument_mod, "time", clock)
    return clock


class _BoomError(RuntimeError):
    """Sentinel for the raising-request path."""


# ===========================================================================
# Duration: measured, in milliseconds, from a monotonic source
# ===========================================================================


@pytest.mark.unit
class TestDurationIsMeasuredNotAssumed:
    def test_latency_is_elapsed_milliseconds(
        self, recorder: CanaryRequestRecorder, fake_clock: _FakeTimeModule
    ) -> None:
        """0.25 s of ``perf_counter`` must become 250.0 ms, not 0.25 and not < 0.

        Kills three mutants at once: ``* 1000.0`` dropped (would give 0.25),
        the subtraction reversed (would give -250.0), and ``perf_counter``
        swapped for the wall clock (the fake wall clock steps backwards, so a
        wall-clock recorder yields a negative duration).
        """
        with recorder.request() as span:
            pass
        assert span.latency_ms == 250.0

    def test_persisted_latency_is_the_measured_one(
        self, recorder: CanaryRequestRecorder, store, fake_clock: _FakeTimeModule
    ) -> None:
        """The audit row carries the measured duration — the exact one.

        ``test_the_cli_persists_a_measured_duration_not_a_constant`` already
        rejects the specific placeholder (``10.0``) and non-positive values.
        This pins the stronger property at the unit boundary: the number in the
        column is the number the recorder measured, so any *other* constant, or
        a rescaling of the real one, is caught too.
        """
        audit, _ = store
        with recorder.request() as span:
            pass
        row = audit.lookup(span.event_id)
        assert row is not None
        assert row.latency_ms == 250.0
        assert row.latency_ms == span.latency_ms

    def test_latency_is_non_negative_under_a_stepping_wall_clock(
        self, recorder: CanaryRequestRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wall clock that jumps backwards must not produce a negative latency.

        Separate from the pinned-value test above so the invariant is stated in
        its own right: a negative duration lands in the wrong histogram bucket
        and there is no value of ``latency_ms`` below zero that is meaningful.
        """
        clock = _FakeTimeModule(schedule=[500.0, 500.1], wall=[9_000.0, 3_000.0])
        monkeypatch.setattr(instrument_mod, "time", clock)
        with recorder.request() as span:
            pass
        assert span.latency_ms is not None
        assert span.latency_ms >= 0.0


# ===========================================================================
# Both halves, or neither: ``recorded`` is an AND
# ===========================================================================


@pytest.mark.unit
class TestRecordedFlagRequiresBothWrites:
    def test_recorded_true_only_when_both_writes_land(
        self, recorder: CanaryRequestRecorder
    ) -> None:
        with recorder.request() as span:
            span.outcome = "ok"
        assert span.recorded is True

    def test_audit_failure_makes_recorded_false(
        self, recorder: CanaryRequestRecorder, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Audit write fails, outcome write succeeds → NOT recorded.

        ``AuditLog.record`` is spec'd fire-and-forget and returns False rather
        than raising, so this is the shape a real disk failure takes. With
        ``and`` degraded to ``or`` — or with the flag hardcoded True — a drill
        would report PASS for an event that reaches no T-series at all.
        """
        audit, outcomes = store
        monkeypatch.setattr(audit, "record", lambda *a, **kw: False)
        with recorder.request() as span:
            span.outcome = "ok"
        assert span.recorded is False
        # The half that did succeed is still on disk — the flag is the only
        # signal that the pair is incomplete.
        assert outcomes.lookup(span.event_id) is not None

    def test_outcome_failure_makes_recorded_false(
        self, recorder: CanaryRequestRecorder, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Outcome write fails, audit write succeeds → NOT recorded."""
        audit, outcomes = store
        monkeypatch.setattr(outcomes, "record", lambda *a, **kw: False)
        with recorder.request() as span:
            span.outcome = "ok"
        assert span.recorded is False
        assert audit.lookup(span.event_id) is not None

    def test_both_failures_make_recorded_false(
        self, recorder: CanaryRequestRecorder, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit, outcomes = store
        monkeypatch.setattr(audit, "record", lambda *a, **kw: False)
        monkeypatch.setattr(outcomes, "record", lambda *a, **kw: False)
        with recorder.request() as span:
            span.outcome = "ok"
        assert span.recorded is False


# ===========================================================================
# A request that raises is still measured and still counted
# ===========================================================================


@pytest.mark.unit
class TestRaisingRequestIsStillRecorded:
    def test_exception_propagates_to_the_caller(self, recorder: CanaryRequestRecorder) -> None:
        """The recorder measures; it does not swallow."""
        with pytest.raises(_BoomError):
            with recorder.request():
                raise _BoomError("downstream blew up")

    def test_both_rows_are_written_when_the_body_raises(
        self, recorder: CanaryRequestRecorder, store
    ) -> None:
        """Dropping failures would bias T3 towards the fast path.

        Kills the mutant that moves the write out of ``finally`` into the
        success path: on it, neither table gets a row for a failed request.
        """
        audit, outcomes = store
        seen: list[CanaryRequestSpan] = []
        with pytest.raises(_BoomError):
            with recorder.request() as span:
                seen.append(span)
                span.response_status = 500
                span.outcome = "data_loss"
                raise _BoomError("downstream blew up")

        eid = seen[0].event_id
        row = audit.lookup(eid)
        assert row is not None, "a request that raised must still leave an audit row"
        assert row.response_status == 500
        outcome_row = outcomes.lookup(eid)
        assert outcome_row is not None, "a request that raised must still leave an outcome row"
        assert outcome_row.outcome == "data_loss"

    def test_latency_is_measured_when_the_body_raises(
        self, recorder: CanaryRequestRecorder, store, fake_clock: _FakeTimeModule
    ) -> None:
        audit, _ = store
        seen: list[CanaryRequestSpan] = []
        with pytest.raises(_BoomError):
            with recorder.request() as span:
                seen.append(span)
                raise _BoomError("boom")
        row = audit.lookup(seen[0].event_id)
        assert row is not None
        assert row.latency_ms == 250.0


# ===========================================================================
# One span, one event_id, two tables
# ===========================================================================


@pytest.mark.unit
class TestOneEventIdSpansBothTables:
    def test_both_writes_use_the_same_event_id(
        self, recorder: CanaryRequestRecorder, store
    ) -> None:
        """The exporter's join is on ``event_id``; two ids means zero series.

        The exporter tests already fail loudly if the outcome write is keyed
        separately — every T-series empties out. Stated here as a direct
        assertion so the cause is one row of output rather than five failing
        aggregate expectations.
        """
        audit, outcomes = store
        with recorder.request() as span:
            span.outcome = "ok"
        assert audit.lookup(span.event_id) is not None
        assert outcomes.lookup(span.event_id) is not None
        assert outcomes.lookup(span.event_id).event_id == audit.lookup(span.event_id).event_id

    def test_caller_supplied_event_id_is_used_verbatim(
        self, recorder: CanaryRequestRecorder, store
    ) -> None:
        """``event_id=`` is honoured, not overwritten by a fresh mint.

        The exporter tests notice this indirectly (they key their assertions off
        ``span.event_id``); pinned here as the parameter contract it is.
        """
        audit, outcomes = store
        supplied = "018bcfe5-6800-7bcd-af01-020304050607"
        with recorder.request(event_id=supplied) as span:
            span.outcome = "ok"
        assert span.event_id == supplied
        assert audit.lookup(supplied) is not None
        assert outcomes.lookup(supplied) is not None

    def test_minted_event_ids_are_unique_across_requests(
        self, recorder: CanaryRequestRecorder, store
    ) -> None:
        """Two un-keyed requests must not collide onto one row (both tables are
        keyed by ``event_id`` PRIMARY KEY, so a constant id silently upserts
        every request onto a single row)."""
        audit, _ = store
        ids = []
        for _ in range(5):
            with recorder.request() as span:
                span.outcome = "ok"
            ids.append(span.event_id)
        assert len(set(ids)) == 5
        assert all(audit.lookup(eid) is not None for eid in ids)


# ===========================================================================
# Field pass-through: what the caller set is what gets persisted
# ===========================================================================


@pytest.mark.unit
class TestSpanFieldsReachTheRightColumns:
    def test_response_status_is_persisted(
        self, recorder: CanaryRequestRecorder, store
    ) -> None:
        """A non-200 status must survive to the audit row.

        Also covered by the exporter's error-rate series; asserted directly here
        so the failure names the column rather than the derived ratio.
        """
        audit, _ = store
        with recorder.request() as span:
            span.response_status = 503
            span.outcome = "data_loss"
        row = audit.lookup(span.event_id)
        assert row is not None
        assert row.response_status == 503

    def test_idempotency_hit_is_persisted(
        self, recorder: CanaryRequestRecorder, store
    ) -> None:
        """Kills a hardcoded ``idempotency_hit=False``."""
        audit, _ = store
        with recorder.request() as span:
            span.idempotency_hit = True
            span.outcome = "ok"
        row = audit.lookup(span.event_id)
        assert row is not None
        assert row.idempotency_hit is True

    @pytest.mark.parametrize("outcome", ["ok", "discrepancy", "data_loss"])
    def test_outcome_is_persisted(
        self, recorder: CanaryRequestRecorder, store, outcome: str
    ) -> None:
        """A hardcoded ``outcome="ok"`` would zero the T2/T4 numerators while
        leaving every row count unchanged. The exporter tests catch that through
        the series; this catches it at the row."""
        _, outcomes = store
        with recorder.request() as span:
            span.outcome = outcome
        row = outcomes.lookup(span.event_id)
        assert row is not None
        assert row.outcome == outcome

    @pytest.mark.parametrize("stage", ["m4_1pct", "m4_10pct", "m4_50pct", "m4_100pct"])
    def test_recorder_stage_is_persisted(self, store, stage: str) -> None:
        """The recorder's stage — not a constant — tags the outcome row."""
        audit, outcomes = store
        rec = CanaryRequestRecorder(audit_log=audit, outcome_store=outcomes, stage=stage)
        assert rec.stage == stage
        with rec.request() as span:
            span.outcome = "ok"
        row = outcomes.lookup(span.event_id)
        assert row is not None
        assert row.stage == stage

    def test_untouched_span_records_the_documented_defaults(
        self, recorder: CanaryRequestRecorder, store
    ) -> None:
        """"The defaults describe a successful request" — a caller that only
        cares about timing says nothing and gets 200/ok."""
        audit, outcomes = store
        with recorder.request() as span:
            pass
        row = audit.lookup(span.event_id)
        assert row is not None
        assert row.response_status == 200
        assert row.idempotency_hit is False
        outcome_row = outcomes.lookup(span.event_id)
        assert outcome_row is not None
        assert outcome_row.outcome == "ok"

    def test_invalid_outcome_raises_rather_than_being_coerced(
        self, recorder: CanaryRequestRecorder, store
    ) -> None:
        """A mislabelled outcome corrupts the T2/T4 numerators, so the write
        must fail loudly (``OutcomeStore.record`` raises) rather than silently
        recording ``ok``."""
        audit, _ = store
        span_box: list[Any] = []
        with pytest.raises(ValueError, match="Unknown canary outcome"):
            with recorder.request() as span:
                span_box.append(span)
                span.outcome = "catastrophe"
        # The audit half is written before the outcome half raises, so the
        # duration is not lost — but the pair is incomplete and must not be
        # flagged as recorded.
        assert span_box[0].recorded is False
        assert audit.lookup(span_box[0].event_id) is not None
