"""Enter on a terminal entry state names a command the installed ``eawf`` carries.

The note Enter logs is what an operator types next, so a verb the CLI retired would
send them to a ``No such command`` error.
"""

from __future__ import annotations

import click
import pytest
import typer

from eawf.surfaces.cli.app import app
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console.test_guarded_quit import FIXTURE, _Host

_COPIED = "copied: eawf "


def _resolves(argv: list[str]) -> bool:
    """Return whether ``argv`` names a command on the live tree, options aside."""
    root = typer.main.get_command(app)
    command: click.Command = root
    ctx = click.Context(root)
    for word in argv:
        if word.startswith("-") or not isinstance(command, click.Group):
            break
        child = command.get_command(ctx, word)
        if child is None:
            return False
        command = child
    return command is not root


@pytest.mark.parametrize("state_id", ["failed", "schema"])
def test_enter_on_a_terminal_entry_copies_a_live_verb(state_id: str) -> None:
    session = Session()
    session.route = "entry"
    session.entry_sel = next(
        i for i, state in enumerate(FIXTURE.proto.entry) if state.id == state_id
    )
    dispatch(Ctx(session=session, fixture=FIXTURE, host=_Host(), w=120, h=30), "Enter")
    note = session.log[-1].note
    assert note.startswith(_COPIED)
    assert _resolves(note.removeprefix(_COPIED).split()), note


def test_a_retired_verb_does_not_resolve() -> None:
    assert not _resolves(["export", "--read-only"])
    assert not _resolves(["workspace", "register", "."])
