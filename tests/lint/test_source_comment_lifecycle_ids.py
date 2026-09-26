"""Source comments in the swept modules carry no wave ids.

A wave id such as ``P12-I03-W04`` in a comment or docstring records when a
line was written, not why it exists. The id stops meaning anything once the
wave closes, and a reader has to open the state file to decode it. These
modules were hand-rewritten so each comment states its reason instead; the
gate keeps an id from creeping back into their comments and docstrings.

Only comments and docstrings are scanned. String literals that are data,
such as the sample wave ids in rendered agent examples, are not prose about
the code and stay out of scope.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src" / "eawf"

#: The modules whose comments were rewritten to drop wave ids.
SWEPT_MODULES: tuple[str, ...] = (
    "runtime/daemon/methods/config.py",
    "surfaces/cli/commands/config.py",
    "workflow/agents/specs/roles.py",
    "surfaces/render/agents.py",
    "runtime/runtimes/claude/plugin_install.py",
    "runtime/runtimes/claude/plugin_package.py",
    "runtime/runtimes/codex/plugin_package.py",
    "surfaces/cli/commands/metrics.py",
    "kernel/state/enums.py",
    "workflow/skills/engine.py",
    "observability/doctor/checks.py",
    "surfaces/cli/commands/hook.py",
)

_WAVE_ID = re.compile(r"P\d{2}-I\d{2}-W\d{2}")

_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _prose(source: str) -> list[tuple[int, str]]:
    """Return ``(line, text)`` for every comment and docstring in *source*."""
    found = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, _DOCSTRING_OWNERS) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.append((first.lineno, first.value.value))
    return found


def _wave_ids(source: str) -> list[tuple[int, str]]:
    return [(line, match) for line, text in _prose(source) for match in _WAVE_ID.findall(text)]


@pytest.mark.parametrize("module", SWEPT_MODULES)
def test_swept_module_comments_carry_no_wave_id(module: str) -> None:
    """No comment or docstring in the module names a wave id."""
    source = (_SRC / module).read_text(encoding="utf-8")
    assert _wave_ids(source) == [], f"{module} comments name wave ids"


@pytest.mark.parametrize("module", SWEPT_MODULES)
def test_planted_wave_id_in_a_comment_reds(module: str) -> None:
    """A wave id planted in a comment of the real module is caught."""
    source = (_SRC / module).read_text(encoding="utf-8")
    planted = f"{source}\n# Added in P99-I01-W01.\n"
    assert [match for _, match in _wave_ids(planted)] == ["P99-I01-W01"]


def test_planted_wave_id_in_a_docstring_reds() -> None:
    """A wave id planted in a docstring is caught, not only in comments."""
    source = 'def f() -> None:\n    """Added in P99-I01-W01."""\n'
    assert _wave_ids(source) == [(2, "P99-I01-W01")]


def test_wave_id_in_a_data_literal_is_not_prose() -> None:
    """A wave id held as data, outside a comment or docstring, is ignored."""
    source = 'EXAMPLE = {"wave_id": "P00-I01-W01"}\n'
    assert _wave_ids(source) == []
