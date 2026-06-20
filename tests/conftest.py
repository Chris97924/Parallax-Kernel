"""Shared pytest fixtures for the Parallax test suite."""

from __future__ import annotations

import pathlib
import sqlite3
import sys
from collections.abc import Iterator

import pytest

from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect

# Files that, run in isolation, legitimately cannot reach the 80% package-wide
# coverage gate — either they cover an out-of-package script (test_regenerate)
# or they are a focused single-feature harness whose value is the assertions,
# not breadth (the M8 hybrid-vs-lexical quality harness). The gate is a
# full-suite contract; relaxing it only when ONE of these is run alone keeps
# the exact per-file runner command green without weakening the suite gate.
_ISOLATION_COV_RELAXED = (
    "test_regenerate",
    "test_hybrid_vs_lexical_quality",
)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Lower cov-fail-under when running only a coverage-exempt file in isolation.

    Some files contribute little or nothing to the parallax package coverage
    total (an external script, or a focused single-feature harness), so running
    them alone would always fail the 80% gate. When the invocation targets only
    such a file, zero the gate.

    pytest-cov reads ``CovPlugin.options.cov_fail_under`` (not
    ``config.option``) at ``pytest_terminal_summary`` time, so we reach
    into the registered plugin instance and zero it out.
    """
    args = sys.argv[1:]
    positional = [a for a in args if not a.startswith("-")]
    relaxed_only = bool(positional) and all(
        any(marker in a for marker in _ISOLATION_COV_RELAXED) for a in positional
    )
    if not relaxed_only:
        return
    # Patch config.option (used by some pytest-cov paths)
    try:
        session.config.option.cov_fail_under = 0.0
    except AttributeError:
        pass
    # Patch the CovPlugin instance directly (used by pytest_terminal_summary)
    plugin = session.config.pluginmanager.get_plugin("_cov")
    if plugin is not None:
        try:
            plugin.options.cov_fail_under = 0.0
        except AttributeError:
            pass


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """Fresh SQLite connection with all migrations applied."""
    db = tmp_path / "parallax.db"
    c = connect(db)
    migrate_to_latest(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _audit_db_path_env(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``PARALLAX_AUDIT_DB_PATH`` at a per-test tmp ``audit.db``.

    The Apex M5 server lifespan (:func:`parallax.server.lifespan.parallax_lifespan`)
    validates ``PARALLAX_AUDIT_DB_PATH`` at startup and refuses to boot
    without it (audit-db-path-config.md §4). This autouse fixture gives
    every test that spins up the app (``TestClient`` / ``create_app``) a
    valid, writable, absolute audit path so the boot gate passes
    transparently. Tests that specifically exercise the boot-fail path
    ``monkeypatch.delenv`` it inside the test body (later monkeypatch wins).
    """
    monkeypatch.setenv("PARALLAX_AUDIT_DB_PATH", str(tmp_path / "audit.db"))
