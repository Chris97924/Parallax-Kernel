"""Pytest config: ensure the project root is importable as a source tree."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _isolate_drain_journal(tmp_path_factory: pytest.TempPathFactory):
    """Keep the #106.3 drain journal out of the working tree.

    ``_drain_inflight`` persists a timeout to the path resolved by
    :func:`parallax.server.drain_journal.resolve_drain_journal_path`, which
    defaults to ``cwd``. Several suites drive that function directly, so without
    this every run would leave (and keep incrementing) a real journal file in
    the repo root — and the next run would restore a non-zero counter from it,
    coupling the suite to on-disk state that outlives it. Autouse and repo-wide
    because the default is process-cwd-relative: an opt-in fixture only protects
    the suites that remember to ask.

    Session-scoped so the whole run shares one temp path rather than minting a
    directory per test. Tests that assert on journal CONTENT pass an explicit
    path instead of relying on this; the tests that fall through to the env var
    only need somewhere harmless to write.

    The import is deferred into the body because this file is what puts the
    source tree on ``sys.path``, and a module-level ``parallax`` import here
    would run before the line above.
    """
    from parallax.server.drain_journal import DRAIN_JOURNAL_ENV

    journal = tmp_path_factory.mktemp("drain-journal") / "parallax_drain_journal.json"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(DRAIN_JOURNAL_ENV, str(journal))
        yield
