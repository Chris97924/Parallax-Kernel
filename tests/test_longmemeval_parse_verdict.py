"""parse_verdict must fail loudly on empty / unparseable judge output.

Regression: on 2026-04-20 a Gemini-2.5-pro judge pass returned ``resp.text=""``
for every call (thinking tokens consumed the 256-token budget). The old
``parse_verdict`` defaulted to ``INCORRECT`` with empty reason, silently
corrupting 149 records across two 500Q runs. The fix is to raise on any
unparseable input so the caller records ``verdict='ERROR'`` instead.
"""

from __future__ import annotations

import pytest

from eval.longmemeval.pipeline import parse_verdict


class TestParseVerdictHappyPath:
    def test_correct_with_reason(self) -> None:
        assert parse_verdict("CORRECT\nparaphrases match") == (
            "CORRECT",
            "paraphrases match",
        )

    def test_incorrect_with_reason(self) -> None:
        assert parse_verdict("INCORRECT\nwrong fact") == ("INCORRECT", "wrong fact")

    def test_mixed_case_head(self) -> None:
        assert parse_verdict("correct\nok") == ("CORRECT", "ok")

    def test_no_reason_line(self) -> None:
        assert parse_verdict("CORRECT") == ("CORRECT", "")

    def test_leading_whitespace(self) -> None:
        assert parse_verdict("  INCORRECT\nwhy") == ("INCORRECT", "why")


class TestParseVerdictRaises:
    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_verdict("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_verdict("   \n  ")

    def test_unparseable_head_raises(self) -> None:
        with pytest.raises(ValueError, match="unparseable"):
            parse_verdict("MAYBE\nunsure")

    def test_empty_head_raises(self) -> None:
        with pytest.raises(ValueError, match="unparseable"):
            parse_verdict("\nreason without verdict")
