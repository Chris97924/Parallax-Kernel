"""Backward-compat fixture test: v0.4 claim passes v0.3 validator zero-error.

Locks the additive-only invariant from
``Aphelion-Graph/spec/v0.3-claim-semantics.md`` — v0.4 introduces only
forward-compatible optional fields, so a v0.4-shaped claim MUST round-trip
through the v0.3 validator without raising any ``SchemaError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from aphelion.v03_validator import validate_v03_fields

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "v04_to_v03_backward_compat"


@pytest.fixture()
def v04_claim_frontmatter() -> dict[str, Any]:
    """Load the v0.4-shaped frontmatter as a parsed mapping."""
    raw = (FIXTURE_DIR / "claim_v04_simple.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict), "fixture must parse to a top-level mapping"
    return parsed


def test_v04_claim_passes_v03_validator(v04_claim_frontmatter: dict[str, Any]) -> None:
    """v0.4 frontmatter passes ``validate_v03_fields`` without raising."""
    validate_v03_fields(v04_claim_frontmatter)


def test_fixture_includes_v04_only_key(v04_claim_frontmatter: dict[str, Any]) -> None:
    """Sanity-check that the fixture is genuinely a v0.4 superset of v0.3."""
    assert "v04_optional_metadata" in v04_claim_frontmatter
