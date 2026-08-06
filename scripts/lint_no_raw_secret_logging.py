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

Known limitations — stated rather than papered over
---------------------------------------------------

This is a name-and-label heuristic over the AST, not taint analysis. It resolves
**one** level of local dataflow: a mapping bound by simple assignment to a name
in the same scope, then passed to a sink. Everything below is out of reach, and
listing it is the point — a gate whose edges are documented is worth more than
one that implies completeness it does not have:

* mappings built or returned by another function, or reached through an
  attribute or subscript (``self.headers``, ``cfg["hdrs"]``);
* mappings built dynamically — ``dict(...)``, comprehensions, ``{**a, **b}``;
* mutation after binding — ``extra["Authorization"] = v``, ``extra.update(...)``;
* non-literal keys (``{HEADER_CONST: v}``), since the label is what it reads;
* credentials in a value whose *name* says nothing and whose *label* says
  nothing either.

The consequence is bounded, and that is why the boundary is acceptable: payload
safety at runtime is enforced by ``parallax.obs.log.safe_log_warning``, which
default-denies every extra it is handed regardless of how the value got there.
This gate exists to fail the build at the line someone writes the obvious form,
not to be the last line of defence.

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
from collections.abc import Sequence
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


def _is_bounded_value(node: ast.AST) -> bool:
    """True when this expression provably cannot carry a credential.

    Used only to decide whether a value sitting under a *credential-named label*
    is acceptable. The label already says "this slot holds a secret", so the bar
    is that the expression is a literal placeholder, a one-way reduction, a
    boolean, or an explicitly derived identifier.
    """
    if isinstance(node, ast.Constant):
        # A placeholder such as "<REDACTED>", or a number. An inlined literal
        # credential in source is the secret-scanner's job, not this rule's.
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in _REDUCING_CALLS
    if isinstance(node, ast.Compare) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
    ):
        return True
    if isinstance(node, ast.IfExp):
        return _is_bounded_value(node.body) and _is_bounded_value(node.orelse)
    if isinstance(node, ast.Name | ast.Attribute | ast.Subscript):
        rendered = _render(node)
        # `authorization=token_hash` is the mitigation; `authorization=value` is
        # the flow the spec rule exists to stop.
        return bool(rendered) and bool(_SAFE_SUFFIX_PATTERN.search(rendered.split(".")[-1]))
    return False


def _labelled_entries(call: ast.Call, arguments: Sequence[ast.AST]) -> list[tuple[str, ast.AST]]:
    """``(label, value)`` pairs where a caller names the slot it is filling.

    Two shapes, both of which put the credential's *name* somewhere the
    expression walker cannot see it:

    * ``logger.info("req", extra={"Authorization": value})`` — the label is a
      mapping key, the value an unmarked local.
    * ``audit_log.write(authorization=value)`` — the label is a keyword name.

    ``arguments`` is the resolved argument list, so a mapping bound to a name
    one line earlier is included. Dict literals are collected recursively, so a
    nested ``extra={"h": {...}}`` is covered too. Standalone string literals are
    deliberately NOT collected: log prose mentioning a bearer token is not a leak.
    """
    entries: list[tuple[str, ast.AST]] = [(kw.arg, kw.value) for kw in call.keywords if kw.arg]
    for argument in arguments:
        for node in ast.walk(argument):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    entries.append((key.value, value))
    return entries


def _mapping_bindings(scope: ast.AST) -> dict[str, list[ast.Dict]]:
    """``name -> dict literals`` bound to it by simple assignment in one scope.

    One level of dataflow, which is what closes the ordinary two-step form::

        extra = {"Authorization": value}
        logger.info("request", extra=extra)

    Nested functions, lambdas and classes are not descended into — they are
    separate scopes, and pretending otherwise would report bindings that never
    reach the sink. A name assigned more than once contributes every mapping it
    was ever bound to: over-approximating is the safe direction for a gate whose
    false negatives are silent and whose false positives are one comment away.
    """
    bindings: dict[str, list[ast.Dict]] = {}

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue  # its own scope
            if isinstance(child, ast.Assign) and isinstance(child.value, ast.Dict):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        bindings.setdefault(target.id, []).append(child.value)
            elif (
                isinstance(child, ast.AnnAssign)
                and isinstance(child.value, ast.Dict)
                and isinstance(child.target, ast.Name)
            ):
                bindings.setdefault(child.target.id, []).append(child.value)
            walk(child)

    walk(scope)
    return bindings


class _Checker(ast.NodeVisitor):
    def __init__(self, path: Path, source_lines: list[str]) -> None:
        self.path = path
        self.lines = source_lines
        self.violations: list[tuple[int, int, str]] = []
        self.bindings: dict[str, list[ast.Dict]] = {}

    def _enter_scope(self, node: ast.AST) -> None:
        outer = self.bindings
        self.bindings = _mapping_bindings(node)
        self.generic_visit(node)
        self.bindings = outer

    def visit_Module(self, node: ast.Module) -> None:  # noqa: N802 — ast.NodeVisitor API
        self._enter_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._enter_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._enter_scope(node)

    def _resolved_arguments(self, call: ast.Call) -> list[ast.AST]:
        """Call arguments, with names substituted by the mappings they hold.

        The bound literal is *added*, not swapped in, so a violation is reported
        at the line where the credential entered the mapping — which is where a
        reader has to go to fix it — while the message still names the sink.
        """
        resolved: list[ast.AST] = []
        for argument in [*call.args, *(kw.value for kw in call.keywords)]:
            resolved.append(argument)
            if isinstance(argument, ast.Name):
                resolved.extend(self.bindings.get(argument.id, ()))
        return resolved

    def _record(self, node: ast.AST, fallback: ast.Call, detail: str, seen: set[str]) -> None:
        line = getattr(node, "lineno", fallback.lineno)
        if SUPPRESSION in self.lines[line - 1] or detail in seen:
            return
        seen.add(detail)
        self.violations.append(
            (line, getattr(node, "col_offset", fallback.col_offset) + 1, detail)
        )

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast.NodeVisitor API
        sink = _is_sink(node)
        if sink is not None:
            seen: set[str] = set()
            # (a) the expression itself names a credential.
            arguments = self._resolved_arguments(node)
            for argument in arguments:
                for rendered in _secret_subexpressions(argument):
                    self._record(
                        argument, node, f"raw credential {rendered!r} passed to {sink}", seen
                    )
            # (b) the expression is anonymous but the *label* names a credential
            # — `extra={"Authorization": value}` is the exact raw-header flow the
            # spec rule prohibits, and (a) cannot see it.
            for label, value in _labelled_entries(node, arguments):
                if not _is_raw_credential(label) or _is_bounded_value(value):
                    continue
                if _secret_subexpressions(value):
                    # The value already names itself, so (a) reported it.
                    # Reporting one leak twice under two rules is noise.
                    continue
                self._record(
                    value,
                    node,
                    f"credential-labelled entry {label!r} carries "
                    f"{_render(value)!r} into {sink}",
                    seen,
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


#: Scanned when the CLI is invoked with no arguments.
DEFAULT_TARGETS = [Path("parallax")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # The default must already hold ``Path`` objects: argparse applies ``type``
    # to command-line strings only, never to a list default, so a plain
    # ``["parallax"]`` reaches ``iter_python_files`` as ``str`` and dies on
    # ``.is_file()`` — i.e. the documented no-argument invocation crashed.
    parser.add_argument("paths", nargs="*", default=DEFAULT_TARGETS, type=Path)
    args = parser.parse_args(argv)

    targets: list[Path] = args.paths or DEFAULT_TARGETS
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
