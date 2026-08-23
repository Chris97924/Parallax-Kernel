"""Mutation-hardening for ``parallax.canary.drill`` (land-20260824 w5 S1).

Additive companion to ``test_drill.py``. Every test below exists because a
semantic mutant of the module SURVIVED the pre-existing suite
(``tests/drill/test_drill.py`` plus ``tests/observability/
test_canary_exporter_106.py``, which is the only other suite that reaches this
module). 66 mutants were applied one at a time to an otherwise pristine tree;
20 died against the existing suite and 46 walked straight through it.

Tally for this module: applied 66 / killed-by-new 46 / already-covered 20 /
unaddressed 0.

What the existing suite is blind to, and why
--------------------------------------------

  * **The drain drill is never observed, only totalled.** Every existing drain
    assertion reads ``drained_count`` and ``residual_count`` on a drill that
    was allowed to run to completion. Nothing looks at *what the loop did* --
    so the per-request work time can be computed from the wrong branch
    (``0.001 if dry_run`` inverted), the real-mode budget can flip ``min`` to
    ``max`` or divide by 2 instead of 4, and the loop can advance two requests
    per iteration on an even fleet, and every existing assertion still holds.
    The tests here inject a recording ``sleep`` and assert the exact sequence
    of durations the loop asked for.

  * **The two halves of the deadline fix each survive alone.** The force-cut
    test passes if EITHER the clock is ``perf_counter`` OR the comparison is
    ``>=`` -- so reverting either half on its own is invisible to it. That is
    not a hypothetical: ``perf_counter`` + ``>`` passes because the first
    reading is a hair above zero, and ``monotonic`` + ``>=`` passes because a
    clock that has not ticked reads exactly 0.0. Only the conjunction is
    correct, and only a clock the test controls can pin both independently.
    ``_FakeClock`` below advances only when the drill sleeps, so "the budget is
    spent" lands on an exact float and the ``>=``/``>`` boundary is a decided
    question rather than a race with the host clock.

  * **Defaults are never exercised.** Every existing call passes
    ``in_flight_count``/``reemit_count``/``concurrency`` explicitly, and
    ``run_full_drill`` is only ever called with all of them spelled out. So
    each default can be changed, and -- worse -- ``run_full_drill`` can quietly
    stop forwarding any argument it is given (``timeout_s``, ``dry_run``,
    ``stage``, ``outcome_store``, ``concurrency``, ``audit_log``) and the whole
    orchestrator still reports three passing drills. Six separate forwarding
    mutants survived. The tests here call the defaults with no arguments and
    assert the observations, and drive each forwarded argument to a value whose
    absence changes the report.

  * **Nothing ever fails a write.** ``AuditLog.record`` is spec'd
    fire-and-forget: it returns False instead of raising. The drill's entire
    verdict rests on those booleans, and no existing test makes one False -- so
    ``all()`` degrades to ``any()``, ``and`` degrades to ``or``, and the
    "recorded" observation can ignore the failure flag, all invisibly. The
    tests here wrap the instance method and inject a False at a chosen position
    in each pass.

  * **The re-emit payload is never read back.** The existing idempotency test
    counts rows. It never looks at a row. So the first pass can stamp a
    different latency, report a 500, or claim an idempotency hit, and the
    replay can drop the hit flag -- the row count stays put and the drill still
    passes. The tests here capture every ``AuditRecord`` the drill hands to the
    store and assert the field values as LITERALS. That is deliberate and must
    stay that way: reading the expectation back out of the module is exactly
    what made ``test_drain_default_timeout_is_60s``-style pins the only ones
    that work here.

  * **The status strings are only ever compared to themselves.** Every
    existing assertion is ``report.overall == DrillStatus.PASS``, so renaming
    the constant renames the expectation with it and the JSON the CLI emits
    changes shape with nothing failing. The tests below build step statuses
    from literal ``"pass"``/``"fail"``/``"skipped"`` strings and assert the
    literal wire values directly.

  * **The idempotency drill never proves it raced.** The existing tests assert
    exactly one worker invocation -- which is also what you get if the threads
    are started and joined one at a time, or if the verdict is hardcoded, or if
    the invariant is relaxed from ``== 1`` to ``>= 1``. A drill that no longer
    races is a drill that cannot fail, and it reports PASS forever. The tests
    here substitute a handler that makes concurrency observable (a barrier that
    only trips if the racers really overlap) and one that breaks the invariant
    on purpose, so the FAIL path is taken at least once.

Deliberately NOT enumerated: dropping the ``invocation_lock`` around
``invocations += 1``. Under the single-worker invariant the drill exists to
assert, that increment runs exactly once and the mutex is never contended, so a
kill would depend on a lost update that cannot be forced deterministically from
outside the function. It is listed here rather than silently omitted.
"""

from __future__ import annotations

import pathlib
import threading
import time as _real_time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

import parallax.canary.drill as drill_mod
from parallax.canary.audit_log import AuditLog, AuditRecord
from parallax.canary.drill import (
    DrillStatus,
    DrillStepResult,
    _aggregate_status,
    run_drain_drill,
    run_full_drill,
    run_idempotency_drill,
    run_reemit_drill,
)
from parallax.canary.outcomes import OutcomeStore

# ----------------------------------------------------------------------
# Fixtures and helpers
# ----------------------------------------------------------------------


@pytest.fixture
def audit_log(tmp_path: pathlib.Path) -> Iterator[AuditLog]:
    log = AuditLog(db_path=tmp_path / "harden_audit.db")
    try:
        yield log
    finally:
        log.close()


@pytest.fixture
def outcome_store(tmp_path: pathlib.Path) -> Iterator[OutcomeStore]:
    store = OutcomeStore(db_path=tmp_path / "harden_outcomes.db")
    try:
        yield store
    finally:
        store.close()


class _FakeClock:
    """Stand-in for the ``time`` module inside :mod:`parallax.canary.drill`.

    ``perf_counter`` advances ONLY when the drill sleeps, and by a fixed
    ``tick`` rather than by the duration requested. Decoupling the two is what
    lets one test pin the deadline arithmetic on exact floats while another
    pins the per-request work time, without either depending on the host clock.

    ``monotonic`` never advances. That is not laziness -- it is the behaviour
    of the real Windows ``monotonic`` inside one of its 15.625 ms ticks, which
    is precisely the condition under which a drain shorter than a tick measures
    as zero elapsed. A drill that reads ``monotonic`` therefore never observes
    its own deadline here, and says so loudly.
    """

    def __init__(self, tick: float) -> None:
        self.tick = tick
        self.now = 0.0
        self.sleeps: list[float] = []

    def perf_counter(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += self.tick


def _recording_sleep() -> tuple[Callable[[float], None], list[float]]:
    """A no-op ``sleep`` that records exactly what the drain loop asked for."""
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)

    return sleep, calls


def _spy_record(
    log: AuditLog, fail_at: frozenset[int] = frozenset()
) -> list[AuditRecord]:
    """Capture every record the drill writes; optionally fail chosen writes.

    ``fail_at`` holds 1-based positions in call order. A failed call returns
    False WITHOUT touching the database, which is exactly how the real
    ``AuditLog.record`` behaves on an error (criterion 1.8: swallow and report
    via the return value).
    """
    captured: list[AuditRecord] = []
    real = log.record

    def record(rec: AuditRecord, **kw: Any) -> bool:
        captured.append(rec)
        if len(captured) in fail_at:
            return False
        return real(rec, **kw)

    log.record = record  # type: ignore[method-assign]
    return captured


def _step(status: str) -> DrillStepResult:
    """A step whose status is a LITERAL wire string, not a module constant."""
    return DrillStepResult(name="synthetic", status=status, detail="", observations={})


def _named(report: Any, name: str) -> DrillStepResult:
    return next(s for s in report.steps if s.name == name)


# ----------------------------------------------------------------------
# Wire-format constants and _aggregate_status
# ----------------------------------------------------------------------


def test_drill_status_wire_values_are_literal_strings() -> None:
    """The three status strings are a wire format, so they are pinned literally.

    Every existing assertion compares a report against ``DrillStatus.X``, which
    moves with the constant. These are the values the CLI serialises to JSON.
    """
    assert DrillStatus.PASS == "pass"
    assert DrillStatus.FAIL == "fail"
    assert DrillStatus.SKIPPED == "skipped"


def test_aggregate_status_of_no_steps_is_skipped_not_pass() -> None:
    """An empty drill is SKIPPED. Nothing ran, so nothing passed."""
    assert _aggregate_status(()) == "skipped"


def test_aggregate_status_all_pass_is_pass() -> None:
    assert _aggregate_status((_step("pass"), _step("pass"))) == "pass"


def test_aggregate_status_any_fail_is_fail() -> None:
    assert _aggregate_status((_step("pass"), _step("fail"), _step("pass"))) == "fail"


def test_aggregate_status_pass_plus_skipped_is_skipped_not_pass() -> None:
    """A partly-skipped drill is NOT a pass.

    This is the case the existing suite never builds: no FAIL present, but not
    every step passing either. Both the ``all(...)`` guard and the trailing
    sentinel are only observable here.
    """
    assert _aggregate_status((_step("pass"), _step("skipped"))) == "skipped"


def test_aggregate_status_single_skipped_step_is_skipped() -> None:
    assert _aggregate_status((_step("skipped"),)) == "skipped"


# ----------------------------------------------------------------------
# Drain drill — defaults and per-request work time
# ----------------------------------------------------------------------


def test_drain_drill_defaults_are_eight_dry_run_requests_at_one_millisecond() -> None:
    """The documented defaults: 8 requests, dry-run, 60 s budget, 1 ms each.

    Every existing drain call passes ``in_flight_count`` explicitly, so all
    three defaults were free to move.
    """
    sleep, calls = _recording_sleep()
    report = run_drain_drill(sleep=sleep)

    assert report.dry_run is True
    register = _named(report, "register_in_flight")
    assert register.observations["in_flight_count"] == 8
    assert register.observations["timeout_s"] == 60.0
    drain = _named(report, "drain_within_deadline")
    assert drain.observations["drained_count"] == 8
    assert drain.observations["residual_count"] == 0
    assert calls == [0.001] * 8


def test_drain_drill_real_mode_work_time_is_a_quarter_of_the_per_request_budget() -> None:
    """Real mode spends min(50 ms, timeout / fleet / 4) per request.

    With a 0.4 s budget over 4 requests that is 0.4/4/4 = 0.025 s, and the
    ``min`` is NOT saturated -- which is the point. Saturating it would hide
    both the ``min``/``max`` choice and the divisor.
    """
    sleep, calls = _recording_sleep()
    report = run_drain_drill(
        in_flight_count=4, timeout_s=0.4, dry_run=False, sleep=sleep
    )

    assert report.dry_run is False
    assert calls == [0.025] * 4
    assert _named(report, "drain_within_deadline").observations["drained_count"] == 4


# ----------------------------------------------------------------------
# Drain drill — the deadline itself
# ----------------------------------------------------------------------


def test_drain_cut_is_on_the_deadline_not_past_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain cuts when the budget is SPENT, and reads a high-resolution clock.

    Ten requests, a 3.0 s budget, and a clock that advances exactly 1.0 s per
    request: the fourth check finds elapsed == 3.0 s, which is the budget, so
    exactly three requests drain. ``>`` instead of ``>=`` buys a fourth
    request; a coarse clock that never ticks buys all ten. Both are decided
    here on exact floats rather than on how fast the host happens to be.
    """
    clock = _FakeClock(tick=1.0)
    monkeypatch.setattr(drill_mod, "time", clock)

    report = run_drain_drill(
        in_flight_count=10, timeout_s=3.0, dry_run=True, sleep=clock.sleep
    )

    drain = _named(report, "drain_within_deadline")
    assert drain.observations["drained_count"] == 3
    assert drain.observations["residual_count"] == 7
    assert drain.observations["elapsed_ms"] == 3000.0
    assert drain.observations["timeout_s"] == 3.0
    assert drain.status == "fail"
    assert drain.detail == "force-cut after 3.0s with 7 residual"
    assert report.overall == "fail"
    # The admission step reports the budget it was GIVEN, not the default.
    assert _named(report, "register_in_flight").observations["timeout_s"] == 3.0


def test_drain_elapsed_ms_keeps_microsecond_precision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``elapsed_ms`` is rounded to 3 decimals, i.e. to the microsecond.

    A single request against a clock tick of 1/512 s -- exactly representable,
    so the expectation is a decided number -- must be reported as 1.953 ms.
    Rounding to whole milliseconds instead would report 2.0 and throw away the
    only resolution the drain report has.
    """
    clock = _FakeClock(tick=0.001953125)
    monkeypatch.setattr(drill_mod, "time", clock)

    report = run_drain_drill(
        in_flight_count=1, timeout_s=10.0, dry_run=True, sleep=clock.sleep
    )

    drain = _named(report, "drain_within_deadline")
    assert drain.observations["elapsed_ms"] == 1.953
    assert drain.observations["drained_count"] == 1
    assert drain.observations["residual_count"] == 0
    assert drain.status == "pass"
    assert drain.detail == "drained 1/1 in 2.0ms"


# ----------------------------------------------------------------------
# Re-emit drill — defaults, payload, and the failure paths
# ----------------------------------------------------------------------


def test_reemit_sandbox_defaults_to_five_events() -> None:
    report = run_reemit_drill()

    assert report.dry_run is True
    generate = _named(report, "generate_events")
    assert generate.observations["reemit_count"] == 5
    assert _named(report, "emit_first_pass").observations["recorded"] == 5
    assert _named(report, "reemit_idempotent").observations["unique_event_ids"] == 5


def test_reemit_real_path_reports_the_dry_run_flag_it_was_given(
    audit_log: AuditLog,
) -> None:
    """The real-path report must mirror ``dry_run``, not hardcode it.

    The sandbox branch legitimately hardcodes True; the real branch does not,
    and the existing real-path test never looks at the flag.
    """
    report = run_reemit_drill(audit_log=audit_log, reemit_count=2, dry_run=False)

    assert report.dry_run is False
    assert report.overall == "pass"


def test_reemit_writes_a_miss_then_a_hit_with_a_stamped_latency(
    audit_log: AuditLog,
) -> None:
    """Un-instrumented re-emit stamps 10.0 ms, a 200, a miss, then a hit.

    The existing test counts rows and never reads one. These are the exact
    field values the audit store receives, as literals.
    """
    captured = _spy_record(audit_log)

    report = run_reemit_drill(audit_log=audit_log, reemit_count=3, dry_run=False)

    assert report.overall == "pass"
    assert len(captured) == 6
    first_pass, replay = captured[:3], captured[3:]
    for rec in first_pass:
        assert rec.latency_ms == 10.0
        assert rec.response_status == 200
        assert rec.idempotency_hit is False
    for rec in replay:
        assert rec.latency_ms == 10.0
        assert rec.response_status == 200
        assert rec.idempotency_hit is True
    # The replay must land on the SAME event_ids, or the UPSERT proves nothing.
    assert [r.event_id for r in first_pass] == [r.event_id for r in replay]


def test_reemit_first_pass_write_failure_fails_the_step(audit_log: AuditLog) -> None:
    """One refused write on the first pass must fail the step and report 0.

    ``AuditLog.record`` returns False rather than raising, so this is the only
    way the drill can learn a write was lost -- and no existing test ever makes
    it happen.
    """
    _spy_record(audit_log, fail_at=frozenset({1}))

    report = run_reemit_drill(audit_log=audit_log, reemit_count=3, dry_run=False)

    emit = _named(report, "emit_first_pass")
    assert emit.status == "fail"
    assert emit.observations["recorded"] == 0
    assert report.overall == "fail"


def test_reemit_replay_write_failure_fails_the_idempotency_step(
    audit_log: AuditLog,
) -> None:
    """A refused write on the replay pass must fail, even with a clean row count.

    The row count is still exactly right here -- the first pass wrote every
    row -- so the verdict rests entirely on the second-pass boolean.
    """
    _spy_record(audit_log, fail_at=frozenset({4}))

    report = run_reemit_drill(audit_log=audit_log, reemit_count=3, dry_run=False)

    idem = _named(report, "reemit_idempotent")
    assert idem.status == "fail"
    assert idem.observations["audit_rows"] == 3
    assert report.overall == "fail"


def test_reemit_measures_only_when_both_store_and_stage_are_supplied(
    audit_log: AuditLog, outcome_store: OutcomeStore
) -> None:
    """Half a producer configuration is not a producer.

    A store without a stage has nowhere to file the outcome, and a stage
    without a store has nothing to file it in. Either alone must leave the
    un-instrumented path in place -- which is observable because that path
    stamps the constant 10.0 rather than a measured duration.
    """
    captured = _spy_record(audit_log)
    report = run_reemit_drill(
        audit_log=audit_log,
        reemit_count=2,
        dry_run=False,
        outcome_store=outcome_store,
        stage=None,
    )
    assert report.overall == "pass"
    assert [r.latency_ms for r in captured] == [10.0] * 4

    captured_2 = _spy_record(audit_log)
    report_2 = run_reemit_drill(
        audit_log=audit_log,
        reemit_count=2,
        dry_run=False,
        outcome_store=None,
        stage="m4_1pct",
    )
    assert report_2.overall == "pass"
    assert [r.latency_ms for r in captured_2] == [10.0] * 4


def test_reemit_instrumented_replay_is_flagged_as_an_idempotency_hit(
    audit_log: AuditLog, outcome_store: OutcomeStore
) -> None:
    """With a stage configured, latency is MEASURED and the replay is a hit."""
    captured = _spy_record(audit_log)

    report = run_reemit_drill(
        audit_log=audit_log,
        reemit_count=3,
        dry_run=False,
        outcome_store=outcome_store,
        stage="m4_1pct",
    )

    assert report.overall == "pass"
    assert len(captured) == 6
    first_pass, replay = captured[:3], captured[3:]
    for rec in first_pass:
        assert rec.idempotency_hit is False
        assert rec.latency_ms != 10.0  # measured, not stamped
    for rec in replay:
        assert rec.idempotency_hit is True
    assert len(list(outcome_store.iter_stage("m4_1pct"))) == 3


def test_reemit_instrumented_first_pass_write_failure_fails_the_step(
    audit_log: AuditLog, outcome_store: OutcomeStore
) -> None:
    """The measured path must honour ``span.recorded`` too, for every event."""
    _spy_record(audit_log, fail_at=frozenset({2}))

    report = run_reemit_drill(
        audit_log=audit_log,
        reemit_count=3,
        dry_run=False,
        outcome_store=outcome_store,
        stage="m4_1pct",
    )

    emit = _named(report, "emit_first_pass")
    assert emit.status == "fail"
    assert emit.observations["recorded"] == 0


def test_reemit_instrumented_replay_write_failure_fails_the_step(
    audit_log: AuditLog, outcome_store: OutcomeStore
) -> None:
    _spy_record(audit_log, fail_at=frozenset({5}))

    report = run_reemit_drill(
        audit_log=audit_log,
        reemit_count=3,
        dry_run=False,
        outcome_store=outcome_store,
        stage="m4_1pct",
    )

    assert _named(report, "reemit_idempotent").status == "fail"
    assert report.overall == "fail"


# ----------------------------------------------------------------------
# Idempotency drill — the invariant, and proving the race happened
# ----------------------------------------------------------------------


class _AlwaysInvokesHandler:
    """Stand-in handler with NO idempotency: every caller runs the worker.

    The drill's whole purpose is to catch this, and no existing test ever lets
    it happen -- so the FAIL branch of the verdict is never taken.
    """

    def __init__(self, *, audit_log: object) -> None:
        self._audit_log = audit_log

    def handle(
        self, *, event_id: str, request: object, worker: Callable[[object], object]
    ) -> object:
        return worker(request)


def _barrier_handler_factory(
    parties: int, timeout: float, log: list[str]
) -> Callable[..., object]:
    """Handler whose ``handle`` only completes if the racers really overlap.

    A barrier of N parties trips only when all N threads are inside it at the
    same time. If the drill starts and joins its threads one at a time, the
    first thread waits alone until the barrier times out and breaks, and every
    later thread fails against the broken barrier immediately.
    """
    barrier = threading.Barrier(parties, timeout=timeout)

    class _BarrierHandler:
        def __init__(self, *, audit_log: object) -> None:
            self._audit_log = audit_log

        def handle(
            self, *, event_id: str, request: object, worker: Callable[[object], object]
        ) -> object:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                log.append("serialised")
            else:
                log.append("concurrent")
            return worker(request)

    return _BarrierHandler


def test_idempotency_drill_defaults_to_eight_racers(audit_log: AuditLog) -> None:
    """Default concurrency is 8. Every existing call spells it out."""
    report = run_idempotency_drill(audit_log=audit_log)

    spawn = _named(report, "spawn_threads")
    assert spawn.observations["concurrency"] == 8
    assert _named(report, "single_worker_invocation").observations["concurrency"] == 8
    assert report.overall == "pass"


def test_idempotency_drill_worker_dwells_inside_the_critical_section(
    monkeypatch: pytest.MonkeyPatch, audit_log: AuditLog
) -> None:
    """The synthetic worker holds the lock for 50 ms on purpose.

    Without the dwell the racers do not overlap and the drill stops being a
    race at all -- it would report the same PASS whether or not the handler
    serialises anything.
    """
    slept: list[float] = []

    class _RecordingTime:
        def sleep(self, seconds: float) -> None:
            slept.append(seconds)
            _real_time.sleep(seconds)

        def perf_counter(self) -> float:
            return _real_time.perf_counter()

        def monotonic(self) -> float:
            return _real_time.monotonic()

    monkeypatch.setattr(drill_mod, "time", _RecordingTime())

    report = run_idempotency_drill(audit_log=audit_log, concurrency=4)

    assert report.overall == "pass"
    assert slept == [0.05]


def test_idempotency_drill_fails_when_the_worker_runs_more_than_once(
    monkeypatch: pytest.MonkeyPatch, audit_log: AuditLog
) -> None:
    """A handler with no idempotency must produce a FAIL verdict.

    This is the only test that takes the failing branch of
    ``single_worker_invocation``: with the real handler the invariant always
    holds, so a hardcoded PASS or a relaxed ``>= 1`` is otherwise invisible.
    """
    monkeypatch.setattr(drill_mod, "IdempotencyHandler", _AlwaysInvokesHandler)

    report = run_idempotency_drill(audit_log=audit_log, concurrency=3)

    step = _named(report, "single_worker_invocation")
    assert step.observations["invocations"] == 3
    assert step.status == "fail"
    assert report.overall == "fail"


def test_idempotency_drill_racers_actually_run_concurrently(
    monkeypatch: pytest.MonkeyPatch, audit_log: AuditLog
) -> None:
    """All racers must be in flight at once, not started and joined in turn.

    Every thread has to reach the barrier before any of them may leave it, so
    a drill that joins each thread before starting the next cannot get past it.
    """
    log: list[str] = []
    monkeypatch.setattr(
        drill_mod,
        "IdempotencyHandler",
        _barrier_handler_factory(parties=3, timeout=1.0, log=log),
    )

    run_idempotency_drill(audit_log=audit_log, concurrency=3)

    assert log == ["concurrent"] * 3


def test_idempotency_drill_does_not_close_a_caller_owned_audit_log(
    monkeypatch: pytest.MonkeyPatch, audit_log: AuditLog
) -> None:
    """The drill closes only the AuditLog it created itself.

    Closing the caller's store would drop connections out from under whatever
    else is using it -- and nothing else in the drill would notice.
    """
    closed: list[int] = []
    monkeypatch.setattr(audit_log, "close", lambda: closed.append(1))

    report = run_idempotency_drill(audit_log=audit_log, concurrency=2)

    assert report.overall == "pass"
    assert closed == []


# ----------------------------------------------------------------------
# Full drill orchestrator — defaults and argument forwarding
# ----------------------------------------------------------------------


def test_run_full_drill_defaults_are_eight_five_eight(audit_log: AuditLog) -> None:
    drain, reemit, idem = run_full_drill(audit_log=audit_log)

    assert _named(drain, "register_in_flight").observations["in_flight_count"] == 8
    assert _named(reemit, "generate_events").observations["reemit_count"] == 5
    assert _named(idem, "spawn_threads").observations["concurrency"] == 8
    assert all(r.overall == "pass" for r in (drain, reemit, idem))


def test_run_full_drill_forwards_the_timeout_to_the_drain(audit_log: AuditLog) -> None:
    """A zero budget given to the orchestrator must reach the drain loop.

    If it does not, the drain silently runs on the 60 s default and reports a
    pass for a drill that was asked to cut immediately.
    """
    drain, _reemit, _idem = run_full_drill(
        audit_log=audit_log, in_flight_count=4, reemit_count=1, concurrency=2,
        timeout_s=0.0,
    )

    step = _named(drain, "drain_within_deadline")
    assert step.observations["timeout_s"] == 0.0
    assert step.observations["drained_count"] == 0
    assert step.observations["residual_count"] == 4
    assert drain.overall == "fail"


def test_run_full_drill_forwards_dry_run_to_the_drain(audit_log: AuditLog) -> None:
    drain, reemit, _idem = run_full_drill(
        audit_log=audit_log, in_flight_count=2, reemit_count=1, concurrency=2,
        dry_run=False,
    )

    assert drain.dry_run is False
    assert reemit.dry_run is False


def test_run_full_drill_forwards_the_producer_configuration(
    audit_log: AuditLog, outcome_store: OutcomeStore
) -> None:
    """Both halves of the producer config must reach the re-emit drill.

    Dropping either one silently downgrades ``parallax canary
    --orbit-reemit-test --stage ...`` from a real T1-T5 producer back to a
    drill with a side effect, and every step still reports PASS.
    """
    _drain, reemit, _idem = run_full_drill(
        audit_log=audit_log, in_flight_count=2, reemit_count=2, concurrency=2,
        outcome_store=outcome_store, stage="m4_1pct",
    )

    assert reemit.overall == "pass"
    assert len(list(outcome_store.iter_stage("m4_1pct"))) == 2


def test_run_full_drill_forwards_concurrency_to_the_idempotency_drill(
    audit_log: AuditLog,
) -> None:
    _drain, _reemit, idem = run_full_drill(
        audit_log=audit_log, in_flight_count=2, reemit_count=1, concurrency=3,
    )

    assert _named(idem, "spawn_threads").observations["concurrency"] == 3
    assert _named(idem, "single_worker_invocation").observations["concurrency"] == 3


def test_run_full_drill_forwards_the_audit_log_to_the_idempotency_drill(
    monkeypatch: pytest.MonkeyPatch, audit_log: AuditLog
) -> None:
    """The idempotency drill must reuse the caller's store, not open its own.

    Falling back to the private temp-file store makes the drill stop exercising
    the database the rest of the run is using, and leaves the file behind.
    """
    built: list[object] = []

    class _CountingAuditLog(AuditLog):
        def __init__(self, *a: Any, **kw: Any) -> None:
            built.append(1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(drill_mod, "AuditLog", _CountingAuditLog)

    _drain, _reemit, idem = run_full_drill(
        audit_log=audit_log, in_flight_count=2, reemit_count=1, concurrency=2,
    )

    assert idem.overall == "pass"
    assert built == []
