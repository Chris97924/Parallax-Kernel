"""Unit tests for plugins/parallax-session-hook/hook.py.

The hook is stdlib-only on purpose — these tests exercise its failure
modes (server down, bad JSON, auth mismatch) to prove it never blocks a
Claude session.
"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

_HOOK_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "plugins"
    / "parallax-session-hook"
    / "hook.py"
)


def _load_hook_module() -> Any:
    spec = importlib.util.spec_from_file_location("parallax_session_hook", _HOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hook_mod() -> Any:
    return _load_hook_module()


class _Handler(BaseHTTPRequestHandler):
    """Test server with pluggable response behaviour via class attrs."""

    response_body: bytes = (
        b'{"reminder": "<system-reminder>\\nhello\\n</system-reminder>", '
        b'"length": 40}'
    )
    response_status: int = 200
    require_auth: bool = False
    expected_token: str = "t0ken"
    last_path: str = ""

    # silence stderr log spam during tests
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        type(self).last_path = self.path
        if self.require_auth:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {self.expected_token}":
                self.send_response(401)
                self.end_headers()
                return
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)


@contextmanager
def _serve(handler_cls: type[BaseHTTPRequestHandler]) -> Any:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture()
def handler() -> type[_Handler]:
    """Fresh handler subclass per test so attribute mutations don't leak."""
    return type("Handler", (_Handler,), {})


def _run_hook(
    hook_mod: Any,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    code = hook_mod.main()
    return code, stdout.getvalue(), stderr.getvalue()


class TestHookHappyPath:
    def test_prints_reminder_and_exits_zero(
        self,
        hook_mod: Any,
        handler: type[_Handler],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with _serve(handler) as (host, port):
            code, out, _ = _run_hook(
                hook_mod,
                {"PARALLAX_API_URL": f"http://{host}:{port}", "PARALLAX_USER_ID": "u"},
                monkeypatch,
            )
        assert code == 0
        assert "<system-reminder>" in out
        assert "hello" in out

    def test_forwards_user_id_as_query_param(
        self,
        hook_mod: Any,
        handler: type[_Handler],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with _serve(handler) as (host, port):
            _run_hook(
                hook_mod,
                {"PARALLAX_API_URL": f"http://{host}:{port}", "PARALLAX_USER_ID": "chris475604"},
                monkeypatch,
            )
        assert "user_id=chris475604" in handler.last_path


class TestHookFailSafe:
    def test_server_down_exits_zero_silently(
        self, hook_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Port 1 is reserved — never reachable.
        code, out, err = _run_hook(
            hook_mod,
            {"PARALLAX_API_URL": "http://127.0.0.1:1", "PARALLAX_HOOK_TIMEOUT": "0.5"},
            monkeypatch,
        )
        assert code == 0
        assert out == ""
        # Silent by default (no debug flag).
        assert err == ""

    def test_server_down_debug_logs_stderr(
        self, hook_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out, err = _run_hook(
            hook_mod,
            {
                "PARALLAX_API_URL": "http://127.0.0.1:1",
                "PARALLAX_HOOK_TIMEOUT": "0.5",
                "PARALLAX_HOOK_DEBUG": "1",
            },
            monkeypatch,
        )
        assert code == 0
        assert out == ""
        assert "parallax-session-hook" in err

    def test_auth_mismatch_exits_zero(
        self,
        hook_mod: Any,
        handler: type[_Handler],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler.require_auth = True
        with _serve(handler) as (host, port):
            code, out, _ = _run_hook(
                hook_mod,
                {
                    "PARALLAX_API_URL": f"http://{host}:{port}",
                    "PARALLAX_TOKEN": "wrong-token",
                },
                monkeypatch,
            )
        assert code == 0
        assert out == ""

    def test_auth_success_injects_header(
        self,
        hook_mod: Any,
        handler: type[_Handler],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler.require_auth = True
        with _serve(handler) as (host, port):
            code, out, _ = _run_hook(
                hook_mod,
                {
                    "PARALLAX_API_URL": f"http://{host}:{port}",
                    "PARALLAX_TOKEN": "t0ken",
                },
                monkeypatch,
            )
        assert code == 0
        assert "<system-reminder>" in out

    def test_bad_json_exits_zero(
        self,
        hook_mod: Any,
        handler: type[_Handler],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler.response_body = b"not-json"
        with _serve(handler) as (host, port):
            code, out, _ = _run_hook(
                hook_mod,
                {"PARALLAX_API_URL": f"http://{host}:{port}"},
                monkeypatch,
            )
        assert code == 0
        assert out == ""

    def test_empty_reminder_field_no_output(
        self,
        hook_mod: Any,
        handler: type[_Handler],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler.response_body = json.dumps({"reminder": "", "length": 0}).encode()
        with _serve(handler) as (host, port):
            code, out, _ = _run_hook(
                hook_mod,
                {"PARALLAX_API_URL": f"http://{host}:{port}"},
                monkeypatch,
            )
        assert code == 0
        assert out == ""


class TestHookUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://attacker.example.com/",
            "gopher://evil/",
            "not-a-url",
            "",
        ],
    )
    def test_rejects_unsafe_scheme_silently(
        self, hook_mod: Any, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        code, out, _err = _run_hook(
            hook_mod,
            {"PARALLAX_API_URL": url, "PARALLAX_TOKEN": "secret"},
            monkeypatch,
        )
        assert code == 0
        assert out == ""

    def test_rejects_unsafe_scheme_debug_stderr(
        self, hook_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, _out, err = _run_hook(
            hook_mod,
            {
                "PARALLAX_API_URL": "file:///etc/passwd",
                "PARALLAX_TOKEN": "secret-should-not-leave",
                "PARALLAX_HOOK_DEBUG": "1",
            },
            monkeypatch,
        )
        assert code == 0
        assert "unsafe" in err.lower()
        # The token must never appear in logs either.
        assert "secret-should-not-leave" not in err
