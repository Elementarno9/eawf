"""Every ``eawf`` command a hint or an operator doc names is a verb the CLI carries.

A next action, repair command or help line that names a retired or never-built verb
sends the reader to an unknown-command error at the moment they follow it. The scan
covers the two places such a command is written as a command rather than prose: a
string in a list of commands (``next_actions``, ``repair_commands``), and a
backtick-quoted ``eawf ...`` span in a source string or an operator doc. Docstrings
describe code, not commands to run, and a doc line that says a verb is retired names it
on purpose, so neither is scanned.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import click
import typer

from eawf.surfaces.cli.app import app

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "eawf"

#: The retired-verb table names every retired verb by design.
_SKIPPED_SOURCES = frozenset({_SRC / "surfaces" / "cli" / "flag_day.py"})

#: The operator-facing docs; the architecture notes record design history.
_DOCS = (
    _ROOT / "docs" / "README.md",
    *sorted((_ROOT / "docs" / "help").rglob("*.md")),
    *sorted((_ROOT / "docs" / "reference").rglob("*.md")),
    *sorted((_ROOT / "docs" / "rules").rglob("*.md")),
)

_SPAN = re.compile(r"`eawf ([^`]*)`")
_FENCED = re.compile(r"^\s*(?:\$ )?eawf (.*)$")
_WORD = re.compile(r"[a-z][a-z0-9-]*")


def _unknown(command: str, root: click.Group) -> str | None:
    """Return the command path up to its first word the tree lacks, ``None`` when live.

    The path ends at a leaf command, at a group that takes arguments instead of
    subcommands, or at the first option or placeholder word.
    """
    ctx = click.Context(root)
    node: click.Command = root
    path: list[str] = []
    for word in command.split():
        if not _WORD.fullmatch(word):
            return None
        if not isinstance(node, click.Group) or not node.list_commands(ctx):
            return None
        child = node.get_command(ctx, word)
        path.append(word)
        if child is None:
            return " ".join(path)
        node = child
    return None


def _docstrings(tree: ast.Module) -> set[int]:
    """Return the ids of the docstring nodes of *tree*."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _leading_text(node: ast.expr) -> str | None:
    """Return the literal text an element of a command list starts with."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _source_commands(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line, command) for every command *path* writes as a command."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple):
            for element in node.elts:
                text = _leading_text(element)
                if text is not None and text.startswith("eawf "):
                    yield element.lineno, text.removeprefix("eawf ")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            for match in _SPAN.finditer(node.value):
                yield node.lineno, match.group(1)


def _doc_commands(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line, command) for every backtick span and fenced command line of *path*."""
    fenced = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if "retired" in line:
            continue
        for match in _SPAN.finditer(line):
            yield number, match.group(1)
        command = _FENCED.match(line) if fenced else None
        if command is not None:
            yield number, command.group(1)


def _dead(root: click.Group) -> list[str]:
    """Return ``file:line: eawf <path>`` for every command naming a verb the tree lacks."""
    hits: list[str] = []
    sources = [
        (path, _source_commands(path))
        for path in sorted(_SRC.rglob("*.py"))
        if path not in _SKIPPED_SOURCES
    ]
    docs = [(path, _doc_commands(path)) for path in _DOCS]
    for path, commands in (*sources, *docs):
        relpath = path.relative_to(_ROOT).as_posix()
        for number, command in commands:
            dead = _unknown(command, root)
            if dead is not None:
                hits.append(f"{relpath}:{number}: eawf {dead}")
    return hits


def _root() -> click.Group:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    return root


def test_every_hint_and_operator_doc_command_names_a_live_verb() -> None:
    assert _dead(_root()) == []


def test_a_retired_or_unbuilt_verb_is_reported() -> None:
    root = _root()
    assert _unknown("prep -i", root) == "prep"
    assert _unknown("roadmap", root) == "roadmap"
    assert _unknown("wave list", root) == "wave"
    assert _unknown("memory compact --scope P01", root) is None
    assert _unknown("milestone try MS-01", root) == "milestone try"
    assert _unknown("help upgrade-from-0.6", root) is None
    assert _unknown("config get <path>", root) is None


def test_a_command_list_and_a_backtick_span_are_scanned_and_a_docstring_is_not(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        '"""Run `eawf docstring-verb`."""\n'
        'ACTIONS = ["eawf listed-verb", f"eawf formatted-verb {1}"]\n'
        'HINT = "run `eawf spanned-verb` first"\n'
        'PROSE = "eawf prose-verb is not a command"\n',
        encoding="utf-8",
    )
    assert sorted(command for _, command in _source_commands(probe)) == [
        "formatted-verb ",
        "listed-verb",
        "spanned-verb",
    ]


def test_the_milestone_try_section_names_a_live_verb() -> None:
    """The Milestone frame's TRY row is a command the operator can run as written."""
    from eawf.surfaces.tui.console.renderers.milestone import _section

    row = _section("try", "MS-01", 80)[0]
    command = row.split("eawf ", 1)[1].strip()
    assert _unknown(command, _root()) is None
