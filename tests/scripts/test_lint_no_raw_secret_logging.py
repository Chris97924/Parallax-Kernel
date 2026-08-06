"""Tests for the PLX-SECRET-LOG gate (``m5-entry-spec.md`` §3.1a).

The spec row calls for a *lint rule*, so the rule itself needs regression
cover — a gate that silently stops matching is worse than no gate, because the
spec would still claim it is enforced. Two fixtures pin both directions:

* ``tests/fixtures/lint_secret_logging/ok_redacted.py`` — every shape the rule
  must permit (placeholders, hashes, presence metadata, prose).
* ``tests/fixtures/lint_secret_logging/bad_authorization.py`` — every route a
  raw credential could take into a log or audit write.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "lint_no_raw_secret_logging.py"
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "lint_secret_logging"


def _load_module():
    """Import the checker by path — ``scripts/`` is not an installed package."""
    spec = importlib.util.spec_from_file_location("plx_secret_log_lint", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lint = _load_module()


def test_positive_fixture_passes() -> None:
    """Redaction, hashing, presence metadata and prose must all lint clean —
    a gate that flags the mitigation teaches authors to reword, not redact."""
    assert lint.check_file(_FIXTURES / "ok_redacted.py") == []


def test_violating_fixture_is_blocked() -> None:
    """Every credential route in the negative fixture is caught."""
    violations = lint.check_file(_FIXTURES / "bad_authorization.py")
    assert len(violations) == 8, "\n".join(violations)
    joined = "\n".join(violations)
    for expected in (
        "self._token",           # attribute, positional arg
        "self.headers['Authorization']",  # subscript into the header map
        "authorization",         # bare name in extra=
        "bearer_token",          # f-string interpolation
        "api_key",               # wrapped in str()
        "password",              # %-formatting arg
        "auth_header",           # audit_log.write() — the ledger half
        "secret_value",          # nested inside a dict/list literal
    ):
        assert expected in joined, f"{expected!r} not flagged:\n{joined}"
    assert "audit_log.write()" in joined


def test_suppression_comment_is_honoured() -> None:
    """A reviewed false positive can be waived in-line, and the waiver stays
    visible in the diff rather than weakening the pattern for everyone."""
    source = _FIXTURES / "ok_redacted.py"
    text = source.read_text(encoding="utf-8")
    assert lint.SUPPRESSION in text, "fixture must exercise the suppression path"
    assert lint.check_file(source) == []


@pytest.mark.parametrize("target", ["parallax", "scripts"])
def test_tracked_tree_is_clean(target: str) -> None:
    """The gate must be green on the tree it guards, or CI cannot adopt it."""
    files = lint.iter_python_files([_REPO_ROOT / target])
    assert files, f"expected python files under {target}/"
    violations = [v for path in files for v in lint.check_file(path)]
    assert violations == [], "\n".join(violations)


def test_cli_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    """The CI step depends on the exit code, so pin it explicitly."""
    assert lint.main([str(_FIXTURES / "ok_redacted.py")]) == 0
    assert lint.main([str(_FIXTURES / "bad_authorization.py")]) == 1
    captured = capsys.readouterr()
    assert lint.RULE_ID in captured.out
