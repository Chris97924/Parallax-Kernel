"""Tests for the Parallax SessionStart memory-regeneration hook script.

Imports the script via importlib to avoid adding it to the package
(it lives outside the Parallax repo).
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
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


# Load once at collection time so fixtures can reference it
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDryRunWritesPreviewOnly:
    def test_preview_written_live_untouched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        # Put a live MEMORY.md in place — must NOT be modified
        live = memory_dir / "MEMORY.md"
        live.write_text("original content", encoding="utf-8")
        original_mtime = live.stat().st_mtime

        token_file = tmp_path / "token"
        token_file.write_text("test-token", encoding="utf-8")
        log_path = tmp_path / "diff.log"

        payload = {
            "memory_md": "# New Memory\nsome content",
            "companion_files": {"companion.md": "companion body"},
        }

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        with patch.object(urllib.request, "urlopen", return_value=_fake_response(payload)):
            code = regen.main(
                memory_dir=memory_dir,
                token_path=token_file,
                log_path=log_path,
                url="http://localhost:8765/export/memory_md",
                timeout=0.8,
            )

        assert code == 0
        # Preview files written
        assert (memory_dir / "MEMORY.md.preview").exists()
        assert (memory_dir / "MEMORY.md.preview").read_text(encoding="utf-8") == "# New Memory\nsome content"
        assert (memory_dir / "companion.md.preview").exists()
        # Live MEMORY.md untouched
        assert live.read_text(encoding="utf-8") == "original content"
        assert live.stat().st_mtime == original_mtime


class TestFallbackWhenParallaxDown:
    def test_connection_refused_returns_0_no_preview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        token_file = tmp_path / "token"
        token_file.write_text("test-token", encoding="utf-8")
        log_path = tmp_path / "diff.log"

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        with patch.object(urllib.request, "urlopen", side_effect=ConnectionRefusedError()):
            code = regen.main(
                memory_dir=memory_dir,
                token_path=token_file,
                log_path=log_path,
                url="http://localhost:8765/export/memory_md",
                timeout=0.8,
            )

        assert code == 0
        assert not list(memory_dir.glob("*.preview"))

    def test_no_stdout_on_connection_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("tok", encoding="utf-8")

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        with patch.object(urllib.request, "urlopen", side_effect=ConnectionRefusedError()):
            regen.main(
                memory_dir=tmp_path / "mem",
                token_path=token_file,
                log_path=tmp_path / "diff.log",
                url="http://localhost:8765/export/memory_md",
                timeout=0.8,
            )

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestTimeoutUsesOldFile:
    def test_socket_timeout_returns_0_no_preview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        token_file = tmp_path / "token"
        token_file.write_text("test-token", encoding="utf-8")

        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        with patch.object(urllib.request, "urlopen", side_effect=TimeoutError()):
            code = regen.main(
                memory_dir=memory_dir,
                token_path=token_file,
                log_path=tmp_path / "diff.log",
                url="http://localhost:8765/export/memory_md",
                timeout=0.8,
            )

        assert code == 0
        assert not list(memory_dir.glob("*.preview"))


class TestMissingTokenFileSilent:
    def test_nonexistent_token_returns_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")
        code = regen.main(
            memory_dir=tmp_path / "memory",
            token_path=tmp_path / "nonexistent_token_file",
            log_path=tmp_path / "diff.log",
            url="http://localhost:8765/export/memory_md",
            timeout=0.8,
        )
        assert code == 0

    def test_no_output_on_missing_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")
        regen.main(
            memory_dir=tmp_path / "memory",
            token_path=tmp_path / "no_such_file",
            log_path=tmp_path / "diff.log",
            url="http://localhost:8765/export/memory_md",
            timeout=0.8,
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestDiffLogRotation:
    def test_rotate_when_over_limit(self, tmp_path: Path) -> None:
        log_path = tmp_path / "diff.log"
        # Write 1.1 MiB of junk
        junk = "x" * (1024 * 1024 + 100 * 1024)  # 1.1 MiB
        log_path.write_text(junk, encoding="utf-8")

        assert os.path.getsize(log_path) > 1048576

        regen.rotate_log_if_needed(log_path, max_bytes=1048576)

        assert os.path.getsize(log_path) < 1048576

    def test_no_rotation_under_limit(self, tmp_path: Path) -> None:
        log_path = tmp_path / "diff.log"
        log_path.write_text("small content", encoding="utf-8")
        original_size = os.path.getsize(log_path)

        regen.rotate_log_if_needed(log_path, max_bytes=1048576)

        assert os.path.getsize(log_path) == original_size

    def test_rotation_then_append_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """After rotation, a subsequent run can still append to diff.log."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        token_file = tmp_path / "token"
        token_file.write_text("tok", encoding="utf-8")
        log_path = tmp_path / "diff.log"

        # Pre-fill log to > 1 MiB
        log_path.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")

        payload = {"memory_md": "new content", "companion_files": {}}
        monkeypatch.setenv("PARALLAX_REGEN_DRY_RUN", "1")

        with patch.object(urllib.request, "urlopen", return_value=_fake_response(payload)):
            code = regen.main(
                memory_dir=memory_dir,
                token_path=token_file,
                log_path=log_path,
                url="http://localhost:8765/export/memory_md",
                timeout=0.8,
            )

        assert code == 0
        # Log was rotated and new diff header was appended
        content = log_path.read_text(encoding="utf-8")
        assert "MEMORY.md" in content
        assert len(content) < 1048576
