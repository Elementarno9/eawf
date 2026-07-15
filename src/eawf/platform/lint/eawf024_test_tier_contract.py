"""EAWF024 — test-tier contract for the ``tests/unit/`` tier.

The test suite is partitioned into tiers by directory: ``tests/unit/``
holds fast, in-process unit tests; ``tests/integration/`` holds tests
that cross module boundaries or drive the CLI; ``tests/tui/`` holds the
Textual TUI tests. The tier a test lives in is a contract, not a label:
a "unit" test that shells out to a subprocess, drives the Typer app
through a ``CliRunner``, or mounts a ``textual`` widget is a heavier,
slower, mislabeled integration/TUI test that inflates the unit tier's
runtime and blurs the tier boundary.

This rule flags the three import shapes that mark a ``tests/unit/`` file
as non-unit:

1. ``import subprocess`` (or ``from subprocess import ...``) — a unit
   test spawns no process.
2. ``import textual`` / ``from textual... import ...`` — a unit test
   mounts no widget (that is the TUI tier).
3. ``CliRunner`` imported by name (``from typer.testing import
   CliRunner``) — a unit test does not drive the CLI app.

The check walks a module AST and inspects every ``import`` /
``from ... import`` statement, so a string literal that merely mentions
``subprocess`` never false-fires. A single misplaced import can carry a
line-level ``# noqa: EAWF024`` waiver (e.g. a deliberate lint-test
fixture) which this check honors. The dispatcher scopes the scan to the
git-tracked ``tests/unit/`` tree; the check itself is content-only, so
the idle-contract gate can prove it flags a bad snippet and clears a
clean one without touching the filesystem.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

RULE_CODE = "EAWF024"

#: Repo-relative prefix of the unit tier. The dispatcher restricts the
#: scan to files beneath this directory; :func:`is_unit_tier_path` is the
#: single predicate both the hook and its tests share.
UNIT_TIER_ROOT = "tests/unit/"

#: Top-level module names whose import marks a unit test as non-unit. A
#: dotted import (``textual.widgets``) is matched on its head segment.
BANNED_MODULES: frozenset[str] = frozenset({"subprocess", "textual"})

#: Imported symbol names whose presence marks a unit test as non-unit.
#: ``CliRunner`` is a name (``from typer.testing import CliRunner``), not
#: a module, so it is matched on the imported alias rather than the
#: module head.
BANNED_NAMES: frozenset[str] = frozenset({"CliRunner"})

#: A line-level waiver: an offending import is exempt when its own source
#: line carries a ``# noqa: EAWF024`` marker. Reserved for deliberate
#: fixtures (this rule's own lint test plants one).
_WAIVER_PATTERN = re.compile(r"#\s*noqa:\s*EAWF024\b")


@dataclass(frozen=True)
class TierViolation:
    """One EAWF024 finding.

    Attributes:
        lineno: 1-based line of the offending ``import`` statement.
        col_offset: 0-based column of the import node.
        imported: The banned token that tripped the rule (a module head
            such as ``subprocess`` / ``textual`` or an imported name such
            as ``CliRunner``).
    """

    lineno: int
    col_offset: int
    imported: str

    @property
    def code(self) -> str:
        """Return the rule code (``EAWF024``)."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``line:col: CODE reason`` style one-liner body."""
        reason = (
            f"unit-tier test imports {self.imported!r}; a test needing it belongs "
            "under tests/integration/ or tests/tui/, not tests/unit/"
        )
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {reason}"


def is_unit_tier_path(path: str) -> bool:
    """Return whether ``path`` is a Python file under the unit tier.

    Accepts both a repo-relative path (``tests/unit/test_x.py``) and an
    absolute one (``/repo/tests/unit/test_x.py``) so an operator can pass
    an explicit file to the hook while the whole-tree scan feeds
    repo-relative paths.

    Args:
        path: A candidate path (any slash flavour, relative or absolute).

    Returns:
        ``True`` when ``path`` (back-slashes folded) is a ``.py`` file
        whose path carries the :data:`UNIT_TIER_ROOT` segment, else
        ``False``.
    """
    norm = path.replace("\\", "/")
    if not norm.endswith(".py"):
        return False
    return norm.startswith(UNIT_TIER_ROOT) or f"/{UNIT_TIER_ROOT}" in norm


def _banned_tokens(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Return the banned tokens an import node carries, in source order.

    An ``import a, b.c`` node yields the head segment of each banned
    module. A ``from m import x, y`` node yields the banned module head
    (when ``m``'s head is banned) followed by each banned imported name.
    A conforming import yields an empty list.
    """
    tokens: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            head = alias.name.split(".", 1)[0]
            if head in BANNED_MODULES:
                tokens.append(head)
        return tokens
    module = node.module or ""
    head = module.split(".", 1)[0]
    if head in BANNED_MODULES:
        tokens.append(head)
    for alias in node.names:
        if alias.name in BANNED_NAMES:
            tokens.append(alias.name)
    return tokens


def check_source(source: str, *, filename: str = "<unknown>") -> list[TierViolation]:
    """Return EAWF024 violations for ``source``.

    The check is content-only: it flags every ``import`` /
    ``from ... import`` of a :data:`BANNED_MODULES` module or a
    :data:`BANNED_NAMES` name, regardless of ``filename`` (the
    unit-tier scoping is the dispatcher's job). An offending import whose
    own line carries a ``# noqa: EAWF024`` marker is exempt.

    Args:
        source: Python source text to inspect.
        filename: name used for the parse (surfaced in ``SyntaxError``).

    Returns:
        Violations sorted by ``(lineno, col_offset)``. A file with no
        banned import yields an empty list.

    Raises:
        SyntaxError: if ``source`` is not parseable Python.
    """
    tree = ast.parse(source, filename=filename)
    source_lines = source.splitlines()
    violations: list[TierViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if 0 < node.lineno <= len(source_lines) and _WAIVER_PATTERN.search(
            source_lines[node.lineno - 1]
        ):
            continue
        for token in _banned_tokens(node):
            violations.append(
                TierViolation(
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    imported=token,
                )
            )
    violations.sort(key=lambda violation: (violation.lineno, violation.col_offset))
    return violations
