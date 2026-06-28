"""Regression tests for parallax.extract.providers._parse.parse_claims_json.

This is the shared, load-bearing parser both LLM providers
(``OpenRouterProvider`` and ``ClaudeSubprocessProvider``) feed raw model
output into. It must NEVER raise and silently filters every malformed,
out-of-shape, or sub-threshold item. Each test below locks in one
documented silent-filter branch and asserts the resulting ``RawClaim``
field *values* (not just counts) so the guard has teeth: remove the
guard in production and the matching test goes red.
"""

from __future__ import annotations

import json

from parallax.extract.providers._parse import MIN_CONFIDENCE, parse_claims_json
from parallax.extract.types import RawClaim


def _item(**overrides: object) -> dict[str, object]:
    """A fully-valid claim dict; override individual fields per test."""
    base: dict[str, object] = {
        "entity": "Acme",
        "claim_text": "ships fast",
        "polarity": 1,
        "confidence": 0.9,
        "claim_type": "feature",
        "evidence": "the changelog says so",
    }
    base.update(overrides)
    return base


def _dump(*items: object) -> str:
    return json.dumps(list(items))


class TestHappyPath:
    def test_valid_item_maps_every_field(self) -> None:
        out = parse_claims_json(_dump(_item()))
        assert out == [
            RawClaim(
                entity="Acme",
                claim_text="ships fast",
                polarity=1,
                confidence=0.9,
                claim_type="feature",
                evidence="the changelog says so",
            )
        ]

    def test_defaults_applied_for_missing_optional_fields(self) -> None:
        # Only confidence is required to clear the floor; everything else
        # falls back to its documented default.
        out = parse_claims_json(_dump({"confidence": 0.8}))
        assert len(out) == 1
        c = out[0]
        assert c.entity == "Unknown"
        assert c.claim_text == ""
        assert c.polarity == 0
        assert c.confidence == 0.8
        assert c.claim_type == "opinion"
        assert c.evidence == ""


class TestFencedCodeStripping:
    def test_json_fence_is_stripped(self) -> None:
        raw = "```json\n" + _dump(_item(entity="Beta")) + "\n```"
        out = parse_claims_json(raw)
        assert len(out) == 1
        assert out[0].entity == "Beta"

    def test_bare_triple_backtick_fence_is_stripped(self) -> None:
        raw = "```\n" + _dump(_item(entity="Gamma")) + "\n```"
        out = parse_claims_json(raw)
        assert len(out) == 1
        assert out[0].entity == "Gamma"

    def test_leading_whitespace_before_fence_still_stripped(self) -> None:
        raw = "   ```json\n" + _dump(_item(entity="Delta")) + "\n```"
        out = parse_claims_json(raw)
        assert len(out) == 1
        assert out[0].entity == "Delta"

    def test_unfenced_json_parses(self) -> None:
        # Exercises the False side of the ``startswith("```")`` branch.
        out = parse_claims_json(_dump(_item(entity="NoFence")))
        assert [c.entity for c in out] == ["NoFence"]


class TestConfidenceFloor:
    def test_below_floor_dropped(self) -> None:
        out = parse_claims_json(_dump(_item(confidence=0.49)))
        assert out == []

    def test_at_floor_kept(self) -> None:
        # Boundary: confidence == MIN_CONFIDENCE must survive (>= , not >).
        out = parse_claims_json(_dump(_item(confidence=MIN_CONFIDENCE)))
        assert len(out) == 1
        assert out[0].confidence == MIN_CONFIDENCE

    def test_just_above_floor_kept(self) -> None:
        out = parse_claims_json(_dump(_item(confidence=0.5001)))
        assert len(out) == 1
        assert out[0].confidence == 0.5001

    def test_missing_confidence_defaults_to_zero_and_dropped(self) -> None:
        out = parse_claims_json(_dump({"entity": "X", "claim_text": "y"}))
        assert out == []

    def test_floor_filters_only_the_weak_item(self) -> None:
        out = parse_claims_json(
            _dump(
                _item(entity="weak", confidence=0.10),
                _item(entity="strong", confidence=0.95),
            )
        )
        assert [c.entity for c in out] == ["strong"]


class TestPolarityClamp:
    def test_out_of_range_positive_clamped_to_zero(self) -> None:
        out = parse_claims_json(_dump(_item(polarity=5)))
        assert len(out) == 1
        assert out[0].polarity == 0

    def test_out_of_range_negative_clamped_to_zero(self) -> None:
        out = parse_claims_json(_dump(_item(polarity=-7)))
        assert len(out) == 1
        assert out[0].polarity == 0

    def test_valid_polarities_preserved(self) -> None:
        for p in (-1, 0, 1):
            out = parse_claims_json(_dump(_item(polarity=p)))
            assert len(out) == 1, f"polarity {p} should survive"
            assert out[0].polarity == p


class TestEvidenceTruncation:
    def test_truncated_to_200_chars(self) -> None:
        long_evidence = "x" * 250
        out = parse_claims_json(_dump(_item(evidence=long_evidence)))
        assert len(out) == 1
        assert len(out[0].evidence) == 200
        assert out[0].evidence == "x" * 200

    def test_short_evidence_untouched(self) -> None:
        out = parse_claims_json(_dump(_item(evidence="brief")))
        assert out[0].evidence == "brief"

    def test_evidence_stripped_before_truncation(self) -> None:
        out = parse_claims_json(_dump(_item(evidence="   padded   ")))
        assert out[0].evidence == "padded"


class TestStringStripping:
    def test_entity_stripped(self) -> None:
        out = parse_claims_json(_dump(_item(entity="  Spaced Co  ")))
        assert out[0].entity == "Spaced Co"

    def test_claim_text_stripped(self) -> None:
        out = parse_claims_json(_dump(_item(claim_text="\n  wraps  \t")))
        assert out[0].claim_text == "wraps"

    def test_claim_type_stripped(self) -> None:
        out = parse_claims_json(_dump(_item(claim_type="  opinion  ")))
        assert out[0].claim_type == "opinion"


class TestShapeRejection:
    def test_non_list_dict_returns_empty(self) -> None:
        assert parse_claims_json(json.dumps({"confidence": 0.9})) == []

    def test_non_list_scalar_returns_empty(self) -> None:
        assert parse_claims_json("42") == []
        assert parse_claims_json('"a string"') == []

    def test_non_dict_array_items_skipped(self) -> None:
        raw = json.dumps([1, "two", None, [3], _item(entity="OnlyValid")])
        out = parse_claims_json(raw)
        assert [c.entity for c in out] == ["OnlyValid"]


class TestMalformedJson:
    def test_garbage_returns_empty(self) -> None:
        assert parse_claims_json("not json at all") == []

    def test_empty_string_returns_empty(self) -> None:
        assert parse_claims_json("") == []

    def test_truncated_json_returns_empty(self) -> None:
        assert parse_claims_json('[{"entity": "X"') == []

    def test_does_not_raise_on_malformed(self) -> None:
        # Documented contract: never raises.
        for bad in ("", "{", "[", "][", "null-ish", "```\n```"):
            assert parse_claims_json(bad) == []


class TestPerItemTypeErrors:
    def test_non_numeric_confidence_skips_item(self) -> None:
        out = parse_claims_json(_dump(_item(confidence="abc")))
        assert out == []

    def test_non_numeric_polarity_skips_item(self) -> None:
        # confidence clears the floor, then int("nope") raises ValueError.
        out = parse_claims_json(_dump(_item(confidence=0.9, polarity="nope")))
        assert out == []

    def test_bad_item_does_not_poison_following_valid_item(self) -> None:
        out = parse_claims_json(
            _dump(
                _item(entity="bad", confidence="oops"),
                _item(entity="good", confidence=0.9),
            )
        )
        assert [c.entity for c in out] == ["good"]

    def test_numeric_string_confidence_is_coerced_and_kept(self) -> None:
        # float("0.7") succeeds, so this item survives the floor.
        out = parse_claims_json(_dump(_item(confidence="0.7")))
        assert len(out) == 1
        assert out[0].confidence == 0.7
