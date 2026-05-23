"""EAWF011 — cognitive-complexity gate (the master-variable rule).

Complexity, not lines-of-code, is the variable that predicts how hard a
function is to read, test, and change. EAWF010's physical-line cap is a
coarse rollup alarm; this rule is the precise per-function gate that
replaces LOC-as-signal with *cognitive complexity* — the SonarSource
metric that scores how much mental effort a reader spends following a
function's control flow.

Cognitive complexity is **not** cyclomatic complexity. Cyclomatic counts
independent paths (the ruff ``C901`` check covers that, warning at 10);
cognitive complexity weights *nested* control flow more heavily than flat
control flow, because a branch three levels deep costs the reader far
more than a branch at the top. The two metrics are complementary: a
function can be cyclomatically simple yet cognitively heavy (deep
nesting) or vice-versa, so the codebase gates on both.

Scoring model (a faithful, pragmatic subset of the SonarSource spec):

* **Structural increments** add ``1 + nesting`` — ``if``, ``for``,
  ``while``, ``except``, and a nested function definition. The
  ``+ nesting`` term is the *nesting penalty*: the same ``if`` costs more
  the deeper it sits.
* **Hybrid increments** add a flat ``+1`` with no nesting penalty —
  ``elif`` / ``else`` (the continuation of a conditional) and each
  ``and`` / ``or`` boolean-operator sequence. A ternary ``a if c else b``
  adds the structural ``1 + nesting`` like an inline ``if``.
* **Nesting increases** inside the body of every structural construct
  (``if`` / ``for`` / ``while`` / ``except`` / nested function). ``try``
  and ``with`` bodies do *not* raise the nesting level themselves (only
  their ``except`` handlers do), matching the SonarSource model.

The default budget is :data:`DEFAULT_MAX_COMPLEXITY` (15). A function at
or under the budget is clean; one above it is a single finding. The
budget is overridable through the ``[tool.eawf.lint.eawf011] max-complexity``
key in ``pyproject.toml`` (see :mod:`eawf.lint`), the same wiring path the
other ``EAWF`` rules use. Per-function waivers ride the ruff ``# noqa``
layer; this rule is intentionally narrow and reports every over-budget
function so the caller decides.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

RULE_CODE = "EAWF011"

# Default per-function cognitive-complexity budget. A function scoring at
# or below this is clean. Overridable via the
# ``[tool.eawf.lint.eawf011] max-complexity`` key in pyproject.
DEFAULT_MAX_COMPLEXITY = 15


@dataclass(frozen=True)
class CognitiveComplexityViolation:
    """One EAWF011 finding.

    Attributes:
        lineno: 1-based line of the offending ``def`` / ``async def``.
        col_offset: 0-based column of the function node.
        name: the function's name.
        complexity: the computed cognitive-complexity score.
        max_complexity: the budget that was exceeded.
    """

    lineno: int
    col_offset: int
    name: str
    complexity: int
    max_complexity: int

    @property
    def code(self) -> str:
        """Return the rule code (``EAWF011``)."""
        return RULE_CODE

    @property
    def reason(self) -> str:
        """Return the human-readable cause."""
        return (
            f"function {self.name!r} has cognitive complexity {self.complexity} "
            f"(max {self.max_complexity}); simplify or flatten its control flow"
        )

    def render(self) -> str:
        """Return a ``line:col: CODE reason`` style one-liner body."""
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {self.reason}"


def _score_exprs(*nodes: ast.AST | None) -> int:
    """Return the expression-level increment for one or more subtrees.

    Scores the SonarSource "fundamental" increments that live in
    expression position, walking each subtree once:

    * each :class:`ast.BoolOp` (a run of ``and`` / ``or``) — a flat ``+1``
      per sequence, regardless of operand count; mixed ``and`` / ``or``
      nest as separate ``BoolOp`` nodes and each count once;
    * each ternary :class:`ast.IfExp` — a flat ``+1``;
    * each :class:`ast.comprehension` ``if`` filter — a flat ``+1``.

    Callers pass only the parts they own — a structural construct's test
    or context expressions (each an :class:`ast.expr`), or a *simple*
    statement that owns no nested statement bodies. Compound-statement
    bodies are scored by the block walker, so passing a simple statement
    here never double-counts. ``None`` entries keep optional-field call
    sites uniform and are skipped.

    Args:
        *nodes: Expression subtrees (or simple statements) to scan;
            ``None`` entries are ignored.

    Returns:
        The summed expression-level increment across all subtrees.
    """
    total = 0
    for root in nodes:
        if root is None:
            continue
        for child in ast.walk(root):
            if isinstance(child, (ast.BoolOp, ast.IfExp)):
                total += 1
            elif isinstance(child, ast.comprehension):
                total += len(child.ifs)
    return total


class _ComplexityCounter:
    """Accumulate the cognitive-complexity score of one function body.

    The counter walks a function's *direct* control flow, tracking the
    current nesting depth so each structural construct can be charged the
    ``1 + nesting`` penalty. Nested function definitions are charged the
    structural increment and then recursed into with a raised nesting
    level, so a closure's own branches inherit the enclosing penalty.
    """

    def __init__(self) -> None:
        self.score = 0

    def run(self, body: list[ast.stmt]) -> int:
        """Score ``body`` and return the total.

        Args:
            body: The statement list of the function under analysis.

        Returns:
            The accumulated cognitive-complexity score.
        """
        self._walk_block(body, nesting=0)
        return self.score

    def _walk_block(self, body: list[ast.stmt], *, nesting: int) -> None:
        for stmt in body:
            self._walk_stmt(stmt, nesting=nesting)

    def _walk_stmt(self, stmt: ast.stmt, *, nesting: int) -> None:
        if isinstance(stmt, ast.If):
            self._score_if(stmt, nesting=nesting)
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            self._score_loop(stmt, nesting=nesting)
        elif isinstance(stmt, ast.Try):
            self._score_try(stmt, nesting=nesting)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            # ``with`` does not raise nesting itself (SonarSource): only
            # its body's own structural constructs do. Score the context
            # expressions, then recurse the body at the same level.
            self.score += _score_exprs(*(item.context_expr for item in stmt.items))
            self._walk_block(stmt.body, nesting=nesting)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A nested function is a structural increment; its body is
            # then scored one nesting level deeper.
            self.score += 1 + nesting
            self._walk_block(stmt.body, nesting=nesting + 1)
        else:
            # Any other statement: simple ones (return / assign / expr /
            # raise / ...) and compound ones the rule does not penalise
            # structurally (a nested ``class``). Charge each direct
            # expression child's increments, and recurse any nested
            # statement child at the same nesting level so a closure or
            # method defined inside still has its control flow scored.
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    self._walk_stmt(child, nesting=nesting)
                else:
                    self.score += _score_exprs(child)

    def _score_if(self, stmt: ast.If, *, nesting: int) -> None:
        # Structural increment for the ``if`` itself (+1 + nesting), plus
        # any boolean sequences / ternaries in the test.
        self.score += 1 + nesting
        self.score += _score_exprs(stmt.test)
        self._walk_block(stmt.body, nesting=nesting + 1)
        self._score_orelse(stmt.orelse, nesting=nesting)

    def _score_orelse(self, orelse: list[ast.stmt], *, nesting: int) -> None:
        if not orelse:
            return
        # An ``elif`` is parsed as a lone ``If`` inside ``orelse``; it is a
        # flat hybrid increment (+1, no nesting penalty) and then its own
        # body/else are scored at the same nesting level.
        if len(orelse) == 1 and isinstance(orelse[0], ast.If):
            elif_node = orelse[0]
            self.score += 1
            self.score += _score_exprs(elif_node.test)
            self._walk_block(elif_node.body, nesting=nesting + 1)
            self._score_orelse(elif_node.orelse, nesting=nesting)
            return
        # A plain ``else`` block: flat +1 for the continuation, body scored
        # one level deeper.
        self.score += 1
        self._walk_block(orelse, nesting=nesting + 1)

    def _score_loop(self, stmt: ast.For | ast.AsyncFor | ast.While, *, nesting: int) -> None:
        self.score += 1 + nesting
        if isinstance(stmt, ast.While):
            self.score += _score_exprs(stmt.test)
        self._walk_block(stmt.body, nesting=nesting + 1)
        # The loop's ``else`` clause is a flat continuation increment.
        if stmt.orelse:
            self.score += 1
            self._walk_block(stmt.orelse, nesting=nesting + 1)

    def _score_try(self, stmt: ast.Try, *, nesting: int) -> None:
        # ``try`` itself does not raise nesting; each ``except`` handler is
        # a structural increment and raises nesting for its own body.
        self._walk_block(stmt.body, nesting=nesting)
        for handler in stmt.handlers:
            self.score += 1 + nesting
            self._walk_block(handler.body, nesting=nesting + 1)
        if stmt.orelse:
            self._walk_block(stmt.orelse, nesting=nesting)
        if stmt.finalbody:
            self._walk_block(stmt.finalbody, nesting=nesting)


def cognitive_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Return the cognitive-complexity score of a function node.

    Args:
        node: The ``def`` / ``async def`` node to score.

    Returns:
        The SonarSource-style cognitive-complexity score (``0`` for a
        straight-line function with no branches, loops, or boolean
        sequences).
    """
    return _ComplexityCounter().run(node.body)


def check_source(
    source: str,
    *,
    filename: str = "<unknown>",
    max_complexity: int = DEFAULT_MAX_COMPLEXITY,
) -> list[CognitiveComplexityViolation]:
    """Return EAWF011 violations for ``source``.

    Each top-level and nested function whose cognitive-complexity score
    exceeds ``max_complexity`` yields one finding. A nested function is
    reported on its own line (independent of its enclosing function's
    score) so the diagnostic points at the offending closure.

    Args:
        source: Python source text to inspect.
        filename: Name used for the parse (surfaced in ``SyntaxError``).
        max_complexity: Per-function budget (defaults to
            :data:`DEFAULT_MAX_COMPLEXITY`).

    Returns:
        Violations in source order; empty when every function is within
        budget.

    Raises:
        SyntaxError: if ``source`` is not parseable Python.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[CognitiveComplexityViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        complexity = cognitive_complexity(node)
        if complexity <= max_complexity:
            continue
        violations.append(
            CognitiveComplexityViolation(
                lineno=node.lineno,
                col_offset=node.col_offset,
                name=node.name,
                complexity=complexity,
                max_complexity=max_complexity,
            )
        )
    violations.sort(key=lambda violation: (violation.lineno, violation.col_offset))
    return violations
