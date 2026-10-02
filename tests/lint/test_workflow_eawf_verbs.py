"""Every ``eawf`` command a GitHub workflow runs names a verb the installed CLI carries.

A workflow step that calls a retired verb fails only on the runner that executes it,
which for the Windows smoke job is the phase pull request itself.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import typer

from eawf.surfaces.cli.app import app

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: ``eawf`` as a command word: after ``uv run``, after ``--``, or at the start of a line.
_INVOCATION = re.compile(r"(?:^|\buv run(?: [^\n]*?)? -- |\buv run )eawf((?: [^\n]*)?)$")

#: A shell redirect (``>/dev/null``, ``*> $null``) and everything after it.
_REDIRECT = re.compile(r"\s[*\d]?>.*$")


def _verb_path(argv: list[str], root: click.Group) -> tuple[str, ...] | None:
    """Return the command path ``argv`` names, ``None`` when a word is not on the tree.

    Root options are skipped with their value; a leaf command or the first option after
    a group ends the path. A bare root call (``eawf --help``) names the empty path.
    """
    ctx = click.Context(root)
    takes_value = {
        flag
        for param in root.params
        if isinstance(param, click.Option) and not param.is_flag
        for flag in param.opts
    }
    words = iter(argv)
    command: click.Command = root
    path: list[str] = []
    for word in words:
        if word.startswith("-"):
            if command is root and word in takes_value:
                next(words, None)
                continue
            if command is root:
                continue
            break
        if not isinstance(command, click.Group):
            break
        child = command.get_command(ctx, word)
        if child is None:
            return None
        command = child
        path.append(word)
    return tuple(path)


def _invocations() -> list[tuple[str, int, list[str]]]:
    """Return every ``eawf`` invocation in the workflows as (file, line, argv)."""
    found: list[tuple[str, int, list[str]]] = []
    for workflow in sorted(_WORKFLOWS.glob("*.yaml")):
        for number, line in enumerate(workflow.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("#", "- name:", "name:")):
                continue
            match = _INVOCATION.search(stripped)
            if match is not None:
                tail = _REDIRECT.sub("", match.group(1))
                found.append((workflow.name, number, shlex.split(tail, posix=True)))
    return found


def test_the_scan_finds_the_windows_smoke_invocations() -> None:
    argvs = [argv for name, _, argv in _invocations() if name == "ci.yaml"]
    assert any(argv[:2] == ["--no-input", "init"] for argv in argvs)
    assert any(argv[:1] == ["hook"] for argv in argvs)


def test_every_workflow_invocation_names_a_live_verb() -> None:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    retired = [
        f"{name}:{number}: eawf {' '.join(argv)}"
        for name, number, argv in _invocations()
        if _verb_path(argv, root) is None
    ]
    assert retired == []


def test_a_retired_verb_is_reported() -> None:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    assert _verb_path(["project", "init", "WSMK"], root) is None
    assert _verb_path(["--workspace", "x", "status"], root) == ("status",)
    assert _verb_path(["--help"], root) == ()
