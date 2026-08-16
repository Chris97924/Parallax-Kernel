"""Mutation-hardening tests for ``parallax.router.dual_read_decision_log`` (S7).

Every test below was written against a specific *surviving* mutant: a
semantic edit to the JSONL producer that the pre-existing suite accepted.
Each docstring names the mutant it kills.

The producer's contract lives in its module docstring — resolution order
for the kill switch and the log directory, the safe defaults applied to a
partially-formed record, and "deterministic key-sorted JSON". Those are
the clauses the mutants below edit and the ones these tests pin.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from parallax.router import dual_read_decision_log as ddlog

_ANCHOR = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env-driven state leaks in — each test sets exactly what it needs."""
    monkeypatch.delenv("DUAL_READ_LOG_ENABLED", raising=False)
    monkeypatch.delenv("DUAL_READ_LOG_DIR", raising=False)
    monkeypatch.delenv("DUAL_READ", raising=False)


def _decision(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "correlation_id": "cid-1",
        "query_type": "recent_context",
        "outcome": "match",
        "winning_source": "parallax",
        "policy_version": "v0.3.0-rc",
        "write_error_observed": False,
        "conflict_event_id": None,
        "data_quality_flag": "normal",
    }
    base.update(overrides)
    return base


def _sole_record(log_dir: Path) -> dict:
    files = sorted(log_dir.glob("dual-read-decisions-*.jsonl"))
    assert len(files) == 1, f"expected one daily file, got {files}"
    lines = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


# ---------------------------------------------------------------------------
# is_log_enabled — the documented resolution order
# ---------------------------------------------------------------------------


class TestKillSwitchResolution:
    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "Yes", "  On  "])
    def test_truthy_vocabulary_enables_the_producer(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: the truthy tuple narrowed from ``("1","true","yes","on")``
        to ``("1","true")``.

        The docstring promises "common truthy / falsy strings", and an
        operator who writes ``DUAL_READ_LOG_ENABLED=yes`` in a compose file
        gets a silently disabled producer if the vocabulary shrinks. The
        pre-existing tests only ever pass ``"true"``/``"false"``, so three
        quarters of the accepted spellings are unpinned — as is the
        ``strip().lower()`` that makes ``"  On  "`` work.
        """
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", raw)
        assert ddlog.is_log_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "   ", "maybe"])
    def test_non_truthy_value_wins_over_the_dual_read_mirror(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: ``if raw is not None:`` weakened to ``if raw:``.

        Resolution order in the module docstring is explicit: an explicit
        ``DUAL_READ_LOG_ENABLED`` is step 1 and the ``DUAL_READ`` mirror is
        step 2. Testing for truthiness instead of presence collapses the
        two for the empty string, so ``DUAL_READ_LOG_ENABLED=`` — how a
        shell, a compose file, or a systemd unit spells "set it to
        nothing" — stops being a kill switch and starts inheriting
        whatever ``DUAL_READ`` says. Here the mirror is deliberately ON, so
        only an explicit-value-wins reading keeps the producer silent.
        """
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", raw)
        monkeypatch.setenv("DUAL_READ", "true")
        assert ddlog.is_log_enabled() is False

    def test_empty_kill_switch_writes_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write-path half of the test above — no record reaches disk."""
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "")
        monkeypatch.setenv("DUAL_READ", "true")

        assert ddlog.append_decision(_decision(), log_dir=tmp_path, now=_ANCHOR) is None
        assert list(tmp_path.glob("dual-read-decisions-*.jsonl")) == []


# ---------------------------------------------------------------------------
# resolve_log_dir
# ---------------------------------------------------------------------------


class TestResolveLogDir:
    def test_empty_env_falls_back_to_the_builtin_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: ``if env:`` tightened to ``if env is not None:``.

        ``Path("").resolve()`` is the current working directory, so an
        empty ``DUAL_READ_LOG_DIR`` would scatter daily JSONL files
        wherever the server happened to be started from — and the metrics
        reader, which looks in the built-in default, would find an empty
        corpus and report a healthy zero. Only the built-in default is a
        safe answer for "set to nothing".
        """
        monkeypatch.setenv("DUAL_READ_LOG_DIR", "")

        resolved = ddlog.resolve_log_dir()

        assert resolved == ddlog._DEFAULT_LOG_DIR.resolve()  # noqa: SLF001
        assert resolved != Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


class TestTimestamps:
    def test_iso_timestamp_keeps_its_microseconds(self, tmp_path: Path, monkeypatch) -> None:
        """MUTANT: ``.replace(microsecond=micros)`` -> ``.replace(microsecond=0)``.

        ``timestamp`` exists so the metrics reader can parse these lines
        without a schema bump, and it is documented as a mirror of
        ``timestamp_us_utc``. Zeroing the microseconds keeps the field
        well-formed and ``timespec="microseconds"`` keeps it looking
        precise — it just stops agreeing with the integer clock beside it,
        which is exactly the kind of drift no existing assertion notices.
        """
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
        anchor = _dt.datetime(2026, 4, 30, 12, 0, 0, 123_456, tzinfo=_dt.UTC)

        ddlog.append_decision(_decision(), log_dir=tmp_path, now=anchor)
        record = _sole_record(tmp_path)

        expected_us = int(anchor.timestamp() * 1_000_000)
        assert expected_us % 1_000_000 != 0, "anchor must carry sub-second detail"
        assert record["timestamp_us_utc"] == expected_us
        assert record["timestamp"].endswith(f".{expected_us % 1_000_000:06d}+00:00"), (
            f"ISO mirror lost its microseconds: {record['timestamp']}"
        )

    def test_aware_non_utc_anchor_keeps_its_instant(self, tmp_path: Path, monkeypatch) -> None:
        """MUTANT: ``if anchor.tzinfo is None:`` -> ``is not None``.

        Inverted, the guard stamps UTC onto an anchor that already carries
        an offset, moving the recorded instant by the size of that offset —
        eight hours here. The pre-existing tests only ever pass UTC-aware
        anchors, where stamping UTC is a no-op, so the guard's direction is
        invisible to them. A non-UTC offset separates the two readings on
        any host, which a naive-datetime test could not do (it would agree
        with the mutant on a UTC machine and disagree on this one).
        """
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
        plus_eight = _dt.timezone(_dt.timedelta(hours=8))
        anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=plus_eight)  # 04:00Z

        ddlog.append_decision(_decision(), log_dir=tmp_path, now=anchor)
        record = _sole_record(tmp_path)

        assert record["timestamp_us_utc"] == int(anchor.timestamp() * 1_000_000)
        assert record["timestamp"].startswith("2026-04-30T04:00:00"), (
            "a +08:00 anchor was recorded at its wall-clock reading, not its "
            f"UTC instant: {record['timestamp']}"
        )


# ---------------------------------------------------------------------------
# _build_record — the documented safe defaults and the JSON shape
# ---------------------------------------------------------------------------


class TestRecordShape:
    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [(1, True), ("yes", True), (0, False), ("", False), ([], False)],
    )
    def test_write_error_observed_is_coerced_to_a_real_bool(
        self, supplied: object, expected: bool, tmp_path: Path, monkeypatch
    ) -> None:
        """MUTANT: the ``bool(...)`` coercion dropped.

        The schema types this field ``bool`` and ``write_error_rate``
        filters the record stream by it. Without the coercion a truthy
        non-bool from any caller lands in the JSONL as ``1`` or ``"yes"``,
        which a reader comparing against ``True`` counts as neither a
        failure nor a success. The pre-existing tests only ever supply real
        booleans.
        """
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")

        ddlog.append_decision(
            _decision(write_error_observed=supplied), log_dir=tmp_path, now=_ANCHOR
        )
        record = _sole_record(tmp_path)

        assert record["write_error_observed"] is expected, (
            f"{supplied!r} reached the JSONL as {record['write_error_observed']!r}"
        )

    def test_missing_winning_source_defaults_to_null_not_empty_string(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """MUTANT: ``.get("winning_source")`` -> ``.get("winning_source", "")``.

        ``null`` is the documented default and it is what the skipped path
        means: no arbitration happened. ``""`` is a different claim — an
        arbitration that picked a source with no name — and it is truthy-
        adjacent enough to slip past a reader checking ``is None``. The
        existing skipped-path test passes ``winning_source=None``
        explicitly, so the *default* is never exercised.
        """
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
        record_in = _decision()
        del record_in["winning_source"]

        ddlog.append_decision(record_in, log_dir=tmp_path, now=_ANCHOR)

        assert _sole_record(tmp_path)["winning_source"] is None

    def test_missing_data_quality_flag_defaults_to_normal(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """MUTANT: the ``"normal"`` default replaced with ``""``.

        ``data_quality_flag`` is a three-value vocabulary
        (``cold_start`` | ``corpus_immature`` | ``normal``); ``""`` is not
        in it. A record defaulted to the empty string is one a
        vocabulary-aware consumer must either drop or guess about.
        """
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
        record_in = _decision()
        del record_in["data_quality_flag"]

        ddlog.append_decision(record_in, log_dir=tmp_path, now=_ANCHOR)

        assert _sole_record(tmp_path)["data_quality_flag"] == "normal"

    def test_keys_are_written_in_sorted_order(self, tmp_path: Path, monkeypatch) -> None:
        """MUTANT: ``json.dumps(..., sort_keys=True)`` -> ``sort_keys=False``.

        "Deterministic key-sorted JSON" is the first line of the schema
        section, and the byte-equality test that guards determinism cannot
        see this: two runs of the same code produce the same *insertion*
        order too, so they stay byte-equal while the sorting is gone. What
        breaks is comparability across callers — a decision built with its
        extras in a different order stops being diffable against this one.
        """
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
        record_in = _decision()
        record_in["zeta_extra"] = 1
        record_in["alpha_extra"] = 2

        ddlog.append_decision(record_in, log_dir=tmp_path, now=_ANCHOR)
        files = sorted(tmp_path.glob("dual-read-decisions-*.jsonl"))
        line = files[0].read_text(encoding="utf-8").splitlines()[0]
        keys = [key for key, _ in json.loads(line, object_pairs_hook=lambda pairs: pairs)]

        assert keys == sorted(keys), f"keys are not sorted: {keys}"
        # Both extras survive — the passthrough is what makes the order visible.
        assert {"alpha_extra", "zeta_extra"} <= set(keys)


# ---------------------------------------------------------------------------
# Directory creation
# ---------------------------------------------------------------------------


class TestLogDirCreation:
    def test_nested_log_dir_is_created_with_its_parents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT: ``mkdir(parents=True, ...)`` -> ``parents=False``.

        The producer is fail-closed, so losing the intermediate directories
        does not raise — ``append_decision`` swallows the FileNotFoundError
        and returns ``None``, and the dual-read path carries on writing
        nothing at all. A ``DUAL_READ_LOG_DIR`` pointing somewhere more
        than one level below an existing directory is an ordinary
        deployment shape, and this is the mutant with the quietest failure
        mode of the set.
        """
        monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
        nested = tmp_path / "var" / "log" / "parallax" / "dual_read"

        path = ddlog.append_decision(_decision(), log_dir=nested, now=_ANCHOR)

        assert path is not None, "a multi-level log dir silently wrote nothing"
        assert path.is_file()
        assert _sole_record(nested)["correlation_id"] == "cid-1"
