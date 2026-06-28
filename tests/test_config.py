"""RED-phase tests for parallax.config.

Contract:
    ParallaxConfig is a frozen dataclass with absolute pathlib.Path fields
    (db_path, vault_path, schema_path). load_config() reads env vars
    PARALLAX_DB_PATH / PARALLAX_VAULT_PATH / PARALLAX_SCHEMA_PATH and falls
    back to project-root defaults.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from parallax.config import ParallaxConfig, load_config

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestParallaxConfig:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(ParallaxConfig)
        params = ParallaxConfig.__dataclass_params__
        assert params.frozen is True

    def test_has_expected_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(ParallaxConfig)}
        assert {"db_path", "vault_path", "schema_path"}.issubset(names)

    def test_cannot_mutate_fields(self, tmp_path: pathlib.Path) -> None:
        cfg = ParallaxConfig(
            db_path=tmp_path / "db.sqlite",
            vault_path=tmp_path / "vault",
            schema_path=tmp_path / "schema.sql",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.db_path = tmp_path / "other.sqlite"  # type: ignore[misc]


class TestLoadConfig:
    def test_defaults_are_absolute_under_project_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in ("PARALLAX_DB_PATH", "PARALLAX_VAULT_PATH", "PARALLAX_SCHEMA_PATH"):
            monkeypatch.delenv(key, raising=False)
        cfg = load_config()
        assert cfg.db_path.is_absolute()
        assert cfg.vault_path.is_absolute()
        assert cfg.schema_path.is_absolute()
        # Defaults point inside the project root
        assert PROJECT_ROOT in cfg.db_path.parents or cfg.db_path.parent == PROJECT_ROOT / "db"
        assert (
            cfg.vault_path == (PROJECT_ROOT / "vault").resolve()
            or cfg.vault_path.parts[-1] == "vault"
        )
        assert cfg.schema_path.name == "schema.sql"

    def test_env_overrides_applied(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "custom" / "x.db"
        vault = tmp_path / "custom" / "vault"
        schema = tmp_path / "custom" / "schema.sql"
        monkeypatch.setenv("PARALLAX_DB_PATH", str(db))
        monkeypatch.setenv("PARALLAX_VAULT_PATH", str(vault))
        monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(schema))
        cfg = load_config()
        assert cfg.db_path == db.resolve()
        assert cfg.vault_path == vault.resolve()
        assert cfg.schema_path == schema.resolve()

    def test_returns_parallax_config_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in ("PARALLAX_DB_PATH", "PARALLAX_VAULT_PATH", "PARALLAX_SCHEMA_PATH"):
            monkeypatch.delenv(key, raising=False)
        cfg = load_config()
        assert isinstance(cfg, ParallaxConfig)

    def test_relative_env_paths_become_absolute(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PARALLAX_DB_PATH", "relative/db.sqlite")
        monkeypatch.delenv("PARALLAX_VAULT_PATH", raising=False)
        monkeypatch.delenv("PARALLAX_SCHEMA_PATH", raising=False)
        cfg = load_config()
        assert cfg.db_path.is_absolute()
        assert cfg.db_path == (tmp_path / "relative" / "db.sqlite").resolve()


class TestShadowConfig:
    """Lane C v0.2.0-beta WS-3 — shadow flag plumbing through ParallaxConfig.

    Per the runbook ``Post-merge Enablement`` section, shadow is gated by
    three env vars: ``SHADOW_MODE`` / ``SHADOW_USER_ALLOWLIST`` / ``SHADOW_LOG_DIR``.
    The first two are read per-request inside ``parallax.router.shadow`` for
    hot-flip semantics; ``ParallaxConfig`` exposes the same values for the
    ``/metrics`` endpoint and the continuity-check CLI.
    """

    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in ("SHADOW_MODE", "SHADOW_USER_ALLOWLIST", "SHADOW_LOG_DIR"):
            monkeypatch.delenv(key, raising=False)
        cfg = load_config()
        assert cfg.shadow_mode is False
        assert cfg.shadow_user_allowlist == ()
        assert cfg.shadow_log_dir.name == "logs"

    def test_shadow_mode_truthy_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for raw in ("true", "True", "TRUE", "1", "yes"):
            monkeypatch.setenv("SHADOW_MODE", raw)
            cfg = load_config()
            assert cfg.shadow_mode is True, raw

    def test_shadow_mode_falsy_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for raw in ("false", "False", "0", "no", ""):
            monkeypatch.setenv("SHADOW_MODE", raw)
            cfg = load_config()
            assert cfg.shadow_mode is False, raw

    def test_shadow_user_allowlist_parses_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "alice,bob , charlie")
        cfg = load_config()
        assert cfg.shadow_user_allowlist == ("alice", "bob", "charlie")

    def test_shadow_user_allowlist_empty_csv_is_empty_tuple(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHADOW_USER_ALLOWLIST", " , ,")
        cfg = load_config()
        assert cfg.shadow_user_allowlist == ()

    def test_shadow_log_dir_env_override(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom = tmp_path / "custom-shadow-logs"
        monkeypatch.setenv("SHADOW_LOG_DIR", str(custom))
        cfg = load_config()
        assert cfg.shadow_log_dir == custom.resolve()
        assert cfg.shadow_log_dir.is_absolute()
