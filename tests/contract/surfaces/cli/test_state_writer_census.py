"""No CLI command writes tree state around the daemon, bar a named, reasoned few.

The in-process ``state_transaction`` writes ``state.json`` directly. The
census walks the live command tree, reads the module behind every leaf,
and fails on any that imports it unless the module is on the exemption
list below with the reason it may still stand. A module that stops
importing it must leave the list, so the list shrinks as writers move.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from collections.abc import Iterator
from typing import Final

import click
import pytest
import typer

from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.contract

_MUTATION_MODULE: Final = "eawf.surfaces.cli._mutation"
_WRITER: Final = "state_transaction"

#: Command modules still allowed the in-process writer, and why.
STATE_WRITER_EXEMPTIONS: Final[dict[str, str]] = {
    "eawf.surfaces.cli.commands.session": (
        "close and recover write through the daemon; the in-process writer is only "
        "the explicit daemonless carve-out an epoch-1 tree settles its sessions with"
    ),
    "eawf.surfaces.cli.commands.worktree": (
        "merge-back, cleanup and land write through the daemon; the in-process writer "
        "is only the explicit daemonless carve-out an epoch-1 tree settles worktrees with"
    ),
}


def _leaf_callbacks(command: click.Command) -> Iterator[object]:
    if isinstance(command, click.Group):
        for child in command.commands.values():
            yield from _leaf_callbacks(child)
    elif command.callback is not None:
        yield command.callback


def _command_modules() -> set[str]:
    root = typer.main.get_command(app)
    modules = {inspect.unwrap(cb).__module__ for cb in _leaf_callbacks(root)}  # type: ignore[arg-type]
    return {name for name in modules if name.startswith("eawf.surfaces.cli.")}


def imports_state_writer(source: str) -> bool:
    """Return whether *source* reaches the in-process writer by import or attribute."""
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == _MUTATION_MODULE
            and any(alias.name == _WRITER for alias in node.names)
        ):
            return True
        if (
            isinstance(node, ast.Attribute)
            and node.attr == _WRITER
            and isinstance(node.value, ast.Name)
            and node.value.id == "_mutation"
        ):
            return True
    return False


def _writers() -> set[str]:
    return {
        name
        for name in _command_modules()
        if imports_state_writer(inspect.getsource(importlib.import_module(name)))
    }


def test_no_command_module_writes_state_outside_the_exemptions() -> None:
    assert sorted(_writers() - set(STATE_WRITER_EXEMPTIONS)) == []


def test_every_exemption_still_names_a_writer() -> None:
    """A module that moved to the daemon leaves the list."""
    assert sorted(set(STATE_WRITER_EXEMPTIONS) - _writers()) == []


def test_every_exemption_states_its_reason() -> None:
    for module, reason in STATE_WRITER_EXEMPTIONS.items():
        assert reason.strip(), module


def test_the_census_sees_the_live_tree() -> None:
    """Boundary: the walk reaches split command modules, not only group roots."""
    modules = _command_modules()
    assert "eawf.surfaces.cli.commands.mcp" in modules
    assert "eawf.surfaces.cli.commands.mcp_grants" in modules


def test_the_detector_fires_on_a_real_writer() -> None:
    """Error path: the session commands import the writer, and the detector says so."""
    source = inspect.getsource(importlib.import_module("eawf.surfaces.cli.commands.session"))
    assert imports_state_writer(source)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from eawf.surfaces.cli._mutation import state_transaction\n", True),
        ("from eawf.surfaces.cli import _mutation\n_mutation.state_transaction(p)\n", True),
        ("from eawf.surfaces.cli._mutation import _proxy_enabled\n", False),
        ("", False),
    ],
)
def test_the_detector_reads_each_import_shape(source: str, expected: bool) -> None:
    assert imports_state_writer(source) is expected
