"""#106.3 — the drain timeout needs a signal that does not depend on a scrape.

``DrainTimeoutDetected`` (severity: critical) reads
``increase(parallax_drain_timeout_total[1h])``. #102 put that counter on the
wire, and #105's follow-up measurement showed the alert still cannot fire:
uvicorn closes the listening socket *before* running lifespan shutdown, so the
post-increment value exists only inside a process Prometheus can no longer
reach. The counter is not wrong, it is unscrapeable — the last increment dies
with the process.

The remaining producer-side channel that outlives the process is the log
stream. It already carried the event, but only as prose: an alert would have had
to regex ``"drain timeout after %.1fs"``, which silently stops matching the day
someone rewords the sentence. This pins a stable structured key instead, so a
log-based alert matches on ``event="drain_timeout"`` and the human sentence
stays free to change.

Deliberately NOT changed here: the severity (still WARNING, as the annotation
and the existing lifespan tests expect) and the counter (still incremented — it
remains correct in-process and for anything that scrapes mid-drain). Which sink
consumes the event, and whether ``_drain_inflight`` should exist at all given
uvicorn drains in-flight requests itself, are operator/architecture calls left
open in the PR description.

RED on the unfixed tree: no record carries the structured key.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
import yaml

from parallax.router.inflight import get_inflight_count, inflight_gauge
from parallax.server.lifespan import DRAIN_TIMEOUT_EVENT, _drain_inflight, drain_timeout_total

_LIFESPAN_LOGGER = "parallax.server.lifespan"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RULES = _REPO_ROOT / "prometheus" / "rules" / "parallax-dual-read.rules.yml"


@pytest.fixture(autouse=True)
def _restore_inflight_gauge():
    """The gauge is a process-global singleton — hand it back as found."""
    before = inflight_gauge._value.get()  # noqa: SLF001 — same read get_inflight_count uses
    try:
        yield
    finally:
        inflight_gauge.set(before)


def _timeout_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _LIFESPAN_LOGGER and getattr(r, "event", None) == DRAIN_TIMEOUT_EVENT
    ]


def test_drain_timeout_emits_a_stable_machine_keyable_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The timeout must be findable by key, not by sentence."""
    inflight_gauge.set(0)
    inflight_gauge.inc()

    with caplog.at_level(logging.WARNING, logger=_LIFESPAN_LOGGER):
        asyncio.run(_drain_inflight(timeout_seconds=0.15, poll_interval_seconds=0.05))

    records = _timeout_records(caplog)
    assert records, (
        "no record carries the structured drain-timeout key — a log-based alert "
        f"would have to regex the prose. Saw: {[r.getMessage() for r in caplog.records]}"
    )


def test_the_event_carries_the_numbers_an_operator_needs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """How many requests were cut, and what deadline cut them.

    Both are structured fields rather than only interpolations in the sentence,
    so a log pipeline can route and threshold on them. Bounded numerics, so they
    need no sanitisation.
    """
    inflight_gauge.set(0)
    inflight_gauge.inc()
    inflight_gauge.inc()

    with caplog.at_level(logging.WARNING, logger=_LIFESPAN_LOGGER):
        asyncio.run(_drain_inflight(timeout_seconds=0.15, poll_interval_seconds=0.05))

    record = _timeout_records(caplog)[0]
    assert getattr(record, "inflight_count", None) == 2
    assert getattr(record, "timeout_seconds", None) == pytest.approx(0.15)


def test_the_event_stays_a_warning_and_still_increments_the_counter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither existing behaviour may be traded away for the new key.

    The counter still measures the event for anything watching in-process, and
    the level is what ``test_lifespan_logs_warning_on_timeout`` and the alert
    annotation both assume.
    """
    before = drain_timeout_total._value.get()  # noqa: SLF001
    inflight_gauge.set(0)
    inflight_gauge.inc()

    with caplog.at_level(logging.WARNING, logger=_LIFESPAN_LOGGER):
        asyncio.run(_drain_inflight(timeout_seconds=0.15, poll_interval_seconds=0.05))

    assert drain_timeout_total._value.get() == pytest.approx(before + 1.0)  # noqa: SLF001
    assert _timeout_records(caplog)[0].levelno == logging.WARNING


def test_the_rendered_line_carries_the_key_and_numbers_not_just_the_extra(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key has to survive ``record.getMessage()`` alone, not only ``extra``.

    Codex review on #107: ``parallax serve`` (parallax.cli._cmd_serve) hands
    uvicorn no custom ``log_config``, so under the canonical launcher this
    logger has no JSON formatter attached — a plain ``StreamHandler`` there
    would call the stdlib default formatter, which renders only
    ``record.getMessage()`` and never touches ``extra``. Every other test in
    this file reads the LogRecord's attributes directly (``getattr(record,
    "inflight_count", ...)``), so a regression that moved the key back into
    ``extra``-only would pass all of them while the alert's match target
    silently stopped reaching stderr in that environment. This formats
    through the same stdlib-default shape a bare handler would use.
    """
    inflight_gauge.set(0)
    inflight_gauge.inc()
    inflight_gauge.inc()

    with caplog.at_level(logging.WARNING, logger=_LIFESPAN_LOGGER):
        asyncio.run(_drain_inflight(timeout_seconds=2.5, poll_interval_seconds=0.05))

    record = _timeout_records(caplog)[0]
    rendered = logging.Formatter("%(levelname)s: %(message)s").format(record)

    assert f"event={DRAIN_TIMEOUT_EVENT}" in rendered, (
        f"rendered line is missing the structured key. Got: {rendered!r}"
    )
    assert "inflight_count=2" in rendered, (
        f"rendered line is missing the inflight count. Got: {rendered!r}"
    )
    assert "timeout_seconds=2.5" in rendered, (
        f"rendered line is missing the timeout. Got: {rendered!r}"
    )


def test_a_clean_drain_emits_no_timeout_event(caplog: pytest.LogCaptureFixture) -> None:
    """Negative control — an always-on key would make the alert fire forever."""
    inflight_gauge.set(0)
    assert get_inflight_count() == 0

    with caplog.at_level(logging.DEBUG, logger=_LIFESPAN_LOGGER):
        asyncio.run(_drain_inflight(timeout_seconds=1.0, poll_interval_seconds=0.05))

    assert _timeout_records(caplog) == []
    # Render-path half of the same control: the key=value tail lives only on
    # the timeout WARNING, so the clean-drain INFO line must not carry it
    # either once rendered through a plain formatter — not just absent from
    # the LogRecord's ``extra``.
    formatter = logging.Formatter("%(levelname)s: %(message)s")
    rendered_lines = [formatter.format(r) for r in caplog.records if r.name == _LIFESPAN_LOGGER]
    assert not any(f"event={DRAIN_TIMEOUT_EVENT}" in line for line in rendered_lines), (
        f"clean drain must not render the timeout event key. Saw: {rendered_lines}"
    )


def test_the_alert_rule_documents_the_key_it_must_be_replaced_by() -> None:
    """Keep the rule and the producer in step.

    The rule's own annotation is what an oncall reads at 3am to learn that the
    alert's silence proves nothing. It has to name the key that actually exists;
    pointing at a prose fragment that has since been restructured is how the
    #102 annotation would rot.
    """
    groups = yaml.safe_load(_RULES.read_text(encoding="utf-8"))["groups"]
    rules = [
        rule
        for group in groups
        for rule in group.get("rules", [])
        if rule.get("alert") == "DrainTimeoutDetected"
    ]

    assert len(rules) == 1, "expected exactly one DrainTimeoutDetected rule"
    description = rules[0]["annotations"]["description"]
    assert DRAIN_TIMEOUT_EVENT in description, (
        "the DrainTimeoutDetected annotation must name the structured event key "
        f"{DRAIN_TIMEOUT_EVENT!r} that replaces the prose grep. Got:\n{description}"
    )
