"""Mutation-hardening for ``parallax.canary.cli`` (land-20260824 w5 S4).

There is no ``test_canary_cli.py`` to be a companion to -- that is the finding.
Every semantic mutant below SURVIVED the pre-existing suite that reaches this
module (``tests/dod/test_dod.py``, ``tests/drill/test_drill.py``,
``tests/observability/test_canary_exporter_106.py``), all three of which import
the CLI incidentally, to drive the layer underneath it. 67 mutants were applied
one at a time to an otherwise pristine tree; 8 died and 59 walked through.

Tally for this module: applied 67 / killed-by-new 59 / already-covered 8 /
unaddressed 0.

This is the argument-plumbing layer between an operator's command line and the
canary machinery, and almost none of it was pinned:

  * **Every ``--flag`` could stop being forwarded.** ``--in-flight``,
    ``--reemit-count``, ``--concurrency``, ``--timeout``, ``--dry-run``,
    ``--days``, ``--audit-db`` and ``--stage`` can each be replaced by the
    hardcoded default at the call site, and every existing test still passes,
    because every existing test invokes the CLI with defaults and asserts on
    what the layer underneath did. Sixteen separate forwarding mutants
    survived. The tests here substitute the drill/DoD entry points with
    recorders and assert the exact keyword arguments the CLI passed, driving
    every flag to a NON-default value so a hardcoded default cannot coincide.

  * **The argparse contract itself is unasserted.** The mode group could stop
    being ``required``, ``--format`` could drop ``json`` from its choices or
    default to it, ``--days`` could parse as a float and ``--timeout`` as an
    int (making ``--timeout 0.5`` a usage error), and every default could move.

  * **Dispatch could be crossed.** ``--dod`` could run the drain drill,
    ``--rollback-drill`` could run only the re-emit drill, ``--drain-test``
    could run the full three-scenario drill; only one of the five wirings was
    covered. ``--check-alerting`` could be dropped from the dispatch table
    entirely and fall through to the "no mode selected" error.

  * **Exit codes are the entire contract with CI, and none were pinned.** A
    failing drill could exit 0, a failing DoD could exit 0, the rollback drill
    could require ALL THREE drills to fail before reporting failure, a missing
    ``--stage`` could exit 1 (a DoD failure) rather than 2 (a usage error), and
    the alerting probe could pass with only one of its two webhooks configured.

  * **``--prometheus-url`` could be overridden by the environment.** The
    documented precedence is flag > env > default; reversing the first two is
    invisible unless a test sets both.

  * **The stage gate on the drills could be dropped.** ``stage`` is only
    forwarded when an outcome store was actually opened; forwarding it
    unconditionally would ask the drill to be a canary producer with nowhere to
    write the outcome half of each event.

  * **The alerting probe reads four environment variables by name.** Every name
    could be changed, and the status strings inverted, with nothing failing.

  * **Both output formats could be swapped.** ``if fmt == "json"`` inverted, the
    early ``return`` after the JSON dump dropped (so both formats print), the
    pass/fail marks exchanged, and the observations line printed only when
    there are no observations -- all invisible.

Expected values are LITERALS throughout: 7, 60.0, 8, 5, "pretty",
"http://localhost:9090", "PAGERDUTY_M4_CANARY_KEY", "SLACK_WEBHOOK_M4_CANARY".
Reading the expectation back out of the module is what let every one of these
defaults move.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
from collections.abc import Callable
from typing import Any

import pytest

import parallax.canary.cli as canary_cli
from parallax.canary.cli import _open_outcome_store, _print_drill_reports, cmd_canary
from parallax.canary.dod import DodMetric, DodReport, DodVerdict, MetricResult
from parallax.canary.drill import DrillReport, DrillStepResult
from parallax.cli import build_parser

_MODE_FUNCS = (
    "_cmd_dod",
    "_cmd_rollback_drill",
    "_cmd_drain_test",
    "_cmd_reemit_test",
    "_cmd_check_alerting",
)


def _parse(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _dod_report(overall: DodVerdict = DodVerdict.PASS) -> DodReport:
    return DodReport(
        stage="m4_1pct",
        window_start="2026-01-01T00:00:00+00:00",
        window_end="2026-01-08T00:00:00+00:00",
        metrics=(
            MetricResult(
                metric=DodMetric.DISCREPANCY_RATE,
                observed=0.001,
                threshold=0.005,
                verdict=DodVerdict.PASS,
                sample_size=123,
            ),
        ),
        overall=overall,
    )


def _drill_report(
    drill: str = "drain", overall: str = "pass", dry_run: bool = True
) -> DrillReport:
    return DrillReport(
        drill=drill,
        dry_run=dry_run,
        steps=(
            DrillStepResult(
                name="only_step",
                status=overall,
                detail="synthetic",
                observations={"k": 1},
            ),
        ),
        overall=overall,
    )


def _recorder(
    monkeypatch: pytest.MonkeyPatch, name: str, result: Any
) -> dict[str, Any]:
    """Replace a canary-cli entry point with a keyword-argument recorder."""
    seen: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return result

    monkeypatch.setattr(canary_cli, name, _capture)
    return seen


class _NetworkBlocked(RuntimeError):
    """Raised when anything in this process tries to open a real connection."""


def _block_all_delivery(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Neutralise every outbound-delivery seam in this process, recording use.

    ``_cmd_check_alerting`` delivers nothing today: it imports argparse, json,
    os and sys and no HTTP client, and it only echoes ``live_probe`` into its
    payload. This guard exists for the day the live probe the module documents
    is implemented, because the test below is the one that arms it.

    ``socket.socket`` and ``socket.create_connection`` are the seam rather
    than any particular client, because every client a future implementation
    might reach for -- urllib, http.client, requests, httpx, urllib3 -- bottoms
    out in one of the two. An attempt is BOTH recorded and refused, so the
    guarantee does not rest on the assertion that reads the recording: delete
    that assertion and the connection is still impossible.
    """
    attempts: list[str] = []

    def _seam(name: str) -> Callable[..., Any]:
        def _blocked(*args: Any, **kwargs: Any) -> Any:
            attempts.append(name)
            raise _NetworkBlocked(
                f"{name}{args!r} was called while the canary alerting live "
                "probe was armed; a live probe must be delivered through an "
                "injectable sender, never a real connection from a unit test"
            )

        return _blocked

    monkeypatch.setattr(socket, "socket", _seam("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", _seam("socket.create_connection"))
    return attempts


# ----------------------------------------------------------------------
# argparse contract
# ----------------------------------------------------------------------


def test_canary_parser_defaults() -> None:
    """The defaults an operator gets when they name only a mode.

    All of these are documented in ``--help`` and none was asserted anywhere.
    """
    args = _parse(["canary", "--rollback-drill"])

    assert args.days == 7
    assert args.timeout == 60.0
    assert args.in_flight == 8
    assert args.reemit_count == 5
    assert args.concurrency == 8
    assert args.format == "pretty"
    assert args.audit_db is None
    assert args.stage is None
    assert args.dry_run is False


def test_canary_parser_argument_types() -> None:
    """``--days`` is whole days; ``--timeout`` is fractional seconds.

    The defaults cannot show this -- argparse only applies ``type`` to strings
    it parses off the command line, so both defaults keep their literal type
    whatever ``type=`` says. Only an explicitly supplied value exercises it,
    and ``--timeout 0.5`` becomes a usage error the moment the type is wrong.
    """
    args = _parse(["canary", "--drain-test", "--days", "3", "--timeout", "0.5"])

    assert args.days == 3
    assert isinstance(args.days, int)
    assert args.timeout == 0.5


def test_canary_parser_requires_a_mode() -> None:
    """``parallax canary`` with no mode is a usage error, not a no-op."""
    with pytest.raises(SystemExit):
        _parse(["canary"])


def test_canary_parser_accepts_json_format() -> None:
    """``--format json`` must stay selectable; it is the machine-readable path."""
    assert _parse(["canary", "--drain-test", "--format", "json"]).format == "json"


# ----------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("--dod", "_cmd_dod"),
        ("--rollback-drill", "_cmd_rollback_drill"),
        ("--drain-test", "_cmd_drain_test"),
        ("--orbit-reemit-test", "_cmd_reemit_test"),
        ("--check-alerting", "_cmd_check_alerting"),
    ],
)
def test_each_mode_flag_reaches_its_own_handler(
    monkeypatch: pytest.MonkeyPatch, flag: str, expected: str
) -> None:
    """Each of the five mode flags runs exactly one handler -- its own.

    Only one of these wirings was covered before, so the other four could be
    crossed: ``--drain-test`` running the whole three-scenario drill would
    quietly turn a preflight smoke check into a real re-emit against the audit
    store.
    """
    called: list[str] = []
    for name in _MODE_FUNCS:
        monkeypatch.setattr(
            canary_cli,
            name,
            (lambda n: lambda args: (called.append(n), 0)[1])(name),
        )

    argv = ["canary", flag]
    if flag == "--dod":
        argv += ["--stage", "m4_1pct"]

    assert cmd_canary(_parse(argv)) == 0
    assert called == [expected]


def test_cmd_canary_with_no_mode_selected_is_a_usage_error() -> None:
    """The defensive fallback exits 2 (usage), which is what CI reads."""
    namespace = argparse.Namespace(
        dod=False,
        rollback_drill=False,
        drain_test=False,
        orbit_reemit_test=False,
        check_alerting=False,
    )

    assert cmd_canary(namespace) == 2


# ----------------------------------------------------------------------
# --dod
# ----------------------------------------------------------------------


def test_dod_without_a_stage_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 2 (usage), not 1 (a DoD failure). CI treats those differently."""
    assert cmd_canary(_parse(["canary", "--dod"])) == 2
    assert "--stage is required" in capsys.readouterr().err


def test_dod_forwards_the_stage_and_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--stage`` and ``--days`` reach the shadow-DoD computation."""
    seen = _recorder(monkeypatch, "compute_shadow_dod", _dod_report())

    assert cmd_canary(_parse(["canary", "--dod", "--stage", "m4_50pct", "--days", "3"])) == 0
    capsys.readouterr()

    assert seen["stage"] == "m4_50pct"
    assert seen["window_days"] == 3


def test_dod_prometheus_url_precedence_is_flag_then_env_then_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Flag beats environment beats the localhost default.

    Reversing the first two is invisible unless a test sets both -- and an
    operator pointing a one-off check at a different Prometheus would silently
    read the one their shell happened to be configured for.
    """
    seen = _recorder(monkeypatch, "compute_shadow_dod", _dod_report())
    base = ["canary", "--dod", "--stage", "m4_1pct"]

    monkeypatch.setenv("PARALLAX_PROMETHEUS_URL", "http://from-env:9090")
    cmd_canary(_parse([*base, "--prometheus-url", "http://from-flag:9090"]))
    assert seen["prom_url"] == "http://from-flag:9090"

    cmd_canary(_parse(base))
    assert seen["prom_url"] == "http://from-env:9090"

    monkeypatch.delenv("PARALLAX_PROMETHEUS_URL")
    cmd_canary(_parse(base))
    assert seen["prom_url"] == "http://localhost:9090"
    capsys.readouterr()


def test_dod_exit_code_follows_the_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PASS exits 0 and anything else exits 1. This is the CI gate."""
    argv = ["canary", "--dod", "--stage", "m4_1pct"]

    _recorder(monkeypatch, "compute_shadow_dod", _dod_report(DodVerdict.PASS))
    assert cmd_canary(_parse(argv)) == 0

    _recorder(monkeypatch, "compute_shadow_dod", _dod_report(DodVerdict.FAIL))
    assert cmd_canary(_parse(argv)) == 1
    capsys.readouterr()


def test_dod_json_carries_the_sample_size_and_the_gate_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSON payload is the machine-readable contract.

    ``sample_size`` is how a reader knows whether to trust a verdict at all,
    and the note is what tells them which three metrics this summary does NOT
    cover -- silently dropping either leaves the payload looking complete.
    """
    _recorder(monkeypatch, "compute_shadow_dod", _dod_report())

    assert cmd_canary(_parse(["canary", "--dod", "--stage", "m4_1pct", "--format", "json"])) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["metrics"][0]["sample_size"] == 123
    assert payload["note"].startswith(
        "error_rate / p99_latency / data_loss are gated by the T1-T5 Prometheus"
    )


# ----------------------------------------------------------------------
# _open_outcome_store
# ----------------------------------------------------------------------


def test_outcome_store_is_not_opened_in_dry_run(tmp_path: pathlib.Path) -> None:
    """A dry run writes nothing, stage or no stage.

    Opening the store here would create a SQLite file for an invocation whose
    whole contract is that it touches no real I/O.
    """
    args = argparse.Namespace(
        stage="m4_1pct", dry_run=True, audit_db=str(tmp_path / "o.db")
    )

    assert _open_outcome_store(args) is None


def test_outcome_store_is_not_opened_without_a_stage(tmp_path: pathlib.Path) -> None:
    """No stage means no producer, even if the attribute is missing entirely."""
    args = argparse.Namespace(dry_run=False, audit_db=str(tmp_path / "o.db"))

    assert _open_outcome_store(args) is None


def test_outcome_store_honours_the_audit_db_path(tmp_path: pathlib.Path) -> None:
    """The outcome half must land in the same file as the audit half.

    They are joined by ``event_id`` in the exporter; splitting them across two
    databases produces two half-populated stores and no series at all.
    """
    db_path = tmp_path / "canary.db"
    args = argparse.Namespace(stage="m4_1pct", dry_run=False, audit_db=str(db_path))

    store = _open_outcome_store(args)
    assert store is not None
    try:
        assert store.db_path == db_path
    finally:
        store.close()


# ----------------------------------------------------------------------
# --rollback-drill
# ----------------------------------------------------------------------


def test_rollback_drill_forwards_every_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
) -> None:
    """Every drill knob reaches ``run_full_drill``, at a non-default value.

    Non-default on purpose: a forwarding that was replaced by the hardcoded
    default is indistinguishable from a working one whenever the test happens
    to pass the default.
    """
    db_path = tmp_path / "audit.db"
    seen = _recorder(
        monkeypatch,
        "run_full_drill",
        (_drill_report("drain"), _drill_report("reemit"), _drill_report("idempotency")),
    )

    rc = cmd_canary(
        _parse(
            [
                "canary", "--rollback-drill",
                "--in-flight", "3",
                "--reemit-count", "2",
                "--concurrency", "4",
                "--timeout", "12.5",
                "--audit-db", str(db_path),
            ]
        )
    )
    capsys.readouterr()

    assert rc == 0
    assert seen["in_flight_count"] == 3
    assert seen["reemit_count"] == 2
    assert seen["concurrency"] == 4
    assert seen["timeout_s"] == 12.5
    assert seen["dry_run"] is False
    # Real mode MUST hit the configured audit store, at the configured path.
    assert seen["audit_log"] is not None
    assert seen["audit_log"].db_path == db_path


def test_rollback_drill_dry_run_opens_no_audit_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` means no audit store and a dry drill underneath."""
    seen = _recorder(
        monkeypatch,
        "run_full_drill",
        (_drill_report("drain"), _drill_report("reemit"), _drill_report("idempotency")),
    )

    assert cmd_canary(_parse(["canary", "--rollback-drill", "--dry-run"])) == 0
    capsys.readouterr()

    assert seen["audit_log"] is None
    assert seen["dry_run"] is True


def test_rollback_drill_only_forwards_a_stage_when_it_opened_an_outcome_store(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
) -> None:
    """A stage without an outcome store is a producer with nowhere to write.

    In dry-run mode no store is opened, so the stage must NOT be passed down --
    otherwise the drill would try to record outcomes against ``None``.
    """
    seen = _recorder(
        monkeypatch,
        "run_full_drill",
        (_drill_report("drain"), _drill_report("reemit"), _drill_report("idempotency")),
    )

    cmd_canary(_parse(["canary", "--rollback-drill", "--stage", "m4_1pct", "--dry-run"]))
    assert seen["stage"] is None
    assert seen["outcome_store"] is None

    cmd_canary(
        _parse(
            [
                "canary", "--rollback-drill",
                "--stage", "m4_1pct",
                "--audit-db", str(tmp_path / "audit.db"),
            ]
        )
    )
    capsys.readouterr()
    assert seen["stage"] == "m4_1pct"
    assert seen["outcome_store"] is not None


def test_rollback_drill_fails_when_any_single_drill_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One failing drill out of three is a failure. Requiring all three is not.

    This is the exit code CI reads to decide whether the canary is safe to
    advance; a drill suite that only fails when EVERY scenario fails would pass
    a run whose drain never completed.
    """
    _recorder(
        monkeypatch,
        "run_full_drill",
        (
            _drill_report("drain", overall="fail"),
            _drill_report("reemit"),
            _drill_report("idempotency"),
        ),
    )
    assert cmd_canary(_parse(["canary", "--rollback-drill", "--dry-run"])) == 1

    _recorder(
        monkeypatch,
        "run_full_drill",
        (_drill_report("drain"), _drill_report("reemit"), _drill_report("idempotency")),
    )
    assert cmd_canary(_parse(["canary", "--rollback-drill", "--dry-run"])) == 0
    capsys.readouterr()


# ----------------------------------------------------------------------
# --drain-test and --orbit-reemit-test
# ----------------------------------------------------------------------


def test_drain_test_forwards_every_flag_and_reports_the_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both dry-run states have to be driven.

    A ``dry_run=True`` hardcoded at the call site is invisible to any test that
    only ever passes ``--dry-run``, and it would turn every real preflight
    drain into a simulation that reports success without draining anything.
    """
    seen = _recorder(monkeypatch, "run_drain_drill", _drill_report("drain"))

    argv = ["canary", "--drain-test", "--in-flight", "3", "--timeout", "12.5", "--dry-run"]
    assert cmd_canary(_parse(argv)) == 0
    assert seen["in_flight_count"] == 3
    assert seen["timeout_s"] == 12.5
    assert seen["dry_run"] is True

    wet = _recorder(monkeypatch, "run_drain_drill", _drill_report("drain"))
    assert cmd_canary(_parse(["canary", "--drain-test", "--in-flight", "3"])) == 0
    assert wet["dry_run"] is False

    _recorder(monkeypatch, "run_drain_drill", _drill_report("drain", overall="fail"))
    assert cmd_canary(_parse(argv)) == 1
    capsys.readouterr()


def test_reemit_test_forwards_every_flag_and_reports_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
) -> None:
    """Both dry-run states again, and real mode must reach the audit store.

    A hardcoded ``dry_run=True`` would send ``run_reemit_drill`` down its
    no-audit sandbox branch on every invocation, so the command would report a
    passing idempotency check having written nothing at all.
    """
    seen = _recorder(monkeypatch, "run_reemit_drill", _drill_report("reemit"))

    argv = [
        "canary", "--orbit-reemit-test", "--reemit-count", "2", "--stage", "m4_1pct",
        "--dry-run",
    ]
    assert cmd_canary(_parse(argv)) == 0
    assert seen["reemit_count"] == 2
    assert seen["dry_run"] is True
    # Dry run opened no outcome store, so no stage may be forwarded.
    assert seen["stage"] is None

    wet = _recorder(monkeypatch, "run_reemit_drill", _drill_report("reemit"))
    assert (
        cmd_canary(
            _parse(
                [
                    "canary", "--orbit-reemit-test",
                    "--reemit-count", "2",
                    "--audit-db", str(tmp_path / "audit.db"),
                ]
            )
        )
        == 0
    )
    assert wet["dry_run"] is False
    assert wet["audit_log"] is not None

    _recorder(monkeypatch, "run_reemit_drill", _drill_report("reemit", overall="fail"))
    assert cmd_canary(_parse(argv)) == 1
    capsys.readouterr()


# ----------------------------------------------------------------------
# Drill rendering
# ----------------------------------------------------------------------


def test_drill_json_output_is_only_json(capsys: pytest.CaptureFixture[str]) -> None:
    """``--format json`` emits the payload and nothing else.

    The early return matters: dropping it appends the pretty rendering to the
    JSON document and every machine reader downstream fails to parse it.
    """
    _print_drill_reports([_drill_report("drain", dry_run=True)], fmt="json")

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "Drill:" not in out
    assert payload[0]["drill"] == "drain"
    assert payload[0]["overall"] == "pass"
    assert payload[0]["dry_run"] is True
    assert payload[0]["steps"][0]["observations"] == {"k": 1}


def test_drill_pretty_output_marks_pass_and_fail_distinctly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A passing step reads as a tick and a failing step as a cross.

    Swapping them turns an operator's read of a drill report exactly upside
    down, and every existing assertion is on the exit code, not the render.
    """
    _print_drill_reports([_drill_report("drain", overall="pass")], fmt="pretty")
    passed = capsys.readouterr().out
    assert "✓ only_step" in passed
    assert "✗" not in passed
    # Observations are rendered underneath the step they belong to.
    assert "(k=1)" in passed

    _print_drill_reports([_drill_report("drain", overall="fail")], fmt="pretty")
    failed = capsys.readouterr().out
    assert "✗ only_step" in failed
    assert "✓" not in failed


def test_drill_pretty_output_omits_an_empty_observations_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A step with no observations gets no empty parenthetical."""
    report = DrillReport(
        drill="drain",
        dry_run=True,
        steps=(
            DrillStepResult(name="bare", status="pass", detail="d", observations={}),
        ),
        overall="pass",
    )

    _print_drill_reports([report], fmt="pretty")

    assert "()" not in capsys.readouterr().out


# ----------------------------------------------------------------------
# --check-alerting
# ----------------------------------------------------------------------


def test_check_alerting_reports_both_webhooks_by_their_env_var_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe reads four environment variables, all named here as literals.

    The variable names ARE the contract with the secret manager: reading a
    differently-named one reports a correctly-configured deployment as missing
    its alerting, and reports the wrong name to the operator trying to fix it.

    THE FIRST FOUR STATEMENTS ARE IN THIS ORDER ON PURPOSE. One of those
    variables, ``PARALLAX_CANARY_ALERTING_LIVE_PROBE``, is the switch the
    module documents as "send a real test notification (pages oncall --
    operators only)", and the two set beside it are credential-shaped. So
    delivery is neutralised FIRST and the switch is armed SECOND: there is
    never an instant in which an armed probe faces a live network. Arming
    first would make the safety of this test rest on ``_cmd_check_alerting``
    staying inert -- i.e. on the very guard logic a mutant is free to invert,
    and on the live probe never being implemented by someone who inherits this
    test green. With the seam neutralised first it rests on nothing.
    """
    attempts = _block_all_delivery(monkeypatch)

    monkeypatch.setenv("PAGERDUTY_M4_CANARY_KEY", "pd-key")
    monkeypatch.setenv("SLACK_WEBHOOK_M4_CANARY", "https://slack.example/hook")
    monkeypatch.setenv("PARALLAX_CANARY_ALERTING_LIVE_PROBE", "1")

    assert cmd_canary(_parse(["canary", "--check-alerting", "--format", "json"])) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["pagerduty"] == {
        "env_var": "PAGERDUTY_M4_CANARY_KEY",
        "status": "configured",
    }
    assert payload["slack"] == {
        "env_var": "SLACK_WEBHOOK_M4_CANARY",
        "status": "configured",
    }
    assert payload["live_probe"] is True
    # The probe is config-only: an ARMED live_probe still delivered nothing.
    # This is the line that has to be revisited -- loudly, not silently -- on
    # the day the live probe is implemented.
    assert attempts == []
    # ...and that emptiness is not vacuous. The recorder is installed and is
    # the only delivery path this process has: it logs the attempt and refuses
    # it, so a monkeypatch that failed to take cannot masquerade as safety.
    with pytest.raises(_NetworkBlocked):
        socket.create_connection(("slack.example", 443))
    assert attempts == ["socket.create_connection"]


def test_check_alerting_live_probe_defaults_to_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live probe pages oncall, so it is off unless explicitly set to 1."""
    monkeypatch.setenv("PAGERDUTY_M4_CANARY_KEY", "pd-key")
    monkeypatch.setenv("SLACK_WEBHOOK_M4_CANARY", "https://slack.example/hook")
    monkeypatch.delenv("PARALLAX_CANARY_ALERTING_LIVE_PROBE", raising=False)

    assert cmd_canary(_parse(["canary", "--check-alerting", "--format", "json"])) == 0

    assert json.loads(capsys.readouterr().out)["live_probe"] is False


def test_check_alerting_requires_both_webhooks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One configured webhook is not enough -- the probe must exit 1.

    Half-configured alerting is the failure mode this check exists to catch:
    the canary would trip and page nobody on the missing channel.
    """
    monkeypatch.setenv("PAGERDUTY_M4_CANARY_KEY", "pd-key")
    monkeypatch.delenv("SLACK_WEBHOOK_M4_CANARY", raising=False)
    monkeypatch.delenv("PARALLAX_CANARY_ALERTING_LIVE_PROBE", raising=False)

    assert cmd_canary(_parse(["canary", "--check-alerting", "--format", "json"])) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["pagerduty"]["status"] == "configured"
    assert payload["slack"]["status"] == "missing"


def test_check_alerting_pretty_output_is_not_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default rendering is the human one."""
    monkeypatch.setenv("PAGERDUTY_M4_CANARY_KEY", "pd-key")
    monkeypatch.setenv("SLACK_WEBHOOK_M4_CANARY", "https://slack.example/hook")
    monkeypatch.delenv("PARALLAX_CANARY_ALERTING_LIVE_PROBE", raising=False)

    assert cmd_canary(_parse(["canary", "--check-alerting"])) == 0

    out = capsys.readouterr().out
    assert out.startswith("Alerting probe (config-only):")
    assert "PagerDuty: configured" in out
    assert "Live probe: disabled" in out
