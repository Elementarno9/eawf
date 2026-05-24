"""Tests for EAWF011 (cognitive-complexity gate) — the master-variable rule.

Pins the SonarSource-style scoring model and the ``check_source`` surface:

- straight-line / single-branch functions score low and never fire;
- nested control flow is charged the ``1 + nesting`` penalty so a deep
  branch costs more than a flat one;
- ``elif`` / ``else`` and boolean sequences are flat hybrid increments;
- a genuinely heavy ("grade-D") function fires at the default budget;
- the threshold is configurable and the boundary (at-cap vs over-cap)
  is inclusive of the cap;
- non-parseable source raises ``SyntaxError`` (the error path).
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from eawf.platform.lint.eawf011 import (
    DEFAULT_MAX_COMPLEXITY,
    RULE_CODE,
    CognitiveComplexityViolation,
    check_source,
    cognitive_complexity,
)


def _func(src: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Parse ``src`` and return its first function node."""
    node = ast.parse(textwrap.dedent(src)).body[0]
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    return node


# --- cognitive_complexity scoring model ----------------------------------


def test_straight_line_function_scores_zero() -> None:
    assert cognitive_complexity(_func("def f(a, b):\n    c = a + b\n    return c\n")) == 0


def test_single_if_scores_one() -> None:
    assert (
        cognitive_complexity(_func("def f(a):\n    if a:\n        return 1\n    return 0\n")) == 1
    )


def test_if_elif_else_each_increment() -> None:
    # if (+1) + elif (+1) + else (+1) = 3, all flat (no nesting penalty).
    src = """
        def f(a):
            if a == 1:
                return 1
            elif a == 2:
                return 2
            else:
                return 3
    """
    assert cognitive_complexity(_func(src)) == 3


def test_nesting_penalty_compounds() -> None:
    # for (1+0) + if@1 (1+1) + if@2 (1+2) = 6: the deepest branch is the
    # most expensive, which is the whole point of the metric.
    src = """
        def f(xs):
            for x in xs:
                if x:
                    if x > 1:
                        return x
            return 0
    """
    assert cognitive_complexity(_func(src)) == 6


def test_boolean_sequence_is_flat_increment() -> None:
    # if (+1) + one and-sequence (+1) = 2, regardless of operand count.
    assert cognitive_complexity(_func("def f(a, b, c):\n    return a and b and c\n")) == 1
    src = "def f(a, b, c):\n    if a and b and c:\n        return 1\n    return 0\n"
    assert cognitive_complexity(_func(src)) == 2


def test_mixed_boolean_operators_count_each_sequence() -> None:
    # ``a and b or c`` parses as two nested BoolOp nodes -> +2.
    assert cognitive_complexity(_func("def f(a, b, c):\n    return a and b or c\n")) == 2


def test_ternary_is_one_increment() -> None:
    assert cognitive_complexity(_func("def f(a):\n    return 1 if a else 2\n")) == 1


def test_comprehension_filters_each_increment() -> None:
    src = "def f(xs):\n    return [x for x in xs if x > 0 if x < 10]\n"
    assert cognitive_complexity(_func(src)) == 2


def test_async_for_and_while_score() -> None:
    src = """
        async def f(xs):
            async for x in xs:
                if x:
                    return x
    """
    assert cognitive_complexity(_func(src)) == 3
    while_src = """
        def f(a):
            while a > 0:
                a -= 1
            return a
    """
    assert cognitive_complexity(_func(while_src)) == 1


def test_except_handler_is_structural_increment() -> None:
    # try body adds nothing; the except handler is +1 + nesting.
    src = """
        def f(a):
            try:
                return 1 / a
            except ZeroDivisionError:
                return 0
    """
    assert cognitive_complexity(_func(src)) == 1


def test_loop_else_clause_is_flat_increment() -> None:
    src = """
        def f(xs):
            for x in xs:
                if x:
                    return x
            else:
                return -1
    """
    # for (1) + if@1 (2) + for-else (1) = 4.
    assert cognitive_complexity(_func(src)) == 4


def test_nested_function_inherits_nesting_penalty() -> None:
    src = """
        def outer(xs):
            def inner(y):
                if y:
                    return 1
                return 0
            return [inner(x) for x in xs]
    """
    # inner def (1+0) + if inside inner (1+0) = 2... inner's body is one
    # level deeper than inner's def, so if@1 -> 2; total 1 + 2 = 3.
    assert cognitive_complexity(_func(src)) == 3


def test_nested_class_method_control_flow_is_scored() -> None:
    src = """
        def make():
            class C:
                def m(self, a):
                    if a:
                        return 1
                    return 0
            return C
    """
    # class is not a structural increment; method m's def (+1) and its
    # if (+1, one level deeper inside m) = 3.
    assert cognitive_complexity(_func(src)) == 3


def test_with_block_does_not_raise_nesting() -> None:
    src = """
        def f(ctx, a):
            with ctx:
                if a:
                    return 1
            return 0
    """
    # with adds nothing; if@0 (the with body is at the same nesting) -> 1.
    assert cognitive_complexity(_func(src)) == 1


# --- check_source: the gate surface --------------------------------------


def test_grade_d_function_fires_at_default_budget() -> None:
    src = textwrap.dedent(
        """
        def grade_d(items, flag):
            total = 0
            for it in items:
                if it > 0 and flag:
                    if it % 2 == 0:
                        for j in range(it):
                            if j > 5 or j < -5:
                                total += j
                            elif j == 0:
                                total -= 1
                    else:
                        while total < 100:
                            total += 1
                elif it < 0:
                    try:
                        total += 1 / it
                    except ZeroDivisionError:
                        total = 0
            return total
    """
    )
    violations = check_source(src)
    assert len(violations) == 1
    assert violations[0].code == RULE_CODE
    assert violations[0].name == "grade_d"
    assert violations[0].max_complexity == DEFAULT_MAX_COMPLEXITY
    assert violations[0].complexity > DEFAULT_MAX_COMPLEXITY


def test_simple_module_is_clean() -> None:
    src = "def a(x):\n    return x\n\ndef b(x, y):\n    return x + y\n"
    assert check_source(src) == []


def test_at_budget_is_clean_over_budget_fires() -> None:
    # Build a flat chain of exactly N independent ``if`` statements, each
    # +1, so the score equals N: at the budget it is clean, one more fires.
    def chain(n: int) -> str:
        body = "".join(f"    if a == {i}:\n        return {i}\n" for i in range(n))
        return f"def f(a):\n{body}    return -1\n"

    assert cognitive_complexity(_func(chain(5))) == 5
    assert check_source(chain(5), max_complexity=5) == []
    over = check_source(chain(6), max_complexity=5)
    assert len(over) == 1
    assert over[0].complexity == 6


def test_custom_threshold_changes_firing() -> None:
    src = "def f(xs):\n    for x in xs:\n        if x:\n            return x\n    return 0\n"
    # score is 3 (for=1, if@1=2): clean at 3, fires at 2.
    assert check_source(src, max_complexity=3) == []
    assert len(check_source(src, max_complexity=2)) == 1


def test_nested_function_reported_independently() -> None:
    # A heavy closure inside a clean outer function is reported on its own.
    src = textwrap.dedent(
        """
        def outer(xs):
            def heavy(a):
                if a == 1:
                    return 1
                elif a == 2:
                    return 2
                elif a == 3:
                    return 3
                return 0
            return [heavy(x) for x in xs]
    """
    )
    violations = check_source(src, max_complexity=2)
    names = {v.name for v in violations}
    assert "heavy" in names


def test_violations_sorted_by_position() -> None:
    src = textwrap.dedent(
        """
        def first(a):
            if a and a > 1 and a < 9:
                if a:
                    return a
            return 0

        def second(b):
            if b or b < 0 or b > 9:
                if b:
                    return b
            return 0
    """
    )
    violations = check_source(src, max_complexity=1)
    assert [v.name for v in violations] == ["first", "second"]
    assert violations[0].lineno < violations[1].lineno


def test_check_source_raises_on_syntax_error() -> None:
    with pytest.raises(SyntaxError):
        check_source("def (:\n")


# --- violation dataclass surface -----------------------------------------


def test_violation_render_shape() -> None:
    violation = CognitiveComplexityViolation(
        lineno=12, col_offset=4, name="loader", complexity=20, max_complexity=15
    )
    rendered = violation.render()
    assert rendered.startswith("12:4: EAWF011 ")
    assert "loader" in rendered
    assert "20" in rendered
    assert violation.code == RULE_CODE


def test_violation_reason_names_the_function() -> None:
    violation = CognitiveComplexityViolation(
        lineno=1, col_offset=0, name="parse", complexity=18, max_complexity=15
    )
    assert "'parse'" in violation.reason
    assert "18" in violation.reason
    assert "max 15" in violation.reason
