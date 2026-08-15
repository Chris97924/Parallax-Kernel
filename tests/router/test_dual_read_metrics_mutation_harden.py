"""Mutation-hardening for ``parallax.router.dual_read_metrics`` (overnight-20260816 S3).

Additive companion to ``test_dual_read_metrics.py``. Each test here pins a
behaviour that a surviving semantic mutant was free to change.

The recurring blind spot is the existing suite's ``_record`` helper: it always
writes every optional field, so no test ever exercises what the module does with
a record that OMITS one, or that carries a value of the wrong type, or that
carries two fields which disagree. Several defaults documented in the module
docstring were therefore unasserted. The builders below deliberately construct
sparse and conflicting records instead.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest

from parallax.router import dual_read_metrics as m

_ANCHOR = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)


def _write(log_dir: Path, records: list[dict[str, Any]], date: str = "2026-04-26") -> Path:
    """Write raw records verbatim — no field defaulting, unlike the main suite."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"dual-read-decisions-{date}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return path


def _bare(outcome: str, **extra: Any) -> dict[str, Any]:
    """A record with ONLY outcome + timestamp. Every other field is absent."""
    record: dict[str, Any] = {
        "outcome": outcome,
        "timestamp": "2026-04-26T11:30:00.000000+00:00",
    }
    record.update(extra)
    return record


# ---------------------------------------------------------------------------
# Defaults for absent fields
# ---------------------------------------------------------------------------


def test_a_record_with_no_data_quality_flag_counts_as_normal(tmp_path: Path) -> None:
    """Missing ``data_quality_flag`` means "normal", so the record still counts.

    Every record in the main suite carries an explicit flag, so the documented
    default was free to be anything. Defaulting to an excluded value (e.g.
    ``cold_start``) would silently drop real production records out of every
    rate — the numerator and the denominator both — and the rates would read
    healthy because they were computed over nothing.
    """
    _write(tmp_path, [_bare("diverge"), _bare("match")])

    assert m.discrepancy_rate("1h", log_dir=tmp_path, now=_ANCHOR) == pytest.approx(0.5)


def test_corpus_immature_records_count_toward_production_rates(tmp_path: Path) -> None:
    """``corpus_immature`` is in the default filter; ``cold_start`` is not."""
    assert m.DEFAULT_DATA_QUALITY_FILTER == ("normal", "corpus_immature")

    _write(
        tmp_path,
        [
            _bare("diverge", data_quality_flag="corpus_immature"),
            _bare("match", data_quality_flag="normal"),
        ],
    )

    assert m.discrepancy_rate("1h", log_dir=tmp_path, now=_ANCHOR) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Field precedence and type strictness
# ---------------------------------------------------------------------------


def test_outcome_wins_over_arbitration_outcome_when_both_are_present(
    tmp_path: Path,
) -> None:
    """``outcome`` is the primary field; ``arbitration_outcome`` is the fallback.

    No existing test writes both, so the precedence was reversible without
    detection. A record carrying both is exactly what the shadow-JSONL compat
    path produces, and reading the wrong one silently reclassifies outcomes.
    """
    _write(tmp_path, [_bare("match", arbitration_outcome="diverge")])

    assert m.discrepancy_rate("1h", log_dir=tmp_path, now=_ANCHOR) == 0.0


def test_arbitration_outcome_is_still_read_when_outcome_is_absent(
    tmp_path: Path,
) -> None:
    """The fallback must keep working — precedence, not exclusivity."""
    _write(
        tmp_path,
        [
            {
                "arbitration_outcome": "diverge",
                "timestamp": "2026-04-26T11:30:00.000000+00:00",
            },
            _bare("match"),
        ],
    )

    assert m.discrepancy_rate("1h", log_dir=tmp_path, now=_ANCHOR) == pytest.approx(0.5)


def test_a_non_boolean_write_error_value_is_not_a_write_error(tmp_path: Path) -> None:
    """``write_error_observed`` is matched with ``is True``, not truthiness.

    A foreign or hand-edited record carrying the STRING ``"false"`` is truthy in
    Python. Relaxing the check to truthiness would count it as a write error and
    push the rate past its 0.02% DoD threshold on data that says the opposite.
    """
    _write(
        tmp_path,
        [_bare("match", write_error_observed="false"), _bare("match")],
    )

    assert m.write_error_rate("1h", log_dir=tmp_path, now=_ANCHOR) == 0.0


# ---------------------------------------------------------------------------
# Conflict counting
# ---------------------------------------------------------------------------


def test_one_record_is_one_conflict_even_when_both_signals_fire(
    tmp_path: Path,
) -> None:
    """A tie verdict that ALSO carries a conflict_event_id counts once, not twice.

    The two conflict signals are independent fields on the same record and a
    live conflict routinely sets both. Counting each signal separately makes the
    numerator exceed the record count, so the "rate" can climb above 1.0 and the
    1% gate trips on arithmetic rather than on conflicts.
    """
    _write(
        tmp_path,
        [
            _bare("diverge", winning_source="tie", conflict_event_id="evt-1"),
            _bare("match"),
        ],
    )

    rate = m.arbitration_conflict_rate("1h", log_dir=tmp_path, now=_ANCHOR)
    assert rate == pytest.approx(0.5)
    assert rate <= 1.0


# ---------------------------------------------------------------------------
# Window boundary
# ---------------------------------------------------------------------------


def test_a_record_exactly_at_the_cutoff_is_inside_the_window(tmp_path: Path) -> None:
    """The window is half-open at the far edge: ``ts < cutoff`` is dropped, ``==`` is kept.

    With ``now`` at 12:00 and a 1h window the cutoff is exactly 11:00. No
    existing test places a record on that instant, so the comparison could
    flip to ``<=`` and silently shrink every window by one sample.
    """
    _write(
        tmp_path,
        [
            {
                "outcome": "diverge",
                "timestamp": "2026-04-26T11:00:00.000000+00:00",
            }
        ],
    )

    assert m.discrepancy_rate("1h", log_dir=tmp_path, now=_ANCHOR) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Log-dir resolution precedence
# ---------------------------------------------------------------------------


def test_an_explicit_log_dir_beats_the_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--log-dir`` on the CLI must override ``DUAL_READ_LOG_DIR``.

    The env var is the deployment default and the argument is the operator's
    override; reversing them would make ``--log-dir`` silently inert on any host
    that sets the variable, and the CLI would report on the wrong corpus while
    looking like it honoured the flag.
    """
    env_dir = tmp_path / "from_env"
    env_dir.mkdir()
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(env_dir))

    explicit_dir = tmp_path / "explicit"
    _write(explicit_dir, [_bare("diverge")])

    assert m.discrepancy_rate("1h", log_dir=explicit_dir, now=_ANCHOR) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# compute_all_rates parity with the standalone functions
# ---------------------------------------------------------------------------


def test_compute_all_rates_crosswalk_miss_counts_misses_not_hits() -> None:
    """The bundled path must agree with ``crosswalk_miss_rate``.

    ``compute_all_rates`` re-implements each rate inline for the single-pass
    optimisation, so its predicates can drift from the standalone functions the
    endpoint and the CLI use. The counts here are deliberately asymmetric (1 of
    4) — a 50/50 split would read the same whether the predicate counted misses
    or hits.
    """
    records = [
        _bare("match", crosswalk_status="miss"),
        _bare("match", crosswalk_status="ok"),
        _bare("match", crosswalk_status="ok"),
        _bare("match", crosswalk_status="ok"),
    ]

    bundled = m.compute_all_rates(records)

    assert bundled["crosswalk_miss_rate"] == pytest.approx(0.25)


def test_compute_all_rates_matches_the_standalone_functions(tmp_path: Path) -> None:
    """One corpus, two code paths, identical numbers.

    This is the regression that keeps the two authoritative gates — ``/metrics``
    and ``scripts/dual_read_continuity_check.py`` — from contradicting each
    other on the same records.
    """
    records = [
        _bare("diverge", crosswalk_status="miss"),
        _bare("match", winning_source="fallback"),
        _bare("match", write_error_observed=True),
        _bare("aphelion_unreachable"),
        _bare("match", circuit_breaker_tripped=True),
    ]
    _write(tmp_path, records)

    bundled = m.compute_all_rates(records)

    assert bundled["discrepancy_rate"] == pytest.approx(
        m.discrepancy_rate("1h", log_dir=tmp_path, now=_ANCHOR)
    )
    assert bundled["arbitration_conflict_rate"] == pytest.approx(
        m.arbitration_conflict_rate("1h", log_dir=tmp_path, now=_ANCHOR)
    )
    assert bundled["write_error_rate"] == pytest.approx(
        m.write_error_rate("1h", log_dir=tmp_path, now=_ANCHOR)
    )
    assert bundled["aphelion_unreachable_rate"] == pytest.approx(
        m.aphelion_unreachable_rate("1h", log_dir=tmp_path, now=_ANCHOR)
    )
    assert bundled["crosswalk_miss_rate"] == pytest.approx(
        m.crosswalk_miss_rate("1h", log_dir=tmp_path, now=_ANCHOR)
    )
    assert bundled["circuit_open_count"] == m.circuit_open_count(
        "1h", log_dir=tmp_path, now=_ANCHOR
    )
