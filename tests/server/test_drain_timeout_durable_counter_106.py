"""#106.3 completion — the drain timeout has to reach Prometheus, not just a log.

#107 gave the event a stable log key and stopped there: the metric half stayed
latent, and ``prometheus/tests/parallax-dual-read.test.yml`` case 20 pinned that
gap in CI with the note "it should start FAILING the day a durable producer is
added". This is that producer.

The mechanism under test is a restart, not a scrape of the dying process. The
timeout is journalled at shutdown; the *next* process restores the accumulated
total into ``parallax_drain_timeout_total`` at startup, so the 0 -> 1 step
happens on a live target that Prometheus is scraping normally.

Three properties, and the middle one is the one a naive implementation gets
wrong: restoring must be idempotent, or every redeploy manufactures a step and
the CRITICAL alert becomes a deploy notification.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from parallax.router.inflight import inflight_gauge
from parallax.server.drain_journal import (
    DRAIN_JOURNAL_ENV,
    read_drain_journal,
    record_drain_timeout,
    resolve_drain_journal_path,
)
from parallax.server.lifespan import (
    _drain_inflight,
    drain_timeout_total,
    restore_drain_timeout_counter,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RULES = _REPO_ROOT / "prometheus" / "rules" / "parallax-dual-read.rules.yml"


def _counter_value() -> float:
    return drain_timeout_total._value.get()  # noqa: SLF001 — no public read accessor


@pytest.fixture(autouse=True)
def _restore_process_globals():
    """The gauge and the counter are process-global singletons — hand them back.

    The counter matters as much as the gauge here: these tests deliberately
    rewind it to model a cold process, and leaking that rewind would corrupt
    every later test in the session that reads ``parallax_drain_timeout_total``.
    """
    gauge_before = inflight_gauge._value.get()  # noqa: SLF001
    counter_before = drain_timeout_total._value.get()  # noqa: SLF001
    try:
        yield
    finally:
        inflight_gauge.set(gauge_before)
        drain_timeout_total._value.set(counter_before)  # noqa: SLF001


@pytest.fixture
def journal(tmp_path: Path) -> Path:
    return tmp_path / "drain.json"


def _time_out_once(journal_path: Path) -> None:
    """Drive one real drain timeout against ``journal_path``."""
    inflight_gauge.set(0)
    inflight_gauge.inc()
    asyncio.run(
        _drain_inflight(
            timeout_seconds=0.15,
            poll_interval_seconds=0.05,
            journal_path=str(journal_path),
        )
    )


def _simulate_process_restart() -> None:
    """Put the counter back to a cold-process value.

    The restore exists to run in a process that has *not* seen the timeout — a
    fresh exporter starts every counter at 0 and the journal is the only record
    of what happened before it. ``drain_timeout_total`` is a module-level
    singleton shared by the whole pytest session, so a test that skipped this
    would measure the restore against a counter that had already been
    incremented in-process by ``_time_out_once`` and see the delta collapse to
    zero for the wrong reason.
    """
    drain_timeout_total._value.set(0.0)  # noqa: SLF001 — no public reset on Counter


# ---------------------------------------------------------------------------
# The journal itself
# ---------------------------------------------------------------------------


def test_a_drain_timeout_is_written_to_the_journal(journal: Path) -> None:
    """The event has to exist on disk once the process that saw it is gone."""
    _time_out_once(journal)

    assert journal.exists(), "no journal was written — the event died with the process"
    state = read_drain_journal(journal)
    assert state.total == 1.0
    assert state.last_inflight_count == 1
    assert state.last_timeout_seconds == pytest.approx(0.15)


def test_a_clean_drain_writes_nothing(journal: Path) -> None:
    """Negative control: an always-written journal would restore a phantom step."""
    inflight_gauge.set(0)
    asyncio.run(
        _drain_inflight(
            timeout_seconds=1.0, poll_interval_seconds=0.05, journal_path=str(journal)
        )
    )

    assert not journal.exists()
    assert read_drain_journal(journal).total == 0.0


def test_the_journal_accumulates_rather_than_flagging(journal: Path) -> None:
    """A second timeout must be distinguishable from the first one persisting."""
    _time_out_once(journal)
    _time_out_once(journal)
    _time_out_once(journal)

    assert read_drain_journal(journal).total == 3.0


def test_a_corrupt_journal_is_reported_not_swallowed(journal: Path) -> None:
    """Fail-open, but say so: a lost signal must not read as 'never timed out'."""
    journal.write_text("{not json", encoding="utf-8")

    state = read_drain_journal(journal)
    assert state.total == 0.0
    assert state.readable is False


def test_a_corrupt_journal_does_not_stop_a_later_event_being_recorded(journal: Path) -> None:
    """Recovery: the next timeout re-establishes a usable journal."""
    journal.write_text("{not json", encoding="utf-8")

    _time_out_once(journal)

    assert read_drain_journal(journal).total == 1.0
    assert json.loads(journal.read_text(encoding="utf-8"))["drain_timeout_total"] == 1.0


def test_a_negative_total_is_not_trusted(journal: Path) -> None:
    """A counter cannot go backwards; a journal claiming it did is corrupt input."""
    journal.write_text(json.dumps({"drain_timeout_total": -5}), encoding="utf-8")

    assert read_drain_journal(journal).total == 0.0


def test_the_journal_path_follows_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators point this at durable storage; the default is cwd-relative."""
    target = tmp_path / "elsewhere" / "drain.json"
    monkeypatch.setenv(DRAIN_JOURNAL_ENV, str(target))

    assert resolve_drain_journal_path() == target

    record_drain_timeout(inflight_count=4, timeout_seconds=900.0)
    assert target.exists()
    assert read_drain_journal().last_inflight_count == 4


# ---------------------------------------------------------------------------
# The restore — what a scrape actually sees
# ---------------------------------------------------------------------------


def test_a_restart_after_a_timeout_makes_the_counter_step(journal: Path) -> None:
    """THE POINT OF #106.3. A scrape of the next process sees the increase.

    This is the assertion promtool case 20 was waiting for: the value a
    Prometheus scrape can reach goes up because of an event that happened in a
    process it could never scrape.
    """
    _time_out_once(journal)
    _simulate_process_restart()

    added = restore_drain_timeout_counter(str(journal))

    assert added == 1.0
    assert _counter_value() == pytest.approx(1.0)


def test_replaying_the_same_journal_adds_nothing(journal: Path) -> None:
    """REPLAY IDEMPOTENCE — a redeploy is not a drain timeout.

    ``inc(total)`` instead of ``inc(total - current)`` passes the test above and
    fails here, turning every restart into a CRITICAL page.
    """
    _time_out_once(journal)
    _simulate_process_restart()
    restore_drain_timeout_counter(str(journal))

    # Second boot of the replacement process. Nothing new was journalled.
    added = restore_drain_timeout_counter(str(journal))

    assert added == 0.0
    assert _counter_value() == pytest.approx(1.0)


def test_multiple_events_accumulate_across_restarts(journal: Path) -> None:
    """Monotonic accumulation end to end: timeout, restart, timeout, restart."""
    _time_out_once(journal)
    _simulate_process_restart()
    assert restore_drain_timeout_counter(str(journal)) == 1.0
    assert _counter_value() == pytest.approx(1.0)

    # The replacement process times out as well. Its own inc() already moved
    # the live counter to 2, so the journal (2) and the counter agree and the
    # NEXT restore has nothing to add — the accumulation is in the journal.
    _time_out_once(journal)
    assert read_drain_journal(journal).total == 2.0
    assert _counter_value() == pytest.approx(2.0)

    _simulate_process_restart()
    assert restore_drain_timeout_counter(str(journal)) == 2.0
    assert _counter_value() == pytest.approx(2.0)


def test_restoring_an_empty_journal_is_a_no_op(journal: Path) -> None:
    """A first boot must not fabricate history."""
    _simulate_process_restart()

    assert restore_drain_timeout_counter(str(journal)) == 0.0
    assert _counter_value() == pytest.approx(0.0)


def test_a_counter_ahead_of_the_journal_is_left_alone(journal: Path) -> None:
    """Never move a counter backwards, and never re-add what is already counted.

    Reachable when the journal write failed after the in-process increment
    landed: the live value is the more complete number, and a restore that
    "corrected" it downwards would look like a counter reset to Prometheus.
    """
    _time_out_once(journal)  # journal == 1, counter == 1 in this process

    assert restore_drain_timeout_counter(str(journal)) == 0.0
    assert _counter_value() == pytest.approx(1.0)


def test_the_restored_counter_is_visible_on_the_metrics_endpoint(journal: Path) -> None:
    """End to end: the restored value reaches the scrape text, not just the object."""
    from fastapi.testclient import TestClient

    from parallax.server.app import create_app

    _time_out_once(journal)
    _simulate_process_restart()
    restore_drain_timeout_counter(str(journal))

    body = TestClient(create_app()).get("/metrics").text
    lines = [ln for ln in body.splitlines() if ln.startswith("parallax_drain_timeout_total")]

    assert lines, f"parallax_drain_timeout_total is absent from the scrape:\n{body[:2000]}"
    assert float(lines[0].split()[-1]) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# The rule has to describe the mechanism that now exists
# ---------------------------------------------------------------------------


def test_the_alert_no_longer_declares_itself_latent() -> None:
    """A rule that says it cannot fire, while it can, is worse than no annotation.

    The #102/#106 annotation told oncall that silence proves nothing. That was
    true then and is false now — leaving it would train the reader to ignore a
    CRITICAL alert that works.
    """
    groups = yaml.safe_load(_RULES.read_text(encoding="utf-8"))["groups"]
    rules = [
        rule
        for group in groups
        for rule in group.get("rules", [])
        if rule.get("alert") == "DrainTimeoutDetected"
    ]
    assert len(rules) == 1
    description = rules[0]["annotations"]["description"]

    assert "KNOWN LATENT" not in description, (
        "DrainTimeoutDetected can fire now — the latency caveat has to go:\n" + description
    )
    assert "journal" in description.lower(), (
        "the annotation must describe the durable path the alert now depends on, so an "
        f"oncall knows a missing journal breaks it:\n{description}"
    )


def test_the_promtool_fixture_expects_the_annotation_the_rule_actually_has() -> None:
    """promtool compares ``exp_annotations`` literally, and CI is the wrong place to find out.

    The rule's description is duplicated into every firing case in
    ``prometheus/tests/parallax-dual-read.test.yml``. Edit one and not the other
    and the suite fails in CI with a wall of diffed prose, minutes after the push
    — so the two copies are pinned here, where the edit happens.
    """
    tests_path = _REPO_ROOT / "prometheus" / "tests" / "parallax-dual-read.test.yml"

    groups = yaml.safe_load(_RULES.read_text(encoding="utf-8"))["groups"]
    expected = next(
        rule["annotations"]
        for group in groups
        for rule in group.get("rules", [])
        if rule.get("alert") == "DrainTimeoutDetected"
    )

    cases = yaml.safe_load(tests_path.read_text(encoding="utf-8"))["tests"]
    fixtures = [
        alert["exp_annotations"]
        for case in cases
        for rule_test in case.get("alert_rule_test", []) or []
        if rule_test.get("alertname") == "DrainTimeoutDetected"
        for alert in rule_test.get("exp_alerts", []) or []
    ]

    assert fixtures, "no firing DrainTimeoutDetected case left — the alert is untested"
    for fixture in fixtures:
        assert fixture == expected, (
            "a promtool case expects annotations the rule no longer has; promtool "
            "compares them literally and will fail in CI"
        )
