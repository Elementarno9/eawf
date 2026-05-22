"""EAWF003 — library-logger acquisition lint.

Enforces the AGENTS python-profile rule that library modules acquire
their logger via ``logger = logging.getLogger(__name__)`` — the
module-qualified ``__name__`` is what makes per-module log filtering and
the ``<funcname> key=value`` event grain attributable to a source file.
A hard-coded logger name (``logging.getLogger("eawf")``), an empty /
root logger (``logging.getLogger()``), or any non-``__name__`` argument
breaks that attribution and is the drift this rule flags.

The rule walks a module AST and inspects every ``getLogger(...)`` call
(both the ``logging.getLogger`` attribute form and a bare ``getLogger``
imported via ``from logging import getLogger``). A call is conforming
when its single positional argument is the name ``__name__``; it is
flagged when the argument is a string literal, the root logger (no
argument), or any other expression. This rule is the acquisition-site
sibling of EAWF001 (message shape) and EAWF002 (key naming): together
they pin the three log-discipline surfaces the conventions name.

CLI handler modules are exempt from the *library* logger convention in
the AGENTS profile, but this rule does not special-case them — call
sites that legitimately need a custom logger name carry an ``# noqa``
at the ruff layer; here the check is intentionally narrow and reports
every non-``__name__`` acquisition so the caller can decide.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

RULE_CODE = "EAWF003"

_GETLOGGER = "getLogger"


@dataclass(frozen=True)
class LoggerNameViolation:
    """One EAWF003 finding.

    Attributes:
        lineno: 1-based line of the offending ``getLogger(...)`` call.
        col_offset: 0-based column of the call node.
        reason: short human-readable cause (what the argument was).
    """

    lineno: int
    col_offset: int
    reason: str

    @property
    def code(self) -> str:
        """Return the rule code (``EAWF003``)."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``line:col: CODE reason`` style one-liner body."""
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {self.reason}"


def _is_getlogger_call(node: ast.Call) -> bool:
    """Return ``True`` if ``node`` is a ``getLogger`` call.

    Matches both ``logging.getLogger(...)`` (attribute form) and a bare
    ``getLogger(...)`` (imported name form).
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == _GETLOGGER
    if isinstance(func, ast.Name):
        return func.id == _GETLOGGER
    return False


def _classify_argument(node: ast.Call) -> str | None:
    """Return a violation reason for a ``getLogger`` call, or ``None``.

    The call is conforming (returns ``None``) when its single positional
    argument is exactly the name ``__name__``. Otherwise a short reason
    string describes the offending argument shape.
    """
    if node.keywords:
        return "getLogger should be called as getLogger(__name__), not with keyword args"
    args = node.args
    if not args:
        return "getLogger() acquires the root logger; use getLogger(__name__)"
    if len(args) > 1:
        return "getLogger takes a single __name__ argument"
    only = args[0]
    if isinstance(only, ast.Name) and only.id == "__name__":
        return None
    if isinstance(only, ast.Constant) and isinstance(only.value, str):
        return f"getLogger uses hard-coded name {only.value!r}; use getLogger(__name__)"
    return "getLogger argument is not __name__; use getLogger(__name__)"


def check_source(source: str, *, filename: str = "<unknown>") -> list[LoggerNameViolation]:
    """Return EAWF003 violations for ``source``.

    Args:
        source: Python source text to inspect.
        filename: name used for the parse (surfaced in ``SyntaxError``).

    Returns:
        Violations in source order. ``getLogger(__name__)`` calls and
        non-``getLogger`` calls produce no findings.

    Raises:
        SyntaxError: if ``source`` is not parseable Python.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[LoggerNameViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_getlogger_call(node):
            continue
        reason = _classify_argument(node)
        if reason is None:
            continue
        violations.append(
            LoggerNameViolation(
                lineno=node.lineno,
                col_offset=node.col_offset,
                reason=reason,
            )
        )
    violations.sort(key=lambda violation: (violation.lineno, violation.col_offset))
    return violations
