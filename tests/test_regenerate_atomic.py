"""Tests for atomic preview writes and last-run.json health file (Story S6)."""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_DEFAULT_SCRIPT_PATH = Path("C:/Users/user/.claude/scripts/parallax_memory/regenerate.py")
_SCRIPT_PATH = Path(os.environ.get("PARALLAX_REGEN_SCRIPT_PATH", str(_DEFAULT_SCRIPT_PATH)))

if not _SCRIPT_PATH.is_file():
    if "PARALLAX_REGEN_SCRIPT_PATH" in os.environ:
        _skip_reason = (
            f"PARALLAX_REGEN_SCRIPT_PATH={_SCRIPT_PATH} is not a file; "
            "point it at your local regenerate.py (Claude Code SessionStart hook)."
        )
    else:
        _skip_reason = (
            f"regenerate.py hook script not found at default {_DEFAULT_SCRIPT_PATH}; "
            "set PARALLAX_REGEN_SCRIPT_PATH to your local path to enable these tests."
        )
    pytest.skip(_skip_reason, allow_module_level=True)

if not _SCRIPT_PATH.is_file():
    pytest.skip(
        f"regenerate.py hook script not present at {_SCRIPT_PATH}; "
        "these tests target a developer-local Claude Code hook outside the repo.",
        allow_module_level=True,
    )


def _load_mod():
    spec = importlib.util.spec_from_file_location("regenerate", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


regen = _load_mod()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_response(payload: dict):
    """Return a urllib-compatible fake response object."""
    body = json.dumps(payload).encode()

    class _FakeResp:
        status = 200

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    return _FakeResp()


def _happy_payload() -> dict:
    return {
        "memory_md": "# User\n",
        "companion_files": {"a.md": "body"},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAtomicReplace:
    def test_write_preview_uses_atomic_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """os.replace must be called at least twice (MEMORY.md.preview + companion)."""
        replace_calls: list[tuple] = []
        real_replace = os.replace

        def recording_replace(src, dst):
            replace_calls.append((src, dst))
            return real_replace(src, dst)

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        token_file = tmp_path / "token"
        token_file.write_text("test-token", encoding="utf-8")
        last_run = tmp_path / "last-run.json"

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        with patch.object(urllib.request, "urlopen", return_value=_fake_response(_happy_payload())):
            with patch("os.replace", side_effect=recording_replace):
                code = regen.main(
                    memory_dir=memory_dir,
                    token_path=token_file,
                    log_path=tmp_path / "diff.log",
                    url="http://localhost:8765/export/memory_md",
                    timeout=0.8,
                    last_run_path=last_run,
                )

        assert code == 0
        # At least MEMORY.md.preview + companion + last-run.json = 3 calls
        assert len(replace_calls) >= 2


class TestLastRunSuccess:
    def test_last_run_json_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path: last-run.json has ok=true, reason=ok, cards_written=1."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        token_file = tmp_path / "token"
        token_file.write_text("test-token", encoding="utf-8")
        last_run = tmp_path / "last-run.json"

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        with patch.object(urllib.request, "urlopen", return_value=_fake_response(_happy_payload())):
            code = regen.main(
                memory_dir=memory_dir,
                token_path=token_file,
                log_path=tmp_path / "diff.log",
                url="http://localhost:8765/export/memory_md",
                timeout=0.8,
                last_run_path=last_run,
            )

        assert code == 0
        data = json.loads(last_run.read_text(encoding="utf-8"))
        assert data["ok"] is True
        assert data["reason"] == "ok"
        assert data["cards_written"] == 1
        assert data["parallax_reachable"] is True
        assert data["elapsed_ms"] >= 0
        # ISO-8601 string — basic structural check
        ts = data["timestamp"]
        assert isinstance(ts, str) and "T" in ts and len(ts) >= 19


class TestLastRunParallaxDown:
    def test_last_run_json_on_parallax_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConnectionRefusedError → reason=parallax_unreachable, ok=false."""
        token_file = tmp_path / "token"
        token_file.write_text("test-token", encoding="utf-8")
        last_run = tmp_path / "last-run.json"

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        with patch.object(urllib.request, "urlopen", side_effect=ConnectionRefusedError()):
            code = regen.main(
                memory_dir=tmp_path / "memory",
                token_path=token_file,
                log_path=tmp_path / "diff.log",
                url="http://localhost:8765/export/memory_md",
                timeout=0.8,
                last_run_path=last_run,
            )

        assert code == 0
        data = json.loads(last_run.read_text(encoding="utf-8"))
        assert data["ok"] is False
        assert data["reason"] == "parallax_unreachable"
        assert data["parallax_reachable"] is False
        assert data["cards_written"] == 0


class TestLastRunTimeout:
    def test_last_run_json_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """socket.timeout → reason=timeout."""
        token_file = tmp_path / "token"
        token_file.write_text("test-token", encoding="utf-8")
        last_run = tmp_path / "last-run.json"

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        with patch.object(urllib.request, "urlopen", side_effect=TimeoutError()):
            code = regen.main(
                memory_dir=tmp_path / "memory",
                token_path=token_file,
                log_path=tmp_path / "diff.log",
                url="http://localhost:8765/export/memory_md",
                timeout=0.8,
                last_run_path=last_run,
            )

        assert code == 0
        data = json.loads(last_run.read_text(encoding="utf-8"))
        assert data["ok"] is False
        assert data["reason"] == "timeout"


class TestLastRunMissingToken:
    def test_last_run_json_on_missing_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing token file → reason=token_missing, ok=false."""
        last_run = tmp_path / "last-run.json"

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        code = regen.main(
            memory_dir=tmp_path / "memory",
            token_path=tmp_path / "nonexistent_token",
            log_path=tmp_path / "diff.log",
            url="http://localhost:8765/export/memory_md",
            timeout=0.8,
            last_run_path=last_run,
        )

        assert code == 0
        data = json.loads(last_run.read_text(encoding="utf-8"))
        assert data["ok"] is False
        assert data["reason"] == "token_missing"


class TestLastRunPhase2Flag:
    def test_last_run_json_on_phase2_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PARALLAX_REGEN_DRY_RUN=0 → reason=phase2_not_enabled, ok=false."""
        last_run = tmp_path / "last-run.json"

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "0")

        code = regen.main(
            memory_dir=tmp_path / "memory",
            token_path=tmp_path / "token",
            log_path=tmp_path / "diff.log",
            url="http://localhost:8765/export/memory_md",
            timeout=0.8,
            last_run_path=last_run,
        )

        assert code == 0
        data = json.loads(last_run.read_text(encoding="utf-8"))
        assert data["ok"] is False
        assert data["reason"] == "phase2_not_enabled"


class TestHalfWriteRecovery:
    def test_half_write_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critical atomicity test: crash during os.replace leaves old .preview intact."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        # Pre-existing valid preview with known content
        preview_path = memory_dir / "MEMORY.md.preview"
        preview_path.write_text("OLD CONTENT", encoding="utf-8")

        token_file = tmp_path / "token"
        token_file.write_text("test-token", encoding="utf-8")
        last_run = tmp_path / "last-run.json"

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        replace_call_count = 0
        real_replace = os.replace

        def crashing_replace(src, dst):
            nonlocal replace_call_count
            replace_call_count += 1
            if replace_call_count == 1:
                raise OSError("simulated crash")
            return real_replace(src, dst)

        payload = {
            "memory_md": "NEW CONTENT",
            "companion_files": {},
        }

        with patch.object(urllib.request, "urlopen", return_value=_fake_response(payload)):
            with patch("os.replace", side_effect=crashing_replace):
                code = regen.main(
                    memory_dir=memory_dir,
                    token_path=token_file,
                    log_path=tmp_path / "diff.log",
                    url="http://localhost:8765/export/memory_md",
                    timeout=0.8,
                    last_run_path=last_run,
                )

        assert code == 0
        # Old content must be preserved — NOT replaced by NEW CONTENT
        assert preview_path.read_text(encoding="utf-8") == "OLD CONTENT"
