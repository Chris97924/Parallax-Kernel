"""Tests for eval/longmemeval/reproduce.py skeleton (ADR-006 gate #5)."""

from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any

import pytest

from eval.longmemeval.reproduce import (
    GATE_THRESHOLD,
    ReproduceConfig,
    ReproduceReport,
    _stub_run,
    main,
    reproduce_once,
    write_reproduce_report,
)
from eval.longmemeval.schema_v2 import RunReportV2


def _fake_run_fn(score_a: float, score_b: float):
    """Return a run_fn that yields a given fallback_e2e per seed."""
    scores = {1: score_a, 2: score_b}

    def _inner(label: str, seed: int) -> dict[str, Any]:
        report = _stub_run(label, seed)
        # Pydantic models live on validated copies; the raw dict is still
        # a plain dict that we can mutate here before it hits RunReportV2.
        report["aggregate"] = {**report["aggregate"], "fallback_e2e": scores[seed]}
        return report

    return _inner


class TestStubRunSchema:
    def test_stub_run_seed_1_validates(self):
        report = _stub_run("smoke", seed=1)
        RunReportV2(**report)  # must not raise

    def test_stub_run_seed_2_validates(self):
        report = _stub_run("smoke", seed=2)
        RunReportV2(**report)  # must not raise

    def test_stub_run_is_deterministic_for_same_seed(self):
        a = _stub_run("smoke", seed=1)
        b = _stub_run("smoke", seed=1)
        # Drop created_at (timestamp, non-deterministic) before comparison.
        a_agg = a["aggregate"]["fallback_e2e"]
        b_agg = b["aggregate"]["fallback_e2e"]
        assert a_agg == b_agg


class TestReproduceOnce:
    def test_passes_gate_when_delta_below_threshold(self):
        cfg = ReproduceConfig(label="ok", seed_a=1, seed_b=2)
        report = reproduce_once(cfg, run_fn=_fake_run_fn(0.860, 0.865))
        assert report.max_minus_min == pytest.approx(0.005)
        assert report.passes_gate is True
        assert report.fallback_e2e_a == pytest.approx(0.860)
        assert report.fallback_e2e_b == pytest.approx(0.865)

    def test_fails_gate_when_delta_above_threshold(self):
        cfg = ReproduceConfig(label="bad", seed_a=1, seed_b=2)
        report = reproduce_once(cfg, run_fn=_fake_run_fn(0.860, 0.880))
        assert report.max_minus_min == pytest.approx(0.020)
        assert report.passes_gate is False

    def test_max_minus_min_is_symmetric(self):
        cfg = ReproduceConfig(label="sym", seed_a=1, seed_b=2)
        up = reproduce_once(cfg, run_fn=_fake_run_fn(0.860, 0.880))
        down = reproduce_once(cfg, run_fn=_fake_run_fn(0.880, 0.860))
        assert up.max_minus_min == pytest.approx(down.max_minus_min)

    def test_does_not_mutate_input_config(self):
        cfg = ReproduceConfig(label="immutable", seed_a=1, seed_b=2)
        original = dataclasses.replace(cfg)
        reproduce_once(cfg, run_fn=_fake_run_fn(0.860, 0.865))
        assert cfg == original

    def test_gate_threshold_constant(self):
        assert GATE_THRESHOLD == 0.01

    def test_just_under_threshold_passes(self):
        cfg = ReproduceConfig(label="under")
        report = reproduce_once(cfg, run_fn=_fake_run_fn(0.860, 0.869))
        assert report.max_minus_min < GATE_THRESHOLD
        assert report.passes_gate is True


class TestWriteReproduceReport:
    def test_roundtrip_json(self, tmp_path: pathlib.Path):
        report = ReproduceReport(
            label="smoke",
            fallback_e2e_a=0.861,
            fallback_e2e_b=0.863,
            max_minus_min=0.002,
            passes_gate=True,
            seed_a=1,
            seed_b=2,
            created_at="2026-04-20T00:00:00+00:00",
        )
        out_path = tmp_path / "smoke.json"
        write_reproduce_report(out_path, report)
        loaded = json.loads(out_path.read_text(encoding="utf-8"))
        assert loaded["label"] == "smoke"
        assert loaded["fallback_e2e_a"] == 0.861
        assert loaded["fallback_e2e_b"] == 0.863
        assert loaded["max_minus_min"] == 0.002
        assert loaded["passes_gate"] is True
        assert loaded["seed_a"] == 1
        assert loaded["seed_b"] == 2

    def test_creates_parent_dirs(self, tmp_path: pathlib.Path):
        report = ReproduceReport(
            label="nested",
            fallback_e2e_a=0.0,
            fallback_e2e_b=0.0,
            max_minus_min=0.0,
            passes_gate=True,
            seed_a=1,
            seed_b=2,
            created_at="2026-04-20T00:00:00+00:00",
        )
        out_path = tmp_path / "a" / "b" / "c" / "nested.json"
        write_reproduce_report(out_path, report)
        assert out_path.exists()


class TestCLI:
    def test_dry_run_exits_zero_without_disk_writes(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
    ):
        rc = main(["--label", "smoke", "--out-dir", str(tmp_path), "--dry-run"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "dry-run" in captured.out
        assert "smoke" in captured.out
        # Dry-run must not touch out-dir.
        assert list(tmp_path.iterdir()) == []

    def test_non_dry_run_writes_json_file(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
    ):
        rc = main(["--label", "live", "--out-dir", str(tmp_path)])
        # Return code depends on the deterministic synthetic score — we just
        # assert the file landed and the output line is present.
        assert rc in (0, 1)
        out_path = tmp_path / "live.json"
        assert out_path.exists()
        loaded = json.loads(out_path.read_text(encoding="utf-8"))
        assert loaded["label"] == "live"
        assert set(loaded.keys()) == {
            "label",
            "fallback_e2e_a",
            "fallback_e2e_b",
            "max_minus_min",
            "passes_gate",
            "seed_a",
            "seed_b",
            "created_at",
        }
        captured = capsys.readouterr()
        assert "reproduce:" in captured.out
        assert "passes_gate=" in captured.out
