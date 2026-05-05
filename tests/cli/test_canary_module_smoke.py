"""Smoke tests so ``pytest tests/cli/`` collects something (AC X.3).

The full canary CLI surface lands with US-009.3; until then this file
just verifies the canary package imports cleanly so the X.3 verification
gate has a non-empty pytest target.
"""

from __future__ import annotations


def test_parallax_cli_imports() -> None:
    import parallax.cli  # noqa: F401

    assert parallax.cli is not None


def test_canary_subpackage_imports() -> None:
    from parallax import canary  # noqa: F401

    assert canary is not None
