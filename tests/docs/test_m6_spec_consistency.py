"""Spec ↔ code consistency check for the M6 ingest implementation spec.

For every CLI flag / env var / error class / reason code present in
``parallax/apex/aphelion_ingest.py`` and ``parallax/cli.py`` (ingest
subparser only), assert that the symbol is mentioned somewhere in
``docs/m6-ingest/m6-ingest-impl-spec.md``. The runbook + fixture catalog
docs are not gated here — only the impl spec is the consistency target
(it's the doc that claims to enumerate the binary).

This test is the doc-PR consistency gate per the P2 backlog #3 retrofit.
If you change the CLI surface, env vars, error classes, or reason codes
you MUST update the impl spec or this test will fail.

The test is intentionally a single ``pytest`` file so it runs as part of
the normal suite (``pytest -k spec_consistency``) without needing a
separate harness.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Path anchors — resolved from this test file's location so the test works
# under pytest invocation from any CWD.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_IMPL_SPEC = _REPO_ROOT / "docs" / "m6-ingest" / "m6-ingest-impl-spec.md"
_INGEST_MODULE = _REPO_ROOT / "parallax" / "apex" / "aphelion_ingest.py"
_CLI_MODULE = _REPO_ROOT / "parallax" / "cli.py"


# ---------------------------------------------------------------------------
# Loaders — read once per test session; the files are small.
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    if not path.is_file():
        pytest.fail(f"required file missing for consistency check: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _extract_ingest_cli_flags(cli_source: str) -> list[str]:
    """Pull every argument name added under ``p_ingest.add_argument(...)``.

    The ingest subparser is bounded by ``# ----- ingest`` and the next
    top-level ``# -----`` section marker. ``cli.py``'s convention is that
    subparser-block markers sit at zero indent inside the parser-builder
    function; we search both indented and unindented to be robust against
    future reformatting.
    """
    start = cli_source.find("# ----- ingest")
    if start == -1:
        pytest.fail("could not locate '# ----- ingest' subparser section in cli.py")
    after = cli_source[start:]
    # Search for either a zero-indent or 4-space-indent next-section marker.
    next_unindented = after.find("\n# -----", 1)
    next_indented = after.find("\n    # -----", 1)
    candidates = [pos for pos in (next_unindented, next_indented) if pos != -1]
    end_relative = min(candidates) if candidates else after.find("return parser")
    if end_relative == -1:
        pytest.fail(
            "could not locate end-of-ingest-subparser marker (next '# -----' "
            "section header or 'return parser') in cli.py"
        )
    block = after[:end_relative]
    pattern = re.compile(r'p_ingest\.add_argument\(\s*["\']([^"\']+)["\']')
    return pattern.findall(block)


# Two explicit env-var access patterns. Keeping them separate avoids the
# false-positive risks of conflating subscript-vs-call syntax in one regex.
_ENV_SUBSCRIPT_RE = re.compile(
    r'os\.environ\[\s*["\'](PARALLAX_[A-Z_0-9]+)["\']\s*\]'
)
_ENV_GET_RE = re.compile(
    r'os\.environ\.get\(\s*["\'](PARALLAX_[A-Z_0-9]+)["\']'
)


def _extract_parallax_env_vars(*sources: str) -> set[str]:
    """Pull every ``os.environ["PARALLAX_..."]`` / ``os.environ.get("PARALLAX_...")``
    key from the provided source strings.

    Both subscript and ``.get`` access styles are supported so future
    rewrites (e.g. switching to ``.get`` with a default) don't silently
    drift past the consistency check.
    """
    found: set[str] = set()
    for src in sources:
        found.update(_ENV_SUBSCRIPT_RE.findall(src))
        found.update(_ENV_GET_RE.findall(src))
    return found


def _extract_error_classes(ingest_source: str) -> list[str]:
    """Pull every top-level ``class Foo...Error(...):`` definition name."""
    pattern = re.compile(r'^class\s+(\w+Error)\b', re.MULTILINE)
    return pattern.findall(ingest_source)


def _extract_reason_codes(ingest_source: str) -> list[str]:
    """Pull every reason_code key out of the ``_REASON_TO_EXIT`` mapping.

    The mapping lives at module top-level in ``aphelion_ingest.py``. We
    locate the literal ``_REASON_TO_EXIT`` assignment and consume up to
    the first dedented closing brace on its own line, so an inline dict
    or dataclass declaration inside the mapping value side cannot
    truncate extraction silently.
    """
    start = ingest_source.find("_REASON_TO_EXIT")
    if start == -1:
        pytest.fail(
            "could not locate _REASON_TO_EXIT mapping in aphelion_ingest.py — "
            "the consistency check depends on this canonical name"
        )
    block_match = re.search(
        r"_REASON_TO_EXIT[^=]*=\s*\{(.*?)^\}",
        ingest_source[start:],
        re.DOTALL | re.MULTILINE,
    )
    if block_match is None:
        pytest.fail(
            "could not locate end of _REASON_TO_EXIT mapping (no dedented "
            "closing brace on its own line)"
        )
    block = block_match.group(1)
    # Reason codes are bare-quoted dict keys at the start of a line in
    # the mapping body. Allow optional dotted depth beyond two so a
    # future ``signer.cert.expired``-style code is also captured.
    pattern = re.compile(r'^\s*"([a-z_]+(?:\.[a-z_]+)+)"\s*:', re.MULTILINE)
    return pattern.findall(block)


def _extract_public_all_exports(ingest_source: str) -> list[str]:
    """Pull every symbol listed in ``__all__`` in aphelion_ingest.py."""
    match = re.search(r"__all__\s*=\s*\[(.*?)\]", ingest_source, re.DOTALL)
    if match is None:
        pytest.fail("could not locate __all__ in aphelion_ingest.py")
    return re.findall(r'["\']([A-Za-z_][\w]*)["\']', match.group(1))


# ---------------------------------------------------------------------------
# Tests — one assertion per symbol category, with the symbol set named in
# the failure message so a missing entry is easy to fix.
# ---------------------------------------------------------------------------


def test_impl_spec_exists() -> None:
    """Sanity: the impl spec file is at the expected path."""
    assert _IMPL_SPEC.is_file(), (
        f"impl spec missing at {_IMPL_SPEC}; "
        "consistency check cannot run"
    )


def test_cli_flags_are_documented() -> None:
    """Every ``p_ingest.add_argument(...)`` name appears in the impl spec."""
    cli_source = _read(_CLI_MODULE)
    spec = _read(_IMPL_SPEC)
    flags = _extract_ingest_cli_flags(cli_source)
    assert flags, "no ingest CLI arguments extracted — extraction regex broken"
    missing = [flag for flag in flags if flag not in spec]
    assert not missing, (
        f"ingest CLI args missing from {_IMPL_SPEC.name}: {missing!r}. "
        f"Extracted args were: {flags!r}"
    )


def test_env_vars_are_documented() -> None:
    """Every ``PARALLAX_...`` env var read by the ingest path is in the impl spec.

    Scope is restricted to env vars actually read by ingest code paths —
    ``parallax/cli.py`` and ``parallax/apex/aphelion_ingest.py``. Other env
    vars (e.g. ``PARALLAX_BIND_HOST`` for serve) are out of scope.
    """
    cli_source = _read(_CLI_MODULE)
    ingest_source = _read(_INGEST_MODULE)
    spec = _read(_IMPL_SPEC)
    all_env = _extract_parallax_env_vars(cli_source, ingest_source)
    # Filter to env vars the ingest path actually touches. The contract is:
    # only env vars whose name appears inside ``_cmd_ingest`` (cli.py) are
    # in scope. We identify the ingest function by locating
    # ``def _cmd_ingest(`` and slicing to the next top-level ``def `` line.
    ingest_fn_start = cli_source.find("def _cmd_ingest(")
    if ingest_fn_start == -1:
        pytest.fail("could not locate _cmd_ingest function in cli.py")
    next_fn = cli_source[ingest_fn_start + 1 :].find("\ndef ")
    ingest_fn_end = (
        ingest_fn_start + 1 + next_fn if next_fn != -1 else len(cli_source)
    )
    ingest_fn_source = cli_source[ingest_fn_start:ingest_fn_end]
    ingest_path_env = {
        name for name in all_env if name in ingest_fn_source
    }
    assert ingest_path_env, (
        "no PARALLAX_* env vars extracted from _cmd_ingest — extraction "
        "regex or function-bounds heuristic broken"
    )
    missing = sorted(name for name in ingest_path_env if name not in spec)
    assert not missing, (
        f"ingest-path env vars missing from {_IMPL_SPEC.name}: {missing!r}. "
        f"Ingest-path env vars were: {sorted(ingest_path_env)!r}"
    )


def test_error_classes_are_documented() -> None:
    """Every ``class .*Error`` defined in aphelion_ingest.py is named in the spec."""
    ingest_source = _read(_INGEST_MODULE)
    spec = _read(_IMPL_SPEC)
    classes = _extract_error_classes(ingest_source)
    assert classes, "no error classes extracted — extraction regex broken"
    missing = [cls for cls in classes if cls not in spec]
    assert not missing, (
        f"error classes missing from {_IMPL_SPEC.name}: {missing!r}. "
        f"Extracted classes were: {classes!r}"
    )


def test_reason_codes_are_documented() -> None:
    """Every reason_code in ``_REASON_TO_EXIT`` is mentioned in the impl spec."""
    ingest_source = _read(_INGEST_MODULE)
    spec = _read(_IMPL_SPEC)
    codes = _extract_reason_codes(ingest_source)
    assert codes, "no reason codes extracted — extraction regex broken"
    missing = [code for code in codes if code not in spec]
    assert not missing, (
        f"reason codes missing from {_IMPL_SPEC.name}: {missing!r}. "
        f"Extracted codes were: {codes!r}"
    )


def test_public_all_exports_are_documented() -> None:
    """Every symbol in ``aphelion_ingest.__all__`` is named in the impl spec.

    The impl spec §4.3 promises an authoritative public-surface table.
    This test holds that promise: any new public export must be added to
    the spec before the consistency gate will accept the PR.
    """
    ingest_source = _read(_INGEST_MODULE)
    spec = _read(_IMPL_SPEC)
    exports = _extract_public_all_exports(ingest_source)
    assert exports, "no __all__ exports extracted — extraction regex broken"
    missing = [name for name in exports if name not in spec]
    assert not missing, (
        f"public __all__ exports missing from {_IMPL_SPEC.name}: {missing!r}. "
        f"Extracted exports were: {exports!r}"
    )
