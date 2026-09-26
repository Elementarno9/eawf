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

A second check binds the kind taxonomy to the gate tier ladder: every
file under a ``tests/<kind>/`` directory runs at that kind's gate tier
(wave, iter, or release), so a file there that declares a kind marker
whose tier differs -- an ``e2e`` marker under ``tests/unit/`` -- sits on
the wrong rung and is flagged by :func:`check_tier_ladder`.

The check walks a module AST and inspects every ``import`` /
``from ... import`` statement, so a string literal that merely mentions
``subprocess`` never false-fires. A single misplaced import can carry a
line-level ``# noqa: EAWF024`` waiver (e.g. a deliberate lint-test
fixture) which this check honors. The dispatcher scans the git-tracked
``tests/`` tree and applies the import rule to ``tests/unit/`` files
only; :func:`check_source` is content-only, so
the idle-contract gate can prove it flags a bad snippet and clears a
clean one without touching the filesystem.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from eawf.platform.lint.kind_taxonomy import (
    GRANDFATHERED_MARKER_CONFLICTS,
    KIND_MARKERS,
    TIER_RUNTIME_BUDGET_SECONDS,
    GateTier,
    TestKind,
    gate_tier_for_kind,
    kind_for_test_path,
    repo_relative_test_path,
)

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


@dataclass(frozen=True)
class TierLadderViolation:
    """One EAWF024 finding: a kind marker on the wrong gate-tier rung.

    Attributes:
        lineno: 1-based line of the offending marker.
        col_offset: 0-based column of the marker node.
        directory_kind: The kind the file's directory places it under.
        declared_kind: The kind the marker declares.
    """

    lineno: int
    col_offset: int
    directory_kind: TestKind
    declared_kind: TestKind

    @property
    def code(self) -> str:
        """Return the rule code (``EAWF024``)."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``line:col: CODE reason`` one-liner naming both rungs."""
        placed = gate_tier_for_kind(self.directory_kind)
        needed = gate_tier_for_kind(self.declared_kind)
        reason = (
            f"{self.declared_kind.value!r} test runs at the {needed.value} tier "
            f"({TIER_RUNTIME_BUDGET_SECONDS[needed]}s budget) but tests/"
            f"{self.directory_kind.value}/ is the {placed.value} tier "
            f"({TIER_RUNTIME_BUDGET_SECONDS[placed]}s budget); move it under "
            f"tests/{self.declared_kind.value}/"
        )
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {reason}"


def _declared_kind(node: ast.Attribute) -> TestKind | None:
    """Return the kind a ``pytest.mark.<kind>`` or ``mark.<kind>`` node declares, or ``None``."""
    if node.attr not in KIND_MARKERS:
        return None
    owner = node.value
    is_mark = (
        isinstance(owner, ast.Attribute)
        and owner.attr == "mark"
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "pytest"
    ) or (isinstance(owner, ast.Name) and owner.id == "mark")
    return TestKind(node.attr) if is_mark else None


def check_tier_ladder(
    source: str,
    *,
    path: str,
    grandfather: frozenset[str] = GRANDFATHERED_MARKER_CONFLICTS,
) -> list[TierLadderViolation]:
    """Return tier-ladder violations for the test file at ``path``.

    A file under a kind directory runs at that kind's gate tier. Every
    ``pytest.mark.<kind>`` (or ``mark.<kind>``) it carries whose kind maps
    to a different tier is a violation; a same-tier kind marker is left to
    the collection-time marker-conflict check.

    Args:
        source: Python source text of the file.
        path: The file's path, absolute or repo-relative; it decides the
            directory kind and hence the tier.
        grandfather: Repo-relative paths exempt from the contract.

    Returns:
        Violations sorted by ``(lineno, col_offset)``; empty for a file
        outside every kind directory, a grandfathered file, or a file
        whose markers all sit on its own rung. A marker whose line carries
        ``# noqa: EAWF024`` is exempt.

    Raises:
        SyntaxError: if ``source`` is not parseable Python.
    """
    directory_kind = kind_for_test_path(path)
    if directory_kind is None or repo_relative_test_path(path) in grandfather:
        return []
    placed: GateTier = gate_tier_for_kind(directory_kind)
    tree = ast.parse(source, filename=path)
    source_lines = source.splitlines()
    violations: list[TierLadderViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        declared = _declared_kind(node)
        if declared is None or gate_tier_for_kind(declared) is placed:
            continue
        if 0 < node.lineno <= len(source_lines) and _WAIVER_PATTERN.search(
            source_lines[node.lineno - 1]
        ):
            continue
        violations.append(
            TierLadderViolation(
                lineno=node.lineno,
                col_offset=node.col_offset,
                directory_kind=directory_kind,
                declared_kind=declared,
            )
        )
    violations.sort(key=lambda violation: (violation.lineno, violation.col_offset))
    return violations
