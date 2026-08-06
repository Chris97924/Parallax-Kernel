#!/usr/bin/env python3
"""Lint gate for the ``m5-entry-spec.md`` §3.1a "Aphelion auth header redaction" rule.

The spec row reads, under a heading stating these invariants *"are not
negotiable and must be enforced via lint/test"*:

    The bearer token in Aphelion HTTP request headers MUST be redacted
    (replaced with ``<REDACTED>``) before any audit-ledger write or log line.
    Lint rule: forbid passing raw ``Authorization`` value to
    ``audit_log.write()`` or ``logger.*()``.

The 2026-08-05 S4 audit found no such rule anywhere in the repo — the invariant
was documented and unenforced. This script is that rule.

Scope, stated plainly so nobody mistakes it for more than it is: it forbids
**credential-named expressions** reaching a log or audit-write call. It does
*not* try to catch every way a secret could reach a log — in particular it does
not flag ``str(exc)``, because an exception message is only conditionally
sensitive and the S4 audit rated the remaining ``str(exc)`` sites in this repo
NO. Payload safety for those is enforced at runtime instead, by
``parallax.obs.log.safe_log_warning``. Widening this rule is a deliberate
decision, not an oversight.

Usage::

    python scripts/lint_no_raw_secret_logging.py [PATH ...]

Defaults to ``parallax``. Exits 1 on any violation, printing
``path:line:col: PLX-SECRET-LOG <detail>``. Suppress a reviewed false positive
with a trailing ``# plx-allow: PLX-SECRET-LOG`` comment on the offending line.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# Identifier fragments that mark an expression as credential-bearing. Matched
# against the *rendered source* of each argument sub-expression, so
# ``headers["Authorization"]``, ``self._token`` and ``cfg.api_key`` all hit.
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|auth[_-]?header|bearer|token|api[_-]?key|apikey"
    r"|secret|password|passwd|credential)"
)

# ...except when the identifier is provably a non-secret derived value. A hash
# or a redacted placeholder is the mitigation this rule exists to encourage, so
# flagging it would push authors the wrong way.
#
# Applied per *identifier*, never to the whole rendered expression. Testing the
# whole render inverts the rule: in `cfg.token_hash.raw_token` the safe `_hash`
# fragment would suppress the raw `.raw_token` sitting right beside it, so one
# derived value anywhere in a dotted chain would launder every credential in it.
_SAFE_SUFFIX_PATTERN = re.compile(
    r"(?i)(_hash|_digest|_sha256|_fingerprint|_redacted|_present|_configured"
    r"|_env|_name|_len|_length|_count|_expiry|_expires_at)$"
)

# Identifier-ish words inside a rendered expression. Subscript string literals
# are included by construction: `self.headers['Authorization']` renders with the
# quoted text, so `Authorization` is one of the words this finds.
_WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_REDUCING_CALLS = frozenset({"len", "bool"})

_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
_LOG_RECEIVER = re.compile(r"(?i)(^|[._])(log|logger|logging)$")
_PROJECT_LOG_HELPERS = frozenset({"safe_log_warning", "_safe_log_warning"})

SUPPRESSION = "# plx-allow: PLX-SECRET-LOG"
RULE_ID = "PLX-SECRET-LOG"


def _render(node: ast.AST) -> str:
    """Source text for a node, tolerant of anything ``ast.unparse`` dislikes."""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover — defensive; unparse handles all real nodes
        return ""


def _is_sink(call: ast.Call) -> str | None:
    """Return a sink description if this call writes to a log or the audit ledger."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in _PROJECT_LOG_HELPERS:
        return f"{func.id}()"
    if isinstance(func, ast.Attribute):
        receiver = _render(func.value)
        if func.attr in _LOG_METHODS and _LOG_RECEIVER.search(receiver):
            return f"{receiver}.{func.attr}()"
        if func.attr in _PROJECT_LOG_HELPERS:
            return f"{receiver}.{func.attr}()"
        if func.attr == "write" and "audit_log" in receiver.lower():
            return f"{receiver}.write()"
    return None


def _is_raw_credential(rendered: str) -> bool:
    """True when any single identifier in ``rendered`` names an undisguised secret.

    Word-by-word, deliberately. The suppression list describes *one identifier*
    being a derived value, so applying it to the whole expression would let a
    single safe word launder its neighbours — ``cfg.token_hash.raw_token`` is
    the shape that motivated this: the ``_hash`` suffix is real, and the raw
    token beside it is real too, and only the second one matters here.
    """
    for word in _WORD_PATTERN.findall(rendered):
        if _SECRET_PATTERN.search(word) and not _SAFE_SUFFIX_PATTERN.search(word):
            return True
    return False


def _secret_subexpressions(node: ast.AST) -> list[str]:
    """Rendered credential-bearing sub-expressions inside an argument.

    Walks the whole argument so f-strings, dict literals, ``str()``/``repr()``
    wrappers and ``%``-formatting tuples are all covered — a secret does not
    stop being a secret because it was interpolated on the way in.

    Only *expressions* (names, attributes, subscripts) are considered. String
    literals are deliberately not scanned: a log message that merely talks about
    a bearer token is prose, not a leak, and an inlined literal credential in
    source is the secret-scanner's job rather than this rule's. Scanning them
    produced exactly one false positive on the existing tree
    (``parallax/server/app.py`` warning that ``/metrics`` is reachable without a
    bearer token) and would have taught authors to reword warnings instead of
    redacting values.
    """
    found: list[str] = []

    def walk(current: ast.AST) -> None:
        if (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id in _REDUCING_CALLS
        ):
            # One-way reductions: the value cannot be recovered from the result,
            # and ``token_len``/``token_present`` metadata is precisely what the
            # rule wants authors to reach for instead of the raw value.
            return
        if isinstance(current, ast.Compare) or (
            isinstance(current, ast.UnaryOp) and isinstance(current.op, ast.Not)
        ):
            # Boolean context — what is emitted is the comparison result.
            return
        if isinstance(current, ast.IfExp):
            # ``"bearer" if token else "none"`` emits the branches, never the
            # credential itself, so only the branches are inspected.
            walk(current.body)
            walk(current.orelse)
            return
        if isinstance(current, ast.Name | ast.Attribute | ast.Subscript):
            rendered = _render(current)
            if rendered and _is_raw_credential(rendered):
                found.append(rendered)
            # ``ast.unparse`` of an attribute/subscript already includes every
            # component, so its children cannot hold a match this missed.
            return
        for child in ast.iter_child_nodes(current):
            walk(child)

    walk(node)
    return found


class _Checker(ast.NodeVisitor):
    def __init__(self, path: Path, source_lines: list[str]) -> None:
        self.path = path
        self.lines = source_lines
        self.violations: list[tuple[int, int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast.NodeVisitor API
        sink = _is_sink(node)
        if sink is not None:
            arguments: list[ast.AST] = list(node.args)
            arguments += [kw.value for kw in node.keywords]
            seen: set[str] = set()
            for argument in arguments:
                for rendered in _secret_subexpressions(argument):
                    line = getattr(argument, "lineno", node.lineno)
                    if SUPPRESSION in self.lines[line - 1] or rendered in seen:
                        continue
                    seen.add(rendered)
                    self.violations.append(
                        (
                            line,
                            getattr(argument, "col_offset", node.col_offset) + 1,
                            f"raw credential {rendered!r} passed to {sink}",
                        )
                    )
        self.generic_visit(node)


def check_file(path: Path) -> list[str]:
    """Return formatted violation strings for one file."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}:{exc.offset}: {RULE_ID} could not parse file ({exc.msg})"]
    checker = _Checker(path, source.splitlines())
    checker.visit(tree)
    return [
        f"{path}:{line}:{col}: {RULE_ID} {detail}" for line, col, detail in checker.violations
    ]


def iter_python_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_file() and target.suffix == ".py":
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(p for p in target.rglob("*.py") if "__pycache__" not in p.parts))
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["parallax"], type=Path)
    args = parser.parse_args(argv)

    targets = args.paths or [Path("parallax")]
    violations: list[str] = []
    for path in iter_python_files(list(targets)):
        violations.extend(check_file(path))

    for violation in violations:
        print(violation)
    if violations:
        # ASCII only: this runs on Windows consoles too, where the default
        # cp950 stdout encoding turns non-ASCII into mojibake at best.
        print(
            f"\n{len(violations)} violation(s) of {RULE_ID} "
            "(m5-entry-spec.md 3.1a - Aphelion auth header redaction).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
