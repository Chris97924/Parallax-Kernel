"""Production-safety guards introduced in v0.6.1.

Two boundaries are pinned here:

* ``/metrics`` is auth-gated whenever a token mode is configured, unless
  the operator explicitly opts in to ``PARALLAX_METRICS_PUBLIC=1``.
  Without these tests, the previous "always unauthenticated" behaviour
  could quietly leak ingest cadence + retrieve volume + shadow
  discrepancy rate to anyone who can reach the listener.
* ``assert_safe_to_start()`` refuses to construct the FastAPI app when
  ``PARALLAX_BIND_HOST`` is non-loopback and no auth is configured.
  This stops a misconfigured PM2 ecosystem file or `--host 0.0.0.0`
  from silently exposing an open kernel.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from parallax.migrations import migrate_to_latest
from parallax.server.app import create_app
from parallax.server.auth import assert_safe_to_start, bind_host_is_safe
from parallax.sqlite_store import connect


@pytest.fixture()
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "safety.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


def _make_app(db_path: pathlib.Path) -> FastAPI:
    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


# ---------------------------------------------------------------------------
# bind_host_is_safe primitive
# ---------------------------------------------------------------------------


class TestBindHostIsSafe:
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "localhost", "::1", "[::1]", "", None, "LocalHost"]
    )
    def test_loopback_variants_all_safe(self, host: str | None) -> None:
        assert bind_host_is_safe(host) is True

    @pytest.mark.parametrize(
        "host", ["0.0.0.0", "192.168.0.10", "parallax.example.com", "::"]
    )
    def test_public_variants_all_unsafe(self, host: str) -> None:
        assert bind_host_is_safe(host) is False


# ---------------------------------------------------------------------------
# assert_safe_to_start
# ---------------------------------------------------------------------------


class TestAssertSafeToStart:
    def test_default_loopback_is_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PARALLAX_BIND_HOST", raising=False)
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        assert_safe_to_start()  # no raise

    def test_public_bind_with_token_is_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("PARALLAX_TOKEN", "secret")
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        assert_safe_to_start()

    def test_public_bind_with_multi_user_is_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        assert_safe_to_start()

    def test_public_bind_no_auth_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        with pytest.raises(RuntimeError, match="refusing to start"):
            assert_safe_to_start()

    def test_explicit_override_unblocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PARALLAX_ALLOW_OPEN_PUBLIC=1 is the documented escape hatch."""
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.setenv("PARALLAX_ALLOW_OPEN_PUBLIC", "1")
        assert_safe_to_start()

    def test_create_app_invokes_safety_check(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        with pytest.raises(RuntimeError, match="refusing to start"):
            _make_app(db_path)


# ---------------------------------------------------------------------------
# /metrics auth posture — the route should fail closed when a token is set.
# ---------------------------------------------------------------------------


class TestMetricsAuthPosture:
    """All cases scrub PARALLAX_BIND_HOST + PARALLAX_ALLOW_OPEN_PUBLIC so a
    test that earlier set them (or pollution from another suite) cannot
    smuggle the safety check into a state that flips _make_app's behaviour.
    The /metrics auth posture is independent of bind-host config and these
    tests must observe that independence."""

    def _scrub_bind_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PARALLAX_BIND_HOST", raising=False)
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)

    def test_metrics_open_when_no_auth(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._scrub_bind_env(monkeypatch)
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_METRICS_PUBLIC", raising=False)

        app = _make_app(db_path)
        with TestClient(app) as client:
            resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_requires_auth_when_token_set(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._scrub_bind_env(monkeypatch)
        monkeypatch.setenv("PARALLAX_TOKEN", "secret")
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_METRICS_PUBLIC", raising=False)

        app = _make_app(db_path)
        with TestClient(app) as client:
            anon = client.get("/metrics")
            authed = client.get(
                "/metrics", headers={"Authorization": "Bearer secret"}
            )
        assert anon.status_code == 401
        assert authed.status_code == 200

    def test_metrics_public_override_keeps_route_open_with_token(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._scrub_bind_env(monkeypatch)
        monkeypatch.setenv("PARALLAX_TOKEN", "secret")
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.setenv("PARALLAX_METRICS_PUBLIC", "1")

        app = _make_app(db_path)
        with TestClient(app) as client:
            resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_public_override_accepts_anonymous_AND_wrong_token(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The override must genuinely bypass auth, not just absorb missing
        headers — verify a wrong token still gets 200 (i.e. require_auth is
        not being silently invoked behind the scenes)."""
        self._scrub_bind_env(monkeypatch)
        monkeypatch.setenv("PARALLAX_TOKEN", "secret")
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.setenv("PARALLAX_METRICS_PUBLIC", "1")

        app = _make_app(db_path)
        with TestClient(app) as client:
            wrong = client.get(
                "/metrics", headers={"Authorization": "Bearer wrong-token"}
            )
            anon = client.get("/metrics")
        assert wrong.status_code == 200
        assert anon.status_code == 200

    def test_wrong_token_rejected(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._scrub_bind_env(monkeypatch)
        monkeypatch.setenv("PARALLAX_TOKEN", "secret")
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_METRICS_PUBLIC", raising=False)

        app = _make_app(db_path)
        with TestClient(app) as client:
            resp = client.get(
                "/metrics", headers={"Authorization": "Bearer wrong"}
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# `parallax serve` CLI binding-host pin
# ---------------------------------------------------------------------------


class TestServeCliPinsBindHost:
    """The `parallax serve` subcommand must pin PARALLAX_BIND_HOST to the
    same --host it hands uvicorn, so assert_safe_to_start sees the real
    bind address. Direct `uvicorn` invocation (without going through the
    CLI) does not get this pin — that's the operator's responsibility,
    documented in pm2/ecosystem.config.js and README."""

    def test_serve_sets_bind_host_env_to_match_host_arg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_uvicorn_run(
            target: str,
            *,
            host: str,
            port: int,
            log_level: str,
            reload: bool,
        ) -> None:
            captured["target"] = target
            captured["host"] = host
            captured["port"] = port
            captured["bind_host_env"] = os.environ.get("PARALLAX_BIND_HOST")

        # Inject a fake uvicorn module so _cmd_serve doesn't actually start a
        # server. The lazy import means the real uvicorn isn't required.
        import sys as _sys
        import types as _types

        fake_mod = _types.ModuleType("uvicorn")
        fake_mod.run = fake_uvicorn_run  # type: ignore[attr-defined]
        monkeypatch.setitem(_sys.modules, "uvicorn", fake_mod)
        monkeypatch.delenv("PARALLAX_BIND_HOST", raising=False)

        from parallax.cli import _cmd_serve

        # _cmd_serve mutates real os.environ via setdefault; monkeypatch only
        # tracks vars it set itself, so we explicitly scrub on the way out to
        # keep the env clean for downstream tests.
        try:
            rc = _cmd_serve(host="0.0.0.0", port=9001, log_level="info", reload=False)
            assert rc == 0
            assert captured["target"] == "parallax.server.app:app"
            assert captured["host"] == "0.0.0.0"
            assert captured["port"] == 9001
            assert captured["bind_host_env"] == "0.0.0.0"
        finally:
            os.environ.pop("PARALLAX_BIND_HOST", None)

    def test_serve_preserves_explicit_operator_bind_host_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the operator already set PARALLAX_BIND_HOST (e.g. via systemd
        EnvironmentFile), don't silently overwrite it. setdefault preserves
        the explicit value and we warn on stderr."""
        captured: dict[str, object] = {}

        def fake_uvicorn_run(
            target: str, *, host: str, port: int, log_level: str, reload: bool
        ) -> None:
            captured["bind_host_env"] = os.environ.get("PARALLAX_BIND_HOST")

        import sys as _sys
        import types as _types

        fake_mod = _types.ModuleType("uvicorn")
        fake_mod.run = fake_uvicorn_run  # type: ignore[attr-defined]
        monkeypatch.setitem(_sys.modules, "uvicorn", fake_mod)
        monkeypatch.setenv("PARALLAX_BIND_HOST", "192.168.1.42")

        from parallax.cli import _cmd_serve

        rc = _cmd_serve(host="0.0.0.0", port=9001, log_level="info", reload=False)
        assert rc == 0
        # operator-supplied env wins; CLI logs a warning to stderr (not
        # asserted here — the contract is "do not silently overwrite").
        # monkeypatch.setenv tracks this var, so teardown restores cleanly.
        assert captured["bind_host_env"] == "192.168.1.42"
